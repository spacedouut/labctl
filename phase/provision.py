"""Create / provision / bootstrap / nextid / wait."""

from __future__ import annotations

import os
import sys
import time

from .log import append_event
from .notify import send
from .plan import (gather_plan, parse_flags, plan_path, print_plan,
                   read_plan, realize_plan)
from .qm import Qm
from .util import PhaseError, die, log, warn
from .vm import (best_guest_ip, find_vmid, next_vmid)


def _plan_for_create(cfg, qm, argv: list[str]):
    """Shared plan resolution for `create` and `provision`."""
    from .plan import PLAN_FLAGS
    specs = dict(PLAN_FLAGS)
    specs.update({"vmidout": {"bool": True, "default": False},
                  "dry-run": {"bool": True, "default": False}})
    opts, pos = parse_flags(argv, specs)
    first = pos[0] if pos else ""
    if first and (first == "planned" or os.path.isfile(plan_path(first))):
        return read_plan(first), opts
    # strip the bare name + create-only flags; gather_plan re-parses the rest
    clean = [a for a in argv if a not in ("--vmidout", "--dry-run")]
    if first:
        clean = [a for a in clean if a != first]
    plan = gather_plan(cfg, qm, clean, name_hint=first)
    return plan, opts


def cmd_vm_create(cfg, qm, argv):
    plan, opts = _plan_for_create(cfg, qm, argv)
    vmid = realize_plan(cfg, qm, plan, dry_run=opts["dry-run"])
    if opts["vmidout"]:
        sys.stdout.write(f"{vmid}\n")
    elif opts["dry-run"]:
        log(f"Dry run only — {plan['name']} (VMID {vmid}) not created.")
    else:
        log(f"Created {plan['name']} (VMID {vmid}), stopped.")
    return 0


def cmd_vm_provision(cfg, qm, argv):
    """create + start + wait for agent/IP + bootstrap (the heavy workflow)."""
    plan, opts = _plan_for_create(cfg, qm, argv)
    name = plan["name"]
    vmid = realize_plan(cfg, qm, plan, dry_run=opts["dry-run"])
    if opts["dry-run"]:
        return 0

    qm.start(vmid)
    log(f"Waiting for guest agent on {name}...")
    if not qm.wait_for_agent(vmid, timeout=120):
        die(f"guest agent never came up on {name}")
    log(f"Waiting for guest network on {name}...")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if best_guest_ip(qm, cfg, vmid):
            break
        time.sleep(2)
    else:
        die(f"guest never got an IP on {name} (networkd-wait-online stuck?)")

    steps = [k for k, v in plan["bootstrap"].items() if v]
    try:
        for step in steps:
            cmd_vm_bootstrap(cfg, qm, [name, f"--{step}", "--os", plan["os"]])
        append_event(event="vm.provisioned", vmid=vmid, name=name,
                     steps=",".join(steps) or "none")
        send(cfg, f"✓ provisioned {name}", f"VMID {vmid} · size {plan['size']} · os {plan['os']}")
    except PhaseError as e:
        append_event(event="vm.provision.failed", vmid=vmid, name=name, detail=str(e))
        send(cfg, f"✗ provision failed: {name}", str(e))
        raise
    return 0


def bootstrap_script(cfg, os_name: str, step: str) -> str | None:
    rel = cfg.get(f"bootstrap.os_overrides.{os_name}.{step}")
    if not rel:
        return None
    base = cfg.get("bootstrap.directory", "/opt/phase/bootstrap")
    return os.path.join(base, rel)


def qga_run_script(qm: Qm, vmid: int, script: str) -> None:
    if not os.path.isfile(script):
        die(f"bootstrap script not readable: {script}")
    remote = f"/tmp/phase-{os.path.basename(script)}"
    with open(script) as f:
        data = f.read()
    qm.guest_exec_stdin(
        vmid, data,
        ["bash", "-lc", f"cat > '{remote}' && chmod +x '{remote}'"],
    )
    qm.guest_exec(vmid, ["bash", "-lc", f"sudo '{remote}'"])


def cmd_vm_bootstrap(cfg, qm, argv):
    if not argv:
        die("VM name is required")
    name = argv[0]
    opts, pos = parse_flags(argv[1:], {
        "system": {"bool": True, "default": False},
        "docker": {"bool": True, "default": False},
        "tailscale": {"bool": True, "default": False},
        "os": {"default": ""},
    })
    if not (opts["system"] or opts["docker"] or opts["tailscale"]):
        opts["system"] = True
    vmid = find_vmid(qm, name)
    if not opts["os"] and os.path.isfile(plan_path(name)):
        opts["os"] = read_plan(name).get("os", "")
    if not opts["os"]:
        die(f"cannot determine OS for {name}; pass --os")
    qm.guest_exec(vmid, ["true"])
    for step in ("system", "docker", "tailscale"):
        if not opts[step]:
            continue
        script = bootstrap_script(cfg, opts["os"], step)
        if not script:
            warn(f"no {step} bootstrap script for {opts['os']}; skipping")
            continue
        log(f"Bootstrapping {step} on {name}...")
        qga_run_script(qm, vmid, script)
    append_event(event="vm.bootstrapped", vmid=vmid, name=name, os=opts["os"])
    return 0


def cmd_vm_nextid(cfg, qm, argv):
    log(str(next_vmid(qm)))
    return 0


def cmd_vm_wait(cfg, qm, argv):
    if not argv:
        die("VM name is required")
    name = argv[0]
    opts, pos = parse_flags(argv[1:], {
        "timeout": {"default": "120"},
        "ip": {"bool": True, "default": False},
    })
    vmid = find_vmid(qm, name)
    timeout = float(opts["timeout"])
    if not qm.wait_for_agent(vmid, timeout=timeout):
        die(f"guest agent never came up on {name} (timeout {timeout}s)")
    deadline = time.monotonic() + timeout
    ip = None
    while time.monotonic() < deadline:
        ip = best_guest_ip(qm, cfg, vmid)
        if ip:
            break
        time.sleep(2)
    if opts["ip"]:
        if not ip:
            die(f"no guest IP for {name} (agent is up, network not ready)")
        log(ip)
    else:
        log(f"{name}: agent up" + (f", ip {ip}" if ip else ", no ip yet"))
    return 0

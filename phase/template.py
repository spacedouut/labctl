"""Template pipeline — clone → configure → minimize → save as template.

The GCP image-family model: `tpl-<os>` is a stable family name; regenerating
creates a new template and (optionally) retires the old one. The pipeline is a
state machine with a human-in-the-loop checkpoint:

    phase template create <name>      # clone+boot+optional bootstrap → checkpoint
    ssh user@ip                       # "give ssh before cleanup"
    phase template finish <name>      # cleanup → minimize → qm template
    phase template abort <name>       # give up, destroy
"""

from __future__ import annotations

import json
import os
import time

from .config import template_dir
from .log import append_event
from .notify import send
from .plan import parse_flags
from .provision import bootstrap_script, qga_run_script
from .qm import Qm
from .util import PhaseError, confirm, die, log, warn
from .vm import (best_guest_ip, find_vmid, next_template_vmid,
                 resolve_template, resolve_vmref, template_name,
                 validate_name)

STATES = ("cloned", "awaiting-config", "ready")


# ---- state file ------------------------------------------------------------

def _state_path(name: str) -> str:
    return os.path.join(template_dir(), f"{name}.json")


def load_state(name: str) -> dict:
    path = _state_path(name)
    if not os.path.isfile(path):
        die(f"no template in progress for {name} — run `phase template create {name}` first")
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        die(f"template state is corrupt: {path}: {e}")


def save_state(st: dict) -> None:
    os.makedirs(template_dir(), exist_ok=True)
    with open(_state_path(st["name"]), "w") as f:
        json.dump(st, f, indent=2)
        f.write("\n")


def list_states() -> list[dict]:
    out = []
    if not os.path.isdir(template_dir()):
        return out
    for fn in sorted(os.listdir(template_dir())):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(template_dir(), fn)) as f:
                out.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            continue
    return out


# ---- create ----------------------------------------------------------------

def cmd_template_create(cfg, qm, argv):
    opts, pos = parse_flags(argv, {
        "name": {"default": ""},
        "os": {"default": ""},
        "from": {"default": ""},
        "system": {"bool": True, "default": False},
        "docker": {"bool": True, "default": False},
        "tailscale": {"bool": True, "default": False},
    })
    if not opts["name"] and not opts["os"]:
        die("--name or --os is required")
    prefix = cfg.get("templates.prefix", "tpl")
    sep = cfg.get("templates.separator", "-")
    name = opts["name"] or template_name(cfg, opts["os"])
    validate_name(cfg, name)
    if not name.startswith(prefix + sep):
        die(f"template names must start with '{prefix}{sep}' (got {name})")
    os_name = opts["os"] or name[len(prefix) + len(sep):]

    # source template: explicit --from, else the family template
    if opts["from"]:
        source = resolve_vmref(qm, opts["from"])
    else:
        source = resolve_template(cfg, qm, os_name)
    if source == (find_vmid(qm, name) if _exists(qm, name) else None):
        die("cannot clone a template into itself — pick a different --name or --from")

    vmid = next_template_vmid(qm)
    log(f"Cloning {name} (VMID {vmid}) from source {source}...")
    qm.clone(source, vmid, name=name, full=1)
    qm.start(vmid)
    log(f"Waiting for guest agent on {name}...")
    if not qm.wait_for_agent(vmid, timeout=120):
        die(f"guest agent never came up on {name}")
    ip = best_guest_ip(qm, cfg, vmid)
    if ip:
        log(f"Guest IP: {ip}")
    else:
        warn(f"no guest IP yet for {name} — agent is up, network still settling")

    steps = []
    for step in ("system", "docker", "tailscale"):
        if opts[step]:
            script = bootstrap_script(cfg, os_name, step)
            if not script:
                warn(f"no {step} bootstrap script for {os_name}; skipping")
                continue
            log(f"Bootstrapping {step} on {name}...")
            qga_run_script(qm, vmid, script)
            steps.append(step)

    st = {
        "name": name,
        "vmid": vmid,
        "source": str(source),
        "os": os_name,
        "state": "awaiting-config",
        "steps": steps,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "finished_at": None,
    }
    save_state(st)
    append_event(event="template.created", vmid=vmid, name=name, state="awaiting-config")
    send(cfg, f"template create: {name}", f"VMID {vmid} awaiting your configuration")

    user = cfg.get("default_user", "root")
    log("")
    log(f"Template {name} (VMID {vmid}) is up and awaiting configuration.")
    if ip:
        log(f"  ssh {user}@{ip}")
    log("Configure it to your liking, then finalize:")
    log(f"  phase template finish {name}")
    log("To give up instead:")
    log(f"  phase template abort {name}")
    return 0


def _exists(qm, name: str) -> bool:
    try:
        find_vmid(qm, name)
        return True
    except PhaseError:
        return False


# ---- finish ----------------------------------------------------------------

def cmd_template_finish(cfg, qm, argv):
    if not argv:
        die("usage: phase template finish <name>")
    name = argv[0]
    st = load_state(name)
    if st["state"] != "awaiting-config":
        die(f"{name} is in state '{st['state']}' — only 'awaiting-config' can be finished")
    opts, _ = parse_flags(argv[1:], {
        "skip-cleanup": {"bool": True, "default": False},
        "no-minimize": {"bool": True, "default": False},
        "cores": {"default": ""},
        "memory": {"default": ""},
    })
    vmid = st["vmid"]

    # 1. cleanup inside the guest (tolerant — this is best-effort)
    if not opts["skip-cleanup"]:
        log(f"Cleaning up {name}...")
        for cmd in (["sudo", "fstrim", "-av"], ["sudo", "apt-get", "-y", "clean"],
                    ["sudo", "rm", "-rf", "/tmp/phase-*"]):
            try:
                qm.guest_exec(vmid, ["bash", "-lc", " ".join(cmd)])
            except PhaseError as e:
                warn(f"cleanup step skipped ({cmd[0]}...): {e}")

    # 2. stop (template conversion requires a stopped VM)
    if qm.status(vmid) == "running":
        log(f"Shutting down {name}...")
        qm.shutdown(vmid)
        if not qm.wait_for_stopped(vmid, timeout=60):
            log("shutdown timed out — forcing stop")
            qm.stop(vmid)

    # 3. minimize: small cores/mem + balloon + trim-on-clone
    if not opts["no-minimize"]:
        micro = cfg.get("templates.sizes.micro") or {"cores": 1, "memory": 1024}
        cores = opts["cores"] or micro.get("cores", 1)
        memory = opts["memory"] or micro.get("memory", 1024)
        log(f"Minimizing {name}: {cores} core(s), {memory} MB, balloon, fstrim-on-clone...")
        qm.set(vmid, cores=cores, memory=memory, balloon=int(memory) // 2,
               agent="enabled=1,fstrim_cloned_disks=1", onboot=0)

    # 4. freeze as template
    log(f"Saving {name} as template...")
    qm.make_template(vmid)

    st["state"] = "ready"
    st["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    save_state(st)
    append_event(event="template.finished", vmid=vmid, name=name)
    send(cfg, f"✓ template ready: {name}", f"VMID {vmid} — minimized and saved")
    log(f"Template {name} (VMID {vmid}) is ready. Clone it with:")
    log(f"  phase vm create --name <vm> --size small --os {st['os']}")
    return 0


# ---- abort ----------------------------------------------------------------

def cmd_template_abort(cfg, qm, argv):
    if not argv:
        die("usage: phase template abort <name> [--force]")
    name = argv[0]
    opts, _ = parse_flags(argv[1:], {"force": {"bool": True, "default": False}})
    st = load_state(name)
    vmid = st["vmid"]
    if not opts["force"] and not confirm(f"Destroy in-progress template {name} (VMID {vmid})"):
        die("aborted")
    try:
        if qm.status(vmid) == "running":
            qm.stop(vmid)
    except PhaseError:
        pass
    qm.destroy(vmid)
    path = _state_path(name)
    if os.path.isfile(path):
        os.unlink(path)
    append_event(event="template.aborted", vmid=vmid, name=name)
    send(cfg, f"template aborted: {name}", f"VMID {vmid} destroyed")
    log(f"Aborted {name} (VMID {vmid} destroyed).")
    return 0


# ---- list / show -----------------------------------------------------------

def template_rows(cfg, qm) -> list[dict]:
    """Live templates merged with in-progress state files."""
    rows = []
    from .vm import list_templates
    for t in list_templates(qm):
        rows.append({
            "name": t["name"], "vmid": t["vmid"], "state": "ready",
            "source": "", "os": "", "created": "",
        })
    for st in list_states():
        rows.append({
            "name": st["name"], "vmid": st["vmid"], "state": st["state"],
            "source": st.get("source", ""), "os": st.get("os", ""),
            "created": (st.get("created_at") or "")[:19],
        })
    seen = set()
    out = []
    for r in rows:
        if r["vmid"] in seen:
            continue
        seen.add(r["vmid"])
        out.append(r)
    return sorted(out, key=lambda r: r["vmid"])


def cmd_template_list(cfg, qm, argv, json_out: bool = False):
    rows = template_rows(cfg, qm)
    if json_out:
        import json as _json
        log(_json.dumps(rows, indent=2))
        return 0
    if not rows:
        log("no templates yet")
        return 0
    log(f"{'VMID':<6} {'STATE':<16} {'NAME':<32} {'SOURCE':<10} {'OS':<14} CREATED")
    log("-" * 92)
    for r in rows:
        log(f"{r['vmid']:<6} {r['state']:<16} {r['name']:<32} "
            f"{r['source']:<10} {r['os']:<14} {r['created']}")
    return 0


def cmd_template_show(cfg, qm, argv):
    if not argv:
        die("usage: phase template show <name>")
    name = argv[0]
    st = load_state(name)
    vmid = st["vmid"]
    log(f"Template in progress: {name}")
    log(f"  VMID:     {vmid}")
    log(f"  State:    {st['state']}")
    log(f"  OS:       {st.get('os', '?')}")
    log(f"  Source:   {st.get('source', '?')}")
    log(f"  Created:  {st.get('created_at', '?')}")
    log(f"  Steps:    {', '.join(st.get('steps') or []) or 'none'}")
    if st["state"] == "awaiting-config":
        log(f"  Next:     configure, then `phase template finish {name}`")
    elif st["state"] == "ready":
        log(f"  Finished: {st.get('finished_at', '?')}")
    return 0

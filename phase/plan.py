"""Plans: build/validate/print/realize + plan library CRUD.

A plan is the v4 equivalent of GCP's instance template: a JSON description of
a VM before it exists. `phase vm plan --save` writes one; `phase vm create`
consumes it; `phase plan list|show|rm|export|import` manages the library.

Schema (v4 — disks replaced the old single `disk` field):
    name, size, os, cores, memory, disks:[{id,role,os,size,storage}],
    net:{bridge,vlan}, ipconfig, gw, onboot, description, protection,
    tags[], ssh_keys[], bootstrap:{system,docker,tailscale}, gpu, vmid
"""

from __future__ import annotations

import glob
import json
import os
import shlex
import time

from .config import plan_dir
from .log import append_event
from .qm import Qm
from .util import (PhaseError, die, have_tty, log, parse_disk_spec, quoted,
                   to_bytes, validate_name)
from .vm import (best_guest_ip, build_sshkeys_file, disk_size_bytes,
                 find_vmid, next_vmid, resolve_size, resolve_template)

# ---------------------------------------------------------------------------
# flag parsing


class FlagError(PhaseError):
    pass


def parse_flags(argv: list[str], specs: dict, positionals: bool = True):
    """Tiny declarative flag parser.

    specs: {name: {"nargs": 1|"*", "repeat": bool, "default": ...}}
    Flags may be --flag value or --flag=value; boolean flags take no value.
    Returns (opts: dict, pos: list[str]).
    """
    opts = {name: spec.get("default") for name, spec in specs.items()}
    for name, spec in specs.items():
        if spec.get("repeat"):
            opts[name] = list(opts[name] or []) if opts[name] else []
    pos = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            pos.extend(argv[i + 1:])
            break
        if a.startswith("--") and "=" in a:
            key, _, val = a[2:].partition("=")
            if key not in specs:
                die(f"unknown option: --{key}")
            spec = specs[key]
            if spec.get("bool"):
                opts[key] = val not in ("0", "false", "no")
            elif spec.get("repeat"):
                opts[key].append(val)
            else:
                opts[key] = val
            i += 1
            continue
        if a.startswith("--") and a[2:] in specs:
            key = a[2:]
            spec = specs[key]
            if spec.get("bool"):
                opts[key] = True
                i += 1
            elif spec.get("repeat"):
                if i + 1 >= len(argv):
                    die(f"option --{key} requires a value")
                opts[key].append(argv[i + 1])
                i += 2
            else:
                if i + 1 >= len(argv):
                    die(f"option --{key} requires a value")
                opts[key] = argv[i + 1]
                i += 2
            continue
        if a.startswith("-") and a != "-":
            die(f"unknown option: {a}")
        pos.append(a)
        i += 1
    return opts, pos


PLAN_FLAGS = {
    "name": {"default": ""},
    "size": {"default": ""},
    "os": {"default": ""},
    "disk": {"repeat": True, "default": []},
    "bridge": {"default": ""},
    "vlan": {"default": ""},
    "ip": {"default": ""},
    "gw": {"default": ""},
    "description": {"default": ""},
    "protect": {"bool": True, "default": False},
    "no-onboot": {"bool": True, "default": False},
    "tag": {"repeat": True, "default": []},
    "ssh-key": {"repeat": True, "default": []},
    "system": {"bool": True, "default": False},
    "docker": {"bool": True, "default": False},
    "tailscale": {"bool": True, "default": False},
    "gpu": {"default": ""},
    "cores": {"default": ""},
    "memory": {"default": ""},
    "dry-run": {"bool": True, "default": False},
}

# ---------------------------------------------------------------------------
# prompts (rich when tui extra present, plain input() otherwise)


def _rich():
    try:
        from rich.prompt import Prompt
        return Prompt
    except ImportError:
        return None


def prompt_text(prompt: str, default: str = "", placeholder: str = "") -> str:
    P = _rich()
    if P:
        return P.ask(prompt, default=default or None) or default
    ans = input(f"{prompt} [{placeholder}] " if not default else f"{prompt} [{default}] ")
    return ans.strip() or default


def prompt_choose(prompt: str, options: list[str]) -> str:
    if not options:
        die(f"{prompt}: no options available")
    P = _rich()
    if P:
        from rich.prompt import Prompt
        return Prompt.ask(prompt, choices=options)
    print(prompt)
    for i, o in enumerate(options, 1):
        print(f"  {i}. {o}")
    while True:
        ans = input("select: ").strip()
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return options[int(ans) - 1]
        if ans in options:
            return ans


# ---------------------------------------------------------------------------
# gather


def gather_plan(cfg, qm, argv: list[str], name_hint: str = "") -> dict:
    opts, pos = parse_flags(argv, PLAN_FLAGS)
    name = opts["name"] or name_hint
    size, os_ = opts["size"], opts["os"]
    disks = [parse_disk_spec(d) for d in opts["disk"]]

    # Required fields: prompts on a TTY, hard error without one.
    if not name and have_tty():
        name = prompt_text("VM name", placeholder="postgres")
    if not size and have_tty():
        size = prompt_choose("Size", sorted((cfg.get("templates.sizes") or {}).keys()))
    if not os_ and have_tty():
        from .vm import list_template_oses
        os_ = prompt_choose("OS template", list_template_oses(cfg, qm))
    for missing, label in ((name, "name"), (size, "size"), (os_, "os")):
        if not missing:
            die(f"missing required field: --{label}")

    validate_name(cfg, name)

    # An os disk is implied when none given (scsi0).
    if not disks:
        disks = [{"id": "scsi0", "role": "os", "os": os_, "size": "", "storage": ""}]
    os_disks = [d for d in disks if d["role"] == "os"]
    if len(os_disks) > 1:
        die("only one os disk allowed per plan")
    plan_os = os_disks[0]["os"] if os_disks else os_

    # Size: preset, custom (--cores/--memory), or auto (derive from template).
    cores, memory = opts["cores"], opts["memory"]
    if size == "auto":
        tpl = resolve_template(cfg, qm, plan_os)
        tconf = qm.config(tpl)
        cores = tconf.get("cores", "1")
        memory = tconf.get("memory", "1024")
        size = "auto"
    elif size != "":
        if cores or memory:
            die("--cores/--memory cannot be combined with --size")
        cores, memory = resolve_size(cfg, size)
    else:
        die("missing required field: --size (or --cores/--memory for custom)")
    cores, memory = str(cores), str(memory)

    plan = {
        "name": name,
        "size": size,
        "os": plan_os,
        "cores": cores,
        "memory": memory,
        "disks": disks,
        "net": {"bridge": opts["bridge"] or None, "vlan": opts["vlan"] or None},
        "ipconfig": opts["ip"] or None,
        "gw": opts["gw"] or None,
        "onboot": not opts["no-onboot"],
        "description": opts["description"] or None,
        "protection": opts["protect"],
        "tags": opts["tag"],
        "ssh_keys": opts["ssh-key"],
        "bootstrap": {
            "system": opts["system"],
            "docker": opts["docker"],
            "tailscale": opts["tailscale"],
        },
        "gpu": opts["gpu"] or None,
        "vmid": 0,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    # Validate template + size exist before we ever touch PVE.
    resolve_template(cfg, qm, plan_os)
    if size != "auto":
        resolve_size(cfg, size)
    return plan


# ---------------------------------------------------------------------------
# print


def print_plan(cfg, qm, plan: dict, out=None) -> None:
    size = plan["size"]
    if size != "auto":
        try:
            cores, mem = resolve_size(cfg, size)
            size_label = f"{size} ({cores} vCPU, {mem} MB)"
        except PhaseError:
            size_label = size
    else:
        size_label = f"auto ({plan['cores']} vCPU, {plan['memory']} MB)"
    os_disk = next((d for d in plan["disks"] if d["role"] == "os"), {})
    data_disks = [d for d in plan["disks"] if d["role"] != "os"]
    lines = [
        f"  Name:      {plan['name']}",
        f"  Size:      {size_label}",
        f"  OS:        {plan['os']}",
        f"  Boot disk: {os_disk.get('id', 'scsi0')} ({os_disk.get('size') or 'template default'}"
        + (f" on {os_disk['storage']}" if os_disk.get("storage") else "") + ")",
    ]
    for d in data_disks:
        lines.append(f"  Data disk: {d['id']} {d.get('size') or '?'}"
                     + (f" on {d['storage']}" if d.get("storage") else ""))
    lines += [
        f"  Network:   bridge {plan['net']['bridge'] or 'template default'}, "
        f"vlan {plan['net']['vlan'] or 'none'}",
        f"  IP:        {plan['ipconfig'] or 'template default'}",
        f"  GPU:       {plan['gpu'] or 'none'}",
        f"  On boot:   {'yes' if plan['onboot'] else 'no'}",
        f"  Protected: {'yes' if plan['protection'] else 'no'}",
        f"  Tags:      {','.join(plan['tags']) if plan['tags'] else 'none'}",
        f"  SSH keys:  {','.join(plan['ssh_keys']) if plan['ssh_keys'] else 'config + template defaults'}",
        f"  Bootstrap: {','.join(k for k, v in plan['bootstrap'].items() if v) or 'none'}",
        f"  Desc:      {plan['description'] or 'none'}",
        f"  VMID:      {plan['vmid'] if plan.get('vmid') else 'allocated at create time'}",
    ]
    if out:
        out.write("Will create:\n" + "\n".join(lines) + "\n")
    else:
        log("Will create:")
        for l in lines:
            log(l)


# ---------------------------------------------------------------------------
# realize


def realize_plan(cfg, qm, plan: dict, dry_run: bool = False, out=None) -> int:
    """Clone + configure a VM from a plan. Allocates the VMID, stamps it back
    into any saved plan, returns it. dry_run prints commands instead of
    executing them (still allocates a VMID for the preview)."""
    name = plan["name"]
    validate_name(cfg, name)

    os_disk = next(d for d in plan["disks"] if d["role"] == "os")
    template = resolve_template(cfg, qm, plan["os"])
    vmid = plan.get("vmid") or next_vmid(qm)
    plan["vmid"] = vmid

    def step(cmd: list[str], quiet: bool = False) -> None:
        if dry_run:
            (out or __import__("sys").stdout).write(f"DRY RUN: {quoted(cmd)}\n")
            return
        if quiet:
            qm.t.run(["qm", *cmd], check=True)
        else:
            getattr(qm, "_run")(cmd)

    # 1. clone (boot disk lands on requested storage when given)
    clone_args = [str(template), str(vmid), "--name", name, "--full", "1"]
    if os_disk.get("storage"):
        clone_args += ["--storage", os_disk["storage"]]
    step(["clone", *clone_args])

    # 2. core sizing / behavior
    set_opts = {
        "cores": plan["cores"],
        "memory": plan["memory"],
        "onboot": "1" if plan["onboot"] else "0",
        "agent": f"enabled={'1' if cfg.get('vm.agent', True) else '0'}",
        "protection": "1" if plan["protection"] else "0",
    }
    if plan.get("description"):
        set_opts["description"] = plan["description"]
    step(["set", str(vmid)] + [p for k, v in set_opts.items()
                               for p in (f"--{k}", str(v))])

    # 3. network
    bridge, vlan = plan["net"]["bridge"], plan["net"]["vlan"]
    if bridge or vlan:
        net0 = f"virtio,bridge={bridge or cfg.get('default_bridge', 'vmbr0')}"
        if vlan:
            net0 += f",tag={vlan}"
        step(["set", str(vmid), "--net0", net0], quiet=True)

    # 4. cloud-init ip
    if plan.get("ipconfig"):
        ip = plan["ipconfig"]
        if plan.get("gw"):
            ip = f"ip={ip},gw={plan['gw']}"
        elif ip != "dhcp":
            ip = f"ip={ip}"
        step(["set", str(vmid), "--ipconfig0", ip], quiet=True)

    # 5. disk resize (grow-only, like v3)
    if os_disk.get("size"):
        cur = disk_size_bytes(qm, vmid, os_disk["id"]) if not dry_run else None
        req = to_bytes(os_disk["size"])
        if cur is not None and req <= cur:
            log(f"phase: requested disk {os_disk['size']} is not larger than the "
                f"template disk; skipping resize")
        else:
            step(["resize", str(vmid), os_disk["id"], os_disk["size"]], quiet=True)

    # 6. data disks
    for d in plan["disks"]:
        if d["role"] != "os":
            if not d.get("size"):
                die(f"data disk {d['id']} requires a size: --disk {d['id']}:data:...:<size>")
            storage = d.get("storage") or cfg.get("default_storage")
            if not storage:
                die(f"data disk {d['id']}: no storage given and no default_storage in config")
            step(["set", str(vmid), f"--{d['id']}", f"{storage}:{d['size']}"], quiet=True)

    # 7. ssh keys (merged, deduped)
    if dry_run:
        (out or __import__("sys").stdout).write(
            "DRY RUN: set <vmid> --sshkeys <template+config merged keys>\n")
    else:
        keyfile = build_sshkeys_file(qm, cfg, vmid, plan.get("ssh_keys") or [])
        if keyfile:
            step(["set", str(vmid), "--sshkeys", keyfile], quiet=True)
            os.unlink(keyfile)

    # 8. tags
    if plan["tags"]:
        step(["set", str(vmid), "--tags", ";".join(plan["tags"])], quiet=True)

    # 9. gpu (mdev passthrough) — see engine.scan for discovery
    if plan.get("gpu"):
        if dry_run:
            (out or __import__("sys").stdout).write(
                f"DRY RUN: set <vmid> --hostpci0 <pci>,mdev={plan['gpu']}\n")
        else:
            from . import engine as engine_mod
            pci = engine_mod.gpu_pci_for(cfg, qm, plan["gpu"])
            step(["set", str(vmid), "--hostpci0", f"{pci},mdev={plan['gpu']}"], quiet=True)

    # 10. boot order → the os disk
    step(["set", str(vmid), "--boot", f"order={os_disk['id']}"], quiet=True)

    # stamp back into saved plan
    path = os.path.join(plan_dir(), f"{name}.json")
    if os.path.isfile(path):
        with open(path) as f:
            saved = json.load(f)
        saved["vmid"] = vmid
        with open(path, "w") as f:
            json.dump(saved, f, indent=2)
            f.write("\n")

    if not dry_run:
        append_event(event="vm.created", vmid=vmid, name=name)
    return vmid


# ---------------------------------------------------------------------------
# plan library


def plan_path(name: str) -> str:
    return os.path.join(plan_dir(), f"{name}.json")


def save_plan(plan: dict, name: str | None = None) -> str:
    name = name or plan["name"]
    os.makedirs(plan_dir(), exist_ok=True)
    path = plan_path(name)
    with open(path, "w") as f:
        json.dump(plan, f, indent=2)
        f.write("\n")
    return path


def read_plan(name: str) -> dict:
    if name == "planned":
        path = latest_plan()
        if not path:
            die(f"no saved plans in {plan_dir()}")
    else:
        path = plan_path(name)
    if not os.path.isfile(path):
        die(f"plan not found: {path}")
    try:
        with open(path) as f:
            plan = json.load(f)
    except json.JSONDecodeError as e:
        die(f"plan is not valid JSON: {path}: {e}")
    return plan


def latest_plan() -> str | None:
    files = glob.glob(os.path.join(plan_dir(), "*.json"))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def list_plans() -> list[dict]:
    out = []
    for path in sorted(glob.glob(os.path.join(plan_dir(), "*.json"))):
        try:
            with open(path) as f:
                p = json.load(f)
            out.append({
                "name": p.get("name", os.path.basename(path)[:-5]),
                "size": p.get("size", ""),
                "os": p.get("os", ""),
                "vmid": p.get("vmid", 0),
                "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path))),
            })
        except json.JSONDecodeError:
            continue
    return out

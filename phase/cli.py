"""phase CLI — GCP Compute Engine-style VM management for Proxmox VE.

Grammar (v3-compatible hybrid, extended):
  verb-first:  phase vm plan|create|provision|nextid ...
  noun-first:  phase vm <name> <action> ...
  legacy:      phase vm <action> <name> ...
  bare name:   phase vm <name>            -> ssh
  bare:        phase                       -> TUI menu (with tui extra)

New in v4: template pipeline, engine (scan/doctor/daemon/systemd),
disk id+role model, ssh-key pool, inventory, plan library, event log.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from . import __version__
from .config import Config, ensure_state_dirs, state_dir
from .disk import cmd_vm_disk
from .inventory import cmd_inventory
from .log import append_event
from .plan import (list_plans, plan_path, print_plan, read_plan, save_plan,
                   parse_flags, gather_plan)
from .provision import (cmd_vm_bootstrap, cmd_vm_create, cmd_vm_nextid,
                        cmd_vm_provision, cmd_vm_wait)
from .qm import Qm, vzdump_backup
from .report import cmd_report
from .transport import make_transport
from .util import (PhaseError, confirm, die, have_tty, log,
                   normalize_tags, validate_name, validate_tag, warn)
from .vm import (all_vms, best_guest_ip, find_vmid, has_known_ssh_key,
                 list_templates, set_tags, vm_tags, vm_ssh_user_ip)

VERB_FIRST = {"plan", "create", "provision", "nextid"}
NAMED_ACTIONS = {
    "shell", "ssh", "connect", "exec", "service", "logs", "status",
    "start", "stop", "reboot", "reset", "shutdown", "pause",
    "tag", "firewall", "rename", "destroy", "bootstrap",
    "info", "wait", "snapshot", "backup", "resize", "disk", "ip", "agent",
}
POWER_ACTIONS = {"start": "start", "stop": "stop", "reboot": "reboot",
                 "reset": "reset", "shutdown": "shutdown", "pause": "suspend"}

USAGE = """\
phase - compute-engine style VM management for Proxmox VE.

Usage:
  phase                                    # TUI menu (with tui extra)
  phase vm plan|create|provision|nextid ...  # verb-first (creation)
  phase vm <name> <action> ...               # noun-first (per-VM ops)
  phase vm <name>                            # bare name = ssh
  phase template create|finish|abort|list|show
  phase engine scan|doctor|daemon|install|uninstall|status|logs|jobs
  phase plan list|show|rm|export|import
  phase disk/ssh-key/inventory/report/extras/templates/update/version

VM actions:
  shell ssh exec service logs status start stop reboot reset shutdown pause
  tag firewall rename destroy bootstrap list info wait snapshot backup
  resize disk ip agent

Global flags (before the subcommand):
  --config <file>   --host <ssh-host>   --json

Configure from /etc/phase.json (see config.schema.json).
"""


# ---------------------------------------------------------------------------
# entry


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        return _main(argv)
    except PhaseError as e:
        print(f"phase: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def _main(argv: list[str]) -> int:
    cfg_path = None
    host = None
    json_out = False
    rest = []
    i = 0
    # Global flags only appear BEFORE the subcommand; anything after belongs
    # to the command itself (e.g. `inventory --host <name>`).
    while i < len(argv) and argv[i].startswith("--"):
        a = argv[i]
        if a in ("--config", "--host"):
            if i + 1 >= len(argv):
                die(f"{a} requires a value")
            if a == "--config":
                cfg_path = argv[i + 1]
            else:
                host = argv[i + 1]
            i += 2
        elif a == "--json":
            json_out = True
            i += 1
        else:
            die(f"unknown global option: {a}")
    rest = argv[i:]

    if not rest:
        if have_tty():
            return _tui_or_usage(cfg_path, host, json_out)
        log(USAGE)
        return 0
    cmd = rest[0]
    argv = rest[1:]
    # --json is harmless anywhere; tolerate it after the subcommand too.
    if "--json" in argv:
        json_out = True
        argv = [a for a in argv if a != "--json"]

    if cmd in ("help", "--help", "-h"):
        log(USAGE)
        return 0
    if cmd == "version":
        log(f"phase {__version__}")
        return 0
    if cmd == "tui":
        return _tui_or_usage(cfg_path, host, json_out)
    if cmd == "update":
        return cmd_update()

    cfg = Config.load_optional(cfg_path)
    qm = Qm(make_transport(host))

    handlers = {
        "vm": lambda: cmd_vm(cfg, qm, argv, json_out),
        "template": lambda: cmd_template(cfg, qm, argv, json_out),
        "plan": lambda: cmd_plan_group(cfg, qm, argv, json_out),
        "engine": lambda: cmd_engine(cfg, qm, argv, json_out),
        "ssh-key": lambda: cmd_sshkey(cfg, argv),
        "inventory": lambda: cmd_inventory(cfg, qm, argv),
        "report": lambda: cmd_report(cfg, qm, argv),
        "extras": lambda: cmd_extras(cfg),
        "templates": lambda: cmd_templates_legacy(cfg, qm, json_out),
    }
    if cmd not in handlers:
        die(f"unknown command: {cmd}\n\n{USAGE}")
    if cmd not in ("extras",) and cfg is None:
        die("config not found (set PHASE_CONFIG or create /etc/phase.json)")
    try:
        rc = handlers[cmd]()
    except PhaseError as e:
        append_event(event="cmd.error", command=" ".join(shlex.quote(c) for c in rest),
                     detail=str(e))
        raise
    append_event(event="cmd", command=" ".join(shlex.quote(c) for c in rest), status="ok")
    return rc


def _tui_or_usage(cfg_path, host, json_out) -> int:
    try:
        from . import tui
        return tui.run(cfg_path, host)
    except PhaseError as e:  # MissingExtra
        print(f"phase: {e}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# vm dispatch (hybrid grammar)


def cmd_vm(cfg, qm, argv, json_out) -> int:
    if not argv:
        if have_tty():
            return _tui_or_usage(None, None, json_out)
        die("usage: phase vm <name|action> ...")
    first = argv[0]
    rest = argv[1:]

    if first == "list" or first == "ls":
        return cmd_vm_list(cfg, qm, json_out)
    if first in VERB_FIRST:
        if first == "plan":
            return cmd_vm_plan(cfg, qm, rest)
        if first == "create":
            return cmd_vm_create(cfg, qm, rest)
        if first == "provision":
            return cmd_vm_provision(cfg, qm, rest)
        if first == "nextid":
            return cmd_vm_nextid(cfg, qm, rest)

    # legacy verb-first: phase vm <action> <name> ...
    if first in NAMED_ACTIONS and rest:
        if first == "tag":
            if len(rest) < 2:
                die("usage: phase vm tag <list|add|remove|set> <name> ...")
            return cmd_vm_tag(cfg, qm, rest[1], rest[0], rest[2:])
        if first == "firewall":
            if len(rest) < 2:
                die("usage: phase vm firewall <add|list> <name> ...")
            return cmd_vm_firewall(cfg, qm, rest[1], rest[0], rest[2:])
        return cmd_vm_named(cfg, qm, rest[0], first, rest[1:], json_out)

    # noun-first: phase vm <name> <action> ...
    if rest and rest[0] in NAMED_ACTIONS:
        return cmd_vm_named(cfg, qm, first, rest[0], rest[1:], json_out)

    # bare name -> shell
    if not rest:
        return cmd_vm_connect(cfg, qm, [first])
    die(f"unknown vm action: {rest[0]}")


def cmd_vm_named(cfg, qm, name, action, rest, json_out) -> int:
    if action in POWER_ACTIONS:
        return cmd_vm_power(cfg, qm, name, POWER_ACTIONS[action])
    if action in ("shell", "ssh", "connect"):
        return cmd_vm_connect(cfg, qm, [name, *rest])
    if action == "exec":
        return cmd_vm_exec(cfg, qm, [name, *rest])
    if action == "service":
        return cmd_vm_service(cfg, qm, [name, *rest])
    if action == "logs":
        return cmd_vm_logs(cfg, qm, [name, *rest])
    if action == "status":
        return cmd_vm_status(cfg, qm, name, json_out)
    if action == "bootstrap":
        return cmd_vm_bootstrap(cfg, qm, [name, *rest])
    if action == "tag":
        if not rest:
            die("usage: phase vm <name> tag <list|add|remove|set> ...")
        return cmd_vm_tag(cfg, qm, name, rest[0], rest[1:])
    if action == "firewall":
        if not rest:
            die("usage: phase vm <name> firewall <add|list> ...")
        return cmd_vm_firewall(cfg, qm, name, rest[0], rest[1:])
    if action == "rename":
        if not rest:
            die("usage: phase vm <name> rename <new-name>")
        return cmd_vm_rename(cfg, qm, name, rest[0])
    if action == "destroy":
        return cmd_vm_destroy(cfg, qm, name, rest)
    if action == "info":
        return cmd_vm_info(cfg, qm, name, json_out)
    if action == "wait":
        return cmd_vm_wait(cfg, qm, [name, *rest])
    if action == "snapshot":
        return cmd_vm_snapshot(cfg, qm, name, rest, json_out)
    if action == "backup":
        return cmd_vm_backup(cfg, qm, name, rest)
    if action == "resize":
        return cmd_vm_resize(cfg, qm, name, rest)
    if action == "disk":
        return cmd_vm_disk(cfg, qm, name, rest, json_out)
    if action == "ip":
        return cmd_vm_ip(cfg, qm, name)
    if action == "agent":
        return cmd_vm_agent(cfg, qm, name)
    die(f"unknown vm action: {action}")


# ---------------------------------------------------------------------------
# vm actions


def cmd_vm_connect(cfg, qm, argv) -> int:
    if not argv:
        die("VM name is required")
    name = argv[0]
    opts, pos = parse_flags(argv[1:], {
        "serial": {"bool": True, "default": False},
        "user": {"default": ""},
        "ip": {"default": ""},
        "no-key-check": {"bool": True, "default": False},
        "no-ephemeral": {"bool": True, "default": False},
        "command": {"default": ""},
    })
    for f in ("user", "ip", "command"):
        if opts[f] and opts["serial"]:
            die(f"--{f} is incompatible with --serial")
    vmid = find_vmid(qm, name)
    if opts["serial"]:
        qm.terminal(vmid)
        return 0
    status = qm.status(vmid)
    if status != "running":
        die(f"{name} is {status}; start it or use --serial")
    conf = qm.config(vmid)
    user = opts["user"] or conf.get("ciuser") or cfg.get("default_user", "root")
    ip = opts["ip"] or (best_guest_ip(qm, cfg, vmid) or "")
    if not ip:
        die(f"no guest-agent IPv4 found for {name}; try --ip or --serial")

    ssh = shutil.which("ssh") or die("ssh not found")
    known = has_known_ssh_key(qm, vmid)
    args = [ssh]
    keyfile = None
    if not known and not opts["no-key-check"]:
        if opts["no-ephemeral"]:
            warn("no local key found in cloud-init sshkeys for " + name)
        else:
            warn("no known key — generating ephemeral key (gcloud-style)")
            keygen = shutil.which("ssh-keygen") or die("ssh-keygen not found")
            fd, keyfile = tempfile.mkstemp(prefix="phase-eph-")
            os.close(fd)
            subprocess.run([keygen, "-t", "ed25519", "-f", keyfile, "-N", "",
                            "-q", "-C", "phase-ephemeral"], check=True)
            pub = open(keyfile + ".pub").read().strip()
            qm.guest_exec_stdin(
                vmid,
                pub + "\n",
                ["bash", "-lc",
                 "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
                 "cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"],
            )
            args += ["-i", keyfile]
    try:
        if opts["command"]:
            args += [f"{user}@{ip}", opts["command"]]
        else:
            args += [f"{user}@{ip}"]
        rc = subprocess.run(args).returncode
        return rc
    finally:
        if keyfile:
            qm.guest_exec(
                vmid,
                ["bash", "-lc",
                 f"grep -vF '{open(keyfile + '.pub').read().strip()}' "
                 f"~/.ssh/authorized_keys > ~/.ssh/authorized_keys.tmp && "
                 f"mv ~/.ssh/authorized_keys.tmp ~/.ssh/authorized_keys"],
            )
            for p in (keyfile, keyfile + ".pub"):
                try:
                    os.unlink(p)
                except OSError:
                    pass


def cmd_vm_exec(cfg, qm, argv) -> int:
    if not argv:
        die("VM name is required")
    name = argv[0]
    rest = argv[1:]
    if rest and rest[0] == "--":
        rest = rest[1:]
    if not rest:
        die("usage: phase vm <name> exec -- <command...>")
    user, ip, _ = vm_ssh_user_ip(qm, cfg, name)
    return subprocess.run(["ssh", f"{user}@{ip}", *rest]).returncode


def cmd_vm_service(cfg, qm, argv) -> int:
    if len(argv) < 3:
        die("usage: phase vm <name> service <unit> "
            "<start|stop|restart|reload|enable|disable|status|is-active|is-enabled> [--user]")
    name, unit, action = argv[0], argv[1], argv[2]
    allowed = {"start", "stop", "restart", "reload", "enable", "disable",
               "status", "is-active", "is-enabled"}
    if action not in allowed:
        die(f"unknown service action: {action}")
    user_units = "--user" in argv[3:]
    user, ip, _ = vm_ssh_user_ip(qm, cfg, name)
    cmd = ["systemctl", "--no-pager"]
    if user_units:
        cmd.append("--user")
    cmd += [action, unit]
    return subprocess.run(["ssh", f"{user}@{ip}", *cmd]).returncode


def cmd_vm_logs(cfg, qm, argv) -> int:
    if not argv:
        die("VM name is required")
    name = argv[0]
    opts, pos = parse_flags(argv[1:], {
        "unit": {"default": ""},
        "u": {"default": ""},
        "lines": {"default": "50"},
        "n": {"default": ""},
        "follow": {"bool": True, "default": False},
        "f": {"bool": True, "default": False},
    })
    unit = opts["unit"] or opts["u"]
    lines = opts["n"] or opts["lines"]
    user, ip, _ = vm_ssh_user_ip(qm, cfg, name)
    cmd = ["journalctl", "--no-pager", "-n", lines]
    if unit:
        cmd += ["-u", unit]
    if opts["follow"] or opts["f"]:
        cmd.append("-f")
    return subprocess.run(["ssh", f"{user}@{ip}", *cmd]).returncode


def cmd_vm_status(cfg, qm, name, json_out) -> int:
    vmid = find_vmid(qm, name)
    conf = qm.config(vmid)
    status = qm.status(vmid)
    ip = best_guest_ip(qm, cfg, vmid) or "unknown"
    row = {
        "name": name, "vmid": vmid, "state": status, "ip": ip,
        "tags": vm_tags(qm, vmid), "cores": conf.get("cores", ""),
        "memory": conf.get("memory", ""), "agent": conf.get("agent", ""),
        "onboot": conf.get("onboot", ""),
    }
    if json_out:
        log(json.dumps(row, indent=2))
        return 0
    log(f"Name:   {name}")
    log(f"VMID:   {vmid}")
    log(f"State:  {status}")
    log(f"IP:     {ip}")
    log(f"Tags:   {row['tags'] or '-'}")
    log(f"Cores:  {row['cores']}")
    log(f"Memory: {row['memory']} MB")
    log(f"Agent:  {row['agent']}")
    log(f"Onboot: {row['onboot']}")
    return 0


def cmd_vm_list(cfg, qm, json_out) -> int:
    rows = all_vms(qm, cfg)
    if json_out:
        log(json.dumps(rows, indent=2))
        return 0
    log(f"{'VMID':<6} {'STATUS':<9} {'IP':<16} {'NAME':<24} TAGS")
    log("-" * 70)
    for r in rows:
        mark = "tpl" if r["template"] else "   "
        log(f"{r['vmid']:<6} {r['status']:<9} {r['ip']:<16} {r['name']:<24} {r['tags']} {mark}")
    return 0


def cmd_vm_info(cfg, qm, name, json_out) -> int:
    vmid = find_vmid(qm, name)
    conf = qm.config(vmid)
    if json_out:
        log(json.dumps(conf, indent=2))
        return 0
    for k, v in conf.items():
        log(f"{k}: {v}")
    return 0


def cmd_vm_power(cfg, qm, name, op) -> int:
    vmid = find_vmid(qm, name)
    getattr(qm, op)(vmid)
    append_event(event=f"vm.{op}", vmid=vmid, name=name)
    log(f"{name}: {op}")
    return 0


def cmd_vm_ip(cfg, qm, name) -> int:
    vmid = find_vmid(qm, name)
    ip = best_guest_ip(qm, cfg, vmid)
    if not ip:
        die(f"no guest-agent IPv4 found for {name}")
    log(ip)
    return 0


def cmd_vm_agent(cfg, qm, name) -> int:
    vmid = find_vmid(qm, name)
    if qm.status(vmid) != "running":
        die(f"{name} is not running")
    if qm.guest_ping(vmid):
        from .vm import guest_ipv4s
        n = len(guest_ipv4s(qm, vmid))
        log(f"{name}: guest agent responsive ({n} ipv4 addresses)")
        return 0
    die(f"{name}: guest agent not responding (is qemu-guest-agent installed?)")


def cmd_vm_tag(cfg, qm, name, sub, rest) -> int:
    vmid = find_vmid(qm, name)
    if sub == "list":
        tags = vm_tags(qm, vmid)
        if tags:
            for t in tags.split(";"):
                log(t)
        return 0
    if sub == "add":
        if not rest:
            die("tag is required")
        tag = rest[0]
        validate_tag(tag)
        next_tags = normalize_tags(vm_tags(qm, vmid), tag)
        set_tags(qm, vmid, next_tags)
        log(next_tags)
        return 0
    if sub in ("remove", "rm"):
        if not rest:
            die("tag is required")
        tag = rest[0]
        validate_tag(tag)
        next_tags = normalize_tags(
            *[t for t in vm_tags(qm, vmid).split(";") if t and t != tag])
        set_tags(qm, vmid, next_tags)
        log(next_tags)
        return 0
    if sub == "set":
        if not rest:
            die("at least one tag is required")
        for t in rest:
            validate_tag(t)
        next_tags = normalize_tags(*rest)
        set_tags(qm, vmid, next_tags)
        log(next_tags)
        return 0
    die(f"unknown vm tag command: {sub}")


def cmd_vm_firewall(cfg, qm, name, sub, rest) -> int:
    vmid = find_vmid(qm, name)
    if sub == "list":
        qm.guest_exec(vmid, ["sudo", "ufw", "status", "numbered"])
        return 0
    if sub == "add":
        opts, pos = parse_flags(rest, {
            "from": {"default": ""}, "port": {"default": ""},
            "proto": {"default": "tcp"},
        })
        if not opts["from"] or not opts["port"]:
            die("--from and --port are required")
        if opts["proto"] not in ("tcp", "udp"):
            die("--proto must be tcp or udp")
        alias = cfg.get(f"networks.{opts['from']}")
        if alias:
            cidrs = alias
        else:
            cidrs = [opts["from"]]
        for cidr in cidrs:
            qm.guest_exec(vmid, ["sudo", "ufw", "allow", "from", cidr,
                                 "to", "any", "port", opts["port"],
                                 "proto", opts["proto"]])
        append_event(event="firewall.add", vmid=vmid, name=name,
                     cidr=",".join(cidrs), port=opts["port"], proto=opts["proto"])
        log(f"allowed {','.join(cidrs)}:{opts['port']}/{opts['proto']} on {name}")
        return 0
    die(f"unknown vm firewall command: {sub}")


def cmd_vm_rename(cfg, qm, name, new_name) -> int:
    validate_name(cfg, new_name)
    vmid = find_vmid(qm, name)
    qm.set(vmid, name=new_name)
    append_event(event="vm.renamed", vmid=vmid, name=name, new_name=new_name)
    log(f"{name} -> {new_name} (VMID {vmid})")
    return 0


def cmd_vm_destroy(cfg, qm, name, rest) -> int:
    opts, pos = parse_flags(rest, {"force": {"bool": True, "default": False}})
    vmid = find_vmid(qm, name)
    status = qm.status(vmid)
    tags = vm_tags(qm, vmid)
    log(f"Will destroy:")
    log(f"  Name: {name}")
    log(f"  VMID: {vmid}")
    log(f"  Status: {status}")
    log(f"  Tags: {tags or 'none'}")
    if opts["force"]:
        if not (name.startswith("tmp-") or name.startswith("lab-")):
            die("--force is only allowed for tmp-* or lab-* VMs")
    elif not confirm(f"Type '{name}' to permanently destroy this VM"):
        die("confirmation did not match; aborting")
    try:
        qm.stop(vmid)
    except PhaseError:
        pass
    qm.destroy(vmid)
    append_event(event="vm.destroyed", vmid=vmid, name=name)
    log(f"Destroyed {name} (VMID {vmid}).")
    return 0


def cmd_vm_snapshot(cfg, qm, name, rest, json_out) -> int:
    vmid = find_vmid(qm, name)
    sub = rest[0] if rest else "create"
    if sub == "list":
        snaps = qm.listsnapshot(vmid)
        if json_out:
            log(json.dumps(snaps, indent=2))
            return 0
        for s in snaps:
            log(f"{s['name']:<24} {s['desc']:<20} vmstate={s['vmstate']}")
        return 0
    if sub == "create":
        if len(rest) < 2:
            die("usage: phase vm <name> snapshot create <snap-name>")
        qm.snapshot(vmid, rest[1])
        append_event(event="vm.snapshot", vmid=vmid, name=name, snap=rest[1])
        log(f"snapshot {rest[1]} taken")
        return 0
    if sub == "rollback":
        if len(rest) < 2:
            die("usage: phase vm <name> snapshot rollback <snap-name>")
        if not confirm(f"Roll back {name} to snapshot {rest[1]}? "
                       "Current state will be lost"):
            die("aborted")
        qm.rollback(vmid, rest[1])
        append_event(event="vm.rollback", vmid=vmid, name=name, snap=rest[1])
        log(f"rolled back {name} to {rest[1]}")
        return 0
    die(f"unknown snapshot command: {sub}")


def cmd_vm_backup(cfg, qm, name, rest) -> int:
    opts, pos = parse_flags(rest, {
        "storage": {"default": ""}, "mode": {"default": "snapshot"},
        "compress": {"default": "zstd"}, "notify": {"bool": True, "default": False},
    })
    vmid = find_vmid(qm, name)
    storage = opts["storage"] or cfg.get("backup.storage") or "local"
    kwargs = {"storage": storage, "mode": opts["mode"]}
    if opts["compress"] != "none":
        kwargs["compress"] = opts["compress"]
    log(f"Backing up {name} (VMID {vmid}) → {storage} [{opts['mode']}]...")
    try:
        out = vzdump_backup(qm.t, vmid, **kwargs)
    except PhaseError as e:
        append_event(event="vm.backup.failed", vmid=vmid, name=name, detail=str(e))
        if opts["notify"] or cfg.get("notify.enabled"):
            from .notify import send
            send(cfg, f"✗ backup failed: {name}", str(e))
        raise
    append_event(event="vm.backup", vmid=vmid, name=name, storage=storage)
    if opts["notify"] or cfg.get("notify.enabled"):
        from .notify import send
        send(cfg, f"✓ backup done: {name}", f"→ {storage}")
    for line in out.splitlines():
        if "INFO: Finished" in line or "INFO: Starting" in line:
            log(line)
    return 0


def cmd_vm_resize(cfg, qm, name, rest) -> int:
    opts, pos = parse_flags(rest, {
        "cores": {"default": ""}, "memory": {"default": ""},
        "disk": {"default": ""},
    })
    if not any((opts["cores"], opts["memory"], opts["disk"])):
        die("usage: phase vm <name> resize --cores N [--memory M] [--disk <id>:<size>]")
    vmid = find_vmid(qm, name)
    set_opts = {}
    if opts["cores"]:
        set_opts["cores"] = opts["cores"]
    if opts["memory"]:
        set_opts["memory"] = opts["memory"]
    if set_opts:
        qm.set(vmid, **set_opts)
        append_event(event="vm.resized", vmid=vmid, name=name, **set_opts)
    if opts["disk"]:
        disk_id, _, size = opts["disk"].partition(":")
        if not size:
            die("--disk wants <id>:<size>, e.g. scsi0:100G")
        from .vm import disk_size_bytes
        from .util import to_bytes
        cur = disk_size_bytes(qm, vmid, disk_id)
        if cur is not None and to_bytes(size) <= cur:
            warn(f"requested size {size} is not larger than current disk; skipping")
        else:
            qm.resize(vmid, disk_id, size)
            append_event(event="disk.resized", vmid=vmid, name=name,
                         disk=disk_id, size=size)
    log(f"{name}: resized" + (f" cores={opts['cores']}" if opts["cores"] else "")
        + (f" memory={opts['memory']}" if opts["memory"] else "")
        + (f" {opts['disk']}" if opts["disk"] else ""))
    return 0


# ---------------------------------------------------------------------------
# vm plan (verb-first compat: phase vm plan [--save] [flags])


def cmd_vm_plan(cfg, qm, argv) -> int:
    save = False
    rest = []
    for a in argv:
        if a == "--save":
            save = True
        else:
            rest.append(a)
    plan = gather_plan(cfg, qm, rest)
    print_plan(cfg, qm, plan)
    if save or confirm("Save this plan"):
        path = save_plan(plan)
        append_event(event="plan.saved", name=plan["name"], path=path)
        log(f"Saved plan: {path}")
    return 0


# ---------------------------------------------------------------------------
# template


def cmd_template(cfg, qm, argv, json_out) -> int:
    if not argv:
        die("usage: phase template <create|finish|abort|list|show> ...")
    sub = argv[0]
    rest = argv[1:]
    from . import template as tpl
    if sub == "create":
        return tpl.cmd_template_create(cfg, qm, rest)
    if sub == "finish":
        return tpl.cmd_template_finish(cfg, qm, rest)
    if sub == "abort":
        return tpl.cmd_template_abort(cfg, qm, rest)
    if sub == "list":
        return tpl.cmd_template_list(cfg, qm, rest, json_out)
    if sub == "show":
        return tpl.cmd_template_show(cfg, qm, rest)
    die(f"unknown template command: {sub}")


# ---------------------------------------------------------------------------
# plan library


def cmd_plan_group(cfg, qm, argv, json_out) -> int:
    if not argv:
        die("usage: phase plan <list|show|rm|export|import> ...")
    sub = argv[0]
    rest = argv[1:]
    if sub == "list":
        plans = list_plans()
        if json_out:
            log(json.dumps(plans, indent=2))
            return 0
        if not plans:
            log("no saved plans")
            return 0
        log(f"{'NAME':<28} {'SIZE':<10} {'OS':<14} {'VMID':<6} UPDATED")
        log("-" * 72)
        for p in plans:
            log(f"{p['name']:<28} {p['size']:<10} {p['os']:<14} {p['vmid']:<6} {p['updated']}")
        return 0
    if sub == "show":
        if not rest:
            die("usage: phase plan show <name>")
        plan = read_plan(rest[0])
        if json_out:
            log(json.dumps(plan, indent=2))
        else:
            print_plan(cfg, qm, plan)
        return 0
    if sub == "rm":
        if not rest:
            die("usage: phase plan rm <name>")
        path = plan_path(rest[0])
        if not os.path.isfile(path):
            die(f"plan not found: {path}")
        os.unlink(path)
        log(f"Deleted plan {rest[0]}.")
        return 0
    if sub == "export":
        if not rest:
            die("usage: phase plan export <name> [--out file]")
        opts, pos = parse_flags(rest[1:], {"out": {"default": ""}})
        plan = read_plan(rest[0])
        text = json.dumps(plan, indent=2)
        if opts["out"]:
            with open(opts["out"], "w") as f:
                f.write(text + "\n")
            log(f"Exported {rest[0]} → {opts['out']}")
        else:
            log(text)
        return 0
    if sub == "import":
        if not rest:
            die("usage: phase plan import <file.json> [--name NAME]")
        opts, pos = parse_flags(rest[1:], {"name": {"default": ""}})
        path = rest[0]
        try:
            with open(path) as f:
                plan = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            die(f"cannot import {path}: {e}")
        name = opts["name"] or plan.get("name") or os.path.basename(path)[:-5]
        plan["name"] = name
        save_plan(plan, name)
        log(f"Imported plan {name}.")
        return 0
    die(f"unknown plan command: {sub}")


# ---------------------------------------------------------------------------
# engine


def cmd_engine(cfg, qm, argv, json_out) -> int:
    if not argv:
        die("usage: phase engine <scan|doctor|daemon|install|uninstall|status|logs|jobs>")
    sub = argv[0]
    rest = argv[1:]
    from . import engine as eng
    if sub == "scan":
        return eng.cmd_engine_scan(cfg, qm, rest, json_out)
    if sub == "doctor":
        return eng.cmd_engine_doctor(cfg, qm, rest)
    if sub == "daemon":
        return eng.cmd_engine_daemon(cfg, qm, rest)
    if sub == "install":
        return eng.cmd_engine_install(cfg, qm, rest)
    if sub == "uninstall":
        return eng.cmd_engine_uninstall(cfg, qm, rest)
    if sub == "status":
        return eng.cmd_engine_status(cfg, qm, rest)
    if sub == "logs":
        return eng.cmd_engine_logs(cfg, qm, rest)
    if sub == "jobs":
        return eng.cmd_engine_jobs(cfg, qm, rest)
    die(f"unknown engine command: {sub}")


# ---------------------------------------------------------------------------
# ssh-key pool


def cmd_sshkey(cfg, argv) -> int:
    if not argv:
        die("usage: phase ssh-key <add|remove|list> ...")
    sub = argv[0]
    rest = argv[1:]
    pool = list(cfg.get("vm.ssh_keys") or [])
    if sub == "list":
        if not pool:
            log("no ssh keys in the config pool (config: vm.ssh_keys)")
            return 0
        for k in pool:
            log(k)
        return 0
    if sub == "add":
        if not rest:
            die("usage: phase ssh-key add <key-or-path>")
        spec = rest[0]
        if spec not in pool:
            pool.append(spec)
            cfg.data.setdefault("vm", {})["ssh_keys"] = pool
            cfg.save()
        append_event(event="sshkey.added", key=spec)
        log(f"added ssh key to pool ({len(pool)} total)")
        return 0
    if sub == "remove" or sub == "rm":
        if not rest:
            die("usage: phase ssh-key remove <key-or-path-or-index>")
        spec = rest[0]
        if spec.isdigit() and 0 < int(spec) <= len(pool):
            removed = pool.pop(int(spec) - 1)
        elif spec in pool:
            pool.remove(spec)
            removed = spec
        else:
            die(f"not in pool: {spec}")
        cfg.data.setdefault("vm", {})["ssh_keys"] = pool
        cfg.save()
        append_event(event="sshkey.removed", key=removed)
        log(f"removed ssh key from pool ({len(pool)} remaining)")
        return 0
    die(f"unknown ssh-key command: {sub}")


# ---------------------------------------------------------------------------
# extras / templates / update


def cmd_extras(cfg) -> int:
    from .extras import CORE_FEATURES, REGISTRY
    log("phase extras")
    log("")
    log("Core (no dependencies, always available):")
    for f in CORE_FEATURES:
        log(f"  · {f}")
    log("")
    log(f"{'EXTRA':<10} {'KIND':<8} {'STATUS':<12} FEATURES")
    log("-" * 78)
    for name, e in sorted(REGISTRY.items()):
        status = "installed" if e.installed() else f"missing: {', '.join(e.missing())}"
        log(f"{name:<10} {e.kind:<8} {status:<12} {'; '.join(e.features)}")
    log("")
    log("Install:  uv pip install -e '.[tui,notify,report]'   (in the checkout)")
    log("          or pip install phase[tui]                   (from PyPI)")
    return 0


def cmd_templates_legacy(cfg, qm, json_out) -> int:
    rows = [{"vmid": t["vmid"], "name": t["name"], "status": t["status"]}
            for t in list_templates(qm)]
    if json_out:
        log(json.dumps(rows, indent=2))
        return 0
    log(f"{'VMID':<6} {'NAME':<28} STATUS")
    log("-" * 44)
    for t in rows:
        log(f"{t['vmid']:<6} {t['name']:<28} {t['status']}")
    return 0


def cmd_update() -> int:
    repo = "/opt/phase"
    if not os.path.isdir(os.path.join(repo, ".git")):
        die("update requires a git checkout at /opt/phase")
    if not shutil.which("git"):
        die("missing required command: git")
    log("Updating phase...")
    subprocess.run(["git", "-C", repo, "fetch", "origin", "v4"], check=True)
    subprocess.run(["git", "-C", repo, "checkout", "-B", "v4", "origin/v4"],
                   check=True)
    # Re-run the installer with the extras recorded at install time.
    installer = os.path.join(repo, "installer.sh")
    if os.path.isfile(installer):
        args = ["bash", installer]
        meta = os.path.join(state_dir(), "install.json")
        try:
            with open(meta) as f:
                saved = json.load(f)
            for extra in saved.get("extras") or []:
                args += ["--extra", extra]
            if saved.get("engine"):
                args.append("--engine")
        except (OSError, ValueError):
            pass  # first install: no metadata yet
        subprocess.run(args, check=True)
    log("Update complete.")
    return 0


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    raise SystemExit(main())

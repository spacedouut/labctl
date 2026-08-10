"""Disk management — id+role model (vm-scoped: `phase vm <name> disk ...`).

Disks are keyed by their PVE id (scsi0, scsi1, ...). Each has a role:
  os   — boot disk, carries the OS (clone source), sits in the boot order
  data — plain storage disk, no OS semantics
"""

from __future__ import annotations

import json as _json

from .log import append_event
from .plan import parse_flags
from .util import PhaseError, confirm, die, is_disk_id, log, to_bytes, warn
from .vm import disk_entries, disk_size_bytes, find_vmid, next_free_disk_id


def cmd_vm_disk(cfg, qm, name: str, argv: list[str], json_out: bool = False):
    if not argv:
        die("usage: phase vm <name> disk <add|list|resize|detach> ...")
    sub = argv[0]
    rest = argv[1:]
    vmid = find_vmid(qm, name)
    if sub == "list":
        return _disk_list(qm, vmid, json_out)
    if sub == "add":
        return _disk_add(cfg, qm, name, vmid, rest)
    if sub == "resize":
        return _disk_resize(qm, name, vmid, rest)
    if sub == "detach":
        return _disk_detach(cfg, qm, name, vmid, rest)
    die(f"unknown disk command: {sub}")


def _disk_list(qm, vmid, json_out):
    entries = disk_entries(qm, vmid)
    if json_out:
        log(_json.dumps(entries, indent=2))
        return 0
    if not entries:
        log("no disks")
        return 0
    log(f"{'ID':<8} {'ROLE':<6} {'STORAGE':<16} {'SIZE':<10} MODEL")
    log("-" * 70)
    for e in entries:
        log(f"{e['id']:<8} {e['role']:<6} {e['storage']:<16} {e['size']:<10} {e['model']}")
    return 0


def _disk_add(cfg, qm, name, vmid, rest):
    opts, pos = parse_flags(rest, {
        "id": {"default": ""},
        "role": {"default": "data"},
        "size": {"default": ""},
        "storage": {"default": ""},
        "model": {"default": "scsi"},
    })
    if opts["role"] != "data":
        die("only data disks can be added after creation — the os disk comes from "
            "the template at `phase vm create` time")
    if not opts["size"]:
        die("--size is required (e.g. --size 100G)")
    to_bytes(opts["size"])  # validate
    storage = opts["storage"] or cfg.get("default_storage")
    if not storage:
        die("--storage is required (or set default_storage in config)")
    disk_id = opts["id"] or next_free_disk_id(qm, vmid, opts["model"])
    if not _is_disk_id(disk_id):
        die(f"invalid disk id: {disk_id}")
    if disk_id in {e["id"] for e in disk_entries(qm, vmid)}:
        die(f"disk {disk_id} already exists on {name}")
    qm.set(vmid, **{disk_id: f"{storage}:{opts['size']}"})
    append_event(event="disk.added", vmid=vmid, name=name, disk=disk_id,
                 size=opts["size"], storage=storage)
    log(f"Added {disk_id} ({opts['size']} on {storage}) to {name}.")
    return 0


def _disk_resize(qm, name, vmid, rest):
    if len(rest) < 2:
        die("usage: phase vm <name> disk resize <id> <size>")
    disk_id, size = rest[0], rest[1]
    if disk_id not in {e["id"] for e in disk_entries(qm, vmid)}:
        die(f"disk {disk_id} not found on {name}")
    cur = disk_size_bytes(qm, vmid, disk_id)
    req = to_bytes(size)
    if cur is not None and req <= cur:
        warn(f"requested size {size} is not larger than current disk; skipping")
        return 0
    qm.resize(vmid, disk_id, size)
    append_event(event="disk.resized", vmid=vmid, name=name, disk=disk_id, size=size)
    log(f"Resized {disk_id} to {size}.")
    return 0


def _disk_detach(cfg, qm, name, vmid, rest):
    if not rest:
        die("usage: phase vm <name> disk detach <id>")
    disk_id = rest[0]
    entries = {e["id"]: e for e in disk_entries(qm, vmid)}
    if disk_id not in entries:
        die(f"disk {disk_id} not found on {name}")
    if entries[disk_id]["role"] == "os":
        die(f"{disk_id} is the os (boot) disk — detaching it would leave the VM "
            "unbootable. Destroy the VM instead, or edit the plan to pick a new boot disk.")
    if not confirm(f"Detach {disk_id} ({entries[disk_id]['size']}) from {name}? "
                   "The disk data will be deleted"):
        die("aborted")
    qm.set(vmid, delete=disk_id)
    append_event(event="disk.detached", vmid=vmid, name=name, disk=disk_id)
    log(f"Detached {disk_id} from {name}.")
    return 0

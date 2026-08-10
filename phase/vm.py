"""VM model + lookup + guest helpers (IP, SSH keys, tags, sizing, VMIDs)."""

from __future__ import annotations

import glob
import os
import re
import tempfile
import json
from urllib.parse import unquote

from .util import PhaseError, ip_in_cidr, to_bytes, validate_name
from .config import state_dir


# ---- lookup ----------------------------------------------------------------

def find_vmid(qm, name: str) -> int:
    for v in qm.list_vms():
        if v["name"] == name:
            return v["vmid"]
    raise PhaseError(f"VM not found by name: {name}")


def resolve_vmref(qm, ref: str) -> int:
    """Accept a VMID or a name."""
    if ref.isdigit():
        vmid = int(ref)
        if any(v["vmid"] == vmid for v in qm.list_vms()):
            return vmid
        raise PhaseError(f"VM not found: {ref}")
    return find_vmid(qm, ref)


# ---- guest network ---------------------------------------------------------

def guest_ipv4s(qm, vmid: int) -> list[str]:
    data = qm.guest_cmd(vmid, "network-get-interfaces")
    if not isinstance(data, list):
        return []
    ips = []
    for iface in data:
        for a in iface.get("ip-addresses") or []:
            if a.get("ip-address-type") == "ipv4":
                ip = a.get("ip-address", "")
                if ip and not ip.startswith("127."):
                    ips.append(ip)
    return ips


def best_guest_ip(qm, cfg, vmid: int) -> str | None:
    ips = guest_ipv4s(qm, vmid)
    if not ips:
        return None
    networks = cfg.get("networks") or {}
    for cidrs in networks.values():
        for cidr in cidrs:
            for ip in ips:
                if ip_in_cidr(ip, cidr):
                    return ip
    return ips[0]


def vm_ssh_user_ip(qm, cfg, name: str) -> tuple[str, str, int]:
    vmid = find_vmid(qm, name)
    if qm.status(vmid) != "running":
        raise PhaseError(f"{name} is not running")
    conf = qm.config(vmid)
    user = conf.get("ciuser") or cfg.get("default_user", "root")
    ip = best_guest_ip(qm, cfg, vmid)
    if not ip:
        raise PhaseError(f"no guest-agent IPv4 found for {name}")
    return user, ip, vmid


# ---- templates & sizes -----------------------------------------------------

def resolve_template(cfg, qm, os: str) -> int:
    prefix = cfg.get("templates.prefix", "tpl")
    sep = cfg.get("templates.separator", "-")
    tname = f"{prefix}{sep}{os}"
    try:
        vmid = find_vmid(qm, tname)
    except PhaseError:
        raise PhaseError(f"template not found: {tname} (create it or pass a different --os)")
    if qm.config(vmid).get("template") != "1":
        raise PhaseError(f"{tname} exists at VMID {vmid} but is not marked as a template")
    return vmid


def resolve_size(cfg, size: str) -> tuple[int, int]:
    s = cfg.get(f"templates.sizes.{size}")
    if not s:
        raise PhaseError(f"unknown size: {size}")
    return s.get("cores"), s.get("memory")


def template_name(cfg, os: str) -> str:
    return f"{cfg.get('templates.prefix', 'tpl')}{cfg.get('templates.separator', '-')}{os}"


def list_templates(qm) -> list[dict]:
    out = []
    for v in qm.list_vms():
        try:
            if qm.config(v["vmid"]).get("template") == "1":
                out.append({"vmid": v["vmid"], "name": v["name"], "status": v["status"]})
        except PhaseError:
            continue
    return out


def list_template_oses(cfg, qm) -> list[str]:
    prefix = cfg.get("templates.prefix", "tpl")
    sep = cfg.get("templates.separator", "-")
    oses = set()
    for t in list_templates(qm):
        n = t["name"]
        if n.startswith(prefix + sep):
            oses.add(n[len(prefix) + len(sep):])
    return sorted(oses)


# ---- vmid allocation -------------------------------------------------------

def next_vmid(qm, lo: int = 100, hi: int = 8999) -> int:
    used = {v["vmid"] for v in qm.list_vms()}
    for i in range(lo, hi + 1):
        if i not in used:
            return i
    raise PhaseError(f"no free VMID in range {lo}-{hi}")


def next_template_vmid(qm, lo: int = 9000, hi: int = 9999) -> int:
    used = {v["vmid"] for v in qm.list_vms()}
    for i in range(lo, hi + 1):
        if i not in used:
            return i
    raise PhaseError(f"no free template VMID in range {lo}-{hi}")


# ---- tags ------------------------------------------------------------------

def vm_tags(qm, vmid: int) -> str:
    return qm.config(vmid).get("tags", "")


def set_tags(qm, vmid: int, tags: str) -> None:
    qm.set(vmid, tags=tags if tags else "none")


# ---- ssh keys --------------------------------------------------------------

def vm_sshkeys_decoded(qm, vmid: int) -> str:
    raw = qm.config(vmid).get("sshkeys", "")
    return unquote(raw) if raw else ""


def local_public_keys() -> list[str]:
    keys = []
    for d in ("/root/.ssh", os.path.expanduser("~/.ssh")):
        for f in glob.glob(os.path.join(d, "*.pub")):
            if not os.path.isfile(f):
                continue
            parts = open(f).readline().strip().split()
            if len(parts) >= 2:
                keys.append(f"{parts[0]} {parts[1]}")
    return keys


def has_known_ssh_key(qm, vmid: int) -> bool:
    vmkeys = vm_sshkeys_decoded(qm, vmid)
    return any(k and k in vmkeys for k in local_public_keys())


def ssh_argv(cfg, qm, user: str, ip: str, vmid: int,
             no_key_check: bool = False, extra: list[str] | None = None,
             quiet: bool = False) -> tuple[list[str], str | None]:
    """Build `ssh` argv (after the binary) for user@ip.

    When the VM has no known key, installs an ephemeral key through the guest
    agent (gcloud-style) so the session works. Returns (argv, keyfile) — the
    caller must unlink keyfile when the session ends.
    """
    import shutil
    import subprocess
    import tempfile
    from .util import die, warn
    ssh = shutil.which("ssh") or die("ssh not found")
    argv: list[str] = []
    keyfile = None
    if not has_known_ssh_key(qm, vmid) and not no_key_check:
        if not quiet:
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
        argv += ["-i", keyfile]
    argv += [f"{user}@{ip}"] + (extra or [])
    return argv, keyfile


def read_key_spec(spec: str) -> str:
    if os.path.isfile(spec):
        try:
            return open(spec).read().strip()
        except OSError as e:
            raise PhaseError(f"cannot read ssh key file {spec}: {e}")
    return spec


def build_sshkeys_file(qm, cfg, vmid: int, extras: list[str] = ()) -> str | None:
    """Merged deduped keyfile = template keys + config vm.ssh_keys + extras.
    Returns a temp file path (caller removes) or None if no keys."""
    lines: list[str] = []
    inherited = vm_sshkeys_decoded(qm, vmid)
    if inherited:
        lines.append(inherited)
    for spec in cfg.get("vm.ssh_keys") or []:
        lines.append(read_key_spec(spec))
    for spec in extras:
        lines.append(read_key_spec(spec))
    seen: list[str] = []
    for blob in lines:
        for ln in blob.splitlines():
            ln = ln.strip()
            if ln and ln not in seen:
                seen.append(ln)
    if not seen:
        return None
    fd, path = tempfile.mkstemp(prefix="phase-keys-")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(seen) + "\n")
    return path


# ---- disk sizes ------------------------------------------------------------

def disk_size_bytes(qm, vmid: int, disk_id: str) -> int | None:
    raw = qm.config(vmid).get(disk_id, "")
    m = re.search(r",size=([^,]+)", raw)
    if not m:
        return None
    try:
        return to_bytes(m.group(1))
    except PhaseError:
        return None


def disk_entries(qm, vmid: int) -> list[dict]:
    """All disk-like devices from config: id, storage, size, model, role."""
    conf = qm.config(vmid)
    boot = (conf.get("boot", "") or "").replace("order=", "").split(",")
    entries = []
    for key in sorted(conf.keys()):
        if re.fullmatch(r"(scsi|sata|virtio|ide)\d+", key):
            val = conf[key]
            m = re.search(r",size=([^,]+)", val)
            storage = val.split(":")[0] if ":" in val else val
            entries.append({
                "id": key,
                "storage": storage,
                "size": m.group(1) if m else "",
                "model": val.split(",")[0] if "," in val else val,
                "role": "os" if key in boot else "data",
            })
    return entries


def next_free_disk_id(qm, vmid: int, bus: str = "scsi") -> str:
    used = {e["id"] for e in disk_entries(qm, vmid)}
    i = 0
    while f"{bus}{i}" in used:
        i += 1
    return f"{bus}{i}"


# Phase-level disk provenance.  Proxmox deliberately has no concept of an
# "OS disk" beyond boot order, which Phase does not manage.  Keep the useful
# provenance in its own state file instead of inventing a PVE config option.
def _system_disk_state_path() -> str:
    return os.path.join(state_dir(), "system-disks.json")


def set_system_disk(vmid: int, disk: str, image: str) -> None:
    path = _system_disk_state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data[str(vmid)] = {"disk": disk, "image": image}
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def system_disk(vmid: int) -> dict:
    try:
        with open(_system_disk_state_path()) as f:
            return json.load(f).get(str(vmid), {})
    except (OSError, ValueError):
        return {}


# ---- aggregate -------------------------------------------------------------

def all_vms(qm, cfg) -> list[dict]:
    """Structured row for every VM: id, name, status, ip, tags, cores, mem."""
    rows = []
    for v in qm.list_vms():
        conf = qm.config(v["vmid"])
        ip = ""
        if v["status"] == "running":
            ip = best_guest_ip(qm, cfg, v["vmid"]) or ""
        rows.append({
            "vmid": v["vmid"],
            "name": v["name"],
            "status": v["status"],
            "ip": ip,
            "tags": conf.get("tags", ""),
            "cores": conf.get("cores", ""),
            "memory": conf.get("memory", ""),
            "cpu": v.get("cpu"),
            "mem_used": v.get("mem") if v.get("maxmem") is not None else None,
            "mem_total": v.get("maxmem"),
            "template": conf.get("template") == "1",
        })
    return sorted(rows, key=lambda row: (int(row["vmid"]), row["name"].casefold()))

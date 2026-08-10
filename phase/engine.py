"""Engine — the host-side machinery phase runs on.

`phase engine scan`   look around: host, storage, bridges, GPU/mdev, quotas
`phase engine doctor` config + dependency checks before attaching things
`phase engine daemon` background worker: scheduled jobs + event log + socket
`phase engine install/uninstall`  systemd service management
`phase engine jobs`   list / run jobs
`phase engine logs`   journalctl for the service
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time

from .config import (engine_socket_path, engine_state_path, ensure_state_dirs,
                     state_dir)
from .log import append_event, tail_events
from .notify import send
from .plan import parse_flags
from .qm import Qm
from .transport import LocalTransport
from .util import PhaseError, die, human_bytes, log, warn
from .vm import list_templates, next_template_vmid, next_vmid

SYSTEMD_UNIT = """\
[Unit]
Description=phase engine — Proxmox VM orchestration worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={bin} engine daemon
Restart=on-failure
RestartSec=5
Environment=PHASE_ENGINE_SOCKET={socket}
# The engine runs qm/pvesh/vzdump and writes /var/lib/phase — root is required.
User=root
Group=root

[Install]
WantedBy=multi-user.target
"""


# ---------------------------------------------------------------------------
# discovery (engine scan)


def _host_info(qm) -> dict:
    transport = qm.t
    node = transport.run(["hostname", "-s"]).stdout.strip()
    info = {"node": node}
    st = qm.pvesh(f"/nodes/{node}/status") or {}
    info.update({
        "pveversion": st.get("pveversion", ""),
        "kversion": st.get("kversion", ""),
        "uptime": st.get("uptime", 0),
        "cpu": st.get("cpu", 0),
        "loadavg": st.get("loadavg", [0, 0, 0]),
    })
    mem = st.get("memory") or {}
    info["memory"] = {
        "total": mem.get("total", 0),
        "used": mem.get("used", 0),
        "free": mem.get("free", 0),
    }
    info["cores_total"] = int(transport.run(["nproc"]).stdout.strip() or 0)
    return info


def _discover_mdev(transport) -> list[dict]:
    """mdevctl types — mediated device (vGPU) types this host can create.

    Two output shapes exist in the wild:
      flat:      nvidia-223\n  device_api=vfio-pci\n  available_instances=4
      scoped:    0000:01:00.0\n  nvidia-257\n    Available instances: 11\n    Name: GRID RTX6000-2Q
    We parse both."""
    p = transport.run(["mdevctl", "types"], check=False)
    types = []
    if p.returncode != 0:
        return types
    cur = None
    parent = ""
    for line in p.stdout.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        s = line.strip()
        if indent == 0:
            if "." in s or "/" in s:
                parent = s  # PCI address scoping the section
                continue
            # flat format: top-level type name
            if cur:
                types.append(cur)
            cur = {"type": s, "parent": "", "name": "",
                   "available_instances": "?"}
            continue
        if ":" in s:
            k, _, v = s.partition(":")
        elif "=" in s:
            k, _, v = s.partition("=")
        else:
            k = v = None
        if k is not None:
            key = k.strip().lower().replace(" ", "_")
            if key in ("available_instances", "device_api", "name",
                       "description") and cur is not None:
                cur[key] = v.strip()
            continue
        # indented non-attr line = type name (scoped format)
        if cur:
            types.append(cur)
        cur = {"type": s, "parent": parent, "name": "",
               "available_instances": "?"}
    if cur:
        types.append(cur)
    return types


def _discover_nvidia(transport) -> dict:
    """nvidia-smi vgpu — active vGPU instances with their VM names."""
    out = {"present": False, "active": []}
    p = transport.run(["nvidia-smi", "vgpu"], check=False)
    if p.returncode != 0:
        return out
    out["present"] = True
    for line in p.stdout.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 4:
            continue
        left = cells[1].split()
        right = cells[2].split()
        # vGPU rows: long numeric vGPU id + type name | VM id + VM name
        if len(left) >= 2 and left[0].isdigit() and len(left[0]) >= 6 \
                and len(right) >= 2:
            out["active"].append({
                "vgpu_id": left[0],
                "type": " ".join(left[1:]),
                "vm_id": right[0],
                "vm_name": right[1],
            })
    return out


def _discover_gpu_pci(transport) -> list[str]:
    p = transport.run(
        ["lspci", "-nn"], check=False)
    if p.returncode != 0:
        return []
    addrs = []
    for line in p.stdout.splitlines():
        if re.search(r"VGA compatible controller.*NVIDIA|3D controller.*NVIDIA",
                     line, re.IGNORECASE):
            addrs.append(line.split()[0])
    return addrs


def _host_transport(qm):
    """Return the host command transport for either VM backend.

    PVE API VM operations do not expose ``.t``. GPU/mdev discovery is still a
    host-local operation, so use a local transport with that backend.
    """
    return getattr(qm, "t", None) or LocalTransport()


def gpu_pci_for(cfg, qm: Qm, mdev_type: str) -> str:
    """Resolve a PCI address for an mdev type: config override, else discovery."""
    override = cfg.get(f"gpus.{mdev_type}.pci")
    if override:
        return override
    addrs = _discover_gpu_pci(_host_transport(qm))
    if not addrs:
        die(f"no NVIDIA GPU found to attach {mdev_type} — run `phase engine scan`")
    return addrs[0]


def _gpu_allocation(qm: Qm) -> list[dict]:
    out = []
    for v in qm.list_vms():
        try:
            conf = qm.config(v["vmid"])
        except PhaseError:
            continue
        for key in ("hostpci0", "hostpci1", "hostpci2"):
            val = conf.get(key, "")
            m = re.search(r"mdev=([^,]+)", val)
            if m:
                out.append({"vmid": v["vmid"], "name": v["name"],
                            "pci": val.split(",")[0], "mdev": m.group(1)})
    return out


def engine_scan(cfg, qm: Qm) -> dict:
    t = qm.t
    vms = qm.list_vms()
    info = {
        "host": _host_info(qm),
        "storages": qm.pvesm(),
        "bridges": _bridges(t),
        "gpu": {
            "mdev_types": _discover_mdev(t),
            "nvidia": _discover_nvidia(t),
            "pci": _discover_gpu_pci(t),
            "allocated": _gpu_allocation(qm),
        },
        "templates": list_templates(qm),
        "vms": len(vms),
        "vmids": {"next": next_vmid(qm), "next_template": next_template_vmid(qm)},
        "quotas": _quotas(qm, vms, info_cache=None),
    }
    return info


def _bridges(transport) -> list[str]:
    p = transport.run(["ip", "-o", "link", "show", "type", "bridge"], check=False)
    if p.returncode != 0:
        return []
    return [line.split(":")[1].strip() for line in p.stdout.splitlines() if ":" in line]


def _quotas(qm: Qm, vms: list[dict], info_cache) -> dict:
    cores_used = 0
    mem_used = 0
    for v in vms:
        if v["status"] != "running":
            continue
        try:
            conf = qm.config(v["vmid"])
        except PhaseError:
            continue
        cores_used += int(conf.get("cores", 0) or 0)
        mem_used += int(conf.get("memory", 0) or 0)
    return {"cores_used": cores_used, "mem_used_mb": mem_used,
            "running": sum(1 for v in vms if v["status"] == "running")}


def _render_scan(cfg, qm, info: dict) -> None:
    h = info["host"]
    la = h.get("loadavg") or []
    if isinstance(la, str):
        la = la.split()
    try:
        la = [round(float(x), 2) for x in la[:3]]
    except (TypeError, ValueError):
        la = list(la)[:3]
    log(f"Host {h['node']}")
    log(f"  PVE:        {h.get('pveversion')} (kernel {h.get('kversion')})")
    log(f"  Cores:      {h.get('cores_total')} total, {info['quotas']['cores_used']} used by running VMs")
    log(f"  Memory:     {human_bytes(h['memory']['total'])} total, "
        f"{human_bytes(h['memory']['free'])} free")
    log(f"  Load:       {la}")
    log("")
    log("Storage")
    if not info["storages"]:
        log("  (none with image content)")
    for s in info["storages"]:
        log(f"  {s['name']:<16} {s['type']:<8} {s.get('status',''):<14} "
            f"avail {s['avail']} / {s['total']}")
    log("")
    log(f"Bridges: {', '.join(info['bridges']) or 'none'}")
    log("")
    g = info["gpu"]
    log("GPU / vGPU")
    if g["nvidia"]["present"]:
        for a in g["nvidia"]["active"]:
            log(f"  active vGPU: {a['type']} → {a['vm_name']} (VM {a['vm_id']})")
    if not g["mdev_types"]:
        log("  no mdev types (no vGPU host?)")
    alloc = [a["mdev"] for a in g["allocated"]]
    for m in g["mdev_types"]:
        avail = m.get("available_instances", "?")
        in_use = alloc.count(m["type"])
        try:
            remaining = int(avail) - in_use
            avail_str = f"{avail} ( {in_use} in use → {remaining} free)"
        except (TypeError, ValueError):
            avail_str = f"{avail} ( {in_use} in use)"
        log(f"  {m['type']:<16} {m.get('name', ''):<28} avail {avail_str}")
    if g["pci"]:
        log(f"  GPU PCI:    {', '.join(g['pci'])}")
    if g["allocated"]:
        for a in g["allocated"]:
            log(f"  attached:   {a['name']} (VMID {a['vmid']}) → {a['mdev']}")
    log("")
    log(f"Templates: {', '.join(t['name'] for t in info['templates']) or 'none'}")
    log(f"VMIDs: next {info['vmids']['next']} · next template {info['vmids']['next_template']}")


def cmd_engine_scan(cfg, qm, argv, json_out=False):
    info = engine_scan(cfg, qm)
    if json_out:
        log(json.dumps(info, indent=2))
    else:
        _render_scan(cfg, qm, info)
    return 0


# ---------------------------------------------------------------------------
# doctor


def cmd_engine_doctor(cfg, qm, argv):
    checks = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    check("config readable", cfg is not None,
          "" if cfg else "set PHASE_CONFIG or create /etc/phase.json")
    if cfg:
        pattern = cfg.get("naming.pattern", "")
        try:
            re.compile(pattern)
            check("naming.pattern regex", True)
        except re.error as e:
            check("naming.pattern regex", False, str(e))
        sizes = cfg.get("templates.sizes") or {}
        check("sizes defined", len(sizes) > 0, ", ".join(sizes.keys()) or "none")
        bad = [n for n, v in sizes.items()
               if not isinstance(v, dict) or not v.get("cores") or not v.get("memory")]
        check("sizes valid", not bad, f"bad: {', '.join(bad)}" if bad else "")
        bdir = cfg.get("bootstrap.directory", "/opt/phase/bootstrap")
        check("bootstrap dir exists", os.path.isdir(bdir), bdir)
    for bin_ in ("qm", "pvesh", "pvesm", "ssh"):
        check(f"binary: {bin_}", qm.t.which(bin_) is not None)
    # Optional binaries are informational, not failures.
    for bin_ in ("mdevctl", "nvidia-smi", "vzdump", "jq", "uv"):
        present = qm.t.which(bin_) is not None
        mark = "✓" if present else "·"
        log(f"{mark} binary: {bin_} (optional)" + ("" if present else " — not found"))
    check("templates reachable", len(list_templates(qm)) > 0)
    check("python", True, sys.version.split()[0])

    failed = [c for c in checks if not c["ok"]]
    for c in checks:
        mark = "✓" if c["ok"] else "✗"
        log(f"{mark} {c['name']}" + (f" — {c['detail']}" if c["detail"] else ""))
    log("")
    if failed:
        log(f"{len(failed)} check(s) failed")
        return 1
    log("all checks passed")
    return 0


# ---------------------------------------------------------------------------
# daemon


def _load_jobs(cfg) -> list[dict]:
    return cfg.get("engine.jobs") or []


def _run_job(cfg, qm, job: dict) -> bool:
    cmd = job.get("command") or []
    if not cmd:
        return False
    log(f"[engine] running job {job.get('name', '?')}: {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    ok = proc.returncode == 0
    append_event(event="engine.job", job=job.get("name", "?"),
                 status="ok" if ok else "error",
                 detail=(proc.stdout + proc.stderr).strip()[:500])
    if not ok:
        warn(f"[engine] job {job.get('name')} failed ({proc.returncode})")
    send(cfg, f"{'✓' if ok else '✗'} engine job: {job.get('name', '?')}",
         (proc.stdout + proc.stderr).strip()[:500])
    return ok


def _load_engine_state() -> dict:
    try:
        with open(engine_state_path()) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"last_run": {}}


def _save_engine_state(st: dict) -> None:
    os.makedirs(os.path.dirname(engine_state_path()), exist_ok=True)
    with open(engine_state_path(), "w") as f:
        json.dump(st, f, indent=2)


async def _socket_server(cfg, qm, stop_evt: asyncio.Event):
    path = engine_socket_path()
    if os.path.exists(path):
        os.unlink(path)
    server = await asyncio.start_unix_server(
        lambda r, w: _handle_client(cfg, qm, r, w), path=path)
    os.chmod(path, 0o600)
    log(f"[engine] socket listening on {path}")
    async with server:
        await stop_evt.wait()


async def _handle_client(cfg, qm, reader, writer):
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        req = json.loads(line)
        cmd = req.get("cmd")
        resp = {"ok": True}
        if cmd == "status":
            resp.update({"running": True, "jobs": len(_load_jobs(cfg))})
        elif cmd == "jobs":
            resp["jobs"] = _load_jobs(cfg)
        elif cmd == "events":
            resp["events"] = tail_events(int(req.get("n", 20)))
        elif cmd == "run":
            job = next((j for j in _load_jobs(cfg)
                        if j.get("name") == req.get("job")), None)
            if not job:
                resp = {"ok": False, "error": f"no job named {req.get('job')}"}
            else:
                resp["result"] = _run_job(cfg, qm, job)
        else:
            resp = {"ok": False, "error": f"unknown cmd: {cmd}"}
        writer.write((json.dumps(resp) + "\n").encode())
    except Exception as e:
        writer.write((json.dumps({"ok": False, "error": str(e)}) + "\n").encode())
    finally:
        writer.close()


async def _daemon_loop(cfg, qm):
    ensure_state_dirs()
    jobs = _load_jobs(cfg)
    st = _load_engine_state()
    log(f"[engine] daemon started — {len(jobs)} job(s), socket {engine_socket_path()}")
    append_event(event="engine.started", jobs=len(jobs))
    stop_evt = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_evt.set)
    task = asyncio.create_task(_socket_server(cfg, qm, stop_evt))
    while not stop_evt.is_set():
        for job in jobs:
            if not job.get("enabled", True):
                continue
            name = job.get("name", "?")
            interval = float(job.get("interval_seconds", 0))
            if interval <= 0:
                continue
            last = st.get("last_run", {}).get(name, 0)
            if time.time() - last >= interval:
                _run_job(cfg, qm, job)
                st.setdefault("last_run", {})[name] = time.time()
                _save_engine_state(st)
        try:
            await asyncio.wait_for(stop_evt.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
    log("[engine] daemon stopping")
    append_event(event="engine.stopped")
    task.cancel()
    return 0


def cmd_engine_daemon(cfg, qm, argv):
    if not cfg:
        cfg = __import__("phase.config", fromlist=["Config"]).Config({"engine": {}}, "?")
    return asyncio.run(_daemon_loop(cfg, qm))


# ---------------------------------------------------------------------------
# service management


def _unit_path(cfg) -> str:
    return cfg.get("engine.systemd.unit", "/etc/systemd/system/phase-engine.service")


def cmd_engine_install(cfg, qm, argv):
    opts, _ = parse_flags(argv, {"no-start": {"bool": True, "default": False},
                                 "bin": {"default": ""}})
    bin_path = opts["bin"] or shutil.which("phase") or "/usr/local/bin/phase"
    if not os.path.exists(bin_path) and not qm.t.which("phase"):
        warn("phase not on PATH — install first, then `phase engine install`")
    unit = SYSTEMD_UNIT.format(bin=bin_path, socket=engine_socket_path())
    path = _unit_path(cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(unit)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    if not opts["no-start"]:
        subprocess.run(["systemctl", "enable", "--now", "phase-engine"], check=True)
    log(f"Installed {path}")
    if opts["no-start"]:
        log("Service created but not started (--no-start); enable with:")
        log("  systemctl enable --now phase-engine")
    else:
        log("phase-engine.service enabled and running.")
    return 0


def cmd_engine_uninstall(cfg, qm, argv):
    if not confirm("Stop, disable and remove the phase-engine systemd service?"):
        die("aborted")
    subprocess.run(["systemctl", "disable", "--now", "phase-engine"], check=False)
    path = _unit_path(cfg)
    if os.path.isfile(path):
        os.unlink(path)
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    log("Removed phase-engine.service")
    return 0


def cmd_engine_status(cfg, qm, argv):
    p = subprocess.run(["systemctl", "is-active", "phase-engine"], capture_output=True,
                       text=True)
    active = p.stdout.strip()
    if active == "active":
        log("phase-engine: active")
    elif active == "inactive":
        log("phase-engine: installed but not running (systemctl start phase-engine)")
    else:
        log("phase-engine: not installed (phase engine install)")
    events = tail_events(3)
    if events:
        log("recent events:")
        for e in events:
            log(f"  {e.get('ts', '?')} {e.get('event', '?')} {e.get('name', e.get('detail', ''))}")
    return 0


def cmd_engine_logs(cfg, qm, argv):
    opts, _ = parse_flags(argv, {"n": {"default": "50"}})
    p = subprocess.run(["journalctl", "-u", "phase-engine", "-n", opts["n"], "--no-pager"],
                       capture_output=True, text=True, check=False)
    log(p.stdout.rstrip())
    return 0


def cmd_engine_jobs(cfg, qm, argv):
    if not argv or argv[0] == "list":
        jobs = _load_jobs(cfg)
        if not jobs:
            log("no jobs configured (config: engine.jobs)")
            return 0
        st = _load_engine_state()["last_run"]
        log(f"{'NAME':<24} {'INTERVAL':<12} {'ENABLED':<8} LAST RUN")
        log("-" * 64)
        for j in jobs:
            last = st.get(j.get("name", "?"), 0)
            log(f"{j.get('name', '?'):<24} {j.get('interval_seconds', 0):<12} "
                f"{'yes' if j.get('enabled', True) else 'no':<8} "
                f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(last)) if last else 'never'}")
        return 0
    if argv[0] == "run":
        if len(argv) < 2:
            die("usage: phase engine jobs run <name>")
        job = next((j for j in _load_jobs(cfg) if j.get("name") == argv[1]), None)
        if not job:
            die(f"no job named {argv[1]}")
        _run_job(cfg, qm, job)
        return 0
    die(f"unknown engine jobs command: {argv[0]}")

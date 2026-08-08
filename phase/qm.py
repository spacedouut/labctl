"""Typed wrappers around the `qm` CLI (and friends: pvesm, pvesh, vzdump).

Every call goes through a Transport so phase can run locally on the PVE host
or remotely via SSH without any other changes.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

from .util import PhaseError, quoted


class Qm:
    def __init__(self, transport):
        self.t = transport

    # -- raw ----------------------------------------------------------------

    def _run(self, args: list[str], **kw):
        return self.t.run(["qm", *args], **kw)

    # -- discovery -----------------------------------------------------------

    def list_vms(self) -> list[dict]:
        out = self._run(["list"]).stdout
        vms = []
        row_re = re.compile(
            r"^\s*(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$")
        for line in out.strip().splitlines():
            m = row_re.match(line)
            if not m:
                continue
            vms.append({
                "vmid": int(m.group(1)),
                "name": m.group(2),
                "status": m.group(3),
                "mem": m.group(4),
                "bootdisk": m.group(5),
                "pid": m.group(6),
            })
        return vms

    def config(self, vmid: int) -> dict[str, str]:
        out = self._run(["config", str(vmid)]).stdout
        cfg = {}
        for line in out.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                cfg[k.strip()] = v.strip()
        return cfg

    def status(self, vmid: int) -> str:
        out = self._run(["status", str(vmid)]).stdout
        m = re.search(r"status:\s*(\S+)", out)
        return m.group(1) if m else "unknown"

    # -- mutation ------------------------------------------------------------

    def set(self, vmid: int, **opts) -> None:
        args = ["set", str(vmid)]
        for k, v in opts.items():
            args.append(f"--{k}")
            if v is not None:
                args.append(str(v))
        self._run(args)

    def clone(self, src: int, vmid: int, **opts) -> None:
        args = ["clone", str(src), str(vmid)]
        for k, v in opts.items():
            args.append(f"--{k}")
            if v is not None:
                args.append(str(v))
        self._run(args)

    def _power(self, op: str, vmid: int) -> None:
        self._run([op, str(vmid)])

    def start(self, vmid): self._power("start", vmid)
    def stop(self, vmid): self._power("stop", vmid)
    def reboot(self, vmid): self._power("reboot", vmid)
    def reset(self, vmid): self._power("reset", vmid)
    def shutdown(self, vmid): self._power("shutdown", vmid)
    def suspend(self, vmid): self._power("suspend", vmid)

    def resize(self, vmid: int, disk: str, size: str) -> None:
        self._run(["resize", str(vmid), disk, size])

    def make_template(self, vmid: int) -> None:
        self._run(["template", str(vmid)])

    def destroy(self, vmid: int, purge: bool = True) -> None:
        args = ["destroy", str(vmid)]
        if purge:
            args.append("--purge")
        self._run(args)

    def snapshot(self, vmid: int, name: str) -> None:
        self._run(["snapshot", str(vmid), name])

    def listsnapshot(self, vmid: int) -> list[dict]:
        out = self._run(["listsnapshot", str(vmid)]).stdout
        rows = []
        for line in out.splitlines():
            if not line.strip().startswith("|"):
                continue  # border lines
            cells = [c.strip() for c in line.split("|")]
            if len(cells) >= 4 and cells[1] and cells[1] != "Name":
                rows.append({"name": cells[1], "desc": cells[2], "vmstate": cells[3]})
        return rows

    def rollback(self, vmid: int, name: str) -> None:
        self._run(["rollback", str(vmid), name])

    def terminal(self, vmid: int) -> None:
        os.execvp("qm", ["qm", "terminal", str(vmid)])

    def pvesh(self, path: str):
        return pvesh_get(self.t, path)

    def pvesm(self) -> list[dict]:
        return pvesm_status(self.t)

    def vzdump(self, vmid: int, **opts) -> str:
        return vzdump_backup(self.t, vmid, **opts)

    # -- guest agent ---------------------------------------------------------

    def guest_ping(self, vmid: int) -> bool:
        return self._run(["guest", "cmd", str(vmid), "ping"], check=False).returncode == 0

    def guest_cmd(self, vmid: int, *cmd):
        p = self._run(["guest", "cmd", str(vmid), *cmd], check=False)
        if p.returncode != 0:
            return None
        try:
            return json.loads(p.stdout)
        except json.JSONDecodeError:
            return None

    def guest_exec(self, vmid: int, argv: list[str], timeout: int = 0) -> dict:
        """Run a command in the guest via QGA, blocking until it exits.
        Streams out-data/err-data; raises PhaseError on non-zero exit."""
        start = self._run(
            ["guest", "exec", str(vmid), "--timeout", str(timeout), "--", *argv]
        ).stdout
        try:
            resp = json.loads(start)
        except json.JSONDecodeError:
            raise PhaseError(f"bad guest exec response: {start[:200]}")
        if resp.get("exited"):
            self._check_exit(resp, argv)
            return resp
        pid = resp.get("pid")
        if pid is None:
            raise PhaseError(f"guest exec: no pid in response: {start[:200]}")
        while True:
            time.sleep(1)
            s = self._run(["guest", "exec-status", str(vmid), str(pid)]).stdout
            try:
                st = json.loads(s)
            except json.JSONDecodeError:
                raise PhaseError(f"bad guest exec-status response: {s[:200]}")
            if st.get("exited"):
                self._check_exit(st, argv)
                return st

    def _check_exit(self, st: dict, argv: list[str]) -> None:
        code = st.get("exitcode", 1)
        out = st.get("out-data", "") or ""
        err = st.get("err-data", "") or ""
        if out:
            sys.stdout.write(out)
        if err:
            sys.stderr.write(err)
        if code != 0:
            raise PhaseError(f"guest command failed ({code}): {quoted(argv)}")

    def guest_exec_stdin(self, vmid: int, data: str, argv: list[str]) -> None:
        self._run(
            ["guest", "exec", str(vmid), "--pass-stdin", "1", "--", *argv],
            input=data,
        )

    # -- helpers -------------------------------------------------------------

    def wait_for_agent(self, vmid: int, timeout: float = 120.0, poll: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.guest_ping(vmid):
                return True
            time.sleep(poll)
        return False

    def wait_for_stopped(self, vmid: int, timeout: float = 60.0, poll: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.status(vmid) != "running":
                return True
            time.sleep(poll)
        return False


def pvesm_status(transport) -> list[dict]:
    """Storage pools, one row per storage (pvesm status columns:
    Name Type Status Total Used Available %)."""
    p = transport.run(["pvesm", "status"], check=False)
    rows = []
    for line in p.stdout.strip().splitlines()[1:]:
        parts = [x for x in re.split(r"\s{2,}", line.strip()) if x]
        if len(parts) >= 6:
            rows.append({
                "name": parts[0], "type": parts[1], "status": parts[2],
                "total": parts[3], "used": parts[4], "avail": parts[5],
            })
    return rows


def pvesh_get(transport, path: str):
    """Structured host data via pvesh (JSON on stdout, no pip needed)."""
    p = transport.run(["pvesh", "get", path, "--output-format", "json"], check=False)
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return None


def vzdump_backup(transport, vmid: int, **opts) -> str:
    args = ["vzdump", str(vmid)]
    for k, v in opts.items():
        args.append(f"--{k}")
        if v is not None:
            args.append(str(v))
    return transport.run(args).stdout

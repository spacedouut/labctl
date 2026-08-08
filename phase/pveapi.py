"""PVE API client — the fast path for phase.

`qm` CLI subprocesses cost ~0.5-0.7s each on PVE (Perl startup), which made
web page loads take 7-10s. This module talks to pveproxy (localhost:8006)
over HTTP/JSON instead: same operations, ~10-50ms per call.

Implements the same method surface as `Qm` (phase/qm.py) so callers don't
care which backend is in use. `make_qm()` in phase/transport.py picks the
backend: PVE API when `pve.api_token` is configured, else the qm CLI.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .util import PhaseError, quoted

DEFAULT_URL = "https://localhost:8006/api2/json"


def _human(n: int) -> str:
    """Bytes → the same human units pvesm shows (e.g. '32.55 GiB')."""
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return str(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.2f} TiB"


class PveApi:
    """Typed wrappers over the PVE API (pveproxy). Mirrors Qm's interface.

    Auth: PVE API token — config `pve.api_token`, full form
    `user@realm!tokenid=secret` (create with
    `pveum user token add root@pam phase --privsep 0`).
    """

    def __init__(self, token: str, url: str | None = None,
                 verify_tls: bool | None = None, timeout: float = 15.0):
        self.token = token
        self.base = url or os.environ.get("PVE_API_URL") or DEFAULT_URL
        self.timeout = timeout
        self._node: str | None = None
        if verify_tls is None:
            verify_tls = os.environ.get("PVE_VERIFY_TLS", "1") not in ("0", "false", "no")
        self._ctx = self._make_ctx(verify_tls)

    # -- plumbing -----------------------------------------------------------

    def _make_ctx(self, verify: bool) -> ssl.SSLContext | None:
        if not self.base.startswith("https"):
            return None
        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        # pveproxy presents a cert signed by the PVE root CA
        ca = "/etc/pve/pve-root-ca.pem"
        if os.path.isfile(ca):
            try:
                ctx.load_verify_locations(cafile=ca)
            except ssl.SSLError:
                pass
        return ctx

    def _req(self, method: str, path: str, body: dict | None = None,
             timeout: float | None = None) -> dict:
        """One API call; returns the decoded `data` envelope."""
        url = self.base.rstrip("/") + path
        data = None
        headers = {"Authorization": f"PVEAPIToken={self.token}"}
        if body:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout,
                                       context=self._ctx) as resp:
                raw = resp.read().decode()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:400]
            except Exception:  # noqa: BLE001
                pass
            raise PhaseError(f"PVE API {method} {path}: HTTP {e.code} {detail}")
        except urllib.error.URLError as e:
            raise PhaseError(f"PVE API {method} {path}: {e.reason}")
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            raise PhaseError(f"PVE API {method} {path}: bad JSON: {raw[:200]}")
        if isinstance(envelope, dict) and "data" in envelope:
            return envelope["data"]
        return envelope

    def node(self) -> str:
        if self._node is None:
            nodes = self._req("GET", "/nodes")
            if not nodes:
                raise PhaseError("PVE API: no nodes found")
            self._node = nodes[0].get("node") if isinstance(nodes, list) else "pve"
        return self._node

    # -- discovery -----------------------------------------------------------

    def list_vms(self) -> list[dict]:
        rows = self._req("GET", "/cluster/resources?type=vm")
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            out.append({
                "vmid": r.get("vmid"),
                "name": r.get("name") or f"vm-{r.get('vmid')}",
                "status": r.get("status", "unknown"),
                "mem": r.get("mem") or r.get("maxmem") or 0,
                "bootdisk": r.get("maxdisk", 0),
                "pid": r.get("pid", 0),
                "template": 1 if r.get("template") else 0,
            })
        return out

    def config(self, vmid: int) -> dict[str, str]:
        raw = self._req("GET", f"/nodes/{self.node()}/qemu/{vmid}/config")
        out = {}
        for k, v in (raw or {}).items():
            out[str(k)] = str(v)
        return out

    def status(self, vmid: int) -> str:
        raw = self._req("GET", f"/nodes/{self.node()}/qemu/{vmid}/status/current")
        return raw.get("status", "unknown") if isinstance(raw, dict) else "unknown"

    def nextid(self) -> int:
        return int(self._req("GET", "/cluster/nextid"))

    # -- mutation ------------------------------------------------------------

    def set(self, vmid: int, **opts) -> None:
        self._req("PUT", f"/nodes/{self.node()}/qemu/{vmid}/config", opts)

    def clone(self, src: int, vmid: int, **opts) -> None:
        body = {"newid": vmid}
        for k, v in opts.items():
            if v is not None:
                body[k] = v
        self._req("POST", f"/nodes/{self.node()}/qemu/{src}/clone", body)

    def _power(self, op: str, vmid: int) -> None:
        self._req("POST", f"/nodes/{self.node()}/qemu/{vmid}/status/{op}")

    def start(self, vmid): self._power("start", vmid)
    def stop(self, vmid): self._power("stop", vmid)
    def reboot(self, vmid): self._power("reboot", vmid)
    def reset(self, vmid): self._power("reset", vmid)
    def shutdown(self, vmid): self._power("shutdown", vmid)
    def suspend(self, vmid): self._power("suspend", vmid)

    def resize(self, vmid: int, disk: str, size: str) -> None:
        self._req("PUT", f"/nodes/{self.node()}/qemu/{vmid}/resize",
                  {"disk": disk, "size": size})

    def make_template(self, vmid: int) -> None:
        self._req("POST", f"/nodes/{self.node()}/qemu/{vmid}/template")

    def destroy(self, vmid: int, purge: bool = True) -> None:
        q = "?purge=1" if purge else ""
        self._req("DELETE", f"/nodes/{self.node()}/qemu/{vmid}{q}")

    def snapshot(self, vmid: int, name: str) -> None:
        self._req("POST", f"/nodes/{self.node()}/qemu/{vmid}/snapshot",
                  {"snapname": name})

    def listsnapshot(self, vmid: int) -> list[dict]:
        rows = self._req("GET", f"/nodes/{self.node()}/qemu/{vmid}/snapshot")
        out = []
        for r in rows or []:
            out.append({
                "name": r.get("name", ""),
                "desc": r.get("description", ""),
                "vmstate": "yes" if r.get("vmstate") else "",
            })
        return out

    def rollback(self, vmid: int, name: str) -> None:
        self._req("POST",
                  f"/nodes/{self.node()}/qemu/{vmid}/snapshot/{name}/rollback")

    def terminal(self, vmid: int) -> None:
        raise PhaseError("terminal is not available via the PVE API (use the CLI)")

    # -- guest agent ---------------------------------------------------------

    def guest_ping(self, vmid: int) -> bool:
        try:
            self._req("POST", f"/nodes/{self.node()}/qemu/{vmid}/agent/ping")
            return True
        except PhaseError:
            return False

    def _agent_exec(self, vmid: int, command: list[str],
                    timeout: int = 0, input_data: str | None = None) -> dict:
        body = {"command": json.dumps(command)}
        if timeout:
            body["timeout"] = timeout
        if input_data is not None:
            body["input-data"] = input_data
        resp = self._req("POST",
                         f"/nodes/{self.node()}/qemu/{vmid}/agent/exec", body)
        if resp.get("exited"):
            return resp
        pid = resp.get("pid")
        if pid is None:
            raise PhaseError(f"guest exec: no pid in response: {resp}")
        deadline = time.monotonic() + (timeout or 300)
        while time.monotonic() < deadline:
            time.sleep(1)
            st = self._req("GET",
                           f"/nodes/{self.node()}/qemu/{vmid}/agent/exec-status"
                           f"?pid={pid}")
            if st.get("exited"):
                return st
        raise PhaseError(f"guest exec timed out after {timeout or 300}s")

    def guest_cmd(self, vmid: int, *cmd):
        """Run a QGA command; returns parsed JSON of its stdout (or None)."""
        try:
            resp = self._agent_exec(vmid, list(cmd), timeout=30)
        except PhaseError:
            return None
        out = resp.get("out-data") or ""
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return None

    def guest_exec(self, vmid: int, argv: list[str], timeout: int = 0) -> dict:
        resp = self._agent_exec(vmid, argv, timeout=timeout)
        self._check_exit(resp, argv)
        return resp

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
        self._agent_exec(vmid, argv, input_data=data)

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

    # -- storage / host reads -------------------------------------------------

    def pvesm(self) -> list[dict]:
        rows = self._req("GET", f"/nodes/{self.node()}/storage")
        out = []
        for r in rows or []:
            out.append({
                "name": r.get("storage", ""),
                "type": r.get("type", ""),
                "status": r.get("status", ""),
                "total": _human(r.get("total", 0)),
                "used": _human(r.get("used", 0)),
                "avail": _human(r.get("avail", 0)),
            })
        return out

    def pvesh(self, path: str):
        return self._req("GET", path)

    def vzdump(self, vmid: int, **opts) -> str:
        """Start a backup task and block until it finishes; return the log."""
        body = {"vmid": vmid}
        for k, v in opts.items():
            if v is not None:
                body[k] = v
        upid = self._req("POST", f"/nodes/{self.node()}/vzdump", body)
        upid = upid if isinstance(upid, str) else upid.get("data", "")
        lines = []
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            st = self._req("GET", f"/nodes/{self.node()}/tasks/{upid}/status")
            log = self._req("GET", f"/nodes/{self.node()}/tasks/{upid}/log?limit=1000")
            if isinstance(log, list):
                lines = [l.get("n", "") for l in log if l.get("n")]
            if st.get("status") == "stopped":
                break
            time.sleep(2)
        return "\n".join(lines)

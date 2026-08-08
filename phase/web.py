"""phase web — VM management console served locally. No deps beyond stdlib.

`phase web [--listen 127.0.0.1] [--port 8080] [--token ***]` serves a
single-page UI (phase/webui/) plus a JSON API. It reuses the same plan,
provision, qm and ssh code as the CLI, so the UI can never drift from what
`phase` actually does.

Endpoints (all /api/* require X-Phase-Token when a token is configured):
  GET  /                  -> the app (index.html)
  GET  /app.js /app.css   -> assets
  GET  /static/<file>     -> vendored xterm.js etc.
  GET  /api/meta          -> sizes, templates, networks, keys, plans
  GET  /api/host          -> host health and storage summary
  GET  /api/settings      -> editable, non-secret phase settings
  GET  /api/plans/<name>  -> full saved plan
  GET  /api/task/<id>     -> async task status (running|done|error)
  GET  /api/vms           -> VM list
  GET  /api/vms/<name>    -> VM detail (config, disks, status, ip)
  GET  /api/vms/<name>/snapshots
  GET  /api/vms/<name>/firewall     (runs ufw status in the guest)
  POST /api/plan          -> validate a wizard state, return plan + errors
  POST /api/save          -> save a plan to the library
  POST /api/create        -> create the VM (stopped)
  POST /api/provision     -> create + start + bootstrap (async task)
  POST /api/vms/<name>/power       {action: start|stop|reboot|shutdown|pause|resume}
  POST /api/vms/<name>/edit        {cores,memory,description,tags,onboot,protection}
  POST /api/vms/<name>/disks/add   {id,size,storage}
  POST /api/vms/<name>/disks/resize {id,size}
  POST /api/vms/<name>/firewall    {from,port,proto}
  POST /api/vms/<name>/snapshot    {name}
  POST /api/vms/<name>/backup
  POST /api/vms/<name>/destroy
  POST /api/settings      -> persist editable phase settings
  GET  /api/ssh/<name>[?token=..]  -> WebSocket SSH terminal (upgrade)
  GET  /api/serial/<name>[?token=..] -> WebSocket VM serial console
  GET  /api/host/terminal[?token=..] -> WebSocket host shell

Long operations run as async tasks; the page polls /api/task/<id>.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import pty as pty_mod
import select
import secrets
import socket
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote

from .log import append_event
from .pveapi import pam_login
from .plan import list_plans, read_plan, realize_plan, save_plan
from .provision import provision_plan
from .util import die, log
from .vm import (all_vms, best_guest_ip, find_vmid, list_template_oses,
                 next_vmid, ssh_argv, system_disk, vm_ssh_user_ip)
from .wizard import validate_step
from .inline_wizard import _dget, _summary_lines, _wizard_state, state_to_plan

WEBUI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui")
_TASKS: dict[str, dict] = {}
_SESSIONS: dict[str, dict] = {}
_SESSION_LOCK = threading.Lock()
_SESSION_TTL = 12 * 60 * 60

# For tests: override what the ssh terminal spawns (e.g. "cat").
# Read at request time so tests can set it after import.


# ---------------------------------------------------------------------------
# async tasks


def _start_task(fn, *args, **kw) -> str:
    tid = uuid.uuid4().hex[:12]
    _TASKS[tid] = {"status": "running"}

    def worker():
        try:
            result = fn(*args, **kw)
            _TASKS[tid] = {"status": "done", "result": result}
        except Exception as e:  # noqa: BLE001 — report to the UI
            _TASKS[tid] = {"status": "error", "error": str(e)}
        _cache_drop()  # every task here mutates VMs — refresh caches

    threading.Thread(target=worker, daemon=True).start()
    return tid


# ---------------------------------------------------------------------------
# helpers


def _meta(cfg, qm) -> dict:
    images = []
    for os_name in list_template_oses(cfg, qm):
        if os_name == "none":
            continue
        try:
            vmid = find_vmid(qm, f"{_dget(cfg, 'templates.prefix', 'tpl')}{_dget(cfg, 'templates.separator', '-')}{os_name}")
            conf = qm.config(vmid)
            disks = [k for k in conf if _DISK_RE.match(k)]
            # One system image per OS.  Config only needs an override for an
            # unusually-laid-out template; ordinary templates use first disk.
            disk = _dget(cfg, f"templates.system_disks.{os_name}") or (sorted(disks)[0] if disks else "scsi0")
            images.append({"os": os_name, "template": vmid, "disk": disk})
        except Exception:
            continue
    return {
        "host": os.uname().nodename,
        "sizes": _dget(cfg, "templates.sizes") or {},
        "oses": [o for o in list_template_oses(cfg, qm) if o != "none"],
        "networks": list((_dget(cfg, "networks") or {}).keys()),
        "ssh_keys": list(_dget(cfg, "vm.ssh_keys") or []),
        "storages": [s.get("name") for s in qm.pvesm() if s.get("name")],
        "plans": list_plans(),
        "next_vmid": next_vmid(qm),
        "system_images": images,
    }


def _host_detail(qm) -> dict:
    """Small, safe host summary.  PVE API and CLI backends share pvesh."""
    try:
        node = qm.node() if hasattr(qm, "node") else os.uname().nodename
        status = qm.pvesh(f"/nodes/{node}/status") or {}
        storage = qm.pvesh(f"/nodes/{node}/storage") or []
        return {"node": node, "status": status, "storage": storage}
    except Exception as e:  # host telemetry should not break the console
        return {"node": "", "status": {}, "storage": [], "error": str(e)}


_EDITABLE_SETTINGS = {
    "default_user": "default_user",
    "default_bridge": "default_bridge",
    "default_storage": "default_storage",
    "vm_agent": "vm.agent",
    "backup_storage": "backup.storage",
}


def _public_settings(cfg) -> dict:
    """Never send credentials or web tokens to the browser."""
    return {key: _dget(cfg, dotted, "") for key, dotted in _EDITABLE_SETTINGS.items()}


def _save_settings(cfg, updates: dict) -> dict:
    if not isinstance(updates, dict):
        raise ValueError("settings must be an object")
    new_token = ""
    for key, value in updates.items():
        if key == "rotate_token" and value:
            new_token = secrets.token_urlsafe(24)
            cfg.data.setdefault("web", {})["token"] = new_token
            continue
        dotted = _EDITABLE_SETTINGS.get(key)
        if not dotted:
            continue
        if key == "vm_agent":
            value = bool(value)
        elif not isinstance(value, str):
            raise ValueError(f"{key} must be text")
        node = cfg.data
        bits = dotted.split(".")
        for bit in bits[:-1]:
            node = node.setdefault(bit, {})
            if not isinstance(node, dict):
                raise ValueError(f"cannot update {key}: invalid config shape")
        node[bits[-1]] = value.strip() if isinstance(value, str) else value
    cfg.save()
    out = _public_settings(cfg)
    if new_token:
        out["new_token"] = new_token  # returned once, only to an authenticated caller
    return out


def _web_auth_config(cfg, token: str) -> tuple[str, set[str], int]:
    """Return auth mode, allowed users and session lifetime.

    PAM mode is opt-in and deny-by-default: an empty `web.pam_users` list
    cannot accidentally turn every privileged PVE PAM account into a Phase
    administrator.
    """
    mode = str(_dget(cfg, "web.auth") or ("token" if token else "none")).lower()
    if mode not in {"none", "token", "pam"}:
        raise ValueError("web.auth must be one of: none, token, pam")
    users = {str(u) for u in (_dget(cfg, "web.pam_users") or [])}
    ttl_hours = _dget(cfg, "web.session_ttl_hours", 12)
    try:
        ttl = max(1, min(int(ttl_hours), 168)) * 60 * 60
    except (TypeError, ValueError):
        ttl = _SESSION_TTL
    return mode, users, ttl


# ---- read caches (qm subprocess calls cost ~0.5s each on PVE) ------------
# meta and the VM list are read-only aggregates; serve them from a short TTL
# cache so page loads don't stall on 6-10s of sequential qm calls. Every
# mutating POST invalidates the affected entries.

_CACHE: dict = {}
_META_TTL = 30.0     # templates/sizes/plans change rarely
_VMS_TTL = 8.0       # status/ip freshness target
_STALE_MAX = 120.0   # never block a page load: serve up to 2min-old data


def _cache_get(key: str, ttl: float, build):
    """Serve cached data; when stale, refresh in the background (SWR).

    The very first build for a key blocks (cold path — the server warms
    both caches at startup so that's rare). Every later request is served
    immediately from cache; stale entries are re-built on a daemon thread
    so a slow 10s qm build never blocks the UI again.
    """
    hit = _CACHE.get(key)
    if hit is not None:
        age = time.monotonic() - hit["ts"]
        if age < _STALE_MAX:
            if age >= ttl and not hit.get("building"):
                hit["building"] = True

                def _refresh():
                    try:
                        value = build()
                        _CACHE[key] = {"ts": time.monotonic(), "value": value}
                    except Exception:  # noqa: BLE001 — keep stale on failure
                        _CACHE[key] = {"ts": hit["ts"], "value": hit["value"]}

                threading.Thread(target=_refresh, daemon=True).start()
            return hit["value"]
        # too old to serve: fall through to a fresh build
    value = build()
    _CACHE[key] = {"ts": time.monotonic(), "value": value}
    return value


def _cache_drop(keys=("meta", "vms")):
    for k in keys:
        _CACHE.pop(k, None)


def _cached_meta(cfg, qm) -> dict:
    return _cache_get("meta", _META_TTL, lambda: _meta(cfg, qm))


def _cached_vms(qm, cfg) -> list:
    return _cache_get("vms", _VMS_TTL, lambda: all_vms(qm, cfg))


_DISK_RE = __import__("re").compile(r"^(scsi|sata|virtio|ide)\d+$")


def _vm_detail(cfg, qm, name: str) -> dict:
    vmid = find_vmid(qm, name)
    conf = qm.config(vmid)
    status = qm.status(vmid)
    provenance = system_disk(vmid)
    system_disk_id = provenance.get("disk", "")
    disks = []
    for k, v in conf.items():
        if _DISK_RE.match(k):
            volume = v.split(",")[0]
            parts = volume.split(":", 1)
            size_match = __import__("re").search(r"(?:^|,)size=([^,]+)", v)
            disks.append({
                "id": k,
                "bus": __import__("re").match(r"^[a-z]+", k).group(0),
                "size": size_match.group(1) if size_match else "",
                "storage": parts[0] if parts else "",
                "volume": parts[1] if len(parts) > 1 else volume,
                "role": "system" if k == system_disk_id else "data",
                "image": provenance.get("image", "") if k == system_disk_id else "",
            })
    return {
        "vmid": vmid,
        "name": name,
        "status": status,
        "ip": best_guest_ip(qm, cfg, vmid) if status == "running" else "",
        "tags": conf.get("tags", ""),
        "cores": conf.get("cores", ""),
        "memory": conf.get("memory", ""),
        "description": conf.get("description", ""),
        "onboot": conf.get("onboot", "1"),
        "protection": conf.get("protection", "0"),
        "template": conf.get("template") == "1",
        "net0": conf.get("net0", ""),
        "disks": disks,
    }


def _firewall_add(cfg, qm, name: str, frm: str, port: str,
                  proto: str = "tcp") -> str:
    vmid = find_vmid(qm, name)
    if not frm or not port:
        raise ValueError("--from and --port are required")
    if proto not in ("tcp", "udp"):
        raise ValueError("--proto must be tcp or udp")
    cidrs = _dget(cfg, f"networks.{frm}") or [frm]
    for cidr in cidrs:
        qm.guest_exec(vmid, ["sudo", "ufw", "allow", "from", cidr,
                             "to", "any", "port", port, "proto", proto])
    return f"allowed {frm} → port {port}/{proto}"


def _vm_destroy(cfg, qm, name: str) -> str:
    vmid = find_vmid(qm, name)
    try:
        qm.stop(vmid)
    except Exception:
        pass
    qm.destroy(vmid)
    append_event(event="vm.destroyed", vmid=vmid, name=name)
    return f"destroyed {name} (VMID {vmid})"


def _vm_backup(cfg, qm, name: str) -> str:
    vmid = find_vmid(qm, name)
    storage = _dget(cfg, "backup.storage") or "local"
    out = qm.vzdump(vmid, storage=storage, mode="snapshot")
    for line in out.splitlines():
        if "INFO: Finished" in line or "INFO: Starting" in line:
            log(line)
    append_event(event="vm.backup", vmid=vmid, name=name, storage=storage)
    return f"backup of {name} → {storage}"


def _plan_response(state: dict, cfg) -> dict:
    errs = {}
    for i in range(1, 6):
        e = validate_step(_wizard_state(state), i, cfg)
        if e:
            errs[str(i)] = e
    if errs:
        return {"ok": False, "errors": errs}
    plan = state_to_plan(state, cfg)
    return {"ok": True, "plan": plan, "plan_text": "\n".join(_summary_lines(state))}


# ---------------------------------------------------------------------------
# websocket (RFC 6455, minimal but correct)


def _ws_accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1(
        (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()


def _ws_send(sock: socket.socket, payload: bytes, opcode: int = 0x1) -> None:
    frame = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        frame.append(n)
    elif n < 65536:
        frame.append(126)
        frame += n.to_bytes(2, "big")
    else:
        frame.append(127)
        frame += n.to_bytes(8, "big")
    sock.sendall(bytes(frame) + payload)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf


def _ws_read_message(sock: socket.socket):
    """Read one complete websocket message. Returns (opcode, payload) or
    None on close. Handles ping/pong/close and fragmentation."""
    opcode = None
    payload = b""
    while True:
        hdr = _recv_exact(sock, 2)
        b1, b2 = hdr[0], hdr[1]
        fin = bool(b1 & 0x80)
        op = b1 & 0x0F
        masked = bool(b2 & 0x80)
        ln = b2 & 0x7F
        if ln == 126:
            ln = int.from_bytes(_recv_exact(sock, 2), "big")
        elif ln == 127:
            ln = int.from_bytes(_recv_exact(sock, 8), "big")
        mask = _recv_exact(sock, 4) if masked else None
        data = _recv_exact(sock, ln)
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if op == 0x8:  # close
            _ws_send(sock, data[:2] if len(data) >= 2 else b"", opcode=0x8)
            return None
        if op == 0x9:  # ping
            _ws_send(sock, data, opcode=0xA)
            continue
        if op == 0xA:  # pong
            continue
        if op in (0x1, 0x2):
            opcode = op
            payload += data
            if fin:
                return opcode, payload
        elif op == 0x0 and opcode is not None:  # continuation
            payload += data
            if fin:
                return opcode, payload


# ---------------------------------------------------------------------------
# handler


def make_handler(cfg, qm, token: str = ""):
    token_box = {"value": token}
    auth_mode, pam_users, session_ttl = _web_auth_config(cfg, token)
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # ---- helpers ----------------------------------------------------

        def _session(self) -> dict | None:
            """Return a live Phase session, expiring it on read."""
            cookie = self.headers.get("Cookie", "")
            sid = next((p.strip()[14:] for p in cookie.split(";")
                        if p.strip().startswith("phase_session=")), "")
            if not sid:
                return None
            with _SESSION_LOCK:
                session = _SESSIONS.get(sid)
                if not session or session["expires"] <= time.time():
                    _SESSIONS.pop(sid, None)
                    return None
                return session

        def _authed(self) -> bool:
            if auth_mode == "none":
                return True
            if auth_mode == "pam":
                return self._session() is not None
            return self.headers.get("X-Phase-Token") == token_box["value"]

        def _ws_token_ok(self, query: str) -> bool:
            if auth_mode == "none":
                return True
            if auth_mode == "pam":
                return self._session() is not None
            return parse_qs(query).get("token", [""])[0] == token_box["value"]

        def _auth_state(self) -> dict:
            session = self._session()
            return {"mode": auth_mode, "authenticated": bool(self._authed()),
                    "user": session.get("user") if session else None,
                    "users": sorted(pam_users) if auth_mode == "pam" else []}

        def _set_session_cookie(self, sid: str):
            # Caddy terminates TLS and sends X-Forwarded-Proto. Keep local
            # `phase web` usable over http while making public cookies Secure.
            secure = self.headers.get("X-Forwarded-Proto", "").lower() == "https"
            cookie = f"phase_session={sid}; Path=/; HttpOnly; SameSite=Strict"
            if secure:
                cookie += "; Secure"
            self.send_header("Set-Cookie", cookie)

        def _json(self, obj, code=200, session_cookie: str = ""):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if session_cookie:
                self._set_session_cookie(session_cookie)
            self.end_headers()
            self.wfile.write(body)

        def _file(self, rel: str, ctype: str, code=200):
            path = os.path.join(WEBUI_DIR, rel)
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                self._json({"error": f"missing file: {rel}"}, 404)
                return
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_state(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                self._json({"error": "bad json"}, 400)
                return None
            state = data.get("state")
            if not isinstance(state, dict):
                self._json({"error": "missing 'state'"}, 400)
                return None
            return state

        def _task_json(self, fn, *args, **kw):
            tid = _start_task(fn, *args, **kw)
            return self._json({"ok": True, "task": tid})

        def log_message(self, fmt, *args):
            # never log query strings (ws token rides in ?token=)
            path = self.path.split("?", 1)[0]
            log(f"web: {self.address_string()} {self.command} {path}")

        # ---- GET ----------------------------------------------------------

        def do_GET(self):
            raw = self.path
            path = unquote(raw.split("?")[0])
            query = raw.split("?", 1)[1] if "?" in raw else ""
            if path == "/":
                return self._file("index.html", "text/html; charset=utf-8")
            if path == "/terminal.html":
                return self._file("terminal.html", "text/html; charset=utf-8")
            if path == "/app.js":
                return self._file("app.js", "text/javascript; charset=utf-8")
            if path == "/app.css":
                return self._file("app.css", "text/css; charset=utf-8")
            if path.startswith("/static/"):
                rel = "static/" + path[len("/static/"):]
                if rel.endswith(".woff2"):
                    return self._file(rel, "font/woff2")
                return self._file(rel,
                                  "text/javascript; charset=utf-8"
                                  if rel.endswith(".js") else "text/css")
            if path == "/api/auth/session":
                return self._json(self._auth_state())
            ws_path = (path == "/api/host/terminal" or
                       path.startswith("/api/serial/") or
                       path.startswith("/api/ssh/"))
            # Browser WebSockets cannot set X-Phase-Token; terminals carry
            # the token in their upgrade query instead.  Keep every other
            # API route header-gated.
            if not self._authed() and not (ws_path and self._ws_token_ok(query)):
                return self._json({"error": "unauthorized"}, 401)
            if path == "/api/meta":
                return self._json(_cached_meta(cfg, qm))
            if path == "/api/host":
                return self._json(_host_detail(qm))
            if path == "/api/settings":
                return self._json(_public_settings(cfg))
            if path == "/api/vms":
                return self._json(_cached_vms(qm, cfg))
            if path.startswith("/api/vms/") and path.endswith("/snapshots"):
                name = path[len("/api/vms/"):-len("/snapshots")]
                try:
                    return self._json(qm.listsnapshot(find_vmid(qm, name)))
                except Exception as e:  # noqa: BLE001
                    return self._json({"error": str(e)}, 400)
            if path.startswith("/api/vms/") and path.endswith("/firewall"):
                name = path[len("/api/vms/"):-len("/firewall")]
                vmid = find_vmid(qm, name)
                try:
                    st = qm.guest_exec(vmid, ["sudo", "ufw", "status", "numbered"])
                    return self._json({"rules": st.get("out-data", "")})
                except Exception as e:  # noqa: BLE001
                    return self._json({"error": str(e)}, 400)
            if path.startswith("/api/vms/"):
                name = path[len("/api/vms/"):]
                try:
                    return self._json(_vm_detail(cfg, qm, name))
                except Exception as e:  # noqa: BLE001
                    return self._json({"error": str(e)}, 404)
            if path.startswith("/api/plans/"):
                name = path[len("/api/plans/"):]
                try:
                    return self._json(read_plan(name))
                except Exception as e:  # noqa: BLE001
                    return self._json({"error": str(e)}, 404)
            if path.startswith("/api/task/"):
                tid = path[len("/api/task/"):]
                return self._json(_TASKS.get(tid, {"status": "unknown"}))
            if path == "/api/host/terminal":
                if not self._ws_token_ok(query):
                    return self._json({"error": "unauthorized"}, 401)
                return self._ws_terminal(argv=[os.environ.get("SHELL", "/bin/bash"), "-l"])
            if path.startswith("/api/serial/"):
                name = path[len("/api/serial/"):]
                if not self._ws_token_ok(query):
                    return self._json({"error": "unauthorized"}, 401)
                try:
                    return self._ws_terminal(argv=["qm", "terminal", str(find_vmid(qm, name)), "-escape", "none"])
                except Exception as e:  # noqa: BLE001
                    return self._json({"error": str(e)}, 400)
            if path.startswith("/api/ssh/"):
                name = path[len("/api/ssh/"):]
                if not self._ws_token_ok(query):
                    return self._json({"error": "unauthorized"}, 401)
                return self._ws_terminal(name)
            self._json({"error": "not found"}, 404)

        # ---- POST ---------------------------------------------------------

        def do_POST(self):
            path = unquote(self.path.split("?")[0])
            if path == "/api/auth/login":
                if auth_mode != "pam":
                    return self._json({"error": "PAM login is not enabled"}, 404)
                body = self._read_body() or {}
                username, password = body.get("username", ""), body.get("password", "")
                if not isinstance(username, str) or not isinstance(password, str):
                    return self._json({"error": "username and password are required"}, 400)
                username = username.strip()
                if not username.endswith("@pam") or username not in pam_users:
                    return self._json({"error": "this PAM user is not allowed in Phase"}, 403)
                try:
                    user = pam_login(username, password, url=_dget(cfg, "pve.api_url") or None,
                                     verify_tls=_dget(cfg, "pve.verify_tls", True))
                except Exception as e:  # noqa: BLE001
                    return self._json({"error": str(e)}, 401)
                sid = secrets.token_urlsafe(32)
                with _SESSION_LOCK:
                    _SESSIONS[sid] = {"user": user, "expires": time.time() + session_ttl}
                append_event(action="web_login", actor=user)
                return self._json({"ok": True, "user": user,
                                   "expires_in": session_ttl}, session_cookie=sid)
            if path == "/api/auth/logout":
                cookie = self.headers.get("Cookie", "")
                sid = next((p.strip()[14:] for p in cookie.split(";")
                            if p.strip().startswith("phase_session=")), "")
                with _SESSION_LOCK:
                    _SESSIONS.pop(sid, None)
                return self._json({"ok": True})
            if not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            if path == "/api/plan":
                state = self._read_state()
                if state is None:
                    return
                return self._json(_plan_response(state, cfg))
            if path == "/api/save":
                state = self._read_state()
                if state is None:
                    return
                try:
                    p = state_to_plan(state, cfg)
                    return self._json({"ok": True, "path": save_plan(p)})
                except Exception as e:  # noqa: BLE001
                    return self._json({"ok": False, "error": str(e)}, 400)
            if path == "/api/create":
                state = self._read_state()
                if state is None:
                    return
                try:
                    plan = state_to_plan(state, cfg)
                    vmid = realize_plan(cfg, qm, plan)
                    _cache_drop()
                    return self._json({"ok": True, "vmid": vmid,
                                       "name": plan["name"]})
                except Exception as e:  # noqa: BLE001
                    return self._json({"ok": False, "error": str(e)}, 400)
            if path == "/api/provision":
                state = self._read_state()
                if state is None:
                    return
                _cache_drop()
                return self._task_json(provision_plan, cfg, qm,
                                       state_to_plan(state, cfg))
            if path == "/api/settings":
                try:
                    result = _save_settings(cfg, self._read_body().get("settings", {}))
                    if result.get("new_token"):
                        token_box["value"] = result["new_token"]
                    _cache_drop()
                    return self._json({"ok": True, "settings": result})
                except Exception as e:  # noqa: BLE001
                    return self._json({"ok": False, "error": str(e)}, 400)

            if path.startswith("/api/vms/") and path.endswith("/power"):
                name = path[len("/api/vms/"):-len("/power")]
                vmid = find_vmid(qm, name)
                body = self._read_body() or {}
                action = body.get("action", "")
                mapping = {"start": "start", "stop": "stop", "reboot": "reboot",
                           "shutdown": "shutdown", "pause": "suspend",
                           "resume": "start"}
                if action not in mapping:
                    return self._json({"error": f"bad action: {action}"}, 400)
                _cache_drop()
                return self._task_json(getattr(qm, mapping[action]), vmid)
            if path.startswith("/api/vms/") and path.endswith("/edit"):
                name = path[len("/api/vms/"):-len("/edit")]
                vmid = find_vmid(qm, name)
                body = self._read_body() or {}
                opts = {}
                for k in ("cores", "memory", "description", "onboot",
                          "protection"):
                    if body.get(k) is not None and body.get(k) != "":
                        opts[k] = body[k]
                if "tags" in body and body["tags"] is not None:
                    tags = body["tags"]
                    opts["tags"] = ";".join(tags) if isinstance(tags, list) \
                        else str(tags)
                if not opts:
                    return self._json({"error": "nothing to change"}, 400)
                _cache_drop()
                return self._task_json(qm.set, vmid, **opts)
            if path.startswith("/api/vms/") and path.endswith("/disks/add"):
                name = path[len("/api/vms/"):-len("/disks/add")]
                vmid = find_vmid(qm, name)
                body = self._read_body() or {}
                disk_id, size, storage = body.get("id"), body.get("size"), \
                    body.get("storage")
                if not disk_id or not size or not storage:
                    return self._json(
                        {"error": "id, size and storage are required"}, 400)
                _cache_drop()
                return self._task_json(qm.set, vmid, **{disk_id: f"{storage}:{size}"})
            if path.startswith("/api/vms/") and path.endswith("/disks/resize"):
                name = path[len("/api/vms/"):-len("/disks/resize")]
                vmid = find_vmid(qm, name)
                body = self._read_body() or {}
                if not body.get("id") or not body.get("size"):
                    return self._json({"error": "id and size are required"}, 400)
                _cache_drop()
                return self._task_json(qm.resize, vmid, body["id"], body["size"])
            if path.startswith("/api/vms/") and path.endswith("/firewall"):
                name = path[len("/api/vms/"):-len("/firewall")]
                body = self._read_body() or {}
                return self._task_json(
                    _firewall_add, cfg, qm, name,
                    body.get("from", ""), body.get("port", ""),
                    body.get("proto", "tcp"))
            if path.startswith("/api/vms/") and path.endswith("/snapshot"):
                name = path[len("/api/vms/"):-len("/snapshot")]
                vmid = find_vmid(qm, name)
                body = self._read_body() or {}
                snap = body.get("name") or f"web-{uuid.uuid4().hex[:8]}"
                _cache_drop()
                return self._task_json(qm.snapshot, vmid, snap)
            if path.startswith("/api/vms/") and path.endswith("/backup"):
                name = path[len("/api/vms/"):-len("/backup")]
                _cache_drop()
                return self._task_json(_vm_backup, cfg, qm, name)
            if path.startswith("/api/vms/") and path.endswith("/destroy"):
                name = path[len("/api/vms/"):-len("/destroy")]
                _cache_drop()
                return self._task_json(_vm_destroy, cfg, qm, name)
            self._json({"error": "not found"}, 404)

        # ---- body / terminal ---------------------------------------------

        def _read_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return {}

        def _ws_terminal(self, name: str | None = None, argv: list[str] | None = None):
            """WebSocket terminal: upgrade, spawn ssh (or test cmd), bridge."""
            key = self.headers.get("Sec-WebSocket-Key")
            if not key:
                return self._json({"error": "not a websocket"}, 400)
            try:
                test_cmd = os.environ.get("PHASE_WEB_TEST_SSH_CMD", "")
                if test_cmd:
                    argv = [test_cmd]
                    keyfile = None
                elif argv is None:
                    assert name is not None
                    user, ip, vmid = vm_ssh_user_ip(qm, cfg, name)
                    argv, keyfile = ssh_argv(cfg, qm, user, ip, vmid,
                                             quiet=True)
                    argv = ["ssh", "-tt"] + argv
                else:
                    keyfile = None
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e)}, 400)

            self.send_response_only(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", _ws_accept(key))
            self.end_headers()
            self.close_connection = True
            sock = self.connection
            sock.settimeout(None)

            master, slave = pty_mod.openpty()
            proc = subprocess.Popen(
                argv, stdin=slave, stdout=slave, stderr=slave,
                close_fds=True)
            os.close(slave)
            dead = threading.Event()

            def pump_out():
                try:
                    while not dead.is_set():
                        r, _, _ = select.select([master], [], [], 0.5)
                        if not r:
                            continue
                        try:
                            data = os.read(master, 65536)
                        except OSError:
                            break
                        if not data:
                            break
                        _ws_send(sock, data)
                except Exception:
                    pass
                finally:
                    dead.set()
                    try:
                        proc.kill()
                    except Exception:
                        pass

            def pump_in():
                try:
                    while not dead.is_set():
                        msg = _ws_read_message(sock)
                        if msg is None:
                            break
                        _op, payload = msg
                        try:
                            os.write(master, payload)
                        except OSError:
                            break
                except Exception:
                    pass
                finally:
                    dead.set()
                    try:
                        proc.kill()
                    except Exception:
                        pass

            t_out = threading.Thread(target=pump_out, daemon=True)
            t_in = threading.Thread(target=pump_in, daemon=True)
            t_out.start()
            t_in.start()
            t_in.join()
            dead.set()
            t_out.join(timeout=2)
            try:
                os.close(master)
            except OSError:
                pass
            if keyfile:
                for f in (keyfile, keyfile + ".pub"):
                    try:
                        os.unlink(f)
                    except OSError:
                        pass
            try:
                sock.close()
            except OSError:
                pass

    return Handler


def run(cfg, qm, listen: str = "127.0.0.1", port: int = 8080,
        token: str = "") -> int:
    if not token:
        token = _dget(cfg, "web.token") or ""
    handler = make_handler(cfg, qm, token)
    try:
        httpd = ThreadingHTTPServer((listen, port), handler)
    except OSError as e:
        die(f"cannot bind {listen}:{port}: {e}")
    url = f"http://{listen}:{port}"
    log("phase web — VM console")
    log(f"  open:  {url}")
    if token:
        log("  auth:  X-Phase-Token required (set via --token *** config web.token)")
    if listen == "127.0.0.1":
        log("  from another machine, tunnel:")
        log("    ssh -L 8080:localhost:8080 root@<this-host>  →  http://localhost:8080")
        log("  or expose on the LAN with:  phase web --listen 0.0.0.0")
    log("  ctrl-c to stop")

    def _warm():
        # qm calls are slow (~0.5-0.7s each); pre-build the caches so the
        # first visitor doesn't sit on a 7-10s blank page after a restart.
        try:
            _cached_meta(cfg, qm)
            _cached_vms(qm, cfg)
        except Exception:  # noqa: BLE001 — warm-up must never kill the server
            pass

    threading.Thread(target=_warm, daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0

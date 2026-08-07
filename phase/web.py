"""phase web — GCE-style Create VM page served locally. No deps beyond stdlib.

`phase web [--listen 127.0.0.1] [--port 8080]` serves a single-page UI
(phase/webui.html) plus a small JSON API. It reuses the same plan-building
and validation code as the CLI/inline wizard, so the web form can never
drift from what `phase vm create` actually does.

Endpoints:
  GET  /                  -> the page
  GET  /api/meta          -> sizes, templates, networks, keys, plans
  GET  /api/plans/<name>  -> full saved plan (load into the form)
  GET  /api/task/<id>     -> provision task status (polled by the page)
  POST /api/plan          -> validate a wizard state, return plan + errors
  POST /api/save          -> save a plan to the library
  POST /api/create        -> create the VM (stopped)
  POST /api/provision     -> create + start + bootstrap (async task)
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from .log import append_event
from .plan import list_plans, read_plan, realize_plan, save_plan
from .provision import provision_plan
from .util import die, log
from .vm import list_template_oses, next_vmid
from .wizard import validate_step
from .inline_wizard import _dget, _summary_lines, _wizard_state, state_to_plan

WEBUI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui.html")

_TASKS: dict[str, dict] = {}


def _meta(cfg, qm) -> dict:
    return {
        "host": os.uname().nodename,
        "sizes": _dget(cfg, "templates.sizes") or {},
        "oses": [o for o in list_template_oses(cfg, qm) if o != "none"],
        "networks": list((_dget(cfg, "networks") or {}).keys()),
        "ssh_keys": list(_dget(cfg, "vm.ssh_keys") or []),
        "plans": list_plans(),
        "next_vmid": next_vmid(qm),
    }


def _plan_response(state: dict, cfg) -> dict:
    """Validate a wizard state; return plan + per-step errors."""
    errs = {}
    for i in range(1, 6):
        e = validate_step(_wizard_state(state), i, cfg)
        if e:
            errs[str(i)] = e
    if errs:
        return {"ok": False, "errors": errs}
    plan = state_to_plan(state, cfg)
    return {
        "ok": True,
        "plan": plan,
        "plan_text": "\n".join(_summary_lines(state)),
    }


def _start_provision(cfg, qm, state: dict) -> str:
    tid = uuid.uuid4().hex[:12]
    _TASKS[tid] = {"status": "running"}

    def worker():
        try:
            vmid = provision_plan(cfg, qm, state_to_plan(state, cfg))
            _TASKS[tid] = {"status": "done", "vmid": vmid}
        except Exception as e:  # noqa: BLE001 — report to the UI
            _TASKS[tid] = {"status": "error", "error": str(e)}

    threading.Thread(target=worker, daemon=True).start()
    return tid


def make_handler(cfg, qm, token: str = ""):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # ---- helpers ----------------------------------------------------

        def _authed(self):
            """Token gate: when a token is configured, /api/* needs it."""
            if not token:
                return True
            return self.headers.get("X-Phase-Token") == token

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _html(self, path, code=200):
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                self._json({"error": f"missing file: {path}"}, 500)
                return
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
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

        def log_message(self, fmt, *args):  # quieter access log
            log(f"web: {self.address_string()} {fmt % args}")

        # ---- GET ----------------------------------------------------------

        def do_GET(self):
            path = unquote(self.path.split("?")[0])
            if path in ("/", "/index.html"):
                return self._html(WEBUI)
            if not self._authed():
                return self._json({"error": "unauthorized — set X-Phase-Token"}, 401)
            if path == "/api/meta":
                return self._json(_meta(cfg, qm))
            if path.startswith("/api/plans/"):
                name = path[len("/api/plans/"):]
                try:
                    return self._json(read_plan(name))
                except Exception as e:  # noqa: BLE001
                    return self._json({"error": str(e)}, 404)
            if path.startswith("/api/task/"):
                tid = path[len("/api/task/"):]
                return self._json(_TASKS.get(tid, {"status": "unknown"}))
            self._json({"error": "not found"}, 404)

        # ---- POST ---------------------------------------------------------

        def do_POST(self):
            path = unquote(self.path.split("?")[0])
            if not self._authed():
                return self._json({"error": "unauthorized — set X-Phase-Token"}, 401)
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
                    path = save_plan(p)
                    return self._json({"ok": True, "path": path})
                except Exception as e:  # noqa: BLE001
                    return self._json({"ok": False, "error": str(e)}, 400)
            if path == "/api/create":
                state = self._read_state()
                if state is None:
                    return
                try:
                    plan = state_to_plan(state, cfg)
                    vmid = realize_plan(cfg, qm, plan)
                    return self._json({"ok": True, "vmid": vmid,
                                       "name": plan["name"]})
                except Exception as e:  # noqa: BLE001
                    return self._json({"ok": False, "error": str(e)}, 400)
            if path == "/api/provision":
                state = self._read_state()
                if state is None:
                    return
                tid = _start_provision(cfg, qm, state)
                return self._json({"ok": True, "task": tid})
            self._json({"error": "not found"}, 404)

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
    log("phase web — GCE-style create VM")
    log(f"  open:  {url}")
    if token:
        log("  auth:  X-Phase-Token required (set via --token or config web.token)")
    if listen == "127.0.0.1":
        log("  from another machine, tunnel:")
        log("    ssh -L 8080:localhost:8080 root@<this-host>  →  http://localhost:8080")
        log("  or expose on the LAN with:  phase web --listen 0.0.0.0")
    log("  ctrl-c to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0

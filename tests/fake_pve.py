#!/usr/bin/env python3
"""Fake PVE API server for tests — same state files as fakebin/qm.

Serves the PVE HTTP API surface that phase/pveapi.py uses, backed by the
FAKE_QM_DIR state (VMID.conf, VMID.power, snap-*.json). Token check via
FAKE_PVE_TOKEN env (any non-empty value matches, mirroring fake auth).
"""

import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl

STATE = os.environ.get("FAKE_QM_DIR")
TOKEN = os.environ.get("FAKE_PVE_TOKEN", "test-token")
if not STATE:
    print("fake pve needs FAKE_QM_DIR", file=sys.stderr)
    sys.exit(2)


def conf_path(vmid):
    return os.path.join(STATE, f"{vmid}.conf")


def power_path(vmid):
    return os.path.join(STATE, f"{vmid}.power")


def read_conf(vmid):
    p = conf_path(vmid)
    if not os.path.isfile(p):
        return None
    conf = {}
    for line in open(p):
        if ":" in line:
            k, v = line.split(":", 1)
            conf[k.strip()] = v.strip()
    return conf


def write_conf(vmid, conf):
    with open(conf_path(vmid), "w") as f:
        for k, v in conf.items():
            f.write(f"{k}: {v}\n")


def get_power(vmid):
    p = power_path(vmid)
    return open(p).read().strip() if os.path.isfile(p) else "stopped"


def set_power(vmid, s):
    with open(power_path(vmid), "w") as f:
        f.write(s + "\n")


def vmids():
    out = []
    for fn in sorted(os.listdir(STATE)):
        if fn.endswith(".conf"):
            out.append(fn[:-5])
    return out


def vm_resources():
    rows = []
    for vmid in vmids():
        conf = read_conf(vmid)
        if conf is None:
            continue
        rows.append({
            "vmid": int(vmid),
            "name": conf.get("name", f"vm-{vmid}"),
            "status": get_power(vmid),
            "mem": int(conf.get("memory", "0")),
            "maxmem": int(conf.get("memory", "0")),
            "disk": 0,
            "maxdisk": 0,
            "pid": 0 if get_power(vmid) != "running" else 1000 + int(vmid),
            "template": 1 if conf.get("template") == "1" else 0,
            "node": "fake",
        })
    return rows


def api_config(vmid):
    """API config mirrors the CLI conf but with typed cores/memory."""
    conf = read_conf(vmid)
    if conf is None:
        return None
    out = {}
    for k, v in conf.items():
        if k in ("cores", "memory"):
            out[k] = int(v)
        else:
            out[k] = v
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _auth(self):
        return self.headers.get("Authorization") == f"PVEAPIToken={TOKEN}"

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _node(self):
        return "fake"

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n).decode()
        try:
            return dict(parse_qsl(raw))
        except Exception:  # noqa: BLE001
            return {}

    # ---- routing -----------------------------------------------------

    def do_GET(self):
        if not self._auth():
            return self._send({"data": None, "errors": ["unauthorized"]}, 401)
        path = self.path.split("?")[0]
        try:
            if path == "/api2/json/nodes":
                return self._send({"data": [{"node": "fake"}]})
            if path == "/api2/json/cluster/nextid":
                used = {int(v) for v in vmids()}
                nid = 100
                while nid in used:
                    nid += 1
                return self._send({"data": nid})
            if path == "/api2/json/cluster/resources":
                return self._send({"data": vm_resources()})
            if path == "/api2/json/nodes/fake/status":
                return self._send({"data": {"pveversion": "fake 9.2.9",
                                            "kversion": "fake", "uptime": 1,
                                            "cpu": 0.1, "loadavg": [0, 0, 0],
                                            "memory": {"total": 8192, "used": 1}}})
            if path == "/api2/json/nodes/fake/storage":
                return self._send({"data": [{"storage": "local",
                                             "type": "dir", "status": "available",
                                             "total": 10 ** 11, "used": 10 ** 9,
                                             "avail": 99 * 10 ** 9}]})
            m = re.fullmatch(r"/api2/json/nodes/fake/qemu/(\d+)/config", path)
            if m:
                conf = api_config(m.group(1))
                if conf is None:
                    return self._send({"data": None, "errors": ["no such VM"]}, 404)
                return self._send({"data": conf})
            m = re.fullmatch(r"/api2/json/nodes/fake/qemu/(\d+)/status/current", path)
            if m:
                return self._send({"data": {"status": get_power(m.group(1))}})
            m = re.fullmatch(r"/api2/json/nodes/fake/qemu/(\d+)/snapshot", path)
            if m:
                vmid = m.group(1)
                snaps = []
                for fn in sorted(os.listdir(STATE)):
                    if fn.startswith("snap-") and fn.endswith(".json"):
                        data = json.load(open(os.path.join(STATE, fn)))
                        if str(data["vmid"]) == vmid:
                            snaps.append({"name": data["name"],
                                          "description": "", "vmstate": 1})
                return self._send({"data": snaps})
            m = re.fullmatch(
                r"/api2/json/nodes/fake/qemu/(\d+)/agent/exec-status.*", path)
            if m:
                return self._send({"data": {"exited": 1, "exitcode": 0,
                                            "out-data": "", "err-data": ""}})
            m = re.fullmatch(r"/api2/json/nodes/fake/tasks/([^/]+)/status", path)
            if m:
                return self._send({"data": {"status": "stopped"}})
            m = re.fullmatch(r"/api2/json/nodes/fake/tasks/([^/]+)/log.*", path)
            if m:
                return self._send({"data": [{"n": "INFO: Starting backup"},
                                            {"n": "INFO: Finished backup successfully"}]})
            return self._send({"data": None, "errors": [f"no GET route: {path}"]}, 404)
        except Exception as e:  # noqa: BLE001
            return self._send({"data": None, "errors": [str(e)]}, 500)

    def do_POST(self):
        if not self._auth():
            return self._send({"data": None, "errors": ["unauthorized"]}, 401)
        path = self.path.split("?")[0]
        body = self._read_body()
        try:
            m = re.fullmatch(r"/api2/json/nodes/fake/qemu/(\d+)/clone", path)
            if m:
                newid = body.get("newid", "999")
                src_conf = dict(read_conf(m.group(1)) or {})
                src_conf.pop("template", None)
                if body.get("name"):
                    src_conf["name"] = body["name"]
                write_conf(newid, src_conf)
                set_power(newid, "stopped")
                return self._send({"data": None})
            m = re.fullmatch(r"/api2/json/nodes/fake/qemu/(\d+)/status/(\w+)", path)
            if m:
                vmid, action = m.group(1), m.group(2)
                if action in ("start", "resume"):
                    set_power(vmid, "running")
                elif action in ("stop", "shutdown"):
                    set_power(vmid, "stopped")
                elif action == "reboot":
                    set_power(vmid, "running")
                elif action == "suspend":
                    set_power(vmid, "paused")
                return self._send({"data": None})
            m = re.fullmatch(r"/api2/json/nodes/fake/qemu/(\d+)/template", path)
            if m:
                conf = read_conf(m.group(1)) or {}
                conf["template"] = "1"
                write_conf(m.group(1), conf)
                return self._send({"data": None})
            m = re.fullmatch(r"/api2/json/nodes/fake/qemu/(\d+)/snapshot", path)
            if m:
                name = body.get("snapname") or "snap"
                with open(os.path.join(STATE, f"snap-{name}.json"), "w") as f:
                    json.dump({"vmid": int(m.group(1)), "name": name}, f)
                return self._send({"data": None})
            m = re.fullmatch(
                r"/api2/json/nodes/fake/qemu/(\d+)/snapshot/([^/]+)/rollback", path)
            if m:
                return self._send({"data": None})
            m = re.fullmatch(r"/api2/json/nodes/fake/qemu/(\d+)/agent/ping", path)
            if m:
                if get_power(m.group(1)) != "running":
                    return self._send({"data": None,
                                       "errors": ["agent not running"]}, 500)
                return self._send({"data": None})
            m = re.fullmatch(r"/api2/json/nodes/fake/qemu/(\d+)/agent/exec", path)
            if m:
                vmid = m.group(1)
                cmd = json.loads(body.get("command", "[]"))
                if cmd and cmd[0] == "network-get-interfaces":
                    ip = f"10.10.1.{int(vmid) - 100}"
                    out = json.dumps([
                        {"hardware-address": "00:00:00:00:00:00", "name": "lo",
                         "ip-addresses": [{"ip-address": "127.0.0.1",
                                           "ip-address-type": "ipv4", "prefix": 8}]},
                        {"hardware-address": "bc:24:11:00:00:00", "name": "eth0",
                         "ip-addresses": [{"ip-address": ip, "ip-address-type": "ipv4",
                                           "prefix": 24}]},
                    ])
                    return self._send({"data": {"exited": 1, "exitcode": 0,
                                                "out-data": out, "err-data": ""}})
                if "input-data" in body:
                    cap = os.environ.get("FAKE_QM_CAPTURE")
                    if cap:
                        with open(os.path.join(STATE, "exec-stdin.log"), "a") as f:
                            f.write(f"{vmid}: {body['input-data'][:200]}\n")
                return self._send({"data": {"exited": 1, "exitcode": 0,
                                            "out-data": "", "err-data": ""}})
            m = re.fullmatch(r"/api2/json/nodes/fake/vzdump", path)
            if m:
                return self._send({"data": "UPID:fake:00000000:00000000:00000000:0:vzdump:100:"})
            return self._send({"data": None, "errors": [f"no POST route: {path}"]}, 404)
        except Exception as e:  # noqa: BLE001
            return self._send({"data": None, "errors": [str(e)]}, 500)

    def do_PUT(self):
        if not self._auth():
            return self._send({"data": None, "errors": ["unauthorized"]}, 401)
        path = self.path.split("?")[0]
        body = self._read_body()
        try:
            m = re.fullmatch(r"/api2/json/nodes/fake/qemu/(\d+)/config", path)
            if m:
                conf = read_conf(m.group(1)) or {}
                for k, v in body.items():
                    if k == "delete":
                        for dk in v.split(","):
                            conf.pop(dk, None)
                        continue
                    # PVE-style disk entries
                    if re.fullmatch(r"(scsi|sata|virtio|ide)\d+", k):
                        mm = re.fullmatch(r"([^:]+):(\d+[GMKT])", v)
                        if mm:
                            v = f"{mm.group(1)}:vm-{m.group(1)}-{k},size={mm.group(2)}"
                    conf[k] = v
                write_conf(m.group(1), conf)
                return self._send({"data": None})
            m = re.fullmatch(r"/api2/json/nodes/fake/qemu/(\d+)/resize", path)
            if m:
                vmid, disk = m.group(1), body.get("disk", "")
                conf = read_conf(vmid) or {}
                cur = conf.get(disk, "")
                mm = re.search(r"size=[\d.]+[GMKT]", cur)
                if mm and body.get("size"):
                    conf[disk] = cur.replace(mm.group(0), f"size={body['size']}")
                write_conf(vmid, conf)
                return self._send({"data": None})
            return self._send({"data": None, "errors": [f"no PUT route: {path}"]}, 404)
        except Exception as e:  # noqa: BLE001
            return self._send({"data": None, "errors": [str(e)]}, 500)

    def do_DELETE(self):
        if not self._auth():
            return self._send({"data": None, "errors": ["unauthorized"]}, 401)
        path = self.path.split("?")[0]
        try:
            m = re.fullmatch(r"/api2/json/nodes/fake/qemu/(\d+)", path)
            if m:
                vmid = m.group(1)
                for p in (conf_path(vmid), power_path(vmid)):
                    if os.path.isfile(p):
                        os.unlink(p)
                return self._send({"data": None})
            return self._send({"data": None, "errors": [f"no DELETE route: {path}"]}, 404)
        except Exception as e:  # noqa: BLE001
            return self._send({"data": None, "errors": [str(e)]}, 500)


def serve(port: int = 8943) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8943
    print(f"fake pve api on 127.0.0.1:{port}")
    serve(port)
    threading.Event().wait()

#!/usr/bin/env python3
"""Smoke tests for the phase v4 python rewrite, against a fake qm.

Run:  .venv/bin/python tests/run_tests.py        (includes TUI headless test)
      python3 tests/run_tests.py                  (skips TUI test)
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKEBIN = os.path.join(ROOT, "tests", "fakebin")
FIXTURE_CONFIG = os.path.join(ROOT, "tests", "fixtures", "phase.json")
BIN = os.path.join(ROOT, "bin", "phase")

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}" + (f" — {detail}" if detail else ""))


def phase(tmp, *args, expect=0):
    env = _env(tmp)
    p = subprocess.run([sys.executable, BIN, *args], capture_output=True,
                       text=True, env=env, cwd=ROOT)
    if p.returncode != expect:
        raise AssertionError(
            f"phase {' '.join(args)} rc={p.returncode} (want {expect})\n"
            f"stdout: {p.stdout[-2000:]}\nstderr: {p.stderr[-2000:]}")
    return p.stdout, p.stderr


def _env(tmp):
    env = os.environ.copy()
    env["PATH"] = FAKEBIN + os.pathsep + env.get("PATH", "")
    env["PHASE_CONFIG"] = FIXTURE_CONFIG
    env["PHASE_PLAN_DIR"] = os.path.join(tmp, "plans")
    env["PHASE_TEMPLATE_DIR"] = os.path.join(tmp, "templates")
    env["PHASE_STATE_DIR"] = os.path.join(tmp, "state")
    env["FAKE_QM_DIR"] = os.path.join(tmp, "fakeq")
    env["PHASE_ENGINE_SOCKET"] = os.path.join(tmp, "engine.sock")
    return env


def fresh_tmp():
    tmp = tempfile.mkdtemp(prefix="phase-test-")
    subprocess.run([sys.executable, os.path.join(ROOT, "tests", "seed.py"),
                    os.path.join(tmp, "fakeq")], check=True)
    return tmp


def test_core():
    print("core commands (fake qm)")
    tmp = fresh_tmp()

    out, _ = phase(tmp, "vm", "list")
    check("vm list shows hermes", "hermes" in out and "gateway" in out)

    out, _ = phase(tmp, "vm", "status", "hermes")
    check("vm status hermes", "State:  running" in out and "VMID:   103" in out)

    out, _ = phase(tmp, "--json", "vm", "status", "hermes")
    check("vm status --json", '"state": "running"' in out and '"vmid": 103' in out)

    out, _ = phase(tmp, "templates")
    check("templates lists tpl-ubuntu-26", "tpl-ubuntu-26" in out)

    out, _ = phase(tmp, "vm", "nextid")
    check("nextid = 104", out.strip() == "104")

    out, _ = phase(tmp, "vm", "plan", "--save", "--name", "postgres",
                   "--size", "small", "--os", "ubuntu-26")
    check("plan --save prints Will create", "Will create" in out)

    out, _ = phase(tmp, "plan", "list")
    check("plan list shows postgres", "postgres" in out)

    out, _ = phase(tmp, "vm", "create", "postgres", "--vmidout")
    vmid = out.strip()
    check("create from saved plan (vmidout)", vmid == "104", f"got {vmid!r}")

    out, _ = phase(tmp, "vm", "status", "postgres")
    check("created vm exists", "VMID:   104" in out)

    out, _ = phase(tmp, "vm", "create", "db2", "--size", "micro", "--os", "ubuntu-26",
                   "--disk", "scsi0:os:ubuntu-26:20G",
                   "--disk", "scsi1:data::50G:local-lvm", "--tag", "db", "--vmidout")
    check("create with data disk", out.strip() == "105")
    out, _ = phase(tmp, "vm", "db2", "disk", "list")
    check("disk list shows scsi1 data", "scsi1" in out and "50G" in out)
    check("disk list marks os", "scsi0" in out and "os" in out)

    out, _ = phase(tmp, "vm", "db2", "tag", "add", "prod")
    check("tag add", ";".join(["db", "prod"]) in out)

    out, _ = phase(tmp, "vm", "db2", "rename", "db-prod")
    check("rename", "db2 -> db-prod" in out)

    out, _ = phase(tmp, "vm", "db-prod", "snapshot", "create", "snap1")
    check("snapshot create", "snap1" in out)

    out, _ = phase(tmp, "vm", "db-prod", "snapshot", "list")
    check("snapshot list", "snap1" in out)

    out, _ = phase(tmp, "vm", "db-prod", "ip")
    check("vm ip", out.strip().startswith("10.10.1."))

    out, _ = phase(tmp, "inventory", "--list")
    inv = json.loads(out)
    check("inventory groups", "tag:prod" in inv and "env:gateway" in inv)
    check("inventory hostvars", "hermes" in inv["_meta"]["hostvars"])
    check("inventory skips templates", "tpl-ubuntu-26" not in inv["all"]["hosts"])

    out, _ = phase(tmp, "inventory", "--host", "hermes")
    hv = json.loads(out)
    check("inventory host ansible_host", hv.get("ansible_host", "").startswith("10.10.1."))

    out, _ = phase(tmp, "extras")
    check("extras lists tui/notify", "tui" in out and "notify" in out and "report" in out)

    out, _ = phase(tmp, "engine", "scan")
    check("engine scan host", "pve-manager" in out)
    check("engine scan storage", "local-lvm" in out)
    check("engine scan mdev", "nvidia-223" in out)

    out, _ = phase(tmp, "--json", "engine", "scan")
    scan = json.loads(out)
    check("engine scan --json", scan["host"]["pveversion"].startswith("pve-manager"))
    check("engine scan gpu pci", "01:00.0" in scan["gpu"]["pci"])
    check("engine scan quotas", scan["quotas"]["cores_used"] >= 4)

    out, _ = phase(tmp, "engine", "doctor", expect=0)
    check("engine doctor passes", "all checks passed" in out)

    out, _ = phase(tmp, "vm", "db-prod", "backup")
    check("backup runs", "Backing up" in out)

    out, _ = phase(tmp, "vm", "gateway", "resize", "--cores", "4")
    check("resize cores", "resized" in out)
    out, _ = phase(tmp, "vm", "gateway", "status")
    check("resize applied", "Cores:  4" in out)

    shutil.rmtree(tmp)


def test_plan_crud():
    print("plan library")
    tmp = fresh_tmp()
    phase(tmp, "vm", "plan", "--save", "--name", "app1", "--size", "standard",
          "--os", "ubuntu-26")
    phase(tmp, "vm", "plan", "--save", "--name", "app2", "--size", "micro",
          "--os", "ubuntu-26", "--tag", "x")
    out, _ = phase(tmp, "plan", "list")
    check("plan list has both", "app1" in out and "app2" in out)
    out, _ = phase(tmp, "plan", "export", "app1")
    check("plan export json", '"name": "app1"' in out)
    phase(tmp, "plan", "rm", "app1")
    out, _ = phase(tmp, "plan", "list")
    check("plan rm", "app1" not in out)
    shutil.rmtree(tmp)


def test_template_pipeline():
    print("template pipeline")
    tmp = fresh_tmp()
    # create → awaiting-config with state file
    out, _ = phase(tmp, "template", "create", "--name", "tpl-ubuntu-26-docker",
                   "--from", "9001", "--os", "ubuntu-26", "--system")
    check("template create says awaiting", "awaiting configuration" in out)
    out, _ = phase(tmp, "template", "list")
    check("template list shows in-progress", "tpl-ubuntu-26-docker" in out)
    check("template list state", "awaiting-config" in out)
    out, _ = phase(tmp, "template", "show", "tpl-ubuntu-26-docker")
    check("template show", "awaiting-config" in out)

    # finish → minimized + templated
    out, _ = phase(tmp, "template", "finish", "tpl-ubuntu-26-docker")
    check("template finish ready", "is ready" in out)
    # verify minimize applied: check the new vmid's conf via qm config through info? use vm info
    out, _ = phase(tmp, "--json", "vm", "info", "tpl-ubuntu-26-docker")
    conf = json.loads(out)
    check("minimize cores=1", conf.get("cores") == "1")
    check("minimize memory=1024", conf.get("memory") == "1024")
    check("fstrim on clone", "fstrim_cloned_disks=1" in conf.get("agent", ""))
    check("templated", conf.get("template") == "1")

    # create another, abort it
    out, _ = phase(tmp, "template", "create", "--name", "tpl-ubuntu-26-abort",
                   "--from", "9001", "--os", "ubuntu-26")
    import re as _re
    vmid = _re.search(r"VMID (\d+)", out).group(1)
    out, _ = phase(tmp, "template", "abort", "tpl-ubuntu-26-abort", "--force")
    check("template abort destroyed", "destroyed" in out)
    out, _ = phase(tmp, "vm", "list")
    check("aborted vmid gone", f"tpl-ubuntu-26-abort" not in out)

    # finish on an unknown template → clean error
    out, err = phase(tmp, "template", "finish", "nope", expect=1)
    check("finish unknown dies", "no template in progress" in err)
    shutil.rmtree(tmp)


def test_inline_wizard():
    print("inline wizard (units + keys)")
    import json as _json
    import pty as _pty
    import threading
    import time
    from phase import inline_wizard as iw
    from phase.wizard import validate_step

    cfg = _json.load(open(FIXTURE_CONFIG))
    st = {
        "name": "pg2", "size": "small", "cores": "", "memory": "",
        "os": "ubuntu-26", "os_disk_size": "30G", "os_disk_storage": "",
        "data_disks": [{"id": "scsi1", "size": "50G", "storage": "nas"}],
        "bridge": "lan", "vlan": "", "ipmode": "static",
        "ip": "10.10.1.50/24", "gw": "10.10.1.1",
        "onboot": True, "protect": False, "tags": ["db"], "ssh_keys": [],
        "gpu": "", "bootstrap": {"system": True, "docker": False,
                                   "tailscale": False},
    }
    plan = iw.state_to_plan(st, cfg)
    check("state_to_plan name/size/os",
          plan["name"] == "pg2" and plan["size"] == "small"
          and plan["os"] == "ubuntu-26")
    check("state_to_plan cores from size", plan["cores"] == "2"
          and plan["memory"] == "2048")
    check("state_to_plan disks", len(plan["disks"]) == 2
          and plan["disks"][0]["role"] == "os" and plan["disks"][1]["role"] == "data")
    check("state_to_plan net/ip", plan["net"]["bridge"] == "lan"
          and plan["ipconfig"] == "10.10.1.50/24" and plan["gw"] == "10.10.1.1")
    check("state_to_plan bootstrap/tags", plan["bootstrap"]["system"]
          and plan["tags"] == ["db"] and plan["ssh_keys"] == [])
    st2 = dict(st, cores="4", memory="4096")
    p2 = iw.state_to_plan(st2, cfg)
    check("state_to_plan overrides", p2["cores"] == "4" and p2["memory"] == "4096")

    check("inline state passes wizard validation",
          validate_step(iw._wizard_state(st), 1, cfg) == [])
    bad = iw._wizard_state(dict(st, name="Bad Name"))
    check("inline state caught by wizard validation",
          any("name" in e for e in validate_step(bad, 1, cfg)))
    check("index_of choice", iw._index_of(["a", "b", "c"], "c", "size") == 2)
    check("index_of default", iw._index_of(["a", "b"], "", "size") == 0)
    check("index_of ssh none", iw._index_of(["(none)", "k"], [], "ssh_keys") == 0)

    # key parsing over a pty (canonical mode would buffer; use cbreak)
    master, slave = _pty.openpty()
    _tty = __import__("tty")
    _tty.setcbreak(slave)
    seen = {}

    def read_key(tag, timeout=3.0):
        seen[tag] = iw._read_key(slave, timeout=timeout)

    for tag, b in (("up", b"\x1b[A"), ("down", b"\x1b[B"),
                   ("enter", b"\r"), ("esc", b"\x1b"),
                   ("char", b"x")):
        t = threading.Thread(target=read_key, args=(tag,))
        t.start()
        time.sleep(0.15)
        os.write(master, b)
        t.join(timeout=3)
        check(f"key parse: {tag}", seen.get(tag) == tag.replace("char", "x") or
              seen.get(tag) == ("esc" if tag == "esc" else tag))
    import termios as _termios
    _termios.tcsetattr(slave, _termios.TCSADRAIN, _tty.tcgetattr(slave))
    os.close(master)
    os.close(slave)

    # CLI: bare create without a TTY still fails cleanly (no hang)
    tmp = fresh_tmp()
    p = subprocess.run([sys.executable, BIN, "vm", "create"], capture_output=True,
                       text=True, env=_env(tmp), cwd=ROOT)
    check("bare create non-tty errors", p.returncode != 0
          and "missing required field" in p.stderr)
    shutil.rmtree(tmp)


def test_web_api():
    print("web api (stdlib server)")
    import json as _json
    import threading as _threading
    import time as _time
    import urllib.request as _url
    from phase.config import Config
    from phase.qm import Qm
    from phase.transport import make_transport
    from phase.web import run

    tmp = fresh_tmp()
    os.environ.update({k: v for k, v in _env(tmp).items()})
    cfg = Config.load()
    qm = Qm(make_transport(None))
    port = 8931
    t = _threading.Thread(target=run, args=(cfg, qm),
                          kwargs={"listen": "127.0.0.1", "port": port},
                          daemon=True)
    t.start()
    _time.sleep(0.8)
    base = f"http://127.0.0.1:{port}"

    def get(p):
        return _json.loads(_url.urlopen(base + p).read())

    def post(p, body):
        req = _url.Request(base + p, data=_json.dumps(body).encode(),
                           headers={"Content-Type": "application/json"})
        return _json.loads(_url.urlopen(req).read())

    meta = get("/api/meta")
    check("web meta sizes+oses", "small" in meta["sizes"] and "ubuntu-26" in meta["oses"])
    page = _url.urlopen(base + "/").read().decode()
    check("web page served", "Machine configuration" in page and "Finalize" in page)

    state = {
        "name": "webvm1", "size": "small", "cores": "", "memory": "",
        "os": "ubuntu-26", "os_disk_size": "30G", "os_disk_storage": "",
        "data_disks": [{"id": "scsi1", "size": "50G", "storage": "nas"}],
        "bridge": "lan", "vlan": "", "ipmode": "dhcp", "ip": "", "gw": "",
        "tags": ["web"], "onboot": True, "protect": False, "gpu": "",
        "bootstrap": {"system": True, "docker": False, "tailscale": False},
        "ssh_keys": [], "description": "",
    }
    r = post("/api/plan", {"state": state})
    check("web plan ok", r["ok"] and "Size:" in r["plan_text"])
    r2 = post("/api/plan", {"state": dict(state, name="Bad Name!")})
    check("web bad name rejected", not r2["ok"] and r2["errors"]["1"])
    r3 = post("/api/save", {"state": state})
    check("web save plan", r3["ok"] and "webvm1.json" in r3["path"])
    r4 = post("/api/create", {"state": state})
    check("web create vm", r4["ok"] and r4["vmid"])
    full = get("/api/plans/webvm1")
    check("web load plan", full["name"] == "webvm1" and len(full["disks"]) == 2)

    # token gate
    port2 = 8932
    t2 = _threading.Thread(target=run, args=(cfg, qm),
                           kwargs={"listen": "127.0.0.1", "port": port2,
                                   "token": "sekrit"}, daemon=True)
    t2.start()
    _time.sleep(0.8)
    base2 = f"http://127.0.0.1:{port2}"
    import urllib.error as _urlerr
    try:
        _url.urlopen(base2 + "/api/meta").read()
        check("web token rejects anonymous", False)
    except _urlerr.HTTPError as e:
        check("web token rejects anonymous", e.code == 401)
    req = _url.Request(base2 + "/api/meta",
                       headers={"X-Phase-Token": "sekrit"})
    check("web token accepts valid", _json.loads(_url.urlopen(req).read())["oses"])
    shutil.rmtree(tmp)


def test_destroy_yes():
    print("destroy --yes")
    tmp = fresh_tmp()
    # non-tty without --yes: confirmation aborts (safe default)
    try:
        phase(tmp, "vm", "create", "tmp-gone", "--size", "micro", "--os", "ubuntu-26")
        p = subprocess.run([sys.executable, BIN, "vm", "tmp-gone", "destroy"],
                           capture_output=True, text=True, env=_env(tmp), cwd=ROOT)
        check("destroy without --yes aborts non-tty", p.returncode != 0
              and "aborting" in p.stderr)
        # --yes skips the typed confirmation
        out, _ = phase(tmp, "vm", "tmp-gone", "destroy", "--yes")
        check("destroy --yes works", "Destroyed tmp-gone" in out)
        out, _ = phase(tmp, "vm", "list")
        check("destroyed vm gone", "tmp-gone" not in out)
    finally:
        shutil.rmtree(tmp)


def test_dry_run():
    print("dry-run")
    tmp = fresh_tmp()
    out, _ = phase(tmp, "vm", "create", "dry1", "--size", "small", "--os", "ubuntu-26",
                   "--dry-run")
    check("dry-run prints DRY RUN", "DRY RUN: clone" in out)
    out, _ = phase(tmp, "vm", "list")
    check("dry-run creates nothing", "dry1" not in out)
    shutil.rmtree(tmp)


def test_util_units():
    print("util units")
    sys.path.insert(0, ROOT)
    from phase.util import ip_in_cidr, normalize_tags, parse_disk_spec, to_bytes
    check("to_bytes 32G", to_bytes("32G") == 32 * 1024**3)
    check("to_bytes 512M", to_bytes("512M") == 512 * 1024**2)
    check("ip_in_cidr", ip_in_cidr("10.10.1.5", "10.0.0.0/8"))
    check("ip not in cidr", not ip_in_cidr("192.168.5.1", "10.0.0.0/8"))
    check("normalize_tags dedupe", normalize_tags("a;b", "b", "c") == "a;b;c")
    d = parse_disk_spec("scsi1:data::50G:local-lvm")
    check("disk spec parse", d["role"] == "data" and d["size"] == "50G"
          and d["storage"] == "local-lvm")


def test_tui():
    print("TUI headless (textual)")
    try:
        import textual  # noqa
    except ImportError:
        print("  - textual not installed, skipping")
        return
    import asyncio
    from textual.widgets import Button, DataTable, ListView
    from phase.tui import PhaseApp

    tmp = fresh_tmp()
    env = _env(tmp)
    os.environ.update({k: v for k, v in env.items()})
    os.environ["PHASE_BIN"] = os.path.join(ROOT, "bin", "phase")

    async def drive():
        app = PhaseApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            check("tui opens on menu", app.screen.__class__.__name__ == "MenuScreen")
            await pilot.press("down", "down", "enter")  # Templates
            await pilot.pause()
            check("tui template screen", "TemplatesScreen" in app.screen.__class__.__name__)
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("up", "up", "enter")  # VMs (back to top of menu)
            await pilot.pause()
            check("tui vm screen", "VMsScreen" in app.screen.__class__.__name__)
            # wait for rows, open detail
            table = app.screen.query_one("#vms-table", DataTable)
            for _ in range(20):
                await pilot.pause()
                if table.row_count:
                    break
            check("vm table populated", table.row_count > 0)
            await pilot.press("enter")
            await pilot.pause()
            check("vm detail screen", "VMDetailScreen" in app.screen.__class__.__name__)
            detail = app.screen
            actions = detail.query_one(ListView)
            # stop (hard) -> confirm modal; Esc = No
            actions.index = 4
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            check("confirm modal on stop", "ConfirmScreen" in app.screen.__class__.__name__)
            await pilot.press("escape")
            await pilot.pause()
            check("confirm Esc cancels", "VMDetailScreen" in app.screen.__class__.__name__)
            # destroy -> type-the-name modal
            actions = detail.query_one(ListView)
            actions.index = 15
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            check("destroy modal", "DestroyScreen" in app.screen.__class__.__name__)
            ds = app.screen
            await pilot.press(*list("nope"))
            await pilot.pause()
            check("wrong name keeps destroy disabled",
                  ds.query_one("#go", Button).disabled)
            # clear and type the real name
            for _ in range(4):
                await pilot.press("backspace")
            name = detail.vm_name
            await pilot.press(*list(name))
            await pilot.pause()
            check("matching name enables destroy",
                  not ds.query_one("#go", Button).disabled)
            await pilot.press("escape")  # cancel the destroy
            await pilot.pause()

    asyncio.run(drive())
    shutil.rmtree(tmp)


def test_tui_create_start():
    print("tui opt-in: phase tui create")
    try:
        import textual  # noqa
    except ImportError:
        print("  - textual not installed, skipping")
        return
    import asyncio
    from phase.tui import PhaseApp
    from phase.wizard import CreateWizardScreen

    tmp = fresh_tmp()
    env = _env(tmp)
    os.environ.update({k: v for k, v in env.items()})
    os.environ["PHASE_BIN"] = os.path.join(ROOT, "bin", "phase")

    async def drive():
        app = PhaseApp(start="create")
        async with app.run_test() as pilot:
            await pilot.pause()
            check("tui create opens wizard directly",
                  isinstance(app.screen, CreateWizardScreen))

    asyncio.run(drive())
    shutil.rmtree(tmp)


def test_wizard_units():
    print("wizard pure units")
    from phase.wizard import plan_argv, validate_step, summary_line

    state = {
        "name": "postgres", "description": "", "tags": ["db", "prod"],
        "onboot": True, "protect": False, "size": "small", "cores": "",
        "memory": "", "gpu": "", "os": "ubuntu-26",
        "os_disk_size": "30G", "os_disk_storage": "",
        "data_disks": [{"id": "scsi1", "size": "50G", "storage": "nas"}],
        "bridge": "lan", "vlan": "", "ipmode": "static", "ip": "10.10.1.50/24",
        "gw": "10.10.1.1",
        "bootstrap": {"system": True, "docker": False, "tailscale": True},
        "ssh_keys": [],
    }
    argv = plan_argv(state, "create")
    check("plan_argv name/size/os",
          argv[:8] == ["vm", "create", "--name", "postgres", "--size", "small",
                       "--os", "ubuntu-26"])
    check("plan_argv os disk spec", "--disk" in argv and "scsi0:os:ubuntu-26:30G" in argv)
    check("plan_argv data disk", "scsi1:data:50G:nas" in argv)
    check("plan_argv net", "--bridge" in argv and "lan" in argv and "--ip" in argv
          and "10.10.1.50/24" in argv and "--gw" in argv)
    check("plan_argv bootstrap", "--system" in argv and "--tailscale" in argv
          and "--docker" not in argv)
    check("plan_argv tags", argv.count("--tag") == 2)
    check("plan_argv save flag", plan_argv(state, "plan")[-1] == "--save")

    over = dict(state, cores="4", memory="8192")
    a2 = plan_argv(over, "create")
    check("plan_argv overrides", "--cores" in a2 and "4" in a2 and "8192" in a2)

    check("validate good basics", validate_step(state, 1) == [])
    bad = dict(state, name="Bad Name!")
    check("validate bad name", any("name" in e for e in validate_step(bad, 1)))
    check("validate empty name", any("required" in e
                                      for e in validate_step(dict(state, name=""), 1)))
    bad_disk = dict(state, data_disks=[{"id": "nvme0", "size": "50G"}])
    check("validate bad disk id", any("disk" in e for e in validate_step(bad_disk, 3)))
    bad_ip = dict(state, ip="")
    check("validate static w/o ip", any("IP" in e for e in validate_step(bad_ip, 4)))
    dhcp = dict(state, ipmode="dhcp", ip="")
    check("validate dhcp ok", validate_step(dhcp, 4) == [])
    bad_cores = dict(state, cores="lots")
    check("validate bad cores", any("cores" in e for e in validate_step(bad_cores, 2)))

    sizes = {"small": {"cores": 2, "memory": 2048}}
    line = summary_line(state, sizes)
    check("summary has name+size+os", "postgres" in line and "small" in line
          and "ubuntu-26" in line and "2c/2048MB" in line)
    check("summary shows disks+net+ip", "2 disks" in line and "lan" in line
          and "10.10.1.50/24" in line)
    check("summary shows bootstrap", "system+tailscale" in line)


def test_wizard_tui():
    print("wizard headless (textual)")
    try:
        import textual  # noqa
    except ImportError:
        print("  - textual not installed, skipping")
        return
    import asyncio
    import os as _os
    from phase.tui import PhaseApp
    from phase.wizard import CreateWizardScreen

    tmp = fresh_tmp()
    env = _env(tmp)
    _os.environ.update({k: v for k, v in env.items()})
    _os.environ["PHASE_BIN"] = _os.path.join(ROOT, "bin", "phase")

    async def drive():
        app = PhaseApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("down", "enter")   # menu → Create VM
            await pilot.pause()
            wiz = app.screen
            check("wizard opens from menu", isinstance(wiz, CreateWizardScreen))
            check("step 1 shown by default",
                  wiz.query_one("#pane-basics").display
                  and not wiz.query_one("#pane-review").display)
            # empty name blocks Enter with an error
            await pilot.press("enter")
            await pilot.pause()
            check("empty name blocked", "name is required"
                  in str(wiz.query_one("#err-basics").content))
            # type a name, advance via Enter
            await pilot.press(*list("postgres"))
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            check("enter advances to resources", wiz.query_one("#pane-resources").display)
            # jump to disks, add a data disk
            await pilot.press("3")
            await pilot.pause()
            check("jump to disks", wiz.query_one("#pane-disks").display)
            await pilot.press("tab")  # os-disk-size → os-disk-storage
            await pilot.press("tab")  # → in-disk-id
            await pilot.press(*list("scsi1"))
            await pilot.press("tab")
            await pilot.press(*list("50G"))
            await pilot.press("tab")
            await pilot.press(*list("nas"))
            await pilot.press("tab")  # → disk-add button
            await pilot.press("enter")  # press it
            await pilot.pause()
            check("data disk added", len(wiz.data_disks) == 1
                  and wiz.data_disks[0]["id"] == "scsi1")
            # network + bootstrap + review
            await pilot.press("4")
            await pilot.pause()
            check("jump to network", wiz.query_one("#pane-network").display)
            await pilot.press("5")
            await pilot.pause()
            await pilot.press("tab")  # bs-system → bs-docker
            await pilot.press("space")  # toggle docker
            await pilot.pause()
            await pilot.press("6")
            await pilot.pause()
            check("review shown", wiz.query_one("#pane-review").display)
            summary = wiz.query_one("#wiz-summary").content
            check("summary live", "postgres" in str(summary) and "2 disks" in str(summary))
            cmd = str(wiz.query_one("#review-cmd").content)
            check("review shows exact command", "phase vm create --name postgres" in cmd
                  and "--size small" in cmd and "--os ubuntu-26" in cmd)
            # save the plan (s) → shell-out writes the plan file
            await pilot.press("s")
            for _ in range(10):
                await pilot.pause()
                if not wiz._busy:
                    break
            plan_file = _os.path.join(tmp, "plans", "postgres.json")
            check("s saves a plan", _os.path.isfile(plan_file))

    asyncio.run(drive())
    shutil.rmtree(tmp)


def test_engine_daemon():
    print("engine daemon (socket rpc)")
    tmp = fresh_tmp()
    import socket
    import time
    # start daemon with a job
    env = _env(tmp)
    env["PHASE_CONFIG"] = FIXTURE_CONFIG
    proc = subprocess.Popen(
        [sys.executable, BIN, "engine", "daemon"],
        env=env, cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        sock_path = os.path.join(tmp, "engine.sock")
        deadline = time.time() + 10
        while not os.path.exists(sock_path) and time.time() < deadline:
            time.sleep(0.2)
        check("daemon socket exists", os.path.exists(sock_path))
        if os.path.exists(sock_path):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(sock_path)
            s.sendall(b'{"cmd":"status"}\n')
            resp = json.loads(s.recv(4096))
            check("daemon rpc status", resp.get("ok") is True)
            s.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    shutil.rmtree(tmp)


def main():
    global failed
    tests = [test_util_units, test_core, test_plan_crud, test_template_pipeline,
             test_dry_run, test_destroy_yes, test_engine_daemon, test_tui,
             test_tui_create_start, test_wizard_units, test_wizard_tui,
             test_inline_wizard, test_web_api]
    for t in tests:
        try:
            t()
        except Exception as e:
            import traceback
            failed += 1
            print(f"  ✗ {t.__name__} raised: {e}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

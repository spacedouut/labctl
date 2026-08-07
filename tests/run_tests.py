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
             test_dry_run, test_engine_daemon, test_tui]
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

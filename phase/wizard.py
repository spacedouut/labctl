"""CreateVM wizard — a step-driven screen for planning/creating VMs.

Replaces the old flat CreateScreen. Layout:

    ┌ phase · Create VM wizard ─────────────────────────────────┐
    │ ┌─ steps ───────┐ ┌─ <current step pane> ───────────────┐ │
    │ │ 1 Basics   ✓  │ │  ... fields for the active step ... │ │
    │ │ 2 Resources →  │ │                                    │ │
    │ │ 3 Disks     ○  │ │                                    │ │
    │ │ 4 Network   ○  │ │                                    │ │
    │ │ 5 Bootstrap ○  │ │                                    │ │
    │ │ 6 Review    ○  │ │                                    │ │
    │ └───────────────┘ └─────────────────────────────────────┘ │
    │ ┌─ live summary ──────────────────────────────────────────┐│
    │ │ postgres · small (2c/2048MB) · ubuntu-26 · 1 disk · lan ││
    │ └─────────────────────────────────────────────────────────┘│
    └─────────────────────────────────────────────────────────────┘

Navigation: Tab/Shift+Tab move between fields, Enter advances the step
(validating it first), Esc steps back (or exits to the menu from step 1),
1-6 jump straight to a step, s saves the plan, c/p create / create+provision.

Design rule (same as the rest of the TUI): the wizard never invents its own
VM logic. Static data comes from the same Config the CLI reads; live data
(templates, plans) and all write actions shell out to `phase <subcommand>`
so the wizard and the scripting surface cannot drift apart.
"""

from __future__ import annotations

import re
import shlex

from .extras import MissingExtra

try:
    from textual.app import ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import Screen
    from textual.widgets import (Button, Checkbox, DataTable, Input, Label,
                                 RadioButton, RadioSet, RichLog, Select, Static)
except ImportError:
    ComposeResult = None  # type: ignore

    class CreateWizardScreen:  # placeholder when the [tui] extra is missing
        def __init__(self, *a, **kw):
            raise MissingExtra("tui")

    # Keep the pure wizard helpers available to the web UI and headless tests
    # when the optional Textual dependency is not installed.
    def plan_argv(state: dict, action: str) -> list[str]:
        a = ["vm", action, "--name", state["name"], "--size", state["size"],
             "--os", state["os"]]
        for key, flag in (("cores", "--cores"), ("memory", "--memory")):
            if state.get(key): a += [flag, str(state[key])]
        if state.get("description"): a += ["--description", state["description"]]
        if state.get("os_disk_size"):
            spec = f"scsi0:os:{state['os']}:{state['os_disk_size']}"
            if state.get("os_disk_storage"): spec += f":{state['os_disk_storage']}"
            a += ["--disk", spec]
        for d in state.get("data_disks", []):
            spec = f"{d['id']}:data:{d.get('size', '')}"
            if d.get("storage"): spec += f":{d['storage']}"
            a += ["--disk", spec]
        if state.get("bridge"): a += ["--bridge", state["bridge"]]
        if state.get("vlan"): a += ["--vlan", state["vlan"]]
        if state.get("ipmode") == "static" and state.get("ip"):
            a += ["--ip", state["ip"]]
            if state.get("gw"): a += ["--gw", state["gw"]]
        elif state.get("ip"): a += ["--ip", "dhcp"]
        if not state.get("onboot", True): a += ["--no-onboot"]
        if state.get("protect"): a += ["--protect"]
        for t in state.get("tags", []): a += ["--tag", t]
        for k in state.get("ssh_keys", []): a += ["--ssh-key", k]
        for flag in ("system", "docker", "tailscale"):
            if state.get("bootstrap", {}).get(flag): a += [f"--{flag}"]
        if state.get("gpu"): a += ["--gpu", state["gpu"]]
        if action == "plan": a += ["--save"]
        return a

    _DISK_ID_RE = re.compile(r"^(scsi|sata|virtio|ide)\d+$")
    _SIZE_RE = re.compile(r"^\d+(\.\d+)?[KMG]?$", re.IGNORECASE)

    def _looks_like_ip(s: str) -> bool:
        p = s.split(".")
        return len(p) == 4 and all(x.isdigit() and 0 <= int(x) <= 255 for x in p)

    def validate_step(state: dict, step: int, cfg=None) -> list[str]:
        errors = []
        if step == 1:
            name = state.get("name", "")
            if not name: errors.append("name is required")
            else:
                pattern = (cfg.get("naming.pattern") if cfg else None) or "^[a-z][a-z0-9-]*$"
                max_len = (cfg.get("naming.max_length") if cfg else None) or 63
                if not re.fullmatch(pattern, name): errors.append(f"name must match {pattern}")
                elif len(name) > max_len: errors.append(f"name too long (max {max_len})")
            for t in state.get("tags", []):
                if not t or any(c.isspace() for c in t): errors.append(f"invalid tag: {t!r}")
        elif step == 2:
            if not state.get("size"): errors.append("pick a size")
            for key in ("cores", "memory"):
                v = state.get(key)
                if v and (not str(v).isdigit() or int(v) <= 0): errors.append(f"{key} must be a positive integer")
        elif step == 3:
            for d in state.get("data_disks", []):
                if not _DISK_ID_RE.match(d.get("id", "")): errors.append(f"bad disk id: {d.get('id')!r} (want scsiN|sataN|virtioN|ideN)")
                if not _SIZE_RE.match(d.get("size", "")): errors.append(f"disk {d.get('id')}: bad size {d.get('size')!r} (e.g. 20G)")
            if state.get("os_disk_size") and not _SIZE_RE.match(state["os_disk_size"]): errors.append(f"boot disk: bad size {state['os_disk_size']!r} (e.g. 20G)")
        elif step == 4:
            if state.get("vlan") and not state["vlan"].isdigit(): errors.append("vlan must be a number")
            if state.get("ipmode") == "static":
                if not state.get("ip"): errors.append("static IP required (or switch to DHCP)")
                elif state["ip"].lower() == "dhcp": errors.append("'dhcp' is not a static IP")
                if state.get("gw") and not _looks_like_ip(state["gw"]): errors.append(f"gateway doesn't look like an IP: {state['gw']!r}")
        return errors

    def summary_line(state: dict, sizes: dict) -> str:
        size = state.get("size") or "?"
        cores = state.get("cores") or sizes.get(size, {}).get("cores", "")
        mem = state.get("memory") or sizes.get(size, {}).get("memory", "")
        resource = f"{size} ({cores}c/{mem}MB)" if cores and mem else size
        n = len(state.get("data_disks", [])) + 1
        net = state.get("bridge") or "default"
        if state.get("vlan"): net += f"/vlan{state['vlan']}"
        ip = state.get("ip") if state.get("ipmode") == "static" else "dhcp"
        bits = [state.get("name") or "(unnamed)", resource, state.get("os") or "os?",
                f"{n} disk{'s' if n != 1 else ''}", net, ip]
        bs = [k for k in ("system", "docker", "tailscale") if state.get("bootstrap", {}).get(k)]
        if bs: bits.append("+".join(bs))
        return " · ".join(bits)
else:

    STEP_NAMES = ["Basics", "Resources", "Disks", "Network", "Bootstrap", "Review"]

    # ------------------------------------------------------------------
    # pure helpers (unit-testable without a terminal)
    # ------------------------------------------------------------------

    def plan_argv(state: dict, action: str) -> list[str]:
        """Build the `phase vm <action> ...` argv for a wizard state.

        action: "plan" (saves), "create", "provision".
        """
        a = ["vm", action, "--name", state["name"], "--size", state["size"]]
        if state.get("cores"):
            a += ["--cores", str(state["cores"])]
        if state.get("memory"):
            a += ["--memory", str(state["memory"])]
        a += ["--os", state["os"]]
        os_size = state.get("os_disk_size") or ""
        os_storage = state.get("os_disk_storage") or ""
        if os_size:
            spec = f"scsi0:os:{state['os']}:{os_size}"
            if os_storage:
                spec += f":{os_storage}"
            a += ["--disk", spec]
        for d in state.get("data_disks", []):
            spec = f'{d["id"]}:data:{d.get("size", "")}'
            if d.get("storage"):
                spec += f":{d['storage']}"
            a += ["--disk", spec]
        if state.get("bridge"):
            a += ["--bridge", state["bridge"]]
        if state.get("vlan"):
            a += ["--vlan", state["vlan"]]
        ip = state.get("ip") or ""
        if state.get("ipmode") == "static" and ip:
            a += ["--ip", ip]
            if state.get("gw"):
                a += ["--gw", state["gw"]]
        elif ip:
            a += ["--ip", "dhcp"]
        if state.get("description"):
            a += ["--description", state["description"]]
        if not state.get("onboot", True):
            a += ["--no-onboot"]
        if state.get("protect"):
            a += ["--protect"]
        for t in state.get("tags", []):
            a += ["--tag", t]
        for k in state.get("ssh_keys", []):
            a += ["--ssh-key", k]
        for flag in ("system", "docker", "tailscale"):
            if state.get("bootstrap", {}).get(flag):
                a += [f"--{flag}"]
        if state.get("gpu"):
            a += ["--gpu", state["gpu"]]
        if action == "plan":
            a += ["--save"]
        return a


    _DISK_ID_RE = re.compile(r"^(scsi|sata|virtio|ide)\d+$")
    _SIZE_RE = re.compile(r"^\d+(\.\d+)?[KMG]?$", re.IGNORECASE)


    def validate_step(state: dict, step: int, cfg=None) -> list[str]:
        """Return a list of human-readable errors for one step ([] = ok)."""
        errors = []

        if step == 1:  # basics
            name = state.get("name", "")
            if not name:
                errors.append("name is required")
            else:
                pattern = (cfg.get("naming.pattern") if cfg else None) or "^[a-z][a-z0-9-]*$"
                max_len = (cfg.get("naming.max_length") if cfg else None) or 63
                if not re.fullmatch(pattern, name):
                    errors.append(f"name must match {pattern}")
                elif len(name) > max_len:
                    errors.append(f"name too long (max {max_len})")
            for t in state.get("tags", []):
                if not t or any(c.isspace() for c in t):
                    errors.append(f"invalid tag: {t!r}")

        elif step == 2:  # resources
            size = state.get("size", "")
            if not size:
                errors.append("pick a size")
            for key in ("cores", "memory"):
                v = state.get(key)
                if v and (not str(v).isdigit() or int(v) <= 0):
                    errors.append(f"{key} must be a positive integer")

        elif step == 3:  # disks
            for d in state.get("data_disks", []):
                if not _DISK_ID_RE.match(d.get("id", "")):
                    errors.append(f"bad disk id: {d.get('id')!r} (want scsiN|sataN|virtioN|ideN)")
                if not _SIZE_RE.match(d.get("size", "")):
                    errors.append(f"disk {d.get('id')}: bad size {d.get('size')!r} (e.g. 20G)")
            if state.get("os_disk_size") and not _SIZE_RE.match(state["os_disk_size"]):
                errors.append(f"boot disk: bad size {state['os_disk_size']!r} (e.g. 20G)")

        elif step == 4:  # network
            if state.get("vlan") and not state["vlan"].isdigit():
                errors.append("vlan must be a number")
            if state.get("ipmode") == "static":
                ip = state.get("ip") or ""
                if not ip:
                    errors.append("static IP required (or switch to DHCP)")
                elif ip.lower() == "dhcp":
                    errors.append("'dhcp' is not a static IP")
                gw = state.get("gw") or ""
                if gw and not _looks_like_ip(gw):
                    errors.append(f"gateway doesn't look like an IP: {gw!r}")

        # steps 5 (bootstrap) and 6 (review) validate their own children
        return errors


    def _looks_like_ip(s: str) -> bool:
        parts = s.split(".")
        return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


    def summary_line(state: dict, sizes: dict) -> str:
        """Compact one-line summary shown under the wizard."""
        bits = []
        bits.append(state.get("name") or "(unnamed)")
        size = state.get("size") or "?"
        cores = state.get("cores") or (sizes.get(size, {}).get("cores") if size in sizes else "")
        mem = state.get("memory") or (sizes.get(size, {}).get("memory") if size in sizes else "")
        if cores and mem:
            bits.append(f"{size} ({cores}c/{mem}MB)")
        else:
            bits.append(size)
        bits.append(state.get("os") or "os?")
        ndisks = len(state.get("data_disks", [])) + 1
        bits.append(f"{ndisks} disk{'s' if ndisks != 1 else ''}")
        net = state.get("bridge") or "default"
        if state.get("vlan"):
            net += f"/vlan{state['vlan']}"
        bits.append(net)
        ip = state.get("ip") if state.get("ipmode") == "static" else "dhcp"
        bits.append(ip)
        bs = [k for k in ("system", "docker", "tailscale") if state.get("bootstrap", {}).get(k)]
        if bs:
            bits.append("+".join(bs))
        return " · ".join(bits)


    # ------------------------------------------------------------------
    # the screen
    # ------------------------------------------------------------------

    class CreateWizardScreen(Screen):
        """Step-driven create VM wizard (sidebar + config pane + summary)."""

        TITLE = "Create VM"

        BINDINGS = [
            Binding("enter", "next_step", "next step"),
            Binding("escape", "prev_step", "back"),
            Binding("1", "goto_step(1)", "basics"),
            Binding("2", "goto_step(2)", "resources"),
            Binding("3", "goto_step(3)", "disks"),
            Binding("4", "goto_step(4)", "network"),
            Binding("5", "goto_step(5)", "bootstrap"),
            Binding("6", "goto_step(6)", "review"),
            Binding("s", "save_plan", "save plan"),
            Binding("c", "create_vm", "create"),
            Binding("p", "provision_vm", "provision"),
        ]

        CSS = """
        #wiz-sidebar {
            width: 34;
            height: 1fr;
            border-right: solid $primary;
            padding: 0 1;
        }
        #wiz-sidebar .wiz-sidebar-title {
            text-style: bold;
            color: $text;
            padding: 1 0 1 0;
        }
        #wiz-sidebar .step-btn {
            width: 100%;
            margin: 0 0 1 0;
            text-align: left;
        }
        #wiz-sidebar .step-btn.done { color: #4ec9a0; }
        #wiz-sidebar .step-btn.cur { text-style: bold; color: $text; }
        #wiz-sidebar .wiz-sidebar-hint {
            color: $text-muted;
            padding: 1 0 0 0;
        }
        #wiz-panes { height: 1fr; padding: 0 2; }
        .wiz-pane { height: auto; padding: 1 0; }
        .wiz-pane-title {
            text-style: bold;
            color: $accent;
            padding: 0 0 1 0;
        }
        .wiz-pane Label { margin-top: 1; color: $text-muted; }
        .wiz-pane Input, .wiz-pane Select { margin-bottom: 1; }
        .wiz-pane Checkbox { margin-bottom: 1; }
        .wiz-err { color: $error; margin-top: 1; }
        #wiz-summary {
            dock: bottom;
            height: 1;
            color: $text;
            background: $boost;
            padding: 0 2;
        }
        #review-plan, #review-cmd {
            margin: 1 0;
        }
        #review-cmd { color: $text-muted; }
        #review-log { height: 8; margin-top: 1; }
        """

        def __init__(self, cfg_path=None, host=None):
            super().__init__()
            self.cfg_path = cfg_path
            self.host = host
            self.cfg = None
            self.sizes: dict = {}
            self.oses: list[str] = []
            self.plans: list[dict] = []
            self.ssh_keys: list[str] = []
            self.step = 1
            self.data_disks: list[dict] = []      # {id, size, storage}
            self._loaded_plan: dict | None = None
            self._busy = False

        # -- state ------------------------------------------------------

        def _radio_value(self, rs: RadioSet, default: str = "") -> str:
            """Selected RadioButton's value, derived from its id (`size-small`)."""
            idx = rs._selected
            buttons = list(rs.query(RadioButton))
            if idx is not None and 0 <= idx < len(buttons):
                rb = buttons[idx]
                if rb.id and "-" in rb.id:
                    return rb.id.split("-", 1)[1]
            return default

        def _radio_select(self, rs: RadioSet, key: str) -> None:
            """Programmatically select the RadioButton whose id ends in `key`."""
            for i, rb in enumerate(rs.query(RadioButton)):
                if rb.id and rb.id.split("-", 1)[1] == key:
                    rs._selected = i
                    return

        def _state(self) -> dict:
            q = self.query_one
            def val(wid: str, cast=str, default=""):
                w = q(f"#{wid}")
                if w is None:
                    return default
                if isinstance(w, (Input, Select)):
                    v = w.value
                    if isinstance(w, Select) and v is Select.NULL:
                        v = default
                elif isinstance(w, Checkbox):
                    v = w.value
                elif isinstance(w, RadioSet):
                    v = self._radio_value(w, default)
                else:
                    v = default
                return cast(v) if v is not None else default
            return {
                "name": val("in-name").strip(),
                "description": val("in-desc").strip(),
                "tags": [t.strip() for t in val("in-tags").split(",") if t.strip()],
                "onboot": val("in-onboot", bool, True),
                "protect": val("in-protect", bool, False),
                "size": val("size-set", str, "small"),
                "cores": val("in-cores").strip(),
                "memory": val("in-memory").strip(),
                "gpu": val("in-gpu").strip(),
                "os": val("sel-os", str, ""),
                "os_disk_size": val("in-osdisk-size").strip(),
                "os_disk_storage": val("in-osdisk-storage").strip(),
                "data_disks": list(self.data_disks),
                "bridge": val("sel-bridge", str, ""),
                "vlan": val("in-vlan").strip(),
                "ipmode": val("ipmode-set", str, "dhcp"),
                "ip": val("in-ip").strip(),
                "gw": val("in-gw").strip(),
                "bootstrap": {
                    "system": val("bs-system", bool, False),
                    "docker": val("bs-docker", bool, False),
                    "tailscale": val("bs-tailscale", bool, False),
                },
                "ssh_keys": self._selected_ssh_keys(),
            }

        def _selected_ssh_keys(self) -> list[str]:
            v = self.query_one("#sel-sshkey", Select).value
            if isinstance(v, str) and v.startswith(("ssh-", "ecdsa-", "sk-",
                                                    "-----BEGIN")):
                return [v]
            return []

        # -- mount / data ------------------------------------------------

        async def on_mount(self) -> None:
            from .config import Config
            from .transport import make_qm
            from .util import PhaseError
            from .vm import list_template_oses

            self.cfg = Config.load_optional(self.cfg_path)
            self.qm = make_qm(self.cfg)
            self.sizes = dict(self.cfg.get("templates.sizes") or {}) if self.cfg else {}

            # sizes -> radio chips (resources step)
            size_set = self.query_one("#size-set", RadioSet)
            for s in sorted(self.sizes):
                c, m = self.sizes[s].get("cores", "?"), self.sizes[s].get("memory", "?")
                size_set.mount(RadioButton(f"{s}  —  {c} vCPU / {m} MB", id=f"size-{s}"))
            if "small" in self.sizes:
                self._radio_select(size_set, "small")
            elif self.sizes:
                size_set._selected = 0

            # networks -> bridge select
            networks = self.cfg.get("networks") or {}
            bridges = list(networks.keys())
            default_bridge = self.cfg.get("default_bridge")
            if default_bridge and default_bridge not in bridges:
                bridges.insert(0, default_bridge)
            self.query_one("#sel-bridge", Select).set_options(
                [("(template default)", "")] + [(b, b) for b in bridges])

            # ipmode radios
            ipmode = self.query_one("#ipmode-set", RadioSet)
            ipmode.mount(RadioButton("DHCP", id="ip-dhcp"))
            ipmode.mount(RadioButton("Static", id="ip-static"))
            ipmode._selected = 0

            # templates + plans + ssh keys via the phase CLI (no drift)
            try:
                rc, out = self._run_phase("template", "list", json_out=True)
                if rc == 0 and out:
                    rows = _last_json(out)
                    self.oses = sorted({r["name"].split("-", 1)[-1] for r in rows
                                        if "-" in r["name"] and r["name"].split("-", 1)[-1] != "none"})
            except Exception:
                self.oses = []
            os_sel = self.query_one("#sel-os", Select)
            if self.oses:
                os_sel.set_options([(o, o) for o in self.oses])
                os_sel.value = self.oses[0]
            else:
                os_sel.set_options([("(no templates found)", "")])

            try:
                rc, out = self._run_phase("plan", "list", json_out=True)
                if rc == 0 and out:
                    self.plans = _last_json(out) or []
            except Exception:
                self.plans = []
            plan_sel = self.query_one("#load-plan", Select)
            if self.plans:
                plan_sel.set_options([("— new VM —", "")] +
                                     [(p["name"], p["name"]) for p in self.plans])
            else:
                plan_sel.set_options([("— new VM —", "")])

            try:
                rc, out = self._run_phase("ssh-key", "list")
                self.ssh_keys = [l.strip() for l in out.splitlines()
                                 if l.strip().startswith(("ssh-", "ecdsa-", "sk-",
                                                          "-----BEGIN"))]
            except Exception:
                self.ssh_keys = []
            key_sel = self.query_one("#sel-sshkey", Select)
            if self.ssh_keys:
                key_sel.set_options([("(none — template defaults)", "")] +
                                    [(k, k) for k in self.ssh_keys])
            else:
                key_sel.set_options([("(config + template defaults)", "")])

            self._show_step(1)
            self.refresh_summary()
            self.query_one("#disk-table", DataTable).can_focus = False

        def _run_phase(self, *args, json_out=False):
            from .tui import _run_phase
            return _run_phase(self.cfg_path, self.host, *args, json_out=json_out)

        # -- layout -------------------------------------------------------

        def compose(self) -> ComposeResult:
            with Vertical(id="wiz-root"):
                with Horizontal(id="wiz-body"):
                    with Vertical(id="wiz-sidebar"):
                        yield Static("CREATE VM", classes="wiz-sidebar-title")
                        for i, name in enumerate(STEP_NAMES, 1):
                            yield Button(f"{i}. {name}", id=f"step-btn-{i}",
                                         classes="step-btn")
                        yield Static("Enter: next · Esc: back\n"
                                     "1-6: jump · s: save plan\n"
                                     "c: create · p: create+provision",
                                     classes="wiz-sidebar-hint")
                    with VerticalScroll(id="wiz-panes"):
                        with Vertical(id="pane-basics", classes="wiz-pane"):
                            yield Label("Basics", classes="wiz-pane-title")
                            yield Label("start from an existing plan (optional)")
                            yield Select([("— new VM —", "")], id="load-plan")
                            yield Label("name")
                            yield Input(placeholder="e.g. postgres", id="in-name")
                            yield Label("description")
                            yield Input(placeholder="optional", id="in-desc")
                            yield Label("tags (comma separated)")
                            yield Input(placeholder="db,prod", id="in-tags")
                            yield Label("os (template)")
                            yield Select([("(loading…)", "")], id="sel-os")
                            yield Checkbox("start on boot", value=True, id="in-onboot")
                            yield Checkbox("protected", id="in-protect")
                            yield Static("", id="err-basics", classes="wiz-err")
                        with Vertical(id="pane-resources", classes="wiz-pane"):
                            yield Label("Resources", classes="wiz-pane-title")
                            yield Label("size preset")
                            yield RadioSet(id="size-set")
                            yield Label("cores (blank = size default)")
                            yield Input(placeholder="", id="in-cores")
                            yield Label("memory MB (blank = size default)")
                            yield Input(placeholder="", id="in-memory")
                            yield Label("gpu mdev (optional)")
                            yield Input(placeholder="e.g. nvidia-223", id="in-gpu")
                            yield Static("", id="err-resources", classes="wiz-err")
                        with Vertical(id="pane-disks", classes="wiz-pane"):
                            yield Label("Disks", classes="wiz-pane-title")
                            yield Label("boot disk (scsi0)")
                            with Horizontal():
                                yield Input(placeholder="size (blank = template default)",
                                            id="in-osdisk-size")
                                yield Input(placeholder="storage (blank = default)",
                                            id="in-osdisk-storage")
                            yield Label("add data disk")
                            with Horizontal():
                                yield Input(placeholder="id (scsi1)", id="in-disk-id")
                                yield Input(placeholder="size (50G)", id="in-disk-size")
                                yield Input(placeholder="storage (nas)", id="in-disk-storage")
                                yield Button("＋ add", id="disk-add", variant="primary")
                                yield Button("－ remove", id="disk-del")
                            yield DataTable(id="disk-table")
                            yield Static("", id="err-disks", classes="wiz-err")
                        with Vertical(id="pane-network", classes="wiz-pane"):
                            yield Label("Network", classes="wiz-pane-title")
                            yield Label("bridge")
                            yield Select([("(template default)", "")], id="sel-bridge")
                            yield Label("vlan tag (optional)")
                            yield Input(placeholder="e.g. 10", id="in-vlan")
                            yield Label("ip mode")
                            yield RadioSet(id="ipmode-set")
                            yield Label("static ip (when Static)")
                            yield Input(placeholder="10.10.1.50/24", id="in-ip")
                            yield Label("gateway (optional)")
                            yield Input(placeholder="10.10.1.1", id="in-gw")
                            yield Static("", id="err-network", classes="wiz-err")
                        with Vertical(id="pane-bootstrap", classes="wiz-pane"):
                            yield Label("Bootstrap", classes="wiz-pane-title")
                            yield Checkbox("system (apt upgrade, ufw, fail2ban)",
                                           id="bs-system")
                            yield Checkbox("docker", id="bs-docker")
                            yield Checkbox("tailscale", id="bs-tailscale")
                            yield Label("ssh keys (config pool)")
                            yield Select([("(config + template defaults)", "")],
                                         id="sel-sshkey")
                            yield Static("", id="err-bootstrap", classes="wiz-err")
                        with Vertical(id="pane-review", classes="wiz-pane"):
                            yield Label("Review", classes="wiz-pane-title")
                            yield Static("", id="review-plan")
                            yield Static("", id="review-cmd")
                            yield Static("", id="err-review", classes="wiz-err")
                            with Horizontal():
                                yield Button("💾 Save plan", id="act-save")
                                yield Button("⚙  Create", id="act-create",
                                             variant="primary")
                                yield Button("🚀 Create + provision",
                                             id="act-provision", variant="success")
                            yield RichLog(highlight=True, wrap=True, id="review-log")
                yield Static("", id="wiz-summary", classes="wiz-summary")

        # -- step navigation ----------------------------------------------

        def _show_step(self, n: int) -> None:
            self.step = n
            for i in range(1, 7):
                pane = self.query_one(f"#pane-{STEP_NAMES[i-1].lower()}")
                pane.display = (i == n)
                btn = self.query_one(f"#step-btn-{i}", Button)
                if i == n:
                    btn.set_class(True, "cur")
                    btn.set_class(False, "done")
                elif self._step_ok(i):
                    btn.set_class(True, "done")
                    btn.set_class(False, "cur")
                else:
                    btn.set_class(False, "cur")
                    btn.set_class(False, "done")
            if n == 6:
                self.refresh_review()
            self._focus_first(n)

        def _focus_first(self, n: int) -> None:
            ids = {1: "#in-name", 2: "#size-set", 3: "#in-osdisk-size",
                   4: "#sel-bridge", 5: "#bs-system", 6: "#act-save"}
            try:
                self.query_one(ids[n]).focus()
            except Exception:
                pass

        def _step_ok(self, n: int) -> bool:
            return not validate_step(self._state(), n, self.cfg)

        def _err_widget(self, n: int):
            return self.query_one(f"#err-{STEP_NAMES[n-1].lower()}")

        def action_next_step(self) -> None:
            if self._busy:
                return
            errs = validate_step(self._state(), self.step, self.cfg)
            if errs:
                self._err_widget(self.step).update("✗ " + "\n".join(errs))
                self._err_widget(self.step).styles.color = "red"
                return
            self._err_widget(self.step).update("")
            if self.step < 6:
                self._show_step(self.step + 1)
            else:
                # review: jump focus to the primary action
                try:
                    self.query_one("#act-create", Button).focus()
                except Exception:
                    pass

        def action_prev_step(self) -> None:
            if self._busy:
                return
            if self.step > 1:
                self._show_step(self.step - 1)
            else:
                self.app.pop_screen()

        def action_goto_step(self, n: int) -> None:
            if self._busy:
                return
            if 1 <= n <= 6:
                self._show_step(n)

        # -- live refresh --------------------------------------------------

        def on_input_changed(self, event: Input.Changed) -> None:
            self._on_any_change()

        def on_input_submitted(self, event: Input.Submitted) -> None:
            """Enter inside an input = next step (or add the disk on the disk
            form). Textual consumes Enter on non-empty inputs, so the screen
            binding can't see it — we catch the submitted message instead."""
            if self._busy:
                return
            if self.step == 3 and event.input.id in ("in-disk-id", "in-disk-size",
                                                     "in-disk-storage"):
                self._add_data_disk()
            else:
                self.action_next_step()

        def on_select_changed(self, event: Select.Changed) -> None:
            if event.control.id == "load-plan" and event.control.value:
                self._load_plan(event.control.value)
            self._on_any_change()

        def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
            self._on_any_change()

        def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
            self._on_any_change()

        def _on_any_change(self) -> None:
            if not self.is_mounted:
                return
            errs = validate_step(self._state(), self.step, self.cfg)
            self._err_widget(self.step).update("✗ " + "\n".join(errs) if errs else "")
            self._update_step_marks()
            self.refresh_summary()

        def _update_step_marks(self) -> None:
            for i in range(1, 7):
                btn = self.query_one(f"#step-btn-{i}", Button)
                if i == self.step:
                    continue
                btn.set_class(self._step_ok(i), "done")

        def refresh_summary(self) -> None:
            self.query_one("#wiz-summary", Static).update(
                "  " + summary_line(self._state(), self.sizes))

        def refresh_review(self) -> None:
            state = self._state()
            q = self.query_one
            # plan preview (mirrors what `phase vm plan` prints)
            size = state["size"]
            s = self.sizes.get(size, {})
            cores = state["cores"] or s.get("cores", "?")
            mem = state["memory"] or s.get("memory", "?")
            lines = [
                f"  Name:      {state['name'] or '(unnamed)'}",
                f"  Size:      {size} ({cores} vCPU, {mem} MB)",
                f"  OS:        {state['os'] or '?'}",
            ]
            os_d = f"scsi0 ({state['os_disk_size'] or 'template default'}"
            os_d += f" on {state['os_disk_storage']})" if state["os_disk_storage"] else ")"
            lines.append(f"  Boot disk: {os_d}")
            for d in state["data_disks"]:
                lines.append(f"  Data disk: {d['id']} {d['size']}"
                             + (f" on {d['storage']}" if d.get("storage") else ""))
            net = state["bridge"] or "template default"
            net += f", vlan {state['vlan']}" if state["vlan"] else ""
            lines.append(f"  Network:   {net}")
            lines.append(f"  IP:        {state['ip'] if state['ipmode']=='static' else 'dhcp'}"
                         + (f" gw {state['gw']}" if state["gw"] else ""))
            lines.append(f"  GPU:       {state['gpu'] or 'none'}")
            lines.append(f"  On boot:   {'yes' if state['onboot'] else 'no'}")
            lines.append(f"  Protected: {'yes' if state['protect'] else 'no'}")
            lines.append(f"  Tags:      {','.join(state['tags']) or 'none'}")
            lines.append(f"  SSH keys:  {','.join(state['ssh_keys']) or 'config + template defaults'}")
            bs = [k for k in ("system", "docker", "tailscale")
                  if state["bootstrap"].get(k)]
            lines.append(f"  Bootstrap: {','.join(bs) or 'none'}")
            lines.append(f"  Desc:      {state['description'] or 'none'}")
            q("#review-plan", Static).update("\n".join(lines))
            q("#review-cmd", Static).update("command:\n  phase " + " ".join(
                shlex.quote(a) for a in plan_argv(state, "create")))
            # errors across all steps
            all_errs = []
            for i in range(1, 6):
                all_errs += validate_step(state, i, self.cfg)
            err = q("#err-review", Static)
            err.update("✗ " + "\n".join(all_errs) if all_errs else "")
            for wid in ("act-save", "act-create", "act-provision"):
                q(f"#{wid}", Button).disabled = bool(all_errs) or not state["name"] \
                    or not state["os"] or self._busy

        def _load_plan(self, name: str) -> None:
            try:
                rc, out = self._run_phase("plan", "show", name, json_out=True)
                if rc != 0:
                    return
                plan = _last_json(out)
            except Exception:
                return
            if not isinstance(plan, dict):
                return
            self._loaded_plan = plan
            self.query_one("#in-name", Input).value = plan.get("name", "")
            self.query_one("#in-desc", Input).value = plan.get("description") or ""
            self.query_one("#in-tags", Input).value = ",".join(plan.get("tags") or [])
            self.query_one("#in-onboot", Checkbox).value = bool(plan.get("onboot", True))
            self.query_one("#in-protect", Checkbox).value = bool(plan.get("protection"))
            size = plan.get("size") or ""
            if size in self.sizes:
                self._radio_select(self.query_one("#size-set", RadioSet), size)
            self.query_one("#in-cores", Input).value = plan.get("cores") or ""
            self.query_one("#in-memory", Input).value = plan.get("memory") or ""
            self.query_one("#in-gpu", Input).value = plan.get("gpu") or ""
            disks = plan.get("disks") or []
            os_d = next((d for d in disks if d.get("role") == "os"), {})
            self.query_one("#in-osdisk-size", Input).value = os_d.get("size") or ""
            self.query_one("#in-osdisk-storage", Input).value = os_d.get("storage") or ""
            self.data_disks = [d for d in disks if d.get("role") != "os"]
            self._render_disk_table()
            net = plan.get("net") or {}
            if net.get("bridge"):
                self.query_one("#sel-bridge", Select).value = net["bridge"]
            self.query_one("#in-vlan", Input).value = net.get("vlan") or ""
            ip = plan.get("ipconfig") or ""
            ipmode = self.query_one("#ipmode-set", RadioSet)
            if ip and ip != "dhcp":
                self._radio_select(ipmode, "static")
                self.query_one("#in-ip", Input).value = ip
            else:
                self._radio_select(ipmode, "dhcp")
                self.query_one("#in-ip", Input).value = ""
            self.query_one("#in-gw", Input).value = plan.get("gw") or ""
            bs = plan.get("bootstrap") or {}
            for flag in ("system", "docker", "tailscale"):
                self.query_one(f"#bs-{flag}", Checkbox).value = bool(bs.get(flag))
            keys = plan.get("ssh_keys") or []
            if len(keys) == 1:
                try:
                    self.query_one("#sel-sshkey", Select).value = keys[0]
                except Exception:
                    pass
            self._on_any_change()

        # -- disks ---------------------------------------------------------

        def _render_disk_table(self) -> None:
            table = self.query_one("#disk-table", DataTable)
            table.clear()
            table.can_focus = False  # stay out of the tab order; arrow keys still work
            if not getattr(self, "_disk_cols", False):
                table.add_columns("ID", "Size", "Storage")
                self._disk_cols = True
            for i, d in enumerate(self.data_disks):
                table.add_row(d.get("id", ""), d.get("size", ""),
                              d.get("storage", ""), key=str(i))

        def on_button_pressed(self, event: Button.Pressed) -> None:
            bid = event.button.id or ""
            if bid.startswith("step-btn-"):
                self.action_goto_step(int(bid[-1]))
            elif bid == "disk-add":
                self._add_data_disk()
            elif bid == "disk-del":
                self._del_data_disk()
            elif bid == "act-save":
                self.save_plan()
            elif bid == "act-create":
                self.create_vm()
            elif bid == "act-provision":
                self.provision_vm()

        def _add_data_disk(self) -> None:
            q = self.query_one
            d = {"id": q("#in-disk-id", Input).value.strip(),
                 "size": q("#in-disk-size", Input).value.strip(),
                 "storage": q("#in-disk-storage", Input).value.strip()}
            if not d["id"] or not d["size"]:
                self.query_one("#err-disks", Static).update(
                    "✗ data disk needs an id and a size")
                return
            self.data_disks.append(d)
            self._render_disk_table()
            q("#in-disk-id", Input).value = ""
            q("#in-disk-size", Input).value = ""
            q("#in-disk-storage", Input).value = ""
            self._on_any_change()

        def _del_data_disk(self) -> None:
            table = self.query_one("#disk-table", DataTable)
            if table.cursor_row is None or table.cursor_row >= len(self.data_disks):
                return
            del self.data_disks[table.cursor_row]
            self._render_disk_table()
            self._on_any_change()

        # -- actions ---------------------------------------------------------

        def _set_busy(self, busy: bool) -> None:
            self._busy = busy
            for i in range(1, 7):
                self.query_one(f"#step-btn-{i}", Button).disabled = busy

        def _run_action(self, action: str) -> None:
            state = self._state()
            errs = []
            for i in range(1, 6):
                errs += validate_step(state, i, self.cfg)
            if errs or not state["name"] or not state["os"]:
                self.refresh_review()
                return
            self._set_busy(True)
            self.refresh_review()
            log = self.query_one("#review-log", RichLog)
            log.write(f"running: phase {' '.join(plan_argv(state, action))}")
            self.run_worker(self._action_worker(state, action), exclusive=True,
                            group="action")

        async def _action_worker(self, state: dict, action: str):
            import asyncio
            rc, out = await asyncio.to_thread(
                self._run_phase, *plan_argv(state, action))
            self._action_done(action, rc, out)

        def _action_done(self, action: str, rc: int, out: str) -> None:
            log = self.query_one("#review-log", RichLog)
            log.write(out)
            log.write(f"\n[exit {rc}]")
            self._set_busy(False)
            self.refresh_review()
            if rc == 0:
                log.write(f"✓ {action} ok")

        def action_save_plan(self) -> None:
            self._run_action("plan")

        def save_plan(self) -> None:
            self._run_action("plan")

        def action_create_vm(self) -> None:
            self._run_action("create")

        def create_vm(self) -> None:
            self._run_action("create")

        def action_provision_vm(self) -> None:
            self._run_action("provision")

        def provision_vm(self) -> None:
            self._run_action("provision")


    def _last_json(out: str):
        """Parse the JSON payload of a `phase --json` command.

        log() prints indented JSON verbatim (possibly many lines), and some
        commands prefix extra lines — so try the full output first, then
        progressively drop leading lines.
        """
        import json
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        if not lines:
            return None
        for start in range(len(lines)):
            cand = "\n".join(lines[start:])
            try:
                return json.loads(cand)
            except json.JSONDecodeError:
                continue
        return None

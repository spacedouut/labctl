"""Textual TUI — the friendly face of phase.

`phase` (bare, on a TTY) or `phase tui` opens this. It's a front-end over the
same command grammar: every menu action shells out to `phase <subcommand>`, so
the TUI and scripting can never drift apart.

Requires the optional [tui] extra:  uv pip install -e '.[tui]'
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

from .extras import MissingExtra


def run(cfg_path=None, host=None) -> int:
    try:
        import textual
        from textual.app import App
    except ImportError:
        raise MissingExtra("tui")
    app = PhaseApp(cfg_path=cfg_path, host=host)
    app.run()
    return 0


def _phase_cmd(cfg_path, host, *args) -> list[str]:
    cmd = [os.environ.get("PHASE_BIN") or shutil.which("phase") or "phase"]
    if cfg_path:
        cmd += ["--config", cfg_path]
    if host:
        cmd += ["--host", host]
    cmd += list(args)
    return cmd


def _run_phase(cfg_path, host, *args, json_out=False) -> tuple[int, str]:
    cmd = _phase_cmd(cfg_path, host, *args)
    if json_out:
        cmd = cmd[:1] + ["--json"] + cmd[1:]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


# ---------------------------------------------------------------------------

from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.screen import Screen  # noqa: E402
from textual.widgets import (DataTable, Footer, Header, Label, ListItem,  # noqa: E402
                             ListView, RichLog, Static)  # noqa: E402

from .wizard import CreateWizardScreen, _last_json  # noqa: E402


class PhaseApp(App):
    TITLE = "phase"
    SUB_TITLE = "Proxmox VM orchestration"
    BINDINGS = [Binding("q", "quit", "quit")]

    def __init__(self, cfg_path=None, host=None):
        super().__init__()
        self.cfg_path = cfg_path
        self.host = host

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Footer()

    def on_mount(self) -> None:
        self.push_screen(MenuScreen(self.cfg_path, self.host))


class MenuScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "back")]

    def __init__(self, cfg_path=None, host=None):
        super().__init__()
        self.cfg_path = cfg_path
        self.host = host

    def compose(self) -> ComposeResult:
        yield Static("┌──────────────────────────────────────────┐\n"
                     "│  🦞  phase — pick your poison            │\n"
                     "└──────────────────────────────────────────┘",
                     classes="title")
        yield ListView(
            ListItem(Label("🖥  VMs"), id="menu-vms"),
            ListItem(Label("➕  Create VM"), id="menu-create"),
            ListItem(Label("🗂  Templates"), id="menu-templates"),
            ListItem(Label("🔍  Engine scan"), id="menu-engine"),
            ListItem(Label("🧩  Extras"), id="menu-extras"),
            ListItem(Label("🚪  Quit"), id="menu-quit"),
        )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        if item_id == "menu-vms":
            self.app.push_screen(VMsScreen(self.cfg_path, self.host))
        elif item_id == "menu-create":
            self.app.push_screen(CreateWizardScreen(self.cfg_path, self.host))
        elif item_id == "menu-templates":
            self.app.push_screen(TemplatesScreen(self.cfg_path, self.host))
        elif item_id == "menu-engine":
            self.app.push_screen(EngineScreen(self.cfg_path, self.host))
        elif item_id == "menu-extras":
            self.app.push_screen(ExtrasScreen(self.cfg_path, self.host))
        elif item_id == "menu-quit":
            self.app.exit()


class _PhaseScreen(Screen):
    """Base: a RichLog that runs a phase command in a thread."""

    def __init__(self, cfg_path=None, host=None):
        super().__init__()
        self.cfg_path = cfg_path
        self.host = host

    def run_cmd(self, *args, json_out=False):
        self.log_console.clear()
        self.log_console.write("running: phase " + " ".join(args))
        rc, out = _run_phase(self.cfg_path, self.host, *args, json_out=json_out)
        self.log_console.write(out)
        self.log_console.write(f"\n[exit {rc}]")
        return rc


class VMsScreen(_PhaseScreen):
    BINDINGS = [
        Binding("r", "refresh", "refresh"),
        Binding("c", "create", "create"),
        Binding("enter", "detail", "detail"),
        Binding("escape", "app.pop_screen", "back"),
    ]

    def __init__(self, cfg_path=None, host=None):
        super().__init__(cfg_path, host)
        self.rows = []

    def compose(self) -> ComposeResult:
        yield Static("VMs — Enter: actions · r: refresh · c: create · Esc: back", id="vmhead")
        yield DataTable(id="vms")
        yield RichLog(highlight=True, wrap=True, id="vmlog")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("VMID", "Name", "Status", "IP", "Tags")
        self.load()

    def load(self) -> None:
        rc, out = _run_phase(self.cfg_path, self.host, "vm", "list", json_out=True)
        table = self.query_one(DataTable)
        table.clear()
        self.rows = []
        if rc == 0:
            try:
                data = _last_json(out) or []
            except json.JSONDecodeError:
                data = []
            for r in data:
                table.add_row(str(r["vmid"]), r["name"], r["status"], r["ip"], r["tags"])
                self.rows.append(r)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        row = self.rows[event.cursor_row]
        self.app.push_screen(VMDetailScreen(self.cfg_path, self.host, row))

    def action_refresh(self):
        self.load()

    def action_create(self):
        self.app.push_screen(CreateWizardScreen(self.cfg_path, self.host))

    def action_detail(self):
        table = self.query_one(DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self.rows):
            self.app.push_screen(
                VMDetailScreen(self.cfg_path, self.host, self.rows[table.cursor_row]))


class VMDetailScreen(_PhaseScreen):
    BINDINGS = [Binding("escape", "app.pop_screen", "back")]

    def __init__(self, cfg_path=None, host=None, row: dict | None = None):
        super().__init__(cfg_path, host)
        self.row = row or {}
        self.name = row.get("name", "?")

    def compose(self) -> ComposeResult:
        r = self.row
        yield Static(
            f"VM {self.name}  ·  VMID {r.get('vmid', '?')}  ·  {r.get('status', '?')}  ·  "
            f"IP {r.get('ip', '-')}  ·  tags: {r.get('tags', '-')}",
            id="vmdet-head")
        yield ListView(
            ListItem(Label("status"), id="det-status"),
            ListItem(Label("start"), id="det-start"),
            ListItem(Label("stop"), id="det-stop"),
            ListItem(Label("reboot"), id="det-reboot"),
            ListItem(Label("shutdown"), id="det-shutdown"),
            ListItem(Label("exec: uptime"), id="det-exec-uptime"),
            ListItem(Label("ip"), id="det-ip"),
            ListItem(Label("agent"), id="det-agent"),
            ListItem(Label("disk list"), id="det-disk"),
            ListItem(Label("snapshot list"), id="det-snapshot"),
        )
        yield RichLog(highlight=True, wrap=True, id="vmdet-log")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        if item_id == "det-exec-uptime":
            self.run_cmd("vm", self.name, "exec", "--", "uptime")
        elif item_id == "det-disk":
            self.run_cmd("vm", self.name, "disk", "list")
        elif item_id == "det-snapshot":
            self.run_cmd("vm", self.name, "snapshot", "list")
        elif item_id == "det-status":
            self.run_cmd("vm", self.name, "status")
        elif item_id in ("det-start", "det-stop", "det-reboot", "det-shutdown"):
            self.run_cmd("vm", self.name, item_id.removeprefix("det-"))
        elif item_id == "det-ip":
            self.run_cmd("vm", self.name, "ip")
        elif item_id == "det-agent":
            self.run_cmd("vm", self.name, "agent")
        else:
            self.log_console.write(f"unknown action: {item_id}")


class TemplatesScreen(_PhaseScreen):
    BINDINGS = [
        Binding("r", "refresh", "refresh"),
        Binding("escape", "app.pop_screen", "back"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("Templates — r: refresh · Esc: back", id="tpl-head")
        yield DataTable(id="tpls")
        yield RichLog(highlight=True, wrap=True, id="tpl-log")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("VMID", "Name", "State")
        self.load()

    def load(self) -> None:
        rc, out = _run_phase(self.cfg_path, self.host, "template", "list", json_out=True)
        table = self.query_one(DataTable)
        table.clear()
        if rc == 0:
            try:
                data = _last_json(out) or []
            except json.JSONDecodeError:
                data = []
            for r in data:
                table.add_row(str(r["vmid"]), r["name"], r["state"])

    def action_refresh(self):
        self.load()


class EngineScreen(_PhaseScreen):
    BINDINGS = [
        Binding("r", "refresh", "refresh"),
        Binding("escape", "app.pop_screen", "back"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("Engine scan — r: refresh · Esc: back", id="engine-head")
        yield RichLog(highlight=True, wrap=True, id="engine-log")

    def on_mount(self) -> None:
        self.load()

    def load(self) -> None:
        self.run_cmd("engine", "scan")

    def action_refresh(self):
        self.load()


class ExtrasScreen(_PhaseScreen):
    BINDINGS = [Binding("escape", "app.pop_screen", "back")]

    def compose(self) -> ComposeResult:
        yield Static("Extras", id="extras-head")
        yield RichLog(highlight=True, wrap=True, id="extras-log")

    def on_mount(self) -> None:
        self.run_cmd("extras")

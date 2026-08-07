"""Textual TUI — the friendly face of phase.

`phase` (bare, on a TTY) or `phase tui` opens this. It's a front-end over the
same command grammar: every menu action shells out to `phase <subcommand>`, so
the TUI and scripting can never drift apart.

Requires the optional [tui] extra:  uv pip install -e '.[tui]'
"""

from __future__ import annotations

import asyncio
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

from rich.text import Text  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import Horizontal, Vertical  # noqa: E402
from textual.screen import ModalScreen, Screen  # noqa: E402
from textual.widgets import (Button, DataTable, Footer, Header, Input,  # noqa: E402
                             Label, ListItem, ListView, RichLog, Static)  # noqa: E402

from .wizard import CreateWizardScreen, _last_json  # noqa: E402


def _status_cell(status: str) -> Text:
    if status == "running":
        return Text("● running", style="bold green")
    if status == "stopped":
        return Text("● stopped", style="bold red")
    if status == "paused":
        return Text("● paused", style="bold yellow")
    return Text(status, style="dim")


def _head(title: str, sub: str = "", keys: str = "") -> Static:
    """Standard screen header bar (title + context + key hints)."""
    markup = f"[b]{title}[/]"
    if sub:
        markup += f"  [dim]{sub}[/]"
    if keys:
        markup += f"\n[dim]{keys}[/]"
    return Static(markup, classes="screen-head")


class PhaseApp(App):
    TITLE = "phase"
    SUB_TITLE = "Proxmox VM orchestration"
    BINDINGS = [Binding("q", "quit", "quit")]

    CSS = """
    Screen { background: $surface; }
    .screen-head {
        height: auto;
        padding: 1 2;
        background: $panel;
        border-bottom: solid $primary;
    }
    .log-panel { border: round $primary; }
    .badge { padding: 0 1; }
    """

    def __init__(self, cfg_path=None, host=None):
        super().__init__()
        self.cfg_path = cfg_path
        self.host = host

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Footer()

    def on_mount(self) -> None:
        self.push_screen(MenuScreen(self.cfg_path, self.host))


# ---------------------------------------------------------------------------
# gum-style dialogs


class ConfirmScreen(ModalScreen):
    """Yes/no confirmation — the Textual twin of the CLI's rich/gum confirm."""

    BINDINGS = [Binding("escape", "no", "no")]

    CSS = """
    #dialog { width: 64; height: auto; border: thick $primary; background: $surface;
              padding: 1 2; }
    #dialog.danger { border: thick $error; }
    #dialog #prompt { padding: 0 0 1 0; }
    #dialog #buttons { height: auto; align: right middle; padding-top: 1; }
    #dialog Button { margin-left: 1; }
    """

    def __init__(self, prompt: str, on_yes, danger: bool = False):
        super().__init__()
        self.prompt = prompt
        self.on_yes = on_yes
        self.danger = danger

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="danger" if self.danger else ""):
            yield Static(self.prompt, id="prompt")
            with Horizontal(id="buttons"):
                yield Button("No", id="no")
                yield Button("Yes", id="yes", variant="error" if self.danger else "primary")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()  # safe default

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "yes":
            self._yes()
        else:
            self._no()

    def action_no(self) -> None:
        self._no()

    def _no(self) -> None:
        self.app.pop_screen()

    def _yes(self) -> None:
        cb = self.on_yes
        self.app.pop_screen()
        cb()


class DestroyScreen(ModalScreen):
    """Type-the-name destroy confirmation — gum-style, explicit and safe."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    CSS = """
    #dialog { width: 64; height: auto; border: thick $error; background: $surface;
              padding: 1 2; }
    #dialog #prompt { padding: 0 0 1 0; }
    #dialog Input { margin: 0 0 1 0; }
    #dialog #err { color: $error; }
    #dialog #buttons { height: auto; align: right middle; padding-top: 1; }
    #dialog Button { margin-left: 1; }
    """

    def __init__(self, name: str, on_destroy):
        super().__init__()
        self.vm_name = name
        self.on_destroy = on_destroy

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(
                f"Type [b]{self.vm_name}[/] to permanently destroy this VM",
                id="prompt")
            yield Input(placeholder=self.vm_name, id="confirm-input")
            yield Static("", id="err")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Destroy", id="go", variant="error", disabled=True)

    def on_mount(self) -> None:
        self.query_one("#confirm-input", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        ok = event.value.strip() == self.vm_name
        self.query_one("#go", Button).disabled = not ok
        self.query_one("#err", Static).update(
            "" if ok or not event.value.strip() else "name does not match")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.value.strip() == self.vm_name:
            self._destroy()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "go":
            self._destroy()
        else:
            self.action_cancel()

    def action_cancel(self) -> None:
        self.app.pop_screen()

    def _destroy(self) -> None:
        cb = self.on_destroy
        self.app.pop_screen()
        cb()


# ---------------------------------------------------------------------------
# base screen


class _PhaseScreen(Screen):
    """Base: a RichLog that runs phase commands off-thread."""

    def __init__(self, cfg_path=None, host=None):
        super().__init__()
        self.cfg_path = cfg_path
        self.host = host

    def run_cmd(self, *args, json_out=False, on_done=None):
        self.log_console.write(f"[bold cyan]▶ phase {' '.join(args)}[/]")
        self.run_worker(self._cmd_worker(args, json_out, on_done),
                        exclusive=True, group="cmd")

    async def _cmd_worker(self, args, json_out, on_done):
        rc, out = await asyncio.to_thread(
            _run_phase, self.cfg_path, self.host, *args, json_out=json_out)
        self._cmd_done(args, rc, out, on_done)

    def _cmd_done(self, args, rc, out, on_done):
        self.log_console.write(out)
        self.log_console.write(
            f"\n[{'bold green' if rc == 0 else 'bold red'}][exit {rc}][/]")
        if on_done:
            on_done(rc, out)


# ---------------------------------------------------------------------------
# menu


class MenuScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "back")]

    CSS = """
    #menu-screen { align: center middle; }
    #menu-box { width: 62; height: auto; max-height: 85%; border: round $primary;
                background: $surface; padding: 1 2; }
    #menu-title { text-style: bold; }
    #menu-sub { color: $text-muted; }
    #menu-list { height: auto; margin: 1 0 1 0; }
    #menu-keys { color: $text-muted; }
    .mi-title { padding: 0 1; }
    .mi-hint { padding: 0 1; color: $text-muted; }
    """

    def __init__(self, cfg_path=None, host=None):
        super().__init__()
        self.cfg_path = cfg_path
        self.host = host

    def compose(self) -> ComposeResult:
        with Vertical(id="menu-screen"):
            with Vertical(id="menu-box"):
                yield Static("🦞  phase", id="menu-title")
                yield Static("Proxmox VM orchestration · "
                             + (self.host or "homelab"), id="menu-sub")
                yield ListView(
                    ListItem(Vertical(
                        Static("[b]🖥  VMs[/]", classes="mi-title"),
                        Static("list · status · power · ssh", classes="mi-hint"),
                    ), id="menu-vms"),
                    ListItem(Vertical(
                        Static("[b]➕  Create VM[/]", classes="mi-title"),
                        Static("6-step wizard · plan · provision", classes="mi-hint"),
                    ), id="menu-create"),
                    ListItem(Vertical(
                        Static("[b]🗂  Templates[/]", classes="mi-title"),
                        Static("clone sources for new VMs", classes="mi-hint"),
                    ), id="menu-templates"),
                    ListItem(Vertical(
                        Static("[b]🔍  Engine scan[/]", classes="mi-title"),
                        Static("vGPU / mdev / PCI discovery", classes="mi-hint"),
                    ), id="menu-engine"),
                    ListItem(Vertical(
                        Static("[b]🧩  Extras[/]", classes="mi-title"),
                        Static("optional features", classes="mi-hint"),
                    ), id="menu-extras"),
                    ListItem(Vertical(
                        Static("[b]🚪  Quit[/]", classes="mi-title"),
                        Static("bye", classes="mi-hint"),
                    ), id="menu-quit"),
                )
                yield Static("↑↓ navigate · enter select · q quit", id="menu-keys")

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


# ---------------------------------------------------------------------------
# VMs


class VMsScreen(_PhaseScreen):
    BINDINGS = [
        Binding("r", "refresh", "refresh"),
        Binding("c", "create", "create"),
        Binding("enter", "detail", "detail"),
        Binding("escape", "app.pop_screen", "back"),
    ]

    CSS = """
    #vms-table { height: 1fr; }
    #vmlog { height: 7; border: none; border-top: solid $panel; }
    """

    def __init__(self, cfg_path=None, host=None):
        super().__init__(cfg_path, host)
        self.rows = []

    def compose(self) -> ComposeResult:
        yield _head("VMs", "loading…", "enter: actions · r: refresh · c: create · Esc: back")
        yield DataTable(id="vms-table")
        yield RichLog(highlight=True, wrap=True, id="vmlog")

    def on_mount(self) -> None:
        table = self.query_one("#vms-table", DataTable)
        table.add_columns("VMID", "Name", "Status", "IP", "Tags")
        table.zebra_stripes = True
        table.cursor_type = "row"
        self.load()

    def load(self) -> None:
        self.run_worker(self._load_worker(), exclusive=True, group="load")

    async def _load_worker(self):
        rc, out = await asyncio.to_thread(
            _run_phase, self.cfg_path, self.host, "vm", "list", json_out=True)
        self._apply(rc, out)

    def _apply(self, rc: int, out: str) -> None:
        table = self.query_one("#vms-table", DataTable)
        table.clear()
        self.rows = []
        data = []
        if rc == 0:
            try:
                data = _last_json(out) or []
            except json.JSONDecodeError:
                data = []
        for r in data:
            tpl = bool(r.get("template"))
            name_cell = Text(r["name"], style="dim" if tpl else "bold")
            if tpl:
                name_cell.append(" (template)", style="dim")
            status = r["status"]
            status_cell = Text("● template", style="dim") if tpl else _status_cell(status)
            table.add_row(str(r["vmid"]), name_cell, status_cell,
                          r["ip"] or "-", r["tags"] or "-")
            self.rows.append(r)
        running = sum(1 for r in self.rows if r["status"] == "running")
        n = len(self.rows)
        sub = f"{n} VM{'s' if n != 1 else ''}"
        if running:
            sub += f" · {running} running"
        self.query_one(".screen-head", Static).update(
            f"[b]VMs[/]  [dim]{sub}[/]\n"
            "[dim]enter: actions · r: refresh · c: create · Esc: back[/]")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        row = self.rows[event.cursor_row]
        self.app.push_screen(VMDetailScreen(self.cfg_path, self.host, row))

    def action_refresh(self):
        self.load()

    def action_create(self):
        self.app.push_screen(CreateWizardScreen(self.cfg_path, self.host))

    def action_detail(self):
        table = self.query_one("#vms-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self.rows):
            self.app.push_screen(
                VMDetailScreen(self.cfg_path, self.host, self.rows[table.cursor_row]))


# ---------------------------------------------------------------------------
# VM detail


class VMDetailScreen(_PhaseScreen):
    BINDINGS = [Binding("escape", "app.pop_screen", "back")]

    CSS = """
    #vmdet-body { height: 1fr; }
    #vmdet-actions { width: 42; border-right: solid $panel; }
    #vmdet-actions ListView { height: 1fr; }
    #vmdet-out { height: 1fr; border: none; }
    .det-group { padding: 0 1; color: $text-muted; text-style: italic; }
    .det-act { padding: 0 2; }
    """

    def __init__(self, cfg_path=None, host=None, row: dict | None = None):
        super().__init__(cfg_path, host)
        self.row = row or {}
        self.vm_name = row.get("name", "?")

    def _head(self) -> Static:
        r = self.row
        tags = f"  [dim]{r.get('tags', '')}[/]" if r.get("tags") else ""
        badge = (Text("● template", style="dim")
                 if r.get("template")
                 else _status_cell(r.get("status", "?")))
        return Static(
            f"[bold]{self.vm_name}[/]  [dim]VMID {r.get('vmid', '?')}[/]  {badge}"
            f"  [dim]IP {r.get('ip') or '-'}[/]{tags}",
            classes="screen-head")

    def _actions(self) -> ListView:
        group = lambda label: ListItem(Static(label, classes="det-group"),
                                       disabled=True)
        act = lambda label, aid: ListItem(Static(f"▶  {label}", classes="det-act"),
                                          id=aid)
        return ListView(
            group("POWER"),
            act("start", "det-start"),
            act("reboot", "det-reboot"),
            act("shutdown", "det-shutdown"),
            act("stop (hard)", "det-stop"),
            group("INSPECT"),
            act("status", "det-status"),
            act("ip", "det-ip"),
            act("agent", "det-agent"),
            group("STORAGE"),
            act("disk list", "det-disk"),
            act("snapshot list", "det-snapshot"),
            group("RUN"),
            act("exec: uptime", "det-exec-uptime"),
            group("DANGER"),
            act("destroy", "det-destroy"),
        )

    def compose(self) -> ComposeResult:
        yield self._head()
        with Horizontal(id="vmdet-body"):
            with Vertical(id="vmdet-actions"):
                yield self._actions()
            yield RichLog(highlight=True, wrap=True, id="vmdet-out")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        if item_id == "det-exec-uptime":
            self.run_cmd("vm", self.vm_name, "exec", "--", "uptime")
        elif item_id == "det-disk":
            self.run_cmd("vm", self.vm_name, "disk", "list")
        elif item_id == "det-snapshot":
            self.run_cmd("vm", self.vm_name, "snapshot", "list")
        elif item_id == "det-status":
            self.run_cmd("vm", self.vm_name, "status")
        elif item_id == "det-start":
            self.run_cmd("vm", self.vm_name, "start")
        elif item_id == "det-stop":
            self._confirm(f"Stop [b]{self.vm_name}[/] (hard power-off)?",
                          lambda: self.run_cmd("vm", self.vm_name, "stop"),
                          danger=True)
        elif item_id == "det-reboot":
            self._confirm(f"Reboot [b]{self.vm_name}[/]?",
                          lambda: self.run_cmd("vm", self.vm_name, "reboot"))
        elif item_id == "det-shutdown":
            self._confirm(f"Shut down [b]{self.vm_name}[/] (ACPI)?",
                          lambda: self.run_cmd("vm", self.vm_name, "shutdown"))
        elif item_id == "det-ip":
            self.run_cmd("vm", self.vm_name, "ip")
        elif item_id == "det-agent":
            self.run_cmd("vm", self.vm_name, "agent")
        elif item_id == "det-destroy":
            self.app.push_screen(DestroyScreen(self.vm_name, self._do_destroy))
        else:
            self.log_console.write(f"unknown action: {item_id}")

    def _confirm(self, prompt: str, on_yes, danger: bool = False) -> None:
        self.app.push_screen(ConfirmScreen(prompt, on_yes, danger=danger))

    def _do_destroy(self) -> None:
        self.run_cmd("vm", self.vm_name, "destroy", "--yes", on_done=self._destroyed)

    def _destroyed(self, rc: int, out: str) -> None:
        if rc != 0:
            return
        self.app.pop_screen()  # back to the VMs list
        vms = self.app.screen
        if isinstance(vms, VMsScreen):
            vms.load()


# ---------------------------------------------------------------------------
# templates / engine / extras


class TemplatesScreen(_PhaseScreen):
    BINDINGS = [
        Binding("r", "refresh", "refresh"),
        Binding("escape", "app.pop_screen", "back"),
    ]

    CSS = """
    #tpls { height: 1fr; }
    #tpl-log { height: 7; border: none; border-top: solid $panel; }
    """

    def __init__(self, cfg_path=None, host=None):
        super().__init__(cfg_path, host)
        self.rows = []

    def compose(self) -> ComposeResult:
        yield _head("Templates", "loading…", "r: refresh · Esc: back")
        yield DataTable(id="tpls")
        yield RichLog(highlight=True, wrap=True, id="tpl-log")

    def on_mount(self) -> None:
        table = self.query_one("#tpls", DataTable)
        table.add_columns("VMID", "Name", "State")
        table.zebra_stripes = True
        table.cursor_type = "row"
        self.load()

    def load(self) -> None:
        self.run_worker(self._load_worker(), exclusive=True, group="load")

    async def _load_worker(self):
        rc, out = await asyncio.to_thread(
            _run_phase, self.cfg_path, self.host, "template", "list", json_out=True)
        self._apply(rc, out)

    def _apply(self, rc: int, out: str) -> None:
        table = self.query_one("#tpls", DataTable)
        table.clear()
        self.rows = []
        data = []
        if rc == 0:
            try:
                data = _last_json(out) or []
            except json.JSONDecodeError:
                data = []
        for r in data:
            table.add_row(str(r["vmid"]), Text(r["name"], style="bold dim"),
                          Text(r["state"], style="yellow"))
            self.rows.append(r)
        self.query_one(".screen-head", Static).update(
            f"[b]Templates[/]  [dim]{len(self.rows)} available[/]\n"
            "[dim]r: refresh · Esc: back[/]")

    def action_refresh(self):
        self.load()


class EngineScreen(_PhaseScreen):
    BINDINGS = [
        Binding("r", "refresh", "refresh"),
        Binding("escape", "app.pop_screen", "back"),
    ]

    def compose(self) -> ComposeResult:
        yield _head("Engine scan", "vGPU / mdev / PCI", "r: refresh · Esc: back")
        yield RichLog(highlight=True, wrap=True, id="engine-log", classes="log-panel")

    def on_mount(self) -> None:
        self.load()

    def load(self) -> None:
        self.run_cmd("engine", "scan")

    def action_refresh(self):
        self.load()


class ExtrasScreen(_PhaseScreen):
    BINDINGS = [Binding("escape", "app.pop_screen", "back")]

    def compose(self) -> ComposeResult:
        yield _head("Extras", "optional features", "Esc: back")
        yield RichLog(highlight=True, wrap=True, id="extras-log", classes="log-panel")

    def on_mount(self) -> None:
        self.run_cmd("extras")

"""Inline GCE-style create wizard — a little box, not a fullscreen takeover.

`phase vm create` / `phase vm provision` (interactive, TTY, no flags) open
this instead of the old fullscreen TUI: a compact two-pane panel rendered in
place with rich (no alternate screen, scrollback preserved).

Layout mirrors the GCE create-VM page: a step list on the left (Machine,
Disks, Network, Others, Finalize) and the selected step's options on the
right. Arrow keys navigate, Enter edits, Esc backs out, Ctrl-C aborts
cleanly. Nothing is executed until Finalize.

Input is handled directly (termios cbreak + raw key parsing) — no Textual,
no gum, no extra dependencies beyond rich (already required).
"""

from __future__ import annotations

import os
import select
import sys
import termios
import time
import tty

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from .plan import save_plan
from .provision import provision_plan
from .util import PhaseError, die, log
from .vm import list_template_oses
from .wizard import validate_step

# ---------------------------------------------------------------------------
# keys


def _read_key(fd: int, timeout: float = 1.0) -> str:
    """Read one keypress from a cbreak tty. Returns names like 'up', 'down',
    'enter', 'esc', 'space', or a single character."""
    def _read1(t: float):
        r, _, _ = select.select([fd], [], [], t)
        if not r:
            return None
        b = os.read(fd, 1)
        return b.decode("latin-1") if b else None

    c = _read1(timeout)
    if c is None:
        return ""
    if c == "\x1b":
        nxt = _read1(0.05)
        if nxt == "[":
            nxt2 = _read1(0.05) or ""
            return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(nxt2, "esc")
        if nxt == "O":
            nxt2 = _read1(0.05) or ""
            return {"H": "home", "F": "end"}.get(nxt2, "esc")
        if nxt is None:
            return "esc"
        return "esc"
    if c in ("\r", "\n"):
        return "enter"
    if c in ("\x7f", "\x08"):
        return "backspace"
    if c == " ":
        return "space"
    if c == "\x03":
        raise KeyboardInterrupt
    if c == "\x1a":
        raise KeyboardInterrupt
    if ord(c) < 32:
        return ""
    return c


class KeyReader:
    """Small wrapper so tests can drive the wizard with injected key feeds."""

    def __init__(self, fd: int):
        self.fd = fd

    def read(self) -> str:
        return _read_key(self.fd)


# ---------------------------------------------------------------------------
# state


def _dget(cfg, dotted, default=None):
    """Dotted-path get that works with Config objects or plain dicts."""
    if hasattr(cfg, "get") and hasattr(cfg, "data"):  # phase.config.Config
        return cfg.get(dotted, default)
    node = cfg
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def new_state(cfg, qm) -> dict:
    """Wizard state — same schema as the Textual wizard (phase/wizard.py),
    so validate_step / summary_line / state_to_plan are shared."""
    oses = [o for o in list_template_oses(cfg, qm) if o != "none"]
    sizes = sorted((_dget(cfg, "templates.sizes") or {}).keys())
    default_size = "small" if "small" in sizes else (sizes[0] if sizes else "")
    bridges = list((_dget(cfg, "networks") or {}).keys())
    return {
        "name": "", "description": "", "tags": [],
        "onboot": True, "protect": False,
        "size": default_size,
        "cores": "", "memory": "", "gpu": "",
        "os": oses[0] if oses else "",
        "os_disk_size": "", "os_disk_storage": "",
        "data_disks": [],
        "bridge": "", "vlan": "", "ipmode": "dhcp", "ip": "", "gw": "",
        "bootstrap": {"system": False, "docker": False, "tailscale": False},
        "ssh_keys": [],
        "_meta": {"sizes": sizes, "oses": oses, "bridges": bridges,
                  "ssh_keys": list(_dget(cfg, "vm.ssh_keys") or [])},
    }


def state_to_plan(state: dict, cfg) -> dict:
    """Convert wizard state to a plan dict (same shape as gather_plan) —
    ready for realize_plan / save_plan / provision_plan."""
    size = state["size"]
    sizes = _dget(cfg, "templates.sizes") or {}
    entry = sizes.get(size)
    if not entry:
        die(f"unknown size: {size}")
    preset_c, preset_m = entry.get("cores"), entry.get("memory")
    cores = state["cores"] or preset_c
    memory = state["memory"] or preset_m
    disks = [{
        "id": "scsi0", "role": "os", "os": state["os"],
        "size": state.get("os_disk_size") or "", "storage": state.get("os_disk_storage") or "",
    }]
    for d in state.get("data_disks", []):
        disks.append({
            "id": d.get("id", ""), "role": "data", "os": "",
            "size": d.get("size", ""), "storage": d.get("storage", ""),
        })
    ip = state.get("ip") or ""
    return {
        "name": state["name"], "size": size, "os": state["os"],
        "cores": str(cores), "memory": str(memory), "disks": disks,
        "net": {"bridge": state.get("bridge") or None,
                "vlan": state.get("vlan") or None},
        "ipconfig": ip if state.get("ipmode") == "static" else None,
        "gw": state.get("gw") or None,
        "onboot": state.get("onboot", True), "description": None,
        "protection": state.get("protect", False),
        "tags": state.get("tags", []), "ssh_keys": state.get("ssh_keys", []),
        "bootstrap": state.get("bootstrap",
                               {"system": False, "docker": False, "tailscale": False}),
        "gpu": state.get("gpu") or None, "vmid": 0,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


# ---------------------------------------------------------------------------
# field model (GCE-style steps)


STEPS = [
    ("Machine", [
        ("name", "name", "text", False),
        ("size", "size preset", "choice", False),
        ("cores", "vCPUs (blank = size default)", "text", True),
        ("memory", "memory MB (blank = size default)", "text", True),
        ("os", "os template", "choice", False),
    ]),
    ("Disks", [
        ("os_disk_size", "boot disk size (blank = template default)", "text", True),
        ("os_disk_storage", "boot disk storage (blank = default)", "text", True),
        ("data_disks", "data disks", "disks", True),
    ]),
    ("Network", [
        ("bridge", "bridge", "choice", True),
        ("vlan", "vlan tag (blank = none)", "text", True),
        ("ipmode", "ip mode", "choice", False),
        ("ip", "static ip (blank = dhcp)", "text", True),
        ("gw", "gateway (optional)", "text", True),
    ]),
    ("Others", [
        ("tags", "tags (comma separated)", "text", True),
        ("onboot", "start on boot", "toggle", False),
        ("protect", "protected", "toggle", False),
        ("gpu", "gpu mdev (optional)", "text", True),
        ("bootstrap", "bootstrap steps", "toggles", True),
        ("ssh_keys", "ssh key (pool)", "choice", True),
    ]),
    ("Finalize", []),
]

STEP_NAME = [s[0] for s in STEPS]
CHOICE_OPTIONS = {
    "size": lambda st: st["_meta"]["sizes"],
    "os": lambda st: st["_meta"]["oses"],
    "bridge": lambda st: ["(template default)"] + st["_meta"]["bridges"],
    "ipmode": lambda st: ["dhcp", "static"],
    "ssh_keys": lambda st: ["(none)"] + st["_meta"]["ssh_keys"],
}
TOGGLE_OPTIONS = {"bootstrap": lambda st: ["system", "docker", "tailscale"]}
DISK_FIELDS = [("id", "id (e.g. scsi1)"), ("size", "size (e.g. 50G)"),
               ("storage", "storage (e.g. nas)")]


def _fmt(state, key, label, kind, optional):
    if kind == "toggle":
        return "[x]" if state.get(key) else "[ ]"
    if kind == "toggles":
        return ", ".join(k for k in ("system", "docker", "tailscale")
                         if state.get("bootstrap", {}).get(k)) or "(none)"
    if kind == "disks":
        return f"{len(state.get('data_disks', []))} disk(s)"
    v = state.get(key)
    if kind == "choice" and key == "bridge" and not v:
        return "(template default)"
    if kind == "choice" and key == "ssh_keys":
        return v[0] if v else "(none)"
    if not v:
        return "(blank)"
    return str(v)


# ---------------------------------------------------------------------------
# the box


def _render(state, step_idx, field_idx, mode, cursor, options, opt_idx,
            errs, summary, action_idx, actions, running, disks_mode,
            text_key=None, disk_form=None, disk_fi=0):
    """Build the two-pane panel. mode: steps|fields|text|choice|toggles|disks|finalize"""
    left = Text()
    for i, (name, _fields) in enumerate(STEPS):
        mark = "✓" if (i < len(STEPS) - 1 and
                       not validate_step(_wizard_state(state), i + 1, None)) else "○"
        if i == step_idx:
            mark = "▶"
        line = Text(f"{mark} {name}", style="bold cyan" if i == step_idx else "white")
        if i == step_idx:
            line.append("  <", style="dim")
        left.append(line)
        left.append("\n")
    left.append("\n")
    left.append(Text("esc: quit", style="dim"))

    right = Text()
    if step_idx == 4:  # Finalize
        right.append(Text("Review\n", style="bold"))
        for line in _summary_lines(state):
            right.append(Text(line + "\n", style="dim"))
        if errs:
            right.append(Text("\n✗ " + "\n".join(errs), style="bold red"))
        if mode == "finalize":
            right.append(Text("\n"))
            for i, (a, _h) in enumerate(actions):
                style = "bold cyan" if i == action_idx else "white"
                right.append(Text(("▶ " if i == action_idx else "  ") + a, style=style))
                right.append(Text("\n"))
    else:
        fields = STEPS[step_idx][1]
        right.append(Text(f"{STEP_NAME[step_idx]}\n", style="bold"))
        for i, (key, label, kind, optional) in enumerate(fields):
            sel = (mode in ("fields", "disks") and i == field_idx)
            prefix = "▶ " if sel else "  "
            style = "bold cyan" if sel else "white"
            right.append(Text(prefix, style=style))
            if kind == "disks" and mode == "disks" and sel:
                right.append(_disks_view(state, disks_mode, disk_fi, cursor,
                                         disk_form or {}), style=style)
            elif kind == "toggles" and mode == "toggles" and sel:
                right.append(_toggles_view(state, opt_idx), style=style)
            elif kind == "choice" and mode == "choice" and sel:
                right.append(_choices_view(options, opt_idx, label), style=style)
            else:
                right.append(Text(f"{label}: ", style=style))
                right.append(Text(_fmt(state, key, label, kind, optional), style="bold"))
            right.append(Text("\n"))
        if mode == "text":
            right.append(Text("\n"))
            right.append(_text_view(state, cursor, text_key))
        if errs:
            right.append(Text("✗ " + "\n".join(errs), style="bold red"))
    right.append(Text("\n" + running, style="dim"))

    layout = Layout()
    layout.split_row(
        Layout(Panel(left, title="Create VM", border_style="cyan"), name="steps", size=24),
        Layout(Panel(right, title=STEP_NAME[step_idx] if step_idx < 5 else "Finalize",
                     border_style="cyan"), name="opts"),
    )
    return layout


def _wizard_state(state):
    """Strip _meta so wizard.validate_step / summary_line can read it."""
    return {k: v for k, v in state.items() if not k.startswith("_")}


def _summary_lines(state):
    st = _wizard_state(state)
    size = st["size"] or "?"
    lines = [
        f"  Name:      {st['name'] or '(unnamed)'}",
        f"  Size:      {size}" + (f"  ·  cores {st['cores']}" if st["cores"] else "")
        + (f"  ·  mem {st['memory']}" if st["memory"] else ""),
        f"  OS:        {st['os'] or '?'}",
    ]
    os_d = f"scsi0 ({st['os_disk_size'] or 'template default'}"
    os_d += f" on {st['os_disk_storage']})" if st["os_disk_storage"] else ")"
    lines.append(f"  Boot disk: {os_d}")
    for d in st["data_disks"]:
        lines.append(f"  Data disk: {d['id']} {d['size']}"
                     + (f" on {d['storage']}" if d.get("storage") else ""))
    net = st["bridge"] or "template default"
    net += f", vlan {st['vlan']}" if st["vlan"] else ""
    lines.append(f"  Network:   {net}")
    lines.append(f"  IP:        {st['ip'] if st['ipmode'] == 'static' else 'dhcp'}"
                 + (f" gw {st['gw']}" if st["gw"] else ""))
    bs = [k for k in ("system", "docker", "tailscale") if st["bootstrap"].get(k)]
    lines.append(f"  Bootstrap: {','.join(bs) or 'none'}")
    lines.append(f"  Tags:      {','.join(st['tags']) or 'none'}")
    return lines


def _choices_view(options, idx, label):
    t = Text(f"{label}:\n")
    for i, o in enumerate(options):
        style = "bold cyan" if i == idx else "white"
        t.append(Text(("▶ " if i == idx else "  ") + str(o) + "\n", style=style))
    return t


def _toggles_view(state, idx):
    t = Text("bootstrap steps (space toggles):\n")
    for i, k in enumerate(("system", "docker", "tailscale")):
        mark = "[x]" if state["bootstrap"].get(k) else "[ ]"
        style = "bold cyan" if i == idx else "white"
        t.append(Text(("▶ " if i == idx else "  ") + f"{mark} {k}\n", style=style))
    return t


def _disks_view(state, disks_mode, disk_fi=0, cursor="", disk_form=None):
    disk_form = disk_form or {}
    disks = state["data_disks"]
    if disks_mode == "list":
        t = Text("data disks (enter: add · enter on row: delete):\n")
        for i, d in enumerate(disks):
            t.append(Text(f"  {d['id']} {d['size']}"
                          + (f" on {d['storage']}" if d.get("storage") else "") + "\n"))
        t.append(Text("  ＋ add disk\n", style="bold cyan"))
        return t
    # disks_mode == "form": entering a new disk
    t = Text("new data disk:\n")
    for i, (dkey, dlabel) in enumerate(DISK_FIELDS):
        val = cursor if i == disk_fi else disk_form.get(dkey, "")
        style = "bold cyan" if i == disk_fi else "white"
        t.append(Text(("▶ " if i == disk_fi else "  ") + f"{dlabel}: ", style=style))
        t.append(Text(str(val) + ("█" if i == disk_fi else "") + "\n", style="bold"))
    return t


def _text_view(state, cursor, text_key):
    label = "input"
    for _name, fields in STEPS:
        for key, flabel, _k, _o in fields:
            if key == text_key:
                label = flabel
    return Text(f"{label}: {cursor}█  (enter ok · esc cancel)", style="bold white")


# ---------------------------------------------------------------------------
# main loop


def run(cfg, qm, action: str = "create") -> int:
    """Run the inline wizard. action: 'create' (default finalize) or
    'provision'. Returns 0 on success, 1 if aborted."""
    if not sys.stdin.isatty():
        die("inline wizard needs a TTY")
    state = new_state(cfg, qm)
    console = Console()
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return _loop(cfg, qm, state, fd, console, action)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _loop(cfg, qm, state, fd, console, action) -> int:
    step_idx = 0
    field_idx = 0
    mode = "steps"        # steps|fields|text|choice|toggles|disks|finalize
    cursor = ""           # text buffer
    text_key = None       # field key being edited
    opt_idx = 0
    options = []
    disks_mode = "list"
    disk_form = {}
    disk_fi = 0
    disk_row = 0
    action_idx = 0
    actions = [("⚙  Create", "create"), ("🚀 Create + provision", "provision"),
               ("💾 Save plan", "save"), ("← Back to steps", "back")]
    if action == "provision":
        actions = [("🚀 Create + provision", "provision"), ("⚙  Create", "create"),
                   ("💾 Save plan", "save"), ("← Back to steps", "back")]
    running = "↑↓ navigate · enter select · esc back · ctrl-c quit"
    errs = []
    result = None

    reader = KeyReader(fd)
    with Live(console=console, refresh_per_second=6, screen=False,
              transient=True) as live:
        while result is None:
            live.update(_render(state, step_idx, field_idx, mode, cursor,
                                options, opt_idx, errs, None, action_idx,
                                actions, running, disks_mode, text_key, disk_form,
                                disk_fi))
            key = reader.read()
            if not key:
                continue

            # --- top level: step navigation ---
            if mode == "steps":
                if key == "up" and step_idx > 0:
                    step_idx -= 1
                elif key == "down" and step_idx < len(STEPS) - 1:
                    step_idx += 1
                elif key in ("enter", "right"):
                    if step_idx == 4:
                        errs = _all_errors(state)
                        if errs:
                            continue
                        action_idx = 0
                        mode = "finalize"
                    else:
                        field_idx = 0
                        mode = "fields"
                elif key == "esc" or key == "q":
                    result = 1
                continue

            # --- finalize actions ---
            if mode == "finalize":
                if key == "up" and action_idx > 0:
                    action_idx -= 1
                elif key == "down" and action_idx < len(actions) - 1:
                    action_idx += 1
                elif key == "enter":
                    what = actions[action_idx][1]
                    if what == "back":
                        mode = "steps"
                    else:
                        result = ("run", what)
                elif key == "esc":
                    mode = "steps"
                continue

            # --- within a step: field list ---
            if mode == "fields":
                fields = STEPS[step_idx][1]
                if key == "up" and field_idx > 0:
                    field_idx -= 1
                elif key == "down" and field_idx < len(fields) - 1:
                    field_idx += 1
                elif key == "enter":
                    fkey, _flabel, fkind, _fopt = fields[field_idx]
                    if fkind == "text":
                        text_key = fkey
                        cursor = str(state.get(fkey) or "")
                        mode = "text"
                    elif fkind == "choice":
                        text_key = fkey
                        options = CHOICE_OPTIONS[fkey](state)
                        opt_idx = _index_of(options, state.get(fkey), fkey)
                        mode = "choice"
                    elif fkind == "toggle":
                        state[fkey] = not state.get(fkey, False)
                    elif fkind == "toggles":
                        text_key = fkey
                        options = TOGGLE_OPTIONS[fkey](state)
                        opt_idx = 0
                        mode = "toggles"
                    elif fkind == "disks":
                        disk_row = 0
                        disks_mode = "list"
                        mode = "disks"
                elif key == "esc":
                    mode = "steps"
                continue

            # --- text editing ---
            if mode == "text":
                if key == "esc":
                    mode = "fields"
                elif key == "enter":
                    _commit_text(state, text_key, cursor)
                    mode = "fields"
                elif key == "backspace":
                    cursor = cursor[:-1]
                elif len(key) == 1 and 32 <= ord(key) <= 126:
                    cursor += key
                continue

            # --- choice editing ---
            if mode == "choice":
                if key == "up" and opt_idx > 0:
                    opt_idx -= 1
                elif key == "down" and opt_idx < len(options) - 1:
                    opt_idx += 1
                elif key == "enter":
                    _commit_choice(state, text_key, options, opt_idx)
                    mode = "fields"
                elif key == "esc":
                    mode = "fields"
                continue

            # --- toggles editing ---
            if mode == "toggles":
                if key == "up" and opt_idx > 0:
                    opt_idx -= 1
                elif key == "down" and opt_idx < len(options) - 1:
                    opt_idx += 1
                elif key in ("enter", "space"):
                    k = options[opt_idx]
                    state["bootstrap"][k] = not state["bootstrap"][k]
                elif key == "esc":
                    mode = "fields"
                continue

            # --- data disks editing ---
            if mode == "disks":
                if disks_mode == "list":
                    n = len(state["data_disks"])
                    if key == "up" and disk_row > 0:
                        disk_row -= 1
                    elif key == "down" and disk_row < n:  # rows + "add"
                        disk_row += 1
                    elif key == "enter":
                        if disk_row == n:  # add
                            disk_form = {}
                            disk_fi = 0
                            cursor = ""
                            disks_mode = "form"
                        elif state["data_disks"]:
                            del state["data_disks"][disk_row]
                            if disk_row >= n - 1:
                                disk_row = max(0, n - 2)
                    elif key == "esc":
                        mode = "fields"
                else:  # form: id / size / storage
                    dkey, _lbl = DISK_FIELDS[disk_fi]
                    if key == "esc":
                        disks_mode = "list"
                        cursor = ""
                    elif key == "enter":
                        disk_form[dkey] = cursor.strip()
                        cursor = ""
                        if disk_fi < len(DISK_FIELDS) - 1:
                            disk_fi += 1
                        else:
                            if disk_form.get("id") and disk_form.get("size"):
                                state["data_disks"].append({
                                    "id": disk_form["id"],
                                    "size": disk_form["size"],
                                    "storage": disk_form.get("storage", ""),
                                })
                            disks_mode = "list"
                    elif key == "backspace":
                        cursor = cursor[:-1]
                    elif len(key) == 1 and 32 <= ord(key) <= 126:
                        cursor += key
                continue

    # Live box is gone — run the chosen action with normal output.
    if isinstance(result, tuple) and result and result[0] == "run":
        return _finalize(cfg, qm, state, result[1])
    return 1


def _index_of(options, cur, key):
    if not cur:
        return 0
    if key == "ssh_keys":
        cur = cur[0] if cur else None
        if not cur:
            return 0
    try:
        return options.index(cur)
    except ValueError:
        return 0


def _all_errors(state) -> list:
    errs = []
    for i in range(1, 5):
        errs += validate_step(_wizard_state(state), i, None)
    return errs


def _commit_text(state, key, value):
    v = value.strip()
    if key == "tags":
        state[key] = [t.strip() for t in v.split(",") if t.strip()]
    elif key in ("cores", "memory"):
        state[key] = v
    else:
        state[key] = v


def _commit_choice(state, key, options, idx):
    val = options[idx]
    if key == "bridge":
        state[key] = "" if val == "(template default)" else val
    elif key == "ssh_keys":
        state[key] = [] if val == "(none)" else [val]
    else:
        state[key] = val


def _finalize(cfg, qm, state, what) -> int:
    if what == "save":
        path = save_plan(state_to_plan(state, cfg))
        log(f"Saved plan: {path}")
        return 0
    plan = state_to_plan(state, cfg)
    if what == "create":
        from .plan import realize_plan
        vmid = realize_plan(cfg, qm, plan)
        log(f"Created {plan['name']} (VMID {vmid}), stopped.")
        return 0
    if what == "provision":
        provision_plan(cfg, qm, plan)
        return 0
    return 1

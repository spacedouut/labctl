"""Optional-extras registry — the copyparty pattern.

Core phase is pure-stdlib and works standalone. Each optional feature lives
behind a named extra; `require()` raises MissingExtra with an install hint
when the extra isn't installed. `phase extras` lists everything.
"""

from __future__ import annotations

import importlib.util
import shutil
from dataclasses import dataclass, field

from .util import PhaseError


class MissingExtra(PhaseError):
    def __init__(self, name: str):
        self.name = name
        super().__init__(
            f"feature '{name}' needs the '{name}' extra — install it with: "
            f"uv pip install -e '.[{name}]'   (or: pip install phase[{name}])"
        )


@dataclass
class Extra:
    name: str
    pip: str | None
    modules: tuple = ()
    binaries: tuple = ()
    features: tuple = ()
    kind: str = "pip"

    def __post_init__(self):
        # tolerate features=("single string") — missing trailing comma
        if isinstance(self.features, str):
            self.features = (self.features,)
        if isinstance(self.modules, str):
            self.modules = (self.modules,)
        if isinstance(self.binaries, str):
            self.binaries = (self.binaries,)

    def installed(self) -> bool:
        mods_ok = all(importlib.util.find_spec(m) is not None for m in self.modules)
        bins_ok = all(shutil.which(b) is not None for b in self.binaries)
        return bool(self.modules) and mods_ok or bool(self.binaries) and bins_ok

    def missing(self) -> list[str]:
        return [m for m in self.modules if importlib.util.find_spec(m) is None] + [
            b for b in self.binaries if shutil.which(b) is None
        ]


REGISTRY: dict[str, Extra] = {
    "tui": Extra(
        "tui", "phase[tui]", modules=("textual", "rich"), kind="pip",
        features=("menu-driven terminal UI (phase / phase tui)",
                  "rich tables, spinners, status cards")),
    "notify": Extra(
        "notify", "phase[notify]", modules=("apprise",), kind="pip",
        features=("push notifications on long operations "
                  "(provision, template, backup, engine jobs)")),
    "report": Extra(
        "report", "phase[report]", modules=("jinja2",), kind="pip",
        features=("HTML inventory/status report (phase report)")),
    "api": Extra(
        "api", "phase[api]", modules=("proxmoxer",), kind="pip",
        features=("Proxmox REST API transport (falls back to qm CLI)")),
    "gum": Extra(
        "gum", None, binaries=("gum",), kind="binary",
        features=("legacy v3 UI — superseded by the tui extra")),
    "rclone": Extra(
        "rclone", None, binaries=("rclone",), kind="binary",
        features=("offsite backup copies (backup --to)")),
    "zstd": Extra(
        "zstd", None, binaries=("zstd",), kind="binary",
        features=("zstd backup compression")),
}

CORE_FEATURES = (
    "vm lifecycle: plan/create/provision/status/exec/service/logs/power/tags/firewall",
    "template pipeline: create → configure (ssh checkpoint) → minimize → save",
    "engine: scan (host/storage/gpu/mdev capabilities), doctor, daemon + systemd",
    "disks: id+role model (os/data), add/list/resize/detach",
    "ssh keys: config pool + ephemeral per-session keys",
    "ansible dynamic inventory, event log, plan library, snapshots, backups",
)


def require(name: str):
    """Import/verify an extra or raise MissingExtra. Returns the module."""
    extra = REGISTRY.get(name)
    if not extra:
        raise PhaseError(f"unknown extra: {name}")
    if not extra.installed():
        raise MissingExtra(name)
    if extra.modules:
        return importlib.import_module(extra.modules[0])
    return None


def installed_extras() -> list[str]:
    return [n for n, e in REGISTRY.items() if e.installed()]

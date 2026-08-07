"""Configuration loading (config.schema.json) and state directories."""

from __future__ import annotations

import json
import os
import shutil
import tempfile

from .util import PhaseError

CONFIG_ENV = "PHASE_CONFIG"
DEFAULT_PATHS = ["/etc/phase.json", "/root/labctl.config.json"]


class Config:
    def __init__(self, data: dict, path: str):
        self.data = data or {}
        self.path = path

    @classmethod
    def load(cls, explicit: str | None = None) -> "Config":
        path = None
        if explicit:
            path = explicit
        elif os.environ.get(CONFIG_ENV):
            path = os.environ[CONFIG_ENV]
        else:
            for p in DEFAULT_PATHS:
                if os.path.isfile(p):
                    path = p
                    break
        if not path:
            raise PhaseError(
                "config not found (set PHASE_CONFIG or create /etc/phase.json)"
            )
        if not os.path.isfile(path):
            raise PhaseError(f"config not readable: {path}")
        try:
            with open(path) as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise PhaseError(f"config is not valid JSON: {path}: {e}")
        return cls(data, path)

    @classmethod
    def load_optional(cls, explicit: str | None = None) -> "Config | None":
        try:
            return cls.load(explicit)
        except PhaseError:
            return None

    def get(self, dotted: str, default=None):
        node = self.data
        for key in dotted.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    # Write back with a backup — only used for small config edits (ssh-key pool).
    def save(self) -> None:
        backup = f"{self.path}.bak"
        try:
            shutil.copy2(self.path, backup)
        except OSError:
            pass
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".", prefix=".phase-")
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, indent=2)
            f.write("\n")
        os.replace(tmp, self.path)


# ---- state directories (env-overridable; /var/lib/phase on the host) ------

def state_dir() -> str:
    return os.environ.get("PHASE_STATE_DIR") or "/var/lib/phase"


def plan_dir() -> str:
    return os.environ.get("PHASE_PLAN_DIR") or os.path.join(state_dir(), "plans")


def template_dir() -> str:
    return os.environ.get("PHASE_TEMPLATE_DIR") or os.path.join(state_dir(), "templates")


def event_log_path() -> str:
    return os.path.join(state_dir(), "events.jsonl")


def engine_state_path() -> str:
    return os.path.join(state_dir(), "engine-state.json")


def engine_socket_path() -> str:
    return os.environ.get("PHASE_ENGINE_SOCKET") or "/run/phase-engine.sock"


def ensure_state_dirs() -> None:
    for d in (plan_dir(), template_dir(), state_dir()):
        os.makedirs(d, exist_ok=True)

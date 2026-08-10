"""Event log: JSONL trail of every phase mutation (Cloud Logging, local)."""

from __future__ import annotations

import json
import os
import time

from .config import event_log_path


def append_event(**fields) -> None:
    try:
        path = event_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "actor": os.environ.get("USER") or os.environ.get("LOGNAME") or "?",
            **fields,
        }
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass  # logging must never break the operation


def tail_events(n: int = 50, path: str | None = None) -> list[dict]:
    path = path or event_log_path()
    if not os.path.isfile(path):
        return []
    lines = open(path).read().splitlines()[-n:]
    out = []
    for l in lines:
        try:
            out.append(json.loads(l))
        except json.JSONDecodeError:
            pass
    return out

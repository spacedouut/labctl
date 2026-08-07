"""Notification hook (apprise) — best-effort, never blocks the operation."""

from __future__ import annotations

from .util import log, warn


def send(cfg, title: str, body: str = "") -> bool:
    if not cfg or not cfg.get("notify.enabled", False):
        return False
    urls = cfg.get("notify.urls") or []
    if not urls:
        return False
    try:
        import apprise
    except ImportError:
        warn("notifications enabled in config but apprise is missing — "
             "install with: uv pip install -e '.[notify]'")
        return False
    a = apprise.Apprise()
    for u in urls:
        a.add(u)
    try:
        a.notify(title=title, body=body)
        return True
    except Exception as e:  # pragma: no cover - provider errors vary
        warn(f"notification failed: {e}")
        return False

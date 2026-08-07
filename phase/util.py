"""Shared helpers: errors, IO, TTY detection, size/CIDR/tag/name handling."""

from __future__ import annotations

import ipaddress
import os
import re
import shlex
import subprocess
import sys

# Human-facing expected failure. Caught in cli.main -> clean exit + message.
class PhaseError(Exception):
    pass


def die(msg: str) -> "NoReturn":
    raise PhaseError(msg)


def error(msg: str) -> None:
    print(f"phase: {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"phase: {msg}", file=sys.stderr)


def log(msg: str, out=None) -> None:
    print(msg, file=out or sys.stdout)


# True when /dev/tty is usable (gum-style check; works inside $(...)/pipes).
def have_tty() -> bool:
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        return False
    os.close(fd)
    return True


def confirm(prompt: str, default: bool = False) -> bool:
    """Yes/no confirmation. TTY: rich prompt (if available) else plain input().
    Non-TTY: returns the default instead of hanging."""
    if not have_tty():
        return default
    try:
        from rich.prompt import Confirm  # tui extra

        return Confirm.ask(f"{prompt}?", default=default)
    except ImportError:
        suffix = " [Y/n] " if default else " [y/N] "
        ans = input(prompt + suffix).strip().lower()
        if not ans:
            return default
        return ans in ("y", "yes")


def run(
    cmd: list[str],
    *,
    input: str | None = None,
    check: bool = True,
    env: dict | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd,
        input=input,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
    )
    if check and proc.returncode != 0:
        shown = " ".join(shlex.quote(c) for c in cmd)
        detail = (proc.stderr or proc.stdout or "").strip()
        raise PhaseError(f"command failed ({proc.returncode}): {shown}\n{detail}")
    return proc


def quoted(args: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in args)


# ---- sizes ----------------------------------------------------------------

_MULT = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


def to_bytes(s: str) -> int:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kKmMgGtT]?)", s.strip())
    if not m:
        die(f"invalid size: {s}")
    return int(float(m.group(1)) * _MULT.get(m.group(2).lower(), 1))


def human_bytes(n: int) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}T"


# ---- ip / cidr -------------------------------------------------------------

def ip_in_cidr(ip: str, cidr: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return ip == cidr


# ---- tags ------------------------------------------------------------------

_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


def validate_tag(tag: str) -> None:
    if not _TAG_RE.fullmatch(tag):
        die(f"invalid tag: {tag}")


def normalize_tags(*parts: str) -> str:
    seen: list[str] = []
    for part in parts:
        for t in re.split(r"[;\n]", part or ""):
            t = t.strip()
            if t and t not in seen:
                seen.append(t)
    return ";".join(seen)


# ---- names -----------------------------------------------------------------

def validate_name(cfg, name: str) -> None:
    pattern = cfg.get("naming.pattern", "^[a-z][a-z0-9-]*$")
    max_len = cfg.get("naming.max_length", 63)
    try:
        ok = re.fullmatch(pattern, name) is not None
    except re.error as e:
        die(f"config naming.pattern is not a valid regex: {e}")
    if not ok:
        die(f"invalid name: {name} (must match {pattern})")
    if len(name) > max_len:
        die(f"name too long: {name} (max {max_len})")


def is_disk_id(s: str) -> bool:
    return re.fullmatch(r"(scsi|sata|virtio|ide)\d+", s) is not None


def parse_disk_spec(spec: str) -> dict:
    """--disk spec: <id>:<role>[:<os>]:<size>[:<storage>]"""
    parts = spec.split(":")
    if len(parts) < 2:
        die(f"invalid --disk spec: {spec} (want id:role[:os]:size[:storage])")
    d = {
        "id": parts[0],
        "role": parts[1],
        "os": parts[2] if len(parts) > 2 else "",
        "size": parts[3] if len(parts) > 3 else "",
        "storage": parts[4] if len(parts) > 4 else "",
    }
    if not is_disk_id(d["id"]):
        die(f"invalid disk id: {d['id']} (want scsiN|sataN|virtioN|ideN)")
    if d["role"] not in ("os", "data"):
        die(f"disk role must be 'os' or 'data': {d['role']}")
    if d["role"] == "os" and not d["os"]:
        die("os disk needs an OS: --disk <id>:os:<os>:<size>[:<storage>]")
    return d


def shell_quote(args: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in args)

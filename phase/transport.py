"""Transport: where phase commands run.

phase normally runs ON the PVE host (local transport). With PHASE_HOST or
--host it can run anywhere and tunnel every qm/pvesh call over SSH — read-only
ops work great remotely; interactive qm terminal does not.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess

from .util import PhaseError


class Transport:
    def run(self, args: list[str], *, input: str | None = None,
            check: bool = True) -> subprocess.CompletedProcess:
        raise NotImplementedError

    def which(self, name: str) -> str | None:
        raise NotImplementedError

    def run_ok(self, args: list[str]) -> bool:
        return self.run(args, check=False).returncode == 0


class LocalTransport(Transport):
    def run(self, args, *, input=None, check=True):
        proc = subprocess.run(args, input=input, capture_output=True, text=True)
        if check and proc.returncode != 0:
            shown = " ".join(shlex.quote(a) for a in args)
            detail = (proc.stderr or proc.stdout or "").strip()
            raise PhaseError(f"command failed ({proc.returncode}): {shown}\n{detail}")
        return proc

    def which(self, name: str) -> str | None:
        return shutil.which(name)


class SSHTransport(Transport):
    """Tunnels commands to a remote host (e.g. root@homelab)."""

    def __init__(self, host: str):
        self.host = host

    def run(self, args, *, input=None, check=True):
        remote = " ".join(shlex.quote(a) for a in args)
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", self.host, remote],
            input=input, capture_output=True, text=True,
        )
        if check and proc.returncode != 0:
            shown = " ".join(shlex.quote(a) for a in args)
            detail = (proc.stderr or proc.stdout or "").strip()
            raise PhaseError(
                f"remote command failed ({proc.returncode}) on {self.host}: {shown}\n{detail}"
            )
        return proc

    def which(self, name: str) -> str | None:
        p = self.run(["command", "-v", name], check=False)
        return p.stdout.strip() if p.returncode == 0 else None


def make_transport(host: str | None = None) -> Transport:
    host = host or os.environ.get("PHASE_HOST") or None
    return SSHTransport(host) if host else LocalTransport()

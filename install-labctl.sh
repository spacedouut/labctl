#!/usr/bin/env bash
set -euo pipefail

# labctl v1 installer — now a migration shim.
#
# labctl was rewritten as `phase` (the v3 branch, now the default branch).
# Existing labctl installs reach this script via `labctl update` (the
# wrapper re-runs it after every git pull). It switches the /opt/labctl
# checkout to v3 and hands off to the phase installer, which installs
# /usr/local/bin/phase and preserves any existing config.

PREFIX="${PREFIX:-/usr/local}"
WORK_DIR="${WORK_DIR:-/opt/labctl}"

require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "installer must run as root" >&2
    exit 1
  fi
}

main() {
  require_root
  command -v git >/dev/null || { echo "missing git; install git first" >&2; exit 1; }

  if [[ ! -d "$WORK_DIR/.git" ]]; then
    echo "no checkout at $WORK_DIR; run the phase installer directly:" >&2
    echo "  curl -fsSL https://raw.githubusercontent.com/spacedouut/phase/refs/heads/v3/installer.sh | bash" >&2
    exit 1
  fi

  echo "labctl: migrating checkout to phase v3..." >&2
  git -C "$WORK_DIR" fetch origin
  git -C "$WORK_DIR" checkout -B v3 origin/v3

  # Hand off to the v3 installer (INSTALL_CONFIG carries through the env).
  exec bash "$WORK_DIR/installer.sh" "$@"
}

main "$@"

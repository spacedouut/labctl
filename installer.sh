#!/usr/bin/env bash
set -euo pipefail

# phase installer.
#
# Layout: the repo is checked out at WORK_DIR (/opt/phase) and a thin wrapper
# at BIN_DIR/phase execs phase.sh from there. Updating is just `git pull` in
# WORK_DIR — no reinstall needed for script changes.

PREFIX="${PREFIX:-/usr/local}"
BIN_DIR="${BIN_DIR:-$PREFIX/bin}"
CONFIG_FILE="${CONFIG_FILE:-/etc/phase.json}"
REPO_URL="${REPO_URL:-https://github.com/spacedouut/phase.git}"
REF="${REF:-v2}"
WORK_DIR="${WORK_DIR:-/opt/phase}"
INSTALL_WRAPPER="${INSTALL_WRAPPER:-1}"
INSTALL_CONFIG="${INSTALL_CONFIG:-1}"

if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
else
  SOURCE_DIR=""
fi

require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "installer must run as root" >&2
    exit 1
  fi
}

main() {
  require_root

  command -v bash >/dev/null || { echo "Missing bash (..how?)" >&2; exit 1; }
  command -v jq >/dev/null   || { echo "Missing jq! Install with: apt install jq" >&2; exit 1; }
  command -v qm >/dev/null   || { echo "Missing qm! (run this on a Proxmox host!)" >&2; exit 1; }
  command -v git >/dev/null  || { echo "Missing git! Install with: apt install git" >&2; exit 1; }
  command -v gum >/dev/null  || echo "Warning: gum not found; phase needs it for interactive prompts and spinners." >&2

  # Ensure the checkout exists at WORK_DIR. If we're running from inside a
  # checkout that already lives there, use it; otherwise clone/refresh.
  if [[ "$SOURCE_DIR" == "$WORK_DIR" && -d "$WORK_DIR/.git" ]]; then
    echo "Using existing checkout: $WORK_DIR"
  elif [[ -d "$WORK_DIR/.git" ]]; then
    echo "Updating existing checkout: $WORK_DIR"
    git -C "$WORK_DIR" pull --ff-only
  else
    echo "Cloning $REPO_URL ($REF) into $WORK_DIR"
    rm -rf "$WORK_DIR"
    git clone --branch "$REF" "$REPO_URL" "$WORK_DIR"
  fi

  for f in phase.sh tools.sh cmds.sh; do
    [[ -f "$WORK_DIR/$f" ]] || { echo "missing $f in $WORK_DIR" >&2; exit 1; }
  done
  chmod +x "$WORK_DIR/phase.sh"

  if [[ "$INSTALL_WRAPPER" == "1" ]]; then
    install -d "$BIN_DIR"
    cat >"$BIN_DIR/phase" <<EOF
#!/usr/bin/env bash
exec "$WORK_DIR/phase.sh" "\$@"
EOF
    chmod 0755 "$BIN_DIR/phase"
    echo "Installed wrapper: $BIN_DIR/phase -> $WORK_DIR/phase.sh"
  fi

  if [[ "$INSTALL_CONFIG" == "1" ]]; then
    if [[ -f "$CONFIG_FILE" ]]; then
      echo "Keeping existing config: $CONFIG_FILE"
    elif [[ -f "$WORK_DIR/labctl.config.json" ]]; then
      install -D -m 0644 "$WORK_DIR/labctl.config.json" "$CONFIG_FILE"
      echo "Installed default config: $CONFIG_FILE"
    else
      echo "No default config found in $WORK_DIR; skipping config install" >&2
    fi
  fi

  echo
  echo "Try:"
  echo "  phase vm plan --name redis --size micro --os ubuntu-26-lts"
  echo "  phase vm create redis"
  echo
  echo "Update later with:  cd $WORK_DIR && git pull"
}

main "$@"

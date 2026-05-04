#!/usr/bin/env bash
set -euo pipefail

PREFIX="${PREFIX:-/usr/local}"
BIN_DIR="${BIN_DIR:-$PREFIX/bin}"
CONFIG_DIR="${CONFIG_DIR:-/etc/labctl}"
CONFIG_FILE="${CONFIG_FILE:-$CONFIG_DIR/config.json}"
REPO_URL="${REPO_URL:-https://github.com/spacedouut/labctl.git}"
REF="${REF:-main}"
WORK_DIR="${WORK_DIR:-/opt/labctl}"
LABCTL_FILE="${LABCTL_FILE:-labctl.sh}"
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
else
  SOURCE_DIR=""
fi

install_file() {
  local src="$1" dest="$2" mode="$3"
  install -D -m "$mode" "$src" "$dest"
}

require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "installer must run as root" >&2
    exit 1
  fi
}

main() {
  require_root

  command -v bash >/dev/null || { echo "missing bash" >&2; exit 1; }
  command -v jq >/dev/null || { echo "missing jq; install jq first" >&2; exit 1; }
  command -v qm >/dev/null || { echo "missing qm; run this on a Proxmox host" >&2; exit 1; }

  if [[ ! -f "$SOURCE_DIR/$LABCTL_FILE" ]]; then
    command -v git >/dev/null || { echo "missing git; install git first" >&2; exit 1; }
    if [[ -f "$WORK_DIR/$LABCTL_FILE" ]]; then
      echo "Using existing checkout: $WORK_DIR"
    else
      echo "Cloning $REPO_URL into $WORK_DIR"
      rm -rf "$WORK_DIR"
      git clone --depth 1 --branch "$REF" "$REPO_URL" "$WORK_DIR"
    fi
    SOURCE_DIR="$WORK_DIR"
  fi

  [[ -f "$SOURCE_DIR/$LABCTL_FILE" ]] || { echo "missing $LABCTL_FILE in $SOURCE_DIR" >&2; exit 1; }

  install_file "$SOURCE_DIR/$LABCTL_FILE" "$BIN_DIR/labctl" 0755

  if [[ -f "$CONFIG_FILE" ]]; then
    echo "Keeping existing config: $CONFIG_FILE"
  elif [[ -f "$SOURCE_DIR/labctl.config.json" ]]; then
    install_file "$SOURCE_DIR/labctl.config.json" "$CONFIG_FILE" 0644
    echo "Installed default config: $CONFIG_FILE"
  else
    echo "No labctl.config.json found next to installer; skipping config install" >&2
  fi

  echo "Installed: $BIN_DIR/labctl"
  echo
  echo "Try:"
  echo "  labctl templates list"
  echo "  labctl vm plan redis --env tmp --size micro --os ubuntu-26-lts"
}

main "$@"

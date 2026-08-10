#!/usr/bin/env bash
# Shared environment bootstrap for phase — used by BOTH the installer and
# local development, so venv logic lives in exactly one place.
#
# Usage:
#   scripts/phase-env.sh <venv-dir> [--extra NAME ...] [--python VER]
#
# Strategy: uv first (fast, self-managing), python3 -m venv fallback.
# Writes the created env's `bin/python` path to stdout on success.
set -euo pipefail

VENV_DIR="${1:?usage: phase-env.sh <venv-dir> [--extra NAME ...]}"
shift || true

EXTRAS=()
PYVER=""
while (($#)); do
  case "$1" in
    --extra) EXTRAS+=("${2:-}"); shift 2 ;;
    --python) PYVER="${2:-}"; shift 2 ;;
    *) echo "phase-env.sh: unknown option: $1" >&2; exit 2 ;;
  esac
done

# repo root = dirname of this script's parent
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

find_uv() {
  command -v uv >/dev/null 2>&1 && { echo uv; return; }
  [[ -x "$HOME/.local/bin/uv" ]] && { echo "$HOME/.local/bin/uv"; return; }
  return 1
}

UV_BIN="$(find_uv || true)"

if [[ -n "$UV_BIN" ]]; then
  echo "phase-env: using uv ($UV_BIN)" >&2
  mkdir -p "$VENV_DIR"
  PY_ARGS=()
  [[ -n "$PYVER" ]] && PY_ARGS+=(--python "$PYVER")
  "$UV_BIN" venv "$VENV_DIR" "${PY_ARGS[@]}" --clear
  INSTALL_ARGS=(--python "$VENV_DIR/bin/python")
  if ((${#EXTRAS[@]})); then
    INSTALL_ARGS+=("-e" ".[$(IFS=,; echo "${EXTRAS[*]}")]")
  else
    INSTALL_ARGS+=("-e" ".")
  fi
  "$UV_BIN" pip install "${INSTALL_ARGS[@]}"
else
  echo "phase-env: uv not found, falling back to python3 -m venv" >&2
  command -v python3 >/dev/null 2>&1 || { echo "phase-env: python3 not found" >&2; exit 1; }
  python3 -m venv "$VENV_DIR" 2>/dev/null || {
    echo "phase-env: python3-venv missing — install it (apt install python3-venv) or install uv" >&2
    exit 1
  }
  EXTRA_FLAG=""
  ((${#EXTRAS[@]})) && EXTRA_FLAG="[$(IFS=,; echo "${EXTRAS[*]}")]"
  "$VENV_DIR/bin/pip" install -q -e ".$EXTRA_FLAG"
fi

echo "$VENV_DIR/bin/python"

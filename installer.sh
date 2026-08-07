#!/usr/bin/env bash
# phase v4 installer — runs on the PVE host (or anywhere).
#
#   bash installer.sh [--extra tui] [--extra notify] [--engine] [--no-start]
#
# Installs to /opt/phase (git checkout of v4), creates the venv through the
# shared scripts/phase-env.sh, symlinks /usr/local/bin/phase, and records the
# chosen extras in /var/lib/phase/install.json so `phase update` reinstalls
# the same set. Config (/etc/phase.json) is never touched.
set -euo pipefail

REPO_DIR="${PHASE_DIR:-/opt/phase}"
BRANCH="v4"
STATE_DIR="${PHASE_STATE_DIR:-/var/lib/phase}"
BIN_DIR="/usr/local/bin"

EXTRAS=()
WITH_ENGINE=0
ENGINE_START=1

while (($#)); do
  case "$1" in
    --extra) EXTRAS+=("${2:-}"); shift 2 ;;
    --engine) WITH_ENGINE=1; shift ;;
    --no-start) ENGINE_START=0; shift ;;
    *) echo "installer: unknown option: $1" >&2; exit 2 ;;
  esac
done

need() { command -v "$1" >/dev/null 2>&1 || { echo "installer: missing required command: $1" >&2; exit 1; }; }
need git

echo "==> fetching phase $BRANCH into $REPO_DIR"
if [[ -d "$REPO_DIR/.git" ]]; then
  git -C "$REPO_DIR" fetch origin "$BRANCH"
  git -C "$REPO_DIR" checkout -B "$BRANCH" "origin/$BRANCH"
else
  mkdir -p "$(dirname "$REPO_DIR")"
  git clone --branch "$BRANCH" https://github.com/spacedouut/phase.git "$REPO_DIR"
fi

echo "==> creating venv (shared logic: scripts/phase-env.sh)"
PY="$(bash "$REPO_DIR/scripts/phase-env.sh" "$REPO_DIR/.venv" \
  $(for e in "${EXTRAS[@]:-}"; do printf -- '--extra %s ' "$e"; done))"

echo "==> installing wrapper $BIN_DIR/phase"
cat > "$BIN_DIR/phase" <<EOF
#!/bin/sh
exec "$PY" -m phase "\$@"
EOF
chmod +x "$BIN_DIR/phase"

echo "==> recording install metadata"
mkdir -p "$STATE_DIR"
cat > "$STATE_DIR/install.json" <<EOF
{"branch": "$BRANCH", "extras": [$(IFS=,; printf '"%s"' "${EXTRAS[*]}")], "engine": $WITH_ENGINE}
EOF

if ((WITH_ENGINE)); then
  echo "==> installing engine systemd service"
  if ((ENGINE_START)); then
    "$BIN_DIR/phase" engine install
  else
    "$BIN_DIR/phase" engine install --no-start
  fi
fi

echo "==> done. try: phase vm list"

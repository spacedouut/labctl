#!/usr/bin/env bash
set -euo pipefail

PHASE_DIR="${PHASE_DIR:-/opt/phase}"
source "$PHASE_DIR/tools.sh"
source "$PHASE_DIR/cmds.sh"


# Environment Variables


cmd_templates() {
  require_config
  local f id name template_flag
  printf '%-8s %s\n' 'VMID' 'Name'
  printf '%-8s %s\n' '--------' '--------'
  for f in "$QEMU_DIR"/*.conf; do
    [[ -e "$f" ]] || continue
    id="${f##*/}"; id="${id%.conf}"
    name="$(vm_name "$id")"
    [[ "$name" == tpl-* ]] || continue
    template_flag="$(template_field "$id" template)"
    [[ "$template_flag" == "1" ]] || continue
    printf '%-8s %s\n' "$id" "$name"
  done | sort -n
}

cmd_update() {
  local repo_dir="/opt/phase"
  [[ -d "$repo_dir/.git" ]] || die "update requires a git checkout at $repo_dir"
  need git

  gum spin --show-error --title "Updating phase..." -- \
    git -C "$repo_dir" pull --ff-only

  # Re-run installer to refresh the symlink; config is preserved.
  if [[ -f "$repo_dir/installer.sh" ]]; then
    INSTALL_CONFIG=0 bash "$repo_dir/installer.sh"
  fi
}


usage() {
  cat <<'EOF'
  phase - compute-engine style management for PVE.

  Example usage:

  # Plan, then create (plan-first workflow):
  phase vm plan --size micro --name postgres --os ubuntu-26 --save
  phase vm create postgres --vmidout   # create from saved plan, print VMID

  # Per-VM operations (noun-first: name comes right after `vm`):
  phase vm postgres                    # interactive SSH (default action)
  phase vm postgres shell              # explicit SSH (alias: ssh)
  phase vm postgres service nginx restart
  phase vm postgres service sherpa-stt-gpu status --user   # user units
  phase vm postgres logs --unit nginx -n 50 --follow
  phase vm postgres exec -- uptime
  phase vm postgres status
  phase vm postgres start|stop|reboot|shutdown
  phase vm postgres tag add db
  phase vm postgres firewall add --from lan --port 6379
  phase vm postgres rename postgresql
  phase vm postgres destroy            # type the name to confirm

  # Legacy verb-first form still works:
  phase vm shell postgres
  phase vm restart postgres

  Commands:
    update, templates, vm

  VM actions:
    plan, create, provision, nextid   (verb-first: phase vm <action> ...)
    shell, ssh, exec, service, logs, status, start, stop, reboot, reset,
    shutdown, pause, tag, firewall, rename, destroy, bootstrap
                                    (noun-first: phase vm <name> <action>)

  Configure from /etc/phase.json
EOF
}

main() {
  local cmd="${1:-}"; shift || true
  case "$cmd" in
    vm) 
      require_config
      cmd_vm "$@"
      ;;
    update) cmd_update ;;
    templates) cmd_templates ;;
    help) usage ;;
    *) usage; die "Unknown command: $cmd" ;;
  esac
}

main "$@"

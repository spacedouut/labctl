#!/usr/bin/env bash
set -euo pipefail

PHASE_DIR="/opt/phase"
source "$PHASE_DIR/tools.sh"
source "$PHASE_DIR/cmds.sh"


# Environment Variables


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
  
  phase vm plan --size micro --name postgres --disk 128GB local-lvm --os ubuntu-26-lts --save
  phase vm create planned # create from temporarily saved plan
  phase vm create postgres --vmidout # create, print only the VMID
  phase vm start postgres
  # Wait a moment...
  phase vm shell postgres --serial


  Full command list:
    update, templates, vm 

  VM command list:
    plan, provision, bootstrap, create, start, 
    stop, reboot, reset, shutdown, shell, 
    firewall, tag, rename, destroy, status, nextid

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
    help) usage ;;
    *) usage; die "Unknown command: $cmd" ;;
  esac
}

main "$@"

#!/usr/bin/env bash
set -euo pipefail

PHASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$PHASE_DIR/tools.sh"
source "$PHASE_DIR/cmds.sh"


# Environment Variables


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
    stop, reboot, reset, shutdown, connect, 
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
    help) usage ;;
    *) usage; die "Unknown command: $cmd" ;;
  esac
}

main "$@"

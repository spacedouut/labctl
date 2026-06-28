
cmd_vm() {
  local sub="${1:-}"; shift || true
    case "$sub" in
    plan) cmd_vm_plan "$@" ;;
    create) cmd_vm_create "$@" ;;
    provision) cmd_vm_provision "$@" ;;
    connect) cmd_vm_connect "$@" ;;
    bootstrap) cmd_vm_bootstrap "$@" ;;
    start|stop|reboot|reset|shutdown|pause) cmd_vm_power "$sub" "$@" ;;
    firewall)
      local fw_sub="${1:-}"; shift || true
      case "$fw_sub" in
        add) cmd_vm_firewall_add "$@" ;;
        *) usage; die "unknown vm firewall command: $fw_sub" ;;
      esac
      ;;
    tag)
      local tag_sub="${1:-}"; shift || true
      case "$tag_sub" in
        list) cmd_vm_tag_list "$@" ;;
        add) cmd_vm_tag_add "$@" ;;
        remove|rm) cmd_vm_tag_remove "$@" ;;
        set) cmd_vm_tag_set "$@" ;;
        *) usage; die "unknown vm tag command: $tag_sub" ;;
      esac
      ;;
    rename) cmd_vm_rename "$@" ;;
    destroy) cmd_vm_destroy "$@" ;;
    *) usage; die "unknown vm command: $sub" ;;
  esac
}

# VM Commands

# Pretty-print a plan JSON (from stdin) for review.
print_plan() {
  local plan="$1" cores mem
  read -r cores mem < <(resolve_size "$(jq -r '.size' <<<"$plan")")
  cat <<EOF
Will create:
  Name:     $(jq -r '.name' <<<"$plan")
  Size:     $(jq -r '.size' <<<"$plan") (${cores} vCPU, ${mem} MB)
  OS:       $(jq -r '.os' <<<"$plan")
  Disk:     $(jq -r '.disk.size // "template default"' <<<"$plan") on $(jq -r '.disk.storage // "template default"' <<<"$plan")
  Network:  bridge $(jq -r '.net.bridge // "template default"' <<<"$plan"), vlan $(jq -r '.net.vlan // "none"' <<<"$plan")
  IP:       $(jq -r '.ipconfig // "template default"' <<<"$plan")
  On boot:  $(jq -r 'if .onboot then "yes" else "no" end' <<<"$plan")
  Protected: $(jq -r 'if .protection then "yes" else "no" end' <<<"$plan")
  Tags:     $(jq -r 'if (.tags | length) > 0 then (.tags | join(",")) else "none" end' <<<"$plan")
  SSH keys: $(jq -r 'if (.ssh_keys // [] | length) > 0 then (.ssh_keys | join(",")) else "config + template defaults" end' <<<"$plan")
  Bootstrap: $(jq -r '[.bootstrap | to_entries[] | select(.value) | .key] | if length > 0 then join(",") else "none" end' <<<"$plan")
  Description: $(jq -r '.description // "none"' <<<"$plan")
  VMID:     allocated at create time
EOF
}

# Offer a gum chooser when a value is missing; flags always win over prompts.
choose_or_die() {
  local prompt="$1"; shift
  (($#)) || die "$prompt: no options available"
  gum_or_abort choose --header "$prompt" "$@"
}

# Build a validated plan JSON from flags, running a full gum wizard for any
# unset field when a TTY is available (flags always win). Emits JSON on stdout.
gather_plan() {
  local name="" size="" os="" disk_size="" disk_store=""
  local bridge="" vlan="" ipcfg="" gw="" description="" protect=""
  local onboot="1"
  local -a tags=() boot=() keys=()
  while (($#)); do
    case "$1" in
      --name) name="${2:-}"; shift 2 ;;
      --size) size="${2:-}"; shift 2 ;;
      --os) os="${2:-}"; shift 2 ;;
      --disk) disk_size="${2:-}"; disk_store="${3:-}"; shift 3 ;;
      --bridge) bridge="${2:-}"; shift 2 ;;
      --vlan) vlan="${2:-}"; shift 2 ;;
      --ip) ipcfg="${2:-}"; shift 2 ;;
      --gw) gw="${2:-}"; shift 2 ;;
      --description) description="${2:-}"; shift 2 ;;
      --protect) protect="1"; shift ;;
      --no-onboot) onboot="0"; shift ;;
      --tag) tags+=("${2:-}"); shift 2 ;;
      --ssh-key) keys+=("${2:-}"); shift 2 ;;
      --system) boot+=("system"); shift ;;
      --docker) boot+=("docker"); shift ;;
      --tailscale) boot+=("tailscale"); shift ;;
      *) die "unknown option: $1" ;;
    esac
  done

  # Required fields need a terminal if unset.
  if [[ -z "$name" || -z "$size" || -z "$os" ]]; then
    require_tty "missing required fields (name/size/os)"
  fi
  [[ -n "$name" ]] || name="$(gum_or_abort input --header "VM name" --placeholder "postgres")"
  [[ -n "$size" ]] || size="$(choose_or_die "Size" $(jq -r '.templates.sizes | keys[]' "$CONFIG_FILE"))"
  if [[ -z "$os" ]]; then
    local -a tpls=()
    mapfile -t tpls < <(list_template_oses)
    os="$(choose_or_die "OS template" "${tpls[@]}")"
  fi

  # Full wizard for the rest — only when interactive AND not already set by flags.
  if have_tty; then
    if [[ -z "$disk_size" ]]; then
      disk_size="$(gum_or_abort input --header "Disk size (blank = template default)" --placeholder "32G")"
      if [[ -n "$disk_size" && -z "$disk_store" ]]; then
        local -a stores=(); mapfile -t stores < <(list_storages)
        ((${#stores[@]})) && disk_store="$(gum_or_abort choose --header "Disk storage" "${stores[@]}")"
      fi
    fi
    if [[ -z "$bridge" ]]; then
      local -a brs=(); mapfile -t brs < <(list_bridges)
      ((${#brs[@]})) && bridge="$(gum_or_abort choose --header "Network bridge (Esc = template default)" "${brs[@]}" || true)"
    fi
    [[ -n "$vlan" ]] || vlan="$(gum_or_abort input --header "VLAN tag (blank = none)" --placeholder "")"
    if [[ -z "$ipcfg" ]]; then
      local mode; mode="$(gum_or_abort choose --header "IP config" "dhcp" "static")"
      if [[ "$mode" == "static" ]]; then
        ipcfg="$(gum_or_abort input --header "Static IP (CIDR, e.g. 10.0.0.5/24)")"
        [[ -n "$gw" ]] || gw="$(gum_or_abort input --header "Gateway (e.g. 10.0.0.1)")"
      else
        ipcfg="dhcp"
      fi
    fi
    gum_or_abort confirm "Start on boot?" && onboot="1" || onboot="0"
    [[ -n "$description" ]] || description="$(gum_or_abort input --header "Description (blank = none)")"
    [[ -n "$protect" ]] || { gum_or_abort confirm --default=no "Protect from deletion?" && protect="1" || protect="0"; }
  fi
  [[ -n "$protect" ]] || protect="0"

  # Validate before emitting — no PVE mutation happens here.
  validate_name "$name"
  resolve_size "$size" >/dev/null
  resolve_template "$size" "$os" >/dev/null

  jq -n \
    --arg name "$name" --arg size "$size" --arg os "$os" \
    --arg dsize "$disk_size" --arg dstore "$disk_store" \
    --arg bridge "$bridge" --arg vlan "$vlan" \
    --arg ipcfg "$ipcfg" --arg gw "$gw" \
    --arg desc "$description" \
    --argjson onboot "$onboot" --argjson protect "$protect" \
    --argjson tags "$(printf '%s\n' "${tags[@]:-}" | jq -R . | jq -s 'map(select(length > 0))')" \
    --argjson keys "$(printf '%s\n' "${keys[@]:-}" | jq -R . | jq -s 'map(select(length > 0))')" \
    --argjson boot "$(printf '%s\n' "${boot[@]:-}" | jq -R . | jq -s 'map(select(length > 0))')" \
    '{
      name: $name,
      size: $size,
      os: $os,
      disk: (if $dsize != "" then {size: $dsize, storage: (if $dstore != "" then $dstore else null end)} else null end),
      net: {
        bridge: (if $bridge != "" then $bridge else null end),
        vlan: (if $vlan != "" then $vlan else null end)
      },
      ipconfig: (if $ipcfg != "" then (if $gw != "" then "ip=\($ipcfg),gw=\($gw)" elif $ipcfg == "dhcp" then "ip=dhcp" else "ip=\($ipcfg)" end) else null end),
      onboot: ($onboot == 1),
      description: (if $desc != "" then $desc else null end),
      protection: ($protect == 1),
      tags: $tags,
      ssh_keys: $keys,
      bootstrap: {
        system: ($boot | index("system") != null),
        docker: ($boot | index("docker") != null),
        tailscale: ($boot | index("tailscale") != null)
      },
      vmid: 0
    }'
}

cmd_vm_plan() {
  local save=0
  local -a rest=()
  while (($#)); do
    case "$1" in
      --save) save=1; shift ;;
      *) rest+=("$1"); shift ;;
    esac
  done

  local plan
  plan="$(gather_plan "${rest[@]}")"
  print_plan "$plan"

  local name
  name="$(jq -r '.name' <<<"$plan")"
  if ((save)) || confirm "Save this plan?"; then
    write_plan "$name" <<<"$plan"
    log "Saved plan: $(plan_path "$name")"
  fi
}

cmd_vm_provision() {
  local vmid name
  vmid="$(cmd_vm_create --vmidout "$@")"
  name="$(vm_name "$vmid")"
  gum spin --title "Starting ${name}..." -- qm start "$vmid"
  log "Waiting for guest agent on ${name}..."
  local ok=0
  for _ in {1..60}; do
    if qm guest cmd "$vmid" ping >/dev/null 2>&1; then ok=1; break; fi
    sleep 2
  done
  ((ok)) || die "guest agent never came up on ${name}"
  cmd_vm_bootstrap "$name"
}

# Clone + configure a VM from a plan JSON (passed as $1). Allocates the VMID
# at this point, stamps it back into any saved plan, and echoes it on stdout.
realize_plan() {
  local plan="$1"
  local name size os cores mem
  name="$(jq -r '.name' <<<"$plan")"
  size="$(jq -r '.size' <<<"$plan")"
  os="$(jq -r '.os' <<<"$plan")"

  # Re-validate: config/templates may have changed since the plan was written.
  validate_name "$name"
  read -r cores mem < <(resolve_size "$size")
  local template vmid agent
  template="$(resolve_template "$size" "$os")"
  vmid="$(next_id)"
  agent=0
  [[ "$(json '.vm.agent // true')" == "true" ]] && agent=1

  # Confirmation gate — interactive only. Non-interactive (provision, --save
  # consumers, CI) proceeds, since the flags/plan ARE the expressed intent.
  print_plan "$plan" >&2
  if have_tty && ! gum_or_abort confirm "Create this VM?"; then
    die "aborted by user"
  fi

  # Arm rollback — if interrupted after this point, the partial VM gets destroyed.
  _PHASE_ROLLBACK_VMID="$vmid"

  # Clone, placing the disk on the requested storage when given.
  local disk_store
  disk_store="$(jq -r '.disk.storage // empty' <<<"$plan")"
  local -a clone_args=("$template" "$vmid" --name "$name" --full 1)
  [[ -n "$disk_store" ]] && clone_args+=(--storage "$disk_store")
  gum spin --show-error --title "Cloning ${name} (VMID ${vmid})..." -- \
    qm clone "${clone_args[@]}"

  # Core config: cpu/mem/onboot/agent/description/protection.
  local onboot protection description
  onboot="$(jq -r 'if .onboot then 1 else 0 end' <<<"$plan")"
  protection="$(jq -r 'if .protection then 1 else 0 end' <<<"$plan")"
  description="$(jq -r '.description // empty' <<<"$plan")"
  local -a set_args=(--cores "$cores" --memory "$mem" --onboot "$onboot" \
    --agent "enabled=${agent}" --protection "$protection")
  [[ -n "$description" ]] && set_args+=(--description "$description")
  gum spin --show-error --title "Configuring ${name}..." -- \
    qm set "$vmid" "${set_args[@]}"

  # Network: rebuild net0 if a bridge or vlan was specified.
  local net_bridge net_vlan
  net_bridge="$(jq -r '.net.bridge // empty' <<<"$plan")"
  net_vlan="$(jq -r '.net.vlan // empty' <<<"$plan")"
  if [[ -n "$net_bridge" || -n "$net_vlan" ]]; then
    [[ -n "$net_bridge" ]] || net_bridge="$(json '.default_bridge // "vmbr0"')"
    local net0="virtio,bridge=${net_bridge}"
    [[ -n "$net_vlan" ]] && net0+=",tag=${net_vlan}"
    qm set "$vmid" --net0 "$net0" >/dev/null
  fi

  # Cloud-init IP config.
  local ipconfig
  ipconfig="$(jq -r '.ipconfig // empty' <<<"$plan")"
  [[ -z "$ipconfig" ]] || qm set "$vmid" --ipconfig0 "$ipconfig" >/dev/null

  # Disk resize — qm resize only GROWS. Warn + skip if not larger than current.
  local disk_size
  disk_size="$(jq -r '.disk.size // empty' <<<"$plan")"
  if [[ -n "$disk_size" ]]; then
    local cur req
    cur="$(disk_bytes "$vmid")"
    req="$(to_bytes "$disk_size")"
    if [[ -n "$req" && -n "$cur" && "$req" -le "$cur" ]]; then
      warn "requested disk ${disk_size} is not larger than current template disk; skipping resize"
    else
      gum spin --show-error --title "Resizing disk to ${disk_size}..." -- \
        qm resize "$vmid" scsi0 "$disk_size"
    fi
  fi

  # Cloud-init SSH keys: merge inherited (from template) + config vm.ssh_keys +
  # any plan-specified keys, deduped. Only re-set if we actually have keys.
  local -a plan_keys=()
  mapfile -t plan_keys < <(jq -r '.ssh_keys[]? // empty' <<<"$plan")
  local keyfile
  keyfile="$(build_sshkeys_file "$vmid" "${plan_keys[@]}")"
  if [[ -n "$keyfile" ]]; then
    _PHASE_CLEANUP_FILES+=("$keyfile")
    qm set "$vmid" --sshkeys "$keyfile" >/dev/null
    rm -f "$keyfile"
  fi

  # Tags.
  local tag_text
  tag_text="$(jq -r '.tags | join(";")' <<<"$plan")"
  [[ -z "$tag_text" ]] || qm set "$vmid" --tags "$tag_text" >/dev/null

  # Stamp the realized VMID back into the saved plan, if it exists on disk.
  if [[ -f "$(plan_path "$name")" ]]; then
    jq --argjson vmid "$vmid" '.vmid = $vmid' "$(plan_path "$name")" \
      | write_plan "$name"
  fi

  # Success — disarm rollback so the trap won't destroy a completed VM.
  _PHASE_ROLLBACK_VMID=""

  # The one line on stdout: the VMID provision needs.
  printf '%s\n' "$vmid"
}

# create [<name>] [flags]
#   - saved plan named <name> (or "planned") exists -> consume it
#   - otherwise -> build from flags, gum-filling any missing required field
#   - missing fields with no TTY -> gather_plan dies
# create [<name>] [flags] [--vmidout]
#   - saved plan named <name> (or "planned") exists -> consume it
#   - otherwise -> build from flags, gum-filling any missing required field
#   - missing fields with no TTY -> gather_plan dies
#   --vmidout: print only the bare VMID (for scripting). Default: a success line.
cmd_vm_create() {
  local vmidout=0
  local -a args=()
  while (($#)); do
    case "$1" in
      --vmidout) vmidout=1; shift ;;
      *) args+=("$1"); shift ;;
    esac
  done
  set -- "${args[@]}"

  local first="${1:-}"
  local -a rest=()
  if [[ -n "$first" && "$first" != --* ]]; then
    shift
    rest=("$@")
  else
    first=""
    rest=("$@")
  fi

  local plan
  if [[ -n "$first" ]] && { [[ "$first" == "planned" ]] || [[ -f "$(plan_path "$first")" ]]; }; then
    plan="$(read_plan "$first")"
  elif [[ -n "$first" ]]; then
    plan="$(gather_plan --name "$first" "${rest[@]}")"
  else
    plan="$(gather_plan "${rest[@]}")"
  fi

  local vmid name
  vmid="$(realize_plan "$plan")"
  name="$(jq -r '.name' <<<"$plan")"

  if ((vmidout)); then
    printf '%s\n' "$vmid"
  else
    log "Created ${name} (VMID ${vmid}), stopped."
  fi
}

cmd_vm_bootstrap() {
  local name="${1:-}"; shift || true
  [[ -n "$name" ]] || die "VM name is required"
  local system=0 docker=0 tailscale=0 os=""
  while (($#)); do
    case "$1" in
      --system) system=1; shift ;;
      --docker) docker=1; shift ;;
      --tailscale) tailscale=1; shift ;;
      --os) os="${2:-}"; shift 2 ;;
      *) die "unknown option: $1" ;;
    esac
  done
  # Default to system-only if no step requested.
  (( system || docker || tailscale )) || system=1

  local vmid
  vmid="$(vmid_by_name "$name")"
  # OS drives which override script set we use; prefer the saved plan.
  if [[ -z "$os" && -f "$(plan_path "$name")" ]]; then
    os="$(jq -r '.os // empty' "$(plan_path "$name")")"
  fi
  [[ -n "$os" ]] || die "cannot determine OS for $name; pass --os"

  qga_exec "$vmid" true >/dev/null

  local step script
  for step in system docker tailscale; do
    case "$step" in
      system) ((system)) || continue ;;
      docker) ((docker)) || continue ;;
      tailscale) ((tailscale)) || continue ;;
    esac
    script="$(bootstrap_script "$os" "$step")"
    [[ -n "$script" ]] || { warn "no $step bootstrap script for $os; skipping"; continue; }
    log "Bootstrapping $step on ${name}..."
    qga_run_script "$vmid" "$script"
  done
}

cmd_vm_power() {
  local action="$1" name="${2:-}"
  [[ -n "$name" ]] || die "VM name is required"
  local vmid
  vmid="$(vmid_by_name "$name")"
  case "$action" in
    start) qm start "$vmid" ;;
    stop) qm stop "$vmid" ;;
    reboot) qm reboot "$vmid" ;;
    reset) qm reset "$vmid" ;;
    shutdown) qm shutdown "$vmid" ;;
    pause) qm suspend "$vmid" ;;
    *) die "unknown power action: $action" ;;
  esac
}

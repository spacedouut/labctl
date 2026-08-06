# shellcheck shell=bash
# phase vm dispatch — hybrid grammar:
#   verb-first (creation/queries): phase vm plan|create|provision|nextid ...
#   noun-first (per-VM ops):       phase vm <name> <action> ...
#   legacy verb-first still works: phase vm <action> <name> ...
#   bare name:                     phase vm <name>            -> shell

VERB_FIRST_ACTIONS="plan create provision nextid"
NAMED_ACTIONS="shell ssh connect exec service logs status start stop reboot reset shutdown pause tag firewall rename destroy bootstrap"

is_verb_first()  { [[ " $VERB_FIRST_ACTIONS " == *" $1 "* ]]; }
is_named_action(){ [[ " $NAMED_ACTIONS " == *" $1 "* ]]; }

cmd_vm() {
  local first="${1:-}"; shift || true

  if is_verb_first "$first"; then
    case "$first" in
      plan)      cmd_vm_plan "$@" ;;
      create)    cmd_vm_create "$@" ;;
      provision) cmd_vm_provision "$@" ;;
      nextid)    cmd_vm_nextid "$@" ;;
    esac
    return
  fi

  # legacy verb-first: phase vm <action> <name> ...
  if [[ -n "$first" ]] && is_named_action "$first"; then
    [[ -n "${1:-}" ]] || die "vm: missing VM name after '$first'"
    case "$first" in
      tag)      [[ -n "${2:-}" ]] || die "usage: phase vm tag <list|add|remove|set> <name> ..."
                cmd_vm_tag "${2}" "${1}" "${@:3}" ;;
      firewall) [[ -n "${2:-}" ]] || die "usage: phase vm firewall <add> <name> ..."
                cmd_vm_firewall "${2}" "${1}" "${@:3}" ;;
      *) cmd_vm_named "$1" "$first" "${@:2}" ;;
    esac
    return
  fi

  # noun-first: phase vm <name> <action> ...
  if [[ -n "$first" && -n "${1:-}" ]] && is_named_action "${1:-}"; then
    local name="$first" action="$1"
    shift || true   # drop the action token; $1 is guaranteed non-empty above
    cmd_vm_named "$name" "$action" "$@"
    return
  fi

  # bare name -> shell
  if [[ -n "$first" ]]; then
    if [[ -z "${1:-}" ]]; then
      cmd_vm_named "$first" shell
      return
    fi
    usage; die "unknown vm action: ${1:-}"
  fi

  usage; die "vm: missing VM name or command"
}

cmd_vm_named() {
  local name="${1:-}" action="${2:-}"
  shift 2 || true
  [[ -n "$name" && -n "$action" ]] || { usage; die "vm: name and action required"; }
  case "$action" in
    shell|ssh|connect) cmd_vm_connect "$name" "$@" ;;
    exec)   cmd_vm_exec "$name" "$@" ;;
    service) cmd_vm_service "$name" "$@" ;;
    logs)   cmd_vm_logs "$name" "$@" ;;
    status) cmd_vm_status "$name" ;;
    start|stop|reboot|reset|shutdown|pause) cmd_vm_power "$action" "$name" ;;
    bootstrap) cmd_vm_bootstrap "$name" "$@" ;;
    tag)     cmd_vm_tag "$name" "$@" ;;
    firewall) cmd_vm_firewall "$name" "$@" ;;
    rename)  cmd_vm_rename "$name" "${1:-}" ;;
    destroy) cmd_vm_destroy "$name" "$@" ;;
    *) usage; die "unknown vm action: $action" ;;
  esac
}

cmd_vm_tag() {
  local name="$1" sub="${2:-}"; shift 2 || true
  case "$sub" in
    list)    cmd_vm_tag_list "$name" ;;
    add)     cmd_vm_tag_add "$name" "${1:-}" ;;
    remove|rm) cmd_vm_tag_remove "$name" "${1:-}" ;;
    set)     cmd_vm_tag_set "$name" "$@" ;;
    *) usage; die "unknown vm tag command: $sub" ;;
  esac
}

cmd_vm_firewall() {
  local name="$1" sub="${2:-}"; shift 2 || true
  case "$sub" in
    add) cmd_vm_firewall_add "$name" "$@" ;;
    *) usage; die "unknown vm firewall command: $sub" ;;
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
  VMID:     $(jq -r 'if .vmid and .vmid != 0 then "\(.vmid)" else "allocated at create time" end' <<<"$plan")
EOF
}

# Offer a gum chooser when a value is missing; flags always win over prompts.
choose_or_die() {
  local prompt="$1"; shift
  (($#)) || die "$prompt: no options available"
  gum choose --header "$prompt" "$@"
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
  [[ -n "$name" ]] || name="$(gum input --header "VM name" --placeholder "postgres")"
  [[ -n "$size" ]] || {
    local -a size_opts=()
    mapfile -t size_opts < <(jq -r '.templates.sizes | keys[]' "$CONFIG_FILE")
    size="$(choose_or_die "Size" "${size_opts[@]}")"
  }
  if [[ -z "$os" ]]; then
    local -a tpls=()
    mapfile -t tpls < <(list_template_oses)
    os="$(choose_or_die "OS template" "${tpls[@]}")"
  fi

  # Full wizard for the rest — only when interactive AND not already set by flags.
  if have_tty; then
    if [[ -z "$disk_size" ]]; then
      disk_size="$(gum input --header "Disk size (blank = template default)" --placeholder "32G")"
      if [[ -n "$disk_size" && -z "$disk_store" ]]; then
        local -a stores=(); mapfile -t stores < <(list_storages)
        ((${#stores[@]})) && disk_store="$(gum choose --header "Disk storage" "${stores[@]}")"
      fi
    fi
    if [[ -z "$bridge" ]]; then
      local -a brs=(); mapfile -t brs < <(list_bridges)
      ((${#brs[@]})) && bridge="$(gum choose --header "Network bridge (Esc = template default)" "${brs[@]}" || true)"
    fi
    [[ -n "$vlan" ]] || vlan="$(gum input --header "VLAN tag (blank = none)" --placeholder "")"
    if [[ -z "$ipcfg" ]]; then
      local mode; mode="$(gum choose --header "IP config" "dhcp" "static")"
      if [[ "$mode" == "static" ]]; then
        ipcfg="$(gum input --header "Static IP (CIDR, e.g. 10.0.0.5/24)")"
        [[ -n "$gw" ]] || gw="$(gum input --header "Gateway (e.g. 10.0.0.1)")"
      else
        ipcfg="dhcp"
      fi
    fi
    gum confirm "Start on boot?" && onboot="1" || onboot="0"
    [[ -n "$description" ]] || description="$(gum input --header "Description (blank = none)")"
    [[ -n "$protect" ]] || { gum confirm --default=no "Protect from deletion?" && protect="1" || protect="0"; }
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
  gum spin --title "Starting ${name}..." -- qm start "$vmid" >&2
  log "Waiting for guest agent on ${name}..."
  local ok=0
  for _ in {1..60}; do
    if qm guest cmd "$vmid" ping >/dev/null 2>&1; then ok=1; break; fi
    sleep 2
  done
  ((ok)) || die "guest agent never came up on ${name}"

  # Guest may boot with no network (cloud-init/networkd-wait-online race) —
  # wait for a real IP BEFORE bootstrap starts, while the agent is still
  # free (qemu-ga serializes commands; an apt exec would block this check).
  log "Waiting for guest network on ${name}..."
  local net_ok=0
  for _ in {1..45}; do
    if [[ -n "$(guest_ipv4s "$vmid" | head -1)" ]]; then net_ok=1; break; fi
    sleep 2
  done
  ((net_ok)) || die "guest never got an IP on ${name} (networkd-wait-online stuck?)"

  # Forward --os to bootstrap (it drives the script set). With no saved plan
  # file, bootstrap has no other way to learn the OS.
  local os=""
  while (($#)); do
    case "$1" in
      --os) os="${2:-}"; shift 2 ;;
      *) shift ;;
    esac
  done
  cmd_vm_bootstrap "$name" ${os:+--os "$os"}
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
  # Stamp the real VMID into the plan so the confirmation print shows it.
  plan="$(jq --argjson vmid "$vmid" '.vmid = $vmid' <<<"$plan")"
  agent=0
  [[ "$(json '.vm.agent // true')" == "true" ]] && agent=1

  # Confirmation gate — interactive only. Non-interactive (provision, --save
  # consumers, CI) proceeds, since the flags/plan ARE the expressed intent.
  print_plan "$plan" >&2
  if have_tty && ! gum confirm "Create this VM?"; then
    die "aborted by user"
  fi

  # Clone, placing the disk on the requested storage when given.
  local disk_store
  disk_store="$(jq -r '.disk.storage // empty' <<<"$plan")"
  local -a clone_args=("$template" "$vmid" --name "$name" --full 1)
  [[ -n "$disk_store" ]] && clone_args+=(--storage "$disk_store")
  # Progress replay goes to stderr: stdout is reserved for the --vmidout contract.
  gum spin --show-error --title "Cloning ${name} (VMID ${vmid})..." -- \
    qm clone "${clone_args[@]}" >&2

  # Core config: cpu/mem/onboot/agent/description/protection.
  local onboot protection description
  onboot="$(jq -r 'if .onboot then 1 else 0 end' <<<"$plan")"
  protection="$(jq -r 'if .protection then 1 else 0 end' <<<"$plan")"
  description="$(jq -r '.description // empty' <<<"$plan")"
  local -a set_args=(--cores "$cores" --memory "$mem" --onboot "$onboot" \
    --agent "enabled=${agent}" --protection "$protection")
  [[ -n "$description" ]] && set_args+=(--description "$description")
  gum spin --show-error --title "Configuring ${name}..." -- \
    qm set "$vmid" "${set_args[@]}" >&2

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
        qm resize "$vmid" scsi0 "$disk_size" >&2
    fi
  fi

  # Cloud-init SSH keys: merge inherited (from template) + config vm.ssh_keys +
  # any plan-specified keys, deduped. Only re-set if we actually have keys.
  local -a plan_keys=()
  mapfile -t plan_keys < <(jq -r '.ssh_keys[]? // empty' <<<"$plan")
  local keyfile
  keyfile="$(build_sshkeys_file "$vmid" "${plan_keys[@]}")"
  if [[ -n "$keyfile" ]]; then
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

  # The one line on stdout: the VMID provision needs.
  printf '%s\n' "$vmid"
}

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

cmd_vm_connect() {
  local name="${1:-}"; shift || true
  [[ -n "$name" ]] || die "VM name is required"
  local serial=0 key_check=1 user="" ip="" command="" ssh_args=()
  while (($#)); do
    case "$1" in
      --serial) serial=1; shift ;;
      --user) user="${2:-}"; shift 2 ;;
      --ip) ip="${2:-}"; shift 2 ;;
      --no-key-check) key_check=0; shift ;;
      --command) command="${2:-}"; shift 2 ;;
      --) shift; ssh_args+=("$@"); break ;;
      *) die "unknown option: $1" ;;
    esac
  done
  # --serial is incompatible with any SSH-specific flag.
  if ((serial)); then
    [[ -z "$user" ]] || die "--user is incompatible with --serial"
    [[ -z "$ip" ]] || die "--ip is incompatible with --serial"
    ((key_check)) || die "--no-key-check is incompatible with --serial"
    [[ -z "$command" ]] || die "--command is incompatible with --serial"
    ((${#ssh_args[@]} == 0)) || die "SSH passthrough args are incompatible with --serial"
  fi
  local vmid
  vmid="$(vmid_by_name "$name")"
  if ((serial)); then
    exec qm terminal "$vmid"
  fi
  local status
  status="$(qm status "$vmid" | awk '{print $2}')"
  [[ "$status" == "running" ]] || die "$name is $status; start it or use --serial"
  [[ -n "$user" ]] || user="$(template_field "$vmid" ciuser)"
  [[ -n "$user" ]] || user="$(json '.default_user // "root"')"
  if [[ -z "$ip" ]]; then
    ip="$(best_guest_ip "$vmid" || true)"
  fi
  [[ -n "$ip" ]] || die "no guest-agent IPv4 found for $name; try --ip or --serial"
  if ((key_check)) && ! has_known_ssh_key "$vmid"; then
    warn "no local /root/.ssh/*.pub key found in cloud-init sshkeys for $name"
  fi
  if [[ -n "$command" ]]; then
    exec ssh "${ssh_args[@]}" "$user@$ip" "$command"
  fi
  exec ssh "${ssh_args[@]}" "$user@$ip"
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

# New noun-first commands

cmd_vm_nextid() {
  next_id
}

cmd_vm_status() {
  local name="$1" vmid state ip tags cores mem agent onboot
  vmid="$(vmid_by_name "$name")"
  state="$(qm status "$vmid" | awk '{print $2}')"
  ip="$(best_guest_ip "$vmid" || printf 'unknown')"
  tags="$(vm_tags "$vmid")"
  cores="$(template_field "$vmid" cores)"
  mem="$(template_field "$vmid" memory)"
  agent="$(template_field "$vmid" agent)"
  onboot="$(template_field "$vmid" onboot)"

  # Plain output when piped/scripted; gum card only on a real TTY.
  if ! have_tty; then
    printf 'Name:   %s\n' "$name"
    printf 'VMID:   %s\n' "$vmid"
    printf 'State:  %s\n' "$state"
    printf 'IP:     %s\n' "$ip"
    printf 'Tags:   %s\n' "${tags:-}"
    printf 'Cores:  %s\n' "$cores"
    printf 'Memory: %s MB\n' "$mem"
    printf 'Agent:  %s\n' "$agent"
    printf 'Onboot: %s\n' "$onboot"
    return
  fi

  local state_fg=1
  case "$state" in
    running) state_fg=2 ;;
    stopped) state_fg=3 ;;
  esac

  # State color goes in the title (gum applies it); gum style strips ANSI
  # from stdin text, so the body card is uniform.
  gum style --border rounded --padding "0 4" --align center --width 52 \
    "$(gum join --horizontal \
        "$(gum style --bold "$name")" \
        "$(gum style --faint "  ·  VMID $vmid")" \
        "$(gum style --foreground "$state_fg" --bold "  [$state]")")"
  printf '%-10s %s\n' \
    'IP' "$ip" \
    'Tags' "${tags:-none}" \
    'Cores' "$cores" \
    'Memory' "$mem MB" \
    'Agent' "$agent" \
    'Onboot' "$onboot" \
    | gum style --border rounded --padding "0 2" --width 52
}

# phase vm <name> service <unit> <action> [--user]
cmd_vm_service() {
  local name="$1" unit="${2:-}" action="${3:-}" arg user_units=0
  [[ -n "$unit" && -n "$action" ]] || die "usage: phase vm <name> service <unit> <start|stop|restart|reload|enable|disable|status|is-active|is-enabled> [--user]"
  shift 3 || true
  for arg in "$@"; do
    case "$arg" in
      --user) user_units=1 ;;
      *) die "unknown option: $arg" ;;
    esac
  done
  case "$action" in
    start|stop|restart|reload|enable|disable|status|is-active|is-enabled) ;;
    *) die "unknown service action: $action" ;;
  esac
  local user ip
  read -r user ip < <(vm_ssh_user_ip "$name")
  local -a sys=(systemctl --no-pager)
  ((user_units)) && sys+=(--user)
  exec ssh "$user@$ip" "${sys[@]}" "$action" "$unit"
}

# phase vm <name> logs [-u|--unit <unit>] [-n|--lines <n>] [-f|--follow]
cmd_vm_logs() {
  local name="$1"; shift
  local unit="" lines="50" follow=0
  while (($#)); do
    case "$1" in
      -u|--unit) unit="${2:-}"; shift 2 ;;
      -n|--lines) lines="${2:-}"; shift 2 ;;
      -f|--follow) follow=1; shift ;;
      *) die "unknown option: $1" ;;
    esac
  done
  local user ip
  read -r user ip < <(vm_ssh_user_ip "$name")
  local -a args=(journalctl --no-pager -n "$lines")
  [[ -n "$unit" ]] && args+=(-u "$unit")
  ((follow)) && args+=(-f)
  exec ssh "$user@$ip" "${args[@]}"
}

# phase vm <name> exec -- <command...>
cmd_vm_exec() {
  local name="$1"; shift
  [[ "${1:-}" == "--" ]] && shift
  (($#)) || die "usage: phase vm <name> exec -- <command...>"
  local user ip
  read -r user ip < <(vm_ssh_user_ip "$name")
  exec ssh "$user@$ip" "$@"
}

# Ported from labctl v1, adapted to v2/v3 config.

cmd_vm_firewall_add() {
  local name="$1"; shift
  local from="" port="" proto="tcp"
  while (($#)); do
    case "$1" in
      --from) from="${2:-}"; shift 2 ;;
      --port) port="${2:-}"; shift 2 ;;
      --proto) proto="${2:-}"; shift 2 ;;
      *) die "unknown option: $1" ;;
    esac
  done
  [[ -n "$from" && -n "$port" ]] || die "--from and --port are required"
  [[ "$proto" == "tcp" || "$proto" == "udp" ]] || die "--proto must be tcp or udp"
  local vmid cidrs cidr
  vmid="$(vmid_by_name "$name")"
  if jq -e --arg from "$from" '.networks[$from] != null' "$CONFIG_FILE" >/dev/null; then
    mapfile -t cidrs < <(jq -r --arg from "$from" '.networks[$from][]' "$CONFIG_FILE")
    ((${#cidrs[@]})) || die "network alias has no CIDRs configured: $from"
  else
    cidrs=("$from")
  fi
  for cidr in "${cidrs[@]}"; do
    qga_exec "$vmid" sudo ufw allow from "$cidr" to any port "$port" proto "$proto"
  done
}

cmd_vm_tag_list() {
  local name="$1" tags
  tags="$(vm_tags "$(vmid_by_name "$name")")"
  [[ -n "$tags" ]] || return 0
  tr ';' '\n' <<<"$tags"
}

cmd_vm_tag_add() {
  local name="$1" tag="${2:-}"
  [[ -n "$tag" ]] || die "tag is required"
  validate_tag "$tag"
  local vmid existing next
  vmid="$(vmid_by_name "$name")"
  existing="$(vm_tags "$vmid")"
  next="$(printf '%s\n%s\n' "${existing//;/ }" "$tag" | tr ' ' '\n' | normalize_tags)"
  set_tags "$vmid" "$next"
  log "$next"
}

cmd_vm_tag_remove() {
  local name="$1" tag="${2:-}"
  [[ -n "$tag" ]] || die "tag is required"
  validate_tag "$tag"
  local vmid existing next
  vmid="$(vmid_by_name "$name")"
  existing="$(vm_tags "$vmid")"
  next="$(tr ';' '\n' <<<"$existing" | awk -v tag="$tag" 'NF && $0 != tag { print }' | normalize_tags)"
  set_tags "$vmid" "$next"
  log "$next"
}

cmd_vm_tag_set() {
  local name="$1"; shift
  (($#)) || die "at least one tag is required"
  local vmid tag
  vmid="$(vmid_by_name "$name")"
  for tag in "$@"; do
    validate_tag "$tag"
  done
  local next
  next="$(printf '%s\n' "$@" | normalize_tags)"
  set_tags "$vmid" "$next"
  log "$next"
}

cmd_vm_rename() {
  local name="$1" new_name="${2:-}"
  [[ -n "$new_name" ]] || die "usage: phase vm <name> rename <new-name>"
  validate_name "$new_name"
  local vmid
  vmid="$(vmid_by_name "$name")"
  qm set "$vmid" --name "$new_name" >/dev/null
  log "$name -> $new_name (VMID $vmid)"
}

cmd_vm_destroy() {
  local name="$1"; shift || true
  local force=0
  while (($#)); do
    case "$1" in
      --force) force=1; shift ;;
      *) die "unknown option: $1" ;;
    esac
  done
  local vmid status tags confirm
  vmid="$(vmid_by_name "$name")"
  status="$(qm status "$vmid" | awk '{print $2}')"
  tags="$(vm_tags "$vmid")"
  cat >&2 <<EOF
Will destroy:
  Name: $name
  VMID: $vmid
  Status: $status
  Tags: ${tags:-none}
EOF
  if ((force)); then
    [[ "$name" =~ ^tmp- || "$name" =~ ^lab- ]] || die "--force is only allowed for tmp-* or lab-* VMs"
  else
    printf 'Type %s to permanently destroy this VM: ' "$name" >&2
    read -r confirm || true
    [[ "$confirm" == "$name" ]] || die "confirmation did not match; aborting"
  fi
  qm stop "$vmid" --skiplock 1 >/dev/null 2>&1 || true
  qm destroy "$vmid" --purge 1
}

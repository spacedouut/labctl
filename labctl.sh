#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${LABCTL_CONFIG:-}" ]]; then
  CONFIG_FILE="$LABCTL_CONFIG"
elif [[ -r /etc/labctl/config.json ]]; then
  CONFIG_FILE="/etc/labctl/config.json"
else
  CONFIG_FILE="/root/labctl.config.json"
fi
QEMU_DIR="/etc/pve/nodes/homelab/qemu-server"

die() {
  printf 'labctl: %s\n' "$*" >&2
  exit 1
}

need() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

usage() {
  cat <<'EOF'
Usage:
  Maintenance:
    labctl update

  Discovery:
    labctl ids next --env <env>
    labctl templates list
    labctl templates resolve --size <size> --os <os>

  Provisioning:
    labctl vm plan <service> --env <env> --size <size> --os <os> [--instance <n>] [--tag <tag> ...]
    labctl vm create <service> --env <env> --size <size> --os <os> [--instance <n>] [--tag <tag> ...] [--bootstrap] [--docker] [--tailscale]
    labctl vm bootstrap <vm-name> [--system] [--docker] [--tailscale]

  Lifecycle:
    labctl vm start <vm-name>
    labctl vm stop <vm-name>
    labctl vm reboot <vm-name>
    labctl vm reset <vm-name>
    labctl vm shutdown <vm-name>
    labctl vm pause <vm-name>

  Access:
    labctl vm connect <vm-name> [--serial] [--user <user>] [--ip <ip>] [--no-key-check] [--command <cmd>] [-- <ssh-args>...]

  Network:
    labctl vm firewall add <vm-name> --from <alias-or-cidr> --port <port> [--proto tcp|udp]

  Metadata:
    labctl vm tag list <vm-name>
    labctl vm tag add <vm-name> <tag>
    labctl vm tag remove <vm-name> <tag>
    labctl vm tag set <vm-name> <tag> [<tag> ...]
    labctl vm rename --vm <vm-name> <new-service>

  Destructive:
    labctl vm destroy <vm-name> [--force]

Names are env-service-instance, for example prod-homeassistant-1.
EOF
}

json() {
  jq -r "$1" "$CONFIG_FILE"
}

vm_name_re='^([a-z]+)-([A-Za-z0-9_][A-Za-z0-9_-]*)-([0-9]+)$'
tag_re='^[A-Za-z0-9_][A-Za-z0-9_.-]*$'

require_config() {
  [[ -r "$CONFIG_FILE" ]] || die "config not readable: $CONFIG_FILE"
  need jq
  need qm
}

env_range() {
  local env="$1"
  jq -er --arg env "$env" '.envs[$env].range // empty | @tsv' "$CONFIG_FILE" || die "unknown env: $env"
}

vmid_exists() {
  local vmid="$1"
  [[ -e "$QEMU_DIR/$vmid.conf" ]]
}

vm_name() {
  local vmid="$1"
  awk -F': ' '$1 == "name" { print $2; exit }' "$QEMU_DIR/$vmid.conf"
}

vmid_by_name() {
  local name="$1" found="" f id current
  for f in "$QEMU_DIR"/*.conf; do
    [[ -e "$f" ]] || continue
    id="${f##*/}"
    id="${id%.conf}"
    current="$(vm_name "$id")"
    if [[ "$current" == "$name" ]]; then
      found="$id"
      break
    fi
  done
  [[ -n "$found" ]] || die "VM not found by name: $name"
  printf '%s\n' "$found"
}

vm_status() {
  local vmid="$1"
  qm status "$vmid" | awk '{ print $2 }'
}

vm_tags() {
  local vmid="$1"
  template_field "$vmid" tags || true
}

validate_tag() {
  local tag="$1"
  [[ "$tag" =~ $tag_re ]] || die "invalid tag: $tag"
}

normalize_tags() {
  awk -v RS='[;\n]' 'NF && !seen[$0]++ { print }' | paste -sd ';' -
}

set_tags() {
  local vmid="$1" tags="$2"
  qm set "$vmid" --tags "$tags" >/dev/null
}

guest_ipv4s() {
  local vmid="$1"
  qm guest cmd "$vmid" network-get-interfaces 2>/dev/null |
    jq -r '[.[]."ip-addresses"[]? | select(."ip-address-type" == "ipv4") | ."ip-address" | select(startswith("127.") | not)] | .[]' 2>/dev/null
}

best_guest_ip() {
  local vmid="$1" cidr ip fallback=""
  mapfile -t ips < <(guest_ipv4s "$vmid")
  ((${#ips[@]})) || return 1
  fallback="${ips[0]}"
  while read -r cidr; do
    [[ -n "$cidr" ]] || continue
    for ip in "${ips[@]}"; do
      if ip_in_cidr "$ip" "$cidr"; then
        printf '%s\n' "$ip"
        return 0
      fi
    done
  done < <(jq -r '.networks.mgmt[]? // empty' "$CONFIG_FILE")
  printf '%s\n' "$fallback"
}

vm_sshkeys() {
  local vmid="$1" raw
  raw="$(template_field "$vmid" sshkeys || true)"
  [[ -n "$raw" ]] || return 0
  printf '%b\n' "${raw//%/\\x}"
}

local_public_keys() {
  local key
  for key in /root/.ssh/*.pub; do
    [[ -r "$key" ]] || continue
    awk '{ print $1 " " $2 }' "$key"
  done
}

has_known_ssh_key() {
  local vmid="$1" vm_keys local_key
  vm_keys="$(vm_sshkeys "$vmid")"
  [[ -n "$vm_keys" ]] || return 1
  while read -r local_key; do
    [[ -n "$local_key" ]] || continue
    if grep -Fq "$local_key" <<<"$vm_keys"; then
      return 0
    fi
  done < <(local_public_keys)
  return 1
}

ip_to_int() {
  local ip="$1" a b c d
  IFS=. read -r a b c d <<<"$ip"
  [[ "$a" =~ ^[0-9]+$ && "$b" =~ ^[0-9]+$ && "$c" =~ ^[0-9]+$ && "$d" =~ ^[0-9]+$ ]] || return 1
  printf '%u\n' "$(( (a << 24) + (b << 16) + (c << 8) + d ))"
}

ip_in_cidr() {
  local ip="$1" cidr="$2" network prefix ip_int net_int mask
  if [[ "$cidr" != */* ]]; then
    [[ "$ip" == "$cidr" ]]
    return
  fi
  network="${cidr%/*}"
  prefix="${cidr#*/}"
  [[ "$prefix" =~ ^[0-9]+$ && "$prefix" -ge 0 && "$prefix" -le 32 ]] || return 1
  ip_int="$(ip_to_int "$ip")" || return 1
  net_int="$(ip_to_int "$network")" || return 1
  if [[ "$prefix" -eq 0 ]]; then
    mask=0
  else
    mask=$(( (0xffffffff << (32 - prefix)) & 0xffffffff ))
  fi
  (( (ip_int & mask) == (net_int & mask) ))
}

next_id() {
  local env="$1" start end id
  read -r start end < <(env_range "$env")
  for ((id=start; id<=end; id++)); do
    if ! vmid_exists "$id"; then
      printf '%s\n' "$id"
      return
    fi
  done
  die "no free VMID in $env range $start-$end"
}

resolve_template() {
  local size="$1" os="$2" template_name vmid
  template_name="tpl-${size}-${os}"
  vmid="$(vmid_by_name "$template_name")"
  [[ "$vmid" != "9000" ]] || die "template $template_name resolved to VMID 9000, which labctl will not use"
  [[ "$(template_field "$vmid" template)" == "1" ]] || die "$template_name exists at VMID $vmid but is not marked as a template"
  printf '%s\n' "$vmid"
}

template_field() {
  local vmid="$1" key="$2"
  awk -F': ' -v key="$key" '$1 == key { print $2; exit }' "$QEMU_DIR/$vmid.conf"
}

disk_gb() {
  local vmid="$1" raw
  raw="$(template_field "$vmid" scsi0)"
  raw="${raw##*,size=}"
  case "$raw" in
    *M) awk -v m="${raw%M}" 'BEGIN { printf "%.0fGB", m / 1024 }' ;;
    *G) printf '%sB\n' "$raw" ;;
    '' ) printf 'unknown\n' ;;
    * ) awk -v b="$raw" 'BEGIN { printf "%.0fGB", b / 1024 / 1024 / 1024 }' ;;
  esac
}

parse_common_plan_args() {
  ENV_NAME=""
  SIZE=""
  OS_NAME=""
  INSTANCE="1"
  TAGS=()
  BOOTSTRAP=0
  WANT_SYSTEM=0
  WANT_DOCKER=0
  WANT_TAILSCALE=0

  while (($#)); do
    case "$1" in
      --env) ENV_NAME="${2:-}"; shift 2 ;;
      --size) SIZE="${2:-}"; shift 2 ;;
      --os) OS_NAME="${2:-}"; shift 2 ;;
      --instance) INSTANCE="${2:-}"; shift 2 ;;
      --tag) TAGS+=("${2:-}"); shift 2 ;;
      --bootstrap) BOOTSTRAP=1; WANT_SYSTEM=1; shift ;;
      --system) WANT_SYSTEM=1; shift ;;
      --docker) BOOTSTRAP=1; WANT_DOCKER=1; shift ;;
      --tailscale) BOOTSTRAP=1; WANT_TAILSCALE=1; shift ;;
      *) die "unknown option: $1" ;;
    esac
  done

  [[ -n "$ENV_NAME" ]] || die "--env is required"
  [[ -n "$SIZE" ]] || die "--size is required"
  [[ -n "$OS_NAME" ]] || die "--os is required"
  [[ "$INSTANCE" =~ ^[0-9]+$ ]] || die "--instance must be numeric"
}

planned_name() {
  local service="$1"
  printf '%s-%s-%s\n' "$ENV_NAME" "$service" "$INSTANCE"
}

cmd_ids() {
  local sub="${1:-}"; shift || true
  case "$sub" in
    next)
      local env=""
      while (($#)); do
        case "$1" in
          --env) env="${2:-}"; shift 2 ;;
          *) die "unknown option: $1" ;;
        esac
      done
      [[ -n "$env" ]] || die "--env is required"
      next_id "$env"
      ;;
    *) usage; die "unknown ids command: $sub" ;;
  esac
}

cmd_templates() {
  local sub="${1:-}"; shift || true
  case "$sub" in
    list)
      local f id name
      for f in "$QEMU_DIR"/*.conf; do
        [[ -e "$f" ]] || continue
        id="${f##*/}"
        id="${id%.conf}"
        name="$(vm_name "$id")"
        [[ "$name" == tpl-* ]] || continue
        [[ "$(template_field "$id" template)" == "1" ]] || continue
        printf '%s\t%s\n' "$id" "$name"
      done | sort -n
    ;;
    resolve)
      local size="" os=""
      while (($#)); do
        case "$1" in
          --size) size="${2:-}"; shift 2 ;;
          --os) os="${2:-}"; shift 2 ;;
          *) die "unknown option: $1" ;;
        esac
      done
      [[ -n "$size" && -n "$os" ]] || die "--size and --os are required"
      resolve_template "$size" "$os"
      ;;
    *) usage; die "unknown templates command: $sub" ;;
  esac
}

print_plan() {
  local service="$1" vmid="$2" template="$3" name cpu mem disk tag_text
  name="$(planned_name "$service")"
  cpu="$(template_field "$template" cores)"
  [[ -n "$cpu" ]] || cpu="1"
  mem="$(template_field "$template" memory)"
  disk="$(disk_gb "$template")"
  if ((${#TAGS[@]})); then
    tag_text="$(IFS=,; printf '%s' "${TAGS[*]}")"
  else
    tag_text="none"
  fi
  cat <<EOF
Will create:
  Name: $name
  VMID: $vmid
  Template: $template ($(vm_name "$template"))
  CPU: $cpu
  RAM: $mem MB
  Disk: $disk
  Tags: $tag_text
  Firewall: 22/tcp from mgmt
EOF
}

cmd_vm_plan() {
  local service="${1:-}"; shift || true
  [[ -n "$service" ]] || die "service name is required"
  parse_common_plan_args "$@"
  print_plan "$service" "$(next_id "$ENV_NAME")" "$(resolve_template "$SIZE" "$OS_NAME")"
}

qga_exec() {
  local vmid="$1"; shift
  local start
  start="$(qm guest exec "$vmid" --timeout 0 -- "$@")"
  jq -e . <<<"$start" >/dev/null
  if [[ "$(jq -r '.exited // false' <<<"$start")" == "1" || "$(jq -r '.exited // false' <<<"$start")" == "true" ]]; then
    jq -r '."out-data" // empty' <<<"$start"
    jq -r '."err-data" // empty' <<<"$start" >&2
    local immediate_code
    immediate_code="$(jq -r '.exitcode // 1' <<<"$start")"
    [[ "$immediate_code" == "0" ]] || die "guest command failed with exit code $immediate_code"
    return
  fi
  local pid
  pid="$(jq -er '.pid' <<<"$start")"
  local status
  while true; do
    status="$(qm guest exec-status "$vmid" "$pid")"
    if [[ "$(jq -r '.exited // false' <<<"$status")" == "1" || "$(jq -r '.exited // false' <<<"$status")" == "true" ]]; then
      jq -r '."out-data" // empty' <<<"$status"
      jq -r '."err-data" // empty' <<<"$status" >&2
      local code
      code="$(jq -r '.exitcode // 1' <<<"$status")"
      [[ "$code" == "0" ]] || die "guest command failed with exit code $code"
      return
    fi
    sleep 1
  done
}

qga_run_script() {
  local vmid="$1" script="$2" remote="/tmp/labctl-$(basename "$script")"
  [[ -r "$script" ]] || die "bootstrap script not readable: $script"
  qm guest exec "$vmid" --pass-stdin 1 -- bash -lc "cat > '$remote' && chmod +x '$remote'" < "$script" >/dev/null
  qga_exec "$vmid" bash -lc "sudo '$remote'"
}

cmd_vm_bootstrap() {
  local name="${1:-}"; shift || true
  [[ -n "$name" ]] || die "VM name is required"
  local system=0 docker=0 tailscale=0
  while (($#)); do
    case "$1" in
      --system) system=1; shift ;;
      --docker) docker=1; shift ;;
      --tailscale) tailscale=1; shift ;;
      *) die "unknown option: $1" ;;
    esac
  done
  (( system || docker || tailscale )) || system=1
  local vmid script
  vmid="$(vmid_by_name "$name")"
  qga_exec "$vmid" true >/dev/null
  if ((system)); then script="$(json '.bootstrap.system')"; qga_run_script "$vmid" "$script"; fi
  if ((docker)); then script="$(json '.bootstrap.docker')"; qga_run_script "$vmid" "$script"; fi
  if ((tailscale)); then script="$(json '.bootstrap.tailscale')"; qga_run_script "$vmid" "$script"; fi
}

cmd_vm_create() {
  local service="${1:-}"; shift || true
  [[ -n "$service" ]] || die "service name is required"
  parse_common_plan_args "$@"
  local vmid template name tag_text
  vmid="$(next_id "$ENV_NAME")"
  template="$(resolve_template "$SIZE" "$OS_NAME")"
  name="$(planned_name "$service")"
  print_plan "$service" "$vmid" "$template"
  qm clone "$template" "$vmid" --name "$name" --full 1
  qm set "$vmid" --onboot 1 --agent enabled=1 >/dev/null
  if ((${#TAGS[@]})); then
    tag_text="$(IFS=';'; printf '%s' "${TAGS[*]}")"
    qm set "$vmid" --tags "$tag_text" >/dev/null
  fi
  if ((BOOTSTRAP)); then
    qm start "$vmid"
    printf 'Waiting for guest agent on %s...\n' "$name"
    for _ in {1..60}; do
      if qm guest cmd "$vmid" ping >/dev/null 2>&1; then
        break
      fi
      sleep 2
    done
    local args=()
    ((WANT_SYSTEM)) && args+=(--system)
    ((WANT_DOCKER)) && args+=(--docker)
    ((WANT_TAILSCALE)) && args+=(--tailscale)
    cmd_vm_bootstrap "$name" "${args[@]}"
  fi
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
  if ((serial)); then
    [[ -z "$user" ]] || die "--user is incompatible with --serial"
    [[ -z "$ip" ]] || die "--ip is incompatible with --serial"
    ((key_check)) || die "--no-key-check is incompatible with --serial"
    [[ -z "$command" ]] || die "--command is incompatible with --serial"
    ((${#ssh_args[@]} == 0)) || die "SSH passthrough args are incompatible with --serial"
  fi
  local vmid status
  vmid="$(vmid_by_name "$name")"
  status="$(vm_status "$vmid")"
  if ((serial)); then
    exec qm terminal "$vmid"
  fi
  [[ "$status" == "running" ]] || die "$name is $status; start it or use --serial"
  [[ -n "$user" ]] || user="$(json '.default_user')"
  if [[ -z "$ip" ]]; then
    ip="$(best_guest_ip "$vmid" || true)"
  fi
  [[ -n "$ip" ]] || die "no guest-agent IPv4 found for $name; try --ip or --serial"
  if ((key_check)) && ! has_known_ssh_key "$vmid"; then
    printf 'labctl: warning: no local /root/.ssh/*.pub key found in cloud-init sshkeys for %s\n' "$name" >&2
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

cmd_vm_firewall_add() {
  local name="${1:-}"; shift || true
  [[ -n "$name" ]] || die "VM name is required"
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

cmd_vm_tag_add() {
  local name="${1:-}" tag="${2:-}"
  [[ -n "$name" && -n "$tag" ]] || die "VM name and tag are required"
  validate_tag "$tag"
  local vmid existing next
  vmid="$(vmid_by_name "$name")"
  existing="$(vm_tags "$vmid")"
  next="$(printf '%s\n%s\n' "${existing//;/ }" "$tag" | tr ' ' '\n' | normalize_tags)"
  set_tags "$vmid" "$next"
  printf '%s\n' "$next"
}

cmd_vm_tag_list() {
  local name="${1:-}"
  [[ -n "$name" ]] || die "VM name is required"
  local vmid tags
  vmid="$(vmid_by_name "$name")"
  tags="$(vm_tags "$vmid")"
  [[ -n "$tags" ]] || return 0
  tr ';' '\n' <<<"$tags"
}

cmd_vm_tag_remove() {
  local name="${1:-}" tag="${2:-}"
  [[ -n "$name" && -n "$tag" ]] || die "VM name and tag are required"
  validate_tag "$tag"
  local vmid existing next
  vmid="$(vmid_by_name "$name")"
  existing="$(vm_tags "$vmid")"
  next="$(tr ';' '\n' <<<"$existing" | awk -v tag="$tag" 'NF && $0 != tag { print }' | normalize_tags)"
  set_tags "$vmid" "$next"
  printf '%s\n' "$next"
}

cmd_vm_tag_set() {
  local name="${1:-}"; shift || true
  [[ -n "$name" ]] || die "VM name is required"
  (($#)) || die "at least one tag is required"
  local vmid tag next
  vmid="$(vmid_by_name "$name")"
  for tag in "$@"; do
    validate_tag "$tag"
  done
  next="$(printf '%s\n' "$@" | normalize_tags)"
  set_tags "$vmid" "$next"
  printf '%s\n' "$next"
}

cmd_vm_rename() {
  local flag="${1:-}" name="${2:-}" service="${3:-}"
  [[ "$flag" == "--vm" && -n "$name" && -n "$service" ]] || die "usage: labctl vm rename --vm <vm-name> <new-service>"
  [[ "$name" =~ $vm_name_re ]] || die "VM name does not match env-service-instance: $name"
  local env="${BASH_REMATCH[1]}" instance="${BASH_REMATCH[3]}" vmid new_name
  vmid="$(vmid_by_name "$name")"
  new_name="$env-$service-$instance"
  qm set "$vmid" --name "$new_name" >/dev/null
  printf '%s\n' "$new_name"
}

cmd_vm_destroy() {
  local name="${1:-}"; shift || true
  [[ -n "$name" ]] || die "VM name is required"
  local force=0
  while (($#)); do
    case "$1" in
      --force) force=1; shift ;;
      *) die "unknown option: $1" ;;
    esac
  done
  local vmid confirm status tags
  vmid="$(vmid_by_name "$name")"
  status="$(vm_status "$vmid")"
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
    read -r confirm
    [[ "$confirm" == "$name" ]] || die "confirmation did not match; aborting"
  fi
  qm stop "$vmid" --skiplock 1 >/dev/null 2>&1 || true
  qm destroy "$vmid" --purge 1
}

cmd_vm() {
  local sub="${1:-}"; shift || true
  case "$sub" in
    plan) cmd_vm_plan "$@" ;;
    create) cmd_vm_create "$@" ;;
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

cmd_update() {
  local repo_dir="/opt/labctl"
  local installer="$repo_dir/install-labctl.sh"
  [[ -d "$repo_dir/.git" ]] || die "update requires a git checkout at $repo_dir"
  [[ -x "$installer" || -f "$installer" ]] || die "installer not found at $installer"
  need git
  git -C "$repo_dir" pull --ff-only
  INSTALL_CONFIG=0 bash "$installer"
}

main() {
  local cmd="${1:-}"; shift || true
  case "$cmd" in
    update) cmd_update "$@" ;;
    ids) cmd_ids "$@" ;;
    templates) cmd_templates "$@" ;;
    vm)
      require_config
      cmd_vm "$@"
      ;;
    -h|--help|help|"") usage ;;
    *) usage; die "unknown command: $cmd" ;;
  esac
}

main "$@"

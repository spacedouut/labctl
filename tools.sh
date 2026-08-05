
# shellcheck shell=bash
if [[ -n "${PHASE_CONFIG:-}" ]]; then
  CONFIG_FILE="$PHASE_CONFIG"
elif [[ -r /etc/phase.json ]]; then
  CONFIG_FILE="/etc/phase.json"
else
  CONFIG_FILE="/root/labctl.config.json"
fi

PLAN_DIR="${PHASE_PLAN_DIR:-/var/lib/phase/plans}"

VMID_MIN=100
VMID_MAX=8999

warn() {
  if command -v gum >/dev/null 2>&1; then
    gum style --foreground 3 --bold "$*"
  else
    printf 'phase: %s\n' "$*" >&2
  fi
}

error() {
  if command -v gum >/dev/null 2>&1; then
    gum style --foreground 1 --bold "$*"
  else
    printf 'phase: %s\n' "$*" >&2
  fi
}

log() {
  printf '%s\n' "$*"
}

die() {
  error "$*" >&2
  exit 1
}

get_hostname() {
  hostname -s
}

get_qemu_dir() {
  printf '/etc/pve/nodes/%s/qemu-server\n' "$(get_hostname)"
}

QEMU_DIR="$(get_qemu_dir)"

need() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

require_config() {
  [[ -r "$CONFIG_FILE" ]] || die "config not readable: $CONFIG_FILE"
  need jq
  need qm
  need gum
}

json() {
  jq -r "$1" "$CONFIG_FILE"
}

# True when /dev/tty is usable — gum's interactive widgets read/write it
# directly, so this stays correct even inside $(...) where stdout is a pipe.
# Over non-interactive SSH/CI /dev/tty does not exist, so this is false.
have_tty() {
  { true >/dev/tty; } 2>/dev/null
}

# Yes/no confirm. Interactive: gum confirm. Non-interactive: returns the
# given default (default: no) instead of crashing on a missing /dev/tty.
confirm() {
  local prompt="$1" default="${2:-no}"
  if have_tty; then
    gum confirm "$prompt"
  else
    [[ "$default" == "yes" ]]
  fi
}

# Require a TTY for an interactive action, or die with a helpful message.
require_tty() {
  have_tty || die "$1 requires an interactive terminal; pass it as a flag instead"
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

template_field() {
  local vmid="$1" key="$2"
  awk -F': ' -v key="$key" '$1 == key { print $2; exit }' "$QEMU_DIR/$vmid.conf"
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

# Resolve a bootstrap step's script path for a given OS, honoring os_overrides.
# Returns empty (success) if no script is configured for that step/OS.
bootstrap_script() {
  local os="$1" step="$2" dir rel
  dir="$(json '.bootstrap.directory // empty')"
  rel="$(jq -r --arg os "$os" --arg step "$step" \
    '.bootstrap.os_overrides[$os][$step] // empty' "$CONFIG_FILE")"
  [[ -n "$rel" ]] || return 0
  printf '%s/%s\n' "${dir:-.}" "$rel"
}

# Run a guest command via QGA, blocking until it exits; streams out/err.
qga_exec() {
  local vmid="$1"; shift
  local start pid status code
  start="$(qm guest exec "$vmid" --timeout 0 -- "$@")"
  jq -e . <<<"$start" >/dev/null
  if [[ "$(jq -r '.exited // false' <<<"$start")" == "1" || "$(jq -r '.exited // false' <<<"$start")" == "true" ]]; then
    jq -r '."out-data" // empty' <<<"$start"
    jq -r '."err-data" // empty' <<<"$start" >&2
    code="$(jq -r '.exitcode // 1' <<<"$start")"
    [[ "$code" == "0" ]] || die "guest command failed with exit code $code"
    return
  fi
  pid="$(jq -er '.pid' <<<"$start")"
  while true; do
    status="$(qm guest exec-status "$vmid" "$pid")"
    if [[ "$(jq -r '.exited // false' <<<"$status")" == "1" || "$(jq -r '.exited // false' <<<"$status")" == "true" ]]; then
      jq -r '."out-data" // empty' <<<"$status"
      jq -r '."err-data" // empty' <<<"$status" >&2
      code="$(jq -r '.exitcode // 1' <<<"$status")"
      [[ "$code" == "0" ]] || die "guest command failed with exit code $code"
      return
    fi
    sleep 1
  done
}

# Upload a local script into the guest and run it with sudo.
qga_run_script() {
  local vmid="$1" script="$2" remote
  remote="/tmp/phase-$(basename "$script")"
  [[ -r "$script" ]] || die "bootstrap script not readable: $script"
  qm guest exec "$vmid" --pass-stdin 1 -- bash -lc "cat > '$remote' && chmod +x '$remote'" < "$script" >/dev/null
  qga_exec "$vmid" bash -lc "sudo '$remote'"
}

# Storage pools that can hold VM disk images, one per line.
list_storages() {
  pvesm status --content images 2>/dev/null | awk 'NR>1 {print $1}'
}

# Parse a size string like "32G", "512M", "1T", or raw bytes into bytes.
to_bytes() {
  local s="$1"
  case "$s" in
    *[Tt]) awk -v n="${s%[Tt]}" 'BEGIN { printf "%.0f", n * 1024^4 }' ;;
    *[Gg]) awk -v n="${s%[Gg]}" 'BEGIN { printf "%.0f", n * 1024^3 }' ;;
    *[Mm]) awk -v n="${s%[Mm]}" 'BEGIN { printf "%.0f", n * 1024^2 }' ;;
    *[Kk]) awk -v n="${s%[Kk]}" 'BEGIN { printf "%.0f", n * 1024 }' ;;
    *[0-9]) printf '%s' "$s" ;;
    *) return 1 ;;
  esac
}

# Current size of a VM's scsi0 disk in bytes (from its config), or empty.
disk_bytes() {
  local vmid="$1" raw
  raw="$(template_field "$vmid" scsi0)"
  raw="${raw##*,size=}"
  [[ -n "$raw" ]] || return 0
  to_bytes "$raw" 2>/dev/null || true
}

# Network bridges on this node, one per line.
list_bridges() {
  ip -o link show type bridge 2>/dev/null | awk -F': ' '{print $2}'
}

# Current tag list of a VM (semicolon-separated), or empty.
vm_tags() {
  local vmid="$1"
  template_field "$vmid" tags || true
}

validate_tag() {
  local tag="$1"
  [[ "$tag" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] || die "invalid tag: $tag"
}

# Normalize tag streams (; or newline separated) to a unique ;-joined line.
normalize_tags() {
  awk -v RS='[;\n]' 'NF && !seen[$0]++ { print }' | paste -sd ';' -
}

set_tags() {
  local vmid="$1" tags="$2"
  qm set "$vmid" --tags "$tags" >/dev/null
}

# Resolve "<user> <ip>" for a running VM, for SSH-based commands (service,
# logs, exec). User comes from cloud-init ciuser, then config default_user.
vm_ssh_user_ip() {
  local name="$1" vmid user ip
  vmid="$(vmid_by_name "$name")"
  [[ "$(qm status "$vmid" | awk '{print $2}')" == "running" ]] || die "$name is not running"
  user="$(template_field "$vmid" ciuser)"
  [[ -n "$user" ]] || user="$(json '.default_user // "root"')"
  ip="$(best_guest_ip "$vmid" || true)"
  [[ -n "$ip" ]] || die "no guest-agent IPv4 found for $name"
  printf '%s %s\n' "$user" "$ip"
}

# Read local SSH public keys, one per line (just type+key, no comment).
local_public_keys() {
  local key
  for key in /root/.ssh/*.pub; do
    [[ -r "$key" ]] || continue
    awk '{ print $1 " " $2 }' "$key"
  done
}

# True if any local SSH key exists in the VM's cloud-init sshkeys.
has_known_ssh_key() {
  local vmid="$1" vm_keys local_key
  vm_keys="$(vm_sshkeys_decoded "$vmid")"
  [[ -n "$vm_keys" ]] || return 1
  while read -r local_key; do
    [[ -n "$local_key" ]] || continue
    if grep -Fq "$local_key" <<<"$vm_keys"; then
      return 0
    fi
  done < <(local_public_keys)
  return 1
}

# Read a VM's current cloud-init sshkeys, URL-decoded, one per line.
vm_sshkeys_decoded() {
  local vmid="$1" raw
  raw="$(template_field "$vmid" sshkeys || true)"
  [[ -n "$raw" ]] || return 0
  printf '%b\n' "${raw//%/\\x}"
}

# Expand one key spec (file path or literal key string) to key lines on stdout.
expand_key_spec() {
  local spec="$1"
  if [[ -f "$spec" ]]; then
    cat "$spec"
  else
    printf '%s\n' "$spec"
  fi
}

# Build a deduped keyfile for VMID $1 = inherited keys + config vm.ssh_keys +
# any extra specs ($2..). Echoes the temp file path, or nothing if no keys.
# Caller is responsible for rm-ing the path.
build_sshkeys_file() {
  local vmid="$1"; shift
  local tmp spec
  tmp="$(mktemp)"
  vm_sshkeys_decoded "$vmid" >>"$tmp"
  while IFS= read -r spec; do
    [[ -n "$spec" ]] || continue
    expand_key_spec "$spec" >>"$tmp"
  done < <(jq -r '.vm.ssh_keys[]? // empty' "$CONFIG_FILE")
  for spec in "$@"; do
    [[ -n "$spec" ]] || continue
    expand_key_spec "$spec" >>"$tmp"
  done
  # dedupe, drop blanks; if nothing, signal "no keys" by removing the file.
  awk 'NF && !seen[$0]++' "$tmp" >"$tmp.d" && mv "$tmp.d" "$tmp"
  if [[ -s "$tmp" ]]; then
    printf '%s\n' "$tmp"
  else
    rm -f "$tmp"
  fi
}

resolve_template() {
  local size="$1" os="$2" prefix sep template_name vmid
  prefix="$(json '.templates.prefix // "tpl"')"
  sep="$(json '.templates.separator // "-"')"
  template_name="${prefix}${sep}${os}"
  vmid="$(vmid_by_name "$template_name" 2>/dev/null)"
  [[ -n "$vmid" ]] || die "template not found: $template_name (create it or pass a different --os)"
  [[ "$(template_field "$vmid" template)" == "1" ]] || die "$template_name exists at VMID $vmid but is not marked as a template"
  printf '%s\n' "$vmid"
}

# Lowest free VMID in the usable range (9xxx is reserved for templates).
next_id() {
  local id
  for ((id=VMID_MIN; id<=VMID_MAX; id++)); do
    vmid_exists "$id" || { printf '%s\n' "$id"; return; }
  done
  die "no free VMID in range $VMID_MIN-$VMID_MAX"
}

# OS suffixes of every VM named tpl-* that is actually a template.
list_template_oses() {
  local f id name
  for f in "$QEMU_DIR"/*.conf; do
    [[ -e "$f" ]] || continue
    id="${f##*/}"; id="${id%.conf}"
    name="$(vm_name "$id")"
    [[ "$name" == tpl-* ]] || continue
    [[ "$(template_field "$id" template)" == "1" ]] || continue
    printf '%s\n' "${name#tpl-}"
  done | sort -u
}

# Validate a service/VM name against the configured naming rules.
validate_name() {
  local name="$1" pattern max_length
  pattern="$(json '.naming.pattern // "^[a-z][a-z0-9-]*$"')"
  max_length="$(json '.naming.max_length // 63')"
  [[ "$name" =~ $pattern ]] || die "invalid name: $name (must match $pattern)"
  (( ${#name} <= max_length )) || die "name too long: $name (max $max_length)"
}

# Look up a size key in config and emit "cores memory" (MB).
resolve_size() {
  local size="$1" out
  out="$(jq -r --arg size "$size" \
    '.templates.sizes[$size] // empty | "\(.cores) \(.memory)"' "$CONFIG_FILE")"
  [[ -n "$out" ]] || die "unknown size: $size"
  printf '%s\n' "$out"
}

plan_path() {
  printf '%s/%s.json\n' "$PLAN_DIR" "$1"
}

read_plan() {
  local name="$1" path
  if [[ "$name" == "planned" ]]; then
    path="$(latest_plan)" || die "no saved plans in $PLAN_DIR"
  else
    path="$(plan_path "$name")"
  fi
  [[ -r "$path" ]] || die "plan not found: $path"
  jq -e . "$path" >/dev/null 2>&1 || die "plan is not valid JSON: $path"
  cat "$path"
}

# Write a plan JSON (read from stdin) to the plan dir, keyed by its name.
write_plan() {
  local name="$1"
  mkdir -p "$PLAN_DIR"
  jq -e . >"$(plan_path "$name")" || die "refusing to write invalid plan JSON"
}

# Most-recently-modified plan file's path.
latest_plan() {
  local f
  f="$(ls -t "$PLAN_DIR"/*.json 2>/dev/null | head -n1)"
  [[ -n "$f" ]] || return 1
  printf '%s\n' "$f"
}



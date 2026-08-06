# phase

Compute-engine style management for Proxmox VE, in the spirit of GCP Compute Engine. `phase` wraps common `qm` workflows: plan-based provisioning, naming, VMID allocation, template cloning, SSH access, guest-agent commands, UFW rules, tags, service management, and guarded deletes.

## Install

Run on the PVE host:

```bash
curl -fsSL https://raw.githubusercontent.com/spacedouut/phase/refs/heads/v3/installer.sh | bash
```

Installs to:

```
/opt/phase        # git checkout (update with `phase update`)
/usr/local/bin/phase  # symlink to /opt/phase/phase.sh
/etc/phase.json   # configuration
```

## Grammar

Two command shapes, both valid:

```bash
# verb-first — creating things
phase vm plan|create|provision|nextid ...

# noun-first — operating on an existing VM (the default when the first
# token isn't a reserved action word)
phase vm <name> <action> ...
```

Legacy verb-first calls (`phase vm shell postgres`) keep working — reserved
action words win as the first token, anything else is a VM name.

## Common Commands

```shell
# Update from git and reinstall the wrapper only:
phase update

# List templates / find the next free VMID:
phase templates
phase vm nextid

# Plan a VM (prints what it will do; --save keeps the plan):
phase vm plan --size small --name postgres --os ubuntu-26 --save

# Create a VM from the saved plan (--vmidout prints just the VMID):
phase vm create postgres --vmidout

# Create + start + bootstrap (system scripts; --docker/--tailscale add steps):
phase vm provision app --size medium --os ubuntu-26 --docker --tailscale

# Per-VM operations:
phase vm postgres                 # interactive SSH (default action)
phase vm postgres shell           # SSH (alias: ssh)
phase vm postgres exec -- uptime  # one-shot SSH command
phase vm postgres service nginx restart        # systemd unit management
phase vm postgres service sherpa-stt-gpu status --user
phase vm postgres logs --unit nginx -n 50      # journald (--follow to tail)
phase vm postgres status          # VMID, state, IP, tags, resources
phase vm postgres start|stop|reboot|shutdown|pause
phase vm postgres tag add db
phase vm postgres tag list
phase vm postgres firewall add --from lan --port 6379
phase vm postgres rename postgresql
phase vm postgres destroy         # type the name to confirm (--force: tmp-*/lab-*)
```

## Naming

Names are lowercase alphanumeric + hyphens, validated against the config
(`naming.pattern`, default `^[a-z][a-z0-9-]*$`, max 63 chars). Follow your own
scheme — `prod-redis-1`, `tmp-smoke-test`, `hermes`, all fine.

Templates are resolved by Proxmox VM name as `<prefix>-<os>`, e.g.:

```
tpl-ubuntu-26
tpl-debian-12
```

The prefix/separator come from config (`templates.prefix`/`separator`).

## Config

`/etc/phase.json` defines local policy:

- `default_user` — SSH user when the VM has no cloud-init `ciuser` (e.g. `ubuntu`)
- `networks` — aliases for firewall rules and best-IP selection (`mgmt`, `lan`...)
- `templates.sizes` — named resource sizes (cores/memory) validated by `--size`
- `bootstrap.directory` + `os_overrides` — per-OS bootstrap script paths
- `vm.agent` / `vm.ssh_keys` — defaults for new VMs (guest agent, SSH keys)
- `ui.confirm_destructive` — reserved for destructive confirmations

Example:

```json
{
  "default_user": "ubuntu",
  "networks": { "lan": ["192.168.0.0/16"], "mgmt": ["10.0.0.0/8"] },
  "templates": {
    "sizes": {
      "micro":  { "cores": 1, "memory": 1024 },
      "small":  { "cores": 2, "memory": 2048 }
    }
  },
  "bootstrap": {
    "directory": "/opt/phase/bootstrap",
    "os_overrides": {
      "ubuntu-26": { "system": "ubuntu/initialize_system.sh" }
    }
  },
  "vm": { "agent": true, "ssh_keys": ["/root/.ssh/id_ed25519.pub"] }
}
```

## Bootstrap

Bootstrap scripts live in `/opt/phase/bootstrap/<os>/`. `initialize_system`
sets up base tooling (ufw, qemu-guest-agent, ...), `initialize_docker`
installs Docker, `initialize_tailscale` installs Tailscale. Scripts are
uploaded and run inside the guest through the QEMU Guest Agent.

## Notes

- `phase vm create` with no saved plan opens an interactive gum wizard when a
  terminal is available; non-interactively (CI, scripts) it requires the
  fields as flags.
- Destructive ops are guarded: `destroy` asks you to type the VM name;
  `--force` only works for `tmp-*`/`lab-*` VMs.
- Assumes Ubuntu-flavoured cloud-init templates by default; other OSes are
  configured via `bootstrap.os_overrides`.

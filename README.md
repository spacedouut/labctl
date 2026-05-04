# labctl
simple commandline tool for managing PVE nodes similar to GCP Compute Engine.

`labctl` wraps common `qm` workflows for VM naming, VMID allocation, template cloning, SSH access, guest-agent commands, UFW rules, tags, and guarded deletes.

## Install

Run on PVE host:

```curl -fsSL https://raw.githubusercontent.com/spacedouut/labctl/refs/heads/main/install-labctl.sh | bash```

## Naming
VM names go by `environment-name-instance`. For example, `prod-homeassistant-1`, `tmp-redis-1`, `lab-minecraft-2`, etc...

Templates are resolved by Proxmox VM name:
```
tpl-<size>-<os>
```
So, for example:
```
tpl-micro-ubuntu-26-lts
tpl-small-ubuntu-26-lts
tpl-medium-ubuntu-26-lts
tpl-large-ubuntu-26-lts
```

installs to:

```
/opt/labctl # repo for updating
/usr/local/bin/labctl # actual binary / script
/etc/labctl/config.json # configuration
```

## Config

/etc/labctl/config.json defines local policy such as:

- VMID ranges by environment
- Network aliases like lan
- Default SSH user
- Bootstrap script paths

Example network alias:
```json
"networks": {
  "lan": ["192.168.0.0/16"]
}
```

## Bootstrap

Bootstrap scripts live in `/opt/labctl/bootstrap`. Each script does something different; `initialize_system` sets up basic tooling like ufw, qemu, and others, `initialize_docker` installs docker, `initialize_tailscale` installs tailscale

## Common Commands
```shell
# List templates
labctl templates list
# Test template resolution
labctl templates resolve --size small --os ubuntu-26-lts
# Find next VMID:
labctl ids next --env prod
# Plan a VM (see what it will do):
labctl vm plan redis --env prod --size small --os ubuntu-26-lts
# Create a VM:
labctl vm create redis --env prod --size small --os ubuntu-26-lts
# Create with bootstrap options (install docker or tailscale too):
labctl vm create app --env lab --size medium --os ubuntu-26-lts --docker --tailscale
# Connect over SSH:
labctl vm connect prod-redis-1
# Run a command over SSH:
labctl vm connect prod-redis-1 --command 'hostname && whoami'
# Open serial console:
labctl vm connect prod-redis-1 --serial
# Add a UFW rule using a network alias:
labctl vm firewall add prod-redis-1 --from lan --port 6379
# Manage tags:
labctl vm tag list prod-redis-1
labctl vm tag add prod-redis-1 db
labctl vm tag remove prod-redis-1 db
labctl vm tag set prod-redis-1 db cache
# Rename while preserving env and instance:
labctl vm rename --vm prod-redis-1 web_redis
# Result: prod-web_redis-1
# Destroy a VM (requires confirmation):
labctl vm destroy prod-redis-1
```

# Friendly install reminder

This essentially *assumes* you'll be using Ubuntu-based VMs with Cloudinit. I'll be making this more dynamic soon, supporting other distros like Debian or even Kali, but for now, ubuntu remains.

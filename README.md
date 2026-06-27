# phase
phase (stylized to lowercase) is a compute-engine-like CLI tool for managing Proxmox VE Virtual Machines.

## Install

Run on PVE host:

```curl -fsSL https://raw.githubusercontent.com/spacedouut/labctl/refs/heads/main/installer.sh | bash```

installs to:

```
/opt/phase # repo for updating
/usr/local/bin/phase # actual binary / script
/etc/phase/config.json # configuration
```

## Config

/etc/phase/config.json defines local policy such as:

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

Bootstrap scripts live in `/opt/phase/bootstrap/` and they run automatically depending on your OS. They configure things like SSH, UFW, Fail2Ban on SSH, etc..

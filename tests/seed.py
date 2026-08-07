#!/usr/bin/env python3
"""Seed the fake-qm state with the fixture VM inventory."""
import os
import sys

STATE = sys.argv[1]
os.makedirs(STATE, exist_ok=True)

VMS = {
    100: ("gateway", "running", {"memory": "1024", "cores": "2", "tags": "prod",
                                 "net0": "virtio=AA:00:00:00:00:01,bridge=vmbr0",
                                 "scsi0": "local-lvm:vm-100-disk-0,size=16G"}),
    101: ("auth", "running", {"memory": "1024", "cores": "2", "tags": "prod;auth"}),
    102: ("minecraft", "stopped", {"memory": "4096", "cores": "4", "tags": "game"}),
    103: ("hermes", "running", {"memory": "8192", "cores": "4", "tags": "ai"}),
    106: ("macos", "stopped", {"memory": "8192", "cores": "8"}),
    9001: ("tpl-ubuntu-26", "stopped", {
        "template": "1", "ciuser": "ubuntu", "cores": "1", "memory": "1024",
        "agent": "enabled=1", "boot": "order=scsi0",
        "scsi0": "local-lvm:base-9001-disk-1,size=8304M",
        "ipconfig0": "ip=dhcp,ip6=auto",
        "sshkeys": "ssh-ed25519%20AAAA-test-key%20root%40homelab%0A"}),
    9005: ("tpl-none", "stopped", {"template": "1", "cores": "1", "memory": "1024",
                                   "boot": "order=scsi0",
                                   "scsi0": "local-lvm:base-9005-disk-0,size=0M"}),
}

for vmid, (name, power, extra) in VMS.items():
    conf = {"name": name, "bios": "ovmf", "boot": "order=scsi0", "cores": "1",
            "memory": "1024", "agent": "enabled=1", "onboot": "0",
            "net0": "virtio=AA:00:00:00:00:01,bridge=vmbr0",
            "scsi0": "local-lvm:vm-%d-disk-0,size=32G" % vmid}
    conf.update(extra)
    with open(os.path.join(STATE, f"{vmid}.conf"), "w") as f:
        for k, v in conf.items():
            f.write(f"{k}: {v}\n")
    with open(os.path.join(STATE, f"{vmid}.power"), "w") as f:
        f.write(power + "\n")
print("seeded", len(VMS), "vms")

#!/usr/bin/env bash
set -euo pipefail

ADMIN_USER="ubuntu"
SSH_PORT="22"
TIMEZONE="${TIMEZONE:-UTC}"
MGMT_CIDR="192.168.0.0/16"
MONITORING_IP="${MONITORING_IP:-}"

echo "== Proxmox Ubuntu Headless Template Setup =="

export DEBIAN_FRONTEND=noninteractive

echo "== Setting timezone =="
timedatectl set-timezone "$TIMEZONE"

echo "== Updating system =="
apt-get update
apt-get upgrade -y

echo "== Installing base packages =="
apt-get install -y \
  qemu-guest-agent \
  cloud-init \
  curl wget git vim nano htop tmux \
  net-tools dnsutils iputils-ping traceroute \
  ca-certificates gnupg lsb-release \
  ufw fail2ban unattended-upgrades \
  prometheus-node-exporter \
  chrony \
  sudo \
  bash-completion \
  jq \
  unzip

echo "== Enabling services =="
systemctl enable qemu-guest-agent || true
systemctl enable chrony
systemctl enable prometheus-node-exporter
systemctl enable fail2ban

echo "== Creating admin user if missing =="
if ! id "$ADMIN_USER" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "$ADMIN_USER"
fi

usermod -aG sudo "$ADMIN_USER"

echo "$ADMIN_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-$ADMIN_USER"
chmod 440 "/etc/sudoers.d/90-$ADMIN_USER"

echo "== Configuring serial console / GRUB =="
if grep -q '^GRUB_CMDLINE_LINUX=' /etc/default/grub; then
  sed -i 's/^GRUB_CMDLINE_LINUX=.*/GRUB_CMDLINE_LINUX="console=tty0 console=ttyS0,115200n8"/' /etc/default/grub
else
  echo 'GRUB_CMDLINE_LINUX="console=tty0 console=ttyS0,115200n8"' >> /etc/default/grub
fi

update-grub
systemctl enable serial-getty@ttyS0.service

echo "== Hardening SSH =="
mkdir -p /etc/ssh/sshd_config.d

cat > /etc/ssh/sshd_config.d/99-homelab-hardening.conf <<EOF
Port $SSH_PORT
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
X11Forwarding no
AllowTcpForwarding yes
ClientAliveInterval 300
ClientAliveCountMax 2
MaxAuthTries 3
EOF

sshd -t
systemctl reload ssh || systemctl reload sshd

echo "== Configuring UFW firewall =="
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow from "$MGMT_CIDR" to any port "$SSH_PORT" proto tcp

if [[ -n "$MONITORING_IP" ]]; then
  ufw allow from "$MONITORING_IP" to any port 9100 proto tcp
fi

ufw --force enable

echo "== Configuring fail2ban =="
cat > /etc/fail2ban/jail.d/sshd.local <<EOF
[sshd]
enabled = true
port = $SSH_PORT
maxretry = 10
findtime = 10m
bantime = 1h
EOF

systemctl restart fail2ban

echo "== Configuring unattended security upgrades =="
apt-get install -y unattended-upgrades

cat > /etc/apt/apt.conf.d/20auto-upgrades <<EOF
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF

echo "== Configuring cloud-init for Proxmox =="
mkdir -p /etc/cloud/cloud.cfg.d

cat > /etc/cloud/cloud.cfg.d/99-proxmox.cfg <<EOF
datasource_list: [ ConfigDrive, NoCloud ]
datasource:
  NoCloud:
    fs_label: CIDATA
EOF

echo "== Disabling disk swap =="
swapoff -a || true
sed -i.bak '/ swap / s/^/#/' /etc/fstab

echo "== Installing and configuring ZRAM swap =="
apt-get install -y zram-tools

cat > /etc/default/zramswap <<EOF
# ZRAM configuration
PERCENT=25
PRIORITY=100
ALGO=lz4
EOF

systemctl enable zramswap
systemctl restart zramswap

echo "== Cleaning logs and machine identity =="
apt-get clean
rm -rf /var/lib/apt/lists/*
rm -rf /var/log/journal/*
truncate -s 0 /var/log/*.log 2>/dev/null || true

cloud-init clean --logs

rm -f /etc/machine-id
touch /etc/machine-id

echo "== Setup complete =="
echo
echo "Before converting to template:"
echo "1. Add your SSH public key via Proxmox cloud-init."
echo "2. Suggested Proxmox settings:"
echo "  - Serial port: socket"
echo "  - Display: serial0"
echo "  - QEMU Guest Agent: enabled"
echo "  - Disk: SCSI, discard enabled"
echo "  - Network: VirtIO"

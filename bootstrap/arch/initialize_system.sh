#!/bin/bash
set -euo pipefail

ADMIN_USER="arch"
SSH_PORT="22"
TIMEZONE="${TIMEZONE:-UTC}"
MGMT_CIDR="192.168.0.0/16"

echo "== Proxmox Arch Headless Template Setup =="

echo "== Setting timezone =="
timedatectl set-timezone "$TIMEZONE"

echo "== Updating system =="
pacman -Syu --noconfirm

echo "== Installing base packages =="
pacman -S --noconfirm \
  qemu-guest-agent \
  cloud-init \
  curl wget git vim nano htop tmux \
  net-tools dnsutils iputils \
  ufw fail2ban \
  prometheus-node-exporter \
  chrony \
  sudo \
  bash-completion \
  jq \
  unzip \
  openssh \
  grub

echo "== Enabling services =="
systemctl enable qemu-guest-agent || true
systemctl enable chronyd
systemctl enable prometheus-node-exporter
systemctl enable fail2ban
systemctl enable ufw
systemctl enable sshd

echo "== Creating admin user if missing =="
if ! id "$ADMIN_USER" >/dev/null 2>&1; then
  useradd -m -s /bin/bash "$ADMIN_USER"
fi

echo "$ADMIN_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-$ADMIN_USER"
chmod 440 "/etc/sudoers.d/90-$ADMIN_USER"

echo "== Configuring serial console / GRUB =="
if grep -q '^GRUB_CMDLINE_LINUX_DEFAULT=' /etc/default/grub; then
  sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT=.*/GRUB_CMDLINE_LINUX_DEFAULT="console=tty0 console=ttyS0,115200n8"/' /etc/default/grub
else
  echo 'GRUB_CMDLINE_LINUX_DEFAULT="console=tty0 console=ttyS0,115200n8"' >> /etc/default/grub
fi

grub-mkconfig -o /boot/grub/grub.cfg
systemctl enable serial-getty@ttyS0.service

echo "== Hardening SSH =="
mkdir -p /etc/ssh/sshd_config.d

cat > /etc/ssh/sshd_config.d/99-hardening.conf <<EOF
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

# Ensure sshd loads the config.d directory
if ! grep -q "Include /etc/ssh/sshd_config.d/\*.conf" /etc/ssh/sshd_config; then
  sed -i '1iInclude /etc/ssh/sshd_config.d/*.conf' /etc/ssh/sshd_config
fi

systemctl restart sshd || true

echo "== Configuring UFW firewall =="
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow from "$MGMT_CIDR" to any port "$SSH_PORT" proto tcp
ufw --force enable

echo "== Configuring cloud-init for Proxmox =="
mkdir -p /etc/cloud/cloud.cfg.d

cat > /etc/cloud/cloud.cfg.d/99-proxmox.cfg <<EOF
datasource_list: [ ConfigDrive, NoCloud ]
datasource:
  NoCloud:
    fs_label: CIDATA
EOF

echo "== Setup complete =="

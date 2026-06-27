#!/bin/sh
set -eu

ADMIN_USER="alpine"
SSH_PORT="22"
TIMEZONE="${TIMEZONE:-UTC}"
MGMT_CIDR="192.168.0.0/16"

echo "== Proxmox Alpine Headless Template Setup =="

echo "== Updating system =="
apk update
apk upgrade

echo "== Installing base packages =="
apk add \
  qemu-guest-agent \
  cloud-init \
  curl wget git vim nano htop tmux \
  net-tools bind-tools iputils \
  ufw fail2ban \
  prometheus-node-exporter \
  chrony \
  sudo \
  bash-completion \
  jq \
  unzip \
  util-linux \
  tzdata \
  openssh

echo "== Setting timezone =="
cp /usr/share/zoneinfo/"$TIMEZONE" /etc/localtime
echo "$TIMEZONE" > /etc/timezone

echo "== Enabling services =="
rc-update add qemu-guest-agent default
rc-update add chronyd default
rc-update add node-exporter default
rc-update add sshd default
rc-update add ufw default
rc-update add fail2ban default

/etc/init.d/qemu-guest-agent start || true

echo "== Creating admin user if missing =="
if ! id "$ADMIN_USER" >/dev/null 2>&1; then
  adduser -D -s /bin/bash "$ADMIN_USER"
  passwd -d "$ADMIN_USER"
fi

echo "$ADMIN_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-$ADMIN_USER"
chmod 440 "/etc/sudoers.d/90-$ADMIN_USER"

echo "== Configuring serial console =="
# Alpine typically uses /etc/inittab for serial getty
if ! grep -q "ttyS0" /etc/inittab; then
  echo "ttyS0::respawn:/sbin/getty -L 115200 ttyS0 vt100" >> /etc/inittab
fi

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

rc-service sshd restart || true

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

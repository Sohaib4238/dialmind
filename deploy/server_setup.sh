#!/usr/bin/env bash
# One-time setup of the Ubuntu 24.04 server (safe to run again). Run with sudo.
#   system updates, 2 GB swap, automatic security updates, UTC, ufw (22/80/443), fail2ban for SSH,
#   Docker + Compose, the /srv/dialmind folders, the daily database backup timer.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a

APP_USER=ubuntu
ROOT=/srv/dialmind

echo "== System updates"
apt-get update -qq
apt-get -y -qq -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold full-upgrade
apt-get -y -qq install ca-certificates curl gnupg ufw fail2ban unattended-upgrades sqlite3

echo "== Timezone: UTC"
timedatectl set-timezone UTC

echo "== Swap: 2 GB (only 1 GB of RAM)"
if ! swapon --show=NAME --noheadings | grep -qx /swapfile; then
    [ -f /swapfile ] || fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
cat >/etc/sysctl.d/90-dialmind.conf <<'EOF'
# Use swap only under real memory pressure
vm.swappiness=10
EOF
sysctl -q --system

echo "== Automatic security updates"
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
# Kernel updates wait for a planned reboot: an automatic one could cut a live call.
cat >/etc/apt/apt.conf.d/51dialmind-unattended <<'EOF'
Unattended-Upgrade::Automatic-Reboot "false";
EOF
systemctl enable --now unattended-upgrades >/dev/null

echo "== SSH: no root login (key-only login is already the image default)"
cat >/etc/ssh/sshd_config.d/90-dialmind.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
EOF
sshd -t
systemctl reload ssh

echo "== Firewall: 22, 80, 443 only"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

echo "== fail2ban for SSH"
cat >/etc/fail2ban/jail.d/dialmind.local <<'EOF'
[sshd]
enabled = true
backend = systemd
maxretry = 5
findtime = 10m
bantime = 1h
EOF
systemctl enable fail2ban >/dev/null
systemctl restart fail2ban

echo "== Docker + Compose"
if ! command -v docker >/dev/null; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        >/etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get -y -qq install docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
mkdir -p /etc/docker
cat >/etc/docker/daemon.json <<'EOF'
{"log-driver": "json-file", "log-opts": {"max-size": "10m", "max-file": "3"}}
EOF
systemctl enable docker containerd >/dev/null
systemctl restart docker
usermod -aG docker "$APP_USER"

echo "== AWS CLI (uploads backups to S3 with the instance's role; no keys on this server)"
snap list aws-cli >/dev/null 2>&1 || snap install aws-cli --classic

echo "== Folders on the server disk"
install -d -o "$APP_USER" -g "$APP_USER" -m 755 "$ROOT" "$ROOT/app"
# Leads, recordings and secrets: readable by the app user only (the containers run as the same uid)
install -d -o "$APP_USER" -g "$APP_USER" -m 700 "$ROOT/data" "$ROOT/config" "$ROOT/logs" "$ROOT/secrets" "$ROOT/backups"
install -d -o root -g root -m 755 "$ROOT/caddy" "$ROOT/caddy/data" "$ROOT/caddy/config"

echo "== Daily database backup (03:00 UTC, keeps the last 7)"
cat >/etc/systemd/system/dialmind-backup.service <<EOF
[Unit]
Description=DialMind database backup

[Service]
Type=oneshot
User=$APP_USER
ExecStart=$ROOT/app/backup_db.sh
EOF
cat >/etc/systemd/system/dialmind-backup.timer <<'EOF'
[Unit]
Description=DialMind daily database backup

[Timer]
OnCalendar=*-*-* 03:00:00 UTC
# If the server was off at 03:00, run it at the next boot
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now dialmind-backup.timer >/dev/null

echo "== Done"

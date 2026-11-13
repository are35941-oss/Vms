#!/usr/bin/env bash
set -euo pipefail

if [[ ! -t 0 ]] && [[ ! -r /dev/tty ]]; then
  echo "An interactive terminal is required." >&2
  exit 1
fi
if [[ ${EUID} -ne 0 ]]; then
  echo "Run this installer as root (for example, curl -fsSL URL | sudo bash)." >&2
  exit 1
fi
if ! command -v apt-get >/dev/null || ! command -v systemctl >/dev/null; then
  echo "This installer requires an apt-based Linux system with systemd." >&2
  exit 1
fi

exec 3</dev/tty
read -r -p "Git repository URL containing bot.py and requirements.txt: " REPO <&3
if [[ ! "$REPO" =~ ^https://[^[:space:]]+$ ]]; then
  echo "A valid HTTPS repository URL is required." >&2
  exit 1
fi
read -r -s -p "Discord bot token: " DISCORD_TOKEN <&3; echo
read -r -s -p "DigitalOcean API token: " CLOUD_API_KEY <&3; echo
read -r -p "Admin Discord user ID: " ADMIN_USER_ID <&3
read -r -p "DigitalOcean SSH key ID or fingerprint: " SSH_KEY_ID <&3
if [[ -z "$DISCORD_TOKEN" || -z "$CLOUD_API_KEY" || ! "$ADMIN_USER_ID" =~ ^[0-9]+$ || -z "$SSH_KEY_ID" ]]; then
  echo "All values are required, and ADMIN_USER_ID must be numeric." >&2
  exit 1
fi
if [[ ! "$DISCORD_TOKEN" =~ ^[A-Za-z0-9._-]+$ || ! "$CLOUD_API_KEY" =~ ^[A-Za-z0-9._-]+$ || ! "$SSH_KEY_ID" =~ ^[A-Za-z0-9:._-]+$ ]]; then
  echo "Credentials contain unsupported characters." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y git python3 python3-venv python3-pip ca-certificates
INSTALL_DIR=/opt/vps-bot
if [[ -e "$INSTALL_DIR" ]]; then
  echo "$INSTALL_DIR already exists; refusing to overwrite it." >&2
  exit 1
fi
git clone -- "$REPO" "$INSTALL_DIR"
test -f "$INSTALL_DIR/bot.py" && test -f "$INSTALL_DIR/requirements.txt"
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
if ! id vpsbot >/dev/null 2>&1; then
  useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin vpsbot
fi
umask 077
printf 'DISCORD_TOKEN=%s\nCLOUD_API_KEY=%s\nADMIN_USER_ID=%s\nSSH_KEY_ID=%s\n' \
  "$DISCORD_TOKEN" "$CLOUD_API_KEY" "$ADMIN_USER_ID" "$SSH_KEY_ID" > "$INSTALL_DIR/.env"
chown -R root:root "$INSTALL_DIR"
chown root:vpsbot "$INSTALL_DIR/.env"
chmod 640 "$INSTALL_DIR/.env"
cat > /etc/systemd/system/vps-bot.service <<'SERVICE'
[Unit]
Description=Discord VPS Deployer Bot
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=vpsbot
Group=vpsbot
WorkingDirectory=/opt/vps-bot
ExecStart=/opt/vps-bot/.venv/bin/python /opt/vps-bot/bot.py
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true

[Install]
WantedBy=multi-user.target
SERVICE
systemctl daemon-reload
systemctl enable --now vps-bot.service
echo "Installed. Check logs with: journalctl -u vps-bot -f"
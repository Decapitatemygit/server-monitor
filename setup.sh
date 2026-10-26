#!/bin/bash
set -euo pipefail

echo "=== Server Monitor Setup ==="

# Install python deps
echo "Installing psutil..."
pip3 install psutil --break-system-packages -q 2>/dev/null || pip3 install psutil -q

# Sudoers entry for smartctl (passwordless, monitor user only)
SUDOERS_LINE="$USER ALL=(ALL) NOPASSWD: /usr/sbin/smartctl"
SUDOERS_FILE="/etc/sudoers.d/server-monitor"

if [ ! -f "$SUDOERS_FILE" ] || ! grep -qF "$SUDOERS_LINE" "$SUDOERS_FILE" 2>/dev/null; then
    echo "Adding sudoers entry for smartctl..."
    echo "$SUDOERS_LINE" | sudo tee "$SUDOERS_FILE" > /dev/null
    sudo chmod 440 "$SUDOERS_FILE"
    echo "  Done. $USER can now run smartctl without a password."
else
    echo "  Sudoers entry already exists."
fi

# Install smartmontools if missing
if ! command -v smartctl &>/dev/null; then
    echo "Installing smartmontools..."
    sudo apt install smartmontools -y
fi

# Create systemd service
INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_FILE="/etc/systemd/system/server-monitor.service"

echo "Creating systemd service..."
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Server Monitor Dashboard
After=network.target docker.service
Wants=docker.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/server_monitor.py
Restart=always
RestartSec=5
Environment=MONITOR_PORT=9090
Environment=MONITOR_DRIVES=/srv/data,/srv/backup
Environment=BORG_REPO=/srv/backup/immich-borg
Environment=BACKUP_LOG=/var/log/immich-backup.log
# Uncomment and fill these for Telegram alerts:
#Environment=TG_BOT_TOKEN=your_bot_token
#Environment=TG_CHAT_ID=your_chat_id

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now server-monitor

echo ""
echo "=== Setup complete ==="
echo "Dashboard: http://$(hostname -I | awk '{print $1}'):9090"
echo ""
echo "To enable Telegram alerts:"
echo "  1. Message @BotFather on Telegram, create a bot, copy the token"
echo "  2. Message @userinfobot to get your chat ID"
echo "  3. Edit $SERVICE_FILE and fill in TG_BOT_TOKEN and TG_CHAT_ID"
echo "  4. sudo systemctl restart server-monitor"

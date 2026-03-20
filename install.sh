#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR=/opt/rpi_ap_tools
WIFI_DB_PATH=${WIFI_DB_PATH:-/etc/rpi_ap_tools_wifi_db.json}

sudo mkdir -p "$INSTALL_DIR"
sudo cp -r web_ui.py lcd_status.py templates systemd "$INSTALL_DIR"/
sudo chmod +x "$INSTALL_DIR"/web_ui.py
sudo chmod +x "$INSTALL_DIR"/lcd_status.py

# Preserve saved Wi-Fi credentials across reinstalls/upgrades.
sudo mkdir -p "$(dirname "$WIFI_DB_PATH")"
if [ ! -f "$WIFI_DB_PATH" ]; then
  sudo sh -c "printf '{}\n' > '$WIFI_DB_PATH'"
fi
sudo chmod 600 "$WIFI_DB_PATH" || true

case "$WIFI_DB_PATH" in
  "$INSTALL_DIR"/*)
    echo "WARNING: WIFI_DB_PATH is inside $INSTALL_DIR and may be overwritten by future installs."
    echo "Recommended: leave it at /etc/rpi_ap_tools_wifi_db.json or another path outside $INSTALL_DIR."
    ;;
esac

sudo cp "$INSTALL_DIR"/systemd/rpi-wlan1-ui.service /etc/systemd/system/
sudo cp "$INSTALL_DIR"/systemd/rpi-lcd-status.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable rpi-wlan1-ui.service
sudo systemctl enable rpi-lcd-status.service

echo "Installed."
echo "Restart services:"
echo "  sudo systemctl restart rpi-wlan1-ui.service"
echo "  sudo systemctl restart rpi-lcd-status.service"

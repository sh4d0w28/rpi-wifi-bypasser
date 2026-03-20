#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR=/opt/rpi_ap_tools

sudo mkdir -p "$INSTALL_DIR"
sudo cp -r web_ui.py lcd_status.py templates systemd "$INSTALL_DIR"/
sudo chmod +x "$INSTALL_DIR"/web_ui.py
sudo chmod +x "$INSTALL_DIR"/lcd_status.py
sudo cp "$INSTALL_DIR"/systemd/rpi-wlan1-ui.service /etc/systemd/system/
sudo cp "$INSTALL_DIR"/systemd/rpi-lcd-status.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable rpi-wlan1-ui.service
sudo systemctl enable rpi-lcd-status.service

echo "Installed."
echo "Restart services:"
echo "  sudo systemctl restart rpi-wlan1-ui.service"
echo "  sudo systemctl restart rpi-lcd-status.service"
#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR=/opt/rpi_ap_tools
WIFI_DB_PATH=${WIFI_DB_PATH:-/etc/rpi_ap_tools_wifi_db.json}
YOUTUBE_CLIENT_CONFIG_PATH=${YOUTUBE_CLIENT_CONFIG_PATH:-/etc/rpi_ap_tools_youtube_client.json}
YOUTUBE_TOKEN_PATH=${YOUTUBE_TOKEN_PATH:-/var/lib/rpi_ap_tools/youtube_token.json}
YOUTUBE_STREAM_STATE_PATH=${YOUTUBE_STREAM_STATE_PATH:-/var/lib/rpi_ap_tools/youtube_stream.json}
APP_STATE_DIR=/var/lib/rpi_ap_tools

sudo mkdir -p "$INSTALL_DIR"
sudo cp -r web_ui.py lcd_status.py youtube_live.py templates systemd "$INSTALL_DIR"/
sudo chmod +x "$INSTALL_DIR"/web_ui.py
sudo chmod +x "$INSTALL_DIR"/lcd_status.py
sudo chmod +x "$INSTALL_DIR"/youtube_live.py
sudo python3 -m pip install --upgrade qrcode[pil]

# Preserve saved Wi-Fi credentials across reinstalls/upgrades.
sudo mkdir -p "$(dirname "$WIFI_DB_PATH")"
if [ ! -f "$WIFI_DB_PATH" ]; then
  sudo sh -c "printf '{}\n' > '$WIFI_DB_PATH'"
fi
sudo chmod 600 "$WIFI_DB_PATH" || true

# App config and persistent secret/state directories.
sudo mkdir -p "$(dirname "$YOUTUBE_CLIENT_CONFIG_PATH")"
sudo mkdir -p "$APP_STATE_DIR"
sudo chmod 700 "$APP_STATE_DIR" || true

if [ ! -f "$YOUTUBE_CLIENT_CONFIG_PATH" ]; then
  sudo sh -c "printf '{\n  \"installed\": {\n    \"client_id\": \"\",\n    \"client_secret\": \"\"\n  }\n}\n' > '$YOUTUBE_CLIENT_CONFIG_PATH'"
fi
sudo chmod 600 "$YOUTUBE_CLIENT_CONFIG_PATH" || true

if [ ! -f "$YOUTUBE_TOKEN_PATH" ]; then
  sudo sh -c "printf '{}\n' > '$YOUTUBE_TOKEN_PATH'"
fi
sudo chmod 600 "$YOUTUBE_TOKEN_PATH" || true

if [ ! -f "$YOUTUBE_STREAM_STATE_PATH" ]; then
  sudo sh -c "printf '{}\n' > '$YOUTUBE_STREAM_STATE_PATH'"
fi
sudo chmod 600 "$YOUTUBE_STREAM_STATE_PATH" || true

case "$WIFI_DB_PATH" in
  "$INSTALL_DIR"/*)
    echo "WARNING: WIFI_DB_PATH is inside $INSTALL_DIR and may be overwritten by future installs."
    echo "Recommended: leave it at /etc/rpi_ap_tools_wifi_db.json or another path outside $INSTALL_DIR."
    ;;
esac

for secret_path in "$YOUTUBE_CLIENT_CONFIG_PATH" "$YOUTUBE_TOKEN_PATH" "$YOUTUBE_STREAM_STATE_PATH"; do
  case "$secret_path" in
    "$INSTALL_DIR"/*)
      echo "WARNING: $secret_path is inside $INSTALL_DIR and may be overwritten by future installs."
      ;;
  esac
done

sudo cp "$INSTALL_DIR"/systemd/rpi-wlan1-ui.service /etc/systemd/system/
sudo cp "$INSTALL_DIR"/systemd/rpi-lcd-status.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable rpi-wlan1-ui.service
sudo systemctl enable rpi-lcd-status.service

echo "Installed."
echo "Prepared files:"
echo "  Wi-Fi DB: $WIFI_DB_PATH"
echo "  YouTube client config: $YOUTUBE_CLIENT_CONFIG_PATH"
echo "  YouTube token: $YOUTUBE_TOKEN_PATH"
echo "  YouTube stream state: $YOUTUBE_STREAM_STATE_PATH"
echo "Restart services:"
echo "  sudo systemctl restart rpi-wlan1-ui.service"
echo "  sudo systemctl restart rpi-lcd-status.service"

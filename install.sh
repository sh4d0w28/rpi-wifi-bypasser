#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR=/opt/rpi_ap_tools
WIFI_DB_PATH=${WIFI_DB_PATH:-/etc/rpi_ap_tools_wifi_db.json}
AP_CONFIG_FILE=${AP_CONFIG_FILE:-/etc/default/rpi_ap_tools_ap}
YOUTUBE_CLIENT_CONFIG_PATH=${YOUTUBE_CLIENT_CONFIG_PATH:-/etc/rpi_ap_tools_youtube_client.json}
YOUTUBE_TOKEN_PATH=${YOUTUBE_TOKEN_PATH:-/var/lib/rpi_ap_tools/youtube_token.json}
YOUTUBE_STREAM_STATE_PATH=${YOUTUBE_STREAM_STATE_PATH:-/var/lib/rpi_ap_tools/youtube_stream.json}
APP_STATE_DIR=/var/lib/rpi_ap_tools
WLAN0_IFACE=${WLAN0_IFACE:-wlan0}
WLAN1_IFACE=${WLAN1_IFACE:-wlan1}

sudo mkdir -p "$INSTALL_DIR"
sudo cp -r web_ui.py lcd_status.py youtube_live.py configure_shared_egress.sh configure_ap.sh update_ap.sh templates systemd "$INSTALL_DIR"/
sudo chmod +x "$INSTALL_DIR"/web_ui.py
sudo chmod +x "$INSTALL_DIR"/lcd_status.py
sudo chmod +x "$INSTALL_DIR"/youtube_live.py
sudo chmod +x "$INSTALL_DIR"/configure_shared_egress.sh
sudo chmod +x "$INSTALL_DIR"/configure_ap.sh
sudo chmod +x "$INSTALL_DIR"/update_ap.sh
sudo cp "$INSTALL_DIR"/update_ap.sh /home/pi/update_ap.sh
sudo chmod +x /home/pi/update_ap.sh
sudo apt-get update
sudo apt-get install -y python3-qrcode python3-pil ffmpeg

install_chromium_if_available() {
  local pkg
  for pkg in chromium-browser chromium; do
    if apt-cache show "$pkg" >/dev/null 2>&1; then
      echo "Installing overlay renderer package: $pkg"
      sudo apt-get install -y "$pkg"
      return 0
    fi
  done
  echo "WARNING: No Chromium-compatible package found in apt metadata."
  echo "Overlay HTML-to-PNG rendering will stay unavailable until chromium is installed manually."
  return 1
}

install_chromium_if_available || true

# Preserve saved Wi-Fi credentials across reinstalls/upgrades.
sudo mkdir -p "$(dirname "$WIFI_DB_PATH")"
if [ ! -f "$WIFI_DB_PATH" ]; then
  sudo sh -c "printf '{}\n' > '$WIFI_DB_PATH'"
fi
sudo chmod 600 "$WIFI_DB_PATH" || true

# Preserve AP config across reinstalls/upgrades.
sudo mkdir -p "$(dirname "$AP_CONFIG_FILE")"
if [ ! -f "$AP_CONFIG_FILE" ]; then
  sudo sh -c "cat > '$AP_CONFIG_FILE' <<'EOF'
# Persistent AP settings for /opt/rpi_ap_tools/configure_ap.sh
WLAN0_IFACE=wlan0
AP_CONNECTION_NAME=rpi-ap
AP_SSID=Rpi_Ap_Secure
AP_PASSWORD=12345678
AP_AUTH_MODE=wpa-psk
AP_BAND=bg
AP_CHANNEL=6
EOF"
fi
sudo chmod 600 "$AP_CONFIG_FILE" || true

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

YOUTUBE_CLIENT_ID_CHECK=$(sudo python3 - <<'PY' "$YOUTUBE_CLIENT_CONFIG_PATH"
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    print("invalid")
    raise SystemExit(0)

if isinstance(data, dict):
    for key in ("installed", "web", "tv", "device"):
        if isinstance(data.get(key), dict):
            data = data[key]
            break

client_id = str(data.get("client_id", "")).strip() if isinstance(data, dict) else ""
print("ready" if client_id else "missing")
PY
)

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
sudo cp "$INSTALL_DIR"/systemd/rpi-shared-egress.service /etc/systemd/system/
sudo cp "$INSTALL_DIR"/systemd/rpi-ap-update.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable rpi-shared-egress.service
sudo systemctl enable rpi-wlan1-ui.service
sudo systemctl enable rpi-lcd-status.service

echo "Applying shared egress configuration for $WLAN0_IFACE -> $WLAN1_IFACE"
if ! sudo env WLAN0_IFACE="$WLAN0_IFACE" WLAN1_IFACE="$WLAN1_IFACE" "$INSTALL_DIR"/configure_shared_egress.sh; then
  echo "WARNING: shared egress configuration did not complete automatically."
  echo "Run manually after NetworkManager profiles are ready:"
  echo "  sudo env WLAN0_IFACE=$WLAN0_IFACE WLAN1_IFACE=$WLAN1_IFACE $INSTALL_DIR/configure_shared_egress.sh"
fi
  
echo "Installed."
echo "Prepared files:"
echo "  Wi-Fi DB: $WIFI_DB_PATH"
echo "  AP config: $AP_CONFIG_FILE"
echo "  YouTube client config: $YOUTUBE_CLIENT_CONFIG_PATH"
echo "  YouTube token: $YOUTUBE_TOKEN_PATH"
echo "  YouTube stream state: $YOUTUBE_STREAM_STATE_PATH"
case "$YOUTUBE_CLIENT_ID_CHECK" in
  ready)
    echo "YouTube OAuth client config: ready"
    ;;
  missing)
    echo "YouTube OAuth client config: missing client_id"
    echo "  Edit $YOUTUBE_CLIENT_CONFIG_PATH and fill in client_id/client_secret before using YouTube features."
    ;;
  *)
    echo "YouTube OAuth client config: invalid JSON"
    echo "  Fix $YOUTUBE_CLIENT_CONFIG_PATH before using YouTube features."
    ;;
esac
echo "Next steps:"
echo "  1. Verify or edit $YOUTUBE_CLIENT_CONFIG_PATH"
echo "  2. Confirm shared egress config if your hotspot uses a captive portal"
echo "  3. Restart services"
echo "  4. Open the web UI and use Start YouTube auth"
echo "Shared egress reconfigure:"
echo "  sudo env WLAN0_IFACE=$WLAN0_IFACE WLAN1_IFACE=$WLAN1_IFACE $INSTALL_DIR/configure_shared_egress.sh"
echo "AP profile create/reconfigure:"
echo "  sudo $INSTALL_DIR/configure_ap.sh"
echo "  Override once with env vars if needed:"
echo "  sudo env WLAN0_IFACE=$WLAN0_IFACE AP_CONNECTION_NAME=rpi-ap AP_SSID=Rpi_Ap_Secure AP_PASSWORD=12345678 AP_BAND=bg AP_CHANNEL=6 $INSTALL_DIR/configure_ap.sh"
echo "Restart services:"
echo "  sudo systemctl restart rpi-shared-egress.service"
echo "  sudo systemctl restart rpi-wlan1-ui.service"
echo "  sudo systemctl restart rpi-lcd-status.service"
echo "Run update service manually:"
echo "  sudo systemctl start --no-block rpi-ap-update.service"

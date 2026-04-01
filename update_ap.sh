#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DEFAULT_REPO_DIR=$SCRIPT_DIR
if [ ! -d "$DEFAULT_REPO_DIR/.git" ] && [ -d /home/pi/rpi_ap_tools_waveshare_bundle/.git ]; then
  DEFAULT_REPO_DIR=/home/pi/rpi_ap_tools_waveshare_bundle
elif [ ! -d "$DEFAULT_REPO_DIR/.git" ] && [ -d /home/pi/rpi_ap_tools_waveshare/.git ]; then
  DEFAULT_REPO_DIR=/home/pi/rpi_ap_tools_waveshare
fi

REPO_DIR=${REPO_DIR:-$DEFAULT_REPO_DIR}
INSTALL_SCRIPT=${INSTALL_SCRIPT:-install.sh}
INSTALL_DIR=${INSTALL_DIR:-/opt/rpi_ap_tools}
AP_CONFIG_FILE=${AP_CONFIG_FILE:-/etc/default/rpi_ap_tools_ap}

log() {
  printf '%s\n' "$*"
}

if [ ! -d "$REPO_DIR/.git" ]; then
  log "Repository not found at $REPO_DIR"
  exit 1
fi

cd "$REPO_DIR"

log "Updating repository in $REPO_DIR"
git fetch --all --prune
git pull --ff-only

if [ ! -x "$INSTALL_SCRIPT" ]; then
  chmod +x "$INSTALL_SCRIPT"
fi

log "Running installer"
"./$INSTALL_SCRIPT"

if [ -f "$AP_CONFIG_FILE" ]; then
  log "Reapplying AP config from $AP_CONFIG_FILE"
else
  log "AP config file not found at $AP_CONFIG_FILE; using configure_ap.sh defaults"
fi

sudo "$INSTALL_DIR"/configure_ap.sh
sudo systemctl restart rpi-shared-egress.service
sudo systemctl restart rpi-wlan1-ui.service
sudo systemctl restart rpi-lcd-status.service

log "Service status summary"
sudo systemctl --no-pager --full status rpi-wlan1-ui.service rpi-lcd-status.service rpi-shared-egress.service || true

log "Update complete"

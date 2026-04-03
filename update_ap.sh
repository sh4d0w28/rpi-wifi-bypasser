#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DEFAULT_REPO_DIR=$SCRIPT_DIR
if [ ! -d "$DEFAULT_REPO_DIR/.git" ] && [ -d /home/pi/rpi_ap_tools_waveshare_bundle/.git ]; then
  DEFAULT_REPO_DIR=/home/pi/rpi_ap_tools_waveshare_bundle
elif [ ! -d "$DEFAULT_REPO_DIR/.git" ] && [ -d /home/pi/rpi_ap_tools_waveshare/.git ]; then
  DEFAULT_REPO_DIR=/home/pi/rpi_ap_tools_waveshare
elif [ ! -d "$DEFAULT_REPO_DIR/.git" ] && [ -d /home/pi/rpi-wifi-bypasser/.git ]; then
  DEFAULT_REPO_DIR=/home/pi/rpi-wifi-bypasser
fi

REPO_DIR=${REPO_DIR:-$DEFAULT_REPO_DIR}
INSTALL_SCRIPT=${INSTALL_SCRIPT:-install.sh}
INSTALL_DIR=${INSTALL_DIR:-/opt/rpi_ap_tools}
AP_CONFIG_FILE=${AP_CONFIG_FILE:-/etc/default/rpi_ap_tools_ap}
UPDATE_REF_PATH=${UPDATE_REF_PATH:-/run/rpi_ap_tools_update_ref}

log() {
  printf '%s\n' "$*"
}

trim() {
  local value=${1-}
  value=${value#"${value%%[![:space:]]*}"}
  value=${value%"${value##*[![:space:]]}"}
  printf '%s' "$value"
}

normalize_ref() {
  local ref
  ref=$(trim "${1-}")
  for prefix in refs/heads/ refs/tags/ origin/; do
    if [[ $ref == "$prefix"* ]]; then
      ref=${ref#"$prefix"}
    fi
  done
  printf '%s' "$ref"
}

load_requested_ref() {
  local ref=""
  if [ -f "$UPDATE_REF_PATH" ]; then
    ref=$(cat "$UPDATE_REF_PATH" 2>/dev/null || true)
    rm -f "$UPDATE_REF_PATH"
  fi
  normalize_ref "$ref"
}

if [ ! -d "$REPO_DIR/.git" ]; then
  log "Repository not found at $REPO_DIR"
  exit 1
fi

cd "$REPO_DIR"

log "Updating repository in $REPO_DIR"
REQUESTED_REF=$(load_requested_ref)
git fetch --all --prune --tags

if [ -n "$REQUESTED_REF" ]; then
  log "Switching repository to $REQUESTED_REF"
  if git show-ref --verify --quiet "refs/remotes/origin/$REQUESTED_REF"; then
    git checkout -B "$REQUESTED_REF" "origin/$REQUESTED_REF"
    git branch --set-upstream-to="origin/$REQUESTED_REF" "$REQUESTED_REF" >/dev/null 2>&1 || true
  elif git show-ref --verify --quiet "refs/heads/$REQUESTED_REF"; then
    git checkout "$REQUESTED_REF"
    if git show-ref --verify --quiet "refs/remotes/origin/$REQUESTED_REF"; then
      git pull --ff-only origin "$REQUESTED_REF"
    fi
  elif git show-ref --verify --quiet "refs/tags/$REQUESTED_REF"; then
    git checkout --detach "$REQUESTED_REF"
  else
    log "Branch or tag not found: $REQUESTED_REF"
    exit 1
  fi
else
  CURRENT_BRANCH=$(git symbolic-ref --short -q HEAD || true)
  if [ -n "$CURRENT_BRANCH" ]; then
    git pull --ff-only
  else
    CURRENT_TAG=$(git describe --tags --exact-match 2>/dev/null || true)
    if [ -n "$CURRENT_TAG" ]; then
      log "Detached at tag $CURRENT_TAG; keeping current tag. Pick a branch or tag in the web UI to switch refs."
    else
      log "Detached HEAD; skipping git pull. Pick a branch or tag in the web UI to switch refs."
    fi
  fi
fi

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

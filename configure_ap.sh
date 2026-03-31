#!/usr/bin/env bash
set -euo pipefail

AP_CONFIG_FILE=${AP_CONFIG_FILE:-/etc/default/rpi_ap_tools_ap}

# Load persisted AP settings when present, but let explicit environment
# variables passed to this script win over the file.
WLAN0_IFACE_WAS_SET=${WLAN0_IFACE+x}
WLAN0_IFACE_ORIG=${WLAN0_IFACE-}
AP_CONNECTION_NAME_WAS_SET=${AP_CONNECTION_NAME+x}
AP_CONNECTION_NAME_ORIG=${AP_CONNECTION_NAME-}
AP_SSID_WAS_SET=${AP_SSID+x}
AP_SSID_ORIG=${AP_SSID-}
AP_PASSWORD_WAS_SET=${AP_PASSWORD+x}
AP_PASSWORD_ORIG=${AP_PASSWORD-}
AP_AUTH_MODE_WAS_SET=${AP_AUTH_MODE+x}
AP_AUTH_MODE_ORIG=${AP_AUTH_MODE-}
AP_BAND_WAS_SET=${AP_BAND+x}
AP_BAND_ORIG=${AP_BAND-}
AP_CHANNEL_WAS_SET=${AP_CHANNEL+x}
AP_CHANNEL_ORIG=${AP_CHANNEL-}

if [ -f "$AP_CONFIG_FILE" ]; then
  # shellcheck disable=SC1090
  . "$AP_CONFIG_FILE"
fi

if [ -n "$WLAN0_IFACE_WAS_SET" ]; then WLAN0_IFACE=$WLAN0_IFACE_ORIG; fi
if [ -n "$AP_CONNECTION_NAME_WAS_SET" ]; then AP_CONNECTION_NAME=$AP_CONNECTION_NAME_ORIG; fi
if [ -n "$AP_SSID_WAS_SET" ]; then AP_SSID=$AP_SSID_ORIG; fi
if [ -n "$AP_PASSWORD_WAS_SET" ]; then AP_PASSWORD=$AP_PASSWORD_ORIG; fi
if [ -n "$AP_AUTH_MODE_WAS_SET" ]; then AP_AUTH_MODE=$AP_AUTH_MODE_ORIG; fi
if [ -n "$AP_BAND_WAS_SET" ]; then AP_BAND=$AP_BAND_ORIG; fi
if [ -n "$AP_CHANNEL_WAS_SET" ]; then AP_CHANNEL=$AP_CHANNEL_ORIG; fi

WLAN_AP=${WLAN0_IFACE:-wlan0}
AP_CONNECTION_NAME=${AP_CONNECTION_NAME:-rpi-ap}
AP_SSID=${AP_SSID:-RPi-AP}
AP_PASSWORD=${AP_PASSWORD:-raspberry}
AP_AUTH_MODE=${AP_AUTH_MODE:-wpa-psk}
AP_BAND=${AP_BAND:-bg}
AP_CHANNEL=${AP_CHANNEL:-}

log() {
  printf '%s\n' "$*"
}

have_connection() {
  local name=$1
  nmcli -t -f NAME connection show 2>/dev/null | grep -Fxq "$name"
}

validate_password() {
  local auth_mode=$1
  local password=$2
  if [ "$auth_mode" = "open" ]; then
    return 0
  fi
  if [ ${#password} -lt 8 ] || [ ${#password} -gt 63 ]; then
    log "AP_PASSWORD must be 8..63 characters for WPA-PSK"
    exit 1
  fi
}

validate_password "$AP_AUTH_MODE" "$AP_PASSWORD"

log "Configuring NetworkManager AP profile"
log "  device: $WLAN_AP"
log "  profile: $AP_CONNECTION_NAME"
log "  ssid: $AP_SSID"
log "  auth: $AP_AUTH_MODE"
log "  config: $AP_CONFIG_FILE"

if have_connection "$AP_CONNECTION_NAME"; then
  nmcli connection modify "$AP_CONNECTION_NAME" \
    connection.interface-name "$WLAN_AP" \
    connection.autoconnect yes \
    802-11-wireless.ssid "$AP_SSID"
else
  nmcli connection add type wifi ifname "$WLAN_AP" con-name "$AP_CONNECTION_NAME" ssid "$AP_SSID"
  nmcli connection modify "$AP_CONNECTION_NAME" \
    connection.interface-name "$WLAN_AP" \
    connection.autoconnect yes
fi

nmcli connection modify "$AP_CONNECTION_NAME" \
  802-11-wireless.mode ap \
  802-11-wireless.band "$AP_BAND" \
  ipv4.method shared \
  ipv6.method disabled

if [ -n "$AP_CHANNEL" ]; then
  nmcli connection modify "$AP_CONNECTION_NAME" 802-11-wireless.channel "$AP_CHANNEL"
fi

if [ "$AP_AUTH_MODE" = "open" ]; then
  nmcli connection modify "$AP_CONNECTION_NAME" remove 802-11-wireless-security || true
else
  nmcli connection modify "$AP_CONNECTION_NAME" \
    802-11-wireless-security.key-mgmt wpa-psk \
    802-11-wireless-security.psk "$AP_PASSWORD"
fi

nmcli connection up "$AP_CONNECTION_NAME"

log "AP profile applied"
log "Bring-up command:"
log "  sudo nmcli connection up \"$AP_CONNECTION_NAME\""

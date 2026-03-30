#!/usr/bin/env bash
set -euo pipefail

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
  nmcli connection modify "$AP_CONNECTION_NAME" \
    802-11-wireless-security.key-mgmt "" \
    802-11-wireless-security.psk ""
else
  nmcli connection modify "$AP_CONNECTION_NAME" \
    802-11-wireless-security.key-mgmt wpa-psk \
    802-11-wireless-security.psk "$AP_PASSWORD"
fi

nmcli connection up "$AP_CONNECTION_NAME"

log "AP profile applied"
log "Bring-up command:"
log "  sudo nmcli connection up \"$AP_CONNECTION_NAME\""

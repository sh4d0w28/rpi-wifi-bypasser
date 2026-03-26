#!/usr/bin/env bash
set -euo pipefail

WLAN_AP=${WLAN0_IFACE:-wlan0}
WLAN_UP=${WLAN1_IFACE:-wlan1}
AP_CONNECTION_NAME=${AP_CONNECTION_NAME:-}
UPLINK_CONNECTION_NAME=${UPLINK_CONNECTION_NAME:-}

log() {
  printf '%s\n' "$*"
}

find_active_connection_name() {
  local device=$1
  nmcli -t -f DEVICE,NAME connection show --active 2>/dev/null | awk -F: -v dev="$device" '$1 == dev { print $2; exit }'
}

find_any_wifi_connection_name() {
  local device=$1
  nmcli -t -f NAME,DEVICE,TYPE connection show 2>/dev/null | awk -F: -v dev="$device" '$2 == dev && $3 == "802-11-wireless" { print $1; exit }'
}

require_connection_name() {
  local current_name=$1
  local device=$2
  if [ -n "$current_name" ]; then
    printf '%s\n' "$current_name"
    return 0
  fi

  current_name=$(find_active_connection_name "$device" || true)
  if [ -n "$current_name" ]; then
    printf '%s\n' "$current_name"
    return 0
  fi

  current_name=$(find_any_wifi_connection_name "$device" || true)
  if [ -n "$current_name" ]; then
    printf '%s\n' "$current_name"
    return 0
  fi

  return 1
}

device_state() {
  local device=$1
  nmcli -t -f DEVICE,STATE device status 2>/dev/null | awk -F: -v dev="$device" '$1 == dev { print $2; exit }'
}

AP_NAME=$(require_connection_name "$AP_CONNECTION_NAME" "$WLAN_AP" || true)
UP_NAME=$(require_connection_name "$UPLINK_CONNECTION_NAME" "$WLAN_UP" || true)
AP_STATE=$(device_state "$WLAN_AP" || true)
UP_STATE=$(device_state "$WLAN_UP" || true)

if [ -z "$UP_NAME" ]; then
  log "No NetworkManager Wi-Fi profile found for $WLAN_UP"
  exit 1
fi

log "Configuring shared egress"
log "  AP device: $WLAN_AP (${AP_NAME:-no-nm-profile}; state=${AP_STATE:-unknown})"
log "  uplink device: $WLAN_UP ($UP_NAME; state=${UP_STATE:-unknown})"

# If the AP is managed by NetworkManager, keep downstream clients behind the Pi's IPv4 address.
if [ -n "$AP_NAME" ] && [ "${AP_STATE:-}" != "unmanaged" ]; then
  nmcli connection modify "$AP_NAME" \
    connection.interface-name "$WLAN_AP" \
    connection.autoconnect yes \
    ipv4.method shared \
    ipv6.method disabled
else
  log "  leaving $WLAN_AP AP config in place (likely hostapd/dnsmasq-managed)"
fi

# Keep the uplink as a single stable IPv4 client toward the hotspot.
nmcli connection modify "$UP_NAME" \
  connection.interface-name "$WLAN_UP" \
  connection.autoconnect yes \
  ipv4.method auto \
  ipv6.method disabled \
  802-11-wireless.cloned-mac-address permanent

/usr/sbin/sysctl -w net.ipv6.conf.all.disable_ipv6=1 >/dev/null
/usr/sbin/sysctl -w net.ipv6.conf.default.disable_ipv6=1 >/dev/null
/usr/sbin/sysctl -w net.ipv6.conf."$WLAN_AP".disable_ipv6=1 >/dev/null 2>&1 || true
/usr/sbin/sysctl -w net.ipv6.conf."$WLAN_UP".disable_ipv6=1 >/dev/null 2>&1 || true

mkdir -p /etc/sysctl.d
cat >/etc/sysctl.d/90-rpi-ap-tools-egress.conf <<EOF
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.${WLAN_AP}.disable_ipv6 = 1
net.ipv6.conf.${WLAN_UP}.disable_ipv6 = 1
EOF

nmcli connection up "$UP_NAME" >/dev/null 2>&1 || true
if [ -n "$AP_NAME" ] && [ "${AP_STATE:-}" != "unmanaged" ]; then
  nmcli connection up "$AP_NAME" >/dev/null 2>&1 || true
fi

log "Shared egress configuration applied"
log "Downstream clients stay behind Pi-managed IPv4 NAT on $WLAN_AP"
log "IPv6 has been disabled to avoid per-client direct egress around the Pi"

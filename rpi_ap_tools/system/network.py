import re
from pathlib import Path

from rpi_ap_tools.core.files import read_config_value
from rpi_ap_tools.core.process import run_command


def read_ap_name(hostapd_conf, ap_config_file, wlan_iface="wlan0"):
    hostapd_conf = Path(hostapd_conf)
    if hostapd_conf.exists():
        for line in hostapd_conf.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("ssid="):
                return line.split("=", 1)[1].strip()

    active = run_command(
        ["nmcli", "-t", "-f", "GENERAL.CONNECTION", "device", "show", wlan_iface],
        check=False,
    )
    connection_name = ""
    for line in active.stdout.splitlines():
        if line.startswith("GENERAL.CONNECTION:"):
            connection_name = line.split(":", 1)[1].strip()
            break
    if connection_name:
        ssid = run_command(
            ["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", connection_name],
            check=False,
        ).stdout.strip()
        if ssid:
            return ssid

    configured = read_config_value(ap_config_file, "AP_SSID", "")
    if configured:
        return configured
    return "unknown"


def read_ipv4(device):
    proc = run_command(["ip", "-4", "-o", "addr", "show", "dev", device], check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return "-"
    match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+/\d+)", proc.stdout)
    return match.group(1) if match else "-"

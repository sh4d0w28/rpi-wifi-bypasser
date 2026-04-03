import json
import os
import re
import time
from pathlib import Path

from rpi_ap_tools.core.process import run_command
from rpi_ap_tools.system.network import read_ap_name, read_ipv4

WLAN_IFACE = os.environ.get("WLAN1_IFACE", "wlan1")
HOSTAPD_CONF = Path(os.environ.get("HOSTAPD_CONF", "/etc/hostapd/hostapd.conf"))
AP_CONFIG_FILE = Path(os.environ.get("AP_CONFIG_FILE", "/etc/default/rpi_ap_tools_ap"))
WIFI_DB_PATH = Path(os.environ.get("WIFI_DB_PATH", "/etc/rpi_ap_tools_wifi_db.json"))
WIFI_SCAN_CACHE_SEC = float(os.environ.get("WIFI_SCAN_CACHE_SEC", "10.0"))
WIFI_RESCAN_MIN_INTERVAL_SEC = float(os.environ.get("WIFI_RESCAN_MIN_INTERVAL_SEC", "30.0"))
SCAN_CACHE = {"rows": [], "cached_at": 0.0, "rescanned_at": 0.0}


def load_wifi_db():
    if not WIFI_DB_PATH.exists():
        return {}
    try:
        data = json.loads(WIFI_DB_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    cleaned = {}
    for ssid, item in data.items():
        if not isinstance(ssid, str) or not isinstance(item, dict):
            continue
        cleaned[ssid] = {"password": str(item.get("password", "")), "auth_type": str(item.get("auth_type", "wpa-psk"))}
    return cleaned


def save_wifi_db(db):
    WIFI_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    WIFI_DB_PATH.write_text(json.dumps(db, indent=2, sort_keys=True), encoding="utf-8")
    try:
        os.chmod(WIFI_DB_PATH, 0o600)
    except OSError:
        pass


def save_wifi_credentials(ssid, password, auth_type):
    db = load_wifi_db()
    db[ssid] = {"password": password or "", "auth_type": auth_type or "wpa-psk"}
    save_wifi_db(db)


def get_saved_wifi(ssid):
    return load_wifi_db().get(ssid, {})


def get_ap_name():
    return read_ap_name(HOSTAPD_CONF, AP_CONFIG_FILE, os.environ.get("WLAN0_IFACE", "wlan0"))


def get_active_connection():
    result = run_command(["nmcli", "-t", "-f", "DEVICE,NAME,TYPE,STATE", "connection", "show", "--active"], check=False)
    active = {"name": "", "state": "disconnected"}
    for line in result.stdout.splitlines():
        parts = line.split(":")
        if len(parts) >= 4 and parts[0] == WLAN_IFACE and parts[2] == "wifi":
            active["name"] = parts[1]
            active["state"] = parts[3]
            return active
    return active


def infer_auth_type(security: str) -> str:
    s = (security or "").upper()
    if s in ("", "--"):
        return "open"
    if "SAE" in s or "WPA3" in s:
        return "sae"
    return "wpa-psk"


def scan_wifi():
    now = time.time()
    if SCAN_CACHE["rows"] and now - SCAN_CACHE["cached_at"] < WIFI_SCAN_CACHE_SEC:
        return list(SCAN_CACHE["rows"])
    wifi_db = load_wifi_db()
    if now - SCAN_CACHE["rescanned_at"] >= WIFI_RESCAN_MIN_INTERVAL_SEC:
        run_command(["nmcli", "dev", "wifi", "rescan", "ifname", WLAN_IFACE], check=False)
        SCAN_CACHE["rescanned_at"] = now
    result = run_command(["nmcli", "-t", "-f", "SSID,SECURITY,SIGNAL", "dev", "wifi", "list", "ifname", WLAN_IFACE], check=False)
    seen = {}
    for line in result.stdout.splitlines():
        parts = re.split(r"(?<!\\):", line)
        if len(parts) < 3:
            continue
        ssid, security, signal = [p.replace("\\:", ":") for p in parts[:3]]
        if not ssid:
            continue
        row = {
            "ssid": ssid,
            "security": security or "--",
            "signal": signal or "0",
            "auth_type": infer_auth_type(security or "--"),
            "saved_password": bool(wifi_db.get(ssid, {}).get("password", "")),
            "saved_password_value": wifi_db.get(ssid, {}).get("password", ""),
            "saved_auth_type": wifi_db.get(ssid, {}).get("auth_type", ""),
        }
        if ssid not in seen or int(row["signal"]) > int(seen[ssid]["signal"]):
            seen[ssid] = row
    rows = list(seen.values())
    rows.sort(key=lambda x: int(x["signal"]), reverse=True)
    SCAN_CACHE["rows"] = rows
    SCAN_CACHE["cached_at"] = now
    return rows


def list_connections():
    result = run_command(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], check=False)
    return [line.split(":")[0] for line in result.stdout.splitlines() if len(line.split(":")) >= 2 and line.split(":")[1] == "802-11-wireless"]


def delete_connection_if_exists(name):
    if name in list_connections():
        run_command(["nmcli", "connection", "delete", name], check=False)


def connect_wifi(ssid, password, auth_type):
    profile_name = f"uplink-{ssid}"
    delete_connection_if_exists(profile_name)
    proc = run_command(["nmcli", "connection", "add", "type", "wifi", "ifname", WLAN_IFACE, "con-name", profile_name, "ssid", ssid], check=False)
    if proc.returncode != 0:
        return False, proc.stderr.strip() or proc.stdout.strip() or "Failed to add connection"
    cmds = [
        ["nmcli", "connection", "modify", profile_name, "connection.autoconnect", "yes"],
        ["nmcli", "connection", "modify", profile_name, "ipv4.method", "auto"],
        ["nmcli", "connection", "modify", profile_name, "ipv6.method", "auto"],
    ]
    if auth_type == "open":
        cmds.append(["nmcli", "connection", "modify", profile_name, "802-11-wireless-security.key-mgmt", ""])
    elif auth_type == "wpa-psk":
        cmds.extend([["nmcli", "connection", "modify", profile_name, "wifi-sec.key-mgmt", "wpa-psk"], ["nmcli", "connection", "modify", profile_name, "wifi-sec.psk", password]])
    elif auth_type == "sae":
        cmds.extend([["nmcli", "connection", "modify", profile_name, "wifi-sec.key-mgmt", "sae"], ["nmcli", "connection", "modify", profile_name, "wifi-sec.psk", password]])
    else:
        return False, f"Unsupported auth type: {auth_type}"
    for cmd in cmds:
        proc = run_command(cmd, check=False)
        if proc.returncode != 0:
            return False, proc.stderr.strip() or proc.stdout.strip() or "Failed to modify connection"
    proc = run_command(["nmcli", "connection", "up", profile_name], check=False)
    if proc.returncode != 0:
        return False, proc.stderr.strip() or proc.stdout.strip() or "Failed to bring up connection"
    return True, proc.stdout.strip() or "Connected"


def get_ip(device):
    return read_ipv4(device)

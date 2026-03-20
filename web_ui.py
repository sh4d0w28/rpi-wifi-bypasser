#!/usr/bin/env python3
import os
import re
import subprocess
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, flash

APP = Flask(__name__)
APP.secret_key = os.environ.get("FLASK_SECRET", "rpi-ap-tools")

WLAN_IFACE = os.environ.get("WLAN1_IFACE", "wlan1")
HOSTAPD_CONF = Path(os.environ.get("HOSTAPD_CONF", "/etc/hostapd/hostapd.conf"))


def run(cmd, check=True):
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def get_ap_name():
    if HOSTAPD_CONF.exists():
        for line in HOSTAPD_CONF.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("ssid="):
                return line.split("=", 1)[1].strip()
    return "unknown"


def get_active_connection():
    result = run(["nmcli", "-t", "-f", "DEVICE,NAME,TYPE,STATE", "connection", "show", "--active"], check=False)
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
    run(["nmcli", "dev", "wifi", "rescan", "ifname", WLAN_IFACE], check=False)
    result = run(["nmcli", "-t", "-f", "SSID,SECURITY,SIGNAL", "dev", "wifi", "list", "ifname", WLAN_IFACE], check=False)
    seen = {}
    for line in result.stdout.splitlines():
        parts = re.split(r'(?<!\\):', line)
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
        }
        # keep strongest duplicate SSID
        if ssid not in seen or int(row["signal"]) > int(seen[ssid]["signal"]):
            seen[ssid] = row

    rows = list(seen.values())
    rows.sort(key=lambda x: int(x["signal"]), reverse=True)
    return rows


def list_connections():
    result = run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], check=False)
    items = []
    for line in result.stdout.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[1] == "802-11-wireless":
            items.append(parts[0])
    return items


def delete_connection_if_exists(name):
    if name in list_connections():
        run(["nmcli", "connection", "delete", name], check=False)


def connect_wifi(ssid, password, auth_type):
    profile_name = f"uplink-{ssid}"
    delete_connection_if_exists(profile_name)

    proc = run(["nmcli", "connection", "add", "type", "wifi", "ifname", WLAN_IFACE, "con-name", profile_name, "ssid", ssid], check=False)
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
        cmds.extend([
            ["nmcli", "connection", "modify", profile_name, "wifi-sec.key-mgmt", "wpa-psk"],
            ["nmcli", "connection", "modify", profile_name, "wifi-sec.psk", password],
        ])
    elif auth_type == "sae":
        cmds.extend([
            ["nmcli", "connection", "modify", profile_name, "wifi-sec.key-mgmt", "sae"],
            ["nmcli", "connection", "modify", profile_name, "wifi-sec.psk", password],
        ])
    else:
        return False, f"Unsupported auth type: {auth_type}"

    for cmd in cmds:
        proc = run(cmd, check=False)
        if proc.returncode != 0:
            return False, proc.stderr.strip() or proc.stdout.strip() or "Failed to modify connection"

    proc = run(["nmcli", "connection", "up", profile_name], check=False)
    if proc.returncode != 0:
        return False, proc.stderr.strip() or proc.stdout.strip() or "Failed to bring up connection"

    return True, proc.stdout.strip() or "Connected"


def get_ip(device):
    proc = run(["ip", "-4", "-o", "addr", "show", "dev", device], check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return "-"
    m = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+/\d+)', proc.stdout)
    return m.group(1) if m else "-"


@APP.route("/", methods=["GET"])
def index():
    wifi_list = scan_wifi()
    return render_template(
        "index.html",
        ap_name=get_ap_name(),
        active=get_active_connection(),
        wifi_list=wifi_list,
        wlan1_ip=get_ip(WLAN_IFACE),
        wlan0_ip=get_ip("wlan0"),
        top_wifi=wifi_list[:6],
    )


@APP.route("/connect", methods=["POST"])
def connect():
    ssid = request.form.get("ssid", "").strip()
    password = request.form.get("password", "")
    auth_type = request.form.get("auth_type", "wpa-psk").strip()

    if not ssid:
        flash("SSID is required", "error")
        return redirect(url_for("index"))

    ok, msg = connect_wifi(ssid, password, auth_type)
    flash(msg, "success" if ok else "error")
    return redirect(url_for("index"))


@APP.route("/disconnect", methods=["POST"])
def disconnect():
    active = get_active_connection()
    if active["name"]:
        proc = run(["nmcli", "connection", "down", active["name"]], check=False)
        flash(proc.stdout.strip() or proc.stderr.strip() or "Disconnected", "success" if proc.returncode == 0 else "error")
    else:
        flash("No active wlan1 connection", "error")
    return redirect(url_for("index"))


if __name__ == "__main__":
    APP.run(host="0.0.0.0", port=8080, debug=False)
#!/usr/bin/env python3
import json
import os
import re
import subprocess
import time
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, flash
from youtube_live import (
    YouTubeLiveError,
    create_stream_bundle,
    get_auth_status,
    load_stream_state,
    qr_data_uri,
    start_device_authorization,
    poll_device_authorization,
)

APP = Flask(__name__)
APP.secret_key = os.environ.get("FLASK_SECRET", "rpi-ap-tools")

WLAN_IFACE = os.environ.get("WLAN1_IFACE", "wlan1")
HOSTAPD_CONF = Path(os.environ.get("HOSTAPD_CONF", "/etc/hostapd/hostapd.conf"))
WIFI_DB_PATH = Path(os.environ.get("WIFI_DB_PATH", "/etc/rpi_ap_tools_wifi_db.json"))
STATUS_PATH = Path(os.environ.get("STATUS_PATH", "/run/rpi_ap_tools_status.json"))
CAPTIVE_PORTAL_ACK_CMD = os.environ.get("CAPTIVE_PORTAL_ACK_CMD", "").strip()
WIFI_SCAN_CACHE_SEC = float(os.environ.get("WIFI_SCAN_CACHE_SEC", "10.0"))
WIFI_RESCAN_MIN_INTERVAL_SEC = float(os.environ.get("WIFI_RESCAN_MIN_INTERVAL_SEC", "30.0"))
SCAN_CACHE = {"rows": [], "cached_at": 0.0, "rescanned_at": 0.0}


def run(cmd, check=True):
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


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
        cleaned[ssid] = {
            "password": str(item.get("password", "")),
            "auth_type": str(item.get("auth_type", "wpa-psk")),
        }
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
    db[ssid] = {
        "password": password or "",
        "auth_type": auth_type or "wpa-psk",
    }
    save_wifi_db(db)


def get_saved_wifi(ssid):
    return load_wifi_db().get(ssid, {})


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
    now = time.time()
    if SCAN_CACHE["rows"] and now - SCAN_CACHE["cached_at"] < WIFI_SCAN_CACHE_SEC:
        return list(SCAN_CACHE["rows"])

    wifi_db = load_wifi_db()
    if now - SCAN_CACHE["rescanned_at"] >= WIFI_RESCAN_MIN_INTERVAL_SEC:
        run(["nmcli", "dev", "wifi", "rescan", "ifname", WLAN_IFACE], check=False)
        SCAN_CACHE["rescanned_at"] = now
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
            "saved_password": bool(wifi_db.get(ssid, {}).get("password", "")),
            "saved_password_value": wifi_db.get(ssid, {}).get("password", ""),
            "saved_auth_type": wifi_db.get(ssid, {}).get("auth_type", ""),
        }
        # keep strongest duplicate SSID
        if ssid not in seen or int(row["signal"]) > int(seen[ssid]["signal"]):
            seen[ssid] = row

    rows = list(seen.values())
    rows.sort(key=lambda x: int(x["signal"]), reverse=True)
    SCAN_CACHE["rows"] = rows
    SCAN_CACHE["cached_at"] = now
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


def load_runtime_status():
    if not STATUS_PATH.exists():
        return {}
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def run_portal_ack():
    if not CAPTIVE_PORTAL_ACK_CMD:
        return False, "No captive portal action configured"
    try:
        proc = subprocess.run(
            CAPTIVE_PORTAL_ACK_CMD,
            text=True,
            capture_output=True,
            shell=True,
            check=False,
            timeout=20,
        )
        message = proc.stdout.strip() or proc.stderr.strip() or "Portal action finished"
        return proc.returncode == 0, message
    except subprocess.TimeoutExpired:
        return False, "Portal action timed out"
    except OSError as exc:
        return False, str(exc)


@APP.route("/", methods=["GET"])
def index():
    wifi_list = scan_wifi()
    runtime = load_runtime_status()
    youtube_auth = get_auth_status()
    youtube_stream = load_stream_state()
    return render_template(
        "index.html",
        ap_name=get_ap_name(),
        active=get_active_connection(),
        wifi_list=wifi_list,
        wlan1_ip=get_ip(WLAN_IFACE),
        wlan0_ip=get_ip("wlan0"),
        top_wifi=wifi_list[:6],
        runtime=runtime,
        portal_ack_available=bool(CAPTIVE_PORTAL_ACK_CMD),
        youtube_auth=youtube_auth,
        youtube_stream=youtube_stream,
        youtube_qr=qr_data_uri((youtube_stream or {}).get("qr_payload", "")),
    )


@APP.route("/connect", methods=["POST"])
def connect():
    ssid = request.form.get("ssid", "").strip()
    password = request.form.get("password", "")
    auth_type = request.form.get("auth_type", "wpa-psk").strip()

    if not ssid:
        flash("SSID is required", "error")
        return redirect(url_for("index"))

    saved = get_saved_wifi(ssid)
    if auth_type != "open" and not password and saved.get("password"):
        password = saved["password"]

    if auth_type != "open" and not password:
        flash("Password is required for secured Wi-Fi", "error")
        return redirect(url_for("index"))

    ok, msg = connect_wifi(ssid, password, auth_type)
    if ok:
        save_wifi_credentials(ssid, "" if auth_type == "open" else password, auth_type)
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


@APP.route("/portal-ack", methods=["POST"])
def portal_ack():
    ok, msg = run_portal_ack()
    flash(msg, "success" if ok else "error")
    return redirect(url_for("index"))


@APP.route("/youtube/device/start", methods=["POST"])
def youtube_device_start():
    try:
        state = start_device_authorization()
        flash(
            f"Open {state.get('verification_url') or state.get('verification_url_complete')}, then enter code {state.get('user_code')}.",
            "success",
        )
    except YouTubeLiveError as exc:
        flash(str(exc), "error")
    return redirect(url_for("index"))


@APP.route("/youtube/device/poll", methods=["POST"])
def youtube_device_poll():
    try:
        poll_device_authorization()
        flash("YouTube authorization completed.", "success")
    except YouTubeLiveError as exc:
        flash(str(exc), "error")
    return redirect(url_for("index"))


@APP.route("/youtube/create", methods=["POST"])
def youtube_create():
    title = request.form.get("title", "").strip()
    ap_ip = get_ip("wlan0").split("/", 1)[0]
    try:
        state = create_stream_bundle(ap_ip=ap_ip, title=title)
        flash(f"YouTube stream created: {state.get('title', 'untitled')}", "success")
    except YouTubeLiveError as exc:
        flash(str(exc), "error")
    return redirect(url_for("index"))


if __name__ == "__main__":
    APP.run(host="0.0.0.0", port=8080, debug=False)

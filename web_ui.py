#!/usr/bin/env python3
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from flask import Flask, Response, flash, redirect, render_template, render_template_string, request, send_file, url_for
from youtube_live import (
    DEFAULT_OVERLAY_HTML,
    YouTubeLiveError,
    ensure_overlay_html_exists,
    get_auth_status,
    list_audio_modes,
    list_fps_modes,
    list_rotation_modes,
    load_creation_log,
    load_creation_state,
    load_overlay_state,
    load_stream_state,
    ensure_proxy_relay_running,
    refresh_proxy_overlay,
    save_overlay_state,
    set_proxy_audio_mode,
    set_proxy_fps_mode,
    set_proxy_rotation_mode,
    start_device_authorization,
    poll_device_authorization,
    start_stream_creation,
)

APP = Flask(__name__)
APP.secret_key = os.environ.get("FLASK_SECRET", "rpi-ap-tools")

WLAN_IFACE = os.environ.get("WLAN1_IFACE", "wlan1")
HOSTAPD_CONF = Path(os.environ.get("HOSTAPD_CONF", "/etc/hostapd/hostapd.conf"))
AP_CONFIG_FILE = Path(os.environ.get("AP_CONFIG_FILE", "/etc/default/rpi_ap_tools_ap"))
WIFI_DB_PATH = Path(os.environ.get("WIFI_DB_PATH", "/etc/rpi_ap_tools_wifi_db.json"))
STATUS_PATH = Path(os.environ.get("STATUS_PATH", "/run/rpi_ap_tools_status.json"))
CAPTIVE_PORTAL_ACK_CMD = os.environ.get("CAPTIVE_PORTAL_ACK_CMD", "").strip()
UPDATE_SERVICE_NAME = os.environ.get("UPDATE_SERVICE_NAME", "rpi-ap-update.service").strip() or "rpi-ap-update.service"
UPDATE_SCRIPT_PATH = Path("/home/pi/update_ap.sh")
WIFI_SCAN_CACHE_SEC = float(os.environ.get("WIFI_SCAN_CACHE_SEC", "10.0"))
WIFI_RESCAN_MIN_INTERVAL_SEC = float(os.environ.get("WIFI_RESCAN_MIN_INTERVAL_SEC", "30.0"))
OVERLAY_RENDER_HTML_PATH = Path(os.environ.get("YOUTUBE_OVERLAY_RENDER_HTML_PATH", "/run/rpi_ap_tools_youtube_overlay_rendered.html"))
OVERLAY_RENDERER_BIN = os.environ.get("YOUTUBE_OVERLAY_BROWSER_BIN", "").strip()
RELAY_ENSURE_INTERVAL_SEC = max(1.0, float(os.environ.get("YOUTUBE_PROXY_ENSURE_INTERVAL_SEC", "1.0")))
SCAN_CACHE = {"rows": [], "cached_at": 0.0, "rescanned_at": 0.0}
OVERLAY_RENDER_LOCK = threading.Lock()
OVERLAY_RENDERER_THREAD = None
RELAY_WATCHDOG_THREAD = None
LOGGER = logging.getLogger(__name__)


def run(cmd, check=True):
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def read_config_value(path, key, default=""):
    if not path.exists():
        return default
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            current_key, value = stripped.split("=", 1)
            if current_key.strip() == key:
                return value.strip().strip("'\"") or default
    except Exception:
        return default
    return default


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
    wlan0 = os.environ.get("WLAN0_IFACE", "wlan0")
    active = run(["nmcli", "-t", "-f", "GENERAL.CONNECTION", "device", "show", wlan0], check=False)
    connection_name = ""
    for line in active.stdout.splitlines():
        if line.startswith("GENERAL.CONNECTION:"):
            connection_name = line.split(":", 1)[1].strip()
            break
    if connection_name:
        ssid = run(["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", connection_name], check=False).stdout.strip()
        if ssid:
            return ssid
    configured = read_config_value(AP_CONFIG_FILE, "AP_SSID", "")
    if configured:
        return configured
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


def overlay_html_path():
    return Path(load_overlay_state().get("html_path") or "")


def overlay_png_path():
    return Path(load_overlay_state().get("png_path") or "")


def load_overlay_html():
    ensure_overlay_html_exists()
    path = overlay_html_path()
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_OVERLAY_HTML


def save_overlay_html(text):
    path = overlay_html_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _overlay_template_context():
    runtime = load_runtime_status()
    return {
        "ap_name": get_ap_name(),
        "active": get_active_connection(),
        "runtime": runtime,
        "wlan0_ip": get_ip("wlan0"),
        "wlan1_ip": get_ip(WLAN_IFACE),
        "now_local": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _overlay_renderer_bin():
    if OVERLAY_RENDERER_BIN:
        return OVERLAY_RENDERER_BIN
    for candidate in ("chromium-browser", "chromium", "google-chrome", "google-chrome-stable"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return ""


def render_overlay_png(force=False):
    with OVERLAY_RENDER_LOCK:
        overlay = load_overlay_state()
        html_source = load_overlay_html()
        if not overlay.get("enabled") and not force:
            return False, "Overlay disabled"
        renderer_bin = _overlay_renderer_bin()
        if not renderer_bin:
            overlay["last_render_error"] = "No Chromium-compatible browser found"
            save_overlay_state(overlay)
            return False, overlay["last_render_error"]
        with APP.app_context():
            rendered_html = render_template_string(html_source, **_overlay_template_context())
        OVERLAY_RENDER_HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
        OVERLAY_RENDER_HTML_PATH.write_text(rendered_html, encoding="utf-8")
        png_path = Path(overlay["png_path"])
        png_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            renderer_bin,
            "--headless",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--hide-scrollbars",
            "--default-background-color=00000000",
            f"--window-size={overlay['width']},{overlay['height']}",
            f"--screenshot={png_path}",
            OVERLAY_RENDER_HTML_PATH.as_uri(),
        ]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            cmd.insert(1, "--no-sandbox")
        proc = subprocess.run(cmd, text=True, capture_output=True, check=False, timeout=30)
        if proc.returncode != 0:
            message = proc.stderr.strip() or proc.stdout.strip() or "Overlay render failed"
            overlay["last_render_error"] = message
            save_overlay_state(overlay)
            return False, message
        overlay["last_render_error"] = ""
        overlay["last_rendered_at"] = time.time()
        save_overlay_state(overlay)
        return True, f"Overlay rendered to {png_path}"


def _overlay_renderer_loop():
    while True:
        overlay = load_overlay_state()
        refresh_sec = max(5, int(overlay.get("refresh_sec") or 10))
        last_rendered_at = float(overlay.get("last_rendered_at") or 0)
        if overlay.get("enabled") and time.time() - last_rendered_at >= refresh_sec:
            render_overlay_png(force=True)
        time.sleep(1.0)


def start_overlay_renderer_thread():
    global OVERLAY_RENDERER_THREAD
    if OVERLAY_RENDERER_THREAD and OVERLAY_RENDERER_THREAD.is_alive():
        return
    ensure_overlay_html_exists()
    OVERLAY_RENDERER_THREAD = threading.Thread(target=_overlay_renderer_loop, name="overlay-renderer", daemon=True)
    OVERLAY_RENDERER_THREAD.start()


def _relay_watchdog_loop():
    while True:
        try:
            ensure_proxy_relay_running()
        except YouTubeLiveError as exc:
            LOGGER.warning("Proxy relay watchdog restart failed: %s", exc)
        except Exception as exc:
            LOGGER.exception("Proxy relay watchdog crashed: %s", exc)
        time.sleep(RELAY_ENSURE_INTERVAL_SEC)


def start_relay_watchdog_thread():
    global RELAY_WATCHDOG_THREAD
    if RELAY_WATCHDOG_THREAD and RELAY_WATCHDOG_THREAD.is_alive():
        return
    RELAY_WATCHDOG_THREAD = threading.Thread(target=_relay_watchdog_loop, name="relay-watchdog", daemon=True)
    RELAY_WATCHDOG_THREAD.start()


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


def systemd_show(unit_name, properties):
    proc = run(["systemctl", "show", unit_name, f"--property={','.join(properties)}"], check=False)
    if proc.returncode != 0:
        return {}

    data = {}
    for line in proc.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value
    return data


def load_update_status():
    props = systemd_show(
        UPDATE_SERVICE_NAME,
        [
            "LoadState",
            "ActiveState",
            "SubState",
            "Result",
            "ExecMainStatus",
            "ExecMainStartTimestamp",
        ],
    )
    script_exists = UPDATE_SCRIPT_PATH.is_file()
    if not props:
        return {
            "service_name": UPDATE_SERVICE_NAME,
            "script_path": str(UPDATE_SCRIPT_PATH),
            "script_exists": script_exists,
            "service_installed": False,
            "running": False,
            "load_state": "unknown",
            "active_state": "unknown",
            "sub_state": "unknown",
            "summary": "update service unavailable",
            "status_class": "err",
            "last_started": "",
            "can_start": False,
        }

    load_state = props.get("LoadState", "unknown")
    active_state = props.get("ActiveState", "unknown")
    sub_state = props.get("SubState", "unknown")
    result = props.get("Result", "")
    exec_main_status = props.get("ExecMainStatus", "")
    last_started = props.get("ExecMainStartTimestamp", "")
    running = active_state in ("active", "activating", "reloading")
    service_installed = load_state not in ("not-found", "unknown", "")

    if not service_installed:
        summary = "update service not installed"
        status_class = "err"
    elif running:
        summary = "update is running"
        status_class = ""
    elif last_started and result and result != "success":
        detail = f" ({result}"
        if exec_main_status and exec_main_status != "0":
            detail += f", exit {exec_main_status}"
        detail += ")"
        summary = f"last run failed{detail}"
        status_class = "err"
    elif last_started:
        summary = "last run succeeded"
        status_class = "ok"
    else:
        summary = "idle"
        status_class = ""

    return {
        "service_name": UPDATE_SERVICE_NAME,
        "script_path": str(UPDATE_SCRIPT_PATH),
        "script_exists": script_exists,
        "service_installed": service_installed,
        "running": running,
        "load_state": load_state,
        "active_state": active_state,
        "sub_state": sub_state,
        "summary": summary,
        "status_class": status_class,
        "last_started": last_started,
        "can_start": service_installed and script_exists and not running,
    }


def start_update_service():
    status = load_update_status()
    if not status["service_installed"]:
        return False, f"{UPDATE_SERVICE_NAME} is not installed"
    if not status["script_exists"]:
        return False, f"Update script not found: {UPDATE_SCRIPT_PATH}"
    if status["running"]:
        return False, "Update already running"

    proc = run(["systemctl", "start", "--no-block", UPDATE_SERVICE_NAME], check=False)
    if proc.returncode != 0:
        return False, proc.stderr.strip() or proc.stdout.strip() or f"Failed to start {UPDATE_SERVICE_NAME}"
    return True, "Update started. The web UI may restart while install runs."


@APP.route("/", methods=["GET"])
def index():
    wifi_list = scan_wifi()
    runtime = load_runtime_status()
    overlay = load_overlay_state()
    youtube_auth = get_auth_status()
    youtube_creation = load_creation_state()
    youtube_creation_log = load_creation_log()
    youtube_stream = load_stream_state()
    youtube_ready = bool(
        youtube_auth.get("client_configured")
        and youtube_auth.get("authorized")
        and (youtube_auth.get("validation") or {}).get("ok")
    )
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
        update_status=load_update_status(),
        youtube_auth=youtube_auth,
        youtube_ready=youtube_ready,
        youtube_creation=youtube_creation,
        youtube_creation_log=youtube_creation_log,
        youtube_stream=youtube_stream,
        youtube_audio_modes=list_audio_modes(),
        youtube_fps_modes=list_fps_modes(),
        youtube_rotation_modes=list_rotation_modes(),
        overlay=overlay,
        overlay_html=load_overlay_html(),
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


@APP.route("/update", methods=["POST"])
def update():
    ok, msg = start_update_service()
    flash(msg, "success" if ok else "error")
    return redirect(url_for("index"))


@APP.route("/overlay/save", methods=["POST"])
def overlay_save():
    current = load_overlay_state()
    previous_structural = {key: current.get(key) for key in ("enabled", "x", "y", "width", "height", "opacity")}
    updated = {
        **current,
        "enabled": request.form.get("enabled") == "on",
        "x": request.form.get("x", current.get("x")),
        "y": request.form.get("y", current.get("y")),
        "width": request.form.get("width", current.get("width")),
        "height": request.form.get("height", current.get("height")),
        "opacity": request.form.get("opacity", current.get("opacity")),
        "refresh_sec": request.form.get("refresh_sec", current.get("refresh_sec")),
    }
    save_overlay_state(updated)
    save_overlay_html(request.form.get("html", load_overlay_html()))
    ok, message = render_overlay_png(force=True)
    flash(message, "success" if ok else "error")
    new_state = load_overlay_state()
    new_structural = {key: new_state.get(key) for key in ("enabled", "x", "y", "width", "height", "opacity")}
    if previous_structural != new_structural:
        try:
            refresh_proxy_overlay()
            flash("Running relay reloaded with new overlay layout.", "success")
        except YouTubeLiveError:
            pass
    return redirect(url_for("index", tab="overlay"))


@APP.route("/overlay/render", methods=["POST"])
def overlay_render():
    ok, message = render_overlay_png(force=True)
    flash(message, "success" if ok else "error")
    return redirect(url_for("index", tab="overlay"))


@APP.route("/overlay/preview", methods=["GET"])
def overlay_preview():
    path = overlay_png_path()
    if not path.is_file():
        return Response("No overlay PNG rendered yet.\n", mimetype="text/plain", status=404)
    return send_file(path, mimetype="image/png", max_age=0)


@APP.route("/youtube/device/start", methods=["POST"])
def youtube_device_start():
    try:
        state = start_device_authorization()
        flash(
            f"Open {state.get('verification_url_complete') or state.get('verification_url')}, then enter code {state.get('user_code')}.",
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
    audio_mode = request.form.get("audio_mode", "normal").strip()
    rotation = request.form.get("rotation", "0").strip()
    fps_mode = request.form.get("fps_mode", "original").strip()
    ap_ip = get_ip("wlan0").split("/", 1)[0]
    runtime = load_runtime_status()
    probe = runtime.get("probe", {}) if isinstance(runtime, dict) else {}
    auth = get_auth_status()
    if probe.get("auth_required") or not auth.get("authorized"):
        flash("AUTH FIRST", "error")
        return redirect(url_for("index"))
    try:
        start_stream_creation(
            ap_ip=ap_ip,
            title=title,
            audio_mode=audio_mode,
            rotation=rotation,
            fps_mode=fps_mode,
        )
        flash("YouTube stream creation started.", "success")
    except YouTubeLiveError as exc:
        flash(str(exc), "error")
    return redirect(url_for("index"))


@APP.route("/youtube/audio-mode", methods=["POST"])
def youtube_audio_mode():
    mode = request.form.get("mode", "").strip()
    try:
        state = set_proxy_audio_mode(mode)
        flash(f"YouTube relay audio mode: {state.get('audio_mode_label', 'Normal')}", "success")
    except YouTubeLiveError as exc:
        flash(str(exc), "error")
    return redirect(url_for("index"))


@APP.route("/youtube/rotation", methods=["POST"])
def youtube_rotation_mode():
    mode = request.form.get("mode", "").strip()
    try:
        state = set_proxy_rotation_mode(mode)
        flash(
            f"YouTube relay rotation: {state.get('rotation_label', 'Off')} (relay reconnects briefly).",
            "success",
        )
    except YouTubeLiveError as exc:
        flash(str(exc), "error")
    return redirect(url_for("index"))


@APP.route("/youtube/fps-mode", methods=["POST"])
def youtube_fps_mode():
    mode = request.form.get("mode", "").strip()
    try:
        state = set_proxy_fps_mode(mode)
        flash(
            f"YouTube relay FPS: {state.get('fps_mode_label', 'Original')} (relay reconnects briefly).",
            "success",
        )
    except YouTubeLiveError as exc:
        flash(str(exc), "error")
    return redirect(url_for("index"))


@APP.route("/youtube/creation-log", methods=["GET"])
def youtube_creation_log():
    payload = load_creation_log(max_bytes=None)
    text = payload.get("text", "")
    if not text:
        text = f"No creation log yet.\nExpected path: {payload.get('path', '-')}\n"
    return Response(text, mimetype="text/plain")


start_overlay_renderer_thread()
start_relay_watchdog_thread()


if __name__ == "__main__":
    APP.run(host="0.0.0.0", port=8080, debug=False)

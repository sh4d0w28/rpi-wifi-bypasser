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
from urllib.parse import quote

from flask import Response, current_app, render_template, render_template_string, send_file

from rpi_ap_tools.core.files import load_json_file, read_config_value
from rpi_ap_tools.core.process import run_command
from rpi_ap_tools.system.network import read_ap_name, read_ipv4
from youtube_live import (
    DEFAULT_OVERLAY_HTML,
    YouTubeLiveError,
    ensure_overlay_html_exists,
    ensure_proxy_relay_running,
    get_auth_status,
    list_audio_modes,
    list_fps_modes,
    list_rotation_modes,
    load_creation_log,
    load_creation_state,
    load_overlay_state,
    load_stream_state,
    poll_device_authorization,
    refresh_proxy_overlay,
    save_overlay_state,
    set_proxy_audio_mode,
    set_proxy_fps_mode,
    set_proxy_rotation_mode,
    start_device_authorization,
    start_stream_creation,
)

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
OVERLAY_WEATHER_CITY = os.environ.get("OVERLAY_WEATHER_CITY", "Bangkok").strip() or "Bangkok"
OVERLAY_WEATHER_COUNTRY = os.environ.get("OVERLAY_WEATHER_COUNTRY", "Thailand").strip()
OVERLAY_WEATHER_LAT = os.environ.get("OVERLAY_WEATHER_LAT", "").strip()
OVERLAY_WEATHER_LON = os.environ.get("OVERLAY_WEATHER_LON", "").strip()
OVERLAY_WEATHER_REFRESH_SEC = max(300, int(os.environ.get("OVERLAY_WEATHER_REFRESH_SEC", "600") or "600"))
OVERLAY_WEATHER_CACHE_PATH = Path(os.environ.get("OVERLAY_WEATHER_CACHE_PATH", "/run/rpi_ap_tools_overlay_weather.json"))
SCAN_CACHE = {"rows": [], "cached_at": 0.0, "rescanned_at": 0.0}
OVERLAY_RENDER_LOCK = threading.Lock()
OVERLAY_RENDERER_THREAD = None
RELAY_WATCHDOG_THREAD = None
LOGGER = logging.getLogger(__name__)


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
    items = []
    for line in result.stdout.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[1] == "802-11-wireless":
            items.append(parts[0])
    return items


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


def load_runtime_status():
    data = load_json_file(STATUS_PATH, {})
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


def _weather_code_label(code):
    labels = {
        0: "Clear sky",
        1: "Mostly clear",
        2: "Partly cloudy",
        3: "Overcast",
        45: "Fog",
        48: "Rime fog",
        51: "Light drizzle",
        53: "Drizzle",
        55: "Heavy drizzle",
        61: "Light rain",
        63: "Rain",
        65: "Heavy rain",
        66: "Freezing rain",
        67: "Heavy freezing rain",
        71: "Light snow",
        73: "Snow",
        75: "Heavy snow",
        77: "Snow grains",
        80: "Rain showers",
        81: "Heavy showers",
        82: "Violent showers",
        85: "Snow showers",
        86: "Heavy snow showers",
        95: "Thunderstorm",
        96: "Thunderstorm and hail",
        99: "Severe thunderstorm",
    }
    try:
        return labels.get(int(code), "Weather")
    except (TypeError, ValueError):
        return "Weather"


def _load_json_file(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _save_json_file(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _weather_cache_default():
    return {
        "city": OVERLAY_WEATHER_CITY,
        "country": OVERLAY_WEATHER_COUNTRY,
        "temperature_c": None,
        "apparent_temperature_c": None,
        "wind_kph": None,
        "summary": "Weather unavailable",
        "updated_at": 0,
        "source": "cache",
    }


def _resolve_weather_coordinates():
    if OVERLAY_WEATHER_LAT and OVERLAY_WEATHER_LON:
        try:
            return float(OVERLAY_WEATHER_LAT), float(OVERLAY_WEATHER_LON), OVERLAY_WEATHER_CITY, OVERLAY_WEATHER_COUNTRY
        except ValueError:
            pass
    query = OVERLAY_WEATHER_CITY
    if OVERLAY_WEATHER_COUNTRY:
        query = f"{query}, {OVERLAY_WEATHER_COUNTRY}"
    url = "https://geocoding-api.open-meteo.com/v1/search?" f"name={quote(query)}&count=1&language=en&format=json"
    payload = run_json_request(url)
    results = payload.get("results") or []
    if not results:
        raise RuntimeError(f"Weather location not found: {query}")
    item = results[0]
    return float(item.get("latitude")), float(item.get("longitude")), item.get("name") or OVERLAY_WEATHER_CITY, item.get("country") or OVERLAY_WEATHER_COUNTRY


def run_json_request(url):
    import urllib.request

    with urllib.request.urlopen(url, timeout=15) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def load_overlay_weather(force_refresh=False):
    cache = _load_json_file(OVERLAY_WEATHER_CACHE_PATH, _weather_cache_default())
    now = time.time()
    if not force_refresh and now - float(cache.get("updated_at") or 0) < OVERLAY_WEATHER_REFRESH_SEC:
        return cache
    try:
        lat, lon, city, country = _resolve_weather_coordinates()
        forecast_url = (
            "https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
            "&timezone=auto"
        )
        payload = run_json_request(forecast_url)
        current = payload.get("current") or {}
        next_cache = {
            "city": city,
            "country": country,
            "latitude": lat,
            "longitude": lon,
            "temperature_c": current.get("temperature_2m"),
            "apparent_temperature_c": current.get("apparent_temperature"),
            "wind_kph": current.get("wind_speed_10m"),
            "summary": _weather_code_label(current.get("weather_code")),
            "updated_at": now,
            "source": "open-meteo",
        }
        _save_json_file(OVERLAY_WEATHER_CACHE_PATH, next_cache)
        cache = next_cache
    except Exception as exc:
        cache.setdefault("summary", "Weather unavailable")
        cache["error"] = str(exc)
        cache["source"] = "cache"
    temperature = cache.get("temperature_c")
    apparent = cache.get("apparent_temperature_c")
    wind = cache.get("wind_kph")
    cache["temperature_text"] = "--" if temperature is None else f"{round(float(temperature))}C"
    cache["apparent_text"] = "--" if apparent is None else f"Feels {round(float(apparent))}C"
    cache["wind_text"] = "--" if wind is None else f"Wind {round(float(wind))} km/h"
    cache["updated_text"] = datetime.fromtimestamp(float(cache.get("updated_at") or 0)).strftime("%H:%M") if cache.get("updated_at") else "-"
    return cache


def overlay_template_context():
    runtime = load_runtime_status()
    return {
        "ap_name": get_ap_name(),
        "active": get_active_connection(),
        "runtime": runtime,
        "wlan0_ip": get_ip("wlan0"),
        "wlan1_ip": get_ip(WLAN_IFACE),
        "now_local": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "weather": load_overlay_weather(),
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
        rendered_html = render_template_string(html_source, **overlay_template_context())
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


def _overlay_renderer_loop(app):
    with app.app_context():
        while True:
            overlay = load_overlay_state()
            refresh_sec = max(5, int(overlay.get("refresh_sec") or 10))
            last_rendered_at = float(overlay.get("last_rendered_at") or 0)
            if overlay.get("enabled") and time.time() - last_rendered_at >= refresh_sec:
                render_overlay_png(force=True)
            time.sleep(1.0)


def start_overlay_renderer_thread(app):
    global OVERLAY_RENDERER_THREAD
    if OVERLAY_RENDERER_THREAD and OVERLAY_RENDERER_THREAD.is_alive():
        return
    ensure_overlay_html_exists()
    OVERLAY_RENDERER_THREAD = threading.Thread(target=_overlay_renderer_loop, args=(app,), name="overlay-renderer", daemon=True)
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
        proc = subprocess.run(CAPTIVE_PORTAL_ACK_CMD, text=True, capture_output=True, shell=True, check=False, timeout=20)
        message = proc.stdout.strip() or proc.stderr.strip() or "Portal action finished"
        return proc.returncode == 0, message
    except subprocess.TimeoutExpired:
        return False, "Portal action timed out"
    except OSError as exc:
        return False, str(exc)


def systemd_show(unit_name, properties):
    proc = run_command(["systemctl", "show", unit_name, f"--property={','.join(properties)}"], check=False)
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
        ["LoadState", "ActiveState", "SubState", "Result", "ExecMainStatus", "ExecMainStartTimestamp"],
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
    proc = run_command(["systemctl", "start", "--no-block", UPDATE_SERVICE_NAME], check=False)
    if proc.returncode != 0:
        return False, proc.stderr.strip() or proc.stdout.strip() or f"Failed to start {UPDATE_SERVICE_NAME}"
    return True, "Update started. The web UI may restart while install runs."


def index_context():
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
    return {
        "ap_name": get_ap_name(),
        "active": get_active_connection(),
        "wifi_list": wifi_list,
        "wlan1_ip": get_ip(WLAN_IFACE),
        "wlan0_ip": get_ip("wlan0"),
        "top_wifi": wifi_list[:6],
        "runtime": runtime,
        "portal_ack_available": bool(CAPTIVE_PORTAL_ACK_CMD),
        "update_status": load_update_status(),
        "youtube_auth": youtube_auth,
        "youtube_ready": youtube_ready,
        "youtube_creation": youtube_creation,
        "youtube_creation_log": youtube_creation_log,
        "youtube_stream": youtube_stream,
        "youtube_audio_modes": list_audio_modes(),
        "youtube_fps_modes": list_fps_modes(),
        "youtube_rotation_modes": list_rotation_modes(),
        "overlay": overlay,
        "overlay_html": load_overlay_html(),
    }


def overlay_preview_response():
    path = overlay_png_path()
    if not path.is_file():
        return Response("No overlay PNG rendered yet.\n", mimetype="text/plain", status=404)
    return send_file(path, mimetype="image/png", max_age=0)

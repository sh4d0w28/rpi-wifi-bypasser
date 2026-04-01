#!/usr/bin/env python3
import json
import logging
import os
import random
import re
import time
import socket
import subprocess
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from threading import Event, Lock, Thread
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
try:
    import qrcode
except Exception:
    qrcode = None
from youtube_live import (
    DEFAULT_OVERLAY_HTML,
    YouTubeLiveError,
    ensure_overlay_html_exists,
    get_auth_status,
    load_creation_state,
    load_overlay_state,
    load_stream_state,
    poll_device_authorization,
    refresh_proxy_overlay,
    save_overlay_state,
    start_device_authorization,
    start_stream_creation,
)

WAVESHARE_PATHS = [
    os.environ.get("WAVESHARE_LCD_PATH", "/home/pi/1.44inch-LCD-HAT-Code/RaspberryPi/python"),
    "/home/pi/1.44inch-LCD-HAT-Code/RaspberryPi/python",
]
for p in WAVESHARE_PATHS:
    if p and p not in sys.path:
        sys.path.append(p)

try:
    import LCD_1in44
    import config
except Exception as exc:
    raise SystemExit(f"LCD library import failed: {exc}")

WAVESHARE_DEV = None
BUTTON_STATE_CACHE = {name: False for name in ("UP", "DOWN", "LEFT", "RIGHT", "PRESS", "KEY1", "KEY2", "KEY3")}
BUTTON_EVENT_QUEUE = deque()
BUTTON_EVENT_LOCK = Lock()
BUTTON_EVENT_MODE = False
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)

WLAN_AP = os.environ.get("WLAN0_IFACE", "wlan0")
WLAN_UP = os.environ.get("WLAN1_IFACE", "wlan1")
HOSTAPD_CONF = Path(os.environ.get("HOSTAPD_CONF", "/etc/hostapd/hostapd.conf"))
AP_CONFIG_FILE = Path(os.environ.get("AP_CONFIG_FILE", "/etc/default/rpi_ap_tools_ap"))
REFRESH_SEC = float(os.environ.get("REFRESH_SEC", "2.0"))
BUTTON_POLL_SEC = float(os.environ.get("BUTTON_POLL_SEC", "0.05"))
DISPLAY_REFRESH_SEC = float(os.environ.get("DISPLAY_REFRESH_SEC", "0.5"))
PROBE_INTERVAL_SEC = float(os.environ.get("PROBE_INTERVAL_SEC", "180.0"))
NETWORK_FALLBACK_REFRESH_SEC = float(os.environ.get("NETWORK_FALLBACK_REFRESH_SEC", "60.0"))
YOUTUBE_STATE_REFRESH_SEC = float(os.environ.get("YOUTUBE_STATE_REFRESH_SEC", "5.0"))
STATUS_WRITE_SEC = float(os.environ.get("STATUS_WRITE_SEC", "5.0"))
STATUS_PATH = Path(os.environ.get("STATUS_PATH", "/run/rpi_ap_tools_status.json"))
UPDATE_SCRIPT_PATH = Path(os.environ.get("UPDATE_SCRIPT_PATH", "/home/pi/update_ap.sh"))
UPDATE_LOG_PATH = Path(os.environ.get("UPDATE_LOG_PATH", "/run/rpi_ap_tools_update.log"))
CAPTIVE_PORTAL_ACK_CMD = os.environ.get("CAPTIVE_PORTAL_ACK_CMD", "").strip()
PORTAL_CAPTURE_URL = os.environ.get("PORTAL_CAPTURE_URL", "http://connectivitycheck.gstatic.com/generate_204").strip()
PORTAL_CAPTURE_HTML_PATH = Path(os.environ.get("PORTAL_CAPTURE_HTML_PATH", "/run/rpi_ap_tools_captive_portal.html"))
PORTAL_CAPTURE_META_PATH = Path(os.environ.get("PORTAL_CAPTURE_META_PATH", "/run/rpi_ap_tools_captive_portal.json"))
PORTAL_CAPTURE_TIMEOUT_SEC = float(os.environ.get("PORTAL_CAPTURE_TIMEOUT_SEC", "15.0"))
PORTAL_CAPTURE_MAX_BYTES = int(os.environ.get("PORTAL_CAPTURE_MAX_BYTES", str(1024 * 1024)))
YOUTUBE_PING_HOST = os.environ.get("YOUTUBE_PING_HOST", "www.youtube.com")
YOUTUBE_RTMP_HOST = os.environ.get("YOUTUBE_RTMP_HOST", "a.rtmp.youtube.com")
YOUTUBE_RTMP_PORT = int(os.environ.get("YOUTUBE_RTMP_PORT", "1935"))
YOUTUBE_PROXY_RTMP_PORT = int(os.environ.get("YOUTUBE_PROXY_RTMP_PORT", "7777") or "7777")
YOUTUBE_PROXY_RTMP_APP = os.environ.get("YOUTUBE_PROXY_RTMP_APP", "live").strip().strip("/")
PIN_UP = 6
PIN_DOWN = 19
PIN_LEFT = 5
PIN_RIGHT = 26
PIN_PRESS = 13
PIN_KEY1 = 21
PIN_KEY2 = 20
PIN_KEY3 = 16
FONT = ImageFont.load_default()
CPU_SAMPLES = deque(maxlen=2)
MATRIX_CHARS = "01アイウエオカキクケコサシスセソ"
MATRIX_FONT_W = 6
MATRIX_FONT_H = 8
MATRIX_COLS = 128 // MATRIX_FONT_W
MATRIX_ROWS = 128 // MATRIX_FONT_H
BUTTON_PINS = {
    "UP": PIN_UP,
    "DOWN": PIN_DOWN,
    "LEFT": PIN_LEFT,
    "RIGHT": PIN_RIGHT,
    "PRESS": PIN_PRESS,
    "KEY1": PIN_KEY1,
    "KEY2": PIN_KEY2,
    "KEY3": PIN_KEY3,
}
STATE_REFRESH_EVENT = Event()

def request_state_refresh():
    STATE_REFRESH_EVENT.set()

def enqueue_button_event(name, is_pressed):
    with BUTTON_EVENT_LOCK:
        BUTTON_STATE_CACHE[name] = is_pressed
        BUTTON_EVENT_QUEUE.append((time.time(), name, is_pressed))

def run(cmd):
    return subprocess.run(cmd, text=True, capture_output=True, check=False)


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

def read_ap_name():
    if HOSTAPD_CONF.exists():
        for line in HOSTAPD_CONF.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("ssid="):
                return line.split("=", 1)[1].strip()
    active_proc = run(["nmcli", "-t", "-f", "GENERAL.CONNECTION", "device", "show", WLAN_AP])
    connection_name = ""
    for line in active_proc.stdout.splitlines():
        if line.startswith("GENERAL.CONNECTION:"):
            connection_name = line.split(":", 1)[1].strip()
            break
    if connection_name:
        ssid_proc = run(["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", connection_name])
        ssid = ssid_proc.stdout.strip()
        if ssid:
            return ssid
    configured = read_config_value(AP_CONFIG_FILE, "AP_SSID", "")
    if configured:
        return configured
    return "unknown"

def read_ap_password():
    return read_config_value(AP_CONFIG_FILE, "AP_PASSWORD", "-")

def read_ipv4(dev):
    proc = run(["ip", "-4", "-o", "addr", "show", "dev", dev])
    if proc.returncode != 0:
        return "-"
    m = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+/\d+)', proc.stdout)
    return m.group(1) if m else "-"

def read_active_wifi():
    proc = run(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL", "dev", "wifi", "list", "ifname", WLAN_UP])
    for line in proc.stdout.splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[0] == "*":
            return {"name": parts[1] or "-", "signal": parts[2] or "-"}
    return {"name": "-", "signal": "-"}

def read_cpu_temp_c():
    candidates = [
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/devices/virtual/thermal/thermal_zone0/temp",
    ]
    for path in candidates:
        try:
            raw = Path(path).read_text().strip()
            return float(raw) / 1000.0
        except Exception:
            continue
    return None

def read_cpu_percent():
    try:
        fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
        values = [int(v) for v in fields]
    except Exception:
        return None

    total = sum(values)
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    CPU_SAMPLES.append((total, idle))
    if len(CPU_SAMPLES) < 2:
        return None

    prev_total, prev_idle = CPU_SAMPLES[0]
    curr_total, curr_idle = CPU_SAMPLES[1]
    total_diff = curr_total - prev_total
    idle_diff = curr_idle - prev_idle
    if total_diff <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (1.0 - (idle_diff / total_diff))))

def read_mem_percent():
    try:
        data = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, value = line.split(":", 1)
            data[key] = int(value.strip().split()[0])
        total = data.get("MemTotal", 0)
        available = data.get("MemAvailable", 0)
        if total <= 0:
            return None
        used = total - available
        return max(0.0, min(100.0, 100.0 * used / total))
    except Exception:
        return None

def read_sysfs_int(path):
    try:
        return int(Path(path).read_text().strip())
    except Exception:
        return 0

def read_bytes(dev):
    base = Path(f"/sys/class/net/{dev}/statistics")
    return {"rx": read_sysfs_int(base / "rx_bytes"), "tx": read_sysfs_int(base / "tx_bytes")}

def atomic_write_json(path, payload):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(path)
    except Exception:
        pass

def atomic_write_text(path, content):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(content, encoding="utf-8")
        temp_path.replace(path)
    except Exception:
        pass

def sanitize_filename_part(value, default="unknown"):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip())
    cleaned = cleaned.strip("._-")
    return cleaned or default

def portal_capture_paths(wifi_name, captured_at):
    stamp = datetime.fromtimestamp(captured_at).strftime("%y_%m_%d_%H:%M:%S")
    safe_wifi = sanitize_filename_part(wifi_name, default="unknown_wifi")
    html_path = PORTAL_CAPTURE_HTML_PATH.parent / f"{stamp}_{safe_wifi}_portal.html"
    meta_path = html_path.with_suffix(".json")
    return html_path, meta_path

def state_signature(state):
    try:
        snapshot = dict(state)
        snapshot.pop("updated_at", None)
        return json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    except Exception:
        return ""

def human_bytes(n):
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(n)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.1f}{unit}"
        value /= 1024.0

def ip_only(value):
    if not value or value == "-":
        return "-"
    return value.split("/", 1)[0]

def fit_text(text, max_chars):
    text = text or "-"
    if len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return text[:max_chars]
    return text[: max_chars - 1] + "."


def translate_button_for_rotation(name):
    mapping = {
        "LEFT": "UP",
        "RIGHT": "DOWN",
        "UP": "RIGHT",
        "DOWN": "LEFT",
    }
    return mapping.get(name, name)

def metric_color(value, warn, danger):
    if value is None:
        return (180, 180, 180)
    if value >= danger:
        return (255, 96, 96)
    if value >= warn:
        return (255, 210, 90)
    return (120, 255, 160)

def signal_color(signal):
    try:
        value = int(signal)
    except Exception:
        return (180, 180, 180)
    if value >= 70:
        return (120, 255, 160)
    if value >= 40:
        return (255, 210, 90)
    return (255, 96, 96)

def draw_label_value(draw, x, y, label, value, value_fill="WHITE", gap=22):
    draw.text((x, y), label, font=FONT, fill=(140, 170, 210))
    draw.text((x + gap, y), value, font=FONT, fill=value_fill)


def chrome_screen_id(state):
    if state.get("ui_mode") == "home":
        return "HOME"
    if state.get("ui_mode") == "menu":
        menu_id = (state.get("menu_id") or "root").lower()
        return {
            "youtube": "YT",
            "youtube_create": "YT",
            "youtube_create_audio": "YT",
            "youtube_create_rotation": "YT",
            "youtube_create_fps": "YT",
            "update_confirm": "UPD",
            "settings": "SET",
            "root": "MENU",
        }.get(menu_id, "MENU")
    return {
        "youtube": "YT",
        "youtube_qr": "QR",
        "settings": "SET",
        "overview": "HOME",
        "probe": "SET",
    }.get(state.get("screen_id"), "MENU")


def draw_chrome(draw, state):
    screen_id = chrome_screen_id(state)
    temp = state.get("cpu_temp")
    cpu_pct = state.get("cpu_pct")
    mem_pct = state.get("mem_pct")
    temp_text = "--" if temp is None else f"{temp:.0f}"
    cpu_text = "--" if cpu_pct is None else f"{cpu_pct:.0f}"
    mem_text = "--" if mem_pct is None else f"{mem_pct:.0f}"

    draw.rectangle((0, 0, 127, 15), fill=(16, 28, 16))
    draw.line((0, 16, 127, 16), fill=(72, 120, 72), width=1)
    draw.text((3, 4), screen_id, font=FONT, fill=(200, 255, 200))
    draw.text((38, 4), f"T:{temp_text}", font=FONT, fill=metric_color(temp, 60, 75))
    draw.text((70, 4), f"C:{cpu_text}", font=FONT, fill=metric_color(cpu_pct, 60, 85))
    draw.text((101, 4), f"M:{mem_text}", font=FONT, fill=metric_color(mem_pct, 70, 85))

    draw.line((0, 111, 127, 111), fill=(72, 120, 72), width=1)
    draw.rectangle((0, 112, 127, 127), fill=(10, 18, 10))
    draw.text((3, 117), "< BACK", font=FONT, fill=(180, 220, 180))
    draw.text((48, 117), "OPEN", font=FONT, fill=(240, 255, 240))
    draw.text((88, 117), "MENU >", font=FONT, fill=(180, 220, 180))


def draw_qr_in_box(image, payload, box):
    if not payload or qrcode is None:
        return False
    qr_image = qrcode.make(payload).convert("RGB")
    x1, y1, x2, y2 = box
    max_w = max(1, x2 - x1)
    max_h = max(1, y2 - y1)
    resampling = getattr(Image, "Resampling", Image)
    qr_image.thumbnail((max_w, max_h), resampling.NEAREST)
    offset_x = x1 + max(0, (max_w - qr_image.width) // 2)
    offset_y = y1 + max(0, (max_h - qr_image.height) // 2)
    image.paste(qr_image, (offset_x, offset_y))
    return True


def relay_probe_url():
    app = f"/{YOUTUBE_PROXY_RTMP_APP}" if YOUTUBE_PROXY_RTMP_APP else ""
    return f"rtmp://127.0.0.1:{YOUTUBE_PROXY_RTMP_PORT}{app}"


def ffprobe_video_dimensions(url):
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0:s=x",
                url,
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=3,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    match = re.search(r"(\d{2,5})x(\d{2,5})", proc.stdout)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def relay_input_connected():
    proc = run(["ss", "-ntp"])
    if proc.returncode != 0:
        return False
    pattern = f":{YOUTUBE_PROXY_RTMP_PORT}"
    for line in proc.stdout.splitlines():
        if "ESTAB" in line and pattern in line and "ffmpeg" in line:
            return True
    return False


def rotate_resolution_text(resolution_text, rotation):
    match = re.match(r"^(\d{2,5})x(\d{2,5})$", str(resolution_text or "").strip())
    if not match:
        return resolution_text or "-"
    width = int(match.group(1))
    height = int(match.group(2))
    if str(rotation) in ("90", "-90"):
        width, height = height, width
    return f"{width}x{height}"


def overlay_static_template():
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <style>
    html, body { margin: 0; width: 100%; height: 100%; background: transparent; overflow: hidden; font-family: Arial, sans-serif; }
    .wrap { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; }
    .card { padding: 24px 28px; border-radius: 22px; background: rgba(15,23,42,0.72); border: 2px solid rgba(125,211,252,0.95); color: #f8fafc; text-align: center; box-shadow: 0 12px 36px rgba(0,0,0,0.35); }
    .eyebrow { font-size: 14px; letter-spacing: 0.1em; text-transform: uppercase; color: #7dd3fc; }
    .title { margin-top: 8px; font-size: 34px; font-weight: 700; }
    .sub { margin-top: 8px; font-size: 18px; color: #cbd5e1; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="eyebrow">Overlay Demo</div>
      <div class="title">STATIC PIC</div>
      <div class="sub">RPi AP Tools</div>
    </div>
  </div>
</body>
</html>
"""


def overlay_weather_template():
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <style>
    html, body { margin: 0; width: 100%; height: 100%; background: transparent; overflow: hidden; font-family: Arial, sans-serif; }
    .panel { margin: 18px; padding: 18px 20px; width: 320px; border-radius: 24px; color: #eff6ff; background: linear-gradient(135deg, rgba(14,116,144,0.82), rgba(30,41,59,0.82)); border: 1px solid rgba(255,255,255,0.28); box-shadow: 0 14px 34px rgba(0,0,0,0.32); }
    .top { display: flex; justify-content: space-between; align-items: baseline; }
    .city { font-size: 20px; font-weight: 700; }
    .temp { font-size: 42px; font-weight: 700; margin-top: 8px; }
    .meta { margin-top: 10px; font-size: 16px; color: #dbeafe; }
    .chip { margin-top: 14px; display: inline-block; padding: 8px 12px; border-radius: 999px; background: rgba(255,255,255,0.14); font-size: 15px; }
  </style>
</head>
<body>
  <div class="panel">
    <div class="top">
      <div class="city">Bangkok Demo</div>
      <div>WEATHER</div>
    </div>
    <div class="temp">31C</div>
    <div class="meta">Partly cloudy</div>
    <div class="chip">Example overlay template</div>
  </div>
</body>
</html>
"""


def draw_hourglass(draw, center_x, center_y, *, fill=(240, 244, 255)):
    top = [(center_x - 10, center_y - 12), (center_x + 10, center_y - 12), (center_x, center_y - 1)]
    bottom = [(center_x - 10, center_y + 12), (center_x + 10, center_y + 12), (center_x, center_y + 1)]
    draw.polygon(top, outline=fill)
    draw.polygon(bottom, outline=fill)
    draw.line((center_x - 10, center_y - 12, center_x - 10, center_y + 12), fill=fill)
    draw.line((center_x + 10, center_y - 12, center_x + 10, center_y + 12), fill=fill)
    draw.line((center_x - 6, center_y - 8, center_x + 6, center_y - 8), fill=fill)
    draw.line((center_x - 6, center_y + 8, center_x + 6, center_y + 8), fill=fill)
    draw.polygon(
        [(center_x - 7, center_y - 9), (center_x + 7, center_y - 9), (center_x, center_y - 2)],
        fill=fill,
    )
    draw.polygon(
        [(center_x - 7, center_y + 9), (center_x + 7, center_y + 9), (center_x, center_y + 2)],
        outline=fill,
    )


def render_busy_overlay(draw, state):
    busy = state.get("busy_action") or {}
    if not busy:
        return
    draw.rectangle((14, 30, 114, 95), fill=(6, 20, 6), outline=(90, 180, 90))
    draw.rectangle((18, 34, 110, 91), outline=(36, 96, 36))
    draw_hourglass(draw, 64, 53, fill=(200, 255, 200))
    draw.text((30, 72), "PLEASE WAIT", font=FONT, fill=(220, 255, 220))
    draw.text((22, 84), fit_text(busy.get("label", "Working"), 14), font=FONT, fill=(140, 220, 140))

def read_button_states():
    states = {name: button_pressed(name, pin) for name, pin in BUTTON_PINS.items()}
    with BUTTON_EVENT_LOCK:
        BUTTON_STATE_CACHE.update(states)
    return states

def drain_button_events():
    with BUTTON_EVENT_LOCK:
        events = list(BUTTON_EVENT_QUEUE)
        BUTTON_EVENT_QUEUE.clear()
    return events

def attach_waveshare_device(lcd):
    global WAVESHARE_DEV
    candidates = [
        lcd,
        getattr(lcd, "LCD", None),
        getattr(lcd, "device", None),
        getattr(lcd, "DEV", None),
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        if hasattr(candidate, "digital_read") and hasattr(candidate, "GPIO_KEY_UP_PIN"):
            WAVESHARE_DEV = candidate
            return
        for value in vars(candidate).values() if hasattr(candidate, "__dict__") else []:
            if hasattr(value, "digital_read") and hasattr(value, "GPIO_KEY_UP_PIN"):
                WAVESHARE_DEV = value
                return

def get_waveshare_button_device(name):
    if WAVESHARE_DEV is None:
        return None
    attr_name = "GPIO_KEY_PRESS_PIN" if name == "PRESS" else f"GPIO_KEY_{name}_PIN"
    return getattr(WAVESHARE_DEV, attr_name, None)

def bind_button_callbacks():
    global BUTTON_EVENT_MODE
    if WAVESHARE_DEV is None:
        return
    bound_any = False
    for name in BUTTON_PINS:
        device = get_waveshare_button_device(name)
        if device is None:
            continue
        try:
            BUTTON_STATE_CACHE[name] = bool(device.is_active)
        except Exception:
            BUTTON_STATE_CACHE[name] = False
        try:
            device.when_activated = (lambda _device, button_name=name: enqueue_button_event(button_name, True))
            device.when_deactivated = (lambda _device, button_name=name: enqueue_button_event(button_name, False))
            bound_any = True
        except Exception:
            continue
    BUTTON_EVENT_MODE = bound_any

def ping_latency_ms(host):
    proc = run(["ping", "-4", "-c", "1", "-W", "1", host])
    if proc.returncode != 0:
        return None
    match = re.search(r'time[=<]([\d.]+)\s*ms', proc.stdout)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None

def tcp_latency_ms(host, port):
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return (time.monotonic() - start) * 1000.0
    except OSError:
        return None

def read_nm_connectivity():
    proc = run(["nmcli", "-t", "networking", "connectivity"])
    if proc.returncode != 0:
        return "unknown"
    value = proc.stdout.strip().splitlines()
    return value[0] if value else "unknown"

def perform_portal_ack():
    if not CAPTIVE_PORTAL_ACK_CMD:
        return {"ok": False, "message": "No portal ack command configured", "at": time.time()}
    try:
        proc = subprocess.run(
            CAPTIVE_PORTAL_ACK_CMD,
            text=True,
            capture_output=True,
            shell=True,
            check=False,
            timeout=20,
        )
        message = proc.stdout.strip() or proc.stderr.strip() or "Portal command finished"
        return {"ok": proc.returncode == 0, "message": message, "at": time.time()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "Portal command timed out", "at": time.time()}
    except Exception as exc:
        return {"ok": False, "message": str(exc), "at": time.time()}

def capture_portal_response(wifi_name="-"):
    if not PORTAL_CAPTURE_URL:
        return {"ok": False, "message": "No portal capture URL configured", "captured_at": time.time()}

    captured_at = time.time()
    html_path, meta_path = portal_capture_paths(wifi_name, captured_at)
    request = urllib.request.Request(
        PORTAL_CAPTURE_URL,
        headers={
            "User-Agent": "rpi-wifi-bypasser/1.0",
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=PORTAL_CAPTURE_TIMEOUT_SEC) as response:
            raw = response.read(PORTAL_CAPTURE_MAX_BYTES + 1)
            if len(raw) > PORTAL_CAPTURE_MAX_BYTES:
                raw = raw[:PORTAL_CAPTURE_MAX_BYTES]
            charset = response.headers.get_content_charset() or "utf-8"
            body = raw.decode(charset, errors="replace")
            meta = {
                "ok": True,
                "captured_at": captured_at,
                "requested_url": PORTAL_CAPTURE_URL,
                "final_url": response.geturl(),
                "status_code": getattr(response, "status", 200),
                "content_type": response.headers.get_content_type() if response.headers else "",
                "content_length": len(raw),
                "wifi_name": wifi_name,
                "html_path": str(html_path),
                "meta_path": str(meta_path),
            }
            atomic_write_text(html_path, body)
            atomic_write_json(meta_path, meta)
            return meta
    except urllib.error.HTTPError as exc:
        raw = exc.read(PORTAL_CAPTURE_MAX_BYTES)
        charset = exc.headers.get_content_charset() if exc.headers else None
        body = raw.decode(charset or "utf-8", errors="replace")
        meta = {
            "ok": False,
            "captured_at": captured_at,
            "requested_url": PORTAL_CAPTURE_URL,
            "final_url": exc.geturl(),
            "status_code": exc.code,
            "content_type": exc.headers.get_content_type() if exc.headers else "",
            "content_length": len(raw),
            "wifi_name": wifi_name,
            "html_path": str(html_path),
            "meta_path": str(meta_path),
            "message": str(exc),
        }
        atomic_write_text(html_path, body)
        atomic_write_json(meta_path, meta)
        return meta
    except Exception as exc:
        meta = {
            "ok": False,
            "captured_at": captured_at,
            "requested_url": PORTAL_CAPTURE_URL,
            "wifi_name": wifi_name,
            "html_path": str(html_path),
            "meta_path": str(meta_path),
            "message": str(exc),
        }
        atomic_write_json(meta_path, meta)
        return meta

def watch_command(cmd, name):
    while True:
        proc = None
        try:
            proc = subprocess.Popen(
                cmd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
            )
            logging.info("Started monitor %s: %s", name, " ".join(cmd))
            for line in proc.stdout:
                if line.strip():
                    logging.debug("Monitor %s event: %s", name, line.strip())
                    request_state_refresh()
        except Exception as exc:
            logging.warning("Monitor %s failed: %s", name, exc)
        finally:
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=1)
                except Exception:
                    pass
        time.sleep(2.0)

def start_watchers():
    commands = [
        (["ip", "monitor", "address", "dev", WLAN_AP], f"{WLAN_AP}-addr"),
        (["ip", "monitor", "address", "dev", WLAN_UP], f"{WLAN_UP}-addr"),
        (["nmcli", "monitor"], "nmcli"),
    ]
    for cmd, name in commands:
        Thread(target=watch_command, args=(cmd, name), daemon=True).start()

def render_overview(draw, state):
    rows = [
        ("AP", fit_text(state["ap_name"], 16), (240, 244, 255)),
        ("IP", fit_text(state["w0"], 16), (120, 220, 255)),
        ("W1", fit_text(state["active_wifi"]["name"], 16), (240, 244, 255)),
        ("IP", fit_text(state["w1"], 16), (120, 220, 255)),
        ("SIG", f"{state['signal']}%" if state["signal"] != "-" else "--", signal_color(state["signal"])),
        ("TXR", fit_text(f"{human_bytes(state['tx1ps'])}/{human_bytes(state['rx1ps'])}", 14), (255, 210, 90)),
    ]
    y = 22
    for label, value, fill in rows:
        draw.text((4, y), label, font=FONT, fill=(140, 170, 210))
        draw.text((27, y), value, font=FONT, fill=fill)
        y += 14

def render_probe(draw, state):
    probe = state["probe"]
    yt_text = "-" if probe["youtube_ping_ms"] is None else f"{probe['youtube_ping_ms']:.0f}ms"
    rtmp_text = "-" if probe["youtube_rtmp_ms"] is None else f"{probe['youtube_rtmp_ms']:.0f}ms"
    portal_fill = (255, 210, 90) if probe["portal_suspected"] else (120, 255, 160) if probe["internet_ok"] else (255, 96, 96)
    portal_text = "PORTAL" if probe["portal_suspected"] else "ONLINE" if probe["internet_ok"] else "OFFLINE"
    rows = [
        ("W1", fit_text(state["active_wifi"]["name"], 16), (120, 220, 255)),
        ("IP", fit_text(state["w1"], 16), (240, 244, 255)),
        ("YT", yt_text, (120, 220, 255)),
        ("RT", rtmp_text, (255, 210, 90)),
        ("NET", fit_text(probe["connectivity"], 14), (240, 244, 255)),
        ("STS", portal_text, portal_fill),
    ]
    y = 22
    for label, value, fill in rows:
        draw.text((4, y), label, font=FONT, fill=(140, 170, 210))
        draw.text((27, y), value, font=FONT, fill=fill)
        y += 14

    if state["portal_ack_last"]:
        msg = fit_text(state["portal_ack_last"]["message"], 18)
        fill = (120, 255, 160) if state["portal_ack_last"]["ok"] else (255, 96, 96)
        draw.text((4, 95), msg, font=FONT, fill=fill)

def render_youtube(draw, image, state):
    youtube = state["youtube"]
    creating = (youtube.get("creation") or {}).get("status") == "creating"
    if creating:
        creation = youtube.get("creation") or {}
        progress_pct = max(0, min(100, int(creation.get("progress_pct", 0) or 0)))
        stage_message = fit_text(creation.get("message", "Working"), 18)
        bar_left = 14
        bar_top = 74
        bar_right = 113
        bar_bottom = 88
        fill_width = int((bar_right - bar_left - 2) * (progress_pct / 100.0))
        draw.rectangle((4, 22, 123, 104), outline=(255, 96, 96), width=2)
        draw.rectangle((8, 26, 119, 100), outline=(255, 96, 96), width=1)
        draw.text((14, 34), "STREAM IS", font=FONT, fill=(255, 230, 230))
        draw.text((16, 48), "CREATING", font=FONT, fill=(255, 230, 230))
        draw.text((14, 62), stage_message, font=FONT, fill=(255, 210, 210))
        draw.rectangle((bar_left, bar_top, bar_right, bar_bottom), outline=(255, 170, 170), width=1)
        draw.rectangle((bar_left + 1, bar_top + 1, bar_left + 1 + fill_width, bar_bottom - 1), fill=(255, 96, 96))
        draw.text((44, 91), f"{progress_pct:3d}%", font=FONT, fill=(255, 230, 230))
        return

    if youtube.get("auth_required") and not youtube.get("qr_payload"):
        draw.rectangle((4, 22, 123, 104), outline=(255, 96, 96), width=2)
        draw.rectangle((8, 26, 119, 100), outline=(255, 96, 96), width=1)
        draw.text((22, 36), "AUTH", font=FONT, fill=(255, 230, 230))
        draw.text((22, 50), "FIRST", font=FONT, fill=(255, 230, 230))
        draw.text((12, 70), fit_text(youtube.get("status_message", "AUTH FIRST"), 18), font=FONT, fill=(255, 210, 210))
        return

    if youtube["auth"].get("device_pending"):
        device = youtube["auth"].get("device") or {}
        code = device.get("user_code", "")
        verify = device.get("verification_url", "") or device.get("verification_url_complete", "")
        draw.text((4, 22), "AUTH PENDING", font=FONT, fill=(255, 210, 90))
        draw.text((4, 37), fit_text(f"Code {code}", 18), font=FONT, fill=(240, 244, 255))
        draw.text((4, 51), fit_text(verify.replace("https://", ""), 18), font=FONT, fill=(120, 220, 255))
        draw.text((4, 79), "PRESS=CHECK", font=FONT, fill=(240, 244, 255))
        return

    incoming = youtube.get("incoming_res") or "-"
    outgoing = youtube.get("outgoing_res") or incoming
    overlay_text = "Active" if youtube.get("overlay_enabled") else "Off"
    rows = [
        ("Name", fit_text(youtube.get("title", "No stream"), 16), (240, 244, 255)),
        ("RTMP", fit_text(youtube.get("rtmp_summary", "-"), 14), (120, 220, 255)),
        ("Rot", youtube.get("rotation_short", "OFF"), (255, 210, 90)),
        ("FPS", youtube.get("fps_mode_short", "ORIG"), (120, 255, 160)),
        ("In", incoming, (240, 244, 255)),
        ("Out", outgoing, (120, 220, 255)),
        ("Ovl", overlay_text, (255, 210, 90) if youtube.get("overlay_enabled") else (180, 180, 180)),
    ]
    y = 22
    for label, value, fill in rows:
        if y > 102:
            break
        draw.text((4, y), label, font=FONT, fill=(140, 170, 210))
        value_x = 34 if len(label) >= 4 else 27
        max_chars = 14 if label in ("Name", "RTMP") else 16
        draw.text((value_x, y), fit_text(str(value), max_chars), font=FONT, fill=fill)
        y += 12


def render_youtube_qr(draw, image, state):
    payload = state.get("youtube", {}).get("qr_payload", "")
    if draw_qr_in_box(image, payload, (18, 24, 110, 102)):
        draw.rectangle((16, 22, 112, 104), outline=(255, 255, 255))
        draw.text((36, 98), "STREAM QR", font=FONT, fill=(240, 244, 255))
        return
    draw.rectangle((8, 30, 119, 96), outline=(90, 180, 90))
    draw.text((22, 46), "NO STREAM", font=FONT, fill=(240, 244, 255))
    draw.text((18, 60), "QR UNAVAILABLE", font=FONT, fill=(180, 180, 180))


def render_settings(draw, state):
    rows = [
        ("RTMP", fit_text(state.get("settings_rtmp", "-"), 14), (120, 220, 255)),
        ("Rot", state.get("settings_rotation", "0"), (255, 210, 90)),
        ("FPS", state.get("settings_fps", "ORIG"), (120, 255, 160)),
        ("Snd", state.get("settings_audio", "NORM"), (240, 244, 255)),
        ("Pass", fit_text(state.get("ap_password", "-"), 14), (255, 210, 90)),
    ]
    y = 24
    for label, value, fill in rows:
        draw.text((4, y), label, font=FONT, fill=(140, 170, 210))
        draw.text((30, y), value, font=FONT, fill=fill)
        y += 15

def render_portal_warning(draw, state):
    if not state["probe"].get("auth_required"):
        return

    draw.rectangle((0, 50, 127, 78), fill=(110, 0, 0))
    draw.line((0, 50, 127, 50), fill=(255, 96, 96), width=1)
    draw.line((0, 78, 127, 78), fill=(255, 96, 96), width=1)
    draw.text((20, 56), "AUTH", font=FONT, fill=(255, 230, 230))
    draw.text((20, 67), "FIRST", font=FONT, fill=(255, 230, 230))


def render_matrix(draw, state):
    matrix = state["matrix"]
    draw.rectangle((0, 0, 127, 127), fill="BLACK")
    for col in matrix["columns"]:
        x = col["x"]
        head = col["head"]
        length = col["length"]
        chars = col["chars"]
        for offset in range(length):
            row = head - offset
            if row < 0 or row >= MATRIX_ROWS:
                continue
            y = row * MATRIX_FONT_H
            char = chars[row % len(chars)]
            if offset == 0:
                fill = (210, 255, 210)
            elif offset < 3:
                fill = (120, 255, 160)
            else:
                fill = (0, max(40, 200 - (offset * 16)), 40)
            draw.text((x, y), char, font=FONT, fill=fill)
    draw.rectangle((0, 0, 127, 15), fill=(0, 18, 0))
    draw.line((0, 16, 127, 16), fill=(0, 64, 0), width=1)
    draw.text((3, 3), "MATRIX", font=FONT, fill=(120, 255, 160))
    draw.text((76, 3), "LEFT BK", font=FONT, fill=(120, 180, 120))


def render_home(draw, state):
    rows = [
        ("AP", fit_text(state.get("ap_name", "-"), 16), (240, 255, 240)),
        ("IP", fit_text(state.get("w0", "-"), 16), (120, 220, 255)),
        ("W1", fit_text((state.get("active_wifi") or {}).get("name", "-"), 16), (240, 244, 255)),
        ("IP", fit_text(state.get("w1", "-"), 16), (120, 220, 255)),
        ("SIG", f"{state.get('signal')}%" if state.get("signal") != "-" else "--", signal_color(state.get("signal"))),
        ("TXR", fit_text(f"{human_bytes(state['tx1ps'])}/{human_bytes(state['rx1ps'])}", 14), (255, 210, 90)),
    ]
    y = 22
    for label, value, fill in rows:
        draw.text((4, y), label, font=FONT, fill=(140, 170, 210))
        draw.text((27, y), value, font=FONT, fill=fill)
        y += 14


def render_game_pong(draw, state):
    game = state["games"]["pong"]
    draw.rectangle((0, 0, 127, 127), fill="BLACK")
    draw.line((63, 16, 63, 127), fill=(36, 72, 36), width=1)
    draw.rectangle((4, game["player_y"], 7, game["player_y"] + 20), fill=(120, 255, 160))
    draw.rectangle((120, game["cpu_y"], 123, game["cpu_y"] + 20), fill=(255, 210, 90))
    draw.rectangle((game["ball_x"], game["ball_y"], game["ball_x"] + 4, game["ball_y"] + 4), fill=(240, 255, 240))
    draw.rectangle((0, 0, 127, 15), fill=(0, 18, 0))
    draw.text((4, 3), "PONG", font=FONT, fill=(160, 255, 160))
    draw.text((48, 3), f"{game['player_score']}:{game['cpu_score']}", font=FONT, fill=(240, 255, 240))
    if game["game_over"]:
        draw.rectangle((18, 44, 109, 84), fill=(8, 24, 8), outline=(90, 180, 90))
        draw.text((33, 50), "GAME OVER", font=FONT, fill=(255, 210, 90))
        draw.text((28, 64), "PRESS RESTART", font=FONT, fill=(200, 230, 200))
    draw.text((10, 119), "U/D MOVE L BK", font=FONT, fill=(120, 180, 120))


def render_game_catch(draw, state):
    game = state["games"]["catch"]
    draw.rectangle((0, 0, 127, 127), fill="BLACK")
    draw.rectangle((0, 0, 127, 15), fill=(12, 12, 44))
    draw.text((4, 3), "CATCH", font=FONT, fill=(160, 220, 255))
    draw.text((56, 3), f"S{game['score']} L{game['lives']}", font=FONT, fill=(240, 255, 240))
    for item in game["drops"]:
        draw.rectangle((item["x"], item["y"], item["x"] + 4, item["y"] + 4), fill=(255, 210, 90))
    draw.rectangle((game["player_x"], 118, game["player_x"] + 20, 122), fill=(120, 220, 255))
    if game["game_over"]:
        draw.rectangle((18, 44, 109, 84), fill=(8, 16, 32), outline=(120, 180, 255))
        draw.text((33, 50), "GAME OVER", font=FONT, fill=(255, 210, 90))
        draw.text((28, 64), "PRESS RESTART", font=FONT, fill=(210, 230, 255))
    draw.text((10, 119), "U/D MOVE L BK", font=FONT, fill=(120, 180, 220))


def render_menu(draw, state):
    title = fit_text(state.get("menu_title", "Menu").upper(), 12)
    items = state.get("menu_items") or []
    selected = state.get("menu_selected", 0)
    if items:
        selected = max(0, min(selected, len(items) - 1))
    else:
        selected = 0

    draw.text((4, 22), title, font=FONT, fill=(160, 255, 160))

    visible_rows = 6
    row_height = 12
    top_index = 0
    if len(items) > visible_rows:
        top_index = max(0, min(selected - (visible_rows - 1), len(items) - visible_rows))

    for row in range(visible_rows):
        idx = top_index + row
        if idx >= len(items):
            break
        item = items[idx]
        y = 35 + (row * row_height)
        is_selected = idx == selected
        label = item.get("label", "-")
        if item.get("checked"):
            label = f"{label} *"
        text = fit_text(label, 18)
        fill = (255, 255, 255) if not item.get("disabled") else (120, 120, 120)
        if is_selected:
            draw.rectangle((2, y - 1, 125, y + 10), fill=(32, 72, 32), outline=(120, 200, 120))
            fill = (240, 255, 240) if not item.get("disabled") else (180, 180, 180)
        draw.text((6, y), text, font=FONT, fill=fill)

    message = fit_text(state.get("menu_message", ""), 20)
    if message:
        draw.text((4, 101), message, font=FONT, fill=(255, 210, 90))


def render_selector_popup(draw, state):
    selector = state.get("modal_selector") or {}
    items = selector.get("items") or []
    if not items:
        return

    draw.rectangle((8, 22, 119, 107), fill=(6, 20, 6), outline=(90, 180, 90))
    draw.rectangle((12, 26, 115, 45), fill=(0, 24, 0), outline=(36, 96, 36))
    draw.text((16, 32), fit_text(selector.get("title", "Select").upper(), 14), font=FONT, fill=(180, 255, 180))

    selected = max(0, min(selector.get("selected", 0), len(items) - 1))
    visible_rows = 4
    top_index = 0
    if len(items) > visible_rows:
        top_index = max(0, min(selected - 1, len(items) - visible_rows))

    for row in range(visible_rows):
        idx = top_index + row
        if idx >= len(items):
            break
        item = items[idx]
        y = 51 + (row * 12)
        is_selected = idx == selected
        fill = (240, 255, 240) if not item.get("disabled") else (150, 150, 150)
        if is_selected:
            draw.rectangle((14, y - 1, 113, y + 10), fill=(32, 72, 32), outline=(120, 200, 120))
        label = item.get("label", "-")
        if item.get("checked"):
            label = f"{label} *"
        draw.text((18, y), fit_text(label, 16), font=FONT, fill=fill)

    value_text = fit_text(selector.get("value_text", ""), 16)
    if value_text:
        draw.text((18, 96), value_text, font=FONT, fill=(255, 210, 90))

def render_screen(lcd, state):
    image = Image.new("RGB", (128, 128), "BLACK")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 127, 127), fill="BLACK")

    if state.get("ui_mode") == "home":
        render_home(draw, state)
    elif state.get("ui_mode") == "menu":
        if state.get("modal_selector"):
            background_state = dict(state)
            background_state["menu_title"] = state.get("menu_base_title", state.get("menu_title"))
            background_state["menu_items"] = state.get("menu_base_items", state.get("menu_items"))
            background_state["menu_selected"] = state.get("menu_base_selected", state.get("menu_selected", 0))
            render_menu(draw, background_state)
            render_selector_popup(draw, state)
        else:
            render_menu(draw, state)
    elif state.get("ui_mode") == "screen":
        pass
    if state.get("ui_mode") in ("home", "menu", "screen"):
        draw_chrome(draw, state)
    if state.get("ui_mode") == "screen" and state["screen_id"] == "overview":
        render_overview(draw, state)
    elif state.get("ui_mode") == "screen" and state["screen_id"] == "probe":
        render_probe(draw, state)
    elif state.get("ui_mode") == "screen" and state["screen_id"] == "youtube":
        render_youtube(draw, image, state)
    elif state.get("ui_mode") == "screen" and state["screen_id"] == "youtube_qr":
        render_youtube_qr(draw, image, state)
    elif state.get("ui_mode") == "screen" and state["screen_id"] == "settings":
        render_settings(draw, state)
    if state.get("ui_mode") == "screen" and state["screen_id"] in ("overview", "probe", "youtube", "settings"):
        render_portal_warning(draw, state)
    render_busy_overlay(draw, state)

    lcd.LCD_ShowImage(image.rotate(90), 0, 0)

def button_pressed(name, pin):
    try:
        if hasattr(config, "digital_read"):
            return config.digital_read(pin) == 0
        pin_attr = get_waveshare_button_device(name)
        if pin_attr is None:
            return False
        return WAVESHARE_DEV.digital_read(pin_attr) == 0
    except Exception:
        return False

def init_buttons():
    try:
        if hasattr(config, "module_init"):
            config.module_init()
    except Exception:
        pass

def main():
    init_buttons()
    start_watchers()
    lcd = LCD_1in44.LCD()
    attach_waveshare_device(lcd)
    bind_button_callbacks()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    try:
        lcd.LCD_Clear()
    except Exception:
        pass
    prev = {WLAN_AP: read_bytes(WLAN_AP), WLAN_UP: read_bytes(WLAN_UP)}
    prev_t = time.time()
    curr = prev
    button_states_prev = {name: False for name in BUTTON_PINS}
    ap_name = read_ap_name()
    ap_password = read_ap_password()
    w0 = ip_only(read_ipv4(WLAN_AP))
    w1 = ip_only(read_ipv4(WLAN_UP))
    active_wifi = read_active_wifi()
    cpu_temp = read_cpu_temp_c()
    cpu_pct = read_cpu_percent()
    mem_pct = read_mem_percent()
    rx1ps = 0.0
    tx1ps = 0.0
    ap_ok = ap_name != "unknown" and w0 != "-"
    cl_ok = active_wifi["name"] != "-" and w1 != "-"
    signal = active_wifi["signal"] if cl_ok else "-"
    probe_cache = {
        "last_run": 0.0,
        "youtube_ping_ms": None,
        "youtube_rtmp_ms": None,
        "connectivity": "unknown",
        "portal_suspected": False,
        "internet_ok": False,
        "auth_required": False,
        "portal_capture": {},
    }
    portal_ack_last = None
    youtube_auth = get_auth_status()
    youtube_creation = load_creation_state()
    youtube_stream = load_stream_state()
    youtube_status_message = "Use YT menu"
    youtube_create_audio_mode = youtube_creation.get("audio_mode") or youtube_stream.get("audio_mode", "normal")
    youtube_create_rotation = youtube_creation.get("rotation") or youtube_stream.get("rotation", "0")
    youtube_create_fps_mode = youtube_creation.get("fps_mode") or youtube_stream.get("fps_mode", "original")
    menu_stack = [{"id": "root", "selected": 0}]
    current_screen = None
    ui_mode = "home"
    ui_message = ""
    busy_action = None
    last_display_at = 0.0
    last_network_refresh_at = 0.0
    last_status_write_at = 0.0
    last_display_signature = None
    last_status_signature = None
    live_input_res = "-"
    live_output_res = "-"
    state_lock = Lock()
    matrix_columns = [
        {
            "x": idx * MATRIX_FONT_W,
            "head": -(idx % MATRIX_ROWS),
            "length": 4 + ((idx * 3) % 9),
            "speed": 1 + (idx % 2),
            "chars": [MATRIX_CHARS[(idx + row * 5) % len(MATRIX_CHARS)] for row in range(MATRIX_ROWS)],
        }
        for idx in range(MATRIX_COLS)
    ]
    matrix_tick = 0
    game_refresh_sec = 0.08
    pong_game = {}
    catch_game = {}
    request_state_refresh()

    def set_ui_message(message):
        nonlocal ui_message
        ui_message = fit_text(message or "", 20)

    def selector_menu_ids():
        return {
            "youtube_create_audio",
            "youtube_create_rotation",
            "youtube_create_fps",
        }

    def menu_definition_for(menu_id, selected=0):
        title, items = get_menu_definition(menu_id)
        if not items:
            items = [{"label": "Empty", "kind": "noop", "disabled": True}]
        selected = max(0, min(selected, len(items) - 1))
        return title, items, selected

    def default_selected_for_menu(menu_id):
        _, items, _ = menu_definition_for(menu_id, 0)
        for idx, item in enumerate(items):
            if item.get("checked"):
                return idx
        return 0

    def selector_value_text(menu_id):
        if menu_id.startswith("youtube_create_"):
            return "NEXT STREAM"
        return fit_text(ui_message or "", 16)

    def rtmp_summary_text():
        stream_url = youtube_stream.get("proxy_publish_url") or youtube_stream.get("target_url") or ""
        if stream_url:
            return stream_url.replace("rtmp://", "").replace("rtmps://", "")
        if YOUTUBE_PROXY_RTMP_APP:
            return f".../{YOUTUBE_PROXY_RTMP_APP}"
        return "rtmp://-"

    def stream_resolution_text():
        relay = youtube_stream.get("relay") or {}
        width = relay.get("video_width")
        height = relay.get("video_height")
        if width and height:
            return f"{width}x{height}"
        return "-"

    def stream_input_resolution_text():
        value = stream_resolution_text()
        if value != "-":
            return value
        return live_input_res

    def stream_output_resolution_text():
        incoming = stream_input_resolution_text()
        if incoming == "-":
            return live_output_res
        return rotate_resolution_text(incoming, youtube_stream.get("rotation", "0"))

    def default_create_settings():
        audio = youtube_creation.get("audio_mode") or youtube_stream.get("audio_mode", "normal")
        rotation = youtube_creation.get("rotation") or youtube_stream.get("rotation", "0")
        fps = youtube_creation.get("fps_mode") or youtube_stream.get("fps_mode", "original")
        return audio, rotation, fps

    def allow_restart_auth():
        if youtube_auth.get("device_pending"):
            return True
        if youtube_auth.get("authorized") and not probe_cache.get("auth_required"):
            return False
        return bool(youtube_status_message and youtube_status_message not in ("Use YT menu", "AUTH FIRST"))

    def apply_overlay_demo(template_name):
        overlay = load_overlay_state()
        overlay["enabled"] = template_name != "off"
        overlay["x"] = 36
        overlay["y"] = 36
        overlay["width"] = 420
        overlay["height"] = 240
        overlay["opacity"] = 1.0
        overlay["refresh_sec"] = 5
        save_overlay_state(overlay)
        ensure_overlay_html_exists()
        html_path = Path(overlay.get("html_path") or "")
        if template_name == "static":
            html = overlay_static_template()
        elif template_name == "weather":
            html = overlay_weather_template()
        elif template_name == "default":
            html = DEFAULT_OVERLAY_HTML
        else:
            html = DEFAULT_OVERLAY_HTML
        if template_name != "off" and html_path:
            html_path.parent.mkdir(parents=True, exist_ok=True)
            html_path.write_text(html, encoding="utf-8")
        try:
            refresh_proxy_overlay()
            set_ui_message(f"Overlay {template_name}")
        except YouTubeLiveError:
            set_ui_message(f"Overlay {template_name} saved")

    def compose_state(now):
        with state_lock:
            menu_title, menu_items = current_menu_definition()
            menu_selected = current_menu_entry()["selected"]
            modal_selector = None
            menu_base_title = menu_title
            menu_base_items = menu_items
            menu_base_selected = menu_selected
            if ui_mode == "menu" and current_menu_entry()["id"] in selector_menu_ids() and len(menu_stack) > 1:
                parent_entry = menu_stack[-2]
                menu_base_title, menu_base_items, menu_base_selected = menu_definition_for(
                    parent_entry["id"],
                    parent_entry.get("selected", 0),
                )
                modal_selector = {
                    "title": menu_title,
                    "items": menu_items,
                    "selected": menu_selected,
                    "value_text": selector_value_text(current_menu_entry()["id"]),
                }
            return {
                "ui_mode": ui_mode,
                "screen_id": current_screen,
                "menu_id": current_menu_entry()["id"],
                "screen_title": {
                    "overview": "Overview",
                    "probe": "Probe",
                    "youtube": "YouTube",
                    "youtube_qr": "Stream QR",
                    "settings": "Settings",
                }.get(current_screen, "Menu"),
                "menu_title": menu_title,
                "menu_items": menu_items,
                "menu_selected": menu_selected,
                "menu_base_title": menu_base_title,
                "menu_base_items": menu_base_items,
                "menu_base_selected": menu_base_selected,
                "modal_selector": modal_selector,
                "menu_message": ui_message,
                "busy_action": busy_action,
                "ap_name": ap_name,
                "w0": w0,
                "w1": w1,
                "active_wifi": active_wifi,
                "cpu_temp": cpu_temp,
                "cpu_pct": cpu_pct,
                "mem_pct": mem_pct,
                "rx1ps": rx1ps,
                "tx1ps": tx1ps,
                "ap_ok": ap_ok,
                "cl_ok": cl_ok,
                "signal": signal,
                "probe": probe_cache,
                "portal_ack_configured": bool(CAPTIVE_PORTAL_ACK_CMD),
                "portal_ack_last": portal_ack_last,
                "youtube": {
                    "auth": youtube_auth,
                    "title": youtube_stream.get("title", ""),
                    "watch_url": youtube_stream.get("watch_url", ""),
                    "qr_payload": youtube_stream.get("qr_payload", ""),
                    "mode": youtube_stream.get("mode", "direct"),
                    "audio_mode": youtube_stream.get("audio_mode", "normal"),
                    "audio_mode_label": youtube_stream.get("audio_mode_label", "Normal"),
                    "audio_mode_short": youtube_stream.get("audio_mode_short", "NORM"),
                    "rotation": youtube_stream.get("rotation", "0"),
                    "rotation_short": youtube_stream.get("rotation_short", "OFF"),
                    "fps_mode": youtube_stream.get("fps_mode", "original"),
                    "fps_mode_short": youtube_stream.get("fps_mode_short", "ORIG"),
                    "relay": youtube_stream.get("relay", {}),
                    "incoming_res": stream_input_resolution_text(),
                    "outgoing_res": stream_output_resolution_text(),
                    "overlay_enabled": bool((youtube_stream.get("relay") or {}).get("overlay_enabled")),
                    "rtmp_summary": rtmp_summary_text(),
                    "creation": youtube_creation,
                    "create_audio_short": {"normal": "NORM", "voice": "VOICE", "mute": "MUTE"}.get(youtube_create_audio_mode, youtube_create_audio_mode.upper()),
                    "create_rotation_short": {"0": "OFF", "90": "+90", "-90": "-90"}.get(youtube_create_rotation, youtube_create_rotation),
                    "create_fps_short": {"original": "ORIG", "30": "30FPS", "20": "20FPS"}.get(youtube_create_fps_mode, youtube_create_fps_mode.upper()),
                    "status_message": youtube_status_message if youtube_auth.get("device_pending") else "AUTH FIRST" if (probe_cache.get("auth_required") or not youtube_auth.get("authorized")) and (youtube_creation or {}).get("status") != "creating" else youtube_status_message,
                    "auth_required": probe_cache.get("auth_required") or not youtube_auth.get("authorized"),
                },
                "settings_rtmp": rtmp_summary_text(),
                "settings_rotation": {"0": "OFF", "90": "+90", "-90": "-90"}.get(youtube_create_rotation, youtube_create_rotation),
                "settings_fps": {"original": "ORIG", "30": "30FPS", "20": "20FPS"}.get(youtube_create_fps_mode, youtube_create_fps_mode.upper()),
                "settings_audio": {"normal": "NORM", "voice": "VOICE", "mute": "MUTE"}.get(youtube_create_audio_mode, youtube_create_audio_mode.upper()),
                "ap_password": ap_password,
                "updated_at": now,
            }

    def repaint_now():
        nonlocal last_display_at, last_display_signature, last_status_write_at, last_status_signature
        now = time.time()
        state = compose_state(now)
        render_screen(lcd, state)
        signature = state_signature(state)
        atomic_write_json(STATUS_PATH, state)
        last_display_at = now
        last_display_signature = signature
        last_status_write_at = now
        last_status_signature = signature

    def show_busy(label, *, screen=None, mode=None):
        nonlocal busy_action, current_screen, ui_mode
        if screen is not None:
            current_screen = screen
        if mode is not None:
            ui_mode = mode
        busy_action = {"label": fit_text(label, 14), "started_at": time.time()}
        repaint_now()

    def clear_busy():
        nonlocal busy_action
        busy_action = None

    def build_root_menu():
        return [
            {"label": "YouTube", "kind": "menu", "target": "youtube"},
            {"label": "Update", "kind": "menu", "target": "update_confirm"},
            {"label": "Settings", "kind": "screen", "target": "settings"},
        ]

    def reset_pong():
        return {
            "player_y": 54,
            "cpu_y": 54,
            "ball_x": 62,
            "ball_y": 62,
            "ball_vx": random.choice((-3, 3)),
            "ball_vy": random.choice((-2, -1, 1, 2)),
            "player_score": 0,
            "cpu_score": 0,
            "game_over": False,
        }

    def reset_catch():
        return {
            "player_x": 53,
            "drops": [],
            "spawn_tick": 0,
            "score": 0,
            "lives": 3,
            "game_over": False,
        }

    def youtube_stream_exists():
        return bool(
            youtube_stream.get("broadcast_id")
            or youtube_stream.get("watch_url")
            or youtube_stream.get("title")
            or youtube_stream.get("proxy_publish_url")
        )

    def build_youtube_menu():
        items = [
            {"label": "Dashboard", "kind": "screen", "target": "youtube"},
        ]
        if youtube_auth.get("device_pending"):
            items.append({"label": "Check Auth", "kind": "action", "action": "youtube_auth_poll"})
        elif youtube_auth.get("authorized") and not probe_cache.get("auth_required"):
            items.append({"label": "Auth OK", "kind": "noop", "disabled": True})
        else:
            items.append({"label": "Start Auth", "kind": "action", "action": "youtube_auth_start"})
        if allow_restart_auth():
            items.append({"label": "Restart Auth", "kind": "action", "action": "youtube_auth_restart"})
        if (youtube_creation or {}).get("status") == "creating":
            items.append({"label": "Creating...", "kind": "noop", "disabled": True})
        elif youtube_auth.get("authorized") and not probe_cache.get("auth_required"):
            items.append({"label": "Create Stream", "kind": "menu", "target": "youtube_create"})
        else:
            items.append({"label": "Create Stream", "kind": "noop", "disabled": True})
        items.append({"label": "Overlay Demo", "kind": "menu", "target": "youtube_overlay"})
        if youtube_stream.get("qr_payload"):
            items.append({"label": "Stream QR", "kind": "screen", "target": "youtube_qr"})
        return items

    def build_youtube_create_menu():
        audio_label = {"normal": "NORM", "voice": "VOICE", "mute": "MUTE"}.get(youtube_create_audio_mode, youtube_create_audio_mode.upper())
        rotation_label = {"0": "OFF", "90": "+90", "-90": "-90"}.get(youtube_create_rotation, youtube_create_rotation)
        fps_label = {"original": "ORIG", "30": "30FPS", "20": "20FPS"}.get(youtube_create_fps_mode, youtube_create_fps_mode.upper())
        items = [
            {"label": "Use Defaults", "kind": "action", "action": "youtube_create_defaults"},
            {"label": f"Rotation {rotation_label}", "kind": "menu", "target": "youtube_create_rotation"},
            {"label": f"FPS {fps_label}", "kind": "menu", "target": "youtube_create_fps"},
            {"label": f"Sound {audio_label}", "kind": "menu", "target": "youtube_create_audio"},
        ]
        if (youtube_creation or {}).get("status") == "creating":
            items.append({"label": "Confirm Create", "kind": "noop", "disabled": True})
        else:
            items.append({"label": "Confirm Create", "kind": "action", "action": "youtube_create"})
        return items

    def build_youtube_create_audio_menu():
        return [
            {
                "label": label,
                "kind": "action",
                "action": "youtube_create_audio",
                "arg": mode,
                "checked": youtube_create_audio_mode == mode,
            }
            for mode, label in (("normal", "Audio Normal"), ("voice", "Audio Voice"), ("mute", "Audio Mute"))
        ]

    def build_youtube_create_rotation_menu():
        return [
            {
                "label": label,
                "kind": "action",
                "action": "youtube_create_rotation",
                "arg": mode,
                "checked": youtube_create_rotation == mode,
            }
            for mode, label in (("90", "Rotate 90"), ("0", "Rotate Off"), ("-90", "Rotate -90"))
        ]

    def build_youtube_create_fps_menu():
        return [
            {
                "label": label,
                "kind": "action",
                "action": "youtube_create_fps",
                "arg": mode,
                "checked": youtube_create_fps_mode == mode,
            }
            for mode, label in (("original", "Original"), ("30", "30 FPS"), ("20", "20 FPS"))
        ]

    def build_youtube_overlay_menu():
        overlay = load_overlay_state()
        current_html = ""
        html_path = Path(overlay.get("html_path") or "")
        if html_path.exists():
            try:
                current_html = html_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                current_html = ""
        current_mode = "off"
        if overlay.get("enabled"):
            if "STATIC PIC" in current_html:
                current_mode = "static"
            elif "Bangkok Demo" in current_html or "WEATHER" in current_html:
                current_mode = "weather"
            else:
                current_mode = "default"
        items = [
            {"label": "Overlay Off", "kind": "action", "action": "overlay_demo", "arg": "off", "checked": current_mode == "off"},
            {"label": "Static Pic", "kind": "action", "action": "overlay_demo", "arg": "static", "checked": current_mode == "static"},
            {"label": "Weather Card", "kind": "action", "action": "overlay_demo", "arg": "weather", "checked": current_mode == "weather"},
            {"label": "Default Card", "kind": "action", "action": "overlay_demo", "arg": "default", "checked": current_mode == "default"},
        ]
        return items

    def build_update_confirm_menu():
        return [
            {"label": "Yes", "kind": "action", "action": "update_run"},
            {"label": "No", "kind": "action", "action": "update_cancel"},
        ]

    def get_menu_definition(menu_id):
        if menu_id == "youtube":
            return "YouTube", build_youtube_menu()
        if menu_id == "youtube_create":
            return "Create", build_youtube_create_menu()
        if menu_id == "youtube_create_audio":
            return "Create Audio", build_youtube_create_audio_menu()
        if menu_id == "youtube_create_rotation":
            return "Create Rotate", build_youtube_create_rotation_menu()
        if menu_id == "youtube_create_fps":
            return "Create FPS", build_youtube_create_fps_menu()
        if menu_id == "youtube_overlay":
            return "Overlay", build_youtube_overlay_menu()
        if menu_id == "update_confirm":
            return "Update", build_update_confirm_menu()
        return "Main", build_root_menu()

    def current_menu_entry():
        return menu_stack[-1]

    def current_menu_definition():
        title, items = get_menu_definition(current_menu_entry()["id"])
        if not items:
            items = [{"label": "Empty", "kind": "noop", "disabled": True}]
        current_menu_entry()["selected"] = max(0, min(current_menu_entry()["selected"], len(items) - 1))
        return title, items

    def youtube_active():
        menu_id = current_menu_entry()["id"] if menu_stack else "root"
        return current_screen == "youtube" or menu_id.startswith("youtube")

    def probe_active():
        menu_id = current_menu_entry()["id"] if menu_stack else "root"
        return current_screen in ("probe", "settings") or menu_id == "settings"

    def move_menu(delta):
        if ui_mode != "menu":
            return
        _, items = current_menu_definition()
        current_menu_entry()["selected"] = (current_menu_entry()["selected"] + delta) % len(items)

    def go_back():
        nonlocal current_screen, ui_mode
        if ui_mode == "screen":
            current_screen = None
            ui_mode = "menu"
        elif len(menu_stack) > 1:
            menu_stack.pop()
        else:
            ui_mode = "home"

    def go_home():
        nonlocal current_screen, ui_mode
        current_screen = None
        del menu_stack[1:]
        ui_mode = "home"

    def open_menu():
        nonlocal current_screen, ui_mode
        current_screen = None
        del menu_stack[1:]
        ui_mode = "menu"

    def open_menu_target(menu_id):
        menu_stack.append({"id": menu_id, "selected": default_selected_for_menu(menu_id)})

    def close_submenu():
        if len(menu_stack) > 1:
            menu_stack.pop()

    def trigger_youtube_create_settings_audio(mode):
        nonlocal youtube_create_audio_mode
        youtube_create_audio_mode = mode
        set_ui_message(f"Sound {mode.upper()}")
        close_submenu()

    def trigger_youtube_create_settings_rotation(mode):
        nonlocal youtube_create_rotation
        youtube_create_rotation = mode
        set_ui_message(f"Create rot {mode}")
        close_submenu()

    def trigger_youtube_create_settings_fps(mode):
        nonlocal youtube_create_fps_mode
        youtube_create_fps_mode = mode
        set_ui_message(f"Create {mode.upper()}")
        close_submenu()

    def trigger_youtube_create_defaults():
        nonlocal youtube_create_audio_mode, youtube_create_rotation, youtube_create_fps_mode
        youtube_create_audio_mode, youtube_create_rotation, youtube_create_fps_mode = default_create_settings()
        set_ui_message("Defaults loaded")

    def trigger_youtube_auth_restart():
        nonlocal youtube_auth, youtube_status_message, current_screen, ui_mode
        show_busy("Restart auth", screen="youtube", mode="screen")
        try:
            start_device_authorization()
            youtube_auth = get_auth_status()
            youtube_status_message = "Open URL, enter code"
            set_ui_message("Auth restarted")
            current_screen = "youtube"
            ui_mode = "screen"
        except YouTubeLiveError as exc:
            youtube_status_message = fit_text(str(exc), 20)
            set_ui_message(youtube_status_message)
            current_screen = "youtube"
            ui_mode = "screen"
        finally:
            clear_busy()

    def trigger_youtube_create():
        nonlocal youtube_creation, youtube_status_message, current_screen, ui_mode
        if probe_cache.get("auth_required") or not youtube_auth.get("authorized"):
            youtube_status_message = "AUTH FIRST"
            set_ui_message("AUTH FIRST")
            current_screen = "youtube"
            ui_mode = "screen"
            return
        show_busy("Create stream", screen="youtube", mode="screen")
        try:
            start_stream_creation(
                ap_ip=w0,
                audio_mode=youtube_create_audio_mode,
                rotation=youtube_create_rotation,
                fps_mode=youtube_create_fps_mode,
            )
            youtube_creation = load_creation_state()
            youtube_status_message = "Stream is creating"
            set_ui_message(youtube_status_message)
            current_screen = "youtube"
            ui_mode = "screen"
        except YouTubeLiveError as exc:
            youtube_status_message = fit_text(str(exc), 20)
            set_ui_message(youtube_status_message)
            current_screen = "youtube"
            ui_mode = "screen"
        finally:
            clear_busy()

    def trigger_youtube_auth_start():
        nonlocal youtube_auth, youtube_status_message, current_screen, ui_mode
        show_busy("Start auth", screen="youtube", mode="screen")
        try:
            start_device_authorization()
            youtube_auth = get_auth_status()
            youtube_status_message = "Open URL, enter code"
            set_ui_message("Device code ready")
            current_screen = "youtube"
            ui_mode = "screen"
        except YouTubeLiveError as exc:
            youtube_status_message = fit_text(str(exc), 20)
            set_ui_message(youtube_status_message)
            current_screen = "youtube"
            ui_mode = "screen"
        finally:
            clear_busy()

    def trigger_youtube_auth_poll():
        nonlocal youtube_auth, youtube_status_message, current_screen, ui_mode
        show_busy("Check auth", screen="youtube", mode="screen")
        try:
            poll_device_authorization()
            youtube_auth = get_auth_status()
            youtube_status_message = "Authorization OK"
            set_ui_message("YouTube authorized")
            current_screen = "youtube"
            ui_mode = "screen"
        except YouTubeLiveError as exc:
            youtube_auth = get_auth_status()
            youtube_status_message = fit_text(str(exc), 20)
            set_ui_message(youtube_status_message)
            current_screen = "youtube"
            ui_mode = "screen"
        finally:
            clear_busy()

    def trigger_update_cancel():
        close_submenu()
        set_ui_message("Update canceled")

    def trigger_update_run():
        nonlocal current_screen, ui_mode
        if not UPDATE_SCRIPT_PATH.exists():
            set_ui_message("update_ap.sh missing")
            return
        show_busy("Start update", mode="menu")
        try:
            UPDATE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            log_handle = UPDATE_LOG_PATH.open("ab")
            subprocess.Popen(
                ["/bin/bash", str(UPDATE_SCRIPT_PATH)],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=str(UPDATE_SCRIPT_PATH.parent),
                start_new_session=True,
            )
            set_ui_message("Update started")
            current_screen = None
            ui_mode = "menu"
        except Exception as exc:
            logging.exception("Failed to start update script")
            set_ui_message(fit_text(str(exc), 20))
        finally:
            clear_busy()

    def open_selected_item():
        nonlocal current_screen, ui_mode
        if ui_mode != "menu":
            return
        _, items = current_menu_definition()
        item = items[current_menu_entry()["selected"]]
        kind = item.get("kind")
        if kind == "screen":
            current_screen = item.get("target")
            ui_mode = "screen"
        elif kind == "menu":
            open_menu_target(item.get("target", "root"))
            current_screen = None
            ui_mode = "menu"
        elif kind == "action":
            action = item.get("action")
            if action == "youtube_auth_start":
                trigger_youtube_auth_start()
            elif action == "youtube_auth_poll":
                trigger_youtube_auth_poll()
            elif action == "youtube_auth_restart":
                trigger_youtube_auth_restart()
            elif action == "youtube_create":
                trigger_youtube_create()
            elif action == "youtube_create_defaults":
                trigger_youtube_create_defaults()
            elif action == "youtube_create_audio":
                trigger_youtube_create_settings_audio(item.get("arg", "normal"))
            elif action == "youtube_create_rotation":
                trigger_youtube_create_settings_rotation(item.get("arg", "0"))
            elif action == "youtube_create_fps":
                trigger_youtube_create_settings_fps(item.get("arg", "original"))
            elif action == "overlay_demo":
                apply_overlay_demo(item.get("arg", "off"))
            elif action == "update_run":
                trigger_update_run()
            elif action == "update_cancel":
                trigger_update_cancel()
        elif item.get("disabled"):
            set_ui_message(item.get("label", "Unavailable"))

    def handle_pressed_button(name):
        if busy_action:
            return
        logical_name = translate_button_for_rotation(name)
        logging.info("Button pressed: %s -> %s", name, logical_name)
        if name in ("KEY1", "KEY2", "KEY3"):
            return
        if ui_mode == "home":
            if logical_name in ("PRESS", "RIGHT"):
                open_menu()
            return
        if logical_name == "UP":
            move_menu(-1)
        elif logical_name == "DOWN":
            move_menu(1)
        elif logical_name == "PRESS":
            if current_screen == "youtube":
                if youtube_auth.get("device_pending"):
                    trigger_youtube_auth_poll()
                elif not youtube_auth.get("authorized"):
                    trigger_youtube_auth_start()
            elif ui_mode == "menu":
                open_selected_item()
        elif logical_name == "RIGHT":
            open_menu()
        elif logical_name == "LEFT":
            go_back()

    def refresh_youtube_state():
        nonlocal youtube_auth, youtube_creation, youtube_stream, live_input_res, live_output_res
        auth = get_auth_status()
        creation = load_creation_state()
        stream = load_stream_state()
        detected_in = "-"
        relay = stream.get("relay") or {}
        width = relay.get("video_width")
        height = relay.get("video_height")
        if width and height:
            detected_in = f"{width}x{height}"
        if detected_in == "-" and stream.get("mode") == "proxy":
            dims = ffprobe_video_dimensions(relay_probe_url())
            if dims:
                detected_in = f"{dims[0]}x{dims[1]}"
                output_res = rotate_resolution_text(detected_in, stream.get("rotation", "0"))
            else:
                if relay_input_connected():
                    detected_in = "LIVE"
                    output_res = "WAIT"
                else:
                    detected_in = "NOFEED"
                    output_res = "NOFEED"
        else:
            output_res = rotate_resolution_text(detected_in, stream.get("rotation", "0"))
        with state_lock:
            youtube_auth = auth
            youtube_creation = creation
            youtube_stream = stream
            live_input_res = detected_in
            live_output_res = output_res

    def refresh_runtime_loop():
        nonlocal prev, prev_t, cpu_temp, cpu_pct, mem_pct, rx1ps, tx1ps
        nonlocal ap_name, ap_password, w0, w1, active_wifi, ap_ok, cl_ok, signal, last_network_refresh_at
        last_youtube_refresh_at = 0.0
        while True:
            now = time.time()
            did_work = False
            if now - prev_t >= REFRESH_SEC:
                curr = {WLAN_AP: read_bytes(WLAN_AP), WLAN_UP: read_bytes(WLAN_UP)}
                dt = max(0.2, now - prev_t)
                next_cpu_temp = read_cpu_temp_c()
                next_cpu_pct = read_cpu_percent()
                next_mem_pct = read_mem_percent()
                next_rx1ps = max(0, (curr[WLAN_UP]["rx"] - prev[WLAN_UP]["rx"]) / dt)
                next_tx1ps = max(0, (curr[WLAN_UP]["tx"] - prev[WLAN_UP]["tx"]) / dt)
                with state_lock:
                    cpu_temp = next_cpu_temp
                    cpu_pct = next_cpu_pct
                    mem_pct = next_mem_pct
                    rx1ps = next_rx1ps
                    tx1ps = next_tx1ps
                    prev = curr
                    prev_t = now
                did_work = True

            if STATE_REFRESH_EVENT.is_set() or (now - last_network_refresh_at >= NETWORK_FALLBACK_REFRESH_SEC):
                next_ap_name = read_ap_name()
                next_ap_password = read_ap_password()
                next_w0 = ip_only(read_ipv4(WLAN_AP))
                next_w1 = ip_only(read_ipv4(WLAN_UP))
                next_active_wifi = read_active_wifi()
                next_ap_ok = next_ap_name != "unknown" and next_w0 != "-"
                next_cl_ok = next_active_wifi["name"] != "-" and next_w1 != "-"
                next_signal = next_active_wifi["signal"] if next_cl_ok else "-"
                with state_lock:
                    ap_name = next_ap_name
                    ap_password = next_ap_password
                    w0 = next_w0
                    w1 = next_w1
                    active_wifi = next_active_wifi
                    ap_ok = next_ap_ok
                    cl_ok = next_cl_ok
                    signal = next_signal
                    last_network_refresh_at = now
                STATE_REFRESH_EVENT.clear()
                did_work = True

            if now - last_youtube_refresh_at >= YOUTUBE_STATE_REFRESH_SEC:
                refresh_youtube_state()
                last_youtube_refresh_at = now
                did_work = True

            if did_work:
                request_state_refresh()
            time.sleep(0.1)

    def refresh_probe_loop():
        nonlocal probe_cache
        while True:
            now = time.time()
            connectivity = read_nm_connectivity()
            auth_required = connectivity == "portal"
            portal_capture = probe_cache.get("portal_capture", {})
            if auth_required:
                with state_lock:
                    wifi_name = active_wifi.get("name", "-")
                portal_capture = capture_portal_response(wifi_name)
            next_probe = {
                "last_run": now,
                "youtube_ping_ms": ping_latency_ms(YOUTUBE_PING_HOST),
                "youtube_rtmp_ms": tcp_latency_ms(YOUTUBE_RTMP_HOST, YOUTUBE_RTMP_PORT),
                "connectivity": connectivity,
                "portal_suspected": auth_required,
                "internet_ok": connectivity == "full",
                "auth_required": auth_required,
                "portal_capture": portal_capture,
            }
            with state_lock:
                probe_cache = next_probe
            request_state_refresh()
            time.sleep(PROBE_INTERVAL_SEC)

    Thread(target=refresh_runtime_loop, daemon=True).start()
    Thread(target=refresh_probe_loop, daemon=True).start()

    while True:
        now = time.time()
        button_states = read_button_states()
        if BUTTON_EVENT_MODE:
            drain_button_events()
        pressed_events = []
        for name, is_pressed in button_states.items():
            if is_pressed and not button_states_prev[name]:
                pressed_events.append(name)
                handle_pressed_button(name)
            button_states_prev[name] = is_pressed

        state = compose_state(now)

        signature = state_signature(state)
        active_refresh_sec = DISPLAY_REFRESH_SEC
        should_refresh_display = bool(pressed_events) or (
            signature != last_display_signature and now - last_display_at >= active_refresh_sec
        )
        if should_refresh_display:
            render_screen(lcd, state)
            last_display_at = now
            last_display_signature = signature

        should_write_status = (
            signature != last_status_signature and now - last_status_write_at >= STATUS_WRITE_SEC
        ) or bool(pressed_events)
        if should_write_status:
            atomic_write_json(STATUS_PATH, state)
            last_status_write_at = now
            last_status_signature = signature

        time.sleep(BUTTON_POLL_SEC)

if __name__ == "__main__":
    main()

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
from youtube_live import (
    YouTubeLiveError,
    get_auth_status,
    load_creation_state,
    load_stream_state,
    poll_device_authorization,
    set_proxy_audio_mode,
    set_proxy_fps_mode,
    set_proxy_rotation_mode,
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
REFRESH_SEC = float(os.environ.get("REFRESH_SEC", "2.0"))
BUTTON_POLL_SEC = float(os.environ.get("BUTTON_POLL_SEC", "0.05"))
DISPLAY_REFRESH_SEC = float(os.environ.get("DISPLAY_REFRESH_SEC", "0.5"))
PROBE_INTERVAL_SEC = float(os.environ.get("PROBE_INTERVAL_SEC", "60.0"))
NETWORK_FALLBACK_REFRESH_SEC = float(os.environ.get("NETWORK_FALLBACK_REFRESH_SEC", "30.0"))
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

def read_ap_name():
    if HOSTAPD_CONF.exists():
        for line in HOSTAPD_CONF.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("ssid="):
                return line.split("=", 1)[1].strip()
    return "unknown"

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
    draw.rounded_rectangle((16, 31, 112, 96), radius=10, fill=(10, 16, 24), outline=(120, 220, 255))
    draw.rounded_rectangle((21, 36, 107, 91), radius=8, fill=(18, 24, 36), outline=(64, 96, 128))
    draw_hourglass(draw, 64, 54)
    draw.text((30, 72), "PLEASE WAIT", font=FONT, fill=(240, 244, 255))
    draw.text((22, 83), fit_text(busy.get("label", "Working"), 14), font=FONT, fill=(180, 180, 180))

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
    ap_ok = state["ap_ok"]
    cl_ok = state["cl_ok"]
    signal = state["signal"]
    cpu_temp = state["cpu_temp"]
    cpu_pct = state["cpu_pct"]
    mem_pct = state["mem_pct"]

    draw_nokia_chrome(draw, "Overview", right_text="Live", left_soft="Back", right_soft="")
    draw_nokia_panel(draw, (5, 22, 122, 104), title="Status")
    draw.text((10, 28), "AP", font=FONT, fill=NOKIA_TEXT_DIM)
    draw.text((26, 28), "OK" if ap_ok else "NO", font=FONT, fill=(NOKIA_OK if ap_ok else NOKIA_ALERT))
    draw.text((52, 28), "CL", font=FONT, fill=NOKIA_TEXT_DIM)
    draw.text((68, 28), "OK" if cl_ok else "NO", font=FONT, fill=(NOKIA_OK if cl_ok else NOKIA_ALERT))
    draw.text((94, 28), f"{signal}%" if signal != "-" else "--", font=FONT, fill=signal_color(signal))

    draw.text((10, 42), fit_text(state["ap_name"], 18), font=FONT, fill=NOKIA_TEXT)
    draw.text((10, 54), fit_text(state["active_wifi"]["name"], 18), font=FONT, fill=NOKIA_HILITE_DARK)

    draw.line((9, 68, 118, 68), fill=NOKIA_PANEL_EDGE, width=1)
    draw_label_value(draw, 10, 72, "w0", fit_text(state["w0"], 14), NOKIA_TEXT, gap=18)
    draw_label_value(draw, 10, 83, "w1", fit_text(state["w1"], 14), NOKIA_TEXT, gap=18)

    draw.line((9, 94, 118, 94), fill=NOKIA_PANEL_EDGE, width=1)
    draw_label_value(draw, 10, 97, "RX", f"{human_bytes(state['rx1ps'])}/s", NOKIA_OK, gap=18)

    temp_text = "-" if cpu_temp is None else f"{cpu_temp:.0f}C"
    cpu_text = "-" if cpu_pct is None else f"{cpu_pct:.0f}%"
    mem_text = "-" if mem_pct is None else f"{mem_pct:.0f}%"
    draw.text((68, 97), fit_text(f"TX {human_bytes(state['tx1ps'])}/s", 9), font=FONT, fill=NOKIA_WARN)
    draw.text((10, 113), fit_text(f"T {temp_text}", 8), font=FONT, fill=metric_color(cpu_temp, 60, 75))
    draw.text((47, 113), fit_text(f"C {cpu_text}", 8), font=FONT, fill=metric_color(cpu_pct, 60, 85))
    draw.text((86, 113), fit_text(f"M {mem_text}", 8), font=FONT, fill=metric_color(mem_pct, 70, 85))

def render_probe(draw, state):
    probe = state["probe"]
    draw_nokia_chrome(draw, "Probe", right_text="Net", left_soft="Back", right_soft="")
    draw_nokia_panel(draw, (5, 22, 122, 104), title="Link Test")
    draw.text((10, 28), fit_text(state["active_wifi"]["name"], 18), font=FONT, fill=NOKIA_HILITE_DARK)
    draw.text((10, 40), f"IP {fit_text(state['w1'], 15)}", font=FONT, fill=NOKIA_TEXT)

    draw.line((9, 53, 118, 53), fill=NOKIA_PANEL_EDGE, width=1)
    yt_text = "-" if probe["youtube_ping_ms"] is None else f"{probe['youtube_ping_ms']:.0f}ms"
    rtmp_text = "-" if probe["youtube_rtmp_ms"] is None else f"{probe['youtube_rtmp_ms']:.0f}ms"
    draw_label_value(draw, 10, 58, "YT", yt_text, NOKIA_HILITE_DARK, gap=18)
    draw_label_value(draw, 68, 58, "RT", rtmp_text, NOKIA_WARN, gap=18)
    draw_label_value(draw, 10, 71, "NET", fit_text(probe["connectivity"], 11), NOKIA_TEXT, gap=24)

    portal_fill = NOKIA_WARN if probe["portal_suspected"] else NOKIA_OK if probe["internet_ok"] else NOKIA_ALERT
    portal_text = "PORTAL" if probe["portal_suspected"] else "ONLINE" if probe["internet_ok"] else "OFFLINE"
    draw.text((10, 86), portal_text, font=FONT, fill=portal_fill)
    ack_hint = "MENU ACK" if state["portal_ack_configured"] else "NO ACK"
    draw.text((60, 86), ack_hint, font=FONT, fill=NOKIA_TEXT_DIM)

    if state["portal_ack_last"]:
        msg = fit_text(state["portal_ack_last"]["message"], 20)
        fill = NOKIA_OK if state["portal_ack_last"]["ok"] else NOKIA_ALERT
        draw.text((10, 98), msg, font=FONT, fill=fill)

def render_youtube(draw, image, state):
    youtube = state["youtube"]
    auth = youtube.get("auth") or {}
    device_pending = bool(auth.get("device_pending"))
    creating = (youtube.get("creation") or {}).get("status") == "creating"
    draw_nokia_chrome(draw, "YouTube", right_text="Live", left_soft="Back", right_soft="OK")
    draw_nokia_panel(draw, (5, 22, 122, 104), title="Stream")
    if creating:
        creation = youtube.get("creation") or {}
        progress_pct = max(0, min(100, int(creation.get("progress_pct", 0) or 0)))
        stage_message = fit_text(creation.get("message", "Working"), 18)
        bar_left = 14
        bar_top = 76
        bar_right = 113
        bar_bottom = 89
        fill_width = int((bar_right - bar_left - 2) * (progress_pct / 100.0))
        draw.text((16, 39), "STREAM", font=FONT, fill=NOKIA_TEXT)
        draw.text((16, 52), "CREATING", font=FONT, fill=NOKIA_TEXT)
        draw.text((16, 64), stage_message, font=FONT, fill=NOKIA_TEXT_DIM)
        draw.rectangle((bar_left, bar_top, bar_right, bar_bottom), outline=NOKIA_PANEL_EDGE, width=1)
        draw.rectangle((bar_left + 1, bar_top + 1, bar_left + 1 + fill_width, bar_bottom - 1), fill=NOKIA_HILITE)
        draw.text((46, 93), f"{progress_pct:3d}%", font=FONT, fill=NOKIA_TEXT)
        return

    if youtube.get("auth_required") and not device_pending:
        draw.text((24, 42), "AUTH", font=FONT, fill=NOKIA_ALERT)
        draw.text((24, 56), "FIRST", font=FONT, fill=NOKIA_ALERT)
        draw.text((14, 78), fit_text(youtube.get("status_message", "AUTH FIRST"), 16), font=FONT, fill=NOKIA_TEXT_DIM)
        return

    auth_text = "READY" if auth.get("authorized") else "PENDING" if device_pending else "SETUP"
    auth_fill = NOKIA_OK if auth_text == "READY" else NOKIA_WARN if auth_text == "PENDING" else NOKIA_ALERT
    draw.text((12, 28), fit_text(youtube.get("title", "No stream yet"), 18), font=FONT, fill=NOKIA_TEXT)
    draw.text((12, 40), auth_text, font=FONT, fill=auth_fill)
    draw.text((54, 40), fit_text(youtube.get("watch_url", "Use web UI").replace("https://", ""), 11), font=FONT, fill=NOKIA_HILITE_DARK)
    draw.line((10, 52, 117, 52), fill=NOKIA_PANEL_EDGE, width=1)
    draw.text((12, 57), fit_text(youtube.get("status_message", "Use YT menu"), 18), font=FONT, fill=NOKIA_TEXT)
    if device_pending:
        device = auth.get("device") or {}
        code = device.get("user_code", "")
        verify = device.get("verification_url_complete", "") or device.get("verification_url", "")
        expires_at = float(device.get("expires_at", 0) or 0)
        seconds_left = max(0, int(expires_at - time.time())) if expires_at else 0
        expires_text = f"EXP {seconds_left // 60}M{seconds_left % 60:02d}" if seconds_left else "EXP SOON"
        draw.text((12, 71), fit_text(f"CODE {code}", 18), font=FONT, fill=NOKIA_WARN)
        draw.text((12, 83), fit_text(verify.replace("https://", ""), 18), font=FONT, fill=NOKIA_HILITE_DARK)
        draw.text((12, 95), fit_text(expires_text, 18), font=FONT, fill=NOKIA_TEXT_DIM)
        return
    elif youtube.get("mode") == "proxy":
        draw.text((12, 71), fit_text(f"AUD {youtube.get('audio_mode_label', 'Normal')}", 18), font=FONT, fill=NOKIA_HILITE_DARK)
    relay = youtube.get("relay") or {}
    if relay.get("video_width") and relay.get("video_height"):
        video_text = f"{relay.get('video_width')}x{relay.get('video_height')} {str(relay.get('video_orientation', '')).upper()}".strip()
        draw.text((12, 85), fit_text(video_text, 18), font=FONT, fill=NOKIA_OK)
        draw.text((12, 97), fit_text(youtube.get("status_message", "Use YT menu"), 18), font=FONT, fill=NOKIA_TEXT_DIM)
        return
    draw.text((12, 87), fit_text(youtube.get("status_message", "Use YT menu"), 18), font=FONT, fill=NOKIA_TEXT_DIM)

def render_portal_warning(draw, state):
    if not state["probe"].get("auth_required"):
        return

    draw.rounded_rectangle((23, 56, 104, 79), radius=6, fill=(228, 206, 188), outline=NOKIA_ALERT)
    draw.text((34, 62), "AUTH FIRST", font=FONT, fill=NOKIA_ALERT)


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
    ap_name = fit_text(state.get("ap_name", "-"), 16)
    wifi_name = fit_text((state.get("active_wifi") or {}).get("name", "-"), 16)
    signal = state.get("signal", "-")
    cpu_pct = state.get("cpu_pct")
    cpu_text = "--" if cpu_pct is None else f"{cpu_pct:.0f}%"
    temp = state.get("cpu_temp")
    temp_text = "--" if temp is None else f"{temp:.0f}C"
    mode_text = "DIRECT"
    if state.get("youtube", {}).get("mode") == "proxy":
        mode_text = state["youtube"].get("audio_mode_short", "NORM")

    draw_nokia_chrome(draw, "Home", right_text="6230", left_soft="", center_soft="Menu", right_soft="")
    draw_nokia_panel(draw, (7, 24, 120, 77), title="Network")
    draw.text((14, 33), fit_text(ap_name.upper(), 14), font=FONT, fill=NOKIA_TEXT)
    draw.text((14, 46), fit_text(wifi_name, 16), font=FONT, fill=NOKIA_HILITE_DARK)
    draw.text((14, 59), fit_text(f"SIG {signal}%", 14) if signal != "-" else "SIG --", font=FONT, fill=signal_color(signal))

    draw_nokia_panel(draw, (7, 81, 120, 103), title="Quick")
    draw.text((13, 91), fit_text(f"YT {mode_text}", 10), font=FONT, fill=NOKIA_WARN)
    draw.text((67, 91), fit_text(f"CPU {cpu_text}", 10), font=FONT, fill=metric_color(cpu_pct, 60, 85))
    draw.text((13, 100), fit_text(f"TEMP {temp_text}", 12), font=FONT, fill=metric_color(temp, 60, 75))


def render_game_pong(draw, state):
    game = state["games"]["pong"]
    draw_nokia_chrome(draw, "Pong", right_text=f"{game['player_score']}:{game['cpu_score']}", left_soft="Back", right_soft="")
    draw.rounded_rectangle((4, 20, 123, 104), radius=6, fill=(190, 204, 181), outline=NOKIA_PANEL_EDGE)
    draw.line((63, 20, 63, 104), fill=NOKIA_PANEL_EDGE, width=1)
    draw.rectangle((4, game["player_y"], 7, game["player_y"] + 20), fill=NOKIA_OK)
    draw.rectangle((120, game["cpu_y"], 123, game["cpu_y"] + 20), fill=NOKIA_WARN)
    draw.rectangle((game["ball_x"], game["ball_y"], game["ball_x"] + 4, game["ball_y"] + 4), fill=NOKIA_TEXT)
    if game["game_over"]:
        draw.rounded_rectangle((18, 44, 109, 84), radius=6, fill=NOKIA_PANEL, outline=NOKIA_PANEL_EDGE)
        draw.text((33, 50), "GAME OVER", font=FONT, fill=NOKIA_WARN)
        draw.text((28, 64), "PRESS RESTART", font=FONT, fill=NOKIA_TEXT)


def render_game_catch(draw, state):
    game = state["games"]["catch"]
    draw_nokia_chrome(draw, "Catch", right_text=f"S{game['score']} L{game['lives']}", left_soft="Back", right_soft="")
    draw.rounded_rectangle((4, 20, 123, 104), radius=6, fill=(190, 204, 181), outline=NOKIA_PANEL_EDGE)
    for item in game["drops"]:
        draw.rectangle((item["x"], item["y"], item["x"] + 4, item["y"] + 4), fill=NOKIA_WARN)
    draw.rectangle((game["player_x"], 100, game["player_x"] + 20, 104), fill=NOKIA_HILITE)
    if game["game_over"]:
        draw.rounded_rectangle((18, 44, 109, 84), radius=6, fill=NOKIA_PANEL, outline=NOKIA_PANEL_EDGE)
        draw.text((33, 50), "GAME OVER", font=FONT, fill=NOKIA_WARN)
        draw.text((28, 64), "PRESS RESTART", font=FONT, fill=NOKIA_TEXT)


def render_menu(draw, state):
    title = fit_text(state.get("menu_title", "Menu").upper(), 12)
    items = state.get("menu_items") or []
    selected = state.get("menu_selected", 0)
    if items:
        selected = max(0, min(selected, len(items) - 1))
    else:
        selected = 0

    draw_nokia_chrome(draw, title, right_text="Menu", left_soft="Back", center_soft="", right_soft="Open")
    draw_nokia_panel(draw, (5, 22, 122, 104), title="Items")

    visible_rows = 6
    row_height = 14
    top_index = 0
    if len(items) > visible_rows:
        top_index = max(0, min(selected - (visible_rows - 1), len(items) - visible_rows))

    for row in range(visible_rows):
        idx = top_index + row
        if idx >= len(items):
            break
        item = items[idx]
        y = 28 + (row * row_height)
        is_selected = idx == selected
        label = item.get("label", "-")
        if item.get("checked"):
            label = f"{label} *"
        text = fit_text(label, 18)
        fill = NOKIA_TEXT if not item.get("disabled") else NOKIA_TEXT_DIM
        if is_selected:
            draw.rounded_rectangle((9, y - 1, 117, y + 10), radius=4, fill=NOKIA_HILITE, outline=NOKIA_HILITE_DARK)
            fill = (236, 241, 238) if not item.get("disabled") else (196, 204, 200)
        draw.text((14, y), text, font=FONT, fill=fill)

    message = fit_text(state.get("menu_message", ""), 20)
    if message:
        draw.text((10, 96), message, font=FONT, fill=NOKIA_WARN)

def render_screen(lcd, state):
    image = Image.new("RGB", (128, 128), NOKIA_BG)
    draw = ImageDraw.Draw(image)

    if state.get("ui_mode") == "home":
        render_home(draw, state)
    elif state.get("ui_mode") == "menu":
        render_menu(draw, state)
    elif state["screen_id"] == "matrix":
        render_matrix(draw, state)
    elif state["screen_id"] == "game_pong":
        render_game_pong(draw, state)
    elif state["screen_id"] == "game_catch":
        render_game_catch(draw, state)
    elif state.get("ui_mode") == "screen":
        draw_nokia_chrome(draw, state.get("screen_title", "Screen"), left_soft="Back", right_soft="")
    if state.get("ui_mode") == "screen" and state["screen_id"] == "overview":
        render_overview(draw, state)
    elif state.get("ui_mode") == "screen" and state["screen_id"] == "probe":
        render_probe(draw, state)
    elif state.get("ui_mode") == "screen" and state["screen_id"] == "youtube":
        render_youtube(draw, image, state)
    if state.get("ui_mode") == "screen" and state["screen_id"] in ("overview", "probe", "youtube"):
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

    def compose_state(now):
        menu_title, menu_items = current_menu_definition()
        return {
            "ui_mode": ui_mode,
            "screen_id": current_screen,
            "screen_title": {
                "overview": "Overview",
                "probe": "Probe",
                "youtube": "YouTube",
                "matrix": "Matrix",
                "game_pong": "Pong",
                "game_catch": "Catch",
            }.get(current_screen, "Menu"),
            "menu_title": menu_title,
            "menu_items": menu_items,
            "menu_selected": current_menu_entry()["selected"],
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
                "relay": youtube_stream.get("relay", {}),
                "creation": youtube_creation,
                "status_message": youtube_status_message if youtube_auth.get("device_pending") else "AUTH FIRST" if (probe_cache.get("auth_required") or not youtube_auth.get("authorized")) and (youtube_creation or {}).get("status") != "creating" else youtube_status_message,
                "auth_required": probe_cache.get("auth_required") or not youtube_auth.get("authorized"),
            },
            "matrix": {
                "columns": matrix_columns,
                "tick": matrix_tick,
            },
            "games": {
                "pong": pong_game,
                "catch": catch_game,
            },
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
            {"label": "Matrix", "kind": "screen", "target": "matrix"},
            {"label": "Games", "kind": "menu", "target": "games"},
            {"label": "FFmpeg", "kind": "menu", "target": "ffmpeg"},
            {"label": "Update", "kind": "action", "action": "update_run"},
            {"label": "Settings", "kind": "menu", "target": "settings"},
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

    def build_youtube_menu():
        items = [
            {"label": "Dashboard", "kind": "screen", "target": "youtube"},
        ]
        if youtube_auth.get("device_pending"):
            items.append({"label": "Check Auth", "kind": "action", "action": "youtube_auth_poll"})
        else:
            items.append({"label": "Start Auth", "kind": "action", "action": "youtube_auth_start"})
        if (youtube_creation or {}).get("status") == "creating":
            items.append({"label": "Create Stream", "kind": "noop", "disabled": True})
        else:
            items.append({"label": "Create Stream", "kind": "action", "action": "youtube_create"})
        return items

    def build_ffmpeg_menu():
        items = [
            {"label": "Mode", "kind": "noop", "disabled": True},
        ]
        if youtube_stream.get("mode") == "proxy":
            for mode, label in (
                ("normal", "Audio Normal"),
                ("voice", "Audio Voice"),
                ("mute", "Audio Mute"),
            ):
                items.append(
                    {
                        "label": label,
                        "kind": "action",
                        "action": "youtube_audio",
                        "arg": mode,
                        "checked": youtube_stream.get("audio_mode") == mode,
                    }
                )
            items.append({"label": "Rotate", "kind": "noop", "disabled": True})
            for mode, label in (
                ("90", "Rotate 90"),
                ("0", "Rotate Off"),
                ("-90", "Rotate -90"),
            ):
                items.append(
                    {
                        "label": label,
                        "kind": "action",
                        "action": "youtube_rotation",
                        "arg": mode,
                        "checked": youtube_stream.get("rotation") == mode,
                    }
                )
            items.append({"label": "FPS", "kind": "menu", "target": "ffmpeg_fps"})
        else:
            items.append({"label": "Needs proxy mode", "kind": "noop", "disabled": True})
        return items

    def build_ffmpeg_fps_menu():
        items = [
            {"label": "Frame Rate", "kind": "noop", "disabled": True},
        ]
        if youtube_stream.get("mode") == "proxy":
            for mode, label in (
                ("original", "Original"),
                ("30", "30 FPS"),
                ("20", "20 FPS"),
            ):
                items.append(
                    {
                        "label": label,
                        "kind": "action",
                        "action": "youtube_fps",
                        "arg": mode,
                        "checked": youtube_stream.get("fps_mode") == mode,
                    }
                )
        else:
            items.append({"label": "Needs proxy mode", "kind": "noop", "disabled": True})
        return items

    def build_settings_menu():
        items = [
            {"label": "Overview", "kind": "screen", "target": "overview"},
            {"label": "Probe", "kind": "screen", "target": "probe"},
        ]
        if CAPTIVE_PORTAL_ACK_CMD:
            items.append({"label": "Portal Ack", "kind": "action", "action": "portal_ack"})
        return items

    def get_menu_definition(menu_id):
        if menu_id == "youtube":
            return "YouTube", build_youtube_menu()
        if menu_id == "games":
            return "Games", [
                {"label": "Pong", "kind": "screen", "target": "game_pong"},
                {"label": "Catch", "kind": "screen", "target": "game_catch"},
            ]
        if menu_id == "ffmpeg":
            return "FFmpeg", build_ffmpeg_menu()
        if menu_id == "ffmpeg_fps":
            return "FPS", build_ffmpeg_fps_menu()
        if menu_id == "settings":
            return "Settings", build_settings_menu()
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
        return current_screen == "youtube" or menu_id in ("youtube", "ffmpeg")

    def probe_active():
        menu_id = current_menu_entry()["id"] if menu_stack else "root"
        return current_screen == "probe" or menu_id == "settings"

    def matrix_active():
        return ui_mode == "screen" and current_screen == "matrix"

    def game_active():
        return ui_mode == "screen" and current_screen in ("game_pong", "game_catch")

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
        ui_mode = "menu"

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
            start_stream_creation(ap_ip=w0)
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

    def trigger_youtube_audio(mode):
        nonlocal youtube_stream, youtube_status_message, current_screen, ui_mode
        show_busy("Audio mode", screen="youtube", mode="screen")
        try:
            youtube_stream = set_proxy_audio_mode(mode)
            youtube_status_message = f"AUDIO {youtube_stream.get('audio_mode_short', mode.upper())}"
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

    def trigger_youtube_rotation(mode):
        nonlocal youtube_stream, youtube_status_message, current_screen, ui_mode
        show_busy("Rotation", screen="youtube", mode="screen")
        try:
            youtube_stream = set_proxy_rotation_mode(mode)
            youtube_status_message = f"ROT {youtube_stream.get('rotation_short', mode)}"
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

    def trigger_youtube_fps(mode):
        nonlocal youtube_stream, youtube_status_message, current_screen, ui_mode
        show_busy("FPS mode", screen="youtube", mode="screen")
        try:
            youtube_stream = set_proxy_fps_mode(mode)
            youtube_status_message = youtube_stream.get("fps_mode_short", mode.upper())
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

    def trigger_portal_ack():
        nonlocal portal_ack_last, current_screen, ui_mode
        show_busy("Portal ack", screen="probe", mode="screen")
        portal_ack_last = perform_portal_ack()
        set_ui_message(portal_ack_last["message"])
        current_screen = "probe"
        ui_mode = "screen"
        clear_busy()

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
        nonlocal current_screen, ui_mode, pong_game, catch_game
        if ui_mode != "menu":
            return
        _, items = current_menu_definition()
        item = items[current_menu_entry()["selected"]]
        kind = item.get("kind")
        if kind == "screen":
            current_screen = item.get("target")
            if current_screen == "game_pong":
                pong_game = reset_pong()
            elif current_screen == "game_catch":
                catch_game = reset_catch()
            ui_mode = "screen"
        elif kind == "menu":
            menu_stack.append({"id": item.get("target", "root"), "selected": 0})
            current_screen = None
            ui_mode = "menu"
        elif kind == "action":
            action = item.get("action")
            if action == "youtube_auth_start":
                trigger_youtube_auth_start()
            elif action == "youtube_auth_poll":
                trigger_youtube_auth_poll()
            elif action == "youtube_create":
                trigger_youtube_create()
            elif action == "youtube_audio":
                trigger_youtube_audio(item.get("arg", "normal"))
            elif action == "youtube_rotation":
                trigger_youtube_rotation(item.get("arg", "0"))
            elif action == "youtube_fps":
                trigger_youtube_fps(item.get("arg", "original"))
            elif action == "update_run":
                trigger_update_run()
            elif action == "portal_ack":
                trigger_portal_ack()
        elif item.get("disabled"):
            set_ui_message(item.get("label", "Unavailable"))

    def handle_pressed_button(name):
        nonlocal pong_game, catch_game
        if busy_action:
            return
        logical_name = translate_button_for_rotation(name)
        logging.info("Button pressed: %s -> %s", name, logical_name)
        if name in ("KEY1", "KEY2", "KEY3"):
            return
        if ui_mode == "home":
            if logical_name == "PRESS":
                open_menu()
            return
        if logical_name == "UP":
            if current_screen == "game_pong":
                pong_game["player_y"] = max(18, pong_game["player_y"] - 7)
            elif current_screen == "game_catch":
                catch_game["player_x"] = max(4, catch_game["player_x"] - 8)
            else:
                move_menu(-1)
        elif logical_name == "DOWN":
            if current_screen == "game_pong":
                pong_game["player_y"] = min(104, pong_game["player_y"] + 7)
            elif current_screen == "game_catch":
                catch_game["player_x"] = min(103, catch_game["player_x"] + 8)
            else:
                move_menu(1)
        elif logical_name in ("PRESS", "RIGHT"):
            if current_screen == "youtube":
                if youtube_auth.get("device_pending"):
                    trigger_youtube_auth_poll()
                elif not youtube_auth.get("authorized"):
                    trigger_youtube_auth_start()
            elif current_screen == "game_pong" and pong_game.get("game_over"):
                pong_game = reset_pong()
            elif current_screen == "game_catch" and catch_game.get("game_over"):
                catch_game = reset_catch()
            elif ui_mode == "menu":
                open_selected_item()
        elif logical_name == "LEFT":
            go_back()

    pong_game = reset_pong()
    catch_game = reset_catch()

    while True:
        now = time.time()
        if now - prev_t >= REFRESH_SEC:
            curr = {WLAN_AP: read_bytes(WLAN_AP), WLAN_UP: read_bytes(WLAN_UP)}
            dt = max(0.2, now - prev_t)
            cpu_temp = read_cpu_temp_c()
            cpu_pct = read_cpu_percent()
            mem_pct = read_mem_percent()
            rx1ps = max(0, (curr[WLAN_UP]["rx"] - prev[WLAN_UP]["rx"]) / dt)
            tx1ps = max(0, (curr[WLAN_UP]["tx"] - prev[WLAN_UP]["tx"]) / dt)
            if youtube_active():
                youtube_auth = get_auth_status()
                youtube_creation = load_creation_state()
                youtube_stream = load_stream_state()
            prev = curr
            prev_t = now

        should_refresh_network = STATE_REFRESH_EVENT.is_set() or (
            now - last_network_refresh_at >= NETWORK_FALLBACK_REFRESH_SEC
        )
        if should_refresh_network:
            STATE_REFRESH_EVENT.clear()
            ap_name = read_ap_name()
            w0 = ip_only(read_ipv4(WLAN_AP))
            w1 = ip_only(read_ipv4(WLAN_UP))
            active_wifi = read_active_wifi()
            ap_ok = ap_name != "unknown" and w0 != "-"
            cl_ok = active_wifi["name"] != "-" and w1 != "-"
            signal = active_wifi["signal"] if cl_ok else "-"
            last_network_refresh_at = now

        button_states = read_button_states()
        if BUTTON_EVENT_MODE:
            drain_button_events()
        pressed_events = []
        for name, is_pressed in button_states.items():
            if is_pressed and not button_states_prev[name]:
                pressed_events.append(name)
                handle_pressed_button(name)
            button_states_prev[name] = is_pressed

        if matrix_active():
            matrix_tick += 1
            for idx, col in enumerate(matrix_columns):
                if matrix_tick % col["speed"] != 0:
                    continue
                col["head"] += 1
                if col["head"] - col["length"] > MATRIX_ROWS:
                    col["head"] = -((idx * 7 + matrix_tick) % MATRIX_ROWS)
                    col["length"] = 4 + ((idx + matrix_tick) % 9)
                    shift = (matrix_tick + idx * 3) % len(MATRIX_CHARS)
                    col["chars"] = [MATRIX_CHARS[(shift + row * 5) % len(MATRIX_CHARS)] for row in range(MATRIX_ROWS)]

        if current_screen == "game_pong" and game_active() and not pong_game["game_over"]:
            pong_game["cpu_y"] += 3 if pong_game["ball_y"] > pong_game["cpu_y"] + 10 else -3 if pong_game["ball_y"] < pong_game["cpu_y"] + 10 else 0
            pong_game["cpu_y"] = max(18, min(104, pong_game["cpu_y"]))
            pong_game["ball_x"] += pong_game["ball_vx"]
            pong_game["ball_y"] += pong_game["ball_vy"]
            if pong_game["ball_y"] <= 18 or pong_game["ball_y"] >= 122:
                pong_game["ball_vy"] *= -1
                pong_game["ball_y"] = max(18, min(122, pong_game["ball_y"]))
            if pong_game["ball_x"] <= 8 and pong_game["player_y"] - 2 <= pong_game["ball_y"] <= pong_game["player_y"] + 22:
                pong_game["ball_vx"] = abs(pong_game["ball_vx"])
            if pong_game["ball_x"] >= 116 and pong_game["cpu_y"] - 2 <= pong_game["ball_y"] <= pong_game["cpu_y"] + 22:
                pong_game["ball_vx"] = -abs(pong_game["ball_vx"])
            if pong_game["ball_x"] < 0:
                pong_game["cpu_score"] += 1
                pong_game["ball_x"], pong_game["ball_y"] = 62, 62
                pong_game["ball_vx"] = 3
                pong_game["ball_vy"] = random.choice((-2, -1, 1, 2))
            elif pong_game["ball_x"] > 124:
                pong_game["player_score"] += 1
                pong_game["ball_x"], pong_game["ball_y"] = 62, 62
                pong_game["ball_vx"] = -3
                pong_game["ball_vy"] = random.choice((-2, -1, 1, 2))
            if pong_game["player_score"] >= 5 or pong_game["cpu_score"] >= 5:
                pong_game["game_over"] = True

        if current_screen == "game_catch" and game_active() and not catch_game["game_over"]:
            catch_game["spawn_tick"] += 1
            if catch_game["spawn_tick"] % 8 == 0:
                catch_game["drops"].append({"x": random.randint(6, 116), "y": 18, "vy": random.randint(4, 6)})
            next_drops = []
            for item in catch_game["drops"]:
                item["y"] += item["vy"]
                caught = item["y"] >= 114 and catch_game["player_x"] - 2 <= item["x"] <= catch_game["player_x"] + 22
                missed = item["y"] > 123
                if caught:
                    catch_game["score"] += 1
                elif missed:
                    catch_game["lives"] -= 1
                else:
                    next_drops.append(item)
            catch_game["drops"] = next_drops
            if catch_game["lives"] <= 0:
                catch_game["game_over"] = True

        if probe_active() and now - probe_cache["last_run"] >= PROBE_INTERVAL_SEC:
            connectivity = read_nm_connectivity()
            auth_required = connectivity == "portal"
            portal_capture = probe_cache.get("portal_capture", {})
            if auth_required:
                portal_capture = capture_portal_response(active_wifi.get("name", "-"))
            probe_cache = {
                "last_run": now,
                "youtube_ping_ms": ping_latency_ms(YOUTUBE_PING_HOST),
                "youtube_rtmp_ms": tcp_latency_ms(YOUTUBE_RTMP_HOST, YOUTUBE_RTMP_PORT),
                "connectivity": connectivity,
                "portal_suspected": auth_required,
                "internet_ok": connectivity == "full",
                "auth_required": auth_required,
                "portal_capture": portal_capture,
            }

        state = compose_state(now)

        signature = state_signature(state)
        active_refresh_sec = game_refresh_sec if current_screen in ("game_pong", "game_catch") and ui_mode == "screen" else DISPLAY_REFRESH_SEC
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

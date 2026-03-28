#!/usr/bin/env python3
import json
import logging
import os
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
    qrcode as youtube_qrcode,
    set_proxy_audio_mode,
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

def read_button_states():
    if BUTTON_EVENT_MODE:
        with BUTTON_EVENT_LOCK:
            return dict(BUTTON_STATE_CACHE)
    return {name: button_pressed(name, pin) for name, pin in BUTTON_PINS.items()}

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

    draw.text((3, 3), "AP", font=FONT, fill=(140, 170, 210))
    draw.text((19, 3), "OK" if ap_ok else "NO", font=FONT, fill=((120, 255, 160) if ap_ok else (255, 96, 96)))
    draw.text((46, 3), "CL", font=FONT, fill=(140, 170, 210))
    draw.text((62, 3), "OK" if cl_ok else "NO", font=FONT, fill=((120, 255, 160) if cl_ok else (255, 96, 96)))
    draw.text((90, 3), f"{signal}%" if signal != "-" else "--", font=FONT, fill=signal_color(signal))

    draw.text((3, 22), fit_text(state["ap_name"], 20), font=FONT, fill=(240, 244, 255))
    draw.text((3, 35), fit_text(state["active_wifi"]["name"], 20), font=FONT, fill=(120, 220, 255))

    draw.line((2, 49, 125, 49), fill=(24, 44, 68), width=1)
    draw_label_value(draw, 3, 54, "w0", fit_text(state["w0"], 15), (255, 255, 255), gap=18)
    draw_label_value(draw, 3, 67, "w1", fit_text(state["w1"], 15), (255, 255, 255), gap=18)

    draw.line((2, 81, 125, 81), fill=(24, 44, 68), width=1)
    draw_label_value(draw, 3, 86, "RX", f"{human_bytes(state['rx1ps'])}/s", (120, 255, 160), gap=18)
    draw_label_value(draw, 3, 99, "TX", f"{human_bytes(state['tx1ps'])}/s", (255, 210, 90), gap=18)

    draw.line((2, 113, 125, 113), fill=(24, 44, 68), width=1)
    temp_text = "-" if cpu_temp is None else f"{cpu_temp:.0f}C"
    cpu_text = "-" if cpu_pct is None else f"{cpu_pct:.0f}%"
    mem_text = "-" if mem_pct is None else f"{mem_pct:.0f}%"
    draw.text((3, 117), "T", font=FONT, fill=(140, 170, 210))
    draw.text((13, 117), temp_text, font=FONT, fill=metric_color(cpu_temp, 60, 75))
    draw.text((43, 117), "C", font=FONT, fill=(140, 170, 210))
    draw.text((53, 117), cpu_text, font=FONT, fill=metric_color(cpu_pct, 60, 85))
    draw.text((83, 117), "M", font=FONT, fill=(140, 170, 210))
    draw.text((93, 117), mem_text, font=FONT, fill=metric_color(mem_pct, 70, 85))

def render_probe(draw, state):
    probe = state["probe"]
    draw.text((3, 3), "PROBE", font=FONT, fill=(140, 170, 210))
    draw.text((3, 20), fit_text(state["active_wifi"]["name"], 20), font=FONT, fill=(120, 220, 255))
    draw.text((3, 33), f"IP {fit_text(state['w1'], 16)}", font=FONT, fill=(240, 244, 255))

    draw.line((2, 48, 125, 48), fill=(24, 44, 68), width=1)
    yt_text = "-" if probe["youtube_ping_ms"] is None else f"{probe['youtube_ping_ms']:.0f}ms"
    rtmp_text = "-" if probe["youtube_rtmp_ms"] is None else f"{probe['youtube_rtmp_ms']:.0f}ms"
    draw_label_value(draw, 3, 54, "YT", yt_text, (120, 220, 255), gap=18)
    draw_label_value(draw, 64, 54, "RT", rtmp_text, (255, 210, 90), gap=18)
    draw_label_value(draw, 3, 67, "NET", fit_text(probe["connectivity"], 12), (240, 244, 255), gap=24)

    portal_fill = (255, 210, 90) if probe["portal_suspected"] else (120, 255, 160) if probe["internet_ok"] else (255, 96, 96)
    portal_text = "PORTAL" if probe["portal_suspected"] else "ONLINE" if probe["internet_ok"] else "OFFLINE"
    draw.text((3, 82), portal_text, font=FONT, fill=portal_fill)
    ack_hint = "K3=ACK" if state["portal_ack_configured"] else "no-ack"
    draw.text((58, 82), ack_hint, font=FONT, fill=(180, 180, 180))

    if state["portal_ack_last"]:
        msg = fit_text(state["portal_ack_last"]["message"], 20)
        fill = (120, 255, 160) if state["portal_ack_last"]["ok"] else (255, 96, 96)
        draw.text((3, 96), msg, font=FONT, fill=fill)

def build_qr_image(payload, size=116):
    if not payload or youtube_qrcode is None:
        return None
    image = youtube_qrcode.make(payload).convert("RGB")
    image = image.resize((size, size))
    return image

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
        draw.text((10, 119), "RIGHT=BACK", font=FONT, fill=(180, 180, 180))
        return

    if youtube.get("auth_required") and not youtube.get("qr_payload"):
        draw.rectangle((4, 22, 123, 104), outline=(255, 96, 96), width=2)
        draw.rectangle((8, 26, 119, 100), outline=(255, 96, 96), width=1)
        draw.text((22, 40), "AUTH", font=FONT, fill=(255, 230, 230))
        draw.text((22, 56), "FIRST", font=FONT, fill=(255, 230, 230))
        draw.text((14, 78), fit_text(youtube.get("status_message", "AUTH FIRST"), 16), font=FONT, fill=(255, 210, 210))
        draw.text((10, 119), "RIGHT=BACK", font=FONT, fill=(180, 180, 180))
        return

    if youtube.get("qr_payload"):
        qr_image = build_qr_image(youtube["qr_payload"])
        if qr_image is not None:
            image.paste(qr_image, (6, 6))
            draw.rectangle((0, 118, 127, 127), fill="BLACK")
            if youtube.get("mode") == "proxy":
                draw.text((4, 119), fit_text(youtube.get("audio_mode_short", "NORM"), 5), font=FONT, fill=(140, 170, 210))
                draw.text((40, 119), "P=NEW 1N2V3M", font=FONT, fill=(180, 180, 180))
            else:
                draw.text((4, 119), fit_text(youtube.get("mode", "direct").upper(), 7), font=FONT, fill=(140, 170, 210))
                draw.text((46, 119), "PRESS=NEW", font=FONT, fill=(180, 180, 180))
            return

    draw.text((3, 3), "YOUTUBE", font=FONT, fill=(140, 170, 210))
    auth_text = "READY" if youtube["auth"].get("authorized") else "PENDING" if youtube["auth"].get("device_pending") else "SETUP"
    auth_fill = (120, 255, 160) if auth_text == "READY" else (255, 210, 90) if auth_text == "PENDING" else (255, 96, 96)
    draw.text((72, 3), auth_text, font=FONT, fill=auth_fill)
    draw.text((3, 20), fit_text(youtube.get("title", "No stream yet"), 20), font=FONT, fill=(240, 244, 255))
    draw.text((3, 33), fit_text(youtube.get("watch_url", "Use web UI"), 20), font=FONT, fill=(120, 220, 255))
    draw.line((2, 48, 125, 48), fill=(24, 44, 68), width=1)
    draw.text((3, 56), fit_text(youtube.get("status_message", "LEFT=YT PRESS=GO"), 20), font=FONT, fill=(240, 244, 255))
    if youtube["auth"].get("device_pending"):
        code = (youtube["auth"].get("device") or {}).get("user_code", "")
        draw.text((3, 72), fit_text(f"CODE {code}", 20), font=FONT, fill=(255, 210, 90))
    elif youtube.get("mode") == "proxy":
        draw.text((3, 72), fit_text(f"AUD {youtube.get('audio_mode_label', 'Normal')}", 20), font=FONT, fill=(120, 220, 255))
    if youtube.get("mode") == "proxy":
        draw.text((3, 88), "1=norm 2=vox 3=mute", font=FONT, fill=(180, 180, 180))
        draw.text((3, 101), "P=create R=back", font=FONT, fill=(180, 180, 180))
    else:
        draw.text((3, 88), "PRESS=create", font=FONT, fill=(180, 180, 180))
        draw.text((3, 101), "RIGHT=back", font=FONT, fill=(180, 180, 180))

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
    draw.text((74, 3), "RIGHT=BK", font=FONT, fill=(120, 180, 120))

def render_screen(lcd, state):
    image = Image.new("RGB", (128, 128), "BLACK")
    draw = ImageDraw.Draw(image)

    if state["page"] == 3:
        render_matrix(draw, state)
    else:
        draw.rectangle((0, 0, 127, 127), fill="BLACK")
        draw.rectangle((0, 0, 127, 15), fill=(18, 18, 18))
        draw.line((0, 16, 127, 16), fill=(64, 64, 64), width=1)
        draw.text((108, 3), f"P{state['page'] + 1}", font=FONT, fill=(180, 180, 180))
    if state["page"] == 0:
        render_overview(draw, state)
    elif state["page"] == 1:
        render_probe(draw, state)
    elif state["page"] == 2:
        render_youtube(draw, image, state)
    if state["page"] != 3:
        render_portal_warning(draw, state)

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
    youtube_status_message = "LEFT=YT PRESS=GO"
    page = 0
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
    request_state_refresh()

    def handle_pressed_button(name):
        nonlocal page, youtube_status_message, youtube_creation, youtube_stream
        logging.info("Button pressed: %s", name)
        if name == "UP" and page == 0:
            page = 3
        elif name == "LEFT":
            page = 2
        elif name == "RIGHT" and page in (2, 3):
            page = 0
        elif name == "PRESS" and page == 2:
            if probe_cache.get("auth_required") or not youtube_auth.get("authorized"):
                youtube_status_message = "AUTH FIRST"
            else:
                try:
                    start_stream_creation(ap_ip=w0)
                    youtube_creation = load_creation_state()
                    youtube_status_message = "Stream is creating"
                except YouTubeLiveError as exc:
                    youtube_status_message = fit_text(str(exc), 20)
        elif name == "KEY1" and page == 2 and youtube_stream.get("mode") == "proxy":
            try:
                youtube_stream = set_proxy_audio_mode("normal")
                youtube_status_message = f"AUDIO {youtube_stream.get('audio_mode_short', 'NORM')}"
            except YouTubeLiveError as exc:
                youtube_status_message = fit_text(str(exc), 20)
        elif name == "KEY2" and page == 2 and youtube_stream.get("mode") == "proxy":
            try:
                youtube_stream = set_proxy_audio_mode("voice")
                youtube_status_message = f"AUDIO {youtube_stream.get('audio_mode_short', 'VOICE')}"
            except YouTubeLiveError as exc:
                youtube_status_message = fit_text(str(exc), 20)
        elif name == "KEY3" and page == 2 and youtube_stream.get("mode") == "proxy":
            try:
                youtube_stream = set_proxy_audio_mode("mute")
                youtube_status_message = f"AUDIO {youtube_stream.get('audio_mode_short', 'MUTE')}"
            except YouTubeLiveError as exc:
                youtube_status_message = fit_text(str(exc), 20)
        elif name == "PRESS":
            page = (page + 1) % 2

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
        pressed_events = []
        if BUTTON_EVENT_MODE:
            for event_ts, name, is_pressed in drain_button_events():
                button_states_prev[name] = is_pressed
                if not is_pressed:
                    continue
                pressed_events.append(name)
                handle_pressed_button(name)
        else:
            for name, is_pressed in button_states.items():
                if is_pressed and not button_states_prev[name]:
                    pressed_events.append(name)
                    handle_pressed_button(name)
                button_states_prev[name] = is_pressed

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

        if now - probe_cache["last_run"] >= PROBE_INTERVAL_SEC:
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

        if "KEY3" in pressed_events and probe_cache["portal_suspected"] and not (page == 2 and youtube_stream.get("mode") == "proxy"):
            portal_ack_last = perform_portal_ack()
            logging.info(
                "Portal action via KEY3: ok=%s message=%s",
                portal_ack_last["ok"],
                portal_ack_last["message"],
            )

        state = {
            "page": page,
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
                "creation": youtube_creation,
                "status_message": "AUTH FIRST" if (probe_cache.get("auth_required") or not youtube_auth.get("authorized")) and (youtube_creation or {}).get("status") != "creating" else youtube_status_message,
                "auth_required": probe_cache.get("auth_required") or not youtube_auth.get("authorized"),
            },
            "matrix": {
                "columns": matrix_columns,
                "tick": matrix_tick,
            },
            "updated_at": now,
        }

        signature = state_signature(state)
        should_refresh_display = bool(pressed_events) or (
            signature != last_display_signature and now - last_display_at >= DISPLAY_REFRESH_SEC
        )
        if should_refresh_display:
            render_screen(lcd, state)
            last_display_at = now
            last_display_signature = signature

        should_write_status = (
            signature != last_status_signature and now - last_status_write_at >= STATUS_WRITE_SEC
        ) or ("KEY3" in pressed_events)
        if should_write_status:
            atomic_write_json(STATUS_PATH, state)
            last_status_write_at = now
            last_status_signature = signature

        time.sleep(BUTTON_POLL_SEC if not BUTTON_EVENT_MODE else min(BUTTON_POLL_SEC, 0.2))

if __name__ == "__main__":
    main()

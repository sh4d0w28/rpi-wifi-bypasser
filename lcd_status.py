#!/usr/bin/env python3
import json
import os
import re
import time
import socket
import subprocess
from collections import deque
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

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

WLAN_AP = os.environ.get("WLAN0_IFACE", "wlan0")
WLAN_UP = os.environ.get("WLAN1_IFACE", "wlan1")
HOSTAPD_CONF = Path(os.environ.get("HOSTAPD_CONF", "/etc/hostapd/hostapd.conf"))
REFRESH_SEC = float(os.environ.get("REFRESH_SEC", "1.0"))
PROBE_INTERVAL_SEC = float(os.environ.get("PROBE_INTERVAL_SEC", "15.0"))
STATUS_PATH = Path(os.environ.get("STATUS_PATH", "/run/rpi_ap_tools_status.json"))
CAPTIVE_PORTAL_ACK_CMD = os.environ.get("CAPTIVE_PORTAL_ACK_CMD", "").strip()
YOUTUBE_PING_HOST = os.environ.get("YOUTUBE_PING_HOST", "www.youtube.com")
YOUTUBE_RTMP_HOST = os.environ.get("YOUTUBE_RTMP_HOST", "a.rtmp.youtube.com")
YOUTUBE_RTMP_PORT = int(os.environ.get("YOUTUBE_RTMP_PORT", "1935"))
PAGE_ROTATE_SEC = float(os.environ.get("PAGE_ROTATE_SEC", "6.0"))
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
    return {name: button_pressed(name, pin) for name, pin in BUTTON_PINS.items()}

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

def render_inputs_probe(draw, state):
    probe = state["probe"]
    button_states = state["buttons"]["states"]
    button_counts = state["buttons"]["counts"]
    last_event = state["buttons"]["last_event"] or "-"

    draw.text((3, 3), "BTN", font=FONT, fill=(140, 170, 210))
    draw.text((26, 3), fit_text(last_event, 14), font=FONT, fill=(240, 244, 255))
    draw.text((3, 18), f"U{int(button_states['UP'])}/{button_counts['UP']:02d}", font=FONT, fill=(120, 255, 160) if button_states["UP"] else (180, 180, 180))
    draw.text((45, 18), f"D{int(button_states['DOWN'])}/{button_counts['DOWN']:02d}", font=FONT, fill=(120, 255, 160) if button_states["DOWN"] else (180, 180, 180))
    draw.text((87, 18), f"P{int(button_states['PRESS'])}/{button_counts['PRESS']:02d}", font=FONT, fill=(120, 255, 160) if button_states["PRESS"] else (180, 180, 180))
    draw.text((3, 31), f"L{int(button_states['LEFT'])}/{button_counts['LEFT']:02d}", font=FONT, fill=(120, 255, 160) if button_states["LEFT"] else (180, 180, 180))
    draw.text((45, 31), f"R{int(button_states['RIGHT'])}/{button_counts['RIGHT']:02d}", font=FONT, fill=(120, 255, 160) if button_states["RIGHT"] else (180, 180, 180))
    draw.text((87, 31), f"K3/{button_counts['KEY3']:02d}", font=FONT, fill=(120, 255, 160) if button_states["KEY3"] else (180, 180, 180))
    draw.text((3, 44), f"K1/{button_counts['KEY1']:02d}", font=FONT, fill=(120, 255, 160) if button_states["KEY1"] else (180, 180, 180))
    draw.text((45, 44), f"K2/{button_counts['KEY2']:02d}", font=FONT, fill=(120, 255, 160) if button_states["KEY2"] else (180, 180, 180))

    draw.line((2, 58, 125, 58), fill=(24, 44, 68), width=1)
    yt_text = "-" if probe["youtube_ping_ms"] is None else f"{probe['youtube_ping_ms']:.0f}ms"
    rtmp_text = "-" if probe["youtube_rtmp_ms"] is None else f"{probe['youtube_rtmp_ms']:.0f}ms"
    draw_label_value(draw, 3, 64, "YT", yt_text, (120, 220, 255), gap=18)
    draw_label_value(draw, 64, 64, "RT", rtmp_text, (255, 210, 90), gap=18)
    draw_label_value(draw, 3, 77, "NET", fit_text(probe["connectivity"], 12), (240, 244, 255), gap=24)

    portal_fill = (255, 210, 90) if probe["portal_suspected"] else (120, 255, 160) if probe["internet_ok"] else (255, 96, 96)
    portal_text = "PORTAL" if probe["portal_suspected"] else "ONLINE" if probe["internet_ok"] else "OFFLINE"
    draw.text((3, 92), portal_text, font=FONT, fill=portal_fill)
    ack_hint = "K3=ACK" if state["portal_ack_configured"] else "no-ack"
    draw.text((58, 92), ack_hint, font=FONT, fill=(180, 180, 180))

    if state["portal_ack_last"]:
        msg = fit_text(state["portal_ack_last"]["message"], 20)
        fill = (120, 255, 160) if state["portal_ack_last"]["ok"] else (255, 96, 96)
        draw.text((3, 107), msg, font=FONT, fill=fill)

def render_screen(lcd, state):
    image = Image.new("RGB", (128, 128), "BLACK")
    draw = ImageDraw.Draw(image)

    draw.rectangle((0, 0, 127, 127), fill=(4, 10, 20))
    draw.rectangle((0, 0, 127, 15), fill=(10, 26, 44))
    draw.line((0, 16, 127, 16), fill=(28, 60, 92), width=1)
    draw.text((108, 3), f"P{state['page'] + 1}", font=FONT, fill=(180, 180, 180))
    if state["page"] == 0:
        render_overview(draw, state)
    else:
        render_inputs_probe(draw, state)

    lcd.LCD_ShowImage(image.rotate(90), 0, 0)

def button_pressed(name, pin):
    try:
        if hasattr(config, "digital_read"):
            return config.digital_read(pin) == 0
        if WAVESHARE_DEV is None:
            return False
        attr_name = "GPIO_KEY_PRESS_PIN" if name == "PRESS" else f"GPIO_KEY_{name}_PIN"
        pin_attr = getattr(WAVESHARE_DEV, attr_name, None)
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
    lcd = LCD_1in44.LCD()
    attach_waveshare_device(lcd)
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    try:
        lcd.LCD_Clear()
    except Exception:
        pass
    prev = {WLAN_AP: read_bytes(WLAN_AP), WLAN_UP: read_bytes(WLAN_UP)}
    prev_t = time.time()
    button_counts = {name: 0 for name in BUTTON_PINS}
    button_states_prev = {name: False for name in BUTTON_PINS}
    last_event = ""
    last_event_ts = 0.0
    probe_cache = {
        "last_run": 0.0,
        "youtube_ping_ms": None,
        "youtube_rtmp_ms": None,
        "connectivity": "unknown",
        "portal_suspected": False,
        "internet_ok": False,
    }
    portal_ack_last = None
    page = 0
    manual_page_until = 0.0
    while True:
        now = time.time()
        curr = {WLAN_AP: read_bytes(WLAN_AP), WLAN_UP: read_bytes(WLAN_UP)}
        dt = max(0.2, now - prev_t)
        ap_name = read_ap_name()
        w0 = ip_only(read_ipv4(WLAN_AP))
        w1 = ip_only(read_ipv4(WLAN_UP))
        active_wifi = read_active_wifi()
        cpu_temp = read_cpu_temp_c()
        cpu_pct = read_cpu_percent()
        mem_pct = read_mem_percent()
        rx1ps = max(0, (curr[WLAN_UP]["rx"] - prev[WLAN_UP]["rx"]) / dt)
        tx1ps = max(0, (curr[WLAN_UP]["tx"] - prev[WLAN_UP]["tx"]) / dt)
        ap_ok = ap_name != "unknown" and w0 != "-"
        cl_ok = active_wifi["name"] != "-" and w1 != "-"
        signal = active_wifi["signal"] if cl_ok else "-"

        button_states = read_button_states()
        pressed_events = []
        for name, is_pressed in button_states.items():
            if is_pressed and not button_states_prev[name]:
                pressed_events.append(name)
                button_counts[name] += 1
                last_event = name
                last_event_ts = now
                if name in ("LEFT", "KEY1"):
                    page = (page - 1) % 2
                    manual_page_until = now + PAGE_ROTATE_SEC * 2
                elif name in ("RIGHT", "KEY2"):
                    page = (page + 1) % 2
                    manual_page_until = now + PAGE_ROTATE_SEC * 2
            button_states_prev[name] = is_pressed

        if now - probe_cache["last_run"] >= PROBE_INTERVAL_SEC:
            connectivity = read_nm_connectivity()
            probe_cache = {
                "last_run": now,
                "youtube_ping_ms": ping_latency_ms(YOUTUBE_PING_HOST),
                "youtube_rtmp_ms": tcp_latency_ms(YOUTUBE_RTMP_HOST, YOUTUBE_RTMP_PORT),
                "connectivity": connectivity,
                "portal_suspected": connectivity == "portal",
                "internet_ok": connectivity == "full",
            }

        if "KEY3" in pressed_events and probe_cache["portal_suspected"]:
            portal_ack_last = perform_portal_ack()

        if manual_page_until <= now:
            page = int(now / PAGE_ROTATE_SEC) % 2

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
            "buttons": {
                "states": button_states,
                "counts": button_counts,
                "last_event": last_event if now - last_event_ts <= 30 else "",
                "last_event_age_sec": max(0.0, now - last_event_ts) if last_event_ts else None,
            },
            "portal_ack_configured": bool(CAPTIVE_PORTAL_ACK_CMD),
            "portal_ack_last": portal_ack_last,
            "updated_at": now,
        }

        render_screen(lcd, state)
        atomic_write_json(STATUS_PATH, state)
        prev = curr
        prev_t = now
        time.sleep(REFRESH_SEC)

if __name__ == "__main__":
    main()

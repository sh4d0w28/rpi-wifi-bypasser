#!/usr/bin/env python3
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

WLAN_AP = os.environ.get("WLAN0_IFACE", "wlan0")
WLAN_UP = os.environ.get("WLAN1_IFACE", "wlan1")
HOSTAPD_CONF = Path(os.environ.get("HOSTAPD_CONF", "/etc/hostapd/hostapd.conf"))
REFRESH_SEC = float(os.environ.get("REFRESH_SEC", "1.0"))
PIN_UP = 6
PIN_DOWN = 19
PIN_PRESS = 13
PIN_KEY1 = 21
PIN_KEY2 = 20
FONT = ImageFont.load_default()
CPU_SAMPLES = deque(maxlen=2)

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

def render_screen(lcd, prev_stats, curr_stats, dt):
    image = Image.new("RGB", (128, 128), "BLACK")
    draw = ImageDraw.Draw(image)
    ap = read_ap_name()
    w0 = ip_only(read_ipv4(WLAN_AP))
    w1 = ip_only(read_ipv4(WLAN_UP))
    active_wifi = read_active_wifi()
    cpu_temp = read_cpu_temp_c()
    cpu_pct = read_cpu_percent()
    mem_pct = read_mem_percent()
    rx1ps = max(0, (curr_stats[WLAN_UP]["rx"] - prev_stats[WLAN_UP]["rx"]) / dt)
    tx1ps = max(0, (curr_stats[WLAN_UP]["tx"] - prev_stats[WLAN_UP]["tx"]) / dt)

    ap_ok = ap != "unknown" and w0 != "-"
    cl_ok = active_wifi["name"] != "-" and w1 != "-"
    signal = active_wifi["signal"] if cl_ok else "-"

    draw.rectangle((0, 0, 127, 127), fill=(4, 10, 20))
    draw.rectangle((0, 0, 127, 15), fill=(10, 26, 44))
    draw.line((0, 16, 127, 16), fill=(28, 60, 92), width=1)

    draw.text((3, 3), "AP", font=FONT, fill=(140, 170, 210))
    draw.text((19, 3), "OK" if ap_ok else "NO", font=FONT, fill=((120, 255, 160) if ap_ok else (255, 96, 96)))
    draw.text((46, 3), "CL", font=FONT, fill=(140, 170, 210))
    draw.text((62, 3), "OK" if cl_ok else "NO", font=FONT, fill=((120, 255, 160) if cl_ok else (255, 96, 96)))
    draw.text((90, 3), f"{signal}%" if signal != "-" else "--", font=FONT, fill=signal_color(signal))

    draw.text((3, 22), fit_text(ap, 20), font=FONT, fill=(240, 244, 255))
    draw.text((3, 35), fit_text(active_wifi["name"], 20), font=FONT, fill=(120, 220, 255))

    draw.line((2, 49, 125, 49), fill=(24, 44, 68), width=1)

    draw_label_value(draw, 3, 54, "w0", fit_text(w0, 15), (255, 255, 255), gap=18)
    draw_label_value(draw, 3, 67, "w1", fit_text(w1, 15), (255, 255, 255), gap=18)

    draw.line((2, 81, 125, 81), fill=(24, 44, 68), width=1)

    draw_label_value(draw, 3, 86, "RX", f"{human_bytes(rx1ps)}/s", (120, 255, 160), gap=18)
    draw_label_value(draw, 3, 99, "TX", f"{human_bytes(tx1ps)}/s", (255, 210, 90), gap=18)

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

    lcd.LCD_ShowImage(image.rotate(90), 0, 0)

def button_pressed(pin):
    try:
        return config.digital_read(pin) == 0
    except Exception:
        return False

def init_buttons():
    try:
        config.module_init()
    except Exception:
        pass
    for pin in [PIN_UP, PIN_DOWN, PIN_PRESS, PIN_KEY1, PIN_KEY2]:
        try:
            config.GPIO.setup(pin, config.GPIO.IN, pull_up_down=config.GPIO.PUD_UP)
        except Exception:
            pass

def main():
    init_buttons()
    lcd = LCD_1in44.LCD()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    try:
        lcd.LCD_Clear()
    except Exception:
        pass
    prev = {WLAN_AP: read_bytes(WLAN_AP), WLAN_UP: read_bytes(WLAN_UP)}
    prev_t = time.time()
    while True:
        now = time.time()
        curr = {WLAN_AP: read_bytes(WLAN_AP), WLAN_UP: read_bytes(WLAN_UP)}
        dt = max(0.2, now - prev_t)
        render_screen(lcd, prev, curr, dt)
        prev = curr
        prev_t = now
        time.sleep(REFRESH_SEC)

if __name__ == "__main__":
    main()

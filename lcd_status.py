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
PAGES = ["main", "totals"]
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

def draw_text_block(draw, lines):
    y = 2
    for line in lines:
        draw.text((2, y), line, font=FONT, fill="WHITE")
        y += 11

def render_page(lcd, page, prev_stats, curr_stats, dt):
    image = Image.new("RGB", (128, 128), "BLACK")
    draw = ImageDraw.Draw(image)
    ap = read_ap_name()
    w0 = read_ipv4(WLAN_AP)
    w1 = read_ipv4(WLAN_UP)
    active_wifi = read_active_wifi()
    cpu_temp = read_cpu_temp_c()
    cpu_pct = read_cpu_percent()
    mem_pct = read_mem_percent()
    if page == "main":
        rx1ps = max(0, (curr_stats[WLAN_UP]["rx"] - prev_stats[WLAN_UP]["rx"]) / dt)
        tx1ps = max(0, (curr_stats[WLAN_UP]["tx"] - prev_stats[WLAN_UP]["tx"]) / dt)
        lines = [
            f"AP:{ap}"[:21],
            f"{WLAN_AP}:{w0}"[:21],
            f"{WLAN_UP}:{w1}"[:21],
            f"WiFi:{active_wifi['name']}"[:21],
            f"Sig:{active_wifi['signal']}%"[:21],
            f"1RX:{human_bytes(rx1ps)}/s"[:21],
            f"1TX:{human_bytes(tx1ps)}/s"[:21],
            f"T:{'-' if cpu_temp is None else f'{cpu_temp:.1f}C'} C:{'-' if cpu_pct is None else f'{cpu_pct:.0f}%'}"[:21],
            f"M:{'-' if mem_pct is None else f'{mem_pct:.0f}%'} {socket.gethostname()}"[:21],
        ]
    else:
        rx0ps = max(0, (curr_stats[WLAN_AP]["rx"] - prev_stats[WLAN_AP]["rx"]) / dt)
        tx0ps = max(0, (curr_stats[WLAN_AP]["tx"] - prev_stats[WLAN_AP]["tx"]) / dt)
        lines = [
            f"AP:{ap}"[:21],
            f"WiFi:{active_wifi['name']}"[:21],
            f"Sig:{active_wifi['signal']}%"[:21],
            f"0RX:{human_bytes(rx0ps)}/s"[:21],
            f"0TX:{human_bytes(tx0ps)}/s"[:21],
            f"{WLAN_AP} RX {human_bytes(curr_stats[WLAN_AP]['rx'])}"[:21],
            f"{WLAN_UP} RX {human_bytes(curr_stats[WLAN_UP]['rx'])}"[:21],
            f"C:{'-' if cpu_pct is None else f'{cpu_pct:.0f}%'} M:{'-' if mem_pct is None else f'{mem_pct:.0f}%'}"[:21],
        ]
    draw_text_block(draw, lines)
    lcd.LCD_ShowImage(image, 0, 0)

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
    page_idx = 0
    prev = {WLAN_AP: read_bytes(WLAN_AP), WLAN_UP: read_bytes(WLAN_UP)}
    prev_t = time.time()
    last_button = 0.0
    while True:
        now = time.time()
        curr = {WLAN_AP: read_bytes(WLAN_AP), WLAN_UP: read_bytes(WLAN_UP)}
        dt = max(0.2, now - prev_t)
        if now - last_button > 0.2:
            if button_pressed(PIN_UP) or button_pressed(PIN_KEY1):
                page_idx = (page_idx - 1) % len(PAGES)
                last_button = now
            elif button_pressed(PIN_DOWN) or button_pressed(PIN_KEY2):
                page_idx = (page_idx + 1) % len(PAGES)
                last_button = now
            elif button_pressed(PIN_PRESS):
                last_button = now
        render_page(lcd, PAGES[page_idx], prev, curr, dt)
        prev = curr
        prev_t = now
        time.sleep(REFRESH_SEC)

if __name__ == "__main__":
    main()

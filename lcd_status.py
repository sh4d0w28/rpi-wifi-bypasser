#!/usr/bin/env python3
import os
import re
import time
import socket
import subprocess
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
    if page == "main":
        rx0ps = max(0, (curr_stats[WLAN_AP]["rx"] - prev_stats[WLAN_AP]["rx"]) / dt)
        tx0ps = max(0, (curr_stats[WLAN_AP]["tx"] - prev_stats[WLAN_AP]["tx"]) / dt)
        rx1ps = max(0, (curr_stats[WLAN_UP]["rx"] - prev_stats[WLAN_UP]["rx"]) / dt)
        tx1ps = max(0, (curr_stats[WLAN_UP]["tx"] - prev_stats[WLAN_UP]["tx"]) / dt)
        lines = [
            f"AP:{ap}"[:21],
            f"{WLAN_AP}:{w0}"[:21],
            f"{WLAN_UP}:{w1}"[:21],
            f"0 RX {human_bytes(rx0ps)}/s"[:21],
            f"0 TX {human_bytes(tx0ps)}/s"[:21],
            f"1 RX {human_bytes(rx1ps)}/s"[:21],
            f"1 TX {human_bytes(tx1ps)}/s"[:21],
            f"Host:{socket.gethostname()}"[:21],
        ]
    else:
        lines = [
            f"AP:{ap}"[:21],
            f"{WLAN_AP} RX {human_bytes(curr_stats[WLAN_AP]['rx'])}"[:21],
            f"{WLAN_AP} TX {human_bytes(curr_stats[WLAN_AP]['tx'])}"[:21],
            f"{WLAN_UP} RX {human_bytes(curr_stats[WLAN_UP]['rx'])}"[:21],
            f"{WLAN_UP} TX {human_bytes(curr_stats[WLAN_UP]['tx'])}"[:21],
            "UP/DOWN page",
            "PRESS refresh",
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
            if button_pressed(PIN_UP):
                page_idx = (page_idx - 1) % len(PAGES)
                last_button = now
            elif button_pressed(PIN_DOWN):
                page_idx = (page_idx + 1) % len(PAGES)
                last_button = now
            elif button_pressed(PIN_PRESS) or button_pressed(PIN_KEY1) or button_pressed(PIN_KEY2):
                last_button = now
        render_page(lcd, PAGES[page_idx], prev, curr, dt)
        prev = curr
        prev_t = now
        time.sleep(REFRESH_SEC)

if __name__ == "__main__":
    main()
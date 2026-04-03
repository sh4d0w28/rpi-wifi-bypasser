import json
import os
import re
import subprocess

from PIL import Image, ImageDraw, ImageFont

from rpi_ap_tools.core.process import run_command
from rpi_ap_tools.lcd.render_helpers import fit_text, human_bytes, metric_color, signal_color

try:
    import qrcode
except Exception:
    qrcode = None


FONT = ImageFont.load_default()
YOUTUBE_PROXY_RTMP_PORT = int(os.environ.get("YOUTUBE_PROXY_RTMP_PORT", "1935") or "1935")
YOUTUBE_PROXY_RTMP_APP = os.environ.get("YOUTUBE_PROXY_RTMP_APP", "live").strip().strip("/")


def state_signature(state):
    try:
        snapshot = dict(state)
        snapshot.pop("updated_at", None)
        return json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    except Exception:
        return ""


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
    proc = run_command(["ss", "-ntp"], check=False)
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
    :root { color-scheme: dark; }
    html, body { margin: 0; width: 100%; height: 100%; background: transparent; overflow: hidden; font-family: "Arial", sans-serif; }
    .stage { position: relative; width: 1920px; height: 1080px; background: radial-gradient(circle at 18% 18%, rgba(125, 211, 252, 0.18), transparent 24%), radial-gradient(circle at 84% 12%, rgba(251, 191, 36, 0.12), transparent 18%), linear-gradient(180deg, rgba(2, 6, 23, 0.0) 0%, rgba(2, 6, 23, 0.10) 58%, rgba(2, 6, 23, 0.18) 100%); }
    .panel { position: absolute; left: 72px; right: 72px; bottom: 60px; min-height: 252px; padding: 34px 42px; border-radius: 34px; color: #eff6ff; background: linear-gradient(135deg, rgba(15, 23, 42, 0.76), rgba(8, 47, 73, 0.78)); border: 1px solid rgba(255, 255, 255, 0.18); box-shadow: 0 24px 80px rgba(0, 0, 0, 0.32); backdrop-filter: blur(10px); }
  </style>
</head>
<body>
  <div class="stage">
    <div class="panel"></div>
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


def render_busy_overlay(draw, state):
    busy = state.get("busy_action") or {}
    if not busy:
        return
    draw.rectangle((14, 30, 114, 95), fill=(6, 20, 6), outline=(90, 180, 90))
    draw.rectangle((18, 34, 110, 91), outline=(36, 96, 36))
    draw_hourglass(draw, 64, 53, fill=(200, 255, 200))
    draw.text((30, 72), "PLEASE WAIT", font=FONT, fill=(220, 255, 220))
    draw.text((22, 84), fit_text(busy.get("label", "Working"), 14), font=FONT, fill=(140, 220, 140))


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
        fill_width = int((113 - 14 - 2) * (progress_pct / 100.0))
        draw.rectangle((4, 22, 123, 104), outline=(255, 96, 96), width=2)
        draw.rectangle((8, 26, 119, 100), outline=(255, 96, 96), width=1)
        draw.text((14, 34), "STREAM IS", font=FONT, fill=(255, 230, 230))
        draw.text((16, 48), "CREATING", font=FONT, fill=(255, 230, 230))
        draw.text((14, 62), stage_message, font=FONT, fill=(255, 210, 210))
        draw.rectangle((14, 74, 113, 88), outline=(255, 170, 170), width=1)
        draw.rectangle((15, 75, 15 + fill_width, 87), fill=(255, 96, 96))
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
    rows = [
        ("Name", fit_text(youtube.get("title", "No stream"), 16), (240, 244, 255)),
        ("RTMP", fit_text(youtube.get("rtmp_summary", "-"), 14), (120, 220, 255)),
        ("Rot", youtube.get("rotation_short", "OFF"), (255, 210, 90)),
        ("FPS", youtube.get("fps_mode_short", "ORIG"), (120, 255, 160)),
        ("In", youtube.get("incoming_res") or "-", (240, 244, 255)),
        ("Out", youtube.get("outgoing_res") or "-", (120, 220, 255)),
        ("Ovl", "Active" if youtube.get("overlay_enabled") else "Off", (255, 210, 90) if youtube.get("overlay_enabled") else (180, 180, 180)),
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

import logging
import os
import subprocess
import time
from collections import deque
from pathlib import Path
from threading import Event, Lock, Thread

from rpi_ap_tools.core.files import atomic_write_json
from rpi_ap_tools.lcd.hardware import attach_waveshare_device, bind_button_callbacks, button_pressed, init_buttons, load_waveshare_modules
from rpi_ap_tools.lcd.render_helpers import fit_text, ip_only, translate_button_for_rotation
from rpi_ap_tools.lcd.screens import (
    ffprobe_video_dimensions,
    overlay_static_template,
    overlay_weather_template,
    relay_input_connected,
    relay_probe_url,
    render_screen,
    rotate_resolution_text,
    state_signature,
)
from rpi_ap_tools.lcd.state import (
    capture_portal_response,
    ping_latency_ms,
    read_active_wifi,
    read_ap_name,
    read_ap_password,
    read_bytes,
    read_cpu_percent,
    read_cpu_temp_c,
    read_ipv4,
    read_mem_percent,
    read_nm_connectivity,
    start_watchers,
    tcp_latency_ms,
)
from rpi_ap_tools.system.expressvpn import connect_auto, connect_region, disconnect as expressvpn_disconnect, get_status_summary, list_country_groups
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
from youtube_live_lib.storage import write_transparent_overlay_png


LCD_1in44, config = load_waveshare_modules()
BUTTON_STATE_CACHE = {name: False for name in ("UP", "DOWN", "LEFT", "RIGHT", "PRESS", "KEY1", "KEY2", "KEY3")}
BUTTON_EVENT_QUEUE = deque()
BUTTON_EVENT_LOCK = Lock()
BUTTON_EVENT_MODE = False
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")

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
VPN_STATE_REFRESH_SEC = float(os.environ.get("VPN_STATE_REFRESH_SEC", "10.0"))
STATUS_PATH = Path(os.environ.get("STATUS_PATH", "/run/rpi_ap_tools_status.json"))
UPDATE_SCRIPT_PATH = Path(os.environ.get("UPDATE_SCRIPT_PATH", "/home/pi/update_ap.sh"))
UPDATE_LOG_PATH = Path(os.environ.get("UPDATE_LOG_PATH", "/run/rpi_ap_tools_update.log"))
PORTAL_CAPTURE_URL = os.environ.get("PORTAL_CAPTURE_URL", "http://connectivitycheck.gstatic.com/generate_204").strip()
PORTAL_CAPTURE_HTML_PATH = Path(os.environ.get("PORTAL_CAPTURE_HTML_PATH", "/run/rpi_ap_tools_captive_portal.html"))
PORTAL_CAPTURE_TIMEOUT_SEC = float(os.environ.get("PORTAL_CAPTURE_TIMEOUT_SEC", "15.0"))
PORTAL_CAPTURE_MAX_BYTES = int(os.environ.get("PORTAL_CAPTURE_MAX_BYTES", str(1024 * 1024)))
YOUTUBE_PING_HOST = os.environ.get("YOUTUBE_PING_HOST", "www.youtube.com")
YOUTUBE_RTMP_HOST = os.environ.get("YOUTUBE_RTMP_HOST", "a.rtmp.youtube.com")
YOUTUBE_RTMP_PORT = int(os.environ.get("YOUTUBE_RTMP_PORT", "1935"))
YOUTUBE_PROXY_RTMP_APP = os.environ.get("YOUTUBE_PROXY_RTMP_APP", "live").strip().strip("/")

BUTTON_PINS = {
    "UP": 6,
    "DOWN": 19,
    "LEFT": 5,
    "RIGHT": 26,
    "PRESS": 13,
    "KEY1": 21,
    "KEY2": 20,
    "KEY3": 16,
}
CPU_SAMPLES = deque(maxlen=2)
STATE_REFRESH_EVENT = Event()


def request_state_refresh():
    STATE_REFRESH_EVENT.set()


def enqueue_button_event(name, is_pressed):
    with BUTTON_EVENT_LOCK:
        BUTTON_STATE_CACHE[name] = is_pressed
        BUTTON_EVENT_QUEUE.append((time.time(), name, is_pressed))


def read_button_states():
    states = {name: button_pressed(name, pin, config) for name, pin in BUTTON_PINS.items()}
    with BUTTON_EVENT_LOCK:
        BUTTON_STATE_CACHE.update(states)
    return states


def drain_button_events():
    with BUTTON_EVENT_LOCK:
        events = list(BUTTON_EVENT_QUEUE)
        BUTTON_EVENT_QUEUE.clear()
    return events


def main():
    init_buttons(config)
    start_watchers(WLAN_AP, WLAN_UP, request_state_refresh)
    lcd = LCD_1in44.LCD()
    attach_waveshare_device(lcd)
    global BUTTON_EVENT_MODE
    BUTTON_EVENT_MODE = bind_button_callbacks(BUTTON_PINS, BUTTON_STATE_CACHE, enqueue_button_event)
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    try:
        lcd.LCD_Clear()
    except Exception:
        pass

    prev = {WLAN_AP: read_bytes(WLAN_AP), WLAN_UP: read_bytes(WLAN_UP)}
    prev_t = time.time()
    button_states_prev = {name: False for name in BUTTON_PINS}
    ap_name = read_ap_name(HOSTAPD_CONF, AP_CONFIG_FILE, WLAN_AP)
    ap_password = read_ap_password(AP_CONFIG_FILE)
    w0 = ip_only(read_ipv4(WLAN_AP))
    w1 = ip_only(read_ipv4(WLAN_UP))
    active_wifi = read_active_wifi(WLAN_UP)
    cpu_temp = read_cpu_temp_c()
    cpu_pct = read_cpu_percent(CPU_SAMPLES)
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
    youtube_create_privacy_status = youtube_creation.get("privacy_status") or youtube_stream.get("privacy_status", "public")
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
    vpn_status = get_status_summary()
    vpn_country_groups = (list_country_groups().get("countries") or [])
    state_lock = Lock()
    request_state_refresh()

    def set_ui_message(message):
        nonlocal ui_message
        ui_message = fit_text(message or "", 20)

    def selector_menu_ids():
        return {"youtube_create_audio", "youtube_create_privacy", "youtube_create_rotation", "youtube_create_fps"}

    def vpn_region_menu_id(country_key):
        return f"expressvpn_region::{country_key}"

    def vpn_country_group(country_key):
        for item in vpn_country_groups:
            if item.get("key") == country_key:
                return item
        return None

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
        return value if value != "-" else live_input_res

    def stream_output_resolution_text():
        incoming = stream_input_resolution_text()
        if incoming == "-":
            return live_output_res
        return rotate_resolution_text(incoming, youtube_stream.get("rotation", "0"))

    def default_create_settings():
        return (
            youtube_creation.get("audio_mode") or youtube_stream.get("audio_mode", "normal"),
            youtube_creation.get("privacy_status") or youtube_stream.get("privacy_status", "public"),
            youtube_creation.get("rotation") or youtube_stream.get("rotation", "0"),
            youtube_creation.get("fps_mode") or youtube_stream.get("fps_mode", "original"),
        )

    def allow_restart_auth():
        if youtube_auth.get("device_pending"):
            return True
        if youtube_auth.get("authorized") and not probe_cache.get("auth_required"):
            return False
        return bool(youtube_status_message and youtube_status_message not in ("Use YT menu", "AUTH FIRST"))

    def apply_overlay_demo(template_name):
        overlay = load_overlay_state()
        previous_structural = {key: overlay.get(key) for key in ("x", "y", "width", "height", "opacity")}
        overlay["enabled"] = template_name != "off"
        overlay["opacity"] = 1.0
        if template_name == "weather":
            overlay.update({"x": 0, "y": 0, "width": 1920, "height": 1080, "refresh_sec": 600})
        else:
            overlay.update({"x": 36, "y": 36, "width": 420, "height": 240, "refresh_sec": 10})
        overlay["last_rendered_at"] = 0
        save_overlay_state(overlay)
        ensure_overlay_html_exists()
        html_path = Path(overlay.get("html_path") or "")
        html = overlay_static_template() if template_name == "static" else overlay_weather_template() if template_name == "weather" else DEFAULT_OVERLAY_HTML
        if template_name != "off" and html_path:
            html_path.parent.mkdir(parents=True, exist_ok=True)
            html_path.write_text(html, encoding="utf-8")
        elif template_name == "off":
            png_path = Path(overlay.get("png_path") or "")
            if png_path:
                write_transparent_overlay_png(png_path)
        new_structural = {key: overlay.get(key) for key in ("x", "y", "width", "height", "opacity")}
        try:
            if previous_structural != new_structural:
                refresh_proxy_overlay()
                set_ui_message(f"Overlay {template_name}")
            else:
                set_ui_message(f"Overlay {template_name} saved")
        except YouTubeLiveError:
            set_ui_message(f"Overlay {template_name} saved")

    def get_menu_definition(menu_id):
        if menu_id == "expressvpn":
            items = [
                {"label": "Status", "kind": "screen", "target": "expressvpn"},
                {"label": "Connect Auto", "kind": "action", "action": "expressvpn_connect_auto"},
                {"label": "Select Country", "kind": "menu", "target": "expressvpn_country"},
                {"label": "Disconnect", "kind": "action", "action": "expressvpn_disconnect"},
            ]
            return "ExpressVPN", items
        if menu_id == "expressvpn_country":
            items = []
            if vpn_country_groups:
                for country in vpn_country_groups:
                    items.append({"label": country.get("label", "-"), "kind": "menu", "target": vpn_region_menu_id(country.get("key", ""))})
            else:
                items.append({"label": "No Countries", "kind": "noop", "disabled": True})
            return "Country", items
        if menu_id.startswith("expressvpn_region::"):
            country_key = menu_id.split("::", 1)[1]
            country = vpn_country_group(country_key)
            if not country:
                return "Region", [{"label": "No Regions", "kind": "noop", "disabled": True}]
            items = [
                {
                    "label": region.get("label", region.get("id", "-")),
                    "kind": "action",
                    "action": "expressvpn_connect_region",
                    "arg": region.get("id", ""),
                    "checked": vpn_status.get("selected_region") == region.get("id", ""),
                }
                for region in country.get("regions") or []
            ]
            return country.get("label", "Region"), items
        if menu_id == "youtube":
            items = [{"label": "Dashboard", "kind": "screen", "target": "youtube"}]
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
            if youtube_stream.get("qr_payload") or youtube_stream.get("watch_url") or youtube_stream.get("title"):
                items.append({"label": "Stream QR", "kind": "screen", "target": "youtube_qr"})
            return "YouTube", items
        if menu_id == "youtube_create":
            audio_label = {"normal": "NORM", "voice": "VOICE", "mute": "MUTE"}.get(youtube_create_audio_mode, youtube_create_audio_mode.upper())
            privacy_label = {"public": "PUB", "private": "PRIV"}.get(youtube_create_privacy_status, youtube_create_privacy_status.upper())
            rotation_label = {"0": "OFF", "90": "+90", "-90": "-90"}.get(youtube_create_rotation, youtube_create_rotation)
            fps_label = {"original": "ORIG", "30": "30FPS", "20": "20FPS"}.get(youtube_create_fps_mode, youtube_create_fps_mode.upper())
            items = [
                {"label": "Use Defaults", "kind": "action", "action": "youtube_create_defaults"},
                {"label": f"Privacy {privacy_label}", "kind": "menu", "target": "youtube_create_privacy"},
                {"label": f"Rotation {rotation_label}", "kind": "menu", "target": "youtube_create_rotation"},
                {"label": f"FPS {fps_label}", "kind": "menu", "target": "youtube_create_fps"},
                {"label": f"Sound {audio_label}", "kind": "menu", "target": "youtube_create_audio"},
            ]
            items.append({"label": "Confirm Create", "kind": "action", "action": "youtube_create", "disabled": (youtube_creation or {}).get("status") == "creating"})
            return "Create", items
        if menu_id == "youtube_create_audio":
            return "Create Audio", [{"label": label, "kind": "action", "action": "youtube_create_audio", "arg": mode, "checked": youtube_create_audio_mode == mode} for mode, label in (("normal", "Audio Normal"), ("voice", "Audio Voice"), ("mute", "Audio Mute"))]
        if menu_id == "youtube_create_privacy":
            return "Create Privacy", [{"label": label, "kind": "action", "action": "youtube_create_privacy", "arg": mode, "checked": youtube_create_privacy_status == mode} for mode, label in (("public", "Public"), ("private", "Private"))]
        if menu_id == "youtube_create_rotation":
            return "Create Rotate", [{"label": label, "kind": "action", "action": "youtube_create_rotation", "arg": mode, "checked": youtube_create_rotation == mode} for mode, label in (("90", "Rotate 90"), ("0", "Rotate Off"), ("-90", "Rotate -90"))]
        if menu_id == "youtube_create_fps":
            return "Create FPS", [{"label": label, "kind": "action", "action": "youtube_create_fps", "arg": mode, "checked": youtube_create_fps_mode == mode} for mode, label in (("original", "Original"), ("30", "30 FPS"), ("20", "20 FPS"))]
        if menu_id == "youtube_overlay":
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
                current_mode = "static" if "STATIC PIC" in current_html else "weather" if "WEATHER" in current_html or "Bangkok Demo" in current_html else "default"
            return "Overlay", [
                {"label": "Overlay Off", "kind": "action", "action": "overlay_demo", "arg": "off", "checked": current_mode == "off"},
                {"label": "Static Pic", "kind": "action", "action": "overlay_demo", "arg": "static", "checked": current_mode == "static"},
                {"label": "Weather Card", "kind": "action", "action": "overlay_demo", "arg": "weather", "checked": current_mode == "weather"},
                {"label": "Default Card", "kind": "action", "action": "overlay_demo", "arg": "default", "checked": current_mode == "default"},
            ]
        if menu_id == "update_confirm":
            return "Update", [{"label": "Yes", "kind": "action", "action": "update_run"}, {"label": "No", "kind": "action", "action": "update_cancel"}]
        return "Main", [{"label": "YouTube", "kind": "menu", "target": "youtube"}, {"label": "ExpressVPN", "kind": "menu", "target": "expressvpn"}, {"label": "Update", "kind": "menu", "target": "update_confirm"}, {"label": "Settings", "kind": "screen", "target": "settings"}]

    def current_menu_entry():
        return menu_stack[-1]

    def current_menu_definition():
        title, items = get_menu_definition(current_menu_entry()["id"])
        if not items:
            items = [{"label": "Empty", "kind": "noop", "disabled": True}]
        current_menu_entry()["selected"] = max(0, min(current_menu_entry()["selected"], len(items) - 1))
        return title, items

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
                menu_base_title, menu_base_items, menu_base_selected = menu_definition_for(parent_entry["id"], parent_entry.get("selected", 0))
                modal_selector = {"title": menu_title, "items": menu_items, "selected": menu_selected, "value_text": selector_value_text(current_menu_entry()["id"])}
            return {
                "ui_mode": ui_mode,
                "screen_id": current_screen,
                "menu_id": current_menu_entry()["id"],
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
                "portal_ack_last": portal_ack_last,
                "youtube": {
                    "auth": youtube_auth,
                    "title": youtube_stream.get("title", ""),
                    "watch_url": youtube_stream.get("watch_url", ""),
                    "qr_payload": youtube_stream.get("qr_payload", ""),
                    "privacy_status": youtube_stream.get("privacy_status", ""),
                    "rotation_short": youtube_stream.get("rotation_short", "OFF"),
                    "fps_mode_short": youtube_stream.get("fps_mode_short", "ORIG"),
                    "incoming_res": stream_input_resolution_text(),
                    "outgoing_res": stream_output_resolution_text(),
                    "overlay_enabled": bool((youtube_stream.get("relay") or {}).get("overlay_enabled")),
                    "rtmp_summary": rtmp_summary_text(),
                    "creation": youtube_creation,
                    "status_message": youtube_status_message if youtube_auth.get("device_pending") else "AUTH FIRST" if (probe_cache.get("auth_required") or not youtube_auth.get("authorized")) and (youtube_creation or {}).get("status") != "creating" else youtube_status_message,
                    "auth_required": probe_cache.get("auth_required") or not youtube_auth.get("authorized"),
                },
                "settings_rtmp": rtmp_summary_text(),
                "settings_privacy": {"public": "PUB", "private": "PRIV"}.get(youtube_create_privacy_status, youtube_create_privacy_status.upper()),
                "settings_rotation": {"0": "OFF", "90": "+90", "-90": "-90"}.get(youtube_create_rotation, youtube_create_rotation),
                "settings_fps": {"original": "ORIG", "30": "30FPS", "20": "20FPS"}.get(youtube_create_fps_mode, youtube_create_fps_mode.upper()),
                "settings_audio": {"normal": "NORM", "voice": "VOICE", "mute": "MUTE"}.get(youtube_create_audio_mode, youtube_create_audio_mode.upper()),
                "vpn": vpn_status,
                "ap_password": ap_password,
                "updated_at": now,
            }

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

    def trigger_simple_setting(target_name, value, message):
        nonlocal youtube_create_audio_mode, youtube_create_privacy_status, youtube_create_rotation, youtube_create_fps_mode
        if target_name == "audio":
            youtube_create_audio_mode = value
        elif target_name == "privacy":
            youtube_create_privacy_status = value
        elif target_name == "rotation":
            youtube_create_rotation = value
        else:
            youtube_create_fps_mode = value
        set_ui_message(message)
        close_submenu()

    def refresh_vpn_groups():
        nonlocal vpn_country_groups
        result = list_country_groups()
        with state_lock:
            vpn_country_groups = result.get("countries") or []
        return result

    def refresh_vpn_state():
        nonlocal vpn_status
        status = get_status_summary()
        with state_lock:
            vpn_status = status
        return status

    def trigger_expressvpn_action(action, region_id=""):
        nonlocal current_screen, ui_mode
        if action == "expressvpn_connect_auto":
            result = connect_auto()
        elif action == "expressvpn_connect_region":
            result = connect_region(region_id)
        else:
            result = expressvpn_disconnect()
        refresh_vpn_state()
        refresh_vpn_groups()
        set_ui_message(result.get("message", "VPN action finished"))
        current_screen = "expressvpn"
        ui_mode = "screen"

    def trigger_youtube_action(action):
        nonlocal youtube_auth, youtube_creation, youtube_status_message, current_screen, ui_mode, youtube_create_privacy_status, youtube_create_rotation, youtube_create_fps_mode
        if action == "youtube_create_defaults":
            audio_mode, privacy_status, rotation, fps_mode = default_create_settings()
            youtube_create_privacy_status = privacy_status
            youtube_create_rotation = rotation
            youtube_create_fps_mode = fps_mode
            trigger_simple_setting("audio", audio_mode, "Defaults loaded")
            return
        if action == "youtube_auth_poll":
            try:
                poll_device_authorization()
                youtube_auth = get_auth_status()
                youtube_status_message = "YouTube auth OK"
                set_ui_message("Auth complete")
            except YouTubeLiveError as exc:
                youtube_auth = get_auth_status()
                youtube_status_message = fit_text(str(exc), 20)
                set_ui_message(youtube_status_message)
            current_screen = "youtube"
            ui_mode = "screen"
            return
        if action == "youtube_create":
            try:
                start_stream_creation(
                    ap_ip=w0,
                    title=ap_name,
                    audio_mode=youtube_create_audio_mode,
                    privacy_status=youtube_create_privacy_status,
                    rotation=youtube_create_rotation,
                    fps_mode=youtube_create_fps_mode,
                )
                youtube_creation = load_creation_state()
                youtube_status_message = "Stream creating"
                set_ui_message(f"Create {youtube_create_privacy_status.upper()}")
            except YouTubeLiveError as exc:
                youtube_status_message = fit_text(str(exc), 20)
                set_ui_message(youtube_status_message)
            current_screen = "youtube"
            ui_mode = "screen"
            return
        if action in ("youtube_auth_start", "youtube_auth_restart"):
            try:
                start_device_authorization()
                youtube_auth = get_auth_status()
                youtube_status_message = "Open URL, enter code"
                set_ui_message("Auth restarted" if action == "youtube_auth_restart" else "Device code ready")
            except YouTubeLiveError as exc:
                youtube_status_message = fit_text(str(exc), 20)
                set_ui_message(youtube_status_message)
            current_screen = "youtube"
            ui_mode = "screen"

    def open_selected_item():
        nonlocal current_screen, ui_mode
        if ui_mode != "menu":
            return
        _, items = current_menu_definition()
        item = items[current_menu_entry()["selected"]]
        if item.get("disabled"):
            set_ui_message(item.get("label", "Unavailable"))
            return
        kind = item.get("kind")
        if kind == "screen":
            current_screen = item.get("target")
            ui_mode = "screen"
            return
        if kind == "menu":
            open_menu_target(item.get("target", "root"))
            current_screen = None
            ui_mode = "menu"
            return
        action = item.get("action")
        if action in {"youtube_auth_start", "youtube_auth_poll", "youtube_auth_restart", "youtube_create", "youtube_create_defaults"}:
            trigger_youtube_action(action)
        elif action == "expressvpn_connect_auto":
            trigger_expressvpn_action(action)
        elif action == "expressvpn_connect_region":
            trigger_expressvpn_action(action, item.get("arg", ""))
        elif action == "expressvpn_disconnect":
            trigger_expressvpn_action(action)
        elif action == "youtube_create_audio":
            trigger_simple_setting("audio", item.get("arg", "normal"), f"Sound {item.get('arg', 'normal').upper()}")
        elif action == "youtube_create_privacy":
            trigger_simple_setting("privacy", item.get("arg", "public"), f"Privacy {item.get('arg', 'public').upper()}")
        elif action == "youtube_create_rotation":
            trigger_simple_setting("rotation", item.get("arg", "0"), f"Create rot {item.get('arg', '0')}")
        elif action == "youtube_create_fps":
            trigger_simple_setting("fps", item.get("arg", "original"), f"Create {item.get('arg', 'original').upper()}")
        elif action == "overlay_demo":
            apply_overlay_demo(item.get("arg", "off"))
        elif action == "update_cancel":
            close_submenu()
            set_ui_message("Update canceled")
        elif action == "update_run":
            if not UPDATE_SCRIPT_PATH.exists():
                set_ui_message("update_ap.sh missing")
            else:
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
                except Exception as exc:
                    logging.exception("Failed to start update script")
                    set_ui_message(fit_text(str(exc), 20))
                current_screen = None
                ui_mode = "menu"

    def handle_pressed_button(name):
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
                    trigger_youtube_action("youtube_auth_poll")
                elif not youtube_auth.get("authorized"):
                    trigger_youtube_action("youtube_auth_start")
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
            elif relay_input_connected():
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
        last_vpn_refresh_at = 0.0
        while True:
            now = time.time()
            did_work = False
            if now - prev_t >= REFRESH_SEC:
                curr = {WLAN_AP: read_bytes(WLAN_AP), WLAN_UP: read_bytes(WLAN_UP)}
                dt = max(0.2, now - prev_t)
                with state_lock:
                    cpu_temp = read_cpu_temp_c()
                    cpu_pct = read_cpu_percent(CPU_SAMPLES)
                    mem_pct = read_mem_percent()
                    rx1ps = max(0, (curr[WLAN_UP]["rx"] - prev[WLAN_UP]["rx"]) / dt)
                    tx1ps = max(0, (curr[WLAN_UP]["tx"] - prev[WLAN_UP]["tx"]) / dt)
                    prev = curr
                    prev_t = now
                did_work = True
            if STATE_REFRESH_EVENT.is_set() or (now - last_network_refresh_at >= NETWORK_FALLBACK_REFRESH_SEC):
                with state_lock:
                    ap_name = read_ap_name(HOSTAPD_CONF, AP_CONFIG_FILE, WLAN_AP)
                    ap_password = read_ap_password(AP_CONFIG_FILE)
                    w0 = ip_only(read_ipv4(WLAN_AP))
                    w1 = ip_only(read_ipv4(WLAN_UP))
                    active_wifi = read_active_wifi(WLAN_UP)
                    ap_ok = ap_name != "unknown" and w0 != "-"
                    cl_ok = active_wifi["name"] != "-" and w1 != "-"
                    signal = active_wifi["signal"] if cl_ok else "-"
                    last_network_refresh_at = now
                STATE_REFRESH_EVENT.clear()
                did_work = True
            if now - last_youtube_refresh_at >= YOUTUBE_STATE_REFRESH_SEC:
                refresh_youtube_state()
                last_youtube_refresh_at = now
                did_work = True
            if now - last_vpn_refresh_at >= VPN_STATE_REFRESH_SEC:
                refresh_vpn_state()
                if not vpn_country_groups or current_menu_entry()["id"].startswith("expressvpn"):
                    refresh_vpn_groups()
                last_vpn_refresh_at = now
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
                portal_capture = capture_portal_response(url=PORTAL_CAPTURE_URL, timeout_sec=PORTAL_CAPTURE_TIMEOUT_SEC, max_bytes=PORTAL_CAPTURE_MAX_BYTES, html_path_base=PORTAL_CAPTURE_HTML_PATH, wifi_name=wifi_name)
            with state_lock:
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
        if bool(pressed_events) or (signature != last_display_signature and now - last_display_at >= DISPLAY_REFRESH_SEC):
            render_screen(lcd, state)
            last_display_at = now
            last_display_signature = signature
        if (signature != last_status_signature and now - last_status_write_at >= STATUS_WRITE_SEC) or bool(pressed_events):
            atomic_write_json(STATUS_PATH, state)
            last_status_write_at = now
            last_status_signature = signature
        time.sleep(BUTTON_POLL_SEC)

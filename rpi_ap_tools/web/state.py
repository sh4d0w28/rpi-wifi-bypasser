from youtube_live import (
    YouTubeLiveError,
    get_auth_status,
    get_stream_monitor_status,
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

from rpi_ap_tools.web.services.overlay_render_service import (
    load_overlay_html,
    load_runtime_status,
    overlay_preview_response,
    render_overlay_png,
    save_overlay_html,
    start_overlay_renderer_thread,
    start_relay_watchdog_thread,
)
from rpi_ap_tools.web.services.portal_browser_service import load_portal_browser_status
from rpi_ap_tools.web.services.update_service import load_portal_preview, load_update_status, portal_ack_available, portal_capture_available, portal_preview_available, start_update_service
from rpi_ap_tools.web.services.weather_service import load_overlay_weather
from rpi_ap_tools.web.services.wifi_service import WLAN_IFACE, get_active_connection, get_ap_name, get_ip, get_saved_wifi, save_wifi_credentials, scan_wifi, connect_wifi


def _strip_rtmp_scheme(url):
    value = str(url or "").strip()
    return value.replace("rtmp://", "").replace("rtmps://", "") if value else "-"


def _rotation_param(rotation):
    value = str(rotation or "0").strip()
    return f"R:{value}"


def _swap_resolution_if_rotated(width, height, rotation):
    if not width or not height:
        return "-"
    if str(rotation) in ("90", "-90"):
        return f"{height}x{width}"
    return f"{width}x{height}"


def _age_text(seconds):
    if seconds is None:
        return "-"
    try:
        value = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        return "-"
    if value < 60:
        return f"{value}s ago"
    return f"{value // 60}m {value % 60}s ago"


def _stream_dashboard(ap_name, stream_state, monitor):
    relay = (stream_state or {}).get("relay") or {}
    privacy = ((stream_state or {}).get("privacy_status") or "public").upper()
    params = ", ".join(
        [
            privacy,
            _rotation_param((stream_state or {}).get("rotation", "0")),
            (stream_state or {}).get("fps_mode_short") or "ORIG",
        ]
    )
    in_res = "-"
    if relay.get("video_width") and relay.get("video_height"):
        in_res = f"{relay.get('video_width')}x{relay.get('video_height')}"
    out_res = _swap_resolution_if_rotated(relay.get("video_width"), relay.get("video_height"), (stream_state or {}).get("rotation", "0"))
    problems = []
    if monitor.get("message") and (not monitor.get("ok") or monitor.get("code") not in ("live", "ready", "stream_active")):
        problems.append(monitor.get("message"))
    for item in monitor.get("issues") or []:
        if item and item not in problems:
            problems.append(item)
    if relay.get("status") == "stopped":
        problems.append("Local relay is stopped.")
    elif relay.get("status") == "standby" and (stream_state or {}).get("target_url"):
        problems.append("Local relay is accepting RTMP but not forwarding to YouTube.")
    if relay.get("status") == "running" and not relay.get("youtube_transfer_active"):
        problems.append("Relay egress is running, but fresh YouTube transfer metrics are not available.")
    transfer_bits = []
    if relay.get("youtube_bitrate_text"):
        transfer_bits.append(relay.get("youtube_bitrate_text"))
    if relay.get("youtube_speed_text"):
        transfer_bits.append(relay.get("youtube_speed_text"))
    transfer_age = _age_text(relay.get("youtube_metrics_age_sec"))
    if transfer_age != "-":
        transfer_bits.append(transfer_age)
    transfer_status = "sending" if relay.get("youtube_transfer_active") else "waiting"
    return {
        "wifi_name": ap_name or "-",
        "rtmp_endpoint": _strip_rtmp_scheme((stream_state or {}).get("proxy_publish_url") or (stream_state or {}).get("target_url")),
        "params": params,
        "input_resolution": in_res,
        "output_resolution": out_res,
        "youtube_status": monitor.get("summary") or "UNKNOWN",
        "youtube_message": monitor.get("message") or "",
        "relay_status": relay.get("status") or "-",
        "transfer_status": transfer_status,
        "transfer_metrics": " / ".join(transfer_bits) if transfer_bits else "-",
        "stream_status": ((monitor.get("stream") or {}).get("stream_status") or "-").upper(),
        "health_status": ((monitor.get("stream") or {}).get("health_status") or "-").upper(),
        "problems": problems,
    }


def index_context():
    wifi_list = scan_wifi()
    runtime = load_runtime_status()
    overlay = load_overlay_state()
    youtube_auth = get_auth_status()
    youtube_creation = load_creation_state()
    youtube_creation_log = load_creation_log()
    youtube_stream = load_stream_state()
    try:
        youtube_monitor = get_stream_monitor_status()
    except YouTubeLiveError as exc:
        youtube_monitor = {
            "ok": False,
            "code": "dashboard_error",
            "summary": "DASHBOARD ERROR",
            "message": str(exc),
            "issues": [],
        }
    youtube_ready = bool(
        youtube_auth.get("client_configured")
        and youtube_auth.get("authorized")
        and (youtube_auth.get("validation") or {}).get("ok")
    )
    youtube_dashboard = _stream_dashboard(get_ap_name(), youtube_stream, youtube_monitor)
    return {
        "ap_name": youtube_dashboard["wifi_name"],
        "active": get_active_connection(),
        "wifi_list": wifi_list,
        "wlan1_ip": get_ip(WLAN_IFACE),
        "wlan0_ip": get_ip("wlan0"),
        "top_wifi": wifi_list[:6],
        "runtime": runtime,
        "portal_ack_available": portal_ack_available(),
        "portal_capture_available": portal_capture_available(),
        "portal_preview_available": portal_preview_available(),
        "portal_preview": load_portal_preview(),
        "portal_browser": load_portal_browser_status(),
        "update_status": load_update_status(),
        "youtube_auth": youtube_auth,
        "youtube_ready": youtube_ready,
        "youtube_creation": youtube_creation,
        "youtube_creation_log": youtube_creation_log,
        "youtube_monitor": youtube_monitor,
        "youtube_dashboard": youtube_dashboard,
        "youtube_stream": youtube_stream,
        "youtube_audio_modes": list_audio_modes(),
        "youtube_privacy_modes": [
            {"value": "public", "label": "Public"},
            {"value": "private", "label": "Private"},
        ],
        "youtube_fps_modes": list_fps_modes(),
        "youtube_rotation_modes": list_rotation_modes(),
        "overlay": overlay,
        "overlay_html": load_overlay_html(),
    }

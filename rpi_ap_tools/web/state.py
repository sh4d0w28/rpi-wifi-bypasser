from youtube_live import (
    YouTubeLiveError,
    get_auth_status,
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


def index_context():
    wifi_list = scan_wifi()
    runtime = load_runtime_status()
    overlay = load_overlay_state()
    youtube_auth = get_auth_status()
    youtube_creation = load_creation_state()
    youtube_creation_log = load_creation_log()
    youtube_stream = load_stream_state()
    youtube_ready = bool(
        youtube_auth.get("client_configured")
        and youtube_auth.get("authorized")
        and (youtube_auth.get("validation") or {}).get("ok")
    )
    return {
        "ap_name": get_ap_name(),
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

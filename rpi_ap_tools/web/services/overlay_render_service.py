import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Response, render_template_string, send_file

from rpi_ap_tools.core.files import load_json_file
from youtube_live import DEFAULT_OVERLAY_HTML, YouTubeLiveError, ensure_overlay_html_exists, ensure_proxy_relay_running, load_overlay_state, refresh_proxy_overlay, save_overlay_state
from youtube_live_lib.storage import write_transparent_overlay_png

from .weather_service import load_overlay_weather
from .wifi_service import WLAN_IFACE, get_active_connection, get_ap_name, get_ip

STATUS_PATH = Path(os.environ.get("STATUS_PATH", "/run/rpi_ap_tools_status.json"))
OVERLAY_RENDER_HTML_PATH = Path(os.environ.get("YOUTUBE_OVERLAY_RENDER_HTML_PATH", "/run/rpi_ap_tools_youtube_overlay_rendered.html"))
OVERLAY_RENDERER_BIN = os.environ.get("YOUTUBE_OVERLAY_BROWSER_BIN", "").strip()
RELAY_ENSURE_INTERVAL_SEC = max(1.0, float(os.environ.get("YOUTUBE_PROXY_ENSURE_INTERVAL_SEC", "1.0")))
OVERLAY_RENDER_LOCK = threading.Lock()
OVERLAY_RENDERER_THREAD = None
RELAY_WATCHDOG_THREAD = None


def load_runtime_status():
    data = load_json_file(STATUS_PATH, {})
    return data if isinstance(data, dict) else {}


def overlay_html_path():
    return Path(load_overlay_state().get("html_path") or "")


def overlay_png_path():
    return Path(load_overlay_state().get("png_path") or "")


def load_overlay_html():
    ensure_overlay_html_exists()
    path = overlay_html_path()
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_OVERLAY_HTML


def save_overlay_html(text):
    path = overlay_html_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def overlay_template_context():
    runtime = load_runtime_status()
    return {
        "ap_name": get_ap_name(),
        "active": get_active_connection(),
        "runtime": runtime,
        "wlan0_ip": get_ip("wlan0"),
        "wlan1_ip": get_ip(WLAN_IFACE),
        "now_local": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "weather": load_overlay_weather(),
    }


def _overlay_renderer_bin():
    if OVERLAY_RENDERER_BIN:
        return OVERLAY_RENDERER_BIN
    for candidate in ("chromium-browser", "chromium", "google-chrome", "google-chrome-stable"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return ""


def render_overlay_png(force=False):
    with OVERLAY_RENDER_LOCK:
        overlay = load_overlay_state()
        png_path = Path(overlay["png_path"])
        if not overlay.get("enabled"):
            write_transparent_overlay_png(png_path)
            overlay["last_render_error"] = ""
            overlay["last_rendered_at"] = time.time()
            save_overlay_state(overlay)
            return True, f"Overlay hidden with transparent frame at {png_path}"
        html_source = load_overlay_html()
        renderer_bin = _overlay_renderer_bin()
        if not renderer_bin:
            overlay["last_render_error"] = "No Chromium-compatible browser found"
            save_overlay_state(overlay)
            return False, overlay["last_render_error"]
        rendered_html = render_template_string(html_source, **overlay_template_context())
        OVERLAY_RENDER_HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
        OVERLAY_RENDER_HTML_PATH.write_text(rendered_html, encoding="utf-8")
        png_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [renderer_bin, "--headless", "--disable-gpu", "--disable-dev-shm-usage", "--hide-scrollbars", "--default-background-color=00000000", f"--window-size={overlay['width']},{overlay['height']}", f"--screenshot={png_path}", OVERLAY_RENDER_HTML_PATH.as_uri()]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            cmd.insert(1, "--no-sandbox")
        proc = subprocess.run(cmd, text=True, capture_output=True, check=False, timeout=30)
        if proc.returncode != 0:
            message = proc.stderr.strip() or proc.stdout.strip() or "Overlay render failed"
            overlay["last_render_error"] = message
            save_overlay_state(overlay)
            return False, message
        overlay["last_render_error"] = ""
        overlay["last_rendered_at"] = time.time()
        save_overlay_state(overlay)
        return True, f"Overlay rendered to {png_path}"


def _overlay_renderer_loop(app):
    with app.app_context():
        while True:
            overlay = load_overlay_state()
            refresh_sec = max(5, int(overlay.get("refresh_sec") or 10))
            last_rendered_at = float(overlay.get("last_rendered_at") or 0)
            if overlay.get("enabled") and time.time() - last_rendered_at >= refresh_sec:
                render_overlay_png(force=True)
            elif not overlay.get("enabled") and not Path(overlay["png_path"]).is_file():
                render_overlay_png(force=True)
            time.sleep(1.0)


def start_overlay_renderer_thread(app):
    global OVERLAY_RENDERER_THREAD
    if OVERLAY_RENDERER_THREAD and OVERLAY_RENDERER_THREAD.is_alive():
        return
    ensure_overlay_html_exists()
    OVERLAY_RENDERER_THREAD = threading.Thread(target=_overlay_renderer_loop, args=(app,), name="overlay-renderer", daemon=True)
    OVERLAY_RENDERER_THREAD.start()


def _relay_watchdog_loop():
    while True:
        try:
            ensure_proxy_relay_running()
        except YouTubeLiveError:
            pass
        except Exception:
            pass
        time.sleep(RELAY_ENSURE_INTERVAL_SEC)


def start_relay_watchdog_thread():
    global RELAY_WATCHDOG_THREAD
    if RELAY_WATCHDOG_THREAD and RELAY_WATCHDOG_THREAD.is_alive():
        return
    RELAY_WATCHDOG_THREAD = threading.Thread(target=_relay_watchdog_loop, name="relay-watchdog", daemon=True)
    RELAY_WATCHDOG_THREAD.start()


def overlay_preview_response():
    path = overlay_png_path()
    if not path.is_file():
        return Response("No overlay PNG rendered yet.\n", mimetype="text/plain", status=404)
    return send_file(path, mimetype="image/png", max_age=0)

#!/usr/bin/env python3
import logging
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from youtube_live_lib import (
    DEFAULT_OVERLAY_HTML,
    OVERLAY_FRAME_INTERVAL_SEC,
    OVERLAY_PNG_PATH,
    YouTubeLiveError,
    _coerce_float,
    _run_creation_job,
    _run_overlay_feed as _relay_overlay_feed,
    ensure_overlay_html_exists,
    ensure_proxy_relay_running,
    get_auth_status,
    get_stream_monitor_status,
    list_audio_modes,
    list_fps_modes,
    list_rotation_modes,
    load_creation_log,
    load_creation_state,
    load_overlay_state,
    load_stream_state,
    normalize_audio_mode,
    normalize_fps_mode,
    normalize_rotation_mode,
    qr_data_uri,
    refresh_proxy_overlay,
    save_overlay_state,
    set_proxy_audio_mode,
    set_proxy_fps_mode,
    set_proxy_rotation_mode,
    start_device_authorization,
    start_stream_creation,
    poll_device_authorization,
)

__all__ = [
    "DEFAULT_OVERLAY_HTML",
    "YouTubeLiveError",
    "ensure_overlay_html_exists",
    "ensure_proxy_relay_running",
    "get_auth_status",
    "get_stream_monitor_status",
    "list_audio_modes",
    "list_fps_modes",
    "list_rotation_modes",
    "load_creation_log",
    "load_creation_state",
    "load_overlay_state",
    "load_stream_state",
    "poll_device_authorization",
    "qr_data_uri",
    "refresh_proxy_overlay",
    "save_overlay_state",
    "set_proxy_audio_mode",
    "set_proxy_fps_mode",
    "set_proxy_rotation_mode",
    "start_device_authorization",
    "start_stream_creation",
]


def _parse_cli_args(argv):
    ap_ip = "-"
    title = ""
    audio_mode = "normal"
    rotation = "0"
    fps_mode = "original"
    privacy_status = None
    idx = 0
    while idx < len(argv):
        item = argv[idx]
        if item == "--ap-ip" and idx + 1 < len(argv):
            ap_ip = argv[idx + 1]
            idx += 2
            continue
        if item == "--title" and idx + 1 < len(argv):
            title = argv[idx + 1]
            idx += 2
            continue
        if item == "--audio-mode" and idx + 1 < len(argv):
            audio_mode = argv[idx + 1]
            idx += 2
            continue
        if item == "--rotation" and idx + 1 < len(argv):
            rotation = argv[idx + 1]
            idx += 2
            continue
        if item == "--fps-mode" and idx + 1 < len(argv):
            fps_mode = argv[idx + 1]
            idx += 2
            continue
        if item == "--privacy-status" and idx + 1 < len(argv):
            privacy_status = argv[idx + 1]
            idx += 2
            continue
        idx += 1
    return (
        ap_ip,
        title,
        normalize_audio_mode(audio_mode),
        normalize_rotation_mode(rotation),
        normalize_fps_mode(fps_mode),
        privacy_status,
    )


def _parse_overlay_feed_args(argv):
    png_path = str(OVERLAY_PNG_PATH)
    interval = OVERLAY_FRAME_INTERVAL_SEC
    idx = 0
    while idx < len(argv):
        item = argv[idx]
        if item == "--png" and idx + 1 < len(argv):
            png_path = argv[idx + 1]
            idx += 2
            continue
        if item == "--interval" and idx + 1 < len(argv):
            interval = _coerce_float(argv[idx + 1], OVERLAY_FRAME_INTERVAL_SEC, minimum=0.2, maximum=10.0)
            idx += 2
            continue
        idx += 1
    return png_path, interval


def _run_overlay_feed(png_path, interval):
    _relay_overlay_feed(png_path, interval)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "create":
        logging.basicConfig(
            level=os.environ.get("LOG_LEVEL", "INFO").upper(),
            format="%(asctime)s %(levelname)s %(message)s",
            force=True,
        )
        cli_ap_ip, cli_title, cli_audio_mode, cli_rotation, cli_fps_mode, cli_privacy_status = _parse_cli_args(sys.argv[2:])
        _run_creation_job(cli_ap_ip, cli_title, cli_rotation, cli_fps_mode, cli_audio_mode, cli_privacy_status)
    elif len(sys.argv) >= 2 and sys.argv[1] == "overlay-feed":
        cli_png_path, cli_interval = _parse_overlay_feed_args(sys.argv[2:])
        _run_overlay_feed(cli_png_path, cli_interval)

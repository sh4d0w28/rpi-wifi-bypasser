"""Shared mode and overlay helpers for YouTube live support."""

from .config import DEFAULT_OVERLAY_HTML, DEFAULT_PROXY_AUDIO_MODE, OVERLAY_FRAME_INTERVAL_SEC, OVERLAY_PNG_PATH
from .legacy import YouTubeLiveError, ensure_overlay_html_exists, qr_data_uri
from .modes import list_audio_modes, list_fps_modes, list_rotation_modes, normalize_audio_mode, normalize_fps_mode, normalize_rotation_mode

__all__ = [
    "DEFAULT_OVERLAY_HTML",
    "DEFAULT_PROXY_AUDIO_MODE",
    "OVERLAY_FRAME_INTERVAL_SEC",
    "OVERLAY_PNG_PATH",
    "YouTubeLiveError",
    "ensure_overlay_html_exists",
    "list_audio_modes",
    "list_fps_modes",
    "list_rotation_modes",
    "normalize_audio_mode",
    "normalize_fps_mode",
    "normalize_rotation_mode",
    "qr_data_uri",
]

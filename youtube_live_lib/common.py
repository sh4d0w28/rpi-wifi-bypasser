"""Shared mode and overlay helpers for YouTube live support."""

import base64
import io

from .config import DEFAULT_OVERLAY_HTML, DEFAULT_PROXY_AUDIO_MODE, OVERLAY_FRAME_INTERVAL_SEC, OVERLAY_PNG_PATH
from .errors import YouTubeLiveError
from .modes import list_audio_modes, list_fps_modes, list_rotation_modes, normalize_audio_mode, normalize_fps_mode, normalize_rotation_mode
from .storage import ensure_overlay_html_exists

try:
    import qrcode
except Exception:
    qrcode = None


def qr_data_uri(payload):
    if not payload or qrcode is None:
        return ""
    image = qrcode.make(payload)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"

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

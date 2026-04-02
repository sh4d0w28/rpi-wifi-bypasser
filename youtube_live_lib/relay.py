"""Relay and overlay state entrypoints."""

from .legacy import (
    _run_overlay_feed,
    _coerce_float,
    ensure_proxy_relay_running,
    load_overlay_state,
    load_stream_state,
    refresh_proxy_overlay,
    save_overlay_state,
    set_proxy_audio_mode,
    set_proxy_fps_mode,
    set_proxy_rotation_mode,
)

__all__ = [
    "_coerce_float",
    "_run_overlay_feed",
    "ensure_proxy_relay_running",
    "load_overlay_state",
    "load_stream_state",
    "refresh_proxy_overlay",
    "save_overlay_state",
    "set_proxy_audio_mode",
    "set_proxy_fps_mode",
    "set_proxy_rotation_mode",
]

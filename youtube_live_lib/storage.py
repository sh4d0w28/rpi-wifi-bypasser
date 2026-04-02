"""JSON-backed state storage and normalization helpers for YouTube live support."""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import (
    CLIENT_CONFIG_PATH,
    CREATION_LOG_PATH,
    DEFAULT_OVERLAY_HTML,
    DEFAULT_PROXY_AUDIO_MODE,
    OVERLAY_HTML_PATH,
    OVERLAY_PNG_PATH,
    OVERLAY_STATE_PATH,
    RELAY_STATE_PATH,
    STREAM_CREATE_STATE_PATH,
    STREAM_STATE_PATH,
    TOKEN_PATH,
    DEVICE_STATE_PATH,
)
from .modes import decorate_stream_state, normalize_audio_mode, normalize_fps_mode, normalize_rotation_mode


def load_json(path, default):
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return data if isinstance(data, type(default)) else default


def save_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temp_path.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def drop_json(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def coerce_int(value, default, minimum=None, maximum=None):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def coerce_float(value, default, minimum=None, maximum=None):
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def default_overlay_state():
    return {
        "enabled": False,
        "x": 36,
        "y": 36,
        "width": 420,
        "height": 240,
        "opacity": 1.0,
        "refresh_sec": 10,
        "html_path": str(OVERLAY_HTML_PATH),
        "png_path": str(OVERLAY_PNG_PATH),
        "renderer": "chromium",
    }


def normalize_overlay_state(state):
    payload = default_overlay_state()
    if isinstance(state, dict):
        payload.update(state)
    payload["enabled"] = bool(payload.get("enabled"))
    payload["x"] = coerce_int(payload.get("x"), 36, minimum=0, maximum=3840)
    payload["y"] = coerce_int(payload.get("y"), 36, minimum=0, maximum=2160)
    payload["width"] = coerce_int(payload.get("width"), 420, minimum=32, maximum=3840)
    payload["height"] = coerce_int(payload.get("height"), 240, minimum=32, maximum=2160)
    payload["opacity"] = coerce_float(payload.get("opacity"), 1.0, minimum=0.0, maximum=1.0)
    payload["refresh_sec"] = coerce_int(payload.get("refresh_sec"), 10, minimum=5, maximum=3600)
    payload["html_path"] = str(payload.get("html_path") or OVERLAY_HTML_PATH)
    payload["png_path"] = str(payload.get("png_path") or OVERLAY_PNG_PATH)
    payload["renderer"] = str(payload.get("renderer") or "chromium")
    return payload


def load_overlay_state():
    state = normalize_overlay_state(load_json(OVERLAY_STATE_PATH, {}))
    png_path = Path(state["png_path"])
    html_path = Path(state["html_path"])
    state["png_exists"] = png_path.is_file()
    state["html_exists"] = html_path.is_file()
    if state["png_exists"]:
        try:
            state["png_mtime"] = png_path.stat().st_mtime
        except OSError:
            state["png_mtime"] = 0
    else:
        state["png_mtime"] = 0
    return state


def save_overlay_state(state):
    save_json(OVERLAY_STATE_PATH, normalize_overlay_state(state))


def ensure_overlay_html_exists():
    if OVERLAY_HTML_PATH.exists():
        return
    OVERLAY_HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
    OVERLAY_HTML_PATH.write_text(DEFAULT_OVERLAY_HTML, encoding="utf-8")


def normalize_client_config(data):
    if not isinstance(data, dict):
        return {}
    for key in ("web", "installed", "tv", "device"):
        if isinstance(data.get(key), dict):
            data = data[key]
            break
    client_id = str(data.get("client_id", "")).strip()
    client_secret = str(data.get("client_secret", "")).strip()
    return {"client_id": client_id, "client_secret": client_secret}


def load_client_config():
    config = normalize_client_config(load_json(CLIENT_CONFIG_PATH, {}))
    if config.get("client_id"):
        return config
    env_client_id = os.environ.get("YOUTUBE_CLIENT_ID", "").strip()
    env_client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET", "").strip()
    if env_client_id:
        return {"client_id": env_client_id, "client_secret": env_client_secret}
    return {}


def load_token():
    return load_json(TOKEN_PATH, {})


def save_token(token):
    save_json(TOKEN_PATH, token)


def load_device_state():
    return load_json(DEVICE_STATE_PATH, {})


def save_device_state(state):
    save_json(DEVICE_STATE_PATH, state)


def clear_device_state():
    drop_json(DEVICE_STATE_PATH)


def normalize_creation_state(state, *, pid_alive=None):
    if not isinstance(state, dict):
        return {}
    if state.get("status") != "creating":
        return state
    pid = state.get("pid")
    if pid and pid_alive and pid_alive(pid):
        return state
    if pid:
        state = dict(state)
        state.setdefault("finished_at", time.time())
        state["status"] = "error"
        state["stage"] = "error"
        state["message"] = state.get("message") or "Stream creation stopped"
    return state


def load_creation_state(*, pid_alive=None):
    return normalize_creation_state(load_json(STREAM_CREATE_STATE_PATH, {}), pid_alive=pid_alive)


def save_creation_state(state):
    save_json(STREAM_CREATE_STATE_PATH, state)


def clear_creation_state():
    drop_json(STREAM_CREATE_STATE_PATH)


def update_creation_state(*, fields, pid_alive=None):
    state = load_creation_state(pid_alive=pid_alive)
    state.update(fields)
    save_creation_state(state)


def reset_creation_log(*, ap_ip="-", title="", rotation="0", fps_mode="original", audio_mode="normal"):
    CREATION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    header = [
        f"[{datetime.now(timezone.utc).isoformat()}] stream creation requested",
        f"ap_ip={ap_ip or '-'}",
        f"title={title or '-'}",
        f"audio_mode={normalize_audio_mode(audio_mode)}",
        f"rotation={normalize_rotation_mode(rotation)}",
        f"fps_mode={normalize_fps_mode(fps_mode)}",
        "",
    ]
    CREATION_LOG_PATH.write_text("\n".join(header), encoding="utf-8")


def load_creation_log(max_bytes=262144):
    if not CREATION_LOG_PATH.exists():
        return {"path": str(CREATION_LOG_PATH), "text": "", "truncated": False}
    try:
        raw = CREATION_LOG_PATH.read_bytes()
    except OSError:
        return {"path": str(CREATION_LOG_PATH), "text": "", "truncated": False}

    truncated = False
    if max_bytes is not None and len(raw) > max_bytes:
        raw = raw[-max_bytes:]
        truncated = True
    text = raw.decode("utf-8", errors="replace")
    if truncated and "\n" in text:
        text = text.split("\n", 1)[1]
    if truncated:
        text = "[earlier log truncated]\n" + text
    return {"path": str(CREATION_LOG_PATH), "text": text, "truncated": truncated}


def normalize_relay_state(
    state,
    *,
    populate_relay_video_fields=None,
    relay_pid_matches=None,
):
    if not isinstance(state, dict):
        return {}
    state = decorate_stream_state(
        state,
        default_audio_mode=DEFAULT_PROXY_AUDIO_MODE,
        default_rotation="0",
        default_fps_mode="original",
    )
    if populate_relay_video_fields is not None:
        state = populate_relay_video_fields(state)
    pid = state.get("pid")
    if not pid:
        return state
    if relay_pid_matches is None:
        state["running"] = False
    else:
        state["running"] = relay_pid_matches(pid, state.get("listen_url", ""), state.get("target_url", ""))
    if not state["running"] and state.get("status") == "running":
        state["status"] = "stopped"
        state.setdefault("stopped_at", time.time())
    return state


def load_relay_state(*, populate_relay_video_fields=None, relay_pid_matches=None):
    return normalize_relay_state(
        load_json(RELAY_STATE_PATH, {}),
        populate_relay_video_fields=populate_relay_video_fields,
        relay_pid_matches=relay_pid_matches,
    )


def save_relay_state(state):
    save_json(
        RELAY_STATE_PATH,
        decorate_stream_state(
            state,
            default_audio_mode=DEFAULT_PROXY_AUDIO_MODE,
            default_rotation="0",
            default_fps_mode="original",
        ),
    )


def clear_relay_state():
    drop_json(RELAY_STATE_PATH)


def load_stream_state(*, load_relay_state_fn=None):
    state = decorate_stream_state(
        load_json(STREAM_STATE_PATH, {}),
        default_audio_mode="normal",
        default_rotation="0",
        default_fps_mode="original",
    )
    if state.get("mode") == "proxy":
        state["audio_mode"] = normalize_audio_mode(state.get("audio_mode") or DEFAULT_PROXY_AUDIO_MODE)
        state["rotation"] = normalize_rotation_mode(state.get("rotation"))
        state["fps_mode"] = normalize_fps_mode(state.get("fps_mode"))
        relay = load_relay_state_fn() if load_relay_state_fn else {}
        if relay:
            state["relay"] = relay
            state = decorate_stream_state(
                state,
                default_audio_mode=relay.get("audio_mode"),
                default_rotation=relay.get("rotation"),
                default_fps_mode=relay.get("fps_mode"),
            )
    return state


def save_stream_state(state):
    save_json(
        STREAM_STATE_PATH,
        decorate_stream_state(state, default_audio_mode="normal", default_rotation="0", default_fps_mode="original"),
    )

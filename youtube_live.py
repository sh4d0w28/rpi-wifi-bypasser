#!/usr/bin/env python3
import base64
import ctypes
import ctypes.util
import io
import json
import logging
import os
import signal
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import qrcode
except Exception:
    qrcode = None

YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube"
CLIENT_CONFIG_PATH = Path(os.environ.get("YOUTUBE_CLIENT_CONFIG_PATH", "/etc/rpi_ap_tools_youtube_client.json"))
TOKEN_PATH = Path(os.environ.get("YOUTUBE_TOKEN_PATH", "/var/lib/rpi_ap_tools/youtube_token.json"))
DEVICE_STATE_PATH = Path(os.environ.get("YOUTUBE_DEVICE_STATE_PATH", "/run/rpi_ap_tools_youtube_device.json"))
STREAM_STATE_PATH = Path(os.environ.get("YOUTUBE_STREAM_STATE_PATH", "/var/lib/rpi_ap_tools/youtube_stream.json"))
STREAM_CREATE_STATE_PATH = Path(os.environ.get("YOUTUBE_STREAM_CREATE_STATE_PATH", "/run/rpi_ap_tools_youtube_create.json"))
STREAM_CREATE_LOCK_PATH = Path(os.environ.get("YOUTUBE_STREAM_CREATE_LOCK_PATH", "/run/rpi_ap_tools_youtube_create.lock"))
CREATION_LOG_PATH = Path(os.environ.get("YOUTUBE_CREATE_LOG_PATH", "/run/rpi_ap_tools_youtube_create.log"))
RELAY_STATE_PATH = Path(os.environ.get("YOUTUBE_RELAY_STATE_PATH", "/var/lib/rpi_ap_tools/youtube_relay.json"))
RELAY_LOG_PATH = Path(os.environ.get("YOUTUBE_RELAY_LOG_PATH", "/run/rpi_ap_tools_youtube_relay.log"))
OVERLAY_STATE_PATH = Path(os.environ.get("YOUTUBE_OVERLAY_STATE_PATH", "/var/lib/rpi_ap_tools/youtube_overlay.json"))
OVERLAY_HTML_PATH = Path(os.environ.get("YOUTUBE_OVERLAY_HTML_PATH", "/var/lib/rpi_ap_tools/youtube_overlay.html"))
OVERLAY_PNG_PATH = Path(os.environ.get("YOUTUBE_OVERLAY_PNG_PATH", "/run/rpi_ap_tools_youtube_overlay.png"))
STREAM_TITLE_PREFIX = os.environ.get("YOUTUBE_STREAM_TITLE_PREFIX", "RPi Live").strip() or "RPi Live"
STREAM_PRIVACY_STATUS = os.environ.get("YOUTUBE_STREAM_PRIVACY_STATUS", "public").strip() or "public"
PROXY_ENABLED = os.environ.get("YOUTUBE_PROXY_ENABLED", "1").strip().lower() not in ("0", "false", "no")
PROXY_PUBLISH_URL_TEMPLATE = os.environ.get("YOUTUBE_PROXY_PUBLISH_URL", "").strip()
PROXY_RTMP_PORT = int(os.environ.get("YOUTUBE_PROXY_RTMP_PORT", "1935") or "1935")
PROXY_RTMP_APP = os.environ.get("YOUTUBE_PROXY_RTMP_APP", "live").strip().strip("/")
PROXY_ZMQ_PORT = int(os.environ.get("YOUTUBE_PROXY_ZMQ_PORT", "5559") or "5559")
FFMPEG_BIN = os.environ.get("YOUTUBE_PROXY_FFMPEG_BIN", "ffmpeg").strip() or "ffmpeg"
DEFAULT_PROXY_AUDIO_MODE = "normal"
DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
LOGGER = logging.getLogger(__name__)
AUDIO_MODE_SPECS = {
    "normal": {
        "label": "Normal",
        "short_label": "NORM",
        "description": "Natural audio mix with live-switchable processing.",
    },
    "voice": {
        "label": "Voice Focus",
        "short_label": "VOICE",
        "description": "Speech-focused band-pass and compression. This is not true vocal isolation.",
    },
    "mute": {
        "label": "Mute",
        "short_label": "MUTE",
        "description": "Drop audio from the outgoing relay.",
    },
}
ROTATION_MODE_SPECS = {
    "0": {
        "label": "Off",
        "short_label": "OFF",
        "description": "Keep the incoming video orientation unchanged.",
        "transpose": None,
    },
    "90": {
        "label": "Rotate 90",
        "short_label": "R+90",
        "description": "Rotate video 90 degrees clockwise before forwarding. The relay uses the Pi hardware encoder when available.",
        "transpose": "1",
    },
    "-90": {
        "label": "Rotate -90",
        "short_label": "R-90",
        "description": "Rotate video 90 degrees counter-clockwise before forwarding. The relay uses the Pi hardware encoder when available.",
        "transpose": "2",
    },
}
FPS_MODE_SPECS = {
    "original": {
        "label": "Original",
        "short_label": "ORIG",
        "description": "Keep the incoming frame rate unchanged.",
        "fps": None,
    },
    "30": {
        "label": "30 FPS",
        "short_label": "30FPS",
        "description": "Cap outgoing video at 30 fps.",
        "fps": "30",
    },
    "20": {
        "label": "20 FPS",
        "short_label": "20FPS",
        "description": "Cap outgoing video at 20 fps to reduce relay CPU load.",
        "fps": "20",
    },
}
if DEFAULT_PROXY_AUDIO_MODE not in AUDIO_MODE_SPECS:
    DEFAULT_PROXY_AUDIO_MODE = "normal"

VIDEO_DIMENSION_RE = re.compile(r'(\d{2,5})x(\d{2,5})(?:\s|\[|,|$)')
FFMPEG_BITRATE_RE = re.compile(r'bitrate=\s*([0-9.]+)\s*kbits/s')
FFMPEG_SPEED_RE = re.compile(r'speed=\s*([0-9.]+)x')
FFMPEG_ENCODER_RE = re.compile(r'^\s*[A-Z\.]+\s+([^\s]+)\s+', re.MULTILINE)
PROXY_VIDEO_PRESET = os.environ.get("YOUTUBE_PROXY_VIDEO_PRESET", "veryfast").strip() or "veryfast"
PROXY_VIDEO_CRF = str(os.environ.get("YOUTUBE_PROXY_VIDEO_CRF", "18") or "18").strip()
PROXY_VIDEO_ENCODER = os.environ.get("YOUTUBE_PROXY_VIDEO_ENCODER", "auto").strip().lower() or "auto"
PROXY_HW_VIDEO_ENCODER = os.environ.get("YOUTUBE_PROXY_HW_VIDEO_ENCODER", "h264_v4l2m2m").strip() or "h264_v4l2m2m"
PROXY_HW_VIDEO_BITRATE = str(os.environ.get("YOUTUBE_PROXY_HW_VIDEO_BITRATE", "6000k") or "6000k").strip()
OVERLAY_FRAME_INTERVAL_SEC = max(0.2, float(os.environ.get("YOUTUBE_OVERLAY_FRAME_INTERVAL_SEC", "1.0") or "1.0"))
RELAY_START_TIMEOUT_SEC = max(1.0, float(os.environ.get("YOUTUBE_RELAY_START_TIMEOUT_SEC", "5.0") or "5.0"))
ZMQ_REQ = 3
ZMQ_LINGER = 17
ZMQ_RCVTIMEO = 27
ZMQ_SNDTIMEO = 28
_ENCODER_CACHE = None
DEFAULT_OVERLAY_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <style>
    html, body {
      margin: 0;
      width: 100%;
      height: 100%;
      background: transparent;
      overflow: hidden;
      font-family: "Segoe UI", Arial, sans-serif;
    }
    .overlay-root {
      width: 100%;
      height: 100%;
      padding: 24px;
      display: flex;
      align-items: flex-start;
      justify-content: flex-start;
      box-sizing: border-box;
    }
    .panel {
      min-width: 260px;
      max-width: 420px;
      padding: 18px 20px;
      border-radius: 20px;
      color: #f8fafc;
      background: rgba(15, 23, 42, 0.64);
      border: 1px solid rgba(148, 163, 184, 0.35);
      box-shadow: 0 18px 44px rgba(2, 6, 23, 0.35);
      backdrop-filter: blur(12px);
    }
    .eyebrow {
      font-size: 13px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: #93c5fd;
    }
    .title {
      margin: 8px 0 4px;
      font-size: 30px;
      font-weight: 700;
    }
    .meta {
      font-size: 15px;
      color: #cbd5e1;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 16px;
    }
    .cell {
      padding: 10px 12px;
      border-radius: 14px;
      background: rgba(15, 23, 42, 0.48);
      border: 1px solid rgba(148, 163, 184, 0.2);
    }
    .label {
      font-size: 12px;
      color: #94a3b8;
    }
    .value {
      margin-top: 4px;
      font-size: 18px;
      font-weight: 600;
    }
  </style>
</head>
<body>
  <div class="overlay-root">
    <div class="panel">
      <div class="eyebrow">RPi Live Overlay</div>
      <div class="title">{{ ap_name }}</div>
      <div class="meta">{{ now_local }}</div>
      <div class="grid">
        <div class="cell">
          <div class="label">Uplink</div>
          <div class="value">{{ active.name or "none" }}</div>
        </div>
        <div class="cell">
          <div class="label">State</div>
          <div class="value">{{ active.state }}</div>
        </div>
        <div class="cell">
          <div class="label">wlan0</div>
          <div class="value">{{ wlan0_ip }}</div>
        </div>
        <div class="cell">
          <div class="label">wlan1</div>
          <div class="value">{{ wlan1_ip }}</div>
        </div>
      </div>
    </div>
  </div>
</body>
</html>
"""


class YouTubeLiveError(RuntimeError):
    pass


def normalize_audio_mode(mode):
    value = (mode or "").strip().lower()
    return value if value in AUDIO_MODE_SPECS else DEFAULT_PROXY_AUDIO_MODE


def audio_mode_spec(mode):
    return AUDIO_MODE_SPECS[normalize_audio_mode(mode)]


def list_audio_modes():
    return [
        {"value": value, **spec}
        for value, spec in AUDIO_MODE_SPECS.items()
    ]


def normalize_rotation_mode(mode):
    value = str(mode or "").strip()
    return value if value in ROTATION_MODE_SPECS else "0"


def rotation_mode_spec(mode):
    return ROTATION_MODE_SPECS[normalize_rotation_mode(mode)]


def list_rotation_modes():
    return [
        {"value": value, **spec}
        for value, spec in ROTATION_MODE_SPECS.items()
    ]


def normalize_fps_mode(mode):
    value = str(mode or "").strip().lower()
    return value if value in FPS_MODE_SPECS else "original"


def fps_mode_spec(mode):
    return FPS_MODE_SPECS[normalize_fps_mode(mode)]


def list_fps_modes():
    return [
        {"value": value, **spec}
        for value, spec in FPS_MODE_SPECS.items()
    ]


def _decorate_audio_mode_fields(state, *, default_mode=None):
    if not isinstance(state, dict) or not state:
        return {}
    payload = dict(state)
    mode = normalize_audio_mode(payload.get("audio_mode") or default_mode or DEFAULT_PROXY_AUDIO_MODE)
    spec = audio_mode_spec(mode)
    payload["audio_mode"] = mode
    payload["audio_mode_label"] = spec["label"]
    payload["audio_mode_short"] = spec["short_label"]
    payload["audio_mode_description"] = spec["description"]
    return payload


def _decorate_rotation_fields(state, *, default_mode=None):
    if not isinstance(state, dict) or not state:
        return {}
    payload = dict(state)
    mode = normalize_rotation_mode(payload.get("rotation") or default_mode or "0")
    spec = rotation_mode_spec(mode)
    payload["rotation"] = mode
    payload["rotation_label"] = spec["label"]
    payload["rotation_short"] = spec["short_label"]
    payload["rotation_description"] = spec["description"]
    return payload


def _decorate_fps_fields(state, *, default_mode=None):
    if not isinstance(state, dict) or not state:
        return {}
    payload = dict(state)
    mode = normalize_fps_mode(payload.get("fps_mode") or default_mode or "original")
    spec = fps_mode_spec(mode)
    payload["fps_mode"] = mode
    payload["fps_mode_label"] = spec["label"]
    payload["fps_mode_short"] = spec["short_label"]
    payload["fps_mode_description"] = spec["description"]
    return payload


def _decorate_stream_state(state, *, default_audio_mode=None, default_rotation=None, default_fps_mode=None):
    payload = _decorate_audio_mode_fields(state, default_mode=default_audio_mode)
    payload = _decorate_rotation_fields(payload, default_mode=default_rotation)
    return _decorate_fps_fields(payload, default_mode=default_fps_mode)


def _load_json(path, default):
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return data if isinstance(data, type(default)) else default


def _save_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temp_path.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _drop_json(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _coerce_int(value, default, minimum=None, maximum=None):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def _coerce_float(value, default, minimum=None, maximum=None):
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
    payload["x"] = _coerce_int(payload.get("x"), 36, minimum=0, maximum=3840)
    payload["y"] = _coerce_int(payload.get("y"), 36, minimum=0, maximum=2160)
    payload["width"] = _coerce_int(payload.get("width"), 420, minimum=32, maximum=3840)
    payload["height"] = _coerce_int(payload.get("height"), 240, minimum=32, maximum=2160)
    payload["opacity"] = _coerce_float(payload.get("opacity"), 1.0, minimum=0.0, maximum=1.0)
    payload["refresh_sec"] = _coerce_int(payload.get("refresh_sec"), 10, minimum=5, maximum=3600)
    payload["html_path"] = str(payload.get("html_path") or OVERLAY_HTML_PATH)
    payload["png_path"] = str(payload.get("png_path") or OVERLAY_PNG_PATH)
    payload["renderer"] = str(payload.get("renderer") or "chromium")
    return payload


def load_overlay_state():
    state = normalize_overlay_state(_load_json(OVERLAY_STATE_PATH, {}))
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
    _save_json(OVERLAY_STATE_PATH, normalize_overlay_state(state))


def ensure_overlay_html_exists():
    if OVERLAY_HTML_PATH.exists():
        return
    OVERLAY_HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
    OVERLAY_HTML_PATH.write_text(DEFAULT_OVERLAY_HTML, encoding="utf-8")


def _http_json(url, *, method="GET", headers=None, data=None):
    request = urllib.request.Request(url, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    if data is not None:
        encoded = json.dumps(data).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    else:
        encoded = None
    try:
        with urllib.request.urlopen(request, data=encoded, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": {"message": raw or str(exc)}}
        message = payload.get("error", {}).get("message") or str(exc)
        raise YouTubeLiveError(message) from exc
    except urllib.error.URLError as exc:
        raise YouTubeLiveError(str(exc.reason)) from exc


def _http_form(url, fields):
    encoded = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(url, data=encoded, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": raw or str(exc)}
        message = payload.get("error_description") or payload.get("error") or str(exc)
        raise YouTubeLiveError(message) from exc
    except urllib.error.URLError as exc:
        raise YouTubeLiveError(str(exc.reason)) from exc


def _normalize_client_config(data):
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
    config = _normalize_client_config(_load_json(CLIENT_CONFIG_PATH, {}))
    if config.get("client_id"):
        return config
    env_client_id = os.environ.get("YOUTUBE_CLIENT_ID", "").strip()
    env_client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET", "").strip()
    if env_client_id:
        return {"client_id": env_client_id, "client_secret": env_client_secret}
    return {}


def load_token():
    return _load_json(TOKEN_PATH, {})


def save_token(token):
    _save_json(TOKEN_PATH, token)


def load_device_state():
    return _load_json(DEVICE_STATE_PATH, {})


def save_device_state(state):
    _save_json(DEVICE_STATE_PATH, state)


def clear_device_state():
    _drop_json(DEVICE_STATE_PATH)


def load_stream_state():
    state = _decorate_stream_state(
        _load_json(STREAM_STATE_PATH, {}),
        default_audio_mode="normal",
        default_rotation="0",
        default_fps_mode="original",
    )
    if state.get("mode") == "proxy":
        state["audio_mode"] = normalize_audio_mode(state.get("audio_mode") or DEFAULT_PROXY_AUDIO_MODE)
        state["rotation"] = normalize_rotation_mode(state.get("rotation"))
        state["fps_mode"] = normalize_fps_mode(state.get("fps_mode"))
        relay = load_relay_state()
        if relay:
            state["relay"] = relay
            state = _decorate_stream_state(
                state,
                default_audio_mode=relay.get("audio_mode"),
                default_rotation=relay.get("rotation"),
                default_fps_mode=relay.get("fps_mode"),
            )
    return state


def save_stream_state(state):
    _save_json(
        STREAM_STATE_PATH,
        _decorate_stream_state(state, default_audio_mode="normal", default_rotation="0", default_fps_mode="original"),
    )


def load_relay_state():
    return normalize_relay_state(_load_json(RELAY_STATE_PATH, {}))


def save_relay_state(state):
    _save_json(
        RELAY_STATE_PATH,
        _decorate_stream_state(
            state,
            default_audio_mode=DEFAULT_PROXY_AUDIO_MODE,
            default_rotation="0",
            default_fps_mode="original",
        ),
    )


def clear_relay_state():
    _drop_json(RELAY_STATE_PATH)


def load_creation_state():
    return normalize_creation_state(_load_json(STREAM_CREATE_STATE_PATH, {}))


def save_creation_state(state):
    _save_json(STREAM_CREATE_STATE_PATH, state)


def clear_creation_state():
    _drop_json(STREAM_CREATE_STATE_PATH)


def update_creation_state(**fields):
    state = load_creation_state()
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


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _pid_cmdline(pid):
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
    except (OSError, TypeError, ValueError):
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore").strip()


def _relay_pid_matches(pid, listen_url="", target_url=""):
    if not _pid_alive(pid):
        return False
    cmdline = _pid_cmdline(pid)
    if not cmdline:
        return False
    ffmpeg_name = Path(FFMPEG_BIN).name
    if ffmpeg_name not in cmdline and "ffmpeg" not in cmdline:
        return False
    if listen_url and listen_url not in cmdline:
        return False
    if target_url and target_url not in cmdline:
        return False
    return True


def _listen_port_from_url(listen_url):
    try:
        parsed = urllib.parse.urlparse(listen_url or "")
    except ValueError:
        return 0
    try:
        return int(parsed.port or 1935)
    except (TypeError, ValueError):
        return 0


def _relay_port_listening(port, pid=None):
    if not port:
        return False
    try:
        proc = subprocess.run(
            ["ss", "-lntp"],
            text=True,
            capture_output=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    token = f":{int(port)}"
    pid_token = f"pid={int(pid)}" if pid else ""
    for line in proc.stdout.splitlines():
        if token not in line:
            continue
        if pid_token and pid_token not in line:
            continue
        return True
    return False


def _tail_log_text(path, max_bytes=4096):
    if not path:
        return ""
    log_path = Path(path)
    if not log_path.exists():
        return ""
    try:
        raw = log_path.read_bytes()
    except OSError:
        return ""
    if max_bytes and len(raw) > max_bytes:
        raw = raw[-max_bytes:]
    return raw.decode("utf-8", errors="ignore").strip()


def normalize_creation_state(state):
    if not isinstance(state, dict):
        return {}
    if state.get("status") != "creating":
        return state
    pid = state.get("pid")
    if pid and _pid_alive(pid):
        return state
    if pid:
        state = dict(state)
        state.setdefault("finished_at", time.time())
        state["status"] = "error"
        state["stage"] = "error"
        state["message"] = state.get("message") or "Stream creation stopped"
    return state


def normalize_relay_state(state):
    if not isinstance(state, dict):
        return {}
    state = _decorate_stream_state(
        state,
        default_audio_mode=DEFAULT_PROXY_AUDIO_MODE,
        default_rotation="0",
        default_fps_mode="original",
    )
    state = _populate_relay_video_fields(state)
    pid = state.get("pid")
    if not pid:
        return state
    state["running"] = _relay_pid_matches(pid, state.get("listen_url", ""), state.get("target_url", ""))
    if not state["running"] and state.get("status") == "running":
        state["status"] = "stopped"
        state.setdefault("stopped_at", time.time())
    return state


def _relay_orientation(width, height):
    try:
        width = int(width or 0)
        height = int(height or 0)
    except (TypeError, ValueError):
        return ""
    if width <= 0 or height <= 0:
        return ""
    if height > width:
        return "portrait"
    if width > height:
        return "landscape"
    return "square"


def _extract_video_dimensions_from_log(log_path):
    if not log_path:
        return {}
    try:
        raw = Path(log_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    matches = VIDEO_DIMENSION_RE.findall(raw)
    if not matches:
        return {}
    for width_text, height_text in reversed(matches):
        width = int(width_text)
        height = int(height_text)
        if width < 32 or height < 32:
            continue
        return {
            "video_width": width,
            "video_height": height,
            "video_orientation": _relay_orientation(width, height),
            "video_detected_at": time.time(),
        }
    return {}


def _extract_relay_runtime_metrics_from_log(log_path):
    if not log_path:
        return {}
    try:
        raw = Path(log_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}

    metrics = {}
    bitrate_matches = FFMPEG_BITRATE_RE.findall(raw)
    if bitrate_matches:
        try:
            bitrate_kbps = float(bitrate_matches[-1])
            metrics["video_bitrate_kbps"] = bitrate_kbps
            metrics["video_bitrate_text"] = f"{int(round(bitrate_kbps))} kbps"
        except ValueError:
            pass

    speed_matches = FFMPEG_SPEED_RE.findall(raw)
    if speed_matches:
        try:
            speed = float(speed_matches[-1])
            metrics["encoder_speed"] = speed
            metrics["encoder_speed_text"] = f"{speed:.2f}x"
        except ValueError:
            pass

    if metrics:
        metrics["metrics_detected_at"] = time.time()
    return metrics


def _ffmpeg_encoders():
    global _ENCODER_CACHE
    if _ENCODER_CACHE is not None:
        return _ENCODER_CACHE
    try:
        result = subprocess.run(
            [FFMPEG_BIN, "-hide_banner", "-encoders"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        _ENCODER_CACHE = set()
        return _ENCODER_CACHE
    _ENCODER_CACHE = set(FFMPEG_ENCODER_RE.findall(result.stdout or ""))
    return _ENCODER_CACHE


def _resolve_proxy_video_encoder():
    selected = PROXY_VIDEO_ENCODER
    encoders = _ffmpeg_encoders()
    hardware_name = PROXY_HW_VIDEO_ENCODER
    if selected == "auto":
        if hardware_name in encoders:
            selected = hardware_name
        else:
            selected = "libx264"
    if selected != "libx264" and selected not in encoders:
        LOGGER.warning(
            "Requested FFmpeg encoder '%s' is unavailable; falling back to libx264",
            selected,
        )
        selected = "libx264"
    if selected == hardware_name:
        return {
            "name": selected,
            "kind": "hardware",
            "label": f"Hardware ({selected})",
        }
    return {
        "name": "libx264",
        "kind": "software",
        "label": "Software (libx264)",
    }


def _populate_relay_video_fields(state):
    if not isinstance(state, dict) or not state:
        return state
    width = state.get("video_width")
    height = state.get("video_height")
    if width and height:
        payload = dict(state)
        payload["video_orientation"] = _relay_orientation(width, height)
        return payload
    detected = _extract_video_dimensions_from_log(state.get("log_path", ""))
    if not detected:
        payload = dict(state)
    else:
        payload = dict(state)
        payload.update(detected)
    payload.update(_extract_relay_runtime_metrics_from_log(state.get("log_path", "")))
    return payload


def _lock_creation():
    STREAM_CREATE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        return os.open(str(STREAM_CREATE_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise YouTubeLiveError("Stream creation already in progress") from exc


def _unlock_creation(fd):
    try:
        os.close(fd)
    except OSError:
        pass
    _drop_json(STREAM_CREATE_LOCK_PATH)


def client_ready():
    return bool(load_client_config().get("client_id"))


def authorization_ready():
    token = load_token()
    return bool(token.get("refresh_token") or token.get("access_token"))


def validate_live_access():
    if not authorization_ready():
        return {
            "ok": False,
            "code": "not_authorized",
            "message": "YouTube is not authorized yet",
        }

    try:
        payload = _api_request(
            "GET",
            "liveBroadcasts",
            params={
                "part": "id,status",
                "broadcastStatus": "all",
                "maxResults": 1,
            },
        )
        items = payload.get("items", [])
        return {
            "ok": True,
            "code": "ok",
            "message": "YouTube Live access verified",
            "checked_at": time.time(),
            "broadcast_count_hint": len(items),
        }
    except YouTubeLiveError as exc:
        message = str(exc).strip() or "YouTube Live validation failed"
        lowered = message.lower()
        code = "api_error"
        if "not authorized" in lowered or "unauthorized" in lowered:
            code = "not_authorized"
        elif "refresh token" in lowered or "invalid_grant" in lowered:
            code = "token_expired"
        elif "insufficient" in lowered or "scope" in lowered:
            code = "scope_error"
        elif "live streaming" in lowered or "live broadcast" in lowered:
            code = "live_not_enabled"
        return {
            "ok": False,
            "code": code,
            "message": message,
            "checked_at": time.time(),
        }


def get_auth_status():
    token = load_token()
    device = load_device_state()
    validation = {
        "ok": False,
        "code": "not_checked",
        "message": "Authorization has not been verified yet",
    }
    if authorization_ready():
        validation = validate_live_access()
    return {
        "client_configured": client_ready(),
        "authorized": authorization_ready(),
        "device_pending": bool(device.get("device_code")),
        "device": device,
        "token": {
            "has_refresh_token": bool(token.get("refresh_token")),
            "expires_at": token.get("expires_at"),
        },
        "validation": validation,
        "creation": load_creation_state(),
    }


def start_device_authorization():
    config = load_client_config()
    if not config.get("client_id"):
        raise YouTubeLiveError(f"Missing YouTube OAuth client config at {CLIENT_CONFIG_PATH}")
    payload = _http_form(
        DEVICE_CODE_URL,
        {
            "client_id": config["client_id"],
            "scope": YOUTUBE_SCOPE,
        },
    )
    state = {
        "device_code": payload.get("device_code", ""),
        "user_code": payload.get("user_code", ""),
        "verification_url": payload.get("verification_url", ""),
        "verification_url_complete": payload.get("verification_url_complete", ""),
        "expires_at": time.time() + int(payload.get("expires_in", 0)),
        "interval": int(payload.get("interval", 5)),
        "started_at": time.time(),
    }
    save_device_state(state)
    return state


def poll_device_authorization():
    config = load_client_config()
    state = load_device_state()
    if not config.get("client_id"):
        raise YouTubeLiveError(f"Missing YouTube OAuth client config at {CLIENT_CONFIG_PATH}")
    if not state.get("device_code"):
        raise YouTubeLiveError("No device authorization is pending")
    if state.get("expires_at", 0) <= time.time():
        clear_device_state()
        raise YouTubeLiveError("Device authorization code expired")

    fields = {
        "client_id": config["client_id"],
        "device_code": state["device_code"],
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }
    if config.get("client_secret"):
        fields["client_secret"] = config["client_secret"]
    try:
        payload = _http_form(TOKEN_URL, fields)
    except YouTubeLiveError as exc:
        message = str(exc)
        if any(code in message for code in ("authorization_pending", "slow_down")):
            raise
        clear_device_state()
        raise

    token = {
        "access_token": payload.get("access_token", ""),
        "refresh_token": payload.get("refresh_token", load_token().get("refresh_token", "")),
        "scope": payload.get("scope", YOUTUBE_SCOPE),
        "token_type": payload.get("token_type", "Bearer"),
        "expires_at": time.time() + int(payload.get("expires_in", 3600)) - 60,
    }
    save_token(token)
    clear_device_state()
    return token


def _refresh_access_token(token):
    config = load_client_config()
    if not config.get("client_id"):
        raise YouTubeLiveError(f"Missing YouTube OAuth client config at {CLIENT_CONFIG_PATH}")
    if not token.get("refresh_token"):
        raise YouTubeLiveError("No YouTube refresh token available")
    fields = {
        "client_id": config["client_id"],
        "refresh_token": token["refresh_token"],
        "grant_type": "refresh_token",
    }
    if config.get("client_secret"):
        fields["client_secret"] = config["client_secret"]
    payload = _http_form(TOKEN_URL, fields)
    token["access_token"] = payload.get("access_token", "")
    token["token_type"] = payload.get("token_type", "Bearer")
    token["expires_at"] = time.time() + int(payload.get("expires_in", 3600)) - 60
    save_token(token)
    return token


def ensure_access_token():
    token = load_token()
    if not token:
        raise YouTubeLiveError("YouTube is not authorized yet")
    if token.get("access_token") and token.get("expires_at", 0) > time.time():
        return token["access_token"]
    token = _refresh_access_token(token)
    if not token.get("access_token"):
        raise YouTubeLiveError("Failed to refresh YouTube access token")
    return token["access_token"]


def _api_request(method, path, *, params=None, body=None):
    access_token = ensure_access_token()
    query = urllib.parse.urlencode(params or {})
    url = f"{YOUTUBE_API_BASE}/{path}"
    if query:
        url = f"{url}?{query}"
    return _http_json(
        url,
        method=method,
        headers={"Authorization": f"Bearer {access_token}"},
        data=body,
    )


def _default_stream_title():
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"{STREAM_TITLE_PREFIX} {stamp}"


def _proxy_publish_url(ap_ip):
    if PROXY_PUBLISH_URL_TEMPLATE:
        return PROXY_PUBLISH_URL_TEMPLATE.format(ap_ip=ap_ip or "")
    host = ap_ip or "127.0.0.1"
    if PROXY_RTMP_APP:
        if PROXY_RTMP_PORT == 1935:
            return f"rtmp://{host}/{PROXY_RTMP_APP}"
        return f"rtmp://{host}:{PROXY_RTMP_PORT}/{PROXY_RTMP_APP}"
    if PROXY_RTMP_PORT == 1935:
        return f"rtmp://{host}"
    return f"rtmp://{host}:{PROXY_RTMP_PORT}"


def _proxy_listen_url():
    if PROXY_RTMP_APP:
        return f"rtmp://0.0.0.0:{PROXY_RTMP_PORT}/{PROXY_RTMP_APP}"
    return f"rtmp://0.0.0.0:{PROXY_RTMP_PORT}"


def _proxy_control_url():
    return f"tcp://127.0.0.1:{PROXY_ZMQ_PORT}"


def _ffmpeg_escape_filter_value(value):
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _relay_audio_filter():
    return ",".join(
        [
            "highpass@voice_hp=f=160:m=0",
            "lowpass@voice_lp=f=3800:m=0",
            "acompressor@voice_comp=threshold=0.08:ratio=3:attack=5:release=60:makeup=2:mix=0",
            "volume@audio_gain=1:eval=once",
            f"azmq@audio_ctrl=b='{_ffmpeg_escape_filter_value(_proxy_control_url())}'",
        ]
    )


def _proxy_relay_argv(*, listen_url, target_url, audio_mode, rotation, fps_mode, overlay=None, overlay_fd=None):
    rotation = normalize_rotation_mode(rotation)
    rotation_spec = rotation_mode_spec(rotation)
    fps_mode = normalize_fps_mode(fps_mode)
    fps_spec = fps_mode_spec(fps_mode)
    overlay = normalize_overlay_state(overlay or {})
    overlay_active = bool(overlay.get("enabled") and overlay_fd is not None and overlay.get("png_path"))
    argv = [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "info",
        "-stats",
        "-listen",
        "1",
        "-i",
        listen_url,
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "44100",
        "-ac",
        "1",
        "-af",
        _relay_audio_filter(),
    ]
    if overlay_active:
        argv.extend(
            [
                "-thread_queue_size",
                "8",
                "-f",
                "image2pipe",
                "-framerate",
                "1",
                "-c:v",
                "png",
                "-i",
                f"pipe:{overlay_fd}",
            ]
        )
    video_filters = []
    if rotation_spec.get("transpose"):
        video_filters.append(f"transpose={rotation_spec['transpose']}")
    if fps_spec.get("fps"):
        video_filters.append(f"fps={fps_spec['fps']}")
    if overlay_active:
        overlay_filters = ["format=rgba"]
        overlay_width = overlay.get("width") or -1
        overlay_height = overlay.get("height") or -1
        overlay_filters.append(f"scale=w={overlay_width}:h={overlay_height}")
        if overlay.get("opacity", 1.0) < 0.999:
            overlay_filters.append(f"colorchannelmixer=aa={overlay['opacity']:.3f}")
        base_chain = ",".join(video_filters) if video_filters else "null"
        argv.extend(
            [
                "-filter_complex",
                f"[0:v]{base_chain}[base];[1:v]{','.join(overlay_filters)}[ov];[base][ov]overlay=x={overlay['x']}:y={overlay['y']}[vout]",
                "-map",
                "[vout]",
                "-map",
                "0:a?",
            ]
        )
    else:
        argv.extend(["-map", "0:v?", "-map", "0:a?"])
    if video_filters or overlay_active:
        video_encoder = _resolve_proxy_video_encoder()
        argv.extend(
            [
                "-vf",
                ",".join(video_filters),
                "-c:v",
            ]
        )
        if video_encoder["name"] == "libx264":
            argv.extend(
                [
                    "libx264",
                    "-preset",
                    PROXY_VIDEO_PRESET,
                    "-tune",
                    "zerolatency",
                    "-crf",
                    PROXY_VIDEO_CRF,
                    "-pix_fmt",
                    "yuv420p",
                ]
            )
        else:
            argv.extend(
                [
                    video_encoder["name"],
                    "-b:v",
                    PROXY_HW_VIDEO_BITRATE,
                    "-maxrate",
                    PROXY_HW_VIDEO_BITRATE,
                    "-bufsize",
                    PROXY_HW_VIDEO_BITRATE,
                    "-pix_fmt",
                    "yuv420p",
                ]
            )
    else:
        argv.extend(["-c:v", "copy"])
    argv.extend(["-f", "flv", target_url])
    return argv


def _proxy_video_pipeline_state(rotation, fps_mode, overlay=None):
    rotation = normalize_rotation_mode(rotation)
    fps_mode = normalize_fps_mode(fps_mode)
    overlay = normalize_overlay_state(overlay or load_overlay_state())
    if rotation == "0" and fps_mode == "original" and not overlay.get("enabled"):
        return {
            "mode": "copy-video-live-audio",
            "video_encoder": "copy",
            "video_encoder_kind": "copy",
            "video_encoder_label": "Passthrough",
        }
    encoder = _resolve_proxy_video_encoder()
    return {
        "mode": "transcode-video-live-audio",
        "video_encoder": encoder["name"],
        "video_encoder_kind": encoder["kind"],
        "video_encoder_label": encoder["label"],
    }


def _load_libzmq():
    candidates = []
    discovered = ctypes.util.find_library("zmq")
    if discovered:
        candidates.append(discovered)
    candidates.extend(
        [
            "libzmq.so.5",
            "libzmq.so",
            "libzmq.dylib",
            "libzmq.dll",
            "zmq.dll",
        ]
    )
    lib = None
    for candidate in candidates:
        try:
            lib = ctypes.CDLL(candidate)
            break
        except OSError:
            continue
    if lib is None:
        raise YouTubeLiveError("libzmq is not installed; live audio switching is unavailable")
    lib.zmq_ctx_new.restype = ctypes.c_void_p
    lib.zmq_ctx_term.argtypes = [ctypes.c_void_p]
    lib.zmq_socket.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.zmq_socket.restype = ctypes.c_void_p
    lib.zmq_setsockopt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
    lib.zmq_connect.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.zmq_send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    lib.zmq_send.restype = ctypes.c_int
    lib.zmq_recv.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    lib.zmq_recv.restype = ctypes.c_int
    lib.zmq_close.argtypes = [ctypes.c_void_p]
    lib.zmq_errno.restype = ctypes.c_int
    lib.zmq_strerror.argtypes = [ctypes.c_int]
    lib.zmq_strerror.restype = ctypes.c_char_p
    return lib


def _zmq_error(lib):
    code = lib.zmq_errno()
    detail = lib.zmq_strerror(code)
    return detail.decode("utf-8", errors="ignore") if detail else f"libzmq error {code}"


def _zmq_send_command(endpoint, message):
    lib = _load_libzmq()
    ctx = lib.zmq_ctx_new()
    if not ctx:
        raise YouTubeLiveError("Failed to create ZMQ context")
    sock = None
    try:
        sock = lib.zmq_socket(ctx, ZMQ_REQ)
        if not sock:
            raise YouTubeLiveError("Failed to create ZMQ socket")
        linger = ctypes.c_int(0)
        timeout_ms = ctypes.c_int(1500)
        if lib.zmq_setsockopt(sock, ZMQ_LINGER, ctypes.byref(linger), ctypes.sizeof(linger)) != 0:
            raise YouTubeLiveError(_zmq_error(lib))
        if lib.zmq_setsockopt(sock, ZMQ_SNDTIMEO, ctypes.byref(timeout_ms), ctypes.sizeof(timeout_ms)) != 0:
            raise YouTubeLiveError(_zmq_error(lib))
        if lib.zmq_setsockopt(sock, ZMQ_RCVTIMEO, ctypes.byref(timeout_ms), ctypes.sizeof(timeout_ms)) != 0:
            raise YouTubeLiveError(_zmq_error(lib))
        if lib.zmq_connect(sock, endpoint.encode("utf-8")) != 0:
            raise YouTubeLiveError(_zmq_error(lib))
        payload = message.encode("utf-8")
        if lib.zmq_send(sock, ctypes.c_char_p(payload), len(payload), 0) < 0:
            raise YouTubeLiveError(_zmq_error(lib))
        reply_buf = ctypes.create_string_buffer(2048)
        reply_len = lib.zmq_recv(sock, reply_buf, ctypes.sizeof(reply_buf) - 1, 0)
        if reply_len < 0:
            raise YouTubeLiveError(_zmq_error(lib))
        return reply_buf.raw[:reply_len].decode("utf-8", errors="ignore").strip()
    finally:
        if sock:
            lib.zmq_close(sock)
        lib.zmq_ctx_term(ctx)


def _live_audio_commands(mode):
    mode = normalize_audio_mode(mode)
    if mode == "voice":
        return [
            ("highpass@voice_hp", "mix", "1"),
            ("lowpass@voice_lp", "mix", "1"),
            ("acompressor@voice_comp", "mix", "1"),
            ("volume@audio_gain", "volume", "2"),
        ]
    if mode == "mute":
        return [
            ("highpass@voice_hp", "mix", "0"),
            ("lowpass@voice_lp", "mix", "0"),
            ("acompressor@voice_comp", "mix", "0"),
            ("volume@audio_gain", "volume", "0"),
        ]
    return [
        ("highpass@voice_hp", "mix", "0"),
        ("lowpass@voice_lp", "mix", "0"),
        ("acompressor@voice_comp", "mix", "0"),
        ("volume@audio_gain", "volume", "1"),
    ]


def _apply_live_audio_mode(relay, mode):
    endpoint = (relay or {}).get("control_url") or _proxy_control_url()
    last_error = None
    for _ in range(20):
        try:
            replies = []
            for target, command, arg in _live_audio_commands(mode):
                reply = _zmq_send_command(endpoint, f"{target} {command} {arg}")
                replies.append(reply)
            for reply in replies:
                if not reply.startswith("0 "):
                    raise YouTubeLiveError(f"Failed to update live audio mode: {reply or 'no relay reply'}")
            return replies
        except YouTubeLiveError as exc:
            last_error = exc
            time.sleep(0.1)
    raise last_error or YouTubeLiveError("Failed to update live audio mode")


def _build_publish_info(stream_name, ingestion_info, ap_ip):
    rtmp_base = ingestion_info.get("ingestionAddress") or ""
    rtmps_base = ingestion_info.get("rtmpsIngestionAddress") or rtmp_base
    target_url = f"{rtmp_base.rstrip('/')}/{stream_name}" if rtmp_base and stream_name else ""
    proxy_publish_url = ""
    qr_payload = target_url
    mode = "direct"
    if PROXY_ENABLED:
        proxy_publish_url = _proxy_publish_url(ap_ip)
        qr_payload = proxy_publish_url
        mode = "proxy"
    return {
        "mode": mode,
        "qr_payload": qr_payload,
        "proxy_publish_url": proxy_publish_url,
        "target_url": target_url,
        "proxy_listen_url": _proxy_listen_url(),
        "target_rtmp_base": rtmp_base,
        "target_rtmps_base": rtmps_base,
    }


def _stop_proxy_relay():
    relay = load_relay_state()
    pid = relay.get("pid")
    overlay_feed_pid = relay.get("overlay_feed_pid")
    if not pid or not relay.get("running"):
        clear_relay_state()
        return
    try:
        os.kill(int(pid), signal.SIGTERM)
    except OSError:
        pass
    if overlay_feed_pid:
        try:
            os.kill(int(overlay_feed_pid), signal.SIGTERM)
        except OSError:
            pass
    save_relay_state(
        {
            **relay,
            "running": False,
            "status": "stopped",
            "stopped_at": time.time(),
        }
    )


def _start_overlay_feed(png_path):
    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "overlay-feed",
        "--png",
        png_path,
        "--interval",
        str(OVERLAY_FRAME_INTERVAL_SEC),
    ]
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


def _await_relay_ready(proc, listen_url, target_url, log_path):
    deadline = time.time() + RELAY_START_TIMEOUT_SEC
    port = _listen_port_from_url(listen_url)
    while time.time() < deadline:
        exit_code = proc.poll()
        if exit_code is not None:
            detail = _tail_log_text(log_path)
            if detail:
                raise YouTubeLiveError(f"Proxy relay exited early: {detail.splitlines()[-1]}")
            raise YouTubeLiveError(f"Proxy relay exited early with code {exit_code}")
        if _relay_pid_matches(proc.pid, listen_url, target_url) and _relay_port_listening(port, proc.pid):
            return
        time.sleep(0.1)
    if not _relay_pid_matches(proc.pid, listen_url, target_url):
        raise YouTubeLiveError("Proxy relay failed to stay alive after launch")
    raise YouTubeLiveError(f"Proxy relay did not open listen port {port} in time")


def _start_proxy_relay(*, listen_url, target_url, stream_title, audio_mode=None, rotation=None, fps_mode=None, overlay=None):
    if not target_url:
        raise YouTubeLiveError("Missing YouTube RTMP target for proxy relay")
    _stop_proxy_relay()
    RELAY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_handle = RELAY_LOG_PATH.open("ab")
    audio_mode = normalize_audio_mode(audio_mode)
    rotation = normalize_rotation_mode(rotation)
    fps_mode = normalize_fps_mode(fps_mode)
    overlay = normalize_overlay_state(overlay or load_overlay_state())
    overlay_active = bool(overlay.get("enabled") and overlay.get("png_path"))
    overlay_feed = None
    overlay_fd = None
    if overlay_active:
        overlay_feed = _start_overlay_feed(overlay["png_path"])
        if overlay_feed.stdout is not None:
            overlay_fd = overlay_feed.stdout.fileno()
    video_pipeline = _proxy_video_pipeline_state(rotation, fps_mode, overlay)
    argv = _proxy_relay_argv(
        listen_url=listen_url,
        target_url=target_url,
        audio_mode=audio_mode,
        rotation=rotation,
        fps_mode=fps_mode,
        overlay=overlay,
        overlay_fd=overlay_fd,
    )
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
            pass_fds=(() if overlay_fd is None else (overlay_fd,)),
        )
    except FileNotFoundError as exc:
        log_handle.close()
        if overlay_feed:
            overlay_feed.kill()
        raise YouTubeLiveError(f"{FFMPEG_BIN} is not installed; proxy relay cannot start") from exc
    except Exception:
        log_handle.close()
        if overlay_feed:
            overlay_feed.kill()
        raise
    if overlay_feed and overlay_feed.stdout is not None:
        overlay_feed.stdout.close()
    try:
        _await_relay_ready(proc, listen_url, target_url, RELAY_LOG_PATH)
    except Exception:
        try:
            proc.terminate()
        except OSError:
            pass
        if overlay_feed:
            try:
                overlay_feed.terminate()
            except OSError:
                pass
        clear_relay_state()
        log_handle.close()
        raise
    relay = _decorate_stream_state(
        {
            "status": "running",
            "running": True,
            "pid": proc.pid,
            "listen_url": listen_url,
            "target_url": target_url,
            "log_path": str(RELAY_LOG_PATH),
            "control_url": _proxy_control_url(),
            "stream_title": stream_title,
            "started_at": time.time(),
            "ffmpeg_bin": FFMPEG_BIN,
            "mode": video_pipeline["mode"],
            "video_encoder": video_pipeline["video_encoder"],
            "video_encoder_kind": video_pipeline["video_encoder_kind"],
            "video_encoder_label": video_pipeline["video_encoder_label"],
            "audio_mode": audio_mode,
            "rotation": rotation,
            "fps_mode": fps_mode,
            "overlay": overlay,
            "overlay_enabled": overlay_active,
            "overlay_feed_pid": overlay_feed.pid if overlay_feed else 0,
        },
        default_audio_mode=audio_mode,
        default_rotation=rotation,
        default_fps_mode=fps_mode,
    )
    if audio_mode != "normal":
        try:
            _apply_live_audio_mode(relay, audio_mode)
        except YouTubeLiveError as exc:
            relay["warning"] = f"Audio mode pending until relay is ready: {exc}"
            LOGGER.warning(
                "Proxy relay started but initial live audio mode command failed: mode=%s error=%s",
                audio_mode,
                exc,
            )
    log_handle.close()
    save_relay_state(relay)
    return relay


def set_proxy_audio_mode(mode):
    desired_mode = normalize_audio_mode(mode)
    state = load_stream_state()
    if not state:
        raise YouTubeLiveError("No YouTube stream has been created yet")
    if state.get("mode") != "proxy":
        raise YouTubeLiveError("Audio mode switching is only available when proxy relay mode is enabled")
    listen_url = state.get("proxy_listen_url", "")
    target_url = state.get("target_url", "")
    if not listen_url or not target_url:
        raise YouTubeLiveError("Proxy relay settings are incomplete")

    current_mode = normalize_audio_mode((state.get("relay") or {}).get("audio_mode") or state.get("audio_mode"))
    relay = state.get("relay") or {}
    if current_mode == desired_mode and relay.get("running"):
        return load_stream_state()

    state["audio_mode"] = desired_mode
    if relay.get("running"):
        _apply_live_audio_mode(relay, desired_mode)
        relay["audio_mode"] = desired_mode
        relay["audio_mode_label"] = audio_mode_spec(desired_mode)["label"]
        relay["audio_mode_short"] = audio_mode_spec(desired_mode)["short_label"]
        relay["audio_mode_description"] = audio_mode_spec(desired_mode)["description"]
        relay["updated_at"] = time.time()
        relay["status"] = "running"
        state["relay"] = relay
    else:
        state["relay"] = _start_proxy_relay(
            listen_url=listen_url,
            target_url=target_url,
            stream_title=state.get("title", ""),
            audio_mode=desired_mode,
            rotation=state.get("rotation"),
            fps_mode=state.get("fps_mode"),
        )
    save_stream_state(state)
    return load_stream_state()


def set_proxy_rotation_mode(mode):
    desired_mode = normalize_rotation_mode(mode)
    state = load_stream_state()
    if not state:
        raise YouTubeLiveError("No YouTube stream has been created yet")
    if state.get("mode") != "proxy":
        raise YouTubeLiveError("Rotation switching is only available when proxy relay mode is enabled")
    listen_url = state.get("proxy_listen_url", "")
    target_url = state.get("target_url", "")
    if not listen_url or not target_url:
        raise YouTubeLiveError("Proxy relay settings are incomplete")

    current_mode = normalize_rotation_mode((state.get("relay") or {}).get("rotation") or state.get("rotation"))
    relay = state.get("relay") or {}
    if current_mode == desired_mode and relay.get("running"):
        return load_stream_state()

    state["rotation"] = desired_mode
    state["relay"] = _start_proxy_relay(
        listen_url=listen_url,
        target_url=target_url,
        stream_title=state.get("title", ""),
        audio_mode=state.get("audio_mode"),
        rotation=desired_mode,
        fps_mode=state.get("fps_mode"),
    )
    save_stream_state(state)
    return load_stream_state()


def set_proxy_fps_mode(mode):
    desired_mode = normalize_fps_mode(mode)
    state = load_stream_state()
    if not state:
        raise YouTubeLiveError("No YouTube stream has been created yet")
    if state.get("mode") != "proxy":
        raise YouTubeLiveError("FPS switching is only available when proxy relay mode is enabled")
    listen_url = state.get("proxy_listen_url", "")
    target_url = state.get("target_url", "")
    if not listen_url or not target_url:
        raise YouTubeLiveError("Proxy relay settings are incomplete")

    current_mode = normalize_fps_mode((state.get("relay") or {}).get("fps_mode") or state.get("fps_mode"))
    relay = state.get("relay") or {}
    if current_mode == desired_mode and relay.get("running"):
        return load_stream_state()

    state["fps_mode"] = desired_mode
    state["relay"] = _start_proxy_relay(
        listen_url=listen_url,
        target_url=target_url,
        stream_title=state.get("title", ""),
        audio_mode=state.get("audio_mode"),
        rotation=state.get("rotation"),
        fps_mode=desired_mode,
    )
    save_stream_state(state)
    return load_stream_state()


def refresh_proxy_overlay():
    state = load_stream_state()
    if not state:
        raise YouTubeLiveError("No YouTube stream has been created yet")
    if state.get("mode") != "proxy":
        raise YouTubeLiveError("Overlay refresh is only available when proxy relay mode is enabled")
    listen_url = state.get("proxy_listen_url", "")
    target_url = state.get("target_url", "")
    if not listen_url or not target_url:
        raise YouTubeLiveError("Proxy relay settings are incomplete")
    relay = state.get("relay") or {}
    state["relay"] = _start_proxy_relay(
        listen_url=listen_url,
        target_url=target_url,
        stream_title=state.get("title", ""),
        audio_mode=(relay.get("audio_mode") or state.get("audio_mode")),
        rotation=(relay.get("rotation") or state.get("rotation")),
        fps_mode=(relay.get("fps_mode") or state.get("fps_mode")),
        overlay=load_overlay_state(),
    )
    save_stream_state(state)
    return load_stream_state()


def create_stream_bundle(*, ap_ip="-", title=None, rotation=None, fps_mode=None, audio_mode=None):
    title = (title or "").strip() or _default_stream_title()
    audio_mode = normalize_audio_mode(audio_mode)
    rotation = normalize_rotation_mode(rotation)
    fps_mode = normalize_fps_mode(fps_mode)
    LOGGER.info("YouTube stream creation request started: title=%s ap_ip=%s", title, ap_ip)
    LOGGER.info("Creating YouTube liveStream resource and waiting for API response")
    update_creation_state(status="creating", message="Creating stream target", progress_pct=20, stage="stream")
    stream = _api_request(
        "POST",
        "liveStreams",
        params={"part": "snippet,cdn,contentDetails,status"},
        body={
            "snippet": {"title": title},
            "cdn": {
                "frameRate": "variable",
                "ingestionType": "rtmp",
                "resolution": "variable",
            },
            "contentDetails": {"isReusable": True},
        },
    )
    stream_id = stream.get("id", "")
    ingestion_info = ((stream.get("cdn") or {}).get("ingestionInfo") or {})
    stream_name = ingestion_info.get("streamName", "")
    LOGGER.info("YouTube liveStream created: stream_id=%s", stream_id or "-")

    scheduled_start = (datetime.now(timezone.utc) + timedelta(minutes=2)).replace(microsecond=0).isoformat()
    LOGGER.info("Creating YouTube liveBroadcast resource and waiting for API response")
    update_creation_state(status="creating", message="Creating broadcast", progress_pct=45, stage="broadcast")
    broadcast = _api_request(
        "POST",
        "liveBroadcasts",
        params={"part": "snippet,status,contentDetails"},
        body={
            "snippet": {
                "title": title,
                "scheduledStartTime": scheduled_start,
            },
            "status": {
                "privacyStatus": STREAM_PRIVACY_STATUS,
                "selfDeclaredMadeForKids": False,
            },
            "contentDetails": {
                "enableAutoStart": True,
                "enableAutoStop": True,
                "monitorStream": {"enableMonitorStream": False},
            },
        },
    )
    broadcast_id = broadcast.get("id", "")
    LOGGER.info("YouTube liveBroadcast created: broadcast_id=%s", broadcast_id or "-")

    LOGGER.info("Binding YouTube broadcast to stream and waiting for API response")
    update_creation_state(status="creating", message="Binding stream", progress_pct=75, stage="bind")
    _api_request(
        "POST",
        "liveBroadcasts/bind",
        params={
            "part": "id,contentDetails",
            "id": broadcast_id,
            "streamId": stream_id,
        },
    )

    publish = _build_publish_info(stream_name, ingestion_info, ap_ip)
    state = {
        "created_at": time.time(),
        "title": title,
        "broadcast_id": broadcast_id,
        "watch_url": f"https://www.youtube.com/watch?v={broadcast_id}" if broadcast_id else "",
        "stream_id": stream_id,
        "stream_name": stream_name,
        "ingestion_address": ingestion_info.get("ingestionAddress", ""),
        "rtmps_ingestion_address": ingestion_info.get("rtmpsIngestionAddress", ""),
        "privacy_status": STREAM_PRIVACY_STATUS,
        "ap_ip": ap_ip,
        "audio_mode": audio_mode if PROXY_ENABLED else "normal",
        "rotation": rotation,
        "fps_mode": fps_mode,
        **publish,
    }
    _stop_proxy_relay()
    if state.get("mode") == "proxy":
        state["relay"] = _start_proxy_relay(
            listen_url=state.get("proxy_listen_url", ""),
            target_url=state.get("target_url", ""),
            stream_title=title,
            audio_mode=state.get("audio_mode"),
            rotation=state.get("rotation"),
            fps_mode=state.get("fps_mode"),
            overlay=load_overlay_state(),
        )
    save_stream_state(state)
    LOGGER.info(
        "YouTube stream bundle ready: title=%s broadcast_id=%s mode=%s",
        state.get("title", ""),
        state.get("broadcast_id", ""),
        state.get("mode", ""),
    )
    return state


def _run_creation_job(ap_ip, title, rotation, fps_mode, audio_mode):
    try:
        update_creation_state(pid=os.getpid(), status="creating")
        LOGGER.info(
            "YouTube async creation job started: ap_ip=%s title=%s audio_mode=%s rotation=%s fps_mode=%s",
            ap_ip,
            title or "",
            audio_mode,
            rotation,
            fps_mode,
        )
        state = create_stream_bundle(
            ap_ip=ap_ip,
            title=title,
            rotation=rotation,
            fps_mode=fps_mode,
            audio_mode=audio_mode,
        )
        save_creation_state(
            {
                "status": "ready",
                "message": "Stream created",
                "progress_pct": 100,
                "stage": "ready",
                "finished_at": time.time(),
                "pid": os.getpid(),
                "title": state.get("title", ""),
                "watch_url": state.get("watch_url", ""),
                "qr_payload": state.get("qr_payload", ""),
                "relay_status": ((state.get("relay") or {}).get("status", "")),
                "log_path": str(CREATION_LOG_PATH),
            }
        )
        LOGGER.info("YouTube async creation job finished successfully: title=%s", state.get("title", ""))
    except Exception as exc:
        save_creation_state(
            {
                "status": "error",
                "message": str(exc),
                "progress_pct": 100,
                "stage": "error",
                "finished_at": time.time(),
                "pid": os.getpid(),
                "log_path": str(CREATION_LOG_PATH),
            }
        )
        LOGGER.exception("YouTube async creation job failed: %s", exc)
        raise


def start_stream_creation(*, ap_ip="-", title=None, rotation=None, fps_mode=None, audio_mode=None):
    if creation_in_progress():
        LOGGER.warning("Rejected YouTube stream creation request because one is already in progress")
        raise YouTubeLiveError("Stream creation already in progress")
    validation = validate_live_access()
    if not validation.get("ok"):
        message = validation.get("message") or "YouTube Live validation failed"
        LOGGER.warning("Rejected YouTube stream creation request because validation failed: %s", message)
        raise YouTubeLiveError(message)
    audio_mode = normalize_audio_mode(audio_mode)
    rotation = normalize_rotation_mode(rotation)
    fps_mode = normalize_fps_mode(fps_mode)
    fd = _lock_creation()
    try:
        if creation_in_progress():
            LOGGER.warning("Rejected YouTube stream creation request because one is already in progress")
            raise YouTubeLiveError("Stream creation already in progress")
        reset_creation_log(
            ap_ip=ap_ip,
            title=title or "",
            rotation=rotation,
            fps_mode=fps_mode,
            audio_mode=audio_mode,
        )
        save_creation_state(
            {
                "status": "creating",
                "message": "Stream is creating",
                "progress_pct": 5,
                "stage": "queued",
                "started_at": time.time(),
                "ap_ip": ap_ip,
                "title": title or "",
                "audio_mode": audio_mode,
                "rotation": rotation,
                "fps_mode": fps_mode,
                "log_path": str(CREATION_LOG_PATH),
            }
        )
        LOGGER.info(
            "Starting background YouTube stream creation process: ap_ip=%s title=%s audio_mode=%s rotation=%s fps_mode=%s",
            ap_ip,
            title or "",
            audio_mode,
            rotation,
            fps_mode,
        )
        argv = [sys.executable, str(Path(__file__).resolve()), "create", "--ap-ip", ap_ip or "-"]
        if title:
            argv.extend(["--title", title])
        if audio_mode != DEFAULT_PROXY_AUDIO_MODE:
            argv.extend(["--audio-mode", audio_mode])
        if rotation != "0":
            argv.extend(["--rotation", rotation])
        if fps_mode != "original":
            argv.extend(["--fps-mode", fps_mode])
        log_handle = CREATION_LOG_PATH.open("ab")
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
            )
        finally:
            log_handle.close()
        update_creation_state(pid=proc.pid)
    finally:
        _unlock_creation(fd)


def creation_in_progress():
    state = load_creation_state()
    return state.get("status") == "creating"


def qr_data_uri(payload):
    if not payload or qrcode is None:
        return ""
    image = qrcode.make(payload)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _parse_cli_args(argv):
    ap_ip = "-"
    title = ""
    audio_mode = DEFAULT_PROXY_AUDIO_MODE
    rotation = "0"
    fps_mode = "original"
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
        idx += 1
    return (
        ap_ip,
        title,
        normalize_audio_mode(audio_mode),
        normalize_rotation_mode(rotation),
        normalize_fps_mode(fps_mode),
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
    last_payload = b""
    while True:
        try:
            payload = Path(png_path).read_bytes()
            if payload.startswith(b"\x89PNG\r\n\x1a\n"):
                last_payload = payload
        except OSError:
            payload = b""
        if last_payload:
            try:
                sys.stdout.buffer.write(last_payload)
                sys.stdout.buffer.flush()
            except BrokenPipeError:
                break
        time.sleep(interval)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "create":
        logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s", force=True)
        cli_ap_ip, cli_title, cli_audio_mode, cli_rotation, cli_fps_mode = _parse_cli_args(sys.argv[2:])
        _run_creation_job(cli_ap_ip, cli_title, cli_rotation, cli_fps_mode, cli_audio_mode)
    elif len(sys.argv) >= 2 and sys.argv[1] == "overlay-feed":
        cli_png_path, cli_interval = _parse_overlay_feed_args(sys.argv[2:])
        _run_overlay_feed(cli_png_path, cli_interval)

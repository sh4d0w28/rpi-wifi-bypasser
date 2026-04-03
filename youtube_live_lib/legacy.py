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
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .auth_service import (
    client_ready as auth_client_ready,
    ensure_access_token as auth_ensure_access_token,
    get_auth_status as auth_get_auth_status,
    make_api_request,
    poll_device_authorization as auth_poll_device_authorization,
    refresh_access_token as auth_refresh_access_token,
    start_device_authorization as auth_start_device_authorization,
    validate_live_access as auth_validate_live_access,
)
from .creation_service import (
    create_stream_bundle as service_create_stream_bundle,
    creation_in_progress as service_creation_in_progress,
    run_creation_job as service_run_creation_job,
    start_stream_creation as service_start_stream_creation,
)
from .config import (
    CLIENT_CONFIG_PATH,
    CREATION_LOG_PATH,
    DEFAULT_OVERLAY_HTML,
    DEFAULT_PROXY_AUDIO_MODE,
    DEVICE_CODE_URL,
    DEVICE_STATE_PATH,
    FFMPEG_BIN,
    OVERLAY_FRAME_INTERVAL_SEC,
    OVERLAY_HTML_PATH,
    OVERLAY_PNG_PATH,
    OVERLAY_STATE_PATH,
    PROXY_ENABLED,
    PROXY_HW_VIDEO_BITRATE,
    PROXY_HW_VIDEO_ENCODER,
    PROXY_PUBLISH_URL_TEMPLATE,
    PROXY_RTMP_APP,
    PROXY_RTMP_PORT,
    PROXY_VIDEO_CRF,
    PROXY_VIDEO_ENCODER,
    PROXY_VIDEO_PRESET,
    PROXY_ZMQ_PORT,
    RELAY_LOCK_PATH,
    RELAY_LOG_PATH,
    RELAY_START_TIMEOUT_SEC,
    RELAY_STATE_PATH,
    RELAY_STOP_TIMEOUT_SEC,
    STREAM_CREATE_LOCK_PATH,
    STREAM_CREATE_STATE_PATH,
    STREAM_PRIVACY_STATUS,
    STREAM_STATE_PATH,
    STREAM_TITLE_PREFIX,
    TOKEN_PATH,
    TOKEN_URL,
    YOUTUBE_API_BASE,
    YOUTUBE_SCOPE,
    ZMQ_LINGER,
    ZMQ_RCVTIMEO,
    ZMQ_REQ,
    ZMQ_SNDTIMEO,
)
from .errors import YouTubeLiveError
from .modes import (
    AUDIO_MODE_SPECS,
    FPS_MODE_SPECS,
    ROTATION_MODE_SPECS,
    audio_mode_spec,
    decorate_audio_mode_fields,
    decorate_fps_fields,
    decorate_rotation_fields,
    decorate_stream_state,
    fps_mode_spec,
    list_audio_modes,
    list_fps_modes,
    list_rotation_modes,
    normalize_audio_mode,
    normalize_fps_mode,
    normalize_rotation_mode,
    rotation_mode_spec,
)
from .relay_runtime import (
    _run_overlay_feed as runtime_run_overlay_feed,
    ensure_proxy_relay_running as runtime_ensure_proxy_relay_running,
    refresh_proxy_overlay as runtime_refresh_proxy_overlay,
    set_proxy_audio_mode as runtime_set_proxy_audio_mode,
    set_proxy_fps_mode as runtime_set_proxy_fps_mode,
    set_proxy_rotation_mode as runtime_set_proxy_rotation_mode,
)
from .storage import (
    clear_creation_state as storage_clear_creation_state,
    clear_device_state as storage_clear_device_state,
    clear_relay_state as storage_clear_relay_state,
    coerce_float,
    coerce_int,
    default_overlay_state as storage_default_overlay_state,
    drop_json,
    ensure_overlay_html_exists as storage_ensure_overlay_html_exists,
    load_client_config as storage_load_client_config,
    load_creation_log as storage_load_creation_log,
    load_creation_state as storage_load_creation_state,
    load_device_state as storage_load_device_state,
    load_json,
    load_overlay_state as storage_load_overlay_state,
    load_relay_state as storage_load_relay_state,
    load_stream_state as storage_load_stream_state,
    load_token as storage_load_token,
    normalize_client_config,
    normalize_creation_state as storage_normalize_creation_state,
    normalize_overlay_state as storage_normalize_overlay_state,
    normalize_relay_state as storage_normalize_relay_state,
    reset_creation_log as storage_reset_creation_log,
    save_creation_state as storage_save_creation_state,
    save_device_state as storage_save_device_state,
    save_json,
    save_overlay_state as storage_save_overlay_state,
    save_relay_state as storage_save_relay_state,
    save_stream_state as storage_save_stream_state,
    save_token as storage_save_token,
    update_creation_state as storage_update_creation_state,
)
from .youtube_api import api_request as youtube_api_request, http_form, http_json

try:
    import fcntl
except Exception:
    fcntl = None

try:
    import qrcode
except Exception:
    qrcode = None

LOGGER = logging.getLogger(__name__)
if DEFAULT_PROXY_AUDIO_MODE not in AUDIO_MODE_SPECS:
    raise RuntimeError("DEFAULT_PROXY_AUDIO_MODE must be defined in AUDIO_MODE_SPECS")

VIDEO_DIMENSION_RE = re.compile(r'(\d{2,5})x(\d{2,5})(?:\s|\[|,|$)')
FFMPEG_BITRATE_RE = re.compile(r'bitrate=\s*([0-9.]+)\s*kbits/s')
FFMPEG_SPEED_RE = re.compile(r'speed=\s*([0-9.]+)x')
FFMPEG_ENCODER_RE = re.compile(r'^\s*[A-Z\.]+\s+([^\s]+)\s+', re.MULTILINE)
_ENCODER_CACHE = None


def _decorate_audio_mode_fields(state, *, default_mode=None):
    return decorate_audio_mode_fields(state, default_mode=default_mode)


def _decorate_rotation_fields(state, *, default_mode=None):
    return decorate_rotation_fields(state, default_mode=default_mode)


def _decorate_fps_fields(state, *, default_mode=None):
    return decorate_fps_fields(state, default_mode=default_mode)


def _decorate_stream_state(state, *, default_audio_mode=None, default_rotation=None, default_fps_mode=None):
    return decorate_stream_state(
        state,
        default_audio_mode=default_audio_mode,
        default_rotation=default_rotation,
        default_fps_mode=default_fps_mode,
    )


def _load_json(path, default):
    return load_json(path, default)


def _save_json(path, payload):
    save_json(path, payload)


def _drop_json(path):
    drop_json(path)


def _coerce_int(value, default, minimum=None, maximum=None):
    return coerce_int(value, default, minimum=minimum, maximum=maximum)


def _coerce_float(value, default, minimum=None, maximum=None):
    return coerce_float(value, default, minimum=minimum, maximum=maximum)


def default_overlay_state():
    return storage_default_overlay_state()


def normalize_overlay_state(state):
    return storage_normalize_overlay_state(state)


def load_overlay_state():
    return storage_load_overlay_state()


def save_overlay_state(state):
    storage_save_overlay_state(state)


def ensure_overlay_html_exists():
    storage_ensure_overlay_html_exists()


def _http_json(url, *, method="GET", headers=None, data=None):
    return http_json(url, method=method, headers=headers, data=data)


def _http_form(url, fields):
    return http_form(url, fields)


def _normalize_client_config(data):
    return normalize_client_config(data)


def load_client_config():
    return storage_load_client_config()


def load_token():
    return storage_load_token()


def save_token(token):
    storage_save_token(token)


def load_device_state():
    return storage_load_device_state()


def save_device_state(state):
    storage_save_device_state(state)


def clear_device_state():
    storage_clear_device_state()


def load_stream_state():
    return storage_load_stream_state(load_relay_state_fn=load_relay_state)


def save_stream_state(state):
    storage_save_stream_state(state)


def load_relay_state():
    return storage_load_relay_state(
        populate_relay_video_fields=_populate_relay_video_fields,
        relay_pid_matches=_relay_pid_matches,
    )


def save_relay_state(state):
    storage_save_relay_state(state)


def clear_relay_state():
    storage_clear_relay_state()


def load_creation_state():
    return storage_load_creation_state(pid_alive=_pid_alive)


def save_creation_state(state):
    storage_save_creation_state(state)


def clear_creation_state():
    storage_clear_creation_state()


def update_creation_state(**fields):
    storage_update_creation_state(fields=fields, pid_alive=_pid_alive)


def reset_creation_log(*, ap_ip="-", title="", rotation="0", fps_mode="original", audio_mode="normal"):
    storage_reset_creation_log(
        ap_ip=ap_ip,
        title=title,
        rotation=rotation,
        fps_mode=fps_mode,
        audio_mode=audio_mode,
    )


def load_creation_log(max_bytes=262144):
    return storage_load_creation_log(max_bytes=max_bytes)


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


def _wait_pid_exit(pid, timeout_sec):
    deadline = time.time() + max(0.1, timeout_sec)
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return not _pid_alive(pid)


def _wait_port_release(port, pid=None, timeout_sec=5.0):
    if not port:
        return True
    deadline = time.time() + max(0.1, timeout_sec)
    while time.time() < deadline:
        if not _relay_port_listening(port, pid):
            return True
        time.sleep(0.1)
    return not _relay_port_listening(port, pid)


@contextmanager
def _relay_lock():
    RELAY_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = RELAY_LOCK_PATH.open("a+b")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def normalize_creation_state(state):
    return storage_normalize_creation_state(state, pid_alive=_pid_alive)


def normalize_relay_state(state):
    return storage_normalize_relay_state(
        state,
        populate_relay_video_fields=_populate_relay_video_fields,
        relay_pid_matches=_relay_pid_matches,
    )


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
    return auth_client_ready(load_client_config_fn=load_client_config)


def authorization_ready():
    return bool(load_token().get("refresh_token") or load_token().get("access_token"))


def validate_live_access():
    return auth_validate_live_access(
        authorization_ready_fn=authorization_ready,
        api_request_fn=_api_request,
    )


def get_auth_status():
    return auth_get_auth_status(
        load_token_fn=load_token,
        load_device_state_fn=load_device_state,
        client_ready_fn=client_ready,
        authorization_ready_fn=authorization_ready,
        validate_live_access_fn=validate_live_access,
        load_creation_state_fn=load_creation_state,
    )


def start_device_authorization():
    return auth_start_device_authorization(
        load_client_config_fn=load_client_config,
        save_device_state_fn=save_device_state,
    )


def poll_device_authorization():
    return auth_poll_device_authorization(
        load_client_config_fn=load_client_config,
        load_device_state_fn=load_device_state,
        clear_device_state_fn=clear_device_state,
        load_token_fn=load_token,
        save_token_fn=save_token,
    )


def _refresh_access_token(token):
    return auth_refresh_access_token(
        token=token,
        load_client_config_fn=load_client_config,
        save_token_fn=save_token,
    )


def ensure_access_token():
    return auth_ensure_access_token(
        load_token_fn=load_token,
        refresh_access_token_fn=_refresh_access_token,
    )


def _api_request(method, path, *, params=None, body=None):
    return youtube_api_request(
        method,
        path,
        ensure_access_token_fn=ensure_access_token,
        params=params,
        body=body,
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
    argv.extend(
        [
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
    )
    if video_filters or overlay_active:
        video_encoder = _resolve_proxy_video_encoder()
        if video_filters and not overlay_active:
            argv.extend(["-vf", ",".join(video_filters)])
        argv.append("-c:v")
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


def _stop_proxy_relay_unlocked():
    relay = load_relay_state()
    pid = relay.get("pid")
    overlay_feed_pid = relay.get("overlay_feed_pid")
    port = _listen_port_from_url(relay.get("listen_url", ""))
    if not pid or not relay.get("running"):
        clear_relay_state()
        return
    try:
        os.kill(int(pid), signal.SIGTERM)
    except OSError:
        pass
    if pid and not _wait_pid_exit(pid, RELAY_STOP_TIMEOUT_SEC):
        try:
            os.kill(int(pid), signal.SIGKILL)
        except OSError:
            pass
        _wait_pid_exit(pid, 1.5)
    if overlay_feed_pid:
        try:
            os.kill(int(overlay_feed_pid), signal.SIGTERM)
        except OSError:
            pass
        if not _wait_pid_exit(overlay_feed_pid, 1.5):
            try:
                os.kill(int(overlay_feed_pid), signal.SIGKILL)
            except OSError:
                pass
            _wait_pid_exit(overlay_feed_pid, 1.0)
    _wait_port_release(port, timeout_sec=RELAY_STOP_TIMEOUT_SEC)
    save_relay_state(
        {
            **relay,
            "running": False,
            "status": "stopped",
            "stopped_at": time.time(),
        }
    )


def _stop_proxy_relay():
    with _relay_lock():
        _stop_proxy_relay_unlocked()


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


def _start_proxy_relay_unlocked(*, listen_url, target_url, stream_title, audio_mode=None, rotation=None, fps_mode=None, overlay=None):
    if not target_url:
        raise YouTubeLiveError("Missing YouTube RTMP target for proxy relay")
    _stop_proxy_relay_unlocked()
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


def _start_proxy_relay(*, listen_url, target_url, stream_title, audio_mode=None, rotation=None, fps_mode=None, overlay=None):
    with _relay_lock():
        return _start_proxy_relay_unlocked(
            listen_url=listen_url,
            target_url=target_url,
            stream_title=stream_title,
            audio_mode=audio_mode,
            rotation=rotation,
            fps_mode=fps_mode,
            overlay=overlay,
        )


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


def ensure_proxy_relay_running():
    with _relay_lock():
        state = load_stream_state()
        if not state or state.get("mode") != "proxy":
            return state
        listen_url = state.get("proxy_listen_url", "")
        target_url = state.get("target_url", "")
        if not listen_url or not target_url:
            return state
        relay = state.get("relay") or {}
        if relay.get("running"):
            return state
        LOGGER.warning(
            "Proxy relay watchdog restarting relay: pid=%s running=%s listen_url=%s",
            relay.get("pid"),
            relay.get("running"),
            listen_url,
        )
        state["relay"] = _start_proxy_relay_unlocked(
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
    with _relay_lock():
        _stop_proxy_relay_unlocked()
        if state.get("mode") == "proxy":
            state["relay"] = _start_proxy_relay_unlocked(
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
    return service_creation_in_progress()


def create_stream_bundle(*, ap_ip="-", title=None, rotation=None, fps_mode=None, audio_mode=None):
    return service_create_stream_bundle(
        api_request_fn=_api_request,
        ap_ip=ap_ip,
        title=title,
        rotation=rotation,
        fps_mode=fps_mode,
        audio_mode=audio_mode,
    )


def _run_creation_job(ap_ip, title, rotation, fps_mode, audio_mode):
    return service_run_creation_job(
        api_request_fn=_api_request,
        ap_ip=ap_ip,
        title=title,
        rotation=rotation,
        fps_mode=fps_mode,
        audio_mode=audio_mode,
    )


def start_stream_creation(*, ap_ip="-", title=None, rotation=None, fps_mode=None, audio_mode=None):
    return service_start_stream_creation(
        validate_live_access_fn=validate_live_access,
        ap_ip=ap_ip,
        title=title,
        rotation=rotation,
        fps_mode=fps_mode,
        audio_mode=audio_mode,
    )


def ensure_proxy_relay_running():
    return runtime_ensure_proxy_relay_running()


def refresh_proxy_overlay():
    return runtime_refresh_proxy_overlay()


def set_proxy_audio_mode(mode):
    return runtime_set_proxy_audio_mode(mode)


def set_proxy_rotation_mode(mode):
    return runtime_set_proxy_rotation_mode(mode)


def set_proxy_fps_mode(mode):
    return runtime_set_proxy_fps_mode(mode)


def _run_overlay_feed(png_path, interval):
    return runtime_run_overlay_feed(png_path, interval)


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

"""Relay runtime and overlay process helpers for YouTube live support."""

import ctypes
import ctypes.util
import logging
import os
import re
import signal
import subprocess
import sys
import time
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except Exception:
    fcntl = None

from .config import (
    FFMPEG_BIN,
    OVERLAY_FRAME_INTERVAL_SEC,
    PROXY_ENABLED,
    PROXY_HW_VIDEO_BITRATE,
    PROXY_HW_VIDEO_ENCODER,
    PROXY_INTERNAL_UDP_PORT,
    PROXY_PUBLISH_URL_TEMPLATE,
    PROXY_RTMP_APP,
    PROXY_RTMP_PORT,
    PROXY_VIDEO_CRF,
    PROXY_VIDEO_ENCODER,
    PROXY_VIDEO_PRESET,
    PROXY_ZMQ_PORT,
    RELAY_EGRESS_LOG_PATH,
    RELAY_LOCK_PATH,
    RELAY_LOG_PATH,
    RELAY_STATE_PATH,
    RELAY_START_TIMEOUT_SEC,
    RELAY_STOP_TIMEOUT_SEC,
    ZMQ_LINGER,
    ZMQ_RCVTIMEO,
    ZMQ_REQ,
    ZMQ_SNDTIMEO,
)
from .errors import YouTubeLiveError
from .modes import (
    audio_mode_spec,
    decorate_stream_state,
    fps_mode_spec,
    normalize_audio_mode,
    normalize_fps_mode,
    normalize_rotation_mode,
    rotation_mode_spec,
)
from .storage import (
    clear_relay_state,
    coerce_float as _coerce_float,
    ensure_overlay_png_exists,
    load_json,
    load_overlay_state,
    load_stream_state as storage_load_stream_state,
    normalize_overlay_state,
    save_relay_state,
    save_stream_state,
)

LOGGER = logging.getLogger(__name__)
VIDEO_DIMENSION_RE = re.compile(r"(\d{2,5})x(\d{2,5})(?:\s|\[|,|$)")
FFMPEG_BITRATE_RE = re.compile(r"bitrate=\s*([0-9.]+)\s*kbits/s")
FFMPEG_SPEED_RE = re.compile(r"speed=\s*([0-9.]+)x")
FFMPEG_ENCODER_RE = re.compile(r"^\s*[A-Z\.]+\s+([^\s]+)\s+", re.MULTILINE)
_ENCODER_CACHE = None


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


def _pid_cmdline_contains(pid, *tokens):
    if not _pid_alive(pid):
        return False
    cmdline = _pid_cmdline(pid)
    if not cmdline:
        return False
    ffmpeg_name = Path(FFMPEG_BIN).name
    if ffmpeg_name not in cmdline and "ffmpeg" not in cmdline:
        return False
    return all(not token or token in cmdline for token in tokens)


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
        proc = subprocess.run(["ss", "-lntp"], text=True, capture_output=True, check=False, timeout=3)
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
        selected = hardware_name if hardware_name in encoders else "libx264"
    if selected != "libx264" and selected not in encoders:
        LOGGER.warning("Requested FFmpeg encoder '%s' is unavailable; falling back to libx264", selected)
        selected = "libx264"
    if selected == hardware_name:
        return {"name": selected, "kind": "hardware", "label": f"Hardware ({selected})"}
    return {"name": "libx264", "kind": "software", "label": "Software (libx264)"}


def _populate_relay_video_fields(state):
    if not isinstance(state, dict) or not state:
        return state
    width = state.get("video_width")
    height = state.get("video_height")
    if width and height:
        payload = dict(state)
        payload["video_orientation"] = _relay_orientation(width, height)
        return payload
    payload = dict(state)
    payload.update(_extract_video_dimensions_from_log(state.get("log_path", "")))
    payload.update(_extract_relay_runtime_metrics_from_log(state.get("log_path", "")))
    return payload


def _normalize_process_state(payload, *, check_fn):
    if not isinstance(payload, dict) or not payload:
        return {}
    state = dict(payload)
    pid = state.get("pid")
    state["running"] = bool(pid and check_fn(pid, state))
    if not state["running"] and state.get("status") in ("running", "standby"):
        state["status"] = "stopped"
        state.setdefault("stopped_at", time.time())
    return state


def _ingress_running(pid, state):
    return _pid_cmdline_contains(pid, state.get("listen_url", ""))


def _egress_running(pid, state):
    tokens = [state.get("input_url", "")]
    if state.get("target_url"):
        tokens.append(state.get("target_url", ""))
    return _pid_cmdline_contains(pid, *tokens)


def _compose_relay_state(*, ingress, egress, stream_title="", audio_mode=None, rotation=None, fps_mode=None, overlay=None):
    ingress = decorate_stream_state(ingress or {}, default_audio_mode=audio_mode, default_rotation=rotation, default_fps_mode=fps_mode)
    egress = decorate_stream_state(egress or {}, default_audio_mode=audio_mode, default_rotation=rotation, default_fps_mode=fps_mode)
    relay = decorate_stream_state(
        {
            "pid": ingress.get("pid", 0),
            "listen_url": ingress.get("listen_url", ""),
            "target_url": egress.get("target_url", ""),
            "log_path": ingress.get("log_path", ""),
            "egress_log_path": egress.get("log_path", ""),
            "control_url": ingress.get("control_url", ""),
            "stream_title": stream_title,
            "started_at": ingress.get("started_at") or egress.get("started_at") or time.time(),
            "ffmpeg_bin": FFMPEG_BIN,
            "mode": ingress.get("mode", ""),
            "video_encoder": ingress.get("video_encoder", ""),
            "video_encoder_kind": ingress.get("video_encoder_kind", ""),
            "video_encoder_label": ingress.get("video_encoder_label", ""),
            "audio_mode": ingress.get("audio_mode") or audio_mode,
            "rotation": ingress.get("rotation") or rotation,
            "fps_mode": ingress.get("fps_mode") or fps_mode,
            "overlay": ingress.get("overlay") or overlay or {},
            "overlay_enabled": bool((ingress.get("overlay") or overlay or {}).get("enabled")),
            "overlay_feed_pid": ingress.get("overlay_feed_pid", 0),
            "internal_output_url": ingress.get("output_url", ""),
            "internal_input_url": egress.get("input_url", _proxy_internal_input_url()),
            "warning": ingress.get("warning") or egress.get("warning") or "",
            "ingress": ingress,
            "egress": egress,
        },
        default_audio_mode=audio_mode,
        default_rotation=rotation,
        default_fps_mode=fps_mode,
    )
    relay["running"] = bool(ingress.get("running"))
    relay["forwarding"] = bool(egress.get("running") and egress.get("target_url"))
    relay["status"] = "running" if relay["forwarding"] else "standby" if relay["running"] else "stopped"
    return _populate_relay_video_fields(relay)


def load_relay_state():
    raw = load_json(RELAY_STATE_PATH, {})
    if not isinstance(raw, dict) or not raw:
        return {}
    ingress = _normalize_process_state(raw.get("ingress") or {}, check_fn=_ingress_running)
    egress = _normalize_process_state(raw.get("egress") or {}, check_fn=_egress_running)
    return _compose_relay_state(
        ingress=ingress,
        egress=egress,
        stream_title=raw.get("stream_title", ""),
        audio_mode=raw.get("audio_mode"),
        rotation=raw.get("rotation"),
        fps_mode=raw.get("fps_mode"),
        overlay=raw.get("overlay"),
    )


def load_stream_state():
    return storage_load_stream_state(load_relay_state_fn=load_relay_state)


def _proxy_publish_url(ap_ip):
    if PROXY_PUBLISH_URL_TEMPLATE:
        return PROXY_PUBLISH_URL_TEMPLATE.format(ap_ip=ap_ip or "")
    host = ap_ip or "127.0.0.1"
    if PROXY_RTMP_APP:
        return f"rtmp://{host}/{PROXY_RTMP_APP}" if PROXY_RTMP_PORT == 1935 else f"rtmp://{host}:{PROXY_RTMP_PORT}/{PROXY_RTMP_APP}"
    return f"rtmp://{host}" if PROXY_RTMP_PORT == 1935 else f"rtmp://{host}:{PROXY_RTMP_PORT}"


def _proxy_listen_url():
    return f"rtmp://0.0.0.0:{PROXY_RTMP_PORT}/{PROXY_RTMP_APP}" if PROXY_RTMP_APP else f"rtmp://0.0.0.0:{PROXY_RTMP_PORT}"


def _proxy_control_url():
    return f"tcp://127.0.0.1:{PROXY_ZMQ_PORT}"


def _proxy_internal_output_url():
    return f"udp://127.0.0.1:{PROXY_INTERNAL_UDP_PORT}?pkt_size=1316"


def _proxy_internal_input_url():
    return f"udp://127.0.0.1:{PROXY_INTERNAL_UDP_PORT}?fifo_size=1000000&overrun_nonfatal=1"


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


def _proxy_ingress_argv(*, listen_url, audio_mode, rotation, fps_mode, overlay=None, overlay_fd=None):
    rotation = normalize_rotation_mode(rotation)
    rotation_spec = rotation_mode_spec(rotation)
    fps_mode = normalize_fps_mode(fps_mode)
    fps_spec = fps_mode_spec(fps_mode)
    overlay = normalize_overlay_state(overlay or {})
    overlay_active = bool(overlay_fd is not None and overlay.get("png_path"))
    argv = [FFMPEG_BIN, "-hide_banner", "-loglevel", "info", "-stats", "-listen", "1", "-i", listen_url]
    if overlay_active:
        argv.extend(["-thread_queue_size", "8", "-f", "image2pipe", "-framerate", "1", "-c:v", "png", "-i", f"pipe:{overlay_fd}"])
    video_filters = []
    if rotation_spec.get("transpose"):
        video_filters.append(f"transpose={rotation_spec['transpose']}")
    if fps_spec.get("fps"):
        video_filters.append(f"fps={fps_spec['fps']}")
    if overlay_active:
        overlay_filters = ["format=rgba", f"scale=w={overlay.get('width') or -1}:h={overlay.get('height') or -1}"]
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
    argv.extend(["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "1", "-af", _relay_audio_filter()])
    if video_filters or overlay_active:
        video_encoder = _resolve_proxy_video_encoder()
        if video_filters and not overlay_active:
            argv.extend(["-vf", ",".join(video_filters)])
        argv.append("-c:v")
        if video_encoder["name"] == "libx264":
            argv.extend(["libx264", "-preset", PROXY_VIDEO_PRESET, "-tune", "zerolatency", "-crf", PROXY_VIDEO_CRF, "-pix_fmt", "yuv420p"])
        else:
            argv.extend([video_encoder["name"], "-b:v", PROXY_HW_VIDEO_BITRATE, "-maxrate", PROXY_HW_VIDEO_BITRATE, "-bufsize", PROXY_HW_VIDEO_BITRATE, "-pix_fmt", "yuv420p"])
    else:
        argv.extend(["-c:v", "copy"])
    tee_target = f"[f=mpegts:onfail=ignore]{_proxy_internal_output_url()}|[f=null]-"
    argv.extend(["-f", "tee", tee_target])
    return argv


def _proxy_egress_argv(*, target_url):
    argv = [FFMPEG_BIN, "-hide_banner", "-loglevel", "info", "-stats", "-i", _proxy_internal_input_url()]
    argv.extend(["-map", "0:v?", "-map", "0:a?", "-c:v", "copy", "-c:a", "copy"])
    if target_url:
        argv.extend(["-f", "flv", target_url])
    else:
        argv.extend(["-f", "null", "-"])
    return argv


def _proxy_video_pipeline_state(rotation, fps_mode, overlay=None):
    rotation = normalize_rotation_mode(rotation)
    fps_mode = normalize_fps_mode(fps_mode)
    overlay = normalize_overlay_state(overlay or load_overlay_state())
    if rotation == "0" and fps_mode == "original" and not overlay.get("png_path"):
        return {"mode": "copy-video-live-audio", "video_encoder": "copy", "video_encoder_kind": "copy", "video_encoder_label": "Passthrough"}
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
    candidates.extend(["libzmq.so.5", "libzmq.so", "libzmq.dylib", "libzmq.dll", "zmq.dll"])
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
        return [("highpass@voice_hp", "mix", "1"), ("lowpass@voice_lp", "mix", "1"), ("acompressor@voice_comp", "mix", "1"), ("volume@audio_gain", "volume", "2")]
    if mode == "mute":
        return [("highpass@voice_hp", "mix", "0"), ("lowpass@voice_lp", "mix", "0"), ("acompressor@voice_comp", "mix", "0"), ("volume@audio_gain", "volume", "0")]
    return [("highpass@voice_hp", "mix", "0"), ("lowpass@voice_lp", "mix", "0"), ("acompressor@voice_comp", "mix", "0"), ("volume@audio_gain", "volume", "1")]


def _apply_live_audio_mode(relay, mode):
    endpoint = (relay or {}).get("control_url") or _proxy_control_url()
    last_error = None
    for _ in range(20):
        try:
            replies = [_zmq_send_command(endpoint, f"{target} {command} {arg}") for target, command, arg in _live_audio_commands(mode)]
            for reply in replies:
                if not reply.startswith("0 "):
                    raise YouTubeLiveError(f"Failed to update live audio mode: {reply or 'no relay reply'}")
            return replies
        except YouTubeLiveError as exc:
            last_error = exc
            time.sleep(0.1)
    raise last_error or YouTubeLiveError("Failed to update live audio mode")


def build_publish_info(stream_name, ingestion_info, ap_ip):
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


def _stop_process(pid, timeout_sec):
    if not pid:
        return
    try:
        os.kill(int(pid), signal.SIGTERM)
    except OSError:
        return
    if not _wait_pid_exit(pid, timeout_sec):
        try:
            os.kill(int(pid), signal.SIGKILL)
        except OSError:
            pass
        _wait_pid_exit(pid, 1.5)


def _stop_proxy_egress_unlocked(relay=None):
    relay = relay or load_relay_state()
    egress = (relay or {}).get("egress") or {}
    _stop_process(egress.get("pid"), RELAY_STOP_TIMEOUT_SEC)


def _stop_proxy_ingress_unlocked(relay=None):
    relay = relay or load_relay_state()
    ingress = (relay or {}).get("ingress") or relay or {}
    overlay_feed_pid = ingress.get("overlay_feed_pid") or relay.get("overlay_feed_pid")
    port = _listen_port_from_url(ingress.get("listen_url", "") or relay.get("listen_url", ""))
    _stop_process(ingress.get("pid") or relay.get("pid"), RELAY_STOP_TIMEOUT_SEC)
    _stop_process(overlay_feed_pid, 1.5)
    _wait_port_release(port, timeout_sec=RELAY_STOP_TIMEOUT_SEC)


def _stop_proxy_relay_unlocked():
    relay = load_relay_state()
    if not relay:
        clear_relay_state()
        return
    _stop_proxy_egress_unlocked(relay)
    _stop_proxy_ingress_unlocked(relay)
    save_relay_state({**relay, "running": False, "forwarding": False, "status": "stopped", "stopped_at": time.time()})


def _stop_proxy_relay():
    with _relay_lock():
        _stop_proxy_relay_unlocked()


def _start_overlay_feed(png_path):
    argv = [sys.executable, str(Path(__file__).resolve().parent.parent / "youtube_live.py"), "overlay-feed", "--png", png_path, "--interval", str(OVERLAY_FRAME_INTERVAL_SEC)]
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


def _await_egress_ready(proc, input_url, target_url, log_path):
    deadline = time.time() + RELAY_START_TIMEOUT_SEC
    while time.time() < deadline:
        exit_code = proc.poll()
        if exit_code is not None:
            detail = _tail_log_text(log_path)
            if detail:
                raise YouTubeLiveError(f"Proxy egress exited early: {detail.splitlines()[-1]}")
            raise YouTubeLiveError(f"Proxy egress exited early with code {exit_code}")
        if _pid_cmdline_contains(proc.pid, input_url, target_url or ""):
            return
        time.sleep(0.1)
    if not _pid_cmdline_contains(proc.pid, input_url, target_url or ""):
        raise YouTubeLiveError("Proxy egress failed to stay alive after launch")


def _overlay_signature(overlay):
    overlay = normalize_overlay_state(overlay or {})
    return (
        bool(overlay.get("enabled")),
        overlay.get("x"),
        overlay.get("y"),
        overlay.get("width"),
        overlay.get("height"),
        overlay.get("opacity"),
        overlay.get("png_path"),
    )


def _can_reuse_ingress(relay, *, listen_url, audio_mode, rotation, fps_mode, overlay):
    ingress = (relay or {}).get("ingress") or {}
    if not ingress.get("running"):
        return False
    return (
        ingress.get("listen_url") == listen_url
        and normalize_audio_mode(ingress.get("audio_mode")) == normalize_audio_mode(audio_mode)
        and normalize_rotation_mode(ingress.get("rotation")) == normalize_rotation_mode(rotation)
        and normalize_fps_mode(ingress.get("fps_mode")) == normalize_fps_mode(fps_mode)
        and _overlay_signature(ingress.get("overlay")) == _overlay_signature(overlay)
    )


def _start_proxy_ingress_unlocked(*, listen_url, stream_title, audio_mode=None, rotation=None, fps_mode=None, overlay=None):
    RELAY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_handle = RELAY_LOG_PATH.open("ab")
    audio_mode = normalize_audio_mode(audio_mode)
    rotation = normalize_rotation_mode(rotation)
    fps_mode = normalize_fps_mode(fps_mode)
    overlay = normalize_overlay_state(overlay or load_overlay_state())
    overlay_active = bool(overlay.get("png_path"))
    overlay_feed = None
    overlay_fd = None
    if overlay_active:
        ensure_overlay_png_exists(overlay)
        overlay_feed = _start_overlay_feed(overlay["png_path"])
        if overlay_feed.stdout is not None:
            overlay_fd = overlay_feed.stdout.fileno()
    video_pipeline = _proxy_video_pipeline_state(rotation, fps_mode, overlay)
    argv = _proxy_ingress_argv(listen_url=listen_url, audio_mode=audio_mode, rotation=rotation, fps_mode=fps_mode, overlay=overlay, overlay_fd=overlay_fd)
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
        _await_relay_ready(proc, listen_url, "", RELAY_LOG_PATH)
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
    ingress = decorate_stream_state(
        {
            "status": "running",
            "running": True,
            "pid": proc.pid,
            "listen_url": listen_url,
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
            "overlay_enabled": bool(overlay.get("enabled")),
            "overlay_feed_pid": overlay_feed.pid if overlay_feed else 0,
            "output_url": _proxy_internal_output_url(),
        },
        default_audio_mode=audio_mode,
        default_rotation=rotation,
        default_fps_mode=fps_mode,
    )
    if audio_mode != "normal":
        try:
            _apply_live_audio_mode(ingress, audio_mode)
        except YouTubeLiveError as exc:
            ingress["warning"] = f"Audio mode pending until relay is ready: {exc}"
            LOGGER.warning("Proxy relay started but initial live audio mode command failed: mode=%s error=%s", audio_mode, exc)
    log_handle.close()
    return ingress


def _start_proxy_egress_unlocked(*, target_url):
    RELAY_EGRESS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_handle = RELAY_EGRESS_LOG_PATH.open("ab")
    argv = _proxy_egress_argv(target_url=target_url)
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        log_handle.close()
        raise YouTubeLiveError(f"{FFMPEG_BIN} is not installed; proxy relay cannot start") from exc
    try:
        _await_egress_ready(proc, _proxy_internal_input_url(), target_url, RELAY_EGRESS_LOG_PATH)
    except Exception:
        try:
            proc.terminate()
        except OSError:
            pass
        log_handle.close()
        raise
    log_handle.close()
    return {
        "status": "running" if target_url else "standby",
        "running": True,
        "pid": proc.pid,
        "input_url": _proxy_internal_input_url(),
        "target_url": target_url,
        "log_path": str(RELAY_EGRESS_LOG_PATH),
        "started_at": time.time(),
        "forwarding": bool(target_url),
    }


def _start_proxy_relay_unlocked(*, listen_url, target_url, stream_title, audio_mode=None, rotation=None, fps_mode=None, overlay=None):
    current = load_relay_state()
    audio_mode = normalize_audio_mode(audio_mode)
    rotation = normalize_rotation_mode(rotation)
    fps_mode = normalize_fps_mode(fps_mode)
    overlay = normalize_overlay_state(overlay or load_overlay_state())
    if _can_reuse_ingress(current, listen_url=listen_url, audio_mode=audio_mode, rotation=rotation, fps_mode=fps_mode, overlay=overlay):
        ingress = (current.get("ingress") or {}).copy()
    else:
        _stop_proxy_ingress_unlocked(current)
        ingress = _start_proxy_ingress_unlocked(
            listen_url=listen_url,
            stream_title=stream_title,
            audio_mode=audio_mode,
            rotation=rotation,
            fps_mode=fps_mode,
            overlay=overlay,
        )
    _stop_proxy_egress_unlocked(current)
    egress = _start_proxy_egress_unlocked(target_url=target_url)
    relay = _compose_relay_state(
        ingress=ingress,
        egress=egress,
        stream_title=stream_title,
        audio_mode=audio_mode,
        rotation=rotation,
        fps_mode=fps_mode,
        overlay=overlay,
    )
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
    if not listen_url:
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
        relay["status"] = "running" if relay.get("forwarding") else "standby"
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
    if not listen_url:
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
    if not listen_url:
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
    if not listen_url:
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
        if not PROXY_ENABLED:
            return state
        if not state:
            state = decorate_stream_state(
                {
                    "mode": "proxy",
                    "title": "",
                    "proxy_publish_url": _proxy_publish_url(""),
                    "proxy_listen_url": _proxy_listen_url(),
                    "target_url": "",
                    "qr_payload": _proxy_publish_url(""),
                    "audio_mode": "normal",
                    "rotation": "0",
                    "fps_mode": "original",
                },
                default_audio_mode="normal",
                default_rotation="0",
                default_fps_mode="original",
            )
        if state.get("mode") != "proxy":
            return state
        listen_url = state.get("proxy_listen_url", "")
        target_url = state.get("target_url", "")
        if not listen_url:
            return state
        relay = state.get("relay") or {}
        ingress = relay.get("ingress") or {}
        egress = relay.get("egress") or {}
        if ingress.get("running") and egress.get("running"):
            return state
        LOGGER.warning(
            "Proxy relay watchdog repairing relay: ingress_pid=%s ingress_running=%s egress_pid=%s egress_running=%s listen_url=%s target_url=%s",
            ingress.get("pid") or relay.get("pid"),
            ingress.get("running") or relay.get("running"),
            egress.get("pid"),
            egress.get("running"),
            listen_url,
            target_url or "-",
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

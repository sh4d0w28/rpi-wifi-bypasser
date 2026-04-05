"""Stream creation workflow for YouTube live support."""

import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import CREATION_LOG_PATH, DEFAULT_PROXY_AUDIO_MODE, PROXY_ENABLED, STREAM_CREATE_LOCK_PATH, STREAM_PRIVACY_STATUS, STREAM_TITLE_PREFIX
from .errors import YouTubeLiveError
from .modes import normalize_audio_mode, normalize_fps_mode, normalize_rotation_mode
from .relay_runtime import build_publish_info, _relay_lock, _start_proxy_relay_unlocked
from .storage import load_creation_state, load_overlay_state, reset_creation_log, save_creation_state, save_stream_state, update_creation_state

LOGGER = logging.getLogger(__name__)


def normalize_privacy_status(value):
    privacy = str(value or "").strip().lower()
    if privacy in ("public", "private"):
        return privacy
    return STREAM_PRIVACY_STATUS


def default_stream_title():
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"{STREAM_TITLE_PREFIX} {stamp}"


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
    try:
        STREAM_CREATE_LOCK_PATH.unlink()
    except FileNotFoundError:
        pass


def creation_in_progress():
    state = load_creation_state()
    return state.get("status") == "creating"


def create_stream_bundle(*, api_request_fn, ap_ip="-", title=None, rotation=None, fps_mode=None, audio_mode=None, privacy_status=None):
    title = (title or "").strip() or default_stream_title()
    audio_mode = normalize_audio_mode(audio_mode)
    rotation = normalize_rotation_mode(rotation)
    fps_mode = normalize_fps_mode(fps_mode)
    privacy_status = normalize_privacy_status(privacy_status)
    LOGGER.info("YouTube stream creation request started: title=%s ap_ip=%s", title, ap_ip)
    LOGGER.info("Creating YouTube liveStream resource and waiting for API response")
    update_creation_state(fields={"status": "creating", "message": "Creating stream target", "progress_pct": 20, "stage": "stream"})
    stream = api_request_fn(
        "POST",
        "liveStreams",
        params={"part": "snippet,cdn,contentDetails,status"},
        body={
            "snippet": {"title": title},
            "cdn": {"frameRate": "variable", "ingestionType": "rtmp", "resolution": "variable"},
            "contentDetails": {"isReusable": True},
        },
    )
    stream_id = stream.get("id", "")
    ingestion_info = ((stream.get("cdn") or {}).get("ingestionInfo") or {})
    stream_name = ingestion_info.get("streamName", "")
    LOGGER.info("YouTube liveStream created: stream_id=%s", stream_id or "-")

    scheduled_start = (datetime.now(timezone.utc) + timedelta(minutes=2)).replace(microsecond=0).isoformat()
    LOGGER.info("Creating YouTube liveBroadcast resource and waiting for API response")
    update_creation_state(fields={"status": "creating", "message": "Creating broadcast", "progress_pct": 45, "stage": "broadcast"})
    broadcast = api_request_fn(
        "POST",
        "liveBroadcasts",
        params={"part": "snippet,status,contentDetails"},
        body={
            "snippet": {"title": title, "scheduledStartTime": scheduled_start},
            "status": {"privacyStatus": privacy_status, "selfDeclaredMadeForKids": False},
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
    update_creation_state(fields={"status": "creating", "message": "Binding stream", "progress_pct": 75, "stage": "bind"})
    api_request_fn("POST", "liveBroadcasts/bind", params={"part": "id,contentDetails", "id": broadcast_id, "streamId": stream_id})

    publish = build_publish_info(stream_name, ingestion_info, ap_ip)
    state = {
        "created_at": time.time(),
        "title": title,
        "broadcast_id": broadcast_id,
        "watch_url": f"https://www.youtube.com/watch?v={broadcast_id}" if broadcast_id else "",
        "stream_id": stream_id,
        "stream_name": stream_name,
        "ingestion_address": ingestion_info.get("ingestionAddress", ""),
        "rtmps_ingestion_address": ingestion_info.get("rtmpsIngestionAddress", ""),
        "privacy_status": privacy_status,
        "ap_ip": ap_ip,
        "audio_mode": audio_mode if PROXY_ENABLED else "normal",
        "rotation": rotation,
        "fps_mode": fps_mode,
        **publish,
    }
    with _relay_lock():
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
    LOGGER.info("YouTube stream bundle ready: title=%s broadcast_id=%s mode=%s", state.get("title", ""), state.get("broadcast_id", ""), state.get("mode", ""))
    return state


def run_creation_job(*, api_request_fn, ap_ip, title, rotation, fps_mode, audio_mode, privacy_status):
    try:
        update_creation_state(fields={"pid": os.getpid(), "status": "creating"})
        LOGGER.info(
            "YouTube async creation job started: ap_ip=%s title=%s audio_mode=%s rotation=%s fps_mode=%s",
            ap_ip,
            title or "",
            audio_mode,
            rotation,
            fps_mode,
        )
        state = create_stream_bundle(
            api_request_fn=api_request_fn,
            ap_ip=ap_ip,
            title=title,
            rotation=rotation,
            fps_mode=fps_mode,
            audio_mode=audio_mode,
            privacy_status=privacy_status,
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


def start_stream_creation(*, validate_live_access_fn, ap_ip="-", title=None, rotation=None, fps_mode=None, audio_mode=None, privacy_status=None):
    if creation_in_progress():
        LOGGER.warning("Rejected YouTube stream creation request because one is already in progress")
        raise YouTubeLiveError("Stream creation already in progress")
    validation = validate_live_access_fn()
    if not validation.get("ok"):
        message = validation.get("message") or "YouTube Live validation failed"
        LOGGER.warning("Rejected YouTube stream creation request because validation failed: %s", message)
        raise YouTubeLiveError(message)
    audio_mode = normalize_audio_mode(audio_mode)
    rotation = normalize_rotation_mode(rotation)
    fps_mode = normalize_fps_mode(fps_mode)
    privacy_status = normalize_privacy_status(privacy_status)
    fd = _lock_creation()
    try:
        if creation_in_progress():
            LOGGER.warning("Rejected YouTube stream creation request because one is already in progress")
            raise YouTubeLiveError("Stream creation already in progress")
        reset_creation_log(ap_ip=ap_ip, title=title or "", rotation=rotation, fps_mode=fps_mode, audio_mode=audio_mode)
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
                "privacy_status": privacy_status,
                "log_path": str(CREATION_LOG_PATH),
            }
        )
        LOGGER.info(
            "Starting background YouTube stream creation process: ap_ip=%s title=%s audio_mode=%s rotation=%s fps_mode=%s privacy=%s",
            ap_ip,
            title or "",
            audio_mode,
            rotation,
            fps_mode,
            privacy_status,
        )
        argv = [sys.executable, str(Path(__file__).resolve().parent.parent / "youtube_live.py"), "create", "--ap-ip", ap_ip or "-"]
        if title:
            argv.extend(["--title", title])
        if audio_mode != DEFAULT_PROXY_AUDIO_MODE:
            argv.extend(["--audio-mode", audio_mode])
        if rotation != "0":
            argv.extend(["--rotation", rotation])
        if fps_mode != "original":
            argv.extend(["--fps-mode", fps_mode])
        if privacy_status != STREAM_PRIVACY_STATUS:
            argv.extend(["--privacy-status", privacy_status])
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
        update_creation_state(fields={"pid": proc.pid})
    finally:
        _unlock_creation(fd)

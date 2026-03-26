#!/usr/bin/env python3
import base64
import io
import json
import logging
import os
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
STREAM_TITLE_PREFIX = os.environ.get("YOUTUBE_STREAM_TITLE_PREFIX", "RPi Live").strip() or "RPi Live"
STREAM_PRIVACY_STATUS = os.environ.get("YOUTUBE_STREAM_PRIVACY_STATUS", "unlisted").strip() or "unlisted"
PROXY_PUBLISH_URL_TEMPLATE = os.environ.get("YOUTUBE_PROXY_PUBLISH_URL", "").strip()
DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
LOGGER = logging.getLogger(__name__)


class YouTubeLiveError(RuntimeError):
    pass


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
    return _load_json(STREAM_STATE_PATH, {})


def save_stream_state(state):
    _save_json(STREAM_STATE_PATH, state)


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


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


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
                "broadcastType": "all",
                "mine": "true",
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


def _build_publish_info(stream_name, ingestion_info, ap_ip):
    rtmps_base = ingestion_info.get("rtmpsIngestionAddress") or ingestion_info.get("ingestionAddress") or ""
    target_url = f"{rtmps_base.rstrip('/')}/{stream_name}" if rtmps_base and stream_name else ""
    proxy_publish_url = ""
    qr_payload = target_url
    mode = "direct"
    if PROXY_PUBLISH_URL_TEMPLATE:
        proxy_publish_url = PROXY_PUBLISH_URL_TEMPLATE.format(ap_ip=ap_ip or "")
        qr_payload = proxy_publish_url
        mode = "proxy"
    return {
        "mode": mode,
        "qr_payload": qr_payload,
        "proxy_publish_url": proxy_publish_url,
        "target_url": target_url,
    }


def create_stream_bundle(*, ap_ip="-", title=None):
    title = (title or "").strip() or _default_stream_title()
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
        **publish,
    }
    save_stream_state(state)
    LOGGER.info(
        "YouTube stream bundle ready: title=%s broadcast_id=%s mode=%s",
        state.get("title", ""),
        state.get("broadcast_id", ""),
        state.get("mode", ""),
    )
    return state


def _run_creation_job(ap_ip, title):
    try:
        update_creation_state(pid=os.getpid(), status="creating")
        LOGGER.info("YouTube async creation job started: ap_ip=%s title=%s", ap_ip, title or "")
        state = create_stream_bundle(ap_ip=ap_ip, title=title)
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
            }
        )
        LOGGER.exception("YouTube async creation job failed: %s", exc)
        raise


def start_stream_creation(*, ap_ip="-", title=None):
    if creation_in_progress():
        LOGGER.warning("Rejected YouTube stream creation request because one is already in progress")
        raise YouTubeLiveError("Stream creation already in progress")
    validation = validate_live_access()
    if not validation.get("ok"):
        message = validation.get("message") or "YouTube Live validation failed"
        LOGGER.warning("Rejected YouTube stream creation request because validation failed: %s", message)
        raise YouTubeLiveError(message)
    fd = _lock_creation()
    try:
        if creation_in_progress():
            LOGGER.warning("Rejected YouTube stream creation request because one is already in progress")
            raise YouTubeLiveError("Stream creation already in progress")
        save_creation_state(
            {
                "status": "creating",
                "message": "Stream is creating",
                "progress_pct": 5,
                "stage": "queued",
                "started_at": time.time(),
                "ap_ip": ap_ip,
                "title": title or "",
            }
        )
        LOGGER.info("Starting background YouTube stream creation process: ap_ip=%s title=%s", ap_ip, title or "")
        argv = [sys.executable, str(Path(__file__).resolve()), "create", "--ap-ip", ap_ip or "-"]
        if title:
            argv.extend(["--title", title])
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
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
        idx += 1
    return ap_ip, title


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "create":
        logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
        cli_ap_ip, cli_title = _parse_cli_args(sys.argv[2:])
        _run_creation_job(cli_ap_ip, cli_title)

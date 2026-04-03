"""Authorization and token-management services for YouTube live support."""

import time

from .config import CLIENT_CONFIG_PATH, DEVICE_CODE_URL, TOKEN_URL, YOUTUBE_SCOPE
from .errors import YouTubeLiveError
from .youtube_api import api_request, http_form


def client_ready(*, load_client_config_fn):
    return bool(load_client_config_fn().get("client_id"))


def authorization_ready(*, load_token_fn):
    token = load_token_fn()
    return bool(token.get("refresh_token") or token.get("access_token"))


def validate_live_access(*, authorization_ready_fn, api_request_fn):
    if not authorization_ready_fn():
        return {
            "ok": False,
            "code": "not_authorized",
            "message": "YouTube is not authorized yet",
        }

    try:
        payload = api_request_fn(
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


def get_auth_status(
    *,
    load_token_fn,
    load_device_state_fn,
    client_ready_fn,
    authorization_ready_fn,
    validate_live_access_fn,
    load_creation_state_fn,
):
    token = load_token_fn()
    device = load_device_state_fn()
    validation = {
        "ok": False,
        "code": "not_checked",
        "message": "Authorization has not been verified yet",
    }
    if authorization_ready_fn():
        validation = validate_live_access_fn()
    return {
        "client_configured": client_ready_fn(),
        "authorized": authorization_ready_fn(),
        "device_pending": bool(device.get("device_code")),
        "device": device,
        "token": {
            "has_refresh_token": bool(token.get("refresh_token")),
            "expires_at": token.get("expires_at"),
        },
        "validation": validation,
        "creation": load_creation_state_fn(),
    }


def start_device_authorization(*, load_client_config_fn, save_device_state_fn):
    config = load_client_config_fn()
    if not config.get("client_id"):
        raise YouTubeLiveError(f"Missing YouTube OAuth client config at {CLIENT_CONFIG_PATH}")
    payload = http_form(
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
    save_device_state_fn(state)
    return state


def poll_device_authorization(
    *,
    load_client_config_fn,
    load_device_state_fn,
    clear_device_state_fn,
    load_token_fn,
    save_token_fn,
):
    config = load_client_config_fn()
    state = load_device_state_fn()
    if not config.get("client_id"):
        raise YouTubeLiveError(f"Missing YouTube OAuth client config at {CLIENT_CONFIG_PATH}")
    if not state.get("device_code"):
        raise YouTubeLiveError("No device authorization is pending")
    if state.get("expires_at", 0) <= time.time():
        clear_device_state_fn()
        raise YouTubeLiveError("Device authorization code expired")

    fields = {
        "client_id": config["client_id"],
        "device_code": state["device_code"],
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }
    if config.get("client_secret"):
        fields["client_secret"] = config["client_secret"]
    try:
        payload = http_form(TOKEN_URL, fields)
    except YouTubeLiveError as exc:
        message = str(exc)
        if any(code in message for code in ("authorization_pending", "slow_down")):
            raise
        clear_device_state_fn()
        raise

    token = {
        "access_token": payload.get("access_token", ""),
        "refresh_token": payload.get("refresh_token", load_token_fn().get("refresh_token", "")),
        "scope": payload.get("scope", YOUTUBE_SCOPE),
        "token_type": payload.get("token_type", "Bearer"),
        "expires_at": time.time() + int(payload.get("expires_in", 3600)) - 60,
    }
    save_token_fn(token)
    clear_device_state_fn()
    return token


def refresh_access_token(*, token, load_client_config_fn, save_token_fn):
    config = load_client_config_fn()
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
    payload = http_form(TOKEN_URL, fields)
    token["access_token"] = payload.get("access_token", "")
    token["token_type"] = payload.get("token_type", "Bearer")
    token["expires_at"] = time.time() + int(payload.get("expires_in", 3600)) - 60
    save_token_fn(token)
    return token


def ensure_access_token(*, load_token_fn, refresh_access_token_fn):
    token = load_token_fn()
    if not token:
        raise YouTubeLiveError("YouTube is not authorized yet")
    if token.get("access_token") and token.get("expires_at", 0) > time.time():
        return token["access_token"]
    token = refresh_access_token_fn(token)
    if not token.get("access_token"):
        raise YouTubeLiveError("Failed to refresh YouTube access token")
    return token["access_token"]


def make_api_request(*, ensure_access_token_fn):
    def _request(method, path, *, params=None, body=None):
        return api_request(
            method,
            path,
            ensure_access_token_fn=ensure_access_token_fn,
            params=params,
            body=body,
        )

    return _request


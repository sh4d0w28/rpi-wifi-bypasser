"""Low-level HTTP and YouTube Data API helpers."""

import json
import urllib.error
import urllib.parse
import urllib.request

from .config import YOUTUBE_API_BASE
from .errors import YouTubeLiveError


def http_json(url, *, method="GET", headers=None, data=None):
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


def http_form(url, fields):
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


def api_request(method, path, *, ensure_access_token_fn, params=None, body=None):
    access_token = ensure_access_token_fn()
    query = urllib.parse.urlencode(params or {})
    url = f"{YOUTUBE_API_BASE}/{path}"
    if query:
        url = f"{url}?{query}"
    return http_json(
        url,
        method=method,
        headers={"Authorization": f"Bearer {access_token}"},
        data=body,
    )


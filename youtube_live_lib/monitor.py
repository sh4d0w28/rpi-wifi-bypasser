"""YouTube stream monitoring helpers."""

from .auth import _api_request, authorization_ready
from .errors import YouTubeLiveError
from .relay import load_stream_state


def get_stream_monitor_status():
    state = load_stream_state()
    if not state:
        return {
            "ok": False,
            "code": "no_stream",
            "summary": "NO STREAM",
            "message": "No YouTube stream has been created yet.",
            "issues": [],
        }

    issues = []
    payload = {
        "ok": True,
        "code": "unknown",
        "summary": "UNKNOWN",
        "message": "",
        "issues": issues,
        "broadcast_id": state.get("broadcast_id", ""),
        "stream_id": state.get("stream_id", ""),
        "watch_url": state.get("watch_url", ""),
        "broadcast": {},
        "stream": {},
    }

    if not authorization_ready():
        payload.update(
            {
                "ok": False,
                "code": "not_authorized",
                "summary": "AUTH REQUIRED",
                "message": "YouTube is not authorized.",
            }
        )
        return payload

    try:
        broadcast_id = state.get("broadcast_id", "")
        if broadcast_id:
            response = _api_request(
                "GET",
                "liveBroadcasts",
                params={"part": "id,snippet,status,contentDetails", "id": broadcast_id},
            )
            items = response.get("items", [])
            if items:
                item = items[0]
                status = item.get("status") or {}
                snippet = item.get("snippet") or {}
                life_cycle = (status.get("lifeCycleStatus") or "").strip()
                payload["broadcast"] = {
                    "id": item.get("id", ""),
                    "life_cycle_status": life_cycle,
                    "privacy_status": status.get("privacyStatus", ""),
                    "recording_status": status.get("recordingStatus", ""),
                    "made_for_kids": status.get("selfDeclaredMadeForKids"),
                    "scheduled_start_time": snippet.get("scheduledStartTime", ""),
                    "actual_start_time": snippet.get("actualStartTime", ""),
                    "actual_end_time": snippet.get("actualEndTime", ""),
                }
            else:
                issues.append("Broadcast not found on YouTube.")
                payload["ok"] = False
                payload["code"] = "broadcast_missing"
                payload["summary"] = "BROADCAST MISSING"
                payload["message"] = "The saved YouTube broadcast no longer exists."

        stream_id = state.get("stream_id", "")
        if stream_id:
            response = _api_request(
                "GET",
                "liveStreams",
                params={"part": "id,status,cdn", "id": stream_id},
            )
            items = response.get("items", [])
            if items:
                item = items[0]
                status = item.get("status") or {}
                health_status = status.get("healthStatus") or {}
                configuration_issues = health_status.get("configurationIssues") or []
                issue_messages = []
                for issue in configuration_issues:
                    if not isinstance(issue, dict):
                        continue
                    issue_messages.append(
                        (issue.get("description") or issue.get("reason") or issue.get("type") or "Unknown stream issue").strip()
                    )
                payload["stream"] = {
                    "id": item.get("id", ""),
                    "stream_status": (status.get("streamStatus") or "").strip(),
                    "health_status": (health_status.get("status") or "").strip(),
                    "issues": issue_messages,
                }
                issues.extend(issue_messages)
            else:
                issues.append("Stream target not found on YouTube.")
                payload["ok"] = False
                payload["code"] = "stream_missing"
                payload["summary"] = "STREAM MISSING"
                payload["message"] = "The saved YouTube stream target no longer exists."
    except YouTubeLiveError as exc:
        payload.update(
            {
                "ok": False,
                "code": "api_error",
                "summary": "YOUTUBE ERROR",
                "message": str(exc),
            }
        )
        return payload

    if payload["code"] in ("broadcast_missing", "stream_missing"):
        return payload

    life_cycle = (payload["broadcast"].get("life_cycle_status") or "").lower()
    stream_status = (payload["stream"].get("stream_status") or "").lower()
    health_status = (payload["stream"].get("health_status") or "").lower()

    if life_cycle in ("complete", "complete_starting", "revoked"):
        payload.update(
            {
                "ok": False,
                "code": "broadcast_finished",
                "summary": "FINISHED",
                "message": "YouTube broadcast has already finished. Create a new stream.",
            }
        )
    elif life_cycle == "live":
        payload.update(
            {
                "ok": True,
                "code": "live",
                "summary": "LIVE" if stream_status == "active" else "LIVE WAITING",
                "message": "Broadcast is live on YouTube." if stream_status == "active" else "Broadcast is live but stream input is not active.",
            }
        )
    elif life_cycle in ("ready", "teststarting", "testing", "created"):
        payload.update(
            {
                "ok": stream_status in ("active", "created", "ready"),
                "code": "ready",
                "summary": "READY",
                "message": "Broadcast is ready on YouTube." if stream_status != "inactive" else "Broadcast exists, but YouTube is not receiving an active stream.",
            }
        )
    elif not life_cycle and stream_status == "active":
        payload.update(
            {
                "ok": True,
                "code": "stream_active",
                "summary": "STREAM ACTIVE",
                "message": "YouTube stream target is active.",
            }
        )
    else:
        payload.update(
            {
                "ok": False if stream_status == "inactive" else payload["ok"],
                "code": "inactive" if stream_status == "inactive" else "unknown",
                "summary": "INACTIVE" if stream_status == "inactive" else "UNKNOWN",
                "message": "YouTube is not receiving an active stream." if stream_status == "inactive" else "Unable to determine YouTube stream state.",
            }
        )

    if health_status and health_status not in ("good", "ok"):
        issues.append(f"YouTube stream health: {health_status}.")
        payload["ok"] = False
        if payload["code"] in ("live", "ready", "stream_active"):
            payload["summary"] = "HEALTH WARN"

    relay = state.get("relay") or {}
    if relay.get("warning"):
        issues.append(relay.get("warning"))

    return payload

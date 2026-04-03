from flask import Blueprint, Response, flash, redirect, render_template, request, url_for

from youtube_live import YouTubeLiveError

from rpi_ap_tools.web import state
from rpi_ap_tools.web.services.overlay_render_service import load_runtime_status
from rpi_ap_tools.web.services.wifi_service import get_ip

bp = Blueprint("youtube", __name__)


@bp.get("/stream", endpoint="stream_page")
def stream_page():
    return render_template("index.html", current_page="stream", **state.index_context())


@bp.post("/youtube/device/start")
def youtube_device_start():
    try:
        auth_state = state.start_device_authorization()
        flash(
            f"Open {auth_state.get('verification_url_complete') or auth_state.get('verification_url')}, then enter code {auth_state.get('user_code')}.",
            "success",
        )
    except YouTubeLiveError as exc:
        flash(str(exc), "error")
    return redirect(url_for("youtube.stream_page"))


@bp.post("/youtube/device/poll")
def youtube_device_poll():
    try:
        state.poll_device_authorization()
        flash("YouTube authorization completed.", "success")
    except YouTubeLiveError as exc:
        flash(str(exc), "error")
    return redirect(url_for("youtube.stream_page"))


@bp.post("/youtube/create")
def youtube_create():
    title = request.form.get("title", "").strip()
    audio_mode = request.form.get("audio_mode", "normal").strip()
    rotation = request.form.get("rotation", "0").strip()
    fps_mode = request.form.get("fps_mode", "original").strip()
    ap_ip = get_ip("wlan0").split("/", 1)[0]
    runtime = load_runtime_status()
    probe = runtime.get("probe", {}) if isinstance(runtime, dict) else {}
    auth = state.get_auth_status()
    if probe.get("auth_required") or not auth.get("authorized"):
        flash("AUTH FIRST", "error")
        return redirect(url_for("youtube.stream_page"))
    try:
        state.start_stream_creation(ap_ip=ap_ip, title=title, audio_mode=audio_mode, rotation=rotation, fps_mode=fps_mode)
        flash("YouTube stream creation started.", "success")
    except YouTubeLiveError as exc:
        flash(str(exc), "error")
    return redirect(url_for("youtube.stream_page"))


@bp.post("/youtube/audio-mode")
def youtube_audio_mode():
    mode = request.form.get("mode", "").strip()
    try:
        relay_state = state.set_proxy_audio_mode(mode)
        flash(f"YouTube relay audio mode: {relay_state.get('audio_mode_label', 'Normal')}", "success")
    except YouTubeLiveError as exc:
        flash(str(exc), "error")
    return redirect(url_for("youtube.stream_page"))


@bp.post("/youtube/rotation")
def youtube_rotation_mode():
    mode = request.form.get("mode", "").strip()
    try:
        relay_state = state.set_proxy_rotation_mode(mode)
        flash(f"YouTube relay rotation: {relay_state.get('rotation_label', 'Off')} (relay reconnects briefly).", "success")
    except YouTubeLiveError as exc:
        flash(str(exc), "error")
    return redirect(url_for("youtube.stream_page"))


@bp.post("/youtube/fps-mode")
def youtube_fps_mode():
    mode = request.form.get("mode", "").strip()
    try:
        relay_state = state.set_proxy_fps_mode(mode)
        flash(f"YouTube relay FPS: {relay_state.get('fps_mode_label', 'Original')} (relay reconnects briefly).", "success")
    except YouTubeLiveError as exc:
        flash(str(exc), "error")
    return redirect(url_for("youtube.stream_page"))


@bp.get("/youtube/creation-log")
def youtube_creation_log():
    payload = state.load_creation_log(max_bytes=None)
    text = payload.get("text", "")
    if not text:
        text = f"No creation log yet.\nExpected path: {payload.get('path', '-')}\n"
    return Response(text, mimetype="text/plain")

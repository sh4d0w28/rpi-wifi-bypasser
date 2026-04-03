from pathlib import Path

from flask import Blueprint, Response, flash, jsonify, redirect, render_template, request, send_file, url_for

from rpi_ap_tools.web import state
from rpi_ap_tools.web.services.portal_browser_service import (
    CAPTIVE_PORTAL_REMOTE_IMAGE_PATH,
    click_portal_browser,
    load_portal_browser_status,
    press_portal_browser_key,
    reload_portal_browser,
    scroll_portal_browser,
    start_portal_browser,
    stop_portal_browser,
    type_portal_browser,
)
from rpi_ap_tools.web.services.update_service import load_portal_preview, run_portal_ack, run_portal_capture, run_portal_preview, start_update_service

bp = Blueprint("system", __name__)


@bp.get("/", endpoint="index")
def index():
    return redirect(url_for("system.wifi_page"))


@bp.get("/wifi", endpoint="wifi_page")
def wifi_page():
    return render_template("index.html", current_page="wifi", **state.index_context())


@bp.post("/portal-ack")
def portal_ack():
    ok, msg = run_portal_ack()
    flash(msg, "success" if ok else "error")
    return redirect(url_for("system.wifi_page"))


@bp.post("/portal-capture")
def portal_capture():
    ok, msg = run_portal_capture()
    flash(msg, "success" if ok else "error")
    return redirect(url_for("system.wifi_page"))


@bp.post("/portal-preview")
def portal_preview():
    ok, msg = run_portal_preview()
    flash(msg, "success" if ok else "error")
    return redirect(url_for("system.wifi_page"))


@bp.get("/portal-preview/image")
def portal_preview_image():
    preview = load_portal_preview()
    path = Path(preview.get("image_path") or "")
    if not path.is_file():
        return Response("No portal preview image captured yet.\n", mimetype="text/plain", status=404)
    return send_file(path, mimetype="image/png", max_age=0)


@bp.get("/portal-remote/image")
def portal_remote_image():
    if not CAPTIVE_PORTAL_REMOTE_IMAGE_PATH.is_file():
        return Response("No portal remote image captured yet.\n", mimetype="text/plain", status=404)
    return send_file(CAPTIVE_PORTAL_REMOTE_IMAGE_PATH, mimetype="image/png", max_age=0)


@bp.get("/portal-remote/status")
def portal_remote_status():
    return jsonify(load_portal_browser_status())


@bp.post("/portal-remote/start")
def portal_remote_start():
    result = start_portal_browser()
    return jsonify(result)


@bp.post("/portal-remote/reload")
def portal_remote_reload():
    result = reload_portal_browser()
    return jsonify(result)


@bp.post("/portal-remote/stop")
def portal_remote_stop():
    result = stop_portal_browser()
    return jsonify(result)


@bp.post("/portal-remote/click")
def portal_remote_click():
    payload = request.get_json(silent=True) or request.form
    result = click_portal_browser(payload.get("x_ratio", 0.5), payload.get("y_ratio", 0.5))
    return jsonify(result)


@bp.post("/portal-remote/type")
def portal_remote_type():
    payload = request.get_json(silent=True) or request.form
    result = type_portal_browser(payload.get("text", ""))
    return jsonify(result)


@bp.post("/portal-remote/key")
def portal_remote_key():
    payload = request.get_json(silent=True) or request.form
    result = press_portal_browser_key(payload.get("key", ""))
    return jsonify(result)


@bp.post("/portal-remote/scroll")
def portal_remote_scroll():
    payload = request.get_json(silent=True) or request.form
    result = scroll_portal_browser(payload.get("delta_y", 0))
    return jsonify(result)


@bp.post("/update")
def update():
    ok, msg = start_update_service(request.form.get("git_ref", ""))
    flash(msg, "success" if ok else "error")
    return redirect(url_for("system.wifi_page"))

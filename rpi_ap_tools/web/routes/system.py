from pathlib import Path

from flask import Blueprint, Response, flash, redirect, render_template, request, send_file, url_for

from rpi_ap_tools.web import state
from rpi_ap_tools.web.services.update_service import load_portal_preview, run_portal_ack, run_portal_preview, start_update_service

bp = Blueprint("system", __name__)


@bp.get("/", endpoint="index")
def index():
    page = (request.args.get("tab") or "").strip().lower()
    if page in ("youtube", "stream"):
        return redirect(url_for("youtube.stream_page"))
    if page == "overlay":
        return redirect(url_for("overlay.overlay_page"))
    return redirect(url_for("system.wifi_page"))


@bp.get("/wifi", endpoint="wifi_page")
def wifi_page():
    return render_template("index.html", current_page="wifi", **state.index_context())


@bp.post("/portal-ack")
def portal_ack():
    ok, msg = run_portal_ack()
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


@bp.post("/update")
def update():
    ok, msg = start_update_service(request.form.get("git_ref", ""))
    flash(msg, "success" if ok else "error")
    return redirect(url_for("system.wifi_page"))

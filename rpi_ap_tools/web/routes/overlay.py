from flask import Blueprint, flash, redirect, render_template, request, url_for

from youtube_live import YouTubeLiveError

from rpi_ap_tools.web import state
from rpi_ap_tools.web.services.overlay_render_service import load_overlay_html, overlay_preview_response, render_overlay_png, save_overlay_html

bp = Blueprint("overlay", __name__)


@bp.get("/overlay", endpoint="overlay_page")
def overlay_page():
    return render_template("index.html", current_page="overlay", **state.index_context())


@bp.post("/overlay/save")
def overlay_save():
    current = state.load_overlay_state()
    previous_enabled = bool(current.get("enabled"))
    previous_structural = {key: current.get(key) for key in ("x", "y", "width", "height", "opacity")}
    updated = {
        **current,
        "enabled": request.form.get("enabled") == "on",
        "x": request.form.get("x", current.get("x")),
        "y": request.form.get("y", current.get("y")),
        "width": request.form.get("width", current.get("width")),
        "height": request.form.get("height", current.get("height")),
        "opacity": request.form.get("opacity", current.get("opacity")),
        "refresh_sec": request.form.get("refresh_sec", current.get("refresh_sec")),
    }
    state.save_overlay_state(updated)
    save_overlay_html(request.form.get("html", load_overlay_html()))
    ok, message = render_overlay_png(force=True)
    flash(message, "success" if ok else "error")
    new_state = state.load_overlay_state()
    new_structural = {key: new_state.get(key) for key in ("x", "y", "width", "height", "opacity")}
    if previous_structural != new_structural:
        try:
            state.refresh_proxy_overlay()
            flash("Running relay reloaded with new overlay layout.", "success")
        except YouTubeLiveError:
            pass
    elif ok and previous_enabled != bool(new_state.get("enabled")):
        flash("Overlay visibility updated without restarting the relay.", "success")
    return redirect(url_for("overlay.overlay_page"))


@bp.post("/overlay/render")
def overlay_render():
    ok, message = render_overlay_png(force=True)
    flash(message, "success" if ok else "error")
    return redirect(url_for("overlay.overlay_page"))


@bp.get("/overlay/preview")
def overlay_preview():
    return overlay_preview_response()

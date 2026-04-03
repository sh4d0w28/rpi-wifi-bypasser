from flask import Blueprint, flash, redirect, request, url_for

from youtube_live import YouTubeLiveError

from rpi_ap_tools.web import state

bp = Blueprint("overlay", __name__)


@bp.post("/overlay/save")
def overlay_save():
    current = state.load_overlay_state()
    previous_structural = {key: current.get(key) for key in ("enabled", "x", "y", "width", "height", "opacity")}
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
    state.save_overlay_html(request.form.get("html", state.load_overlay_html()))
    ok, message = state.render_overlay_png(force=True)
    flash(message, "success" if ok else "error")
    new_state = state.load_overlay_state()
    new_structural = {key: new_state.get(key) for key in ("enabled", "x", "y", "width", "height", "opacity")}
    if previous_structural != new_structural:
        try:
            state.refresh_proxy_overlay()
            flash("Running relay reloaded with new overlay layout.", "success")
        except YouTubeLiveError:
            pass
    return redirect(url_for("index", tab="overlay"))


@bp.post("/overlay/render")
def overlay_render():
    ok, message = state.render_overlay_png(force=True)
    flash(message, "success" if ok else "error")
    return redirect(url_for("index", tab="overlay"))


@bp.get("/overlay/preview")
def overlay_preview():
    return state.overlay_preview_response()


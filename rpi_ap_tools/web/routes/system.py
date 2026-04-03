from flask import Blueprint, flash, redirect, render_template, url_for

from rpi_ap_tools.web import state
from rpi_ap_tools.web.services.update_service import run_portal_ack, start_update_service

bp = Blueprint("system", __name__)


@bp.get("/", endpoint="index")
def index():
    return render_template("index.html", **state.index_context())


@bp.post("/portal-ack")
def portal_ack():
    ok, msg = run_portal_ack()
    flash(msg, "success" if ok else "error")
    return redirect(url_for("index"))


@bp.post("/update")
def update():
    ok, msg = start_update_service()
    flash(msg, "success" if ok else "error")
    return redirect(url_for("index"))

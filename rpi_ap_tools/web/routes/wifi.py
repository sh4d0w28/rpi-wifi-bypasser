from flask import Blueprint, flash, redirect, request, url_for

from rpi_ap_tools.core.process import run_command
from rpi_ap_tools.web import state

bp = Blueprint("wifi", __name__)


@bp.post("/connect")
def connect():
    ssid = request.form.get("ssid", "").strip()
    password = request.form.get("password", "")
    auth_type = request.form.get("auth_type", "wpa-psk").strip()
    if not ssid:
        flash("SSID is required", "error")
        return redirect(url_for("index"))
    saved = state.get_saved_wifi(ssid)
    if auth_type != "open" and not password and saved.get("password"):
        password = saved["password"]
    if auth_type != "open" and not password:
        flash("Password is required for secured Wi-Fi", "error")
        return redirect(url_for("index"))
    ok, msg = state.connect_wifi(ssid, password, auth_type)
    if ok:
        state.save_wifi_credentials(ssid, "" if auth_type == "open" else password, auth_type)
    flash(msg, "success" if ok else "error")
    return redirect(url_for("index"))


@bp.post("/disconnect")
def disconnect():
    active = state.get_active_connection()
    if active["name"]:
        proc = run_command(["nmcli", "connection", "down", active["name"]], check=False)
        flash(proc.stdout.strip() or proc.stderr.strip() or "Disconnected", "success" if proc.returncode == 0 else "error")
    else:
        flash("No active wlan1 connection", "error")
    return redirect(url_for("index"))


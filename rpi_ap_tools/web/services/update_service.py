import os
import subprocess
from pathlib import Path

from rpi_ap_tools.core.process import run_command

CAPTIVE_PORTAL_ACK_CMD = os.environ.get("CAPTIVE_PORTAL_ACK_CMD", "").strip()
UPDATE_SERVICE_NAME = os.environ.get("UPDATE_SERVICE_NAME", "rpi-ap-update.service").strip() or "rpi-ap-update.service"
UPDATE_SCRIPT_PATH = Path("/home/pi/update_ap.sh")


def run_portal_ack():
    if not CAPTIVE_PORTAL_ACK_CMD:
        return False, "No captive portal action configured"
    try:
        proc = subprocess.run(CAPTIVE_PORTAL_ACK_CMD, text=True, capture_output=True, shell=True, check=False, timeout=20)
        message = proc.stdout.strip() or proc.stderr.strip() or "Portal action finished"
        return proc.returncode == 0, message
    except subprocess.TimeoutExpired:
        return False, "Portal action timed out"
    except OSError as exc:
        return False, str(exc)


def systemd_show(unit_name, properties):
    proc = run_command(["systemctl", "show", unit_name, f"--property={','.join(properties)}"], check=False)
    if proc.returncode != 0:
        return {}
    data = {}
    for line in proc.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value
    return data


def load_update_status():
    props = systemd_show(UPDATE_SERVICE_NAME, ["LoadState", "ActiveState", "SubState", "Result", "ExecMainStatus", "ExecMainStartTimestamp"])
    script_exists = UPDATE_SCRIPT_PATH.is_file()
    if not props:
        return {"service_name": UPDATE_SERVICE_NAME, "script_path": str(UPDATE_SCRIPT_PATH), "script_exists": script_exists, "service_installed": False, "running": False, "load_state": "unknown", "active_state": "unknown", "sub_state": "unknown", "summary": "update service unavailable", "status_class": "err", "last_started": "", "can_start": False}
    load_state = props.get("LoadState", "unknown")
    active_state = props.get("ActiveState", "unknown")
    result = props.get("Result", "")
    exec_main_status = props.get("ExecMainStatus", "")
    last_started = props.get("ExecMainStartTimestamp", "")
    running = active_state in ("active", "activating", "reloading")
    service_installed = load_state not in ("not-found", "unknown", "")
    if not service_installed:
        summary, status_class = "update service not installed", "err"
    elif running:
        summary, status_class = "update is running", ""
    elif last_started and result and result != "success":
        detail = f" ({result}"
        if exec_main_status and exec_main_status != "0":
            detail += f", exit {exec_main_status}"
        detail += ")"
        summary, status_class = f"last run failed{detail}", "err"
    elif last_started:
        summary, status_class = "last run succeeded", "ok"
    else:
        summary, status_class = "idle", ""
    return {"service_name": UPDATE_SERVICE_NAME, "script_path": str(UPDATE_SCRIPT_PATH), "script_exists": script_exists, "service_installed": service_installed, "running": running, "load_state": load_state, "active_state": active_state, "sub_state": props.get("SubState", "unknown"), "summary": summary, "status_class": status_class, "last_started": last_started, "can_start": service_installed and script_exists and not running}


def start_update_service():
    status = load_update_status()
    if not status["service_installed"]:
        return False, f"{UPDATE_SERVICE_NAME} is not installed"
    if not status["script_exists"]:
        return False, f"Update script not found: {UPDATE_SCRIPT_PATH}"
    if status["running"]:
        return False, "Update already running"
    proc = run_command(["systemctl", "start", "--no-block", UPDATE_SERVICE_NAME], check=False)
    if proc.returncode != 0:
        return False, proc.stderr.strip() or proc.stdout.strip() or f"Failed to start {UPDATE_SERVICE_NAME}"
    return True, "Update started. The web UI may restart while install runs."


def portal_ack_available():
    return bool(CAPTIVE_PORTAL_ACK_CMD)

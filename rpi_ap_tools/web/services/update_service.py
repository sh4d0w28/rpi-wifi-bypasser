import os
import subprocess
from datetime import datetime
from pathlib import Path

from rpi_ap_tools.core.files import load_json_file
from rpi_ap_tools.core.process import run_command

CAPTIVE_PORTAL_ACK_CMD = os.environ.get("CAPTIVE_PORTAL_ACK_CMD", "").strip()
CAPTIVE_PORTAL_PREVIEW_CMD = os.environ.get("CAPTIVE_PORTAL_PREVIEW_CMD", "").strip()
CAPTIVE_PORTAL_DEBUG_DIR = Path(os.environ.get("CAPTIVE_PORTAL_DEBUG_DIR", "/run/rpi_ap_tools_portal_action").strip() or "/run/rpi_ap_tools_portal_action")
UPDATE_SERVICE_NAME = os.environ.get("UPDATE_SERVICE_NAME", "rpi-ap-update.service").strip() or "rpi-ap-update.service"
UPDATE_SCRIPT_PATH = Path("/home/pi/update_ap.sh")
UPDATE_REF_PATH = Path(os.environ.get("UPDATE_REF_PATH", "/run/rpi_ap_tools_update_ref").strip() or "/run/rpi_ap_tools_update_ref")


def _env_float(name, default, minimum=1.0):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        return default


CAPTIVE_PORTAL_ACK_TIMEOUT_SEC = _env_float("CAPTIVE_PORTAL_ACK_TIMEOUT_SEC", 60.0, minimum=5.0)


def _candidate_update_repo_dirs():
    env_repo_dir = os.environ.get("UPDATE_REPO_DIR", "").strip()
    if env_repo_dir:
        yield Path(env_repo_dir).expanduser()
    yield Path(__file__).resolve().parents[3]
    yield UPDATE_SCRIPT_PATH.parent
    yield Path("/home/pi/rpi_ap_tools_waveshare_bundle")
    yield Path("/home/pi/rpi_ap_tools_waveshare")


def _default_update_repo_dir():
    for candidate in _candidate_update_repo_dirs():
        if (candidate / ".git").is_dir():
            return candidate
    env_repo_dir = os.environ.get("UPDATE_REPO_DIR", "").strip()
    if env_repo_dir:
        return Path(env_repo_dir).expanduser()
    return UPDATE_SCRIPT_PATH.parent


UPDATE_REPO_DIR = _default_update_repo_dir()


def normalize_update_ref(value):
    ref = str(value or "").strip()
    for prefix in ("refs/heads/", "refs/tags/", "origin/"):
        if ref.startswith(prefix):
            ref = ref[len(prefix):]
    if not ref:
        return ""
    if len(ref) > 120:
        raise ValueError("Git ref is too long")
    if ref.startswith(("-", "/", ".")) or ref.endswith(("/", ".")):
        raise ValueError("Git ref must look like a branch or tag name")
    if any(token in ref for token in ("..", "@{", "\\", "//")) or ref.endswith(".lock"):
        raise ValueError("Git ref contains invalid characters")
    if not all(ch.isalnum() or ch in "._/-" for ch in ref):
        raise ValueError("Git ref may only contain letters, numbers, ., _, -, and /")
    return ref


def load_requested_update_ref():
    try:
        return normalize_update_ref(UPDATE_REF_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    except (OSError, ValueError):
        return ""


def save_requested_update_ref(ref_name):
    ref_name = normalize_update_ref(ref_name)
    UPDATE_REF_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPDATE_REF_PATH.write_text(f"{ref_name}\n", encoding="utf-8")
    return ref_name


def clear_requested_update_ref():
    try:
        UPDATE_REF_PATH.unlink()
    except FileNotFoundError:
        pass


def _git_stdout(args):
    if not (UPDATE_REPO_DIR / ".git").is_dir():
        return ""
    try:
        proc = subprocess.run(
            ["git", *args],
            text=True,
            capture_output=True,
            check=False,
            cwd=str(UPDATE_REPO_DIR),
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def current_update_ref():
    branch = _git_stdout(["symbolic-ref", "--short", "-q", "HEAD"])
    if branch:
        return branch
    tag = _git_stdout(["describe", "--tags", "--exact-match"])
    if tag:
        return tag
    return _git_stdout(["rev-parse", "--short", "HEAD"])


def update_ref_options():
    branches = [line for line in _git_stdout(["branch", "--format=%(refname:short)"]).splitlines() if line]
    tags = [line for line in _git_stdout(["tag", "--sort=-creatordate"]).splitlines() if line][:20]
    combined = []
    seen = set()
    for item in branches + tags:
        if item not in seen:
            seen.add(item)
            combined.append(item)
    return combined


def portal_preview_command():
    if CAPTIVE_PORTAL_PREVIEW_CMD:
        return CAPTIVE_PORTAL_PREVIEW_CMD
    if not CAPTIVE_PORTAL_ACK_CMD:
        return ""
    if "captive_portal_playwright.py" in CAPTIVE_PORTAL_ACK_CMD and "--preview-only" not in CAPTIVE_PORTAL_ACK_CMD:
        return f"{CAPTIVE_PORTAL_ACK_CMD} --preview-only"
    if "--preview-only" in CAPTIVE_PORTAL_ACK_CMD:
        return CAPTIVE_PORTAL_ACK_CMD
    return ""


def portal_preview_paths():
    return {
        "image": CAPTIVE_PORTAL_DEBUG_DIR / "before.png",
        "json": CAPTIVE_PORTAL_DEBUG_DIR / "before.json",
        "html": CAPTIVE_PORTAL_DEBUG_DIR / "before.html",
    }


def _portal_preview_updated_at(paths):
    mtimes = []
    for path in paths.values():
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    return max(mtimes) if mtimes else 0.0


def load_portal_preview():
    paths = portal_preview_paths()
    payload = load_json_file(paths["json"], {})
    analysis = payload.get("analysis", {}) if isinstance(payload, dict) else {}
    top_candidates = analysis.get("top_candidates", []) if isinstance(analysis, dict) else []
    if not isinstance(top_candidates, list):
        top_candidates = []
    recommended = analysis.get("recommended", {}) if isinstance(analysis, dict) else {}
    if not isinstance(recommended, dict):
        recommended = {}
    updated_at = _portal_preview_updated_at(paths)
    return {
        "available": bool(portal_preview_command()),
        "image_path": str(paths["image"]),
        "json_path": str(paths["json"]),
        "html_path": str(paths["html"]),
        "image_exists": paths["image"].is_file(),
        "json_exists": paths["json"].is_file(),
        "html_exists": paths["html"].is_file(),
        "updated_at": updated_at,
        "updated_at_text": datetime.fromtimestamp(updated_at).strftime("%Y-%m-%d %H:%M:%S") if updated_at else "",
        "image_ts": int(updated_at) if updated_at else 0,
        "url": payload.get("url", "") if isinstance(payload, dict) else "",
        "title": payload.get("title", "") if isinstance(payload, dict) else "",
        "reason": analysis.get("reason", "") if isinstance(analysis, dict) else "",
        "recommended": recommended,
        "top_candidates": top_candidates[:6],
        "candidate_count": analysis.get("candidate_count", 0) if isinstance(analysis, dict) else 0,
        "checkbox_count": analysis.get("checkbox_count", 0) if isinstance(analysis, dict) else 0,
    }


def run_portal_preview():
    command = portal_preview_command()
    if not command:
        return False, "No captive portal preview command configured"
    before_updated_at = _portal_preview_updated_at(portal_preview_paths())
    try:
        proc = subprocess.run(command, text=True, capture_output=True, shell=True, check=False, timeout=CAPTIVE_PORTAL_ACK_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return False, "Portal preview timed out"
    except OSError as exc:
        return False, str(exc)
    message = proc.stdout.strip() or proc.stderr.strip() or "Portal preview finished"
    after = load_portal_preview()
    preview_captured = after["updated_at"] > before_updated_at and (after["image_exists"] or after["json_exists"])
    if proc.returncode == 0:
        return True, message
    if preview_captured:
        return True, f"Preview captured with warnings: {message}"
    return False, message


def run_portal_ack():
    if not CAPTIVE_PORTAL_ACK_CMD:
        return False, "No captive portal action configured"
    try:
        proc = subprocess.run(CAPTIVE_PORTAL_ACK_CMD, text=True, capture_output=True, shell=True, check=False, timeout=CAPTIVE_PORTAL_ACK_TIMEOUT_SEC)
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
    repo_exists = (UPDATE_REPO_DIR / ".git").is_dir()
    current_ref = current_update_ref() if repo_exists else ""
    requested_ref = load_requested_update_ref()
    ref_options = update_ref_options() if repo_exists else []
    if not props:
        return {"service_name": UPDATE_SERVICE_NAME, "script_path": str(UPDATE_SCRIPT_PATH), "script_exists": script_exists, "repo_path": str(UPDATE_REPO_DIR), "repo_exists": repo_exists, "current_ref": current_ref, "requested_ref": requested_ref, "ref_input_value": requested_ref or current_ref, "ref_options": ref_options, "service_installed": False, "running": False, "load_state": "unknown", "active_state": "unknown", "sub_state": "unknown", "summary": "update service unavailable", "status_class": "err", "last_started": "", "can_start": False}
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
    return {"service_name": UPDATE_SERVICE_NAME, "script_path": str(UPDATE_SCRIPT_PATH), "script_exists": script_exists, "repo_path": str(UPDATE_REPO_DIR), "repo_exists": repo_exists, "current_ref": current_ref, "requested_ref": requested_ref, "ref_input_value": requested_ref or current_ref, "ref_options": ref_options, "service_installed": service_installed, "running": running, "load_state": load_state, "active_state": active_state, "sub_state": props.get("SubState", "unknown"), "summary": summary, "status_class": status_class, "last_started": last_started, "can_start": service_installed and script_exists and repo_exists and not running}


def start_update_service(ref_name=""):
    status = load_update_status()
    if not status["service_installed"]:
        return False, f"{UPDATE_SERVICE_NAME} is not installed"
    if not status["script_exists"]:
        return False, f"Update script not found: {UPDATE_SCRIPT_PATH}"
    if not status["repo_exists"]:
        return False, f"Git repository not found: {UPDATE_REPO_DIR}"
    if status["running"]:
        return False, "Update already running"
    previous_ref = load_requested_update_ref()
    try:
        selected_ref = normalize_update_ref(ref_name)
    except ValueError as exc:
        return False, str(exc)
    if selected_ref:
        save_requested_update_ref(selected_ref)
    else:
        clear_requested_update_ref()
    proc = run_command(["systemctl", "start", "--no-block", UPDATE_SERVICE_NAME], check=False)
    if proc.returncode != 0:
        if previous_ref:
            save_requested_update_ref(previous_ref)
        else:
            clear_requested_update_ref()
        return False, proc.stderr.strip() or proc.stdout.strip() or f"Failed to start {UPDATE_SERVICE_NAME}"
    if selected_ref:
        return True, f"Update started for {selected_ref}. The web UI may restart while install runs."
    return True, "Update started. The web UI may restart while install runs."


def portal_ack_available():
    return bool(CAPTIVE_PORTAL_ACK_CMD)


def portal_preview_available():
    return bool(portal_preview_command())

import os
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

from rpi_ap_tools.core.files import load_json_file

STATUS_PATH = Path(os.environ.get("STATUS_PATH", "/run/rpi_ap_tools_status.json"))
PORTAL_CAPTURE_URL = os.environ.get("PORTAL_CAPTURE_URL", "http://connectivitycheck.gstatic.com/generate_204").strip()
CAPTIVE_PORTAL_BROWSER_BIN = os.environ.get("CAPTIVE_PORTAL_BROWSER_BIN", "").strip()
CAPTIVE_PORTAL_REMOTE_DIR = Path(os.environ.get("CAPTIVE_PORTAL_REMOTE_DIR", "/run/rpi_ap_tools_portal_remote"))
CAPTIVE_PORTAL_REMOTE_IMAGE_PATH = CAPTIVE_PORTAL_REMOTE_DIR / "current.png"
CAPTIVE_PORTAL_REMOTE_WIDTH = max(640, int(os.environ.get("CAPTIVE_PORTAL_REMOTE_WIDTH", "1365")))
CAPTIVE_PORTAL_REMOTE_HEIGHT = max(480, int(os.environ.get("CAPTIVE_PORTAL_REMOTE_HEIGHT", "940")))
CAPTIVE_PORTAL_REMOTE_SETTLE_SEC = max(0.5, float(os.environ.get("CAPTIVE_PORTAL_REMOTE_SETTLE_SEC", "2.0")))
CAPTIVE_PORTAL_REMOTE_ACTION_TIMEOUT_SEC = max(5.0, float(os.environ.get("CAPTIVE_PORTAL_REMOTE_ACTION_TIMEOUT_SEC", "45.0")))


def _load_runtime_status():
    data = load_json_file(STATUS_PATH, {})
    return data if isinstance(data, dict) else {}


def _current_portal_target():
    runtime = _load_runtime_status()
    probe = runtime.get("probe", {}) if isinstance(runtime, dict) else {}
    portal = probe.get("portal_capture", {}) if isinstance(probe, dict) else {}
    final_url = str(portal.get("final_url", "")).strip()
    requested_url = str(portal.get("requested_url", "")).strip()
    active_wifi = runtime.get("active_wifi", {}) if isinstance(runtime, dict) else {}
    wifi_name = str(active_wifi.get("name", "")).strip() if isinstance(active_wifi, dict) else ""
    return {
        "url": final_url or requested_url or PORTAL_CAPTURE_URL,
        "wifi_name": wifi_name or str(portal.get("wifi_name", "")).strip() or "-",
    }


class _PortalBrowserWorker:
    def __init__(self):
        self._thread = None
        self._requests = queue.Queue()
        self._thread_lock = threading.Lock()

    def _ensure_thread(self):
        with self._thread_lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="portal-browser-worker", daemon=True)
            self._thread.start()

    def call(self, action, **kwargs):
        if action == "status" and not (self._thread and self._thread.is_alive()):
            return self._idle_state()
        self._ensure_thread()
        response_queue = queue.Queue(maxsize=1)
        self._requests.put({"action": action, "kwargs": kwargs, "response_queue": response_queue})
        try:
            return response_queue.get(timeout=CAPTIVE_PORTAL_REMOTE_ACTION_TIMEOUT_SEC)
        except queue.Empty:
            return {
                **self._idle_state(),
                "ok": False,
                "message": "Portal browser action timed out",
                "last_error": "Portal browser action timed out",
            }

    def _idle_state(self):
        target = _current_portal_target()
        return {
            "ok": True,
            "available": True,
            "session_open": False,
            "current_url": "",
            "target_url": target["url"],
            "wifi_name": target["wifi_name"],
            "message": "",
            "last_error": "",
            "updated_at": 0.0,
            "updated_at_text": "",
            "image_path": str(CAPTIVE_PORTAL_REMOTE_IMAGE_PATH),
            "image_exists": CAPTIVE_PORTAL_REMOTE_IMAGE_PATH.is_file(),
            "image_ts": int(CAPTIVE_PORTAL_REMOTE_IMAGE_PATH.stat().st_mtime) if CAPTIVE_PORTAL_REMOTE_IMAGE_PATH.is_file() else 0,
            "viewport_width": CAPTIVE_PORTAL_REMOTE_WIDTH,
            "viewport_height": CAPTIVE_PORTAL_REMOTE_HEIGHT,
        }

    def _run(self):
        state = self._idle_state()
        playwright = None
        browser = None
        context = None
        page = None

        def set_state(**updates):
            state.update(updates)
            updated_at = time.time()
            state["updated_at"] = updated_at
            state["updated_at_text"] = datetime.fromtimestamp(updated_at).strftime("%Y-%m-%d %H:%M:%S")
            state["image_exists"] = CAPTIVE_PORTAL_REMOTE_IMAGE_PATH.is_file()
            state["image_ts"] = int(CAPTIVE_PORTAL_REMOTE_IMAGE_PATH.stat().st_mtime) if CAPTIVE_PORTAL_REMOTE_IMAGE_PATH.is_file() else 0

        def snapshot(message=""):
            if page is None:
                return
            CAPTIVE_PORTAL_REMOTE_DIR.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(CAPTIVE_PORTAL_REMOTE_IMAGE_PATH), full_page=False)
            set_state(
                session_open=True,
                current_url=page.url,
                message=message or state.get("message", ""),
                last_error="",
            )

        def ensure_runtime():
            nonlocal playwright, browser
            if playwright is None:
                from playwright.sync_api import sync_playwright

                playwright = sync_playwright().start()
            if browser is None:
                launch_kwargs = {
                    "headless": True,
                    "args": [
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                    ],
                }
                if CAPTIVE_PORTAL_BROWSER_BIN:
                    launch_kwargs["executable_path"] = CAPTIVE_PORTAL_BROWSER_BIN
                if hasattr(os, "geteuid") and os.geteuid() == 0:
                    launch_kwargs["args"].append("--no-sandbox")
                browser = playwright.chromium.launch(**launch_kwargs)

        def ensure_page(target_url=""):
            nonlocal context, page
            ensure_runtime()
            if context is None:
                context = browser.new_context(
                    ignore_https_errors=True,
                    viewport={"width": CAPTIVE_PORTAL_REMOTE_WIDTH, "height": CAPTIVE_PORTAL_REMOTE_HEIGHT},
                )
            if page is None or page.is_closed():
                page = context.new_page()
                page.set_default_timeout(int(CAPTIVE_PORTAL_REMOTE_ACTION_TIMEOUT_SEC * 1000))
                page.on("dialog", lambda dialog: dialog.accept())
            if target_url:
                page.goto(target_url, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=int(CAPTIVE_PORTAL_REMOTE_SETTLE_SEC * 1000))
                except Exception:
                    pass
            return page

        def close_page():
            nonlocal context, page
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
                page = None
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
                context = None
            set_state(session_open=False, current_url="", message="Portal browser stopped")

        while True:
            request = self._requests.get()
            action = request["action"]
            kwargs = request["kwargs"]
            response_queue = request["response_queue"]
            try:
                if action == "status":
                    response_queue.put(dict(state))
                    continue
                if action == "start":
                    target = _current_portal_target()
                    target_url = str(kwargs.get("url", "")).strip() or target["url"]
                    ensure_page(target_url)
                    snapshot(f"Portal browser opened {target_url}")
                    set_state(target_url=target_url, wifi_name=target["wifi_name"])
                    response_queue.put({**dict(state), "ok": True})
                    continue
                if action == "reload":
                    target = _current_portal_target()
                    if page is None or page.is_closed():
                        ensure_page(target["url"])
                    else:
                        page.reload(wait_until="domcontentloaded")
                        try:
                            page.wait_for_load_state("networkidle", timeout=int(CAPTIVE_PORTAL_REMOTE_SETTLE_SEC * 1000))
                        except Exception:
                            pass
                    snapshot("Portal browser reloaded")
                    set_state(target_url=target["url"], wifi_name=target["wifi_name"])
                    response_queue.put({**dict(state), "ok": True})
                    continue
                if action == "click":
                    ensure_page()
                    x_ratio = min(1.0, max(0.0, float(kwargs.get("x_ratio", 0.5))))
                    y_ratio = min(1.0, max(0.0, float(kwargs.get("y_ratio", 0.5))))
                    x = max(1, min(CAPTIVE_PORTAL_REMOTE_WIDTH - 1, int(CAPTIVE_PORTAL_REMOTE_WIDTH * x_ratio)))
                    y = max(1, min(CAPTIVE_PORTAL_REMOTE_HEIGHT - 1, int(CAPTIVE_PORTAL_REMOTE_HEIGHT * y_ratio)))
                    page.mouse.click(x, y)
                    page.wait_for_timeout(500)
                    snapshot(f"Clicked at {x},{y}")
                    response_queue.put({**dict(state), "ok": True})
                    continue
                if action == "type":
                    ensure_page()
                    text = str(kwargs.get("text", ""))
                    if text:
                        page.keyboard.type(text, delay=15)
                    snapshot("Typed into portal browser")
                    response_queue.put({**dict(state), "ok": True})
                    continue
                if action == "key":
                    ensure_page()
                    key = str(kwargs.get("key", "")).strip()
                    if not key:
                        raise RuntimeError("Missing key")
                    page.keyboard.press(key)
                    page.wait_for_timeout(250)
                    snapshot(f"Pressed {key}")
                    response_queue.put({**dict(state), "ok": True})
                    continue
                if action == "scroll":
                    ensure_page()
                    delta_y = int(kwargs.get("delta_y", 0))
                    page.mouse.wheel(0, delta_y)
                    page.wait_for_timeout(250)
                    snapshot("Scrolled portal browser")
                    response_queue.put({**dict(state), "ok": True})
                    continue
                if action == "stop":
                    close_page()
                    response_queue.put({**dict(state), "ok": True})
                    continue
                raise RuntimeError(f"Unsupported portal browser action: {action}")
            except Exception as exc:
                set_state(ok=False, message=str(exc), last_error=str(exc))
                response_queue.put({**dict(state), "ok": False})


_WORKER = _PortalBrowserWorker()


def load_portal_browser_status():
    return _WORKER.call("status")


def start_portal_browser(url=""):
    return _WORKER.call("start", url=url)


def reload_portal_browser():
    return _WORKER.call("reload")


def stop_portal_browser():
    return _WORKER.call("stop")


def click_portal_browser(x_ratio, y_ratio):
    return _WORKER.call("click", x_ratio=x_ratio, y_ratio=y_ratio)


def type_portal_browser(text):
    return _WORKER.call("type", text=text)


def press_portal_browser_key(key):
    return _WORKER.call("key", key=key)


def scroll_portal_browser(delta_y):
    return _WORKER.call("scroll", delta_y=delta_y)

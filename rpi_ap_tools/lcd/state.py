import logging
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from threading import Thread

from rpi_ap_tools.core.files import atomic_write_json, atomic_write_text, read_config_value
from rpi_ap_tools.core.process import run_command
from rpi_ap_tools.lcd.render_helpers import sanitize_filename_part
from rpi_ap_tools.system.network import read_ap_name as resolve_ap_name
from rpi_ap_tools.system.network import read_ipv4 as resolve_ipv4


def read_ap_name(hostapd_conf, ap_config_file, wlan_ap):
    return resolve_ap_name(hostapd_conf, ap_config_file, wlan_ap)


def read_ap_password(ap_config_file):
    return read_config_value(ap_config_file, "AP_PASSWORD", "-")


def read_ipv4(dev):
    return resolve_ipv4(dev)


def read_active_wifi(wlan_up):
    proc = run_command(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL", "dev", "wifi", "list", "ifname", wlan_up], check=False)
    for line in proc.stdout.splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[0] == "*":
            return {"name": parts[1] or "-", "signal": parts[2] or "-"}
    return {"name": "-", "signal": "-"}


def read_cpu_temp_c():
    for path in ["/sys/class/thermal/thermal_zone0/temp", "/sys/devices/virtual/thermal/thermal_zone0/temp"]:
        try:
            raw = Path(path).read_text().strip()
            return float(raw) / 1000.0
        except Exception:
            continue
    return None


def read_cpu_percent(cpu_samples):
    try:
        fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
        values = [int(v) for v in fields]
    except Exception:
        return None
    total = sum(values)
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    cpu_samples.append((total, idle))
    if len(cpu_samples) < 2:
        return None
    prev_total, prev_idle = cpu_samples[0]
    curr_total, curr_idle = cpu_samples[1]
    total_diff = curr_total - prev_total
    idle_diff = curr_idle - prev_idle
    if total_diff <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (1.0 - (idle_diff / total_diff))))


def read_mem_percent():
    try:
        data = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, value = line.split(":", 1)
            data[key] = int(value.strip().split()[0])
        total = data.get("MemTotal", 0)
        available = data.get("MemAvailable", 0)
        if total <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * (total - available) / total))
    except Exception:
        return None


def read_sysfs_int(path):
    try:
        return int(Path(path).read_text().strip())
    except Exception:
        return 0


def read_bytes(dev):
    base = Path(f"/sys/class/net/{dev}/statistics")
    return {"rx": read_sysfs_int(base / "rx_bytes"), "tx": read_sysfs_int(base / "tx_bytes")}


def portal_capture_paths(base_path, wifi_name, captured_at):
    stamp = datetime.fromtimestamp(captured_at).strftime("%y_%m_%d_%H:%M:%S")
    safe_wifi = sanitize_filename_part(wifi_name, default="unknown_wifi")
    html_path = base_path.parent / f"{stamp}_{safe_wifi}_portal.html"
    meta_path = html_path.with_suffix(".json")
    return html_path, meta_path


def ping_latency_ms(host):
    proc = run_command(["ping", "-4", "-c", "1", "-W", "1", host], check=False)
    if proc.returncode != 0:
        return None
    match = re.search(r"time[=<]([\d.]+)\s*ms", proc.stdout)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def tcp_latency_ms(host, port):
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return (time.monotonic() - start) * 1000.0
    except OSError:
        return None


def read_nm_connectivity():
    proc = run_command(["nmcli", "-t", "networking", "connectivity"], check=False)
    if proc.returncode != 0:
        return "unknown"
    value = proc.stdout.strip().splitlines()
    return value[0] if value else "unknown"


def perform_portal_ack(command):
    if not command:
        return {"ok": False, "message": "No portal ack command configured", "at": time.time()}
    try:
        proc = subprocess.run(command, text=True, capture_output=True, shell=True, check=False, timeout=20)
        return {"ok": proc.returncode == 0, "message": proc.stdout.strip() or proc.stderr.strip() or "Portal command finished", "at": time.time()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "Portal command timed out", "at": time.time()}
    except Exception as exc:
        return {"ok": False, "message": str(exc), "at": time.time()}


def capture_portal_response(*, url, timeout_sec, max_bytes, html_path_base, wifi_name="-"):
    if not url:
        return {"ok": False, "message": "No portal capture URL configured", "captured_at": time.time()}
    captured_at = time.time()
    html_path, meta_path = portal_capture_paths(html_path_base, wifi_name, captured_at)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "rpi-wifi-bypasser/1.0",
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raw = raw[:max_bytes]
            charset = response.headers.get_content_charset() or "utf-8"
            body = raw.decode(charset, errors="replace")
            meta = {
                "ok": True,
                "captured_at": captured_at,
                "requested_url": url,
                "final_url": response.geturl(),
                "status_code": getattr(response, "status", 200),
                "content_type": response.headers.get_content_type() if response.headers else "",
                "content_length": len(raw),
                "wifi_name": wifi_name,
                "html_path": str(html_path),
                "meta_path": str(meta_path),
            }
            atomic_write_text(html_path, body)
            atomic_write_json(meta_path, meta)
            return meta
    except urllib.error.HTTPError as exc:
        raw = exc.read(max_bytes)
        charset = exc.headers.get_content_charset() if exc.headers else None
        body = raw.decode(charset or "utf-8", errors="replace")
        meta = {
            "ok": False,
            "captured_at": captured_at,
            "requested_url": url,
            "final_url": exc.geturl(),
            "status_code": exc.code,
            "content_type": exc.headers.get_content_type() if exc.headers else "",
            "content_length": len(raw),
            "wifi_name": wifi_name,
            "html_path": str(html_path),
            "meta_path": str(meta_path),
            "message": str(exc),
        }
        atomic_write_text(html_path, body)
        atomic_write_json(meta_path, meta)
        return meta
    except Exception as exc:
        meta = {
            "ok": False,
            "captured_at": captured_at,
            "requested_url": url,
            "wifi_name": wifi_name,
            "html_path": str(html_path),
            "meta_path": str(meta_path),
            "message": str(exc),
        }
        atomic_write_json(meta_path, meta)
        return meta


def watch_command(cmd, name, request_state_refresh):
    while True:
        proc = None
        try:
            proc = subprocess.Popen(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
            logging.info("Started monitor %s: %s", name, " ".join(cmd))
            for line in proc.stdout:
                if line.strip():
                    logging.debug("Monitor %s event: %s", name, line.strip())
                    request_state_refresh()
        except Exception as exc:
            logging.warning("Monitor %s failed: %s", name, exc)
        finally:
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=1)
                except Exception:
                    pass
        time.sleep(2.0)


def start_watchers(wlan_ap, wlan_up, request_state_refresh):
    commands = [
        (["ip", "monitor", "address", "dev", wlan_ap], f"{wlan_ap}-addr"),
        (["ip", "monitor", "address", "dev", wlan_up], f"{wlan_up}-addr"),
        (["nmcli", "monitor"], "nmcli"),
    ]
    for cmd, name in commands:
        Thread(target=watch_command, args=(cmd, name, request_state_refresh), daemon=True).start()

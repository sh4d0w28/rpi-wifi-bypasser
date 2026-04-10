import os
import re
import shlex
import shutil
import subprocess


DEFAULT_TIMEOUT_SEC = float(os.environ.get("EXPRESSVPN_TIMEOUT_SEC", "12") or "12")
VPN_BACKEND = os.environ.get("VPN_BACKEND", "local").strip().lower() or "local"
VPN_SSH_HOST = os.environ.get("VPN_SSH_HOST", "").strip()
VPN_SSH_USER = os.environ.get("VPN_SSH_USER", "pi").strip() or "pi"
VPN_SSH_PASSWORD = os.environ.get("VPN_SSH_PASSWORD", "").strip()
VPN_SSH_OPTIONS = [
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
]

GROUP_PREFIX_LABELS = {
    "australia": "Australia",
    "brazil": "Brazil",
    "canada": "Canada",
    "france": "France",
    "germany": "Germany",
    "hong-kong": "Hong Kong",
    "italy": "Italy",
    "japan": "Japan",
    "netherlands": "Netherlands",
    "south-korea": "South Korea",
    "spain": "Spain",
    "sweden": "Sweden",
    "switzerland": "Switzerland",
    "uk": "UK",
    "usa": "USA",
}


def _normalize_value(value, default="-"):
    text = str(value or "").strip()
    if not text or text.lower() in {"unknown", "none", "null", "n/a"}:
        return default
    return text


def _humanize_slug(value):
    text = str(value or "").strip().strip('"')
    if not text:
        return "-"
    text = text.replace("-via-", " via ")
    parts = re.split(r"([()])", text.replace("-", " "))
    out = []
    for part in parts:
        if part in {"(", ")"}:
            out.append(part)
            continue
        tokens = []
        for token in part.split():
            if token.upper() in {"UK", "USA"}:
                tokens.append(token.upper())
            elif token.lower() == "cbd":
                tokens.append("CBD")
            else:
                tokens.append(token.capitalize())
        out.append(" ".join(tokens))
    return re.sub(r"\s+", " ", "".join(out)).strip() or "-"


def _group_for_region(region_id):
    raw = _normalize_value(region_id, default="")
    if not raw:
        return {"country_key": "unknown", "country_label": "Unknown", "region_label": "Unknown"}
    if raw == "smart":
        return {"country_key": "smart", "country_label": "Smart", "region_label": "Smart"}
    for prefix in sorted(GROUP_PREFIX_LABELS, key=len, reverse=True):
        marker = f"{prefix}-"
        if raw.startswith(marker):
            suffix = raw[len(marker) :]
            if suffix.isdigit():
                region_label = f"{GROUP_PREFIX_LABELS[prefix]} {suffix}"
            else:
                region_label = _humanize_slug(suffix)
            return {
                "country_key": prefix,
                "country_label": GROUP_PREFIX_LABELS[prefix],
                "region_label": region_label,
            }
    label = _humanize_slug(raw)
    return {"country_key": raw, "country_label": label, "region_label": label}


def _build_command(args):
    command = ["expressvpnctl", "--timeout", str(int(DEFAULT_TIMEOUT_SEC))] + list(args)
    if VPN_BACKEND == "ssh":
        remote = shlex.join(command)
        ssh_command = []
        if VPN_SSH_PASSWORD:
            ssh_command.extend(["sshpass", "-p", VPN_SSH_PASSWORD])
        ssh_command.extend(["ssh", *VPN_SSH_OPTIONS, f"{VPN_SSH_USER}@{VPN_SSH_HOST}", remote])
        return ssh_command
    return command


def _run_command(args, timeout_sec=DEFAULT_TIMEOUT_SEC):
    command = _build_command(args)
    try:
        proc = subprocess.run(command, text=True, capture_output=True, check=False, timeout=timeout_sec)
    except FileNotFoundError as exc:
        return {"ok": False, "stdout": "", "stderr": str(exc), "returncode": 127}
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "stdout": exc.stdout or "", "stderr": exc.stderr or "Command timed out", "returncode": 124}
    return {
        "ok": proc.returncode == 0,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "returncode": proc.returncode,
    }


def _extract_status_summary(status_text):
    network_lock = "-"
    split_tunnel = "-"
    for line in (status_text or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = _normalize_value(value)
        if key == "network lock":
            network_lock = value
        elif key == "split tunnel":
            split_tunnel = value
    return {"network_lock_summary": network_lock, "split_tunnel_summary": split_tunnel}


def expressvpn_available():
    if VPN_BACKEND == "ssh":
        if not VPN_SSH_HOST:
            return False
        result = _run_command(["--version"], timeout_sec=6)
        return result["ok"]
    return shutil.which("expressvpnctl") is not None


def list_regions():
    result = _run_command(["get", "regions"])
    if not result["ok"]:
        return {"ok": False, "regions": [], "message": _normalize_value(result["stderr"] or result["stdout"], "Failed to load regions")}
    regions = []
    for line in result["stdout"].splitlines():
        value = line.strip()
        if value:
            regions.append(value)
    return {"ok": True, "regions": regions, "message": ""}


def list_country_groups():
    result = list_regions()
    if not result["ok"]:
        return {"ok": False, "countries": [], "message": result["message"]}
    grouped = {}
    for region_id in result["regions"]:
        info = _group_for_region(region_id)
        bucket = grouped.setdefault(
            info["country_key"],
            {"key": info["country_key"], "label": info["country_label"], "regions": []},
        )
        bucket["regions"].append({"id": region_id, "label": info["region_label"]})
    countries = list(grouped.values())
    for country in countries:
        country["regions"].sort(key=lambda item: (item["id"] != "smart", item["label"].lower(), item["id"]))
    countries.sort(key=lambda item: (item["key"] != "smart", item["label"].lower()))
    return {"ok": True, "countries": countries, "message": ""}


def _get_value(name):
    result = _run_command(["get", name])
    value = _normalize_value(result["stdout"].strip())
    if not result["ok"]:
        return "-", _normalize_value(result["stderr"] or result["stdout"], f"Failed to get {name}")
    return value, ""


def get_status_summary():
    available = expressvpn_available()
    status_result = _run_command(["status"]) if available else {"ok": False, "stdout": "", "stderr": "expressvpnctl not available", "returncode": 127}
    connection_state, err_connection = _get_value("connectionstate") if available else ("-", "expressvpnctl not available")
    selected_region, err_region = _get_value("region") if available else ("-", "expressvpnctl not available")
    public_ip, err_pubip = _get_value("pubip") if available else ("-", "expressvpnctl not available")
    vpn_ip, err_vpnip = _get_value("vpnip") if available else ("-", "expressvpnctl not available")
    group_info = _group_for_region(selected_region if selected_region != "-" else "")
    summary = _extract_status_summary(status_result["stdout"])
    last_error = ""
    for value in [err_connection, err_region, err_pubip, err_vpnip, _normalize_value(status_result["stderr"], "")]:
        if value:
            last_error = value
            break
    return {
        "available": available,
        "connection_state": connection_state,
        "selected_region": selected_region,
        "selected_country": group_info["country_label"] if selected_region != "-" else "-",
        "region_label": group_info["region_label"] if selected_region != "-" else "-",
        "public_ip": public_ip,
        "vpn_ip": vpn_ip,
        "network_lock_summary": summary["network_lock_summary"],
        "split_tunnel_summary": summary["split_tunnel_summary"],
        "status_text": status_result["stdout"].strip(),
        "last_error": last_error,
    }


def _action_result(result, fallback_message):
    message = _normalize_value(result["stdout"] or result["stderr"], fallback_message)
    return {"ok": result["ok"], "message": message}


def connect_auto():
    return _action_result(
        _run_command(["connect", "smart"], timeout_sec=max(DEFAULT_TIMEOUT_SEC, 30)),
        "Connect smart finished",
    )


def connect_region(region_id):
    target = _normalize_value(region_id, default="")
    if not target:
        return {"ok": False, "message": "Region is required"}
    return _action_result(
        _run_command(["connect", target], timeout_sec=max(DEFAULT_TIMEOUT_SEC, 30)),
        f"Connect {target} finished",
    )


def disconnect():
    return _action_result(
        _run_command(["disconnect"], timeout_sec=max(DEFAULT_TIMEOUT_SEC, 20)),
        "Disconnect finished",
    )

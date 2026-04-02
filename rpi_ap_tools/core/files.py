import json
from pathlib import Path


def read_config_value(path, key, default=""):
    path = Path(path)
    if not path.exists():
        return default
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            current_key, value = stripped.split("=", 1)
            if current_key.strip() == key:
                return value.strip().strip("'\"") or default
    except Exception:
        return default
    return default


def load_json_file(path, default):
    path = Path(path)
    if not path.exists():
        return default
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return payload


def atomic_write_json(path, payload):
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(path)
    except Exception:
        pass


def atomic_write_text(path, content):
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(content, encoding="utf-8")
        temp_path.replace(path)
    except Exception:
        pass


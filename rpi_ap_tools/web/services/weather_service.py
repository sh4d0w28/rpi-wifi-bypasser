import json
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

OVERLAY_WEATHER_CITY = os.environ.get("OVERLAY_WEATHER_CITY", "Bangkok").strip() or "Bangkok"
OVERLAY_WEATHER_COUNTRY = os.environ.get("OVERLAY_WEATHER_COUNTRY", "Thailand").strip()
OVERLAY_WEATHER_LAT = os.environ.get("OVERLAY_WEATHER_LAT", "").strip()
OVERLAY_WEATHER_LON = os.environ.get("OVERLAY_WEATHER_LON", "").strip()
OVERLAY_WEATHER_REFRESH_SEC = max(300, int(os.environ.get("OVERLAY_WEATHER_REFRESH_SEC", "600") or "600"))
OVERLAY_WEATHER_CACHE_PATH = Path(os.environ.get("OVERLAY_WEATHER_CACHE_PATH", "/run/rpi_ap_tools_overlay_weather.json"))


def _weather_code_label(code):
    labels = {0: "Clear sky", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Rime fog", 51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Heavy freezing rain", 71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Rain showers", 81: "Heavy showers", 82: "Violent showers", 85: "Snow showers", 86: "Heavy snow showers", 95: "Thunderstorm", 96: "Thunderstorm and hail", 99: "Severe thunderstorm"}
    try:
        return labels.get(int(code), "Weather")
    except (TypeError, ValueError):
        return "Weather"


def _load_json_file(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _save_json_file(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _weather_cache_default():
    return {"city": OVERLAY_WEATHER_CITY, "country": OVERLAY_WEATHER_COUNTRY, "temperature_c": None, "apparent_temperature_c": None, "wind_kph": None, "summary": "Weather unavailable", "updated_at": 0, "source": "cache"}


def _resolve_weather_coordinates():
    if OVERLAY_WEATHER_LAT and OVERLAY_WEATHER_LON:
        try:
            return float(OVERLAY_WEATHER_LAT), float(OVERLAY_WEATHER_LON), OVERLAY_WEATHER_CITY, OVERLAY_WEATHER_COUNTRY
        except ValueError:
            pass
    query = OVERLAY_WEATHER_CITY if not OVERLAY_WEATHER_COUNTRY else f"{OVERLAY_WEATHER_CITY}, {OVERLAY_WEATHER_COUNTRY}"
    url = "https://geocoding-api.open-meteo.com/v1/search?" f"name={quote(query)}&count=1&language=en&format=json"
    payload = run_json_request(url)
    results = payload.get("results") or []
    if not results:
        raise RuntimeError(f"Weather location not found: {query}")
    item = results[0]
    return float(item.get("latitude")), float(item.get("longitude")), item.get("name") or OVERLAY_WEATHER_CITY, item.get("country") or OVERLAY_WEATHER_COUNTRY


def run_json_request(url):
    import urllib.request

    with urllib.request.urlopen(url, timeout=15) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def load_overlay_weather(force_refresh=False):
    cache = _load_json_file(OVERLAY_WEATHER_CACHE_PATH, _weather_cache_default())
    now = time.time()
    if not force_refresh and now - float(cache.get("updated_at") or 0) < OVERLAY_WEATHER_REFRESH_SEC:
        return cache
    try:
        lat, lon, city, country = _resolve_weather_coordinates()
        forecast_url = "https://api.open-meteo.com/v1/forecast?" f"latitude={lat}&longitude={lon}" "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m" "&timezone=auto"
        payload = run_json_request(forecast_url)
        current = payload.get("current") or {}
        next_cache = {"city": city, "country": country, "latitude": lat, "longitude": lon, "temperature_c": current.get("temperature_2m"), "apparent_temperature_c": current.get("apparent_temperature"), "wind_kph": current.get("wind_speed_10m"), "summary": _weather_code_label(current.get("weather_code")), "updated_at": now, "source": "open-meteo"}
        _save_json_file(OVERLAY_WEATHER_CACHE_PATH, next_cache)
        cache = next_cache
    except Exception as exc:
        cache.setdefault("summary", "Weather unavailable")
        cache["error"] = str(exc)
        cache["source"] = "cache"
    temperature = cache.get("temperature_c")
    apparent = cache.get("apparent_temperature_c")
    wind = cache.get("wind_kph")
    cache["temperature_text"] = "--" if temperature is None else f"{round(float(temperature))}C"
    cache["apparent_text"] = "--" if apparent is None else f"Feels {round(float(apparent))}C"
    cache["wind_text"] = "--" if wind is None else f"Wind {round(float(wind))} km/h"
    cache["updated_text"] = datetime.fromtimestamp(float(cache.get("updated_at") or 0)).strftime("%H:%M") if cache.get("updated_at") else "-"
    return cache

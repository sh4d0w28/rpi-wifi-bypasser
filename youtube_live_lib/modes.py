"""Mode normalization and display metadata for YouTube relay behavior."""

from youtube_live_lib.config import DEFAULT_PROXY_AUDIO_MODE

AUDIO_MODE_SPECS = {
    "normal": {
        "label": "Normal",
        "short_label": "NORM",
        "description": "Natural audio mix with live-switchable processing.",
    },
    "voice": {
        "label": "Voice Focus",
        "short_label": "VOICE",
        "description": "Speech-focused band-pass and compression. This is not true vocal isolation.",
    },
    "mute": {
        "label": "Mute",
        "short_label": "MUTE",
        "description": "Drop audio from the outgoing relay.",
    },
}
ROTATION_MODE_SPECS = {
    "0": {
        "label": "Off",
        "short_label": "OFF",
        "description": "Keep the incoming video orientation unchanged.",
        "transpose": None,
    },
    "90": {
        "label": "Rotate 90",
        "short_label": "R+90",
        "description": "Rotate video 90 degrees clockwise before forwarding. The relay uses the Pi hardware encoder when available.",
        "transpose": "1",
    },
    "-90": {
        "label": "Rotate -90",
        "short_label": "R-90",
        "description": "Rotate video 90 degrees counter-clockwise before forwarding. The relay uses the Pi hardware encoder when available.",
        "transpose": "2",
    },
}
FPS_MODE_SPECS = {
    "original": {
        "label": "Original",
        "short_label": "ORIG",
        "description": "Keep the incoming frame rate unchanged.",
        "fps": None,
    },
    "30": {
        "label": "30 FPS",
        "short_label": "30FPS",
        "description": "Cap outgoing video at 30 fps.",
        "fps": "30",
    },
    "20": {
        "label": "20 FPS",
        "short_label": "20FPS",
        "description": "Cap outgoing video at 20 fps to reduce relay CPU load.",
        "fps": "20",
    },
}


def normalize_audio_mode(mode):
    value = (mode or "").strip().lower()
    return value if value in AUDIO_MODE_SPECS else DEFAULT_PROXY_AUDIO_MODE


def audio_mode_spec(mode):
    return AUDIO_MODE_SPECS[normalize_audio_mode(mode)]


def list_audio_modes():
    return [{"value": value, **spec} for value, spec in AUDIO_MODE_SPECS.items()]


def normalize_rotation_mode(mode):
    value = str(mode or "").strip()
    return value if value in ROTATION_MODE_SPECS else "0"


def rotation_mode_spec(mode):
    return ROTATION_MODE_SPECS[normalize_rotation_mode(mode)]


def list_rotation_modes():
    return [{"value": value, **spec} for value, spec in ROTATION_MODE_SPECS.items()]


def normalize_fps_mode(mode):
    value = str(mode or "").strip().lower()
    return value if value in FPS_MODE_SPECS else "original"


def fps_mode_spec(mode):
    return FPS_MODE_SPECS[normalize_fps_mode(mode)]


def list_fps_modes():
    return [{"value": value, **spec} for value, spec in FPS_MODE_SPECS.items()]


def decorate_audio_mode_fields(state, *, default_mode=None):
    if not isinstance(state, dict) or not state:
        return {}
    payload = dict(state)
    mode = normalize_audio_mode(payload.get("audio_mode") or default_mode or DEFAULT_PROXY_AUDIO_MODE)
    spec = audio_mode_spec(mode)
    payload["audio_mode"] = mode
    payload["audio_mode_label"] = spec["label"]
    payload["audio_mode_short"] = spec["short_label"]
    payload["audio_mode_description"] = spec["description"]
    return payload


def decorate_rotation_fields(state, *, default_mode=None):
    if not isinstance(state, dict) or not state:
        return {}
    payload = dict(state)
    mode = normalize_rotation_mode(payload.get("rotation") or default_mode or "0")
    spec = rotation_mode_spec(mode)
    payload["rotation"] = mode
    payload["rotation_label"] = spec["label"]
    payload["rotation_short"] = spec["short_label"]
    payload["rotation_description"] = spec["description"]
    return payload


def decorate_fps_fields(state, *, default_mode=None):
    if not isinstance(state, dict) or not state:
        return {}
    payload = dict(state)
    mode = normalize_fps_mode(payload.get("fps_mode") or default_mode or "original")
    spec = fps_mode_spec(mode)
    payload["fps_mode"] = mode
    payload["fps_mode_label"] = spec["label"]
    payload["fps_mode_short"] = spec["short_label"]
    payload["fps_mode_description"] = spec["description"]
    return payload


def decorate_stream_state(state, *, default_audio_mode=None, default_rotation=None, default_fps_mode=None):
    payload = decorate_audio_mode_fields(state, default_mode=default_audio_mode)
    payload = decorate_rotation_fields(payload, default_mode=default_rotation)
    return decorate_fps_fields(payload, default_mode=default_fps_mode)


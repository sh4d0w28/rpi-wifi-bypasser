"""Stream creation entrypoints."""

from .auth import _api_request, validate_live_access
from .creation_service import run_creation_job, start_stream_creation as service_start_stream_creation
from .storage import load_creation_log, load_creation_state


def _run_creation_job(ap_ip, title, rotation, fps_mode, audio_mode, privacy_status):
    return run_creation_job(
        api_request_fn=_api_request,
        ap_ip=ap_ip,
        title=title,
        rotation=rotation,
        fps_mode=fps_mode,
        audio_mode=audio_mode,
        privacy_status=privacy_status,
    )


def start_stream_creation(*, ap_ip="-", title=None, rotation=None, fps_mode=None, audio_mode=None, privacy_status=None):
    return service_start_stream_creation(
        validate_live_access_fn=validate_live_access,
        ap_ip=ap_ip,
        title=title,
        rotation=rotation,
        fps_mode=fps_mode,
        audio_mode=audio_mode,
        privacy_status=privacy_status,
    )

__all__ = [
    "_run_creation_job",
    "load_creation_log",
    "load_creation_state",
    "start_stream_creation",
]

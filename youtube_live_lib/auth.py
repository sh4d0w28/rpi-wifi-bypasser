"""Authorization and device-flow entrypoints."""

from .legacy import get_auth_status, poll_device_authorization, start_device_authorization

__all__ = [
    "get_auth_status",
    "poll_device_authorization",
    "start_device_authorization",
]

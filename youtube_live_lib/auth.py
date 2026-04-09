"""Authorization and device-flow entrypoints."""

from .auth_service import (
    authorization_ready as service_authorization_ready,
    ensure_access_token as service_ensure_access_token,
    get_auth_status as service_get_auth_status,
    make_api_request,
    poll_device_authorization as service_poll_device_authorization,
    refresh_access_token as service_refresh_access_token,
    start_device_authorization as service_start_device_authorization,
    validate_live_access as service_validate_live_access,
)
from .storage import (
    clear_device_state,
    load_client_config,
    load_creation_state,
    load_device_state,
    load_token,
    save_device_state,
    save_token,
)


def client_ready():
    return bool(load_client_config().get("client_id"))


def authorization_ready():
    return service_authorization_ready(load_token_fn=load_token)


def _refresh_access_token(token):
    return service_refresh_access_token(
        token=token,
        load_client_config_fn=load_client_config,
        save_token_fn=save_token,
    )


def ensure_access_token():
    return service_ensure_access_token(
        load_token_fn=load_token,
        refresh_access_token_fn=_refresh_access_token,
    )


def _api_request(method, path, *, params=None, body=None):
    return make_api_request(ensure_access_token_fn=ensure_access_token)(
        method,
        path,
        params=params,
        body=body,
    )


def validate_live_access():
    return service_validate_live_access(
        authorization_ready_fn=authorization_ready,
        api_request_fn=_api_request,
    )


def get_auth_status():
    return service_get_auth_status(
        load_token_fn=load_token,
        load_device_state_fn=load_device_state,
        client_ready_fn=client_ready,
        authorization_ready_fn=authorization_ready,
        validate_live_access_fn=validate_live_access,
        load_creation_state_fn=load_creation_state,
    )


def start_device_authorization():
    return service_start_device_authorization(
        load_client_config_fn=load_client_config,
        save_device_state_fn=save_device_state,
    )


def poll_device_authorization():
    return service_poll_device_authorization(
        load_client_config_fn=load_client_config,
        load_device_state_fn=load_device_state,
        clear_device_state_fn=clear_device_state,
        load_token_fn=load_token,
        save_token_fn=save_token,
    )

__all__ = [
    "get_auth_status",
    "poll_device_authorization",
    "start_device_authorization",
]

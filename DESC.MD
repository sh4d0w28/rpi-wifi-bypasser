# Repository File Map

This file explains where the main logic lives after the refactor.

## Top-Level Entry Files

- [`youtube_live.py`](/d:/rpi_ap_tools_waveshare_bundle/youtube_live.py)
  Public entrypoint for YouTube live/auth/relay features. This should stay thin and stable.

- [`web_ui.py`](/d:/rpi_ap_tools_waveshare_bundle/web_ui.py)
  Public entrypoint for the Flask web UI. It now forwards into the `rpi_ap_tools.web` package.

- [`lcd_status.py`](/d:/rpi_ap_tools_waveshare_bundle/lcd_status.py)
  Public entrypoint for the Waveshare LCD app. It now forwards into the `rpi_ap_tools.lcd` package.

- [`README.md`](/d:/rpi_ap_tools_waveshare_bundle/README.md)
  Main project documentation and refactor progress tracker.

- [`DESC.MD`](/d:/rpi_ap_tools_waveshare_bundle/DESC.MD)
  This file. Quick navigation guide for developers.

## Shared Python Packages

### `rpi_ap_tools/core`

- [`rpi_ap_tools/core/files.py`](/d:/rpi_ap_tools_waveshare_bundle/rpi_ap_tools/core/files.py)
  Shared file helpers such as config reads and atomic writes.

- [`rpi_ap_tools/core/process.py`](/d:/rpi_ap_tools_waveshare_bundle/rpi_ap_tools/core/process.py)
  Shared subprocess helpers.

Use this package for generic helpers that are not specific to web, LCD, or YouTube.

### `rpi_ap_tools/system`

- [`rpi_ap_tools/system/network.py`](/d:/rpi_ap_tools_waveshare_bundle/rpi_ap_tools/system/network.py)
  Shared network and interface helpers used by multiple surfaces.

Use this package for OS and networking integration code.

## Web UI Package

### Bootstrap

- [`rpi_ap_tools/web/app.py`](/d:/rpi_ap_tools_waveshare_bundle/rpi_ap_tools/web/app.py)
  Flask app factory/bootstrap. This is the internal entry for the web UI.

- [`rpi_ap_tools/web/state.py`](/d:/rpi_ap_tools_waveshare_bundle/rpi_ap_tools/web/state.py)
  Shared web-side state composition and glue code.

### Routes

- [`rpi_ap_tools/web/routes/wifi.py`](/d:/rpi_ap_tools_waveshare_bundle/rpi_ap_tools/web/routes/wifi.py)
  Wi-Fi HTTP routes.

- [`rpi_ap_tools/web/routes/youtube.py`](/d:/rpi_ap_tools_waveshare_bundle/rpi_ap_tools/web/routes/youtube.py)
  YouTube auth/create/relay routes.

- [`rpi_ap_tools/web/routes/overlay.py`](/d:/rpi_ap_tools_waveshare_bundle/rpi_ap_tools/web/routes/overlay.py)
  Overlay editor/render routes.

- [`rpi_ap_tools/web/routes/system.py`](/d:/rpi_ap_tools_waveshare_bundle/rpi_ap_tools/web/routes/system.py)
  System/update/captive-portal related routes.

### Services

- [`rpi_ap_tools/web/services/wifi_service.py`](/d:/rpi_ap_tools_waveshare_bundle/rpi_ap_tools/web/services/wifi_service.py)
  Wi-Fi scanning, saved credentials, connection orchestration.

- [`rpi_ap_tools/web/services/weather_service.py`](/d:/rpi_ap_tools_waveshare_bundle/rpi_ap_tools/web/services/weather_service.py)
  Weather fetch/cache logic.

- [`rpi_ap_tools/web/services/update_service.py`](/d:/rpi_ap_tools_waveshare_bundle/rpi_ap_tools/web/services/update_service.py)
  Update script/service coordination.

- [`rpi_ap_tools/web/services/overlay_render_service.py`](/d:/rpi_ap_tools_waveshare_bundle/rpi_ap_tools/web/services/overlay_render_service.py)
  HTML-to-overlay rendering support and related helpers.

### Templates

- [`templates/index.html`](/d:/rpi_ap_tools_waveshare_bundle/templates/index.html)
  Main web UI page template.

## LCD Package

### Bootstrap and App Flow

- [`rpi_ap_tools/lcd/app.py`](/d:/rpi_ap_tools_waveshare_bundle/rpi_ap_tools/lcd/app.py)
  Main LCD runtime loop, menu navigation, button handling, background refresh loops, and action dispatch.

### Hardware / State / Rendering

- [`rpi_ap_tools/lcd/hardware.py`](/d:/rpi_ap_tools_waveshare_bundle/rpi_ap_tools/lcd/hardware.py)
  Waveshare LCD/button setup and callback wiring.

- [`rpi_ap_tools/lcd/state.py`](/d:/rpi_ap_tools_waveshare_bundle/rpi_ap_tools/lcd/state.py)
  Network/system probing and state collection helpers.

- [`rpi_ap_tools/lcd/render_helpers.py`](/d:/rpi_ap_tools_waveshare_bundle/rpi_ap_tools/lcd/render_helpers.py)
  Small LCD formatting helpers such as text fitting and color selection.

- [`rpi_ap_tools/lcd/screens.py`](/d:/rpi_ap_tools_waveshare_bundle/rpi_ap_tools/lcd/screens.py)
  Screen drawing functions, chrome, QR rendering, relay probing helpers, and overlay demo templates used by the LCD flow.

## YouTube Package

This package still keeps compatibility facade modules, but most behavior now lives in focused modules.

### Public Compatibility Files

- [`youtube_live_lib/common.py`](/d:/rpi_ap_tools_waveshare_bundle/youtube_live_lib/common.py)
  Shared public exports and compatibility surface.

- [`youtube_live_lib/auth.py`](/d:/rpi_ap_tools_waveshare_bundle/youtube_live_lib/auth.py)
  Public auth-facing facade.

- [`youtube_live_lib/relay.py`](/d:/rpi_ap_tools_waveshare_bundle/youtube_live_lib/relay.py)
  Public relay-facing facade.

- [`youtube_live_lib/creation.py`](/d:/rpi_ap_tools_waveshare_bundle/youtube_live_lib/creation.py)
  Public stream-creation facade.

- [`youtube_live_lib/__init__.py`](/d:/rpi_ap_tools_waveshare_bundle/youtube_live_lib/__init__.py)
  Package export surface.

### Internal Behavior Modules

- [`youtube_live_lib/config.py`](/d:/rpi_ap_tools_waveshare_bundle/youtube_live_lib/config.py)
  Environment variables, paths, and config defaults.

- [`youtube_live_lib/modes.py`](/d:/rpi_ap_tools_waveshare_bundle/youtube_live_lib/modes.py)
  Audio/rotation/FPS normalization and labels.

- [`youtube_live_lib/storage.py`](/d:/rpi_ap_tools_waveshare_bundle/youtube_live_lib/storage.py)
  JSON-backed state persistence for auth, stream, relay, creation, and overlay state.

- [`youtube_live_lib/errors.py`](/d:/rpi_ap_tools_waveshare_bundle/youtube_live_lib/errors.py)
  Shared YouTube-specific exceptions.

- [`youtube_live_lib/youtube_api.py`](/d:/rpi_ap_tools_waveshare_bundle/youtube_live_lib/youtube_api.py)
  Raw YouTube API request helpers.

- [`youtube_live_lib/auth_service.py`](/d:/rpi_ap_tools_waveshare_bundle/youtube_live_lib/auth_service.py)
  Device auth flow, polling, and token refresh logic.

- [`youtube_live_lib/relay_runtime.py`](/d:/rpi_ap_tools_waveshare_bundle/youtube_live_lib/relay_runtime.py)
  Relay runtime, FFmpeg orchestration, proxy overlay refresh, and stream relay behavior.

- [`youtube_live_lib/creation_service.py`](/d:/rpi_ap_tools_waveshare_bundle/youtube_live_lib/creation_service.py)
  Stream and broadcast creation workflow.

### Legacy Bridge

- [`youtube_live_lib/legacy.py`](/d:/rpi_ap_tools_waveshare_bundle/youtube_live_lib/legacy.py)
  Compatibility bridge for older logic paths. This is a cleanup target and should shrink over time.

## Shell and Service Files

- [`configure_ap.sh`](/d:/rpi_ap_tools_waveshare_bundle/configure_ap.sh)
  Access point configuration script.

- [`configure_shared_egress.sh`](/d:/rpi_ap_tools_waveshare_bundle/configure_shared_egress.sh)
  Shared egress / NAT configuration script.

- [`install.sh`](/d:/rpi_ap_tools_waveshare_bundle/install.sh)
  Installation/setup script.

- [`update_ap.sh`](/d:/rpi_ap_tools_waveshare_bundle/update_ap.sh)
  Update workflow script.

- [`systemd/rpi-wlan1-ui.service`](/d:/rpi_ap_tools_waveshare_bundle/systemd/rpi-wlan1-ui.service)
  Web UI service unit.

- [`systemd/rpi-lcd-status.service`](/d:/rpi_ap_tools_waveshare_bundle/systemd/rpi-lcd-status.service)
  LCD app service unit.

- [`systemd/rpi-ap-update.service`](/d:/rpi_ap_tools_waveshare_bundle/systemd/rpi-ap-update.service)
  Update service unit.

- [`systemd/rpi-shared-egress.service`](/d:/rpi_ap_tools_waveshare_bundle/systemd/rpi-shared-egress.service)
  Shared egress setup unit.

## Quick Rules For Developers

- If you need web HTTP behavior, start in `rpi_ap_tools/web/routes` and `rpi_ap_tools/web/services`.
- If you need LCD behavior, start in `rpi_ap_tools/lcd/app.py` and `rpi_ap_tools/lcd/screens.py`.
- If you need YouTube stream/auth/relay behavior, start in `youtube_live_lib/*_service.py`, `youtube_live_lib/relay_runtime.py`, and `youtube_live_lib/storage.py`.
- If you need generic helpers, check `rpi_ap_tools/core` and `rpi_ap_tools/system`.
- Keep `youtube_live.py`, `web_ui.py`, and `lcd_status.py` thin and stable.

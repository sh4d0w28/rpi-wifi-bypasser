# Raspberry Pi AP tools bundle v2 (Waveshare 1.44inch LCD HAT)

## Refactor Progress

- Active local branch: `refactor`
- Workflow rule: milestone commits are local only during the refactor; nothing has been pushed from this branch yet
- Stable entrypoints during the refactor:
  - `youtube_live.py`
  - `web_ui.py`
  - `lcd_status.py`
- Completed milestones:
  - `077c5a0` `refactor: extract shared core and system helpers`
  - `7954637` `refactor: split youtube live config and mode helpers`
  - `70d889f` `refactor: split youtube live state storage`
  - `7ac568b` `refactor: move youtube auth and api logic out of legacy`
  - `fec0206` `refactor: move relay runtime and overlay services out of legacy`
  - `ba93bc4` `refactor: move stream creation workflow out of legacy`
  - `c4e5b51` `refactor: convert web ui into app factory and route modules`
  - `bec5275` `refactor: extract web services for wifi overlay weather and updates`
  - `b8e9882` `refactor: split lcd status into hardware state and render modules`
- Remaining milestones:
  - LCD app/bootstrap separation
  - final cleanup
  - each of the milestones above will be committed locally as its own checkpoint

Changes in this version:
- Web UI is split into tabs:
  - `Wi-Fi`
  - `YouTube`
  - `Overlay`
  - `System`
- Added HTML-to-PNG stream overlay support:
  - Web UI stores an overlay HTML template plus layout settings
  - a background renderer snapshots that HTML to a transparent PNG on a timer
  - the YouTube proxy relay can composite that PNG onto outgoing video
  - HTML/CSS rendering is treated as an authoring step; the relay itself receives PNG frames
  - changing overlay content updates live without recreating the YouTube stream bundle
  - changing overlay geometry or opacity reloads the running proxy relay briefly
- Web UI table is mobile-friendly
- Clicking a Wi-Fi row prefills:
  - SSID
  - inferred auth type
- Table shows only:
  - SSID
  - security
  - signal
- Added quick buttons for strongest networks
- LCD diagnostics now show live Waveshare joystick/button activity
- LCD and web UI now show:
  - YouTube ICMP latency
  - YouTube RTMP TCP latency
  - live wlan1 RX/TX link throughput
  - the Pi's own NetworkManager connectivity state / captive portal suspicion
- LCD pages switch on joystick `PRESS`
- LCD shows a red `AUTH ACTION REQUIRED` warning when NetworkManager reports captive-portal state
- Optional captive-portal action hook:
  - Set `CAPTIVE_PORTAL_ACK_CMD` to a site-specific command or script
  - Press `KEY3` on the HAT or use the web button to run it when a portal is suspected
- Web UI software update action:
  - starts `/home/pi/update_ap.sh` through `rpi-ap-update.service`
  - runs outside the Flask service so the web UI can restart safely during install
  - shows last known update status and points to the journal command
- Shared egress helper:
  - `configure_shared_egress.sh` forces `wlan0` clients behind Pi-managed IPv4 NAT
  - disables IPv6 on `wlan0` and `wlan1` to avoid downstream devices bypassing the Pi
  - this is the practical setup when you want captive-portal authorization on the upstream hotspot to apply once for all AP clients
  - `rpi-shared-egress.service` reapplies that policy at boot

Current web UI behavior:
- Successful secured Wi-Fi connections save the password in a local JSON DB
- Saved passwords are reused and prefilled for the same SSID next time
- Wi-Fi rows and quick buttons show a saved-password marker for known SSIDs
- The password field has a show/hide button
- YouTube section can:
  - start Google device authorization
  - poll for completed authorization
  - create a YouTube Live stream/broadcast pair
  - start a local RTMP passthrough relay on the Pi and show the publish URL / relay details
  - detect the incoming relay video dimensions and show portrait vs landscape once a sender is connected
  - switch proxy relay audio between `Normal`, `Voice Focus`, and `Mute` without reconnecting the sender
- Captive-portal status in the UI is only the Pi uplink's status, not a per-client browser/session test for each device behind `wlan0`
- Software update section in the UI can:
  - run `/home/pi/update_ap.sh`
  - show whether the update job is idle, running, or failed
  - keep working even though the update restarts the web UI service, because the job runs in its own oneshot systemd service

LCD controls:
- The LCD now starts on a main home screen like an old phone
- The screen is rotated, so joystick directions are remapped:
  - physical `LEFT` acts as UI `UP`
  - physical `RIGHT` acts as UI `DOWN`
  - physical `UP` acts as UI `RIGHT`
  - physical `DOWN` acts as UI `LEFT`
- `PRESS` or remapped `RIGHT` from home opens the main menu
- remapped `UP` / `DOWN` scroll the current menu
- `PRESS` opens the selected item or runs the primary action on the current screen
- remapped `LEFT` goes back one level
- remapped `RIGHT` jumps back to the root menu
- backing out from the root menu returns to the home screen
- `KEY1`, `KEY2`, and `KEY3` are not used by the LCD UI
- All normal LCD pages now use:
  - a top header with screen id plus `T`, `C`, and `M`
  - a bottom action bar with `< BACK`, `OPEN`, and `MENU >`
- The main menu contains:
  - `YouTube`
  - `Update`
  - `Settings`
- The `YouTube` submenu contains:
  - `Dashboard`
  - `Start Auth`
  - `Check Auth` when a device code is pending
  - `Restart Auth` when auth is pending or failed
  - `Create Stream`
  - `Stream QR` when a stream exists
- On the YouTube screen:
  - the dashboard shows stream title, RTMP summary, rotation, FPS, input/output resolution, and overlay state
  - `PRESS` starts device auth when YouTube is not authorized
  - the device code and verification URL are shown on the LCD
  - `PRESS` checks auth completion while a device code is pending
- The create-stream submenu contains:
  - `Use Defaults`
  - `Rotation`
  - `FPS`
  - `Sound`
  - `Confirm Create`
- Probe checks and YouTube state refresh only run while their related screen or menu is active
- `Update` runs `/home/pi/update_ap.sh` in the background from the LCD UI
- `Update` now asks for confirmation with `Yes` / `No`
- LCD-triggered update output is written to `/run/rpi_ap_tools_update.log`
- `Settings` shows the RTMP summary, default rotation/FPS/sound, and the AP Wi-Fi password

YouTube config:
- `YOUTUBE_CLIENT_CONFIG_PATH` default: `/etc/rpi_ap_tools_youtube_client.json`
- `YOUTUBE_TOKEN_PATH` default: `/var/lib/rpi_ap_tools/youtube_token.json`
- `YOUTUBE_DEVICE_STATE_PATH` default: `/run/rpi_ap_tools_youtube_device.json`
- `YOUTUBE_STREAM_STATE_PATH` default: `/var/lib/rpi_ap_tools/youtube_stream.json`
- `YOUTUBE_CREATE_LOG_PATH` default: `/run/rpi_ap_tools_youtube_create.log`
- `YOUTUBE_STREAM_TITLE_PREFIX` default: `RPi Live`
- `YOUTUBE_STREAM_PRIVACY_STATUS` default: `public`
- `YOUTUBE_PROXY_ENABLED` default: `1`
- `YOUTUBE_PROXY_AUDIO_MODE` is currently forced to `normal` at startup
- Local proxy relay defaults:
  - publish URL: `rtmp://{ap_ip}/live`
  - listen URL: `rtmp://0.0.0.0:1935/live`
  - live audio control URL: `tcp://127.0.0.1:5559`
  - upstream target: YouTube `ingestionAddress` plus the generated stream key
  - relay process keeps video copy and always encodes audio once, so audio mode can change live
  - when rotation or FPS capping is enabled, the relay now prefers FFmpeg's `h264_v4l2m2m` hardware encoder on the Pi and falls back to `libx264` if that encoder is unavailable
  - `voice` mode keeps video copy and enables a speech-focused filter chain on the running relay
  - `mute` mode sets outgoing audio gain to zero on the running relay
  - relay orientation detection is inferred from the incoming video dimensions seen in the ffmpeg relay log
  - audio mode changes are sent to ffmpeg through `azmq`, so the upstream RTMP sender stays connected
  - this requires an ffmpeg build with `azmq` support and a system `libzmq` shared library available on the Pi
- Optional relay env vars:
  - `YOUTUBE_PROXY_PUBLISH_URL`
  - `YOUTUBE_PROXY_RTMP_PORT`
  - `YOUTUBE_PROXY_RTMP_APP`
  - `YOUTUBE_PROXY_ZMQ_PORT`
  - `YOUTUBE_PROXY_FFMPEG_BIN`
  - `YOUTUBE_PROXY_VIDEO_ENCODER` (`auto`, `libx264`, or a specific FFmpeg encoder name)
  - `YOUTUBE_PROXY_HW_VIDEO_ENCODER` (default: `h264_v4l2m2m`)
  - `YOUTUBE_PROXY_HW_VIDEO_BITRATE` (default: `6000k`)
  - `YOUTUBE_RELAY_LOG_PATH`
  - overlay paths and rendering:
    - `YOUTUBE_OVERLAY_STATE_PATH`
    - `YOUTUBE_OVERLAY_HTML_PATH`
    - `YOUTUBE_OVERLAY_PNG_PATH`
    - `YOUTUBE_OVERLAY_RENDER_HTML_PATH`
    - `YOUTUBE_OVERLAY_BROWSER_BIN`
    - `YOUTUBE_OVERLAY_FRAME_INTERVAL_SEC`
    - `OVERLAY_WEATHER_CITY` (default: `Bangkok`)
    - `OVERLAY_WEATHER_COUNTRY` (default: `Thailand`)
    - `OVERLAY_WEATHER_LAT`
    - `OVERLAY_WEATHER_LON`
    - `OVERLAY_WEATHER_REFRESH_SEC` (default: `600`)
    - `OVERLAY_WEATHER_CACHE_PATH`
  - the overlay renderer currently expects a Chromium-compatible browser on the Pi such as `chromium-browser` or `chromium`
  - the Weather overlay demo now renders a 1920x1080 lower-third style card and refreshes live weather data through Open-Meteo on a 10-minute cache by default

Screen map:

Home:
```text
+------------------+
| HOME T:49 C:12 M:|
| AP  Rpi_Ap_Secure|
| IP  192.168.4.1  |
| W1  HotspotName  |
| IP  10.0.0.15    |
| SIG 78%          |
| TXR 1.2M/3.4M    |
|<BACK OPEN MENU > |
+------------------+
```

Menu:
```text
+------------------+
| MENU T:49 C:12 M:|
| MAIN             |
| > YouTube        |
|   Update         |
|   Settings       |
|                  |
|<BACK OPEN MENU > |
+------------------+
```

YouTube auth pending:
```text
+------------------+
| YT   T:49 C:12 M:|
| AUTH PENDING     |
| Code ABCD-EFGH   |
| google.com/device|
| PRESS=CHECK      |
|                  |
|                  |
|<BACK OPEN MENU > |
+------------------+
```

YouTube dashboard:
```text
+------------------+
| YT   T:49 C:12 M:|
| Name Live Test   |
| RTMP .../live    |
| Rot  OFF         |
| FPS  ORIG        |
| In   1080x1920   |
| Out  1080x1920   |
|<BACK OPEN MENU > |
+------------------+
```

Credential DB:
- Default path: `/etc/rpi_ap_tools_wifi_db.json`
- Override with env var: `WIFI_DB_PATH`
- Installer preserves the DB and does not overwrite existing saved passwords

Suggested storage layout:
- `/etc`
  - static config such as OAuth client config
- `/var/lib/rpi_ap_tools`
  - persistent secret/state such as YouTube token and last stream metadata
- `/run`
  - transient runtime state
  - relay log such as `/run/rpi_ap_tools_youtube_relay.log`

Update runner:
- systemd unit: `rpi-ap-update.service`
- default script path: `/home/pi/update_ap.sh`
- manual start: `sudo systemctl start --no-block rpi-ap-update.service`
- logs: `journalctl -u rpi-ap-update.service -n 50`
- the repo now ships `update_ap.sh`, and `install.sh` installs it to `/home/pi/update_ap.sh`
- the update script runs `install.sh` and then reapplies the AP from `/etc/default/rpi_ap_tools_ap`

Shared egress / captive portal notes:
- For "authorize once, all devices work", the Pi must be the only upstream client identity.
- This bundle now enforces that by using IPv4 shared mode on `wlan0` and disabling IPv6.
- If a captive portal keys only on hotspot MAC/IP, one authorization on the Pi/uplink should then cover all devices behind `wlan0`.
- If a captive portal keys on browser cookies, per-device TLS interception, or per-device application login, no generic router-side change can fully collapse all devices into one browser session.

AP setup:
- This bundle expects `wlan0` to be the local AP device and `wlan1` to be the upstream client device.
- Persistent AP settings live in `/etc/default/rpi_ap_tools_ap`.
- `configure_ap.sh` reads that file by default, so reinstalls and update scripts can reapply the same AP settings without hardcoding repo defaults again.
- If you moved to a new USB Wi-Fi adapter or a fresh Pi image, recreate the AP profile with:
  - `sudo /opt/rpi_ap_tools/configure_ap.sh`
- Example `/etc/default/rpi_ap_tools_ap` for a Pi 4B 2.4 GHz AP:
  - `WLAN0_IFACE=wlan0`
  - `AP_CONNECTION_NAME=rpi-ap`
  - `AP_SSID=Rpi_Ap_Secure`
  - `AP_PASSWORD=12345678`
  - `AP_AUTH_MODE=wpa-psk`
  - `AP_BAND=bg`
  - `AP_CHANNEL=6`
- The AP helper creates or updates a NetworkManager hotspot profile on `wlan0` with IPv4 shared mode.
- If you want an open AP for testing, use `AP_AUTH_MODE=open`.
- The web UI and LCD now read the AP SSID from either `hostapd.conf` or the NetworkManager profile.

Diagnostics env vars:
- `STATUS_PATH` default: `/run/rpi_ap_tools_status.json`
- `REFRESH_SEC` default: `2`
- `DISPLAY_REFRESH_SEC` default: `0.5`
- `PROBE_INTERVAL_SEC` default: `60`
- `STATUS_WRITE_SEC` default: `5`
- `YOUTUBE_PING_HOST` default: `www.youtube.com`
- `YOUTUBE_RTMP_HOST` default: `a.rtmp.youtube.com`
- `YOUTUBE_RTMP_PORT` default: `1935`

Web UI scan throttling:
- `WIFI_SCAN_CACHE_SEC` default: `10`
- `WIFI_RESCAN_MIN_INTERVAL_SEC` default: `30`

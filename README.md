# Raspberry Pi AP tools bundle v2 (Waveshare 1.44inch LCD HAT)

Changes in this version:
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
- `PRESS` from home opens the main menu
- remapped `UP` / `DOWN` scroll the current menu
- `PRESS` or remapped `RIGHT` open the selected item
- remapped `LEFT` goes back one level
- backing out from the root menu returns to the home screen
- `KEY1`, `KEY2`, and `KEY3` are not used by the LCD UI
- The main menu contains:
  - `YouTube`
  - `Matrix`
  - `Games`
  - `FFmpeg`
  - `Update`
  - `Settings`
- The `Games` submenu contains:
  - `Pong`
  - `Catch`
- The `YouTube` submenu contains:
  - dashboard / stream status view
  - start device auth
  - check auth when a device code is pending
  - create stream
- The `FFmpeg` submenu contains:
  - proxy audio mode switches when proxy mode is active
- On the YouTube screen:
  - `PRESS` starts device auth when YouTube is not authorized
  - the device code and verification URL are shown on the LCD
  - `PRESS` checks auth completion while a device code is pending
- Matrix, games, probe checks, and YouTube state refresh only run while their related screen or menu is active
- `Update` runs `/home/pi/update_ap.sh` in the background from the LCD UI
- LCD-triggered update output is written to `/run/rpi_ap_tools_update.log`
- If `CAPTIVE_PORTAL_ACK_CMD` is configured, the `Settings` submenu also shows `Portal Ack`

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
  - publish URL: `rtmp://{ap_ip}:7777/live`
  - listen URL: `rtmp://0.0.0.0:7777/live`
  - live audio control URL: `tcp://127.0.0.1:5559`
  - upstream target: YouTube `ingestionAddress` plus the generated stream key
  - relay process keeps video copy and always encodes audio once, so audio mode can change live
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
  - `YOUTUBE_RELAY_LOG_PATH`

Screen map:

Home:
```text
+------------------+
| HOME         MENU|
|                  |
|   AP NAME        |
|   Uplink SSID    |
|   SIG 78%        |
|                  |
| YT NORM  CPU 12% |
| TEMP 49C         |
| PRESS CENTER     |
+------------------+
```

Menu:
```text
+------------------+
| MAIN             |
| > YouTube        |
|   Matrix         |
|   Games          |
|   FFmpeg         |
|   Update         |
|   Settings       |
+------------------+
```

YouTube auth pending:
```text
+------------------+
| YOUTUBE   PENDING|
| Open URL, enter  |
| code             |
| CODE ABCD-EFGH   |
| youtube.com/...  |
| EXP 14M32        |
| PRESS CHECK      |
+------------------+
```

YouTube relay active:
```text
+------------------+
| YOUTUBE     READY|
| Stream title     |
| youtube watch... |
| AUDIO VOICE      |
| 1080x1920        |
| PORTRAIT         |
| LEFT=BACK        |
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

Shared egress / captive portal notes:
- For "authorize once, all devices work", the Pi must be the only upstream client identity.
- This bundle now enforces that by using IPv4 shared mode on `wlan0` and disabling IPv6.
- If a captive portal keys only on hotspot MAC/IP, one authorization on the Pi/uplink should then cover all devices behind `wlan0`.
- If a captive portal keys on browser cookies, per-device TLS interception, or per-device application login, no generic router-side change can fully collapse all devices into one browser session.

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

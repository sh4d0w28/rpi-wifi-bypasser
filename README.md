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
  - start a local RTMP passthrough relay on the Pi and show a QR for the current publish target
- Captive-portal status in the UI is only the Pi uplink's status, not a per-client browser/session test for each device behind `wlan0`

LCD YouTube controls:
- Press `LEFT` to open the YouTube page
- Press `PRESS` on the YouTube page to create a stream
- If a stream exists, the YouTube page shows its QR

YouTube config:
- `YOUTUBE_CLIENT_CONFIG_PATH` default: `/etc/rpi_ap_tools_youtube_client.json`
- `YOUTUBE_TOKEN_PATH` default: `/var/lib/rpi_ap_tools/youtube_token.json`
- `YOUTUBE_DEVICE_STATE_PATH` default: `/run/rpi_ap_tools_youtube_device.json`
- `YOUTUBE_STREAM_STATE_PATH` default: `/var/lib/rpi_ap_tools/youtube_stream.json`
- `YOUTUBE_STREAM_TITLE_PREFIX` default: `RPi Live`
- `YOUTUBE_STREAM_PRIVACY_STATUS` default: `unlisted`
- `YOUTUBE_PROXY_ENABLED` default: `1`
- Local proxy relay defaults:
  - publish URL: `rtmp://{ap_ip}:7777/live`
  - listen URL: `rtmp://0.0.0.0:7777/live`
  - upstream target: YouTube `ingestionAddress` plus the generated stream key
  - relay process: `ffmpeg -listen 1 -c copy`
  - no decode or re-encode is performed, which is the only viable mode for a Pi Zero
- Optional relay env vars:
  - `YOUTUBE_PROXY_PUBLISH_URL`
  - `YOUTUBE_PROXY_RTMP_PORT`
  - `YOUTUBE_PROXY_RTMP_APP`
  - `YOUTUBE_PROXY_FFMPEG_BIN`
  - `YOUTUBE_RELAY_LOG_PATH`

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

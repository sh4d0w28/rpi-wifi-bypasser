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
  - NetworkManager connectivity state / captive portal suspicion
- LCD pages switch on joystick `PRESS`
- LCD shows a red `AUTH ACTION REQUIRED` warning when NetworkManager reports captive-portal state
- Optional captive-portal action hook:
  - Set `CAPTIVE_PORTAL_ACK_CMD` to a site-specific command or script
  - Press `KEY3` on the HAT or use the web button to run it when a portal is suspected

Current web UI behavior:
- Successful secured Wi-Fi connections save the password in a local JSON DB
- Saved passwords are reused and prefilled for the same SSID next time
- Wi-Fi rows and quick buttons show a saved-password marker for known SSIDs
- The password field has a show/hide button
- YouTube section can:
  - start Google device authorization
  - poll for completed authorization
  - create a YouTube Live stream/broadcast pair
  - show a QR for the current publish target

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
- Optional proxy publish URL template:
  - `YOUTUBE_PROXY_PUBLISH_URL`
  - Example: `rtmp://{ap_ip}:7777/live`
  - This only changes the published QR/payload and relay target metadata; an actual local RTMP relay is still required separately.

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

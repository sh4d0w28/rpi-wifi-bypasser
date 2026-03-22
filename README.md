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
- Optional captive-portal action hook:
  - Set `CAPTIVE_PORTAL_ACK_CMD` to a site-specific command or script
  - Press `KEY3` on the HAT or use the web button to run it when a portal is suspected

Current web UI behavior:
- Successful secured Wi-Fi connections save the password in a local JSON DB
- Saved passwords are reused and prefilled for the same SSID next time
- Wi-Fi rows and quick buttons show a saved-password marker for known SSIDs
- The password field has a show/hide button

Credential DB:
- Default path: `/etc/rpi_ap_tools_wifi_db.json`
- Override with env var: `WIFI_DB_PATH`
- Installer preserves the DB and does not overwrite existing saved passwords

Diagnostics env vars:
- `STATUS_PATH` default: `/run/rpi_ap_tools_status.json`
- `PROBE_INTERVAL_SEC` default: `15`
- `YOUTUBE_PING_HOST` default: `www.youtube.com`
- `YOUTUBE_RTMP_HOST` default: `a.rtmp.youtube.com`
- `YOUTUBE_RTMP_PORT` default: `1935`
- `PAGE_ROTATE_SEC` default: `6`

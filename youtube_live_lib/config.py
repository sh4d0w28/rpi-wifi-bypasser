"""Configuration constants for YouTube live support."""

import os
from pathlib import Path

YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube"
CLIENT_CONFIG_PATH = Path(os.environ.get("YOUTUBE_CLIENT_CONFIG_PATH", "/etc/rpi_ap_tools_youtube_client.json"))
TOKEN_PATH = Path(os.environ.get("YOUTUBE_TOKEN_PATH", "/var/lib/rpi_ap_tools/youtube_token.json"))
DEVICE_STATE_PATH = Path(os.environ.get("YOUTUBE_DEVICE_STATE_PATH", "/run/rpi_ap_tools_youtube_device.json"))
STREAM_STATE_PATH = Path(os.environ.get("YOUTUBE_STREAM_STATE_PATH", "/var/lib/rpi_ap_tools/youtube_stream.json"))
STREAM_CREATE_STATE_PATH = Path(os.environ.get("YOUTUBE_STREAM_CREATE_STATE_PATH", "/run/rpi_ap_tools_youtube_create.json"))
STREAM_CREATE_LOCK_PATH = Path(os.environ.get("YOUTUBE_STREAM_CREATE_LOCK_PATH", "/run/rpi_ap_tools_youtube_create.lock"))
CREATION_LOG_PATH = Path(os.environ.get("YOUTUBE_CREATE_LOG_PATH", "/run/rpi_ap_tools_youtube_create.log"))
RELAY_STATE_PATH = Path(os.environ.get("YOUTUBE_RELAY_STATE_PATH", "/var/lib/rpi_ap_tools/youtube_relay.json"))
RELAY_LOG_PATH = Path(os.environ.get("YOUTUBE_RELAY_LOG_PATH", "/run/rpi_ap_tools_youtube_relay.log"))
RELAY_LOCK_PATH = Path(os.environ.get("YOUTUBE_RELAY_LOCK_PATH", "/run/rpi_ap_tools_youtube_relay.lock"))
OVERLAY_STATE_PATH = Path(os.environ.get("YOUTUBE_OVERLAY_STATE_PATH", "/var/lib/rpi_ap_tools/youtube_overlay.json"))
OVERLAY_HTML_PATH = Path(os.environ.get("YOUTUBE_OVERLAY_HTML_PATH", "/var/lib/rpi_ap_tools/youtube_overlay.html"))
OVERLAY_PNG_PATH = Path(os.environ.get("YOUTUBE_OVERLAY_PNG_PATH", "/run/rpi_ap_tools_youtube_overlay.png"))
STREAM_TITLE_PREFIX = os.environ.get("YOUTUBE_STREAM_TITLE_PREFIX", "RPi Live").strip() or "RPi Live"
STREAM_PRIVACY_STATUS = os.environ.get("YOUTUBE_STREAM_PRIVACY_STATUS", "public").strip() or "public"
PROXY_ENABLED = os.environ.get("YOUTUBE_PROXY_ENABLED", "1").strip().lower() not in ("0", "false", "no")
PROXY_PUBLISH_URL_TEMPLATE = os.environ.get("YOUTUBE_PROXY_PUBLISH_URL", "").strip()
PROXY_RTMP_PORT = int(os.environ.get("YOUTUBE_PROXY_RTMP_PORT", "1935") or "1935")
PROXY_RTMP_APP = os.environ.get("YOUTUBE_PROXY_RTMP_APP", "live").strip().strip("/")
PROXY_ZMQ_PORT = int(os.environ.get("YOUTUBE_PROXY_ZMQ_PORT", "5559") or "5559")
FFMPEG_BIN = os.environ.get("YOUTUBE_PROXY_FFMPEG_BIN", "ffmpeg").strip() or "ffmpeg"
DEFAULT_PROXY_AUDIO_MODE = "normal"
DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
PROXY_VIDEO_PRESET = os.environ.get("YOUTUBE_PROXY_VIDEO_PRESET", "veryfast").strip() or "veryfast"
PROXY_VIDEO_CRF = str(os.environ.get("YOUTUBE_PROXY_VIDEO_CRF", "18") or "18").strip()
PROXY_VIDEO_ENCODER = os.environ.get("YOUTUBE_PROXY_VIDEO_ENCODER", "auto").strip().lower() or "auto"
PROXY_HW_VIDEO_ENCODER = os.environ.get("YOUTUBE_PROXY_HW_VIDEO_ENCODER", "h264_v4l2m2m").strip() or "h264_v4l2m2m"
PROXY_HW_VIDEO_BITRATE = str(os.environ.get("YOUTUBE_PROXY_HW_VIDEO_BITRATE", "6000k") or "6000k").strip()
OVERLAY_FRAME_INTERVAL_SEC = max(0.2, float(os.environ.get("YOUTUBE_OVERLAY_FRAME_INTERVAL_SEC", "1.0") or "1.0"))
RELAY_START_TIMEOUT_SEC = max(1.0, float(os.environ.get("YOUTUBE_RELAY_START_TIMEOUT_SEC", "5.0") or "5.0"))
RELAY_STOP_TIMEOUT_SEC = max(1.0, float(os.environ.get("YOUTUBE_RELAY_STOP_TIMEOUT_SEC", "5.0") or "5.0"))
ZMQ_REQ = 3
ZMQ_LINGER = 17
ZMQ_RCVTIMEO = 27
ZMQ_SNDTIMEO = 28
DEFAULT_OVERLAY_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <style>
    html, body {
      margin: 0;
      width: 100%;
      height: 100%;
      background: transparent;
      overflow: hidden;
      font-family: "Segoe UI", Arial, sans-serif;
    }
    .overlay-root {
      width: 100%;
      height: 100%;
      padding: 24px;
      display: flex;
      align-items: flex-start;
      justify-content: flex-start;
      box-sizing: border-box;
    }
    .panel {
      min-width: 260px;
      max-width: 420px;
      padding: 18px 20px;
      border-radius: 20px;
      color: #f8fafc;
      background: rgba(15, 23, 42, 0.64);
      border: 1px solid rgba(148, 163, 184, 0.35);
      box-shadow: 0 18px 44px rgba(2, 6, 23, 0.35);
      backdrop-filter: blur(12px);
    }
    .eyebrow {
      font-size: 13px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: #93c5fd;
    }
    .title {
      margin: 8px 0 4px;
      font-size: 30px;
      font-weight: 700;
    }
    .meta {
      font-size: 15px;
      color: #cbd5e1;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 16px;
    }
    .cell {
      padding: 10px 12px;
      border-radius: 14px;
      background: rgba(15, 23, 42, 0.48);
      border: 1px solid rgba(148, 163, 184, 0.2);
    }
    .label {
      font-size: 12px;
      color: #94a3b8;
    }
    .value {
      margin-top: 4px;
      font-size: 18px;
      font-weight: 600;
    }
  </style>
</head>
<body>
  <div class="overlay-root">
    <div class="panel">
      <div class="eyebrow">RPi Live Overlay</div>
      <div class="title">{{ ap_name }}</div>
      <div class="meta">{{ now_local }}</div>
      <div class="grid">
        <div class="cell">
          <div class="label">Uplink</div>
          <div class="value">{{ active.name or "none" }}</div>
        </div>
        <div class="cell">
          <div class="label">State</div>
          <div class="value">{{ active.state }}</div>
        </div>
        <div class="cell">
          <div class="label">wlan0</div>
          <div class="value">{{ wlan0_ip }}</div>
        </div>
        <div class="cell">
          <div class="label">wlan1</div>
          <div class="value">{{ wlan1_ip }}</div>
        </div>
      </div>
    </div>
  </div>
</body>
</html>
"""


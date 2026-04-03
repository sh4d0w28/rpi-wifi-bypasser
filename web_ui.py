#!/usr/bin/env python3

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from rpi_ap_tools.web.app import APP


if __name__ == "__main__":
    APP.run(host="0.0.0.0", port=8080, debug=False)

#!/usr/bin/env python3

from rpi_ap_tools.web.app import APP


if __name__ == "__main__":
    APP.run(host="0.0.0.0", port=8080, debug=False)

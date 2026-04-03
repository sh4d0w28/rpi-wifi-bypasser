#!/usr/bin/env python3

import importlib
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def load_flask_app():
    module = importlib.import_module("rpi_ap_tools.web.app")
    app = getattr(module, "APP", None)
    if app is None:
        factory = getattr(module, "create_app", None)
        if callable(factory):
            app = factory()
    if app is None:
        raise RuntimeError("rpi_ap_tools.web.app does not expose APP or create_app()")
    return app


if __name__ == "__main__":
    load_flask_app().run(host="0.0.0.0", port=8080, debug=False)

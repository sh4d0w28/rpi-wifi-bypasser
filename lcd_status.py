#!/usr/bin/env python3

import importlib
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def load_lcd_entrypoint():
    module = importlib.import_module("rpi_ap_tools.lcd.app")
    for name in ("main", "run", "start"):
        entrypoint = getattr(module, name, None)
        if callable(entrypoint):
            return entrypoint
    raise RuntimeError("rpi_ap_tools.lcd.app does not expose a callable main entrypoint")


if __name__ == "__main__":
    load_lcd_entrypoint()()

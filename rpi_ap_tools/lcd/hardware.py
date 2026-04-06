import os
import sys

WAVESHARE_DEV = None


def load_waveshare_modules():
    paths = [
        os.environ.get("WAVESHARE_LCD_PATH", "/home/pi/1.44inch-LCD-HAT-Code/RaspberryPi/python"),
        "/home/pi/1.44inch-LCD-HAT-Code/RaspberryPi/python",
    ]
    for path in paths:
        if path and path not in sys.path:
            sys.path.append(path)
    try:
        import LCD_1in44  # type: ignore
        import config  # type: ignore
    except Exception as exc:
        raise SystemExit(f"LCD library import failed: {exc}")
    return LCD_1in44, config


def attach_waveshare_device(lcd):
    global WAVESHARE_DEV
    candidates = [lcd, getattr(lcd, "LCD", None), getattr(lcd, "device", None), getattr(lcd, "DEV", None)]
    for candidate in candidates:
        if candidate is None:
            continue
        if hasattr(candidate, "digital_read") and hasattr(candidate, "GPIO_KEY_UP_PIN"):
            WAVESHARE_DEV = candidate
            return
        for value in vars(candidate).values() if hasattr(candidate, "__dict__") else []:
            if hasattr(value, "digital_read") and hasattr(value, "GPIO_KEY_UP_PIN"):
                WAVESHARE_DEV = value
                return


def get_waveshare_button_device(name):
    if WAVESHARE_DEV is None:
        return None
    attr_name = "GPIO_KEY_PRESS_PIN" if name == "PRESS" else f"GPIO_KEY_{name}_PIN"
    return getattr(WAVESHARE_DEV, attr_name, None)


def bind_button_callbacks(button_pins, button_state_cache, enqueue_button_event):
    bound_any = False
    if WAVESHARE_DEV is None:
        return False
    for name in button_pins:
        device = get_waveshare_button_device(name)
        if device is None:
            continue
        try:
            button_state_cache[name] = bool(device.is_active)
        except Exception:
            button_state_cache[name] = False
        try:
            device.when_activated = lambda _device, button_name=name: enqueue_button_event(button_name, True)
            device.when_deactivated = lambda _device, button_name=name: enqueue_button_event(button_name, False)
            bound_any = True
        except Exception:
            continue
    return bound_any


def button_pressed(name, pin, config_module):
    try:
        if hasattr(config_module, "digital_read"):
            return config_module.digital_read(pin) == 0
        pin_attr = get_waveshare_button_device(name)
        if pin_attr is None:
            return False
        return WAVESHARE_DEV.digital_read(pin_attr) == 0
    except Exception:
        return False


def init_buttons(config_module):
    try:
        if hasattr(config_module, "module_init"):
            config_module.module_init()
    except Exception:
        pass


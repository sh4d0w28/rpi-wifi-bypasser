from pathlib import Path

from flask import Flask

from rpi_ap_tools.web.services.overlay_render_service import start_overlay_renderer_thread, start_relay_watchdog_thread
from rpi_ap_tools.web.routes.overlay import bp as overlay_bp
from rpi_ap_tools.web.routes.system import bp as system_bp
from rpi_ap_tools.web.routes.wifi import bp as wifi_bp
from rpi_ap_tools.web.routes.youtube import bp as youtube_bp


def create_app():
    app = Flask(__name__.split(".")[0], template_folder=str(Path(__file__).resolve().parents[2] / "templates"))
    app.secret_key = __import__("os").environ.get("FLASK_SECRET", "rpi-ap-tools")
    app.register_blueprint(system_bp)
    app.register_blueprint(wifi_bp)
    app.register_blueprint(overlay_bp)
    app.register_blueprint(youtube_bp)
    start_overlay_renderer_thread(app)
    start_relay_watchdog_thread()
    return app


APP = create_app()

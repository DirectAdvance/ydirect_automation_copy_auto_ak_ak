"""
Standalone Flask entrypoint for `/direct/*`.

Run it as a single-process service behind nginx:
    DIRECT_PORT=5020 python -m direct.main

The main seoadvanced app keeps `/login`, `/logout`, `/` and the admin rights
registry. This process owns only the Direct blueprint and reuses the same
Flask session secret, so the existing site cookie remains valid.
"""
import json
import gzip
import os
import sys
from datetime import timedelta
from pathlib import Path

from flask import Flask, redirect, request, session


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# load credentials from .secret/.env
for _p in Path(__file__).resolve().parents:
    if (_p / ".secret" / "loader.py").exists():
        sys.path.insert(0, str(_p / ".secret"))
        break

from loader import _get  # noqa: E402
from direct.create.blueprint import bp as direct_bp, init_direct  # noqa: E402


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(ROOT / "templates"),
        static_folder=str(ROOT / "static"),
    )
    app.secret_key = _get("FLASK_SECRET_KEY")
    app.permanent_session_lifetime = timedelta(days=30)

    @app.after_request
    def compress_large_json(response):
        """Compress API JSON at the app boundary when nginx has no json gzip type.

        The account directory is highly repetitive (about 681 KiB raw / 49 KiB gzip),
        so this removes most of the transfer time without changing endpoint contracts.
        """
        accepts = request.headers.get("Accept-Encoding", "").lower()
        content_type = (response.headers.get("Content-Type") or "").lower()
        if (
            response.status_code not in (204, 304)
            and "gzip" in accepts
            and "application/json" in content_type
            and not response.headers.get("Content-Encoding")
        ):
            body = response.get_data()
            if len(body) >= 1024:
                response.set_data(gzip.compress(body, compresslevel=5))
                response.headers["Content-Encoding"] = "gzip"
                response.headers["Content-Length"] = str(len(response.get_data()))
                response.headers.add("Vary", "Accept-Encoding")
        return response

    @app.context_processor
    def inject_nav_context():
        """Keep the shared nav template working in the standalone Direct app."""
        if not session.get("logged_in"):
            return {"other_services": []}
        try:
            from telegram_parsing.db import list_services, get_user_by_username
            all_services = list_services()
        except Exception:
            return {"other_services": []}
        if session.get("is_admin"):
            return {"other_services": all_services}
        try:
            user = get_user_by_username(session["username"])
            user_svcs = user["services"] if user else []
            session["user_services"] = user_svcs
        except Exception:
            user_svcs = session.get("user_services") or []
        return {"other_services": [s for s in all_services if s["name"] in (user_svcs or [])]}

    # Compatibility endpoints for auth.py: decorators call url_for("login") /
    # url_for("home"). nginx sends those paths to the main app, not this service.
    @app.route("/login", endpoint="login")
    def login_redirect():
        return redirect("/login")

    @app.route("/", endpoint="home")
    def home_redirect():
        return redirect("/")

    app.register_blueprint(direct_bp)
    init_direct()
    return app


app = create_app()


if __name__ == "__main__":
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    port = int(os.environ.get("DIRECT_PORT") or cfg.get("direct_port") or 5020)
    app.run(host=os.environ.get("DIRECT_HOST", "127.0.0.1"),
            port=port, debug=False, threaded=True, use_reloader=False)

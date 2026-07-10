"""
Standalone Flask entrypoint for Direct content editor only.

Run it behind nginx:
    DIRECT_CONTENT_PORT=5021 python -m direct.content_main

This process owns only:
    /direct/automation/content
    /direct/api/content-editor/*

The main Direct automation app remains in direct-create.service on port 5020.
"""
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

from flask import Blueprint, Flask, redirect, session


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _p in Path(__file__).resolve().parents:
    if (_p / ".secret" / "loader.py").exists():
        sys.path.insert(0, str(_p / ".secret"))
        break

from loader import _get  # noqa: E402

# Import the legacy wiring with content routes disabled on its own blueprint.
# We reuse its DB/token/API helpers, then register content routes on a clean bp.
os.environ.setdefault("DIRECT_REGISTER_CONTENT_EDITOR", "0")
from auth import _service_required_any  # noqa: E402
from direct import blueprint as direct_legacy  # noqa: E402
from direct.routes_content_editor import register_content_editor_routes  # noqa: E402


def _inject_nav_context():
    """Keep shared nav/auth redirects compatible with the main site session."""
    if not session.get("logged_in"):
        return {"other_services": []}
    if session.get("content_admin"):
        session["user_services"] = ["direct:content"]
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
        if user:
            user_svcs = user["services"] or []
            session["user_services"] = user_svcs
        else:
            # пользователь не из БД (контент-редактор из JSON-конфига) — не затирать сессию
            user_svcs = session.get("user_services") or []
    except Exception:
        user_svcs = session.get("user_services") or []
    return {"other_services": [s for s in all_services if s["name"] in (user_svcs or [])]}


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(ROOT / "templates"),
        static_folder=str(ROOT / "static"),
    )
    app.secret_key = _get("FLASK_SECRET_KEY")
    app.permanent_session_lifetime = timedelta(days=30)
    app.context_processor(_inject_nav_context)

    @app.route("/login", endpoint="login")
    def login_redirect():
        return redirect("/login")

    @app.route("/", endpoint="home")
    def home_redirect():
        return redirect("/")

    bp = Blueprint(
        "direct",
        __name__,
        url_prefix="/direct",
        template_folder=str(ROOT / "templates"),
    )
    def _balance_response():
        import requests as requests_module
        from concurrent.futures import ThreadPoolExecutor, as_completed
        return direct_legacy._do_balance(requests_module, ThreadPoolExecutor, as_completed)

    register_content_editor_routes(
        bp,
        _service_required_any("work", "work:direct", "direct:content", "direct"),
        victory_conn=direct_legacy._victory_conn,
        token_for_login=direct_legacy._token_for_login,
        direct_tokens=direct_legacy._direct_tokens,
        v5_call=direct_legacy._v5_call,
        v501_svc=direct_legacy._v501_svc,
        default_status=direct_legacy.DEFAULT_STATUS,
        exclude_directologs=direct_legacy._EXCLUDE_DIRECTOLOGS,
        balance_response=_balance_response,
        check_blocks_response=direct_legacy._check_blocks_response,
        victory_conn_rw=direct_legacy._victory_conn_rw,
    )
    app.register_blueprint(bp)
    return app


app = create_app()


if __name__ == "__main__":
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    port = int(os.environ.get("DIRECT_CONTENT_PORT") or cfg.get("direct_content_port") or 5021)
    app.run(host=os.environ.get("DIRECT_CONTENT_HOST", "127.0.0.1"),
            port=port, debug=False, threaded=True, use_reloader=False)

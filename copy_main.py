"""
Standalone Flask entrypoint for the Direct campaign-copy service only.

Run it behind nginx:
    DIRECT_COPY_PORT=5022 python -m direct.copy_main

This process owns only:
    /direct/automation/copy
    /direct/api/copy_campaigns, /direct/api/copy_target_prefill,
    /direct/api/copy_start, /direct/api/copy_status/<job_id>

Копирование исполняется в СОБСТВЕННОЙ in-memory очереди этого процесса
(_ensure_copy_worker) — изолировано от очереди создания РК в direct-create.service.
Рестарт любого из сервисов не трогает очередь другого:
  • copy_main НЕ поднимает create-set recover/sweep/resume/repair/поллер;
  • recover основного процесса исключает kind='copy_campaigns'.
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

# Import the legacy wiring with copy routes disabled on ITS blueprint — мы переиспользуем
# его DB/token/API-хелперы, но вешаем copy-роуты на чистый bp этого процесса и на СВОЙ воркер.
os.environ.setdefault("DIRECT_REGISTER_COPY", "0")
from direct import blueprint as direct_legacy  # noqa: E402


def _inject_nav_context():
    """Keep shared nav/auth redirects compatible with the main site session."""
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
    # Own isolated copy queue: ensure_worker=_ensure_copy_worker (не create-set пул/демоны).
    direct_legacy._wire_copy_routes(bp, ensure_worker=direct_legacy._ensure_copy_worker)
    app.register_blueprint(bp)
    return app


app = create_app()


if __name__ == "__main__":
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    port = int(os.environ.get("DIRECT_COPY_PORT") or cfg.get("direct_copy_port") or 5022)
    app.run(host=os.environ.get("DIRECT_COPY_HOST", "127.0.0.1"),
            port=port, debug=False, threaded=True, use_reloader=False)

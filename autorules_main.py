"""
Standalone Flask entrypoint for Direct Autorules (auto-rules engine).

Run it behind nginx:
    AUTORULES_PORT=5027 python -m direct.autorules_main

This process owns:
    /direct/autorules         — страница
    /direct/api/ar/*          — Phase 2 API (overview, goals, balance, sensors)

Бизнес-логика сенсоров/правил — в пакете direct.autorules.
"""
import os
import sys
from datetime import timedelta
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _p in Path(__file__).resolve().parents:
    if (_p / ".secret" / "loader.py").exists():
        sys.path.insert(0, str(_p / ".secret"))
        break

# ВАЖНО: setdefault ДО импорта automation_runtime (иначе role=all запустит воркеров)
os.environ.setdefault("DIRECT_ROLE", "web")

from flask import Blueprint, Flask, redirect, session  # noqa: E402

from loader import _get, load_db  # noqa: E402
from auth import _service_required_any  # noqa: E402
from direct.autorules import repository  # noqa: E402
from direct.web.routes_autorules import register_autorules_routes  # noqa: E402


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

    # ── seoadvanced БД (схема direct_autorules) ───────────────────────────────
    creds_sa = load_db("home_server")
    _dsn = {
        "host": "127.0.0.1",
        "port": int(creds_sa.get("port", 5432)),
        "dbname": "seoadvanced",
        "user": creds_sa["user"],
        "password": creds_sa["password"],
        "client_encoding": "utf8",
        "connect_timeout": 5,
        "options": "-csearch_path=direct_autorules",
    }
    repository.configure(lambda: psycopg2.connect(**_dsn))
    repository.ensure_schema()

    # ── Victory БД — для overview/sensors (read-only) ─────────────────────────
    from direct.direct_repository import victory_conn as _victory_conn  # noqa: E402

    # ── Direct API транспорт ───────────────────────────────────────────────────
    from direct.yandex_gateway import (  # noqa: E402
        direct_tokens as _direct_tokens,
        token_for_login as _token_for_login,
        LIVE_V4_URL as _LIVE_V4_URL,
    )

    # ── blueprint_metrika (цели Метрики из metrika_goals) ─────────────────────
    from direct.create import blueprint_metrika as _bm  # noqa: E402
    _bm.configure({
        "_victory_conn": _victory_conn,
        "_direct_tokens": _direct_tokens,
    })

    # ── Deps dict для routes_autorules ────────────────────────────────────────
    _deps = {
        "direct_tokens":       _direct_tokens,
        "token_for_login":     _token_for_login,
        "victory_conn":        _victory_conn,
        "metrika_goals_for":   _bm._metrika_goals_for,
        "sensor_runs_append":  repository.sensor_runs_append,
        "LIVE_V4_URL":         _LIVE_V4_URL,
        # Phase 4 — Rules/Adjust/Queries/Placements/Log
        "rules_list":          repository.rules_list,
        "rules_get":           repository.rules_get,
        "rules_create":        repository.rules_create,
        "rules_update":        repository.rules_update,
        "rules_delete":        repository.rules_delete,
        "rule_runs_append":    repository.rule_runs_append,
        "audit_log_append":    repository.audit_log_append,
        "audit_log_list":      repository.audit_log_list,
        "home_accounts_list":  repository.home_accounts_list,
        "home_account_upsert": repository.home_account_upsert,
        "optimizer_events_list":       repository.optimizer_events_list,
        "optimizer_event_get":         repository.optimizer_event_get,
        "optimizer_event_create":      repository.optimizer_event_create,
        "optimizer_event_update":      repository.optimizer_event_update,
        "optimizer_event_delete":      repository.optimizer_event_delete,
        "optimizer_event_runs_append": repository.optimizer_event_runs_append,
    }

    bp = Blueprint(
        "direct_autorules",
        __name__,
        url_prefix="/direct",
        template_folder=str(ROOT / "templates"),
    )
    register_autorules_routes(
        bp,
        _service_required_any("work", "work:direct"),
        deps=_deps,
    )
    app.register_blueprint(bp)
    return app


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("AUTORULES_PORT", 5027))
    host = os.environ.get("AUTORULES_HOST", "127.0.0.1")
    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)

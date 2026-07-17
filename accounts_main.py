"""Standalone Flask entrypoint for the read-only Direct dashboards
«Обзор» + «Статистика по аккаунтам».

Run it behind nginx:
    DIRECT_ACCOUNTS_PORT=5024 python -m direct.accounts_main

Зачем отдельный процесс (решение Семёна 2026-07-17): эти дашборды бьют в Direct API —
медленно, могут ВИСНУТЬ. Раньше их зависание и деплой сидели в ОДНОМ процессе с созданием РК
(direct-create.service :5020). Вынос в свой процесс даёт:
  (а) тормоза/рестарты дашбордов НЕ трогают создание РК;
  (б) правки дашбордов без рестарта direct-create.

Этот процесс поднимает register_account_routes + register_overview_routes (те же route-функции,
что и :5020). Какие из эндпоинтов сюда реально доходят — режет nginx: на :5024 уводятся только
тяжёлые read-only дашборд-вызовы (/api/overview, /api/account_stats, /api/balance,
/api/accounts_otkrut, /api/statuses). Остальные /api/account_* (prefill/assets/audiences/
goal_for_counter/account_info/accounts) обслуживает create-процесс на :5020 — форма создания РК
от :5024 НЕ зависит. Дублирование регистрации безвредно: сюда их nginx просто не направляет.

Страницу /direct/automation (вкладки «Обзор»/«Статистика») по-прежнему рендерит :5020 — этот
процесс шаблон не отдаёт, только API.

The main Direct automation app remains in direct-create.service on port 5020.
"""
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

from flask import Blueprint, Flask, redirect, render_template, session


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _p in Path(__file__).resolve().parents:
    if (_p / ".secret" / "loader.py").exists():
        sys.path.insert(0, str(_p / ".secret"))
        break

from loader import _get  # noqa: E402

# КРИТИЧНО: роль выставляем ДО импорта automation_runtime (он на импорте тянет queue_server).
# Дефолт роли — 'all' (queue_server._direct_role), а он поднял бы в ЭТОМ процессе воркеров и
# демоны создания РК (recover/sweep/resume/repair) — ровно то, от чего мы уходим. Тот же приём:
# content_main.py:35, slepki_main.py:48, worker_main.py.
os.environ.setdefault("DIRECT_ROLE", "web")
# Роуты редактора контента/копирования/слепков этому процессу не нужны — их держат :5021/:5022/:5023.
os.environ.setdefault("DIRECT_REGISTER_CONTENT_EDITOR", "0")
os.environ.setdefault("DIRECT_REGISTER_COPY", "0")

from auth import _service_required_any  # noqa: E402
# Domain wiring only: importing automation_runtime выполняет ВСЮ DI-проводку
# (account_service.configure + blueprint_metrika + repository) — без direct.blueprint,
# без основного Flask-app и без воркеров создания (роль 'web' выставлена выше).
from direct import automation_runtime as _rt  # noqa: E402
from direct.routes_accounts import register_account_routes  # noqa: E402
from direct.routes_overview import register_overview_routes  # noqa: E402


def _inject_nav_context():
    """Keep shared nav/auth redirects compatible with the main site session."""
    if not session.get("logged_in"):
        return {"other_services": []}
    try:
        from telegram_parsing.db import list_services, get_user_by_username
        all_services = list_services()
    except Exception:  # noqa: BLE001
        return {"other_services": []}
    if session.get("is_admin"):
        return {"other_services": all_services}
    try:
        user = get_user_by_username(session["username"])
        user_svcs = user["services"] if user else []
        session["user_services"] = user_svcs
    except Exception:  # noqa: BLE001
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
    access = _service_required_any("work", "work:direct")

    @bp.route("/automation/accounts")
    @access
    def accounts_page():
        """Изолированная страница дашбордов «Обзор» + «Статистика» (свой процесс
        direct-accounts.service). Весь JS — в /static/direct/accounts_ui.js; тяжёлые
        дашборд-эндпоинты nginx уводит сюда (:5024), остальные AJAX идут на :5020."""
        return render_template("direct/accounts.html", is_admin=bool(session.get("is_admin")))

    register_overview_routes(
        bp,
        access,
        victory_conn=_rt._victory_conn,
    )

    register_account_routes(
        bp,
        access,
        victory_conn=_rt._victory_conn,
        parse_counter_ids=_rt._parse_counter_ids,
        parse_number=lambda val, default: _rt._num(val, default),
        metrika_goals_for=_rt._metrika_goals_for,
        resolve_region=_rt._resolve_region,
        account_prefill_func=_rt._account_prefill_response,
        account_assets_func=_rt._account_assets_response,
        account_audiences_func=_rt._account_audiences_response,
        balance_func=_rt._do_balance,
        pull_begin=_rt._pull_begin,
        pull_end=_rt._pull_end,
        busy_response=_rt._busy_response,
        cooldowns=_rt._COOLDOWN,
        goal_vse_formy=_rt._goal_vse_formy,
        account_cols=_rt._ACCOUNT_COLS,
        default_status=_rt.DEFAULT_STATUS,
        exclude_directologs=_rt._EXCLUDE_DIRECTOLOGS,
    )
    app.register_blueprint(bp)
    return app


app = create_app()


if __name__ == "__main__":
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    port = int(os.environ.get("DIRECT_ACCOUNTS_PORT") or cfg.get("direct_accounts_port") or 5024)
    app.run(host=os.environ.get("DIRECT_ACCOUNTS_HOST", "127.0.0.1"),
            port=port, debug=False, threaded=True, use_reloader=False)

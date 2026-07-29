"""Standalone Flask entrypoint for the Direct «Обучение ИИ» API only.

Run it behind nginx:
    DIRECT_AI_PORT=5026 python -m direct.ai_main

This process owns only:
    /direct/api/ai/*   (8 эндпоинтов routes_ai: status, chat, agents, promo/generate,
                        campaign/generate, slepok_content/seed, slepok_content, promo/publish)

Зачем отдельный процесс (решение Семёна 2026-07-17): эти роуты ходят в M3/LLM — это ДОЛГИЕ
блокирующие вызовы (_M3_LLM_TIMEOUT — минуты), и раньше они занимали Flask-воркеров того же
процесса, что и создание РК (direct-create.service :5020). Вынос в свой процесс даёт:
  (а) залипшая генерация/чат с M3 НЕ отъедает воркеров у создания РК;
  (б) правки/рестарт ИИ-роутов не трогают direct-create.

Отдельной СТРАНИЦЫ у вкладки нет: UI «Обучение ИИ» — вкладка на /direct/automation, её
по-прежнему рендерит :5020. Этот процесс шаблонов не отдаёт, только API.

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

# КРИТИЧНО: роль выставляем ДО импорта automation_runtime (он на импорте тянет queue_server).
# Дефолт роли — 'all' (queue_server._direct_role), а он поднял бы в ЭТОМ процессе воркеров и
# демоны создания РК (recover/sweep/resume/repair) — ровно то, от чего мы уходим. Тот же приём:
# content_main.py:35, slepki_main.py:48, accounts_main.py:49, gateway_main.py:50, worker_main.py.
os.environ.setdefault("DIRECT_ROLE", "web")
# DIRECT_REGISTER_* здесь НЕ выставляем осознанно: эти флаги читает ТОЛЬКО direct/blueprint.py
# (строки 90/234/285), а этот процесс blueprint вообще не импортирует (automation_runtime его не
# тянет) — чужие роуты тут физически не поднимаются. Соседние *_main.py их всё же ставят
# defensively; здесь это был бы мёртвый no-op.

from auth import _service_required_any  # noqa: E402
# Domain wiring only: importing automation_runtime выполняет ВСЮ DI-проводку — без
# direct.blueprint, без основного Flask-app и без воркеров создания (роль 'web' выставлена выше).
# Имена берём из _rt теми же алиасами, что и blueprint.py:342 (там они попадают в globals()
# через globals().update(vars(automation_runtime))) — проводка сверяется с ним один-в-один.
from direct import automation_runtime as _rt  # noqa: E402
from direct.web.routes_ai import register_ai_routes  # noqa: E402


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
    access = _service_required_any("work", "work:direct")  # == blueprint._direct_access

    register_ai_routes(
        bp,
        access,
        ai_agents=_rt._ai_agents_routes,
        campaign_module=_rt.cmc,
        promo_client_cls=_rt._PromoClientRoutes,
        m3_llm_url=_rt._M3_LLM_URL,
        m3_llm_timeout=_rt._M3_LLM_TIMEOUT,
        m3_complete=_rt._m3_complete,
        promo_ctx=_rt._promo_ctx,
        promo_extract_json=_rt._promo_extract_json,
        promo_from_slepok=_rt._promo_from_slepok,
        promo_preview=_rt._promo_preview,
        promo_validate=_rt._promo_validate,
        promo_amount_steps=_rt._promo_amount_steps,
        gen_campaign_content=_rt._gen_campaign_content,
        seed_slepok_content=_rt._seed_slepok_content,
        victory_conn=_rt._victory_conn,
        direct_tokens=_rt._direct_tokens,
        token_for_login=_rt._token_for_login,
        pull_begin=_rt._pull_begin,
        pull_end=_rt._pull_end,
        busy_response=_rt._busy_response,
        v5_get=_rt._v5_get,
    )
    app.register_blueprint(bp)
    return app


app = create_app()


if __name__ == "__main__":
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    port = int(os.environ.get("DIRECT_AI_PORT") or cfg.get("direct_ai_port") or 5026)
    app.run(host=os.environ.get("DIRECT_AI_HOST", "127.0.0.1"),
            port=port, debug=False, threaded=True, use_reloader=False)

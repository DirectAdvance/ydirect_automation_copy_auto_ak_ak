"""Standalone Flask entrypoint for the Direct «Структура слепков» editor only.

Run it behind nginx:
    DIRECT_SLEPKI_PORT=5023 python -m direct.slepki_main

This process owns only:
    /direct/automation/slepki
    /direct/api/slepki/*   (13 эндпоинтов routes_slepki_edit)

Зачем отдельный процесс (решение Семёна 2026-07-17): рестарт редактора структуры не должен
ронять очередь создания РК, и наоборот — рестарт direct-create.service (частый) не должен
ронять страницу слепков.

Обмен со сервисом создания — ФАЙЛОМ на общей ФС, БЕЗ HTTP-API между процессами:
  • правки пишутся АТОМАРНО (temp + os.replace, см. slepki_editor.py) — читатель видит либо
    старый, либо новый ЦЕЛЫЙ снимок slepki_structure.json;
  • direct-create перечитывает структуру свежей на каждый _json() (инвалидация по mtime+size).
Этот процесс правки НЕ применяет: он только КЛАДЁТ edit-джобу в БД (job_new при DIRECT_ROLE=web),
а исполняет её direct-create-worker (queue_server._is_edit_job → slepki_editor.handle_job).

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

# КРИТИЧНО: роль выставляем ДО импорта queue_server/automation_runtime.
# Дефолт роли — 'all' (queue_server._direct_role), а он поднял бы в ЭТОМ процессе воркеров и
# демоны создания РК (recover/sweep/resume/repair) — ровно то, от чего мы уходим. При 'web'
# _job_new уходит в _job_new_web: джоба пишется в БД с _web_posted=true и её забирает
# direct-create-worker. Тот же приём: worker_main.py:41 ('worker'), content_main.py:35.
os.environ.setdefault("DIRECT_ROLE", "web")
# Роуты редактора контента/копирования этому процессу не нужны — их держат :5021 / :5022.
os.environ.setdefault("DIRECT_REGISTER_CONTENT_EDITOR", "0")
os.environ.setdefault("DIRECT_REGISTER_COPY", "0")

from auth import _service_required_any  # noqa: E402
# Domain wiring only: no direct.blueprint, no main Flask app and no unrelated routes.
# automation_runtime на импорте делает _sed.configure({...}) — без него редактор без DI.
from direct import automation_runtime as _runtime  # noqa: E402
from direct import slepki_editor as _sed  # noqa: E402
from direct.routes_slepki_edit import register_slepki_edit_routes  # noqa: E402


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

    @bp.route("/automation/slepki")
    @access
    def slepki_page():
        """Отдельная изолированная страница «Структура слепков» (свой процесс
        direct-slepki.service). Срез структуры страница тянет с /direct/api/ui_structure
        (:5020, общая сессия через nginx) — этот процесс его не рендерит."""
        return render_template("direct/slepki.html", is_admin=bool(session.get("is_admin")))

    register_slepki_edit_routes(
        bp,
        access,
        slepki_editor=_sed,
        job_new=_runtime._job_new,          # DIRECT_ROLE=web → _job_new_web (только БД)
        ag_part1_map=_runtime._ag_part1_map,
    )
    app.register_blueprint(bp)
    return app


app = create_app()


if __name__ == "__main__":
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    port = int(os.environ.get("DIRECT_SLEPKI_PORT") or cfg.get("direct_slepki_port") or 5023)
    app.run(host=os.environ.get("DIRECT_SLEPKI_HOST", "127.0.0.1"),
            port=port, debug=False, threaded=True, use_reloader=False)

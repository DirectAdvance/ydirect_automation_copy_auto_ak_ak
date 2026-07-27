"""Standalone Flask entrypoint for the Direct «Структура слепков» editor only.

Run it behind nginx:
    DIRECT_SLEPKI_PORT=5023 python -m direct.slepki_main

This process owns only:
    /direct/automation/slepki
    /direct/api/slepki/*   (routes_slepki_edit + read-only ui_structure/minus-places)

Зачем отдельный процесс (решение Семёна 2026-07-17): рестарт редактора структуры не должен
ронять очередь создания РК, и наоборот — рестарт direct-create.service (частый) не должен
ронять страницу слепков.

Обмен с сервисом создания — через общую ФС и очередь, БЕЗ HTTP-зависимости страницы от :5020:
  • структура живёт в direct/slepki/<key>.json + _order.json;
  • read-only срез страницы отдаёт этот процесс через /direct/api/slepki/ui_structure;
  • правки кладутся edit-джобами в БД, применяет direct-slepki-worker.

The main Direct automation app remains in direct-create.service on port 5020.
"""
import gzip
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

from flask import Blueprint, Flask, jsonify, make_response, redirect, render_template, request, session


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
from direct.routes_settings import save_global_minus_places_payload  # noqa: E402


def _gzip_response(resp):
    """Compress large page/API responses for browser refreshes behind nginx."""
    if resp.status_code == 304 or len(resp.get_data()) < 1024:
        return resp
    if "gzip" not in (request.headers.get("Accept-Encoding") or "").lower():
        return resp
    gz = gzip.compress(resp.get_data(), compresslevel=5)
    resp.set_data(gz)
    resp.headers["Content-Encoding"] = "gzip"
    resp.headers["Content-Length"] = str(len(gz))
    resp.headers["Vary"] = "Accept-Encoding"
    return resp


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
    app.json.ensure_ascii = False
    app.json.compact = True
    app.json.sort_keys = False
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
        direct-slepki.service). Срез структуры отдаётся тем же процессом, чтобы рестарт
        direct-create.service не ломал открытие страницы."""
        initial_payload = _runtime._ui_structure_payload(selected_slepok="pavlov", light=True)
        initial_json = json.dumps(initial_payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        resp = make_response(render_template(
            "direct/slepki.html",
            is_admin=bool(session.get("is_admin")),
            initial_ui_json=initial_json,
        ))
        resp.headers["Cache-Control"] = "no-store"
        return _gzip_response(resp)

    @bp.route("/api/slepki/ui_structure")
    @access
    def slepki_api_ui_structure():
        """Read-only срез структуры для отдельной страницы слепков (:5023)."""
        light_arg = (request.args.get("light") or "").strip()
        payload = _runtime._ui_structure_payload(
            selected_slepok=(request.args.get("selected") or "").strip(),
            # This endpoint belongs only to the isolated slepki page. Default to the fast
            # light slice so stale HTML without `light=1` cannot pull the old 11MB payload.
            light=(request.args.get("full") or "") != "1" and light_arg != "0",
        )
        resp = jsonify(payload)
        sig = payload.get("sig")
        if sig:
            resp.set_etag(sig)
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
            return _gzip_response(resp.make_conditional(request))
        resp.headers["Cache-Control"] = "no-store"
        return _gzip_response(resp)

    @bp.route("/api/slepki/minus-places")
    @access
    def slepki_api_minus_places_get():
        """Общий disabledPlaces list через slepki-сервис, без зависимости страницы от :5020."""
        return jsonify({"places": _runtime._global_minus_places()})

    @bp.route("/api/slepki/minus-places", methods=["POST"])
    @access
    def slepki_api_minus_places_post():
        """Admin-only replace-all для общего disabledPlaces list через slepki-сервис."""
        if not session.get("is_admin"):
            return jsonify({"ok": False, "error": "только администратор"}), 403
        payload, status = save_global_minus_places_payload(
            request.json or {},
            global_minus_places=_runtime._global_minus_places,
            minus_places_ensure=_runtime._minus_places_ensure,
            place_host=_runtime._place_host,
            victory_conn_rw=_runtime._victory_conn_rw,
        )
        return jsonify(payload), status

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

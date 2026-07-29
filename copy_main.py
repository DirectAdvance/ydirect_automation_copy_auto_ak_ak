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

from auth import _service_required_any  # noqa: E402
# Domain wiring only: no direct.blueprint, no main Flask app and no unrelated routes.
from direct import automation_runtime as _runtime  # noqa: E402,F401
from direct import account_service as accounts  # noqa: E402
from direct import blueprint_metrika as metrika  # noqa: E402
from direct import copy_engine  # noqa: E402
from direct import queue_server as queue  # noqa: E402
from direct import yandex_gateway as yandex  # noqa: E402
from direct.routes_copy import register_copy_routes  # noqa: E402
from direct.copy_api import copy_api_bp, register_copy_api  # noqa: E402


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


def _parse_number(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _content_access_path() -> Path:
    for parent in Path(__file__).resolve().parents:
        cand = parent / ".secret" / "content_editor_access.json"
        if cand.exists():
            return cand
    return ROOT / "direct" / "content_editor_access.json"


def _copy_queue_allowed_directologists() -> list[str] | None:
    """Scope copy queue exactly like content-editor users.

    None means full access; [] means no scoped access.
    """
    if session.get("is_admin") or session.get("content_admin"):
        return None
    username = (session.get("username") or "").strip()
    if not username:
        return []
    try:
        data = json.loads(_content_access_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    for row in data.get("users") or []:
        if (row.get("username") or "").strip() == username and row.get("status") != "blocked":
            return [
                str(x).strip()
                for x in (row.get("directologists") or [])
                if str(x).strip()
            ]
    return []


def _copy_repair_pending(job_id: str) -> bool:
    """True если для copy-job есть незавершённая запись в public.direct_delayed_repairs.

    Persistent добивка (kind='content_repair') создаётся queue_server после done copy-job
    (_schedule_delayed_content_repair_after_done) и выполняется демоном direct-create-worker.
    Пока статус не в (done/failed/cancelled/superseded) — добивка ещё идёт.
    Best-effort: при недоступности БД (Victory) возвращает False, не подвешивает поллинг.
    """
    if not job_id:
        return False
    try:
        from direct.direct_repository import victory_conn_rw
        conn = victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM public.direct_delayed_repairs "
                "WHERE parent_job_id = %s "
                "  AND status IN ('waiting', 'running') "
                "LIMIT 1",
                (job_id,),
            )
            return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — Victory недоступна или схема ещё не создана
        return False


def _copy_api_idempotency_lookup(idempotency_key: str) -> dict | None:
    """Найти copy-job по внешнему Idempotency-Key без отдельной миграции схемы."""
    key = (idempotency_key or "").strip()
    if not key:
        return None
    try:
        import psycopg2.extras
        from direct.direct_repository import victory_conn

        conn = victory_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT job_id, login, status, total, kind, agency, body "
                "FROM public.direct_automation_jobs "
                "WHERE kind='copy_campaigns' "
                "  AND body->>'_copy_api_idempotency_key' = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (key,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — best-effort: API всё равно создаст обычную джобу
        return None


def _copy_queue_jobs() -> list[dict]:
    """Последние 200 copy-джоб из public.direct_automation_jobs для вкладки «Очередь».
    Возвращает список dict с полями: job_id, status, total, done, created, failed,
    error, created_at, source_login, target_login, elapsed.
    При недоступности Victory возвращает пустой список."""
    allowed_directologists = _copy_queue_allowed_directologists()
    try:
        import psycopg2.extras
        from direct.direct_repository import victory_conn

        conn = victory_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            if allowed_directologists is None:
                cur.execute(
                    "SELECT job_id, login, status, total, done, created, failed, "
                    "       error, created_at, body "
                    "FROM public.direct_automation_jobs "
                    "WHERE kind='copy_campaigns' "
                    "ORDER BY created_at DESC LIMIT 200"
                )
            elif not allowed_directologists:
                rows = []
                cur.close()
                return []
            else:
                cur.execute(
                    "SELECT j.job_id, j.login, j.status, j.total, j.done, j.created, j.failed, "
                    "       j.error, j.created_at, j.body "
                    "FROM public.direct_automation_jobs j "
                    "WHERE j.kind='copy_campaigns' "
                    "  AND EXISTS ("
                    "    SELECT 1 FROM public.local_gsheet_sites s "
                    "    WHERE s.direction='Авто' "
                    "      AND s.directologist = ANY(%s) "
                    "      AND s.login_key IN (j.body->>'source_login', COALESCE(j.body->>'target_login', j.login))"
                    "  ) "
                    "ORDER BY j.created_at DESC LIMIT 200",
                    (allowed_directologists,),
                )
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — Victory недоступна → тихий пустой список
        return []

    result = []
    for r in rows:
        body = r.get("body") if isinstance(r.get("body"), dict) else {}
        source_login = body.get("source_login") or ""
        target_login = body.get("target_login") or r.get("login") or ""
        created_by = body.get("created_by") or body.get("username") or body.get("_actor") or ""
        created_at = r.get("created_at")
        if hasattr(created_at, "timestamp"):
            try:
                created_at = created_at.timestamp()
            except Exception:  # noqa: BLE001
                created_at = None
        result.append({
            "job_id": r.get("job_id"),
            "status": r.get("status"),
            "total": int(r.get("total") or 0),
            "done": int(r.get("done") or 0),
            "created": int(r.get("created") or 0),
            "failed": int(r.get("failed") or 0),
            "error": r.get("error") or "",
            "created_at": created_at,
            "source_login": source_login,
            "target_login": target_login,
            "created_by": created_by,
            "elapsed": None,
        })
    return result


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(ROOT / "templates"),
        static_folder=str(ROOT / "static"),
    )
    app.secret_key = _get("FLASK_SECRET_KEY")
    app.permanent_session_lifetime = timedelta(days=30)
    # Жёсткий лимит multipart-запросов на уровне Flask (до f.read() в роуте).
    # Flask сам вернёт 413 при превышении — RAM direct-copy.service не пострадает.
    # 64 МБ: достаточно для пачки фото (~15-20 больших JPEG) или zip-архива с десятками PNG.
    # Должно совпадать с client_max_body_size в nginx (location ^~ /direct/api/copy_).
    # Если лимит nginx меньше — он режет запрос СВОЕЙ HTML-страницей 413 до Flask,
    # и фронт падает на «Unexpected token '<'» вместо внятной ошибки.
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024 * 1024  # 256 МБ
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
    # Own isolated copy queue: queue.ensure_copy_worker starts no create-set daemons/poller.
    register_copy_routes(
        bp,
        _service_required_any("work", "work:direct", "direct:content", "direct"),
        api_campaigns_func=accounts._campaigns_response,
        account_prefill_func=accounts._account_prefill_response,
        metrika_goals_for=metrika._metrika_goals_for,
        parse_number=_parse_number,
        copy_default_feed_path=copy_engine._COPY_DEFAULT_FEED_PATH,
        counter_foreign_owner=metrika._counter_foreign_owner,
        resolve_agency_hint=yandex.resolve_agency_hint,
        ensure_create_worker=queue._ensure_copy_worker,
        job_new=queue._job_new,
        copy_job_upsert=copy_engine._copy_job_upsert,
        create_jobs_ahead=queue._create_jobs_ahead,
        create_jobs=queue._CREATE_JOBS,
        create_jobs_lock=queue._CREATE_JOBS_LOCK,
        copy_jobs=copy_engine._COPY_JOBS,
        copy_jobs_lock=copy_engine._COPY_JOBS_LOCK,
        feeds_preview_func=copy_engine._copy_feeds_preview,
        feeds_check_func=copy_engine._copy_feeds_check,
        geo_regions_func=metrika._geo_regions_list,          # legacy fallback
        geo_regions_tree_func=metrika._geo_regions_tree,     # дерево для нового виджета
        geo_validate_id_func=metrika._geo_is_valid_id,       # валидация id от клиента
        images_upload_func=copy_engine._copy_images_upload,
        target_campaigns_info_func=copy_engine._copy_target_campaigns_info,  # кол-во кампаний на цели
        repair_pending_func=_copy_repair_pending,  # persistent добивка direct_delayed_repairs
        job_db_get_func=queue._job_db_get,
        copy_queue_func=_copy_queue_jobs,           # список джоб для вкладки «Очередь»
    )
    # Программный API /api/v1/copy для внешних клиентов (auth по X-API-Key, fail-closed).
    register_copy_api(
        copy_api_bp,
        ensure_create_worker=queue._ensure_copy_worker,
        job_new=queue._job_new,
        copy_job_upsert=copy_engine._copy_job_upsert,
        create_jobs_ahead=queue._create_jobs_ahead,
        create_jobs=queue._CREATE_JOBS,
        create_jobs_lock=queue._CREATE_JOBS_LOCK,
        copy_jobs=copy_engine._COPY_JOBS,
        copy_jobs_lock=copy_engine._COPY_JOBS_LOCK,
        resolve_agency_hint=yandex.resolve_agency_hint,
        copy_default_feed_path=copy_engine._COPY_DEFAULT_FEED_PATH,
        counter_foreign_owner=metrika._counter_foreign_owner,
        api_campaigns_func=accounts._campaigns_response,
        parse_number=_parse_number,
        geo_validate_id_func=metrika._geo_is_valid_id,   # валидация geo_region_ids (mode=other/change)
        repair_pending_func=_copy_repair_pending,
        job_db_get_func=queue._job_db_get,
        idempotency_lookup_func=_copy_api_idempotency_lookup,
    )
    app.register_blueprint(copy_api_bp)

    app.register_blueprint(bp)
    # Agent Board done -> copy retry must not wait for the next manual copy_start.
    # Start the isolated copy worker/retry daemon with the service process itself.
    queue._ensure_copy_worker(app)
    return app


app = create_app()


if __name__ == "__main__":
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    port = int(os.environ.get("DIRECT_COPY_PORT") or cfg.get("direct_copy_port") or 5022)
    app.run(host=os.environ.get("DIRECT_COPY_HOST", "127.0.0.1"),
            port=port, debug=False, threaded=True, use_reloader=False)

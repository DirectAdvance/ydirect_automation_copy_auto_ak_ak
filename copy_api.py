"""
Программный API сервиса копирования кампаний Директа.

Blueprint: copy_api_bp, url_prefix /api/v1/copy

Внешние клиенты (другой сайт, скрипты) управляют копированием по API-ключу.
НЕ использует Flask-сессию — только заголовок X-API-Key (COPY_API_KEY в .secret/.env).

Роуты:
    POST /api/v1/copy/start           — поставить задачу копирования в очередь
    GET  /api/v1/copy/status/<job_id> — статус / прогресс / лог задачи
    GET  /api/v1/copy/campaigns       — список кампаний source-аккаунта

Регистрация в copy_main.py — snippet в .claude/sdd/copy-api-report.md.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Optional

from flask import Blueprint, current_app, jsonify, request

# ── .secret/loader ────────────────────────────────────────────────────────────
for _p in Path(__file__).resolve().parents:
    if (_p / ".secret" / "loader.py").exists():
        sys.path.insert(0, str(_p / ".secret"))
        break
from loader import _get  # noqa: E402


# ─── Blueprint (создаётся на уровне модуля — до register_copy_api) ────────────
copy_api_bp: Blueprint = Blueprint("copy_api", __name__, url_prefix="/api/v1/copy")


# ─── CORS ─────────────────────────────────────────────────────────────────────

def _copy_api_allowed_origins() -> list:
    """Белый список CORS-origin'ов из COPY_API_CORS_ORIGINS в .secret/.env.

    Формат: comma-separated строка, напр. «https://site1.ru,https://site2.ru».
    Пустая строка или ключ отсутствует — CORS не разрешён совсем (не «*»!).
    """
    try:
        raw = _get("COPY_API_CORS_ORIGINS", "")
    except KeyError:
        return []
    return [o.strip() for o in (raw or "").split(",") if o.strip()]


def _copy_api_add_cors(response, origin: Optional[str]):
    """Добавляет CORS-заголовки только для origin'ов из белого списка (НЕ «*»).

    Изменяет response in-place и возвращает его же — безопасно вызывать в цепочке.
    """
    if not origin:
        return response
    if origin in _copy_api_allowed_origins():
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


# ─── Auth ─────────────────────────────────────────────────────────────────────

def _copy_api_key_ok() -> bool:
    """True если X-API-Key из запроса совпадает с COPY_API_KEY из .secret/.env.

    При отсутствии ключа в .env или пустом значении — всегда False (fail-closed).
    Сравнение делается по значению; ключ в лог не попадает.
    """
    try:
        expected = _get("COPY_API_KEY", "")
    except KeyError:
        return False
    if not expected:
        return False
    return request.headers.get("X-API-Key", "") == expected


# ─── Вспомогательные утилиты ─────────────────────────────────────────────────

def _copy_api_parse_ids(body: dict) -> list:
    """Разбирает campaign_ids из тела запроса; пропускает нечисловые значения."""
    raw = body.get("campaign_ids") or []
    return [int(x) for x in raw if str(x).isdigit()]


# ─── Регистрация роутов (DI через параметры, по образцу routes_copy.py) ───────

def register_copy_api(
    bp: Blueprint,
    *,
    ensure_create_worker: Callable,
    job_new: Callable,
    copy_job_upsert: Callable,
    create_jobs_ahead: Callable,
    create_jobs: dict,
    create_jobs_lock,
    copy_jobs: dict,
    copy_jobs_lock,
    resolve_agency_hint: Callable,
    copy_default_feed_path: str,
    counter_foreign_owner: Optional[Callable] = None,
    api_campaigns_func: Optional[Callable] = None,
    parse_number: Optional[Callable] = None,
) -> None:
    """Добавляет роуты /api/v1/copy/* в переданный blueprint.

    Параметры — те же DI-объекты, что register_copy_routes получает в copy_main.py:
    ensure_create_worker = queue._ensure_copy_worker
    job_new              = queue._job_new
    copy_job_upsert      = copy_engine._copy_job_upsert
    create_jobs_ahead    = queue._create_jobs_ahead
    create_jobs          = queue._CREATE_JOBS
    create_jobs_lock     = queue._CREATE_JOBS_LOCK
    copy_jobs            = copy_engine._COPY_JOBS
    copy_jobs_lock       = copy_engine._COPY_JOBS_LOCK
    resolve_agency_hint  = yandex.resolve_agency_hint
    copy_default_feed_path = copy_engine._COPY_DEFAULT_FEED_PATH

    Опциональные:
    counter_foreign_owner = metrika._counter_foreign_owner  (проверка владельца счётчика)
    api_campaigns_func    = accounts._campaigns_response    (список кампаний)
    parse_number          = _parse_number из copy_main.py   (fallback: int(float(v)))

    Секреты/токены в параметрах не передаются.
    """

    def _num(value, default: int = 0) -> int:
        if parse_number is not None:
            return parse_number(value, default)
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    # ── OPTIONS preflight (CORS) ──────────────────────────────────────────────

    @bp.route("/start", methods=["OPTIONS"])
    def copy_api_start_options():
        resp = jsonify({})
        return _copy_api_add_cors(resp, request.headers.get("Origin"))

    @bp.route("/campaigns", methods=["OPTIONS"])
    def copy_api_campaigns_options():
        resp = jsonify({})
        return _copy_api_add_cors(resp, request.headers.get("Origin"))

    # ── POST /api/v1/copy/start ───────────────────────────────────────────────

    @bp.route("/start", methods=["POST"])
    def copy_api_start():
        """Поставить задачу копирования в очередь.

        Тело (JSON):
            source_login    str        логин источника
            target_login    str        логин цели
            campaign_ids    [int]      ID кампаний для копирования
            target_domain   str        домен целевого аккаунта
            counter_id      int        счётчик Метрики
            goal_id         int        цель «Все формы»
            target_city     str        (mode=auto) город
            target_region   str        (mode=auto) регион
            mode            str        «auto» (дефолт) или «other»
            target_cleanup  str        none / delete_drafts / archive (дефолт: none)
            target_feed_url str        URL фида (дефолт: _COPY_DEFAULT_FEED_PATH)
            feed_map        object     (опц.) маппинг фидов источника → цели
            agency          str        (опц.) агентство

        Ответ 200:
            {job_id, login, agency, total, kind, ahead, existing?, note?}

        Ошибки: 400 / 401 / 500 с {error: "..."}
        """
        origin = request.headers.get("Origin")
        if not _copy_api_key_ok():
            return _copy_api_add_cors(
                jsonify({"error": "неверный или отсутствующий API-ключ"}), origin
            ), 401

        body = request.get_json(silent=True) or {}
        source_login = (body.get("source_login") or "").strip()
        target_login = (body.get("target_login") or "").strip()
        campaign_ids = _copy_api_parse_ids(body)
        counter_id = _num(body.get("counter_id"), 0)
        goal_id = _num(body.get("goal_id"), 0)
        target_domain = (body.get("target_domain") or "").strip()
        target_city = (body.get("target_city") or "").strip()
        target_region = (body.get("target_region") or "").strip()
        target_feed_url = (body.get("target_feed_url") or copy_default_feed_path).strip()
        target_cleanup = (body.get("target_cleanup") or "none").strip()
        mode = (body.get("mode") or "auto").strip()

        # Валидация обязательных полей
        if not source_login or not target_login:
            return _copy_api_add_cors(
                jsonify({"error": "source_login и target_login обязательны"}), origin
            ), 400
        if not campaign_ids:
            return _copy_api_add_cors(
                jsonify({"error": "campaign_ids: выберите хотя бы одну кампанию"}), origin
            ), 400
        if not counter_id:
            return _copy_api_add_cors(
                jsonify({"error": "counter_id обязателен"}), origin
            ), 400
        if not goal_id:
            return _copy_api_add_cors(
                jsonify({"error": "goal_id обязателен"}), origin
            ), 400
        if not target_domain:
            return _copy_api_add_cors(
                jsonify({"error": "target_domain обязателен"}), origin
            ), 400
        if mode not in ("auto", "other"):
            return _copy_api_add_cors(
                jsonify({"error": "mode допустимо: auto, other"}), origin
            ), 400
        if mode == "auto" and not (target_city or target_region):
            return _copy_api_add_cors(
                jsonify({"error": "target_city или target_region обязательны при mode=auto"}), origin
            ), 400
        if target_cleanup not in ("none", "delete_drafts", "archive"):
            return _copy_api_add_cors(
                jsonify({"error": "target_cleanup допустимо: none, delete_drafts, archive"}), origin
            ), 400
        if target_feed_url and not (
            target_feed_url.startswith("/")
            or target_feed_url.startswith(("http://", "https://"))
        ):
            return _copy_api_add_cors(
                jsonify({"error": "target_feed_url: абсолютный URL или путь от /"}), origin
            ), 400

        # Проверка владельца счётчика (если DI подключён)
        if counter_foreign_owner is not None:
            try:
                owner = counter_foreign_owner(counter_id, target_login)
            except Exception:  # noqa: BLE001
                owner = None
            if owner:
                return _copy_api_add_cors(
                    jsonify({"error": f"счётчик {counter_id} принадлежит «{owner}», а не «{target_login}»"}),
                    origin,
                ), 400

        # Тело джобы по образцу _copy_start_impl из routes_copy.py
        job_body = dict(body)
        job_body["mode"] = mode              # из валидации роута, не из payload клиента
        job_body["_kind"] = "copy_campaigns"
        job_body["login"] = target_login
        job_body["source_login"] = source_login
        job_body["target_login"] = target_login
        job_body["counter_id"] = counter_id
        job_body["goal_id"] = goal_id
        job_body["target_domain"] = target_domain
        job_body["target_city"] = target_city
        job_body["target_region"] = target_region
        job_body["target_feed_url"] = target_feed_url
        job_body["target_cleanup"] = target_cleanup
        job_body["campaign_ids"] = campaign_ids
        # feed_map, image_mode, image_hashes и прочие опциональные поля — as-is из body

        resolved_ag = resolve_agency_hint(target_login, (body.get("agency") or "").strip())
        if resolved_ag:
            job_body["agency"] = resolved_ag

        # Запустить воркер (идемпотентно) и поставить джобу в очередь
        app = current_app._get_current_object()
        ensure_create_worker(app)

        with create_jobs_lock:
            existing_ids = set(create_jobs.keys())

        # job_new: dedup_login=True → при активной джобе того же login'а вернёт её job_id
        job_id = job_new(len(campaign_ids), target_login, job_body, {}, dedup_login=True)

        if job_id in existing_ids:
            # Дубль: для этого login'а уже есть незавершённая джоба
            with create_jobs_lock:
                ahead = create_jobs_ahead(job_id)
            return _copy_api_add_cors(
                jsonify({
                    "job_id": job_id,
                    "login": target_login,
                    "agency": resolved_ag or "",
                    "total": len(campaign_ids),
                    "kind": "copy_campaigns",
                    "ahead": ahead,
                    "existing": True,
                    "note": "для этого аккаунта уже есть активная задача; дубль не создан",
                }),
                origin,
            )

        # Новая джоба: инициализируем запись в copy-специфичном словаре
        copy_job_upsert(
            job_id,
            status="queued",
            progress=0,
            source_login=source_login,
            target_login=target_login,
            selected=len(campaign_ids),
            total=len(campaign_ids),
        )
        with create_jobs_lock:
            ahead = create_jobs_ahead(job_id)

        return _copy_api_add_cors(
            jsonify({
                "job_id": job_id,
                "login": target_login,
                "agency": resolved_ag or "",
                "total": len(campaign_ids),
                "kind": "copy_campaigns",
                "ahead": ahead,
            }),
            origin,
        )

    # ── GET /api/v1/copy/status/<job_id> ─────────────────────────────────────

    @bp.route("/status/<job_id>")
    def copy_api_status(job_id: str):
        """Статус / прогресс / лог задачи копирования.

        Ответ 200:
            {job_id, status, progress, total, selected, source_login, target_login,
             created_at, updated_at, result, error, log[-50:]}

        Секреты, session-снапшоты, внутренние поля (_kind, _web_posted и т.п.) — не отдаются.
        """
        origin = request.headers.get("Origin")
        if not _copy_api_key_ok():
            return _copy_api_add_cors(
                jsonify({"error": "неверный или отсутствующий API-ключ"}), origin
            ), 401

        with copy_jobs_lock:
            job = dict(copy_jobs.get(job_id) or {})

        if not job:
            return _copy_api_add_cors(
                jsonify({"error": "job не найден"}), origin
            ), 404

        # Безопасная выжимка: только публичные поля
        _SAFE = (
            "job_id", "status", "progress", "total", "selected",
            "source_login", "target_login", "created_at", "updated_at",
            "result", "error",
        )
        safe: dict = {k: job[k] for k in _SAFE if k in job}
        raw_log = job.get("log")
        if isinstance(raw_log, list):
            safe["log"] = raw_log[-50:]    # последние 50 строк, не полотно

        return _copy_api_add_cors(jsonify(safe), origin)

    # ── GET /api/v1/copy/campaigns ────────────────────────────────────────────

    @bp.route("/campaigns")
    def copy_api_campaigns():
        """Список кампаний source-аккаунта.

        Query params: login (обязателен), agency (опц.) — аналогично /direct/api/copy_campaigns.
        Ответ: {campaigns:[{id, name, type, state, status}], ...} — прозрачный проброс
        ответа от account_service._campaigns_response.

        Если api_campaigns_func не передана в register_copy_api — 503 с TODO.
        """
        origin = request.headers.get("Origin")
        if not _copy_api_key_ok():
            return _copy_api_add_cors(
                jsonify({"error": "неверный или отсутствующий API-ключ"}), origin
            ), 401

        if api_campaigns_func is None:
            return _copy_api_add_cors(
                jsonify({
                    "error": "api_campaigns_func не подключена",
                    "TODO": "передай api_campaigns_func=accounts._campaigns_response в register_copy_api",
                }),
                origin,
            ), 503

        # _campaigns_response читает request.args["login"] изнутри (Flask-aware)
        try:
            raw = api_campaigns_func()
        except Exception as exc:  # noqa: BLE001
            return _copy_api_add_cors(
                jsonify({"error": f"ошибка получения кампаний: {str(exc)[:200]}"}), origin
            ), 502

        if hasattr(raw, "headers"):
            return _copy_api_add_cors(raw, origin)
        return raw

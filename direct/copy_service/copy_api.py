"""
Программный API сервиса копирования кампаний Директа.

Blueprint: copy_api_bp, url_prefix /api/v1/copy

Внешние клиенты (другой сайт, скрипты) управляют копированием по API-ключу.
НЕ использует Flask-сессию — только заголовок X-API-Key (COPY_API_KEY в .secret/.env).

Роуты:
    POST /api/v1/copy/start           — поставить задачу копирования в очередь
    GET  /api/v1/copy/status/<job_id> — статус / прогресс / лог задачи
    GET  /api/v1/copy/campaigns       — список кампаний source-аккаунта
    GET  /api/v1/copy/health          — health-check внешнего API

Регистрация в copy_main.py — snippet в .claude/sdd/copy-api-report.md.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional

from flask import Blueprint, current_app, jsonify, request

from .copy_request import (
    default_geo_mode,
    parse_feed_map,
    parse_image_hashes,
    validate_api_campaign_ids,
    validate_other_geo,
)

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
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key, Idempotency-Key"
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
    return hmac.compare_digest(request.headers.get("X-API-Key", ""), expected)


# ─── Вспомогательные утилиты ─────────────────────────────────────────────────

def _copy_api_error(message: str, code: str, status: int, origin: Optional[str]):
    """Единый error envelope для внешних клиентов."""
    return _copy_api_add_cors(
        jsonify({"ok": False, "error": message, "error_code": code}),
        origin,
    ), status


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:  # noqa: BLE001
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _copy_api_payload_hash(job_body: dict) -> str:
    """Hash stable public payload fields; internal queue keys are ignored."""
    public_body = {k: v for k, v in (job_body or {}).items() if not str(k).startswith("_")}
    raw = json.dumps(public_body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _copy_api_result_summary(result: Any) -> dict | None:
    """Stable, sanitized summary instead of raw worker result with internal paths."""
    data = _as_dict(result)
    if not data:
        return None
    rows = data.get("results") if isinstance(data.get("results"), list) else []
    summary: dict[str, Any] = {
        "ok": data.get("ok"),
        "campaigns": {
            "total": len(rows),
            "created": sum(1 for row in rows if isinstance(row, dict) and row.get("ok")),
            "failed": sum(1 for row in rows if isinstance(row, dict) and row.get("ok") is False),
        },
    }
    if data.get("source_login"):
        summary["source_login"] = data.get("source_login")
    if data.get("target_login"):
        summary["target_login"] = data.get("target_login")
    for key in ("created_campaign_ids", "target_campaign_ids"):
        if isinstance(data.get(key), list):
            summary[key] = data.get(key)

    post = _as_dict(data.get("cookie_postprocess"))
    verify = _as_dict(data.get("copy_verify") or post.get("copy_verify"))
    live = _as_dict(data.get("live_verification") or post.get("live_verification"))
    repair = _as_dict(data.get("auto_repair") or post.get("copy_repair") or post.get("auto_repair"))
    gate = _as_dict(data.get("verification_gate") or post.get("verification_gate"))

    if verify:
        verify_rows = verify.get("results") or verify.get("diffs") or verify.get("diff") or []
        summary["verification"] = {
            "ok": verify.get("ok"),
            "summary": verify.get("summary"),
            "diff_count": len(verify_rows) if isinstance(verify_rows, list) else 0,
        }
    if live:
        summary["live_verification"] = {
            "ok": live.get("ok"),
            "status": live.get("status"),
            "summary": live.get("summary"),
        }
    if repair:
        summary["repair"] = {
            "ok": repair.get("ok"),
            "status": repair.get("status"),
            "repairs_count": len(repair.get("repairs") or []),
            "errors_count": len(repair.get("errors") or []),
        }
    if gate:
        blockers = gate.get("blockers") or []
        summary["verification_gate"] = {
            "ok": gate.get("ok"),
            "blockers_count": len(blockers) if isinstance(blockers, list) else 0,
            "blockers": blockers[:10] if isinstance(blockers, list) else [],
        }
    errors = []
    for row in rows:
        if isinstance(row, dict) and row.get("error"):
            errors.append({"campaign_id": row.get("source_id") or row.get("campaign_id"), "error": row.get("error")})
    if data.get("error"):
        errors.append({"error": data.get("error")})
    if errors:
        summary["errors"] = errors[:20]
    return _json_safe(summary)


def _copy_api_job_from_db(row: dict) -> dict:
    body = _as_dict(row.get("body"))
    total = int(row.get("total") or len(body.get("campaign_ids") or []) or 0)
    done = int(row.get("done") or row.get("created") or 0)
    progress = 100 if row.get("status") in ("done", "error", "cancelled", "interrupted") else (
        min(99, round(done * 100 / total)) if total else 0
    )
    return {
        "job_id": row.get("job_id"),
        "status": row.get("status"),
        "progress": progress,
        "total": total,
        "done": done,
        "created": int(row.get("created") or 0),
        "failed": int(row.get("failed") or 0),
        "source_login": body.get("source_login"),
        "target_login": body.get("target_login") or row.get("login"),
        "selected": len(body.get("campaign_ids") or []) or total,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "result": row.get("result"),
        "error": row.get("error"),
        "kind": row.get("kind"),
        "body": body,
    }


def _copy_api_status_payload(job_id: str, job: dict, repair_pending_func: Optional[Callable]) -> dict:
    status = job.get("status")
    settling = bool(job.get("settling"))
    repair_pending = False
    if status == "done" and repair_pending_func is not None:
        try:
            repair_pending = bool(repair_pending_func(job_id))
        except Exception:  # noqa: BLE001
            repair_pending = False
    public_status = "settling" if settling or (status == "done" and repair_pending) else status
    terminal = public_status in ("done", "error", "cancelled", "interrupted", "superseded")

    safe: dict = {
        "ok": True,
        "schema_version": 1,
        "job_id": job_id,
        "status": status,
        "public_status": public_status,
        "terminal": terminal,
        "settling": settling,
        "repair_pending": repair_pending,
    }
    for key in (
        "progress", "total", "done", "created", "failed", "selected",
        "source_login", "target_login", "created_at", "updated_at", "error",
    ):
        if key in job:
            safe[key] = _json_safe(job[key])
    if terminal and job.get("updated_at"):
        safe["finalized_at"] = _json_safe(job.get("updated_at"))
    result_summary = _copy_api_result_summary(job.get("result"))
    if result_summary is not None:
        safe["result_summary"] = result_summary
    raw_log = job.get("log")
    if isinstance(raw_log, list):
        safe["log"] = raw_log[-50:]
    return safe


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
    geo_validate_id_func: Optional[Callable] = None,
    repair_pending_func: Optional[Callable] = None,
    job_db_get_func: Optional[Callable] = None,
    idempotency_lookup_func: Optional[Callable] = None,
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
    job_db_get_func       = job_repository._job_db_get       (fallback статуса после рестарта)
    idempotency_lookup_func = lookup по Idempotency-Key      (повтор POST без дубля)

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

    @bp.route("/status/<job_id>", methods=["OPTIONS"])
    def copy_api_status_options(job_id: str):  # noqa: ARG001
        """Preflight для поллинга статуса с X-API-Key заголовком (A2)."""
        resp = jsonify({})
        return _copy_api_add_cors(resp, request.headers.get("Origin"))

    @bp.route("/status", methods=["OPTIONS"])
    def copy_api_status_base_options():
        """Preflight для клиентов, которые проверяют base status route до подстановки job_id."""
        resp = jsonify({})
        return _copy_api_add_cors(resp, request.headers.get("Origin"))

    @bp.route("/health", methods=["OPTIONS"])
    def copy_api_health_options():
        resp = jsonify({})
        return _copy_api_add_cors(resp, request.headers.get("Origin"))

    @bp.route("/health")
    def copy_api_health():
        """Health-check внешнего copy API. Не раскрывает секреты и состояние аккаунтов."""
        origin = request.headers.get("Origin")
        if not _copy_api_key_ok():
            return _copy_api_error("неверный или отсутствующий API-ключ", "AUTH_FAILED", 401, origin)
        return _copy_api_add_cors(
            jsonify({"ok": True, "service": "direct-copy", "api": "v1"}),
            origin,
        )

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
            target_feed_url str        путь фида от / (дефолт: _COPY_DEFAULT_FEED_PATH)
            feed_map        object     (опц.) маппинг фидов источника → цели
            agency          str        (опц.) агентство

        Ответ 200:
            {job_id, login, agency, total, kind, ahead, existing?, note?}

        Ошибки: 400 / 401 / 409 / 500 с {ok:false, error, error_code}
        """
        origin = request.headers.get("Origin")
        if not _copy_api_key_ok():
            return _copy_api_error("неверный или отсутствующий API-ключ", "AUTH_FAILED", 401, origin)

        body = request.get_json(silent=True) or {}
        campaign_ids, campaign_ids_error = validate_api_campaign_ids(body)
        source_login = (body.get("source_login") or "").strip()
        target_login = (body.get("target_login") or "").strip()
        counter_id = _num(body.get("counter_id"), 0)
        goal_id = _num(body.get("goal_id"), 0)
        target_domain = (body.get("target_domain") or "").strip()
        target_city = (body.get("target_city") or "").strip()
        target_region = (body.get("target_region") or "").strip()
        target_feed_url = (body.get("target_feed_url") or copy_default_feed_path).strip()
        target_cleanup = (body.get("target_cleanup") or "none").strip()
        mode = (body.get("mode") or "auto").strip()
        # A1: geo_mode с дефолтом как в _copy_start_impl (routes_copy.py:224).
        # mode="other" → keep (нет city/region); mode="auto" → replace (привычный путь).
        geo_mode = default_geo_mode(body, mode)

        # Валидация обязательных полей
        if not source_login or not target_login:
            return _copy_api_error("source_login и target_login обязательны", "VALIDATION_ERROR", 400, origin)
        if campaign_ids_error:
            return _copy_api_error(campaign_ids_error, "INVALID_CAMPAIGN_IDS", 400, origin)
        if not counter_id:
            return _copy_api_error("counter_id обязателен", "VALIDATION_ERROR", 400, origin)
        if not goal_id:
            return _copy_api_error("goal_id обязателен", "VALIDATION_ERROR", 400, origin)
        if not target_domain:
            return _copy_api_error("target_domain обязателен", "VALIDATION_ERROR", 400, origin)
        geo_error, _geo_region_ids_parsed = validate_other_geo(
            body, mode, geo_mode, geo_validate_id_func=geo_validate_id_func, surface="api"
        )
        if geo_error:
            return _copy_api_error(geo_error, "INVALID_GEO", 400, origin)
        if mode == "auto" and not (target_city or target_region):
            return _copy_api_error(
                "target_city или target_region обязательны при mode=auto",
                "VALIDATION_ERROR",
                400,
                origin,
            )
        if target_cleanup not in ("none", "delete_drafts", "archive"):
            return _copy_api_error(
                "target_cleanup допустимо: none, delete_drafts, archive",
                "VALIDATION_ERROR",
                400,
                origin,
            )
        if target_feed_url and not target_feed_url.startswith("/"):
            return _copy_api_error(
                "target_feed_url: во внешнем API допустим только путь от /",
                "INVALID_FEED_URL",
                400,
                origin,
            )
        feed_map = parse_feed_map(body)
        if mode == "other":
            image_mode = (body.get("image_mode") or "copy").strip()
            if image_mode not in ("copy", "upload"):
                return _copy_api_error(
                    "image_mode допустимо: copy, upload",
                    "VALIDATION_ERROR",
                    400,
                    origin,
                )
            image_hashes = parse_image_hashes(body)
            if image_mode == "upload":
                if not isinstance(body.get("image_hashes"), list) or not image_hashes:
                    return _copy_api_error(
                        "image_mode='upload': передайте image_hashes как список с хотя бы одним image_hash",
                        "VALIDATION_ERROR",
                        400,
                        origin,
                    )
        else:
            image_mode = ""
            image_hashes = []
        # Проверка владельца счётчика (если DI подключён)
        if counter_foreign_owner is not None:
            try:
                owner = counter_foreign_owner(counter_id, target_login)
            except Exception:  # noqa: BLE001
                owner = None
            if owner:
                return _copy_api_error(
                    f"счётчик {counter_id} принадлежит «{owner}», а не «{target_login}»",
                    "COUNTER_FOREIGN_OWNER",
                    400,
                    origin,
                )

        # Тело джобы по образцу _copy_start_impl из routes_copy.py
        # Явный allowlist: только известные поля, произвольные ключи клиента не пробрасываются.
        _JOB_BODY_ALLOWLIST = {
            "source_login", "target_login", "campaign_ids", "target_domain",
            "target_city", "target_region", "counter_id", "goal_id",
            "target_feed_url", "feed_map", "target_cleanup", "mode",
            "image_mode", "image_hashes", "geo_mode",
        }
        job_body = {k: v for k, v in body.items() if k in _JOB_BODY_ALLOWLIST}
        job_body["mode"] = mode              # из валидации роута, не из payload клиента
        job_body["geo_mode"] = geo_mode       # A1: нормализованный geo_mode (не из payload)
        if _geo_region_ids_parsed:
            job_body["geo_region_ids"] = _geo_region_ids_parsed  # A1: валидированный список
        job_body["_kind"] = "copy_campaigns"
        job_body["login"] = target_login
        job_body["created_by"] = "copy-api"
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
        job_body["feed_map"] = feed_map
        if mode == "other":
            job_body["image_mode"] = image_mode
            job_body["image_hashes"] = image_hashes if image_mode == "upload" else []
        else:
            job_body.pop("image_mode", None)
            job_body.pop("image_hashes", None)

        resolved_ag = resolve_agency_hint(target_login, (body.get("agency") or "").strip())
        if resolved_ag:
            job_body["agency"] = resolved_ag

        idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
        if len(idempotency_key) > 128:
            return _copy_api_error("Idempotency-Key: максимум 128 символов", "INVALID_IDEMPOTENCY_KEY", 400, origin)
        payload_hash = _copy_api_payload_hash(job_body)
        if idempotency_key and idempotency_lookup_func is not None:
            existing_row = idempotency_lookup_func(idempotency_key)
            if existing_row:
                existing_body = _as_dict(existing_row.get("body"))
                existing_hash = existing_body.get("_copy_api_payload_hash")
                if existing_hash and existing_hash != payload_hash:
                    return _copy_api_error(
                        "Idempotency-Key уже использован с другим payload",
                        "IDEMPOTENCY_CONFLICT",
                        409,
                        origin,
                    )
                existing_job_id = str(existing_row.get("job_id") or "")
                return _copy_api_add_cors(
                    jsonify({
                        "ok": True,
                        "job_id": existing_job_id,
                        "login": existing_row.get("login") or target_login,
                        "agency": existing_row.get("agency") or resolved_ag or "",
                        "total": int(existing_row.get("total") or len(campaign_ids or [])),
                        "kind": "copy_campaigns",
                        "status": existing_row.get("status"),
                        "existing": True,
                        "idempotent": True,
                        "status_url": f"/api/v1/copy/status/{existing_job_id}",
                    }),
                    origin,
                )
        if idempotency_key:
            job_body["_copy_api_idempotency_key"] = idempotency_key
            job_body["_copy_api_payload_hash"] = payload_hash

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
                    "ok": True,
                    "job_id": job_id,
                    "login": target_login,
                    "agency": resolved_ag or "",
                    "total": len(campaign_ids),
                    "kind": "copy_campaigns",
                    "ahead": ahead,
                    "existing": True,
                    "idempotent": False,
                    "note": "для этого аккаунта уже есть активная задача; дубль не создан",
                    "status_url": f"/api/v1/copy/status/{job_id}",
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
                "ok": True,
                "job_id": job_id,
                "login": target_login,
                "agency": resolved_ag or "",
                "total": len(campaign_ids),
                "kind": "copy_campaigns",
                "ahead": ahead,
                "idempotent": False,
                "status_url": f"/api/v1/copy/status/{job_id}",
            }),
            origin,
        )

    # ── GET /api/v1/copy/status/<job_id> ─────────────────────────────────────

    @bp.route("/status/<job_id>")
    def copy_api_status(job_id: str):
        """Статус / прогресс / лог задачи копирования.

        Ответ 200:
            {ok, schema_version, job_id, status, public_status, terminal, settling,
             repair_pending, progress, total, selected, source_login, target_login,
             created_at, updated_at, result_summary, error, log[-50:]}

        Секреты, session-снапшоты, внутренние поля (_kind, _web_posted и т.п.) — не отдаются.
        """
        origin = request.headers.get("Origin")
        if not _copy_api_key_ok():
            return _copy_api_error("неверный или отсутствующий API-ключ", "AUTH_FAILED", 401, origin)

        with copy_jobs_lock:
            job = dict(copy_jobs.get(job_id) or {})

        if not job and job_db_get_func is not None:
            try:
                row = job_db_get_func(job_id)
            except Exception:  # noqa: BLE001
                row = None
            if row and row.get("kind") == "copy_campaigns":
                job = _copy_api_job_from_db(row)

        if not job:
            return _copy_api_error("job не найден", "JOB_NOT_FOUND", 404, origin)

        return _copy_api_add_cors(jsonify(_copy_api_status_payload(job_id, job, repair_pending_func)), origin)

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
            return _copy_api_error("неверный или отсутствующий API-ключ", "AUTH_FAILED", 401, origin)

        if api_campaigns_func is None:
            return _copy_api_error("api_campaigns_func не подключена", "CAMPAIGNS_API_UNAVAILABLE", 503, origin)

        # _campaigns_response читает request.args["login"] изнутри (Flask-aware)
        try:
            raw = api_campaigns_func()
        except Exception:  # noqa: BLE001
            return _copy_api_error("ошибка получения кампаний", "CAMPAIGNS_FETCH_FAILED", 502, origin)

        if hasattr(raw, "headers"):
            return _copy_api_add_cors(raw, origin)
        return raw

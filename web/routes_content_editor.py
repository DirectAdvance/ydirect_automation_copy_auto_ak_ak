"""Редактор контента Direct — массовый поиск и замена AI-текстов.

Отдельная изолированная страница ``/direct/automation/content`` и её API
(``/direct/api/content-editor/*``). Назначение: контент (уточнения, быстрые
ссылки, заголовки/тексты объявлений) генерирует M3-пак и иногда выдаёт неверные
фразы. Этот сервис нужен для МАССОВОЙ коррекции — найти паттерн ошибки поиском
и заменить его во ВСЕХ кампаниях/группах аккаунта за один проход.

Загрузка использует официальный Direct API v5 как read-only источник снимка и дополняет
часть данных cookie/Grid-чтением. Запись через OAuth API v5/v501 запрещена:
массовые правки контента в кампаниях идут только через cookie/Grid writer.

Порядок правки:
  1) POST /load    — прочитать весь контент аккаунта + где что используется;
  2) POST /preview — сколько объектов затронет замена old_text→new_text (без записи);
  3) POST /replace — применить замену.

Замена по типам:
  • ad_title / ad_title2 / ad_text — ``UpdateAdaptiveTextAds`` по cookies/Grid;
  • sitelink_title — cookie/Grid flow: AddSitelinkSets + переназначение объявлений;
  • callout — Grid/cookie: создать новый callout, убрать старый id из кампаний и привязать новый.

Структурное разбиение (2026-07-27):
  Вся логика помощников, маршруты и регистраторы вынесены в:
    content_editor_helpers.py  — v5/Grid/UAC helpers, jobs-DB, _load_account, constants
    content_jobs_routes.py     — /jobs /status /cancel
    content_sitelinks_routes.py — sitelink helpers + sitelinks/reorder_async + sitelinks/assign_async
    content_callouts_routes.py  — callout helpers + callouts/assign_async
    content_replace_routes.py   — load/preview/replace routes + _do_replace dispatcher
  Этот файл регистрирует главный blueprint, делает backward-compat re-exports
  (test_routes.py монкейпатчит имена из этого модуля), держит make_job_executor.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Callable

from flask import jsonify, render_template, request, session

# ── Sub-module registration ────────────────────────────────────────────────────
from ..content.content_dashboard_routes import register_content_dashboards
from ..content.content_images_routes import register_image_routes
from ..content.content_price_check_routes import register_price_check_routes
from ..content.content_renames_routes import register_rename_routes
from ..content.content_jobs_routes import register_jobs_routes
from ..content.content_sitelinks_routes import register_sitelinks_routes
from ..content.content_callouts_routes import register_callouts_routes
from ..content.content_replace_routes import register_replace_routes

# ── Backward-compat re-exports (test_routes.py monkeypatches these names on this module) ──
# IMPORTANT: these names MUST stay at module level here because:
#   1. test_routes.py patches ``content_editor.<name>`` and calls make_job_executor();
#   2. the executor's closures look up names in this module's namespace at call time;
#   3. content_images_routes.py lazy-imports this module and accesses rce._v5_paginate etc.
from ..content.content_editor_helpers import (  # noqa: F401
    _result,
    _v5_paginate,
    _v5_paginate_campaign_batches,
    _strip_campaign_subfield_names,
    _grid_client,
    CE_JOBS_TABLE,
    CE_DAILY_JOB_CAP,
    CE_EKB_DAY_SQL,
    _jobs_db,
    _jobs_exec,
    ensure_jobs_table,
    _content_job_public,
    _scope_check,
    _grid_campaign_callout_ids,
    _grid_tp67_campaigns,
    _ad_texts,
    _ad_href,
    _href_host_path,
    _href_scheme,
    _ad_content_rows,
    _extract_uac_text_list,
    _unwrap_uac_response,
    _uac_text_item_text,
    _frag_trim,
    _FRAG_INVISIBLE,
    _uac_replace_text_items,
    _UAC_PATCH_FULL_KEYS,
    _uac_campaign_patch_payload,
    _uac_patch_campaign_texts,
    _uac_read_client,
    _uac_client,
    _uac_cids_from_targets,
    _load_account,
    _AD_FIELD,
    _AD_API_FIELD,
    _SITELINK_TYPES,
    _SITELINK_FIELD,
    _SITELINK_JOB_TYPES,
    _campaign_callout_ids,
    _ads_using_set,
    _match_targets,
    _already_applied_sitelink_result,
    _content_regular_campaign_ids,
    _content_inventory_for_campaigns,
    _grid_clear_text_ads_overrides,
    _grid_clear_responsive_ads_overrides,
    _clear_ad_level_asset_overrides,
)
from ..content.content_editor_helpers import ensure_content_job_agent_column  # noqa: F401
from ..content.content_sitelinks_routes import _validate_permutation  # noqa: F401  (monkeypatched)
from ..content.content_replace_routes import _do_replace  # noqa: F401  (monkeypatched)


# ── make_job_executor — MUST stay here ────────────────────────────────────────
# test_routes.py patches content_editor._load_account / content_editor._do_replace
# then calls content_editor.make_job_executor(...). The execute() closure references
# those names in THIS module's namespace at call time → must be defined here, not in helpers.

def make_job_executor(*, victory_conn, token_for_login, direct_tokens, v5_call, v501_svc):
    """Исполнитель задания замены без Flask-контекста — используется воркером очереди."""

    def execute(job: dict, is_cancelled=lambda: False) -> dict:
        allowed = job.get("access_directologists")
        ok, err = _scope_check(victory_conn, job["login"], allowed)
        if not ok:
            raise RuntimeError(err)
        tokens = direct_tokens()
        if not tokens:
            raise RuntimeError("нет агентских токенов (loader.load_yandex_direct вернул пусто)")
        token, _agency = token_for_login(job["login"], "", tokens)
        if not token:
            raise RuntimeError(f"ни один агентский токен не открывает аккаунт {job['login']}")
        job_agency = str(job.get("agency") or "").strip()

        def _job_grid_client(login: str):
            if not job_agency:
                return _grid_client(login)
            from ..core import campaign as cmc
            from ..clients.grid_finalize import get_grid_client

            cookie = cmc.pick_working_cookie(
                login,
                accounts=(job_agency,),
                force_refresh=True,
            )
            return get_grid_client(login, cookie=cookie)

        # Эти типы работают строго по payload и не требуют текстового снимка аккаунта.
        if (job.get("type") or "") in {"image_replace", "campaign_rename"}:
            if is_cancelled():
                return {"cancelled": True}
            return _do_replace(token, job["login"], job["type"], job["old_text"],
                               job["new_text"], {}, v5_call, v501_svc,
                               mode=(job.get("mode") or "exact"),
                               grid_client_factory=_job_grid_client)
        # campaign-level sitelinks нужны ТОЛЬКО заданиям замены набора уровня кампании
        # (sitelink_title/description). Для ad_title/ad_text/callout/ad_href не гоняем
        # лишний Grid-round-trip (см. блок 3c в _load_account).
        _need_cl_sitelinks = (job.get("type") or "") in _SITELINK_JOB_TYPES
        content = _load_account(
            token, job["login"], v5_call,
            include_adgroups=(job.get("type") or "") != "ad_href",
            include_campaign_sitelinks=_need_cl_sitelinks,
            include_uac_campaigns=(job.get("type") or "") != "ad_href",
            include_callouts=(job.get("type") or "") == "callout",
        )
        if content.get("error"):
            raise RuntimeError(content["error"])
        if is_cancelled():
            return {"cancelled": True}
        return _do_replace(token, job["login"], job["type"], job["old_text"], job["new_text"],
                           content, v5_call, v501_svc, mode=(job.get("mode") or "exact"),
                           grid_client_factory=_job_grid_client)

    return execute


# ── Route registration ─────────────────────────────────────────────────────────

def register_content_editor_routes(
    bp,
    access,
    *,
    victory_conn: Callable,
    token_for_login: Callable,
    direct_tokens: Callable,
    v5_call: Callable,
    v501_svc: Callable,
    default_status: str = "Контекст активно",
    exclude_directologs: list[str] | None = None,
    balance_response: Callable | None = None,
    check_blocks_response: Callable | None = None,
    victory_conn_rw: Callable | None = None,
) -> None:
    """Регистрирует изолированную страницу редактора контента и её API."""
    exclude_directologs = exclude_directologs or []

    def _resolve_access_cfg_path() -> Path:
        """Файл доступа редактора контента = plaintext-пароли → живёт в .secret/ (вне git и
        вне Mutagen; на LXC101 своя копия). Ищем .secret/ вверх по дереву (как loader.py).
        Fallback — старое место рядом с модулем (совместимость на время переноса)."""
        here = Path(__file__).resolve()
        for parent in here.parents:
            cand = parent / ".secret" / "content_editor_access.json"
            if cand.exists():
                return cand
        return here.parent / "content_editor_access.json"

    access_cfg_path = _resolve_access_cfg_path()

    def _access_cfg() -> dict:
        if not access_cfg_path.exists():
            return {"users": []}
        try:
            data = json.loads(access_cfg_path.read_text(encoding="utf-8"))
        except Exception:
            return {"users": []}
        if not isinstance(data, dict):
            return {"users": []}
        users = []
        for row in data.get("users") or []:
            if not isinstance(row, dict):
                continue
            username = str(row.get("username") or "").strip()
            if not username:
                continue
            status = "blocked" if row.get("status") == "blocked" else "active"
            users.append({
                "username": username,
                "fio": str(row.get("fio") or "").strip(),
                "password": str(row.get("password") or ""),
                "status": status,
                "directologists": [
                    str(x).strip()
                    for x in (row.get("directologists") or [])
                    if str(x).strip()
                ],
            })
        data["users"] = users
        return data

    def _save_access_cfg(data: dict) -> None:
        access_cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _content_full_access() -> bool:
        return bool(session.get("is_admin") or session.get("content_admin"))

    def _content_user_record() -> dict | None:
        username = (session.get("username") or "").strip()
        if not username:
            return None
        for row in _access_cfg().get("users") or []:
            if (row.get("username") or "").strip() == username:
                return row
        return None

    def _allowed_directologists() -> list[str] | None:
        if _content_full_access():
            return None
        row = _content_user_record()
        if not row:
            return []
        if row.get("status") == "blocked":
            return []
        return [str(x).strip() for x in (row.get("directologists") or []) if str(x).strip()]

    def _login_allowed_for(login: str, allowed: list[str] | None) -> tuple[bool, str]:
        return _scope_check(victory_conn, login, allowed)

    def _login_allowed(login: str) -> tuple[bool, str]:
        return _login_allowed_for(login, _allowed_directologists())

    def _admin_allowed() -> bool:
        return bool(session.get("is_admin"))

    def _content_tools_allowed() -> bool:
        """Доп. вкладки content-editor для active scoped-user и full-access."""
        if _content_full_access():
            return True
        return bool(_allowed_directologists())

    def _token(login: str):
        tokens = direct_tokens()
        if not tokens:
            return None, None, "нет агентских токенов (loader.load_yandex_direct вернул пусто)"
        token, agency = token_for_login(login, "", tokens)
        if not token:
            return None, None, (f"ни один агентский токен не открывает аккаунт {login} — "
                                f"проверьте доступ агентства и актуальность OAuth-токенов")
        return token, agency, None

    def _load_with_index(token: str, login: str, *,
                         include_campaign_sitelinks: bool = True,
                         include_uac_campaigns: bool = True,
                         include_callouts: bool = True) -> dict:
        # _load_account уже строит индекс _ads_by_set (без второго запроса ads.get).
        # include_campaign_sitelinks=False пропускает Grid-round-trip блока 3c —
        # для /links и preview/replace НЕ-sitelink типов campaign-level наборы не нужны.
        return _load_account(token, login, v5_call,
                             include_campaign_sitelinks=include_campaign_sitelinks,
                             include_uac_campaigns=include_uac_campaigns,
                             include_callouts=include_callouts)

    # Исполнение заданий вынесено в direct-content-worker.service (make_job_executor).
    try:
        ensure_jobs_table()
        ensure_content_job_agent_column(_jobs_exec, CE_JOBS_TABLE)
    except Exception as e:
        print(f"[content-editor] ensure_jobs_table failed: {e}", flush=True)
        raise

    # ── Страница ──────────────────────────────────────────────────────────────

    @bp.route("/automation/content")
    @access
    def content_editor_page():
        return render_template("direct/content_editor.html")

    @bp.route("/automation/content/admin")
    @access
    def content_editor_admin_page():
        if not _admin_allowed():
            return jsonify({"error": "Forbidden"}), 403
        return render_template("direct/content_admin.html")

    @bp.route("/api/content-editor/me")
    @access
    def ce_me():
        username = (session.get("username") or "").strip()
        # is_admin — РЕАЛЬНЫЙ админ (не content_admin): для фич уровня «только is_admin»
        # (сверка цен). full_access = is_admin OR content_admin — для остальной админки.
        is_admin = bool(session.get("is_admin"))
        if _content_full_access():
            return jsonify({
                "username": username or "admin",
                "fio": "Администратор",
                "full_access": True,
                "is_admin": is_admin,
                "can_extra_tabs": True,
                "can_queue": True,
                "directologists": None,
            })
        row = _content_user_record() or {}
        return jsonify({
            "username": username,
            "fio": str(row.get("fio") or "").strip(),
            "full_access": False,
            "is_admin": is_admin,
            "can_extra_tabs": _content_tools_allowed(),
            "can_queue": _content_tools_allowed(),
            "directologists": [
                str(x).strip()
                for x in (row.get("directologists") or [])
                if str(x).strip()
            ],
        })

    def _pairs_allowed() -> tuple[bool, str]:
        """Пары {login, agency} из запроса — только свои аккаунты (по директологам)."""
        allowed = _allowed_directologists()
        if allowed is None:
            return True, ""
        if not allowed:
            return False, "Доступы не выданы"
        logins = [str((p or {}).get("login") or "").strip()
                  for p in (request.json or {}).get("pairs") or []]
        logins = sorted({x for x in logins if x})
        if not logins:
            return True, ""
        conn = victory_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT count(DISTINCT login_key), "
                "count(DISTINCT login_key) FILTER "
                "(WHERE directologist IS NULL OR directologist <> ALL(%s)) "
                "FROM public.local_gsheet_sites "
                "WHERE direction='Авто' AND login_key = ANY(%s)",
                (allowed, logins),
            )
            found, cnt_foreign = cur.fetchone()
        finally:
            conn.close()
        if cnt_foreign:
            return False, "в запросе чужие аккаунты"
        if found < len(logins):
            # неизвестный логин ≠ свой: иначе можно читать балансы любых аккаунтов агентств
            return False, "в запросе неизвестные аккаунты"
        return True, ""

    if balance_response is not None:
        @bp.route("/api/content-editor/balance", methods=["POST"])
        @access
        def ce_balance():
            ok, err = _pairs_allowed()
            if not ok:
                return jsonify({"error": err}), 403
            return balance_response()

    if check_blocks_response is not None:
        @bp.route("/api/content-editor/check_blocks", methods=["POST"])
        @access
        def ce_check_blocks():
            ok, err = _pairs_allowed()
            if not ok:
                return jsonify({"error": err}), 403
            return check_blocks_response()

    # Read-only дашборды (директологи / аккаунты / 404) — вынесены в content_dashboard_routes.py
    register_content_dashboards(
        bp,
        access,
        victory_conn=victory_conn,
        _allowed_directologists=_allowed_directologists,
        _admin_allowed=_admin_allowed,
        _content_tools_allowed=_content_tools_allowed,
        default_status=default_status,
        exclude_directologs=exclude_directologs,
    )

    @bp.route("/api/content-editor/admin/access", methods=["GET"])
    @access
    def ce_admin_access_get():
        if not _admin_allowed():
            return jsonify({"error": "Forbidden"}), 403
        return jsonify(_access_cfg())

    @bp.route("/api/content-editor/admin/access", methods=["POST"])
    @access
    def ce_admin_access_save():
        if not _admin_allowed():
            return jsonify({"error": "Forbidden"}), 403
        data = request.json or {}
        users = []
        for row in data.get("users") or []:
            username = str(row.get("username") or "").strip()
            if not username:
                continue
            password = str(row.get("password") or "")
            status = "blocked" if row.get("status") == "blocked" else "active"
            directologists = []
            for d in row.get("directologists") or []:
                d = str(d or "").strip()
                if d and d not in directologists:
                    directologists.append(d)
            users.append({
                "username": username,
                "fio": str(row.get("fio") or "").strip(),
                "password": password,
                "status": status,
                "directologists": directologists,
            })
        _save_access_cfg({"users": users})
        return jsonify({"ok": True, "users": users})

    # ── Сверка цен (admin-only): Direct ↔ фиды → расхождения → заливка ─────────
    # Весь блок вынесен в content_price_check_routes.py (self-contained, RW-only).
    if victory_conn_rw is not None:
        register_price_check_routes(
            bp,
            access,
            victory_conn=victory_conn,
            victory_conn_rw=victory_conn_rw,
            token_for_login=token_for_login,
            direct_tokens=direct_tokens,
            v5_call=v5_call,
            _allowed_directologists=_allowed_directologists,
            _content_full_access=_content_full_access,
            _login_allowed_for=_login_allowed_for,
            _admin_allowed=_admin_allowed,
            _jobs_exec=_jobs_exec,
            CE_JOBS_TABLE=CE_JOBS_TABLE,
            default_status=default_status,
            exclude_directologs=exclude_directologs,
        )

    # ── Общий хвост постановки задачи content-editor в очередь ───────────────
    def _enqueue_content_job(login, agency, db_type, old_val, new_val, mode_val,
                             campaign_count, *, resp_type=None):
        """Общий хвост постановки задачи content-editor в очередь.

        day-cap → job_id → INSERT → ahead → JSON-ответ. Различаются только значения,
        пишущиеся в строку (db_type/old_val/new_val/mode_val) и наличие поля ``type``
        в ответе (``resp_type``). Порядок ключей ответа сохранён 1:1 с прежним."""
        day_cnt = _jobs_exec(
            f"SELECT count(*) AS c FROM {CE_JOBS_TABLE} "
            f"WHERE login=%s AND status<>'cancelled' AND created_at >= {CE_EKB_DAY_SQL}",
            (login,), "one")["c"]
        if day_cnt >= CE_DAILY_JOB_CAP:
            return jsonify({"error": f"лимит {CE_DAILY_JOB_CAP} заданий на аккаунт в сутки "
                                     f"(уже поставлено {day_cnt})"}), 429
        job_id = "ce_" + uuid.uuid4().hex[:12]
        allowed = _allowed_directologists()
        _jobs_exec(
            f"INSERT INTO {CE_JOBS_TABLE} "
            "(job_id, username, login, agency, type, old_text, new_text, mode, campaign_count, access_directologists) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (job_id, (session.get("username") or "").strip(), login, agency or "", db_type,
             old_val, new_val, mode_val, campaign_count,
             json.dumps(allowed, ensure_ascii=False) if allowed is not None else None))
        ahead = _jobs_exec(
            f"SELECT count(*) AS c FROM {CE_JOBS_TABLE} WHERE status='queued' "
            f"AND created_at < (SELECT created_at FROM {CE_JOBS_TABLE} WHERE job_id=%s)",
            (job_id,), "one")["c"]
        resp = {"ok": True, "job_id": job_id, "login": login, "agency": agency}
        if resp_type is not None:
            resp["type"] = resp_type
        resp.update({"status": "queued", "total": 1, "ahead": ahead,
                     "campaign_count": campaign_count})
        return jsonify(resp)

    def _queued_ahead_map() -> dict:
        rows = _jobs_exec(
            f"SELECT job_id FROM {CE_JOBS_TABLE} WHERE status='queued' ORDER BY created_at",
            (), "all") or []
        return {r["job_id"]: i for i, r in enumerate(rows)}

    def _job_owned(row: dict) -> bool:
        if _content_full_access():
            return True
        return (row.get("username") or "") == (session.get("username") or "").strip()

    # ── Replace routes (load / preview / replace / replace_async / links / ad-content) ──
    register_replace_routes(
        bp,
        access,
        v5_call=v5_call,
        v501_svc=v501_svc,
        _login_allowed=_login_allowed,
        _content_tools_allowed=_content_tools_allowed,
        _token=_token,
        _load_with_index=_load_with_index,
        _enqueue_content_job=_enqueue_content_job,
    )

    # ── Sitelinks routes (reorder_async / assign_async) ────────────────────────
    register_sitelinks_routes(
        bp,
        access,
        _login_allowed=_login_allowed,
        _content_tools_allowed=_content_tools_allowed,
        _token=_token,
        _enqueue_content_job=_enqueue_content_job,
    )

    # ── Callouts routes (assign_async) ────────────────────────────────────────
    register_callouts_routes(
        bp,
        access,
        _login_allowed=_login_allowed,
        _content_tools_allowed=_content_tools_allowed,
        _token=_token,
        _enqueue_content_job=_enqueue_content_job,
    )

    # ── Jobs routes (jobs / status / cancel) ──────────────────────────────────
    register_jobs_routes(
        bp,
        access,
        _content_full_access=_content_full_access,
        _queued_ahead_map=_queued_ahead_map,
        _job_owned=_job_owned,
    )

    # ── Смена изображения (admin-only) ─────────────────────────────────────────
    # Весь блок вынесен в content_images_routes.py. Регистрируется в САМОМ КОНЦЕ:
    # ручкам нужен уже определённый замыкатель _enqueue_content_job.
    register_image_routes(
        bp,
        access,
        v5_call=v5_call,
        _login_allowed=_login_allowed,
        _admin_allowed=_admin_allowed,
        _token=_token,
        _enqueue_content_job=_enqueue_content_job,
    )

    # ── Смена названий (admin-only) ────────────────────────────────────────────
    # Переименование кампаний и групп через Grid без изменения других настроек.
    # Review-first: отдельный GET-preview перед apply_async.
    register_rename_routes(
        bp,
        access,
        v5_call=v5_call,
        _login_allowed=_login_allowed,
        _admin_allowed=_admin_allowed,
        _token=_token,
        _enqueue_content_job=_enqueue_content_job,
    )

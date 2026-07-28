"""
Direct Automation runtime — domain wiring without Flask route registration.

Веб-модуль seoadvanced.ru: создание «Мастер кампаний» / Товарных РК в Я.Директе,
работа с аккаунтами (баланс/блокировки/ассеты), и ИИ-генерация ПРОМОАКЦИЙ в стиле
агентов-«слепков директологов» через локальную LLM на M3.

Подробная документация модуля — см. ./README.md (доступ, источники данных,
эндпоинты, агенты, лимиты промо, публикация через grid/api).

Доступ: @_direct_access = _service_required_any("work", "work:direct") — НЕ только админ
(админ bypass; обычный юзер с сервис-ключом — тоже). Совпадает с _nav.html и app.py.

Вендорные движки: ./campaign.py (UAC мастер/товарные), ./promo.py (промо через grid/api),
./ai_agents.py (профили агентов + промпты). Папка самодостаточна; нужен .secret/loader.py
выше по дереву (куки главпотока, токены Директа/Метрики, креды БД Victory).
"""
import copy
import json
import os
import re
import sys
import threading
import time
import tempfile
import hashlib
import random
import importlib.util
import posixpath
from pathlib import Path

import uuid
from flask import render_template, request, jsonify, current_app, session, send_file

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Доступ к Директу: админ (bypass внутри декоратора) ИЛИ юзер с ключом
# "work" (parent) / "work:direct". Совпадает с навигацией (_nav.html) и
# реестром _BUILTIN_SECTIONS в app.py — юзер с грантом видит ссылку И может всё.
# РАЗРУШИТЕЛЬНЫЕ операции (остановить ВСЕ РК, удалить ВСЕ черновики) — отдельный, более узкий грант
# "work:direct:danger". Админ — bypass (внутри декоратора). Обычный юзер с одним лишь "work"/"work:direct"
# создавать может, но массово останавливать/удалять — НЕТ (нужен явный danger-грант). Безопасный дефолт:
# нет danger-гранта → разрушительные операции доступны только админу.

from . import campaign as cmc  # vendored движок
from . import grid_finalize as gf  # Grid-докрутка ЕПК (tp1-tp5): места показа/ассеты/инварианты
from . import grid_create as gc  # Куки-движок создания/удаления (Grid web-api, без баллов v5)
from . import kontent_pack as kp  # чтение контент-пака с M3 (/opt/neuro_kontent)
from . import llm_providers as _llmp   # M3/OpenRouter (вынесено из blueprint; heartbeat инъектим ниже)
from .llm_providers import (           # ре-экспорт: внутренние вызовы + deps-словари модулей
    _M3_LLM_URL, _M3_LLM_TIMEOUT, _M3_LLM_URLS_14B, _M3_LLM_URL_72B,
    _M3_LLM_TIMEOUT_14B, _M3_LLM_REPAIR_TIMEOUT, _M3_CONTENT_IDLE_TIMEOUT, _OPENROUTER_LLM_MODEL,
    _m3_llm_probe, _m3_complete, _m3_complete_url, _m3_complete_parallel,
    _openrouter_api_key, _openrouter_probe, _openrouter_completion_probe,
    _or_complete_url, _llm_pair_for,
    _strip_error_leak, _has_error_leak,
)
from . import text_norm as _tn        # анти-AI санитайзеры (вынесено; _bad_credit_payment_range инъектим ниже)
from .text_norm import (              # ре-экспорт: внутренние вызовы + deps-словари (globals-lookup)
    _replace_emdash, _replace_sep_hyphen, _is_bad_start, _trim_to_word,
    _strip_dangling_num_tail, _strip_dangling_word_tail, _sanitize_content,
    _normalize_numeric_suffixes_bp, _strip_credit_rate, _cap_first, _sentence_case,
    _split_utp, _has_stamp, _alternate_rhythm, _dedup_by_first_word, _has_number,
    _bad_ad_title, _bad_ad_text, _bad_ad_sitelink, _RSYA_TEXT_MAX,
)
from . import city_morph as _cm        # склонения/замена городов (вынесено; _title2_blocklist инъектим ниже)
from .city_morph import (              # ре-экспорт: внутренние вызовы + deps модулей
    _city_locative, _content_city, _RU_CITIES, _replace_foreign_city, _drop_foreign_city_keywords,
)
from . import promo_gen as _pg         # генерация/валидация промо (вынесено; _victory_conn инъектим ниже)
from .promo_gen import (               # ре-экспорт: внутренние вызовы + deps (routes_ai, create_content)
    _promo_extract_json, _extract_title_candidates, _extract_text_candidates,
    _promo_validate, _promo_amount_steps, _promo_preview, _promo_ctx,
)
from . import campaign_naming as _cn   # кодер-имена + ротатор Title2 (вынесено; 4 DI инъектим ниже)
from .campaign_naming import (         # ре-экспорт: deps модулей (в т.ч. globals-lookup _master_product_deps)
    _ag_part1_map, _ct_for_name, _title2_blocklist, _next_title2,
    _coder_name_real_brand, _brand_ct_from_coder, _brand_from_coder,
)
from . import model_urls               # URL-хелперы (чистый, без DI)
from .model_urls import (
    _strip_url_query, _brand_level_url, _is_site_domain_name, _model_page_href,
)
# _title2_blocklist теперь из campaign_naming — инъектим его в city_morph (перенесено из середины).
_cm.configure({"_title2_blocklist": _title2_blocklist})
from . import text_gen as _tg          # генерация текстов/заголовков (вынесено; 7 DI инъектим ниже)
from .text_gen import (                # ре-экспорт: внутр. вызовы + deps-словари (globals-lookup) + _bad_credit_payment_range→text_norm
    _title_from_template, _PCT_DISC_RE, _coherent_discounts, _variant_norm_key,
    _dedup_prefix_absorb, _fill_variants, _rotated_content_window, _filter_group_keywords,
    _own_brand_tokens, _display_brand, _rsya_texts, _diverse_text_offers,
    _fallback_master_titles, _bad_credit_payment_range, _coherent_payments, _discount_pcts,
    _dominant_discount_pct, _fill_title, _brand_title_set, _rsya_titles,
)
from . import ai_content as _aic       # AI-контент объявлений + слепок-контент (вынесено; 4 DI инъектим ниже)
from .ai_content import (              # ре-экспорт: deps-словари (globals-lookup) + _bp._seed_slepok_content (seed entrypoint)
    _CONTENT_CACHE, _CONTENT_CACHE_LOCK, _content_cache_key, _content_complete,
    _ai_campaign_content_for_item, _ai_group_content, _slepok_content_ensure,
    _slepok_content_get, _slepok_content_save, _gen_campaign_content, _seed_slepok_content,
    _account_content_get, _account_content_put,   # #16: account-level content reuse (в пределах прохода)
)
from . import copy_engine as _ce       # копирование кампаний 1:1 (вынесено; 28 DI инъектим ниже)
from . import copy_verify as _cv       # сверка source↔target после копирования (report-only)
from .copy_engine import (             # ре-экспорт: _create_worker_loop/_ensure_copy_worker/_wire_copy_routes
    _copy_run_job, _copy_job_upsert, _copy_feeds_preview, _copy_jobs_recover,
    _COPY_JOBS, _COPY_JOBS_LOCK, _COPY_DEFAULT_FEED_PATH,
)
from . import repair_gate as rgate  # read-only repair-gate helpers
from . import repair_executor as rex  # scoped repair executors (cookie/Grid-first)
from . import repair_auto as rauto  # repair orchestration without Flask/DB wiring
from . import verification_service as vsvc  # live verification orchestration without Flask
from . import blueprint_targeting as _btg  # ct→сегмент классификатор + профиль таргетинга (DI ниже)
from . import slepki_editor as _sed  # редактор структуры/ключей слепков (edit-джобы очереди, DI ниже)
from .blueprint_targeting import (         # ре-экспорт: внутр. вызовы + внешний from direct.blueprint import _ct_segment
    _gc_ct, _ct_is_model_map, _ct_segment_map, _ct_segment, _seg_canon, _model_cts,
    _segment_donor, _targeting_profile, _slepok_tp_modes, _slepok_profile_excludes_tp,
    _slepki_structure_for_ui, _donor_tp4_models_map, _pack_for_item,
    _slepok_is_auto, _non_auto_slepki, _non_auto_site_types, _slepki_pack_facts,
    _slepki_pack_signature,
)
from . import blueprint_metrika as _bmt    # Метрика (счётчики/цели) + гео-справочник Директа (DI ниже)
from .blueprint_metrika import (           # ре-экспорт: внутр. вызовы (routes/DI-словари)
    _parse_counter_ids, _metrika_goals_for, _counter_foreign_owner,
    _geo_load, _geo_id, _geo_name_by_id, _geo_type_by_id, _metrika_token, _goal_vse_formy,
)
from . import blueprint_content_rules as _bcr  # правила вкладки «Контент» + фильтрация ассетов (DI ниже)
from .blueprint_content_rules import (     # ре-экспорт: внутр. вызовы + route-registration (_CONTENT_RULES_CACHE)
    _content_rules_ensure, _content_rules_map, _asset_key_from_local, _manual_rule_lookup_key,
    _content_rule_key, _ct_allowed_for, _content_allowed_list, _content_slepok_list,
    _slepok_allowed_for, _content_only_this_ct, _filter_content_assets, _prioritized_content_assets,
    _explicit_content_assets_for, _ahash_distance, _dedupe_content_assets_for_ui, _CONTENT_RULES_CACHE,
)
from .direct_repository import victory_conn as _victory_conn, victory_conn_rw as _victory_conn_rw
from .yandex_gateway import (
    LIVE_V4_URL as _LIVE_V4, V5_URL as _V5, V501_URL as _V501,
    GRID_URL as _GRID_URL, UNITS_PER_CAMPAIGN as _UNITS_PER_CAMPAIGN,
    direct_tokens as _direct_tokens, v5_get as _v5_get, v5_units as _v5_units,
    bounded_post as _bounded_post, v5_call as _v5_call, v501_call as _v501_call,
    v501_svc as _v501_svc, v5_err as _v5_err, is_transient as _is_transient,
    agency_override_get as _agency_override_get, resolve_agency_hint as _resolve_agency_hint,
    agency_override_save as _agency_override_save, token_for_login as _token_for_login,
    units_alive_for_login as _units_alive_for_login, grid_list_campaigns as _grid_list_campaigns,
    grid_post as _grid_post, grid_csrf as _grid_csrf,
    block_bootstrap as _block_bootstrap, block_check as _block_check,
)

# ── Фаза 2 gateway: доступ к Директу через внутренний брокер direct-gateway (:5025) ──────────────
# gateway_client каждую функцию: HTTP к брокеру → при недоступности ФОЛБЭК на локальную (тот же
# yandex_gateway.*), плюс self-guard (в самом брокере HTTP не делается). Перецеливаем _-алиасы,
# которые ниже раздаются по DI во все под-модули → потребители переключаются прозрачно.
# Инкремент 1: ТОЛЬКО units_alive (2 потребителя, не горячий путь) — остальные алиасы пока локальны.
from . import gateway_client as _gwc  # noqa: E402
_units_alive_for_login = _gwc.gw_units_alive
from . import pack_resolver as _pack_resolver
from .pack_resolver import (
    _CALLOUT_MAX_EACH, _CALLOUT_MAX_TOTAL_DESKTOP, _CALLOUT_MAX_TOTAL_MOBILE,
    _SLEPOK_KEY, _SLEPOK_CANONICAL, _slepok_key_from_text, _selected_slepok_key,
    _m3_content_status, _m3_gate_wait, _cookies_status_response, _m3_status_response,
    _pack_preview_response, _slepok_segment_counts_response,
)
from . import account_service as _account_service
from .account_service import (
    _ACCOUNT_COLS, DEFAULT_STATUS, _EXCLUDE_DIRECTOLOGS, _TOKEN_ONLY_TYPES, _do_balance, _preflight_creds,
    _account_assets_response, _do_assets, _account_audiences_response,
    _account_prefill_response, _campaigns_response, _stop_all_response, _check_blocks_response,
    _is_tool_campaign, _delete_drafts_core, _grid_empty_unified_drafts, _sweep_empty_drafts,
    _delete_partial_campaign, _delete_drafts_response, _delete_drafts_async_response,
)
from . import job_repository as _job_repository
from .job_repository import (
    _JOB_DB_LAST, _jobs_db_init, _job_db_save, _job_db_delete, _job_db_get,
    _job_db_active_by_login, _job_db_set_status, _job_control_set,
    _job_db_web_await_feed, _job_db_web_resolve_feed, _job_db_list_recent,
    _job_db_ahead, _job_db_progress, _jobs_db_mark_stale_running,
    _next_units_reset_utc, _deferred_db_init, _deferred_save, _deferred_set_status,
    _deferred_bump_resume_at, _delayed_repair_db_init, _delayed_repair_set_status,
    _delayed_content_repair_save, _supersede_delayed_repairs_for_login,
    _ready_logins_db_init, _ready_login_upsert, _ready_login_remove,
)
from . import queue_server as _queue_server
from .queue_server import (
    _CREATE_JOBS, _CREATE_JOBS_LOCK, _CREATE_COND, _CREATE_QUEUE, _CREATE_WORKER,
    _CREATE_WATCHDOG, _JOB_TERMINAL, _CREATE_WORKERS, _CREATE_POOL_PAUSE,
    _CREATE_MAX_PER_AGENCY, _CREATE_ACTIVE_AGENCIES, _CREATE_RUNNING_TIMEOUT,
    _CREATE_FINALIZE_TIMEOUT, _CREATE_WATCHDOG_POLL, _DCR_DETACH_PARENT,
    _CREATE_DRAIN, _WORKER_POLLER, _WORKER_POLL_SEC, _JOB_MUT_LOCK,
    _JOB_HISTORY_TTL, _RESUME_DAEMON, _RESUME_MAX, _RESUME_POLL,
    _DEFERRED_STALE_HOURS, _DELAYED_REPAIR_DAEMON, _DELAYED_REPAIR_POLL,
    _DELAYED_CONTENT_REPAIR_DELAY_SECONDS, _DELAYED_FULL_REPAIR_MAX_ITERATIONS,
    _DELAYED_REPAIR_STUCK_TIMEOUT_SECONDS, _DELAYED_REPAIR_TIME_BUDGET_SECONDS,
    _DELAYED_REPAIR_MAX_RESCHEDULES, _CLAIMED_WATCHDOG_TS,
    _direct_role, _worker_request_drain, _worker_is_draining, _job_agency, _job_touch,
    _bump_job, _bump_item, _add_job_err, _jobs_db_recover, _jobs_purge_old,
    _create_watchdog_tick, _create_watchdog_loop, _ensure_create_watchdog,
    _repair_failures_nonfixable, _ready_logins_track, _requeue_missing_positions_once,
    _position_live_in_names, _plan_positions_all_live, _reconcile_parent_job_counters,
    _delayed_repair_reschedule, _child_parent_ref, _parent_update,
    _parent_absorb_child_start, _parent_absorb_child_progress, _merge_resume_into_parent,
    _cancel_children_of, _record_delayed_content_repair, _record_auto_repair_full,
    _schedule_delayed_content_repair_after_done, _run_delayed_content_repair,
    _run_delayed_finalize, _delayed_repair_daemon_loop, _ensure_delayed_repair_daemon,
    _resume_one_deferred, _deferred_enqueue_now, _resume_daemon_loop,
    _ensure_resume_daemon, _job_kind, _job_new_web, _job_new, _create_jobs_ahead,
    _agency_gate_claim, _agency_gate_release, _agency_gate_sweep, _claim_next_job,
    _create_worker_loop, _create_workers_count, _ensure_create_worker, _ensure_copy_worker,
    _worker_claim_web_jobs, _worker_adopt_job, _worker_expire_awaiting_feed,
    _worker_apply_controls, _worker_reclaim_stuck_claimed, _worker_poll_once,
    _worker_poll_loop, _ensure_worker_poller, _worker_bootstrap,
)

_HERE = Path(__file__).resolve().parent

_JSON_CACHE: dict[str, tuple[int, int, object]] = {}
_JSON_CACHE_LOCK = threading.RLock()


def _json(name: str):
    """Read package JSON with mtime invalidation.

    The structure file is about 2.5 MiB and used by several UI/context helpers in a
    single request.  Re-reading and decoding it for every helper added avoidable
    latency.  The editor writes files atomically, so ``mtime_ns + size`` gives us a
    cheap invalidation key without making UI edits stale.

    slepki_structure.json больше НЕ монолит на диске — он разбит на per-slepok файлы
    (direct/slepki/<key>.json). Собираем единый словарь в памяти через slepki_store (у него
    свой кэш по сигнатуре частей). Этот перехват — единственная точка: все читатели ходят через
    инъектированный _json (== этот), поэтому call-sites НЕ меняются.
    """
    if name == "slepki_structure.json":
        from . import slepki_store as _sstore  # noqa: PLC0415
        return _sstore.assemble()
    path = _HERE / name
    stat = path.stat()
    key = str(path)
    signature = (stat.st_mtime_ns, stat.st_size)
    with _JSON_CACHE_LOCK:
        cached = _JSON_CACHE.get(key)
        if cached and cached[:2] == signature:
            return cached[2]
    data = json.loads(path.read_text(encoding="utf-8"))
    with _JSON_CACHE_LOCK:
        _JSON_CACHE[key] = (signature[0], signature[1], data)
    return data





def _load_audiences() -> list[dict]:
    return _json("audiences_preset.json").get("audiences", [])


_TP67_RELEVANCE_CATEGORIES = [
    "EXACT_V2_MARK", "ACCESSORY_MARK", "BROADER_MARK", "ALTERNATIVE_MARK", "NARROW_MARK",
]
# UAC «Подобрать оптимальную» (tp6/tp7 группа = ТОЛЬКО автотаргетинг): HAR 34 PATCH
# /web-api/uac/campaign/{id}. Ровно эти 5 категорий (ВНИМАНИЕ: EXACT_MARK/COMPETITOR_MARK, НЕ
# EXACT_V2_MARK/NARROW_MARK), keywords=[] и socdem на полный диапазон (age_18→age_inf, оба пола).
_TP67_OPTIMAL_CATEGORIES = [
    # COMPETITOR_MARK ОБЯЗАТЕЛЕН: его отсутствие = категория исключена → в UI группы чип
    # «Запросы с упоминанием брендов конкурентов» в минус-словах (жалоба Семёна ×3, НЕ убирать!)
    "ALTERNATIVE_MARK", "ACCESSORY_MARK", "BROADER_MARK", "COMPETITOR_MARK", "EXACT_MARK",
]


def _audience_object_for_id(aid: str, preset: dict[str, dict] | None = None) -> dict:
    """UAC audience entry in the same object shape the UI uses; id-only fallback is valid."""
    sid = str(aid or "").strip()
    if not sid:
        return {}
    src = (preset or {}).get(sid)
    if src:
        return dict(src)
    if sid.startswith("249"):
        return {"id": sid, "type": "INTERESTS"}
    if sid.startswith("199"):
        return {"id": sid, "type": "APPLICATION"}
    if sid.startswith("190"):
        return {"id": sid, "type": "HOST"}
    return {"id": sid}


def _audience_objects(ids: list[str]) -> list[dict]:
    preset = {str(a.get("id")): a for a in _load_audiences() if isinstance(a, dict) and a.get("id")}
    out, seen = [], set()
    for aid in ids or []:
        sid = str(aid or "").strip()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        obj = _audience_object_for_id(sid, preset)
        if obj:
            out.append(obj)
    return out


_GLOBAL_FEED_DEFAULTS = [
    "credit-page-01-a.xml",
    "dostup-k-rasprodazhe-01-a.xml",
    "dostup-k-rasprodazhe-01-b.xml",
    "dostup-k-rasprodazhe-live-01-b.xml",
    "dostup-k-rasprodazhe-live-01-c.xml",
    "yandex-catalog-model-color.xml",
    "yandex-catalog-model-design-custom-name.xml",
    "yandex-catalog-new.xml",
    "yandex.xml",
    "yandex_auto_ext_preview.xml",
    "yandex_auto_ext_preview_benefit.xml",
    "yandex_auto_preview.xml",
    "zabronirovat-01-a.xml",
    "zabronirovat-01-b.xml",
]


def _feed_key(s: str) -> str:
    s = (s or "").strip().split("?")[0].rstrip("/")
    return os.path.basename(s).lower()


# Каталог-фиды (role='catalog') — ТОЧНЫЙ список feed_key. Только эти множит tp1-товарка (fan-out):
# у них реальные модельные листинги. Всё, чего нет в списке (лендинг/оффер-фиды: zabronirovat*,
# dostup-k-rasprodazhe*, credit-page*, сырой yandex.xml) → role='landing', в tp1 НЕ участвует.
# Матчинг ВЕЗДЕ по ТОЧНОМУ равенству нормализованного feed_key (_feed_key), НЕ по подстроке.
_CATALOG_FEED_KEYS = {
    "yandex-catalog-model-color.xml",
    "yandex-catalog-model-design-custom-name.xml",
    "yandex-catalog-new.xml",
    "yandex_auto_ext_preview.xml",
    "yandex_auto_ext_preview_benefit.xml",
    "yandex_auto_preview.xml",
}


def _feed_rules_defaults() -> list[dict]:
    return [{"name": f, "url": "/" + f, "enabled": True, "sort": i} for i, f in enumerate(_GLOBAL_FEED_DEFAULTS, 1)]


# ── Защита по времени (анти-блок аккаунта) ─────────────────────────────────────
# Глобальный лок: одновременно идёт только ОДНА тяжёлая выгрузка по куки/API —
# нельзя дёргать из разных вкладок параллельно. Плюс кулдаун между повторами.
_PULL_LOCK = threading.Lock()
_PULL_LAST: dict = {}                       # ключ действия → monotonic время последнего запуска
_PULL_OWNER: dict = {"key": None, "since": 0.0}
_COOLDOWN = {"balance": 60.0, "assets": 20.0}   # сек между повторами одного действия


def _pull_begin(key: str, cooldown: float) -> tuple[bool, str, int]:
    """Захватить право на выгрузку. (ok, reason, wait_sec).
    reason: '' | 'cooldown' (рано повторять) | 'busy' (идёт другая выгрузка)."""
    now = time.monotonic()
    wait = cooldown - (now - _PULL_LAST.get(key, 0.0))
    if wait > 0:
        return False, "cooldown", int(wait) + 1
    if not _PULL_LOCK.acquire(blocking=False):
        return False, "busy", int(now - _PULL_OWNER.get("since", now))
    _PULL_OWNER["key"] = key
    _PULL_OWNER["since"] = now
    return True, "", 0


def _pull_end(key: str) -> None:
    """Освободить лок и отметить время (вызывать ТОЛЬКО если _pull_begin вернул ok)."""
    _PULL_LAST[key] = time.monotonic()
    _PULL_OWNER["key"] = None
    try:
        _PULL_LOCK.release()
    except RuntimeError:
        pass


# ── Асинхронные джобы создания набора (create_set) — чтобы большой набор НЕ упирался в
# nginx proxy_read_timeout (504 HTML). Фронт стартует джобу и опрашивает прогресс. ──
# Пул создания: параллелим по разным агентствам, но на ОДНО агентство держим только 1
# активную create-джобу. Практически весь боевой путь использует UAC/Grid/куки хотя бы
# на части шагов, и 2 одновременных аккаунта одного агентства дают зависания/гонки сессии.
# R2-1 (2026-07-09): done>=total = цикл создания завершён, идёт ФИНАЛИЗАЦИЯ (promo/postprocess/
# build_response/DB-хвост). Раньше watchdog БЕЗУСЛОВНО щадил done>=total → зависшая финализация
# висела running ВЕЧНО (job e05fbc86e8ca, done=14/14, >33мин, CPU 0%). #17 таймбоксил ТОЛЬКО
# run_create_set_postprocess, а promo/build_response/DB-хвост/орфан-лок постпроцесса — нет. Отдельный
# КОНЕЧНЫЙ бюджет на фазу финализации (> postprocess-бюджета 600с) → воркер/джоба всегда к терминалу.
# ТРЕК A (2026-07-10): delayed content_repair (dcr) — это ОТЛОЖЕННАЯ фоновая добивка (run_at + свой
# демон-исполнитель `_delayed_repair_daemon_loop`), фаза «докрутка» в UI ВЫКЛючена (F async-finalize
# OFF). Раньше `_schedule_delayed_content_repair_after_done` вливал dcr как _active_children через
# `_parent_absorb_child_start` → флипал уже-`done` родителя обратно в `running`; watchdog ЛЕГИТИМНО
# щадит такую джобу (blueprint:868) пока ребёнок жив, а dcr, застрявший в `partial` (keywords_repair
# не может дозалить ключи → reschedule до кап), держал родителя `running` ~час (прогон 59581fdd9f9d:
# 14 РК за 41 мин, потом ~час висел running done=14/14 → interrupted). Детач: dcr НЕ держит родителя —
# родитель доходит до ТЕРМИНАЛА (`done`) после создания+аудита, dcr крутится демоном асинхронно.
# Реальные дочерние докрутки (recreate/UAC-replace/resume, child_jid=job_id) и finalize (`fin:`) —
# НЕ тронуты (их child_jid НЕ начинается с `dcr:`; ими рулят K1/F watchdog'и). Реверс: env=0.


# ── Роль процесса (Фаза 2: раздельные сервисы web/worker) ───────────────────────
# DIRECT_ROLE управляет тем, кто держит очередь создания РК:
#   'all'    (дефолт) — постановка in-memory + воркеры/демоны в одном процессе (как раньше,
#                        полная обратная совместимость: код можно задеплоить БЕЗ включения split);
#   'web'    — только Flask + постановка джоб в БД (status='queued', _web_posted=true).
#              Воркеры/демоны/поллер НЕ стартуют. Статус/отмена/resume/feed — через БД.
#   'worker' — worker_main.py: воркеры + все демоны + БД-поллер, забирающий web-posted джобы
#              из БД в свою in-memory очередь. Именно этот процесс исполняет создание РК.


# Drain: SIGTERM воркеру → перестать брать НОВЫЕ джобы, дать текущим доработать текущий item,
# затем выйти (running-остаток в БД → _jobs_db_recover при следующем старте пометит interrupted).










# Сериализует read-modify-write счётчиков job при C1-параллельных каналах создания
# (DIRECT_PARALLEL_CHANNELS): master/product-путь канала B бампает эти же функции напрямую (через
# _master_product_deps), а не через обёртки оркестратора. Uncontended в OFF/однопоточных потоках →
# поведение не меняется (только защита от lost-update при двух потоках).








# ── Серверная персистентность очереди (public.direct_automation_jobs на Victory) ──
# Цель: очередь живёт на СЕРВЕРЕ — видна с любого устройства, переживает рестарт сервиса
# (для просмотра). Все DB-операции best-effort: падение БД НЕ ломает создание кампаний.








# ── web-роль: общение с worker'ом через БД (без in-memory очереди) ──────────────






























# ── Авто-докрутка остатка набора после сброса баллов Директа (полночь МСК) ──────
# При error 152 (исчерпан суточный лимит баллов) остаток набора НЕ теряем: сохраняем в
# public.direct_deferred_creates и фоновый демон докручивает его, как только баллы восстановятся
# (сброс — полночь МСК = 21:00 UTC). Дедупа не нужно: остаток = пункты, которые ещё НЕ начинали.
                                                      # Семён 2026-07-07 — никаких ночных отложек по расписанию;
                                                      # запрос дешёвый LIMIT 5, нагрузку не меняет)
                                                      # (джоба умерла при рестарте) → вернуть в waiting+now()
# B2: явный бюджет времени на ОДНУ repair-джобу (< watchdog-таймаута 1800с). Исчерпан → корректно
# завершаем статусом partial БЕЗ reschedule (иначе на большом аккаунте цикл full-Grid-верификаций
# упирается в watchdog kill вместо чистого partial).
# B1: коды Grid-ошибок «поле недоступно/неизвестно для этой схемы/фида» — НЕфиксабельно in-place
# (мутация вернёт executed=0 навсегда). Такую проблему исключаем из inplace-остатка, чтобы цикл
# не перепланировался по кругу на нечинимом флаге до watchdog kill.
























# ── «Готовые логины» — реестр аккаунтов с загруженными кампаниями (вкладка UI) ──────
# Пополняется на done create-джобы (kind set/slepok, created>0); логин УХОДИТ из списка,
# когда наш сервис удалил черновики (kind delete_drafts done). Ручное удаление/очистка — API.





























































# ── Кросс-процессный per-agency гейт (create-worker ↔ copy-worker) ──────────────────
# _CREATE_ACTIVE_AGENCIES — in-memory В КАЖДОМ процессе, потому direct-worker (create) и
# direct-copy (copy) НЕ координируются → create+copy одного агентства жгут куки/API Яндекса
# параллельно (152, инвалидация кук). Слот агентства держим в БД (одна строка на кластер).
# FAIL-OPEN: ЛЮБОЙ сбой БД → ведём себя как раньше (не блокируем) — гейт не может сломать пайплайн.
















# ── worker-роль: БД-поллер (забирает web-posted джобы из БД в in-memory очередь) ──




















def _busy_response(reason: str, wait: int):
    if reason == "cooldown":
        msg = f"Подождите ещё ~{wait} c перед повторной выгрузкой (защита аккаунта от блокировки)."
    else:
        msg = "Сейчас уже идёт выгрузка (возможно, в другой вкладке). Дождитесь её завершения."
    return jsonify({"error": msg, "locked": True, "reason": reason, "wait": wait}), 429


# ── Pages ─────────────────────────────────────────────────────────────────────

_UI_STRUCTURE_CACHE: dict = {"signature": None, "data": None}
_UI_STRUCTURE_CACHE_LOCK = threading.Lock()


def _slepki_structure_for_ui_from_struct(struct: dict) -> dict:
    """Apply the UI-only filters/manifest expansion to an already selected structure slice."""
    out = copy.deepcopy(struct)
    for d in out.get("directologists", []):
        key = d.get("key") or ""
        source_manifest = d.get("source_manifest")
        source_campaigns_by_site: dict[str, list] = {}
        if source_manifest:
            try:
                manifest = _json(source_manifest)
                if manifest.get("slepok") == key:
                    source_campaigns_by_site[manifest.get("site_type") or ""] = copy.deepcopy(
                        manifest.get("campaigns") or [])
            except Exception:  # noqa: BLE001 — broken manifest is reported by preflight, UI still opens
                source_campaigns_by_site = {}
        for st in d.get("site_types", []):
            stype = st.get("name") or ""
            if st.get("tp"):
                st["tp"] = [t for t in st.get("tp", [])
                            if not _slepok_profile_excludes_tp(key, stype, t.get("code") or "")]
            if source_campaigns_by_site.get(stype):
                st["source_campaigns"] = source_campaigns_by_site[stype]
    return out


def _ct_segment_map_for_light_ui() -> dict:
    """Fast ct→segment map for the isolated slepki page; avoids Victory DB on refresh."""
    try:
        data = _json("ct_segments_cache.json")
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return _ct_segment_map()


def _donor_tp4_models_map_for_light_ui() -> dict:
    """Fast donor tp4 map for the isolated slepki page; avoids Victory DB on refresh."""
    try:
        data = _json("donor_tp4_models_cache.json")
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return _donor_tp4_models_map()


def _ui_structure_payload(*, selected_slepok: str = "", light: bool = False) -> dict:
    """Heavy structure data loaded only by UI panels that actually need it."""
    from . import slepki_store as _sstore  # noqa: PLC0415
    struct = (_sstore.assemble_light_for_selected(selected_slepok)
              if light else _json("slepki_structure.json"))
    # Структура слепков живёт в per-slepok файлах (direct/slepki/*.json) — монолита
    # slepki_structure.json на диске НЕТ. Брать его stat() нельзя: `if exists()` молча
    # выбрасывал структуру из сигнатуры → правка ЛЮБОГО слепка не меняла ETag → браузер
    # получал 304 и рисовал старое дерево (регрессия инцидента 2026-07-16). Сигнатуру частей
    # даёт сам store — тот же ключ, по которому он инвалидирует свой кэш assemble().
    names = ["targeting_profile.json", "tp67_real_keywords.json"]
    names.extend(
        d.get("source_manifest") for d in (struct.get("directologists") or [])
        if d.get("source_manifest")
    )
    signature = (("light", bool(light), selected_slepok or ""), ("slepki_parts", _sstore._signature()),) + tuple(
        (name, (_HERE / name).stat().st_mtime_ns, (_HERE / name).stat().st_size)
        for name in names if (_HERE / name).exists()
    ) + (("pack_files", _slepki_pack_signature(struct)),)
    with _UI_STRUCTURE_CACHE_LOCK:
        if _UI_STRUCTURE_CACHE.get("signature") == signature:
            return _UI_STRUCTURE_CACHE["data"]
    struct_ui = (_slepki_structure_for_ui_from_struct(struct) if light else _slepki_structure_for_ui())
    packs = _slepki_pack_facts(struct_ui)
    data = {
        "slepki_structure": struct_ui,
        # Пер-групповой факт ключей (real/auto) для бейджей таргетинга tp1/2/4/5/6/7, посчитанный
        # на СЕРВЕРЕ по тем же пер-групповым пакам (ct~gk), что раньше UI тянул лениво через
        # /direct/api/slepki/keywords. Клиент сидит этим _SL_PACK_CACHE ДО первого рендера →
        # бейдж сразу верный, без «прыжка» эвристики. Кэшируется той же signature, что и структура.
        "pack_facts": packs["facts"],
        # Счётчик «≈N ключевых слов» карточки обзора — тоже с сервера (тот же обход, 0 доп. чтений).
        # Раньше клиент ради него слал по запросу НА ГРУППУ (522 запроса / 21 МБ на открытие).
        "kw_totals": packs["kw_totals"],
        "model_cts": [] if light else _model_cts(),
        "ct_segments": _ct_segment_map_for_light_ui() if light else _ct_segment_map(),
        "non_auto_slepki": _non_auto_slepki(),
        "donor_tp4_models": _donor_tp4_models_map_for_light_ui() if light else _donor_tp4_models_map(),
        # Версия среза = та же signature (mtime+size источников). Уходит в ETag: браузер
        # ревалидирует и получает 304, пока структура не поменялась. Без этого вкладка,
        # открытая часами, рисует старое дерево из памяти (инцидент 2026-07-16: UI показывал
        # tp5 kuderko с Фидами через 8 часов после их удаления из структуры).
        "sig": hashlib.md5(repr(signature).encode()).hexdigest(),
    }
    with _UI_STRUCTURE_CACHE_LOCK:
        _UI_STRUCTURE_CACHE.update(signature=signature, data=data)
    return data


def _render_page():
    return render_template(
        "direct/index.html",
        active_section="work", active_page="direct_automation",
        audiences=_load_audiences(),
        feeds_catalog=_json("feeds_catalog.json"),
        default_name=cmc.DEFAULT_DISPLAY_NAME,
        is_admin=bool(session.get("is_admin")),  # редактор «Структуры слепков» — только админ
    )




_FEED_RULES_ENSURED = False                              # DDL/дефолты/бэкфилл роли — 1 раз на процесс


def _feed_rules_ensure(cur) -> None:
    global _FEED_RULES_ENSURED
    if _FEED_RULES_ENSURED:
        return                                           # #4: не гоняем DDL+дефолты+information_schema на каждый item
    cur.execute(
        "CREATE TABLE IF NOT EXISTS public.direct_global_feed_rules ("
        "feed_key text PRIMARY KEY, name text NOT NULL, url text NOT NULL, "
        "enabled boolean NOT NULL DEFAULT true, sort integer NOT NULL DEFAULT 0, "
        "updated_at timestamptz NOT NULL DEFAULT now())"
    )
    # role: каталог vs лендинг. Товарка tp1 множится ТОЛЬКО по catalog-фидам (модельные листинги
    # реальны); лендинг/оффер-фиды дают ПУСТОЙ model-ListingAd → tp1 удаляла всю кампанию. tp7
    # продолжает использовать ВСЕ enabled-фиды (не трогаем). Колонку добавляем идемпотентно; backfill
    # СУЩЕСТВУЮЩИХ строк гоним ОДИН раз при первом создании колонки, чтобы НЕ затирать ручные правки
    # роли из UI. Колонку гарантируем ДО вставки дефолтов, чтобы задать role прямо в INSERT.
    # МАТЧИНГ — ТОЛЬКО по ТОЧНОМУ feed_key (равенство, `= ANY(список)` / `in`); НИКАКИХ LIKE/ILIKE/подстрок.
    cur.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
        "AND table_name='direct_global_feed_rules' AND column_name='role'"
    )
    if cur.fetchone() is None:
        cur.execute(
            "ALTER TABLE public.direct_global_feed_rules "
            "ADD COLUMN role text NOT NULL DEFAULT 'landing'"
        )
        cur.execute(
            "UPDATE public.direct_global_feed_rules SET role='catalog' WHERE feed_key = ANY(%s)",
            (sorted(_CATALOG_FEED_KEYS),),
        )
    # Дефолт-фиды: role проставляем ПО ЧЛЕНСТВУ в _CATALOG_FEED_KEYS прямо на INSERT (колонка выше уже
    # гарантирована). ON CONFLICT DO NOTHING → существующие строки (в т.ч. ручные правки роли из UI) не
    # трогаем; НОВЫЙ catalog-дефолт, добавленный в след. релизе, получит role='catalog' даже на старой БД,
    # где одноразовый backfill выше уже не сработает — закрывает расхождение константа↔БД (#3 review).
    for row in _feed_rules_defaults():
        _fk = _feed_key(row["url"])
        _role = "catalog" if _fk in _CATALOG_FEED_KEYS else "landing"
        cur.execute(
            "INSERT INTO public.direct_global_feed_rules(feed_key, name, url, enabled, sort, role, updated_at) "
            "VALUES(%s, %s, %s, %s, %s, %s, now()) "
            "ON CONFLICT(feed_key) DO NOTHING",
            (_fk, row["name"], row["url"], bool(row["enabled"]), int(row["sort"]), _role),
        )
    _FEED_RULES_ENSURED = True


def _global_feed_rules() -> list[dict]:
    import psycopg2.extras
    conn = _victory_conn_rw()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _feed_rules_ensure(cur)
        conn.commit()
        cur.execute(
            "SELECT feed_key, name, url, enabled, sort, role FROM public.direct_global_feed_rules "
            "ORDER BY sort, name"
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _allowed_feed_keys() -> set[str]:
    try:
        rows = _global_feed_rules()
        return {_feed_key(r.get("url") or r.get("name") or r.get("feed_key") or "")
                for r in rows if r.get("enabled")}
    except Exception:  # noqa: BLE001
        # Если Victory временно недоступна, не валим создание: используем текущий дефолтный allow-list.
        return {_feed_key(f) for f in _GLOBAL_FEED_DEFAULTS}


def _catalog_feed_keys() -> set[str]:
    """feed_key ВСЕХ enabled-фидов Глобальных правил. Роль «каталог/лендинг» удалена (Семён 2026-07-06):
    логика бинарная — галочка включена = фид участвует полностью, в т.ч. в товарке tp1 (модельные
    листинги). Ответственность «не включать пустой оффер-лендинг-фид» на операторе. Технически равно
    _allowed_feed_keys(); оставлено отдельной функцией ради стабильного catalog_only-контракта call-site."""
    return _allowed_feed_keys()


def _feed_row_allowed(feed: dict, allowed: set[str] | None = None) -> bool:
    allowed = _allowed_feed_keys() if allowed is None else allowed
    allowed_keys = set()
    for k in (allowed or set()):
        kk = _feed_key(k)
        if not kk:
            continue
        allowed_keys.add(kk)
        if kk.endswith(".xml"):
            allowed_keys.add(kk[:-4])

    feed_keys = set()
    for key in ("feed_name", "feedKey", "feed_key", "name", "url", "href", "source", "sourceUrl", "SourceUrl", "Name"):
        raw = str(feed.get(key) or "").strip()
        if not raw:
            continue
        parts = re.split(r"[—–|]+", raw)
        for part in [raw] + parts:
            fk = _feed_key(part)
            if not fk:
                continue
            feed_keys.add(fk)
            if fk.endswith(".xml"):
                feed_keys.add(fk[:-4])
    return bool(feed_keys & allowed_keys)


def _filter_allowed_feed_rows(rows: list[dict]) -> list[dict]:
    allowed = _allowed_feed_keys()
    if not allowed:
        return []
    return [f for f in (rows or []) if _feed_row_allowed(f, allowed)]


# ── Минус-СЛОВА (минус-фразы): ЕДИНЫЙ глобальный источник минус-фраз кампаний/групп ────────────────
# Все минус-фразы, которые вешаются на кампании/группы при создании (tp1–tp7), берутся ТОЛЬКО отсюда
# (вкладка «Минус-слова» на /direct/automation → таблица public.direct_global_minus_words). Пак M3
# (_minus.txt/_minus_shared.txt) и хардкод ["отзывы"] как источники минус-ФРАЗ отключены. Минус-ПЛОЩАДКИ
# и минус-МАРКИ/МОДЕЛИ — отдельные сущности (свои таблицы/вкладки), сюда не относятся.
_MINUS_WORDS_ENSURED = False                             # DDL/сид гоняем 1 раз на процесс
_MINUS_WORDS_CACHE: dict[tuple, dict] = {}               # TTL-кэш: (geo, ct, campaign_level) → {ts, val}
_MINUS_WORDS_TTL = 30.0                                   # сек


def _minus_words_ensure(cur) -> None:
    """DDL-миграция таблицы минус-слов: добавляет измерения geo+ct, пересоздаёт PK.
    Идемпотентно: безопасно запускать на чистой БД, на старой (word PK) и на уже мигрированной."""
    global _MINUS_WORDS_ENSURED
    if _MINUS_WORDS_ENSURED:
        return
    # Фиксируем существование ДО CREATE TABLE (сид — только при первом создании):
    cur.execute("SELECT to_regclass('public.direct_global_minus_words')")
    _r = cur.fetchone()
    existed = bool((_r["to_regclass"] if isinstance(_r, dict) else _r[0]) if _r else None)
    # Чистая БД: создаём сразу с composite PK и колонками geo/ct:
    cur.execute(
        "CREATE TABLE IF NOT EXISTS public.direct_global_minus_words ("
        "word text NOT NULL, geo text NOT NULL DEFAULT '*', ct text NOT NULL DEFAULT '*', "
        "enabled boolean NOT NULL DEFAULT true, "
        "sort integer NOT NULL DEFAULT 0, updated_at timestamptz NOT NULL DEFAULT now(), "
        "PRIMARY KEY (word, geo, ct))"
    )
    # Существующая таблица без geo/ct: ADD COLUMN IF NOT EXISTS — no-op если уже есть:
    cur.execute(
        "ALTER TABLE public.direct_global_minus_words "
        "ADD COLUMN IF NOT EXISTS geo text NOT NULL DEFAULT '*'"
    )
    cur.execute(
        "ALTER TABLE public.direct_global_minus_words "
        "ADD COLUMN IF NOT EXISTS ct text NOT NULL DEFAULT '*'"
    )
    # Пересоздать PK на (word, geo, ct) если текущий — по одной колонке (старый word PK):
    cur.execute(
        "SELECT conname, array_length(conkey, 1) AS ncols FROM pg_constraint "
        "WHERE conrelid='public.direct_global_minus_words'::regclass AND contype='p'"
    )
    _pk = cur.fetchone()
    if _pk:
        _pk_name = _pk["conname"] if isinstance(_pk, dict) else _pk[0]
        _pk_ncols = (_pk["ncols"] if isinstance(_pk, dict) else _pk[1]) or 1
        if _pk_ncols < 3:
            cur.execute(
                f'ALTER TABLE public.direct_global_minus_words DROP CONSTRAINT "{_pk_name}"'
            )
            cur.execute(
                "ALTER TABLE public.direct_global_minus_words ADD PRIMARY KEY (word, geo, ct)"
            )
    # Сид «отзывы» только при первом создании — существующие строки через DEFAULT '*' уже корректны:
    if not existed:
        cur.execute(
            "INSERT INTO public.direct_global_minus_words(word, geo, ct, enabled, sort, updated_at) "
            "VALUES('отзывы', '*', '*', true, 1, now()) ON CONFLICT(word, geo, ct) DO NOTHING"
        )
    _MINUS_WORDS_ENSURED = True


def _global_minus_words() -> list[dict]:
    """Все сохранённые минус-слова: [{word, enabled, sort}] (по sort, word). Только СОХРАНЁННЫЕ строки."""
    import psycopg2.extras
    conn = _victory_conn_rw()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _minus_words_ensure(cur)
        conn.commit()
        cur.execute("SELECT word, enabled, sort FROM public.direct_global_minus_words ORDER BY sort, word")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _global_minus_words_slice(geo: str = "*", ct: str = "*") -> list[dict]:
    """Минус-слова конкретного среза (geo, ct): [{word, enabled, sort}] — для admin UI / GET-эндпоинта."""
    import psycopg2.extras
    conn = _victory_conn_rw()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _minus_words_ensure(cur)
        conn.commit()
        cur.execute(
            "SELECT word, enabled, sort FROM public.direct_global_minus_words "
            "WHERE geo=%s AND ct=%s ORDER BY sort, word",
            (geo, ct),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _minus_words_fetch(geo: str, ct: str, campaign_level: bool) -> list[str]:
    """Низкоуровневый TTL-кэшируемый читатель минус-слов с фильтром по (geo, ct, campaign_level).
    campaign_level=True → ct='*' AND geo IN ('*', geo) (campaign минус).
    campaign_level=False → ct=<ct> AND geo IN ('*', geo) (group-level delta; ct должен быть не '*')."""
    cache_key = (geo, ct, campaign_level)
    now = time.time()
    entry = _MINUS_WORDS_CACHE.get(cache_key)
    if entry and now - entry["ts"] < _MINUS_WORDS_TTL:
        return list(entry["val"])
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            _minus_words_ensure(cur)
            conn.commit()
            if campaign_level:
                cur.execute(
                    "SELECT word FROM public.direct_global_minus_words "
                    "WHERE enabled AND ct='*' AND (geo='*' OR geo=%s)",
                    (geo,),
                )
            else:
                cur.execute(
                    "SELECT word FROM public.direct_global_minus_words "
                    "WHERE enabled AND ct=%s AND (geo='*' OR geo=%s)",
                    (ct, geo),
                )
            out: list[str] = []
            seen: set[str] = set()
            for row in cur.fetchall():
                w = re.sub(r"\s+", " ", str(row[0] or "").strip())
                if not w or len(w.split()) > 7:
                    continue
                k = w.lower()
                if k not in seen:
                    seen.add(k)
                    out.append(w)
        finally:
            conn.close()
        _MINUS_WORDS_CACHE[cache_key] = {"ts": now, "val": list(out)}
        return out
    except Exception as _exc:  # noqa: BLE001 — недоступность БД не валит создание
        import logging as _log
        _log.warning("[minus_words] _minus_words_fetch(%s,%s,%s) failed: %s", geo, ct, campaign_level, _exc)
        entry = _MINUS_WORDS_CACHE.get(cache_key)
        return list(entry["val"]) if entry else []


def _minus_words_all(geo: str = "*") -> list[str]:
    """Минус-слова уровня кампании: enabled, ct='*', geo IN ('*', <geo>) — дедупликация. TTL-кэш 30с."""
    return _minus_words_fetch(geo, "*", campaign_level=True)


def _minus_words_ct(geo: str = "*", ct: str = "*") -> list[str]:
    """Минус-слова уровня группы (дельта): enabled, ct=<ct>, geo IN ('*', <geo>). При ct='*' → [].
    TTL-кэш 30с. Используется как дополнение к _minus_words_all для ct-специфичных фраз."""
    if ct == "*":
        return []
    return _minus_words_fetch(geo, ct, campaign_level=False)


def _enabled_minus_words() -> list[str]:
    """ЕДИНЫЙ источник минус-фраз кампаний/групп. Обратная совместимость → _minus_words_all('*').
    Все существующие вызовы (tp1-tp7, feed/text/builders) продолжают работать без изменений."""
    return _minus_words_all("*")


# ── Минус-площадки РСЯ (#21): общий список URL, добавляется в disabledPlaces tp1 ────────────────
_MINUS_PLACES_ENSURED = False                            # DDL глобальной таблицы — 1 раз на процесс


def _minus_places_ensure(cur) -> None:
    """DDL глобальной таблицы минус-площадок."""
    global _MINUS_PLACES_ENSURED
    if _MINUS_PLACES_ENSURED:
        return
    cur.execute(
        "CREATE TABLE IF NOT EXISTS public.direct_global_minus_places ("
        "url text PRIMARY KEY, enabled boolean NOT NULL DEFAULT true, "
        "sort integer NOT NULL DEFAULT 0, updated_at timestamptz NOT NULL DEFAULT now())"
    )
    _MINUS_PLACES_ENSURED = True


def _global_minus_places() -> list[dict]:
    """Read глобальной таблицы: UI показывает её целиком, движок берёт enabled-домены."""
    import psycopg2.extras
    conn = _victory_conn_rw()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _minus_places_ensure(cur)
        conn.commit()
        cur.execute("SELECT url, enabled, sort FROM public.direct_global_minus_places ORDER BY sort, url")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _place_host(u: str) -> str:
    """Голый хост площадки для disabledPlaces: Яндекс ждёт ДОМЕН, а не URL со схемой/путём.
    'https://gdz.ru/' → 'gdz.ru'; 'gdz.ru/x' → 'gdz.ru'; 'gdz.ru' → 'gdz.ru'. (#2 review — полный
    URL молча отбрасывался Яндексом → disabledPlaces приходил пустым)."""
    s = str(u or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", s)             # срезаем схему
    return s.split("/", 1)[0].strip()                        # срезаем путь/слэш → остаётся хост


def _enabled_minus_places(slepok: str = "") -> list[str]:
    """Хосты включённых минус-площадок для disabledPlaces tp1 — ОБЩИЙ список (единый для всех слепков).
    Читает direct_global_minus_places (домен, не URL; только enabled; дедуп). Параметр slepok
    сохранён для обратной совместимости 6 call-sites tp1, но ИГНОРИРУЕТСЯ. [] при сбое."""
    return _enabled_global_minus_places()


def _enabled_global_minus_places() -> list[str]:
    """Глобальный список минус-площадок: домен (не URL), только enabled, дедуп. [] при сбое."""
    try:
        out: list[str] = []
        seen: set[str] = set()
        for r in _global_minus_places():
            if not r.get("enabled"):
                continue
            h = _place_host(r.get("url"))
            if h and h not in seen:
                seen.add(h)
                out.append(h)
        return out
    except Exception:  # noqa: BLE001 — недоступность БД не валит копирование
        return []


_POST_MINUS_PLACES_ENSURED = False


def _post_minus_places_ensure(cur) -> None:
    """DDL минус-площадок для tp8/tp9/tp10 (Посевы): общий geo='*' + городские срезы."""
    global _POST_MINUS_PLACES_ENSURED
    if _POST_MINUS_PLACES_ENSURED:
        return
    cur.execute(
        "CREATE TABLE IF NOT EXISTS public.direct_post_minus_places ("
        "geo text NOT NULL DEFAULT '*', "
        "url text NOT NULL, "
        "enabled boolean NOT NULL DEFAULT true, "
        "sort integer NOT NULL DEFAULT 0, "
        "updated_at timestamptz NOT NULL DEFAULT now(), "
        "PRIMARY KEY(geo, url))"
    )
    _POST_MINUS_PLACES_ENSURED = True


def _post_minus_places_slice(geo: str = "*") -> list[dict]:
    """UI-read минус-площадок Посевов в пределах одного geo-среза."""
    import psycopg2.extras
    g = (geo or "*").strip() or "*"
    conn = _victory_conn_rw()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _post_minus_places_ensure(cur)
        conn.commit()
        cur.execute(
            "SELECT geo, url, enabled, sort FROM public.direct_post_minus_places "
            "WHERE geo=%s ORDER BY sort, url",
            (g,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _enabled_post_minus_places(geo: str = "*") -> list[str]:
    """Хосты disabledPlaces для Посевов: общий пак geo='*' + конкретное geo, с дедупом."""
    try:
        g = (geo or "*").strip() or "*"
        out: list[str] = []
        seen: set[str] = set()
        for source_geo in (["*"] if g == "*" else ["*", g]):
            for r in _post_minus_places_slice(source_geo):
                if not r.get("enabled"):
                    continue
                h = _place_host(r.get("url"))
                if h and h not in seen:
                    seen.add(h)
                    out.append(h)
        return out
    except Exception:  # noqa: BLE001 — недоступность БД не валит создание посевов
        return []


_BASELINE_MINUS_PLACES_ENSURED = False                   # DDL baseline-таблицы — 1 раз на процесс


def _baseline_minus_places_ensure(cur) -> None:
    """DDL baseline-таблицы стандартных анти-фрод минус-площадок (источник для copy: клон 1:1 без слепка)."""
    global _BASELINE_MINUS_PLACES_ENSURED
    if _BASELINE_MINUS_PLACES_ENSURED:
        return
    cur.execute(
        "CREATE TABLE IF NOT EXISTS public.direct_baseline_minus_places ("
        "url text PRIMARY KEY, enabled boolean NOT NULL DEFAULT true, "
        "sort integer NOT NULL DEFAULT 0, updated_at timestamptz NOT NULL DEFAULT now())"
    )
    _BASELINE_MINUS_PLACES_ENSURED = True


def _baseline_minus_places() -> list[dict]:
    """Baseline список стандартных минус-площадок → [{url, enabled, sort}]. [] при сбое."""
    import psycopg2.extras
    conn = _victory_conn_rw()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _baseline_minus_places_ensure(cur)
        conn.commit()
        cur.execute("SELECT url, enabled, sort FROM public.direct_baseline_minus_places ORDER BY sort, url")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _enabled_baseline_minus_places() -> list[str]:
    """Хосты включённых минус-площадок из BASELINE-таблицы (стандартный анти-фрод список для copy).
    Тело как _enabled_global_minus_places: домен (не URL), только enabled, дедуп. [] при сбое."""
    try:
        out: list[str] = []
        seen: set[str] = set()
        for r in _baseline_minus_places():
            if not r.get("enabled"):
                continue
            h = _place_host(r.get("url"))
            if h and h not in seen:
                seen.add(h)
                out.append(h)
        return out
    except Exception:  # noqa: BLE001 — недоступность БД не валит копирование
        return []


_MINUS_MARKS_ENSURED = False                             # DDL гоняем 1 раз на процесс
_MINUS_MARKS_CACHE: dict = {"ts": 0.0, "val": []}        # TTL-кэш для create-loop'ов (много вызовов подряд)
_MINUS_MARKS_TTL = 30.0                                   # сек


def _minus_marks_ensure(cur) -> None:
    global _MINUS_MARKS_ENSURED
    if _MINUS_MARKS_ENSURED:
        return
    cur.execute(
        "CREATE TABLE IF NOT EXISTS public.direct_global_minus_marks ("
        "mark text PRIMARY KEY, enabled boolean NOT NULL DEFAULT false, "
        "updated_at timestamptz NOT NULL DEFAULT now())"
    )
    _MINUS_MARKS_ENSURED = True


def _global_minus_marks() -> list[dict]:
    """Все сохранённые минус-марки фида: [{mark, enabled}] (по алфавиту). Только СОХРАНЁННЫЕ строки."""
    import psycopg2.extras
    conn = _victory_conn_rw()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _minus_marks_ensure(cur)
        conn.commit()
        cur.execute("SELECT mark, enabled FROM public.direct_global_minus_marks ORDER BY mark")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _enabled_minus_marks() -> list[str]:
    """Включённые минус-марки (значение как сохранено, дедуп). [] при сбое/пустом. TTL-кэш 30с —
    вызывается в циклах по товарным группам при создании (иначе N обращений к БД на набор)."""
    now = time.time()
    if now - _MINUS_MARKS_CACHE["ts"] < _MINUS_MARKS_TTL:
        return list(_MINUS_MARKS_CACHE["val"])
    try:
        out: list[str] = []
        seen: set[str] = set()
        for r in _global_minus_marks():
            if not r.get("enabled"):
                continue
            m = str(r.get("mark") or "").strip()
            if m and m.lower() not in seen:
                seen.add(m.lower())
                out.append(m)
        _MINUS_MARKS_CACHE.update({"ts": now, "val": list(out)})
        return out
    except Exception:  # noqa: BLE001 — недоступность БД не валит создание
        return list(_MINUS_MARKS_CACHE["val"])


# ── Минус-МОДЕЛИ (фид): второй уровень «Минус марки/модели» — исключение отдельной модели ──────────
# Справочник «марка → модели» — статический JSON brand_models_catalog.json, конечный список,
# правится вручную в коде (НЕ парсится из фидов в рантайме); выбор моделей (что минусовать) — в БД
# direct_global_minus_models.
_MODELS_CATALOG_PATH = _HERE / "brand_models_catalog.json"
_MINUS_MODELS_ENSURED = False
_MINUS_MODELS_CACHE: dict = {"ts": 0.0, "val": []}
_MINUS_MODEL_PAIRS_CACHE: dict = {"ts": 0.0, "val": []}  # companion для групп (с привязкой бренд→модель)
_MINUS_MODELS_TTL = 30.0


def _load_brand_models_catalog() -> dict:
    """Постоянный справочник {mark_canon: {label, models[]}} из brand_models_catalog.json.
    → {"updated_at", "sources", "brands"}. Файла нет / битый → пустой каркас (UI покажет только марки)."""
    try:
        data = json.loads(_MODELS_CATALOG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("brands"), dict):
            return data
    except Exception:  # noqa: BLE001 — нет файла/битый JSON → пустой справочник, не валим страницу
        pass
    return {"updated_at": None, "sources": [], "brands": {}}


def _minus_models_ensure(cur) -> None:
    global _MINUS_MODELS_ENSURED
    if _MINUS_MODELS_ENSURED:
        return
    cur.execute(
        "CREATE TABLE IF NOT EXISTS public.direct_global_minus_models ("
        "mark text NOT NULL, model text NOT NULL, enabled boolean NOT NULL DEFAULT true, "
        "updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(mark, model))"
    )
    _MINUS_MODELS_ENSURED = True


def _global_minus_models() -> list[dict]:
    """Сохранённые минус-модели: [{mark, model, enabled}] (только СОХРАНЁННЫЕ строки)."""
    import psycopg2.extras
    conn = _victory_conn_rw()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _minus_models_ensure(cur)
        conn.commit()
        cur.execute("SELECT mark, model, enabled FROM public.direct_global_minus_models ORDER BY mark, model")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _enabled_minus_models() -> list[str]:
    """Имена включённых минус-моделей (чистые, как в справочнике: «Tiggo 7 Pro») для feedFilter по полю
    model (NOT_CONTAINS[_ALL] — модель это подстрока фидового <model> «Tiggo 7 Pro от … Звоните»).
    TTL-кэш 30с — зовётся в циклах товарных групп. [] при сбое/пустом."""
    now = time.time()
    if now - _MINUS_MODELS_CACHE["ts"] < _MINUS_MODELS_TTL:
        return list(_MINUS_MODELS_CACHE["val"])
    try:
        out: list[str] = []
        seen: set[str] = set()
        for r in _global_minus_models():
            if not r.get("enabled"):
                continue
            m = str(r.get("model") or "").strip()
            if m and m.lower() not in seen:
                seen.add(m.lower())
                out.append(m)
        _MINUS_MODELS_CACHE.update({"ts": now, "val": list(out)})
        return out
    except Exception:  # noqa: BLE001 — недоступность БД не валит создание
        return list(_MINUS_MODELS_CACHE["val"])


def _enabled_minus_model_pairs() -> list[tuple[str, str]]:
    """Пары (mark_canon, model_lower_collapsed) включённых минус-моделей для группового
    минус-фильтра в create_set_tp1_builders (матч с привязкой к бренду, точное равенство).
    Сигнатуру _enabled_minus_models() НЕ меняет — та остаётся для feedFilter (create_set_feeds).
    Допущение: mark в БД уже в латинской канонике (baic/chery/mg/…) — _brand_canon идемпотентен.
    TTL-кэш 30с. [] при сбое/пустом."""
    now = time.time()
    if now - _MINUS_MODEL_PAIRS_CACHE["ts"] < _MINUS_MODELS_TTL:
        return list(_MINUS_MODEL_PAIRS_CACHE["val"])
    try:
        out: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for r in _global_minus_models():
            if not r.get("enabled"):
                continue
            mark = str(r.get("mark") or "").strip()
            model = str(r.get("model") or "").strip()
            if not mark or not model:
                continue
            mark_c = _brand_canon(mark.lower())          # латиница → идемпотентно
            model_l = " ".join(model.lower().split())    # схлопнуть повторные пробелы
            key = (mark_c, model_l)
            if key not in seen:
                seen.add(key)
                out.append(key)
        _MINUS_MODEL_PAIRS_CACHE.update({"ts": now, "val": list(out)})
        return out
    except Exception:  # noqa: BLE001 — недоступность БД не валит создание
        return list(_MINUS_MODEL_PAIRS_CACHE["val"])


# Правила вкладки «Контент» + фильтрация ассетов вынесены в blueprint_content_rules
# (ре-экспорт выше); _CONTENT_RULES_CACHE/_content_rule_key/_filter_content_assets и др.


# ── Аккаунты (Victory DB local_gsheet_sites, direction='Авто') ─────────────────

# _victory_conn определён — инъектим в promo_gen (_promo_ctx читает local_gsheet_sites).
_pg.configure({"_victory_conn": _victory_conn})








# _parse_counter_ids/_metrika_goals_for/_counter_foreign_owner вынесены в blueprint_metrika.


# DI для blueprint_metrika: _victory_conn (выше) + _direct_tokens (последняя зависимость).
# _V5 — модульная константа внутри blueprint_metrika (совпадает с _V5 ниже по файлу).
_bmt.configure({
    "_victory_conn": _victory_conn,
    "_direct_tokens": _direct_tokens,
})




# OAuth/v5/v501/Grid transports and agency resolution live in yandex_gateway.py.
_STATE_ORDER = {"ON": 0, "SUSPENDED": 1, "OFF": 2, "ENDED": 3, "CONVERTED": 4, "ARCHIVED": 5}




# Типы кампаний, которым НЕ нужна агентская кука (только OAuth-токен v5/v501):
# tp1 РСЯ, tp3 товарная галерея РСЯ, tp2/tp4 текстовые. Всё остальное (tp5 grid-докрутка,
# tp6 МК / tp7 товарка через UAC) ходит на куке агентства → её тоже надо проверить ДО создания.








# Slepok keys, M3 health and provider gate live in pack_resolver.py.


# ── Резолвер контента группы из пака M3 (по нашему ct) ──────────────────────────
# _gc_ct и классификатор ct→сегмент вынесены в blueprint_targeting (ре-экспорт выше).







# ── Автоподстановка значений из БД (тип сайта/город/счётчик/цель/тексты) ────────

# Гео-справочник (_geo_load/_geo_id) + Метрика-токен/цель (_metrika_token/_goal_vse_formy)
# вынесены в blueprint_metrika (ре-экспорт выше).










# Имя кампании, созданной ЭТИМ модулем, всегда начинается с кодера tpN_{cpc|cpa}_{site|kviz}_…
# (ЕПК tp1–tp5 и UAC tp6/tp7 — см. _uac_campaign_name / _tp1_group_name). Ручные/чужие кампании
# этому шаблону НЕ соответствуют → удаление черновиков НЕ должно их трогать (защита от сноса чужого).
















# Grid transport helpers live in yandex_gateway.py.


# Все агентские куки — для перебора, если agency_account в таблице неверный/без прав.




# ── Генератор имени кампании + планировщик набора ──────────────────────────────
# Тип сайта → код для середины имени (остальные типы добавим позже).


def _create_set_plan_deps() -> dict:
    return {
        "_SLEPOK_KEY": _SLEPOK_KEY,
        "_CATALOG_FEED_KEYS": _CATALOG_FEED_KEYS,   # роль фида (catalog/landing) для явного feed_role tp7
        "_ag_part1_map": _ag_part1_map,
        "_ct_for_name": _ct_for_name,
        "_ct_segment": _ct_segment,
        "_direct_tokens": _direct_tokens,
        "_feed_key": _feed_key,   # нормализованный ключ фида для явного feed_role/feed_key tp7
        "_filter_allowed_feed_rows": _filter_allowed_feed_rows,
        "_first_url_feed": _first_url_feed,   # обёртка с configure() — НЕ импортировать из csf напрямую
        "_gc_ct": _gc_ct,
        "_grid_feeds": _grid_feeds,
        "_grid_list_campaigns": _grid_list_campaigns,
        "_is_site_domain_name": _is_site_domain_name,
        "_json": _json,
        # Метрика на шаге плана (_metrika_alert_for → prepare_metrika): те же три коллбэка,
        # что получает оркестратор на шаге создания — проверка одна и та же, не вторая копия.
        "_metrika_goals_for": _metrika_goals_for,
        "_goal_vse_formy": _goal_vse_formy,
        "_counter_foreign_owner": _counter_foreign_owner,
        "_segment_donor": _segment_donor,
        "_slepok_interest_for_struct": _slepok_interest_for_struct,
        "_slepok_profile_excludes_tp": _slepok_profile_excludes_tp,
        "_slepok_struct_groups": _slepok_struct_groups,
        "_slepok_tp_modes": _slepok_tp_modes,
        "_token_for_login": _token_for_login,
        "_tp67_kw_position_key": _tp67_kw_position_key,
        "_tp67_targeting_mode": _tp67_targeting_mode,
        "_v5_get": _v5_get,
        "_victory_conn": _victory_conn,
    }


def _create_set_plan_module():
    from . import create_set_plan as csp
    csp.configure(_create_set_plan_deps())
    return csp


def _resolve_region(city: str | None):
    return _create_set_plan_module()._resolve_region(city)



# Редактор контента может работать отдельным процессом direct-content.service.
# В direct.service его выключаем через DIRECT_REGISTER_CONTENT_EDITOR=0, чтобы
# /direct/automation и /direct/automation/content перезапускались независимо.


# Бренд-нейтральные заголовки-филлеры (≤56 симв.) — добор до 5 слотов Мастера, когда у слепка
# заголовков меньше. Подходят к любой картинке салона (не привязаны к модели).
# БАГ 9: кредитные УТП в приоритете (первый взнос, ставка, платеж, господдержка).
# БАГ 4→исправлен: дефис-разделитель заменён на точку (правило Кудерко); БАГ 7: «0%» убрано из кредитных заголовков.
_GENERIC_TITLE_FILLERS = [
    "Кредит на новый авто. Первый взнос 0 ₽. Ключи за 1 день",  # [55]
    "Авто в кредит от 9 000 ₽/мес. КАСКО на 1 год бесплатно",   # [54]
    "Кредит на авто. КАСКО на 1 год. Подбор условий",            # [51]
    "Оценим авто в трейд-ин. Платеж от 9 000 ₽/мес онлайн",     # [52]
    "Первый взнос 0 ₽. Подбор кредита онлайн за 1 день",         # [52]
    "Новые авто в наличии. Кредитное решение за 1 день",         # [51]
    "Автокредит по заявке. Решение за 30 минут онлайн",          # [51]
]
# Заголовки под АВТОТАРГЕТ общих запросов (tp7 Товарка ct0000): ключевая фраза запроса СТОИТ ПЕРВОЙ -
# до точки/запятой (купить/новый/авто/цена/кредит), движок автотаргета цепляет её как ключ. БЕЗ марок/моделей
# (общая кампания). Правило пользователя: для «Общих запросов» - заголовки под общий запрос, не под бренд.
# БАГ 9: кредитные УТП приоритетом (2-3 из 5); БАГ 4→исправлен: разделитель — точка, не дефис.
_GENERIC_AT_TITLES = [
    # 8 строк: все с цифрой, разные первые слова, разные УТП-бакеты
    # (платёж / взнос / КАСКО / решение+срок / скидка / трейд-ин / наличие / одобрение)
    "Авто в кредит от 9 000 ₽/мес. Одобрение за 30 минут",     # [51] платёж
    "Кредит на авто. Первый взнос 0 ₽. Ключи за 1 день",       # [50] взнос
    "Кредит на новое авто. КАСКО на 1 год бесплатно",           # [52] КАСКО
    "Автокредит по заявке. Решение за 30 минут онлайн",         # [54] решение+срок
    "Выгода до 45% на новые авто. Кредитное решение",           # [48] скидка%
    "Трейд-ин выше рынка. Платеж от 9 000 ₽/мес в кредит",      # [51] трейд-ин
    "Новые авто в наличии. Первый взнос 0 ₽. Ключи за 1 день",  # [55] наличие+взнос
    "Одобрение за 30 минут. Кредитное решение онлайн",          # [50] одобрение
]
# Брендонейтральные фоллбэки текстов/ссылок - ГАРАНТ полноты tp6/tp7 (5 заголовков / 3 текста / 8 ссылок),
# когда контента слепка/шаблонов не хватило. Без марок - годятся для любой общей (ct0000) кампании.
# БАГ 9: кредитные УТП в первых 2 текстах (первый взнос, платеж, ставка, господдержка).
# БАГ 4→исправлен: разделитель — точка, не дефис.
_GENERIC_TEXT_FILLERS = [
    # 4 строки: все с цифрой, без «автокредит» (блокируется _bad_ad_text)
    # УТП-бакеты: платёж+банки / взнос+КАСКО / трейд-ин+срок / наличие+срок
    "Кредит на авто от 9 000 ₽/мес. Подберем условия. Одобрение за 1 час.",               # [81] платёж
    "Кредит без первого взноса на новое авто. Одобрение за 1 день онлайн.",               # [79] взнос
    "КАСКО на 1 год бесплатно при покупке в кредит. Ключи в день покупки. Одобрение.",    # [79] КАСКО
    "Трейд-ин выше рынка. Оценим авто за 30 минут и зачтём в счёт нового кредита.",       # [76] трейд-ин
]
_TP67_MIN_TEXT_LEN = 70
_GENERIC_SITELINK_FILLERS = [  # все заголовки 22–30 симв (fix 1c, 2026-07-02); тема-дедуп: кредит-слотов ровно 2
    # D1 (2026-07-09): продающие короткие офферы вместо слабых филлеров. Убраны висячий год
    # («на авто 2025») и размытые канцеляризмы («проверим доступные программы», «зафиксируем
    # персональные условия»). Каждая ссылка — конкретный оффер с цифрой/фактом (₽, %, «0 ₽», «за 1 день»).
    # Темы (topic-дедуп _sitelink_utp_bucket, credit ≤2 — SITELINK_CREDIT_DUPLICATE):
    # взнос(credit) · трейд-ин · КАСКО(gift) · одобрение(credit) · выгода(discount) · господдержка(support) ·
    # тест-драйв(testdrive) · наличие(availability). Порядок бакетов сохранён 1:1 с прежним набором.
    {"title": "Первый взнос 0 ₽ онлайн", "description": "Оформим автокредит без первого взноса за 30 минут"},
    {"title": "Оценка авто в трейд-ин", "description": "Оценим ваш автомобиль выше рынка и зачтём в покупку"},
    {"title": "КАСКО на 1 год в подарок", "description": "Дарим КАСКО на 1 год при покупке автомобиля"},
    {"title": "Одобрение банка за 30 минут", "description": "Отправьте заявку онлайн и узнайте решение сегодня"},
    {"title": "Выгода до 30% при покупке", "description": "Персональная скидка до 30% при покупке автомобиля"},
    {"title": "Купить по госпрограмме", "description": "Оформим господдержку до 20% при покупке авто"},
    {"title": "Тест-драйв без предоплаты", "description": "Выберите удобное время для тест-драйва онлайн"},
    {"title": "Авто в наличии сегодня", "description": "Подберём автомобиль под ваш бюджет и выдадим за 1 день"},
    # Backup-филлеры (позиции 9–10): используются когда _title_has_pct=True фильтрует позиции 5–6
    # (с «до 30%»/«до 20%»), иначе UAC tp7 получает 6/8 сайтлинков (UAC_SITELINKS_MISSING, psm5h7q6).
    # Без % → не фильтруются при _title_has_pct=True. Темы: rассрочка + гарантия (новые бакеты).
    {"title": "Кредитное решение онлайн", "description": "Оформим заявку без скрытых комиссий"},
    {"title": "Гарантия на автомобиль", "description": "Расширенная гарантия при покупке нового автомобиля"},
]


def _build_name(is_master: bool, is_auto: bool, pay: str, r_code: str, oblast: str,
                sq: str = "site", cat: str | None = None, ct: str = "ct0000",
                targeting_label: str | None = None, struct_name: str | None = None) -> str:
    return _create_set_plan_module()._build_name(
        is_master, is_auto, pay, r_code, oblast, sq, cat, ct, targeting_label, struct_name
    )


def _rule_sets(site_type: str, city: str) -> dict:
    return _create_set_plan_module()._rule_sets(site_type, city)


def _tp_plan_names(slepok: str, site_type: str, tp_code: str) -> list[dict]:
    return _create_set_plan_module()._tp_plan_names(slepok, site_type, tp_code)


def _tp1_plan_names(slepok: str, site_type: str, r_code: str) -> list[dict]:
    return _create_set_plan_module()._tp1_plan_names(slepok, site_type, r_code)


def _set_plan_response():
    return _create_set_plan_module()._set_plan_response()




def _num(val, default):
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default




# Копирование кампаний может работать отдельным процессом direct-copy.service со своей
# in-memory очередью. В direct.service его выключаем через DIRECT_REGISTER_COPY=0, чтобы
# /direct/automation и /direct/automation/copy перезапускались независимо (рестарт одного
# не роняет очередь другого). Дефолт (флаг не задан) = '1' — копирование в основном
# процессе (обратная совместимость single-process-режима).


def _prefetch_start(login, body, *, is_cancelled=lambda: False):
    """Прогрев queued-джобы в фоне (Фаза 1). Конфигурируем модуль лениво (все
    инъектируемые хелперы — _account_offer_prices и т.п. — определены ниже по
    файлу, резолвятся в момент вызова, а не определения)."""
    if os.getenv("DIRECT_CREATE_QUEUE_PREFETCH", "0").strip().lower() not in ("1", "true", "yes", "on"):
        return
    try:
        from . import ai_agents as _A
        from . import create_set_prefetch as _pf
        from . import campaign as _cmc
        _pf.configure({
            "account_ctx": _account_ctx,
            "cached_campaign_content": _cached_campaign_content,
            "content_cache_key": _content_cache_key,
            "content_cache": _CONTENT_CACHE,
            "content_cache_lock": _CONTENT_CACHE_LOCK,
            "brand_ct_from_coder": _brand_ct_from_coder,
            "account_offer_prices": _account_offer_prices,
            "account_content_put": _account_content_put,
            "preupload_tp1_images": _preupload_tp1_images,
            "creative_images_for_ct": _creative_images_for_ct,
            "image_ct_for_content": _image_ct_for_content,
            "get_agent": _A.get_agent,
            "pick_working_cookie": _cmc.pick_working_cookie,
            "videos_pool_for_ct": kp.videos_pool_for_ct,
        })
        _pf.start_prefetch(login, body, is_cancelled=is_cancelled)
    except Exception:  # noqa: BLE001 — прогрев не смеет ломать постановку джобы
        pass




def _create_set_context_deps() -> dict:
    return {
        "_SLEPOK_KEY": _SLEPOK_KEY,
        "_drop_foreign_city_keywords": _drop_foreign_city_keywords,
        "_drop_used_car": _drop_used_car,
        "_geo_load": _geo_load,
        "_json": _json,
        "_kw_clean": _kw_clean,
        "_victory_conn": _victory_conn,
        "kp": kp,
    }


def _create_set_context_module():
    from . import create_set_context as cctx
    cctx.configure(_create_set_context_deps())
    return cctx


def _account_ctx(login: str):
    return _create_set_context_module()._account_ctx(login)


def _templates_for(site_type: str):
    return _create_set_context_module()._templates_for(site_type)


def _slepok_audiences_for(slepok: str, site_type: str, tp: str) -> list[str]:
    return _create_set_context_module()._slepok_audiences_for(slepok, site_type, tp)


def _norm_slepok_audience_category(x: str | None) -> str:
    return _create_set_context_module()._norm_slepok_audience_category(x)


def _tp67_targeting_mode(g: dict) -> str:
    return _create_set_context_module()._tp67_targeting_mode(g)


def _tp67_audience_category_candidates(g: dict) -> list[str]:
    return _create_set_context_module()._tp67_audience_category_candidates(g)


def _slepok_audience_cats(slepok: str, site_type: str, tp: str) -> list[dict]:
    return _create_set_context_module()._slepok_audience_cats(slepok, site_type, tp)


def _slepok_struct_groups(slepok: str, site_type: str, tp: str) -> list[dict]:
    return _create_set_context_module()._slepok_struct_groups(slepok, site_type, tp)


def _slepok_interest_for_cat(slepok: str, site_type: str, tp: str, cat: str | None) -> list:
    return _create_set_context_module()._slepok_interest_for_cat(slepok, site_type, tp, cat)


def _slepok_interest_for_struct(slepok: str, site_type: str, tp: str, g: dict) -> tuple[list[str], str]:
    return _create_set_context_module()._slepok_interest_for_struct(slepok, site_type, tp, g)


def _tp67_kw_position_key(text: str | None) -> str:
    return _create_set_context_module()._tp67_kw_position_key(text)


def _tp67_real_keyword_items() -> list[dict]:
    return _create_set_context_module()._tp67_real_keyword_items()


def _tp67_keywords_from_real_library(slepok: str, site_type: str, tp: str, ct: str,
                                     city: str, position_name: str | None,
                                     sq: str | None = None) -> tuple[list[str], list[str]]:
    return _create_set_context_module()._tp67_keywords_from_real_library(
        slepok, site_type, tp, ct, city, position_name, sq
    )


def _tp67_keywords_for(slepok: str, site_type: str, tp: str, ct: str, city: str,
                       position_name: str | None = None, sq: str | None = None,
                       group: str = "") -> tuple[list[str], list[str]]:
    return _create_set_context_module()._tp67_keywords_for(
        slepok, site_type, tp, ct, city, position_name, sq, group=group
    )

def _tp67_keywords_for_groups(slepok: str, site_type: str, tp: str, ct: str, city: str,
                              position_name: str | None = None, sq: str | None = None,
                              groups=None) -> tuple[list[str], list[str]]:
    return _create_set_context_module()._tp67_keywords_for_groups(
        slepok, site_type, tp, ct, city, position_name, sq, groups=groups
    )


def _slepok_uses_shopping(slepok: str, tp: str) -> bool:
    return _create_set_context_module()._slepok_uses_shopping(slepok, tp)


def _create_set_feeds_deps() -> dict:
    return {
        "_GRID_URL": _GRID_URL,
        "_ag_part1_map": _ag_part1_map,
        "_allowed_feed_keys": _allowed_feed_keys,
        "_block_bootstrap": _block_bootstrap,
        "_catalog_feed_keys": _catalog_feed_keys,
        "_coder_name_real_brand": _coder_name_real_brand,
        "_ct_segment_map": _ct_segment_map,
        "_enabled_minus_marks": _enabled_minus_marks,
        "_enabled_minus_models": _enabled_minus_models,
        "_feed_row_allowed": _feed_row_allowed,
        "_filter_allowed_feed_rows": _filter_allowed_feed_rows,
        "_gc_ct": _gc_ct,
        "_grid_csrf": _grid_csrf,
        "_v5_get": _v5_get,
        "cmc": cmc,
        "gc": gc,
        "gf": gf,
    }


def _create_set_feeds_module():
    from . import create_set_feeds as csf
    csf.configure(_create_set_feeds_deps())
    return csf

def _first_url_feed(*args, **kwargs):
    return _create_set_feeds_module()._first_url_feed(*args, **kwargs)

def _catalog_feed(*args, **kwargs):
    return _create_set_feeds_module()._catalog_feed(*args, **kwargs)

def _grid_feeds(*args, **kwargs):
    return _create_set_feeds_module()._grid_feeds(*args, **kwargs)

def _account_model_feeds(*args, **kwargs):
    return _create_set_feeds_module()._account_model_feeds(*args, **kwargs)

def _offer_price_keys(*args, **kwargs):
    return _create_set_feeds_module()._offer_price_keys(*args, **kwargs)

def _merge_price(*args, **kwargs):
    return _create_set_feeds_module()._merge_price(*args, **kwargs)

def _grid_feed_offer_prices(*args, **kwargs):
    return _create_set_feeds_module()._grid_feed_offer_prices(*args, **kwargs)

def _grid_feed_offer_urls(*args, **kwargs):
    return _create_set_feeds_module()._grid_feed_offer_urls(*args, **kwargs)

def _feed_url_for_model(*args, **kwargs):
    return _create_set_feeds_module()._feed_url_for_model(*args, **kwargs)

def _ad_price_for_brand(*args, **kwargs):
    return _create_set_feeds_module()._ad_price_for_brand(*args, **kwargs)

def _min_offer_price(*args, **kwargs):
    return _create_set_feeds_module()._min_offer_price(*args, **kwargs)

def _group_ad_price(*args, **kwargs):
    return _create_set_feeds_module()._group_ad_price(*args, **kwargs)

def _safe_old_price(*args, **kwargs):
    return _create_set_feeds_module()._safe_old_price(*args, **kwargs)

def _grid_ad_price_payload(*args, **kwargs):
    return _create_set_feeds_module()._grid_ad_price_payload(*args, **kwargs)

def _cached_upload_image(*args, **kwargs):
    _touch_running_jobs_heartbeat()   # аплоад картинки = прогресс (сотни на кампанию — анти-watchdog)
    return _create_set_feeds_module()._cached_upload_image(*args, **kwargs)

def _parallel_upload_images(*args, **kwargs):
    return _create_set_feeds_module()._parallel_upload_images(*args, **kwargs)

def _homepage_url(*args, **kwargs):
    return _create_set_feeds_module()._homepage_url(*args, **kwargs)

def _combo_button(*args, **kwargs):
    return _create_set_feeds_module()._combo_button(*args, **kwargs)

def _grid_set_ad_prices(*args, **kwargs):
    return _create_set_feeds_module()._grid_set_ad_prices(*args, **kwargs)

def _grid_update_adaptive_ads(*args, **kwargs):
    return _create_set_feeds_module()._grid_update_adaptive_ads(*args, **kwargs)

def _apply_combo_button(*args, **kwargs):
    return _create_set_feeds_module()._apply_combo_button(*args, **kwargs)

def _grid_price_feed(*args, **kwargs):
    return _create_set_feeds_module()._grid_price_feed(*args, **kwargs)

def _price_feeds_for(*args, **kwargs):
    return _create_set_feeds_module()._price_feeds_for(*args, **kwargs)

def _account_offer_prices(*args, **kwargs):
    return _create_set_feeds_module()._account_offer_prices(*args, **kwargs)

def _account_offer_urls(*args, **kwargs):
    return _create_set_feeds_module()._account_offer_urls(*args, **kwargs)

def _match_collection(*args, **kwargs):
    return _create_set_feeds_module()._match_collection(*args, **kwargs)

def _brand_collection_ids(*args, **kwargs):
    return _create_set_feeds_module()._brand_collection_ids(*args, **kwargs)

def _feed_collections(*args, **kwargs):
    return _create_set_feeds_module()._feed_collections(*args, **kwargs)

def _brand_canon(*args, **kwargs):
    return _create_set_feeds_module()._brand_canon(*args, **kwargs)

def _brand_in_name(*args, **kwargs):
    return _create_set_feeds_module()._brand_in_name(*args, **kwargs)

def _known_brand_canons(*args, **kwargs):
    return _create_set_feeds_module()._known_brand_canons(*args, **kwargs)

def _is_brand_canon(*args, **kwargs):
    return _create_set_feeds_module()._is_brand_canon(*args, **kwargs)


# 4 DI campaign_naming определены (_victory_conn/_ct_segment/_brand_canon/_is_brand_canon) — инъектим.
_cn.configure({"_victory_conn": _victory_conn, "_ct_segment": _ct_segment,
               "_is_brand_canon": _is_brand_canon, "_brand_canon": _brand_canon})

# uac_verifier: резолвер ct-сегмента, чтобы модельный фильтр товарки требовать только для «Модели»
# (иначе ложный UAC_PRODUCT_MODEL_FILTER_MISSING на ct-«Общее» вроде ct0001/ct0006).
from . import uac_verifier as _uv  # noqa: E402
_uv.configure({"_ct_segment": _ct_segment})


def _vendor_value(*args, **kwargs):
    return _create_set_feeds_module()._vendor_value(*args, **kwargs)

def _vendor_filter_values(*args, **kwargs):
    return _create_set_feeds_module()._vendor_filter_values(*args, **kwargs)

def _model_field_values(*args, **kwargs):
    return _create_set_feeds_module()._model_field_values(*args, **kwargs)

def _listing_name_value(*args, **kwargs):
    return _create_set_feeds_module()._listing_name_value(*args, **kwargs)

def _brand_level_collection_id(*args, **kwargs):
    return _create_set_feeds_module()._brand_level_collection_id(*args, **kwargs)

def _feed_models_from_collections(*args, **kwargs):
    return _create_set_feeds_module()._feed_models_from_collections(*args, **kwargs)

def _tp7_product_feed_filters(*args, **kwargs):
    return _create_set_feeds_module()._tp7_product_feed_filters(*args, **kwargs)

def _tp7_listing_plus_filter(*args, **kwargs):
    return _create_set_feeds_module()._tp7_listing_plus_filter(*args, **kwargs)

def _tp7_listings_minus_filters(*args, **kwargs):
    return _create_set_feeds_module()._tp7_listings_minus_filters(*args, **kwargs)


# ── Shared минус-набор для tp2/tp4 (TEXT_CAMPAIGN) — канон CODER.md §«Минус» ──────
# Путь ИДЕНТИЧЕН tp1/tp5: взять существующий набор «Минуса общие» из аккаунта через
# v5 negativekeywordsharedsets.get. Если в аккаунте нет ни одного — собрать минусы
# из пака M3 (все ct данного tp, объединить+дедупликация), обрезать по бюджету
# КАМПАНИИ 20 000 символов БЕЗ пробелов (лимит Директа), создать набор.
# Привязка — через v5 campaigns.update (NegativeKeywordSharedSetIds) — для TEXT_CAMPAIGN
# это валидное поле верхнего уровня (в отличие от tp1/tp5 где Grid libraryMinusKeywordsIds).
# Карта механизма привязки минусов по слепку — ЕДИНЫЙ источник: `create_set_minus`.
# Здесь раньше лежал ВТОРОЙ экземпляр той же карты, и они разошлись: `kuderko` был внесён только
# в create_set_minus, а create_set_feed_builders берёт карту через deps ИМЕННО отсюда. Реэкспорт
# вместо копии: `create_set_minus` ничего не импортирует из automation_runtime (только DI через
# configure), поэтому цикла импорта нет.
from .create_set_minus import _SLEPOK_MINUS_MODE  # noqa: E402  (единая карта режимов минусов)


def _create_set_minus_deps() -> dict:
    return {
        "_SLEPOK_KEY": _SLEPOK_KEY,
        "_enabled_minus_words": _enabled_minus_words,
        "_v5_call": _v5_call,
        "_v5_err": _v5_err,
        "_v5_get": _v5_get,
        "kp": kp,
    }


def _create_set_minus_module():
    from . import create_set_minus as csm
    csm.configure(_create_set_minus_deps())
    return csm


def _collect_pack_minus(*args, **kwargs):
    return _create_set_minus_module()._collect_pack_minus(*args, **kwargs)


def _minus_char_budget(*args, **kwargs):
    return _create_set_minus_module()._minus_char_budget(*args, **kwargs)


def _get_or_create_minus_set(*args, **kwargs):
    return _create_set_minus_module()._get_or_create_minus_set(*args, **kwargs)


def _attach_minus_set_to_text_campaign(*args, **kwargs):
    return _create_set_minus_module()._attach_minus_set_to_text_campaign(*args, **kwargs)


def _apply_campaign_direct_minus(*args, **kwargs):
    return _create_set_minus_module()._apply_campaign_direct_minus(*args, **kwargs)


def _ensure_named_minus_sets(*args, **kwargs):
    """Именованные наборы минус-фраз слепка → библиотека минус-фраз аккаунта (идемпотентно)."""
    return _create_set_minus_module().ensure_named_minus_sets_cached(*args, **kwargs)


def _create_search_test_campaign(
    token: str,
    login: str,
    name: str,
    audiences: list[dict],
    counter_id: int = 0,
    mode: str = "search",
    pay: str = "cpa",
    goal_id: int = 0,
    cpa_rub: int = 0,
    budget_rub: int = 0,
) -> dict:
    """Создать текстовую кампанию (TEXT_CAMPAIGN) через API v5.

    mode: 'search' (tp2/tp5 — стратегия на ПОИСКЕ, сеть OFF) | 'network' (tp1 РСЯ — поиск OFF,
    стратегия в СЕТЯХ). По факту живых аккаунтов tp1/tp2/tp5 = TEXT_CAMPAIGN, отличаются стороной.

    Стратегия — ТОЛЬКО конверсионная (правило «Глобальных настроек», по факту аккаунтов):
      pay='tcpa' → AVERAGE_CPA        (оптимизация конверсий по средней цене, оплата за клики)
      pay='cpa'  → PAY_FOR_CONVERSION (оплата за конверсию)
    Обе требуют GoalId + цену (AverageCpa/Cpa) + WeeklySpendLimit. Деньги в МИКРО (₽×1_000_000).
    StartDate в будущем + State по умолчанию = безопасный черновик (без трат до явного запуска).

    Параметры:
        goal_id   — цель Метрики (обязательна для конверсионных стратегий)
        cpa_rub   — целевая цена конверсии, ₽; budget_rub — недельный бюджет (WeeklySpendLimit), ₽
        audiences — [{"id":<retargeting_list_id>, "adjustment":<int%>}] (может быть пустым).

    Возвращает {"name","ok","campaign_id","modifiers_set"} или {"name","ok":False,"error"}.
    """
    # ── 1. campaigns.add — безопасный черновик (StartDate в будущем, State=OFF по умолчанию) ──
    # Конверсионная стратегия по pay; сторона по mode (tp1 РСЯ → сети; tp2/tp5 → поиск). ₽→микро.
    _RUB = 1_000_000
    goal = int(goal_id) if goal_id else None
    cpa_micros = int(cpa_rub) * _RUB if cpa_rub else None
    wsl_micros = int(budget_rub) * _RUB if budget_rub else None
    if pay == "cpa":            # оплата за конверсию
        side = {"BiddingStrategyType": "PAY_FOR_CONVERSION",
                "PayForConversion": {**({"Cpa": cpa_micros} if cpa_micros else {}),
                                     **({"GoalId": goal} if goal else {}),
                                     **({"WeeklySpendLimit": wsl_micros} if wsl_micros else {})}}
    else:                       # tcpa — оптимизация по средней цене конверсии (оплата за клики)
        side = {"BiddingStrategyType": "AVERAGE_CPA",
                "AverageCpa": {**({"AverageCpa": cpa_micros} if cpa_micros else {}),
                               **({"GoalId": goal} if goal else {}),
                               **({"WeeklySpendLimit": wsl_micros} if wsl_micros else {})}}
    _off = {"BiddingStrategyType": "SERVING_OFF"}
    bidding = {"Search": _off, "Network": side} if mode == "network" else {"Search": side, "Network": _off}
    campaign_payload = {
        "Campaigns": [{
            "Name": name,
            "StartDate": "2030-01-01",          # дата в далёком будущем → не запустится случайно
            "TextCampaign": {
                "BiddingStrategy": bidding,
                # Инварианты (CAMPAIGN_INVARIANTS.md): персонализация ВЫКЛ, мониторинг ВКЛ, расш.гео ВЫКЛ,
                # «Карты и список организаций» ВЫКЛ.
                # ENABLE_COMPANY_INFO=NO дублирует enableCompanyInfo=False из Grid-финализации:
                # если Grid упадёт (протухшие куки/CSRF), кампания всё равно НЕ будет привязана к
                # организации и НЕ попадёт на Карты. Аналогично campaign.py::create_unified_campaign
                # (UnifiedCampaign), где это поле проверено live 2026-06-21 на porg-psm5h7q6.
                # Товарная галерея (placementTypes=["SEARCH_PAGE","ADV_GALLERY"]) — только Grid-only,
                # v5 не умеет; при сбое Grid кампания останется без галереи (см. grid_warn ниже).
                "Settings": [
                    {"Option": "ALTERNATIVE_TEXTS_ENABLED", "Value": "NO"},          # #3 персонализация (адаптивные тексты) ВЫКЛ
                    {"Option": "ENABLE_SITE_MONITORING", "Value": "YES"},            # #4 мониторинг сайта ВКЛ
                    {"Option": "ENABLE_AREA_OF_INTEREST_TARGETING", "Value": "NO"},  # #5 расширенный гео ВЫКЛ
                    {"Option": "ENABLE_COMPANY_INFO", "Value": "NO"},               # «Карты/список организаций» ВЫКЛ (B1-фикс: резервный контроль без Grid)
                ],
                # #1 Метрика: привязка счётчика к кампании (дефолт Директа CounterIds=None)
                **({"CounterIds": {"Items": [int(counter_id)]}} if counter_id else {}),
            }
        }]
    }
    j_add = _v5_call("campaigns", "add", token, login, campaign_payload)
    if "error" in j_add:
        return {"name": name, "ok": False, "error": _v5_err(j_add)}
    results = (j_add.get("result") or {}).get("AddResults", [])
    if not results:
        return {"name": name, "ok": False, "error": "API вернул пустой AddResults"}
    first = results[0]
    api_errors = first.get("Errors") or []
    if api_errors:
        err_text = "; ".join(
            e.get("Message") or e.get("Details") or str(e) for e in api_errors
        )
        return {"name": name, "ok": False, "error": err_text}
    campaign_id = first.get("Id")
    if not campaign_id:
        return {"name": name, "ok": False, "error": "API не вернул Id кампании"}

    # ── 2. bidmodifiers.add — корректировки аудиторий (если переданы) ─────────
    if not audiences:
        return {"name": name, "ok": True, "campaign_id": campaign_id, "modifiers_set": 0}

    retargeting_adjustments = []
    for aud in audiences:
        aud_id = aud.get("id")
        adjustment = aud.get("adjustment", 0)
        if not aud_id:
            continue
        # Конверсия: adjustment % → BidModifier (clamp 0..1300)
        bm = max(0, min(1300, 100 + int(adjustment)))
        retargeting_adjustments.append({
            "RetargetingConditionId": int(aud_id),
            "BidModifier": bm,
        })

    if not retargeting_adjustments:
        return {"name": name, "ok": True, "campaign_id": campaign_id, "modifiers_set": 0}

    bm_payload = {
        "BidModifiers": [{
            "CampaignId": campaign_id,
            "RetargetingAdjustments": retargeting_adjustments,
            # NOTE: поле Level НЕ передаём — иначе ошибка 8000 (проверено)
        }]
    }
    j_bm = _v5_call("bidmodifiers", "add", token, login, bm_payload)
    if "error" in j_bm:
        bm_err = _v5_err(j_bm)
        return {"name": name, "ok": True, "campaign_id": campaign_id, "modifiers_set": 0,
                "error": f"кампания создана, но bidmodifier упал: {bm_err}"}
    bm_results = (j_bm.get("result") or {}).get("AddResults", [])
    bm_api_errors = []
    for r in bm_results:
        bm_api_errors.extend(r.get("Errors") or [])
    if bm_api_errors:
        bm_err_text = "; ".join(
            e.get("Message") or e.get("Details") or str(e) for e in bm_api_errors
        )
        return {"name": name, "ok": True, "campaign_id": campaign_id, "modifiers_set": 0,
                "error": f"кампания создана, но bidmodifier отклонён: {bm_err_text}"}

    # Считаем сколько корректировок добавлено (Ids в первом AddResult)
    modifiers_set = len((bm_results[0].get("Ids") or []) if bm_results else [])
    if modifiers_set == 0:
        modifiers_set = len(retargeting_adjustments)  # fallback: считаем по запросу
    return {"name": name, "ok": True, "campaign_id": campaign_id, "modifiers_set": modifiers_set}


# ── Движок tp2: наполнение Поисковой кампании группами/ключами/объявлениями ──────
def _kw_positive_words(phrase: str) -> int:
    """Число ПОЗИТИВНЫХ слов фразы — минус-части (`-слово`) не считаются.

    Лимит Директа: «Количество слов для одной ключевой фразы — не более 7, без учёта стоп-слов»,
    а минус-фразы лимитируются ОТДЕЛЬНО (каждая ≤7 слов) и в лимит самой фразы не входят
    (docs/troubleshooting/interface.md «Ключевые фразы» / «Минус-фразы на группу»). Символьный
    лимит 4096 — наоборот, «включая минус-слова», он считается по всей строке (см. `_kw_clean`).

    Боевой факт 2026-07-28: `_kw_clean` считал ВСЕ токены, поэтому легальный ключ
    «drom ru продажа авто -запчасти -экзамен …» (4 позитивных + 13 минусов = 17 токенов)
    выбрасывался целиком → группы tp1 «Общие - КС» уезжали пустыми (ct0010, 155 ключей).
    Аналог для дискриминации моделей — `text_gen._kw_positive_tokens`."""
    return sum(1 for w in str(phrase or "").split() if not w.startswith("-"))


def _kw_clean(words: list, cap: int) -> list:
    """Очистка ключей под Директ: strip, dedup, ≤7 ПОЗИТИВНЫХ слов (минус-части не считаются,
    но СОХРАНЯЮТСЯ в фразе), разумная длина, cap по count."""
    out, seen = [], set()
    for w in words:
        w = re.sub(r"\s+", " ", (str(w) or "").strip())
        if not w or _kw_positive_words(w) > 7 or len(w) > 4096:
            continue
        k = w.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(w)
        if len(out) >= cap:
            break
    return out


# ── Manual-креативы: изображения из /opt/creatives/Manual/{ct}/ ──────────────
MANUAL_CREATIVES_DIR = "/opt/creatives/Manual"

_AC_GROUP_CAP = 150
_AC_CHUNK_AG = 100
_AC_CHUNK_KW = 1000
_AC_CHUNK_AD = 100
_AC_BATCH_SLEEP = 0.4
_RA_TITLE_MAX = 56
_RA_TEXT_MAX = 81
_RA_TITLES_CAP = 7
_RA_TEXTS_CAP = 3
_CALLOUT_POOL_CAP = 200
_CALLOUT_PER_CAMPAIGN_CAP = 8
_MINUS_SHARED_SET_CHAR_BUDGET = 4_096
_MINUS_CAMPAIGN_CHAR_BUDGET = 20_000
_AUTOTARGET_KW = "---autotargeting"


def _create_set_assets_deps() -> dict:
    return {
        "_AC_BATCH_SLEEP": _AC_BATCH_SLEEP,
        "_CALLOUT_MAX_EACH": _CALLOUT_MAX_EACH,
        "_CALLOUT_PER_CAMPAIGN_CAP": _CALLOUT_PER_CAMPAIGN_CAP,
        "_CALLOUT_POOL_CAP": _CALLOUT_POOL_CAP,
        "_GENERIC_TEXT_FILLERS": _GENERIC_TEXT_FILLERS,
        "_RA_TEXTS_CAP": _RA_TEXTS_CAP,
        "_RA_TEXT_MAX": _RA_TEXT_MAX,
        "_RA_TITLES_CAP": _RA_TITLES_CAP,
        "_RA_TITLE_MAX": _RA_TITLE_MAX,
        "_coder_name_real_brand": _coder_name_real_brand,
        "_coherent_discounts": _coherent_discounts,
        # Финальная сборка адаптива (_upgrade_credit_*) идёт ПОСЛЕ всех `_cf`, поэтому
        # site_type-фильтр нужен и здесь — иначе хардкод-варианты про «новые авто»
        # уезжают в Б/У-кампании мимо любой фильтрации выше (tp1–tp5).
        "_drop_new_car": _drop_new_car,
        "_is_bu_site": _is_bu_site,
        "_fill_title": _fill_title,
        "_trim_to_word": _trim_to_word,
        "_v5_call": _v5_call,
        "_variant_norm_key": _variant_norm_key,
        "kp": kp,
    }


def _create_set_assets_module():
    from . import create_set_assets as csa
    csa.configure(_create_set_assets_deps())
    return csa


def _manual_creative_paths(*args, **kwargs):
    return _create_set_assets_module()._manual_creative_paths(*args, **kwargs)

def _dedup_cap(*args, **kwargs):
    return _create_set_assets_module()._dedup_cap(*args, **kwargs)

def _combo_fill_titles(*args, **kwargs):
    return _create_set_assets_module()._combo_fill_titles(*args, **kwargs)

def _combo_fill_texts(*args, **kwargs):
    return _create_set_assets_module()._combo_fill_texts(*args, **kwargs)

def _credit_title_bucket(*args, **kwargs):
    return _create_set_assets_module()._credit_title_bucket(*args, **kwargs)

def _credit_title_anchor(*args, **kwargs):
    return _create_set_assets_module()._credit_title_anchor(*args, **kwargs)

def _valid_pack_brand_name(*args, **kwargs):
    return _create_set_assets_module()._valid_pack_brand_name(*args, **kwargs)

def _pack_group_display_name(*args, **kwargs):
    return _create_set_assets_module()._pack_group_display_name(*args, **kwargs)

def _struct_ct_names(*args, **kwargs):
    # {ctNNNN: тема из структуры} для НЕ-авто слепков (dmp); {} для авто — делегируем в tp1-builders.
    return _create_set_tp1_builder_module()._struct_ct_names(*args, **kwargs)

def _trim_ad_line(*args, **kwargs):
    return _create_set_assets_module()._trim_ad_line(*args, **kwargs)

def _needs_credit_title_upgrade(*args, **kwargs):
    return _create_set_assets_module()._needs_credit_title_upgrade(*args, **kwargs)

def _upgrade_credit_titles(*args, **kwargs):
    return _create_set_assets_module()._upgrade_credit_titles(*args, **kwargs)

def _upgrade_credit_texts(*args, **kwargs):
    return _create_set_assets_module()._upgrade_credit_texts(*args, **kwargs)

def _responsive_ad(*args, **kwargs):
    return _create_set_assets_module()._responsive_ad(*args, **kwargs)

def _responsive_image_hashes(*args, **kwargs):
    return _create_set_assets_module()._responsive_image_hashes(*args, **kwargs)

def _responsive_retry_items(*args, **kwargs):
    return _create_set_assets_module()._responsive_retry_items(*args, **kwargs)

def _chunks(*args, **kwargs):
    return _create_set_assets_module()._chunks(*args, **kwargs)

def _normalize_callout_text(*args, **kwargs):
    return _create_set_assets_module()._normalize_callout_text(*args, **kwargs)

def _callout_semantic_key(*args, **kwargs):
    return _create_set_assets_module()._callout_semantic_key(*args, **kwargs)

def _dedup_callouts(*args, **kwargs):
    return _create_set_assets_module()._dedup_callouts(*args, **kwargs)

def _dedup_callout_ids(*args, **kwargs):
    return _create_set_assets_module()._dedup_callout_ids(*args, **kwargs)

def _ensure_callout_exts(*args, **kwargs):
    return _create_set_assets_module()._ensure_callout_exts(*args, **kwargs)


def _create_set_text_builder_deps() -> dict:
    return {
        "_AC_BATCH_SLEEP": _AC_BATCH_SLEEP,
        "_AC_CHUNK_AD": _AC_CHUNK_AD,
        "_AC_CHUNK_AG": _AC_CHUNK_AG,
        "_AC_CHUNK_KW": _AC_CHUNK_KW,
        "_AC_GROUP_CAP": _AC_GROUP_CAP,
        "_AUTOTARGET_KW": _AUTOTARGET_KW,
        "_MINUS_SHARED_SET_CHAR_BUDGET": _MINUS_SHARED_SET_CHAR_BUDGET,
        "_SLEPOK_KEY": _SLEPOK_KEY,
        "_UTM_TEMPLATE_TP1": _UTM_TEMPLATE_TP1,
        "_account_offer_prices": _account_offer_prices,
        "_enabled_minus_words": _enabled_minus_words,
        "_account_offer_urls": _account_offer_urls,
        "_ag_part1_map": _ag_part1_map,
        "_brand_level_url": _brand_level_url,
        "_cached_upload_image": _cached_upload_image,
        "_parallel_upload_images": _parallel_upload_images,
        "_chunks": _chunks,
        "_creative_images_for_ct": _creative_images_for_ct,
        "_ct_segment": _ct_segment,
        "_ensure_callout_exts": _ensure_callout_exts,
        "_feed_url_for_model": _feed_url_for_model,
        "_filter_group_keywords": _filter_group_keywords,
        "_gc_ct": _gc_ct,
        "_grid_set_ad_prices": _grid_set_ad_prices,
        "_grid_update_adaptive_ads": _grid_update_adaptive_ads,
        "_group_ad_price": _group_ad_price,
        "_json": _json,
        "_kw_clean": _kw_clean,
        "_minus_char_budget": _minus_char_budget,
        "_model_page_href": _model_page_href,
        "_next_title2": _next_title2,
        "_responsive_ad": _responsive_ad,
        "_responsive_retry_items": _responsive_retry_items,
        "_rsya_texts": _rsya_texts,
        "_rsya_titles": _rsya_titles,
        "_segment_donor": _segment_donor,
        "_slepok_campaign_content": _slepok_campaign_content,
        "_strip_url_query": _strip_url_query,
        "_title_from_template": _title_from_template,
        "_v501_svc": _v501_svc,
        "_v5_call": _v5_call,
        "_v5_err": _v5_err,
        "_valid_pack_brand_name": _valid_pack_brand_name,
        "_pack_group_display_name": _pack_group_display_name,
        "_struct_ct_names": _struct_ct_names,
        "cmc": cmc,
        "kp": kp,
        # gc/gf: token-путь tp2/tp4 (DIRECT_API_FIRST) — Фаза 1 создаёт группы АТОМАРНО через Grid
        # AddUnifiedAdGroups(profile=search_tp2) + post-create Grid-репейр картинок/цен (Фаза 3.4).
        # Раньше не прокидывались → Фаза 3.4 gf молча падала в except (флаг OFF — не исполнялась).
        "gc": gc,
        "gf": gf,
    }


def _create_set_text_builder_module():
    from . import create_set_text_builders as cstb
    cstb.configure(_create_set_text_builder_deps())
    return cstb


def _build_tp2_adgroups(*args, **kwargs):
    return _create_set_text_builder_module()._build_tp2_adgroups(*args, **kwargs)

def _struct_cts(*args, **kwargs):
    return _create_set_text_builder_module()._struct_cts(*args, **kwargs)

def _struct_has_tp(*args, **kwargs):
    return _create_set_text_builder_module()._struct_has_tp(*args, **kwargs)

def _tp2_struct_cts(*args, **kwargs):
    return _create_set_text_builder_module()._tp2_struct_cts(*args, **kwargs)

def _text_group_name(*args, **kwargs):
    return _create_set_text_builder_module()._text_group_name(*args, **kwargs)

def _build_text_from_pack(*args, **kwargs):
    return _create_set_text_builder_module()._build_text_from_pack(*args, **kwargs)

def _build_tp2_from_pack(*args, **kwargs):
    return _create_set_text_builder_module()._build_tp2_from_pack(*args, **kwargs)


# DI для blueprint_targeting: все зависимости определены выше (_json, _ag_part1_map, _SLEPOK_KEY)
# и _struct_cts (шим выше по файлу) — инъектим ПОСЛЕ последней (_struct_cts).
_btg.configure({
    "_json": _json,
    "_ag_part1_map": _ag_part1_map,
    "_struct_cts": _struct_cts,
    "_SLEPOK_KEY": _SLEPOK_KEY,
})


def _sed_profile_invalidate() -> None:
    """Сброс кэша targeting_profile после записи редактором (профиль кэшируется в _btg)."""
    _btg._TARGETING_PROFILE_CACHE = None


_sed.configure({
    "_victory_conn_rw": _victory_conn_rw,
    "_profile_invalidate": _sed_profile_invalidate,
    "_ct_segment": _ct_segment,
    "_ag_part1_map": _ag_part1_map,
    # Карточка ключей tp6/tp7 обязана показывать то, что реально уедет в кабинет → читает ключи
    # ТЕМ ЖЕ путём, что и создание (пак → tp67_real_keywords.json → цепочка tp7↦tp6).
    "_tp67_keywords_for": _tp67_keywords_for,
    "_tp67_keywords_for_groups": _tp67_keywords_for_groups,
    "_tp67_targeting_mode": _tp67_targeting_mode,   # гейт: ключи только у keyword-позиций, как в создании
})


# ── Движок tp1 (РСЯ): создание кампании + бренд-групп из пака M3 ─────────────
# Отличия от tp2:
#  - stратегия: ЕПК mode=network_cpa (AVERAGE_CPA), НЕ TextCampaign/HIGHEST_POSITION
#  - группы: каждая ct-папка пака = отдельная группа с кодер-именем (см. CODER.md)
#  - объявления: TextAd с AdImageHash (картинка из пака через adimages.add)
#  - UTM: TrackingParams на уровне группы (#2 инвариант)
#  - минус-слова: на уровне группы (из пака scherbakova_minus)
#  - sitelinks: SitelinkSetId на объявлении (из direct_slepok_content)
#  - callouts: AdExtensions на объявлении (из пака + read_callouts scherbakova)
#  - БЕЗ карт: mode=network_cpa выключает ShowInMaps

_UTM_TEMPLATE_TP1 = cmc.UTM_TEMPLATE  # макрос UTM из campaign.py


def _create_set_tp1_builder_deps() -> dict:
    return {
        "_AC_BATCH_SLEEP": _AC_BATCH_SLEEP,
        "_AC_CHUNK_AD": _AC_CHUNK_AD,
        "_AC_CHUNK_AG": _AC_CHUNK_AG,
        "_AC_CHUNK_KW": _AC_CHUNK_KW,
        "_AC_GROUP_CAP": _AC_GROUP_CAP,
        "_AUTOTARGET_KW": _AUTOTARGET_KW,
        "_GENERIC_AT_TITLES": _GENERIC_AT_TITLES,
        "_GENERIC_TEXT_FILLERS": _GENERIC_TEXT_FILLERS,
        "_GRID_URL": _GRID_URL,
        "_SLEPOK_KEY": _SLEPOK_KEY,
        "_UTM_TEMPLATE_TP1": _UTM_TEMPLATE_TP1,
        "_account_offer_prices": _account_offer_prices,
        "_account_offer_urls": _account_offer_urls,
        "_ag_part1_map": _ag_part1_map,
        "_ai_common_sitelinks": _ai_common_sitelinks,
        "_ai_sitelinks": _ai_sitelinks,
        "_apply_campaign_direct_minus": _apply_campaign_direct_minus,
        "_apply_corrections": _apply_corrections,
        "_brand_canon": _brand_canon,
        "_brand_level_url": _brand_level_url,
        "_cached_upload_image": _cached_upload_image,
        "_parallel_upload_images": _parallel_upload_images,
        "_chunks": _chunks,
        "_coherent_payments": _coherent_payments,
        "_ensure_callout_exts": _ensure_callout_exts,   # ревью 03.07 #4: имени не было в deps → callouts-блок tp1 молча падал NameError
        "_touch_running_jobs_heartbeat": _touch_running_jobs_heartbeat,
        "_creative_images_for_ct": _creative_images_for_ct,
        "_ct_segment": _ct_segment,
        "_drop_new_car": _drop_new_car,   # круг 4: фид-товарка галерея на Б/У режет «новые авто» (create_set_tp1_builders.py:980-981); без deps → NameError
        "_enabled_minus_marks": _enabled_minus_marks,
        "_enabled_minus_models": _enabled_minus_models,
        "_enabled_minus_model_pairs": _enabled_minus_model_pairs,
        "_enabled_minus_places": _enabled_minus_places,
        "_enabled_minus_words": _enabled_minus_words,
        "_feed_collections": _feed_collections,
        "_feed_models_from_collections": _feed_models_from_collections,
        "_feed_url_for_model": _feed_url_for_model,
        "_filter_group_keywords": _filter_group_keywords,
        "_finalize_rsya": _finalize_rsya,
        "_first_url_feed": _first_url_feed,
        "_get_or_reuse_sitelink_set": _get_or_reuse_sitelink_set,
        "_get_or_reuse_sitelink_sets": _get_or_reuse_sitelink_sets,   # батч: N наборов → 1 sitelinks.add
        "_grid_ad_price_payload": _grid_ad_price_payload,
        "_grid_bid_modifiers": _grid_bid_modifiers,
        "_grid_feed_offer_prices": _grid_feed_offer_prices,
        "_grid_list_campaigns": _grid_list_campaigns,
        "_grid_price_feed": _grid_price_feed,
        "_grid_set_ad_prices": _grid_set_ad_prices,
        "_grid_update_adaptive_ads": _grid_update_adaptive_ads,
        "_group_ad_price": _group_ad_price,
        "_kw_clean": _kw_clean,
        "_listing_name_value": _listing_name_value,
        "_m3_content_status": _m3_content_status,
        "_model_field_values": _model_field_values,
        "_model_page_href": _model_page_href,
        "_next_title2": _next_title2,
        "_pack_group_display_name": _pack_group_display_name,
        "_resolve_campaign_assets": _resolve_campaign_assets,
        "_responsive_ad": _responsive_ad,
        "_rsya_texts": _rsya_texts,
        "_rsya_titles": _rsya_titles,
        "_slepok_campaign_content": _slepok_campaign_content,
        "_strip_url_query": _strip_url_query,
        "_text_group_name": _text_group_name,
        "_title_from_template": _title_from_template,
        "_v501_call": _v501_call,
        "_v501_svc": _v501_svc,
        "_v5_call": _v5_call,
        "_v5_err": _v5_err,
        "_vendor_filter_values": _vendor_filter_values,
        "_vendor_value": _vendor_value,
        "_valid_pack_brand_name": _valid_pack_brand_name,
        "cmc": cmc,
        "gc": gc,
        "gf": gf,
        "kp": kp,
    }


def _create_set_tp1_builder_module():
    from . import create_set_tp1_builders as cstp1
    cstp1.configure(_create_set_tp1_builder_deps())
    return cstp1

def _tp1_group_name(*args, **kwargs):
    return _create_set_tp1_builder_module()._tp1_group_name(*args, **kwargs)








def _tp1_video_ads(*args, **kwargs):
    return _create_set_tp1_builder_module()._tp1_video_ads(*args, **kwargs)


def _build_tp1_adgroups(*args, **kwargs):
    return _create_set_tp1_builder_module()._build_tp1_adgroups(*args, **kwargs)


def _ai_sitelinks(login: str, agent_key: str, site_type: str) -> list[dict]:
    """Быстрые ссылки через ИИ M3 — ФОЛБЭК для tp1, когда у слепка их нет (директива пользователя).
    → [{title,description}] (8). При недоступности M3 — _GENERIC_SITELINK_FILLERS (никогда не пусто)."""
    try:
        from . import ai_agents as A
        agent = A.get_agent(agent_key)
        ctx = _promo_ctx(login) or {"site_type": site_type, "domain": "", "salon": "", "city": ""}
        if agent:
            msgs = A.build_sitelinks_messages(agent, ctx)
            txt, err = _m3_complete_url(_M3_LLM_URLS_14B[0], msgs, max_tokens=400,
                                        temperature=0.7, top_p=0.9, repetition_penalty=1.15)
            if not err:
                raw = _promo_extract_json(txt) or {}
                out = [{"title": (s.get("title") or "").strip(), "description": (s.get("description") or "").strip()}
                       for s in (raw.get("sitelinks") or []) if isinstance(s, dict) and (s.get("title") or "").strip()][:8]
                if out:
                    return out
    except Exception:  # noqa: BLE001 — генерация не критична, ниже общий фолбэк
        pass
    return list(_GENERIC_SITELINK_FILLERS)


def _slepok_sitelinks_for(slepok: str, site_type: str) -> list[dict]:
    """Быстрые ссылки из структуры слепка для (slepok × site_type).
    Источник — колонка sitelinks в direct_slepok_content.
    Возвращает [{Title, Href, Description}, ...] или []."""
    try:
        conn = _victory_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT content FROM public.direct_slepok_content "
            "WHERE slepok=%s AND site_type=%s AND kind='sitelinks' LIMIT 1",
            (slepok, site_type))
        row = cur.fetchone()
        conn.close()
        if not row or not row[0]:
            return []
        raw = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        if isinstance(raw, list):
            return [{"Title": s.get("title", "")[:30],
                     "Href": s.get("href", s.get("url", "")),
                     "Description": s.get("description", "")[:60]}
                    for s in raw if isinstance(s, dict) and s.get("title")]
    except Exception:  # noqa: BLE001
        pass
    return []


def _norm_sitelinks_for_v501(sitelinks: list, href: str = "") -> list[dict]:
    """Нормализовать быстрые ссылки из M3/item/БД в формат sitelinks.add.
    Href у всех ссылок ведёт на главную аккаунта: /sl1.. давали 404."""
    from . import ai_agents as A
    _title_min = A.SITELINK_TITLE_MIN_ACCEPT            # порог приёмки; цель генерации = SITELINK_TITLE_TARGET_MIN
    base = (href or "").rstrip("/")
    out, seen, seen_topics = [], set(), set()
    for s in list(sitelinks or []) + list(_GENERIC_SITELINK_FILLERS):
        if not isinstance(s, dict):
            continue
        title = _trim_to_word(_sanitize_content(s.get("Title") or s.get("title") or "", 30), 30).strip()
        desc = _trim_to_word(_sanitize_content(s.get("Description") or s.get("description") or "", 60), 60).strip()
        if not title or len(title) < _title_min:  # порог приёмки: отсечь совсем короткие
            continue
        if _bad_ad_sitelink(title, desc):
            continue
        k = _variant_norm_key(f"{title} {desc}") or title.lower()
        if k in seen:
            continue
        seen.add(k)
        # Семантический topic-дедуп: кредит/платёж/взнос — одна тема (не более 1 ссылки).
        # Ловит дубль «Платёж от 9 000 ₽/мес» (реальный) + «Первый взнос 0 ₽» (филлер).
        _t_lower = title.lower().replace("ё", "е")
        _topic = "credit" if re.search(r"кредит|платеж|взнос|рассрочк", _t_lower) else None
        if _topic and _topic in seen_topics:
            continue
        if _topic:
            seen_topics.add(_topic)
        # Пустой Href → Яндекс отбивает валидацией → весь набор молча теряется.
        # Берём собственный href ссылки (если есть), иначе base, иначе пропускаем.
        sl_href = s.get("Href") or s.get("href") or s.get("url") or base
        if not sl_href:
            continue  # нет href ни у ссылки ни у base — не пускаем сломанный Href=''
        out.append({"Title": title, "Href": sl_href, "Description": desc})
    # Порядок источников: реальные ссылки (sitelinks) — первыми, _GENERIC_SITELINK_FILLERS — добивка до 8.
    # НЕ сортируем по длине: сортировка по длине выталкивала реальные ссылки (18-24 симв)
    # вниз из-за более длинных филлеров (22-26 симв) — теперь source-order сохраняется.
    return out[:8]


# ── Реюз наборов быстрых ссылок ПО СОДЕРЖИМОМУ (перф, 2026-07-27) ────────────
# Мотив: прогон 69a140093e78 — `v501:sitelinks.add` 444.0с / 774 вызова (17.4% wall).
# Источник вызовов — per-group наборы tp1/tp5 (`create_set_tp1_builders:541`): кэш там
# ЛОКАЛЬНЫЙ на кампанию, поэтому один и тот же набор (одинаковый deep-link группы)
# пересоздавался заново в КАЖДОЙ из 14 tp1-кампаний слепка.
# Ключ — сигнатура СОДЕРЖИМОГО (title/href-без-якоря/description + порядок) на login:
# разные наборы → разные ключи → свои id (склейки разного быть не может).
_SITELINK_SET_CACHE_LOCK = threading.Lock()
_SITELINK_SET_CACHE: dict = {}   # (login, sig) → (set_id, ts)


def _sitelink_set_cache_enabled() -> bool:
    """Kill-switch: DIRECT_SITELINK_SET_CACHE=0 → каждый набор создаётся заново (старое поведение)."""
    return os.environ.get("DIRECT_SITELINK_SET_CACHE", "1").strip().lower() not in (
        "0", "false", "off", "no", "")


def _sitelink_set_cache_ttl() -> int:
    try:
        return max(1, int(os.environ.get("DIRECT_SITELINK_SET_CACHE_TTL_SEC", "21600")))
    except Exception:  # noqa: BLE001
        return 21600


def _sitelink_set_sig(sitelinks: list) -> str:
    """Сигнатура набора по СОДЕРЖИМОМУ (не «один на аккаунт вслепую»).
    Href нормализуем тем же `_strip_href_fragment`, что и оба пути отправки (v5 и Grid),
    иначе наборы, различающиеся только служебным #якорем, получили бы разные ключи при
    физически одинаковом результате. Пустая сигнатура ('') = кэш не применяем."""
    norm = []
    for s in sitelinks or []:
        if not isinstance(s, dict):
            return ""
        norm.append([
            str(s.get("Title", s.get("title", "")) or "").strip(),
            _tn._strip_href_fragment(str(s.get("Href", s.get("href", "")) or "")),
            str(s.get("Description", s.get("description", "")) or "").strip(),
        ])
    if not norm:
        return ""
    try:
        raw = json.dumps(norm, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return ""
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _sitelink_set_cache_get(login: str, sig: str) -> int | None:
    if not sig or not _sitelink_set_cache_enabled():
        return None
    ttl = _sitelink_set_cache_ttl()
    now = time.time()
    with _SITELINK_SET_CACHE_LOCK:
        ent = _SITELINK_SET_CACHE.get((login or "", sig))
        if not ent:
            return None
        if now - ent[1] > ttl:
            _SITELINK_SET_CACHE.pop((login or "", sig), None)
            return None
        return ent[0]


def _sitelink_set_cache_put(login: str, sig: str, set_id: int | None) -> None:
    """Кладём ТОЛЬКО успешный id: провал (None) не кэшируем — следующий вызов пробует снова."""
    if not sig or not set_id or not _sitelink_set_cache_enabled():
        return
    with _SITELINK_SET_CACHE_LOCK:
        _SITELINK_SET_CACHE[(login or "", sig)] = (set_id, time.time())
        extra = len(_SITELINK_SET_CACHE) - 5000
        if extra > 0:
            for k in list(_SITELINK_SET_CACHE.keys())[:extra]:
                _SITELINK_SET_CACHE.pop(k, None)


def _sitelink_set_cache_clear(login: str | None = None) -> None:
    """Сброс кэша (тесты/переиспользование процесса воркера между аккаунтами)."""
    with _SITELINK_SET_CACHE_LOCK:
        if login is None:
            _SITELINK_SET_CACHE.clear()
        else:
            for k in [k for k in _SITELINK_SET_CACHE if k[0] == login]:
                _SITELINK_SET_CACHE.pop(k, None)


def _get_or_reuse_sitelink_set(token: str, login: str, sitelinks: list,
                               warns: list | None = None) -> int | None:
    """Создать набор быстрых ссылок через v5; при 152 — Grid (БЕЗ баллов).
    Grid-путь: GridClient.add_sitelink_set (реверс HAR23/entry262 AddSitelinkSets).
    Best-effort: при любой ошибке возвращает None (без ссылок).
    warns (опц., Fix 8): список для диагностики — реальные причины сбоя v5/Grid пишутся сюда
    И в журнал, чтобы null-набор БЕЗ ошибки перестал быть «слепым» (раньше оба пути молчали).

    Реюз: набор с ТЕМ ЖЕ содержимым на том же login отдаётся из процессного кэша без
    похода в API (см. `_sitelink_set_sig`)."""
    if not sitelinks:
        return None
    _sig = _sitelink_set_sig(sitelinks)
    _hit = _sitelink_set_cache_get(login, _sig)
    if _hit:
        return _hit
    def _warn(msg: str) -> None:
        print(f"[sitelink-set] {login}: {msg}", flush=True)
        if warns is not None:
            warns.append(msg)
    if token:
        cl = cmc.DirectV501Client(token, login)
        try:
            _sid = cl.add_sitelinks_set(sitelinks)
            _sitelink_set_cache_put(login, _sig, _sid)
            return _sid
        except cmc.DirectV501Error as e:
            if e.code != 152:
                _warn(f"v5 add_sitelinks_set провал (code={e.code}): {str(e)[:180]}")
                return None
            _warn("v5 add_sitelinks_set 152 (нет баллов) → Grid-фолбэк")
        # 152 → fallthrough к Grid
    # Grid-путь (БЕЗ баллов): работает и при 0 units, и без token
    try:
        gc = gf.get_grid_client(login)
        sid = gc.add_sitelink_set(sitelinks)
        if sid:
            _sitelink_set_cache_put(login, _sig, sid)
            return sid
        _warn("Grid add_sitelink_set вернул пусто (набор НЕ создан)")
    except Exception as e:  # noqa: BLE001
        _warn(f"Grid add_sitelink_set исключение: {str(e)[:180]}")
    return None


def _get_or_reuse_sitelink_sets(token: str, login: str, sets: list,
                                warns: list | None = None) -> list:
    """Батч-версия `_get_or_reuse_sitelink_set`: N наборов → 1 запрос `sitelinks.add`
    вместо N (v5 принимает массив SitelinksSets). Возвращает список id/None ПОЗИЦИОННО
    по sets.

    Порядок: (1) отдать из процессного кэша всё, что уже создано с тем же содержимым;
    (2) уникальные промахи одним батч-вызовом v5; (3) на набор с 152 (нет баллов) —
    Grid-фолбэк поштучно, как в одиночном пути; (4) прочая ошибка набора → None + warn.
    Транспортный сбой всего батча → поштучный `_get_or_reuse_sitelink_set` (старый путь)."""
    out: list = [None] * len(sets or [])
    if not sets:
        return out

    def _warn(msg: str) -> None:
        print(f"[sitelink-set] {login}: {msg}", flush=True)
        if warns is not None:
            warns.append(msg)

    sigs = [(_sitelink_set_sig(sl) if sl else "") for sl in sets]
    # ЛОКАЛЬНАЯ карта sig→id этого вызова. Отдельно от процессного кэша сознательно:
    # kill-switch DIRECT_SITELINK_SET_CACHE=0 выключает КРОСС-вызовный реюз, но внутри
    # одного батча одинаковые наборы всё равно создаются один раз — без локальной карты
    # дубли получали бы None (кэш при выключенном флаге всегда отдаёт промах).
    local: dict = {}
    todo: dict = {}          # sig → первый индекс с этим содержимым
    for i, sl in enumerate(sets):
        if not sl:
            continue
        hit = _sitelink_set_cache_get(login, sigs[i])
        if hit:
            out[i] = hit
            local[sigs[i]] = hit
        elif sigs[i] and sigs[i] not in todo:
            todo[sigs[i]] = i
        elif not sigs[i]:
            todo[f"_idx{i}"] = i   # без сигнатуры — создаём отдельно, без реюза
    if not todo:
        return out
    order = list(todo.values())
    if token:
        try:
            res = cmc.DirectV501Client(token, login).add_sitelinks_sets(
                [sets[i] for i in order])
        except Exception as e:  # noqa: BLE001 — транспорт/общий сбой батча → старый поштучный путь
            _warn(f"v5 add_sitelinks_sets батч упал ({str(e)[:120]}) → поштучно")
            res = None
        if res is None:
            for i in order:
                out[i] = _get_or_reuse_sitelink_set(token, login, sets[i], warns=warns)
                if out[i] and sigs[i]:
                    local[sigs[i]] = out[i]
        else:
            for k, i in enumerate(order):
                item = res[k] if k < len(res) else {"id": None, "code": 0,
                                                    "message": "нет ответа на набор"}
                if item.get("id"):
                    out[i] = item["id"]
                    _sitelink_set_cache_put(login, sigs[i], item["id"])
                elif item.get("code") == 152:
                    _warn("v5 add_sitelinks_sets 152 (нет баллов) → Grid-фолбэк")
                    out[i] = _grid_sitelink_set(login, sets[i], sigs[i], _warn)
                else:
                    _warn(f"v5 add_sitelinks_sets провал (code={item.get('code')}): "
                          f"{str(item.get('message'))[:180]}")
                if out[i] and sigs[i]:
                    local[sigs[i]] = out[i]
    else:
        for i in order:
            out[i] = _grid_sitelink_set(login, sets[i], sigs[i], _warn)
            if out[i] and sigs[i]:
                local[sigs[i]] = out[i]
    # дубли по содержимому получают тот же id, что и «первый» индекс
    for i, sl in enumerate(sets):
        if out[i] is None and sl and sigs[i]:
            out[i] = local.get(sigs[i])
    return out


def _grid_sitelink_set(login: str, sitelinks: list, sig: str, warn) -> int | None:
    """Grid-путь одного набора (БЕЗ баллов) + запись в кэш содержимого."""
    try:
        sid = gf.get_grid_client(login).add_sitelink_set(sitelinks)
        if sid:
            _sitelink_set_cache_put(login, sig, sid)
            return sid
        warn("Grid add_sitelink_set вернул пусто (набор НЕ создан)")
    except Exception as e:  # noqa: BLE001
        warn(f"Grid add_sitelink_set исключение: {str(e)[:180]}")
    return None


def _resolve_campaign_assets(
    token: str,
    login: str,
    href: str,
    *,
    sitelinks: list | None = None,
    assets: dict | None = None,
    slepok: str = "",
    site_type: str = "",
    prefer_callout_texts: list | None = None,
    prefer_callout_ids: list | None = None,
    grid_cookie: str | None = None,
) -> dict:
    """Собрать ассеты кампании и надёжно получить sitelinkSetId.

    Порядок для быстрых ссылок:
    1. Grid AddSitelinkSets — primary, без баллов/units.
    2. v5 add_sitelinks_set / reuse — fallback.
    """
    out = dict(assets or {})
    _prefer_callout_ids = [int(x) for x in (prefer_callout_ids or []) if str(x or "").strip().isdigit()]
    if not out:
        if token:
            try:
                out = _tp5_account_data(token, login, slepok, site_type,
                                        prefer_callout_texts=prefer_callout_texts,
                                        prefer_callout_ids=_prefer_callout_ids)
            except Exception:  # noqa: BLE001
                out = {}
        # Cookie/Grid-путь может работать вообще без живого v5-токена. В этом случае всё равно
        # поднимаем быстрые ссылки из слепка и уточнения через Grid, иначе tp1 создаётся «голой».
        if not out:
            out = {"sitelinks": _slepok_sitelinks_for(slepok, site_type)[:8],
                   "callout_ids": [], "promos": []}
            try:
                _gc_assets = gf.get_grid_client(login, cookie=grid_cookie)
                if _prefer_callout_ids:
                    out["callout_ids"] = _prefer_callout_ids[:8]
                elif prefer_callout_texts:
                    # Семантический дедуп + кап: ценовые «от N р/мес» и склад/склады/стоянку -45%
                    # схлопываются (иначе свалка десятков почти-дублей уходила в add_callouts).
                    _clean = _dedup_callouts(prefer_callout_texts, cap=8)
                    if _clean:
                        out["callout_ids"] = list(_gc_assets.add_callouts(_clean).values())[:8]
                if not out["callout_ids"]:
                    out["callout_ids"] = _dedup_callout_ids(_gc_assets.get_callouts())  # #24: normalize+dedup
            except Exception:  # noqa: BLE001
                pass
    out.setdefault("callout_ids", [])
    if _prefer_callout_ids:
        out["callout_ids"] = _prefer_callout_ids[:8]
    out.setdefault("promos", [])
    out.setdefault("sitelinks", [])
    out["sitelink_set_id"] = None
    out.setdefault("asset_warns", [])   # Fix 8: причины «набор быстрых ссылок не создан» — не глушим
    asset_sl = _norm_sitelinks_for_v501(sitelinks or [], href) or _norm_sitelinks_for_v501(out["sitelinks"], href)
    out["asset_sitelinks"] = asset_sl   # нормализованный шаблон для per-group наборов (#ФИКС-3)
    if asset_sl:
        _primary_err = None
        try:
            out["sitelink_set_id"] = gf.get_grid_client(login, cookie=grid_cookie).add_sitelink_set(asset_sl)
        except Exception as _sl_exc:  # noqa: BLE001
            _primary_err = str(_sl_exc)[:180]
            out["sitelink_set_id"] = None
        # Primary Grid не дал id (исключение ИЛИ пустой ответ — раньше пустой ответ молча ронял
        # в None без фолбэка) → v5/Grid-reuse с прокинутой в asset_warns причиной.
        if not out["sitelink_set_id"]:
            out["asset_warns"].append(
                f"Grid add_sitelink_set (primary) исключение: {_primary_err}" if _primary_err
                else "Grid add_sitelink_set (primary) вернул пусто")
            out["sitelink_set_id"] = _get_or_reuse_sitelink_set(
                token, login, asset_sl, warns=out["asset_warns"])
        if not out["sitelink_set_id"]:
            _m = "sitelink_set_id=None: набор быстрых ссылок НЕ создан (см. asset_warns)"
            out["asset_warns"].append(_m)
            print(f"[sitelink-set] {login}: {_m}", flush=True)
    return out


def _slepok_campaign_content(slepok: str, site_type: str) -> dict:
    """Контент слепка из kind='campaign' → {titles:[...], texts:[...], sitelinks:[{title,description}]}.
    Заголовки/тексты/ссылки лежат ВНУТРИ campaign-контента (отдельных строк нет)."""
    out = {"titles": [], "texts": [], "sitelinks": []}
    try:
        conn = _victory_conn()
        cur = conn.cursor()
        cur.execute("SELECT content FROM public.direct_slepok_content "
                    "WHERE slepok=%s AND site_type=%s AND kind='campaign' LIMIT 1",
                    (slepok, site_type))
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            c = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            out["titles"] = [t for t in (c.get("titles") or []) if t]
            out["texts"] = [t for t in (c.get("texts") or []) if t]
            out["sitelinks"] = [{"title": s.get("title", ""), "description": s.get("description", "")}
                                for s in (c.get("sitelinks") or []) if isinstance(s, dict) and s.get("title")][:8]
    except Exception:  # noqa: BLE001
        pass
    return out


















# Крупные города РФ (именительный, lower) — для фильтра «чужой город в контенте». Подстрочный
# матч ловит склонённые формы (новгород ⊂ «в Новгороде»). Дополняет аккаунт-города из БД.
def _is_bu_site(site_type: str) -> bool:
    """Тип сайта продаёт Б/У. Рабочее правило: БУ-лексика допустима только для «С пробегом»."""
    return (site_type or "").strip() == "С пробегом"


# БАГ-12: расширен фильтр б/у — «б у», «б+у», «бу», «used», «пробег» (без «с»), «подержанн»
_BU_RE = re.compile(
    r"(?i)"
    r"(?<![а-яё])(б\s*/?\s*у|б\s*\+\s*у|бу)(?![а-яё])"  # б/у, б+у, б у, бу
    r"|с\s+пробегом"                                        # с пробегом
    r"|\bпробег\b"                                          # просто «пробег»
    r"|\bused\b"                                            # used (англ.)
    r"|подержанн"                                           # подержанн(ый/ые)
)


def _drop_used_car(items: list, site_type: str) -> list:
    """Если сайт НЕ Б/У — выкинуть варианты с упоминанием Б/У («бу», «б/у», «с пробегом»,
    «подержанные», «used», «пробег»): для нового-авто-сайта такие УТП недопустимы."""
    if _is_bu_site(site_type):
        return list(items)
    return [x for x in items if not _BU_RE.search(str(x.get("title", "") if isinstance(x, dict) else x))]


# Парный фильтр к _BU_RE: лексика НОВЫХ авто, недопустимая на сайте «С пробегом».
# Матчим ТОЛЬКО связку «новый+авто/автомобиль» — прилагательное само по себе легитимно
# («новый год», «как новый», «новинка», «обновление» НЕ должны попадать под нож).
#   • левая граница (?<![а-яё]) режет «обНОВление»;
#   • окончание сразу после «нов» режет «НОВинка» (после «нов» идёт «и», не «ое/ые/ый/ых»);
#   • обязательный хвост «авто»/«автомобил» режет «новый год» (нет авто-слова);
#   • «авто(?![а-яё-])» — только отдельное слово: «новый автокредит» / «новый автосалон» ЖИВУТ,
#     а дефис в lookahead добавлен 2026-07-19: без него «новый авто-кредит» / «новые авто-услуги»
#     ложно матчились (после «авто» шёл дефис, не буква → lookahead проходил);
#   • до 2 промежуточных слов («новые китайские авто»); класс букв не пускает точку,
#     поэтому «Новый год. Авто в кредит» не схлопывается в матч.
# ⚠️ ИЗВЕСТНЫЕ ГРАНИЦЫ (не баг, а осознанный объём — см. ERRORS_JOURNAL NEW_CAR_LEXICON_ON_BU_SITE):
# НЕ ловятся синонимы без слова «авто/автомобиль» («новые машины», «новый кроссовер»,
# «новые модели», «новые иномарки») и инверсный порядок («Автомобили новые в наличии»).
# В текущих пулах таких строк нет; расширять регулярку вслепую нельзя (`нов\w+\s+[A-Z]`
# начнёт резать легитимное «Новый Haval» на сайтах НОВЫХ авто).
_NEW_RE = re.compile(
    r"(?i)(?<![а-яё])нов(?:ое|ые|ый|ых|ым|ыми|ого|ому|ой|ую)"
    r"(?:\s+[а-яёa-z]+){0,2}"
    r"\s+(?:авто(?![а-яё-])|автомобил[а-яё]*)"
)


def _drop_new_car(items: list, site_type: str) -> list:
    """Если сайт Б/У («С пробегом») — выкинуть варианты про НОВЫЕ авто («новое авто»,
    «новые авто», «новый автомобиль», «выгода на новые авто»): на Б/У-сайте такие УТП врут.

    ⚖️ Симметрия с `_drop_used_car`: тот режет Б/У-лексику, когда сайт НЕ Б/У; этот режет
    новое-авто-лексику, когда сайт Б/У. Условия ВЗАИМОИСКЛЮЧАЮЩИЕ (`_is_bu_site` истинно
    ровно для одного из двух) — на любом site_type работает максимум ОДИН из фильтров,
    выкосить набор вдвоём они не могут.
    """
    if not _is_bu_site(site_type):
        return list(items)
    return [x for x in items if not _NEW_RE.search(str(x.get("title", "") if isinstance(x, dict) else x))]
























_SLEPOK_IMG_TPS = ("tp6", "tp7", "tp1", "tp5", "tp3", "tp2", "tp4")
_COMMON_IMAGE_CTS = {
    "ct0000", "ct0001", "ct0002", "ct0003", "ct0004", "ct0005", "ct0006",
    "ct0007", "ct0008", "ct0009", "ct0010", "ct0013", "ct0014",
}
_BODY_IMAGE_CTS = {"ct0015", "ct0016", "ct0017", "ct0018"}

# DI для blueprint_content_rules: _victory_conn_rw (выше) + _COMMON_IMAGE_CTS (последняя зависимость).
# MANUAL_CREATIVES_DIR — модульная константа внутри blueprint_content_rules; _gc_ct он берёт из
# blueprint_targeting напрямую (без DI).
_bcr.configure({
    "_victory_conn_rw": _victory_conn_rw,
    "_COMMON_IMAGE_CTS": _COMMON_IMAGE_CTS,
})


def _image_ct_for_content(ct: str) -> str:
    """Какую папку картинок использовать для ct.
    Общие/аудиторные ct0000-ct0014 берут общий пул ct0000; кузова ct0015-ct0018 — свой ct;
    модельные/марочные ct — свой ct."""
    c = _gc_ct(ct)
    if c in _COMMON_IMAGE_CTS:
        return "ct0000"
    return c


# ── Визуальный дедуп пула картинок (добор до 5 «разными», а не повтором) ─────────────────────
# Один и тот же креатив может лежать в РАЗНЫХ источниках каскада (Manual/<ct> и харвест ЧУЖОГО
# слепка в `_image_store/slepki/`) как ПЕРЕСОХРАНЁННАЯ копия: другой путь, другое имя, другой md5,
# картинка та же. Живой факт 2026-07-28 (кампания 713096702, порг porg-pl6iavd5, ct0300 Tenet):
# Manual/ct0300 дал 4 креатива, 5-м добором приехал
# `_image_store/slepki/karavaev/porg-psm5h7q6/8ZN6fuwhY3sKUKPOJJHFzQ.png` — ТОТ ЖЕ баннер
# «КАСКО в подарок / Tenet T7» (md5 549664ef… ≠ f1937a7e… у Manual/ct0300_КАСКО.png, размеры
# 2021990 ≠ 2126596 байт, pHash совпадает бит-в-бит) → в объявлении 5 картинок, из них 2 одинаковые.
# Признак тождества — pHash содержимого (`uac_client._image_phash`), а НЕ путь/имя (их дедупил
# прежний `dict.fromkeys` — не помогло) и НЕ md5 (пересохранение его меняет).
# Сравнение — ТОЧНОЕ равенство pHash (hamming 0), НЕ порог: замер по всем 199 папкам
# `_manual/ct*` (2026-07-28) дал минимальную дистанцию между РАЗНЫМИ легитимными креативами
# одного шаблона = 6 (гистограмма минимумов: 6→3 папки, 8→18, 10→45, 12→69, 14→51, 16→13),
# т.е. любой порог ≥6 схлопнул бы «Зимние шины» и «Топливную карту» в одну картинку.
# Нет Pillow / битый файл → pHash=None → фолбэк на md5 байтов; не прочитали и его → путь
# считаем уникальным (решит upload). Правило Семёна: лучше 4 РАЗНЫХ, чем 5 с повтором.
def _image_identity_key(path: str) -> str:
    """Идентификатор КАРТИНКИ (не файла): «p:<pHash>» → «m:<md5>» → «x:<путь>»."""
    try:
        from .uac_client import _image_phash as _ph   # локальный импорт: без цикла и без старт-цены
        _v = _ph(path)                                # кэш по пути внутри uac_client
    except Exception:  # noqa: BLE001 — нет Pillow/numpy → визуальный уровень пропускаем
        _v = None
    if _v is not None:
        return f"p:{_v}"
    try:
        return "m:" + hashlib.md5(Path(path).read_bytes()).hexdigest()  # noqa: S324 — дедуп, не крипта
    except Exception:  # noqa: BLE001 — нечитаемый файл: считаем уникальным
        return f"x:{path}"


class _UniqueImagePool:
    """Накопитель пула: принимает только ВИЗУАЛЬНО уникальные картинки, не больше `limit`.
    Каскад источников не меняется — меняется только то, что дубль НЕ занимает слот и НЕ
    останавливает добор: за повтором каскад идёт дальше к следующему источнику."""

    def __init__(self, limit: int):
        self.limit = max(1, int(limit or 5))
        self.paths: list[str] = []
        self.dropped = 0
        self._seen: set[str] = set()

    @property
    def full(self) -> bool:
        return len(self.paths) >= self.limit

    def add(self, paths) -> None:
        for p in paths or []:
            if self.full:
                return
            if not p:
                continue
            k = _image_identity_key(str(p))
            if k in self._seen:
                self.dropped += 1                     # тот же креатив из другого источника
                continue
            self._seen.add(k)
            self.paths.append(p)


_IMG_POOL_WARNED: set = set()                          # (slepok, tp, img_ct, n) — 1 строка на процесс


def _finish_image_pool(pool: "_UniqueImagePool", *, tp: str, ct: str, img_ct: str,
                       slepok: str) -> list:
    """Логи дефицита/дублей — по образцу `UAC_IMAGES_POOL_SHORT` (warn, НЕ блокировка).
    Для tp6/tp7 короткий пул и так уезжает warning'ом в результат позиции
    (`create_set_master_product`: IMAGES_POOL_SHORT → live-верификатор `UAC_IMAGES_POOL_SHORT`);
    здесь дефицит становится видимым и для tp1/tp5, у которых своего warn-канала нет.
    Ключ дедупа строк — (слепок, tp, ct-папка, число), иначе на 300 групп × 14 кампаний
    журнал воркера залило бы тысячами одинаковых строк."""
    _n = len(pool.paths)
    _key = (slepok, tp, img_ct, _n, pool.dropped)
    if _key not in _IMG_POOL_WARNED:
        _IMG_POOL_WARNED.add(_key)
        if pool.dropped:
            print(f"[images-dedup] tp={tp} ct={ct} img_ct={img_ct} slepok={slepok}: "
                  f"отброшено визуальных дублей {pool.dropped}, уникальных {_n}", flush=True)
        if _n < pool.limit:
            print(f"[images-pool-short] IMAGES_POOL_SHORT: tp={tp} ct={ct} img_ct={img_ct} "
                  f"slepok={slepok} — уникальных картинок {_n} при цели {pool.limit}. "
                  f"Повтором НЕ добиваем (лучше {_n} разных, чем {pool.limit} с дублем); "
                  f"это предупреждение, не ошибка — кампания создаётся. "
                  f"Чтобы стало {pool.limit}, добавить PNG в Manual/{img_ct}/", flush=True)
    return pool.paths[:pool.limit]


def _creative_images_for_ct(site_type: str, tp: str, ct: str, slepok: str,
                            *, allow_manual: bool = True, limit: int = 5,
                            domain: str = "") -> list:
    limit = max(1, int(limit or 5))
    # Не-авто слепок (B2B-лидоген dmp и будущие): картинки ТОЛЬКО собственные слепка
    # (его image_slepki), БЕЗ общего manual/M3/feed-пула — там авто-баннеры/дилерские залы,
    # которые заполнили бы лимит раньше dmp-картинок (живой баг 2026-07-11: МК dmp вышли с
    # авто-салонами). У не-авто слепка все креативы лежат в его ct0000/image_slepki.
    # Per-domain: если domain задан → тег «{slepok}:{domain}» → только картинки этого домена.
    # Несовпавший домен → [] (БЕЗ fallback): мешать баннеры разных сайтов нельзя.
    if not _slepok_is_auto(slepok):
        img_tag = f"{slepok}:{domain}" if domain else slepok
        # dmp per-domain: манифест dmp-картинок (тег «dmp:<домен>») живёт ЕДИНОЖДЫ в
        # dmp/tp6/ct0000/image_slepki.txt и общий на все tp кампании. domain непустой ⟺ dmp
        # (create_set_master_product.py: _img_domain=_norm_domain if _sk=="dmp" else "") →
        # читаем из константной tp6-папки независимо от tp кампании, иначе для tp≠tp6 была бы
        # пустая папка. Прочие не-авто слепки (domain пусто) хранят картинки в СВОЁМ tp — tp как было.
        _img_tp = "tp6" if domain else tp
        own = kp.read_slepok_images(site_type, _img_tp, "ct0000", img_tag) or []
        own = list(dict.fromkeys(p for p in own if p))
        # Дедуп ТОЛЬКО байт-идентичных (путь → md5). pHash-уровень для не-авто (dmp
        # B2B-лидоген) УБРАН осознанно (root-cause 2026-07-14, подтв. Семёном): доменные
        # dmp-баннеры одного шаблона (тёмный фон + РАЗНЫЙ текст) — это РАЗНЫЕ объявления,
        # Директ их принимает. pHash hamming≤10 ошибочно схлопывал их как «дубли» → в spec
        # доезжало 2 из ~50. Теперь для не-авто оставляем path+md5 (не льём один и тот же файл
        # дважды) и возвращаем первые limit УНИКАЛЬНЫХ по содержимому. Меж-доменная изоляция
        # цела (img_tag = «dmp:<домен>»). Ветка не-авто ⟹ авто-слепки/tp1-tp5 сюда НЕ заходят
        # (у них pHash-дедуп в campaign.collect_image_files сохранён — защита от клонов).
        distinct: list[str] = []
        seen_md5: set[str] = set()
        for _p in own:
            try:
                _m = hashlib.md5(Path(_p).read_bytes()).hexdigest()  # noqa: S324 — дедуп, не крипта
            except Exception:  # noqa: BLE001 — нечитаемый файл: оставляем, upload разберётся
                distinct.append(_p)
                if len(distinct) >= limit:
                    break
                continue
            if _m in seen_md5:                            # байт-идентичный дубль под другим именем
                continue
            seen_md5.add(_m)
            distinct.append(_p)
            if len(distinct) >= limit:
                break
        return distinct
    img_ct = _image_ct_for_content(ct)
    manual_imgs = _manual_creative_paths(img_ct) if allow_manual else []
    if img_ct == "ct0000":
        # Для ct0000 не используем общий M3/feed-пул: там часто лежат модельные баннеры, которые
        # нельзя ставить в общие группы. Но если manual-пула не хватает, пользователь разрешил
        # добирать ИМЕННО из выбранного слепка (его ct0000/image_slepki), а не из общего пула M3.
        own = _filter_content_assets(
            manual_imgs or [], ct,
            source_segment="Общее", source_tp="manual", source_ct="ct0000",
            target_slepok=slepok, source_slepok="",
        )
        pool = _UniqueImagePool(limit)
        pool.add(own)
        if not pool.full:
            slepok_imgs = kp.read_slepok_images(site_type, tp, "ct0000", slepok) or []
            slepok_imgs = _prioritized_content_assets(
                slepok_imgs or [], ct,
                source_segment=site_type, source_tp=tp, source_ct="ct0000",
                target_slepok=slepok, source_slepok=slepok, limit=limit,
            )
            pool.add(slepok_imgs)
        if not pool.full:
            any_slepok_imgs = kp.read_any_slepok_images(
                site_type, tp, "ct0000", prefer=slepok,
                exclude_bu_slepoks=not _is_bu_site(site_type)) or []
            any_slepok_imgs = _prioritized_content_assets(
                any_slepok_imgs or [], ct,
                source_segment=site_type, source_tp=tp, source_ct="ct0000",
                target_slepok=slepok, source_slepok="", limit=limit,
            )
            pool.add(any_slepok_imgs)
        if not pool.full:
            extra = _explicit_content_assets_for(ct, target_slepok=slepok,
                                                 asset_types={"image", "image_slepki"}, limit=limit)
            pool.add(extra)
        return _finish_image_pool(pool, tp=tp, ct=ct, img_ct="ct0000", slepok=slepok)
    # Марки, модели и кузова: Manual/{ct} → выбранный слепок/{ct} → явно
    # разрешённые ассеты. Другие слепки и общий M3/feed-пул не подмешиваем.
    pool = _UniqueImagePool(limit)
    if manual_imgs:
        # Manual-креативы для модельного/брендового ct могут быть размечены во вкладке «Контент»
        # как ct0000/common, хотя физически лежат в папке модели. В таком случае сначала берём
        # строгий матч по ct папки, затем мягко добираем те же файлы как common-пул.
        pool.add(_filter_content_assets(
            manual_imgs, ct,
            source_segment="Общее", source_tp="manual", source_ct=img_ct,
            target_slepok=slepok, source_slepok="",
        ))
        if not pool.full:
            common_manual = _filter_content_assets(
                manual_imgs, ct,
                source_segment="Общее", source_tp="manual", source_ct="ct0000",
                target_slepok=slepok, source_slepok="",
            )
            pool.add(common_manual)
    if not pool.full:
        slepok_imgs = kp.read_slepok_images(site_type, tp, img_ct, slepok) or []
        slepok_imgs = _prioritized_content_assets(
            slepok_imgs or [], ct, source_segment=site_type, source_tp=tp, source_ct=img_ct,
            target_slepok=slepok, source_slepok=slepok, limit=limit,
        )
        pool.add(slepok_imgs)
    if not pool.full:
        any_slepok_imgs = kp.read_any_slepok_images(
            site_type, tp, img_ct, prefer=slepok,
            exclude_bu_slepoks=not _is_bu_site(site_type)) or []
        any_slepok_imgs = _prioritized_content_assets(
            any_slepok_imgs or [], ct, source_segment=site_type, source_tp=tp, source_ct=img_ct,
            target_slepok=slepok, source_slepok="", limit=limit,
        )
        pool.add(any_slepok_imgs)
    if not pool.full:
        explicit = _explicit_content_assets_for(ct, target_slepok=slepok,
                                                asset_types={"image", "image_slepki"}, limit=limit)
        pool.add(explicit)
    return _finish_image_pool(pool, tp=tp, ct=ct, img_ct=img_ct, slepok=slepok)


def _is_common_ct(ct: str) -> bool:
    return _gc_ct(ct) in _COMMON_IMAGE_CTS


def _slepok_images_any_tp(site_type: str, ct: str, slepok: str, prefer_tp: str = "") -> list:
    """Картинки СЛЕПКА по ct из ЛЮБОЙ его папки. Правило пользователя для модельных tp6/tp7:
    сначала папка слепка СВОЕГО типа (tp6/tp7) по ct модели; если там пусто — любая папка
    этого слепка по этому ct (tp1/tp5/…). → первый непустой список локальных путей."""
    order = ([prefer_tp] if prefer_tp else []) + [t for t in _SLEPOK_IMG_TPS if t != prefer_tp]
    for tp in order:
        try:
            imgs = kp.read_slepok_images(site_type, tp, ct, slepok)
        except Exception:  # noqa: BLE001
            imgs = []
        if imgs:
            return imgs
    return []


















# ── конец анти-AI правил ──────────────────────────────────────────────────────








# _bad_credit_payment_range определён — инъектим в text_norm (его _bad_ad_title/_text зовут его).
_tn.configure({"_bad_credit_payment_range": _bad_credit_payment_range})












def _sitelink_has_pct(s: dict) -> bool:
    return bool(_PCT_DISC_RE.search(f"{s.get('title', '')} {s.get('description', '')}"))


def _promo_content_lines(items: list[dict]) -> list[str]:
    lines: list[str] = []
    for it in items or []:
        lines += [str(x or "") for x in (it.get("titles") or [])]
        lines += [str(x or "") for x in (it.get("texts") or [])]
        for s in (it.get("sitelinks") or []):
            if isinstance(s, dict):
                lines.append(str(s.get("title") or ""))
                lines.append(str(s.get("description") or ""))
    return lines


def _promo_usable_for_content(promo: dict, content_lines: list[str]) -> tuple[bool, str]:
    """Не цеплять кривое/конфликтующее промо к набору кампаний."""
    blob = " ".join(str(promo.get(k) or "") for k in ("Name", "Description", "Promocode", "Type"))
    if str(promo.get("AmountUnit") or "").upper() == "PCT" and promo.get("Amount") is not None:
        blob += f" {int(float(promo.get('Amount') or 0))}%"
    # Технический мусор вроде «Скидка 50% 11212» без валюты/контекста.
    if re.search(r"(?<![\d])\d{4,}(?![\d\s]*(?:₽|руб|/мес))", blob):
        return False, "в промо есть техническое число"
    promo_pcts = set(_discount_pcts([blob]))
    content_pcts = set(_discount_pcts(content_lines))
    # Конфликт по проценту — только когда % указан в ОБОИХ (и они расходятся).
    # Если в контенте нет скидочного % (напр. B2B-лидоген dmp «до 150% лидов» не
    # парсится как скидка → content_pcts=∅) — не отбраковывать промо по проценту.
    if promo_pcts and content_pcts and promo_pcts != content_pcts:
        return False, "процент промо не совпадает с процентом в контенте"
    if any(x in blob.lower() for x in ("кешбэк", "cashback")):
        return False, "кешбэк запрещён"
    return True, ""










def _build_tp1_from_pack(*args, **kwargs):
    return _create_set_tp1_builder_module()._build_tp1_from_pack(*args, **kwargs)


# Платформы канала «только РСЯ» (network) для tp1 — без поиска/органики/галереи/карт.
_PLATFORMS_RSYA = {
    "gallery": False, "search": False, "organic": False, "network": True,
    "yandexMaps": False, "serpGeoWizard": False, "telegram": False, "maxMessenger": False,
    "taxi": False, "pillar": False, "cityBusDisplay": False, "showcaseScreen": False,
    "mediafacade": False, "supersite": False, "billboard": False, "cityboard": False,
    "cityformat": False,
}
# Места показа tp2 «Поисковая выдача» / tp4 «Поиск + Динамика» (HAR 33/34, UpdateCampaigns
# biddingStategyWithPlatforms.platforms): ТОЛЬКО search, gallery=False (в отличие от tp5 «Поиск +
# Товарная галерея», где gallery=True). placementTypes=["SEARCH_PAGE"]. Единственное различие
# tp2 vs tp4 — поле `organic` (= галочка «Динамика»): tp2 → organic=False, tp4 → organic=True.
_PLATFORMS_SEARCH_ONLY = {
    "gallery": False, "search": True, "organic": False, "network": False,
    "yandexMaps": False, "serpGeoWizard": False, "telegram": False, "maxMessenger": False,
    "taxi": False, "pillar": False, "cityBusDisplay": False, "showcaseScreen": False,
    "mediafacade": False, "supersite": False, "billboard": False, "cityboard": False,
    "cityformat": False,
}


def _create_set_finalize_deps() -> dict:
    return {
        "_CALLOUTS_Q": _CALLOUTS_Q,
        "_GRID_ACCOUNT_TTL": _GRID_ACCOUNT_TTL,
        "_GRID_CALLOUTS_CACHE": _GRID_CALLOUTS_CACHE,
        "_GRID_MINUS_PACK_CACHE": _GRID_MINUS_PACK_CACHE,
        "_MINUS_LIB_Q": _MINUS_LIB_Q,
        "_PLATFORMS_RSYA": _PLATFORMS_RSYA,
        "_PLATFORMS_SEARCH_ONLY": _PLATFORMS_SEARCH_ONLY,
        "_dedup_callout_ids": _dedup_callout_ids,
        "gf": gf,
    }


def _create_set_finalize_module():
    from . import create_set_finalize as csfin
    csfin.configure(_create_set_finalize_deps())
    return csfin


_FINALIZE_QUEUE_CONFIGURED = {"done": False}


def _finalize_queue_module():
    """Модуль async-финализации (Задача F). Конфигурируем один раз: REAL finalize-функции
    (без capture-обёртки — иначе replay в воркере снова захватился бы) + rw-conn."""
    from . import create_set_finalize_queue as csfq
    if not _FINALIZE_QUEUE_CONFIGURED["done"]:
        csfin = _create_set_finalize_module()
        csfq.configure({
            "victory_conn_rw": _victory_conn_rw,
            "finalize_rsya": csfin._finalize_rsya,
            "finalize_search_via_grid": csfin._finalize_search_via_grid,
        })
        _FINALIZE_QUEUE_CONFIGURED["done"] = True
    return csfq

def _search_platforms(*args, **kwargs):
    return _create_set_finalize_module()._search_platforms(*args, **kwargs)


def _finalize_rsya(*args, **kwargs):
    # Задача F (DIRECT_ASYNC_FINALIZE): ON+активное окно захвата → финализацию НЕ исполняем
    # инлайн (уходит в очередь finalize_set, replay в фоне). OFF → capture=False мгновенно →
    # байт-в-байт прежнее инлайн-исполнение.
    if _finalize_queue_module().capture_finalize("rsya", args, kwargs):
        return []
    return _create_set_finalize_module()._finalize_rsya(*args, **kwargs)


_MINUS_LIB_Q = ("query MinusPhraseLibrary($input:GdGetMinusKeywordsPacksInput!){reqId:getReqId "
                "getLibraryMinusKeywordsPacks(input:$input){rowset{id name minusKeywords}totalCount}}")


_GRID_MINUS_PACK_CACHE: dict = {}                         # (login,marker) → (pack_id|None, ts) — аккаунт-стабилен
_GRID_CALLOUTS_CACHE: dict = {}                           # login → (by_text:dict, ts) — аккаунт-стабилен
_GRID_ACCOUNT_TTL = 20 * 60                               # как _OFFER_PRICE_TTL: за джобу не меняется


def _grid_minus_pack_id(*args, **kwargs):
    return _create_set_finalize_module()._grid_minus_pack_id(*args, **kwargs)


_CALLOUTS_Q = ("query Callouts($login:String!){reqId:getReqId callouts(input:{searchBy:{login:$login}"
               "filter:{deleted:false}}){clientId id text statusModerate}}")


def _grid_callout_ids(*args, **kwargs):
    return _create_set_finalize_module()._grid_callout_ids(*args, **kwargs)


def _finalize_search_via_grid(*args, **kwargs):
    # Задача F (DIRECT_ASYNC_FINALIZE): см. _finalize_rsya. OFF → инлайн как раньше.
    if _finalize_queue_module().capture_finalize("search", args, kwargs):
        return []
    return _create_set_finalize_module()._finalize_search_via_grid(*args, **kwargs)


def _add_listing_ads_v501(*args, **kwargs):
    return _create_set_tp1_builder_module()._add_listing_ads_v501(*args, **kwargs)


def _create_tp1_single(*args, **kwargs):
    return _create_set_tp1_builder_module()._create_tp1_single(*args, **kwargs)


def _create_tp1_campaign(*args, **kwargs):
    return _create_set_tp1_builder_module()._create_tp1_campaign(*args, **kwargs)


def _grid_account_image_hashes(*args, **kwargs):
    return _create_set_tp1_builder_module()._grid_account_image_hashes(*args, **kwargs)


def _account_image_map(*args, **kwargs):
    """Кэшированная (по логину) account-map {basename: imageHash}. Обёртка над сырым
    `_grid_account_image_hashes`, который читает ВСЕ кампании+объявления аккаунта."""
    return _create_set_tp1_builder_module()._account_image_map(*args, **kwargs)


def _preupload_tp1_images(*args, **kwargs):
    # Обёртка гарантирует, что cstp1.configure() (globals().update(deps)) исполнён ДО вызова
    # _preupload_tp1_images — иначе набор-level прогрев в фон-потоке падал NameError на первом
    # инъектируемом глобале (_SLEPOK_KEY/kp/gf/…), т.к. фон-поток импортил функцию сырьём в обход
    # ленивого configure (инцидент IMG_PREUPLOAD_SLEPOK_KEY_UNDEF, 2026-07-09).
    return _create_set_tp1_builder_module()._preupload_tp1_images(*args, **kwargs)


def _tp1_pack_groups(*args, **kwargs):
    return _create_set_tp1_builder_module()._tp1_pack_groups(*args, **kwargs)


def _pack_groups_with_retry(*args, **kwargs):
    return _create_set_tp1_builder_module()._pack_groups_with_retry(*args, **kwargs)


def _create_tp1_via_cookie(*args, **kwargs):
    return _create_set_tp1_builder_module()._create_tp1_via_cookie(*args, **kwargs)


def _create_set_feed_builder_deps() -> dict:
    return {
        "_SLEPOK_MINUS_MODE": _SLEPOK_MINUS_MODE,
        "_apply_campaign_direct_minus": _apply_campaign_direct_minus,
        "_collect_pack_minus": _collect_pack_minus,   # #9 cookie-путь tp2/tp4: слепковый _minus_shared в spec.minusKeywords
        "_minus_char_budget": _minus_char_budget,     # #9 кап минусов кампании (≤20 000 симв.)
        "_account_model_feeds": _account_model_feeds,
        "_account_offer_prices": _account_offer_prices,
        "_add_job_err": _add_job_err,
        "_add_listing_ads_v501": _add_listing_ads_v501,
        "_enabled_minus_places": _enabled_minus_places,   # ревью 03.07 #7/#8: NameError в tp3 (v5 падал целиком, куки — без финализации)
        "_enabled_minus_words": _enabled_minus_words,    # #4/#5: глобальный минус на tp2/tp4 (DEPS.get в feed_builders:133,315)
        "_ai_common_sitelinks": _ai_common_sitelinks,
        "_allowed_feed_keys": _allowed_feed_keys,
        "_apply_corrections": _apply_corrections,
        "_build_tp1_from_pack": _build_tp1_from_pack,
        "_build_text_from_pack": _build_text_from_pack,   # token-путь tp2/tp4 (DIRECT_API_FIRST) — наполнение групп v5
        "_bump_job": _bump_job,
        "_create_search_test_campaign": _create_search_test_campaign,
        "_ct_segment": _ct_segment,
        "_dedup_callout_ids": _dedup_callout_ids,
        "_delete_partial_campaign": _delete_partial_campaign,
        "_ensure_callout_exts": _ensure_callout_exts,
        "_feed_row_allowed": _feed_row_allowed,
        "_filter_allowed_feed_rows": _filter_allowed_feed_rows,
        "_first_url_feed": _first_url_feed,   # single_feed tp5/tp3: резолв как plan (strict /yandex.xml), не «первый фид»
        "_finalize_rsya": _finalize_rsya,
        "_finalize_search_via_grid": _finalize_search_via_grid,
        "_get_or_reuse_sitelink_set": _get_or_reuse_sitelink_set,
        "_get_or_reuse_sitelink_sets": _get_or_reuse_sitelink_sets,   # батч: N наборов → 1 sitelinks.add
        "_grid_account_image_hashes": _grid_account_image_hashes,
        "_account_image_map": _account_image_map,   # кэш по логину — cookie tp2/tp4 звали сырой читатель на КАЖДОЙ РК
        "_grid_ad_price_payload": _grid_ad_price_payload,
        "_grid_bid_modifiers": _grid_bid_modifiers,
        "_grid_callout_ids": _grid_callout_ids,
        "_grid_feeds": _grid_feeds,
        "_grid_minus_pack_id": _grid_minus_pack_id,
        "_grid_update_adaptive_ads": _grid_update_adaptive_ads,
        "_group_ad_price": _group_ad_price,
        "_is_site_domain_name": _is_site_domain_name,
        "_job_db_progress": _job_db_progress,
        "_norm_sitelinks_for_v501": _norm_sitelinks_for_v501,
        "_pack_groups_with_retry": _pack_groups_with_retry,
        "_resolve_campaign_assets": _resolve_campaign_assets,
        "_search_platforms": _search_platforms,
        "_slepok_sitelinks_for": _slepok_sitelinks_for,
        "_text_group_name": _text_group_name,
        "_v5_get": _v5_get,
        "_victory_conn": _victory_conn,
        "cmc": cmc,
        "gc": gc,
        "gf": gf,
    }


def _create_set_feed_builder_module():
    from . import create_set_feed_builders as csfb
    csfb.configure(_create_set_feed_builder_deps())
    return csfb

def _create_text_via_cookie(*args, **kwargs):
    return _create_set_feed_builder_module()._create_text_via_cookie(*args, **kwargs)


def _create_text_via_token(*args, **kwargs):
    return _create_set_feed_builder_module()._create_text_via_token(*args, **kwargs)


def _create_shopping_via_cookie(*args, **kwargs):
    return _create_set_feed_builder_module()._create_shopping_via_cookie(*args, **kwargs)


def _tp5_account_data(*args, **kwargs):
    return _create_set_feed_builder_module()._tp5_account_data(*args, **kwargs)


def _create_tp5_single(*args, **kwargs):
    return _create_set_feed_builder_module()._create_tp5_single(*args, **kwargs)


def _create_tp5_campaign(*args, **kwargs):
    return _create_set_feed_builder_module()._create_tp5_campaign(*args, **kwargs)


def _create_tp3_single(*args, **kwargs):
    return _create_set_feed_builder_module()._create_tp3_single(*args, **kwargs)


def _create_tp3_campaign(*args, **kwargs):
    return _create_set_feed_builder_module()._create_tp3_campaign(*args, **kwargs)


def _create_set_corrections_deps() -> dict:
    return {
        "_v5_call": _v5_call,
        "_v5_err": _v5_err,
        "_v5_get": _v5_get,
        "_victory_conn": _victory_conn,
    }


def _create_set_corrections_module():
    from . import create_set_corrections as cscorr
    cscorr.configure(_create_set_corrections_deps())
    return cscorr

def _load_corrections(*args, **kwargs):
    return _create_set_corrections_module()._load_corrections(*args, **kwargs)


def _account_retargeting(*args, **kwargs):
    return _create_set_corrections_module()._account_retargeting(*args, **kwargs)


def _seg_key(*args, **kwargs):
    return _create_set_corrections_module()._seg_key(*args, **kwargs)


def _corrections_by_segment(*args, **kwargs):
    return _create_set_corrections_module()._corrections_by_segment(*args, **kwargs)


def _correction_bidmodifiers(*args, **kwargs):
    return _create_set_corrections_module()._correction_bidmodifiers(*args, **kwargs)


def _grid_bid_modifiers(*args, **kwargs):
    return _create_set_corrections_module()._grid_bid_modifiers(*args, **kwargs)


def _apply_corrections(*args, **kwargs):
    return _create_set_corrections_module()._apply_corrections(*args, **kwargs)


# error 152 Direct API = «Превышено суточное ограничение количества баллов» (units кончились на сутки).
# Признаём по коду 152 в строке v501-ошибки ИЛИ по словам про units/баллы. После этого ВСЕ дальнейшие
# вызовы в наборе всё равно упадут — нет смысла долбить API, нужно остановиться и сказать «повтори завтра».
def _is_units_exhausted(msg) -> bool:
    """True, если текст ошибки = исчерпание суточного лимита баллов Директа (error 152)."""
    from .create_set_units import is_units_exhausted
    return is_units_exhausted(msg)


def _units_in_result(r) -> bool:
    """152 в результате пункта: и в top-level error, и в вложенных campaigns (tp1 кладёт сводку,
    tp3/tp5 — плоско; проверяем оба, чтобы не пропустить лимит ни в одном движке)."""
    from .create_set_units import units_in_result
    return units_in_result(r)


def _auth_error_in_result(r) -> bool:
    """53 (auth error) в результате пункта — переключаем на cookie-путь как при 152."""
    from .create_set_units import auth_error_in_result
    return auth_error_in_result(r)


def _master_product_deps() -> dict:
    names = [
        "_BU_RE", "_GENERIC_AT_TITLES", "_GENERIC_SITELINK_FILLERS", "_GENERIC_TEXT_FILLERS",
        "_GENERIC_TITLE_FILLERS", "_SLEPOK_KEY", "_TP67_MIN_TEXT_LEN", "_TP67_OPTIMAL_CATEGORIES",
        "_TP67_RELEVANCE_CATEGORIES", "_account_model_feeds", "_account_offer_urls", "_add_job_err", "_audience_objects",
        "_bad_ad_sitelink", "_bad_ad_text", "_bad_ad_title", "_brand_ct_from_coder", "_brand_title_set",
        "_build_name", "_bump_item", "_bump_job", "_cached_campaign_content", "_catalog_feed",
        "_coherent_discounts", "_coherent_payments", "_creative_images_for_ct", "_dedup_prefix_absorb",
        "_discount_pcts", "_diverse_text_offers", "_drop_new_car", "_drop_used_car", "_enabled_minus_words",
        "_fallback_master_titles", "_first_url_feed",
        "_fill_title", "_fill_variants", "_has_number", "_image_ct_for_content", "_is_bad_start",
        "_is_bu_site", "_is_common_ct", "_is_site_domain_name", "_job_db_progress", "_lines",
        "_feed_url_for_model", "_match_collection", "_num", "_own_brand_tokens", "_replace_emdash", "_replace_foreign_city",
        "_replace_sep_hyphen", "_resolve_region", "_rsya_texts", "_rsya_titles", "_sanitize_content",
        "_sitelink_has_pct", "_slepok_audiences_for", "_slepok_campaign_content", "_strip_credit_rate",
        "_title2_blocklist", "_tp67_keywords_for", "_tp67_targeting_mode", "_tp7_product_feed_filters",
        "_tp7_listing_plus_filter", "_tp7_listings_minus_filters",
        "_trim_to_word", "_variant_norm_key",
        "_slepok_is_auto",  # не-авто признак (B2B dmp и будущие): переключает базу заголовков/текстов
    ]
    g = globals()
    return {name: g[name] for name in names}


def _run_master_product_item(*, it, name, href, region_ids, counter_id, goal_id,
                             cpa, launch, client, agent, eff_site, ctx,
                             tpl_titles, tpl_texts, tpl_sitelinks, rs, login,
                             _st_token, _w_agency, _stream_agent, _job, _tp7_mf):
    """tp6/tp7 item handler adapter; implementation lives in create_set_master_product."""
    from .create_set_master_product import run_master_product_item
    return run_master_product_item(
        _master_product_deps(),
        it=it, name=name, href=href, region_ids=region_ids, counter_id=counter_id, goal_id=goal_id,
        cpa=cpa, launch=launch, client=client, agent=agent, eff_site=eff_site, ctx=ctx,
        tpl_titles=tpl_titles, tpl_texts=tpl_texts, tpl_sitelinks=tpl_sitelinks, rs=rs, login=login,
        _st_token=_st_token, _w_agency=_w_agency, _stream_agent=_stream_agent, _job=_job, _tp7_mf=_tp7_mf,
    )


def _create_set_orchestrator_deps() -> dict:
    names = [
        "_CALLOUT_PER_CAMPAIGN_CAP", "_CONTENT_CACHE", "_CONTENT_CACHE_LOCK", "_CREATE_JOBS",
        "_RESUME_MAX", "_SLEPOK_MINUS_MODE", "_TOKEN_ONLY_TYPES", "_account_ctx", "_account_model_feeds",
        "_account_retargeting", "_add_job_err", "_apply_campaign_direct_minus", "_apply_corrections",
        "_attach_minus_set_to_text_campaign", "_attach_post_repair_verification", "_bump_item", "_bump_job",
        "_busy_response", "_cached_campaign_content", "_callout_semantic_key", "_content_cache_key",
        "_account_content_get", "_account_content_put",   # #16: account-level content reuse (в пределах прохода)
        "_content_copy", "_counter_foreign_owner", "_create_account_promo_from_slepok", "_ct_segment",
        "_create_set_live_verification", "_create_shopping_via_cookie", "_create_text_via_cookie",
        "_create_text_via_token",   # DIRECT_API_FIRST: tp2/tp4 через баллы (token), фолбэк на cookie
        "_create_tp1_campaign", "_create_tp1_via_cookie", "_create_tp3_campaign", "_create_tp5_campaign",
        "_dedup_callouts", "_deferred_save", "_deferred_set_status", "_first_url_feed",
        "_ensure_named_minus_sets",   # именованные наборы минус-фраз слепка → библиотека аккаунта
        "_account_offer_urls", "_feed_url_for_model",
        "_account_offer_prices", "_brand_ct_from_coder", "_creative_images_for_ct",
        "_get_or_create_minus_set", "_goal_vse_formy", "_grid_list_campaigns", "_ints", "_job_db_progress",
        "_job_new", "_m3_gate_wait", "_slepok_profile_excludes_tp",
        "_set_llm_heartbeat_job", "_current_llm_heartbeat_job",
        "_lines", "_load_corrections", "_metrika_goals_for", "_next_units_reset_utc", "_normalize_callout_text",
        "_num", "_preflight_creds", "_promo_content_lines", "_promo_usable_for_content",
        "_preupload_tp1_images",   # набор-level прогрев картинок tp1 через configured-модуль (DI-инъекции)
        "_pull_begin", "_pull_end", "_repair_deps", "_resolve_region", "_rotated_content_window",
        "_rule_sets", "_run_master_product_item", "_selected_slepok_key", "_slepok_content_get",
        "_slepok_uses_shopping", "_templates_for",
        "_auth_error_in_result", "_units_in_result", "_v5_get", "_v5_call",
        "_units_alive_for_login",   # баллы живы → сегментный tp5 добиваем токеном сразу (не ждём полночь)
        # DI для create_set_tp8_10 (Посевы): Manual-пул + ct→img_ct
        "_manual_creative_paths", "_image_ct_for_content", "_enabled_post_minus_places",
    ]
    g = globals()
    return {name: g[name] for name in names}


def _create_set_response():
    """Create-set endpoint adapter; orchestration lives in create_set_orchestrator."""
    from .create_set_orchestrator import create_set_response
    return create_set_response(_create_set_orchestrator_deps())


def _create_set_repairing_deps() -> dict:
    return {
        "_CALLOUT_PER_CAMPAIGN_CAP": _CALLOUT_PER_CAMPAIGN_CAP,
        "_CREATE_JOBS": _CREATE_JOBS,
        "_CREATE_JOBS_LOCK": _CREATE_JOBS_LOCK,
        "_SLEPOK_KEY": _SLEPOK_KEY,
        "_account_ctx": _account_ctx,
        "_account_offer_prices": _account_offer_prices,
        "_ag_part1_map": _ag_part1_map,
        "_create_account_promo_from_slepok": _create_account_promo_from_slepok,
        "_create_jobs_ahead": _create_jobs_ahead,
        "_ct_segment": _ct_segment,
        "_dedup_callouts": _dedup_callouts,
        "_direct_tokens": _direct_tokens,
        "_filter_allowed_feed_rows": _filter_allowed_feed_rows,
        "_filter_group_keywords": _filter_group_keywords,
        "_grid_feeds": _grid_feeds,
        "_grid_list_campaigns": _grid_list_campaigns,
        "_group_ad_price": _group_ad_price,
        "_ints": _ints,
        "_is_tool_campaign": _is_tool_campaign,
        "_job_db_get": _job_db_get,
        "_job_new": _job_new,
        "_lines": _lines,
        "_listing_name_value": _listing_name_value,
        "_model_field_values": _model_field_values,
        "_num": _num,
        "_pack_groups_with_retry": _pack_groups_with_retry,
        "_promo_content_lines": _promo_content_lines,
        "_resolve_region": _resolve_region,
        "_templates_for": _templates_for,
        "_text_group_name": _text_group_name,
        "_token_for_login": _token_for_login,
        "_units_alive_for_login": _units_alive_for_login,   # recreate: баллы живы → токеном, не по куке
        "_valid_pack_brand_name": _valid_pack_brand_name,
        "_vendor_value": _vendor_value,
        "gc": gc,
        "kp": kp,
        "rauto": rauto,
        "rex": rex,
        "rgate": rgate,
        "vsvc": vsvc,
        # Repair image/price/text callbacks (добавлены 2026-07-03 для in-place repair executors)
        "_cached_upload_image": _cached_upload_image,
        "_parallel_upload_images": _parallel_upload_images,
        "_creative_images_for_ct": _creative_images_for_ct,
        "_grid_update_adaptive_ads": _grid_update_adaptive_ads,
        "_grid_set_ad_prices": _grid_set_ad_prices,
    }


def _create_set_repairing_module():
    from . import create_set_repairing as csr
    csr.configure(_create_set_repairing_deps())
    return csr

def _create_set_live_verification(*args, **kwargs):
    return _create_set_repairing_module()._create_set_live_verification(*args, **kwargs)


def _create_set_job_context(*args, **kwargs):
    return _create_set_repairing_module()._create_set_job_context(*args, **kwargs)


def _repair_text_content_context(*args, **kwargs):
    return _create_set_repairing_module()._repair_text_content_context(*args, **kwargs)


def _repair_shopping_content_context(*args, **kwargs):
    return _create_set_repairing_module()._repair_shopping_content_context(*args, **kwargs)


def _repair_keywords_group_context(*args, **kwargs):
    return _create_set_repairing_module()._repair_keywords_group_context(*args, **kwargs)


def _attach_post_repair_verification(*args, **kwargs):
    return _create_set_repairing_module()._attach_post_repair_verification(*args, **kwargs)


def _repair_deps(*args, **kwargs):
    return _create_set_repairing_module()._repair_deps(*args, **kwargs)


# Все 28 DI определены → инъектим в copy_engine (Direct API/токены/Grid/очередь; _CREATE_JOBS —
# ТОТ ЖЕ объект для mirror прогресса копирования в create-карточку).
_ce.configure({
    "_v5_call": _v5_call, "_v501_svc": _v501_svc, "_v5_err": _v5_err,
    "_token_for_login": _token_for_login, "_direct_tokens": _direct_tokens,
    "_resolve_agency_hint": _resolve_agency_hint, "_victory_conn_rw": _victory_conn_rw,
    "_resolve_region": _resolve_region,   # город → (r_code, oblast) для ремапа кодера при копировании
    "_grid_list_campaigns": _grid_list_campaigns, "_grid_feeds": _grid_feeds,
    "_grid_feed_offer_prices": _grid_feed_offer_prices, "_group_ad_price": _group_ad_price,
    "_grid_set_ad_prices": _grid_set_ad_prices, "_grid_update_adaptive_ads": _grid_update_adaptive_ads,
    "_account_offer_prices": _account_offer_prices, "_account_ctx": _account_ctx,
    "_geo_id": _geo_id, "_geo_name_by_id": _geo_name_by_id, "_geo_type_by_id": _geo_type_by_id,
    "_enabled_minus_places": _enabled_minus_places,
    "_enabled_global_minus_places": _enabled_global_minus_places,   # copy → глобальная таблица (не per-слепок)
    "_enabled_baseline_minus_places": _enabled_baseline_minus_places,   # legacy DI; copy disabledPlaces теперь 1в1
    "_filter_allowed_feed_rows": _filter_allowed_feed_rows, "_feed_key": _feed_key,
    "_create_set_live_verification": _create_set_live_verification,
    "_attach_post_repair_verification": _attach_post_repair_verification, "_repair_deps": _repair_deps,
    "_CREATE_JOBS": _CREATE_JOBS, "_CREATE_JOBS_LOCK": _CREATE_JOBS_LOCK, "_JOB_TERMINAL": _JOB_TERMINAL,
    "_job_touch": _job_touch, "_job_db_save": _job_db_save,
    "_CALLOUT_PER_CAMPAIGN_CAP": _CALLOUT_PER_CAMPAIGN_CAP,
})

# copy_verify: движок сверки source↔target (нужны только v5-читатели цели).
_cv.configure({
    "_v5_call": _v5_call, "_token_for_login": _token_for_login, "_direct_tokens": _direct_tokens,
})


def _delete_uac_repair_campaigns(*args, **kwargs):
    return _create_set_repairing_module()._delete_uac_repair_campaigns(*args, **kwargs)


def _delete_search_draft_campaigns(*args, **kwargs):
    return _create_set_repairing_module()._delete_search_draft_campaigns(*args, **kwargs)


def _queue_recreate_repair_job(*args, **kwargs):
    return _create_set_repairing_module()._queue_recreate_repair_job(*args, **kwargs)


def _auto_queue_recreate_after_done(*args, **kwargs):
    return _create_set_repairing_module()._auto_queue_recreate_after_done(*args, **kwargs)


def _spec_audit_deps() -> dict:
    """IO-хелперы для campaign_spec_audit (spec-аудит live-кампаний vs декларативная спека)."""
    return {
        "_account_ctx": _account_ctx,
        "_ct_segment": _ct_segment,
        "_ag_part1_map": _ag_part1_map,
        "_valid_pack_brand_name": _valid_pack_brand_name,
        "_filter_group_keywords": _filter_group_keywords,
        "_account_offer_prices": _account_offer_prices,
        "_direct_tokens": _direct_tokens,
        "_token_for_login": _token_for_login,
        "_enabled_minus_marks": _enabled_minus_marks,
        "_enabled_minus_words": _enabled_minus_words,   # D6: глоб.минус-слова на кампанию (GLOBAL_MINUS_CAMPAIGN_MISSING)
        "_grid_list_campaigns": _grid_list_campaigns,
        "_is_tool_campaign": _is_tool_campaign,
        "_struct_cts": _struct_cts,
        "_struct_has_tp": _struct_has_tp,
        "_SLEPOK_KEY": _SLEPOK_KEY,
        "_repair_deps": _repair_deps,
        "_tp1_video_ads": _tp1_video_ads,   # deferred-video: добивка видео после создания
        "kp": kp,
        # fix_generic_fallback_group: удаление DRAFT-пустышки + deferred токеном
        "_deferred_save": _deferred_save,
        "_next_units_reset_utc": _next_units_reset_utc,
        "_units_alive_for_login": _units_alive_for_login,   # баллы живы → добивать сразу (не ждать полночь)
        "_victory_conn_rw": _victory_conn_rw,
    }


def _configure_spec_audit():
    """Configure and return the campaign_spec_audit module (deps injection)."""
    from . import campaign_spec_audit as csa
    csa.configure(_spec_audit_deps())
    return csa


def _run_spec_audit_and_fix(login: str, ctx: dict, *, skip_recreate: bool = False) -> dict:
    """Run the declarative spec-audit for one account and auto-fix KEYWORDS_WRONG_GROUP in-place.

    Called from the delayed-repair cycle after the standard in-place actions. Returns a compact
    report {issues_by_code, fixed} — fixes go through repair_executor (no Direct create units).

    skip_recreate=True: пропускает fix_generic_fallback_group (не запускает новое recreate).
    Используется для ре-аудитов после recreate (kind='content_repair_post_recreate') — антицикл.
    """
    csa = _configure_spec_audit()
    body = (ctx or {}).get("body") or {}
    # ``results`` ОБЯЗАТЕЛЕН: campaign_spec_audit.audit_account_jobs строит из него builds_by_cid
    # для отложенной сверки «build ⇄ кабинет» (_audit_build_vs_live). Без ключа сверка получала
    # пустой словарь и МОЛЧАЛА во всём отложенном проходе (ревью этапа 1, находка A1).
    job_result = {"body": body, "agency": ctx.get("agency") or "",
                  "results": ctx.get("results") or []}
    report = csa.audit_account_jobs(login, job_result)
    out = {
        "campaigns": report.get("campaigns"),
        "per_tp": report.get("per_tp"),
        "issues_by_code": report.get("counts") or {},
    }
    wrong = [it for it in (report.get("issues") or []) if it.get("code") == "KEYWORDS_WRONG_GROUP"]
    if wrong:
        fix = csa.fix_keywords_wrong_group(login, ctx, wrong)
        out["keywords_wrong_group_fix"] = {
            "ok": fix.get("ok"), "fixed_adgroups": fix.get("fixed_adgroups"),
            "deleted_keywords": fix.get("deleted_keywords"), "error": fix.get("error"),
        }
    short = [it for it in (report.get("issues") or []) if it.get("code") == "SHORT_TITLES"]
    if short:
        fix_st = csa.fix_short_titles(login, ctx, short)
        out["short_titles_fix"] = {
            "ok": fix_st.get("ok"), "campaigns_fixed": fix_st.get("campaigns_fixed"),
            "titles_extended": fix_st.get("titles_extended"),
            "terminal": (fix_st.get("terminal") or [])[:5],   # SHORT_TITLES_UNFIXABLE (hard-fail)
            "errors": (fix_st.get("errors") or [])[:5],
        }
    texts_low = [it for it in (report.get("issues") or []) if it.get("code") == "CONTENT_TEXTS_LOW"]
    if texts_low:
        fix_tl = csa.fix_texts_low(login, ctx, texts_low)
        out["content_texts_low_fix"] = {
            "ok": fix_tl.get("ok"), "campaigns_fixed": fix_tl.get("campaigns_fixed"),
            "texts_added": fix_tl.get("texts_added"),
            "errors": (fix_tl.get("errors") or [])[:5],
        }
    brand_first = [it for it in (report.get("issues") or []) if it.get("code") == "BRAND_NOT_FIRST"]
    if brand_first:
        fix_bf = csa.fix_brand_not_first(login, ctx, brand_first)
        out["brand_not_first_fix"] = {
            "ok": fix_bf.get("ok"), "campaigns_fixed": fix_bf.get("campaigns_fixed"),
            "terminal": (fix_bf.get("terminal") or [])[:5],   # BRAND_NOT_FIRST_UNFIXABLE (hard-fail)
            "errors": (fix_bf.get("errors") or [])[:5],
        }
    btn = [it for it in (report.get("issues") or []) if it.get("code") == "BUTTON_MISSING"]
    if btn:
        fix_btn = csa.fix_button_missing(login, ctx, btn)
        out["button_missing_fix"] = {
            "ok": fix_btn.get("ok"), "campaigns_fixed": fix_btn.get("campaigns_fixed"),
            "errors": (fix_btn.get("errors") or [])[:5],
        }
    ff = [it for it in (report.get("issues") or []) if it.get("code") == "FEED_FILTER_MISSING_UAC"]
    if ff:
        fix_ff = csa.fix_feed_filters_uac(login, ctx, ff)
        out["feed_filters_uac_fix"] = {
            "ok": fix_ff.get("ok"), "campaigns_fixed": fix_ff.get("campaigns_fixed"),
            "errors": (fix_ff.get("errors") or [])[:5],
        }
    vm = [it for it in (report.get("issues") or []) if it.get("code") == "VIDEO_MISSING"]
    if vm:
        fix_vm = csa.fix_video_missing(login, ctx, vm)
        out["video_missing_fix"] = {
            "ok": fix_vm.get("ok"), "campaigns_fixed": fix_vm.get("campaigns_fixed"),
            "campaigns": fix_vm.get("campaigns"),
            # still_missing_total/requeue_needed прокидываем наверх — delayed-repair подмешивает
            # их в remaining, чтобы «до нуля»-reschedule перезапустил докрутку видео (Семён 2026-07-08).
            "still_missing_total": fix_vm.get("still_missing_total"),
            "requeue_needed": fix_vm.get("requeue_needed"),
            "errors": (fix_vm.get("errors") or [])[:5],
        }
    nl = [it for it in (report.get("issues") or []) if it.get("code") == "NO_LISTING"]
    if nl:
        fix_nl = csa.fix_no_listing(login, ctx, nl)
        out["no_listing_fix"] = {
            "ok": fix_nl.get("ok"), "campaigns_fixed": fix_nl.get("campaigns_fixed"),
            "campaigns": fix_nl.get("campaigns"),
            "errors": (fix_nl.get("errors") or [])[:5],
        }
    im = [it for it in (report.get("issues") or []) if it.get("code") == "IMAGE_MISSING"]
    if im:
        fix_im = csa.fix_image_missing(login, ctx, im)
        out["image_missing_fix"] = {
            "ok": fix_im.get("ok"), "campaigns_fixed": fix_im.get("campaigns_fixed"),
            "errors": (fix_im.get("errors") or [])[:5],
        }
    ffg = [it for it in (report.get("issues") or []) if it.get("code") == "FEED_FILTER_MISSING_GRID"]
    if ffg:
        fix_ffg = csa.fix_feed_filters_grid(login, ctx, ffg)
        out["feed_filters_grid_fix"] = {
            "ok": fix_ffg.get("ok"), "campaigns_fixed": fix_ffg.get("campaigns_fixed"),
            "campaigns": fix_ffg.get("campaigns"),
            "errors": (fix_ffg.get("errors") or [])[:5],
        }
    pw = [it for it in (report.get("issues") or []) if it.get("code") == "PLACEMENTS_WRONG"]
    if pw:
        fix_pw = csa.fix_placements_wrong(login, ctx, pw)
        out["placements_wrong_fix"] = {
            "ok": fix_pw.get("ok"), "campaigns_fixed": fix_pw.get("campaigns_fixed"),
            "errors": (fix_pw.get("errors") or [])[:5],
        }
    slm = [it for it in (report.get("issues") or []) if it.get("code") == "SITELINK_MISSING"]
    if slm:
        fix_slm = csa.fix_sitelinks_missing(login, ctx, slm)
        out["sitelinks_missing_fix"] = {
            "ok": fix_slm.get("ok"), "campaigns_fixed": fix_slm.get("campaigns_fixed"),
            "set_id": fix_slm.get("set_id"), "errors": (fix_slm.get("errors") or [])[:5],
        }
    gfb = [it for it in (report.get("issues") or []) if it.get("code") == "GENERIC_FALLBACK_GROUP"]
    if gfb and not skip_recreate:
        fix_gfb = csa.fix_generic_fallback_group(login, ctx, gfb)
        out["generic_fallback_group_fix"] = {
            "ok": fix_gfb.get("ok"), "campaigns_fixed": fix_gfb.get("campaigns_fixed"),
            "deferred_ids": fix_gfb.get("deferred_ids"),
            "skipped": (fix_gfb.get("skipped") or [])[:5],
            "errors": (fix_gfb.get("errors") or [])[:5],
        }
    elif gfb and skip_recreate:
        out["generic_fallback_group_fix"] = {
            "ok": True, "campaigns_fixed": 0,
            "note": "пропущено (skip_recreate=True, ре-аудит после recreate)",
        }
    fmk = [it for it in (report.get("issues") or []) if it.get("code") == "FOREIGN_MODEL_KEYWORDS"]
    if fmk:
        fix_fmk = csa.fix_foreign_model_keywords(login, ctx, fmk)
        out["foreign_model_keywords_fix"] = {
            "ok": fix_fmk.get("ok"), "deleted": fix_fmk.get("deleted"),
            "adgroups_fixed": fix_fmk.get("adgroups_fixed"),
            "note": fix_fmk.get("note"), "error": fix_fmk.get("error"),
        }
    gmc = [it for it in (report.get("issues") or []) if it.get("code") == "GLOBAL_MINUS_CAMPAIGN_MISSING"]
    if gmc:
        fix_gmc = csa.fix_global_minus_campaign(login, ctx, gmc)
        out["global_minus_campaign_fix"] = {
            "ok": fix_gmc.get("ok"), "campaigns_fixed": fix_gmc.get("campaigns_fixed"),
            "errors": (fix_gmc.get("errors") or [])[:5],
        }
    lpf = [it for it in (report.get("issues") or []) if it.get("code") == "LISTING_POSITIVE_FILTER_MISSING"]
    if lpf:
        fix_lpf = csa.fix_listing_positive_filter(login, ctx, lpf)
        out["listing_positive_filter_fix"] = {
            "ok": fix_lpf.get("ok"), "campaigns_fixed": fix_lpf.get("campaigns_fixed"),
            "campaigns": fix_lpf.get("campaigns"),
            "errors": (fix_lpf.get("errors") or [])[:5],
        }
    return out


def _lines(val) -> list[str]:
    """textarea → список непустых строк (или уже список)."""
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    return [ln.strip() for ln in (val or "").splitlines() if ln.strip()]


def _ints(val) -> list[int]:
    if isinstance(val, list):
        return [int(x) for x in val if str(x).strip()]
    return [int(x) for x in (val or "").replace(",", " ").split() if x.strip().isdigit()]


def _legacy_create_response():
    d = request.json or {}
    try:
        spec = cmc.MasterCampaignSpec(
            href=(d.get("href") or "").strip(),
            titles=_lines(d.get("titles")),
            texts=_lines(d.get("texts")),
            region_ids=_ints(d.get("region_ids")) or [225],
            counter_id=int(d["counter_id"]),
            goal_id=int(d["goal_id"]),
            cpa=int(d["cpa"]),
            week_budget=int(d["week_budget"]),
            display_name=(d.get("display_name") or "").strip() or None,
            campaign_type=d.get("campaign_type") or "master",
            feed_id=int(d["feed_id"]) if d.get("feed_id") else None,
            minus_keywords=_lines(d.get("minus_keywords")) or _enabled_minus_words(),
            audiences=d.get("audiences") or [],
            image_urls=_lines(d.get("image_urls")),
            video_urls=_lines(d.get("video_urls")),
        )
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({"ok": False, "error": f"Проверьте поля: {e}"}), 400

    ulogin = (d.get("ulogin") or "").strip()
    if not ulogin:
        return jsonify({"ok": False, "error": "ulogin обязателен"}), 400
    launch = False   # ⛔ ПРАВИЛО: только черновики, авто-публикация запрещена (см. api_create_set)

    try:
        client = cmc.build_client(ulogin, account=(d.get("account") or None))
        cid = client.create_master_campaign(spec, launch=launch)
    except cmc.UacApiError as e:
        return jsonify({"ok": False, "error": f"Direct API [{e.step}] {e.status}: {e.body[:300]}"}), 502
    except Exception as e:  # noqa: BLE001 — показать пользователю причину
        return jsonify({"ok": False, "error": str(e)[:400]}), 500

    url = f"https://direct.yandex.ru/wizard/campaigns/{cid}/?ulogin={ulogin}"
    return jsonify({"ok": True, "id": cid, "launched": launch, "url": url})




# ── Локальная ИИ на M3 (mlx_lm.server, OpenAI-совместимый API) ──────────────────
# URL берём из окружения, чтобы менять схему подключения (прямой Tailscale-IP M3
# либо локальный SSH-туннель на LXC101) без правки кода. По умолчанию — туннель.
# С 03.07 (решение Семёна): на M3 ОДНА модель — 72B на 8086 (speculative decoding с драфтом
# 1.5B). Связка 4×14B выключена и удалена: RAM-конкуренция душила 72B и роняла mlx
# (RemoteDisconnected). Все три константы указывают на один сервер; имена сохранены
# (десятки использований), env-переопределение работает как раньше.


# ── ИИ-агенты «слепки директологов»: генерация/публикация промоакций ────────────
# Агент = стиль реального директолога. ИИ на M3 генерит промо в его стиле → превью →
# публикация в кабинет клиента через grid/api (promo.PromoClient). Публикация — только
# по явному подтверждению пользователя (создаёт реальную промо у клиента).

# Фингерпринты служебных ошибок, которые draft-модель/спекулятивный декодер на M3 иногда
# ВКЛЕИВАЕТ прямо в сгенерированный текст при обрыве соединения с под-сервисом (баг S559/S560:
# «Connection aborted / RemoteDisconnected» уезжает в content). Такой ответ — мусор: режем по
# первому фингерпринту, а если осмысленного префикса не осталось — считаем генерацию неудачной.


_LLM_HEARTBEAT_CONTEXT = threading.local()
_LLM_HEARTBEAT_DB_LAST: dict[str, float] = {}
_LLM_HEARTBEAT_DB_INTERVAL = 30.0


def _set_llm_heartbeat_job(job_id: str | None) -> None:
    """Ограничить LLM-heartbeat текущей worker job в этом thread.

    Без контекста heartbeat не должен трогать все running jobs: при параллельных агентствах
    активная LLM-генерация одного аккаунта иначе маскирует зависание другого аккаунта.
    """
    if job_id:
        _LLM_HEARTBEAT_CONTEXT.job_id = str(job_id)
        return
    try:
        delattr(_LLM_HEARTBEAT_CONTEXT, "job_id")
    except AttributeError:
        pass


def _current_llm_heartbeat_job() -> str | None:
    """Текущий scoped heartbeat job-id; нужен для переноса контекста в LLM thread-pool."""
    return getattr(_LLM_HEARTBEAT_CONTEXT, "job_id", None)


def _touch_running_jobs_heartbeat() -> None:
    """LLM-запрос = прогресс: бампаем _heartbeat только текущей worker job.

    Root cause watchdog-киллов 2026-07-02 (jobs 9126bf12fb3a/ac6d98864aa4/c8c444a166d4):
    _M3_LLM_TIMEOUT=480с × несколько запросов на item — при перегруженном M3 (sshfs-выкачка
    видео душила Мак) генерация ПЕРВОГО item шла >15 мин, item-heartbeat не тикал → watchdog
    убивал ЖИВУЮ джобу. Теперь каждый M3-вызов (включая ретраи) отмечает активность своей job,
    не продлевая чужие зависшие jobs."""
    _jid = getattr(_LLM_HEARTBEAT_CONTEXT, "job_id", None)
    if not _jid:
        return
    flush_job = None
    if not _JOB_MUT_LOCK.acquire(blocking=False):
        return
    try:
        now = time.time()
        # Best-effort: не берём _CREATE_JOBS_LOCK из горячего SSE-пути. Этот lock обслуживает
        # queue condition/claim, и ожидание его из LLM-потоков может повесить as_completed().
        _j = _CREATE_JOBS.get(str(_jid))
        if _j is not None and _j.get("status") == "running":
            _j["_heartbeat"] = now
            mono = time.monotonic()
            if mono - _LLM_HEARTBEAT_DB_LAST.get(str(_jid), 0.0) >= _LLM_HEARTBEAT_DB_INTERVAL:
                _LLM_HEARTBEAT_DB_LAST[str(_jid)] = mono
                flush_job = _j
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            _JOB_MUT_LOCK.release()
        except Exception:  # noqa: BLE001
            pass
    if flush_job is not None:
        try:
            _job_db_progress(flush_job)
        except Exception:  # noqa: BLE001
            pass


# heartbeat очереди определён — инъектим его в llm_providers (каждый M3/OpenRouter-вызов
# бампает _heartbeat текущей job; иначе долгая генерация выглядела бы «зависанием» для watchdog).
_llmp.configure({"_touch_running_jobs_heartbeat": _touch_running_jobs_heartbeat,
                 "_set_llm_heartbeat_job": _set_llm_heartbeat_job,
                 "_current_llm_heartbeat_job": _current_llm_heartbeat_job})

# DI для text_gen (генерация текстов): функции blueprint + константы-пула.
# `_is_bu_site` нужен `_title_from_template`: шаблон «Новые {brand} в {city}» уходит в Title
# МИМО `_cf`, а `_NEW_RE` его и не поймал бы («новые BAIC» — марка, а не слово «авто»).
_tg.configure({
    "_drop_used_car": _drop_used_car, "_drop_new_car": _drop_new_car,
    "_is_bu_site": _is_bu_site,
    "_brand_canon": _brand_canon, "_ct_segment": _ct_segment,
    "_GENERIC_TITLE_FILLERS": _GENERIC_TITLE_FILLERS, "_GENERIC_AT_TITLES": _GENERIC_AT_TITLES,
    "_RA_TITLES_CAP": _RA_TITLES_CAP, "_RA_TEXTS_CAP": _RA_TEXTS_CAP,
    "_NON_AUTO_SITE_TYPES": _non_auto_site_types(),   # B2B site_type → без авто-фильтра ключей
})




# ── Бренд кампании из КОДЕРА: первый ct с 4 цифрами (ct####) → имя марки/модели (ag_part1) ──






# ── Model page URL: глубокая ссылка на страницу модели ────────────────────────
# Мэппинг тип сайта → шаблон URL (проверено HEAD-запросами к vitmp.ru 2026-06-22).






def _content_copy(content: dict | None) -> dict:
    if not isinstance(content, dict):
        return {}
    try:
        return json.loads(json.dumps(content, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        return dict(content)








def _ai_common_sitelinks(login: str, slepok: str, site_type: str, city: str,
                         tp_code: str) -> list[dict]:
    if os.environ.get("DIRECT_CREATE_AI_COMMON_SITELINKS", "0").lower() not in ("1", "true", "yes", "on"):
        return []
    item = {
        "brand": "",
        "gc": "ct0000",
        "ct": "ct0000",
        "tp": tp_code,
        "campaign_type": tp_code,
        "type": tp_code,
        "name": "ct0000",
        # Провайдер обязателен: без него _gen_campaign_content уходит на M3-дефолт (перегружен →
        # ЗАВИСАЕТ >170с → финализ tp5 не доходит до сайтлинков, #7). OpenRouter (deepseek-chat) ~50с
        # и кэшируется по ct0000. M3 остаётся фолбэком внутри _llm_pair_for.
        "llm_provider": "openrouter",
    }
    content = _ai_campaign_content_for_item(login, slepok, site_type, city, item)
    if not isinstance(content, dict):
        return []
    return [s for s in (content.get("sitelinks") or []) if isinstance(s, dict) and s.get("title")][:8]


def _cached_campaign_content(login: str, agent_obj: dict, agent_key: str, item: dict,
                             site_type: str, city: str, avoid: list | None = None,
                             fast_mode: bool = False) -> dict | None:
    """Получить/сгенерировать контент для st/ct. В кэш кладём только полный валидный 5/3/8."""
    if not agent_obj:
        return None
    key = _content_cache_key(agent_key, site_type, city, item)
    if not fast_mode:
        with _CONTENT_CACHE_LOCK:
            cached = _CONTENT_CACHE.get(key)
        if cached:
            return _content_copy(cached)

    res = _gen_campaign_content(
        login, agent_obj, (agent_key or "").strip().lower(), item,
        avoid=avoid or [], fast_mode=fast_mode,
    )
    if not isinstance(res, dict) or not res.get("ok"):
        return None
    content = _content_copy(res.get("content") or {})
    if _content_complete(content) and not fast_mode:
        with _CONTENT_CACHE_LOCK:
            _CONTENT_CACHE[key] = _content_copy(content)
    return content or None








def _promo_from_slepok(agent: dict, ctx: dict, force_type: str | None = None,
                       avoid: list | None = None, avoid_amounts: list | None = None,
                       slepok_key: str = "") -> tuple[dict, list[str]]:
    """Фолбэк-промо, когда M3 недоступна/сбоит. Сначала из БД-библиотеки слепка (если засеяна),
    иначе детерминированно из пресета agent['promo'] + примеров стиля. → (promo, warnings).
    Описание проходит тот же _promo_validate (лимиты + гард типа сайта)."""
    import random
    from . import ai_agents as A
    p = agent["promo"]
    site_type = (ctx.get("site_type") or "").strip()
    avoid_l = {str(a).strip().lower() for a in (avoid or [])}
    ft = (force_type or "").upper()
    # 1) БД-библиотека слепка (приоритет) — берём вариант нужного типа, не из уже показанных
    lib = _slepok_content_get(slepok_key, site_type, "promo") if slepok_key else None
    if isinstance(lib, list) and lib:
        items = [x for x in lib if isinstance(x, dict)]
        pool = [x for x in items if (not ft or str(x.get("type", "")).upper() == ft)] or items
        fresh = [x for x in pool if str(x.get("description", "")).strip().lower() not in avoid_l] or pool
        if fresh:
            promo, warns = _promo_validate(random.choice(fresh), agent, site_type=site_type)
            if ft == "GIFT":
                promo["unit"] = "RUB"
            return promo, warns
    # 2) иначе — из код-корпуса пресета агента
    typ = (force_type or p["type"]).upper()
    if typ not in A.PROMO_TYPES:
        typ = p["type"]
    unit = "RUB" if typ == "GIFT" else p["unit"]
    # размер: «красивый» шаг из диапазона стиля, по возможности не из уже показанных
    excl = {int(a) for a in (avoid_amounts or []) if str(a).strip().isdigit()}
    steps = [x for x in _promo_amount_steps(p, unit, typ) if x not in excl] or _promo_amount_steps(p, unit, typ)
    amount = random.choice(steps)
    # описание: пример из корпуса стиля агента, по возможности не из уже показанных
    avoid_l = {str(a).strip().lower() for a in (avoid or [])}
    examples = [e for e in (p.get("examples") or []) if e and e.strip().lower() not in avoid_l]
    if not examples:
        examples = list(p.get("examples") or ["спецпредложение"])
    desc = random.choice(examples)
    raw = {"type": typ, "amount": amount, "unit": unit, "prefix": p.get("prefix"), "description": desc}
    promo, warns = _promo_validate(raw, agent, site_type=(ctx.get("site_type") or ""))
    if typ == "GIFT":
        promo["unit"] = "RUB"
    return promo, warns


def _seed_one_slepok_promo(slepok_key: str, site_type: str, m3_timeout: float = 25.0) -> dict:
    """Ensure `direct_slepok_content(kind='promo')` for one slepok x site_type.

    This is the lightweight version used inside campaign creation: it does not seed
    campaign text banks and does not loop over all agents/site types.
    """
    from . import ai_agents as A
    key = _slepok_key_from_text(slepok_key)
    st = (site_type or "").strip()
    agent = A.get_agent(key)
    if not agent or not st:
        return {"ok": False, "error": "unknown_slepok_or_site_type"}
    existing = _slepok_content_get(key, st, "promo")
    if isinstance(existing, list) and existing:
        return {"ok": True, "source": "skip", "n": len(existing)}

    p = agent["promo"]
    neutral = {"С пробегом": ["на авто с пробегом", "на проверенные авто", "за автокредит"],
               "Мульти + БУ": ["на авто в наличии", "за автокредит", "при покупке в кредит"]}
    neutral_new = ["на новые авто", "при покупке в кредит", "по госпрограмме"]
    # dmp — B2B-лидоген (НЕ авто): не подмешивать авто-фолбэк («на новые авто»/«в кредит»/
    # «по госпрограмме»). У dmp есть собственные B2B-examples в agent["promo"]["examples"].
    neutral_fallback = [] if st == "dmp" else neutral_new
    ctx = {"site_type": st, "domain": "", "salon": "", "city": ""}
    variants, src, seen = [], "slepok", set()

    for ft in (p["type"], "PROFIT", "GIFT"):
        msgs = A.build_promo_messages(agent, ctx, force_type=ft)
        text, err = _m3_complete(msgs, max_tokens=300, temperature=0.95,
                                 tries=1, timeout=m3_timeout)
        raw = _promo_extract_json(text) if not err else {}
        if not raw:
            continue
        pr, _ = _promo_validate(raw, agent, site_type=st)
        if ft in A.PROMO_TYPES:
            pr["type"] = ft
            if ft == "GIFT":
                pr["unit"] = "RUB"
        k = (pr.get("description") or "").strip().lower()
        if k and k not in seen:
            seen.add(k)
            variants.append(pr)
            src = "m3"

    for ex in list(p.get("examples") or []) + neutral.get(st, neutral_fallback):
        if A.is_bu_site_type(st) and A._bad_for_bu(ex):
            continue
        pr, _ = _promo_validate({"type": p["type"], "amount": None, "unit": p["unit"],
                                 "prefix": p.get("prefix"), "description": ex},
                                agent, site_type=st)
        k = (pr.get("description") or "").strip().lower()
        if k and k not in seen:
            seen.add(k)
            variants.append(pr)

    if not variants:
        return {"ok": False, "error": "no_variants"}
    saved = _slepok_content_save(key, st, "promo", variants[:8], src)
    return {"ok": bool(saved), "source": src, "n": len(variants[:8])}


def _create_account_promo_from_slepok(client, login: str, token: str | None, ctx: dict,
                                      slepok_key: str, content_lines: list[str]) -> tuple[int | None, str]:
    """Create one promo in client's library, without attaching it yet."""
    from . import ai_agents as A
    from .promo import PromoClient

    key = _selected_slepok_key(slepok_key)
    agent_obj = A.get_agent(key)
    if not agent_obj:
        return None, "слепок для автопромо не выбран явно"

    st = (ctx.get("site_type") or "").strip()
    seed = _seed_one_slepok_promo(key, st, m3_timeout=25.0)
    promo, warns = _promo_from_slepok(agent_obj, ctx, slepok_key=key)
    content_pct = _dominant_discount_pct(content_lines)
    if content_pct:
        promo["type"] = "PROFIT" if promo.get("type") == "PROFIT" else "DISCOUNT"
        promo["unit"] = "PCT"
        promo["amount"] = int(content_pct)
        if promo.get("prefix") not in ("TO", "FROM"):
            promo["prefix"] = "TO"
    # dmp — B2B-лидоген «до N% лидов»: text-only промо (НЕ денежная скидка). Grid-схема
    # для этого промо требует amount/unit/prefix = null (иначе DefectIds.MUST_BE_NULL,
    # см. ERRORS_JOURNAL DMP_PROMO_GRID_MUST_BE_NULL). Изолировано по site_type — авто-слепки
    # со скидкой (amount нужен) не затрагиваются. Посыл несёт description (text-only).
    if st == "dmp":
        promo["amount"] = None
        promo["unit"] = None
        promo["prefix"] = None
    pseudo = {"Name": _promo_preview(promo), "Description": promo.get("description"),
              "Promocode": promo.get("promocode"), "Type": promo.get("type"),
              "Amount": promo.get("amount"), "AmountUnit": promo.get("unit")}
    okp, why = _promo_usable_for_content(pseudo, content_lines)
    if not okp:
        return None, f"сгенерированное промо конфликтует с контентом: {why}"

    domain = (ctx.get("domain") or "").strip()
    href = "https://" + domain if domain and not domain.startswith(("http://", "https://")) else domain
    if not href:
        return None, "нет домена аккаунта для промо"
    try:
        client.link_info(href)
    except Exception:
        pass
    pid, perr = PromoClient(client, login).add(
        type=promo["type"], description=promo["description"], href=href,
        amount=promo["amount"], unit=promo["unit"], prefix=promo["prefix"],
        promocode=promo["promocode"], finish=promo["finishDate"],
    )
    if not pid:
        return None, f"grid отклонил автопромо: {perr}"

    verified = ""
    if token:
        jp = _v5_get("promotions", token, login,
                     ["Id", "Type", "Name", "Description", "Amount", "AmountUnit"], criteria={})
        for it in (jp.get("result") or {}).get("Promotions", []):
            if str(it.get("Id")) == str(pid):
                verified = " подтверждено v5"
                break
    seed_note = f", seed={seed.get('source')}/{seed.get('n')}" if seed.get("ok") else ""
    warn_note = f"; {'; '.join(warns[:2])}" if warns else ""
    return int(pid), f"автопромо создано по слепку {key}: id {pid}{verified}{seed_note}{warn_note}"






from . import ai_agents as _ai_agents_routes  # noqa: E402
from .promo import PromoClient as _PromoClientRoutes  # noqa: E402



# Bind the extracted services once all runtime callbacks are available.
_queue_server.configure({
    "_sweep_empty_drafts": _sweep_empty_drafts,
    "_create_set_job_context": _create_set_job_context,
    "_repair_deps": _repair_deps,
    "_create_set_live_verification": _create_set_live_verification,
    "_run_spec_audit_and_fix": _run_spec_audit_and_fix,
    "_finalize_queue_module": _finalize_queue_module,
    "_delete_drafts_core": _delete_drafts_core,
    "_create_set_response": _create_set_response,
    "_auto_queue_recreate_after_done": _auto_queue_recreate_after_done,
    "_prefetch_start": _prefetch_start,
    "_set_llm_heartbeat_job": _set_llm_heartbeat_job,
})
_job_repository.configure({
    "_child_parent_ref": _child_parent_ref,
    "_parent_absorb_child_progress": _parent_absorb_child_progress,
    "_account_ctx": _account_ctx,
})
_pack_resolver.configure({
    "_json": _json,
    "_ct_segment_map": _ct_segment_map,
    "_m3_llm_probe": _m3_llm_probe,
    "_openrouter_probe": _openrouter_probe,
    "_openrouter_completion_probe": _openrouter_completion_probe,
    "_touch_running_jobs_heartbeat": _touch_running_jobs_heartbeat,
})
_account_service.configure({
    "_pull_begin": _pull_begin,
    "_pull_end": _pull_end,
    "_busy_response": _busy_response,
    "_global_feed_rules": _global_feed_rules,
    "_filter_allowed_feed_rows": _filter_allowed_feed_rows,
    "_grid_feeds": _grid_feeds,
    "_account_ctx": _account_ctx,
    "_metrika_goals_for": _metrika_goals_for,
    "_goal_vse_formy": _goal_vse_formy,
    "_load_corrections": _load_corrections,
    "_corrections_by_segment": _corrections_by_segment,
    "_bump_job": _bump_job,
    "_job_db_progress": _job_db_progress,
    "_job_db_save": _job_db_save,
    "_job_new": _job_new,
    "_create_jobs_ahead": _create_jobs_ahead,
    "_ensure_create_worker": _ensure_create_worker,
    "_CREATE_JOBS_LOCK": _CREATE_JOBS_LOCK,
})


# 4 DI для ai_content (AI-контент): БД-коннекты + _gc_ct + _cached_campaign_content (blueprint).
# В КОНЦЕ модуля: _cached_campaign_content определяется ниже места импорта — инъекция после его def.
_aic.configure({
    "_victory_conn": _victory_conn, "_victory_conn_rw": _victory_conn_rw,
    "_gc_ct": _gc_ct, "_cached_campaign_content": _cached_campaign_content,
})

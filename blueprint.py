"""
Direct Automation Blueprint — «Автоматизация Директа».

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
from flask import Blueprint, render_template, request, jsonify, current_app, session, send_file

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auth import _service_required_any  # noqa: E402

# Доступ к Директу: админ (bypass внутри декоратора) ИЛИ юзер с ключом
# "work" (parent) / "work:direct". Совпадает с навигацией (_nav.html) и
# реестром _BUILTIN_SECTIONS в app.py — юзер с грантом видит ссылку И может всё.
_direct_access = _service_required_any("work", "work:direct")
_direct_minusphrase_access = _service_required_any("work", "work:direct", "work:direct:minusphrase")
# РАЗРУШИТЕЛЬНЫЕ операции (остановить ВСЕ РК, удалить ВСЕ черновики) — отдельный, более узкий грант
# "work:direct:danger". Админ — bypass (внутри декоратора). Обычный юзер с одним лишь "work"/"work:direct"
# создавать может, но массово останавливать/удалять — НЕТ (нужен явный danger-грант). Безопасный дефолт:
# нет danger-гранта → разрушительные операции доступны только админу.
_direct_danger = _service_required_any("work:direct:danger")

from . import campaign as cmc  # vendored движок
from . import grid_finalize as gf  # Grid-докрутка ЕПК (tp1-tp5): места показа/ассеты/инварианты
from . import grid_create as gc  # Куки-движок создания/удаления (Grid web-api, без баллов v5)
from . import kontent_pack as kp  # чтение контент-пака с M3 (/opt/neuro_kontent)
from . import llm_providers as _llmp   # M3/OpenRouter (вынесено из blueprint; heartbeat инъектим ниже)
from .llm_providers import (           # ре-экспорт: внутренние вызовы + deps-словари модулей
    _M3_LLM_URL, _M3_LLM_TIMEOUT, _M3_LLM_URLS_14B, _M3_LLM_URL_72B,
    _M3_LLM_TIMEOUT_14B, _M3_LLM_REPAIR_TIMEOUT, _M3_CONTENT_IDLE_TIMEOUT, _OPENROUTER_LLM_MODEL,
    _m3_llm_probe, _m3_complete, _m3_complete_url, _m3_complete_parallel,
    _openrouter_api_key, _openrouter_probe, _or_complete_url, _llm_pair_for,
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
)
from . import copy_engine as _ce       # копирование кампаний 1:1 (вынесено; 28 DI инъектим ниже)
from .copy_engine import (             # ре-экспорт: _create_worker_loop/_ensure_copy_worker/_wire_copy_routes
    _copy_run_job, _copy_job_upsert, _copy_feeds_preview, _copy_jobs_recover,
    _COPY_JOBS, _COPY_JOBS_LOCK, _COPY_DEFAULT_FEED_PATH,
)
from . import repair_gate as rgate  # read-only repair-gate helpers
from . import repair_executor as rex  # scoped repair executors (cookie/Grid-first)
from . import repair_auto as rauto  # repair orchestration without Flask/DB wiring
from . import verification_service as vsvc  # live verification orchestration without Flask

_HERE = Path(__file__).resolve().parent


def _json(name: str):
    return json.loads((_HERE / name).read_text(encoding="utf-8"))

bp = Blueprint(
    "direct",
    __name__,
    url_prefix="/direct",
    template_folder=str(Path(__file__).resolve().parents[1] / "templates"),
)


def init_direct() -> None:
    """Хук инициализации (БД не нужна)."""
    return None


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
_CREATE_JOBS: dict = {}          # job_id → {status, login, done, total, created, failed, result, error, cancel, body, session}
_CREATE_JOBS_LOCK = threading.Lock()
_CREATE_COND = threading.Condition(_CREATE_JOBS_LOCK)   # сигналит worker'у о новой джобе
_CREATE_QUEUE: list = []         # job_id'ы, ждущие выполнения (FIFO)
_CREATE_WORKER: dict = {"started": False}
_CREATE_WATCHDOG: dict = {"started": False}
_JOB_TERMINAL = ("done", "error", "cancelled", "interrupted")
_JOB_DB_LAST: dict = {}          # jid → monotonic последнего DB-флеша прогресса (троттлинг)
# Пул создания: параллелим по разным агентствам, но на ОДНО агентство держим только 1
# активную create-джобу. Практически весь боевой путь использует UAC/Grid/куки хотя бы
# на части шагов, и 2 одновременных аккаунта одного агентства дают зависания/гонки сессии.
_CREATE_WORKERS = 0              # 0 = по числу агентских токенов/кук
_CREATE_POOL_PAUSE = 15          # сек паузы после УСПЕШНОГО полного аккаунта
_CREATE_MAX_PER_AGENCY = 1
_CREATE_ACTIVE_AGENCIES: dict[str, int] = {}   # агентский ключ -> число активных джоб прямо сейчас
_CREATE_RUNNING_TIMEOUT = 1200   # сек без прогресса -> watchdog завершает зависшую running-джобу
_CREATE_WATCHDOG_POLL = 30       # период watchdog, сек


# ── Роль процесса (Фаза 2: раздельные сервисы web/worker) ───────────────────────
# DIRECT_ROLE управляет тем, кто держит очередь создания РК:
#   'all'    (дефолт) — постановка in-memory + воркеры/демоны в одном процессе (как раньше,
#                        полная обратная совместимость: код можно задеплоить БЕЗ включения split);
#   'web'    — только Flask + постановка джоб в БД (status='queued', _web_posted=true).
#              Воркеры/демоны/поллер НЕ стартуют. Статус/отмена/resume/feed — через БД.
#   'worker' — worker_main.py: воркеры + все демоны + БД-поллер, забирающий web-posted джобы
#              из БД в свою in-memory очередь. Именно этот процесс исполняет создание РК.
def _direct_role() -> str:
    r = (os.environ.get("DIRECT_ROLE") or "all").strip().lower()
    return r if r in ("web", "worker", "all") else "all"


# Drain: SIGTERM воркеру → перестать брать НОВЫЕ джобы, дать текущим доработать текущий item,
# затем выйти (running-остаток в БД → _jobs_db_recover при следующем старте пометит interrupted).
_CREATE_DRAIN = {"on": False}
_WORKER_POLLER = {"started": False}
_WORKER_POLL_SEC = 2             # период БД-поллинга web-posted джоб (только worker-роль)


def _worker_request_drain() -> None:
    """SIGTERM handler в worker_main: включить drain и разбудить всех ждущих воркеров."""
    _CREATE_DRAIN["on"] = True
    try:
        with _CREATE_COND:
            _CREATE_COND.notify_all()
    except Exception:  # noqa: BLE001
        pass


def _worker_is_draining() -> bool:
    return bool(_CREATE_DRAIN.get("on"))


def _job_agency(job: dict) -> str:
    """Ключ агентства джобы — партиционирование очереди.

    api_create_set_async разрешает реальное агентство ДО постановки в очередь
    (_resolve_agency_hint: кэш БД + local_gsheet_sites, без API-вызовов к Яндексу),
    поэтому body["agency"] уже содержит физическое название агентства (не "").
    Фолбэк «» сохранён консервативно: для пустого ключа действует тот же лимит параллельности."""
    return ((job.get("body") or {}).get("agency") or "").strip().lower()


def _job_touch(job: dict | None) -> None:
    """Локальный heartbeat джобы для watchdog'а."""
    if not job:
        return
    job["_heartbeat"] = time.time()


# Сериализует read-modify-write счётчиков job при C1-параллельных каналах создания
# (DIRECT_PARALLEL_CHANNELS): master/product-путь канала B бампает эти же функции напрямую (через
# _master_product_deps), а не через обёртки оркестратора. Uncontended в OFF/однопоточных потоках →
# поведение не меняется (только защита от lost-update при двух потоках).
_JOB_MUT_LOCK = threading.Lock()


def _bump_job(job, ok: bool = True, n: int = 1) -> None:
    """Инкремент счётчиков по ФАКТУ созданной кампании (fan-out даёт N кампаний на 1 пункт плана)."""
    if not job:
        return
    with _JOB_MUT_LOCK:
        if ok:
            job["created"] = int(job.get("created") or 0) + n
        else:
            job["failed"] = int(job.get("failed") or 0) + n
        job["_heartbeat"] = time.time()   # watchdog: прогресс по ЛЮБОЙ кампании (создание/ошибка) = живой


def _bump_item(job) -> None:
    """Инкремент set_done: вызывать ОДИН РАЗ после завершения каждого item набора (не за каждую кампанию fan-out)."""
    if not job:
        return
    with _JOB_MUT_LOCK:
        job["set_done"] = int(job.get("set_done") or 0) + 1
        job["_heartbeat"] = time.time()   # watchdog: каждый обработанный item (вкл. skip/пропуск) = живой


def _add_job_err(job, err) -> None:
    """Добавить ошибку в job['errors_log'] (лимит 100). err — строка или dict с ключом 'error'."""
    if not job:
        return
    msg = (err if isinstance(err, str)
           else (err.get("error") or "; ".join(err.get("errors") or [])))
    if not msg:
        return
    log = job.setdefault("errors_log", [])
    log.append(str(msg)[:300])
    if len(log) > 100:
        del log[:-100]


# ── Серверная персистентность очереди (public.direct_automation_jobs на Victory) ──
# Цель: очередь живёт на СЕРВЕРЕ — видна с любого устройства, переживает рестарт сервиса
# (для просмотра). Все DB-операции best-effort: падение БД НЕ ломает создание кампаний.
def _jobs_db_init() -> None:
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS public.direct_automation_jobs (
                    job_id     text PRIMARY KEY,
                    login      text,
                    status     text,
                    total      int DEFAULT 0,
                    done       int DEFAULT 0,
                    created     int DEFAULT 0,
                    failed     int DEFAULT 0,
                    kind       text,
                    publish    boolean DEFAULT false,
                    error      text,
                    result     jsonb,
                    body       jsonb,
                    agency     text,
                    control    text,
                    created_at timestamptz DEFAULT now(),
                    updated_at timestamptz DEFAULT now()
                )""")
            # миграция: добавить колонки если таблица уже существовала без них
            cur.execute("ALTER TABLE public.direct_automation_jobs ADD COLUMN IF NOT EXISTS body jsonb")
            cur.execute("ALTER TABLE public.direct_automation_jobs ADD COLUMN IF NOT EXISTS agency text")
            cur.execute("ALTER TABLE public.direct_automation_jobs ADD COLUMN IF NOT EXISTS errors_log jsonb")
            # control: команда web→worker (сейчас используется 'cancel' для running-джоб; worker её NULL-ит)
            cur.execute("ALTER TABLE public.direct_automation_jobs ADD COLUMN IF NOT EXISTS control text")
            # Кросс-процессный per-agency гейт (create-worker ↔ copy-worker делят куки/баллы агентства).
            cur.execute("""
                CREATE TABLE IF NOT EXISTS public.direct_agency_active (
                    agency     text PRIMARY KEY,
                    job_id     text NOT NULL,
                    started_at timestamptz NOT NULL DEFAULT now()
                )""")
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _job_db_save(jid: str, job: dict, *, full: bool = False) -> None:
    """UPSERT строки джобы. full=True пишет result (на терминальном статусе).
    body/agency сохраняются только при INSERT (не перетираются при обновлении прогресса)."""
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            res_json = json.dumps(job.get("result"), ensure_ascii=False) if (full and job.get("result")) else None
            body_raw = job.get("body")
            body_json = json.dumps(body_raw, ensure_ascii=False) if body_raw else None
            agency_val = (job.get("agency") or _job_agency(job) or None)
            err_log = job.get("errors_log")
            err_log_json = json.dumps(err_log, ensure_ascii=False) if err_log else None
            cur.execute("""
                INSERT INTO public.direct_automation_jobs
                    (job_id, login, status, total, done, created, failed, kind, publish, error, result, body, agency, errors_log, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                ON CONFLICT (job_id) DO UPDATE SET
                    status=EXCLUDED.status, total=EXCLUDED.total, done=EXCLUDED.done,
                    created=EXCLUDED.created, failed=EXCLUDED.failed, error=EXCLUDED.error,
                    result=COALESCE(EXCLUDED.result, public.direct_automation_jobs.result),
                    body=COALESCE(public.direct_automation_jobs.body, EXCLUDED.body),
                    agency=COALESCE(public.direct_automation_jobs.agency, EXCLUDED.agency),
                    errors_log=COALESCE(EXCLUDED.errors_log, public.direct_automation_jobs.errors_log),
                    updated_at=now()
            """, (jid, job.get("login"), job.get("status"), int(job.get("total") or 0),
                  int(job.get("done") or 0), int(job.get("created") or 0), int(job.get("failed") or 0),
                  job.get("kind"), bool(job.get("publish")), (job.get("error") or None)[:500] if job.get("error") else None,
                  res_json, body_json, agency_val, err_log_json))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _job_db_delete(jid: str) -> None:
    """Удалить строку джобы из БД немедленно (ручная «отмена» завершённой карточки — без ожидания TTL)."""
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM public.direct_automation_jobs WHERE job_id=%s", (jid,))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _job_db_get(jid: str) -> dict | None:
    """Прочитать сохранённую джобу из БД, включая terminal result."""
    if not jid:
        return None
    try:
        conn = _victory_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM public.direct_automation_jobs WHERE job_id=%s", (jid,))
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


# ── web-роль: общение с worker'ом через БД (без in-memory очереди) ──────────────
def _job_db_active_by_login(login: str) -> str | None:
    """job_id активной (не завершённой) джобы этого логина в БД — дедуп на web-роли."""
    lg = (login or "").strip()
    if not lg:
        return None
    try:
        conn = _victory_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT job_id FROM public.direct_automation_jobs "
                        "WHERE login=%s AND status NOT IN ('done','error','cancelled','interrupted') "
                        "ORDER BY updated_at DESC LIMIT 1", (lg,))
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


def _job_db_set_status(jid: str, status: str, error: str | None = None) -> None:
    """Прямая смена статуса джобы в БД (web-роль: отмена queued/awaiting, resolve feed)."""
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE public.direct_automation_jobs "
                        "SET status=%s, error=COALESCE(%s,error), updated_at=now() WHERE job_id=%s",
                        (status, (error[:500] if error else None), jid))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _job_control_set(jid: str, control: str) -> None:
    """Записать команду web→worker в колонку control (worker применит и обнулит)."""
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE public.direct_automation_jobs SET control=%s, updated_at=now() "
                        "WHERE job_id=%s", (control, jid))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _job_db_web_await_feed(jid: str, deadline: float) -> None:
    """web-роль: перевести свежую queued web-джобу в ожидание решения по фиду (дедлайн в body)."""
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE public.direct_automation_jobs "
                "SET status='awaiting_feed_decision', "
                "    body = jsonb_set(COALESCE(body,'{}'::jsonb), '{_feed_deadline}', to_jsonb(%s::double precision)), "
                "    updated_at=now() "
                "WHERE job_id=%s AND status='queued'", (float(deadline), jid))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _job_db_web_resolve_feed(jid: str, decision: str) -> None:
    """web-роль: ответ пользователя по фиду. run_without_feed → _skip_feed_types, затем status='queued'
    (worker подхватит клеймом). _feed_deadline очищаем."""
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            if decision == "run_without_feed":
                cur.execute(
                    "UPDATE public.direct_automation_jobs "
                    "SET status='queued', "
                    "    body = jsonb_set(body - '_feed_deadline', '{_skip_feed_types}', "
                    "                     '[\"product\",\"master\"]'::jsonb), "
                    "    updated_at=now() "
                    "WHERE job_id=%s AND status='awaiting_feed_decision'", (jid,))
            else:  # run_all
                cur.execute(
                    "UPDATE public.direct_automation_jobs "
                    "SET status='queued', body = (body - '_feed_deadline' - '_skip_feed_types'), "
                    "    updated_at=now() "
                    "WHERE job_id=%s AND status='awaiting_feed_decision'", (jid,))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _job_db_list_recent(active_only: bool = False) -> list[dict]:
    """web-роль: живая очередь из БД (аналог in-memory _CREATE_JOBS для обзора)."""
    import psycopg2.extras
    try:
        conn = _victory_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            if active_only:
                cur.execute("SELECT * FROM public.direct_automation_jobs "
                            "WHERE status NOT IN ('done','error','cancelled','interrupted') "
                            "ORDER BY created_at DESC LIMIT 50")
            else:
                cur.execute("SELECT * FROM public.direct_automation_jobs "
                            "ORDER BY updated_at DESC LIMIT 50")
            return list(cur.fetchall() or [])
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return []


def _job_db_ahead(jid: str) -> int:
    """web-роль: сколько активных джоб «впереди» (created_at раньше). Приблизительно, для UI."""
    try:
        conn = _victory_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT count(*) FROM public.direct_automation_jobs a "
                "JOIN public.direct_automation_jobs b ON b.job_id=%s "
                "WHERE a.status IN ('queued','claimed','running','awaiting_feed_decision') "
                "  AND a.created_at < b.created_at", (jid,))
            row = cur.fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return 0


def _job_db_progress(job: dict) -> None:
    """Лёгкий троттлинг-флеш прогресса в БД (не чаще ~4 c на джобу)."""
    jid = job.get("_id")
    if not jid:
        return
    _job_touch(job)
    now = time.monotonic()
    if now - _JOB_DB_LAST.get(jid, 0.0) < 4.0:
        return
    _JOB_DB_LAST[jid] = now
    _job_db_save(jid, job)
    # Live-вливание прогресса дочерней добивки в родительскую карточку (троттлинг тот же ≤4 c).
    _pref = _child_parent_ref(job.get("body"))
    if _pref and _pref != jid:
        _parent_absorb_child_progress(
            _pref, jid, int(job.get("created") or 0), int(job.get("failed") or 0),
            int(job.get("set_done") or job.get("done") or 0), final=False)


def _jobs_db_mark_stale_running(timeout_sec: int = _CREATE_RUNNING_TIMEOUT) -> list[str]:
    """Битые running-джобы в БД (без heartbeat слишком долго) → interrupted.

    Нужен как бэкстоп: после reload/restart или зависшего внешнего cookie/UAC-вызова в UI не
    должно оставаться вечных 'running'.
    """
    stuck: list[str] = []
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE public.direct_automation_jobs
                   SET status='interrupted',
                       error=CASE
                           WHEN coalesce(error,'')='' THEN %s
                           ELSE error
                       END,
                       updated_at=now()
                 WHERE status='running'
                   AND updated_at < now() - make_interval(secs => %s)
                RETURNING job_id
                """,
                (f"watchdog: running без прогресса > {int(timeout_sec // 60)} мин", int(timeout_sec)),
            )
            stuck = [r[0] for r in (cur.fetchall() or [])]
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return []
    return stuck


def _jobs_db_recover() -> None:
    """При старте сервиса: поднять недавние джобы в память для ПРОСМОТРА; незавершённые
    (queued/running) пометить 'interrupted' — worker-очередь после рестарта пуста, авто-докрутку
    не делаем (защита от дублей: повторный клик «Создать» сам пропустит уже созданные через set_plan)."""
    _interrupted_logins: list = []
    _deferred_db_init()                                  # таблица остатков должна существовать до UPDATE ниже
    _delayed_repair_db_init()
    try:
        import psycopg2.extras
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            # ЛОГИНЫ прерванных джоб — для авто-очистки их пустышек (кампания создана, рестарт убил
            # процесс до наполнения групп → 0 групп). Берём ДО UPDATE, пока статус ещё running/queued.
            # kind='copy_campaigns' исключаем: эти джобы принадлежат отдельному процессу
            # direct-copy.service — их recover/sweep не наш (иначе затрём чужой статус и снесём
            # свежесозданные черновики копирования). См. _ensure_copy_worker/_copy_jobs_recover.
            cur.execute("SELECT DISTINCT login FROM public.direct_automation_jobs "
                        "WHERE status IN ('queued','running') AND login IS NOT NULL "
                        "  AND coalesce(kind,'') <> 'copy_campaigns' "
                        "  AND updated_at > now() - interval '6 hours'")
            _interrupted_logins = [r["login"] for r in cur.fetchall() if r.get("login")]
            # битые running/queued → interrupted (single UPDATE).
            # ВАЖНО: web-posted queued-джобы (_web_posted=true) НЕ трогаем — их ещё не начинал
            # исполнять ни один воркер, они ждут клейма поллером. Пометив их interrupted, мы бы
            # потеряли постановку сразу после рестарта воркера. Гасим только «свои» in-memory queued
            # (их в БД пишет _job_new всех ролей кроме web) и любые running.
            cur.execute("UPDATE public.direct_automation_jobs SET status='interrupted', updated_at=now() "
                        "WHERE (status='running' "
                        "       OR (status='queued' AND coalesce(body->>'_web_posted','') <> 'true')) "
                        "  AND coalesce(kind,'') <> 'copy_campaigns'")
            # 'claimed' — web-posted джоба, которую поллер забрал из БД, но воркер упал ДО того, как
            # завёл её в in-memory очередь (окно миллисекунды). body ещё содержит items+session →
            # безопасно вернуть в 'queued' для повторного клейма (дубля нет: set_plan пропустит созданное).
            cur.execute("UPDATE public.direct_automation_jobs SET status='queued', updated_at=now() "
                        "WHERE status='claimed'")
            # CRASH-SAFETY ОСТАТКОВ: 'resumed'-остаток (докрутка по куке поставлена в очередь), который
            # завис дольше N часов без финала — джоба умерла при рестарте, остаток осиротел. Возвращаем
            # в waiting+resume_at=now(), чтобы демон подхватил его ПО КУКЕ. Дубля нет: set_plan пропустит
            # уже созданные кампании; финал докрутки пометит строку done (не зациклится на рестартах).
            # resume_count += 1 + кап < _RESUME_MAX: «ядовитый» набор (всегда падает) не перезапускается
            # бесконечно при каждом рестарте — после _RESUME_MAX оживлений остаётся 'resumed' (брошен).
            cur.execute("UPDATE public.direct_deferred_creates "
                        "SET status='waiting', resume_at=now(), updated_at=now(), "
                        "    resume_count = resume_count + 1 "
                        "WHERE status='resumed' AND updated_at < now() - make_interval(hours => %s) "
                        "  AND COALESCE(resume_count,0) < %s",
                        (int(_DEFERRED_STALE_HOURS), int(_RESUME_MAX)))
            # СТАРУЮ историю не храним: завершённые джобы старше TTL — удаляем сразу при старте.
            cur.execute("DELETE FROM public.direct_automation_jobs WHERE status = ANY(%s) "
                        "AND updated_at < now() - make_interval(secs => %s)",
                        (list(_JOB_TERMINAL), _JOB_HISTORY_TTL))
            conn.commit()
            # поднять только СВЕЖИЕ джобы (активные + завершённые за последние TTL) — историю не копим
            cur.execute("SELECT * FROM public.direct_automation_jobs "
                        "WHERE status NOT IN ('done','error','cancelled','interrupted') "
                        "   OR updated_at > now() - make_interval(secs => %s) "
                        "ORDER BY updated_at DESC LIMIT 50", (_JOB_HISTORY_TTL,))
            for r in cur.fetchall():
                jid = r["job_id"]
                if jid in _CREATE_JOBS:
                    continue
                # finished_at терминальной джобы = когда она реально завершилась (из updated_at),
                # чтобы карточка ушла ровно через TTL после завершения, а не после рестарта.
                fin = None
                if r["status"] in _JOB_TERMINAL:
                    try:
                        fin = r["updated_at"].timestamp()
                    except Exception:  # noqa: BLE001
                        fin = time.time()
                # body/agency восстанавливаем из БД — нужны для resume прерванных джоб
                saved_body = r.get("body")   # psycopg2 RealDictCursor уже десериализует jsonb → dict
                _CREATE_JOBS[jid] = {"status": r["status"], "login": r.get("login"),
                                     "done": r.get("done") or 0, "total": r.get("total") or 0,
                                     "created": r.get("created") or 0, "failed": r.get("failed") or 0,
                                     "result": r.get("result"), "error": r.get("error"),
                                     "cancel": False, "kind": r.get("kind"),
                                     "publish": bool(r.get("publish")), "_id": jid,
                                     "finished_at": fin, "body": saved_body,
                                     "agency": r.get("agency"),
                                     "session": None,
                                     "step": None, "stream_content": False}   # step/stream не хранятся в БД
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass
    # АВТО-ОЧИСТКА ПУСТЫШЕК: для аккаунтов прерванных джоб удаляем пустые ЕПК-черновики (0 групп —
    # кампания создалась, но рестарт убил сборку). В фоне (не блокируем старт) и ТОЛЬКО при старте,
    # когда активного создания ещё нет (гонок с наполнением групп нет). По куке, без баллов.
    if _interrupted_logins:
        # Собрать прерванные джобы для reconciler — _CREATE_JOBS уже заполнен SELECT'ом выше.
        # Исключаем requeue-джобы (_requeue_of != '') — они сами по себе уже доставка,
        # внучки не ставим (gate внутри _requeue_missing_positions_once тоже проверяет это).
        _interrupted_jobs: list = []  # [(job_id, login, body), ...]
        _interrupted_login_set = set(_interrupted_logins)
        with _CREATE_JOBS_LOCK:
            for _jid, _jdata in _CREATE_JOBS.items():
                if (_jdata.get("status") == "interrupted"
                        and _jdata.get("login") in _interrupted_login_set):
                    _ijbody = _jdata.get("body") or {}
                    if (_ijbody.get("items")                         # есть позиции для доставки
                            and not str(_ijbody.get("_requeue_of") or "").strip()):  # не сама доставка
                        _interrupted_jobs.append((_jid, str(_jdata["login"]), _ijbody))

        def _bg_sweep(logins, interrupted_jobs):
            time.sleep(8)                                # дать сервису и воркеру подняться
            for lg in logins:
                try:
                    n = _sweep_empty_drafts(lg)
                    if n:
                        print(f"[startup-sweep] {lg}: удалено пустых ЕПК-черновиков: {n}", flush=True)
                except Exception:  # noqa: BLE001
                    pass
            # RECONCILER: после сноса пустышек сверяем план vs. кабинет для каждой прерванной
            # джобы и доставляем недостающие позиции повторной джобой. Гейты внутри
            # _requeue_missing_positions_once: (1) _requeue_of → без внучек;
            # (2) auto_requeue_missing → без дублей при повторных рестартах;
            # (3) _job_db_active_by_login → не конкурируем с текущей активной джобой логина.
            # Порядок важен: sweep сначала (пустые UAC-оболочки удалены), тогда Grid покажет
            # реальное отсутствие позиций, которые были удалены до обрыва.
            time.sleep(5)                                # Grid: пауза после sweep для стабилизации
            for job_id, lg, body in interrupted_jobs:
                try:
                    new_jid = _requeue_missing_positions_once(job_id, lg, body)
                    if new_jid:
                        print(f"[startup-reconcile] {lg}: восстановление прерванных позиций "
                              f"→ джоба {new_jid} (родитель {job_id})", flush=True)
                except Exception:  # noqa: BLE001
                    pass
        threading.Thread(target=_bg_sweep, args=(list(_interrupted_logins), _interrupted_jobs), daemon=True).start()


_JOB_HISTORY_TTL = 86400        # сек: завершённые джобы (история + errors_log) живут СУТКИ, потом удаляются


def _jobs_purge_old() -> None:
    """Удалить завершённые джобы старше TTL — из памяти и из БД. Историю не храним (по требованию)."""
    now = time.time()
    with _CREATE_JOBS_LOCK:
        stale = [k for k, v in _CREATE_JOBS.items()
                 if v.get("status") in _JOB_TERMINAL
                 and (now - (v.get("finished_at") or 0)) > _JOB_HISTORY_TTL]
        for k in stale:
            _CREATE_JOBS.pop(k, None)
            _JOB_DB_LAST.pop(k, None)
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM public.direct_automation_jobs WHERE status = ANY(%s) "
                        "AND updated_at < now() - make_interval(secs => %s)",
                        (list(_JOB_TERMINAL), _JOB_HISTORY_TTL))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _create_watchdog_tick() -> None:
    """Одиночный проход watchdog: локальные зависшие running-джобы и stale running в БД."""
    timed_out: list[tuple[str, dict]] = []
    now = time.time()
    with _CREATE_COND:
        for jid, job in list(_CREATE_JOBS.items()):
            if job.get("status") != "running":
                continue
            heartbeat = max(float(job.get("_heartbeat") or 0), float(job.get("started_at") or 0))
            if not heartbeat or (now - heartbeat) <= _CREATE_RUNNING_TIMEOUT:
                continue
            # Не красим error почти-завершённую джобу: на куки-бэкфилле она массово ПРОПУСКАЕТ уже
            # созданные (created не растёт, но done доходит до total) — это не зависание. heartbeat
            # теперь тикает на каждый обработанный item (_bump_item/_bump_job), но done>=total — явный
            # признак, что джоба фактически дошла до конца и финализируется.
            if int(job.get("done") or 0) >= int(job.get("total") or 0) > 0:
                continue
            job["status"] = "error"
            job["error"] = f"watchdog: running без прогресса > {int(_CREATE_RUNNING_TIMEOUT // 60)} мин"
            job["result"] = {"error": job["error"]}
            job["finished_at"] = now
            job["_watchdog_done"] = True
            job["cancel"] = True
            timed_out.append((jid, dict(job)))
            agency = _job_agency(job)
            active = max(0, int(_CREATE_ACTIVE_AGENCIES.get(agency, 0)) - 1)
            if active:
                _CREATE_ACTIVE_AGENCIES[agency] = active
            else:
                _CREATE_ACTIVE_AGENCIES.pop(agency, None)
            _agency_gate_release(agency, jid)             # кросс-процессный слот (watchdog-таймаут)
        # Проверка awaiting_feed_decision: дедлайн истёк — запускаем без фида
        _expired_feed_awaiting = False
        for jid, job in list(_CREATE_JOBS.items()):
            if job.get("status") != "awaiting_feed_decision":
                continue
            _dl = float(job.get("feed_deadline") or 0)
            if not _dl or now <= _dl:
                continue
            _body = job.get("body") or {}
            _body["_skip_feed_types"] = ["product", "master"]
            job["status"] = "queued"
            _CREATE_QUEUE.append(jid)
            _expired_feed_awaiting = True
        if timed_out or _expired_feed_awaiting:
            _CREATE_COND.notify_all()
    if timed_out:
        # Диагностика зависаний (2026-07-02): watchdog убивает джобу, но БЕЗ стека виновника
        # причину не найти (jobs 9126bf12fb3a/ac6d98864aa4 — «тишина 24 мин»). Дампим стеки ВСЕХ
        # тредов в /tmp — файл переживает джобу, py-spy пост-фактум уже бесполезен (тред вернулся в пул).
        try:
            import faulthandler
            _tr_path = f"/tmp/direct_stall_{int(now)}.trace"
            with open(_tr_path, "w") as _fh:
                _fh.write(f"watchdog kill: {[j for j, _ in timed_out]} at {time.ctime(now)}\n\n")
                faulthandler.dump_traceback(file=_fh, all_threads=True)
            import logging as _lg
            _lg.getLogger("direct.watchdog").warning(
                "watchdog kill %s — стеки тредов: %s", [j for j, _ in timed_out], _tr_path)
        except Exception:  # noqa: BLE001
            pass
    for jid, snap in timed_out:
        _job_db_save(jid, snap, full=True)
    _jobs_db_mark_stale_running(_CREATE_RUNNING_TIMEOUT)
    _agency_gate_sweep()                                  # освободить слоты агентств крашнутых/терминальных джоб


def _create_watchdog_loop() -> None:
    while True:
        try:
            _create_watchdog_tick()
            _jobs_purge_old()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(_CREATE_WATCHDOG_POLL)


def _ensure_create_watchdog() -> None:
    with _CREATE_JOBS_LOCK:
        if _CREATE_WATCHDOG["started"]:
            return
        _CREATE_WATCHDOG["started"] = True
    threading.Thread(target=_create_watchdog_loop, daemon=True).start()


# ── Авто-докрутка остатка набора после сброса баллов Директа (полночь МСК) ──────
# При error 152 (исчерпан суточный лимит баллов) остаток набора НЕ теряем: сохраняем в
# public.direct_deferred_creates и фоновый демон докручивает его, как только баллы восстановятся
# (сброс — полночь МСК = 21:00 UTC). Дедупа не нужно: остаток = пункты, которые ещё НЕ начинали.
_RESUME_DAEMON = {"started": False}
_RESUME_MAX = 3                                       # макс. авто-докруток одного остатка (анти-цикл)
_RESUME_POLL = 120                                    # период опроса демона, сек (~2 мин: добивка «сразу»,
                                                      # Семён 2026-07-07 — никаких ночных отложек по расписанию;
                                                      # запрос дешёвый LIMIT 5, нагрузку не меняет)
_DEFERRED_STALE_HOURS = 3                             # 'resumed'-остаток без финала дольше N часов = осиротел
                                                      # (джоба умерла при рестарте) → вернуть в waiting+now()
_DELAYED_REPAIR_DAEMON = {"started": False}
_DELAYED_REPAIR_POLL = 60
_DELAYED_CONTENT_REPAIR_DELAY_SECONDS = 180
_DELAYED_FULL_REPAIR_MAX_ITERATIONS = 2   # верифай→исполнить-всё→ре-верифай; защита от ping-pong
_DELAYED_REPAIR_STUCK_TIMEOUT_SECONDS = 1800  # 30 мин: running→failed (watchdog ПРАВКА P2)
# B2: явный бюджет времени на ОДНУ repair-джобу (< watchdog-таймаута 1800с). Исчерпан → корректно
# завершаем статусом partial БЕЗ reschedule (иначе на большом аккаунте цикл full-Grid-верификаций
# упирается в watchdog kill вместо чистого partial).
_DELAYED_REPAIR_TIME_BUDGET_SECONDS = 1200  # 20 мин
# B1: коды Grid-ошибок «поле недоступно/неизвестно для этой схемы/фида» — НЕфиксабельно in-place
# (мутация вернёт executed=0 навсегда). Такую проблему исключаем из inplace-остатка, чтобы цикл
# не перепланировался по кругу на нечинимом флаге до watchdog kill.
_REPAIR_NONFIXABLE_FIELD_MARKERS = (
    "UNAVAILABLE_FIELD", "UNKNOWN_FIELD", "MINUS_MARKS_FILTER_MISSING",
    "FIELD_NOT_ALLOWED", "INVALID_FIELD",
)


def _repair_failures_nonfixable(failed_actions: list) -> bool:
    """True если ВСЕ провалившиеся in-place действия несут field-ошибку Grid (нечинимо для этой
    схемы/фида) — тогда повторять/перепланировать бессмысленно. Пусто → False (нечего оценивать).

    Проверяем ТОЛЬКО структурированные коды ошибок (validationResult.errors[].code и
    extensions.code из top-level errors) — НЕ весь сериализованный blob. Если структура
    ошибки неоднородна (plain exception-строка, нет dict-result) — считаем fixable (ретраить
    безопаснее, чем ошибочно бросить)."""
    if not failed_actions:
        return False
    for fa in failed_actions:
        result = fa.get("result") if isinstance(fa, dict) else None
        if not isinstance(result, dict):
            # Структура неизвестна (plain-exception или нет result) → считаем fixable
            return False
        # Собираем коды ошибок из двух источников:
        # 1) top-level errors[].extensions.code (транспортные/авторизационные ошибки Grid)
        # 2) validationResult.errors[].code (валидационные ошибки схемы/фида)
        codes: list[str] = []
        for e in (result.get("errors") or []):
            c = (e.get("extensions") or {}).get("code") if isinstance(e, dict) else None
            if c:
                codes.append(str(c).upper())
        for e in (result.get("validationResult") or {}).get("errors") or []:
            c = e.get("code") if isinstance(e, dict) else None
            if c:
                codes.append(str(c).upper())
        if not codes:
            # Нет структурированных кодов → неизвестная ошибка, считаем fixable
            return False
        if not any(any(m in code for m in _REPAIR_NONFIXABLE_FIELD_MARKERS) for code in codes):
            return False   # хотя бы один код НЕ field-ошибка → возможно чинимо
    return True


def _next_units_reset_utc():
    """Следующий сброс суточных баллов Директа = полночь МСК (UTC+3) + буфер 15 мин → aware UTC datetime.
    Полночь МСК = 21:00 UTC. Если уже позже — переносим на завтра."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    target = now.replace(hour=21, minute=15, second=0, microsecond=0)
    if now >= target:
        target = target + timedelta(days=1)
    return target


def _deferred_db_init() -> None:
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS public.direct_deferred_creates (
                    id          text PRIMARY KEY,
                    login       text,
                    agency      text,
                    job_id      text,
                    body        jsonb,
                    n_items     int DEFAULT 0,
                    status      text DEFAULT 'waiting',
                    resume_count int DEFAULT 0,
                    resume_at   timestamptz,
                    note        text,
                    created_at  timestamptz DEFAULT now(),
                    updated_at  timestamptz DEFAULT now()
                )""")
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _deferred_save(login: str, agency: str, body: dict, remaining_items: list,
                   job_id: str | None, resume_count: int = 0,
                   resume_at: str | None = None, exclude_id: str | None = None) -> str | None:
    """Сохранить остаток набора для авто-докрутки после сброса баллов. → id или None.

    resume_at=None (по умолчанию) → докрутка по куке СРАЗУ (now()), как раньше.
    resume_at=ISO-строка (напр. _next_units_reset_utc().isoformat()) — докрутка не раньше
    этого момента: нужна пунктам, которые в принципе НЕ создать по куке (напр. NO_BRAND_
    SEGMENTS_AVAILABLE — сегментный tp5 требует ТОКЕН/M3, не Grid-куку) — тогда caller
    также должен положить body['_resume_via_token']=True (см. _resume_one_deferred)."""
    if not remaining_items:
        return None
    b = dict(body or {})
    b["items"] = remaining_items
    b.pop("_job_id", None)
    did = uuid.uuid4().hex[:12]
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            # ДЕДУП (2026-07-07): одна позиция НЕ должна висеть в двух активных деферредах —
            # двойная постановка (guardrail сработал в двух джобах подряд) давала ДУБЛИ кампаний
            # при докрутке (кейс psm: 8 деферредов на 4 позиции → 8 лишних tp5). Если ЛЮБОЕ из
            # имён remaining_items уже в waiting/resumed деферреде этого логина — возвращаем
            # существующий id, второй не создаём.
            # exclude_id: НЕ дедупить по самой резюмящейся строке. Токен-ретрай сегментного tp5
            # планируется ИЗ resume-джобы, чья строка (body._deferred_id) уже 'resumed' и содержит
            # тот же item → без исключения дедуп вернул бы ЕЁ id (self-reference) → финал пометил бы
            # её done, а новая токен-строка не создалась → tp5 теряется (инцидент 08.07 721641cad7c1).
            for _it in remaining_items:
                _nm = str((_it or {}).get("name") or "").strip()
                if not _nm:
                    continue
                if exclude_id:
                    cur.execute(
                        "SELECT id FROM public.direct_deferred_creates "
                        "WHERE login=%s AND status IN ('waiting','resumed') AND id <> %s "
                        "AND body->'items' @> %s::jsonb LIMIT 1",
                        (login, exclude_id, json.dumps([{"name": _nm}], ensure_ascii=False)))
                else:
                    cur.execute(
                        "SELECT id FROM public.direct_deferred_creates "
                        "WHERE login=%s AND status IN ('waiting','resumed') "
                        "AND body->'items' @> %s::jsonb LIMIT 1",
                        (login, json.dumps([{"name": _nm}], ensure_ascii=False)))
                _dup = cur.fetchone()
                if _dup:
                    print(f"[deferred-save] {login}: позиция «{_nm[:60]}» уже в активном "
                          f"деферреде {_dup[0]} — дубль не создаём", flush=True)
                    return str(_dup[0])
            if resume_at:
                cur.execute(
                    "INSERT INTO public.direct_deferred_creates "
                    "(id, login, agency, job_id, body, n_items, status, resume_count, resume_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,'waiting',%s,%s::timestamptz)",
                    (did, login, agency, job_id, json.dumps(b, ensure_ascii=False),
                     len(remaining_items), int(resume_count), resume_at))
            else:
                cur.execute(
                    "INSERT INTO public.direct_deferred_creates "
                    "(id, login, agency, job_id, body, n_items, status, resume_count, resume_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,'waiting',%s, now())",   # resume_at=now(): докрутка по куке СРАЗУ
                    (did, login, agency, job_id, json.dumps(b, ensure_ascii=False),
                     len(remaining_items), int(resume_count)))
            conn.commit()
        finally:
            conn.close()
        return did
    except Exception as _e:  # noqa: BLE001
        # НЕ молчим (фикс 2026-07-06): немой None здесь = потерянный набор без следа
        # (guardrail NO_BRAND_SEGMENTS_AVAILABLE обещал докрутку токеном, а деферред не создавался).
        print(f"[deferred-save] {login}: ошибка сохранения остатка ({len(remaining_items)} шт): "
              f"{type(_e).__name__}: {str(_e)[:200]}", flush=True)
        return None


def _deferred_set_status(did: str, status: str, note: str | None = None) -> None:
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE public.direct_deferred_creates SET status=%s, note=COALESCE(%s,note), "
                        "updated_at=now() WHERE id=%s", (status, note, did))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _deferred_bump_resume_at(did: str, hours: int = 1) -> None:
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE public.direct_deferred_creates SET resume_at=now()+(%s||' hours')::interval, "
                        "updated_at=now() WHERE id=%s", (str(int(hours)), did))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _delayed_repair_db_init() -> None:
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS public.direct_delayed_repairs (
                    id            text PRIMARY KEY,
                    parent_job_id text,
                    login         text,
                    agency        text,
                    kind          text,
                    status        text DEFAULT 'waiting',
                    attempts      int DEFAULT 0,
                    run_at        timestamptz,
                    note          text,
                    result        jsonb,
                    created_at    timestamptz DEFAULT now(),
                    updated_at    timestamptz DEFAULT now()
                )""")
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS direct_delayed_repairs_parent_kind_uq
                ON public.direct_delayed_repairs(parent_job_id, kind)
            """)
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _delayed_repair_set_status(did: str, status: str, note: str | None = None,
                               result: dict | None = None) -> None:
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            result_json = json.dumps(result, ensure_ascii=False) if result is not None else None
            cur.execute("""
                UPDATE public.direct_delayed_repairs
                   SET status=%s,
                       note=COALESCE(%s,note),
                       result=COALESCE(%s::jsonb,result),
                       updated_at=now()
                 WHERE id=%s
            """, (status, note, result_json, did))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _delayed_content_repair_save(parent_job_id: str, login: str, agency: str,
                                 *, delay_seconds: int = _DELAYED_CONTENT_REPAIR_DELAY_SECONDS,
                                 kind: str = "content_repair") -> str | None:
    parent_job_id = (parent_job_id or "").strip()
    login = (login or "").strip()
    if not parent_job_id or not login:
        return None
    did = uuid.uuid4().hex[:12]
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO public.direct_delayed_repairs
                    (id, parent_job_id, login, agency, kind, status, attempts, run_at)
                VALUES (%s,%s,%s,%s,%s,'waiting',0,
                        now() + (%s || ' seconds')::interval)
                ON CONFLICT (parent_job_id, kind) DO NOTHING
                RETURNING id
            """, (did, parent_job_id, login, agency or "", kind, str(int(delay_seconds))))
            row = cur.fetchone()
            conn.commit()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


def _supersede_delayed_repairs_for_login(login: str) -> None:
    """При пользовательском ПЕРЕЗАПУСКЕ набора на тот же login (dedup_login=True, старая джоба
    уже terminal → дедуп не сработал → новая джоба пересоздаёт кампании под новыми campaign_id) —
    старые waiting/running добивки от ПРЕДЫДУЩЕГО прогона гоняют удалённые campaign_id и падают
    с CAMPAIGN_NOT_FOUND, жгут попытки впустую (живой кейс 06.07.2026, porg-7bqj56f4). Best-effort,
    fail-open: сбой БД НЕ должен блокировать постановку новой джобы."""
    login = (login or "").strip()
    if not login:
        return
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("""
                UPDATE public.direct_delayed_repairs
                   SET status='superseded', note='login пересоздан новым набором', updated_at=now()
                 WHERE login=%s AND status IN ('waiting','running')
            """, (login,))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


_DELAYED_REPAIR_MAX_RESCHEDULES = 1   # 1 reschedule → 2 прогона добивки всего (+ 1 создание = 3 попытки)


# ── «Готовые логины» — реестр аккаунтов с загруженными кампаниями (вкладка UI) ──────
# Пополняется на done create-джобы (kind set/slepok, created>0); логин УХОДИТ из списка,
# когда наш сервис удалил черновики (kind delete_drafts done). Ручное удаление/очистка — API.

def _ready_logins_db_init() -> None:
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS public.direct_ready_logins (
                    id            serial PRIMARY KEY,
                    login         text UNIQUE NOT NULL,
                    loaded_at     timestamptz DEFAULT now(),
                    campaigns     int DEFAULT 0,
                    specialist    text, city text, domain text, site_type text,
                    slepok        text, content_source text, elapsed_seconds int
                )
            """)
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — best-effort, реестр не валит воркер
        pass


def _ready_login_upsert(login: str, *, campaigns: int, slepok: str, content_source: str,
                        elapsed_seconds: int, add: bool = False) -> None:
    """UPSERT строки реестра. add=True (джоба-доставка) — кампании ПРИБАВЛЯЮТСЯ к существующим."""
    login = (login or "").strip()
    if not login:
        return
    acc = _account_ctx(login) or {}
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO public.direct_ready_logins
                    (login, loaded_at, campaigns, specialist, city, domain, site_type,
                     slepok, content_source, elapsed_seconds)
                VALUES (%s, now(), %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (login) DO UPDATE SET
                    loaded_at=now(),
                    campaigns=CASE WHEN %s THEN public.direct_ready_logins.campaigns + EXCLUDED.campaigns
                                   ELSE EXCLUDED.campaigns END,
                    specialist=EXCLUDED.specialist, city=EXCLUDED.city, domain=EXCLUDED.domain,
                    site_type=EXCLUDED.site_type, slepok=EXCLUDED.slepok,
                    content_source=EXCLUDED.content_source,
                    elapsed_seconds=CASE WHEN %s THEN COALESCE(public.direct_ready_logins.elapsed_seconds,0)
                                              + COALESCE(EXCLUDED.elapsed_seconds,0)
                                         ELSE EXCLUDED.elapsed_seconds END
            """, (login, int(campaigns or 0), (acc.get("directologist") or "").strip(),
                  (acc.get("city") or "").strip(), (acc.get("domain") or "").strip(),
                  (acc.get("site_type") or "").strip(), (slepok or "").strip(),
                  (content_source or "").strip(), int(elapsed_seconds or 0),
                  bool(add), bool(add)))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _ready_login_remove(login: str) -> None:
    login = (login or "").strip()
    if not login:
        return
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM public.direct_ready_logins WHERE login=%s", (login,))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _ready_logins_track(jid: str, job: dict) -> None:
    """Хук финализации воркера: пополнить/убрать логин в реестре «Готовые логины»."""
    try:
        if (job or {}).get("status") != "done":
            return
        login = (job.get("login") or "").strip()
        kind = job.get("kind") or ""
        body = job.get("body") or {}
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except Exception:  # noqa: BLE001
                body = {}
        if kind == "delete_drafts":
            _ready_login_remove(login)
            return
        if kind not in ("set", "slepok"):
            return
        created = int(job.get("created") or 0)
        if created <= 0:
            return
        _ready_logins_db_init()
        result = rgate.dict_from_jsonish(job.get("result")) or {}
        src = str(result.get("content_source") or body.get("content_source") or "").strip()
        content_label = ("М3/слепок" if src in ("slepok_library", "m3", "slepok")
                         else ("OpenRouter" if body.get("stream_content") else (src or "М3")))
        _ready_login_upsert(
            login,
            campaigns=created,
            slepok=str(body.get("agent") or "").strip(),
            content_source=content_label,
            elapsed_seconds=int(result.get("elapsed_seconds") or job.get("elapsed") or 0),
            add=bool(str(body.get("_requeue_of") or "").strip()),   # доставка → прибавляем
        )
    except Exception:  # noqa: BLE001
        pass


def _requeue_missing_positions_once(parent_job_id: str, login: str, body: dict) -> str | None:
    """Доставить потерянные позиции набора повторной create-джобой (ОДИН раз на родителя).

    Гейты: (1) у родителя failed>0 или created<total; (2) в result родителя ещё нет маркера
    auto_requeue_missing; (3) сама джоба-доставка (_requeue_of в body) внучек не ставит.
    Тело — то же (без runtime-ключей): RESUME-SKIP в оркестраторе пропустит уже созданные.
    Возвращает job_id новой джобы или None."""
    try:
        if str((body or {}).get("_requeue_of") or "").strip():
            return None                              # джоба-доставка → без внучек
        pj = _job_db_get(parent_job_id) or {}
        if not pj:
            return None
        total = int(pj.get("total") or 0)
        created = int(pj.get("created") or 0)
        failed = int(pj.get("failed") or 0)
        if failed <= 0 and created >= total:
            return None                              # состав полный — доставка не нужна
        p_res = rgate.dict_from_jsonish(pj.get("result"))
        if not isinstance(p_res, dict):
            p_res = {}
        if p_res.get("auto_requeue_missing"):
            return None                              # уже доставляли — не зацикливаемся
        rbody = {k: v for k, v in dict(body or {}).items()
                 if not str(k).startswith("_")
                 and k not in ("feed_alert", "feed_confirmed", "status", "result", "error")}
        items = rbody.get("items") or []
        if not items:
            return None
        # ТОЛЬКО реально отсутствующие позиции (сверка по кабинету): полное тело создало бы
        # ДУБЛИ tp6/tp7 — их live-имена переименованы UAC и RESUME-SKIP их не матчит
        # (живой кейс: «ТК_AT_tcpa …_v02» дубли). Grid недоступен → НЕ доставляем (риск дублей).
        rows = _grid_list_campaigns(login) or []
        names = {str(r.get("name") or "").strip() for r in rows if r.get("name")}
        if not names:
            return None
        missing = [it for it in items
                   if not _position_live_in_names(str((it or {}).get("name") or ""), names)]
        if not missing:
            return None                              # состав фактически полный — доставка не нужна
        rbody["items"] = missing
        rbody["_requeue_of"] = parent_job_id
        # Активная джоба логина? Тогда доставку НЕ ставим и маркер НЕ сжигаем (ревью 06.07:
        # dedup_login=True возвращал ЧУЖОЙ job_id, rbody выбрасывался, а одноразовый маркер
        # auto_requeue_missing сгорал → позиции не доставлялись никогда). Доставим на следующем
        # финале delayed-repair, когда логин освободится.
        if _job_db_active_by_login(login):
            print(f"[requeue-missing] {login}: у логина активная джоба — доставка отложена, "
                  f"маркер не проставлен", flush=True)
            return None
        # доставка остатка = добивка → приоритет (Семён 2026-07-06: сразу, не в конец очереди)
        new_jid = _job_new_web(len(missing), login, rbody, {}, False, priority=True)
        if not new_jid:
            return None
        p_res["auto_requeue_missing"] = {"job_id": new_jid, "was_created": created,
                                         "was_failed": failed, "total": total}
        pj["result"] = p_res
        _job_db_save(parent_job_id, pj, full=True)
        with _CREATE_JOBS_LOCK:
            mem = _CREATE_JOBS.get(parent_job_id)
            if mem is not None and isinstance(mem.get("result"), dict):
                mem["result"]["auto_requeue_missing"] = p_res["auto_requeue_missing"]
        print(f"[requeue-missing] {login}: доставка недостающих позиций джобой {new_jid} "
              f"(родитель {parent_job_id}: created={created}/{total}, failed={failed})", flush=True)
        return new_jid
    except Exception:  # noqa: BLE001 — доставка best-effort
        return None


def _position_live_in_names(nm: str, names: set) -> bool:
    """Позиция плана жива? already_in_direct + UAC-нормализация: tp6/tp7 при создании
    переименовывают фид-суффикс («…site/yandex.xml» → «…site — yandex»), поэтому полный
    item-name не матчится — пробуем без последнего « — сегмента» (только для tp6/tp7 с
    ≥2 сепараторами, иначе усечение до 'tp1_cpc_site' сматчило бы ЛЮБУЮ tp1)."""
    from .create_set_resume import already_in_direct
    nm = (nm or "").strip()
    if not nm:
        return True
    if already_in_direct(nm, names):
        return True
    if nm.startswith(("tp6_", "tp7_")) and nm.count(" — ") >= 2:
        base = nm.rsplit(" — ", 1)[0].strip()
        if base and already_in_direct(base, names):
            return True
    return False


def _plan_positions_all_live(login: str, body: dict) -> bool | None:
    """Каждая позиция плана имеет живую кампанию в кабинете? (префикс-матч как RESUME-SKIP).
    None — Grid недоступен (консервативно: НЕ реконсилировать). Live-сверка результатов слепа
    к НЕсозданным позициям (видит только results) — этот чек закрывает дыру."""
    try:
        rows = _grid_list_campaigns(login) or []
        names = {str(r.get("name") or "").strip() for r in rows if r.get("name")}
        if not names:
            return None
        for it in ((body or {}).get("items") or []):
            if not _position_live_in_names(str(it.get("name") or ""), names):
                return False
        return True
    except Exception:  # noqa: BLE001
        return None


def _reconcile_parent_job_counters(parent_job_id: str, last_live: dict, last_summ: dict,
                                   *, login: str = "", body: dict | None = None) -> bool:
    """После УСПЕШНОЙ добивки карточка не должна показывать «создано N · ❌ M», если ошибок
    реально нет (требование Семёна 2026-07-05). СТРОГО: обновляем счётчики ТОЛЬКО когда
    live-сверка по кабинету дала errors=0, очередь пересоздания пуста И (при переданных
    login+body) КАЖДАЯ позиция плана жива в кабинете — иначе не трогаем
    (честность важнее красивой карточки). failed→0, created→total, пометка в result."""
    try:
        live_errors = int(((last_live or {}).get("summary") or {}).get("errors") or 0)
        queued_rec = int((last_summ or {}).get("queued_recreate_items") or 0)
        if live_errors > 0 or queued_rec > 0:
            return False
        if login and body:
            if _plan_positions_all_live(login, body) is not True:
                return False                   # позиция плана отсутствует в кабинете / Grid недоступен
        job = _job_db_get(parent_job_id) or {}
        if not job:
            return False
        total = int(job.get("total") or 0)
        created = int(job.get("created") or 0)
        failed = int(job.get("failed") or 0)
        if failed <= 0 and created >= total:
            return False                       # уже чисто — нечего реконсилировать
        result = rgate.dict_from_jsonish(job.get("result"))
        if not isinstance(result, dict):
            result = {}
        result["counters_reconciled_by_repair"] = {
            "was_created": created, "was_failed": failed,
            "live_errors": live_errors, "note": "добивка подтвердила: все кампании набора живы",
        }
        job["created"] = max(created, total)
        job["failed"] = 0
        job["error"] = None
        job["result"] = result
        _job_db_save(parent_job_id, job, full=True)
        with _CREATE_JOBS_LOCK:
            mem = _CREATE_JOBS.get(parent_job_id)
            if mem is not None:
                mem["created"] = job["created"]
                mem["failed"] = 0
                mem["error"] = None
                if isinstance(mem.get("result"), dict):
                    mem["result"]["counters_reconciled_by_repair"] = result["counters_reconciled_by_repair"]
                _job_touch(mem)
        return True
    except Exception:  # noqa: BLE001 — реконсиляция best-effort, добивка уже записана
        return False


def _delayed_repair_reschedule(did: str, row: dict, remaining: int) -> bool:
    """Вернуть partial-строку добивки в waiting для следующего цикла («до нуля»).
    attempts++ при каждом повторе; после _DELAYED_REPAIR_MAX_RESCHEDULES — стоп (нечинимый
    остаток не должен крутить демона вечно). True — перепланировано."""
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("""
                UPDATE public.direct_delayed_repairs
                   SET status='waiting', attempts=attempts+1,
                       run_at=now() + (%s || ' seconds')::interval,
                       note='повтор добивки (остаток ' || %s || ')',
                       updated_at=now()
                 WHERE id=%s AND attempts < %s
            """, (str(_DELAYED_CONTENT_REPAIR_DELAY_SECONDS), str(int(remaining)),
                  did, _DELAYED_REPAIR_MAX_RESCHEDULES))
            conn.commit()
            return bool(cur.rowcount)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — повтор best-effort, partial-статус уже записан
        return False


def _child_parent_ref(body) -> str:
    """job_id родителя для ЛЮБОЙ дочерней джобы: докрутка 152/резерв (_resume_of), доставка
    недостающих (_requeue_of), recreate-починка (_repair_parent_job_id). '' — джоба самостоятельная."""
    b = body or {}
    for k in ("_resume_of", "_requeue_of", "_repair_parent_job_id"):
        v = str((b.get(k) or "")).strip()
        if v:
            return v
    return ""


def _parent_update(parent_jid: str, mutate) -> bool:
    """Прочитать родительскую джобу (БД → истина), применить mutate(job,result), записать
    в БД и in-memory. mutate возвращает False → изменений нет (не пишем). Best-effort:
    родителя нет (TTL/убран) → тихо пропускаем (Семён 2026-07-07: дочерняя работает без карточки)."""
    job = _job_db_get(parent_jid) or {}
    if not job:
        return False
    result = rgate.dict_from_jsonish(job.get("result"))
    if not isinstance(result, dict):
        result = {}
    job["result"] = result
    if mutate(job, result) is False:
        return False
    _job_db_save(parent_jid, job, full=True)
    with _CREATE_JOBS_LOCK:
        mem = _CREATE_JOBS.get(parent_jid)
        if mem is not None:
            for _k in ("created", "failed", "done", "total", "set_total", "status",
                       "error", "finished_at"):
                if _k in job:
                    mem[_k] = job[_k]
            mem["result"] = result
            _job_touch(mem)
    return True


def _parent_absorb_child_start(parent_jid: str, child_jid: str, child_total: int) -> None:
    """Старт дочерней добивки → родитель снова «в работе»: done -= объём добивки (total остаётся),
    прогресс-бар падает (было 14/14 → стало 9/14 при добивке 5 шт.), добиваемые пункты покидают
    bucket «не создано» (failed -= child_total; при неуспехе вернутся дельтой через
    _parent_absorb_child_progress). Идемпотентно по child_jid (ре-клейм после рестарта не
    задваивает)."""
    if not parent_jid or not child_jid or parent_jid == child_jid:
        return
    def _m(job, result):
        children = result.setdefault("_resume_children", {})
        if child_jid in children:
            return False                                 # уже учтён — не задваиваем
        children[child_jid] = {"c": 0, "f": 0, "d": 0}
        active = result.setdefault("_active_children", [])
        if child_jid not in active:
            active.append(child_jid)
        ct = int(child_total or 0)
        job["done"] = max(0, int(job.get("done") or 0) - ct)
        job["failed"] = max(0, int(job.get("failed") or 0) - ct)
        job["status"] = "running"
        job["error"] = None
        job["finished_at"] = None
        return True
    try:
        _parent_update(parent_jid, _m)
    except Exception:  # noqa: BLE001 — вливание best-effort
        pass


def _parent_absorb_child_progress(parent_jid: str, child_jid: str, created: int,
                                  failed: int, done_units: int, *, final: bool = False) -> None:
    """Влить ЖИВОЙ прогресс дочерней добивки в родителя дельтами (без задвоения при повторных
    вызовах — база хранится в result['_resume_children'][child_jid]). created/failed/done
    родителя пополняются по мере добивки; при final последний ребёнок → карточка снова
    терминальная (done, бар 100%)."""
    if not parent_jid or not child_jid or parent_jid == child_jid:
        return
    def _m(job, result):
        children = result.setdefault("_resume_children", {})
        base = children.get(child_jid)
        if base is None:                                 # start-хук не отработал → учитываем с нуля
            base = {"c": 0, "f": 0, "d": 0}
            children[child_jid] = base
        dc = int(created or 0) - int(base.get("c") or 0)
        df = int(failed or 0) - int(base.get("f") or 0)
        dd = int(done_units or 0) - int(base.get("d") or 0)
        job["created"] = max(0, min(int(job.get("total") or 0), int(job.get("created") or 0) + dc))
        job["failed"] = max(0, min(int(job.get("total") or 0), int(job.get("failed") or 0) + df))
        job["done"] = min(int(job.get("total") or 0), int(job.get("done") or 0) + dd)
        base["c"] = int(created or 0)
        base["f"] = int(failed or 0)
        base["d"] = int(done_units or 0)
        if final:
            hist = result.get("resume_merged")
            if not isinstance(hist, list):
                hist = []
            hist.append({"job_id": child_jid, "created": int(created or 0),
                         "failed": int(failed or 0)})
            result["resume_merged"] = hist[-10:]
            active = result.setdefault("_active_children", [])
            if child_jid in active:
                active.remove(child_jid)
            if not active:                               # все добивки закрыты → карточка терминальна
                job["done"] = int(job.get("total") or 0)
                job["status"] = "done"
                job["finished_at"] = time.time()
        else:
            job["status"] = "running"
        return True
    try:
        _parent_update(parent_jid, _m)
    except Exception:  # noqa: BLE001 — вливание best-effort
        pass


def _merge_resume_into_parent(jid: str, job_final: dict, body: dict) -> None:
    """Финальное вливание дочерней добивки (докрутка/доставка/recreate) в родительскую карточку
    (Семён 2026-07-06/07: «по карточке видно сколько создалось/добилось/готово»). Дельтами через
    _parent_absorb_child_progress — согласовано с live-прогрессом (start-хук + периодический sync),
    без задвоения. Саму дочернюю джобу /api/create_jobs НЕ отдаёт отдельной карточкой."""
    parent_jid = _child_parent_ref(body)
    if not parent_jid or parent_jid == jid:
        return
    _du = int(job_final.get("set_done") or job_final.get("done") or job_final.get("total") or 0)
    _parent_absorb_child_progress(
        parent_jid, jid, int(job_final.get("created") or 0),
        int(job_final.get("failed") or 0), _du, final=True)


def _cancel_children_of(parent_jid: str) -> int:
    """Отмена родителя каскадом гасит его активные дочерние джобы (докрутка/доставка/recreate)
    того же логина (Семён 2026-07-07). queued/claimed/awaiting → cancelled; running → control=cancel
    (worker остановит после текущей кампании). → число погашенных дочерних."""
    parent_jid = (parent_jid or "").strip()
    if not parent_jid:
        return 0
    rows = []
    try:
        import psycopg2.extras
        conn = _victory_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""
                SELECT job_id, status FROM public.direct_automation_jobs
                 WHERE status NOT IN ('done','error','cancelled','interrupted')
                   AND (body->>'_resume_of'=%s OR body->>'_requeue_of'=%s
                        OR body->>'_repair_parent_job_id'=%s)
            """, (parent_jid, parent_jid, parent_jid))
            rows = cur.fetchall() or []
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        rows = []
    n = 0
    for r in rows:
        cjid = r["job_id"]
        st = (r.get("status") or "").strip()
        try:
            if st in ("queued", "claimed", "awaiting_feed_decision"):
                _job_db_set_status(cjid, "cancelled", "отменено вместе с родителем")
            else:                                        # running → команда worker'у
                _job_control_set(cjid, "cancel")
            with _CREATE_JOBS_LOCK:
                mem = _CREATE_JOBS.get(cjid)
                if mem is not None:
                    mem["cancel"] = True
                    if mem.get("status") in ("queued", "claimed") and cjid in _CREATE_QUEUE:
                        _CREATE_QUEUE.remove(cjid)
                        mem["status"] = "cancelled"
            n += 1
        except Exception:  # noqa: BLE001
            pass
    return n


def _record_delayed_content_repair(parent_job_id: str, row: dict) -> None:
    job = _job_db_get(parent_job_id) or {}
    if not job:
        return
    result = rgate.dict_from_jsonish(job.get("result"))
    if not isinstance(result, dict):
        result = {}
    history = result.get("delayed_content_repair")
    if not isinstance(history, list):
        history = [] if history is None else [history]
    history.append(row)
    result["delayed_content_repair"] = history[-5:]
    job["result"] = result
    _job_db_save(parent_job_id, job, full=True)
    with _CREATE_JOBS_LOCK:
        mem = _CREATE_JOBS.get(parent_job_id)
        if mem is not None and isinstance(mem.get("result"), dict):
            mem["result"] = result
            _job_touch(mem)


def _record_auto_repair_full(parent_job_id: str, payload: dict) -> None:
    """Write the top-level ``auto_repair_full`` summary into the parent job result (mem + DB).

    UI (_renderJobVerification) reads this key to show «✅ авто-добивка: исполнено X действий».
    """
    job = _job_db_get(parent_job_id) or {}
    if not job:
        return
    result = rgate.dict_from_jsonish(job.get("result"))
    if not isinstance(result, dict):
        result = {}
    result["auto_repair_full"] = payload
    job["result"] = result
    _job_db_save(parent_job_id, job, full=True)
    with _CREATE_JOBS_LOCK:
        mem = _CREATE_JOBS.get(parent_job_id)
        if mem is not None and isinstance(mem.get("result"), dict):
            mem["result"]["auto_repair_full"] = payload
            _job_touch(mem)


def _schedule_delayed_content_repair_after_done(parent_job_id: str, job_snapshot: dict) -> dict | None:
    req = rauto.delayed_content_repair_request(parent_job_id, job_snapshot)
    if not req:
        return None
    if req.get("scheduled") is False:
        return req
    did = _delayed_content_repair_save(
        parent_job_id,
        req.get("login") or "",
        req.get("agency") or "",
        kind="content_repair_post_recreate" if req.get("post_recreate") else "content_repair",
    )
    if did:
        # Родитель уже «done» — возвращаем в running: delayed-repair ещё не завершён.
        # child_total=0 → done/failed не трогаем, только status=running + добавляем в _active_children.
        _parent_absorb_child_start(parent_job_id, f"dcr:{did}", 0)
    out = {
        "scheduled": bool(did),
        "delayed_repair_id": did,
        "source": req.get("source") or "delayed_after_done",
        "content_repairs": req.get("content_repairs") or 0,
        "run_after_seconds": _DELAYED_CONTENT_REPAIR_DELAY_SECONDS,
        "uses_direct_units": False,
    }
    if not did:
        out["note"] = "delayed content repair уже был запланирован или не сохранён"
    return out


def _run_delayed_content_repair(row: dict) -> None:
    """Delayed FULL in-place repair cycle after a create job is done.

    Runs OFF the worker thread (in the delayed-repair daemon) on a job whose status is already
    ``done`` and ``finished_at`` is set → the watchdog (_create_watchdog_tick) only touches
    ``running`` jobs, so no heartbeat bump is needed here.

    Cycle: fresh Grid-first live verification (Grid has caught up after the delay) → execute ALL
    executable in-place actions (content/promo/callouts/rename) via the SAME executors as the
    manual «План добивки» button (rauto.execute_all_in_place) → re-verify. Up to
    _DELAYED_FULL_REPAIR_MAX_ITERATIONS iterations; stop early if nothing progresses (anti
    ping-pong). Recreate/UAC-replace stays with _auto_queue_recreate_after_done.
    """
    did = (row.get("id") or "").strip()
    parent_job_id = (row.get("parent_job_id") or "").strip()
    _delayed_repair_set_status(did, "running", "повторная Grid-first проверка перед авто-добивкой")
    job, result, ctx, err = _create_set_job_context(parent_job_id)
    if err:
        out = {"ok": False, "error": err[0].get("error"), "uses_direct_units": False}
        _delayed_repair_set_status(did, "error", out["error"], out)
        _record_delayed_content_repair(parent_job_id, {"id": did, "status": "error", **out})
        _parent_absorb_child_progress(parent_job_id, f"dcr:{did}", 0, 0, 0, final=True)
        return
    login = (ctx.get("login") or row.get("login") or "").strip()
    if not login:
        out = {"ok": False, "error": "login не сохранён в job", "uses_direct_units": False}
        _delayed_repair_set_status(did, "error", out["error"], out)
        _record_delayed_content_repair(parent_job_id, {"id": did, "status": "error", **out})
        _parent_absorb_child_progress(parent_job_id, f"dcr:{did}", 0, 0, 0, final=True)
        return
    agency = ctx.get("agency") or row.get("agency") or ""
    results_tree = ctx.get("results") or []
    body = ctx.get("body") or {}
    deps = _repair_deps()

    def _live_plan() -> tuple[dict, dict, int, dict]:
        lv = _create_set_live_verification(login, results_tree, agency=agency, use_v5=False)
        pl = (lv or {}).get("repair_plan") or {}
        summ = rgate.summarize_repair_gate(body, results_tree, pl)
        # ВСЕ in-place действия, которые реально исполняет execute_all_in_place (keywords_repair /
        # adprice_repair / images_repair / images_forbidden / content / default_text / promo /
        # callout / rename) = executable_now минус recreate-очередь (recreate/UAC-replace — НЕ in-place,
        # уходят в _auto_queue_recreate_after_done). Раньше cnt считал только content+promo+callout+
        # rename → keywords_repair и adprice_repair НЕ добивались авто (gate inplace_cnt<=0 → break,
        # execute_all_in_place не вызывался) — «поисковые группы без ключей» оставались навсегда.
        cnt = int(summ.get("executable_now") or 0) - int(summ.get("queued_recreate_items") or 0)
        return lv, pl, cnt, summ

    all_executed: list[dict] = []
    all_failed: list[dict] = []
    all_outputs: list[dict] = []
    units_gated: list[dict] = []
    iterations = 0
    last_live: dict = {}
    last_summ: dict = {}
    remaining = 0
    _repair_started = time.time()          # B2: старт бюджета времени repair-джобы
    _budget_exhausted = False
    _nonfixable_stop = False
    try:
        for _ in range(_DELAYED_FULL_REPAIR_MAX_ITERATIONS):
            # B2: бюджет времени исчерпан → выходим ЧИСТО (partial, без reschedule), не давая
            # watchdog-у (1800с) убить джобу. remaining держит последний известный остаток.
            if time.time() - _repair_started > _DELAYED_REPAIR_TIME_BUDGET_SECONDS:
                _budget_exhausted = True
                # Свежий пересчёт remaining: предыдущее значение было взято ДО execute_all_in_place
                # последней итерации → финальный отчёт должен отражать реальное состояние после неё.
                try:
                    last_live, _fp, remaining, last_summ = _live_plan()
                except Exception:  # noqa: BLE001 — best-effort, не сбиваем бюджет-break
                    pass
                break
            live_report, plan, inplace_cnt, last_summ = _live_plan()
            last_live = live_report
            remaining = inplace_cnt         # актуальный остаток (на случай budget-break на след. итерации)
            if inplace_cnt <= 0:
                remaining = 0
                break
            iterations += 1
            # Живой прогресс в note (иначе «повторная Grid-first проверка» висит замороженной
            # 10+ мин и выглядит как зависание) + бамп updated_at защищает от watchdog-а.
            _delayed_repair_set_status(
                did, "running",
                f"авто-добивка: итерация {iterations}, план {inplace_cnt} действ., "
                f"исполнено {len(all_executed)}")
            # post_verify не передаём: цикл сам делает свежую live-сверку через _live_plan()
            # перед следующим проходом и в конце — иначе был бы лишний Grid-запрос на итерацию.
            res = rauto.execute_all_in_place(login, ctx, plan, deps)
            all_executed.extend(res.get("executed_actions") or [])
            all_failed.extend(res.get("failed_actions") or [])
            all_outputs.extend(res.get("results") or [])
            units_gated.extend(res.get("units_gated") or [])
            if not (res.get("executed") or 0):
                # ничего не исполнилось за проход → повторная попытка бессмысленна (anti ping-pong)
                # B1: если ВСЕ провалы — field-ошибки Grid (UNAVAILABLE_FIELD/UNKNOWN_FIELD/…),
                # проблема нечинима in-place: НЕ считаем её остатком и НЕ перепланируем (иначе цикл
                # долбит по кругу на одном флаге до watchdog kill). Иначе — обычная сверка остатка.
                if _repair_failures_nonfixable(res.get("failed_actions") or []):
                    _nonfixable_stop = True
                    remaining = 0
                    break
                last_live, _fp, remaining, last_summ = _live_plan()
                break
        else:
            # исчерпали лимит итераций → финальная сверка остатка
            last_live, _fp, remaining, last_summ = _live_plan()
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": str(e)[:240], "uses_direct_units": False,
               "auto_repair_full": {"executed": all_executed[:40], "failed": all_failed[:20],
                                    "iterations": iterations, "remaining_actions": remaining}}
        _delayed_repair_set_status(did, "error", out["error"], out)
        _record_delayed_content_repair(parent_job_id, {"id": did, "status": "error", **out})
        _record_auto_repair_full(parent_job_id, out["auto_repair_full"])
        _parent_absorb_child_progress(parent_job_id, f"dcr:{did}", 0, 0, 0, final=True)
        return

    # Declarative spec-audit (keyword-shift/images-forbidden/plan⊆slepok) + auto-fix of
    # KEYWORDS_WRONG_GROUP. Runs after the standard in-place actions; failures never break the
    # delayed-repair cycle (best-effort, no Direct create units).
    spec_audit: dict = {}
    _is_post_recreate = (row.get("kind") or "") == "content_repair_post_recreate"
    try:
        spec_audit = _run_spec_audit_and_fix(login, ctx, skip_recreate=_is_post_recreate)
    except Exception as e:  # noqa: BLE001
        spec_audit = {"error": str(e)[:220]}

    # Видео недогружено (still_missing из spec-audit) → считаем это остатком, чтобы «до нуля»
    # reschedule перезапустил докрутку: per-ct breaker + 2 ретрая аплоада добьют видео на следующем
    # цикле, когда Grid догонит edit-view lag. video_no_pool в still_missing НЕ входит (fixable=False)
    # → нечинимый «нет ролика в паке» не создаёт вечный цикл. (Семён 2026-07-08: «чтобы загружалось».)
    _video_still = int((spec_audit.get("video_missing_fix") or {}).get("still_missing_total") or 0)
    if _video_still > 0:
        remaining = int(remaining) + _video_still

    afr = {
        "executed": all_executed[:40],
        "failed": all_failed[:20],
        "iterations": iterations,
        "remaining_actions": int(remaining),
        "units_gated": units_gated[:10],
        "results": all_outputs[:20],
        "spec_audit": spec_audit,
        "budget_exhausted": _budget_exhausted,        # B2
        "nonfixable_stop": _nonfixable_stop,          # B1
    }
    ok = (not all_failed) and int(remaining) == 0
    if not all_executed and not all_failed:
        final_status = "skipped" if int(remaining) == 0 else "partial"
    elif ok:
        final_status = "done"
    else:
        final_status = "partial"
    out = {
        "ok": ok,
        "auto_repair_full": afr,
        "delayed_repair_id": did,
        "parent_job_id": parent_job_id,
        "live_verification": last_live,
        "uses_direct_units": False,
    }
    # Собираем campaigns_fixed из всех sub-fix в spec_audit для правдивого note (ПРАВКА A)
    _sa_fixed = sum(
        int((spec_audit.get(k) or {}).get("campaigns_fixed") or 0)
        for k in (spec_audit or {})
        if isinstance((spec_audit or {}).get(k), dict)
    )
    _delayed_repair_set_status(
        did, final_status,
        (f"авто-добивка: исполнено {len(all_executed)}, остаток {remaining}, итераций {iterations}"
         + (f", spec_audit={_sa_fixed}" if _sa_fixed else "")
         + (" · бюджет времени исчерпан (partial без reschedule)" if _budget_exhausted else "")
         + (" · остаток нечиним in-place (field-ошибка Grid) — reschedule отменён"
            if _nonfixable_stop else "")),
        out,
    )
    _record_delayed_content_repair(parent_job_id, {"id": did, "status": final_status, **out})
    _record_auto_repair_full(parent_job_id, afr)
    # «ДО НУЛЯ» (требование Семёна 2026-07-05): partial с остатком → вернуть ЭТУ ЖЕ строку в
    # waiting — демон прогонит цикл ещё раз (Grid к тому времени догонит edit-view lag).
    # Кап _DELAYED_REPAIR_MAX_RESCHEDULES защищает от вечного цикла на нечинимом остатке.
    # B1/B2: НЕ перепланируем если остаток нечиним (field-ошибки Grid) или исчерпан бюджет времени —
    # это не даёт циклу долбить по кругу и упереться в watchdog kill.
    if (final_status == "partial" and int(remaining) > 0
            and not _nonfixable_stop and not _budget_exhausted):
        _delayed_repair_reschedule(did, row, remaining)
    elif final_status in ("done", "skipped"):
        # Реконсиляция счётчиков карточки (требование Семёна 2026-07-05): после добивки НЕ должно
        # оставаться «создано 13 · ❌ 1», ЕСЛИ ошибок ДЕЙСТВИТЕЛЬНО нет. Только при подтверждённом
        # нуле: live-сверка по кабинету errors=0, in-place остаток 0, очередь пересоздания пуста.
        _reconcile_parent_job_counters(parent_job_id, last_live, last_summ,
                                       login=login, body=body)
        # Требование «в итоге все кампании созданы»: если ЭТА добивка — по джобе-доставке
        # (_requeue_of), и её live-сверка чистая — реконсилируем и ИСХОДНУЮ джобу.
        _rq_parent = str((body or {}).get("_requeue_of") or "").strip()
        if _rq_parent:
            _reconcile_parent_job_counters(_rq_parent, last_live, last_summ,
                                           login=login, body=body)
    # «ДО НУЛЯ» по СОСТАВУ НАБОРА (кейс 2026-07-05: «tp1(куки): partial-кампания удалена —
    # объявления не созданы» — позиция терялась НАВСЕГДА: ни deferred, ни auto-recreate её не
    # подхватывали, live-сверка видит только СОЗДАННЫЕ результаты). Если у родительской джобы
    # failed>0 / created<total — доставляем ОДНОЙ повторной джобой с тем же телом: RESUME-SKIP
    # оркестратора пропустит уже созданные кампании (tp1_rsy — пофидово), создастся только
    # недостающее. Один уровень: джоба-доставка сама внучек не плодит (_requeue_of-гейт).
    _requeue_missing_positions_once(parent_job_id, login, body)
    # Возвращаем родителя в терминальный статус после завершения delayed-repair.
    # Если repair перепланирован (partial + remaining>0) — родитель остаётся running до
    # следующего прохода демона. Во всех остальных случаях (done/skipped/partial-без-остатка/
    # error/исключение — они handled выше через return) — убираем dcr:{did} из _active_children;
    # если active пусто → карточка снова «done» (статус/done=total/finished_at).
    if not (final_status == "partial" and int(remaining) > 0):
        _parent_absorb_child_progress(parent_job_id, f"dcr:{did}", 0, 0, 0, final=True)


def _run_delayed_finalize(row: dict) -> None:
    """Задача F: REPLAY захваченной Grid-финализации набора (kind='finalize_set').

    Демон-путь: те же функции _finalize_rsya/_finalize_search_via_grid, что и инлайн — «ровно
    тот же набор Grid-операций», только вне цикла создания. Идемпотентно (UpdateCampaigns теми
    же значениями). remaining>0 → reschedule (attempts cap). done → карточка снова терминальна
    (закрываем child fin:{did}) + снимаем finalize_pending. Баллы Директа НЕ тратит."""
    did = (row.get("id") or "").strip()
    parent_job_id = (row.get("parent_job_id") or "").strip()
    _delayed_repair_set_status(did, "running", "async-финализация: replay Grid-финализаций")
    try:
        out = _finalize_queue_module().run_finalize_job(row)
    except Exception as e:  # noqa: BLE001 — весь replay best-effort, карточку не вешаем
        out = {"ok": False, "error": str(e)[:240], "remaining": 1, "uses_direct_units": False}
    remaining = int(out.get("remaining") or 0)
    ok = bool(out.get("ok")) and remaining == 0
    final_status = "done" if ok else ("partial" if remaining > 0 else "error")
    _delayed_repair_set_status(
        did, final_status,
        f"async-финализация: применено {out.get('applied', 0)}/{out.get('total', 0)}, "
        f"остаток {remaining}",
        out)
    _record_delayed_content_repair(parent_job_id, {"id": did, "status": final_status,
                                                   "kind": "finalize_set", **out})
    # reschedule до нуля (attempts cap защищает от вечного цикла на нечинимом остатке).
    _rescheduled = False
    if final_status == "partial" and remaining > 0:
        _rescheduled = _delayed_repair_reschedule(did, row, remaining)
    if not _rescheduled:
        # Терминал (done/error или исчерпан лимит reschedule): снимаем finalize_pending и
        # закрываем child → карточка снова терминальна. При error оставляем в result отметку.
        try:
            def _clear_pending(job, result):
                if isinstance(result.get("finalize_pending"), dict):
                    result["finalize_finished"] = {"status": final_status,
                                                    "applied": out.get("applied", 0),
                                                    "remaining": remaining}
                    result.pop("finalize_pending", None)
                    return True
                return False
            _parent_update(parent_job_id, _clear_pending)
        except Exception:  # noqa: BLE001
            pass
        _parent_absorb_child_progress(parent_job_id, f"fin:{did}", 0, 0, 0, final=True)


def _delayed_repair_daemon_loop(app) -> None:
    import psycopg2.extras
    while True:
        # ПРАВКА P2: watchdog — строки в status='running' дольше порога → помечать failed.
        # Только реально просроченные по updated_at (активная строка обновляется set_status).
        _wd_failed_finalize: list[tuple] = []
        _wd_failed_content: list[tuple] = []
        try:
            _wconn = _victory_conn_rw()
            try:
                _wcur = _wconn.cursor()
                _wcur.execute("""
                    UPDATE public.direct_delayed_repairs
                       SET status='failed',
                           note='watchdog: stuck running >' || %s || ' мин',
                           updated_at=now()
                     WHERE status='running'
                       AND updated_at < now() - (%s || ' seconds')::interval
                    RETURNING id, parent_job_id, kind
                """, (str(_DELAYED_REPAIR_STUCK_TIMEOUT_SECONDS // 60),
                      str(_DELAYED_REPAIR_STUCK_TIMEOUT_SECONDS)))
                _wd_all = _wcur.fetchall() or []
                _wd_failed_finalize = [(r[0], r[1]) for r in _wd_all
                                       if (r[2] or "") == "finalize_set"]
                _wd_failed_content = [(r[0], r[1]) for r in _wd_all
                                      if (r[2] or "").startswith("content_repair")]
                _wconn.commit()
            finally:
                _wconn.close()
        except Exception:  # noqa: BLE001
            pass
        # Задача F (DIRECT_ASYNC_FINALIZE): watchdog пометил застрявшую finalize-строку failed, но
        # child fin:{did} остаётся ОТКРЫТ (не пройден терминальный путь _run_delayed_finalize) →
        # карточка вечно «running» с невыставленными инвариантами (Карты OFF / места показа #3-#6).
        # Закрываем child как терминальный + снимаем finalize_pending (тот же терминал, что в
        # _run_delayed_finalize:2018-2032) — иначе набор виснет навсегда. Строки kind='finalize_set'
        # существуют ТОЛЬКО при DIRECT_ASYNC_FINALIZE=ON (создаются capture-путём) → при OFF список
        # пуст, no-op (нормальный dcr-путь не трогаем).
        if _wd_failed_finalize:
            def _clear_pending_wd(job, result):
                if isinstance(result.get("finalize_pending"), dict):
                    result["finalize_finished"] = {"status": "failed",
                                                    "note": "watchdog: stuck running"}
                    result.pop("finalize_pending", None)
                    return True
                return False
            for _fdid, _fparent in _wd_failed_finalize:
                if not _fparent:
                    continue
                try:
                    _parent_update(_fparent, _clear_pending_wd)
                except Exception:  # noqa: BLE001
                    pass
                _parent_absorb_child_progress(_fparent, f"fin:{_fdid}", 0, 0, 0, final=True)
        # К1 (2026-07-09): watchdog пометил застрявшую content_repair-строку failed (напр. spec_audit-
        # фиксер завис на мёртвом M3 до фикса idle/circuit-breaker — тогда весь delayed-repair
        # цикл вис, строка не доходила до терминала), но child dcr:{did} остаётся ОТКРЫТ →
        # карточка вечно «running» (осиротевший delayed-repair). Закрываем child как терминальный
        # (тот же вызов, что все терминальные ветки _run_delayed_content_repair:
        # _parent_absorb_child_progress final=True) + фиксируем провал в result-хвосте. Иначе
        # delayed content_repair не доходит до терминала. content_repair_post_recreate покрыт
        # startswith. finalize_set сюда НЕ попадает (обработан выше отдельным блоком).
        if _wd_failed_content:
            for _cdid, _cparent in _wd_failed_content:
                if not _cparent:
                    continue
                try:
                    _record_delayed_content_repair(_cparent, {
                        "id": _cdid, "status": "failed", "uses_direct_units": False,
                        "error": ("watchdog: content_repair stuck running >"
                                  f"{_DELAYED_REPAIR_STUCK_TIMEOUT_SECONDS // 60} мин без прогресса")})
                except Exception:  # noqa: BLE001
                    pass
                _parent_absorb_child_progress(_cparent, f"dcr:{_cdid}", 0, 0, 0, final=True)
        rows = []
        try:
            conn = _victory_conn_rw()
            try:
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute("""
                    SELECT * FROM public.direct_delayed_repairs
                     WHERE status='waiting' AND run_at <= now()
                     ORDER BY run_at LIMIT 3
                """)
                rows = cur.fetchall()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            rows = []
        for row in rows:
            try:
                if (row.get("kind") or "") == "finalize_set":
                    _run_delayed_finalize(row)               # Задача F: async-финализация
                else:
                    _run_delayed_content_repair(row)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(_DELAYED_REPAIR_POLL)


def _ensure_delayed_repair_daemon(app) -> None:
    with _CREATE_JOBS_LOCK:
        if _DELAYED_REPAIR_DAEMON["started"]:
            return
        _DELAYED_REPAIR_DAEMON["started"] = True
    _delayed_repair_db_init()
    threading.Thread(target=_delayed_repair_daemon_loop, args=(app,), daemon=True).start()


def _resume_one_deferred(app, row) -> None:
    """Докрутить один остаток ПО КУКЕ (без баллов): поставить новую джобу с via_cookie=True.
    152 = автоматический переход на куки, поэтому ждать сброса баллов НЕ нужно — Grid/UAC создают
    черновики без units. Дубля нет: set_plan пропустит уже созданные кампании."""
    did = row["id"]
    login = row.get("login") or ""
    body = row.get("body") or {}
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:  # noqa: BLE001
            _deferred_set_status(did, "error", "битый body"); return
    items = body.get("items") or []
    if not items:
        _deferred_set_status(did, "done", "нет пунктов"); return
    # Агентство для партиционирования очереди (по куке units не нужны — баллы НЕ проверяем).
    _tok, ag = _token_for_login(login, row.get("agency") or "", _direct_tokens())
    body["_resume_count"] = int(row.get("resume_count") or 0) + 1
    body["agency"] = ag or body.get("agency") or row.get("agency") or ""
    # _resume_via_token: пункты, которые в принципе НЕ создать по куке (NO_BRAND_SEGMENTS_AVAILABLE —
    # сегментный tp5 требует M3/токен) — сохранены с этим флагом; куку им НЕ навязываем.
    if body.get("_resume_via_token"):
        # Токен-докрутку СТАВИМ В ОЧЕРЕДЬ ТОЛЬКО когда есть И токен, И баллы. Иначе воркер уйдёт на
        # cookie-путь (пустой токен ИЛИ preflight-152 форсит via_cookie) → NO_BRAND → self-reference-
        # дедуп → финал гасит строку в done → сегментный tp5 теряется (инцидент 08.07 721641cad7c1 /
        # job 23677e1473d1, porg-psm5h7q6). Нет кредов → НЕ ставим джобу, оставляем строку waiting с
        # бэкоффом; демон повторит. Строка НЕ будет помечена done несуществующим финалом джобы.
        if not _tok:
            _deferred_bump_resume_at(did, 1)
            _deferred_set_status(did, "waiting",
                                 "токен-докрутка сегментного tp5 ждёт агентский токен (не найден) — повтор через 1ч")
            return
        _alive = _units_alive_for_login(login, ag or "")
        if _alive is False:
            from datetime import datetime, timezone
            _now = datetime.now(timezone.utc)
            _reset = _next_units_reset_utc()
            _secs = (_reset - _now).total_seconds()
            _hrs = max(1, int(_secs // 3600) + (1 if _secs % 3600 else 0))
            _deferred_bump_resume_at(did, _hrs)
            _deferred_set_status(did, "waiting",
                                 f"токен есть, баллы Директа исчерпаны — ждём сброс ({_reset.isoformat()})")
            return
        # токен + баллы есть → добиваем ТОКЕНОМ (via_cookie НЕ ставим: сегментный tp5 пойдёт API-путём)
    else:
        body["via_cookie"] = True                          # докрутка ПО КУКЕ (без баллов) — не ждём полночь
    body["_deferred_id"] = did                             # финал джобы пометит остаток done (анти-цикл)
    # Семён 2026-07-06: добивка — сразу (не в конец очереди) и без НОВОЙ карточки; _resume_of →
    # воркер вольёт created/failed докрутки в родительскую джобу (row["job_id"] = исходная джоба).
    body["_resume_of"] = row.get("job_id")
    sess = {"logged_in": True, "is_admin": True, "_resume": True}   # системная докрутка — авторизована заранее
    try:
        _ensure_create_worker(app)
        jid = _job_new(len(items), login, body, sess, priority=True)
        body["_job_id"] = jid                              # как в api_create_set_async: воркер-путь + прогресс джобы
        _path = "токеном" if body.get("_resume_via_token") else "по куке"
        _deferred_set_status(did, "resumed", f"докрутка {_path} #{body['_resume_count']} поставлена в очередь (приоритет)")
    except Exception as e:  # noqa: BLE001
        _deferred_bump_resume_at(did, 1)
        _deferred_set_status(did, "waiting", f"ошибка постановки: {str(e)[:120]}")


def _deferred_enqueue_now(app, did: str) -> tuple | None:
    """On-demand: поставить остаток отложенного набора в ОЧЕРЕДЬ СЕЙЧАС (кнопка «создать через
    куки») — БЕЗ ожидания сброса баллов и БЕЗ units-гейта (пользователь явно выбрал «сейчас»).
    По куке Мастер/Товарка создадутся без баллов; текстовые/РСЯ при 152 снова уйдут на докрутку.
    → (jid, total, login, agency) | None."""
    import psycopg2.extras
    row = None
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM public.direct_deferred_creates WHERE id=%s", (did,))
            row = cur.fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None
    if not row:
        return None
    login = row.get("login") or ""
    body = row.get("body") or {}
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:  # noqa: BLE001
            return None
    items = body.get("items") or []
    if not items:
        _deferred_set_status(did, "done", "нет пунктов"); return None
    body["_resume_count"] = int(row.get("resume_count") or 0) + 1
    ag = row.get("agency") or body.get("agency") or ""
    body["agency"] = ag                                   # ключ партиционирования очереди
    # По куке ЭТИ пункты создать нельзя (см. _resume_via_token в _resume_one_deferred) — кнопка
    # «сейчас» тут бессильна раньше сброса баллов, поэтому куку им не навязываем (тот же отказ).
    if not body.get("_resume_via_token"):
        body["via_cookie"] = True                         # ЯВНОЕ согласие пользователя (попап) → token-типы по куке
    body["_deferred_id"] = did                            # финал джобы пометит остаток done (анти-цикл)
    body["_resume_of"] = row.get("job_id")                # → воркер вольёт created/failed в родительскую джобу
    sess = {"logged_in": True, "is_admin": True, "_resume": True}   # системная докрутка — авторизована
    _ensure_create_worker(app)
    jid = _job_new(len(items), login, body, sess, priority=True)   # _job_new сам проставит body["_job_id"]
    _deferred_set_status(did, "resumed", "запущено вручную (куки/сейчас) — поставлено в очередь (приоритет)")
    return jid, len(items), login, ag


def _resume_daemon_loop(app) -> None:
    """Фоновый демон: раз в ~10 мин докручивает остатки, у которых наступил resume_at и есть баллы."""
    import psycopg2.extras
    while True:
        rows = []
        try:
            conn = _victory_conn_rw()
            try:
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute("SELECT * FROM public.direct_deferred_creates "
                            "WHERE status='waiting' AND resume_at <= now() ORDER BY resume_at LIMIT 5")
                rows = cur.fetchall()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            rows = []
        for row in rows:
            try:
                _resume_one_deferred(app, row)
            except Exception:  # noqa: BLE001
                pass
        try:
            _jobs_purge_old()                            # бэкстоп-чистка истории джоб (память+БД)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(_RESUME_POLL)


def _ensure_resume_daemon(app) -> None:
    """Лениво поднимает демон авто-докрутки (1 раз)."""
    with _CREATE_JOBS_LOCK:
        if _RESUME_DAEMON["started"]:
            return
        _RESUME_DAEMON["started"] = True
    _deferred_db_init()
    threading.Thread(target=_resume_daemon_loop, args=(app,), daemon=True).start()


def _job_kind(body: dict | None) -> str:
    b = body or {}
    if b.get("_kind") == "delete_drafts":
        return "delete_drafts"
    if b.get("_kind") == "copy_campaigns":
        return "copy_campaigns"
    if b.get("content_source") == "slepok_library":
        return "slepok"
    return "set"


def _job_new_web(total: int, login: str, body: dict, saved_session: dict,
                 dedup_login: bool, priority: bool = False) -> str:
    """web-роль: постановка джобы ТОЛЬКО в БД (status='queued', _web_posted=true, session в body).
    Воркер-процесс заберёт её клеймом из БД. In-memory очередь web-процесса не используется.

    priority=True — добивка/доставка остатка: body['_priority']=true → воркер клеймит такие
    джобы РАНЬШЕ обычных (см. _worker_claim_web_jobs) и ставит в НАЧАЛО in-memory очереди
    (_worker_adopt_job). Семён 2026-07-06: «добивка сразу, а не в конец очереди»."""
    if dedup_login:
        existing = _job_db_active_by_login(login)
        if existing:
            if body is not None:
                body["_job_id"] = existing
            return existing
    jid = uuid.uuid4().hex[:12]
    if body is not None:
        body["_job_id"] = jid
        body["_web_posted"] = True                       # маркер: поллер воркера забирает только такие
        if priority:
            body["_priority"] = True                     # добивка: клейм и очередь — впереди обычных
        body["_session_snapshot"] = dict(saved_session or {})   # нужен для test_request_context в воркере
    job = {"status": "queued", "login": login, "done": 0,
           "total": int(total), "created": 0, "failed": 0,
           "set_done": 0, "set_total": int(total),
           "result": None, "error": None, "cancel": False,
           "kind": _job_kind(body), "publish": bool((body or {}).get("launch")),
           "stream_content": bool((body or {}).get("stream_content")),
           "step": None, "_id": jid, "body": body,
           "session": None, "agency": (body or {}).get("agency")}
    _job_db_save(jid, job)                                # INSERT: пишет body (с session+маркерами)+agency
    if dedup_login:   # дедуп не сработал (старая джоба уже terminal) → это ПЕРЕЗАПУСК на тот же login
        _supersede_delayed_repairs_for_login(login)
    return jid


def _job_new(total: int, login: str, body: dict, saved_session: dict,
             dedup_login: bool = False, priority: bool = False) -> str:
    """Регистрирует джобу в статусе 'queued' и ставит её в глобальную очередь.

    dedup_login=True (пользовательский submit) — АТОМАРНЫЙ дедуп: если по этому логину уже есть
    НЕзавершённая джоба (queued/running), второй джоб НЕ создаём, а возвращаем существующий job_id.
    Проверка+вставка под ОДНИМ _CREATE_JOBS_LOCK → закрывает гонку двух сабмитов подряд (TOCTOU:
    раньше эндпоинт сканировал и ОТПУСКАЛ лок до _job_new, два запроса успевали вставить обе копии).
    Внутренние постановки (докрутка/resume/delete_drafts) идут с dedup_login=False (намеренные).

    priority=True — докрутка/остаток (152, resume): встаёт В НАЧАЛО очереди, а не в конец
    (Семён 2026-07-06: «добивка сразу, а не в конец очереди»), НЕ ждёт своей очереди за новыми
    наборами. web-роль: приоритет уезжает в БД флагом body['_priority'] (см. _job_new_web).

    web-роль: НЕ трогаем in-memory очередь — джоба уходит только в БД (её заберёт worker-процесс)."""
    if _direct_role() == "web":
        return _job_new_web(total, login, body, saved_session, dedup_login, priority)
    jid = uuid.uuid4().hex[:12]
    with _CREATE_JOBS_LOCK:
        if dedup_login:
            _login = (login or "").strip()
            for _ejid, _ej in _CREATE_JOBS.items():
                if _ej.get("status") not in _JOB_TERMINAL and (_ej.get("login") or "").strip() == _login:
                    if body is not None:
                        body["_job_id"] = _ejid           # прогресс/отмена смотрят на СУЩЕСТВУЮЩУЮ джобу
                    return _ejid                          # дубль не создаём — отдаём активный job_id
        # _job_id ДОЛЖЕН быть в body ДО notify (и под этим же локом): иначе воркер (его будит
        # _CREATE_COND.notify ниже) успевает забрать body и сериализовать его в JSON ДО того, как
        # вызывающий код проставит body["_job_id"] → внутри create_set _job=None → прогресс/счётчик
        # «создано K из N» застывает на 0, хотя кампании реально создаются (гонка). Ставим здесь.
        if body is not None:
            body["_job_id"] = jid
        _is_stream = bool((body or {}).get("stream_content"))
        job = {"status": "queued", "login": login, "done": 0,
               "total": int(total), "created": 0, "failed": 0,
               "set_done": 0, "set_total": int(total),
               "result": None, "error": None, "cancel": False,
               "kind": _job_kind(body),
               "publish": bool((body or {}).get("launch")),
               "stream_content": _is_stream,   # stream=True → фаза generating перед creating
               "step": None,                   # текущая фаза: None/generating/creating (только при stream)
               "_id": jid, "body": body, "session": saved_session,
               "_heartbeat": time.time()}
        _CREATE_JOBS[jid] = job
        if priority:
            _CREATE_QUEUE.insert(0, jid)
        else:
            _CREATE_QUEUE.append(jid)
        # лёгкая чистка СТАРЫХ ЗАВЕРШЁННЫХ джоб (активные/очередь не трогаем), держим ~40
        terminal = [k for k, v in _CREATE_JOBS.items() if v["status"] in _JOB_TERMINAL]
        if len(terminal) > 40:
            for old in terminal[:-40]:
                _CREATE_JOBS.pop(old, None)
                _JOB_DB_LAST.pop(old, None)
        _CREATE_COND.notify()
    _job_db_save(jid, job)                                # серверная персистентность (видна с любого устройства)
    if dedup_login:   # дедуп не сработал (старая джоба уже terminal) → это ПЕРЕЗАПУСК на тот же login
        _supersede_delayed_repairs_for_login(login)
    return jid


def _create_jobs_ahead(jid: str) -> int:
    """Сколько джоб впереди (выполняется + ждут раньше в очереди) — для «в очереди, перед вами N»."""
    running = sum(1 for v in _CREATE_JOBS.values() if v["status"] == "running")
    try:
        idx = _CREATE_QUEUE.index(jid)
    except ValueError:
        return 0
    return running + idx


# ── Кросс-процессный per-agency гейт (create-worker ↔ copy-worker) ──────────────────
# _CREATE_ACTIVE_AGENCIES — in-memory В КАЖДОМ процессе, потому direct-worker (create) и
# direct-copy (copy) НЕ координируются → create+copy одного агентства жгут куки/API Яндекса
# параллельно (152, инвалидация кук). Слот агентства держим в БД (одна строка на кластер).
# FAIL-OPEN: ЛЮБОЙ сбой БД → ведём себя как раньше (не блокируем) — гейт не может сломать пайплайн.
def _agency_gate_claim(agency: str, job_id: str) -> bool:
    """Занять кросс-процессный слот агентства. True = слот наш / не применимо; False = занят другим процессом."""
    if not agency:                                    # пустой ключ агентства — не гейтим (как in-memory)
        return True
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO public.direct_agency_active (agency, job_id, started_at) "
                "VALUES (%s, %s, now()) ON CONFLICT (agency) DO NOTHING RETURNING agency",
                (agency, job_id))
            got = cur.fetchone() is not None
            conn.commit()
            return got
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — FAIL-OPEN
        print(f"[agency-gate] claim fail-open ({agency}): {str(e)[:120]}", flush=True)
        return True


def _agency_gate_release(agency: str, job_id: str) -> None:
    """Освободить СВОЙ слот агентства (идемпотентно, только своя job_id). FAIL-OPEN."""
    if not agency:
        return
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM public.direct_agency_active WHERE agency=%s AND job_id=%s",
                        (agency, job_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        print(f"[agency-gate] release fail-open ({agency}): {str(e)[:120]}", flush=True)


def _agency_gate_sweep() -> None:
    """Backstop (из watchdog): освободить слоты, чей job больше не running/claimed
    (терминальный/пропал/краш процесса — после того как watchdog пометил его interrupted)."""
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM public.direct_agency_active a WHERE NOT EXISTS ("
                "  SELECT 1 FROM public.direct_automation_jobs j "
                "   WHERE j.job_id = a.job_id AND j.status IN ('running','claimed'))")
            conn.commit()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        print(f"[agency-gate] sweep fail-open: {str(e)[:120]}", flush=True)


def _claim_next_job():
    """Берёт из очереди следующую джобу, если по агентству ещё не достигнут лимит параллельности.
    Ждёт, если очередь пуста ИЛИ все доступные джобы упёрлись в лимит агентства.
    Возвращает (jid, job, body, saved) и увеличивает счётчик агентства. Снятые отмены — пропускает."""
    with _CREATE_COND:
        while True:
            if _CREATE_DRAIN.get("on"):
                return None                               # drain: воркер завершает работу (worker_main SIGTERM)
            pick = None
            for i, q_jid in enumerate(_CREATE_QUEUE):
                q_job = _CREATE_JOBS.get(q_jid)
                if q_job is None:
                    _CREATE_QUEUE.pop(i)
                    pick = "retry"; break
                if q_job.get("cancel"):                   # отменили, пока ждал в очереди
                    _CREATE_QUEUE.pop(i)
                    q_job["status"] = "cancelled"; q_job["finished_at"] = time.time()
                    _job_db_save(q_jid, q_job, full=True)
                    pick = "retry"; break
                active = _CREATE_ACTIVE_AGENCIES.get(_job_agency(q_job), 0)
                if active >= _CREATE_MAX_PER_AGENCY:
                    continue                              # лимит по агентству исчерпан (в этом процессе) — ждёт
                # Кросс-процессный гейт: агентство может быть занято ДРУГИМ процессом (copy↔create) —
                # тогда не берём, ждём (не жжём куки/баллы одного агентства параллельно). FAIL-OPEN внутри.
                if not _agency_gate_claim(_job_agency(q_job), q_jid):
                    continue
                # подходит: по агентству есть свободный слот (и локально, и кросс-процессно)
                _CREATE_QUEUE.pop(i)
                q_job["status"] = "running"
                q_job["started_at"] = time.time()         # старт прогона — для «ушло времени» в итоге
                _job_touch(q_job)
                _CREATE_ACTIVE_AGENCIES[_job_agency(q_job)] = active + 1
                return q_jid, q_job, q_job["body"], q_job["session"]
            if pick == "retry":
                continue                                  # снятую/битую убрали — пересканируем
            _CREATE_COND.wait()                           # нечего брать (пусто или агентства заняты)


def _create_worker_loop(app):
    """Worker пула создания: параллелит аккаунты, но держит лимит на агентство.
    После УСПЕШНОГО полного аккаунта — пауза _CREATE_POOL_PAUSE сек."""
    while True:
        claimed = _claim_next_job()
        if claimed is None:                               # drain (SIGTERM воркеру): завершаем тред
            return
        jid, job, body, saved = claimed
        agency = _job_agency(job)
        final_status = "error"
        # Задача F (DIRECT_ASYNC_FINALIZE): открыть окно захвата финализации набора (по login).
        # OFF → register вернёт None (no-op). Снятие — в finally (гарантированно, даже при падении).
        _fin_login = str((body or {}).get("login") or "").strip()
        _finalize_queue_module().register(_fin_login, jid, agency)   # окно захвата (OFF → no-op)
        try:
            _job_touch(job)
            _job_db_save(jid, job)                        # → 'running' в БД
            # Дочерняя добивка (докрутка/доставка/recreate) стартовала → родитель снова «в работе»:
            # его total растёт на объём добивки, прогресс-бар был 100% → снижается (Семён 2026-07-07).
            _parent_ref = _child_parent_ref(body)
            if _parent_ref and _parent_ref != jid:
                _parent_absorb_child_start(_parent_ref, jid, int(job.get("total") or 0))
            # сам прогон — ВНЕ lock'а (долгий), прогресс джоба обновляет по ссылке внутри ядра
            if (body or {}).get("_kind") == "delete_drafts":
                # Удаление черновиков в ОБЩЕЙ очереди — то же ядро, что и синхронный эндпоинт,
                # но с прогрессом джобы (карточка показывает «удалено N · обработка набора N/M»).
                try:
                    data = _delete_drafts_core(body.get("login", ""), body.get("agency", ""), job=job)
                except Exception as e:  # noqa: BLE001
                    data = {"error": str(e)[:300]}
            elif (body or {}).get("_kind") == "copy_campaigns":
                try:
                    _copy_run_job(jid, body)
                    with _COPY_JOBS_LOCK:
                        cj = dict(_COPY_JOBS.get(jid) or {})
                    data = cj.get("result") if cj.get("status") == "done" else {"error": cj.get("error") or "копирование не завершилось"}
                except Exception as e:  # noqa: BLE001
                    data = {"error": str(e)[:300]}
            else:
                with app.test_request_context("/direct/api/create_set", method="POST", json=body):
                    try:
                        session.update(saved)                 # _direct_access увидит права
                        resp = _create_set_response()
                        obj = resp[0] if isinstance(resp, tuple) else resp
                        data = obj.get_json(silent=True) if hasattr(obj, "get_json") else None
                        if data is None:                      # редирект/HTML (нет прав) → честная ошибка
                            data = {"error": "фоновое создание не выполнено (нет JSON-ответа; проверьте права/сессию)"}
                    except Exception as e:  # noqa: BLE001
                        import traceback as _tb
                        print(f"[worker-tb] {_tb.format_exc()}", flush=True)
                        data = {"error": str(e)[:300]}
            _job_final = None
            with _CREATE_JOBS_LOCK:
                j = _CREATE_JOBS.get(jid)
                if j is not None:
                    if j.get("_watchdog_done"):
                        final_status = j["status"]
                        _job_final = dict(j)
                        j = None
                if j is not None:
                    j["result"] = data
                    if data:
                        j["created"] = data.get("created", j["created"])
                        j["failed"] = data.get("failed", j["failed"])
                    if j.get("cancel"):                   # отмена во время прогона (стоп после тек. кампании)
                        j["status"] = "cancelled"
                    elif (data or {}).get("error"):
                        j["status"] = "error"; j["error"] = data.get("error")
                    else:
                        j["status"] = "done"; j["done"] = j["total"]
                    # «Сколько ушло времени» — от старта прогона до терминала (сек). Кладём и в result,
                    # чтобы итоговый баннер показал длительность даже после рестарта (хранится в result jsonb).
                    if j.get("started_at"):
                        _el = max(0, int(time.time() - j["started_at"]))
                        j["elapsed"] = _el
                        if isinstance(data, dict):
                            data.setdefault("elapsed_seconds", _el)
                    _job_touch(j)
                    j["finished_at"] = time.time()         # момент завершения → карточка уйдёт через TTL
                    final_status = j["status"]
                    _job_final = dict(j)                   # снимок под lock'ом для DB-записи вне lock'а
            if _job_final is not None:
                _job_db_save(jid, _job_final, full=True)   # финальный статус + result в БД
                _ready_logins_track(jid, _job_final)       # вкладка «Готовые логины» (add/remove)
                _merge_resume_into_parent(jid, _job_final, body)
                if final_status == "done":
                    auto_queued = _auto_queue_recreate_after_done(jid, _job_final)
                    delayed_content = _schedule_delayed_content_repair_after_done(jid, _job_final)
                    # Задача F: захваченные финализации → очередь finalize_set. Пока не докручены,
                    # набор НЕ готов: держим карточку «running» (child fin:{did}) + finalize_pending
                    # в result (summary не зелёный). Демон REPLAY-нёт → реконсиляция → зелёный.
                    _finalize_enqueued = None
                    _finalize_inline = None
                    try:
                        _rec = _finalize_queue_module().unregister(_fin_login) if _fin_login else None
                        if _rec is not None and _rec.specs:
                            _finalize_enqueued = _finalize_queue_module().enqueue(
                                jid, _fin_login, agency, _rec.specs)
                            if _finalize_enqueued:
                                _parent_absorb_child_start(jid, f"fin:{_finalize_enqueued}", 0)
                            else:
                                # enqueue вернул None (ошибка БД / нет коннекта / ON CONFLICT): захваченную
                                # финализацию НЕ терять — в синхронном пути она бы отработала. Inline-replay
                                # ТЕМИ ЖЕ функциями, что delayed-демон (run_finalize_job → finalize_rsya/
                                # finalize_search_via_grid), синхронно здесь. Идемпотентно (finalize —
                                # UpdateCampaigns одними значениями). remaining>0 → ниже пометим finalize_pending.
                                _finalize_inline = _finalize_queue_module().run_finalize_job(
                                    {"result": {"specs": _rec.specs}})
                                print(f"[finalize-queue] enqueue=None → inline-replay {_fin_login}: "
                                      f"applied={_finalize_inline.get('applied')} "
                                      f"remaining={_finalize_inline.get('remaining')}", flush=True)
                    except Exception as _fe:  # noqa: BLE001 — постановка finalize best-effort
                        print(f"[finalize-queue] done-enqueue {_fin_login}: {str(_fe)[:200]}", flush=True)
                    post_done_changed = False
                    if auto_queued:
                        with _CREATE_JOBS_LOCK:
                            j = _CREATE_JOBS.get(jid)
                            if j is not None and isinstance(j.get("result"), dict):
                                j["result"]["auto_queued_repair"] = auto_queued
                                _lv = j["result"].get("live_verification")
                                if isinstance(_lv, dict):
                                    _rp = _lv.get("repair_plan")
                                    if isinstance(_rp, dict):
                                        _rp["status"] = "resolved"
                                        _rp["resolved_by"] = auto_queued.get("job_id", "")
                                _job_touch(j)
                                _job_final = dict(j)
                                post_done_changed = True
                    if delayed_content:
                        with _CREATE_JOBS_LOCK:
                            j = _CREATE_JOBS.get(jid)
                            if j is not None and isinstance(j.get("result"), dict):
                                j["result"]["delayed_content_repair_scheduled"] = delayed_content
                                _job_touch(j)
                                _job_final = dict(j)
                                post_done_changed = True
                    if _finalize_enqueued:
                        # DoD: набор ещё не финализирован → summary НЕ зелёный (finalize_pending).
                        with _CREATE_JOBS_LOCK:
                            j = _CREATE_JOBS.get(jid)
                            if j is not None and isinstance(j.get("result"), dict):
                                j["result"]["finalize_pending"] = {
                                    "delayed_repair_id": _finalize_enqueued,
                                    "specs": len(_rec.specs) if _rec else 0,
                                }
                                _job_touch(j)
                                _job_final = dict(j)
                                post_done_changed = True
                    if _finalize_inline is not None and _finalize_inline.get("remaining"):
                        # Inline-replay (enqueue вернул None) отработал ЧАСТИЧНО → набор финализирован
                        # не полностью: summary НЕ зелёный, помечаем finalize_pending + ошибку, чтобы
                        # повторный проход/ручная докрутка это подобрали (не выдаём невыполненную
                        # финализацию за успех). remaining==0 → всё применено inline, зелёный корректен.
                        with _CREATE_JOBS_LOCK:
                            j = _CREATE_JOBS.get(jid)
                            if j is not None and isinstance(j.get("result"), dict):
                                j["result"]["finalize_pending"] = {
                                    "inline_replay": True,
                                    "applied": _finalize_inline.get("applied", 0),
                                    "remaining": _finalize_inline.get("remaining", 0),
                                    "failed": _finalize_inline.get("failed", []),
                                    "error": "enqueue finalize вернул None; inline-replay выполнен частично",
                                }
                                _job_touch(j)
                                _job_final = dict(j)
                                post_done_changed = True
                    if post_done_changed and _job_final is not None:
                        _job_db_save(jid, _job_final, full=True)
        finally:
            # Задача F: гарантированно закрыть окно захвата (при error/cancel done-блок не отработал →
            # иначе recorder висит в реестре и глотает финализацию следующего набора того же login).
            # Идемпотентно: если done-блок уже снял — pop вернёт None.
            if _fin_login:
                try:
                    _finalize_queue_module().unregister(_fin_login)
                except Exception:  # noqa: BLE001
                    pass
            # освобождаем слот агентства и будим пул
            with _CREATE_COND:
                active = max(0, int(_CREATE_ACTIVE_AGENCIES.get(agency, 0)) - 1)
                if active:
                    _CREATE_ACTIVE_AGENCIES[agency] = active
                else:
                    _CREATE_ACTIVE_AGENCIES.pop(agency, None)
                _CREATE_COND.notify_all()
            _agency_gate_release(agency, jid)             # кросс-процессный слот (вне _CREATE_COND — DB I/O не держит лок)
        if final_status == "done":                        # пауза ТОЛЬКО после успешного полного аккаунта
            time.sleep(_CREATE_POOL_PAUSE)


def _create_workers_count() -> int:
    """Количество worker'ов = число известных агентств, минимум 2."""
    try:
        n = len([k for k in (_direct_tokens() or {}).keys() if str(k).strip()])
    except Exception:  # noqa: BLE001
        n = 0
    return max(2, n or 2)


def _ensure_create_worker(app):
    """Лениво поднимает ПУЛ воркеров (при первом async-запросе):
    инициализирует таблицу персистентности и поднимает недавние джобы из БД (для просмотра).

    web-роль: воркеры/демоны/recover НЕ стартуем (их держит worker-процесс). Делаем только
    _jobs_db_init — чтобы таблица и колонка control существовали для постановки/статуса/команд.
    recover в web-роли ЗАПРЕЩЁН: он бы пометил web-posted queued-джобы interrupted и убил очередь."""
    with _CREATE_JOBS_LOCK:
        if _CREATE_WORKER["started"]:
            return
        _CREATE_WORKER["started"] = True
    _jobs_db_init()
    if _direct_role() == "web":
        return                                            # web: только схема БД, никаких фоновых тредов
    # СТОРОННИЙ процесс (ручной скрипт/агент, импортировавший blueprint БЕЗ явной роли и вне
    # systemd) НЕ должен выполнять recover и поднимать воркеров/демонов: его recover помечал
    # running-джобы ЖИВОГО воркера 'interrupted' и рвал прогоны (кейс 2026-07-06: контроль №2
    # 53fd086ef597 прерван скриптом с ролью-дефолтом 'all'). Признак сервиса — systemd
    # INVOCATION_ID или явно выставленный DIRECT_ROLE.
    if not os.environ.get("DIRECT_ROLE") and not os.environ.get("INVOCATION_ID"):
        return
    _jobs_db_recover()
    _ensure_create_watchdog()
    _create_watchdog_tick()
    workers = int(_CREATE_WORKERS or _create_workers_count())
    for _ in range(workers):                              # параллельно по разным агентствам
        threading.Thread(target=_create_worker_loop, args=(app,), daemon=True).start()
    _ensure_resume_daemon(app)                            # демон авто-докрутки остатка после сброса баллов
    _ensure_delayed_repair_daemon(app)                    # guarded content repair после Grid lag


def _ensure_copy_worker(app):
    """Воркер-пул отдельного copy-сервиса (direct-copy.service). Владеет ТОЛЬКО copy_campaigns
    в собственной in-memory очереди этого процесса.

    Умышленно НЕ поднимает create-set инфраструктуру: НЕТ _jobs_db_recover (деструктивен для
    общей таблицы), НЕТ startup-sweep пустых черновиков, НЕТ resume/delayed-repair демонов и НЕТ
    web-posted поллера. Поэтому рестарт этого сервиса НИКОГДА не трогает очередь создания РК, а
    рестарт direct.service не трогает копирование (его recover исключает kind='copy_campaigns')."""
    with _CREATE_JOBS_LOCK:
        if _CREATE_WORKER["started"]:
            return
        _CREATE_WORKER["started"] = True
    _jobs_db_init()                                       # схема таблицы (mirror прогресса копирования)
    _copy_jobs_recover()                                  # crash-cleanup ТОЛЬКО своих copy-джоб
    _ensure_create_watchdog()                             # heartbeat зависших джоб (по in-memory этого процесса)
    _create_watchdog_tick()
    workers = int(_CREATE_WORKERS or _create_workers_count())
    for _ in range(workers):                              # параллельно по разным агентствам
        threading.Thread(target=_create_worker_loop, args=(app,), daemon=True).start()


# ── worker-роль: БД-поллер (забирает web-posted джобы из БД в in-memory очередь) ──
def _worker_claim_web_jobs() -> list:
    """Атомарно клеймит web-posted queued-джобы: queued→claimed RETURNING (защита от двойного клейма)."""
    import psycopg2.extras
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "UPDATE public.direct_automation_jobs SET status='claimed', updated_at=now() "
                "WHERE job_id IN ("
                "    SELECT job_id FROM public.direct_automation_jobs "
                "     WHERE status='queued' AND coalesce(body->>'_web_posted','')='true' "
                "     ORDER BY (coalesce(body->>'_priority','')='true') DESC, created_at "
                "     LIMIT 10 FOR UPDATE SKIP LOCKED) "
                "RETURNING job_id, login, total, body")
            rows = cur.fetchall() or []
            conn.commit()
            return rows
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return []


def _worker_adopt_job(app, row) -> None:
    """Завести заклеймленную web-джобу в in-memory очередь воркера (status back → 'queued').

    ⚠️ Гейт «уже в памяти» проверяет РЕАЛЬНОЕ участие (в _CREATE_QUEUE или running), а не голое
    наличие в _CREATE_JOBS: стартовый загрузчик истории (см. ~строка 716) поднимает из БД ВСЕ
    незавершённые джобы как записи-карточки БЕЗ постановки в очередь → старый гейт `jid in
    _CREATE_JOBS` молча пропускал адопт и джоба зависала в 'claimed' НАВСЕГДА (root-cause
    инцидента f64fc17a3ae5, 2026-07-06: воспроизводилось при КАЖДОМ рестарте с queued web-джобой
    в БД). Стале-запись перезаписываем и ставим в очередь."""
    jid = row["job_id"]
    _term = None
    with _CREATE_JOBS_LOCK:
        _mem = _CREATE_JOBS.get(jid)
        if _mem is not None and (jid in _CREATE_QUEUE or _mem.get("status") == "running"):
            return                                        # реально в очереди/исполняется
        if _mem is not None and _mem.get("status") in _JOB_TERMINAL:
            _term = dict(_mem)
    if _term is not None:
        # Джоба УЖЕ terminal в ЭТОМ процессе (done/error/cancelled), а в БД остался стале
        # 'queued'/'claimed' (сбой финального _job_db_save / cancel без сейва) → НЕ переисполнять
        # (повторный прогон = ДУБЛИ кампаний в кабинете клиента, ревью 06.07), а досинхронизировать
        # терминальный статус в БД, чтобы поллер перестал её клеймить.
        _job_db_save(jid, _term)
        return
    body = row.get("body") or {}
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:  # noqa: BLE001
            _job_db_set_status(jid, "error", "битый body web-джобы"); return
    login = row.get("login") or ""
    items = body.get("items") or []
    total = int(row.get("total") or len(items))
    saved_session = body.get("_session_snapshot") or {"logged_in": True, "is_admin": True}
    body["_job_id"] = jid
    with _CREATE_JOBS_LOCK:
        job = {"status": "queued", "login": login, "done": 0,
               "total": total, "created": 0, "failed": 0,
               "set_done": 0, "set_total": total,
               "result": None, "error": None, "cancel": False,
               "kind": _job_kind(body), "publish": bool(body.get("launch")),
               "stream_content": bool(body.get("stream_content")),
               "step": None, "_id": jid, "body": body, "session": saved_session,
               "agency": body.get("agency"), "_heartbeat": time.time()}
        _CREATE_JOBS[jid] = job
        if body.get("_priority"):
            _CREATE_QUEUE.insert(0, jid)                  # добивка/доставка — впереди обычных наборов
        else:
            _CREATE_QUEUE.append(jid)
        _CREATE_COND.notify()
    _job_db_save(jid, job)                                # claimed → queued (running проставит воркер)
    try:
        _prefetch_start(login, body)                     # Фаза 1: греем кэши процесса-ИСПОЛНИТЕЛЯ
    except Exception:  # noqa: BLE001
        pass


def _worker_expire_awaiting_feed() -> None:
    """web-роль поставила ожидание решения по фиду; дедлайн истёк → запускаем без фида (worker-время)."""
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE public.direct_automation_jobs "
                "SET status='queued', "
                "    body = jsonb_set(body - '_feed_deadline', '{_skip_feed_types}', "
                "                     '[\"product\",\"master\"]'::jsonb), "
                "    updated_at=now() "
                "WHERE status='awaiting_feed_decision' "
                "  AND coalesce((body->>'_feed_deadline')::double precision, 0) > 0 "
                "  AND (body->>'_feed_deadline')::double precision < extract(epoch from now())")
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _worker_apply_controls() -> None:
    """Применить команды web→worker из колонки control (сейчас: 'cancel' running-джобы) и обнулить её."""
    import psycopg2.extras
    rows = []
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT job_id, control FROM public.direct_automation_jobs WHERE control IS NOT NULL")
            rows = cur.fetchall() or []
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return
    for r in rows:
        jid = r["job_id"]
        ctrl = (r.get("control") or "").strip()
        if ctrl == "cancel":
            _cancelled = None
            with _CREATE_COND:
                j = _CREATE_JOBS.get(jid)
                if j is not None:
                    j["cancel"] = True                    # стоп после текущей кампании item'а
                    if j.get("status") == "queued" and jid in _CREATE_QUEUE:
                        _CREATE_QUEUE.remove(jid)
                        j["status"] = "cancelled"; j["finished_at"] = time.time()
                        _cancelled = dict(j)
                _CREATE_COND.notify_all()
            if _cancelled is not None:
                # Персистим отмену в БД (ревью 06.07): без этого строка остаётся 'queued'
                # (_web_posted) → поллер ре-клеймит её и отменённая джоба ИСПОЛНЯЕТСЯ.
                _job_db_save(jid, _cancelled)
        # feed-решения web-роль применяет напрямую (status flip в БД), поэтому здесь только 'cancel'.
        try:
            conn = _victory_conn_rw()
            try:
                cur = conn.cursor()
                cur.execute("UPDATE public.direct_automation_jobs SET control=NULL WHERE job_id=%s", (jid,))
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            pass


_CLAIMED_WATCHDOG_TS = {"t": 0.0}    # троттл watchdog'а зависших claimed (поллер крутится каждые 2с)


def _worker_reclaim_stuck_claimed() -> None:
    """Watchdog: джоба, заклеймленная (queued→claimed), но НЕ заведённая в in-memory очередь
    (исключение в _worker_adopt_job / рестарт между клеймом и адоптом), зависает в 'claimed'
    НАВСЕГДА: клейм берёт только status='queued', а стартовое рекавери claimed→queued работает
    лишь при рестарте воркера. Живой кейс 2026-07-06: f64fc17a3ae5 (доставка остатка Щербаковой,
    7 tp5) висела в claimed без прогресса. Возвращаем в 'queued' claimed старше 5 мин, которых
    НЕТ в _CREATE_JOBS этого процесса (есть в памяти → доведёт адопт/исполнение, не трогаем).
    Троттл 60с — не дёргать Victory каждый 2-секундный тик поллера."""
    if time.time() - _CLAIMED_WATCHDOG_TS["t"] < 60:
        return
    _CLAIMED_WATCHDOG_TS["t"] = time.time()
    try:
        with _CREATE_JOBS_LOCK:
            # «знакомые» = реально в работе (в очереди или исполняются); голая запись-карточка
            # из стартового загрузчика истории — НЕ работа (см. гейт в _worker_adopt_job)
            known = {j for j, v in _CREATE_JOBS.items()
                     if j in _CREATE_QUEUE or (v or {}).get("status") == "running"}
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("SELECT job_id FROM public.direct_automation_jobs "
                        "WHERE status='claimed' AND updated_at < now() - interval '5 minutes'")
            stale = [r[0] for r in (cur.fetchall() or []) if r[0] not in known]
            if stale:
                cur.execute("UPDATE public.direct_automation_jobs SET status='queued', "
                            "updated_at=now() WHERE status='claimed' AND job_id = ANY(%s)", (stale,))
                conn.commit()
                print(f"[claimed-watchdog] зависшие claimed возвращены в очередь: {stale}", flush=True)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — watchdog best-effort, поллер не валим
        pass


def _worker_poll_once(app) -> None:
    _worker_expire_awaiting_feed()
    for row in _worker_claim_web_jobs():
        try:
            _worker_adopt_job(app, row)
        except Exception as _ae:  # noqa: BLE001
            # НЕ молчим (фикс 2026-07-06): проглоченный адопт оставлял джобу в 'claimed' навсегда
            # (кейс f64fc17a3ae5). След в журнале + вернёт claimed-watchdog ниже.
            print(f"[worker-adopt] job {row.get('job_id')}: {type(_ae).__name__}: {str(_ae)[:200]}",
                  flush=True)
    _worker_reclaim_stuck_claimed()
    _worker_apply_controls()


def _worker_poll_loop(app) -> None:
    while not _CREATE_DRAIN.get("on"):
        try:
            _worker_poll_once(app)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(_WORKER_POLL_SEC)


def _ensure_worker_poller(app) -> None:
    """Стартует БД-поллер web-posted джоб. Только worker-роль (в 'all' постановка идёт in-memory,
    web-posted джоб нет; в 'web' воркеров нет)."""
    if _direct_role() != "worker":
        return
    with _CREATE_JOBS_LOCK:
        if _WORKER_POLLER["started"]:
            return
        _WORKER_POLLER["started"] = True
    threading.Thread(target=_worker_poll_loop, args=(app,), daemon=True).start()


def _worker_bootstrap(app) -> None:
    """Точка входа worker_main: поднять пул воркеров, все демоны и БД-поллер."""
    _ensure_create_worker(app)                            # jobs_db_init + recover + watchdog + воркеры + демоны
    _ensure_worker_poller(app)                            # + поллер web-posted джоб из БД


def _busy_response(reason: str, wait: int):
    if reason == "cooldown":
        msg = f"Подождите ещё ~{wait} c перед повторной выгрузкой (защита аккаунта от блокировки)."
    else:
        msg = "Сейчас уже идёт выгрузка (возможно, в другой вкладке). Дождитесь её завершения."
    return jsonify({"error": msg, "locked": True, "reason": reason, "wait": wait}), 429


# ── Pages ─────────────────────────────────────────────────────────────────────

def _render_page():
    return render_template(
        "direct/index.html",
        active_section="work", active_page="direct_automation",
        audiences=_load_audiences(),
        feeds_catalog=_json("feeds_catalog.json"),
        slepki_structure=_slepki_structure_for_ui(),   # фильтр по боевому профилю (донорские tp скрыты)
        model_cts=_model_cts(),                 # модельные ct (совместимость)
        ct_segments=_ct_segment_map(),          # ct → 'Модели'|'Марки'|'Общее' (единый источник для UI и плана)
        donor_tp4_models=_donor_tp4_models_map(),  # {slepok: [site_type]} — tp4 «Модели» от донора
        default_name=cmc.DEFAULT_DISPLAY_NAME,
    )

from .routes_pages import register_page_routes  # noqa: E402

register_page_routes(
    bp,
    _direct_access,
    _direct_minusphrase_access,
    render_page=_render_page,
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


# ── Минус-площадки РСЯ (#21): глобальный список URL, добавляется в disabledPlaces всех tp1 ─────────
_MINUS_PLACES_ENSURED = False                            # DDL гоняем 1 раз на процесс, не на каждый вызов


def _minus_places_ensure(cur) -> None:
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


def _enabled_minus_places() -> list[str]:
    """Хосты включённых минус-площадок для disabledPlaces tp1 (домен, не URL; дедуп). [] при сбое/пустом."""
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
    except Exception:  # noqa: BLE001 — недоступность БД не валит создание
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


def _content_rules_ensure(cur) -> None:
    cur.execute(
        "CREATE TABLE IF NOT EXISTS public.direct_content_asset_rules ("
        "asset_key text PRIMARY KEY, asset_type text NOT NULL, source_segment text NOT NULL, "
        "source_tp text NOT NULL, source_ct text NOT NULL, asset_path text NOT NULL, "
        "name text NOT NULL DEFAULT '', enabled boolean NOT NULL DEFAULT true, "
        "allowed_for jsonb NOT NULL DEFAULT '[]'::jsonb, updated_at timestamptz NOT NULL DEFAULT now())"
    )
    cur.execute("ALTER TABLE public.direct_content_asset_rules "
                "ADD COLUMN IF NOT EXISTS source_slepok text NOT NULL DEFAULT ''")
    cur.execute("ALTER TABLE public.direct_content_asset_rules "
                "ADD COLUMN IF NOT EXISTS allowed_slepki jsonb NOT NULL DEFAULT '[]'::jsonb")


_CONTENT_RULES_CACHE: dict = {"ts": 0.0, "rows": {}}


def _content_rules_map(force: bool = False) -> dict:
    now = time.monotonic()
    if not force and _CONTENT_RULES_CACHE["rows"] and now - _CONTENT_RULES_CACHE["ts"] < 60:
        return _CONTENT_RULES_CACHE["rows"]
    import psycopg2.extras
    conn = _victory_conn_rw()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _content_rules_ensure(cur)
        conn.commit()
        cur.execute(
            "SELECT asset_key, asset_type, source_segment, source_tp, source_ct, asset_path, "
            "name, enabled, allowed_for, source_slepok, allowed_slepki "
            "FROM public.direct_content_asset_rules"
        )
        rows = {str(r["asset_key"]): dict(r) for r in cur.fetchall()}
        _CONTENT_RULES_CACHE.update({"ts": now, "rows": rows})
        return rows
    finally:
        conn.close()


def _asset_key_from_local(path: str) -> str:
    return os.path.splitext(os.path.basename(str(path or "")))[0]


def _manual_rule_lookup_key(path: str, ct: str) -> tuple[str, str, str, str, str] | None:
    """Scoped-key для локального Manual-файла.

    Во вкладке «Контент» Manual хранится как remote-файл M3
    /Users/Shared/agency/creatives/Manual/{ct}/{file}.png, а в создании кампаний мы читаем
    локальный mount /opt/creatives/Manual/{ct}/{file}.png. Чтобы выключения/allowed_for работали
    одинаково, строим тот же scoped asset_key.
    """
    try:
        import os as _os
        from . import kontent_pack as _kp
        p = str(path or "")
        manual_root = str(MANUAL_CREATIVES_DIR).rstrip("/")
        if not p.startswith(manual_root + "/"):
            return None
        ct_norm = _gc_ct(ct) or _gc_ct(_os.path.basename(_os.path.dirname(p))) or "ct0000"
        remote = posixpath.join(getattr(_kp, "M3_MANUAL_ROOT", "/Users/Shared/agency/creatives/Manual"),
                                ct_norm, _os.path.basename(p))
        original_key = _kp.remote_asset_key(remote)
        return ("Общее", "manual", ct_norm, original_key, "")
    except Exception:  # noqa: BLE001
        return None


def _content_rule_key(segment: str, tp: str, ct: str, asset_key: str, source_slepok: str = "") -> str:
    """Scope правила контента: тип сайта + tp + ct + слепок + файл.

    Один и тот же файл может лежать в одинаковом ct у разных типов сайтов; правило
    отключения/allowed_for не должно протекать между ними. Слепок также входит в scope,
    чтобы включение/выключение в одном слепке не меняло тот же ct у другого слепка.
    """
    raw = "|".join([
        str(segment or "").strip(),
        str(tp or "").strip(),
        (_gc_ct(ct) or str(ct or "").strip().lower()),
        str(source_slepok or "").strip().lower(),
        str(asset_key or "").strip(),
    ])
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _ct_allowed_for(rule: dict, target_ct: str) -> bool:
    target_ct = _gc_ct(target_ct) or str(target_ct or "").strip().lower()
    allowed = _content_allowed_list(rule)
    source_ct = str(rule.get("source_ct") or "").strip().lower()
    if not allowed:
        return not target_ct or target_ct == source_ct
    if "*" in allowed:
        return True
    if "common" in allowed and target_ct in _COMMON_IMAGE_CTS:
        return True
    return target_ct in allowed


def _content_allowed_list(rule: dict) -> list[str]:
    allowed = rule.get("allowed_for") or []
    if isinstance(allowed, str):
        try:
            allowed = json.loads(allowed)
        except Exception:  # noqa: BLE001
            allowed = [x.strip() for x in allowed.split(",") if x.strip()]
    return [str(x).strip().lower() for x in (allowed or []) if str(x).strip()]


def _content_slepok_list(rule: dict) -> list[str]:
    allowed = rule.get("allowed_slepki") or []
    if isinstance(allowed, str):
        try:
            allowed = json.loads(allowed)
        except Exception:  # noqa: BLE001
            allowed = [x.strip() for x in re.split(r"[,;\s]+", allowed) if x.strip()]
    return [str(x).strip().lower() for x in (allowed or []) if str(x).strip()]


def _slepok_allowed_for(rule: dict, target_slepok: str) -> bool:
    target = str(target_slepok or "").strip().lower()
    source = str(rule.get("source_slepok") or "").strip().lower()
    allowed = _content_slepok_list(rule)
    if not allowed:
        return (not source) or (not target) or source == target
    if "*" in allowed:
        return True
    if "common" in allowed and not target:
        return True
    return target in allowed


def _content_only_this_ct(rule: dict, target_ct: str) -> bool:
    target_ct = _gc_ct(target_ct) or str(target_ct or "").strip().lower()
    source_ct = str(rule.get("source_ct") or "").strip().lower()
    if source_ct != target_ct:
        return False
    allowed = _content_allowed_list(rule)
    if not allowed:
        return True
    return len(allowed) == 1 and allowed[0] == target_ct


def _filter_content_assets(paths: list, target_ct: str, *, source_segment: str = "", source_tp: str = "",
                           source_ct: str = "", target_slepok: str = "", source_slepok: str = "") -> list:
    """Применить вкладку «Контент»: выключенные ассеты режем, allowed_for ограничивает целевой ct.
    Если правила на файл нет — сохраняем старое поведение и пропускаем."""
    if not paths:
        return []
    try:
        rules = _content_rules_map()
    except Exception:  # noqa: BLE001
        return list(paths)
    out = []
    for p in paths:
        file_key = _asset_key_from_local(p)
        r = None
        manual_scope = _manual_rule_lookup_key(p, source_ct or target_ct)
        if manual_scope:
            r = rules.get(_content_rule_key(*manual_scope))
        if source_segment and source_tp and source_ct:
            r = r or rules.get(_content_rule_key(source_segment, source_tp, source_ct, file_key, source_slepok))
            if not r and source_slepok:
                r = rules.get(_content_rule_key(source_segment, source_tp, source_ct, file_key, ""))
        if not r and not (source_segment and source_tp and source_ct):
            r = rules.get(file_key)
        if r:
            if not r.get("enabled"):
                continue
            if not _ct_allowed_for(r, target_ct):
                continue
            if not _slepok_allowed_for(r, target_slepok):
                continue
        out.append(p)
    return out


def _prioritized_content_assets(paths: list, target_ct: str, *, source_segment: str, source_tp: str,
                                source_ct: str, target_slepok: str = "", source_slepok: str = "",
                                limit: int = 5) -> list:
    """Отфильтровать ассеты и поднять наверх выбранные «только этот ct».

    Если таких приоритетных ассетов больше limit, берём случайные limit штук.
    """
    if not paths:
        return []
    try:
        rules = _content_rules_map()
    except Exception:  # noqa: BLE001
        return list(dict.fromkeys(paths))[:limit]
    priority: list = []
    regular: list = []
    seen: set[str] = set()
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        file_key = _asset_key_from_local(p)
        manual_scope = _manual_rule_lookup_key(p, source_ct or target_ct)
        r = rules.get(_content_rule_key(*manual_scope)) if manual_scope else None
        r = r or rules.get(_content_rule_key(source_segment, source_tp, source_ct, file_key, source_slepok))
        if not r and source_slepok:
            r = rules.get(_content_rule_key(source_segment, source_tp, source_ct, file_key, ""))
        if r:
            if not r.get("enabled"):
                continue
            if not _ct_allowed_for(r, target_ct):
                continue
            if not _slepok_allowed_for(r, target_slepok):
                continue
            if _content_only_this_ct(r, target_ct):
                priority.append(p)
                continue
        regular.append(p)
    if len(priority) >= limit:
        return random.sample(priority, limit)
    return (priority + [p for p in regular if p not in priority])[:limit]


def _explicit_content_assets_for(target_ct: str, *, target_slepok: str = "",
                                 asset_types: set[str] | None = None, limit: int = 5) -> list:
    """Ассеты, явно разрешённые во вкладке «Контент» для другого/общего ct."""
    try:
        rules = _content_rules_map()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rules.values():
        if not r.get("enabled") or not _ct_allowed_for(r, target_ct):
            continue
        if not _slepok_allowed_for(r, target_slepok):
            continue
        if asset_types and str(r.get("asset_type") or "") not in asset_types:
            continue
        p = kp.fetch_remote_asset(r.get("asset_path") or "")
        if p and p not in out:
            out.append(p)
        if len(out) >= limit:
            break
    return out


def _ahash_distance(a: int, b: int) -> int:
    return bin(int(a) ^ int(b)).count("1")


def _dedupe_content_assets_for_ui(assets: list[dict], threshold: int = 18) -> tuple[list[dict], int]:
    """Быстрый дедуп для вкладки «Контент».

    Визуальный pHash требует скачать/декодировать каждую картинку с M3. На больших папках это
    блокирует первый экран, поэтому в интерактивном API скрываем только точные дубли по remote/token.
    Визуальную чистку дублей надо делать отдельной фоновой задачей, а не при каждом клике по ct.
    """
    kept: list[dict] = []
    seen: set[str] = set()
    hidden = 0
    for a in assets or []:
        key = str(a.get("remote") or a.get("original_asset_key") or a.get("asset_key") or a.get("token") or "")
        if key and key in seen:
            hidden += 1
            continue
        if key:
            seen.add(key)
        kept.append(a)
    return kept, hidden


# ── Аккаунты (Victory DB local_gsheet_sites, direction='Авто') ─────────────────

_ACCOUNT_COLS = ["domain", "salon", "city", "site_type", "login_key", "counter_number",
                 "client_id", "agency_account", "directologist", "status"]
DEFAULT_STATUS = "Контекст активно"
# Директологи-исключения (агентства/субподряд — не нужны в таблице)
_EXCLUDE_DIRECTOLOGS = ["Аксиома", "О-Лидер", "Медиа-Актив", "Ниндзя Илья"]


def _victory_conn():
    import psycopg2
    sd = str(cmc._find_secret_dir())
    if sd not in sys.path:
        sys.path.insert(0, sd)
    from loader import load_db  # noqa: E402
    cfg = load_db("victory")
    conn = psycopg2.connect(host=cfg["host"], port=cfg["port"], database=cfg["database"],
                            user=cfg["user"], password=cfg["password"], connect_timeout=15)
    conn.set_session(readonly=True, autocommit=True)
    return conn


# _victory_conn определён — инъектим в promo_gen (_promo_ctx читает local_gsheet_sites).
_pg.configure({"_victory_conn": _victory_conn})


from .routes_reference import register_reference_routes  # noqa: E402
from .routes_settings import register_settings_routes  # noqa: E402
from .routes_accounts import register_account_routes  # noqa: E402
from .routes_content import register_content_routes  # noqa: E402
from .routes_content_editor import register_content_editor_routes  # noqa: E402
from .routes_ai import register_ai_routes  # noqa: E402
from .routes_copy import register_copy_routes  # noqa: E402
from .routes_jobs import register_job_routes  # noqa: E402
from .routes_create_set import register_create_set_routes  # noqa: E402
from .routes_overview import register_overview_routes  # noqa: E402
from .routes_deferred import register_deferred_routes  # noqa: E402
from .routes_pack import register_pack_routes  # noqa: E402
from .routes_campaigns import register_campaign_routes  # noqa: E402
from .routes_set_plan import register_set_plan_routes  # noqa: E402
from .routes_ready_logins import register_ready_logins_routes  # noqa: E402

register_reference_routes(
    bp,
    _direct_access,
    list_feeds_for_site=cmc.list_feeds_for_site,
    load_json=_json,
    load_audiences=_load_audiences,
    victory_conn=_victory_conn,
    ag_part1_map=_ag_part1_map,
)

register_overview_routes(
    bp,
    _direct_access,
    victory_conn=_victory_conn,
)

register_ready_logins_routes(
    bp,
    _direct_access,
    victory_conn=_victory_conn,
    victory_conn_rw=lambda: _victory_conn_rw(),
    db_init=_ready_logins_db_init,
)

def _victory_conn_rw():
    """Подключение к Victory с правами на запись (для UPDATE правил РК)."""
    import psycopg2
    sd = str(cmc._find_secret_dir())
    if sd not in sys.path:
        sys.path.insert(0, sd)
    from loader import load_db  # noqa: E402
    cfg = load_db("victory")
    conn = psycopg2.connect(host=cfg["host"], port=cfg["port"], database=cfg["database"],
                            user=cfg["user"], password=cfg["password"], connect_timeout=15)
    conn.autocommit = False
    return conn


register_settings_routes(
    bp,
    _direct_access,
    global_feed_rules=_global_feed_rules,
    feed_key=_feed_key,
    feed_rules_ensure=_feed_rules_ensure,
    global_minus_places=_global_minus_places,
    minus_places_ensure=_minus_places_ensure,
    place_host=_place_host,
    minus_words_slice=_global_minus_words_slice,
    minus_words_ensure=_minus_words_ensure,
    global_minus_marks=_global_minus_marks,
    minus_marks_ensure=_minus_marks_ensure,
    known_brand_canons=lambda: sorted(_known_brand_canons()),
    global_minus_models=_global_minus_models,
    minus_models_ensure=_minus_models_ensure,
    load_brand_models_catalog=_load_brand_models_catalog,
    victory_conn=_victory_conn,
    victory_conn_rw=_victory_conn_rw,
)

def _parse_counter_ids(text) -> list[int]:
    """'[103879503, 94543727]' → [103879503, 94543727]. Кривое/пустое → []."""
    if not text:
        return []
    try:
        arr = json.loads(text)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for x in (arr if isinstance(arr, list) else []):
        s = str(x).strip()
        if s.lstrip("-").isdigit():
            out.append(int(s))
    return out


def _metrika_goals_for(login: str):
    """Счётчики Метрики и цель «Все формы» из public.metrika_goals (внешняя таблица Victory).
    → {counters:[int,...], goal_id:int|None} либо None, если строки по логину нет."""
    if not login:
        return None
    conn = _victory_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT counter_ids, all_forms FROM public.metrika_goals "
                    "WHERE account_login=%s LIMIT 1", (login,))
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"counters": _parse_counter_ids(row[0]),
            "goal_id": int(row[1]) if row[1] is not None else None}


def _counter_foreign_owner(counter_id: int, login: str):
    """Если счётчик Метрики закреплён в public.metrika_goals за ДРУГИМ аккаунтом (не `login`) —
    вернуть логин-владельца, иначе None. Counter расшарен и на сам `login` → None (легитимно).
    Anti-footgun: ловит «вставили счётчик/цель от ДРУГОГО аккаунта» ДО трат M3 и campaigns.add."""
    if not counter_id:
        return None
    try:
        conn = _victory_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT account_login, counter_ids FROM public.metrika_goals "
                        "WHERE counter_ids LIKE %s", (f"%{int(counter_id)}%",))
            rows = cur.fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None
    owners = [lk for lk, ct in rows if int(counter_id) in _parse_counter_ids(ct)]  # точное вхождение
    if not owners or login in owners:      # ничей / принадлежит самому аккаунту → не блокируем
        return None
    return owners[0]                       # счётчик есть только у чужого аккаунта


_LIVE_V4 = "https://api.direct.yandex.ru/live/v4/json/"


def _direct_tokens() -> dict:
    """{agency_account → oauth_token} из loader.load_yandex_direct (совпадает с колонкой agency_account)."""
    sd = str(cmc._find_secret_dir())
    if sd not in sys.path:
        sys.path.insert(0, sd)
    from loader import load_yandex_direct  # noqa: E402
    out = {}
    for ag, info in (load_yandex_direct().get("tokens") or {}).items():
        tok = info.get("oauth_token") if isinstance(info, dict) else info
        if tok:
            out[ag] = tok
    return out


def _do_balance(_rqs, ThreadPoolExecutor, as_completed):
    pairs = (request.json or {}).get("pairs") or []
    by_agency: dict[str, list[str]] = {}
    for p in pairs:
        lg = (p.get("login") or "").strip()
        ag = (p.get("agency") or "").strip()
        if lg and ag and ag != "None":
            by_agency.setdefault(ag, []).append(lg)

    tokens = _direct_tokens()
    balances: dict = {}

    def _fetch(tok: str, chunk: list[str], out: dict) -> None:
        """AccountManagement.Get с дроблением: один битый логин роняет весь батч (501),
        поэтому при ошибке делим пополам и изолируем плохой логин."""
        if not chunk:
            return
        body = {"method": "AccountManagement", "token": tok,
                "param": {"Action": "Get", "SelectionCriteria": {"Logins": chunk}}}
        try:
            j = _rqs.post(_LIVE_V4, json=body, timeout=30).json()
        except Exception:  # noqa: BLE001
            j = {"error_code": "net"}
        accs = (j.get("data") or {}).get("Accounts")
        if accs is not None and not j.get("error_code"):
            for acc in accs:
                out[acc.get("Login")] = round(float(acc.get("Amount") or 0), 2)
            return
        if len(chunk) == 1:           # одиночный битый логин — пропускаем
            return
        mid = len(chunk) // 2
        _fetch(tok, chunk[:mid], out)
        _fetch(tok, chunk[mid:], out)

    def _batch(ag: str, logins: list[str]) -> dict:
        tok = tokens.get(ag)
        if not tok:
            return {}
        out: dict = {}
        for i in range(0, len(logins), 50):            # начальные батчи по 50
            _fetch(tok, logins[i:i + 50], out)
        return out

    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(_batch, ag, lgs): ag for ag, lgs in by_agency.items()}
        for f in as_completed(futs):
            balances.update(f.result())

    # Фолбэк: колонка agency_account в БД бывает устаревшей/None — логин реально
    # управляется ДРУГИМ агентством, и AccountManagement.Get под записанным
    # токеном его не вернёт. Добираем недостающие перебором всех токенов
    # (тот же приём, что в проверке блокировок). Баланс 0 ₽ не дёргаем повторно.
    all_logins = []
    for p in pairs:
        lg = (p.get("login") or "").strip()
        if lg:
            all_logins.append(lg)
    missing = [lg for lg in all_logins if balances.get(lg) is None]
    if missing:
        for tok in tokens.values():
            if not missing:
                break
            out: dict = {}
            for i in range(0, len(missing), 50):
                _fetch(tok, missing[i:i + 50], out)
            balances.update({k: v for k, v in out.items() if v is not None})
            missing = [lg for lg in missing if balances.get(lg) is None]

    # логины без ответа → null
    for lg in all_logins:
        balances.setdefault(lg, None)
    return jsonify({"balances": balances})


_V5 = "https://api.direct.yandex.com/json/v5/"
_V501 = "https://api.direct.yandex.com/json/v501/"


def _v5_get(svc: str, token: str, login: str, fieldnames: list[str], criteria=None,
            extra: dict | None = None) -> dict:
    """Официальный OAuth API v5 GET одного сервиса. Возвращает распарсенный JSON.
    extra — дополнительные type-specific params (напр. {"UrlFeedFieldNames": ["Url"]})."""
    import requests as _rqs
    h = {"Authorization": "Bearer " + token, "Client-Login": login,
         "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8",
         "Use-Operator-Units": "true"}
    params: dict = {"FieldNames": fieldnames}
    if criteria is not None:
        params["SelectionCriteria"] = criteria
    if extra:
        params.update(extra)
    try:
        return _rqs.post(_V5 + svc, headers=h, json={"method": "get", "params": params}, timeout=30).json()
    except Exception as e:  # noqa: BLE001
        return {"error": {"error_string": str(e)[:120]}}


# Грубая оценка расхода баллов Директа на 1 созданную кампанию (для прикидки «хватит/не хватит»).
# Каждая кампания = campaign.add + десятки-сотни adgroups/keywords/ads (батчами). Цифра намеренно
# консервативная (округляем оценку «кампаний» ВНИЗ), чтобы не обещать лишнего.
_UNITS_PER_CAMPAIGN = 2500


def _v5_units(token: str, login: str) -> dict | None:
    """Остаток баллов агентства из заголовка ``Units`` (дешёвый GET campaigns, Limit:1).
    Формат заголовка Яндекса: ``Spent/Available/DailyLimit``. → {spent, rest, limit} или None."""
    import requests as _rqs
    h = {"Authorization": "Bearer " + token, "Client-Login": login,
         "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8",
         "Use-Operator-Units": "true"}
    body = {"method": "get", "params": {"FieldNames": ["Id"],
                                        "SelectionCriteria": {}, "Page": {"Limit": 1}}}
    try:
        r = _rqs.post(_V5 + "campaigns", headers=h, json=body, timeout=20)
        parts = [int(x) for x in (r.headers.get("Units") or "").split("/") if x.strip().lstrip("-").isdigit()]
        if len(parts) == 3:
            spent, rest, limit = parts
            return {"spent": spent, "rest": max(0, rest), "limit": limit}
    except Exception:  # noqa: BLE001
        pass
    return None


def _v5_call(svc: str, method: str, token: str, login: str, params: dict) -> dict:
    """Универсальный вызов v5 (get/suspend/…). Возвращает распарсенный JSON."""
    import requests as _rqs
    h = {"Authorization": "Bearer " + token, "Client-Login": login,
         "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8",
         "Use-Operator-Units": "true"}
    try:
        return _rqs.post(_V5 + svc, headers=h, json={"method": method, "params": params}, timeout=60).json()
    except Exception as e:  # noqa: BLE001
        return {"error": {"error_string": str(e)[:120]}}


def _v501_call(method: str, token: str, login: str, params: dict) -> dict:
    """Вызов v501 (campaigns.update и т.д.). Возвращает распарсенный JSON."""
    import requests as _rqs
    h = {"Authorization": "Bearer " + token, "Client-Login": login,
         "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8",
         "Use-Operator-Units": "true"}
    try:
        return _rqs.post(_V501 + "campaigns", headers=h,
                         json={"method": method, "params": params}, timeout=60).json()
    except Exception as e:  # noqa: BLE001
        return {"error": {"error_string": str(e)[:120]}}


def _v501_svc(svc: str, method: str, token: str, login: str, params: dict) -> dict:
    """Вызов произвольного сервиса v501 (ads/adgroups/…). Для ResponsiveAd (Комбинаторное)
    обязателен v501 — v5 отвечает «не поддерживается, используйте v501»."""
    import requests as _rqs
    h = {"Authorization": "Bearer " + token, "Client-Login": login,
         "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8",
         "Use-Operator-Units": "true"}
    try:
        return _rqs.post(_V501 + svc, headers=h,
                         json={"method": method, "params": params}, timeout=60).json()
    except Exception as e:  # noqa: BLE001
        return {"error": {"error_string": str(e)[:120]}}


def _v5_err(j: dict) -> str:
    e = j.get("error")
    if not isinstance(e, dict):
        return str(e)
    parts = [e.get("error_string") or ""]
    detail = e.get("error_detail") or e.get("message") or ""
    if detail and detail not in parts:
        parts.append(str(detail))
    return " — ".join(p for p in parts if p)


# Порядок статусов в списке: активные → остановленные → завершённые → архив.
_STATE_ORDER = {"ON": 0, "SUSPENDED": 1, "OFF": 2, "ENDED": 3, "CONVERTED": 4, "ARCHIVED": 5}


# ─── Авто-фолбэк агентства ────────────────────────────────────────────────────
# agency_account в local_gsheet_sites бывает неверным/устаревшим (и затирается прогоном
# big_analytics_v5). Логика: пробуем агентство «как есть» (override-кэш → БД); если доступа
# нет (НЕ транзиентная 429/сеть) — перебираем агентские токены и сохраняем найденное в
# отдельный кэш direct_agency_overrides (переживает перезалив local_*).

# Маркеры транзиентных сбоев — на них НЕ перебираем агентства (иначе при 429 долбим все подряд).
_TRANSIENT_MARKERS = ("timeout", "timed out", "connection", "temporar", "rate limit",
                      "too many request", "429", "503", "502", "unavailable", "503 ", "gateway")


def _is_transient(j: dict) -> bool:
    """True, если ошибка похожа на временную (rate-limit/сеть/таймаут), а не на отказ доступа."""
    if "error" not in j:
        return False
    s = (_v5_err(j) or "").lower()
    return any(m in s for m in _TRANSIENT_MARKERS)


def _agency_override_get(login: str) -> str | None:
    """Ранее найденное рабочее агентство для логина (кэш), либо None."""
    try:
        conn = _victory_conn()
    except Exception:  # noqa: BLE001
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT agency_account FROM public.direct_agency_overrides "
                        "WHERE login_key = %s", (login,))
            row = cur.fetchone()
        return (row[0] if row and row[0] else None)
    except Exception:  # noqa: BLE001 — таблицы ещё нет / нет доступа → просто без кэша
        return None
    finally:
        conn.close()


def _resolve_agency_hint(login: str, agency_hint: str) -> str:
    """Быстрое разрешение реального агентства для постановки джобы в очередь.

    НЕ делает API-вызовы к Яндексу (только кэш БД + local_gsheet_sites).
    Порядок: 1) явный agency_hint (уже передан с фронта)
             2) кэш direct_agency_overrides (из прошлых прогонов _token_for_login)
             3) колонка agency_account из local_gsheet_sites
    Возвращает разрешённое агентство или agency_hint (может быть "") если нигде не нашли.
    Best-effort: любой сбой БД → возвращаем agency_hint как есть."""
    ag = (agency_hint or "").strip().lower()
    if ag and ag != "none":
        return ag                                          # явный hint — берём сразу
    if not login:
        return ag
    # кэш override (таблица может ещё не существовать — _agency_override_get обработает)
    cached = _agency_override_get(login)
    if cached:
        return cached.strip().lower()
    # колонка из основной таблицы
    try:
        conn = _victory_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT agency_account FROM public.local_gsheet_sites "
                            "WHERE login_key=%s AND direction='Авто' LIMIT 1", (login,))
                row = cur.fetchone()
            db_ag = (row[0] if row and row[0] else None)
        finally:
            conn.close()
        if db_ag and db_ag.strip().lower() not in ("none", ""):
            return db_ag.strip().lower()
    except Exception:  # noqa: BLE001
        pass
    return ag                                              # "" если ничего не нашли


def _agency_override_save(login: str, agency: str) -> None:
    """Сохранить найденное рабочее агентство, чтобы в следующий раз не перебирать."""
    if not login or not agency:
        return
    try:
        conn = _victory_conn_rw()
    except Exception:  # noqa: BLE001
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS public.direct_agency_overrides ("
                " login_key text PRIMARY KEY,"
                " agency_account text NOT NULL,"
                " updated_at timestamptz NOT NULL DEFAULT now())")
            cur.execute(
                "INSERT INTO public.direct_agency_overrides (login_key, agency_account, updated_at) "
                "VALUES (%s, %s, now()) "
                "ON CONFLICT (login_key) DO UPDATE SET "
                "agency_account = EXCLUDED.agency_account, updated_at = now()",
                (login, agency))
        conn.commit()
    except Exception:  # noqa: BLE001 — не валим основную операцию из-за кэша
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        conn.close()


def _token_for_login(login: str, agency: str, tokens: dict) -> tuple[str | None, str | None]:
    """Токен агентства, под которым реально открывается аккаунт login.

    Порядок: override-кэш → agency «как есть» из БД → перебор всех агентских токенов.
    На каждом кандидате — лёгкая проверка campaigns.get(Id). Транзиентная ошибка (429/сеть)
    кандидата НЕ запускает перебор (возвращаем кандидата как есть). При переборе найденное
    агентство сохраняется в кэш. Возвращает (token, agency_used)."""
    seen: set[str] = set()
    # 1) Кандидаты «как есть»: сначала ранее найденное (кэш), затем из БД.
    for cand in (_agency_override_get(login), (agency if agency and agency != "None" else None)):
        if not cand or cand in seen or not tokens.get(cand):
            continue
        seen.add(cand)
        j = _v5_get("campaigns", tokens[cand], login, ["Id"], criteria={})
        if "error" not in j:
            return tokens[cand], cand
        # 152 (нет баллов) = аккаунт ВЛАДЕЕТСЯ этим агентством (иначе была бы ошибка доступа 8800):
        # доступ есть, просто исчерпаны баллы — по КУКЕ дальше можно получать/удалять/создавать
        # (tp6/tp7). Не считаем это «нет доступа», отдаём кандидата (как и транзиент 429/сеть).
        if _is_transient(j) or _is_units_exhausted(j.get("error")):
            return tokens[cand], cand
    # 2) Перебор остальных агентств (ошибка доступа у кандидатов) + сохранение находки.
    # 152 у перебираемого токена тоже = он ВЛАДЕЕТ аккаунтом (нет баллов ≠ нет доступа) → берём его.
    for ag, tok in tokens.items():
        if ag in seen:
            continue
        j = _v5_get("campaigns", tok, login, ["Id"], criteria={})
        if "error" not in j or _is_units_exhausted(j.get("error")):
            _agency_override_save(login, ag)
            return tok, ag
    return None, None


def _units_alive_for_login(login: str, agency: str = "") -> "bool | None":
    """Живы ли баллы агентства для этого логина (хватит ≥1 кампании)? Политика «баллы первичны»
    (Семён 2026-07-07): True → добивать ТОКЕНОМ немедленно; False → реальный 152 (только тогда
    ждать сброса). None — не удалось прочитать остаток (нет токена/сеть) → трактуем как «не мешать»
    (caller решает; для выбора resume_at None = не форсим ночную отложку)."""
    try:
        tok, _ag = _token_for_login(login, agency or "", _direct_tokens())
        if not tok:
            return None
        u = _v5_units(tok, login)
        if not u:
            return None
        return int(u.get("rest") or 0) >= int(_UNITS_PER_CAMPAIGN)
    except Exception:  # noqa: BLE001
        return None


register_deferred_routes(
    bp,
    _direct_access,
    direct_tokens=_direct_tokens,
    token_for_login=_token_for_login,
    v5_units=_v5_units,
    units_per_campaign=_UNITS_PER_CAMPAIGN,
    ensure_resume_daemon=_ensure_resume_daemon,
    victory_conn=_victory_conn,
    deferred_set_status=_deferred_set_status,
    deferred_enqueue_now=_deferred_enqueue_now,
    create_jobs_lock=_CREATE_JOBS_LOCK,
    create_jobs_ahead=_create_jobs_ahead,
)


# Типы кампаний, которым НЕ нужна агентская кука (только OAuth-токен v5/v501):
# tp1 РСЯ, tp3 товарная галерея РСЯ, tp2/tp4 текстовые. Всё остальное (tp5 grid-докрутка,
# tp6 МК / tp7 товарка через UAC) ходит на куке агентства → её тоже надо проверить ДО создания.
_TOKEN_ONLY_TYPES = {"search_test", "search_dynamic"}


def _preflight_creds(login: str, agency_hint: str, need_cookie: bool) -> dict:
    """ПРЕДПОЛЁТНАЯ проверка кредов ДО создания РК — «какой токен/куку реально использовать».

    Делает лёгкие read-only вызовы (с таймаутами: v5 GET 30c, grid 40c), чтобы при битых/
    протухших кредах упасть БЫСТРО и ЯВНО, а не уйти в тихий висяк на пути создания:
      1) токен агентства, реально открывающий ``login`` (через ``_token_for_login`` — внутри
         проба ``campaigns.get(Id)``; перебор всех агентских токенов с persist находки);
      2) если набор содержит grid/UAC-типы (tp5/tp6/tp7) — self-probe куки агентства в grid.

    Возвращает ``{ok, token, agency, cookie, error}``. Кука нужна только при ``need_cookie``;
    для чисто токенных наборов (tp1/tp2/tp3/tp4) мёртвая кука НЕ блокирует."""
    tokens = _direct_tokens()
    if not tokens:
        return {"ok": False, "token": None, "agency": None, "cookie": None,
                "error": "нет агентских токенов (loader.load_yandex_direct вернул пусто)"}
    token, agency = _token_for_login(login, agency_hint, tokens)
    if not token:
        # Нет рабочего токена (error 53 / аккаунт porg-* без агентского OAuth) — пробуем
        # cookie-only-путь: Grid/UAC создаёт РК и ставит цены без API-баллов (token="" в builders).
        # Fallback только при need_cookie=True; token-only типы (search_test/dynamic) — без fallback.
        if need_cookie:
            try:
                _fb_cookie = cmc.pick_working_cookie(login)
            except Exception:  # noqa: BLE001
                _fb_cookie = None
            if _fb_cookie:
                cmc.remember_working_cookie(login, _fb_cookie)
                return {"ok": True, "token": "", "agency": None, "cookie": _fb_cookie,
                        "cookie_only": True, "error": None}
        return {"ok": False, "token": None, "agency": None, "cookie": None,
                "error": (f"ни один агентский токен не открывает аккаунт {login} — проверьте "
                          f"доступ агентства к клиенту и актуальность OAuth-токенов")}
    cookie = None
    if need_cookie:
        try:
            cookie = cmc.pick_working_cookie(login, accounts=(agency,))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "token": token, "agency": agency, "cookie": None,
                    "error": f"кука агентства {agency} не загрузилась: {str(e)[:140]}"}
        if not cookie:
            return {"ok": False, "token": token, "agency": agency, "cookie": None,
                    "error": f"нет куки агентства {agency} — grid/uac-типы создать нельзя"}
        if _block_bootstrap(cookie, agency) is None:     # None = кука мертва/нет ответа grid
            return {"ok": False, "token": token, "agency": agency, "cookie": cookie,
                    "error": (f"кука агентства {agency} не отвечает в grid (протухла/нет доступа) — "
                              f"обновите куки; grid/uac-типы создать нельзя")}
        # ВАЖНО: downstream Grid/UAC-клиенты ниже по create-path вызывают pick_working_cookie(login)
        # без знания конкретной агентской куки из preflight. Если не запомнить уже проверенную куку,
        # они могут взять другую/битую и словить HTML Login вместо JSON на addShoppingAds/finalize.
        cmc.remember_working_cookie(login, cookie)
    return {"ok": True, "token": token, "agency": agency, "cookie": cookie, "error": None}


def _account_assets_response():
    """Что РЕАЛЬНО заведено на аккаунте (живьём, офиц. v5): фиды / аудитории / промоакции.

    ?login=<login>&agency=<agency_account>. Ответ:
      {feeds:[{id,name,business_type,source_type}], audiences:[{id,name,type,scope}],
       promos:[{id,name,type,description,amount,unit,prefix,promocode,href,start,end}], errors:{}}.
    """
    login = (request.args.get("login") or "").strip()
    agency = (request.args.get("agency") or "").strip()
    if not login:
        return jsonify({"error": "login обязателен"}), 400

    ok, reason, wait = _pull_begin(f"assets:{login}", _COOLDOWN["assets"])
    if not ok:
        return _busy_response(reason, wait)
    try:
        return _do_assets(login, agency)
    finally:
        _pull_end(f"assets:{login}")


def _do_assets(login: str, agency: str):
    tokens = _direct_tokens()
    token, agency_used = _token_for_login(login, agency, tokens)
    out: dict = {"login": login, "agency": agency_used, "feeds": [], "audiences": [], "promos": [], "errors": {}}
    if not token:
        out["errors"]["all"] = "нет рабочего агентского токена для этого логина"
        return jsonify(out)

    jf = _v5_get("feeds", token, login, ["Id", "Name", "BusinessType", "SourceType", "Url"])
    if "error" in jf:
        out["errors"]["feeds"] = jf["error"].get("error_string")
    else:
        raw_feeds = (jf.get("result") or {}).get("Feeds", [])
        out["feeds"] = [{"id": f["Id"], "name": f.get("Name"), "business_type": f.get("BusinessType"),
                         "source_type": f.get("SourceType")} for f in raw_feeds]
        # Количество разрешённых URL-фидов для предпланового бейджа tp5/tp7 (fan-out по фидам).
        out["allowed_feeds_count"] = len(_filter_allowed_feed_rows(raw_feeds))

    ja = _v5_get("retargetinglists", token, login, ["Id", "Name", "Type", "Scope"], criteria={})
    if "error" in ja:
        out["errors"]["audiences"] = ja["error"].get("error_string")
    else:
        # только раздел RETARGETING (исключаем AUDIENCE «Интересы и привычки» и пр.)
        out["audiences"] = [{"id": a["Id"], "name": a.get("Name"), "type": a.get("Type"),
                             "scope": a.get("Scope")}
                            for a in (ja.get("result") or {}).get("RetargetingLists", [])
                            if a.get("Type") == "RETARGETING"]

    jp = _v5_get("promotions", token, login,
                 ["Id", "Type", "Name", "Description", "Amount", "AmountPrefix", "AmountUnit",
                  "Promocode", "Href", "StartDate", "EndDate"], criteria={})
    if "error" in jp:
        out["errors"]["promos"] = jp["error"].get("error_string")
    else:
        out["promos"] = [{"id": p["Id"], "name": p.get("Name"), "type": p.get("Type"),
                          "description": p.get("Description"), "amount": p.get("Amount"),
                          "unit": p.get("AmountUnit"), "prefix": p.get("AmountPrefix"),
                          "promocode": p.get("Promocode"), "href": p.get("Href"),
                          "start": p.get("StartDate"), "end": p.get("EndDate")}
                         for p in (jp.get("result") or {}).get("Promotions", [])]
    return jsonify(out)


def _account_audiences_response():
    """Аудитории типа RETARGETING (пригодные для корректировок ставок) на аккаунте.

    ?login=<login>&agency=<agency_account>
    Ответ: {"audiences":[{"id":<int>,"name":<str>}], "error":<str, опционально>}.
    Фильтр: Type==RETARGETING И Scope==FOR_TARGETS_AND_ADJUSTMENTS (списки AUDIENCE/
    FOR_TARGETS_ONLY корректировку bidmodifiers НЕ принимают).
    """
    login = (request.args.get("login") or "").strip()
    agency = (request.args.get("agency") or "").strip()
    if not login:
        return jsonify({"audiences": [], "error": "login обязателен"}), 400

    tokens = _direct_tokens()
    token, _ = _token_for_login(login, agency, tokens)
    if not token:
        return jsonify({"audiences": [], "error": "нет рабочего агентского токена для этого логина"})

    ja = _v5_get("retargetinglists", token, login, ["Id", "Name", "Type", "Scope"], criteria={})
    if "error" in ja:
        err_str = (ja.get("error") or {}).get("error_string") or str(ja.get("error"))
        return jsonify({"audiences": [], "error": err_str})

    audiences = [
        {"id": a["Id"], "name": a.get("Name")}
        for a in (ja.get("result") or {}).get("RetargetingLists", [])
        if a.get("Type") == "RETARGETING" and a.get("Scope") == "FOR_TARGETS_AND_ADJUSTMENTS"
    ]
    # Процент корректировки для каждой аудитории берём из «Глобальных правил» по городу
    # аккаунта (матчинг geo_X→<город>, self_X→self). Нет правила → adj=None (фронт ставит дефолт).
    ctx = _account_ctx(login)
    corr = _load_corrections((ctx or {}).get("city") or "*")
    seg_pct = _corrections_by_segment(corr.get("audiences", []), [a.get("name") or "" for a in audiences])
    for a in audiences:
        a["adj"] = seg_pct.get(a.get("name") or "")   # int% из правил (с кросс-кл. фолбэком), либо None
    return jsonify({"audiences": audiences})


# Лимиты «Уточнений» (callouts) в Яндекс.Директе.
_CALLOUT_MAX_EACH = 25            # длина одного уточнения
_CALLOUT_MAX_TOTAL_DESKTOP = 132  # суммарно на десктопе
_CALLOUT_MAX_TOTAL_MOBILE = 76    # суммарно на мобильных

_SLEPOK_KEY = {"слепок_павлов": "pavlov", "слепок_щербакова": "scherbakova",
               "слепок_крючкова": "kryuchkova", "слепок_терехов": "terehov",
               "слепок_караваев": "karavaev", "слепок_саламахин": "salamahin",
               "слепок_гордеева": "gordeeva", "слепок_зубакин": "zubakin",
               "слепок_чепелев": "chepelev", "слепок_тумашенко": "tumashenko",
               "слепок_кудерко": "kuderko", "слепок_gen_ses": "gen_ses",
               "слепок_dmp": "dmp"}
_SLEPOK_CANONICAL = {"pavlov", "kryuchkova", "scherbakova", "terehov", "karavaev",
                      "salamahin", "gordeeva", "zubakin", "chepelev", "tumashenko",
                      "kuderko", "gen_ses", "dmp"}


def _slepok_key_from_text(raw: str) -> str:
    """Best-effort: имя слепка/директолога из БД/UI → canonical ai_agents key."""
    s = (raw or "").strip().lower()
    if not s:
        return ""
    if s in _SLEPOK_KEY:
        return _SLEPOK_KEY[s]
    if s in _SLEPOK_CANONICAL:
        return s
    if "павлов" in s:
        return "pavlov"
    if "крючков" in s:
        return "kryuchkova"
    if "щербаков" in s:
        return "scherbakova"
    if "терехов" in s:
        return "terehov"
    if "караваев" in s:
        return "karavaev"
    if "саламахин" in s:
        return "salamahin"
    if "гордеев" in s:
        return "gordeeva"
    if "зубакин" in s:
        return "zubakin"
    if "чепелев" in s:
        return "chepelev"
    if "тумашенко" in s:
        return "tumashenko"
    if "кудерко" in s:
        return "kuderko"
    return ""


def _selected_slepok_key(raw: str) -> str:
    """Strict canonical key from the user's selected slepok field.

    Unlike ``_slepok_key_from_text`` this does not infer a slepok from an
    arbitrary surname. Auto-promo must follow the selected slepok only.
    """
    s = (raw or "").strip().lower()
    if not s:
        return ""
    if s in _SLEPOK_CANONICAL:
        return s
    label = re.sub(r"\s+", "_", s)
    return _SLEPOK_KEY.get(label, "")


# ── Статус контент-пака M3 (живое чтение по sshfs-мосту) ────────────────────────
_M3_KONTENT_ROOT = "/opt/neuro_kontent"  # sshfs-монт папки нейродиректолога с M3
_M3_SEGMENTS = ("Монобренд", "Мультибренд", "Квиз", "Мульти + БУ", "С пробегом")


def _m3_content_status(timeout: float = 8.0) -> dict:
    """Статус контента M3 — теперь по ЛОКАЛЬНОМУ ИНДЕКСУ (структура пака закэширована локально,
    байты тянем точечно с таймаутом). НЕ ходит в sshfs → не виснет даже при перегруженной M3."""
    out = {"ok": False, "mount": "local-index", "segments": [], "coder": False, "detail": ""}
    res: dict = {}

    def _probe():
        try:
            res["status"] = kp.pack_status()
        except Exception as e:  # noqa: BLE001
            res["err"] = str(e)[:80]

    th = threading.Thread(target=_probe, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        out["detail"] = "индекс собирается…"
        return out
    if res.get("err"):
        out["detail"] = "недоступно: " + res["err"]
        return out
    st = res.get("status") or {}
    out["ok"] = bool(st.get("pack_root_exists"))
    out["coder"] = bool(st.get("feeds_exists"))
    out["write_ok"] = False
    out["read_only"] = True
    age = st.get("index_age_sec")
    age_txt = (f"{age // 60} мин назад" if isinstance(age, int) else "—")
    out["segments"] = ["idx"] if out["ok"] else []
    out["detail"] = (f"локальный индекс (обновлён {age_txt}) · фид-моделей {st.get('feeds_models', 0)} · "
                     f"байты точечно с M3 · M3 read-only") if out["ok"] else "индекс ещё не собран"
    return out


_M3_STATUS_CACHE: dict = {"at": 0.0, "data": None}
_M3_STATUS_TTL = 300                                      # кэш статуса M3 ~5 мин (polling 20 мин не дёргает чаще)




# Одна короткая перепроверка ИИ перед фолбэком на слепок (Семён 09.07): раньше при обоих
# лёгших провайдерах гейт висел 6×10мин+1ч и потом останавливал набор — часовой висяк.
# Теперь: одна быстрая перепроверка (вдруг мигнуло), затем продолжаем создание на слепке.
_M3_GATE_RECHECK_SEC = 20


def _m3_gate_wait(job: dict | None = None) -> bool:
    """Гейт создания РК. Провайдеров два (каскад _llm_pair_for): M3 и OpenRouter.
    - M3 жив → True.
    - M3 лёг, OpenRouter жив → True (фолбэк переключит генерацию на OpenRouter сам).
    - Оба легли → НЕ висим 6×10мин+1ч (правило Семёна 09.07): делаем ОДНУ короткую
      перепроверку (_M3_GATE_RECHECK_SEC) на случай моргания и продолжаем создание —
      контент возьмётся из СЛЕПКА (run_gen_campaign_content: assemble_campaign + слепковый
      фолбэк дают titles/texts/sitelinks детерминированно, без LLM).
    True = продолжаем создание (на ИИ или на слепке); False = ТОЛЬКО если джобу отменили."""
    if _m3_llm_probe():
        return True
    if _openrouter_probe():
        print("[m3-gate] M3 недоступен, OpenRouter жив → контент пойдёт через "
              "DeepSeek V4 Flash (платно)", flush=True)
        return True
    # Оба провайдера легли. Короткая перепроверка, затем — на слепок (не висим часами).
    print(f"[m3-gate] M3 и OpenRouter недоступны — короткая перепроверка "
          f"{_M3_GATE_RECHECK_SEC} с перед фолбэком на слепок", flush=True)
    if job is not None:
        job["note"] = "ИИ недоступен (M3+OpenRouter) — перепроверка перед контентом из слепка"
    _t_end = time.time() + _M3_GATE_RECHECK_SEC
    while time.time() < _t_end:
        time.sleep(5)
        try:
            _touch_running_jobs_heartbeat()
        except Exception:  # noqa: BLE001
            pass
        if job is not None and job.get("cancel"):
            return False
    if job is not None:
        job.pop("note", None)
    if _m3_llm_probe() or _openrouter_probe():
        print("[m3-gate] ИИ снова доступен после перепроверки — продолжаем на ИИ", flush=True)
        return True
    print("[m3-gate] ИИ по-прежнему недоступен — НЕ ждём, продолжаем создание на контенте "
          "из слепка (детерминированный фолбэк run_gen_campaign_content)", flush=True)
    return True


_COOKIES_STATUS_CACHE: dict = {"at": 0.0, "data": None}
_COOKIES_STATUS_TTL = 300.0


def _cookies_status_response():
    """Health агентских кук главпотока для бейджа в сайдбаре (под M3). {ok, alive, total, detail}.
    Probe = UacClient.link_info по куке каждого агентства (локальная → главпоток). Живость решает
    deny-лист: протухла ТОЛЬКО если получили однозначный need_reset/редирект в Паспорт; любая другая
    ошибка (в т.ч. «нет прав»/"No rights" на конкретного клиента) = сессия жива. Кэш 5 мин;
    ?force=1 — обход кэша (кнопка «⟳ Обновить»)."""
    now = time.time()
    _force = str(request.args.get("force") or "") in ("1", "true")
    cached = _COOKIES_STATUS_CACHE.get("data")
    if not _force and cached and (now - float(_COOKIES_STATUS_CACHE.get("at") or 0)) < _COOKIES_STATUS_TTL:
        out = dict(cached)
        out["cached"] = True
        return jsonify(out)
    # Probe агентской куки требует КЛИЕНТСКИЙ ulogin: до 5 клиентов на агентство из
    # local_gsheet_sites — ОДИН клиент давал ложные ✗, когда попадался отвязанный
    # (victoryagency14/porg-23yivon2, факт 2026-07-03; victorylotsofads1, факт 2026-07-06 —
    # Яндекс вернул «нет прав» ДВУМЯ разными формами: 403 {"code":54,"text":"Нет прав"} И
    # 401 {"code":0,"text":"No rights"} — allow-лист конкретных текстов не поспевает за формами).
    # Метод сменён на deny-лист: единственный ОДНОЗНАЧНЫЙ сигнал «кука мертва» — редирект в
    # Паспорт/need_reset (`{"code":null,"text":"need_reset","recoveryUrl":".../passport.yandex.ru/..."}`,
    # видели на протухшем `.secret/cookies.json`). Любая ДРУГАЯ ошибка (в т.ч. «нет прав»/"No rights"
    # на конкретного клиента) означает, что сессия АВТОРИЗОВАНА — просто нет доступа к ЭТОМУ ulogin,
    # это не признак протухания.
    # login_key в local_gsheet_sites иногда мусор ("Нет"/"Да"/пустая заглушка вместо реального
    # ulogin) — такой кандидат гарантированно даёт "чужую" ошибку (не "Нет прав"/code 54) и тратит
    # одну из немногих попыток впустую, из-за чего при неудачном порядке выборки (без ORDER BY)
    # аккаунт мог ошибочно попасть в "протухла", хотя кука рабочая. Пускаем в проверку только
    # похожие на реальный ulogin строки (латиница/цифры/дефис/подчёркивание, без кириллицы).
    _UAC_ULOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,}$")
    probe_logins: dict = {}
    try:
        conn = _victory_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT agency_account, login_key FROM public.local_gsheet_sites "
                        "WHERE coalesce(agency_account,'') NOT IN ('','None') "
                        "AND coalesce(login_key,'')<>'' ORDER BY login_key")
            for a, l in cur.fetchall():
                if not _UAC_ULOGIN_RE.match(l or ""):
                    continue
                lst = probe_logins.setdefault(a, [])
                if l not in lst and len(lst) < 5:
                    lst.append(l)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        probe_logins = {}
    parts, alive = [], 0
    accounts = tuple(getattr(cmc, "DEFAULT_COOKIE_ACCOUNTS", ()) or ())
    for acc in accounts:
        acc_ok, src = False, ""
        ulogins = probe_logins.get(acc) or [acc]
        for label, getter in (("локальная", cmc.load_cookie_local),
                              ("главпоток", cmc.fetch_cookie_glavpotok)):
            try:
                cookie = getter(acc)
            except Exception:  # noqa: BLE001
                cookie = None
            if not cookie:
                continue
            cookie_dead = False
            for ulogin in ulogins:
                try:
                    cmc.UacClient(cookie, ulogin).link_info("https://ya.ru")
                    acc_ok = True
                    break
                except Exception as pe:  # noqa: BLE001
                    body = str(getattr(pe, "body", "") or pe)
                    if "need_reset" in body or "Истек срок" in body or "Истёк срок" in body \
                            or "passport.yandex.ru/auth" in body:
                        cookie_dead = True   # ОДНОЗНАЧНЫЙ признак: сессия правда истекла
                        break
                    continue              # любая другая ошибка ("нет прав"/"No rights" и т.п. на
                                          # ЭТОГО клиента) — сессия жива, пробуем следующего ulogin
            if acc_ok:
                src = label
                break
            if not cookie_dead:
                # ни один ulogin не дал явного успеха, но и признака протухания не было —
                # кука отвечает предметно (не редиректит в Паспорт), считаем живой
                acc_ok, src = True, label
                break
        alive += 1 if acc_ok else 0
        parts.append(f"{acc}: {'✓ ' + src if acc_ok else '✗ протухла'}")
    dead = [p.split(":")[0] for p in parts if "✗" in p]
    data = {"ok": alive > 0, "alive": alive, "total": len(accounts), "dead": dead,
            "detail": " · ".join(parts), "checked_at": time.strftime("%H:%M")}
    _COOKIES_STATUS_CACHE["at"] = now
    _COOKIES_STATUS_CACHE["data"] = dict(data)
    data["cached"] = False
    return jsonify(data)


def _m3_status_response():
    """Лёгкий health M3 для ПОСТОЯННОГО индикатора в сайдбаре. {ok, detail, checked_at}. Кэш ~5 мин.
    ok = контент-индекс (sshfs) И LLM-эндпоинт живы — для генерации РК нужны ОБА (см. _m3_llm_probe).
    ?force=1 — обход кэша (кнопка «⟳ Обновить» на бейдже: без него клик возвращал старый статус)."""
    now = time.time()
    _force = str(request.args.get("force") or "") in ("1", "true")
    cached = _M3_STATUS_CACHE.get("data")
    if not _force and cached and (now - float(_M3_STATUS_CACHE.get("at") or 0)) < _M3_STATUS_TTL:
        out = dict(cached)
        out["cached"] = True
        return jsonify(out)
    st = _m3_content_status(timeout=6.0)
    content_ok = bool(st.get("ok"))
    llm_ok = _m3_llm_probe()
    detail = st.get("detail") or ""
    if content_ok and not llm_ok:
        detail = "⚠ LLM недоступна (mlx 8082 молчит) — генерация РК не пойдёт · " + detail
    from datetime import datetime
    out = {"ok": content_ok and llm_ok, "detail": detail,
           "checked_at": datetime.now().strftime("%H:%M"), "cached": False}
    _M3_STATUS_CACHE["at"] = now
    _M3_STATUS_CACHE["data"] = {k: out[k] for k in ("ok", "detail", "checked_at")}
    return jsonify(out)


# ── Резолвер контента группы из пака M3 (по нашему ct) ──────────────────────────
def _gc_ct(gc: str) -> str:
    """Первый ctNNNN из кодера группы (gc) = ag_part1 = бренд/модель."""
    m = re.search(r"ct\d{4}", gc or "")
    return m.group(0) if m else ""


register_content_routes(
    bp,
    _direct_access,
    kp=kp,
    m3_content_status=_m3_content_status,
    content_rules_map=_content_rules_map,
    content_rule_key=_content_rule_key,
    dedupe_content_assets_for_ui=_dedupe_content_assets_for_ui,
    content_rules_ensure=_content_rules_ensure,
    gc_ct=_gc_ct,
    victory_conn_rw=_victory_conn_rw,
    content_rules_cache=_CONTENT_RULES_CACHE,
)


_CT_MODEL_CACHE: dict | None = None


def _ct_is_model_map() -> dict:
    """ct → True если МОДЕЛЬ (бренд+модель), False если МАРКА или ТЕМА.

    Модель = существует более короткое имя ag_part1, являющееся СЛОВЕСНЫМ префиксом
    данного («BAIC» → «BAIC X35», «Great Wall» → «Great Wall Poer»). Бренды («BAIC»)
    и темы («Авито», «Автокредит/кредит», «Седаны», «кластер запросов…») своего
    бренда-префикса не имеют → Марки. Источник — gsheet_naming(ag_part1), кэш на процесс.
    Это РОВНО та раскладка, что в боевых аккаунтах Щербаковой (РСЯ-Марки / РСЯ-Модели)."""
    global _CT_MODEL_CACHE
    if _CT_MODEL_CACHE is not None:
        return _CT_MODEL_CACHE
    low = {ct: (nm or "").strip().lower() for ct, nm in _ag_part1_map().items()}
    vals = set(low.values())
    out: dict = {}
    for ct, ln in low.items():
        toks = ln.split()
        out[ct] = any(" ".join(toks[:i]) in vals for i in range(1, len(toks)))
    _CT_MODEL_CACHE = out
    return out


_CT_SEG_CACHE: dict | None = None
def _ct_segment_map() -> dict:
    """ct → сегмент: 'Модели' | 'Марки' | 'Общее' (как в БОЕВЫХ аккаунтах: Поиск/РСЯ делятся на
    Марки / Модели / Общее). Робастная классификация по справочнику gsheet_naming(ag_part1):
      • БРЕНД (Марки)  = слово ведёт ≥2 модельных имён ИЛИ есть как одиночная категория и ведёт ≥1
        (ловит и бренды без отдельной ct-категории: «Jac», «Solaris»).
      • МОДЕЛЬ (Модели) = многословное имя, чьё ПЕРВОЕ слово — бренд («BAIC X35», «Jac J7»).
      • ТЕМА (Общее)   = не бренд и не модель («Авито», «Автосалон/салон/Дилер», «Авто/Автомобили»).
    Кэш на процесс."""
    global _CT_SEG_CACHE
    if _CT_SEG_CACHE is not None:
        return _CT_SEG_CACHE
    from collections import Counter
    low = {ct: (nm or "").strip().lower() for ct, nm in _ag_part1_map().items()}
    lead: Counter = Counter()
    single: set = set()
    for ln in low.values():
        parts = ln.split()
        if len(parts) >= 2:
            lead[parts[0]] += 1
        elif ln:
            single.add(ln)

    def _is_brand(tok: str) -> bool:
        return lead.get(tok, 0) >= 2 or (lead.get(tok, 0) >= 1 and tok in single)

    out: dict = {}
    for ct, ln in low.items():
        parts = ln.split()
        if len(parts) >= 2 and _is_brand(parts[0]):
            out[ct] = "Модели"
        elif ln and _is_brand(ln):
            out[ct] = "Марки"
        else:
            out[ct] = "Общее"
    _CT_SEG_CACHE = out
    return out


def _ct_segment(ct: str) -> str:
    """Сегмент группы по её ct/кодеру: 'Модели' | 'Марки' | 'Общее' (единый источник — _ct_segment_map)."""
    return _ct_segment_map().get(_gc_ct(ct), "Марки")


def _seg_canon(s: str) -> str:
    """Канон сегмента для сверки классификатора с профилем: общие темы → 'общая'
    (классификатор даёт «Общее», профиль из живых имён — «общая»/«Общие запросы»)."""
    s = (s or "").strip().lower()
    return "общая" if s.startswith("общ") else s


def _model_cts() -> list:
    """Список модельных ct (совместимость; новый единый источник — _ct_segment_map)."""
    return [ct for ct, seg in _ct_segment_map().items() if seg == "Модели"]


# Слепок-донор сегмента: если у целевого слепка НЕТ своих ct сегмента (напр. Терехов tp4 без
# «Моделей») — берём структуру и контент сегмента у донора («как в других слепках»). Щербакова —
# самый полный модельный слепок (tp4 = 138 модельных ct). Расширяемо при необходимости.
_SEGMENT_DONORS = {"Модели": ["scherbakova"]}


def _segment_donor(segment: str, tp_code: str, site_type: str, exclude: str = "") -> str | None:
    """Первый донор, у которого ЕСТЬ ct данного сегмента для (tp_code, site_type). Иначе None."""
    for donor in _SEGMENT_DONORS.get(segment, []):
        if donor == exclude:
            continue
        if any(_ct_segment(ct) == segment for ct in _struct_cts(donor, site_type, tp_code)):
            return donor
    return None


_TARGETING_PROFILE_CACHE: dict | None = None


def _targeting_profile() -> dict:
    """Профиль таргетинга слепков из боевых аккаунтов: {slepok:{site_type:{tp:{segment:{mode:cnt}}}}}.
    Источник — targeting_profile.json (сгенерён из raw_grid). Кэшируется."""
    global _TARGETING_PROFILE_CACHE
    if _TARGETING_PROFILE_CACHE is None:
        _TARGETING_PROFILE_CACHE = _json("targeting_profile.json") or {}
    return _TARGETING_PROFILE_CACHE


def _slepok_tp_modes(slepok: str, site_type: str, tp: str, segment: str) -> list | None:
    """Какие режимы таргетинга (КС/Автотаргет) реально ведёт слепок для (site_type, tp, segment).

    None  → нет данных (слепка нет в профиле ИЛИ этого tp нет у слепка) → дефолт (как раньше).
    []    → tp у слепка ЕСТЬ, но именно ЭТОГО сегмента нет → НЕ строить (гейт-вниз, «не лишнее»).
    [...] → строить ровно эти режимы (в порядке КС, Автотаргет).
    """
    skey = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    prof = _targeting_profile()
    if skey not in prof:
        return None
    tps = prof.get(skey, {}).get(site_type, {}) or {}
    if tp not in tps:                       # нет данных по этому tp у слепка → дефолт, не гейт
        return None
    # Сегмент сверяем КАНОНИЧЕСКИ: «Общее» (классификатор) ↔ «общая» (профиль из живых имён).
    seg_tps = tps.get(tp, {}) or {}
    sc = _seg_canon(segment)
    modes = next((v for k, v in seg_tps.items() if _seg_canon(k) == sc), {}) or {}
    return [m for m in ("КС", "Автотаргет") if m in modes]


def _slepok_profile_excludes_tp(slepok: str, site_type: str, tp: str) -> bool:
    """True, если у слепка ЕСТЬ боевой профиль для site_type, но данного tp в нём НЕТ.

    Смысл — «строгое соответствие набору слепка» (баг porg-psm5h7q6: просочился tp4).
    Профиль (targeting_profile.json) — слепок РЕАЛЬНЫХ боевых аккаунтов; если он есть, он
    АВТОРИТЕТЕН по составу типов. Структура (slepki_structure.json) может содержать tp для
    ДОНОРСКИХ целей (напр. scherbakova держит tp4 как донор «Моделей» для др. слепков,
    _SEGMENT_DONORS), но сам слепок его не ведёт → строить его в СВОЁМ аккаунте нельзя.
    Слепка/типа сайта нет в профиле → False (профиль не авторитетен, поведение как раньше —
    не ломаем слепки без профиля, напр. Терехов).
    """
    skey = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    st = _targeting_profile().get(skey, {}).get(site_type)
    if not st:
        return False
    return tp not in st


def _slepki_structure_for_ui() -> dict:
    """Копия slepki_structure для чекбоксов набора в UI с ФИЛЬТРОМ по боевому профилю:
    у слепка, у которого есть targeting_profile для site_type, скрываем tp, которых в профиле
    НЕТ (донорские tp — напр. scherbakova держит tp4 «Модели» как донор, но в СВОЙ аккаунт его
    не ведёт; gate _slepok_profile_excludes_tp его всё равно молча режет → нельзя предлагать в UI).
    Слепок без профиля (напр. Терехов) — не трогаем, tp остаются (он реально их создаёт).
    ВАЖНО: донорская логика (_donor_tp4_models_map / _segment_donor / _struct_cts) читает
    slepki_structure.json С ДИСКА напрямую — этот фильтр её НЕ затрагивает."""
    import copy
    out = copy.deepcopy(_json("slepki_structure.json"))
    for d in out.get("directologists", []):
        key = d.get("key") or ""
        for st in d.get("site_types", []):
            stype = st.get("name") or ""
            st["tp"] = [t for t in st.get("tp", [])
                        if not _slepok_profile_excludes_tp(key, stype, t.get("code") or "")]
    return out


def _donor_tp4_models_map() -> dict:
    """{slepok_key: [site_type,...]} — где у слепка НЕТ своих tp4-«Моделей», но донор их покрывает.
    UI по этой карте показывает донорский чекбокс «Модели» для tp4 (напр. Терехов)."""
    out: dict = {}
    for d in _json("slepki_structure.json").get("directologists", []):
        key = d.get("key")
        if not key:
            continue
        for st in d.get("site_types", []):
            stype = st.get("name")
            if not any(t.get("code") == "tp4" for t in st.get("tp", [])):
                continue
            own_models = any(_ct_segment(ct) == "Модели" for ct in _struct_cts(key, stype, "tp4"))
            if not own_models and _segment_donor("Модели", "tp4", stype, exclude=key):
                out.setdefault(key, []).append(stype)
    return out


def _pack_for_item(slepok: str, site_type: str, tp: str, gc: str) -> dict:
    """Контент пака для одной группы набора (по нашему ct из gc).

    → {ct, model, keywords, minus, callouts, images, from}.
    ct0000/пусто → from='fallback' (берём корпус слепка вне пака)."""
    ct = _gc_ct(gc)
    kw = kp.read_keywords(site_type, tp, ct, slepok)
    co = kp.read_callouts(site_type, tp, ct, slepok)
    im = kp.read_images(site_type, tp, ct)
    has = bool(kw["positive"] or kw["minus"] or co or im)
    return {"ct": ct, "model": kp.feeds_ct_model().get(ct, ""),
            "keywords": kw["positive"], "minus": kw["minus"],
            "callouts": co, "images": im,
            "from": "pack" if has else "fallback"}


def _pack_preview_response():
    """Предпросмотр: что именно мы возьмём из пака M3 для слепка×типа сайта.
    ?slepok=<key|Слепок_Имя>&site_type=<сегмент>. Read-only, ничего не создаёт."""
    raw = (request.args.get("slepok") or "").strip()
    slepok = _SLEPOK_KEY.get(raw.lower(), raw.lower())
    site_type = (request.args.get("site_type") or "").strip()
    out = {"slepok": slepok, "site_type": site_type, "tp": [],
           "totals": {"keywords": 0, "minus": 0, "callouts": 0, "images": 0,
                      "groups": 0, "groups_from_pack": 0}}
    if not slepok or not site_type:
        return jsonify({**out, "error": "slepok и site_type обязательны"})
    struct = _json("slepki_structure.json").get("directologists", [])
    dirr = next((d for d in struct if d.get("key") == slepok), None)
    if not dirr:
        return jsonify({**out, "error": f"слепок '{slepok}' не найден в структуре"})
    st = next((s for s in dirr.get("site_types", []) if s.get("name") == site_type), None)
    if not st:
        return jsonify({**out, "error": f"тип сайта '{site_type}' нет у слепка"})
    T = out["totals"]
    for tp in st.get("tp", []):
        tp_code = tp.get("code", "")
        tp_out = {"code": tp_code, "title": tp.get("title", ""), "groups": []}
        seen_ct = set()
        for grp in tp.get("groups", []):
            for it in grp.get("items", []):
                gc = it.get("gc", "")
                ct = _gc_ct(gc)
                key = (tp_code, ct)
                if key in seen_ct:        # один ct в tp читаем один раз
                    continue
                seen_ct.add(key)
                r = _pack_for_item(slepok, site_type, tp_code, gc)
                T["groups"] += 1
                if r["from"] == "pack":
                    T["groups_from_pack"] += 1
                T["keywords"] += len(r["keywords"])
                T["minus"] += len(r["minus"])
                T["callouts"] += len(r["callouts"])
                T["images"] += len(r["images"])
                tp_out["groups"].append({
                    "ct": ct, "model": r["model"], "tag": it.get("t", ""),
                    "keywords": len(r["keywords"]), "minus": len(r["minus"]),
                    "callouts": len(r["callouts"]), "images": len(r["images"]),
                    "from": r["from"],
                    "sample_kw": r["keywords"][:5],
                })
        if tp_out["groups"]:
            out["tp"].append(tp_out)
    return jsonify(out)


def _slepok_segment_counts_response():
    """Фактические счётчики групп по сегментам из живого M3-пака для слепка×типа сайта.
    ?slepok=<key>&site_type=<name>. Критерий совпадает с реальным созданием: считаем только
    ct у которых есть непустые positive-ключи в паке — ct без ключей пропускаем (как
    create_set_tp1_builders строка «if not data.get(positive): continue»)."""
    raw = (request.args.get("slepok") or "").strip()
    slepok = _SLEPOK_KEY.get(raw.lower(), raw.lower())
    site_type = (request.args.get("site_type") or "").strip()
    if not slepok or not site_type:
        return jsonify({"error": "slepok и site_type обязательны"})
    struct = _json("slepki_structure.json").get("directologists", [])
    dirr = next((d for d in struct if d.get("key") == slepok), None)
    if not dirr:
        return jsonify({"error": f"слепок '{slepok}' не найден"})
    st = next((s for s in dirr.get("site_types", []) if s.get("name") == site_type), None)
    if not st:
        return jsonify({"error": f"тип сайта '{site_type}' нет у слепка"})
    seg_map = _ct_segment_map()
    counts: dict = {}
    for tp_entry in st.get("tp", []):
        tp_code = tp_entry.get("code", "")
        m = re.match(r"^tp(\d+)$", tp_code)
        if not m:
            continue
        tpn = int(m.group(1))
        if tpn not in (1, 2, 4, 5):   # только сегментные tp (как _isSegmentTp в UI)
            continue
        pack = kp.gather(slepok, site_type, tp_code)
        seg_counts: dict = {}
        for ct, data in pack.items():
            if not data.get("positive"):
                continue               # тот же пропуск что при реальном создании
            seg = seg_map.get(ct, "Марки")
            seg_counts[seg] = seg_counts.get(seg, 0) + 1
        # pack_ok=False означает что gather вернул пустой результат (M3 недоступен или пак пуст).
        # Фронт использует это для защиты: при pack_ok=False не патчит счётчики → остаётся статика.
        counts[tp_code] = {"segs": seg_counts, "pack_ok": bool(pack)}
    return jsonify({"slepok": slepok, "site_type": site_type, "counts": counts})


register_pack_routes(
    bp,
    _direct_access,
    victory_conn=_victory_conn,
    slepok_key_map=_SLEPOK_KEY,
    callout_limits={
        "max_each": _CALLOUT_MAX_EACH,
        "max_total_desktop": _CALLOUT_MAX_TOTAL_DESKTOP,
        "max_total_mobile": _CALLOUT_MAX_TOTAL_MOBILE,
    },
    m3_content_status=_m3_content_status,
    m3_status_response=_m3_status_response,
    pack_preview_response=_pack_preview_response,
    slepok_segment_counts_response=_slepok_segment_counts_response,
    cookies_status_response=_cookies_status_response,
)


# ── Автоподстановка значений из БД (тип сайта/город/счётчик/цель/тексты) ────────

_GEO_LOCK = threading.Lock()
_GEO_BY_NAME: dict = {}                       # lower(имя региона) → GeoRegionId (словарь Директа, кэш)


def _geo_load() -> dict:
    """Словарь GeoRegions Директа (имя→id), грузится один раз на процесс."""
    global _GEO_BY_NAME
    if _GEO_BY_NAME:
        return _GEO_BY_NAME
    with _GEO_LOCK:
        if _GEO_BY_NAME:
            return _GEO_BY_NAME
        import requests as _rqs
        tok = next(iter(_direct_tokens().values()), None)
        if not tok:
            return {}
        try:
            r = _rqs.post(_V5 + "dictionaries",
                          headers={"Authorization": "Bearer " + tok, "Accept-Language": "ru",
                                   "Content-Type": "application/json; charset=utf-8"},
                          json={"method": "get", "params": {"DictionaryNames": ["GeoRegions"]}}, timeout=60)
            geos = (r.json().get("result") or {}).get("GeoRegions", [])
        except Exception:  # noqa: BLE001
            return {}
        d: dict = {}
        for g in geos:                        # города идут раньше областей — приоритет точному совпадению
            nm = (g.get("GeoRegionName") or "").strip().lower()
            if nm and nm not in d:
                d[nm] = g.get("GeoRegionId")
        _GEO_BY_NAME = d
        return d


def _geo_id(city: str | None, region: str | None):
    """city → id (приоритет), иначе region → id. Возвращает (id, имя) или (None, None)."""
    d = _geo_load()
    for nm in (city, region):
        if nm:
            gid = d.get(nm.strip().lower())
            if gid:
                return gid, nm.strip()
    return None, None


def _metrika_token() -> str | None:
    sd = str(cmc._find_secret_dir())
    if sd not in sys.path:
        sys.path.insert(0, sd)
    from loader import load_yandex_metrika  # noqa: E402
    m = load_yandex_metrika()
    return m.get("oauth_token") if isinstance(m, dict) else None


def _goal_vse_formy(counter_id: int | None):
    """Цель «Все формы» счётчика Метрики → (goal_id, name) или (None, None)."""
    tok = _metrika_token()
    if not tok or not counter_id:
        return None, None
    import requests as _rqs
    try:
        r = _rqs.get(f"https://api-metrika.yandex.net/management/v1/counter/{counter_id}/goals",
                     headers={"Authorization": "OAuth " + tok}, timeout=30)
        if r.status_code != 200:
            return None, None
        for g in r.json().get("goals", []):
            if "все формы" in (g.get("name") or "").strip().lower():
                return g.get("id"), g.get("name")
    except Exception:  # noqa: BLE001
        return None, None
    return None, None


def _account_prefill_response():
    """Значения для формы по логину: href/тип сайта/регион/счётчик/цель «Все формы»/тексты из БД."""
    import psycopg2.extras
    login = (request.args.get("login") or "").strip()
    if not login:
        return jsonify({"error": "login обязателен"}), 400

    conn = _victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT domain, city, region, site_type, counter_number, agency_account "
                    "FROM public.local_gsheet_sites WHERE login_key=%s AND direction='Авто' LIMIT 1", (login,))
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"error": f"аккаунт {login} не найден в local_gsheet_sites (Авто)"}), 404

    warnings: list[str] = []
    domain = (row["domain"] or "").strip()
    site_type = (row["site_type"] or "").strip()
    cc = (row["counter_number"] or "").strip()
    counter_id = int(cc) if cc.isdigit() else None

    # Счётчик/цель из metrika_goals (Victory): если в таблице сайтов счётчик не
    # заполнен — берём counter_ids; цель goal_id — из all_forms этой же таблицы.
    mg = _metrika_goals_for(login)
    counter_options = mg["counters"] if mg else []
    if not counter_id and counter_options:
        counter_id = counter_options[0]
    if not counter_id:
        warnings.append("счётчик Метрики не найден ни в таблице, ни в metrika_goals")

    # Резолвим geoid ОБЛАСТИ (не города): city → Область через БД → geoid словаря Директа.
    # Та же логика что _account_ctx (create_set_context.py); для мультигород-аккаунтов → 225.
    acc_ctx = _account_ctx(login) or {}
    region_id = acc_ctx.get("geoid") or 225
    region_used = acc_ctx.get("oblast") or ("Россия" if region_id == 225 else None)
    if region_id == 225 and row.get("city"):
        warnings.append("регион не распознан по городу — поставил Россия (225)")

    # goal_id: приоритет — all_forms из metrika_goals; иначе цель «Все формы» из API Метрики
    goal_id = mg["goal_id"] if mg else None
    goal_name = "Все формы" if goal_id else None
    if not goal_id and counter_id:
        goal_id, goal_name = _goal_vse_formy(counter_id)
    if counter_id and not goal_id:
        warnings.append("цель «Все формы» не найдена (нет в metrika_goals и в счётчике)")

    titles: list[str] = []
    texts: list[str] = []
    if site_type:
        c2 = _victory_conn()
        try:
            cur = c2.cursor()
            cur.execute("SELECT kind, content FROM public.direct_ad_templates "
                        "WHERE enabled AND site_type=%s ORDER BY kind, id", (site_type,))
            for kind, content in cur.fetchall():
                (titles if kind == "title" else texts).append(content)
        finally:
            c2.close()
    if site_type and not titles and not texts:
        warnings.append(f"нет шаблонных текстов для типа сайта «{site_type}»")

    # Правила РК по (site_type, city аккаунта) с фолбэком на (site_type, '*')
    rule_goal_type = rule_cpa = rule_budget = rule_adjustment_pct = None
    acc_city = (row.get("city") or "").strip()
    if site_type:
        c3 = _victory_conn()
        try:
            cur = c3.cursor()
            r_rule = None
            # Приоритет: правило для конкретного города аккаунта
            if acc_city:
                cur.execute("SELECT goal_type, cpa::numeric, budget::numeric, adjustment_pct "
                            "FROM public.direct_automation_rules "
                            "WHERE site_type=%s AND city=%s LIMIT 1", (site_type, acc_city))
                r_rule = cur.fetchone()
            # Фолбэк: дефолтное правило (city='*')
            if not r_rule:
                cur.execute("SELECT goal_type, cpa::numeric, budget::numeric, adjustment_pct "
                            "FROM public.direct_automation_rules "
                            "WHERE site_type=%s AND city='*' LIMIT 1", (site_type,))
                r_rule = cur.fetchone()
            if r_rule:
                rule_goal_type = r_rule[0]
                rule_cpa = float(r_rule[1])
                rule_budget = float(r_rule[2])
                rule_adjustment_pct = int(r_rule[3])
        except Exception:  # noqa: BLE001  — таблица может отсутствовать в dev-окружении
            pass
        finally:
            c3.close()

    resp: dict = {
        "login": login, "domain": domain, "href": ("https://" + domain) if domain else "",
        "site_type": site_type, "city": row.get("city"), "region": row.get("region"),
        "region_id": region_id, "region_used": region_used,
        "counter_id": counter_id, "counter_options": counter_options,
        "goal_id": goal_id, "goal_name": goal_name,
        "titles": titles, "texts": texts, "agency": row.get("agency_account"), "warnings": warnings,
    }
    if rule_goal_type is not None:
        resp["rule_goal_type"] = rule_goal_type
        resp["rule_cpa"] = rule_cpa
        resp["rule_budget"] = rule_budget
        resp["rule_adjustment_pct"] = rule_adjustment_pct
    return jsonify(resp)


def _campaigns_response():
    """Кампании аккаунта (офиц. v5 campaigns.get): id + название + статус."""
    login = (request.args.get("login") or "").strip()
    agency = (request.args.get("agency") or "").strip()
    if not login:
        return jsonify({"error": "login обязателен"}), 400
    token, ag = _token_for_login(login, agency, _direct_tokens())
    if not token:
        return jsonify({"error": "нет рабочего агентского токена для этого логина", "campaigns": []})
    j = _v5_get("campaigns", token, login, ["Id", "Name", "Type", "State", "Status"], criteria={})
    # v5-чтение стоит баллов: при 152 (нет баллов) НЕ выходим с ошибкой — список добираем по
    # КУКЕ через Grid (без баллов), как «Показать РК» и должно работать на исчерпанном аккаунте.
    v5_err = j["error"].get("error_string") if "error" in j else None
    camps = ([] if v5_err else
             [{"id": c["Id"], "name": c.get("Name"), "type": c.get("Type"),
               "state": c.get("State"), "status": c.get("Status"), "src": "v5"}
              for c in (j.get("result") or {}).get("Campaigns", [])])
    # Grid видит ВСЕ типы (text/unified/UAC) — добираем всё, чего нет в v5 (без дублей).
    # Это и есть «часть по апи (v5) + часть по куки (grid)». Статус мапим из primaryStatus/archived,
    # иначе архивная/черновик показывались как «идёт» (была эта ошибка).
    _GRID_STATE = {"DRAFT": "DRAFT", "ARCHIVED": "ARCHIVED", "ENDED": "ENDED",
                   "STOPPED": "SUSPENDED", "SUSPENDED": "SUSPENDED", "PAUSED": "SUSPENDED"}
    uac_added = 0
    grid_err = None
    try:
        seen = {str(c["id"]) for c in camps}
        for g in _grid_list_campaigns(login):
            if str(g.get("id")) in seen:
                continue
            gstatus = (g.get("status") or "").upper()
            state = "ARCHIVED" if g.get("archived") else _GRID_STATE.get(gstatus, "ON")
            camps.append({"id": g["id"], "name": g.get("name"), "type": g.get("typename"),
                          "state": state, "status": g.get("status"), "src": "grid"})
            uac_added += 1
    except Exception as e:  # noqa: BLE001 — grid недоступен (часто протухшая кука) → показываем хотя бы v5
        grid_err = str(e)
    camps.sort(key=lambda c: (_STATE_ORDER.get(c["state"], 9), str(c["name"] or "")))
    out = {"login": login, "agency": ag, "campaigns": camps, "uac_added": uac_added}
    if v5_err:
        # v5 не отдал (обычно 152 — нет баллов): список добираем по куке (Grid). Если и Grid пуст —
        # причина чаще НЕ баллы, а ПРОТУХШАЯ кука на главпотоке (need_reset) → показываем именно это,
        # иначе «Недостаточно баллов» вводит в заблуждение (видно на скрине Семёна).
        if camps:
            out["note"] = f"баллы исчерпаны ({v5_err}) — список по куке (Grid); текстовые/РСЯ из v5 могут быть не все"
        elif grid_err and any(s in grid_err for s in ("протухла", "need_reset", "Истек", "Истёк")):
            out["error"] = f"баллы исчерпаны + кука протухла на главпотоке: {grid_err[:240]}"
        elif grid_err:
            out["error"] = f"{v5_err} (кука тоже не отдала список: {grid_err[:140]})"
        else:
            # Grid отработал БЕЗ ошибки и отдал 0 кампаний → аккаунт реально пуст (напр. после
            # «Удалить черновики»). Красная «Недостаточно баллов» тут вводила в заблуждение
            # (скрин Семёна 03.07 #84) — это не ошибка чтения, а честная пустота.
            out["note"] = f"кампаний в аккаунте нет (проверено по куке/Grid); v5 недоступен: {v5_err}"
    return jsonify(out)


def _stop_all_response():
    """Остановить ВСЕ активные (State=ON) кампании аккаунта через v5 campaigns.suspend.

    Тело: {"login": "...", "agency": "..."}. Обратимо (resume в Директе)."""
    body = request.json or {}
    login = (body.get("login") or "").strip()
    agency = (body.get("agency") or "").strip()
    if not login:
        return jsonify({"error": "login обязателен"}), 400

    ok, reason, wait = _pull_begin(f"stopall:{login}", 15.0)
    if not ok:
        return _busy_response(reason, wait)
    try:
        token, ag = _token_for_login(login, agency, _direct_tokens())
        if not token:
            return jsonify({"error": "нет рабочего агентского токена для этого логина"})
        jg = _v5_call("campaigns", "get", token, login,
                      {"SelectionCriteria": {"States": ["ON"]}, "FieldNames": ["Id", "Name", "Type"]})
        if "error" in jg:
            return jsonify({"error": _v5_err(jg)})
        camps = (jg.get("result") or {}).get("Campaigns", [])
        if not camps:
            return jsonify({"ok": True, "stopped": 0, "total": 0,
                            "message": "активных (ON) кампаний нет — останавливать нечего"})
        # Мастер кампании (UNIFIED_CAMPAIGN) v5 не глушит — стопаем нативным UAC API (куки).
        unified = [c["Id"] for c in camps if c.get("Type") == "UNIFIED_CAMPAIGN"]
        standard = [c["Id"] for c in camps if c.get("Type") != "UNIFIED_CAMPAIGN"]
        stopped, by_v5, by_uac, errors = 0, 0, 0, []

        for i in range(0, len(standard), 100):       # обычные → v5 suspend
            js = _v5_call("campaigns", "suspend", token, login,
                          {"SelectionCriteria": {"Ids": standard[i:i + 100]}})
            if "error" in js:
                errors.append(_v5_err(js))
                continue
            for rr in (js.get("result") or {}).get("SuspendResults", []):
                if rr.get("Id") and not rr.get("Errors"):
                    stopped += 1
                    by_v5 += 1
                elif rr.get("Errors"):
                    errors.append(str(rr["Errors"])[:120])

        if unified:                                   # Мастер → UAC set_status=stopped (куки)
            try:
                uac = cmc.build_client(login, account=(ag or None))
                uac.link_info("https://ya.ru")        # bootstrap CSRF
                for cid in unified:
                    try:
                        uac.set_status(str(cid), "stopped")
                        stopped += 1
                        by_uac += 1
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"мастер {cid}: {str(e)[:80]}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"UAC-куки недоступны: {str(e)[:90]}")

        return jsonify({"ok": True, "stopped": stopped, "total": len(camps),
                        "by_v5": by_v5, "by_uac": by_uac, "masters": len(unified),
                        "errors": errors[:5]})
    finally:
        _pull_end(f"stopall:{login}")


def _grid_list_campaigns(login: str, only_draft: bool = False) -> list[dict]:
    """ВСЕ кампании клиента через Grid API (куки) — включая UAC (Мастер tp6 / Товарка tp7),
    которые НЕВИДИМЫ в v5. → [{id, name, typename, status, archived}]. only_draft → только DRAFT.
    Заменяет битый GET /web-api/uac/campaigns (HTTP 405). Служит «Показать РК» (полный список)
    и удалению UAC-черновиков. primaryStatus='DRAFT' — признак черновика (проверено live)."""
    import requests as _rqs
    import re as _re
    cookie = cmc.pick_working_cookie(login)
    if not cookie:
        raise RuntimeError("нет рабочей куки для grid")
    sess = _rqs.Session()
    sess.verify = False
    csrf = {"t": None}

    def _g(op, q, var):
        h = {"Cookie": cookie, "dna-operation-name": op, "x-direct-api": "1",
             "x-detected-locale": "ru", "Content-Type": "application/json", "User-Agent": cmc.USER_AGENT,
             "Origin": "https://direct.yandex.ru",
             "Referer": f"https://direct.yandex.ru/dna/grid/campaigns?ulogin={login}"}
        if csrf["t"]:
            h["x-csrf-token"] = csrf["t"]
        r = sess.post(f"{_GRID_URL}?operationName={op}&ulogin={login}",
                      json={"operationName": op, "query": q, "variables": var}, headers=h, timeout=40)
        if r.status_code == 403:
            m = _re.search(r"_direct_csrf_token=([^;,\s]+)", r.headers.get("Set-Cookie", ""))
            t = r.cookies.get("_direct_csrf_token") or (m.group(1) if m else None)
            if t:
                csrf["t"] = t
                h["x-csrf-token"] = t
                r = sess.post(f"{_GRID_URL}?operationName={op}&ulogin={login}",
                              json={"operationName": op, "query": q, "variables": var}, headers=h, timeout=40)
        return r

    _g("Callouts", "query Callouts($login:String!){callouts(input:{searchBy:{login:$login}"
       "filter:{deleted:false}}){id}}", {"login": login})           # bootstrap CSRF
    Q = ("query C($login:String!,$inp:GdCampaignsContainerInput!){client(searchBy:{login:$login}){"
         "campaigns(input:$inp){rowset{id name __typename status{primaryStatus archived}}}}}")
    out: list[dict] = []
    offset = 0
    while True:
        inp = {"filter": {}, "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
               "limitOffset": {"limit": 200, "offset": offset}, "orderBy": [{"order": "ASC", "field": "STATUS"}]}
        d = _g("C", Q, {"login": login, "inp": inp}).json()
        if d.get("errors"):
            raise RuntimeError("grid campaigns: " + json.dumps(d["errors"], ensure_ascii=False)[:200])
        rs = (((d.get("data") or {}).get("client") or {}).get("campaigns") or {}).get("rowset") or []
        for c in rs:
            st = (c.get("status") or {})
            out.append({"id": c.get("id"), "name": c.get("name"), "typename": c.get("__typename"),
                        "status": st.get("primaryStatus") or "", "archived": bool(st.get("archived"))})
        if len(rs) < 200:
            break
        offset += 200
    return [c for c in out if c["status"] == "DRAFT"] if only_draft else out


# Имя кампании, созданной ЭТИМ модулем, всегда начинается с кодера tpN_{cpc|cpa}_{site|kviz}_…
# (ЕПК tp1–tp5 и UAC tp6/tp7 — см. _uac_campaign_name / _tp1_group_name). Ручные/чужие кампании
# этому шаблону НЕ соответствуют → удаление черновиков НЕ должно их трогать (защита от сноса чужого).
_TOOL_CAMPAIGN_RE = re.compile(r"^\s*tp\d+_(cpc|cpa)_(site|kviz)[\s_—–]", re.IGNORECASE)


def _is_tool_campaign(name: str | None) -> bool:
    """True, если имя кампании похоже на созданное этим сервисом (кодер tpN_{cpc|cpa}_{site|kviz}_…)."""
    return bool(_TOOL_CAMPAIGN_RE.match(str(name or "")))


def _delete_drafts_core(login: str, agency: str, job: dict | None = None) -> dict:
    """Ядро удаления черновиков (DRAFT) аккаунта, СОЗДАННЫХ ЭТИМ МОДУЛЕМ (по кодеру в имени).
    Чужие/ручные DRAFT-кампании НЕ трогаются (фильтр _is_tool_campaign) — защита от сноса чужого.
    Используется и синхронным эндпоинтом,
    и воркером общей очереди (job ≠ None → прогресс done/created в карточке очереди).

    DRAFT-кампании делятся на два слоя:
    - ЕПК (tp1–tp5, UNIFIED_CAMPAIGN через v5): видны в v5 с State=OFF + Status=DRAFT → v5 delete.
    - UAC (tp6 Мастер, tp7 Товарка): НЕВИДИМЫ в v5; список даёт Grid (_grid_list_campaigns),
      удаляем через DELETE /web-api/uac/campaign/{id}/ (DRAFT удаляется напрямую).
    НЕОБРАТИМО. → {ok, deleted, by_v5, by_uac, errors, created, failed, kind}."""
    token, ag = _token_for_login(login, agency, _direct_tokens())
    if not token:
        return {"ok": False, "error": "нет рабочего агентского токена для этого логина",
                "created": 0, "failed": 1, "deleted": 0, "kind": "delete_drafts"}

    deleted, by_v5, by_uac, by_cookie, errors = 0, 0, 0, 0, []
    skipped_foreign = 0                                   # чужие/ручные DRAFT — НЕ наши, не удаляем
    v5_done: set = set()                                  # id, удалённые через v5 (чтобы слой 2 их не трогал)
    epk_cookie: list = []                                 # ЕПК-черновики на удаление ПО КУКЕ (v5 не смог: 152)
    done = 0

    def _adv(ok: bool):                                   # прогресс джобы: created/failed + done
        nonlocal done
        done += 1
        _bump_job(job, ok)
        if job is not None:
            job["done"] = done
            _job_db_progress(job)

    # --- Слой 1 (сбор): ЕПК через v5 (State=OFF + Status=DRAFT, не UNIFIED) ---
    # v5-чтение стоит баллов: при 152 (исчерпан лимит) НЕ падаем — добираем список по куке (Grid)
    # и дальше удаляем тоже по куке (молча). Пользователю об этом не сообщаем — только финал.
    draft_v5: list = []
    _units_out = False
    jg = _v5_call("campaigns", "get", token, login,
                  {"SelectionCriteria": {"States": ["OFF"]},
                   "FieldNames": ["Id", "Name", "Type", "Status"]})
    if "error" in jg:
        if _is_units_exhausted(_v5_err(jg)):
            _units_out = True                             # баллы кончились → весь путь уходит на куки (молча)
        else:
            errors.append("v5 get: " + _v5_err(jg))
    else:
        _all_v5 = [c for c in (jg.get("result") or {}).get("Campaigns", [])
                   if c.get("Status") == "DRAFT" and c.get("Type") != "UNIFIED_CAMPAIGN"]
        draft_v5 = [c["Id"] for c in _all_v5 if _is_tool_campaign(c.get("Name"))]
        skipped_foreign += len(_all_v5) - len(draft_v5)   # чужие ручные черновики — не трогаем

    # --- Слой 2 (сбор): UAC/ЕПК-черновики через Grid (видит скрытые от v5 Мастер/Товарка) ---
    grid_drafts: list = []
    try:
        _all_grid = [c for c in _grid_list_campaigns(login, only_draft=True)
                     if c.get("id") and int(c["id"]) not in set(draft_v5)]
        grid_drafts = [c for c in _all_grid if _is_tool_campaign(c.get("name"))]
        skipped_foreign += len(_all_grid) - len(grid_drafts)   # чужие → не трогаем
    except Exception as e:  # noqa: BLE001
        errors.append(f"Grid-список недоступен: {str(e)[:90]}")

    # total известен ДО удаления — карточка очереди сразу показывает «обработка набора 0/N»
    if job is not None:
        job["total"] = len(draft_v5) + len(grid_drafts)
        _job_db_progress(job)

    # --- Слой 1 (удаление): пачками по 100 (v5; при 152 чанк уходит на куки) ---
    for i in range(0, len(draft_v5), 100):
        if job is not None and job.get("cancel"):
            break
        chunk = draft_v5[i:i + 100]
        jd = _v5_call("campaigns", "delete", token, login, {"SelectionCriteria": {"Ids": chunk}})
        if "error" in jd:
            if _is_units_exhausted(_v5_err(jd)):
                _units_out = True; epk_cookie.extend(chunk)   # 152 → удалим по куке ниже (молча)
            else:
                errors.append("v5 delete: " + _v5_err(jd))
                for _ in chunk:
                    _adv(False)
            continue
        for rr in (jd.get("result") or {}).get("DeleteResults", []):
            if rr.get("Id") and not rr.get("Errors"):
                deleted += 1; by_v5 += 1; v5_done.add(rr["Id"]); _adv(True)
            else:
                errors.append(str(rr.get("Errors"))[:120]); _adv(False)

    # --- Слой 2 (удаление): роутинг по типу (ЕПК → v5, при 152 → куки; UAC → uac.delete по куке) ---
    uac = None
    for c in grid_drafts:
        if job is not None and job.get("cancel"):
            break
        cid = int(c["id"])
        if cid in v5_done:
            continue
        tn = c.get("typename") or ""
        try:
            if tn == "GdUnifiedCampaign":                # ЕПК — через v5/v501 (при 152 → копим на куки)
                if _units_out:                           # баллы уже кончились → сразу по куке (не тратим вызов)
                    epk_cookie.append(cid); continue
                jd = _v5_call("campaigns", "delete", token, login, {"SelectionCriteria": {"Ids": [cid]}})
                if "error" in jd and _is_units_exhausted(_v5_err(jd)):
                    _units_out = True; epk_cookie.append(cid); continue   # 152 → на куки (молча)
                rr = ((jd.get("result") or {}).get("DeleteResults") or [{}])[0]
                if rr.get("Id") and not rr.get("Errors"):
                    deleted += 1; by_v5 += 1; _adv(True)
                elif _is_units_exhausted(str(rr.get("Errors"))):
                    epk_cookie.append(cid)               # per-id 152 → на куки (молча)
                else:
                    errors.append(f"ЕПК delete {cid}: {(_v5_err(jd) if 'error' in jd else rr.get('Errors'))}"[:120])
                    _adv(False)
            else:                                        # UAC Мастер/Товарка — приватный uac/campaign/{id} (по куке)
                if uac is None:
                    uac = cmc.build_client(login, account=(ag or None))
                    uac.link_info("https://ya.ru")       # bootstrap CSRF
                uac.delete_campaign(str(cid))
                deleted += 1; by_uac += 1; _adv(True)
        except Exception as e:  # noqa: BLE001
            errors.append(f"delete {cid}: {str(e)[:80]}"); _adv(False)

    # --- Слой 3 (фолбэк по куке): ЕПК-черновики, которые v5 не смог удалить из-за 152 (нет баллов).
    # Удаляем через Grid deleteCampaigns на куке агентства — без баллов, молча. Сообщаем только финал.
    if epk_cookie and not (job is not None and job.get("cancel")):
        try:
            cl = gc.GridCreateClient(login)              # сам подберёт рабочую куку агентства для login
            for i in range(0, len(epk_cookie), 100):
                if job is not None and job.get("cancel"):
                    break
                chunk = epk_cookie[i:i + 100]
                res = cl.delete_campaigns(chunk)
                ok_ids = set(res.get("deleted") or [])
                for cid in chunk:
                    if cid in ok_ids:
                        deleted += 1; by_cookie += 1; _adv(True)
                    else:
                        errors.append(f"куки delete {cid}: не удалён"); _adv(False)
        except Exception as e:  # noqa: BLE001
            for cid in epk_cookie:
                errors.append(f"куки delete {cid}: {str(e)[:70]}"); _adv(False)

    return {"ok": True, "deleted": deleted, "by_v5": by_v5, "by_uac": by_uac, "by_cookie": by_cookie,
            "errors": errors[:5], "created": deleted, "failed": len(errors),
            "skipped_foreign": skipped_foreign,          # чужие/ручные черновики — пропущены (не наши)
            "kind": "delete_drafts"}


def _grid_empty_unified_drafts(login: str, agency: str) -> list:
    """ЕПК-черновики (GdUnifiedCampaign) с 0 групп = ПУСТЫШКИ (кампания создалась, сборка не дошла —
    напр. рестарт убил процесс на середине). Только НАШИ (имя с 'tp'). UAC (tp6/tp7) НЕ трогаем —
    у них 0 grid-групп штатно (структура через UAC, не adGroups). → [campaign_id, ...]."""
    import requests as _rqs
    try:
        drafts = [c for c in _grid_list_campaigns(login, only_draft=True)
                  if c.get("typename") == "GdUnifiedCampaign"
                  and str(c.get("name") or "").strip().lower().startswith("tp") and c.get("id")]
    except Exception:  # noqa: BLE001
        return []
    if not drafts:
        return []
    ids = [str(c["id"]) for c in drafts]
    try:
        cookie = cmc.load_cookie(agency)
    except Exception:  # noqa: BLE001
        cookie = None
    if not cookie:
        return []
    csrf = _block_bootstrap(cookie, agency)
    h = {"Cookie": cookie, "dna-operation-name": "AG", "x-direct-api": "1", "x-detected-locale": "ru",
         "Content-Type": "application/json", "User-Agent": cmc.USER_AGENT}
    if csrf:
        h["x-csrf-token"] = csrf
    AG = ("query AG($login:String!,$inp:GdAdGroupsContainerInput!){client(searchBy:{login:$login}){"
          "adGroups(input:$inp){rowset{id campaignId}}}}")
    inp = {"filter": {"campaignIdIn": ids},
           "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
           "limitOffset": {"limit": 5000, "offset": 0}, "orderBy": [{"order": "ASC", "field": "ID"}]}
    try:
        r = _rqs.post(f"{_GRID_URL}?operationName=AG&ulogin={login}",
                      json={"operationName": "AG", "query": AG, "variables": {"login": login, "inp": inp}},
                      headers=h, timeout=40, verify=False)
        if r.status_code == 403:
            c2 = _grid_csrf(r)
            if c2:
                h["x-csrf-token"] = c2
                r = _rqs.post(f"{_GRID_URL}?operationName=AG&ulogin={login}",
                              json={"operationName": "AG", "query": AG, "variables": {"login": login, "inp": inp}},
                              headers=h, timeout=40, verify=False)
        d = r.json()
        ags = (((d.get("data") or {}).get("client") or {}).get("adGroups") or {}).get("rowset") or []
    except Exception:  # noqa: BLE001
        return []
    have = {str(a.get("campaignId")) for a in ags}
    return [int(i) for i in ids if i not in have]   # нет ни одной группы → пустышка


def _sweep_empty_drafts(login: str, agency: str = "") -> int:
    """Авто-очистка: удалить пустые ЕПК-черновики (0 групп) аккаунта по куке. → число удалённых.
    Безопасно ТОЛЬКО когда нет активного создания (вызывать при старте после рестарта)."""
    ag = agency or _resolve_agency_hint(login, "") or ""
    empties = _grid_empty_unified_drafts(login, ag)
    if not empties:
        return 0
    try:
        res = gc.GridCreateClient(login).delete_campaigns(empties)
        return len(res.get("deleted") or [])
    except Exception:  # noqa: BLE001
        return 0


def _delete_partial_campaign(token: str, login: str, campaign_id: int | str | None) -> bool:
    """Удалить один недособранный черновик: v5 сначала, Grid-cookie как фолбэк при лимитах/типах."""
    if not campaign_id:
        return False
    try:
        cmc.DirectV501Client(token, login).delete_campaigns([int(campaign_id)])
        return True
    except Exception:  # noqa: BLE001
        try:
            deleted = gc.GridCreateClient(login).delete_campaigns([campaign_id]).get("deleted") or []
            return int(campaign_id) in {int(x) for x in deleted}
        except Exception:  # noqa: BLE001
            return False


def _delete_drafts_response():
    """Синхронное удаление черновиков (обратная совместимость). Тело: {login, agency}."""
    body = request.json or {}
    login = (body.get("login") or "").strip()
    agency = (body.get("agency") or "").strip()
    if not login:
        return jsonify({"error": "login обязателен"}), 400
    ok, reason, wait = _pull_begin(f"deldrafts:{login}", 20.0)
    if not ok:
        return _busy_response(reason, wait)
    try:
        return jsonify(_delete_drafts_core(login, agency))
    finally:
        _pull_end(f"deldrafts:{login}")


def _delete_drafts_async_response():
    """Удаление черновиков ФОНОВОЙ джобой в ОБЩЕЙ очереди создания (та же карточка, что и создание РК).
    Возврат {job_id} сразу; прогресс — через /api/create_set_status. Тело: {login, agency}."""
    body = dict(request.json or {})
    login = (body.get("login") or "").strip()
    if not login:
        return jsonify({"error": "login обязателен"}), 400
    body["_kind"] = "delete_drafts"                       # маркер для воркера (ветка удаления)
    resolved_ag = _resolve_agency_hint(login, (body.get("agency") or "").strip())
    if resolved_ag:
        body["agency"] = resolved_ag                     # ключ партиционирования очереди (как у создания)
    app = current_app._get_current_object()
    _ensure_create_worker(app)
    saved_session = dict(session)
    job_id = _job_new(0, login, body, saved_session)     # total уточнит воркер после подсчёта черновиков
    with _CREATE_JOBS_LOCK:
        ahead = _create_jobs_ahead(job_id)
    return jsonify({"job_id": job_id, "total": 0, "login": login, "ahead": ahead})


# ── Проверка блокировок (Grid CampaignsTotal на куках — как check_block_direct) ─
_GRID_URL = "https://direct.yandex.ru/web-api/grid/api"
_BLOCK_QUERY = (
    "query CampaignsTotal($login:String! $campaignInput:GdCampaignsContainerInput!){"
    "userFeatures client(searchBy:{login:$login}){"
    "campaigns(input:$campaignInput){totalCampaigns{totalSumRest}}}}"
)
_BLOCK_INPUT = {"filter": {}, "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
                "limitOffset": {"limit": 1, "offset": 0}, "orderBy": [{"order": "ASC", "field": "STATUS"}]}


def _grid_post(cookie: str, csrf, login: str):
    import requests as _rqs
    headers = {"Cookie": cookie, "dna-operation-name": "CampaignsTotal", "x-direct-api": "1",
               "x-detected-locale": "ru", "Content-Type": "application/json", "User-Agent": cmc.USER_AGENT}
    if csrf:
        headers["x-csrf-token"] = csrf
    url = f"{_GRID_URL}?operationName=CampaignsTotal&ulogin={login}"
    payload = {"operationName": "CampaignsTotal", "query": _BLOCK_QUERY,
               "variables": {"login": login, "campaignInput": _BLOCK_INPUT}}
    try:
        return _rqs.post(url, json=payload, headers=headers, timeout=40, verify=False)
    except Exception:  # noqa: BLE001
        return None


def _grid_csrf(resp):
    import re
    if resp is None:
        return None
    c = resp.cookies.get("_direct_csrf_token")
    if c:
        return c
    m = re.search(r"_direct_csrf_token=([^;,\s]+)", resp.headers.get("Set-Cookie", ""))
    return m.group(1) if m else None


def _block_bootstrap(cookie: str, agency_login: str):
    """Self-probe куки на агентском логине → CSRF ('' = не нужен, None = кука мертва)."""
    r = _grid_post(cookie, None, agency_login)
    if r is None:
        return None
    if r.status_code == 200:
        return _grid_csrf(r) or ""
    if r.status_code == 403:
        return _grid_csrf(r)
    return None


def _block_check(cookie: str, csrf, login: str):
    """True=BLOCKED, False=OK, None=не удалось проверить."""
    r = _grid_post(cookie, csrf or None, login)
    if r is not None and r.status_code == 403 and not csrf:
        c2 = _grid_csrf(r)
        if c2:
            r = _grid_post(cookie, c2, login)
    if r is None or r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        return None
    if data.get("errors"):
        return None
    top = data.get("data") or {}
    if "userFeatures" in top:
        return "BLOCKED" in (top["userFeatures"] or [])
    return None


# Все агентские куки — для перебора, если agency_account в таблице неверный/без прав.
_KNOWN_AGENCIES = ["victorylotsofads1", "victoryagency-direct1618440", "victoryagency14",
                   "y-direct-victory", "victoryagencydirect", "useful-call-agency"]


def _check_blocks_response():
    """Блокировки аккаунтов (Grid userFeatures на агентских куках). Только переданные логины.

    Своё агентство из строки пробуем первым; если нет прав/ошибка — перебираем
    остальные агентские куки (как check_block_direct), пока не получим ответ."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    pairs = (request.json or {}).get("pairs") or []
    ok, reason, wait = _pull_begin("blocks", 60.0)
    if not ok:
        return _busy_response(reason, wait)
    try:
        # Список агентств: из строк + известные (на случай неверного agency_account).
        agencies: list[str] = []
        for p in pairs:
            ag = (p.get("agency") or "").strip()
            if ag and ag != "None" and ag not in agencies:
                agencies.append(ag)
        for ag in _KNOWN_AGENCIES:
            if ag not in agencies:
                agencies.append(ag)

        # Одна сессия (cookie+csrf) на агентство — поднимаем один раз.
        sessions: dict[str, tuple] = {}
        for ag in agencies:
            try:
                cookie = cmc.load_cookie(ag)
            except Exception:  # noqa: BLE001
                cookie = None
            if not cookie:
                continue
            csrf = _block_bootstrap(cookie, ag)
            if csrf is None:
                continue
            sessions[ag] = (cookie, csrf)

        def check_one(login: str, own: str):
            order = ([own] if own in sessions else []) + [a for a in sessions if a != own]
            for ag in order:
                cookie, csrf = sessions[ag]
                res = _block_check(cookie, csrf, login)
                if res is not None:
                    return res
            return None

        items = [((p.get("login") or "").strip(), (p.get("agency") or "").strip()) for p in pairs]
        items = [(lg, ag) for lg, ag in items if lg]
        blocks: dict = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(check_one, lg, ag): lg for lg, ag in items}
            for f in as_completed(futs):
                blocks[futs[f]] = f.result()
        for p in pairs:
            blocks.setdefault((p.get("login") or "").strip(), None)
        return jsonify({"blocks": blocks})
    finally:
        _pull_end("blocks")


register_campaign_routes(
    bp,
    _direct_access,
    _direct_danger,
    campaigns_response=_campaigns_response,
    stop_all_response=_stop_all_response,
    delete_drafts_response=_delete_drafts_response,
    delete_drafts_async_response=_delete_drafts_async_response,
    check_blocks_response=_check_blocks_response,
)


# ── Генератор имени кампании + планировщик набора ──────────────────────────────
# Тип сайта → код для середины имени (остальные типы добавим позже).


def _create_set_plan_deps() -> dict:
    return {
        "_SLEPOK_KEY": _SLEPOK_KEY,
        "_ag_part1_map": _ag_part1_map,
        "_ct_for_name": _ct_for_name,
        "_ct_segment": _ct_segment,
        "_direct_tokens": _direct_tokens,
        "_filter_allowed_feed_rows": _filter_allowed_feed_rows,
        "_first_url_feed": _first_url_feed,   # обёртка с configure() — НЕ импортировать из csf напрямую
        "_gc_ct": _gc_ct,
        "_grid_feeds": _grid_feeds,
        "_grid_list_campaigns": _grid_list_campaigns,
        "_is_site_domain_name": _is_site_domain_name,
        "_json": _json,
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


register_account_routes(
    bp,
    _direct_access,
    victory_conn=_victory_conn,
    parse_counter_ids=_parse_counter_ids,
    parse_number=lambda val, default: _num(val, default),
    metrika_goals_for=_metrika_goals_for,
    resolve_region=_resolve_region,
    account_prefill_func=_account_prefill_response,
    account_assets_func=_account_assets_response,
    account_audiences_func=_account_audiences_response,
    balance_func=_do_balance,
    pull_begin=_pull_begin,
    pull_end=_pull_end,
    busy_response=_busy_response,
    cooldowns=_COOLDOWN,
    goal_vse_formy=_goal_vse_formy,
    account_cols=_ACCOUNT_COLS,
    default_status=DEFAULT_STATUS,
    exclude_directologs=_EXCLUDE_DIRECTOLOGS,
)

# Редактор контента может работать отдельным процессом direct-content.service.
# В direct.service его выключаем через DIRECT_REGISTER_CONTENT_EDITOR=0, чтобы
# /direct/automation и /direct/automation/content перезапускались независимо.
if os.environ.get("DIRECT_REGISTER_CONTENT_EDITOR", "1") != "0":
    register_content_editor_routes(
        bp,
        _service_required_any("work", "work:direct", "direct:content", "direct"),
        victory_conn=_victory_conn,
        token_for_login=_token_for_login,
        direct_tokens=_direct_tokens,
        v5_call=_v5_call,
        v501_svc=_v501_svc,
        default_status=DEFAULT_STATUS,
        exclude_directologs=_EXCLUDE_DIRECTOLOGS,
    )


# Бренд-нейтральные заголовки-филлеры (≤56 симв.) — добор до 5 слотов Мастера, когда у слепка
# заголовков меньше. Подходят к любой картинке салона (не привязаны к модели).
# БАГ 9: кредитные УТП в приоритете (первый взнос, ставка, платеж, господдержка).
# БАГ 4→исправлен: дефис-разделитель заменён на точку (правило Кудерко); БАГ 7: «0%» убрано из кредитных заголовков.
_GENERIC_TITLE_FILLERS = [
    "Кредит на новый авто. Первый взнос 0 ₽. Ключи за 1 день",  # [55]
    "Авто в кредит от 9 000 ₽/мес. КАСКО на 1 год бесплатно",   # [54]
    "Кредит на авто. КАСКО на 1 год. Подбор от 15 банков",       # [51]
    "Оценим авто в трейд-ин. Платеж от 9 000 ₽/мес онлайн",     # [52]
    "Первый взнос 0 ₽. Подбор кредита от 15 банков онлайн",      # [52]
    "Новые авто в наличии. Кредит от 15 банков за 1 день",       # [51]
    "Автокредит от 15 банков. Решение за 30 минут онлайн",       # [51]
]
# Заголовки под АВТОТАРГЕТ общих запросов (tp7 Товарка ct0000): ключевая фраза запроса СТОИТ ПЕРВОЙ -
# до точки/запятой (купить/новый/авто/цена/кредит), движок автотаргета цепляет её как ключ. БЕЗ марок/моделей
# (общая кампания). Правило пользователя: для «Общих запросов» - заголовки под общий запрос, не под бренд.
# БАГ 9: кредитные УТП приоритетом (2-3 из 5); БАГ 4→исправлен: разделитель — точка, не дефис.
_GENERIC_AT_TITLES = [
    # 8 строк: все с цифрой, разные первые слова, разные УТП-бакеты
    # (платёж / взнос / КАСКО / банки+срок / скидка / трейд-ин / наличие / одобрение)
    "Авто в кредит от 9 000 ₽/мес. Одобрение за 30 минут",     # [51] платёж
    "Кредит на авто. Первый взнос 0 ₽. Ключи за 1 день",       # [50] взнос
    "Купить новое авто в кредит. КАСКО на 1 год бесплатно",     # [52] КАСКО
    "Автокредит от 15 банков-партнеров. Решение за 30 минут",   # [54] банки+срок
    "Выгода до 45% на новые авто. Кредит от 15 банков",         # [48] скидка%
    "Трейд-ин выше рынка. Платеж от 9 000 ₽/мес в кредит",      # [51] трейд-ин
    "Новые авто в наличии. Первый взнос 0 ₽. Ключи за 1 день",  # [55] наличие+взнос
    "Одобрение за 30 минут. Кредит на авто от 15 банков",       # [50] одобрение
]
# Брендонейтральные фоллбэки текстов/ссылок - ГАРАНТ полноты tp6/tp7 (5 заголовков / 3 текста / 8 ссылок),
# когда контента слепка/шаблонов не хватило. Без марок - годятся для любой общей (ct0000) кампании.
# БАГ 9: кредитные УТП в первых 2 текстах (первый взнос, платеж, ставка, господдержка).
# БАГ 4→исправлен: разделитель — точка, не дефис.
_GENERIC_TEXT_FILLERS = [
    # 4 строки: все с цифрой, без «автокредит» (блокируется _bad_ad_text)
    # УТП-бакеты: платёж+банки / взнос+КАСКО / трейд-ин+срок / наличие+срок
    "Кредит на авто от 9 000 ₽/мес. Подберем условия от 15 банков. Одобрение за 1 час.",  # [81] платёж
    "Кредит без первого взноса на новое авто. Одобрение за 1 день. 15 банков онлайн.",    # [79] взнос
    "КАСКО на 1 год бесплатно при покупке в кредит. Ключи в день покупки. Одобрение.",    # [79] КАСКО
    "Трейд-ин выше рынка. Оценим авто за 30 минут и зачтём в счёт нового кредита.",       # [76] трейд-ин
]
_TP67_MIN_TEXT_LEN = 70
_GENERIC_SITELINK_FILLERS = [  # все заголовки ≥ 22 симв (fix 1c, 2026-07-02); кредит-тема — 1 слот
    # «Автокредит от 9 000 ₽/мес» удалён: при наличии реального сitelink «Платёж от X ₽/мес»
    # образовывал смысловой дубль (кредит+платёж = одна тема); остался «Первый взнос» как кредит-слот.
    {"title": "Первый взнос 0 ₽ онлайн", "description": "Оформим кредит без первоначального взноса онлайн"},
    {"title": "Оценка авто на трейд-ин", "description": "Оценим ваш автомобиль и зачтем в покупку онлайн"},
    {"title": "КАСКО на 1 год бесплатно", "description": "Условия действуют при покупке автомобиля в кредит"},
    {"title": "Одобрение за 30 мин онлайн", "description": "Отправьте заявку и получите решение банка сегодня"},
    {"title": "Выгода до 30% при покупке", "description": "Зафиксируем персональные условия покупки автомобиля"},
    {"title": "Господдержка на авто 2025", "description": "Проверим доступные программы покупки автомобиля"},
    {"title": "Тест-драйв без предоплаты", "description": "Выберите удобное время для знакомства с автомобилем"},
    {"title": "Авто в наличии сегодня", "description": "Подберем автомобиль под ваш бюджет онлайн сегодня"},
]


def _build_name(is_master: bool, is_auto: bool, pay: str, r_code: str, oblast: str,
                sq: str = "site", cat: str | None = None, ct: str = "ct0000") -> str:
    return _create_set_plan_module()._build_name(is_master, is_auto, pay, r_code, oblast, sq, cat, ct)


def _rule_sets(site_type: str, city: str) -> dict:
    return _create_set_plan_module()._rule_sets(site_type, city)


def _tp_plan_names(slepok: str, site_type: str, tp_code: str) -> list[dict]:
    return _create_set_plan_module()._tp_plan_names(slepok, site_type, tp_code)


def _tp1_plan_names(slepok: str, site_type: str, r_code: str) -> list[dict]:
    return _create_set_plan_module()._tp1_plan_names(slepok, site_type, r_code)


def _set_plan_response():
    return _create_set_plan_module()._set_plan_response()


register_set_plan_routes(
    bp,
    _direct_access,
    set_plan_response=_set_plan_response,
)


def _num(val, default):
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


def _wire_copy_routes(target_bp, *, ensure_worker):
    """Единая проводка copy-роутов — вызывается и основным процессом (bp), и отдельным
    copy_main.py (свой fresh bp). ensure_worker разный: _ensure_create_worker в общем
    процессе, _ensure_copy_worker в direct-copy.service (изолированная copy-очередь)."""
    register_copy_routes(
        target_bp,
        _direct_access,
        api_campaigns_func=_campaigns_response,
        account_prefill_func=_account_prefill_response,
        metrika_goals_for=_metrika_goals_for,
        parse_number=_num,
        copy_default_feed_path=_COPY_DEFAULT_FEED_PATH,
        counter_foreign_owner=_counter_foreign_owner,
        resolve_agency_hint=_resolve_agency_hint,
        ensure_create_worker=ensure_worker,
        job_new=_job_new,
        copy_job_upsert=_copy_job_upsert,
        create_jobs_ahead=_create_jobs_ahead,
        create_jobs=_CREATE_JOBS,
        create_jobs_lock=_CREATE_JOBS_LOCK,
        copy_jobs=_COPY_JOBS,
        copy_jobs_lock=_COPY_JOBS_LOCK,
        feeds_preview_func=_copy_feeds_preview,
    )


# Копирование кампаний может работать отдельным процессом direct-copy.service со своей
# in-memory очередью. В direct.service его выключаем через DIRECT_REGISTER_COPY=0, чтобы
# /direct/automation и /direct/automation/copy перезапускались независимо (рестарт одного
# не роняет очередь другого). Дефолт (флаг не задан) = '1' — копирование в основном
# процессе (обратная совместимость single-process-режима).
if os.environ.get("DIRECT_REGISTER_COPY", "1") != "0":
    _wire_copy_routes(bp, ensure_worker=_ensure_create_worker)


def _prefetch_start(login, body, *, is_cancelled=lambda: False):
    """Прогрев queued-джобы в фоне (Фаза 1). Конфигурируем модуль лениво (все
    инъектируемые хелперы — _account_offer_prices и т.п. — определены ниже по
    файлу, резолвятся в момент вызова, а не определения)."""
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
            "get_agent": _A.get_agent,
            "pick_working_cookie": _cmc.pick_working_cookie,
            "videos_pool_for_ct": kp.videos_pool_for_ct,
        })
        _pf.start_prefetch(login, body, is_cancelled=is_cancelled)
    except Exception:  # noqa: BLE001 — прогрев не смеет ломать постановку джобы
        pass


register_job_routes(
    bp,
    _direct_access,
    _direct_danger,
    parse_number=_num,
    metrika_goals_for=_metrika_goals_for,
    counter_foreign_owner=_counter_foreign_owner,
    resolve_agency_hint=_resolve_agency_hint,
    ensure_create_worker=_ensure_create_worker,
    job_new=_job_new,
    create_jobs_ahead=_create_jobs_ahead,
    create_watchdog_tick=_create_watchdog_tick,
    jobs_purge_old=_jobs_purge_old,
    job_agency=_job_agency,
    job_db_save=_job_db_save,
    job_db_delete=_job_db_delete,
    delete_drafts_core=_delete_drafts_core,
    create_jobs=_CREATE_JOBS,
    create_jobs_lock=_CREATE_JOBS_LOCK,
    create_cond=_CREATE_COND,
    create_queue=_CREATE_QUEUE,
    job_terminal=_JOB_TERMINAL,
    job_db_last=_JOB_DB_LAST,
    start_prefetch=_prefetch_start,
    role_is_web=lambda: _direct_role() == "web",
    job_db_get=_job_db_get,
    job_db_ahead=_job_db_ahead,
    job_db_set_status=_job_db_set_status,
    job_control_set=_job_control_set,
    job_db_web_await_feed=_job_db_web_await_feed,
    job_db_web_resolve_feed=_job_db_web_resolve_feed,
    job_db_active_by_login=_job_db_active_by_login,
    job_db_list_recent=_job_db_list_recent,
    cancel_children=_cancel_children_of,
)


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
                       position_name: str | None = None, sq: str | None = None) -> tuple[list[str], list[str]]:
    return _create_set_context_module()._tp67_keywords_for(slepok, site_type, tp, ct, city, position_name, sq)


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

def _tp7_listings_minus_filters(*args, **kwargs):
    return _create_set_feeds_module()._tp7_listings_minus_filters(*args, **kwargs)


# ── Shared минус-набор для tp2/tp4 (TEXT_CAMPAIGN) — канон CODER.md §«Минус» ──────
# Путь ИДЕНТИЧЕН tp1/tp5: взять существующий набор «Минуса общие» из аккаунта через
# v5 negativekeywordsharedsets.get. Если в аккаунте нет ни одного — собрать минусы
# из пака M3 (все ct данного tp, объединить+дедупликация), обрезать по бюджету
# КАМПАНИИ 20 000 символов БЕЗ пробелов (лимит Директа), создать набор.
# Привязка — через v5 campaigns.update (NegativeKeywordSharedSetIds) — для TEXT_CAMPAIGN
# это валидное поле верхнего уровня (в отличие от tp1/tp5 где Grid libraryMinusKeywordsIds).
# Карта механизма привязки минусов по слепку (как в РЕАЛЬНЫХ аккаунтах — live-аудит):
#   campaign   → NegativeKeywords прямо на кампании (≤20 000 симв. без пробелов) — pavlov, kryuchkova
#   shared_set → переиспользовать/создать набор «Минуса общие», привязать через NegativeKeywordSharedSetIds — scherbakova
#   group      → NegativeKeywords на каждой группе объявлений (≤4 096 симв./группа) — terehov
# Default для неизвестного слепка — "group" (безопасно, текущее поведение).
_SLEPOK_MINUS_MODE: dict[str, str] = {
    "pavlov": "campaign",
    "kryuchkova": "campaign",
    "scherbakova": "shared_set",
    "terehov": "group",
    "karavaev": "group",
}


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
def _kw_clean(words: list, cap: int) -> list:
    """Очистка ключей под Директ: strip, dedup, ≤7 слов, разумная длина, cap по count."""
    out, seen = [], set()
    for w in words:
        w = re.sub(r"\s+", " ", (str(w) or "").strip())
        if not w or len(w.split()) > 7 or len(w) > 4096:
            continue
        k = w.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(w)
        if len(out) >= cap:
            break
    return out


# ── Manual-креативы: дополнительные изображения из /opt/creatives/Manual/{ct}/ ──────────
# Папка монтирована на LXC 101. Структура: ct0019/ct0019_Trade-in.png и т.д.
# Используется как ДОБИВКА к пак-картинкам M3: сначала пак (read_slepok_images/read_images),
# затем Manual. Итого → upload_image → AdImageHashes (≤5 на ResponsiveAd).
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


def _get_or_reuse_sitelink_set(token: str, login: str, sitelinks: list) -> int | None:
    """Создать набор быстрых ссылок через v5; при 152 — Grid (БЕЗ баллов).
    Grid-путь: GridClient.add_sitelink_set (реверс HAR23/entry262 AddSitelinkSets).
    Best-effort: при любой ошибке возвращает None (без ссылок)."""
    if not sitelinks:
        return None
    if token:
        cl = cmc.DirectV501Client(token, login)
        try:
            return cl.add_sitelinks_set(sitelinks)
        except cmc.DirectV501Error as e:
            if e.code != 152:
                return None
        # 152 → fallthrough к Grid
    # Grid-путь (БЕЗ баллов): работает и при 0 units, и без token
    try:
        gc = gf.get_grid_client(login)
        sid = gc.add_sitelink_set(sitelinks)
        if sid:
            return sid
    except Exception:  # noqa: BLE001
        pass
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
    asset_sl = _norm_sitelinks_for_v501(sitelinks or [], href) or _norm_sitelinks_for_v501(out["sitelinks"], href)
    out["asset_sitelinks"] = asset_sl   # нормализованный шаблон для per-group наборов (#ФИКС-3)
    if asset_sl:
        try:
            out["sitelink_set_id"] = gf.get_grid_client(login, cookie=grid_cookie).add_sitelink_set(asset_sl)
        except Exception:  # noqa: BLE001
            out["sitelink_set_id"] = _get_or_reuse_sitelink_set(token, login, asset_sl)
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


























_SLEPOK_IMG_TPS = ("tp6", "tp7", "tp1", "tp5", "tp3", "tp2", "tp4")
_COMMON_IMAGE_CTS = {
    "ct0000", "ct0001", "ct0002", "ct0003", "ct0004", "ct0005", "ct0006",
    "ct0007", "ct0008", "ct0009", "ct0010", "ct0013", "ct0014",
}
_BODY_IMAGE_CTS = {"ct0015", "ct0016", "ct0017", "ct0018"}


def _image_ct_for_content(ct: str) -> str:
    """Какую папку картинок использовать для ct.
    Общие/аудиторные ct0000-ct0014 берут общий пул ct0000; кузова ct0015-ct0018 — свой ct;
    модельные/марочные ct — свой ct."""
    c = _gc_ct(ct)
    if c in _COMMON_IMAGE_CTS:
        return "ct0000"
    return c


def _creative_images_for_ct(site_type: str, tp: str, ct: str, slepok: str,
                            *, allow_manual: bool = True, limit: int = 5) -> list:
    limit = max(1, int(limit or 5))
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
        imgs = list(dict.fromkeys(own))[:limit]
        if len(imgs) < limit:
            slepok_imgs = kp.read_slepok_images(site_type, tp, "ct0000", slepok) or []
            slepok_imgs = _prioritized_content_assets(
                slepok_imgs or [], ct,
                source_segment=site_type, source_tp=tp, source_ct="ct0000",
                target_slepok=slepok, source_slepok=slepok, limit=limit,
            )
            imgs += [p for p in slepok_imgs if p not in imgs]
        if len(imgs) < limit:
            extra = _explicit_content_assets_for(ct, target_slepok=slepok,
                                                 asset_types={"image", "image_slepki"}, limit=limit)
            imgs += [p for p in extra if p not in imgs]
        return list(dict.fromkeys(imgs))[:limit]
    # Приоритет пользователя: сначала ручной общий пул agency/creatives/Manual/{ct}, затем добор
    # по выбранному слепку, затем общий M3-пул этого type/tp/ct. Правила вкладки «Контент»
    # могут отключить файл или разрешить его для других ct/слепков.
    imgs = []
    if manual_imgs:
        # Manual-креативы для модельного/брендового ct могут быть размечены во вкладке «Контент»
        # как ct0000/common, хотя физически лежат в папке модели. В таком случае сначала берём
        # строгий матч по ct папки, затем мягко добираем те же файлы как common-пул.
        imgs += _filter_content_assets(
            manual_imgs, ct,
            source_segment="Общее", source_tp="manual", source_ct=img_ct,
            target_slepok=slepok, source_slepok="",
        )
        if len(imgs) < limit:
            common_manual = _filter_content_assets(
                manual_imgs, ct,
                source_segment="Общее", source_tp="manual", source_ct="ct0000",
                target_slepok=slepok, source_slepok="",
            )
            imgs += [p for p in common_manual if p not in imgs]
        if imgs:
            # Пользовательский приоритет: если для ct есть manual-пул, не смешиваем его с чужими
            # slepok/M3-картинками. Добор из слепка разрешён только когда manual-пул пуст.
            return list(dict.fromkeys(imgs))[:limit]
    slepok_imgs = kp.read_slepok_images(site_type, tp, img_ct, slepok) or []
    imgs += [p for p in slepok_imgs if p not in imgs]
    # БАГ-17: для НЕ-б/у кампаний не подмешиваем картинки из б/у-слепков (terehov и др.):
    # read_any_slepok_images(exclude_bu_slepoks=True) пропускает _BU_SLEPOKS при переборе.
    # Для б/у-сайта (terehov) — прежний путь через read_images (без исключений).
    if slepok in kp._BU_SLEPOKS:
        common_imgs = kp.read_images(site_type, tp, img_ct) or []
    else:
        common_imgs = kp.read_any_slepok_images(site_type, tp, img_ct, prefer=slepok,
                                                exclude_bu_slepoks=True) or []
    imgs += [p for p in common_imgs if p not in imgs]
    imgs = _prioritized_content_assets(
        imgs or [], ct, source_segment=site_type, source_tp=tp, source_ct=img_ct,
        target_slepok=slepok, source_slepok=slepok, limit=limit
    )
    if len(imgs) < limit:
        explicit = _explicit_content_assets_for(ct, target_slepok=slepok,
                                                asset_types={"image", "image_slepki"}, limit=limit)
        imgs += [p for p in explicit if p not in imgs]
    return list(dict.fromkeys(imgs))[:limit]


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
    if promo_pcts or content_pcts:
        if promo_pcts != content_pcts:
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
        "_finalize_rsya": _finalize_rsya,
        "_finalize_search_via_grid": _finalize_search_via_grid,
        "_get_or_reuse_sitelink_set": _get_or_reuse_sitelink_set,
        "_grid_account_image_hashes": _grid_account_image_hashes,
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
        "_TP67_RELEVANCE_CATEGORIES", "_account_model_feeds", "_add_job_err", "_audience_objects",
        "_bad_ad_sitelink", "_bad_ad_text", "_bad_ad_title", "_brand_ct_from_coder", "_brand_title_set",
        "_build_name", "_bump_item", "_bump_job", "_cached_campaign_content", "_catalog_feed",
        "_coherent_discounts", "_coherent_payments", "_creative_images_for_ct", "_dedup_prefix_absorb",
        "_discount_pcts", "_diverse_text_offers", "_drop_used_car", "_enabled_minus_words",
        "_fallback_master_titles",
        "_fill_title", "_fill_variants", "_has_number", "_image_ct_for_content", "_is_bad_start",
        "_is_bu_site", "_is_common_ct", "_is_site_domain_name", "_job_db_progress", "_lines",
        "_match_collection", "_num", "_own_brand_tokens", "_replace_emdash", "_replace_foreign_city",
        "_replace_sep_hyphen", "_resolve_region", "_rsya_texts", "_rsya_titles", "_sanitize_content",
        "_sitelink_has_pct", "_slepok_audiences_for", "_slepok_campaign_content", "_strip_credit_rate",
        "_title2_blocklist", "_tp67_keywords_for", "_tp67_targeting_mode", "_tp7_product_feed_filters",
        "_tp7_listings_minus_filters",
        "_trim_to_word", "_variant_norm_key",
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
        "_content_copy", "_counter_foreign_owner", "_create_account_promo_from_slepok",
        "_create_set_live_verification", "_create_shopping_via_cookie", "_create_text_via_cookie",
        "_create_text_via_token",   # DIRECT_API_FIRST: tp2/tp4 через баллы (token), фолбэк на cookie
        "_create_tp1_campaign", "_create_tp1_via_cookie", "_create_tp3_campaign", "_create_tp5_campaign",
        "_dedup_callouts", "_deferred_save", "_deferred_set_status", "_first_url_feed",
        "_get_or_create_minus_set", "_goal_vse_formy", "_grid_list_campaigns", "_ints", "_job_db_progress",
        "_job_new", "_m3_gate_wait", "_slepok_profile_excludes_tp",
        "_lines", "_load_corrections", "_metrika_goals_for", "_next_units_reset_utc", "_normalize_callout_text",
        "_num", "_preflight_creds", "_promo_content_lines", "_promo_usable_for_content",
        "_preupload_tp1_images",   # набор-level прогрев картинок tp1 через configured-модуль (DI-инъекции)
        "_pull_begin", "_pull_end", "_repair_deps", "_resolve_region", "_rotated_content_window",
        "_rule_sets", "_run_master_product_item", "_selected_slepok_key", "_slepok_content_get",
        "_slepok_uses_shopping", "_templates_for",
        "_auth_error_in_result", "_units_in_result", "_v5_get", "_v5_call",
        "_units_alive_for_login",   # баллы живы → сегментный tp5 добиваем токеном сразу (не ждём полночь)
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
    "_geo_id": _geo_id, "_enabled_minus_places": _enabled_minus_places,
    "_filter_allowed_feed_rows": _filter_allowed_feed_rows, "_feed_key": _feed_key,
    "_create_set_live_verification": _create_set_live_verification,
    "_attach_post_repair_verification": _attach_post_repair_verification, "_repair_deps": _repair_deps,
    "_CREATE_JOBS": _CREATE_JOBS, "_CREATE_JOBS_LOCK": _CREATE_JOBS_LOCK, "_JOB_TERMINAL": _JOB_TERMINAL,
    "_job_touch": _job_touch, "_job_db_save": _job_db_save,
    "_CALLOUT_PER_CAMPAIGN_CAP": _CALLOUT_PER_CAMPAIGN_CAP,
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
    job_result = {"body": body, "agency": ctx.get("agency") or ""}
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


register_create_set_routes(
    bp,
    _direct_access,
    create_set_response=_create_set_response,
    legacy_create_response=_legacy_create_response,
    repair_gate=rgate,
    repair_auto=rauto,
    create_set_job_context=_create_set_job_context,
    create_set_live_verification=_create_set_live_verification,
    repair_deps=_repair_deps,
    queue_recreate_repair_job=_queue_recreate_repair_job,
    attach_post_repair_verification=_attach_post_repair_verification,
    ensure_create_worker=_ensure_create_worker,
    create_jobs=_CREATE_JOBS,
    create_jobs_lock=_CREATE_JOBS_LOCK,
    job_terminal=_JOB_TERMINAL,
)


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


def _touch_running_jobs_heartbeat() -> None:
    """LLM-запрос = прогресс: бампаем _heartbeat всех running-джоб.

    Root cause watchdog-киллов 2026-07-02 (jobs 9126bf12fb3a/ac6d98864aa4/c8c444a166d4):
    _M3_LLM_TIMEOUT=480с × несколько запросов на item — при перегруженном M3 (sshfs-выкачка
    видео душила Мак) генерация ПЕРВОГО item шла >15 мин, item-heartbeat не тикал → watchdog
    убивал ЖИВУЮ джобу. Теперь каждый M3-вызов (включая ретраи) отмечает активность."""
    try:
        now = time.time()
        with _CREATE_JOBS_LOCK:
            for _j in _CREATE_JOBS.values():
                if _j.get("status") == "running":
                    _j["_heartbeat"] = now
    except Exception:  # noqa: BLE001
        pass


# heartbeat очереди определён — инъектим его в llm_providers (каждый M3/OpenRouter-вызов
# бампает _heartbeat running-джоб; иначе долгая генерация выглядела бы «зависанием» для watchdog).
_llmp.configure({"_touch_running_jobs_heartbeat": _touch_running_jobs_heartbeat})

# 7 DI для text_gen (генерация текстов): 3 функции blueprint + 4 константы-пула.
_tg.configure({
    "_drop_used_car": _drop_used_car, "_brand_canon": _brand_canon, "_ct_segment": _ct_segment,
    "_GENERIC_TITLE_FILLERS": _GENERIC_TITLE_FILLERS, "_GENERIC_AT_TITLES": _GENERIC_AT_TITLES,
    "_RA_TITLES_CAP": _RA_TITLES_CAP, "_RA_TEXTS_CAP": _RA_TEXTS_CAP,
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

    for ex in list(p.get("examples") or []) + neutral.get(st, neutral_new):
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

register_ai_routes(
    bp,
    _direct_access,
    ai_agents=_ai_agents_routes,
    campaign_module=cmc,
    promo_client_cls=_PromoClientRoutes,
    m3_llm_url=_M3_LLM_URL,
    m3_llm_timeout=_M3_LLM_TIMEOUT,
    m3_complete=_m3_complete,
    promo_ctx=_promo_ctx,
    promo_extract_json=_promo_extract_json,
    promo_from_slepok=_promo_from_slepok,
    promo_preview=_promo_preview,
    promo_validate=_promo_validate,
    promo_amount_steps=_promo_amount_steps,
    gen_campaign_content=_gen_campaign_content,
    seed_slepok_content=_seed_slepok_content,
    victory_conn=_victory_conn,
    direct_tokens=_direct_tokens,
    token_for_login=_token_for_login,
    pull_begin=_pull_begin,
    pull_end=_pull_end,
    busy_response=_busy_response,
    v5_get=_v5_get,
)


# 4 DI для ai_content (AI-контент): БД-коннекты + _gc_ct + _cached_campaign_content (blueprint).
# В КОНЦЕ модуля: _cached_campaign_content определяется ниже места импорта — инъекция после его def.
_aic.configure({
    "_victory_conn": _victory_conn, "_victory_conn_rw": _victory_conn_rw,
    "_gc_ct": _gc_ct, "_cached_campaign_content": _cached_campaign_content,
})

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
    "ALTERNATIVE_MARK", "ACCESSORY_MARK", "COMPETITOR_MARK", "BROADER_MARK", "EXACT_MARK",
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
_CREATE_RUNNING_TIMEOUT = 900    # сек без прогресса -> watchdog завершает зависшую running-джобу
_CREATE_WATCHDOG_POLL = 30       # период watchdog, сек
_CONTENT_CACHE_LOCK = threading.Lock()
_CONTENT_CACHE: dict = {}        # (agent, site_type, city, ct, brand) → generated content
_COPY_JOBS: dict = {}
_COPY_JOBS_LOCK = threading.Lock()
_DIRECT_COPY_MOD = None


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


def _direct_copy_module():
    """Ленивая загрузка work/slepki_direktologov/scripts/direct_copy.py как модуля."""
    global _DIRECT_COPY_MOD
    if _DIRECT_COPY_MOD is not None:
        return _DIRECT_COPY_MOD
    mod_path = _HERE.parents[2] / "work" / "slepki_direktologov" / "scripts" / "direct_copy.py"
    spec = importlib.util.spec_from_file_location("seoadvanced_direct_copy", mod_path)
    if not spec or not spec.loader:
        raise RuntimeError(f"не удалось загрузить direct_copy.py: {mod_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _DIRECT_COPY_MOD = mod
    return mod


def _copy_job_upsert(job_id: str, **fields) -> dict:
    with _COPY_JOBS_LOCK:
        job = _COPY_JOBS.setdefault(job_id, {"job_id": job_id, "log": [], "created_at": time.time()})
        job.update(fields)
        job["updated_at"] = time.time()
        snap = dict(job)
    _copy_mirror_create_job(job_id, snap)
    return snap


def _copy_mirror_create_job(job_id: str, copy_job: dict) -> None:
    """Mirror copy-flow progress into the shared create queue card."""
    snap = None
    with _CREATE_JOBS_LOCK:
        j = _CREATE_JOBS.get(job_id)
        if not j or j.get("kind") != "copy_campaigns":
            return
        status = copy_job.get("status")
        total = int(copy_job.get("total") or j.get("total") or 0)
        progress = int(copy_job.get("progress") or 0)
        result = copy_job.get("result") if isinstance(copy_job.get("result"), dict) else {}
        rows = result.get("results") or []
        created = sum(1 for r in rows if isinstance(r, dict) and r.get("ok"))
        failed = sum(1 for r in rows if isinstance(r, dict) and r.get("ok") is False)
        if status:
            j["status"] = status
        j["total"] = total
        j["set_total"] = total
        j["done"] = total if status in _JOB_TERMINAL else (min(total, round(total * progress / 100)) if total else progress)
        j["set_done"] = j["done"]
        j["created"] = created or int((result.get("uac_copy") or {}).get("created") or j.get("created") or 0)
        j["failed"] = failed
        if copy_job.get("error"):
            j["error"] = copy_job.get("error")
        if copy_job.get("result") is not None:
            j["result"] = copy_job.get("result")
        _job_touch(j)
        snap = dict(j)
    if snap:
        _job_db_save(job_id, snap, full=status in _JOB_TERMINAL)


def _copy_job_log(job_id: str, message: str) -> None:
    with _COPY_JOBS_LOCK:
        job = _COPY_JOBS.setdefault(job_id, {"job_id": job_id, "log": [], "created_at": time.time()})
        log = job.setdefault("log", [])
        log.append(str(message)[:400])
        if len(log) > 200:
            del log[:-200]
        job["updated_at"] = time.time()
        snap = dict(job)
    _copy_mirror_create_job(job_id, snap)


def _copy_read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def _copy_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _copy_filter_snapshot(src_dir: Path, selected_campaign_ids: set[int]) -> dict:
    """Оставить в snapshot только выбранные кампании и связанные сущности."""
    campaigns = [c for c in _copy_read_json(src_dir / "campaigns.json") if int(c.get("Id") or 0) in selected_campaign_ids]
    campaign_ids = {int(c["Id"]) for c in campaigns if c.get("Id")}
    adgroups = [g for g in _copy_read_json(src_dir / "adgroups.json") if int(g.get("CampaignId") or 0) in campaign_ids]
    adgroup_ids = {int(g["Id"]) for g in adgroups if g.get("Id")}
    ads = [a for a in _copy_read_json(src_dir / "ads.json")
           if int(a.get("CampaignId") or 0) in campaign_ids or int(a.get("AdGroupId") or 0) in adgroup_ids]
    shopping_ads = [a for a in _copy_read_json(src_dir / "shopping_ads.json") if int(a.get("AdGroupId") or 0) in adgroup_ids]
    keywords = [k for k in _copy_read_json(src_dir / "keywords.json")
                if int(k.get("CampaignId") or 0) in campaign_ids or int(k.get("AdGroupId") or 0) in adgroup_ids]
    bidmods = [m for m in _copy_read_json(src_dir / "bidmodifiers.json")
               if int(m.get("CampaignId") or 0) in campaign_ids or int(m.get("AdGroupId") or 0) in adgroup_ids]
    selected_domains = set()
    for a in ads:
        for key in ("TextAd", "TextImageAd", "TextAdBuilderAd", "DynamicTextAd", "SmartAd"):
            href = str((a.get(key) or {}).get("Href") or "")
            if "://" in href:
                selected_domains.add(href.split("://", 1)[1].split("/", 1)[0].lower())

    sitelink_ids, callout_ids, vcard_ids, image_hashes, feed_ids, shared_ids, retargeting_ids = set(), set(), set(), set(), set(), set(), set()
    for c in campaigns:
        for struct_key in ("TextCampaign", "DynamicTextCampaign", "SmartCampaign", "CpmBannerCampaign", "UnifiedAdCampaign"):
            td = c.get(struct_key) or {}
            shared_ids.update((td.get("NegativeKeywordSharedSetIds") or {}).get("Items") or [])
    for g in adgroups:
        shared_ids.update((g.get("NegativeKeywordSharedSetIds") or {}).get("Items") or [])
        fp = g.get("TextAdGroupFeedParams") or {}
        if fp.get("FeedId"):
            feed_ids.add(int(fp["FeedId"]))
    for a in ads + shopping_ads:
        for key in ("TextAd", "DynamicTextAd", "ShoppingAd"):
            td = a.get(key) or {}
            if td.get("SitelinkSetId"):
                sitelink_ids.add(int(td["SitelinkSetId"]))
            for ext in (td.get("AdExtensions") or []):
                if ext.get("AdExtensionId"):
                    callout_ids.add(int(ext["AdExtensionId"]))
            if td.get("VCardId"):
                vcard_ids.add(int(td["VCardId"]))
            if td.get("AdImageHash"):
                image_hashes.add(td["AdImageHash"])
            if td.get("FeedId"):
                feed_ids.add(int(td["FeedId"]))
    for m in bidmods:
        payload = m.get("RetargetingAdjustment") or {}
        if payload.get("RetargetingConditionId"):
            retargeting_ids.add(int(payload["RetargetingConditionId"]))

    sitelinks = [s for s in _copy_read_json(src_dir / "sitelinks.json") if int(s.get("Id") or 0) in sitelink_ids]
    callouts = [c for c in _copy_read_json(src_dir / "callouts.json") if int(c.get("Id") or 0) in callout_ids]
    vcards = [v for v in _copy_read_json(src_dir / "vcards.json") if int(v.get("Id") or 0) in vcard_ids]
    feeds = [f for f in _copy_read_json(src_dir / "feeds.json") if int(f.get("Id") or 0) in feed_ids]
    shared_sets = [s for s in _copy_read_json(src_dir / "negative_keyword_shared_sets.json") if int(s.get("Id") or 0) in shared_ids]
    ret_lists = [r for r in _copy_read_json(src_dir / "retargeting_lists.json") if int(r.get("Id") or 0) in retargeting_ids]
    promotions = []
    for p in _copy_read_json(src_dir / "promotions.json"):
        href = str(p.get("Href") or "")
        dom = href.split("://", 1)[1].split("/", 1)[0].lower() if "://" in href else ""
        if dom and dom in selected_domains:
            promotions.append(p)

    _copy_write_json(src_dir / "campaigns.json", campaigns)
    _copy_write_json(src_dir / "campaigns_skipped.json", [])
    _copy_write_json(src_dir / "adgroups.json", adgroups)
    _copy_write_json(src_dir / "ads.json", ads)
    _copy_write_json(src_dir / "shopping_ads.json", shopping_ads)
    _copy_write_json(src_dir / "keywords.json", keywords)
    _copy_write_json(src_dir / "bidmodifiers.json", bidmods)
    _copy_write_json(src_dir / "sitelinks.json", sitelinks)
    _copy_write_json(src_dir / "callouts.json", callouts)
    _copy_write_json(src_dir / "vcards.json", vcards)
    _copy_write_json(src_dir / "feeds.json", feeds)
    _copy_write_json(src_dir / "negative_keyword_shared_sets.json", shared_sets)
    _copy_write_json(src_dir / "retargeting_lists.json", ret_lists)
    _copy_write_json(src_dir / "promotions.json", promotions)

    img_dir = src_dir / "images"
    if img_dir.exists():
        for img in img_dir.glob("*.img"):
            if img.stem not in image_hashes:
                try:
                    img.unlink()
                except Exception:  # noqa: BLE001
                    pass

    meta = {
        "campaigns": len(campaigns), "adgroups": len(adgroups), "ads": len(ads),
        "keywords": len(keywords), "sitelinks": len(sitelinks), "callouts": len(callouts),
        "vcards": len(vcards), "adimages_used": len(image_hashes), "promotions": len(promotions),
        "shared_sets": len(shared_sets), "bidmodifiers": len(bidmods), "feeds": len(feeds),
        "retargeting_lists": len(ret_lists),
    }
    meta_path = src_dir / "_meta.json"
    meta_json = _copy_read_json(meta_path) if meta_path.exists() else {}
    meta_json["counts"] = meta
    _copy_write_json(meta_path, meta_json)
    return meta


_COPY_DEFAULT_FEED_PATH = "/dostup-k-rasprodazhe-live-01-b.xml"
_COPY_SUPPORTED_V5_TYPES = {"TEXT_CAMPAIGN", "DYNAMIC_TEXT_CAMPAIGN"}
_COPY_JSON_PAYLOADS = (
    "campaigns.json", "adgroups.json", "ads.json", "shopping_ads.json", "keywords.json",
    "sitelinks.json", "vcards.json", "feeds.json", "promotions.json",
)


def _copy_ctx(login: str) -> dict:
    try:
        ctx = _account_ctx(login) or {}
    except Exception:  # noqa: BLE001 - source login may be outside local_gsheet_sites
        ctx = {}
    return {
        "domain": (ctx.get("domain") or "").strip(),
        "city": (ctx.get("city") or "").strip(),
        "region": (ctx.get("oblast") or ctx.get("region") or "").strip(),
        "geoid": ctx.get("geoid"),
    }


def _copy_walk_strings(obj, fn):
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, list):
        return [_copy_walk_strings(x, fn) for x in obj]
    if isinstance(obj, dict):
        return {k: _copy_walk_strings(v, fn) for k, v in obj.items()}
    return obj


def _copy_replacement_forms(src: str, dst: str) -> list[tuple[str, str]]:
    src = (src or "").strip()
    dst = (dst or "").strip()
    if not src or not dst or src.lower() == dst.lower():
        return []
    forms = [(src, dst), (src.lower(), dst.lower()), (src.upper(), dst.upper()), (src.title(), dst.title())]
    out: list[tuple[str, str]] = []
    seen = set()
    for a, b in forms:
        key = (a, b)
        if a and key not in seen:
            out.append((a, b))
            seen.add(key)
    return out


def _copy_geo_replacements(source_ctx: dict, target_city: str, target_region: str) -> list[tuple[str, str]]:
    target_city = (target_city or "").strip()
    target_region = (target_region or "").strip()
    replacements: list[tuple[str, str]] = []
    replacements += _copy_replacement_forms(source_ctx.get("city") or "", target_city)
    replacements += _copy_replacement_forms(source_ctx.get("region") or "", target_region or target_city)

    # Частый сбой при копировании: источник вне local_gsheet_sites, но старое гео есть в названиях/текстах.
    if "краснодар" not in f"{target_city} {target_region}".lower():
        replacements += _copy_replacement_forms("Краснодарский край", target_region or target_city)
        replacements += _copy_replacement_forms("Краснодарского края", target_region or target_city)
        replacements += _copy_replacement_forms("Краснодар", target_city or target_region)

    out: list[tuple[str, str]] = []
    seen = set()
    for a, b in sorted(replacements, key=lambda p: len(p[0]), reverse=True):
        key = (a, b)
        if a and b and key not in seen:
            out.append((a, b))
            seen.add(key)
    return out


def _copy_scan_payload_terms(src_dir: Path, terms: list[str], *, limit: int = 8) -> list[str]:
    terms_l = [t.strip().lower() for t in terms if str(t or "").strip()]
    if not terms_l:
        return []
    hits: list[str] = []
    for name in _COPY_JSON_PAYLOADS:
        path = src_dir / name
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8").lower()
        except Exception:  # noqa: BLE001
            continue
        for term in terms_l:
            if term in text:
                hits.append(f"{name}: {term}")
                if len(hits) >= limit:
                    return hits
    return hits


def _copy_rewrite_snapshot_context(src_dir: Path, source_ctx: dict, target_ctx: dict) -> dict:
    """Replace source geo words in copied payloads before upload."""
    target_city = (target_ctx.get("city") or "").strip()
    target_region = (target_ctx.get("region") or "").strip()
    replacements = _copy_geo_replacements(source_ctx, target_city, target_region)

    if not replacements:
        return {"files": 0, "replacements": 0, "pairs": []}

    changed_files = 0
    changed_count = 0

    def repl(s: str) -> str:
        nonlocal changed_count
        out = s
        for a, b in replacements:
            if a in out:
                n = out.count(a)
                out = out.replace(a, b)
                changed_count += n
        return out

    for name in _COPY_JSON_PAYLOADS:
        path = src_dir / name
        if not path.exists():
            continue
        before = path.read_text(encoding="utf-8")
        data = json.loads(before)
        data = _copy_walk_strings(data, repl)
        after = json.dumps(data, ensure_ascii=False, indent=1)
        if after != before:
            path.write_text(after, encoding="utf-8")
            changed_files += 1
    forbidden = [a for a, _b in replacements if a.lower() not in f"{target_city} {target_region}".lower()]
    residual = _copy_scan_payload_terms(src_dir, forbidden)
    return {"files": changed_files, "replacements": changed_count, "pairs": replacements, "residual_geo": residual}


def _copy_target_href(href: str | None, source_domain: str, target_domain: str) -> str:
    href = str(href or "").strip()
    target = str(target_domain or "").strip().strip("/")
    if target and not target.startswith(("http://", "https://")):
        target_abs = "https://" + target
    else:
        target_abs = target
    if not href:
        return target_abs
    src = str(source_domain or "").strip()
    if src and target:
        return href.replace(src, target)
    return href


def _copy_snapshot_preflight(src_dir: Path, *, target_feed_url: str, target_city: str, target_region: str) -> dict:
    campaigns = _copy_read_json(src_dir / "campaigns.json")
    adgroups = _copy_read_json(src_dir / "adgroups.json")
    ads = _copy_read_json(src_dir / "ads.json")
    shopping_ads = _copy_read_json(src_dir / "shopping_ads.json")

    critical: list[str] = []
    warnings: list[str] = []
    unsupported = [c for c in campaigns if (c.get("Type") or "TEXT_CAMPAIGN") not in _COPY_SUPPORTED_V5_TYPES]
    if unsupported:
        sample = ", ".join(f"{c.get('Name') or c.get('Id')}[{c.get('Type')}]" for c in unsupported[:6])
        critical.append(
            "выбраны типы РК, которые старый direct_copy не восстанавливает корректно: "
            f"{sample}. Для UAC/tp6/tp7 и ЕПК нужен create_set/нейродиректолог, не snapshot-copy"
        )

    ads_by_group: dict[int, int] = {}
    for a in ads + shopping_ads:
        try:
            gid = int(a.get("AdGroupId") or 0)
        except Exception:  # noqa: BLE001
            gid = 0
        if gid:
            ads_by_group[gid] = ads_by_group.get(gid, 0) + 1
    empty_groups = [g for g in adgroups if int(g.get("Id") or 0) not in ads_by_group]
    if empty_groups:
        sample = ", ".join(str(g.get("Name") or g.get("Id")) for g in empty_groups[:8])
        critical.append(f"в snapshot есть группы без объявлений ({len(empty_groups)}): {sample}")

    feed_group_count = sum(1 for g in adgroups if (g.get("TextAdGroupFeedParams") or {}).get("FeedId"))
    shopping_count = len(shopping_ads)
    if (feed_group_count or shopping_count) and not target_feed_url:
        critical.append("есть товарные/каталожные группы или ShoppingAd, но целевой фид не задан")
    if shopping_count and not campaigns:
        critical.append("есть ShoppingAd без выбранных кампаний — snapshot неконсистентен")

    target_geo = " ".join(x for x in (target_city, target_region) if x).strip()
    if not target_geo:
        critical.append("целевое гео пустое")

    return {
        "critical": critical,
        "warnings": warnings,
        "campaigns": len(campaigns),
        "adgroups": len(adgroups),
        "ads": len(ads),
        "shopping_ads": shopping_count,
        "feed_groups": feed_group_count,
    }


def _copy_build_results(src_dir: Path, workdir: Path) -> list[dict]:
    """Build create_set-like result rows from direct_copy id_maps for live verification."""
    maps = _copy_read_json(workdir / "id_maps.json") if (workdir / "id_maps.json").exists() else {}
    camp_map = maps.get("campaigns") or {}
    adgroup_map = maps.get("adgroups") or {}
    ad_map = maps.get("ads") or {}
    campaigns = _copy_read_json(src_dir / "campaigns.json")
    adgroups = _copy_read_json(src_dir / "adgroups.json")
    ads = _copy_read_json(src_dir / "ads.json")
    shopping_ads = _copy_read_json(src_dir / "shopping_ads.json")

    groups_by_campaign: dict[int, int] = {}
    ads_by_campaign: dict[int, int] = {}
    shopping_by_campaign: dict[int, int] = {}
    for g in adgroups:
        try:
            src_gid = str(int(g.get("Id") or 0))
            src_cid = int(g.get("CampaignId") or 0)
        except Exception:  # noqa: BLE001
            continue
        if src_gid in adgroup_map:
            groups_by_campaign[src_cid] = groups_by_campaign.get(src_cid, 0) + 1
    for a in ads:
        try:
            src_aid = str(int(a.get("Id") or 0))
            src_cid = int(a.get("CampaignId") or 0)
        except Exception:  # noqa: BLE001
            continue
        if src_aid in ad_map:
            ads_by_campaign[src_cid] = ads_by_campaign.get(src_cid, 0) + 1
    for a in shopping_ads:
        try:
            src_aid = str(int(a.get("Id") or 0))
            src_cid = int(a.get("CampaignId") or 0)
        except Exception:  # noqa: BLE001
            continue
        if src_aid in ad_map:
            ads_by_campaign[src_cid] = ads_by_campaign.get(src_cid, 0) + 1
            shopping_by_campaign[src_cid] = shopping_by_campaign.get(src_cid, 0) + 1

    out = []
    for c in campaigns:
        src_id = str(c.get("Id") or "")
        target_id = camp_map.get(src_id)
        name = str(c.get("Name") or "").strip()
        if target_id:
            out.append({
                "ok": True,
                "id": int(target_id),
                "campaign_id": int(target_id),
                "name": name,
                "result": {
                    "build": {
                        "groups": groups_by_campaign.get(int(c.get("Id") or 0), 0),
                        "ads": ads_by_campaign.get(int(c.get("Id") or 0), 0),
                        "shopping_ads": shopping_by_campaign.get(int(c.get("Id") or 0), 0),
                    }
                },
            })
        else:
            out.append({"ok": False, "name": name, "error": "campaign not mapped by direct_copy"})
    return out


def _copy_selected_grid_campaigns(login: str, selected_ids: set[int]) -> list[dict]:
    if not selected_ids:
        return []
    try:
        rows = _grid_list_campaigns(login)
    except Exception:
        return []
    out = []
    for row in rows or []:
        try:
            cid = int(row.get("id") or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid in selected_ids:
            out.append(row)
    return out


def _copy_is_uac_grid_row(row: dict) -> bool:
    typ = str(row.get("typename") or row.get("type") or "").lower()
    name = str(row.get("name") or "").lower()
    return "uac" in typ or "tp6_" in name or "tp7_" in name


def _copy_uac_value(row: dict, *keys, default=None):
    for key in keys:
        val = row.get(key)
        if val not in (None, "", []):
            return val
    return default


def _copy_uac_strings(value, *keys: str, limit: int = 8) -> list[str]:
    """Extract UAC text arrays from either strings or browser dict rows."""
    vals = []
    if isinstance(value, dict):
        raw = _copy_uac_value(value, *keys, default=[])
    else:
        raw = value
    for item in (raw or []):
        if isinstance(item, dict):
            text = ""
            for key in ("text", "title", "value", "body", "name"):
                text = str(item.get(key) or "").strip()
                if text:
                    break
        else:
            text = str(item or "").strip()
        if text and text not in vals:
            vals.append(text)
        if len(vals) >= limit:
            break
    return vals


def _copy_uac_sitelinks(value, *, source_domain: str, target_domain: str) -> list[dict]:
    out = []
    for item in (value or []):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("Title") or item.get("text") or "").strip()
        href = str(item.get("href") or item.get("Href") or item.get("url") or "").strip()
        desc = str(item.get("description") or item.get("Description") or "").strip()
        if not title:
            continue
        out.append({"title": title, "href": _copy_target_href(href, source_domain, target_domain), "description": desc})
    return out


def _copy_uac_media_urls(row: dict, *, want: str) -> list[str]:
    """Find reusable media URLs in unstable UAC detail payloads."""
    rx = re.compile(r"https?://[^\s\"'<>]+", re.I)
    urls: list[str] = []
    seen: set[str] = set()
    image_ext = (".jpg", ".jpeg", ".png", ".webp", ".gif")
    video_ext = (".mp4", ".mov", ".webm")
    preferred_keys = {
        "image": ("images", "image_urls", "media", "contents", "content"),
        "video": ("videos", "video_urls", "media", "contents", "content"),
    }.get(want, ())

    def ok(url: str) -> bool:
        low = url.lower().split("?", 1)[0]
        if want == "video":
            return low.endswith(video_ext)
        return low.endswith(image_ext) or any(x in low for x in ("/image/", "/img/", "avatars.mds.yandex.net"))

    def walk(node):
        if isinstance(node, dict):
            for key, val in node.items():
                key_l = str(key).lower()
                if key_l in {"url", "href", "source_url", "preview_url", "download_url"} and isinstance(val, str):
                    for u in rx.findall(val):
                        if ok(u) and u not in seen:
                            seen.add(u); urls.append(u)
                else:
                    walk(val)
        elif isinstance(node, list):
            for val in node:
                walk(val)
        elif isinstance(node, str):
            for u in rx.findall(node):
                if ok(u) and u not in seen:
                    seen.add(u); urls.append(u)

    for key in preferred_keys:
        walk(row.get(key))
    return urls[:5 if want == "image" else 2]


def _copy_uac_filter_list(value) -> list[dict]:
    if isinstance(value, list):
        return value
    return []


def _copy_uac_campaigns(source_login: str, target_login: str, target_agency: str,
                        selected_grid_rows: list[dict], body: dict, *,
                        target_href: str, region_ids: list[int], counter_id: int,
                        goal_id: int, target_feed_id: int | None) -> dict:
    """Recreate selected UAC/tp6/tp7 campaigns from source detail into target account."""
    rep = {"created": 0, "results": [], "errors": [], "uses_direct_units": False}
    rows = [r for r in selected_grid_rows if _copy_is_uac_grid_row(r)]
    if not rows:
        return rep
    try:
        from .uac_read import UacReadClient
        source_reader = UacReadClient(source_login)
        target_client = cmc.build_client(target_login, account=(target_agency or None))
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"uac init: {str(e)[:220]}")
        return rep

    default_cpa = int(body.get("cpa") or 2000)
    default_budget = int(body.get("week_budget") or body.get("budget") or 5000)
    for row in rows:
        try:
            src_id = int(row.get("id") or 0)
        except (TypeError, ValueError):
            src_id = 0
        name = str(row.get("name") or "").strip() or f"copy-uac-{src_id}"
        if src_id <= 0:
            continue
        try:
            d = source_reader.campaign_detail(src_id)
            source_domain = str(body.get("_copy_source_domain") or "").strip()
            target_domain = str(body.get("target_domain") or "").strip()
            titles = _copy_uac_strings(d, "titles", "title_items", limit=5)
            texts = _copy_uac_strings(d, "texts", "text_items", limit=3)
            sitelinks = _copy_uac_sitelinks(_copy_uac_value(d, "sitelinks", default=[]) or [],
                                            source_domain=source_domain, target_domain=target_domain)
            keywords = _copy_uac_strings(d, "keywords", limit=200)
            minus_keywords = _copy_uac_strings(d, "minus_keywords", limit=200)
            audiences = _copy_uac_value(d, "audiences", "interest_ids", default=[]) or []
            pricing = str(_copy_uac_value(d, "pricing", "payment_type", "paymentType", default="PER_CLICK") or "PER_CLICK")
            week_limit = _copy_uac_value(d, "week_limit", "weekly_budget", "weekBudget", default=default_budget)
            cpa = default_cpa
            goals = _copy_uac_value(d, "goals", default=[]) or []
            if isinstance(goals, list) and goals and isinstance(goals[0], dict):
                cpa = int(goals[0].get("cpa") or cpa)
            if not titles:
                titles = ["Автомобили в наличии", "Выгода на авто", "Официальный дилер"]
            if not texts:
                texts = ["Подберите автомобиль с выгодой. Оставьте заявку на сайте."]
            is_product = name.lower().startswith("tp7_") or bool(_copy_uac_value(d, "feed_id", "listings_feed_id"))
            feed_id = int(target_feed_id or 0) if is_product else None
            spec = cmc.MasterCampaignSpec(
                href=target_href,
                titles=titles[:5],
                texts=texts[:3],
                region_ids=region_ids,
                counter_id=int(counter_id),
                goal_id=int(goal_id),
                cpa=cpa,
                week_budget=float(week_limit or default_budget),
                campaign_type=("product" if is_product else "master"),
                feed_id=feed_id,
                listings_feed_id=feed_id,
                feed_filters=_copy_uac_filter_list(d.get("feed_filters")),
                listings_feed_filters=_copy_uac_filter_list(d.get("listings_feed_filters")),
                display_name=name,
                pricing=pricing,
                keywords=keywords,
                minus_keywords=minus_keywords or ["отзывы"],
                sitelinks=sitelinks,
                image_urls=_copy_uac_media_urls(d, want="image"),
                video_urls=_copy_uac_media_urls(d, want="video"),
                audiences=audiences if isinstance(audiences, list) else [],
                limit_period=str(_copy_uac_value(d, "limit_period", "limitPeriod", default="week") or "week"),
                alternative_texts_enabled=False,
                ml_banners_enabled=False,
                yandex_maps_enabled=False,
                utm_template=cmc.UTM_TEMPLATE,
            )
            cid = target_client.create_master_campaign(spec, launch=False)
            res = {"ok": True, "id": int(cid), "campaign_id": int(cid), "name": name, "kind": "uac", "source_id": src_id}
            rep["results"].append(res)
            rep["created"] += 1
        except Exception as e:  # noqa: BLE001
            msg = str(e)[:260]
            rep["errors"].append(f"{name}: {msg}")
            rep["results"].append({"ok": False, "name": name, "source_id": src_id, "error": msg})
    return rep


def _copy_target_feed_id(target_login: str, target_agency: str, workdir: Path) -> int | None:
    maps = _copy_read_json(workdir / "id_maps.json") if (workdir / "id_maps.json").exists() else {}
    for raw in (maps.get("feeds") or {}).values():
        try:
            fid = int(raw)
        except (TypeError, ValueError):
            fid = 0
        if fid > 0:
            return fid
    try:
        rows = _filter_allowed_feed_rows(_grid_feeds(target_login, target_agency))
        for row in rows:
            try:
                fid = int(row.get("id") or 0)
            except (TypeError, ValueError):
                fid = 0
            if fid > 0:
                return fid
    except Exception:  # noqa: BLE001
        pass
    return None


def _copy_cookie_postprocess(job_id: str, target_login: str, target_agency: str,
                             src_dir: Path, workdir: Path, body: dict) -> dict:
    """Cookie/Grid fallback after direct_copy upload: callouts, ShoppingAd, ListingAd, verification, repair."""
    rep = {
        "callouts_created_or_found": 0,
        "callouts_attached_campaigns": 0,
        "keywords_added": 0,
        "promos_created": 0,
        "promos_attached_campaigns": 0,
        "shopping_added": 0,
        "listing_added": 0,
        "skipped": [],
        "errors": [],
        "uses_direct_units": False,
    }
    maps_path = workdir / "id_maps.json"
    maps = _copy_read_json(maps_path) if maps_path.exists() else {}
    maps.setdefault("campaigns", {})
    maps.setdefault("adgroups", {})
    maps.setdefault("ads", {})
    maps.setdefault("feeds", {})
    maps.setdefault("callouts", {})
    maps.setdefault("promotions", {})

    try:
        client = cmc.build_client(target_login, account=(target_agency or None))
        cookie = client.sess.headers.get("Cookie") or ""
        grid = gf.GridClient(target_login, cookie=cookie)
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"cookie init: {str(e)[:220]}")
        return rep

    # 1) Уточнения: если v5 adextensions.add не создал часть ids, добираем через Grid и
    # прикрепляем на campaign-level, чтобы объявления получили наследуемые callouts.
    callouts = _copy_read_json(src_dir / "callouts.json")
    callout_texts = []
    for c in callouts:
        txt = str(((c.get("Callout") or {}).get("CalloutText")) or "").strip()
        if txt:
            callout_texts.append(txt)
    if callout_texts:
        try:
            callout_map = grid.add_callouts(callout_texts)
            for c in callouts:
                src_id = str(c.get("Id") or "")
                txt = str(((c.get("Callout") or {}).get("CalloutText")) or "").strip()
                if src_id and txt and callout_map.get(txt):
                    maps["callouts"][src_id] = int(callout_map[txt])
            co_ids = list(dict.fromkeys(int(x) for x in maps["callouts"].values() if str(x).isdigit()))
            camp_ids = [int(x) for x in maps["campaigns"].values() if str(x).isdigit()]
            if co_ids and camp_ids:
                updated = grid.set_campaign_callouts(camp_ids, co_ids[:_CALLOUT_PER_CAMPAIGN_CAP])
                rep["callouts_attached_campaigns"] = len(updated or camp_ids)
            rep["callouts_created_or_found"] = len(callout_map)
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"callouts grid: {str(e)[:220]}")

    # 1a) Промоакции: official promotions.add часто заблокирован, поэтому переносим
    # библиотечные промо через Grid addPromoExtensions. Attachment делаем только если промо одно:
    # иначе неизвестна source-связь "какое промо к какой кампании" и blind attach опасен.
    promotions = _copy_read_json(src_dir / "promotions.json")
    created_promo_ids = []
    if promotions:
        try:
            from .promo import PromoClient
            pc = PromoClient(client, target_login)
            for p in promotions:
                src_id = str(p.get("Id") or "")
                if src_id and src_id in maps["promotions"]:
                    created_promo_ids.append(int(maps["promotions"][src_id]))
                    continue
                pid, perr = pc.add(
                    type=p.get("Type") or "DISCOUNT",
                    description=p.get("Description") or p.get("Name") or "акция",
                    href=_copy_target_href(
                        p.get("Href"),
                        str(body.get("_copy_source_domain") or ""),
                        str(body.get("target_domain") or ""),
                    ),
                    amount=p.get("Amount"),
                    unit=p.get("AmountUnit"),
                    prefix=p.get("AmountPrefix"),
                    promocode=p.get("Promocode"),
                    start=p.get("StartDate"),
                    finish=p.get("EndDate"),
                )
                if pid:
                    maps["promotions"][src_id] = int(pid)
                    created_promo_ids.append(int(pid))
                elif perr:
                    rep["errors"].append(f"promo {src_id}: {perr[:180]}")
            rep["promos_created"] = len(created_promo_ids)
            unique_promos = list(dict.fromkeys(created_promo_ids))
            camp_ids = [int(x) for x in maps["campaigns"].values() if str(x).isdigit()]
            if len(unique_promos) == 1 and camp_ids:
                attach = pc.attach(unique_promos[0], camp_ids)
                errors = (((attach.get("data") or {}).get("updateCampaignsPromoExtension") or {})
                          .get("validationResult") or {}).get("errors") or attach.get("errors")
                if errors:
                    rep["errors"].append("promo attach: " + json.dumps(errors, ensure_ascii=False)[:180])
                else:
                    rep["promos_attached_campaigns"] = len(camp_ids)
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"promos grid: {str(e)[:220]}")

    # 1b) Keywords fallback по куки. Если v5 keywords.add упёрся в 152, direct_copy оставляет
    # фразы отсутствующими в keywords_done.json. Добираем их через Grid addKeywords.
    keywords = _copy_read_json(src_dir / "keywords.json")
    done_kw_path = workdir / "keywords_done.json"
    done_kw = set(_copy_read_json(done_kw_path)) if done_kw_path.exists() else set()
    kw_items = []
    kw_keys = []
    for k in keywords:
        key = f"{k.get('AdGroupId')}|{k.get('Keyword')}"
        if key in done_kw:
            continue
        gid = maps["adgroups"].get(str(k.get("AdGroupId") or ""))
        phrase = str(k.get("Keyword") or "").strip()
        if not gid or not phrase or phrase.startswith("---"):
            continue
        row = {"adgroup_id": int(gid), "keyword": phrase}
        bid = k.get("Bid")
        if bid is not None:
            try:
                row["price"] = float(bid) / 1_000_000
            except (TypeError, ValueError):
                pass
        kw_items.append(row)
        kw_keys.append(key)
    if kw_items:
        try:
            added = grid.add_keywords(kw_items)
            added_count = len(added or [])
            for key in kw_keys[:added_count]:
                done_kw.add(key)
            _copy_write_json(done_kw_path, sorted(done_kw))
            rep["keywords_added"] = added_count
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"keywords grid: {str(e)[:220]}")

    # 2) ShoppingAd fallback по куки. direct_copy пытается v501 ads.add; если баллов не хватило
    # или v501 отклонил, source ShoppingAd останется без maps['ads'][src_id].
    shopping_ads = _copy_read_json(src_dir / "shopping_ads.json")
    shop_items = []
    shop_sources = []
    for sa in shopping_ads:
        src_ad_id = str(sa.get("Id") or "")
        if src_ad_id and src_ad_id in maps["ads"]:
            continue
        gid = maps["adgroups"].get(str(sa.get("AdGroupId") or ""))
        sad = sa.get("ShoppingAd") or {}
        fid = maps["feeds"].get(str(sad.get("FeedId") or ""))
        if not gid or not fid:
            rep["skipped"].append({"shopping_ad": src_ad_id, "reason": "no mapped adgroup/feed"})
            continue
        item = {"adgroup_id": int(gid), "feed_id": int(fid)}
        # Preserve simple official API filters where possible.
        raw_conds = sad.get("FeedFilterConditions") or []
        if isinstance(raw_conds, dict):
            raw_conds = raw_conds.get("Items") or []
        for cond in raw_conds:
            if not isinstance(cond, dict):
                continue
            op = str(cond.get("Operand") or "")
            args = cond.get("Arguments") or []
            if op == "collectionId" and args:
                item["collection_id"] = str(args[0])
            elif op == "vendor" and args:
                item["vendor"] = str(args[0])
            elif op == "model" and args:
                item["model"] = [str(x) for x in args]
        shop_items.append(item)
        shop_sources.append(src_ad_id)
    if shop_items:
        try:
            new_ids = grid.add_shopping_ads(shop_items)
            mapped_new_ids = []
            for src_id, new_id in zip(shop_sources, new_ids):
                if new_id:
                    maps["ads"][str(src_id)] = int(new_id)
                    mapped_new_ids.append(new_id)
            rep["shopping_added"] = len(mapped_new_ids)
            if mapped_new_ids:
                listing = grid.add_listing_ads_by_shopping_ads(mapped_new_ids)
                rep["listing_added"] = len(listing or [])
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"shopping/listing grid: {str(e)[:220]}")

    _copy_write_json(maps_path, maps)

    # 3) Grid-first live verification + safe auto-repair.
    results = _copy_build_results(src_dir, workdir) + list(body.get("_copy_uac_results") or [])
    copy_body = {
        **body,
        "items": [{"name": r.get("name"), "type": "copy"} for r in results if r.get("name")],
        "callouts": callout_texts,
        "_skip_auto_queued_repair": True,
    }
    try:
        live = _create_set_live_verification(target_login, results, agency=target_agency, use_v5=False)
        rep["live_verification"] = live
        try:
            gate = rgate.summarize_repair_gate(copy_body, results, (live or {}).get("repair_plan") or {})
        except Exception as e:  # noqa: BLE001
            gate = {"status": "error", "error": str(e)[:180]}
        rep["repair_gate"] = gate
        if int((gate or {}).get("executable_now") or 0) > 0:
            ctx = {"login": target_login, "agency": target_agency, "body": copy_body, "results": results}
            auto = rauto.execute_safe_post_create(
                target_login,
                ctx,
                (live or {}).get("repair_plan") or {},
                _repair_deps(),
                post_verify=_attach_post_repair_verification,
            )
            rep["auto_repair"] = auto
            if (auto or {}).get("post_repair_live_verification"):
                rep["live_verification"] = auto["post_repair_live_verification"]
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"verification/repair: {str(e)[:220]}")
    rep["results"] = results
    return rep


def _copy_rewrite_strategy_goal(strategy: dict, goal_id: int) -> dict:
    """Проставить новую цель «Все формы» во всех goal-bearing стратегиях."""
    s = json.loads(json.dumps(strategy or {}))
    for side in ("Search", "Network"):
        blk = s.get(side) or {}
        for k in ("AverageCpa", "PayForConversion", "WbMaximumConversionRate", "AverageCrr", "PayForConversionCrr", "AverageRoi"):
            if isinstance(blk.get(k), dict):
                blk[k]["GoalId"] = int(goal_id)
    return s


def _copy_apply_metrika(login: str, token: str, src_dir: Path, workdir: Path,
                        counter_id: int, goal_id: int, source_ids: set[int], job_id: str) -> dict:
    """Докрутить на скопированных кампаниях счётчик Метрики и goal_id."""
    maps = _copy_read_json(workdir / "id_maps.json") if (workdir / "id_maps.json").exists() else {}
    camp_map = maps.get("campaigns") or {}
    campaigns = [c for c in _copy_read_json(src_dir / "campaigns.json") if int(c.get("Id") or 0) in source_ids]
    updated = 0
    warned = 0
    for c in campaigns:
        src_id = str(c.get("Id"))
        tgt_id = camp_map.get(src_id)
        if not tgt_id:
            continue
        ctype = c.get("Type", "TEXT_CAMPAIGN")
        struct_key = {
            "TEXT_CAMPAIGN": "TextCampaign",
            "DYNAMIC_TEXT_CAMPAIGN": "DynamicTextCampaign",
            "SMART_CAMPAIGN": "SmartCampaign",
            "CPM_BANNER_CAMPAIGN": "CpmBannerCampaign",
        }.get(ctype)
        if not struct_key:
            warned += 1
            _copy_job_log(job_id, f"метрика: {c.get('Name') or src_id} — тип {ctype} оставлен без авто-докрутки стратегии")
            continue
        type_data = c.get(struct_key) or {}
        body = {"Id": int(tgt_id), struct_key: {}}
        if struct_key == "SmartCampaign":
            body[struct_key]["CounterId"] = int(counter_id)
        else:
            body[struct_key]["CounterIds"] = {"Items": [int(counter_id)]}
        if type_data.get("TrackingParams"):
            body[struct_key]["TrackingParams"] = type_data["TrackingParams"]
        if type_data.get("AttributionModel"):
            body[struct_key]["AttributionModel"] = type_data["AttributionModel"]
        strategy = type_data.get("BiddingStrategy") or {}
        if strategy:
            body[struct_key]["BiddingStrategy"] = _copy_rewrite_strategy_goal(strategy, goal_id)
        try:
            j = _v5_call("campaigns", "update", token, login, {"Campaigns": [body]})
            if "error" in j:
                warned += 1
                _copy_job_log(job_id, f"метрика update {c.get('Name') or src_id}: {_v5_err(j)[:220]}")
                continue
            updated += 1
        except Exception as e:  # noqa: BLE001
            warned += 1
            _copy_job_log(job_id, f"метрика update {c.get('Name') or src_id}: {str(e)[:220]}")
    return {"updated": updated, "warned": warned}


def _copy_run_job(job_id: str, body: dict) -> None:
    source_login = (body.get("source_login") or "").strip()
    target_login = (body.get("target_login") or "").strip()
    selected_ids = {int(x) for x in (body.get("campaign_ids") or []) if str(x).isdigit()}
    counter_id = int(body.get("counter_id") or 0)
    goal_id = int(body.get("goal_id") or 0)
    target_domain = (body.get("target_domain") or "").strip()
    target_city = (body.get("target_city") or "").strip()
    target_region = (body.get("target_region") or "").strip()
    target_feed_url = (body.get("target_feed_url") or _COPY_DEFAULT_FEED_PATH).strip()
    try:
        tmp_root = Path(tempfile.gettempdir())
        tmp_root.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix=f"direct-copy-{job_id[:8]}-", dir=str(tmp_root)))
        src_dir = workdir / "source"
        _copy_job_upsert(job_id, status="running", progress=5, workdir=str(workdir))
        dc = _direct_copy_module()
        selected_grid_rows = _copy_selected_grid_campaigns(source_login, selected_ids)
        selected_uac_rows = [r for r in selected_grid_rows if _copy_is_uac_grid_row(r)]
        if selected_uac_rows:
            _copy_job_log(job_id, f"uac selected: {len(selected_uac_rows)} кампаний через Grid/UAC")
        _copy_job_log(job_id, f"pull источника {source_login}")
        src_auth = dc.find_working_auth(source_login, cookie_account=source_login)
        dc.phase_pull(src_dir, src_auth, source_login)
        meta = _copy_filter_snapshot(src_dir, selected_ids)
        _copy_job_upsert(job_id, progress=28, total=int(meta.get("campaigns") or len(selected_ids)))
        _copy_job_log(job_id, f"snapshot отфильтрован: {meta.get('campaigns')} кампаний")
        expected_snapshot = max(0, len(selected_ids) - len(selected_uac_rows))
        if int(meta.get("campaigns") or 0) != expected_snapshot:
            raise RuntimeError(
                f"snapshot неполный: выбрано {len(selected_ids)}, UAC/tp6/tp7 {len(selected_uac_rows)}, "
                f"в v5 snapshot {meta.get('campaigns')} вместо {expected_snapshot}"
            )
        target_feed_abs = dc.build_url_feed_url(target_domain, target_feed_url) if target_feed_url else ""
        audit = _copy_snapshot_preflight(
            src_dir,
            target_feed_url=target_feed_abs,
            target_city=target_city,
            target_region=target_region,
        )
        _copy_job_upsert(job_id, preflight=audit)
        for msg in audit.get("warnings") or []:
            _copy_job_log(job_id, f"preflight warning: {msg}")
        if audit.get("critical"):
            for msg in audit["critical"]:
                _copy_job_log(job_id, f"preflight error: {msg}")
            raise RuntimeError("preflight остановил копирование: " + "; ".join(audit["critical"][:3]))

        source_ctx = _copy_ctx(source_login)
        target_ctx = _copy_ctx(target_login)
        target_ctx["city"] = target_city or target_ctx.get("city") or ""
        target_ctx["region"] = target_region or target_ctx.get("region") or ""
        rewrite_meta = _copy_rewrite_snapshot_context(src_dir, source_ctx, target_ctx)
        _copy_job_upsert(job_id, context_rewrite=rewrite_meta)
        if rewrite_meta.get("replacements"):
            _copy_job_log(job_id, f"гео в snapshot заменено: {rewrite_meta['replacements']} в {rewrite_meta['files']} файлах")
        if rewrite_meta.get("residual_geo"):
            sample = ", ".join(rewrite_meta["residual_geo"][:5])
            raise RuntimeError(f"после гео-замены в snapshot осталось старое гео: {sample}")

        tgt_auth = dc.find_working_auth(target_login, cookie_account=target_login)
        src_domain = dc.infer_source_domain(src_dir)
        tgt_region_id = None
        geo_source = ""
        if target_city or target_region:
            local_gid, local_geo_name = _geo_id(target_city, target_region)
            if local_gid:
                tgt_region_id = int(local_gid)
                geo_source = f"dict:{local_geo_name}"
            else:
                tgt_region_id = dc.lookup_geo_region_id(target_city, target_region, tgt_auth, target_login)
                geo_source = "direct_copy"
            if not tgt_region_id:
                raise RuntimeError(f"не найден GeoRegionId для целевого гео: city={target_city!r}, region={target_region!r}")
        body["_copy_source_domain"] = src_domain
        _copy_job_upsert(job_id, progress=42)
        _copy_job_log(job_id, f"upload в {target_login} (домен={target_domain or '—'}, geo={target_city or target_region or '—'} #{tgt_region_id or '—'} {geo_source}, feed={target_feed_abs or '—'})")
        dc.phase_upload(
            src_dir, workdir, tgt_auth, source_login, target_login,
            src_domain, target_domain, tgt_region_id,
            force_feed_url=target_feed_abs,
            force_feed_name=target_feed_abs.rsplit("/", 1)[-1] if target_feed_abs else None,
        )
        _copy_job_upsert(job_id, progress=82)
        token, _ag = _token_for_login(target_login, "", _direct_tokens())
        metrika_res = {"updated": 0, "warned": 0}
        if token:
            _copy_job_log(job_id, f"докрутка Метрики: counter={counter_id}, goal={goal_id}")
            metrika_res = _copy_apply_metrika(target_login, token, src_dir, workdir, counter_id, goal_id, selected_ids, job_id)
        target_agency = _ag or _resolve_agency_hint(target_login, "")
        uac_copy = {"created": 0, "results": [], "errors": [], "uses_direct_units": False}
        if selected_uac_rows:
            target_feed_id = _copy_target_feed_id(target_login, target_agency or "", workdir)
            region_id_list = [int(tgt_region_id)] if tgt_region_id else [225]
            target_href = _copy_target_href(None, "", target_domain)
            _copy_job_log(job_id, f"uac copy: {len(selected_uac_rows)} → {target_login} (feed={target_feed_id or '—'})")
            uac_copy = _copy_uac_campaigns(
                source_login, target_login, target_agency or "", selected_uac_rows, body,
                target_href=target_href, region_ids=region_id_list, counter_id=counter_id,
                goal_id=goal_id, target_feed_id=target_feed_id,
            )
            body["_copy_uac_results"] = uac_copy.get("results") or []
            if uac_copy.get("errors"):
                for err in uac_copy["errors"][:8]:
                    _copy_job_log(job_id, f"uac copy warning: {err}")
            _copy_job_log(job_id, f"uac copy done: {uac_copy.get('created') or 0} created")
        _copy_job_log(job_id, "cookie postprocess: уточнения / ShoppingAd / ListingAd / live-check / auto-repair")
        cookie_post = _copy_cookie_postprocess(job_id, target_login, target_agency or "", src_dir, workdir, body)
        if cookie_post.get("errors"):
            for err in cookie_post["errors"][:8]:
                _copy_job_log(job_id, f"cookie postprocess warning: {err}")
        live_status = ((cookie_post.get("live_verification") or {}).get("status") or "")
        if live_status:
            _copy_job_log(job_id, f"live verification: {live_status}")
        _copy_job_upsert(
            job_id, status="done", progress=100,
            result={
                "source_login": source_login,
                "target_login": target_login,
                "selected": len(selected_ids),
                "snapshot": meta,
                "metrika": metrika_res,
                "uac_copy": uac_copy,
                "cookie_postprocess": cookie_post,
                "results": cookie_post.get("results") or _copy_build_results(src_dir, workdir),
                "live_verification": cookie_post.get("live_verification"),
                "repair_gate": cookie_post.get("repair_gate"),
                "auto_repair": cookie_post.get("auto_repair"),
                "preflight": audit,
                "context_rewrite": rewrite_meta,
                "target_feed_url": target_feed_abs,
                "workdir": str(workdir),
            })
    except BaseException as e:  # noqa: BLE001
        _copy_job_upsert(job_id, status="error", error=str(e)[:500], progress=100)
        _copy_job_log(job_id, f"ошибка: {str(e)[:300]}")
    finally:
        # Артефакты оставляем во временной папке до ручной очистки — полезно для отладки id_maps/upload_log.
        pass


def _bump_job(job, ok: bool = True, n: int = 1) -> None:
    """Инкремент счётчиков по ФАКТУ созданной кампании (fan-out даёт N кампаний на 1 пункт плана)."""
    if not job:
        return
    if ok:
        job["created"] = int(job.get("created") or 0) + n
    else:
        job["failed"] = int(job.get("failed") or 0) + n
    job["_heartbeat"] = time.time()   # watchdog: прогресс по ЛЮБОЙ кампании (создание/ошибка) = живой


def _bump_item(job) -> None:
    """Инкремент set_done: вызывать ОДИН РАЗ после завершения каждого item набора (не за каждую кампанию fan-out)."""
    if not job:
        return
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
                    created_at timestamptz DEFAULT now(),
                    updated_at timestamptz DEFAULT now()
                )""")
            # миграция: добавить колонки если таблица уже существовала без них
            cur.execute("ALTER TABLE public.direct_automation_jobs ADD COLUMN IF NOT EXISTS body jsonb")
            cur.execute("ALTER TABLE public.direct_automation_jobs ADD COLUMN IF NOT EXISTS agency text")
            cur.execute("ALTER TABLE public.direct_automation_jobs ADD COLUMN IF NOT EXISTS errors_log jsonb")
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
            cur.execute("SELECT DISTINCT login FROM public.direct_automation_jobs "
                        "WHERE status IN ('queued','running') AND login IS NOT NULL "
                        "  AND updated_at > now() - interval '6 hours'")
            _interrupted_logins = [r["login"] for r in cur.fetchall() if r.get("login")]
            # битые running/queued → interrupted (single UPDATE)
            cur.execute("UPDATE public.direct_automation_jobs SET status='interrupted', updated_at=now() "
                        "WHERE status IN ('queued','running')")
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
        def _bg_sweep(logins):
            time.sleep(8)                                # дать сервису и воркеру подняться
            for lg in logins:
                try:
                    n = _sweep_empty_drafts(lg)
                    if n:
                        print(f"[startup-sweep] {lg}: удалено пустых ЕПК-черновиков: {n}", flush=True)
                except Exception:  # noqa: BLE001
                    pass
        threading.Thread(target=_bg_sweep, args=(list(_interrupted_logins),), daemon=True).start()


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
        if timed_out:
            _CREATE_COND.notify_all()
    for jid, snap in timed_out:
        _job_db_save(jid, snap, full=True)
    _jobs_db_mark_stale_running(_CREATE_RUNNING_TIMEOUT)


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
_RESUME_POLL = 600                                    # период опроса демона, сек (~10 мин)
_DEFERRED_STALE_HOURS = 3                             # 'resumed'-остаток без финала дольше N часов = осиротел
                                                      # (джоба умерла при рестарте) → вернуть в waiting+now()
_DELAYED_REPAIR_DAEMON = {"started": False}
_DELAYED_REPAIR_POLL = 60
_DELAYED_CONTENT_REPAIR_DELAY_SECONDS = 180


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
                   job_id: str | None, resume_count: int = 0) -> str | None:
    """Сохранить остаток набора для авто-докрутки после сброса баллов. → id или None."""
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
    except Exception:  # noqa: BLE001
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
                                 *, delay_seconds: int = _DELAYED_CONTENT_REPAIR_DELAY_SECONDS) -> str | None:
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
                VALUES (%s,%s,%s,%s,'content_repair','waiting',0,
                        now() + (%s || ' seconds')::interval)
                ON CONFLICT (parent_job_id, kind) DO NOTHING
                RETURNING id
            """, (did, parent_job_id, login, agency or "", str(int(delay_seconds))))
            row = cur.fetchone()
            conn.commit()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


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
    )
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
    did = (row.get("id") or "").strip()
    parent_job_id = (row.get("parent_job_id") or "").strip()
    _delayed_repair_set_status(did, "running", "повторная Grid-first проверка перед content repair")
    job, result, ctx, err = _create_set_job_context(parent_job_id)
    if err:
        out = {"ok": False, "error": err[0].get("error"), "uses_direct_units": False}
        _delayed_repair_set_status(did, "error", out["error"], out)
        _record_delayed_content_repair(parent_job_id, {"id": did, "status": "error", **out})
        return
    login = (ctx.get("login") or row.get("login") or "").strip()
    if not login:
        out = {"ok": False, "error": "login не сохранён в job", "uses_direct_units": False}
        _delayed_repair_set_status(did, "error", out["error"], out)
        _record_delayed_content_repair(parent_job_id, {"id": did, "status": "error", **out})
        return
    try:
        live_report = _create_set_live_verification(
            login,
            ctx.get("results") or [],
            agency=ctx.get("agency") or row.get("agency") or "",
            use_v5=False,
        )
        plan = (live_report or {}).get("repair_plan") or {}
        content_repairs, unsupported = rgate.executable_content_repairs(ctx.get("body") or {}, plan)
        if not content_repairs:
            out = {
                "ok": True,
                "skipped": True,
                "reason": "content_repair_not_confirmed_after_delay",
                "live_verification": live_report,
                "uses_direct_units": False,
            }
            _delayed_repair_set_status(did, "skipped", out["reason"], out)
            _record_delayed_content_repair(parent_job_id, {"id": did, "status": "skipped", **out})
            return
        out, status = rex.execute_content_repair(login, ctx, content_repairs, _repair_deps())
        _attach_post_repair_verification(out, login, ctx)
        out.update({
            "delayed_repair_id": did,
            "parent_job_id": parent_job_id,
            "repair_plan": plan,
            "executed_actions": [{k: v for k, v in a.items() if k != "item"} for a in content_repairs][:40],
            "unsupported_actions": unsupported[:40],
        })
        final_status = "done" if 200 <= int(status) < 300 and out.get("ok") else "error"
        _delayed_repair_set_status(did, final_status, f"content repair status={status}", out)
        _record_delayed_content_repair(parent_job_id, {"id": did, "status": final_status, **out})
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": str(e)[:240], "uses_direct_units": False}
        _delayed_repair_set_status(did, "error", out["error"], out)
        _record_delayed_content_repair(parent_job_id, {"id": did, "status": "error", **out})


def _delayed_repair_daemon_loop(app) -> None:
    import psycopg2.extras
    while True:
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
    body["via_cookie"] = True                              # докрутка ПО КУКЕ (без баллов) — не ждём полночь
    body["_deferred_id"] = did                             # финал джобы пометит остаток done (анти-цикл)
    sess = {"logged_in": True, "is_admin": True, "_resume": True}   # системная докрутка — авторизована заранее
    try:
        _ensure_create_worker(app)
        jid = _job_new(len(items), login, body, sess)
        body["_job_id"] = jid                              # как в api_create_set_async: воркер-путь + прогресс джобы
        _deferred_set_status(did, "resumed", f"докрутка по куке #{body['_resume_count']} поставлена в очередь")
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
    body["via_cookie"] = True                             # ЯВНОЕ согласие пользователя (попап) → token-типы по куке
    body["_deferred_id"] = did                            # финал джобы пометит остаток done (анти-цикл)
    sess = {"logged_in": True, "is_admin": True, "_resume": True}   # системная докрутка — авторизована
    _ensure_create_worker(app)
    jid = _job_new(len(items), login, body, sess)         # _job_new сам проставит body["_job_id"]
    _deferred_set_status(did, "resumed", "запущено вручную (куки/сейчас) — поставлено в очередь")
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


def _job_new(total: int, login: str, body: dict, saved_session: dict,
             dedup_login: bool = False) -> str:
    """Регистрирует джобу в статусе 'queued' и ставит её в глобальную очередь.

    dedup_login=True (пользовательский submit) — АТОМАРНЫЙ дедуп: если по этому логину уже есть
    НЕзавершённая джоба (queued/running), второй джоб НЕ создаём, а возвращаем существующий job_id.
    Проверка+вставка под ОДНИМ _CREATE_JOBS_LOCK → закрывает гонку двух сабмитов подряд (TOCTOU:
    раньше эндпоинт сканировал и ОТПУСКАЛ лок до _job_new, два запроса успевали вставить обе копии).
    Внутренние постановки (докрутка/resume/delete_drafts) идут с dedup_login=False (намеренные)."""
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
               "kind": ("delete_drafts" if (body or {}).get("_kind") == "delete_drafts"
                        else "copy_campaigns" if (body or {}).get("_kind") == "copy_campaigns"
                        else "slepok" if (body or {}).get("content_source") == "slepok_library" else "set"),
               "publish": bool((body or {}).get("launch")),
               "stream_content": _is_stream,   # stream=True → фаза generating перед creating
               "step": None,                   # текущая фаза: None/generating/creating (только при stream)
               "_id": jid, "body": body, "session": saved_session,
               "_heartbeat": time.time()}
        _CREATE_JOBS[jid] = job
        _CREATE_QUEUE.append(jid)
        # лёгкая чистка СТАРЫХ ЗАВЕРШЁННЫХ джоб (активные/очередь не трогаем), держим ~40
        terminal = [k for k, v in _CREATE_JOBS.items() if v["status"] in _JOB_TERMINAL]
        if len(terminal) > 40:
            for old in terminal[:-40]:
                _CREATE_JOBS.pop(old, None)
                _JOB_DB_LAST.pop(old, None)
        _CREATE_COND.notify()
    _job_db_save(jid, job)                                # серверная персистентность (видна с любого устройства)
    return jid


def _create_jobs_ahead(jid: str) -> int:
    """Сколько джоб впереди (выполняется + ждут раньше в очереди) — для «в очереди, перед вами N»."""
    running = sum(1 for v in _CREATE_JOBS.values() if v["status"] == "running")
    try:
        idx = _CREATE_QUEUE.index(jid)
    except ValueError:
        return 0
    return running + idx


def _claim_next_job():
    """Берёт из очереди следующую джобу, если по агентству ещё не достигнут лимит параллельности.
    Ждёт, если очередь пуста ИЛИ все доступные джобы упёрлись в лимит агентства.
    Возвращает (jid, job, body, saved) и увеличивает счётчик агентства. Снятые отмены — пропускает."""
    with _CREATE_COND:
        while True:
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
                    continue                              # лимит по агентству исчерпан — ждёт
                # подходит: по агентству есть свободный слот
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
        jid, job, body, saved = _claim_next_job()
        agency = _job_agency(job)
        final_status = "error"
        try:
            _job_touch(job)
            _job_db_save(jid, job)                        # → 'running' в БД
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
                        resp = api_create_set()
                        obj = resp[0] if isinstance(resp, tuple) else resp
                        data = obj.get_json(silent=True) if hasattr(obj, "get_json") else None
                        if data is None:                      # редирект/HTML (нет прав) → честная ошибка
                            data = {"error": "фоновое создание не выполнено (нет JSON-ответа; проверьте права/сессию)"}
                    except Exception as e:  # noqa: BLE001
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
                if final_status == "done":
                    auto_queued = _auto_queue_recreate_after_done(jid, _job_final)
                    delayed_content = _schedule_delayed_content_repair_after_done(jid, _job_final)
                    post_done_changed = False
                    if auto_queued:
                        with _CREATE_JOBS_LOCK:
                            j = _CREATE_JOBS.get(jid)
                            if j is not None and isinstance(j.get("result"), dict):
                                j["result"]["auto_queued_repair"] = auto_queued
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
                    if post_done_changed and _job_final is not None:
                        _job_db_save(jid, _job_final, full=True)
        finally:
            # освобождаем слот агентства и будим пул
            with _CREATE_COND:
                active = max(0, int(_CREATE_ACTIVE_AGENCIES.get(agency, 0)) - 1)
                if active:
                    _CREATE_ACTIVE_AGENCIES[agency] = active
                else:
                    _CREATE_ACTIVE_AGENCIES.pop(agency, None)
                _CREATE_COND.notify_all()
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
    инициализирует таблицу персистентности и поднимает недавние джобы из БД (для просмотра)."""
    with _CREATE_JOBS_LOCK:
        if _CREATE_WORKER["started"]:
            return
        _CREATE_WORKER["started"] = True
    _jobs_db_init()
    _jobs_db_recover()
    _ensure_create_watchdog()
    _create_watchdog_tick()
    workers = int(_CREATE_WORKERS or _create_workers_count())
    for _ in range(workers):                              # параллельно по разным агентствам
        threading.Thread(target=_create_worker_loop, args=(app,), daemon=True).start()
    _ensure_resume_daemon(app)                            # демон авто-докрутки остатка после сброса баллов
    _ensure_delayed_repair_daemon(app)                    # guarded content repair после Grid lag


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
        slepki_structure=_json("slepki_structure.json"),
        model_cts=_model_cts(),                 # модельные ct (совместимость)
        ct_segments=_ct_segment_map(),          # ct → 'Модели'|'Марки'|'Общее' (единый источник для UI и плана)
        donor_tp4_models=_donor_tp4_models_map(),  # {slepok: [site_type]} — tp4 «Модели» от донора
        default_name=cmc.DEFAULT_DISPLAY_NAME,
    )


@bp.route("/")
@_direct_access
def index():
    from flask import redirect
    return redirect("/direct/automation")


@bp.route("/automation")
@_direct_access
def automation():
    """Канонический маршрут страницы «Автоматизация Директа»."""
    return _render_page()


@bp.route("/minusphrase")
@_direct_minusphrase_access
def minusphrase():
    """Инструмент подбора минус-фраз для ключевых слов."""
    return render_template(
        "direct/minusphrase.html",
        active_section="work",
        active_page="direct_minusphrase",
    )


# ── API ───────────────────────────────────────────────────────────────────────

@bp.route("/api/feeds")
@_direct_access
def api_feeds():
    site = (request.args.get("site") or "").strip()
    if not site:
        return jsonify({"error": "site обязателен"}), 400
    return jsonify(cmc.list_feeds_for_site(site, _json("feeds_catalog.json")))


@bp.route("/api/audiences")
@_direct_access
def api_audiences():
    return jsonify(_load_audiences())


@bp.route("/api/units")
@_direct_access
def api_units():
    """Остаток баллов Директа для агентства аккаунта (заголовок Units). → {rest, limit, spent,
    agency, est_campaigns}. est_campaigns — грубая прикидка «на сколько кампаний хватит»."""
    login = (request.args.get("login") or "").strip()
    if not login:
        return jsonify({"error": "login обязателен"}), 400
    tok, ag = _token_for_login(login, (request.args.get("agency") or "").strip(), _direct_tokens())
    if not tok:
        return jsonify({"error": "не найден агентский токен, открывающий этот аккаунт"}), 404
    u = _v5_units(tok, login)
    if not u:
        return jsonify({"error": "не удалось прочитать остаток баллов (Units)"}), 502
    u["agency"] = ag
    u["est_campaigns"] = u["rest"] // _UNITS_PER_CAMPAIGN
    u["per_campaign"] = _UNITS_PER_CAMPAIGN
    return jsonify(u)


@bp.route("/api/deferred")
@_direct_access
def api_deferred():
    """Список ожидающих авто-докруток (остатки наборов после исчерпания баллов). ?login=… фильтрует."""
    _ensure_resume_daemon(current_app._get_current_object())
    login = (request.args.get("login") or "").strip()
    out = []
    try:
        import psycopg2.extras
        conn = _victory_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            if login:
                cur.execute("SELECT id, login, agency, n_items, status, resume_count, resume_at, note "
                            "FROM public.direct_deferred_creates WHERE status='waiting' AND login=%s "
                            "ORDER BY resume_at LIMIT 50", (login,))
            else:
                cur.execute("SELECT id, login, agency, n_items, status, resume_count, resume_at, note "
                            "FROM public.direct_deferred_creates WHERE status='waiting' "
                            "ORDER BY resume_at LIMIT 50")
            for r in cur.fetchall():
                out.append({"id": r["id"], "login": r["login"], "agency": r["agency"],
                            "n_items": r["n_items"], "resume_count": r["resume_count"],
                            "resume_at": r["resume_at"].isoformat() if r["resume_at"] else None,
                            "note": r["note"]})
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass
    return jsonify({"deferred": out})


@bp.route("/api/deferred_cancel", methods=["POST"])
@_direct_access
def api_deferred_cancel():
    """Отменить ожидающую авто-докрутку (пользователь выбрал «не докручивать» / создаст иначе)."""
    did = ((request.json or {}).get("id") or "").strip()
    if not did:
        return jsonify({"error": "id обязателен"}), 400
    _deferred_set_status(did, "cancelled", "отменено пользователем")
    return jsonify({"ok": True})


@bp.route("/api/deferred_resume_now", methods=["POST"])
@_direct_access
def api_deferred_resume_now():
    """Создать остаток отложенного набора СЕЙЧАС (кнопка «создать через куки») — ставит его
    в ОБЩУЮ очередь немедленно, без ожидания сброса баллов. → {job_id, total, login, agency}."""
    did = ((request.json or {}).get("id") or "").strip()
    if not did:
        return jsonify({"error": "id обязателен"}), 400
    app = current_app._get_current_object()
    res = _deferred_enqueue_now(app, did)
    if not res:
        return jsonify({"error": "не удалось поставить остаток в очередь (запись не найдена/пуста)"}), 404
    jid, total, login, agency = res
    with _CREATE_JOBS_LOCK:
        ahead = _create_jobs_ahead(jid)
    return jsonify({"job_id": jid, "total": total, "login": login, "agency": agency, "ahead": ahead})


# ── Шаблонные тексты объявлений (Victory: public.direct_ad_templates) ───────────

@bp.route("/api/ad_template_sites")
@_direct_access
def api_ad_template_sites():
    """Типы сайтов с числом шаблонов — для выпадающего списка в форме."""
    conn = _victory_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT site_type, count(*) FROM public.direct_ad_templates "
                    "WHERE enabled GROUP BY site_type ORDER BY site_type")
        return jsonify({"sites": [{"site_type": r[0], "n": r[1]} for r in cur.fetchall()]})
    finally:
        conn.close()


@bp.route("/api/ad_templates")
@_direct_access
def api_ad_templates():
    """Заголовки/тексты по типу сайта: {site_type, titles:[...], texts:[...]}."""
    st = (request.args.get("site_type") or "").strip()
    if not st:
        return jsonify({"error": "site_type обязателен"}), 400
    conn = _victory_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT kind, content FROM public.direct_ad_templates "
                    "WHERE enabled AND site_type=%s ORDER BY kind, id", (st,))
        titles, texts = [], []
        for kind, content in cur.fetchall():
            (titles if kind == "title" else texts).append(content)
        return jsonify({"site_type": st, "titles": titles, "texts": texts})
    finally:
        conn.close()


# ── Правила РК (Victory: public.direct_automation_rules) ────────────────────────

@bp.route("/api/cities")
@_direct_access
def api_cities():
    """Список городов direction='Авто' из local_gsheet_sites (для гео-дропдауна правил).
    → {"cities": ["Екатеринбург", "Казань", ...]}"""
    conn = _victory_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT city FROM public.local_gsheet_sites "
                    "WHERE direction='Авто' AND city IS NOT NULL AND city<>'' ORDER BY city")
        return jsonify({"cities": [row[0] for row in cur.fetchall()]})
    finally:
        conn.close()


@bp.route("/api/rules")
@_direct_access
def api_rules_get():
    """Правила РК из public.direct_automation_rules с гео-слиянием.
    ?city=<city> (по умолчанию '*' — только дефолтные строки).
    Логика: берём дефолтные строки city='*' (все 5 типов) и строки выбранного города;
    для каждого site_type возвращаем значения города (is_override=true) если есть,
    иначе дефолт city='*' (is_override=false).
    → {"city":<city>, "rules":[{site_type, goal_type, cpa, budget, adjustment_pct, is_override}]}"""
    import psycopg2.extras
    city = (request.args.get("city") or "*").strip() or "*"
    # Колонки: CPA-набор (goal_type/cpa/budget) + CPC-набор (cpc_*). adjustment_pct — общий.
    cols = ("site_type, goal_type, cpa::numeric AS cpa, budget::numeric AS budget, adjustment_pct, "
            "cpc_goal_type, cpc_cpa::numeric AS cpc_cpa, cpc_budget::numeric AS cpc_budget")

    def _rule(r, is_override):
        return {"site_type": r["site_type"], "goal_type": r["goal_type"],
                "cpa": float(r["cpa"]), "budget": float(r["budget"]),
                "cpc_goal_type": r.get("cpc_goal_type") or r["goal_type"],
                "cpc_cpa": float(r["cpc_cpa"]) if r.get("cpc_cpa") is not None else float(r["cpa"]),
                "cpc_budget": float(r["cpc_budget"]) if r.get("cpc_budget") is not None else float(r["budget"]),
                "adjustment_pct": r["adjustment_pct"], "is_override": is_override}

    conn = _victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if city == "*":
            cur.execute("SELECT " + cols + " FROM public.direct_automation_rules WHERE city='*' ORDER BY site_type")
            rules = [_rule(r, False) for r in cur.fetchall()]
        else:
            cur.execute("SELECT " + cols + " FROM public.direct_automation_rules WHERE city='*' ORDER BY site_type")
            defaults = {r["site_type"]: dict(r) for r in cur.fetchall()}
            cur.execute("SELECT " + cols + " FROM public.direct_automation_rules WHERE city=%s", (city,))
            overrides = {r["site_type"]: dict(r) for r in cur.fetchall()}
            rules = [_rule(overrides[st], True) if st in overrides else _rule(d, False)
                     for st, d in sorted(defaults.items())]
    finally:
        conn.close()
    return jsonify({"city": city, "rules": rules})


@bp.route("/api/corrections")
@_direct_access
def api_corrections_get():
    """Корректировки аудиторий и демографические по гео.
    ?city=<city> (по умолчанию '*' — дефолтные строки).
    Логика мержа: описания/label берём из '*'; pct — города если есть, иначе дефолт '*'.
    → {"city":<city>, "audiences":[{name,description,pct,is_override}],
       "demographic":[{kind,key,label,pct,is_override}]}"""
    import psycopg2.extras
    city = (request.args.get("city") or "*").strip() or "*"
    conn = _victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # ── Аудиторные: дефолты city='*' ──
        cur.execute(
            "SELECT name, description, pct, sort FROM public.direct_audience_corrections "
            "WHERE city='*' ORDER BY sort, name"
        )
        aud_defaults = {r["name"]: dict(r) for r in cur.fetchall()}
        # ── Аудиторные: переопределения города ──
        aud_overrides: dict = {}
        if city != "*":
            cur.execute(
                "SELECT name, pct FROM public.direct_audience_corrections "
                "WHERE city=%s ORDER BY name", (city,)
            )
            aud_overrides = {r["name"]: r["pct"] for r in cur.fetchall()}
        audiences = []
        for name, d in sorted(aud_defaults.items(), key=lambda x: (x[1]["sort"] or 0, x[0])):
            if city != "*" and name in aud_overrides:
                audiences.append({"name": name, "description": d["description"],
                                  "pct": aud_overrides[name], "is_override": True})
            else:
                audiences.append({"name": name, "description": d["description"],
                                  "pct": d["pct"], "is_override": False})

        # ── Демографические: дефолты city='*' ──
        cur.execute(
            "SELECT kind, key, label, pct, sort FROM public.direct_demographic_corrections "
            "WHERE city='*' ORDER BY kind, sort, key"
        )
        dem_defaults = {(r["kind"], r["key"]): dict(r) for r in cur.fetchall()}
        # ── Демографические: переопределения города ──
        dem_overrides: dict = {}
        if city != "*":
            cur.execute(
                "SELECT kind, key, pct FROM public.direct_demographic_corrections "
                "WHERE city=%s", (city,)
            )
            dem_overrides = {(r["kind"], r["key"]): r["pct"] for r in cur.fetchall()}
        demographic = []
        for (kind, key), d in sorted(dem_defaults.items(), key=lambda x: (x[1]["kind"], x[1]["sort"] or 0, x[0][1])):
            ovr_key = (kind, key)
            if city != "*" and ovr_key in dem_overrides:
                demographic.append({"kind": kind, "key": key, "label": d["label"],
                                    "pct": dem_overrides[ovr_key], "is_override": True})
            else:
                demographic.append({"kind": kind, "key": key, "label": d["label"],
                                    "pct": d["pct"], "is_override": False})
    finally:
        conn.close()
    return jsonify({"city": city, "audiences": audiences, "demographic": demographic})


@bp.route("/api/corrections", methods=["POST"])
@_direct_access
def api_corrections_post():
    """Сохранить корректировки.
    Тело: {"city":<city>, "audiences":[{name,pct}], "demographic":[{kind,key,pct}]}.
    UPSERT по (city,name) и (city,kind,key). description/label при city≠'*' НЕ трогаем.
    → {"ok":true, "saved":<n>}"""
    body = request.json or {}
    city = (body.get("city") or "*").strip() or "*"
    audiences = body.get("audiences") or []
    demographic = body.get("demographic") or []
    if not audiences and not demographic:
        return jsonify({"ok": False, "error": "audiences или demographic обязательны"}), 400
    conn = _victory_conn_rw()
    saved = 0
    try:
        cur = conn.cursor()
        for aud in audiences:
            name = (aud.get("name") or "").strip()
            if not name:
                continue
            try:
                pct = int(aud.get("pct") if aud.get("pct") is not None else -100)
            except (TypeError, ValueError):
                continue
            cur.execute(
                "INSERT INTO public.direct_audience_corrections(city, name, pct, updated_at) "
                "VALUES(%s, %s, %s, now()) "
                "ON CONFLICT(city, name) DO UPDATE SET pct=EXCLUDED.pct, updated_at=now()",
                (city, name, pct)
            )
            saved += cur.rowcount
        for dem in demographic:
            kind = (dem.get("kind") or "").strip()
            key = (dem.get("key") or "").strip()
            if not kind or not key:
                continue
            try:
                pct = int(dem.get("pct") if dem.get("pct") is not None else -100)
            except (TypeError, ValueError):
                continue
            cur.execute(
                "INSERT INTO public.direct_demographic_corrections(city, kind, key, pct, updated_at) "
                "VALUES(%s, %s, %s, %s, now()) "
                "ON CONFLICT(city, kind, key) DO UPDATE SET pct=EXCLUDED.pct, updated_at=now()",
                (city, kind, key, pct)
            )
            saved += cur.rowcount
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
        raise
    finally:
        conn.close()
    return jsonify({"ok": True, "saved": saved})


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
    """feed_key всех enabled-фидов с role='catalog' в Глобальных правилах (лендинги role='landing'
    исключены). Нужно для tp1-товарки: множить фид-фан-аут ТОЛЬКО по каталог-фидам с реальными
    модельными листингами — лендинг-фиды дают пустой model-ListingAd и валили создание кампании."""
    try:
        rows = _global_feed_rules()
        return {_feed_key(r.get("url") or r.get("name") or r.get("feed_key") or "")
                for r in rows if r.get("enabled") and (r.get("role") or "landing") == "catalog"}
    except Exception:  # noqa: BLE001
        # Victory недоступна: не валим создание — берём ТОЧНЫЙ список каталог-фидов (тот же, что
        # backfill role='catalog' в _feed_rules_ensure). Матч по точному feed_key, не по подстроке.
        return {_feed_key(f) for f in _CATALOG_FEED_KEYS}


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


@bp.route("/api/feed-rules")
@_direct_access
def api_feed_rules_get():
    rows = _global_feed_rules()
    return jsonify({"feeds": rows})


@bp.route("/api/feed-rules", methods=["POST"])
@_direct_access
def api_feed_rules_post():
    body = request.json or {}
    feeds = body.get("feeds") or []
    if not isinstance(feeds, list):
        return jsonify({"ok": False, "error": "feeds должен быть массивом"}), 400
    conn = _victory_conn_rw()
    saved = 0
    try:
        cur = conn.cursor()
        _feed_rules_ensure(cur)
        for i, row in enumerate(feeds, 1):
            name = (row.get("name") or row.get("url") or "").strip()
            url = (row.get("url") or name).strip()
            if not name or not url:
                continue
            key = _feed_key(url or name)
            if not key:
                continue
            cur.execute(
                "INSERT INTO public.direct_global_feed_rules(feed_key, name, url, enabled, sort, updated_at) "
                "VALUES(%s, %s, %s, %s, %s, now()) "
                "ON CONFLICT(feed_key) DO UPDATE SET name=EXCLUDED.name, url=EXCLUDED.url, "
                "enabled=EXCLUDED.enabled, sort=EXCLUDED.sort, updated_at=now()",
                (key, name, url, bool(row.get("enabled")), i),
            )
            # role (каталог/лендинг) обновляем ТОЛЬКО если поле пришло в теле — иначе не трогаем
            # (старый UI без role-тоггла не должен сбрасывать роль каталога/лендинга).
            if "role" in row:
                # Безопасный дефолт: 'catalog' ТОЛЬКО при точном значении 'catalog'; всё прочее
                # (null/пусто/'landing'/мусор) → 'landing'. 'catalog' включает фан-аут tp1, поэтому
                # неоднозначное значение НЕ должно тянуть к нему (совпадает с DEFAULT колонки). (#2 review)
                role_val = "catalog" if str(row.get("role") or "").strip().lower() == "catalog" else "landing"
                cur.execute(
                    "UPDATE public.direct_global_feed_rules SET role=%s, updated_at=now() WHERE feed_key=%s",
                    (role_val, key),
                )
            saved += 1
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
        raise
    finally:
        conn.close()
    return jsonify({"ok": True, "saved": saved})


@bp.route("/api/minus-places")
@_direct_access
def api_minus_places_get():
    """Список минус-площадок РСЯ (#21). → {places:[{url,enabled,sort}]}."""
    return jsonify({"places": _global_minus_places()})


@bp.route("/api/minus-places", methods=["POST"])
@_direct_access
def api_minus_places_post():
    """Сохранить минус-площадки (replace-all из textarea). Тело: {places:[url|{url,enabled}]}.
    URL приводим к НИЖНЕМУ регистру (решение Семёна). Пустой список → очистка таблицы."""
    body = request.json or {}
    if "places" not in body:                                 # guard: отсутствие ключа = malformed POST,
        return jsonify({"ok": False, "error": "нет ключа 'places' — replace-all не выполняется"}), 400  # НЕ стираем таблицу
    raw = body.get("places")
    if not isinstance(raw, list):
        return jsonify({"ok": False, "error": "places должен быть массивом"}), 400
    # Доп. защита от случайной полной очистки: пустой список стирает таблицу ТОЛЬКО с явным confirm_clear.
    if not raw and not bool(body.get("confirm_clear")):
        try:
            _cur_cnt = len(_global_minus_places())
        except Exception:  # noqa: BLE001
            _cur_cnt = 0
        if _cur_cnt:
            return jsonify({"ok": False, "needs_confirm": True,
                            "error": f"список пуст — для полной очистки {_cur_cnt} площадок передай confirm_clear=true"}), 409
    # нормализация: нижний регистр, дедуп, сохранить порядок
    seen: set[str] = set()
    items: list[tuple[str, bool]] = []
    for r in raw:
        url = (r.get("url") if isinstance(r, dict) else r) or ""
        url = _place_host(url)                               # #2: голый хост (Яндекс ждёт домен, не URL)
        if not url or url in seen:
            continue
        seen.add(url)
        en = bool(r.get("enabled", True)) if isinstance(r, dict) else True
        items.append((url, en))
    conn = _victory_conn_rw()
    try:
        cur = conn.cursor()
        _minus_places_ensure(cur)
        cur.execute("DELETE FROM public.direct_global_minus_places")   # replace-all (textarea = источник)
        for i, (url, en) in enumerate(items, 1):
            cur.execute(
                "INSERT INTO public.direct_global_minus_places(url, enabled, sort, updated_at) "
                "VALUES(%s, %s, %s, now()) ON CONFLICT(url) DO UPDATE SET enabled=EXCLUDED.enabled, "
                "sort=EXCLUDED.sort, updated_at=now()",
                (url, en, i),
            )
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
        raise
    finally:
        conn.close()
    return jsonify({"ok": True, "saved": len(items)})


@bp.route("/api/content-tree")
@_direct_access
def api_content_tree():
    try:
        force = (request.args.get("refresh") or "").strip() in {"1", "true", "yes"}
        return jsonify({"ok": True, "tree": kp.content_tree(force_refresh=force), "status": _m3_content_status()})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)[:300]}), 500


@bp.route("/api/content-assets")
@_direct_access
def api_content_assets():
    segment = (request.args.get("segment") or "").strip()
    tp = (request.args.get("tp") or "").strip()
    ct = (request.args.get("ct") or "").strip()
    slepok = (request.args.get("slepok") or "").strip().lower()
    try:
        limit = int(request.args.get("limit") or 24)
    except (TypeError, ValueError):
        limit = 24
    try:
        offset = int(request.args.get("offset") or 0)
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, 120))
    offset = max(0, offset)
    if not segment or not tp or not ct:
        return jsonify({"ok": False, "error": "segment, tp, ct обязательны"}), 400
    assets = kp.content_assets(segment, tp, ct, slepok=slepok)
    try:
        rules = _content_rules_map()
    except Exception:
        rules = {}
    for a in assets:
        original_key = a.get("asset_key") or ""
        source_slepok = (a.get("slepok") or "").strip().lower()
        scoped_key = _content_rule_key(segment, tp, ct, original_key, source_slepok)
        a["original_asset_key"] = original_key
        a["asset_key"] = scoped_key
        a["source_slepok"] = source_slepok
        r = rules.get(scoped_key)
        if r:
            a["enabled"] = bool(r.get("enabled"))
            a["allowed_for"] = r.get("allowed_for") or []
            a["allowed_slepki"] = r.get("allowed_slepki") or []
        else:
            a["enabled"] = True
            a["allowed_for"] = []
            a["allowed_slepki"] = []
    raw_total = len(assets)
    # Не дедуплицируем всю папку до пагинации: для больших ct это тысячи pHash-запросов
    # к M3 и секундные/минутные зависания. Берём ограниченное окно, схлопываем повторы
    # только внутри него и отдаём страницу. Следующая порция обработается при скролле.
    scan_limit = min(raw_total, offset + max(limit * 3, limit))
    window = assets[offset:scan_limit]
    page_assets, hidden_duplicates = _dedupe_content_assets_for_ui(window)
    page_assets = page_assets[:limit]
    next_offset = scan_limit
    thumb_paths = [
        a.get("remote") for a in page_assets
        if "image" in str(a.get("asset_type") or "") and a.get("remote")
    ]
    if thumb_paths:
        if len(thumb_paths) <= 8:
            kp.prefetch_remote_thumbnails(thumb_paths, size=360, max_workers=4)
        else:
            threading.Thread(
                target=kp.prefetch_remote_thumbnails,
                args=(thumb_paths,),
                kwargs={"size": 360, "max_workers": 4},
                daemon=True,
            ).start()
    return jsonify({
        "ok": True, "segment": segment, "tp": tp, "ct": ct,
        "slepok": slepok,
        "assets": page_assets,
        "total_assets": raw_total,
        "raw_total_assets": raw_total,
        "hidden_duplicates": hidden_duplicates,
        "limit": limit,
        "offset": offset,
        "next_offset": next_offset,
        "has_more": next_offset < raw_total,
    })


@bp.route("/api/content-preview/<token>")
@_direct_access
def api_content_preview(token):
    remote = kp.decode_remote_asset(token)
    if not remote:
        return ("bad token", 400)
    # Превью только для разрешённых контент-корней на M3.
    roots = [
        getattr(kp, "M3_AGENCY_ROOT", kp.M3_PACK_ROOT),
        getattr(kp, "M3_MANUAL_ROOT", ""),
    ]
    if not any(r and remote.startswith(r + "/") for r in roots):
        return ("forbidden", 403)
    local = kp.fetch_remote_asset(remote)
    if not local or not os.path.isfile(local):
        return ("not found", 404)
    return send_file(local, conditional=True, max_age=86400)


@bp.route("/api/content-thumb/<token>")
@_direct_access
def api_content_thumb(token):
    remote = kp.decode_remote_asset(token)
    if not remote:
        return ("bad token", 400)
    roots = [
        getattr(kp, "M3_AGENCY_ROOT", kp.M3_PACK_ROOT),
        getattr(kp, "M3_MANUAL_ROOT", ""),
    ]
    if not any(r and remote.startswith(r + "/") for r in roots):
        return ("forbidden", 403)
    local = kp.fetch_remote_thumbnail(remote, size=360)
    if not local or not os.path.isfile(local):
        local = kp.fetch_remote_asset(remote)
    if not local or not os.path.isfile(local):
        return ("not found", 404)
    return send_file(local, conditional=True, max_age=86400)


@bp.route("/api/content-rules", methods=["POST"])
@_direct_access
def api_content_rules_post():
    body = request.json or {}
    assets = body.get("assets") or []
    if not isinstance(assets, list):
        return jsonify({"ok": False, "error": "assets должен быть массивом"}), 400
    conn = _victory_conn_rw()
    saved = 0
    try:
        cur = conn.cursor()
        _content_rules_ensure(cur)
        for a in assets:
            src_segment = (a.get("segment") or a.get("source_segment") or "").strip()
            src_tp = (a.get("tp") or a.get("source_tp") or "").strip()
            src_ct = (_gc_ct(a.get("ct") or a.get("source_ct") or "") or "ct0000")
            src_slepok = (a.get("slepok") or a.get("source_slepok") or "").strip().lower()
            original_key = (a.get("original_asset_key") or "").strip()
            key = (a.get("asset_key") or "").strip()
            if original_key:
                key = _content_rule_key(src_segment, src_tp, src_ct, original_key, src_slepok)
            remote = (a.get("remote") or a.get("asset_path") or "").strip()
            if not key or not remote:
                continue
            allowed = a.get("allowed_for") or []
            if isinstance(allowed, str):
                allowed = [x.strip().lower() for x in re.split(r"[,;\s]+", allowed) if x.strip()]
            else:
                allowed = [str(x).strip().lower() for x in allowed if str(x).strip()]
            allowed_slepki = a.get("allowed_slepki") or []
            if isinstance(allowed_slepki, str):
                allowed_slepki = [x.strip().lower() for x in re.split(r"[,;\s]+", allowed_slepki) if x.strip()]
            else:
                allowed_slepki = [str(x).strip().lower() for x in allowed_slepki if str(x).strip()]
            cur.execute(
                "INSERT INTO public.direct_content_asset_rules("
                "asset_key, asset_type, source_segment, source_tp, source_ct, asset_path, "
                "name, enabled, allowed_for, source_slepok, allowed_slepki, updated_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,now()) "
                "ON CONFLICT(asset_key) DO UPDATE SET asset_type=EXCLUDED.asset_type, "
                "source_segment=EXCLUDED.source_segment, source_tp=EXCLUDED.source_tp, "
                "source_ct=EXCLUDED.source_ct, asset_path=EXCLUDED.asset_path, name=EXCLUDED.name, "
                "enabled=EXCLUDED.enabled, allowed_for=EXCLUDED.allowed_for, "
                "source_slepok=EXCLUDED.source_slepok, allowed_slepki=EXCLUDED.allowed_slepki, updated_at=now()",
                (
                    key,
                    (a.get("asset_type") or "image").strip(),
                    src_segment,
                    src_tp,
                    src_ct,
                    remote,
                    (a.get("name") or os.path.basename(remote)).strip(),
                    bool(a.get("enabled")),
                    json.dumps(allowed, ensure_ascii=False),
                    src_slepok,
                    json.dumps(allowed_slepki, ensure_ascii=False),
                ),
            )
            saved += 1
        conn.commit()
        _CONTENT_RULES_CACHE.update({"ts": 0.0, "rows": {}})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return jsonify({"ok": True, "saved": saved})


@bp.route("/api/rules", methods=["POST"])
@_direct_access
def api_rules_post():
    """Сохранить правила РК. Тело: {"city":<city>, "rules":[{site_type, goal_type, cpa, budget, adjustment_pct}, ...]}.
    UPSERT по (site_type, city). city='*' → правит дефолты.
    → {"ok": true, "saved": <n>}"""
    body = request.json or {}
    city = (body.get("city") or "*").strip() or "*"
    rules = body.get("rules") or []
    if not rules:
        return jsonify({"ok": False, "error": "rules обязательны"}), 400
    conn = _victory_conn_rw()
    saved = 0
    try:
        cur = conn.cursor()
        for rule in rules:
            st = (rule.get("site_type") or "").strip()
            if not st:
                continue
            goal_type = (rule.get("goal_type") or "Все формы").strip()
            cpc_goal_type = (rule.get("cpc_goal_type") or goal_type).strip()
            try:
                cpa = float(rule.get("cpa") or 0)
                budget = float(rule.get("budget") or 0)
                adjustment_pct = int(rule.get("adjustment_pct") or -100)
                cpc_cpa = float(rule.get("cpc_cpa") if rule.get("cpc_cpa") is not None else cpa)
                cpc_budget = float(rule.get("cpc_budget") if rule.get("cpc_budget") is not None else budget)
            except (TypeError, ValueError):
                continue
            cur.execute(
                "INSERT INTO public.direct_automation_rules "
                "(site_type, city, goal_type, cpa, budget, adjustment_pct, "
                "cpc_goal_type, cpc_cpa, cpc_budget, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
                "ON CONFLICT (site_type, city) DO UPDATE "
                "SET goal_type=EXCLUDED.goal_type, cpa=EXCLUDED.cpa, "
                "budget=EXCLUDED.budget, adjustment_pct=EXCLUDED.adjustment_pct, "
                "cpc_goal_type=EXCLUDED.cpc_goal_type, cpc_cpa=EXCLUDED.cpc_cpa, "
                "cpc_budget=EXCLUDED.cpc_budget, updated_at=now()",
                (st, city, goal_type, cpa, budget, adjustment_pct, cpc_goal_type, cpc_cpa, cpc_budget)
            )
            saved += cur.rowcount
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
        raise
    finally:
        conn.close()
    return jsonify({"ok": True, "saved": saved})


@bp.route("/api/overview")
@_direct_access
def api_overview():
    """Обзор по директологу из общей таблицы public.gsheet_sites.
    Без параметра → список директологов с числом сайтов.
    ?directologist=<name> → строки этого директолога (domain/salon/city/site_type/login_key/client_id/crm/template).
    → {"directologist", "directologists":[{name,n}], "rows":[...]}"""
    import psycopg2.extras
    dirq = (request.args.get("directologist") or "").strip()
    statusq = [s.strip() for s in (request.args.get("status") or "").split(",") if s.strip()]
    conn = _victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Счётчик сайтов директолога — с учётом фильтра статуса (count FILTER, чтобы директолог
        # с 0 по выбранному статусу не пропадал из списка).
        if statusq:
            cur.execute("SELECT directologist, count(*) FILTER (WHERE status = ANY(%s)) AS n "
                        "FROM public.gsheet_sites WHERE directologist IS NOT NULL AND directologist <> '' "
                        "GROUP BY directologist ORDER BY n DESC, directologist", (statusq,))
        else:
            cur.execute("SELECT directologist, count(*) AS n FROM public.gsheet_sites "
                        "WHERE directologist IS NOT NULL AND directologist <> '' "
                        "GROUP BY directologist ORDER BY n DESC, directologist")
        directologists = [{"name": r["directologist"], "n": r["n"]} for r in cur.fetchall()]
        rows = []
        if dirq:
            # Открут_Факт = sum(total_cost) из yandex_direct_manager_reports по аккаунту за ТЕКУЩИЙ месяц.
            cur.execute(
                "SELECT g.domain, g.salon, g.city, g.site_type, g.login_key, g.crm, g.template, g.status, "
                "       COALESCE(r.fact, 0)::double precision AS otkrut_fact "
                "FROM public.gsheet_sites g "
                "LEFT JOIN (SELECT account_login, sum(total_cost) AS fact "
                "           FROM public.yandex_direct_manager_reports "
                "           WHERE left(\"Date\", 7) = to_char(now(), 'YYYY-MM') "
                "           GROUP BY account_login) r ON r.account_login = g.login_key "
                "WHERE g.directologist = %s ORDER BY g.domain NULLS LAST", (dirq,))
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return jsonify({"directologist": dirq, "directologists": directologists, "rows": rows})


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


@bp.route("/api/statuses")
@_direct_access
def api_statuses():
    """Список статусов direction='Авто' для фильтра (с количеством)."""
    import psycopg2.extras
    conn = _victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT status, count(*) AS n FROM public.local_gsheet_sites "
                    "WHERE direction='Авто' AND status IS NOT NULL AND status<>'' "
                    "GROUP BY status ORDER BY n DESC")
        return jsonify({"default": DEFAULT_STATUS, "statuses": cur.fetchall()})
    finally:
        conn.close()


@bp.route("/api/accounts")
@_direct_access
def api_accounts():
    """Аккаунты из local_gsheet_sites (direction='Авто') с фильтром статуса и умным поиском."""
    import psycopg2.extras
    status = (request.args.get("status") or DEFAULT_STATUS).strip()
    q = (request.args.get("q") or "").strip()

    where = ["direction='Авто'", "login_key IS NOT NULL", "login_key<>''",
             "lower(btrim(login_key)) NOT IN ('нет', 'авито')",   # плейсхолдеры — не показываем
             "btrim(login_key) !~ '^-+$'",                        # логин из одних дефисов («----»)
             "(directologist IS NULL OR directologist <> ALL(%s))"]
    params: list = [_EXCLUDE_DIRECTOLOGS]
    if status and status != "__all__":
        where.append("status=%s")
        params.append(status)
    if q:
        where.append("(domain ILIKE %s OR salon ILIKE %s OR login_key ILIKE %s "
                     "OR directologist ILIKE %s OR city ILIKE %s OR site_type ILIKE %s)")
        params += [f"%{q}%"] * 6

    # LEFT JOIN metrika_goals: для аккаунтов без counter_number фронт покажет
    # счётчики из counter_ids (дропдаун, если их >1) и цель из all_forms.
    cols_sel = ", ".join("s." + c for c in _ACCOUNT_COLS)
    # Открут НЕ джойним здесь (тяжёлый запрос по 2М+ строкам отчётов) — грузится отдельно
    # по кнопке через /api/accounts_otkrut. Первичная загрузка списка остаётся быстрой.
    sql = (f"SELECT {cols_sel}, mg.counter_ids AS mg_counter_ids, mg.all_forms AS mg_all_forms "
           f"FROM public.local_gsheet_sites s "
           f"LEFT JOIN public.metrika_goals mg ON mg.account_login = s.login_key "
           f"WHERE {' AND '.join(where)} ORDER BY s.domain LIMIT 2000")
    conn = _victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        rows = cur.fetchall()
    finally:
        conn.close()
    for r in rows:
        r["mg_counters"] = _parse_counter_ids(r.pop("mg_counter_ids", None))
        r["mg_goal"] = r.pop("mg_all_forms", None)
    return jsonify({"rows": rows})


@bp.route("/api/accounts_otkrut")
@_direct_access
def api_accounts_otkrut():
    """Открут_Факт по аккаунтам за ТЕКУЩИЙ месяц (sum total_cost из yandex_direct_manager_reports).
    Грузится отдельно по кнопке «Обновить» (тяжёлый запрос). → {"fact": {login_key: sum}}"""
    conn = _victory_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT account_login, sum(total_cost)::double precision "
                    "FROM public.yandex_direct_manager_reports "
                    "WHERE left(\"Date\", 7) = to_char(now(), 'YYYY-MM') GROUP BY account_login")
        fact = {r[0]: r[1] for r in cur.fetchall() if r[0]}
    finally:
        conn.close()
    return jsonify({"fact": fact})


@bp.route("/api/account_info")
@_direct_access
def api_account_info():
    """Лёгкая инфа по логину из БД (без гео/Метрики) — для подсказки «тип сайта» при вводе."""
    import psycopg2.extras
    login = (request.args.get("login") or "").strip()
    if not login:
        return jsonify({})
    conn = _victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT domain, salon, city, site_type, counter_number, agency_account, directologist "
                    "FROM public.local_gsheet_sites WHERE login_key=%s AND direction='Авто' LIMIT 1", (login,))
        info = cur.fetchone() or {}
    finally:
        conn.close()
    # Счётчики из metrika_goals — чтобы подсказка показывала счётчик даже когда
    # counter_number в таблице сайтов пуст (источник для создания РК — metrika_goals).
    mg = _metrika_goals_for(login)
    info["mg_counters"] = mg["counters"] if mg else []
    # Регион аккаунта — для подстановки реального r_code в кодеры кампаний («Создание РК»)
    r_code, oblast = _resolve_region(info.get("city"))
    info["r_code"] = r_code
    info["oblast"] = oblast
    return jsonify(info)


@bp.route("/api/account_stats")
@_direct_access
def api_account_stats():
    """Статистика по аккаунту помесячно из public.yandex_direct_manager_reports.

    ?login=<account_login> → {login, months:[{month, total_cost, all_forms, cpl}]}.
    month — ISO-строка YYYY-MM-01; cpl — float или null (нет форм).
    """
    import psycopg2.extras
    login = (request.args.get("login") or "").strip()
    if not login:
        return jsonify({"error": "login обязателен"}), 400
    conn = _victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT date_trunc('month', "Date"::date)::date            AS month,
                   ROUND(SUM(COALESCE(total_cost,0))::numeric, 2)     AS total_cost,
                   COALESCE(SUM(all_forms),0)::bigint                 AS all_forms,
                   CASE WHEN SUM(all_forms) > 0
                        THEN ROUND(SUM(COALESCE(total_cost,0))::numeric / NULLIF(SUM(all_forms),0), 2)
                        ELSE NULL END                                  AS cpl
            FROM public.yandex_direct_manager_reports
            WHERE account_login = %s
            GROUP BY date_trunc('month', "Date"::date)
            ORDER BY month
            """,
            (login,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    months = [
        {
            "month": row["month"].isoformat(),
            "total_cost": float(row["total_cost"]),
            "all_forms": int(row["all_forms"]),
            "cpl": float(row["cpl"]) if row["cpl"] is not None else None,
        }
        for row in rows
    ]
    return jsonify({"login": login, "months": months})


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


@bp.route("/api/balance", methods=["POST"])
@_direct_access
def api_balance():
    """Баланс по логинам через ОФИЦИАЛЬНЫЙ API (Live v4 AccountManagement.Get), без кук.

    Тело: {"pairs": [{"login": "...", "agency": "victorylotsofads1"}, ...]}.
    Ответ: {"balances": {login: rub|null}}.
    """
    import requests as _rqs
    from concurrent.futures import ThreadPoolExecutor, as_completed

    ok, reason, wait = _pull_begin("balance", _COOLDOWN["balance"])
    if not ok:
        return _busy_response(reason, wait)
    try:
        return _do_balance(_rqs, ThreadPoolExecutor, as_completed)
    finally:
        _pull_end("balance")


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


def _v5_get(svc: str, token: str, login: str, fieldnames: list[str], criteria=None) -> dict:
    """Официальный OAuth API v5 GET одного сервиса. Возвращает распарсенный JSON."""
    import requests as _rqs
    h = {"Authorization": "Bearer " + token, "Client-Login": login,
         "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8"}
    params: dict = {"FieldNames": fieldnames}
    if criteria is not None:
        params["SelectionCriteria"] = criteria
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
         "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8"}
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
         "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8"}
    try:
        return _rqs.post(_V5 + svc, headers=h, json={"method": method, "params": params}, timeout=60).json()
    except Exception as e:  # noqa: BLE001
        return {"error": {"error_string": str(e)[:120]}}


def _v501_call(method: str, token: str, login: str, params: dict) -> dict:
    """Вызов v501 (campaigns.update и т.д.). Возвращает распарсенный JSON."""
    import requests as _rqs
    h = {"Authorization": "Bearer " + token, "Client-Login": login,
         "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8"}
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
         "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8"}
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


@bp.route("/api/account_assets")
@_direct_access
def api_account_assets():
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

    jf = _v5_get("feeds", token, login, ["Id", "Name", "BusinessType", "SourceType"])
    if "error" in jf:
        out["errors"]["feeds"] = jf["error"].get("error_string")
    else:
        out["feeds"] = [{"id": f["Id"], "name": f.get("Name"), "business_type": f.get("BusinessType"),
                         "source_type": f.get("SourceType")} for f in (jf.get("result") or {}).get("Feeds", [])]

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


@bp.route("/api/account_audiences")
@_direct_access
def api_account_audiences():
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
               "слепок_крючкова": "kryuchkova", "слепок_терехов": "terehov"}
_SLEPOK_CANONICAL = {"pavlov", "kryuchkova", "scherbakova", "terehov"}


def _slepok_key_from_text(raw: str) -> str:
    """Best-effort: имя слепка/директолога из БД/UI → canonical ai_agents key."""
    s = (raw or "").strip().lower()
    if not s:
        return ""
    if s in _SLEPOK_KEY:
        return _SLEPOK_KEY[s]
    if s in {"pavlov", "kryuchkova", "scherbakova", "terehov"}:
        return s
    if "павлов" in s:
        return "pavlov"
    if "крючков" in s:
        return "kryuchkova"
    if "щербаков" in s:
        return "scherbakova"
    if "терехов" in s:
        return "terehov"
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


@bp.route("/api/slepok_callouts")
@_direct_access
def api_slepok_callouts():
    """«Уточнения» (callouts) выбранного слепка из public.direct_slepok_callouts,
    отсортированные по частоте использования в реальных аккаунтах слепка.
    ?slepok=<key|Слепок_Имя>. Ответ: {callouts:[{text,usage,accounts,len}], лимиты}."""
    raw = (request.args.get("slepok") or "").strip()
    slepok = _SLEPOK_KEY.get(raw.lower(), raw.lower())
    out = {"callouts": [], "max_each": _CALLOUT_MAX_EACH,
           "max_total_desktop": _CALLOUT_MAX_TOTAL_DESKTOP, "max_total_mobile": _CALLOUT_MAX_TOTAL_MOBILE}
    if not slepok:
        return jsonify(out)
    try:
        conn = _victory_conn()
    except Exception as e:  # noqa: BLE001
        return jsonify({**out, "error": str(e)[:160]})
    try:
        cur = conn.cursor()
        cur.execute("SELECT text, usage_count, accounts_count, char_len "
                    "FROM public.direct_slepok_callouts WHERE slepok=%s "
                    "ORDER BY usage_count DESC, accounts_count DESC, text", (slepok,))
        out["callouts"] = [{"text": t, "usage": u, "accounts": a, "len": l}
                           for t, u, a, l in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:160]
    finally:
        conn.close()
    return jsonify(out)


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


@bp.route("/api/m3_content_status")
@_direct_access
def api_m3_content_status():
    """Подсказка в UI: читаем ли мы контент с M3 (статус sshfs-моста)."""
    return jsonify(_m3_content_status())


_M3_STATUS_CACHE: dict = {"at": 0.0, "data": None}
_M3_STATUS_TTL = 300                                      # кэш статуса M3 ~5 мин (polling 20 мин не дёргает чаще)


@bp.route("/api/m3-status")
@_direct_access
def api_m3_status():
    """Лёгкий health M3 для ПОСТОЯННОГО индикатора в сайдбаре. {ok, detail, checked_at}. Кэш ~5 мин.
    Единый источник правды (тот же `_m3_content_status`, что питает зелёный баннер «Контент с M3»)."""
    now = time.time()
    cached = _M3_STATUS_CACHE.get("data")
    if cached and (now - float(_M3_STATUS_CACHE.get("at") or 0)) < _M3_STATUS_TTL:
        out = dict(cached)
        out["cached"] = True
        return jsonify(out)
    st = _m3_content_status(timeout=6.0)
    from datetime import datetime
    out = {"ok": bool(st.get("ok")), "detail": (st.get("detail") or ""),
           "checked_at": datetime.now().strftime("%H:%M"), "cached": False}
    _M3_STATUS_CACHE["at"] = now
    _M3_STATUS_CACHE["data"] = {k: out[k] for k in ("ok", "detail", "checked_at")}
    return jsonify(out)


# ── Резолвер контента группы из пака M3 (по нашему ct) ──────────────────────────
def _gc_ct(gc: str) -> str:
    """Первый ctNNNN из кодера группы (gc) = ag_part1 = бренд/модель."""
    m = re.search(r"ct\d{4}", gc or "")
    return m.group(0) if m else ""


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
_BRAND_CANON_SET: set | None = None


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


@bp.route("/api/pack_preview")
@_direct_access
def api_pack_preview():
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


@bp.route("/api/account_prefill")
@_direct_access
def api_account_prefill():
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

    region_id, region_used = _geo_id(row.get("city"), row.get("region"))
    if not region_id:
        region_id, region_used = 225, "Россия"
        warnings.append("регион не распознан — поставил Россия (225)")

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


@bp.route("/api/goal_for_counter")
@_direct_access
def api_goal_for_counter():
    """Цель «Все формы» по номеру счётчика (Метрика). Для автоподстановки при ручном вводе счётчика."""
    counter = _num(request.args.get("counter"), 0)
    if not counter:
        return jsonify({"error": "counter обязателен"})
    gid, gname = _goal_vse_formy(counter)
    if not gid:
        return jsonify({"error": "цель «Все формы» не найдена в счётчике (или нет доступа)"})
    return jsonify({"goal_id": gid, "goal_name": gname})


@bp.route("/api/campaigns")
@_direct_access
def api_campaigns():
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
            out["error"] = v5_err
    return jsonify(out)


@bp.route("/api/copy_campaigns")
@_direct_access
def api_copy_campaigns():
    """Список кампаний источника для вкладки «Копирование кампаний»."""
    return api_campaigns()


@bp.route("/api/copy_target_prefill")
@_direct_access
def api_copy_target_prefill():
    """Префилл целевого аккаунта для копирования: домен/гео/счётчик/цель."""
    login = (request.args.get("login") or "").strip()
    if not login:
        return jsonify({"error": "login обязателен"}), 400
    try:
        base = api_account_prefill()
        payload = base.get_json(silent=True) if hasattr(base, "get_json") else None
        if payload and not payload.get("error"):
            payload["found"] = True
            return jsonify(payload)
    except Exception:  # noqa: BLE001
        pass
    mg = _metrika_goals_for(login)
    return jsonify({
        "found": False,
        "login": login,
        "domain": "",
        "href": "",
        "site_type": "",
        "city": "",
        "region": "",
        "region_id": None,
        "region_used": "",
        "counter_id": (mg["counters"][0] if mg and mg["counters"] else None),
        "counter_options": (mg["counters"] if mg else []),
        "goal_id": (mg["goal_id"] if mg else None),
        "goal_name": ("Все формы" if mg and mg["goal_id"] else None),
        "warnings": ["аккаунт не найден в local_gsheet_sites — заполни домен/гео вручную"],
    })


@bp.route("/api/copy_start", methods=["POST"])
@_direct_access
def api_copy_start():
    """Запустить выборочное копирование кампаний источник → цель."""
    body = request.json or {}
    source_login = (body.get("source_login") or "").strip()
    target_login = (body.get("target_login") or "").strip()
    selected_ids = [int(x) for x in (body.get("campaign_ids") or []) if str(x).isdigit()]
    counter_id = _num(body.get("counter_id"), 0)
    goal_id = _num(body.get("goal_id"), 0)
    target_domain = (body.get("target_domain") or "").strip()
    target_city = (body.get("target_city") or "").strip()
    target_region = (body.get("target_region") or "").strip()
    target_feed_url = (body.get("target_feed_url") or _COPY_DEFAULT_FEED_PATH).strip()
    if not source_login or not target_login:
        return jsonify({"error": "source_login и target_login обязательны"}), 400
    if not selected_ids:
        return jsonify({"error": "выберите хотя бы одну кампанию"}), 400
    if not counter_id:
        return jsonify({"error": "счётчик Метрики обязателен"}), 400
    if not goal_id:
        return jsonify({"error": "цель «Все формы» обязательна"}), 400
    if not target_domain:
        return jsonify({"error": "укажите домен целевого аккаунта"}), 400
    if not (target_city or target_region):
        return jsonify({"error": "укажите город или регион целевого аккаунта"}), 400
    if target_feed_url and not (target_feed_url.startswith("/") or target_feed_url.startswith(("http://", "https://"))):
        return jsonify({"error": "целевой фид должен быть абсолютным URL или путём от корня сайта"}), 400
    owner = _counter_foreign_owner(counter_id, target_login)
    if owner:
        return jsonify({"error": f"счётчик Метрики {counter_id} принадлежит аккаунту «{owner}», а не «{target_login}»"}), 400
    body = dict(body)
    body["_kind"] = "copy_campaigns"
    body["login"] = target_login
    resolved_ag = _resolve_agency_hint(target_login, (body.get("agency") or "").strip())
    if resolved_ag:
        body["agency"] = resolved_ag
    app = current_app._get_current_object()
    _ensure_create_worker(app)
    saved_session = dict(session)
    with _CREATE_JOBS_LOCK:
        existing_ids = set(_CREATE_JOBS.keys())
    job_id = _job_new(len(selected_ids), target_login, body, saved_session, dedup_login=True)
    if job_id in existing_ids:
        with _CREATE_JOBS_LOCK:
            ahead = _create_jobs_ahead(job_id)
        return jsonify({"ok": True, "job_id": job_id, "total": len(selected_ids), "login": target_login,
                        "ahead": ahead, "existing": True,
                        "note": "для целевого аккаунта уже есть активная задача; дубль копирования не создан"})
    _copy_job_upsert(job_id, status="queued", progress=0, source_login=source_login,
                     target_login=target_login, selected=len(selected_ids), total=len(selected_ids))
    with _CREATE_JOBS_LOCK:
        ahead = _create_jobs_ahead(job_id)
    return jsonify({"ok": True, "job_id": job_id, "total": len(selected_ids), "login": target_login, "ahead": ahead})


@bp.route("/api/copy_status/<job_id>")
@_direct_access
def api_copy_status(job_id: str):
    with _COPY_JOBS_LOCK:
        job = dict(_COPY_JOBS.get(job_id) or {})
    if not job:
        return jsonify({"error": "job не найден"}), 404
    return jsonify(job)


@bp.route("/api/campaigns/stop_all", methods=["POST"])
@_direct_danger
def api_stop_all():
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


@bp.route("/api/campaigns/delete_drafts", methods=["POST"])
@_direct_danger
def api_delete_drafts():
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


@bp.route("/api/campaigns/delete_drafts_async", methods=["POST"])
@_direct_danger
def api_delete_drafts_async():
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


@bp.route("/api/check_blocks", methods=["POST"])
@_direct_access
def api_check_blocks():
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


# ── Генератор имени кампании + планировщик набора ──────────────────────────────
# Тип сайта → код для середины имени (остальные типы добавим позже).


def _resolve_region(city: str | None):
    """город → (r_code, область словами). Не нашлось → ('r0000', область|'Россия')."""
    if not city or not city.strip():
        return "r0000", "Россия"
    conn = _victory_conn()
    try:
        cur = conn.cursor()
        cur.execute('SELECT "Область" FROM public.local_gsheet_yandex_direct_id_location '
                    "WHERE \"GeoRegionType\"='City' AND lower(btrim(location))=lower(btrim(%s)) LIMIT 1", (city,))
        row = cur.fetchone()
        oblast = row[0] if row else None
        if not oblast:
            return "r0000", "Россия"
        cur.execute("SELECT code FROM public.local_gsheet_naming WHERE type='ag_part4' "
                    "AND lower(btrim(name))=lower(btrim(%s)) LIMIT 1", (oblast,))
        r = cur.fetchone()
        return (r[0] if r else "r0000"), oblast
    finally:
        conn.close()


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
_GENERIC_SITELINK_FILLERS = [
    {"title": "Автокредит от 9 000 ₽/мес", "description": "Подберем условия от банков-партнеров онлайн сегодня"},
    {"title": "Первый взнос 0 ₽", "description": "Оформим кредит без первоначального взноса онлайн"},
    {"title": "Трейд-ин выше рынка", "description": "Оценим ваш автомобиль и зачтем в покупку онлайн"},
    {"title": "КАСКО на 1 год бесплатно", "description": "Условия действуют при покупке автомобиля в кредит"},
    {"title": "Одобрение за 30 минут", "description": "Отправьте заявку и получите решение банка сегодня"},
    {"title": "Выгода при покупке", "description": "Зафиксируем персональные условия покупки автомобиля"},
    {"title": "Господдержка 2025", "description": "Проверим доступные программы покупки автомобиля"},
    {"title": "Тест-драйв 2025", "description": "Выберите удобное время для знакомства с автомобилем"},
    {"title": "Авто в наличии", "description": "Подберем автомобиль под ваш бюджет онлайн сегодня"},
]


def _build_name(is_master: bool, is_auto: bool, pay: str, r_code: str, oblast: str,
                sq: str = "site", cat: str | None = None, ct: str = "ct0000") -> str:
    """Имя кампании по спеке: {коды} — {МК|ТК}_{AT|RA}_{pay}[_kviz][ - {категория}] - {область}.
    sq: 'site' (посадка = домен) | 'kviz' (посадка = домен/quiz).
    cat: категория/модель группы (Haval Jolion/Интересы/…) — отдельная кампания на неё.
    ct: 1-й код кодера. Для tp6 ПО МОДЕЛИ — ct модели (ct0119 для Haval Jolion), иначе ct0000.
        Это даёт «контент по кодеру»: движок видит модель в ct и берёт её картинку+заголовки."""
    tp = "tp6" if is_master else "tp7"
    paycode = "cpc" if pay == "tcpa" else "cpa"          # сегмент оплаты в кодах
    sqcode = "kviz" if sq == "kviz" else "site"          # ось посадки в кодах
    # Формат (ag_part5): tp6 МК → ct001 (ТГО). tp7 Товарка = Каталог+ТГО+Фид (комбинированное:
    # ListingAd по каталогу + ShoppingAd по фиду + товарное ТГО) → ct010, НЕ ct009 (ct009 = товарное
    # БЕЗ ТГО). Правило пользователя: tp7 нейминг = ct010.
    fmt = "ct001" if is_master else "ct010"              # формат: ТГО / Каталог+ТГО+Фид
    # возраст 24-55+ есть ТОЛЬКО у мастер-ручной; у товарных возраст не настраивается → всегда «Все»
    age = "ag011" if (is_master and not is_auto) else "ag001"
    codes = f"{tp}_{paycode}_{sqcode}_{ct or 'ct0000'}_aon_n000_{r_code}_{fmt}_{age}_g00"
    tp_label = "Мастер кампаний" if is_master else "Товарка"  # #6: канон CODER.md (было МК_AT_tcpa)
    cat_part = f" - {cat}" if cat else ""                 # категория аудитории в человекочитаемое имя (как в слепках)
    return f"{codes} — {tp_label}{cat_part} - {oblast}"


def _rule_sets(site_type: str, city: str) -> dict:
    """Наборы бюджет/CPA из direct_automation_rules по (site_type, city)→'*':
    {'cpa','budget'} — оплата за конверсии (CPA), {'cpc_cpa','cpc_budget'} — оплата за клики (CPC).
    Дефолт 2000/5000. cpc_* фолбэчат на cpa/budget, если NULL."""
    d = {"cpa": 2000, "budget": 5000, "cpc_cpa": 2000, "cpc_budget": 5000}
    st = (site_type or "").strip()
    if not st:
        return d
    sql = ("SELECT cpa::numeric, budget::numeric, cpc_cpa::numeric, cpc_budget::numeric "
           "FROM public.direct_automation_rules WHERE site_type=%s AND city=%s LIMIT 1")
    try:
        conn = _victory_conn()
        try:
            cur = conn.cursor()
            r = None
            if city and city != "*":
                cur.execute(sql, (st, city))
                r = cur.fetchone()
            if not r:
                cur.execute(sql, (st, "*"))
                r = cur.fetchone()
            if r:
                d["cpa"] = int(float(r[0])); d["budget"] = int(float(r[1]))
                d["cpc_cpa"] = int(float(r[2])) if r[2] is not None else d["cpa"]
                d["cpc_budget"] = int(float(r[3])) if r[3] is not None else d["budget"]
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — таблица/колонки могут отсутствовать в dev-окружении
        pass
    return d


def _tp_plan_names(slepok: str, site_type: str, tp_code: str) -> list[dict]:
    """Позиции tp из структуры слепка — одна запись на каждый item (per-кампания).

    Канон CODER.md: каждая позиция (item) = отдельная кампания. Используется для item-level
    tp, где кампании дробятся по таргетингу/марке внутри одной группы:
      tp1 (РСЯ по моделям/марке), tp4 (Поиск+Динамика по маркам/темам).
    item.t — полное имя таргетинга («РСЯ BAIC BJ40», «Поиск+Динамика Haval марка», …).
    Имя кампании строится в api_set_plan: tp{N}_cpc_site — {item.t}.

    Возвращает [{"label": item.t, "gc": item.gc}, …] или [] если нет данных.
    Дедуп по label (item.t) — на случай дублей в структуре."""
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    struct = _json("slepki_structure.json").get("directologists", [])
    d = next((x for x in struct if x.get("key") == key), None)
    if not d:
        return []
    st = next((s for s in d.get("site_types", []) if s.get("name") == site_type), None)
    if not st:
        return []
    result: list[dict] = []
    seen: set = set()
    for tp in st.get("tp", []):
        if tp.get("code") != tp_code:
            continue
        blocks = tp.get("splits") or [{"groups": tp.get("groups", [])}]
        for sp in blocks:
            for grp in sp.get("groups", []):
                for item in grp.get("items", []):
                    if not isinstance(item, dict):
                        continue
                    label = (item.get("t") or "").strip()
                    if not label or label in seen:
                        continue
                    seen.add(label)
                    result.append({"label": label, "gc": item.get("gc", "")})
    return result


def _tp1_plan_names(slepok: str, site_type: str, r_code: str) -> list[dict]:
    """Обёртка совместимости: позиции tp1 (см. _tp_plan_names)."""
    return _tp_plan_names(slepok, site_type, "tp1")


@bp.route("/api/set_plan", methods=["POST"])
@_direct_access
def api_set_plan():
    """План набора (предпросмотр, БЕЗ создания): какие кампании и с какими именами создадутся."""
    import psycopg2.extras
    body = request.json or {}
    login = (body.get("login") or "").strip()
    agent = (body.get("agent") or "").strip()            # ключ слепка-директолога (для аудиторий tp6/tp7, структуры tp1)
    variants = body.get("variants") or []                # master_auto/master_manual/product_auto/product_manual
    tp_sq = body.get("tp_sq") or {}                       # {"6":["site","kviz"], "7":["site"]} — оси посадки из набора
    # selected_pos: {tp_num_str: {labels:[...], groups:[...]}} — пер-позиционный выбор с фронта.
    # Если пришёл — фильтруем план по нему. Не пришёл — поведение прежнее (все позиции).
    selected_pos: dict = body.get("selected_pos") or {}
    def _sel_labels(tp_num: int) -> set | None:
        """Выбранные label'ы для tp (tp1). None = нет ограничений."""
        sp = selected_pos.get(str(tp_num)) or selected_pos.get(tp_num)
        if sp is None:
            return None
        labs = sp.get("labels") or []
        return set(labs) if labs else None
    def _sel_groups(tp_num: int) -> set | None:
        """Выбранные группы для tp (tp2/5/6/7). None = нет ограничений."""
        sp = selected_pos.get(str(tp_num)) or selected_pos.get(tp_num)
        if sp is None:
            return None
        grps = sp.get("groups") or []
        return set(grps) if grps else None
    def _sq_for(tp_num: str) -> list:                     # какие посадки (site/kviz) создавать для tp
        v = tp_sq.get(tp_num) or tp_sq.get(f"tp{tp_num}")
        return [s for s in (v or []) if s in ("site", "kviz")] or ["site"]
    if not login:
        return jsonify({"error": "login обязателен"}), 400
    ov_site = (body.get("site_type") or "").strip()      # ручной override типа сайта (правится в форме)
    ov_city = (body.get("city") or "").strip()           # ручной override города

    conn = _victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT city, site_type, agency_account, domain FROM public.local_gsheet_sites "
                    "WHERE login_key=%s AND direction='Авто' LIMIT 1", (login,))
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"error": f"аккаунт {login} не найден в local_gsheet_sites (Авто)"}), 404

    site_type = ov_site or (row["site_type"] or "").strip()   # override приоритетнее БД (правка ошибки в БД)
    city = ov_city or (row.get("city") or "")
    r_code, oblast = _resolve_region(city)
    # Наборы бюджет/CPA из «Глобальных правил». pay=cpa → CPA-набор (оплата за конверсии),
    # pay=tcpa → CPC-набор (оплата за клики). НЕ из формы.
    rs = _rule_sets(site_type, city)
    cpa, budget = rs["cpa"], rs["budget"]                # для resolved (read-only справка в форме)

    def _bud(pay):                                       # бюджет недели по типу оплаты
        return rs["cpa"] * 10 if pay == "cpa" else rs["cpc_budget"]

    def _cpa_for(pay):                                   # целевой CPA по типу оплаты
        return rs["cpa"] if pay == "cpa" else rs["cpc_cpa"]
    warnings: list[str] = []
    if r_code == "r0000":
        warnings.append("регион не определён — r0000")

    token, _ = _token_for_login(login, row.get("agency_account") or "", _direct_tokens())
    existing = set()
    if token:
        jc = _v5_get("campaigns", token, login, ["Name"], criteria={})
        existing = {(c.get("Name") or "") for c in (jc.get("result") or {}).get("Campaigns", [])}
        # v5 не видит черновики (State=OFF; UNIFIED/UAC-черновики v5 не отдаёт вовсе) →
        # дополняем именами из Grid (видит ВСЕ кампании, включая DRAFT и UAC). Иначе повторное
        # «Создать набор» по тому же аккаунту плодит дубли черновиков (П.4). Мягкая деградация при сбое куки.
        try:
            existing |= {(c.get("name") or "") for c in _grid_list_campaigns(login)}
        except Exception as e:  # noqa: BLE001
            warnings.append(f"Grid-список имён недоступен — дубли черновиков возможны: {str(e)[:80]}")
    else:
        warnings.append("нет агентского токена — проверка дублей имён недоступна")

    feeds = []
    if any(str(v).startswith("product") for v in variants):
        # tp7 (Товарка) размножается по фидам — но ТОЛЬКО по тем, что разрешены в «Глобальных
        # правилах» (тот же allow-list, что и tp1/tp5: _filter_allowed_feed_rows). Раньше фильтра
        # тут не было → tp7 плодил кампанию на КАЖДЫЙ фид аккаунта (вкл. неотмеченные). Фильтруем
        # СЫРЫЕ строки (у них есть name/Name → совпадает с feed_key глобальных правил), затем мапим.
        if token:
            jf = _v5_get("feeds", token, login, ["Id", "Name", "SourceType"])
            _raw = [f for f in (jf.get("result") or {}).get("Feeds", []) if f.get("SourceType") == "URL"]
            feeds = [{"id": f["Id"], "name": f.get("Name")} for f in _filter_allowed_feed_rows(_raw)]
        if not feeds:
            # v5 пусто (часто 152 — нет баллов): фиды есть, но v5-чтение стоит баллов → читаем
            # список по КУКЕ через Grid (без баллов), иначе товарные не спланируются на исчерпанном аккаунте.
            try:
                _raw = _filter_allowed_feed_rows(_grid_feeds(login, row.get("agency_account") or ""))
                feeds = [{"id": int(f["id"]), "name": f.get("name")} for f in _raw if f.get("id")]
            except Exception:  # noqa: BLE001
                feeds = []
        if not feeds:
            warnings.append("у аккаунта нет РАЗРЕШЁННЫХ фидов в «Глобальных правилах» — товарные не создадутся")
        # Галочка «по одному фиду» (single_feed): план тоже строим по ПЕРВОМУ фиду, чтобы
        # предпросмотр/счётчик совпадали с реальным созданием (иначе превью показывало все фиды,
        # а создавался один → выглядело как «галочка не работает»).
        if bool(body.get("single_feed")) and len(feeds) > 1:
            feeds = feeds[:1]
            warnings.append("«по одному фиду»: план и создание — только первый фид")

    used: set = set()

    def _uniq(name: str):
        """Уникализация имени: занято (в аккаунте или в наборе) → +_v01…_v99."""
        if name not in existing and name not in used:
            used.add(name)
            return name, False
        for v in range(1, 100):
            cand = f"{name}_v{v:02d}"
            if cand not in existing and cand not in used:
                used.add(cand)
                return cand, True
        used.add(name)
        return name, True

    pays = ["tcpa", "cpa"]
    plan = []
    want_master = want_product = False                    # tp6/tp7 строим из структуры после цикла variants
    # Текстовые движки: один элемент-кампания на tp (наполняется моделями из пака внутри).
    # tp1_rsy → ЕПК РСЯ v501 mode=network_cpa (правильный путь из CODER.md + CAMPAIGN_INVARIANTS.md)
    _TEXT_PLAN = {"search_test": "Поиск (тест)", "tp1_rsy": "РСЯ", "search_gallery": "Поиск + Динамика + ТГ",
                  "search_dynamic": "Поиск + Динамика", "rsya_gallery": "Товарная галерея (РСЯ)"}
    for v in variants:
        if str(v) in _TEXT_PLAN:
            # tp1_rsy: имя кампании строим по канону CODER.md из структуры слепка.
            # Каждый item структуры tp1 = отдельная кампания (item.t = имя таргетинга/кампании).
            if str(v) == "tp1_rsy":
                # СЕГМЕНТЫ, как в боевых аккаунтах Щербаковой: 1 РСЯ-кампания на «Марки» и
                # 1 на «Модели» (бренды/модели — ГРУППЫ ВНУТРИ, не отдельные кампании).
                # Сегмент позиции структуры определяем по первому ct её группового кодера (gc).
                # cpc+cpa-пара строится внутри движка (_create_tp1_campaign).
                tp1_items = _tp1_plan_names(agent, site_type, r_code)
                segs_present = []
                for pos in tp1_items:
                    seg = _ct_segment(pos.get("gc", ""))
                    if seg not in segs_present:
                        segs_present.append(seg)
                segs_present = [s for s in ("Марки", "Модели", "Общее") if s in segs_present] or ["Марки"]
                # Фильтр по выбранным сегментам (selected_pos[1].labels = ["Марки","Модели"]).
                sel_tp1 = _sel_labels(1)
                for seg in segs_present:
                    if sel_tp1 is not None and seg not in sel_tp1:
                        continue
                    # Режимы (КС/Автотаргет) — РОВНО как у реального аккаунта слепка (профиль).
                    # None (нет профиля, напр. Терехов) → КС, как раньше. [] → не строить (нет у слепка).
                    modes = _slepok_tp_modes(agent, site_type, "tp1", seg)
                    if modes is None:
                        modes = ["КС"]
                    for mode in modes:
                        at = mode == "Автотаргет"
                        suffix = "Автотаргетинг" if at else "КС"
                        label = f"РСЯ - {seg} - {suffix}" + (f" - {oblast}" if oblast else "")
                        nm, renamed = _uniq(f"tp1_cpc_site — {label}")
                        plan.append({"type": "tp1_rsy", "variant": v, "pay": None, "feed_id": None,
                                     "feed_name": None, "name": nm, "renamed": renamed,
                                     "budget": rs["cpc_budget"], "cpa": rs["cpc_cpa"],
                                     "tp1_segment": seg, "tp1_label": label, "autotarget": at})
                # Смарт-Баннер / Фиды — товарные объявления БЕЗ ТГО + автотаргет (как боевые),
                # отдельной кампанией если профиль слепка их ведёт. В боевых КС-варианта нет.
                for fmt in ("Смарт-Баннер", "Фиды"):
                    if sel_tp1 is not None and fmt not in sel_tp1:
                        continue
                    if "Автотаргет" not in (_slepok_tp_modes(agent, site_type, "tp1", fmt) or []):
                        continue                        # формат есть только как автотаргет (как боевые)
                    label = f"РСЯ - {fmt} - Автотаргетинг" + (f" - {oblast}" if oblast else "")
                    nm, renamed = _uniq(f"tp1_cpc_site — {label}")
                    plan.append({"type": "tp1_rsy", "variant": v, "pay": None, "feed_id": None,
                                 "feed_name": None, "name": nm, "renamed": renamed,
                                 "budget": rs["cpc_budget"], "cpa": rs["cpc_cpa"],
                                 "tp1_segment": None, "tp1_label": label,
                                 "autotarget": True, "products_only": True})
                continue
            # tp4 «Поиск + Динамика» — поисковые ТЕКСТ-кампании (движок tp2), но item-level по
            # маркам/темам (LIVE Кудерко porg-mgrauofh: TEXT_CAMPAIGN, Search=AVERAGE_CPA, Network=OFF).
            if str(v) == "search_dynamic":
                # Сегменты «Марки»/«Модели» (как боевые), бренды/модели — ГРУППЫ внутри.
                tp4_items = _tp_plan_names(agent, site_type, "tp4")
                segs4 = []
                for pos in tp4_items:
                    seg = _ct_segment(pos.get("gc", ""))
                    if seg not in segs4:
                        segs4.append(seg)
                segs4 = [s for s in ("Марки", "Модели", "Общее") if s in segs4] or ["Марки"]
                # Донор-сегмент: у слепка нет своих «Моделей» в tp4 (напр. Терехов) → добавляем
                # «Модели» от донора, чтобы структура совпала с другими слепками (контент в fill
                # возьмёт _build_text_from_pack у донора). Только если донор реально покрывает site_type.
                if "Модели" not in segs4 and _segment_donor("Модели", "tp4", site_type):
                    segs4.append("Модели")
                sel4 = _sel_labels(4)
                for seg in segs4:
                    if sel4 is not None and seg not in sel4:
                        continue
                    for pay in pays:
                        paycode = "cpc" if pay == "tcpa" else "cpa"
                        label = f"Поиск + Динамика - {seg} - КС" + (f" - {oblast}" if oblast else "")
                        nm, renamed = _uniq(f"tp4_{paycode}_site — {label}")
                        plan.append({"type": "search_dynamic", "variant": v, "pay": pay,
                                     "feed_id": None, "feed_name": None, "name": nm, "renamed": renamed,
                                     "budget": _bud(pay), "cpa": _cpa_for(pay), "tp": "tp4",
                                     "tp4_segment": seg, "tp4_label": label})
                continue
            # tp3 «Товарная галерея» (РСЯ): один item на оплату-пару — движок размножит по ВСЕМ фидам
            # (FAN-OUT, имя += фид) и сам создаст пару cpc+cpa, поэтому pay=None (как tp1).
            if str(v) == "rsya_gallery":
                nm, renamed = _uniq("tp3_cpc_site — ТГ - Фид (товары)")
                plan.append({"type": "rsya_gallery", "variant": v, "pay": None,
                             "feed_id": None, "feed_name": None, "name": nm, "renamed": renamed,
                             "budget": rs["cpc_budget"], "cpa": rs["cpc_cpa"], "tp": "tp3"})
                continue
            # tp2 «Поиск» — сегментные ТЕКСТ-кампании (как боевые: Марки/Модели × {КС, Автотаргет},
            # бренды/модели — ГРУППЫ внутри). Режимы — по профилю слепка (гейт: ровно что есть, не лишнее).
            if str(v) == "search_test":
                tp2_items = _tp_plan_names(agent, site_type, "tp2")
                segs2 = []
                for pos in tp2_items:
                    seg = _ct_segment(pos.get("gc", ""))
                    if seg not in segs2:
                        segs2.append(seg)
                segs2 = [s for s in ("Марки", "Модели", "Общее") if s in segs2] or ["Марки"]
                sel2 = _sel_labels(2)
                for seg in segs2:
                    if sel2 is not None and seg not in sel2:
                        continue
                    modes = _slepok_tp_modes(agent, site_type, "tp2", seg)
                    if modes is None:
                        modes = ["КС"]
                    for mode in modes:
                        at = mode == "Автотаргет"
                        suffix = "Автотаргетинг" if at else "КС"
                        for pay in pays:
                            paycode = "cpc" if pay == "tcpa" else "cpa"
                            label = f"Поиск - {seg} - {suffix}" + (f" - {oblast}" if oblast else "")
                            nm, renamed = _uniq(f"tp2_{paycode}_site — {label}")
                            plan.append({"type": "search_test", "variant": v, "pay": pay,
                                         "feed_id": None, "feed_name": None, "name": nm, "renamed": renamed,
                                         "budget": _bud(pay), "cpa": _cpa_for(pay), "tp": "tp2",
                                         "tp4_segment": seg, "autotarget": at})
                continue
            # tp5 «Поиск + Динамика + ТГ» — сегментные кампании Марки/Модели × {КС, Автотаргет}
            # по профилю слепка (как боевые; бренды/модели — ГРУППЫ внутри). Имя — cpc-канон;
            # движок _create_tp5_campaign сам делает пару cpc+cpa и FAN-OUT по фидам, поэтому pay=None.
            if str(v) == "search_gallery":
                tp5_items = _tp_plan_names(agent, site_type, "tp5")
                segs5 = []
                for pos in tp5_items:
                    seg = _ct_segment(pos.get("gc", ""))
                    if seg not in segs5:
                        segs5.append(seg)
                segs5 = [s for s in ("Марки", "Модели", "Общее") if s in segs5] or ["Марки"]
                sel5 = _sel_labels(5)
                for seg in segs5:
                    if sel5 is not None and seg not in sel5:
                        continue
                    modes = _slepok_tp_modes(agent, site_type, "tp5", seg)
                    if modes is None:
                        modes = ["КС"]
                    for mode in modes:
                        at = mode == "Автотаргет"
                        suffix = "Автотаргетинг" if at else "КС"
                        label = f"Поиск + Динамика + ТГ - {seg} - {suffix}" + (f" - {oblast}" if oblast else "")
                        nm, renamed = _uniq(f"tp5_cpc_site — {label}")
                        plan.append({"type": "search_gallery", "variant": v, "pay": None, "feed_id": None,
                                     "feed_name": None, "name": nm, "renamed": renamed,
                                     "budget": rs["cpc_budget"], "cpa": rs["cpc_cpa"], "tp": "tp5",
                                     "tp5_segment": seg, "autotarget": at})
                # tp5 Фиды — товарные БЕЗ ТГО + автотаргет (как боевые pavlov), если профиль ведёт.
                if "Автотаргет" in (_slepok_tp_modes(agent, site_type, "tp5", "Фиды") or []) and (sel5 is None or "Фиды" in sel5):
                    label = f"Поиск + Динамика + ТГ - Фиды - Автотаргетинг" + (f" - {oblast}" if oblast else "")
                    nm, renamed = _uniq(f"tp5_cpc_site — {label}")
                    plan.append({"type": "search_gallery", "variant": v, "pay": None, "feed_id": None,
                                 "feed_name": None, "name": nm, "renamed": renamed,
                                 "budget": rs["cpc_budget"], "cpa": rs["cpc_cpa"], "tp": "tp5",
                                 "tp5_segment": None, "autotarget": True, "products_only": True})
                continue
        # tp6 (Мастер) / tp7 (Товарка) строим НЕ здесь, а из СТРУКТУРЫ слепка (после цикла) —
        # чтобы предпросмотр/создание 1:1 совпадали с вкладками «Структура»/«Создание РК».
        if str(v).startswith("master"):
            want_master = True
        elif str(v).startswith("product"):
            want_product = True

    # ── tp6/tp7: источник — slepki_structure.json (как верх). 1 кампания на (группа × оплата). ──
    # Без взрыва по фидам: товарной (UAC product) нужен ОДИН feed_id (первый XML-фид аккаунта).
    feed0 = feeds[0] if feeds else None

    emitted_tp67: set[tuple] = set()

    def _emit_struct(tp_code: str, is_master: bool):
        tp_num = 6 if is_master else 7
        groups = _slepok_struct_groups(agent, site_type, tp_code)
        if not groups:                                   # нет структуры → одна кампания без разреза (фолбэк)
            groups = [{"name": None, "sq": "site", "is_auto": True}]
        # Фильтр по выбранным позициям кампаний (tp6/tp7 — это кампании, НЕ группы).
        sel_pos = _sel_labels(tp_num) or _sel_groups(tp_num)
        if sel_pos is not None:
            groups = [g for g in groups
                      if (g.get("name") or "") in sel_pos or (g.get("group") or "") in sel_pos]
        allowed = _sq_for("6" if is_master else "7")
        for g in groups:
            if g["sq"] not in allowed:                   # уважать выбранные оси посадки (site/kviz) из набора
                continue
            cat = g["name"]
            targeting_mode = _tp67_targeting_mode(g)
            is_auto_name = targeting_mode != "keywords"
            cat_base = (g.get("group") or cat or "").strip()
            interest_cat = g.get("group") or cat
            ints, ints_source = (_slepok_interest_for_struct(agent, site_type, tp_code, g)
                                 if targeting_mode == "audience" else ([], "not-audience"))
            # Если название группы — РЕАЛЬНАЯ марка/модель (tp6 Мастер: «Haval Jolion»), берём её ct
            # (ct0119) в КОДЕР → движок выберет картинку+заголовки этой модели. Тема/общее → ct0000.
            cat_ct = (_ct_for_name(cat_base) or _ct_for_name(cat) or _gc_ct(g.get("code") or "") or "ct0000")
            # FAN-OUT (CODER.md): tp7 (Товарка) фидовый → каждый фид своя кампания, имя += фид.
            # tp6 (Мастер кампаний) — без фида (одна запись).
            feed_list = ([(None, None)] if is_master
                         else [((f or {}).get("id"), (f or {}).get("name")) for f in (feeds or [None])])
            for f_id, f_name in feed_list:
                for pay in pays:
                    base_nm = _build_name(is_master, is_auto_name, pay, r_code, oblast, g["sq"], cat, ct=cat_ct)
                    if f_name and not _is_site_domain_name(f_name, row.get("domain") or ""):
                        base_nm = f"{base_nm} — {f_name}"
                    payload_sig = (
                        "master" if is_master else "product",
                        tp_code,
                        pay,
                        g["sq"],
                        f_id or 0,
                        cat_ct,
                        targeting_mode,
                        _tp67_kw_position_key(cat or interest_cat or ""),
                        tuple(str(x) for x in (ints or [])),
                    )
                    if payload_sig in emitted_tp67:
                        continue
                    emitted_tp67.add(payload_sig)
                    nm, renamed = _uniq(base_nm)
                    plan.append({"type": "master" if is_master else "product",
                                 "variant": ("master_" if is_master else "product_") + ("manual" if targeting_mode == "keywords" else "auto"),
                                 "pay": pay, "sq": g["sq"], "tp": tp_code,
                                 "feed_id": f_id, "feed_name": f_name, "ct": cat_ct,
                                 "coder_ct": cat_ct, "coder_brand": _ag_part1_map().get(cat_ct, ""),
                                 "name": nm, "renamed": renamed, "budget": _bud(pay), "cpa": _cpa_for(pay),
                                 "audience_cat": interest_cat, "position_name": cat,
                                 "targeting_mode": targeting_mode, "audience_source": ints_source,
                                 "structure_code": g.get("code") or "", "interest_ids": ints})

    if want_master:
        _emit_struct("tp6", True)
    if want_product:
        _emit_struct("tp7", False)
    return jsonify({"login": login, "site_type": site_type, "r_code": r_code, "oblast": oblast,
                    "feeds": len(feeds), "count": len(plan),
                    "resolved_cpa": cpa, "resolved_budget": budget,   # бюджет/CPA из правил (для read-only + создания)
                    "renamed": sum(1 for p in plan if p["renamed"]), "plan": plan, "warnings": warnings})


def _num(val, default):
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


def _account_ctx(login: str):
    """Контекст для создания: domain, site_type, agency, geoid ОБЛАСТИ (таргетинг — область, не город)."""
    import psycopg2.extras
    conn = _victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT domain, city, site_type, agency_account, directologist FROM public.local_gsheet_sites "
                    "WHERE login_key=%s AND direction='Авто' LIMIT 1", (login,))
        row = cur.fetchone()
        if not row:
            return None
        oblast = None
        if row.get("city"):
            cur.execute('SELECT "Область" AS o FROM public.local_gsheet_yandex_direct_id_location '
                        "WHERE \"GeoRegionType\"='City' AND lower(btrim(location))=lower(btrim(%s)) LIMIT 1",
                        (row["city"],))
            r = cur.fetchone()
            oblast = (r["o"] if r else None)
    finally:
        conn.close()
    geoid = 225                                          # таргет — geoid ОБЛАСТИ (через словарь Директа)
    if oblast:
        gid = _geo_load().get(oblast.strip().lower())
        if gid:
            geoid = int(gid)
    return {"domain": (row.get("domain") or "").strip(), "site_type": (row.get("site_type") or "").strip(),
            "agency": row.get("agency_account"), "geoid": geoid, "oblast": oblast,
            "city": (row.get("city") or "").strip(),
            "directologist": (row.get("directologist") or "").strip()}


def _templates_for(site_type: str):
    """→ (titles, texts, sitelinks[{title,description}]) по типу сайта."""
    conn = _victory_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT kind, content FROM public.direct_ad_templates "
                    "WHERE enabled AND site_type=%s ORDER BY kind, id", (site_type,))
        titles, texts, sitelinks = [], [], []
        for kind, content in cur.fetchall():
            if kind == "title":
                titles.append(content)
            elif kind == "text":
                texts.append(content)
            elif kind == "sitelink":
                try:
                    d = json.loads(content)
                    sitelinks.append({"title": d.get("title", ""), "description": d.get("description", "")})
                except Exception:  # noqa: BLE001
                    pass
        return titles, texts, sitelinks
    finally:
        conn.close()


def _slepok_audiences_for(slepok: str, site_type: str, tp: str) -> list[str]:
    """Нативные интересы слепка для (slepok × site_type × tp) → объединённый список id (str).
    Источник: public.direct_slepok_audiences (kind in_market/interests). Пусто → []."""
    if not (slepok and site_type and tp):
        return []
    try:
        conn = _victory_conn()
    except Exception:  # noqa: BLE001
        return []
    try:
        cur = conn.cursor()
        cur.execute("SELECT interest_ids FROM public.direct_slepok_audiences "
                    "WHERE slepok=%s AND site_type=%s AND tp=%s", (slepok, site_type, tp))
        ids: set = set()
        for (arr,) in cur.fetchall():
            for x in (arr or []):
                if str(x).strip():
                    ids.add(str(x))
        return sorted(ids)
    except Exception:  # noqa: BLE001
        return []
    finally:
        conn.close()


def _norm_slepok_audience_category(x: str | None) -> str:
    s = re.sub(r"\s+", " ", (x or "").strip().lower())
    if s in ("", "общая"):
        return "(общая)"
    return s


def _tp67_targeting_mode(g: dict) -> str:
    """Новый канон tp6/tp7: keywords / autotarget / audience. Старые RA/RC-коды не поддерживаем."""
    text = " ".join(str(g.get(k) or "") for k in ("name", "group", "label", "code")).lower()
    label = str(g.get("label") or "").lower()
    if ("ключев" in label) or re.search(r"\bкс\b", label):
        return "keywords"
    if re.search(r"автотаргет|автоматическ", text):
        return "autotarget"
    if re.search(r"интерес|автокредит|авито|дром|auto\.ru|авто ру|конкурент", text):
        return "audience"
    return "autotarget"


def _tp67_audience_category_candidates(g: dict) -> list[str]:
    """Категории только внутри конкретного слепка; aliases нужны для старых подписей структуры."""
    text = " ".join(str(g.get(k) or "") for k in ("name", "group", "label")).lower()
    raw = [g.get("group"), g.get("name"), g.get("label")]
    out: list[str] = []
    for x in raw:
        nx = _norm_slepok_audience_category(str(x or ""))
        if nx and nx not in out:
            out.append(nx)
    if "общие запрос" in text:
        out.append("общие запросы")
    if "дилер интерес" in text:
        out.append("дилер интересы")
    if "дилер" in text:
        out.append("дилер")
    if "интерес" in text:
        out.append("интересы")
    if re.search(r"общая|товарная|модели|марки|автокредит|кредит|авито|дром|авто ру|auto\.ru", text):
        out.extend(["(общая)", "(нестандарт)"])
    dedup: list[str] = []
    for x in out:
        nx = _norm_slepok_audience_category(x)
        if nx and nx not in dedup:
            dedup.append(nx)
    return dedup


def _slepok_audience_cats(slepok: str, site_type: str, tp: str) -> list[dict]:
    """Аудитории слепка ПО КАТЕГОРИЯМ (БЕЗ мёржа) — как в слепках: отдельная кампания на категорию.
    → [{"category": str, "interest_ids": [str,...]}] из public.direct_slepok_audiences.
    Пустые категории отбрасываем. Источник тот же, что у _slepok_audiences_for, но без объединения."""
    if not (slepok and site_type and tp):
        return []
    try:
        conn = _victory_conn()
    except Exception:  # noqa: BLE001
        return []
    try:
        cur = conn.cursor()
        cur.execute("SELECT category, interest_ids FROM public.direct_slepok_audiences "
                    "WHERE slepok=%s AND site_type=%s AND tp=%s ORDER BY category", (slepok, site_type, tp))
        out = []
        for cat, arr in cur.fetchall():
            ids = sorted({str(x) for x in (arr or []) if str(x).strip()})
            if ids:
                out.append({"category": (cat or "(общая)"), "interest_ids": ids})
        return out
    except Exception:  # noqa: BLE001
        return []
    finally:
        conn.close()


def _slepok_struct_groups(slepok: str, site_type: str, tp: str) -> list[dict]:
    """Позиции СТРУКТУРЫ слепка для (slepok, site_type, tp6|tp7).

    Источник — slepki_structure.json (ТОТ ЖЕ, что рисует вкладки «Структура»/«Создание РК»),
    чтобы план создания совпадал с показом. is_auto берём из таргетинга группы (item.t):
    есть «КС»/«ключев…» → ручной (manual, ключи), иначе автотаргетинг."""
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    d = next((x for x in _json("slepki_structure.json").get("directologists", []) if x.get("key") == key), None)
    if not d:
        return []
    st = next((s for s in d.get("site_types", []) if s.get("name") == site_type), None)
    if not st:
        return []
    out: list[dict] = []
    for t in st.get("tp", []):
        if t.get("code") != tp:
            continue
        blocks = t.get("splits") or ([{"sq": "site", "groups": t.get("groups", [])}] if t.get("groups") else [])
        for sp in blocks:
            sq = sp.get("sq") or "site"
            for g in sp.get("groups", []):
                gname = (g.get("name") or "").strip()
                if gname.lower() in ("(общая)", "общая"):
                    gname = "Общая"
                items = [it for it in (g.get("items") or []) if isinstance(it, dict)] or [{}]
                for idx, it in enumerate(items):
                    label = (it.get("t") or "").strip()
                    label_clean = "" if label in ("", "—", "-") else label
                    tl = label.lower()
                    is_auto = not (("ключев" in tl) or re.search(r"\bкс\b", tl))
                    display = gname
                    if label_clean and label_clean.lower() not in gname.lower():
                        display = f"{gname} - {label_clean}" if gname else label_clean
                    out.append({"name": display or label_clean or gname or None,
                                "group": gname, "label": label_clean,
                                "sq": sq, "is_auto": is_auto,
                                "code": it.get("c") or it.get("code") or "",
                                "pos_key": f"{sq}|{gname}|{label or idx}"})
    return out


def _slepok_interest_for_cat(slepok: str, site_type: str, tp: str, cat: str | None) -> list:
    """interest_ids слепка для категории структурной группы (если совпала с категорией аудиторий
    direct_slepok_audiences). Нет совпадения → [] (create_set фолбэкнет на объединённый список)."""
    if not cat:
        return []
    low = cat.strip().lower()
    for c in _slepok_audience_cats(slepok, site_type, tp):
        if (c.get("category") or "").strip().lower() == low:
            return c.get("interest_ids") or []
    return []


def _slepok_interest_for_struct(slepok: str, site_type: str, tp: str, g: dict) -> tuple[list[str], str]:
    """Аудитории строго по текущему слепку; no cross-slepok/global merge."""
    cats = _slepok_audience_cats(slepok, site_type, tp)
    by_cat = {_norm_slepok_audience_category(c.get("category")): c.get("interest_ids") or [] for c in cats}
    for cand in _tp67_audience_category_candidates(g):
        ids = by_cat.get(cand)
        if ids:
            return ids, cand
    merged = sorted({str(x) for ids in by_cat.values() for x in ids if str(x).strip()})
    return merged, "fallback" if merged else "none"


def _tp67_kw_position_key(text: str | None) -> str:
    """Нормализованный ключ позиции для fallback-библиотеки реальных UAC keywords."""
    s = re.sub(r"\[[^\]]*\]", " ", str(text or "").replace("\xa0", " "))
    s = re.sub(r"\b(мк|тк|ключевики|кс)\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(автотаргетинг|автоматическая)\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"[·—–_-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return "общие запросы" if s in ("общая", "общие", "общие запросы") else s


def _tp67_real_keyword_items() -> list[dict]:
    try:
        return _json("tp67_real_keywords.json").get("items") or []
    except Exception:  # noqa: BLE001
        return []


def _tp67_keywords_from_real_library(slepok: str, site_type: str, tp: str, ct: str,
                                     city: str, position_name: str | None,
                                     sq: str | None = None) -> tuple[list[str], list[str]]:
    """Fallback: реальные keywords из cookie-payload UAC, когда M3-пак пустой.

    Приоритет точный: слепок + ст + tp + sq + ct/позиция. Разные позиции ct0000
    (Автосалон/Дилер/Общие запросы) не схлопываем, потому что в реальных аккаунтах
    у них разные keyword lists.
    """
    skey = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    pos_key = _tp67_kw_position_key(position_name)
    ct_key = (ct or "").strip().lower()
    sq_key = (sq or "").strip().lower()
    items = _tp67_real_keyword_items()

    def _score(it: dict) -> tuple[int, int, int, int, int, int] | None:
        if it.get("tp") != tp:
            return None
        same_slepok = 1 if it.get("slepok") == skey else 0
        site_score = 1 if (not site_type or it.get("site_type") == site_type) else 0
        sq_score = 1 if (not sq_key or it.get("sq") == sq_key) else 0
        ct_score = 1 if (ct_key and it.get("ct") == ct_key) else 0
        pos_score = 1 if (pos_key and it.get("position") == pos_key) else 0
        if not (ct_score or pos_score):
            return None
        # Приоритет: тот же слепок/site/sq, затем позиция, затем ct.
        # Если точного слепка нет в partial live-reference, берём лучший реальный набор
        # по той же позиции/ct из другого слепка вместо падения "КС без ключей".
        return (same_slepok, site_score, sq_score, pos_score, ct_score, len(it.get("keywords") or []))

    best = None
    best_score = None
    for it in items:
        sc = _score(it)
        if sc is not None and (best_score is None or sc > best_score):
            best = it
            best_score = sc
    if not best:
        return [], []
    pos = _kw_clean(_drop_used_car(_drop_foreign_city_keywords(best.get("keywords") or [], city), site_type), 200)
    neg = _kw_clean(best.get("minus") or [], 100)
    return pos, neg


def _tp67_keywords_for(slepok: str, site_type: str, tp: str, ct: str, city: str,
                       position_name: str | None = None, sq: str | None = None) -> tuple[list[str], list[str]]:
    """Ключи из M3-пака текущего слепка; если M3 пустой — fallback из реальных UAC payload по кукам."""
    skey = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    ct_key = ct or "ct0000"

    def _pack_keywords(tp_key: str) -> tuple[list[str], list[str]]:
        kw = kp.read_keywords(site_type, tp_key, ct_key, skey)
        pos = _kw_clean(_drop_used_car(_drop_foreign_city_keywords(kw.get("positive") or [], city), site_type), 200)
        neg = _kw_clean(kw.get("minus") or [], 100)
        return pos, neg

    pos, neg = _pack_keywords(tp)
    if pos:
        return pos, neg

    pos, neg = _tp67_keywords_from_real_library(slepok, site_type, tp, ct_key, city, position_name, sq)
    if pos:
        return pos, neg

    # tp7 «Товарка» по интенту близка к tp6 «Мастер кампаний»: если отдельный tp7-пул пуст,
    # берём ключи tp6 по тому же ct/позиции, чтобы не терять кампанию.
    if tp == "tp7":
        pos, neg = _pack_keywords("tp6")
        if pos:
            return pos, neg
        return _tp67_keywords_from_real_library(slepok, site_type, "tp6", ct_key, city, position_name, sq)

    return [], []


# Состав групп «Т+Л+ТОВ» (TextAd + ListingAd + ShoppingAd по фиду) — у каких слепков для какого tp.
# Сверено LIVE grid 2026-06-21: Щербакова tp1 = товарные всегда; Павлов/Крючкова (wide=Модели) tp1 = нет.
# tp5 («Поиск + Динамика + Товарная Галерея») — товарные у ВСЕХ слепков (это его суть).
_SHOPPING_RULE = {"tp1": {"scherbakova"}, "tp5": {"scherbakova", "kryuchkova", "pavlov"}}


def _slepok_uses_shopping(slepok: str, tp: str) -> bool:
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    return key in _SHOPPING_RULE.get(tp, set())


def _first_url_feed(token: str, login: str, agency: str = "") -> int:
    """Первый XML-фид аккаунта (SourceType=URL) → id, или 0. Нужен для товарных объявлений tp1/tp5.
    При пустом v5 (часто 152 — нет баллов) фолбэк на список фидов по КУКЕ (Grid, без баллов).
    Пропускает фиды в ERROR-состоянии (cmc._dead_feed_ids — пополняется при FEED_NOT_EXIST retry)."""
    if token:
        try:
            jf = _v5_get("feeds", token, login, ["Id", "Name", "SourceType"])
            allowed = _allowed_feed_keys()
            if not allowed:
                return 0
            for f in (jf.get("result") or {}).get("Feeds", []):
                if f.get("SourceType") == "URL":
                    try:
                        fid = int(f["Id"])
                    except (TypeError, ValueError):
                        continue
                    if fid in cmc._dead_feed_ids:
                        continue
                    if not _feed_row_allowed(f, allowed):
                        continue
                    return fid
        except Exception:  # noqa: BLE001
            pass
    if agency:                                            # v5 пусто/152 → по куке
        for f in _filter_allowed_feed_rows(_grid_feeds(login, agency)):
            try:
                fid = int(f["id"])
            except (TypeError, ValueError, KeyError):
                continue
            if fid in cmc._dead_feed_ids:
                continue
            return fid
    return 0


def _catalog_feed(token: str, login: str, prefer_id: int = 0, agency: str = "") -> int:
    """Фид «страницы каталога» для tp7 (listings). Правило пользователя: берём ТОТ ЖЕ,
    что под товары (prefer_id); иначе фид с именем yandex-catalog.xml. Фолбэк по КУКЕ (Grid) при 152."""
    if prefer_id:
        return prefer_id
    if token:
        try:
            jf = _v5_get("feeds", token, login, ["Id", "Name", "SourceType"])
            allowed = _allowed_feed_keys()
            if not allowed:
                return 0
            for f in (jf.get("result") or {}).get("Feeds", []):
                nm = (f.get("Name") or "").lower()
                if not _feed_row_allowed(f, allowed):
                    continue
                if "yandex-catalog" in nm or nm.replace(" ", "").endswith("catalog.xml"):
                    return int(f["Id"])
        except Exception:  # noqa: BLE001
            pass
    if agency:                                            # v5 пусто/152 → по куке: фид с listings (каталог) или первый
        rows = _filter_allowed_feed_rows(_grid_feeds(login, agency))
        for f in rows:                                    # предпочитаем фид с листингами (страницы каталога)
            if (f.get("listings") or []):
                try:
                    return int(f["id"])
                except (TypeError, ValueError, KeyError):
                    pass
        for f in rows:
            try:
                return int(f["id"])
            except (TypeError, ValueError, KeyError):
                continue
    return 0


# ── Модельные коллекции фидов (для фильтра товарных по модели — «только Lada Granta») ──────────
_FEEDS_QUERY = ("query Feeds($login:String!){client(searchBy:{login:$login}){"
                "feeds{rowset{id name listings{id name}}}}}")


def _grid_feeds(login: str, agency: str) -> list:
    """rowset фидов аккаунта через grid (куки агентства): [{id,name,listings:[{id,name}]}]. [] при сбое."""
    import requests as _rqs
    try:
        cookie = cmc.pick_working_cookie(login, accounts=((agency,) if agency else cmc.DEFAULT_COOKIE_ACCOUNTS))
    except Exception:  # noqa: BLE001
        return []
    if not cookie:
        return []
    csrf = _block_bootstrap(cookie, agency)            # self-probe → CSRF
    headers = {"Cookie": cookie, "dna-operation-name": "Feeds", "x-direct-api": "1",
               "x-detected-locale": "ru", "Content-Type": "application/json", "User-Agent": cmc.USER_AGENT}
    if csrf:
        headers["x-csrf-token"] = csrf
    url = f"{_GRID_URL}?operationName=Feeds&ulogin={login}"
    payload = {"operationName": "Feeds", "query": _FEEDS_QUERY, "variables": {"login": login}}
    try:
        r = _rqs.post(url, json=payload, headers=headers, timeout=60, verify=False)
        if r.status_code == 403:                       # протух CSRF → один ретрай
            c2 = _grid_csrf(r)
            if c2:
                headers["x-csrf-token"] = c2
                r = _rqs.post(url, json=payload, headers=headers, timeout=60, verify=False)
        data = r.json()
        return (((data.get("data") or {}).get("client") or {}).get("feeds") or {}).get("rowset") or []
    except Exception:  # noqa: BLE001
        return []


def _account_model_feeds(login: str, agency: str, catalog_only: bool = False) -> list[dict]:
    """ВСЕ фиды аккаунта с модельными коллекциями (listings вида 'model_N').
    → [{"id":int, "name":str, "models":{listing_name_lower: collection_id}}]. Фиды без моделей отброшены.

    catalog_only=True → оставить ТОЛЬКО фиды с role='catalog' в Глобальных правилах (лендинг-фиды
    role='landing' отброшены — их model-листинги пусты, из-за чего tp1 удаляла кампанию). Флаг
    задаёт ТОЛЬКО tp1 (см. call-site run_create_set_tp1); tp7/product зовёт без флага (все enabled)."""
    out = []
    cat_keys = _catalog_feed_keys() if catalog_only else None
    for f in _filter_allowed_feed_rows(_grid_feeds(login, agency)):
        if cat_keys is not None and not _feed_row_allowed(f, cat_keys):
            continue                                   # не-каталог (лендинг/оффер) фид → пропускаем
        models = {}
        for l in (f.get("listings") or []):
            lid = str(l.get("id") or "")
            if lid.startswith("model_") and l.get("name"):
                models[(l.get("name") or "").strip().lower()] = lid
        if models:
            try:
                out.append({"id": int(f["id"]), "name": (f.get("name") or "").strip(), "models": models})
            except (TypeError, ValueError):
                continue
    return out


# ── Цена в комбинаторном объявлении из фида (Grid по куке, без баллов) ──────────
# FeedOffersPreview → цена товара (current/old); UpdateAdaptiveTextAds → проставить adPrice на объявление.
_FEED_OFFERS_Q = ("query FeedOffersPreview($feedId:Long!$filterConditions:[GdSmartFilterConditionInput!]!"
                  "$filterMobileAppOffers:Boolean$bannerId:Long$campaignId:Long){feedOffersPreview("
                  "feedId:$feedId filterConditions:$filterConditions filterMobileAppOffers:$filterMobileAppOffers "
                  "bannerId:$bannerId campaignId:$campaignId){previews{price{current old} text{name} targetUrl}}}")
_UPD_ADAPTIVE_Q = ("mutation UpdateAdaptiveTextAds($updateInput:GdUpdateAdaptiveTextAdsInput!){"
                   "updateAdaptiveTextAds(input:$updateInput){updatedAds{id}validationResult{errors{code path params}}}}")


def _offer_price_keys(name: str) -> set:
    """Ключи цены из имени оффера (lower, промо/хвост уже срезаны). Нормализация чтобы:
      • модель группы матчилась несмотря на ГОД/тех.хвост: «baic u5 plus 2026» → ключ «baic u5 plus»;
      • офферы вида «автомобиль baic x75 1.5 л. (177 л.с)» дали бренд «baic», а не «автомобиль»
        (иначе реальные офферы со скидкой теряются из бренд-фолбэка).
    Возвращает набор {полное, без-года, бренд(1-е слово)} для исходного и очищенного вида."""
    base = (name or "").strip().lower()
    if not base:
        return set()
    keys: set = set()

    def _add(s: str) -> None:
        s = re.sub(r"\s+", " ", (s or "").strip())
        if s:
            keys.add(s)
            if " " in s:
                keys.add(s.split()[0])

    _add(base)                                                   # исходное имя (обратная совместимость)
    cleaned = re.sub(r"^(?:автомобиль|автомобили|машина|машины|авто|новый|новая|новые)\s+", "", base)
    cleaned = re.split(r"\s+\d+(?:[.,]\d+)?\s*л\b", cleaned, maxsplit=1)[0]   # тех.хвост: «1.5 л …»
    cleaned = re.split(r"\s*\(", cleaned, maxsplit=1)[0]          # «(177 л.с)»
    _add(cleaned)
    _add(re.sub(r"\s*\b20\d\d\b", " ", cleaned))                 # без года: «baic u5 plus 2026» → «baic u5 plus»
    return keys


def _merge_price(prev: tuple | None, new: tuple) -> tuple:
    """Выбрать лучшую пару (current, old): чистый МИНИМУМ current; при равном — больший old
    (зачёркнутая цена). Приоритет скидки НЕ даём — цена «от X» важнее наличия crossed-out."""
    cur, old = new
    if prev is None:
        return new
    prev_cur, prev_old = prev
    if cur < prev_cur or (cur == prev_cur and old > prev_old):
        return new
    return prev


def _grid_feed_offer_prices(login: str, feed_id: int) -> dict:
    """Цены товаров фида по куке (без баллов): {ключ(lower): (current:int, old:int)} — для бренда
    (первое слово) и для модели целиком берём САМЫЙ ДЕШЁВЫЙ оффер («от X» в комбинаторном). {} при сбое."""
    if not feed_id:
        return {}
    try:
        gc = gf.GridClient(login)
        gc._bootstrap_csrf()
        v = {"feedId": int(feed_id), "filterConditions": [], "filterMobileAppOffers": False,
             "bannerId": None, "campaignId": None}
        r = gc._post("FeedOffersPreview", _FEED_OFFERS_Q, v)
        if r.status_code == 403:
            r = gc._post("FeedOffersPreview", _FEED_OFFERS_Q, v)
        prev = ((r.json().get("data") or {}).get("feedOffersPreview") or {}).get("previews") or []
    except Exception:  # noqa: BLE001
        return {}
    prices: dict = {}
    for o in prev:
        p = o.get("price") or {}
        try:
            cur, old = int(p.get("current") or 0), int(p.get("old") or 0)
        except (TypeError, ValueError):
            continue
        if cur <= 0:
            continue
        name = ((o.get("text") or {}).get("name") or "").split(":")[0].strip()
        # Чистим промо-обёртку: «-35% renault arkana — за заявку» / «skoda … от 2 681 000 р. звоните»
        # → «renault arkana» / «skoda …». Иначе split()[0] = «-35%» (бренд-матч ломался).
        name = re.sub(r"^\s*[-–]?\s*\d+\s*%\s*", "", name)            # ведущий «-35% »
        name = re.split(r"\s+(?:—|–|от|за)\s", name, maxsplit=1)[0]   # хвост «— за заявку» / «от … р»
        name = name.strip().lower()
        if not name:
            continue
        keys = _offer_price_keys(name)                              # модель(+без года) + бренд (без «автомобиль»)
        for k in keys:
            prices[k] = _merge_price(prices.get(k), (cur, old))
    return prices


_OFFER_URL_CACHE: dict = {}   # (login, feed_id) → (url_map, ts)


def _grid_feed_offer_urls(login: str, feed_id: int) -> dict:
    """URL страницы модели из фида (targetUrl): {ключ(lower) → url} — первый оффер на ключ.
    Нормализация та же, что _offer_price_keys. Кэш 20 мин. {} при сбое или feed_id=0."""
    if not feed_id:
        return {}
    _key = (login, feed_id)
    _hit = _OFFER_URL_CACHE.get(_key)
    if _hit and (time.time() - _hit[1]) < _OFFER_PRICE_TTL:
        return _hit[0]
    try:
        gc = gf.GridClient(login)
        gc._bootstrap_csrf()
        v = {"feedId": int(feed_id), "filterConditions": [], "filterMobileAppOffers": False,
             "bannerId": None, "campaignId": None}
        r = gc._post("FeedOffersPreview", _FEED_OFFERS_Q, v)
        if r.status_code == 403:
            r = gc._post("FeedOffersPreview", _FEED_OFFERS_Q, v)
        prev = ((r.json().get("data") or {}).get("feedOffersPreview") or {}).get("previews") or []
    except Exception:  # noqa: BLE001
        return {}
    urls: dict = {}
    for o in prev:
        turl = (o.get("targetUrl") or "").strip()
        if not turl:
            continue
        name = ((o.get("text") or {}).get("name") or "").split(":")[0].strip()
        name = re.sub(r"^\s*[-–]?\s*\d+\s*%\s*", "", name)
        name = re.split(r"\s+(?:—|–|от|за)\s", name, maxsplit=1)[0].strip().lower()
        if not name:
            continue
        for k in _offer_price_keys(name):
            if k not in urls:          # первый оффер на ключ
                urls[k] = turl
    if urls:
        _OFFER_URL_CACHE[_key] = (urls, time.time())
    return urls


def _feed_url_for_model(urls: dict, model: str) -> str | None:
    """targetUrl из фида для модели/марки. Логика та же, что _ad_price_for_brand."""
    b = (model or "").strip().lower()
    if not b or not urls:
        return None
    if b in urls:
        return urls[b]
    b_noyear = re.sub(r"\s*\b20\d\d\b", " ", b).strip()
    if b_noyear != b and b_noyear in urls:
        return urls[b_noyear]
    return urls.get(b.split()[0]) if b else None


def _ad_price_for_brand(prices: dict, brand: str) -> tuple:
    """(current, old) под бренд/модель группы из карты цен фида; (0,0) если нет совпадения.
    Стрипаем год (20\\d\\d) из имени группы — ct вида «BAIC X35 2026» матчит ключ «baic x35»."""
    b = (brand or "").strip().lower()
    if not b:
        return (0, 0)
    if b in prices:
        return prices[b]
    b_noyear = re.sub(r"\s*\b20\d\d\b", " ", b).strip()
    if b_noyear != b and b_noyear in prices:
        return prices[b_noyear]
    return prices.get(b.split()[0], (0, 0))               # по бренду (первое слово модели)


def _min_offer_price(prices: dict) -> tuple:
    """Минимальная цена товара из фида для общих групп, где нет марки/модели."""
    best: tuple[int, int] = (0, 0)
    for cur, old in (prices or {}).values():
        try:
            cur_i, old_i = int(cur or 0), int(old or 0)
        except (TypeError, ValueError):
            continue
        if cur_i <= 0:
            continue
        if not best[0] or cur_i < best[0] or (cur_i == best[0] and old_i > best[1]):
            best = (cur_i, old_i)
    return best


def _group_ad_price(prices: dict, brand: str, seg: str = "") -> tuple:
    """(current, old) для adPrice С УЧЁТОМ СЕГМЕНТА группы (правило пользователя):
      seg=='Марки' (группа ТОЛЬКО по марке, напр. «Changan») → МИНИМАЛЬНАЯ цена по марке —
        всегда ключ-бренд (первое слово), а не цена конкретной модели. _grid_feed_offer_prices
        уже агрегирует минимум оффера на ключ-бренд, поэтому берём именно его.
      Модели → цена модели целиком, при отсутствии — фолбэк на бренд (_ad_price_for_brand).
      Общее/аудиторные ct → минимальный товар из фида + безопасная старая цена на этапе записи."""
    if seg and seg not in ("Марки", "Модели"):
        return _min_offer_price(prices)
    b = (brand or "").strip().lower()
    if not b:
        return _min_offer_price(prices)
    if seg == "Марки":
        pr = prices.get(b.split()[0], prices.get(b, (0, 0)))     # МИН цена марки (ключ-бренд)
    else:                                                        # Модели
        pr = _ad_price_for_brand(prices, brand)
    # Фолбэк (#3 review): брендовая группа без своего оффера в фиде → «от {мин цена фида}», а не пустая
    # цена (иначе тумблер «Цена» выключен). Пустая карта → _min_offer_price даёт (0,0) — цену не выдумываем.
    return pr if pr and pr[0] else _min_offer_price(prices)


def _safe_old_price(current: int, old: int = 0) -> int:
    """Вернуть старую цену для adPrice. Если фид не дал old или old<=current,
    создаём безопасную зачёркнутую цену выше текущей."""
    try:
        cur = int(current or 0)
        old_i = int(old or 0)
    except (TypeError, ValueError):
        return 0
    if cur <= 0:
        return 0
    if old_i > cur:
        return old_i
    # +12%, округление вверх до 10 000 ₽: выглядит как нормальная старая цена и гарантирует old>cur.
    import math
    return int(math.ceil((cur * 1.12) / 10000.0) * 10000)


def _grid_ad_price_payload(current: int, old: int = 0) -> dict | None:
    try:
        cur = int(current or 0)
    except (TypeError, ValueError):
        return None
    if cur <= 0:
        return None
    old_i = _safe_old_price(cur, old)
    # prefix="FROM" → в UI Директа цена показывается как «от X» (HAR 29, UpdateAdaptiveTextAds.adPrice).
    # Раньше был None (= «Без префикса»). Правило директолога Михаила: всегда «от».
    return {"price": str(cur), "priceOld": str(old_i) if old_i else "",
            "prefix": "FROM", "currency": "RUB"}


# ── Персистентный (на время жизни процесса) кеш imageHash для Grid-аплоадов ──────────────
# imageHash в Яндексе живёт в библиотеке ЛОГИНА и валиден для всех его кампаний → каждую
# уникальную картинку достаточно залить ОДИН раз на аккаунт. Без этого кеша cookie/Grid-путь
# заливал те же бренд-картинки ЗАНОВО на каждую кампанию (десятки секунд × сотни кампаний).
# Ключ = (login, realpath) — НЕ basename: у разных брендов файлы могут зваться "1.jpg",
# basename-ключ перепутал бы хеши и размазал чужую картинку на все бренды.
_GRID_IMG_HASH_CACHE: dict[tuple[str, str], str] = {}
_GRID_IMG_HASH_LOCK = threading.Lock()
_GRID_IMG_CACHE_STATS = {"hit": 0, "miss": 0}


def _cached_upload_image(gc_img, login: str, path: str):
    """Залить картинку в библиотеку логина через Grid с переиспользованием imageHash между
    кампаниями/воркерами одного процесса. Потокобезопасно (общий lock на кеш). Возвращает
    imageHash (str) или None. realpath-ключ гарантирует точное соответствие файл→хеш."""
    if not path:
        return None
    try:
        key = (login, os.path.realpath(path))
    except Exception:  # noqa: BLE001
        key = (login, path)
    with _GRID_IMG_HASH_LOCK:
        _h = _GRID_IMG_HASH_CACHE.get(key)
        if _h:
            _GRID_IMG_CACHE_STATS["hit"] += 1
            _hit_n = _GRID_IMG_CACHE_STATS["hit"]
            # HIT частый — печатаем каждый 25-й (журнал не засоряем, но видно что кеш живой)
            if _hit_n % 25 == 0:
                try:
                    print(f"[img-cache] HIT total={_hit_n} miss={_GRID_IMG_CACHE_STATS['miss']} "
                          f"{login} {os.path.basename(path)}", flush=True)
                except Exception:  # noqa: BLE001
                    pass
            return _h
    # miss — грузим ВНЕ лока (медленный сетевой вызов не должен блокировать другие воркеры)
    _h = gc_img.upload_image(path)
    if _h:
        with _GRID_IMG_HASH_LOCK:
            _GRID_IMG_HASH_CACHE[key] = _h
            _GRID_IMG_CACHE_STATS["miss"] += 1
            _miss_n = _GRID_IMG_CACHE_STATS["miss"]
        # MISS = реальный аплоад в Яндекс (теперь РЕДКИЙ, 1 раз на картинку аккаунта) — печатаем всегда
        try:
            print(f"[img-cache] MISS->upload total={_miss_n} hit={_GRID_IMG_CACHE_STATS['hit']} "
                  f"{login} {os.path.basename(path)}", flush=True)
        except Exception:  # noqa: BLE001
            pass
    return _h


def _homepage_url(href: str) -> str:
    """Главная сайта (scheme://host) из любого URL объявления — для кнопки «Получить скидку»."""
    m = re.match(r"(https?://[^/]+)", (href or "").strip())
    return m.group(1) if m else ""


def _combo_button(href: str) -> dict | None:
    """Кнопка РСЯ «Получить скидку» → ТА ЖЕ ссылка, что у объявления (Семён: кнопка обязана вести
    туда же, куда объявление, а не на главную). Только для комбинаторных (адаптивных) объявлений
    (GdBannerButton, HAR39). action=GET_DISCOUNT = предустановленный текст «Получить скидку».
    href = ссылка объявления (as-is, чтобы совпадала точь-в-точь). None если href пуст."""
    h = (href or "").strip()
    # href обязан быть абсолютным (scheme://host…) — иначе кнопка Директа отклонит невалидный URL.
    # Относительный/без схемы → кнопку не вешаем (как раньше _homepage_url возвращал '' → она пропускалась).
    return {"action": "GET_DISCOUNT", "href": h} if re.match(r"https?://", h) else None


def _grid_set_ad_prices(login: str, items: list) -> int:
    """Проставить adPrice пачкой на комбинаторные объявления (Grid UpdateAdaptiveTextAds, по куке).
    items: [{id, href, titles, bodies, image_hashes, current, old}]. → число обновлённых.
    Поля inheritableCallouts/inheritableSitelinkSet НЕ шлём — иначе их policy=CLEAR затрёт
    быстрые ссылки/уточнения (проверено: без них ссылки сохраняются)."""
    upd = []
    for it in (items or []):
        if not it.get("current"):
            continue
        _old = _safe_old_price(it.get("current"), it.get("old"))
        _payload = _grid_ad_price_payload(it.get("current"), _old)
        if not _payload:
            continue
        _item = {"href": it.get("href") or "", "hrefParams": "",
                 "titles": it.get("titles") or [], "bodies": it.get("bodies") or [],
                 "imageHashes": it.get("image_hashes") or [], "creativeIds": [],
                 "adPrice": _payload,
                 "id": str(it["id"])}
        upd.append(_item)   # КНОПКУ тут НЕ шлём — ставится отдельным апдейтом (см. _grid_update_adaptive_ads)
    if not upd:
        return 0
    try:
        gc = gf.GridClient(login)
        gc._bootstrap_csrf()
        v = {"updateInput": {"adUpdateItems": upd, "saveDraft": True}}
        r = gc._post("UpdateAdaptiveTextAds", _UPD_ADAPTIVE_Q, v)
        if r.status_code == 403:
            r = gc._post("UpdateAdaptiveTextAds", _UPD_ADAPTIVE_Q, v)
        j = r.json()
        return len(((j.get("data") or {}).get("updateAdaptiveTextAds") or {}).get("updatedAds") or [])
    except Exception:  # noqa: BLE001
        return 0


def _grid_update_adaptive_ads(login: str, items: list[dict]) -> int:
    """Обновить комбинаторные объявления через Grid UpdateAdaptiveTextAds.
    items: [{id, href, titles, bodies, image_hashes?, adPrice?}, ...]
    image_hashes опциональны: если не переданы, существующие картинки не трогаем."""
    upd = []
    for it in (items or []):
        if not it.get("id"):
            continue
        item = {
            "href": it.get("href") or "",
            "hrefParams": "",
            "titles": it.get("titles") or [],
            "bodies": it.get("bodies") or [],
            "creativeIds": [],
            "id": str(it["id"]),
        }
        if "image_hashes" in it and it.get("image_hashes") is not None:
            item["imageHashes"] = list(it.get("image_hashes") or [])
        if it.get("adPrice"):
            item["adPrice"] = it["adPrice"]
        upd.append(item)   # КНОПКА — отдельным апдейтом ПОСЛЕ (изоляция, code-review #4)
    if not upd:
        return 0
    try:
        gc = gf.GridClient(login)
        gc._bootstrap_csrf()
        v = {"updateInput": {"adUpdateItems": upd, "saveDraft": True}}
        r = gc._post("UpdateAdaptiveTextAds", _UPD_ADAPTIVE_Q, v)
        if r.status_code == 403:
            r = gc._post("UpdateAdaptiveTextAds", _UPD_ADAPTIVE_Q, v)
        j = r.json()
        n = len(((j.get("data") or {}).get("updateAdaptiveTextAds") or {}).get("updatedAds") or [])
        _apply_combo_button(gc, upd)   # кнопка ОТДЕЛЬНО: её отказ (напр. чистый Поиск) не роняет картинки/цену
        return n
    except Exception:  # noqa: BLE001
        return 0


def _apply_combo_button(gc, upd_items: list) -> int:
    """Поставить кнопку «Получить скидку» ОТДЕЛЬНЫМ UpdateAdaptiveTextAds — ПОСЛЕ картинок/цены, чтобы
    возможный отказ Grid на кнопке (напр. на чистом Поиске tp2/tp4) НЕ ронял батч с картинками/ценой
    (code-review #4). Полный payload (full-replace) обязателен → копируем поля item + button. Best-effort."""
    btn_items = []
    for it in (upd_items or []):
        b = _combo_button(it.get("href") or "")
        if not b:
            continue
        bi = {k: it[k] for k in ("href", "hrefParams", "titles", "bodies",
                                 "imageHashes", "creativeIds", "adPrice", "id") if k in it}
        bi["button"] = b
        btn_items.append(bi)
    if not btn_items:
        return 0
    try:
        v = {"updateInput": {"adUpdateItems": btn_items, "saveDraft": True}}
        r = gc._post("UpdateAdaptiveTextAds", _UPD_ADAPTIVE_Q, v)
        if r.status_code == 403:
            r = gc._post("UpdateAdaptiveTextAds", _UPD_ADAPTIVE_Q, v)
        j = r.json()
        return len(((j.get("data") or {}).get("updateAdaptiveTextAds") or {}).get("updatedAds") or [])
    except Exception:  # noqa: BLE001 — кнопка best-effort, картинки/цена уже применены
        return 0


_GRID_FEEDS_CNT_Q = ("query Feeds($login:String!$url:String$limitOffset:GdLimitOffsetInput){client(searchBy:{login:$login}){"
                     "feeds(limitOffset:$limitOffset url:$url){defaultFeedId rowset{id name offersCount}}}}")
# Фиды для ЦЕН (по указанию пользователя): у них ЧИСТЫЕ имена офферов «бренд модель …», тогда как
# дефолтный фид часто имеет промо-префикс «-35% renault arkana — за заявку» → split()[0]='-35%' (матч ломался).
_PRICE_FEED_PREFS = ("zabronirovat-01-a", "yandex-used-auto", "yandex-catalog-model-design-custom-name")


def _grid_price_feed(login: str, url: str = "") -> int:
    """Фид аккаунта для ЦЕН (по куке, без баллов): как веб-UI — defaultFeedId по домену (url),
    иначе фид с наибольшим числом офферов. 0 при сбое. url = https://домен аккаунта."""
    try:
        gc = gf.GridClient(login)
        gc._bootstrap_csrf()
        # url → КОРЕНЬ домена (homepage): deep-link (/auto/baic) сузил бы фиды по пути и вернул 0
        # (фиды на корне) → цена не считалась бы. Корень = ВСЕ фиды аккаунта (как при заполнении товаров).
        v = {"login": login, "url": (_homepage_url(url) or None), "limitOffset": {"limit": 1000, "offset": 0}}
        r = gc._post("Feeds", _GRID_FEEDS_CNT_Q, v)
        if r.status_code == 403:
            r = gc._post("Feeds", _GRID_FEEDS_CNT_Q, v)
        fc = ((r.json().get("data") or {}).get("client") or {}).get("feeds") or {}
        if fc.get("defaultFeedId"):
            try:
                return int(fc["defaultFeedId"])
            except (TypeError, ValueError):
                pass
        best, best_n = 0, -1                              # фолбэк: максимум офферов
        for f in (fc.get("rowset") or []):
            try:
                n, fid = int(f.get("offersCount") or 0), int(f["id"])
            except (TypeError, ValueError, KeyError):
                continue
            if n > best_n:
                best, best_n = fid, n
        return best
    except Exception:  # noqa: BLE001
        return 0


def _price_feeds_for(login: str, url: str = "") -> list:
    """ID фидов для ЦЕН в порядке предпочтения пользователя (_PRICE_FEED_PREFS) — у них чистые имена
    офферов. Фолбэк: defaultFeedId / фид с макс. офферов. → [feed_id, ...] (без баллов, по куке)."""
    try:
        gc = gf.GridClient(login)
        gc._bootstrap_csrf()
        # url → КОРЕНЬ домена (homepage): deep-link (/auto/baic) сузил бы фиды по пути и вернул 0
        # (фиды на корне) → цена не считалась бы. Корень = ВСЕ фиды аккаунта (как при заполнении товаров).
        v = {"login": login, "url": (_homepage_url(url) or None), "limitOffset": {"limit": 1000, "offset": 0}}
        r = gc._post("Feeds", _GRID_FEEDS_CNT_Q, v)
        if r.status_code == 403:
            r = gc._post("Feeds", _GRID_FEEDS_CNT_Q, v)
        fc = ((r.json().get("data") or {}).get("client") or {}).get("feeds") or {}
        rows = fc.get("rowset") or []
        # ВСЕ фиды аккаунта с офферами, упорядоченные по «чистоте имени» — мердж в _account_offer_prices
        # берёт МИН-цену, поэтому чистые имена (prefs → yandex-<бренд>) обрабатываются ПЕРВЫМИ и
        # выигрывают на конфликте. Бренды раскиданы по пер-брендовым фидам, а baic/belgee — только
        # в полном «dostup-k-rasprodazhe» → берём фиды ВСЕХ категорий, иначе у части марок нет цены.
        def _rank(name: str) -> int:
            nm = name or ""
            if any(p in nm for p in _PRICE_FEED_PREFS):
                return 0
            if "yandex-" in nm:                           # пер-брендовые фиды (чистые имена)
                return 1
            if any(s in nm for s in ("catalog", "rasprodazh", "dostup", "target")):
                return 2                                  # полный каталог (есть редкие марки)
            return 3
        feeds: list = []
        for f in rows:
            try:
                fid, n = int(f["id"]), int(f.get("offersCount") or 0)
            except (TypeError, ValueError, KeyError):
                continue
            if n > 0:
                feeds.append((_rank(f.get("name") or ""), -n, fid))
        feeds.sort()                                      # rank ↑, затем больше офферов
        ids = [fid for _, _, fid in feeds][:30]           # кап на патологические аккаунты
        if ids:
            return ids
        if fc.get("defaultFeedId"):                       # фолбэк: дефолтный фид домена
            try:
                return [int(fc["defaultFeedId"])]
            except (TypeError, ValueError):
                pass
        return []
    except Exception:  # noqa: BLE001
        return []


_OFFER_PRICE_CACHE: dict = {}                             # (login,url) → (price_map, ts); мердж 30 фидов дорог
_OFFER_PRICE_TTL = 20 * 60


def _account_offer_prices(login: str, url: str = "") -> dict:
    """Объединённая карта цен {ключ: (current, old)} из ВСЕХ фидов аккаунта (мердж; на конфликте —
    самый дешёвый оффер «от X»). Покрывает ВСЕ марки (раньше брался один фид → у baic/belgee цены не
    было). Кэш на аккаунт (TTL 20 мин): мердж до 30 фидов дорог, цены за джобу не меняются. {} при сбое."""
    key = (login, url)
    hit = _OFFER_PRICE_CACHE.get(key)
    if hit and (time.time() - hit[1]) < _OFFER_PRICE_TTL:
        return hit[0]
    out: dict = {}
    for fid in _price_feeds_for(login, url):
        for k, val in _grid_feed_offer_prices(login, fid).items():
            out[k] = _merge_price(out.get(k), val)
    # НЕ кэшируем пустую карту: транзиентный сбой фида/куки (403/таймаут/152) дал бы {} на 20 мин →
    # adPrice не проставился бы на ВСЕ объявления аккаунта. Пустой → следующий вызов перечитает.
    if out:
        _OFFER_PRICE_CACHE[key] = (out, time.time())
    return out


_OFFER_URL_CACHE_ACCT: dict = {}  # (login, url) → (url_map, ts); account-level URL merge


def _account_offer_urls(login: str, url: str = "") -> dict:
    """Объединённая карта targetUrl {ключ: url} из ВСЕХ фидов аккаунта (первый URL на ключ).
    Аналог _account_offer_prices для targetUrl: использует те же _price_feeds_for-ранги.
    Цены мёржились со всех фидов, URL — только с одного → модели без URL в том фиде
    падали на формульный _model_page_href (#Баг-8). Теперь покрытие то же, что у цен.
    Кэш 20 мин (TTL = _OFFER_PRICE_TTL). {} при сбое."""
    key = (login, url)
    hit = _OFFER_URL_CACHE_ACCT.get(key)
    if hit and (time.time() - hit[1]) < _OFFER_PRICE_TTL:
        return hit[0]
    out: dict = {}
    for fid in _price_feeds_for(login, url):
        for k, v in _grid_feed_offer_urls(login, fid).items():
            if k not in out:   # первый фид на ключ (предпочтительные идут первыми по рангу)
                out[k] = v
    if out:
        _OFFER_URL_CACHE_ACCT[key] = (out, time.time())
    return out


def _match_collection(model_name: str, feed_models: dict) -> str | None:
    """Модель группы (из ct, напр. 'Lada Granta') → collectionId фида по совпадению в имени listing
    ('Lada Granta в наличии…'). Сначала полное вхождение, затем по полному набору токенов модели.
    НЕЛЬЗЯ матчить только по последнему слову: это даёт ложные совпадения вроде
    'Changan CS55 Plus' -> 'BAIC U5 Plus' из-за общего хвоста 'plus'. None — нет."""
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^\wа-яё]+", " ", (s or "").lower())).strip()

    def _compact(s: str) -> str:
        return re.sub(r"[^\wа-яё]+", "", (s or "").lower()).strip()

    mn = _norm(model_name or "")
    if not mn or not feed_models:
        return None
    for lname, cid in feed_models.items():
        lname_n = _norm(lname or "")
        if mn and mn in lname_n:
            return cid
    mn_compact = _compact(model_name or "")
    if mn_compact:
        for lname, cid in feed_models.items():
            if mn_compact in _compact(lname or ""):
                return cid
    parts = [p for p in re.split(r"[\s/]+", mn) if p]
    if parts:
        for lname, cid in feed_models.items():
            lname_parts = set(_norm(lname or "").split())
            if all(p in lname_parts for p in parts):
                return cid
    if len(parts) > 1:
        brand = parts[0]
        tail_compact = _compact(" ".join(parts[1:]))
        if tail_compact and (re.search(r"\d", tail_compact) or len(tail_compact) >= 6):
            for lname, cid in feed_models.items():
                lname_n = _norm(lname or "")
                lname_compact = _compact(lname or "")
                if brand in lname_n.split() and tail_compact in lname_compact:
                    return cid
    return None


def _brand_collection_ids(brand_name: str, feed_models: dict) -> list[str]:
    """Все collectionId фида для марки.

    Для ListingAd брендовой группы v501 не принимает фильтр по vendor, поэтому собираем
    список model_* коллекций этой марки и фильтруем по collectionId EQUALS_ANY.
    """
    if not brand_name or not feed_models:
        return []
    brand_c = _brand_canon(re.split(r"[\s/]+", str(brand_name or "").strip().lower())[0])  # кир↔лат канон
    if not brand_c:
        return []
    out: list[str] = []
    for lname, cid in (feed_models or {}).items():
        tokens = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", str(lname or "").lower())
        if tokens and _brand_canon(tokens[0]) == brand_c and cid and cid not in out:
            out.append(str(cid))
    return out


# ── Коллекции фида для фильтра «Страницы каталога» (ListingAd) ──────────────────
# HAR-реверс (porg-24rg6hzy, feed 3501015): фильтр листинга = collectionId (НЕ vendor — vendor даёт
# 5005). Список коллекций тянется Grid-операцией Listings (feeds(filter:{searchBy:$feedId})). Общий
# _grid_feeds возвращает listings УРЕЗАННО (areListingsTruncated), поэтому для резолва нужен этот
# точечный per-feed запрос. Коллекции двухуровневые: бренд-уровень (id числовой, имя «Новые
# автомобили BAIC …») и модельные (id 'model_N', имя «BAIC BJ40 …»).
_FEED_LISTINGS_QUERY = ("query Listings($login:String!$feedId:String!){reqId:getReqId "
                        "client(searchBy:{login:$login}){feeds(limitOffset:{limit:1 offset:0}"
                        "filter:{searchBy:$feedId}){rowset{areListingsTruncated "
                        "listings{id name offerCount}}}}}")
_FEED_COLLECTIONS_CACHE: dict[tuple, list] = {}


def _feed_collections(login: str, feed_id: int, agency: str = "", cookie: str | None = None) -> list[dict]:
    """Коллекции (listings) фида через Grid op Listings. → [{id,name,offers}]. Кэш по (login,feed_id)."""
    if not feed_id:
        return []
    key = (login, int(feed_id))
    if key in _FEED_COLLECTIONS_CACHE:
        return _FEED_COLLECTIONS_CACHE[key]
    out: list[dict] = []
    try:
        import requests as _rqs
        ck = cookie or cmc.pick_working_cookie(
            login, accounts=((agency,) if agency else cmc.DEFAULT_COOKIE_ACCOUNTS))
        if ck:
            csrf = _block_bootstrap(ck, agency)
            headers = {"Cookie": ck, "dna-operation-name": "Listings", "x-direct-api": "1",
                       "x-detected-locale": "ru", "Content-Type": "application/json",
                       "User-Agent": cmc.USER_AGENT}
            if csrf:
                headers["x-csrf-token"] = csrf
            url = f"{_GRID_URL}?operationName=Listings&ulogin={login}"
            payload = {"operationName": "Listings", "query": _FEED_LISTINGS_QUERY,
                       "variables": {"login": login, "feedId": str(int(feed_id))}}
            r = _rqs.post(url, json=payload, headers=headers, timeout=40, verify=False)
            if r.status_code == 403:                       # протух CSRF → один ретрай
                c2 = _grid_csrf(r)
                if c2:
                    headers["x-csrf-token"] = c2
                    r = _rqs.post(url, json=payload, headers=headers, timeout=40, verify=False)
            data = r.json()
            rs = (((data.get("data") or {}).get("client") or {}).get("feeds") or {}).get("rowset") or []
            if rs:
                out = [{"id": str(l.get("id")), "name": (l.get("name") or ""),
                        "offers": l.get("offerCount") or 0}
                       for l in (rs[0].get("listings") or []) if l.get("id")]
    except Exception:  # noqa: BLE001
        out = []
    # Кэшируем ТОЛЬКО непустой результат: пустой/сбойный (протухла кука/152/403/сеть) НЕ сохраняем,
    # иначе один сбой навсегда лишал бы брендовые группы collectionId-фильтра (следующий вызов повторит).
    if out:
        _FEED_COLLECTIONS_CACHE[key] = out
    return out


# Бренд-матч для collectionId-фильтра: план-марка может быть кириллицей (Лада, Москвич, Хавал),
# а коллекция фида — латиницей (Lada, Moskvich, Haval) и наоборот → нужна нормализация к канону.
_RU2LAT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
           "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
           "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
           "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"}
# Явные алиасы марок (любая форма → канон-латиница). Покрывает непрямые случаи, где транслит не
# совпадает (Чери≠cheri, а chery; Бельджи≠belzhi, а belgee и т.п.).
_BRAND_CANON = {
    "лада": "lada", "ваз": "lada", "москвич": "moskvich", "хавал": "haval", "хавейл": "haval",
    "чери": "chery", "чанган": "changan", "хендай": "hyundai", "хёндай": "hyundai", "хундай": "hyundai",
    "киа": "kia", "ниссан": "nissan", "омода": "omoda", "шкода": "skoda", "фольксваген": "volkswagen",
    "бельджи": "belgee", "баик": "baic", "джили": "geely", "джип": "jeep", "джак": "jac",
    "эксид": "exeed", "джетур": "jetour", "джету": "jetour", "газ": "gaz", "уаз": "uaz",
    "тойота": "toyota", "мазда": "mazda", "хонда": "honda", "митсубиси": "mitsubishi", "рено": "renault",
    "пежо": "peugeot", "ситроен": "citroen", "форд": "ford", "вольво": "volvo", "ауди": "audi",
    "ливан": "livan", "гак": "gac", "фав": "faw", "донгфенг": "dongfeng", "танк": "tank",
}


def _brand_canon(tok: str) -> str:
    """Каноническая форма марки (латиница) для сравнения кир↔лат. Сначала явный алиас, затем транслит."""
    t = (tok or "").strip().lower()
    if not t:
        return ""
    if t in _BRAND_CANON:
        return _BRAND_CANON[t]
    return "".join(_RU2LAT.get(ch, ch) for ch in t)


def _brand_in_name(brand: str, name: str) -> bool:
    """Есть ли марка в имени коллекции — с нормализацией кир↔лат (по канону любого токена имени)."""
    bc = _brand_canon(re.split(r"[\s/]+", (brand or "").strip().lower())[0] if brand else "")
    if not bc:
        return False
    for tok in re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", (name or "").lower()):
        if _brand_canon(tok) == bc:
            return True
    return False


def _known_brand_canons() -> set:
    """Канон-набор РЕАЛЬНЫХ марок авто — выводится из ТОГО ЖЕ классификатора, что делит ct на
    Марки/Модели/Общее (_ct_segment_map). Для ct сегмента «Марки» брендом служит само имя ag_part1,
    для «Модели» — его первое слово. Используется как защита helper'ов: vendor/name-фильтр строится
    ТОЛЬКО для реальной марки, не для темы («Автокредит»→avtokredit, «Trade-in»→trade, «Авито»→avito).
    Кэш на процесс. Пустой набор (классификатор/БД недоступны) → helper'ы деградируют ПЕРМИССИВНО."""
    global _BRAND_CANON_SET
    if _BRAND_CANON_SET is not None:
        return _BRAND_CANON_SET
    seg = _ct_segment_map()
    names = _ag_part1_map()
    brands: set = set()
    for ct, s in seg.items():
        nm = (names.get(_gc_ct(ct)) or names.get(ct) or "").strip().lower()
        parts = nm.split()
        if not parts:
            continue
        if s in ("Марки", "Модели"):
            bc = _brand_canon(parts[0])
            if bc:
                brands.add(bc)
    _BRAND_CANON_SET = brands
    return brands


def _is_brand_canon(tok_canon: str) -> bool:
    """Является ли канон токена реальной маркой авто (по _known_brand_canons). При пустом справочнике
    (классификатор недоступен) — пермиссивно True, чтобы не ломать создание настоящих марок."""
    if not tok_canon:
        return False
    known = _known_brand_canons()
    return (not known) or (tok_canon in known)


def _vendor_value(brand: str) -> str | None:
    """Значение vendor-фильтра ТОВАРОВ (ShoppingAd): марка (manufacturer) В РЕГИСТРЕ ФИДА —
    <vendor>Belgee</vendor>/<vendor>Lada</vendor> (HAR42: применённый фильтр = ["Belgee"] с заглавной,
    НЕ ["belgee"]). Латиница марки кодера отдаётся как есть (Belgee/GAC/Lada), кириллица → транслит
    Title-case (Лада→Lada). Защита: только реальная марка (тот же справочник Марки/Модели/Общее);
    тема («Автокредит»/«Trade-in»/«Авито») → None."""
    b = (brand or "").strip()
    if not b:
        return None
    first_raw = re.split(r"[\s/]+", b)[0]
    canon = _brand_canon(first_raw.lower())
    if not canon or not _is_brand_canon(canon):
        return None
    # vendor должен совпадать с фидовым <vendor>: латиница кодера уже в нужном регистре (Belgee, GAC),
    # отдаём как есть; кириллица — латинизируем с заглавной (Лада→Lada).
    return first_raw if re.search(r"[A-Za-z]", first_raw) else canon.title()


def _vendor_filter_values(vendor: str) -> list[str]:
    """Значения для vendor-фильтра товаров (CONTAINS_ANY) во ВСЕХ регистрах фида. Регистр <vendor>
    зависит от фида (HAR42/43: один фид «Belgee», другой «baic»), а CONTAINS_ANY чувствителен к
    регистру → шлём [исходный, нижний, Title], чтобы совпасть с любым написанием. Единый источник:
    тот же набор используется и в add_shopping_ads (создание), и в set_default_text (перезапись)."""
    v = str(vendor or "").strip()
    return list(dict.fromkeys([v, v.lower(), v.title()])) if v else []


def _model_field_values(brand: str, seg: str) -> list[str]:
    """Значения для model-фильтра ТОВАРОВ (Модели-группы): хвост имени БЕЗ марки во всех регистрах
    фида (BAIC X35 → X35; Lada Vesta Седан → Vesta Седан). HAR: <vendor>Lada</vendor><model>Vesta Седан</model>.
    [] если не «Модели» / нет хвоста / не реальная марка. Фид без поля model → add_shopping_ads ретраит без него."""
    if seg != "Модели":
        return []
    b = (brand or "").strip()
    if not b or not _coder_name_real_brand(b):
        return []
    parts = re.split(r"\s+", b, maxsplit=1)
    if len(parts) < 2:
        return []
    tail = parts[1].strip()
    return list(dict.fromkeys([tail, tail.lower(), tail.title()])) if tail else []


def _listing_name_value(brand: str, seg: str) -> str | None:
    """Значение name-фильтра «Страницы каталога» (ListingAd, HAR36): марка (Марки) или марка+модель
    (Модели), нижний регистр латиницей. Имена страниц каталога содержат «Belgee X50 …» → CONTAINS_ANY.
    Защита: первый токен ОБЯЗАН быть реальной маркой (тот же справочник). Тема → None."""
    b = (brand or "").strip()
    if not b:
        return None
    first = _brand_canon(re.split(r"[\s/]+", b.lower())[0])
    if not first or not _is_brand_canon(first):
        return None
    if seg == "Марки":
        return first
    toks = [_brand_canon(t) for t in re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", b.lower())]
    return " ".join(t for t in toks if t) or None


def _brand_level_collection_id(brand_name: str, collections: list[dict]) -> str | None:
    """Бренд-уровневая коллекция фида для марки («Новые автомобили BAIC …», id='25').
    Берём коллекцию с id НЕ 'model_*', в имени которой есть слово марки (кир↔лат нормализация);
    при нескольких — с наибольшим offerCount (полнее). None — не нашли (caller → model_* фолбэк)."""
    if not brand_name or not collections:
        return None
    if not re.split(r"[\s/]+", str(brand_name or "").strip().lower())[0]:
        return None
    cands = []
    for c in collections:
        cid = str(c.get("id") or "")
        if not cid or cid.startswith("model_"):
            continue
        if _brand_in_name(brand_name, c.get("name") or ""):
            cands.append(c)
    if not cands:
        return None
    cands.sort(key=lambda c: -(int(c.get("offers") or 0)))
    return str(cands[0]["id"])


def _feed_models_from_collections(collections: list[dict]) -> dict:
    """Из коллекций фида → {name_lower: id} ТОЛЬКО для модельных (id 'model_*') — совместимо с
    _match_collection / _brand_collection_ids (фолбэк, когда _account_model_feeds фид не нашёл)."""
    out = {}
    for c in (collections or []):
        cid = str(c.get("id") or "")
        nm = (c.get("name") or "").strip().lower()
        if cid.startswith("model_") and nm:
            out[nm] = cid
    return out


def _tp7_product_feed_filters(brand_model: str, ct: str) -> list[dict]:
    """UAC feed_filters для товарной части tp7.

    Для product-объявлений у мастера фильтр задаётся отдельно от listings_feed_filters.
    Используем канонические варианты модели:
      - полное имя из ct/ag_part1;
      - хвост без бренда (например, 'Tiggo 8 Pro Max');
    чтобы товарка не шла по всему фиду.
    """
    base = (brand_model or "").strip()
    if not base or not ct or ct in ("ct0000", "ct0111"):
        return []
    if not _coder_name_real_brand(base):   # общий ст (Авито/Дром/тема) — НЕ марка → фильтр по модели НЕ строим
        return []
    parts = [p for p in re.split(r"\s+", base) if p]
    variants: list[str] = []
    for cand in (base, " ".join(parts[1:]) if len(parts) > 1 else ""):
        cand = re.sub(r"\s+", " ", str(cand or "").strip())
        if cand and len(cand) >= 3 and cand not in variants:
            variants.append(cand)
    if not variants:
        return []
    return [{"conditions": [{"field": "model", "operator": "CONTAINS",
                             "value": json.dumps(variants, ensure_ascii=False)}]}]


# ── Shared минус-набор для tp2/tp4 (TEXT_CAMPAIGN) — канон CODER.md §«Минус» ──────
# Путь ИДЕНТИЧЕН tp1/tp5: взять существующий набор «Минуса общие» из аккаунта через
# v5 negativekeywordsharedsets.get. Если в аккаунте нет ни одного — собрать минусы
# из пака M3 (все ct данного tp, объединить+дедупликация), обрезать по бюджету
# КАМПАНИИ 20 000 символов БЕЗ пробелов (лимит Директа), создать набор.
# Привязка — через v5 campaigns.update (NegativeKeywordSharedSetIds) — для TEXT_CAMPAIGN
# это валидное поле верхнего уровня (в отличие от tp1/tp5 где Grid libraryMinusKeywordsIds).
_MINUS_SET_NAME_MARKER = "Минуса общие"  # маркер имени, как у слепков Щербаковой
# Лимиты Директа, символы БЕЗ пробелов (офиц. дока + v5 ref, см. CODER.md):
_MINUS_SHARED_SET_CHAR_BUDGET = 4_096    # библиотечный набор (negativekeywordsharedsets) — как группа
_MINUS_CAMPAIGN_CHAR_BUDGET = 20_000     # минусы НАПРЯМУЮ на кампании (NegativeKeywords кампании)
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
}


def _collect_pack_minus(slepok: str, site_type: str, tp_code: str) -> list[str]:
    """Собрать ПОЛНЫЙ список минус-фраз из пака M3 для (slepok, site_type, tp_code).

    Обходит все ct-папки пака по данному tp, объединяет {slepok}_minus.txt +
    {slepok}_minus_shared.txt, дедуплицирует (case-insensitive), фильтрует ≤7 слов.
    Возвращает список (не обрезанный по символам).
    """
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    pack = kp.gather(key, site_type, tp_code)  # {ctNNNN: {"minus":[...]}}
    seen: set[str] = set()
    result: list[str] = []
    for ct_data in pack.values():
        for w in (ct_data.get("minus") or []):
            w = re.sub(r"\s+", " ", str(w).strip())
            if not w or len(w.split()) > 7:
                continue
            k = w.lower()
            if k not in seen:
                seen.add(k)
                result.append(w)
    return result


def _minus_char_budget(words: list[str], budget: int = _MINUS_CAMPAIGN_CHAR_BUDGET) -> list[str]:
    """Обрезать список минус-фраз по символьному бюджету (БЕЗ пробелов).

    Директ считает символы каждой фразы без пробелов (официальная дока).
    Добавляем фразы пока сумма не превысит бюджет.
    """
    total, out = 0, []
    for w in words:
        cost = len(w.replace(" ", ""))
        if total + cost > budget:
            break
        total += cost
        out.append(w)
    return out


def _get_or_create_minus_set(token: str, login: str,
                              slepok: str, site_type: str, tp_code: str) -> int | None:
    """Вернуть id shared минус-набора для tp2/tp4 (зеркалит путь tp1/tp5).

    1. Берём существующий набор «Минуса общие» из аккаунта — КАК ДЕЛАЮТ tp1/tp5
       (_tp5_account_data: next(...'Минуса общие'..., msets[0][0])).
       Если есть — возвращаем сразу, без чтения пака.
    2. Если аккаунт пуст (нет ни одного набора) — собираем минусы из пака M3
       (все ct данного tp, объединить+дедуп), обрезаем по 20 000 симв. без пробелов,
       создаём новый набор через v5 negativekeywordsharedsets.add.
    3. None при любой ошибке (не валит создание кампании).
    """
    try:
        jm = _v5_get("negativekeywordsharedsets", token, login, ["Id", "Name"])
        msets = [(s["Id"], s.get("Name") or "")
                 for s in (jm.get("result") or {}).get("NegativeKeywordSharedSets", [])]
        # Путь tp1/tp5: берём набор с «Минуса общие» в имени, иначе первый из списка
        minus_set = next((mid for mid, nm in msets if _MINUS_SET_NAME_MARKER in nm),
                         (msets[0][0] if msets else None))
        if minus_set:
            return minus_set
        # Аккаунт без shared-set: создаём из пака M3
        words = _collect_pack_minus(slepok, site_type, tp_code)
        words = _minus_char_budget(words, _MINUS_SHARED_SET_CHAR_BUDGET)  # набор ≤4096, не 20000
        if not words:
            return None
        j_add = _v5_call("negativekeywordsharedsets", "add", token, login, {
            "NegativeKeywordSharedSets": [{
                "Name": f"{_MINUS_SET_NAME_MARKER} {tp_code}",
                "NegativeKeywords": words,
            }]
        })
        add_res = (j_add.get("result") or {}).get("AddResults", [])
        new_id = (add_res[0].get("Id") if add_res else None)
        return new_id or None
    except Exception:  # noqa: BLE001 — мягкая деградация, не валим кампанию
        return None


def _attach_minus_set_to_text_campaign(token: str, login: str,
                                        campaign_id: int, minus_set_id: int) -> str | None:
    """Привязать shared минус-набор к v5 TEXT_CAMPAIGN через campaigns.update.

    NegativeKeywordSharedSetIds — поле верхнего уровня кампании (не внутри TextCampaign).
    Возвращает None при успехе, текст ошибки при неудаче.
    """
    try:
        j = _v5_call("campaigns", "update", token, login, {
            "Campaigns": [{
                "Id": campaign_id,
                "NegativeKeywordSharedSetIds": {"Items": [int(minus_set_id)]},
            }]
        })
        upd_res = (j.get("result") or {}).get("UpdateResults", [])
        errs = (upd_res[0].get("Errors") or []) if upd_res else []
        if errs:
            return "; ".join(e.get("Message") or e.get("Details") or str(e) for e in errs)
        if "error" in j:
            return _v5_err(j)
        return None
    except Exception as e:  # noqa: BLE001
        return str(e)[:120]


def _apply_campaign_direct_minus(token: str, login: str,
                                  campaign_id: int,
                                  slepok: str, site_type: str, tp_code: str) -> str | None:
    """Повесить минусы campaign-direct (pavlov/kryuchkova) напрямую на кампанию.

    Механизм: campaigns.update с NegativeKeywords: {"Items": [...]}.
    Лимит: ≤20 000 символов без пробелов (NegativeKeywords кампании, офиц. дока).
    Мягкая деградация: при ошибке возвращает текст ошибки, кампанию НЕ откатывает.
    Возвращает None при успехе, строку ошибки при неудаче.
    """
    try:
        words = _collect_pack_minus(slepok, site_type, tp_code)
        words = _minus_char_budget(words, _MINUS_CAMPAIGN_CHAR_BUDGET)  # ≤20 000 симв.
        if not words:
            return "нет минусов в паке (campaign-direct пропущен)"
        j = _v5_call("campaigns", "update", token, login, {
            "Campaigns": [{
                "Id": campaign_id,
                "NegativeKeywords": {"Items": words},
            }]
        })
        upd_res = (j.get("result") or {}).get("UpdateResults", [])
        errs = (upd_res[0].get("Errors") or []) if upd_res else []
        if errs:
            return "; ".join(e.get("Message") or e.get("Details") or str(e) for e in errs)
        if "error" in j:
            return _v5_err(j)
        return None  # успех
    except Exception as e:  # noqa: BLE001
        return str(e)[:120]


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


def _manual_creative_paths(ct_code: str) -> list:
    """Manual-креативы для ct как локальные пути на LXC.

    Источник правды теперь тот же, что и у вкладки «Контент»:
    `kontent_pack` индексирует external_assets из M3 (`/Users/Shared/agency/creatives/Manual/...`),
    а здесь мы лениво скачиваем эти файлы в локальный cache через `fetch_remote_asset`.

    Legacy-fallback `/opt/creatives/Manual/{ct}/` оставлен только если такой mount реально есть.
    """
    import os as _os
    ct = (ct_code or "").strip().lower()
    if not ct:
        return []
    out: list[str] = []

    # 1) Старый локальный mount, если он вообще существует на текущем LXC.
    folder = _os.path.join(MANUAL_CREATIVES_DIR, ct)
    try:
        if _os.path.isdir(folder):
            out.extend(sorted(
                _os.path.join(folder, f)
                for f in _os.listdir(folder)
                if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
            ))
    except Exception:  # noqa: BLE001
        pass

    # 2) Актуальный путь: Manual-ассеты из M3 index -> локальный cache path.
    try:
        idx = kp._load_index() or {}
        ext_key = f"Manual|manual|{ct}"
        rows = (idx.get("external_assets") or {}).get(ext_key) or []
        for rec in rows:
            if str(rec.get("kind") or "") != "image_manual":
                continue
            remote = str(rec.get("remote") or "").strip()
            if not remote:
                continue
            local = kp.fetch_remote_asset(remote)
            if local:
                out.append(local)
    except Exception:  # noqa: BLE001
        pass

    return sorted(dict.fromkeys(out))


# ── Анти-блок: широкая структура = десятки-сотни групп на кампанию. Пофайловый цикл
#    (3 вызова/группу) = сотни запросов → риск 429/блокировки. Лечим БАТЧИНГОМ (один
#    adgroups.add берёт до 1000 групп) + паузами между пачками + капом групп за проход.
_AC_GROUP_CAP = 150           # макс. групп на кампанию за один проход (остальное → deferred)
_AC_CHUNK_AG = 100            # групп в одном adgroups.add
_AC_CHUNK_KW = 1000           # ключей в одном keywords.add
_AC_CHUNK_AD = 100            # объявлений в одном ads.add
_AC_BATCH_SLEEP = 0.4         # пауза между батч-вызовами (троттл, сек)
# Комбинаторное объявление (RESPONSIVE_AD) — замена ТГО (TextAd), которое отключают с 30.06.2026.
# Создаётся ТОЛЬКО через v501 ads.add {ResponsiveAd:{Titles[],Texts[],Href,AdImageHashes[],...}}.
# Несколько заголовков/текстов в ОДНОМ объявлении (Яндекс комбинирует). Уточнения наследуются
# на уровне группы/кампании (поле AdExtensions у ResponsiveAd НЕ поддерживается).
_RA_TITLE_MAX = 56            # лимит длины заголовка
_RA_TEXT_MAX = 81            # лимит длины текста
_RA_TITLES_CAP = 7           # макс. заголовков в комбинаторном (как в UI Директа «… из 7»)
_RA_TEXTS_CAP = 3            # макс. текстов в комбинаторном (Яндекс: Texts от 1 до 3 — 5 = ошибка ads.add)


def _dedup_cap(items, maxlen: int, cap: int) -> list:
    """Обрезать по длине, выкинуть пустые/дубли, ограничить количеством. Дедуп — по
    НОРМАЛИЗОВАННОМУ ключу (_variant_norm_key: числа схлопнуты), чтобы «…стоянку - 45%» и
    «…стоянку - 40%» не уходили оба в одно объявление (Комбинаторное: ≤5 заголовков / ≤3 текста)."""
    out: list = []
    seen: set = set()
    for it in items or []:
        s = (str(it) or "").strip()[:maxlen]
        if not s:
            continue
        k = _variant_norm_key(s)
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
        if len(out) >= cap:
            break
    return out


def _combo_fill_titles(items: list, cap: int = _RA_TITLES_CAP) -> list:
    """Добор заголовков ResponsiveAd до 7 без ломания бренда/модели в первом заголовке."""
    src = [str(x or "").strip() for x in (items or []) if str(x or "").strip()]
    out = list(src)
    anchor = (src[0].split(".")[0].strip() if src else "Авто в кредит").rstrip(" ,.")
    if len(anchor) > 34:
        sp = anchor[:34].rfind(" ")
        anchor = anchor[:sp if sp > 15 else 34].rstrip(" ,.")
    tails = [
        "Кредит от 9 000 ₽/мес",
        "Одобрение за 30 минут",
        "КАСКО в подарок",
        "Трейд-ин выше рынка",
        "Первый взнос 0 ₽",
        "Господдержка на авто",
        "15 банков-партнеров",
        "Авто в наличии",
    ]
    for tail in tails:
        if len(out) >= cap:
            break
        cand = f"{anchor}. {tail}" if anchor else tail
        if len(cand) > _RA_TITLE_MAX:
            cand = f"{anchor} {tail}"
        if len(cand) > _RA_TITLE_MAX:
            cand = cand[:_RA_TITLE_MAX].rsplit(" ", 1)[0].rstrip(" ,.")
        # Правило Семёна: свободно ≤8 симв. Если кандидат короче hi-8 — добиваем хвостами.
        if cand and len(cand) < _RA_TITLE_MAX - 8:
            cand = _fill_title(cand, _RA_TITLE_MAX - 8, _RA_TITLE_MAX)
        if cand and cand not in out:
            out.append(cand)
    return out


def _combo_fill_texts(items: list, cap: int = _RA_TEXTS_CAP) -> list:
    """Добор текстов ResponsiveAd до 3, с разными УТП и длиной до 81.
    #26: используем _GENERIC_TEXT_FILLERS (76-81 симв) вместо коротких строк;
    _trim_to_word вместо [:max].rsplit — не срезает последнее слово у коротких текстов."""
    out = [str(x or "").strip() for x in (items or []) if str(x or "").strip()]
    for cand in _GENERIC_TEXT_FILLERS:
        if len(out) >= cap:
            break
        cand = _trim_to_word(cand, _RA_TEXT_MAX).rstrip(" ,.")
        if cand and cand not in out:
            out.append(cand)
    return out[:cap]   # seed из items мог уже быть >cap — держим контракт (ads.add: Texts ≤3)


def _credit_title_bucket(s: str) -> str:
    x = (s or "").lower()
    if re.search(r"9\s*000|/мес|плат[её]ж", x):
        return "payment"
    if re.search(r"0\s*₽|0\s*руб|первый\s+взнос", x):
        return "first_payment"
    if re.search(r"каско|1\s*год", x):
        return "kasko"
    if re.search(r"30\s*мин|одобр", x):
        return "approval"
    if re.search(r"15\s*банк|банк", x):
        return "banks"
    if re.search(r"45\s*%|скидк|выгод", x):
        return "discount"
    if re.search(r"150\s*%|трейд", x):
        return "tradein"
    if re.search(r"2026|госпрограмм|господдерж", x):
        return "state"
    if re.search(r"1\s*мин|заявк", x):
        return "apply"
    return "other"


def _credit_title_anchor(items: list[str]) -> tuple[str, str]:
    first = str((items or [""])[0] or "").strip()
    anchor = (first.split(".")[0].strip() if first else "Авто в кредит").rstrip(" ,.")
    anchor = re.sub(r"(?i)^(кредит\s+на|купить)\s+", "", anchor).strip()
    anchor = re.sub(r"(?i)\s+в\s+кредит\b", "", anchor).strip()
    brand = anchor
    m = re.match(r"(.+?)\s+в\s+[А-ЯA-ZЁ0-9]", anchor)
    if m:
        brand = m.group(1).strip()
    brand = brand.rstrip(" ,.")
    return anchor, brand or anchor


def _valid_pack_brand_name(ct: str, raw_name: str) -> str:
    name = str(raw_name or "").strip()
    low = name.lower()
    if (ct or "").strip().lower() == "ct0000":
        return ""
    if not name:
        return ""
    if low.startswith("кластер запросов не определен") or low == "полное отсутствие ключей":
        return ""
    if not _coder_name_real_brand(name):   # «Авито»/«Дром»/«Автосалон» (сегмент Общее) — НЕ марка:
        return ""                          # иначе _brand_text_set лепит «Купить Авито в кредит» в тексты
    return name


def _pack_group_display_name(ct: str, raw_name: str, brand: str = "") -> str:
    """Человекочитаемый суффикс имени группы. Для общих ct бренд остаётся пустым
    (чтобы не попасть в тексты/фильтры), но в имени группы показываем тему."""
    b = str(brand or "").strip()
    if b:
        return b
    c = (ct or "").strip().lower()
    name = str(raw_name or "").strip()
    low = name.lower()
    if c == "ct0000" or low == "полное отсутствие ключей":
        return "Общая"
    if low.startswith("кластер запросов не определен"):
        return "Общая"
    return name or "Общая"


def _trim_ad_line(s: str, maxlen: int) -> str:
    s = str(s or "").strip()
    if len(s) <= maxlen:
        return s
    cut = s[:maxlen]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.")


def _needs_credit_title_upgrade(items: list[str]) -> bool:
    seq = [str(x or "").strip() for x in (items or []) if str(x or "").strip()]
    if not seq:
        return True
    buckets = {_credit_title_bucket(x) for x in seq}
    first_words = [x.split()[0].lower().rstrip(".,!?") for x in seq if x.split()]
    same_prefix = max((first_words.count(w) for w in set(first_words)), default=0)
    missing_numbers = sum(1 for x in seq if not re.search(r"\d", x))
    return len(buckets - {"other"}) < 5 or same_prefix >= 4 or missing_numbers > 0


def _upgrade_credit_titles(items: list[str], cap: int = _RA_TITLES_CAP) -> list[str]:
    seq = [str(x or "").strip() for x in (items or []) if str(x or "").strip()]
    if not _needs_credit_title_upgrade(seq):
        return seq[:cap]
    anchor, brand = _credit_title_anchor(seq)
    brand_low = (brand or "").strip().lower()
    if not brand or brand_low.startswith("авто"):
        variants = [
            "Новые авто в кредит. Первый взнос 0 ₽",
            "Купить новое авто. КАСКО на 1 год бесплатно",
            "Платеж от 9 000 ₽/мес. Новые авто в наличии",
            "Одобрение за 30 минут онлайн. Новые авто",
            "Кредит от 15 банков онлайн. Подбор авто",
            "Выгода до 45% на новые авто. Узнайте условия",
            "Трейд-ин до 150% цены авто. Оценка онлайн",
            "Госпрограмма 2026. Кредит на новые авто",
        ]
    else:
        variants = [
            f"Кредит на {anchor}. Первый взнос 0 ₽",
            f"Купить {anchor}. КАСКО на 1 год бесплатно",
            f"Платеж от 9 000 ₽/мес. {anchor}",
            f"Одобрение за 30 минут онлайн. {anchor}",
            f"Кредит от 15 банков онлайн. {anchor}",
            f"Выгода до 45% при покупке. {anchor}",
            f"Трейд-ин до 150% цены авто. {anchor}",
            f"Госпрограмма 2026 и кредит. {brand}",
            f"Заявка на кредит за 1 минуту. {brand}",
        ]
    out: list[str] = []
    seen: set[str] = set()
    for cand in variants + seq:
        line = _trim_ad_line(cand, _RA_TITLE_MAX)
        if not line:
            continue
        low = line.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(line)
        if len(out) >= cap:
            break
    return out


def _upgrade_credit_texts(items: list[str], cap: int = _RA_TEXTS_CAP) -> list[str]:
    seq = [str(x or "").strip() for x in (items or []) if str(x or "").strip()]
    anchor, brand = _credit_title_anchor(seq or ["Авто в кредит"])
    brand_low = (brand or "").strip().lower()
    if not brand or brand_low.startswith("авто"):
        variants = [
            "Новые авто в кредит. Первый взнос 0 ₽. КАСКО на 1 год. Оставьте заявку.",
            "Платеж от 9 000 ₽/мес. Одобрение за 30 минут. Подберем условия от 15 банков.",
            "Выгода до 45% на новые авто. Трейд-ин до 150% цены автомобиля. Узнайте условия.",
        ]
    else:
        variants = [
            f"Кредит на {brand}. Первый взнос 0 ₽. КАСКО на 1 год. Заявка онлайн.",
            f"{anchor}. Платеж от 9 000 ₽/мес. Одобрение за 30 минут. Узнайте условия.",
            f"{brand} в кредит от 15 банков. Трейд-ин до 150% цены авто. Выберите авто.",
        ]
    out: list[str] = []
    seen: set[str] = set()
    for cand in seq + variants:
        line = _trim_ad_line(cand, _RA_TEXT_MAX)
        if not line:
            continue
        low = line.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(line)
        if len(out) >= cap:
            break
    if len(out) < cap:
        for cand in variants:
            line = _trim_ad_line(cand, _RA_TEXT_MAX)
            low = line.lower()
            if line and low not in seen:
                seen.add(low)
                out.append(line)
            if len(out) >= cap:
                break
    return out[:cap]


def _responsive_ad(titles, texts, href: str, image_hashes=None, display_path: str = "") -> dict | None:
    """Собрать ResponsiveAd (Комбинаторное): несколько заголовков + текстов в одном объявлении.
    → dict для ads.add ИЛИ None, если нет обязательных Titles/Texts/Href."""
    # #5/#6 Когерентность скидок в ОДНОМ объявлении (заголовки+тексты): одно ₽/%-значение (эталон —
    # самое частое) → заголовок и текст согласованы, почти-дубли с разными суммами схлопнутся дедупом.
    titles, texts = _coherent_discounts(list(titles or []), list(texts or []))
    titles = _upgrade_credit_titles(list(titles or []), _RA_TITLES_CAP)
    texts = _upgrade_credit_texts(list(texts or []), _RA_TEXTS_CAP)
    t = _dedup_cap(_combo_fill_titles(titles), _RA_TITLE_MAX, _RA_TITLES_CAP)
    x = _dedup_cap(_combo_fill_texts(texts), _RA_TEXT_MAX, _RA_TEXTS_CAP)
    if len(t) < _RA_TITLES_CAP:
        t = _dedup_cap(t + _combo_fill_titles(t), _RA_TITLE_MAX, _RA_TITLES_CAP)
    if len(x) < _RA_TEXTS_CAP:
        x = _dedup_cap(x + _combo_fill_texts(x), _RA_TEXT_MAX, _RA_TEXTS_CAP)
    if not (t and x and href):
        return None
    ad: dict = {"Titles": t, "Texts": x, "Href": href}
    imgs = [h for h in (image_hashes or []) if h]
    if imgs:
        # v501 ResponsiveAd expects raw JSON array of hashes.
        ad["AdImageHashes"] = imgs[:5]
    if display_path:
        ad["DisplayUrlPath"] = display_path[:20]
    return ad


def _responsive_image_hashes(ra: dict | None) -> list[str]:
    """Return ResponsiveAd image hashes from either v501 payload or legacy raw-list payload."""
    val = (ra or {}).get("AdImageHashes")
    if isinstance(val, dict):
        return [h for h in (val.get("Items") or []) if h]
    if isinstance(val, list):
        return [h for h in val if h]
    return []


def _responsive_retry_items(items: list[dict], *, drop_sitelinks: bool = False,
                            drop_images: bool = False) -> list[dict]:
    out = []
    for it in items or []:
        it2 = {"AdGroupId": it.get("AdGroupId"), "ResponsiveAd": dict(it.get("ResponsiveAd") or {})}
        if drop_sitelinks:
            it2["ResponsiveAd"].pop("SitelinkSetId", None)
        if drop_images:
            it2["ResponsiveAd"].pop("AdImageHashes", None)
        out.append(it2)
    return out
_CALLOUT_POOL_CAP = 200       # макс. уникальных «Уточнений» (AdExtensions) на проход
# Автотаргетинг в v5: спецключ "---autotargeting" в группе (вместо реальных ключей). НЕ ресурс
# relevancematch (его в v5 нет — 404). Проверено live на боевом аккаунте porg-36k7btt7.
_AUTOTARGET_KW = "---autotargeting"


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _normalize_callout_text(text: str) -> str:
    """Нормализовать уточнение до создания ассета Директа."""
    s = str(text or "").strip()
    # В кредитных автосвязках нужен КАСКО; ОСАГО в уточнениях было ошибкой контента.
    s = re.sub(r"(?i)\bосаго\b", "КАСКО", s)
    # Исправление типичных M3-опечаток (LLM иногда роняет букву):
    # «3 латежа» → «3 платежа»  (пропущена «п» в «платеж»)
    s = re.sub(r"(?i)\bлатеж",
               lambda m: ("П" if m.group()[0].isupper() else "п") + "латеж", s)
    # «3 платеда» → «3 платежа»  (опечатка «д» вместо «ж»)
    s = re.sub(r"(?i)\bплатед",
               lambda m: ("П" if m.group()[0].isupper() else "п") + "латеж", s)
    # «Рапродаем/рапродаём» → «Распродаем/распродаём»  (пропущена «с» в «распродаж»)
    s = re.sub(r"(?i)\bрапрода",
               lambda m: ("Р" if m.group()[0].isupper() else "р") + "аспрода", s)
    return s[:_CALLOUT_MAX_EACH].strip()


def _callout_semantic_key(text: str) -> str:
    """Смысловой ключ уточнения: не даём двум УТП одного смысла пройти как разные строки.
    Нормализует ценовые «<кредит/платёж> от N р/мес» (любая сумма → один ключ) и
    «освобождаем склад/склады/стоянку -45%» (стемминг склад*/стоянк* + нормализация
    дефисов/двоеточий «-45% / --45% / -45:» → один ключ). Иначе десятки почти-дублей с разной
    цифрой/окончанием уходят как «разные»."""
    s = str(text or "").lower().replace("ё", "е")
    s = re.sub(r"[-–—]+", "-", s)                    # любые тире (--/–/—) → один дефис
    # «Освобождаем склад/склады/стоянку -45% / --45% / -45:» → один ключ (стемминг + дефис/двоеточие)
    if re.search(r"освобожда", s) and re.search(r"(склад|стоянк|сток)", s):
        return "free_stock"
    if "шин" in s or "резин" in s:
        return "tires"
    if "каско" in s:
        return "kasko"
    # «Платеж/взнос от N р/мес» — уже схлопывались по слову; ценовой «автокредит от N р/мес» — нет.
    if "платеж" in s:
        return "payment"
    if "взнос" in s:
        return "first_payment"
    if "трейд" in s:
        return "tradein"
    if "одобр" in s:
        return "approval"
    # «Автокредит/кредит от N руб/мес» (любая сумма) → один смысловой ключ. НЕ трогаем
    # «кредит от 15 банков» (нет руб/мес → остаётся отдельным офером).
    if (re.search(r"\b(авто)?кредит\b", s) and re.search(r"\bот\b", s)
            and re.search(r"(руб|р\s*/?\s*мес|₽|/\s*мес|в\s*мес)", s)):
        return "credit_monthly"
    return re.sub(r"\s+", " ", s).strip()


# Разумный максимум показываемых уточнений на кампанию: Яндекс выводит ограниченное число,
# десятки почти-дублей бессмысленны. Пул AdExtensions на аккаунт — отдельный кап (_CALLOUT_POOL_CAP).
_CALLOUT_PER_CAMPAIGN_CAP = 8


def _dedup_callouts(texts, cap: int = _CALLOUT_PER_CAMPAIGN_CAP) -> list:
    """Семантический дедуп уточнений + кап. Один смысловой ключ (_callout_semantic_key) → одно
    уточнение; ценовые «от N р/мес» и склад/склады/стоянку схлопываются. Возвращает ≤cap строк
    (нормализованных через _normalize_callout_text). Разные смысловые оферы — сохраняются."""
    seen: set = set()
    out: list = []
    for t in texts or []:
        t = _normalize_callout_text(t)
        if not t:
            continue
        k = _callout_semantic_key(t)
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
        if len(out) >= cap:
            break
    return out


def _dedup_callout_ids(co_map: dict, cap: int = 8) -> list:
    """Нормализовать тексты из {text: id}, семантически дедупить, вернуть ≤cap id.
    Предотвращает «ОСАГО» вместо «КАСКО» и два «шины»/«шиномонтаж» в одном наборе (#24)."""
    norm_to_id: dict = {}
    for t, cid in (co_map or {}).items():
        nt = _normalize_callout_text(str(t))
        if nt and nt not in norm_to_id:
            norm_to_id[nt] = cid
    clean_texts = _dedup_callouts(list(norm_to_id), cap=cap)
    return [norm_to_id[t] for t in clean_texts if t in norm_to_id]


def _ensure_callout_exts(token: str, login: str, texts: list) -> dict:
    """Создать «Уточнения» (Callout AdExtensions) для уникальных текстов → {text: ext_id}.
    Дедуп (регистронезависимо), ≤25 симв., кап пула. Упавшие молча пропускаем (callouts необяз.)."""
    clean = _dedup_callouts(texts, cap=_CALLOUT_POOL_CAP)   # единый семантический дедуп + кап пула
    pool: dict = {}
    for chunk in _chunks(clean, 50):
        j = _v5_call("adextensions", "add", token, login,
                     {"AdExtensions": [{"Callout": {"CalloutText": t}} for t in chunk]})
        res = (j.get("result") or {}).get("AddResults", [])
        for t, r in zip(chunk, res):
            if r.get("Id"):
                pool[t] = r["Id"]
        time.sleep(_AC_BATCH_SLEEP)
    return pool


def _build_tp2_adgroups(token: str, login: str, campaign_id: int,
                        region_ids: list, groups: list,
                        feed_id: int = 0, with_shopping: bool = False,
                        apply_group_minus: bool = True,
                        autotarget: bool = False) -> dict:
    """Наполнить Поисковую (tp2) / tp5 группами БАТЧЕМ: adgroups.add → keywords.add → ads.add(TextAd).

    groups: [{name, keywords:[], minus:[], title, text, href, title2?, callout_ext_ids?}].
    feed_id + with_shopping (tp5 «Товарная галерея», как в слепках): дополнительно к TextAd в каждую
    группу — ListingAd (динамика) + ShoppingAd (товарное) по фиду → состав «Т+Л+ТОВ». Проверено live:
    v5 TEXT-кампания принимает ShoppingAd. Аддитивно: нет фида/флага → только TextAd (старое поведение).
    apply_group_minus: если False — групповые минусы НЕ вешаются (для campaign/shared_set слепков, где
    минусы уходят на уровень кампании, а не групп — как в реальных аккаунтах pavlov/kryuchkova/scherbakova).
    Анти-блок: операции идут пачками (см. _AC_CHUNK_*) с паузами, групп ≤ _AC_GROUP_CAP за проход.
    Кампания остаётся черновиком (State=OFF из оболочки). Лимиты Директа: ключей ≤200/группу,
    минус ≤4096 симв. без пробелов/группу (terehov), Title ≤56, Title2 ≤30, Text ≤81, уточнений ≤4/объявление.
    → {adgroups, keywords, ads, errors, deferred}."""
    rep = {"adgroups": 0, "keywords": 0, "ads": 0, "images_uploaded": 0, "errors": [], "deferred": 0}
    rids = [int(r) for r in (region_ids or []) if str(r).lstrip("-").isdigit()] or [225]
    if len(groups) > _AC_GROUP_CAP:                       # кап за проход (анти-блок)
        rep["deferred"] = len(groups) - _AC_GROUP_CAP
        groups = groups[:_AC_GROUP_CAP]

    # ── Фаза 0: картинки НЕ грузим через v501 заранее.
    # Живой баг 2026-06-28: массовый upload_image на token-path мог подвешивать создание кампании
    # до ads.add, в итоге кампания появлялась с adgroups, но без объявлений. Боевой fallback теперь
    # такой: ResponsiveAd создаём сразу, а image hashes добиваем post-create через Grid/куки
    # (_grid_update_adaptive_ads + GridClient.upload_image) по фактическим ad_id.

    # ── Фаза 1: adgroups.add пачками; ag_ids[i] выровнен по индексу группы (AddResults в порядке входа)
    specs = []
    for g in groups:
        ag = {"Name": (g.get("name") or "группа")[:255], "CampaignId": int(campaign_id), "RegionIds": rids,
              "TrackingParams": _UTM_TEMPLATE_TP1}      # #2 UTM на уровне группы (tp2/tp5 Поиск, v5 — проверено LIVE)
        if apply_group_minus:
            # Групповые минусы: обрезка по символьному бюджету 4096 (≤4096 симв. без пробелов/группу,
            # как у terehov — полный список без cap=100). Для campaign/shared_set слепков apply_group_minus=False.
            minus = _minus_char_budget(g.get("minus") or [], _MINUS_SHARED_SET_CHAR_BUDGET)
            if minus:
                ag["NegativeKeywords"] = {"Items": minus}
        specs.append(ag)
    ag_ids = [None] * len(groups)
    idx = 0
    for chunk in _chunks(specs, _AC_CHUNK_AG):
        ja = _v5_call("adgroups", "add", token, login, {"AdGroups": chunk})
        if "error" in ja:
            rep["errors"].append(f"adgroups.add {_v5_err(ja)}")
            idx += len(chunk)
            time.sleep(_AC_BATCH_SLEEP)
            continue
        for r in (ja.get("result") or {}).get("AddResults", []):
            errs = r.get("Errors") or []
            if r.get("Id") and not errs:
                ag_ids[idx] = r["Id"]
                rep["adgroups"] += 1
            else:
                nm = groups[idx].get("name", "?") if idx < len(groups) else "?"
                rep["errors"].append(f"{nm}: adgroup " + ("; ".join(e.get("Message", "") for e in errs) or "нет Id"))
            idx += 1
        time.sleep(_AC_BATCH_SLEEP)

    # ── Фаза 2: keywords.add пачками (≤200/группу, до _AC_CHUNK_KW items за вызов)
    # autotarget=True → вместо реальных ключей вешаем спецключ "---autotargeting" (1 на группу) —
    # это и есть автотаргетинг в v5 (проверено live на боевом аккаунте porg-36k7btt7).
    kw_items = []
    for i, g in enumerate(groups):
        if not ag_ids[i]:
            continue
        if autotarget:
            kw_items.append({"Keyword": _AUTOTARGET_KW, "AdGroupId": int(ag_ids[i])})
            continue
        for k in _kw_clean(g.get("keywords") or [], 200):
            kw_items.append({"Keyword": k, "AdGroupId": int(ag_ids[i])})
    for chunk in _chunks(kw_items, _AC_CHUNK_KW):
        jk = _v5_call("keywords", "add", token, login, {"Keywords": chunk})
        if "error" not in jk:
            rep["keywords"] += sum(1 for r in (jk.get("result") or {}).get("AddResults", []) if r.get("Id"))
        else:
            rep["errors"].append(f"keywords.add {_v5_err(jk)}")
        time.sleep(_AC_BATCH_SLEEP)

    # ── Фаза 3: ads.add пачками — КОМБИНАТОРНОЕ объявление (ResponsiveAd) через v501.
    # Замена ТГО (TextAd, отключают с 30.06): несколько заголовков/текстов в одном объявлении.
    # Уточнения наследуются на уровне группы/кампании (AdExtensions у ResponsiveAd нет).
    ad_items = []
    ad_meta = []   # параллельно ad_items — для adPrice из фида (#2)
    _acc_url = ""
    for i, g in enumerate(groups):
        if not ag_ids[i]:
            continue
        href = g.get("href") or ""
        if href and not _acc_url:
            _acc_url = re.sub(r"(https?://[^/]+).*", r"\1", href)   # https://домен — для выбора фида цен
        img_paths = g.get("image_paths") or ([g.get("image_path")] if g.get("image_path") else [])
        ra = _responsive_ad(g.get("titles") or [g.get("title"), g.get("name")],
                            g.get("texts") or [g.get("text")], href,
                            image_hashes=None)
        if not ra:
            rep["errors"].append(f"{g.get('name', '?')}: пропущено объявление (нет заголовков/текстов/href)")
            continue
        ad_items.append({"AdGroupId": int(ag_ids[i]), "ResponsiveAd": ra})
        ad_meta.append({"brand": g.get("brand") or g.get("name") or "", "href": href,
                        "seg": _ct_segment(g.get("ct") or ""),   # 'Марки' → цена = МИН по марке
                        "titles": ra.get("Titles") or [], "bodies": ra.get("Texts") or [],
                        "image_hashes": [],
                        "image_paths": img_paths[:5]})
    created_ad_meta = []
    repair_items: list[dict] = []
    _base = 0
    for chunk in _chunks(ad_items, _AC_CHUNK_AD):
        jd = _v501_svc("ads", "add", token, login, {"Ads": chunk})
        used_retry = ""
        if "error" in jd:
            for retry_name, retry_chunk in (
                ("без быстрых ссылок", _responsive_retry_items(chunk, drop_sitelinks=True)),
                ("без быстрых ссылок и картинок", _responsive_retry_items(chunk, drop_sitelinks=True, drop_images=True)),
            ):
                jd2 = _v501_svc("ads", "add", token, login, {"Ads": retry_chunk})
                if "error" not in jd2:
                    jd = jd2
                    used_retry = retry_name
                    rep.setdefault("warnings", []).append(f"ads.add(tp1 ResponsiveAd): retry {retry_name}")
                    break
        if "error" not in jd:
            for k, r in enumerate((jd.get("result") or {}).get("AddResults", [])):
                if r.get("Id"):
                    rep["ads"] += 1
                    gi = _base + k
                    if gi < len(ad_meta):
                        created_ad_meta.append((int(r["Id"]), ad_meta[gi]))
                for e in (r.get("Errors") or []):
                    rep["errors"].append(f"ResponsiveAd: {e.get('Message')} {e.get('Details','')}".strip())
        else:
            rep["errors"].append(f"ads.add(ResponsiveAd) {_v5_err(jd)}")
        _base += len(chunk)
        time.sleep(_AC_BATCH_SLEEP)

    # Фаза 3.4: post-create repair через Grid/куки для token-path.
    # Даже если v501 ads.add прошло, live payload в Директе может схлопнуться до 2 заголовков /
    # 1-2 картинок. Поэтому после создания всегда добиваем фактическое объявление через Grid:
    # titles + bodies + imageHashes. Это и есть fallback «если токены недогрузили — догружаем по куки».
    if created_ad_meta:
        try:
            import os as _os3
            _gc_img = gf.GridClient(login)
            _uploaded_by_name: dict[str, str] = {}
            _upd_items = []
            for ad_id, meta in created_ad_meta:
                _hashes = list(dict.fromkeys(meta.get("image_hashes") or []))
                for _pth in (meta.get("image_paths") or []):
                    if len(_hashes) >= 5:
                        break
                    if not _pth or not _os3.path.isfile(_pth):
                        continue
                    _bn = _os3.path.basename(_pth)
                    _h = _uploaded_by_name.get(_bn)
                    if not _h:
                        _h = _cached_upload_image(_gc_img, login, _pth)
                        if _h:
                            _uploaded_by_name[_bn] = _h
                    if _h and _h not in _hashes:
                        _hashes.append(_h)
                if _hashes:
                    meta["image_hashes"] = _hashes[:5]
                _upd = {"id": ad_id, "href": meta["href"], "titles": meta["titles"], "bodies": meta["bodies"]}
                if meta.get("image_hashes") is not None:
                    _upd["image_hashes"] = meta.get("image_hashes") or []
                _upd_items.append(_upd)
            if _upd_items:
                repair_items = list(_upd_items)
                rep["ads_repaired"] = _grid_update_adaptive_ads(login, _upd_items)
                rep["image_groups"] = sum(1 for _it in _upd_items if _it.get("image_hashes"))
        except Exception as _e:  # noqa: BLE001
            rep.setdefault("warnings", []).append(f"tp2/tp4 repair: {str(_e)[:100]}")

    # Фаза 3.5: ЦЕНА из фида в комбинаторное (#2) — Grid по куке. adPrice по бренду/модели группы.
    # Цены берём из предпочтительных фидов (чистые имена офферов), а не из дефолтного (промо-префикс).
    try:
        _pmap = _account_offer_prices(login, _acc_url)
        if _pmap and created_ad_meta:
            _pitems = []
            for ad_id, meta in created_ad_meta:
                cur, old = _group_ad_price(_pmap, meta.get("brand", ""), meta.get("seg", ""))
                if cur:
                    _pitems.append({"id": ad_id, "href": meta["href"], "titles": meta["titles"],
                                    "bodies": meta["bodies"], "image_hashes": meta["image_hashes"],
                                    "current": cur, "old": old})
            rep["prices_set"] = _grid_set_ad_prices(login, _pitems)
            if repair_items:
                rep["ads_repaired_after_price"] = _grid_update_adaptive_ads(login, repair_items)
    except Exception as _e:  # noqa: BLE001
        rep.setdefault("warnings", []).append(f"adPrice: {str(_e)[:100]}")

    # ── Фаза 4 (tp5): товарные по фиду — ListingAd (динамика) + ShoppingAd (товарное) в каждую группу.
    # Состав «Т+Л+ТОВ» как в слепках. v501 add_listing_ad/add_shopping_ad (FeedId в объявлении).
    if feed_id and with_shopping:
        rep["listing_ads"], rep["shopping_ads"] = 0, 0
        v501c = cmc.DirectV501Client(token, login)
        v501c.sess.headers.update({"Authorization": f"Bearer {token}"})
        for i in range(len(groups)):
            if not ag_ids[i]:
                continue
            try:
                if v501c.add_listing_ad(int(ag_ids[i]), int(feed_id)):
                    rep["listing_ads"] += 1
            except Exception as e:  # noqa: BLE001 — товарные не критичны, TextAd уже создан
                rep["errors"].append(f"{groups[i].get('name','?')}: listing_ad {str(e)[:80]}")
            try:
                if v501c.add_shopping_ad(int(ag_ids[i]), int(feed_id)):
                    rep["shopping_ads"] += 1
            except Exception as e:  # noqa: BLE001
                rep["errors"].append(f"{groups[i].get('name','?')}: shopping_ad {str(e)[:80]}")
            time.sleep(_AC_BATCH_SLEEP)
    return rep


def _struct_cts(slepok: str, site_type: str, tp_code: str) -> list:
    """Список модель-ct для (слепок, тип сайта, tp_code) из структуры (формат groups+gc).
    Грубый формат (splits без gc) → []."""
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    struct = _json("slepki_structure.json").get("directologists", [])
    d = next((x for x in struct if x.get("key") == key), None)
    if not d:
        return []
    st = next((s for s in d.get("site_types", []) if s.get("name") == site_type), None)
    if not st:
        return []
    cts, seen = [], set()
    for tp in st.get("tp", []):
        if tp.get("code") != tp_code:
            continue
        for grp in tp.get("groups", []):          # формат terehov; у splits ключа groups нет
            for it in grp.get("items", []):
                ct = _gc_ct(it.get("gc", ""))
                if ct and ct != "ct0000" and ct not in seen:
                    seen.add(ct)
                    cts.append(ct)
    return cts


def _tp2_struct_cts(slepok: str, site_type: str) -> list:
    """Совместимость: модель-ct для tp2."""
    return _struct_cts(slepok, site_type, "tp2")


def _text_group_name(ct: str, r_code: str, model: str) -> str:
    """Кодер-имя группы текстовой кампании (tp2/tp4 Поиск) по текущему канону:
    {ct}_aon_n000_{r_code}_ct001_ag011_g00 — {model}.
    Если r_code пуст (нет контекста) — отдаём просто модель (старое поведение)."""
    if not r_code:
        return model or ct
    return f"{ct}_aon_n000_{r_code}_ct001_ag011_g00 — {model}"


def _build_text_from_pack(token: str, login: str, campaign_id: int, slepok: str,
                          site_type: str, tp_code: str, region_ids: list, href: str,
                          titles: list | None, texts: list,
                          feed_id: int = 0, with_shopping: bool = False,
                          r_code: str = "", segment: str | None = None,
                          ai_title2: str = "",
                          apply_group_minus: bool = True,
                          city: str = "", autotarget: bool = False) -> dict:
    """Наполнить текстовую кампанию (tp1/tp2/tp5): структура→модель-ct→ключи/минус/уточнения
    из пака M3 (по tp_code)→группы+объявления+callouts. Тексты — из titles/texts. Всё черновиком.

    segment ('Марки'|'Модели'|None): фильтр ct-групп по сегменту (tp4 — марки/модели разными
    кампаниями, как боевые). None → все ct (поведение tp2/tp5 неизменно).

    ⚠️ tp5 «Поиск+Динамика+Товарная галерея»: тут строится ТОЛЬКО поисковый backbone
    (TEXT_AD + ключи). Фид-объявления (LISTING_AD «динамика» / SHOPPING_AD «товарная») —
    автогенерация Яндекса из фида, НЕ через v5 ads.add; добавятся отдельным шагом."""
    cts = _struct_cts(slepok, site_type, tp_code)
    if segment:
        cts = [ct for ct in cts if _ct_segment(ct) == segment]
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    gather_key = key
    donor_note = ""
    # Фолбэк донора: сегмент задан, но у слепка нет своих ct этого сегмента (напр. Терехов tp4
    # без «Моделей») → берём ct И контент сегмента у слепка-донора («по примеру других слепков»,
    # структура слепка не теряется — его «Марки» строятся отдельной кампанией своим контентом).
    if segment and not cts:
        donor = _segment_donor(segment, tp_code, site_type, exclude=key)
        if donor:
            cts = [ct for ct in _struct_cts(donor, site_type, tp_code) if _ct_segment(ct) == segment]
            gather_key = _SLEPOK_KEY.get(donor, donor)
            donor_note = f"сегмент «{segment}» взят у донора «{donor}» (у «{slepok}» своих нет)"
    if not cts:
        return {"skipped": f"нет модель-ct в структуре для {tp_code} (грубый формат)"}
    pack = kp.gather(gather_key, site_type, tp_code)   # один ssh-вызов к M3
    if not pack:
        return {"skipped": "пак недоступен (мост M3?)"}
    text0 = (texts[0] if texts else "")[:81]
    ct_name = _ag_part1_map()                   # ct→имя из gsheet_naming (полное покрытие 318) — кодер
    ct_model = kp.feeds_ct_model()              # фид-индекс (модельные ct) — фолбэк
    _sc_titles = _slepok_campaign_content(slepok, site_type).get("titles") or []  # пул слепка — 1 раз
    # URL страниц моделей: account-level мёрж (все фиды, как цены) → покрывает марки без URL
    # в конкретном feed_id (был Баг-8: formular _model_page_href на 404). (#ФИКС-8)
    _feed_urls = _account_offer_urls(login, href)
    groups = []
    for ct in cts:
        data = pack.get(ct) or {}
        if not data.get("positive"):
            continue                            # нет ключей в паке — пропускаем модель
        model = _valid_pack_brand_name(ct, ct_name.get(ct) or ct_model.get(ct) or ct) or "Авто"
        # deep-link на страницу модели: сначала реальный URL из фида, фолбэк на формульный слаг.
        # ФИКС A: Марки → обрезаем до /auto/{brand}, Модели → полный путь (без query). (#ФИКС-A)
        _raw_feed_url = _feed_url_for_model(_feed_urls, model)
        if _raw_feed_url:
            model_href = (_brand_level_url(_raw_feed_url) if _ct_segment(ct) == "Марки"
                          else _strip_url_query(_raw_feed_url))
        else:
            model_href = _model_page_href(href, site_type, model)
        # Title: шаблон «Новые {model} в {город}. {акция}» (≤35 симв.) — фолбэк model[:56].
        title = _title_from_template(model or "Авто", city) if (not ai_title2 and model) else (model or "Авто")[:56]
        ttl2 = (ai_title2[:30] if ai_title2 else _next_title2())   # ИИ-title2 или round-robin из пула
        # В боевом create_set контент генерим ОДИН РАЗ на кампанию/item. Делать M3-вызов на
        # КАЖДУЮ ct-группу нельзя: tp1/tp5 содержат десятки групп, и создание зависает на минуты
        # ещё до первой кампании. Внутри группы используем уже готовый кампанийный набор +
        # локальную rsya-добивку/дедупликацию.
        g_titles = _rsya_titles(model, city, site_type, ai_title2=ai_title2,
                                base=list(titles or []) + [title, ttl2], pool=_sc_titles,
                                is_brand=(_ct_segment(ct) in ("Марки", "Модели")))
        g_texts = _rsya_texts(list(texts or []) + ([text0] if text0 else []), site_type, city, model)
        # Картинки: общие ct0000-ct0014 → общий пул ct0000; кузова ct0015-ct0018 → свой ct;
        # модели/марки → свой ct.
        tp2_all_images = _creative_images_for_ct(site_type, tp_code, ct, key)
        groups.append({
            "name": _text_group_name(ct, r_code, model),
            # БАГ-13: для «Марки» — убрать ключи «марка+модель» (напр. «Chery Tiggo 8 Pro»)
            "keywords": _filter_group_keywords(data.get("positive", []), _ct_segment(ct), model, city, site_type),
            "minus": data.get("minus", []),
            "ct": ct,                            # баг #5: нужен для _ct_segment→seg→adPrice по Марке
            "brand": model,                      # модель/бренд группы — для adPrice из фида (#2)
            "titles": g_titles,                  # ← Комбинаторное: список заголовков
            "texts": g_texts,                    # ← Комбинаторное: список текстов
            "title": title, "title2": ttl2,      # совместимость (в ResponsiveAd не используются)
            "text": text0 or "Официальный дилер. Тест-драйв и выгодные условия по кредиту. Авто в наличии.",
            "href": model_href,                  # deep-link страницы модели (если возможен)
            "image_path": tp2_all_images[0] if tp2_all_images else None,   # баг #4: картинки tp2/tp4
            "image_paths": tp2_all_images[:5],                              # баг #4: все пути
            "callouts": data.get("callouts", []),   # уточнения слепка по модели (из пака)
        })
    if not groups:
        return {"skipped": f"пак пуст по {len(cts)} модель-ct"}
    # «Уточнения» (callouts) из пака: создаём общий пул AdExtensions один раз (дедуп) →
    # привязываем ≤4 на объявление каждой группы. Падение callouts не валит сборку.
    co_pool = {}
    try:
        all_co = [c for g in groups for c in (g.get("callouts") or [])]
        co_pool = _ensure_callout_exts(token, login, all_co) if all_co else {}
        for g in groups:
            ids = [co_pool[c] for c in (g.get("callouts") or []) if c in co_pool]
            if ids:
                g["callout_ext_ids"] = ids[:4]
    except Exception:  # noqa: BLE001
        co_pool = {}
    rep = _build_tp2_adgroups(token, login, campaign_id, region_ids, groups,
                              feed_id=feed_id, with_shopping=with_shopping,
                              apply_group_minus=apply_group_minus, autotarget=autotarget)
    rep["cts"] = len(cts)
    rep["groups_built"] = len(groups)
    rep["callouts_pool"] = len(co_pool)
    if donor_note:
        rep["donor"] = donor_note
    return rep


def _build_tp2_from_pack(token: str, login: str, campaign_id: int, slepok: str,
                         site_type: str, region_ids: list, href: str,
                         titles: list | None, texts: list) -> dict:
    """Совместимость: наполнение Поисковой (tp2)."""
    return _build_text_from_pack(token, login, campaign_id, slepok, site_type, "tp2",
                                 region_ids, href, titles, texts)


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


def _tp1_group_name(ct: str, r_code: str, brand: str, with_shopping: bool = False,
                    autotarget: bool = False) -> str:
    """Имя группы tp1 по CANON CODER.md (правило «кодер = реальный состав»).
    Суффикс _NN (было _11/_21/_22) убран по решению директолога (A2, 2026-06-22).

    with_shopping=False (TextAd only):  ct{N}_{aon/aoff}_n000_{r}_ct001_ag011_g00 — {Бренд}
    with_shopping=True  (TextAd+ListingAd+ShoppingAd = «Т+Л+ТОВ»):
                        ct{N}_aon_n000_{r}_ct010_ag011_g00 — {Бренд}
    Источник: справочник local_gsheet_naming (ag_part5): ct010 = «Комбинированный: ТГО + каталог/фид»,
    ct009 = «Товарное (Фид/каталог)» — БЕЗ TextAd. Группа с TextAd+ListingAd+ShoppingAd → ct010.
    ag011 (24-55+) — демо-таргетинг TextAd несёт корректировки по возрасту. Совпадает с эталоном Щербаковой.
    """
    aud_code = "aon" if autotarget else "aoff"
    if with_shopping:
        return f"{ct}_{aud_code}_n000_{r_code}_ct010_ag011_g00 — {brand}"
    return f"{ct}_{aud_code}_n000_{r_code}_ct001_ag011_g00 — {brand}"


# Акционные фразы для шаблона Title (ротация round-robin при формировании групп).
_TITLE_PROMO_POOL = [
    "Распродаем -45%",
    "Выгода до 45%",
    "Скидки на новые авто",
    "Специальные условия",
]
_TITLE_PROMO_IDX = 0


def _next_title_promo() -> str:
    """Следующая акционная фраза из пула (round-robin)."""
    global _TITLE_PROMO_IDX
    phrase = _TITLE_PROMO_POOL[_TITLE_PROMO_IDX % len(_TITLE_PROMO_POOL)]
    _TITLE_PROMO_IDX += 1
    return phrase


# Словарь предложного падежа для реальных городов аккаунтов (direction='Авто').
# Несклоняемые (Кемерово, Тольятти) — стоят как есть. Составные (Нижний Новгород,
# Ростов-на-Дону, Южно-Сахалинск, Санкт-Петербург) — прописаны явно.
# Источник: SELECT DISTINCT city FROM local_gsheet_sites WHERE direction='Авто' (2026-06-22).
_CITY_LOCATIVE: dict[str, str] = {
    "Владивосток":       "Владивостоке",
    "Волгоград":         "Волгограде",
    "Екатеринбург":      "Екатеринбурге",
    "Иркутск":           "Иркутске",
    "Казань":            "Казани",
    "Калининград":       "Калининграде",
    "Кемерово":          "Кемерово",          # несклоняемое
    "Краснодар":         "Краснодаре",
    "Липецк":            "Липецке",
    "Магнитогорск":      "Магнитогорске",
    "Москва":            "Москве",
    "Нижний Новгород":   "Нижнем Новгороде",  # составной
    "Новокузнецк":       "Новокузнецке",
    "Новосибирск":       "Новосибирске",
    "Омск":              "Омске",
    "Ростов-на-Дону":    "Ростове-на-Дону",   # составной
    "Самара":            "Самаре",
    "Санкт Петербург":   "Санкт-Петербурге",  # вариант без дефиса из БД
    "Санкт-Петербург":   "Санкт-Петербурге",
    "Саратов":           "Саратове",
    "Ставрополь":        "Ставрополе",
    "Сургут":            "Сургуте",
    "Тольятти":          "Тольятти",           # несклоняемое
    "Тула":              "Туле",
    "Тюмень":            "Тюмени",
    "Уфа":               "Уфе",
    "Челябинск":         "Челябинске",
    "Южно-Сахалинск":    "Южно-Сахалинске",
}


def _city_locative(city: str) -> str:
    """Вернуть город в предложном падеже («в Краснодаре», «в Москве»).

    Алгоритм:
    1. Точное совпадение из _CITY_LOCATIVE (все реальные города аккаунтов).
    2. Фолбэк-правила окончаний для незнакомых городов:
       -ово/-ево/-ино/-ыно → несклоняемое (Внуково, Бутово)
       -а/-я → -е (Москва→Москве, Казань→Казани handled above)
       -ь → -е (Ставрополь→Ставрополе)
       согласный → +е (Краснодар→Краснодаре)
    3. Если ни одно правило не сработало — возвращаем именительный (лучше чем ошибка).
    """
    city = (city or "").strip()
    if not city:
        return city
    # 1. Словарь (покрывает все реальные города)
    if city in _CITY_LOCATIVE:
        return _CITY_LOCATIVE[city]
    # 2. Фолбэк-правила (для новых городов, которые появятся в аккаунтах позднее)
    low = city.lower()
    if low.endswith(("ово", "ево", "ино", "ыно", "о")):
        return city  # несклоняемые на -о
    if low.endswith("ь"):
        return city[:-1] + "е"
    if low.endswith(("а", "я")):
        return city[:-1] + "е"
    # Согласная в конце → добавить -е
    vowels = set("аеёиоуыэюяАЕЁИОУЫЭЮЯ")
    if city[-1] not in vowels:
        return city + "е"
    return city  # фолбэк: именительный


def _content_city(city: str) -> str:
    """Город для КОНТЕНТА. При МУЛЬТИГОРОДЕ (аккаунт на несколько городов → city записан через запятую,
    напр. «Краснодар, Нижний Новгород, …») НЕ подставляем город в заголовки/тексты — иначе получаем
    «Новые Lada в Краснодар, Нижний Новгород, …». → '' (контент без города). Один город → как есть."""
    c = (city or "").strip()
    if "," in c or ";" in c:
        return ""
    return c


def _title_from_template(brand: str, city: str = "") -> str:
    """Сформировать Title по эталонному шаблону «Новые {brand} в {город}. {акция}».
    Лимит Директа для ЕПК TextAd — 35 символов (поле Title). Обрезаем аккуратно.

    Если brand без пробела (просто марка «BAIC») — «Новые BAIC в Краснодаре. Выгода до 45%».
    Если brand с пробелом (марка+модель «BAIC BJ40») — «Новые BAIC BJ40 в Краснодаре. …».
    city — город из аккаунта (ctx.city); пустой → без города («Новые {brand}. {акция}»).
    Город подставляется в предложном падеже через _city_locative().
    Фолбэк: если шаблон не влезает даже без акции — возвращаем brand[:35].
    """
    city = _content_city(city)                            # мультигород (через запятую) → без города
    city_loc = _city_locative(city) if city else ""
    promo = _next_title_promo()
    if city_loc:
        full = f"Новые {brand} в {city_loc}. {promo}"
    else:
        full = f"Новые {brand}. {promo}"
    if len(full) <= 35:
        return full
    # Попробуем без акционной фразы
    short = f"Новые {brand} в {city_loc}" if city_loc else f"Новые {brand}"
    if len(short) <= 35:
        return short[:35]
    # Фолбэк: просто бренд
    return brand[:35]


def _tp1_video_ads(v501_client, login: str, ag_video: list) -> dict:
    """[ЗАДЕЛ НА БУДУЩЕЕ] Видео-креативы для РСЯ tp1 — ОТДЕЛЬНЫЕ объявления, НЕ TextAd.
    ag_video: [(adgroup_id, [абс.путь_видео, ...]), ...] — что собрал _build_tp1_from_pack.

    Механика РСЯ-видео в ЕПК (почему отдельно от картинки):
      видео → CREATIVE (видеоконструктор Директа) → CpcVideoAd(CreativeId) в группу.
      • v5 API НЕ создаёт видео-креатив из файла (creatives.get — только чтение; креатив
        делается в конструкторе/grid). → нужен creative-API (grid/web-api) — ПОКА не подключён.
      • UAC-путь upload_video_file→content_id (campaign.py) — ТОЛЬКО для tp6/tp7 (Мастер/Товарка),
        для ЕПК РСЯ не годится.
      • TextAd видео не несёт (только AdImageHash — картинка; это уже работает).

    Состояние: в паке M3 видео для tp1 НЕТ (скан 2026-06-22 → 0) → функция dormant.
    Когда появятся: (1) положить видео в манифест `video_slepki.txt` (как image_slepki.txt),
    (2) kp.read_videos подхватит, (3) здесь создать креатив и CpcVideoAd(CreativeId).
    Возврат: отчёт без падения сборки (видео — необязательно)."""
    rep = {"video_groups": sum(1 for _, v in ag_video if v), "video_ads": 0,
           "note": "creative-API ЕПК не подключён — видео-объявления РСЯ пока не создаются (см. докстринг)"}
    # TODO(video): for ag_id, paths in ag_video: creative_id = _make_video_creative(path);
    #              v501_client.add_cpc_video_ad(ag_id, creative_id)
    return rep


def _build_tp1_adgroups(
    token: str,
    login: str,
    campaign_id: int,
    region_ids: list,
    href: str,
    groups: list,
    sitelink_set_id: int | None = None,
    feed_id: int = 0,
    with_shopping: bool = False,
    feed_models: dict | None = None,
    autotarget: bool = False,
    products_only: bool = False,
    grid_cookie: str | None = None,
    base_sitelinks: list | None = None,
) -> dict:
    """Наполнить РСЯ (tp1 ЕПК) группами БАТЧЕМ через v501:
    adgroups.add (с TrackingParams и minus) → keywords.add → adimages.add → ads.add(TextAd+Image).

    feed_id + with_shopping (как в слепках Щербаковой): дополнительно к TextAd в КАЖДУЮ группу
    добавляем ListingAd (динамика) + ShoppingAd (товарное) по фиду — состав «Т+Л+ТОВ» в группе.
    Аддитивно: нет feed_id / with_shopping=False → создаём только TextAd (старое поведение).

    groups: [{name, ct, brand, keywords:[], minus:[], title, text, image_path?, callout_ext_ids?}].
    Анти-блок: операции батчами с паузами, групп ≤ _AC_GROUP_CAP за проход.
    Лимиты: ключей ≤200/группу, минус ≤100/группу, Title ≤35, Text ≤81.
    → {adgroups, keywords, ads, images_uploaded, sitelinks_set, errors, deferred}."""
    rep = {"adgroups": 0, "keywords": 0, "ads": 0, "images_uploaded": 0,
           "sitelinks_set": sitelink_set_id or 0, "errors": [], "deferred": 0}
    rids = [int(r) for r in (region_ids or []) if str(r).lstrip("-").isdigit()] or [225]
    if len(groups) > _AC_GROUP_CAP:
        rep["deferred"] = len(groups) - _AC_GROUP_CAP
        groups = groups[:_AC_GROUP_CAP]

    # ── Фаза 1: adgroups.add с TrackingParams ────────────────────────────────
    # v501 ЕПК: TrackingParams на уровне группы (#2 инвариант — UTM)
    # РСЯ (tp1): минуса на группе НЕ ставим — в сетях они режут охват без пользы.
    # Минус-слова для поисковых (tp2/tp4) — в _build_tp2_adgroups (отдельный путь).
    specs = []
    for g in groups:
        ag: dict = {
            "Name": (g.get("name") or "группа")[:255],
            "CampaignId": int(campaign_id),
            "RegionIds": rids,
            "TrackingParams": _UTM_TEMPLATE_TP1,   # #2 UTM на уровне группы
        }
        specs.append(ag)

    ag_ids = [None] * len(groups)
    idx = 0
    for chunk in _chunks(specs, _AC_CHUNK_AG):
        ja = _v5_call("adgroups", "add", token, login, {"AdGroups": chunk})
        if "error" in ja:
            rep["errors"].append(f"adgroups.add {_v5_err(ja)}")
            idx += len(chunk)
            time.sleep(_AC_BATCH_SLEEP)
            continue
        for r in (ja.get("result") or {}).get("AddResults", []):
            errs = r.get("Errors") or []
            if r.get("Id") and not errs:
                ag_ids[idx] = r["Id"]
                rep["adgroups"] += 1
            else:
                nm = groups[idx].get("name", "?") if idx < len(groups) else "?"
                rep["errors"].append(f"{nm}: adgroup " + ("; ".join(e.get("Message", "") for e in errs) or "нет Id"))
            idx += 1
        time.sleep(_AC_BATCH_SLEEP)

    # ── Фаза 2: keywords.add (ключи на группу ≤200) ──────────────────────────
    # autotarget=True → спецключ "---autotargeting" (1/группу) вместо реальных ключей (РСЯ-Автотаргет).
    kw_items = []
    for i, g in enumerate(groups):
        if not ag_ids[i]:
            continue
        if autotarget:
            kw_items.append({"Keyword": _AUTOTARGET_KW, "AdGroupId": int(ag_ids[i])})
            continue
        for k in _kw_clean(g.get("keywords") or [], 200):
            kw_items.append({"Keyword": k, "AdGroupId": int(ag_ids[i])})
    for chunk in _chunks(kw_items, _AC_CHUNK_KW):
        jk = _v5_call("keywords", "add", token, login, {"Keywords": chunk})
        if "error" not in jk:
            rep["keywords"] += sum(1 for r in (jk.get("result") or {}).get("AddResults", []) if r.get("Id"))
        else:
            rep["errors"].append(f"keywords.add {_v5_err(jk)}")
        time.sleep(_AC_BATCH_SLEEP)

    # ── Фаза 3: ads.add без предварительной token-загрузки картинок ──────────
    # Живой баг 2026-06-28: v501 upload_image на больших tp1 мог зависать до стадии ads.add.
    # Поэтому здесь не тратим время на upload_image вообще: ResponsiveAd создаём сразу,
    # а картинки добиваем post-create через Grid/куки по фактическим ad_id.

    # products_only (Смарт-Баннер/Фиды «без ТГО»): пропускаем КОМБИНАТОРНОЕ, оставляем только товарные (Фаза 4).
    # КОМБИНАТОРНОЕ (ResponsiveAd) через v501 — замена ТГО (отключают с 30.06): несколько заголовков/текстов
    # + картинки (AdImageHashes) + быстрые ссылки (SitelinkSetId). Уточнения наследуются (AdExtensions нет).
    _sl_set_cache: dict = {}   # ad_href → sitelink_set_id (per-group кэш, #ФИКС-3)
    _base_href = (href or "").rstrip("/")
    ad_items = []
    ad_meta = []   # параллельно ad_items: {brand,href,titles,bodies,image_hashes} — для adPrice из фида
    for i, g in enumerate(groups):
        if products_only:
            break
        if not ag_ids[i]:
            continue
        ad_href = g.get("href") or href   # per-group deep-link приоритетнее общего href кампании
        img_paths = g.get("image_paths") or ([g.get("image_path")] if g.get("image_path") else [])
        ra = _responsive_ad(g.get("titles") or [g.get("title"), g.get("brand"), g.get("name")],
                            g.get("texts") or [g.get("text")], ad_href,
                            image_hashes=None)
        if not ra:
            rep["errors"].append(f"{g.get('name', '?')}: пропущено объявление (нет заголовков/текстов/href)")
            continue
        # Per-group sitelink set (#ФИКС-3): href группы ≠ href кампании → создать/закэшировать набор
        _use_sl_id = sitelink_set_id
        if base_sitelinks and ad_href and ad_href.rstrip("/") != _base_href:
            if ad_href not in _sl_set_cache:
                try:
                    _grp_sls = [{**s, "Href": ad_href} for s in base_sitelinks]
                    _sl_set_cache[ad_href] = _get_or_reuse_sitelink_set(token, login, _grp_sls)
                except Exception:  # noqa: BLE001
                    _sl_set_cache[ad_href] = None
            _use_sl_id = _sl_set_cache.get(ad_href) or sitelink_set_id
        if _use_sl_id:
            ra["SitelinkSetId"] = _use_sl_id
        ad_items.append({"AdGroupId": int(ag_ids[i]), "ResponsiveAd": ra})
        ad_meta.append({"brand": g.get("brand") or g.get("name") or "", "href": ad_href,
                        "seg": _ct_segment(g.get("ct") or ""),   # 'Марки' → цена = МИН по марке
                        "titles": ra.get("Titles") or [], "bodies": ra.get("Texts") or [],
                        "image_hashes": [],
                        "image_paths": img_paths[:5]})

    created_ad_meta = []   # [{id, meta}] созданных — для image backfill + adPrice
    repair_items: list[dict] = []
    _base = 0
    for chunk in _chunks(ad_items, _AC_CHUNK_AD):
        jd = _v501_svc("ads", "add", token, login, {"Ads": chunk})
        if "error" not in jd:
            for k, r in enumerate((jd.get("result") or {}).get("AddResults", [])):
                if r.get("Id"):
                    rep["ads"] += 1
                    gi = _base + k
                    if gi < len(ad_meta):
                        created_ad_meta.append({"id": int(r["Id"]), "meta": ad_meta[gi]})
                for e in (r.get("Errors") or []):
                    rep["errors"].append(f"ResponsiveAd(tp1): {e.get('Message')} {e.get('Details','')}".strip())
        else:
            rep["errors"].append(f"ads.add(tp1 ResponsiveAd) {_v5_err(jd)}")
        _base += len(chunk)
        time.sleep(_AC_BATCH_SLEEP)

    # ── Фаза 3.4: post-create repair через Grid/куки для token-path ──────────────────────────
    # Даже если v501 ads.add прошло, live payload в Директе может урезаться. После создания
    # всегда добиваем titles + bodies + imageHashes через Grid. Это основной fallback:
    # если token-path недогрузил объявление, куки-path дособирает именно его, а не оставляет брак.
    if created_ad_meta:
        try:
            import os as _os3
            _gc_img = gf.GridClient(login, cookie=grid_cookie)
            _uploaded_by_name: dict[str, str] = {}
            _upd_items = []
            for _rec in created_ad_meta:
                _meta = _rec["meta"]
                _hashes = list(dict.fromkeys(_meta.get("image_hashes") or []))
                for _pth in (_meta.get("image_paths") or []):
                    if len(_hashes) >= 5:
                        break
                    if not _pth or not _os3.path.isfile(_pth):
                        continue
                    _bn = _os3.path.basename(_pth)
                    _h = _uploaded_by_name.get(_bn)
                    if not _h:
                        _h = _cached_upload_image(_gc_img, login, _pth)
                        if _h:
                            _uploaded_by_name[_bn] = _h
                    if _h and _h not in _hashes:
                        _hashes.append(_h)
                if _hashes:
                    _meta["image_hashes"] = _hashes[:5]
                _upd = {"id": _rec["id"], "href": _meta["href"],
                        "titles": _meta["titles"], "bodies": _meta["bodies"]}
                if _meta.get("image_hashes") is not None:
                    _upd["image_hashes"] = _meta.get("image_hashes") or []
                _upd_items.append(_upd)
            if _upd_items:
                repair_items = list(_upd_items)
                rep["ads_repaired"] = _grid_update_adaptive_ads(login, _upd_items)
                rep["image_groups"] = len(_upd_items)
        except Exception as _e:  # noqa: BLE001
            rep.setdefault("warnings", []).append(f"tp1 repair: {str(_e)[:100]}")

    # ── Фаза 3.5: ЦЕНА из фида в комбинаторное объявление (#2) — Grid по куке (без баллов).
    # adPrice = {current, old} самого дешёвого оффера фида по бренду/модели группы («от X · зачёркнуто old»).
    try:
        _pfeed = feed_id or _grid_price_feed(login, href) or _first_url_feed(token, login)
        _pmap = _grid_feed_offer_prices(login, _pfeed) if _pfeed else {}
        if _pmap and created_ad_meta:
            _pitems = []
            for _rec in created_ad_meta:
                ad_id, meta = _rec["id"], _rec["meta"]
                cur, old = _group_ad_price(_pmap, meta.get("brand", ""), meta.get("seg", ""))
                if cur:
                    _pitems.append({"id": ad_id, "href": meta["href"], "titles": meta["titles"],
                                    "bodies": meta["bodies"], "image_hashes": meta["image_hashes"],
                                    "current": cur, "old": old})
            rep["prices_set"] = _grid_set_ad_prices(login, _pitems)
            if repair_items:
                rep["ads_repaired_after_price"] = _grid_update_adaptive_ads(login, repair_items)
    except Exception as _e:  # noqa: BLE001 — цена не критична, объявление уже создано
        rep.setdefault("warnings", []).append(f"adPrice: {str(_e)[:100]}")

    # ── Фаза 4: товарные по фиду (как в слепках Щербаковой): ListingAd (динамика) + ShoppingAd (товарное)
    # в каждую группу → состав «Т+Л+ТОВ». ShoppingAd создаём ЧЕРЕЗ GRID (addShoppingAds, БЕЗ баллов) —
    # v501 ads.add(ShoppingAd) требовал units и валил докрутку в 152. Только если есть фид и флаг.
    if feed_id and with_shopping:
        rep["listing_ads"], rep["shopping_ads"], rep["shopping_skipped"] = 0, 0, 0
        rep["shopping_ad_ids"] = []   # собираем id для set_default_text (#6 фикс пустого текста)
        rep["shopping_filters"] = {}
        rep["listing_build_items"] = []
        rep["listing_name_by_shop"] = {}   # {shopping_ad_id: name_value} — для name-фильтра листинга
        _grid_shop_items = []         # батч для Grid addShoppingAds: [{adgroup_id, feed_id, vendor/coll}]
        # Коллекции фида (HAR: фильтр «Страницы каталога» = collectionId). Тянем РОВНО этот фид через
        # Grid op Listings (точечный per-feed запрос, не урезанный _grid_feeds). Для брендовых групп
        # резолвим бренд-уровневую коллекцию (id вроде '25' = «Новые автомобили BAIC»), для модельных —
        # model_*. Пустой список → фолбэк на feed_models из _account_model_feeds.
        _feed_colls = _feed_collections(login, int(feed_id), cookie=grid_cookie)
        _feed_models_eff = dict(feed_models or {})
        if not _feed_models_eff:
            _feed_models_eff = _feed_models_from_collections(_feed_colls)
        for i in range(len(groups)):
            if not ag_ids[i]:
                continue
            # Фильтр по бренду/модели — ОБЯЗАТЕЛЕН для товарных объявлений в брендовой группе.
            # Без фильтра ShoppingAd/ListingAd показывает ВЕСЬ фид (все марки), что недопустимо
            # в группе конкретного бренда (например, Lada Granta → только Lada, не Haval/Changan).
            #
            # Алгоритм:
            #   feed_models передан   → попробовать collectionId по имени модели/бренда группы;
            #                           нет совпадения → пропускаем (нет коллекции этого бренда в фиде).
            #   feed_models is None   → фид без model-листингов / agency не передан;
            #                           «Vendor»-фильтр через FeedFilterConditions в v501 НЕ верифицирован
            #                           живым тестом → создавать объявление по всему фиду ЗАПРЕЩЕНО.
            # ДВА РАЗНЫХ фильтра по типу объявления (решение Семёна, HAR36):
            #   Товары (ShoppingAd)        → vendor CONTAINS_ANY [марка] (НЕ collectionId — task-6 сломал).
            #   Страницы каталога (Listing) → name CONTAINS_ANY [марка | марка+модель] (updateListingAds).
            #   ct0000/общее (без марки)   → без фильтра (вся витрина).
            _g_brand = (groups[i].get("brand") or "").strip()
            _g_seg = _ct_segment(groups[i].get("ct") or "")
            # Фильтр по производителю/названию валиден ТОЛЬКО для брендовых групп («Марки»/«Модели»).
            # Для «Общее» brand = имя темы («Автокредит» и т.п.) → vendor/name стали бы мусором
            # (vendor содержит «avtokredit» → 0 товаров). Общее → товарка по всему фиду, каталог — все стр.
            _is_brand_seg = _g_seg in ("Марки", "Модели")
            _vendor = _vendor_value(_g_brand) if (_g_brand and _is_brand_seg) else None
            _name_val = _listing_name_value(_g_brand, _g_seg) if (_g_brand and _is_brand_seg) else None
            _model_vals = _model_field_values(_g_brand, _g_seg) if (_g_brand and _is_brand_seg) else []  # Модели → +model
            _grid_shop_items.append({"adgroup_id": int(ag_ids[i]), "feed_id": int(feed_id),
                                     "vendor": _vendor, "collection_id": None, "model": _model_vals,
                                     "name": groups[i].get("name", "?")})
            rep["listing_build_items"].append({
                "adgroup_id": int(ag_ids[i]),
                "feed_id": int(feed_id),
                "name_value": _name_val,                       # name-фильтр листинга (None для ct0000)
                "name": groups[i].get("name", "?"),
            })
        # Батч Grid addShoppingAds — без баллов (id в порядке adAddItems). При сбое всего батча
        # каждая группа уже имеет TextAd; товарка докрутится ретраем — не валим кампанию.
        if _grid_shop_items:
            try:
                _ids = gf.GridClient(login, cookie=grid_cookie).add_shopping_ads(_grid_shop_items)
                rep["shopping_ad_ids"] = [int(x) for x in _ids if x]
                rep["shopping_ads"] = len(rep["shopping_ad_ids"])
                for _li, (_raw_id, _src) in enumerate(zip(_ids, _grid_shop_items)):
                    if not _raw_id:
                        continue
                    # листинг этой группы (by-shopping) получит name-фильтр по shopping_ad_id
                    _nv = (rep["listing_build_items"][_li] or {}).get("name_value") if _li < len(rep["listing_build_items"]) else None
                    if _nv:
                        rep["listing_name_by_shop"][int(_raw_id)] = _nv
                    _conds = []
                    if _src.get("vendor"):
                        _conds.append({"field": "vendor", "operator": "CONTAINS_ANY",
                                       "stringValue": json.dumps(_vendor_filter_values(_src["vendor"]), ensure_ascii=False)})
                    if _src.get("model"):
                        _mvals = _src["model"] if isinstance(_src["model"], list) else [str(_src["model"])]
                        _mvals = [str(x) for x in _mvals if str(x).strip()]
                        if _mvals:
                            _conds.append({"field": "model", "operator": "CONTAINS_ANY",
                                           "stringValue": json.dumps(_mvals, ensure_ascii=False)})
                    if not _conds and _src.get("collection_id"):
                        _conds.append({"field": "collectionId", "operator": "EQUALS_ANY",   # collectionId → EQUALS_ANY
                                       "stringValue": json.dumps([str(_src["collection_id"])], ensure_ascii=False)})
                    if _conds:
                        rep["shopping_filters"][int(_raw_id)] = {"tab": "CONDITION", "conditions": _conds}
            except Exception as e:  # noqa: BLE001
                rep["errors"].append(f"shopping(Grid addShoppingAds): {str(e)[:120]}")
    return rep


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
    base = (href or "").rstrip("/")
    out, seen = [], set()
    for s in list(sitelinks or []) + list(_GENERIC_SITELINK_FILLERS):
        if not isinstance(s, dict):
            continue
        title = _trim_to_word(_sanitize_content(s.get("Title") or s.get("title") or "", 30), 30).strip()
        desc = _trim_to_word(_sanitize_content(s.get("Description") or s.get("description") or "", 60), 60).strip()
        if not title:
            continue
        if _bad_ad_sitelink(title, desc):
            continue
        k = _variant_norm_key(f"{title} {desc}") or title.lower()
        if k in seen:
            continue
        seen.add(k)
        # Пустой Href → Яндекс отбивает валидацией → весь набор молча теряется.
        # Берём собственный href ссылки (если есть), иначе base, иначе пропускаем.
        sl_href = s.get("Href") or s.get("href") or s.get("url") or base
        if not sl_href:
            continue  # нет href ни у ссылки ни у base — не пускаем сломанный Href=''
        out.append({"Title": title, "Href": sl_href, "Description": desc})
        if len(out) >= 8:
            break
    return out


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
        gc = gf.GridClient(login)
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
                _gc_assets = gf.GridClient(login, cookie=grid_cookie)
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
            out["sitelink_set_id"] = gf.GridClient(login, cookie=grid_cookie).add_sitelink_set(asset_sl)
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


_RUB_DISC_RE = re.compile(r"(\d[\d\s ]*\d|\d)\s*(₽|руб\.?)", re.IGNORECASE)
_PCT_DISC_RE = re.compile(r"(\d{1,3})\s*%")
_RUB_DISCOUNT_CTX_RE = re.compile(r"(?i)(скидк|выгод|господдерж|госпрограмм|подар)")


def _fmt_thousands(digits: str) -> str:
    """'890000' → '890 000' (узкий неразрывный пробел как разделитель тысяч, как в Директе)."""
    d = re.sub(r"\D", "", digits or "")
    if not d:
        return digits
    out = []
    for i, ch in enumerate(reversed(d)):
        if i and i % 3 == 0:
            out.append(" ")
        out.append(ch)
    return "".join(reversed(out))


def _coherent_discounts(titles: list, texts: list) -> tuple:
    """Согласовать скидки/выгоды в заголовках и текстах ОДНОЙ кампании: одно ₽-число и один %-число
    на всю кампанию (эталон = САМОЕ ЧАСТОЕ значение в контенте — без выдумывания новых цифр).
    Лечит #6 (в заголовке X% → и в тексте X%) и #5 (890/860 «выгода» → единое число → дедуп). → (titles, texts)."""
    from collections import Counter
    alls = [s for s in (list(titles or []) + list(texts or [])) if isinstance(s, str)]
    rub = Counter(
        re.sub(r"[\s ]", "", m.group(1))
        for s in alls
        if _RUB_DISCOUNT_CTX_RE.search(s or "")
        for m in _RUB_DISC_RE.finditer(s)
    )
    pct = Counter(m.group(1) for s in alls for m in _PCT_DISC_RE.finditer(s))
    canon_rub = rub.most_common(1)[0][0] if rub else None
    canon_pct = pct.most_common(1)[0][0] if pct else None
    if not canon_rub and not canon_pct:
        return list(titles or []), list(texts or [])
    fr = _fmt_thousands(canon_rub) if canon_rub else None

    def _fix(s):
        if not isinstance(s, str):
            return s
        if fr:
            if _RUB_DISCOUNT_CTX_RE.search(s):
                s = _RUB_DISC_RE.sub(lambda m: f"{fr} {m.group(2)}", s)
        if canon_pct:
            s = _PCT_DISC_RE.sub(f"{canon_pct}%", s)
        return s
    return [_fix(t) for t in (titles or [])], [_fix(t) for t in (texts or [])]


def _variant_norm_key(x) -> str:
    """Нормализованный ключ для дедупа вариантов контента (заголовки/тексты/быстрые ссылки).
    Схлопывает ЧИСЛА в один маркер «#», поэтому «…скидки до 890 000 ₽…» и «…до 860 000 ₽…»
    считаются ОДНИМ вариантом (отличие только в цифре = по сути дубль). Плюс схлоп пробелов."""
    s = (x.get("title", "") if isinstance(x, dict) else str(x)).strip().lower()
    s = re.sub(r"(?i)\bplug[-\s]?in\s+hybrid\b", "", s)
    s = re.sub(r"(?i)^\s*(купить|новые?|оформите)\s+", "", s)
    s = re.sub(r"\d[\d\s ]*\d", "#", s)             # 2+ значные числа/цены (45%, 890 000) -> маркер
    s = re.sub(r"\s+", " ", s)                            # схлоп пробелов (модели X35/F7 сохраняем)
    return s.strip()


def _text_norm_tokens(x) -> list:
    """Нормализованное ядро текста в токены для префиксного дедупа: lower, ё→е, убрана
    пунктуация/валюта/проценты, схлоп пробелов. «Каско на 1 год бесплатно.» →
    ['каско','на','1','год','бесплатно']."""
    s = (x.get("title", "") if isinstance(x, dict) else str(x or "")).lower().replace("ё", "е")
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)     # пунктуация/₽/% → пробел
    return [t for t in s.split() if t]


def _dedup_prefix_absorb(items: list, min_tokens: int = 4) -> list:
    """Схлопнуть пары, где нормализованное ядро одной строки — ПРЕФИКС (надмножество с хвостом)
    другой: «Первый взнос 0 ₽. КАСКО на 1 год бесплатно.» поглощается строкой
    «…бесплатно при покупке в кредит». Оставляем БОЛЕЕ информативную (длинную). Короткую с
    <min_tokens токенами не трогаем (чтобы не схлопнуть по 1-2 общим словам). Реально разные
    оферы (расходятся с начала) — оба сохраняются. Порядок исходных строк сохранён."""
    toks = [(_text_norm_tokens(x), x) for x in (items or [])]
    drop: set = set()
    for i in range(len(toks)):
        if i in drop:
            continue
        ti = toks[i][0]
        if len(ti) < min_tokens:
            continue
        for j in range(len(toks)):
            if i == j or j in drop:
                continue
            tj = toks[j][0]
            # ti — префикс tj (ti короче или равно), хвост tj = «расширение» → поглощаем короткую ti
            if len(ti) <= len(tj) and tj[:len(ti)] == ti:
                drop.add(i)
                break
    return [x for k, (_t, x) in enumerate(toks) if k not in drop]


def _fill_variants(primary: list, supplement: list, need: int) -> list:
    """Контент слепка (primary) в приоритете + добор из supplement до need штук.
    Дедуп по НОРМАЛИЗОВАННОМУ ключу (числа схлопнуты) — чтобы не грузить почти-одинаковые
    заголовки/тексты, отличающиеся только цифрой (см. _variant_norm_key)."""
    seen, out = set(), []
    for x in list(primary) + list(supplement):
        k = _variant_norm_key(x)
        if k and k not in seen:
            seen.add(k)
            out.append(x)
        if len(out) >= need:
            break
    return out


def _rotated_content_window(items: list, need: int, offset: int) -> list:
    """Взять до need элементов с ротацией по offset, сохраняя порядок и дедуп.
    Для быстрых черновиков по слепку это даёт не однотипный контент между РК,
    но источник остаётся тем же слепком."""
    src = [x for x in (items or []) if x]
    if not src:
        return []
    out, seen = [], set()
    n = len(src)
    for i in range(n):
        x = src[(offset + i) % n]
        k = _variant_norm_key(x)
        if k and k not in seen:
            seen.add(k)
            out.append(x)
        if len(out) >= need:
            break
    return out


# Крупные города РФ (именительный, lower) — для фильтра «чужой город в контенте». Подстрочный
# матч ловит склонённые формы (новгород ⊂ «в Новгороде»). Дополняет аккаунт-города из БД.
_RU_CITIES = {
    "москва", "санкт-петербург", "петербург", "новосибирск", "екатеринбург", "казань",
    "нижний новгород", "новгород", "челябинск", "самара", "омск", "ростов", "уфа", "красноярск",
    "воронеж", "пермь", "волгоград", "краснодар", "саратов", "тюмень", "тольятти", "ижевск",
    "барнаул", "ульяновск", "иркутск", "хабаровск", "ярославль", "владивосток", "махачкала",
    "томск", "оренбург", "кемерово", "новокузнецк", "рязань", "астрахань", "набережные челны",
    "пенза", "липецк", "киров", "чебоксары", "тула", "калининград", "балашиха", "курск",
    "ставрополь", "улан-удэ", "тверь", "магнитогорск", "сочи", "иваново", "брянск", "белгород",
    "сургут", "владимир", "нижний тагил", "архангельск", "чита", "симферополь", "калуга",
    "смоленск", "волжский", "якутск", "саранск", "череповец", "курган", "орёл", "вологда",
    "владикавказ", "подольск", "грозный", "мурманск", "тамбов", "стерлитамак", "петрозаводск",
    "кострома", "нижневартовск", "новороссийск", "йошкар-ола", "таганрог", "комсомольск",
    "сыктывкар", "нальчик", "шахты", "дзержинск", "братск", "орск", "ангарск", "благовещенск",
    "энгельс", "псков", "бийск", "армавир", "рыбинск", "северодвинск", "абакан", "норильск",
}


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


def _drop_brand_model_keys(keywords: list, brand: str) -> list:
    """БАГ-13: для кампании ПО МАРКЕ (ct_type='Марки') — исключить ключи, содержащие марку И модель
    одновременно. Ключи «Chery» — оставить. Ключи «Chery Tiggo 8 Pro» — убрать.

    Механика: если ключ содержит бренд + ещё одно слово-не-предлог (не артикль/союз) — это
    ключ марка+модель. brand — название марки из ct (напр. «Chery», «Haval»).
    Пустой brand или brand из одного слова ≥4 букв = безопасный фолбэк (не фильтруем).
    Нечувствителен к регистру."""
    brand = (brand or "").strip()
    if not brand:
        return list(keywords)
    # Стоп-слова (предлоги/союзы/частицы): их наличие рядом с маркой ≠ признак модели
    _STOP = {"в", "и", "на", "от", "до", "для", "с", "по", "за", "к", "у", "о", "или", "не",
             "авто", "официальный", "официальные", "дилер", "купить", "цена", "цены", "новые", "новый"}
    brand_re = re.compile(r"(?i)\b" + re.escape(brand) + r"\b")
    result = []
    for kw in keywords:
        s = str(kw).strip()
        if not brand_re.search(s):
            result.append(kw)   # нет марки → оставляем
            continue
        # Убираем марку и смотрим, есть ли рядом содержательные слова (модель)
        without_brand = brand_re.sub("", s).strip()
        extra_words = [w for w in re.split(r"\W+", without_brand) if w and w.lower() not in _STOP and len(w) >= 3]
        if extra_words:
            continue     # есть слово вне стоп-списка → это ключ «марка+модель» → выкидываем
        result.append(kw)
    return result


_BRAND_MODEL_TOKEN_SET: set | None = None


def _brand_model_token_set() -> set:
    """Канон-токены ВСЕХ марок и моделей (сегменты «Марки»/«Модели» классификатора) — чтобы отсеять
    модельные ключи из ОБЩИХ групп (Дром/Авто/ct0001…): общая группа не должна нести ключ конкретной
    модели (Cityray/Monjaro/Tiggo…). В набор кладём и сырой токен, и его канон (кир→лат). Кэш."""
    global _BRAND_MODEL_TOKEN_SET
    if _BRAND_MODEL_TOKEN_SET is not None:
        return _BRAND_MODEL_TOKEN_SET
    _STOP = {"авто", "автомобиль", "автомобили", "машина", "машины", "new", "plus", "pro",
             "max", "sport", "купить", "цена", "новый", "новые"}
    toks: set = set()
    for ct, nm in _ag_part1_map().items():
        if _ct_segment(ct) in ("Марки", "Модели"):
            for w in re.findall(r"[a-zа-яё0-9]+", str(nm or "").lower()):
                if len(w) >= 3 and w not in _STOP:
                    toks.add(w)
                    toks.add(_brand_canon(w) or w)
    _BRAND_MODEL_TOKEN_SET = toks
    return toks


def _drop_model_keys_common(keywords: list) -> list:
    """Из ключей ОБЩЕЙ группы («Общее») убрать те, что содержат токен конкретной марки/модели.
    Общая группа (Авто/Дром/ct0001) несёт только общие запросы (автокредит, авто в кредит), а не
    «Geely Cityray цена». Ловит латиницу и канон кириллицы (хавал→haval, джили→geely)."""
    toks = _brand_model_token_set()
    if not toks:
        return list(keywords or [])
    out = []
    for kw in (keywords or []):
        words = set()
        for w in re.findall(r"[a-zа-яё0-9]+", str(kw).lower()):
            words.add(w)
            words.add(_brand_canon(w) or w)
        if words & toks:
            continue
        out.append(kw)
    return out


def _filter_group_keywords(positive: list, seg: str, brand: str, city: str, site_type: str) -> list:
    """Единый отбор ключей группы по сегменту ct: 'Марки' → убрать «марка+модель»; 'Общее' → убрать
    любые модельные/марочные ключи (cityray/monjaro в общей группе — баг); 'Модели' → оставить как есть
    (модельные ключи — суть группы). Предварительно всегда: б/у и чужой город."""
    kws = _drop_used_car(_drop_foreign_city_keywords(positive or [], city), site_type)
    if seg == "Марки":
        out = _drop_brand_model_keys(kws, brand)
    elif seg == "Общее":
        out = _drop_model_keys_common(kws)
    else:
        return kws
    # Guard (#6): seg-фильтр НЕ должен обнулять непустой набор → иначе группа без ключей (мёртвая).
    # Пример: ct0014 «Авто/Автомобили/Машины» (Общее) несёт ТОЛЬКО модельные ключи («auto ru monjaro»)
    # → _drop_model_keys_common вырезал всё. Лучше оставить менее-отфильтрованный kws, чем пустую группу
    # (как _rsya_titles с фолбэком). Если и kws пуст (все чужой-город/бу) — вернём как есть.
    return out or kws


_AUTO_BRAND_TOKEN_CACHE: set[str] | None = None


def _auto_brand_tokens() -> set[str]:
    """Канонические токены марок из фид-индекса + ручной минимум для фильтра чужих брендов в текстах."""
    global _AUTO_BRAND_TOKEN_CACHE
    if _AUTO_BRAND_TOKEN_CACHE is not None:
        return _AUTO_BRAND_TOKEN_CACHE
    base = {
        "lada", "лада", "baic", "belgee", "changan", "chery", "dongfeng", "exeed", "faw",
        "gac", "geely", "haval", "hyundai", "jac", "jaecoo", "kaiyi", "kia", "livan",
        "mazda", "moskvich", "москвич", "omoda", "renault", "skoda", "tank", "toyota",
        "voyah",
    }
    try:
        for model in kp.feeds_ct_model().values():
            toks = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", str(model or ""))
            if toks:
                base.add(toks[0].lower())
    except Exception:  # noqa: BLE001
        pass
    _AUTO_BRAND_TOKEN_CACHE = {x for x in base if len(x) >= 2}
    return _AUTO_BRAND_TOKEN_CACHE


def _own_brand_tokens(brand: str) -> set[str]:
    toks = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", str(brand or ""))
    own = {toks[0].lower()} if toks else set()
    if "lada" in own:
        own.add("лада")
    if "лада" in own:
        own.add("lada")
    if "moskvich" in own:
        own.add("москвич")
    if "москвич" in own:
        own.add("moskvich")
    return own


def _drop_foreign_brand_mentions(items: list, brand: str) -> list:
    """Для группы BAIC/Haval/... выкинуть тексты с чужой маркой, например LADA в группе BAIC."""
    own = _own_brand_tokens(brand)
    if not own:
        return list(items or [])
    foreign = _auto_brand_tokens() - own
    out = []
    for x in items or []:
        s = str(x)
        sl = s.lower()
        if any(re.search(r"(?<![a-zа-яё0-9])" + re.escape(tok) + r"(?![a-zа-яё0-9])", sl)
               for tok in foreign):
            continue
        out.append(s)
    return out


def _brand_text_set(brand: str, city: str = "") -> list[str]:
    """Брендовые УТП для РСЯ-группы, чтобы добивка не брала тексты про чужую марку."""
    city = _content_city(city)                            # мультигород (через запятую) → без города
    brand = _display_brand(brand)
    if not brand:
        return []
    city_loc = _city_locative((city or "").strip()) if (city or "").strip() else ""
    loc = f" в {city_loc}" if city_loc else ""
    return [
        f"Купить {brand} в кредит{loc}. Первый взнос 0 ₽ и КАСКО на 1 год бесплатно",
        f"{brand} в наличии{loc}. Одобрение за 30 минут и подбор от 15 банков",
        f"Кредит на {brand}{loc}. Трейд-ин выше рынка и заявка онлайн",
        f"{brand} в кредит{loc}. Платеж от 9 000 ₽/мес и оформление онлайн",
        f"{brand}{loc}. Выгода при покупке в кредит и быстрое одобрение",
    ]


def _display_brand(brand: str) -> str:
    """Название марки/модели для текстов: без slash-технических склеек вроде UNI-S/CS55Plus."""
    return re.sub(r"\s+", " ", str(brand or "").replace("/", " ")).strip()


def _brand_in_text(text: str, brand: str) -> bool:
    """Есть ли в строке явное упоминание текущей марки/модели."""
    own = _own_brand_tokens(brand)
    if not own:
        return False
    low = str(text or "").lower()
    return any(re.search(r"(?<![a-zа-яё0-9])" + re.escape(tok) + r"(?![a-zа-яё0-9])", low) for tok in own)


def _city_prep(city: str) -> str:
    """Совместимый алиас к _city_locative()."""
    return _city_locative(city)


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


def _replace_foreign_city(items: list, own_city: str, cities: set) -> list:
    """ЗАМЕНИТЬ чужой город в заголовках/текстах на город аккаунта (город в контенте обязан
    совпадать с городом аккаунта). «…в Новгороде» → «…в Кемерово». После «в/во» — предложный
    падеж; без предлога — именительный. Свой город / без города — без изменений.
    Пул городов: наши (local_gsheet_sites) + крупные РФ (_RU_CITIES)."""
    own = _content_city(own_city)                         # мультигород (через запятую) → не подставляем город
    if not own:
        return [str(x) for x in items]
    own_l = own.lower()
    prep = _city_prep(own)
    pool = (cities or set()) | _RU_CITIES
    out = []
    for x in items:
        s = str(x)
        if own_l in s.lower():                            # уже свой город — не трогаем
            out.append(s)
            continue
        for c in pool:
            if c == own_l or c in own_l or own_l in c or len(c) < 4 or c not in s.lower():
                continue
            s2 = re.sub(r"(?i)(\bво?\s+)" + re.escape(c) + r"[а-яё\-]*", r"\1" + prep, s)
            if s2 == s:                                   # без предлога → именительный
                s2 = re.sub(r"(?i)" + re.escape(c) + r"[а-яё\-]*", own, s)
            s = s2
        out.append(s)
    return out


# Чистые УТП-тексты для РСЯ (≤56, ОДНА мысль/предложение) — приоритетнее «кашеобразных» текстов
# слепка («Господдержка … Нулевой утильсбор … Распродаём стоянку -45%. Звоните!»). Правило пользователя.
# ⛔ БЕЗ %-ставки кредита/рассрочки (правило Семёна). Полные, «вкусные», грамотные УТП-предложения
# (с заглавной, законченная мысль) — чтобы при склейке к 81 не было «дермовых» огрызков.
_RSYA_TEXT_POOL = [
    "Кредит на новый авто. Одобрение за 30 минут онлайн",
    "Автокредит от 9 000 руб. Одобрение за 30 минут онлайн",
    "КАСКО в подарок и оценка авто в трейд-ин при кредите",
    "Первый взнос 0 руб. Подберем условия от банков",
    "Господдержка и выгодный кредит. Заявка онлайн",
    "Новые авто в наличии. Выгодные условия и подарки",
    "Тест-драйв сегодня. Запишитесь онлайн за пару минут",
    "Авто в наличии. Выгода месяца и подарки при покупке",
]


# ── БАГ 4: замена длинного тире на точку ──────────────────────────────────────
_EMDASH_RE = re.compile(r"[—–]")   # — (U+2014), – (U+2013)
# Дефис как РАЗДЕЛИТЕЛЬ смысловых частей фразы: « - » (пробел-дефис-пробел).
# Правило Кудерко: разделитель частей заголовка — точка, не дефис.
# ТОЛЬКО пробельный дефис; внутрисловной дефис (трейд-ин, тест-драйв) НЕ трогаем.
_SEP_HYPHEN_RE = re.compile(r" - ")


def _replace_emdash(s: str) -> str:
    """Заменить длинное/короткое тире на «. » с заглавной первой буквой после точки."""
    s = str(s or "")
    parts = _EMDASH_RE.split(s)
    result = parts[0].rstrip()
    for part in parts[1:]:
        part = part.lstrip()
        capped = (part[:1].upper() + part[1:]) if part else part
        result = result + ". " + capped
    return result


def _replace_sep_hyphen(s: str) -> str:
    """Заменить дефис-разделитель « - » на «. » с заглавной буквой первого слова после точки
    (правило Кудерко: не дефис между частями фразы, а точка как разделитель предложений)."""
    def _cap_after(m: re.Match) -> str:
        # следующий символ после «. » — заглавная буква
        return ". "
    s = str(s or "")
    # Заменяем « - » на «. » и делаем заглавной букву после точки
    parts = _SEP_HYPHEN_RE.split(s)
    result = parts[0]
    for part in parts[1:]:
        capped = (part[:1].upper() + part[1:]) if part else part
        result = result + ". " + capped
    return result


# ── БАГ 8: предлоги/маленькая буква в начале текста ───────────────────────────
_FRAG_LEAD_ALL = ("до ", "с ", "за ", "по ", "из ", "от ", "в ", "на ", "у ", "к ", "о ", "со ", "и ", "а ")


def _is_bad_start(s: str) -> bool:
    """Текст начинается с предлога/союза (огрызок) или с маленькой буквы (БАГ 8)."""
    s = (s or "").strip()
    if not s:
        return True
    sl = s.lower()
    if any(sl.startswith(p) for p in _FRAG_LEAD_ALL):
        return True
    return bool(s[0].islower())


# ── БАГ 3: обрезка текста по целому слову ──────────────────────────────────────
def _trim_to_word(s: str, max_len: int) -> str:
    """Обрезать строку до max_len по последнему целому слову (БАГ 3)."""
    if len(s) <= max_len:
        return s
    cut = s[:max_len]
    sp = cut.rfind(" ")
    return (cut[:sp].rstrip(" .,!?-") if sp > max_len // 2 else cut).rstrip()


_DANGLING_NUM_TAIL_RE = re.compile(
    r"(?i)(?:[.!?]\s*)?(?:[А-Яа-яЁёA-Za-z-]+\s+){0,3}(?:за|до|от)\s+\d[\d\s\u00a0]*$"
)
_DANGLING_WORD_TAIL_RE = re.compile(
    r"(?i)(?:\s+|^)(?:в|во|на|по|при|с|со|для|от|и|или|а|но|к|ко|за|без|"
    r"выгодный|выгодные|комфортный|низкий|новый|новые|первый|кре|кредитн|"
    r"господдержк|покупк)\s*$"
)


def _strip_dangling_num_tail(s: str) -> str:
    """Убрать обрыв после обрезки: «Одобрение за 30», «платеж от 8 000» без единицы/валюты."""
    s = str(s or "").rstrip()
    m = _DANGLING_NUM_TAIL_RE.search(s)
    if not m:
        return s
    head = s[:m.start()].rstrip(" .,;:!?-")
    return head if len(head) >= 20 else s


def _strip_dangling_word_tail(s: str) -> str:
    """Убрать хвост после обрезки: предлог/незавершенное прилагательное в конце строки."""
    s = str(s or "").rstrip(" .,;:!?-")
    while True:
        m = _DANGLING_WORD_TAIL_RE.search(s)
        if not m:
            return s
        head = s[:m.start()].rstrip(" .,;:!?-")
        if len(head) < 20:
            return s
        s = head


def _sanitize_content(s: str, max_len: int = 0) -> str:
    """Единая пост-обработка: БАГ 4→исправлен (тире->точка), БАГ 8 (капитализация), БАГ 3 (обрезка по слову)."""
    s = _replace_emdash(str(s or ""))
    s = re.sub(r"(?i)\bтрейд-?ин\s+при\s+покупк[еаи]\b", "трейд-ин при оформлении кредита", s)
    s = re.sub(r"(?i)\bкредит\s+и\s+трейд-?ин\s+при\s+покупк[еаи]\b", "кредит и оценка авто в трейд-ин", s)
    s = re.sub(r"(?i)\b(?:прямо\s+)?у\s+дилера\b", "", s)
    s = re.sub(r"(?i)\bот\s+дилера\b", "", s)
    s = re.sub(r"(?i)\bдилер[а-яё]*\b", "", s)
    s = re.sub(r"\s+([,.!?])", r"\1", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" ,.")
    s = re.sub(r'(\.\s+)([а-яёa-z])', lambda m: m.group(1) + m.group(2).upper(), s)
    s = _cap_first(s)
    if max_len:
        s = _trim_to_word(s, max_len)
        s = _strip_dangling_num_tail(s)
        s = _strip_dangling_word_tail(s)
    return s.strip()


_PREFIX_RUB_RE_BP = re.compile(r"₽\s*(\d[\d\s\u00a0]*)")
_PREFIX_PCT_RE_BP = re.compile(r"%\s*(\d[\d\s\u00a0]*)")


def _normalize_numeric_suffixes_bp(s: str) -> str:
    """Нормализует порядок знаков вокруг числа: ₽9000 -> 9000₽, %45 -> 45%, 0 руб. -> 0 ₽."""
    x = str(s or "")
    x = _PREFIX_RUB_RE_BP.sub(lambda m: f"{m.group(1)}₽", x)
    x = _PREFIX_PCT_RE_BP.sub(lambda m: f"{m.group(1)}%", x)
    x = re.sub(r"(?i)(\d[\d\s\u00a0]*)\s*(?:р\.|руб\.?|рублей)\b", lambda m: f"{m.group(1)} ₽", x)
    x = re.sub(r"(\d)\s+₽\b", r"\1₽", x)
    x = re.sub(r"(\d)\s+%", r"\1%", x)
    return x


_CREDIT_RATE_RE1 = re.compile(r"(?i)(кредит\w*|рассрочк\w*|ставк\w*|переплат\w*)\s*(?:всего\s+|от\s+|под\s+)?\d+[.,]?\d*\s*%")
_CREDIT_RATE_RE2 = re.compile(r"(?i)\b(?:от\s+|под\s+)?\d+[.,]?\d*\s*%\s*годовых")
_CREDIT_RATE_RE3 = re.compile(r"(?i)\bпод\s+\d+[.,]?\d*\s*%")


def _strip_credit_rate(s: str) -> str:
    """Убрать %-СТАВКУ кредита/рассрочки (правило Семёна).
    «кредит 0%»->«кредит», «рассрочка 0%»->«рассрочка», «0% годовых»->'', «под 5%»->''. Скидки/выгоды
    в % НЕ трогаем (запрещена только ставка кредита)."""
    s = _CREDIT_RATE_RE1.sub(r"\1", str(s or ""))
    s = _CREDIT_RATE_RE2.sub("", s)
    s = _CREDIT_RATE_RE3.sub("", s)
    s = re.sub(r"(?i)\s+годовых\b", "", s)           # осиротевшее «годовых» после снятия ставки
    return re.sub(r"\s{2,}", " ", s).strip(" -—–·,.")


def _cap_first(s: str) -> str:
    """Заглавная первая буква (фикс «У дилера. авто…» → «У дилера. Авто…» при склейке)."""
    s = str(s).strip()
    return (s[:1].upper() + s[1:]) if s else s


_SENTENCE_CASE_RE_BP = re.compile(r"(^|[.!?]\s+)([a-zа-яё])")


def _sentence_case(s: str) -> str:
    """Поднять первую букву у каждого предложения."""
    return _SENTENCE_CASE_RE_BP.sub(lambda m: m.group(1) + m.group(2).upper(), str(s or ""))


# Предлоги/союзы в начале → это ОГРЫЗОК из середины предложения (после _split_utp), выкидываем.
_FRAG_LEAD = ("до ", "от ", "и ", "с ", "в ", "на ", "по ", "за ", "у ", "из ", "к ", "о ", "со ")
_RSYA_TEXT_MAX = 56          # тексты РСЯ режем жёстче (≤56), чтобы были чёткие УТП, а не каша


def _split_utp(s: str) -> list:
    """Разбить «кашеобразный» текст («А до 925000₽. Б утильсбор. В -45%. Звоните!») на ОТДЕЛЬНЫЕ
    чёткие УТП ≤56 (правило пользователя: «структура предложений»). Слабые огрызки (<8 симв,
    «Звоните!») выкидываем."""
    parts = re.split(r"(?<=[.!?])\s+|\s+[—–]\s+", str(s or ""))
    out = []
    for p in parts:
        p = p.strip().rstrip(".!?").strip()
        if 8 <= len(p) <= _RSYA_TEXT_MAX:
            out.append(p)
    return out


def _rsya_texts(incoming: list, site_type: str, city: str,
                brand: str = "", cap: int = _RA_TEXTS_CAP) -> list:
    """≤cap чётких УТП-текстов РСЯ (≤56, ОДНА мысль). СОХРАНЯЕМ контент M3/слепка: короткие тексты —
    как есть, длинные «кашеобразные» — РАЗБИВАЕМ на отдельные УТП (`_split_utp`). Чистый пул
    `_RSYA_TEXT_POOL` — только добивка, если своих не хватило. Чистка: не-Б/У сайт → без «б/у»;
    чужой город → город аккаунта. Когерентность с заголовками — в _responsive_ad."""
    _, _cities_bl = _title2_blocklist()
    acc_city = (city or "").strip()

    def _cf(lst):
        return _replace_foreign_city(_drop_used_car(list(lst or []), site_type), acc_city, _cities_bl)

    raw_incoming = _cf(list(incoming or []))
    branded_incoming = [t for t in raw_incoming if _brand_in_text(t, brand)] if brand else []
    generic_incoming = [t for t in raw_incoming if t not in branded_incoming]
    pieces = []
    source_items = ((branded_incoming + generic_incoming) if not brand else (branded_incoming + generic_incoming[:1]))
    for t in source_items:
        t = _strip_credit_rate(str(t))               # ⛔ убрать %-ставку кредита (правило Семёна)
        if len(t) <= _RSYA_TEXT_MAX:
            pieces.append(t)                         # уже чёткий ≤56 — как есть
        else:
            pieces += _split_utp(t)                  # длинный → отдельные УТП ≤56
    brand_fillers = _brand_text_set(brand, city) if brand else []
    pieces = _drop_foreign_brand_mentions(brand_fillers + pieces, brand) if brand else pieces
    # Чистка КАЖДОГО куска: убрать %-ставку, заглавная буква, ВЫКИНУТЬ огрызки (начинается с предлога
    # «до/от/у…» = середина предложения → «дермовый» текст). Дедуп. Чистый пул — в добивку.
    seen, uniq, stamp_fallback = set(), [], []
    for p in (pieces + list(_RSYA_TEXT_POOL)):
        # БАГ 4→исправлен: тире -> точка; дефис-разделитель → точка; БАГ 8: капитализация; %-ставка убирается
        p = _normalize_numeric_suffixes_bp(
            _sentence_case(_cap_first(_replace_sep_hyphen(_replace_emdash(_strip_credit_rate(p)))))
        )
        # БАГ 8: предлог в начале / маленькая буква / слишком коротко -> выкидываем
        if len(p) < 15 or _is_bad_start(p):
            continue
        if _bad_ad_text(p):
            continue
        # БАГ 2: дедупликация по смысловому ключу (_variant_norm_key схлопывает числа)
        k = _variant_norm_key(p)
        if k and k not in seen:
            seen.add(k)
            # АНТИ-AI ПРАВИЛО 1: штампы идут в резерв — используются только если чистых не хватает
            if _has_stamp(p):
                stamp_fallback.append(p)
            else:
                uniq.append(p)
    # Докидываем штамп-резерв только если чистых текстов недостаточно
    if len(uniq) < cap:
        uniq += stamp_fallback
    # АНТИ-AI ПРАВИЛО 4: тексты с цифрами — первыми (конкретика важнее абстракций).
    uniq = [t for t in uniq if _has_number(t)] + [t for t in uniq if not _has_number(t)]
    # ЖАДНАЯ СКЛЕЙКА УТП в ПОЛНЫЙ текст <=81 — добиваем как можно ближе к 81.
    # БАГ 3: склеенный текст обрезаем по последнему целому слову (_trim_to_word), не жёстко.
    _TXT_MAX = 81
    out, i = [], 0
    while i < len(uniq) and len(out) < cap:
        cur = uniq[i].rstrip(".!?")
        i += 1
        while i < len(uniq):
            nxt = _cap_first(uniq[i].rstrip(".!?"))   # заглавная у каждой склеиваемой части
            if len(cur) + 2 + len(nxt) <= _TXT_MAX:
                cur = cur + ". " + nxt
                i += 1
            else:
                break
        # БАГ 3: обрезка по целому слову, не посреди
        out.append(_sentence_case(_trim_to_word(cur.strip(), _TXT_MAX).rstrip()))
    # Практическое требование по tp1: первый текст не должен оставаться коротким однофразником,
    # если в лимите 81 есть место для нормального второго УТП.
    pad_tails = [
        "Одобрение за 30 минут",
        "КАСКО в подарок",
        "Трейд-ин выше рынка",
        "Запись на тест-драйв",
        "Заявка онлайн",
    ]
    padded: list[str] = []
    used_tail_buckets: set[str] = set()

    def _tail_bucket(s: str) -> str:
        x = str(s or "").lower()
        if "одобр" in x:
            return "approval"
        if "каско" in x or "подар" in x:
            return "gift"
        if "трейд" in x:
            return "tradein"
        if "тест" in x:
            return "testdrive"
        if "дилер" in x:
            return "dealer"
        return x

    for text in out:
        cur = str(text or "").rstrip(".!?")
        for tail in pad_tails:
            if len(cur) >= 75:
                break
            if tail.lower() in cur.lower():
                continue
            tb = _tail_bucket(tail)
            if tb in used_tail_buckets:
                continue
            nxt = _cap_first(tail.rstrip(".!?"))
            if len(cur) + 2 + len(nxt) <= _TXT_MAX:
                cur = f"{cur}. {nxt}"
                used_tail_buckets.add(tb)
        padded.append(_normalize_numeric_suffixes_bp(_sanitize_content(cur, _TXT_MAX)))
    out = padded
    return out[:cap]


def _text_offer_buckets(s: str) -> set[str]:
    x = str(s or "").lower()
    out: set[str] = set()
    if "кредит" in x or "автокредит" in x:
        out.add("credit")
    if "одобр" in x:
        out.add("approval")
    if re.search(r"плат[её]ж|/мес|\bот\s+\d[\d\s]*\s*(?:₽|руб)", x):
        out.add("payment")
    if re.search(r"перв\w*\s+взнос|без\s+перв", x):
        out.add("first_payment")
    if "каско" in x or "подар" in x or "шин" in x or "комплект" in x:
        out.add("gift")
    if "трейд" in x or "обмен" in x:
        out.add("tradein")
    if "господдерж" in x or "госпрограм" in x:
        out.add("support")
    if "скид" in x or "выгод" in x or "%" in x:
        out.add("discount")
    if "тест" in x:
        out.add("testdrive")
    if "налич" in x:
        out.add("availability")
    return out


def _diverse_text_offers(candidates: list[str], limit: int = 3) -> list[str]:
    """Выбрать тексты без повторения одинаковых УТП внутри одного комбинаторного объявления."""
    clean = [str(x or "").strip() for x in (candidates or []) if str(x or "").strip()]
    out: list[str] = []
    used: set[str] = set()
    for x in clean:
        b = _text_offer_buckets(x)
        if b and used.intersection(b):
            continue
        out.append(x)
        used.update(b)
        if len(out) >= limit:
            return out
    fallback = [
        "Кредит на авто от 9 000 ₽/мес. Подберем условия от 15 банков. Одобрение за 1 час.",
        "Кредит без первого взноса на новое авто. Одобрение за 1 день. 15 банков онлайн.",
        "КАСКО на 1 год бесплатно при покупке в кредит. Ключи в день покупки. Одобрение.",
        "Трейд-ин выше рынка. Оценим авто за 30 минут и зачтем в счет нового кредита.",
    ]
    for x in fallback:
        b = _text_offer_buckets(x)
        if b and used.intersection(b):
            continue
        out.append(x)
        used.update(b)
        if len(out) >= limit:
            return out
    for x in clean:
        if x not in out:
            out.append(x)
        if len(out) >= limit:
            break
    return out


def _fallback_master_titles(brand: str, city: str, site_type: str, limit: int = 5) -> list[str]:
    """Безопасный добор для tp6/tp7, если строгие фильтры вычистили все заголовки."""
    brand = str(brand or "").strip()
    city = str(city or "").strip()
    if brand:
        raw = [
            # все строки с цифрой; «автокредит» в заголовках не блокируется, но в текстах — да
            f"{brand} в кредит. Первый взнос 0 ₽",
            f"Купить {brand} в кредит от 15 банков",
            f"{brand} с КАСКО на 1 год бесплатно",
            f"{brand} в наличии. Кредит от 9 000 ₽/мес",
            f"Одобрение за 30 минут онлайн. {brand} в кредит",
            f"{brand} по госпрограмме 2026. Взнос 0 ₽",
        ]
    else:
        raw = [
            # все строки с цифрой; разные УТП-бакеты
            "Авто в кредит. Первый взнос 0 ₽",
            "Новые авто в наличии. Кредит от 9 000 ₽/мес",
            "Автокредит от 15 банков. Решение онлайн",
            "КАСКО на 1 год бесплатно при кредите",
            "Господдержка 2026. Авто в кредит от 15 банков",
            "Трейд-ин выше рынка. Платеж от 9 000 ₽/мес",
        ]
    _, cities_bl = _title2_blocklist()
    out: list[str] = []
    seen: set[str] = set()
    for t in _replace_foreign_city(_drop_used_car(raw, site_type), city, cities_bl):
        s = _trim_to_word(_sanitize_content(_strip_credit_rate(str(t)), 56), 56).rstrip()
        if not s or _is_bad_start(s) or _bad_ad_title(s):
            continue
        nk = _variant_norm_key(s)
        if nk and nk in seen:
            continue
        if nk:
            seen.add(nk)
        out.append(s)
        if len(out) >= limit:
            break
    # Этаж-гарант: если бренд-вариант вычистился в ноль (бренд-токен абсурден, напр. источник
    # «Авито»/«Дром» по ошибке прилетел как brand) — отдаём БЕЗ-брендовые общие заголовки.
    # Они всегда проходят фильтры → tp6/tp7 НИКОГДА не падает «нужен хотя бы один заголовок».
    if not out and brand:
        return _fallback_master_titles("", city, site_type, limit)
    return out


# ── АНТИ-AI ПРАВИЛА (4 штуки) ────────────────────────────────────────────────
# ПРАВИЛО 1: Блэклист AI-штампов — слова, делающие текст «генерёнкой».
_AI_STAMP_WORDS = {
    "широкий выбор", "большой выбор", "удобный", "надёжный", "уникальный",
    "инновационный", "безупречный", "высокое качество", "лучший выбор",
    "не упустите", "узнайте больше", "идеальный", "профессиональный",
    "современный", "передовой", "исключительный", "выгодное предложение",
}


def _has_stamp(text: str) -> bool:
    """True если текст содержит хотя бы один AI-штамп (регистронезависимо)."""
    tl = str(text).lower()
    return any(w in tl for w in _AI_STAMP_WORDS)


# ПРАВИЛО 2: Пул коротких ударных заголовков (<5 слов) для чередования ритма.
_SHORT_TITLE_POOL = [
    "Автомобили в кредит. Одобрение за 30 минут. КАСКО",   # 49 симв; б/у «без переплат» убрано
    "Новые авто. Выгодный кредит",
    "Трейд-ин на новое авто. Оценка машины за 30 минут",   # 49 симв; б/у «Сдай старый» убрано
    "Авто в наличии. Выгодно",
    "Автомобили в наличии. Первый взнос 0 ₽. Выбор онлайн",  # 52 симв; б/у «Купи авто» убрано
]


def _alternate_rhythm(titles: list) -> list:
    """Если все заголовки одной длины (±1 слово) — переставить для чередования
    коротких (<5 слов) и длинных (>6 слов). Если коротких нет — добрать из
    _SHORT_TITLE_POOL. Возвращает список той же длины (порядок может измениться)."""
    if len(titles) < 2:
        return titles
    counts = [len(t.split()) for t in titles]
    mn, mx = min(counts), max(counts)
    if mx - mn > 1:          # уже разнобой — не трогаем
        return titles
    # все одной длины — нужно чередование
    short = [t for t in titles if len(t.split()) < 5]
    long_ = [t for t in titles if len(t.split()) >= 5]
    # добрать коротких из пула если не хватает
    for s in _SHORT_TITLE_POOL:
        if len(short) >= (len(titles) // 2):
            break
        if s not in titles and not _bad_ad_title(s) and not _is_bad_start(s):
            short.append(s)
    # чередуем: short, long, short, long …
    result, si, li = [], 0, 0
    toggle = True
    while len(result) < len(titles):
        if toggle and si < len(short):
            result.append(short[si]); si += 1
        elif not toggle and li < len(long_):
            result.append(long_[li]); li += 1
        elif si < len(short):
            result.append(short[si]); si += 1
        elif li < len(long_):
            result.append(long_[li]); li += 1
        else:
            break
        toggle = not toggle
    # если итог короче (пул не смог добить) — дополнить оригиналами
    used = set(id(x) for x in result)
    for t in titles:
        if len(result) >= len(titles):
            break
        if id(t) not in used:
            result.append(t)
    return result[:len(titles)]


def _dedup_by_first_word(titles: list) -> list:
    """ПРАВИЛО 3: Не более 2 заголовков с одинаковым первым словом.
    Бренд-заголовки (BAIC/Haval/Lada/…) естественно начинаются с марки — жёсткое ≤1
    ограничивало набор до 3-4 вместо 7. Лимит 2 сохраняет разнообразие и не выкидывает
    половину бренд-шаблонов."""
    seen_first: dict = {}   # first_word → count
    out = []
    for t in titles:
        first = str(t).split()[0].lower().rstrip(".,!?") if t else ""
        cnt = seen_first.get(first, 0)
        if first and cnt >= 2:
            continue
        if first:
            seen_first[first] = cnt + 1
        out.append(t)
    return out


def _has_number(text: str) -> bool:
    """ПРАВИЛО 4: True если в тексте есть хотя бы одна цифра."""
    return bool(re.search(r"\d", str(text)))


# ── конец анти-AI правил ──────────────────────────────────────────────────────

# «Хвосты»-УТП для добивки коротких заголовков до 45-56 симв. (правило Семёна: заполнять по максимуму).
# ⛔ БЕЗ %-ставки кредита/рассрочки (правило: «нельзя указывать % ставку кредита»).
# Sorted longest-first: при добивке до 48+ сначала пробуем самые длинные хвосты (плотнее заполняем).
_TITLE_TAILS = ("одобрение за 5 минут", "трейд-ин выше рынка", "без первого взноса",
                "подарки от салона", "одобрение онлайн", "КАСКО в подарок",
                "успей по акции", "авто в наличии", "господдержка")

_BAD_AD_TITLE_RE = re.compile(
    r"(?i)(авито|автосалон/салон|/(?!\s*мес)|низкая\s+ставка|"
    r"скидк\w*\s+до\s+-?\d+\s*%|"
    r"выгод\w*\s+до\s+\d[\d\s\u00a0]*\s*(?:₽|руб))"
)
_BAD_AD_TEXT_RE = re.compile(
    r"(?i)(автокредит|скидк\w*\s+до\s+-?\d+\s*%|выгод\w*\s+до\s+-?\d+\s*%|госпрограмм\w*\s+до\s+-?\d+\s*%|"
    r"выгод\w*\s+до\s+\d[\d\s\u00a0]*\s*(?:₽|руб)|скидк\w*\s+до\s+\d[\d\s\u00a0]*\s*(?:₽|руб))"
)
_BAD_CONTENT_RE = re.compile(
    r"(?i)(\bосаго\b|комисс\w*\s+по\s+кредит|скрыт\w*\s+комисс|"
    r"(?:госпрограмм|господдержк)\w*.*в\s+подарок|в\s+подарок.*(?:госпрограмм|господдержк)\w*|"
    r"\bот\s*0\s*(?:₽|руб\.?|рублей)?\s*(?:/мес|в\s+месяц)|"
    r"нов\w+\s+авто\s+от\s*0\s*(?:₽|руб\.?|рублей)?\s+в\s+месяц|"
    r"перв\w*\s+взнос\s+9\s*000)"
)
_LOW_MONTHLY_PAYMENT_RE = re.compile(r"(?i)(?:от\s*)?(\d[\d\s\u00a0]{2,})\s*(?:₽|руб)?\s*/\s*мес")
_LOW_PAYMENT_TEXT_RE = re.compile(r"(?i)плат[её]ж\w*\s+от\s+(\d[\d\s\u00a0]{2,})\s*(?:₽|руб)")
_PAYMENT_VALUE_RE = re.compile(
    r"(?i)((?:ежемесячн\w+\s+)?плат[её]ж\w*\s+от\s+|(?:авто)?кредит\w*\s+от\s+|от\s+)"
    r"(\d[\d\s\u00a0]{2,})"
    r"(\s*(?:₽|руб\.?|рублей)?(?:\s*/\s*мес|\s+в\s+месяц)?)"
)
_CREDIT_PAYMENT_RANGE_RE = re.compile(
    r"(?i)\b((?:авто)?кредит\w*|плат[её]ж\w*)\b"
    r"[^.!?\n]{0,24}?\bот\s+(\d[\d\s\u00a0]{2,})\s*(?:₽|руб\.?|рублей)?\s*(?:/\s*мес|\bв\s+месяц\b)"
)


def _payment_value(m) -> int:
    return int(re.sub(r"\D", "", m.group(2)) or 0)


def _bad_credit_payment_range(s: str) -> bool:
    """Ежемесячный кредитный платёж в объявлениях держим в реалистичном коридоре 9-15 тыс."""
    for m in _CREDIT_PAYMENT_RANGE_RE.finditer(str(s or "")):
        n = int(re.sub(r"\D", "", m.group(2)) or 0)
        if n and not (9000 <= n <= 15000):
            return True
    return False


def _payment_amounts(lines: list[str]) -> list[str]:
    vals: list[str] = []
    for s in lines or []:
        for m in _CREDIT_PAYMENT_RANGE_RE.finditer(str(s or "")):
            n = int(re.sub(r"\D", "", m.group(2)) or 0)
            if 9000 <= n <= 15000:
                vals.append(m.group(2))
    return vals


def _apply_payment_amount(s: str, pay: str | None) -> str:
    if not pay or not s:
        return s

    def _rp(m):
        n = int(re.sub(r"\D", "", m.group(2)) or 0)
        if 9000 <= n <= 15000:
            return m.group(0).replace(m.group(2), pay, 1)
        return m.group(0)

    return _CREDIT_PAYMENT_RANGE_RE.sub(_rp, str(s))


def _coherent_payments(titles: list, texts: list, sitelinks: list) -> tuple[list, list, list, bool]:
    """Один ежемесячный платеж на всю UAC-кампанию: заголовки + тексты + быстрые ссылки."""
    flat = [str(x or "") for x in (titles or [])] + [str(x or "") for x in (texts or [])]
    for s in sitelinks or []:
        if isinstance(s, dict):
            flat.append(str(s.get("title") or ""))
            flat.append(str(s.get("description") or ""))
    vals = _payment_amounts(flat)
    if not vals:
        return titles, texts, sitelinks, False
    canon = vals[0]
    nt = [_apply_payment_amount(t, canon) for t in (titles or [])]
    nx = [_apply_payment_amount(x, canon) for x in (texts or [])]
    ns = [{"title": _apply_payment_amount(s.get("title", ""), canon),
           "description": _apply_payment_amount(s.get("description", ""), canon)}
          for s in (sitelinks or []) if isinstance(s, dict)]
    changed = (nt != list(titles or [])) or (nx != list(texts or [])) or (ns != list(sitelinks or []))
    return nt, nx, ns, changed


def _discount_pcts(lines: list[str]) -> list[str]:
    """Процентные скидки/выгоды в контенте. Для дублей расширений достаточно числа процента."""
    out: list[str] = []
    for s in lines or []:
        for m in _PCT_DISC_RE.finditer(str(s or "")):
            v = m.group(1)
            if v not in out:
                out.append(v)
    return out


def _dominant_discount_pct(lines: list[str]) -> str:
    """Most frequent percent in generated content, preserving first-seen tie order."""
    vals = []
    for s in lines or []:
        vals.extend(m.group(1) for m in _PCT_DISC_RE.finditer(str(s or "")))
    if not vals:
        return ""
    counts = {v: vals.count(v) for v in dict.fromkeys(vals)}
    return max(counts, key=counts.get)


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


def _bad_ad_title(s: str) -> bool:
    """Фразы, которые не должны попадать в заголовки: названия общих тем, проценты/0₽/слэши."""
    s = str(s or "")
    from . import ai_agents as A
    if A.has_forbidden_claim(s):
        return True
    if _BAD_CONTENT_RE.search(s):
        return True
    if _bad_credit_payment_range(s):
        return True
    if re.search(r"(?i)\bбез\s+переплат\b", s):
        return True
    if re.search(r"(?i)\bкассов\w*\s+взрыв\w*\b", s):
        return True
    if re.search(r"(?i)\bтрейд-?ин\b[^.]{0,24}\b(?:1[0-9]{2}|[2-9][0-9]{2})\s*%", s):
        return True
    if re.search(r"(?i)\bбез\s+документ", s):
        return True
    if re.search(r"\s[-–—]\s|(?<!\d)-\d+\s*%", s):
        return True
    if re.search(r"(?i)госпрограмм\w*.*в\s+подарок|в\s+подарок.*госпрограмм\w*", s):
        return True
    if re.search(r"(?i)\bкредит\w*\b[^.]{0,24}\bдо\s+\d{1,2}\s*%\s+скидк", s):
        return True
    if re.search(r"(?i)\bкредит\w*\b[^.]{0,28}\bдо\s+\d{1,2}\s*%(?!\s*(?:год|лет|месяц))", s):
        return True
    if re.search(r"(?i)\bкредит\s+на\b[^.]{0,36}\bдо\s+\d{1,2}\s*%", s):
        return True
    if re.search(r"(?i)\b(?:безопасн\w+\s+сделк|взнос\s+отсутствует)\b", s):
        return True
    if re.search(r"(?i)\bусловия\s+кредитован\w*\b[^.]{0,24}\bдо\s+\d[\d\s\u00a0]{4,}\s*(?:₽|руб)", s):
        return True
    if re.search(r"(?i)\bкредит\s+и\s+шин\w+\s+на\s+1\s+сезон\b", s):
        return True
    if re.search(r"(?i)\b(?:резин\w*|шин\w+)\b[^.]{0,28}\bна\s+1\s+сезон\b", s):
        return True
    if re.search(r"(?i)\bтрейд-?ин\s+при\s+покупк[еаи]\b", s):
        return True
    if re.search(r"(?i)\bтрейд-?ин\b.*\bпри\s+покупк[еаи]\b", s):
        return True
    if re.search(r"(?i)(^|[.!?]\s*)(со\s+скидк\w*|скидки\s+месяца|акци[яи])\s*$", s.strip()):
        return True
    return bool(_BAD_AD_TITLE_RE.search(s))


def _bad_ad_text(s: str) -> bool:
    """Фразы, которые не должны попадать в тексты: непроверяемые скидки/выгоды до N%/N руб."""
    s = str(s or "")
    from . import ai_agents as A
    return (A.has_forbidden_claim(s) or _bad_credit_payment_range(s)
            or bool(re.search(r"(?i)\bбез\s+переплат\b", s))
            or bool(re.search(r"(?i)\bкассов\w*\s+взрыв\w*\b", s))
            or bool(re.search(r"(?i)\bсрочно\s+прода[её]м\b|\bпозвоните\s+за\s+скидк", s))
            or bool(re.search(r"(?i)\b(?:безопасн\w+\s+сделк|взнос\s+отсутствует)\b", s))
            or bool(re.search(r"(?i)\bкредит\w*\b[^.]{0,28}\bдо\s+\d{1,2}\s*%(?!\s*(?:год|лет|месяц))", s))
            or bool(re.search(r"(?i)перв(?:ый|ого)\s+взнос\w*\s+0\s*%", s))
            or bool(re.search(r"(?i)ваш\s+нов\w+\s+автомобил\w+\s+жд[её]т|распродаж\w+\s+месяц\w+\s+стартовал", s))
            or bool(_BAD_CONTENT_RE.search(s)) or bool(_BAD_AD_TEXT_RE.search(s)))


def _bad_ad_sitelink(title: str, description: str = "") -> bool:
    """Фразы, которые не должны попадать в быстрые ссылки UAC/ЕПК."""
    s = f"{title or ''} {description or ''}"
    from . import ai_agents as A
    if A.has_forbidden_claim(s):
        return True
    if _BAD_CONTENT_RE.search(s):
        return True
    if re.search(r"(?i)\bбез\s+переплат\b", s):
        return True
    title_l = (title or "").strip().lower()
    desc_l = (description or "").strip().lower()
    if re.search(r"(?i)\b(запишитесь|записаться|запись)\b.*тест-драйв|тест-драйв.*\b(запишитесь|записаться)\b", title_l):
        return True
    if title_l in {"тест-драйв", "запишитесь на тест-драйв", "запись на тест-драйв",
                   "тест-драйв онлайн", "тест драйв онлайн"}:
        return True
    if title_l.startswith("запишитесь") and "тест-драйв" in desc_l:
        return True
    if re.search(r"(?i)\bкредит\s+и\s+шин\w+\s+на\s+1\s+сезон\b", s):
        return True
    if re.search(r"(?i)\b(?:резин\w*|шин\w+)\b[^.]{0,28}\bна\s+1\s+сезон\b", s):
        return True
    return bool(re.search(
        r"(?i)(авито|автосалон/салон|/(?!\s*мес)|низкая\s+ставка|"
        r"выгод\w*\s+до\s+\d[\d\s\u00a0]*\s*(?:₽|руб)|"
        r"скидк\w*\s+до\s+\d[\d\s\u00a0]*\s*(?:₽|руб)|кешбэк|cashback)",
        s,
    ))


def _fill_title(t: str, lo: int = 45, hi: int = 56) -> str:
    """Дотянуть заголовок до lo-hi симв., подклеивая УТП-хвосты «. …» БЕЗ обрезки слов и БЕЗ
    повтора уже упомянутого УТП. Если ни один хвост не влезает - оставляем как есть (но ≤hi).
    Разделитель — точка (правило Кудерко: дефис как разделитель частей фразы недопустим)."""
    t = _strip_dangling_word_tail(_trim_to_word(str(t), hi)).rstrip(" -—·.,")
    for f in _TITLE_TAILS:
        if len(t) >= hi - 8:          # правило Семёна: свободно ≤8 симв (hi-8 = 48 при hi=56)
            break
        kw = f.split()[0].lower().rstrip("%")
        if kw and kw in t.lower():
            continue                                 # не дублируем уже упомянутое (кредит/КАСКО/трейд-ин…)
        if len(t) + 2 + len(f) <= hi:
            t = f"{t}. {_cap_first(f)}"
    return _normalize_numeric_suffixes_bp(
        _strip_dangling_word_tail(_trim_to_word(t, hi)).rstrip(" -—·.,")
    )


def _brand_title_set(brand: str, city: str) -> list:
    """≤7 ПОЛНЫХ «вкусных» заголовков для группы по МАРКЕ - КАЖДЫЙ содержит марку И УТП (кредит,
    трейд-ин, КАСКО, скидка, выгодный кредит), а не бледное «BAIC». ЗАПОЛНЯЕМ длину 45-56 (правило Семёна:
    не оставлять места) - короткие добиваем УТП-хвостами через _fill_title.
    БАГ 9: кредитные УТП приоритетом (первый взнос, платеж, ставка, господдержка).
    БАГ 4→исправлен: разделитель «. » (точка), не дефис. БАГ 7: «0%» убрано (strip_credit_rate уберёт 0%).
    Не пишем «официального дилера» (правило пользователя). Длина ≤56.
    Мультигород (город через запятую) → без города (_content_city)."""
    city = _content_city(city)
    brand = _display_brand(brand)
    city_loc = _city_locative((city or "").strip()) if (city or "").strip() else ""
    loc = f" в {city_loc}" if city_loc else ""
    cand = [
        f"Кредит на {brand}{loc}. Первый взнос 0 ₽",           # начало: «Кредит на»
        f"Купить {brand}{loc}. КАСКО на 1 год бесплатно",       # начало: «Купить»
        f"{brand}{loc}. Платеж от 9 000 ₽/мес",                 # начало: марка (1-е из 2 допустимых; #23)
        f"Новый {brand}{loc}. Одобрение за 30 минут",           # начало: «Новый» (#23)
        f"Трейд-ин {brand}{loc}. Оценка авто за 30 минут",      # начало: «Трейд-ин» (#23)
        f"Авто {brand}{loc}. Выгода до 45% при покупке",        # начало: «Авто» (#23)
        f"{brand}{loc}. Кредит от 15 банков онлайн",             # начало: марка (2-е из 2 допустимых; #23)
        f"Госпрограмма {brand}{loc}. Кредит 2026",              # начало: «Госпрограмма» (#23)
    ]
    out: list = []
    for t in cand:
        ft = _fill_title(_strip_credit_rate(t), 45, 56)   # убрать %-ставку, потом добить до 45-56
        if ft and ft not in out:
            out.append(ft)
    return out[:8] or [f"Новые {brand}{loc}"[:56], brand]   # 8 = все #23-шаблоны, вкл. «Госпрограмма»


# Стемы крупных городов РФ (для матча в склонениях: «москв»→москва/москве; «казан»→казань/казани).
# Дополняет города из local_gsheet_sites — ловит ключи слепка с городами, где нет наших аккаунтов.
_RU_CITY_STEMS = (
    "москв", "петербург", "новосибирск", "екатеринбург", "казан", "нижний новгород", "челябинск",
    "самар", "омск", "ростов", "уф", "красноярск", "воронеж", "перм", "волгоград", "краснодар",
    "саратов", "тюмен", "тольятти", "ижевск", "барнаул", "ульяновск", "иркутск", "хабаровск",
    "ярославл", "владивосток", "махачкал", "томск", "оренбург", "кемеров", "новокузнецк", "рязан",
    "астрахан", "пенз", "липецк", "тул", "киров", "чебоксар", "калининград", "курск", "сочи",
    "ставропол", "тверь", "магнитогорск", "иванов", "брянск", "сургут", "белгород", "владимир",
    "архангельск", "калуг", "смоленск", "вологд", "курган", "мурманск", "тамбов", "новгород",
    "кострома", "нижневартовск", "таганрог", "сыктывкар", "нальчик", "новороссийск",
)


def _drop_foreign_city_keywords(keywords: list, own_city: str) -> list:
    """Выкинуть ключи с упоминанием ЧУЖОГО города (не города рекламирования) — правило пользователя:
    «changan волгоград» в кампании Кемерово → отбросить. Источник городов: local_gsheet_sites +
    стемы крупных городов РФ. Свой город НЕ трогаем (ключ «changan кемерово» остаётся)."""
    _, acc_cities = _title2_blocklist()
    own = (own_city or "").strip().lower()
    if not own:
        # #6 review: без своего города «чужой» определить НЕЛЬЗЯ — иначе (own="") ВСЕ городские стемы
        # попадают в foreign и вырезают ВСЕ гео-ключи группы (баг: tp2/tp4 остались без ключевых фраз).
        return list(keywords or [])
    own5 = own[:5]
    foreign = [c for c in (set(_RU_CITY_STEMS) | (acc_cities or set()))
               if c and c not in own and (not own5 or own5 not in c)]
    out = []
    for k in (keywords or []):
        kl = str(k).lower()
        if any(re.search(r"\b" + re.escape(c), kl) for c in foreign):
            continue
        out.append(k)
    return out


def _rsya_titles(brand: str, city: str, site_type: str, ai_title2: str = "",
                 base: list | None = None, pool: list | None = None, is_brand: bool = True,
                 cap: int = _RA_TITLES_CAP) -> list:
    """≤cap заголовков комбинаторного РСЯ. Группа по МАРКЕ (is_brand) → ВСЕ заголовки ПОЛНЫЕ и с
    маркой (`_brand_title_set`), без бледных дженериков; пул слепка — лишь добивка если не хватило.
    Группа «Общее» (тема, не марка) → бренд-шаблоны НЕ применяем, ведём пулом слепка. Чистка:
    не-Б/У сайт → без «б/у»; чужой город → город аккаунта; длина ≤56. Когерентность — в _responsive_ad."""
    _, _cities_bl = _title2_blocklist()
    acc_city = (city or "").strip()

    def _cf(lst):
        return _replace_foreign_city(_drop_used_car(list(lst or []), site_type), acc_city, _cities_bl)

    if brand and is_brand:
        branded_base = [t for t in (list(base or [])) if t and _brand_in_text(t, brand)]
        branded_pool = [t for t in (list(pool or [])) if t and _brand_in_text(t, brand)]
        branded_ai = [str(ai_title2)[:56]] if (ai_title2 and _brand_in_text(ai_title2, brand)) else []
        primary = _brand_title_set(brand, city) + branded_base + branded_ai
        supp = branded_pool + _GENERIC_TITLE_FILLERS   # только если шаблонов < cap после дедупа
    else:
        primary = [t for t in (list(base or [])) if t]
        if ai_title2:
            primary.append(str(ai_title2)[:56])
        supp = list(pool or []) + _GENERIC_TITLE_FILLERS
    primary = _drop_foreign_brand_mentions(primary, brand)
    supp = _drop_foreign_brand_mentions(supp, brand)
    titles = _fill_variants(_cf(primary), _cf(supp) + _GENERIC_TITLE_FILLERS, cap)
    # убрать %-ставку кредита + тире/дефис-разделитель → точка + добить до 45-56; дедуп по норм-ключу (БАГ 2).
    out: list = []
    seen_keys: set = set()
    for t in titles:
        if not (t and str(t).strip()):
            continue
        if _bad_ad_title(str(t)):
            continue
        # БАГ 8: не берём заголовки начинающиеся с предлога или маленькой буквы
        if _is_bad_start(str(t)):
            continue
        s = _normalize_numeric_suffixes_bp(
            _fill_title(_replace_sep_hyphen(_replace_emdash(_strip_credit_rate(str(t)))), 45, 56)
        )
        if not s:
            continue
        # БАГ 2: дедупликация по смысловому ключу (схлопывает числа: 57%==35%==скидка_до_#%)
        nk = _variant_norm_key(s)
        if nk and nk in seen_keys:
            continue
        if nk:
            seen_keys.add(nk)
        if s not in out:
            out.append(s)
    if brand and is_brand:
        own = _own_brand_tokens(brand)
        def _has_own_brand(v: str) -> bool:
            vl = str(v or "").lower()
            return any(re.search(r"(?<![a-zа-яё0-9])" + re.escape(tok) + r"(?![a-zа-яё0-9])", vl)
                       for tok in own)
        out = [t for t in out if _has_own_brand(t)]
    # АНТИ-AI ПРАВИЛО 1: фильтр штампов — пропускаем «широкий выбор», «надёжный» и т.п.
    # Если после фильтра не хватает — докидываем обратно нефильтрованные (не обнуляем набор).
    clean = [t for t in out if not _has_stamp(t)]
    if len(clean) < cap and len(clean) < len(out):
        stamps = [t for t in out if _has_stamp(t)]
        clean = (clean + stamps)[:cap]
    out = clean
    # АНТИ-AI ПРАВИЛО 3: не более 1 заголовка с одинаковым первым словом.
    out = _dedup_by_first_word(out)
    # АНТИ-AI ПРАВИЛО 2: чередование ритма (коротких/длинных) — только если набор полный (cap штук).
    if len(out) >= cap and not (brand and is_brand):
        out = _alternate_rhythm(out[:cap])
    if not (brand and is_brand) and len(out) < cap:
        for cand in (_GENERIC_AT_TITLES + _GENERIC_TITLE_FILLERS):
            s = _normalize_numeric_suffixes_bp(
                _fill_title(_replace_sep_hyphen(_replace_emdash(_strip_credit_rate(str(cand)))), 45, 56)
            )
            if not s or _bad_ad_title(s) or _is_bad_start(s):
                continue
            nk = _variant_norm_key(s)
            if any(_variant_norm_key(x) == nk for x in out):
                continue
            out.append(s)
            if len(out) >= cap:
                break
    # Брендовая/модельная группа: первый заголовок обязан быть с этой маркой/моделью, а набор
    # должен добиваться до cap даже если AI-контент уровня кампании не содержал бренд и был
    # полностью вычищен фильтрами. Иначе tp1/tp2 могут схлопнуться до `[title, brand]`.
    if brand and is_brand:
        own = _own_brand_tokens(brand)

        def _has_own_brand_final(v: str) -> bool:
            vl = str(v or "").lower()
            return any(re.search(r"(?<![a-zа-яё0-9])" + re.escape(tok) + r"(?![a-zа-яё0-9])", vl)
                       for tok in own)

        brand_fillers = _brand_title_set(brand, city) + [
            f"{brand} в наличии. Кредит и оценка авто в трейд-ин",
            f"Оформите {brand} в кредит. Первый взнос 0 ₽",
            f"{brand} с выгодой по кредиту. Заявка онлайн",
            f"Купить {brand} в кредит. Решение банка за 30 минут",
            f"{brand} в кредит. КАСКО на 1 год при покупке",
        ]

        seed = ""
        for cand in brand_fillers:
            s = _normalize_numeric_suffixes_bp(
                _fill_title(_replace_sep_hyphen(_replace_emdash(_strip_credit_rate(str(cand)))), 45, 56)
            )
            if s and _has_own_brand_final(s) and not _bad_ad_title(s) and not _has_stamp(s):
                seed = s
                break

        if seed:
            seed_key = _variant_norm_key(seed)
            out = [seed] + [t for t in out if t != seed and _variant_norm_key(t) != seed_key]

        seen_keys = {_variant_norm_key(x) for x in out if _variant_norm_key(x)}
        for cand in brand_fillers:
            s = _normalize_numeric_suffixes_bp(
                _fill_title(_replace_sep_hyphen(_replace_emdash(_strip_credit_rate(str(cand)))), 45, 56)
            )
            if (not s or _bad_ad_title(s) or _is_bad_start(s) or not _has_own_brand_final(s)):
                continue
            nk = _variant_norm_key(s)
            if nk and nk in seen_keys:
                continue
            out.append(s)
            if nk:
                seen_keys.add(nk)
            if len(out) >= cap:
                break
    return out[:cap]


def _build_tp1_from_pack(
    token: str,
    login: str,
    campaign_id: int,
    slepok: str,
    site_type: str,
    region_ids: list,
    href: str,
    r_code: str,
    titles: list | None,
    texts: list,
    counter_id: int = 0,
    feed_id: int = 0,
    with_shopping: bool = False,
    feed_models: dict | None = None,
    segment: str | None = None,
    ai_title2: str = "",
    city: str = "",
    autotarget: bool = False,
    products_only: bool = False,
    tp_code: str = "tp1",
    sitelinks: list | None = None,
    grid_cookie: str | None = None,
) -> dict:
    """Наполнить РСЯ (tp1/tp5 ЕПК) бренд-группами из пака M3.

    tp_code: код пака M3 для gather() — 'tp1' для РСЯ-кампаний, 'tp5' для комбинированных
    поисковых. По умолчанию 'tp1' (обратная совместимость).
    segment ('Марки'|'Модели'|None): фильтр ct-папок по сегменту (как в боевых аккаунтах —
    марки и модели РАЗНЫМИ кампаниями). None → все группы (старое поведение).
    Каждая ct-папка пака = отдельная группа. Имя группы = КАНОН CODER.md.
    Ключи/минус/уточнения/картинки — из пака. Объявления: TextAd + AdImageHash.
    Быстрые ссылки (sitelinks) — из direct_slepok_content слепка: создаём набор ОДИН раз,
    привязываем через SitelinkSetId ко ВСЕМ объявлениям группы.
    Callouts — из пака (per-бренд) через AdExtensions на объявление.
    """
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    pack = kp.gather(key, site_type, tp_code)  # один ssh-вызов к M3
    if not pack:
        # Пустой пак (у слепка нет tp_code-пака, напр. pavlov/tp5; M3 при этом жив — tp1-пак есть).
        # Для ТОВАРНОЙ ГАЛЕРЕИ по фиду это НЕ блокер: фид-товарка не зависит от бренд-пака →
        # проваливаемся в фид-фолбэк ниже (создаст товарную галерею по фиду). Иначе — честный скип.
        if not (with_shopping and feed_id):
            return {"skipped": "пак недоступен (мост M3?)"}
        pack = {}

    text0 = (texts[0] if texts else "")[:81] or "Официальный дилер. Тест-драйв и выгодные условия по кредиту. Авто в наличии."
    ct_model = kp.feeds_ct_model()            # фид-картиночный индекс (ct0020+, модели) — фолбэк
    ct_name = _ag_part1_map()                 # ct→имя из gsheet_naming (ag_part1, полное покрытие 318)
    _sc_titles = _slepok_campaign_content(slepok, site_type).get("titles") or []  # пул слепка — 1 раз на кампанию
    # URL страниц моделей: account-level мёрж (все фиды, как цены) → покрывает марки без URL
    # в конкретном feed_id (#ФИКС-8).
    _feed_urls_tp1 = _account_offer_urls(login, href)

    # Строим группы ТОЛЬКО для ct-папок у которых есть ключи scherbakova
    groups = []
    _img_rr = 0                                   # round-robin по пулу картинок ct (Павлов: «разбавить однотипными»)
    for ct in sorted(pack.keys()):
        data = pack.get(ct) or {}
        if not data.get("positive"):
            continue                           # пропускаем ct без ключей scherbakova
        if segment and _ct_segment(ct) != segment:
            continue                           # сегментный фильтр (Марки/Модели как в боевых)
        raw_brand = ct_name.get(ct) or ct_model.get(ct) or ct
        brand = _valid_pack_brand_name(ct, raw_brand)   # логический бренд: пустой для «Общее»
        group_label = _pack_group_display_name(ct, raw_brand, brand)
        group_name = _tp1_group_name(ct, r_code, group_label, with_shopping=with_shopping,
                                     autotarget=autotarget)
        # Картинки: общие ct0000-ct0014 → общий пул ct0000; кузова ct0015-ct0018 → свой ct;
        # модели/марки → свой ct.
        all_images = _creative_images_for_ct(site_type, tp_code, ct, key)
        # Ротация по пулу картинок (а не всегда [0]) — чтобы РСЯ-объявления не были однотипными.
        # image_path — первая из ротации (совместимость); image_paths — все (для мульти-upload в Фазе 3).
        image_path = all_images[_img_rr % len(all_images)] if all_images else None
        _img_rr += 1
        # deep-link: сначала реальный URL из фида (targetUrl), фолбэк на формульный слаг (#ФИКС-2).
        # ФИКС A: Марки → /auto/{brand} (первые 2 сегмента), Модели → полный путь без query. (#ФИКС-A)
        _raw_feed_url = _feed_url_for_model(_feed_urls_tp1, brand)
        if _raw_feed_url:
            model_href = (_brand_level_url(_raw_feed_url) if _ct_segment(ct) == "Марки"
                          else _strip_url_query(_raw_feed_url))
        else:
            model_href = _model_page_href(href, site_type, brand)
        # Title: шаблон «Новые {brand} в {город}. {акция}» (≤35 симв.) — фолбэк brand[:35].
        # ai_title2 — ИИ-заголовок (если дан), иначе round-robin из пула.
        is_brand_group = _ct_segment(ct) in ("Марки", "Модели")
        title = (_title_from_template(brand, city) if (is_brand_group and not ai_title2)
                 else (_GENERIC_AT_TITLES[0] if not is_brand_group else brand[:35]))
        ttl2 = (ai_title2[:30] if ai_title2 else _next_title2())   # ИИ-title2 или round-robin из пула
        # Внутри pack-групп не вызываем M3 per-ct: ИИ уже сгенерировал контент кампании/item.
        # Иначе tp1/tp5 на больших паках создаются неприемлемо долго и старт очереди "замирает".
        g_titles = _rsya_titles(brand, city, site_type, ai_title2=ai_title2,
                                base=(list(titles or []) + [title, ttl2] if is_brand_group
                                      else list(titles or []) + list(_GENERIC_AT_TITLES)),
                                pool=_sc_titles, is_brand=is_brand_group)
        g_texts = _rsya_texts(list(texts or []) + ([text0] if text0 else []), site_type, city, brand)
        groups.append({
            "name": group_name,
            "ct": ct,
            "brand": brand,
            # БАГ-13: для «Марки» — убрать ключи «марка+модель» (напр. «Chery Tiggo 8 Pro»)
            "keywords": _filter_group_keywords(data.get("positive", []), _ct_segment(ct), brand, city, site_type),
            "minus": data.get("minus", []),
            "titles": g_titles or ([title, brand] if brand else [title]),
            "texts": g_texts or ([text0] if text0 else []),
            "title": title, "title2": ttl2, "text": text0,   # совместимость
            "href": model_href,               # deep-link страницы модели
            "image_path": image_path,         # первая картинка (round-robin); совместимость
            "image_paths": all_images[:5],    # все пути (пак + Manual) — для мульти-upload в Фазе 3
            "callouts": data.get("callouts", []),
        })

    if not groups and with_shopping and feed_id:
        # Бренд-пак слепка для tp_code ПУСТ (напр. у pavlov нет tp5-пака), НО это товарная галерея
        # по фиду — фид-товарка (ShoppingAd/ListingAd) НЕ зависит от бренд-пака. Чтобы tp5/tp3 не
        # выходили пустыми, создаём ОДНУ товарную-галерею группу по всему фиду: автотаргет + общие
        # заголовки/тексты + товарные объявления (with_shopping ниже добавит ShoppingAd+ListingAd).
        groups = [{
            "name": "Товарная галерея", "ct": "ct0000", "brand": "",
            "keywords": [], "minus": [],
            "titles": list(_GENERIC_AT_TITLES),
            "texts": list(_GENERIC_TEXT_FILLERS),
            "title": _GENERIC_AT_TITLES[0], "title2": "",
            "text": (_GENERIC_TEXT_FILLERS[0] if _GENERIC_TEXT_FILLERS else ""),
            "href": href, "image_path": None, "callouts": [],
        }]
        autotarget = True                                 # товарная галерея по фиду = автотаргет (нет бренд-ключей)
    if not groups:
        return {"skipped": f"пак пуст: нет ct-папок с ключами scherbakova для {tp_code}"}

    # Быстрые ссылки: создаём набор ОДИН раз → SitelinkSetId на каждое объявление.
    # Важно: v5-only путь здесь молча оставлял tp1 без ссылок при пустом kind='sitelinks'.
    # Общий resolver берёт campaign-content слепка и умеет Grid/cookie fallback без баллов.
    sitelink_set_id = None
    base_sitelinks: list = []   # нормализованные ссылки для per-group наборов (#ФИКС-3)
    asset_warns = []
    try:
        if not sitelinks:
            sitelinks = _ai_common_sitelinks(login, slepok, site_type, city, tp_code)
        _assets = _resolve_campaign_assets(token, login, href, sitelinks=sitelinks,
                                           slepok=slepok, site_type=site_type, grid_cookie=grid_cookie)
        sitelink_set_id = _assets.get("sitelink_set_id")
        base_sitelinks = _assets.get("asset_sitelinks") or []
    except Exception as e:  # noqa: BLE001 — sitelinks не критичны, но должны быть видны в отчёте
        asset_warns.append(f"sitelinks(tp1): {str(e)[:120]}")

    # Callouts: создаём общий пул AdExtensions (уточнения из пака)
    co_pool = {}
    try:
        all_co = [c for g in groups for c in (g.get("callouts") or [])]
        co_pool = _ensure_callout_exts(token, login, all_co) if all_co else {}
        for g in groups:
            ids = [co_pool[c] for c in (g.get("callouts") or []) if c in co_pool]
            if ids:
                g["callout_ext_ids"] = ids[:4]
    except Exception:  # noqa: BLE001
        co_pool = {}

    rep = _build_tp1_adgroups(token, login, campaign_id, region_ids, href, groups,
                               sitelink_set_id=sitelink_set_id,
                               base_sitelinks=base_sitelinks or None,
                               feed_id=feed_id, with_shopping=with_shopping, feed_models=feed_models,
                               autotarget=autotarget, products_only=products_only,
                               grid_cookie=grid_cookie)
    rep["cts"] = len(pack)
    rep["groups_built"] = len(groups)
    rep["callouts_pool"] = len(co_pool)
    rep["sitelinks_set_id"] = sitelink_set_id
    if asset_warns:
        rep.setdefault("warnings", []).extend(asset_warns)
    # [задел на будущее] видео-объявления РСЯ — отдельный хук _tp1_video_ads (сейчас dormant:
    # видео для tp1 в паке M3 нет + creative-API ЕПК не подключён; картинки РСЯ уже работают).
    rep["video"] = "хук готов (_tp1_video_ads), dormant — нет видео в tp1 на M3 + нужен creative-API ЕПК"
    return rep


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


def _search_platforms(tp_code: str) -> dict:
    """Платформы поисковых мест показа (HAR 33/34): search=True, gallery/network=False.
    Динамика (tp4) = organic=True; чистый Поиск (tp2) = organic=False."""
    p = dict(_PLATFORMS_SEARCH_ONLY)
    p["organic"] = (str(tp_code or "").lower() == "tp4")
    return p


def _finalize_rsya(login: str, campaign_id: int, *, name: str, goal_id: int,
                   cpa_rub, weekly_rub, counter_ids: list, pay_for_conversion: bool,
                   callout_ids=None, sitelink_set_id=None, promo_id=None,
                   minus_set_ids=None, bid_modifiers: dict | None = None,
                   grid_cookie: str | None = None, disabled_places: list | None = None) -> list:
    """Grid-докрутка ЕПК tp1 (канал РСЯ): уточнения/быстрые ссылки/промо на уровне кампании +
    инварианты, СОХРАНЯЯ чистый РСЯ. Ключевое отличие от gf.GridClient.finalize (поиск-only):
    network-only + isOrganicSearchEnabled=False + placementTypes=[] — иначе grid отдаёт
    ORGANIC_PLACEMENT_TYPES_INVALID_COMBINATION (проверено live на porg-psm5h7q6, 2026-06-22).
    grid_finalize.py не трогаем — берём только его примитивы (_TEMPLATE/_MUTATION/CSRF/post)."""
    import datetime as _dt
    gc = gf.GridClient(login, cookie=grid_cookie)
    gc._bootstrap_csrf()
    uc = json.loads(json.dumps(gf._TEMPLATE))            # deepcopy HAR-шаблона
    uc["id"] = str(campaign_id)
    uc["name"] = name
    uc["strategyId"] = None
    uc["startDate"] = _dt.date.today().isoformat()       # шаблонная дата устаревает → ставим сегодня
    uc["metrikaCounters"] = [int(c) for c in (counter_ids or [])]
    uc["biddingStategyWithPlatforms"]["platforms"] = dict(_PLATFORMS_RSYA)
    uc["biddingStategyWithPlatforms"]["strategyData"] = {
        "goalId": str(goal_id), "avgCpa": str(int(cpa_rub)), "sum": str(int(weekly_rub)),
        "budgetType": "WEEKLY", "payForConversion": bool(pay_for_conversion),
        "payForShows": False, "isExplorationBudgetValueCustom": None,
        "minExplorationBudget": None,
    }
    uc["placementTypes"] = []                            # РСЯ: пустой список (НЕ None — иначе ORGANIC-конфликт)
    uc["disabledPlaces"] = list(disabled_places or [])   # #21 минус-площадки РСЯ (HAR45, нижний регистр)
    uc["isOrganicSearchEnabled"] = False                # органика ВЫКЛ — обязательно при пустом placement
    uc["bannerHrefParams"] = ""                          # UTM только на уровне групп (trackingParams), не кампании
    uc["inheritableCallouts"] = {"calloutIds": [str(i) for i in (callout_ids or [])]}
    uc["inheritableSitelinkSet"] = {"sitelinkSetId": str(sitelink_set_id) if sitelink_set_id else None}
    uc["promoExtensionId"] = str(promo_id) if promo_id else None
    uc["libraryMinusKeywordsIds"] = [str(i) for i in (minus_set_ids or [])]
    uc["bidModifiers"] = bid_modifiers or {}             # корректировки: Grid (HAR21) на куки-пути; {}=v5-ом ПОСЛЕ
    uc["isAlternativeTextsEnabled"] = False              # инвариант #3
    uc["hasSiteMonitoring"] = True                       # инвариант #4
    uc["hasExtendedGeoTargeting"] = False                # инвариант #5
    uc["isRecommendationsManagementEnabled"] = False     # инвариант #6
    uc["isPriceRecommendationsManagementEnabled"] = False
    uc["enableCompanyInfo"] = False                      # «Карты/Организация» НЕ включаем (шаблон шлёт True)
    r = gc._post("UpdateCampaigns", gf._MUTATION,
                 {"input": {"campaignUpdateItems": [{"unifiedCampaign": uc}]}, "login": login})
    data = r.json()
    res = (data.get("data") or {}).get("updateCampaigns") or {}
    vr = res.get("validationResult") or {}
    if data.get("errors") or vr.get("errors"):
        raise gf.GridFinalizeError("РСЯ-finalize: " + json.dumps(
            data.get("errors") or vr.get("errors"), ensure_ascii=False)[:400])
    return res.get("updatedCampaigns") or []


_MINUS_LIB_Q = ("query MinusPhraseLibrary($input:GdGetMinusKeywordsPacksInput!){reqId:getReqId "
                "getLibraryMinusKeywordsPacks(input:$input){rowset{id name minusKeywords}totalCount}}")


_GRID_MINUS_PACK_CACHE: dict = {}                         # (login,marker) → (pack_id|None, ts) — аккаунт-стабилен
_GRID_CALLOUTS_CACHE: dict = {}                           # login → (by_text:dict, ts) — аккаунт-стабилен
_GRID_ACCOUNT_TTL = 20 * 60                               # как _OFFER_PRICE_TTL: за джобу не меняется


def _grid_minus_pack_id(login: str, name_marker: str = "Минуса общие") -> int | None:
    """Grid (БЕЗ баллов): id набора минус-фраз по маркеру имени («Минуса общие»). HAR40
    MinusPhraseLibrary → getLibraryMinusKeywordsPacks. Куки-фолбэк к v5 negativekeywordsharedsets
    (требуют баллов). None если набора нет/сбой. Кэш per-(login,marker) — id аккаунт-стабилен,
    не дёргаем Grid+CSRF на каждую кампанию."""
    key = (login, name_marker)
    hit = _GRID_MINUS_PACK_CACHE.get(key)
    if hit and (time.time() - hit[1]) < _GRID_ACCOUNT_TTL:
        return hit[0]
    try:
        gc = gf.GridClient(login)
        gc._bootstrap_csrf()
        r = gc._post("MinusPhraseLibrary", _MINUS_LIB_Q, {"input": {}})
        if r.status_code == 403:
            r = gc._post("MinusPhraseLibrary", _MINUS_LIB_Q, {"input": {}})
        rows = ((r.json().get("data") or {}).get("getLibraryMinusKeywordsPacks") or {}).get("rowset") or []
        marker = (name_marker or "").lower()
        named = [x for x in rows if marker and marker in str(x.get("name") or "").lower()]
        pool = named or rows
        best = max(pool, key=lambda x: len(x.get("minusKeywords") or [])) if pool else None   # самый полный набор
        pack_id = int(best["id"]) if (best and best.get("id")) else None
        _GRID_MINUS_PACK_CACHE[key] = (pack_id, time.time())
        return pack_id
    except Exception:  # noqa: BLE001 — best-effort, кампанию не валим
        return None


_CALLOUTS_Q = ("query Callouts($login:String!){reqId:getReqId callouts(input:{searchBy:{login:$login}"
               "filter:{deleted:false}}){clientId id text statusModerate}}")


def _grid_callout_ids(login: str, texts: list | None = None, limit: int = 4) -> list:
    """Grid (БЕЗ баллов): id уточнений аккаунта по текстам (HAR40 Callouts). Куки-фолбэк к v5, когда
    после 152 уточнения не читаются/не цепляются. texts пусто → первые limit уточнений. → список id(str).
    Карта by_text аккаунт-стабильна → кэшируем per-login (Grid+CSRF не на каждую кампанию)."""
    try:
        _hit = _GRID_CALLOUTS_CACHE.get(login)
        if _hit and (time.time() - _hit[1]) < _GRID_ACCOUNT_TTL:
            by_text = _hit[0]
        else:
            gc = gf.GridClient(login)
            gc._bootstrap_csrf()
            r = gc._post("Callouts", _CALLOUTS_Q, {"login": login})
            if r.status_code == 403:
                r = gc._post("Callouts", _CALLOUTS_Q, {"login": login})
            rows = ((r.json().get("data") or {}).get("callouts")) or []
            by_text = {}
            for c in rows:
                t = str(c.get("text") or "").strip().lower()
                if t and c.get("id") and t not in by_text:
                    by_text[t] = str(c["id"])
            _GRID_CALLOUTS_CACHE[login] = (by_text, time.time())
        wanted = [str(t).strip().lower() for t in (texts or []) if str(t).strip()]
        ids: list[str] = []
        for t in wanted:
            if t in by_text and by_text[t] not in ids:
                ids.append(by_text[t])
            if len(ids) >= limit:
                break
        if not ids:                                      # тексты не дали совпадений (или их нет) —
            # #24: единый семантический дедуп — ТА ЖЕ функция, что и v5-путь (одна точка правды,
            # не два расходящихся инлайн-цикла). by_text — уже {text: id}, ровно вход _dedup_callout_ids.
            ids = _dedup_callout_ids(by_text, cap=limit)
        return ids
    except Exception:  # noqa: BLE001 — best-effort
        return []


def _finalize_search_via_grid(login: str, campaign_id: int, *, name: str, goal_id: int,
                              cpa_rub, weekly_rub, counter_ids: list, pay_for_conversion: bool,
                              callout_ids=None, sitelink_set_id=None, promo_id=None,
                              minus_set_ids=None, bid_modifiers: dict | None = None,
                              placement_types: list[str] | None = None,
                              platforms: dict | None = None) -> list:
    """Grid-докрутка ЕПК tp2/tp4 (канал Поиск): инварианты #3/#4/#5/#6 + ассеты кампании + МЕСТА
    ПОКАЗА. platforms: HAR 33/34 — tp2 `_search_platforms('tp2')` (organic=False), tp4 (organic=True);
    дефолт (None) = `gf.PLATFORMS_SEARCH` (старое поведение, gallery=True — для совместимости).
    placementTypes=["SEARCH_PAGE"]. Не ставим isOrganicSearchEnabled=False/placementTypes=[] (это РСЯ)."""
    import datetime as _dt
    gc_fin = gf.GridClient(login)
    gc_fin._bootstrap_csrf()
    uc = json.loads(json.dumps(gf._TEMPLATE))
    uc["id"] = str(campaign_id)
    uc["name"] = name
    uc["strategyId"] = None
    uc["startDate"] = _dt.date.today().isoformat()
    uc["metrikaCounters"] = [int(c) for c in (counter_ids or [])]
    uc["biddingStategyWithPlatforms"]["platforms"] = dict(platforms or gf.PLATFORMS_SEARCH)
    uc["biddingStategyWithPlatforms"]["strategyData"] = {
        "goalId": str(goal_id), "avgCpa": str(int(cpa_rub)), "sum": str(int(weekly_rub)),
        "budgetType": "WEEKLY", "payForConversion": bool(pay_for_conversion),
        "payForShows": False, "isExplorationBudgetValueCustom": None,
        "minExplorationBudget": None,
    }
    # HAR20: tp5 «Ручная настройка» = placementTypes=null + platforms gallery/organic/search.
    # Sentinel [] (пустой список) → null; None (не передан, tp2/tp4) → ["SEARCH_PAGE"]; явный список → сам список.
    uc["placementTypes"] = list(placement_types) if placement_types else (["SEARCH_PAGE"] if placement_types is None else None)
    # #4 review: «Динамические места на поиске» = isOrganicSearchEnabled. Шаблон grid_uc_template.json
    # приносит True; привязываем к platforms.organic (иначе tp2 протекал True). tp2 organic=False→OFF,
    # tp4/tp5 organic=True→ON (как раньше). Ровно один источник правды — тот же (platforms or PLATFORMS_SEARCH).
    uc["isOrganicSearchEnabled"] = bool((platforms or gf.PLATFORMS_SEARCH).get("organic"))
    uc["bannerHrefParams"] = ""                            # UTM только на уровне групп (trackingParams), не кампании
    uc["inheritableCallouts"] = {"calloutIds": [str(i) for i in (callout_ids or [])]}
    uc["inheritableSitelinkSet"] = {"sitelinkSetId": str(sitelink_set_id) if sitelink_set_id else None}
    uc["promoExtensionId"] = str(promo_id) if promo_id else None
    uc["libraryMinusKeywordsIds"] = [str(i) for i in (minus_set_ids or [])]
    uc["bidModifiers"] = bid_modifiers or {}
    uc["isAlternativeTextsEnabled"] = False               # инвариант #3
    uc["hasSiteMonitoring"] = True                        # инвариант #4
    uc["hasExtendedGeoTargeting"] = False                 # инвариант #5
    uc["isRecommendationsManagementEnabled"] = False      # инвариант #6
    uc["isPriceRecommendationsManagementEnabled"] = False
    uc["enableCompanyInfo"] = False
    r = gc_fin._post("UpdateCampaigns", gf._MUTATION,
                     {"input": {"campaignUpdateItems": [{"unifiedCampaign": uc}]}, "login": login})
    data = r.json()
    res = (data.get("data") or {}).get("updateCampaigns") or {}
    vr = res.get("validationResult") or {}
    if data.get("errors") or vr.get("errors"):
        raise gf.GridFinalizeError("search-finalize: " + json.dumps(
            data.get("errors") or vr.get("errors"), ensure_ascii=False)[:400])
    return res.get("updatedCampaigns") or []


def _add_listing_ads_v501(token: str, login: str, items: list[dict]) -> list[int]:
    """Создать ListingAd через v501 с явным FeedFilterConditions по collectionId.

    Для брендовых групп список collectionId уже развёрнут заранее. Пустой collection_ids
    считаем ошибкой данных и не создаём листинг по всему фиду.
    """
    out: list[int] = []
    for it in items or []:
        coll_ids = [str(x) for x in (it.get("collection_ids") or []) if str(x).strip()]
        if not coll_ids:
            continue
        payload = {
            "Ads": [{
                "AdGroupId": int(it["adgroup_id"]),
                "ListingAd": {
                    "FeedId": int(it["feed_id"]),
                    "FeedFilterConditions": [{
                        "Operand": "collectionId",
                        "Operator": "EQUALS_ANY",
                        "Arguments": coll_ids,
                    }],
                },
            }],
        }
        j = _v501_svc("ads", "add", token, login, payload)
        add_res = ((j.get("result") or {}).get("AddResults") or [{}])[0]
        if add_res.get("Id") and not (add_res.get("Errors") or []):
            out.append(int(add_res["Id"]))
            continue
        errs = add_res.get("Errors") or []
        msg = "; ".join((e.get("Message") or "") for e in errs if isinstance(e, dict)).strip()
        if not msg:
            msg = _v5_err(j)
        raise RuntimeError(f"{it.get('name', '?')}: listing v501 {msg[:180]}")
    return out


def _create_tp1_single(
    token: str,
    login: str,
    name: str,
    counter_id: int,
    goal_id: int,
    cpa_value_rub: int,
    mode: str,
    region_ids: list,
    href: str,
    slepok: str,
    site_type: str,
    r_code: str,
    titles: list | None,
    texts: list,
    feed_id: int = 0,
    with_shopping: bool = False,
    feed_models: dict | None = None,
    budget_rub: int = 0,
    segment: str | None = None,
    city: str = "",
    ai_title2: str = "",
    sitelinks: list | None = None,
    callout_texts: list | None = None,
    callout_ids: list | None = None,
    autotarget: bool = False,
    products_only: bool = False,
    grid_cookie: str | None = None,
) -> dict:
    """Создать ОДНУ кампанию tp1 (РСЯ) через ЕПК v501 с указанным mode.

    mode='network_cpa'     → cpc-вариант: AVERAGE_CPA в сетях (tp1_cpc_site)
    mode='network_payconv' → cpa-вариант: PAY_FOR_CONVERSION в сетях (tp1_cpa_site)

    Инварианты: персонализация ВЫКЛ, мониторинг ВКЛ, расш.гео ВЫКЛ.
    Кампания создаётся как DRAFT (launch=False).

    Возвращает {"ok": True, "campaign_id": ..., "tp1_build": {...}} или {"ok": False, ...}.
    """
    spec = cmc.UnifiedCampaignSpec(
        name=name,
        client_login=login,
        oauth_token=token,
        mode=mode,
        region_ids=region_ids,
        counter_ids=[counter_id] if counter_id else None,
        goal_id=goal_id or None,
        network_average_cpa=int(cpa_value_rub) * 1_000_000,  # руб → мкруб (для network_cpa)
        search_cpa=int(cpa_value_rub) * 1_000_000,            # руб → мкруб (для network_payconv)
        apply_invariants=True,                                  # #3/#4/#5 из CAMPAIGN_INVARIANTS.md
    )
    v501 = cmc.DirectV501Client(token, login)
    campaign_id = None

    def _cleanup_partial(reason: str) -> dict:
        deleted = False
        if campaign_id:
            try:
                v501.delete_campaigns([int(campaign_id)])
                deleted = True
            except Exception:  # noqa: BLE001
                try:
                    deleted = bool(campaign_id in (gc.GridCreateClient(login).delete_campaigns([campaign_id]).get("deleted") or []))
                except Exception:  # noqa: BLE001
                    deleted = False
        return {"ok": False, "name": name, "campaign_id": campaign_id,
                "partial_deleted": deleted, "error": reason[:240]}

    try:
        campaign_id = v501.create_unified_campaign(spec, launch=False)

        # Привязка счётчика Метрики через v501 campaigns.update.
        # Soft-операция: если упадёт — кампания создана, просто без счётчика.
        counter_note = None
        if counter_id:
            try:
                j_upd = _v501_call("update", token, login, {
                    "Campaigns": [{"Id": campaign_id,
                                   "UnifiedCampaign": {"CounterIds": {"Items": [int(counter_id)]}}}]
                })
                upd_errs = ((j_upd.get("result") or {}).get("UpdateResults") or [{}])[0].get("Errors") or []
                if upd_errs:
                    counter_note = f"счётчик {counter_id} не привязался: {upd_errs[0].get('Message','?')}"
            except Exception as e:  # noqa: BLE001
                counter_note = f"счётчик {counter_id} не привязался: {str(e)[:120]}"

        # Наполняем бренд-группами из пака M3.
        tp1_build = _build_tp1_from_pack(
            token, login, campaign_id, slepok, site_type, region_ids,
            href, r_code, titles, texts, counter_id=counter_id,
            feed_id=feed_id, with_shopping=with_shopping, feed_models=feed_models,
            segment=segment, ai_title2=ai_title2, city=city, autotarget=autotarget,
            products_only=products_only, sitelinks=sitelinks, grid_cookie=grid_cookie)
        if tp1_build.get("error") or tp1_build.get("skipped") or not tp1_build.get("adgroups"):
            return _cleanup_partial("tp1 не дозаполнена: " + str(tp1_build.get("error") or tp1_build.get("skipped") or "группы не созданы"))
        if not products_only and not tp1_build.get("ads"):
            _details = []
            for _k in ("adgroups", "keywords", "images_uploaded"):
                if tp1_build.get(_k) is not None:
                    _details.append(f"{_k}={tp1_build.get(_k)}")
            _errs = (tp1_build.get("errors") or [])[:3]
            _warns = (tp1_build.get("warnings") or [])[:2]
            if _errs:
                _details.append("errors: " + "; ".join(str(x) for x in _errs))
            if _warns:
                _details.append("warnings: " + "; ".join(str(x) for x in _warns))
            return _cleanup_partial("tp1 не дозаполнена: объявления не созданы"
                                    + (f" ({'; '.join(_details)})" if _details else ""))

        # #6 Фикс пустого текста товарных объявлений (ShoppingAd) в tp1.
        _tp1_default_text = ((texts[0] if texts else "")[:81]
                             or "Официальный дилер. Тест-драйв и выгодные условия по кредиту. Авто в наличии.")
        _shop_ids = tp1_build.get("shopping_ad_ids") or []
        if with_shopping and feed_id and not _shop_ids:
            # Bug2 graceful (как tp7 whole-feed fallback): фид без офферов/модельных листингов
            # (напр. лендинг-фид) → НЕ удаляем кампанию — в ней валидные TextAd-группы РСЯ.
            # Оставляем как РСЯ без товарки + warning (hard-fail остаётся только «группы не созданы»).
            tp1_build.setdefault("warnings", []).append(
                "товарка не создана: фид без ShoppingAd — оставлена РСЯ без товарных объявлений")
        if _shop_ids and feed_id:
            _gcl = gf.GridClient(login, cookie=grid_cookie)
            # G review: set_default_text в СВОЁМ try — падение (Яндекс 500) НЕ должно блокировать листинги
            # (раньше оба в одном try → текст падал → листинги пропускались → 0 ListingAd).
            try:
                _gcl.set_default_text(
                    _shop_ids, feed_id, _tp1_default_text,
                    filters_by_ad_id=(tp1_build.get("shopping_filters") or {}),
                )
                tp1_build["shopping_text_set"] = len(_shop_ids)
            except Exception as _e:  # noqa: BLE001
                tp1_build.setdefault("warnings", []).append(f"shopping text: {str(_e)[:120]}")
            # Листинги «Страницы каталога» — НЕЗАВИСИМО от текста (Grid by-shopping, без баллов), затем
            # name-фильтр CONTAINS_ANY [марка|марка+модель] (HAR36 updateListingAds; by-shopping не наследует).
            # #ФИКС-1: saveDraft:True → addedAds пуст → строим _lf_items из listing_build_items по adGroupId.
            try:
                _rows = _gcl.add_listing_ads_by_shopping_ads(_shop_ids) or []
                tp1_build["listing_ads"] = len(_rows)
                # adGroupId→name_val из listing_build_items (независимо от addedAds)
                _agid_to_nv = {str(it["adgroup_id"]): it.get("name_value")
                               for it in (tp1_build.get("listing_build_items") or [])
                               if it.get("adgroup_id") and it.get("name_value")}
                _lf_items = []
                for _row in _rows:
                    _lid = _row.get("id") if isinstance(_row, dict) else _row
                    _agid = str(_row.get("adGroupId") or "") if isinstance(_row, dict) else ""
                    _val = _agid_to_nv.get(_agid)
                    if _lid and _val:
                        _lf_items.append({"id": _lid, "feed_id": feed_id, "value": _val,
                                          "bodies": [_tp1_default_text]})
                if not _lf_items and _agid_to_nv:
                    # saveDraft:True → addedAds пуст; строим по adGroupId (фильтр ставится на группу)
                    for _agid_s, _val in _agid_to_nv.items():
                        _lf_items.append({"adgroup_id": _agid_s, "feed_id": feed_id,
                                          "value": _val, "bodies": [_tp1_default_text]})
                if _lf_items:
                    tp1_build["listing_name_set"] = _gcl.set_listing_name_filters(_lf_items)
            except Exception as _le:  # noqa: BLE001
                tp1_build.setdefault("warnings", []).append(f"listing(grid): {str(_le)[:160]}")
        if with_shopping and feed_id and _shop_ids and not int(tp1_build.get("listing_ads") or 0):
            # Bug2 graceful: ShoppingAd есть, а листинги «Страницы каталога» пусты (фид-каталог без
            # готовых офферов) → НЕ удаляем кампанию, оставляем товарку без листингов + warning.
            tp1_build.setdefault("warnings", []).append(
                "листинги каталога: 0 ListingAd (по by-shopping) — оставлена товарка без листингов")

        result_d = {"ok": True, "name": name, "campaign_id": campaign_id,
                    "launched": False, "tp1_build": tp1_build,
                    "url": f"https://direct.yandex.ru/dna/campaign/{campaign_id}?ulogin={login}"}
        if counter_note:
            result_d["counter_note"] = counter_note

        # ── Grid-докрутка РСЯ: уточнения/промо/быстрые ссылки на УРОВНЕ КАМПАНИИ ──
        # БАГ-1 FIX (2026-06-24): вынесена в ОТДЕЛЬНЫЙ try/except (ранее была внутри общего
        # try → GridFinalizeError → except Exception → _cleanup_partial УДАЛЯЛ кампанию с
        # 34+ объявлениями!). Теперь финализация best-effort: кампания остаётся, ошибка
        # пишется в result_d["finalize_warn"]. Grid принимает goalId="0" (проверено live).
        _ai_sitelinks = sitelinks or _ai_common_sitelinks(login, slepok, site_type, city, "tp1")
        a = _resolve_campaign_assets(token, login, href, sitelinks=_ai_sitelinks,
                                     slepok=slepok, site_type=site_type,
                                     prefer_callout_texts=callout_texts,
                                     prefer_callout_ids=callout_ids,
                                     grid_cookie=grid_cookie)
        slset = a.get("sitelink_set_id")
        wkl = int(budget_rub) if budget_rub else int(cpa_value_rub) * 10
        try:
            _finalize_rsya(
                login, campaign_id, name=name, goal_id=goal_id or 0,
                cpa_rub=cpa_value_rub, weekly_rub=wkl,
                counter_ids=[counter_id] if counter_id else [],
                pay_for_conversion=(mode == "network_payconv"),
                callout_ids=a["callout_ids"], sitelink_set_id=slset,
                promo_id=(a["promos"][0] if a["promos"] else None),
                minus_set_ids=None, grid_cookie=grid_cookie)
            result_d["rsya_finalized"] = True
            result_d["callouts_set"] = len(a["callout_ids"])
            result_d["sitelink_set_id"] = slset
        except Exception as _fe:  # noqa: BLE001 — Grid-ошибка не удаляет кампанию (она уже ok)
            result_d["rsya_finalized"] = False
            result_d["finalize_warn"] = f"Grid-финализация (ассеты) не прошла: {str(_fe)[:200]}"
        return result_d
    except cmc.DirectV501Error as e:
        if campaign_id:
            return _cleanup_partial(str(e))
        return {"ok": False, "name": name, "error": str(e)[:240]}
    except Exception as e:  # noqa: BLE001
        if campaign_id:
            return _cleanup_partial(str(e))
        return {"ok": False, "name": name, "error": str(e)[:240]}


def _create_tp1_campaign(
    token: str,
    login: str,
    name: str,
    counter_id: int,
    goal_id: int,
    cpc_cpa: int,
    region_ids: list,
    href: str,
    slepok: str,
    site_type: str,
    r_code: str,
    titles: list | None,
    texts: list,
    feed_id: int = 0,
    with_shopping: bool = False,
    feed_models: dict | None = None,
    budget_rub: int = 0,
    segment: str | None = None,
    ai_title2: str = "",
    sitelinks: list | None = None,
    callout_texts: list | None = None,
    callout_ids: list | None = None,
    city: str = "",
    autotarget: bool = False,
    products_only: bool = False,
    no_cpa: bool = False,
    grid_cookie: str | None = None,
    job=None,
) -> dict:
    """Создать ПАРУ кампаний tp1 (РСЯ): cpc-вариант (AVERAGE_CPA) + cpa-вариант (PAY_FOR_CONVERSION).

    no_cpa=True (галочка «под стиль сайта» снята) → создаём ТОЛЬКО cpc-вариант (без оплаты за конверсии).

    segment ('Марки'|'Модели'|None) — какие ct-группы класть в обе кампании пары.

    Канон CODER.md: каждый текстовый tp = ПАРА кампаний (cpc + cpa).
    - tp1_cpc_site: mode='network_cpa'     (Network=AVERAGE_CPA, оплата за клики)
    - tp1_cpa_site: mode='network_payconv' (Network=PAY_FOR_CONVERSION, оплата за конверсии)

    Имя кампании (аргумент name) интерпретируется как канон cpc-варианта:
      'tp1_cpc_site — РСЯ - {cat} - {targ}'
    cpa-вариант получает то же имя с заменой 'tp1_cpc_site' → 'tp1_cpa_site'.

    Группы из пака M3 наполняются в обе кампании (общий slepok/site_type).

    Возвращает {"ok": True, "campaigns": [cpc_result, cpa_result]} или {"ok": False, ...}.
    """
    # Генерим имя cpa-кампании из cpc: замена суффикса оплаты в кодере
    name_cpa = name.replace("tp1_cpc_site", "tp1_cpa_site", 1)

    cpc_result = _create_tp1_single(
        token=token, login=login, name=name, counter_id=counter_id,
        goal_id=goal_id, cpa_value_rub=cpc_cpa, mode="network_cpa",
        region_ids=region_ids, href=href, slepok=slepok,
        site_type=site_type, r_code=r_code, titles=titles, texts=texts,
        feed_id=feed_id, with_shopping=with_shopping, feed_models=feed_models,
        budget_rub=budget_rub, segment=segment, city=city,
        ai_title2=ai_title2, sitelinks=sitelinks,
        callout_texts=callout_texts, callout_ids=callout_ids,
        autotarget=autotarget, products_only=products_only,
        grid_cookie=grid_cookie,
    )
    cpa_result = None
    # no_cpa → пропускаем вариант оплаты за конверсии; отмена → cpa тоже пропускаем (cpc уже достроен).
    if not no_cpa and not (job and job.get("cancel")):
        cpa_result = _create_tp1_single(
            token=token, login=login, name=name_cpa, counter_id=counter_id,
            goal_id=goal_id, cpa_value_rub=cpc_cpa, mode="network_payconv",
            region_ids=region_ids, href=href, slepok=slepok,
            site_type=site_type, r_code=r_code, titles=titles, texts=texts,
            feed_id=feed_id, with_shopping=with_shopping, feed_models=feed_models,
            budget_rub=budget_rub, segment=segment, city=city,
            ai_title2=ai_title2, sitelinks=sitelinks,
            callout_texts=callout_texts, callout_ids=callout_ids,
            autotarget=autotarget, products_only=products_only,
            grid_cookie=grid_cookie,
        )
    # Сводный результат: ok=True если хоть одна создалась
    ok = cpc_result.get("ok") or (bool(cpa_result) and cpa_result.get("ok"))
    # Обратная совместимость с api_create_set: возвращаем campaign_id первой созданной
    first_id = cpc_result.get("campaign_id") or (cpa_result.get("campaign_id") if cpa_result else None)
    out = {
        "ok": ok, "name": name, "campaign_id": first_id,
        "launched": False,
        "campaigns": [cpc_result] + ([cpa_result] if cpa_result else []),
        "url": (cpc_result.get("url") or (cpa_result.get("url") if cpa_result else "") or ""),
    }
    if not ok:
        # Обе кампании пары упали → поднимаем РЕАЛЬНУЮ причину наверх (иначе UI показывает пустое «()»).
        _errs = [c.get("error") for c in out["campaigns"] if c and c.get("error")]
        out["error"] = ("; ".join(dict.fromkeys(_errs))[:240]
                        or "tp1: кампании пары не создались (причина не определена)")
    return out


def _grid_account_image_hashes(login: str) -> dict:
    """{image_name: imageHash} картинок, УЖЕ загруженных в аккаунт — читается ПО КУКЕ через Grid
    (БЕЗ баллов). Name = basename файла M3 (upload_image кладёт Name=os.path.basename(path)).
    Нужно куки-пути РСЯ (tp1): при 0 баллов залить НОВУЮ картинку нельзя (adimages.add → 152),
    но ПЕРЕИСПОЛЬЗОВАТЬ хэш уже залитой (предыдущими v5-созданиями) — можно. Покрытие растёт по
    мере «созревания» аккаунта. Мягкая деградация: нет куки/ошибка → {} (создаём без картинок)."""
    import requests as _rqs
    import re as _re
    try:
        cookie = cmc.pick_working_cookie(login)
    except Exception:  # noqa: BLE001
        return {}
    if not cookie:
        return {}
    sess = _rqs.Session()
    sess.verify = False
    csrf = {"t": None}

    def _g(op, q, var):
        h = {"Cookie": cookie, "dna-operation-name": op, "x-direct-api": "1", "x-detected-locale": "ru",
             "Content-Type": "application/json", "User-Agent": cmc.USER_AGENT, "Origin": "https://direct.yandex.ru",
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
                r = sess.post(f"{_GRID_URL}?operationName={op}&ulogin={login}",
                              json={"operationName": op, "query": q, "variables": var}, headers=h, timeout=40)
        return r

    try:
        _g("Callouts", "query Callouts($login:String!){callouts(input:{searchBy:{login:$login}"
           "filter:{deleted:false}}){id}}", {"login": login})
        camp_ids = [c["id"] for c in _grid_list_campaigns(login) if c.get("id")]
    except Exception:  # noqa: BLE001
        return {}
    A = ("query A($login:String!,$inp:GdAdsContainerInput!){client(searchBy:{login:$login}){"
         "ads(input:$inp){rowset{id ...on GdAdaptiveTextAd{images{imageHash name}}}}}}")
    out: dict = {}
    for i in range(0, len(camp_ids), 100):
        inp = {"filter": {"campaignIdIn": [str(x) for x in camp_ids[i:i + 100]]},
               "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
               "limitOffset": {"limit": 5000, "offset": 0}, "orderBy": [{"order": "ASC", "field": "ID"}]}
        try:
            d = _g("A", A, {"login": login, "inp": inp}).json()
        except Exception:  # noqa: BLE001
            continue
        if d.get("errors"):
            continue
        for ad in (((d.get("data") or {}).get("client") or {}).get("ads") or {}).get("rowset") or []:
            for im in (ad.get("images") or []):
                if im.get("name") and im.get("imageHash"):
                    out.setdefault(im["name"], im["imageHash"])
    return out


def _tp1_pack_groups(login: str, slepok: str, site_type: str, r_code: str, href: str,
                     titles: list | None, texts: list,
                     segment: str | None = None, ai_title2: str = "", city: str = "",
                     with_shopping: bool = False, tp_code: str = "tp1",
                     image_map: dict | None = None, autotarget: bool = False,
                     feed_url_by_model: dict | None = None) -> list:
    """Бренд-группы tp1/tp5 из пака M3 — ЧИСТО данные (без API-вызовов, без баллов). Зеркало
    группо-сборки _build_tp1_from_pack (см. там), вынесено для куки-пути (grid_create.create_full).
    image_map (РСЯ tp1): {basename→imageHash} уже залитых картинок аккаунта — переиспользуем хэши
    (картинку при 0 баллов залить нельзя). Источник картинок — как в v5 (_build_tp1_from_pack:
    read_slepok_images ∥ read_images), basename матчим с image_map.
    → [{name, ct, brand, keywords, minus, titles, texts, href[, image_hashes]}]."""
    import os as _os
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    # #3 (решение Семёна): tp4 = те же кампании, что tp2 (отличие — только галочка «Динамика»). Пак
    # tp4 беднее (были группы с 1 ключом) → ИСТОЧНИК ГРУПП/КЛЮЧЕЙ для tp4 берём из tp2-пака. Алиас
    # касается ТОЛЬКО `kp.gather`; место показа (organic=True), нейминг/кодер, тип Поиск+Динамика,
    # корректировки и контент tp4 остаются tp4 (ниже `tp_code` не подменяется).
    _pack_tp = "tp2" if tp_code == "tp4" else tp_code
    pack = kp.gather(key, site_type, _pack_tp)
    if not pack:
        return []
    text0 = (texts[0] if texts else "")[:81] or "Официальный дилер. Тест-драйв и выгодные условия по кредиту. Авто в наличии."
    ct_model = kp.feeds_ct_model()
    ct_name = _ag_part1_map()
    _sc_titles = _slepok_campaign_content(slepok, site_type).get("titles") or []  # пул слепка — 1 раз
    groups = []
    for ct in sorted(pack.keys()):
        data = pack.get(ct) or {}
        if not data.get("positive"):
            continue
        if segment and _ct_segment(ct) != segment:
            continue
        raw_brand = ct_name.get(ct) or ct_model.get(ct) or ct
        brand = _valid_pack_brand_name(ct, raw_brand)
        group_label = _pack_group_display_name(ct, raw_brand, brand)
        # tp2/tp4 — поисковые группы: в кодере используем aoff, не сетевой tp1-формат.
        _is_search_tp = tp_code in ("tp2", "tp4")
        group_name = (_text_group_name(ct, r_code, group_label)
                      if _is_search_tp
                      else _tp1_group_name(ct, r_code, group_label, with_shopping=with_shopping,
                                           autotarget=autotarget))
        # deep-link: сначала реальный URL из фида, фолбэк на формульный слаг (#ФИКС-2).
        # ФИКС A: Марки → /auto/{brand} (первые 2 сегмента), Модели → полный путь без query. (#ФИКС-A)
        _raw_feed_url = (_feed_url_for_model(feed_url_by_model, brand) if feed_url_by_model else None)
        if _raw_feed_url:
            model_href = (_brand_level_url(_raw_feed_url) if _ct_segment(ct) == "Марки"
                          else _strip_url_query(_raw_feed_url))
        else:
            model_href = _model_page_href(href, site_type, brand)
        is_brand_group = _ct_segment(ct) in ("Марки", "Модели")
        title = (_title_from_template(brand, city) if (is_brand_group and not ai_title2)
                 else (_GENERIC_AT_TITLES[0] if not is_brand_group else brand[:35]))
        ttl2 = (ai_title2[:30] if ai_title2 else _next_title2())
        # Cookie/Grid-путь не должен делать M3-вызов на каждую ct-группу: это и было источником
        # зависания боевого create_set после restart. ИИ остаётся на уровне item, а группа берёт
        # локально собранный набор в том же стиле.
        _gt = _rsya_titles(brand, city, site_type, ai_title2=ai_title2,
                           base=(list(titles or []) + [title, ttl2] if is_brand_group
                                 else list(titles or []) + list(_GENERIC_AT_TITLES)),
                           pool=_sc_titles, is_brand=is_brand_group)
        _gx = _rsya_texts([t for t in (list(texts or []) + ([text0] if text0 else [])) if t], site_type, city, brand)
        _gt, _gx, _sl_dummy, _pay_changed = _coherent_payments(_gt, _gx, [])
        g = {
            "name": group_name, "ct": ct, "brand": brand, "seg": _ct_segment(ct),  # 'Марки' → цена=МИН по марке
            # БАГ-13: для «Марки» — убрать ключи «марка+модель»
            "keywords": _filter_group_keywords(data.get("positive", []), _ct_segment(ct), brand, city, site_type),
            "minus": data.get("minus", []),
            "titles": _gt or [t for t in ([title, brand] if brand else [title]) if t],
            "texts": _gx or ([text0] if text0 else []),
            "href": model_href,
        }
        _all_imgs = _creative_images_for_ct(site_type, tp_code, ct, key)
        if _all_imgs:
            g["image_paths"] = _all_imgs[:5]
        # РСЯ-картинки по куке (БЕЗ баллов): источник — пак M3 + Manual-добивка.
        # basename → hash из image_map (уже залитые в аккаунт, без баллов). Найденные → imageHashes.
        # Fallback по другим слепкам для того же ct (не менять ct — ct0000 ЗАПРЕЩЁН).
        if image_map:
            _hh = [image_map.get(_os.path.basename(p)) for p in _all_imgs]
            _hh = [h for h in _hh if h]
            if _hh:
                g["image_hashes"] = _hh[:5]
        groups.append(g)
    return groups


def _pack_groups_with_retry(login: str, slepok: str, site_type: str, r_code: str, href: str,
                            titles, texts, *, retries: int = 2, **kw) -> tuple[list, bool]:
    """`_tp1_pack_groups` с КОРОТКИМИ ретраями (M3-пак мог быть ВРЕМЕННО недоступен — sshfs/relay).
    Пустой пак больше НЕ повод для мгновенного permanent-fail. Бюджет ОГРАНИЧЕН: это вызывается и на
    СИНХРОННОМ route /api/create_set — длинные sleep вешали бы запрос. Worst-case ~0.5с sleep + ~3с
    статус M3. → (groups, m3_alive); m3_alive=False → пак пуст И M3 лежит → caller отправит в deferred."""
    groups: list = []
    for _i in range(max(1, int(retries))):
        try:
            groups = _tp1_pack_groups(login, slepok, site_type, r_code, href, titles, texts, **kw)
        except Exception as _e:  # noqa: BLE001 — сбой чтения пака считаем как «пусто», ретраим
            groups = []
        if groups:
            return groups, True
        if _i < retries - 1:
            time.sleep(0.5)                               # короткий backoff (не вешать sync-route)
    # Пусто после ретраев — жив ли M3 (единый источник правды о статусе)? Логируем для диагностики.
    try:
        _m3 = _m3_content_status(timeout=3.0)
    except Exception:  # noqa: BLE001
        _m3 = {"ok": False, "detail": "статус M3 не прочитан"}
    _alive = bool(_m3.get("ok"))
    print(f"[pack-empty] slepok={slepok} site_type={site_type} tp_retry={retries} "
          f"M3_alive={_alive} detail={_m3.get('detail')}", flush=True)
    return [], _alive


def _create_tp1_via_cookie(
    login: str, name: str, counter_id: int, goal_id: int, cpc_cpa: int,
    region_ids: list, href: str, slepok: str, site_type: str, r_code: str,
    titles: list | None, texts: list, budget_rub: int = 0, segment: str | None = None,
    ai_title2: str = "", city: str = "", autotarget: bool = False, no_cpa: bool = False,
    token: str = "", corr: dict | None = None, ret_map: dict | None = None,
    callout_texts: list | None = None, sitelinks: list | None = None,
    callout_ids: list | None = None,
    feed_id: int = 0, with_shopping: bool = False, feed_models: dict | None = None,
    job=None,
) -> dict:
    """tp1 РСЯ ПО КУКЕ (без баллов v5) — когда исчерпан лимит (152) и пользователь согласился через
    попап. Кампания+группы+комбинаторные объявления через grid_create.create_full.
    При наличии фида добиваем ShoppingAd+ListingAd через Grid, как и на token-path.
    → {"ok", "campaign_id", "campaigns":[...], "via":"cookie"} (форма как у _create_tp1_campaign)."""
    import datetime as _dt
    # РСЯ-картинки по куке: переиспользуем хэши уже залитых в аккаунт картинок (basename→hash).
    # При 0 баллов залить новую нельзя (adimages.add=152), но reuse — без баллов. Best-effort: {} → без картинок.
    _img_map = _grid_account_image_hashes(login)
    # URL страниц моделей: account-level мёрж (все фиды, как цены) — покрывает марки без URL
    # в конкретном feed_id (#ФИКС-8).
    _feed_url_map = _account_offer_urls(login, href)
    groups, _m3_alive = _pack_groups_with_retry(login, slepok, site_type, r_code, href, titles, texts,
                                                segment=segment, ai_title2=ai_title2, city=city, tp_code="tp1",
                                                image_map=_img_map, autotarget=autotarget,
                                                with_shopping=with_shopping,
                                                feed_url_by_model=_feed_url_map or None)
    if not groups:
        seg_note = f", segment={segment}" if segment else ""
        # Пак пуст после ретраев → НЕ permanent-fail: помечаем defer (пункт уйдёт на отложенную
        # докрутку позже, когда M3/пак восстановится), а не считаем окончательной ошибкой.
        return {"ok": False, "defer": True, "name": name,
                "error": (f"tp1(куки): пак M3 пуст/недоступен (M3_alive={_m3_alive}) для "
                          f"slepok={slepok}, site_type={site_type}, tp=tp1{seg_note} → отложено на докрутку")}
    start_date = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=3))).strftime("%Y-%m-%d")  # МСК
    wkl = int(budget_rub) if budget_rub else int(cpc_cpa) * 10
    name_cpa = name.replace("tp1_cpc_site", "tp1_cpa_site", 1)
    variants = [(name, "network_cpa", False)]
    if not no_cpa:
        variants.append((name_cpa, "network_payconv", True))
    # ЦЕНА из фида в комбинаторное по куке (как v5 Фаза 3.5): adPrice по бренду группы. Без баллов.
    # Раньше куки-путь цены не ставил вовсе (price_map не прокидывался). Best-effort: {} → без цен.
    try:
        _price_map = _account_offer_prices(login, href)   # цены из предпочтительных фидов (чистые имена)
    except Exception:  # noqa: BLE001
        _price_map = {}
    # Ассеты кампании (уточнения/быстрые ссылки/промо) — чтобы кампания была ДОЗАПОЛНЕНА как на v5-пути.
    # Грузим один раз; v5-GET'ы и Grid-докрутка баллов НЕ стоят (units тратят только add/update РК/объяв).
    # БАГ-1 FIX: ассеты загружаем ВСЕГДА при наличии токена, не только при goal_id.
    # Grid принимает goalId="0" (проверено live 2026-06-24): кампания обновляется, callouts/sitelinks ставятся.
    _ai_sitelinks = sitelinks or _ai_common_sitelinks(login, slepok, site_type, city, "tp1")
    # ФИКС B: Сайтлинки → href первой брендовой группы, а не базовый сайт. Cookie-путь создаёт
    # сайтлинки на уровне кампании (gc.create_full не поддерживает per-group). Берём первую
    # группу с не-базовым href как представителя. Для полноценных per-group сайтлинков нужен
    # рефакторинг gc.create_full (намеренно не трогается). (#ФИКС-B)
    _sl_href = next(
        (g["href"] for g in groups if g.get("href") and g["href"] != href.rstrip("/")),
        href
    )
    _assets = _resolve_campaign_assets(
        token, login, _sl_href, sitelinks=_ai_sitelinks,
        slepok=slepok, site_type=site_type, prefer_callout_texts=callout_texts,
        prefer_callout_ids=callout_ids)
    _slset = _assets.get("sitelink_set_id")
    _mp_disabled = _enabled_minus_places()                   # #21 минус-площадки РСЯ (1 раз на аккаунт)
    out_campaigns = []
    for nm, _mode, pay_conv in variants:
        if job and job.get("cancel"):                        # отмена: стоп ПЕРЕД cpa-вариантом пары
            break                                             # (cpc уже создан/дозаполнен)
        spec = {"name": nm, "counter_id": counter_id or 0, "goal_id": goal_id or 0,
                "cpa": int(cpc_cpa), "weekly_budget": wkl, "start_date": start_date,
                "network": True, "search": False, "pay_for_conversion": pay_conv,
                "disabled_places": _mp_disabled}             # #21 → build_unified_campaign.disabledPlaces
        try:
            rep = gc.create_full(login, campaign_spec=spec, groups=groups,
                                 region_ids=region_ids, href=href, goal_id=goal_id or 0,
                                 autotargeting=bool(autotarget),
                                 price_map=_price_map, brand_price_fn=_group_ad_price)
            cid = rep.get("campaign_id")
            ok = bool(cid) and bool(rep.get("ads")) and not (rep.get("errors") and not rep.get("groups"))
            if cid and not rep.get("ads"):
                if not rep.get("errors"):                     # ДИАГНОСТИКА: add_ads вернул пусто БЕЗ исключения
                    rep.setdefault("errors", []).append(
                        f"объявления(куки): 0 TextAd (groups={rep.get('groups')}, "
                        f"adgroup_ids={rep.get('adgroup_ids')}) — add_ads вернул пусто без ошибки Grid")
                print(f"[tp1-cookie] {nm}: 0 ads groups={rep.get('groups')} feed={feed_id} errs={rep.get('errors')}", flush=True)
                try:
                    gc.GridCreateClient(login).delete_campaigns([cid])
                except Exception:  # noqa: BLE001
                    pass
                out_campaigns.append({
                    "ok": False, "name": nm, "campaign_id": cid, "launched": False,
                    "via": "cookie",
                    "tp1_build": {"groups": rep.get("groups"), "ads": rep.get("ads"),
                                  "errors": rep.get("errors", [])[:5]},
                    "url": (f"https://direct.yandex.ru/dna/campaign/{cid}?ulogin={login}" if cid else ""),
                    "error": "tp1(куки): partial-кампания удалена — объявления не созданы",
                    "partial_deleted": True,
                })
                continue
            _shop_ids: list[int] = []
            _listing_ids: list[int] = []
            if ok and with_shopping and feed_id:
                _grid_shop_items = []
                # ДВА фильтра по типу (решение Семёна, HAR36): Товары → vendor [марка]; Страницы каталога
                # → name [марка|марка+модель]. ct0000 без марки → без фильтра. (Коллекции фида для
                # collectionId БОЛЬШЕ НЕ нужны — товары на vendor, листинг на name.)
                _shop_name_vals = []   # параллельно _grid_shop_items: name-значение листинга на группу
                for _grp, _agid in zip(groups, rep.get("adgroup_ids") or []):
                    if not _agid:
                        continue
                    _g_brand = (_grp.get("brand") or "").strip()
                    _g_seg = _ct_segment(_grp.get("ct") or "")
                    # Фильтр валиден ТОЛЬКО для брендовых групп («Марки»/«Модели»). «Общее» (тема в
                    # brand: «Автокредит»/«Trade-in»/«Авито») → без фильтра: товары по всему фиду, каталог — все стр.
                    _is_brand_seg = _g_seg in ("Марки", "Модели")
                    _vendor = _vendor_value(_g_brand) if (_g_brand and _is_brand_seg) else None     # товары: vendor [марка]
                    _name_val = _listing_name_value(_g_brand, _g_seg) if (_g_brand and _is_brand_seg) else None  # листинг: name
                    _model_vals = _model_field_values(_g_brand, _g_seg) if (_g_brand and _is_brand_seg) else []  # Модели → +model
                    _grid_shop_items.append({
                        "adgroup_id": int(_agid),
                        "feed_id": int(feed_id),
                        "vendor": _vendor,
                        "collection_id": None,
                        "model": _model_vals,
                        "name": _grp.get("name", "?"),
                    })
                    _shop_name_vals.append(_name_val)
                if _grid_shop_items:
                    try:
                        _gcl_shop = gf.GridClient(login)
                        _add_ids = _gcl_shop.add_shopping_ads(_grid_shop_items) or []
                        _shop_ids = [int(x) for x in _add_ids if x]
                        # карта shopping_ad_id → name_value (для name-фильтра листинга)
                        _name_by_shop = {}
                        for _ai, _raw in enumerate(_add_ids):
                            if _raw and _ai < len(_shop_name_vals) and _shop_name_vals[_ai]:
                                _name_by_shop[int(_raw)] = _shop_name_vals[_ai]
                        if _shop_ids:
                            _default_text = ((texts[0] if texts else "")[:81]
                                             or "Официальный дилер. Тест-драйв и выгодные условия по кредиту. Авто в наличии.")
                            _shop_filters = {}
                            for _sid, _src in zip(_shop_ids, [s for s in _grid_shop_items]):
                                _conds = []
                                if _src.get("vendor"):
                                    _conds.append({"field": "vendor", "operator": "CONTAINS_ANY",
                                                   "stringValue": json.dumps(_vendor_filter_values(_src["vendor"]), ensure_ascii=False)})
                                if _src.get("model"):
                                    _mvals = _src["model"] if isinstance(_src["model"], list) else [str(_src["model"])]
                                    _mvals = [str(x) for x in _mvals if str(x).strip()]
                                    if _mvals:
                                        _conds.append({"field": "model", "operator": "CONTAINS_ANY",
                                                       "stringValue": json.dumps(_mvals, ensure_ascii=False)})
                                if _conds:
                                    _shop_filters[int(_sid)] = {"tab": "CONDITION", "conditions": _conds}
                            # G review: set_default_text в СВОЁМ try — падение (Яндекс 500) НЕ блокирует
                            # создание листингов ниже (раньше оба в одном try → текст падал → 0 ListingAd).
                            try:
                                _gcl_shop.set_default_text(
                                    _shop_ids, int(feed_id), _default_text,
                                    filters_by_ad_id=_shop_filters,
                                )
                            except Exception as _dte:  # noqa: BLE001
                                rep.setdefault("warnings", []).append(f"shopping text(куки): {str(_dte)[:140]}")
                            # #ФИКС-1(v2): adGroupId→name_val НАПРЯМУЮ из параллельных массивов —
                            # БЕЗ _add_ids. При частичном создании (len(_add_ids)<len(items))
                            # старая индексная адресация через enumerate(_add_ids) давала смещение:
                            # _shop_name_vals[i] уходило на _grid_shop_items[i] чужой марки.
                            # adGroupId в items надёжен (группа создана ДО add_shopping_ads).
                            _agid_to_nv2 = {}
                            for _gsi2, _nv2 in zip(_grid_shop_items, _shop_name_vals):
                                if _nv2 and isinstance(_gsi2, dict):
                                    _gi2 = _gsi2.get("adgroup_id")
                                    if _gi2:
                                        _agid_to_nv2[str(_gi2)] = _nv2
                            _listing_rows = (_gcl_shop.add_listing_ads_by_shopping_ads(_shop_ids) or [])
                            _listing_ids = []
                            _lf_items = []
                            for _row in _listing_rows:
                                try:
                                    _lid = _row.get("id") if isinstance(_row, dict) else _row
                                    _agid = str(_row.get("adGroupId") or "") if isinstance(_row, dict) else ""
                                    if _lid:
                                        _listing_ids.append(int(_lid))
                                    _val = _agid_to_nv2.get(_agid)
                                    if _lid and _val:
                                        _lf_items.append({"id": _lid, "feed_id": int(feed_id),
                                                          "value": _val, "bodies": [_default_text]})
                                except Exception:  # noqa: BLE001
                                    continue
                            if not _lf_items and _agid_to_nv2:
                                # saveDraft:True → addedAds пуст; строим по adGroupId (фильтр ставится на группу)
                                for _agid_s, _val in _agid_to_nv2.items():
                                    _lf_items.append({"adgroup_id": _agid_s, "feed_id": int(feed_id),
                                                      "value": _val, "bodies": [_default_text]})
                            # name-фильтр «Страницы каталога» (HAR36; by-shopping фильтр не наследует)
                            if _lf_items:
                                try:
                                    rep["listing_name_set"] = _gcl_shop.set_listing_name_filters(_lf_items)
                                except Exception as _lfe:  # noqa: BLE001
                                    rep["errors"].append(f"listing name-filter(куки): {str(_lfe)[:140]}")
                    except Exception as _shop_exc:  # noqa: BLE001
                        rep["errors"].append(f"shopping/listing(куки): {str(_shop_exc)[:160]}")
                if not _shop_ids:
                    # Bug2 graceful (как v5-путь / tp7 whole-feed fallback): фид без офферов
                    # (напр. лендинг-фид) → НЕ удаляем кампанию — в ней валидные TextAd-группы РСЯ.
                    # Диагностику пишем в WARNINGS (не errors!), чтобы выжившая ok=True кампания не
                    # показывала ложную «ошибку» в карточке — консистентно с v5-путём. (#1 review)
                    rep.setdefault("warnings", []).append(
                        "товарка(куки): 0 ShoppingAd — фид без офферов; оставлена РСЯ без товарных")
                    print(f"[tp1-cookie] {nm}: ShoppingAd=0 feed={feed_id} (graceful, РСЯ без товарки)", flush=True)
                elif not _listing_ids:
                    # Bug2 graceful: ShoppingAd есть, листинги пусты (фид-каталог без готовых офферов)
                    # → НЕ удаляем кампанию, оставляем товарку без листингов. Диагностика → warnings.
                    rep.setdefault("warnings", []).append(
                        f"листинги(куки): 0 ListingAd из {len(_shop_ids)} ShoppingAd (feed={feed_id}) — "
                        "0 ListingAd из by-shopping — оставлена товарка без листингов")
                    print(f"[tp1-cookie] {nm}: ListingAd=0 shop={len(_shop_ids)} feed={feed_id} (graceful)", flush=True)
            # Grid-докрутка РСЯ: уточнения/быстрые ссылки/промо на уровне кампании (без баллов).
            # БАГ-1 FIX: вызываем ВСЕГДА при ok+cid, не только при goal_id.
            # Grid принимает goalId="0" без ошибки (verified live 2026-06-24): ассеты ставятся корректно.
            _fin = None
            if ok and cid:
                try:
                    # Корректировки «Глобальных правил» через Grid (HAR21, без баллов) — campaignId
                    # ЭТОЙ кампании. v5 bidmodifiers.add тут недоступен (152), поэтому Grid.
                    _bm = _grid_bid_modifiers(cid, corr or {}, ret_map or {})
                    _finalize_rsya(
                        login, cid, name=nm, goal_id=goal_id or 0, cpa_rub=cpc_cpa, weekly_rub=wkl,
                        counter_ids=[counter_id] if counter_id else [],
                        pay_for_conversion=pay_conv,
                        callout_ids=_assets.get("callout_ids"), sitelink_set_id=_slset,
                        promo_id=(_assets["promos"][0] if _assets.get("promos") else None),
                        minus_set_ids=None, bid_modifiers=_bm, disabled_places=_mp_disabled)
                    _fin = {"callouts": len(_assets.get("callout_ids") or []),
                            "sitelink_set": _slset, "promo": bool(_assets.get("promos")),
                            "corrections": len((_bm.get("bidModifierRetargeting") or {}).get("adjustments") or [])}
                    if token:
                        _v5_mods, _v5_mod_err = _apply_corrections(token, login, cid, corr or {}, ret_map or {})
                        _fin["v5_corrections"] = _v5_mods
                        if _v5_mod_err:
                            _fin["v5_corrections_error"] = _v5_mod_err[:160]
                    # demographic (age/gender) теперь через Grid (bidModifierDemographics, HAR23/JS реверс).
                    # _grid_bid_modifiers уже включил их в _bm → _finalize_rsya применила Grid-ом.
                    _fin["demographic_corrections"] = len((_bm.get("bidModifierDemographics") or {}).get("adjustments") or [])
                except Exception as _fe:  # noqa: BLE001
                    _fin = {"error": str(_fe)[:160]}
                # ── Картинки для РСЯ-объявлений (новые аккаунты без истории) ─────────────
                # Если reuse image_hashes не сработал (новый аккаунт) — пробуем довесить картинки
                # ПО ГРУППАМ, чтобы не размазывать один и тот же хэш на все бренды кампании.
                _ad_ids = rep.get("ad_ids") or []
                if _ad_ids:
                    try:
                        import os as _os2
                        _gc_img = gf.GridClient(login)
                        _uploaded_by_name: dict[str, str] = {}
                        _upd_items = []
                        for _aid, _grp in zip(_ad_ids, groups):
                            _gpaths = _grp.get("image_paths") or []
                            _hashes = list(dict.fromkeys(_grp.get("image_hashes") or []))
                            for _pth in _gpaths:
                                if len(_hashes) >= 5:
                                    break
                                if not _pth or not _os2.path.isfile(_pth):
                                    continue
                                _bn = _os2.path.basename(_pth)
                                _h = _uploaded_by_name.get(_bn)
                                if not _h:
                                    _h = _cached_upload_image(_gc_img, login, _pth)
                                    if _h:
                                        _uploaded_by_name[_bn] = _h
                                if _h and _h not in _hashes:
                                    _hashes.append(_h)
                            _upd = {"id": _aid, "href": _grp.get("href") or href,
                                    "titles": _grp.get("titles") or [],
                                    "bodies": _grp.get("texts") or []}
                            if _hashes:
                                _upd["image_hashes"] = _hashes[:5]
                            _cur, _old = _group_ad_price(
                                _price_map, _grp.get("brand") or _grp.get("name") or "",
                                _grp.get("seg") or _ct_segment(_grp.get("ct") or "")
                            )
                            _ad_price = _grid_ad_price_payload(_cur, _old)
                            if _ad_price:
                                _upd["adPrice"] = _ad_price
                            _upd_items.append(_upd)
                        # Не используем suggest_images: Яндекс может предложить чужую/модельную картинку.
                        # Если своих картинок нет или они запрещены вкладкой «Контент», объявление остаётся без картинки.
                        if _upd_items:
                            _imgs_applied = _grid_update_adaptive_ads(login, _upd_items)
                            if _fin and isinstance(_fin, dict):
                                _fin["ads_repaired"] = _imgs_applied
                                _fin["image_groups"] = sum(1 for _it in _upd_items if _it.get("image_hashes"))
                    except Exception:  # noqa: BLE001 — картинки не критичны
                        pass
            out_campaigns.append({
                "ok": ok, "name": nm, "campaign_id": cid, "launched": False,
                "via": "cookie", "rsya_finalized": _fin,
                "tp1_build": {"groups": rep.get("groups"), "ads": rep.get("ads"),
                              "shopping_ads": len(_shop_ids), "listing_ads": len(_listing_ids),
                              "errors": rep.get("errors", [])[:5],
                              "warnings": rep.get("warnings", [])[:5]},
                "url": (f"https://direct.yandex.ru/dna/campaign/{cid}?ulogin={login}" if cid else ""),
                "error": ("; ".join(rep.get("errors") or [])[:240] if not ok else None),
            })
        except Exception as e:  # noqa: BLE001
            out_campaigns.append({"ok": False, "name": nm, "error": f"tp1(куки): {str(e)[:200]}"})
    ok = any(c.get("ok") for c in out_campaigns)
    first_id = next((c.get("campaign_id") for c in out_campaigns if c.get("campaign_id")), None)
    out = {"ok": ok, "name": name, "campaign_id": first_id, "launched": False,
           "via": "cookie", "campaigns": out_campaigns,
           "url": next((c.get("url") for c in out_campaigns if c.get("url")), "")}
    if not ok:
        _errs = [c.get("error") for c in out_campaigns if c and c.get("error")]
        out["error"] = ("; ".join(dict.fromkeys(_errs))[:240] or "tp1(куки): пара не создалась")
    return out


def _create_text_via_cookie(
    login: str, name: str, tp_code: str, counter_id: int, goal_id: int, cpa_rub: int,
    budget_rub: int, region_ids: list, href: str, slepok: str, site_type: str, r_code: str,
    titles: list | None, texts: list, pay: str = "cpa", city: str = "", autotarget: bool = False,
    corr: dict | None = None, ret_map: dict | None = None,
    token: str = "", callout_texts: list | None = None,
    callout_ids: list | None = None,
    precreated_promo_id: int | None = None,
) -> dict:
    """tp2/tp4 (Поиск / Поиск+Динамика) ПО КУКЕ (без баллов) — после согласия через попап (152).
    Кампания (search) + группы (ключи+минуса) + комбинаторные объявления через grid_create.
    Корректировки «Глобальных правил» — через Grid (HAR21) прямо в AddCampaigns (без баллов).
    БАГ-11 фикс: Grid-финализация инвариантов (#3/#4/#5/#6) + ассеты (sitelinks/callouts/promo)
    через _finalize_rsya (ПОИСК-режим: _PLATFORMS_SEARCH вместо РСЯ-платформ)."""
    import datetime as _dt
    _img_map = _grid_account_image_hashes(login)
    groups, _m3_alive = _pack_groups_with_retry(login, slepok, site_type, r_code, href, titles, texts,
                                                city=city, tp_code=tp_code, image_map=_img_map,
                                                autotarget=bool(autotarget))
    if not groups:
        # Пак пуст после ретраев → defer (отложенная докрутка), НЕ permanent-fail.
        return {"ok": False, "defer": True, "name": name,
                "error": f"{tp_code}(куки): пак M3 пуст/недоступен (M3_alive={_m3_alive}) — отложено на докрутку"}
    start_date = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=3))).strftime("%Y-%m-%d")
    wkl = int(budget_rub) if budget_rub else int(cpa_rub) * 10
    # Корректировки в AddCampaigns (HAR21): campaignId-плейсхолдер 9999999 — Yandex привяжет к реальной.
    _bm = _grid_bid_modifiers(9999999, corr or {}, ret_map or {})
    spec = {"name": name, "counter_id": counter_id or 0, "goal_id": goal_id or 0,
            "cpa": int(cpa_rub), "weekly_budget": wkl, "start_date": start_date,
            "network": False, "search": True, "pay_for_conversion": (pay == "cpa"),
            "bid_modifiers": _bm,
            # #11: tp2/tp4 — только страница поиска (['SEARCH_PAGE']), без «динамических мест на поиске».
            # Динамика tp4 идёт через organic (platforms), не через placementTypes. Ставим на СОЗДАНИИ,
            # чтобы не было окна с placementTypes=None (=дефолт с динамич. местами), если финализация упадёт.
            "placement_types": ["SEARCH_PAGE"]}
    # БАГ-10: цены из фида для tp2/tp4 cookie-пути (раньше price_map не прокидывался).
    try:
        _tp24_price_map = _account_offer_prices(login, href)
    except Exception:  # noqa: BLE001
        _tp24_price_map = {}
    try:
        rep = gc.create_full(login, campaign_spec=spec, groups=groups, region_ids=region_ids,
                             href=href, goal_id=goal_id or 0, autotargeting=bool(autotarget),
                             price_map=_tp24_price_map, brand_price_fn=_group_ad_price)
        cid = rep.get("campaign_id")
        ok = bool(cid) and not (rep.get("errors") and not rep.get("groups"))
        # БАГ-11 фикс: Grid-финализация инвариантов + ассеты для tp2/tp4 куки-пути.
        # БАГ-1 FIX: вызываем ВСЕГДА при ok+cid, не только при goal_id.
        # Использует _finalize_search_via_grid (поисковые платформы, не РСЯ).
        # Сбой финализации не блокирует результат — группы/объявления уже созданы.
        _fin = None
        if ok and cid:
            try:
                _assets = {"callout_ids": [], "promos": [], "sitelinks": []}
                _slset = None
                _prefer_callout_ids = [int(x) for x in (callout_ids or []) if str(x or "").strip().isdigit()]
                if token:
                    try:
                        _assets = _tp5_account_data(token, login, slepok, site_type,
                                                    prefer_callout_texts=callout_texts or [],
                                                    prefer_callout_ids=_prefer_callout_ids)
                    except Exception:  # noqa: BLE001
                        pass
                # #10 КУКИ-ФОЛБЭК: если v5 не дал уточнений (пусто/после 152) — берём id уточнений
                # аккаунта через Grid Callouts (БЕЗ баллов), сопоставив по тексту слепка (HAR40).
                if _prefer_callout_ids:
                    _assets["callout_ids"] = _prefer_callout_ids[:8]
                elif not _assets.get("callout_ids"):
                    _gco = _grid_callout_ids(login, callout_texts or [])
                    if _gco:
                        _assets["callout_ids"] = _gco
                _ai_sitelinks = _ai_common_sitelinks(login, slepok, site_type, city, tp_code)
                # Sitelinks: Grid-первичный (БЕЗ баллов) — HAR23/entry262 AddSitelinkSets.
                _asl = _norm_sitelinks_for_v501(_ai_sitelinks or (_assets.get("sitelinks") or []), href)
                if _asl:
                    try:
                        _slset = gf.GridClient(login).add_sitelink_set(_asl)
                    except Exception:  # noqa: BLE001
                        _slset = _get_or_reuse_sitelink_set(token, login, _asl)  # v5 fallback
                # HAR-24/entry183: UpdateCampaigns должен получать реальный campaignId внутри
                # bidModifiers (не placeholder 9999999 из AddCampaigns). Перестраиваем с cid.
                _bm_fin = _grid_bid_modifiers(cid, corr or {}, ret_map or {})
                # #14 КУКИ-ФОЛБЭК: минус-набор «Минуса общие» через Grid (libraryMinusKeywordsIds, БЕЗ
                # баллов) для слепков с режимом shared_set (scherbakova). v5-привязка ниже остаётся как
                # дополнение при наличии баллов. Grid-пак ищем по имени (HAR40 MinusPhraseLibrary).
                _minus_ids = []
                if _SLEPOK_MINUS_MODE.get(slepok) == "shared_set":
                    _mp = _grid_minus_pack_id(login)
                    if _mp:
                        _minus_ids = [_mp]
                _finalize_search_via_grid(
                    login, cid, name=name, goal_id=goal_id or 0, cpa_rub=cpa_rub, weekly_rub=wkl,
                    counter_ids=[counter_id] if counter_id else [],
                    pay_for_conversion=(pay == "cpa"),
                    callout_ids=_assets.get("callout_ids"),
                    sitelink_set_id=_slset,
                    promo_id=(_assets["promos"][0] if _assets.get("promos") else precreated_promo_id),
                    minus_set_ids=_minus_ids,
                    bid_modifiers=_bm_fin,
                    platforms=_search_platforms(tp_code))   # места показа: tp2 organic=False / tp4 organic=True
                _fin = {"callouts": len(_assets.get("callout_ids") or []),
                        "sitelink_set": _slset, "promo": bool(_assets.get("promos") or precreated_promo_id),
                        "minus_set_grid": _minus_ids,
                        "corrections": len((_bm_fin.get("bidModifierRetargeting") or {}).get("adjustments") or [])}
                if token:
                    _v5_mods, _v5_mod_err = _apply_corrections(token, login, cid, corr or {}, ret_map or {})
                    _fin["v5_corrections"] = _v5_mods
                    if _v5_mod_err:
                        _fin["v5_corrections_error"] = _v5_mod_err[:160]
                # demographic (age/gender) через Grid (bidModifierDemographics).
                _fin["demographic_corrections"] = len((_bm_fin.get("bidModifierDemographics") or {}).get("adjustments") or [])
            except Exception as _fe:  # noqa: BLE001
                _fin = {"error": str(_fe)[:160]}
            _ad_ids = rep.get("ad_ids") or []
            if _ad_ids:
                try:
                    _upd_items = []
                    for _aid, _grp in zip(_ad_ids, groups):
                        # tp2/tp4 — ПОИСК: картинки НЕ грузим (их там быть не должно, решение Семёна).
                        # Обновляем только цену (adPrice показывается и на Поиске) + кнопку (отд. апдейтом).
                        _upd = {"id": _aid, "href": _grp.get("href") or href,
                                "titles": _grp.get("titles") or [],
                                "bodies": _grp.get("texts") or []}
                        _cur, _old = _group_ad_price(
                            _tp24_price_map, _grp.get("brand") or _grp.get("name") or "",
                            _grp.get("seg") or _ct_segment(_grp.get("ct") or "")
                        )
                        _ad_price = _grid_ad_price_payload(_cur, _old)
                        if _ad_price:
                            _upd["adPrice"] = _ad_price
                        _upd_items.append(_upd)
                    _repaired = _grid_update_adaptive_ads(login, _upd_items)
                    if _fin is None or not isinstance(_fin, dict):
                        _fin = {}
                    _fin["ads_repaired"] = _repaired
                    _fin["image_groups"] = sum(1 for _it in _upd_items if _it.get("image_hashes"))
                except Exception as _fe2:  # noqa: BLE001
                    if _fin is None or not isinstance(_fin, dict):
                        _fin = {}
                    _fin["repair_error"] = str(_fe2)[:160]
        return {"ok": ok, "name": name, "campaign_id": cid, "launched": False, "via": "cookie",
                "search_finalized": _fin,
                "build": {"groups": rep.get("groups"), "ads": rep.get("ads"),
                          "errors": rep.get("errors", [])[:5]},
                "url": (f"https://direct.yandex.ru/dna/campaign/{cid}?ulogin={login}" if cid else ""),
                "error": ("; ".join(rep.get("errors") or [])[:240] if not ok else None)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "name": name, "error": f"{tp_code}(куки): {str(e)[:200]}"}


def _create_shopping_via_cookie(
    login: str, name: str, tp_code: str, counter_id: int, goal_id: int, cpa_rub: int,
    budget_rub: int, region_ids: list, href: str, agency: str = "",
    body_text: str = "", feed_id: int = 0,
    corr: dict | None = None, ret_map: dict | None = None,
    token: str = "", slepok: str = "", site_type: str = "",
    callout_texts: list | None = None, feed_name: str = "",
    callout_ids: list | None = None,
    ct: str = "ct0000", r_code: str = "",
) -> dict:
    """tp3 (Товарная галерея РСЯ) / tp5 (Поиск + Товарная галерея) ПО КУКЕ (без баллов) — после
    согласия через попап (152). Кампания (gallery+organic) + группа (автотаргет) + товарное
    объявление по фиду (grid_create.create_shopping_full, реверс HAR17). → res-форма.
    tp3 → РСЯ-канал (network), tp5 → Поиск (search). Фид обязателен (читаем по куке).
    БАГ-12 фикс: после создания — Grid-finalize с callouts/sitelinks/инвариантами (раньше отсутствовал)."""
    import datetime as _dt
    fid = int(feed_id) if feed_id else 0
    feed_name = (feed_name or "").strip()
    if not fid:
        try:
            _rows = _filter_allowed_feed_rows(_grid_feeds(login, agency))
            _first = next((f for f in _rows if f.get("id")), None)
            fid = int(_first["id"]) if _first else 0
            feed_name = feed_name or ((_first or {}).get("name") or "")
        except Exception:  # noqa: BLE001
            fid = 0
    elif not feed_name and agency:
        try:
            _rows = _filter_allowed_feed_rows(_grid_feeds(login, agency))
            _match = next((f for f in _rows if int(f.get("id") or 0) == fid), None)
            feed_name = ((_match or {}).get("name") or "").strip()
        except Exception:  # noqa: BLE001
            feed_name = ""
    if not fid:
        return {"ok": False, "name": name, "error": f"{tp_code}(куки): нет URL-фида на аккаунте — товарную галерею не создать"}
    if tp_code == "tp5" and feed_name and feed_name not in name and not _is_site_domain_name(feed_name, href):
        name = f"{name} — {feed_name}"
    is_rsya = (tp_code == "tp3")
    start_date = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=3))).strftime("%Y-%m-%d")
    wkl = int(budget_rub) if budget_rub else int(cpa_rub) * 10
    _bm = _grid_bid_modifiers(9999999, corr or {}, ret_map or {})  # корректировки в AddCampaigns (HAR21)
    spec = {"name": name, "counter_id": counter_id or 0, "goal_id": goal_id or 0,
            "cpa": int(cpa_rub), "weekly_budget": wkl, "start_date": start_date,
            "network": is_rsya, "search": (not is_rsya), "organic": (not is_rsya),
            "pay_for_conversion": False, "bid_modifiers": _bm,
            # Места показа при СОЗДАНИИ НЕ форсируем (create=null — эталон HAR20 tp5-create).
            # Их выставляет finalize: _finalize_search_via_grid(placement_types=PLACEMENTS_TP5),
            # HAR49-эталон 712024652 (known-good). Форс ['SEARCH_PAGE','ADV_GALLERY'] в AddCampaigns
            # не подтверждён и рискует ORGANIC_PLACEMENT_TYPES_INVALID_COMBINATION → падение ВСЕГО
            # create (code-review C). Прежний create-guard закрывал лишь микро-окно «только Поиск»
            # и был добавлен под ложную тревогу UI-кэша (live был уже корректен). tp3 (РСЯ) — тоже null.
            "placement_types": None}
    try:
        # #5: имя группы tp5/tp3 по кодеру (как tp1/tp2): {ct}_aon_n000_{r_code}_..._g00 — Товарная
        # галерея. Без r_code (нет контекста) — прежнее «Товарная галерея».
        _grp_name = _text_group_name(ct, r_code, "Товарная галерея") if r_code else "Товарная галерея"
        rep = gc.create_shopping_full(login, campaign_spec=spec, group_names=[_grp_name],
                                      feed_id=fid, region_ids=region_ids, href=href,
                                      body_text=(body_text or "")[:81], goal_id=goal_id or 0)
        cid = rep.get("campaign_id")
        ok = bool(cid) and not (rep.get("errors") and not rep.get("groups"))
        # БАГ-12 фикс: Grid-finalize — callouts/sitelinks/инварианты на уровне кампании.
        # Раньше отсутствовал полностью для tp3/tp5 куки-пути → кампании без ассетов и без инвариантов.
        # Сбой финализации не блокирует результат — товарная галерея уже создана.
        _fin = None
        if ok and cid:
            try:
                _sh_assets = {"callout_ids": [], "promos": [], "sitelinks": []}
                _sh_slset = None
                _prefer_callout_ids = [int(x) for x in (callout_ids or []) if str(x or "").strip().isdigit()]
                if token:
                    try:
                        _sh_assets = _tp5_account_data(token, login, slepok, site_type,
                                                       prefer_callout_texts=callout_texts or [],
                                                       prefer_callout_ids=_prefer_callout_ids)
                    except Exception:  # noqa: BLE001
                        pass
                if _prefer_callout_ids:
                    _sh_assets["callout_ids"] = _prefer_callout_ids[:8]
                # Sitelinks: Grid-первичный (БЕЗ баллов) — HAR23/entry262 AddSitelinkSets.
                _sh_asl = _norm_sitelinks_for_v501(_sh_assets.get("sitelinks") or [], href)
                if _sh_asl:
                    try:
                        _sh_slset = gf.GridClient(login).add_sitelink_set(_sh_asl)
                    except Exception:  # noqa: BLE001
                        _sh_slset = _get_or_reuse_sitelink_set(token, login, _sh_asl)
                # HAR-24/entry183: UpdateCampaigns должен получать реальный campaignId внутри
                # bidModifiers (не placeholder 9999999 из AddCampaigns). Перестраиваем с cid.
                _bm_fin = _grid_bid_modifiers(cid, corr or {}, ret_map or {})
                if is_rsya:
                    # tp3 — РСЯ-канал: _finalize_rsya (network-only, placementTypes=[] хардкодом
                    # внутри — параметра placement_types у него НЕТ, передавать его = TypeError → ловилось
                    # except'ом и tp3-куки оставалась БЕЗ финализации (callouts/sitelinks/промо/корр.)).
                    _finalize_rsya(
                        login, cid, name=name, goal_id=goal_id or 0, cpa_rub=cpa_rub, weekly_rub=wkl,
                        counter_ids=[counter_id] if counter_id else [],
                        pay_for_conversion=False,
                        callout_ids=_sh_assets.get("callout_ids"),
                        sitelink_set_id=_sh_slset,
                        promo_id=(_sh_assets["promos"][0] if _sh_assets.get("promos") else None),
                        minus_set_ids=None, bid_modifiers=_bm_fin)
                else:
                    # tp5 «Поиск + Товарная галерея»: места показа SEARCH_PAGE + ADV_GALLERY (HAR20),
                    # platforms по умолчанию = PLATFORMS_SEARCH (gallery=True — товарная галерея НА поиске).
                    _finalize_search_via_grid(
                        login, cid, name=name, goal_id=goal_id or 0, cpa_rub=cpa_rub, weekly_rub=wkl,
                        counter_ids=[counter_id] if counter_id else [],
                        pay_for_conversion=False,
                        callout_ids=_sh_assets.get("callout_ids"),
                        sitelink_set_id=_sh_slset,
                        promo_id=(_sh_assets["promos"][0] if _sh_assets.get("promos") else None),
                        minus_set_ids=None, bid_modifiers=_bm_fin,
                        # tp5 «Ручная настройка + ТГ» = ЯВНЫЙ список ["SEARCH_PAGE","ADV_GALLERY"] (HAR49
                        # эталон 712024652). null давал пресет «Поиск» (Grid откатывает к дефолту, ADV_GALLERY
                        # не входит в пресет). Динамика = isOrganicSearchEnabled=True (platforms.organic). (C review)
                        placement_types=list(gf.PLACEMENTS_TP5))
                _fin = {"callouts": len(_sh_assets.get("callout_ids") or []),
                        "sitelink_set": _sh_slset, "promo": bool(_sh_assets.get("promos")),
                        "corrections": len((_bm_fin.get("bidModifierRetargeting") or {}).get("adjustments") or [])}
                if token:
                    _v5_mods, _v5_mod_err = _apply_corrections(token, login, cid, corr or {}, ret_map or {})
                    _fin["v5_corrections"] = _v5_mods
                    if _v5_mod_err:
                        _fin["v5_corrections_error"] = _v5_mod_err[:160]
            except Exception as _fe:  # noqa: BLE001
                _fin = {"error": str(_fe)[:160]}
        out = {"ok": ok, "name": name, "campaign_id": cid, "launched": False, "via": "cookie",
               "shopping_finalized": _fin,
               "build": {"groups": rep.get("groups"), "ads": rep.get("ads"), "feed_id": fid,
                         "errors": rep.get("errors", [])[:5]},
               "url": (f"https://direct.yandex.ru/dna/campaign/{cid}?ulogin={login}" if cid else ""),
               "error": ("; ".join(rep.get("errors") or [])[:240] if not ok else None)}
        return out
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "name": name, "error": f"{tp_code}(куки): {str(e)[:200]}"}


def _tp5_account_data(token: str, login: str, slepok: str, site_type: str, agency: str = "",
                      prefer_callout_texts: list | None = None,
                      prefer_callout_ids: list | None = None) -> dict:
    """Однократно собрать данные tp5: фиды, промо, минус-набор, уточнения, sitelinks, дефолт-текст.
    Фиды: v5 (баллы), при пустом (часто 152) — фолбэк на список по КУКЕ (Grid, без баллов).
    prefer_callout_texts — ВЫБРАННЫЕ пользователем уточнения (из попапа набора): создаём/находим их
    ID и вешаем именно их (inheritableCallouts кампании). Пусто → берём уточнения аккаунта (как было)."""
    cl = cmc.DirectV501Client(token, login)
    jf = _v5_get("feeds", token, login, ["Id", "Name", "SourceType"])
    allowed = _allowed_feed_keys()
    feeds = [(f["Id"], f.get("Name") or "") for f in (jf.get("result") or {}).get("Feeds", [])
             if f.get("SourceType") == "URL" and allowed and _feed_row_allowed(f, allowed)]
    if not feeds and agency:                              # v5 пусто/152 → фиды по куке (без баллов)
        feeds = [(int(f["id"]), f.get("name") or "") for f in _filter_allowed_feed_rows(_grid_feeds(login, agency)) if f.get("id")]
    jp = _v5_get("promotions", token, login, ["Id"])
    promos = [p["Id"] for p in (jp.get("result") or {}).get("Promotions", [])]
    jm = _v5_get("negativekeywordsharedsets", token, login, ["Id", "Name"])
    msets = [(s["Id"], s.get("Name") or "") for s in (jm.get("result") or {}).get("NegativeKeywordSharedSets", [])]
    minus_set = next((mid for mid, nm in msets if "Минуса общие" in nm), (msets[0][0] if msets else None))
    sitelinks, default_text = [], ""
    try:
        conn = _victory_conn()
        cur = conn.cursor()
        cur.execute("SELECT content FROM public.direct_slepok_content "
                    "WHERE slepok=%s AND site_type=%s AND kind='campaign'", (slepok, site_type))
        row = cur.fetchone()
        conn.close()
        if row:
            c = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            sitelinks = (c.get("sitelinks") or [])[:8]
            default_text = next((t for t in (c.get("texts") or []) if len(t) <= 81), "")
    except Exception:  # noqa: BLE001
        pass
    # БАГ-Б фикс: если kind='campaign' не содержит sitelinks — пробуем отдельную kind='sitelinks'.
    # Это тот же фолбэк что и на v5-пути (_build_tp1_from_pack: _slepok_sitelinks_for).
    if not sitelinks:
        sitelinks = _slepok_sitelinks_for(slepok, site_type)[:8]
    _prefer_callout_ids = [int(x) for x in (prefer_callout_ids or []) if str(x or "").strip().isdigit()]
    callout_ids = _prefer_callout_ids[:8]
    try:
        if callout_ids:
            pass
        elif prefer_callout_texts:                        # выбранные пользователем → создаём/находим их ID
            callout_ids = list(_ensure_callout_exts(token, login, prefer_callout_texts).values())[:8]
            if not callout_ids:
                # _ensure_callout_exts упал (152?) → пробуем Grid (без баллов)
                try:
                    _clean = [(str(t) or "").strip()[:25] for t in prefer_callout_texts if t]
                    _clean = [t for t in _clean if t]
                    if _clean:
                        _gc_co = gf.GridClient(login)
                        callout_ids = list(_gc_co.add_callouts(_clean).values())[:8]
                except Exception:  # noqa: BLE001
                    pass
        if not callout_ids:                               # ничего не выбрано / не создалось → уточнения аккаунта
            callout_ids = _dedup_callout_ids(cl.get_callouts())  # #24: normalize+dedup
        if not callout_ids:
            # v5 get_callouts пусто (новый аккаунт / 152 на get) → Grid (без баллов)
            try:
                _gc_co = gf.GridClient(login)
                callout_ids = _dedup_callout_ids(_gc_co.get_callouts())  # #24: normalize+dedup
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return {"cl": cl, "feeds": feeds, "promos": promos, "minus_set": minus_set,
            "sitelinks": sitelinks, "default_text": default_text, "callout_ids": callout_ids}


def _create_tp5_single(data: dict, token: str, login: str, name: str, pay: str,
                       goal_id: int, cpa_rub: int, budget_rub: int,
                       counter_id: int, region_ids: list, href: str, feed_id: int,
                       feed_name: str, slepok: str, site_type: str, r_code: str,
                       corr: dict, ret_map: dict,
                       feed_models: dict | None = None,
                       titles: list | None = None,
                       city: str = "", segment: str | None = None,
                       autotarget: bool = False, products_only: bool = False,
                       grid_cookie: str | None = None) -> dict:
    """Одна боевая tp5 (комбинированная, как эталон Щербаковой 2026-06-22):
    TEXT_CAMPAIGN (поиск-only) + бренд-группы из пака M3 (TextAd + ListingAd + ShoppingAd).

    pay='tcpa' → AVERAGE_CPA (cpc-вариант, кодер tp5_cpc_site)
    pay='cpa'  → PAY_FOR_CONVERSION (cpa-вариант, кодер tp5_cpa_site)

    Каждая группа = ct-папка пака M3 (tp5) → кодер ct{N}_aon_n000_{r}_ct010_ag011_g00.
    FeedFilterConditions по collectionId если feed_models передан; иначе по всему фиду.
    Grid-докрутка: места показа (gallery + search), ассеты кампании, минус, инварианты.
    Корректировки «Глобальных правил» — ПОСЛЕ Grid (он перезаписывает bidModifiers).
    """
    # ── 1. TEXT_CAMPAIGN через _create_search_test_campaign ─────────────────────
    res = _create_search_test_campaign(
        token, login, name, audiences=[],
        counter_id=counter_id, mode="search", pay=pay,
        goal_id=goal_id, cpa_rub=cpa_rub, budget_rub=budget_rub)
    if not res.get("ok"):
        return {"ok": False, "name": name, "feed": feed_name, "error": res.get("error", "campaigns.add упал")}
    cid = res["campaign_id"]

    # ── 2. Наполнение: бренд-группы из пака M3 с TextAd + ListingAd + ShoppingAd ──
    # _build_tp1_from_pack → _build_tp1_adgroups: with_shopping=True даёт «Т+Л+ТОВ» в каждой группе.
    # Кодер группы: _tp1_group_name(ct, r_code, brand, with_shopping=True)
    #   → ct{N}_aon_n000_{r}_ct010_ag011_g00 — {Бренд}  (CODER.md §tp5 2026-06-22)
    texts = [data.get("default_text") or "Официальный дилер. Тест-драйв и выгодные условия по кредиту. Авто в наличии."]
    tp5_build: dict = {}
    try:
        tp5_build = _build_tp1_from_pack(
            token, login, cid, slepok, site_type, region_ids,
            href, r_code, titles, texts, counter_id=counter_id,
            feed_id=feed_id, with_shopping=bool(feed_id),
            feed_models=feed_models, city=city,
            segment=segment, autotarget=autotarget, products_only=products_only,
            tp_code="tp5")
    except Exception as e:  # noqa: BLE001
        tp5_build = {"error": str(e)[:240]}

    # Защита от пустышек: кампания создана, но сборка не дошла (нет групп) → удаляем недоделанную.
    if tp5_build.get("error") or tp5_build.get("skipped") or not tp5_build.get("adgroups"):
        _delete_partial_campaign(token, login, cid)
        return {"ok": False, "name": name, "feed": feed_name, "campaign_id": cid, "partial_deleted": True,
                "error": "tp5 не дозаполнена: " + str(
                    tp5_build.get("error") or tp5_build.get("skipped") or "группы не созданы")[:200]}

    # ── 3. Текст по умолчанию для ShoppingAd ────────────────────────────────────
    _default_text = data.get("default_text") or "Официальный дилер. Тест-драйв и выгодные условия по кредиту. Авто в наличии."
    _shop_ids = tp5_build.get("shopping_ad_ids") or []
    if feed_id and not _shop_ids:
        _delete_partial_campaign(token, login, cid)
        return {"ok": False, "name": name, "feed": feed_name, "campaign_id": cid, "partial_deleted": True,
                "error": "tp5 не дозаполнена: фидовая кампания создана без ShoppingAd"}
    if _shop_ids and feed_id:
        try:
            _gcl = gf.GridClient(login, cookie=grid_cookie)
            _gcl.set_default_text(
                _shop_ids, feed_id, _default_text,
                filters_by_ad_id=(tp5_build.get("shopping_filters") or {}),
            )
            tp5_build["shopping_text_set"] = len(_shop_ids)
            try:
                _la = _add_listing_ads_v501(token, login, tp5_build.get("listing_build_items") or [])
                tp5_build["listing_ads"] = len(_la)
            except Exception as _le:  # noqa: BLE001
                _msg = str(_le)[:160]
                if "Недостаточно баллов" in _msg or "152" in _msg:
                    try:
                        _la = _gcl.add_listing_ads_by_shopping_ads(_shop_ids)
                        tp5_build["listing_ads"] = len(_la)
                        tp5_build.setdefault("warnings", []).append("listing-v501: 152 -> fallback grid")
                    except Exception as _lge:  # noqa: BLE001
                        tp5_build.setdefault("warnings", []).append(f"listing-grid: {str(_lge)[:160]}")
                else:
                    tp5_build.setdefault("warnings", []).append(f"listing-v501: {_msg}")
        except Exception as _e:  # noqa: BLE001
            tp5_build.setdefault("warnings", []).append(f"shopping text: {str(_e)[:120]}")
    if feed_id and not int(tp5_build.get("listing_ads") or 0):
        _delete_partial_campaign(token, login, cid)
        return {"ok": False, "name": name, "feed": feed_name, "campaign_id": cid, "partial_deleted": True,
                "error": "tp5 не дозаполнена: фидовая кампания создана без ListingAd"}

    # ── 4. Grid-докрутка: места показа (gallery + search), ассеты кампании, минус, инварианты ──
    _assets = _resolve_campaign_assets(
        token, login, href,
        sitelinks=_ai_common_sitelinks(login, slepok, site_type, city, "tp5"),
        assets=data, slepok=slepok, site_type=site_type,
        grid_cookie=grid_cookie,
    )
    slset_grid = _assets.get("sitelink_set_id")
    grid_warn: str | None = None  # B1: Grid-сбой не блокирует, но должен быть виден в ответе
    try:
        gridc = gf.GridClient(login)
        gridc.finalize(
            cid, name=name, goal_id=goal_id, cpa_rub=cpa_rub, weekly_rub=budget_rub,
            counter_ids=[counter_id] if counter_id else [],
            pay_for_conversion=(pay == "cpa"),
            callout_ids=_assets["callout_ids"], sitelink_set_id=slset_grid,
            promo_id=(_assets["promos"][0] if _assets["promos"] else None),
            minus_set_ids=[_assets["minus_set"]] if _assets["minus_set"] else None)
    except Exception as _grid_exc:  # noqa: BLE001
        # Grid-докрутка не блокирует создание, но сбой ДОЛЖЕН быть виден:
        # при упавшем Grid кампания останется без товарной галереи (placementTypes не выставлен)
        # и без ассетов (callouts/sitelinks/promo). ENABLE_COMPANY_INFO=NO в v5 Settings уже
        # защищает от Карт/организации. Требуется ретрай Grid вручную.
        grid_warn = f"Grid-докрутка не прошла (товарная галерея/ассеты НЕ выставлены): {str(_grid_exc)[:200]}"

    # ── 5. Корректировки «Глобальных правил» — ПОСЛЕ Grid ───────────────────────
    nmod = 0
    try:
        v501cl = cmc.DirectV501Client(token, login)
        nmod = gf.apply_corrections(v501cl, cid, corr.get("demographic", []),
                                    corr.get("audiences", []), ret_map)
    except Exception:  # noqa: BLE001
        pass
    out = {"ok": True, "campaign_id": cid, "id": cid, "name": name, "feed": feed_name,
           "tp5_build": tp5_build, "modifiers_set": nmod,
           "url": f"https://direct.yandex.ru/dna/campaign/{cid}?ulogin={login}"}
    if grid_warn:
        out["grid_warn"] = grid_warn  # B1: Grid-сбой виден в ответе; товарная галерея требует ретрая
    return out


def _create_tp5_campaign(token: str, login: str, base_name: str, counter_id: int,
                         goal_id: int, cpa_rub: int, budget_rub: int, region_ids: list,
                         href: str, slepok: str, site_type: str, r_code: str,
                         corr: dict, ret_map: dict, job=None,
                         titles: list | None = None,
                         agency: str = "", city: str = "",
                         segment: str | None = None, autotarget: bool = False,
                         products_only: bool = False, no_cpa: bool = False,
                         single_feed: bool = False,
                         grid_cookie: str | None = None) -> dict:
    """Боевая tp5 (комбинированная, эталон Щербаковой 2026-06-22): TEXT_CAMPAIGN поиск-only
    + бренд-группы из пака M3 (TextAd + ListingAd + ShoppingAd), кодер ct010_ag011.
    FAN-OUT: мультиплицируется по ВСЕМ URL-фидам аккаунта — каждый фид своя пара cpc+cpa.
    single_feed=True → только ПЕРВЫЙ фид (галочка «по одному фиду»).
    agency — для _account_model_feeds (collectionId по модели из listings фида).
    base_name — канон cpc: 'tp5_cpc_site — Поиск + Динамика + Товарная галерея'."""
    data = _tp5_account_data(token, login, slepok, site_type, agency)
    if not data["feeds"]:
        return {"ok": False, "name": base_name, "error": "нет URL-фидов на аккаунте для tp5"}
    if single_feed:
        data["feeds"] = data["feeds"][:1]                # «по одному фиду» — только первый
    # Модельные коллекции фидов (listings 'model_N') — для FeedFilterConditions по модели.
    mf_list = _account_model_feeds(login, agency) if agency else []
    results = []
    for feed_id, feed_name in data["feeds"]:                  # FAN-OUT: каждый фид → своя пара
        if job and job.get("cancel"):                        # отмена: стоп ПЕРЕД следующим фидом
            break
        nm_cpc = (f"{base_name} — {feed_name}" if not _is_site_domain_name(feed_name, href)
                  else base_name)
        nm_cpa = nm_cpc.replace("tp5_cpc_site", "tp5_cpa_site", 1)
        fm_entry = next((f for f in mf_list if int(f["id"]) == int(feed_id)), None)
        feed_models = fm_entry["models"] if fm_entry else None
        _pairs = [(nm_cpc, "tcpa")] if no_cpa else [(nm_cpc, "tcpa"), (nm_cpa, "cpa")]
        for nm, pay in _pairs:
            if job and job.get("cancel"):                    # отмена: стоп ПЕРЕД следующей кампанией пары
                break
            try:
                results.append(_create_tp5_single(
                    data, token, login, nm, pay, goal_id, cpa_rub, budget_rub,
                    counter_id, region_ids, href, feed_id, feed_name,
                    slepok, site_type, r_code, corr, ret_map,
                    feed_models=feed_models, titles=titles, city=city,
                    segment=segment, autotarget=autotarget, products_only=products_only,
                    grid_cookie=grid_cookie))
                _bump_job(job, True)                         # live: +1 кампания
            except Exception as e:  # noqa: BLE001
                results.append({"ok": False, "name": nm, "error": str(e)[:240]})
                _add_job_err(job, str(e)[:240])
                _bump_job(job, False)
            if job:
                _job_db_progress(job)
    ok = any(r.get("ok") for r in results)
    first_id = next((r["campaign_id"] for r in results if r.get("ok")), None)
    return {"ok": ok, "name": base_name, "campaign_id": first_id, "id": first_id,
            "launched": False, "campaigns": results,
            "url": next((r.get("url") for r in results if r.get("ok")), "")}


def _create_tp3_single(data: dict, token: str, login: str, name: str, mode: str,
                       pay_for_conv: bool, goal_id: int, cpa_rub: int, budget_rub: int,
                       counter_id: int, region_ids: list, href: str, feed_id: int,
                       feed_name: str, group_name: str, corr: dict, ret_map: dict) -> dict:
    """Одна боевая tp3 «Товарная галерея» (ЕПК, канал РСЯ, товарная по ВСЕМУ фиду).
    Отличие от tp5: канал network (network_cpa=AVERAGE_CPA / network_payconv=PAY_FOR_CONVERSION) +
    РСЯ-докрутка (_finalize_rsya, чистый network-only). Группа — ShoppingAd+ListingAd по всему фиду
    (без ТГО, без модель-фильтра — товарная галерея целиком). UTM на группе."""
    cl = data["cl"]
    spec = cmc.UnifiedCampaignSpec(
        name=name, client_login=login, oauth_token=token, mode=mode,
        region_ids=region_ids, counter_ids=[counter_id], goal_id=goal_id,
        network_average_cpa=int(cpa_rub) * 1_000_000, search_cpa=int(cpa_rub) * 1_000_000,
        apply_invariants=True)
    cid = cl.create_unified_campaign(spec, launch=False)
    # Защита от пустышек: группа/товарное объявление не создались → удаляем недоделанную кампанию.
    try:
        ag = cl.add_product_adgroup(cid, name=group_name, region_ids=region_ids)
        shop = cl.add_shopping_ad(ag, feed_id=feed_id) if ag else None
    except Exception as _e:  # noqa: BLE001
        ag, shop = None, None
    if not ag or not shop:
        _delete_partial_campaign(token, login, cid)
        return {"ok": False, "name": name, "feed": feed_name, "campaign_id": cid, "partial_deleted": True,
                "error": "tp3 не дозаполнена: группа/товарное объявление не созданы"}
    cl.add_listing_ad(ag, feed_id=feed_id)
    try:
        cl._call("adgroups", "update", {"AdGroups": [{"Id": ag, "TrackingParams": cmc.UTM_TEMPLATE}]})
    except Exception:  # noqa: BLE001
        pass
    slset = None
    if data["sitelinks"]:
        base = href.rstrip("/")
        # Быстрые ссылки ведут ТОЛЬКО на главную страницу (base_href без пути).
        # /sl1../sl8 давали 404 — исправлено: Href = главная для всех ссылок.
        sl = [{"Title": s.get("title", ""), "Description": s.get("description", ""),
               "Href": base} for s in data["sitelinks"]]
        try:
            slset = cl.add_sitelinks_set(sl)
        except Exception:  # noqa: BLE001
            slset = None
    warn = None
    # РСЯ-докрутка: уточнения/промо/ссылки уровня кампании, чистый РСЯ (как tp1)
    try:
        _finalize_rsya(
            login, cid, name=name, goal_id=goal_id, cpa_rub=cpa_rub,
            weekly_rub=(budget_rub or int(cpa_rub) * 10),
            counter_ids=[counter_id] if counter_id else [], pay_for_conversion=pay_for_conv,
            callout_ids=data["callout_ids"], sitelink_set_id=slset,
            promo_id=(data["promos"][0] if data["promos"] else None),
            minus_set_ids=[data["minus_set"]] if data["minus_set"] else None)
    except Exception as e:  # noqa: BLE001
        warn = f"РСЯ-докрутка упала: {str(e)[:140]}"
    # текст по умолчанию на товарном объявлении (как в tp5)
    if data["default_text"]:
        try:
            gf.GridClient(login).set_default_text([shop], feed_id, data["default_text"])
        except Exception:  # noqa: BLE001
            pass
    # корректировки «Глобальных правил» — ПОСЛЕ Grid (он перезаписывает bidModifiers)
    nmod = 0
    try:
        nmod = gf.apply_corrections(cl, cid, corr.get("demographic", []),
                                    corr.get("audiences", []), ret_map)
    except Exception:  # noqa: BLE001
        pass
    res = {"ok": True, "campaign_id": cid, "id": cid, "name": name, "feed": feed_name,
           "modifiers_set": nmod,
           "url": f"https://direct.yandex.ru/dna/campaign/{cid}?ulogin={login}"}
    if warn:
        res.setdefault("warnings", []).append(warn)
    return res


def _create_tp3_campaign(token: str, login: str, base_name: str, counter_id: int,
                         goal_id: int, cpa_rub: int, budget_rub: int, region_ids: list,
                         href: str, slepok: str, site_type: str, r_code: str,
                         corr: dict, ret_map: dict, job=None, no_cpa: bool = False,
                         single_feed: bool = False, agency: str = "") -> dict:
    """Боевая tp3 «Товарная галерея» (ЕПК, РСЯ, товарная по фиду) — ПАРА cpc+cpa.
    FAN-OUT (CODER.md): мультиплицируется по ВСЕМ URL-фидам аккаунта — каждый фид своя пара,
    имя несёт название фида. single_feed=True → только ПЕРВЫЙ фид. job — live-счётчик."""
    data = _tp5_account_data(token, login, slepok, site_type, agency)
    if not data["feeds"]:
        return {"ok": False, "name": base_name, "error": "нет URL-фидов на аккаунте для tp3"}
    if single_feed:
        data["feeds"] = data["feeds"][:1]                # «по одному фиду» — только первый
    # ct009 = «Товарное/Фид» (CODER.md ag_part5): ShoppingAd+ListingAd по фиду.
    group_name = f"ct0000_aon_n000_{r_code}_ct009_ag001_g00 — Товарная галерея"
    results = []
    for feed_id, feed_name in data["feeds"]:                  # FAN-OUT: каждый фид → своя пара
        if job and job.get("cancel"):                        # отмена: стоп ПЕРЕД следующим фидом
            break
        nm_cpc = (f"{base_name} — {feed_name}" if not _is_site_domain_name(feed_name, href)
                  else base_name)
        nm_cpa = nm_cpc.replace("tp3_cpc_site", "tp3_cpa_site", 1)
        _t3 = ([(nm_cpc, "network_cpa", False)] if no_cpa
               else [(nm_cpc, "network_cpa", False), (nm_cpa, "network_payconv", True)])
        for nm, mode, pay in _t3:
            if job and job.get("cancel"):                    # отмена: стоп ПЕРЕД следующей кампанией пары
                break
            try:
                results.append(_create_tp3_single(
                    data, token, login, nm, mode, pay, goal_id, cpa_rub, budget_rub,
                    counter_id, region_ids, href, feed_id, feed_name, group_name, corr, ret_map))
                _bump_job(job, True)                         # live: +1 кампания
            except Exception as e:  # noqa: BLE001
                results.append({"ok": False, "name": nm, "error": str(e)[:240]})
                _add_job_err(job, str(e)[:240])
                _bump_job(job, False)
            if job:
                _job_db_progress(job)
    ok = any(r.get("ok") for r in results)
    first_id = next((r["campaign_id"] for r in results if r.get("ok")), None)
    return {"ok": ok, "name": base_name, "campaign_id": first_id, "id": first_id,
            "launched": False, "campaigns": results,
            "url": next((r.get("url") for r in results if r.get("ok")), "")}


def _load_corrections(city: str) -> dict:
    """Корректировки ставок из «Глобальных правил» (мерж город→'*', город приоритетнее).
    → {"audiences":[{name,pct}], "demographic":[{kind,key,pct}]}."""
    city = (city or "*").strip() or "*"
    out = {"audiences": [], "demographic": []}
    try:
        conn = _victory_conn()
    except Exception:  # noqa: BLE001
        return out
    try:
        cur = conn.cursor()
        cur.execute("SELECT name, pct FROM public.direct_audience_corrections WHERE city='*'")
        ad = {n: p for n, p in cur.fetchall()}
        if city != "*":
            cur.execute("SELECT name, pct FROM public.direct_audience_corrections WHERE city=%s", (city,))
            for n, p in cur.fetchall():
                ad[n] = p
        out["audiences"] = [{"name": n, "pct": int(p or 0)} for n, p in ad.items()]
        cur.execute("SELECT kind, key, pct FROM public.direct_demographic_corrections WHERE city='*'")
        dm = {(k, key): p for k, key, p in cur.fetchall()}
        if city != "*":
            cur.execute("SELECT kind, key, pct FROM public.direct_demographic_corrections WHERE city=%s", (city,))
            for k, key, p in cur.fetchall():
                dm[(k, key)] = p
        out["demographic"] = [{"kind": k, "key": key, "pct": int(p or 0)} for (k, key), p in dm.items()]
    except Exception:  # noqa: BLE001
        pass
    finally:
        conn.close()
    return out


def _account_retargeting(token: str, login: str) -> dict:
    """{имя_условия → retargeting_condition_id} аккаунта — для матчинга аудиторных корректировок."""
    if not token:
        return {}
    try:
        j = _v5_get("retargetinglists", token, login, ["Id", "Name"])
        return {a["Name"]: a["Id"] for a in (j.get("result") or {}).get("RetargetingLists", []) if a.get("Name")}
    except Exception:  # noqa: BLE001
        return {}


def _seg_key(name: str) -> tuple:
    """Ключ матчинга сегмента: (класс, остаток). Первый токен имени — это ИСТОЧНИК:
    `geo` в Глобальных правилах = плейсхолдер города → на аккаунте заменён кодом города
    (geo_all_visit_none_lal → kem_all_visit_none_lal). `self` — литерал, остаётся `self`.
    Поэтому класс = 'self' если префикс 'self', иначе 'geo' (любой код города)."""
    pfx, _, rest = (name or "").partition("_")
    return ("self" if pfx == "self" else "geo", rest)


def _corrections_by_segment(corr_audiences: list, seg_names: list) -> dict:
    """Для каждого сегмента аккаунта → процент корректировки из «Глобальных правил».
    Матчинг: сначала точный по (класс, остаток); если там пусто/0, а правило С ТЕМ ЖЕ
    остатком в ДРУГОМ классе имеет ненулевой pct — берём самое сильное (по |pct|).
    Это чинит кейс, когда исключение задано как `self_ms_all_none_minus=-100`, а на аккаунте
    сегмент `kem_ms_all_none_minus` (гео-класс): −100 всё равно доезжает. Явный ненулевой
    pct своего класса НЕ перетирается (self остаётся self). → {имя_сегмента: pct|None}."""
    by_classrest: dict = {}                       # (класс, остаток) → pct
    by_rest: dict = {}                            # остаток → [ненулевые pct]
    for a in corr_audiences:
        k = _seg_key(a.get("name") or "")
        p = int(a.get("pct") or 0)
        by_classrest[k] = p
        if p != 0:
            by_rest.setdefault(k[1], []).append(p)
    out: dict = {}
    for nm in seg_names:
        k = _seg_key(nm)
        p = by_classrest.get(k)                   # точный по классу
        if not p:                                 # None или 0 → пробуем ненулевое кросс-классом
            alt = by_rest.get(k[1])
            if alt:
                p = max(alt, key=abs)
        out[nm] = p
    return out


def _correction_bidmodifiers(campaign_id: int, corr: dict, ret_map: dict) -> list:
    """BidModifiers-items (Demographics + Retargeting) для bidmodifiers.add — только pct≠0.
    pct → BidModifier: clamp(0,1300, 100+pct). −100% → 0 (исключение).
    Аудитории матчатся по (класс, остаток): `geo_X` правила ↔ `<город>_X` аккаунта; `self_X` ↔ `self_X`."""
    items = []
    dem = []
    for d in corr.get("demographic", []):
        pct = int(d.get("pct") or 0)
        if pct == 0:
            continue
        bm = max(0, min(1300, 100 + pct))
        if d["kind"] == "age":
            dem.append({"Age": d["key"], "BidModifier": bm})
        elif d["kind"] == "gender":
            dem.append({"Gender": d["key"], "BidModifier": bm})
    if dem:
        items.append({"CampaignId": int(campaign_id), "DemographicsAdjustments": dem})
    # Идём ОТ сегментов аккаунта: для каждого — процент из правил (с кросс-классовым
    # фолбэком для исключений вроде ms). Применяем только ненулевые.
    seg_pct = _corrections_by_segment(corr.get("audiences", []), list(ret_map.keys()))
    ret = []
    for nm, rid in ret_map.items():
        pct = seg_pct.get(nm)
        if not pct:                              # None или 0 → корректировку не вешаем
            continue
        ret.append({"RetargetingConditionId": int(rid), "BidModifier": max(0, min(1300, 100 + int(pct)))})
    if ret:
        items.append({"CampaignId": int(campaign_id), "RetargetingAdjustments": ret})
    return items


def _grid_bid_modifiers(campaign_id: int, corr: dict, ret_map: dict) -> dict:
    """Корректировки ставок для GRID (bidModifiers объект ЕПК) — БЕЗ баллов. Реверс:
    HAR21 (AddCampaigns, retargeting) + JS-код из HAR23 entry 163 (demographic/demography).
    На куки-пути v5 bidmodifiers.add недоступен (стоит баллов) → ставим через Grid.

    ВАЖНО: в Grid `percent` = ДЕЛЬТА корректировки напрямую (НЕ 100+pct как в v5 BidModifier).
    age — Grid GdAgeTypeInput в живой схеме 2026-06-25: "_0_17"/"_18_24"/"_25_34"/"_35_44"/
    "_45_54"/"_55_" (не совпадает с v5/БД-ключами "AGE_*"). Поэтому здесь маппим БД-ключи
    вида AGE_18_24 → _18_24 перед отправкой в Grid, иначе AddCampaigns падает validation error.
    gender=None → корректировка для обоих полов сразу.
    → {} если нечего ставить."""
    result: dict = {}
    # ── retargeting ──────────────────────────────────────────────────────────────
    seg_pct = _corrections_by_segment(corr.get("audiences", []), list(ret_map.keys()))
    ret_adj = []
    for nm, rid in (ret_map or {}).items():
        pct = seg_pct.get(nm)
        if not pct or int(pct) <= 0:                 # Grid принимает только положительный percent
            continue
        ret_adj.append({"percent": int(pct), "retargetingConditionId": str(rid)})
    if ret_adj:
        result["bidModifierRetargeting"] = {
            "campaignId": str(campaign_id), "enabled": True,
            "adjustments": ret_adj, "type": "RETARGETING_MULTIPLIER"}
    # ── demographic (age/gender) ─────────────────────────────────────────────────
    # Реверс JS-кода HAR23/entry163: bidModifierDemographics.adjustments[].{percent,age,gender,id}.
    # percent = дельта (как retargeting); age/gender — строки Grid enum.
    # ВАЖНО: Grid GdAgeTypeInput НЕ содержит AGE_0_17 (есть в v5, нет в Grid) — пропускаем.
    # id=None → Grid сам назначит id при создании (для новых корректировок).
    _GRID_AGE_MAP = {
        "AGE_0_17": "_0_17",
        "AGE_18_24": "_18_24",
        "AGE_25_34": "_25_34",
        "AGE_35_44": "_35_44",
        "AGE_45_54": "_45_54",
        "AGE_55": "_55_",
    }
    dem_adj = []
    for d in corr.get("demographic", []):
        pct = int(d.get("pct") or 0)
        if pct <= 0:                                 # Grid Add/UpdateCampaigns валидирует percent > 0
            continue
        adj_entry: dict = {"percent": pct, "id": None}
        if d.get("kind") == "age":
            _grid_age = _GRID_AGE_MAP.get(d["key"])
            if not _grid_age:
                continue
            adj_entry["age"] = _grid_age
            adj_entry["gender"] = None          # оба пола
        elif d.get("kind") == "gender":
            adj_entry["age"] = None             # все возраста
            adj_entry["gender"] = d["key"]
        else:
            continue
        dem_adj.append(adj_entry)
    if dem_adj:
        result["bidModifierDemographics"] = {
            "campaignId": str(campaign_id), "enabled": True,
            "adjustments": dem_adj, "type": "DEMOGRAPHY_MULTIPLIER"}
    return result


def _apply_corrections(token: str, login: str, campaign_id: int, corr: dict, ret_map: dict) -> tuple:
    """Применить корректировки «Глобальных правил» к кампании (bidmodifiers.add). → (кол-во, ошибка|None)."""
    items = _correction_bidmodifiers(campaign_id, corr, ret_map)
    if not items:
        return 0, None
    j = _v5_call("bidmodifiers", "add", token, login, {"BidModifiers": items})
    if "error" in j:
        return 0, _v5_err(j)
    n = 0
    for r in (j.get("result") or {}).get("AddResults", []):
        n += len(r.get("Ids") or [])
    return n, None


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


@bp.route("/api/create_set", methods=["POST"])
@_direct_access
def _run_master_product_item(*, it, name, href, region_ids, counter_id, goal_id,
                             cpa, launch, client, agent, eff_site, ctx,
                             tpl_titles, tpl_texts, tpl_sitelinks, rs, login,
                             _st_token, _w_agency, _stream_agent, _job, _tp7_mf):
    """tp6 (МК) / tp7 (Товарка) item handler, вынесен из api_create_set.

    Same-module функция (не отдельный файл): ветка тянет 50+ module-global helper'ов и
    мутирует общий ленивый кэш _tp7_mf между item'ами — DI/отдельный модуль тут = 50+
    параметров и высокий риск. Здесь все helper'ы резолвятся через globals модуля.
    Возвращает (results, _tp7_mf) — обновлённый кэш фидов возвращаем наружу.
    """
    results = []
    is_manual = str(it.get("variant", "")).endswith("manual")
    is_product = it.get("type") == "product"
    targeting_mode = str(it.get("targeting_mode") or (
        "keywords" if is_manual else _tp67_targeting_mode({
            "name": it.get("position_name") or name,
            "group": it.get("audience_cat") or "",
            "label": it.get("position_name") or "",
            "code": it.get("structure_code") or "",
        })
    )).strip()
    # Ось посадки: kviz-кодер → домен/quiz, иначе обычный домен
    it_sq = it.get("sq") or "site"
    it_href = href + ("/quiz" if it_sq == "kviz" else "")
    # Нативные интересы слепка для этого tp (МК=tp6 / Товарка=tp7) — «как в слепках».
    # Приоритет: интересы КОНКРЕТНОЙ категории из плана (отдельная кампания на категорию);
    # фолбэк — объединённый список (старое поведение, если план без категорий).
    it_tp = it.get("tp") or ("tp7" if is_product else "tp6")
    native_ints = ([str(x) for x in (it.get("interest_ids") or []) if str(x).strip()]
                   or _slepok_audiences_for(agent, eff_site, it_tp))
    # Контент слепка (приоритет) + добор из шаблонов до ПОЛНЫХ слотов (Мультибренд чист от б/у):
    # заголовки до 5, тексты до 3, быстрые ссылки до 8.
    # ПРАВИЛО ПОЛЬЗОВАТЕЛЯ: контент по 4-значному ct кодера. ct=марка/модель → её
    # картинка+заголовки; ct0000/нет марки → общий текст (tp6/tp7 сейчас всегда ct0000).
    c_brand, c_ct = _brand_ct_from_coder(it)
    it_keywords: list[str] = []
    it_minus_keywords: list[str] = []
    it_targeting_warnings: list[str] = []
    if targeting_mode == "keywords":
        it_keywords, it_minus_keywords = _tp67_keywords_for(
            agent, eff_site, it_tp, c_ct or str(it.get("ct") or "ct0000"), ctx.get("city") or "",
            it.get("position_name") or it.get("audience_cat") or name,
            it_sq,
        )
        if not it_keywords:
            _err = (f"tp6/tp7 КС без ключей: slepok={agent}, site_type={eff_site}, "
                    f"tp={it_tp}, ct={c_ct or it.get('ct') or 'ct0000'}")
            targeting_mode = "autotarget"
            it_targeting_warnings.append(_err + " → fallback autotarget")
            _add_job_err(_job, it_targeting_warnings[-1])
    it_audiences = _audience_objects(native_ints) if targeting_mode == "audience" else []
    if targeting_mode == "audience" and not it_audiences:
        _err = (f"tp6/tp7 аудитория без аудиторий слепка: slepok={agent}, "
                f"site_type={eff_site}, tp={it_tp}, category={it.get('audience_cat') or it.get('position_name') or ''}")
        targeting_mode = "autotarget"
        it_targeting_warnings.append(_err + " → fallback autotarget")
        _add_job_err(_job, it_targeting_warnings[-1])
    if is_product and not c_brand and it_sq != "kviz":
        it_href = re.sub(r"/+$", "", href) + "/auto"
    _sc = _slepok_campaign_content(agent, eff_site)
    if c_brand:                                    # кодер несёт модель -> ведём контент по ней
        _b0 = c_brand.split()[0].lower()
        _model_t = [t for t in _sc["titles"] if _b0 in t.lower()]
        title_primary = _brand_title_set(c_brand, ctx.get("city") or "") + _model_t
        title_supp = title_primary + _sc["titles"] + tpl_titles + _GENERIC_TITLE_FILLERS
    elif is_product:
        # tp7 ct0000 (общая кампания) - заголовки ТОЛЬКО без марки/модели.
        # _sc["titles"] часто брендовые («Haval…», «Chery…») - НЕ используем как base.
        title_primary = _GENERIC_AT_TITLES
        title_supp = _GENERIC_AT_TITLES + _GENERIC_TITLE_FILLERS
    else:                                          # tp6 МК ct0000 → общие (баг #8: без брендов)
        # баг #8: _sc["titles"] часто содержат марки («Haval у дилера»…) — для общей МК (ct0000)
        # это неверно. Используем _GENERIC_AT_TITLES как base; _sc["titles"] только в supp-добавке.
        title_primary = _GENERIC_AT_TITLES
        title_supp = _sc["titles"] + tpl_titles + _GENERIC_TITLE_FILLERS
    # ЖЁСТКОЕ ПРАВИЛО tp6/tp7: РОВНО 5 заголовков / 3 текста / 8 быстрых ссылок.
    # ГОРОД В КОНТЕНТЕ обязан совпадать с городом аккаунта.
    _acc_city = (ctx.get("city") or "").strip()
    _, _cities_bl = _title2_blocklist()
    _cf = lambda lst: _replace_foreign_city(_drop_used_car(lst, eff_site), _acc_city, _cities_bl)  # noqa: E731
    # БАГ 1: для tp7 ct0000 - base = ТОЛЬКО общие заголовки, игнорируем it.get("titles")
    if not c_brand:
        # ct0000 (общая кампания) — tp6 МК И tp7 Товарка: base = только общие заголовки,
        # игнорируем it.get("titles") (могут содержать бренды от ИИ).
        # баг #8: фильтр брендовых токенов распространён на tp6 ct0000 (раньше только tp7).
        _t_base = list(title_primary)
        _ct_brand_tokens: set = {v.split()[0].lower() for v in kp.feeds_ct_model().values() if v}
        _foreign_brand_tokens, _foreign_city_tokens = _title2_blocklist()
    else:
        _t_base = title_primary
        _ct_brand_tokens = set()
        _foreign_brand_tokens, _foreign_city_tokens = set(), set()

    def _common_content_ok(_s: str) -> bool:
        if c_brand:
            return True
        _words = set(re.sub(r"[^\wа-яё]+", " ", str(_s or "").lower()).split())
        return not (_words & _foreign_brand_tokens) and not (_words & _foreign_city_tokens)
    # Сборка заголовков с пост-обработкой (БАГ 1+2+3+4+7+8)
    _raw_titles = _fill_variants(_cf(_t_base), _cf(title_supp) + _GENERIC_TITLE_FILLERS, 12)
    it_titles = []
    _seen_tk: set = set()
    for _t in _raw_titles:
        if not _t or not str(_t).strip():
            continue
        _t = _sanitize_content(str(_t), max_len=56)   # БАГ 3+4+8
        _t = _fill_title(_replace_sep_hyphen(_replace_emdash(_strip_credit_rate(_t))), 45, 56)  # добить до 45-56 (#26)
        if not _t or _is_bad_start(_t) or _bad_ad_title(_t) or not _common_content_ok(_t) or not _has_number(_t):
            continue  # number-gate: каждый заголовок tp6/tp7 обязан содержать цифру
        if c_brand:
            _own = _own_brand_tokens(c_brand)
            _tl = _t.lower()
            if _own and not any(re.search(r"(?<![a-zа-яё0-9])" + re.escape(tok) + r"(?![a-zа-яё0-9])", _tl)
                                for tok in _own):
                continue
        if _ct_brand_tokens:                           # БАГ 1: фильтр марок для ct0000
            _tl = _t.lower()
            if any(_tok in _tl for _tok in _ct_brand_tokens):
                continue
        _nk = _variant_norm_key(_t)                   # БАГ 2: дедуп по смысловому ключу
        if _nk and _nk in _seen_tk:
            continue
        if _nk:
            _seen_tk.add(_nk)
        it_titles.append(_t)
        if len(it_titles) >= 5:
            break
    if c_brand and len(it_titles) < 5:
        _own = _own_brand_tokens(c_brand)
        for _t in _rsya_titles(c_brand, _acc_city, eff_site, base=[], pool=title_supp,
                               is_brand=True, cap=5):
            _tl = _t.lower()
            if _own and not any(re.search(r"(?<![a-zа-яё0-9])" + re.escape(tok) + r"(?![a-zа-яё0-9])", _tl)
                                for tok in _own):
                continue
            if _is_bad_start(_t) or _bad_ad_title(_t) or not _has_number(_t):
                continue  # тот же number-gate/бэд-фильтр, что и основной цикл (12858) — иначе заголовок без цифры
            _nk = _variant_norm_key(_t)
            if _nk and _nk in _seen_tk:
                continue
            if _nk:
                _seen_tk.add(_nk)
            it_titles.append(_t)
            if len(it_titles) >= 5:
                break
    # Сборка текстов с пост-обработкой (БАГ 2+3+4+8)
    # баг #8: для ct0000 (общая кампания) тексты из ИИ/слепка могут содержать марки →
    # при ct0000 используем только _GENERIC_TEXT_FILLERS как base (без брендовых текстов ИИ).
    if not c_brand:
        _x_base = _GENERIC_TEXT_FILLERS
    else:
        _x_base = _rsya_texts((_lines(it.get("texts")) or []) + (_sc["texts"] or []),
                              eff_site, ctx.get("city") or "", c_brand, cap=8)
    _raw_texts = _fill_variants(_cf(_x_base), tpl_texts + _GENERIC_TEXT_FILLERS, 8)
    it_texts = []
    _seen_xk: set = set()
    for _x in _raw_texts:
        if not _x or not str(_x).strip():
            continue
        _x = _sanitize_content(str(_x), max_len=81)   # БАГ 3+4+8
        _x = _strip_credit_rate(_x)
        _x = _trim_to_word(_x, 81).rstrip()           # БАГ 3: обрезка по целому слову
        if not _x or _is_bad_start(_x) or _bad_ad_text(_x) or not _common_content_ok(_x) or not _has_number(_x):
            continue  # number-gate: каждый текст tp6/tp7 обязан содержать цифру
        _nk = _variant_norm_key(_x)                   # БАГ 2
        if _nk and _nk in _seen_xk:
            continue
        if _nk:
            _seen_xk.add(_nk)
        it_texts.append(_x)
        if len(it_texts) >= 3:
            break
    # #5/#6 Когерентность скидок: одно число на кампанию.
    it_titles, it_texts = _coherent_discounts(it_titles, it_texts)
    # Финальный кап ПОСЛЕ когерентности (она могла удлинить строку). БАГ 3: по слову.
    _title_cap = 5
    it_titles = list(dict.fromkeys(_trim_to_word(t, 56).rstrip() for t in it_titles if t and t.strip()))[:_title_cap]
    it_texts = _diverse_text_offers(
        list(dict.fromkeys(_trim_to_word(t, 81).rstrip() for t in it_texts if t and t.strip())),
        3,
    )
    raw_sl = it.get("sitelinks") if isinstance(it.get("sitelinks"), list) else None
    _sl_base = ([{"title": s.get("title", ""), "description": s.get("description", "")}
                 for s in raw_sl if isinstance(s, dict) and s.get("title")] if raw_sl
                else _sc["sitelinks"])
    it_sitelinks = []
    _seen_sl_keys: set = set()
    _title_has_pct = bool(_discount_pcts(it_titles))
    for _s in _fill_variants(_sl_base, tpl_sitelinks + _GENERIC_SITELINK_FILLERS, 16):
        if not isinstance(_s, dict):
            continue
        _st = _trim_to_word(_sanitize_content(_s.get("title", ""), max_len=30), 30).rstrip()
        _sd = _trim_to_word(_sanitize_content(_s.get("description", ""), max_len=60), 60).rstrip()
        if not _is_bu_site(eff_site) and _BU_RE.search(f"{_st} {_sd}"):
            continue
        if _title_has_pct and _sitelink_has_pct({"title": _st, "description": _sd}):
            continue
        if not _st or _bad_ad_sitelink(_st, _sd) or not _common_content_ok(f"{_st} {_sd}"):
            continue
        _sk = (_st.lower(), _sd.lower())
        if _sk in _seen_sl_keys:
            continue
        _seen_sl_keys.add(_sk)
        it_sitelinks.append({"title": _st, "description": _sd})
        if len(it_sitelinks) >= 8:
            break
    if len(it_sitelinks) < 8:
        for _s in _GENERIC_SITELINK_FILLERS:
            _st = _trim_to_word(_sanitize_content(_s.get("title", ""), max_len=30), 30).rstrip()
            _sd = _trim_to_word(_sanitize_content(_s.get("description", ""), max_len=60), 60).rstrip()
            _sk = (_st.lower(), _sd.lower())
            if not _is_bu_site(eff_site) and _BU_RE.search(f"{_st} {_sd}"):
                continue
            if _title_has_pct and _sitelink_has_pct({"title": _st, "description": _sd}):
                continue
            if (_st and not _bad_ad_sitelink(_st, _sd) and _common_content_ok(f"{_st} {_sd}")
                    and _sk not in _seen_sl_keys):
                _seen_sl_keys.add(_sk)
                it_sitelinks.append({"title": _st, "description": _sd})
            if len(it_sitelinks) >= 8:
                break
    try:
        from . import ai_agents as _A
        it_titles, it_texts, _t2_unused, it_sitelinks, _utp_changed = _A.unify_utp_numbers(
            it_titles, it_texts, "", it_sitelinks
        )
    except Exception:  # noqa: BLE001
        pass
    it_titles, it_texts, it_sitelinks, _pay_changed = _coherent_payments(
        it_titles, it_texts, it_sitelinks
    )
    if len(it_titles) < 5 or len(it_texts) < 3:
        try:
            from . import ai_agents as _A
            _regen_agent = _stream_agent or _A.get_agent(agent or "")
            if _regen_agent:
                _rc = _cached_campaign_content(
                    login, _regen_agent, (agent or "").strip().lower(),
                    it, eff_site, ctx.get("city") or "", avoid=it_titles,
                ) or {}
                for _t in _lines(_rc.get("titles")):
                    if len(it_titles) >= 5:
                        break
                    _t = _sanitize_content(str(_t), max_len=56)
                    _t = _strip_credit_rate(_t)[:56].rstrip()
                    if not _t or _is_bad_start(_t) or _bad_ad_title(_t) or not _common_content_ok(_t) or not _has_number(_t):
                        continue  # number-gate regen
                    if c_brand:
                        _own = _own_brand_tokens(c_brand)
                        _tl = _t.lower()
                        if _own and not any(
                            re.search(r"(?<![a-zа-яё0-9])" + re.escape(tok) + r"(?![a-zа-яё0-9])", _tl)
                            for tok in _own
                        ):
                            continue
                    if _ct_brand_tokens:
                        _tl = _t.lower()
                        if any(_tok in _tl for _tok in _ct_brand_tokens):
                            continue
                    _nk = _variant_norm_key(_t)
                    if _nk and (_nk in _seen_tk or any(_variant_norm_key(x) == _nk for x in it_titles)):
                        continue
                    if _nk:
                        _seen_tk.add(_nk)
                    it_titles.append(_t)
                for _x in _lines(_rc.get("texts")):
                    if len(it_texts) >= 3:
                        break
                    _x = _sanitize_content(str(_x), max_len=81)
                    _x = _trim_to_word(_strip_credit_rate(_x), 81).rstrip()
                    if not _x or _is_bad_start(_x) or _bad_ad_text(_x) or not _common_content_ok(_x) or not _has_number(_x):
                        continue  # number-gate regen
                    _nk = _variant_norm_key(_x)
                    if _nk and (_nk in _seen_xk or any(_variant_norm_key(x) == _nk for x in it_texts)):
                        continue
                    if _nk:
                        _seen_xk.add(_nk)
                    it_texts.append(_x)
                if _rc.get("sitelinks") and len(it_sitelinks) < 8:
                    for _s in _rc.get("sitelinks") or []:
                        if len(it_sitelinks) >= 8 or not isinstance(_s, dict):
                            break
                        _st = _trim_to_word(_sanitize_content(_s.get("title", ""), max_len=30), 30).rstrip()
                        _sd = _trim_to_word(_sanitize_content(_s.get("description", ""), max_len=60), 60).rstrip()
                        _sk2 = (_st.lower(), _sd.lower())
                        if (_st and not _bad_ad_sitelink(_st, _sd)
                                and _common_content_ok(f"{_st} {_sd}")
                                and _sk2 not in _seen_sl_keys
                                and not (not _is_bu_site(eff_site) and _BU_RE.search(f"{_st} {_sd}"))):
                            _seen_sl_keys.add(_sk2)
                            it_sitelinks.append({"title": _st, "description": _sd})
        except Exception:  # noqa: BLE001
            pass
        it_titles, it_texts = _coherent_discounts(it_titles, it_texts)
        it_titles, it_texts, it_sitelinks, _pay_changed = _coherent_payments(
            it_titles, it_texts, it_sitelinks
        )
        it_titles = list(dict.fromkeys(
            _trim_to_word(t, 56).rstrip() for t in it_titles if t and t.strip()
        ))[:5]
        it_texts = _diverse_text_offers(
            list(dict.fromkeys(_trim_to_word(t, 81).rstrip() for t in it_texts if t and t.strip())),
            3,
        )
    if len(it_titles) < 5:
        for _t in _fallback_master_titles(c_brand or "", _acc_city, eff_site, 5):
            if len(it_titles) >= 5:
                break
            _nk = _variant_norm_key(_t)
            if _nk and any(_variant_norm_key(x) == _nk for x in it_titles):
                continue
            it_titles.append(_t)
    # Префиксное поглощение: «…бесплатно» vs «…бесплатно при покупке в кредит» = почти-дубль
    # (один — расширение другого) → оставляем более информативную. ПОСЛЕ — добор из банка до 3.
    it_texts = [x for x in _dedup_prefix_absorb(it_texts) if len(str(x).strip()) >= _TP67_MIN_TEXT_LEN]
    if len(it_texts) < 3:
        it_texts = _diverse_text_offers(
            _dedup_prefix_absorb(it_texts + list(_GENERIC_TEXT_FILLERS)), 3)
    if len(it_sitelinks) < 8:
        _sl_candidates = list(it_sitelinks or [])
        for _s in _GENERIC_SITELINK_FILLERS:
            _st = _trim_to_word(_sanitize_content(_s.get("title", ""), max_len=30), 30).rstrip()
            _sd = _trim_to_word(_sanitize_content(_s.get("description", ""), max_len=60), 60).rstrip()
            if (_st and _sd and not _bad_ad_sitelink(_st, _sd)
                    and _common_content_ok(f"{_st} {_sd}")
                    and not (not _is_bu_site(eff_site) and _BU_RE.search(f"{_st} {_sd}"))):
                _sl_candidates.append({"title": _st, "description": _sd})
        try:
            from . import ai_agents as _A
            it_sitelinks = _A._dedup_sitelinks(_sl_candidates, eff_site, 8)
        except Exception:  # noqa: BLE001
            it_sitelinks = _sl_candidates[:8]
    # Картинки: СНАЧАЛА картинки ЭТОГО слепка (read_slepok_images по канону слепка), потом
    # общий пул по типу сайта. Иначе пул Мультибренда общий на ВСЕ слепки → Павлов брал бы
    # картинки Кудерко (баг: read_images ключ = тип сайта, не слепок). ВИДЕО — по логину.
    _sk = _SLEPOK_KEY.get((agent or "").lower(), (agent or "").lower())   # канон слепка
    try:
        _candidate_image_limit = 12
        if c_ct and not _is_common_ct(c_ct):        # модель/кузов в кодере (tp6/tp7)
            img_ct = _image_ct_for_content(c_ct)
            it_images = _creative_images_for_ct(eff_site, it_tp, img_ct, _sk,
                                                limit=_candidate_image_limit)
        else:                                      # ct0000 (Общее) → только безопасный общий пул.
            # M3 ct0000 и image_slepki сейчас содержат модельные баннеры, поэтому для общих
            # групп не используем их вообще.
            it_images = _creative_images_for_ct(eff_site, it_tp, "ct0000", _sk,
                                                limit=_candidate_image_limit)
    except Exception:  # noqa: BLE001
        it_images = []
    # Передаём запас кандидатов: campaign.py загрузит до 5 УСПЕШНО принятых Direct файлов.
    it_images = list(dict.fromkeys(it_images))[:12]
    try:                                          # видео per-кодер (ct): per-модель → фолбэк на логин
        it_videos = (kp.videos_for_ct(login, c_ct) if c_ct else []) or kp.videos_for_login(login)
    except Exception:  # noqa: BLE001
        it_videos = []
    it_warnings: list[str] = []
    if it_targeting_warnings:
        it_warnings.extend(it_targeting_warnings)
    if c_brand and re.search(r"(?i)\bhaval\b|хавал", c_brand) and not it_videos:
        it_warnings.append("video_missing: Haval")
    # Бюджет — из «Глобальных правил» (НЕ дефолт): cpa→budget, tcpa→cpc_budget.
    it_budget = _num(it.get("budget"), rs["cpc_budget"] if it.get("pay") == "tcpa" else rs["budget"])
    # Fix A: UAC (tp6/tp7) может получить сырой структурный слаг вместо красивого имени
    # (если items пришли не через _emit_struct/_build_name). Детектируем по шаблону
    # tp[67]_cp[ac]_(site|kviz)_ct\d+_a(on|off)_ и пересобираем через _build_name.
    # Идемпотентно: красивое имя (содержит «—») не матчит regex → не переформатируется.
    if re.match(r'^tp[67]_cp[ac]_(site|kviz)_ct\d+_a(?:on|off)_', name):
        _r_code_fix, _oblast_fix = _resolve_region(ctx.get("city") or "")
        name = _build_name(
            is_master=not is_product,
            is_auto=(targeting_mode != "keywords"),
            pay=it.get("pay") or "cpa",
            r_code=_r_code_fix,
            oblast=_oblast_fix,
            sq=it_sq,
            cat=(it.get("position_name") or it.get("audience_cat") or None),
            ct=(c_ct or it.get("ct") or "ct0000"),
        )
    # Ручная аудитория («Настроить вручную») → отражаем в кодер-имени (aoff→aon).
    disp_name = name.replace("_aoff_", "_aon_", 1) if (is_manual and "_aon_" not in name) else name
    _feed_name = (it.get("feed_name") or "").strip()
    if is_product and _feed_name and _feed_name not in disp_name and not _is_site_domain_name(_feed_name, href):
        disp_name = f"{disp_name} — {_feed_name}"
    # tp7: фид «страницы каталога» = тот же, что под товары; иначе yandex-catalog.xml.
    it_feed = (_num(it.get("feed_id"), 0) or None) if is_product else None
    it_listings = _catalog_feed(_st_token, login, it_feed or 0, _w_agency or "") if is_product else None
    # tp7 фильтр по collectionId: показываем только модель кодера (не весь фид).
    # Загружаем фиды с модельными коллекциями лениво — один раз на весь запрос.
    it_lff = []                                  # listings_feed_filters
    it_ff = []                                   # feed_filters (товарная часть)
    if is_product and it_feed and c_brand and c_ct and c_ct != "ct0000" and c_ct != "ct0111":
        it_ff = _tp7_product_feed_filters(c_brand, c_ct)
        if _tp7_mf is None:                      # ленивая загрузка (не дёргать API на каждый item)
            _tp7_mf = _account_model_feeds(login, _w_agency or "")
        fm_entry = next((f for f in _tp7_mf if f["id"] == it_feed), None)
        feed_models = fm_entry["models"] if fm_entry else None
        coll_id = _match_collection(c_brand, feed_models) if feed_models else None
        # coll_id найден → фильтр по коллекции; иначе — весь фид (марка или неизвестная модель)
        if coll_id:
            it_lff = [{"conditions": [{"field": "collectionId", "operator": "CONTAINS",
                                       "value": f'["{coll_id}"]'}]}]
    try:
        spec = cmc.MasterCampaignSpec(
            href=it_href, titles=it_titles, texts=it_texts, region_ids=region_ids,
            counter_id=counter_id, goal_id=goal_id, cpa=_num(it.get("cpa"), cpa),
            week_budget=it_budget,
            display_name=disp_name, sitelinks=it_sitelinks,
            image_files=it_images, video_files=it_videos,
            image_limit=5,
            campaign_type="product" if is_product else "master",
            feed_id=it_feed,
            listings_feed_id=(it_listings or None) if is_product else None,
            feed_filters=it_ff,                 # товарка tp7: фильтр по модели/марке, не по всему фиду
            listings_feed_filters=it_lff,        # фильтр по collectionId (tp7-only; [] = весь фид)
            keywords=it_keywords,
            minus_keywords=list(dict.fromkeys((it_minus_keywords or []) + (["отзывы"] if targeting_mode == "keywords" else []))),
            audiences=it_audiences,
            audience_interest_type="short-term",
            # #7: группа ТОЛЬКО автотаргетинг → «Подобрать оптимальную» (HAR 34): пустые
            # keywords/audiences (уже так выше) + optimal-категории + полный socdem (age_18).
            # Прочие режимы (ручные интересы/КС) — прежний набор и socdem.
            relevance_match_categories=(_TP67_OPTIMAL_CATEGORIES if targeting_mode == "autotarget"
                                        else _TP67_RELEVANCE_CATEGORIES),
            alternative_texts_enabled=False,   # #3 персонализация (адаптивные тексты) ВЫКЛ
            # tcpa = оплата за клики (PER_CLICK), cpa = оплата за конверсии (PER_CONVERSION)
            pricing="PER_CONVERSION" if it.get("pay") == "cpa" else "PER_CLICK",
            age_lower=("age_18" if targeting_mode == "autotarget"
                       else ("age_25" if (is_manual and not is_product) else "age_18")),
        )
        cid = client.create_master_campaign(spec, launch=launch)
        _res = {"name": disp_name, "ok": True, "id": cid, "launched": launch,
                "images": len(it_images), "videos": len(it_videos),
                "sitelinks": len(it_sitelinks),
                "url": f"https://direct.yandex.ru/wizard/campaigns/{cid}/?ulogin={login}"}
        if it_warnings:
            _res["warnings"] = it_warnings
        results.append(_res)
        _bump_job(_job, True)
    except Exception as e:  # noqa: BLE001 — ошибку по кампании показываем, набор не валим
        results.append({"name": disp_name, "ok": False, "error": str(e)[:240]})
        _add_job_err(_job, str(e)[:240])
        _bump_job(_job, False)
    _bump_item(_job)                                 # item завершён (tp6/tp7 fall-through)
    if _job:
        _job_db_progress(_job)
    return results, _tp7_mf


def api_create_set():
    """Создание набора кампаний через UAC-движок. Только переданные items.
    ⛔ ПРАВИЛО: ВСЕ кампании создаются ТОЛЬКО ЧЕРНОВИКАМИ (launch принудительно False для всех типов,
    включая UAC tp6/tp7). Сервис НИКОГДА не публикует автоматически — публикация = ручной шаг в
    Директе после проверки. Кнопка «Создать и опубликовать» отличается лишь источником контента
    (M3/ИИ поитемно, stream_content), но РК всё равно DRAFT.
    content_source='slepok_library' → контент из direct_slepok_content (БД-слепок) вместо M3/ИИ."""
    body = request.json or {}
    from .create_set_input import normalize_create_set_input
    _input = normalize_create_set_input(
        body,
        normalize_callout_text=_normalize_callout_text,
        callout_semantic_key=_callout_semantic_key,
        parse_number=_num,
    )
    login = _input["login"]
    items = _input["items"]
    agent = _input["agent"]            # ключ слепка — для привязки нативных интересов
    content_source = _input["content_source"]  # "slepok_library" → БД-контент без M3
    callouts = _input["callouts"]
    # ⛔ ПРАВИЛО: сервис создаёт ВСЕ кампании ТОЛЬКО ЧЕРНОВИКАМИ (никогда не публикует
    # автоматически — публикация = ручной шаг в Директе после проверки). launch принудительно
    # False для ВСЕХ типов, включая UAC tp6/tp7 (раньше они уходили на показы при «Создать и
    # опубликовать»). ЕПК tp1–tp5 и так всегда DRAFT. body.get("launch") игнорируется намеренно.
    launch = False
    counter_id = _input["counter_id"]
    goal_id = _input["goal_id"]
    cpa = _input["cpa"]
    # Галочка «под стиль сайта» (по умолчанию ВКЛ). Снята → no_cpa=True: НЕ создаём CPA-кампании
    # (оплата за конверсии): tp1/tp3/tp5 — только cpc-вариант пары; tp2/tp4 pay=cpa — пропускаем.
    no_cpa = _input["no_cpa"]
    # Галочка «загружать кампании по одному фиду»: вместо фан-аута по ВСЕМ фидам аккаунта —
    # только ПЕРВЫЙ фид. Для tp1/tp3/tp5 режем список фидов ниже; для tp7 (раскрыт по фидам ещё
    # в плане) — оставляем item'ы только первого встреченного feed_id.
    single_feed = _input["single_feed"]
    # via_cookie: принудительный cookie-first режим для всего набора.
    # Если via_cookie=False, token-типы всё равно идут API-first и АВТОМАТИЧЕСКИ переключаются
    # на cookie-path при error 152 (баллы Директа закончились).
    via_cookie = _input["via_cookie"]
    # stream_content: путь «Создать и опубликовать» БЕЗ предпросмотра — ИИ-контент М3 генерим
    # ПОИТЕМНО прямо здесь, перед созданием каждой РК (контент 1 РК → создаём 1 РК → следующая),
    # а НЕ всю пачку заранее во фронте. Прогресс виден сразу, при 152/сбое уже созданные сохранены.
    stream_content = _input["stream_content"]
    _stream_agent = None
    if stream_content:
        try:
            from . import ai_agents as _A
            _stream_agent = _A.get_agent(body.get("agent") or "")
        except Exception:  # noqa: BLE001
            _stream_agent = None
    if not login or not items:
        return jsonify({"error": "login и items обязательны"}), 400
    from .create_set_metrika import prepare_metrika
    _metrika = prepare_metrika(
        login=login,
        counter_id=counter_id,
        goal_id=goal_id,
        via_cookie=via_cookie,
        no_cpa=no_cpa,
        metrika_goals_for=_metrika_goals_for,
        goal_vse_formy=_goal_vse_formy,
        counter_foreign_owner=_counter_foreign_owner,
    )
    counter_id = _metrika.get("counter_id") or 0
    goal_id = _metrika.get("goal_id") or 0
    metrika_note = _metrika.get("metrika_note")
    metrika_optional = bool(_metrika.get("metrika_optional"))
    precreate_report = None
    precreated_promo_id = None
    precreated_promo_note = None
    precreated_promo_skipped = []
    precreated_callout_ids = []
    precreated_callouts_note = None
    if not _metrika.get("ok"):
        return jsonify({"error": _metrika.get("error")}), int(_metrika.get("status") or 400)

    # Воркер-путь (есть _job_id): конкуренцию контролирует ПУЛ с лимитом по агентству
    # воркеров — глобальный _PULL_LOCK тут НЕ берём (иначе 2-й параллельный аккаунт получил бы 429).
    # Ручной/синхронный путь (без _job_id) — как раньше, под глобальным локом.
    _worker_path = bool(body.get("_job_id"))
    if not _worker_path:
        ok, reason, wait = _pull_begin(f"createset:{login}", 10.0)
        if not ok:
            return _busy_response(reason, wait)
    try:
        from .create_set_account import prepare_create_set_account, validate_create_set_content
        _account = prepare_create_set_account(
            login=login,
            body=body,
            account_ctx=_account_ctx,
            templates_for=_templates_for,
            parse_region_ids=_ints,
        )
        if not _account.get("ok"):
            return jsonify({"error": _account.get("error")}), int(_account.get("status") or 400)
        ctx = _account["ctx"]
        eff_site = _account["site_type"]   # ручной override типа сайта
        tpl_titles = _account["tpl_titles"]
        tpl_texts = _account["tpl_texts"]
        tpl_sitelinks = _account["tpl_sitelinks"]
        # content_source=slepok_library → подставляем контент из direct_slepok_content (БД-слепок).
        # Если записи нет → честный фолбэк на шаблоны по типу сайта + пометка в ответе.
        try:
            from . import ai_agents as _A
            _unify_utp = _A.unify_utp_numbers
        except Exception:  # noqa: BLE001
            _unify_utp = None
        from .create_set_slepok_content import apply_slepok_campaign_content
        slepok_content_note = apply_slepok_campaign_content(
            items=items,
            content_source=content_source,
            agent=agent,
            site_type=eff_site,
            slepok_content_get=_slepok_content_get,
            rotate_window=_rotated_content_window,
            unify_utp_numbers=_unify_utp,
        )
        # Контент берём из item (сгенерированный ИИ-агентом/из слепка/отредактированный), иначе —
        # шаблоны по типу сайта. Хард-фейл только если нет НИ ИИ-контента, НИ шаблонов.
        _content_check = validate_create_set_content(
            items=items,
            tpl_titles=tpl_titles,
            tpl_texts=tpl_texts,
            site_type=eff_site,
        )
        if not _content_check.get("ok"):
            return jsonify({"error": _content_check.get("error")}), int(_content_check.get("status") or 400)
        href = _account["href"]
        region_ids = _account["region_ids"]

        # ПРЕДПОЛЁТ: ДО создания РК проверяем «тот ли токен/куку используем» — лёгкие read-only
        # вызовы с таймаутами. Битые/протухшие креды → быстрый явный отказ (а не тихий висяк на
        # пути создания). Кука обязательна только если в наборе есть grid/UAC-типы (tp5/tp6/tp7).
        _need_cookie = any((it.get("type") or "") not in _TOKEN_ONLY_TYPES for it in items)
        _pf = _preflight_creds(login, body.get("agency") or ctx["agency"] or "", _need_cookie)
        if not _pf["ok"]:
            return jsonify({"error": f"предполётная проверка кредов: {_pf['error']}"}), 502
        _st_token, _w_agency = _pf["token"], _pf["agency"]
        # UAC-клиент на куке ТОГО ЖЕ агентства (предполёт уже подтвердил, что кука жива).
        try:
            client = cmc.build_client(login, account=(_w_agency or None))
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"не удалось подобрать рабочую куку: {str(e)[:160]}"}), 502

        # Корректировки ставок из «Глобальных правил» по городу аккаунта (с фолбэком на глобальные '*').
        # Применяются ТОЛЬКО к tp1–tp5 (поисковые семейства). МК(tp6)/Товарка(tp7) — без корректировок.
        corr = _load_corrections(ctx.get("city") or "*")
        ret_map = _account_retargeting(_st_token, login) if _st_token else {}

        # CPA/бюджет из «Глобальных правил» (для tp1 — cpc_cpa целевой CPA):
        rs = _rule_sets(eff_site, ctx.get("city") or "*")
        # r_code и oblast — для правильного кодер-имени групп tp1
        r_code_ctx, _ = _resolve_region(ctx.get("city"))

        results = []
        _tp7_mf = None                                   # ленивый кэш фидов с коллекциями (tp7 фильтр)
        _job = _CREATE_JOBS.get(body.get("_job_id")) if body.get("_job_id") else None
        _units_block = False                             # сработал лимит баллов Директа (error 152)
        _units_pending = 0                               # сколько пунктов плана НЕ создано из-за лимита
        _units_from = None                               # индекс ПЕРВОГО несозданного пункта (для остатка/докрутки)
        _units_seen = False                              # 152 встречался хоть раз (даже если inline-cookie спас пункт)
        _units_switched = False                          # на 152 ВЕСЬ остаток переведён на куки-путь (бесшовно)
        _scan_i = 0                                      # курсор скана results на маркер 152 (учёт continue-веток)
        # RESUME-SKIP (по требованию): ОДИН раз bulk-читаем имена кампаний аккаунта (Grid, без баллов)
        # и дальше пропускаем пункты, чья кампания УЖЕ существует в Директе. Так «Продолжить» докручивает
        # только недостающее (вкл. ранее упавшие — их в Директе нет → создадутся), а не гонит набор с нуля.
        # НЕ пер-item API: одна выборка имён. Имя пункта (it["name"]) строится тем же кодером, что и при
        # создании; fan-out по фиду добавляет ' — {feed}' → считаем существующим и его.
        _existing_names: set = set()
        try:
            _existing_names = {(c.get("name") or "").strip()
                               for c in _grid_list_campaigns(login) if c.get("name")}
        except Exception:  # noqa: BLE001 — нет куки/Grid
            pass
        if _st_token and not _existing_names:
            # Фолбэк на v5 когда Grid недоступен (протухла кука): v5 не видит черновики/UAC/DRAFT,
            # но лучше частичного set, чем пустого (пустой = дубли всего при докрутке).
            try:
                _v5r = _v5_get("campaigns", _st_token, login, ["Name"], criteria={})
                _existing_names = {(c.get("Name") or "").strip()
                                   for c in (_v5r.get("result") or {}).get("Campaigns", [])
                                   if c.get("Name")}
            except Exception:  # noqa: BLE001
                pass
        if not _existing_names and body.get("_repair_force_names"):
            # В контексте «Продолжить» пустой _existing_names = Grid и v5 недоступны.
            # already_in_direct вернёт False для всех пунктов → весь набор пересоздаётся
            # → гарантированные дубли. АБОРТ обязателен — НЕ продолжаем создание.
            return jsonify({
                "error": ("[resume-abort] _existing_names пуст (Grid+v5 недоступны) — "
                          "докрутка остановлена во избежание дублей. "
                          "Проверьте сессию и куки аккаунта."),
                "abort_reason": "empty_existing_names_in_resume",
            }), 503
        from .create_set_precreate import run_create_set_precreate
        _precreated = run_create_set_precreate(
            login=login,
            body=body,
            items=items,
            account=ctx,
            agent=agent,
            site_type=eff_site,
            callouts=callouts,
            stream_content=stream_content,
            existing_names_count=len(_existing_names),
            token=_st_token,
            client=client,
            dedup_callouts=_dedup_callouts,
            callout_cap=_CALLOUT_PER_CAMPAIGN_CAP,
            grid_client_factory=gf.GridClient,
            v5_get=_v5_get,
            promo_usable_for_content=_promo_usable_for_content,
            create_account_promo_from_slepok=_create_account_promo_from_slepok,
            selected_slepok_key=_selected_slepok_key,
        )
        precreate_report = _precreated.get("report")
        precreated_promo_id = _precreated.get("promo_id")
        precreated_promo_note = _precreated.get("promo_note")
        precreated_promo_skipped = _precreated.get("promo_skipped") or []
        precreated_callout_ids = _precreated.get("callout_ids") or []
        precreated_callouts_note = _precreated.get("callouts_note")
        from .create_set_resume import already_in_direct, force_recreate, items_for_result_names
        _repair_force_names = {str(n).strip() for n in (body.get("_repair_force_names") or [])
                               if str(n or "").strip()}

        _content_executor = None
        _content_futures: dict[int, object] = {}
        _content_futures_by_key: dict[tuple, object] = {}
        _generated_content_by_key: dict[tuple, dict] = {}

        def _stream_content_item(src: dict) -> dict:
            return dict(src or {})

        def _prefetch_content(idx: int) -> None:
            if not (stream_content and _stream_agent and 0 <= idx < len(items)):
                return
            src = items[idx]
            if src.get("titles") and src.get("texts") and src.get("sitelinks"):
                return
            if idx in _content_futures:
                return
            _ckey = _content_cache_key((agent or "").strip().lower(), eff_site, ctx.get("city") or "", src)
            with _CONTENT_CACHE_LOCK:
                _cached_ready = _CONTENT_CACHE.get(_ckey)
            if _cached_ready:
                return
            if _ckey in _content_futures_by_key:
                _content_futures[idx] = _content_futures_by_key[_ckey]
                return
            nonlocal _content_executor
            if _content_executor is None:
                from concurrent.futures import ThreadPoolExecutor
                _content_executor = ThreadPoolExecutor(max_workers=2)
            fut = _content_executor.submit(
                _cached_campaign_content,
                login,
                _stream_agent,
                (agent or "").strip().lower(),
                _stream_content_item(src),
                eff_site,
                ctx.get("city") or "",
                [],
                True,
            )
            _content_futures[idx] = fut
            _content_futures_by_key[_ckey] = fut

        def _take_prefetched_content(idx: int, src: dict) -> dict | None:
            if not (stream_content and _stream_agent):
                return None
            fut = _content_futures.pop(idx, None)
            if fut is not None:
                try:
                    return fut.result()
                except Exception:  # noqa: BLE001
                    return None
            return _cached_campaign_content(
                login,
                _stream_agent,
                (agent or "").strip().lower(),
                _stream_content_item(src),
                eff_site,
                ctx.get("city") or "",
                [],
                True,
            )

        for _pref_i in range(min(3, len(items))):
            _prefetch_content(_pref_i)

        for _ci, it in enumerate(items):
            _prefetch_content(_ci + 1)
            _prefetch_content(_ci + 2)
            # Исчерпание суточного лимита баллов Директа (error 152): при ПЕРВОМ же маркере 152
            # БЕСШОВНО переводим ВЕСЬ остаток набора на куки-путь (Grid/UAC, без баллов) — НЕ рвём
            # цикл и НЕ отправляем массово в deferred-до-полуночи (это и было «висит без движения»).
            # Куки-типы (tp1–tp7) создаются без баллов. Пункт, на котором сорвало, мог восстановиться
            # inline-cookie (тогда он ok); если нет — остаётся failed, дубля не будет (повторный набор
            # пропустит уже созданные через set_plan). Это убирает требование ручного попапа-согласия
            # для системной/фоновой докрутки: 152 = автоматический переход на куки.
            while _scan_i < len(results):
                _r = results[_scan_i]; _scan_i += 1
                if _units_in_result(_r):
                    _units_seen = True
                    if not _r.get("ok"):
                        _units_block = True
            if _units_seen and not via_cookie:
                via_cookie = True            # с этого пункта и до конца набора — только по куке (без баллов)
                _units_switched = True
                _units_block = False         # 152 больше НЕ блокирующий стоп: продолжаем по куке, не break
            if _job and _job.get("cancel"):              # отмена: стоп ПОСЛЕ текущей (не рвём кампанию на полпути)
                break
            # Примечание: явные CPA-пункты (pay=cpa: tp2/tp4/tp6/tp7) гейтит ПРЕВЬЮ (галочка «под стиль
            # сайта» снимает их отметки → во фронт не уходят), поэтому здесь их НЕ пропускаем — уважаем
            # ручной выбор пользователя. no_cpa тут гасит только cpa-половину пар-движков tp1/tp3/tp5
            # (у них отдельной строки в превью нет).
            if _job:                                     # done = обработано ПУНКТОВ плана; created/failed —
                _job["done"] = _ci                       # по ФАКТУ каждой созданной кампании (fan-out даёт
                _job_db_progress(_job)                   # N кампаний на 1 пункт), бампается ниже _bump_job().
            name = it.get("name") or ""
            # RESUME-SKIP: кампания пункта УЖЕ есть в Директе → не пересоздаём (и не тратим M3-генерацию).
            # Пропускаем БЫСТРО (heartbeat тикает в _bump_item → watchdog не считает джобу зависшей).
            # ⚠ tp1_rsy — МУЛЬТИ-ФИД fan-out ({name} — {feed1}, {name} — {feed2}): item-level prefix-skip
            # пропустил бы ВЕСЬ пункт, если создана ХОТЬ ОДНА фид-кампания → недосозданные фиды потерялись
            # бы. Для tp1_rsy item-skip НЕ делаем — внутри его цикла skip ПОФИДОВО (по полному имени nm).
            if (it.get("type") != "tp1_rsy"
                    and already_in_direct(name, _existing_names)
                    and not force_recreate(name, _repair_force_names)):
                results.append({"ok": True, "name": name, "skipped": True,
                                "note": "уже создана в Директе — пропущена при докрутке"})
                _bump_item(_job)
                if _job:
                    _job_db_progress(_job)
                continue
            _it_ckey = _content_cache_key((agent or "").strip().lower(), eff_site, ctx.get("city") or "", it)

            # Для парных кампаний и дублей с тем же st/ct/brand в рамках ОДНОГО набора не
            # генерируем заново: вторая кампания получает ТОЧНО тот же готовый контентный набор.
            if not (it.get("titles") and it.get("texts") and it.get("sitelinks")):
                _prev_content = _generated_content_by_key.get(_it_ckey)
                if _prev_content:
                    if _prev_content.get("titles") and not it.get("titles"):
                        it["titles"] = list(_prev_content["titles"])
                    if _prev_content.get("texts") and not it.get("texts"):
                        it["texts"] = list(_prev_content["texts"])
                    if _prev_content.get("sitelinks") and not it.get("sitelinks"):
                        it["sitelinks"] = _content_copy({"sitelinks": _prev_content["sitelinks"]}).get("sitelinks", [])
                    if _prev_content.get("title2") and not it.get("title2"):
                        it["title2"] = _prev_content["title2"]

            # ПОТОКОВАЯ ГЕНЕРАЦИЯ (publish без предпросмотра): контент М3 для ЭТОЙ РК — прямо
            # перед её созданием (1 РК: сгенерили → создаём → следующая). Если контент уже есть
            # в item (ручной путь с предпросмотром) — не трогаем. Сбой генерации → создатель сам
            # упадёт на слепок/шаблоны (фолбэк), набор не валим.
            if stream_content and _stream_agent and not (it.get("titles") and it.get("texts") and it.get("sitelinks")):
                if _job:
                    _job["step"] = "generating"          # UI: «генерирую контент…»
                try:
                    _c = _take_prefetched_content(_ci, it) or {}
                    if _c.get("titles") and not it.get("titles"):
                        it["titles"] = _c["titles"]
                    if _c.get("texts") and not it.get("texts"):
                        it["texts"] = _c["texts"]
                    if _c.get("sitelinks") and not it.get("sitelinks"):
                        it["sitelinks"] = _c["sitelinks"]
                    if _c.get("title2") and not it.get("title2"):
                        it["title2"] = _c["title2"]
                except Exception:  # noqa: BLE001 — генерация не критична: фолбэк на слепок/шаблоны
                    pass
                if _job:
                    _job["step"] = "creating"            # UI: «создаю кампанию…»

            if it.get("titles") and it.get("texts") and it.get("sitelinks"):
                _generated_content_by_key[_it_ckey] = {
                    "titles": list(it.get("titles") or []),
                    "texts": list(it.get("texts") or []),
                    "sitelinks": _content_copy({"sitelinks": it.get("sitelinks") or []}).get("sitelinks", []),
                    "title2": it.get("title2") or "",
                }

            # ── tp1 РСЯ: ЕПК v501 mode=network_cpa с бренд-группами из пака M3 ──────
            if it.get("type") == "tp1_rsy":
                from .create_set_tp1 import run_create_set_tp1
                results.extend(run_create_set_tp1(
                    it=it, name=name,
                    login=login, slepok=agent, site_type=eff_site, w_agency=(_w_agency or ""),
                    city=(ctx.get("city") or ""), r_code=r_code_ctx, href=href, region_ids=region_ids,
                    counter_id=counter_id, goal_id=goal_id,
                    st_token=_st_token, via_cookie=via_cookie, no_cpa=no_cpa, single_feed=single_feed,
                    grid_cookie=_pf.get("cookie"),
                    tpl_titles=tpl_titles, tpl_texts=tpl_texts, rs=rs,
                    corr=corr, ret_map=ret_map, callouts=callouts, callout_ids=precreated_callout_ids,
                    existing_names=_existing_names, repair_force_names=_repair_force_names, job=_job,
                    lines=_lines, num=_num,
                    slepok_uses_shopping=_slepok_uses_shopping,
                    # tp1 множит товарку ТОЛЬКО по catalog-фидам (лендинги → пустой ListingAd → фейл).
                    # tp7/product зовут _account_model_feeds БЕЗ флага (все enabled-фиды) — см. 11939/11206.
                    account_model_feeds=(lambda _l, _a: _account_model_feeds(_l, _a, catalog_only=True)),
                    first_url_feed=_first_url_feed,
                    create_tp1_via_cookie=_create_tp1_via_cookie,
                    create_tp1_campaign=_create_tp1_campaign,
                    units_in_result=_units_in_result,
                    apply_corrections=_apply_corrections,
                    job_db_progress=_job_db_progress,
                    add_job_err=_add_job_err,
                    bump_job=_bump_job,
                    bump_item=_bump_item,
                ))
                continue

            # ── tp5 «Поиск + Товарная галерея»: комбинированная (TextAd+ListingAd+ShoppingAd, эталон Щербаковой) ──
            if it.get("type") in ("search_gallery", "rsya_gallery"):
                from .create_set_gallery import run_create_set_gallery
                results.extend(run_create_set_gallery(
                    kind=("tp5" if it.get("type") == "search_gallery" else "tp3"),
                    it=it, name=name,
                    login=login, slepok=agent, site_type=eff_site, w_agency=(_w_agency or ""),
                    city=(ctx.get("city") or ""), r_code=r_code_ctx, href=href, region_ids=region_ids,
                    counter_id=counter_id, goal_id=goal_id,
                    st_token=_st_token, via_cookie=via_cookie, no_cpa=no_cpa, single_feed=single_feed,
                    grid_cookie=_pf.get("cookie"),
                    tpl_titles=tpl_titles, tpl_texts=tpl_texts, rs=rs,
                    corr=corr, ret_map=ret_map, callouts=callouts, callout_ids=precreated_callout_ids,
                    job=_job,
                    lines=_lines, num=_num,
                    create_tp5_campaign=_create_tp5_campaign,
                    create_tp3_campaign=_create_tp3_campaign,
                    create_shopping_via_cookie=_create_shopping_via_cookie,
                    units_in_result=_units_in_result,
                    add_job_err=_add_job_err,
                    bump_job=_bump_job,
                    job_db_progress=_job_db_progress,
                    bump_item=_bump_item,
                ))
                continue

            # Текстовые кампании v5 TextCampaign: tp2 Поиск + tp4 Поиск+Динамика (тот же движок;
            # LIVE Кудерко: tp4 = TEXT_CAMPAIGN, Search=AVERAGE_CPA, Network=OFF — как tp2).
            _TEXT_ENGINE = {"search_test": ("tp2", "search"), "search_dynamic": ("tp4", "search")}
            if it.get("type") in _TEXT_ENGINE:
                # tp2/tp4 создаём ВСЕГДА по cookie/Grid (#1). Историческая v5/v501-ветка была за
                # `if True: … continue` (недостижима) — при выносе опущена как мёртвый код (см. git).
                from .create_set_text import run_create_set_text
                results.extend(run_create_set_text(
                    it=it, name=name, tp_code=_TEXT_ENGINE[it["type"]][0],
                    login=login, slepok=agent, site_type=eff_site, r_code=r_code_ctx,
                    city=(ctx.get("city") or ""), href=href, region_ids=region_ids,
                    counter_id=counter_id, goal_id=goal_id, st_token=_st_token,
                    tpl_titles=tpl_titles, tpl_texts=tpl_texts, rs=rs,
                    corr=corr, ret_map=ret_map, callouts=callouts, callout_ids=precreated_callout_ids,
                    precreated_promo_id=precreated_promo_id,
                    job=_job,
                    lines=_lines, num=_num,
                    create_text_via_cookie=_create_text_via_cookie,
                    slepok_minus_mode=_SLEPOK_MINUS_MODE,
                    apply_campaign_direct_minus=_apply_campaign_direct_minus,
                    get_or_create_minus_set=_get_or_create_minus_set,
                    attach_minus_set_to_text_campaign=_attach_minus_set_to_text_campaign,
                    add_job_err=_add_job_err,
                    bump_job=_bump_job,
                    job_db_progress=_job_db_progress,
                    bump_item=_bump_item,
                ))
                continue
            _prod_results, _tp7_mf = _run_master_product_item(
                it=it, name=name, href=href, region_ids=region_ids, counter_id=counter_id,
                goal_id=goal_id, cpa=cpa, launch=launch, client=client, agent=agent,
                eff_site=eff_site, ctx=ctx, tpl_titles=tpl_titles, tpl_texts=tpl_texts,
                tpl_sitelinks=tpl_sitelinks, rs=rs, login=login, _st_token=_st_token,
                _w_agency=_w_agency, _stream_agent=_stream_agent, _job=_job, _tp7_mf=_tp7_mf)
            results.extend(_prod_results)
        if _content_executor is not None:
            try:
                _content_executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                _content_executor.shutdown(wait=False)
        # 152-докрутка ИМЕНОВАННО (а не «хвостом»): после Goal-1 флипа цикл НЕ прерывается — все пункты
        # обрабатываются, несозданные из-за 152 РАЗБРОСАНЫ по results. Собираем их по ИМЕНИ — работает на
        # ЛЮБОМ пути, ВКЛЮЧАЯ via_cookie-резюм (keywords.add ещё стоит units → 152 возможен и тут; раньше
        # под `not via_cookie` остаток терялся молча → кампании пропадали). _units_block выводим из факта.
        from .create_set_units import count_created, count_failed, count_skipped_existing, units_failed_names
        _units_failed_names = units_failed_names(results)
        _units_block = bool(_units_failed_names)
        # skipped (RESUME-SKIP: уже есть в Директе) не считаем НОВО-созданными.
        created = count_created(results)
        skipped_existing = count_skipped_existing(results)
        # Провал по лимиту баллов (152) — это НЕ «ошибка кампании», а стоп: не считаем его в failed.
        # defer (M3-пак пуст/недоступен) — тоже НЕ failed: пункт уходит на отложенную докрутку.
        failed = count_failed(results)
        if _job:                                         # финал прогресса: ВСЕ items обработаны (цикл не рвётся)
            _job["done"] = len(items)
            _job["created"] = created
            _job["failed"] = failed
        # ОТЛОЖЕННАЯ ДОКРУТКА пунктов с пустым/недоступным M3-паком (defer): НЕ permanent-fail —
        # сохраняем их в deferred, демон докрутит по куке позже (когда пак/M3 восстановится). Дубля нет
        # (set_plan/RESUME-SKIP пропустит уже созданные). resume_at=now() → подхват в ближайший поллинг.
        _defer_names = {(r.get("name") or "") for r in results if r.get("defer")}
        if _defer_names:
            _defer_items = items_for_result_names(items, _defer_names)
            _rc_def = int(body.get("_resume_count") or 0)
            if _defer_items and _rc_def < _RESUME_MAX:
                _ddid = _deferred_save(login, (_w_agency or body.get("agency") or ""),
                                       body, _defer_items, body.get("_job_id"), resume_count=_rc_def)
                if _ddid:
                    _add_job_err(_job, f"M3-пак пуст у {len(_defer_items)} пунктов → отложено на докрутку ({_ddid})")
        # Промо: сначала берём пригодное из библиотеки аккаунта. Если промо нет (или все конфликтуют
        # с контентом), создаём одно промо по слепку/M3 в библиотеке клиента и сразу привязываем
        # к созданным кампаниям. Создание промо НЕ публикует кампании: РК остаются черновиками.
        from .create_set_promo import attach_or_create_promo
        promo_note = attach_or_create_promo(
            login=login,
            items=items,
            results=results,
            token=_st_token,
            client=client,
            account=ctx,
            site_type=eff_site,
            agent=agent,
            precreated_promo_id=precreated_promo_id,
            precreated_promo_note=precreated_promo_note,
            v5_get=_v5_get,
            promo_content_lines=_promo_content_lines,
            promo_usable_for_content=_promo_usable_for_content,
            create_account_promo_from_slepok=_create_account_promo_from_slepok,
            selected_slepok_key=_selected_slepok_key,
        )
        # «Уточнения» (callouts): обещаем подтверждение только если precreate реально дал id.
        # Live 2026-07-01: текущая Grid-схема не поддерживает AddCallouts, поэтому при новом
        # аккаунте precreate может безопасно вернуть пустой id-пул. В этом случае verifier должен
        # честно поставить warning/repair-кандидат, а не считать callouts подтверждёнными.
        from .create_set_callouts import build_callouts_note
        callouts_note = build_callouts_note(
            callouts=callouts,
            precreated_callout_ids=precreated_callout_ids,
            precreated_callouts_note=precreated_callouts_note,
        )
        from .create_set_postprocess import run_create_set_postprocess
        post = run_create_set_postprocess(
            login=login,
            items=items,
            results=results,
            body=body,
            agent=agent,
            counter_id=counter_id,
            goal_id=goal_id,
            site_type=eff_site,
            agency=(_w_agency or body.get("agency") or ""),
            promo_note=promo_note,
            callouts_note=callouts_note,
            callouts=callouts,
            live_verification=lambda _login, _results: _create_set_live_verification(
                _login,
                _results,
                agency=(_w_agency or body.get("agency") or ""),
                use_v5=False,
            ),
            repair_deps=_repair_deps,
            post_verify=_attach_post_repair_verification,
        )
        verification = post.get("verification")
        live_verification = post.get("live_verification")
        repair_gate_summary = post.get("repair_gate")
        auto_repair = post.get("auto_repair")
        # Лимит баллов Директа (152): человекочитаемое предупреждение + сколько НЕ создано.
        units_note = None
        deferred_id = None
        deferred_at = None
        if _units_block:
            # Остаток = ИМЕННО пункты, чей результат нёс 152 и НЕ создан (по имени, с fan-out-префиксом).
            _remaining = items_for_result_names(items, _units_failed_names)
            _pend = len(_remaining)
            _units_pending = _pend                       # для ответа units_pending
            _tail = (f"; не создано пунктов плана: {_pend}" if _pend else "")
            _rc = int(body.get("_resume_count") or 0)
            if _remaining and _rc < _RESUME_MAX:
                deferred_id = _deferred_save(login, (_w_agency or body.get("agency") or ""),
                                             body, _remaining, body.get("_job_id"), resume_count=_rc)
                if deferred_id:
                    deferred_at = _next_units_reset_utc().isoformat()
            if deferred_id:
                units_note = (f"⛔ Суточный лимит баллов Яндекс.Директа исчерпан (error 152). "
                              f"Создано кампаний: {created}{_tail}. Остаток ({len(_remaining)} пунктов) "
                              f"поставлен на АВТО-ДОКРУТКУ после сброса баллов (полночь МСК) — "
                              f"повторно кликать не нужно. Дублей не будет.")
            elif _remaining and _rc >= _RESUME_MAX:
                units_note = (f"⛔ Баллы Директа исчерпаны (error 152). Создано: {created}{_tail}. "
                              f"Достигнут лимит авто-докруток ({_RESUME_MAX}) — остаток не докручен "
                              f"автоматически. Запустите набор вручную после сброса баллов.")
            else:
                units_note = (f"⛔ Суточный лимит баллов Яндекс.Директа исчерпан (error 152). "
                              f"Создано кампаний: {created}{_tail}. Повторите запуск после сброса баллов "
                              f"(полночь МСК) — уже созданные пропустятся.")
        elif _units_switched and not units_note:
            # 152 случился в середине набора → остаток БЕСШОВНО создан по куке (без баллов).
            units_note = (f"Баллы Директа исчерпаны (error 152) во время набора — остаток автоматически "
                          f"создан по куке (Grid/UAC, без баллов). Создано кампаний: {created}.")
        # Если это была ДОКРУТКА осиротевшего остатка (resume по куке): помечаем родительскую строку
        # direct_deferred_creates терминально, чтобы рестарт не реанимировал её повторно (анти-цикл).
        _parent_did = body.get("_deferred_id")
        if _parent_did:
            try:
                if deferred_id:
                    _deferred_set_status(_parent_did, "done", f"остаток перенесён → {deferred_id}")
                else:
                    _deferred_set_status(_parent_did, "done",
                                         f"докручено по куке: создано {created}, не создано {failed}")
            except Exception:  # noqa: BLE001
                pass
        from .create_set_response import build_create_set_response
        return jsonify(build_create_set_response(
            created=created,
            failed=failed,
            launch=launch,
            results=results,
            promo_note=promo_note,
            callouts_note=callouts_note,
            units_block=_units_block,
            units_switched=_units_switched,
            units_note=units_note,
            units_pending=_units_pending,
            deferred_id=deferred_id,
            deferred_at=deferred_at,
            content_source=content_source,
            slepok_content_note=slepok_content_note,
            metrika_note=metrika_note,
            verification=verification,
            live_verification=live_verification,
            precreate_report=precreate_report,
            repair_gate_summary=repair_gate_summary,
            auto_repair=auto_repair,
        ))
    finally:
        if not _worker_path:
            _pull_end(f"createset:{login}")


def _create_set_live_verification(login: str, results: list, *, agency: str = "",
                                  use_v5: bool = False) -> dict:
    """Read-only live check for create_set results.

    Default path is Grid/cookie-only: this is intentional because Direct API
    units are scarce and UAC tp6/tp7 is not visible in v5 anyway.
    """
    def _token_getter(_login: str, _agency: str) -> str | None:
        token, _ag = _token_for_login(_login, _agency, _direct_tokens())
        return token

    return vsvc.verify_create_set_live(
        login,
        results or [],
        agency=agency,
        use_v5=use_v5,
        grid_campaigns_getter=_grid_list_campaigns,
        token_getter=_token_getter,
    )


def _create_set_job_context(jid: str) -> tuple[dict, dict, dict, tuple[dict, int] | None]:
    """Load terminal create_set job context from memory/DB for verification/repair endpoints."""
    jid = (jid or "").strip()
    if not jid:
        return {}, {}, {}, ({"error": "job_id обязателен"}, 400)
    with _CREATE_JOBS_LOCK:
        job = dict(_CREATE_JOBS.get(jid) or {})
    if not job:
        job = _job_db_get(jid) or {}
    if not job:
        return {}, {}, {}, ({"error": "job не найдена"}, 404)
    result = rgate.dict_from_jsonish(job.get("result"))
    if not isinstance(result, dict):
        return job, {}, {}, ({"error": "у job нет сохранённого результата для проверки",
                              "status": job.get("status")}, 422)
    ctx = rgate.normalize_job_context(job, result)
    return job, result, ctx, None


@bp.route("/api/create_set_async", methods=["POST"])
@_direct_access
def api_create_set_async():
    """Старт create_set в ФОНЕ — большой набор (сотни кампаний + fan-out по фидам) не упирается
    в nginx proxy_read_timeout (504 «<html>…» = причина прошлой ошибки). Возврат {job_id, total}
    сразу; прогресс «загружено X/Y» — через /api/create_set_status. Воркер гоняет ТОТ ЖЕ
    api_create_set (логика/правила/кодер/инварианты идентичны синхронному пути)."""
    body = dict(request.json or {})
    items = body.get("items") or []
    login = (body.get("login") or "").strip()
    if not items:
        return jsonify({"error": "items обязательны"}), 400
    # PRE-FLIGHT (ДО постановки в очередь и ДО генерации контента/трат M3): счётчик из формы должен
    # принадлежать ВЫБРАННОМУ аккаунту. Частый footgun — вставили счётчик/цель ОТ ДРУГОГО аккаунта →
    # Яндекс на campaigns.add отвечает «Указанная цель не найдена», а контент уже сгенерён зря.
    _cid_pf = _num(body.get("counter_id"), 0)
    if not _cid_pf:
        _mg_pf = _metrika_goals_for(login)
        if _mg_pf and _mg_pf["counters"]:
            _cid_pf = _mg_pf["counters"][0]
    _owner_pf = _counter_foreign_owner(_cid_pf, login) if _cid_pf else None
    if _owner_pf:
        return jsonify({"error": f"счётчик Метрики {_cid_pf} принадлежит аккаунту «{_owner_pf}», "
                                 f"а не «{login}» — Яндекс отклонит цель как «не найдена». "
                                 f"Укажите счётчик и цель самого «{login}»."}), 400
    # Разрешаем реальное агентство ДО постановки в очередь (без API-вызовов к Яндексу —
    # только кэш БД). Это гарантирует корректный ключ партиционирования в _job_agency():
    # две джобы на одном физическом агентстве не пойдут параллельно, даже если agency=""
    # (автоподбор) у одной и явное название — у другой.
    resolved_ag = _resolve_agency_hint(login, (body.get("agency") or "").strip())
    if resolved_ag:
        body["agency"] = resolved_ag                     # воркер увидит правильный ключ
    app = current_app._get_current_object()
    _ensure_create_worker(app)                           # глобальный serial-worker (поднимается 1 раз)
    # Дедуп (быстрый путь + понятное сообщение): если по логину уже есть активная джоба — не плодим.
    with _CREATE_JOBS_LOCK:
        for _jid, _j in _CREATE_JOBS.items():
            if _j.get("status") not in _JOB_TERMINAL and (_j.get("login") or "").strip() == login:
                return jsonify({
                    "job_id": _jid, "total": int(_j.get("total") or len(items)),
                    "login": login, "ahead": _create_jobs_ahead(_jid),
                    "existing": True,
                    "note": "для аккаунта уже есть активная задача; дубль не создан",
                })
    saved_session = dict(session)                        # переносим авторизацию в фоновый контекст
    # dedup_login=True — АТОМАРНЫЙ бэкстоп против гонки (два сабмита между pre-scan и вставкой):
    # проверка+вставка под одним локом внутри _job_new. На гонке вернётся СУЩЕСТВУЮЩИЙ job_id.
    with _CREATE_JOBS_LOCK:
        existing_ids = set(_CREATE_JOBS.keys())
    job_id = _job_new(len(items), login, body, saved_session, dedup_login=True)
    body["_job_id"] = job_id                             # create_set найдёт джобу для прогресса/отмены
    deduped = job_id in existing_ids                     # _job_new вернул уже существующую джобу (гонка)
    with _CREATE_JOBS_LOCK:
        ahead = _create_jobs_ahead(job_id)
    resp = {"job_id": job_id, "total": len(items), "login": login, "ahead": ahead}
    if deduped:
        resp["existing"] = True
        resp["note"] = "для аккаунта уже есть активная задача; дубль не создан"
    return jsonify(resp)


@bp.route("/api/create_set_status", methods=["GET"])
@_direct_access
def api_create_set_status():
    """Прогресс/результат async-джобы create_set. → {status, done, total, created, failed, result}."""
    jid = (request.args.get("job_id") or "").strip()
    _ensure_create_worker(current_app._get_current_object())  # гидрация/очистка stale running из БД
    _create_watchdog_tick()
    with _CREATE_JOBS_LOCK:
        j = _CREATE_JOBS.get(jid)
        if not j:
            return jsonify({"error": "job не найдена (возможно, устарела)"}), 404
        ahead = _create_jobs_ahead(jid) if j["status"] == "queued" else 0
        return jsonify({"status": j["status"], "login": j.get("login", ""),
                        "agency": _job_agency(j), "done": j["done"], "total": j["total"],
                        "created": j["created"], "failed": j["failed"],
                        "set_done": j.get("set_done", 0), "set_total": j.get("set_total", j["total"]),
                        "ahead": ahead, "error": j["error"], "elapsed": j.get("elapsed"),
                        "step": j.get("step"),  # текущая фаза: generating/creating (только при stream_content)
                        "stream_content": bool(j.get("stream_content")),
                        "result": j["result"] if j["status"] in _JOB_TERMINAL else None})


@bp.route("/api/create_set_verification", methods=["GET"])
@_direct_access
def api_create_set_verification():
    """Read-only verification report for a finished create_set job.

    Default is cheap: return stored/static verification and, when live=1, use
    Grid/cookie as the primary source because Direct API units are scarce. Add
    v5=1 only for a deeper paid-by-units check of regular tp1-tp5 campaigns.
    """
    jid = (request.args.get("job_id") or "").strip()
    live = request.args.get("live") in ("1", "true", "yes")
    use_v5 = request.args.get("v5") in ("1", "true", "yes")
    job, result, ctx, err = _create_set_job_context(jid)
    if err:
        return jsonify(err[0]), err[1]
    out = {
        "job_id": jid,
        "login": ctx.get("login") or "",
        "status": job.get("status"),
        "stored": result.get("verification"),
    }
    if not live:
        return jsonify(out)

    login = (ctx.get("login") or "").strip()
    if not login:
        return jsonify({**out, "live": {"status": "error", "error": "login не сохранён в job"}}), 422
    live_report = _create_set_live_verification(login, ctx.get("results") or [],
                                                agency=ctx.get("agency") or "", use_v5=use_v5)
    return jsonify({**out, "live": live_report})


def _repair_text_content_context(login: str, ctx: dict, action: dict) -> dict:
    """Build Grid content payload for in-place tp2/tp4 repair."""
    body = ctx.get("body") or {}
    item = action.get("item") if isinstance(action.get("item"), dict) else {}
    acc = _account_ctx(login)
    if not acc:
        raise RuntimeError(f"аккаунт {login} не найден в БД")
    domain = (acc.get("domain") or "").strip()
    if not domain:
        raise RuntimeError("у аккаунта нет домена в БД")
    href = "https://" + domain
    site_type = (body.get("site_type") or "").strip() or acc.get("site_type") or ""
    slepok = (body.get("agent") or "").strip()
    if not slepok:
        raise RuntimeError("в сохранённой job нет выбранного слепка")
    tpl_titles, tpl_texts, _tpl_sitelinks = _templates_for(site_type)
    titles = _lines(item.get("titles")) or tpl_titles
    texts = _lines(item.get("texts")) or tpl_texts
    tp_code = "tp4" if item.get("type") == "search_dynamic" else "tp2"
    r_code, _ = _resolve_region(acc.get("city"))
    body_region_ids = _ints(body.get("region_ids"))
    region_ids = body_region_ids if body_region_ids else [acc.get("geoid") or 225]
    groups, m3_alive = _pack_groups_with_retry(
        login,
        slepok,
        site_type,
        r_code,
        href,
        titles,
        texts,
        city=(acc.get("city") or ""),
        tp_code=tp_code,
        image_map={},
        autotarget=bool(item.get("autotarget")),
    )
    if not groups:
        raise RuntimeError(
            f"{tp_code} content-repair: пак M3 пуст/недоступен (M3_alive={m3_alive})"
        )
    goal_id = _num(body.get("goal_id"), 0)
    try:
        price_map = _account_offer_prices(login, href)
    except Exception:  # noqa: BLE001
        price_map = {}
    return {
        "groups": groups,
        "region_ids": region_ids,
        "href": href,
        "goal_id": goal_id,
        "autotargeting": bool(item.get("autotarget")),
        "price_map": price_map,
        "brand_price_fn": _group_ad_price,
    }


def _repair_shopping_content_context(login: str, ctx: dict, action: dict) -> dict:
    """Build Grid shopping payload for in-place tp3/tp5 repair."""
    body = ctx.get("body") or {}
    item = action.get("item") if isinstance(action.get("item"), dict) else {}
    acc = _account_ctx(login)
    if not acc:
        raise RuntimeError(f"аккаунт {login} не найден в БД")
    body_region_ids = _ints(body.get("region_ids"))
    region_ids = body_region_ids if body_region_ids else [acc.get("geoid") or 225]
    agency = (ctx.get("agency") or body.get("agency") or acc.get("agency") or "").strip()
    fid = _num(item.get("feed_id"), 0)
    if not fid:
        rows = _filter_allowed_feed_rows(_grid_feeds(login, agency))
        first = next((f for f in rows if f.get("id")), None)
        fid = int(first["id"]) if first else 0
    if not fid:
        raise RuntimeError("shopping content-repair: нет URL-фида")
    ct = (item.get("ct") or "ct0000").strip()
    r_code, _ = _resolve_region(acc.get("city"))
    group_name = _text_group_name(ct, r_code, "Товарная галерея") if r_code else "Товарная галерея"
    ct_model = kp.feeds_ct_model()
    ct_name = _ag_part1_map()
    raw_brand = ct_name.get(ct) or ct_model.get(ct) or ""
    seg = _ct_segment(ct)
    brand = _valid_pack_brand_name(ct, raw_brand) if raw_brand else ""
    is_brand_seg = seg in ("Марки", "Модели")
    vendor = _vendor_value(brand) if (brand and is_brand_seg) else None
    model_vals = _model_field_values(brand, seg) if (brand and is_brand_seg) else []
    listing_name = _listing_name_value(brand, seg) if (brand and is_brand_seg) else None
    tpl_titles, tpl_texts, _tpl_sitelinks = _templates_for(
        (body.get("site_type") or "").strip() or acc.get("site_type") or ""
    )
    texts = _lines(item.get("texts")) or tpl_texts
    body_text = (texts[0] if texts else "")[:81]
    return {
        "groups": [{
            "name": group_name,
            "vendor": vendor,
            "model": model_vals,
            "listing_name": listing_name,
        }],
        "feed_id": fid,
        "region_ids": region_ids,
        "body_text": body_text,
        "goal_id": _num(body.get("goal_id"), 0),
    }


def _repair_keywords_group_context(login: str, ctx: dict, meta: dict) -> dict:
    """Recompute the correct keyword phrases for ONE existing search adgroup during keyword-repair.

    Mirrors the create-time keyword derivation (_build_text_from_pack): M3 pack for the campaign's
    slepok/site_type/tp → positive phrases for this group's ct → project keyword guard
    (_filter_group_keywords: seg/brand/foreign-city/used-car). Empty result (M3 down or nothing to
    add) is safe — the executor then keeps the group's existing keywords and only fixes autotarget.
    """
    body = ctx.get("body") or {}
    acc = _account_ctx(login)
    if not acc:
        raise RuntimeError(f"аккаунт {login} не найден в БД")
    slepok = (body.get("agent") or "").strip()
    if not slepok:
        raise RuntimeError("в сохранённой job нет выбранного слепка")
    site_type = (body.get("site_type") or "").strip() or acc.get("site_type") or ""
    city = (acc.get("city") or "")
    ct = (meta.get("ct") or "ct0000").strip()
    tp_code = (meta.get("tp_code") or "tp2").strip()
    gather_key = _SLEPOK_KEY.get(slepok.lower(), slepok.lower())
    pack = kp.gather(gather_key, site_type, tp_code)
    pos = (pack.get(ct) or {}).get("positive") or []
    seg = _ct_segment(ct)
    ct_name = _ag_part1_map()
    ct_model = kp.feeds_ct_model()
    raw_brand = ct_name.get(ct) or ct_model.get(ct) or ct
    brand = _valid_pack_brand_name(ct, raw_brand) or "Авто"
    kws = _filter_group_keywords(pos, seg, brand, city, site_type)
    return {"keywords": kws, "seg": seg, "brand": brand}


def _attach_post_repair_verification(out: dict, login: str, ctx: dict) -> dict:
    """Attach a Grid-first live report after an in-place repair mutation."""
    try:
        post_live = _create_set_live_verification(
            login,
            ctx.get("results") or [],
            agency=ctx.get("agency") or "",
            use_v5=False,
        )
        out["post_repair_live_verification"] = post_live
        out["remaining_repair_plan"] = (post_live or {}).get("repair_plan")
    except Exception as e:  # noqa: BLE001
        out["post_repair_live_verification_error"] = str(e)[:220]
    return out


def _repair_deps() -> rex.RepairDeps:
    """Wire blueprint IO helpers into pure repair executors."""
    return rex.RepairDeps(
        account_ctx=_account_ctx,
        promo_content_lines=_promo_content_lines,
        create_account_promo_from_slepok=_create_account_promo_from_slepok,
        dedup_callouts=_dedup_callouts,
        text_content_context=_repair_text_content_context,
        shopping_content_context=_repair_shopping_content_context,
        callout_cap=_CALLOUT_PER_CAMPAIGN_CAP,
        group_keywords_context=_repair_keywords_group_context,
    )


def _delete_uac_repair_campaigns(login: str, agency: str, replacements: list[dict]) -> dict:
    """Delete specific incomplete UAC drafts before queued recreate."""
    rows = []
    failed = []
    if not replacements:
        return {"deleted": rows, "failed": failed}
    client = None
    for row in replacements:
        try:
            cid = int(row.get("campaign_id") or 0)
        except (TypeError, ValueError):
            cid = 0
        name = str(row.get("name") or "").strip()
        if cid <= 0 or not name.lower().startswith(("tp6_", "tp7_")) or not _is_tool_campaign(name):
            failed.append({"campaign_id": cid, "name": name, "error": "небезопасное имя UAC для replace"})
            continue
        try:
            if client is None:
                client = cmc.build_client(login, account=(agency or None))
                client.link_info("https://ya.ru")
            client.delete_campaign(str(cid))
            rows.append({"campaign_id": cid, "name": name, "issue_code": row.get("issue_code")})
        except Exception as e:  # noqa: BLE001
            failed.append({"campaign_id": cid, "name": name, "error": str(e)[:220]})
    return {"deleted": rows, "failed": failed}


def _delete_search_draft_campaigns(login: str, agency: str, delete_items: list[dict]) -> dict:
    """Delete specific ПОИСКОВЫЕ (non-UAC) campaigns before queued recreate.

    Безопасность: удаляем ТОЛЬКО если
    1) имя создано этим инструментом (_is_tool_campaign)
    2) кампания в статусе DRAFT по Grid (primaryStatus='DRAFT')
    Если хотя бы одна кампания NOT DRAFT → возвращаем blocked_non_draft, не удаляем ничего.
    """
    rows: list[dict] = []
    failed: list[dict] = []
    blocked: list[dict] = []
    if not delete_items:
        return {"deleted": rows, "failed": failed, "blocked_non_draft": blocked}
    try:
        draft_ids = {int(c["id"]) for c in _grid_list_campaigns(login, only_draft=True) if c.get("id")}
    except Exception as e:  # noqa: BLE001
        return {"deleted": [], "failed": [], "blocked_non_draft": [],
                "error": f"не удалось получить список черновиков Grid: {str(e)[:200]}"}
    ids_to_delete: list[dict] = []
    for row in delete_items:
        try:
            cid = int(row.get("campaign_id") or 0)
        except (TypeError, ValueError):
            cid = 0
        name = str(row.get("name") or "").strip()
        if cid <= 0 or not _is_tool_campaign(name):
            failed.append({"campaign_id": cid, "name": name, "error": "небезопасное имя для delete"})
            continue
        if cid not in draft_ids:
            blocked.append({"campaign_id": cid, "name": name,
                            "reason": "не DRAFT — авто-удаление заблокировано"})
            continue
        ids_to_delete.append({"campaign_id": cid, "name": name, "issue_code": row.get("issue_code")})
    if blocked:
        # Если хотя бы одна кампания не DRAFT — не удаляем ни одну (консервативно)
        return {"deleted": [], "failed": failed, "blocked_non_draft": blocked}
    if not ids_to_delete:
        return {"deleted": [], "failed": failed, "blocked_non_draft": []}
    # Ре-снимаем draft_ids непосредственно перед удалением: за время классификации выше
    # кампания могла быть опубликована (draft_ids → published; гонка). Удаляем только
    # пересечение с актуальным DRAFT-списком.
    try:
        draft_ids2 = {int(c["id"]) for c in _grid_list_campaigns(login, only_draft=True)
                      if c.get("id")}
    except Exception as _e2:  # noqa: BLE001
        return {"deleted": [], "failed": [], "blocked_non_draft": [],
                "error": f"повторная проверка DRAFT перед удалением упала: {str(_e2)[:200]}"}
    _still_draft: list[dict] = []
    for _r2 in ids_to_delete:
        if int(_r2["campaign_id"]) not in draft_ids2:
            blocked.append({"campaign_id": _r2["campaign_id"], "name": _r2["name"],
                            "reason": "при повторной проверке кампания не DRAFT — удаление заблокировано"})
        else:
            _still_draft.append(_r2)
    if blocked:
        return {"deleted": [], "failed": failed, "blocked_non_draft": blocked}
    ids_to_delete = _still_draft
    if not ids_to_delete:
        return {"deleted": [], "failed": failed, "blocked_non_draft": []}
    try:
        res = gc.GridCreateClient(login).delete_campaigns([r["campaign_id"] for r in ids_to_delete])
        deleted_ids = {int(i) for i in (res.get("deleted") or [])}
        for r in ids_to_delete:
            if int(r["campaign_id"]) in deleted_ids:
                rows.append(r)
            else:
                failed.append({**r, "error": "Grid не подтвердил удаление"})
    except Exception as e:  # noqa: BLE001
        failed.extend([{**r, "error": str(e)[:200]} for r in ids_to_delete])
    return {"deleted": rows, "failed": failed, "blocked_non_draft": []}


def _queue_recreate_repair_job(login: str, ctx: dict, plan: dict, *,
                               parent_job_id: str, saved_session: dict,
                               dedup_login: bool = True) -> dict:
    """Queue resume/recreate repair items without Direct API units."""
    def _create_job(total: int, job_login: str, body: dict, sess: dict, dedup: bool) -> str:
        return _job_new(total, job_login, body, sess, dedup_login=dedup)

    def _ahead(job_id: str) -> int:
        with _CREATE_JOBS_LOCK:
            return _create_jobs_ahead(job_id)

    return rauto.queue_recreate_repair_job(
        login,
        ctx,
        plan or {},
        parent_job_id=parent_job_id,
        saved_session=saved_session,
        delete_uac=_delete_uac_repair_campaigns,
        create_job=_create_job,
        jobs_ahead=_ahead,
        dedup_login=dedup_login,
        delete_search_draft=_delete_search_draft_campaigns,
    )


def _auto_queue_recreate_after_done(parent_job_id: str, job_snapshot: dict) -> dict | None:
    """Queue async recreate/UAC replace after the parent create job is terminal.

    Draft-only gate: если план содержит requires_campaign_delete (поисковые кампании
    с NO_KEYWORDS_LIVE/WRONG_AUTOTARGET), авто-recreate разрешается ТОЛЬКО когда все
    такие кампании находятся в статусе DRAFT по Grid. Это безопасно: все РК создаются
    с launch=False → всегда DRAFT; авто-удаление боевых кампаний заблокировано.
    """
    req = rauto.auto_recreate_request(parent_job_id, job_snapshot)
    if not req:
        return None

    # Если заблокировано из-за requires_campaign_delete — проверяем: все ли DRAFT?
    if req.get("queued") is False and req.get("requires_explicit_trigger"):
        result = job_snapshot.get("result") if isinstance(job_snapshot.get("result"), dict) else {}
        live = result.get("live_verification") if isinstance(result.get("live_verification"), dict) else {}
        plan = live.get("repair_plan") if isinstance(live.get("repair_plan"), dict) else {}
        delete_items = rgate.executable_recreate_delete_campaigns(plan)
        if delete_items:
            login_for_check = (job_snapshot.get("login") or result.get("login") or "").strip()
            try:
                draft_ids = {
                    int(c["id"])
                    for c in _grid_list_campaigns(login_for_check, only_draft=True)
                    if c.get("id")
                }
                all_draft = all(
                    int(it.get("campaign_id") or 0) in draft_ids for it in delete_items
                )
            except Exception:  # noqa: BLE001
                # Grid недоступен → оставляем блокировку (консервативно)
                return {**req, "draft_gate": "Grid недоступен для проверки статуса — заблокировано"}
            if not all_draft:
                return {**req, "draft_gate": "не все кампании в DRAFT — авто-удаление заблокировано"}
            # Все DRAFT → снимаем блокировку, добавив флаг в body snapshot
            modified_body = {**(job_snapshot.get("body") or {}), "_auto_recreate_with_delete": True}
            modified_snapshot = {**job_snapshot, "body": modified_body}
            req = rauto.auto_recreate_request(parent_job_id, modified_snapshot)
            if not req:
                return None

    if req.get("queued") is False:
        return req
    queued = _queue_recreate_repair_job(
        req["login"],
        req.get("ctx") or {},
        req.get("plan") or {},
        parent_job_id=req.get("parent_job_id") or parent_job_id,
        saved_session=req.get("saved_session") or {},
        dedup_login=True,
    )
    queued["source"] = req.get("source") or "auto_after_done"
    return queued


@bp.route("/api/create_set_repair", methods=["POST"])
@_direct_access
def api_create_set_repair():
    """Build or execute a scoped repair plan for a completed create_set job.

    Default is read-only. With execute=1, runs one scoped repair action type
    per call in cookie/Grid-first mode, without Direct API units by default.
    """
    body = request.get_json(silent=True) or {}
    jid = (body.get("job_id") or request.args.get("job_id") or "").strip()
    execute = rgate.truthy(body.get("execute")) or rgate.truthy(request.args.get("execute"))
    use_v5 = rgate.truthy(body.get("v5")) or rgate.truthy(request.args.get("v5"))
    job, result, ctx, err = _create_set_job_context(jid)
    if err:
        return jsonify(err[0]), err[1]
    login = (ctx.get("login") or "").strip()
    if not login:
        return jsonify({"error": "login не сохранён в job", "job_id": jid}), 422
    live_report = _create_set_live_verification(login, ctx.get("results") or [],
                                                agency=ctx.get("agency") or "", use_v5=use_v5)
    plan = (live_report or {}).get("repair_plan")
    if not plan:
        try:
            from .repair_planner import build_repair_plan
            plan = build_repair_plan(live_report)
        except Exception:  # noqa: BLE001
            plan = {"status": "error"}
    if execute:
        repair_deps = _repair_deps()
        with _CREATE_JOBS_LOCK:
            recreate_preflight = rauto.recreate_queue_preflight(
                login,
                ctx.get("body") or {},
                plan or {},
                _CREATE_JOBS,
                _JOB_TERMINAL,
            )
        items = recreate_preflight.get("items") or []
        unsupported = recreate_preflight.get("unsupported_actions") or []
        if items:
            if recreate_preflight.get("conflict"):
                return jsonify({
                    **recreate_preflight["conflict"],
                    "job_id": jid,
                }), 409
            app = current_app._get_current_object()
            _ensure_create_worker(app)
            saved_session = dict(session)
            queued = _queue_recreate_repair_job(
                login,
                ctx,
                plan or {},
                parent_job_id=jid,
                saved_session=saved_session,
                dedup_login=True,
            )
            if not queued.get("queued"):
                out, status = rauto.recreate_queue_failure_response(jid, login, queued, plan)
                return jsonify(out), status
            return jsonify(rauto.recreate_queue_success_response(
                jid,
                login,
                queued,
                plan,
                unsupported,
            ))

        in_place = rauto.execute_next_in_place(
            login,
            ctx,
            plan or {},
            repair_deps,
            job_id=jid,
            post_verify=_attach_post_repair_verification,
        )
        if in_place:
            out, status = in_place
            return jsonify(out), status

        if not items:
            return jsonify(rauto.no_safe_action_response(jid, plan, unsupported)), 422
    return jsonify(rgate.build_repair_gate_payload(
        job_id=jid, job=job, result=result, live_report=live_report, plan=plan,
    ))


@bp.route("/api/create_jobs", methods=["GET"])
@_direct_access
def api_create_jobs():
    """Живая очередь создания РК — СЕРВЕРНЫЙ источник правды (видна с любого устройства,
    переживает обновление страницы и рестарт сервиса). ?active=1 → только незавершённые.
    Возвращает все джобы (активные + недавние завершённые) из памяти, гидрированной из БД при старте."""
    active_only = request.args.get("active") in ("1", "true")
    _ensure_create_worker(current_app._get_current_object())  # гарантируем гидрацию из БД
    _create_watchdog_tick()
    _jobs_purge_old()                                        # историю не храним: чистим завершённые > TTL
    out = []
    with _CREATE_JOBS_LOCK:
        for jid, j in _CREATE_JOBS.items():
            if active_only and j["status"] in _JOB_TERMINAL:
                continue
            ahead = _create_jobs_ahead(jid) if j["status"] == "queued" else 0
            out.append({"job_id": jid, "status": j["status"], "login": j.get("login", ""),
                        "agency": _job_agency(j),
                        "done": j.get("done", 0), "total": j.get("total", 0),
                        "created": j.get("created", 0), "failed": j.get("failed", 0),
                        "set_done": j.get("set_done", 0), "set_total": j.get("set_total", j.get("total", 0)),
                        "kind": j.get("kind", "set"), "publish": bool(j.get("publish")),
                        "ahead": ahead, "error": j.get("error"), "elapsed": j.get("elapsed"),
                        "step": j.get("step"),
                        "stream_content": bool(j.get("stream_content")),
                        "result": j.get("result") if j["status"] in _JOB_TERMINAL else None})
    # активные — первыми, затем по «свежести» (running/queued выше)
    order = {"running": 0, "queued": 1}
    out.sort(key=lambda x: order.get(x["status"], 2))
    return jsonify({"jobs": out})


@bp.route("/api/create_set_cancel", methods=["POST"])
@_direct_access
def api_create_set_cancel():
    """Отмена джобы создания. Если ждёт в очереди — снимется до старта; если выполняется —
    остановится ПОСЛЕ текущей кампании (не рвёт кампанию на полпути → нет битых черновиков)."""
    jid = ((request.json or {}).get("job_id") or "").strip()
    with _CREATE_JOBS_LOCK:
        j = _CREATE_JOBS.get(jid)
        if not j:
            return jsonify({"error": "job не найдена"}), 404
        if j["status"] in _JOB_TERMINAL:
            # Уже завершена → «отмена» = УБРАТЬ КАРТОЧКУ СЕЙЧАС (без ожидания TTL 2 мин).
            _CREATE_JOBS.pop(jid, None)
            _JOB_DB_LAST.pop(jid, None)
            _job_db_delete(jid)
            return jsonify({"ok": True, "status": j["status"], "removed": True, "note": "убрана из очереди"})
        j["cancel"] = True
        if j["status"] == "queued" and jid in _CREATE_QUEUE:
            _CREATE_QUEUE.remove(jid)                     # из очереди — сразу
            j["status"] = "cancelled"
        snap = dict(j)
    _job_db_save(jid, snap, full=True)
    return jsonify({"ok": True, "status": snap["status"]})


@bp.route("/api/jobs/<job_id>/resume", methods=["POST"])
@_direct_access
def api_job_resume(job_id: str):
    """Возобновление джобы, прерванной рестартом сервиса (статус 'interrupted').
    Создаёт новую джобу с тем же body — set_plan внутри create_set пропустит уже созданные РК.
    Возвращает {ok, new_job_id, total, login, ahead}."""
    jid = job_id.strip()
    with _CREATE_JOBS_LOCK:
        j = _CREATE_JOBS.get(jid)
        if not j:
            return jsonify({"error": "job не найдена (возможно, уже убрана)"}), 404
        if j.get("status") != "interrupted":
            return jsonify({"error": f"job не прервана (статус: {j.get('status')})"}), 400
        body = j.get("body")
        login = j.get("login") or ""
        _done_idx = int(j.get("done") or 0)   # индекс последнего начатого пункта (0-based)
    if not body:
        return jsonify({"error": "body джобы не сохранён — невозможно возобновить автоматически. "
                                 "Запустите создание вручную через форму."}), 422
    if not body.get("items"):
        return jsonify({"error": "items в body пусты — нечего создавать"}), 422
    app = current_app._get_current_object()
    _ensure_create_worker(app)
    # Системная сессия: прерванная джоба была авторизована оригинальным пользователем,
    # resume запускает тот же авторизованный пользователь → используем его текущую сессию.
    saved_session = dict(session)
    # Сбрасываем _job_id из старого body (будет переназначен _job_new) и флаги фазы.
    new_body = dict(body)
    new_body.pop("_job_id", None)
    new_body.pop("_resume_count", None)
    # Последняя частичная кампания (индекс done): могла создаться неполностью до прерывания.
    # Помечаем её в _repair_force_names → force_recreate перебьёт already_in_direct,
    # кампания будет пересоздана даже если её след остался в Grid.
    _items_list = body.get("items") or []
    # single_feed: normalize_create_set_input фильтрует items до первого feed_id
    # (first_feed_items). _done_idx при выполнении джобы индексировал ОТФИЛЬТРОВАННЫЙ
    # список → нормализуем здесь тем же способом, иначе _done_idx укажет на неверный item.
    if body.get("single_feed") and _items_list:
        from .create_set_input import first_feed_items
        _items_list = first_feed_items(_items_list, parse_number=_num)
    if 0 <= _done_idx < len(_items_list):
        _partial_name = (_items_list[_done_idx].get("name") or "").strip()
        if _partial_name:
            _force = [str(n).strip() for n in (new_body.get("_repair_force_names") or [])
                      if str(n or "").strip()]
            if _partial_name not in _force:
                _force.append(_partial_name)
            new_body["_repair_force_names"] = _force
    new_job_id = _job_new(len(new_body["items"]), login, new_body, saved_session)
    with _CREATE_JOBS_LOCK:
        ahead = _create_jobs_ahead(new_job_id)
    return jsonify({"ok": True, "new_job_id": new_job_id,
                    "total": len(new_body["items"]), "login": login, "ahead": ahead})


@bp.route("/api/jobs/<job_id>/delete_created", methods=["POST"])
@_direct_danger
def api_job_delete_created(job_id: str):
    """Удалить все черновики аккаунта из прерванной джобы (статус 'interrupted').
    Использует тот же _delete_drafts_core что и кнопка «Удалить черновики» —
    удаляет только кампании созданные этим модулем (_is_tool_campaign), чужие не трогает.
    Возвращает {ok, deleted, errors}."""
    jid = job_id.strip()
    with _CREATE_JOBS_LOCK:
        j = _CREATE_JOBS.get(jid)
        if not j:
            return jsonify({"error": "job не найдена (возможно, уже убрана)"}), 404
        login = j.get("login") or ""
        agency = j.get("agency") or ""
        status = j.get("status") or ""
    if not login:
        return jsonify({"error": "login не сохранён в джобе"}), 422
    if status not in ("interrupted", "error", "done", "cancelled"):
        return jsonify({"error": f"Удаление доступно только для завершённых/прерванных джоб (статус: {status})"}), 400
    try:
        result = _delete_drafts_core(login, agency)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)[:300]}), 500
    return jsonify({"ok": result.get("ok", True),
                    "deleted": result.get("deleted", 0),
                    "by_v5": result.get("by_v5", 0),
                    "by_uac": result.get("by_uac", 0),
                    "by_cookie": result.get("by_cookie", 0),
                    "errors": result.get("errors") or []})


def _lines(val) -> list[str]:
    """textarea → список непустых строк (или уже список)."""
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    return [ln.strip() for ln in (val or "").splitlines() if ln.strip()]


def _ints(val) -> list[int]:
    if isinstance(val, list):
        return [int(x) for x in val if str(x).strip()]
    return [int(x) for x in (val or "").replace(",", " ").split() if x.strip().isdigit()]


@bp.route("/api/create", methods=["POST"])
@_direct_access
def api_create():
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
            minus_keywords=_lines(d.get("minus_keywords")) or ["отзывы"],
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
_M3_LLM_URL = os.environ.get("M3_LLM_URL", "http://127.0.0.1:8082").rstrip("/")
_M3_LLM_TIMEOUT = float(os.environ.get("M3_LLM_TIMEOUT", "480"))   # 72B медленнее 14B (под nginx 500с)
# Fan-out: 4×14B генераторы на отдельных портах + 1×72B валидатор.
# Env M3_LLM_URLS_14B — comma-sep URLs; иначе дефолт 8082–8085 (те же, что туннель).
_M3_LLM_URLS_14B: list = [
    s.strip().rstrip("/") for s in os.environ.get("M3_LLM_URLS_14B", "").split(",") if s.strip()
] or [f"http://127.0.0.1:{p}" for p in (8082, 8083, 8084, 8085)]
_M3_LLM_URL_72B: str = os.environ.get("M3_LLM_URL_72B", "http://127.0.0.1:8086").rstrip("/")
_M3_LLM_TIMEOUT_14B = float(os.environ.get("M3_LLM_TIMEOUT_14B", "120"))  # 14B быстрее
_M3_LLM_REPAIR_TIMEOUT = float(os.environ.get("M3_LLM_REPAIR_TIMEOUT", "35"))


@bp.route("/api/ai/status")
@_direct_access
def api_ai_status():
    """Жив ли локальный mlx-сервер на M3 + какая модель загружена."""
    import requests as _rqs
    try:
        r = _rqs.get(f"{_M3_LLM_URL}/v1/models", timeout=8)
        if r.status_code != 200:
            return jsonify({"online": False, "url": _M3_LLM_URL,
                            "error": f"HTTP {r.status_code}"}), 200
        data = r.json().get("data") or []
        # mlx отдаёт id как путь/HF-репо — показываем ЧИСТОЕ имя модели (basename).
        models = [(m.get("id") or "").rstrip("/").split("/")[-1] for m in data if m.get("id")]
        return jsonify({"online": True, "url": _M3_LLM_URL, "models": models})
    except Exception as e:  # noqa: BLE001
        return jsonify({"online": False, "url": _M3_LLM_URL, "error": str(e)[:200]}), 200


@bp.route("/api/ai/chat", methods=["POST"])
@_direct_access
def api_ai_chat():
    """Прокси к локальной ИИ (OpenAI /v1/chat/completions).
    Тело — ЛИБО чат-история {messages:[{role,content},...]}, ЛИБО {prompt, system?}.
    Доп.: {model?, max_tokens?, temperature?}. Ответ: {ok, text, usage} | {ok:false, error}."""
    import requests as _rqs
    d = request.json or {}
    messages = []
    raw = d.get("messages")
    if isinstance(raw, list) and raw:
        for m in raw[-40:]:                       # ограничиваем глубину истории
            role = m.get("role")
            content = (m.get("content") or "").strip()
            if role in ("system", "user", "assistant") and content:
                messages.append({"role": role, "content": content[:6000]})
    if not messages:                              # обратная совместимость: одиночный prompt
        prompt = (d.get("prompt") or "").strip()
        if not prompt:
            return jsonify({"ok": False, "error": "prompt или messages обязательны"}), 400
        system = (d.get("system") or "").strip()
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
    payload = {
        "messages": messages,
        "max_tokens": int(d.get("max_tokens") or 512),
        "temperature": float(d.get("temperature") if d.get("temperature") is not None else 0.7),
    }
    # «model» НЕ шлём по умолчанию: mlx_lm.server использует уже загруженную модель.
    # Левое имя (напр. "local") заставляет его грузить несуществующий репо с HF → 404.
    if d.get("model"):
        payload["model"] = d["model"]
    try:
        r = _rqs.post(f"{_M3_LLM_URL}/v1/chat/completions", json=payload, timeout=_M3_LLM_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"M3 недоступна: {str(e)[:200]}"}), 502
    if r.status_code != 200:
        return jsonify({"ok": False, "error": f"HTTP {r.status_code}: {r.text[:300]}"}), 502
    j = r.json()
    try:
        text = j["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return jsonify({"ok": False, "error": "пустой ответ модели"}), 502
    return jsonify({"ok": True, "text": text, "usage": j.get("usage")})


# ── ИИ-агенты «слепки директологов»: генерация/публикация промоакций ────────────
# Агент = стиль реального директолога. ИИ на M3 генерит промо в его стиле → превью →
# публикация в кабинет клиента через grid/api (promo.PromoClient). Публикация — только
# по явному подтверждению пользователя (создаёт реальную промо у клиента).

# Фингерпринты служебных ошибок, которые draft-модель/спекулятивный декодер на M3 иногда
# ВКЛЕИВАЕТ прямо в сгенерированный текст при обрыве соединения с под-сервисом (баг S559/S560:
# «Connection aborted / RemoteDisconnected» уезжает в content). Такой ответ — мусор: режем по
# первому фингерпринту, а если осмысленного префикса не осталось — считаем генерацию неудачной.
_M3_LEAK_MARKERS = (
    "M3 недоступна", "Connection aborted", "RemoteDisconnected", "Remote end closed",
    "ConnectionError", "Max retries exceeded", "Traceback (most recent call",
    "HTTPConnectionPool", "Failed to establish a new connection",
)


def _strip_error_leak(text: str) -> str:
    """Срезает текст по первому маркеру служебной ошибки (обрыв драфт-модели). → чистый префикс."""
    if not text:
        return text or ""
    low = text.lower()
    cut = len(text)
    for mk in _M3_LEAK_MARKERS:
        i = low.find(mk.lower())
        if i != -1 and i < cut:
            cut = i
    return text[:cut].rstrip(" \n\t·-—–:(") if cut < len(text) else text


def _has_error_leak(text: str) -> bool:
    low = (text or "").lower()
    return any(mk.lower() in low for mk in _M3_LEAK_MARKERS)


def _m3_complete(messages: list, max_tokens: int = 400, temperature: float = 0.8,
                 top_p: float | None = None, repetition_penalty: float | None = None,
                 tries: int = 3, backoff: float = 5.0, timeout: float | None = None) -> tuple[str | None, str | None]:
    """Один вызов M3 (mlx /v1/chat/completions). → (text, error). model не шлём.
    top_p/repetition_penalty — анти-зацикливание (14B на длинном промпте иначе уходит в повтор).
    tries/backoff — ретраи на ТРАНСПОРТНОМ обрыве и «утечке» служебной ошибки в текст: mlx на M3
    под нехваткой RAM (speculative decoding) падает в процессе генерации (RemoteDisconnected),
    watchdog поднимает его за ~5с — поэтому между попытками ждём `backoff` секунд.
    timeout — переопределение таймаута запроса (для сид-прохода: быстрый фейл-фаст)."""
    import time as _time
    import requests as _rqs
    _to = float(timeout) if timeout is not None else _M3_LLM_TIMEOUT
    payload = {"messages": messages, "max_tokens": int(max_tokens), "temperature": float(temperature)}
    if top_p is not None:
        payload["top_p"] = float(top_p)
    if repetition_penalty is not None:
        payload["repetition_penalty"] = float(repetition_penalty)
    last_err = "M3 недоступна"
    n = max(1, tries)
    for attempt in range(n):
        last = (attempt == n - 1)
        try:
            r = _rqs.post(f"{_M3_LLM_URL}/v1/chat/completions", json=payload, timeout=_to)
        except Exception as e:  # noqa: BLE001
            last_err = f"M3 недоступна: {str(e)[:160]}"
            if not last:
                _time.sleep(backoff)   # обрыв (RemoteDisconnected) → ждём перезапуск mlx watchdog'ом
            continue
        if r.status_code != 200:
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            if not last:
                _time.sleep(backoff)
            continue
        try:
            content = r.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError):
            last_err = "пустой ответ модели"
            if not last:
                _time.sleep(backoff)
            continue
        # Драфт-модель вклеила служебную ошибку в текст → чистим. Если после чистки осталась
        # осмысленная часть — отдаём её; иначе это мусор, повторяем генерацию.
        if _has_error_leak(content):
            cleaned = _strip_error_leak(content)
            if len(cleaned.strip()) >= 12 and last:
                return cleaned, None   # последняя попытка — спасаем осмысленный префикс
            last_err = "M3 вернула обрывок (сбой драфт-модели)"
            if not last:
                _time.sleep(backoff)
            continue
        return content, None
    return None, last_err


def _m3_complete_url(url: str, messages: list, max_tokens: int = 400, temperature: float = 0.8,
                     top_p: float | None = None, repetition_penalty: float | None = None,
                     tries: int = 2, backoff: float = 5.0, timeout: float | None = None) -> tuple:
    """Как _m3_complete, но с явным URL — для fan-out на разные порты 14B/72B."""
    import time as _time
    import requests as _rqs
    _to = float(timeout) if timeout is not None else _M3_LLM_TIMEOUT
    payload = {"messages": messages, "max_tokens": int(max_tokens), "temperature": float(temperature)}
    if top_p is not None:
        payload["top_p"] = float(top_p)
    if repetition_penalty is not None:
        payload["repetition_penalty"] = float(repetition_penalty)
    last_err = f"M3 ({url}) недоступна"
    n = max(1, tries)
    for attempt in range(n):
        last = (attempt == n - 1)
        try:
            r = _rqs.post(f"{url}/v1/chat/completions", json=payload, timeout=_to)
        except Exception as e:  # noqa: BLE001
            last_err = f"M3 ({url}) недоступна: {str(e)[:120]}"
            if not last:
                _time.sleep(backoff)
            continue
        if r.status_code != 200:
            last_err = f"HTTP {r.status_code}: {r.text[:160]}"
            if not last:
                _time.sleep(backoff)
            continue
        try:
            content = r.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError):
            last_err = "пустой ответ модели"
            if not last:
                _time.sleep(backoff)
            continue
        if _has_error_leak(content):
            cleaned = _strip_error_leak(content)
            if len(cleaned.strip()) >= 12 and last:
                return cleaned, None
            last_err = "M3 вернула обрывок (сбой драфт-модели)"
            if not last:
                _time.sleep(backoff)
            continue
        return content, None
    return None, last_err


def _m3_complete_parallel(tasks: list) -> list:
    """Параллельный вызов нескольких mlx-инстансов через ThreadPoolExecutor.
    tasks: [(url, messages, kwargs_dict), ...] — каждый task идёт на свой порт.
    Возвращает list[(text|None, error|None)] в том же порядке что tasks."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _call(idx_task):
        idx, (url, msgs, kw) = idx_task
        return idx, _m3_complete_url(url, msgs, **kw)

    results = [None] * len(tasks)
    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        futs = [ex.submit(_call, (i, t)) for i, t in enumerate(tasks)]
        for f in as_completed(futs):
            idx, res = f.result()
            results[idx] = res
    return results


_MONTHS_RU = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def _promo_extract_json(text: str) -> dict:
    """Достаём первый {...}-блок из ответа модели (бывает в ```json или с болтовнёй)."""
    import re
    if not text:
        return {}
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return {}


def _extract_title_candidates(raw: dict) -> tuple[list[str], str]:
    """Нормализовать ответ M3 по заголовкам.

    Модель иногда нарушает контракт и возвращает:
    {"titles":[{"title1":"...","title2":"..."}, ...]}
    вместо ожидаемого:
    {"titles":["..."], "title2":"..."}.
    Здесь приводим оба формата к единому виду, чтобы не терять валидные заголовки.
    """
    titles: list[str] = []
    title2 = ""
    if not isinstance(raw, dict):
        return titles, title2
    raw_title2 = raw.get("title2")
    if isinstance(raw_title2, str) and raw_title2.strip():
        title2 = raw_title2.strip()
    for it in (raw.get("titles") or []):
        if isinstance(it, str):
            txt = it.strip()
            if txt:
                titles.append(txt)
            continue
        if not isinstance(it, dict):
            continue
        txt = str(
            it.get("title1")
            or it.get("title")
            or it.get("headline")
            or it.get("text")
            or ""
        ).strip()
        t2 = str(
            it.get("title2")
            or it.get("subtitle")
            or it.get("subTitle")
            or ""
        ).strip()
        if txt:
            titles.append(txt)
        if not title2 and t2:
            title2 = t2
    return titles, title2


def _extract_text_candidates(raw: dict) -> list[str]:
    """Нормализовать тексты объявлений из строкового или объектного ответа M3."""
    out: list[str] = []
    if not isinstance(raw, dict):
        return out
    for it in (raw.get("texts") or []):
        if isinstance(it, str):
            txt = it.strip()
        elif isinstance(it, dict):
            txt = str(
                it.get("text")
                or it.get("body")
                or it.get("description")
                or it.get("copy")
                or ""
            ).strip()
        else:
            txt = ""
        if txt:
            out.append(txt)
    return out


def _promo_validate(d: dict, agent: dict, site_type: str = "") -> tuple[dict, list[str]]:
    """Нормализуем/клампим промо под лимиты Директа. → (promo, warnings).
    site_type — для гарда «не та лексика типу сайта» (б/у на сайте про НОВЫЕ авто)."""
    from . import ai_agents as A
    p = agent["promo"]
    warns: list[str] = []

    typ = str(d.get("type") or "").upper()
    if typ not in A.PROMO_TYPES:
        typ = p["type"]
    if typ == "CASHBACK":          # ⛔ глобальный запрет кешбэка → заменяем тип на дефолт стиля
        typ = p["type"] if p["type"] != "CASHBACK" else "DISCOUNT"
        warns.append("кешбэк запрещён — тип акции заменён")
    unit = str(d.get("unit") or "").upper()
    if unit not in A.PROMO_UNITS:
        unit = p["unit"]
    if typ == "GIFT":          # подарок всегда в рублях (до клампа, чтобы не срезать сумму по %-капу)
        unit = "RUB"

    amount = d.get("amount")
    try:
        amount = int(float(amount))
    except (TypeError, ValueError):
        amount = (p["amount_min"] + p["amount_max"]) // 2
    cap = A.AMOUNT_MAX_PCT if unit == "PCT" else A.AMOUNT_MAX_RUB
    if amount < 1:
        amount = 1
    if amount > cap:
        amount = cap
        warns.append(f"размер обрезан до {cap} ({unit})")
    # Реалистичность размера ПОД ТИП акции. Подарок — стоимость в ₽ (десятки–сотни тыс.),
    # а НЕ процентное число, ошибочно прочитанное как рубли («Подарок до 58 ₽» — баг).
    import random as _rnd
    if typ == "GIFT":
        unit = "RUB"
        if not (A.GIFT_AMOUNT_MIN <= amount <= A.GIFT_AMOUNT_MAX):
            amount = _rnd.choice(A.GIFT_STEPS)
            warns.append("сумма подарка приведена к реалистичной (₽)")

    prefix = str(d.get("prefix") or "").upper()
    if prefix not in A.PROMO_PREFIXES:
        prefix = p.get("prefix")          # может быть None — это ок

    promocode = (d.get("promocode") or "").strip()[:A.PROMOCODE_MAX] or None

    desc = (d.get("description") or "").strip().strip('"').replace("\n", " ")
    # подстраховка: если служебная ошибка драфт-модели просочилась ВНУТРЬ JSON-описания — режем её
    if _has_error_leak(desc):
        desc = _strip_error_leak(desc).strip()
        warns.append("убран обрывок служебной ошибки M3 из описания")
    # бан-фраза «закрытие …» → подменяем дефолтом стиля агента
    if any(b in desc.lower() for b in A.BANNED_SUBSTR):
        desc = (p.get("examples") or ["спецпредложение"])[0]
        warns.append("убрана запрещённая формулировка («закрытие автосалона»)")
    if A.has_forbidden_claim(desc):
        desc = (p.get("examples") or ["спецпредложение"])[0]
        warns.append("убрана запрещённая формулировка («гарантия»)")
    # ⛔ глобальный запрет кешбэка: вычищаем упоминание из описания
    if A.has_cashback(desc):
        fixed = A.strip_cashback(desc)
        desc = fixed if len(fixed.strip()) >= 3 else (p.get("examples") or ["спецпредложение"])[0]
        warns.append("убрано упоминание кешбэка (запрещён)")
    # ГАРД ТИПА САЙТА: на сайте про НОВЫЕ авто (Монобренд/Мультибренд/Квиз) лексика
    # «с пробегом / б/у» неуместна → вычищаем её из описания (зеркало бан-листа для б/у).
    if (site_type or "").strip() in A.NEW_ONLY_SITE_TYPES and A._bad_for_new(desc):
        fixed = A.strip_used_words(desc)
        if A.eff_len(fixed) >= 3:
            desc = fixed
        else:   # после чистки почти ничего не осталось — берём дефолт стиля агента
            desc = (p.get("examples") or ["на новые автомобили"])[0]
        warns.append("убрана лексика «с пробегом/б/у» — сайт про новые авто")
    # Грамматическая чистка ДО обрезки: двойной предлог «на при покупке» → «при покупке»,
    # приклеенный второй обрывок с заглавной («…авто Распродаём склад») → режем, висящие хвосты.
    desc = A.fix_promo_desc(desc)
    if re.search(r"(?<![\d])\d{4,}(?![\d\s]*(?:₽|руб|/мес))", desc):
        desc = (p.get("examples") or ["спецпредложение"])[0]
        warns.append("убрано техническое число из описания промо")
    # Длина описания «как режет ГРИД»: считаются ВСЕ символы (пробелы И знаки препинания).
    # Жёсткий лимит грида ≤ 25 (A.DESCRIPTION_MAX) — без template-маркера. Длиннее → grid
    # отклоняет промо (TEXT_LENGTH_WITHOUT_TEMPLATE_MARKER_CANNOT_BE_MORE_THAN).
    lim = A.DESCRIPTION_MAX
    if len(desc) > lim:
        words = desc.split(" ")
        while len(words) > 1 and len(" ".join(words)) > lim:
            words.pop()
        desc = " ".join(words)
        while len(desc) > lim:   # одно длинное слово — режем посимвольно
            desc = desc[:-1]
        desc = desc.rstrip()
        # после обрезки мог остаться висящий предлог в конце («…новый Haval при») — срезаем
        desc = A._promo_strip_tail(desc)
        warns.append(f"описание обрезано до {lim} симв. (лимит грида, считаются все символы)")
    if len(desc.strip()) < 3:
        desc = (p.get("examples") or ["спецпредложение"])[0]
        warns.append("описание было пустым — подставлен дефолт стиля")
    # с МАЛЕНЬКОЙ буквы (описание — продолжение фразы), кроме брендов-аббревиатур
    fw = desc.split()[0] if desc.split() else ""
    if fw and fw[0].isalpha() and not (len(fw) >= 2 and fw.isupper()):
        desc = desc[0].lower() + desc[1:]

    # Дату окончания акции НЕ указываем (по требованию) — промо показывается всегда.
    return ({"type": typ, "amount": amount, "unit": unit, "prefix": prefix,
             "description": desc, "promocode": promocode, "finishDate": None}, warns)


def _promo_amount_steps(p: dict, unit: str, promo_type: str = "") -> list[int]:
    """«Красивые» шаги размера в диапазоне агента — для вариативности при регенерации.
    Для подарка (GIFT) — реалистичные суммы стоимости подарка, а не процентный диапазон агента."""
    from . import ai_agents as A
    if (promo_type or "").upper() == "GIFT":
        return list(A.GIFT_STEPS)
    pool = ([40, 43, 45, 48, 50, 53, 55, 57, 60, 63] if unit == "PCT"
            else [700_000, 800_000, 900_000, 1_000_000, 1_200_000, 1_300_000, 1_500_000])
    lo, hi = int(p.get("amount_min") or 1), int(p.get("amount_max") or (100 if unit == "PCT" else 1_000_000))
    steps = [x for x in pool if lo <= x <= hi]
    return steps or [(lo + hi) // 2]


def _promo_preview(promo: dict) -> str:
    """Эмуляция итогового Name Директа: «{Тип} {преф} {размер} {описание} до {дата}»."""
    from . import ai_agents as A
    parts = [A.TYPE_WORD.get(promo["type"], promo["type"])]
    if promo.get("prefix"):
        parts.append(A.PREFIX_WORD.get(promo["prefix"], ""))
    if promo.get("amount") is not None:
        parts.append(f"{promo['amount']}%" if promo["unit"] == "PCT" else f"{promo['amount']:,} ₽".replace(",", " "))
    if promo.get("description"):
        parts.append(promo["description"])
    if promo.get("promocode"):
        parts.append(f"промокод {promo['promocode']}")
    txt = " ".join([x for x in parts if x])
    f = promo.get("finishDate")
    if f:
        try:
            y, mo, da = f.split("-")
            txt += f" до {int(da)} {_MONTHS_RU[int(mo)]}"
        except Exception:  # noqa: BLE001
            pass
    return txt


def _promo_ctx(login: str) -> dict | None:
    """Лёгкий контекст салона для генерации: domain/salon/city/site_type/agency."""
    import psycopg2.extras
    conn = _victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT domain, salon, city, site_type, agency_account FROM public.local_gsheet_sites "
                    "WHERE login_key=%s AND direction='Авто' LIMIT 1", (login,))
        return cur.fetchone()
    finally:
        conn.close()


# ── Бренд кампании из КОДЕРА: первый ct с 4 цифрами (ct####) → имя марки/модели (ag_part1) ──
_AG1_NAME_CACHE: dict | None = None
_CT4_RE = re.compile(r"ct\d{4}")


def _ag_part1_map() -> dict:
    """ct-код → имя марки/модели из gsheet_naming (ag_part1). Кэш на процесс."""
    global _AG1_NAME_CACHE
    if _AG1_NAME_CACHE is not None:
        return _AG1_NAME_CACHE
    m: dict = {}
    try:
        conn = _victory_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT code, name FROM public.gsheet_naming WHERE type='ag_part1'")
            for code, name in cur.fetchall():
                if code and name:
                    m[str(code).strip()] = str(name).strip()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — БД недоступна → без бренда (общий контент)
        pass
    _AG1_NAME_CACHE = m
    return m


_AG1_REV_CACHE: dict | None = None


def _ag_part1_rev() -> dict:
    """Обратная карта имя_модели(lower) → ct (ag_part1). Для tp6/tp7, где модель — в НАЗВАНИИ
    группы («Haval Jolion»), а не в ct кодера: по имени достаём ct модели (ct0119) для
    кодера/картинки/текста. Берём только реальные марки/модели (пропускаем темы/общие)."""
    global _AG1_REV_CACHE
    if _AG1_REV_CACHE is not None:
        return _AG1_REV_CACHE
    rev: dict = {}
    for ct, name in _ag_part1_map().items():
        nm = (name or "").strip().lower()
        if not nm or nm.startswith("кластер запросов не определен") or nm == "полное отсутствие ключей":
            continue
        rev.setdefault(nm, ct)
    _AG1_REV_CACHE = rev
    return rev


def _ct_for_name(name: str) -> str:
    """ct модели по её имени (название группы tp6/tp7). Нет совпадения → '' (общий контент)."""
    raw = (name or "").strip()
    low = raw.lower()
    rev = _ag_part1_rev()
    if low in rev:
        return rev[low]
    base = re.split(r"\s+-\s+", low, maxsplit=1)[0].strip()
    if base in rev:
        return rev[base]
    norm = re.sub(r"[^a-zа-яё0-9]+", " ", base).strip()
    if not norm:
        return ""
    # Фолбэк для структурных подписей вида "Lada Granta - Ключевики":
    # ищем самую длинную модель, входящую целиком в начало/текст названия.
    for nm, ct in sorted(rev.items(), key=lambda x: len(x[0]), reverse=True):
        nn = re.sub(r"[^a-zа-яё0-9]+", " ", nm).strip()
        if nn and (norm == nn or norm.startswith(nn + " ") or (" " + nn + " ") in (" " + norm + " ")):
            return ct
    return ""


# ── Title2: загрузка из БД и выбор по кругу ───────────────────────────────────
_TITLE2_CACHE: list | None = None
_TITLE2_IDX: int = 0


_TITLE2_BLOCK_CACHE: tuple | None = None


def _title2_blocklist() -> tuple[set, set]:
    """Слова, которых НЕ должно быть в обобщённом Title2: названия марок/моделей (сегменты
    Марки/Модели из gsheet_naming — НЕ темы «Авто/Автосалон/Авито») + города (local_gsheet_sites).
    Пул Title2 общий на ВСЕ аккаунты → бренд/город конкретного слепка туда попадать не должны
    (иначе «Автосалон Lada в Тольятти» бледит в группу BAIC/Кемерово). Кэш на процесс."""
    global _TITLE2_BLOCK_CACHE
    if _TITLE2_BLOCK_CACHE is not None:
        return _TITLE2_BLOCK_CACHE
    brands: set = set()
    try:
        for ct, nm in _ag_part1_map().items():
            if _ct_segment(ct) in ("Марки", "Модели"):
                w = (nm or "").strip().lower().split()
                if w and len(w[0]) >= 3:                 # ведущее слово = марка (lada, baic, chery…)
                    brands.add(w[0])
    except Exception:  # noqa: BLE001
        brands = set()
    cities: set = set()
    try:
        conn = _victory_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT lower(city) FROM public.local_gsheet_sites "
                        "WHERE direction='Авто' AND city IS NOT NULL AND city<>''")
            cities = {r[0].strip() for r in cur.fetchall() if r[0] and r[0].strip() and len(r[0].strip()) >= 4}
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        cities = set()
    _TITLE2_BLOCK_CACHE = (brands, cities)
    return _TITLE2_BLOCK_CACHE


def _title2_is_generic(text: str, brands: set, cities: set) -> bool:
    """True, если Title2 НЕ содержит конкретной марки/модели и города (обобщённый УТП)."""
    words = set(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())
    return not (words & brands) and not (words & cities)


def _load_title2_pool() -> list[str]:
    """Загрузить пул Title2 из public.direct_title2 (Victory). Кэш на процесс.
    Фолбэк-список встроен — сервис работает даже без БД. Авто-фильтр: строки с конкретной
    маркой/городом отсеиваются (Title2 обязан быть обобщённым, без чужого бренда/города)."""
    global _TITLE2_CACHE
    if _TITLE2_CACHE is not None:
        return _TITLE2_CACHE
    fallback = [
        "Авто в наличии", "Официальный дилер", "Кредит с господдержкой",
        "Trade-in в день обращения", "Тест-драйв без записи",
        "Выгода до 200 000 руб.", "КАСКО на 1 год",
        "Выгодные условия покупки", "Быстрое оформление за 1 час",
        "Приятные бонусы",
    ]
    try:
        conn = _victory_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT text FROM public.direct_title2 "
                "WHERE site_type='all' ORDER BY usage_count DESC, id"
            )
            rows = [r[0].strip() for r in cur.fetchall() if r[0] and r[0].strip()]
        finally:
            conn.close()
        # Авто-фильтр: убрать Title2 с конкретной маркой/городом (чужой бренд не должен бледить).
        br, ci = _title2_blocklist()
        if br or ci:
            rows = [t for t in rows if _title2_is_generic(t, br, ci)]
        _TITLE2_CACHE = rows if rows else fallback
    except Exception:  # noqa: BLE001
        _TITLE2_CACHE = fallback
    return _TITLE2_CACHE


def _next_title2() -> str:
    """Выбрать следующий Title2 по кругу из пула (round-robin).
    Round-robin даёт разнообразие в рамках одного пакетного прогона."""
    global _TITLE2_IDX
    pool = _load_title2_pool()
    if not pool:
        return ""
    t2 = pool[_TITLE2_IDX % len(pool)]
    _TITLE2_IDX += 1
    return t2


# ── Model page URL: глубокая ссылка на страницу модели ────────────────────────
# Мэппинг тип сайта → шаблон URL (проверено HEAD-запросами к vitmp.ru 2026-06-22).
_SITE_TYPE_URL_TPL: dict[str, str | None] = {
    "Мультибренд": "/auto/{brand_slug}/{model_slug}",
    "Монобренд":   "/auto/{brand_slug}/{model_slug}",
    "Квиз":        None,   # лендинг-квиз, страниц моделей нет → только главная
    "С пробегом":  "/catalog/{brand_slug}/{model_slug}",
    "Мульти + БУ": "/auto/{brand_slug}/{model_slug}",
    "Неопределено": None,
    "Не трогать!": None,
}


def _slugify(name: str) -> str:
    """Строка → slug URL: нижний регистр, пробелы/дефисы, убрать всё лишнее.
    Пример: 'Haval Jolion' → 'haval-jolion', 'LADA Granta' → 'lada-granta'.
    Кириллица транслитерируется по минимальной таблице авто-брендов."""
    _CYR = {
        "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"yo","ж":"zh",
        "з":"z","и":"i","й":"j","к":"k","л":"l","м":"m","н":"n","о":"o",
        "п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f","х":"kh","ц":"ts",
        "ч":"ch","ш":"sh","щ":"shch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya",
    }
    s = (name or "").strip().lower()
    out = []
    for ch in s:
        if ch in _CYR:
            out.append(_CYR[ch])
        elif ch.isalnum() or ch in "-_":
            out.append(ch)
        elif ch in " \t_/":
            out.append("-")
        # else: пропускаем (спецсимволы, скобки)
    slug = "-".join(p for p in "".join(out).split("-") if p)
    return slug[:60]


def _strip_url_query(u: str) -> str:
    """Срезать query-string (?...) и fragment (#...) из URL."""
    u = (u or "").strip()
    for sep in ("?", "#"):
        i = u.find(sep)
        if i >= 0:
            u = u[:i]
    return u.rstrip("/")


def _brand_level_url(u: str) -> str:
    """Обрезать абсолютный URL до уровня марки: домен + первые 2 сегмента пути.
    Пример: https://site/auto/baic/u5-plus/i/sedan?fid=x → https://site/auto/baic.
    Ожидает абсолютный URL из фида (targetUrl)."""
    u = _strip_url_query(u)
    if not u:
        return ""
    m = re.match(r"(https?://[^/]+)(.*)", u)
    if not m:
        return u
    origin, path = m.group(1), m.group(2)
    parts = [p for p in path.split("/") if p]
    brand_path = "/" + "/".join(parts[:2]) if len(parts) >= 2 else ("/" + parts[0] if parts else "")
    return origin + brand_path


def _is_site_domain_name(f_name: str, href: str = "") -> bool:
    """True если f_name совпадает с hostname аккаунта (href) — пропустить в имени кампании.
    Защита от вставки домена (напр. «autos-kemerovo.site») вместо имени фида контента."""
    if not f_name or not href:
        return False
    nm = (f_name or "").strip().lower()
    if nm.startswith("www."):
        nm = nm[4:]
    h = href.lower()
    for pfx in ("https://www.", "http://www.", "https://", "http://"):
        if h.startswith(pfx):
            h = h[len(pfx):]
            break
    host = h.split("/")[0].split("?")[0]
    return bool(host and nm == host)


def _model_page_href(base_href: str, site_type: str, model_name: str) -> str:
    """Построить deep-link страницы модели для объявления.

    base_href:  корневой URL сайта (например https://ac-aceauto.ru)
    site_type:  тип сайта из local_gsheet_sites (Мультибренд / Монобренд / С пробегом / …)
    model_name: название модели из ag_part1 (например «Haval Jolion», «LADA Granta»)

    Логика:
    - У модели «Haval Jolion» первое слово — бренд, остальное — модель.
    - Монобренд: бренд уже в домене, но URL та же /auto/{brand}/{model} (проверено live).
    - Нет шаблона для типа (Квиз/None) ИЛИ нет имени модели → возвращаем голый base_href.
    - Slugify: 'Haval Jolion' → brand_slug='haval', model_slug='jolion'.

    Примеры (проверено HEAD 2026-06-22):
      Мультибренд «LADA Granta»  → /auto/lada/granta
      Монобренд «Belgee X50»     → /auto/belgee/x50
      С пробегом «Haval Jolion»  → /catalog/haval/jolion
    """
    tpl = _SITE_TYPE_URL_TPL.get(site_type)
    if not tpl or not model_name:
        return base_href.rstrip("/")
    parts = (model_name or "").strip().split(None, 1)  # split по первому пробелу
    if len(parts) < 2:
        # Только МАРКА (группа сегмента «Марки», напр. «Lada»): deep-link на страницу марки
        # /auto/{brand} (без модели). Правило Семёна: марочное комбо-объявление ведёт на марку,
        # не на главную. Пример: Lada → https://site/auto/lada.
        _bs = _slugify(parts[0]) if parts else ""
        if not _bs:
            return base_href.rstrip("/")
        return base_href.rstrip("/") + tpl.format(brand_slug=_bs, model_slug="").rstrip("/")
    brand_slug = _slugify(parts[0])
    model_slug = _slugify(parts[1])
    if not brand_slug or not model_slug:
        return base_href.rstrip("/")
    path = tpl.format(brand_slug=brand_slug, model_slug=model_slug)
    return base_href.rstrip("/") + path


def _coder_name_real_brand(name: str) -> bool:
    """True если имя ag_part1 — РЕАЛЬНАЯ марка/модель авто (а не «Общее»-метка: Авито/Дром/Дзен/
    Автокредит/Trade-in). Без этой защиты источник/тема трактуется как бренд → заголовки требуют
    токен бренда («авито») → все отбиты → 0 валидных → краш «нужен заголовок» (и товарный feedFilter
    field=model=[«Дром»] → UNKNOWN_FIELD). Тот же справочник, что разводит ct на Марки/Модели/Общее."""
    nm = (name or "").strip()
    if not nm:
        return False
    return _is_brand_canon(_brand_canon(re.split(r"[\s/]+", nm.lower())[0]))


def _brand_ct_from_coder(item: dict) -> tuple[str, str]:
    """(марка/модель, ct) из КОДЕРА: ПЕРВЫЙ ct#### (≠ct0000), у которого ag_part1 — реальная
    марка/модель. Источник — gc (tp1-5) / code|c (tp6/7) / name. Правило пользователя:
    есть марка/модель в ct → контент по модели; ct0000/нет/НЕ-марка → общий. → ('','') если нет."""
    if not isinstance(item, dict):
        return "", ""
    explicit_ct = str(item.get("coder_ct") or item.get("ct") or "").strip()
    if explicit_ct:
        name = _ag_part1_map().get(explicit_ct)
        if (explicit_ct == "ct0000" or not name or name.startswith("кластер запросов не определен")
                or name == "полное отсутствие ключей" or not _coder_name_real_brand(name)):
            return "", explicit_ct
        return name, explicit_ct
    for key in ("coder_ct", "ct"):
        ct = str(item.get(key) or "").strip()
        if ct and ct != "ct0000":
            name = _ag_part1_map().get(ct)
            if (name and not name.startswith("кластер запросов не определен")
                    and name != "полное отсутствие ключей" and _coder_name_real_brand(name)):
                return name, ct
    direct_brand = str(item.get("coder_brand") or item.get("brand") or "").strip()
    if direct_brand:
        direct_ct = _ct_for_name(direct_brand)
        if direct_ct and direct_ct != "ct0000":
            _nm = _ag_part1_map().get(direct_ct, direct_brand)
            return (_nm if _coder_name_real_brand(_nm) else ""), direct_ct
    for key in ("gc", "code", "c", "name"):
        code = str(item.get(key) or "")
        all_ct = _CT4_RE.findall(code)
        if len(set(all_ct)) > 1:
            return "", explicit_ct or ""
        for mt in _CT4_RE.finditer(code):
            ct = mt.group(0)
            if ct == "ct0000":
                continue
            name = _ag_part1_map().get(ct)
            if (name and not name.startswith("кластер запросов не определен")
                    and name != "полное отсутствие ключей" and _coder_name_real_brand(name)):
                return name, ct
    return "", ""


def _brand_from_coder(item: dict) -> str:
    """Марка/модель кампании из КОДЕРА: ПЕРВЫЙ ct с 4 цифрами (ct####) → имя ag_part1.
    Источник — групповой кодер gc (tp1-5) или кампанийный c/code (tp6/7). ct0000 / нет → ''.
    Прямое поле item['brand'] имеет приоритет (если фронт уже прислал марку)."""
    if not isinstance(item, dict):
        return ""
    direct = str(item.get("brand") or "").strip()
    if direct:
        return direct
    return _brand_ct_from_coder(item)[0]


def _content_cache_key(agent_key: str, site_type: str, city: str, item: dict) -> tuple:
    """Ключ контента: один валидный набор переиспользуется для того же st/ct кодера."""
    brand, ct = _brand_ct_from_coder(item)
    if not ct:
        ct = "ct0000"
    return (
        (agent_key or "").strip().lower(),
        (site_type or "").strip(),
        (city or "").strip().lower(),
        _gc_ct(ct),
        (brand or "").strip().lower(),
    )


def _content_copy(content: dict | None) -> dict:
    if not isinstance(content, dict):
        return {}
    try:
        return json.loads(json.dumps(content, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        return dict(content)


def _content_complete(content: dict | None) -> bool:
    if not isinstance(content, dict):
        return False
    return (len(content.get("titles") or []) >= 5
            and len(content.get("texts") or []) >= 3
            and len(content.get("sitelinks") or []) >= 8)


def _ai_campaign_content_for_item(login: str, slepok: str, site_type: str, city: str,
                                  item: dict, avoid: list | None = None) -> dict | None:
    """AI-first контент для конкретного st/ct; слепок используется внутри _gen_campaign_content как фолбэк."""
    if not (login and slepok and isinstance(item, dict)):
        return None
    city = _content_city(city)                            # мультигород (через запятую) → M3 без города
    try:
        from . import ai_agents as A
        agent_obj = A.get_agent(slepok)
    except Exception:  # noqa: BLE001
        agent_obj = None
    if not agent_obj:
        return None
    return _cached_campaign_content(
        login, agent_obj, (slepok or "").strip().lower(), item,
        site_type, city, avoid=avoid or [],
    )


def _ai_group_content(login: str, slepok: str, site_type: str, city: str,
                      tp_code: str, ct: str, brand: str,
                      avoid: list | None = None) -> dict | None:
    item = {
        "brand": (brand or "").strip(),
        "gc": ct,
        "ct": ct,
        "tp": tp_code,
        "campaign_type": tp_code,
        "type": tp_code,
        "name": brand or ct,
    }
    return _ai_campaign_content_for_item(login, slepok, site_type, city, item, avoid=avoid)


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


@bp.route("/api/ai/agents")
@_direct_access
def api_ai_agents():
    """Список ИИ-агентов (слепков директологов) для дропдауна."""
    from . import ai_agents as A
    return jsonify({"agents": A.agent_list()})


# ── БД-библиотека контента слепков (фолбэк при сбое M3): (слепок × тип сайта × kind) → jsonb ──
# kind='promo' → СПИСОК вариантов промо; kind='campaign' → {titles,texts,sitelinks}.
def _slepok_content_ensure(cur) -> None:
    cur.execute(
        "CREATE TABLE IF NOT EXISTS public.direct_slepok_content ("
        " slepok text NOT NULL, site_type text NOT NULL, kind text NOT NULL,"
        " content jsonb NOT NULL, source text NOT NULL DEFAULT 'slepok',"
        " updated_at timestamptz DEFAULT now(),"
        " PRIMARY KEY (slepok, site_type, kind))")


def _slepok_content_get(slepok: str, site_type: str, kind: str):
    """Контент из БД-библиотеки слепков. → list/dict или None (нет записи / БД недоступна).
    NB: используем readonly-коннекшен (read-only), поэтому _slepok_content_ensure пропускаем —
    CREATE TABLE на readonly упадёт с ReadOnlySqlTransaction и скроет результат."""
    if not (slepok and site_type):
        return None
    try:
        conn = _victory_conn()
    except Exception:  # noqa: BLE001
        return None
    try:
        cur = conn.cursor()
        # NB: НЕ зовём _slepok_content_ensure здесь — она делает CREATE TABLE IF NOT EXISTS,
        # что падает на readonly-коннекшене и глушит весь запрос.
        cur.execute("SELECT content FROM public.direct_slepok_content "
                    "WHERE slepok=%s AND site_type=%s AND kind=%s", (slepok, site_type, kind))
        row = cur.fetchone()
        return row[0] if row else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        conn.close()


def _slepok_content_save(slepok: str, site_type: str, kind: str, content, source: str = "slepok") -> bool:
    try:
        conn = _victory_conn_rw()
    except Exception:  # noqa: BLE001
        return False
    try:
        cur = conn.cursor()
        _slepok_content_ensure(cur)
        cur.execute(
            "INSERT INTO public.direct_slepok_content(slepok, site_type, kind, content, source, updated_at) "
            "VALUES (%s, %s, %s, %s::jsonb, %s, now()) "
            "ON CONFLICT (slepok, site_type, kind) DO UPDATE SET "
            "content = EXCLUDED.content, source = EXCLUDED.source, updated_at = now()",
            (slepok, site_type, kind, json.dumps(content, ensure_ascii=False), source))
        conn.commit()
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        conn.close()


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


@bp.route("/api/ai/promo/generate", methods=["POST"])
@_direct_access
def api_ai_promo_generate():
    """Сгенерировать промоакцию в стиле выбранного агента. Тело: {login, agent}."""
    from . import ai_agents as A
    d = request.json or {}
    login = (d.get("login") or "").strip()
    agent = A.get_agent(d.get("agent"))
    if not login:
        return jsonify({"ok": False, "error": "login обязателен"}), 400
    if not agent:
        return jsonify({"ok": False, "error": "неизвестный агент"}), 400
    ctx = _promo_ctx(login)
    if not ctx:
        return jsonify({"ok": False, "error": f"аккаунт {login} не найден в БД (Авто)"}), 404

    avoid = d.get("avoid") if isinstance(d.get("avoid"), list) else []
    avoid = [str(a)[:60] for a in avoid if a][-6:]
    # РОТАЦИЯ ТИПА АКЦИИ при «Сгенерировать снова»: меняем сам характер акции (Выгода→Скидка→
    # Кешбэк→Подарок), а не только текст. На первом прогоне — дефолтный тип стиля агента.
    avoid_types = [str(t).upper() for t in (d.get("avoid_types") or []) if t][-6:]
    force_type = A.next_promo_type(avoid_types, agent["promo"]["type"]) if avoid else None
    messages = A.build_promo_messages(agent, ctx, avoid=avoid, force_type=force_type)
    # на повторных генерациях поднимаем температуру — больше разнообразия
    temp = 1.05 if avoid else 0.85
    text, err = _m3_complete(messages, max_tokens=400, temperature=temp)
    raw = _promo_extract_json(text) if not err else {}
    domain = (ctx.get("domain") or "").strip()
    # M3 недоступна/обрыв/не-JSON → НЕ падаем, а собираем промо из СЛЕПКА (пресет стиля агента).
    if err or not raw:
        promo, warns = _promo_from_slepok(agent, ctx, force_type=force_type,
                                          avoid=avoid, avoid_amounts=d.get("avoid_amounts"),
                                          slepok_key=(d.get("agent") or ""))
        reason = err or "модель вернула не-JSON"
        warns = [f"⚠ M3 недоступна ({reason}) — промо собрано из слепка «{agent['name']}»"] + warns
        return jsonify({"ok": True, "agent": agent["name"], "login": login, "fallback": True,
                        "domain": domain, "href": ("https://" + domain) if domain else "",
                        "promo": promo, "preview": _promo_preview(promo), "warnings": warns})
    promo, warns = _promo_validate(raw, agent, site_type=(ctx.get("site_type") or ""))
    # Гарантируем ротацию: даже если модель проигнорила — ставим запрошенный тип акции.
    if force_type:
        promo["type"] = force_type
        if force_type == "GIFT":
            promo["unit"] = "RUB"
    # При регенерации модель якорит размер на одном числе → подбираем другой «красивый»
    # шаг из диапазона агента, отличный от уже показанных (avoid_amounts с фронта).
    if avoid:
        import random
        excl = {int(a) for a in (d.get("avoid_amounts") or []) if str(a).strip().isdigit()}
        steps = [x for x in _promo_amount_steps(agent["promo"], promo["unit"], promo["type"]) if x not in excl]
        if steps:
            promo["amount"] = random.choice(steps)
    return jsonify({"ok": True, "agent": agent["name"], "login": login,
                    "domain": domain, "href": ("https://" + domain) if domain else "",
                    "promo": promo, "preview": _promo_preview(promo), "warnings": warns})


@bp.route("/api/ai/campaign/generate", methods=["POST"])
@_direct_access
def api_ai_campaign_generate():
    """Сгенерировать контент ОДНОЙ РК (заголовки/тексты/быстрые ссылки) в стиле агента.
    Тело: {login?, ctx?, agent, item:{name,type,variant}, avoid?:[заголовки]}.
    → {ok, agent, login, item, content:{titles,texts,sitelinks}, warnings}."""
    from . import ai_agents as A
    d = request.json or {}
    login = (d.get("login") or "").strip()
    ctx = d.get("ctx") if isinstance(d.get("ctx"), dict) else None
    agent = A.get_agent(d.get("agent"))
    item = d.get("item") if isinstance(d.get("item"), dict) else {}
    if not login and not ctx:
        return jsonify({"ok": False, "error": "нужен login или ctx"}), 400
    if not agent:
        return jsonify({"ok": False, "error": "неизвестный агент"}), 400
    avoid0 = d.get("avoid") if isinstance(d.get("avoid"), list) else []
    res = _gen_campaign_content(login, agent, (d.get("agent") or "").strip().lower(),
                                item, avoid=avoid0, ctx_override=ctx)
    if not res.get("ok"):
        return jsonify(res), (404 if "не найден" in (res.get("error") or "") else 400)
    return jsonify(res)


def _gen_campaign_content(login: str, agent: dict, agent_key: str, item: dict,
                          avoid: list | None = None, ctx_override: dict | None = None,
                          fast_mode: bool = False) -> dict:
    """Ядро генерации контента ОДНОЙ РК (M3 fan-out 14B×3 + 72B-патч + фолбэк слепка). БЕЗ HTTP —
    зовётся и эндпоинтом api_ai_campaign_generate, и потоково из create_set (контент 1 РК → создание 1 РК).
    Тонкая обёртка: тело вынесено в `create_content.run_gen_campaign_content` (DI-модуль).
    → {ok, agent, login, item, brand, content:{titles,texts,sitelinks,title2?}, warnings, fallback}
      | {ok:False, error}."""
    from .create_content import run_gen_campaign_content
    return run_gen_campaign_content(
        login=login, agent=agent, agent_key=agent_key, item=item,
        avoid=avoid, ctx_override=ctx_override, fast_mode=fast_mode,
        _bad_ad_sitelink=_bad_ad_sitelink, _bad_ad_text=_bad_ad_text, _bad_ad_title=_bad_ad_title,
        _brand_from_coder=_brand_from_coder, _display_brand=_display_brand,
        _extract_text_candidates=_extract_text_candidates,
        _extract_title_candidates=_extract_title_candidates,
        _m3_complete_parallel=_m3_complete_parallel, _m3_complete_url=_m3_complete_url,
        _promo_ctx=_promo_ctx, _promo_extract_json=_promo_extract_json,
        _slepok_content_get=_slepok_content_get, _title2_blocklist=_title2_blocklist,
        _variant_norm_key=_variant_norm_key,
        _M3_LLM_REPAIR_TIMEOUT=_M3_LLM_REPAIR_TIMEOUT, _M3_LLM_TIMEOUT_14B=_M3_LLM_TIMEOUT_14B,
        _M3_LLM_URLS_14B=_M3_LLM_URLS_14B, _M3_LLM_URL_72B=_M3_LLM_URL_72B, _RU_CITIES=_RU_CITIES,
    )


def _seed_slepok_content(only_missing: bool = True, m3_timeout: float = 45.0) -> dict:
    """Засев БД-библиотеки слепков `direct_slepok_content`: на каждый (слепок × тип сайта из
    site_fit) ПРОБУЕМ M3, при неудаче берём из корпуса слепка. kind='promo' (список вариантов)
    и kind='campaign' ({titles,texts,sitelinks}). only_missing — пропускать уже заполненные.
    Request-free: вызывается из скрипта/эндпоинта. Возвращает отчёт по источникам."""
    import random
    from . import ai_agents as A
    # ВСЕ типы сайта (а не только site_fit слепка) — чтобы по КАЖДОМУ слепку был контент на любой
    # тип сайта (фолбэк сработает, какой бы слепок×тип пользователь ни выбрал; слепок адаптируется).
    site_types = list(A.SITE_TYPE_PROFILE.keys())
    # нейтральные осмысленные описания промо под тип сайта — на случай НЕ-родного слепку типа
    _neutral = {"С пробегом": ["на авто с пробегом", "на проверенные авто", "за автокредит"],
                "Мульти + БУ": ["на авто в наличии", "за автокредит", "при покупке в кредит"]}
    _neutral_new = ["на новые авто", "при покупке в кредит", "по госпрограмме"]
    rep = {"combos": 0, "promo": {"m3": 0, "slepok": 0, "skip": 0},
           "campaign": {"m3": 0, "slepok": 0, "skip": 0}}
    for a in A.agent_list():
        key = a["key"]
        agent = A.get_agent(key)
        if not agent:
            continue
        p = agent["promo"]
        for st in site_types:
            rep["combos"] += 1
            ctx = {"site_type": st, "domain": "", "salon": "", "city": ""}
            # ── PROMO: набор вариантов (M3 по разным типам + примеры слепка + нейтральные) ──
            if only_missing and _slepok_content_get(key, st, "promo"):
                rep["promo"]["skip"] += 1
            else:
                variants, src, seen = [], "slepok", set()
                for ft in (p["type"], "PROFIT", "GIFT"):
                    msgs = A.build_promo_messages(agent, ctx, force_type=ft)
                    text, err = _m3_complete(msgs, max_tokens=300, temperature=0.95,
                                             tries=1, timeout=m3_timeout)
                    raw = _promo_extract_json(text) if not err else {}
                    if raw:
                        pr, _ = _promo_validate(raw, agent, site_type=st)
                        if ft in A.PROMO_TYPES:
                            pr["type"] = ft
                            if ft == "GIFT":
                                pr["unit"] = "RUB"
                        k = (pr.get("description") or "").strip().lower()
                        if k and k not in seen:
                            seen.add(k); variants.append(pr); src = "m3"
                # добиваем примерами из корпуса слепка + нейтральными под тип сайта (для НЕ-родных комбо)
                ex_pool = list(p.get("examples") or []) + _neutral.get(st, _neutral_new)
                for ex in ex_pool:
                    if A.is_bu_site_type(st) and A._bad_for_bu(ex):
                        continue   # на б/у-сайте новоавтомобильные примеры слепка неуместны
                    pr, _ = _promo_validate({"type": p["type"], "amount": None, "unit": p["unit"],
                                             "prefix": p.get("prefix"), "description": ex},
                                            agent, site_type=st)
                    k = (pr.get("description") or "").strip().lower()
                    if k and k not in seen:
                        seen.add(k); variants.append(pr)
                if variants:
                    _slepok_content_save(key, st, "promo", variants[:8], src)
                    rep["promo"][src] += 1
            # ── CAMPAIGN: один полный комплект (заголовки/тексты/ссылки) ──
            if only_missing and _slepok_content_get(key, st, "campaign"):
                rep["campaign"]["skip"] += 1
            else:
                msgs = A.build_campaign_messages(agent, ctx, item={})
                text, err = _m3_complete(msgs, max_tokens=800, temperature=0.8,
                                         top_p=0.9, repetition_penalty=1.15, tries=1, timeout=m3_timeout)
                raw = _promo_extract_json(text) if not err else {}
                if raw:
                    content, _ = A.validate_campaign(raw, agent, site_type=st)
                    content, _ = A.assemble_campaign(content["titles"], content["texts"],
                                                     content["sitelinks"], agent, site_type=st, brand="")
                    csrc = "m3"
                else:
                    content, _ = A.assemble_campaign([], [], [], agent, site_type=st, brand="")
                    csrc = "slepok"
                _slepok_content_save(key, st, "campaign", content, csrc)
                rep["campaign"][csrc] += 1
    return rep


@bp.route("/api/ai/slepok_content/seed", methods=["POST"])
@_direct_access
def api_ai_slepok_content_seed():
    """Засев/обновление БД-библиотеки контента слепков. Тело: {only_missing?:bool, m3_timeout?:float}.
    ВНИМАНИЕ: долгий (до десятков M3-вызовов) — для разового прогона лучше скрипт seed_slepok_content.py."""
    d = request.json or {}
    rep = _seed_slepok_content(only_missing=bool(d.get("only_missing", True)),
                               m3_timeout=float(d.get("m3_timeout") or 45))
    return jsonify({"ok": True, "report": rep})


@bp.route("/api/ai/slepok_content")
@_direct_access
def api_ai_slepok_content():
    """Просмотр БД-библиотеки слепков: {rows:[{slepok,site_type,kind,source,n,updated_at}]}."""
    import psycopg2.extras
    try:
        conn = _victory_conn()
    except Exception as e:  # noqa: BLE001
        return jsonify({"rows": [], "error": str(e)[:200]})
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # NB: НЕ зовём _slepok_content_ensure — readonly-коннекшен не поддерживает CREATE TABLE.
        # Таблица уже существует (засеяна); если вдруг нет — SELECT вернёт пустой список.
        cur.execute(
            "SELECT slepok, site_type, kind, source, "
            "       jsonb_array_length(CASE WHEN jsonb_typeof(content)='array' THEN content ELSE '[]'::jsonb END) AS n, "
            "       updated_at FROM public.direct_slepok_content ORDER BY slepok, site_type, kind")
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r["updated_at"] = r["updated_at"].isoformat() if r.get("updated_at") else None
        return jsonify({"rows": rows, "total": len(rows)})
    finally:
        conn.close()


@bp.route("/api/ai/promo/publish", methods=["POST"])
@_direct_access
def api_ai_promo_publish():
    """Опубликовать промо в кабинет клиента через grid/api. ТОЛЬКО по подтверждению.
    Тело: {login, agency?, promo:{type,amount,unit,prefix,description,promocode?,finishDate?}, href?}."""
    from . import ai_agents as A
    from .promo import PromoClient
    body = request.json or {}
    login = (body.get("login") or "").strip()
    raw = body.get("promo") or {}
    if not login or not raw:
        return jsonify({"ok": False, "error": "login и promo обязательны"}), 400

    ctx = _promo_ctx(login)
    if not ctx:
        return jsonify({"ok": False, "error": f"аккаунт {login} не найден в БД"}), 404
    domain = (ctx.get("domain") or "").strip()
    href = (body.get("href") or (("https://" + domain) if domain else "")).strip()
    if not href:
        return jsonify({"ok": False, "error": "нет домена аккаунта для ссылки промо"}), 400

    # повторная валидация на сервере (фронту не доверяем)
    agent = A.get_agent(body.get("agent")) or {"promo": {"type": "DISCOUNT", "unit": "PCT",
                                                          "prefix": "TO", "amount_min": 30,
                                                          "amount_max": 50, "examples": ["Спецпредложение"]}}
    promo, _ = _promo_validate(raw, agent)

    ok, reason, wait = _pull_begin(f"promo:{login}", 8.0)
    if not ok:
        return _busy_response(reason, wait)
    try:
        # Рабочее агентство: override-кэш → БД → перебор токенов (+persist); куку берём ту же.
        _pr_token, _w_agency = _token_for_login(login, body.get("agency") or ctx.get("agency_account") or "", _direct_tokens())
        try:
            client = cmc.build_client(login, account=(_w_agency or None))
            client.link_info(href)            # bootstrap CSRF на куках главпотока
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"не удалось подобрать рабочую куку: {str(e)[:160]}"}), 502

        pc = PromoClient(client, login)
        pid, perr = pc.add(type=promo["type"], description=promo["description"], href=href,
                           amount=promo["amount"], unit=promo["unit"], prefix=promo["prefix"],
                           promocode=promo["promocode"], finish=promo["finishDate"])
        if not pid:
            return jsonify({"ok": False, "error": f"grid отклонил промо: {perr}"}), 502

        # верификация официальным OAuth promotions.get у клиента (токен уже резолвлен выше)
        verified = None
        token = _pr_token
        if token:
            jp = _v5_get("promotions", token, login,
                         ["Id", "Type", "Name", "Description", "Amount", "AmountUnit"], criteria={})
            for it in (jp.get("result") or {}).get("Promotions", []):
                if str(it.get("Id")) == str(pid):
                    verified = {"id": it.get("Id"), "name": it.get("Name"),
                                "type": it.get("Type"), "amount": it.get("Amount")}
                    break
        return jsonify({"ok": True, "id": pid, "preview": _promo_preview(promo),
                        "verified": verified})
    finally:
        _pull_end(f"promo:{login}")

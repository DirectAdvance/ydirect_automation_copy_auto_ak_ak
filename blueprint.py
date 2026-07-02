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
_COPY_SUPPORTED_V5_TYPES = {"TEXT_CAMPAIGN", "DYNAMIC_TEXT_CAMPAIGN", "UNIFIED_AD_CAMPAIGN"}
_COPY_JSON_PAYLOADS = (
    "campaigns.json", "adgroups.json", "ads.json", "shopping_ads.json", "keywords.json",
    "sitelinks.json", "vcards.json", "feeds.json", "promotions.json",
)


def _copy_canonical_region_name(region: str) -> str:
    """Normalize Direct/Victory region labels for copied campaign names."""
    text = (region or "").strip()
    low = text.lower().replace("ё", "е")
    if low in {"башкортостан, республика", "республика башкортостан"}:
        return "Республика Башкортостан"
    return text


def _copy_geo_id_for_target(city: str | None, region: str | None) -> tuple[int | None, str | None]:
    """Geo for copy-flow.

    For campaign copy we target the account's business region, not the city
    prefill. Example: Ufa accounts must copy to Republic of Bashkortostan
    (11111), while generic create_set still keeps city-first _geo_id().
    """
    region_name = _copy_canonical_region_name(region or "")
    if region_name:
        gid, used = _geo_id(None, region_name)
        if gid:
            return gid, used
    return _geo_id(city, region_name or region)


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


def _copy_apply_geo_replacements(text: str | None, replacements: list[tuple[str, str]]) -> str:
    out = str(text or "")
    for old, new in replacements or []:
        if old:
            out = out.replace(old, new)
    return out


def _copy_normalize_campaign_name(name: str | None, replacements: list[tuple[str, str]]) -> str:
    out = _copy_apply_geo_replacements(name, replacements).strip()
    out = re.sub(r"^\s*Копия\s+ХАВАЛ\s+", "Haval ", out, flags=re.I)
    out = re.sub(r"^\s*Копия\s+", "", out, flags=re.I)
    out = out.replace("Башкортостан, республика", "Республика Башкортостан")
    out = out.replace("ХАВАЛ", "Haval")
    return out.strip()


def _copy_domain_from_href(href: str | None) -> str:
    m = re.match(r"^https?://([^/?#]+)", str(href or "").strip(), re.I)
    return (m.group(1).lower() if m else "").strip()


def _copy_grid_ad_image_hashes(ad: dict) -> list[str]:
    out: list[str] = []
    img = ad.get("image")
    if isinstance(img, dict) and img.get("imageHash"):
        out.append(str(img["imageHash"]))
    for item in ad.get("images") or []:
        if isinstance(item, dict) and item.get("imageHash"):
            out.append(str(item["imageHash"]))
    return list(dict.fromkeys(x for x in out if x))


def _copy_v501_ad_image_hashes(login: str, campaign_ids: set[int], agency_hint: str = "") -> dict[int, list[str]]:
    """Best-effort source image read: {adGroupId: [imageHash, ...]}.

    Grid read often returns ``GdTextAd.image`` as null, while v501 exposes
    legacy ``TextAd.AdImageHash`` and responsive ``AdImages``. This is read-only
    and used only to preserve source creatives during cookie copy when available.
    """
    ids = [int(x) for x in (campaign_ids or []) if int(x) > 0]
    if not ids:
        return {}
    try:
        token, _agency = _token_for_login(login, agency_hint or _resolve_agency_hint(login, ""), _direct_tokens())
    except Exception:
        token = None
    if not token:
        return {}
    out: dict[int, list[str]] = {}

    def _add(gid: int, value) -> None:
        if not gid or not value:
            return
        vals = out.setdefault(int(gid), [])
        if isinstance(value, dict):
            value = value.get("Items") or []
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    h = item.get("ImageHash") or item.get("AdImageHash") or item.get("Hash")
                else:
                    h = item
                h = str(h or "").strip()
                if h and h not in vals:
                    vals.append(h)
        else:
            h = str(value or "").strip()
            if h and h not in vals:
                vals.append(h)

    for i in range(0, len(ids), 10):
        params = {
            "SelectionCriteria": {"CampaignIds": ids[i:i + 10]},
            "FieldNames": ["Id", "CampaignId", "AdGroupId", "Type"],
            "TextAdFieldNames": ["AdImageHash"],
            "ResponsiveAdFieldNames": ["AdImages"],
            "Page": {"Limit": 10000, "Offset": 0},
        }
        try:
            data = _v501_svc("ads", "get", token, login, params)
        except Exception:
            continue
        if data.get("error"):
            continue
        for ad in ((data.get("result") or {}).get("Ads") or []):
            try:
                gid = int(ad.get("AdGroupId") or 0)
            except (TypeError, ValueError):
                continue
            if ad.get("TextAd"):
                _add(gid, (ad.get("TextAd") or {}).get("AdImageHash"))
            if ad.get("ResponsiveAd"):
                _add(gid, (ad.get("ResponsiveAd") or {}).get("AdImages"))
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


def _copy_grid_read_selected(login: str, selected_ids: set[int]) -> dict:
    """Read selected Unified campaigns with Grid cookies, without Direct API units."""
    from .grid_read import GridReadClient

    ids = [int(x) for x in selected_ids if int(x) > 0]
    if not ids:
        return {"campaigns": [], "groups": [], "ads": []}
    id_strings = [str(x) for x in ids]
    reader = GridReadClient(login)
    inp_common = {
        "filter": {"campaignIdIn": id_strings},
        "statRequirements": {"preset": "TODAY", "goalIds": [], "useCampaignGoalIds": True},
        "limitOffset": {"limit": 10000, "offset": 0},
        "orderBy": [{"order": "ASC", "field": "ID"}],
    }
    q_campaigns = (
        "query CopyCamp($login:String!,$inp:GdCampaignsContainerInput!){"
        "client(searchBy:{login:$login}){campaigns(input:$inp){rowset{"
        "id name __typename status{primaryStatus archived} "
        "...on GdUnifiedCampaign{metrikaCounters placementTypes additionalData{href} "
        "minusKeywords disabledPlaces}}}}}"
    )
    camps_data = reader._post("CopyCamp", q_campaigns, {"login": login, "inp": inp_common})
    campaigns = ((((camps_data.get("data") or {}).get("client") or {})
                  .get("campaigns") or {}).get("rowset") or [])

    q_ads = (
        "query CopyAds($login:String!,$inp:GdAdsContainerInput!){"
        "client(searchBy:{login:$login}){ads(input:$inp){rowset{"
        "__typename id campaignId adGroupId "
        "...on GdTextAd{href title titleExtension body domain image{imageHash name} status{primaryStatus}} "
        "...on GdAdaptiveTextAd{href titles bodies images{imageHash name}} "
        "...on GdShoppingAd{id adGroupId campaignId} "
        "...on GdListingAd{id adGroupId campaignId}"
        "}}}}"
    )
    ads_data = reader._post("CopyAds", q_ads, {"login": login, "inp": inp_common})
    ads = ((((ads_data.get("data") or {}).get("client") or {})
            .get("ads") or {}).get("rowset") or [])

    groups = gf.GridClient(login, cookie=reader.cookie).groups_for_edit(ids)
    return {"campaigns": campaigns, "groups": groups, "ads": ads}


def _copy_grid_campaign_spec(name: str, counter_id: int, goal_id: int) -> dict:
    m = re.search(r"\btp(\d+)_", str(name or ""), re.I)
    tp = int(m.group(1)) if m else 1
    search = tp in (2, 4, 5)
    gallery = tp in (3, 5)
    network = tp in (1, 3)
    return {
        "name": str(name or "")[:255],
        "counter_id": int(counter_id or 0),
        "goal_id": int(goal_id or 0),
        "cpa": 250,
        "weekly_budget": 7000,
        "start_date": time.strftime("%Y-%m-%d"),
        "network": bool(network),
        "search": bool(search),
        "gallery": bool(gallery),
        "organic": bool(tp == 5),
        "pay_for_conversion": False,
    }


def _copy_grid_unified_campaigns(job_id: str, body: dict, selected_grid_rows: list[dict],
                                 workdir: Path) -> dict:
    """Cookie-only copy for selected Grid GdUnifiedCampaign rows.

    This path is intentionally narrower than direct_copy.py: it handles draft Unified campaigns
    visible in Grid when v5 units are depleted, preserving campaign/group names, keywords, text ads,
    and adding product Shopping/Listing ads where the source had them.
    """
    source_login = (body.get("source_login") or "").strip()
    target_login = (body.get("target_login") or "").strip()
    selected_ids = {int(x) for x in (body.get("campaign_ids") or []) if str(x).isdigit()}
    counter_id = int(body.get("counter_id") or 0)
    goal_id = int(body.get("goal_id") or 0)
    target_domain = (body.get("target_domain") or "").strip()
    target_city = (body.get("target_city") or "").strip()
    target_region = (body.get("target_region") or "").strip()
    target_agency = body.get("agency") or _resolve_agency_hint(target_login, "")
    target_feed_id = _copy_target_feed_id(target_login, target_agency or "", workdir, target_domain)

    target_region = _copy_canonical_region_name(target_region)
    local_gid, local_geo_name = _copy_geo_id_for_target(target_city, target_region)
    if not local_gid:
        raise RuntimeError(f"не найден GeoRegionId для целевого гео: city={target_city!r}, region={target_region!r}")
    region_ids = [int(local_gid)]
    source_ctx = _copy_ctx(source_login)
    replacements = _copy_geo_replacements(source_ctx, target_city, target_region)
    src_domain = (source_ctx.get("domain") or "").strip()

    _copy_job_log(job_id, f"grid-cookie snapshot источника {source_login}: {len(selected_ids)} кампаний")
    snap = _copy_grid_read_selected(source_login, selected_ids)
    source_image_hashes = _copy_v501_ad_image_hashes(
        source_login,
        selected_ids,
        body.get("source_agency") or body.get("sourceAgency") or target_agency or "",
    )
    campaigns = [c for c in (snap.get("campaigns") or []) if str(c.get("__typename")) == "GdUnifiedCampaign"]
    if len(campaigns) != len(selected_ids):
        raise RuntimeError(f"grid snapshot неполный: выбрано {len(selected_ids)}, прочитано {len(campaigns)} Unified")
    if not src_domain:
        for camp in campaigns:
            src_domain = _copy_domain_from_href((camp.get("additionalData") or {}).get("href"))
            if src_domain:
                break
    if not src_domain:
        for ad in snap.get("ads") or []:
            src_domain = str(ad.get("domain") or "").strip().lower() or _copy_domain_from_href(ad.get("href"))
            if src_domain:
                break

    groups_by_campaign: dict[int, list[dict]] = {}
    for grp in snap.get("groups") or []:
        try:
            groups_by_campaign.setdefault(int(grp.get("campaign_id")), []).append(grp)
        except (TypeError, ValueError):
            continue
    ads_by_group: dict[int, list[dict]] = {}
    shopping_groups: set[int] = set()
    listing_groups: set[int] = set()
    for ad in snap.get("ads") or []:
        try:
            gid = int(ad.get("adGroupId") or 0)
        except (TypeError, ValueError):
            continue
        if gid <= 0:
            continue
        ads_by_group.setdefault(gid, []).append(ad)
        typ = str(ad.get("__typename") or "")
        if typ == "GdShoppingAd":
            shopping_groups.add(gid)
        elif typ == "GdListingAd":
            listing_groups.add(gid)

    results = []
    maps = {"campaigns": {}, "adgroups": {}, "ads": {}, "feeds": {}, "callouts": {}}
    if target_feed_id:
        maps["feeds"]["target"] = int(target_feed_id)

    for idx, camp in enumerate(campaigns, start=1):
        old_cid = int(camp["id"])
        old_name = str(camp.get("name") or "")
        new_name = _copy_normalize_campaign_name(old_name, replacements)
        base_href = _copy_target_href(((camp.get("additionalData") or {}).get("href")), src_domain, target_domain)
        src_groups = groups_by_campaign.get(old_cid) or []
        if not src_groups:
            results.append({"ok": False, "source_id": old_cid, "name": new_name, "error": "нет групп в Grid snapshot"})
            continue

        group_specs = []
        src_group_ids = []
        for grp in src_groups:
            gid = int(grp.get("adgroup_id") or 0)
            src_group_ids.append(gid)
            text_ads = [a for a in ads_by_group.get(gid, []) if str(a.get("__typename") or "") in ("GdTextAd", "GdAdaptiveTextAd")]
            titles: list[str] = []
            bodies: list[str] = []
            image_hashes: list[str] = list(source_image_hashes.get(gid) or [])
            href = base_href
            for ad in text_ads:
                if ad.get("href"):
                    href = _copy_target_href(ad.get("href"), src_domain, target_domain)
                image_hashes += _copy_grid_ad_image_hashes(ad)
                if ad.get("__typename") == "GdTextAd":
                    titles += [ad.get("title"), ad.get("titleExtension")]
                    bodies.append(ad.get("body"))
                else:
                    titles += list(ad.get("titles") or [])
                    bodies += list(ad.get("bodies") or [])
            titles = [_copy_apply_geo_replacements(t, replacements) for t in titles if str(t or "").strip()]
            bodies = [_copy_apply_geo_replacements(t, replacements) for t in bodies if str(t or "").strip()]
            group_specs.append({
                "name": _copy_apply_geo_replacements(grp.get("adgroup_name") or "группа", replacements),
                "keywords": [_copy_apply_geo_replacements(k, replacements) for k in (grp.get("keywords") or [])],
                "minus": list(grp.get("minus_keywords") or []),
                "titles": titles,
                "texts": bodies,
                "image_hashes": list(dict.fromkeys(h for h in image_hashes if h))[:5],
                "href": href,
                "brand": "Haval",
            })

        rep = gc.create_full(
            target_login,
            campaign_spec=_copy_grid_campaign_spec(new_name, counter_id, goal_id),
            groups=group_specs,
            region_ids=region_ids,
            href=base_href,
            goal_id=goal_id,
            autotargeting=True,
        )
        new_cid = rep.get("campaign_id")
        if not new_cid or rep.get("errors"):
            results.append({"ok": False, "source_id": old_cid, "name": new_name, "error": "; ".join(rep.get("errors") or ["не создана"])})
            _copy_job_log(job_id, f"grid-cookie {old_name}: ошибка {results[-1]['error'][:220]}")
            continue
        maps["campaigns"][str(old_cid)] = int(new_cid)
        for old_gid, new_gid in zip(src_group_ids, rep.get("adgroup_ids") or []):
            if new_gid:
                maps["adgroups"][str(old_gid)] = int(new_gid)

        shopping_added = 0
        listing_added = 0
        if target_feed_id:
            shop_items = []
            for old_gid in src_group_ids:
                new_gid = maps["adgroups"].get(str(old_gid))
                if new_gid and old_gid in shopping_groups:
                    shop_items.append({"adgroup_id": int(new_gid), "feed_id": int(target_feed_id), "vendor": "Haval"})
            if shop_items:
                grid = gf.GridClient(target_login)
                shop_ids = [int(x) for x in (grid.add_shopping_ads(shop_items) or []) if x]
                shopping_added = len(shop_ids)
                if shop_ids and any(g in listing_groups for g in src_group_ids):
                    listing_rows = grid.add_listing_ads_by_shopping_ads(shop_ids) or []
                    listing_added = len([x for x in listing_rows if (x.get("id") if isinstance(x, dict) else x)])

        results.append({
            "ok": True,
            "source_id": old_cid,
            "id": int(new_cid),
            "campaign_id": int(new_cid),
            "name": new_name,
            "result": {
                "build": {
                    "groups": int(rep.get("groups") or 0),
                    "ads": int(rep.get("ads") or 0),
                    "shopping_ads": shopping_added,
                    "listing_ads": listing_added,
                }
            },
        })
        _copy_job_upsert(job_id, progress=min(95, 10 + int(idx * 80 / max(1, len(campaigns)))))
        _copy_job_log(job_id, f"grid-cookie copied: {new_name} → {new_cid} ({idx}/{len(campaigns)})")

    _copy_write_json(workdir / "id_maps.json", maps)
    created_ids = [int(r["id"]) for r in results if r.get("ok") and r.get("id")]
    verify = {"status": "ok" if len(created_ids) == len(selected_ids) else "warning",
              "created": len(created_ids), "expected": len(selected_ids)}
    errors = [r for r in results if not r.get("ok")]
    return {
        "source_login": source_login,
        "target_login": target_login,
        "selected": len(selected_ids),
        "created": len(created_ids),
        "results": results,
        "errors": errors,
        "target_region_id": int(local_gid),
        "target_region_source": f"dict:{local_geo_name}",
        "target_feed_id": target_feed_id,
        "context_rewrite": {"replacements": len(replacements), "files": 0, "residual_geo": []},
        "live_verification": verify,
        "workdir": str(workdir),
        "uses_direct_units": False,
    }


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


def _copy_target_feed_id(target_login: str, target_agency: str, workdir: Path,
                         target_domain: str = "") -> int | None:
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
        wanted_key = _feed_key(_COPY_DEFAULT_FEED_PATH)
        wanted_domain = (target_domain or "").strip().lower()

        def _score(row: dict) -> tuple[int, int, int]:
            raw = " ".join(str(row.get(k) or "") for k in ("name", "url", "href", "source", "SourceUrl"))
            key = _feed_key(raw)
            low = raw.lower()
            return (
                1 if key == wanted_key else 0,
                1 if wanted_domain and wanted_domain in low else 0,
                1 if row.get("listings") else 0,
            )

        for row in sorted(rows, key=_score, reverse=True):
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
        selected_unified_rows = [
            r for r in selected_grid_rows
            if str(r.get("typename") or r.get("type") or "") == "GdUnifiedCampaign"
        ]
        if selected_unified_rows and len(selected_unified_rows) == len(selected_ids):
            _copy_job_log(job_id, f"grid-cookie copy: {len(selected_unified_rows)} Unified campaigns без Direct API баллов")
            grid_res = _copy_grid_unified_campaigns(job_id, body, selected_unified_rows, workdir)
            status = "done" if not grid_res.get("errors") else "error"
            _copy_job_upsert(
                job_id,
                status=status,
                progress=100,
                result=grid_res,
                error=("; ".join(str(e.get("error") or e) for e in (grid_res.get("errors") or [])[:3])[:500]
                       if grid_res.get("errors") else None),
            )
            return
        _copy_job_log(job_id, f"pull источника {source_login}")
        source_token, source_agency = _token_for_login(
            source_login, _resolve_agency_hint(source_login, ""), _direct_tokens()
        )
        source_cookie_account = source_agency or _resolve_agency_hint(source_login, "") or source_login
        src_auth = dc.find_working_auth(source_login, cookie_account=source_cookie_account)
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

        target_token, target_token_agency = _token_for_login(
            target_login, _resolve_agency_hint(target_login, ""), _direct_tokens()
        )
        target_cookie_account = (
            target_token_agency or _resolve_agency_hint(target_login, "") or body.get("agency") or target_login
        )
        tgt_auth = dc.find_working_auth(target_login, cookie_account=target_cookie_account)
        src_domain = dc.infer_source_domain(src_dir)
        tgt_region_id = None
        geo_source = ""
        target_region = _copy_canonical_region_name(target_region)
        if target_city or target_region:
            local_gid, local_geo_name = _copy_geo_id_for_target(target_city, target_region)
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
        token, _ag = target_token, target_token_agency
        metrika_res = {"updated": 0, "warned": 0}
        if token:
            _copy_job_log(job_id, f"докрутка Метрики: counter={counter_id}, goal={goal_id}")
            metrika_res = _copy_apply_metrika(target_login, token, src_dir, workdir, counter_id, goal_id, selected_ids, job_id)
        target_agency = _ag or _resolve_agency_hint(target_login, "")
        uac_copy = {"created": 0, "results": [], "errors": [], "uses_direct_units": False}
        if selected_uac_rows:
            target_feed_id = _copy_target_feed_id(target_login, target_agency or "", workdir, target_domain)
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

register_reference_routes(
    bp,
    _direct_access,
    list_feeds_for_site=cmc.list_feeds_for_site,
    load_json=_json,
    load_audiences=_load_audiences,
    victory_conn=_victory_conn,
)

register_overview_routes(
    bp,
    _direct_access,
    victory_conn=_victory_conn,
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
               "слепок_караваев": "karavaev"}
_SLEPOK_CANONICAL = {"pavlov", "kryuchkova", "scherbakova", "terehov", "karavaev"}


def _slepok_key_from_text(raw: str) -> str:
    """Best-effort: имя слепка/директолога из БД/UI → canonical ai_agents key."""
    s = (raw or "").strip().lower()
    if not s:
        return ""
    if s in _SLEPOK_KEY:
        return _SLEPOK_KEY[s]
    if s in {"pavlov", "kryuchkova", "scherbakova", "terehov", "karavaev"}:
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


def _m3_status_response():
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
            out["error"] = v5_err
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
        "_gc_ct": _gc_ct,
        "_grid_feeds": _grid_feeds,
        "_grid_list_campaigns": _grid_list_campaigns,
        "_is_site_domain_name": _is_site_domain_name,
        "_json": _json,
        "_segment_donor": _segment_donor,
        "_slepok_interest_for_struct": _slepok_interest_for_struct,
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

# Редактор контента (массовая коррекция AI-текстов) — изолированная страница
# /direct/automation/content + API /direct/api/content-editor/*. Доступ: как у остального
# Direct (work/work:direct), плюс отдельные ключи direct:content/direct на будущее; админ проходит.
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


register_copy_routes(
    bp,
    _direct_access,
    api_campaigns_func=_campaigns_response,
    account_prefill_func=_account_prefill_response,
    metrika_goals_for=_metrika_goals_for,
    parse_number=_num,
    copy_default_feed_path=_COPY_DEFAULT_FEED_PATH,
    counter_foreign_owner=_counter_foreign_owner,
    resolve_agency_hint=_resolve_agency_hint,
    ensure_create_worker=_ensure_create_worker,
    job_new=_job_new,
    copy_job_upsert=_copy_job_upsert,
    create_jobs_ahead=_create_jobs_ahead,
    create_jobs=_CREATE_JOBS,
    create_jobs_lock=_CREATE_JOBS_LOCK,
    copy_jobs=_COPY_JOBS,
    copy_jobs_lock=_COPY_JOBS_LOCK,
)


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
    create_queue=_CREATE_QUEUE,
    job_terminal=_JOB_TERMINAL,
    job_db_last=_JOB_DB_LAST,
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
    return _create_set_feeds_module()._cached_upload_image(*args, **kwargs)

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
        "_account_offer_urls": _account_offer_urls,
        "_ag_part1_map": _ag_part1_map,
        "_brand_level_url": _brand_level_url,
        "_cached_upload_image": _cached_upload_image,
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
    }


def _create_set_text_builder_module():
    from . import create_set_text_builders as cstb
    cstb.configure(_create_set_text_builder_deps())
    return cstb


def _build_tp2_adgroups(*args, **kwargs):
    return _create_set_text_builder_module()._build_tp2_adgroups(*args, **kwargs)

def _struct_cts(*args, **kwargs):
    return _create_set_text_builder_module()._struct_cts(*args, **kwargs)

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
        "_apply_corrections": _apply_corrections,
        "_brand_level_url": _brand_level_url,
        "_cached_upload_image": _cached_upload_image,
        "_chunks": _chunks,
        "_coherent_payments": _coherent_payments,
        "_creative_images_for_ct": _creative_images_for_ct,
        "_ct_segment": _ct_segment,
        "_enabled_minus_places": _enabled_minus_places,
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

def _search_platforms(*args, **kwargs):
    return _create_set_finalize_module()._search_platforms(*args, **kwargs)


def _finalize_rsya(*args, **kwargs):
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
    return _create_set_finalize_module()._finalize_search_via_grid(*args, **kwargs)


def _add_listing_ads_v501(*args, **kwargs):
    return _create_set_tp1_builder_module()._add_listing_ads_v501(*args, **kwargs)


def _create_tp1_single(*args, **kwargs):
    return _create_set_tp1_builder_module()._create_tp1_single(*args, **kwargs)


def _create_tp1_campaign(*args, **kwargs):
    return _create_set_tp1_builder_module()._create_tp1_campaign(*args, **kwargs)


def _grid_account_image_hashes(*args, **kwargs):
    return _create_set_tp1_builder_module()._grid_account_image_hashes(*args, **kwargs)


def _tp1_pack_groups(*args, **kwargs):
    return _create_set_tp1_builder_module()._tp1_pack_groups(*args, **kwargs)


def _pack_groups_with_retry(*args, **kwargs):
    return _create_set_tp1_builder_module()._pack_groups_with_retry(*args, **kwargs)


def _create_tp1_via_cookie(*args, **kwargs):
    return _create_set_tp1_builder_module()._create_tp1_via_cookie(*args, **kwargs)


def _create_set_feed_builder_deps() -> dict:
    return {
        "_SLEPOK_MINUS_MODE": _SLEPOK_MINUS_MODE,
        "_account_model_feeds": _account_model_feeds,
        "_account_offer_prices": _account_offer_prices,
        "_add_job_err": _add_job_err,
        "_add_listing_ads_v501": _add_listing_ads_v501,
        "_ai_common_sitelinks": _ai_common_sitelinks,
        "_allowed_feed_keys": _allowed_feed_keys,
        "_apply_corrections": _apply_corrections,
        "_build_tp1_from_pack": _build_tp1_from_pack,
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


def _master_product_deps() -> dict:
    names = [
        "_BU_RE", "_GENERIC_AT_TITLES", "_GENERIC_SITELINK_FILLERS", "_GENERIC_TEXT_FILLERS",
        "_GENERIC_TITLE_FILLERS", "_SLEPOK_KEY", "_TP67_MIN_TEXT_LEN", "_TP67_OPTIMAL_CATEGORIES",
        "_TP67_RELEVANCE_CATEGORIES", "_account_model_feeds", "_add_job_err", "_audience_objects",
        "_bad_ad_sitelink", "_bad_ad_text", "_bad_ad_title", "_brand_ct_from_coder", "_brand_title_set",
        "_build_name", "_bump_item", "_bump_job", "_cached_campaign_content", "_catalog_feed",
        "_coherent_discounts", "_coherent_payments", "_creative_images_for_ct", "_dedup_prefix_absorb",
        "_discount_pcts", "_diverse_text_offers", "_drop_used_car", "_fallback_master_titles",
        "_fill_title", "_fill_variants", "_has_number", "_image_ct_for_content", "_is_bad_start",
        "_is_bu_site", "_is_common_ct", "_is_site_domain_name", "_job_db_progress", "_lines",
        "_match_collection", "_num", "_own_brand_tokens", "_replace_emdash", "_replace_foreign_city",
        "_replace_sep_hyphen", "_resolve_region", "_rsya_texts", "_rsya_titles", "_sanitize_content",
        "_sitelink_has_pct", "_slepok_audiences_for", "_slepok_campaign_content", "_strip_credit_rate",
        "_title2_blocklist", "_tp67_keywords_for", "_tp67_targeting_mode", "_tp7_product_feed_filters",
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
        "_create_tp1_campaign", "_create_tp1_via_cookie", "_create_tp3_campaign", "_create_tp5_campaign",
        "_dedup_callouts", "_deferred_save", "_deferred_set_status", "_first_url_feed",
        "_get_or_create_minus_set", "_goal_vse_formy", "_grid_list_campaigns", "_ints", "_job_db_progress",
        "_lines", "_load_corrections", "_metrika_goals_for", "_next_units_reset_utc", "_normalize_callout_text",
        "_num", "_preflight_creds", "_promo_content_lines", "_promo_usable_for_content",
        "_pull_begin", "_pull_end", "_repair_deps", "_resolve_region", "_rotated_content_window",
        "_rule_sets", "_run_master_product_item", "_selected_slepok_key", "_slepok_content_get",
        "_slepok_uses_shopping", "_templates_for",
        "_units_in_result", "_v5_get",
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
        "_valid_pack_brand_name": _valid_pack_brand_name,
        "_vendor_value": _vendor_value,
        "gc": gc,
        "rauto": rauto,
        "rex": rex,
        "rgate": rgate,
        "vsvc": vsvc,
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


def _delete_uac_repair_campaigns(*args, **kwargs):
    return _create_set_repairing_module()._delete_uac_repair_campaigns(*args, **kwargs)


def _delete_search_draft_campaigns(*args, **kwargs):
    return _create_set_repairing_module()._delete_search_draft_campaigns(*args, **kwargs)


def _queue_recreate_repair_job(*args, **kwargs):
    return _create_set_repairing_module()._queue_recreate_repair_job(*args, **kwargs)


def _auto_queue_recreate_after_done(*args, **kwargs):
    return _create_set_repairing_module()._auto_queue_recreate_after_done(*args, **kwargs)


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

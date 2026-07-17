"""Копирование кампаний Яндекс.Директа 1:1 (обёртки-оркестрация поверх внешнего движка
work/slepki_direktologov/scripts/direct_copy.py) — вынесено из blueprint.py.

Инвариант wiring-hub: НЕ импортирует blueprint. Direct API/токены/Grid-обёртки/очередь-хелперы
инъектятся через configure(deps). Sibling-модули (campaign, grid_create, grid_finalize,
llm_providers) — прямой импорт (цикла нет). copy_geo_morph/copy_steps — ленивые внутри функций.
Прогресс копирования зеркалится в ОБЩУЮ create-очередь (_CREATE_JOBS, инъектится тем же объектом).
"""
from __future__ import annotations

import importlib.util
import json
import re
import tempfile
import threading
import time
from pathlib import Path

from . import campaign as cmc
from . import grid_create as gc
from . import grid_finalize as gf
from . import repair_auto as rauto
from . import repair_gate as rgate
from .llm_providers import _m3_complete, _m3_llm_probe

_HERE = Path(__file__).resolve().parent

# ── DI из blueprint (инъектятся configure; None до инъекции — заглушки для статики) ──
_v5_call = _v501_svc = _v5_err = _token_for_login = _direct_tokens = None
_resolve_agency_hint = _victory_conn_rw = None
_resolve_region = None   # город → (r_code, oblast); ремап r-сегмента кодера при копировании
_grid_list_campaigns = _grid_feeds = _grid_feed_offer_prices = _group_ad_price = None
_grid_set_ad_prices = _grid_update_adaptive_ads = _account_offer_prices = _account_ctx = None
_geo_id = _geo_name_by_id = _geo_type_by_id = _enabled_minus_places = _filter_allowed_feed_rows = _feed_key = None
_enabled_global_minus_places = None   # copy = клон 1:1 без слепка → глобальная таблица минус-площадок (legacy)
_enabled_baseline_minus_places = None   # copy = клон 1:1 без слепка → baseline анти-фрод список минус-площадок
_create_set_live_verification = _attach_post_repair_verification = _repair_deps = None
_CREATE_JOBS = _CREATE_JOBS_LOCK = _JOB_TERMINAL = None   # общие ОБЪЕКТЫ (mirror в create-карточку)
_job_touch = _job_db_save = _CALLOUT_PER_CAMPAIGN_CAP = None


def configure(deps: dict) -> None:
    """Инъекция зависимостей из blueprint (Direct API/токены/Grid/очередь)."""
    globals().update(deps)


_COPY_JOBS: dict = {}
_COPY_JOBS_LOCK = threading.Lock()
_DIRECT_COPY_MOD = None


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
    # Страновой таргетинг: РФ/Россия/Russia → GeoRegionId России (225). Мультигород-аккаунты
    # префилл сводит к 225 (account_service.py:623, «регион не распознан по городу → Россия»),
    # но текст региона уходит как «РФ»/«Россия» — резолвим его тем же id, иначе city-мультистрока
    # («Краснодар, Нижний Новгород, …») не матчится и копирование падает (не найден GeoRegionId).
    if (region_name or "").strip().lower().replace("ё", "е") in {
        "рф", "россия", "russia", "ru", "российская федерация",
    }:
        return 225, "Россия"
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


def _copy_m3_decliner():
    """Callable для copy_geo_morph: messages -> (text, err). temperature=0, короткий таймаут,
    2 попытки (склонение — быстрая задача, долго ждать M3 в copy-job'е не нужно)."""
    def _call(messages):
        return _m3_complete(messages, max_tokens=220, temperature=0.0, tries=2, backoff=4.0, timeout=60)
    return _call


def _copy_build_geo(source_ctx: dict, target_city: str, target_region: str, log=None):
    """Строит морфологические пары гео-замены (все падежи) через M3 + метадату.

    Возвращает (pairs, meta). pairs — list[(old, new)] по падежам, отсортировано по длине убыв.
    meta — {m3_used, m3_failed, source_forms (для residual), pairs_count}. Фолбзк на именительный
    (по границам слов) внутри copy_geo_morph, если M3 недоступен/невалиден — job не падает."""
    from . import copy_geo_morph as cgm
    target_city = (target_city or "").strip()
    target_region = (target_region or "").strip()
    src_city = (source_ctx.get("city") or "").strip()
    src_region = (source_ctx.get("region") or "").strip()
    geo_map: list[tuple[str, str]] = []
    if src_city:
        geo_map.append((src_city, target_city or target_region))
    if src_region:
        geo_map.append((src_region, target_region or target_city))
    # Частый случай: источник вне local_gsheet_sites, но в названиях/текстах реально фигурирует Краснодар.
    if "краснодар" not in f"{target_city} {target_region}".lower():
        geo_map.append(("Краснодар", target_city or target_region))
        geo_map.append(("Краснодарский край", target_region or target_city))
    _log = log or (lambda _m: None)
    # Пробуем M3 только если LLM жива (иначе paradigm_for отдаст закэшированное, а несозданное — фолбэк).
    try:
        m3 = _copy_m3_decliner() if _m3_llm_probe() else None
    except Exception:  # noqa: BLE001
        m3 = None
    return cgm.build_geo_pairs(geo_map, m3_complete=m3, log=_log)


def _copy_geo_replacements(source_ctx: dict, target_city: str, target_region: str, log=None) -> list[tuple[str, str]]:
    pairs, _meta = _copy_build_geo(source_ctx, target_city, target_region, log=log)
    return pairs


def _copy_apply_geo_replacements(text: str | None, replacements: list[tuple[str, str]]) -> str:
    from . import copy_geo_morph as cgm
    out, _n = cgm.apply_replacements(text, replacements or [])
    return out


_COPY_R_CODE_RE = re.compile(r"(?<=_)r\d{4}(?=_)")


def _copy_target_region_code(target_city: str, target_region: str) -> str:
    """Целевой r-код кодера (ag_part4) по гео target-аккаунта. Один источник для кампаний и групп.

    Использует DI'd _resolve_region(city) -> (r_code, oblast) (create_set_plan). Возвращает валидный
    r#### ТОЛЬКО если он определён и не плейсхолдер r0000 — иначе '' (ремап пропускается, чтобы не
    затирать исходный код неопределённым плейсхолдером). None-DI (standalone без wiring) → ''."""
    if not _resolve_region:
        return ""
    for probe in (target_city, target_region):
        probe = (probe or "").strip()
        if not probe:
            continue
        try:
            r_code, _oblast = _resolve_region(probe)
        except Exception:  # noqa: BLE001 — резолв региона best-effort, ремап не критичен для create
            r_code = ""
        r_code = str(r_code or "").strip()
        if re.fullmatch(r"r\d{4}", r_code) and r_code != "r0000":
            return r_code
    return ""


def _copy_rcode_to_region(r_code: str) -> str:
    """Обратный резолв: r-код кодера → область словами (public.local_gsheet_naming, type='ag_part4').

    Best-effort: при ошибке / неизвестном коде / отсутствии БД-инъекции → пустая строка.
    Используется в гео-фолбэке grid-cookie ветки, когда source не в local_gsheet_sites."""
    if not r_code or not re.fullmatch(r"r\d{4}", str(r_code or "")) or r_code == "r0000":
        return ""
    if not _victory_conn_rw:
        return ""
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM public.local_gsheet_naming "
                        "WHERE type='ag_part4' AND code=%s LIMIT 1", (r_code,))
            row = cur.fetchone()
            return str(row[0]).strip() if row and row[0] else ""
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return ""


def _copy_remap_region_code(name: str | None, target_r_code: str) -> str:
    """Перекодировать r-сегмент кодера (`_r0300_` → `_<target_r_code>_`) в имени кампании/группы.

    Баги 1/4: гео-морфология меняет только словоформы (\\b), но регион в кодере зашит КОДОМ
    (`ag_part4`, напр. r0300=Краснодарский край) — код словами не задеть. Здесь ремапим сам код на
    r-код target-региона. target_r_code пуст/невалиден → имя без изменений (безопасно)."""
    text = str(name or "")
    if not text or not re.fullmatch(r"r\d{4}", str(target_r_code or "")):
        return text
    return _COPY_R_CODE_RE.sub(target_r_code, text)


def _copy_normalize_campaign_name(name: str | None, replacements: list[tuple[str, str]],
                                  target_r_code: str = "") -> str:
    out = _copy_apply_geo_replacements(name, replacements).strip()
    out = re.sub(r"^\s*Копия\s+ХАВАЛ\s+", "Haval ", out, flags=re.I)
    out = re.sub(r"^\s*Копия\s+", "", out, flags=re.I)
    out = out.replace("Башкортостан, республика", "Республика Башкортостан")
    out = out.replace("ХАВАЛ", "Haval")
    # Баг 4: r-сегмент кодера в ИМЕНИ кампании (tp6/tp7 — весь кодер в имени) → target r-код.
    out = _copy_remap_region_code(out, target_r_code)
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


def _copy_image_remapper(source_login: str, source_agency: str, target_login: str,
                         target_agency: str, all_source_hashes, maps: dict, workdir: Path,
                         *, log=lambda m: None, provided_hashes: list | None = None):
    """Build ``fn(src_hashes) -> [target-valid image hashes]`` для ЕПК-ветки копировщика (по кукам).

    Image-хэши в Яндекс.Директе привязаны к АККАУНТУ: source-хэш валиден в target только если такая
    же картинка уже загружена в target (контент-хэш совпал). Иначе AddAdaptiveTextAds падает
    ``BannerDefectIds.Gen.IMAGE_NOT_FOUND`` и роняет ВЕСЬ ad-add кампании (живой инцидент job
    b344eafcdad8: src 712117605/712117626 → 2 битые оболочки).

    Стратегия (п.12 «картинки 1:1», 0 v5-баллов):
      • хэш уже есть в target (v501 ``adimages.get`` target) → используем как есть;
      • иначе скачиваем оригинал источника (v501 ``adimages.get`` source → ``OriginalUrl``, публичный
        avatars-URL) и ПЕРЕАПЛОАДИМ в target по кукам (``gf.GridClient.upload_image`` →
        web-api/image/upload, 0 баллов) → target-хэш, кэшируем в ``maps['images']`` (src→tgt);
      • картинку не удалось скачать/залить → ДРОПАЕМ этот хэш (лог), НЕ роняем ad-add
        (объявление без 1 картинки лучше, чем падение всей кампании).

    mode="other" + provided_hashes: вместо ремапа из источника подставляем загруженные хэши
    ПО КРУГУ (вызов i → hash[i % len(hashes)], детерминировано по порядку вызовов).
    """
    # mode="other": предзагруженные хэши уже в target-аккаунте → round-robin по вызовам.
    if provided_hashes:
        _ph = [str(h).strip() for h in provided_hashes if str(h).strip()]
        if _ph:
            _counter = [0]

            def _remap_provided(src_hashes):  # noqa: ARG001 — src_hashes игнорируется
                idx = _counter[0]
                _counter[0] += 1
                return [_ph[idx % len(_ph)]]

            return _remap_provided

    import requests as _rqs
    maps.setdefault("images", {})
    img_cache = maps["images"]  # src_hash -> tgt_hash (persist across all campaigns of the job)

    # 1) существующие хэши target — их можно ставить как есть (1:1, без переаплоада).
    target_hashes: set[str] = set()
    try:
        tgt_token, _ = _token_for_login(
            target_login, target_agency or _resolve_agency_hint(target_login, ""), _direct_tokens())
    except Exception:  # noqa: BLE001
        tgt_token = None
    if tgt_token:
        data = _v501_svc("adimages", "get", tgt_token, target_login,
                         {"SelectionCriteria": {}, "FieldNames": ["AdImageHash"]})
        for im in ((data.get("result") or {}).get("AdImages") or []):
            h = str(im.get("AdImageHash") or "").strip()
            if h:
                target_hashes.add(h)

    # 2) OriginalUrl источника для хэшей, которых НЕТ в target (кандидаты на переаплоад).
    need = [h for h in {str(x).strip() for x in (all_source_hashes or []) if str(x).strip()}
            if h not in target_hashes]
    src_url_by_hash: dict[str, str] = {}
    if need:
        try:
            src_token, _ = _token_for_login(
                source_login, source_agency or _resolve_agency_hint(source_login, ""), _direct_tokens())
        except Exception:  # noqa: BLE001
            src_token = None
        if src_token:
            for i in range(0, len(need), 100):
                data = _v501_svc("adimages", "get", src_token, source_login,
                                 {"SelectionCriteria": {"AdImageHashes": need[i:i + 100]},
                                  "FieldNames": ["AdImageHash", "OriginalUrl"]})
                for im in ((data.get("result") or {}).get("AdImages") or []):
                    h = str(im.get("AdImageHash") or "").strip()
                    u = str(im.get("OriginalUrl") or "").strip()
                    if h and u:
                        src_url_by_hash[h] = u
        log(f"картинки: target уже имеет {len(target_hashes)} хэшей, к переаплоаду {len(need)} "
            f"(получено URL источника: {len(src_url_by_hash)})")

    cache_dir = Path(workdir) / "_image_cache"
    tgt_grid_holder: dict = {}

    def _tgt_grid():
        if "cli" not in tgt_grid_holder:
            tgt_grid_holder["cli"] = gf.GridClient(target_login)
        return tgt_grid_holder["cli"]

    def _remap(src_hashes):
        out: list[str] = []
        for h in [str(x).strip() for x in (src_hashes or []) if str(x).strip()]:
            if h in target_hashes:                 # уже валиден в target — 1:1 без переаплоада
                out.append(h)
                continue
            if h in img_cache:                     # уже переаплоадили ранее в этом job
                out.append(img_cache[h])
                continue
            url = src_url_by_hash.get(h)
            if not url:
                log(f"картинка {h[:12]}…: нет OriginalUrl источника — дроп (ad-add не падает)")
                continue
            cache_dir.mkdir(parents=True, exist_ok=True)
            dst = cache_dir / f"{h}.img"
            try:
                if not (dst.exists() and dst.stat().st_size > 0):
                    with _rqs.get(url, stream=True, timeout=60, verify=False) as r:
                        if r.status_code != 200:
                            log(f"картинка {h[:12]}…: скачивание HTTP {r.status_code} — дроп")
                            continue
                        with open(dst, "wb") as fh:
                            for chunk in r.iter_content(chunk_size=1 << 16):
                                if chunk:
                                    fh.write(chunk)
                if dst.stat().st_size <= 0:
                    log(f"картинка {h[:12]}…: пустой файл — дроп")
                    continue
            except Exception as e:  # noqa: BLE001
                log(f"картинка {h[:12]}…: скачивание не удалось ({str(e)[:120]}) — дроп")
                continue
            try:
                tgt_hash = _tgt_grid().upload_image(str(dst))
            except Exception as e:  # noqa: BLE001
                log(f"картинка {h[:12]}…: переаплоад в target не удался ({str(e)[:120]}) — дроп")
                tgt_hash = None
            if tgt_hash:
                img_cache[h] = tgt_hash
                target_hashes.add(tgt_hash)
                out.append(tgt_hash)
            else:
                log(f"картинка {h[:12]}…: upload_image вернул пусто — дроп")
        return list(dict.fromkeys(out))

    return _remap


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


def _copy_rewrite_snapshot_context(src_dir: Path, source_ctx: dict, target_ctx: dict, log=None) -> dict:
    """Replace source geo words in copied payloads before upload — морфологически (по падежам).

    Пары строит _copy_build_geo (M3-парадигма 6 падежей для старого и нового города/области),
    замена — copy_geo_morph.apply_replacements: ПО ГРАНИЦАМ СЛОВ + сохранение регистра.
    Так «в Краснодаре»→«в Уфе», «Краснодара»→«Уфы», а не «Уфае/Уфаа». Residual — по ВСЕМ падежам."""
    from . import copy_geo_morph as cgm
    target_city = (target_ctx.get("city") or "").strip()
    target_region = (target_ctx.get("region") or "").strip()
    pairs, geo_meta = _copy_build_geo(source_ctx, target_city, target_region, log=log)

    if not pairs:
        return {"files": 0, "replacements": 0, "pairs": [], "m3_used": False, "residual_geo": []}

    changed_files = 0
    changed_count = 0

    for name in _COPY_JSON_PAYLOADS:
        path = src_dir / name
        if not path.exists():
            continue
        before = path.read_text(encoding="utf-8")
        data = json.loads(before)
        cnt = {"n": 0}

        def _repl(s, _c=cnt):
            out, n = cgm.apply_replacements(s, pairs)
            _c["n"] += n
            return out

        data = _copy_walk_strings(data, _repl)
        after = json.dumps(data, ensure_ascii=False, indent=1)
        if after != before:
            path.write_text(after, encoding="utf-8")
            changed_files += 1
        changed_count += cnt["n"]

    # Residual (case-aware): любая падежная форма старого гео, кроме форм, входящих в новое гео.
    paths_texts: list[tuple[str, str]] = []
    for name in _COPY_JSON_PAYLOADS:
        p = src_dir / name
        if p.exists():
            try:
                paths_texts.append((name, p.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                pass
    residual = cgm.scan_residual(
        paths_texts, geo_meta.get("source_forms") or [],
        target_text=f"{target_city} {target_region}",
    )
    return {
        "files": changed_files,
        "replacements": changed_count,
        "pairs": pairs,
        "m3_used": bool(geo_meta.get("m3_used")),
        "m3_failed": geo_meta.get("m3_failed") or [],
        "residual_geo": residual,
    }


def _copy_target_href(href: str | None, source_domain: str, target_domain: str) -> str:
    """Доменно-агностичная трансформация URL объявления/ссылки в целевой домен (баг 3).

    Раньше: наивный ``href.replace(source_domain, target)`` по ОДНОМУ инференс-домену — если href
    указывал на ДРУГОЙ хост (quiz-поддомен, турбо-страница, яндексовая «Подборка», маркетплейс), он
    уезжал в target без замены. Теперь заменяем ЛЮБОЙ хост, не равный target, на target-хост:
      • хост источника или его ПОДДОМЕН (тот же бизнес, сменил домен) → target-хост + path/query/fragment;
      • ЧУЖОЙ хост (Яндекс-«Подборка»/турбо/маркетплейс — path на клиентском домене не существует) →
        голый target (без мусорного пути, иначе 404).
    Относительный URL (без хоста) и пустой target — не трогаем."""
    from urllib.parse import urlsplit, urlunsplit
    href = str(href or "").strip()
    target = str(target_domain or "").strip()
    # target-хост: срезаем возможную схему/путь, берём чистый netloc.
    t_split = urlsplit(target if "://" in target else "https://" + target)
    target_host = (t_split.netloc or t_split.path.strip("/").split("/", 1)[0]).strip().strip("/")
    target_abs = ("https://" + target_host) if target_host else ""
    if not href:
        return target_abs
    if not target_host:
        return href
    parts = urlsplit(href)
    if not parts.netloc:            # относительный URL (нет хоста) — оставляем как есть
        return href
    host = parts.netloc.lower()
    if host == target_host.lower():
        return href                 # уже целевой хост
    scheme = parts.scheme or "https"
    src = str(source_domain or "").strip().lower().lstrip(".")
    # свой домен/поддомен источника → перенос пути; чужой (подборка/турбо/маркетплейс) → голый target.
    same_business = bool(src) and (host == src or host.endswith("." + src))
    if src and not same_business:
        return target_abs
    return urlunsplit((scheme, target_host, parts.path, parts.query, parts.fragment))


def _copy_snapshot_preflight(src_dir: Path, *, target_feed_url: str, target_city: str, target_region: str,
                             geo_mode: str = "") -> dict:
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
    if not target_geo and geo_mode != "keep":
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
        "minusKeywords disabledPlaces strategy{budget{sum}}}}}}"
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


def _copy_grid_campaign_spec(name: str, counter_id: int, goal_id: int,
                              weekly_budget: int = 7000) -> dict:
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
        "weekly_budget": int(weekly_budget) if weekly_budget and int(weekly_budget) > 0 else 7000,
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
    from .copy_steps import _clean_group_brand as _csteps_clean_group_brand
    source_login = (body.get("source_login") or "").strip()
    target_login = (body.get("target_login") or "").strip()
    selected_ids = {int(x) for x in (body.get("campaign_ids") or []) if str(x).isdigit()}
    counter_id = int(body.get("counter_id") or 0)
    goal_id = int(body.get("goal_id") or 0)
    target_domain = (body.get("target_domain") or "").strip()
    target_city = (body.get("target_city") or "").strip()
    target_region = (body.get("target_region") or "").strip()
    target_agency = body.get("agency") or _resolve_agency_hint(target_login, "")
    # ДОРАБОТКА 1: feed_map (пофидовая замена) в ЕПК-ветке. Раньше брался ОДИН авто-фид
    # (_copy_target_feed_id, feed_map игнорировался). Теперь: если body.feed_map задан и валиден
    # (те же проверки, что в _copy_run_job — целевой фид ПРИНАДЛЕЖИТ target-аккаунту), используем
    # целевой фид ИЗ карты для shopping/listing. Общий кейс «все source-фиды → один target-фид» —
    # берём этот единый target feed_id. feed_map пуст/невалиден → прежнее поведение.
    feed_map_valid = _copy_grid_validate_feed_map(
        target_login, target_agency or "", body, log=(lambda m: _copy_job_log(job_id, m)))
    feed_map_targets = list(dict.fromkeys(int(v) for v in feed_map_valid.values()))
    if feed_map_targets:
        target_feed_id = feed_map_targets[0]
        _copy_job_log(job_id, f"feed_map активен: целевые фиды {feed_map_targets}, "
                              f"shopping/listing → {target_feed_id}")
    else:
        target_feed_id = _copy_target_feed_id(target_login, target_agency or "", workdir, target_domain)

    mode = (body.get("mode") or "auto").strip()
    geo_mode = (body.get("geo_mode") or "replace").strip()
    provided_hashes = [str(h).strip() for h in (body.get("image_hashes") or []) if str(h).strip()]

    if mode == "other" and geo_mode == "keep":
        # Ветка (а): RegionIds — из групп источника как есть (узнаем после snap-чтения).
        # Ветка (б): гео-морфология — пропускаем.
        # Ветка (в): r-код кодера — не ремапим.
        local_gid = None
        local_geo_name = ""
        region_ids = []         # будет заполнено из первой непустой группы источника
        target_r_code = ""
        replacements = []
        source_ctx: dict = {}
        _copy_job_log(job_id, "гео: режим 'keep' — гео-замена и ремап RegionIds пропущены")
    elif mode == "other" and geo_mode == "change":
        # Новый контракт: geo_region_ids — список (положительные = включения, отрицательные = исключения).
        # Обратная совместимость: если список не задан, берём скалярный geo_region_id.
        _geo_ids_raw = body.get("geo_region_ids") or []
        if not _geo_ids_raw:
            _scalar = int(body.get("geo_region_id") or 0)
            _geo_ids_raw = [_scalar] if _scalar else []
        geo_region_ids = [int(x) for x in _geo_ids_raw
                          if str(x).lstrip("-").isdigit() and int(x) != 0]
        if not geo_region_ids:
            raise RuntimeError("mode='other', geo_mode='change': geo_region_ids не задан в запросе")
        positive_ids = [x for x in geo_region_ids if x > 0]
        negative_ids = [x for x in geo_region_ids if x < 0]
        local_gid = positive_ids[0] if positive_ids else None
        region_ids = geo_region_ids   # содержит и плюсы, и минусы
        target_r_code = ""  # r-код не ремапим для «Прочие сферы»
        source_ctx = _copy_ctx(source_login)
        # Морфология текстов: ТОЛЬКО если ровно 1 плюс-регион, нет исключений, тип НЕ World/Country.
        _morph_type = (_geo_type_by_id(positive_ids[0]) if _geo_type_by_id and positive_ids else None) or ""
        _do_morph = (len(positive_ids) == 1 and len(negative_ids) == 0
                     and _morph_type not in ("World", "Country"))
        if _do_morph:
            # Имя региона резолвим на сервере из справочника GeoRegions — не доверяем клиенту.
            geo_region_name_str = (_geo_name_by_id(positive_ids[0]) if _geo_name_by_id else "") or ""
            if not geo_region_name_str:
                raise RuntimeError(
                    f"geo_region_id={positive_ids[0]}: имя региона не найдено в справочнике GeoRegions"
                )
            replacements = _copy_geo_replacements(
                source_ctx, "", geo_region_name_str, log=(lambda m: _copy_job_log(job_id, m))
            )
            _copy_job_log(job_id, f"гео: 1 регион, меняем тексты: region_id={positive_ids[0]} name={geo_region_name_str!r}")
        else:
            replacements = []
            _copy_job_log(job_id,
                          f"гео: {len(positive_ids)} регион(ов), {len(negative_ids)} исключений"
                          f" → тексты не меняем (RegionIds ставим)")
        _copy_job_log(job_id, f"гео: режим 'change', region_ids={geo_region_ids[:10]!r}")
    else:
        target_region = _copy_canonical_region_name(target_region)
        local_gid, local_geo_name = _copy_geo_id_for_target(target_city, target_region)
        if not local_gid:
            raise RuntimeError(f"не найден GeoRegionId для целевого гео: city={target_city!r}, region={target_region!r}")
        region_ids = [int(local_gid)]
        # Баги 1/4: r-код target-региона для ремапа кодера (один источник — имена кампаний И групп).
        target_r_code = _copy_target_region_code(target_city, target_region)
        if target_r_code:
            _copy_job_log(job_id, f"кодер: r-сегмент региона → {target_r_code}")
        source_ctx = _copy_ctx(source_login)
        # Баг 1 фолбэк: source не в local_gsheet_sites → source_ctx пуст → replacements=[].
        # Резолвим источник по r-коду из имени source-кампании и добавляем регион в source_ctx.
        if not (source_ctx.get("city") or source_ctx.get("region")):
            _src_r_code = ""
            for _sr in selected_grid_rows:
                _m = _COPY_R_CODE_RE.search(str(_sr.get("name") or ""))
                if _m:
                    _src_r_code = _m.group()
                    break
            if _src_r_code and _src_r_code != target_r_code:
                _src_oblast = _copy_rcode_to_region(_src_r_code)
                if _src_oblast:
                    source_ctx = dict(source_ctx) if source_ctx else {}
                    source_ctx["region"] = _src_oblast
                    _copy_job_log(job_id,
                                  f"гео-фолбэк: {_src_r_code} → {_src_oblast!r} (source не в local_gsheet_sites)")
        replacements = _copy_geo_replacements(
            source_ctx, target_city, target_region, log=(lambda m: _copy_job_log(job_id, m))
        )
    src_domain = (source_ctx.get("domain") or "").strip()

    _copy_job_log(job_id, f"grid-cookie snapshot источника {source_login}: {len(selected_ids)} кампаний")
    try:
        snap = _copy_grid_read_selected(source_login, selected_ids)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"grid snapshot не получен ({source_login}): {str(e)[:220]}") from e
    try:
        source_image_hashes = _copy_v501_ad_image_hashes(
            source_login,
            selected_ids,
            body.get("source_agency") or body.get("sourceAgency") or target_agency or "",
        )
    except Exception as e:  # noqa: BLE001 — v501 image-хэши best-effort: картинки доберём из grid-ads
        _copy_job_log(job_id, f"v501 image-хэши источника не получены ({str(e)[:180]}) — продолжаю без них")
        source_image_hashes = {}
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

    # geo_mode="keep": region_ids из первой непустой группы источника (best-effort для UAC-ветки).
    if geo_mode == "keep" and not region_ids:
        for _grp in (snap.get("groups") or []):
            _gids = [int(x) for x in (_grp.get("region_ids") or []) if str(x).lstrip("-").isdigit()]
            if _gids:
                region_ids = _gids
                _copy_job_log(job_id, f"гео keep: RegionIds из группы источника: {region_ids}")
                break
        if not region_ids:
            region_ids = [225]   # Россия — последний резерв
            _copy_job_log(job_id, "гео keep: RegionIds источника не найдены → [225] (Россия)")

    results = []
    maps = {"campaigns": {}, "adgroups": {}, "ads": {}, "feeds": {}, "callouts": {},
            "images": {}, "promotions": {}, "sitelinks": {}}
    # feed_map: заносим ВСЕ выбранные target-фиды в maps["feeds"] (step_prices читает их значения).
    for _sid, _tid in (feed_map_valid or {}).items():
        maps["feeds"][str(_sid)] = int(_tid)
    if target_feed_id:
        maps["feeds"]["target"] = int(target_feed_id)
    # ФИКС IMAGE_NOT_FOUND: image-хэши источника account-scoped → в target валидны только если
    # такая же картинка уже там. Ремаппер: as-is если хэш есть в target, иначе скачать оригинал
    # источника (OriginalUrl) и переаплоадить в target по кукам (0 баллов); недоступную — дропнуть.
    _all_src_hashes: set[str] = set()
    for _hs in (source_image_hashes or {}).values():
        _all_src_hashes.update(_hs or [])
    for _ad in (snap.get("ads") or []):
        _all_src_hashes.update(_copy_grid_ad_image_hashes(_ad))
    _remap_images = _copy_image_remapper(
        source_login, body.get("source_agency") or body.get("sourceAgency") or "",
        target_login, target_agency or "", _all_src_hashes, maps, workdir,
        log=(lambda m: _copy_job_log(job_id, m)),
        provided_hashes=(provided_hashes or None))
    if provided_hashes:
        _copy_job_log(job_id, f"картинки: mode='other', использую {len(provided_hashes)} загруженных хэшей round-robin")
    # Синтетический snapshot для copy_steps (в ЕПК-ветке НЕТ v5 phase_pull): campaigns.json (network
    # для step_disabled_places), adgroups.json/ads.json (бренд группы для step_prices).
    src_dir = workdir / "source"
    snap_campaigns_json: list[dict] = []
    snap_adgroups_json: list[dict] = []
    snap_ads_json: list[dict] = []

    for idx, camp in enumerate(campaigns, start=1):
        old_cid = int(camp["id"])
        old_name = str(camp.get("name") or "")
        new_name = _copy_normalize_campaign_name(old_name, replacements, target_r_code)
        base_href = _copy_target_href(((camp.get("additionalData") or {}).get("href")), src_domain, target_domain)
        src_groups = groups_by_campaign.get(old_cid) or []
        if not src_groups:
            results.append({"ok": False, "source_id": old_cid, "name": new_name, "error": "нет групп в Grid snapshot"})
            continue

        group_specs = []
        src_group_ids = []
        group_vendor_by_gid: dict[int, str] = {}   # old_gid → vendor (марка из имени группы) для shopping
        for grp in src_groups:
            gid = int(grp.get("adgroup_id") or 0)
            src_group_ids.append(gid)
            # Бренд/марка из ИМЕНИ ГРУППЫ источника (не хардкод «Haval»): для vendor товарки и adPrice.
            g_brand = _csteps_clean_group_brand(str(grp.get("adgroup_name") or ""))
            g_vendor = (g_brand.split()[0] if g_brand else "") or "Haval"
            group_vendor_by_gid[gid] = g_vendor
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
                # Баг 1: гео-словоформы + ремап r-сегмента кодера группы (r-код словами не задеть).
                "name": _copy_remap_region_code(
                    _copy_apply_geo_replacements(grp.get("adgroup_name") or "группа", replacements),
                    target_r_code),
                "keywords": [_copy_apply_geo_replacements(k, replacements) for k in (grp.get("keywords") or [])],
                "minus": list(grp.get("minus_keywords") or []),
                "titles": titles,
                "texts": bodies,
                "image_hashes": _remap_images(list(dict.fromkeys(h for h in image_hashes if h))[:5]),
                "href": href,
                "brand": g_brand or "Haval",
            })

        try:
            # Бюджет: берём из strategy.budget.sum source-кампании (добавлено в q_campaigns).
            # Фолбэк 7000 — если source не вернул или значение некорректно.
            _src_budget_raw = ((camp.get("strategy") or {}).get("budget") or {}).get("sum")
            try:
                _src_budget = max(1, int(float(str(_src_budget_raw)))) if _src_budget_raw else 7000
            except (TypeError, ValueError):
                _src_budget = 7000
            rep = gc.create_full(
                target_login,
                campaign_spec=_copy_grid_campaign_spec(new_name, counter_id, goal_id,
                                                       weekly_budget=_src_budget),
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

            # maps["ads"]: сорсовые текст/адаптив-объявления группы → ЕДИНОЕ комбинированное объявление,
            # которое create_full создал для этой группы (rep["ad_ids"] выровнен 1:1 с group_specs/src_groups).
            # Нужно для step_prices (adPrice на созданные адаптивы) и step_videos (перенос видео 1:1).
            new_ad_ids = rep.get("ad_ids") or []
            for gi, old_gid in enumerate(src_group_ids):
                new_ad_id = new_ad_ids[gi] if gi < len(new_ad_ids) else None
                if not new_ad_id:
                    continue
                for ad in ads_by_group.get(old_gid, []):
                    if str(ad.get("__typename") or "") in ("GdTextAd", "GdAdaptiveTextAd"):
                        src_ad_id = str(ad.get("id") or "")
                        if src_ad_id.isdigit():
                            maps["ads"][src_ad_id] = int(new_ad_id)
                            snap_ads_json.append({"Id": int(src_ad_id), "AdGroupId": int(old_gid),
                                                  "CampaignId": int(old_cid)})
            # Синтетический snapshot: network кампании (по тому же spec, что и create_full) + имена групп.
            spec_net = bool(_copy_grid_campaign_spec(new_name, counter_id, goal_id).get("network"))
            snap_campaigns_json.append({
                "Id": int(old_cid), "Name": new_name,
                "UnifiedAdCampaign": {"BiddingStrategy": {"Network": {
                    "BiddingStrategyType": ("AVERAGE_CPA" if spec_net else "SERVING_OFF")}}}})
            for grp in src_groups:
                gid = int(grp.get("adgroup_id") or 0)
                if gid > 0:
                    snap_adgroups_json.append({"Id": gid, "CampaignId": int(old_cid),
                                               "Name": str(grp.get("adgroup_name") or "группа")})

            shopping_added = 0
            listing_added = 0
            if target_feed_id:
                shop_items = []
                for old_gid in src_group_ids:
                    new_gid = maps["adgroups"].get(str(old_gid))
                    if new_gid and old_gid in shopping_groups:
                        _si = {"adgroup_id": int(new_gid), "feed_id": int(target_feed_id),
                               "vendor": group_vendor_by_gid.get(old_gid) or "Haval"}
                        try:
                            from . import create_set_feeds as _csf_ff
                            _si["brand_field"] = _csf_ff._resolve_feed_field(target_login, int(target_feed_id), "brand") or "vendor"
                            _si["model_field"] = _csf_ff._resolve_feed_field(target_login, int(target_feed_id), "model") or "model"
                        except Exception:  # noqa: BLE001
                            _si["brand_field"] = "vendor"
                            _si["model_field"] = "model"
                        shop_items.append(_si)
                if shop_items:
                    grid = gf.GridClient(target_login)
                    # add_shopping_ads возвращает ПОЗИЦИОННЫЙ list[int|None] (None = не создан). Спариваем
                    # id↔item ДО отбрасывания None — иначе schлопывание сдвинет vendor-фильтр на чужой товар.
                    _shop_pairs = [(int(x), _si) for x, _si in zip(grid.add_shopping_ads(shop_items) or [], shop_items) if x]
                    shop_ids = [_sid for _sid, _ in _shop_pairs]
                    shopping_added = len(shop_ids)
                    # Баг 5: «текст по умолчанию» товарных объявлений (у ShoppingAd нет текста
                    # без явного set_default_text). Берём тело ТГО группы (уже гео-морфнутое) →
                    # фолбэк на бренд. Фильтры по vendor + глобальные минус-марки (как create_shopping_content).
                    if shop_ids:
                        try:
                            default_text = ""
                            for gs in group_specs:
                                for _t in (gs.get("texts") or []):
                                    if str(_t or "").strip():
                                        default_text = str(_t).strip()
                                        break
                                if default_text:
                                    break
                            if not default_text:
                                _brand0 = (group_specs[0].get("brand") if group_specs else "") or "Haval"
                                default_text = f"{_brand0} в наличии. Успей купить по выгодной цене"
                            from .text_norm import _trim_clean as _tc
                            default_text = _tc(default_text, 81)
                            filters_by_ad_id = {}
                            for _sid, _src in _shop_pairs:
                                conds = []
                                _vv = str(_src.get("vendor") or "").strip()
                                if _vv:
                                    _variants = list(dict.fromkeys([_vv, _vv.lower(), _vv.title()]))
                                    conds.append({"field": _src.get("brand_field") or "vendor",
                                                  "operator": "CONTAINS_ANY",
                                                  "stringValue": json.dumps(_variants, ensure_ascii=False)})
                                try:
                                    from . import create_set_feeds as _csf_dt
                                    conds.extend(_csf_dt._minus_marks_grid_conditions(
                                        brand_field=_src.get("brand_field") or "vendor",
                                        model_field=_src.get("model_field") or "model"))
                                except Exception:  # noqa: BLE001
                                    pass
                                if conds:
                                    filters_by_ad_id[int(_sid)] = {"tab": "CONDITION", "conditions": conds}
                            if default_text:
                                grid.set_default_text(shop_ids, int(target_feed_id), default_text,
                                                      filters_by_ad_id=filters_by_ad_id)
                                _copy_job_log(job_id, f"grid-cookie {new_name}: текст по умолчанию "
                                                      f"проставлен на {len(shop_ids)} товарных")
                        except Exception as _e_dt:  # noqa: BLE001 — товарные созданы; текст по умолчанию не критичен для сборки
                            _copy_job_log(job_id, f"grid-cookie {new_name}: текст по умолчанию не проставлен ({str(_e_dt)[:160]})")
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
        except Exception as e:  # noqa: BLE001 — транспортный/прочий сбой не убивает весь job
            results.append({"ok": False, "source_id": old_cid, "name": new_name, "error": str(e)[:220]})
            _copy_job_log(job_id, f"grid-cookie {old_name}: исключение {str(e)[:200]}")
            continue

    _copy_write_json(workdir / "id_maps.json", maps)

    # ДОРАБОТКА 2: copy_steps-постобработка для ЕПК-ветки (те же под-сервисы, что v5-путь).
    # Пишем синтетический snapshot (source-dir) и прогоняем применимые шаги cookie/Grid (0 v5-баллов).
    cookie_post = {"skipped": ["postprocess (нет созданных кампаний)"], "errors": []}
    if maps["campaigns"]:
        try:
            src_dir.mkdir(parents=True, exist_ok=True)
            _copy_write_json(src_dir / "campaigns.json", snap_campaigns_json)
            _copy_write_json(src_dir / "adgroups.json", snap_adgroups_json)
            _copy_write_json(src_dir / "ads.json", snap_ads_json)
            cookie_post = _copy_grid_unified_steps(
                job_id, body, target_login, target_agency or "", src_domain,
                replacements, maps, src_dir, workdir)
        except Exception as e:  # noqa: BLE001 — постобработка не валит уже созданные кампании
            cookie_post = {"errors": [f"grid unified postprocess: {str(e)[:220]}"]}
            _copy_job_log(job_id, f"grid-cookie postprocess: ошибка {str(e)[:200]}")
        for _err in (cookie_post.get("errors") or [])[:8]:
            _copy_job_log(job_id, f"grid-cookie postprocess warning: {_err}")

    created_ids = [int(r["id"]) for r in results if r.get("ok") and r.get("id")]
    verify = {"status": "ok" if len(created_ids) == len(selected_ids) else "warning",
              "created": len(created_ids), "expected": len(selected_ids)}
    errors = [r for r in results if not r.get("ok")]
    return {
        "cookie_post": cookie_post,
        "source_login": source_login,
        "target_login": target_login,
        "selected": len(selected_ids),
        "created": len(created_ids),
        "results": results,
        "errors": errors,
        "target_region_id": int(local_gid) if local_gid else None,
        "target_region_source": f"dict:{local_geo_name}" if local_gid else "keep",
        "target_feed_id": target_feed_id,
        "context_rewrite": {"replacements": len(replacements), "files": 0, "residual_geo": []},
        "live_verification": verify,
        "workdir": str(workdir),
        "uses_direct_units": False,
    }


def _copy_grid_validate_feed_map(target_login: str, target_agency: str, body: dict,
                                 *, log=lambda m: None) -> dict:
    """Разобрать и провалидировать body.feed_map для ЕПК-ветки (та же логика, что _copy_run_job).

    Возвращает {src_feed_id: tgt_feed_id} только с ЦЕЛЕВЫМИ фидами, ПРИНАДЛЕЖАЩИМИ target-аккаунту.
    Grid недоступен/пустой список фидов → доверяем вводу без валидации (как в _copy_run_job).
    feed_map пуст/битый → {}."""
    raw: dict[str, int] = {}
    fm = body.get("feed_map")
    if not isinstance(fm, dict):
        return {}
    for k, v in fm.items():
        if str(k).strip().isdigit() and str(v).strip().isdigit() and int(v) > 0:
            raw[str(int(k))] = int(v)
    if not raw:
        return {}
    try:
        tgt_ids = {int(f.get("id")) for f in _grid_feeds(target_login, target_agency or _resolve_agency_hint(target_login, ""))
                   if str(f.get("id") or "").strip().isdigit()}
    except Exception:  # noqa: BLE001
        tgt_ids = set()
    if not tgt_ids:
        log("feed_map: фиды target недоступны (grid пуст/ошибка) — feed_map применён без валидации")
        return raw
    valid: dict[str, int] = {}
    for sid, tid in raw.items():
        if tid in tgt_ids:
            valid[sid] = tid
        else:
            log(f"feed_map: целевой фид {tid} не принадлежит {target_login} — пропуск (source {sid})")
    return valid


def _copy_grid_bridge_callouts(source_grid, target_grid, src_dir: Path, maps: dict,
                               *, log=lambda m: None) -> None:
    """Перенести уточнения источника на target ПО ТЕКСТУ (Grid, 0 баллов) и заполнить
    maps['callouts'] = {src_callout_id: tgt_callout_id}.

    Связь campaign→callout_ids даёт campaign_callouts.json (pull_source_campaign_assets), а тексты —
    source_grid.get_callouts() ({текст: id}, инвертируем в {id: текст}). Затем target_grid.add_callouts
    создаёт (с дедупом) те же тексты на target.

    Баг 2b: раньше при source_grid=None (сбой куки источника) — ТИХИЙ no-op, уточнения молча терялись.
    Теперь: если исходные callout-id ЕСТЬ, а source/target grid недоступен — поднимаем ошибку (caller
    вынесет её в rep['errors']). Нет исходных id — реально нечего переносить, тихо ок."""
    links = _copy_read_json(src_dir / "campaign_callouts.json")
    links = links if isinstance(links, dict) else {}
    wanted_ids = {str(x) for co_ids in links.values() for x in (co_ids or []) if str(x).strip()}
    if not wanted_ids:
        return
    if target_grid is None:
        raise RuntimeError("нет target grid-клиента — уточнения не перенесены")
    if source_grid is None:
        raise RuntimeError(
            f"нет source grid-клиента (куки источника) — {len(wanted_ids)} уточнений не перенесены")
    src_text_by_id: dict[str, str] = {}
    try:
        for text, cid in (source_grid.get_callouts() or {}).items():
            src_text_by_id[str(cid)] = text
    except Exception as e:  # noqa: BLE001
        log(f"уточнения: чтение текстов источника не удалось ({str(e)[:150]})")
        return
    texts = list(dict.fromkeys(
        src_text_by_id[i] for i in wanted_ids if i in src_text_by_id and str(src_text_by_id[i]).strip()))
    if not texts:
        return
    try:
        tgt_map = target_grid.add_callouts(texts) or {}   # {текст: tgt_id}
    except Exception as e:  # noqa: BLE001
        log(f"уточнения: создание на target не удалось ({str(e)[:150]})")
        return
    maps.setdefault("callouts", {})
    for src_id in wanted_ids:
        text = src_text_by_id.get(src_id)
        if text and tgt_map.get(text):
            maps["callouts"][str(src_id)] = int(tgt_map[text])
    log(f"уточнения перенесены на target: {len(maps['callouts'])} id (из {len(wanted_ids)} исходных)")


def _copy_grid_unified_steps(job_id: str, body: dict, target_login: str, target_agency: str,
                             src_domain: str, replacements, maps: dict,
                             src_dir: Path, workdir: Path) -> dict:
    """copy_steps-постобработка ЕПК-ветки (cookie/Grid, НОЛЬ v5-баллов).

    Применяет к комбинированным кампаниям, созданным create_full, те же под-сервисы, что и
    v5-snapshot путь (_copy_cookie_postprocess):
      • step_age_bidmods    (п.14) — возраст −100% (<18/18–24) через Grid set_campaign_age_bidmods;
      • step_disabled_places(п.13) — наш минус-список площадок на РСЯ (network из синт. campaigns.json);
      • step_attach_callouts(п.11) — уточнения по исходной связи (text-bridge источник→target);
      • step_attach_promos  (п.10) — формально (нет source-promo-def reader → безопасный no-op);
      • step_prices         (п.8)  — новые цены из ФИДА target-аккаунта на комбинаторные объявления;
      • step_videos         (п.12) — видео 1:1 по куке (resolver originalUrl из Grid-интроспекции).

    СОЗНАТЕЛЬНО НЕ вызываем (иначе двойная работа — см. ДОРАБОТКА 3):
      • step_keywords — create_full УЖЕ залил ключи по Grid (0 баллов); повтор = дубли фраз;
      • step_adaptive_creatives — create_full УЖЕ собрал комбинированное объявление 1:1
        (заголовки/тексты/картинки источника + гео-склонения применены в _copy_apply_geo_replacements);
        картинки ремапятся ДО create_full (_copy_image_remapper: as-is или переаплоад в target по
        кукам, недоступные дропаются) — повторная запись здесь избыточна.
    """
    from . import copy_steps as csteps
    rep: dict = {"skipped": ["keywords (create_full уже залил)",
                             "adaptive_creatives (create_full собрал 1:1)"], "errors": []}
    source_login = (body.get("source_login") or "").strip()
    # Баг 2a/3: source-домен для доменной трансформации href быстрых ссылок (step_attach_sitelinks).
    if src_domain and not body.get("_copy_source_domain"):
        body["_copy_source_domain"] = src_domain
    try:
        tgt_uac = cmc.build_client(target_login, account=(target_agency or None))
        tgt_cookie = tgt_uac.sess.headers.get("Cookie") or ""
        grid = gf.GridClient(target_login, cookie=tgt_cookie)
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"target cookie init: {str(e)[:200]}")
        return rep
    source_grid = None
    if source_login:
        try:
            _src_ag = _resolve_agency_hint(source_login, "")
            _src_uac = cmc.build_client(source_login, account=(_src_ag or None))
            source_grid = gf.GridClient(source_login, cookie=(_src_uac.sess.headers.get("Cookie") or ""))
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"source cookie init: {str(e)[:180]}")
    try:
        _tgt_token, _ = _token_for_login(
            target_login, target_agency or _resolve_agency_hint(target_login, ""), _direct_tokens())
    except Exception:  # noqa: BLE001
        _tgt_token = ""

    # Исходные связи campaign→callouts/promo (Grid источника) → src_dir/campaign_callouts.json + promos.
    src_camp_ids = [int(x) for x in (maps.get("campaigns") or {}).keys() if str(x).isdigit()]
    try:
        csteps.pull_source_campaign_assets(
            source_grid, src_camp_ids, src_dir, log=(lambda m: _copy_job_log(job_id, m)))
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"pull source assets: {str(e)[:180]}")
    # Уточнения: тексты source-callout id → создать на target → maps['callouts'].
    try:
        _copy_grid_bridge_callouts(
            source_grid, grid, src_dir, maps, log=(lambda m: _copy_job_log(job_id, m)))
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"callouts bridge: {str(e)[:180]}")

    ctx = csteps.CopyCtx(
        target_login=target_login, target_agency=target_agency or "",
        src_dir=src_dir, workdir=workdir, body=body, maps=maps,
        grid=grid, target_token=_tgt_token or "",
        log=(lambda m: _copy_job_log(job_id, m)),
        v5_call=_v5_call, enabled_minus_places=_enabled_baseline_minus_places,
        feed_offer_prices=_grid_feed_offer_prices, account_offer_prices=_account_offer_prices,
        group_ad_price=_group_ad_price, set_ad_prices=_grid_set_ad_prices,
    )
    ctx.source_login = source_login
    ctx.source_grid = source_grid
    ctx.geo_pairs = replacements or []
    ctx.update_adaptive_ads = _grid_update_adaptive_ads
    ctx.video_upload_client = tgt_uac
    ctx.video_file_resolver = _copy_make_video_resolver(job_id, source_grid, maps, workdir)
    try:
        from .promo import PromoClient
        ctx.promo_client = PromoClient(tgt_uac, target_login)
    except Exception:  # noqa: BLE001
        ctx.promo_client = None

    # Промоакции: читаем определения промо из сырых строк CampaignsEditData источника и
    # создаём их на target. campaigns_edit_rows возвращает promoExtension{id type prefix amount
    # unit description startDate finishDate href promocode} — полный объект для воссоздания.
    # После этого maps["promotions"] заполнен → step_attach_promos привяжет промо по связи.
    created_promo_ids: list[int] = []
    if source_grid is not None and src_camp_ids and ctx.promo_client is not None:
        try:
            _src_raw = source_grid.campaigns_edit_rows(src_camp_ids)
            _promo_defs: dict[str, dict] = {}
            for _cid, _row in (_src_raw or {}).items():
                _promo = _row.get("promoExtension") or {}
                _pid = str(_promo.get("id") or "")
                if _pid and _pid != "0" and _promo.get("description"):
                    _promo_defs[_pid] = _promo
            _src_domain_for_promo = (body.get("_copy_source_domain") or src_domain or "").strip()
            _tgt_domain_for_promo = (body.get("target_domain") or "").strip()
            for _src_pid, _pdef in _promo_defs.items():
                if str(_src_pid) in maps["promotions"]:
                    created_promo_ids.append(int(maps["promotions"][str(_src_pid)]))
                    continue
                _new_pid, _perr = ctx.promo_client.add(
                    type=_pdef.get("type") or "DISCOUNT",
                    description=_pdef.get("description") or "акция",
                    href=_copy_target_href(_pdef.get("href"),
                                          _src_domain_for_promo, _tgt_domain_for_promo),
                    amount=_pdef.get("amount"),
                    unit=_pdef.get("unit"),
                    prefix=_pdef.get("prefix"),
                    promocode=_pdef.get("promocode"),
                    start=_pdef.get("startDate"),
                    finish=_pdef.get("finishDate"),
                )
                if _new_pid:
                    maps["promotions"][str(_src_pid)] = int(_new_pid)
                    created_promo_ids.append(int(_new_pid))
                elif _perr:
                    rep["errors"].append(f"promo {_src_pid}: {_perr[:180]}")
            rep["promos_created"] = len(created_promo_ids)
            _copy_job_log(job_id, f"промо: создано {len(created_promo_ids)} из {len(_promo_defs)} источника")
        except Exception as _e:  # noqa: BLE001
            rep["errors"].append(f"promo defs read/create: {str(_e)[:200]}")

    for name, fn in (
        ("age_bidmods", lambda: csteps.step_age_bidmods(ctx)),
        ("disabled_places", lambda: csteps.step_disabled_places(ctx)),
        ("attach_callouts", lambda: csteps.step_attach_callouts(ctx, per_campaign_cap=_CALLOUT_PER_CAMPAIGN_CAP)),
        # Баг 2a: быстрые ссылки по исходной связи campaign→sitelinkSet (source-grid read → target set).
        ("attach_sitelinks", lambda: csteps.step_attach_sitelinks(ctx)),
        # maps["promotions"] заполнен выше → created_promo_ids содержит реальные target-id.
        ("attach_promos", lambda: csteps.step_attach_promos(ctx, list(created_promo_ids))),
        ("prices", lambda: csteps.step_prices(ctx)),
        ("videos", lambda: csteps.step_videos(ctx)),
    ):
        try:
            r = fn()
            rep[name] = r
            if isinstance(r, dict):
                rep["errors"] += r.get("errors") or []
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"{name}: {str(e)[:200]}")
    _copy_write_json(workdir / "id_maps.json", maps)
    return rep


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
                        goal_id: int, target_feed_id: int | None,
                        feed_map: dict | None = None) -> dict:
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

    # image_mode=upload: картинки берём из ЦЕЛЕВОГО аккаунта (уже залиты copy_other._copy_images_upload).
    # Иначе upload_content качает файл источника → одинаковый хэш и бренд-тема источника во всех копиях.
    tgt_img_urls: list[str] = []
    if str(body.get("image_mode") or "") == "upload":
        hashes = [str(h).strip() for h in (body.get("image_hashes") or []) if str(h).strip()]
        if hashes:
            try:
                _tt, _ = _token_for_login(
                    target_login, target_agency or _resolve_agency_hint(target_login, ""), _direct_tokens())
            except Exception:  # noqa: BLE001
                _tt = None
            for i in range(0, len(hashes), 100) if _tt else []:
                try:
                    data = _v501_svc("adimages", "get", _tt, target_login,
                                     {"SelectionCriteria": {"AdImageHashes": hashes[i:i + 100]},
                                      "FieldNames": ["AdImageHash", "OriginalUrl"]})
                except Exception:  # noqa: BLE001
                    continue
                for im in ((data.get("result") or {}).get("AdImages") or []):
                    u = str(im.get("OriginalUrl") or "").strip()
                    if u and u not in tgt_img_urls:
                        tgt_img_urls.append(u)

    for cidx, row in enumerate(rows):
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
            src_feed_raw = _copy_uac_value(d, "feed_id", "listings_feed_id")
            is_product = name.lower().startswith("tp7_") or bool(src_feed_raw)
            # Пофидовая замена: если исходный фид кампании есть в feed_map — берём целевой из карты,
            # иначе фолбэк на общий target_feed_id (прежнее поведение).
            eff_target_feed = target_feed_id
            try:
                _sf = str(int(src_feed_raw)) if src_feed_raw not in (None, "") else ""
            except (TypeError, ValueError):
                _sf = ""
            if feed_map and _sf and _sf in feed_map:
                eff_target_feed = feed_map[_sf]
            feed_id = int(eff_target_feed or 0) if is_product else None
            # Детерминированный round-robin: у каждой i-й МК свои 5 картинок из архива цели.
            if tgt_img_urls:
                _img = [tgt_img_urls[(cidx * 5 + k) % len(tgt_img_urls)] for k in range(5)]
            else:
                _img = _copy_uac_media_urls(d, want="image")
            # socdem источника, иначе датакласс молча подставит дефолт age_18 вместо возраста источника.
            _sd = _copy_uac_value(d, "socdem", default={}) or {}
            if not isinstance(_sd, dict):
                _sd = {}
            # Таргетинг-поля источника: не передашь — датакласс молча подставит свой дефолт (как было с socdem).
            _dev = _copy_uac_value(d, "device_types", default=[]) or []
            _dev = [str(x).strip() for x in _dev if str(x).strip()] if isinstance(_dev, list) else []
            _mreg_raw = _copy_uac_value(d, "minus_regions", "minus_region_ids", default=[]) or []
            _mreg: list[int] = []
            for _r in (_mreg_raw if isinstance(_mreg_raw, list) else []):
                try:
                    _mreg.append(int(_r))
                except (TypeError, ValueError):
                    continue
            _rm = _copy_uac_value(d, "relevance_match", default={}) or {}
            if not isinstance(_rm, dict):
                _rm = {}
            _rm_cats = [str(c).strip() for c in (_rm.get("categories") or []) if str(c).strip()]
            _ttg = _copy_uac_value(d, "time_target", default={}) or {}
            if not isinstance(_ttg, dict):
                _ttg = {}
            try:
                _tz = int(_ttg.get("id_time_zone") or 130)
            except (TypeError, ValueError):
                _tz = 130
            _extra = {"relevance_match_categories": _rm_cats} if _rm_cats else {}
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
                image_urls=_img,
                video_urls=_copy_uac_media_urls(d, want="video"),
                audiences=audiences if isinstance(audiences, list) else [],
                genders=_sd.get("genders") or ["female", "male"],
                age_lower=str(_sd.get("age_lower") or "age_18"),
                age_upper=str(_sd.get("age_upper") or "age_inf"),
                limit_period=str(_copy_uac_value(d, "limit_period", "limitPeriod", default="week") or "week"),
                device_types=_dev or ["all"],
                minus_regions=_mreg,
                id_time_zone=_tz,
                # Наш стандарт tp6/tp7 (create_set_master_product.py:692, коды uac_verifier
                # UAC_ALTERNATIVE_TEXTS_ENABLED / UAC_MAPS_ENABLED) — сильнее «копии 1:1».
                alternative_texts_enabled=False,
                ml_banners_enabled=False,
                yandex_maps_enabled=False,
                utm_template=cmc.UTM_TEMPLATE,
                **_extra,
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


def _copy_make_video_resolver(job_id: str, source_grid, maps: dict, workdir: Path):
    """ФАЗА 3c п.12: resolver mp4 исходного видео по Grid-интроспекции (originalUrl).

    Один prefetch по куки ИСТОЧНИКА (source_grid.video_creative_urls на src camp/ad из maps) →
    {src_creative_id: originalUrl}. Затем на каждый вызов (meta.creative_id) скачивает mp4 в
    workdir/_video_cache/<cid>.mp4 (кэш — один и тот же ролик у нескольких объявлений качаем раз) и
    отдаёт путь; None — если URL нет или скачать не удалось (step_videos тогда честно репортит).

    Скачиваемость доказана live 2026-07-03: originalUrl отдаёт HTTP 200 video/mp4 без авторизации.
    Приоритет URL: originalUrl (исходник 1:1) → livePreviewUrl (рендер-превью) как запасной."""
    src_camp_ids = [int(x) for x in (maps.get("campaigns") or {}).keys() if str(x).isdigit()]
    src_ad_ids = [int(x) for x in (maps.get("ads") or {}).keys() if str(x).isdigit()]
    url_map: dict[str, dict] = {}
    if source_grid is not None and src_ad_ids:
        try:
            url_map = source_grid.video_creative_urls(src_camp_ids, src_ad_ids) or {}
            if url_map:
                _copy_job_log(job_id, f"видео: Grid-интроспекция дала {len(url_map)} скачиваемых mp4-URL (originalUrl)")
        except Exception as e:  # noqa: BLE001
            _copy_job_log(job_id, f"видео: чтение URL источника не удалось ({str(e)[:150]})")
            url_map = {}
    if not url_map:
        return None
    cache_dir = Path(workdir) / "_video_cache"

    def _resolver(meta: dict):
        cid = str((meta or {}).get("creative_id") or "").strip()
        info = url_map.get(cid) or {}
        url = info.get("original_url") or info.get("live_preview_url") or ""
        if not cid or not url:
            return None
        cache_dir.mkdir(parents=True, exist_ok=True)
        dst = cache_dir / f"{cid}.mp4"
        if dst.exists() and dst.stat().st_size > 0:
            return str(dst)
        import requests as _rqs
        try:
            with _rqs.get(url, stream=True, timeout=90, verify=False) as r:
                if r.status_code != 200:
                    _copy_job_log(job_id, f"видео {cid}: originalUrl HTTP {r.status_code} — пропуск")
                    return None
                ct = (r.headers.get("Content-Type") or "").lower()
                if "video" not in ct and "octet-stream" not in ct and "mp4" not in ct:
                    _copy_job_log(job_id, f"видео {cid}: неожиданный content-type {ct!r} — пропуск")
                    return None
                with open(dst, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            fh.write(chunk)
        except Exception as e:  # noqa: BLE001
            _copy_job_log(job_id, f"видео {cid}: скачивание не удалось ({str(e)[:120]})")
            try:
                if dst.exists():
                    dst.unlink()
            except OSError:
                pass
            return None
        if dst.stat().st_size <= 0:
            return None
        return str(dst)

    return _resolver


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

    # ФАЗА 1 под-сервисы (copy_steps): единый контекст для шагов П.10/П.11/П.13/П.14.
    from . import copy_steps as csteps
    try:
        _tgt_token, _ = _token_for_login(
            target_login, target_agency or _resolve_agency_hint(target_login, ""), _direct_tokens())
    except Exception:  # noqa: BLE001
        _tgt_token = ""
    cstep_ctx = csteps.CopyCtx(
        target_login=target_login, target_agency=target_agency or "",
        src_dir=src_dir, workdir=workdir, body=body, maps=maps,
        grid=grid, target_token=_tgt_token or "",
        log=(lambda m: _copy_job_log(job_id, m)),
        v5_call=_v5_call, enabled_minus_places=_enabled_baseline_minus_places,
        # П.8: прайс-хелперы (create_set_feeds через blueprint-обёртки — configure() внутри).
        feed_offer_prices=_grid_feed_offer_prices, account_offer_prices=_account_offer_prices,
        group_ad_price=_group_ad_price, set_ad_prices=_grid_set_ad_prices,
    )

    # ФАЗА 3b (п.4 адаптивы / п.12 видео): source-Grid (куки источника, чтение состава), гео-пары
    # job'а, RMW-апдейтер адаптивов (сохраняет target href/adPrice/видео), видео-аплоуд по куки.
    cstep_ctx.update_adaptive_ads = _grid_update_adaptive_ads
    cstep_ctx.video_upload_client = client          # UacClient target: upload_video_creative по куки
    cstep_ctx.video_file_resolver = None            # заполним ниже, если source_grid поднялся (см. п.12)
    try:
        _src_login = (body.get("source_login") or "").strip()
        cstep_ctx.source_login = _src_login
        if _src_login:
            _src_ag = _resolve_agency_hint(_src_login, "")
            _src_cli2 = cmc.build_client(_src_login, account=(_src_ag or None))
            cstep_ctx.source_grid = gf.GridClient(_src_login, cookie=(_src_cli2.sess.headers.get("Cookie") or ""))
            _src_ctx = _copy_ctx(_src_login)
            _geo_mode_pp = (body.get("geo_mode") or "replace").strip()
            _mode_pp = (body.get("mode") or "auto").strip()
            if _geo_mode_pp == "keep":
                # geo_mode="keep": никакой гео-замены — источник остаётся как есть.
                # target_city/region не переданы — _copy_geo_replacements с пустыми строками
                # строил бы пары вида ("Краснодар", "") → стирал города из быстрых ссылок.
                cstep_ctx.geo_pairs = []
            elif _mode_pp == "other" and _geo_mode_pp == "change":
                # mode="other" + geo_mode="change": имя резолвим по ID из справочника.
                # target_city/region пусты — нельзя их использовать.
                _pp_geo_rid = int(body.get("geo_region_id") or 0)
                _pp_rname = (_geo_name_by_id(_pp_geo_rid) if _geo_name_by_id and _pp_geo_rid else "") or ""
                cstep_ctx.geo_pairs = _copy_geo_replacements(
                    _src_ctx, "", _pp_rname,
                    log=(lambda m: _copy_job_log(job_id, m))) if _pp_rname else []
            else:
                # mode="auto": target_city/region пришли из body (валидированы роутом).
                cstep_ctx.geo_pairs = _copy_geo_replacements(
                    _src_ctx, body.get("target_city") or "", body.get("target_region") or "",
                    log=(lambda m: _copy_job_log(job_id, m)))
            # ФАЗА 3c п.12: скачиваемый URL исходного видео найден Grid-интроспекцией —
            # GdVideoAdditionCreative.originalUrl = прямой mp4 (storage.mds.yandex.net, HTTP 200
            # video/mp4 без авторизации, проверено live 2026-07-03). Строим resolver: creative_id
            # источника → скачать mp4 → отдать в step_videos (аплоуд по куки + RMW-привязка).
            cstep_ctx.video_file_resolver = _copy_make_video_resolver(
                job_id, cstep_ctx.source_grid, maps, workdir)
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"adaptive/video ctx init: {str(e)[:180]}")

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
            rep["callouts_created_or_found"] = len(callout_map)
            # П.11: вешаем на КАЖДУЮ кампанию только её ремапленные callout-id (по исходной связи),
            # а не общий union. Фолбэк на union — внутри шага, если source-связь недоступна.
            co_report = csteps.step_attach_callouts(cstep_ctx, per_campaign_cap=_CALLOUT_PER_CAMPAIGN_CAP)
            rep["callouts_attached_campaigns"] = co_report.get("attached_campaigns") or 0
            rep["callouts_per_campaign"] = co_report
            rep["errors"] += co_report.get("errors") or []
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
            # П.10: привязка промо по исходной связи campaign→promo (работает и при 2+ промо).
            # Фолбэк на прежнее единичное поведение — внутри шага.
            cstep_ctx.promo_client = pc
            promo_report = csteps.step_attach_promos(cstep_ctx, created_promo_ids)
            rep["promos_attached_campaigns"] = promo_report.get("attached_campaigns") or 0
            rep["promos_per_campaign"] = promo_report
            rep["errors"] += promo_report.get("errors") or []
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"promos grid: {str(e)[:220]}")

    # 1b) КЛЮЧЕВЫЕ СЛОВА Grid-FIRST (ФАЗА 3c п.2). phase_upload теперь skip_keywords=True → v5 ключи
    # не жёг (152). step_keywords добавляет ВСЕ фразы через Grid (0 баллов), v5 — только фолбэк
    # (UserParam-фразы + не прошедшие Grid). group-remap/ставки/UserParam/done-учёт — внутри шага.
    try:
        kw_rep = csteps.step_keywords(cstep_ctx)
        rep["keywords"] = kw_rep
        rep["keywords_added"] = int(kw_rep.get("via_grid") or 0) + int(kw_rep.get("via_v5") or 0)
        if int(kw_rep.get("via_v5") or 0) > 0:
            rep["uses_direct_units"] = True     # v5-фолбэк (UserParam/Grid-fail) тратит баллы
        rep["errors"] += kw_rep.get("errors") or []
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"keywords grid-first: {str(e)[:220]}")

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
        try:
            from . import create_set_feeds as _csf_ff
            item["brand_field"] = _csf_ff._resolve_feed_field(target_login, int(fid), "brand") or "vendor"
            item["model_field"] = _csf_ff._resolve_feed_field(target_login, int(fid), "model") or "model"
        except Exception:  # noqa: BLE001
            item["brand_field"] = "vendor"
            item["model_field"] = "model"
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

    # П.15: восстановление стратегии PAY_FOR_CONVERSION_MULTIPLE_GOALS через Grid.
    # Кампании с этой стратегией создавались с WB_MAXIMUM_CLICKS (v5 не принимает PFCMG).
    # Здесь восстанавливаем реальную стратегию: payForConversion=True + goalId + sum (рубли).
    try:
        src_campaigns_all = _copy_read_json(src_dir / "campaigns.json")
        _pfcmg_restored = 0
        _pfcmg_errors = []
        goal_id_body = int(body.get("goal_id") or 0)
        for _sc in src_campaigns_all:
            _src_id = str(_sc.get("Id") or "")
            _tgt_id = maps.get("campaigns", {}).get(_src_id)
            if not _tgt_id:
                continue
            # Ищем PAY_FOR_CONVERSION_MULTIPLE_GOALS в любом из struct_key.BiddingStrategy.Search/Network
            _weekly_rub = None
            for _struct_key in ("TextCampaign", "DynamicTextCampaign", "UnifiedAdCampaign"):
                _td = _sc.get(_struct_key) or {}
                _bs = _td.get("BiddingStrategy") or {}
                for _side in ("Search", "Network"):
                    _sb = _bs.get(_side) or {}
                    if _sb.get("BiddingStrategyType") == "PAY_FOR_CONVERSION_MULTIPLE_GOALS":
                        _blk = _sb.get("PayForConversionMultipleGoals") or {}
                        _weekly_micro = int(_blk.get("WeeklySpendLimit") or 300_000_000_000)
                        _weekly_rub = _weekly_micro / 1_000_000  # микро → рубли
                        break
                if _weekly_rub is not None:
                    break
            if _weekly_rub is None:
                continue
            _g_id = goal_id_body or 0
            _copy_job_log(job_id, f"restore PFCMG стратегии: кампания {_src_id}→{_tgt_id} "
                                  f"goal={_g_id} weekly_rub={int(_weekly_rub)}")
            try:
                _updated = grid.restore_pay_for_conversion_strategy(
                    int(_tgt_id), _g_id, _weekly_rub)
                if _updated:
                    _pfcmg_restored += 1
                    _copy_job_log(job_id, f"PFCMG стратегия восстановлена: {_tgt_id}")
                else:
                    _pfcmg_errors.append(f"кампания {_tgt_id}: Grid вернул пустой updatedCampaigns")
            except Exception as _e:  # noqa: BLE001
                _pfcmg_errors.append(f"кампания {_tgt_id}: {str(_e)[:220]}")
        rep["strategy_restore"] = {"restored": _pfcmg_restored, "errors": _pfcmg_errors}
        rep["errors"] += _pfcmg_errors
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"strategy restore: {str(e)[:200]}")

    # Перенос Grid-only настроек (isOrganicSearchEnabled / placementTypes) 1:1 из источника.
    # v5-путь создания не трогает эти поля → они остаются на дефолтах Директа. Добавлено 2026-07-17.
    try:
        rep["organic_placement"] = csteps.step_fix_organic_placement(cstep_ctx)
        rep["errors"] += rep["organic_placement"].get("errors") or []
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"organic placement: {str(e)[:200]}")

    # ФИНАЛ: сверка настроек источник ↔ копия ПО КУКАМ (report-only, 0 v5-баллов).
    # Идёт ПОСЛЕДНЕЙ — после всех добивок (цены/видео/адаптивы/стратегия), иначе сравнивали бы
    # промежуточное состояние. Ловит молчаливые потери, которых v5 не показывает (минус-слова
    # кампаний, brandSafety, временной таргетинг, contextLimit и пр.). Расхождения — в отчёт джоба;
    # автопочинка тут НЕ делается: чинит adjacent-шаг repair, а этот честно показывает факт.
    try:
        rep["settings_diff"] = csteps.step_settings_diff(cstep_ctx)
        rep["errors"] += (rep["settings_diff"].get("errors") or [])
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"settings diff: {str(e)[:200]}")

    # П.14: стандартные возрастные корректировки −100% (<18, 18–24) через v5.
    try:
        rep["age_bidmods"] = csteps.step_age_bidmods(cstep_ctx)
        rep["errors"] += rep["age_bidmods"].get("errors") or []
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"age bidmods: {str(e)[:200]}")
    # П.13: наш стандартный disabledPlaces на скопированные РСЯ-кампании (Grid).
    try:
        rep["disabled_places"] = csteps.step_disabled_places(cstep_ctx)
        rep["errors"] += rep["disabled_places"].get("errors") or []
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"disabled places: {str(e)[:200]}")
    # П.4 (ФАЗА 3b): адаптивные креативы 1:1 по куки (Grid) — заголовки/тексты/картинки источника,
    # гео в тексте с падежами; БЕЗ исходного CreativeId и БЕЗ v5-баллов. ДО step_prices, чтобы
    # adPrice лёг на уже приведённый 1:1 контент (RMW step_prices его сохранит).
    try:
        rep["adaptive_creatives"] = csteps.step_adaptive_creatives(cstep_ctx)
        rep["errors"] += rep["adaptive_creatives"].get("errors") or []
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"adaptive creatives: {str(e)[:200]}")

    # П.8: НОВЫЕ РЕАЛЬНЫЕ цены из ФИДА target-аккаунта на созданные адаптивные объявления (Grid adPrice).
    try:
        rep["prices"] = csteps.step_prices(cstep_ctx)
        rep["errors"] += rep["prices"].get("errors") or []
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"prices: {str(e)[:200]}")

    # П.12 (ФАЗА 3b/3c): видео 1:1 по куки — ПОСЛЕ prices (attach через RMW сохраняет контент/цену,
    # а step_prices через _grid_set_ad_prices слал creativeIds=[] → до него видео стерлось бы).
    # ФАЗА 3c: video_file_resolver теперь заполнен (originalUrl из Grid-интроспекции) → видео
    # реально переносится (скачать mp4 → аплоуд по куки → RMW-привязка). Нет URL/скачивания —
    # честный report-only (внутри step_videos).
    try:
        rep["videos"] = csteps.step_videos(cstep_ctx)
        rep["errors"] += rep["videos"].get("errors") or []
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"videos: {str(e)[:200]}")

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

    # Обязательная сверка source↔target после создания (REPORT-ONLY, движок copy_verify).
    try:
        from . import copy_verify as cv
        verify_result = cv.run_copy_verification(
            src_dir=src_dir, workdir=workdir,
            target_login=target_login, target_agency=target_agency,
            grid=grid, source_grid=cstep_ctx.source_grid,
            log=(lambda m: _copy_job_log(job_id, m)),
        )
        rep["copy_verify"] = verify_result
        _s = verify_result.get("summary") or {}
        _copy_job_log(job_id, f"copy_verify: ok={_s.get('ok')}, mismatch={_s.get('mismatch')}, "
                              f"missing={_s.get('missing')}, unreadable={_s.get('unreadable')}")
    except Exception as _ve:  # noqa: BLE001
        rep["errors"].append(f"copy_verify: {str(_ve)[:200]}")

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
        # PAY_FOR_CONVERSION_MULTIPLE_GOALS: v5 не принимает без счётчика+целей (4000/8000).
        # Стратегия будет восстановлена через Grid в _copy_cookie_postprocess — здесь пропускаем.
        _has_pfcmg = any(
            (strategy.get(side) or {}).get("BiddingStrategyType") == "PAY_FOR_CONVERSION_MULTIPLE_GOALS"
            for side in ("Search", "Network")
        )
        if strategy and not _has_pfcmg:
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
            continue
        # Цели переносим 1:1 только при ОБЩЕМ счётчике: GoalId привязан к счётчику источника,
        # чужой счётчик → невалидные цели (4000/8000). Value — микро-единицы, как в источнике.
        pg_items = (type_data.get("PriorityGoals") or {}).get("Items") or []
        src_counters = [int(x) for x in ((type_data.get("CounterIds") or {}).get("Items") or [])
                        if str(x).strip().isdigit()]
        if not (pg_items and int(counter_id) in src_counters):
            continue
        goals = []
        for g in pg_items:
            gid = g.get("GoalId")
            if gid in (None, ""):
                continue
            # Operation только SET: ADD отвергается API (3500).
            item = {"GoalId": int(gid), "Operation": "SET"}
            if g.get("Value") is not None:
                item["Value"] = int(g["Value"])
            if g.get("IsMetrikaSourceOfValue") is not None:
                item["IsMetrikaSourceOfValue"] = g["IsMetrikaSourceOfValue"]
            goals.append(item)
        if not goals:
            continue
        # Отдельным update ПОСЛЕ основного: отказ по целям не должен утащить CounterIds/стратегию.
        try:
            j = _v5_call("campaigns", "update", token, login,
                         {"Campaigns": [{"Id": int(tgt_id), struct_key: {"PriorityGoals": {"Items": goals}}}]})
            if "error" in j:
                warned += 1
                _copy_job_log(job_id, f"цели update {c.get('Name') or src_id}: {_v5_err(j)[:220]}")
        except Exception as e:  # noqa: BLE001
            warned += 1
            _copy_job_log(job_id, f"цели update {c.get('Name') or src_id}: {str(e)[:220]}")
    return {"updated": updated, "warned": warned}


def _copy_preseed_feed_maps(workdir: Path, feed_map: dict) -> None:
    """Предзаписать id_maps.json с пофидовым маппингом ДО phase_upload. direct_copy.phase_upload
    делает `maps = jload(id_maps.json) if exists` и для фида, уже присутствующего в maps['feeds'],
    пропускает создание (continue) → подставит наш целевой FeedId в группы/ShoppingAd/ListingAd.
    Пишем ПОЛНЫЙ скелет ключей — иначе phase_upload обратится к maps['shared_sets'] и упадёт KeyError."""
    maps_path = workdir / "id_maps.json"
    maps = _copy_read_json(maps_path) if maps_path.exists() else {}
    for key in ("shared_sets", "vcards", "images", "sitelinks", "callouts",
                "campaigns", "adgroups", "ads", "promotions", "feeds"):
        maps.setdefault(key, {})
    for sid, tid in (feed_map or {}).items():
        maps["feeds"][str(sid)] = int(tid)
    _copy_write_json(maps_path, maps)


def _copy_feeds_preview(source_login: str, target_login: str, selected_ids: set[int]) -> dict:
    """Данные для секции «Замена фидов»: фиды исходного аккаунта с кол-вом кампаний/групп
    из выбранных (selected_ids), фиды целевого аккаунта. Grid-фиды без балловой стоимости."""
    def _feeds_for(login: str) -> list[dict]:
        agency = _resolve_agency_hint(login, "")
        rows = _grid_feeds(login, agency) or []
        out = []
        for f in rows:
            fid = f.get("id")
            if not str(fid or "").strip().isdigit():
                continue
            out.append({
                "id": int(fid),
                "name": (f.get("name") or "").strip() or f"feed {fid}",
            })
        out.sort(key=lambda r: r["name"].lower())
        return out

    # Task 2: подсчёт выбранных кампаний/групп, использующих каждый исходный фид (v5 adgroups.get)
    feed_camps: dict[int, set] = {}   # feed_id → set of campaign_ids
    feed_groups: dict[int, int] = {}  # feed_id → count of adgroups
    if selected_ids:
        try:
            src_agency = _resolve_agency_hint(source_login, "")
            src_token, _ = _token_for_login(source_login, src_agency, _direct_tokens())
            if src_token:
                params = {
                    "SelectionCriteria": {"CampaignIds": list(selected_ids)},
                    "FieldNames": ["Id", "CampaignId"],
                    "TextAdGroupFeedParamFieldNames": ["FeedId"],
                }
                data = _v5_call("adgroups", "get", src_token, source_login, params)
                for ag in ((data.get("result") or {}).get("AdGroups") or []):
                    fp = ag.get("TextAdGroupFeedParams") or {}
                    fid_raw = fp.get("FeedId")
                    if not fid_raw:
                        continue
                    try:
                        fid = int(fid_raw)
                        cid = int(ag.get("CampaignId") or 0)
                    except (TypeError, ValueError):
                        continue
                    feed_camps.setdefault(fid, set()).add(cid)
                    feed_groups[fid] = feed_groups.get(fid, 0) + 1
        except Exception:  # noqa: BLE001 — best-effort, не ломаем превью
            pass

    source_feeds = []
    for f in _feeds_for(source_login):
        fid = f["id"]
        f["campaigns"] = len(feed_camps.get(fid) or set())
        f["groups"] = feed_groups.get(fid) or 0
        source_feeds.append(f)

    return {"source_feeds": source_feeds, "target_feeds": _feeds_for(target_login)}


def _copy_skip_unmapped_feed_campaigns(src_dir: Path, feed_map: dict, *, log=None) -> list[int]:
    """Task 4: убрать из snapshot кампании, использующие фиды без замены в feed_map.

    Читает campaigns.json и adgroups.json из уже отфильтрованного snapshot, определяет кампании,
    у которых хотя бы одна группа ссылается на фид, не входящий в feed_map, и удаляет их вместе
    со связанными сущностями. Возвращает список ID пропущенных кампаний."""
    if not feed_map:
        return []
    _log = log or (lambda _m: None)

    campaigns = _copy_read_json(src_dir / "campaigns.json")
    adgroups = _copy_read_json(src_dir / "adgroups.json")

    # feed_id → set campaign_ids (какие кампании используют этот фид)
    feeds_by_campaign: dict[int, set] = {}
    for g in adgroups:
        cid = int(g.get("CampaignId") or 0)
        fp = g.get("TextAdGroupFeedParams") or {}
        fid = fp.get("FeedId")
        if fid and cid:
            feeds_by_campaign.setdefault(cid, set()).add(str(int(fid)))

    mapped_feeds = {str(k) for k in feed_map}
    skip_ids: set[int] = set()
    for c in campaigns:
        cid = int(c.get("Id") or 0)
        unmapped = feeds_by_campaign.get(cid, set()) - mapped_feeds
        if unmapped:
            skip_ids.add(cid)
            _log(f"пропуск кампании «{c.get('Name') or cid}»: фиды без замены: {', '.join(sorted(unmapped))}")

    if not skip_ids:
        return []

    remaining_ids = {int(c.get("Id") or 0) for c in campaigns} - skip_ids
    _copy_write_json(src_dir / "campaigns.json", [c for c in campaigns if int(c.get("Id") or 0) in remaining_ids])
    _copy_write_json(src_dir / "campaigns_skipped.json", [
        {"id": int(c.get("Id") or 0), "name": c.get("Name") or "", "reason": "feed_not_mapped"}
        for c in campaigns if int(c.get("Id") or 0) in skip_ids
    ])

    remaining_ag_ids = {int(g.get("Id") or 0) for g in adgroups if int(g.get("CampaignId") or 0) in remaining_ids}
    _copy_write_json(src_dir / "adgroups.json", [g for g in adgroups if int(g.get("CampaignId") or 0) in remaining_ids])

    for fname in ("ads.json", "shopping_ads.json", "keywords.json", "bidmodifiers.json"):
        path = src_dir / fname
        if not path.exists():
            continue
        items = _copy_read_json(path)
        _copy_write_json(path, [
            x for x in items
            if int(x.get("CampaignId") or 0) in remaining_ids
            or int(x.get("AdGroupId") or 0) in remaining_ag_ids
        ])

    _log(f"feed-фильтрация: пропущено {len(skip_ids)} кампаний из {len(campaigns)}")
    return list(skip_ids)


def _copy_target_campaigns_info(login: str) -> dict:
    """Read-only снимок кампаний целевого аккаунта для UI выбора очистки.

    Возвращает {total, draft_count, non_draft_count, archivable_count, breakdown}.
    DRAFT не могут быть заархивированы (v5 API error 8303) →
    archivable_count = non_draft_count (не-черновики, не архивированные).
    campaigns.get без фильтра по умолчанию не возвращает ARCHIVED-кампании.
    """
    try:
        agency = _resolve_agency_hint(login, "")
        token, _ = _token_for_login(login, agency, _direct_tokens())
        if not token:
            return {"error": "нет рабочего токена для этого логина"}
        j = _v5_call("campaigns", "get", token, login, {
            "FieldNames": ["Id", "State", "Status"],
            "SelectionCriteria": {},
        })
        if "error" in j:
            err_text = (_v5_err(j) if callable(_v5_err) else str(j.get("error")))
            return {"error": (err_text or "API error")[:200]}
        campaigns = (j.get("result") or {}).get("Campaigns", [])
        draft_count = sum(1 for c in campaigns if c.get("Status") == "DRAFT")
        non_draft_count = len(campaigns) - draft_count
        breakdown: dict[str, int] = {}
        for c in campaigns:
            key = f"{c.get('State') or '?'}/{c.get('Status') or '?'}"
            breakdown[key] = breakdown.get(key, 0) + 1
        return {
            "total": len(campaigns),
            "draft_count": draft_count,
            "non_draft_count": non_draft_count,
            # DRAFT нельзя архивировать (v5 8303); archivable = только не-черновики
            "archivable_count": non_draft_count,
            "breakdown": breakdown,
        }
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


def _copy_cleanup_uac_drafts(job_id: str, login: str, errors: list[str]) -> int:
    """Удалить UAC/МК-ЧЕРНОВИКИ целевого логина по кукам (v5 их не видит → дубли +3 за прогон).

    Удаляем ТОЛЬКО status=DRAFT и не-archived: запущенные кампании не трогаем.
    Сбой кук — не критичен (v5-ветка уже отработала): пишем в errors, копирование не рвём.
    """
    try:
        rows = _grid_list_campaigns(login) or []
    except Exception as e:  # noqa: BLE001
        errors.append(f"cleanup uac list: {str(e)[:120]}")
        return 0
    ids: list[int] = []
    for row in rows:
        if str(row.get("status") or "").upper() != "DRAFT" or row.get("archived"):
            continue
        if not _copy_is_uac_grid_row(row):
            continue
        try:
            cid = int(row.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if cid > 0:
            ids.append(cid)
    if not ids:
        return 0
    _copy_job_log(job_id, f"cleanup delete_drafts: МК-черновиков по кукам {len(ids)} → удаляю")
    try:
        res = gc.GridCreateClient(login).delete_campaigns(ids)
    except Exception as e:  # noqa: BLE001
        errors.append(f"cleanup uac delete: {str(e)[:120]}")
        return 0
    for err in (res.get("errors") or []):
        errors.append(f"uac delete: {str(err)[:100]}")
    return len(res.get("deleted") or [])


def _copy_target_cleanup(job_id: str, login: str, agency: str, mode: str) -> dict:
    """Очистить целевой аккаунт ДО копирования.

    mode='delete_drafts': удаляет только кампании со Status=DRAFT (v5 campaigns.delete,
        без archive-шага — DRAFT удаляются напрямую, подтверждено тестом 2026-07-17).
    mode='archive':       архивирует все non-DRAFT кампании. ON-кампании сначала
        suspend, потом archive. DRAFT пропускаются (v5 8303).

    Возвращает {ok, deleted, archived, skipped_non_draft|skipped_draft, errors}.
    Поднимает RuntimeError при критической ошибке API (не per-item) — _copy_run_job
    тогда прерывает копирование, не льёт РК в неочищенный аккаунт.
    """
    if mode not in ("delete_drafts", "archive"):
        return {"ok": True, "deleted": 0, "archived": 0, "skipped": 0, "errors": []}

    ag = agency or _resolve_agency_hint(login, "")
    token, _ = _token_for_login(login, ag, _direct_tokens())
    if not token:
        raise RuntimeError(f"cleanup: нет рабочего токена для {login!r}")

    _copy_job_log(job_id, f"cleanup {mode}: получаю кампании {login}")
    j = _v5_call("campaigns", "get", token, login, {
        "FieldNames": ["Id", "Name", "State", "Status"],
        "SelectionCriteria": {},
    })
    if "error" in j:
        raise RuntimeError(f"cleanup campaigns.get: {(_v5_err(j) if callable(_v5_err) else str(j.get('error')))[:200]}")

    campaigns = (j.get("result") or {}).get("Campaigns", [])
    errors: list[str] = []

    if mode == "delete_drafts":
        drafts = [c["Id"] for c in campaigns if c.get("Status") == "DRAFT"]
        non_draft_skip = len(campaigns) - len(drafts)
        _copy_job_log(job_id, f"cleanup delete_drafts: к удалению {len(drafts)}, пропускаем не-черновики {non_draft_skip}")
        deleted = 0
        for i in range(0, len(drafts), 100):
            chunk = drafts[i:i + 100]
            jd = _v5_call("campaigns", "delete", token, login, {"SelectionCriteria": {"Ids": chunk}})
            if "error" in jd:
                raise RuntimeError(f"cleanup delete batch: {(_v5_err(jd) if callable(_v5_err) else str(jd.get('error')))[:200]}")
            for rr in (jd.get("result") or {}).get("DeleteResults", []):
                if rr.get("Id") and not rr.get("Errors"):
                    deleted += 1
                else:
                    errors.append(f"delete {rr.get('Id')}: {str(rr.get('Errors') or 'unknown')[:100]}")
        # МК/tp6 невидимы для v5 (campaigns.get их не отдаёт) → копятся дублями: чистим по кукам.
        deleted += _copy_cleanup_uac_drafts(job_id, login, errors)
        _copy_job_log(job_id, f"cleanup delete_drafts: удалено {deleted}, ошибок {len(errors)}, пропущено {non_draft_skip}")
        return {"ok": True, "deleted": deleted, "archived": 0,
                "skipped_non_draft": non_draft_skip, "errors": errors}

    # mode == "archive"
    non_draft = [c for c in campaigns if c.get("Status") != "DRAFT"]
    draft_skip = len(campaigns) - len(non_draft)
    on_ids = [c["Id"] for c in non_draft if c.get("State") == "ON"]
    archive_ids = [c["Id"] for c in non_draft]
    _copy_job_log(job_id, f"cleanup archive: к архивации {len(archive_ids)}, suspend ON {len(on_ids)}, пропускаем DRAFT {draft_skip}")

    if on_ids:
        for i in range(0, len(on_ids), 100):
            chunk = on_ids[i:i + 100]
            js = _v5_call("campaigns", "suspend", token, login, {"SelectionCriteria": {"Ids": chunk}})
            if "error" in js:
                raise RuntimeError(f"cleanup suspend: {(_v5_err(js) if callable(_v5_err) else str(js.get('error')))[:200]}")
            for rr in (js.get("result") or {}).get("SuspendResults", []):
                if rr.get("Errors"):
                    errors.append(f"suspend {rr.get('Id')}: {str(rr['Errors'])[:80]}")

    archived = 0
    for i in range(0, len(archive_ids), 100):
        chunk = archive_ids[i:i + 100]
        ja = _v5_call("campaigns", "archive", token, login, {"SelectionCriteria": {"Ids": chunk}})
        if "error" in ja:
            raise RuntimeError(f"cleanup archive batch: {(_v5_err(ja) if callable(_v5_err) else str(ja.get('error')))[:200]}")
        for rr in (ja.get("result") or {}).get("ArchiveResults", []):
            if rr.get("Id") and not rr.get("Errors"):
                archived += 1
            else:
                errors.append(f"archive {rr.get('Id')}: {str(rr.get('Errors') or 'unknown')[:100]}")

    _copy_job_log(job_id, f"cleanup archive: заархивировано {archived}, пропущено DRAFT {draft_skip}, ошибок {len(errors)}")
    return {"ok": True, "deleted": 0, "archived": archived,
            "skipped_draft": draft_skip, "errors": errors}


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
    mode = (body.get("mode") or "auto").strip()
    geo_mode = (body.get("geo_mode") or "replace").strip()
    provided_image_hashes = [str(h).strip() for h in (body.get("image_hashes") or []) if str(h).strip()]
    # Пофидовая замена (source_feed_id → target_feed_id, только существующие фиды target-аккаунта).
    # Пусто → поведение как раньше (единый target_feed_url / авто-пересоздание URL-фидов).
    # mode="other": feed_map строится автоматически (auto-match по URL/имени файла); пользовательский feed_map игнорируется.
    feed_map_raw: dict[str, int] = {}
    if mode == "other":
        # Авто-подбор фидов — та же эвристика, что JS _feedMatchTarget (full path → filename match)
        try:
            feed_map_raw = _copy_auto_feed_map(source_login, target_login)
        except Exception:  # noqa: BLE001 — best-effort
            feed_map_raw = {}
    else:
        _fm = body.get("feed_map")
        if not isinstance(_fm, dict):
            _fm = {}
        for _k, _v in _fm.items():
            if str(_k).strip().isdigit() and str(_v).strip().isdigit() and int(_v) > 0:
                feed_map_raw[str(int(_k))] = int(_v)
    use_feed_map = bool(feed_map_raw)
    # target_cleanup: 'none' | 'delete_drafts' | 'archive' — очистка цели ДО копирования.
    # Инициализируем вне try, чтобы cleanup_result не потерялся при ошибке.
    target_cleanup = (body.get("target_cleanup") or "none").strip()
    if target_cleanup not in ("none", "delete_drafts", "archive"):
        target_cleanup = "none"
    cleanup_result: dict | None = None
    try:
        # === ШАГ 0: Очистка целевого аккаунта (ДО pull/upload) ===
        if target_cleanup != "none":
            _copy_job_upsert(job_id, status="running", progress=2)
            _copy_job_log(job_id, f"cleanup: начало ({target_cleanup}) на {target_login}")
            target_ag_cleanup = body.get("agency") or _resolve_agency_hint(target_login, "")
            cleanup_result = _copy_target_cleanup(job_id, target_login, target_ag_cleanup, target_cleanup)
            # Per-item errors — предупреждения, не останавливаем; критические ошибки уже вызвали RuntimeError
            for err in (cleanup_result.get("errors") or [])[:5]:
                _copy_job_log(job_id, f"cleanup предупреждение: {err}")
            # Немедленно фиксируем факт очистки в статусе job — ДО pull/upload.
            # Это гарантирует, что пользователь увидит удалённые/заархивированные кампании
            # даже если последующие шаги (snapshot/pull/upload) упадут с ошибкой.
            _copy_job_upsert(job_id, result={"cleanup": cleanup_result})

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
        # ФАЗА 1 (П.11/П.10): зафиксировать исходную связь campaign→callouts/promo с ИСТОЧНИКА (Grid).
        # Best-effort: недоступность Grid не валит копирование — постпроцесс откатится на union/единичное.
        try:
            from . import copy_steps as _csteps
            _src_camp_ids = [int(c["Id"]) for c in _copy_read_json(src_dir / "campaigns.json")
                             if str(c.get("Id") or "").isdigit()]
            _src_cli = cmc.build_client(source_login, account=(source_agency or None))
            _src_grid = gf.GridClient(source_login, cookie=(_src_cli.sess.headers.get("Cookie") or ""))
            _pa = _csteps.pull_source_campaign_assets(
                _src_grid, _src_camp_ids, src_dir, log=(lambda m: _copy_job_log(job_id, m)))
            if _pa.get("errors"):
                _copy_job_log(job_id, f"pull source assets warnings: {'; '.join(_pa['errors'][:3])[:220]}")
        except Exception as e:  # noqa: BLE001
            _copy_job_log(job_id, f"pull source assets: пропуск ({str(e)[:180]})")
        expected_snapshot = max(0, len(selected_ids) - len(selected_uac_rows))
        if int(meta.get("campaigns") or 0) != expected_snapshot:
            raise RuntimeError(
                f"snapshot неполный: выбрано {len(selected_ids)}, UAC/tp6/tp7 {len(selected_uac_rows)}, "
                f"в v5 snapshot {meta.get('campaigns')} вместо {expected_snapshot}"
            )
        # Task 4: пропустить кампании с фидами без замены (только если feed_map задан)
        if feed_map_raw:
            skipped_cids = _copy_skip_unmapped_feed_campaigns(
                src_dir, feed_map_raw, log=lambda m: _copy_job_log(job_id, m))
            if skipped_cids:
                remaining = int(meta.get("campaigns") or 0) - len(skipped_cids)
                _copy_job_upsert(job_id, total=max(0, remaining))
        target_feed_abs = dc.build_url_feed_url(target_domain, target_feed_url) if target_feed_url else ""
        audit = _copy_snapshot_preflight(
            src_dir,
            # feed_map покрывает фиды пофидово → сентинел удовлетворяет проверку «целевой фид задан».
            target_feed_url=(target_feed_abs or ("__feed_map__" if use_feed_map else "")),
            target_city=target_city,
            target_region=target_region,
            geo_mode=geo_mode,
        )
        _copy_job_upsert(job_id, preflight=audit)
        for msg in audit.get("warnings") or []:
            _copy_job_log(job_id, f"preflight warning: {msg}")
        if audit.get("critical"):
            for msg in audit["critical"]:
                _copy_job_log(job_id, f"preflight error: {msg}")
            raise RuntimeError("preflight остановил копирование: " + "; ".join(audit["critical"][:3]))

        # ── Ветка (б): гео-морфология ─────────────────────────────────────────────────
        if mode == "other" and geo_mode == "keep":
            # Пропускаем гео-замену целиком: M3 не вызывается, snapshot не трогаем.
            rewrite_meta = {"files": 0, "replacements": 0, "pairs": [], "m3_used": False, "residual_geo": []}
            _copy_job_upsert(job_id, context_rewrite=rewrite_meta)
            _copy_job_log(job_id, "гео: режим 'keep' — морфологическая замена пропущена")
        else:
            source_ctx = _copy_ctx(source_login)
            if mode == "other" and geo_mode == "change":
                # mode="other": гео-замена по выбранному региону (если правило морфологии выполнено).
                # Морфология: только 1 плюс-регион, 0 минусов, тип НЕ World/Country.
                _gids_raw_v5 = body.get("geo_region_ids") or []
                if not _gids_raw_v5:
                    _s = int(body.get("geo_region_id") or 0)
                    _gids_raw_v5 = [_s] if _s else []
                _pos_v5 = [int(x) for x in _gids_raw_v5 if str(x).lstrip("-").isdigit() and int(x) > 0]
                _neg_v5 = [int(x) for x in _gids_raw_v5 if str(x).lstrip("-").isdigit() and int(x) < 0]
                _mtype_v5 = (_geo_type_by_id(_pos_v5[0]) if _geo_type_by_id and _pos_v5 else None) or ""
                _do_morph_v5 = (len(_pos_v5) == 1 and len(_neg_v5) == 0
                                and _mtype_v5 not in ("World", "Country"))
                if _do_morph_v5 and _pos_v5:
                    geo_rname = (_geo_name_by_id(_pos_v5[0]) if _geo_name_by_id else "") or ""
                    if not geo_rname:
                        raise RuntimeError(
                            f"geo_region_id={_pos_v5[0]}: имя региона не найдено в справочнике GeoRegions"
                        )
                    target_ctx = {"city": "", "region": geo_rname}
                    _copy_job_log(job_id, f"гео: 1 регион, меняем тексты: region_id={_pos_v5[0]} name={geo_rname!r}")
                else:
                    # Несколько регионов / есть минусы / страна → тексты не трогаем
                    rewrite_meta = {"files": 0, "replacements": 0, "pairs": [], "m3_used": False, "residual_geo": []}
                    _copy_job_upsert(job_id, context_rewrite=rewrite_meta)
                    _copy_job_log(job_id,
                                  f"гео: {len(_pos_v5)} регион(ов), {len(_neg_v5)} исключений"
                                  f" → тексты не меняем (RegionIds ставим)")
                    target_ctx = None  # сигнал: морфологию пропустить
            else:
                target_ctx = _copy_ctx(target_login)
                target_ctx["city"] = target_city or target_ctx.get("city") or ""
                target_ctx["region"] = target_region or target_ctx.get("region") or ""
            if target_ctx is not None:
                rewrite_meta = _copy_rewrite_snapshot_context(
                    src_dir, source_ctx, target_ctx, log=(lambda m: _copy_job_log(job_id, m))
                )
                _copy_job_upsert(job_id, context_rewrite=rewrite_meta)
                if rewrite_meta.get("m3_used"):
                    _copy_job_log(job_id, "гео-склонения: M3 парадигма падежей применена")
                else:
                    _copy_job_log(job_id, "гео-склонения: M3 недоступен, замена только по границам слов (именительный)")
                if rewrite_meta.get("m3_failed"):
                    _copy_job_log(job_id, f"гео-склонения: фолбэк для {', '.join(rewrite_meta['m3_failed'][:4])}")
                if rewrite_meta.get("replacements"):
                    _copy_job_log(job_id, f"гео в snapshot заменено: {rewrite_meta['replacements']} в {rewrite_meta['files']} файлах")
                if mode != "other" and rewrite_meta.get("residual_geo"):
                    # Для mode="other" residual-проверку пропускаем: источник может быть вне Краснодара/etc.
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
        # ── Ветка (а): RegionIds ──────────────────────────────────────────────────────
        if mode == "other" and geo_mode == "keep":
            # GeoRegionId копируется из снимка как есть (phase_upload c tgt_region_id=None).
            _copy_job_log(job_id, "гео: режим 'keep' — RegionIds из источника без изменений")
        elif mode == "other" and geo_mode == "change":
            # tgt_region_id = список int (положительные + отрицательные); backward compat со скаляром.
            _gids_v5_b = body.get("geo_region_ids") or []
            if not _gids_v5_b:
                _s_b = int(body.get("geo_region_id") or 0)
                _gids_v5_b = [_s_b] if _s_b else []
            _gids_v5 = [int(x) for x in _gids_v5_b if str(x).lstrip("-").isdigit() and int(x) != 0]
            if not _gids_v5:
                raise RuntimeError("mode='other', geo_mode='change': geo_region_ids не задан в запросе")
            tgt_region_id = _gids_v5   # phase_upload получает список
            geo_source = f"other:region_ids={_gids_v5[:5]!r}"
            _copy_job_log(job_id, f"гео: режим 'change', region_ids={_gids_v5[:10]!r}")
        else:
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
        # Пофидовая замена: валидируем целевые фиды по аккаунту (только СВОИ фиды) и предзаписываем
        # id_maps.json — phase_upload загрузит его и подставит целевые фиды вместо единого forced-фида.
        feed_map_valid: dict[str, int] = {}
        if use_feed_map:
            _tgt_feed_ids_ok = True
            try:
                _tgt_feed_ids = {
                    int(f.get("id")) for f in _grid_feeds(target_login, target_token_agency or _resolve_agency_hint(target_login, ""))
                    if str(f.get("id") or "").strip().isdigit()
                }
            except Exception:  # noqa: BLE001
                _tgt_feed_ids = set()
                _tgt_feed_ids_ok = False
            if not _tgt_feed_ids_ok or not _tgt_feed_ids:
                # Grid недоступен или вернул пустой список — не можем проверить, доверяем вводу
                feed_map_valid = dict(feed_map_raw)
                _copy_job_log(job_id, "feed_map: не удалось получить фиды target (grid недоступен или список пуст) — feed_map применён без валидации")
            else:
                for _sid, _tid in feed_map_raw.items():
                    if _tid in _tgt_feed_ids:
                        feed_map_valid[_sid] = _tid
                    else:
                        _copy_job_log(job_id, f"feed_map: целевой фид {_tid} не принадлежит {target_login} — пропуск (source {_sid})")
            use_feed_map = bool(feed_map_valid)
            if use_feed_map:
                _copy_preseed_feed_maps(workdir, feed_map_valid)
                _copy_job_log(job_id, f"пофидовая замена активна: {feed_map_valid}")
        _copy_job_upsert(job_id, progress=42, feed_map=feed_map_valid)
        _copy_job_log(job_id, f"upload в {target_login} (домен={target_domain or '—'}, geo={target_city or target_region or '—'} #{tgt_region_id or '—'} {geo_source}, feed={'по карте' if use_feed_map else (target_feed_abs or '—')})")
        dc.phase_upload(
            src_dir, workdir, tgt_auth, source_login, target_login,
            src_domain, target_domain, tgt_region_id,
            force_feed_url=("" if use_feed_map else target_feed_abs),
            force_feed_name=(None if use_feed_map else (target_feed_abs.rsplit("/", 1)[-1] if target_feed_abs else None)),
            skip_keywords=True,   # ФАЗА 3c п.2: ключи — Grid-first в постпроцессе (0 v5-баллов)
            # mode="other": картинки сайта, загруженные пользователем в целевой аккаунт. Без этого
            # v5-ветка перезаливала картинку ИСТОЧНИКА (тот же контент → тот же хэш) и на объявлениях
            # оставался чужой сайт, а загруженные не использовались (прогон 2026-07-17).
            image_hashes=([str(h).strip() for h in (body.get("image_hashes") or []) if str(h).strip()]
                          or None),
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
            # tgt_region_id теперь может быть списком (geo_region_ids), скаляром или None
            if isinstance(tgt_region_id, list):
                region_id_list = tgt_region_id if tgt_region_id else [225]
            else:
                region_id_list = [int(tgt_region_id)] if tgt_region_id else [225]
            target_href = _copy_target_href(None, "", target_domain)
            _copy_job_log(job_id, f"uac copy: {len(selected_uac_rows)} → {target_login} (feed={target_feed_id or '—'})")
            uac_copy = _copy_uac_campaigns(
                source_login, target_login, target_agency or "", selected_uac_rows, body,
                target_href=target_href, region_ids=region_id_list, counter_id=counter_id,
                goal_id=goal_id, target_feed_id=target_feed_id, feed_map=feed_map_valid,
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
        skipped_camps = _copy_read_json(src_dir / "campaigns_skipped.json")
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
                "skipped_campaigns": skipped_camps,
                "live_verification": cookie_post.get("live_verification"),
                "repair_gate": cookie_post.get("repair_gate"),
                "auto_repair": cookie_post.get("auto_repair"),
                "preflight": audit,
                "context_rewrite": rewrite_meta,
                "target_feed_url": target_feed_abs,
                "workdir": str(workdir),
                "cleanup": cleanup_result,
            })
    except BaseException as e:  # noqa: BLE001
        # cleanup_result инициализирован вне try → всегда доступен здесь.
        # Если очистка отработала до падения job — явно включаем её в result,
        # чтобы пользователь видел факт удаления/архивации в карточке статуса.
        _err_result = {"cleanup": cleanup_result} if cleanup_result is not None else None
        _copy_job_upsert(job_id, status="error", error=str(e)[:500], progress=100,
                         **({} if _err_result is None else {"result": _err_result}))
        _copy_job_log(job_id, f"ошибка: {str(e)[:300]}")
    finally:
        # Артефакты оставляем во временной папке до ручной очистки — полезно для отладки id_maps/upload_log.
        pass




def _copy_jobs_recover() -> None:
    """Старт copy-сервиса (direct-copy.service): осиротевшие copy-джобы (running/queued) → interrupted.
    Трогает ТОЛЬКО kind='copy_campaigns' — очередь создания РК в direct.service не задета.
    Авто-докрутку не делаем: повторный «Копировать» сам пропустит уже созданное (суффикс _vNN)."""
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE public.direct_automation_jobs SET status='interrupted', updated_at=now() "
                        "WHERE kind='copy_campaigns' AND status IN ('running','queued')")
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


# ── copy_other: ре-экспорт функций вкладки «Прочие сферы» ───────────────────
# copy_other не импортирует copy_engine на уровне модуля → цикла нет.
# DI (_grid_feeds, _resolve_agency_hint, _copy_feeds_preview) берётся из
# copy_engine._xxx в рантайме (ленивый import внутри тел функций copy_other).
from .copy_other import (                                    # noqa: E402
    _ARCHIVE_MAX_FILES, _ARCHIVE_MAX_BYTES, _IMAGE_EXTS,
    _feed_url_path, _feed_url_file, _feed_auto_match_one,
    _copy_auto_feed_map, _copy_feeds_check,
    _extract_archive_images, _copy_images_upload,
)

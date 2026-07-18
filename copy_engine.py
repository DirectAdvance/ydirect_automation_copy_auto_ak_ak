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
    """Инъекция зависимостей из blueprint (Direct API/токены/Grid/очередь).

    Фан-аут: те же deps раздаются извлечённым суб-модулям распила (у каждого свой
    globals().update — берёт нужные ключи). Модули импортируются ниже (ре-экспорт распила),
    к моменту вызова configure() (runtime, после load) имена уже связаны.
    """
    globals().update(deps)
    for _sub in (copy_jobs, copy_geo, copy_snapshot, copy_images, copy_metrika,
                 copy_feeds, copy_grid_read, copy_uac, copy_cleanup, copy_grid_steps):
        try:
            _sub.configure(deps)
        except Exception:  # noqa: BLE001 — фан-аут best-effort, не валит основную инъекцию
            pass


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




























# _REGION_ALIASES / _REGION_ALIASES_NORM / _norm_region_alias_key перенесены в copy_geo.py
# (распил): их использует _copy_geo_replacements там же. Ре-экспорт — ниже в блоке copy_geo.


def _copy_geo_filter_negatives(minus_list: list, replacements: list) -> list:
    """Задача 1: убрать из минус-слов те, что содержат форму ЦЕЛЕВОГО города/области.

    После гео-замены «Краснодар» в минусах стал «Москва» — если целевой аккаунт в Москве,
    он заминусует сам себе целевой город и потеряет показы. Фильтруем таких.
    Пустой replacements (geo_mode=keep / нет гео-замены) → без изменений."""
    if not minus_list or not replacements:
        return list(minus_list)
    # Правые части пар = формы ЦЕЛЕВОГО гео (то, во что заменяем).
    target_forms = sorted(
        {new.lower() for _, new in replacements if (new or "").strip()},
        key=len, reverse=True,
    )
    if not target_forms:
        return list(minus_list)
    result = []
    for m in minus_list:
        low = (m or "").lower()
        blocked = any(
            re.search(r"\b" + re.escape(tf) + r"\b", low, re.UNICODE)
            for tf in target_forms
        )
        if not blocked:
            result.append(m)
    return result










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
    # [geo-честность] ключи и минусы для check_geo_kw_consistency в ЕПК-пути.
    # Ключи уже гео-заменены (copy_engine:1237), минусы — гео-заменены и отфильтрованы (:1240-1243).
    snap_keywords_json: list[dict] = []

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
                # Задача 1: (а) применить гео-замену к группо-уровневым минусам (как к ключам);
                # (б) убрать минусы, содержащие форму ЦЕЛЕВОГО города — иначе target заминусует себя.
                "minus": _copy_geo_filter_negatives(
                    [_copy_apply_geo_replacements(m, replacements)
                     for m in (grp.get("minus_keywords") or []) if str(m or "").strip()],
                    replacements),
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
            # [geo-честность] snap_adgroups включает NegativeKeywords (уже гео-заменённые +
            # отфильтрованные _copy_geo_filter_negatives) и собираем snap_keywords.
            # zip корректен: group_specs и src_groups строятся из одного src_groups в том же порядке.
            for grp, spec in zip(src_groups, group_specs):
                gid = int(grp.get("adgroup_id") or 0)
                if gid > 0:
                    snap_adgroups_json.append({
                        "Id": gid, "CampaignId": int(old_cid),
                        "Name": str(grp.get("adgroup_name") or "группа"),
                        "NegativeKeywords": {"Items": list(spec.get("minus") or [])},
                    })
                    for _kw in (spec.get("keywords") or []):
                        if _kw and str(_kw).strip():
                            snap_keywords_json.append({"Keyword": str(_kw), "AdGroupId": gid,
                                                       "CampaignId": int(old_cid)})

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
            # [geo-честность] keywords.json: уже гео-заменённые ключи для check_geo_kw_consistency
            _copy_write_json(src_dir / "keywords.json", snap_keywords_json)
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


























def _copy_timed(job_id: str, label: str, fn):
    """Обёртка-таймер фазы постпроцесса: логирует `[timing] <label>: Ns`.

    Замер #23 (профиль скорости копирования): постпроцесс+verify — ~76% времени копии,
    но лог фаз без таймстампов не показывал ВНУТРЕННИЙ хог. Тайминг лёгкий (monotonic),
    остаётся навсегда — видно, какую фазу распараллеливать/батчить."""
    _t = time.monotonic()
    try:
        return fn()
    finally:
        _copy_job_log(job_id, f"[timing] {label}: {time.monotonic() - _t:.0f}s")


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

    # 1a) Промоакции: official promotions.add заблокирован (8000/ЕРИР) → переносим через Grid
    # addPromoExtensions. КЛЮЧЕВОЕ (root-cause привязки): создаём промо ИЗ source
    # campaigns_edit_rows (promoExtension), тогда maps["promotions"] ключуется по promoExtension.id,
    # совпадающему с campaign_promos.json → step_attach_promos привязывает per-campaign даже при
    # 2+ промо. Раньше создавали из promotions.json (ключ = promotions.json.Id) ≠ promoExtension.id
    # из campaign_promos.json → by_promo пуст → fallback_single → при 10 промо не привязывало.
    # Фолбэк (нет source_grid / нет промо в edit_rows): прежний путь из promotions.json.
    promotions = _copy_read_json(src_dir / "promotions.json")
    created_promo_ids = []
    if promotions or cstep_ctx.source_grid is not None:
        try:
            from .promo import PromoClient
            pc = PromoClient(client, target_login)
            _src_dom = str(body.get("_copy_source_domain") or "")
            _tgt_dom = str(body.get("target_domain") or "")
            _used_edit_rows = False
            if cstep_ctx.source_grid is not None:
                _src_cids = [int(x) for x in (maps.get("campaigns") or {}).keys() if str(x).isdigit()]
                _src_rows = cstep_ctx.source_grid.campaigns_edit_rows(_src_cids) if _src_cids else {}
                _promo_defs: dict[str, dict] = {}
                _cp_links: dict[str, str] = {}
                for _cid, _row in (_src_rows or {}).items():
                    _pe = _row.get("promoExtension") or {}
                    _pid = str(_pe.get("id") or "")
                    if not _pid or _pid == "0":
                        continue
                    _cp_links[str(_cid)] = _pid
                    if _pe.get("description"):
                        _promo_defs[_pid] = _pe
                if _promo_defs:
                    _used_edit_rows = True
                    for _src_pid, _pdef in _promo_defs.items():
                        if str(_src_pid) in maps["promotions"]:
                            created_promo_ids.append(int(maps["promotions"][str(_src_pid)]))
                            continue
                        _npid, _perr = pc.add(
                            type=_pdef.get("type") or "DISCOUNT",
                            description=_pdef.get("description") or "акция",
                            href=_copy_target_href(_pdef.get("href"), _src_dom, _tgt_dom),
                            amount=_pdef.get("amount"), unit=_pdef.get("unit"),
                            prefix=_pdef.get("prefix"), promocode=_pdef.get("promocode"),
                            start=_pdef.get("startDate"), finish=_pdef.get("finishDate"))
                        if _npid:
                            maps["promotions"][str(_src_pid)] = int(_npid)
                            created_promo_ids.append(int(_npid))
                        elif _perr:
                            rep["errors"].append(f"promo {_src_pid}: {_perr[:180]}")
                    if _cp_links:
                        (src_dir / "campaign_promos.json").write_text(
                            json.dumps(_cp_links, ensure_ascii=False, indent=1), encoding="utf-8")
                        _copy_job_log(job_id, f"промо: создано из source edit_rows "
                                              f"({len(_promo_defs)} промо, {len(_cp_links)} связей)")
            if not _used_edit_rows:
                for p in (promotions or []):
                    src_id = str(p.get("Id") or "")
                    if src_id and src_id in maps["promotions"]:
                        created_promo_ids.append(int(maps["promotions"][src_id]))
                        continue
                    pid, perr = pc.add(
                        type=p.get("Type") or "DISCOUNT",
                        description=p.get("Description") or p.get("Name") or "акция",
                        href=_copy_target_href(p.get("Href"), _src_dom, _tgt_dom),
                        amount=p.get("Amount"), unit=p.get("AmountUnit"),
                        prefix=p.get("AmountPrefix"), promocode=p.get("Promocode"),
                        start=p.get("StartDate"), finish=p.get("EndDate"))
                    if pid:
                        maps["promotions"][src_id] = int(pid)
                        created_promo_ids.append(int(pid))
                    elif perr:
                        rep["errors"].append(f"promo {src_id}: {perr[:180]}")
            rep["promos_created"] = len(created_promo_ids)
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
        kw_rep = _copy_timed(job_id, "keywords", lambda: csteps.step_keywords(cstep_ctx))
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
        rep["organic_placement"] = _copy_timed(job_id, "organic_placement", lambda: csteps.step_fix_organic_placement(cstep_ctx))
        rep["errors"] += rep["organic_placement"].get("errors") or []
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"organic placement: {str(e)[:200]}")

    # ФИНАЛ: сверка настроек источник ↔ копия ПО КУКАМ (report-only, 0 v5-баллов).
    # Идёт ПОСЛЕДНЕЙ — после всех добивок (цены/видео/адаптивы/стратегия), иначе сравнивали бы
    # промежуточное состояние. Ловит молчаливые потери, которых v5 не показывает (минус-слова
    # кампаний, brandSafety, временной таргетинг, contextLimit и пр.). Расхождения — в отчёт джоба;
    # автопочинка тут НЕ делается: чинит adjacent-шаг repair, а этот честно показывает факт.
    try:
        rep["settings_diff"] = _copy_timed(job_id, "settings_diff", lambda: csteps.step_settings_diff(cstep_ctx))
        rep["errors"] += (rep["settings_diff"].get("errors") or [])
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"settings diff: {str(e)[:200]}")

    # П.14: стандартные возрастные корректировки −100% (<18, 18–24) через v5.
    try:
        rep["age_bidmods"] = _copy_timed(job_id, "age_bidmods", lambda: csteps.step_age_bidmods(cstep_ctx))
        rep["errors"] += rep["age_bidmods"].get("errors") or []
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"age bidmods: {str(e)[:200]}")
    # П.13: наш стандартный disabledPlaces на скопированные РСЯ-кампании (Grid).
    try:
        rep["disabled_places"] = _copy_timed(job_id, "disabled_places", lambda: csteps.step_disabled_places(cstep_ctx))
        rep["errors"] += rep["disabled_places"].get("errors") or []
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"disabled places: {str(e)[:200]}")
    # П.4 (ФАЗА 3b): адаптивные креативы 1:1 по куки (Grid) — заголовки/тексты/картинки источника,
    # гео в тексте с падежами; БЕЗ исходного CreativeId и БЕЗ v5-баллов. ДО step_prices, чтобы
    # adPrice лёг на уже приведённый 1:1 контент (RMW step_prices его сохранит).
    try:
        rep["adaptive_creatives"] = _copy_timed(job_id, "adaptive_creatives", lambda: csteps.step_adaptive_creatives(cstep_ctx))
        rep["errors"] += rep["adaptive_creatives"].get("errors") or []
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"adaptive creatives: {str(e)[:200]}")

    # П.8: НОВЫЕ РЕАЛЬНЫЕ цены из ФИДА target-аккаунта на созданные адаптивные объявления (Grid adPrice).
    try:
        rep["prices"] = _copy_timed(job_id, "prices", lambda: csteps.step_prices(cstep_ctx))
        rep["errors"] += rep["prices"].get("errors") or []
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"prices: {str(e)[:200]}")

    # П.12 (ФАЗА 3b/3c): видео 1:1 по куки — ПОСЛЕ prices (attach через RMW сохраняет контент/цену,
    # а step_prices через _grid_set_ad_prices слал creativeIds=[] → до него видео стерлось бы).
    # ФАЗА 3c: video_file_resolver теперь заполнен (originalUrl из Grid-интроспекции) → видео
    # реально переносится (скачать mp4 → аплоуд по куки → RMW-привязка). Нет URL/скачивания —
    # честный report-only (внутри step_videos).
    try:
        rep["videos"] = _copy_timed(job_id, "videos", lambda: csteps.step_videos(cstep_ctx))
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
    _t_verify = time.monotonic()
    try:
        from . import copy_verify as cv
        verify_result = cv.run_copy_verification(
            src_dir=src_dir, workdir=workdir,
            target_login=target_login, target_agency=target_agency,
            grid=grid, source_grid=cstep_ctx.source_grid,
            geo_pairs=cstep_ctx.geo_pairs or [],
            log=(lambda m: _copy_job_log(job_id, m)),
        )
        _copy_job_log(job_id, f"[timing] copy_verify: {time.monotonic() - _t_verify:.0f}s")
        rep["copy_verify"] = verify_result
        _s = verify_result.get("summary") or {}
        _copy_job_log(job_id, f"copy_verify: ok={_s.get('ok')}, mismatch={_s.get('mismatch')}, "
                              f"missing={_s.get('missing')}, unreadable={_s.get('unreadable')}")
    except Exception as _ve:  # noqa: BLE001
        rep["errors"].append(f"copy_verify: {str(_ve)[:200]}")

    # B1: авто-ремонт repairable=True измерений (shared_sets D3 + shopping D19).
    # Обновляет report["results"] in-place; ошибки → rep["copy_repair"]["errors"].
    try:
        from . import copy_verify as cv
        _cv_report = rep.get("copy_verify") or {}
        if _cv_report.get("results"):
            _t_repair = time.monotonic()
            _repair_result = cv.run_copy_repair(
                _cv_report,
                src_dir=src_dir,
                workdir=workdir,
                target_login=target_login,
                target_agency=target_agency,
                grid=grid,
                geo_pairs=(cstep_ctx.geo_pairs or []),
                log=(lambda m: _copy_job_log(job_id, m)),
            )
            rep["copy_repair"] = _repair_result
            _rr = _repair_result
            _copy_job_log(
                job_id,
                f"copy_repair: repairs={len(_rr.get('repairs') or [])}, "
                f"errors={len(_rr.get('errors') or [])}",
            )
            _copy_job_log(job_id, f"[timing] copy_repair: {time.monotonic() - _t_repair:.0f}s")
            # sitelinks_present: те же под-копированные кампании иногда не получают набор быстрых
            # ссылок при первом проходе (flux во время создания). step_attach_sitelinks идемпотентен
            # (дедуп через maps["sitelinks"]) — повторный вызов дозакрывает пропущенные кампании.
            try:
                _sl_miss = [
                    r for r in (_cv_report.get("results") or [])
                    if r.get("dimension") == "sitelinks_present"
                    and r.get("status") not in ("ok", "excluded_intentional")
                ]
                if _sl_miss and cstep_ctx.source_grid is not None and grid is not None:
                    _sl_rep = csteps.step_attach_sitelinks(cstep_ctx)
                    _copy_job_log(
                        job_id,
                        f"copy_repair sitelinks-retry: наборов {_sl_rep.get('sets_created', 0)}, "
                        f"привязано {_sl_rep.get('attached_campaigns', 0)}, "
                        f"пропущено {_sl_rep.get('skipped', 0)}",
                    )
            except Exception as _sle:  # noqa: BLE001
                rep["errors"].append(f"copy_repair sitelinks-retry: {str(_sle)[:150]}")
    except Exception as _re:  # noqa: BLE001
        rep["errors"].append(f"copy_repair: {str(_re)[:200]}")

    rep["results"] = results
    return rep


















# verify-after-settle: in-job copy_verify бежит ДО статуса done, а привязки (sitelinks/промо/
# картинки) доливаются/индексируются 5-10+ мин ПОСЛЕ done (dcr-демон direct-create-worker +
# async-индексация Яндекса). Доказано: settle-wait 150/240с в джобе и re-verify +300с → 0
# sitelinks, но спот-проверка позже = 9/46 на цели. Поэтому re-verify АДАПТИВНЫЙ: поллит цель
# до появления sitelinks (или таймаут), затем гонит полную сверку и перезаписывает copy_verify.
_COPY_REVERIFY_FIRST_SEC = 240          # первая проба (dcr стартует ~180с после done)
_COPY_REVERIFY_POLL_SEC = 90            # шаг опроса оседания
_COPY_REVERIFY_MAX_SEC = 900            # общий бюджет ожидания оседания (15 мин)


def _copy_target_sitelinks_ready(target_login: str, target_agency: str,
                                 workdir: Path) -> bool:
    """Быстрая проба: появились ли sitelinks на объявлениях цели (индикатор оседания привязок).

    Читает id_maps.json (созданные кампании) + v5 ads.get TextAd.SitelinkSetId по первым
    кампаниям. True как только хоть одно объявление имеет sitelink. Best-effort → False при сбое.
    """
    try:
        maps = _copy_read_json(workdir / "id_maps.json")
        cids = [int(v) for v in (maps.get("campaigns") or {}).values() if str(v).isdigit()][:6]
        if not cids:
            return False
        tr = _token_for_login(target_login, target_agency or "", _direct_tokens())
        tok = tr[0] if isinstance(tr, (tuple, list)) else tr
        if not tok:
            return False
        r = _v5_call("ads", "get", tok, target_login, {
            "SelectionCriteria": {"CampaignIds": cids},
            "FieldNames": ["Id"], "TextAdFieldNames": ["SitelinkSetId"],
            "Page": {"Limit": 500}})
        for a in ((r.get("result") or {}).get("Ads") or []):
            if (a.get("TextAd") or {}).get("SitelinkSetId"):
                return True
        return False
    except Exception:  # noqa: BLE001
        return False


def _copy_delayed_reverify(job_id: str, src_dir: Path, workdir: Path,
                           target_login: str, target_agency: str,
                           source_login: str = "") -> None:
    """Отложенная адаптивная пере-сверка source↔target ПОСЛЕ оседания привязок.

    Ждёт первую пробу, затем поллит появление sitelinks до таймаута; как только осело (или
    бюджет исчерпан) — гонит полную copy_verify и перезаписывает результат job'а (UI читает его).
    source_grid ПЕРЕСОБИРАЕТСЯ (иначе build_source_profile уходит в fallback без Grid и недочитывает
    адаптивы titles/bodies/images источника → ложный mismatch adaptive/images).
    Best-effort: ошибки/рестарт сервиса не критичны — in-job copy_verify остаётся как есть.
    """
    try:
        time.sleep(_COPY_REVERIFY_FIRST_SEC)
        _waited = _COPY_REVERIFY_FIRST_SEC
        while _waited < _COPY_REVERIFY_MAX_SEC:
            if _copy_target_sitelinks_ready(target_login, target_agency, workdir):
                break
            _copy_job_log(job_id, f"copy_verify: жду оседания привязок ({_waited}/{_COPY_REVERIFY_MAX_SEC}s)")
            time.sleep(_COPY_REVERIFY_POLL_SEC)
            _waited += _COPY_REVERIFY_POLL_SEC
        _src_grid_rv = None
        if source_login:
            try:
                _src_ag_rv = _resolve_agency_hint(source_login, "")
                _src_cli_rv = cmc.build_client(source_login, account=(_src_ag_rv or None))
                _src_grid_rv = gf.GridClient(source_login, cookie=(_src_cli_rv.sess.headers.get("Cookie") or ""))
            except Exception as _sge:  # noqa: BLE001 — без source_grid профиль источника уйдёт в fallback
                _copy_job_log(job_id, f"copy_verify (осевший): source_grid не пересобран ({str(_sge)[:120]})")
        from . import copy_verify as cv
        vr = cv.run_copy_verification(
            src_dir=src_dir, workdir=workdir,
            target_login=target_login, target_agency=target_agency or "",
            geo_pairs=[], grid=None, source_grid=_src_grid_rv,
            log=(lambda m: _copy_job_log(job_id, m)))
        _s = vr.get("summary") or {}
        _copy_job_log(job_id, f"copy_verify (осевший, +{_waited}s): "
                              f"ok={_s.get('ok')}, mismatch={_s.get('mismatch')}, "
                              f"unreadable={_s.get('unreadable')}")
        with _COPY_JOBS_LOCK:
            j = _COPY_JOBS.get(job_id)
            _res = dict(j["result"]) if (j and isinstance(j.get("result"), dict)) else None
        if _res is not None:
            _res["copy_verify_settled"] = vr
            _cp = _res.get("cookie_postprocess")
            if isinstance(_cp, dict):
                _cp = dict(_cp)
                _cp["copy_verify"] = vr        # UI (_cvAggregate) читает отсюда → покажет осевшее
                _res["cookie_postprocess"] = _cp
            _copy_job_upsert(job_id, result=_res)
    except Exception as e:  # noqa: BLE001
        _copy_job_log(job_id, f"copy_verify (осевший) error: {str(e)[:200]}")


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
        # Кросс-чек с v5 (авторитетный, стабильный источник типа). Grid-typename флейкует:
        # наблюдалось «13 GdUnifiedCampaign» на кампаниях, которые v5 стабильно отдаёт как
        # TEXT_CAMPAIGN → неверный grid-cookie путь → битый CopyCamp-снапшот (EOF@305) и падение
        # ПОСЛЕ delete_drafts. Кампанию, которую v5 видит как НЕ-ЕПК (текст/динамика/приложение),
        # НИКОГДА не гоним grid-unified путём — только v5-pull (как в рабочем прогоне). Настоящие
        # UAC/ЕПК-черновики v5 не отдаёт → в _v5_native их нет → grid-путь для них сохраняется.
        _v5_native: set[int] = set()
        try:
            _st_x, _sa_x = _token_for_login(source_login, _resolve_agency_hint(source_login, ""), _direct_tokens())
            _vr_x = _v5_call("campaigns", "get", _st_x, source_login,
                             {"SelectionCriteria": {"Ids": list(selected_ids)}, "FieldNames": ["Id", "Type"]})
            for _cx in ((_vr_x.get("result") or {}).get("Campaigns") or []):
                if str(_cx.get("Type") or "") in (
                    "TEXT_CAMPAIGN", "DYNAMIC_TEXT_CAMPAIGN", "MOBILE_APP_CAMPAIGN", "SMART_CAMPAIGN",
                ):
                    _v5_native.add(int(_cx.get("Id") or 0))
        except Exception as _ex:  # noqa: BLE001 — кросс-чек best-effort; при сбое остаётся прежняя grid-логика
            _copy_job_log(job_id, f"v5-кросс-чек типов недоступен ({str(_ex)[:80]}) — grid-классификация как есть")
        selected_unified_rows = [
            r for r in selected_grid_rows
            if str(r.get("typename") or r.get("type") or "") == "GdUnifiedCampaign"
            and int(r.get("id") or 0) not in _v5_native
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
        # verify-after-settle: отложенная пере-сверка после оседания dcr-привязок (см. выше).
        try:
            threading.Thread(
                target=_copy_delayed_reverify,
                args=(job_id, src_dir, workdir, target_login, target_agency or "", source_login),
                daemon=True, name=f"copy-reverify-{job_id[:8]}").start()
            _copy_job_log(job_id, f"copy_verify: осевшая пере-сверка запланирована (до {_COPY_REVERIFY_MAX_SEC}s ожидания оседания)")
        except Exception as _te:  # noqa: BLE001
            _copy_job_log(job_id, f"copy_verify reverify schedule error: {str(_te)[:150]}")
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






# Модульные имена для DI-фан-аута в configure() (см. выше).
from . import copy_jobs, copy_geo, copy_snapshot, copy_images, copy_metrika  # noqa: E402,F401
from . import copy_feeds, copy_grid_read, copy_uac, copy_cleanup, copy_grid_steps  # noqa: E402,F401

from .copy_jobs import (  # noqa: E402,F401  (ре-экспорт распила)
    _COPY_JOBS, _COPY_JOBS_LOCK, _copy_job_upsert, _copy_mirror_create_job, _copy_job_log, _copy_jobs_recover,
)

from .copy_geo import (  # noqa: E402,F401  (ре-экспорт распила)
    _COPY_R_CODE_RE, _copy_canonical_region_name, _copy_geo_id_for_target, _copy_ctx, _copy_m3_decliner, _copy_build_geo, _copy_geo_replacements, _copy_apply_geo_replacements, _copy_target_region_code, _copy_remap_region_code, _copy_normalize_campaign_name, _copy_domain_from_href, _copy_target_href,
    _REGION_ALIASES, _REGION_ALIASES_NORM, _norm_region_alias_key, _REGION_ALIAS_DASH_RE,
)

from .copy_snapshot import (  # noqa: E402,F401  (ре-экспорт распила)
    _COPY_JSON_PAYLOADS, _COPY_SUPPORTED_V5_TYPES, _copy_read_json, _copy_write_json, _copy_filter_snapshot, _copy_walk_strings, _copy_scan_payload_terms, _copy_rewrite_snapshot_context, _copy_snapshot_preflight, _copy_build_results, _copy_preseed_feed_maps, _copy_skip_unmapped_feed_campaigns,
)

from .copy_images import (  # noqa: E402,F401  (ре-экспорт распила)
    _copy_grid_ad_image_hashes, _copy_v501_ad_image_hashes, _copy_image_remapper,
)

from .copy_metrika import (  # noqa: E402,F401  (ре-экспорт распила)
    _copy_rewrite_strategy_goal, _copy_apply_metrika,
)

from .copy_feeds import (  # noqa: E402,F401  (ре-экспорт распила)
    _COPY_DEFAULT_FEED_PATH, _copy_target_feed_id, _copy_feeds_preview, _copy_grid_validate_feed_map,
)

from .copy_grid_read import (  # noqa: E402,F401  (ре-экспорт распила)
    _copy_selected_grid_campaigns, _copy_grid_read_selected, _copy_grid_campaign_spec,
)

from .copy_uac import (  # noqa: E402,F401  (ре-экспорт распила)
    _copy_is_uac_grid_row, _copy_uac_value, _copy_uac_strings, _copy_uac_sitelinks, _copy_uac_media_urls, _copy_uac_filter_list, _copy_uac_campaigns,
)

from .copy_cleanup import (  # noqa: E402,F401  (ре-экспорт распила)
    _copy_target_campaigns_info, _copy_cleanup_uac_drafts, _copy_target_cleanup,
)

from .copy_grid_steps import (  # noqa: E402,F401  (ре-экспорт распила)
    _copy_grid_bridge_callouts, _copy_grid_unified_steps, _copy_make_video_resolver,
)

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

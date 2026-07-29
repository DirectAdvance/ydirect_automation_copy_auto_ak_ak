"""Grid-only UnifiedCampaign copy path extracted from copy_engine."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .. import grid_create as gc
from .. import grid_finalize as gf


def _engine():
    from . import copy_engine as ce  # lazy to avoid import-time cycle
    return ce


def configure(_deps: dict) -> None:
    return None


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
    ce = _engine()
    if not r_code or not re.fullmatch(r"r\d{4}", str(r_code or "")) or r_code == "r0000":
        return ""
    if not ce._victory_conn_rw:
        return ""
    try:
        conn = ce._victory_conn_rw()
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
    ce = _engine()
    from .copy_steps import _clean_group_brand as _csteps_clean_group_brand
    source_login = (body.get("source_login") or "").strip()
    target_login = (body.get("target_login") or "").strip()
    selected_ids = {int(x) for x in (body.get("campaign_ids") or []) if str(x).isdigit()}
    counter_id = int(body.get("counter_id") or 0)
    goal_id = int(body.get("goal_id") or 0)
    target_domain = (body.get("target_domain") or "").strip()
    target_city = (body.get("target_city") or "").strip()
    target_region = (body.get("target_region") or "").strip()
    target_agency = body.get("agency") or ce._resolve_agency_hint(target_login, "")
    # ДОРАБОТКА 1: feed_map (пофидовая замена) в ЕПК-ветке. Раньше брался ОДИН авто-фид
    # (ce._copy_target_feed_id, feed_map игнорировался). Теперь: если body.feed_map задан и валиден
    # (те же проверки, что в _copy_run_job — целевой фид ПРИНАДЛЕЖИТ target-аккаунту), используем
    # целевой фид ИЗ карты для shopping/listing. Общий кейс «все source-фиды → один target-фид» —
    # берём этот единый target feed_id. feed_map пуст/невалиден → прежнее поведение.
    feed_map_valid = ce._copy_grid_validate_feed_map(
        target_login, target_agency or "", body, log=(lambda m: ce._copy_job_log(job_id, m)))
    feed_map_targets = list(dict.fromkeys(int(v) for v in feed_map_valid.values()))
    if feed_map_targets:
        target_feed_id = feed_map_targets[0]
        ce._copy_job_log(job_id, f"feed_map активен: целевые фиды {feed_map_targets}, "
                              f"shopping/listing → {target_feed_id}")
    else:
        target_feed_id = ce._copy_target_feed_id(target_login, target_agency or "", workdir, target_domain)

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
        ce._copy_job_log(job_id, "гео: режим 'keep' — гео-замена и ремап RegionIds пропущены")
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
        source_ctx = ce._copy_ctx(source_login)
        # Морфология текстов: ТОЛЬКО если ровно 1 плюс-регион, нет исключений, тип НЕ World/Country.
        _morph_type = (ce._geo_type_by_id(positive_ids[0]) if ce._geo_type_by_id and positive_ids else None) or ""
        _do_morph = (len(positive_ids) == 1 and len(negative_ids) == 0
                     and _morph_type not in ("World", "Country"))
        if _do_morph:
            # Имя региона резолвим на сервере из справочника GeoRegions — не доверяем клиенту.
            geo_region_name_str = (ce._geo_name_by_id(positive_ids[0]) if ce._geo_name_by_id else "") or ""
            if not geo_region_name_str:
                raise RuntimeError(
                    f"geo_region_id={positive_ids[0]}: имя региона не найдено в справочнике GeoRegions"
                )
            replacements = ce._copy_geo_replacements(
                source_ctx, "", geo_region_name_str, log=(lambda m: ce._copy_job_log(job_id, m))
            )
            ce._copy_job_log(job_id, f"гео: 1 регион, меняем тексты: region_id={positive_ids[0]} name={geo_region_name_str!r}")
        else:
            replacements = []
            ce._copy_job_log(job_id,
                          f"гео: {len(positive_ids)} регион(ов), {len(negative_ids)} исключений"
                          f" → тексты не меняем (RegionIds ставим)")
        ce._copy_job_log(job_id, f"гео: режим 'change', region_ids={geo_region_ids[:10]!r}")
    else:
        target_region = ce._copy_canonical_region_name(target_region)
        local_gid, local_geo_name = ce._copy_geo_id_for_target(target_city, target_region)
        if not local_gid:
            raise RuntimeError(f"не найден GeoRegionId для целевого гео: city={target_city!r}, region={target_region!r}")
        region_ids = [int(local_gid)]
        # Баги 1/4: r-код target-региона для ремапа кодера (один источник — имена кампаний И групп).
        target_r_code = ce._copy_target_region_code(target_city, target_region)
        if target_r_code:
            ce._copy_job_log(job_id, f"кодер: r-сегмент региона → {target_r_code}")
        source_ctx = ce._copy_ctx(source_login)
        # Баг 1 фолбэк: source не в local_gsheet_sites → source_ctx пуст → replacements=[].
        # Резолвим источник по r-коду из имени source-кампании и добавляем регион в source_ctx.
        if not (source_ctx.get("city") or source_ctx.get("region")):
            _src_r_code = ""
            for _sr in selected_grid_rows:
                _m = ce._COPY_R_CODE_RE.search(str(_sr.get("name") or ""))
                if _m:
                    _src_r_code = _m.group()
                    break
            if _src_r_code and _src_r_code != target_r_code:
                _src_oblast = _copy_rcode_to_region(_src_r_code)
                if _src_oblast:
                    source_ctx = dict(source_ctx) if source_ctx else {}
                    source_ctx["region"] = _src_oblast
                    ce._copy_job_log(job_id,
                                  f"гео-фолбэк: {_src_r_code} → {_src_oblast!r} (source не в local_gsheet_sites)")
        replacements = ce._copy_geo_replacements(
            source_ctx, target_city, target_region, log=(lambda m: ce._copy_job_log(job_id, m))
        )
    src_domain = (source_ctx.get("domain") or "").strip()

    ce._copy_job_log(job_id, f"grid-cookie snapshot источника {source_login}: {len(selected_ids)} кампаний")
    try:
        snap = ce._copy_grid_read_selected(source_login, selected_ids)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"grid snapshot не получен ({source_login}): {str(e)[:220]}") from e
    try:
        source_image_hashes = ce._copy_v501_ad_image_hashes(
            source_login,
            selected_ids,
            body.get("source_agency") or body.get("sourceAgency") or target_agency or "",
        )
    except Exception as e:  # noqa: BLE001 — v501 image-хэши best-effort: картинки доберём из grid-ads
        ce._copy_job_log(job_id, f"v501 image-хэши источника не получены ({str(e)[:180]}) — продолжаю без них")
        source_image_hashes = {}
    campaigns = [c for c in (snap.get("campaigns") or []) if str(c.get("__typename")) == "GdUnifiedCampaign"]
    if len(campaigns) != len(selected_ids):
        raise RuntimeError(f"grid snapshot неполный: выбрано {len(selected_ids)}, прочитано {len(campaigns)} Unified")
    if not src_domain:
        for camp in campaigns:
            src_domain = ce._copy_domain_from_href((camp.get("additionalData") or {}).get("href"))
            if src_domain:
                break
    if not src_domain:
        for ad in snap.get("ads") or []:
            src_domain = str(ad.get("domain") or "").strip().lower() or ce._copy_domain_from_href(ad.get("href"))
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
                ce._copy_job_log(job_id, f"гео keep: RegionIds из группы источника: {region_ids}")
                break
        if not region_ids:
            region_ids = [225]   # Россия — последний резерв
            ce._copy_job_log(job_id, "гео keep: RegionIds источника не найдены → [225] (Россия)")

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
        _all_src_hashes.update(ce._copy_grid_ad_image_hashes(_ad))
    _remap_images = ce._copy_image_remapper(
        source_login, body.get("source_agency") or body.get("sourceAgency") or "",
        target_login, target_agency or "", _all_src_hashes, maps, workdir,
        log=(lambda m: ce._copy_job_log(job_id, m)),
        provided_hashes=(provided_hashes or None))
    if provided_hashes:
        ce._copy_job_log(job_id, f"картинки: mode='other', использую {len(provided_hashes)} загруженных хэшей round-robin")
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
        new_name = ce._copy_normalize_campaign_name(old_name, replacements, target_r_code)
        base_href = ce._copy_target_href(((camp.get("additionalData") or {}).get("href")), src_domain, target_domain)
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
                    href = ce._copy_target_href(ad.get("href"), src_domain, target_domain)
                image_hashes += ce._copy_grid_ad_image_hashes(ad)
                if ad.get("__typename") == "GdTextAd":
                    titles += [ad.get("title"), ad.get("titleExtension")]
                    bodies.append(ad.get("body"))
                else:
                    titles += list(ad.get("titles") or [])
                    bodies += list(ad.get("bodies") or [])
            titles = [ce._copy_apply_geo_replacements(t, replacements) for t in titles if str(t or "").strip()]
            bodies = [ce._copy_apply_geo_replacements(t, replacements) for t in bodies if str(t or "").strip()]
            group_specs.append({
                # Баг 1: гео-словоформы + ремап r-сегмента кодера группы (r-код словами не задеть).
                "name": ce._copy_remap_region_code(
                    ce._copy_apply_geo_replacements(grp.get("adgroup_name") or "группа", replacements),
                    target_r_code),
                "keywords": [ce._copy_apply_geo_replacements(k, replacements) for k in (grp.get("keywords") or [])],
                # Задача 1: (а) применить гео-замену к группо-уровневым минусам (как к ключам);
                # (б) убрать минусы, содержащие форму ЦЕЛЕВОГО города — иначе target заминусует себя.
                "minus": _copy_geo_filter_negatives(
                    [ce._copy_apply_geo_replacements(m, replacements)
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
                campaign_spec=ce._copy_grid_campaign_spec(new_name, counter_id, goal_id,
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
                ce._copy_job_log(job_id, f"grid-cookie {old_name}: ошибка {results[-1]['error'][:220]}")
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
            spec_net = bool(ce._copy_grid_campaign_spec(new_name, counter_id, goal_id).get("network"))
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
                            from .. import create_set_feeds as _csf_ff
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
                            from ..text_norm import _trim_clean as _tc
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
                                    from .. import create_set_feeds as _csf_dt
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
                                ce._copy_job_log(job_id, f"grid-cookie {new_name}: текст по умолчанию "
                                                      f"проставлен на {len(shop_ids)} товарных")
                        except Exception as _e_dt:  # noqa: BLE001 — товарные созданы; текст по умолчанию не критичен для сборки
                            ce._copy_job_log(job_id, f"grid-cookie {new_name}: текст по умолчанию не проставлен ({str(_e_dt)[:160]})")
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
            ce._copy_job_upsert(job_id, progress=min(95, 10 + int(idx * 80 / max(1, len(campaigns)))))
            ce._copy_job_log(job_id, f"grid-cookie copied: {new_name} → {new_cid} ({idx}/{len(campaigns)})")
        except Exception as e:  # noqa: BLE001 — транспортный/прочий сбой не убивает весь job
            results.append({"ok": False, "source_id": old_cid, "name": new_name, "error": str(e)[:220]})
            ce._copy_job_log(job_id, f"grid-cookie {old_name}: исключение {str(e)[:200]}")
            continue

    ce._copy_write_json(workdir / "id_maps.json", maps)

    # ДОРАБОТКА 2: copy_steps-постобработка для ЕПК-ветки (те же под-сервисы, что v5-путь).
    # Пишем синтетический snapshot (source-dir) и прогоняем применимые шаги cookie/Grid (0 v5-баллов).
    cookie_post = {"skipped": ["postprocess (нет созданных кампаний)"], "errors": []}
    if maps["campaigns"]:
        try:
            src_dir.mkdir(parents=True, exist_ok=True)
            ce._copy_write_json(src_dir / "campaigns.json", snap_campaigns_json)
            ce._copy_write_json(src_dir / "adgroups.json", snap_adgroups_json)
            ce._copy_write_json(src_dir / "ads.json", snap_ads_json)
            # [geo-честность] keywords.json: уже гео-заменённые ключи для check_geo_kw_consistency
            ce._copy_write_json(src_dir / "keywords.json", snap_keywords_json)
            cookie_post = ce._copy_grid_unified_steps(
                job_id, body, target_login, target_agency or "", src_domain,
                replacements, maps, src_dir, workdir)
        except Exception as e:  # noqa: BLE001 — постобработка не валит уже созданные кампании
            cookie_post = {"errors": [f"grid unified postprocess: {str(e)[:220]}"]}
            ce._copy_job_log(job_id, f"grid-cookie postprocess: ошибка {str(e)[:200]}")
        for _err in (cookie_post.get("errors") or [])[:8]:
            ce._copy_job_log(job_id, f"grid-cookie postprocess warning: {_err}")

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

"""Файловый слой поверх JSON-снапшота кабинета (0 DI): фильтр/переписывание/preflight.

Вынесено из copy_engine.py (чистый code-motion, логика не изменена).
DI инъектится copy_engine.configure() фан-аутом; sibling-модули copy_* — прямой импорт (DAG, цикла нет).
"""
from __future__ import annotations

import json
from pathlib import Path

from .copy_geo import _copy_build_geo


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

    # Добавляем campaign-level уточнения (inheritableCallouts) в callout_ids.
    # campaign_callouts.json создаётся pull_source_campaign_assets ДО вызова этой функции.
    # Без этого кампании, у которых уточнения только на уровне кампании (ad-level=0),
    # давали пустой callouts.json → callout_texts=[] → step_attach_callouts не запускался.
    _camp_co_path = src_dir / "campaign_callouts.json"
    if _camp_co_path.exists():
        _camp_co = _copy_read_json(_camp_co_path)
        if isinstance(_camp_co, dict):
            for _co_list in _camp_co.values():
                for _co_id in (_co_list or []):
                    try:
                        callout_ids.add(int(_co_id))
                    except (TypeError, ValueError):
                        pass
    # Добавляем campaign-level быстрые ссылки (inheritableSitelinkSet) в sitelink_ids.
    # На будущее: если phase_pull начнёт скачивать campaign-level наборы, они попадут в sitelinks.json.
    _camp_sl_path = src_dir / "campaign_sitelinks.json"
    if _camp_sl_path.exists():
        _camp_sl = _copy_read_json(_camp_sl_path)
        if isinstance(_camp_sl, dict):
            for _sl_val in _camp_sl.values():
                try:
                    if _sl_val:
                        sitelink_ids.add(int(_sl_val))
                except (TypeError, ValueError):
                    pass

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

    # Пропустить группы без копируемых объявлений (например, все объявления архивные).
    # Делаем ДО записи json — preflight не увидит пустых групп и не остановит джобу.
    groups_with_ads = {int(a.get("AdGroupId") or 0) for a in (ads + shopping_ads) if a.get("AdGroupId")}
    dropped_ids = {int(g.get("Id") or 0) for g in adgroups if int(g.get("Id") or 0) not in groups_with_ads}
    if dropped_ids:
        adgroups = [g for g in adgroups if int(g.get("Id") or 0) not in dropped_ids]
        keywords = [k for k in keywords if int(k.get("AdGroupId") or 0) not in dropped_ids]
        bidmods = [m for m in bidmods if int(m.get("AdGroupId") or 0) not in dropped_ids]

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
        "retargeting_lists": len(ret_lists), "dropped_empty_adgroups": len(dropped_ids),
    }
    meta_path = src_dir / "_meta.json"
    meta_json = _copy_read_json(meta_path) if meta_path.exists() else {}
    meta_json["counts"] = meta
    _copy_write_json(meta_path, meta_json)
    return meta


_COPY_SUPPORTED_V5_TYPES = {"TEXT_CAMPAIGN", "DYNAMIC_TEXT_CAMPAIGN", "UNIFIED_AD_CAMPAIGN"}


_COPY_JSON_PAYLOADS = (
    "campaigns.json", "adgroups.json", "ads.json", "shopping_ads.json", "keywords.json",
    "sitelinks.json", "vcards.json", "feeds.json", "promotions.json",
)


def _copy_walk_strings(obj, fn):
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, list):
        return [_copy_walk_strings(x, fn) for x in obj]
    if isinstance(obj, dict):
        return {k: _copy_walk_strings(v, fn) for k, v in obj.items()}
    return obj


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

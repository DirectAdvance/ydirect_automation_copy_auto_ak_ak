"""Source profile builder for copy verification."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core import campaign as cmc
from ..clients import grid_finalize as gf
from ..clients import grid_read as gr
from .copy_asset_steps import _CALLOUT_PER_CAMPAIGN_CAP_DEFAULT as _CALLOUT_CAP
from .copy_verify_utils import (
    _OK, _MISMATCH, _MISSING, _UNREADABLE, _EXCLUDED,
    _feed_filter_signature, _nolog, _read_audience_signatures_grid,
    _rj, _rj_dict, _strip_domain,
)


def build_source_profile(src_dir: Any,
                          grid_snapshot: Optional[Dict[int, dict]] = None,
                          source_grid: Optional[gf.GridClient] = None,
                          cached_audiences: Optional[Dict[int, dict]] = None,
                          log: Optional[Callable[[str], None]] = None) -> Dict[str, dict]:
    """Нормализованный профиль источника по кампаниям из snapshot-файлов.

    Args:
        src_dir: Директория со snapshot-файлами (campaigns.json, adgroups.json, …).
            Пишется direct_copy.phase_pull + pull_source_campaign_assets.
        grid_snapshot: Опциональный pre-fetched dict {int(ad_id): {...}} из
            adaptive_ads_for_update источника (titles, bodies, imageHashes, hasVideo,
            hasButton). Если None — fallback на TextAd-поля из ads.json.
        source_grid: Опциональный GridClient источника для чтения audience/retargeting-привязок.
        cached_audiences: Уже прочитанные аудитории источника {campaign_id: {adgroup_id: [sig]}}.
        log: Функция логирования.

    Returns:
        {str(src_campaign_id): {dimension_key: value}}
    """
    src_dir = Path(src_dir)
    _log = log or _nolog

    campaigns = _rj(src_dir / "campaigns.json")
    adgroups = _rj(src_dir / "adgroups.json")
    ads = _rj(src_dir / "ads.json")
    shopping_ads = _rj(src_dir / "shopping_ads.json")
    keywords = _rj(src_dir / "keywords.json")
    bidmods = _rj(src_dir / "bidmodifiers.json")
    adimages = _rj(src_dir / "adimages.json")

    # pull_source_campaign_assets пишет эти файлы в src_dir
    campaign_callouts = _rj_dict(src_dir / "campaign_callouts.json")   # str(cid) → [str(co_id)]
    campaign_promos = _rj_dict(src_dir / "campaign_promos.json")       # str(cid) → str(promo_id)
    campaign_sitelinks = _rj_dict(src_dir / "campaign_sitelinks.json") # str(cid) → sitelink_set_id
    fallback_callouts_count = len({
        str((c.get("Callout") or {}).get("CalloutText") or c.get("CalloutText") or c.get("Id") or "").strip().lower()
        for c in _rj(src_dir / "callouts.json")
        if str((c.get("Callout") or {}).get("CalloutText") or c.get("CalloutText") or c.get("Id") or "").strip()
    })
    source_audiences_readable = False
    source_audiences: Optional[Dict[int, dict]] = cached_audiences
    if source_audiences is not None:
        source_audiences_readable = True
    elif source_grid is not None:
        cids_for_audiences = []
        for c in campaigns:
            try:
                cid_int = int(c.get("Id") or 0)
            except (TypeError, ValueError):
                continue
            if cid_int > 0:
                cids_for_audiences.append(cid_int)
        source_audiences, source_audiences_readable = _read_audience_signatures_grid(
            source_grid, cids_for_audiences, _log
        )

    def _setting_yes(camp: dict, option: str, default: Optional[bool] = None) -> Optional[bool]:
        for skey in ("TextCampaign", "DynamicTextCampaign", "SmartCampaign",
                     "CpmBannerCampaign", "UnifiedAdCampaign"):
            settings = (camp.get(skey) or {}).get("Settings") or []
            for item in settings if isinstance(settings, list) else []:
                if item.get("Option") == option:
                    return str(item.get("Value") or "").upper() == "YES"
        return default

    def _items_list(raw: Any) -> list:
        if isinstance(raw, dict):
            raw = raw.get("Items") or []
        return raw if isinstance(raw, list) else []

    def _grid_ad_has_payload(row: Any) -> bool:
        if not isinstance(row, dict):
            return False
        return bool(
            row.get("titles") or row.get("bodies") or row.get("imageHashes") or
            row.get("creativeIds") or row.get("button")
        )

    def _ad_uses_adaptive_text_metrics(ad: dict) -> bool:
        # Товарные/каталожные/смарт-объявления не участвуют в adaptive text metrics
        # (titles/bodies/images/video/button) ни через Grid, ни через TextAd-fallback —
        # target-профиль их тоже не считает в этих размерностях (copy_verify_target.py
        # исключает SHOPPING_AD/SMART_AD/LISTING_AD из adaptive_total). Раньше хелпер
        # только выключал Grid-ветку, а TextAd/img_count_by_ad fallback ниже всё равно мог
        # засчитать товарное объявление в ads_with_images → завышение против честного 0
        # у источника.
        ad_type = str(ad.get("Type") or "").upper()
        return ad_type not in ("SHOPPING_AD", "LISTING_AD", "SMART_AD")

    # ── Индексы для быстрого lookup ──────────────────────────────────────────
    ags_by_camp: Dict[str, list] = {}
    for ag in adgroups:
        cid = str(ag.get("CampaignId") or "")
        if cid:
            ags_by_camp.setdefault(cid, []).append(ag)

    kw_by_agid: Dict[str, int] = {}     # str(adgroup_id) → count
    for kw in keywords:
        agid = str(kw.get("AdGroupId") or "")
        if agid:
            kw_by_agid[agid] = kw_by_agid.get(agid, 0) + 1

    ads_by_camp: Dict[str, list] = {}
    for ad in ads:
        cid = str(ad.get("CampaignId") or "")
        if cid:
            ads_by_camp.setdefault(cid, []).append(ad)

    shopping_by_camp: Dict[str, list] = {}
    shopping_by_group: Dict[str, list] = {}
    for sa in shopping_ads:
        cid = str(sa.get("CampaignId") or "")
        if cid:
            shopping_by_camp.setdefault(cid, []).append(sa)
        gid = str(sa.get("AdGroupId") or "")
        if gid:
            shopping_by_group.setdefault(gid, []).append(sa)

    # bidmod: типы присутствующих корректировок (для report, не для сравнения)
    bidmod_types_by_camp: Dict[str, set] = {}
    _adj_keys = (
        "DemographicsAdjustments", "RetargetingAdjustments", "MobileAdjustment",
        "VideoAdjustment", "IncomeGradeAdjustments", "RegionalAdjustments",
        "SerpLayoutAdjustments",
    )
    for bm in bidmods:
        cid = str(bm.get("CampaignId") or "")
        if not cid:
            continue
        s = bidmod_types_by_camp.setdefault(cid, set())
        for k in _adj_keys:
            if bm.get(k):
                s.add(k)

    # adimages: str(ad_id) → count of hashes
    img_count_by_ad: Dict[str, int] = {}
    for img in adimages:
        aid = str(img.get("AdId") or "")
        h = img.get("AdImageHash") or ""
        if aid and h:
            img_count_by_ad[aid] = img_count_by_ad.get(aid, 0) + 1

    # ── Строим профиль per campaign ──────────────────────────────────────────
    profile: Dict[str, dict] = {}

    for camp in campaigns:
        cid = str(camp.get("Id") or "")
        if not cid:
            continue

        camp_ags = ags_by_camp.get(cid, [])
        adgroup_count = len(camp_ags)

        # D2: keywords
        kw_count = sum(kw_by_agid.get(str(ag.get("Id") or ""), 0) for ag in camp_ags)

        # D2b: campaign-level negatives
        raw_negs = camp.get("NegativeKeywords") or {}
        if isinstance(raw_negs, dict):
            camp_neg_items = raw_negs.get("Items") or []
        elif isinstance(raw_negs, list):
            camp_neg_items = raw_negs
        else:
            camp_neg_items = []
        camp_neg_count = len(camp_neg_items)

        # D2c: group-level negatives count (sum across groups)
        group_neg_count = 0
        for ag in camp_ags:
            raw_gneg = ag.get("NegativeKeywords") or {}
            if isinstance(raw_gneg, dict):
                group_neg_count += len(raw_gneg.get("Items") or [])
            elif isinstance(raw_gneg, list):
                group_neg_count += len(raw_gneg)

        # D3: shared_sets. Source-снапшот хранит их ВЛОЖЕННО в TextCampaign.NegativeKeywordSharedSetIds
        # (формат {"Items":[...]}), а не top-level → раньше читалось 0 → ложный mismatch vs target.
        _nks = camp.get("NegativeKeywordSharedSetIds")
        if not _nks:
            _nks = (camp.get("TextCampaign") or {}).get("NegativeKeywordSharedSetIds")
        if isinstance(_nks, dict):
            _nks = _nks.get("Items") or []
        shared_set_count = len([x for x in (_nks or []) if str(x).strip()])

        # D4: promo (from pull_source_campaign_assets или promotions.json count)
        promo_id = campaign_promos.get(cid)
        has_promo = bool(promo_id)

        # D7: callouts. If campaign-level source links were unreadable, writer falls back to
        # the union from callouts.json for every mapped campaign; verifier must compare against
        # the same intended target state, not the empty campaign_callouts.json file.
        # FIX C (2026-08-05): writer caps каждую кампанию per_campaign_cap уточнений
        # (copy_asset_steps.py step_attach_callouts, use_ids[:per_campaign_cap]) — источник с
        # 10 уточнениями штатно копируется как 8. Сравниваем ожидаемое (capped) значение, а не
        # сырое количество источника (66 ложных mismatch на кампаниях с >cap уточнениями).
        # Цель НИЖЕ capped-ожидания остаётся реальным расхождением (min не завышает ожидание).
        callout_ids = campaign_callouts.get(cid) or []
        _raw_callout_count = len(callout_ids) if campaign_callouts else fallback_callouts_count
        callout_count = min(_raw_callout_count, _CALLOUT_CAP)

        # D8: sitelinks. Быстрые ссылки бывают на двух независимых уровнях:
        # campaign-level (Grid inheritableSitelinkSet) и ad-level
        # (TextAd/DynamicTextAd.SitelinkSetId). Проверяем оба, потому copy должен
        # сохранить тот же вариант привязки, а не только общий факт наличия ссылок.
        sitelink_set_id = campaign_sitelinks.get(cid)
        campaign_has_sitelinks = bool(sitelink_set_id)
        ad_sitelinks_count = 0
        for ad in ads_by_camp.get(cid, []):
            ta = ad.get("TextAd") or ad.get("DynamicTextAd") or {}
            if ta.get("SitelinkSetId"):
                ad_sitelinks_count += 1
        has_sitelinks = campaign_has_sitelinks or ad_sitelinks_count > 0

        # D12: strategy — ищем BiddingStrategy в типовом поле кампании
        strategy_name = ""
        for skey in ("TextCampaign", "DynamicTextCampaign", "SmartCampaign",
                     "CpmBannerCampaign", "UnifiedAdCampaign"):
            stype = camp.get(skey)
            if stype:
                bid_strat = stype.get("BiddingStrategy") or {}
                strategy_name = (
                    (bid_strat.get("Search") or {}).get("BiddingStrategyType") or
                    (bid_strat.get("Network") or {}).get("BiddingStrategyType") or ""
                )
                break

        # D16: UTM tracking без домена
        tracking_norm = _strip_domain(camp.get("TrackingParams") or "")

        # D5/D6/D9/D13/D14: ads
        camp_ads_list = ads_by_camp.get(cid, [])
        ads_with_titles = 0
        ads_with_texts = 0
        ads_with_images = 0
        ads_with_video = 0
        ads_with_button = 0
        # Latent-bug guard (found on review): hasButton has no TextAd fallback at all —
        # if Grid snapshot has no payload for ANY eligible ad of this campaign,
        # ads_with_button stays 0 not because there are no buttons, but because button
        # state was never read. Track how many eligible ads actually got measured so the
        # diff layer can tell "genuinely 0" from "unknown" and never bless a target that
        # merely differs from an unread source (see ads_with_button_readable below).
        ads_button_eligible = 0
        ads_button_measured = 0

        for ad in camp_ads_list:
            aid_str = str(ad.get("Id") or "")
            if not aid_str:
                continue
            if not _ad_uses_adaptive_text_metrics(ad):
                continue
            ads_button_eligible += 1
            try:
                aid_int = int(aid_str)
            except ValueError:
                aid_int = 0

            if (
                grid_snapshot
                and _grid_ad_has_payload(grid_snapshot.get(aid_int))
            ):
                gs = grid_snapshot[aid_int]
                if gs.get("titles"):
                    ads_with_titles += 1
                if gs.get("bodies"):
                    ads_with_texts += 1
                if gs.get("imageHashes"):
                    ads_with_images += 1
                if gs.get("hasVideo"):
                    ads_with_video += 1
                ads_button_measured += 1
                if gs.get("hasButton"):
                    ads_with_button += 1
            else:
                # Fallback: TextAd / DynamicTextAd поля
                ta = ad.get("TextAd") or ad.get("DynamicTextAd") or {}
                if ta.get("Title"):
                    ads_with_titles += 1
                if ta.get("Text"):
                    ads_with_texts += 1
                if ta.get("AdImageHash") or img_count_by_ad.get(aid_str, 0) > 0:
                    ads_with_images += 1
                # hasVideo / hasButton недоступны без Grid-источника — ads_button_measured
                # НЕ инкрементируется, ads_with_button остаётся честным недосчётом.

        # 0 eligible ads → 0 buttons is a true fact (nothing to have a button on);
        # eligible ads exist but none of them was actually measured via Grid → unknown.
        ads_with_button_readable = ads_button_eligible == 0 or ads_button_measured > 0

        # D19: товарные/каталожные объявления. SHOPPING_AD берём из детального
        # shopping_ads.json, LISTING_AD — из общего ads.json (v5 не отдаёт для него детальный
        # ListingAd payload в source snapshot, но сам факт/количество обязаны быть 1в1).
        camp_shopping = shopping_by_camp.get(cid, [])
        shopping_count = len(camp_shopping)
        listing_count = len([ad for ad in camp_ads_list if ad.get("Type") == "LISTING_AD"])
        shopping_filter_signatures_by_group: Dict[str, list] = {}
        listing_filter_signatures_by_group: Dict[str, list] = {}
        shopping_op_types: set = set()
        for sa in camp_shopping:
            gid = str(sa.get("AdGroupId") or "")
            conditions = (sa.get("ShoppingAd") or {}).get("FeedFilterConditions") or []
            if gid:
                shopping_filter_signatures_by_group.setdefault(gid, []).append(
                    _feed_filter_signature(conditions)
                )
            for cond in (conditions if isinstance(conditions, list) else []):
                op = (cond.get("Operator") or cond.get("operator") or "")
                if op:
                    shopping_op_types.add(op)
        for la in [ad for ad in camp_ads_list if ad.get("Type") == "LISTING_AD"]:
            gid = str(la.get("AdGroupId") or "")
            if not gid:
                continue
            # LISTING_AD в source ads.json приходит без ListingAd-подтела. Копировщик
            # восстанавливает его из ShoppingAd той же группы, поэтому это и есть expected.
            source_shop = (shopping_by_group.get(gid) or [None])[0]
            conditions = ((source_shop or {}).get("ShoppingAd") or {}).get("FeedFilterConditions") or []
            listing_filter_signatures_by_group.setdefault(gid, []).append(
                _feed_filter_signature(conditions)
            )

        # Тип кампании: UAC (tp6/tp7) vs остальные
        is_uac = bool(camp.get("UnifiedAdCampaign"))

        profile[cid] = {
            # D1
            "adgroup_count": adgroup_count,
            # D2
            "kw_count": kw_count,
            "camp_neg_count": camp_neg_count,
            "group_neg_count": group_neg_count,
            # D3
            "shared_set_count": shared_set_count,
            # D4
            "has_promo": has_promo,
            "promo_id": str(promo_id) if promo_id else None,
            # D5
            "ads_with_titles": ads_with_titles,
            # D6
            "ads_with_texts": ads_with_texts,
            # D7
            "callout_count": callout_count,
            # D8
            "has_sitelinks": has_sitelinks,
            "campaign_has_sitelinks": campaign_has_sitelinks,
            "ad_sitelinks_count": ad_sitelinks_count,
            # D9
            "ads_with_images": ads_with_images,
            # D10: audience/retargeting-привязки групп через Grid. None = не прочитано.
            "audiences": (
                (source_audiences or {}).get(int(cid), {})
                if source_audiences_readable else None
            ),
            # D11: bidmods — excluded (наш стандарт)
            "bid_modifier_types": sorted(bidmod_types_by_camp.get(cid, set())),
            # D12
            "strategy_name": strategy_name,
            # D13
            "ads_with_video": ads_with_video,
            # D14: button — только если grid_snapshot передан
            "ads_with_button": ads_with_button,
            # D14: True если хотя бы один подходящий ad реально измерен через Grid
            # (или подходящих ads нет вовсе); False = hasButton неизвестен для ВСЕХ ads
            # кампании — 0 не значит "нет кнопок", diff должен читать это как unreadable
            "ads_with_button_readable": ads_with_button_readable,
            # D15: adPrice — excluded (берётся из фида цели)
            # D16
            "tracking_norm": tracking_norm,
            # D17
            "site_monitoring": _setting_yes(camp, "ENABLE_SITE_MONITORING", default=True),
            # D18
            "minus_places": sorted(str(x).strip() for x in (
                _items_list(camp.get("ExcludedSites")) +
                _items_list(camp.get("ExcludedSitesForVideoAds"))
            ) if str(x).strip()),
            # D19
            "shopping_count": shopping_count,
            "listing_count": listing_count,
            "shopping_filter_signatures_by_group": {
                k: sorted(v) for k, v in shopping_filter_signatures_by_group.items()
            },
            "listing_filter_signatures_by_group": {
                k: sorted(v) for k, v in listing_filter_signatures_by_group.items()
            },
            "shopping_op_types": sorted(shopping_op_types),
            # meta
            "_is_uac": is_uac,
            "_ads_count": len(camp_ads_list),
        }

    return profile

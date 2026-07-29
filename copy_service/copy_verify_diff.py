"""Profile diff logic for copy verification."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import campaign as cmc
from ..clients import grid_finalize as gf
from ..clients import grid_read as gr
from .copy_verify_utils import (
    _OK, _MISMATCH, _MISSING, _UNREADABLE, _EXCLUDED,
    _nolog, _rj, _rj_dict, _strip_domain,
)


def diff_profiles(src_profile: Dict[str, dict],
                  tgt_profile: Dict[str, dict],
                  id_maps: dict) -> List[dict]:
    """Структурный diff профилей по 20+ измерениям (D1–D19, D2b условная, + 2 гео отдельно).

    Args:
        src_profile: Профиль источника {str(src_camp_id): {...}}.
        tgt_profile: Профиль цели {str(tgt_camp_id): {...}}.
        id_maps: Карта id_maps.json.

    Returns:
        List[{scope, dimension, status, source, target, repairable, repair_hint}]
    """
    camp_maps = id_maps.get("campaigns") or {}   # str(src_id) → tgt_id
    results: List[dict] = []

    def _row(scope: str, dimension: str, status: str,
             source: Any, target: Any,
             repairable: bool = False, repair_hint: str = "") -> dict:
        return {
            "scope": scope,
            "dimension": dimension,
            "status": status,
            "source": source,
            "target": target,
            "repairable": repairable,
            "repair_hint": repair_hint,
        }

    adgroup_maps = id_maps.get("adgroups") or {}

    def _project_group_signatures(raw: Any) -> dict:
        projected: dict = {}
        for src_gid, sigs in (raw or {}).items():
            tgt_gid = adgroup_maps.get(str(src_gid))
            key = str(tgt_gid) if tgt_gid else f"MISSING_GROUP:{src_gid}"
            projected[key] = sorted(sigs or [])
        return dict(sorted(projected.items()))

    def _target_group_signatures(raw: Any) -> dict:
        return {
            str(gid): sorted(sigs or [])
            for gid, sigs in sorted((raw or {}).items(), key=lambda kv: str(kv[0]))
        }

    for src_id, src_c in src_profile.items():
        tgt_id_raw = camp_maps.get(src_id)
        is_uac = bool(src_c.get("_is_uac"))

        # UAC кампании не попадают в id_maps["campaigns"] — это известная дыра сверки
        if not tgt_id_raw:
            hint = ("UAC tp6/tp7 не пишутся в id_maps['campaigns'] "
                    "(copy_engine.py §1, _copy_uac_results); "
                    "сверка через матч по имени/uac_results — TODO (recon §7)") if is_uac \
                else "Кампания не найдена в id_maps.campaigns — не была создана"
            results.append(_row(
                f"campaign:{src_id}→MISSING", "campaign_exists",
                _MISSING, src_id, None, False, hint,
            ))
            continue

        tgt_id = str(tgt_id_raw)
        scope = f"campaign:{src_id}→{tgt_id}"
        tgt_c = tgt_profile.get(tgt_id)

        if not tgt_c:
            results.append(_row(
                scope, "campaign_exists", _MISSING, src_id, tgt_id, False,
                "Кампания в id_maps, но не прочитана в целевом профиле — проверить Grid",
            ))
            continue

        reads_ok = tgt_c.get("_reads_ok") or {}

        # D1: adgroup count
        s1, t1 = src_c["adgroup_count"], tgt_c["adgroup_count"]
        results.append(_row(scope, "adgroup_count",
                            _OK if s1 == t1 else _MISMATCH, s1, t1,
                            repairable=False,
                            repair_hint="Нет мутатора 'добавить группу' в copy-пути — "
                                        "группа создаётся только в phase_upload (direct_copy.py:883)"))

        # D2: keyword count
        s2, t2 = src_c["kw_count"], tgt_c["kw_count"]
        if t2 is None:
            results.append(_row(scope, "keyword_count", _UNREADABLE, s2, None,
                                repairable=True,
                                repair_hint="step_keywords copy_steps.py:825 — Grid-first, 0 баллов"))
        else:
            results.append(_row(scope, "keyword_count",
                                _OK if s2 == t2 else _MISMATCH, s2, t2,
                                repairable=True,
                                repair_hint="step_keywords copy_steps.py:825"))

        # D2b: campaign negatives (report; ремонта нет — только settings_diff)
        s2b = src_c["camp_neg_count"]
        if s2b > 0:
            results.append(_row(scope, "campaign_neg_count", _UNREADABLE, s2b, None,
                                repairable=False,
                                repair_hint="step_settings_diff report-only; "
                                            "отдельного шага ремонта кампанийных минус-слов нет "
                                            "(copy_steps.py:1284 _DIFF_SKIP_KEYS — не ремонтируется)"))

        # D3: shared set count (ремап через maps["shared_sets"] → сравниваем COUNT)
        s3, t3 = src_c["shared_set_count"], tgt_c["shared_set_count"]
        if not reads_ok.get("invariants"):
            results.append(_row(scope, "shared_set_count", _UNREADABLE, s3, None,
                                repairable=True,
                                repair_hint="read_campaign_invariants grid_finalize.py:1107 не прочитал; "
                                            "привязка: UpdateCampaigns libraryMinusKeywordsIds "
                                            "(grid_finalize.py finalize:408)"))
        else:
            results.append(_row(scope, "shared_set_count",
                                _OK if s3 == t3 else _MISMATCH, s3, t3,
                                repairable=True,
                                repair_hint="UAC tp6/7: writer НЕТ (MasterCampaignSpec нет shared_sets); "
                                            "v5-путь: UpdateCampaigns libraryMinusKeywordsIds "
                                            "(grid_finalize.py finalize:408)"))

        # D4: promo
        s4, t4 = src_c["has_promo"], tgt_c["has_promo"]
        results.append(_row(scope, "promo_attached",
                            _OK if s4 == t4 else _MISMATCH, src_c["promo_id"], tgt_c["promo_id"],
                            repairable=True,
                            repair_hint="step_attach_promos copy_steps.py:497; "
                                        "PromoClient copy_engine.py:830"))

        # D5: adaptive titles (ads_with_titles vs adaptive_total)
        s5 = src_c["ads_with_titles"]
        t5 = tgt_c["ads_with_titles"]   # = adaptive_total от campaign_content_counts
        if t5 is None:
            results.append(_row(scope, "adaptive_titles_count", _UNREADABLE, s5, None,
                                repairable=True,
                                repair_hint="step_adaptive_creatives copy_steps.py:1049 — titles/bodies RMW; "
                                            "adaptive_ads_for_update grid_finalize.py:2537"))
        else:
            results.append(_row(scope, "adaptive_titles_count",
                                _OK if s5 == t5 else _MISMATCH, s5, t5,
                                repairable=True,
                                repair_hint="step_adaptive_creatives copy_steps.py:1049 — "
                                            "UpdateAdaptiveTextAds RMW titles/bodies"))

        # D6: texts — target прокси = adaptive_total (адаптивное объявление имеет и bodies, и titles;
        # отдельного счётчика bodies в campaign_content_counts нет, поэтому target использует тот же
        # proxy, что и D5). t6 берётся из явного поля ads_with_texts target-профиля, а не из t5.
        s6 = src_c["ads_with_texts"]
        t6 = tgt_c.get("ads_with_texts")   # = adaptive_total (proxy); None если не читалось
        if t6 is None:
            results.append(_row(scope, "adaptive_bodies_count", _UNREADABLE, s6, None,
                                repairable=True,
                                repair_hint="step_adaptive_creatives copy_steps.py:1049 — bodies RMW"))
        else:
            results.append(_row(scope, "adaptive_bodies_count",
                                _OK if s6 == t6 else _MISMATCH, s6, t6,
                                repairable=True,
                                repair_hint="step_adaptive_creatives copy_steps.py:1049 — bodies RMW"))

        # D7: callouts (edit_rows — campaign-level, но Grid может не вернуть черновик)
        s7, t7 = src_c["callout_count"], tgt_c["callout_count"]
        if not reads_ok.get("edit_rows"):
            results.append(_row(scope, "callout_count", _UNREADABLE, s7, None,
                                repairable=True,
                                repair_hint="campaigns_edit_rows не вернул кампанию (возможно черновик); "
                                            "step_attach_callouts copy_steps.py:332"))
        else:
            results.append(_row(scope, "callout_count",
                                _OK if s7 == t7 else _MISMATCH, s7, t7,
                                repairable=True,
                                repair_hint="step_attach_callouts copy_steps.py:332; "
                                            "grid.add_callouts grid_finalize.py:492"))

        # D8: sitelinks. Проверяем общий факт и отдельно уровни привязки:
        # campaign-level inheritableSet и ad-level SitelinkSetId на объявлениях.
        s8, t8 = src_c["has_sitelinks"], tgt_c["has_sitelinks"]
        if not reads_ok.get("edit_rows"):
            results.append(_row(scope, "sitelinks_present", _UNREADABLE, s8, None,
                                repairable=True,
                                repair_hint="campaigns_edit_rows не вернул кампанию (возможно черновик); "
                                            "step_attach_sitelinks copy_steps.py:387"))
        else:
            results.append(_row(scope, "sitelinks_present",
                                _OK if s8 == t8 else _MISMATCH, s8, t8,
                                repairable=True,
                                repair_hint="campaign-level: step_attach_sitelinks; "
                                            "ad-level: direct_copy phase_upload SitelinkSetId"))

        s8c = bool(src_c.get("campaign_has_sitelinks"))
        t8c = bool(tgt_c.get("campaign_has_sitelinks"))
        if not reads_ok.get("edit_rows"):
            results.append(_row(scope, "sitelinks_campaign_level_present", _UNREADABLE, s8c, None,
                                repairable=True,
                                repair_hint="campaigns_edit_rows не вернул campaign-level быстрые ссылки"))
        else:
            results.append(_row(scope, "sitelinks_campaign_level_present",
                                _OK if s8c == t8c else _MISMATCH, s8c, t8c,
                                repairable=True,
                                repair_hint="step_attach_sitelinks переносит inheritableSitelinkSet 1:1 по campaign"))

        s8a = src_c.get("ad_sitelinks_count")
        t8a = tgt_c.get("ad_sitelinks_count")
        if not reads_ok.get("ad_level_sitelinks"):
            results.append(_row(scope, "sitelinks_ad_level_count", _UNREADABLE, s8a, None,
                                repairable=True,
                                repair_hint="v5 ads.get не прочитал TextAd.SitelinkSetId на цели"))
        else:
            results.append(_row(scope, "sitelinks_ad_level_count",
                                _OK if s8a == t8a else _MISMATCH, s8a, t8a,
                                repairable=True,
                                repair_hint="direct_copy phase_upload должен сохранить SitelinkSetId на каждом объявлении"))

        # D9: images
        s9 = src_c["ads_with_images"]
        t9 = tgt_c["ads_with_images"]
        if t9 is None:
            results.append(_row(scope, "ads_with_images", _UNREADABLE, s9, None,
                                repairable=True,
                                repair_hint="repair_auto image upload; "
                                            "_enrich_adaptive_images grid_read.py:417"))
        else:
            results.append(_row(scope, "ads_with_images",
                                _OK if s9 == t9 else _MISMATCH, s9, t9,
                                repairable=True,
                                repair_hint="repair_auto image upload; "
                                            "UpdateAdaptiveTextAds imageHashes grid_finalize.py:2137"))

        # D10: audience/retargeting-привязки групп. GdGridOfferRetargeting (оферный
        # ретаргетинг фида) сюда не входит — это часть товарки, не пользовательские аудитории.
        s10_raw = src_c.get("audiences")
        t10_raw = tgt_c.get("audiences")
        if s10_raw is None or t10_raw is None:
            results.append(_row(scope, "audiences", _UNREADABLE, s10_raw, t10_raw,
                                repairable=False,
                                repair_hint="Grid не прочитал GdRetargeting-привязки аудиторий"))
        else:
            s10 = _project_group_signatures(s10_raw)
            t10 = _target_group_signatures(t10_raw)
            results.append(_row(scope, "audiences",
                                _OK if s10 == t10 else _MISMATCH, s10, t10,
                                repairable=False,
                                repair_hint="Аудитории/ретаргетинг групп должны совпадать 1в1"))

        # D11: bid_modifiers — excluded intentional
        results.append(_row(scope, "bid_modifiers", _EXCLUDED,
                            src_c.get("bid_modifier_types"), "our_standard",
                            repairable=False,
                            repair_hint="В _DIFF_SKIP_KEYS (copy_steps.py:1284); "
                                        "step_age_bidmods ставит −100% <18/18-24 (copy_steps.py:186)"))

        # D12: strategy name
        s12, t12 = src_c["strategy_name"], tgt_c["strategy_name"]
        if not reads_ok.get("edit_rows"):
            results.append(_row(scope, "strategy_name", _UNREADABLE, s12, None,
                                repairable=True,
                                repair_hint="campaigns_edit_rows не прочитан (grid_finalize.py:1071)"))
        elif not t12:
            results.append(_row(scope, "strategy_name", _UNREADABLE, s12, None,
                                repairable=True,
                                repair_hint="strategyName не в campaigns_edit_rows ответе; "
                                            "PFCMG-восстановление copy_engine.py:969"))
        else:
            results.append(_row(scope, "strategy_name",
                                _OK if s12 == t12 else _MISMATCH, s12, t12,
                                repairable=True,
                                repair_hint="PFCMG-восстановление copy_engine.py:969; "
                                            "strategy_fallback при создании direct_copy.py:437"))

        # D13: video
        s13 = src_c["ads_with_video"]
        t13 = tgt_c["ads_with_video"]
        if t13 is None:
            results.append(_row(scope, "ads_with_video", _UNREADABLE, s13, None,
                                repairable=True,
                                repair_hint="step_videos copy_steps.py:1177 — скачать→аплоуд→RMW; "
                                            "hasVideo из adaptive_ads_for_update (grid_finalize.py:2537); "
                                            "target adaptive_ads_for_update не прочитан"))
        else:
            results.append(_row(scope, "ads_with_video",
                                _OK if s13 == t13 else _MISMATCH, s13, t13,
                                repairable=True,
                                repair_hint="step_videos copy_steps.py:1110"))

        # D14: button/CTA — проверяем, если target adaptive_ads_for_update прочитан.
        s14 = src_c["ads_with_button"]
        t14 = tgt_c["ads_with_button"]
        if t14 is None:
            results.append(_row(scope, "button_cta", _UNREADABLE, s14, None,
                                repairable=False,
                                repair_hint="hasButton из adaptive_ads_for_update не прочитан"))
        else:
            results.append(_row(scope, "button_cta",
                                _OK if s14 == t14 else _MISMATCH, s14, t14,
                                repairable=False,
                                repair_hint="CTA должен сохраняться RMW в update_adaptive_ads; "
                                            "отдельного repair-шага пока нет"))

        # D15: adPrice — excluded intentional
        results.append(_row(scope, "ad_price", _EXCLUDED, None, None,
                            repairable=False,
                            repair_hint="Берётся из фида ЦЕЛИ, не источника "
                                        "(step_prices copy_steps.py:689; copy-fix-spec.md §УТОЧНЕНИЯ)"))

        # D16: UTM tracking (без домена). Target читается из bannerHrefParams (Grid CampaignsEditData,
        # 0 доп. запросов) через read_campaign_invariants → tri-state (None = поле не пришло).
        s16 = src_c["tracking_norm"]
        t16 = tgt_c["tracking_norm"]   # None если bannerHrefParams не в ответе Grid (tri-state)
        if t16 is None:
            results.append(_row(scope, "utm_tracking", _UNREADABLE, s16, None,
                                repairable=False,
                                repair_hint="bannerHrefParams не в ответе Grid (CampaignsEditData); "
                                            "domain-замена при копировании в direct_copy.py do_replace"))
        else:
            results.append(_row(scope, "utm_tracking",
                                _OK if s16 == t16 else _MISMATCH, s16, t16,
                                repairable=True,
                                repair_hint="Tracking с заменой домена (direct_copy.py do_replace); "
                                            "target читается из bannerHrefParams (grid_finalize.py:1237)"))

        # D17: site_monitoring — читаем через v5 Settings, потому Grid CampaignsEditData
        # это поле не отдаёт стабильно.
        s17 = src_c.get("site_monitoring")
        t17 = tgt_c.get("site_monitoring")
        if t17 is None:
            results.append(_row(scope, "site_monitoring", _UNREADABLE, s17, None,
                                repairable=True,
                                repair_hint="v5 campaigns.get не вернул ENABLE_SITE_MONITORING для цели"))
        else:
            results.append(_row(scope, "site_monitoring",
                                _OK if bool(s17) == bool(t17) else _MISMATCH, s17, t17,
                                repairable=True,
                                repair_hint="Settings.ENABLE_SITE_MONITORING должен совпадать с источником"))

        # D18: minus_places — copy 1в1 из source ExcludedSites в target disabledPlaces.
        s18 = src_c.get("minus_places")
        t18 = tgt_c.get("minus_places")
        if t18 is None:
            results.append(_row(scope, "minus_places", _UNREADABLE, s18, None,
                                repairable=True,
                                repair_hint="target disabledPlaces не прочитаны из CampaignsEditData"))
        else:
            results.append(_row(scope, "minus_places",
                                _OK if s18 == t18 else _MISMATCH, s18, t18,
                                repairable=True,
                                repair_hint="step_disabled_places копирует disabledPlaces источника 1в1"))

        # D19: shopping/listing filters. Source = len(shopping_ads.json) для SHOPPING_AD
        # и count LISTING_AD из ads.json. Target — v501/v5 live types. Оба типа проверяем
        # отдельно: ListingAd не должен превращаться во второй ShoppingAd.
        s19 = src_c["shopping_count"]
        t19 = tgt_c["shopping_count"]   # None если v5 fallback не отработал
        if s19 > 0:
            if t19 is None:
                results.append(_row(scope, "shopping_filter_count", _UNREADABLE, s19, None,
                                    repairable=True,
                                    repair_hint="_enrich_shopping_bodies проверяет только пустые тексты, "
                                                "не фильтры (grid_read.py:591); "
                                                "writer: grid.add_shopping_ads grid_finalize.py:1801"))
            else:
                results.append(_row(scope, "shopping_filter_count",
                                    _OK if s19 == t19 else _MISMATCH, s19, t19,
                                    repairable=True,
                                    repair_hint="grid.add_shopping_ads grid_finalize.py:1801"))
        elif t19 is not None and t19 > 0:
            # Источник без шоппинга, но на цели появились SMART_AD — явный рассинхрон.
            # t19 is None → пропускаем (fallback не отработал, нельзя отличить от «нет шоппинга»).
            results.append(_row(scope, "shopping_filter_count",
                                _MISMATCH, 0, t19,
                                repairable=False,
                                repair_hint="На цели SMART_AD без источника — лишние объявления; "
                                            "ручная проверка / удаление через Grid"))

        src_shop_sig = _project_group_signatures(
            src_c.get("shopping_filter_signatures_by_group")
        )
        tgt_shop_sig = _target_group_signatures(
            tgt_c.get("shopping_filter_signatures_by_group")
        )
        if src_shop_sig or tgt_shop_sig:
            if not tgt_c.get("product_filters_readable"):
                results.append(_row(scope, "shopping_filter_signature", _UNREADABLE,
                                    src_shop_sig, None,
                                    repairable=False,
                                    repair_hint="Grid не отдал feedFilter товарных объявлений"))
            else:
                results.append(_row(scope, "shopping_filter_signature",
                                    _OK if src_shop_sig == tgt_shop_sig else _MISMATCH,
                                    src_shop_sig, tgt_shop_sig,
                                    repairable=False,
                                    repair_hint="Фильтры ShoppingAd должны совпадать 1в1"))

        s19l = src_c.get("listing_count")
        t19l = tgt_c.get("listing_count")
        if s19l:
            if t19l is None:
                results.append(_row(scope, "listing_filter_count", _UNREADABLE, s19l, None,
                                    repairable=True,
                                    repair_hint="v5/v501 ads.get не прочитал LISTING_AD на цели"))
            else:
                results.append(_row(scope, "listing_filter_count",
                                    _OK if s19l == t19l else _MISMATCH, s19l, t19l,
                                    repairable=True,
                                    repair_hint="direct_copy.phase_upload должен создать ListingAd 1в1"))
        elif t19l is not None and t19l > 0:
            results.append(_row(scope, "listing_filter_count",
                                _MISMATCH, 0, t19l,
                                repairable=False,
                                repair_hint="На цели LISTING_AD без источника — лишние объявления"))

        src_listing_sig = _project_group_signatures(
            src_c.get("listing_filter_signatures_by_group")
        )
        tgt_listing_sig = _target_group_signatures(
            tgt_c.get("listing_filter_signatures_by_group")
        )
        if src_listing_sig or tgt_listing_sig:
            if not tgt_c.get("product_filters_readable"):
                results.append(_row(scope, "listing_filter_signature", _UNREADABLE,
                                    src_listing_sig, None,
                                    repairable=False,
                                    repair_hint="Grid не отдал feedFilter каталожных объявлений"))
            else:
                results.append(_row(scope, "listing_filter_signature",
                                    _OK if src_listing_sig == tgt_listing_sig else _MISMATCH,
                                    src_listing_sig, tgt_listing_sig,
                                    repairable=False,
                                    repair_hint="Фильтры ListingAd должны совпадать 1в1"))

    return results

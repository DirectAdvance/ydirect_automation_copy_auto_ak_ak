"""Profile diff logic for copy verification."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core import campaign as cmc
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
    capped_ad_dimensions = {
        "adaptive_titles_count",
        "adaptive_bodies_count",
        "sitelinks_ad_level_count",
        "ads_with_images",
        "ads_with_video",
        "button_cta",
    }

    def _is_accepted_ad_cap(src_c: dict, source: Any, target: Any) -> bool:
        """Direct can reject the 4th adaptive text banner in a legacy one-group campaign.

        Semen accepted this as 3/4 copy for Agent Board #65. Keep it narrow: only treat
        one-group source campaigns with exactly four source ads and three target ads as
        intentional, so ordinary content loss still stays visible.
        """
        try:
            return (
                int(src_c.get("adgroup_count") or 0) == 1
                and int(src_c.get("_ads_count") or 0) == 4
                and int(source) == 4
                and int(target) == 3
            )
        except (TypeError, ValueError):
            return False

    def _status_with_ad_cap(src_c: dict, dimension: str, source: Any, target: Any) -> str:
        if source == target:
            return _OK
        if dimension in capped_ad_dimensions and _is_accepted_ad_cap(src_c, source, target):
            return _EXCLUDED
        return _MISMATCH

    def _target_extra_optional_status(source: Any, target: Any) -> str:
        """Extra target-only optional assets are not a failed copy.

        The settled gate must catch loss from source to target. Some postprocess
        steps can leave harmless target-only enrichments (campaign-level sitelinks,
        videos, listing filters) even when the source count was zero; those should
        stay visible in the report without blocking the retry loop.
        """
        if source == target:
            return _OK
        try:
            if int(source or 0) == 0 and int(target or 0) > 0:
                return _EXCLUDED
        except (TypeError, ValueError):
            pass
        return _MISMATCH

    def _hint_with_ad_cap(base: str, status: str) -> str:
        if status == _EXCLUDED:
            return (
                "Direct Grid limit: MAX_ADAPTIVE_TEXT_BANNERS_IN_ADGROUP max=3; "
                "legacy source has 4 ads in one group, accepted as 3/4 by operator"
            )
        return base

    def _group_sig_is_empty(sigs: Any) -> bool:
        """FIX D (2026-08-05): a group whose every recorded signature is ``[]`` (an ad with
        zero FeedFilterConditions) carries no real filter — e.g. ``{gid: [[]]}``. That must
        compare as equivalent to the group being absent entirely, not as a mismatch against
        the other side which genuinely has no entry for this group (11 ложных строк)."""
        return not any(sig for sig in (sigs or []))

    def _project_group_signatures(raw: Any) -> dict:
        projected: dict = {}
        for src_gid, sigs in (raw or {}).items():
            sorted_sigs = sorted(sigs or [])
            if _group_sig_is_empty(sorted_sigs):
                continue
            tgt_gid = adgroup_maps.get(str(src_gid))
            key = str(tgt_gid) if tgt_gid else f"MISSING_GROUP:{src_gid}"
            projected[key] = sorted_sigs
        return dict(sorted(projected.items()))

    def _target_group_signatures(raw: Any) -> dict:
        projected: dict = {}
        for gid, sigs in sorted((raw or {}).items(), key=lambda kv: str(kv[0])):
            sorted_sigs = sorted(sigs or [])
            if _group_sig_is_empty(sorted_sigs):
                continue
            projected[str(gid)] = sorted_sigs
        return projected

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
            _st5 = _status_with_ad_cap(src_c, "adaptive_titles_count", s5, t5)
            results.append(_row(scope, "adaptive_titles_count",
                                _st5, s5, t5,
                                repairable=(_st5 != _EXCLUDED),
                                repair_hint=_hint_with_ad_cap(
                                    "step_adaptive_creatives copy_steps.py:1049 — "
                                    "UpdateAdaptiveTextAds RMW titles/bodies",
                                    _st5,
                                )))

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
            _st6 = _status_with_ad_cap(src_c, "adaptive_bodies_count", s6, t6)
            results.append(_row(scope, "adaptive_bodies_count",
                                _st6, s6, t6,
                                repairable=(_st6 != _EXCLUDED),
                                repair_hint=_hint_with_ad_cap(
                                    "step_adaptive_creatives copy_steps.py:1049 — bodies RMW",
                                    _st6,
                                )))

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
            _st8c = _OK if s8c == t8c else _MISMATCH
            if not s8c and t8c and bool(s8) and bool(t8):
                _st8c = _EXCLUDED
            results.append(_row(scope, "sitelinks_campaign_level_present",
                                _st8c, s8c, t8c,
                                repairable=(_st8c != _EXCLUDED),
                                repair_hint=(
                                    "target-only campaign-level sitelinks are accepted when "
                                    "overall sitelinks_present matches"
                                    if _st8c == _EXCLUDED
                                    else "step_attach_sitelinks переносит inheritableSitelinkSet 1:1 по campaign"
                                )))

        s8a = src_c.get("ad_sitelinks_count")
        t8a = tgt_c.get("ad_sitelinks_count")
        if not reads_ok.get("ad_level_sitelinks"):
            results.append(_row(scope, "sitelinks_ad_level_count", _UNREADABLE, s8a, None,
                                repairable=True,
                                repair_hint="v5 ads.get не прочитал TextAd.SitelinkSetId на цели"))
        else:
            _st8a = _status_with_ad_cap(src_c, "sitelinks_ad_level_count", s8a, t8a)
            results.append(_row(scope, "sitelinks_ad_level_count",
                                _st8a, s8a, t8a,
                                repairable=(_st8a != _EXCLUDED),
                                repair_hint=_hint_with_ad_cap(
                                    "direct_copy phase_upload должен сохранить SitelinkSetId на каждом объявлении",
                                    _st8a,
                                )))

        # D9: images
        s9 = src_c["ads_with_images"]
        t9 = tgt_c["ads_with_images"]
        if t9 is None:
            results.append(_row(scope, "ads_with_images", _UNREADABLE, s9, None,
                                repairable=True,
                                repair_hint="repair_auto image upload; "
                                            "_enrich_adaptive_images grid_read.py:417"))
        else:
            _st9 = _status_with_ad_cap(src_c, "ads_with_images", s9, t9)
            results.append(_row(scope, "ads_with_images",
                                _st9, s9, t9,
                                repairable=(_st9 != _EXCLUDED),
                                repair_hint=_hint_with_ad_cap(
                                    "repair_auto image upload; "
                                    "UpdateAdaptiveTextAds imageHashes grid_finalize.py:2137",
                                    _st9,
                                )))

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
            _st13 = _status_with_ad_cap(src_c, "ads_with_video", s13, t13)
            if _st13 == _MISMATCH:
                _st13 = _target_extra_optional_status(s13, t13)
            results.append(_row(scope, "ads_with_video",
                                _st13, s13, t13,
                                repairable=(_st13 != _EXCLUDED),
                                repair_hint=_hint_with_ad_cap("step_videos copy_steps.py:1110", _st13)))

        # D14: button/CTA — проверяем, если target adaptive_ads_for_update прочитан.
        # Semen 2026-08-05: copy добавляет CTA-кнопку «Получить скидку» (GET_DISCOUNT) на
        # адаптивные текстовые объявления, которых не было у источника — намеренный
        # copy-стандарт (live 77/77 объявлений, полный Grid payload с обеих сторон, не
        # непрочитанный snapshot), см. _COPY_VERIFY_REPORT_ONLY_DIMENSIONS. Асимметрия:
        # только target⊃source (0→N) исключается через _target_extra_optional_status —
        # source⊃target (кнопка была и пропала) остаётся mismatch как раньше.
        s14 = src_c["ads_with_button"]
        t14 = tgt_c["ads_with_button"]
        s14_readable = src_c.get("ads_with_button_readable", True)
        if t14 is None:
            results.append(_row(scope, "button_cta", _UNREADABLE, s14, None,
                                repairable=False,
                                repair_hint="hasButton из adaptive_ads_for_update не прочитан"))
        elif not s14_readable:
            # Latent-bug guard: Grid snapshot без payload для всех подходящих ads кампании
            # — TextAd fallback hasButton не несёт вовсе, значит source "0" здесь неизвестен,
            # не "нет кнопок". Не путать с target-extra-стандартом выше: unreadable source
            # никогда не может быть молча признан excluded_intentional.
            results.append(_row(scope, "button_cta", _UNREADABLE, None, t14,
                                repairable=False,
                                repair_hint="hasButton источника не прочитан: Grid snapshot без "
                                            "payload для объявлений кампании, TextAd fallback "
                                            "hasButton не поддерживает (copy_verify_source.py)"))
        else:
            _st14 = _status_with_ad_cap(src_c, "button_cta", s14, t14)
            if _st14 == _MISMATCH:
                _st14 = _target_extra_optional_status(s14, t14)
            if _st14 == _EXCLUDED and _is_accepted_ad_cap(src_c, s14, t14):
                _hint14 = _hint_with_ad_cap(
                    "CTA должен сохраняться RMW в update_adaptive_ads; "
                    "отдельного repair-шага пока нет",
                    _st14,
                )
            elif _st14 == _EXCLUDED:
                _hint14 = ("Решение Семёна 2026-08-05: copy намеренно добавляет CTA-кнопку "
                           "«Получить скидку» (GET_DISCOUNT) на адаптивные объявления — "
                           "источник её не нёс, это стандарт копирования, не потеря")
            else:
                _hint14 = ("CTA должен сохраняться RMW в update_adaptive_ads; "
                           "отдельного repair-шага пока нет")
            results.append(_row(scope, "button_cta",
                                _st14, s14, t14,
                                repairable=False,
                                repair_hint=_hint14))

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
        # Semen 2026-08-05: grid_finalize.py:1200-1202 set_campaign_invariants форсирует
        # hasSiteMonitoring=True при любом invariant-репейре — намеренный copy-стандарт
        # (~24-32 строк across jobs), см. _COPY_VERIFY_REPORT_ONLY_DIMENSIONS. Асимметрия:
        # source False → target True — excluded_intentional; source True → target False
        # (мониторинг был включён и пропал) — реальная потеря, остаётся mismatch.
        s17 = src_c.get("site_monitoring")
        t17 = tgt_c.get("site_monitoring")
        if t17 is None:
            results.append(_row(scope, "site_monitoring", _UNREADABLE, s17, None,
                                repairable=True,
                                repair_hint="v5 campaigns.get не вернул ENABLE_SITE_MONITORING для цели"))
        elif s17 is None:
            results.append(_row(scope, "site_monitoring", _UNREADABLE, None, t17,
                                repairable=False,
                                repair_hint="v5 campaigns.get не вернул ENABLE_SITE_MONITORING для источника"))
        elif bool(s17) == bool(t17):
            results.append(_row(scope, "site_monitoring", _OK, s17, t17,
                                repairable=True,
                                repair_hint="Settings.ENABLE_SITE_MONITORING должен совпадать с источником"))
        elif not bool(s17) and bool(t17):
            results.append(_row(scope, "site_monitoring", _EXCLUDED, s17, t17,
                                repairable=False,
                                repair_hint="Решение Семёна 2026-08-05: grid_finalize.py:1200-1202 "
                                            "set_campaign_invariants форсирует hasSiteMonitoring=True "
                                            "при любом invariant-репейре — намеренный copy-стандарт, "
                                            "не потеря"))
        else:
            results.append(_row(scope, "site_monitoring", _MISMATCH, s17, t17,
                                repairable=True,
                                repair_hint="Settings.ENABLE_SITE_MONITORING должен совпадать с "
                                            "источником — источник включён, цель выключена, реальная потеря"))

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
                                _target_extra_optional_status(0, t19l), 0, t19l,
                                repairable=False,
                                repair_hint="Target-only ListingAd filters are reported but do not block copy"))

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

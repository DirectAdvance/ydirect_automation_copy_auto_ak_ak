"""Target profile builder for copy verification."""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import campaign as cmc
from . import grid_finalize as gf
from . import grid_read as gr
from .copy_verify_utils import (
    _OK, _MISMATCH, _MISSING, _UNREADABLE, _EXCLUDED,
    _feed_filter_signature, _nolog, _read_audience_signatures_grid,
    _rj, _rj_dict, _strip_domain,
)
from . import copy_verify_state as _state


def _read_product_filter_signatures_grid(
    grid: gf.GridClient,
    login: str,
    campaign_ids: List[int],
    log: Callable[[str], None],
) -> tuple[Dict[int, dict], bool]:
    """Прочитать feedFilter товарных/каталожных объявлений через Grid.

    v5/v501 надёжны для count fallback, но тело фильтра на свежих draft может приходить
    ``null``. Grid возвращает реальную форму ``feedFilter.conditions``.
    """
    out: Dict[int, dict] = {}
    if not campaign_ids:
        return out, True
    q = (
        "query CopyProductFilters($login:String!,$inp:GdAdsContainerInput!){"
        "client(searchBy:{login:$login}){ads(input:$inp){rowset{id adGroupId campaignId __typename "
        "...on GdShoppingAd{feedFilter{tab conditions{field operator stringValue}}} "
        "...on GdListingAd{feedFilter{tab conditions{field operator stringValue}}}}}}}"
    )
    try:
        grid._bootstrap_csrf()
    except Exception as exc:  # noqa: BLE001
        log(f"copy_verify: product feedFilter csrf error: {str(exc)[:160]}")
        return out, False
    try:
        for i in range(0, len(campaign_ids), 50):
            chunk = [str(x) for x in campaign_ids[i:i + 50]]
            offset = 0
            limit = 10000
            while True:
                inp = {
                    "filter": {"campaignIdIn": chunk},
                    "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
                    "limitOffset": {"limit": limit, "offset": offset},
                    "orderBy": [{"order": "ASC", "field": "ID"}],
                }
                r = grid._post("CopyProductFilters", q, {"login": login, "inp": inp})
                data = r.json()
                if data.get("errors"):
                    log(f"copy_verify: product feedFilter grid errors: {str(data.get('errors'))[:180]}")
                    return out, False
                rows = ((((data.get("data") or {}).get("client") or {}).get("ads") or {}).get("rowset") or [])
                for row in rows:
                    tn = str(row.get("__typename") or "")
                    if tn not in ("GdShoppingAd", "GdListingAd"):
                        continue
                    try:
                        cid = int(row.get("campaignId") or 0)
                    except (TypeError, ValueError):
                        continue
                    gid = str(row.get("adGroupId") or "")
                    if cid <= 0 or not gid:
                        continue
                    bucket_name = "shopping" if tn == "GdShoppingAd" else "listing"
                    rec = out.setdefault(cid, {
                        "shopping": {},
                        "listing": {},
                        "shopping_count": 0,
                        "listing_count": 0,
                    })
                    rec[f"{bucket_name}_count"] = int(rec.get(f"{bucket_name}_count") or 0) + 1
                    rec[bucket_name].setdefault(gid, []).append(
                        _feed_filter_signature(row.get("feedFilter"))
                    )
                if len(rows) < limit:
                    break
                offset += limit
    except Exception as exc:  # noqa: BLE001
        log(f"copy_verify: product feedFilter grid error: {str(exc)[:180]}")
        return out, False
    for rec in out.values():
        rec["shopping"] = {k: sorted(v) for k, v in (rec.get("shopping") or {}).items()}
        rec["listing"] = {k: sorted(v) for k, v in (rec.get("listing") or {}).items()}
    return out, True


def build_target_profile(target_login: str,
                          id_maps: dict,
                          grid: Optional[gf.GridClient] = None,
                          cached_counts: Optional[Dict[int, dict]] = None,
                          cached_edit_rows: Optional[Dict[int, dict]] = None,
                          cached_invariants: Optional[Dict[int, dict]] = None,
                          cached_adaptive: Optional[Dict[int, dict]] = None,
                          log: Optional[Callable[[str], None]] = None,
                          target_agency: str = "",
                          ) -> Dict[str, dict]:
    """Нормализованный профиль цели по созданным кампаниям через Grid/cookie.

    ПЕРЕИСПОЛЬЗУЕТ уже прочитанное в постпроцессе: если caller передаёт
    cached_counts / cached_edit_rows / cached_invariants / cached_adaptive,
    повторных Grid-запросов не делается.

    Args:
        target_login: Логин целевого аккаунта.
        id_maps: id_maps.json (str(src_id) → tgt_id по ключам campaigns/ads/etc.).
        grid: Опциональный pre-built gf.GridClient (цели). Если None — строится сам.
        cached_counts: Результат GridReadClient.campaign_content_counts.
        cached_edit_rows: Результат gf.GridClient.campaigns_edit_rows.
        cached_invariants: Результат gf.GridClient.read_campaign_invariants.
        cached_adaptive: Результат gf.GridClient.adaptive_ads_for_update цели
            {int(ad_id): {...}}.
        log: Функция логирования.
        target_agency: Агентство цели (для получения v5-токена в fallback).

    Returns:
        {str(tgt_campaign_id): {dimension_key: value}}
    """
    _log = log or _nolog
    camp_maps = id_maps.get("campaigns") or {}    # str(src_id) → tgt_id

    tgt_camp_ids: List[int] = []
    for tgt_id_raw in camp_maps.values():
        try:
            cid = int(tgt_id_raw)
        except (TypeError, ValueError):
            continue
        if cid > 0 and cid not in tgt_camp_ids:
            tgt_camp_ids.append(cid)

    if not tgt_camp_ids:
        return {}

    tgt_ad_ids: List[int] = []
    for tgt_ad_raw in (id_maps.get("ads") or {}).values():
        try:
            aid = int(tgt_ad_raw)
        except (TypeError, ValueError):
            continue
        if aid > 0 and aid not in tgt_ad_ids:
            tgt_ad_ids.append(aid)

    # ── Строим Grid-клиенты если не переданы ────────────────────────────────
    _grid = grid
    _grc: Optional[gr.GridReadClient] = None
    try:
        if _grid is None:
            client = cmc.build_client(target_login)
            cookie = client.sess.headers.get("Cookie") or ""
            _grid = gf.GridClient(target_login, cookie=cookie)
        # GridReadClient можно строить из той же куки, что и _grid
        _grc = gr.GridReadClient(target_login, cookie=_grid.cookie)
    except Exception as e:
        _log(f"copy_verify: build_clients error: {str(e)[:200]}")
        return {}

    # ── campaign_content_counts ──────────────────────────────────────────────
    counts: Dict[int, dict] = {}
    try:
        if cached_counts is not None:
            counts = cached_counts
        else:
            counts = _grc.campaign_content_counts(tgt_camp_ids)
    except Exception as e:
        _log(f"copy_verify: campaign_content_counts error: {str(e)[:200]}")

    # ── campaigns_edit_rows ──────────────────────────────────────────────────
    edit_rows: Dict[int, dict] = {}
    try:
        if cached_edit_rows is not None:
            edit_rows = cached_edit_rows
        else:
            edit_rows = _grid.campaigns_edit_rows(tgt_camp_ids)
    except Exception as e:
        _log(f"copy_verify: campaigns_edit_rows error: {str(e)[:200]}")

    # ── read_campaign_invariants ─────────────────────────────────────────────
    invariants: Dict[int, dict] = {}
    try:
        if cached_invariants is not None:
            invariants = cached_invariants
        else:
            invariants = _grid.read_campaign_invariants(tgt_camp_ids)
    except Exception as e:
        _log(f"copy_verify: read_campaign_invariants error: {str(e)[:200]}")

    # ── adaptive_ads_for_update (цель) для D13/D14 ──────────────────────────
    # Если caller уже собрал cached_adaptive, используем его без повторного Grid-чтения.
    # При ручной/осевшей пере-сверке кэша может не быть — дочитываем target adaptive сами,
    # иначе видео/CTA висели как unreadable даже при наличии id_maps["ads"].
    adaptive_src = cached_adaptive
    if adaptive_src is None and tgt_ad_ids:
        try:
            adaptive_src = _grid.adaptive_ads_for_update(tgt_camp_ids, tgt_ad_ids) or {}
        except Exception as e:
            _log(f"copy_verify: target adaptive_ads_for_update error: {str(e)[:200]}")
            adaptive_src = None
    adaptive_by_campaign: Dict[int, dict] = {}
    for comp in (adaptive_src or {}).values():
        try:
            cid = int(comp.get("campaignId") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if cid <= 0:
            continue
        rec = adaptive_by_campaign.setdefault(cid, {"video": 0, "button": 0})
        if comp.get("creativeIds") or comp.get("hasVideo"):
            rec["video"] += 1
        if comp.get("hasButton"):
            rec["button"] += 1

    target_campaign_v5: Dict[int, dict] = {}

    def _strategy_name_from_campaign(camp: dict) -> str:
        for skey in ("TextCampaign", "DynamicTextCampaign", "SmartCampaign",
                     "CpmBannerCampaign", "UnifiedAdCampaign"):
            stype = camp.get(skey)
            if not isinstance(stype, dict):
                continue
            bid_strat = stype.get("BiddingStrategy") or {}
            return (
                (bid_strat.get("Search") or {}).get("BiddingStrategyType") or
                (bid_strat.get("Network") or {}).get("BiddingStrategyType") or ""
            )
        return ""

    def _setting_yes(camp: dict, option: str) -> Optional[bool]:
        for skey in ("TextCampaign", "DynamicTextCampaign", "SmartCampaign",
                     "CpmBannerCampaign", "UnifiedAdCampaign"):
            settings = (camp.get(skey) or {}).get("Settings") or []
            for item in settings if isinstance(settings, list) else []:
                if item.get("Option") == option:
                    return str(item.get("Value") or "").upper() == "YES"
        return None

    # ── v5 fallback для черновиков (Draft/State=OFF) ─────────────────────────
    # Grid entity-запросы фильтруют по statRequirements (LAST_30DAYS): свежие черновики
    # (0 impressions) отдают структуру (группы), но 0 КОНТЕНТА (ключи/объявления=0).
    # Триггерим на 0 КОНТЕНТА (keywords_count пуст), а НЕ на 0 групп — Grid группы отдаёт,
    # поэтому старое условие adgroups==0 не срабатывало (no-op). v5.get без stat-фильтра
    # даёт реальные счётчики; ОБЯЗАТЕЛЬНА пагинация (548 групп / ~38k ключей > одной страницы).
    # Per-campaign v5-чтение теперь безопасно (не 4001) → добираем ВСЕ кампании: Grid stat-счётчики
    # ненадёжны для свежих черновиков не только при 0, но и при частичных значениях. Точные v5-счёта.
    _draft_cids = list(tgt_camp_ids)
    if _draft_cids and _state._v5_call is not None and _state._token_for_login is not None and _state._direct_tokens is not None:
        try:
            _tr = _state._token_for_login(target_login, target_agency or "", _state._direct_tokens())
            _tok = _tr[0] if isinstance(_tr, (tuple, list)) else _tr
            if _tok:
                _log(f"copy_verify: v5 fallback (0 контента) для {len(_draft_cids)} кампаний")

                def _v5_paged(_svc: str, _key: str, _fields: list) -> list:
                    # ПО ОДНОЙ кампании: batch CampaignIds на смешанном наборе (TextCampaign +
                    # UAC/товарка) даёт API 4001 → 0 (ложный tgt=0). Per-campaign безопасно.
                    def _one(_dcid: int) -> list:
                        _items_all: list = []
                        _off = 0
                        for _ in range(50):
                            try:
                                _r = _state._v5_call(_svc, "get", _tok, target_login, {
                                    "SelectionCriteria": {"CampaignIds": [_dcid]},
                                    "FieldNames": _fields,
                                    "Page": {"Limit": 10000, "Offset": _off},
                                })
                                _res = _r.get("result") or {}
                                _items_all += (_res.get(_key) or [])
                                _lb = _res.get("LimitedBy")
                                if _lb:
                                    _off = int(_lb)
                                    continue
                            except Exception as _ce:  # noqa: BLE001
                                _log(f"copy_verify: v5 {_svc}.get cid={_dcid} error: {str(_ce)[:140]}")
                            break
                        return _items_all

                    _out: list = []
                    with ThreadPoolExecutor(max_workers=min(2, len(_draft_cids))) as _pool:
                        for _items in _pool.map(_one, list(_draft_cids)):
                            _out += _items
                    return _out

                try:
                    _camp_r = _state._v5_call("campaigns", "get", _tok, target_login, {
                        "SelectionCriteria": {"Ids": _draft_cids},
                        "FieldNames": ["Id", "Type"],
                        "TextCampaignFieldNames": ["Settings", "BiddingStrategy"],
                        "DynamicTextCampaignFieldNames": ["Settings", "BiddingStrategy"],
                        "SmartCampaignFieldNames": ["Settings", "BiddingStrategy"],
                        "CpmBannerCampaignFieldNames": ["Settings", "BiddingStrategy"],
                        "Page": {"Limit": 10000, "Offset": 0},
                    })
                    for _camp in ((_camp_r.get("result") or {}).get("Campaigns") or []):
                        try:
                            _cid3 = int(_camp.get("Id") or 0)
                        except (TypeError, ValueError):
                            continue
                        if _cid3 > 0:
                            target_campaign_v5[_cid3] = _camp
                except Exception as _cve:  # noqa: BLE001
                    _log(f"copy_verify: v5 campaigns.get details error: {str(_cve)[:160]}")

                # adgroups — реальное число групп на кампанию (перекрывает Grid-нуль)
                _ag_cnt: Dict[int, int] = {}
                for _ag in _v5_paged("adgroups", "AdGroups", ["Id", "CampaignId"]):
                    try:
                        _ag_cnt[int(_ag.get("CampaignId") or 0)] = _ag_cnt.get(int(_ag.get("CampaignId") or 0), 0) + 1
                    except (TypeError, ValueError):
                        pass
                for _cid2, _n in _ag_cnt.items():
                    if _cid2 in counts:
                        counts[_cid2]["adgroups"] = _n
                # keywords
                _kw_cnt: Dict[int, int] = {}
                for _kw in _v5_paged("keywords", "Keywords", ["Id", "CampaignId"]):
                    try:
                        _kw_cnt[int(_kw.get("CampaignId") or 0)] = _kw_cnt.get(int(_kw.get("CampaignId") or 0), 0) + 1
                    except (TypeError, ValueError):
                        pass
                for _cid2, _n in _kw_cnt.items():
                    if _cid2 in counts:
                        counts[_cid2]["keywords_count"] = _n
                        counts[_cid2]["keywords_read"] = True
                # ads → adaptive_total + shopping/listing counts.
                # Добавляем Type в FieldNames — тот же запрос, 0 доп. обращений.
                _ads_cnt: Dict[int, int] = {}
                _shop_cnt: Dict[int, int] = {}
                _listing_cnt: Dict[int, int] = {}
                for _ad in _v5_paged("ads", "Ads", ["Id", "CampaignId", "Type"]):
                    try:
                        _ad_cid = int(_ad.get("CampaignId") or 0)
                    except (TypeError, ValueError):
                        continue
                    if _ad_cid <= 0:
                        continue
                    _ads_cnt[_ad_cid] = _ads_cnt.get(_ad_cid, 0) + 1
                    if (_ad.get("Type") or "") in ("SHOPPING_AD", "SMART_AD"):
                        _shop_cnt[_ad_cid] = _shop_cnt.get(_ad_cid, 0) + 1
                    if (_ad.get("Type") or "") == "LISTING_AD":
                        _listing_cnt[_ad_cid] = _listing_cnt.get(_ad_cid, 0) + 1
                for _cid2, _n in _ads_cnt.items():
                    if _cid2 in counts and counts[_cid2].get("adaptive_total") in (None, 0):
                        counts[_cid2]["adaptive_total"] = _n
                for _cid2, _n in _shop_cnt.items():
                    if _cid2 in counts:
                        counts[_cid2]["shopping_count_v5"] = _n
                for _cid2 in _draft_cids:
                    if _cid2 in counts:
                        counts[_cid2]["listing_count_v5"] = _listing_cnt.get(_cid2, 0)
                _log(f"copy_verify: v5 fallback завершён — групп/ключей/объявл добрано; "
                     f"шоппинг-объявл: {sum(_shop_cnt.values())} по {len(_shop_cnt)} кампаниям, "
                     f"листингов: {sum(_listing_cnt.values())} по {len(_listing_cnt)} кампаниям")
        except Exception as _v5e:
            _log(f"copy_verify: v5 fallback error: {str(_v5e)[:200]}")

    # ── Ad-level sitelinks/images через v5 (для ВСЕХ target-кампаний) ─────────
    # Sitelinks/картинки копируются на ОБЪЯВЛЕНИЯ; verify (edit_rows) читает inheritable на
    # уровне КАМПАНИИ → 0 при per-ad привязке → ложный mismatch. Добираем ad-level v5.
    if _state._v5_call is not None and _state._token_for_login is not None and _state._direct_tokens is not None:
        try:
            _tr2 = _state._token_for_login(target_login, target_agency or "", _state._direct_tokens())
            _tok2 = _tr2[0] if isinstance(_tr2, (tuple, list)) else _tr2
            if _tok2:
                _sl_by: Dict[int, int] = {}
                _img_by: Dict[int, int] = {}
                # ПО ОДНОЙ кампании: ads.get с TextAdFieldNames на СМЕШАННОМ наборе (TextCampaign +
                # UAC/товарка вместе) даёт error 4001 → 0 объявлений (ложный mismatch по sitelinks/
                # images). Per-campaign: UAC вернёт 0 TextAds без ошибки, TextCampaign — свои.
                # Доказано: batch(13)=0/4001, per-campaign=1152 объявл/218 sitelinks на той же цели.
                def _ads_level_one(_cid: int) -> tuple[int, int, int]:
                    _sl_n = 0
                    _img_n = 0
                    _off2 = 0
                    for _ in range(50):
                        try:
                            _r2 = _state._v5_call("ads", "get", _tok2, target_login, {
                                "SelectionCriteria": {"CampaignIds": [_cid]},
                                "FieldNames": ["Id", "CampaignId"],
                                "TextAdFieldNames": ["SitelinkSetId", "AdImageHash"],
                                "DynamicTextAdFieldNames": ["SitelinkSetId"],
                                "Page": {"Limit": 10000, "Offset": _off2}})
                            _res2 = _r2.get("result") or {}
                            for _ad in (_res2.get("Ads") or []):
                                _ta = _ad.get("TextAd") or {}
                                _dta = _ad.get("DynamicTextAd") or {}
                                if _ta.get("SitelinkSetId") or _dta.get("SitelinkSetId"):
                                    _sl_n += 1
                                if _ta.get("AdImageHash"):
                                    _img_n += 1
                            _lb2 = _res2.get("LimitedBy")
                            if _lb2:
                                _off2 = int(_lb2)
                                continue
                        except Exception as _ae:  # noqa: BLE001
                            _log(f"copy_verify: ad-level v5 ads.get cid={_cid} error: {str(_ae)[:140]}")
                        break
                    return _cid, _sl_n, _img_n

                _tgt_cids_list = list(tgt_camp_ids)
                if _tgt_cids_list:
                    with ThreadPoolExecutor(max_workers=min(2, len(_tgt_cids_list))) as _pool2:
                        for _cid, _sl_n, _img_n in _pool2.map(_ads_level_one, _tgt_cids_list):
                            if _cid in counts:
                                counts[_cid]["sitelinks_ad_level_count_v5"] = _sl_n
                            if _sl_n:
                                _sl_by[_cid] = _sl_n
                            if _img_n:
                                _img_by[_cid] = _img_n
                for _c in _sl_by:
                    if _c in counts:
                        counts[_c]["has_sitelinks_v5"] = True
                for _c, _n in _img_by.items():
                    if _c in counts:
                        counts[_c]["ads_with_images_v5"] = _n
                _log(f"copy_verify: ad-level v5 (per-campaign) — sitelinks у {len(_sl_by)} камп, images у {len(_img_by)}")
        except Exception as _ale:  # noqa: BLE001
            _log(f"copy_verify: ad-level v5 error: {str(_ale)[:200]}")

    # D19b/D19c: реальные feedFilter товарных и каталожных объявлений. v5 fallback выше
    # даёт только количество типов; сами условия фильтра берём из Grid.
    product_filter_rows, product_filters_readable = _read_product_filter_signatures_grid(
        _grid, target_login, tgt_camp_ids, _log
    )
    audience_rows, audiences_readable = _read_audience_signatures_grid(
        _grid, tgt_camp_ids, _log
    )

    # ── Строим профиль per target campaign ──────────────────────────────────
    profile: Dict[str, dict] = {}

    for tgt_id in tgt_camp_ids:
        counts_c = counts.get(tgt_id) or {}
        edit_c = edit_rows.get(tgt_id) or {}
        inv_c = invariants.get(tgt_id) or {}
        product_c = product_filter_rows.get(tgt_id) or {}

        # D1: adgroup count
        adgroup_count = counts_c.get("adgroups") or 0

        # D2: keywords_count (от _enrich_group_targeting)
        kw_count = counts_c.get("keywords_count")   # None если не прочитано

        # D3: shared sets (из invariants.library_minus_ids)
        lib_minus = inv_c.get("library_minus_ids") or []
        shared_set_count = len([x for x in lib_minus if str(x).strip()])

        # D4: promo
        promo_ext_id = counts_c.get("promo_extension_id")
        has_promo = bool(
            promo_ext_id and str(promo_ext_id) not in ("", "0", "None")
        )

        # D7: callouts из campaigns_edit_rows. Grid отдаёт привязку под .assetValue (см.
        # grid_finalize.py:602), а НЕ .calloutIds — раньше читалось calloutIds → 0 → ложный
        # mismatch, хотя callouts привязаны (проверено: 50 callout-расширений associated на цели).
        callouts_data = edit_c.get("inheritableCallouts") or {}
        _co_raw = callouts_data.get("assetValue")
        if _co_raw is None:
            _co_raw = callouts_data.get("calloutIds")   # фолбэк на старое имя поля
        callout_ids_tgt = [str(x) for x in (_co_raw or []) if str(x).strip()]
        callout_count = len(callout_ids_tgt)

        # D8: sitelinks — campaign-level inheritable (Grid отдаёт под .assetValue, grid_finalize:603)
        # ИЛИ ad-level (v5) привязка.
        sl_data = edit_c.get("inheritableSitelinkSet") or {}
        sl_set_id = str(sl_data.get("assetValue") or sl_data.get("sitelinkSetId") or "")
        campaign_has_sitelinks = bool(sl_set_id and sl_set_id not in ("0", ""))
        ad_sitelinks_count = counts_c.get("sitelinks_ad_level_count_v5")
        has_sitelinks = campaign_has_sitelinks or bool(counts_c.get("has_sitelinks_v5"))

        # D9: images — campaign_content_counts.adaptive_images_read
        adaptive_total = counts_c.get("adaptive_total")       # None если не читалось
        no_images_ads = counts_c.get("no_images_ads")         # None если не читалось
        adaptive_images_read = bool(counts_c.get("adaptive_images_read"))

        if counts_c.get("ads_with_images_v5") is not None:
            ads_with_images = counts_c.get("ads_with_images_v5")   # честный ad-level v5-счёт
        elif adaptive_images_read and adaptive_total is not None and no_images_ads is not None:
            ads_with_images = max(0, adaptive_total - no_images_ads)
        else:
            ads_with_images = None

        # D5/D6: titles and bodies — approximated from adaptive_total (per-campaign count)
        ads_with_titles = adaptive_total  # adaptive = has titles by definition

        # D12: strategy name
        camp_v5 = target_campaign_v5.get(tgt_id) or {}
        strat_data = edit_c.get("strategyData") or {}
        strategy_name = (strat_data.get("strategyName") or
                         edit_c.get("strategyName") or
                         _strategy_name_from_campaign(camp_v5))
        site_monitoring = _setting_yes(camp_v5, "ENABLE_SITE_MONITORING")

        # D13/D14: video (hasVideo) и button/CTA (hasButton) — из adaptive_ads_for_update.
        adaptive_c = adaptive_by_campaign.get(tgt_id) or {}
        ads_with_video = adaptive_c.get("video") if adaptive_c else None
        ads_with_button = adaptive_c.get("button") if adaptive_c else None

        # D16: UTM tracking через bannerHrefParams из read_campaign_invariants (CampaignsEditData,
        # 0 доп. запросов). bannerHrefParams ≡ v5 TrackingParams. _strip_domain нормализует домены.
        # Tri-state: None если поле не пришло → diff выдаст UNREADABLE (fail-safe, не ложный OK).
        _bhp = inv_c.get("banner_href_params")   # None = поле не в ответе Grid
        tracking_norm = _strip_domain(_bhp or "") if _bhp is not None else None
        minus_places = sorted(str(x).strip() for x in (edit_c.get("disabledPlaces") or [])
                              if str(x).strip()) if edit_c else None

        profile[str(tgt_id)] = {
            # D1
            "adgroup_count": adgroup_count,
            # D2
            "kw_count": kw_count,
            "camp_neg_count": None,   # не читается отдельно
            "group_neg_count": None,
            # D3
            "shared_set_count": shared_set_count,
            # D4
            "has_promo": has_promo,
            "promo_id": str(promo_ext_id) if has_promo else None,
            # D5
            "ads_with_titles": ads_with_titles,
            # D6: прокси — adaptive_total (адаптивное объявление всегда имеет и заголовки, и тексты;
            # отдельного счётчика bodies в campaign_content_counts нет).
            "ads_with_texts": adaptive_total,
            # D7
            "callout_count": callout_count,
            # D8
            "has_sitelinks": has_sitelinks,
            "campaign_has_sitelinks": campaign_has_sitelinks,
            "ad_sitelinks_count": ad_sitelinks_count,
            # D9
            "ads_with_images": ads_with_images,
            # D10: audience/retargeting-привязки групп через Grid. None = не прочитано.
            "audiences": (audience_rows or {}).get(tgt_id, {}) if audiences_readable else None,
            # D11: bidmods — EXCLUDED (наш стандарт поверх источника).
            "bid_modifier_types": None,
            # D12
            "strategy_name": strategy_name,
            # D13
            "ads_with_video": ads_with_video,
            # D14
            "ads_with_button": ads_with_button,
            # D16
            "tracking_norm": tracking_norm,
            # D17
            "site_monitoring": site_monitoring,
            # D18
            "minus_places": minus_places,
            # D19: shopping filters — количество SMART_AD на цели (v5 fallback, тот же запрос ads,
            # тот же SMART_AD тип что и в shopping_ads.json источника). None если fallback не отработал.
            "shopping_count": counts_c.get("shopping_count_v5"),
            "listing_count": counts_c.get("listing_count_v5"),
            "shopping_filter_signatures_by_group": product_c.get("shopping") or {},
            "listing_filter_signatures_by_group": product_c.get("listing") or {},
            "product_filters_readable": bool(product_filters_readable),
            "shopping_op_types": None,
            # Мета
            "_adaptive_total": adaptive_total,
            "_reads_ok": {
                "counts": bool(counts_c),
                "edit_rows": bool(edit_c),
                "invariants": bool(inv_c),
                "adaptive_images": adaptive_images_read,
                "adaptive_target": adaptive_src is not None,
                "ad_level_sitelinks": "sitelinks_ad_level_count_v5" in counts_c,
                "campaign_v5": bool(camp_v5),
                "product_filters": bool(product_filters_readable),
                "audiences": bool(audiences_readable),
            },
        }

    return profile

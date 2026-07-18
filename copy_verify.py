"""copy_verify — движок сверки source↔target при копировании РК (REPORT-ONLY).

Строит нормализованные профили источника и цели, диффует по 19 измерениям,
возвращает структурный отчёт. Авто-ремонт НЕ реализован — каждая строка
несёт repairable:bool + repair_hint «где чинить».

Точка входа:
    run_copy_verification(src_dir, workdir, target_login, target_agency, *, grid=None,
                          source_grid=None, log=None) -> dict

    Возвращает:
        {
            "results": [{
                "scope":        "campaign:SRC_ID→TGT_ID",
                "dimension":    str,
                "status":       "ok"|"mismatch"|"missing"|"unreadable"|"excluded_intentional",
                "source":       Any,
                "target":       Any,
                "repairable":   bool,
                "repair_hint":  str,
            }],
            "summary": {"ok": int, "mismatch": int, "missing": int, "unreadable": int}
        }

Инвариант wiring-hub: НЕ импортирует blueprint.
campaign / grid_finalize / grid_read — прямой импорт (цикла нет).
Всё остальное (v5_call, token helpers) — инъектируется через configure(deps).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# ── Sibling-импорты (цикла нет — не тянут blueprint) ────────────────────────
from . import campaign as cmc
from . import grid_finalize as gf
from . import grid_read as gr

# ── DI из blueprint ──────────────────────────────────────────────────────────
# None до инъекции. configure() заполняет глобалами из blueprint.
_v5_call = None          # _v5_call(svc, method, token, login, params) → dict
_token_for_login = None  # (login, agency, tokens_dict) → (token, units_left)
_direct_tokens = None    # () → dict  (все токены агентства)


def configure(deps: dict) -> None:
    """Инъекция зависимостей из blueprint (v5_call, token_for_login, direct_tokens)."""
    globals().update(deps)


# ── Статусы результата ────────────────────────────────────────────────────────
_OK = "ok"
_MISMATCH = "mismatch"
_MISSING = "missing"
_UNREADABLE = "unreadable"
_EXCLUDED = "excluded_intentional"


# ── Хелперы I/O ─────────────────────────────────────────────────────────────

def _nolog(_m: str) -> None:
    return None


def _rj(path: Any) -> list:
    """Читать JSON-файл как список ([] при отсутствии/ошибке)."""
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
        return []
    except Exception:
        return []


def _rj_dict(path: Any) -> dict:
    """Читать JSON-файл как словарь ({} при отсутствии/ошибке)."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ── Нормализация UTM ─────────────────────────────────────────────────────────
# Домен в tracking-шаблоне намеренно меняется → убираем перед сравнением.
_DOMAIN_RE = re.compile(r"https?://[^/?#&\s]+", re.IGNORECASE)


def _strip_domain(template: Optional[str]) -> str:
    """Убрать домен из UTM-шаблона (domain намеренно меняется при копировании)."""
    if not template:
        return ""
    return _DOMAIN_RE.sub("{DOMAIN}", template.strip())


# ── ПРОФИЛЬ ИСТОЧНИКА ────────────────────────────────────────────────────────

def build_source_profile(src_dir: Any,
                          grid_snapshot: Optional[Dict[int, dict]] = None) -> Dict[str, dict]:
    """Нормализованный профиль источника по кампаниям из snapshot-файлов.

    Args:
        src_dir: Директория со snapshot-файлами (campaigns.json, adgroups.json, …).
            Пишется direct_copy.phase_pull + pull_source_campaign_assets.
        grid_snapshot: Опциональный pre-fetched dict {int(ad_id): {...}} из
            adaptive_ads_for_update источника (titles, bodies, imageHashes, hasVideo,
            hasButton). Если None — fallback на TextAd-поля из ads.json.

    Returns:
        {str(src_campaign_id): {dimension_key: value}}
    """
    src_dir = Path(src_dir)

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
    for sa in shopping_ads:
        cid = str(sa.get("CampaignId") or "")
        if cid:
            shopping_by_camp.setdefault(cid, []).append(sa)

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

        # D7: callouts
        callout_ids = campaign_callouts.get(cid) or []
        callout_count = len(callout_ids)

        # D8: sitelinks
        sitelink_set_id = campaign_sitelinks.get(cid)
        has_sitelinks = bool(sitelink_set_id)

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

        for ad in camp_ads_list:
            aid_str = str(ad.get("Id") or "")
            if not aid_str:
                continue
            try:
                aid_int = int(aid_str)
            except ValueError:
                aid_int = 0

            if grid_snapshot and aid_int in grid_snapshot:
                gs = grid_snapshot[aid_int]
                if gs.get("titles"):
                    ads_with_titles += 1
                if gs.get("bodies"):
                    ads_with_texts += 1
                if gs.get("imageHashes"):
                    ads_with_images += 1
                if gs.get("hasVideo"):
                    ads_with_video += 1
                if gs.get("hasButton"):
                    ads_with_button += 1
            else:
                # Fallback: TextAd / DynamicTextAd поля
                ta = ad.get("TextAd") or ad.get("DynamicTextAd") or {}
                if ta.get("Title"):
                    ads_with_titles += 1
                if ta.get("Text"):
                    ads_with_texts += 1
                if img_count_by_ad.get(aid_str, 0) > 0:
                    ads_with_images += 1
                # hasVideo / hasButton недоступны без Grid-источника

        # D19: shopping filters
        camp_shopping = shopping_by_camp.get(cid, [])
        shopping_count = len(camp_shopping)
        shopping_op_types: set = set()
        for sa in camp_shopping:
            conditions = (sa.get("ShoppingAd") or {}).get("FeedFilterConditions") or []
            for cond in (conditions if isinstance(conditions, list) else []):
                op = (cond.get("Operator") or cond.get("operator") or "")
                if op:
                    shopping_op_types.add(op)

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
            # D9
            "ads_with_images": ads_with_images,
            # D10: audiences — ADGROUPS_FIELDS не содержит таргет-аудиторий
            "audiences": None,
            # D11: bidmods — excluded (наш стандарт)
            "bid_modifier_types": sorted(bidmod_types_by_camp.get(cid, set())),
            # D12
            "strategy_name": strategy_name,
            # D13
            "ads_with_video": ads_with_video,
            # D14: button — только если grid_snapshot передан
            "ads_with_button": ads_with_button,
            # D15: adPrice — excluded (берётся из фида цели)
            # D16
            "tracking_norm": tracking_norm,
            # D17: site_monitoring — всегда True после finalize; Grid не читает
            # D18: minus_places — excluded (baseline)
            # D19
            "shopping_count": shopping_count,
            "shopping_op_types": sorted(shopping_op_types),
            # meta
            "_is_uac": is_uac,
            "_ads_count": len(camp_ads_list),
        }

    return profile


# ── ПРОФИЛЬ ЦЕЛИ ─────────────────────────────────────────────────────────────

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

    # ── adaptive_ads_for_update (цель) ───────────────────────────────────────
    # В v1 campaign_content_counts (adaptive_total / no_images_ads) достаточно для D5/D6/D9.
    # cached_adaptive принимается для совместимости API и будет использоваться в v2
    # (per-ad сравнение titles/bodies/imageHashes — требует ad→campaign маппинга через src_dir).
    # В v1 данные не агрегируются по кампании здесь; v2 TODO: передавать ad→camp dict отдельно.
    del cached_adaptive  # явно помечаем как неиспользуемый в v1; del убирает pyflakes warning

    # ── v5 fallback для черновиков (Draft/State=OFF) ─────────────────────────
    # Grid entity-запросы фильтруют по statRequirements (LAST_30DAYS): свежие черновики
    # (0 impressions) отдают структуру (группы), но 0 КОНТЕНТА (ключи/объявления=0).
    # Триггерим на 0 КОНТЕНТА (keywords_count пуст), а НЕ на 0 групп — Grid группы отдаёт,
    # поэтому старое условие adgroups==0 не срабатывало (no-op). v5.get без stat-фильтра
    # даёт реальные счётчики; ОБЯЗАТЕЛЬНА пагинация (548 групп / ~38k ключей > одной страницы).
    # Per-campaign v5-чтение теперь безопасно (не 4001) → добираем ВСЕ кампании: Grid stat-счётчики
    # ненадёжны для свежих черновиков не только при 0, но и при частичных значениях. Точные v5-счёта.
    _draft_cids = list(tgt_camp_ids)
    if _draft_cids and _v5_call is not None and _token_for_login is not None and _direct_tokens is not None:
        try:
            _tr = _token_for_login(target_login, target_agency or "", _direct_tokens())
            _tok = _tr[0] if isinstance(_tr, (tuple, list)) else _tr
            if _tok:
                _log(f"copy_verify: v5 fallback (0 контента) для {len(_draft_cids)} кампаний")

                def _v5_paged(_svc: str, _key: str, _fields: list) -> list:
                    # ПО ОДНОЙ кампании: batch CampaignIds на смешанном наборе (TextCampaign +
                    # UAC/товарка) даёт API 4001 → 0 (ложный tgt=0). Per-campaign безопасно.
                    _out: list = []
                    for _dcid in _draft_cids:
                        _off = 0
                        for _ in range(50):
                            _r = _v5_call(_svc, "get", _tok, target_login, {
                                "SelectionCriteria": {"CampaignIds": [_dcid]},
                                "FieldNames": _fields,
                                "Page": {"Limit": 10000, "Offset": _off},
                            })
                            _res = _r.get("result") or {}
                            _items = _res.get(_key) or []
                            _out += _items
                            _lb = _res.get("LimitedBy")
                            if _lb:
                                _off = int(_lb)
                            else:
                                break
                    return _out

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
                # ads → adaptive_total (приближение: все объявления считаем адаптивными)
                _ads_cnt: Dict[int, int] = {}
                for _ad in _v5_paged("ads", "Ads", ["Id", "CampaignId"]):
                    try:
                        _ads_cnt[int(_ad.get("CampaignId") or 0)] = _ads_cnt.get(int(_ad.get("CampaignId") or 0), 0) + 1
                    except (TypeError, ValueError):
                        pass
                for _cid2, _n in _ads_cnt.items():
                    if _cid2 in counts and counts[_cid2].get("adaptive_total") in (None, 0):
                        counts[_cid2]["adaptive_total"] = _n
                _log(f"copy_verify: v5 fallback завершён — групп/ключей/объявл добрано для {len(_draft_cids)} кампаний")
        except Exception as _v5e:
            _log(f"copy_verify: v5 fallback error: {str(_v5e)[:200]}")

    # ── Ad-level sitelinks/images через v5 (для ВСЕХ target-кампаний) ─────────
    # Sitelinks/картинки копируются на ОБЪЯВЛЕНИЯ; verify (edit_rows) читает inheritable на
    # уровне КАМПАНИИ → 0 при per-ad привязке → ложный mismatch. Добираем ad-level v5.
    if _v5_call is not None and _token_for_login is not None and _direct_tokens is not None:
        try:
            _tr2 = _token_for_login(target_login, target_agency or "", _direct_tokens())
            _tok2 = _tr2[0] if isinstance(_tr2, (tuple, list)) else _tr2
            if _tok2:
                _sl_by: Dict[int, int] = {}
                _img_by: Dict[int, int] = {}
                # ПО ОДНОЙ кампании: ads.get с TextAdFieldNames на СМЕШАННОМ наборе (TextCampaign +
                # UAC/товарка вместе) даёт error 4001 → 0 объявлений (ложный mismatch по sitelinks/
                # images). Per-campaign: UAC вернёт 0 TextAds без ошибки, TextCampaign — свои.
                # Доказано: batch(13)=0/4001, per-campaign=1152 объявл/218 sitelinks на той же цели.
                for _cid in list(tgt_camp_ids):
                    _off2 = 0
                    for _ in range(50):
                        _r2 = _v5_call("ads", "get", _tok2, target_login, {
                            "SelectionCriteria": {"CampaignIds": [_cid]},
                            "FieldNames": ["Id", "CampaignId"],
                            "TextAdFieldNames": ["SitelinkSetId", "AdImageHash"],
                            "Page": {"Limit": 10000, "Offset": _off2}})
                        _res2 = _r2.get("result") or {}
                        for _ad in (_res2.get("Ads") or []):
                            _ta = _ad.get("TextAd") or {}
                            if _ta.get("SitelinkSetId"):
                                _sl_by[_cid] = _sl_by.get(_cid, 0) + 1
                            if _ta.get("AdImageHash"):
                                _img_by[_cid] = _img_by.get(_cid, 0) + 1
                        _lb2 = _res2.get("LimitedBy")
                        if _lb2:
                            _off2 = int(_lb2)
                        else:
                            break
                for _c in _sl_by:
                    if _c in counts:
                        counts[_c]["has_sitelinks_v5"] = True
                for _c, _n in _img_by.items():
                    if _c in counts:
                        counts[_c]["ads_with_images_v5"] = _n
                _log(f"copy_verify: ad-level v5 (per-campaign) — sitelinks у {len(_sl_by)} камп, images у {len(_img_by)}")
        except Exception as _ale:  # noqa: BLE001
            _log(f"copy_verify: ad-level v5 error: {str(_ale)[:200]}")

    # ── Строим профиль per target campaign ──────────────────────────────────
    profile: Dict[str, dict] = {}

    for tgt_id in tgt_camp_ids:
        counts_c = counts.get(tgt_id) or {}
        edit_c = edit_rows.get(tgt_id) or {}
        inv_c = invariants.get(tgt_id) or {}

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
        has_sitelinks = bool(sl_set_id and sl_set_id not in ("0", "")) or bool(counts_c.get("has_sitelinks_v5"))

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
        strat_data = edit_c.get("strategyData") or {}
        strategy_name = (strat_data.get("strategyName") or
                         edit_c.get("strategyName") or "")

        # D13/D14: video and button — не читаются в campaign_content_counts;
        # требуют adaptive_ads_for_update с aggregation по кампании (отдельный вызов).
        # В v1 помечаем unreadable — в cached_adaptive нет campaignId.
        ads_with_video = None
        ads_with_button = None

        # D16: tracking — не читается напрямую из campaigns_edit_rows в текущем формате
        tracking_norm = None

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
            # D6 — покрыто adaptive_total
            # D7
            "callout_count": callout_count,
            # D8
            "has_sitelinks": has_sitelinks,
            # D9
            "ads_with_images": ads_with_images,
            # D10: audiences — unreadable
            "audiences": None,
            # D11: bidmods — excluded
            "bid_modifier_types": None,
            # D12
            "strategy_name": strategy_name,
            # D13
            "ads_with_video": ads_with_video,
            # D14
            "ads_with_button": ads_with_button,
            # D16
            "tracking_norm": tracking_norm,
            # D19: shopping filters — _enrich_shopping_bodies проверяет только тексты
            "shopping_count": None,
            "shopping_op_types": None,
            # Мета
            "_adaptive_total": adaptive_total,
            "_reads_ok": {
                "counts": bool(counts_c),
                "edit_rows": bool(edit_c),
                "invariants": bool(inv_c),
                "adaptive_images": adaptive_images_read,
            },
        }

    return profile


# ── DIFF ─────────────────────────────────────────────────────────────────────

def diff_profiles(src_profile: Dict[str, dict],
                  tgt_profile: Dict[str, dict],
                  id_maps: dict) -> List[dict]:
    """Структурный diff профилей по 19 измерениям.

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
                                repair_hint="step_keywords copy_steps.py:807 — Grid-first, 0 баллов"))
        else:
            results.append(_row(scope, "keyword_count",
                                _OK if s2 == t2 else _MISMATCH, s2, t2,
                                repairable=True,
                                repair_hint="step_keywords copy_steps.py:807"))

        # D2b: campaign negatives (report; ремонта нет — только settings_diff)
        s2b = src_c["camp_neg_count"]
        if s2b > 0:
            results.append(_row(scope, "campaign_neg_count", _UNREADABLE, s2b, None,
                                repairable=False,
                                repair_hint="step_settings_diff report-only; "
                                            "отдельного шага ремонта кампанийных минус-слов нет "
                                            "(copy_steps.py:1244 _DIFF_SKIP_KEYS — не ремонтируется)"))

        # D3: shared set count (ремап через maps["shared_sets"] → сравниваем COUNT)
        s3, t3 = src_c["shared_set_count"], tgt_c["shared_set_count"]
        if not reads_ok.get("invariants"):
            results.append(_row(scope, "shared_set_count", _UNREADABLE, s3, None,
                                repairable=True,
                                repair_hint="read_campaign_invariants grid_finalize.py:933 не прочитал; "
                                            "привязка: UpdateCampaigns libraryMinusKeywordsIds "
                                            "(grid_finalize.py:277)"))
        else:
            results.append(_row(scope, "shared_set_count",
                                _OK if s3 == t3 else _MISMATCH, s3, t3,
                                repairable=True,
                                repair_hint="UAC tp6/7: writer НЕТ (MasterCampaignSpec нет shared_sets); "
                                            "v5-путь: UpdateCampaigns libraryMinusKeywordsIds "
                                            "(grid_finalize.py:277)"))

        # D4: promo
        s4, t4 = src_c["has_promo"], tgt_c["has_promo"]
        results.append(_row(scope, "promo_attached",
                            _OK if s4 == t4 else _MISMATCH, src_c["promo_id"], tgt_c["promo_id"],
                            repairable=True,
                            repair_hint="step_attach_promos copy_steps.py:487; "
                                        "PromoClient.add copy_engine.py:2025"))

        # D5: adaptive titles (ads_with_titles vs adaptive_total)
        s5 = src_c["ads_with_titles"]
        t5 = tgt_c["ads_with_titles"]   # = adaptive_total от campaign_content_counts
        if t5 is None:
            results.append(_row(scope, "adaptive_titles_count", _UNREADABLE, s5, None,
                                repairable=True,
                                repair_hint="step_adaptive_creatives copy_steps.py:982 — titles/bodies RMW; "
                                            "adaptive_ads_for_update grid_finalize.py:2186"))
        else:
            results.append(_row(scope, "adaptive_titles_count",
                                _OK if s5 == t5 else _MISMATCH, s5, t5,
                                repairable=True,
                                repair_hint="step_adaptive_creatives copy_steps.py:982 — "
                                            "UpdateAdaptiveTextAds RMW titles/bodies"))

        # D6: texts (covered by same adaptive_total block as D5; отдельная строка для явности)
        s6 = src_c["ads_with_texts"]
        results.append(_row(scope, "adaptive_bodies_count",
                            _OK if s6 == (t5 or 0) else _MISMATCH, s6, t5,
                            repairable=True,
                            repair_hint="step_adaptive_creatives copy_steps.py:982 — bodies RMW"))

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
                                            "grid.add_callouts copy_engine.py:1987"))

        # D8: sitelinks (аналогично D7 — campaign-level, зависит от edit_rows)
        s8, t8 = src_c["has_sitelinks"], tgt_c["has_sitelinks"]
        if not reads_ok.get("edit_rows"):
            results.append(_row(scope, "sitelinks_present", _UNREADABLE, s8, None,
                                repairable=True,
                                repair_hint="campaigns_edit_rows не вернул кампанию (возможно черновик); "
                                            "step_attach_sitelinks copy_steps.py:377"))
        else:
            results.append(_row(scope, "sitelinks_present",
                                _OK if s8 == t8 else _MISMATCH, s8, t8,
                                repairable=True,
                                repair_hint="step_attach_sitelinks copy_steps.py:377 (с гео-морфом)"))

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

        # D10: audiences — unreadable с обеих сторон
        results.append(_row(scope, "audiences", _UNREADABLE, None, None,
                            repairable=False,
                            repair_hint="v5-путь: ADGROUPS_FIELDS не содержит таргет-аудиторий "
                                        "(direct_copy.py:132); "
                                        "UAC: interest_ids/audience_id 403 на всех агентствах Victory; "
                                        "_enrich_group_targeting читает только relevance_match "
                                        "(grid_read.py:206)"))

        # D11: bid_modifiers — excluded intentional
        results.append(_row(scope, "bid_modifiers", _EXCLUDED,
                            src_c.get("bid_modifier_types"), "our_standard",
                            repairable=False,
                            repair_hint="В _DIFF_SKIP_KEYS (copy_steps.py:1232); "
                                        "step_age_bidmods ставит −100% <18/18-24 (copy_steps.py:186)"))

        # D12: strategy name
        s12, t12 = src_c["strategy_name"], tgt_c["strategy_name"]
        if not reads_ok.get("edit_rows"):
            results.append(_row(scope, "strategy_name", _UNREADABLE, s12, None,
                                repairable=True,
                                repair_hint="campaigns_edit_rows не прочитан (grid_finalize.py:897)"))
        elif not t12:
            results.append(_row(scope, "strategy_name", _UNREADABLE, s12, None,
                                repairable=True,
                                repair_hint="strategyName не в campaigns_edit_rows ответе; "
                                            "PFCMG-восстановление copy_engine.py:2118"))
        else:
            results.append(_row(scope, "strategy_name",
                                _OK if s12 == t12 else _MISMATCH, s12, t12,
                                repairable=True,
                                repair_hint="PFCMG-восстановление copy_engine.py:2118; "
                                            "strategy_fallback при создании direct_copy.py:437"))

        # D13: video
        s13 = src_c["ads_with_video"]
        t13 = tgt_c["ads_with_video"]
        # t13 всегда None в v1 (нет per-campaign агрегата video в campaign_content_counts)
        if t13 is None:
            results.append(_row(scope, "ads_with_video", _UNREADABLE, s13, None,
                                repairable=True,
                                repair_hint="step_videos copy_steps.py:1110 — скачать→аплоуд→RMW; "
                                            "hasVideo из adaptive_ads_for_update (grid_finalize.py:2219); "
                                            "нужен отдельный per-campaign агрегат для сверки"))
        else:
            results.append(_row(scope, "ads_with_video",
                                _OK if s13 == t13 else _MISMATCH, s13, t13,
                                repairable=True,
                                repair_hint="step_videos copy_steps.py:1110"))

        # D14: button/CTA — мутатора НЕТ
        s14 = src_c["ads_with_button"]
        results.append(_row(scope, "button_cta", _UNREADABLE, s14, None,
                            repairable=False,
                            repair_hint="Мутатора кнопки НЕТ — step_adaptive_creatives (copy_steps.py:1021-1023) "
                                        "намеренно не переносит button; UpdateAdaptiveTextAds не пишет button "
                                        "(grid_finalize.py:2137); UAC — нет поля в MasterCampaignSpec"))

        # D15: adPrice — excluded intentional
        results.append(_row(scope, "ad_price", _EXCLUDED, None, None,
                            repairable=False,
                            repair_hint="Берётся из фида ЦЕЛИ, не источника "
                                        "(step_prices copy_steps.py:671; copy-fix-spec.md §УТОЧНЕНИЯ)"))

        # D16: UTM tracking (без домена)
        s16 = src_c["tracking_norm"]
        t16 = tgt_c["tracking_norm"]   # None в v1
        if t16 is None:
            results.append(_row(scope, "utm_tracking", _UNREADABLE, s16, None,
                                repairable=False,
                                repair_hint="TODO: читать из campaigns_edit_rows.trackingParams "
                                            "или v5 campaigns.get; domain-замена в direct_copy.py:916"))
        else:
            results.append(_row(scope, "utm_tracking",
                                _OK if s16 == t16 else _MISMATCH, s16, t16,
                                repairable=True,
                                repair_hint="Tracking с заменой домена (direct_copy.py:916 do_replace)"))

        # D17: site_monitoring — Grid не читает hasSiteMonitoring
        results.append(_row(scope, "site_monitoring", _UNREADABLE, True, None,
                            repairable=False,
                            repair_hint="hasSiteMonitoring НЕ в read-схеме Grid "
                                        "(grid_finalize.py:942, комментарий read_campaign_invariants); "
                                        "set_campaign_invariants форсит True при любом ремонте"))

        # D18: minus_places — excluded intentional
        results.append(_row(scope, "minus_places", _EXCLUDED, None, None,
                            repairable=False,
                            repair_hint="В _DIFF_SKIP_KEYS (copy_steps.py:1232); "
                                        "step_disabled_places ставит baseline "
                                        "(copy_engine.py:1929 _enabled_baseline_minus_places)"))

        # D19: shopping filters
        s19 = src_c["shopping_count"]
        t19 = tgt_c["shopping_count"]   # None: нет чтения фильтров в Grid
        if s19 > 0:
            if t19 is None:
                results.append(_row(scope, "shopping_filter_count", _UNREADABLE, s19, None,
                                    repairable=True,
                                    repair_hint="_enrich_shopping_bodies проверяет только пустые тексты, "
                                                "не фильтры (grid_read.py:456); "
                                                "writer: grid.add_shopping_ads grid_finalize.py:1503; "
                                                "copy_engine.py:2084 усекает до 3 операндов — "
                                                "нужен полный перенос conditions"))
            else:
                results.append(_row(scope, "shopping_filter_count",
                                    _OK if s19 == t19 else _MISMATCH, s19, t19,
                                    repairable=True,
                                    repair_hint="copy_engine.py:2084 усекает до 3 операндов; "
                                                "grid.add_shopping_ads grid_finalize.py:1503"))

    return results


# ── ТОЧКА ВХОДА ──────────────────────────────────────────────────────────────

def check_geo_kw_consistency(src_dir: Any,
                             geo_pairs: List[Tuple[str, str]],
                             *,
                             snapshot_transformed: bool = True,
                             log: Optional[Callable[[str], None]] = None,
                             ) -> List[dict]:
    """Задача 1: измерение гео-консистентности ключей/минус-слов (REPORT-ONLY, snapshot-based).

    Проверяет snapshot-файлы ИСТОЧНИКА на два признака:
    1. geo_kw_source_residual — количество фраз в keywords.json, содержащих формы
       ИСТОЧНИКА (они должны были быть заменены step_keywords гео-морфологией).
    2. geo_neg_target_blocked — количество минус-слов в adgroups.json/campaigns.json,
       содержащих формы ЦЕЛЕВОГО города (они должны были быть отфильтрованы).

    Args:
        snapshot_transformed: True (ЕПК-путь) — snapshot содержит УЖЕ ЗАМЕЩЁННЫЕ ключи;
            residual==0 означает корректную замену.
            False (v5-путь) — snapshot содержит СЫРЫЕ ключи источника (замена применена
            step_keywords к заливаемым данным, не к snapshot); остаточный подсчёт дал бы
            ЛОЖНЫЙ MISMATCH → оба измерения возвращаются _EXCLUDED с пояснением.

    Возвращает две строки [{scope="global", dimension=..., status, source, target, ...}].
    Пустой geo_pairs → оба измерения excluded_intentional (нет гео-замены — нет требования)."""
    _log = log or _nolog
    src_dir = Path(src_dir)
    rows: List[dict] = []

    def _row(dimension: str, status: str, source: Any, target: Any,
             repair_hint: str = "") -> dict:
        return {
            "scope": "global",
            "dimension": dimension,
            "status": status,
            "source": source,
            "target": target,
            "repairable": False,
            "repair_hint": repair_hint,
        }

    # A3: v5-путь → snapshot сырой, residual = ложный MISMATCH → исключаем (не считаем).
    if not snapshot_transformed:
        rows.append(_row("geo_kw_source_residual", _EXCLUDED, 0, None,
                         "v5-путь: snapshot источника сырой — гео-замена применена "
                         "step_keywords при заливке, а не к snapshot; "
                         "residual по snapshot будет ложно-положительным — измерение исключено"))
        rows.append(_row("geo_neg_target_blocked", _EXCLUDED, 0, None,
                         "v5-путь: snapshot источника сырой — фильтрация минусов по целевому гео "
                         "применена step_keywords, не к snapshot — измерение исключено"))
        return rows

    if not geo_pairs:
        rows.append(_row("geo_kw_source_residual", _EXCLUDED,
                         0, None,
                         "geo_pairs пуст (geo_mode=keep или нет гео-замены) — проверка не нужна"))
        rows.append(_row("geo_neg_target_blocked", _EXCLUDED,
                         0, None,
                         "geo_pairs пуст — фильтрация целевого гео в минусах не применялась"))
        return rows

    # Формы источника (левые части пар) и целевого гео (правые части).
    source_forms = sorted(
        {old.lower() for old, _ in geo_pairs if (old or "").strip()},
        key=len, reverse=True,
    )
    target_forms = sorted(
        {new.lower() for _, new in geo_pairs if (new or "").strip()},
        key=len, reverse=True,
    )

    # 1) geo_kw_source_residual: сколько фраз в keywords.json ещё содержат формы ИСТОЧНИКА.
    # A4: различаем «файл отсутствует» (норма) от «файл есть, но чтение упало» (EXCLUDED+ошибка).
    kw_with_src = 0
    _kw_read_err: Optional[str] = None
    try:
        kw_path = src_dir / "keywords.json"
        if kw_path.exists():
            _raw_kw = json.loads(kw_path.read_text(encoding="utf-8"))
            keywords: list = _raw_kw if isinstance(_raw_kw, list) else \
                ([_raw_kw] if isinstance(_raw_kw, dict) else [])
            for kw in keywords:
                phrase = (kw.get("Keyword") or "").lower()
                if not phrase:
                    continue
                if any(re.search(r"\b" + re.escape(sf) + r"\b", phrase, re.UNICODE)
                       for sf in source_forms):
                    kw_with_src += 1
        # else: файла нет → нечего проверять (не ошибка)
    except Exception as e:  # noqa: BLE001
        _kw_read_err = str(e)[:200]
        _log(f"geo_kw_consistency: keywords.json read error: {_kw_read_err}")

    if _kw_read_err:
        rows.append(_row(
            "geo_kw_source_residual", _EXCLUDED, 0, None,
            f"A4: ошибка чтения keywords.json (файл существует, но прочитать не удалось): {_kw_read_err}",
        ))
    else:
        rows.append(_row(
            "geo_kw_source_residual",
            _OK if kw_with_src == 0 else _MISMATCH,
            kw_with_src,
            0,
            repair_hint=(
                "v5-путь: step_keywords (copy_steps.py) применяет гео-замену через ctx.geo_pairs; "
                "ЕПК-путь: замена применена инлайн в group_specs перед create_full (copy_engine.py:1237); "
                "в snapshot пишутся уже замещённые ключи → 0 = замена корректна"
            ),
        ))

    # 2) geo_neg_target_blocked: сколько минус-слов содержат формы ЦЕЛЕВОГО города.
    # Проверяем campaigns.json (campaign-level NegativeKeywords) и adgroups.json (group-level).
    # A4: отдельные read_err для каждого файла; результат — EXCLUDED если оба прочитать не удалось.
    neg_with_tgt = 0
    _camp_read_err: Optional[str] = None
    _ag_read_err: Optional[str] = None

    try:
        camp_path = src_dir / "campaigns.json"
        if camp_path.exists():
            _raw_camps = json.loads(camp_path.read_text(encoding="utf-8"))
            camps_list: list = _raw_camps if isinstance(_raw_camps, list) else \
                ([_raw_camps] if isinstance(_raw_camps, dict) else [])
            for camp in camps_list:
                raw = camp.get("NegativeKeywords") or {}
                items = raw.get("Items") if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
                for m in (items or []):
                    low = (m or "").lower()
                    if any(re.search(r"\b" + re.escape(tf) + r"\b", low, re.UNICODE)
                           for tf in target_forms):
                        neg_with_tgt += 1
    except Exception as e:  # noqa: BLE001
        _camp_read_err = str(e)[:150]
        _log(f"geo_kw_consistency: campaigns.json read error: {_camp_read_err}")

    try:
        ag_path = src_dir / "adgroups.json"
        if ag_path.exists():
            _raw_ags = json.loads(ag_path.read_text(encoding="utf-8"))
            ags_list: list = _raw_ags if isinstance(_raw_ags, list) else \
                ([_raw_ags] if isinstance(_raw_ags, dict) else [])
            for ag in ags_list:
                raw = ag.get("NegativeKeywords") or {}
                items = raw.get("Items") if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
                for m in (items or []):
                    low = (m or "").lower()
                    if any(re.search(r"\b" + re.escape(tf) + r"\b", low, re.UNICODE)
                           for tf in target_forms):
                        neg_with_tgt += 1
    except Exception as e:  # noqa: BLE001
        _ag_read_err = str(e)[:150]
        _log(f"geo_kw_consistency: adgroups.json read error: {_ag_read_err}")

    _neg_read_err_msg = "; ".join(
        f for f in [
            (f"campaigns.json: {_camp_read_err}" if _camp_read_err else ""),
            (f"adgroups.json: {_ag_read_err}" if _ag_read_err else ""),
        ] if f
    )
    if _neg_read_err_msg and neg_with_tgt == 0:
        # Оба файла не прочитаны и счётчик 0 → не можем отличить «OK» от «ошибка чтения»
        rows.append(_row(
            "geo_neg_target_blocked", _EXCLUDED, 0, None,
            f"A4: ошибка чтения файлов минус-слов, результат ненадёжен: {_neg_read_err_msg}",
        ))
    else:
        if _neg_read_err_msg:
            _log(f"geo_kw_consistency: частичная ошибка чтения минусов, счётчик={neg_with_tgt}: {_neg_read_err_msg}")
        rows.append(_row(
            "geo_neg_target_blocked",
            _OK if neg_with_tgt == 0 else _MISMATCH,
            neg_with_tgt,
            0,
            repair_hint=(
                "ЕПК-путь: _copy_geo_filter_negatives удаляет минусы с формами ЦЕЛЕВОГО гео "
                "(copy_engine.py:1240-1243); snapshot пишется с отфильтрованными минусами → 0 = фильтрация корректна; "
                "v5-путь: campaign/group негативы заливаются direct_copy.py до наших шагов — "
                "ненулевое значение = диагностически значимо"
            ),
        ))

    _log(f"geo_kw_consistency: source_residual_kw={kw_with_src}, neg_with_target_geo={neg_with_tgt}")
    return rows


def run_copy_verification(src_dir: Any,
                          workdir: Any,
                          target_login: str,
                          target_agency: str,
                          *,
                          geo_pairs: Optional[list] = None,
                          grid: Optional[gf.GridClient] = None,
                          source_grid: Optional[gf.GridClient] = None,
                          cached_counts: Optional[Dict[int, dict]] = None,
                          cached_edit_rows: Optional[Dict[int, dict]] = None,
                          cached_invariants: Optional[Dict[int, dict]] = None,
                          cached_adaptive_src: Optional[Dict[int, dict]] = None,
                          cached_adaptive_tgt: Optional[Dict[int, dict]] = None,
                          log: Optional[Callable[[str], None]] = None,
                          ) -> dict:
    """Верхняя точка входа: сверка source↔target после копирования РК.

    Вызывается из _copy_cookie_postprocess после завершения всех шагов.
    REPORT-ONLY: не изменяет кампании.

    Args:
        src_dir: Директория снапшота источника (phase_pull).
        workdir: Рабочая директория джобы (содержит id_maps.json).
        target_login: Логин целевого аккаунта.
        target_agency: Агентство цели (для context, не используется напрямую).
        grid: Pre-built gf.GridClient цели (из _copy_cookie_postprocess).
        source_grid: Pre-built gf.GridClient источника (для grid_snapshot adaptive).
        cached_counts / cached_edit_rows / cached_invariants: Pre-fetched Grid-читатели
            (если уже были вызваны в постпроцессе — передать сюда для DRY).
        cached_adaptive_src: Pre-fetched adaptive_ads_for_update ИСТОЧНИКА
            {int(ad_id): {...}} (titles/bodies/imageHashes/hasVideo/hasButton).
        cached_adaptive_tgt: Pre-fetched adaptive_ads_for_update ЦЕЛИ.
        log: Функция логирования (копирует в job-лог).

    Returns:
        {
            "results":  [{scope, dimension, status, source, target, repairable, repair_hint}],
            "summary":  {"ok": int, "mismatch": int, "missing": int, "unreadable": int},
        }
    """
    _log = log or _nolog
    src_dir = Path(src_dir)
    workdir = Path(workdir)

    _empty: Dict[str, Any] = {
        "results": [],
        "summary": {"ok": 0, "mismatch": 0, "missing": 0, "unreadable": 0},
    }

    # id_maps.json
    maps_path = workdir / "id_maps.json"
    id_maps: dict = {}
    try:
        if maps_path.exists():
            id_maps = json.loads(maps_path.read_text(encoding="utf-8"))
    except Exception as e:
        _log(f"copy_verify: id_maps read error: {str(e)[:200]}")
        out = dict(_empty)
        out["error"] = f"id_maps read: {str(e)[:200]}"
        return out

    # Если source_grid передан — получаем adaptive source data (titles/bodies/video/button)
    grid_snapshot: Optional[Dict[int, dict]] = cached_adaptive_src
    if grid_snapshot is None and source_grid is not None:
        # Собираем src_ad_ids из snapshot ads.json для запроса
        src_ads = _rj(src_dir / "ads.json")
        src_ad_ids: List[int] = []
        src_camp_ids_for_adaptive: List[int] = []
        for ad in src_ads:
            try:
                aid = int(ad.get("Id") or 0)
                cid = int(ad.get("CampaignId") or 0)
            except (TypeError, ValueError):
                continue
            if aid > 0 and aid not in src_ad_ids:
                src_ad_ids.append(aid)
            if cid > 0 and cid not in src_camp_ids_for_adaptive:
                src_camp_ids_for_adaptive.append(cid)
        if src_ad_ids:
            try:
                grid_snapshot = source_grid.adaptive_ads_for_update(
                    src_camp_ids_for_adaptive, src_ad_ids)
            except Exception as e:
                _log(f"copy_verify: source adaptive_ads_for_update error: {str(e)[:200]}")
                grid_snapshot = None

    _log("copy_verify: строим профиль источника…")
    try:
        src_profile = build_source_profile(src_dir, grid_snapshot=grid_snapshot)
    except Exception as e:
        _log(f"copy_verify: build_source_profile error: {str(e)[:200]}")
        out = dict(_empty)
        out["error"] = f"build_source_profile: {str(e)[:200]}"
        return out

    _log(f"copy_verify: source profile — {len(src_profile)} кампаний")

    _log("copy_verify: строим профиль цели…")
    try:
        tgt_profile = build_target_profile(
            target_login=target_login,
            id_maps=id_maps,
            grid=grid,
            cached_counts=cached_counts,
            cached_edit_rows=cached_edit_rows,
            cached_invariants=cached_invariants,
            cached_adaptive=cached_adaptive_tgt,
            log=_log,
            target_agency=target_agency,
        )
    except Exception as e:
        _log(f"copy_verify: build_target_profile error: {str(e)[:200]}")
        out = dict(_empty)
        out["error"] = f"build_target_profile: {str(e)[:200]}"
        return out

    _log(f"copy_verify: target profile — {len(tgt_profile)} кампаний")

    _log("copy_verify: diff profiles…")
    try:
        results = diff_profiles(src_profile, tgt_profile, id_maps)
    except Exception as e:
        _log(f"copy_verify: diff_profiles error: {str(e)[:200]}")
        out = dict(_empty)
        out["error"] = f"diff_profiles: {str(e)[:200]}"
        return out

    # Задача 1: гео-консистентность ключей/минусов (REPORT-ONLY, snapshot-based).
    try:
        # A3: v5-путь — snapshot сырой → snapshot_transformed=False → оба измерения EXCLUDED.
        geo_rows = check_geo_kw_consistency(
            src_dir, geo_pairs or [], snapshot_transformed=False, log=_log
        )
        results.extend(geo_rows)
    except Exception as e:
        _log(f"copy_verify: geo_kw_consistency error: {str(e)[:200]}")

    summary: Dict[str, int] = {"ok": 0, "mismatch": 0, "missing": 0, "unreadable": 0}
    for r in results:
        st = r.get("status", "")
        if st in summary:
            summary[st] += 1
        # excluded_intentional не считаем ошибкой — не в summary

    _log(f"copy_verify: итог — ok={summary['ok']}, mismatch={summary['mismatch']}, "
         f"missing={summary['missing']}, unreadable={summary['unreadable']}, "
         f"total={len(results)}")

    return {"results": results, "summary": summary}


# ── АВТО-РЕМОНТ (B1) ─────────────────────────────────────────────────────────

def _repair_shared_sets(
    results: List[dict],
    *,
    src_dir: Path,
    workdir: Path,
    target_login: str,
    target_agency: str,
    repairs: list,
    errors: list,
    log: Callable[[str], None],
) -> None:
    """D3: создать/найти shared минус-наборы в целевом аккаунте и привязать к кампаниям.

    Use-Operator-Units: v5 с токеном агентства (не Grid-куки).
    Идемпотентно: maps["shared_sets"] уже содержит mapping → только привязка.
    """
    if _v5_call is None or _token_for_login is None or _direct_tokens is None:
        errors.append("repair_shared_sets: DI не инициализирован (configure() не вызван)")
        return

    # Читаем кампании источника
    src_camps_by_id: Dict[str, dict] = {}
    try:
        camp_path = src_dir / "campaigns.json"
        if camp_path.exists():
            for c in json.loads(camp_path.read_text(encoding="utf-8")):
                cid = str(c.get("Id") or "")
                if cid:
                    src_camps_by_id[cid] = c
    except Exception as exc:  # noqa: BLE001
        errors.append(f"repair_shared_sets: campaigns.json: {str(exc)[:150]}")
        return

    # Snapshot shared sets (Name + NegativeKeywords)
    src_sets_by_id: Dict[str, dict] = {}
    try:
        sset_path = src_dir / "negative_keyword_shared_sets.json"
        if sset_path.exists():
            for s in json.loads(sset_path.read_text(encoding="utf-8")):
                sid = str(s.get("Id") or "")
                if sid:
                    src_sets_by_id[sid] = s
    except Exception:  # noqa: BLE001
        pass  # snapshot может не содержать sets — деградируем к пустым словам

    # id_maps.json
    maps_path = workdir / "id_maps.json"
    maps: dict = {}
    try:
        if maps_path.exists():
            maps = json.loads(maps_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"repair_shared_sets: id_maps.json: {str(exc)[:150]}")
        return
    maps.setdefault("shared_sets", {})
    maps.setdefault("campaigns", {})

    # Токен для целевого аккаунта
    try:
        token, _ = _token_for_login(target_login, target_agency, _direct_tokens())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"repair_shared_sets: token: {str(exc)[:150]}")
        return
    if not token:
        errors.append("repair_shared_sets: токен пуст, пропуск")
        return

    # Существующие shared sets в целевом (кэш по имени для dedup)
    existing_by_name: Dict[str, int] = {}
    try:
        jg = _v5_call("negativekeywordsharedsets", "get", token, target_login, {
            "SelectionCriteria": {}, "FieldNames": ["Id", "Name"],
        })
        for s in (jg.get("result") or {}).get("NegativeKeywordSharedSets", []):
            nm = s.get("Name") or ""
            sid_raw = s.get("Id")
            if nm and sid_raw:
                existing_by_name[nm] = int(sid_raw)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"repair_shared_sets: v5 get: {str(exc)[:150]}")
        return

    maps_dirty = False
    for row in results:
        if row.get("dimension") != "shared_set_count":
            continue
        if not row.get("repairable"):
            continue
        if row.get("status") in (_OK, _EXCLUDED):
            continue
        scope = row.get("scope", "")
        if not scope.startswith("campaign:"):
            continue
        tail = scope[len("campaign:"):]
        if "→" not in tail:
            continue
        src_id, tgt_id_str = tail.split("→", 1)

        src_camp = src_camps_by_id.get(src_id) or {}
        raw_ids = src_camp.get("NegativeKeywordSharedSetIds") or {}
        if isinstance(raw_ids, dict):
            raw_ids = raw_ids.get("Items") or []
        if not raw_ids:
            continue  # источник без shared sets — ничего не привязываем

        tgt_set_ids: List[int] = []
        for sid_str in [str(x) for x in raw_ids if str(x).strip()]:
            # Уже смапирован в предыдущих проходах/phase_upload
            if sid_str in maps["shared_sets"]:
                tgt_set_ids.append(int(maps["shared_sets"][sid_str]))
                continue
            # Ищем / создаём в целевом
            src_s = src_sets_by_id.get(sid_str) or {}
            set_name = src_s.get("Name") or f"copy_set_{sid_str}"
            if set_name in existing_by_name:
                tgt_sid = existing_by_name[set_name]
            else:
                words = src_s.get("NegativeKeywords") or []
                if isinstance(words, dict):
                    words = words.get("Items") or []
                try:
                    j_add = _v5_call("negativekeywordsharedsets", "add", token, target_login, {
                        "NegativeKeywordSharedSets": [{
                            "Name": set_name,
                            "NegativeKeywords": words,
                        }],
                    })
                    add_results = (j_add.get("result") or {}).get("AddResults", [])
                    new_id_raw = add_results[0].get("Id") if add_results else None
                    if not new_id_raw:
                        errors.append(
                            f"repair_shared_sets: add «{set_name}» нет Id: "
                            f"{str(j_add)[:120]}"
                        )
                        continue
                    tgt_sid = int(new_id_raw)
                    existing_by_name[set_name] = tgt_sid
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"repair_shared_sets: add «{set_name}»: {str(exc)[:150]}")
                    continue
            maps["shared_sets"][sid_str] = tgt_sid
            maps_dirty = True
            tgt_set_ids.append(tgt_sid)

        if not tgt_set_ids:
            continue

        try:
            tgt_id = int(tgt_id_str)
        except (TypeError, ValueError):
            errors.append(f"repair_shared_sets: bad tgt_id «{tgt_id_str}»")
            continue

        # Привязываем наборы к целевой кампании
        try:
            j_upd = _v5_call("campaigns", "update", token, target_login, {
                "Campaigns": [{
                    "Id": tgt_id,
                    "NegativeKeywordSharedSetIds": {"Items": tgt_set_ids},
                }],
            })
            upd_res = (j_upd.get("result") or {}).get("UpdateResults", [])
            api_errs = (upd_res[0].get("Errors") or []) if upd_res else []
            if api_errs:
                msg = "; ".join(
                    e.get("Message") or e.get("Details") or str(e) for e in api_errs
                )
                errors.append(f"repair_shared_sets: attach camp {tgt_id}: {msg}")
                continue
            if "error" in j_upd:
                errors.append(
                    f"repair_shared_sets: attach camp {tgt_id}: {j_upd.get('error')}"
                )
                continue
            # Успех — обновляем строку отчёта in-place
            row["status"] = _OK
            row["target"] = len(tgt_set_ids)
            repairs.append({
                "scope": scope,
                "dimension": "shared_set_count",
                "action": "created_and_attached",
                "target_set_ids": tgt_set_ids,
            })
            log(f"repair_shared_sets: {scope} → привязано {len(tgt_set_ids)} наборов")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"repair_shared_sets: update camp {tgt_id}: {str(exc)[:150]}")

    if maps_dirty:
        try:
            maps_path.write_text(
                json.dumps(maps, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"repair_shared_sets: maps write: {str(exc)[:120]}")


def _repair_shopping_filters(
    results: List[dict],
    *,
    src_dir: Path,
    workdir: Path,
    grid: gf.GridClient,
    repairs: list,
    errors: list,
    log: Callable[[str], None],
) -> None:
    """D19: до-создать ShoppingAd для кампаний с неполным shopping_filter_count.

    Grid-путь без баллов (gf.GridClient.add_shopping_ads).
    Идемпотентно: объявления уже в maps["ads"] → пропуск.
    """
    # Собираем src_id кампаний, требующих ремонта
    affected: Dict[str, str] = {}  # str(src_id) → str(tgt_id)
    for row in results:
        if row.get("dimension") != "shopping_filter_count":
            continue
        if not row.get("repairable"):
            continue
        if row.get("status") in (_OK, _EXCLUDED):
            continue
        scope = row.get("scope", "")
        if scope.startswith("campaign:"):
            tail = scope[len("campaign:"):]
            if "→" in tail:
                s_id, t_id = tail.split("→", 1)
                affected[s_id] = t_id
    if not affected:
        return

    # Читаем shopping_ads.json
    shopping_ads: list = []
    try:
        sa_path = src_dir / "shopping_ads.json"
        if sa_path.exists():
            shopping_ads = json.loads(sa_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"repair_shopping: shopping_ads.json: {str(exc)[:150]}")
        return

    # Читаем id_maps
    maps_path = workdir / "id_maps.json"
    maps: dict = {}
    try:
        if maps_path.exists():
            maps = json.loads(maps_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"repair_shopping: id_maps.json: {str(exc)[:150]}")
        return
    maps.setdefault("ads", {})
    maps.setdefault("adgroups", {})
    maps.setdefault("feeds", {})

    shop_items: list = []
    shop_src_ids: list = []
    shop_camp_src_ids: list = []
    for sa in shopping_ads:
        src_camp_id = str(sa.get("CampaignId") or "")
        if src_camp_id not in affected:
            continue
        src_ad_id = str(sa.get("Id") or "")
        if src_ad_id and src_ad_id in maps["ads"]:
            continue  # идемпотентно
        gid = maps["adgroups"].get(str(sa.get("AdGroupId") or ""))
        sad = sa.get("ShoppingAd") or {}
        fid = maps["feeds"].get(str(sad.get("FeedId") or ""))
        if not gid or not fid:
            log(f"repair_shopping: пропуск ad {src_ad_id} — нет mapped adgroup/feed")
            continue
        item: dict = {"adgroup_id": int(gid), "feed_id": int(fid)}
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
        shop_src_ids.append(src_ad_id)
        shop_camp_src_ids.append(src_camp_id)

    if not shop_items:
        return

    try:
        new_ids = grid.add_shopping_ads(shop_items) or []
        added_by_camp: Dict[str, int] = {}
        for src_ad, camp_id, new_id in zip(shop_src_ids, shop_camp_src_ids, new_ids):
            if new_id:
                maps["ads"][str(src_ad)] = int(new_id)
                added_by_camp[camp_id] = added_by_camp.get(camp_id, 0) + 1

        if added_by_camp:
            maps_path.write_text(
                json.dumps(maps, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            for row in results:
                if row.get("dimension") != "shopping_filter_count":
                    continue
                scope = row.get("scope", "")
                if not scope.startswith("campaign:"):
                    continue
                tail = scope[len("campaign:"):]
                s_id = tail.split("→", 1)[0] if "→" in tail else ""
                if s_id not in added_by_camp:
                    continue
                n_added = added_by_camp[s_id]
                n_src = row.get("source") or 0
                row["target"] = n_added
                row["status"] = _OK if n_added >= n_src else _MISMATCH
                repairs.append({
                    "scope": scope,
                    "dimension": "shopping_filter_count",
                    "action": "add_shopping_ads",
                    "added": n_added,
                    "source": n_src,
                })
                log(f"repair_shopping: {scope} → {n_added}/{n_src} ShoppingAds")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"repair_shopping: add_shopping_ads: {str(exc)[:200]}")


def _repair_keywords(
    results: List[dict],
    *,
    src_dir: Path,
    workdir: Path,
    target_login: str,
    target_agency: str,
    geo_pairs: Optional[list],
    repairs: list,
    errors: list,
    log: Callable[[str], None],
) -> None:
    """keyword_count: дозалить недостающие ключи по кампании через v5 (operator units).

    Причина существования: наблюдалось, что на части кампаний ключи при аплоаде не оседают,
    хотя v5 keywords.add вернул truthy Id и failed=0 (под-копирование во время создания кампании).
    verify это ловит, но auto_repair раньше НЕ имел ремонтёра keyword_count → repairs=0.
    Здесь сверяем ЖИВОЙ keywords.get по кампании с источником и дозаливаем недостающее.

    Персистентность доказана: одиночный/батчевый v5 add на этих же группах оседает; ограничение
    API — не более 1000 ключей на запрос (код 9300), поэтому батч 900. Гео-морфология фраз —
    та же (geo_pairs), что применял step_keywords, иначе дубли (морфнутая≠исходная) раздуют цель."""
    if _v5_call is None or _token_for_login is None or _direct_tokens is None:
        errors.append("repair_keywords: DI не инициализирован (configure() не вызван)")
        return
    rows_kw = [
        r for r in results
        if r.get("dimension") == "keyword_count" and r.get("repairable")
        and r.get("status") not in (_OK, _EXCLUDED)
    ]
    if not rows_kw:
        return
    try:
        keywords = json.loads((src_dir / "keywords.json").read_text("utf-8")) \
            if (src_dir / "keywords.json").exists() else []
        adgroups = json.loads((src_dir / "adgroups.json").read_text("utf-8")) \
            if (src_dir / "adgroups.json").exists() else []
        maps = json.loads((workdir / "id_maps.json").read_text("utf-8")) \
            if (workdir / "id_maps.json").exists() else {}
    except Exception as exc:  # noqa: BLE001
        errors.append(f"repair_keywords: чтение снапшота: {str(exc)[:150]}")
        return
    adg_map = {str(k): str(v) for k, v in (maps.get("adgroups") or {}).items()}
    g2c = {str(g.get("Id")): str(g.get("CampaignId")) for g in adgroups}
    cgm = None
    if geo_pairs:
        try:
            from . import copy_geo_morph as cgm  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            cgm = None
    try:
        token, _ = _token_for_login(target_login, target_agency, _direct_tokens())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"repair_keywords: token: {str(exc)[:150]}")
        return
    if not token:
        errors.append("repair_keywords: токен пуст, пропуск")
        return

    def _chunks(seq, n):
        for i in range(0, len(seq), n):
            yield seq[i:i + n]

    def _live_kw(tgt_cid: int) -> Dict[int, set]:
        """{tgt_gid: {phrase_lower}} — живые ключи цели по кампании (постранично)."""
        out: Dict[int, set] = {}
        offset = 0
        while True:
            j = _v5_call("keywords", "get", token, target_login, {
                "SelectionCriteria": {"CampaignIds": [tgt_cid]},
                "FieldNames": ["Id", "AdGroupId", "Keyword"],
                "Page": {"Limit": 10000, "Offset": offset},
            })
            batch = (j.get("result") or {}).get("Keywords") or []
            for kw in batch:
                out.setdefault(int(kw.get("AdGroupId") or 0), set()).add(
                    str(kw.get("Keyword") or "").strip().lower())
            if len(batch) < 10000:
                break
            offset += 10000
        return out

    for row in rows_kw:
        scope = row.get("scope", "")
        if not scope.startswith("campaign:") or "→" not in scope:
            continue
        src_id, tgt_id_str = scope[len("campaign:"):].split("→", 1)
        if not tgt_id_str.strip().isdigit():
            continue
        tgt_cid = int(tgt_id_str)
        # желаемые фразы по target-группам (гео-морф как в step_keywords, без autotargeting)
        desired: Dict[int, set] = {}
        for k in keywords:
            if g2c.get(str(k.get("AdGroupId"))) != src_id:
                continue
            phrase = str(k.get("Keyword") or "").strip()
            if not phrase or phrase.startswith("---"):
                continue
            tgt_g = adg_map.get(str(k.get("AdGroupId")))
            if not tgt_g or not str(tgt_g).isdigit():
                continue
            if cgm and geo_pairs:
                p2, _n = cgm.apply_replacements(phrase, geo_pairs)
                phrase = (p2 or "").strip() or phrase
            desired.setdefault(int(tgt_g), set()).add(phrase)
        if not desired:
            continue
        try:
            live = _live_kw(tgt_cid)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"repair_keywords {tgt_cid}: live get: {str(exc)[:150]}")
            continue
        to_add = [
            {"AdGroupId": gid, "Keyword": ph}
            for gid, phrases in desired.items()
            for ph in phrases
            if ph.strip().lower() not in live.get(gid, set())
        ]
        if not to_add:
            continue
        added = 0
        for batch in _chunks(to_add, 900):
            try:
                j = _v5_call("keywords", "add", token, target_login, {"Keywords": batch})
            except Exception as exc:  # noqa: BLE001
                errors.append(f"repair_keywords {tgt_cid}: add: {str(exc)[:150]}")
                continue
            err = (j.get("error") or {}).get("error_string") if isinstance(j.get("error"), dict) else None
            if err:
                errors.append(f"repair_keywords {tgt_cid}: v5 {err[:120]}")
                continue
            for ar in ((j.get("result") or {}).get("AddResults") or []):
                if isinstance(ar, dict) and ar.get("Id"):
                    added += 1
        if added:
            repairs.append({"scope": scope, "dimension": "keyword_count",
                            "action": "add_keywords", "added": added})
            try:
                row["target"] = int(row.get("target") or 0) + added
                if str(row.get("target")) == str(row.get("source")):
                    row["status"] = _OK
            except (TypeError, ValueError):
                pass
            log(f"repair keywords {tgt_cid}: дозалито {added} (недоставало {len(to_add)})")


def run_copy_repair(
    report: dict,
    *,
    src_dir: Any,
    workdir: Any,
    target_login: str,
    target_agency: str,
    grid: Optional[gf.GridClient] = None,
    geo_pairs: Optional[list] = None,
    log: Optional[Callable[[str], None]] = None,
) -> dict:
    """Авто-ремонт repairable=True измерений из результата run_copy_verification.

    ГАРАНТИИ (жёсткие):
    - Только ADD/SET недостающего, никогда не удаляет entity цели.
    - Идемпотентно: уже созданное/привязанное → пропуск.
    - Ошибки → rep["errors"], исключение НЕ поднимает.
    - После ремонта обновляет строки report["results"] in-place (status / target).
    - Use-Operator-Units: D3 — v5 с токеном; D19 — Grid без баллов.

    Ремонтируемые (repairable=True в verify):
        D3  shared_set_count — создать/найти shared минус-наборы + привязать (v5).
        D19 shopping_filter_count — до-создать ShoppingAd через Grid.

    НЕ ремонтируем (repairable=False в verify):
        D10 audiences, D11 bid_modifiers, D14 button_cta — технический барьер (нет writer).

    Returns:
        {"repairs": [{scope, dimension, action, ...}], "errors": [str, ...]}
    """
    _log = log or _nolog
    repairs: List[dict] = []
    errors: List[str] = []
    src_dir_p = Path(src_dir)
    workdir_p = Path(workdir)

    results = report.get("results") or []
    has_repairable = any(
        r.get("repairable") and r.get("status") not in (_OK, _EXCLUDED)
        for r in results
    )
    if not has_repairable:
        _log("copy_repair: нечего чинить (нет repairable=True строк с неOK/неEXCLUDED статусом)")
        return {"repairs": repairs, "errors": errors}

    # D3: shared negative keyword sets (v5 с токеном — operator units)
    _repair_shared_sets(
        results,
        src_dir=src_dir_p,
        workdir=workdir_p,
        target_login=target_login,
        target_agency=target_agency,
        repairs=repairs,
        errors=errors,
        log=_log,
    )

    # keyword_count: дозалить недостающие ключи по кампании (v5, operator units).
    _repair_keywords(
        results,
        src_dir=src_dir_p,
        workdir=workdir_p,
        target_login=target_login,
        target_agency=target_agency,
        geo_pairs=geo_pairs,
        repairs=repairs,
        errors=errors,
        log=_log,
    )

    # D19: shopping filter count (Grid без баллов)
    if grid is not None:
        _repair_shopping_filters(
            results,
            src_dir=src_dir_p,
            workdir=workdir_p,
            grid=grid,
            repairs=repairs,
            errors=errors,
            log=_log,
        )
    else:
        _log("copy_repair: D19 пропущен — grid=None (GridClient цели не передан)")

    _log(
        f"copy_repair: итог — repairs={len(repairs)}, errors={len(errors)}"
    )
    return {"repairs": repairs, "errors": errors}

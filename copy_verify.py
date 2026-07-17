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

        # D3: shared_sets
        shared_sets_raw = camp.get("NegativeKeywordSharedSetIds") or []
        shared_set_count = len([x for x in shared_sets_raw if str(x).strip()])

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

        # D7: callouts из campaigns_edit_rows
        callouts_data = edit_c.get("inheritableCallouts") or {}
        callout_ids_tgt = [str(x) for x in (callouts_data.get("calloutIds") or [])
                           if str(x).strip()]
        callout_count = len(callout_ids_tgt)

        # D8: sitelinks
        sl_data = edit_c.get("inheritableSitelinkSet") or {}
        sl_set_id = str(sl_data.get("sitelinkSetId") or "")
        has_sitelinks = bool(sl_set_id and sl_set_id not in ("0", ""))

        # D9: images — campaign_content_counts.adaptive_images_read
        adaptive_total = counts_c.get("adaptive_total")       # None если не читалось
        no_images_ads = counts_c.get("no_images_ads")         # None если не читалось
        adaptive_images_read = bool(counts_c.get("adaptive_images_read"))

        if adaptive_images_read and adaptive_total is not None and no_images_ads is not None:
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

        # D7: callouts
        s7, t7 = src_c["callout_count"], tgt_c["callout_count"]
        results.append(_row(scope, "callout_count",
                            _OK if s7 == t7 else _MISMATCH, s7, t7,
                            repairable=True,
                            repair_hint="step_attach_callouts copy_steps.py:332; "
                                        "grid.add_callouts copy_engine.py:1987"))

        # D8: sitelinks
        s8, t8 = src_c["has_sitelinks"], tgt_c["has_sitelinks"]
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
                            repair_hint="Мутатора кнопки НЕТ — copy_steps.py:998 намеренно "
                                        "не переносит button; UpdateAdaptiveTextAds не пишет button "
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
                             log: Optional[Callable[[str], None]] = None,
                             ) -> List[dict]:
    """Задача 1: измерение гео-консистентности ключей/минус-слов (REPORT-ONLY, snapshot-based).

    Проверяет snapshot-файлы ИСТОЧНИКА на два признака:
    1. geo_kw_source_residual — количество фраз в keywords.json, содержащих формы
       ИСТОЧНИКА (они должны были быть заменены step_keywords гео-морфологией).
    2. geo_neg_target_blocked — количество минус-слов в adgroups.json/campaigns.json,
       содержащих формы ЦЕЛЕВОГО города (они должны были быть отфильтрованы).

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
    kw_with_src = 0
    try:
        keywords = _rj(src_dir / "keywords.json")
        for kw in keywords:
            phrase = (kw.get("Keyword") or "").lower()
            if not phrase:
                continue
            if any(re.search(r"\b" + re.escape(sf) + r"\b", phrase, re.UNICODE)
                   for sf in source_forms):
                kw_with_src += 1
    except Exception as e:  # noqa: BLE001
        _log(f"geo_kw_consistency: keywords.json read error: {str(e)[:150]}")

    rows.append(_row(
        "geo_kw_source_residual",
        _OK if kw_with_src == 0 else _MISMATCH,
        kw_with_src,
        0,
        repair_hint=(
            "step_keywords (copy_steps.py) применяет гео-замену к фразам "
            "через ctx.geo_pairs; 0 = замена успешно убрала формы источника из keywords.json"
        ),
    ))

    # 2) geo_neg_target_blocked: сколько минус-слов содержат формы ЦЕЛЕВОГО города.
    # Проверяем campaigns.json (campaign-level NegativeKeywords) и adgroups.json (group-level).
    neg_with_tgt = 0
    try:
        campaigns = _rj(src_dir / "campaigns.json")
        for camp in campaigns:
            raw = camp.get("NegativeKeywords") or {}
            items = raw.get("Items") if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
            for m in (items or []):
                low = (m or "").lower()
                if any(re.search(r"\b" + re.escape(tf) + r"\b", low, re.UNICODE)
                       for tf in target_forms):
                    neg_with_tgt += 1
    except Exception as e:  # noqa: BLE001
        _log(f"geo_kw_consistency: campaigns.json read error: {str(e)[:150]}")

    try:
        adgroups = _rj(src_dir / "adgroups.json")
        for ag in adgroups:
            raw = ag.get("NegativeKeywords") or {}
            items = raw.get("Items") if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
            for m in (items or []):
                low = (m or "").lower()
                if any(re.search(r"\b" + re.escape(tf) + r"\b", low, re.UNICODE)
                       for tf in target_forms):
                    neg_with_tgt += 1
    except Exception as e:  # noqa: BLE001
        _log(f"geo_kw_consistency: adgroups.json read error: {str(e)[:150]}")

    rows.append(_row(
        "geo_neg_target_blocked",
        _OK if neg_with_tgt == 0 else _MISMATCH,
        neg_with_tgt,
        0,
        repair_hint=(
            "grid-cookie путь: _copy_geo_filter_negatives (copy_engine.py) фильтрует group-level; "
            "v5-путь: campaign/group негативы заливаются direct_copy.py до наших шагов — "
            "ненулевое значение = диагностически значимо для v5-копирования"
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
        geo_rows = check_geo_kw_consistency(src_dir, geo_pairs or [], log=_log)
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

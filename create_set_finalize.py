"""Create-set Grid finalize helpers extracted from blueprint.py."""

from __future__ import annotations

import json
import time

_DEPS: dict = {}


def configure(deps: dict) -> None:
    """Bind blueprint dependencies used by finalize helpers."""
    _DEPS.clear()
    _DEPS.update(deps)
    globals().update(deps)


def _search_platforms(tp_code: str) -> dict:
    """Платформы поисковых мест показа (HAR 33/34): search=True, gallery/network=False.
    Динамика (tp4) = organic=True; чистый Поиск (tp2) = organic=False."""
    p = dict(_PLATFORMS_SEARCH_ONLY)
    p["organic"] = (str(tp_code or "").lower() == "tp4")
    return p

def _finalize_rsya(login: str, campaign_id: int, *, name: str, goal_id: int,
                   cpa_rub, weekly_rub, counter_ids: list, pay_for_conversion: bool,
                   callout_ids=None, sitelink_set_id=None, promo_id=None,
                   minus_set_ids=None, bid_modifiers: dict | None = None,
                   grid_cookie: str | None = None, disabled_places: list | None = None) -> list:
    """Grid-докрутка ЕПК tp1 (канал РСЯ): уточнения/быстрые ссылки/промо на уровне кампании +
    инварианты, СОХРАНЯЯ чистый РСЯ. Ключевое отличие от gf.GridClient.finalize (поиск-only):
    network-only + isOrganicSearchEnabled=False + placementTypes=[] — иначе grid отдаёт
    ORGANIC_PLACEMENT_TYPES_INVALID_COMBINATION (проверено live на porg-psm5h7q6, 2026-06-22).
    grid_finalize.py не трогаем — берём только его примитивы (_TEMPLATE/_MUTATION/CSRF/post)."""
    import datetime as _dt
    gc = gf.GridClient(login, cookie=grid_cookie)
    gc._bootstrap_csrf()
    uc = json.loads(json.dumps(gf._TEMPLATE))            # deepcopy HAR-шаблона
    uc["id"] = str(campaign_id)
    uc["name"] = name
    uc["strategyId"] = None
    uc["startDate"] = _dt.date.today().isoformat()       # шаблонная дата устаревает → ставим сегодня
    uc["metrikaCounters"] = [int(c) for c in (counter_ids or [])]
    uc["biddingStategyWithPlatforms"]["platforms"] = dict(_PLATFORMS_RSYA)
    uc["biddingStategyWithPlatforms"]["strategyData"] = {
        "goalId": str(goal_id), "avgCpa": str(int(cpa_rub)), "sum": str(int(weekly_rub)),
        "budgetType": "WEEKLY", "payForConversion": bool(pay_for_conversion),
        "payForShows": False, "isExplorationBudgetValueCustom": None,
        "minExplorationBudget": None,
    }
    uc["placementTypes"] = []                            # РСЯ: пустой список (НЕ None — иначе ORGANIC-конфликт)
    uc["disabledPlaces"] = list(disabled_places or [])   # #21 минус-площадки РСЯ (HAR45, нижний регистр)
    uc["isOrganicSearchEnabled"] = False                # органика ВЫКЛ — обязательно при пустом placement
    uc["bannerHrefParams"] = ""                          # UTM только на уровне групп (trackingParams), не кампании
    uc["inheritableCallouts"] = {"calloutIds": [str(i) for i in (callout_ids or [])]}
    uc["inheritableSitelinkSet"] = {"sitelinkSetId": str(sitelink_set_id) if sitelink_set_id else None}
    uc["promoExtensionId"] = str(promo_id) if promo_id else None
    uc["libraryMinusKeywordsIds"] = [str(i) for i in (minus_set_ids or [])]
    uc["bidModifiers"] = bid_modifiers or {}             # корректировки: Grid (HAR21) на куки-пути; {}=v5-ом ПОСЛЕ
    uc["isAlternativeTextsEnabled"] = False              # инвариант #3
    uc["hasSiteMonitoring"] = True                       # инвариант #4
    uc["hasExtendedGeoTargeting"] = False                # инвариант #5
    uc["isRecommendationsManagementEnabled"] = False     # инвариант #6
    uc["isPriceRecommendationsManagementEnabled"] = False
    uc["enableCompanyInfo"] = False                      # «Карты/Организация» НЕ включаем (шаблон шлёт True)
    r = gc._post("UpdateCampaigns", gf._MUTATION,
                 {"input": {"campaignUpdateItems": [{"unifiedCampaign": uc}]}, "login": login})
    data = r.json()
    res = (data.get("data") or {}).get("updateCampaigns") or {}
    vr = res.get("validationResult") or {}
    if data.get("errors") or vr.get("errors"):
        raise gf.GridFinalizeError("РСЯ-finalize: " + json.dumps(
            data.get("errors") or vr.get("errors"), ensure_ascii=False)[:400])
    # fix 3c: логируем warnings (мутация уже запрашивает их через ValidationWarningFragment)
    warns = vr.get("warnings") or []
    if warns:
        import logging as _log
        _log.getLogger("direct.finalize").warning(
            "РСЯ-finalize campaign %s warnings: %s",
            campaign_id, json.dumps(warns, ensure_ascii=False)[:400],
        )
    # fix 3c: read-back disabledPlaces — подтвердить что Grid принял значение
    if disabled_places:
        _rb_q = (
            "query CampDP($login:String!,$inp:GdCampaignsContainerInput!){"
            "client(searchBy:{login:$login}){campaigns(input:$inp){rowset{"
            "id ...on GdUnifiedCampaign{disabledPlaces}}}}}"
        )
        _rb_inp = {
            "filter": {"campaignIdIn": [str(campaign_id)]},
            "statRequirements": {"preset": "TODAY", "goalIds": [], "useCampaignGoalIds": True},
            "limitOffset": {"limit": 1, "offset": 0},
            "orderBy": [{"order": "ASC", "field": "ID"}],
        }
        try:
            _rb_j = gc._post("CampDP", _rb_q, {"login": login, "inp": _rb_inp}).json()
            _rb_rows = ((((_rb_j.get("data") or {}).get("client") or {})
                         .get("campaigns") or {}).get("rowset") or [])
            _rb_dp = _rb_rows[0].get("disabledPlaces") if _rb_rows else None
            if not _rb_dp:
                import logging as _log
                _log.getLogger("direct.finalize").warning(
                    "РСЯ-finalize campaign %s: disabledPlaces sent=%r read-back=%r — Grid не применил",
                    campaign_id, disabled_places, _rb_dp,
                )
        except Exception:  # noqa: BLE001 — read-back best-effort, не валим создание
            pass
    return res.get("updatedCampaigns") or []

def _grid_minus_pack_id(login: str, name_marker: str = "Минуса общие") -> int | None:
    """Grid (БЕЗ баллов): id набора минус-фраз по маркеру имени («Минуса общие»). HAR40
    MinusPhraseLibrary → getLibraryMinusKeywordsPacks. Куки-фолбэк к v5 negativekeywordsharedsets
    (требуют баллов). None если набора нет/сбой. Кэш per-(login,marker) — id аккаунт-стабилен,
    не дёргаем Grid+CSRF на каждую кампанию."""
    key = (login, name_marker)
    hit = _GRID_MINUS_PACK_CACHE.get(key)
    if hit and (time.time() - hit[1]) < _GRID_ACCOUNT_TTL:
        return hit[0]
    try:
        gc = gf.GridClient(login)
        gc._bootstrap_csrf()
        r = gc._post("MinusPhraseLibrary", _MINUS_LIB_Q, {"input": {}})
        if r.status_code == 403:
            r = gc._post("MinusPhraseLibrary", _MINUS_LIB_Q, {"input": {}})
        rows = ((r.json().get("data") or {}).get("getLibraryMinusKeywordsPacks") or {}).get("rowset") or []
        marker = (name_marker or "").lower()
        named = [x for x in rows if marker and marker in str(x.get("name") or "").lower()]
        pool = named or rows
        best = max(pool, key=lambda x: len(x.get("minusKeywords") or [])) if pool else None   # самый полный набор
        pack_id = int(best["id"]) if (best and best.get("id")) else None
        _GRID_MINUS_PACK_CACHE[key] = (pack_id, time.time())
        return pack_id
    except Exception:  # noqa: BLE001 — best-effort, кампанию не валим
        return None

def _grid_callout_ids(login: str, texts: list | None = None, limit: int = 4) -> list:
    """Grid (БЕЗ баллов): id уточнений аккаунта по текстам (HAR40 Callouts). Куки-фолбэк к v5, когда
    после 152 уточнения не читаются/не цепляются. texts пусто → первые limit уточнений. → список id(str).
    Карта by_text аккаунт-стабильна → кэшируем per-login (Grid+CSRF не на каждую кампанию)."""
    try:
        _hit = _GRID_CALLOUTS_CACHE.get(login)
        if _hit and (time.time() - _hit[1]) < _GRID_ACCOUNT_TTL:
            by_text = _hit[0]
        else:
            gc = gf.GridClient(login)
            gc._bootstrap_csrf()
            r = gc._post("Callouts", _CALLOUTS_Q, {"login": login})
            if r.status_code == 403:
                r = gc._post("Callouts", _CALLOUTS_Q, {"login": login})
            rows = ((r.json().get("data") or {}).get("callouts")) or []
            by_text = {}
            for c in rows:
                t = str(c.get("text") or "").strip().lower()
                if t and c.get("id") and t not in by_text:
                    by_text[t] = str(c["id"])
            _GRID_CALLOUTS_CACHE[login] = (by_text, time.time())
        wanted = [str(t).strip().lower() for t in (texts or []) if str(t).strip()]
        ids: list[str] = []
        for t in wanted:
            if t in by_text and by_text[t] not in ids:
                ids.append(by_text[t])
            if len(ids) >= limit:
                break
        if not ids:                                      # тексты не дали совпадений (или их нет) —
            # #24: единый семантический дедуп — ТА ЖЕ функция, что и v5-путь (одна точка правды,
            # не два расходящихся инлайн-цикла). by_text — уже {text: id}, ровно вход _dedup_callout_ids.
            ids = _dedup_callout_ids(by_text, cap=limit)
        return ids
    except Exception:  # noqa: BLE001 — best-effort
        return []

def _finalize_search_via_grid(login: str, campaign_id: int, *, name: str, goal_id: int,
                              cpa_rub, weekly_rub, counter_ids: list, pay_for_conversion: bool,
                              callout_ids=None, sitelink_set_id=None, promo_id=None,
                              minus_set_ids=None, bid_modifiers: dict | None = None,
                              placement_types: list[str] | None = None,
                              platforms: dict | None = None) -> list:
    """Grid-докрутка ЕПК tp2/tp4 (канал Поиск): инварианты #3/#4/#5/#6 + ассеты кампании + МЕСТА
    ПОКАЗА. platforms: HAR 33/34 — tp2 `_search_platforms('tp2')` (organic=False), tp4 (organic=True);
    дефолт (None) = `gf.PLATFORMS_SEARCH` (старое поведение, gallery=True — для совместимости).
    placementTypes=["SEARCH_PAGE"]. Не ставим isOrganicSearchEnabled=False/placementTypes=[] (это РСЯ)."""
    import datetime as _dt
    gc_fin = gf.GridClient(login)
    gc_fin._bootstrap_csrf()
    uc = json.loads(json.dumps(gf._TEMPLATE))
    uc["id"] = str(campaign_id)
    uc["name"] = name
    uc["strategyId"] = None
    uc["startDate"] = _dt.date.today().isoformat()
    uc["metrikaCounters"] = [int(c) for c in (counter_ids or [])]
    uc["biddingStategyWithPlatforms"]["platforms"] = dict(platforms or gf.PLATFORMS_SEARCH)
    uc["biddingStategyWithPlatforms"]["strategyData"] = {
        "goalId": str(goal_id), "avgCpa": str(int(cpa_rub)), "sum": str(int(weekly_rub)),
        "budgetType": "WEEKLY", "payForConversion": bool(pay_for_conversion),
        "payForShows": False, "isExplorationBudgetValueCustom": None,
        "minExplorationBudget": None,
    }
    # HAR20: tp5 «Ручная настройка» = placementTypes=null + platforms gallery/organic/search.
    # Sentinel [] (пустой список) → null; None (не передан, tp2/tp4) → ["SEARCH_PAGE"]; явный список → сам список.
    uc["placementTypes"] = list(placement_types) if placement_types else (["SEARCH_PAGE"] if placement_types is None else None)
    # #4 review: «Динамические места на поиске» = isOrganicSearchEnabled. Шаблон grid_uc_template.json
    # приносит True; привязываем к platforms.organic (иначе tp2 протекал True). tp2 organic=False→OFF,
    # tp4/tp5 organic=True→ON (как раньше). Ровно один источник правды — тот же (platforms or PLATFORMS_SEARCH).
    uc["isOrganicSearchEnabled"] = bool((platforms or gf.PLATFORMS_SEARCH).get("organic"))
    uc["bannerHrefParams"] = ""                            # UTM только на уровне групп (trackingParams), не кампании
    uc["inheritableCallouts"] = {"calloutIds": [str(i) for i in (callout_ids or [])]}
    uc["inheritableSitelinkSet"] = {"sitelinkSetId": str(sitelink_set_id) if sitelink_set_id else None}
    uc["promoExtensionId"] = str(promo_id) if promo_id else None
    uc["libraryMinusKeywordsIds"] = [str(i) for i in (minus_set_ids or [])]
    uc["bidModifiers"] = bid_modifiers or {}
    uc["isAlternativeTextsEnabled"] = False               # инвариант #3
    uc["hasSiteMonitoring"] = True                        # инвариант #4
    uc["hasExtendedGeoTargeting"] = False                 # инвариант #5
    uc["isRecommendationsManagementEnabled"] = False      # инвариант #6
    uc["isPriceRecommendationsManagementEnabled"] = False
    uc["enableCompanyInfo"] = False
    r = gc_fin._post("UpdateCampaigns", gf._MUTATION,
                     {"input": {"campaignUpdateItems": [{"unifiedCampaign": uc}]}, "login": login})
    data = r.json()
    res = (data.get("data") or {}).get("updateCampaigns") or {}
    vr = res.get("validationResult") or {}
    if data.get("errors") or vr.get("errors"):
        raise gf.GridFinalizeError("search-finalize: " + json.dumps(
            data.get("errors") or vr.get("errors"), ensure_ascii=False)[:400])
    return res.get("updatedCampaigns") or []

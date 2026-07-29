"""Чистые payload-фабрики Grid (спек → формат Grid GraphQL).

Вынесены из grid_create.py для уменьшения размера модуля. grid_create.py ре-экспортирует
все имена отсюда, чтобы внешние импортёры (copy_engine, repair_executor, automation_runtime,
account_service, campaign_spec_audit) продолжали использовать gc.build_ad и т.д. без изменений.

Нет DI-глобалей, нет GridCreateClient. Ленивые импорты внутри функций сохранены намеренно.
"""
from __future__ import annotations

import re

from ..core import campaign as cmc
from ..ai_agents import extend_title_to_max
from ..create.create_set_minus import _minus_char_budget as _cm_minus_char_budget  # единый бюджет 20 000 симв.
from ..text_norm import _trim_clean

# ── Сборщики payload'ов (наш спек → формат Grid) ────────────────────────────
_PLATFORMS_OFF = {k: False for k in (
    "gallery", "network", "search", "telegram", "maxMessenger", "taxi", "pillar",
    "cityBusDisplay", "showcaseScreen", "mediafacade", "supersite", "billboard",
    "cityboard", "cityformat", "organic", "serpGeoWizard", "yandexMaps")}


def _campaign_minus_kw(words) -> list:
    """Обрезать список минус-фраз по символьному бюджету кампании (20 000 симв. без пробелов).
    Применяется к 'minusKeywords' в AddCampaigns; лимит — официальная дока Яндекса.
    Нормализует входной список (str→strip→пропуск пустых), затем делегирует в
    create_set_minus._minus_char_budget — единственный источник правды по бюджету (20 000 симв.)."""
    normalized = [s for w in (words or []) for s in [str(w).strip()] if s]
    return _cm_minus_char_budget(normalized)  # _MINUS_CAMPAIGN_CHAR_BUDGET=20_000 из create_set_minus


def build_unified_campaign(*, name: str, counter_id: int, goal_id: int, cpa: int,
                           weekly_budget: int, start_date: str, href: str = "",
                           network: bool = True, search: bool = False,
                           gallery: bool = False, organic: bool = False,
                           pay_for_conversion: bool = False, time_zone: str = "130",
                           email: str = "", bid_modifiers: dict | None = None,
                           placement_types: list | None = None,
                           disabled_places: list | None = None,
                           minus_keywords: list | None = None) -> dict:
    """Собрать ПОЛНЫЙ GdUnifiedCampaign (48 полей) для AddCampaigns (реверс из HAR14).
    network=True → РСЯ (tp1); search=True → Поиск; gallery+organic → Товарная галерея (tp3/tp5, HAR17).
    Стратегия AUTOBUDGET_AVG_CPA (целевой CPA).
    bid_modifiers — корректировки ставок (HAR21): ставятся прямо в AddCampaigns (campaignId-плейсхолдер
    «9999999» в объекте). Так корректировки попадают на куки-пути БЕЗ баллов для ВСЕХ tp1–tp5.
    minus_keywords — глобальные минус-слова кампании (direct_global_minus_words); проставляются
    на ВСЕХ cookie-типах tp1–tp5/tp3. Обрезаются до 20 000 симв. без пробелов (_campaign_minus_kw)."""
    platforms = dict(_PLATFORMS_OFF)
    platforms["network"] = bool(network)
    platforms["search"] = bool(search)
    platforms["gallery"] = bool(gallery)
    platforms["organic"] = bool(organic)
    return {
        "abExperiments": [], "abSegmentRetargetingConditionId": None,
        "abSegmentStatisticRetargetingConditionId": None,
        "additionalData": {"href": href} if href else {"href": None},
        "attributionModel": "AUTOMATIC", "bannerHrefParams": "", "bidModifiers": (bid_modifiers or {}),
        "biddingStategyWithPlatforms": {
            "platforms": platforms,
            "strategyData": {
                "goalId": str(goal_id), "avgCpa": str(int(cpa)),
                "sum": str(int(weekly_budget) if weekly_budget else int(cpa) * 10),
                "budgetType": "WEEKLY", "payForConversion": bool(pay_for_conversion),
                "payForShows": False, "isExplorationBudgetValueCustom": False,
            },
            "strategyName": "AUTOBUDGET_AVG_CPA",
        },
        "brandSafetyCategories": [],
        "broadMatch": {"broadMatchFlag": False, "broadMatchGoalId": None, "broadMatchLimit": 0},
        "dayBudget": "0",
        "defaultPermalinkId": None, "defaultTrackingPhoneId": None, "deliveryId": None,
        "disabledIps": None, "disabledPlaces": list(disabled_places or []), "dynamicPlacesAdvTextsOnly": False,
        "enableCompanyInfo": True, "enableCpcHold": False, "endDate": None,
        "excludePausedCompetingAds": False,
        "hasAddMetrikaTagToUrl": False, "hasAddOpenstatTagToUrl": False,
        "hasExtendedGeoTargeting": False, "hasSiteMonitoring": True, "hasTitleSubstitute": True,
        "inheritableCallouts": {"calloutIds": []},
        "inheritableSitelinkSet": {"sitelinkSetId": None},
        "isAlternativeTextsEnabled": False, "isOrderPhraseLengthPrecedenceEnabled": False,
        "isOrganicSearchEnabled": False, "isPriceRecommendationsManagementEnabled": False,
        "isRecommendationsManagementEnabled": False, "isS2sTrackingEnabled": False,
        "isUniversalCamp": False, "libraryMinusKeywordsIds": [], "meaningfulGoals": [],
        "metrikaCounters": [int(counter_id)] if counter_id else [],
        "minusKeywords": _campaign_minus_kw(minus_keywords), "name": name[:255],
        "notification": {
            "smsSettings": {"smsTime": {"startTime": {"hour": 9, "minute": 0},
                                        "endTime": {"hour": 21, "minute": 0}}, "enableEvents": []},
            "emailSettings": {"stopByReachDailyBudget": True, "email": email or "victoryagency02@yandex.ru"},
        },
        "placementTypes": (list(placement_types) if placement_types else None),  # tp2 → ['SEARCH_PAGE'] (без динамич. мест)
        "promoExtensionId": None, "reserveHref": None,
        "startDate": start_date,
        "timeTarget": {"enabledHolidaysMode": False, "holidaysSettings": None,
                       "idTimeZone": str(time_zone), "timeBoard": [[100] * 24 for _ in range(7)],
                       "useWorkingWeekends": True},
        "useDiscounts": False,
    }


def build_adgroup(*, campaign_id: int, name: str, region_ids: list, keywords: list,
                  minus_keywords: list | None = None, goal_id: int = 0,
                  autotargeting: bool = True, autotargeting_profile: str = "",
                  retargeting_ids: list | None = None,
                  retargeting_on_search: bool = False) -> dict:
    """Собрать GdAddUnifiedAdGroupItem: группа + ключи (phrase) + минус-слова + интерес-таргетинг.

    retargeting_ids — id УСЛОВИЙ ретаргетинга (аудитории из структуры слепка), уже
    резолвнутые под ЦЕЛЕВОЙ кабинет (`create_set_audiences.resolve_for_account`).
    retargeting_on_search=True → поиск (tp2/tp4) → поле `searchRetargetings`;
    False → сеть (tp1/tp5) → поле `retargetings`. Пусто → оба поля пустые, как раньше.
    """
    kw = [{"phrase": str(k)} for k in (keywords or []) if str(k).strip()][:200]
    if autotargeting_profile == "search_tp2":
        # Поиск tp2/tp4 (HAR 38): группа ВСЕГДА имеет активный relevanceMatch профиля
        # «Целевые запросы» (EXACT_V2_MARK) + «Запросы без упоминания вашего бренда или
        # брендов конкурентов» (WITHOUT_BRAND) — НЕЗАВИСИМО от того, autotarget-группа или
        # с реальными ключами. В интерфейсе Директа поисковая кампания не может иметь
        # выключенный автотаргет, поэтому профиль применяется и при autotargeting=False.
        relevance_match = {
            "isActive": True, "id": None,
            "relevanceMatchCategories": ["EXACT_V2_MARK"],
            "autotargetingBrandSettings": ["WITHOUT_BRAND"],
        }
    elif autotargeting:
        # РСЯ (tp1) / Товарная галерея (tp3/tp5) — своя ветка: все категории + все бренды.
        relevance_match = {
            "isActive": True, "id": None,
            "relevanceMatchCategories": ["ALTERNATIVE_MARK", "BROADER_MARK",
                                         "ACCESSORY_MARK", "EXACT_V2_MARK", "NARROW_MARK"],
            "autotargetingBrandSettings": ["WITH_BRAND", "WITHOUT_BRAND",
                                           "WITH_COMPETITOR_BRAND"],
        }
    else:
        relevance_match = {"isActive": False, "id": None, "relevanceMatchCategories": [],
                           "autotargetingBrandSettings": []}

    # Аудитории группы (условия ретаргетинга). Формат — дословно билдер интерфейса Директа
    # (HAR 73har, чанк b10fd987c1079081.chunk.js): {retCondId: <id условия>, id: <id связки|null>}.
    # Связка новая → id=None. Поиск (tp2/tp4) → searchRetargetings, сеть (tp1/tp5) → retargetings.
    from ..create.create_set_audiences import retargetings_payload as _rets_payload
    _rets = _rets_payload(retargeting_ids)

    item = {
        "campaignId": str(campaign_id), "name": (name or "группа")[:255],
        "regionIds": [int(r) for r in (region_ids or []) if str(r).lstrip("-").isdigit()] or [225],
        "hyperGeoId": None, "hyperlocalGeoSegments": None, "audienceTargeting": "ALL_AUDIENCE",
        "adGroupMinusKeywords": [str(m) for m in (minus_keywords or [])][:100],
        "keywords": kw, "libraryMinusKeywordsIds": [],
        # INLINE-условие (retargetingCondition) НЕ ставим: гол в conditionRules требует поле time
        # (RetargetingDefectIds.REQUIRED_TIME_FOR_GOAL_OR_SEGMENT), а INTERESTS/HOST/APPLICATION
        # идут другим путём (UAC tp6/tp7). Аудитории структуры — это ГОТОВЫЕ условия по id, они
        # живут в retargetings/searchRetargetings ниже, а не здесь. Цель конверсии — в СТРАТЕГИИ.
        "retargetingCondition": None,
        "caRetargetingCondition": None,
        "retargetings": ([] if retargeting_on_search else _rets),
        "searchRetargetings": (_rets if retargeting_on_search else []),
        "offerRetargeting": {"isActive": True, "id": None},
        "relevanceMatch": relevance_match,
        "promoExtensionInheritancePolicy": "MERGE",
        "inheritableCallouts": {"policy": "INHERIT"},
        "inheritableSitelinkSet": {"policy": "INHERIT"},
        # UTM на УРОВНЕ ГРУППЫ (правило Семёна: метка должна быть в tp1–tp5). Тот же макрос, что и
        # v5-путь (_UTM_TEMPLATE_TP1 = cmc.UTM_TEMPLATE) — параметры доедут до всех ссылок группы.
        "generalPrice": None, "trackingParams": cmc.UTM_TEMPLATE, "bidModifiers": {},
        "contentTypeShowSettings": {"usualAdsShowFilter": "NO_ADULT_CONTENT"},
        "contentLanguage": None, "useBidModifiers": True,
    }
    return item


def _safe_old_price(current, old=0) -> int:
    """Старая цена ТОЛЬКО из фида (правило Семёна 2026-07-05, синхронно с
    create_set_feeds._safe_old_price): нет old в фиде / old<=current → 0 (поле пустое).
    Синтетика +12% убрана — цены не выдумываем."""
    try:
        cur = int(current or 0)
        old_i = int(old or 0)
    except (TypeError, ValueError):
        return 0
    if cur <= 0:
        return 0
    return old_i if old_i > cur else 0


def _dedup_keep(seq, n, cut):
    """Обрезать каждый элемент до cut, выкинуть пустые и ДУБЛИ (Grid: MUST_NOT_CONTAIN_DUPLICATED),
    сохранить порядок, вернуть ≤n штук."""
    out, seen = [], set()
    for x in (seq or []):
        raw = str(x).strip()
        # Direct treats slash-separated chunks as one word for the max-word validator.
        raw = re.sub(r"\S{23,}", lambda m: m.group(0).replace("/", " "), raw)
        # Обрезка по слову + чистка оборванного хвоста («…Одобрение за 30» — live-кейс psm/ozge),
        # а не жёсткий срез посреди слова; ≤cut строки _trim_clean не трогает (кроме word-tail).
        s = (raw if len(raw) <= cut else _trim_clean(raw, cut)).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= n:
            break
    return out


def _fill_titles(seq, n=7, cut=56):
    src = [str(x or "").strip() for x in (seq or []) if str(x or "").strip()]
    # Добиваем короткие входящие заголовки суффиксом-УТП до целевых 48–56 символов.
    # idx ротирует банк суффиксов; used — предотвращает повторы суффикса внутри набора.
    _ext_used: set[str] = set()
    src = [extend_title_to_max(t, idx, max_len=cut, used=_ext_used)
           for idx, t in enumerate(src)]
    out = list(src)
    anchor = (src[0].split(".")[0].strip() if src else "Авто в кредит").rstrip(" ,.")
    if len(anchor) > 34:
        sp = anchor[:34].rfind(" ")
        anchor = anchor[:sp if sp > 15 else 34].rstrip(" ,.")
    tails = [
        "Кредит от 9 000 ₽/мес",
        "Одобрение за 30 минут",
        "Трейд-ин выше рынка",
        "КАСКО на 1 год",
        "Первый взнос 0 ₽",
        "Выгода при покупке",
        "15 банков-партнеров",
        "Авто в наличии",
    ]
    for tail in tails:
        if len(out) >= n:
            break
        cand = f"{anchor}. {tail}" if anchor else tail
        if len(cand) > cut:
            cand = f"{anchor} {tail}"
        if len(cand) > cut:
            cand = _trim_clean(cand, cut)   # по слову + чистка хвоста («…Одобрение за 30»)
        # Добиваем сгенерированный кандидат до целевой длины (≤2 свободных символов).
        if cand:
            cand = extend_title_to_max(cand, len(out), max_len=cut, used=_ext_used)
        if cand and cand not in out:
            out.append(cand)
    return out


def _fill_bodies(seq, n=3, cut=81):
    out = [str(x or "").strip() for x in (seq or []) if str(x or "").strip()]
    fillers = [
        "Автокредит от 9 000 ₽/мес. Подберите авто онлайн.",
        "Первый взнос 0 ₽. КАСКО на 1 год бесплатно.",
        "Трейд-ин выше рынка. Оценим авто и зачтем в кредит.",
        "Новые авто в наличии. Подбор кредита от 15 банков-партнеров.",
    ]
    for cand in fillers:
        if len(out) >= n:
            break
        cand = _trim_clean(cand, cut).rstrip(" ,.")
        if cand and cand not in out:
            out.append(cand)
    return out


def build_ad(*, adgroup_id: int, href: str, titles: list, bodies: list,
             image_hashes: list | None = None, ad_price: dict | None = None) -> dict:
    """Собрать GdAddAdaptiveTextAd: комбинаторное объявление (несколько заголовков/текстов) + adPrice."""
    return {
        "href": href, "hrefParams": "", "domain": None,
        "titles": _dedup_keep(_fill_titles(titles), 7, 56),   # 7 заголовков (UI Директа «… из 7»)
        "bodies": _dedup_keep(_fill_bodies(bodies), 3, 81),
        # ДЕДУП хэшей ОБЯЗАТЕЛЕН: пул ct с одинаковыми файлами → один hash дважды →
        # MUST_NOT_CONTAIN_DUPLICATED_ELEMENTS валит ВЕСЬ батч AddAdaptiveTextAds (150 групп,
        # «0 ads») → partial-кампания удаляется. Живой кейс 2026-07-05: tp1 Модели-АТ падала
        # 3 раза подряд на adAddItems[N].imageHashes[4] (в RMW-апдейтах дедуп был, тут — нет).
        "imageHashes": list(dict.fromkeys(image_hashes or [])), "creativeIds": [],
        "permalinkId": None, "phoneId": None,
        "adPrice": ad_price,                              # {"price","priceOld","prefix","currency":"RUB"} | None
        "erirAdDescription": None,
        "inheritableCallouts": {"policy": "INHERIT"},
        "inheritableSitelinkSet": {"policy": "INHERIT"},
        "adGroupId": str(adgroup_id),
    }


def build_shopping_ad(*, adgroup_id: int, feed_id: int, body: str = "", login: str = "") -> dict:
    """Собрать GdAddShoppingAdItem (Товарная галерея tp3/tp5) — реверс из HAR17. feed_id обязателен;
    body — единый текст объявления (товары тянутся из фида). fieldsToUseAs* = None (дефолт фида).
    login: если передан — добавляет feedFilter с глобальными минус-марками/моделями."""
    item: dict = {
        "adGroupId": str(adgroup_id), "permalinkId": None, "phoneId": None,
        "fieldsToUseAsBody": None, "fieldsToUseAsName": None, "feedId": str(feed_id),
        "bodies": [_trim_clean(str(body), 81)] if str(body or "").strip() else [],
        "hrefParams": "",
        "inheritableCallouts": {"policy": "INHERIT"},
        "inheritableSitelinkSet": {"policy": "INHERIT"},
    }
    if login:
        try:
            from ..create import create_set_feeds as _csf
            _bf = _csf._resolve_feed_field(login, feed_id, "brand") or "vendor"
            _mf = _csf._resolve_feed_field(login, feed_id, "model") or "model"
            conds = _csf._minus_marks_grid_conditions(brand_field=_bf, model_field=_mf)
            if conds:
                item["feedFilter"] = {"tab": "CONDITION", "conditions": conds}
        except Exception:  # noqa: BLE001
            pass
    return item

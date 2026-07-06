"""Куки-движок СОЗДАНИЯ кампаний (Grid GraphQL на куках агентства, БЕЗ баллов API).

Назначение: когда у агентства исчерпаны баллы (error 152), а пользователь ЯВНО согласился
через поп-ап «создать по куки (небезопасно)» — создаём tp1–tp5 не через v5/v501 (баллы),
а через приватный web-api/grid (куки). Мутации реверс-инжинирены из HAR (см. direct/_har/):
  AddCampaigns        — кампания (unifiedCampaign: стратегия/платформы/счётчик)
  AddUnifiedAdGroups  — группы + ключи + таргетинг + минус-слова
  AddAdaptiveTextAds  — комбинаторные объявления (ResponsiveAd) + adPrice

⚠️ НЕБЕЗОПАСНО: приватный интерфейс Директа, риск временной блокировки. Запускается ТОЛЬКО
по явному согласию пользователя (флаг via_cookie из поп-апа), НИКОГДА автоматически.
"""
from __future__ import annotations

import re
import time
from typing import Any

import requests

from . import campaign as cmc
from .text_norm import _trim_clean

GRID_URL = "https://direct.yandex.ru/web-api/grid/api"
COMMON_IMAGE_CTS = {
    "ct0000", "ct0001", "ct0002", "ct0003", "ct0004", "ct0005", "ct0006",
    "ct0007", "ct0008", "ct0009", "ct0010", "ct0013", "ct0014",
}

_ADD_CAMPAIGNS_Q = (
    "mutation AddCampaigns($input:GdAddCampaignsInput!$login:String!){reqId:getReqId "
    "addCampaigns(input:$input){addedCampaigns{id}validationResult{errors{code params path}"
    "warnings{code params path}}}getClientMutationId(input:{login:$login}){mutationId}}")
_ADD_GROUPS_Q = (
    "mutation AddUnifiedAdGroups($unifiedAddInput:[GdAddUnifiedAdGroupItemInput!]!){reqId:getReqId "
    "addUnifiedAdGroups(input:{addItems:$unifiedAddInput}){addedAdGroupItems{adGroupId}"
    "validationResult{errors{code params path}warnings{code params path}}}}")
_ADD_ADS_Q = (
    "mutation AddAdaptiveTextAds($addInput:GdAddAdaptiveTextAdsInput!){reqId:getReqId "
    "addAdaptiveTextAds(input:$addInput){addedAds{id}validationResult{errors{code params path}"
    "warnings{code params path}}}}")
_DELETE_CAMPAIGNS_Q = (
    "mutation DeleteCampaigns($input:GdCampaignIdsListInput!){reqId:getReqId "
    "deleteCampaigns(input:$input){validationResult{errors{code params path}"
    "warnings{code params path}}}}")
_ADD_SHOPPING_ADS_Q = (   # товарное объявление (Товарная галерея tp3/tp5) по фиду — реверс из HAR17
    "mutation AddShoppingAds($input:GdAddShoppingAdsInput!){reqId:getReqId "
    "addShoppingAds(input:$input){addedAds{id}validationResult{errors{code params path}"
    "warnings{code params path}}}}")
_ADD_KEYWORDS_Q = (       # заливка ключевых фраз через Grid (без баллов API)
    "mutation AddKeywords($input:GdAddKeywordsInput!){"
    "addKeywords(input:$input){addedItems{adGroupId keywordId}"
    "validationResult{errors{code params path}warnings{code params path}}}}")
_ADGROUP_NAMES_Q = (      # read-back name→id (фикс позиционного сдвига, см. _read_adgroup_name_to_id)
    "query AdGroupNames($login:String!,$inp:GdAdGroupsContainerInput!){"
    "client(searchBy:{login:$login}){"
    "adGroups(input:$inp){rowset{id name}}}}")


class GridCreateError(RuntimeError):
    pass


class GridCreateClient:
    """Тонкий клиент создания через web-api/grid на куках (как grid_finalize.GridClient)."""

    def __init__(self, login: str, cookie: str | None = None, *, timeout: int = 60):
        self.login = login
        self.cookie = cookie or cmc.pick_working_cookie(login)
        self.csrf: str | None = None
        self.timeout = timeout
        self.sess = requests.Session()
        self.sess.verify = False

    @staticmethod
    def _looks_like_login_page(resp: requests.Response) -> bool:
        text = (resp.text or "")[:800].lower()
        return (
            "need_reset" in text
            or "passport.yandex" in text
            or "<title>log in</title>" in text
            or ("<html" in text and "login" in text)
        )

    def _refresh_cookie(self) -> None:
        self.cookie = cmc.pick_working_cookie(self.login, force_refresh=True)
        self.csrf = None
        self.sess = requests.Session()
        self.sess.verify = False

    # ── низкий уровень ───────────────────────────────────────────────────────
    def _post(self, op: str, query: str, variables: dict) -> dict:
        headers = {
            "Cookie": self.cookie, "dna-operation-name": op, "x-direct-api": "1",
            "x-detected-locale": "ru", "Content-Type": "application/json",
            "User-Agent": cmc.USER_AGENT, "Origin": "https://direct.yandex.ru",
            "Referer": f"https://direct.yandex.ru/dna/grid/campaigns?ulogin={self.login}",
        }
        if self.csrf:
            headers["x-csrf-token"] = self.csrf
        url = f"{GRID_URL}?operationName={op}&ulogin={self.login}"
        # БЕЗ транспортного ретрая: AddCampaigns/AddGroups/AddAds — НЕ идемпотентны. Обрыв ответа
        # после commit + ретрай = ДУБЛЬ кампании/группы/объявления. Единичный обрыв ловит
        # per-iteration try/except в _copy_grid_unified_campaigns (кампания падает чисто, ре-ран добьёт).
        r = self.sess.post(url, json={"operationName": op, "query": query, "variables": variables},
                           headers=headers, timeout=self.timeout)
        m = re.search(r"_direct_csrf_token=([^;,\s]+)", r.headers.get("Set-Cookie", ""))
        tok = r.cookies.get("_direct_csrf_token") or (m.group(1) if m else None)
        if tok:
            self.csrf = tok
        return r

    def _bootstrap_csrf(self) -> None:
        q = ("query Callouts($login:String!){callouts(input:{searchBy:{login:$login}"
             "filter:{deleted:false}}){id}}")
        r = self._post("Callouts", q, {"login": self.login})
        if r.status_code == 403:
            self._post("Callouts", q, {"login": self.login})

    # Транзиентные серверные ошибки Яндекса в top-level errors (НЕ валидация) — ретраим с backoff.
    # Приходят как {'message':'Внутренняя ошибка сервера ... reqId=...'} → group=0 → hard-fail айтема.
    # Валидационные ошибки (res.validationResult.errors) сюда НЕ попадают — их не ретраим (детерминизм).
    _TRANSIENT_ERR = ("внутренняя ошибка сервера", "internal server error", "internal error",
                      "timeout", "timed out", "temporarily", "try again", "503", "502", "504")

    def _mutate(self, op: str, query: str, variables: dict) -> dict:
        """POST мутации с ретраем на 403 (свежий CSRF) И на транзиентную серверную ошибку Яндекса
        ('Внутренняя ошибка сервера' в top-level errors) — tries+backoff. → JSON. GridCreateError на сбое."""
        _last_transient = None
        for srv_try in range(3):                     # до 3 попыток на транзиентную 5xx-подобную ошибку
            for attempt in range(2):                 # внутренний ретрай: 403(CSRF)/страница логина
                if self.csrf is None:
                    self._bootstrap_csrf()
                r = self._post(op, query, variables)
                if r.status_code == 403:
                    r = self._post(op, query, variables)
                if self._looks_like_login_page(r) and attempt == 0:
                    self._refresh_cookie()
                    continue
                break
            try:
                j = r.json()
            except Exception as e:  # noqa: BLE001
                raise GridCreateError(f"{op}: не-JSON HTTP {r.status_code}: {r.text[:160]}") from e
            errs = j.get("errors")
            if errs:
                _msg = str(errs).lower()
                if any(t in _msg for t in self._TRANSIENT_ERR) and srv_try < 2:
                    _last_transient = str(errs)[:240]
                    time.sleep(0.6 * (srv_try + 1))  # backoff 0.6s → 1.2s перед повтором
                    continue                         # транзиент Яндекса → повторяем мутацию
                raise GridCreateError(f"{op}: {str(errs)[:240]}")
            return j
        raise GridCreateError(f"{op}: транзиент Яндекса не ушёл за 3 попытки: {_last_transient}")

    # ── шаги создания ────────────────────────────────────────────────────────
    def add_campaign(self, unified_campaign: dict) -> int:
        """AddCampaigns → id созданной кампании. unified_campaign — полный GdUnifiedCampaign-блок."""
        j = self._mutate("AddCampaigns", _ADD_CAMPAIGNS_Q,
                         {"login": self.login, "input": {"campaignAddItems": [{"unifiedCampaign": unified_campaign}]}})
        res = (j.get("data") or {}).get("addCampaigns") or {}
        vr = res.get("validationResult") or {}
        if vr.get("errors"):
            raise GridCreateError(f"AddCampaigns validation: {str(vr['errors'])[:240]}")
        added = res.get("addedCampaigns") or []
        if not added or not added[0].get("id"):
            raise GridCreateError(f"AddCampaigns: нет id ({str(res)[:160]})")
        return int(added[0]["id"])

    def add_adgroups(self, items: list[dict]) -> list[int | None]:
        """AddUnifiedAdGroups → список adGroupId (в порядке items; None для упавших)."""
        if not items:
            return []
        if len(items) > _GRID_MUTATION_CHUNK:
            out: list[int | None] = []
            for i in range(0, len(items), _GRID_MUTATION_CHUNK):
                out.extend(self.add_adgroups(items[i:i + _GRID_MUTATION_CHUNK]))
                time.sleep(0.15)
            return out
        j = self._mutate("AddUnifiedAdGroups", _ADD_GROUPS_Q, {"unifiedAddInput": items})
        res = (j.get("data") or {}).get("addUnifiedAdGroups") or {}
        vr = res.get("validationResult") or {}
        if vr.get("errors"):
            raise GridCreateError(f"AddUnifiedAdGroups validation: {str(vr['errors'])[:240]}")
        out: list[int | None] = []
        for a in (res.get("addedAdGroupItems") or []):
            try:
                out.append(int(a["adGroupId"]))
            except (TypeError, ValueError, KeyError):
                out.append(None)
        return out

    def add_keywords(self, items: list[dict]) -> list[dict]:
        """AddKeywords через Grid (без баллов). items: [{adGroupId, keyword}]. → addedItems.

        ЕДИНСТВЕННЫЙ источник ключей: build_adgroup передаёт keywords=[], а фразы льются этой
        мутацией. Раньше keywords дублировались в спеке группы — Grid создавал их ДВАЖДЫ для
        групп <~140 ключей (крупные AddUnifiedAdGroups keywords игнорирует, отсюда была иллюзия
        «молча игнорирует»). addKeywords работает для ЕПК и любого объёма (проверено live).
        Пропускает ---autotargeting спецключ (он живёт в relevanceMatch, не в keywords).
        """
        clean = [
            {"adGroupId": str(it.get("adGroupId") or ""), "keyword": str(it.get("keyword") or "")}
            for it in (items or [])
            if str(it.get("keyword") or "").strip() and not str(it.get("keyword") or "").startswith("---")
               and str(it.get("adGroupId") or "").strip()
        ]
        if not clean:
            return []
        if len(clean) > 1000:
            out: list[dict] = []
            for i in range(0, len(clean), 1000):
                out.extend(self.add_keywords(clean[i:i + 1000]))
                time.sleep(0.1)
            return out
        j = self._mutate("AddKeywords", _ADD_KEYWORDS_Q, {"input": {"addItems": clean}})
        res = (j.get("data") or {}).get("addKeywords") or {}
        return res.get("addedItems") or []

    def _read_ads_agid_map(self, campaign_id: int) -> dict[str, int]:
        """Read-back adGroupId→adId для кампании после add_ads.

        Нужен когда Grid возвращает addedAds КОРОЧЕ входного списка: упавшие объявления
        пропускаются в ответе без null-заглушки (тот же класс сдвига, что X35↔X40 у групп,
        ревью 03.07 #5/#21) — картинки/цены/видео группы N уезжали на объявление группы N+1.
        → dict{adGroupId(str): adId(int)} или {} при ошибке (best-effort; у комбинаторных
        tp1/tp2 — одно объявление на группу)."""
        q = ("query AdsAgid($login:String!,$inp:GdAdsContainerInput!){"
             "client(searchBy:{login:$login}){ads(input:$inp){rowset{id adGroupId}}}}")
        try:
            j = self._mutate("AdsAgid", q, {
                "login": self.login,
                "inp": {"filter": {"campaignIdIn": [str(campaign_id)]},
                        "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [],
                                             "useCampaignGoalIds": True},
                        "limitOffset": {"limit": 10000, "offset": 0},
                        "orderBy": [{"order": "ASC", "field": "ID"}]},
            })
            rows = (((j.get("data") or {}).get("client") or {}).get("ads") or {}).get("rowset") or []
            out: dict[str, int] = {}
            for row in rows:
                agid = str(row.get("adGroupId") or "")
                try:
                    aid = int(row.get("id") or 0)
                except (TypeError, ValueError):
                    aid = 0
                if agid and aid and agid not in out:   # первое объявление группы (комбинаторное)
                    out[agid] = aid
            return out
        except Exception:  # noqa: BLE001
            return {}

    def _read_adgroup_name_to_id(self, campaign_id: int) -> dict[str, int]:
        """Read-back name→adGroupId для кампании после add_adgroups.

        Нужен когда Grid возвращает addedAdGroupItems КОРОЧЕ входного списка: упавшие группы
        просто пропускаются в ответе (без null-заглушки), и zip(use_groups, ag_ids) смещает
        маппинг — ключи группы N попадают в adGroupId группы N+1. Читаем актуальный name→id и
        перестраиваем список строго по именам. → dict{name: id} или {} при ошибке (best-effort)."""
        try:
            j = self._mutate("AdGroupNames", _ADGROUP_NAMES_Q, {
                "login": self.login,
                "inp": {"filter": {"campaignIdIn": [str(campaign_id)]},
                        "limitOffset": {"limit": 10000, "offset": 0}},
            })
            rows = (((j.get("data") or {}).get("client") or {}).get("adGroups") or {}).get("rowset") or []
            out: dict[str, int] = {}
            for row in rows:
                name = str(row.get("name") or "")
                try:
                    gid = int(row.get("id") or 0)
                except (TypeError, ValueError):
                    gid = 0
                if name and gid:
                    out[name] = gid
            return out
        except Exception:  # noqa: BLE001
            return {}

    def add_ads(self, items: list[dict], *, save_draft: bool = True) -> list[int | None]:
        """AddAdaptiveTextAds → список id объявлений (в порядке items; None для упавших)."""
        if not items:
            return []
        if len(items) > _GRID_MUTATION_CHUNK:
            out: list[int | None] = []
            for i in range(0, len(items), _GRID_MUTATION_CHUNK):
                out.extend(self.add_ads(items[i:i + _GRID_MUTATION_CHUNK], save_draft=save_draft))
                time.sleep(0.15)
            return out
        j = self._mutate("AddAdaptiveTextAds", _ADD_ADS_Q,
                         {"addInput": {"adAddItems": items, "saveDraft": bool(save_draft)}})
        res = (j.get("data") or {}).get("addAdaptiveTextAds") or {}
        vr = res.get("validationResult") or {}
        if vr.get("errors"):
            raise GridCreateError(f"AddAdaptiveTextAds validation: {str(vr['errors'])[:240]}")
        out: list[int | None] = []
        for a in (res.get("addedAds") or []):
            try:
                out.append(int(a["id"]))
            except (TypeError, ValueError, KeyError):
                out.append(None)
        return out

    def add_shopping_ads(self, items: list[dict], *, save_draft: bool = True) -> list[int | None]:
        """AddShoppingAds → список id товарных объявлений (Товарная галерея tp3/tp5). items —
        [{adGroupId, feedId, bodies, ...}] из build_shopping_ad. None для упавших."""
        if not items:
            return []
        j = self._mutate("AddShoppingAds", _ADD_SHOPPING_ADS_Q,
                         {"input": {"adAddItems": items, "saveDraft": bool(save_draft)}})
        res = (j.get("data") or {}).get("addShoppingAds") or {}
        vr = res.get("validationResult") or {}
        vr_errors = vr.get("errors") or []
        if vr_errors:
            # Фид в ERROR-состоянии: Директ возвращает FEED_NOT_EXIST в validationResult.
            # Retry без feedId — товарка без фида лучше, чем падение всей кампании.
            has_feed_error = any("FEED_NOT_EXIST" in str(e.get("code") or "") for e in vr_errors)
            if has_feed_error:
                retry_items = [{k: v for k, v in it.items() if k != "feedId"} for it in items]
                j2 = self._mutate("AddShoppingAds", _ADD_SHOPPING_ADS_Q,
                                  {"input": {"adAddItems": retry_items, "saveDraft": bool(save_draft)}})
                res2 = (j2.get("data") or {}).get("addShoppingAds") or {}
                vr2 = res2.get("validationResult") or {}
                if vr2.get("errors"):
                    raise GridCreateError(f"AddShoppingAds validation(no-feed retry): {str(vr2['errors'])[:240]}")
                vr_errors = []   # retry прошёл — сбрасываем ошибки, берём результат
                res = res2
            else:
                raise GridCreateError(f"AddShoppingAds validation: {str(vr_errors)[:240]}")
        out: list[int | None] = []
        for a in (res.get("addedAds") or []):
            try:
                out.append(int(a["id"]))
            except (TypeError, ValueError, KeyError):
                out.append(None)
        return out

    def delete_campaigns(self, campaign_ids: list) -> dict:
        """deleteCampaigns(input:{campaignIds:[…]}) — удаление кампаний ПО КУКЕ (без баллов v5).
        Работает для ЛЮБЫХ типов, включая ЕПК (GdUnifiedCampaign), невидимые/недоступные v5 при 152.
        → {"deleted": [ids], "errors": [...]}. Бросает GridCreateError на транспортной ошибке."""
        ids = [int(c) for c in (campaign_ids or []) if str(c).strip()]
        if not ids:
            return {"deleted": [], "errors": []}
        j = self._mutate("DeleteCampaigns", _DELETE_CAMPAIGNS_Q,
                         {"input": {"campaignIds": ids}})
        res = (j.get("data") or {}).get("deleteCampaigns") or {}
        vr = res.get("validationResult") or {}
        errs = list(vr.get("errors") or [])
        # Удалёнными считаем все, по которым нет ошибки валидации (Grid возвращает per-id ошибки в errors[].path).
        bad = set()
        for e in errs:
            for p in (e.get("path") or []):
                if str(p).isdigit():
                    bad.add(int(p))
        deleted = [i for idx, i in enumerate(ids) if idx not in bad]
        return {"deleted": deleted, "errors": errs}


# ── Сборщики payload'ов (наш спек → формат Grid) ────────────────────────────
_PLATFORMS_OFF = {k: False for k in (
    "gallery", "network", "search", "telegram", "maxMessenger", "taxi", "pillar",
    "cityBusDisplay", "showcaseScreen", "mediafacade", "supersite", "billboard",
    "cityboard", "cityformat", "organic", "serpGeoWizard", "yandexMaps")}


def build_unified_campaign(*, name: str, counter_id: int, goal_id: int, cpa: int,
                           weekly_budget: int, start_date: str, href: str = "",
                           network: bool = True, search: bool = False,
                           gallery: bool = False, organic: bool = False,
                           pay_for_conversion: bool = False, time_zone: str = "130",
                           email: str = "", bid_modifiers: dict | None = None,
                           placement_types: list | None = None,
                           disabled_places: list | None = None) -> dict:
    """Собрать ПОЛНЫЙ GdUnifiedCampaign (48 полей) для AddCampaigns (реверс из HAR14).
    network=True → РСЯ (tp1); search=True → Поиск; gallery+organic → Товарная галерея (tp3/tp5, HAR17).
    Стратегия AUTOBUDGET_AVG_CPA (целевой CPA).
    bid_modifiers — корректировки ставок (HAR21): ставятся прямо в AddCampaigns (campaignId-плейсхолдер
    «9999999» в объекте). Так корректировки попадают на куки-пути БЕЗ баллов для ВСЕХ tp1–tp5."""
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
        "contextLimit": 100, "dayBudget": "0",
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
        "minusKeywords": [], "name": name[:255],
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
                  autotargeting: bool = True, autotargeting_profile: str = "") -> dict:
    """Собрать GdAddUnifiedAdGroupItem: группа + ключи (phrase) + минус-слова + интерес-таргетинг."""
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

    item = {
        "campaignId": str(campaign_id), "name": (name or "группа")[:255],
        "regionIds": [int(r) for r in (region_ids or []) if str(r).lstrip("-").isdigit()] or [225],
        "hyperGeoId": None, "hyperlocalGeoSegments": None, "audienceTargeting": "ALL_AUDIENCE",
        "adGroupMinusKeywords": [str(m) for m in (minus_keywords or [])][:100],
        "keywords": kw, "libraryMinusKeywordsIds": [],
        # Цель-ретаргетинг на группе НЕ ставим: для РСЯ/Поиска (tp1/tp2/tp4) и Товарной галереи
        # (tp3/tp5) таргетинг = ключи + автотаргет (relevanceMatch). Цель живёт в СТРАТЕГИИ кампании.
        # Гол в conditionRules требует поле time (RetargetingDefectIds.REQUIRED_TIME_FOR_GOAL_OR_SEGMENT) —
        # как в HAR17/v501-пути, оставляем retargetingCondition=null.
        "retargetingCondition": None,
        "caRetargetingCondition": None, "retargetings": [], "searchRetargetings": [],
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


_AC_GROUP_CAP = 150        # макс. групп на кампанию за проход (как в v501-пути)
_GRID_MUTATION_CHUNK = 50  # приватный Grid нестабилен на пачках ~150: режем bulk-мутации
_KW_BUDGET = 9800          # консервативный лимит ключей/кампанию (Яндекс: 10 000)
_KW_MAX_PER_GROUP = 200    # верхний предохранитель на группу


def _alloc_kw_caps(groups: list) -> list[int]:
    """Two-pass keyword budget allocation (Яндекс лимит: ≤10 000 ключей/кампанию, берём 9800).

    Pass-1: base_cap = min(200, 9800 // n) — равномерная «точка старта».
    Pass-2: остаток бюджета (от групп, не дошедших до base_cap) перераспределяется между
            «плотными» группами (len > base_cap), увеличивая их cap.
    Гарантирует sum(caps[i]) ≤ 9800 при любом наборе групп.

    Примеры:
      150 × 200 kw  → base_cap=65, sum=9800 (50 групп по 66, 100 по 65).
      149×3 + 1×3000 → base_cap=65, крупная получает min(3000, 9288+65)=3000, sum=3447.
      10 × любые   → base_cap=200, урезания нет.
    """
    n = len(groups)
    if n == 0:
        return []
    base_cap = min(_KW_MAX_PER_GROUP, _KW_BUDGET // n)
    kw_counts = [len(g.get("keywords") or []) for g in groups]
    caps = [min(cnt, base_cap) for cnt in kw_counts]
    remainder = _KW_BUDGET - sum(caps)
    if remainder > 0:
        capped_idxs = [i for i, cnt in enumerate(kw_counts) if cnt > base_cap]
        if capped_idxs:
            extra = remainder // len(capped_idxs)
            leftover = remainder - extra * len(capped_idxs)
            for i in capped_idxs:
                caps[i] = min(kw_counts[i], base_cap + extra)
            for i in capped_idxs[:leftover]:
                caps[i] = min(kw_counts[i], caps[i] + 1)
    return caps


def create_full(login: str, *, campaign_spec: dict, groups: list, region_ids: list,
                href: str, goal_id: int = 0, autotargeting: bool = True,
                price_map: dict | None = None, brand_price_fn=None) -> dict:
    """Создать кампанию + группы (+ключи) + комбинаторные объявления (+adPrice) ПО КУКЕ за один вызов.

    campaign_spec — kwargs для build_unified_campaign (name/counter_id/goal_id/cpa/weekly_budget/...).
    groups — [{name, keywords:[...], minus:[...], titles:[...], texts:[...], image_hashes:[...],
              href, brand}]. price_map + brand_price_fn(price_map, brand) → (current, old) для adPrice.
    → {campaign_id, groups: N, ads: N, prices_set: N, errors: [...]}.
    """
    rep = {"campaign_id": None, "groups": 0, "ads": 0, "keywords": 0, "ad_ids": [],
           "adgroup_ids": [], "prices_set": 0, "errors": []}
    cl = GridCreateClient(login)
    cl._bootstrap_csrf()
    try:
        cid = cl.add_campaign(build_unified_campaign(href=href, **campaign_spec))
    except GridCreateError as e:
        rep["errors"].append(f"кампания(куки): {str(e)[:200]}")
        return rep
    rep["campaign_id"] = cid

    use_groups = groups[:_AC_GROUP_CAP]
    if len(groups) > _AC_GROUP_CAP:
        rep["deferred"] = len(groups) - _AC_GROUP_CAP

    # Two-pass аллокация ключей: _alloc_kw_caps даёт per-group cap при суммарном бюджете ≤9800.
    # Плотные группы получают максимум оставшегося бюджета (баги #3/#9 code-review).
    kw_caps = _alloc_kw_caps(use_groups)

    # Группы пачкой (AddUnifiedAdGroups принимает список) — adGroupId выровнен по порядку.
    search_only = bool(campaign_spec.get("search")) and not bool(campaign_spec.get("network"))
    at_profile = "search_tp2" if search_only else ""
    # Группы БЕЗ ключей в поле AddUnifiedAdGroups. Раньше keywords передавались и сюда, и в
    # отдельный AddKeywords ниже — Grid СОЗДАВАЛ их ДВАЖДЫ для групп <~140 ключей (точные
    # дубли фраз, каждая со своим keywordId). Единственный источник ключей — AddKeywords ниже
    # (проверен на всех объёмах, вкл. крупные группы, где AddUnifiedAdGroups keywords игнорирует).
    g_items = [build_adgroup(campaign_id=cid, name=g.get("name") or "группа",
                             region_ids=region_ids, keywords=[],
                             minus_keywords=g.get("minus") or [], goal_id=goal_id,
                             autotargeting=autotargeting,
                             autotargeting_profile=at_profile) for g in use_groups]
    try:
        ag_ids = cl.add_adgroups(g_items)
    except GridCreateError as e:
        rep["errors"].append(f"группы(куки): {str(e)[:200]}")
        return rep
    rep["groups"] = sum(1 for x in ag_ids if x)
    rep["adgroup_ids"] = [int(x) if x else None for x in ag_ids]

    # Защита от позиционного сдвига: Grid пропускает упавшие группы в addedAdGroupItems
    # (не возвращает null-заглушку) → ag_ids короче use_groups → zip смещает маппинг.
    # При несовпадении длин делаем read-back name→id и выравниваем строго по имени группы.
    if len(ag_ids) != len(use_groups):
        _name_to_id = cl._read_adgroup_name_to_id(cid)
        if _name_to_id:
            ag_ids = [_name_to_id.get(g.get("name") or "") for g in use_groups]
        else:
            ag_ids = list(ag_ids) + [None] * (len(use_groups) - len(ag_ids))
            rep["errors"].append("позиционный сдвиг: read-back недоступен, ключи могут быть смещены")
        rep["adgroup_ids"] = [int(x) if x else None for x in ag_ids]

    # Ключи ЕДИНСТВЕННЫМ путём — отдельным AddKeywords (в build_adgroup keywords=[], иначе дубли).
    _kw_items = []
    for g, agid, cap in zip(use_groups, ag_ids, kw_caps):
        if not agid:
            continue
        for k in (g.get("keywords") or [])[:cap]:
            if str(k).strip() and not str(k).startswith("---"):
                _kw_items.append({"adGroupId": str(agid), "keyword": str(k)})
    if _kw_items:
        try:
            rep["keywords"] = len(cl.add_keywords(_kw_items))
        except Exception as e:  # noqa: BLE001 — группы созданы; но ключи теперь ЕДИНСТВЕННЫМ путём,
            # молчать нельзя: сбой = кампания без ключей (не будет показываться). Выносим в rep["errors"],
            # чтобы вызывающий (ЕПК-копир/feed/tp1/repair) увидел провал по стандартной проверке errors.
            rep["errors"].append(f"ключи(AddKeywords): {str(e)[:200]}")

    # Объявления пачкой + adPrice по бренду группы.
    ad_items, ad_brand = [], []
    for g, agid in zip(use_groups, ag_ids):
        if not agid:
            continue
        price = None
        if price_map and brand_price_fn:
            # seg группы ('Марки' → МИН цена по марке; иначе цена модели) — передаём третьим арг.
            # _group_ad_price принимает (prices, brand, seg); старый _ad_price_for_brand игнорил seg.
            try:
                cur, old = brand_price_fn(price_map, g.get("brand") or g.get("name") or "", g.get("seg") or "")
            except TypeError:                       # совместимость со старой 2-арг функцией
                cur, old = brand_price_fn(price_map, g.get("brand") or g.get("name") or "")
            if cur:
                # Старая цена только из фида (old_safe=0 → поле пустое, без зачёркнутой).
                old_safe = _safe_old_price(cur, old)
                price = {"price": str(cur), "priceOld": (str(old_safe) if old_safe else ""),
                         "prefix": "FROM", "currency": "RUB"}   # «от X» (HAR 29)
        # Common ct image safety is enforced before this point: callers may pass only
        # safe Manual/ct0000 hashes for common groups, never M3/feed/model hashes.
        image_hashes = g.get("image_hashes") or []
        ad_items.append(build_ad(adgroup_id=agid, href=g.get("href") or href,
                                 titles=g.get("titles") or [], bodies=g.get("texts") or [],
                                 image_hashes=image_hashes, ad_price=price))
        if price:
            rep["prices_set"] += 1
    try:
        a_ids = cl.add_ads(ad_items)
        rep["ads"] = sum(1 for x in a_ids if x)
        # КОНТРАКТ (ревью 03.07 #5/#21): ad_ids СТРОГО 1:1 с ag_ids/группами, None для групп
        # без agid и упавших объявлений. Компакт-список смещал zip(ad_ids, groups) у потребителей
        # (картинки/цены/видео чужой группе — класс X35↔X40). При расхождении длин ответа Grid —
        # read-back adGroupId→adId.
        rep["ad_ids"] = _aligned_ad_ids(cl, cid, ad_items, a_ids, ag_ids)
    except GridCreateError as e:
        rep["errors"].append(f"объявления(куки): {str(e)[:200]}")
    return rep


def _aligned_ad_ids(cl, campaign_id: int, ad_items: list, a_ids: list, ag_ids: list) -> list:
    """ad_ids СТРОГО 1:1 с ag_ids (None для пропусков) — общий гард сдвига create_full /
    add_text_content_to_existing (ревью 03.07 #5/#21, класс X35↔X40).

    add_ads должен отдавать позиционный список, но Grid пропускает упавшие объявления в
    addedAds без null-заглушек → при len-расхождении восстанавливаем соответствие
    read-back'ом adGroupId→adId; иначе матчим по adGroupId отправленных items."""
    sent = [str(it.get("adGroupId") or "") for it in (ad_items or [])]
    ids = list(a_ids or [])
    if len(ids) != len(sent):
        ag2ad = cl._read_ads_agid_map(int(campaign_id or 0))
        ids = [ag2ad.get(s) for s in sent]
    by_agid = {s: i for s, i in zip(sent, ids) if s and i}
    return [by_agid.get(str(a)) if a else None for a in (ag_ids or [])]


def add_text_content_to_existing(login: str, *, campaign_id: int, groups: list,
                                 region_ids: list, href: str, goal_id: int = 0,
                                 autotargeting: bool = True, search_only: bool = False,
                                 price_map: dict | None = None, brand_price_fn=None) -> dict:
    """Добавить группы + adaptive text ads в УЖЕ существующую кампанию через Grid/cookie.

    Используется repair-gate для пустых tp2/tp4 черновиков: кампанию не пересоздаём и не тратим
    Direct API units, только добиваем missing content.
    """
    rep = {"campaign_id": int(campaign_id or 0), "groups": 0, "ads": 0, "keywords": 0,
           "ad_ids": [], "adgroup_ids": [], "prices_set": 0, "errors": []}
    cid = rep["campaign_id"]
    if not cid:
        rep["errors"].append("content-repair: нет campaign_id")
        return rep
    use_groups = list(groups or [])[:_AC_GROUP_CAP]
    if not use_groups:
        rep["errors"].append("content-repair: нет групп для добавления")
        return rep
    if len(groups or []) > _AC_GROUP_CAP:
        rep["deferred"] = len(groups or []) - _AC_GROUP_CAP

    cl = GridCreateClient(login)
    cl._bootstrap_csrf()
    at_profile = "search_tp2" if search_only else ""
    # Консервативный kw_cap: не знаем сколько ключей уже в кампании → аллоцируем только по
    # добавляемым группам (worst-case). Это та же формула что в create_full.
    kw_caps = _alloc_kw_caps(use_groups)
    # keywords=[] в группе: единственный источник ключей — AddKeywords ниже (иначе Grid создаёт
    # дубли для групп <~140 ключей — то же, что чинилось в create_full).
    g_items = [build_adgroup(campaign_id=cid, name=g.get("name") or "группа",
                             region_ids=region_ids, keywords=[],
                             minus_keywords=g.get("minus") or [], goal_id=goal_id,
                             autotargeting=autotargeting,
                             autotargeting_profile=at_profile) for g in use_groups]
    try:
        ag_ids = cl.add_adgroups(g_items)
    except GridCreateError as e:
        rep["errors"].append(f"группы(куки): {str(e)[:200]}")
        return rep
    rep["groups"] = sum(1 for x in ag_ids if x)
    rep["adgroup_ids"] = [int(x) if x else None for x in ag_ids]

    # Защита от позиционного сдвига (идентично create_full).
    if len(ag_ids) != len(use_groups):
        _name_to_id = cl._read_adgroup_name_to_id(cid)
        if _name_to_id:
            ag_ids = [_name_to_id.get(g.get("name") or "") for g in use_groups]
        else:
            ag_ids = list(ag_ids) + [None] * (len(use_groups) - len(ag_ids))
            rep["errors"].append("позиционный сдвиг: read-back недоступен, ключи могут быть смещены")
        rep["adgroup_ids"] = [int(x) if x else None for x in ag_ids]

    # Ключи ЕДИНСТВЕННЫМ путём — отдельным AddKeywords (в build_adgroup keywords=[], иначе дубли).
    _kw_items_tc = []
    for g, agid, cap in zip(use_groups, ag_ids, kw_caps):
        if not agid:
            continue
        for k in (g.get("keywords") or [])[:cap]:
            if str(k).strip() and not str(k).startswith("---"):
                _kw_items_tc.append({"adGroupId": str(agid), "keyword": str(k)})
    if _kw_items_tc:
        try:
            rep["keywords"] = len(cl.add_keywords(_kw_items_tc))
        except Exception as e:  # noqa: BLE001 — группы созданы; ключи теперь ЕДИНСТВЕННЫМ путём,
            # сбой = группы без ключей. Выносим в rep["errors"] (repair-gate проверяет not errors).
            rep["errors"].append(f"ключи(AddKeywords): {str(e)[:200]}")

    ad_items = []
    for g, agid in zip(use_groups, ag_ids):
        if not agid:
            continue
        price = None
        if price_map and brand_price_fn:
            try:
                cur, old = brand_price_fn(price_map, g.get("brand") or g.get("name") or "", g.get("seg") or "")
            except TypeError:
                cur, old = brand_price_fn(price_map, g.get("brand") or g.get("name") or "")
            if cur:
                old_safe = _safe_old_price(cur, old)
                price = {"price": str(cur), "priceOld": (str(old_safe) if old_safe else ""),
                         "prefix": "FROM", "currency": "RUB"}
        ad_items.append(build_ad(adgroup_id=agid, href=g.get("href") or href,
                                 titles=g.get("titles") or [], bodies=g.get("texts") or [],
                                 image_hashes=(g.get("image_hashes") or []), ad_price=price))
        if price:
            rep["prices_set"] += 1
    try:
        a_ids = cl.add_ads(ad_items)
        rep["ads"] = sum(1 for x in a_ids if x)
        # тот же выровненный контракт 1:1, что в create_full (ревью 03.07 #5/#21)
        rep["ad_ids"] = _aligned_ad_ids(cl, cid, ad_items, a_ids, ag_ids)
    except GridCreateError as e:
        rep["errors"].append(f"объявления(куки): {str(e)[:200]}")
    return rep


def add_shopping_content_to_existing(login: str, *, campaign_id: int, groups: list,
                                     feed_id: int, region_ids: list, body_text: str = "",
                                     goal_id: int = 0) -> dict:
    """Добавить товарные группы + ShoppingAd/ListingAd в существующую tp3/tp5 кампанию.

    ``groups``: [{name, vendor?, model?, collection_id?, listing_name?}].
    Фильтры ShoppingAd ставятся через ``grid_finalize.GridClient.add_shopping_ads``:
    vendor/model для товаров, затем отдельный name-фильтр для ListingAd.
    """
    rep = {"campaign_id": int(campaign_id or 0), "groups": 0, "shopping_ads": 0,
           "listing_ads": 0, "adgroup_ids": [], "shopping_ad_ids": [],
           "listing_ad_ids": [], "listing_name_set": 0, "errors": []}
    cid = rep["campaign_id"]
    fid = int(feed_id or 0)
    use_groups = list(groups or [])[:_AC_GROUP_CAP]
    if not cid:
        rep["errors"].append("shopping-content-repair: нет campaign_id")
        return rep
    if not fid:
        rep["errors"].append("shopping-content-repair: нет feed_id")
        return rep
    if not use_groups:
        rep["errors"].append("shopping-content-repair: нет групп для добавления")
        return rep

    cl = GridCreateClient(login)
    cl._bootstrap_csrf()
    g_items = [build_adgroup(campaign_id=cid, name=g.get("name") or "Товарная галерея",
                             region_ids=region_ids, keywords=[], minus_keywords=[],
                             goal_id=goal_id, autotargeting=True) for g in use_groups]
    try:
        ag_ids = cl.add_adgroups(g_items)
    except GridCreateError as e:
        rep["errors"].append(f"группы(куки): {str(e)[:200]}")
        return rep
    rep["groups"] = sum(1 for x in ag_ids if x)
    rep["adgroup_ids"] = [int(x) if x else None for x in ag_ids]

    shop_items = []
    listing_names = []
    for g, agid in zip(use_groups, ag_ids):
        if not agid:
            continue
        shop_items.append({
            "adgroup_id": int(agid),
            "feed_id": fid,
            "vendor": g.get("vendor"),
            "model": g.get("model") or [],
            "collection_id": g.get("collection_id"),
        })
        listing_names.append(g.get("listing_name"))
    if not shop_items:
        rep["errors"].append("shopping-content-repair: группы не вернули adGroupId")
        return rep

    try:
        from . import grid_finalize as gf

        grid = gf.GridClient(login)
        raw_shop_ids = grid.add_shopping_ads(shop_items) or []
        shop_ids = [int(x) for x in raw_shop_ids if x]
        rep["shopping_ad_ids"] = shop_ids
        rep["shopping_ads"] = len(shop_ids)
        if not shop_ids:
            rep["errors"].append("товарные объявления(куки): Grid вернул 0 ShoppingAd")
            return rep

        text = _trim_clean(str(body_text or ""), 81)
        if text:
            filters_by_ad_id = {}
            for sid, src in zip(shop_ids, shop_items):
                conds = []
                if src.get("vendor"):
                    vv = str(src["vendor"])
                    variants = list(dict.fromkeys([vv, vv.lower(), vv.title()]))
                    import json
                    conds.append({"field": "vendor", "operator": "CONTAINS_ANY",
                                  "stringValue": json.dumps(variants, ensure_ascii=False)})
                if src.get("model"):
                    mvals = src["model"] if isinstance(src["model"], list) else [str(src["model"])]
                    mvals = [str(x) for x in mvals if str(x).strip()]
                    if mvals:
                        import json
                        conds.append({"field": "model", "operator": "CONTAINS_ANY",
                                      "stringValue": json.dumps(mvals, ensure_ascii=False)})
                if not conds and src.get("collection_id"):
                    import json
                    conds.append({"field": "collectionId", "operator": "EQUALS_ANY",
                                  "stringValue": json.dumps([str(src["collection_id"])], ensure_ascii=False)})
                try:                                     # глобальные минус-марки (фид): производитель/модель НЕ содержит
                    from . import create_set_feeds as _csf
                    # per-feed поля: yandex.xml = mark_id/folder_id, YML = vendor/model — дефолт 'vendor'/'model'
                    # на AUTO_RU-фиде 'vendor'/'model' давал UNKNOWN_FIELD и фильтр не вставал (ревью 03.07)
                    _bf = _csf._resolve_feed_field(login, fid, "brand") or "vendor"
                    _mf = _csf._resolve_feed_field(login, fid, "model") or "model"
                    conds.extend(_csf._minus_marks_grid_conditions(brand_field=_bf, model_field=_mf))
                except Exception:  # noqa: BLE001
                    pass
                if conds:
                    filters_by_ad_id[int(sid)] = {"tab": "CONDITION", "conditions": conds}
            grid.set_default_text(shop_ids, fid, text, filters_by_ad_id=filters_by_ad_id)

        listing_rows = grid.add_listing_ads_by_shopping_ads(shop_ids) or []
        lf_items = []
        for row in listing_rows:
            lid = row.get("id") if isinstance(row, dict) else row
            said = row.get("shoppingAdId") if isinstance(row, dict) else None
            if lid:
                rep["listing_ad_ids"].append(int(lid))
            if lid and said:
                try:
                    idx = shop_ids.index(int(said))
                except (ValueError, TypeError):
                    idx = -1
                if idx >= 0 and idx < len(listing_names) and listing_names[idx]:
                    lf_items.append({"id": lid, "feed_id": fid, "value": listing_names[idx],
                                     "bodies": ([text] if text else [])})
        rep["listing_ads"] = len(rep["listing_ad_ids"])
        if lf_items:
            rep["listing_name_set"] = grid.set_listing_name_filters(lf_items)
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"shopping/listing(куки): {str(e)[:200]}")
    return rep


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
            from . import create_set_feeds as _csf
            _bf = _csf._resolve_feed_field(login, feed_id, "brand") or "vendor"
            _mf = _csf._resolve_feed_field(login, feed_id, "model") or "model"
            conds = _csf._minus_marks_grid_conditions(brand_field=_bf, model_field=_mf)
            if conds:
                item["feedFilter"] = {"tab": "CONDITION", "conditions": conds}
        except Exception:  # noqa: BLE001
            pass
    return item


def create_shopping_full(login: str, *, campaign_spec: dict, group_names: list, feed_id: int,
                         region_ids: list, href: str, body_text: str = "", goal_id: int = 0) -> dict:
    """Создать Товарную галерею (tp3 РСЯ / tp5 Поиск) ПО КУКЕ: кампания (gallery+organic) + группы
    (автотаргет, без ключей) + товарные объявления (ShoppingAd по фиду). → {campaign_id, groups, ads, errors}."""
    rep = {"campaign_id": None, "groups": 0, "ads": 0, "errors": []}
    if not feed_id:
        rep["errors"].append("товарная галерея(куки): нет фида (feed_id)")
        return rep
    cl = GridCreateClient(login)
    cl._bootstrap_csrf()
    try:
        # organic (поисковая органика) валиден ТОЛЬКО с search (tp5). Для tp3 (РСЯ/network) organic
        # запрещён (ORGANIC_PLACEMENT_TYPES_INVALID_COMBINATION) — берём его из campaign_spec.
        cid = cl.add_campaign(build_unified_campaign(href=href, gallery=True, **campaign_spec))
    except GridCreateError as e:
        rep["errors"].append(f"кампания(куки): {str(e)[:200]}")
        return rep
    rep["campaign_id"] = cid
    names = (group_names or ["Товарная галерея"])[:_AC_GROUP_CAP]
    # Группы товарной галереи: автотаргет ВКЛ, БЕЗ ключей (товары из фида) — как в HAR17.
    # tp5 (search=True, network=False) → search_tp2 профиль (Целевые + без упоминания бренда),
    # tp3 (РСЯ, network=True) → "" (все категории + все бренды, без изменений).
    _gc_at_profile = "search_tp2" if (campaign_spec.get("search") and not campaign_spec.get("network")) else ""
    g_items = [build_adgroup(campaign_id=cid, name=(nm or "Товарная галерея"), region_ids=region_ids,
                             keywords=[], minus_keywords=[], goal_id=goal_id, autotargeting=True,
                             autotargeting_profile=_gc_at_profile)
               for nm in names]
    try:
        ag_ids = cl.add_adgroups(g_items)
    except GridCreateError as e:
        rep["errors"].append(f"группы(куки): {str(e)[:200]}")
        return rep
    rep["groups"] = sum(1 for x in ag_ids if x)
    ad_items = [build_shopping_ad(adgroup_id=agid, feed_id=feed_id, body=body_text, login=login)
                for agid in ag_ids if agid]
    try:
        a_ids = cl.add_shopping_ads(ad_items)
        rep["ads"] = sum(1 for x in a_ids if x)
        rep["shopping_ad_ids"] = [int(x) for x in a_ids if x]
    except GridCreateError as e:
        rep["errors"].append(f"товарные объявления(куки): {str(e)[:200]}")
    return rep

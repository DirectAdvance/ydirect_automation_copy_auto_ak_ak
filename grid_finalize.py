"""Grid-докрутка ЕПК (tp1–tp5) — то, что официальный v5 API НЕ умеет, но нужно для
«как боевая»: места показа («Ручная настройка» через placementTypes), наследуемые
ассеты кампании (уточнения/быстрые ссылки), промо, библиотечный минус-набор, инварианты.
Делается через приватный web-api/grid/api на агентских куках (как UAC для tp6/tp7).

ПОРЯДОК ГИБРИДА (строгий):
  1) v5 каркас        — campaign.py: create_unified_campaign + товарные/листинг объявления
  2) Grid-докрутка    — этот модуль: GridClient.finalize(...)
  3) v5-корректировки — apply_corrections(...) ПОСЛЕ Grid (UpdateCampaigns перезаписывает
     bidModifiers целиком — если ставить корректировки до Grid, они слетят).

Реверс-инжиниринг из HAR direct.yandex.ru (2026-06-21/22), проверено live на porg-psm5h7q6.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests
import urllib3

try:                                    # пакетный контекст (blueprint: from . import …)
    from . import campaign as cmc       # USER_AGENT, pick_working_cookie, DirectV501Client
except ImportError:                     # плоский запуск (локальные тесты из direct/)
    import campaign as cmc

urllib3.disable_warnings()

_DIR = Path(__file__).parent
GRID_URL = "https://direct.yandex.ru/web-api/grid/api"
_GRID_MUTATION_CHUNK = 50  # приватный Grid нестабилен на больших пачках add*Ads
_MUTATION = (_DIR / "grid_uc_mutation.graphql").read_text(encoding="utf-8")
_TEMPLATE = json.loads((_DIR / "grid_uc_template.json").read_text(encoding="utf-8"))
_SHOPPING_MUTATION = (_DIR / "grid_shopping_mutation.graphql").read_text(encoding="utf-8")

# Транзиентные серверные ошибки Яндекса (top-level errors, НЕ валидация) — ретраим с backoff.
_TRANSIENT_ERR = ("внутренняя ошибка сервера", "internal server error", "internal error",
                  "timeout", "timed out", "temporarily", "try again", "503", "502", "504")

# READ: облегчённый GroupsForEdit (реверс HAR GroupsForEdit) — только поля, нужные для round-trip
# UpdateUnifiedAdGroups + идемпотентность (kw-count/relevanceMatch) + safety (bidModifiers/retargetings).
# Фильтр по campaignIdIn (как в grid_read.campaign_content_counts) — можно читать пачкой кампаний.
_GROUPS_FOR_EDIT_LITE_Q = (
    "query GroupsForEditLite($login:String!,$agInp:GdAdGroupsContainerInput!,"
    "$scInp:GdShowConditionsContainerInput!,$rtInp:GdRetargetingsContainerInput!){"
    "reqId:getReqId client(searchBy:{login:$login}){"
    "adGroups(input:$agInp){rowset{__typename id name type "
    "regionsInfo{regionIds} minusKeywords libraryMinusKeywordsPacks{id} hyperGeoId "
    "hyperlocalGeoSegments{name segmentType radius points{latitude longitude}} "
    "campaign{__typename id name type} bidModifiers{id} "
    "...on GdUnifiedAdGroup{audienceTargeting trackingParams contentLanguage "
    "promoExtensionInheritancePolicy contentTypeShowSettings{usualAdsShowFilter} "
    "inheritableCallouts{policy} inheritableSitelinkSet{policy} offerRetargeting{isActive} "
    "relevanceMatch{id isActive relevanceMatchCategories autotargetingBrandSettings}}}}"
    "showConditions(input:$scInp){rowset{__typename ...on GdKeyword{id keyword adGroupId}}}"
    "retargetings(input:$rtInp){rowset{...on GdRetargeting{adGroupId}}}}}"
)

# WRITE: UpdateUnifiedAdGroups (реверс HAR UpdateUnifiedAdGroups) — ПОЛНАЯ замена полей группы.
_UPDATE_UNIFIED_ADGROUPS_Q = (
    "mutation UpdateUnifiedAdGroups($unifiedUpdateInput:[GdUpdateUnifiedAdGroupItemInput!]!){"
    "reqId:getReqId updateUnifiedAdGroups(input:{updateItems:$unifiedUpdateInput}){"
    "updatedAdGroupItems{adGroupId}"
    "validationResult{errors{code params path}warnings{code params path}}}}"
)

# placementTypes (явный список → интерфейс показывает «Ручная настройка», не пресет «Поиск»).
# tp5 «Поиск + Товарная галерея»: продвижение в выдаче + товарная галерея на поиске.
PLACEMENTS_TP5 = ["SEARCH_PAGE", "ADV_GALLERY"]
# Платформы канала (поиск-only, без РСЯ/Карт/орг-списка) — согласовано с placementTypes.
PLATFORMS_SEARCH = {
    "gallery": True, "search": True, "organic": True, "network": False,
    "yandexMaps": False, "serpGeoWizard": False, "telegram": False, "maxMessenger": False,
    "taxi": False, "pillar": False, "cityBusDisplay": False, "showcaseScreen": False,
    "mediafacade": False, "supersite": False, "billboard": False, "cityboard": False,
    "cityformat": False,
}


class GridFinalizeError(RuntimeError):
    pass


class GridClient:
    """Тонкий клиент web-api/grid/api на агентских куках (CSRF добирается сам)."""

    def __init__(self, login: str, cookie: str | None = None):
        self.login = login
        self.cookie = cookie or cmc.pick_working_cookie(login)
        self.csrf: str | None = None
        self.sess = requests.Session()
        self.sess.verify = False

    def _post(self, op: str, query: str, variables: dict) -> requests.Response:
        headers = {
            "Cookie": self.cookie, "dna-operation-name": op, "x-direct-api": "1",
            "x-detected-locale": "ru", "Content-Type": "application/json",
            "User-Agent": cmc.USER_AGENT, "Origin": "https://direct.yandex.ru",
            "Referer": f"https://direct.yandex.ru/dna/grid/campaigns?ulogin={self.login}",
        }
        if self.csrf:
            headers["x-csrf-token"] = self.csrf
        url = f"{GRID_URL}?operationName={op}&ulogin={self.login}"
        r = self.sess.post(url, json={"operationName": op, "query": query, "variables": variables},
                           headers=headers, timeout=40)
        m = re.search(r"_direct_csrf_token=([^;,\s]+)", r.headers.get("Set-Cookie", ""))
        tok = r.cookies.get("_direct_csrf_token") or (m.group(1) if m else None)
        if tok:
            self.csrf = tok
        return r

    def _bootstrap_csrf(self) -> None:
        q = ("query Callouts($login:String!){callouts(input:{searchBy:{login:$login}"
             "filter:{deleted:false}}){id}}")
        r = self._post("Callouts", q, {"login": self.login})
        if r.status_code == 403:                       # первый POST даёт CSRF → ретрай
            self._post("Callouts", q, {"login": self.login})

    def finalize(self, campaign_id: int, *, name: str, goal_id: int,
                 cpa_rub: int | float, weekly_rub: int | float, counter_ids: list[int],
                 pay_for_conversion: bool, placement_types: list[str] | None = None,
                 platforms: dict | None = None, callout_ids: list | None = None,
                 sitelink_set_id: int | None = None, promo_id: int | None = None,
                 minus_set_ids: list[int] | None = None,
                 notification_email: str | None = None) -> list:
        """Докрутить ЕПК (full-object UpdateCampaigns). НЕ трогает bidModifiers (={} —
        корректировки ставит apply_corrections ПОСЛЕ). Бросает при validationResult.errors.

        cpa_rub / weekly_rub — в РУБЛЯХ (Grid strategyData оперирует рублями строкой, НЕ микро).
        placement_types: явный список → «Ручная настройка» (по умолч. PLACEMENTS_TP5).
        """
        self._bootstrap_csrf()
        uc = json.loads(json.dumps(_TEMPLATE))         # deepcopy шаблона
        uc["id"] = str(campaign_id)
        uc["name"] = name
        uc["strategyId"] = None                        # пересоберётся по strategyData
        uc["metrikaCounters"] = [int(c) for c in (counter_ids or [])]
        uc["biddingStategyWithPlatforms"]["platforms"] = dict(platforms or PLATFORMS_SEARCH)
        uc["biddingStategyWithPlatforms"]["strategyData"] = {
            "goalId": str(goal_id), "avgCpa": str(int(cpa_rub)), "sum": str(int(weekly_rub)),
            "budgetType": "WEEKLY", "payForConversion": bool(pay_for_conversion),
            "payForShows": False, "isExplorationBudgetValueCustom": None,
            "minExplorationBudget": None,
        }
        # tp5 «Места показа» (HAR20 direct.yandex.ru.20har 2026-06-24): placementTypes=null +
        # платформы gallery+search+organic (галерея на поиске, продвижение в выдаче, динамические
        # места), serpGeoWizard/yandexMaps/network=false (список организаций и РСЯ выключены).
        # placement_types передан явно (старое «Ручная настройка») → шлём список; иначе — null (HAR20).
        uc["placementTypes"] = list(placement_types) if placement_types else None
        uc["inheritableCallouts"] = {"calloutIds": [str(i) for i in (callout_ids or [])]}
        uc["inheritableSitelinkSet"] = {"sitelinkSetId": str(sitelink_set_id) if sitelink_set_id else None}
        uc["promoExtensionId"] = str(promo_id) if promo_id else None
        uc["libraryMinusKeywordsIds"] = [str(i) for i in (minus_set_ids or [])]
        uc["bidModifiers"] = {}                         # корректировки — v5-ом ПОСЛЕ
        # инварианты блек-листа
        uc["isAlternativeTextsEnabled"] = False         # персонализация ВЫКЛ
        uc["hasSiteMonitoring"] = True                  # мониторинг сайта ВКЛ
        uc["hasExtendedGeoTargeting"] = False           # расш.гео ВЫКЛ
        # «Карты и список организаций» / «Организация из Я.Бизнеса» — НЕ включаем:
        # без организации Директ ругается «Без организации не получится продвигаться в Картах».
        # Шаблон по умолчанию шлёт enableCompanyInfo=True → площадка «Карты» отмечалась сама.
        uc["enableCompanyInfo"] = False
        pf = uc["biddingStategyWithPlatforms"]["platforms"]
        pf["yandexMaps"] = False                        # Карты — выключены на уровне площадок
        pf["serpGeoWizard"] = False                     # гео-колдунщик (список организаций) — выкл
        uc["isRecommendationsManagementEnabled"] = False  # «Директ помогает» ВЫКЛ
        uc["isPriceRecommendationsManagementEnabled"] = False
        if notification_email:
            uc.setdefault("notification", {}).setdefault("emailSettings", {})["email"] = notification_email
        r = self._post("UpdateCampaigns", _MUTATION,
                       {"input": {"campaignUpdateItems": [{"unifiedCampaign": uc}]}, "login": self.login})
        data = r.json()
        res = (data.get("data") or {}).get("updateCampaigns") or {}
        vr = res.get("validationResult") or {}
        if data.get("errors") or vr.get("errors"):
            raise GridFinalizeError(
                "Grid finalize: " + json.dumps(data.get("errors") or vr.get("errors"),
                                               ensure_ascii=False)[:500])
        return res.get("updatedCampaigns") or []

    # ── Grid-ассеты (без баллов v5) ────────────────────────────────────────────

    def get_callouts(self) -> dict[str, int]:
        """Список уточнений аккаунта через Grid (БЕЗ баллов) → {текст: id}.
        Реверс HAR23/entry290: query Callouts. Используется как fallback при 152."""
        self._bootstrap_csrf()
        q = ("query Callouts($login:String!){callouts(input:{searchBy:{login:$login}"
             "filter:{deleted:false}}){id text}}")
        r = self._post("Callouts", q, {"login": self.login})
        data = r.json()
        rows = ((data.get("data") or {}).get("callouts") or [])
        return {row["text"]: int(row["id"]) for row in rows if row.get("text") and row.get("id")}

    def add_callouts(self, texts: list[str]) -> dict[str, int]:
        """Создать уточнения через Grid (БЕЗ баллов) → {текст: id}.
        Сначала читаем существующие (get_callouts) — дедуп. Только новые тексты создаём.
        Mutation AddCallouts — схема аналогична AddSitelinkSets (HAR23/entry262).
        Если приватная схема не поддерживает AddCallouts, безопасно возвращаем только
        существующие уточнения: создание кампаний не должно падать из-за optional asset.
        Лимит ≤25 симв. на текст должен быть выполнен на стороне вызывающего."""
        existing = self.get_callouts()
        to_create = [t for t in texts if t and t not in existing]
        if not to_create:
            return {t: existing[t] for t in texts if t in existing}
        self._bootstrap_csrf()
        q = ("mutation AddCallouts($input:GdAddCalloutsInput!$login:String!){"
             "reqId:getReqId addCallouts(input:$input){"
             "addedCallouts{id __typename}"
             "validationResult{errors{code params path}warnings{code params path}}}"
             "getClientMutationId(input:{login:$login}){mutationId}}")
        # Создаём батчем
        r = self._post("AddCallouts", q, {
            "login": self.login,
            "input": {"calloutsAddItems": [{"text": t} for t in to_create]},
        })
        data = r.json()
        res = (data.get("data") or {}).get("addCallouts") or {}
        vr = res.get("validationResult") or {}
        if data.get("errors") or vr.get("errors"):
            err_blob = json.dumps(data.get("errors") or vr.get("errors"), ensure_ascii=False)
            if "GdAddCalloutsInput" in err_blob or "Unknown type" in err_blob or "UnknownType" in err_blob:
                return {t: existing[t] for t in texts if t in existing}
            raise GridFinalizeError(
                "Grid add-callouts: " + err_blob[:400])
        added = res.get("addedCallouts") or []
        for text, item in zip(to_create, added):
            if item and item.get("id"):
                existing[text] = int(item["id"])
        return {t: existing[t] for t in texts if t in existing}

    def set_campaign_callouts(self, campaign_ids: list[int], callout_ids: list[int | str]) -> list:
        """Attach inheritable callouts to campaigns through a narrow Grid update.

        This intentionally updates only ``inheritableCallouts``. Full
        ``finalize(...)`` sends a large campaign object and is too broad for a
        repair executor that should not touch strategy, placements, or other
        already verified settings.
        """
        ids = []
        for raw in campaign_ids or []:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                continue
            if cid > 0 and cid not in ids:
                ids.append(cid)
        co_ids = []
        for raw in callout_ids or []:
            try:
                co = int(raw)
            except (TypeError, ValueError):
                continue
            if co > 0 and co not in co_ids:
                co_ids.append(co)
        if not ids or not co_ids:
            return []
        self._bootstrap_csrf()
        q = ("mutation UpdateCampaigns($input:GdUpdateCampaignsInput!$login:String!){"
             "updateCampaigns(input:$input){updatedCampaigns{id}"
             "validationResult{errors{code params path}warnings{code params path}}}"
             "getClientMutationId(input:{login:$login}){mutationId}}")
        items = [{"unifiedCampaign": {
            "id": str(cid),
            "inheritableCallouts": {"calloutIds": [str(i) for i in co_ids]},
        }} for cid in ids]
        r = self._post("UpdateCampaigns", q, {
            "login": self.login,
            "input": {"campaignUpdateItems": items},
        })
        data = r.json()
        res = (data.get("data") or {}).get("updateCampaigns") or {}
        vr = res.get("validationResult") or {}
        if data.get("errors") or vr.get("errors"):
            raise GridFinalizeError(
                "Grid set-callouts: " + json.dumps(
                    data.get("errors") or vr.get("errors"), ensure_ascii=False)[:400])
        return res.get("updatedCampaigns") or []

    def set_campaign_names(self, campaign_names: dict[int, str]) -> list:
        """Rename campaigns through a narrow Grid update.

        Only ``name`` is sent for each campaign. This is used by repair-gate
        after live verification detects that Direct/Grid kept an old or
        truncated name while the expected coder/name is known from create_set.
        """
        items = []
        seen: set[int] = set()
        for raw_id, raw_name in (campaign_names or {}).items():
            try:
                cid = int(raw_id)
            except (TypeError, ValueError):
                continue
            name = str(raw_name or "").strip()
            if cid <= 0 or not name or cid in seen:
                continue
            seen.add(cid)
            items.append({"unifiedCampaign": {"id": str(cid), "name": name}})
        if not items:
            return []
        self._bootstrap_csrf()
        q = ("mutation UpdateCampaigns($input:GdUpdateCampaignsInput!$login:String!){"
             "updateCampaigns(input:$input){updatedCampaigns{id}"
             "validationResult{errors{code params path}warnings{code params path}}}"
             "getClientMutationId(input:{login:$login}){mutationId}}")
        r = self._post("UpdateCampaigns", q, {
            "login": self.login,
            "input": {"campaignUpdateItems": items},
        })
        data = r.json()
        res = (data.get("data") or {}).get("updateCampaigns") or {}
        vr = res.get("validationResult") or {}
        if data.get("errors") or vr.get("errors"):
            raise GridFinalizeError(
                "Grid set-names: " + json.dumps(
                    data.get("errors") or vr.get("errors"), ensure_ascii=False)[:400])
        return res.get("updatedCampaigns") or []

    def add_keywords(self, items: list[dict]) -> list[dict]:
        """Add keyword phrases through Grid (no Direct API units).

        items: [{adgroup_id, keyword, price?}, ...]. ``price`` is in rubles; callers
        that have v5 micros must divide by 1_000_000 before passing it here.
        Returns Grid ``addedItems`` rows.
        """
        clean = []
        for it in items or []:
            phrase = str(it.get("keyword") or it.get("Keyword") or "").strip()
            if not phrase or phrase.startswith("---"):
                continue
            try:
                gid = int(it.get("adgroup_id") or it.get("AdGroupId") or 0)
            except (TypeError, ValueError):
                gid = 0
            if gid <= 0:
                continue
            row = {"adGroupId": str(gid), "keyword": phrase}
            if it.get("price") is not None:
                row["price"] = it.get("price")
            clean.append(row)
        if not clean:
            return []
        if len(clean) > 1000:
            out = []
            for i in range(0, len(clean), 1000):
                out.extend(self.add_keywords(clean[i:i + 1000]))
            return out
        self._bootstrap_csrf()
        q = ("mutation AddKeywords($input:GdAddKeywordsInput!){"
             "addKeywords(input:$input){addedItems{adGroupId keywordId}"
             "validationResult{errors{code params path}warnings{code params path}}}}")
        r = self._post("AddKeywords", q, {"input": {"addItems": clean}})
        data = r.json()
        res = (data.get("data") or {}).get("addKeywords") or {}
        vr = res.get("validationResult") or {}
        if data.get("errors") or vr.get("errors"):
            raise GridFinalizeError(
                "Grid add-keywords: " + json.dumps(
                    data.get("errors") or vr.get("errors"), ensure_ascii=False)[:400])
        return res.get("addedItems") or []

    def add_sitelink_set(self, sitelinks: list[dict]) -> int | None:
        """Создать набор быстрых ссылок через Grid (БЕЗ баллов) → id набора или None.
        Реверс HAR23/entry262: mutation AddSitelinkSets.
        sitelinks: [{title, href, description?}, ...] — title≤30, description≤60.
        Возвращает id созданного SitelinkSet (int) или None при ошибке."""
        if not sitelinks:
            return None
        self._bootstrap_csrf()
        q = ("mutation AddSitelinkSets($input:GdAddSitelinkSetsInput!$login:String!){"
             "reqId:getReqId addSitelinkSets(input:$input){"
             "addedSitelinkSets{id __typename}"
             "validationResult{errors{code params path}warnings{code params path}}}"
             "getClientMutationId(input:{login:$login}){mutationId}}")
        items = [{"title": (s.get("Title") or s.get("title") or "")[:30],
                  "href": (s.get("Href") or s.get("href") or ""),
                  "description": (s.get("Description") or s.get("description") or "")[:60]}
                 for s in sitelinks if (s.get("Title") or s.get("title"))
                                    and (s.get("Href") or s.get("href"))]
        if not items:
            return None
        r = self._post("AddSitelinkSets", q, {
            "login": self.login,
            "input": {"sitelinkSetsAddItems": [{"sitelinks": items}]},
        })
        data = r.json()
        res = (data.get("data") or {}).get("addSitelinkSets") or {}
        vr = res.get("validationResult") or {}
        if data.get("errors") or vr.get("errors"):
            raise GridFinalizeError(
                "Grid add-sitelink-set: " + json.dumps(
                    data.get("errors") or vr.get("errors"), ensure_ascii=False)[:400])
        added = res.get("addedSitelinkSets") or []
        if added and added[0] and added[0].get("id"):
            return int(added[0]["id"])
        return None

    def set_default_text(self, shopping_ad_ids: list, feed_id: int, text: str,
                         filters_by_ad_id: dict | None = None) -> list:
        """«Текст по умолчанию» товарных объявлений (ShoppingAd) — поле bodies через
        UpdateShoppingAds (в v5 у ShoppingAd текстового поля нет). policy:INHERIT —
        не трогаем наследуемые от кампании уточнения/ссылки."""
        # F review: приватный Grid падает «Внутренняя ошибка сервера» на больших пачках (150 ShoppingAd
        # одним запросом). Чанкуем по _GRID_MUTATION_CHUNK (как add_shopping_ads), иначе bodies остаются пусты.
        if len(shopping_ad_ids or []) > _GRID_MUTATION_CHUNK:
            out: list = []
            for i in range(0, len(shopping_ad_ids), _GRID_MUTATION_CHUNK):
                out.extend(self.set_default_text(shopping_ad_ids[i:i + _GRID_MUTATION_CHUNK],
                                                 feed_id, text, filters_by_ad_id))
                time.sleep(0.15)
            return out
        self._bootstrap_csrf()
        items = []
        for s in shopping_ad_ids:
            item = {"id": str(s), "permalinkId": None, "phoneId": None,
                    "fieldsToUseAsBody": None, "fieldsToUseAsName": None,
                    "feedId": str(feed_id), "bodies": [text], "hrefParams": "",
                    "inheritableCallouts": {"policy": "INHERIT"},
                    "inheritableSitelinkSet": {"policy": "INHERIT"}}
            if filters_by_ad_id:
                ff = filters_by_ad_id.get(s) or filters_by_ad_id.get(str(s))
                if ff:
                    item["feedFilter"] = ff
            items.append(item)
        r = self._post("UpdateShoppingAds", _SHOPPING_MUTATION,
                       {"updateShoppingInput": {"adUpdateItems": items, "saveDraft": True}})
        data = r.json()
        res = (data.get("data") or {}).get("updateShoppingAds") or {}
        if data.get("errors") or (res.get("validationResult") or {}).get("errors"):
            raise GridFinalizeError("Grid default-text: " + json.dumps(
                data.get("errors") or res.get("validationResult"), ensure_ascii=False)[:400])
        return res.get("updatedAds") or []

    def add_shopping_ads(self, items: list) -> list:
        """Товарные объявления (ShoppingAd) ЧЕРЕЗ GRID — БЕЗ БАЛЛОВ (реверс HAR19, addShoppingAds).
        v501 ads.add(ShoppingAd) требует баллов (152 при исчерпании) — главная причина падения
        куки-докрутки. Grid-мутация addShoppingAds создаёт товарку на куках без units.

        items: [{adgroup_id, feed_id, vendor?, collection_id?}, ...].
          vendor      → группа по МАРКЕ: feedFilter field=vendor CONTAINS_ANY (HAR19-проверено).
          collection_id → группа по МОДЕЛИ: field=collectionId CONTAINS_ANY.
          ни того, ни другого → товарка по ВСЕМУ фиду (вся витрина, намеренно для общих галерей).
        → список id созданных ShoppingAd (в порядке adAddItems), для set_default_text/листингов."""
        if len(items or []) > _GRID_MUTATION_CHUNK:
            out = []
            for i in range(0, len(items), _GRID_MUTATION_CHUNK):
                out.extend(self.add_shopping_ads(items[i:i + _GRID_MUTATION_CHUNK]))
            return out
        ad_items = []
        for it in items:
            entry = {
                "adGroupId": str(it["adgroup_id"]), "permalinkId": None, "phoneId": None,
                "fieldsToUseAsBody": None, "fieldsToUseAsName": None,
                "feedId": str(it["feed_id"]), "bodies": [], "hrefParams": "",
                "inheritableCallouts": {"policy": "INHERIT"},
                "inheritableSitelinkSet": {"policy": "INHERIT"},
            }
            conds = []
            if it.get("vendor"):
                # Регистр vendor зависит от ФИДА (HAR42/43: один фид <vendor>Belgee</vendor>, другой
                # <vendor>baic</vendor>). CONTAINS_ANY case-sensitive → передаём оба регистра, чтобы
                # совпасть с фидом независимо от его написания.
                _vv = str(it["vendor"])
                _variants = list(dict.fromkeys([_vv, _vv.lower(), _vv.title()]))
                conds.append({"field": "vendor", "operator": "CONTAINS_ANY",
                              "stringValue": json.dumps(_variants, ensure_ascii=False)})
            # МОДЕЛЬ (Модели-группы): доп. условие field=model (HAR: <vendor>Lada</vendor><model>Vesta Седан</model>).
            # Фид может не иметь поля model → UNKNOWN_FIELD → ретрай без него (ниже).
            _mv = it.get("model")
            if _mv:
                _mvals = _mv if isinstance(_mv, list) else [str(_mv)]
                _mvals = [str(x) for x in _mvals if str(x).strip()]
                if _mvals:
                    conds.append({"field": "model", "operator": "CONTAINS_ANY",
                                  "stringValue": json.dumps(_mvals, ensure_ascii=False)})
            if not conds and it.get("collection_id"):
                # collectionId требует EQUALS_ANY (НЕ CONTAINS_ANY → Grid даёт INVALID_OPERATOR и
                # ShoppingAd не создаётся). vendor — CONTAINS_ANY (строка), collectionId — EQUALS_ANY (id-set).
                conds.append({"field": "collectionId", "operator": "EQUALS_ANY",
                              "stringValue": json.dumps([str(it["collection_id"])], ensure_ascii=False)})
            if conds:
                entry["feedFilter"] = {"tab": "CONDITION", "conditions": conds}
            ad_items.append(entry)
        if not ad_items:
            return []
        self._bootstrap_csrf()
        q = ("mutation AddShoppingAds($addShoppingInput:GdAddShoppingAdsInput!){"
             "addShoppingAds(input:$addShoppingInput){addedAds{id}"
             "validationResult{errors{code params path}}}}")
        r = self._post("AddShoppingAds", q,
                       {"addShoppingInput": {"adAddItems": ad_items, "saveDraft": True}})
        data = r.json()
        res = (data.get("data") or {}).get("addShoppingAds") or {}
        vr_errors = (res.get("validationResult") or {}).get("errors") or []
        if data.get("errors") or vr_errors:
            # UNKNOWN_FIELD: мы добавили условие field=model (Модели-группы), но фид не имеет поля
            # <model> → Директ отбивает. Ретрай БЕЗ model-условия (vendor остаётся) — товарка по марке
            # лучше падения всей кампании (та же грабля, что у lzjk6p5m с tp7).
            has_unknown = any("UNKNOWN_FIELD" in str(e.get("code") or "") for e in vr_errors)
            if has_unknown and not data.get("errors"):
                _stripped = []
                for it in ad_items:
                    it2 = dict(it)
                    ff = it2.get("feedFilter")
                    if ff and ff.get("conditions"):
                        _c = [c for c in ff["conditions"] if c.get("field") != "model"]
                        if _c:
                            it2["feedFilter"] = {"tab": "CONDITION", "conditions": _c}
                        else:
                            it2.pop("feedFilter", None)
                    _stripped.append(it2)
                r3 = self._post("AddShoppingAds", q,
                                {"addShoppingInput": {"adAddItems": _stripped, "saveDraft": True}})
                d3 = r3.json()
                res3 = (d3.get("data") or {}).get("addShoppingAds") or {}
                if not (d3.get("errors") or (res3.get("validationResult") or {}).get("errors")):
                    return [a.get("id") for a in (res3.get("addedAds") or []) if a.get("id")]
                # без model тоже не вышло → общая обработка ниже (FEED_NOT_EXIST / raise), data/res ОСТАЮТСЯ исходными
            # Фид в ERROR-состоянии: Директ возвращает FEED_NOT_EXIST в validationResult.
            # Retry без feedId — товарка без фида лучше, чем падение всей кампании.
            has_feed_error = any("FEED_NOT_EXIST" in str(e.get("code") or "") for e in vr_errors)
            if has_feed_error and not data.get("errors"):
                retry_items = []
                for it in ad_items:
                    it2 = dict(it)
                    it2.pop("feedId", None)
                    it2.pop("feedFilter", None)
                    retry_items.append(it2)
                r2 = self._post("AddShoppingAds", q,
                                {"addShoppingInput": {"adAddItems": retry_items, "saveDraft": True}})
                data2 = r2.json()
                res2 = (data2.get("data") or {}).get("addShoppingAds") or {}
                if data2.get("errors") or (res2.get("validationResult") or {}).get("errors"):
                    raise GridFinalizeError("Grid add-shopping(no-feed retry): " + json.dumps(
                        data2.get("errors") or res2.get("validationResult"), ensure_ascii=False)[:400])
                return [a.get("id") for a in (res2.get("addedAds") or []) if a.get("id")]
            raise GridFinalizeError("Grid add-shopping: " + json.dumps(
                data.get("errors") or res.get("validationResult"), ensure_ascii=False)[:400])
        return [a.get("id") for a in (res.get("addedAds") or []) if a.get("id")]

    def add_listing_ads_by_shopping_ads(self, shopping_ad_ids: list) -> list:
        """«Страницы каталога» (ListingAd) ИЗ товарных (ShoppingAd) — реверс HAR19. Создаются ОТ
        ShoppingAd и НАСЛЕДУЮТ его текст («текст по умолчанию») + фильтр (vendor/collectionId).
        Так на «Страницах каталога» появляется тот же текст, что у «Товаров» (правило пользователя)."""
        ids = [str(s) for s in (shopping_ad_ids or []) if s]
        if not ids:
            return []
        if len(ids) > _GRID_MUTATION_CHUNK:
            out = []
            for i in range(0, len(ids), _GRID_MUTATION_CHUNK):
                out.extend(self.add_listing_ads_by_shopping_ads(ids[i:i + _GRID_MUTATION_CHUNK]))
            return out
        self._bootstrap_csrf()
        q = ("mutation AddListingAdsByShoppingAds($input:GdAddListingAdsByShoppingAdsInput!){"
             "addListingAdsByShoppingAds(input:$input){addedAds{id}"
             "validationResult{errors{code params path}}}}")
        r = self._post("AddListingAdsByShoppingAds", q,
                       {"input": {"shoppingAds": [{"id": i} for i in ids], "saveDraft": True}})
        data = r.json()
        res = (data.get("data") or {}).get("addListingAdsByShoppingAds") or {}
        if data.get("errors") or (res.get("validationResult") or {}).get("errors"):
            raise GridFinalizeError("Grid listing-by-shopping: " + json.dumps(
                data.get("errors") or res.get("validationResult"), ensure_ascii=False)[:400])
        return res.get("addedAds") or []

    def set_listing_name_filters(self, items: list) -> int:
        """Фильтр «Страницы каталога» (ListingAd) по ИМЕНИ каталога (HAR36 direct.yandex.ru.36har):
        `mutation updateListingAds` (строчная u!) с feedFilter {field:name, operator:CONTAINS_ANY,
        stringValue: json([value])}. value — марка (Марки) или марка+модель (Модели) в нижнем регистре.
        Grid by-shopping листинг фильтр НЕ наследует → ставим явно ПОСЛЕ создания. Полный item обязателен
        (permalinkWithPhone/bodies/inheritable* — иначе internal error). items:[{id,feed_id,value,bodies}].
        → число обновлённых. Бросает GridFinalizeError при ошибке."""
        # F review: чанкинг — приватный Grid падает 500 на больших пачках (как set_default_text/add_shopping_ads).
        if len(items or []) > _GRID_MUTATION_CHUNK:
            total = 0
            for i in range(0, len(items), _GRID_MUTATION_CHUNK):
                total += self.set_listing_name_filters(items[i:i + _GRID_MUTATION_CHUNK])
                time.sleep(0.15)
            return total
        upd = []
        for it in (items or []):
            val = (it.get("value") or "").strip()
            _item_id = it.get("id")
            _item_agid = it.get("adgroup_id")
            # поддержка adgroup_id как ключа (saveDraft:True → addedAds пуст, фильтр ставится на группу)
            if (not _item_id and not _item_agid) or not it.get("feed_id") or not val:
                continue
            _entry: dict = {
                "permalinkWithPhone": {"policy": "CLEAR"},
                "fieldsToUseAsBody": None, "fieldsToUseAsName": None,
                "feedId": str(it["feed_id"]),
                "feedFilter": {"tab": "CONDITION", "conditions": [
                    {"field": "name", "operator": "CONTAINS_ANY",
                     "stringValue": json.dumps([val], ensure_ascii=False)}]},
                "bodies": list(it.get("bodies") or []),
                "hrefParams": "",
                "inheritableCallouts": {"policy": "INHERIT"},
                "inheritableSitelinkSet": {"policy": "INHERIT"},
            }
            if _item_id:
                _entry["id"] = str(_item_id)
            else:
                _entry["adGroupId"] = str(_item_agid)
            upd.append(_entry)
        if not upd:
            return 0
        self._bootstrap_csrf()
        q = ("mutation updateListingAds($updateListingInput:GdUpdateListingAdsInput!){"
             "updateListingAds(input:$updateListingInput){updatedAds{id}"
             "validationResult{errors{code params path}}}}")
        r = self._post("updateListingAds", q,
                       {"updateListingInput": {"adUpdateItems": upd, "saveDraft": True}})
        data = r.json()
        res = (data.get("data") or {}).get("updateListingAds") or {}
        if data.get("errors") or (res.get("validationResult") or {}).get("errors"):
            raise GridFinalizeError("updateListingAds(name-filter): " + json.dumps(
                data.get("errors") or res.get("validationResult"), ensure_ascii=False)[:400])
        return len(res.get("updatedAds") or [])

    # ── Изображения для РСЯ-объявлений (куки-путь, без баллов) ───────────────

    def suggest_images(self, campaign_id: int) -> list[str]:
        """SuggestImages Grid-query — хэши изображений, которые Директ предлагает по кампании.
        Реверс из HAR-25/Entry52. Исключаем NEURO_STOCK/PHOTO_STOCK/WEB_SITE/GEO_SEARCH (как в HAR).
        → список imageHash строк (может быть пустым). Не бросает — [] при ошибке."""
        self._bootstrap_csrf()
        q = ("query SuggestImages($input:GdSuggestImagesInput!){"
             "reqId:getReqId suggestImages(input:$input){"
             "suggests{uploadedImage{imageHash}}}}")
        v = {"input": {
            "cid": str(campaign_id),
            "sourceFilter": {"type": "EXCLUDE",
                             "sources": ["NEURO_STOCK", "PHOTO_STOCK", "WEB_SITE", "GEO_SEARCH"]},
        }}
        try:
            r = self._post("SuggestImages", q, v)
            if r.status_code == 403:
                r = self._post("SuggestImages", q, v)
            data = r.json()
            suggests = ((data.get("data") or {}).get("suggestImages") or {}).get("suggests") or []
            out = []
            for s in suggests:
                h = ((s.get("uploadedImage") or {}).get("imageHash") or "")
                if h and h not in out:
                    out.append(h)
            return out
        except Exception:  # noqa: BLE001
            return []

    def upload_image(self, image_path: str) -> str | None:
        """Загрузить файл картинки в библиотеку Директа через web-api/image/upload (multipart).
        Реверс из HAR-25/Entry10. image_type=BANNER_TEXT (РСЯ-баннер).
        → imageHash строка или None при ошибке. Не требует баллов (куки-путь)."""
        import os as _os
        try:
            if not _os.path.isfile(image_path):
                return None
            self._bootstrap_csrf()
            url = f"https://direct.yandex.ru/web-api/image/upload?ulogin={self.login}"
            headers = {
                "Cookie": self.cookie,
                "User-Agent": cmc.USER_AGENT,
                "Origin": "https://direct.yandex.ru",
                "Referer": f"https://direct.yandex.ru/dna/grid/campaigns?ulogin={self.login}",
            }
            if self.csrf:
                headers["x-csrf-token"] = self.csrf
            fname = _os.path.basename(image_path)
            with open(image_path, "rb") as fh:
                files = {"files": (fname, fh, "image/jpeg")}
                data = {"image_type": "BANNER_TEXT"}
                r = self.sess.post(url, files=files, data=data, headers=headers, timeout=60)
            if r.status_code == 403:
                # CSRF мог обновиться — пробуем ещё раз
                with open(image_path, "rb") as fh:
                    files = {"files": (fname, fh, "image/jpeg")}
                    r = self.sess.post(url, files=files, data=data, headers=headers, timeout=60)
            j = r.json()
            result = ((j.get("result") or [None])[0]) or {}
            return result.get("hash") or None
        except Exception:  # noqa: BLE001
            return None

    def update_ad_images(self, ad_items: list[dict], *, allow_empty_images: bool = False) -> int:
        """Добавить imageHashes к объявлениям через UpdateAdaptiveTextAds Grid-mutation.
        Реверс из HAR-25/Entry27.
        ad_items: [{id, href, titles, bodies, imageHashes, adPrice?}, ...]
        allow_empty_images=True lets callers intentionally clear imageHashes while updating text.
        adPrice: {"price","priceOld","prefix","currency"} | None.
        → число обновлённых объявлений. Не бросает — 0 при ошибке."""
        upd = []
        for it in (ad_items or []):
            if not it.get("id") or (not allow_empty_images and not it.get("imageHashes")):
                continue
            item = {
                "href": it.get("href") or "",
                "hrefParams": "",
                "domain": None,
                "titles": it.get("titles") or [],
                "bodies": it.get("bodies") or [],
                "imageHashes": list(it["imageHashes"]),
                "creativeIds": [],
                "permalinkId": None,
                "phoneId": None,
                "erirAdDescription": None,
                "inheritableCallouts": {"policy": "INHERIT"},
                "inheritableSitelinkSet": {"policy": "INHERIT"},
                "id": str(it["id"]),
            }
            if it.get("adPrice"):
                item["adPrice"] = it["adPrice"]
            upd.append(item)
        if not upd:
            return 0
        q = ("mutation UpdateAdaptiveTextAds($updateInput:GdUpdateAdaptiveTextAdsInput!){"
             "reqId:getReqId updateAdaptiveTextAds(input:$updateInput){"
             "updatedAds{id}validationResult{errors{code params path}"
             "warnings{code params path}}}}")
        try:
            self._bootstrap_csrf()
            r = self._post("UpdateAdaptiveTextAds", q,
                           {"updateInput": {"adUpdateItems": upd, "saveDraft": True}})
            if r.status_code == 403:
                r = self._post("UpdateAdaptiveTextAds", q,
                               {"updateInput": {"adUpdateItems": upd, "saveDraft": True}})
            data = r.json()
            res = (data.get("data") or {}).get("updateAdaptiveTextAds") or {}
            return len(res.get("updatedAds") or [])
        except Exception:  # noqa: BLE001
            return 0

    # ── AUTO-REPAIR: чтение групп (GroupsForEdit) + full-object обновление групп ──

    def _post_json_retry(self, op: str, query: str, variables: dict) -> dict:
        """POST с ретраем на 403 (свежий CSRF) и на транзиентную серверную ошибку Яндекса
        (top-level errors) — 3 попытки с backoff. → JSON. GridFinalizeError при финальном сбое.
        (Тот же паттерн, что grid_create._mutate — но без refresh_cookie: клиент уже с валидной кукой.)"""
        last_transient = None
        for srv_try in range(3):
            r = self._post(op, query, variables)
            if r.status_code == 403:                 # первый POST дал CSRF → ретрай
                r = self._post(op, query, variables)
            try:
                j = r.json()
            except Exception as e:  # noqa: BLE001
                raise GridFinalizeError(f"{op}: не-JSON HTTP {r.status_code}: {r.text[:160]}") from e
            errs = j.get("errors")
            if errs:
                msg = str(errs).lower()
                if any(t in msg for t in _TRANSIENT_ERR) and srv_try < 2:
                    last_transient = str(errs)[:240]
                    import time as _t
                    _t.sleep(0.6 * (srv_try + 1))
                    continue
                raise GridFinalizeError(f"{op}: {str(errs)[:300]}")
            return j
        raise GridFinalizeError(f"{op}: транзиент Яндекса не ушёл за 3 попытки: {last_transient}")

    def groups_for_edit(self, campaign_id: int | list[int]) -> list[dict]:
        """Прочитать группы кампании(й) для read-modify-write UpdateUnifiedAdGroups.

        Возвращает список нормализованных групп со всеми полями, нужными для полного
        (full-object) обновления группы БЕЗ потери минус-слов/регионов/трекинга, плюс
        служебные поля для идемпотентности (keyword_count/relevance_match) и safety
        (bid_modifiers_present/retargetings_present). Только GdUnifiedAdGroup — остальные
        типы отдаём с ``supported=False`` (их этот путь не трогает).

        campaign_id может быть int или списком int (читаем пачкой одним запросом)."""
        if isinstance(campaign_id, (list, tuple, set)):
            ids = [int(c) for c in campaign_id if str(c).strip().lstrip("-").isdigit()]
        else:
            ids = [int(campaign_id)] if str(campaign_id).strip().lstrip("-").isdigit() else []
        ids = [c for c in dict.fromkeys(ids) if c > 0]
        if not ids:
            return []
        self._bootstrap_csrf()
        id_strings = [str(c) for c in ids]
        variables = {
            "login": self.login,
            "agInp": {"filter": {"campaignIdIn": id_strings},
                      "statRequirements": {"preset": "TODAY"},
                      "limitOffset": {"offset": 0, "limit": 10000},
                      "orderBy": [{"field": "ID", "order": "ASC"}]},
            "scInp": {"filter": {"typeIn": ["KEYWORD"], "campaignIdIn": id_strings},
                      "statRequirements": {"preset": "TODAY"},
                      "limitOffset": {"offset": 0, "limit": 10000},
                      "orderBy": [{"order": "DESC", "field": "GROUP_ID"}]},
            "rtInp": {"filter": {"campaignIdIn": id_strings, "typeNotIn": ["INTERESTS"]},
                      "statRequirements": {"preset": "TODAY"},
                      "limitOffset": {"offset": 0, "limit": 10000},
                      "orderBy": [{"order": "DESC", "field": "GROUP_ID"}]},
        }
        j = self._post_json_retry("GroupsForEditLite", _GROUPS_FOR_EDIT_LITE_Q, variables)
        client = (j.get("data") or {}).get("client") or {}
        ag_rows = ((client.get("adGroups") or {}).get("rowset") or [])
        sc_rows = ((client.get("showConditions") or {}).get("rowset") or [])
        rt_rows = ((client.get("retargetings") or {}).get("rowset") or [])

        kw_by_group: dict[str, list[str]] = {}
        for row in sc_rows:
            if (row.get("__typename") or "") != "GdKeyword":
                continue
            gid = str(row.get("adGroupId") or "")
            phrase = str(row.get("keyword") or "").strip()
            if gid and phrase:
                kw_by_group.setdefault(gid, []).append(phrase)
        rt_groups = {str(r.get("adGroupId") or "") for r in rt_rows if r.get("adGroupId")}

        out: list[dict] = []
        for g in ag_rows:
            gid = str(g.get("id") or "")
            if not gid:
                continue
            typename = str(g.get("__typename") or "")
            camp = g.get("campaign") or {}
            try:
                camp_id = int(camp.get("id"))
            except (TypeError, ValueError):
                camp_id = None
            region_ids = [int(x) for x in ((g.get("regionsInfo") or {}).get("regionIds") or [])
                          if str(x).lstrip("-").isdigit()]
            lib_ids = [str(p.get("id")) for p in (g.get("libraryMinusKeywordsPacks") or []) if p.get("id")]
            rm = g.get("relevanceMatch")
            relevance = None
            if isinstance(rm, dict):
                relevance = {
                    "id": (str(rm.get("id")) if rm.get("id") not in (None, "") else None),
                    "isActive": bool(rm.get("isActive")),
                    "relevanceMatchCategories": list(rm.get("relevanceMatchCategories") or []),
                    "autotargetingBrandSettings": list(rm.get("autotargetingBrandSettings") or []),
                }
            offer = g.get("offerRetargeting")
            out.append({
                "adgroup_id": int(gid),
                "adgroup_name": str(g.get("name") or ""),
                "type": typename,
                "supported": typename == "GdUnifiedAdGroup",
                "campaign_id": camp_id,
                "campaign_name": str(camp.get("name") or ""),
                "keywords": kw_by_group.get(gid, []),
                "keyword_count": len(kw_by_group.get(gid, [])),
                "relevance_match": relevance,
                "region_ids": region_ids,
                "minus_keywords": [str(m) for m in (g.get("minusKeywords") or [])],
                "library_minus_ids": lib_ids,
                "hyper_geo_id": g.get("hyperGeoId"),
                "hyperlocal_geo_segments": g.get("hyperlocalGeoSegments"),
                "audience_targeting": g.get("audienceTargeting") or "ALL_AUDIENCE",
                "content_type_show_settings": g.get("contentTypeShowSettings"),
                "tracking_params": g.get("trackingParams"),
                "content_language": g.get("contentLanguage"),
                "promo_inheritance_policy": g.get("promoExtensionInheritancePolicy") or "MERGE",
                "inheritable_callouts_policy": ((g.get("inheritableCallouts") or {}).get("policy") or "INHERIT"),
                "inheritable_sitelink_policy": ((g.get("inheritableSitelinkSet") or {}).get("policy") or "INHERIT"),
                "offer_retargeting": ({"isActive": bool(offer.get("isActive"))}
                                      if isinstance(offer, dict) else None),
                "bid_modifiers_present": bool(g.get("bidModifiers")),
                "retargetings_present": gid in rt_groups,
            })
        return out

    def build_update_item(self, grp: dict, *, keywords: list[str],
                               relevance_match: dict | None) -> dict:
        """Собрать GdUpdateUnifiedAdGroupItem: round-trip ВСЕХ полей группы (регионы/минус-слова/
        трекинг/аудитория сохраняются как прочитано) + подставить keywords и relevanceMatch.
        Поля, которые не отдаёт GroupsForEdit-lite (caRetargetingCondition/retargetings/
        searchRetargetings/generalPrice/bidModifiers), шлём в дефолте build_adgroup — это боевой
        стейт групп, создаваемых этим сервисом; вызывающий обязан пропускать группы с
        retargetings_present/bid_modifiers_present, чтобы не затереть непустые значения."""
        kw = [{"phrase": str(k)} for k in (keywords or []) if str(k).strip()][:200]
        item = {
            "adGroupId": str(grp["adgroup_id"]),
            "adGroupName": grp.get("adgroup_name") or "",
            "adGroupMinusKeywords": [str(m) for m in (grp.get("minus_keywords") or [])][:100],
            "bidModifiers": {},
            "libraryMinusKeywordsIds": [str(i) for i in (grp.get("library_minus_ids") or [])],
            "regionIds": [int(r) for r in (grp.get("region_ids") or [])] or [225],
            "hyperGeoId": grp.get("hyper_geo_id"),
            "hyperlocalGeoSegments": grp.get("hyperlocal_geo_segments"),
            "audienceTargeting": grp.get("audience_targeting") or "ALL_AUDIENCE",
            "contentTypeShowSettings": grp.get("content_type_show_settings"),
            "keywords": kw,
            "caRetargetingCondition": None,
            "retargetings": [],
            "searchRetargetings": [],
            "offerRetargeting": ({"isActive": bool((grp.get("offer_retargeting") or {}).get("isActive")),
                                  "id": None} if grp.get("offer_retargeting") else None),
            "relevanceMatch": relevance_match,
            "promoExtensionInheritancePolicy": grp.get("promo_inheritance_policy") or "MERGE",
            "inheritableCallouts": {"policy": grp.get("inheritable_callouts_policy") or "INHERIT"},
            "inheritableSitelinkSet": {"policy": grp.get("inheritable_sitelink_policy") or "INHERIT"},
            "generalPrice": None,
            "trackingParams": grp.get("tracking_params") if grp.get("tracking_params") is not None else cmc.UTM_TEMPLATE,
            "contentLanguage": grp.get("content_language"),
            "useBidModifiers": True,
        }
        return item

    def update_unified_adgroups(self, items: list[dict]) -> list[int]:
        """UpdateUnifiedAdGroups (full-object) → список обновлённых adGroupId (int).
        Ретрай на транзиент/403. Бросает GridFinalizeError при validationResult.errors."""
        items = [it for it in (items or []) if it and it.get("adGroupId")]
        if not items:
            return []
        updated: list[int] = []
        for i in range(0, len(items), _GRID_MUTATION_CHUNK):
            chunk = items[i:i + _GRID_MUTATION_CHUNK]
            self._bootstrap_csrf()
            j = self._post_json_retry("UpdateUnifiedAdGroups", _UPDATE_UNIFIED_ADGROUPS_Q,
                                      {"unifiedUpdateInput": chunk})
            res = (j.get("data") or {}).get("updateUnifiedAdGroups") or {}
            vr = res.get("validationResult") or {}
            if vr.get("errors"):
                raise GridFinalizeError(
                    "UpdateUnifiedAdGroups validation: "
                    + json.dumps(vr.get("errors"), ensure_ascii=False)[:400])
            for row in (res.get("updatedAdGroupItems") or []):
                try:
                    updated.append(int(row.get("adGroupId")))
                except (TypeError, ValueError):
                    continue
        return updated


# ── v5-корректировки «Глобальных правил» (множественный формат, ПОСЛЕ Grid) ──
def _seg_key(name: str) -> tuple:
    pfx, _, rest = (name or "").partition("_")
    return ("self" if pfx == "self" else "geo", rest)


def corrections_by_segment(corr_audiences: list, seg_names: list) -> dict:
    """Сегмент аккаунта → pct из правил (кросс-классовый фолбэк для исключений)."""
    by_cr, by_rest = {}, {}
    for a in corr_audiences:
        k = _seg_key(a.get("name") or "")
        p = int(a.get("pct") or 0)
        by_cr[k] = p
        if p:
            by_rest.setdefault(k[1], []).append(p)
    out = {}
    for nm in seg_names:
        k = _seg_key(nm)
        p = by_cr.get(k)
        if not p:
            alt = by_rest.get(k[1])
            if alt:
                p = max(alt, key=abs)
        out[nm] = p
    return out


def apply_corrections(v5: cmc.DirectV501Client, campaign_id: int,
                      demographic: list, audiences: list, ret_map: dict) -> int:
    """Поставить корректировки «Глобальных правил» через v5 bidmodifiers.add (только pct≠0).
    Формат — МНОЖЕСТВЕННЫЙ (DemographicsAdjustments/RetargetingAdjustments). → кол-во применённых.
    ВАЖНО: вызывать ПОСЛЕ GridClient.finalize (Grid перезаписывает bidModifiers)."""
    dem = []
    for d in demographic:
        pct = int(d.get("pct") or 0)
        if not pct:
            continue
        bm = max(0, min(1300, 100 + pct))
        if d["kind"] == "age":
            dem.append({"Age": d["key"], "BidModifier": bm})
        elif d["kind"] == "gender":
            dem.append({"Gender": d["key"], "BidModifier": bm})
    seg_pct = corrections_by_segment(audiences, list(ret_map.keys()))
    ret = []
    for nm, rid in ret_map.items():
        pct = seg_pct.get(nm)
        if pct:
            ret.append({"RetargetingConditionId": int(rid), "BidModifier": max(0, min(1300, 100 + int(pct)))})
    items = []
    if dem:
        items.append({"CampaignId": int(campaign_id), "DemographicsAdjustments": dem})
    if ret:
        items.append({"CampaignId": int(campaign_id), "RetargetingAdjustments": ret})
    if not items:
        return 0
    r = v5._call("bidmodifiers", "add", {"BidModifiers": items})
    return sum(1 for x in r.get("AddResults", []) if x.get("Id"))

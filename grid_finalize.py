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
import threading
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
_CAMPAIGNS_EDIT_DATA_Q = (_DIR / "grid_campaigns_edit_data.graphql").read_text(encoding="utf-8")

# Транзиентные серверные ошибки Яндекса (top-level errors, НЕ валидация) — ретраим с backoff.
_TRANSIENT_ERR = ("внутренняя ошибка сервера", "internal server error", "internal error",
                  "timeout", "timed out", "temporarily", "try again", "503", "502", "504")


def _is_transient_data_error(errs) -> bool:
    """True если data['errors'] содержит транзиентную серверную ошибку (нужно ретраить).
    False — если ошибка валидационная/авторизационная (не ретраить)."""
    for e in (errs if isinstance(errs, list) else [errs]):
        txt = (str(e.get("message") or "") + " " +
               str((e.get("extensions") or {}).get("code") or "")).lower()
        if any(t in txt for t in _TRANSIENT_ERR):
            return True
    return False


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

# Fallback broadMatch for narrow campaign mutations — broadMatch is NonNull in
# GdUpdateCampaignsInput; omitting it produces: Field 'broadMatch' has coerced Null
# value for NonNull type 'GdBroadMatchRequestInput!'.
_BROAD_MATCH_DEFAULT: dict = {"broadMatchFlag": False, "broadMatchGoalId": None, "broadMatchLimit": 0}


def _strip_graphql_typenames(value):
    if isinstance(value, dict):
        return {k: _strip_graphql_typenames(v) for k, v in value.items() if k != "__typename"}
    if isinstance(value, list):
        return [_strip_graphql_typenames(v) for v in value]
    return value


class GridFinalizeError(RuntimeError):
    pass


# A2: переиспользование GridClient (сессия + CSRF) на протяжении набора/кампании вместо создания
# нового инстанса на КАЖДЫЙ из ~28 вызовов в цикле create_set (каждый новый инстанс = новый
# requests.Session + повторный _bootstrap_csrf POST). Кэш ключуется по (login, cookie, thread_ident):
#   • thread_ident → каждый поток пула A1 получает СВОЙ клиент (requests.Session не потокобезопасна —
#     нельзя шарить один Session между воркерами);
#   • cookie → явная агентская кука (copy_engine/UAC-сессии) не смешивается с дефолтной;
#   • cookie_only включён в ключ (см. ниже) — cookie_only=True и False дают разные инстансы,
#     иначе первый вызов с cookie_only=True необратимо переключал бы флаг у общего инстанса.
_GRID_CLIENT_CACHE: dict = {}
_GRID_CLIENT_LOCK = threading.Lock()


def get_grid_client(login: str, cookie: str | None = None,
                    cookie_only: bool = False) -> "GridClient":
    """Переиспользуемый GridClient для (login, cookie, cookie_only, текущий поток). Держит
    сессию и CSRF между вызовами → нет повторного bootstrap-POST и нового TCP-пула на каждую
    Grid-операцию. Потокобезопасно: ключ включает thread ident, поэтому воркеры пула A1 не
    делят один Session. cookie_only входит в ключ: разные режимы не отравляют кэш друг друга."""
    key = (login, cookie or "", cookie_only, threading.get_ident())
    with _GRID_CLIENT_LOCK:
        cli = _GRID_CLIENT_CACHE.get(key)
        if cli is None:
            cli = GridClient(login, cookie=cookie, cookie_only=cookie_only)
            _GRID_CLIENT_CACHE[key] = cli
    return cli


def reset_grid_client_cache(login: str | None = None) -> None:
    """Сбросить кэш клиентов (например после протухания куки/force_refresh). None → весь кэш."""
    with _GRID_CLIENT_LOCK:
        if login is None:
            _GRID_CLIENT_CACHE.clear()
        else:
            for k in [k for k in _GRID_CLIENT_CACHE if k[0] == login]:
                _GRID_CLIENT_CACHE.pop(k, None)


class GridClient:
    """Тонкий клиент web-api/grid/api на агентских куках (CSRF добирается сам)."""

    def __init__(self, login: str, cookie: str | None = None, cookie_only: bool = False):
        self.login = login
        self.cookie = cookie or cmc.pick_working_cookie(login)
        self.csrf: str | None = None
        self.sess = requests.Session()
        self.sess.verify = False
        # cookie_only=True → кампания/группы созданы САМИМ Grid (create_full по куке), а не токеном
        # v501, поэтому token→Grid replication lag ОТСУТСТВУЕТ: пред-эмптивные паузы можно пропустить
        # (A3). На токен-пути (cookie_only=False) паузы остаются — там лаг реален.
        self._cookie_only = bool(cookie_only)
        # A2-heal: отслеживаем, была ли кука передана явно (копировщик / UAC) или взята через
        # pick_working_cookie. При протухании куки (стаканный 403) _reauth обновит куку только
        # для не-явного пути (pick_working_cookie снова); для явного — только сбросит CSRF.
        self._explicit_cookie = bool(cookie)

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
        # БЕЗ транспортного ретрая: add_shopping_ads/add_listing_ads/add_callouts/add_keywords —
        # НЕ идемпотентны (обрыв ответа после commit + ретрай = ДУБЛЬ). Идемпотентные RMW-сеттеры
        # (disabledPlaces/age/callouts full-RMW) переживают единичный обрыв через ре-ран джобы.
        _had_csrf = self.csrf is not None   # A2-heal: различаем bootstrap-403 и stale-cookie-403
        r = self.sess.post(url, json={"operationName": op, "query": query, "variables": variables},
                           headers=headers, timeout=40)
        m = re.search(r"_direct_csrf_token=([^;,\s]+)", r.headers.get("Set-Cookie", ""))
        tok = r.cookies.get("_direct_csrf_token") or (m.group(1) if m else None)
        if tok:
            self.csrf = tok
        # A2-heal: если CSRF уже был установлен, но всё равно 403 — кука протухла после
        # кэширования (ротация сессии Яндекса). Переподхватываем куку + CSRF и повторяем ОДИН раз.
        # Случай первого bootstrap-403 (_had_csrf=False) сюда не попадает — им управляет
        # _bootstrap_csrf (ретрай снаружи). Рекурсии нет: _reauth обнуляет self.csrf → вложенный
        # вызов _post из _bootstrap_csrf видит _had_csrf=False и не заходит в эту ветку.
        if r.status_code == 403 and _had_csrf:
            print(f"[grid] stale-cookie 403 {self.login}/{op}: reauth → retry", flush=True)
            self._reauth()
            headers["Cookie"] = self.cookie
            if self.csrf:
                headers["x-csrf-token"] = self.csrf
            r = self.sess.post(url, json={"operationName": op, "query": query, "variables": variables},
                               headers=headers, timeout=40)
            m2 = re.search(r"_direct_csrf_token=([^;,\s]+)", r.headers.get("Set-Cookie", ""))
            tok2 = r.cookies.get("_direct_csrf_token") or (m2.group(1) if m2 else None)
            if tok2:
                self.csrf = tok2
        return r

    def _bootstrap_csrf(self) -> None:
        # A2: идемпотентность — CSRF-токен добывается ОДИН раз на инстанс (переиспользуемый через
        # get_grid_client клиент держит его между вызовами finalize/add_*/set_*), повторный bootstrap
        # = лишний Grid-POST на каждой операции.
        if self.csrf:
            return
        q = ("query Callouts($login:String!){callouts(input:{searchBy:{login:$login}"
             "filter:{deleted:false}}){id}}")
        r = self._post("Callouts", q, {"login": self.login})
        if r.status_code == 403:                       # первый POST даёт CSRF → ретрай
            self._post("Callouts", q, {"login": self.login})

    def _reauth(self) -> None:
        """A2-heal: сброс CSRF + обновление куки при stale-cookie-403.

        Вызывается из _post когда: csrf уже был установлен (сессия кэшировалась), но
        пришёл 403 (кука протухла во время набора, ротация сессии Яндекса).

        Для не-явной куки (pick_working_cookie путь, типичный finalize) — подхватываем
        свежую рабочую куку. Для явной куки (copy_engine / UAC) — только сбрасываем CSRF
        (куку контролирует вызывающий, мы её не меняем).
        После сброса вызываем _bootstrap_csrf — он видит csrf=None → выполняет полный
        bootstrap-POST. _post внутри bootstrap видит _had_csrf=False → не заходит в _reauth
        повторно (нет рекурсии)."""
        self.csrf = None
        if not self._explicit_cookie:
            self.cookie = cmc.pick_working_cookie(self.login)
        self._bootstrap_csrf()

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
        # startDate: в шаблоне ЗАХАРДКОЖЕНА дата съёма HAR (2026-06-21) → как только календарь
        # ушёл дальше, КАЖДЫЙ finalize валился DateDefectIds.MUST_BE_GREATER_THAN_OR_EQUAL_TO_MIN
        # (min=сегодня) → места показа/автотаргет НЕ выставлялись → verifier ставил
        # WRONG_AUTOTARGET и сносил свежие tp5 на пересоздание (карусель 2026-07-06).
        # Черновик стартует не раньше сегодня — всегда «сегодня по МСК» (таймзона Директа).
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        uc["startDate"] = _dt.now(_tz(_td(hours=3))).strftime("%Y-%m-%d")
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
        HAR56: редактор кампаний создаёт новые уточнения через SaveCallouts.
        Лимит ≤25 симв. на текст должен быть выполнен на стороне вызывающего."""
        existing = self.get_callouts()
        to_create = [t for t in texts if t and t not in existing]
        if not to_create:
            return {t: existing[t] for t in texts if t in existing}
        self._bootstrap_csrf()
        q = ("mutation SaveCallouts($input:GdSaveCalloutsInput!){"
             "saveCallouts(input:$input){calloutIds "
             "validationResult{errors{code params path}warnings{params path code}}}}")
        r = self._post("SaveCallouts", q, {
            "input": {"saveItems": [{"text": t} for t in to_create]},
        })
        data = r.json()
        res = (data.get("data") or {}).get("saveCallouts") or {}
        vr = res.get("validationResult") or {}
        if data.get("errors") or vr.get("errors"):
            err_blob = json.dumps(data.get("errors") or vr.get("errors"), ensure_ascii=False)
            raise GridFinalizeError(
                "Grid save-callouts: " + err_blob[:400])
        added_ids = res.get("calloutIds") or []
        for text, raw_id in zip(to_create, added_ids):
            try:
                cid = int(raw_id)
            except (TypeError, ValueError):
                cid = 0
            if cid > 0:
                existing[text] = cid
        missing = [t for t in to_create if t not in existing]
        if missing:
            fresh = self.get_callouts()
            for text in missing:
                if text in fresh:
                    existing[text] = fresh[text]
        return {t: existing[t] for t in texts if t in existing}

    def _read_broad_match_map(self, campaign_ids: list[int]) -> dict[int, dict]:
        """Read broadMatch for campaigns to echo back in narrow UpdateCampaigns mutations.

        broadMatch is NonNull in GdUnifiedCampaignInput — narrow mutations that omit it
        receive: "Field 'broadMatch' has coerced Null value for NonNull type
        'GdBroadMatchRequestInput!'". This reads the current value so it can be included
        unchanged. Falls back to _BROAD_MATCH_DEFAULT on any read failure.
        """
        ids = [cid for cid in (campaign_ids or []) if cid > 0]
        if not ids:
            return {}
        q = ("query CampaignsBroadMatch($login:String!,$inp:GdCampaignsContainerInput!){"
             "client(searchBy:{login:$login}){campaigns(input:$inp){"
             "rowset{id name startDate endDate timeTarget{enabledHolidaysMode "
             "holidaysSettings{isShow startHour endHour rateCorrections}idTimeZone timeBoard "
             "useWorkingWeekends} notification{smsSettings{smsTime{startTime{hour minute}"
             "endTime{hour minute}}}emailSettings{stopByReachDailyBudget email}} "
             "...on GdUnifiedCampaign{dayBudget enableCompanyInfo "
             "excludePausedCompetingAds hasAddMetrikaTagToUrl hasAddOpenstatTagToUrl "
             "hasExtendedGeoTargeting broadMatch{"
             "broadMatchFlag broadMatchGoalId broadMatchLimit}}}}}}")
        out: dict[int, dict] = {}
        for chunk in [ids[i:i + 100] for i in range(0, len(ids), 100)]:
            inp = {
                "filter": {"campaignIdIn": [str(cid) for cid in chunk]},
                "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
                "limitOffset": {"limit": 5000, "offset": 0},
                "orderBy": [{"order": "ASC", "field": "ID"}],
            }
            r = self._post("CampaignsBroadMatch", q, {"login": self.login, "inp": inp})
            data = r.json()
            rows = ((((data.get("data") or {}).get("client") or {})
                     .get("campaigns") or {}).get("rowset") or [])
            for row in rows:
                try:
                    cid = int(row.get("id"))
                except (TypeError, ValueError):
                    continue
                bm = row.get("broadMatch") if isinstance(row.get("broadMatch"), dict) else {}
                out[cid] = {
                    "name": row.get("name") or "",
                    "startDate": row.get("startDate") or None,
                    "endDate": row.get("endDate") or None,
                    "timeTarget": row.get("timeTarget") or None,
                    "notification": row.get("notification") or None,
                    "broadMatchFlag": bool(bm.get("broadMatchFlag")),
                    "broadMatchGoalId": bm.get("broadMatchGoalId"),
                    "broadMatchLimit": int(bm.get("broadMatchLimit") or 0),
                    "dayBudget": str(row.get("dayBudget") or "0"),
                    "enableCompanyInfo": bool(row.get("enableCompanyInfo")),
                    "excludePausedCompetingAds": bool(row.get("excludePausedCompetingAds")),
                    "hasAddMetrikaTagToUrl": bool(row.get("hasAddMetrikaTagToUrl")),
                    "hasAddOpenstatTagToUrl": bool(row.get("hasAddOpenstatTagToUrl")),
                    "hasExtendedGeoTargeting": bool(row.get("hasExtendedGeoTargeting")),
                    "hasSiteMonitoring": None,
                    "hasTitleSubstitute": None,
                }
        return out

    def _narrow_campaign_base(self, cid: int, bm_map: dict[int, dict]) -> dict:
        """Build the minimal GdUnifiedCampaignInput skeleton for narrow campaign mutations.

        All narrow UpdateCampaigns mutations (set-callouts, set-sitelink-set, set-names)
        must include broadMatch because the Grid schema declares it NonNull. The caller
        adds the mutation-specific field on top of the returned dict.
        """
        bm = bm_map.get(cid) or _BROAD_MATCH_DEFAULT
        has_site_monitoring = bm.get("hasSiteMonitoring")
        has_title_substitute = bm.get("hasTitleSubstitute")
        notification = bm.get("notification") or {
            "smsSettings": {
                "smsTime": {
                    "startTime": {"hour": 9, "minute": 0},
                    "endTime": {"hour": 21, "minute": 0},
                },
                "enableEvents": [],
            },
            "emailSettings": {"stopByReachDailyBudget": True, "email": ""},
        }
        notification.setdefault("smsSettings", {})
        notification["smsSettings"].setdefault("enableEvents", [])
        notification.setdefault("emailSettings", {})
        notification["emailSettings"].setdefault("stopByReachDailyBudget", True)
        notification["emailSettings"].setdefault("email", "")
        _sd = bm.get("startDate")
        if not _sd:
            # Кампания ещё не видна read-реплике (token→Grid lag) → bm=дефолт БЕЗ startDate →
            # UpdateCampaigns валится DateDefectIds.MUST_BE_GREATER_THAN_OR_EQUAL_TO_MIN
            # (живой кейс tp5 2026-07-06, min=сегодня). Grid требует дату ≥ сегодня — ставим
            # сегодня по МСК (таймзона Директа).
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            _sd = _dt.now(_tz(_td(hours=3))).strftime("%Y-%m-%d")
        return {
            "id": str(cid),
            "name": str(bm.get("name") or ""),
            "state": bm.get("state") or "COMPLETE",
            "startDate": _sd,
            "endDate": bm.get("endDate"),
            "timeTarget": bm.get("timeTarget"),
            "notification": notification,
            "attributionModel": "AUTOMATIC",
            "broadMatch": {
                "broadMatchFlag": bool(bm.get("broadMatchFlag")),
                "broadMatchGoalId": bm.get("broadMatchGoalId"),
                "broadMatchLimit": int(bm.get("broadMatchLimit") or 0),
            },
            "dayBudget": str(bm.get("dayBudget") or "0"),
            "enableCompanyInfo": bool(bm.get("enableCompanyInfo")),
            "hasAddMetrikaTagToUrl": bool(bm.get("hasAddMetrikaTagToUrl")),
            "hasAddOpenstatTagToUrl": bool(bm.get("hasAddOpenstatTagToUrl")),
            "hasExtendedGeoTargeting": bool(bm.get("hasExtendedGeoTargeting")),
            "hasSiteMonitoring": bool(has_site_monitoring) if has_site_monitoring is not None else True,
            "hasTitleSubstitute": bool(has_title_substitute) if has_title_substitute is not None else True,
            "excludePausedCompetingAds": bool(bm.get("excludePausedCompetingAds")),
        }

    @staticmethod
    def _strategy_update_payload(row: dict) -> dict:
        strategy = row.get("strategy") or {}
        platforms = strategy.get("platforms") or {}
        budget = strategy.get("budget") or {}
        strategy_type = str(strategy.get("strategyType") or "")
        if strategy_type == "OPTIMIZE_CONVERSIONS":
            strategy_name = "AUTOBUDGET_AVG_CPA"
        elif strategy_type == "OPTIMIZE_CLICKS":
            # ⚠️ Во write-enum Грида НЕТ имени для «Максимум кликов» с недельным бюджетом:
            # AUTOBUDGET означает «Максимум конверсий» и МЕНЯЕТ стратегию кампании
            # (проверено на porg-qfnapixm/702916352). Такие кампании помечаются
            # _unsupported_strategy и пропускаются узкими апдейтами.
            if strategy.get("clicksLimit"):
                strategy_name = "AUTOBUDGET_WEEK_BUNDLE"
            elif strategy.get("avgBid"):
                strategy_name = "AUTOBUDGET_AVG_CLICK"
            else:
                strategy_name = "AUTOBUDGET"
        else:
            strategy_name = strategy.get("strategyName") or strategy_type or "AUTOBUDGET_AVG_CPA"
        return {
            "platforms": {
                "gallery": bool(platforms.get("gallery")),
                "network": bool(platforms.get("network")),
                "search": bool(platforms.get("search")),
                "telegram": bool(platforms.get("telegram")),
                "maxMessenger": bool(platforms.get("maxMessenger")),
                "taxi": bool(platforms.get("taxi")),
                "pillar": bool(platforms.get("pillar")),
                "cityBusDisplay": bool(platforms.get("cityBusDisplay")),
                "showcaseScreen": bool(platforms.get("showcaseScreen")),
                "mediafacade": bool(platforms.get("mediafacade")),
                "supersite": bool(platforms.get("supersite")),
                "billboard": bool(platforms.get("billboard")),
                "cityboard": bool(platforms.get("cityboard")),
                "cityformat": bool(platforms.get("cityformat")),
                "organic": bool(platforms.get("organic")),
                "serpGeoWizard": bool(platforms.get("serpGeoWizard")),
                "yandexMaps": bool(platforms.get("yandexMaps")),
            },
            "strategyData": {
                "goalId": str(strategy.get("goalId") or "0"),
                # avgCpa и sum добавляются ниже только если заданы:
                # «0» не проходит валидатор (MUST_BE_GREATER_THAN_OR_EQUAL_TO_MIN)
                **({"avgCpa": str(int(strategy.get("avgCpa") or 0))}
                   if int(strategy.get("avgCpa") or 0) > 0 else {}),
                "budgetType": "WEEKLY" if budget.get("period") == "WEEK" else str(budget.get("period") or "WEEKLY"),
                "payForConversion": bool(strategy.get("payForConversion")),
                "payForShows": bool(strategy.get("payForShows")),
                "autoApplyRecommendationOptions": {"budgetIncreasePercent": None},
                "isExplorationBudgetValueCustom": bool(strategy.get("isExplorationBudgetValueCustom")),
                **({"sum": str(int(budget.get("sum") or 0))} if int(budget.get("sum") or 0) > 0 else {}),
            },
            "strategyName": strategy_name,
        }

    @staticmethod
    def _notification_update_payload(row: dict) -> dict:
        notification = row.get("notification") or {}
        sms = notification.get("smsSettings") or {}
        email = notification.get("emailSettings") or {}
        events = []
        for event in sms.get("events") or []:
            if event.get("checked") and event.get("event"):
                events.append(event.get("event"))
        return {
            "smsSettings": {
                "smsTime": sms.get("smsTime") or {
                    "startTime": {"hour": 9, "minute": 0},
                    "endTime": {"hour": 21, "minute": 0},
                },
                "enableEvents": events,
            },
            "emailSettings": {
                "stopByReachDailyBudget": bool(email.get("stopByReachDailyBudget")),
                "email": email.get("email") or "",
            },
        }

    @staticmethod
    def _bid_modifiers_update_payload(row: dict) -> dict:
        out: dict = {}
        campaign_id = str(row.get("id") or "")
        for modifier in row.get("bidModifiers") or []:
            mtype = modifier.get("type")
            clean = {
                "campaignId": campaign_id,
                "enabled": bool(modifier.get("enabled")),
                "adjustments": [],
                "type": mtype,
            }
            for adj in modifier.get("adjustments") or []:
                item = {"percent": int(adj.get("percent") or 0), "id": str(adj.get("id") or "")}
                if mtype == "RETARGETING_MULTIPLIER":
                    item["retargetingConditionId"] = str(adj.get("retargetingConditionId") or "")
                elif mtype == "DEMOGRAPHY_MULTIPLIER":
                    item["age"] = adj.get("age")
                    item["gender"] = adj.get("gender")
                clean["adjustments"].append(item)
            if mtype == "RETARGETING_MULTIPLIER":
                out["bidModifierRetargeting"] = clean
            elif mtype == "DEMOGRAPHY_MULTIPLIER":
                out["bidModifierDemographics"] = clean
        return out

    @classmethod
    def _unified_campaign_update_from_edit_row(cls, row: dict) -> dict:
        """Build browser-shaped GdUnifiedCampaignInput from CampaignsEditData."""
        # startDate: у свежесозданной (token) кампании CampaignsEditData может отставать
        # (реплика) и отдать пустую/прошлую дату → UpdateCampaigns валится
        # DateDefectIds.MUST_BE_GREATER_THAN_OR_EQUAL_TO_MIN (min=сегодня; живой кейс tp5
        # 2026-07-06, вторая точка после _narrow_campaign_base). Поднимаем до «сегодня по МСК»
        # ТОЛЬКО пустую дату или прошлую у ЧЕРНОВИКА (primaryStatus DRAFT): у запущенной
        # кампании прошлый startDate легитимен, менять его нельзя.
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        _today_msk = _dt.now(_tz(_td(hours=3))).strftime("%Y-%m-%d")
        _sd = str(row.get("startDate") or "")
        _is_draft = str(row.get("primaryStatus") or "").upper() == "DRAFT"
        if not _sd or (_is_draft and _sd < _today_msk):
            row = dict(row)
            row["startDate"] = _today_msk
        promo = row.get("promoExtension") or {}
        callouts = (row.get("inheritableCallouts") or {}).get("assetValue") or []
        sitelink_set_id = (row.get("inheritableSitelinkSet") or {}).get("assetValue")
        additional = row.get("additionalData") or {}
        payload = _strip_graphql_typenames({
            "abExperiments": [],
            "abSegmentRetargetingConditionId": ((row.get("abSegmentRetargetingCondition") or {}).get("id")),
            "abSegmentStatisticRetargetingConditionId": ((row.get("abSegmentStatisticRetargetingCondition") or {}).get("id")),
            "name": row.get("name") or "",
            "enableCpcHold": bool(row.get("hasEnableCpcHold")),
            "contextLimit": int(row.get("contextLimit") or 100),
            "dynamicPlacesAdvTextsOnly": bool(row.get("dynamicPlacesAdvTextsOnly")),
            "dayBudget": str(int(float(row.get("dayBudget") or 0))),
            "attributionModel": row.get("attributionModel") or "AUTOMATIC",
            "metrikaCounters": [int(x) for x in (row.get("metrikaCounters") or []) if str(x).isdigit()],
            "meaningfulGoals": [],
            "strategyId": str(row.get("strategyId") or "0"),
            "biddingStategyWithPlatforms": cls._strategy_update_payload(row),
            "startDate": row.get("startDate"),
            "endDate": row.get("endDate"),
            "notification": cls._notification_update_payload(row),
            "hasTitleSubstitute": bool(row.get("hasTitleSubstitution")),
            "disabledPlaces": list(row.get("disabledPlaces") or []),
            "hasSiteMonitoring": True,
            "hasExtendedGeoTargeting": bool(row.get("hasExtendedGeoTargeting")),
            "disabledIps": row.get("disabledIps"),
            "hasAddOpenstatTagToUrl": bool(row.get("hasAddOpenstatTagToUrl")),
            "excludePausedCompetingAds": bool(row.get("excludePausedCompetingAds")),
            "enableCompanyInfo": bool(row.get("enableCompanyInfo")),
            "timeTarget": row.get("timeTarget"),
            "minusKeywords": list(row.get("minusKeywords") or []),
            "libraryMinusKeywordsIds": [str(x) for x in (row.get("libraryMinusKeywordsIds") or [])],
            "defaultPermalinkId": row.get("defaultPermalinkId"),
            "brandSafetyCategories": list(row.get("brandSafetyCategories") or []),
            "defaultTrackingPhoneId": row.get("defaultTrackingPhoneId"),
            "isOrderPhraseLengthPrecedenceEnabled": bool(row.get("isOrderPhraseLengthPrecedenceEnabled")),
            "placementTypes": row.get("placementTypes") or None,
            "promoExtensionId": str(promo.get("id")) if promo.get("id") else None,
            "deliveryId": row.get("deliveryId"),
            "bannerHrefParams": row.get("bannerHrefParams") or "",
            "isRecommendationsManagementEnabled": bool(row.get("isRecommendationsManagementEnabled")),
            "isPriceRecommendationsManagementEnabled": bool(row.get("isPriceRecommendationsManagementEnabled")),
            "isAlternativeTextsEnabled": bool(row.get("isAlternativeTextsEnabled")),
            "hasAddMetrikaTagToUrl": bool(row.get("hasAddMetrikaTagToUrl")),
            "bidModifiers": cls._bid_modifiers_update_payload(row),
            "isS2sTrackingEnabled": bool(row.get("isS2sTrackingEnabled")),
            "isUniversalCamp": bool(row.get("isUniversalCamp")),
            "broadMatch": _BROAD_MATCH_DEFAULT,
            "isOrganicSearchEnabled": bool(row.get("isOrganicSearchEnabled")),
            "inheritableCallouts": {"calloutIds": [str(x) for x in callouts]},
            "inheritableSitelinkSet": {"sitelinkSetId": str(sitelink_set_id) if sitelink_set_id else None},
            "useDiscounts": bool(row.get("useDiscounts")),
            "reserveHref": row.get("reserveHref"),
            "state": "COMPLETE",
            "id": str(row.get("id") or ""),
        })
        href = additional.get("href") or ""
        if href:
            # пустой href не проходит валидатор (EMPTY_HREF) — поле шлём только заполненным
            payload["additionalData"] = {"href": href}
        strategy_type = str((row.get("strategy") or {}).get("strategyType") or "")
        if strategy_type == "OPTIMIZE_CLICKS" and not (row.get("strategy") or {}).get("clicksLimit") \
                and not (row.get("strategy") or {}).get("avgBid"):
            # «Максимум кликов + недельный бюджет»: валидного write-имени нет,
            # полный апдейт сменил бы стратегию — узкие апдейты обязаны пропустить кампанию.
            payload["_unsupported_strategy"] = "Максимум кликов (недельный бюджет)"
        return payload

    @staticmethod
    def _narrow_bases(payloads: dict, ids: list[int], op: str) -> tuple[dict[int, dict], dict[int, str]]:
        """Подготовка payload'ов для узкого UpdateCampaigns.

        Возвращает ({cid: чистый payload}, {cid: причина пропуска}). Кампании с маркером
        _unsupported_strategy (например «Максимум кликов» — write-имени нет в enum Грида,
        полный апдейт сменил бы стратегию) уходят в skipped; служебные _-ключи зачищаются,
        чтобы не улететь в GraphQL-input. Отсутствие payload'а — фатально.
        """
        bases: dict[int, dict] = {}
        skipped: dict[int, str] = {}
        for cid in ids:
            base = payloads.get(cid)
            if not base:
                raise GridFinalizeError(f"{op}: не удалось прочитать кампанию {cid}")
            if base.get("_unsupported_strategy"):
                skipped[cid] = str(base["_unsupported_strategy"])
                continue
            bases[cid] = {k: v for k, v in base.items() if not k.startswith("_")}
        return bases, skipped

    def _read_unified_campaign_update_payloads(self, campaign_ids: list[int]) -> dict[int, dict]:
        ids = []
        for raw in campaign_ids or []:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                continue
            if cid > 0 and cid not in ids:
                ids.append(cid)
        if not ids:
            return {}
        self._bootstrap_csrf()
        out: dict[int, dict] = {}
        for chunk in [ids[i:i + 50] for i in range(0, len(ids), 50)]:
            inp = {
                "filter": {"campaignIdIn": [str(cid) for cid in chunk]},
                "orderBy": [{"field": "ID", "order": "ASC"}],
                "statRequirements": {"preset": "TODAY"},
                "limitOffset": {"offset": 0, "limit": len(chunk)},
            }
            r = self._post("CampaignsEditData", _CAMPAIGNS_EDIT_DATA_Q, {
                "login": self.login,
                "campaignInput": inp,
            })
            data = r.json()
            rows = (((data.get("data") or {}).get("client") or {})
                    .get("campaigns") or {}).get("rowset") or []
            # Частичные GraphQL-ошибки (например strategyLearningStatus падает у Яндекса
            # на батчах) не мешают чтению rowset — фатально только отсутствие данных.
            if data.get("errors") and not rows:
                raise GridFinalizeError(
                    "Grid read-campaign-edit-data: " + json.dumps(data.get("errors"), ensure_ascii=False)[:400])
            for row in rows:
                if row.get("__typename") != "GdUnifiedCampaign":
                    continue
                try:
                    cid = int(row.get("id") or 0)
                except (TypeError, ValueError):
                    continue
                if cid > 0:
                    out[cid] = self._unified_campaign_update_from_edit_row(row)
        return out

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
        payloads = self._read_unified_campaign_update_payloads(ids)
        q = ("mutation UpdateCampaigns($input:GdUpdateCampaignsInput!$login:String!){"
             "updateCampaigns(input:$input){updatedCampaigns{id}"
             "validationResult{errors{code params path}warnings{code params path}}}"
             "getClientMutationId(input:{login:$login}){mutationId}}")
        bases, skipped = self._narrow_bases(payloads, ids, "Grid set-callouts")
        if skipped:
            cid0, why = next(iter(skipped.items()))
            raise GridFinalizeError(
                f"Grid set-callouts: кампания {cid0}: стратегия «{why}» не поддерживается — пропущена")
        items = []
        for cid in ids:
            base = bases[cid]
            base["inheritableCallouts"] = {"calloutIds": [str(i) for i in co_ids]}
            items.append({"unifiedCampaign": base})
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

    def set_campaign_sitelink_set(self, campaign_ids: list[int], sitelink_set_id: int | str) -> list:
        """Attach one inheritable sitelink set to campaigns through Grid.

        Content editor uses this when a sitelink title/description changes:
        create a new SitelinkSet, then repoint campaigns from the old set to
        the new one. Ads in these campaigns inherit the campaign-level asset.
        """
        ids = []
        for raw in campaign_ids or []:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                continue
            if cid > 0 and cid not in ids:
                ids.append(cid)
        try:
            sid = int(sitelink_set_id)
        except (TypeError, ValueError):
            sid = 0
        if not ids or sid <= 0:
            return []
        payloads = self._read_unified_campaign_update_payloads(ids)
        q = ("mutation UpdateCampaigns($input:GdUpdateCampaignsInput!$login:String!){"
             "updateCampaigns(input:$input){updatedCampaigns{id}"
             "validationResult{errors{code params path}warnings{code params path}}}"
             "getClientMutationId(input:{login:$login}){mutationId}}")
        bases, skipped = self._narrow_bases(payloads, ids, "Grid set-sitelink-set")
        for cid, why in skipped.items():
            print(f"[grid] set-sitelink-set: кампания {cid} пропущена — стратегия «{why}»", flush=True)
        items = []
        for cid, base in bases.items():
            base["inheritableSitelinkSet"] = {"sitelinkSetId": str(sid)}
            items.append({"unifiedCampaign": base})
        if not items:
            return []
        r = self._post("UpdateCampaigns", q, {
            "login": self.login,
            "input": {"campaignUpdateItems": items},
        })
        data = r.json()
        res = (data.get("data") or {}).get("updateCampaigns") or {}
        vr = res.get("validationResult") or {}
        if data.get("errors") or vr.get("errors"):
            raise GridFinalizeError(
                "Grid set-sitelink-set: " + json.dumps(
                    data.get("errors") or vr.get("errors"), ensure_ascii=False)[:400])
        return res.get("updatedCampaigns") or []

    def set_campaign_disabled_places(self, campaign_ids: list[int], hosts: list[str]) -> list:
        """Set the campaign-level disabledPlaces (minus площадки) through a narrow Grid update.

        Copy-path use (П.13): apply our standard РСЯ minus-list to copied network
        campaigns. Like ``set_campaign_callouts`` this reads the full unified payload
        and rewrites ONLY ``disabledPlaces`` so strategy/placements stay untouched.
        """
        ids = []
        for raw in campaign_ids or []:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                continue
            if cid > 0 and cid not in ids:
                ids.append(cid)
        clean_hosts = [str(h).strip() for h in (hosts or []) if str(h).strip()]
        if not ids or not clean_hosts:
            return []
        payloads = self._read_unified_campaign_update_payloads(ids)
        q = ("mutation UpdateCampaigns($input:GdUpdateCampaignsInput!$login:String!){"
             "updateCampaigns(input:$input){updatedCampaigns{id}"
             "validationResult{errors{code params path}warnings{code params path}}}"
             "getClientMutationId(input:{login:$login}){mutationId}}")
        bases, skipped = self._narrow_bases(payloads, ids, "Grid set-disabled-places")
        for cid, why in skipped.items():
            print(f"[grid] set-disabled-places: кампания {cid} пропущена — стратегия «{why}»", flush=True)
        items = []
        for cid, base in bases.items():
            # MERGE: сохраняем ранее скопированные excluded-площадки + добавляем новые без дублей
            existing = list(base.get("disabledPlaces") or [])
            seen = set(existing)
            merged = existing + [h for h in clean_hosts if h not in seen]
            base["disabledPlaces"] = merged
            items.append({"unifiedCampaign": base})
        if not items:
            return []
        r = self._post("UpdateCampaigns", q, {
            "login": self.login,
            "input": {"campaignUpdateItems": items},
        })
        data = r.json()
        res = (data.get("data") or {}).get("updateCampaigns") or {}
        vr = res.get("validationResult") or {}
        if data.get("errors") or vr.get("errors"):
            raise GridFinalizeError(
                "Grid set-disabled-places: " + json.dumps(
                    data.get("errors") or vr.get("errors"), ensure_ascii=False)[:400])
        return res.get("updatedCampaigns") or []

    def read_campaign_invariants(self, campaign_ids: list[int]) -> dict[int, dict]:
        """Read campaign-level invariant галочки (blacklist toggles) via CampaignsEditData.

        Возвращает ``{cid: {field: tri-state}}`` для DoD-инвариантов кампании tp1–tp5:
        персонализация / расш.гео / «Директ помогает» / ценовые рек. / Карты (enableCompanyInfo) /
        Карты-платформа (yandexMaps) / список организаций (serpGeoWizard) / стратегия
        (payForConversion) + libraryMinusKeywordsIds. Каждое булево — **tri-state**: реальный
        ``True``/``False`` только если Grid вернул поле; иначе ``None`` (fail-safe — верификатор такое
        НЕ флагает, чтобы Grid-лаг/FieldUndefined не породил ложный детект и ложный ремонт, журнал I).
        ⚠️ ``hasSiteMonitoring`` (#4) в read-схеме Grid ОТСУТСТВУЕТ (нет в grid_campaigns_edit_data.graphql
        и в CampaignsBroadMatch) → не читается и НЕ детектируется отдельно; его лишь идемпотентно
        переставляет ``set_campaign_invariants`` (=True) при любом другом инвариант-ремонте.
        Fail-safe: любая ошибка запроса → пропуск кампании (её нет в ответе → verifier молчит)."""
        ids: list[int] = []
        for raw in campaign_ids or []:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                continue
            if cid > 0 and cid not in ids:
                ids.append(cid)
        if not ids:
            return {}
        self._bootstrap_csrf()

        def _tri(v):
            return bool(v) if isinstance(v, bool) else None

        out: dict[int, dict] = {}
        for chunk in [ids[i:i + 50] for i in range(0, len(ids), 50)]:
            inp = {
                "filter": {"campaignIdIn": [str(cid) for cid in chunk]},
                "orderBy": [{"field": "ID", "order": "ASC"}],
                "statRequirements": {"preset": "TODAY"},
                "limitOffset": {"offset": 0, "limit": len(chunk)},
            }
            r = self._post("CampaignsEditData", _CAMPAIGNS_EDIT_DATA_Q, {
                "login": self.login,
                "campaignInput": inp,
            })
            data = r.json()
            rows = (((data.get("data") or {}).get("client") or {})
                    .get("campaigns") or {}).get("rowset") or []
            # Частичные GraphQL-ошибки (strategyLearningStatus и пр. падают у Яндекса на батчах) не
            # мешают чтению rowset — фатально только полное отсутствие данных (тогда raise → guarded).
            if data.get("errors") and not rows:
                raise GridFinalizeError(
                    "Grid read-campaign-invariants: " + json.dumps(data.get("errors"), ensure_ascii=False)[:400])
            for row in rows:
                if row.get("__typename") != "GdUnifiedCampaign":
                    continue
                try:
                    cid = int(row.get("id") or 0)
                except (TypeError, ValueError):
                    continue
                if cid <= 0:
                    continue
                strat = row.get("strategy") if isinstance(row.get("strategy"), dict) else {}
                pf = strat.get("platforms") if isinstance(strat.get("platforms"), dict) else {}
                out[cid] = {
                    "is_alternative_texts_enabled": _tri(row.get("isAlternativeTextsEnabled")),
                    "has_extended_geo_targeting": _tri(row.get("hasExtendedGeoTargeting")),
                    "enable_company_info": _tri(row.get("enableCompanyInfo")),
                    "is_recommendations_management_enabled": _tri(row.get("isRecommendationsManagementEnabled")),
                    "is_price_recommendations_management_enabled": _tri(row.get("isPriceRecommendationsManagementEnabled")),
                    "yandex_maps_enabled": _tri(pf.get("yandexMaps")),
                    "serp_geo_wizard_enabled": _tri(pf.get("serpGeoWizard")),
                    "pay_for_conversion": _tri(strat.get("payForConversion")),
                    "library_minus_ids": [str(x) for x in (row.get("libraryMinusKeywordsIds") or [])],
                }
        return out

    def set_campaign_invariants(self, campaign_ids: list[int]) -> list:
        """Идемпотентно переставить кампанийные инварианты-галочки tp1–tp5 (in-place, БЕЗ баллов).

        Ремонт дыры P0 (DOD §1.c): re-apply кампанийного инвариант-блока финализации через узкий
        ``UpdateCampaigns`` (РК всегда DRAFT). Шаблон = ``set_campaign_disabled_places`` /
        ``set_campaign_placement_types``: читаем полный unified-payload из edit-view и переписываем
        ТОЛЬКО инвариантные поля (персонализация OFF, мониторинг ON, расш.гео OFF, «Директ помогает»
        OFF, ценовые рек. OFF, Карты/организации OFF), остальное (стратегия/ключи/места) — без
        изменений. Значения — те же константы, что при создании (``create_set_finalize:211-216`` /
        ``grid_finalize.finalize:280-291``) → идемпотентно, повторный вызов не меняет корректную РК.
        Блик-радиус ложного детекта = один безвредный повторный UpdateCampaigns (НЕ удаление, в отличие
        от recreate-ремонтов, журнал I)."""
        ids: list[int] = []
        for raw in campaign_ids or []:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                continue
            if cid > 0 and cid not in ids:
                ids.append(cid)
        if not ids:
            return []
        payloads = self._read_unified_campaign_update_payloads(ids)
        q = ("mutation UpdateCampaigns($input:GdUpdateCampaignsInput!$login:String!){"
             "updateCampaigns(input:$input){updatedCampaigns{id}"
             "validationResult{errors{code params path}warnings{code params path}}}"
             "getClientMutationId(input:{login:$login}){mutationId}}")
        bases, skipped = self._narrow_bases(payloads, ids, "Grid set-invariants")
        for _cid, _why in skipped.items():
            print(f"[grid] set-invariants: кампания {_cid} пропущена — стратегия «{_why}»", flush=True)
        items = []
        for cid, base in bases.items():
            base["isAlternativeTextsEnabled"] = False          # #3 персонализация ВЫКЛ
            base["hasSiteMonitoring"] = True                   # #4 мониторинг сайта ВКЛ
            base["hasExtendedGeoTargeting"] = False            # #5 расш.гео ВЫКЛ
            base["isRecommendationsManagementEnabled"] = False  # #6 «Директ помогает» ВЫКЛ
            base["isPriceRecommendationsManagementEnabled"] = False
            base["enableCompanyInfo"] = False                  # Карты/список организаций ВЫКЛ
            bs = base.get("biddingStategyWithPlatforms") if isinstance(base.get("biddingStategyWithPlatforms"), dict) else {}
            pf = bs.get("platforms") if isinstance(bs.get("platforms"), dict) else {}
            pf["yandexMaps"] = False                           # Карты — платформа ВЫКЛ
            pf["serpGeoWizard"] = False                        # список организаций (гео-колдунщик) ВЫКЛ
            bs["platforms"] = pf
            base["biddingStategyWithPlatforms"] = bs
            items.append({"unifiedCampaign": base})
        if not items:
            return []
        data = self._post_json_retry("UpdateCampaigns", q, {
            "login": self.login,
            "input": {"campaignUpdateItems": items},
        })
        res = (data.get("data") or {}).get("updateCampaigns") or {}
        vr = res.get("validationResult") or {}
        if data.get("errors") or vr.get("errors"):
            raise GridFinalizeError(
                "Grid set-invariants: " + json.dumps(
                    data.get("errors") or vr.get("errors"), ensure_ascii=False)[:400])
        return res.get("updatedCampaigns") or []

    def set_campaign_minus_keywords(self, campaign_ids: list[int], words: list[str]) -> list:
        """Идемпотентно добавить глобальные минус-слова НА УРОВЕНЬ КАМПАНИИ (inline minusKeywords)
        через узкий ``UpdateCampaigns`` (in-place, БЕЗ баллов, РК DRAFT). D6 2026-07-09
        (GLOBAL_MINUS_CAMPAIGN_MISSING): аддитивно к существующим inline-минусам; шаблон —
        ``set_campaign_invariants``. Union сохраняет порядок; повторный вызов не меняет корректную
        РК (слова уже есть → items пуст). ``libraryMinusKeywordsIds`` (shared-set) НЕ трогаем."""
        add = [str(w).strip() for w in (words or []) if str(w or "").strip()]
        ids: list[int] = []
        for raw in campaign_ids or []:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                continue
            if cid > 0 and cid not in ids:
                ids.append(cid)
        if not ids or not add:
            return []
        payloads = self._read_unified_campaign_update_payloads(ids)
        q = ("mutation UpdateCampaigns($input:GdUpdateCampaignsInput!$login:String!){"
             "updateCampaigns(input:$input){updatedCampaigns{id}"
             "validationResult{errors{code params path}warnings{code params path}}}"
             "getClientMutationId(input:{login:$login}){mutationId}}")
        bases, skipped = self._narrow_bases(payloads, ids, "Grid set-minus")
        for _cid, _why in skipped.items():
            print(f"[grid] set-minus: кампания {_cid} пропущена — стратегия «{_why}»", flush=True)
        items = []
        for cid, base in bases.items():
            cur = [str(m) for m in (base.get("minusKeywords") or [])]
            cur_low = {m.lower() for m in cur}
            missing = [w for w in add if w.lower() not in cur_low]
            if not missing:
                continue   # уже есть все — идемпотентно пропускаем
            base["minusKeywords"] = cur + missing
            items.append({"unifiedCampaign": base})
        if not items:
            return []
        data = self._post_json_retry("UpdateCampaigns", q, {
            "login": self.login,
            "input": {"campaignUpdateItems": items},
        })
        res = (data.get("data") or {}).get("updateCampaigns") or {}
        vr = res.get("validationResult") or {}
        if data.get("errors") or vr.get("errors"):
            raise GridFinalizeError(
                "Grid set-minus: " + json.dumps(
                    data.get("errors") or vr.get("errors"), ensure_ascii=False)[:400])
        return res.get("updatedCampaigns") or []

    def set_campaign_age_bidmods(self, campaign_ids: list[int],
                                 ages_percent: dict[str, int]) -> list[int]:
        """Set age demographic bid modifiers on campaigns through the narrow Grid RMW
        (UpdateCampaigns) — БЕЗ v5-баллов. ``ages_percent`` — {Grid-age-enum: percent},
        напр. {"_0_17": -100, "_18_24": -100} (−100% == исключить возраст).

        СЕМАНТИКА Grid: поле demographic-adjustment ``percent`` — это МУЛЬТИПЛИКАТОР 0..1300
        (min=0), а НЕ знаковая дельта. 100 = нейтрально (как v5 BidModifier=100+delta),
        0 = −100% (исключить), 130 = +30%. Вход ``ages_percent`` использует конвенцию «дельта»
        (−100..+1200), а конвертация delta→multiplier (``100 + pct``, clamp 0..1300) делается
        ЗДЕСЬ, в Grid-слое. Отрицательный percent Grid отвергает
        (``INVALID_PERCENT_SHOULD_BE_POSITIVE``) → раньше это уводило age в v5-фолбэк (баллы).

        Идемпотентно: возраст, у которого на кампании уже есть adjustment, пропускается.
        Возвращает список campaign_id, ГАРАНТИРОВАННО удовлетворённых (обновлены ИЛИ уже имели
        нужные возрасты). Бросает GridFinalizeError на validation error (→ v5-фолбэк у вызывающего)."""
        ids = []
        for raw in campaign_ids or []:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                continue
            if cid > 0 and cid not in ids:
                ids.append(cid)
        wanted = {str(k): int(v) for k, v in (ages_percent or {}).items() if str(k).strip()}
        if not ids or not wanted:
            return []
        payloads = self._read_unified_campaign_update_payloads(ids)
        q = ("mutation UpdateCampaigns($input:GdUpdateCampaignsInput!$login:String!){"
             "updateCampaigns(input:$input){updatedCampaigns{id}"
             "validationResult{errors{code params path}warnings{code params path}}}"
             "getClientMutationId(input:{login:$login}){mutationId}}")
        bases, skipped = self._narrow_bases(payloads, ids, "Grid set-age-bidmods")
        for _cid, _why in skipped.items():
            print(f"[grid] set-age-bidmods: кампания {_cid} пропущена — стратегия «{_why}»", flush=True)
        items = []
        satisfied: list[int] = []           # уже-ок (без апдейта)
        to_send: list[int] = []             # уходят в UpdateCampaigns
        for cid in ids:
            base = bases.get(cid)
            if base is None:
                continue  # пропущенная стратегия
            bm = base.get("bidModifiers")
            if not isinstance(bm, dict):
                bm = {}
                base["bidModifiers"] = bm
            dem = bm.get("bidModifierDemographics")
            if not isinstance(dem, dict) or not dem:
                dem = {"campaignId": str(cid), "enabled": True,
                       "adjustments": [], "type": "DEMOGRAPHY_MULTIPLIER"}
                bm["bidModifierDemographics"] = dem
            adjustments = list(dem.get("adjustments") or [])
            have_ages = {str(a.get("age")) for a in adjustments if a.get("age")}
            missing = {age: pct for age, pct in wanted.items() if age not in have_ages}
            if not missing:
                satisfied.append(cid)       # все нужные возрасты уже есть → без апдейта (0 запросов)
                continue
            for age, pct in missing.items():
                # Grid percent = мультипликатор 0..1300 (не знаковая дельта): delta→mult = 100+pct.
                # −100 → 0 (исключить), +30 → 130. clamp в допустимый диапазон.
                mult = max(0, min(1300, 100 + int(pct)))
                adjustments.append({"percent": mult, "id": None, "age": age, "gender": None})
            dem["adjustments"] = adjustments
            dem["enabled"] = True
            items.append({"unifiedCampaign": base})
            to_send.append(cid)
        if items:
            _vars = {"login": self.login, "input": {"campaignUpdateItems": items}}
            r = self._post("UpdateCampaigns", q, _vars)
            if r.status_code >= 500:                 # 1 ретрай на «внутреннюю ошибку сервера» Grid
                time.sleep(1.0)                       # (живой прогон: 500 → age ушёл в v5-фолбэк = баллы)
                r = self._post("UpdateCampaigns", q, _vars)
            try:
                data = r.json()
            except Exception as e:  # noqa: BLE001 — non-JSON (напр. 5xx HTML) не должен дать сырой JSONDecodeError
                raise GridFinalizeError(
                    f"Grid set-age-bidmods: bad json (HTTP {r.status_code}) {str(e)[:120]}") from e
            res = (data.get("data") or {}).get("updateCampaigns") or {}
            vr = res.get("validationResult") or {}
            if data.get("errors") or vr.get("errors"):
                raise GridFinalizeError(
                    "Grid set-age-bidmods: " + json.dumps(
                        data.get("errors") or vr.get("errors"), ensure_ascii=False)[:400])
            satisfied.extend(to_send)
        return satisfied

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
        # broadMatch is NonNull in GdUpdateCampaignsInput — read current value per campaign.
        _cids = []
        for _it in items:
            try:
                _cids.append(int((_it.get("unifiedCampaign") or {}).get("id") or 0))
            except (TypeError, ValueError):
                pass
        bm_map = self._read_broad_match_map([c for c in _cids if c > 0])
        items_with_bm = []
        for _it in items:
            _uc = _it.get("unifiedCampaign") or {}
            try:
                _cid = int(_uc.get("id") or 0)
            except (TypeError, ValueError):
                _cid = 0
            if _cid <= 0:
                continue
            _base = self._narrow_campaign_base(_cid, bm_map)
            _base["name"] = _uc.get("name") or ""
            items_with_bm.append({"unifiedCampaign": _base})
        q = ("mutation UpdateCampaigns($input:GdUpdateCampaignsInput!$login:String!){"
             "updateCampaigns(input:$input){updatedCampaigns{id}"
             "validationResult{errors{code params path}warnings{code params path}}}"
             "getClientMutationId(input:{login:$login}){mutationId}}")
        r = self._post("UpdateCampaigns", q, {
            "login": self.login,
            "input": {"campaignUpdateItems": items_with_bm},
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
        # Большие батчи режем на СЫРЫХ items ДО нормализации. Раньше рекурсия шла по уже
        # нормализованным rows (ключ camelCase ``adGroupId``), а нормализатор ниже читает только
        # ``adgroup_id``/``AdGroupId`` → gid=0 → все ключи тихо отбрасывались, add_keywords
        # возвращал [] (баг NO_KEYWORDS_LIVE на tp2 «Поиск-Марки» с >1000 ключей). Резка сырых
        # items сохраняет и adgroup_id, и price_context при повторной нормализации.
        if items and len(items) > 1000:
            out = []
            for i in range(0, len(items), 1000):
                out.extend(self.add_keywords(items[i:i + 1000]))
            return out
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
            # priceContext = сетевая ставка (v5 ContextBid). Аддитивно: старые вызовы (create-set
            # repair) не передают price_context → поведение не меняется. GdAddKeywordsItemInput
            # поддерживает priceContext (интроспекция 2026-07-03).
            if it.get("price_context") is not None:
                row["priceContext"] = it.get("price_context")
            clean.append(row)
        if not clean:
            return []
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
        items = []
        for s in sitelinks:
            title = (s.get("Title") or s.get("title") or "")[:30]
            href = s.get("Href") or s.get("href") or ""
            if not title or not href:
                continue
            item = {"title": title, "href": href}
            desc = (s.get("Description") or s.get("description") or "")[:60]
            if desc:
                # Grid-валидатор не принимает пустую строку (SITELINK_DESCRIPTION_CANNOT_BE_EMPTY) —
                # у ссылок без описания поле опускаем целиком.
                item["description"] = desc
            items.append(item)
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

    def get_sitelink_sets(self, sitelink_set_ids: list[int | str]) -> dict[int, list[dict]]:
        """Read sitelink set contents through Grid/cookies → {set_id: [{title, href, description}]}."""
        ids = []
        for raw in sitelink_set_ids or []:
            try:
                sid = int(raw)
            except (TypeError, ValueError):
                continue
            if sid > 0 and sid not in ids:
                ids.append(sid)
        if not ids:
            return {}
        self._bootstrap_csrf()
        q = (
            "query SitelinkSets($login:String!$sitelinkSetsInput:GdSitelinkSetsFilterInput!){"
            "client(searchBy:{login:$login}){sitelinkSets(input:$sitelinkSetsInput){"
            "id sitelinks{id title description href}}}}"
        )
        out: dict[int, list[dict]] = {}
        for chunk in [ids[i:i + 100] for i in range(0, len(ids), 100)]:
            r = self._post("SitelinkSets", q, {
                "login": self.login,
                "sitelinkSetsInput": {"sitelinkSetIdsIn": [str(sid) for sid in chunk]},
            })
            data = r.json()
            if data.get("errors"):
                raise GridFinalizeError(
                    "Grid get-sitelink-sets: " + json.dumps(data.get("errors"), ensure_ascii=False)[:400])
            rows = (((data.get("data") or {}).get("client") or {}).get("sitelinkSets") or [])
            for row in rows:
                try:
                    sid = int(row.get("id") or 0)
                except (TypeError, ValueError):
                    sid = 0
                if sid <= 0:
                    continue
                out[sid] = [{
                    "title": item.get("title") or "",
                    "href": item.get("href") or "",
                    "description": item.get("description") or "",
                } for item in (row.get("sitelinks") or [])]
        return out

    def set_default_text(self, shopping_ad_ids: list, feed_id: int, text: str,
                         filters_by_ad_id: dict | None = None) -> list:
        """«Текст по умолчанию» товарных объявлений (ShoppingAd) — поле bodies через
        UpdateShoppingAds (в v5 у ShoppingAd текстового поля нет). policy:INHERIT —
        не трогаем наследуемые от кампании уточнения/ссылки."""
        # F review: приватный Grid падает «Внутренняя ошибка сервера» на больших пачках (150 ShoppingAd
        # одним запросом). Чанкуем по _GRID_MUTATION_CHUNK (как add_shopping_ads), иначе bodies остаются пусты.
        if len(shopping_ad_ids or []) > _GRID_MUTATION_CHUNK:
            out: list = []
            import logging as _log_sdt
            _log_sdt = _log_sdt.getLogger("direct.finalize")
            for i in range(0, len(shopping_ad_ids), _GRID_MUTATION_CHUNK):
                try:
                    out.extend(self.set_default_text(shopping_ad_ids[i:i + _GRID_MUTATION_CHUNK],
                                                     feed_id, text, filters_by_ad_id))
                except GridFinalizeError as _sdt_ce:
                    _log_sdt.warning(
                        "set_default_text chunk %d/%d потерян (server error), skip; feed=%d: %s",
                        i // _GRID_MUTATION_CHUNK + 1,
                        -(-len(shopping_ad_ids) // _GRID_MUTATION_CHUNK),
                        feed_id, str(_sdt_ce)[:200])
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
        # token→Grid replication lag: свежесозданный ShoppingAd ещё не виден UpdateShoppingAds
        # → «внутренняя ошибка сервера» на первых 1-2 попытках (подтверждено логами, 2026-07-07).
        # Q2 (2026-07-08): снизили с 1.2с до 0.2с (безусловный пре-сон убирал ~60-90с на tp5/tp7).
        # Первая попытка сразу, 0.2с — минимальная страховка от ShoppingAd→Grid lag;
        # ретрай-петля ниже (_sdt_wait=(2,5)) ловит транзиентные ошибки если lag ещё жив.
        # A3: cookie-only — ShoppingAd создан САМИМ Grid, лага нет, пауза не нужна.
        if not self._cookie_only:
            time.sleep(0.2)
        _sdt_wait = (2, 5)
        for _sdt_att in range(3):
            r = self._post("UpdateShoppingAds", _SHOPPING_MUTATION,
                           {"updateShoppingInput": {"adUpdateItems": items, "saveDraft": True}})
            data = r.json()
            if data.get("errors") and _is_transient_data_error(data["errors"]) and _sdt_att < 2:
                import logging as _log_sdt_r
                _log_sdt_r.getLogger("direct.finalize").warning(
                    "set_default_text server error attempt %d, retry in %ds; feed=%d login=%s",
                    _sdt_att + 1, _sdt_wait[_sdt_att], feed_id, self.login)
                time.sleep(_sdt_wait[_sdt_att])
                continue
            break
        res = (data.get("data") or {}).get("updateShoppingAds") or {}
        vr_upd_errs = (res.get("validationResult") or {}).get("errors") or []
        if data.get("errors") or vr_upd_errs:
            # Bug C fix: UNKNOWN_FIELD в UpdateShoppingAds → feedFilter содержит поле, которого
            # нет в фиде (напр. vendor для AUTO_RU). Снимаем feedFilter и ретраим (текст сохраняется).
            # С исправлением Bug A caller теперь передаёт правильный brand_field → UNKNOWN_FIELD
            # здесь маловероятен, но оставляем как страховку.
            _has_unk = any("UNKNOWN_FIELD" in str(e.get("code") or "") for e in vr_upd_errs)
            if _has_unk and not data.get("errors"):
                import logging as _log_dt
                _log_dt.getLogger("direct.finalize").warning(
                    "set_default_text UNKNOWN_FIELD: снимаем feedFilter, ретрай без фильтра; "
                    "feed=%d login=%s", feed_id, self.login)
                _items_no_ff = [{k: v for k, v in it.items() if k != "feedFilter"} for it in items]
                r2 = self._post("UpdateShoppingAds", _SHOPPING_MUTATION,
                                {"updateShoppingInput": {"adUpdateItems": _items_no_ff, "saveDraft": True}})
                d2 = r2.json()
                res2 = (d2.get("data") or {}).get("updateShoppingAds") or {}
                if not (d2.get("errors") or (res2.get("validationResult") or {}).get("errors")):
                    return res2.get("updatedAds") or []
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
            # brand_field/model_field: разрешённые имена полей для этого фида.
            # Caller (create_set_tp1_builders) выставляет их через _resolve_feed_field.
            # Фолбэк "vendor"/"model" — для обратной совместимости и если probe не запускался.
            _brand_fld = it.get("brand_field") or "vendor"
            _model_fld = it.get("model_field") or "model"
            if it.get("vendor"):
                # Регистр зависит от ФИДА (HAR42/43). CONTAINS_ANY case-sensitive → передаём оба регистра.
                _vv = str(it["vendor"])
                _variants = list(dict.fromkeys([_vv, _vv.lower(), _vv.title()]))
                conds.append({"field": _brand_fld, "operator": "CONTAINS_ANY",
                              "stringValue": json.dumps(_variants, ensure_ascii=False)})
            # МОДЕЛЬ (Модели-группы): доп. условие по model_field (AUTO_RU: folder_id; YML: model).
            # Фид может не иметь поля → UNKNOWN_FIELD → ретрай без него (ниже).
            _mv = it.get("model")
            if _mv:
                _mvals = _mv if isinstance(_mv, list) else [str(_mv)]
                _mvals = [str(x) for x in _mvals if str(x).strip()]
                if _mvals:
                    conds.append({"field": _model_fld, "operator": "CONTAINS_ANY",
                                  "stringValue": json.dumps(_mvals, ensure_ascii=False)})
            if not conds and it.get("collection_id"):
                # collectionId требует EQUALS_ANY (НЕ CONTAINS_ANY → Grid даёт INVALID_OPERATOR и
                # ShoppingAd не создаётся). brand_fld — CONTAINS_ANY (строка), collectionId — EQUALS_ANY.
                conds.append({"field": "collectionId", "operator": "EQUALS_ANY",
                              "stringValue": json.dumps([str(it["collection_id"])], ensure_ascii=False)})
            # Глобальные минус-марки: «марка/модель НЕ содержит …» — используем ТОТ ЖЕ brand_fld/model_fld.
            # Добавляем ПОСЛЕ brand/model/collectionId, в т.ч. для ct0000 (тогда — к всей витрине).
            try:
                from . import create_set_feeds as _csf
                conds.extend(_csf._minus_marks_grid_conditions(brand_field=_brand_fld, model_field=_model_fld))
            except Exception:  # noqa: BLE001 — минус-марки best-effort
                pass
            if conds:
                entry["feedFilter"] = {"tab": "CONDITION", "conditions": conds}
            ad_items.append(entry)
        if not ad_items:
            return []
        self._bootstrap_csrf()
        q = ("mutation AddShoppingAds($addShoppingInput:GdAddShoppingAdsInput!){"
             "addShoppingAds(input:$addShoppingInput){addedAds{id}"
             "validationResult{errors{code params path}}}}")
        # token→Grid replication lag (группа C 2026-07-06): свежесозданная токеном кампания/группа
        # ещё не видна мутации → *_NOT_FOUND. Ретраим ЗДЕСЬ, на уровне ЧАНКА (метод почанковый,
        # ≤50 items) и ТОЛЬКО при полном отказе (addedAds пуст) — внешний ретрай целого батча в
        # caller'е дублировал ShoppingAd уже успешных чанков (ревью 06.07). Узкие коды: FEED_NOT_
        # EXIST/UNKNOWN_FIELD не транзиентны, их лечат свои ветки ниже.
        for _lag_try in range(3):
            r = self._post("AddShoppingAds", q,
                           {"addShoppingInput": {"adAddItems": ad_items, "saveDraft": True}})
            data = r.json()
            res = (data.get("data") or {}).get("addShoppingAds") or {}
            vr_errors = (res.get("validationResult") or {}).get("errors") or []
            _lag = any(any(t in str(e.get("code") or "") for t in
                           ("CAMPAIGN_NOT_FOUND", "ADGROUP_NOT_FOUND", "AD_GROUP_NOT_FOUND"))
                       for e in vr_errors)
            if _lag and not (res.get("addedAds") or []) and _lag_try < 2:
                time.sleep(1.2 * (_lag_try + 1))
                continue
            break
        if data.get("errors") or vr_errors:
            # UNKNOWN_FIELD: фид не поддерживает одно или несколько полей условия (model, vendor
            # или иное — зависит от формата: yandex.xml авто не имеет <vendor>).
            # Парсим path каждой ошибки вида "adAddItems[N].feedFilter.conditions[M]" → field в M-й
            # позиции → собираем bad_fields и снимаем именно их (обобщение, не хардкод "model").
            # Fallback: если paths непарсируемы — полный сброс feedFilter (товарка по всему фиду).
            has_unknown = any("UNKNOWN_FIELD" in str(e.get("code") or "") for e in vr_errors)
            if has_unknown and not data.get("errors"):
                import re as _re_uf
                bad_fields: set = set()
                for _uf_e in vr_errors:
                    if "UNKNOWN_FIELD" not in str(_uf_e.get("code") or ""):
                        continue
                    _uf_p = str(_uf_e.get("path") or "")
                    _uf_m = _re_uf.search(
                        r"adAddItems\[(\d+)\]\.feedFilter\.conditions\[(\d+)\]", _uf_p)
                    if _uf_m:
                        _ni, _ci = int(_uf_m.group(1)), int(_uf_m.group(2))
                        if _ni < len(ad_items):
                            _ff0 = ad_items[_ni].get("feedFilter") or {}
                            _cc0 = _ff0.get("conditions") or []
                            if _ci < len(_cc0):
                                _bf = _cc0[_ci].get("field")
                                if _bf:
                                    bad_fields.add(_bf)
                _strip_all_ff = not bad_fields   # не смогли распарсить path → ядерный fallback
                # Предупреждение: сообщаем какие поля сняты (помогает выявить фиды без нужных полей)
                import logging as _log_uf
                _log_uf.getLogger("direct.finalize").warning(
                    "add_shopping_ads UNKNOWN_FIELD: bad_fields=%r strip_all=%s login=%s",
                    bad_fields, _strip_all_ff, self.login)
                _stripped = []
                for it in ad_items:
                    it2 = dict(it)
                    ff = it2.get("feedFilter")
                    if ff and ff.get("conditions"):
                        if _strip_all_ff:
                            it2.pop("feedFilter", None)
                        else:
                            _c = [c for c in ff["conditions"] if c.get("field") not in bad_fields]
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
                # поле-специфичный стрип не помог → ядерный fallback: полный сброс feedFilter
                if not _strip_all_ff:
                    _nuked = [{k: v for k, v in it.items() if k != "feedFilter"}
                              for it in ad_items]
                    r4 = self._post("AddShoppingAds", q,
                                    {"addShoppingInput": {"adAddItems": _nuked, "saveDraft": True}})
                    d4 = r4.json()
                    res4 = (d4.get("data") or {}).get("addShoppingAds") or {}
                    if not (d4.get("errors") or (res4.get("validationResult") or {}).get("errors")):
                        return [a.get("id") for a in (res4.get("addedAds") or []) if a.get("id")]
                # все ретраи не вышли → общая обработка ниже (FEED_NOT_EXIST / raise),
                # data/res ОСТАЮТСЯ исходными (первый ответ с UNKNOWN_FIELD)
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
        # ⛔ adGroupId в addedAds НЕ запрашивать: GdAddListingAdByShoppingAdItem его НЕ имеет —
        # FieldUndefined валил ВСЮ мутацию (инцидент 03.07 15:36-41: ListingAd=0 на новых
        # кампаниях; live-откат проверен — листинг создался). shoppingAdId — валидное поле
        # (fix-3 08.07.2026): позволяет матчить листинг → name_value без adGroupId.
        q = ("mutation AddListingAdsByShoppingAds($input:GdAddListingAdsByShoppingAdsInput!){"
             "addListingAdsByShoppingAds(input:$input){addedAds{id shoppingAdId}"
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
        import logging as _log_lnf
        _lnf_log = _log_lnf.getLogger("direct.finalize")
        # F review: чанкинг — приватный Grid падает 500 на больших пачках (как set_default_text/add_shopping_ads).
        if len(items or []) > _GRID_MUTATION_CHUNK:
            total = 0
            for i in range(0, len(items), _GRID_MUTATION_CHUNK):
                try:
                    total += self.set_listing_name_filters(items[i:i + _GRID_MUTATION_CHUNK])
                except GridFinalizeError as _lnf_ce:
                    _lnf_log.warning(
                        "set_listing_name_filters chunk %d потерян, skip: %s",
                        i // _GRID_MUTATION_CHUNK + 1, str(_lnf_ce)[:200])
                time.sleep(0.15)
            return total
        # D4 (backlog H, 2026-07-09): поле name-фильтра резолвим через _resolve_feed_field(...,'name')
        # тем же механизмом, что brand/model. У AUTO_RU yandex.xml поля `name` в fieldsForUseAs НЕТ →
        # захардкоженный {field:'name'} валил updateListingAds с UNAVAILABLE_FIELD, чанк терялся молча
        # (listing_name_set=0, «Страницы каталога» = весь фид). Фолбэк — 'name' (Market-фиды).
        _name_field_cache: dict = {}

        def _resolve_name_field(_fid) -> str:
            _fid = int(_fid or 0)
            if _fid in _name_field_cache:
                return _name_field_cache[_fid]
            _fld = "name"
            try:
                from . import create_set_feeds as _csf_nf
                _fld = _csf_nf._resolve_feed_field(self.login, _fid, "name") or "name"
            except Exception:  # noqa: BLE001 — фолбэк на 'name' при сбое резолва
                _fld = "name"
            _name_field_cache[_fid] = _fld
            return _fld

        def _build_upd(field_override) -> list:
            _u: list = []
            for it in (items or []):
                val = (it.get("value") or "").strip()
                _item_id = it.get("id")
                # adGroupId отсутствует в GdUpdateListingAdInput (fix-3 08.07.2026) — id листинга
                # обязан приходить через ключ "id" (shoppingAdId-матч); без id — пропуск.
                if not _item_id or not it.get("feed_id") or not val:
                    continue
                _fld = field_override or _resolve_name_field(it["feed_id"])
                _lnf_conds = [{"field": _fld, "operator": "CONTAINS_ANY",
                               "stringValue": json.dumps([val], ensure_ascii=False)}]
                if it.get("extra_conds"):
                    _lnf_conds.extend(it["extra_conds"])
                _u.append({
                    "id": str(_item_id),
                    "permalinkWithPhone": {"policy": "CLEAR"},
                    "fieldsToUseAsBody": None, "fieldsToUseAsName": None,
                    "feedId": str(it["feed_id"]),
                    "feedFilter": {"tab": "CONDITION", "conditions": _lnf_conds},
                    "bodies": list(it.get("bodies") or []),
                    "hrefParams": "",
                    "inheritableCallouts": {"policy": "INHERIT"},
                    "inheritableSitelinkSet": {"policy": "INHERIT"},
                })
            return _u

        def _errs_have_unavailable_field(_errs) -> bool:
            for _e in (_errs or []):
                _c = str((_e or {}).get("code") or "")
                if "UNAVAILABLE_FIELD" in _c or "UNKNOWN_FIELD" in _c or "INVALID_FIELD" in _c:
                    return True
            return False

        # НЕ терять чанк молча при UNAVAILABLE_FIELD: последовательность полей-кандидатов —
        # per-feed резолв (override=None) → доступные текстовые поля фида → явный 'name' (last-resort).
        _first_fid = int((items[0] or {}).get("feed_id") or 0) if items else 0
        _alt_overrides: list = [None]
        try:
            from . import create_set_feeds as _csf_af
            _avail_f = _csf_af._feed_filter_fields(self.login, _first_fid)
            for _cand in ("name", "model", "modification", "folder_id"):
                if _cand in _avail_f and _cand not in _alt_overrides:
                    _alt_overrides.append(_cand)
        except Exception:  # noqa: BLE001
            pass
        if "name" not in _alt_overrides:
            _alt_overrides.append("name")

        self._bootstrap_csrf()
        q = ("mutation updateListingAds($updateListingInput:GdUpdateListingAdsInput!){"
             "updateListingAds(input:$updateListingInput){updatedAds{id}"
             "validationResult{errors{code params path}}}}")
        _lnf_wait = (2, 5)
        _last_err = None
        for _oi, _ovr in enumerate(_alt_overrides):
            upd = _build_upd(_ovr)
            if not upd:
                return 0
            data: dict = {}
            for _lnf_att in range(3):
                r = self._post("updateListingAds", q,
                               {"updateListingInput": {"adUpdateItems": upd, "saveDraft": True}})
                data = r.json()
                if data.get("errors") and _is_transient_data_error(data["errors"]) and _lnf_att < 2:
                    _lnf_log.warning(
                        "set_listing_name_filters server error attempt %d, retry in %ds; login=%s",
                        _lnf_att + 1, _lnf_wait[_lnf_att], self.login)
                    time.sleep(_lnf_wait[_lnf_att])
                    continue
                break
            res = (data.get("data") or {}).get("updateListingAds") or {}
            _verrs = (res.get("validationResult") or {}).get("errors") or []
            if data.get("errors") or _verrs:
                _last_err = data.get("errors") or _verrs
                # UNAVAILABLE_FIELD → ретрай чанка со следующим полем-кандидатом (не терять молча).
                if _errs_have_unavailable_field(_verrs) and _oi + 1 < len(_alt_overrides):
                    _lnf_log.warning(
                        "set_listing_name_filters UNAVAILABLE_FIELD (field=%s feed=%s login=%s) → "
                        "ретрай с полем '%s'",
                        _ovr or _resolve_name_field(_first_fid), _first_fid, self.login,
                        _alt_overrides[_oi + 1] or "resolved")
                    continue
                raise GridFinalizeError("updateListingAds(name-filter): " + json.dumps(
                    _last_err, ensure_ascii=False)[:400])
            return len(res.get("updatedAds") or [])
        raise GridFinalizeError("updateListingAds(name-filter): " + json.dumps(
            _last_err, ensure_ascii=False)[:400])

    def set_product_feed_filters(self, items: list, *, listing: bool = False) -> int:
        """Проставить ПРОИЗВОЛЬНЫЙ feedFilter товарным (updateShoppingAds) или каталожным
        (updateListingAds) объявлениям. Live-подтверждено 03.07.2026 на camp 712120488:
        vendor NOT_CONTAINS_ALL ["uaz"] встал и читается назад. Полный item обязателен
        (permalinkWithPhone/bodies/inheritable* — иначе internal error, как у name-фильтров).
        items: [{id, feed_id, conditions:[{field,operator,stringValue}], bodies}].
        → число обновлённых. Бросает GridFinalizeError при ошибке (UNKNOWN_FIELD — тоже:
        вызывающий решает, пропускать ли фид без поля)."""
        if len(items or []) > _GRID_MUTATION_CHUNK:
            total = 0
            for i in range(0, len(items), _GRID_MUTATION_CHUNK):
                try:
                    total += self.set_product_feed_filters(
                        items[i:i + _GRID_MUTATION_CHUNK], listing=listing)
                except GridFinalizeError as _pff_ce:
                    import logging as _log_pff
                    _log_pff.getLogger("direct.finalize").warning(
                        "set_product_feed_filters chunk %d потерян, skip: %s",
                        i // _GRID_MUTATION_CHUNK + 1, str(_pff_ce)[:200])
                time.sleep(0.15)
            return total
        upd = []
        for it in (items or []):
            conds = list(it.get("conditions") or [])
            if not it.get("id") or not it.get("feed_id") or not conds:
                continue
            upd.append({
                "id": str(it["id"]),
                "feedId": str(it["feed_id"]),
                "feedFilter": {"tab": "CONDITION", "conditions": conds},
                "bodies": list(it.get("bodies") or []),
                "hrefParams": "",
                "fieldsToUseAsBody": None, "fieldsToUseAsName": None,
                "permalinkWithPhone": {"policy": "CLEAR"},
                "inheritableCallouts": {"policy": "INHERIT"},
                "inheritableSitelinkSet": {"policy": "INHERIT"},
            })
        if not upd:
            return 0
        self._bootstrap_csrf()
        op = "updateListingAds" if listing else "updateShoppingAds"
        gtype = "GdUpdateListingAdsInput" if listing else "GdUpdateShoppingAdsInput"
        q = ("mutation %s($inp:%s!){%s(input:$inp){updatedAds{id}"
             "validationResult{errors{code params path}}}}" % (op, gtype, op))
        _pff_wait = (2, 5)
        for _pff_att in range(3):
            r = self._post(op, q, {"inp": {"adUpdateItems": upd, "saveDraft": True}})
            data = r.json()
            if data.get("errors") and _is_transient_data_error(data["errors"]) and _pff_att < 2:
                import logging as _log_pff2
                _log_pff2.getLogger("direct.finalize").warning(
                    "set_product_feed_filters server error attempt %d, retry in %ds; login=%s",
                    _pff_att + 1, _pff_wait[_pff_att], self.login)
                time.sleep(_pff_wait[_pff_att])
                continue
            break
        res = (data.get("data") or {}).get(op) or {}
        if data.get("errors") or (res.get("validationResult") or {}).get("errors"):
            raise GridFinalizeError(f"{op}(feed-filter): " + json.dumps(
                data.get("errors") or res.get("validationResult"), ensure_ascii=False)[:400])
        return len(res.get("updatedAds") or [])

    def set_campaign_placement_types(self, campaign_ids: list[int],
                                     placement_types: list[str]) -> list:
        """Узкий UpdateCampaigns: только placementTypes («Места показа → Ручная настройка»).
        Шаблон = set_campaign_sitelink_set (narrow-мутации обязаны эхом вернуть broadMatch
        и базовый скелет — _read_unified_campaign_update_payloads). Для tp5 эталон —
        PLACEMENTS_TP5 (Товарная галерея + Продвижение в поисковой выдаче)."""
        ids = []
        for raw in campaign_ids or []:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                continue
            if cid > 0 and cid not in ids:
                ids.append(cid)
        pts = [str(p) for p in (placement_types or []) if p]
        if not ids or not pts:
            return []
        payloads = self._read_unified_campaign_update_payloads(ids)
        q = ("mutation UpdateCampaigns($input:GdUpdateCampaignsInput!$login:String!){"
             "updateCampaigns(input:$input){updatedCampaigns{id}"
             "validationResult{errors{code params path}warnings{code params path}}}"
             "getClientMutationId(input:{login:$login}){mutationId}}")
        bases, skipped = self._narrow_bases(payloads, ids, "Grid set-placements")
        for _cid, _why in skipped.items():
            print(f"[grid] set-placements: кампания {_cid} пропущена — стратегия «{_why}»", flush=True)
        items = []
        for cid, base in bases.items():
            base["placementTypes"] = pts
            items.append({"unifiedCampaign": base})
        if not items:
            return []
        # _post_json_retry: 403/CSRF + транзиент-ретраи (правило «tries+backoff в HTTP»)
        data = self._post_json_retry("UpdateCampaigns", q, {
            "login": self.login,
            "input": {"campaignUpdateItems": items},
        })
        res = (data.get("data") or {}).get("updateCampaigns") or {}
        vr = res.get("validationResult") or {}
        if data.get("errors") or vr.get("errors"):
            raise GridFinalizeError(
                "Grid set-placements: " + json.dumps(
                    data.get("errors") or vr.get("errors"), ensure_ascii=False)[:400])
        return res.get("updatedCampaigns") or []

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
        fname = _os.path.basename(image_path or "")
        try:
            if not _os.path.isfile(image_path):
                print(f"[img-upload] FAIL {self.login} {fname}: файл не найден ({image_path})",
                      flush=True)
                return None
            if not self.csrf:                          # CSRF живёт на клиенте — не бутстрапить
                self._bootstrap_csrf()                 # заново на каждую картинку
            url = f"https://direct.yandex.ru/web-api/image/upload?ulogin={self.login}"
            headers = {
                "Cookie": self.cookie,
                "User-Agent": cmc.USER_AGENT,
                "Origin": "https://direct.yandex.ru",
                "Referer": f"https://direct.yandex.ru/dna/grid/campaigns?ulogin={self.login}",
            }
            if self.csrf:
                headers["x-csrf-token"] = self.csrf
            # ЧИТАЕМ ФАЙЛ В ПАМЯТЬ до POST: путь бывает на sshfs (Мак M3) — стрим файл-хэндла
            # в requests растягивал ОТПРАВКУ тела на минуты/бесконечно (read-timeout=60 меряет
            # только ответ, не отправку) → воркер висел в ssl.read, watchdog убивал джобу
            # (live-стек 2026-07-02 21:39, job 0bf287c861f2: upload_image → ssl.read).
            with open(image_path, "rb") as fh:
                _img_bytes = fh.read()
            files = {"files": (fname, _img_bytes, "image/jpeg")}
            data = {"image_type": "BANNER_TEXT"}
            r = self.sess.post(url, files=files, data=data, headers=headers, timeout=60)
            if r.status_code == 403:
                # CSRF протух — добираем свежий и повторяем с ним (те же bytes, без sshfs).
                # Если ре-бутстрап токен не дал — стейл-заголовок УБИРАЕМ, а не шлём повторно
                # тот же, что только что дал 403 (сессия могла получить свежую куку токена).
                self.csrf = None
                self._bootstrap_csrf()
                if self.csrf:
                    headers["x-csrf-token"] = self.csrf
                else:
                    headers.pop("x-csrf-token", None)
                r = self.sess.post(url, files=files, data=data, headers=headers, timeout=60)
            j = r.json()
            result = ((j.get("result") or [None])[0]) or {}
            h = result.get("hash") or None
            if not h:
                print(f"[img-upload] FAIL {self.login} {fname}: HTTP {r.status_code} "
                      f"resp={r.text[:200]!r}", flush=True)
            return h
        except Exception as e:  # noqa: BLE001
            print(f"[img-upload] FAIL {self.login} {fname}: {type(e).__name__}: {str(e)[:160]}",
                  flush=True)
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
                "imageHashes": list(it.get("imageHashes") or []),
                # видео-креативы вызывающего (напр. из adaptive_ads_for_update.creativeIds) —
                # раньше жёсткий [] стирал видео при чистке картинок (ревью 03.07 #13)
                "creativeIds": [str(c) for c in (it.get("creativeIds") or []) if c],
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

    def adaptive_ads_for_update(self, campaign_ids: list[int], ad_ids: list[int]) -> dict[int, dict]:
        """Read full adaptive ads needed for safe ``UpdateAdaptiveTextAds`` round-trip.

        Grid update replaces the editable ad payload, so content editor must
        preserve href, titles, bodies, images, and price while changing only
        the requested text fragment.
        """
        cids: list[int] = []
        for raw in campaign_ids or []:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                continue
            if cid > 0 and cid not in cids:
                cids.append(cid)
        wanted: set[int] = set()
        for raw in ad_ids or []:
            try:
                aid = int(raw)
            except (TypeError, ValueError):
                continue
            if aid > 0:
                wanted.add(aid)
        if not cids or not wanted:
            return {}
        self._bootstrap_csrf()
        # typedCreatives{creativeId} — ЧИТАЕМЫЙ источник видео-креативов (подтверждено live
        # 03.07.2026, интроспекция): закрывает давнюю дыру «creativeIds нечитаем → RMW стирает
        # видео». hasButton/button — для детекта и добивки кнопки «Получить скидку».
        q = ("query AdaptiveAdsForUpdate($login:String!,$inp:GdAdsContainerInput!){"
             "client(searchBy:{login:$login}){ads(input:$inp){rowset{id campaignId "
             "...on GdAdaptiveTextAd{href titles bodies images{imageHash} "
             "bannerPrice{price priceOld prefix currency} "
             "hasVideo hasButton button{action href} "
             "typedCreatives{creativeId creativeType}}"
             "}}}}")
        out: dict[int, dict] = {}
        for chunk in [cids[i:i + 100] for i in range(0, len(cids), 100)]:
            inp = {
                "filter": {"campaignIdIn": [str(cid) for cid in chunk]},
                "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
                "limitOffset": {"limit": 5000, "offset": 0},
                "orderBy": [{"order": "ASC", "field": "ID"}],
            }
            data = self._post_json_retry(
                "AdaptiveAdsForUpdate",
                q,
                {"login": self.login, "inp": inp},
            )
            rows = ((((data.get("data") or {}).get("client") or {})
                     .get("ads") or {}).get("rowset") or [])
            for row in rows:
                try:
                    aid = int(row.get("id"))
                except (TypeError, ValueError):
                    continue
                if aid not in wanted:
                    continue
                image_hashes = []
                for image in row.get("images") or []:
                    image_hash = (image or {}).get("imageHash")
                    if image_hash:
                        image_hashes.append(image_hash)
                # видео-креативы: только VIDEO_ADDITION (другие типы в creativeIds Grid не ждёт)
                creative_ids = [str(c.get("creativeId")) for c in (row.get("typedCreatives") or [])
                                if c and c.get("creativeId")
                                and (c.get("creativeType") or "") == "VIDEO_ADDITION"]
                out[aid] = {
                    "id": aid,
                    "href": row.get("href") or "",
                    "titles": list(row.get("titles") or []),
                    "bodies": list(row.get("bodies") or []),
                    "imageHashes": image_hashes,
                    "adPrice": row.get("bannerPrice"),
                    "creativeIds": creative_ids,
                    "hasVideo": bool(row.get("hasVideo")),
                    "hasButton": bool(row.get("hasButton")),
                    "button": row.get("button"),
                }
        return out

    def video_creative_urls(self, campaign_ids: list[int], ad_ids: list[int]) -> dict[str, dict]:
        """Скачиваемые URL видео-креативов (VIDEO_ADDITION) по куки → {creative_id: {...}}.

        ФАЗА 3c п.12: Grid-интроспекция (2026-07-03) вскрыла тип ``GdVideoAdditionCreative`` с
        полем ``originalUrl`` — это ПРЯМОЙ mp4 исходника (``https://storage.mds.yandex.net/get-bstor/
        …*.mp4``, отдаётся HTTP 200 ``video/mp4`` БЕЗ авторизации — проверено live). Читаем его по
        куки ИСТОЧНИКА, чтобы перенести ролик 1:1. Возвращаем и запасные ``livePreviewUrl``/
        ``previewUrl`` на случай пустого originalUrl.
        """
        cids: list[int] = []
        for raw in campaign_ids or []:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                continue
            if cid > 0 and cid not in cids:
                cids.append(cid)
        wanted: set[int] = set()
        for raw in ad_ids or []:
            try:
                aid = int(raw)
            except (TypeError, ValueError):
                continue
            if aid > 0:
                wanted.add(aid)
        if not cids or not wanted:
            return {}
        self._bootstrap_csrf()
        q = ("query VideoCreativeUrls($login:String!,$inp:GdAdsContainerInput!){"
             "client(searchBy:{login:$login}){ads(input:$inp){rowset{id campaignId "
             "...on GdAdaptiveTextAd{typedCreatives{creativeId creativeType "
             "...on GdVideoAdditionCreative{originalUrl livePreviewUrl previewUrl duration}}}"
             "}}}}")
        out: dict[str, dict] = {}
        for chunk in [cids[i:i + 100] for i in range(0, len(cids), 100)]:
            inp = {
                "filter": {"campaignIdIn": [str(cid) for cid in chunk]},
                "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
                "limitOffset": {"limit": 5000, "offset": 0},
                "orderBy": [{"order": "ASC", "field": "ID"}],
            }
            data = self._post_json_retry(
                "VideoCreativeUrls", q, {"login": self.login, "inp": inp})
            rows = ((((data.get("data") or {}).get("client") or {})
                     .get("ads") or {}).get("rowset") or [])
            for row in rows:
                try:
                    aid = int(row.get("id"))
                except (TypeError, ValueError):
                    continue
                if aid not in wanted:
                    continue
                for c in (row.get("typedCreatives") or []):
                    if not c or (c.get("creativeType") or "") != "VIDEO_ADDITION":
                        continue
                    ccid = str(c.get("creativeId") or "").strip()
                    if not ccid:
                        continue
                    out[ccid] = {
                        "creative_id": ccid,
                        "original_url": c.get("originalUrl") or "",
                        "live_preview_url": c.get("livePreviewUrl") or "",
                        "preview_url": c.get("previewUrl") or "",
                        "duration": c.get("duration"),
                    }
        return out

    def update_adaptive_text_ads(self, ad_items: list[dict]) -> int:
        """Update adaptive ads text fields through Grid and raise on validation errors."""
        upd = []
        for it in ad_items or []:
            if not it.get("id"):
                continue
            item = {
                "href": it.get("href") or "",
                "hrefParams": "",
                "domain": None,
                "titles": list(it.get("titles") or []),
                "bodies": list(it.get("bodies") or []),
                "imageHashes": list(it.get("imageHashes") or []),
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
        self._bootstrap_csrf()
        data = self._post_json_retry(
            "UpdateAdaptiveTextAds",
            q,
            {"updateInput": {"adUpdateItems": upd, "saveDraft": True}},
        )
        res = (data.get("data") or {}).get("updateAdaptiveTextAds") or {}
        vr = res.get("validationResult") or {}
        if data.get("errors") or vr.get("errors"):
            raise GridFinalizeError(
                "Grid update-adaptive-texts: " + json.dumps(
                    data.get("errors") or vr.get("errors"), ensure_ascii=False)[:500])
        return len(res.get("updatedAds") or [])

    def find_and_replace_text(
        self,
        ad_ids: list[int],
        *,
        target_types: list[str],
        search: str,
        replace: str,
        case_sensitive: bool = True,
        sitelink_title_order_nums: list[int] | None = None,
        sitelink_description_order_nums: list[int] | None = None,
        sitelink_href_order_nums: list[int] | None = None,
    ) -> dict:
        """Run Direct Grid mass find-and-replace for ad text fields.

        This is the cookie/Grid path used by the content editor for old
        ``GdTextAd`` and newer adaptive ads. ``target_types`` are Grid enum
        values: ``TITLE``, ``TITLE_EXTENSION``, ``BODY``, ``SITELINK_TITLE``.
        """
        ids = []
        for raw in ad_ids or []:
            try:
                aid = int(raw)
            except (TypeError, ValueError):
                continue
            if aid > 0 and aid not in ids:
                ids.append(aid)
        targets = []
        allowed = {"TITLE", "TITLE_EXTENSION", "BODY", "SITELINK_TITLE", "SITELINK_DESCRIPTION", "SITELINK_HREF"}
        for raw in target_types or []:
            target = str(raw or "").strip().upper()
            if target in allowed and target not in targets:
                targets.append(target)
        if not ids or not targets or not str(search or ""):
            return {"replaced": 0, "total": 0, "rowset": [], "errors": []}
        self._bootstrap_csrf()
        q = ("mutation FindAndReplaceText($input:GdFindAndReplaceTextInput!){"
             "findAndReplaceText(input:$input){successCount totalCount "
             "rowset{adId}validationResult{errors{code params path}"
             "warnings{code params path}}}}")
        def _clean_order_nums(values: list[int] | None) -> list[int]:
            clean: list[int] = []
            for raw in values or []:
                try:
                    num = int(raw)
                except (TypeError, ValueError):
                    continue
                if num > 0 and num not in clean:
                    clean.append(num)
            return clean

        variables = {
            "input": {
                "adIds": [str(i) for i in ids],
                "cacheKey": None,
                "limitOffset": {"limit": len(ids), "offset": 0},
                "targetTypes": targets,
                "replaceInstruction": {
                    "search": str(search),
                    "replace": str(replace),
                    "options": {
                        "caseSensitive": bool(case_sensitive),
                        "linkReplacementMode": "FULL",
                        "replacementMode": "FIND_AND_REPLACE",
                        "sitelinkOrderNumsToUpdateDescription": _clean_order_nums(sitelink_description_order_nums),
                        "sitelinkOrderNumsToUpdateHref": _clean_order_nums(sitelink_href_order_nums),
                        "sitelinkOrderNumsToUpdateTitle": _clean_order_nums(sitelink_title_order_nums),
                    },
                },
            }
        }
        data = self._post_json_retry("FindAndReplaceText", q, variables)
        res = (data.get("data") or {}).get("findAndReplaceText") or {}
        vr = res.get("validationResult") or {}
        if data.get("errors") or vr.get("errors"):
            raise GridFinalizeError(
                "Grid find-replace-text: " + json.dumps(
                    data.get("errors") or vr.get("errors"), ensure_ascii=False)[:500])
        return {
            "replaced": int(res.get("successCount") or 0),
            "total": int(res.get("totalCount") or 0),
            "rowset": res.get("rowset") or [],
            "errors": [],
        }

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

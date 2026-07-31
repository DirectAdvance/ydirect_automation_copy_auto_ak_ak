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

import contextlib
import json
import re
import threading
import time
from pathlib import Path

import requests
import urllib3

try:                                    # пакетный контекст (blueprint: from . import …)
    from ..core import campaign as cmc       # USER_AGENT, pick_working_cookie, DirectV501Client
except ImportError:                     # плоский запуск (локальные тесты из direct/)
    import campaign as cmc

try:
    from ..text_norm import _strip_href_fragment
except ImportError:                     # плоский запуск (локальные тесты из direct/)
    from text_norm import _strip_href_fragment

try:                                    # STAGE_TIMING: пер-стадийный замер (только замер + лог)
    from ..create import stage_timing as _timing
except ImportError:                     # плоский запуск (локальные тесты из direct/)
    import stage_timing as _timing

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
# ⚠️ Жёсткий потолок строк на секцию (Grid: offset-пагинация ЗА этот предел не работает). Ответ
# ровно на лимит = усечён → см. ``groups_for_edit(meta=...)``: по ключам такой набор судить нельзя.
_GFE_LIMIT = 10000
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

# tp5 «Поиск + Товарная галерея»: ручная настройка задаётся placementTypes=null
# и platforms gallery+search+organic. Непустой список Direct может свернуть в UI-пресет.
TP5_PLACEMENT_TYPES = None
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


# ── Картинки Grid: превью и write-shape наследуемых наборов ─────────────────────────────────
# Живой HAR браузера (direct.yandex.ru.62har.har, entry [55] BannersQueryForEdit + GET'ы картинок):
# у GdImage есть formats[]{imageSize{height width} path}, а превью отдаётся по
# https://direct.yandex.ru/images + path (подтверждено 200 image/webp на /x300, /y484, /x600).
_GRID_IMAGE_BASE = "https://direct.yandex.ru/images"
_GRID_IMAGE_PREVIEW_W = 300   # средний размер: браузер грузит именно /x300 в списке объявлений

# Пагинация чтения объявлений Grid (client.ads): страница + потолок числа страниц.
# 5000 — предел, который Grid отдаёт за один limitOffset; 200 страниц = 1 000 000 объявлений,
# заведомо больше любого аккаунта, и при этом сервис не виснет на кривом ответе API.
_ADS_PAGE_LIMIT = 5000
_ADS_PAGE_MAX_PAGES = 200


def _safe_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _grid_image_preview_url(formats) -> str:
    """URL превью картинки по её formats[]: берём формат с шириной ближе всего к 300px.
    Форматов нет / нет path → "" (вызывающий покажет плейсхолдер)."""
    best_path, best_delta = "", None
    for fmt in formats or []:
        path = str((fmt or {}).get("path") or "").strip()
        if not path:
            continue
        width = _safe_int(((fmt or {}).get("imageSize") or {}).get("width"))
        delta = abs(width - _GRID_IMAGE_PREVIEW_W) if width else 10 ** 6
        if best_delta is None or delta < best_delta:
            best_path, best_delta = path, delta
    return (_GRID_IMAGE_BASE + best_path) if best_path else ""


def _grid_images_rich(raw_images) -> list[dict]:
    """images{} из Grid → [{imageHash, name, mdsGroupId, width, height, preview_url}, …]."""
    out: list[dict] = []
    for img in raw_images or []:
        if not isinstance(img, dict):
            continue
        image_hash = str(img.get("imageHash") or "").strip()
        if not image_hash:
            continue
        size = img.get("imageSize") or {}
        out.append({
            "imageHash": image_hash,
            "name": img.get("name") or "",
            "mdsGroupId": str(img.get("mdsGroupId") or ""),
            "width": _safe_int(size.get("width")),
            "height": _safe_int(size.get("height")),
            "preview_url": _grid_image_preview_url(img.get("formats")),
        })
    return out


def _grid_multicards_write(raw_multicards) -> list[dict]:
    """Read shape ``multicards`` -> UpdateAdaptiveTextAds write shape.

    Browser HAR for adaptive carousel sends only scalar card fields plus ``imageHash``.
    Keeping this in every RMW payload prevents unrelated text/price/image updates from
    wiping an existing carousel.
    """
    out: list[dict] = []
    seen_hashes: set[str] = set()
    for card in raw_multicards or []:
        if not isinstance(card, dict):
            continue
        image = card.get("image") if isinstance(card.get("image"), dict) else {}
        image_hash = str(card.get("imageHash") or image.get("imageHash") or "").strip()
        if not image_hash or image_hash in seen_hashes:
            continue
        seen_hashes.add(image_hash)
        out.append({
            "imageHash": image_hash,
            "currency": card.get("currency") or None,
            "href": card.get("href") or None,
            "price": card.get("price") or None,
            "priceOld": card.get("priceOld") or None,
            "text": card.get("text") or None,
        })
    return out


def _grid_inheritable_write(raw, value_key: str | None) -> dict | None:
    """Прочитанный ``{policy, assetValue}`` → write-shape для UpdateAdaptiveTextAds.

    Асимметрия имён (как linkTail→displayHref): читается ``assetValue``, пишется ``sitelinkSetId``
    (HAR entry [187]: ``{"policy":"OVERRIDE","sitelinkSetId":"1494667558"}``) либо ``calloutIds``
    (интроспекция 2026-07-18: ``GdInheritableCalloutsInput{calloutIds:[ID] calloutsIds:[ID]
    policy:GdAssetInheritancePolicyInput!}``; каноничное имя — ``calloutIds``: оно есть в 20+ input-
    типах схемы, а ``calloutsIds`` — ровно в двух и всегда дублем рядом с ним, т.е. легаси-алиас).

    ``assetValue`` бывает СКАЛЯРОМ (набор быстрых ссылок — один id) и СПИСКОМ (уточнения — список
    id, живой probe porg-pvrbl7mh: ``{"policy":"OVERRIDE","assetValue":["43516097",…]}``), поэтому
    форма значения выводится из самого значения, а не задаётся вызывающим.
    ``value_key=None`` = write-shape значения для этого набора НЕ подтверждена → при ``OVERRIDE``
    возвращаем None, чтобы вызывающий взял свой fallback, а не слал догадку.
    """
    if not isinstance(raw, dict):
        return None
    policy = str(raw.get("policy") or "").strip().upper()
    if not policy:
        return None
    if policy == "OVERRIDE":
        value = raw.get("assetValue")
        if not value_key:
            # write-shape значения не подтверждена → не гадаем, вызывающий берёт свой fallback
            return None
        if value in (None, "", [], {}):
            # OVERRIDE с ПУСТЫМ значением = «у объявления набора нет, кампанийный не наследуем».
            # Возврат None давал fallback INHERIT (update_ad_images:2283) → объявление получало
            # уточнения/ссылки КАМПАНИИ, которых у него не было. Семантически это CLEAR
            # (то же значение шлёт браузер в HAR entry [187] для пустых уточнений).
            return {"policy": "CLEAR"}
        if isinstance(value, (list, tuple)):
            ids = [str(v) for v in value if v not in (None, "")]
            if not ids:
                return {"policy": "CLEAR"}
            return {"policy": "OVERRIDE", value_key: ids}
        return {"policy": "OVERRIDE", value_key: str(value)}
    return {"policy": policy}


class GridFinalizeError(RuntimeError):
    pass


# Транзиентные сбои Grid, при которых ПОВТОР мутации имеет смысл. Маркеры русские: Grid отвечает
# с Accept-Language ru, и «Внутренняя ошибка сервера … reqId = …» (наблюдалась live 2026-07-19,
# job b0d25ad114c5, кампания 712885317) без явного маркера транзиентом не считалась вовсе.
# ⚠️ Ошибки ВАЛИДАЦИИ этих маркеров не содержат → под ретрай не подпадают (как и в
# yandex_gateway._TRANSIENT_MARKERS — тот же принцип, свой список: там v5 JSON, здесь GraphQL).
_GRID_TRANSIENT_MARKERS = (
    "внутренняя ошибка сервера", "временно недоступ", "сервис недоступен", "сервер недоступен",
    "попробуйте позже", "повторите", "сервер занят",
    "internal server error", "timeout", "timed out", "unavailable", "gateway", "bad gateway",
)
# Держим коротким: finalize идёт внутри джобы на десятки кампаний, длинный backoff растянет прогон.
_GRID_RETRY_TRIES = 3
_GRID_RETRY_BACKOFF = (2, 5)


def _grid_errors_transient(errors) -> bool:
    """True, если ответ Grid содержит транзиентную ошибку (повтор идемпотентной мутации оправдан)."""
    if not errors:
        return False
    try:
        blob = json.dumps(errors, ensure_ascii=False).lower()
    except (TypeError, ValueError):
        blob = str(errors).lower()
    return any(marker in blob for marker in _GRID_TRANSIENT_MARKERS)


def _grid_updated_ad_ids(res: dict) -> list[str]:
    """Реально обновлённые объявления из ответа ``update*Ads`` — элементы с непустым ``id``.

    ⚠️ Считать длину ``updatedAds`` НЕЛЬЗЯ: при отказе Директ отдаёт список ТОЙ ЖЕ ДЛИНЫ из
    ``null`` (живой probe 2026-07-19, porg-gcegsszl camp 704132838: 15 items отклонены
    ``ACTION_IN_ARCHIVED_CAMPAIGN``, HTTP 200, ``updatedAds:[null ×15]``) — ``len()`` давал
    ``replaced:15, errors:[]`` при НУЛЕ изменённых объявлений. Сигнатура
    ``GRID_UPDATE_ADS_NULL_ITEMS_FALSE_SUCCESS`` в ERRORS_JOURNAL.
    """
    out: list[str] = []
    for x in (res.get("updatedAds") or []):
        if isinstance(x, dict) and x.get("id"):
            out.append(str(x["id"]))
    return out


def _grid_failed_ad_ids(res: dict, sent_items: list[dict]) -> list[str]:
    """Id отправленных объявлений, которым Grid не вернул ``updatedAds.id``.

    ``updatedAds`` в observed-ответах позиционный: успешный элемент = ``{"id": ...}``,
    отказ = ``null`` на той же позиции. Если Grid вернул список короче отправленного,
    недостающий хвост тоже считаем отказом.
    """
    returned = list((res or {}).get("updatedAds") or [])
    failed: list[str] = []
    for idx, it in enumerate(sent_items or []):
        row = returned[idx] if idx < len(returned) else None
        if isinstance(row, dict) and row.get("id"):
            continue
        aid = str((it or {}).get("id") or "").strip()
        if aid:
            failed.append(aid)
    return failed


def _grid_validation_reasons(res: dict, data: dict | None = None) -> list[str]:
    """Человекочитаемые причины отказа из ``validationResult`` + GraphQL-уровня ``errors``.

    Формат элемента: ``CODE @path (params)`` — код нужен для журнала, path/params показывают,
    какой именно item отклонён. Warnings добавляются отдельным префиксом ``warning:``.
    """
    def _fmt(e, prefix: str = "") -> str:
        if not isinstance(e, dict):
            return f"{prefix}{str(e)[:160]}"
        code = e.get("code") or e.get("message") or "?"
        path = e.get("path")
        params = e.get("params")
        s = f"{prefix}{code}"
        if path:
            s += f" @{path if isinstance(path, str) else json.dumps(path, ensure_ascii=False)}"
        if params:
            s += f" ({json.dumps(params, ensure_ascii=False)[:120]})"
        return s[:240]

    vr = (res or {}).get("validationResult") or {}
    reasons = [_fmt(e) for e in (vr.get("errors") or [])]
    reasons += [_fmt(e, "warning: ") for e in (vr.get("warnings") or [])]
    reasons += [_fmt(e) for e in ((data or {}).get("errors") or [])]
    return reasons


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
        self._reauth_depth = 0
        # Причины неполной записи последнего update_ad_images / update_text_ad_images.
        # Методы возвращают ЧИСЛО, а причина отказа обязана быть видна наверху (инвариант
        # «не отработало → видно в результате задания»), поэтому кладём её сюда.
        self.last_ad_update_errors: list[str] = []

    def _post(self, op: str, query: str, variables: dict) -> requests.Response:
        def _looks_like_login_page(resp: requests.Response) -> bool:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "json" in ctype:
                return False
            head = (resp.text or "")[:500].lower()
            return "<html" in head and ("<title>log in</title>" in head or "passport.yandex" in head)

        def _remember_csrf(resp: requests.Response) -> None:
            m = re.search(r"_direct_csrf_token=([^;,\s]+)", resp.headers.get("Set-Cookie", ""))
            tok = resp.cookies.get("_direct_csrf_token") or (m.group(1) if m else None)
            if tok:
                self.csrf = tok

        headers = {
            "Cookie": self.cookie, "dna-operation-name": op, "x-direct-api": "1",
            "x-detected-locale": "ru", "Content-Type": "application/json",
            "User-Agent": cmc.USER_AGENT, "Origin": "https://direct.yandex.ru",
            "Referer": f"https://direct.yandex.ru/dna/grid/campaigns?ulogin={self.login}",
        }
        if self.csrf:
            headers["x-csrf-token"] = self.csrf
        url = f"{GRID_URL}?operationName={op}&ulogin={self.login}"
        # STAGE_TIMING: замер одной Grid-операции (только внутри item-контекста создания набора —
        # тот же транспорт в content/copy сервисах строк не пишет). Поведение не меняется.
        # ⚠️ Вложенный вызов НЕ мерим: при stale-cookie 403 внешний _post зовёт _reauth →
        # _bootstrap_csrf → _post("Callouts"). Строка grid:Callouts легла бы ВНУТРЬ внешней
        # grid:<op>, и агрегация group_by(.stage)|map(ms|add) посчитала бы это время дважды
        # (сумма стадий > item_total) — ровно там, где мы ищем аномалию. Признак вложенности —
        # уже существующий self._reauth_depth (>0 только внутри _reauth). Верхнеуровневый
        # _bootstrap_csrf (вне _post) при этом мерится как раньше: это реальное время item'а.
        _tm = (contextlib.nullcontext() if getattr(self, "_reauth_depth", 0)
               else _timing.stage(f"grid:{op}", only_in_item=True))
        with _tm:
            # БЕЗ транспортного ретрая: add_shopping_ads/add_listing_ads/add_callouts/add_keywords —
            # НЕ идемпотентны (обрыв ответа после commit + ретрай = ДУБЛЬ). Идемпотентные RMW-сеттеры
            # (disabledPlaces/age/callouts full-RMW) переживают единичный обрыв через ре-ран джобы.
            _had_csrf = self.csrf is not None   # A2-heal: различаем bootstrap-403 и stale-cookie-403
            r = self.sess.post(url, json={"operationName": op, "query": query, "variables": variables},
                               headers=headers, timeout=40)
            _remember_csrf(r)
            # A2-heal: если CSRF уже был установлен, но всё равно 403 — кука протухла после
            # кэширования (ротация сессии Яндекса). Переподхватываем куку + CSRF и повторяем ОДИН раз.
            # Случай первого bootstrap-403 (_had_csrf=False) сюда не попадает — им управляет
            # _bootstrap_csrf (ретрай снаружи). Рекурсии нет: _reauth обнуляет self.csrf → вложенный
            # вызов _post из _bootstrap_csrf видит _had_csrf=False и не заходит в эту ветку.
            if ((r.status_code == 403 and _had_csrf) or _looks_like_login_page(r)) and not getattr(self, "_reauth_depth", 0):
                reason = "stale-cookie 403" if r.status_code == 403 else "login-page"
                print(f"[grid] {reason} {self.login}/{op}: reauth → retry", flush=True)
                self._reauth()
                headers["Cookie"] = self.cookie
                if self.csrf:
                    headers["x-csrf-token"] = self.csrf
                r = self.sess.post(url, json={"operationName": op, "query": query, "variables": variables},
                                   headers=headers, timeout=40)
                _remember_csrf(r)
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
        if getattr(self, "_reauth_depth", 0):
            raise RuntimeError(f"Grid reauth уже выполняется для ulogin={self.login}")
        self._reauth_depth = int(getattr(self, "_reauth_depth", 0)) + 1
        self.csrf = None
        try:
            if not self._explicit_cookie:
                self.cookie = cmc.pick_working_cookie(self.login, force_refresh=True)
            self._bootstrap_csrf()
            if not self.csrf:
                raise RuntimeError(f"Grid reauth не получил CSRF для ulogin={self.login}")
        finally:
            self._reauth_depth = max(0, int(getattr(self, "_reauth_depth", 1)) - 1)

    def post_idempotent(self, op: str, query: str, variables: dict, *,
                        tries: int = _GRID_RETRY_TRIES) -> requests.Response:
        """``_post`` + ретрай ТРАНЗИЕНТНЫХ сбоев. ТОЛЬКО для ИДЕМПОТЕНТНЫХ мутаций.

        Разрешено для операций, идемпотентных по Id (``UpdateCampaigns`` — перезапись полей
        СУЩЕСТВУЮЩЕЙ кампании теми же значениями: повтор даёт тот же результат).
        ⛔ Для ``add*``-мутаций ЗАПРЕЩЕНО: обрыв после commit + повтор = дубли объектов
        (см. комментарий в ``_post`` и журнал RETRY_ON_NETWORK_LOSS_DUPLICATES_ADD) — именно
        поэтому ретрай живёт отдельным методом, а не внутри ``_post``.

        Повторяем: транспортный сбой (исключение requests), HTTP 5xx и ответ 200 с транзиентной
        ошибкой Директа. Ошибки валидации маркеров не содержат → возвращаются вызывающему сразу,
        с прежним поведением. Исчерпали попытки — возвращаем ПОСЛЕДНИЙ ответ (вызывающий разберёт
        его и бросит свою ошибку, как раньше); если ответа не было ни разу — пробрасываем исключение.
        """
        attempts = max(1, int(tries))
        resp: requests.Response | None = None
        last_exc: Exception | None = None
        for attempt in range(attempts):
            if attempt:
                time.sleep(_GRID_RETRY_BACKOFF[min(attempt - 1, len(_GRID_RETRY_BACKOFF) - 1)])
            try:
                resp = self._post(op, query, variables)
            except Exception as exc:  # noqa: BLE001 — транспортный сбой идемпотентной мутации
                last_exc = exc
                print(f"[grid] {op}: транспортный сбой ({str(exc)[:120]}), "
                      f"попытка {attempt + 1}/{attempts}", flush=True)
                continue
            last_exc = None
            if resp.status_code >= 500:
                print(f"[grid] {op}: HTTP {resp.status_code}, попытка {attempt + 1}/{attempts}",
                      flush=True)
                continue
            try:
                data = resp.json()
            except ValueError:
                return resp
            if _grid_errors_transient(data.get("errors")):
                print(f"[grid] {op}: транзиентная ошибка Директа, "
                      f"попытка {attempt + 1}/{attempts}", flush=True)
                continue
            return resp
        if resp is None and last_exc is not None:
            raise last_exc
        return resp

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
        placement_types: None/[] → placementTypes=null; явный список — legacy override.
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
        # UpdateCampaigns идемпотентна по id → транзиентный 500/обрыв ретраится (post_idempotent).
        r = self.post_idempotent("UpdateCampaigns", _MUTATION,
                                 {"input": {"campaignUpdateItems": [{"unifiedCampaign": uc}]},
                                  "login": self.login})
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
        budget_sum = int(budget.get("sum") or 0)
        avg_bid = int(strategy.get("avgBid") or 0)
        if strategy_type == "OPTIMIZE_CONVERSIONS":
            if int(strategy.get("avgCpa") or 0) > 0:
                strategy_name = "AUTOBUDGET_AVG_CPA"
            else:
                # avgCpa=None/0 → стратегия «Максимум конверсий» (WB_MAXIMUM_CONVERSION_RATE в v5).
                # Grid write-enum для неё — AUTOBUDGET (AUTOBUDGET_AVG_CPA требует ненулевой avgCpa
                # → CANNOT_BE_NULL). Это round-trip, не смена стратегии. 2026-07-17 porg-jh2si7rh.
                strategy_name = "AUTOBUDGET"
        elif strategy_type == "OPTIMIZE_CLICKS":
            if strategy.get("clicksLimit"):
                strategy_name = "AUTOBUDGET_WEEK_BUNDLE"
            elif avg_bid > 0 or budget_sum > 0:
                # HAR direct.yandex.ru.67har.har (UpdateCampaigns, 2026-07-20):
                # UI пишет «Максимум кликов + недельный бюджет» как AUTOBUDGET_AVG_CLICK
                # со strategyData.avgBid + sum + budgetType=WEEKLY. Когда Grid read отдаёт
                # avgBid=None у уже созданного черновика, используем UI-дефолт 100 руб.,
                # иначе узкие апдейты снова будут пропускать такие кампании.
                strategy_name = "AUTOBUDGET_AVG_CLICK"
            else:
                strategy_name = "AUTOBUDGET"
        elif strategy_type == "DEFAULT":
            # Grid read returns DEFAULT, but UpdateCampaigns expects DEFAULT_.
            # Verified live on porg-mushirne/712796008: read-back keeps strategyType=DEFAULT.
            strategy_name = "DEFAULT_"
        elif strategy_type == "MULTIPLE_CPA":
            # Grid read returns MULTIPLE_CPA, while write enum is AUTOBUDGET_MULTIPLE_CPA.
            # Verified live on porg-mushirne/712829915 without strategy semantic change.
            strategy_name = "AUTOBUDGET_MULTIPLE_CPA"
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
                **({"avgBid": str(avg_bid if avg_bid > 0 else 100)}
                   if strategy_name == "AUTOBUDGET_AVG_CLICK" else {}),
                "budgetType": "WEEKLY" if budget.get("period") == "WEEK" else str(budget.get("period") or "WEEKLY"),
                "payForConversion": bool(strategy.get("payForConversion")),
                "payForShows": bool(strategy.get("payForShows")),
                "autoApplyRecommendationOptions": {"budgetIncreasePercent": None},
                "isExplorationBudgetValueCustom": bool(strategy.get("isExplorationBudgetValueCustom")),
                **({"sum": str(budget_sum)} if budget_sum > 0 else {}),
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
            "dynamicPlacesAdvTextsOnly": bool(row.get("dynamicPlacesAdvTextsOnly")),
            "dayBudget": str(int(float(row.get("dayBudget") or 0))),
            "attributionModel": row.get("attributionModel") or "AUTOMATIC",
            "metrikaCounters": [int(x) for x in (row.get("metrikaCounters") or []) if str(x).isdigit()],
            "meaningfulGoals": (row.get("meaningfulGoals") or []),
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
        if strategy_type == "OPTIMIZE_CLICKS":
            _strategy = row.get("strategy") or {}
            _budget = _strategy.get("budget") or {}
            if not _strategy.get("clicksLimit") and not _strategy.get("avgBid") \
                    and int(_budget.get("sum") or 0) <= 0:
                payload["_unsupported_strategy"] = "Максимум кликов (без лимита кликов/avgBid/бюджета)"
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
        if not ids:
            return []
        self._bootstrap_csrf()
        payloads = self._read_unified_campaign_update_payloads(ids)
        q = ("mutation UpdateCampaigns($input:GdUpdateCampaignsInput!$login:String!){"
             "updateCampaigns(input:$input){updatedCampaigns{id}"
             "validationResult{errors{code params path}warnings{code params path}}}"
             "getClientMutationId(input:{login:$login}){mutationId}}")
        bases, skipped = self._narrow_bases(payloads, ids, "Grid set-callouts")
        for cid, why in skipped.items():
            print(f"[grid] set-callouts: кампания {cid} пропущена — стратегия «{why}»", flush=True)
        items = []
        for cid, base in bases.items():
            base["inheritableCallouts"] = {"calloutIds": [str(i) for i in co_ids]}
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
                "Grid set-callouts: " + json.dumps(
                    data.get("errors") or vr.get("errors"), ensure_ascii=False)[:400])
        return res.get("updatedCampaigns") or []

    def set_campaign_sitelink_set(self, campaign_ids: list[int], sitelink_set_id: int | str | None) -> list:
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
        sid_raw = None
        if sitelink_set_id not in (None, "", 0, "0"):
            try:
                sid_raw = str(int(sitelink_set_id))
            except (TypeError, ValueError):
                sid_raw = None
        if not ids:
            return []
        self._bootstrap_csrf()
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
            base["inheritableSitelinkSet"] = {"sitelinkSetId": sid_raw}
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
                "Grid set-sitelink-set: " + json.dumps(
                    data.get("errors") or vr.get("errors"), ensure_ascii=False)[:400])
        return res.get("updatedCampaigns") or []

    def set_campaign_disabled_places(self, campaign_ids: list[int], hosts: list[str]) -> list:
        """Set campaign-level disabledPlaces (minus площадки) through a narrow Grid update.

        Copy-path use: copy the source campaign disabledPlaces 1:1. Like
        ``set_campaign_callouts`` this reads the full unified payload and rewrites
        ONLY ``disabledPlaces`` so strategy/placements stay untouched.
        """
        ids = []
        for raw in campaign_ids or []:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                continue
            if cid > 0 and cid not in ids:
                ids.append(cid)
        clean_hosts = list(dict.fromkeys(str(h).strip() for h in (hosts or []) if str(h).strip()))
        if not ids:
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
            base["disabledPlaces"] = list(clean_hosts)
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

    def campaigns_edit_rows(self, campaign_ids: list[int]) -> dict[int, dict]:
        """Сырые строки CampaignsEditData ПО КУКЕ (0 v5-баллов) → ``{cid: row}``.

        Нужен для сверки «настройки источника vs настройки копии» (step_settings_diff): отдаёт
        кампанию так, как её видит редактор Директа — стратегия, корректировки, временной таргетинг,
        brandSafety, уведомления и т.д. Многого из этого v5 не показывает вовсе."""
        ids = []
        for c in (campaign_ids or []):
            try:
                cid = int(c)
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
            r = self._post("CampaignsEditData", _CAMPAIGNS_EDIT_DATA_Q,
                           {"login": self.login, "campaignInput": inp})
            rows = (((r.json().get("data") or {}).get("client") or {})
                    .get("campaigns") or {}).get("rowset") or []
            for row in rows:
                try:
                    out[int(row.get("id"))] = row
                except (TypeError, ValueError):
                    continue
        return out

    def read_campaign_invariants(self, campaign_ids: list[int]) -> dict[int, dict]:
        """Read campaign-level invariant галочки (blacklist toggles) via CampaignsEditData.

        Возвращает ``{cid: {field: tri-state}}`` для DoD-инвариантов кампании tp1–tp5:
        персонализация / расш.гео / «Директ помогает» / ценовые рек. / Карты (enableCompanyInfo) /
        Карты-платформа (yandexMaps) / список организаций (serpGeoWizard) / стратегия
        (payForConversion) + libraryMinusKeywordsIds + кампанийные АССЕТЫ ``callout_ids`` /
        ``sitelink_set_id`` / ``promo_extension_id`` (тот же ответ, 0 доп. запросов). Каждое булево — **tri-state**: реальный
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

        def _num(v):
            """Числовое поле tri-state: не число / не пришло → None (верификатор молчит)."""
            if isinstance(v, bool) or v in (None, ""):
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

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
                # Кампанийные АССЕТЫ (уточнения / набор быстрых ссылок / промо) — ИЗ ТОГО ЖЕ ответа,
                # без единого дополнительного запроса: поля уже в фрагменте UnifiedCampaign
                # (grid_campaigns_edit_data.graphql). Нормализация — ровно как в
                # _unified_campaign_update_from_edit_row:601-603 (сырой rowset отдаёт их под
                # inheritableCallouts{assetValue} / inheritableSitelinkSet{assetValue} /
                # promoExtension{id}, а НЕ плоскими ключами — читать сырьё «по имени write-поля»
                # нельзя, будет вечный None).
                # Read-гейт — ПРИСУТСТВИЕ КЛЮЧА в row: GraphQL всегда возвращает запрошенный ключ
                # (пусть и null), а его отсутствие = поле не пришло/схема поменялась → tri-state None
                # → верификатор МОЛЧИТ (fail-safe, тот же контракт, что у инвариант-галочек).
                _assets_read = any(k in row for k in
                                   ("inheritableCallouts", "inheritableSitelinkSet", "promoExtension"))
                _co_raw = (row.get("inheritableCallouts") or {}).get("assetValue") or []
                _sl_raw = (row.get("inheritableSitelinkSet") or {}).get("assetValue")
                _pr_raw = (row.get("promoExtension") or {}).get("id")
                # ── Кампанийная СПЕЦИФИКАЦИЯ из ТОГО ЖЕ ответа CampaignsEditData ───────────
                # Счётчик Метрики / цель / UTM-параметры кампании / недельный бюджет / статус
                # черновика / расписание показов. Поля уже присутствуют в
                # grid_campaigns_edit_data.graphql (metrikaCounters, meaningfulGoals{goalId},
                # bannerHrefParams, hasAddMetrikaTagToUrl, strategy.budget{sum,period,
                # autoProlongation}, status{primaryStatus}, aggregatedStatusInfo{status},
                # timeTarget{timeBoard,idTimeZone}) → НОЛЬ дополнительных запросов и баллов.
                # Read-гейт тот же, что у ассетов: ПРИСУТСТВИЕ ключа в row (GraphQL всегда вернёт
                # запрошенный ключ, пусть и null; отсутствие = схема поменялась) → иначе None и
                # верификатор МОЛЧИТ (tri-state fail-safe, журнал I).
                _bud = strat.get("budget") if isinstance(strat.get("budget"), dict) else {}
                _st = row.get("status") if isinstance(row.get("status"), dict) else {}
                _agg = (row.get("aggregatedStatusInfo")
                        if isinstance(row.get("aggregatedStatusInfo"), dict) else {})
                _tt = row.get("timeTarget") if isinstance(row.get("timeTarget"), dict) else {}
                _spec_read = any(k in row for k in
                                 ("metrikaCounters", "meaningfulGoals", "bannerHrefParams",
                                  "status", "aggregatedStatusInfo", "timeTarget"))
                _mg_raw = row.get("meaningfulGoals")
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
                    "campaign_assets_read": bool(_assets_read),
                    "callout_ids": ([str(x) for x in _co_raw]
                                    if "inheritableCallouts" in row else None),
                    "sitelink_set_id": ((str(_sl_raw) if _sl_raw else "")
                                        if "inheritableSitelinkSet" in row else None),
                    "promo_extension_id": ((str(_pr_raw) if _pr_raw else "")
                                           if "promoExtension" in row else None),
                    # ── спецификация кампании (tri-state, тот же ответ) ────────────────
                    "campaign_spec_read": bool(_spec_read),
                    "metrika_counters": ([str(x) for x in (row.get("metrikaCounters") or [])]
                                         if "metrikaCounters" in row else None),
                    "meaningful_goal_ids": ([str(g.get("goalId")) for g in (_mg_raw or [])
                                             if isinstance(g, dict) and g.get("goalId")]
                                            if "meaningfulGoals" in row else None),
                    "strategy_goal_id": ((str(strat.get("goalId"))
                                          if strat.get("goalId") not in (None, "") else "")
                                         if isinstance(row.get("strategy"), dict) else None),
                    "banner_href_params": (str(row.get("bannerHrefParams") or "")
                                           if "bannerHrefParams" in row else None),
                    "has_add_metrika_tag_to_url": _tri(row.get("hasAddMetrikaTagToUrl")),
                    "budget_sum": _num(_bud.get("sum")) if "budget" in strat else None,
                    "budget_period": ((str(_bud.get("period") or ""))
                                      if "budget" in strat else None),
                    "budget_auto_prolongation": _tri(_bud.get("autoProlongation")),
                    "status_primary": (str(_st.get("primaryStatus") or "")
                                       if "status" in row else None),
                    "aggregated_status": (str(_agg.get("status") or "")
                                          if "aggregatedStatusInfo" in row else None),
                    "time_board": (list(_tt.get("timeBoard") or [])
                                   if "timeTarget" in row else None),
                    "time_zone_id": ((str(_tt.get("idTimeZone"))
                                      if _tt.get("idTimeZone") not in (None, "") else "")
                                     if "timeTarget" in row else None),
                }
        return out

    def restore_pay_for_conversion_strategy(self, campaign_id: int, goal_id: int,
                                              weekly_rub: float,
                                              avg_cpa_rub: float = 0) -> list:
        """Восстановить стратегию PAY_FOR_CONVERSION_MULTIPLE_GOALS через Grid updateCampaigns.

        Применяется как постпроцесс копирования: кампания была создана с WB_MAXIMUM_CLICKS (v5
        не принимает PAY_FOR_CONVERSION_MULTIPLE_GOALS без счётчика+целей), а затем здесь
        восстанавливается реальная стратегия.

        weekly_rub  — недельный бюджет В РУБЛЯХ (Grid strategyData.sum — рубли, не микро).
        goal_id     — целевой GoalId Метрики (для payForConversion).
        avg_cpa_rub — средняя цена конверсии В РУБЛЯХ; обязательна для AUTOBUDGET_AVG_CPA
                      (Grid возвращает CANNOT_BE_NULL если не передать или передать 0).
                      Берётся из PriorityGoals.Items[0].Value / 1_000_000 источника.

        ВАЖНО: целенаправленно ОБХОДИТ _narrow_bases — та помечает WB_MAXIMUM_CLICKS как
        _unsupported_strategy (нет writeable write-имени для «Максимум кликов»). Здесь мы
        ХОТИМ сменить стратегию → _unsupported_strategy игнорируем. Все прочие ключи удаляем."""
        payloads = self._read_unified_campaign_update_payloads([campaign_id])
        base = payloads.get(campaign_id)
        if not base:
            raise GridFinalizeError(
                f"Grid restore-strategy: кампания {campaign_id} не найдена в edit-view")
        # Убираем internal _-маркеры (в т.ч. _unsupported_strategy) — мы намеренно меняем стратегию
        base = {k: v for k, v in base.items() if not k.startswith("_")}
        # Патчим стратегию: Grid read отдаёт MULTIPLE_CPA, но UpdateCampaigns принимает
        # write-enum AUTOBUDGET_MULTIPLE_CPA (HAR direct.yandex.ru.75har.har, #79).
        # Важно: для AUTOBUDGET_MULTIPLE_CPA payForConversion=false (оплата за конверсию —
        # свойство самой стратегии, не флаг внутри strategyData).
        # goalId="0" — цели задаются через meaningfulGoals, не через goalId.
        # avgCpa для MULTIPLE_CPA не нужен (AUTOBUDGET_AVG_CPA-специфичное поле).
        bs = base.get("biddingStategyWithPlatforms") or {}
        if not isinstance(bs, dict):
            bs = {}
        sd = bs.get("strategyData") if isinstance(bs.get("strategyData"), dict) else {}
        sd["payForConversion"] = False
        sd["goalId"] = "0"
        sd["sum"] = str(int(weekly_rub))
        sd["budgetType"] = "WEEKLY"
        sd.pop("avgCpa", None)           # не нужен для MULTIPLE_CPA
        sd.setdefault("payForShows", False)
        sd.setdefault("autoApplyRecommendationOptions", {"budgetIncreasePercent": None})
        sd.setdefault("isExplorationBudgetValueCustom", None)
        bs["strategyData"] = sd
        bs["strategyName"] = "AUTOBUDGET_MULTIPLE_CPA"
        base["biddingStategyWithPlatforms"] = bs
        # Задаём цели: цель goal_id с CPA = avg_cpa_rub; вторую цель (прочие,
        # если avg_cpa_rub > 0) добавляем тоже чтобы дать MULTIPLE_CPA > 1 цели.
        mg = []
        if goal_id and int(goal_id) > 0:
            goal_item = {"goalId": str(int(goal_id)), "conversionStrategy": "AVERAGE_CPA",
                         "isMetrikaSourceOfValue": False}
            if avg_cpa_rub and avg_cpa_rub > 0:
                goal_item["value"] = str(int(avg_cpa_rub))
            mg.append(goal_item)
        base["meaningfulGoals"] = mg
        q = ("mutation UpdateCampaigns($input:GdUpdateCampaignsInput!$login:String!){"
             "updateCampaigns(input:$input){updatedCampaigns{id}"
             "validationResult{errors{code params path}warnings{code params path}}}"
             "getClientMutationId(input:{login:$login}){mutationId}}")
        r = self._post("UpdateCampaigns", q, {
            "login": self.login,
            "input": {"campaignUpdateItems": [{"unifiedCampaign": base}]},
        })
        data = r.json()
        res = (data.get("data") or {}).get("updateCampaigns") or {}
        vr = res.get("validationResult") or {}
        if data.get("errors") or vr.get("errors"):
            raise GridFinalizeError(
                "Grid restore-strategy: " + json.dumps(
                    data.get("errors") or vr.get("errors"), ensure_ascii=False)[:500])
        return res.get("updatedCampaigns") or []

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
        r = self.post_idempotent("UpdateCampaigns", q, {
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

    def set_adgroup_names(self, adgroup_names: dict[int, str],
                          campaign_ids: list[int]) -> dict:
        """Переименовать группы через full-object RMW UpdateUnifiedAdGroups.

        Меняет ТОЛЬКО adGroupName; все остальные поля (ключи, регионы, минус-слова,
        трекинг, аудитории) читаются из Grid и записываются обратно без изменений.
        Группы с retargetings_present=True или bid_modifiers_present=True пропускаются:
        их поля не проходят безопасно через build_update_item, как и в других RMW-путях.

        Returns {"updated": [adgroup_id, ...], "skipped": [...], "errors": [...]}.
        """
        clean: dict[int, str] = {}
        for raw_id, raw_name in (adgroup_names or {}).items():
            try:
                gid = int(raw_id)
            except (TypeError, ValueError):
                continue
            name = str(raw_name or "").strip()
            if gid > 0 and name and gid not in clean:
                clean[gid] = name
        if not clean:
            return {"updated": [], "skipped": [], "errors": []}

        cids = [int(c) for c in (campaign_ids or [])
                if str(c).strip().lstrip("-").isdigit() and int(c) > 0]
        if not cids:
            return {"updated": [], "skipped": [],
                    "errors": ["нет campaign_ids для чтения групп"]}

        try:
            groups = self.groups_for_edit(cids)
        except GridFinalizeError as e:
            return {"updated": [], "skipped": [],
                    "errors": [f"groups_for_edit: {str(e)[:300]}"]}

        items: list[dict] = []
        skipped: list[dict] = []
        seen_gids: set[int] = set()

        for grp in groups:
            gid = grp.get("adgroup_id")
            if gid not in clean:
                continue
            if gid in seen_gids:
                continue
            seen_gids.add(gid)
            if not grp.get("supported"):
                skipped.append({"adgroup_id": gid,
                                 "reason": "тип группы не GdUnifiedAdGroup — RMW недоступен"})
                continue
            if grp.get("retargetings_present"):
                skipped.append({"adgroup_id": gid,
                                 "reason": "есть ретаргетинги — RMW небезопасен"})
                continue
            if grp.get("bid_modifiers_present"):
                skipped.append({"adgroup_id": gid,
                                 "reason": "есть корректировки ставок — RMW небезопасен"})
                continue
            grp_copy = dict(grp)
            grp_copy["adgroup_name"] = clean[gid]
            item = self.build_update_item(
                grp_copy,
                keywords=grp_copy.get("keywords") or [],
                relevance_match=grp_copy.get("relevance_match"),
            )
            items.append(item)

        not_found = sorted(gid for gid in clean if gid not in seen_gids)
        errors: list[str] = []
        if not_found:
            errors.append(f"группы не найдены в указанных кампаниях: {not_found[:20]}")

        if not items:
            return {"updated": [], "skipped": skipped, "errors": errors}

        try:
            updated_ids = self.update_unified_adgroups(items)
        except GridFinalizeError as e:
            errors.append(f"update_unified_adgroups: {str(e)[:400]}")
            return {"updated": [], "skipped": skipped, "errors": errors}

        return {"updated": updated_ids, "skipped": skipped, "errors": errors}

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

    def add_sitelink_set(self, sitelinks: list[dict], *, preserve_fragment: bool = False) -> int | None:
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
            raw_href = s.get("Href") or s.get("href") or ""
            # create-path исторически режет #якорь как внутреннюю служебную метку,
            # но content-editor назначает ФИНАЛЬНЫЕ посадочные URL и должен сохранять fragment.
            href = str(raw_href or "").strip() if preserve_fragment else _strip_href_fragment(raw_href)
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
        _sdt_wait = (2, 5, 10)
        for _sdt_att in range(len(_sdt_wait) + 1):
            r = self._post("UpdateShoppingAds", _SHOPPING_MUTATION,
                           {"updateShoppingInput": {"adUpdateItems": items, "saveDraft": True}})
            data = r.json()
            if data.get("errors") and _is_transient_data_error(data["errors"]) and _sdt_att < len(_sdt_wait):
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
          apply_global_minus=False → не добавлять глобальные минус-марки/модели к feedFilter.
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
            # Для Б/У-сайтов builder передаёт apply_global_minus=False: там эти фильтры вырезают
            # нужные used-car офферы из общего фида.
            if it.get("apply_global_minus", True) is not False:
                try:
                    from ..create import create_set_feeds as _csf
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
                from ..create import create_set_feeds as _csf_nf
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
            from ..create import create_set_feeds as _csf_af
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
        _lnf_wait = (2, 5, 10)
        _last_err = None
        for _oi, _ovr in enumerate(_alt_overrides):
            upd = _build_upd(_ovr)
            if not upd:
                return 0
            data: dict = {}
            for _lnf_att in range(len(_lnf_wait) + 1):
                r = self._post("updateListingAds", q,
                               {"updateListingInput": {"adUpdateItems": upd, "saveDraft": True}})
                data = r.json()
                if data.get("errors") and _is_transient_data_error(data["errors"]) and _lnf_att < len(_lnf_wait):
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
                                     placement_types: list[str] | None) -> list:
        """Узкий UpdateCampaigns: только placementTypes.
        Шаблон = set_campaign_sitelink_set (narrow-мутации обязаны эхом вернуть broadMatch
        и базовый скелет — _read_unified_campaign_update_payloads). Для tp5 эталон — null
        (ручная настройка через platforms gallery+search+organic)."""
        ids = []
        for raw in campaign_ids or []:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                continue
            if cid > 0 and cid not in ids:
                ids.append(cid)
        pts = [str(p) for p in (placement_types or []) if p] if placement_types is not None else None
        if not ids:
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
            base["placementTypes"] = pts if pts else None
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

    def set_campaign_organic_and_placement(self, campaign_values: dict) -> dict:
        """Узкий UpdateCampaigns: только isOrganicSearchEnabled + placementTypes (1:1 из источника).

        Предназначен для копировщика: переносит Grid-only настройки из источника на копию без
        изменения стратегии и прочих полей. campaign_values = {tgt_cid: {"isOrganicSearchEnabled":
        bool, "placementTypes": list|None}} — значения берутся 1:1 из источника.

        Кампании с _unsupported_strategy (DEFAULT / OPTIMIZE_CLICKS без лимита и бюджета)
        пропускаются — им нет безопасного write-enum → отдаются в skipped.
        Добавлено 2026-07-17; OPTIMIZE_CLICKS с недельным бюджетом подтверждён HAR 2026-07-20.
        """
        ids = []
        for raw in (campaign_values or {}):
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                continue
            if cid > 0 and cid not in ids:
                ids.append(cid)
        if not ids:
            return {"updated": [], "skipped": {}, "errors": []}
        try:
            payloads = self._read_unified_campaign_update_payloads(ids)
        except GridFinalizeError as e:
            return {"updated": [], "skipped": {}, "errors": [str(e)[:300]]}
        q = ("mutation UpdateCampaigns($input:GdUpdateCampaignsInput!$login:String!){"
             "updateCampaigns(input:$input){updatedCampaigns{id}"
             "validationResult{errors{code params path}warnings{code params path}}}"
             "getClientMutationId(input:{login:$login}){mutationId}}")
        bases, skipped = self._narrow_bases(payloads, ids, "Grid set-organic-placement")
        for _cid, _why in skipped.items():
            print(f"[grid] set-organic-placement: кампания {_cid} пропущена — «{_why}»", flush=True)
        items = []
        for cid, base in bases.items():
            vals = campaign_values.get(cid) or campaign_values.get(str(cid)) or {}
            is_organic = bool(vals.get("isOrganicSearchEnabled"))
            pts = vals.get("placementTypes") or []
            pts_str = [str(p) for p in pts if p]
            # Кампанейный флаг
            base["isOrganicSearchEnabled"] = is_organic
            base["placementTypes"] = pts_str or None
            # РЕАЛЬНЫЙ контрол — biddingStategyWithPlatforms.platforms.
            # isOrganicSearchEnabled == platforms.organic,
            # ADV_GALLERY в placementTypes == platforms.gallery.
            # Без патча этих платформ мутация эхует целевые значения и ничего не меняет.
            # Добавлено 2026-07-17 (диагностика: source gallery=F/organic=F, copy gallery=T/organic=T).
            bs = base.get("biddingStategyWithPlatforms") or {}
            plats = dict(bs.get("platforms") or {})
            plats["organic"] = is_organic
            plats["gallery"] = "ADV_GALLERY" in pts_str
            bs["platforms"] = plats
            base["biddingStategyWithPlatforms"] = bs
            items.append({"unifiedCampaign": base})
        if not items:
            return {"updated": [], "skipped": {str(k): v for k, v in skipped.items()}, "errors": []}
        data = self._post_json_retry("UpdateCampaigns", q, {
            "login": self.login,
            "input": {"campaignUpdateItems": items},
        })
        res = (data.get("data") or {}).get("updateCampaigns") or {}
        vr = res.get("validationResult") or {}
        errors: list[str] = []
        if data.get("errors") or vr.get("errors"):
            errors.append("Grid set-organic-placement: " + json.dumps(
                data.get("errors") or vr.get("errors"), ensure_ascii=False)[:400])
        updated_ids = [int(c["id"]) for c in (res.get("updatedCampaigns") or [])
                       if isinstance(c, dict) and c.get("id")]
        return {"updated": updated_ids, "skipped": {str(k): v for k, v in skipped.items()},
                "errors": errors}

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
        try:                                     # пакетный контекст / standalone
            from .. import kontent_pack as _kpf
        except ImportError:
            import kontent_pack as _kpf          # type: ignore[no-redef]
        fname = _os.path.basename(image_path or "")
        try:
            if not _kpf.isfile_bounded(image_path):
                print(f"[img-upload] FAIL {self.login} {fname}: файл не найден или недоступен "
                      f"({image_path})", flush=True)
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
            # 2026-07-18: сам fh.read() тоже зависал НАВСЕГДА — у FUSE таймаута нет by design
            # (job f58a123d8405 / a4bef725b5cb: 12-14 потоков стояли здесь, created 3/20).
            # Читаем с пределом времени; недоступен → пропускаем картинку, а не вешаем джобу.
            _img_bytes = _kpf.read_bytes_bounded(image_path)
            if not _img_bytes:
                print(f"[img-upload] SKIP {self.login} {fname}: файл не прочитан за "
                      f"{_kpf._FS_OP_TIMEOUT:.0f}с или пуст ({image_path})", flush=True)
                return None
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

    def _note_ad_update_shortfall(self, op: str, res: dict, data: dict,
                                  done: int, sent: int,
                                  sent_items: list[dict] | None = None) -> list[str]:
        """Причины неполной записи батча объявлений (пусто = записались все ``sent``).

        Отказ Директа приходит как ``updatedAds:[null, …]`` (длина совпадает с отправленной)
        + ``validationResult.errors``. Раньше ошибки только печатались в stdout, наверх шла
        длина списка → задание выглядело успешным. Теперь причина возвращается вызывающему.
        """
        reasons = _grid_validation_reasons(res, data)
        if done >= sent:
            # полный успех: warnings не превращаем в ошибку задания, но ошибки-уровня
            # validationResult при полном успехе не бывает — если пришли, показываем.
            return [r for r in reasons if not r.startswith("warning: ")]
        if not reasons:
            reasons = ["Директ вернул updatedAds без id (отказ без объяснения)"]
        head = f"{op}: обновлено {done} из {sent} объявл."
        if done == 0:
            head = f"{op}: НЕ обновлено ни одного из {sent} объявл."
        failed = _grid_failed_ad_ids(res, sent_items or [])
        failed_note = ""
        if failed:
            failed_note = f"; failed_ad_ids={','.join(failed)}"
        blobs = [f"{head} — {reason}{failed_note}" for reason in reasons]
        for blob in blobs:
            print(f"[grid] {self.login} {blob}", flush=True)
        return blobs

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
                # визитка: as-is из RMW-чтения (adaptive_ads_for_update), None = прежнее поведение
                # для вызывающих, которые состояние объявления не читают
                "permalinkId": it.get("permalinkId") or None,
                "phoneId": it.get("phoneId") or None,
                "erirAdDescription": None,
                # ad-level наборы: мутация REPLACE'ит payload целиком, поэтому хардкод INHERIT
                # СТИРАЛ ad-level набор быстрых ссылок (policy OVERRIDE) — браузер в том же вызове
                # шлёт РЕАЛЬНОЕ состояние (HAR entry [187]: OVERRIDE+sitelinkSetId, callouts CLEAR).
                # INHERIT остаётся ТОЛЬКО как fallback для вызывающих, которые состояние не читают.
                "inheritableCallouts": (it.get("inheritableCallouts")
                                        if isinstance(it.get("inheritableCallouts"), dict)
                                        else {"policy": "INHERIT"}),
                "inheritableSitelinkSet": (it.get("inheritableSitelinkSet")
                                           if isinstance(it.get("inheritableSitelinkSet"), dict)
                                           else {"policy": "INHERIT"}),
                "id": str(it["id"]),
            }
            if it.get("adPrice"):
                # сырой bannerPrice из adaptive_ads_for_update несёт __typename (Grid его на входе
                # не ждёт) — снимаем. У вызывающих, дающих уже нормализованный adPrice, это no-op.
                item["adPrice"] = _strip_graphql_typenames(it["adPrice"])
            # кнопка: full-replace без неё СТИРАЕТ кнопку (доказано live 2026-07-06 — именно поэтому
            # create_set_feeds._apply_combo_button переставляет её после ценового апдейта).
            # Ключа нет → не шлём вообще (прежнее поведение, отказ на кнопке не роняет батч).
            btn = it.get("button")
            if isinstance(btn, dict) and btn.get("action"):
                item["button"] = {"action": btn["action"], "href": btn.get("href") or ""}
                # кастомная надпись кнопки: ключа нет → прежнее поведение (браузер в HAR его тоже
                # не шлёт, когда customText=null), есть → без него full-replace обнулял текст
                if btn.get("customText"):
                    item["button"]["customText"] = btn["customText"]
            # отображаемая ссылка: тот же класс потери, что inheritable* — мутация REPLACE'ит
            # payload, а displayHref в нём не было → linkTail стирался у всех объявлений, где он
            # задан (живой probe porg-pvrbl7mh: 102/102 непустых). Ключа нет → не шлём (прежнее
            # поведение для вызывающих, которые состояние не читают, напр. repair_media).
            if it.get("displayHref"):
                item["displayHref"] = str(it["displayHref"])
            if it.get("multicards"):
                item["multicards"] = [
                    dict(card) for card in (it.get("multicards") or []) if isinstance(card, dict)
                ]
            upd.append(item)
        self.last_ad_update_errors = []
        if not upd:
            return 0
        q = ("mutation UpdateAdaptiveTextAds($updateInput:GdUpdateAdaptiveTextAdsInput!){"
             "reqId:getReqId updateAdaptiveTextAds(input:$updateInput){"
             "updatedAds{id}validationResult{errors{code params path}"
             "warnings{code params path}}}}")
        updated_total = 0
        all_errors: list[str] = []
        for chunk in [upd[i:i + _GRID_MUTATION_CHUNK]
                      for i in range(0, len(upd), _GRID_MUTATION_CHUNK)]:
            try:
                self._bootstrap_csrf()
                r = self._post("UpdateAdaptiveTextAds", q,
                               {"updateInput": {"adUpdateItems": chunk, "saveDraft": True}})
                if r.status_code == 403:
                    r = self._post("UpdateAdaptiveTextAds", q,
                                   {"updateInput": {"adUpdateItems": chunk, "saveDraft": True}})
                data = r.json()
                res = (data.get("data") or {}).get("updateAdaptiveTextAds") or {}
                done = _grid_updated_ad_ids(res)
                updated_total += len(done)
                all_errors.extend(self._note_ad_update_shortfall(
                    "UpdateAdaptiveTextAds", res, data, len(done), len(chunk), chunk))
            except Exception as e:  # noqa: BLE001
                all_errors.append(
                    f"UpdateAdaptiveTextAds: {type(e).__name__}: {str(e)[:160]} "
                    f"— 0 из {len(chunk)} объявл. обновлено; "
                    f"failed_ad_ids={','.join(str(x.get('id')) for x in chunk if x.get('id'))}")
        self.last_ad_update_errors = all_errors
        return updated_total

    def _ads_rows_paginated(self, op_name: str, query: str, chunk_cids: list[int]) -> list[dict]:
        """Все строки ``client.ads.rowset`` по чанку кампаний — с ПАГИНАЦИЕЙ по limitOffset.

        Grid отдаёт максимум ``_ADS_PAGE_LIMIT`` строк за запрос, а один аккаунт легко даёт больше:
        живой probe porg-pvrbl7mh (80 кампаний, ОДИН чанк) = 5588 объявлений, т.е. одностраничное
        чтение молча теряло 588 (~336 адаптивных). Тихая потеря особенно опасна для вкладки замены
        картинок: невидимое объявление не попадает в инвентарь и остаётся со старой картинкой,
        а UI рапортует успех. Бюджет страницы делят ВСЕ типы объявлений (Shopping/Listing/Text),
        не только адаптивные, поэтому переполнение наступает раньше, чем кажется по числу адаптивов.

        Выход из цикла: страница короче лимита. Страховка от бесконечного цикла на кривом ответе
        API — жёсткий потолок ``_ADS_PAGE_MAX_PAGES`` итераций (с логом, дальше отдаём что набрали).
        """
        rows_all: list[dict] = []
        offset = 0
        for _ in range(_ADS_PAGE_MAX_PAGES):
            inp = {
                "filter": {"campaignIdIn": [str(cid) for cid in chunk_cids]},
                "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
                "limitOffset": {"limit": _ADS_PAGE_LIMIT, "offset": offset},
                "orderBy": [{"order": "ASC", "field": "ID"}],
            }
            data = self._post_json_retry(op_name, query, {"login": self.login, "inp": inp})
            rows = ((((data.get("data") or {}).get("client") or {})
                     .get("ads") or {}).get("rowset") or [])
            rows_all.extend(rows)
            if len(rows) < _ADS_PAGE_LIMIT:
                return rows_all
            offset += len(rows)
        print(f"[grid] {op_name} {self.login}: достигнут потолок пагинации "
              f"{_ADS_PAGE_MAX_PAGES} страниц (offset={offset}), инвентарь может быть неполным",
              flush=True)
        return rows_all

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
        # inheritable*/permalinkWithPhone — ad-level состояние, которое мутация REPLACE'ит: без него
        # RMW стирал набор быстрых ссылок объявления и визитку (HAR entry [55] BannersQueryForEdit,
        # те же поля у GdAdaptiveTextAd). images{} расширены до полей превью (name/mdsGroupId/
        # imageSize/formats) — вкладка массовой замены картинок показывает их без доп. запроса.
        q = ("query AdaptiveAdsForUpdate($login:String!,$inp:GdAdsContainerInput!){"
             "client(searchBy:{login:$login}){ads(input:$inp){rowset{id campaignId "
             "...on GdAdaptiveTextAd{href linkTail titles bodies "
             "images{imageHash name mdsGroupId namespace imageSize{height width} "
             "formats{imageSize{height width} path}} "
             "bannerPrice{price priceOld prefix currency} "
             # customText — кастомная надпись кнопки; без неё RMW обнулял текст у кнопок,
             # где он задан (GdBannerButtonInput{action customText href}, интроспекция 2026-07-18)
             "hasVideo hasButton button{action customText href} "
             "multicards{id text href price priceOld currency image{imageHash}} "
             "inheritableCallouts{policy assetValue} inheritableSitelinkSet{policy assetValue} "
             "permalinkWithPhone{permalinkId phoneId policy} "
             "typedCreatives{creativeId creativeType}}"
             "}}}}")
        out: dict[int, dict] = {}
        for chunk in [cids[i:i + 100] for i in range(0, len(cids), 100)]:
            rows = self._ads_rows_paginated("AdaptiveAdsForUpdate", q, chunk)
            for row in rows:
                try:
                    aid = int(row.get("id"))
                except (TypeError, ValueError):
                    continue
                if aid not in wanted:
                    continue
                images_rich = _grid_images_rich(row.get("images"))
                image_hashes = [im["imageHash"] for im in images_rich]
                pwp = row.get("permalinkWithPhone") or {}
                # видео-креативы: только VIDEO_ADDITION (другие типы в creativeIds Grid не ждёт)
                creative_ids = [str(c.get("creativeId")) for c in (row.get("typedCreatives") or [])
                                if c and c.get("creativeId")
                                and (c.get("creativeType") or "") == "VIDEO_ADDITION"]
                out[aid] = {
                    "id": aid,
                    "campaignId": row.get("campaignId"),
                    "href": row.get("href") or "",
                    # отображаемая ссылка: читается как linkTail, пишется как displayHref (хвост, не
                    # полный URL — проверено live 17.07.2026) → без чтения RMW стирал её full-replace'ом
                    "displayHref": row.get("linkTail") or "",
                    "titles": list(row.get("titles") or []),
                    "bodies": list(row.get("bodies") or []),
                    "imageHashes": image_hashes,
                    # полные данные картинок для превью (вкладка массовой замены):
                    # [{imageHash, name, mdsGroupId, width, height, preview_url}, …]
                    "images": images_rich,
                    "adPrice": row.get("bannerPrice"),
                    "creativeIds": creative_ids,
                    "hasVideo": bool(row.get("hasVideo")),
                    "hasButton": bool(row.get("hasButton")),
                    "button": row.get("button"),
                    "multicards": _grid_multicards_write(row.get("multicards")),
                    # ad-level наборы уже в WRITE-shape (assetValue→sitelinkSetId) — вызывающий
                    # кладёт их в update_ad_images as-is. None = состояние не пришло → fallback.
                    "inheritableSitelinkSet": _grid_inheritable_write(
                        row.get("inheritableSitelinkSet"), "sitelinkSetId"),
                    # уточнения: assetValue — СПИСОК id, пишется как calloutIds (интроспекция
                    # GdInheritableCalloutsInput 2026-07-18 + живой probe 102/102 OVERRIDE).
                    # Раньше здесь стоял None → fallback INHERIT стирал уточнения объявления.
                    "inheritableCallouts": _grid_inheritable_write(
                        row.get("inheritableCallouts"), "calloutIds"),
                    "permalinkId": pwp.get("permalinkId"),
                    "phoneId": pwp.get("phoneId"),
                }
        return out

    def text_ads_for_update(self, campaign_ids: list[int], ad_ids: list[int]) -> dict[int, dict]:
        """RMW-снимок ОБЫЧНЫХ текстовых объявлений (``GdTextAd``) для ``UpdateTextAds``.

        Отдельный путь от ``adaptive_ads_for_update``: у TextAd картинка ОДНА и лежит в
        ``bannerImage`` (не список ``images``), а пишется скаляром ``textBannerImageHash``
        (браузерный эталон ``_har/TEXTAD_image_replace.json``, мутация ``UpdateTextAds``).

        ``UpdateTextAds`` — full-replace, как и адаптивная мутация, поэтому читаем ВСЁ, что
        придётся отдать назад. Состав сверен с интроспекцией входного типа ``GdUpdateAdInput``
        (**32 поля**, живая схема 2026-07-19) — см. отчёт `images-tab-textad-report.md`.

        Формат выдачи намеренно совпадает с ``adaptive_ads_for_update`` (``imageHashes``
        списком из одного элемента, ``images`` — rich-превью), чтобы инвентарь вкладки
        обрабатывал оба типа одним кодом. ``kind='text'`` отличает их при записи.

        ``rmw_unsafe`` — непустая строка, если у объявления есть поле, чью write-форму
        подтвердить нечем (турболендинг, мультикарточки). Такое объявление НЕ обновляем:
        full-replace без этих ключей стёр бы настройку (класс
        ``UAC_FULL_PATCH_REPLACE_DROPS_ASYMMETRIC_KEY``). Лучше честный пропуск, чем потеря.
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
        q = ("query TextAdsForUpdate($login:String!,$inp:GdAdsContainerInput!){"
             "client(searchBy:{login:$login}){ads(input:$inp){rowset{id campaignId "
             "...on GdTextAd{href hrefParams linkTail title titleExtension body isMobile "
             "erirAdDescription showTitleAndBody preferVCardOverPermalink vcardId "
             "turboGalleryHref "
             "bannerImage{imageHash name mdsGroupId namespace imageSize{height width} "
             "formats{imageSize{height width} path}} "
             # hasButton НЕ читаем: в GdUpdateAdInput такого ключа нет (интроспекция
             # 2026-07-19, 32 поля), кнопка пишется объектом button — флаг был мёртвым
             "logoImage{imageHash} button{action customText href} "
             "bannerPrice{price priceOld prefix currency} "
             "inheritableCallouts{policy assetValue} inheritableSitelinkSet{policy assetValue} "
             "permalinkWithPhone{permalinkId phoneId policy} "
             "typedCreative{creativeId creativeType} "
             "multicards{__typename} turbolanding{id}}"
             "}}}}")
        out: dict[int, dict] = {}
        for chunk in [cids[i:i + 100] for i in range(0, len(cids), 100)]:
            rows = self._ads_rows_paginated("TextAdsForUpdate", q, chunk)
            for row in rows:
                try:
                    aid = int(row.get("id"))
                except (TypeError, ValueError):
                    continue
                if aid not in wanted:
                    continue
                images_rich = _grid_images_rich([row.get("bannerImage")]
                                                if row.get("bannerImage") else [])
                creative = row.get("typedCreative") or {}
                creative_id = (str(creative.get("creativeId"))
                               if creative.get("creativeId")
                               and (creative.get("creativeType") or "") == "VIDEO_ADDITION"
                               else None)
                unsafe = []
                if row.get("turbolanding"):
                    unsafe.append("турболендинг")
                if row.get("multicards"):
                    unsafe.append("мультикарточки")
                out[aid] = {
                    "id": aid,
                    "kind": "text",
                    "href": row.get("href") or "",
                    "hrefParams": row.get("hrefParams") or "",
                    # читается linkTail, пишется displayHref — та же асимметрия, что у
                    # адаптивных (GRID_RMW_DISPLAY_HREF_WIPED); ключ есть в GdUpdateAdInput
                    "displayHref": row.get("linkTail") or "",
                    "title": row.get("title") or "",
                    "titleExtension": row.get("titleExtension") or None,
                    "body": row.get("body") or "",
                    "isMobile": bool(row.get("isMobile")),
                    "erirAdDescription": row.get("erirAdDescription") or None,
                    "showTitleAndBody": bool(row.get("showTitleAndBody")),
                    "preferVCardOverPermalink": bool(row.get("preferVCardOverPermalink")),
                    "vcardId": row.get("vcardId") or None,
                    "turboGalleryHref": row.get("turboGalleryHref") or None,
                    # картинка ОДНА: список из одного элемента — только ради общей формы
                    # с адаптивным путём (инвентарь вкладки ходит по imageHashes)
                    "imageHashes": [im["imageHash"] for im in images_rich],
                    "images": images_rich,
                    "logoImageHash": (row.get("logoImage") or {}).get("imageHash") or None,
                    "button": row.get("button"),
                    "adPrice": row.get("bannerPrice"),
                    "creativeId": creative_id,
                    "inheritableSitelinkSet": _grid_inheritable_write(
                        row.get("inheritableSitelinkSet"), "sitelinkSetId"),
                    "inheritableCallouts": _grid_inheritable_write(
                        row.get("inheritableCallouts"), "calloutIds"),
                    # у TextAd визитка пишется ОБЪЕКТОМ permalinkWithPhone (у адаптивных —
                    # плоскими permalinkId/phoneId): так шлёт браузер в эталоне
                    "permalinkWithPhone": row.get("permalinkWithPhone") or None,
                    "rmw_unsafe": ", ".join(unsafe),
                }
        return out

    def update_text_ad_images(self, ad_items: list[dict]) -> int:
        """Заменить картинку обычных текстовых объявлений через ``UpdateTextAds``.

        ``ad_items`` — снимки из ``text_ads_for_update`` с уже подменённым ``imageHashes[0]``
        (или явным ``textBannerImageHash``). Мутация REPLACE'ит объявление целиком, поэтому
        собираем ПОЛНЫЙ payload: 17 ключей браузерного эталона (шлются ВСЕГДА, в т.ч.
        ``adPrice``/``permalinkWithPhone`` с пустым значением) + поля, которые браузер не
        слал только потому, что они были пусты у его объявления (displayHref, button,
        logoImageHash, vcardId, showTitleAndBody, preferVCardOverPermalink) — их шлём
        только когда значение непустое, т.е. поведение на «пустом» объявлении совпадает
        с эталоном байт в байт.

        ``rmw_unsafe`` (турболендинг/мультикарточки) → объявление пропускаем: их write-форму
        подтвердить нечем, а full-replace без них стёр бы настройку.
        → число обновлённых объявлений. Не бросает — 0 при ошибке.
        """
        return self.update_text_ads(ad_items)

    def update_text_ads(self, ad_items: list[dict], *, allow_empty_image_hashes: bool = False) -> int:
        """Full-replace TextAd payload through Grid without requiring image mutation."""
        self.last_ad_update_errors = []
        upd = []
        for it in (ad_items or []):
            if not it.get("id") or it.get("rmw_unsafe"):
                continue
            hashes = list(it.get("imageHashes") or [])
            image_hash = str(it.get("textBannerImageHash") or (hashes[0] if hashes else "") or "")
            if not image_hash and not allow_empty_image_hashes:
                continue
            item = {
                "href": it.get("href") or "",
                "hrefParams": it.get("hrefParams") or "",
                # домен браузер шлёт null и на объявлении с непустым читаемым domain —
                # Директ выводит его из href сам (эталон TEXTAD_image_replace.json)
                "domain": None,
                "body": it.get("body") or "",
                "title": it.get("title") or "",
                "titleExtension": it.get("titleExtension") or None,
                "textBannerImageHash": image_hash or None,
                "creativeId": it.get("creativeId") or None,
                "adPrice": _strip_graphql_typenames(it["adPrice"]) if it.get("adPrice") else None,
                "isMobile": bool(it.get("isMobile")),
                "erirAdDescription": it.get("erirAdDescription") or None,
                "inheritableCallouts": (it.get("inheritableCallouts")
                                        if isinstance(it.get("inheritableCallouts"), dict)
                                        else {"policy": "INHERIT"}),
                "inheritableSitelinkSet": (it.get("inheritableSitelinkSet")
                                           if isinstance(it.get("inheritableSitelinkSet"), dict)
                                           else {"policy": "INHERIT"}),
                "adType": "TEXT",
                "id": str(it["id"]),
                "turboGalleryParams": {"turboGalleryHref": it.get("turboGalleryHref") or None},
            }
            # ``adPrice`` и ``permalinkWithPhone`` шлём БЕЗУСЛОВНО — как браузер. Раньше оба
            # ключа опускались на объявлении с пустым значением, и живой замер показал, что
            # это не редкость: 4970 из 7501 объявлений уходили бы с 15 ключами вместо 17.
            # Эквивалентность «ключа нет» ≡ «CLEAR» под REPLACE-мутацией НЕ доказана (эталон
            # покрывает только ветку «оба непусты»), поэтому не опираемся на неё вовсе:
            # состав payload теперь ОДИН для всех объявлений и равен браузерному.
            # adPrice: GdAdPriceInput (nullable, интроспекция 2026-07-19) → явный null валиден.
            pwp = it.get("permalinkWithPhone")
            if isinstance(pwp, dict) and pwp.get("policy"):
                item["permalinkWithPhone"] = {
                    k: v for k, v in {
                        "policy": str(pwp.get("policy")),
                        "permalinkId": pwp.get("permalinkId"),
                        "phoneId": pwp.get("phoneId"),
                    }.items() if v is not None
                }
            else:
                # визитки нет → ровно то, что шлёт браузер (``policy`` внутри объекта
                # NON_NULL, поэтому пустой объект слать нельзя)
                item["permalinkWithPhone"] = {"policy": "CLEAR"}
            if it.get("displayHref"):
                item["displayHref"] = str(it["displayHref"])
            if it.get("logoImageHash"):
                item["logoImageHash"] = str(it["logoImageHash"])
            if it.get("vcardId"):
                item["vcardId"] = str(it["vcardId"])
            if it.get("showTitleAndBody"):
                item["showTitleAndBody"] = True
            if it.get("preferVCardOverPermalink"):
                item["preferVCardOverPermalink"] = True
            btn = it.get("button")
            if isinstance(btn, dict) and btn.get("action"):
                item["button"] = {"action": btn["action"], "href": btn.get("href") or ""}
                if btn.get("customText"):
                    item["button"]["customText"] = btn["customText"]
            upd.append(item)
        if not upd:
            return 0
        q = ("mutation UpdateTextAds($updateInput:GdUpdateAdsInput!){"
             "reqId:getReqId updateAds(input:$updateInput){"
             "updatedAds{id}validationResult{errors{code params path}"
             "warnings{code params path}}}}")
        # saveDraft=false — так шлёт браузер для TextAd (у адаптивных true). Не переносим
        # значение между мутациями: сверено по эталону, 3/3 запроса UpdateTextAds = false.
        updated_total = 0
        all_errors: list[str] = []
        for chunk in [upd[i:i + _GRID_MUTATION_CHUNK]
                      for i in range(0, len(upd), _GRID_MUTATION_CHUNK)]:
            variables = {"updateInput": {"adUpdateItems": chunk, "saveDraft": False}}
            try:
                self._bootstrap_csrf()
                r = self._post("UpdateTextAds", q, variables)
                if r.status_code == 403:
                    r = self._post("UpdateTextAds", q, variables)
                data = r.json()
                res = (data.get("data") or {}).get("updateAds") or {}
                done = _grid_updated_ad_ids(res)
                updated_total += len(done)
                all_errors.extend(self._note_ad_update_shortfall(
                    "UpdateTextAds", res, data, len(done), len(chunk), chunk))
            except Exception as e:  # noqa: BLE001
                all_errors.append(
                    f"UpdateTextAds: {type(e).__name__}: {str(e)[:160]} "
                    f"— 0 из {len(chunk)} объявл. обновлено; "
                    f"failed_ad_ids={','.join(str(x.get('id')) for x in chunk if x.get('id'))}")
        self.last_ad_update_errors = all_errors
        return updated_total

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
                "creativeIds": [str(c) for c in (it.get("creativeIds") or []) if c],
                "permalinkId": it.get("permalinkId") or None,
                "phoneId": it.get("phoneId") or None,
                "erirAdDescription": None,
                "inheritableCallouts": (it.get("inheritableCallouts")
                                        if isinstance(it.get("inheritableCallouts"), dict)
                                        else {"policy": "INHERIT"}),
                "inheritableSitelinkSet": (it.get("inheritableSitelinkSet")
                                           if isinstance(it.get("inheritableSitelinkSet"), dict)
                                           else {"policy": "INHERIT"}),
                "id": str(it["id"]),
            }
            if it.get("adPrice"):
                item["adPrice"] = _strip_graphql_typenames(it["adPrice"])
            btn = it.get("button")
            if isinstance(btn, dict) and btn.get("action"):
                item["button"] = {"action": btn["action"], "href": btn.get("href") or ""}
                if btn.get("customText"):
                    item["button"]["customText"] = btn["customText"]
            if it.get("displayHref"):
                item["displayHref"] = str(it["displayHref"])
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

    def groups_for_edit(self, campaign_id: int | list[int], *,
                        meta: dict | None = None) -> list[dict]:
        """Прочитать группы кампании(й) для read-modify-write UpdateUnifiedAdGroups.

        Возвращает список нормализованных групп со всеми полями, нужными для полного
        (full-object) обновления группы БЕЗ потери минус-слов/регионов/трекинга, плюс
        служебные поля для идемпотентности (keyword_count/relevance_match) и safety
        (bid_modifiers_present/retargetings_present). Только GdUnifiedAdGroup — остальные
        типы отдаём с ``supported=False`` (их этот путь не трогает).

        campaign_id может быть int или списком int (читаем пачкой одним запросом).

        ``meta`` (опционально, для вызывающих, которые СУДЯТ по счётчикам): словарь заполняется
        признаками ОБРЕЗКИ ответа по лимиту ``_GFE_LIMIT`` — ``adgroups_truncated`` /
        ``keywords_truncated`` (ровно лимит строк = список почти наверняка усечён). Grid отдаёт
        максимум ``_GFE_LIMIT`` строк на секцию, offset-пагинация за этот предел не работает →
        у крупного набора ``keyword_count`` НЕДОсчитан. Вызывающий обязан при ``keywords_truncated``
        НЕ судить по ключам (иначе ложный «live < build» → ложный ремонт). Параметр опционален:
        существующие вызовы (RMW-докрутка, spec-audit) поведения не меняют."""
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
                      "limitOffset": {"offset": 0, "limit": _GFE_LIMIT},
                      "orderBy": [{"field": "ID", "order": "ASC"}]},
            "scInp": {"filter": {"typeIn": ["KEYWORD"], "campaignIdIn": id_strings},
                      "statRequirements": {"preset": "TODAY"},
                      "limitOffset": {"offset": 0, "limit": _GFE_LIMIT},
                      "orderBy": [{"order": "DESC", "field": "GROUP_ID"}]},
            "rtInp": {"filter": {"campaignIdIn": id_strings, "typeNotIn": ["INTERESTS"]},
                      "statRequirements": {"preset": "TODAY"},
                      "limitOffset": {"offset": 0, "limit": _GFE_LIMIT},
                      "orderBy": [{"order": "DESC", "field": "GROUP_ID"}]},
        }
        j = self._post_json_retry("GroupsForEditLite", _GROUPS_FOR_EDIT_LITE_Q, variables)
        client = (j.get("data") or {}).get("client") or {}
        ag_rows = ((client.get("adGroups") or {}).get("rowset") or [])
        sc_rows = ((client.get("showConditions") or {}).get("rowset") or [])
        rt_rows = ((client.get("retargetings") or {}).get("rowset") or [])
        if meta is not None:
            # Ровно лимит строк ⇒ ответ почти наверняка обрезан (пагинация за лимит не работает).
            meta["adgroup_rows"] = len(ag_rows)
            meta["keyword_rows"] = len(sc_rows)
            meta["adgroups_truncated"] = len(ag_rows) >= _GFE_LIMIT
            meta["keywords_truncated"] = len(sc_rows) >= _GFE_LIMIT

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
                               relevance_match: dict | None,
                               retargeting_ids: list | None = None,
                               retargeting_on_search: bool = False) -> dict:
        """Собрать GdUpdateUnifiedAdGroupItem: round-trip ВСЕХ полей группы (регионы/минус-слова/
        трекинг/аудитория сохраняются как прочитано) + подставить keywords и relevanceMatch.

        retargeting_ids — id УСЛОВИЙ ретаргетинга (аудитории), резолвнутые под ЦЕЛЕВОЙ кабинет
        (`create_set_audiences.resolve_for_account`). Формат элемента — билдер интерфейса
        Директа: {retCondId, id:null}. retargeting_on_search=True → searchRetargetings
        (поиск tp2/tp4), иначе retargetings (сеть tp1/tp5).

        ⚠️ Дефолт остаётся ПУСТЫМ списком, и это перезапись поля целиком. `GroupsForEditLite`
        отдаёт по ретаргетингам только `adGroupId` (флаг `retargetings_present`), без
        `retargetingConditionId` и без признака поиск/сеть — значит СОХРАНИТЬ уже стоящие
        аудитории этот RMW не может. Поэтому вызывающий ОБЯЗАН и дальше пропускать группы с
        `retargetings_present`/`bid_modifiers_present` (repair_keywords.py:93,
        grid_finalize.py:1730), либо передавать сюда явный список аудиторий."""
        kw = [{"phrase": p} for p in dict.fromkeys(
            s for s in (str(k).strip() for k in (keywords or [])) if s)][:200]
        from ..create.create_set_audiences import retargetings_payload as _rets_payload
        _rets = _rets_payload(retargeting_ids)
        item = {
            "adGroupId": str(grp["adgroup_id"]),
            "adGroupName": grp.get("adgroup_name") or "",
            "adGroupMinusKeywords": list(dict.fromkeys(str(m) for m in (grp.get("minus_keywords") or [])))[:100],
            "bidModifiers": {},
            "libraryMinusKeywordsIds": list(dict.fromkeys(str(i) for i in (grp.get("library_minus_ids") or []))),
            "regionIds": list(dict.fromkeys(int(r) for r in (grp.get("region_ids") or []))) or [225],
            "hyperGeoId": grp.get("hyper_geo_id"),
            "hyperlocalGeoSegments": grp.get("hyperlocal_geo_segments"),
            "audienceTargeting": grp.get("audience_targeting") or "ALL_AUDIENCE",
            "contentTypeShowSettings": grp.get("content_type_show_settings"),
            "keywords": kw,
            "caRetargetingCondition": None,
            "retargetings": ([] if retargeting_on_search else _rets),
            "searchRetargetings": (_rets if retargeting_on_search else []),
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
            r = self.post_idempotent(
                "UpdateUnifiedAdGroups",
                _UPDATE_UNIFIED_ADGROUPS_Q,
                {"unifiedUpdateInput": chunk},
            )
            try:
                j = r.json()
            except Exception as e:  # noqa: BLE001
                raise GridFinalizeError(
                    f"UpdateUnifiedAdGroups: не-JSON HTTP {r.status_code}: {r.text[:160]}"
                ) from e
            if j.get("errors"):
                raise GridFinalizeError(
                    "UpdateUnifiedAdGroups: "
                    + json.dumps(j.get("errors"), ensure_ascii=False)[:400]
                )
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

"""Create-set feed/catalog/price helpers extracted from blueprint.py."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time

_BRAND_CANON_SET: set | None = None
_DEPS: dict = {}


def configure(deps: dict) -> None:
    """Bind blueprint dependencies used by feed/catalog helpers."""
    _DEPS.clear()
    _DEPS.update(deps)
    globals().update(deps)


def _first_url_feed(token: str, login: str, agency: str = "", strict: bool = False,
                    url_key: str = "") -> int:
    """/yandex.xml XML-фид аккаунта (SourceType=URL) → id, или первый fallback, или 0.
    Нужен для товарных объявлений tp1/tp5.
    При пустом v5 (часто 152 — нет баллов) фолбэк на список фидов по КУКЕ (Grid, без баллов).
    Пропускает фиды в ERROR-состоянии (cmc._dead_feed_ids — пополняется при FEED_NOT_EXIST retry).
    strict=True: если целевой фид не найден ни в v5, ни в Grid — возвращает 0 (не берёт первый
    попавшийся фид). Используется для single_feed/tp7, чтобы не создавать кампанию с чужим фидом.
    url_key: искать фид по этому ключу вместо /yandex.xml (фолбэк-фид из feed_alert)."""
    from .create_set_input import feed_row_matches_key, prefer_single_feed_rows
    from .create_set_input import SINGLE_FEED_KEY as _SF_KEY
    _match_key = (url_key or _SF_KEY).strip()

    def feed_row_matches_single_feed(row):
        return feed_row_matches_key(row, _match_key)
    if token:
        try:
            jf = _v5_get("feeds", token, login, ["Id", "Name", "SourceType"])
            allowed = _allowed_feed_keys()
            if not allowed:
                return 0
            rows = []
            for f in (jf.get("result") or {}).get("Feeds", []):
                if f.get("SourceType") != "URL":
                    continue
                try:
                    fid = int(f["Id"])
                except (TypeError, ValueError):
                    continue
                if fid in cmc._dead_feed_ids:
                    continue
                if not _feed_row_allowed(f, allowed):
                    continue
                rows.append(f)
            if rows:
                if strict:
                    for f in rows:
                        if feed_row_matches_single_feed(f):
                            try:
                                return int(f["Id"])
                            except (TypeError, ValueError, KeyError):
                                continue
                    # strict-мисс по v5 — НЕ приговор: v5-строки без Url (FieldNames не включают
                    # UrlFeed) матчатся только по имени кабинета. Проваливаемся в Grid-ветку ниже —
                    # там реальные url-поля (ревью 03.07 #1: «/yandex.xml не найден» при живом фиде).
                    pass
                else:
                    for f in prefer_single_feed_rows(rows):
                        try:
                            return int(f["Id"])
                        except (TypeError, ValueError, KeyError):
                            continue
        except Exception:  # noqa: BLE001
            pass
    if agency:                                            # v5 пусто/152 → по куке
        grid_rows = _filter_allowed_feed_rows(_grid_feeds(login, agency))
        if strict:
            for f in grid_rows:
                if not feed_row_matches_single_feed(f):
                    continue
                try:
                    fid = int(f["id"])
                except (TypeError, ValueError, KeyError):
                    continue
                if fid in cmc._dead_feed_ids:
                    continue
                return fid
            return 0  # strict: /yandex.xml не найден в Grid-фидах — не брать fallback
        for f in prefer_single_feed_rows(grid_rows):
            try:
                fid = int(f["id"])
            except (TypeError, ValueError, KeyError):
                continue
            if fid in cmc._dead_feed_ids:
                continue
            return fid
    return 0


def _catalog_feed(token: str, login: str, prefer_id: int = 0, agency: str = "") -> int:
    """Фид «страницы каталога» для tp7 (listings). Правило пользователя: берём ТОТ ЖЕ,
    что под товары (prefer_id); иначе фид с именем yandex-catalog.xml. Фолбэк по КУКЕ (Grid) при 152."""
    if prefer_id:
        return prefer_id
    if token:
        try:
            jf = _v5_get("feeds", token, login, ["Id", "Name", "SourceType"])
            allowed = _allowed_feed_keys()
            if not allowed:
                return 0
            for f in (jf.get("result") or {}).get("Feeds", []):
                nm = (f.get("Name") or "").lower()
                if not _feed_row_allowed(f, allowed):
                    continue
                if "yandex-catalog" in nm or nm.replace(" ", "").endswith("catalog.xml"):
                    return int(f["Id"])
        except Exception:  # noqa: BLE001
            pass
    if agency:                                            # v5 пусто/152 → по куке: фид с listings (каталог) или первый
        rows = _filter_allowed_feed_rows(_grid_feeds(login, agency))
        for f in rows:                                    # предпочитаем фид с листингами (страницы каталога)
            if (f.get("listings") or []):
                try:
                    return int(f["id"])
                except (TypeError, ValueError, KeyError):
                    pass
        for f in rows:
            try:
                return int(f["id"])
            except (TypeError, ValueError, KeyError):
                continue
    return 0


# ── Модельные коллекции фидов (для фильтра товарных по модели — «только Lada Granta») ──────────
_FEEDS_QUERY = ("query Feeds($login:String!){client(searchBy:{login:$login}){"
                "feeds{rowset{id name url listings{id name}}}}}")


def _grid_feeds(login: str, agency: str) -> list:
    """rowset фидов аккаунта через grid (куки агентства): [{id,name,listings:[{id,name}]}]. [] при сбое."""
    import requests as _rqs
    try:
        cookie = cmc.pick_working_cookie(login, accounts=((agency,) if agency else cmc.DEFAULT_COOKIE_ACCOUNTS))
    except Exception:  # noqa: BLE001
        return []
    if not cookie:
        return []
    csrf = _block_bootstrap(cookie, agency)            # self-probe → CSRF
    headers = {"Cookie": cookie, "dna-operation-name": "Feeds", "x-direct-api": "1",
               "x-detected-locale": "ru", "Content-Type": "application/json", "User-Agent": cmc.USER_AGENT}
    if csrf:
        headers["x-csrf-token"] = csrf
    url = f"{_GRID_URL}?operationName=Feeds&ulogin={login}"
    payload = {"operationName": "Feeds", "query": _FEEDS_QUERY, "variables": {"login": login}}
    try:
        r = _rqs.post(url, json=payload, headers=headers, timeout=60, verify=False)
        if r.status_code == 403:                       # протух CSRF → один ретрай
            c2 = _grid_csrf(r)
            if c2:
                headers["x-csrf-token"] = c2
                r = _rqs.post(url, json=payload, headers=headers, timeout=60, verify=False)
        data = r.json()
        return (((data.get("data") or {}).get("client") or {}).get("feeds") or {}).get("rowset") or []
    except Exception:  # noqa: BLE001
        return []


def _account_model_feeds(login: str, agency: str, catalog_only: bool = False) -> list[dict]:
    """ВСЕ фиды аккаунта с модельными коллекциями (listings вида 'model_N').
    → [{"id":int, "name":str, "models":{listing_name_lower: collection_id}}]. Фиды без моделей отброшены.

    catalog_only=True → оставить ТОЛЬКО фиды с role='catalog' в Глобальных правилах (лендинг-фиды
    role='landing' отброшены — их model-листинги пусты, из-за чего tp1 удаляла кампанию). Флаг
    задаёт ТОЛЬКО tp1 (см. call-site run_create_set_tp1); tp7/product зовёт без флага (все enabled)."""
    out = []
    cat_keys = _catalog_feed_keys() if catalog_only else None
    for f in _filter_allowed_feed_rows(_grid_feeds(login, agency)):
        if cat_keys is not None and not _feed_row_allowed(f, cat_keys):
            continue                                   # не-каталог (лендинг/оффер) фид → пропускаем
        models = {}
        for l in (f.get("listings") or []):
            lid = str(l.get("id") or "")
            if lid.startswith("model_") and l.get("name"):
                models[(l.get("name") or "").strip().lower()] = lid
        if models:
            try:
                out.append({"id": int(f["id"]), "name": (f.get("name") or "").strip(), "models": models})
            except (TypeError, ValueError):
                continue
    return out


# ── Цена в комбинаторном объявлении из фида (Grid по куке, без баллов) ──────────
# FeedOffersPreview → цена товара (current/old); UpdateAdaptiveTextAds → проставить adPrice на объявление.
_FEED_OFFERS_Q = ("query FeedOffersPreview($feedId:Long!$filterConditions:[GdSmartFilterConditionInput!]!"
                  "$filterMobileAppOffers:Boolean$bannerId:Long$campaignId:Long){feedOffersPreview("
                  "feedId:$feedId filterConditions:$filterConditions filterMobileAppOffers:$filterMobileAppOffers "
                  "bannerId:$bannerId campaignId:$campaignId){previews{price{current old} text{name} targetUrl}}}")
_UPD_ADAPTIVE_Q = ("mutation UpdateAdaptiveTextAds($updateInput:GdUpdateAdaptiveTextAdsInput!){"
                   "updateAdaptiveTextAds(input:$updateInput){updatedAds{id}validationResult{"
                   "errors{code path params}warnings{code path params}}}}")  # warnings added fix 2


def _offer_price_keys(name: str) -> set:
    """Ключи цены из имени оффера (lower, промо/хвост уже срезаны). Нормализация чтобы:
      • модель группы матчилась несмотря на ГОД/тех.хвост: «baic u5 plus 2026» → ключ «baic u5 plus»;
      • офферы вида «автомобиль baic x75 1.5 л. (177 л.с)» дали бренд «baic», а не «автомобиль»
        (иначе реальные офферы со скидкой теряются из бренд-фолбэка).
    Возвращает набор {полное, без-года, бренд(1-е слово)} для исходного и очищенного вида."""
    base = (name or "").strip().lower()
    if not base:
        return set()
    keys: set = set()

    def _add(s: str) -> None:
        s = re.sub(r"\s+", " ", (s or "").strip())
        if s:
            keys.add(s)
            if " " in s:
                keys.add(s.split()[0])

    _add(base)                                                   # исходное имя (обратная совместимость)
    cleaned = re.sub(r"^(?:автомобиль|автомобили|машина|машины|авто|новый|новая|новые)\s+", "", base)
    cleaned = re.split(r"\s+\d+(?:[.,]\d+)?\s*л\b", cleaned, maxsplit=1)[0]   # тех.хвост: «1.5 л …»
    cleaned = re.split(r"\s*\(", cleaned, maxsplit=1)[0]          # «(177 л.с)»
    _add(cleaned)
    _add(re.sub(r"\s*\b20\d\d\b", " ", cleaned))                 # без года: «baic u5 plus 2026» → «baic u5 plus»
    return keys


def _merge_price(prev: tuple | None, new: tuple) -> tuple:
    """Выбрать лучшую пару (current, old): чистый МИНИМУМ current; при равном — больший old
    (зачёркнутая цена). Приоритет скидки НЕ даём — цена «от X» важнее наличия crossed-out."""
    cur, old = new
    if prev is None:
        return new
    prev_cur, prev_old = prev
    if cur < prev_cur or (cur == prev_cur and old > prev_old):
        return new
    return prev


def _grid_feed_offer_prices(login: str, feed_id: int) -> dict:
    """Цены товаров фида по куке (без баллов): {ключ(lower): (current:int, old:int)} — для бренда
    (первое слово) и для модели целиком берём САМЫЙ ДЕШЁВЫЙ оффер («от X» в комбинаторном). {} при сбое."""
    if not feed_id:
        return {}
    try:
        gc = gf.GridClient(login)
        gc._bootstrap_csrf()
        v = {"feedId": int(feed_id), "filterConditions": [], "filterMobileAppOffers": False,
             "bannerId": None, "campaignId": None}
        r = gc._post("FeedOffersPreview", _FEED_OFFERS_Q, v)
        if r.status_code == 403:
            r = gc._post("FeedOffersPreview", _FEED_OFFERS_Q, v)
        prev = ((r.json().get("data") or {}).get("feedOffersPreview") or {}).get("previews") or []
    except Exception:  # noqa: BLE001
        return {}
    prices: dict = {}
    for o in prev:
        p = o.get("price") or {}
        try:
            cur, old = int(p.get("current") or 0), int(p.get("old") or 0)
        except (TypeError, ValueError):
            continue
        if cur <= 0:
            continue
        name = ((o.get("text") or {}).get("name") or "").split(":")[0].strip()
        # Чистим промо-обёртку: «-35% renault arkana — за заявку» / «skoda … от 2 681 000 р. звоните»
        # → «renault arkana» / «skoda …». Иначе split()[0] = «-35%» (бренд-матч ломался).
        name = re.sub(r"^\s*[-–]?\s*\d+\s*%\s*", "", name)            # ведущий «-35% »
        name = re.split(r"\s+(?:—|–|от|за)\s", name, maxsplit=1)[0]   # хвост «— за заявку» / «от … р»
        name = name.strip().lower()
        if not name:
            continue
        keys = _offer_price_keys(name)                              # модель(+без года) + бренд (без «автомобиль»)
        for k in keys:
            prices[k] = _merge_price(prices.get(k), (cur, old))
    return prices


_OFFER_URL_CACHE: dict = {}   # (login, feed_id) → (url_map, ts)


def _grid_feed_offer_urls(login: str, feed_id: int) -> dict:
    """URL страницы модели из фида (targetUrl): {ключ(lower) → url} — первый оффер на ключ.
    Нормализация та же, что _offer_price_keys. Кэш 20 мин. {} при сбое или feed_id=0."""
    if not feed_id:
        return {}
    _key = (login, feed_id)
    _hit = _OFFER_URL_CACHE.get(_key)
    if _hit and (time.time() - _hit[1]) < _OFFER_PRICE_TTL:
        return _hit[0]
    try:
        gc = gf.GridClient(login)
        gc._bootstrap_csrf()
        v = {"feedId": int(feed_id), "filterConditions": [], "filterMobileAppOffers": False,
             "bannerId": None, "campaignId": None}
        r = gc._post("FeedOffersPreview", _FEED_OFFERS_Q, v)
        if r.status_code == 403:
            r = gc._post("FeedOffersPreview", _FEED_OFFERS_Q, v)
        prev = ((r.json().get("data") or {}).get("feedOffersPreview") or {}).get("previews") or []
    except Exception:  # noqa: BLE001
        return {}
    urls: dict = {}
    for o in prev:
        turl = (o.get("targetUrl") or "").strip()
        if not turl:
            continue
        name = ((o.get("text") or {}).get("name") or "").split(":")[0].strip()
        name = re.sub(r"^\s*[-–]?\s*\d+\s*%\s*", "", name)
        name = re.split(r"\s+(?:—|–|от|за)\s", name, maxsplit=1)[0].strip().lower()
        if not name:
            continue
        for k in _offer_price_keys(name):
            if k not in urls:          # первый оффер на ключ
                urls[k] = turl
    if urls:
        _OFFER_URL_CACHE[_key] = (urls, time.time())
    return urls


def _feed_url_for_model(urls: dict, model: str) -> str | None:
    """targetUrl из фида для модели/марки. Логика та же, что _ad_price_for_brand."""
    b = (model or "").strip().lower()
    if not b or not urls:
        return None
    if b in urls:
        return urls[b]
    b_noyear = re.sub(r"\s*\b20\d\d\b", " ", b).strip()
    if b_noyear != b and b_noyear in urls:
        return urls[b_noyear]
    return urls.get(b.split()[0]) if b else None


def _ad_price_for_brand(prices: dict, brand: str, *, no_brand_fallback: bool = False) -> tuple:
    """(current, old) под бренд/модель группы из карты цен фида; (0,0) если нет совпадения.
    Стрипаем год (20\\d\\d) из имени группы — ct вида «BAIC X35 2026» матчит ключ «baic x35».
    no_brand_fallback=True (ПРАВКА 5, seg=='Модели'): нет точного ключа → (0,0), НЕ фолбэчить
    на b.split()[0] — это цена ДРУГОГО оффера (BAIC X55→2500000 вместо 1960000 фида)."""
    b = (brand or "").strip().lower()
    if not b:
        return (0, 0)
    if b in prices:
        return prices[b]
    b_noyear = re.sub(r"\s*\b20\d\d\b", " ", b).strip()
    if b_noyear != b and b_noyear in prices:
        return prices[b_noyear]
    if no_brand_fallback:
        return (0, 0)                                      # Модели: нет точного ключа → пустая цена
    return prices.get(b.split()[0], (0, 0))               # по бренду (первое слово модели)


def _min_offer_price(prices: dict) -> tuple:
    """Минимальная цена товара из фида для общих групп, где нет марки/модели."""
    best: tuple[int, int] = (0, 0)
    for cur, old in (prices or {}).values():
        try:
            cur_i, old_i = int(cur or 0), int(old or 0)
        except (TypeError, ValueError):
            continue
        if cur_i <= 0:
            continue
        if not best[0] or cur_i < best[0] or (cur_i == best[0] and old_i > best[1]):
            best = (cur_i, old_i)
    return best


def _group_ad_price(prices: dict, brand: str, seg: str = "") -> tuple:
    """(current, old) для adPrice С УЧЁТОМ СЕГМЕНТА группы (правило пользователя):
      seg=='Марки' (группа ТОЛЬКО по марке, напр. «Changan») → МИНИМАЛЬНАЯ цена по марке —
        всегда ключ-бренд (первое слово), а не цена конкретной модели. _grid_feed_offer_prices
        уже агрегирует минимум оффера на ключ-бренд, поэтому берём именно его.
      Модели → цена модели целиком, при отсутствии — фолбэк на бренд (_ad_price_for_brand).
      Общее/аудиторные ct → минимальный товар из фида + безопасная старая цена на этапе записи."""
    if seg and seg not in ("Марки", "Модели"):
        return _min_offer_price(prices)
    b = (brand or "").strip().lower()
    if not b:
        return _min_offer_price(prices)
    if seg == "Марки":
        pr = prices.get(b.split()[0], prices.get(b, (0, 0)))     # МИН цена марки (ключ-бренд)
    else:                                                        # Модели
        pr = _ad_price_for_brand(prices, brand, no_brand_fallback=True)  # ПРАВКА 5: нет → (0,0)
    # Правило Семёна (2026-07-02): марки/модели НЕТ в фиде → цена ПУСТАЯ (тумблер выключен),
    # НЕ подставлять минимальную цену фида — «Tank от 789 900 ₽» вводит в заблуждение.
    # Фолбэк _min_offer_price остаётся только для Общее/аудиторных групп (выше, без марки).
    return pr if pr and pr[0] else (0, 0)


def _safe_old_price(current: int, old: int = 0) -> int:
    """Вернуть старую цену для adPrice. Если фид не дал old или old<=current,
    создаём безопасную зачёркнутую цену выше текущей."""
    try:
        cur = int(current or 0)
        old_i = int(old or 0)
    except (TypeError, ValueError):
        return 0
    if cur <= 0:
        return 0
    if old_i > cur:
        return old_i
    # +12%, округление вверх до 10 000 ₽: выглядит как нормальная старая цена и гарантирует old>cur.
    import math
    return int(math.ceil((cur * 1.12) / 10000.0) * 10000)


def _grid_ad_price_payload(current: int, old: int = 0) -> dict | None:
    try:
        cur = int(current or 0)
    except (TypeError, ValueError):
        return None
    if cur <= 0:
        return None
    old_i = _safe_old_price(cur, old)
    # prefix="FROM" → в UI Директа цена показывается как «от X» (HAR 29, UpdateAdaptiveTextAds.adPrice).
    # Раньше был None (= «Без префикса»). Правило директолога Михаила: всегда «от».
    return {"price": str(cur), "priceOld": str(old_i) if old_i else "",
            "prefix": "FROM", "currency": "RUB"}


def _norm_read_ad_price(raw: dict | None) -> dict | None:
    """Нормализовать bannerPrice из Grid-чтения к формату Grid-мутации.

    Grid READ возвращает: {"price": "2040546.00", "priceOld": null, "prefix": "FROM", "currency": "RUB"}
    Grid MUTATION ожидает: {"price": "2040546", "priceOld": "2285412", "prefix": "FROM", "currency": "RUB"}

    Десятичная строка в price (от READ) → Grid МОЛЧА сбрасывает bannerPrice при UpdateAdaptiveTextAds.
    Проверено live на кампании 712104527, ad 1914498802671820819 (2026-07-03).
    """
    if not isinstance(raw, dict):
        return None
    try:
        cur = int(float(raw.get("price") or 0))
    except (TypeError, ValueError):
        return None
    if cur <= 0:
        return None
    try:
        old_raw = int(float(raw.get("priceOld") or 0))
    except (TypeError, ValueError):
        old_raw = 0
    old_i = _safe_old_price(cur, old_raw)
    return {
        "price": str(cur),
        "priceOld": str(old_i) if old_i else "",
        "prefix": str(raw.get("prefix") or "FROM"),
        "currency": str(raw.get("currency") or "RUB"),
    }


# ── Персистентный (на время жизни процесса) кеш imageHash для Grid-аплоадов ──────────────
# imageHash в Яндексе живёт в библиотеке ЛОГИНА и валиден для всех его кампаний → каждую
# уникальную картинку достаточно залить ОДИН раз на аккаунт. Без этого кеша cookie/Grid-путь
# заливал те же бренд-картинки ЗАНОВО на каждую кампанию (десятки секунд × сотни кампаний).
# Ключ = (login, realpath) — НЕ basename: у разных брендов файлы могут зваться "1.jpg",
# basename-ключ перепутал бы хеши и размазал чужую картинку на все бренды.
_GRID_IMG_HASH_CACHE: dict[tuple[str, str], str] = {}
_GRID_IMG_HASH_LOCK = threading.Lock()
_GRID_IMG_CACHE_STATS = {"hit": 0, "miss": 0}
# Негативный кэш: после _IMG_FAIL_LIMIT неудач файл не пробуем _IMG_FAIL_TTL секунд — иначе
# упавший аплоад молча повторялся на КАЖДОЙ группе каждой кампании (лайв 03.07: 20+ мин
# впустую на джобу). TTL, а не «до конца процесса»: добивка IMAGE_MISSING работает в ТОМ ЖЕ
# процессе воркера — вечный blacklist не дал бы ей дозалить после восстановления Яндекса.
_GRID_IMG_FAIL_COUNT: dict[tuple[str, str], tuple[int, float]] = {}   # key → (fails, last_ts)
# Лимит 3, не 2: check→upload→increment разнесены по секциям лока (upload сознательно вне),
# create-воркер и delayed-repair daemon могут упасть на одном key одновременно — при лимите 2
# один транзиентный сбой Яндекса сжигал бы весь бюджет за раунд (ревью 03.07, TOCTOU).
_IMG_FAIL_LIMIT = 3
_IMG_FAIL_TTL = 900.0                                                 # 15 мин


def _reset_img_fail_cache(login: str, paths: list | None = None) -> int:
    """Снять blacklist неудачных аплоадов: точечно по paths (realpath-нормализация как в кэше)
    или по всему логину. Зовёт images_repair перед повторной заливкой — добивка обязана
    попробовать заново. paths ОБЯЗАТЕЛЕН из repair-цикла по кампаниям: сброс всего логина
    на каждую кампанию ре-армил бы файлы, только что заблэклищенные create-воркером (ревью 03.07)."""
    if paths is not None:
        want = set()
        for p in paths:
            if not p:
                continue
            try:
                want.add(os.path.realpath(p))
            except Exception:  # noqa: BLE001
                want.add(p)
        with _GRID_IMG_HASH_LOCK:
            keys = [k for k in _GRID_IMG_FAIL_COUNT if k[0] == login and k[1] in want]
            for k in keys:
                _GRID_IMG_FAIL_COUNT.pop(k, None)
        return len(keys)
    with _GRID_IMG_HASH_LOCK:
        keys = [k for k in _GRID_IMG_FAIL_COUNT if k[0] == login]
        for k in keys:
            _GRID_IMG_FAIL_COUNT.pop(k, None)
    return len(keys)


def _cached_upload_image(gc_img, login: str, path: str):
    """Залить картинку в библиотеку логина через Grid с переиспользованием imageHash между
    кампаниями/воркерами одного процесса. Потокобезопасно (общий lock на кеш). Возвращает
    imageHash (str) или None. realpath-ключ гарантирует точное соответствие файл→хеш."""
    if not path:
        return None
    try:
        key = (login, os.path.realpath(path))
    except Exception:  # noqa: BLE001
        key = (login, path)
    _hit_print = None
    with _GRID_IMG_HASH_LOCK:
        _h = _GRID_IMG_HASH_CACHE.get(key)
        if _h:
            _GRID_IMG_CACHE_STATS["hit"] += 1
            _hit_n = _GRID_IMG_CACHE_STATS["hit"]
            # HIT частый — печатаем каждый 25-й (журнал не засоряем, но видно что кеш живой).
            # Сам print — ВНЕ лока: блокирующий write в забитый pipe держал бы общий лок кэша.
            if _hit_n % 25 == 0:
                _hit_print = (f"[img-cache] HIT total={_hit_n} "
                              f"miss={_GRID_IMG_CACHE_STATS['miss']} "
                              f"{login} {os.path.basename(path)}")
        else:
            _fails, _last_ts = _GRID_IMG_FAIL_COUNT.get(key, (0, 0.0))
            if _fails >= _IMG_FAIL_LIMIT:
                if time.time() - _last_ts < _IMG_FAIL_TTL:
                    return None                        # blacklisted — причина уже в журнале
                _GRID_IMG_FAIL_COUNT.pop(key, None)    # TTL истёк — даём новую попытку
    if _h:
        if _hit_print:
            try:
                print(_hit_print, flush=True)
            except Exception:  # noqa: BLE001
                pass
        return _h
    # miss — грузим ВНЕ лока (медленный сетевой вызов не должен блокировать другие воркеры)
    _h = gc_img.upload_image(path)
    if _h:
        with _GRID_IMG_HASH_LOCK:
            _GRID_IMG_HASH_CACHE[key] = _h
            _GRID_IMG_FAIL_COUNT.pop(key, None)
            _GRID_IMG_CACHE_STATS["miss"] += 1
            _miss_n = _GRID_IMG_CACHE_STATS["miss"]
        # MISS = реальный аплоад в Яндекс (теперь РЕДКИЙ, 1 раз на картинку аккаунта) — печатаем всегда
        try:
            print(f"[img-cache] MISS->upload total={_miss_n} hit={_GRID_IMG_CACHE_STATS['hit']} "
                  f"{login} {os.path.basename(path)}", flush=True)
        except Exception:  # noqa: BLE001
            pass
    else:
        with _GRID_IMG_HASH_LOCK:
            _fails, _ = _GRID_IMG_FAIL_COUNT.get(key, (0, 0.0))
            _GRID_IMG_FAIL_COUNT[key] = (_fails + 1, time.time())
            _blk = _fails + 1 == _IMG_FAIL_LIMIT
        if _blk:
            try:
                print(f"[img-cache] BLACKLIST на {int(_IMG_FAIL_TTL // 60)} мин после "
                      f"{_IMG_FAIL_LIMIT} неудач: {login} {os.path.basename(path)}", flush=True)
            except Exception:  # noqa: BLE001
                pass
    return _h


def _homepage_url(href: str) -> str:
    """Главная сайта (scheme://host) из любого URL объявления — для кнопки «Получить скидку»."""
    m = re.match(r"(https?://[^/]+)", (href or "").strip())
    return m.group(1) if m else ""


def _combo_button(href: str) -> dict | None:
    """Кнопка РСЯ «Получить скидку» → ТА ЖЕ ссылка, что у объявления (Семён: кнопка обязана вести
    туда же, куда объявление, а не на главную). Только для комбинаторных (адаптивных) объявлений
    (GdBannerButton, HAR39). action=GET_DISCOUNT = предустановленный текст «Получить скидку».
    href = ссылка объявления (as-is, чтобы совпадала точь-в-точь). None если href пуст."""
    h = (href or "").strip()
    # href обязан быть абсолютным (scheme://host…) — иначе кнопка Директа отклонит невалидный URL.
    # Относительный/без схемы → кнопку не вешаем (как раньше _homepage_url возвращал '' → она пропускалась).
    return {"action": "GET_DISCOUNT", "href": h} if re.match(r"https?://", h) else None


def _grid_set_ad_prices(login: str, items: list) -> int:
    """Проставить adPrice пачкой на комбинаторные объявления (Grid UpdateAdaptiveTextAds, по куке).
    items: [{id, href, titles, bodies, image_hashes, current, old}]. → число обновлённых.
    Поля inheritableCallouts/inheritableSitelinkSet НЕ шлём — иначе их policy=CLEAR затрёт
    быстрые ссылки/уточнения (проверено: без них ссылки сохраняются)."""
    upd = []
    for it in (items or []):
        if not it.get("current"):
            continue
        _old = _safe_old_price(it.get("current"), it.get("old"))
        _payload = _grid_ad_price_payload(it.get("current"), _old)
        if not _payload:
            continue
        _item = {"href": it.get("href") or "", "hrefParams": "",
                 "titles": it.get("titles") or [], "bodies": it.get("bodies") or [],
                 "imageHashes": it.get("image_hashes") or [], "creativeIds": [],
                 "adPrice": _payload,
                 "id": str(it["id"])}
        upd.append(_item)   # КНОПКУ тут НЕ шлём — ставится отдельным апдейтом (см. _grid_update_adaptive_ads)
    if not upd:
        return 0
    try:
        gc = gf.GridClient(login)
        gc._bootstrap_csrf()
        v = {"updateInput": {"adUpdateItems": upd, "saveDraft": True}}
        r = gc._post("UpdateAdaptiveTextAds", _UPD_ADAPTIVE_Q, v)
        if r.status_code == 403:
            r = gc._post("UpdateAdaptiveTextAds", _UPD_ADAPTIVE_Q, v)
        j = r.json()
        _mut = (j.get("data") or {}).get("updateAdaptiveTextAds") or {}
        _vr = _mut.get("validationResult") or {}
        if _vr.get("warnings"):  # fix 2: log adPrice warnings для диагностики field-name проблемы
            import logging as _lg
            _lg.getLogger("direct.feeds").warning(
                "adPrice UpdateAdaptiveTextAds warns login=%s: %s", login, str(_vr["warnings"])[:400])
        return len(_mut.get("updatedAds") or [])
    except Exception:  # noqa: BLE001
        return 0


def _grid_update_adaptive_ads(login: str, items: list[dict],
                               campaign_ids: list[int] | None = None) -> int:
    """Обновить комбинаторные объявления через Grid UpdateAdaptiveTextAds.
    items: [{id, href, titles, bodies, image_hashes?, creative_ids?, adPrice?}, ...]
    campaign_ids: если передан — выполняем read-modify-write: читаем текущее состояние объявлений
    и для полей отсутствующих/пустых в item берём значение из Grid. Это предотвращает затирание
    imageHashes/adPrice/creativeIds полем full-replace при частичном обновлении (напр. видео-attach).
    При сбое чтения — fall-safe: отправляем как раньше (не ломаем создание кампании)."""
    # RMW: читаем текущее состояние если знаем campaign_ids
    current_map: dict[int, dict] = {}
    if campaign_ids:
        try:
            ad_ids = [int(it["id"]) for it in (items or []) if it.get("id")]
            cids = [int(c) for c in (campaign_ids or []) if c]
            if cids and ad_ids:
                current_map = gf.GridClient(login).adaptive_ads_for_update(cids, ad_ids)
        except Exception:  # noqa: BLE001 — fall-safe: при сбое чтения шлём как есть
            current_map = {}

    upd = []
    skipped_bare = 0
    for it in (items or []):
        if not it.get("id"):
            continue
        aid = int(it["id"])
        cur = current_map.get(aid) or {}
        # GUARD (ревью 03.07 #3/#19): «голый» item (без своих titles/href — фиксеры кнопки/заголовков
        # шлют только id) при НЕудавшемся RMW-чтении дал бы ПУСТОЙ full-replace → затирание живого
        # объявления. Такие item'ы пропускаем: фикс не применится (повторит следующий цикл), но
        # контент цел. Старые вызовы с полным payload проходят как раньше (fall-safe).
        if not cur and not (it.get("titles") and it.get("href")):
            skipped_bare += 1
            continue
        # Full-replace: поля отсутствующие/пустые в item берём из текущего состояния (RMW).
        href = it.get("href") or cur.get("href") or ""
        titles = it.get("titles") or cur.get("titles") or []
        bodies = it.get("bodies") or cur.get("bodies") or []
        # imageHashes: берём из item если явно передан и не пуст; иначе из current (не стираем).
        # dict.fromkeys — дубль хэшей валит объявление MUST_NOT_CONTAIN_DUPLICATED_ELEMENTS при
        # updatedAds:[null] (лайв 03.07: пул ct с одинаковыми файлами → один hash → «ok» без эффекта).
        if "image_hashes" in it and it.get("image_hashes"):
            image_hashes = list(dict.fromkeys(it["image_hashes"]))
        elif "image_hashes" in it and it.get("image_hashes") is not None and not cur:
            # явная пустая передача без current-данных — оставляем пустым как раньше
            image_hashes = []
        else:
            image_hashes = list(dict.fromkeys(cur.get("imageHashes") or []))
        # Видео: явные creative_ids вызывающего кода, иначе — из RMW-чтения (typedCreatives,
        # VIDEO_ADDITION; читаемо — подтверждено live 03.07.2026). Раньше поле было нечитаемо
        # (FieldUndefined на creativeIds) и частичный апдейт стирал видео.
        creative_ids = ([str(c) for c in (it.get("creative_ids") or []) if c]
                        or [str(c) for c in (cur.get("creativeIds") or []) if c])
        # _norm_read_ad_price: raw bannerPrice из adaptive_ads_for_update имеет decimal-строки
        # ("2040546.00") — Grid молча сбрасывает цену если передать их обратно в UpdateAdaptiveTextAds.
        ad_price = it.get("adPrice") or _norm_read_ad_price(cur.get("adPrice"))

        item = {
            "href": href,
            "hrefParams": "",
            "titles": titles,
            "bodies": bodies,
            "imageHashes": image_hashes,
            "creativeIds": creative_ids,
            "id": str(it["id"]),
        }
        if ad_price:
            item["adPrice"] = ad_price
        # Ревью 03.07 #17: СУЩЕСТВУЮЩУЮ кнопку проносим в основной payload (full-replace её стирал,
        # а повторная установка _apply_combo_button — best-effort). НОВУЮ кнопку по-прежнему ставит
        # отдельный апдейт ПОСЛЕ (изоляция отказов, code-review #4).
        _btn = it.get("button") or (cur.get("button") if isinstance(cur.get("button"), dict) else None)
        if _btn and _btn.get("action") and _btn.get("href"):
            item["button"] = {"action": _btn["action"], "href": _btn["href"]}
        upd.append(item)   # КНОПКА (новая) — отдельным апдейтом ПОСЛЕ (изоляция, code-review #4)
    if skipped_bare:
        print(f"[rmw-guard] {skipped_bare} голых item'ов пропущено (RMW-чтение не дало current) "
              f"login={login}", flush=True)
    if not upd:
        return 0
    try:
        gc = gf.GridClient(login)
        gc._bootstrap_csrf()
        v = {"updateInput": {"adUpdateItems": upd, "saveDraft": True}}
        r = gc._post("UpdateAdaptiveTextAds", _UPD_ADAPTIVE_Q, v)
        if r.status_code == 403:
            r = gc._post("UpdateAdaptiveTextAds", _UPD_ADAPTIVE_Q, v)
        j = r.json()
        res = ((j.get("data") or {}).get("updateAdaptiveTextAds") or {})
        # updatedAds содержит null для КАЖДОГО отвергнутого валидацией item'а — считать len()
        # значило «ok» без эффекта (лайв 03.07: 15×null при MUST_NOT_CONTAIN_DUPLICATED_ELEMENTS).
        n = len([x for x in (res.get("updatedAds") or []) if x])
        _val_errs = ((res.get("validationResult") or {}) or {}).get("errors") or []
        if _val_errs:
            print(f"[rmw-update] validationResult: {len(_val_errs)} ошибок, применено {n}/{len(upd)}: "
                  f"{json.dumps(_val_errs[:3], ensure_ascii=False)[:300]} login={login}", flush=True)
        _apply_combo_button(gc, upd)   # кнопка ОТДЕЛЬНО: её отказ (напр. чистый Поиск) не роняет картинки/цену
        return n
    except Exception:  # noqa: BLE001
        return 0


def _apply_combo_button(gc, upd_items: list) -> int:
    """Поставить кнопку «Получить скидку» ОТДЕЛЬНЫМ UpdateAdaptiveTextAds — ПОСЛЕ картинок/цены, чтобы
    возможный отказ Grid на кнопке (напр. на чистом Поиске tp2/tp4) НЕ ронял батч с картинками/ценой
    (code-review #4). Полный payload (full-replace) обязателен → копируем поля item + button. Best-effort."""
    btn_items = []
    for it in (upd_items or []):
        b = _combo_button(it.get("href") or "")
        if not b:
            continue
        bi = {k: it[k] for k in ("href", "hrefParams", "titles", "bodies",
                                 "imageHashes", "creativeIds", "adPrice", "id") if k in it}
        bi["button"] = b
        btn_items.append(bi)
    if not btn_items:
        return 0
    try:
        v = {"updateInput": {"adUpdateItems": btn_items, "saveDraft": True}}
        r = gc._post("UpdateAdaptiveTextAds", _UPD_ADAPTIVE_Q, v)
        if r.status_code == 403:
            r = gc._post("UpdateAdaptiveTextAds", _UPD_ADAPTIVE_Q, v)
        j = r.json()
        return len(((j.get("data") or {}).get("updateAdaptiveTextAds") or {}).get("updatedAds") or [])
    except Exception:  # noqa: BLE001 — кнопка best-effort, картинки/цена уже применены
        return 0


_GRID_FEEDS_CNT_Q = ("query Feeds($login:String!$url:String$limitOffset:GdLimitOffsetInput){client(searchBy:{login:$login}){"
                     "feeds(limitOffset:$limitOffset url:$url){defaultFeedId rowset{id name offersCount}}}}")
# Фиды для ЦЕН (по указанию пользователя): у них ЧИСТЫЕ имена офферов «бренд модель …», тогда как
# дефолтный фид часто имеет промо-префикс «-35% renault arkana — за заявку» → split()[0]='-35%' (матч ломался).
_PRICE_FEED_PREFS = ("zabronirovat-01-a", "yandex-used-auto", "yandex-catalog-model-design-custom-name")


def _grid_price_feed(login: str, url: str = "") -> int:
    """Фид аккаунта для ЦЕН (по куке, без баллов): как веб-UI — defaultFeedId по домену (url),
    иначе фид с наибольшим числом офферов. 0 при сбое. url = https://домен аккаунта."""
    try:
        gc = gf.GridClient(login)
        gc._bootstrap_csrf()
        # url → КОРЕНЬ домена (homepage): deep-link (/auto/baic) сузил бы фиды по пути и вернул 0
        # (фиды на корне) → цена не считалась бы. Корень = ВСЕ фиды аккаунта (как при заполнении товаров).
        v = {"login": login, "url": (_homepage_url(url) or None), "limitOffset": {"limit": 1000, "offset": 0}}
        r = gc._post("Feeds", _GRID_FEEDS_CNT_Q, v)
        if r.status_code == 403:
            r = gc._post("Feeds", _GRID_FEEDS_CNT_Q, v)
        fc = ((r.json().get("data") or {}).get("client") or {}).get("feeds") or {}
        if fc.get("defaultFeedId"):
            try:
                return int(fc["defaultFeedId"])
            except (TypeError, ValueError):
                pass
        best, best_n = 0, -1                              # фолбэк: максимум офферов
        for f in (fc.get("rowset") or []):
            try:
                n, fid = int(f.get("offersCount") or 0), int(f["id"])
            except (TypeError, ValueError, KeyError):
                continue
            if n > best_n:
                best, best_n = fid, n
        return best
    except Exception:  # noqa: BLE001
        return 0


def _price_feeds_for(login: str, url: str = "") -> list:
    """ID фидов для ЦЕН в порядке предпочтения пользователя (_PRICE_FEED_PREFS) — у них чистые имена
    офферов. Фолбэк: defaultFeedId / фид с макс. офферов. → [feed_id, ...] (без баллов, по куке)."""
    try:
        gc = gf.GridClient(login)
        gc._bootstrap_csrf()
        # url → КОРЕНЬ домена (homepage): deep-link (/auto/baic) сузил бы фиды по пути и вернул 0
        # (фиды на корне) → цена не считалась бы. Корень = ВСЕ фиды аккаунта (как при заполнении товаров).
        v = {"login": login, "url": (_homepage_url(url) or None), "limitOffset": {"limit": 1000, "offset": 0}}
        r = gc._post("Feeds", _GRID_FEEDS_CNT_Q, v)
        if r.status_code == 403:
            r = gc._post("Feeds", _GRID_FEEDS_CNT_Q, v)
        fc = ((r.json().get("data") or {}).get("client") or {}).get("feeds") or {}
        rows = fc.get("rowset") or []
        # ВСЕ фиды аккаунта с офферами, упорядоченные по «чистоте имени» — мердж в _account_offer_prices
        # берёт МИН-цену, поэтому чистые имена (prefs → yandex-<бренд>) обрабатываются ПЕРВЫМИ и
        # выигрывают на конфликте. Бренды раскиданы по пер-брендовым фидам, а baic/belgee — только
        # в полном «dostup-k-rasprodazhe» → берём фиды ВСЕХ категорий, иначе у части марок нет цены.
        def _rank(name: str) -> int:
            nm = name or ""
            if any(p in nm for p in _PRICE_FEED_PREFS):
                return 0
            if "yandex-" in nm:                           # пер-брендовые фиды (чистые имена)
                return 1
            if any(s in nm for s in ("catalog", "rasprodazh", "dostup", "target")):
                return 2                                  # полный каталог (есть редкие марки)
            return 3
        feeds: list = []
        for f in rows:
            try:
                fid, n = int(f["id"]), int(f.get("offersCount") or 0)
            except (TypeError, ValueError, KeyError):
                continue
            if n > 0:
                feeds.append((_rank(f.get("name") or ""), -n, fid))
        feeds.sort()                                      # rank ↑, затем больше офферов
        ids = [fid for _, _, fid in feeds][:30]           # кап на патологические аккаунты
        if ids:
            return ids
        if fc.get("defaultFeedId"):                       # фолбэк: дефолтный фид домена
            try:
                return [int(fc["defaultFeedId"])]
            except (TypeError, ValueError):
                pass
        return []
    except Exception:  # noqa: BLE001
        return []


_OFFER_PRICE_CACHE: dict = {}                             # (login,url) → (price_map, ts); мердж 30 фидов дорог
_OFFER_PRICE_TTL = 20 * 60


def _account_offer_prices(login: str, url: str = "") -> dict:
    """Объединённая карта цен {ключ: (current, old)} из ВСЕХ фидов аккаунта (мердж; на конфликте —
    самый дешёвый оффер «от X»). Покрывает ВСЕ марки (раньше брался один фид → у baic/belgee цены не
    было). Кэш на аккаунт (TTL 20 мин): мердж до 30 фидов дорог, цены за джобу не меняются. {} при сбое."""
    key = (login, url)
    hit = _OFFER_PRICE_CACHE.get(key)
    if hit and (time.time() - hit[1]) < _OFFER_PRICE_TTL:
        return hit[0]
    out: dict = {}
    for fid in _price_feeds_for(login, url):
        for k, val in _grid_feed_offer_prices(login, fid).items():
            out[k] = _merge_price(out.get(k), val)
    # НЕ кэшируем пустую карту: транзиентный сбой фида/куки (403/таймаут/152) дал бы {} на 20 мин →
    # adPrice не проставился бы на ВСЕ объявления аккаунта. Пустой → следующий вызов перечитает.
    if out:
        _OFFER_PRICE_CACHE[key] = (out, time.time())
    return out


_OFFER_URL_CACHE_ACCT: dict = {}  # (login, url) → (url_map, ts); account-level URL merge


def _account_offer_urls(login: str, url: str = "") -> dict:
    """Объединённая карта targetUrl {ключ: url} из ВСЕХ фидов аккаунта (первый URL на ключ).
    Аналог _account_offer_prices для targetUrl: использует те же _price_feeds_for-ранги.
    Цены мёржились со всех фидов, URL — только с одного → модели без URL в том фиде
    падали на формульный _model_page_href (#Баг-8). Теперь покрытие то же, что у цен.
    Кэш 20 мин (TTL = _OFFER_PRICE_TTL). {} при сбое."""
    key = (login, url)
    hit = _OFFER_URL_CACHE_ACCT.get(key)
    if hit and (time.time() - hit[1]) < _OFFER_PRICE_TTL:
        return hit[0]
    out: dict = {}
    for fid in _price_feeds_for(login, url):
        for k, v in _grid_feed_offer_urls(login, fid).items():
            if k not in out:   # первый фид на ключ (предпочтительные идут первыми по рангу)
                out[k] = v
    if out:
        _OFFER_URL_CACHE_ACCT[key] = (out, time.time())
    return out


def _match_collection(model_name: str, feed_models: dict) -> str | None:
    """Модель группы (из ct, напр. 'Lada Granta') → collectionId фида по совпадению в имени listing
    ('Lada Granta в наличии…'). Сначала полное вхождение, затем по полному набору токенов модели.
    НЕЛЬЗЯ матчить только по последнему слову: это даёт ложные совпадения вроде
    'Changan CS55 Plus' -> 'BAIC U5 Plus' из-за общего хвоста 'plus'. None — нет."""
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^\wа-яё]+", " ", (s or "").lower())).strip()

    def _compact(s: str) -> str:
        return re.sub(r"[^\wа-яё]+", "", (s or "").lower()).strip()

    mn = _norm(model_name or "")
    if not mn or not feed_models:
        return None
    for lname, cid in feed_models.items():
        lname_n = _norm(lname or "")
        if mn and mn in lname_n:
            return cid
    mn_compact = _compact(model_name or "")
    if mn_compact:
        for lname, cid in feed_models.items():
            if mn_compact in _compact(lname or ""):
                return cid
    parts = [p for p in re.split(r"[\s/]+", mn) if p]
    if parts:
        for lname, cid in feed_models.items():
            lname_parts = set(_norm(lname or "").split())
            if all(p in lname_parts for p in parts):
                return cid
    if len(parts) > 1:
        brand = parts[0]
        tail_compact = _compact(" ".join(parts[1:]))
        if tail_compact and (re.search(r"\d", tail_compact) or len(tail_compact) >= 6):
            for lname, cid in feed_models.items():
                lname_n = _norm(lname or "")
                lname_compact = _compact(lname or "")
                if brand in lname_n.split() and tail_compact in lname_compact:
                    return cid
    return None


def _brand_collection_ids(brand_name: str, feed_models: dict) -> list[str]:
    """Все collectionId фида для марки.

    Для ListingAd брендовой группы v501 не принимает фильтр по vendor, поэтому собираем
    список model_* коллекций этой марки и фильтруем по collectionId EQUALS_ANY.
    """
    if not brand_name or not feed_models:
        return []
    brand_c = _brand_canon(re.split(r"[\s/]+", str(brand_name or "").strip().lower())[0])  # кир↔лат канон
    if not brand_c:
        return []
    out: list[str] = []
    for lname, cid in (feed_models or {}).items():
        tokens = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", str(lname or "").lower())
        if tokens and _brand_canon(tokens[0]) == brand_c and cid and cid not in out:
            out.append(str(cid))
    return out


# ── Коллекции фида для фильтра «Страницы каталога» (ListingAd) ──────────────────
# HAR-реверс (porg-24rg6hzy, feed 3501015): фильтр листинга = collectionId (НЕ vendor — vendor даёт
# 5005). Список коллекций тянется Grid-операцией Listings (feeds(filter:{searchBy:$feedId})). Общий
# _grid_feeds возвращает listings УРЕЗАННО (areListingsTruncated), поэтому для резолва нужен этот
# точечный per-feed запрос. Коллекции двухуровневые: бренд-уровень (id числовой, имя «Новые
# автомобили BAIC …») и модельные (id 'model_N', имя «BAIC BJ40 …»).
_FEED_LISTINGS_QUERY = ("query Listings($login:String!$feedId:String!){reqId:getReqId "
                        "client(searchBy:{login:$login}){feeds(limitOffset:{limit:1 offset:0}"
                        "filter:{searchBy:$feedId}){rowset{areListingsTruncated "
                        "listings{id name offerCount}}}}}")
_FEED_COLLECTIONS_CACHE: dict[tuple, list] = {}


def _feed_collections(login: str, feed_id: int, agency: str = "", cookie: str | None = None) -> list[dict]:
    """Коллекции (listings) фида через Grid op Listings. → [{id,name,offers}]. Кэш по (login,feed_id)."""
    if not feed_id:
        return []
    key = (login, int(feed_id))
    if key in _FEED_COLLECTIONS_CACHE:
        return _FEED_COLLECTIONS_CACHE[key]
    out: list[dict] = []
    try:
        import requests as _rqs
        ck = cookie or cmc.pick_working_cookie(
            login, accounts=((agency,) if agency else cmc.DEFAULT_COOKIE_ACCOUNTS))
        if ck:
            csrf = _block_bootstrap(ck, agency)
            headers = {"Cookie": ck, "dna-operation-name": "Listings", "x-direct-api": "1",
                       "x-detected-locale": "ru", "Content-Type": "application/json",
                       "User-Agent": cmc.USER_AGENT}
            if csrf:
                headers["x-csrf-token"] = csrf
            url = f"{_GRID_URL}?operationName=Listings&ulogin={login}"
            payload = {"operationName": "Listings", "query": _FEED_LISTINGS_QUERY,
                       "variables": {"login": login, "feedId": str(int(feed_id))}}
            r = _rqs.post(url, json=payload, headers=headers, timeout=40, verify=False)
            if r.status_code == 403:                       # протух CSRF → один ретрай
                c2 = _grid_csrf(r)
                if c2:
                    headers["x-csrf-token"] = c2
                    r = _rqs.post(url, json=payload, headers=headers, timeout=40, verify=False)
            data = r.json()
            rs = (((data.get("data") or {}).get("client") or {}).get("feeds") or {}).get("rowset") or []
            if rs:
                out = [{"id": str(l.get("id")), "name": (l.get("name") or ""),
                        "offers": l.get("offerCount") or 0}
                       for l in (rs[0].get("listings") or []) if l.get("id")]
    except Exception:  # noqa: BLE001
        out = []
    # Кэшируем ТОЛЬКО непустой результат: пустой/сбойный (протухла кука/152/403/сеть) НЕ сохраняем,
    # иначе один сбой навсегда лишал бы брендовые группы collectionId-фильтра (следующий вызов повторит).
    if out:
        _FEED_COLLECTIONS_CACHE[key] = out
    return out


# Бренд-матч для collectionId-фильтра: план-марка может быть кириллицей (Лада, Москвич, Хавал),
# а коллекция фида — латиницей (Lada, Moskvich, Haval) и наоборот → нужна нормализация к канону.
_RU2LAT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
           "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
           "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
           "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"}
# Явные алиасы марок (любая форма → канон-латиница). Покрывает непрямые случаи, где транслит не
# совпадает (Чери≠cheri, а chery; Бельджи≠belzhi, а belgee и т.п.).
_BRAND_CANON = {
    "лада": "lada", "ваз": "lada", "москвич": "moskvich", "хавал": "haval", "хавейл": "haval",
    "чери": "chery", "чанган": "changan", "хендай": "hyundai", "хёндай": "hyundai", "хундай": "hyundai",
    "киа": "kia", "ниссан": "nissan", "омода": "omoda", "шкода": "skoda", "фольксваген": "volkswagen",
    "бельджи": "belgee", "баик": "baic", "джили": "geely", "джип": "jeep", "джак": "jac",
    "эксид": "exeed", "джетур": "jetour", "джету": "jetour", "газ": "gaz", "уаз": "uaz",
    "тойота": "toyota", "мазда": "mazda", "хонда": "honda", "митсубиси": "mitsubishi", "рено": "renault",
    "пежо": "peugeot", "ситроен": "citroen", "форд": "ford", "вольво": "volvo", "ауди": "audi",
    "ливан": "livan", "гак": "gac", "фав": "faw", "донгфенг": "dongfeng", "танк": "tank",
}


def _brand_canon(tok: str) -> str:
    """Каноническая форма марки (латиница) для сравнения кир↔лат. Сначала явный алиас, затем транслит."""
    t = (tok or "").strip().lower()
    if not t:
        return ""
    if t in _BRAND_CANON:
        return _BRAND_CANON[t]
    return "".join(_RU2LAT.get(ch, ch) for ch in t)


def _brand_in_name(brand: str, name: str) -> bool:
    """Есть ли марка в имени коллекции — с нормализацией кир↔лат (по канону любого токена имени)."""
    bc = _brand_canon(re.split(r"[\s/]+", (brand or "").strip().lower())[0] if brand else "")
    if not bc:
        return False
    for tok in re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", (name or "").lower()):
        if _brand_canon(tok) == bc:
            return True
    return False


def _known_brand_canons() -> set:
    """Канон-набор РЕАЛЬНЫХ марок авто — выводится из ТОГО ЖЕ классификатора, что делит ct на
    Марки/Модели/Общее (_ct_segment_map). Для ct сегмента «Марки» брендом служит само имя ag_part1,
    для «Модели» — его первое слово. Используется как защита helper'ов: vendor/name-фильтр строится
    ТОЛЬКО для реальной марки, не для темы («Автокредит»→avtokredit, «Trade-in»→trade, «Авито»→avito).
    Кэш на процесс. Пустой набор (классификатор/БД недоступны) → helper'ы деградируют ПЕРМИССИВНО."""
    global _BRAND_CANON_SET
    if _BRAND_CANON_SET is not None:
        return _BRAND_CANON_SET
    seg = _ct_segment_map()
    names = _ag_part1_map()
    brands: set = set()
    for ct, s in seg.items():
        nm = (names.get(_gc_ct(ct)) or names.get(ct) or "").strip().lower()
        parts = nm.split()
        if not parts:
            continue
        if s in ("Марки", "Модели"):
            bc = _brand_canon(parts[0])
            if bc:
                brands.add(bc)
    _BRAND_CANON_SET = brands
    return brands


def _is_brand_canon(tok_canon: str) -> bool:
    """Является ли канон токена реальной маркой авто (по _known_brand_canons). При пустом справочнике
    (классификатор недоступен) — пермиссивно True, чтобы не ломать создание настоящих марок."""
    if not tok_canon:
        return False
    known = _known_brand_canons()
    return (not known) or (tok_canon in known)


def _vendor_value(brand: str) -> str | None:
    """Значение vendor-фильтра ТОВАРОВ (ShoppingAd): марка (manufacturer) В РЕГИСТРЕ ФИДА —
    <vendor>Belgee</vendor>/<vendor>Lada</vendor> (HAR42: применённый фильтр = ["Belgee"] с заглавной,
    НЕ ["belgee"]). Латиница марки кодера отдаётся как есть (Belgee/GAC/Lada), кириллица → транслит
    Title-case (Лада→Lada). Защита: только реальная марка (тот же справочник Марки/Модели/Общее);
    тема («Автокредит»/«Trade-in»/«Авито») → None."""
    b = (brand or "").strip()
    if not b:
        return None
    first_raw = re.split(r"[\s/]+", b)[0]
    canon = _brand_canon(first_raw.lower())
    if not canon or not _is_brand_canon(canon):
        return None
    # vendor должен совпадать с фидовым <vendor>: латиница кодера уже в нужном регистре (Belgee, GAC),
    # отдаём как есть; кириллица — латинизируем с заглавной (Лада→Lada).
    return first_raw if re.search(r"[A-Za-z]", first_raw) else canon.title()


def _vendor_filter_values(vendor: str) -> list[str]:
    """Значения для vendor-фильтра товаров (CONTAINS_ANY) во ВСЕХ регистрах фида. Регистр <vendor>
    зависит от фида (HAR42/43: один фид «Belgee», другой «baic»), а CONTAINS_ANY чувствителен к
    регистру → шлём [исходный, нижний, Title], чтобы совпасть с любым написанием. Единый источник:
    тот же набор используется и в add_shopping_ads (создание), и в set_default_text (перезапись)."""
    v = str(vendor or "").strip()
    return list(dict.fromkeys([v, v.lower(), v.title()])) if v else []


def _model_field_values(brand: str, seg: str) -> list[str]:
    """Значения для model-фильтра ТОВАРОВ (Модели-группы): хвост имени БЕЗ марки во всех регистрах
    фида (BAIC X35 → X35; Lada Vesta Седан → Vesta Седан). HAR: <vendor>Lada</vendor><model>Vesta Седан</model>.
    [] если не «Модели» / нет хвоста / не реальная марка. Фид без поля model → add_shopping_ads ретраит без него."""
    if seg != "Модели":
        return []
    b = (brand or "").strip()
    if not b or not _coder_name_real_brand(b):
        return []
    parts = re.split(r"\s+", b, maxsplit=1)
    if len(parts) < 2:
        return []
    tail = parts[1].strip()
    return list(dict.fromkeys([tail, tail.lower(), tail.title()])) if tail else []


def _listing_name_value(brand: str, seg: str) -> str | None:
    """Значение name-фильтра «Страницы каталога» (ListingAd, HAR36): марка (Марки) или марка+модель
    (Модели), нижний регистр латиницей. Имена страниц каталога содержат «Belgee X50 …» → CONTAINS_ANY.
    Защита: первый токен ОБЯЗАН быть реальной маркой (тот же справочник). Тема → None."""
    b = (brand or "").strip()
    if not b:
        return None
    first = _brand_canon(re.split(r"[\s/]+", b.lower())[0])
    if not first or not _is_brand_canon(first):
        return None
    if seg == "Марки":
        return first
    toks = [_brand_canon(t) for t in re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", b.lower())]
    return " ".join(t for t in toks if t) or None


def _brand_level_collection_id(brand_name: str, collections: list[dict]) -> str | None:
    """Бренд-уровневая коллекция фида для марки («Новые автомобили BAIC …», id='25').
    Берём коллекцию с id НЕ 'model_*', в имени которой есть слово марки (кир↔лат нормализация);
    при нескольких — с наибольшим offerCount (полнее). None — не нашли (caller → model_* фолбэк)."""
    if not brand_name or not collections:
        return None
    if not re.split(r"[\s/]+", str(brand_name or "").strip().lower())[0]:
        return None
    cands = []
    for c in collections:
        cid = str(c.get("id") or "")
        if not cid or cid.startswith("model_"):
            continue
        if _brand_in_name(brand_name, c.get("name") or ""):
            cands.append(c)
    if not cands:
        return None
    cands.sort(key=lambda c: -(int(c.get("offers") or 0)))
    return str(cands[0]["id"])


def _feed_models_from_collections(collections: list[dict]) -> dict:
    """Из коллекций фида → {name_lower: id} ТОЛЬКО для модельных (id 'model_*') — совместимо с
    _match_collection / _brand_collection_ids (фолбэк, когда _account_model_feeds фид не нашёл)."""
    out = {}
    for c in (collections or []):
        cid = str(c.get("id") or "")
        nm = (c.get("name") or "").strip().lower()
        if cid.startswith("model_") and nm:
            out[nm] = cid
    return out


def _minus_marks_enabled() -> list[str]:
    """Включённые глобальные минус-марки (через инъекцию blueprint._enabled_minus_marks).
    Fail-safe: пустой список, если configure ещё не отработал или БД недоступна."""
    fn = globals().get("_enabled_minus_marks")
    if not callable(fn):
        return []
    try:
        return [str(m).strip() for m in (fn() or []) if str(m).strip()]
    except Exception:  # noqa: BLE001 — минус-марки best-effort, не валят создание
        return []


def _minus_marks_grid_conditions(brand_field: str = "vendor") -> list[dict]:
    """Grid feedFilter-условия «Марка НЕ содержит <марка>» для КАЖДОЙ включённой минус-марки.
    brand_field: разрешённое имя поля для этого фида ('vendor', 'mark_id', и т.п.) — берётся из
    _resolve_feed_field(login, feed_id, 'brand'). Default 'vendor' для обратной совместимости.
    Одно условие на марку, AND → offer исключается если марка совпала с любой.
    Добавляется К brand/model-conditions товарки, НЕ заменяет их."""
    return [{"field": brand_field, "operator": "NOT_CONTAINS_ALL",
             "stringValue": json.dumps([m], ensure_ascii=False)} for m in _minus_marks_enabled()]


def _minus_marks_uac_conditions(brand_field: str = "vendor") -> list[dict]:
    """UAC feed_filters-условия (формат с value, не stringValue) для минус-марок tp7:
    «Производитель НЕ содержит <марка>». Добавляется К conditions товарного фильтра мастера.
    brand_field: разрешённое имя поля для фида ('vendor', 'mark_id', и т.п.) — берётся из
    _resolve_feed_field(login, feed_id, 'brand'). Default 'vendor' для обратной совместимости."""
    return [{"field": brand_field, "operator": "NOT_CONTAINS",
             "value": json.dumps([m], ensure_ascii=False)} for m in _minus_marks_enabled()]


# ── Динамическое разрешение полей фида (Bug A/B fix 2026-07-02) ──────────────────
# Разные форматы фида используют разные имена полей в Grid feedFilter:
#   YANDEX_MARKET (YML): vendor, model, categoryId, name, description
#   AUTO_RU (yandex.xml авто): mark_id, folder_id, year, body_type, name, description
#   Target: brand, model, name, description
# Проверяем fieldsForUseAs из Grid Feeds-запроса (кэш 10 мин per (login, feed_id)).

_BRAND_FIELD_SYNONYMS: tuple = ("vendor", "mark_id", "brand", "make")
_MODEL_FIELD_SYNONYMS: tuple = ("model", "folder_id", "modification")
_FEED_FIELDS_CACHE: dict = {}   # (login, feed_id) → (frozenset[str], ts)
_FEED_FIELDS_TTL = 600          # 10 мин

_FEED_FIELDS_QUERY = ("query FeedsFields($login:String!$lo:GdLimitOffsetInput$f:GdFeedsFilterInput)"
                      "{reqId:getReqId client(searchBy:{login:$login}){feeds(limitOffset:$lo filter:$f)"
                      "{rowset{fieldsForUseAs}}}}")


def _feed_filter_fields(login: str, feed_id: int) -> frozenset:
    """Доступные имена полей feedFilter для фида через Grid fieldsForUseAs (кэш 10 мин).
    AUTO_RU → {mark_id, folder_id, ...}; YANDEX_MARKET → {vendor, model, ...}.
    При сбое запроса возвращает frozenset() — caller делает фолбэк на первый синоним."""
    key = (str(login), int(feed_id))
    cached = _FEED_FIELDS_CACHE.get(key)
    if cached:
        flds, ts = cached
        if time.time() - ts < _FEED_FIELDS_TTL:
            return flds
    flds: frozenset = frozenset()
    try:
        _gf = globals().get("gf")
        if _gf is None:
            from . import grid_finalize as _gf  # noqa: F811
        _gcl = _gf.GridClient(login)
        _gcl._bootstrap_csrf()   # CSRF обязателен; без него первый _post → 403 → пустой rowset
        _r = _gcl._post("FeedsFields", _FEED_FIELDS_QUERY,
                        {"login": login, "lo": {"limit": 1, "offset": 0},
                         "f": {"searchBy": str(feed_id)}})
        _rows = ((((_r.json().get("data") or {}).get("client") or {}).get("feeds") or {})
                 .get("rowset") or [])
        flds = frozenset((_rows[0] if _rows else {}).get("fieldsForUseAs") or [])
    except Exception:  # noqa: BLE001 — best-effort, фолбэк на primary синоним
        pass
    _FEED_FIELDS_CACHE[key] = (flds, time.time())
    return flds


def _resolve_feed_field(login: str, feed_id: int, semantic: str) -> str | None:
    """Разрешает имя поля feedFilter для семантики ('brand' или 'model') по fieldsForUseAs фида.
    Возвращает первый подходящий синоним или None (фолбэк: без поля → только collectionId или предупреждение).
    При пустом fieldsForUseAs (сбой запроса) возвращает первый синоним (vendor/model) — UNKNOWN_FIELD retry
    в add_shopping_ads/set_default_text отработает как страховка."""
    synonyms = _BRAND_FIELD_SYNONYMS if semantic == "brand" else _MODEL_FIELD_SYNONYMS
    available = _feed_filter_fields(login, int(feed_id))
    if not available:                  # probe failed → assume primary, let UNKNOWN_FIELD retry handle
        return synonyms[0]
    for s in synonyms:
        if s in available:
            return s
    return None                        # ни один синоним не в fieldsForUseAs → no field-filter + warning


def _tp7_product_feed_filters(brand_model: str, ct: str,
                              *, login: str = "", feed_id: int = 0) -> list[dict]:
    """UAC feed_filters для товарной части tp7.

    Для product-объявлений у мастера фильтр задаётся отдельно от listings_feed_filters.
    Используем канонические варианты модели:
      - полное имя из ct/ag_part1;
      - хвост без бренда (например, 'Tiggo 8 Pro Max');
    чтобы товарка не шла по всему фиду.
    login + feed_id: если переданы — резолвим реальные имена полей через _resolve_feed_field
    (AUTO_RU: mark_id/folder_id вместо vendor/model). Без них — фолбэк на vendor/model.
    """
    # Резолвим brand/model поля для UAC-условий по фиду (Bug A: AUTO_RU = mark_id/folder_id).
    if login and feed_id:
        _bf = _resolve_feed_field(login, int(feed_id), "brand") or "vendor"
        _mf = _resolve_feed_field(login, int(feed_id), "model") or "model"
    else:
        _bf, _mf = "vendor", "model"
    # Глобальные минус-марки применяются ДАЖЕ к товарке по всему фиду (ct0000/общий ст) —
    # это и есть «во всех кампаниях с фидами не показывать эти марки».
    minus = _minus_marks_uac_conditions(brand_field=_bf)
    base = (brand_model or "").strip()
    real_brand = bool(base) and bool(ct) and ct not in ("ct0000", "ct0111") and _coder_name_real_brand(base)
    conditions: list[dict] = []
    if real_brand:
        parts = [p for p in re.split(r"\s+", base) if p]
        variants: list[str] = []
        for cand in (base, " ".join(parts[1:]) if len(parts) > 1 else ""):
            cand = re.sub(r"\s+", " ", str(cand or "").strip())
            if cand and len(cand) >= 3 and cand not in variants:
                variants.append(cand)
        if variants:
            conditions.append({"field": _mf, "operator": "CONTAINS",
                               "value": json.dumps(variants, ensure_ascii=False)})
    conditions.extend(minus)                 # минус-марки К бренд-условию (AND) или самостоятельно
    if not conditions:
        return []
    return [{"conditions": conditions}]

"""Create-set tp3/tp5 and cookie builders extracted from blueprint.py."""

from __future__ import annotations

from .text_norm import _trim_clean

import json

_DEPS: dict = {}


def configure(deps: dict) -> None:
    """Bind blueprint dependencies used by feed/cookie builders."""
    _DEPS.clear()
    _DEPS.update(deps)
    globals().update(deps)


# Детерминированный резерв сайтлинков (#7): когда БД-слепок пуст И LLM вернул пусто/упал —
# всё равно прикрепляем осмысленный авто-кредитный набор, чтобы объявления НЕ оставались без
# быстрых ссылок. LLM (_ai_common_sitelinks) флапает (у одного аккаунта 8 ссылок, у другого 0),
# из-за чего #7 был недетерминирован — фиксированный фолбэк убирает эту недетерминированность.
# frag — уникальный якорь: Grid AddSitelinkSets требует href на каждую ссылку и не любит дубли.
_DEFAULT_SITELINKS_FALLBACK = [
    {"title": "Автокредит от 9 000 ₽/мес", "description": "Одобрение за 15 минут онлайн", "frag": "#credit"},
    {"title": "Первый взнос 0 ₽", "description": "Кредит без первоначального взноса", "frag": "#no-first-pay"},
    {"title": "Кредит от 15 банков", "description": "Подберём лучшую ставку под вас", "frag": "#banks"},
    {"title": "Трейд-ин с выгодой", "description": "Обмен вашего авто на новое", "frag": "#trade-in"},
]


def _sitelinks_fallback_with_href(href: str) -> list:
    """Статический резерв с проставленным href (Grid требует href на каждую ссылку). Без href —
    вернём как есть (add_sitelink_set просто пропустит: не хуже прежнего поведения)."""
    base = (href or "").strip()
    out = []
    for s in _DEFAULT_SITELINKS_FALLBACK:
        item = {"title": s["title"], "description": s["description"]}
        if base:
            item["href"] = base + s["frag"]
        out.append(item)
    return out


def _ensure_sitelink_hrefs(items: list, base_href: str) -> list:
    """Гарантировать href на каждой быстрой ссылке (Grid AddSitelinkSets требует href, иначе
    отбрасывает ссылку). БД/LLM часто дают только title+description (href=None) → backfill из
    base_href с уникальным #якорем по индексу (Grid не любит дубли href)."""
    base = (base_href or "").strip()
    out = []
    for i, s in enumerate(items or []):
        if not isinstance(s, dict) or not (s.get("title") or s.get("Title")):
            continue
        cur = (s.get("href") or s.get("Href") or "").strip()
        if not cur:
            if not base:
                continue   # нет ни href у ссылки, ни базового — пропускаем (add_sitelink_set её всё равно отбросит)
            s = {**s, "href": base + f"#sl{i + 1}"}   # всегда уникальный якорь (Grid не любит дубли href)
        out.append(s)
    return out


def _common_sitelinks_fast(login, slepok, site_type, city, tp_code, href=""):
    """Сайтлинки БЕЗ баллов и БЕЗ зависания LLM (#7): сначала БД-библиотека слепка
    (`_slepok_content_get`, мгновенно), потом AI-генерация, и наконец детерминированный
    статический резерв — чтобы быстрые ссылки прикреплялись ВСЕГДА (не зависели от флапающего LLM).
    href backfill на всех источниках (БД/LLM часто без href → Grid отбрасывал ссылки → #7 не чинился).
    Возвращает list[dict{title,href,description}] или [] (пусто → вызывающий сам решает: v5-ассеты
    аккаунта на creation-пути ИЛИ детерминированный резерв в fix_sitelinks_missing). НЕ подставляет
    статический резерв сам — иначе затенял бы реальные v5-сайтлинки (`_assets.get('sitelinks')`)."""
    try:
        from .ai_content import _slepok_content_get
        _db = _slepok_content_get(slepok, site_type, "campaign")
        _sl = (_db or {}).get("sitelinks") if isinstance(_db, dict) else None
        if _sl:
            picked = _ensure_sitelink_hrefs(
                [s for s in _sl if isinstance(s, dict) and s.get("title")][:8], href)
            if picked:
                return picked
    except Exception:  # noqa: BLE001
        pass
    try:
        ai = _ensure_sitelink_hrefs(_ai_common_sitelinks(login, slepok, site_type, city, tp_code) or [], href)
        if ai:
            return ai
    except Exception:  # noqa: BLE001
        pass
    return []


def _create_text_via_cookie(
    login: str, name: str, tp_code: str, counter_id: int, goal_id: int, cpa_rub: int,
    budget_rub: int, region_ids: list, href: str, slepok: str, site_type: str, r_code: str,
    titles: list | None, texts: list, pay: str = "cpa", city: str = "", autotarget: bool = False,
    corr: dict | None = None, ret_map: dict | None = None,
    token: str = "", callout_texts: list | None = None,
    callout_ids: list | None = None,
    precreated_promo_id: int | None = None,
) -> dict:
    """tp2/tp4 (Поиск / Поиск+Динамика) ПО КУКЕ (без баллов) — после согласия через попап (152).
    Кампания (search) + группы (ключи+минуса) + комбинаторные объявления через grid_create.
    Корректировки «Глобальных правил» — через Grid (HAR21) прямо в AddCampaigns (без баллов).
    БАГ-11 фикс: Grid-финализация инвариантов (#3/#4/#5/#6) + ассеты (sitelinks/callouts/promo)
    через _finalize_rsya (ПОИСК-режим: _PLATFORMS_SEARCH вместо РСЯ-платформ)."""
    import datetime as _dt
    _img_map = _grid_account_image_hashes(login)
    groups, _m3_alive = _pack_groups_with_retry(login, slepok, site_type, r_code, href, titles, texts,
                                                city=city, tp_code=tp_code, image_map=_img_map,
                                                autotarget=bool(autotarget))
    if not groups:
        # Пак пуст после ретраев → defer (отложенная докрутка), НЕ permanent-fail.
        return {"ok": False, "defer": True, "name": name,
                "error": f"{tp_code}(куки): пак M3 пуст/недоступен (M3_alive={_m3_alive}) — отложено на докрутку"}
    start_date = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=3))).strftime("%Y-%m-%d")
    wkl = int(budget_rub) if budget_rub else int(cpa_rub) * 10
    # Корректировки в AddCampaigns (HAR21): campaignId-плейсхолдер 9999999 — Yandex привяжет к реальной.
    _bm = _grid_bid_modifiers(9999999, corr or {}, ret_map or {})
    spec = {"name": name, "counter_id": counter_id or 0, "goal_id": goal_id or 0,
            "cpa": int(cpa_rub), "weekly_budget": wkl, "start_date": start_date,
            "network": False, "search": True, "pay_for_conversion": (pay == "cpa"),
            "bid_modifiers": _bm,
            # #11: tp2/tp4 — только страница поиска (['SEARCH_PAGE']), без «динамических мест на поиске».
            # Динамика tp4 идёт через organic (platforms), не через placementTypes. Ставим на СОЗДАНИИ,
            # чтобы не было окна с placementTypes=None (=дефолт с динамич. местами), если финализация упадёт.
            "placement_types": ["SEARCH_PAGE"]}
    # БАГ-10: цены из фида для tp2/tp4 cookie-пути (раньше price_map не прокидывался).
    try:
        _tp24_price_map = _account_offer_prices(login, href)
    except Exception:  # noqa: BLE001
        _tp24_price_map = {}
    try:
        rep = gc.create_full(login, campaign_spec=spec, groups=groups, region_ids=region_ids,
                             href=href, goal_id=goal_id or 0, autotargeting=bool(autotarget),
                             price_map=_tp24_price_map, brand_price_fn=_group_ad_price)
        cid = rep.get("campaign_id")
        ok = bool(cid) and not (rep.get("errors") and not rep.get("groups"))
        # БАГ-11 фикс: Grid-финализация инвариантов + ассеты для tp2/tp4 куки-пути.
        # БАГ-1 FIX: вызываем ВСЕГДА при ok+cid, не только при goal_id.
        # Использует _finalize_search_via_grid (поисковые платформы, не РСЯ).
        # Сбой финализации не блокирует результат — группы/объявления уже созданы.
        _fin = None
        if ok and cid:
            try:
                _assets = {"callout_ids": [], "promos": [], "sitelinks": []}
                _slset = None
                _prefer_callout_ids = [int(x) for x in (callout_ids or []) if str(x or "").strip().isdigit()]
                if token:
                    try:
                        _assets = _tp5_account_data(token, login, slepok, site_type,
                                                    prefer_callout_texts=callout_texts or [],
                                                    prefer_callout_ids=_prefer_callout_ids)
                    except Exception:  # noqa: BLE001
                        pass
                # #10 КУКИ-ФОЛБЭК: если v5 не дал уточнений (пусто/после 152) — берём id уточнений
                # аккаунта через Grid Callouts (БЕЗ баллов), сопоставив по тексту слепка (HAR40).
                if _prefer_callout_ids:
                    _assets["callout_ids"] = _prefer_callout_ids[:8]
                elif not _assets.get("callout_ids"):
                    _gco = _grid_callout_ids(login, callout_texts or [])
                    if _gco:
                        _assets["callout_ids"] = _gco
                _ai_sitelinks = _common_sitelinks_fast(login, slepok, site_type, city, tp_code, href=href)
                # Sitelinks: Grid-первичный (БЕЗ баллов) — HAR23/entry262 AddSitelinkSets.
                _asl = _norm_sitelinks_for_v501(_ai_sitelinks or (_assets.get("sitelinks") or []), href)
                if _asl:
                    try:
                        _slset = gf.GridClient(login).add_sitelink_set(_asl)
                    except Exception:  # noqa: BLE001
                        _slset = _get_or_reuse_sitelink_set(token, login, _asl)  # v5 fallback
                # HAR-24/entry183: UpdateCampaigns должен получать реальный campaignId внутри
                # bidModifiers (не placeholder 9999999 из AddCampaigns). Перестраиваем с cid.
                _bm_fin = _grid_bid_modifiers(cid, corr or {}, ret_map or {})
                # #14 КУКИ-ФОЛБЭК: минус-набор «Минуса общие» через Grid (libraryMinusKeywordsIds, БЕЗ
                # баллов) для слепков с режимом shared_set (scherbakova). v5-привязка ниже остаётся как
                # дополнение при наличии баллов. Grid-пак ищем по имени (HAR40 MinusPhraseLibrary).
                _minus_ids = []
                if _SLEPOK_MINUS_MODE.get(slepok) == "shared_set":
                    _mp = _grid_minus_pack_id(login)
                    if _mp:
                        _minus_ids = [_mp]
                _finalize_search_via_grid(
                    login, cid, name=name, goal_id=goal_id or 0, cpa_rub=cpa_rub, weekly_rub=wkl,
                    counter_ids=[counter_id] if counter_id else [],
                    pay_for_conversion=(pay == "cpa"),
                    callout_ids=_assets.get("callout_ids"),
                    sitelink_set_id=_slset,
                    promo_id=(_assets["promos"][0] if _assets.get("promos") else precreated_promo_id),
                    minus_set_ids=_minus_ids,
                    bid_modifiers=_bm_fin,
                    platforms=_search_platforms(tp_code))   # места показа: tp2 organic=False / tp4 organic=True
                _fin = {"callouts": len(_assets.get("callout_ids") or []),
                        "sitelink_set": _slset, "promo": bool(_assets.get("promos") or precreated_promo_id),
                        "minus_set_grid": _minus_ids,
                        "corrections": len((_bm_fin.get("bidModifierRetargeting") or {}).get("adjustments") or [])}
                if token:
                    _v5_mods, _v5_mod_err = _apply_corrections(token, login, cid, corr or {}, ret_map or {})
                    _fin["v5_corrections"] = _v5_mods
                    if _v5_mod_err:
                        _fin["v5_corrections_error"] = _v5_mod_err[:160]
                # demographic (age/gender) через Grid (bidModifierDemographics).
                _fin["demographic_corrections"] = len((_bm_fin.get("bidModifierDemographics") or {}).get("adjustments") or [])
            except Exception as _fe:  # noqa: BLE001
                _fin = {"error": str(_fe)[:160]}
            _ad_ids = rep.get("ad_ids") or []
            if _ad_ids:
                try:
                    _upd_items = []
                    # ad_ids 1:1 с groups (None = объявление не создано) — ревью 03.07 #5/#21
                    for _aid, _grp in zip(_ad_ids, groups):
                        if not _aid:
                            continue
                        # tp2/tp4 — ПОИСК: картинки НЕ грузим (их там быть не должно, решение Семёна).
                        # Обновляем только цену (adPrice показывается и на Поиске) + кнопку (отд. апдейтом).
                        _upd = {"id": _aid, "href": _grp.get("href") or href,
                                "titles": _grp.get("titles") or [],
                                "bodies": _grp.get("texts") or []}
                        _cur, _old = _group_ad_price(
                            _tp24_price_map, _grp.get("brand") or _grp.get("name") or "",
                            _grp.get("seg") or _ct_segment(_grp.get("ct") or "")
                        )
                        _ad_price = _grid_ad_price_payload(_cur, _old)
                        if _ad_price:
                            _upd["adPrice"] = _ad_price
                        _upd_items.append(_upd)
                    _repaired = _grid_update_adaptive_ads(login, _upd_items,
                                                           campaign_ids=[cid] if cid else None)
                    if _fin is None or not isinstance(_fin, dict):
                        _fin = {}
                    _fin["ads_repaired"] = _repaired
                    _fin["image_groups"] = sum(1 for _it in _upd_items if _it.get("image_hashes"))
                except Exception as _fe2:  # noqa: BLE001
                    if _fin is None or not isinstance(_fin, dict):
                        _fin = {}
                    _fin["repair_error"] = str(_fe2)[:160]
        return {"ok": ok, "name": name, "campaign_id": cid, "launched": False, "via": "cookie",
                "search_finalized": _fin,
                "build": {"groups": rep.get("groups"), "ads": rep.get("ads"),
                          "keywords": rep.get("keywords", 0),
                          "errors": rep.get("errors", [])[:5]},
                "url": (f"https://direct.yandex.ru/dna/campaign/{cid}?ulogin={login}" if cid else ""),
                "error": ("; ".join(rep.get("errors") or [])[:240] if not ok else None)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "name": name, "error": f"{tp_code}(куки): {str(e)[:200]}"}

def _create_shopping_via_cookie(
    login: str, name: str, tp_code: str, counter_id: int, goal_id: int, cpa_rub: int,
    budget_rub: int, region_ids: list, href: str, agency: str = "",
    body_text: str = "", feed_id: int = 0,
    corr: dict | None = None, ret_map: dict | None = None,
    token: str = "", slepok: str = "", site_type: str = "", city: str = "",
    callout_texts: list | None = None, feed_name: str = "",
    callout_ids: list | None = None,
    ct: str = "ct0000", r_code: str = "",
    single_feed: bool = False,
) -> dict:
    """tp3 (Товарная галерея РСЯ) / tp5 (Поиск + Товарная галерея) ПО КУКЕ (без баллов) — после
    согласия через попап (152). Кампания (gallery+organic) + группа (автотаргет) + товарное
    объявление по фиду (grid_create.create_shopping_full, реверс HAR17). → res-форма.
    tp3 → РСЯ-канал (network), tp5 → Поиск (search). Фид обязателен (читаем по куке).
    БАГ-12 фикс: после создания — Grid-finalize с callouts/sitelinks/инвариантами (раньше отсутствовал)."""
    import datetime as _dt
    fid = int(feed_id) if feed_id else 0
    feed_name = (feed_name or "").strip()
    if not fid:
        try:
            _rows = [f for f in _filter_allowed_feed_rows(_grid_feeds(login, agency)) if f.get("id")]
            # single_feed → предпочесть /yandex.xml (как API-путь tp5/tp3), НЕ первый попавшийся
            # фид: баг porg-psm5h7q6 — при feed_id=0 cookie-путь брал zabronirovat вместо yandex.
            if single_feed and _rows:
                from .create_set_input import prefer_single_feed_rows
                _rows = prefer_single_feed_rows(_rows)
            _first = _rows[0] if _rows else None
            fid = int(_first["id"]) if _first else 0
            feed_name = feed_name or ((_first or {}).get("name") or "")
        except Exception:  # noqa: BLE001
            fid = 0
    elif not feed_name and agency:
        try:
            _rows = _filter_allowed_feed_rows(_grid_feeds(login, agency))
            _match = next((f for f in _rows if int(f.get("id") or 0) == fid), None)
            feed_name = ((_match or {}).get("name") or "").strip()
        except Exception:  # noqa: BLE001
            feed_name = ""
    if not fid:
        return {"ok": False, "name": name, "error": f"{tp_code}(куки): нет URL-фида на аккаунте — товарную галерею не создать"}
    if tp_code == "tp5" and feed_name and feed_name not in name and not _is_site_domain_name(feed_name, href):
        name = f"{name} — {feed_name}"
    is_rsya = (tp_code == "tp3")
    start_date = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=3))).strftime("%Y-%m-%d")
    wkl = int(budget_rub) if budget_rub else int(cpa_rub) * 10
    _bm = _grid_bid_modifiers(9999999, corr or {}, ret_map or {})  # корректировки в AddCampaigns (HAR21)
    spec = {"name": name, "counter_id": counter_id or 0, "goal_id": goal_id or 0,
            "cpa": int(cpa_rub), "weekly_budget": wkl, "start_date": start_date,
            "network": is_rsya, "search": (not is_rsya), "organic": (not is_rsya),
            "pay_for_conversion": False, "bid_modifiers": _bm,
            # Места показа при СОЗДАНИИ НЕ форсируем (create=null — эталон HAR20 tp5-create).
            # Их выставляет finalize: _finalize_search_via_grid(placement_types=PLACEMENTS_TP5),
            # HAR49-эталон 712024652 (known-good). Форс ['SEARCH_PAGE','ADV_GALLERY'] в AddCampaigns
            # не подтверждён и рискует ORGANIC_PLACEMENT_TYPES_INVALID_COMBINATION → падение ВСЕГО
            # create (code-review C). Прежний create-guard закрывал лишь микро-окно «только Поиск»
            # и был добавлен под ложную тревогу UI-кэша (live был уже корректен). tp3 (РСЯ) — тоже null.
            "placement_types": None}
    try:
        # #5: имя группы tp5/tp3 по кодеру (как tp1/tp2): {ct}_aon_n000_{r_code}_..._g00 — Товарная
        # галерея. Без r_code (нет контекста) — прежнее «Товарная галерея».
        _grp_name = _text_group_name(ct, r_code, "Товарная галерея") if r_code else "Товарная галерея"
        rep = gc.create_shopping_full(login, campaign_spec=spec, group_names=[_grp_name],
                                      feed_id=fid, region_ids=region_ids, href=href,
                                      body_text=_trim_clean(body_text or "", 81), goal_id=goal_id or 0)
        cid = rep.get("campaign_id")
        ok = bool(cid) and not (rep.get("errors") and not rep.get("groups"))
        # БАГ-8 фикс: ListingAd «Страницы каталога» — by-shopping, без name-фильтра (Общее, автотаргет).
        # create_shopping_full создаёт ShoppingAd но не ListingAd; докрутка через Grid (без баллов).
        # Сбой не блокирует — ShoppingAd уже создан; warnings идут в rep["errors"].
        # Guard: ТОЛЬКО не-РСЯ (tp5 Поиск+Динамика+ТГ). tp3 = РСЯ товарная галерея — ListingAd
        # (страницы каталога, поиск/динамика) там НЕ нужен, иначе лишние объявления в РСЯ.
        _sh_ids = rep.get("shopping_ad_ids") or []
        if ok and cid and _sh_ids and not is_rsya:
            try:
                from .create_set_tp1_builders import _grid_add_listings_with_name_filters
                _lst_build: dict = {"listing_build_items": [], "listing_name_by_shop": {}}
                _grid_add_listings_with_name_filters(
                    gf.GridClient(login), _sh_ids, _lst_build, fid, _trim_clean(body_text or "", 81))
                rep["listing_ads"] = _lst_build.get("listing_ads", 0)
            except Exception as _le8:  # noqa: BLE001
                rep.setdefault("errors", []).append(f"листинги(куки): {str(_le8)[:120]}")
        # БАГ-12 фикс: Grid-finalize — callouts/sitelinks/инварианты на уровне кампании.
        # Раньше отсутствовал полностью для tp3/tp5 куки-пути → кампании без ассетов и без инвариантов.
        # Сбой финализации не блокирует результат — товарная галерея уже создана.
        _fin = None
        if ok and cid:
            try:
                _sh_assets = {"callout_ids": [], "promos": [], "sitelinks": []}
                _sh_slset = None
                _prefer_callout_ids = [int(x) for x in (callout_ids or []) if str(x or "").strip().isdigit()]
                if token:
                    try:
                        _sh_assets = _tp5_account_data(token, login, slepok, site_type,
                                                       prefer_callout_texts=callout_texts or [],
                                                       prefer_callout_ids=_prefer_callout_ids)
                    except Exception:  # noqa: BLE001
                        pass
                if _prefer_callout_ids:
                    _sh_assets["callout_ids"] = _prefer_callout_ids[:8]
                # Sitelinks: если v5 ничего не дал (152/нет токена) — локальный фолбэк как в tp1/tp2-пути.
                if not _sh_assets.get("sitelinks"):
                    _ai_sl = _common_sitelinks_fast(login, slepok, site_type, city, tp_code, href=href)
                    if _ai_sl:
                        _sh_assets["sitelinks"] = _ai_sl
                # Sitelinks: Grid-первичный (БЕЗ баллов) — HAR23/entry262 AddSitelinkSets.
                _sh_asl = _norm_sitelinks_for_v501(_sh_assets.get("sitelinks") or [], href)
                if _sh_asl:
                    try:
                        _sh_slset = gf.GridClient(login).add_sitelink_set(_sh_asl)
                    except Exception:  # noqa: BLE001
                        _sh_slset = _get_or_reuse_sitelink_set(token, login, _sh_asl)
                # HAR-24/entry183: UpdateCampaigns должен получать реальный campaignId внутри
                # bidModifiers (не placeholder 9999999 из AddCampaigns). Перестраиваем с cid.
                _bm_fin = _grid_bid_modifiers(cid, corr or {}, ret_map or {})
                if is_rsya:
                    # tp3 — РСЯ-канал: _finalize_rsya (network-only, placementTypes=[] хардкодом
                    # внутри — параметра placement_types у него НЕТ, передавать его = TypeError → ловилось
                    # except'ом и tp3-куки оставалась БЕЗ финализации (callouts/sitelinks/промо/корр.)).
                    _mp_disabled = _enabled_minus_places()   # #21 минус-площадки РСЯ (tp3 куки-путь)
                    _finalize_rsya(
                        login, cid, name=name, goal_id=goal_id or 0, cpa_rub=cpa_rub, weekly_rub=wkl,
                        counter_ids=[counter_id] if counter_id else [],
                        pay_for_conversion=False,
                        callout_ids=_sh_assets.get("callout_ids"),
                        sitelink_set_id=_sh_slset,
                        promo_id=(_sh_assets["promos"][0] if _sh_assets.get("promos") else None),
                        minus_set_ids=None, bid_modifiers=_bm_fin,
                        disabled_places=_mp_disabled)
                else:
                    # tp5 «Поиск + Товарная галерея»: места показа SEARCH_PAGE + ADV_GALLERY (HAR20),
                    # platforms по умолчанию = PLATFORMS_SEARCH (gallery=True — товарная галерея НА поиске).
                    _finalize_search_via_grid(
                        login, cid, name=name, goal_id=goal_id or 0, cpa_rub=cpa_rub, weekly_rub=wkl,
                        counter_ids=[counter_id] if counter_id else [],
                        pay_for_conversion=False,
                        callout_ids=_sh_assets.get("callout_ids"),
                        sitelink_set_id=_sh_slset,
                        promo_id=(_sh_assets["promos"][0] if _sh_assets.get("promos") else None),
                        minus_set_ids=None, bid_modifiers=_bm_fin,
                        # tp5 «Ручная настройка + ТГ» = ЯВНЫЙ список ["SEARCH_PAGE","ADV_GALLERY"] (HAR49
                        # эталон 712024652). null давал пресет «Поиск» (Grid откатывает к дефолту, ADV_GALLERY
                        # не входит в пресет). Динамика = isOrganicSearchEnabled=True (platforms.organic). (C review)
                        placement_types=list(gf.PLACEMENTS_TP5))
                _fin = {"callouts": len(_sh_assets.get("callout_ids") or []),
                        "sitelink_set": _sh_slset, "promo": bool(_sh_assets.get("promos")),
                        "corrections": len((_bm_fin.get("bidModifierRetargeting") or {}).get("adjustments") or [])}
                if token:
                    _v5_mods, _v5_mod_err = _apply_corrections(token, login, cid, corr or {}, ret_map or {})
                    _fin["v5_corrections"] = _v5_mods
                    if _v5_mod_err:
                        _fin["v5_corrections_error"] = _v5_mod_err[:160]
            except Exception as _fe:  # noqa: BLE001
                _fin = {"error": str(_fe)[:160]}
        out = {"ok": ok, "name": name, "campaign_id": cid, "launched": False, "via": "cookie",
               "shopping_finalized": _fin,
               "build": {"groups": rep.get("groups"), "ads": rep.get("ads"), "feed_id": fid,
                         "errors": rep.get("errors", [])[:5]},
               "url": (f"https://direct.yandex.ru/dna/campaign/{cid}?ulogin={login}" if cid else ""),
               "error": ("; ".join(rep.get("errors") or [])[:240] if not ok else None)}
        return out
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "name": name, "error": f"{tp_code}(куки): {str(e)[:200]}"}

def _tp5_account_data(token: str, login: str, slepok: str, site_type: str, agency: str = "",
                      prefer_callout_texts: list | None = None,
                      prefer_callout_ids: list | None = None) -> dict:
    """Однократно собрать данные tp5: фиды, промо, минус-набор, уточнения, sitelinks, дефолт-текст.
    Фиды: v5 (баллы), при пустом (часто 152) — фолбэк на список по КУКЕ (Grid, без баллов).
    prefer_callout_texts — ВЫБРАННЫЕ пользователем уточнения (из попапа набора): создаём/находим их
    ID и вешаем именно их (inheritableCallouts кампании). Пусто → берём уточнения аккаунта (как было)."""
    cl = cmc.DirectV501Client(token, login)
    # Bug D (доводка, ревью 03.07 #2): URL фида в v5 живёт в UrlFeed.Url — верхнеуровневого Url
    # у Feed НЕТ, без UrlFeedFieldNames кортеж всегда получал '' и имена кампаний брали короткое
    # имя кабинета, а single_feed не мог матчиться по url.
    jf = _v5_get("feeds", token, login, ["Id", "Name", "SourceType"],
                 extra={"UrlFeedFieldNames": ["Url"]})
    allowed = _allowed_feed_keys()
    feeds = [(f["Id"], f.get("Name") or "", ((f.get("UrlFeed") or {}).get("Url") or ""))
             for f in (jf.get("result") or {}).get("Feeds", [])
             if f.get("SourceType") == "URL" and allowed and _feed_row_allowed(f, allowed)]
    if not feeds and agency:                              # v5 пусто/152 → фиды по куке (без баллов)
        feeds = [(int(f["id"]), f.get("name") or "", f.get("url") or "") for f in _filter_allowed_feed_rows(_grid_feeds(login, agency)) if f.get("id")]
    jp = _v5_get("promotions", token, login, ["Id"])
    promos = [p["Id"] for p in (jp.get("result") or {}).get("Promotions", [])]
    jm = _v5_get("negativekeywordsharedsets", token, login, ["Id", "Name"])
    msets = [(s["Id"], s.get("Name") or "") for s in (jm.get("result") or {}).get("NegativeKeywordSharedSets", [])]
    minus_set = next((mid for mid, nm in msets if "Минуса общие" in nm), (msets[0][0] if msets else None))
    sitelinks, default_text = [], ""
    try:
        conn = _victory_conn()
        cur = conn.cursor()
        cur.execute("SELECT content FROM public.direct_slepok_content "
                    "WHERE slepok=%s AND site_type=%s AND kind='campaign'", (slepok, site_type))
        row = cur.fetchone()
        conn.close()
        if row:
            c = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            sitelinks = (c.get("sitelinks") or [])[:8]
            default_text = next((t for t in (c.get("texts") or []) if len(t) <= 81), "")
    except Exception:  # noqa: BLE001
        pass
    # БАГ-Б фикс: если kind='campaign' не содержит sitelinks — пробуем отдельную kind='sitelinks'.
    # Это тот же фолбэк что и на v5-пути (_build_tp1_from_pack: _slepok_sitelinks_for).
    if not sitelinks:
        sitelinks = _slepok_sitelinks_for(slepok, site_type)[:8]
    _prefer_callout_ids = [int(x) for x in (prefer_callout_ids or []) if str(x or "").strip().isdigit()]
    callout_ids = _prefer_callout_ids[:8]
    try:
        if callout_ids:
            pass
        elif prefer_callout_texts:                        # выбранные пользователем → создаём/находим их ID
            callout_ids = list(_ensure_callout_exts(token, login, prefer_callout_texts).values())[:8]
            if not callout_ids:
                # _ensure_callout_exts упал (152?) → пробуем Grid (без баллов)
                try:
                    _clean = [(str(t) or "").strip()[:25] for t in prefer_callout_texts if t]
                    _clean = [t for t in _clean if t]
                    if _clean:
                        _gc_co = gf.GridClient(login)
                        callout_ids = list(_gc_co.add_callouts(_clean).values())[:8]
                except Exception:  # noqa: BLE001
                    pass
        if not callout_ids:                               # ничего не выбрано / не создалось → уточнения аккаунта
            callout_ids = _dedup_callout_ids(cl.get_callouts())  # #24: normalize+dedup
        if not callout_ids:
            # v5 get_callouts пусто (новый аккаунт / 152 на get) → Grid (без баллов)
            try:
                _gc_co = gf.GridClient(login)
                callout_ids = _dedup_callout_ids(_gc_co.get_callouts())  # #24: normalize+dedup
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return {"cl": cl, "feeds": feeds, "promos": promos, "minus_set": minus_set,
            "sitelinks": sitelinks, "default_text": default_text, "callout_ids": callout_ids}

def _create_tp5_single(data: dict, token: str, login: str, name: str, pay: str,
                       goal_id: int, cpa_rub: int, budget_rub: int,
                       counter_id: int, region_ids: list, href: str, feed_id: int,
                       feed_name: str, slepok: str, site_type: str, r_code: str,
                       corr: dict, ret_map: dict,
                       feed_models: dict | None = None,
                       titles: list | None = None,
                       city: str = "", segment: str | None = None,
                       autotarget: bool = False, products_only: bool = False,
                       grid_cookie: str | None = None) -> dict:
    """Одна боевая tp5 (комбинированная, как эталон Щербаковой 2026-06-22):
    TEXT_CAMPAIGN (поиск-only) + бренд-группы из пака M3 (TextAd + ListingAd + ShoppingAd).

    pay='tcpa' → AVERAGE_CPA (cpc-вариант, кодер tp5_cpc_site)
    pay='cpa'  → PAY_FOR_CONVERSION (cpa-вариант, кодер tp5_cpa_site)

    Каждая группа = ct-папка пака M3 (tp5) → кодер ct{N}_aon_n000_{r}_ct010_ag011_g00.
    FeedFilterConditions по collectionId если feed_models передан; иначе по всему фиду.
    Grid-докрутка: места показа (gallery + search), ассеты кампании, минус, инварианты.
    Корректировки «Глобальных правил» — ПОСЛЕ Grid (он перезаписывает bidModifiers).
    """
    # ── 1. TEXT_CAMPAIGN через _create_search_test_campaign ─────────────────────
    res = _create_search_test_campaign(
        token, login, name, audiences=[],
        counter_id=counter_id, mode="search", pay=pay,
        goal_id=goal_id, cpa_rub=cpa_rub, budget_rub=budget_rub)
    if not res.get("ok"):
        return {"ok": False, "name": name, "feed": feed_name, "error": res.get("error", "campaigns.add упал")}
    cid = res["campaign_id"]

    # ── 2. Наполнение: бренд-группы из пака M3 с TextAd + ListingAd + ShoppingAd ──
    # _build_tp1_from_pack → _build_tp1_adgroups: with_shopping=True даёт «Т+Л+ТОВ» в каждой группе.
    # Кодер группы: _tp1_group_name(ct, r_code, brand, with_shopping=True)
    #   → ct{N}_aon_n000_{r}_ct010_ag011_g00 — {Бренд}  (CODER.md §tp5 2026-06-22)
    texts = [data.get("default_text") or "Официальный дилер. Тест-драйв и выгодные условия по кредиту. Авто в наличии."]
    tp5_build: dict = {}
    try:
        tp5_build = _build_tp1_from_pack(
            token, login, cid, slepok, site_type, region_ids,
            href, r_code, titles, texts, counter_id=counter_id,
            feed_id=feed_id, with_shopping=bool(feed_id),
            feed_models=feed_models, city=city,
            segment=segment, autotarget=autotarget, products_only=products_only,
            tp_code="tp5")
    except Exception as e:  # noqa: BLE001
        tp5_build = {"error": str(e)[:240]}

    # Защита от пустышек: кампания создана, но сборка не дошла (нет групп) → удаляем недоделанную.
    if tp5_build.get("error") or tp5_build.get("skipped") or not tp5_build.get("adgroups"):
        _delete_partial_campaign(token, login, cid)
        _fail = {"ok": False, "name": name, "feed": feed_name, "campaign_id": cid, "partial_deleted": True,
                 "error": "tp5 не дозаполнена: " + str(
                     tp5_build.get("error") or tp5_build.get("skipped") or "группы не созданы")[:200]}
        if tp5_build.get("defer"):
            _fail["defer"] = True   # пустой пак M3 (временный сбой) → докрутка, не permanent-fail
        return _fail

    # ── 3. Текст по умолчанию для ShoppingAd ────────────────────────────────────
    _default_text = data.get("default_text") or "Официальный дилер. Тест-драйв и выгодные условия по кредиту. Авто в наличии."
    _shop_ids = tp5_build.get("shopping_ad_ids") or []
    if feed_id and not _shop_ids:
        _delete_partial_campaign(token, login, cid)
        return {"ok": False, "name": name, "feed": feed_name, "campaign_id": cid, "partial_deleted": True,
                "error": "tp5 не дозаполнена: фидовая кампания создана без ShoppingAd"}
    if _shop_ids and feed_id:
        _gcl = gf.GridClient(login, cookie=grid_cookie)
        # Текст и листинги в РАЗДЕЛЬНЫХ try (как tp1, «G review»): падение текста (Яндекс 500)
        # раньше выкидывало из общего try и листинги вообще не создавались → «без ListingAd».
        try:
            _gcl.set_default_text(
                _shop_ids, feed_id, _default_text,
                filters_by_ad_id=(tp5_build.get("shopping_filters") or {}),
            )
            tp5_build["shopping_text_set"] = len(_shop_ids)
        except Exception as _e:  # noqa: BLE001
            tp5_build.setdefault("warnings", []).append(f"shopping text: {str(_e)[:120]}")
        # Листинги — общий Grid-путь tp1/tp5 (by-shopping, без баллов, + name-фильтры).
        # v501-путь удалён: listing_build_items несут name_value (HAR36), а не collection_ids —
        # v501 молча возвращал [] и кампания удалялась («создана без ListingAd», ❌5 03.07.2026).
        # Хелпер самодостаточен (только аргументы) — прямой импорт из configure-модуля безопасен.
        from .create_set_tp1_builders import _grid_add_listings_with_name_filters
        _grid_add_listings_with_name_filters(_gcl, _shop_ids, tp5_build, feed_id, _default_text)
    if feed_id and _shop_ids and not int(tp5_build.get("listing_ads") or 0):
        # НЕ удаляем кампанию (принцип «дозаполнять, не удалять»): TextAd+ShoppingAd уже есть,
        # листинги добьются ретраем; причина — в warnings item'а (раньше терялась при удалении).
        tp5_build.setdefault("warnings", []).append(
            "листинги каталога: 0 ListingAd (by-shopping) — кампания оставлена, добить ретраем")

    # ── 4. Grid-докрутка: места показа (gallery + search), ассеты кампании, минус, инварианты ──
    _assets = _resolve_campaign_assets(
        token, login, href,
        sitelinks=(_common_sitelinks_fast(login, slepok, site_type, city, "tp5", href=href)
                   or _sitelinks_fallback_with_href(href)),
        assets=data, slepok=slepok, site_type=site_type,
        grid_cookie=grid_cookie,
    )
    slset_grid = _assets.get("sitelink_set_id")
    grid_warn: str | None = None  # B1: Grid-сбой не блокирует, но должен быть виден в ответе
    try:
        gridc = gf.GridClient(login)
        gridc.finalize(
            cid, name=name, goal_id=goal_id, cpa_rub=cpa_rub, weekly_rub=budget_rub,
            counter_ids=[counter_id] if counter_id else [],
            pay_for_conversion=(pay == "cpa"),
            callout_ids=_assets["callout_ids"], sitelink_set_id=slset_grid,
            promo_id=(_assets["promos"][0] if _assets["promos"] else None),
            minus_set_ids=[_assets["minus_set"]] if _assets["minus_set"] else None,
            placement_types=list(gf.PLACEMENTS_TP5))
    except Exception as _grid_exc:  # noqa: BLE001
        # Grid-докрутка не блокирует создание, но сбой ДОЛЖЕН быть виден:
        # при упавшем Grid кампания останется без товарной галереи (placementTypes не выставлен)
        # и без ассетов (callouts/sitelinks/promo). ENABLE_COMPANY_INFO=NO в v5 Settings уже
        # защищает от Карт/организации. Требуется ретрай Grid вручную.
        grid_warn = f"Grid-докрутка не прошла (товарная галерея/ассеты НЕ выставлены): {str(_grid_exc)[:200]}"

    # ── 5. Корректировки «Глобальных правил» — ПОСЛЕ Grid ───────────────────────
    nmod = 0
    try:
        v501cl = cmc.DirectV501Client(token, login)
        nmod = gf.apply_corrections(v501cl, cid, corr.get("demographic", []),
                                    corr.get("audiences", []), ret_map)
    except Exception:  # noqa: BLE001
        pass
    out = {"ok": True, "campaign_id": cid, "id": cid, "name": name, "feed": feed_name,
           "tp5_build": tp5_build, "modifiers_set": nmod,
           "url": f"https://direct.yandex.ru/dna/campaign/{cid}?ulogin={login}"}
    if grid_warn:
        out["grid_warn"] = grid_warn  # B1: Grid-сбой виден в ответе; товарная галерея требует ретрая
    return out

def _create_tp5_campaign(token: str, login: str, base_name: str, counter_id: int,
                         goal_id: int, cpa_rub: int, budget_rub: int, region_ids: list,
                         href: str, slepok: str, site_type: str, r_code: str,
                         corr: dict, ret_map: dict, job=None,
                         titles: list | None = None,
                         agency: str = "", city: str = "",
                         segment: str | None = None, autotarget: bool = False,
                         products_only: bool = False, no_cpa: bool = False,
                         single_feed: bool = False,
                         grid_cookie: str | None = None) -> dict:
    """Боевая tp5 (комбинированная, эталон Щербаковой 2026-06-22): TEXT_CAMPAIGN поиск-only
    + бренд-группы из пака M3 (TextAd + ListingAd + ShoppingAd), кодер ct010_ag011.
    FAN-OUT: мультиплицируется по ВСЕМ URL-фидам аккаунта — каждый фид своя пара cpc+cpa.
    single_feed=True → только /yandex.xml (fallback: первый фид).
    agency — для _account_model_feeds (collectionId по модели из listings фида).
    base_name — канон cpc: 'tp5_cpc_site — Поиск + Динамика + Товарная галерея'."""
    data = _tp5_account_data(token, login, slepok, site_type, agency)
    if not data["feeds"]:
        return {"ok": False, "name": base_name, "error": "нет URL-фидов на аккаунте для tp5"}
    if single_feed:
        from .create_set_input import prefer_single_feed_variants
        data["feeds"] = prefer_single_feed_variants(data["feeds"])
    # Модельные коллекции фидов (listings 'model_N') — для FeedFilterConditions по модели.
    mf_list = _account_model_feeds(login, agency) if agency else []
    results = []
    for feed_id, feed_name, feed_url in data["feeds"]:        # FAN-OUT: каждый фид → своя пара
        if job and job.get("cancel"):                        # отмена: стоп ПЕРЕД следующим фидом
            break
        # Bug D fix: используем URL фида (без https://) вместо короткого имени из кабинета.
        import re as _re_fn
        _f_label = (_re_fn.sub(r'^https?://', '', feed_url) if feed_url else feed_name)
        nm_cpc = (f"{base_name} — {_f_label}" if not _is_site_domain_name(feed_name, href)
                  else base_name)
        nm_cpa = nm_cpc.replace("tp5_cpc_site", "tp5_cpa_site", 1)
        fm_entry = next((f for f in mf_list if int(f["id"]) == int(feed_id)), None)
        feed_models = fm_entry["models"] if fm_entry else None
        _pairs = [(nm_cpc, "tcpa")] if no_cpa else [(nm_cpc, "tcpa"), (nm_cpa, "cpa")]
        for nm, pay in _pairs:
            if job and job.get("cancel"):                    # отмена: стоп ПЕРЕД следующей кампанией пары
                break
            try:
                results.append(_create_tp5_single(
                    data, token, login, nm, pay, goal_id, cpa_rub, budget_rub,
                    counter_id, region_ids, href, feed_id, feed_name,
                    slepok, site_type, r_code, corr, ret_map,
                    feed_models=feed_models, titles=titles, city=city,
                    segment=segment, autotarget=autotarget, products_only=products_only,
                    grid_cookie=grid_cookie))
                _bump_job(job, True)                         # live: +1 кампания
            except Exception as e:  # noqa: BLE001
                results.append({"ok": False, "name": nm, "error": str(e)[:240]})
                _add_job_err(job, str(e)[:240])
                _bump_job(job, False)
            if job:
                _job_db_progress(job)
    ok = any(r.get("ok") for r in results)
    first_id = next((r["campaign_id"] for r in results if r.get("ok")), None)
    return {"ok": ok, "name": base_name, "campaign_id": first_id, "id": first_id,
            "launched": False, "campaigns": results,
            "url": next((r.get("url") for r in results if r.get("ok")), "")}

def _create_tp3_single(data: dict, token: str, login: str, name: str, mode: str,
                       pay_for_conv: bool, goal_id: int, cpa_rub: int, budget_rub: int,
                       counter_id: int, region_ids: list, href: str, feed_id: int,
                       feed_name: str, group_name: str, corr: dict, ret_map: dict) -> dict:
    """Одна боевая tp3 «Товарная галерея» (ЕПК, канал РСЯ, товарная по ВСЕМУ фиду).
    Отличие от tp5: канал network (network_cpa=AVERAGE_CPA / network_payconv=PAY_FOR_CONVERSION) +
    РСЯ-докрутка (_finalize_rsya, чистый network-only). Группа — ShoppingAd+ListingAd по всему фиду
    (без ТГО, без модель-фильтра — товарная галерея целиком). UTM на группе."""
    cl = data["cl"]
    spec = cmc.UnifiedCampaignSpec(
        name=name, client_login=login, oauth_token=token, mode=mode,
        region_ids=region_ids, counter_ids=[counter_id], goal_id=goal_id,
        network_average_cpa=int(cpa_rub) * 1_000_000, search_cpa=int(cpa_rub) * 1_000_000,
        apply_invariants=True)
    cid = cl.create_unified_campaign(spec, launch=False)
    # Защита от пустышек: группа/товарное объявление не создались → удаляем недоделанную кампанию.
    try:
        ag = cl.add_product_adgroup(cid, name=group_name, region_ids=region_ids)
        shop = cl.add_shopping_ad(ag, feed_id=feed_id) if ag else None
    except Exception as _e:  # noqa: BLE001
        ag, shop = None, None
    if not ag or not shop:
        _delete_partial_campaign(token, login, cid)
        return {"ok": False, "name": name, "feed": feed_name, "campaign_id": cid, "partial_deleted": True,
                "error": "tp3 не дозаполнена: группа/товарное объявление не созданы"}
    cl.add_listing_ad(ag, feed_id=feed_id)
    try:
        cl._call("adgroups", "update", {"AdGroups": [{"Id": ag, "TrackingParams": cmc.UTM_TEMPLATE}]})
    except Exception:  # noqa: BLE001
        pass
    slset = None
    if data["sitelinks"]:
        base = href.rstrip("/")
        # Быстрые ссылки ведут ТОЛЬКО на главную страницу (base_href без пути).
        # /sl1../sl8 давали 404 — исправлено: Href = главная для всех ссылок.
        sl = [{"Title": s.get("title", ""), "Description": s.get("description", ""),
               "Href": base} for s in data["sitelinks"]]
        try:
            slset = cl.add_sitelinks_set(sl)
        except Exception:  # noqa: BLE001
            slset = None
    warn = None
    # РСЯ-докрутка: уточнения/промо/ссылки уровня кампании, чистый РСЯ (как tp1)
    _mp_disabled = _enabled_minus_places()               # #21 минус-площадки РСЯ (tp3 v5-путь)
    try:
        _finalize_rsya(
            login, cid, name=name, goal_id=goal_id, cpa_rub=cpa_rub,
            weekly_rub=(budget_rub or int(cpa_rub) * 10),
            counter_ids=[counter_id] if counter_id else [], pay_for_conversion=pay_for_conv,
            callout_ids=data["callout_ids"], sitelink_set_id=slset,
            promo_id=(data["promos"][0] if data["promos"] else None),
            minus_set_ids=[data["minus_set"]] if data["minus_set"] else None,
            disabled_places=_mp_disabled)
    except Exception as e:  # noqa: BLE001
        warn = f"РСЯ-докрутка упала: {str(e)[:140]}"
    # текст по умолчанию на товарном объявлении (как в tp5)
    if data["default_text"]:
        try:
            gf.GridClient(login).set_default_text([shop], feed_id, data["default_text"])
        except Exception:  # noqa: BLE001
            pass
    # корректировки «Глобальных правил» — ПОСЛЕ Grid (он перезаписывает bidModifiers)
    nmod = 0
    try:
        nmod = gf.apply_corrections(cl, cid, corr.get("demographic", []),
                                    corr.get("audiences", []), ret_map)
    except Exception:  # noqa: BLE001
        pass
    res = {"ok": True, "campaign_id": cid, "id": cid, "name": name, "feed": feed_name,
           "modifiers_set": nmod,
           "url": f"https://direct.yandex.ru/dna/campaign/{cid}?ulogin={login}"}
    if warn:
        res.setdefault("warnings", []).append(warn)
    return res

def _create_tp3_campaign(token: str, login: str, base_name: str, counter_id: int,
                         goal_id: int, cpa_rub: int, budget_rub: int, region_ids: list,
                         href: str, slepok: str, site_type: str, r_code: str,
                         corr: dict, ret_map: dict, job=None, no_cpa: bool = False,
                         single_feed: bool = False, agency: str = "") -> dict:
    """Боевая tp3 «Товарная галерея» (ЕПК, РСЯ, товарная по фиду) — ПАРА cpc+cpa.
    FAN-OUT (CODER.md): мультиплицируется по ВСЕМ URL-фидам аккаунта — каждый фид своя пара,
    имя несёт название фида. single_feed=True → только /yandex.xml (fallback: первый фид). job — live-счётчик."""
    data = _tp5_account_data(token, login, slepok, site_type, agency)
    if not data["feeds"]:
        return {"ok": False, "name": base_name, "error": "нет URL-фидов на аккаунте для tp3"}
    if single_feed:
        from .create_set_input import prefer_single_feed_variants
        data["feeds"] = prefer_single_feed_variants(data["feeds"])
    # ct009 = «Товарное/Фид» (CODER.md ag_part5): ShoppingAd+ListingAd по фиду.
    group_name = f"ct0000_aon_n000_{r_code}_ct009_ag001_g00 — Товарная галерея"
    results = []
    for feed_id, feed_name, feed_url in data["feeds"]:        # FAN-OUT: каждый фид → своя пара
        if job and job.get("cancel"):                        # отмена: стоп ПЕРЕД следующим фидом
            break
        # Bug D fix: используем URL фида (без https://) вместо короткого имени из кабинета.
        import re as _re_fn3
        _f_label3 = (_re_fn3.sub(r'^https?://', '', feed_url) if feed_url else feed_name)
        nm_cpc = (f"{base_name} — {_f_label3}" if not _is_site_domain_name(feed_name, href)
                  else base_name)
        nm_cpa = nm_cpc.replace("tp3_cpc_site", "tp3_cpa_site", 1)
        _t3 = ([(nm_cpc, "network_cpa", False)] if no_cpa
               else [(nm_cpc, "network_cpa", False), (nm_cpa, "network_payconv", True)])
        for nm, mode, pay in _t3:
            if job and job.get("cancel"):                    # отмена: стоп ПЕРЕД следующей кампанией пары
                break
            try:
                results.append(_create_tp3_single(
                    data, token, login, nm, mode, pay, goal_id, cpa_rub, budget_rub,
                    counter_id, region_ids, href, feed_id, feed_name, group_name, corr, ret_map))
                _bump_job(job, True)                         # live: +1 кампания
            except Exception as e:  # noqa: BLE001
                results.append({"ok": False, "name": nm, "error": str(e)[:240]})
                _add_job_err(job, str(e)[:240])
                _bump_job(job, False)
            if job:
                _job_db_progress(job)
    ok = any(r.get("ok") for r in results)
    first_id = next((r["campaign_id"] for r in results if r.get("ok")), None)
    return {"ok": ok, "name": base_name, "campaign_id": first_id, "id": first_id,
            "launched": False, "campaigns": results,
            "url": next((r.get("url") for r in results if r.get("ok")), "")}

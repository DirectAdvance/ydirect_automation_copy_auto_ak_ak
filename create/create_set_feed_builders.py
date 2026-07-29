"""Create-set tp3/tp5 and cookie builders extracted from blueprint.py."""

from __future__ import annotations

from ..text_norm import _trim_clean
from .create_set_minus import _MINUS_SET_NAME_MARKER
from ..model_urls import _strip_site_domain_label as _strip_dom_lbl
from . import create_set_context as _csctx  # dedup_name_segments (чистый хелпер, без configure)
from .create_set_feed_result import ensure_shopping_cookie_error, shopping_cookie_success

import json
import re as _re_fb

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
    {"title": "Кредитное решение", "description": "Подберём условия под вашу заявку", "frag": "#banks"},
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
    (`_slepok_content_get`, мгновенно), без AI-генерации на create-stage. Детерминированный
    статический резерв добавляет вызывающий код, когда БД пуста.
    href backfill на всех источниках (БД/LLM часто без href → Grid отбрасывал ссылки → #7 не чинился).
    Возвращает list[dict{title,href,description}] или [] (пусто → вызывающий сам решает: v5-ассеты
    аккаунта на creation-пути ИЛИ детерминированный резерв в fix_sitelinks_missing). НЕ подставляет
    статический резерв сам — иначе затенял бы реальные v5-сайтлинки (`_assets.get('sitelinks')`)."""
    try:
        from ..ai_content import _slepok_content_get
        _db = _slepok_content_get(slepok, site_type, "campaign")
        _sl = (_db or {}).get("sitelinks") if isinstance(_db, dict) else None
        if _sl:
            picked = _ensure_sitelink_hrefs(
                [s for s in _sl if isinstance(s, dict) and s.get("title")][:8], href)
            if picked:
                return picked
    except Exception:  # noqa: BLE001
        pass
    return []


def _create_text_via_cookie(
    login: str, name: str, tp_code: str, counter_id: int, goal_id: int, cpa_rub: int,
    budget_rub: int, region_ids: list, href: str, slepok: str, site_type: str, r_code: str,
    titles: list | None, texts: list, pay: str = "cpa", city: str = "", autotarget: bool = False,
    keep_keywords: bool = False,
    segment: str | None = None,
    only_cts: list[str] | None = None,
    only_gks: set | None = None,
    corr: dict | None = None, ret_map: dict | None = None,
    token: str = "", callout_texts: list | None = None,
    callout_ids: list | None = None,
    precreated_promo_id: int | None = None,
) -> dict:
    """tp2/tp4 (Поиск / Поиск+Динамика) ПО КУКЕ (без баллов) — после согласия через попап (152).
    Кампания (search) + группы (ключи+минуса) + комбинаторные объявления через grid_create.
    Корректировки «Глобальных правил» — через Grid (HAR21) прямо в AddCampaigns (без баллов).
    БАГ-11 фикс: Grid-финализация инвариантов (#3/#4/#5/#6) + ассеты (sitelinks/callouts/promo)
    через _finalize_rsya (ПОИСК-режим: _PLATFORMS_SEARCH вместо РСЯ-платформ).
    segment (Марки/Модели/Общее): без него _pack_groups_with_retry строит ВСЕ ct пака без разреза —
    марки и модели попадали в одну кампанию вперемешку (живой баг 2026-07-06, porg-lzjk6p5m/terehov)."""
    import datetime as _dt
    # Кэш account-map по логину (было: `_grid_account_image_hashes` — чтение ВСЕХ кампаний+объявлений
    # аккаунта на КАЖДОЙ cookie-кампании набора → квадратичный рост по ходу прогона).
    _img_map = _account_image_map(login)
    groups, _m3_alive = _pack_groups_with_retry(login, slepok, site_type, r_code, href, titles, texts,
                                                segment=segment, city=city, tp_code=tp_code,
                                                image_map=_img_map, autotarget=bool(autotarget),
                                                keep_keywords=bool(keep_keywords),
                                                only_cts=only_cts, only_gks=only_gks)
    if not groups:
        # Keyword pack пуст после ретраев. Deferred уместен только при реальной недоступности
        # локального зеркала/M3-источника: если источник жив, но под выбранные only_cts/only_gks нет ключей, это детерминированный
        # content-gap. Повторная "докрутка" создаёт ядовитую очередь 0/N без шанса на успех.
        if not _m3_alive:
            return {"ok": False, "defer": True, "name": name,
                    "error": (f"{tp_code}(куки): keyword pack пуст/недоступен "
                              f"(local/M3 source alive={_m3_alive}) — отложено на докрутку")}
        _cts = ", ".join(list(only_cts or [])[:12])
        _gks = ", ".join(list(only_gks or [])[:12])
        _more_cts = max(0, len(only_cts or []) - 12)
        _more_gks = max(0, len(only_gks or []) - 12)
        if _more_cts:
            _cts += f"; +{_more_cts}"
        if _more_gks:
            _gks += f"; +{_more_gks}"
        _detail = []
        if _cts:
            _detail.append(f"ct=[{_cts}]")
        if _gks:
            _detail.append(f"gk=[{_gks}]")
        _suffix = ("; " + "; ".join(_detail)) if _detail else ""
        return {"ok": False, "name": name,
                "error": (f"{tp_code}(куки): content-gap — локальное зеркало/M3-источник доступны, "
                          f"но нет групп/ключей для {slepok}/{site_type}{_suffix}")}
    start_date = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=3))).strftime("%Y-%m-%d")
    wkl = int(budget_rub) if budget_rub else int(cpa_rub) * 10
    # Корректировки в AddCampaigns (HAR21): campaignId-плейсхолдер 9999999 — Yandex привяжет к реальной.
    _bm = _grid_bid_modifiers(9999999, corr or {}, ret_map or {})
    # Минус-слова кампании для Grid-cookie spec (minusKeywords, campaign-level INLINE, без баллов):
    # глоб. вкладка «Минус-слова» + слепковый пак (_minus_shared) для campaign/shared_set-режимов.
    # #9 остаточный гэп (ЗАКРЫТИЕ): в cookie-пути слепковый минус раньше не долетал — spec нёс только
    # глобальные слова, а расшаренный Grid-набор «Минуса общие» (_grid_minus_pack_id, ниже) слов слепка
    # НЕ содержит. Пак кладём INLINE per-кампания → общий аккаунтный набор НЕ мутируется (др. кампании,
    # делящие тот же набор, не затронуты). Зеркало _apply_campaign_direct_minus (token-путь): group-режим
    # (terehov/karavaev) пропускаем — минусы уже на группах; tp1 (РСЯ) пропускаем — минуса режут охват
    # (в tp2/tp4 tp1 не бывает, гейт для симметрии). Пак недоступен (ssh M3) → деградация к глоб. словам.
    _mk_words = list((_DEPS.get("_enabled_minus_words") or (lambda: []))() or [])
    if tp_code != "tp1" and _SLEPOK_MINUS_MODE.get(slepok, "group") != "group":
        _cpm = _DEPS.get("_collect_pack_minus")
        if callable(_cpm):
            try:
                _pack_minus = _cpm(slepok, site_type, tp_code)
            except Exception:  # noqa: BLE001 — пак M3 недоступен → деградируем к глоб. словам
                _pack_minus = []
            _seen_mk = {w.lower() for w in _mk_words}
            for _w in _pack_minus:
                if _w.lower() not in _seen_mk:
                    _seen_mk.add(_w.lower())
                    _mk_words.append(_w)
            _cap = _DEPS.get("_minus_char_budget")
            if callable(_cap):
                _mk_words = _cap(_mk_words)   # ≤20 000 симв. без пробелов (кампания)
    spec = {"name": name, "counter_id": counter_id or 0, "goal_id": goal_id or 0,
            "cpa": int(cpa_rub), "weekly_budget": wkl, "start_date": start_date,
            "network": False, "search": True, "pay_for_conversion": (pay == "cpa"),
            "bid_modifiers": _bm,
            # #11: tp2/tp4 — только страница поиска (['SEARCH_PAGE']), без «динамических мест на поиске».
            # Динамика tp4 идёт через organic (platforms), не через placementTypes. Ставим на СОЗДАНИИ,
            # чтобы не было окна с placementTypes=None (=дефолт с динамич. местами), если финализация упадёт.
            "placement_types": ["SEARCH_PAGE"],
            # Минус-слова кампании в spec через Grid-cookie (minusKeywords, без баллов) — собраны выше
            # (_mk_words: глоб. слова + слепковый пак для campaign/shared_set). _apply_campaign_direct_minus
            # через v5 downstream НЕ вызывается (Grid-spec уже ставит campaign-level, второй v5 = дубль).
            "minus_keywords": _mk_words}

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
        _errs = rep.get("errors") or []
        _underfilled = (
            not rep.get("groups")
            or not rep.get("ads")
            or (((not autotarget) or keep_keywords) and not rep.get("keywords"))
        )
        ok = bool(cid) and not (_errs or _underfilled)
        if cid and not ok:
            try:
                _delete_partial_campaign(token, login, int(cid))
            except Exception:  # noqa: BLE001
                pass
            _reason = str(
                "; ".join(str(x) for x in _errs)
                or ("группы не созданы" if not rep.get("groups")
                    else "объявления не созданы" if not rep.get("ads")
                    else "ключи не созданы")
            )[:200]
            return {"ok": False, "name": name, "campaign_id": cid, "partial_deleted": True,
                    "defer": True,
                    "build": {"groups": rep.get("groups"), "ads": rep.get("ads"),
                              "keywords": rep.get("keywords", 0),
                              # groups_expected/warnings — расхождение «создано ≠ отправлено»
                              # (grid_create._gate_groups_created). НЕ в errors: там оно = приговор
                              # (удаление кампании ниже), а верификатор ловит его по этим полям.
                              "groups_expected": rep.get("groups_expected"),
                              "warnings": (rep.get("warnings") or [])[:5],
                              "errors": _errs[:5]},
                    "error": f"{tp_code}(куки) не дозаполнена: {_reason}"}
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
                        _slset = gf.get_grid_client(login).add_sitelink_set(_asl)
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
                          # см. комментарий выше: расхождение «создано ≠ отправлено» видно
                          # верификатору (GROUPS_CREATED_LESS_THAN_SENT), но кампанию не сносит.
                          "groups_expected": rep.get("groups_expected"),
                          "warnings": (rep.get("warnings") or [])[:5],
                          "errors": rep.get("errors", [])[:5]},
                "url": (f"https://direct.yandex.ru/dna/campaign/{cid}?ulogin={login}" if cid else ""),
                "error": ("; ".join(rep.get("errors") or [])[:240] if not ok else None)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "name": name, "error": f"{tp_code}(куки): {str(e)[:200]}"}

def _grid_set_search_autotarget(login: str, campaign_id: int) -> int:
    """⚠️ УПРАЗДНЁН (2026-07-09, НЕ ВЫЗЫВАЕТСЯ) — оставлен как справка/анти-паттерн.
    relevanceMatch tp2/tp4 token-пути теперь ставится АТОМАРНО при создании групп
    (_build_tp2_adgroups Фаза 1 → Grid AddUnifiedAdGroups profile=search_tp2). Этот пост-патч через
    groups_for_edit (edit-view) ловил ЛАГ реплики и молча возвращал 0 → WRONG_AUTOTARGET
    (журнал TP5_AUTOTARGET v2/I/J). НЕ переиспользовать edit-view для детекта/патча свежих групп.

    Grid (БЕЗ баллов): поставить relevanceMatch «Целевые запросы» (EXACT_V2_MARK) + «Запросы без
    бренда» (WITHOUT_BRAND) на ВСЕ GdUnifiedAdGroup кампании. Нужно ТОЛЬКО на token-пути tp2/tp4:
    v5 adgroups.add НЕ ставит автотаргет-профиль (в отличие от Grid AddUnifiedAdGroups куки-пути и
    _build_tp1_adgroups(search_tp2) для tp5). Read-modify-write через groups_for_edit +
    build_update_item + update_unified_adgroups (тот же примитив, что и repair_executor). Идемпотентно
    (корректный профиль пропускается) и безопасно (группы с ретаргетингом/bidModifiers не трогаем).
    → кол-во обновлённых групп; сбой не валит создание (best-effort)."""
    try:
        grid = gf.get_grid_client(login)
        groups = grid.groups_for_edit(campaign_id)
    except Exception:  # noqa: BLE001
        return 0
    items = []
    for grp in groups:
        if not grp.get("supported"):
            continue
        if grp.get("retargetings_present") or grp.get("bid_modifiers_present"):
            continue
        rm = grp.get("relevance_match")
        if isinstance(rm, dict) and rm.get("isActive") \
                and {str(x).upper() for x in (rm.get("relevanceMatchCategories") or [])} == {"EXACT_V2_MARK"} \
                and {str(x).upper() for x in (rm.get("autotargetingBrandSettings") or [])} == {"WITHOUT_BRAND"}:
            continue   # уже корректный профиль
        target_rm = {"isActive": True,
                     "id": (rm or {}).get("id") if isinstance(rm, dict) else None,
                     "relevanceMatchCategories": ["EXACT_V2_MARK"],
                     "autotargetingBrandSettings": ["WITHOUT_BRAND"]}
        try:
            items.append(grid.build_update_item(grp, keywords=list(grp.get("keywords") or []),
                                                relevance_match=target_rm))
        except Exception:  # noqa: BLE001
            continue
    if not items:
        return 0
    try:
        return len(grid.update_unified_adgroups(items))
    except Exception:  # noqa: BLE001
        return 0


def _create_text_via_token(
    login: str, name: str, tp_code: str, counter_id: int, goal_id: int, cpa_rub: int,
    budget_rub: int, region_ids: list, href: str, slepok: str, site_type: str, r_code: str,
    titles: list | None, texts: list, pay: str = "cpa", city: str = "", autotarget: bool = False,
    # keep_keywords — тот же режимный флаг, что у cookie-близнеца (_create_text_via_cookie:94).
    # run_create_set_text шлёт ОДИН и тот же kwargs-набор в оба пути (create_set_text.py:53-83,
    # «token_kwargs == cookie_kwargs»), а тело ниже уже прокидывает его в _build_text_from_pack →
    # _build_tp2_adgroups (create_set_text_builders.py:168 `if autotarget and not keep_keywords:
    # continue`). Без параметра в сигнатуре token-путь падал TypeError ещё до shell-кампании.
    keep_keywords: bool = False,
    segment: str | None = None,
    only_cts: list[str] | None = None,
    only_gks: set | None = None,
    corr: dict | None = None, ret_map: dict | None = None,
    token: str = "", callout_texts: list | None = None,
    callout_ids: list | None = None,
    precreated_promo_id: int | None = None,
) -> dict:
    """tp2/tp4 (Поиск / Поиск+Динамика) через ТОКЕН/API v5 (тратит баллы Директа) — путь
    DIRECT_API_FIRST при живых баллах. Shell TEXT_CAMPAIGN (search-only, инварианты #3/#4/#5 +
    ENABLE_COMPANY_INFO=NO) + группы (_build_text_from_pack: v5 adgroups/keywords + v501 ads +
    post-create Grid-репейр картинок/цен) + Grid-докрутка инвариантов, ИДЕНТИЧНАЯ cookie-пути
    (те же примитивы _finalize_search_via_grid / _tp5_account_data / _common_sitelinks_fast /
    _grid_callout_ids / _grid_minus_pack_id / _apply_campaign_direct_minus / _apply_corrections),
    ПЛЮС relevanceMatch EXACT_V2_MARK+WITHOUT_BRAND (v5-группы автотаргет не получают → добиваем Grid).

    Возвращает res-форму (via='token'). При исчерпании баллов (152) / недозаполнении — удаляет
    недоделанную кампанию и ставит defer=True; фолбэк на _create_text_via_cookie делает вызывающий
    (run_create_set_text), набор не валим."""
    from .create_set_units import is_units_exhausted as _is_units
    corr = corr or {}
    ret_map = ret_map or {}
    if not token:
        return {"ok": False, "name": name, "error": f"{tp_code}(token): нет токена"}
    wkl = int(budget_rub) if budget_rub else int(cpa_rub) * 10
    # ── 1. SHELL: TEXT_CAMPAIGN (search-only) через v5 ─────────────────────────────
    # pay='cpa' → PAY_FOR_CONVERSION, 'tcpa' → AVERAGE_CPA (те же стратегии, что cookie-путь).
    _build_text_from_pack = _DEPS.get("_build_text_from_pack")
    if not callable(_build_text_from_pack):
        return {"ok": False, "name": name, "error": f"{tp_code}(token): нет билдера групп"}
    try:
        res = _create_search_test_campaign(
            token, login, name, audiences=[], counter_id=counter_id,
            mode="search", pay=("cpa" if pay == "cpa" else "tcpa"),
            goal_id=goal_id or 0, cpa_rub=int(cpa_rub), budget_rub=wkl)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "name": name, "defer": bool(_is_units(str(e))),
                "error": f"{tp_code}(token): shell {str(e)[:200]}"}
    if not res.get("ok") or not res.get("campaign_id"):
        _err = res.get("error") or "campaigns.add упал"
        return {"ok": False, "name": name, "defer": bool(_is_units(_err)),
                "error": f"{tp_code}(token): {str(_err)[:200]}"}
    cid = res["campaign_id"]
    # ── 2. ГРУППЫ (v5 adgroups/keywords + v501 ads, тратит баллы) ──────────────────
    # apply_group_minus: group-режим → минусы на группах; campaign/shared_set → на кампании (ниже).
    _mm = _SLEPOK_MINUS_MODE.get(slepok, "group")
    _apply_group_minus = (_mm == "group")
    try:
        build = _build_text_from_pack(token, login, cid, slepok, site_type, tp_code,
                                      region_ids, href, titles, texts, r_code=r_code,
                                      segment=segment, city=city, autotarget=bool(autotarget),
                                      keep_keywords=bool(keep_keywords),
                                      apply_group_minus=_apply_group_minus, only_cts=only_cts,
                                      only_gks=only_gks,
                                      # shell TEXT_CAMPAIGN создан шагом 1 выше → групп в нём 0
                                      campaign_is_new=True,
                                      # ТОТ ЖЕ mode, которым создан shell (mode="search"):
                                      # tp2/tp4 — Search-канал → аудитории в searchRetargetings
                                      campaign_mode="search")
    except Exception as e:  # noqa: BLE001
        build = {"error": str(e)[:240]}
    _errs = build.get("errors") or []
    _units_hit = _is_units(build.get("error")) or any(_is_units(x) for x in _errs)
    # Недозаполнение (нет групп / пак пуст / 152) → удаляем недоделанную РК + defer (фолбэк на куку).
    # `not build.get("ads")` — группы БЕЗ объявлений это тоже недозаполнение: кампания с группами и
    # ключами, но без единого объявления не показывается, а ok:True скрыл бы дефект (шли пустышки).
    # Безопасность (проверено AST по боевым файлам): гейт живёт ТОЛЬКО на token-пути tp2/tp4
    # (orchestrator `_TEXT_ENGINE` = search_test/search_dynamic), а `_build_text_from_pack` здесь
    # вызывается БЕЗ feed_id/with_shopping (дефолты 0/False) → блок товарных объявлений
    # (create_set_text_builders.py:231 `if feed_id and with_shopping`) не выполняется и ключей
    # listing_ads/shopping_ads в build нет. Кампании «только listing/shopping без TextAd» на этом
    # пути не существует, поэтому пустой `ads` здесь = дефект, а не легальный товарный состав.
    # (В tp1/tp5, где with_shopping=True, товарные аддитивны ПОСЛЕ TextAd и идут другим билдером —
    # этого гейта там нет.) `ads` в rep инициализируется всегда (rep = {... "ads": 0 ...}).
    if build.get("error") or build.get("skipped") or _errs or not build.get("adgroups") or not build.get("ads"):
        try:
            _delete_partial_campaign(token, login, cid)
        except Exception:  # noqa: BLE001
            pass
        _reason = str(build.get("error") or build.get("skipped")
                      or "; ".join(str(x) for x in _errs) or "группы не созданы")[:200]
        return {"ok": False, "name": name, "campaign_id": cid, "partial_deleted": True,
                "defer": bool(build.get("defer") or build.get("skipped") or _units_hit),
                "error": f"{tp_code}(token) не дозаполнена: {_reason}"}
    # ── 3. relevanceMatch EXACT_V2_MARK + WITHOUT_BRAND — ставится АТОМАРНО при СОЗДАНИИ групп ──
    # _build_text_from_pack → _build_tp2_adgroups Фаза 1 создаёт группы через Grid
    # AddUnifiedAdGroups(profile=search_tp2). Пост-патч _grid_set_search_autotarget (groups_for_edit +
    # update_unified_adgroups) УПРАЗДНЁН: он ловил лаг реплики edit-view и молча возвращал 0 →
    # кампания отдавалась ok:True БЕЗ корректного автотаргета → WRONG_AUTOTARGET (журнал
    # TP5_AUTOTARGET v2 «не помогло», I/J). relevance_match_set == adgroups (атомарно); при сбое
    # Grid-групп build вернёт 0 adgroups → выше кампания удаляется + defer (ok:True невозможен).
    _rm_set = int(build.get("relevance_match_set") or 0)
    # ── 4. Grid-докрутка УРОВНЯ КАМПАНИИ: ассеты + места показа + organic + инварианты #3/#4/#5/#6 ──
    # Тот же контур, что в _create_text_via_cookie (строки asset-gathering + _finalize_search_via_grid).
    _fin = None
    try:
        _assets = {"callout_ids": [], "promos": [], "sitelinks": []}
        _slset = None
        _prefer_callout_ids = [int(x) for x in (callout_ids or []) if str(x or "").strip().isdigit()]
        try:
            _assets = _tp5_account_data(token, login, slepok, site_type,
                                        prefer_callout_texts=callout_texts or [],
                                        prefer_callout_ids=_prefer_callout_ids)
        except Exception:  # noqa: BLE001
            pass
        if _prefer_callout_ids:
            _assets["callout_ids"] = _prefer_callout_ids[:8]
        elif not _assets.get("callout_ids"):
            _gco = _grid_callout_ids(login, callout_texts or [])
            if _gco:
                _assets["callout_ids"] = _gco
        _ai_sitelinks = _common_sitelinks_fast(login, slepok, site_type, city, tp_code, href=href)
        _asl = _norm_sitelinks_for_v501(_ai_sitelinks or (_assets.get("sitelinks") or []), href)
        if _asl:
            try:
                _slset = gf.get_grid_client(login).add_sitelink_set(_asl)
            except Exception:  # noqa: BLE001
                _slset = _get_or_reuse_sitelink_set(token, login, _asl)
        _bm_fin = _grid_bid_modifiers(cid, corr, ret_map)
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
            platforms=_search_platforms(tp_code))   # tp2 organic=False / tp4 organic=True
        _fin = {"callouts": len(_assets.get("callout_ids") or []),
                "sitelink_set": _slset, "promo": bool(_assets.get("promos") or precreated_promo_id),
                "minus_set_grid": _minus_ids, "relevance_match_set": _rm_set,
                "corrections": len((_bm_fin.get("bidModifierRetargeting") or {}).get("adjustments") or [])}
        _fin["demographic_corrections"] = len((_bm_fin.get("bidModifierDemographics") or {}).get("adjustments") or [])
    except Exception as _fe:  # noqa: BLE001
        _fin = {"error": str(_fe)[:160]}
    # ── 4b. v5 _apply_corrections — независимо от Grid-finalize (defense-in-depth):
    #       Grid UpdateCampaigns может упасть по схеме/сети; v5-корректировки логически независимы. ──
    try:
        _v5_mods, _v5_mod_err = _apply_corrections(token, login, cid, corr, ret_map)
        if isinstance(_fin, dict):
            _fin["v5_corrections"] = _v5_mods
            if _v5_mod_err:
                _fin["v5_corrections_error"] = _v5_mod_err[:160]
    except Exception as _ce:  # noqa: BLE001
        if isinstance(_fin, dict):
            _fin.setdefault("warnings", []).append(f"v5 corrections: {str(_ce)[:120]}")
    # ── 5. Глобальные минус-слова уровня кампании — ВСЕ режимы (v5 NegativeKeywords = _enabled_minus_words),
    #       аддитивно к shared_set (libraryMinusKeywordsIds). Эквивалент cookie-spec "minus_keywords". ──
    try:
        _cd = _apply_campaign_direct_minus(token, login, cid, slepok, site_type, tp_code, city=city)
        if isinstance(_fin, dict):
            _fin["minus_campaign_note"] = _cd or "campaign-direct OK"
    except Exception as _me:  # noqa: BLE001
        if isinstance(_fin, dict):
            _fin.setdefault("warnings", []).append(f"campaign-direct минусы: {str(_me)[:120]}")
    return {"ok": True, "name": name, "campaign_id": cid, "launched": False, "via": "token",
            "search_finalized": _fin,
            "build": {"groups": build.get("adgroups"), "groups_built": build.get("groups_built"),
                      "ads": build.get("ads"), "keywords": build.get("keywords", 0),
                      "errors": (build.get("errors") or [])[:5]},
            "url": f"https://direct.yandex.ru/dna/campaign/{cid}?ulogin={login}"}

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
    """tp3 (Товарная галерея Поиск, placementTypes=['ADV_GALLERY']) / tp5 (Поиск + Товарная галерея)
    ПО КУКЕ (без баллов) — после согласия через попап (152). Кампания (gallery+organic) + группа
    (автотаргет) + товарное объявление по фиду (grid_create.create_shopping_full, реверс HAR17). → res-форма.
    tp3 и tp5 — Search-канал (search=True, network=False). Фид обязателен (читаем по куке).
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
    # Cookie-путь tp5 брал имя фида из кабинета (`_grid_feeds`) — оно идёт с доменом-ПРЕФИКСОМ,
    # старый гард на ТОЧНОЕ равенство хосту его пропускал. Режем домен-префикс из самой метки
    # (симметрично API-пути tp5/tp3 ниже); пусто на выходе → суффикс не добавляем.
    _feed_lbl = _strip_dom_lbl(feed_name, href)
    if tp_code == "tp5" and _feed_lbl and _feed_lbl not in name:
        # Метка фида клеится ПОСЛЕ `_uniq` (другой модуль) → общий дедуп сегментов надо применить
        # здесь же, иначе живое имя tp5 может нести повтор, которого нет в плановом.
        name = _csctx.dedup_name_segments(f"{name} — {_feed_lbl}")
    is_rsya = False  # tp3 и tp5 — оба Search-канал (tp3 был ошибочно network — исправлено)
    start_date = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=3))).strftime("%Y-%m-%d")
    wkl = int(budget_rub) if budget_rub else int(cpa_rub) * 10
    _bm = _grid_bid_modifiers(9999999, corr or {}, ret_map or {})  # корректировки в AddCampaigns (HAR21)
    spec = {"name": name, "counter_id": counter_id or 0, "goal_id": goal_id or 0,
            "cpa": int(cpa_rub), "weekly_budget": wkl, "start_date": start_date,
            "network": is_rsya, "search": (not is_rsya), "organic": (not is_rsya),
            "pay_for_conversion": False, "bid_modifiers": _bm,
            # Места показа при СОЗДАНИИ НЕ форсируем: tp5 canonical = placementTypes=null +
            # platforms gallery/search/organic. Форс ['SEARCH_PAGE','ADV_GALLERY'] рискует свернуться
            # в UI-пресет «Поиск» или дать ORGANIC_PLACEMENT_TYPES_INVALID_COMBINATION.
            # tp3 тоже создаётся без форса на AddCampaigns; finalize ниже ставит ADV_GALLERY.
            "placement_types": None,
            # tp3/tp5 куки-путь: _apply_campaign_direct_minus downstream не вызывается (в отличие от tp2/tp4),
            # поэтому ставим минусы в spec для ВСЕХ режимов без риска дубля.
            # _enabled_minus_words не прокинута в feed_builder_deps — доступ через _DEPS (safe get).
            "minus_keywords": (_DEPS.get("_enabled_minus_words") or (lambda: []))()}
    try:
        # #5: имя группы tp5/tp3 по кодеру (как tp1/tp2): {ct}_aon_n000_{r_code}_..._g00 — Товарная
        # галерея. Без r_code (нет контекста) — прежнее «Товарная галерея».
        _grp_name = _text_group_name(ct, r_code, "Товарная галерея") if r_code else "Товарная галерея"
        rep = gc.create_shopping_full(login, campaign_spec=spec, group_names=[_grp_name],
                                      feed_id=fid, region_ids=region_ids, href=href,
                                      body_text=_trim_clean(body_text or "", 81), goal_id=goal_id or 0)
        cid = rep.get("campaign_id")
        ok = shopping_cookie_success(rep)
        err_text = None if ok else ensure_shopping_cookie_error(rep)
        # БАГ-8 фикс: ListingAd «Страницы каталога» — by-shopping, без name-фильтра (Общее, автотаргет).
        # create_shopping_full создаёт ShoppingAd но не ListingAd; докрутка через Grid (без баллов).
        # Сбой не блокирует — ShoppingAd уже создан; warnings идут в rep["errors"].
        # Guard: ShoppingAd должен быть создан. ListingAd добавляем для tp3 и tp5 (оба Search-канал).
        _sh_ids = rep.get("shopping_ad_ids") or []
        if ok and cid and _sh_ids and not is_rsya:
            try:
                from .create_set_tp1_builders import _grid_add_listings_with_name_filters
                _lst_build: dict = {"listing_build_items": [], "listing_name_by_shop": {}}
                _grid_add_listings_with_name_filters(
                    gf.get_grid_client(login), _sh_ids, _lst_build, fid, _trim_clean(body_text or "", 81))
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
                        _sh_slset = gf.get_grid_client(login).add_sitelink_set(_sh_asl)
                    except Exception:  # noqa: BLE001
                        _sh_slset = _get_or_reuse_sitelink_set(token, login, _sh_asl)
                # HAR-24/entry183: UpdateCampaigns должен получать реальный campaignId внутри
                # bidModifiers (не placeholder 9999999 из AddCampaigns). Перестраиваем с cid.
                _bm_fin = _grid_bid_modifiers(cid, corr or {}, ret_map or {})
                # Search-финализация: tp3 = ADV_GALLERY; tp5 = placementTypes=null
                # (ручная настройка через platforms gallery+search+organic).
                # isOrganicSearchEnabled=True из platforms.organic (gallery=True в PLATFORMS_SEARCH).
                _tp_placements = (["ADV_GALLERY"] if tp_code == "tp3" else [])
                _finalize_search_via_grid(
                    login, cid, name=name, goal_id=goal_id or 0, cpa_rub=cpa_rub, weekly_rub=wkl,
                    counter_ids=[counter_id] if counter_id else [],
                    pay_for_conversion=False,
                    callout_ids=_sh_assets.get("callout_ids"),
                    sitelink_set_id=_sh_slset,
                    promo_id=(_sh_assets["promos"][0] if _sh_assets.get("promos") else None),
                    minus_set_ids=None, bid_modifiers=_bm_fin,
                    placement_types=_tp_placements)
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
               "build": {"groups": rep.get("groups"), "ads": rep.get("ads"),
                         "shopping_ads": rep.get("ads"), "feed_id": fid,
                         # groups_expected/warnings — расхождение «создано ≠ отправлено»
                         # (grid_create._gate_groups_created в create_shopping_full). Без этих
                         # полей товарка tp3/tp5 теряла гейт: «создано 13 из 14» проходило молча.
                         # НЕ в errors: там оно = приговор кампании (см. текстовый куки-путь).
                         "groups_expected": rep.get("groups_expected"),
                         "warnings": (rep.get("warnings") or [])[:5],
                         "errors": rep.get("errors", [])[:5]},
               "url": (f"https://direct.yandex.ru/dna/campaign/{cid}?ulogin={login}" if cid else ""),
               "error": err_text}
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
    # Библиотечный набор минусов цепляем ТОЛЬКО слепкам режима shared_set (как на пути tp2/tp4,
    # feed_builders:435). Раньше набор резолвился безусловно, и слепку в режиме campaign (pavlov)
    # на tp5/tp3 всё равно прикреплялся чужой «Минуса общие» в обход режима.
    # Слепой фолбэк msets[0][0] убран: без набора с нашим маркером в имени НИЧЕГО не цепляем —
    # иначе на кабинете директолога с собственными наборами прицепился бы ЧУЖОЙ набор.
    minus_set = None
    if _SLEPOK_MINUS_MODE.get(slepok) == "shared_set":
        jm = _v5_get("negativekeywordsharedsets", token, login, ["Id", "Name"])
        msets = [(s["Id"], s.get("Name") or "") for s in (jm.get("result") or {}).get("NegativeKeywordSharedSets", [])]
        minus_set = next((mid for mid, nm in msets if _MINUS_SET_NAME_MARKER in nm), None)
    sitelinks, default_text = [], ""
    try:
        from .. import kontent_pack as _kp_fb  # noqa: PLC0415 — локальный импорт, как в create_set_structure
        _st_norm = _kp_fb.base_site_type(site_type)  # split «Монобренд · Lada» → «Монобренд»
        conn = _victory_conn()
        cur = conn.cursor()
        cur.execute("SELECT content FROM public.direct_slepok_content "
                    "WHERE slepok=%s AND site_type=%s AND kind='campaign'", (slepok, _st_norm))
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
                        _gc_co = gf.get_grid_client(login)
                        callout_ids = list(_gc_co.add_callouts(_clean).values())[:8]
                except Exception:  # noqa: BLE001
                    pass
        if not callout_ids:                               # ничего не выбрано / не создалось → уточнения аккаунта
            callout_ids = _dedup_callout_ids(cl.get_callouts())  # #24: normalize+dedup
        if not callout_ids:
            # v5 get_callouts пусто (новый аккаунт / 152 на get) → Grid (без баллов)
            try:
                _gc_co = gf.get_grid_client(login)
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
                       autotarget: bool = False, keep_keywords: bool = False,
                       products_only: bool = False,
                       grid_cookie: str | None = None,
                       only_gks: set | None = None, only_cts: set | None = None,
                       all_feeds_list: list | None = None) -> dict:
    """Одна боевая tp5 (поиск + товарная галерея, Семён 2026-07-07):
    TEXT_CAMPAIGN (поиск-only) + бренд-группы из пака M3 (ShoppingAd + ListingAd, БЕЗ TextAd).

    pay='tcpa' → AVERAGE_CPA (cpc-вариант, кодер tp5_cpc_site)
    pay='cpa'  → PAY_FOR_CONVERSION (cpa-вариант, кодер tp5_cpa_site)

    Каждая группа = ct-папка пака M3 (tp5) → кодер ct{N}_aon_n000_{r}_ct010_ag011_g00.
    Группа содержит ключи + автотаргет + TextAd + ShoppingAd + ListingAd («Т+Л+ТОВ», как tp1/tp3).
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
    # R2-8 2026-07-10 (дефекты 5+6): единый ShoppingAd default_text БЕЗ кредита, ОДИН общий на все
    # каталожные/товарные кампании — НЕ из slepok_content/фида (был hardcoded «…по кредиту…» + разнобой).
    try:
        from .create_set_assets import SHOPPING_DEFAULT_TEXT as _SDT
    except Exception:  # noqa: BLE001 — fail-safe если импорт недоступен
        _SDT = "Новые автомобили в наличии. Большой выбор. Скидки до 45% при покупке. Тест-драйв."
    texts = [_SDT]
    tp5_build: dict = {}
    try:
        tp5_build = _build_tp1_from_pack(
            token, login, cid, slepok, site_type, region_ids,
            href, r_code, titles, texts, counter_id=counter_id,
            feed_id=feed_id, with_shopping=bool(feed_id),
            feed_models=feed_models, city=city,
            segment=segment, autotarget=autotarget, keep_keywords=keep_keywords,
            products_only=products_only,
            tp_code="tp5", only_gks=only_gks, only_cts=only_cts,
            all_feeds_list=all_feeds_list,
            # кампания создана шагом 1 выше (_create_search_test_campaign) → групп в ней 0
            campaign_is_new=True,
            # ТОТ ЖЕ mode, которым создана кампания шагом 1 (mode="search"): tp5 — Search-канал,
            # значит аудитории структуры едут в searchRetargetings, а не в сетевое retargetings
            campaign_mode="search")
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
    _default_text = _SDT   # R2-8: единый ShoppingAd default_text без кредита (см. выше)
    _shop_ids = tp5_build.get("shopping_ad_ids") or []
    if feed_id and not _shop_ids:
        _delete_partial_campaign(token, login, cid)
        # Причина обычно transient: token→Grid replication lag (add_shopping_ads не увидел
        # свежесозданную кампанию/группы → *_NOT_FOUND даже после ретраев). Это НЕ permanent-fail:
        # удаляем недоделанную РК, но пункт уходит на ДОКРУТКУ (defer, bounded _RESUME_MAX=3) —
        # пересоздастся по куке, когда реплика догонит. Иначе часть tp5 аккаунта молча терялась
        # (2026-07-06 группа C). Причину add_shopping_ads прокидываем в error (раньше терялась).
        _shop_err = "; ".join(str(x) for x in (tp5_build.get("errors") or []))[:200]
        return {"ok": False, "defer": True, "name": name, "feed": feed_name, "campaign_id": cid,
                "partial_deleted": True,
                "error": "tp5 не дозаполнена: фидовая кампания создана без ShoppingAd"
                         + (f" [{_shop_err}]" if _shop_err else "")}
    if _shop_ids and feed_id:
        # A3: cookie-only — ShoppingAd создан Grid'ом по куке, token→Grid lag отсутствует → без пауз.
        _gcl = gf.get_grid_client(login, cookie=grid_cookie, cookie_only=True)
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
        gridc = gf.get_grid_client(login)
        gridc.finalize(
            cid, name=name, goal_id=goal_id, cpa_rub=cpa_rub, weekly_rub=budget_rub,
            counter_ids=[counter_id] if counter_id else [],
            pay_for_conversion=(pay == "cpa"),
            callout_ids=_assets["callout_ids"], sitelink_set_id=slset_grid,
            promo_id=(_assets["promos"][0] if _assets["promos"] else None),
            minus_set_ids=[_assets["minus_set"]] if _assets.get("minus_set") else None,
            placement_types=None)
    except Exception as _grid_exc:  # noqa: BLE001
        # Grid-докрутка не блокирует создание, но сбой ДОЛЖЕН быть виден:
        # при упавшем Grid кампания останется без товарной галереи (placementTypes не выставлен)
        # и без ассетов (callouts/sitelinks/promo). ENABLE_COMPANY_INFO=NO в v5 Settings уже
        # защищает от Карт/организации. Требуется ретрай Grid вручную.
        grid_warn = f"Grid-докрутка не прошла (товарная галерея/ассеты НЕ выставлены): {str(_grid_exc)[:200]}"

    # ── 4.5. relevanceMatch tp5 — упразднён (v3, 2026-07-08) ───────────────────
    # relevanceMatch теперь ставится АТОМАРНО при создании групп: _build_tp1_adgroups
    # использует Grid AddUnifiedAdGroups(autotargeting_profile="search_tp2") вместо v501
    # adgroups.add → EXACT_V2_MARK + WITHOUT_BRAND гарантированы с первого момента.
    # rep["relevance_match_set"] уже заполнен в Фазе 1 _build_tp1_adgroups.
    # (ERRORS_JOURNAL: TP5_AUTOTARGET_ALL_CATEGORIES решение v3)

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

    # ── 6. Глобальные минус-слова уровня кампании — ВСЕ режимы ──────────────────
    # tp2/tp4 закрыты через spec (minusKeywords); tp5 создаётся без spec → добавляем напрямую.
    # shared_set (scherbakova): «Минуса общие 016» содержит корпус слепка, но НЕ содержит слова
    # из direct_global_minus_words («отзывы» и др.) — это разные источники. NegativeKeywords
    # (campaign-level inline) и NegativeKeywordSharedSetIds аддитивны → оба ставятся одновременно.
    # Требование Семёна: global_minus_words ДОЛЖНЫ быть на уровне кампании для ВСЕХ режимов.
    _mm5 = _SLEPOK_MINUS_MODE.get(slepok, "group")  # оставляем для логирования
    if token:
        try:
            _cd5 = _apply_campaign_direct_minus(token, login, cid, slepok, site_type, "tp5", city=city)
            out["minus_campaign_note"] = f"campaign-direct: {_cd5}" if _cd5 else "campaign-direct OK"
        except Exception as _me5:  # noqa: BLE001 — best-effort, не валим кампанию
            out.setdefault("warnings", []).append(f"campaign-direct минусы tp5: {str(_me5)[:120]}")

    return out

def _resolve_single_feed_variants(data: dict, token: str, login: str, agency: str, job=None) -> None:
    """Профильные фиды для tp5/tp3 single_feed: находим ВСЕ профильные фиды аккаунта
    (yandex.xml + yandex-used-auto.xml) через _first_url_feed(strict=True) — fan-out до 2.
    Причина: prefer_single_feed_variants тихо брала variants[:1] (первый разрешённый фид,
    напр. credit-page-01-a.xml) → tp5/tp3 создавались на ЧУЖОМ фиде, вразрез с планом/превью.
    Ни одного профильного → канонический фолбэк (yandex-catalog-model-design-custom-name.xml),
    но ТОЛЬКО при подтверждённом single_feed_fallback (как plan). Не резолвится → данные фид не берём
    (data['feeds']=[]): не создаём товарные галереи на произвольном фиде. Мутирует data['feeds']."""
    from .create_set_input import PROFILE_FEED_KEYS, FALLBACK_SINGLE_FEED_KEY
    # Collect IDs for ALL present profile feeds (strict per-key lookup)
    _profile_ids: list[int] = []
    for _pk in PROFILE_FEED_KEYS:
        _pid = _first_url_feed(token, login, agency, strict=True, url_key=_pk)
        if _pid and _pid not in _profile_ids:
            _profile_ids.append(_pid)
    if _profile_ids:
        # Filter data["feeds"] to profile feed entries (fan-out по профильным фидам)
        sel = [f for f in data["feeds"] if int(f[0]) in _profile_ids]
        # Если профильные не нашлись в data["feeds"] (кастомное именование / новый фид):
        # добавляем их по ID с пустыми name/url — _create_tp5_single подхватит через fid
        data["feeds"] = sel if sel else [(pid, "", "") for pid in _profile_ids]
        return
    # Ни одного профильного → fallback (как plan-гейт): только при подтверждённом фолбэке.
    # UI шлёт feed_confirmed (кнопка «Продолжить с другим фидом»), plan-гейт исторически читал
    # single_feed_fallback → рассинхрон, фолбэк на каталог-фид не открывался. Принимаем ОБА.
    _fb_body = (job or {}).get("body", {})
    if _fb_body.get("single_feed_fallback") or _fb_body.get("feed_confirmed"):
        sf_id = _first_url_feed(token, login, agency, strict=True, url_key=FALLBACK_SINGLE_FEED_KEY)
        if sf_id:
            sel = [f for f in data["feeds"] if int(f[0]) == int(sf_id)]
            data["feeds"] = sel or [(int(sf_id), "", "")]
            return
    data["feeds"] = []


def _create_tp5_campaign(token: str, login: str, base_name: str, counter_id: int,
                         goal_id: int, cpa_rub: int, budget_rub: int, region_ids: list,
                         href: str, slepok: str, site_type: str, r_code: str,
                         corr: dict, ret_map: dict, job=None,
                         titles: list | None = None,
                         agency: str = "", city: str = "",
                         segment: str | None = None, autotarget: bool = False,
                         keep_keywords: bool = False,
                         products_only: bool = False, no_cpa: bool = False,
                         single_feed: bool = False,
                         grid_cookie: str | None = None,
                         only_gks: set | None = None, only_cts: set | None = None,
                         all_feeds: bool = False) -> dict:
    """Боевая tp5 (комбинированная, эталон Щербаковой 2026-06-22): TEXT_CAMPAIGN поиск-only
    + бренд-группы из пака M3 (TextAd + ListingAd + ShoppingAd), кодер ct010_ag011.
    FAN-OUT: мультиплицируется по ВСЕМ URL-фидам аккаунта — каждый фид своя пара cpc+cpa.
    single_feed=True → только /yandex.xml (как plan: _first_url_feed strict; нет → канонический
    фолбэк лишь при подтверждённом single_feed_fallback, иначе tp5 пропускается — НЕ первый фид).
    all_feeds=True (тег «все фиды»): вместо fan-out N кампаний — ОДНА пара cpc+cpa, внутри —
    группа на каждый разрешённый фид (ShoppingAd + ListingAd). Конфликтует с single_feed (single_feed
    имеет приоритет: схлопывает до /yandex.xml, all_feeds не расширяет обратно).
    agency — для _account_model_feeds (collectionId по модели из listings фида).
    base_name — канон cpc: 'tp5_cpc_site — Поиск + Динамика + Товарная галерея'."""
    data = _tp5_account_data(token, login, slepok, site_type, agency)
    if not data["feeds"]:
        return {"ok": False, "name": base_name, "error": "нет URL-фидов на аккаунте для tp5"}
    if single_feed:
        _resolve_single_feed_variants(data, token, login, agency, job)
        if not data["feeds"]:
            return {"ok": False, "name": base_name,
                    "error": "single_feed: целевой фид (/yandex.xml или подтверждённый фолбэк) не найден — tp5 пропущена"}
    # «Все фиды» (тег): ONE кампания-пара, группа на каждый фид → all_feeds_list в _create_tp5_single.
    # single_feed уже схлопнул data["feeds"] до одного — при all_feeds+single_feed = one feed one group.
    if all_feeds and not single_feed:
        _tp5_af_list = list(data["feeds"])   # [(feed_id, feed_name, feed_url), …]
        results = []
        nm_cpc_af = base_name
        nm_cpa_af = nm_cpc_af.replace("tp5_cpc_site", "tp5_cpa_site", 1)
        _pairs_af = [(nm_cpc_af, "tcpa")] if no_cpa else [(nm_cpc_af, "tcpa"), (nm_cpa_af, "cpa")]
        for nm_af, pay_af in _pairs_af:
            if job and job.get("cancel"):
                break
            try:
                results.append(_create_tp5_single(
                    data, token, login, nm_af, pay_af, goal_id, cpa_rub, budget_rub,
                    counter_id, region_ids, href, 0, "",   # feed_id=0, feed_name="" (all-feeds)
                    slepok, site_type, r_code, corr, ret_map,
                    feed_models=None, titles=titles, city=city,
                    segment=segment, autotarget=autotarget, keep_keywords=keep_keywords,
                    products_only=products_only,
                    grid_cookie=grid_cookie, only_gks=only_gks, only_cts=only_cts,
                    all_feeds_list=_tp5_af_list))
                _bump_job(job, True)
            except Exception as e:  # noqa: BLE001
                results.append({"ok": False, "name": nm_af, "error": str(e)[:240]})
                _add_job_err(job, str(e)[:240])
                _bump_job(job, False)
            if job:
                _job_db_progress(job)
        ok = any(r.get("ok") for r in results)
        first_id = next((r["campaign_id"] for r in results if r.get("ok")), None)
        return {"ok": ok, "name": base_name, "campaign_id": first_id, "id": first_id,
                "launched": False, "campaigns": results,
                "url": next((r.get("url") for r in results if r.get("ok")), "")}
    # Модельные коллекции фидов (listings 'model_N') — для FeedFilterConditions по модели.
    mf_list = _account_model_feeds(login, agency) if agency else []
    results = []
    for feed_id, feed_name, feed_url in data["feeds"]:        # FAN-OUT: каждый фид → своя пара
        if job and job.get("cancel"):                        # отмена: стоп ПЕРЕД следующим фидом
            break
        # Bug D fix: используем URL фида (без https://) вместо короткого имени из кабинета.
        # Гард сверяем с ТЕМ, что реально уходит в имя (label), а не с коротким feed_name,
        # и режем домен-ПРЕФИКС, а не только точное равенство хосту.
        _f_label = _strip_dom_lbl(
            (_re_fb.sub(r'^https?://', '', feed_url) if feed_url else feed_name), href)
        # Дедуп сегментов — здесь же: метка фида приклеивается ПОСЛЕ `_uniq` (create_set_plan),
        # поэтому воронка плана её не видит. `nm_cpa` выводится из уже каноничного `nm_cpc`.
        # Структурные tp5 (camp_names/segment) должны называться ровно как в слепке.
        # Суффикс фида нужен только при настоящем fan-out по нескольким фидам, иначе в кабинете
        # имя расходится со структурой («… — yandex.xml» вместо каноничного «КС+Автотаргетинг»).
        # ⚠️ Условие обязано смотреть на ЧИСЛО фидов, а не только на тип кампании. Цикл выше —
        # fan-out по ВСЕМ фидам аккаунта; при структурной tp5 (`segment`/`only_*`) имя оставалось
        # одним на все итерации → кампании-близнецы с ОДИНАКОВЫМ именем, без суффикса версии
        # (`_uniq` их не видит: метка фида приклеивается уже после плана).
        # Живой инцидент 2026-07-28, porg-pl6iavd5: 12 кампаний tp5 на 2 уникальных имени
        # (9 + 3), при плане в 6 позиций и 9 разрешённых фидах аккаунта.
        # Один фид — имя как в слепке (расхождения со структурой нет). Несколько — метка фида
        # обязательна, иначе имена неразличимы (решение Семёна 2026-07-28).
        _multi_feed = len(data["feeds"]) > 1
        _keep_struct_name = (not _multi_feed) and bool(
            single_feed or segment or products_only or only_gks or only_cts)
        nm_cpc = base_name if _keep_struct_name else (_csctx.dedup_name_segments(f"{base_name} — {_f_label}") if _f_label else base_name)
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
                    segment=segment, autotarget=autotarget, keep_keywords=keep_keywords,
                    products_only=products_only,
                    grid_cookie=grid_cookie, only_gks=only_gks, only_cts=only_cts))
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
                       feed_name: str, group_name: str, corr: dict, ret_map: dict,
                       all_feeds_list: list | None = None) -> dict:
    """Одна боевая tp3 «Товарная галерея» (ЕПК, канал Поиск, placementTypes=['ADV_GALLERY']).
    Отличие от tp5: места показа ТОЛЬКО галерея (без SEARCH_PAGE); стратегия
    search_cpa=AVERAGE_CPA / search_payconv=PAY_FOR_CONVERSION (Search=ON, Network=OFF);
    Search-докрутка (_finalize_search_via_grid, placement_types=['ADV_GALLERY']).
    Группа — ShoppingAd+ListingAd по всему фиду (без ТГО, без модель-фильтра). UTM на группе.

    all_feeds_list (тег «все фиды», задача 7): [(feed_id, feed_name), …] — ОДНА кампания, ГРУППА
    на КАЖДЫЙ разрешённый фид (вместо per-feed fan-out кампаний). None → один фид (feed_id)."""
    cl = data["cl"]
    spec = cmc.UnifiedCampaignSpec(
        name=name, client_login=login, oauth_token=token, mode=mode,
        region_ids=region_ids, counter_ids=[counter_id], goal_id=goal_id,
        network_average_cpa=int(cpa_rub) * 1_000_000, search_cpa=int(cpa_rub) * 1_000_000,
        apply_invariants=True)
    cid = cl.create_unified_campaign(spec, launch=False)
    # Группы: по одной на КАЖДЫЙ фид (all_feeds) либо одна на переданный feed_id.
    _feeds_for_groups = ([(int(_f[0]), _f[1]) for _f in all_feeds_list if _f and _f[0]]
                         if all_feeds_list else [(feed_id, feed_name)])
    _shops: list = []                                   # [(shop_id, feed_id)] для set_default_text
    # Phase 1: создаём adgroup для каждого фида (последовательно — нужны Id для phase 2)
    _adgroup_infos: list = []                           # [(ag_id or None, fid, gn)]
    for _i, (_fid, _fnm) in enumerate(_feeds_for_groups):
        # имя группы уникально в кампании (Яндекс требует) — при >1 фиде добавляем метку фида
        _gn = group_name if len(_feeds_for_groups) == 1 else f"{group_name} · {(_fnm or _fid)}"[:255]
        try:
            ag = cl.add_product_adgroup(cid, name=_gn, region_ids=region_ids)
        except Exception:  # noqa: BLE001
            ag = None
        _adgroup_infos.append((ag, _fid, _gn))
    # Phase 2: batch ShoppingAd+ListingAd для всех валидных adgroup — 2×N → 1 вызов ads.add
    # СОХРАНЕНА семантика оригинала: ShoppingAd failure → skip; ListingAd failure → delete campaign.
    _valid_idx = [idx for idx, (ag, _, __) in enumerate(_adgroup_infos) if ag is not None]
    if _valid_idx:
        _feed_groups_batch = [
            {"adgroup_id": _adgroup_infos[idx][0], "feed_id": _adgroup_infos[idx][1]}
            for idx in _valid_idx
        ]
        try:
            _ad_results = cl.add_feed_ads_batch(_feed_groups_batch)
        except Exception as _be:  # noqa: BLE001 — HTTP-level failure всего batch
            try:
                _delete_partial_campaign(token, login, cid)
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "name": name, "feed": feed_name, "campaign_id": cid,
                    "partial_deleted": True, "defer": True,
                    "error": f"tp3 не дозаполнена: batch ads.add упал: {str(_be)[:160]}"}
        for j, idx in enumerate(_valid_idx):
            ag, _fid, _gn = _adgroup_infos[idx]
            shop_id, listing_id, _ad_err = _ad_results[j]
            if shop_id is None:
                # ShoppingAd упал → пропустить группу (как оригинальный continue)
                continue
            if listing_id is None:
                # ShoppingAd succeeded, ListingAd упал → удалить всю кампанию (#ФИКС-8)
                try:
                    _delete_partial_campaign(token, login, cid)
                except Exception:  # noqa: BLE001
                    pass
                return {"ok": False, "name": name, "feed": feed_name, "campaign_id": cid,
                        "partial_deleted": True, "defer": True,
                        "error": f"tp3 не дозаполнена: ListingAd упал: {(_ad_err or 'нет деталей')[:160]}"}
            try:
                cl._call("adgroups", "update", {"AdGroups": [{"Id": ag, "TrackingParams": cmc.UTM_TEMPLATE}]})
            except Exception:  # noqa: BLE001
                pass
            _shops.append((shop_id, _fid))
    # Защита от пустышек: ни одна группа/товарное не создались → удаляем недоделанную кампанию.
    if not _shops:
        _delete_partial_campaign(token, login, cid)
        return {"ok": False, "name": name, "feed": feed_name, "campaign_id": cid, "partial_deleted": True,
                "error": "tp3 не дозаполнена: группа/товарное объявление не созданы"}
    shop = _shops[0][0]                                  # представитель (совместимость ниже)
    slset = None
    if data["sitelinks"]:
        base = href.rstrip("/")
        # Быстрые ссылки ведут ТОЛЬКО на главную страницу (base_href без пути).
        # /sl1../sl8 давали 404 — исправлено: Href = главная для всех ссылок.
        sl = [{"Title": s.get("title", ""), "Description": s.get("description", ""),
               "Href": base} for s in data["sitelinks"]]
        # Через общий `_get_or_reuse_sitelink_set`: (1) реюз набора с ТЕМ ЖЕ содержимым в
        # рамках прохода — tp3 фан-аутится по фидам, а Href у всех ссылок = главная, поэтому
        # набор у всех кампаний фан-аута идентичен; (2) Grid-фолбэк при 152 (нет баллов),
        # которого у прямого `cl.add_sitelinks_set` не было. Провал → None, как и раньше.
        try:
            slset = _get_or_reuse_sitelink_set(token, login, sl)
        except Exception:  # noqa: BLE001
            slset = None
    warn = None
    # Search-докрутка: уточнения/промо/ссылки уровня кампании,
    # места показа ADV_GALLERY (товарная галерея на поиске, без SEARCH_PAGE).
    try:
        _finalize_search_via_grid(
            login, cid, name=name, goal_id=goal_id, cpa_rub=cpa_rub,
            weekly_rub=(budget_rub or int(cpa_rub) * 10),
            counter_ids=[counter_id] if counter_id else [], pay_for_conversion=pay_for_conv,
            callout_ids=data["callout_ids"], sitelink_set_id=slset,
            promo_id=(data["promos"][0] if data["promos"] else None),
            minus_set_ids=[data["minus_set"]] if data.get("minus_set") else None,
            placement_types=["ADV_GALLERY"])
    except Exception as e:  # noqa: BLE001
        warn = f"Search-докрутка упала: {str(e)[:140]}"
    # текст по умолчанию на товарном объявлении: единый SHOPPING_DEFAULT_TEXT БЕЗ кредита,
    # как в tp5 (create_set_feed_builders R2-8 2026-07-10 «единый ShoppingAd default_text без
    # кредита, ОДИН общий на все каталожные/товарные кампании»). Раньше tp3 брал
    # data["default_text"] из slepok_content (тексты с кредитным углом) → «кредит» протекал
    # в товарное/каталожное объявление (CREDIT_IN_DEFAULT_TEXT_PRODUCT); tp5 уже был исправлен.
    try:
        from .create_set_assets import SHOPPING_DEFAULT_TEXT as _SDT3
    except Exception:  # noqa: BLE001 — fail-safe если импорт недоступен
        _SDT3 = "Новые автомобили в наличии. Большой выбор. Скидки до 45% при покупке. Тест-драйв."
    try:
        _gcl3 = gf.get_grid_client(login)
        for _sh, _sfid in _shops:                        # текст по умолчанию на КАЖДОМ товарном (все фиды)
            _gcl3.set_default_text([_sh], _sfid, _SDT3)
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
                         single_feed: bool = False, agency: str = "",
                         only_cts: set | None = None, only_gks: set | None = None,
                         all_feeds: bool = False) -> dict:
    """Боевая tp3 «Товарная галерея» (ЕПК, Поиск, placementTypes=['ADV_GALLERY'], товарная по фиду) — ПАРА cpc+cpa.
    FAN-OUT (CODER.md): мультиплицируется по ВСЕМ URL-фидам аккаунта — каждый фид своя пара,
    имя несёт название фида. single_feed=True → только /yandex.xml (как plan: _first_url_feed strict;
    фолбэк лишь при подтверждённом single_feed_fallback, иначе tp3 пропускается — НЕ первый фид). job — live-счётчик.

    Задача 7: base_name = имя camp_names-кампании (1:1 со «Структурой»). При ОДНОМ фиде fan-out
    даёт ровно 1 кампанию на camp_name. all_feeds (тег «все фиды») → ОДНА кампания, ГРУППА на фид
    (без per-feed fan-out). only_cts/only_gks приняты для сигнатурной паритетности (tp3-товарка —
    ct0000 автотаргет по фиду, не модель-роутинг; фактическая 1:1 — через имя camp_names + фид-группы)."""
    data = _tp5_account_data(token, login, slepok, site_type, agency)
    if not data["feeds"]:
        return {"ok": False, "name": base_name, "error": "нет URL-фидов на аккаунте для tp3"}
    if single_feed:
        _resolve_single_feed_variants(data, token, login, agency, job)
        if not data["feeds"]:
            return {"ok": False, "name": base_name,
                    "error": "single_feed: целевой фид (/yandex.xml или подтверждённый фолбэк) не найден — tp3 пропущена"}
    # ct009 = «Товарное/Фид» (CODER.md ag_part5): ShoppingAd+ListingAd по фиду.
    group_name = f"ct0000_aon_n000_{r_code}_ct009_ag001_g00 — Товарная галерея"
    results = []
    # ── тег «все фиды»: ОДНА кампания (пара cpc+cpa), ГРУППА на каждый разрешённый фид ──
    if all_feeds:
        _af_list = [(fid, fnm) for fid, fnm, _fu in data["feeds"]]
        _t3a = ([(base_name, "search_cpa", False)] if no_cpa
                else [(base_name, "search_cpa", False),
                      (base_name.replace("tp3_cpc_site", "tp3_cpa_site", 1), "search_payconv", True)])
        for nm, mode, pay in _t3a:
            if job and job.get("cancel"):
                break
            try:
                results.append(_create_tp3_single(
                    data, token, login, nm, mode, pay, goal_id, cpa_rub, budget_rub,
                    counter_id, region_ids, href, _af_list[0][0], _af_list[0][1], group_name,
                    corr, ret_map, all_feeds_list=_af_list))
                _bump_job(job, True)
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
    for feed_id, feed_name, feed_url in data["feeds"]:        # FAN-OUT: каждый фид → своя пара
        if job and job.get("cancel"):                        # отмена: стоп ПЕРЕД следующим фидом
            break
        # Bug D fix: используем URL фида (без https://) вместо короткого имени из кабинета.
        # Гард — по реально подставляемой метке (label), с срезкой домен-префикса (см. tp5 выше).
        _f_label3 = _strip_dom_lbl(
            (_re_fb.sub(r'^https?://', '', feed_url) if feed_url else feed_name), href)
        # Дедуп сегментов — здесь же (метка фида приклеивается ПОСЛЕ `_uniq`, см. tp5 выше).
        nm_cpc = _csctx.dedup_name_segments(f"{base_name} — {_f_label3}") if _f_label3 else base_name
        nm_cpa = nm_cpc.replace("tp3_cpc_site", "tp3_cpa_site", 1)
        _t3 = ([(nm_cpc, "search_cpa", False)] if no_cpa
               else [(nm_cpc, "search_cpa", False), (nm_cpa, "search_payconv", True)])
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

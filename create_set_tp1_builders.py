"""Create-set tp1/RSYA builders extracted from blueprint.py."""

from __future__ import annotations

import json
import time

_DEPS: dict = {}


def configure(deps: dict) -> None:
    """Bind blueprint dependencies used by tp1 builders."""
    _DEPS.clear()
    _DEPS.update(deps)
    globals().update(deps)


def _tp1_group_name(ct: str, r_code: str, brand: str, with_shopping: bool = False,
                    autotarget: bool = False) -> str:
    """Имя группы tp1 по CANON CODER.md (правило «кодер = реальный состав»).
    Суффикс _NN (было _11/_21/_22) убран по решению директолога (A2, 2026-06-22).

    with_shopping=False (TextAd only):  ct{N}_{aon/aoff}_n000_{r}_ct001_ag011_g00 — {Бренд}
    with_shopping=True  (TextAd+ListingAd+ShoppingAd = «Т+Л+ТОВ»):
                        ct{N}_aon_n000_{r}_ct010_ag011_g00 — {Бренд}
    Источник: справочник local_gsheet_naming (ag_part5): ct010 = «Комбинированный: ТГО + каталог/фид»,
    ct009 = «Товарное (Фид/каталог)» — БЕЗ TextAd. Группа с TextAd+ListingAd+ShoppingAd → ct010.
    ag011 (24-55+) — демо-таргетинг TextAd несёт корректировки по возрасту. Совпадает с эталоном Щербаковой.
    """
    aud_code = "aon" if autotarget else "aoff"
    if with_shopping:
        return f"{ct}_{aud_code}_n000_{r_code}_ct010_ag011_g00 — {brand}"
    return f"{ct}_{aud_code}_n000_{r_code}_ct001_ag011_g00 — {brand}"

def _tp1_video_ads(v501_client, login: str, ag_video: list) -> dict:
    """[ЗАДЕЛ НА БУДУЩЕЕ] Видео-креативы для РСЯ tp1 — ОТДЕЛЬНЫЕ объявления, НЕ TextAd.
    ag_video: [(adgroup_id, [абс.путь_видео, ...]), ...] — что собрал _build_tp1_from_pack.

    Механика РСЯ-видео в ЕПК (почему отдельно от картинки):
      видео → CREATIVE (видеоконструктор Директа) → CpcVideoAd(CreativeId) в группу.
      • v5 API НЕ создаёт видео-креатив из файла (creatives.get — только чтение; креатив
        делается в конструкторе/grid). → нужен creative-API (grid/web-api) — ПОКА не подключён.
      • UAC-путь upload_video_file→content_id (campaign.py) — ТОЛЬКО для tp6/tp7 (Мастер/Товарка),
        для ЕПК РСЯ не годится.
      • TextAd видео не несёт (только AdImageHash — картинка; это уже работает).

    Состояние: в паке M3 видео для tp1 НЕТ (скан 2026-06-22 → 0) → функция dormant.
    Когда появятся: (1) положить видео в манифест `video_slepki.txt` (как image_slepki.txt),
    (2) kp.read_videos подхватит, (3) здесь создать креатив и CpcVideoAd(CreativeId).
    Возврат: отчёт без падения сборки (видео — необязательно)."""
    rep = {"video_groups": sum(1 for _, v in ag_video if v), "video_ads": 0,
           "note": "creative-API ЕПК не подключён — видео-объявления РСЯ пока не создаются (см. докстринг)"}
    # TODO(video): for ag_id, paths in ag_video: creative_id = _make_video_creative(path);
    #              v501_client.add_cpc_video_ad(ag_id, creative_id)
    return rep

def _build_tp1_adgroups(
    token: str,
    login: str,
    campaign_id: int,
    region_ids: list,
    href: str,
    groups: list,
    sitelink_set_id: int | None = None,
    feed_id: int = 0,
    with_shopping: bool = False,
    feed_models: dict | None = None,
    autotarget: bool = False,
    products_only: bool = False,
    grid_cookie: str | None = None,
    base_sitelinks: list | None = None,
) -> dict:
    """Наполнить РСЯ (tp1 ЕПК) группами БАТЧЕМ через v501:
    adgroups.add (с TrackingParams и minus) → keywords.add → adimages.add → ads.add(TextAd+Image).

    feed_id + with_shopping (как в слепках Щербаковой): дополнительно к TextAd в КАЖДУЮ группу
    добавляем ListingAd (динамика) + ShoppingAd (товарное) по фиду — состав «Т+Л+ТОВ» в группе.
    Аддитивно: нет feed_id / with_shopping=False → создаём только TextAd (старое поведение).

    groups: [{name, ct, brand, keywords:[], minus:[], title, text, image_path?, callout_ext_ids?}].
    Анти-блок: операции батчами с паузами, групп ≤ _AC_GROUP_CAP за проход.
    Лимиты: ключей ≤200/группу, минус ≤100/группу, Title ≤35, Text ≤81.
    → {adgroups, keywords, ads, images_uploaded, sitelinks_set, errors, deferred}."""
    rep = {"adgroups": 0, "keywords": 0, "ads": 0, "images_uploaded": 0,
           "sitelinks_set": sitelink_set_id or 0, "errors": [], "deferred": 0}
    rids = [int(r) for r in (region_ids or []) if str(r).lstrip("-").isdigit()] or [225]
    if len(groups) > _AC_GROUP_CAP:
        rep["deferred"] = len(groups) - _AC_GROUP_CAP
        groups = groups[:_AC_GROUP_CAP]

    # ── Фаза 1: adgroups.add с TrackingParams ────────────────────────────────
    # v501 ЕПК: TrackingParams на уровне группы (#2 инвариант — UTM)
    # РСЯ (tp1): минуса на группе НЕ ставим — в сетях они режут охват без пользы.
    # Минус-слова для поисковых (tp2/tp4) — в _build_tp2_adgroups (отдельный путь).
    specs = []
    for g in groups:
        ag: dict = {
            "Name": (g.get("name") or "группа")[:255],
            "CampaignId": int(campaign_id),
            "RegionIds": rids,
            "TrackingParams": _UTM_TEMPLATE_TP1,   # #2 UTM на уровне группы
        }
        specs.append(ag)

    ag_ids = [None] * len(groups)
    idx = 0
    for chunk in _chunks(specs, _AC_CHUNK_AG):
        ja = _v5_call("adgroups", "add", token, login, {"AdGroups": chunk})
        if "error" in ja:
            rep["errors"].append(f"adgroups.add {_v5_err(ja)}")
            idx += len(chunk)
            time.sleep(_AC_BATCH_SLEEP)
            continue
        for r in (ja.get("result") or {}).get("AddResults", []):
            errs = r.get("Errors") or []
            if r.get("Id") and not errs:
                ag_ids[idx] = r["Id"]
                rep["adgroups"] += 1
            else:
                nm = groups[idx].get("name", "?") if idx < len(groups) else "?"
                rep["errors"].append(f"{nm}: adgroup " + ("; ".join(e.get("Message", "") for e in errs) or "нет Id"))
            idx += 1
        time.sleep(_AC_BATCH_SLEEP)

    # ── Фаза 2: keywords.add (ключи на группу ≤200) ──────────────────────────
    # autotarget=True → спецключ "---autotargeting" (1/группу) вместо реальных ключей (РСЯ-Автотаргет).
    kw_items = []
    for i, g in enumerate(groups):
        if not ag_ids[i]:
            continue
        if autotarget:
            kw_items.append({"Keyword": _AUTOTARGET_KW, "AdGroupId": int(ag_ids[i])})
            continue
        for k in _kw_clean(g.get("keywords") or [], 200):
            kw_items.append({"Keyword": k, "AdGroupId": int(ag_ids[i])})
    for chunk in _chunks(kw_items, _AC_CHUNK_KW):
        jk = _v5_call("keywords", "add", token, login, {"Keywords": chunk})
        if "error" not in jk:
            rep["keywords"] += sum(1 for r in (jk.get("result") or {}).get("AddResults", []) if r.get("Id"))
        else:
            rep["errors"].append(f"keywords.add {_v5_err(jk)}")
        time.sleep(_AC_BATCH_SLEEP)

    # ── Фаза 3: ads.add без предварительной token-загрузки картинок ──────────
    # Живой баг 2026-06-28: v501 upload_image на больших tp1 мог зависать до стадии ads.add.
    # Поэтому здесь не тратим время на upload_image вообще: ResponsiveAd создаём сразу,
    # а картинки добиваем post-create через Grid/куки по фактическим ad_id.

    # products_only (Смарт-Баннер/Фиды «без ТГО»): пропускаем КОМБИНАТОРНОЕ, оставляем только товарные (Фаза 4).
    # КОМБИНАТОРНОЕ (ResponsiveAd) через v501 — замена ТГО (отключают с 30.06): несколько заголовков/текстов
    # + картинки (AdImageHashes) + быстрые ссылки (SitelinkSetId). Уточнения наследуются (AdExtensions нет).
    _sl_set_cache: dict = {}   # ad_href → sitelink_set_id (per-group кэш, #ФИКС-3)
    _base_href = (href or "").rstrip("/")
    ad_items = []
    ad_meta = []   # параллельно ad_items: {brand,href,titles,bodies,image_hashes} — для adPrice из фида
    for i, g in enumerate(groups):
        if products_only:
            break
        if not ag_ids[i]:
            continue
        ad_href = g.get("href") or href   # per-group deep-link приоритетнее общего href кампании
        img_paths = g.get("image_paths") or ([g.get("image_path")] if g.get("image_path") else [])
        ra = _responsive_ad(g.get("titles") or [g.get("title"), g.get("brand"), g.get("name")],
                            g.get("texts") or [g.get("text")], ad_href,
                            image_hashes=None)
        if not ra:
            rep["errors"].append(f"{g.get('name', '?')}: пропущено объявление (нет заголовков/текстов/href)")
            continue
        # Per-group sitelink set (#ФИКС-3): href группы ≠ href кампании → создать/закэшировать набор
        _use_sl_id = sitelink_set_id
        if base_sitelinks and ad_href and ad_href.rstrip("/") != _base_href:
            if ad_href not in _sl_set_cache:
                try:
                    _grp_sls = [{**s, "Href": ad_href} for s in base_sitelinks]
                    _sl_set_cache[ad_href] = _get_or_reuse_sitelink_set(token, login, _grp_sls)
                except Exception:  # noqa: BLE001
                    _sl_set_cache[ad_href] = None
            _use_sl_id = _sl_set_cache.get(ad_href) or sitelink_set_id
        if _use_sl_id:
            ra["SitelinkSetId"] = _use_sl_id
        ad_items.append({"AdGroupId": int(ag_ids[i]), "ResponsiveAd": ra})
        ad_meta.append({"brand": g.get("brand") or g.get("name") or "", "href": ad_href,
                        "seg": _ct_segment(g.get("ct") or ""),   # 'Марки' → цена = МИН по марке
                        "titles": ra.get("Titles") or [], "bodies": ra.get("Texts") or [],
                        "image_hashes": [],
                        "image_paths": img_paths[:5]})

    created_ad_meta = []   # [{id, meta}] созданных — для image backfill + adPrice
    repair_items: list[dict] = []
    _base = 0
    for chunk in _chunks(ad_items, _AC_CHUNK_AD):
        jd = _v501_svc("ads", "add", token, login, {"Ads": chunk})
        if "error" not in jd:
            for k, r in enumerate((jd.get("result") or {}).get("AddResults", [])):
                if r.get("Id"):
                    rep["ads"] += 1
                    gi = _base + k
                    if gi < len(ad_meta):
                        created_ad_meta.append({"id": int(r["Id"]), "meta": ad_meta[gi]})
                for e in (r.get("Errors") or []):
                    rep["errors"].append(f"ResponsiveAd(tp1): {e.get('Message')} {e.get('Details','')}".strip())
        else:
            rep["errors"].append(f"ads.add(tp1 ResponsiveAd) {_v5_err(jd)}")
        _base += len(chunk)
        time.sleep(_AC_BATCH_SLEEP)

    # ── Фаза 3.4: post-create repair через Grid/куки для token-path ──────────────────────────
    # Даже если v501 ads.add прошло, live payload в Директе может урезаться. После создания
    # всегда добиваем titles + bodies + imageHashes через Grid. Это основной fallback:
    # если token-path недогрузил объявление, куки-path дособирает именно его, а не оставляет брак.
    if created_ad_meta:
        try:
            import os as _os3
            _gc_img = gf.GridClient(login, cookie=grid_cookie)
            _uploaded_by_name: dict[str, str] = {}
            _upd_items = []
            for _rec in created_ad_meta:
                _meta = _rec["meta"]
                _hashes = list(dict.fromkeys(_meta.get("image_hashes") or []))
                for _pth in (_meta.get("image_paths") or []):
                    if len(_hashes) >= 5:
                        break
                    if not _pth or not _os3.path.isfile(_pth):
                        continue
                    _bn = _os3.path.basename(_pth)
                    _h = _uploaded_by_name.get(_bn)
                    if not _h:
                        _h = _cached_upload_image(_gc_img, login, _pth)
                        if _h:
                            _uploaded_by_name[_bn] = _h
                    if _h and _h not in _hashes:
                        _hashes.append(_h)
                if _hashes:
                    _meta["image_hashes"] = _hashes[:5]
                _upd = {"id": _rec["id"], "href": _meta["href"],
                        "titles": _meta["titles"], "bodies": _meta["bodies"]}
                if _meta.get("image_hashes") is not None:
                    _upd["image_hashes"] = _meta.get("image_hashes") or []
                _upd_items.append(_upd)
            if _upd_items:
                repair_items = list(_upd_items)
                rep["ads_repaired"] = _grid_update_adaptive_ads(login, _upd_items)
                rep["image_groups"] = len(_upd_items)
        except Exception as _e:  # noqa: BLE001
            rep.setdefault("warnings", []).append(f"tp1 repair: {str(_e)[:100]}")

    # ── Фаза 3.5: ЦЕНА из фида в комбинаторное объявление (#2) — Grid по куке (без баллов).
    # adPrice = {current, old} самого дешёвого оффера фида по бренду/модели группы («от X · зачёркнуто old»).
    try:
        _pfeed = feed_id or _grid_price_feed(login, href) or _first_url_feed(token, login)
        _pmap = _grid_feed_offer_prices(login, _pfeed) if _pfeed else {}
        if _pmap and created_ad_meta:
            _pitems = []
            for _rec in created_ad_meta:
                ad_id, meta = _rec["id"], _rec["meta"]
                cur, old = _group_ad_price(_pmap, meta.get("brand", ""), meta.get("seg", ""))
                if cur:
                    _pitems.append({"id": ad_id, "href": meta["href"], "titles": meta["titles"],
                                    "bodies": meta["bodies"], "image_hashes": meta["image_hashes"],
                                    "current": cur, "old": old})
            rep["prices_set"] = _grid_set_ad_prices(login, _pitems)
            if repair_items:
                rep["ads_repaired_after_price"] = _grid_update_adaptive_ads(login, repair_items)
    except Exception as _e:  # noqa: BLE001 — цена не критична, объявление уже создано
        rep.setdefault("warnings", []).append(f"adPrice: {str(_e)[:100]}")

    # ── Фаза 4: товарные по фиду (как в слепках Щербаковой): ListingAd (динамика) + ShoppingAd (товарное)
    # в каждую группу → состав «Т+Л+ТОВ». ShoppingAd создаём ЧЕРЕЗ GRID (addShoppingAds, БЕЗ баллов) —
    # v501 ads.add(ShoppingAd) требовал units и валил докрутку в 152. Только если есть фид и флаг.
    if feed_id and with_shopping:
        rep["listing_ads"], rep["shopping_ads"], rep["shopping_skipped"] = 0, 0, 0
        rep["shopping_ad_ids"] = []   # собираем id для set_default_text (#6 фикс пустого текста)
        rep["shopping_filters"] = {}
        rep["listing_build_items"] = []
        rep["listing_name_by_shop"] = {}   # {shopping_ad_id: name_value} — для name-фильтра листинга
        _grid_shop_items = []         # батч для Grid addShoppingAds: [{adgroup_id, feed_id, vendor/coll}]
        # Коллекции фида (HAR: фильтр «Страницы каталога» = collectionId). Тянем РОВНО этот фид через
        # Grid op Listings (точечный per-feed запрос, не урезанный _grid_feeds). Для брендовых групп
        # резолвим бренд-уровневую коллекцию (id вроде '25' = «Новые автомобили BAIC»), для модельных —
        # model_*. Пустой список → фолбэк на feed_models из _account_model_feeds.
        _feed_colls = _feed_collections(login, int(feed_id), cookie=grid_cookie)
        _feed_models_eff = dict(feed_models or {})
        if not _feed_models_eff:
            _feed_models_eff = _feed_models_from_collections(_feed_colls)
        for i in range(len(groups)):
            if not ag_ids[i]:
                continue
            # Фильтр по бренду/модели — ОБЯЗАТЕЛЕН для товарных объявлений в брендовой группе.
            # Без фильтра ShoppingAd/ListingAd показывает ВЕСЬ фид (все марки), что недопустимо
            # в группе конкретного бренда (например, Lada Granta → только Lada, не Haval/Changan).
            #
            # Алгоритм:
            #   feed_models передан   → попробовать collectionId по имени модели/бренда группы;
            #                           нет совпадения → пропускаем (нет коллекции этого бренда в фиде).
            #   feed_models is None   → фид без model-листингов / agency не передан;
            #                           «Vendor»-фильтр через FeedFilterConditions в v501 НЕ верифицирован
            #                           живым тестом → создавать объявление по всему фиду ЗАПРЕЩЕНО.
            # ДВА РАЗНЫХ фильтра по типу объявления (решение Семёна, HAR36):
            #   Товары (ShoppingAd)        → vendor CONTAINS_ANY [марка] (НЕ collectionId — task-6 сломал).
            #   Страницы каталога (Listing) → name CONTAINS_ANY [марка | марка+модель] (updateListingAds).
            #   ct0000/общее (без марки)   → без фильтра (вся витрина).
            _g_brand = (groups[i].get("brand") or "").strip()
            _g_seg = _ct_segment(groups[i].get("ct") or "")
            # Фильтр по производителю/названию валиден ТОЛЬКО для брендовых групп («Марки»/«Модели»).
            # Для «Общее» brand = имя темы («Автокредит» и т.п.) → vendor/name стали бы мусором
            # (vendor содержит «avtokredit» → 0 товаров). Общее → товарка по всему фиду, каталог — все стр.
            _is_brand_seg = _g_seg in ("Марки", "Модели")
            _vendor = _vendor_value(_g_brand) if (_g_brand and _is_brand_seg) else None
            _name_val = _listing_name_value(_g_brand, _g_seg) if (_g_brand and _is_brand_seg) else None
            _model_vals = _model_field_values(_g_brand, _g_seg) if (_g_brand and _is_brand_seg) else []  # Модели → +model
            _grid_shop_items.append({"adgroup_id": int(ag_ids[i]), "feed_id": int(feed_id),
                                     "vendor": _vendor, "collection_id": None, "model": _model_vals,
                                     "name": groups[i].get("name", "?")})
            rep["listing_build_items"].append({
                "adgroup_id": int(ag_ids[i]),
                "feed_id": int(feed_id),
                "name_value": _name_val,                       # name-фильтр листинга (None для ct0000)
                "name": groups[i].get("name", "?"),
            })
        # Батч Grid addShoppingAds — без баллов (id в порядке adAddItems). При сбое всего батча
        # каждая группа уже имеет TextAd; товарка докрутится ретраем — не валим кампанию.
        if _grid_shop_items:
            try:
                _ids = gf.GridClient(login, cookie=grid_cookie).add_shopping_ads(_grid_shop_items)
                rep["shopping_ad_ids"] = [int(x) for x in _ids if x]
                rep["shopping_ads"] = len(rep["shopping_ad_ids"])
                for _li, (_raw_id, _src) in enumerate(zip(_ids, _grid_shop_items)):
                    if not _raw_id:
                        continue
                    # листинг этой группы (by-shopping) получит name-фильтр по shopping_ad_id
                    _nv = (rep["listing_build_items"][_li] or {}).get("name_value") if _li < len(rep["listing_build_items"]) else None
                    if _nv:
                        rep["listing_name_by_shop"][int(_raw_id)] = _nv
                    _conds = []
                    if _src.get("vendor"):
                        _conds.append({"field": "vendor", "operator": "CONTAINS_ANY",
                                       "stringValue": json.dumps(_vendor_filter_values(_src["vendor"]), ensure_ascii=False)})
                    if _src.get("model"):
                        _mvals = _src["model"] if isinstance(_src["model"], list) else [str(_src["model"])]
                        _mvals = [str(x) for x in _mvals if str(x).strip()]
                        if _mvals:
                            _conds.append({"field": "model", "operator": "CONTAINS_ANY",
                                           "stringValue": json.dumps(_mvals, ensure_ascii=False)})
                    if not _conds and _src.get("collection_id"):
                        _conds.append({"field": "collectionId", "operator": "EQUALS_ANY",   # collectionId → EQUALS_ANY
                                       "stringValue": json.dumps([str(_src["collection_id"])], ensure_ascii=False)})
                    if _conds:
                        rep["shopping_filters"][int(_raw_id)] = {"tab": "CONDITION", "conditions": _conds}
            except Exception as e:  # noqa: BLE001
                rep["errors"].append(f"shopping(Grid addShoppingAds): {str(e)[:120]}")
    return rep

def _build_tp1_from_pack(
    token: str,
    login: str,
    campaign_id: int,
    slepok: str,
    site_type: str,
    region_ids: list,
    href: str,
    r_code: str,
    titles: list | None,
    texts: list,
    counter_id: int = 0,
    feed_id: int = 0,
    with_shopping: bool = False,
    feed_models: dict | None = None,
    segment: str | None = None,
    ai_title2: str = "",
    city: str = "",
    autotarget: bool = False,
    products_only: bool = False,
    tp_code: str = "tp1",
    sitelinks: list | None = None,
    grid_cookie: str | None = None,
) -> dict:
    """Наполнить РСЯ (tp1/tp5 ЕПК) бренд-группами из пака M3.

    tp_code: код пака M3 для gather() — 'tp1' для РСЯ-кампаний, 'tp5' для комбинированных
    поисковых. По умолчанию 'tp1' (обратная совместимость).
    segment ('Марки'|'Модели'|None): фильтр ct-папок по сегменту (как в боевых аккаунтах —
    марки и модели РАЗНЫМИ кампаниями). None → все группы (старое поведение).
    Каждая ct-папка пака = отдельная группа. Имя группы = КАНОН CODER.md.
    Ключи/минус/уточнения/картинки — из пака. Объявления: TextAd + AdImageHash.
    Быстрые ссылки (sitelinks) — из direct_slepok_content слепка: создаём набор ОДИН раз,
    привязываем через SitelinkSetId ко ВСЕМ объявлениям группы.
    Callouts — из пака (per-бренд) через AdExtensions на объявление.
    """
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    pack = kp.gather(key, site_type, tp_code)  # один ssh-вызов к M3
    if not pack:
        # Пустой пак (у слепка нет tp_code-пака, напр. pavlov/tp5; M3 при этом жив — tp1-пак есть).
        # Для ТОВАРНОЙ ГАЛЕРЕИ по фиду это НЕ блокер: фид-товарка не зависит от бренд-пака →
        # проваливаемся в фид-фолбэк ниже (создаст товарную галерею по фиду). Иначе — честный скип.
        if not (with_shopping and feed_id):
            return {"skipped": "пак недоступен (мост M3?)"}
        pack = {}

    text0 = (texts[0] if texts else "")[:81] or "Официальный дилер. Тест-драйв и выгодные условия по кредиту. Авто в наличии."
    ct_model = kp.feeds_ct_model()            # фид-картиночный индекс (ct0020+, модели) — фолбэк
    ct_name = _ag_part1_map()                 # ct→имя из gsheet_naming (ag_part1, полное покрытие 318)
    _sc_titles = _slepok_campaign_content(slepok, site_type).get("titles") or []  # пул слепка — 1 раз на кампанию
    # URL страниц моделей: account-level мёрж (все фиды, как цены) → покрывает марки без URL
    # в конкретном feed_id (#ФИКС-8).
    _feed_urls_tp1 = _account_offer_urls(login, href)

    # Строим группы ТОЛЬКО для ct-папок у которых есть ключи scherbakova
    groups = []
    _img_rr = 0                                   # round-robin по пулу картинок ct (Павлов: «разбавить однотипными»)
    for ct in sorted(pack.keys()):
        data = pack.get(ct) or {}
        if not data.get("positive"):
            continue                           # пропускаем ct без ключей scherbakova
        if segment and _ct_segment(ct) != segment:
            continue                           # сегментный фильтр (Марки/Модели как в боевых)
        raw_brand = ct_name.get(ct) or ct_model.get(ct) or ct
        brand = _valid_pack_brand_name(ct, raw_brand)   # логический бренд: пустой для «Общее»
        group_label = _pack_group_display_name(ct, raw_brand, brand)
        group_name = _tp1_group_name(ct, r_code, group_label, with_shopping=with_shopping,
                                     autotarget=autotarget)
        # Картинки: общие ct0000-ct0014 → общий пул ct0000; кузова ct0015-ct0018 → свой ct;
        # модели/марки → свой ct.
        all_images = _creative_images_for_ct(site_type, tp_code, ct, key)
        # Ротация по пулу картинок (а не всегда [0]) — чтобы РСЯ-объявления не были однотипными.
        # image_path — первая из ротации (совместимость); image_paths — все (для мульти-upload в Фазе 3).
        image_path = all_images[_img_rr % len(all_images)] if all_images else None
        _img_rr += 1
        # deep-link: сначала реальный URL из фида (targetUrl), фолбэк на формульный слаг (#ФИКС-2).
        # ФИКС A: Марки → /auto/{brand} (первые 2 сегмента), Модели → полный путь без query. (#ФИКС-A)
        _raw_feed_url = _feed_url_for_model(_feed_urls_tp1, brand)
        if _raw_feed_url:
            model_href = (_brand_level_url(_raw_feed_url) if _ct_segment(ct) == "Марки"
                          else _strip_url_query(_raw_feed_url))
        else:
            model_href = _model_page_href(href, site_type, brand)
        # Title: шаблон «Новые {brand} в {город}. {акция}» (≤35 симв.) — фолбэк brand[:35].
        # ai_title2 — ИИ-заголовок (если дан), иначе round-robin из пула.
        is_brand_group = _ct_segment(ct) in ("Марки", "Модели")
        title = (_title_from_template(brand, city) if (is_brand_group and not ai_title2)
                 else (_GENERIC_AT_TITLES[0] if not is_brand_group else brand[:35]))
        ttl2 = (ai_title2[:30] if ai_title2 else _next_title2())   # ИИ-title2 или round-robin из пула
        # Внутри pack-групп не вызываем M3 per-ct: ИИ уже сгенерировал контент кампании/item.
        # Иначе tp1/tp5 на больших паках создаются неприемлемо долго и старт очереди "замирает".
        g_titles = _rsya_titles(brand, city, site_type, ai_title2=ai_title2,
                                base=(list(titles or []) + [title, ttl2] if is_brand_group
                                      else list(titles or []) + list(_GENERIC_AT_TITLES)),
                                pool=_sc_titles, is_brand=is_brand_group)
        g_texts = _rsya_texts(list(texts or []) + ([text0] if text0 else []), site_type, city, brand)
        groups.append({
            "name": group_name,
            "ct": ct,
            "brand": brand,
            # БАГ-13: для «Марки» — убрать ключи «марка+модель» (напр. «Chery Tiggo 8 Pro»)
            "keywords": _filter_group_keywords(data.get("positive", []), _ct_segment(ct), brand, city, site_type),
            "minus": data.get("minus", []),
            "titles": g_titles or ([title, brand] if brand else [title]),
            "texts": g_texts or ([text0] if text0 else []),
            "title": title, "title2": ttl2, "text": text0,   # совместимость
            "href": model_href,               # deep-link страницы модели
            "image_path": image_path,         # первая картинка (round-robin); совместимость
            "image_paths": all_images[:5],    # все пути (пак + Manual) — для мульти-upload в Фазе 3
            "callouts": data.get("callouts", []),
        })

    if not groups and with_shopping and feed_id:
        # Бренд-пак слепка для tp_code ПУСТ (напр. у pavlov нет tp5-пака), НО это товарная галерея
        # по фиду — фид-товарка (ShoppingAd/ListingAd) НЕ зависит от бренд-пака. Чтобы tp5/tp3 не
        # выходили пустыми, создаём ОДНУ товарную-галерею группу по всему фиду: автотаргет + общие
        # заголовки/тексты + товарные объявления (with_shopping ниже добавит ShoppingAd+ListingAd).
        groups = [{
            "name": "Товарная галерея", "ct": "ct0000", "brand": "",
            "keywords": [], "minus": [],
            "titles": list(_GENERIC_AT_TITLES),
            "texts": list(_GENERIC_TEXT_FILLERS),
            "title": _GENERIC_AT_TITLES[0], "title2": "",
            "text": (_GENERIC_TEXT_FILLERS[0] if _GENERIC_TEXT_FILLERS else ""),
            "href": href, "image_path": None, "callouts": [],
        }]
        autotarget = True                                 # товарная галерея по фиду = автотаргет (нет бренд-ключей)
    if not groups:
        return {"skipped": f"пак пуст: нет ct-папок с ключами scherbakova для {tp_code}"}

    # Быстрые ссылки: создаём набор ОДИН раз → SitelinkSetId на каждое объявление.
    # Важно: v5-only путь здесь молча оставлял tp1 без ссылок при пустом kind='sitelinks'.
    # Общий resolver берёт campaign-content слепка и умеет Grid/cookie fallback без баллов.
    sitelink_set_id = None
    base_sitelinks: list = []   # нормализованные ссылки для per-group наборов (#ФИКС-3)
    asset_warns = []
    try:
        if not sitelinks:
            sitelinks = _ai_common_sitelinks(login, slepok, site_type, city, tp_code)
        _assets = _resolve_campaign_assets(token, login, href, sitelinks=sitelinks,
                                           slepok=slepok, site_type=site_type, grid_cookie=grid_cookie)
        sitelink_set_id = _assets.get("sitelink_set_id")
        base_sitelinks = _assets.get("asset_sitelinks") or []
    except Exception as e:  # noqa: BLE001 — sitelinks не критичны, но должны быть видны в отчёте
        asset_warns.append(f"sitelinks(tp1): {str(e)[:120]}")

    # Callouts: создаём общий пул AdExtensions (уточнения из пака)
    co_pool = {}
    try:
        all_co = [c for g in groups for c in (g.get("callouts") or [])]
        co_pool = _ensure_callout_exts(token, login, all_co) if all_co else {}
        for g in groups:
            ids = [co_pool[c] for c in (g.get("callouts") or []) if c in co_pool]
            if ids:
                g["callout_ext_ids"] = ids[:4]
    except Exception:  # noqa: BLE001
        co_pool = {}

    rep = _build_tp1_adgroups(token, login, campaign_id, region_ids, href, groups,
                               sitelink_set_id=sitelink_set_id,
                               base_sitelinks=base_sitelinks or None,
                               feed_id=feed_id, with_shopping=with_shopping, feed_models=feed_models,
                               autotarget=autotarget, products_only=products_only,
                               grid_cookie=grid_cookie)
    rep["cts"] = len(pack)
    rep["groups_built"] = len(groups)
    rep["callouts_pool"] = len(co_pool)
    rep["sitelinks_set_id"] = sitelink_set_id
    if asset_warns:
        rep.setdefault("warnings", []).extend(asset_warns)
    # [задел на будущее] видео-объявления РСЯ — отдельный хук _tp1_video_ads (сейчас dormant:
    # видео для tp1 в паке M3 нет + creative-API ЕПК не подключён; картинки РСЯ уже работают).
    rep["video"] = "хук готов (_tp1_video_ads), dormant — нет видео в tp1 на M3 + нужен creative-API ЕПК"
    return rep

def _add_listing_ads_v501(token: str, login: str, items: list[dict]) -> list[int]:
    """Создать ListingAd через v501 с явным FeedFilterConditions по collectionId.

    Для брендовых групп список collectionId уже развёрнут заранее. Пустой collection_ids
    считаем ошибкой данных и не создаём листинг по всему фиду.
    """
    out: list[int] = []
    for it in items or []:
        coll_ids = [str(x) for x in (it.get("collection_ids") or []) if str(x).strip()]
        if not coll_ids:
            continue
        payload = {
            "Ads": [{
                "AdGroupId": int(it["adgroup_id"]),
                "ListingAd": {
                    "FeedId": int(it["feed_id"]),
                    "FeedFilterConditions": [{
                        "Operand": "collectionId",
                        "Operator": "EQUALS_ANY",
                        "Arguments": coll_ids,
                    }],
                },
            }],
        }
        j = _v501_svc("ads", "add", token, login, payload)
        add_res = ((j.get("result") or {}).get("AddResults") or [{}])[0]
        if add_res.get("Id") and not (add_res.get("Errors") or []):
            out.append(int(add_res["Id"]))
            continue
        errs = add_res.get("Errors") or []
        msg = "; ".join((e.get("Message") or "") for e in errs if isinstance(e, dict)).strip()
        if not msg:
            msg = _v5_err(j)
        raise RuntimeError(f"{it.get('name', '?')}: listing v501 {msg[:180]}")
    return out

def _create_tp1_single(
    token: str,
    login: str,
    name: str,
    counter_id: int,
    goal_id: int,
    cpa_value_rub: int,
    mode: str,
    region_ids: list,
    href: str,
    slepok: str,
    site_type: str,
    r_code: str,
    titles: list | None,
    texts: list,
    feed_id: int = 0,
    with_shopping: bool = False,
    feed_models: dict | None = None,
    budget_rub: int = 0,
    segment: str | None = None,
    city: str = "",
    ai_title2: str = "",
    sitelinks: list | None = None,
    callout_texts: list | None = None,
    callout_ids: list | None = None,
    autotarget: bool = False,
    products_only: bool = False,
    grid_cookie: str | None = None,
) -> dict:
    """Создать ОДНУ кампанию tp1 (РСЯ) через ЕПК v501 с указанным mode.

    mode='network_cpa'     → cpc-вариант: AVERAGE_CPA в сетях (tp1_cpc_site)
    mode='network_payconv' → cpa-вариант: PAY_FOR_CONVERSION в сетях (tp1_cpa_site)

    Инварианты: персонализация ВЫКЛ, мониторинг ВКЛ, расш.гео ВЫКЛ.
    Кампания создаётся как DRAFT (launch=False).

    Возвращает {"ok": True, "campaign_id": ..., "tp1_build": {...}} или {"ok": False, ...}.
    """
    spec = cmc.UnifiedCampaignSpec(
        name=name,
        client_login=login,
        oauth_token=token,
        mode=mode,
        region_ids=region_ids,
        counter_ids=[counter_id] if counter_id else None,
        goal_id=goal_id or None,
        network_average_cpa=int(cpa_value_rub) * 1_000_000,  # руб → мкруб (для network_cpa)
        search_cpa=int(cpa_value_rub) * 1_000_000,            # руб → мкруб (для network_payconv)
        apply_invariants=True,                                  # #3/#4/#5 из CAMPAIGN_INVARIANTS.md
    )
    v501 = cmc.DirectV501Client(token, login)
    campaign_id = None

    def _cleanup_partial(reason: str) -> dict:
        deleted = False
        if campaign_id:
            try:
                v501.delete_campaigns([int(campaign_id)])
                deleted = True
            except Exception:  # noqa: BLE001
                try:
                    deleted = bool(campaign_id in (gc.GridCreateClient(login).delete_campaigns([campaign_id]).get("deleted") or []))
                except Exception:  # noqa: BLE001
                    deleted = False
        return {"ok": False, "name": name, "campaign_id": campaign_id,
                "partial_deleted": deleted, "error": reason[:240]}

    try:
        campaign_id = v501.create_unified_campaign(spec, launch=False)

        # Привязка счётчика Метрики через v501 campaigns.update.
        # Soft-операция: если упадёт — кампания создана, просто без счётчика.
        counter_note = None
        if counter_id:
            try:
                j_upd = _v501_call("update", token, login, {
                    "Campaigns": [{"Id": campaign_id,
                                   "UnifiedCampaign": {"CounterIds": {"Items": [int(counter_id)]}}}]
                })
                upd_errs = ((j_upd.get("result") or {}).get("UpdateResults") or [{}])[0].get("Errors") or []
                if upd_errs:
                    counter_note = f"счётчик {counter_id} не привязался: {upd_errs[0].get('Message','?')}"
            except Exception as e:  # noqa: BLE001
                counter_note = f"счётчик {counter_id} не привязался: {str(e)[:120]}"

        # Наполняем бренд-группами из пака M3.
        tp1_build = _build_tp1_from_pack(
            token, login, campaign_id, slepok, site_type, region_ids,
            href, r_code, titles, texts, counter_id=counter_id,
            feed_id=feed_id, with_shopping=with_shopping, feed_models=feed_models,
            segment=segment, ai_title2=ai_title2, city=city, autotarget=autotarget,
            products_only=products_only, sitelinks=sitelinks, grid_cookie=grid_cookie)
        if tp1_build.get("error") or tp1_build.get("skipped") or not tp1_build.get("adgroups"):
            return _cleanup_partial("tp1 не дозаполнена: " + str(tp1_build.get("error") or tp1_build.get("skipped") or "группы не созданы"))
        if not products_only and not tp1_build.get("ads"):
            _details = []
            for _k in ("adgroups", "keywords", "images_uploaded"):
                if tp1_build.get(_k) is not None:
                    _details.append(f"{_k}={tp1_build.get(_k)}")
            _errs = (tp1_build.get("errors") or [])[:3]
            _warns = (tp1_build.get("warnings") or [])[:2]
            if _errs:
                _details.append("errors: " + "; ".join(str(x) for x in _errs))
            if _warns:
                _details.append("warnings: " + "; ".join(str(x) for x in _warns))
            return _cleanup_partial("tp1 не дозаполнена: объявления не созданы"
                                    + (f" ({'; '.join(_details)})" if _details else ""))

        # #6 Фикс пустого текста товарных объявлений (ShoppingAd) в tp1.
        _tp1_default_text = ((texts[0] if texts else "")[:81]
                             or "Официальный дилер. Тест-драйв и выгодные условия по кредиту. Авто в наличии.")
        _shop_ids = tp1_build.get("shopping_ad_ids") or []
        if with_shopping and feed_id and not _shop_ids:
            # Bug2 graceful (как tp7 whole-feed fallback): фид без офферов/модельных листингов
            # (напр. лендинг-фид) → НЕ удаляем кампанию — в ней валидные TextAd-группы РСЯ.
            # Оставляем как РСЯ без товарки + warning (hard-fail остаётся только «группы не созданы»).
            tp1_build.setdefault("warnings", []).append(
                "товарка не создана: фид без ShoppingAd — оставлена РСЯ без товарных объявлений")
        if _shop_ids and feed_id:
            _gcl = gf.GridClient(login, cookie=grid_cookie)
            # G review: set_default_text в СВОЁМ try — падение (Яндекс 500) НЕ должно блокировать листинги
            # (раньше оба в одном try → текст падал → листинги пропускались → 0 ListingAd).
            try:
                _gcl.set_default_text(
                    _shop_ids, feed_id, _tp1_default_text,
                    filters_by_ad_id=(tp1_build.get("shopping_filters") or {}),
                )
                tp1_build["shopping_text_set"] = len(_shop_ids)
            except Exception as _e:  # noqa: BLE001
                tp1_build.setdefault("warnings", []).append(f"shopping text: {str(_e)[:120]}")
            # Листинги «Страницы каталога» — НЕЗАВИСИМО от текста (Grid by-shopping, без баллов), затем
            # name-фильтр CONTAINS_ANY [марка|марка+модель] (HAR36 updateListingAds; by-shopping не наследует).
            # #ФИКС-1: saveDraft:True → addedAds пуст → строим _lf_items из listing_build_items по adGroupId.
            try:
                _rows = _gcl.add_listing_ads_by_shopping_ads(_shop_ids) or []
                tp1_build["listing_ads"] = len(_rows)
                # adGroupId→name_val из listing_build_items (независимо от addedAds)
                _agid_to_nv = {str(it["adgroup_id"]): it.get("name_value")
                               for it in (tp1_build.get("listing_build_items") or [])
                               if it.get("adgroup_id") and it.get("name_value")}
                _lf_items = []
                for _idx, _row in enumerate(_rows):
                    _lid = _row.get("id") if isinstance(_row, dict) else _row
                    _agid = str(_row.get("adGroupId") or "") if isinstance(_row, dict) else ""
                    _val = _agid_to_nv.get(_agid)
                    if not _val and _idx < len(_shop_ids):
                        _val = (tp1_build.get("listing_name_by_shop") or {}).get(int(_shop_ids[_idx]))
                    if _lid and _val:
                        _lf_items.append({"id": _lid, "feed_id": feed_id, "value": _val,
                                          "bodies": [_tp1_default_text]})
                if not _lf_items and _agid_to_nv:
                    # saveDraft:True → addedAds пуст; строим по adGroupId (фильтр ставится на группу)
                    for _agid_s, _val in _agid_to_nv.items():
                        _lf_items.append({"adgroup_id": _agid_s, "feed_id": feed_id,
                                          "value": _val, "bodies": [_tp1_default_text]})
                if _lf_items:
                    tp1_build["listing_name_set"] = _gcl.set_listing_name_filters(_lf_items)
            except Exception as _le:  # noqa: BLE001
                tp1_build.setdefault("warnings", []).append(f"listing(grid): {str(_le)[:160]}")
        if with_shopping and feed_id and _shop_ids and not int(tp1_build.get("listing_ads") or 0):
            # Bug2 graceful: ShoppingAd есть, а листинги «Страницы каталога» пусты (фид-каталог без
            # готовых офферов) → НЕ удаляем кампанию, оставляем товарку без листингов + warning.
            tp1_build.setdefault("warnings", []).append(
                "листинги каталога: 0 ListingAd (по by-shopping) — оставлена товарка без листингов")

        result_d = {"ok": True, "name": name, "campaign_id": campaign_id,
                    "launched": False, "tp1_build": tp1_build,
                    "url": f"https://direct.yandex.ru/dna/campaign/{campaign_id}?ulogin={login}"}
        if counter_note:
            result_d["counter_note"] = counter_note

        # ── Grid-докрутка РСЯ: уточнения/промо/быстрые ссылки на УРОВНЕ КАМПАНИИ ──
        # БАГ-1 FIX (2026-06-24): вынесена в ОТДЕЛЬНЫЙ try/except (ранее была внутри общего
        # try → GridFinalizeError → except Exception → _cleanup_partial УДАЛЯЛ кампанию с
        # 34+ объявлениями!). Теперь финализация best-effort: кампания остаётся, ошибка
        # пишется в result_d["finalize_warn"]. Grid принимает goalId="0" (проверено live).
        _ai_sitelinks = sitelinks or _ai_common_sitelinks(login, slepok, site_type, city, "tp1")
        a = _resolve_campaign_assets(token, login, href, sitelinks=_ai_sitelinks,
                                     slepok=slepok, site_type=site_type,
                                     prefer_callout_texts=callout_texts,
                                     prefer_callout_ids=callout_ids,
                                     grid_cookie=grid_cookie)
        slset = a.get("sitelink_set_id")
        wkl = int(budget_rub) if budget_rub else int(cpa_value_rub) * 10
        try:
            _finalize_rsya(
                login, campaign_id, name=name, goal_id=goal_id or 0,
                cpa_rub=cpa_value_rub, weekly_rub=wkl,
                counter_ids=[counter_id] if counter_id else [],
                pay_for_conversion=(mode == "network_payconv"),
                callout_ids=a["callout_ids"], sitelink_set_id=slset,
                promo_id=(a["promos"][0] if a["promos"] else None),
                minus_set_ids=None, grid_cookie=grid_cookie)
            result_d["rsya_finalized"] = True
            result_d["callouts_set"] = len(a["callout_ids"])
            result_d["sitelink_set_id"] = slset
        except Exception as _fe:  # noqa: BLE001 — Grid-ошибка не удаляет кампанию (она уже ok)
            result_d["rsya_finalized"] = False
            result_d["finalize_warn"] = f"Grid-финализация (ассеты) не прошла: {str(_fe)[:200]}"
        return result_d
    except cmc.DirectV501Error as e:
        if campaign_id:
            return _cleanup_partial(str(e))
        return {"ok": False, "name": name, "error": str(e)[:240]}
    except Exception as e:  # noqa: BLE001
        if campaign_id:
            return _cleanup_partial(str(e))
        return {"ok": False, "name": name, "error": str(e)[:240]}

def _create_tp1_campaign(
    token: str,
    login: str,
    name: str,
    counter_id: int,
    goal_id: int,
    cpc_cpa: int,
    region_ids: list,
    href: str,
    slepok: str,
    site_type: str,
    r_code: str,
    titles: list | None,
    texts: list,
    feed_id: int = 0,
    with_shopping: bool = False,
    feed_models: dict | None = None,
    budget_rub: int = 0,
    segment: str | None = None,
    ai_title2: str = "",
    sitelinks: list | None = None,
    callout_texts: list | None = None,
    callout_ids: list | None = None,
    city: str = "",
    autotarget: bool = False,
    products_only: bool = False,
    no_cpa: bool = False,
    grid_cookie: str | None = None,
    job=None,
) -> dict:
    """Создать ПАРУ кампаний tp1 (РСЯ): cpc-вариант (AVERAGE_CPA) + cpa-вариант (PAY_FOR_CONVERSION).

    no_cpa=True (галочка «под стиль сайта» снята) → создаём ТОЛЬКО cpc-вариант (без оплаты за конверсии).

    segment ('Марки'|'Модели'|None) — какие ct-группы класть в обе кампании пары.

    Канон CODER.md: каждый текстовый tp = ПАРА кампаний (cpc + cpa).
    - tp1_cpc_site: mode='network_cpa'     (Network=AVERAGE_CPA, оплата за клики)
    - tp1_cpa_site: mode='network_payconv' (Network=PAY_FOR_CONVERSION, оплата за конверсии)

    Имя кампании (аргумент name) интерпретируется как канон cpc-варианта:
      'tp1_cpc_site — РСЯ - {cat} - {targ}'
    cpa-вариант получает то же имя с заменой 'tp1_cpc_site' → 'tp1_cpa_site'.

    Группы из пака M3 наполняются в обе кампании (общий slepok/site_type).

    Возвращает {"ok": True, "campaigns": [cpc_result, cpa_result]} или {"ok": False, ...}.
    """
    # Генерим имя cpa-кампании из cpc: замена суффикса оплаты в кодере
    name_cpa = name.replace("tp1_cpc_site", "tp1_cpa_site", 1)

    cpc_result = _create_tp1_single(
        token=token, login=login, name=name, counter_id=counter_id,
        goal_id=goal_id, cpa_value_rub=cpc_cpa, mode="network_cpa",
        region_ids=region_ids, href=href, slepok=slepok,
        site_type=site_type, r_code=r_code, titles=titles, texts=texts,
        feed_id=feed_id, with_shopping=with_shopping, feed_models=feed_models,
        budget_rub=budget_rub, segment=segment, city=city,
        ai_title2=ai_title2, sitelinks=sitelinks,
        callout_texts=callout_texts, callout_ids=callout_ids,
        autotarget=autotarget, products_only=products_only,
        grid_cookie=grid_cookie,
    )
    cpa_result = None
    # no_cpa → пропускаем вариант оплаты за конверсии; отмена → cpa тоже пропускаем (cpc уже достроен).
    if not no_cpa and not (job and job.get("cancel")):
        cpa_result = _create_tp1_single(
            token=token, login=login, name=name_cpa, counter_id=counter_id,
            goal_id=goal_id, cpa_value_rub=cpc_cpa, mode="network_payconv",
            region_ids=region_ids, href=href, slepok=slepok,
            site_type=site_type, r_code=r_code, titles=titles, texts=texts,
            feed_id=feed_id, with_shopping=with_shopping, feed_models=feed_models,
            budget_rub=budget_rub, segment=segment, city=city,
            ai_title2=ai_title2, sitelinks=sitelinks,
            callout_texts=callout_texts, callout_ids=callout_ids,
            autotarget=autotarget, products_only=products_only,
            grid_cookie=grid_cookie,
        )
    # Сводный результат: ok=True если хоть одна создалась
    ok = cpc_result.get("ok") or (bool(cpa_result) and cpa_result.get("ok"))
    # Обратная совместимость с api_create_set: возвращаем campaign_id первой созданной
    first_id = cpc_result.get("campaign_id") or (cpa_result.get("campaign_id") if cpa_result else None)
    out = {
        "ok": ok, "name": name, "campaign_id": first_id,
        "launched": False,
        "campaigns": [cpc_result] + ([cpa_result] if cpa_result else []),
        "url": (cpc_result.get("url") or (cpa_result.get("url") if cpa_result else "") or ""),
    }
    if not ok:
        # Обе кампании пары упали → поднимаем РЕАЛЬНУЮ причину наверх (иначе UI показывает пустое «()»).
        _errs = [c.get("error") for c in out["campaigns"] if c and c.get("error")]
        out["error"] = ("; ".join(dict.fromkeys(_errs))[:240]
                        or "tp1: кампании пары не создались (причина не определена)")
    return out

def _grid_account_image_hashes(login: str) -> dict:
    """{image_name: imageHash} картинок, УЖЕ загруженных в аккаунт — читается ПО КУКЕ через Grid
    (БЕЗ баллов). Name = basename файла M3 (upload_image кладёт Name=os.path.basename(path)).
    Нужно куки-пути РСЯ (tp1): при 0 баллов залить НОВУЮ картинку нельзя (adimages.add → 152),
    но ПЕРЕИСПОЛЬЗОВАТЬ хэш уже залитой (предыдущими v5-созданиями) — можно. Покрытие растёт по
    мере «созревания» аккаунта. Мягкая деградация: нет куки/ошибка → {} (создаём без картинок)."""
    import requests as _rqs
    import re as _re
    try:
        cookie = cmc.pick_working_cookie(login)
    except Exception:  # noqa: BLE001
        return {}
    if not cookie:
        return {}
    sess = _rqs.Session()
    sess.verify = False
    csrf = {"t": None}

    def _g(op, q, var):
        h = {"Cookie": cookie, "dna-operation-name": op, "x-direct-api": "1", "x-detected-locale": "ru",
             "Content-Type": "application/json", "User-Agent": cmc.USER_AGENT, "Origin": "https://direct.yandex.ru",
             "Referer": f"https://direct.yandex.ru/dna/grid/campaigns?ulogin={login}"}
        if csrf["t"]:
            h["x-csrf-token"] = csrf["t"]
        r = sess.post(f"{_GRID_URL}?operationName={op}&ulogin={login}",
                      json={"operationName": op, "query": q, "variables": var}, headers=h, timeout=40)
        if r.status_code == 403:
            m = _re.search(r"_direct_csrf_token=([^;,\s]+)", r.headers.get("Set-Cookie", ""))
            t = r.cookies.get("_direct_csrf_token") or (m.group(1) if m else None)
            if t:
                csrf["t"] = t
                r = sess.post(f"{_GRID_URL}?operationName={op}&ulogin={login}",
                              json={"operationName": op, "query": q, "variables": var}, headers=h, timeout=40)
        return r

    try:
        _g("Callouts", "query Callouts($login:String!){callouts(input:{searchBy:{login:$login}"
           "filter:{deleted:false}}){id}}", {"login": login})
        camp_ids = [c["id"] for c in _grid_list_campaigns(login) if c.get("id")]
    except Exception:  # noqa: BLE001
        return {}
    A = ("query A($login:String!,$inp:GdAdsContainerInput!){client(searchBy:{login:$login}){"
         "ads(input:$inp){rowset{id ...on GdAdaptiveTextAd{images{imageHash name}}}}}}")
    out: dict = {}
    for i in range(0, len(camp_ids), 100):
        inp = {"filter": {"campaignIdIn": [str(x) for x in camp_ids[i:i + 100]]},
               "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
               "limitOffset": {"limit": 5000, "offset": 0}, "orderBy": [{"order": "ASC", "field": "ID"}]}
        try:
            d = _g("A", A, {"login": login, "inp": inp}).json()
        except Exception:  # noqa: BLE001
            continue
        if d.get("errors"):
            continue
        for ad in (((d.get("data") or {}).get("client") or {}).get("ads") or {}).get("rowset") or []:
            for im in (ad.get("images") or []):
                if im.get("name") and im.get("imageHash"):
                    out.setdefault(im["name"], im["imageHash"])
    return out

def _tp1_pack_groups(login: str, slepok: str, site_type: str, r_code: str, href: str,
                     titles: list | None, texts: list,
                     segment: str | None = None, ai_title2: str = "", city: str = "",
                     with_shopping: bool = False, tp_code: str = "tp1",
                     image_map: dict | None = None, autotarget: bool = False,
                     feed_url_by_model: dict | None = None) -> list:
    """Бренд-группы tp1/tp5 из пака M3 — ЧИСТО данные (без API-вызовов, без баллов). Зеркало
    группо-сборки _build_tp1_from_pack (см. там), вынесено для куки-пути (grid_create.create_full).
    image_map (РСЯ tp1): {basename→imageHash} уже залитых картинок аккаунта — переиспользуем хэши
    (картинку при 0 баллов залить нельзя). Источник картинок — как в v5 (_build_tp1_from_pack:
    read_slepok_images ∥ read_images), basename матчим с image_map.
    → [{name, ct, brand, keywords, minus, titles, texts, href[, image_hashes]}]."""
    import os as _os
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    # #3 (решение Семёна): tp4 = те же кампании, что tp2 (отличие — только галочка «Динамика»). Пак
    # tp4 беднее (были группы с 1 ключом) → ИСТОЧНИК ГРУПП/КЛЮЧЕЙ для tp4 берём из tp2-пака. Алиас
    # касается ТОЛЬКО `kp.gather`; место показа (organic=True), нейминг/кодер, тип Поиск+Динамика,
    # корректировки и контент tp4 остаются tp4 (ниже `tp_code` не подменяется).
    _pack_tp = "tp2" if tp_code == "tp4" else tp_code
    pack = kp.gather(key, site_type, _pack_tp)
    if not pack:
        return []
    text0 = (texts[0] if texts else "")[:81] or "Официальный дилер. Тест-драйв и выгодные условия по кредиту. Авто в наличии."
    ct_model = kp.feeds_ct_model()
    ct_name = _ag_part1_map()
    _sc_titles = _slepok_campaign_content(slepok, site_type).get("titles") or []  # пул слепка — 1 раз
    groups = []
    for ct in sorted(pack.keys()):
        data = pack.get(ct) or {}
        if not data.get("positive"):
            continue
        if segment and _ct_segment(ct) != segment:
            continue
        raw_brand = ct_name.get(ct) or ct_model.get(ct) or ct
        brand = _valid_pack_brand_name(ct, raw_brand)
        group_label = _pack_group_display_name(ct, raw_brand, brand)
        # tp2/tp4 — поисковые группы: в кодере используем aoff, не сетевой tp1-формат.
        _is_search_tp = tp_code in ("tp2", "tp4")
        group_name = (_text_group_name(ct, r_code, group_label)
                      if _is_search_tp
                      else _tp1_group_name(ct, r_code, group_label, with_shopping=with_shopping,
                                           autotarget=autotarget))
        # deep-link: сначала реальный URL из фида, фолбэк на формульный слаг (#ФИКС-2).
        # ФИКС A: Марки → /auto/{brand} (первые 2 сегмента), Модели → полный путь без query. (#ФИКС-A)
        _raw_feed_url = (_feed_url_for_model(feed_url_by_model, brand) if feed_url_by_model else None)
        if _raw_feed_url:
            model_href = (_brand_level_url(_raw_feed_url) if _ct_segment(ct) == "Марки"
                          else _strip_url_query(_raw_feed_url))
        else:
            model_href = _model_page_href(href, site_type, brand)
        is_brand_group = _ct_segment(ct) in ("Марки", "Модели")
        title = (_title_from_template(brand, city) if (is_brand_group and not ai_title2)
                 else (_GENERIC_AT_TITLES[0] if not is_brand_group else brand[:35]))
        ttl2 = (ai_title2[:30] if ai_title2 else _next_title2())
        # Cookie/Grid-путь не должен делать M3-вызов на каждую ct-группу: это и было источником
        # зависания боевого create_set после restart. ИИ остаётся на уровне item, а группа берёт
        # локально собранный набор в том же стиле.
        _gt = _rsya_titles(brand, city, site_type, ai_title2=ai_title2,
                           base=(list(titles or []) + [title, ttl2] if is_brand_group
                                 else list(titles or []) + list(_GENERIC_AT_TITLES)),
                           pool=_sc_titles, is_brand=is_brand_group)
        _gx = _rsya_texts([t for t in (list(texts or []) + ([text0] if text0 else [])) if t], site_type, city, brand)
        _gt, _gx, _sl_dummy, _pay_changed = _coherent_payments(_gt, _gx, [])
        g = {
            "name": group_name, "ct": ct, "brand": brand, "seg": _ct_segment(ct),  # 'Марки' → цена=МИН по марке
            # БАГ-13: для «Марки» — убрать ключи «марка+модель»
            "keywords": _filter_group_keywords(data.get("positive", []), _ct_segment(ct), brand, city, site_type),
            "minus": data.get("minus", []),
            "titles": _gt or [t for t in ([title, brand] if brand else [title]) if t],
            "texts": _gx or ([text0] if text0 else []),
            "href": model_href,
        }
        _all_imgs = _creative_images_for_ct(site_type, tp_code, ct, key)
        if _all_imgs:
            g["image_paths"] = _all_imgs[:5]
        # РСЯ-картинки по куке (БЕЗ баллов): источник — пак M3 + Manual-добивка.
        # basename → hash из image_map (уже залитые в аккаунт, без баллов). Найденные → imageHashes.
        # Fallback по другим слепкам для того же ct (не менять ct — ct0000 ЗАПРЕЩЁН).
        if image_map:
            _hh = [image_map.get(_os.path.basename(p)) for p in _all_imgs]
            _hh = [h for h in _hh if h]
            if _hh:
                g["image_hashes"] = _hh[:5]
        groups.append(g)
    return groups

def _pack_groups_with_retry(login: str, slepok: str, site_type: str, r_code: str, href: str,
                            titles, texts, *, retries: int = 2, **kw) -> tuple[list, bool]:
    """`_tp1_pack_groups` с КОРОТКИМИ ретраями (M3-пак мог быть ВРЕМЕННО недоступен — sshfs/relay).
    Пустой пак больше НЕ повод для мгновенного permanent-fail. Бюджет ОГРАНИЧЕН: это вызывается и на
    СИНХРОННОМ route /api/create_set — длинные sleep вешали бы запрос. Worst-case ~0.5с sleep + ~3с
    статус M3. → (groups, m3_alive); m3_alive=False → пак пуст И M3 лежит → caller отправит в deferred."""
    groups: list = []
    for _i in range(max(1, int(retries))):
        try:
            groups = _tp1_pack_groups(login, slepok, site_type, r_code, href, titles, texts, **kw)
        except Exception as _e:  # noqa: BLE001 — сбой чтения пака считаем как «пусто», ретраим
            groups = []
        if groups:
            return groups, True
        if _i < retries - 1:
            time.sleep(0.5)                               # короткий backoff (не вешать sync-route)
    # Пусто после ретраев — жив ли M3 (единый источник правды о статусе)? Логируем для диагностики.
    try:
        _m3 = _m3_content_status(timeout=3.0)
    except Exception:  # noqa: BLE001
        _m3 = {"ok": False, "detail": "статус M3 не прочитан"}
    _alive = bool(_m3.get("ok"))
    print(f"[pack-empty] slepok={slepok} site_type={site_type} tp_retry={retries} "
          f"M3_alive={_alive} detail={_m3.get('detail')}", flush=True)
    return [], _alive

def _create_tp1_via_cookie(
    login: str, name: str, counter_id: int, goal_id: int, cpc_cpa: int,
    region_ids: list, href: str, slepok: str, site_type: str, r_code: str,
    titles: list | None, texts: list, budget_rub: int = 0, segment: str | None = None,
    ai_title2: str = "", city: str = "", autotarget: bool = False, no_cpa: bool = False,
    token: str = "", corr: dict | None = None, ret_map: dict | None = None,
    callout_texts: list | None = None, sitelinks: list | None = None,
    callout_ids: list | None = None,
    feed_id: int = 0, with_shopping: bool = False, feed_models: dict | None = None,
    job=None,
) -> dict:
    """tp1 РСЯ ПО КУКЕ (без баллов v5) — когда исчерпан лимит (152) и пользователь согласился через
    попап. Кампания+группы+комбинаторные объявления через grid_create.create_full.
    При наличии фида добиваем ShoppingAd+ListingAd через Grid, как и на token-path.
    → {"ok", "campaign_id", "campaigns":[...], "via":"cookie"} (форма как у _create_tp1_campaign)."""
    import datetime as _dt
    # РСЯ-картинки по куке: переиспользуем хэши уже залитых в аккаунт картинок (basename→hash).
    # При 0 баллов залить новую нельзя (adimages.add=152), но reuse — без баллов. Best-effort: {} → без картинок.
    _img_map = _grid_account_image_hashes(login)
    # URL страниц моделей: account-level мёрж (все фиды, как цены) — покрывает марки без URL
    # в конкретном feed_id (#ФИКС-8).
    _feed_url_map = _account_offer_urls(login, href)
    groups, _m3_alive = _pack_groups_with_retry(login, slepok, site_type, r_code, href, titles, texts,
                                                segment=segment, ai_title2=ai_title2, city=city, tp_code="tp1",
                                                image_map=_img_map, autotarget=autotarget,
                                                with_shopping=with_shopping,
                                                feed_url_by_model=_feed_url_map or None)
    if not groups:
        seg_note = f", segment={segment}" if segment else ""
        # Пак пуст после ретраев → НЕ permanent-fail: помечаем defer (пункт уйдёт на отложенную
        # докрутку позже, когда M3/пак восстановится), а не считаем окончательной ошибкой.
        return {"ok": False, "defer": True, "name": name,
                "error": (f"tp1(куки): пак M3 пуст/недоступен (M3_alive={_m3_alive}) для "
                          f"slepok={slepok}, site_type={site_type}, tp=tp1{seg_note} → отложено на докрутку")}
    start_date = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=3))).strftime("%Y-%m-%d")  # МСК
    wkl = int(budget_rub) if budget_rub else int(cpc_cpa) * 10
    name_cpa = name.replace("tp1_cpc_site", "tp1_cpa_site", 1)
    variants = [(name, "network_cpa", False)]
    if not no_cpa:
        variants.append((name_cpa, "network_payconv", True))
    # ЦЕНА из фида в комбинаторное по куке (как v5 Фаза 3.5): adPrice по бренду группы. Без баллов.
    # Раньше куки-путь цены не ставил вовсе (price_map не прокидывался). Best-effort: {} → без цен.
    try:
        _price_map = _account_offer_prices(login, href)   # цены из предпочтительных фидов (чистые имена)
    except Exception:  # noqa: BLE001
        _price_map = {}
    # Ассеты кампании (уточнения/быстрые ссылки/промо) — чтобы кампания была ДОЗАПОЛНЕНА как на v5-пути.
    # Грузим один раз; v5-GET'ы и Grid-докрутка баллов НЕ стоят (units тратят только add/update РК/объяв).
    # БАГ-1 FIX: ассеты загружаем ВСЕГДА при наличии токена, не только при goal_id.
    # Grid принимает goalId="0" (проверено live 2026-06-24): кампания обновляется, callouts/sitelinks ставятся.
    _ai_sitelinks = sitelinks or _ai_common_sitelinks(login, slepok, site_type, city, "tp1")
    # ФИКС B: Сайтлинки → href первой брендовой группы, а не базовый сайт. Cookie-путь создаёт
    # сайтлинки на уровне кампании (gc.create_full не поддерживает per-group). Берём первую
    # группу с не-базовым href как представителя. Для полноценных per-group сайтлинков нужен
    # рефакторинг gc.create_full (намеренно не трогается). (#ФИКС-B)
    _sl_href = next(
        (g["href"] for g in groups if g.get("href") and g["href"] != href.rstrip("/")),
        href
    )
    _assets = _resolve_campaign_assets(
        token, login, _sl_href, sitelinks=_ai_sitelinks,
        slepok=slepok, site_type=site_type, prefer_callout_texts=callout_texts,
        prefer_callout_ids=callout_ids)
    _slset = _assets.get("sitelink_set_id")
    _mp_disabled = _enabled_minus_places()                   # #21 минус-площадки РСЯ (1 раз на аккаунт)
    out_campaigns = []
    for nm, _mode, pay_conv in variants:
        if job and job.get("cancel"):                        # отмена: стоп ПЕРЕД cpa-вариантом пары
            break                                             # (cpc уже создан/дозаполнен)
        spec = {"name": nm, "counter_id": counter_id or 0, "goal_id": goal_id or 0,
                "cpa": int(cpc_cpa), "weekly_budget": wkl, "start_date": start_date,
                "network": True, "search": False, "pay_for_conversion": pay_conv,
                "disabled_places": _mp_disabled}             # #21 → build_unified_campaign.disabledPlaces
        try:
            rep = gc.create_full(login, campaign_spec=spec, groups=groups,
                                 region_ids=region_ids, href=href, goal_id=goal_id or 0,
                                 autotargeting=bool(autotarget),
                                 price_map=_price_map, brand_price_fn=_group_ad_price)
            cid = rep.get("campaign_id")
            ok = bool(cid) and bool(rep.get("ads")) and not (rep.get("errors") and not rep.get("groups"))
            if cid and not rep.get("ads"):
                if not rep.get("errors"):                     # ДИАГНОСТИКА: add_ads вернул пусто БЕЗ исключения
                    rep.setdefault("errors", []).append(
                        f"объявления(куки): 0 TextAd (groups={rep.get('groups')}, "
                        f"adgroup_ids={rep.get('adgroup_ids')}) — add_ads вернул пусто без ошибки Grid")
                print(f"[tp1-cookie] {nm}: 0 ads groups={rep.get('groups')} feed={feed_id} errs={rep.get('errors')}", flush=True)
                try:
                    gc.GridCreateClient(login).delete_campaigns([cid])
                except Exception:  # noqa: BLE001
                    pass
                out_campaigns.append({
                    "ok": False, "name": nm, "campaign_id": cid, "launched": False,
                    "via": "cookie",
                    "tp1_build": {"groups": rep.get("groups"), "ads": rep.get("ads"),
                                  "errors": rep.get("errors", [])[:5]},
                    "url": (f"https://direct.yandex.ru/dna/campaign/{cid}?ulogin={login}" if cid else ""),
                    "error": "tp1(куки): partial-кампания удалена — объявления не созданы",
                    "partial_deleted": True,
                })
                continue
            _shop_ids: list[int] = []
            _listing_ids: list[int] = []
            if ok and with_shopping and feed_id:
                _grid_shop_items = []
                # ДВА фильтра по типу (решение Семёна, HAR36): Товары → vendor [марка]; Страницы каталога
                # → name [марка|марка+модель]. ct0000 без марки → без фильтра. (Коллекции фида для
                # collectionId БОЛЬШЕ НЕ нужны — товары на vendor, листинг на name.)
                _shop_name_vals = []   # параллельно _grid_shop_items: name-значение листинга на группу
                for _grp, _agid in zip(groups, rep.get("adgroup_ids") or []):
                    if not _agid:
                        continue
                    _g_brand = (_grp.get("brand") or "").strip()
                    _g_seg = _ct_segment(_grp.get("ct") or "")
                    # Фильтр валиден ТОЛЬКО для брендовых групп («Марки»/«Модели»). «Общее» (тема в
                    # brand: «Автокредит»/«Trade-in»/«Авито») → без фильтра: товары по всему фиду, каталог — все стр.
                    _is_brand_seg = _g_seg in ("Марки", "Модели")
                    _vendor = _vendor_value(_g_brand) if (_g_brand and _is_brand_seg) else None     # товары: vendor [марка]
                    _name_val = _listing_name_value(_g_brand, _g_seg) if (_g_brand and _is_brand_seg) else None  # листинг: name
                    _model_vals = _model_field_values(_g_brand, _g_seg) if (_g_brand and _is_brand_seg) else []  # Модели → +model
                    _grid_shop_items.append({
                        "adgroup_id": int(_agid),
                        "feed_id": int(feed_id),
                        "vendor": _vendor,
                        "collection_id": None,
                        "model": _model_vals,
                        "name": _grp.get("name", "?"),
                    })
                    _shop_name_vals.append(_name_val)
                if _grid_shop_items:
                    try:
                        _gcl_shop = gf.GridClient(login)
                        _add_ids = _gcl_shop.add_shopping_ads(_grid_shop_items) or []
                        _shop_ids = [int(x) for x in _add_ids if x]
                        # карта shopping_ad_id → name_value (для name-фильтра листинга)
                        _name_by_shop = {}
                        for _ai, _raw in enumerate(_add_ids):
                            if _raw and _ai < len(_shop_name_vals) and _shop_name_vals[_ai]:
                                _name_by_shop[int(_raw)] = _shop_name_vals[_ai]
                        if _shop_ids:
                            _default_text = ((texts[0] if texts else "")[:81]
                                             or "Официальный дилер. Тест-драйв и выгодные условия по кредиту. Авто в наличии.")
                            _shop_filters = {}
                            for _sid, _src in zip(_shop_ids, [s for s in _grid_shop_items]):
                                _conds = []
                                if _src.get("vendor"):
                                    _conds.append({"field": "vendor", "operator": "CONTAINS_ANY",
                                                   "stringValue": json.dumps(_vendor_filter_values(_src["vendor"]), ensure_ascii=False)})
                                if _src.get("model"):
                                    _mvals = _src["model"] if isinstance(_src["model"], list) else [str(_src["model"])]
                                    _mvals = [str(x) for x in _mvals if str(x).strip()]
                                    if _mvals:
                                        _conds.append({"field": "model", "operator": "CONTAINS_ANY",
                                                       "stringValue": json.dumps(_mvals, ensure_ascii=False)})
                                if _conds:
                                    _shop_filters[int(_sid)] = {"tab": "CONDITION", "conditions": _conds}
                            # G review: set_default_text в СВОЁМ try — падение (Яндекс 500) НЕ блокирует
                            # создание листингов ниже (раньше оба в одном try → текст падал → 0 ListingAd).
                            try:
                                _gcl_shop.set_default_text(
                                    _shop_ids, int(feed_id), _default_text,
                                    filters_by_ad_id=_shop_filters,
                                )
                            except Exception as _dte:  # noqa: BLE001
                                rep.setdefault("warnings", []).append(f"shopping text(куки): {str(_dte)[:140]}")
                            # #ФИКС-1(v2): adGroupId→name_val НАПРЯМУЮ из параллельных массивов —
                            # БЕЗ _add_ids. При частичном создании (len(_add_ids)<len(items))
                            # старая индексная адресация через enumerate(_add_ids) давала смещение:
                            # _shop_name_vals[i] уходило на _grid_shop_items[i] чужой марки.
                            # adGroupId в items надёжен (группа создана ДО add_shopping_ads).
                            _agid_to_nv2 = {}
                            for _gsi2, _nv2 in zip(_grid_shop_items, _shop_name_vals):
                                if _nv2 and isinstance(_gsi2, dict):
                                    _gi2 = _gsi2.get("adgroup_id")
                                    if _gi2:
                                        _agid_to_nv2[str(_gi2)] = _nv2
                            _listing_rows = (_gcl_shop.add_listing_ads_by_shopping_ads(_shop_ids) or [])
                            _listing_ids = []
                            _lf_items = []
                            for _idx2, _row in enumerate(_listing_rows):
                                try:
                                    _lid = _row.get("id") if isinstance(_row, dict) else _row
                                    _agid = str(_row.get("adGroupId") or "") if isinstance(_row, dict) else ""
                                    if _lid:
                                        _listing_ids.append(int(_lid))
                                    _val = _agid_to_nv2.get(_agid)
                                    if not _val and _idx2 < len(_shop_name_vals):
                                        _val = _shop_name_vals[_idx2]
                                    if _lid and _val:
                                        _lf_items.append({"id": _lid, "feed_id": int(feed_id),
                                                          "value": _val, "bodies": [_default_text]})
                                except Exception:  # noqa: BLE001
                                    continue
                            if not _lf_items and _agid_to_nv2:
                                # saveDraft:True → addedAds пуст; строим по adGroupId (фильтр ставится на группу)
                                for _agid_s, _val in _agid_to_nv2.items():
                                    _lf_items.append({"adgroup_id": _agid_s, "feed_id": int(feed_id),
                                                      "value": _val, "bodies": [_default_text]})
                            # name-фильтр «Страницы каталога» (HAR36; by-shopping фильтр не наследует)
                            if _lf_items:
                                try:
                                    rep["listing_name_set"] = _gcl_shop.set_listing_name_filters(_lf_items)
                                except Exception as _lfe:  # noqa: BLE001
                                    rep["errors"].append(f"listing name-filter(куки): {str(_lfe)[:140]}")
                    except Exception as _shop_exc:  # noqa: BLE001
                        rep["errors"].append(f"shopping/listing(куки): {str(_shop_exc)[:160]}")
                if not _shop_ids:
                    # Bug2 graceful (как v5-путь / tp7 whole-feed fallback): фид без офферов
                    # (напр. лендинг-фид) → НЕ удаляем кампанию — в ней валидные TextAd-группы РСЯ.
                    # Диагностику пишем в WARNINGS (не errors!), чтобы выжившая ok=True кампания не
                    # показывала ложную «ошибку» в карточке — консистентно с v5-путём. (#1 review)
                    rep.setdefault("warnings", []).append(
                        "товарка(куки): 0 ShoppingAd — фид без офферов; оставлена РСЯ без товарных")
                    print(f"[tp1-cookie] {nm}: ShoppingAd=0 feed={feed_id} (graceful, РСЯ без товарки)", flush=True)
                elif not _listing_ids:
                    # Bug2 graceful: ShoppingAd есть, листинги пусты (фид-каталог без готовых офферов)
                    # → НЕ удаляем кампанию, оставляем товарку без листингов. Диагностика → warnings.
                    rep.setdefault("warnings", []).append(
                        f"листинги(куки): 0 ListingAd из {len(_shop_ids)} ShoppingAd (feed={feed_id}) — "
                        "0 ListingAd из by-shopping — оставлена товарка без листингов")
                    print(f"[tp1-cookie] {nm}: ListingAd=0 shop={len(_shop_ids)} feed={feed_id} (graceful)", flush=True)
            # Grid-докрутка РСЯ: уточнения/быстрые ссылки/промо на уровне кампании (без баллов).
            # БАГ-1 FIX: вызываем ВСЕГДА при ok+cid, не только при goal_id.
            # Grid принимает goalId="0" без ошибки (verified live 2026-06-24): ассеты ставятся корректно.
            _fin = None
            if ok and cid:
                try:
                    # Корректировки «Глобальных правил» через Grid (HAR21, без баллов) — campaignId
                    # ЭТОЙ кампании. v5 bidmodifiers.add тут недоступен (152), поэтому Grid.
                    _bm = _grid_bid_modifiers(cid, corr or {}, ret_map or {})
                    _finalize_rsya(
                        login, cid, name=nm, goal_id=goal_id or 0, cpa_rub=cpc_cpa, weekly_rub=wkl,
                        counter_ids=[counter_id] if counter_id else [],
                        pay_for_conversion=pay_conv,
                        callout_ids=_assets.get("callout_ids"), sitelink_set_id=_slset,
                        promo_id=(_assets["promos"][0] if _assets.get("promos") else None),
                        minus_set_ids=None, bid_modifiers=_bm, disabled_places=_mp_disabled)
                    _fin = {"callouts": len(_assets.get("callout_ids") or []),
                            "sitelink_set": _slset, "promo": bool(_assets.get("promos")),
                            "corrections": len((_bm.get("bidModifierRetargeting") or {}).get("adjustments") or [])}
                    if token:
                        _v5_mods, _v5_mod_err = _apply_corrections(token, login, cid, corr or {}, ret_map or {})
                        _fin["v5_corrections"] = _v5_mods
                        if _v5_mod_err:
                            _fin["v5_corrections_error"] = _v5_mod_err[:160]
                    # demographic (age/gender) теперь через Grid (bidModifierDemographics, HAR23/JS реверс).
                    # _grid_bid_modifiers уже включил их в _bm → _finalize_rsya применила Grid-ом.
                    _fin["demographic_corrections"] = len((_bm.get("bidModifierDemographics") or {}).get("adjustments") or [])
                except Exception as _fe:  # noqa: BLE001
                    _fin = {"error": str(_fe)[:160]}
                # ── Картинки для РСЯ-объявлений (новые аккаунты без истории) ─────────────
                # Если reuse image_hashes не сработал (новый аккаунт) — пробуем довесить картинки
                # ПО ГРУППАМ, чтобы не размазывать один и тот же хэш на все бренды кампании.
                _ad_ids = rep.get("ad_ids") or []
                if _ad_ids:
                    try:
                        import os as _os2
                        _gc_img = gf.GridClient(login)
                        _uploaded_by_name: dict[str, str] = {}
                        _upd_items = []
                        for _aid, _grp in zip(_ad_ids, groups):
                            _gpaths = _grp.get("image_paths") or []
                            _hashes = list(dict.fromkeys(_grp.get("image_hashes") or []))
                            for _pth in _gpaths:
                                if len(_hashes) >= 5:
                                    break
                                if not _pth or not _os2.path.isfile(_pth):
                                    continue
                                _bn = _os2.path.basename(_pth)
                                _h = _uploaded_by_name.get(_bn)
                                if not _h:
                                    _h = _cached_upload_image(_gc_img, login, _pth)
                                    if _h:
                                        _uploaded_by_name[_bn] = _h
                                if _h and _h not in _hashes:
                                    _hashes.append(_h)
                            _upd = {"id": _aid, "href": _grp.get("href") or href,
                                    "titles": _grp.get("titles") or [],
                                    "bodies": _grp.get("texts") or []}
                            if _hashes:
                                _upd["image_hashes"] = _hashes[:5]
                            _cur, _old = _group_ad_price(
                                _price_map, _grp.get("brand") or _grp.get("name") or "",
                                _grp.get("seg") or _ct_segment(_grp.get("ct") or "")
                            )
                            _ad_price = _grid_ad_price_payload(_cur, _old)
                            if _ad_price:
                                _upd["adPrice"] = _ad_price
                            _upd_items.append(_upd)
                        # Не используем suggest_images: Яндекс может предложить чужую/модельную картинку.
                        # Если своих картинок нет или они запрещены вкладкой «Контент», объявление остаётся без картинки.
                        if _upd_items:
                            _imgs_applied = _grid_update_adaptive_ads(login, _upd_items)
                            if _fin and isinstance(_fin, dict):
                                _fin["ads_repaired"] = _imgs_applied
                                _fin["image_groups"] = sum(1 for _it in _upd_items if _it.get("image_hashes"))
                    except Exception:  # noqa: BLE001 — картинки не критичны
                        pass
            out_campaigns.append({
                "ok": ok, "name": nm, "campaign_id": cid, "launched": False,
                "via": "cookie", "rsya_finalized": _fin,
                "tp1_build": {"groups": rep.get("groups"), "ads": rep.get("ads"),
                              "shopping_ads": len(_shop_ids), "listing_ads": len(_listing_ids),
                              "errors": rep.get("errors", [])[:5],
                              "warnings": rep.get("warnings", [])[:5]},
                "url": (f"https://direct.yandex.ru/dna/campaign/{cid}?ulogin={login}" if cid else ""),
                "error": ("; ".join(rep.get("errors") or [])[:240] if not ok else None),
            })
        except Exception as e:  # noqa: BLE001
            out_campaigns.append({"ok": False, "name": nm, "error": f"tp1(куки): {str(e)[:200]}"})
    ok = any(c.get("ok") for c in out_campaigns)
    first_id = next((c.get("campaign_id") for c in out_campaigns if c.get("campaign_id")), None)
    out = {"ok": ok, "name": name, "campaign_id": first_id, "launched": False,
           "via": "cookie", "campaigns": out_campaigns,
           "url": next((c.get("url") for c in out_campaigns if c.get("url")), "")}
    if not ok:
        _errs = [c.get("error") for c in out_campaigns if c and c.get("error")]
        out["error"] = ("; ".join(dict.fromkeys(_errs))[:240] or "tp1(куки): пара не создалась")
    return out

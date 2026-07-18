"""Create-set live verification and repair helpers extracted from blueprint.py."""

from __future__ import annotations

from .text_norm import _trim_clean

_DEPS: dict = {}


def configure(deps: dict) -> None:
    """Bind blueprint dependencies used by repair helpers."""
    _DEPS.clear()
    _DEPS.update(deps)
    globals().update(deps)


def _create_set_live_verification(login: str, results: list, *, agency: str = "",
                                  use_v5: bool = False,
                                  account_has_promo_library: bool | None = None) -> dict:
    """Read-only live check for create_set results.

    Default path is Grid/cookie-only: this is intentional because Direct API
    units are scarce and UAC tp6/tp7 is not visible in v5 anyway.

    ``account_has_promo_library`` — tri-state признак «в библиотеке аккаунта есть промо»,
    прочитанный в штатном потоке создания (``create_set_promo.attach_or_create_promo``);
    ``None`` (вызов не из потока создания) → верификатор использует свой прокси-фолбэк.
    """
    def _token_getter(_login: str, _agency: str) -> str | None:
        token, _ag = _token_for_login(_login, _agency, _direct_tokens())
        return token

    from .campaign_result import created_campaigns as _created_campaigns
    _created = _created_campaigns(results or [])
    # Сужаем Grid read-back до кампаний ЭТОГО набора: primary-ключ — numeric id (устойчив
    # к переименованию, в т.ч. UAC tp6/tp7). Name-fallback — только для ok-результатов
    # без campaign_id (теоретический edge-case; на практике id всегда присутствует).
    _ok_ids: frozenset[int] = frozenset(
        int(c["id"]) for c in _created if c.get("id") is not None
    )
    _ok_names: frozenset[str] = frozenset(
        c["name"] for c in _created
        if c.get("id") is None and c.get("name")
    )

    def _filtered_grid_getter(_login: str) -> list:
        rows = _grid_list_campaigns(_login)
        if not _ok_ids and not _ok_names:
            # Нет созданных РК с известным id/именем → не фильтруем,
            # верификатор сам выдаст ошибку по пустым results.
            return rows
        out = []
        for r in rows:
            rid_raw = r.get("id")
            try:
                rid = int(rid_raw) if rid_raw not in (None, "") else None
            except (TypeError, ValueError):
                rid = None
            if (rid is not None and rid in _ok_ids) or (r.get("name") and r["name"] in _ok_names):
                out.append(r)
        return out

    return vsvc.verify_create_set_live(
        login,
        results or [],
        agency=agency,
        use_v5=use_v5,
        grid_campaigns_getter=_filtered_grid_getter,
        token_getter=_token_getter,
        account_has_promo_library=account_has_promo_library,
    )

def _create_set_job_context(jid: str) -> tuple[dict, dict, dict, tuple[dict, int] | None]:
    """Load terminal create_set job context from memory/DB for verification/repair endpoints."""
    jid = (jid or "").strip()
    if not jid:
        return {}, {}, {}, ({"error": "job_id обязателен"}, 400)
    with _CREATE_JOBS_LOCK:
        job = dict(_CREATE_JOBS.get(jid) or {})
    if not job:
        job = _job_db_get(jid) or {}
    if not job:
        return {}, {}, {}, ({"error": "job не найдена"}, 404)
    result = rgate.dict_from_jsonish(job.get("result"))
    if not isinstance(result, dict):
        return job, {}, {}, ({"error": "у job нет сохранённого результата для проверки",
                              "status": job.get("status")}, 422)
    ctx = rgate.normalize_job_context(job, result)
    return job, result, ctx, None

def _repair_text_content_context(login: str, ctx: dict, action: dict) -> dict:
    """Build Grid content payload for in-place tp2/tp4 repair."""
    body = ctx.get("body") or {}
    item = action.get("item") if isinstance(action.get("item"), dict) else {}
    acc = _account_ctx(login)
    if not acc:
        raise RuntimeError(f"аккаунт {login} не найден в БД")
    domain = (acc.get("domain") or "").strip()
    if not domain:
        raise RuntimeError("у аккаунта нет домена в БД")
    href = "https://" + domain
    site_type = (body.get("site_type") or "").strip() or acc.get("site_type") or ""
    slepok = (body.get("agent") or "").strip()
    if not slepok:
        raise RuntimeError("в сохранённой job нет выбранного слепка")
    tpl_titles, tpl_texts, _tpl_sitelinks = _templates_for(site_type)
    titles = _lines(item.get("titles")) or tpl_titles
    texts = _lines(item.get("texts")) or tpl_texts
    tp_code = "tp4" if item.get("type") == "search_dynamic" else "tp2"
    r_code, _ = _resolve_region(acc.get("city"))
    body_region_ids = _ints(body.get("region_ids"))
    region_ids = body_region_ids if body_region_ids else (list(acc.get("geoids") or [])
                                                          or [acc.get("geoid") or 225])
    groups, m3_alive = _pack_groups_with_retry(
        login,
        slepok,
        site_type,
        r_code,
        href,
        titles,
        texts,
        segment=item.get("tp4_segment"),   # тот же ключ плана для tp2 И tp4, см. create_set_plan.py
        city=(acc.get("city") or ""),
        tp_code=tp_code,
        image_map={},
        autotarget=bool(item.get("autotarget")),
    )
    if not groups:
        raise RuntimeError(
            f"{tp_code} content-repair: пак M3 пуст/недоступен (M3_alive={m3_alive})"
        )
    goal_id = _num(body.get("goal_id"), 0)
    try:
        price_map = _account_offer_prices(login, href)
    except Exception:  # noqa: BLE001
        price_map = {}
    return {
        "groups": groups,
        "region_ids": region_ids,
        "href": href,
        "goal_id": goal_id,
        "autotargeting": bool(item.get("autotarget")),
        "price_map": price_map,
        "brand_price_fn": _group_ad_price,
    }

def _repair_shopping_content_context(login: str, ctx: dict, action: dict) -> dict:
    """Build Grid shopping payload for in-place tp3/tp5 repair."""
    body = ctx.get("body") or {}
    item = action.get("item") if isinstance(action.get("item"), dict) else {}
    acc = _account_ctx(login)
    if not acc:
        raise RuntimeError(f"аккаунт {login} не найден в БД")
    body_region_ids = _ints(body.get("region_ids"))
    region_ids = body_region_ids if body_region_ids else (list(acc.get("geoids") or [])
                                                          or [acc.get("geoid") or 225])
    agency = (ctx.get("agency") or body.get("agency") or acc.get("agency") or "").strip()
    fid = _num(item.get("feed_id"), 0)
    if not fid:
        rows = _filter_allowed_feed_rows(_grid_feeds(login, agency))
        first = next((f for f in rows if f.get("id")), None)
        fid = int(first["id"]) if first else 0
    if not fid:
        raise RuntimeError("shopping content-repair: нет URL-фида")
    ct = (item.get("ct") or "ct0000").strip()
    r_code, _ = _resolve_region(acc.get("city"))
    group_name = _text_group_name(ct, r_code, "Товарная галерея") if r_code else "Товарная галерея"
    ct_model = kp.feeds_ct_model()
    ct_name = _ag_part1_map()
    raw_brand = ct_name.get(ct) or ct_model.get(ct) or ""
    seg = _ct_segment(ct)
    brand = _valid_pack_brand_name(ct, raw_brand) if raw_brand else ""
    is_brand_seg = seg in ("Марки", "Модели")
    vendor = _vendor_value(brand) if (brand and is_brand_seg) else None
    model_vals = _model_field_values(brand, seg) if (brand and is_brand_seg) else []
    listing_name = _listing_name_value(brand, seg) if (brand and is_brand_seg) else None
    # Д5/Д6 (2026-07-10): единая константа SHOPPING_DEFAULT_TEXT (без кредит/взнос/платёж —
    # запрещено в товарных объявлениях). НЕ берём texts[0] из AI-контента: там может быть
    # кредитный текст или короткий fallback. Fail-safe: texts[0] если импорт упал.
    try:
        from .create_set_assets import SHOPPING_DEFAULT_TEXT as _SDT_R
        body_text = _SDT_R
    except Exception:  # noqa: BLE001
        tpl_titles, tpl_texts, _tpl_sitelinks = _templates_for(
            (body.get("site_type") or "").strip() or acc.get("site_type") or ""
        )
        texts = _lines(item.get("texts")) or tpl_texts
        body_text = _trim_clean(texts[0] if texts else "", 81)
    return {
        "groups": [{
            "name": group_name,
            "vendor": vendor,
            "model": model_vals,
            "listing_name": listing_name,
        }],
        "feed_id": fid,
        "region_ids": region_ids,
        "body_text": body_text,
        "goal_id": _num(body.get("goal_id"), 0),
    }

def _repair_keywords_group_context(login: str, ctx: dict, meta: dict) -> dict:
    """Recompute the correct keyword phrases for ONE existing search adgroup during keyword-repair.

    Mirrors the create-time keyword derivation (_build_text_from_pack): M3 pack for the campaign's
    slepok/site_type/tp → positive phrases for this group's ct → project keyword guard
    (_filter_group_keywords: seg/brand/foreign-city/used-car). Empty result (M3 down or nothing to
    add) is safe — the executor then keeps the group's existing keywords and only fixes autotarget.
    """
    body = ctx.get("body") or {}
    acc = _account_ctx(login)
    if not acc:
        raise RuntimeError(f"аккаунт {login} не найден в БД")
    slepok = (body.get("agent") or "").strip()
    if not slepok:
        raise RuntimeError("в сохранённой job нет выбранного слепка")
    site_type = (body.get("site_type") or "").strip() or acc.get("site_type") or ""
    city = (acc.get("city") or "")
    ct = (meta.get("ct") or "ct0000").strip()
    tp_code = (meta.get("tp_code") or "tp2").strip()
    gather_key = _SLEPOK_KEY.get(slepok.lower(), slepok.lower())
    pack = kp.gather(gather_key, site_type, tp_code)
    pos = (pack.get(ct) or {}).get("positive") or []
    seg = _ct_segment(ct)
    ct_name = _ag_part1_map()
    ct_model = kp.feeds_ct_model()
    raw_brand = ct_name.get(ct) or ct_model.get(ct) or ct
    brand = _valid_pack_brand_name(ct, raw_brand) or "Авто"
    kws = _filter_group_keywords(pos, seg, brand, city, site_type, model=brand)
    return {"keywords": kws, "seg": seg, "brand": brand}

def _attach_post_repair_verification(out: dict, login: str, ctx: dict) -> dict:
    """Attach a Grid-first live report after an in-place repair mutation."""
    try:
        post_live = _create_set_live_verification(
            login,
            ctx.get("results") or [],
            agency=ctx.get("agency") or "",
            use_v5=False,
        )
        out["post_repair_live_verification"] = post_live
        out["remaining_repair_plan"] = (post_live or {}).get("repair_plan")
    except Exception as e:  # noqa: BLE001
        out["post_repair_live_verification_error"] = str(e)[:220]
    return out

def _campaign_images_repair(login: str, campaign_id: int, ctx: dict) -> dict:
    """Пере-залить imageHashes tp1 через RMW + UpdateAdaptiveTextAds (in-place, no Direct units).

    Алгоритм:
    1. groups_for_edit → (adgroup_id, ct) маппинг для tp1-групп
    2. Grid-запрос ads с images{imageHash} → находим объявления без картинок
    3. Для каждого ct: _creative_images_for_ct → пути → _cached_upload_image → imageHash
    4. _grid_update_adaptive_ads(RMW) проставляет imageHashes не трогая titles/bodies/adPrice
    """
    from . import grid_finalize as _gf
    from . import campaign as _cmc
    from . import repair_executor as _rex
    body = ctx.get("body") or {}
    acc = _account_ctx(login)
    if not acc:
        return {"ok": False, "error": f"аккаунт {login} не найден"}
    agency = (ctx.get("agency") or body.get("agency") or acc.get("agency") or "").strip()
    try:
        client = _cmc.build_client(login, account=(agency or None))
        cookie = client.sess.headers.get("Cookie") or ""
        grid = _gf.get_grid_client(login, cookie=cookie)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"кука: {str(e)[:160]}"}

    try:
        groups = grid.groups_for_edit(campaign_id)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"groups_for_edit: {str(e)[:200]}"}

    # Маппинг adgroup_id → ct (только tp1)
    group_ct: dict[int, str] = {}
    for grp in groups:
        m = __import__("re").match(r"^\s*tp(\d+)_", str(grp.get("campaign_name") or ""), __import__("re").I)
        if not m or int(m.group(1)) != 1:
            continue
        gid = int(grp.get("adgroup_id") or 0)
        ct = _rex._ct_of(grp.get("adgroup_name") or "")
        if gid > 0:
            group_ct[gid] = ct

    if not group_ct:
        return {"ok": True, "note": "нет tp1-групп в кампании", "ads_updated": 0}

    # Читаем объявления кампании с images{imageHash} и adGroupId
    try:
        from . import grid_read as _gr
        rc = _gr.GridReadClient(login, cookie=cookie)
        rc._bootstrap_csrf()
        q = ("query AdsRepairImg($login:String!,$inp:GdAdsContainerInput!){"
             "client(searchBy:{login:$login}){ads(input:$inp){rowset{id adGroupId campaignId "
             "__typename ...on GdAdaptiveTextAd{images{imageHash}}}}}}")
        inp = {
            "filter": {"campaignIdIn": [str(campaign_id)]},
            "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
            "limitOffset": {"limit": 5000, "offset": 0},
            "orderBy": [{"order": "ASC", "field": "ID"}],
        }
        data = rc._post("AdsRepairImg", q, {"login": login, "inp": inp})
        ad_rows = ((((data.get("data") or {}).get("client") or {})
                   .get("ads") or {}).get("rowset") or [])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"чтение объявлений: {str(e)[:200]}"}

    ads_need_images: list[dict] = []
    for row in ad_rows:
        # По __typename, НЕ по «images is None»: Grid отдаёт images:null для ГОЛОГО адаптивного
        # объявления (live 03.07: 420/420) — старый идиом пропускал ровно те объявления,
        # которые репейр должен чинить (поэтому добивка картинок «никогда не срабатывала»).
        if row.get("__typename") != "GdAdaptiveTextAd":
            continue
        if any(img.get("imageHash") for img in (row.get("images") or [])):
            continue
        try:
            aid = int(row.get("id"))
            gid = int(row.get("adGroupId") or 0)
        except (TypeError, ValueError):
            continue
        ct = group_ct.get(gid, "ct0000")
        ads_need_images.append({"id": aid, "ct": ct})

    if not ads_need_images:
        return {"ok": True, "note": "все tp1-объявления уже имеют imageHashes", "ads_updated": 0}

    slepok = (body.get("agent") or "").strip()
    if not slepok:
        # Пустой slepok отключает фильтр по тегу в read_slepok_images → в кампанию полились бы
        # картинки ВСЕХ слепков (ревью 03.07, CONFIRMED). Лучше честный отказ, чем чужой контент.
        return {"ok": False, "error": "slepok (body.agent) пуст — не рискуем чужими картинками; "
                                      "запусти аудит с --agent", "no_image_ads": len(ads_need_images)}
    site_type = (body.get("site_type") or acc.get("site_type") or "").strip()
    ct_hashes: dict[str, list[str]] = {}
    # Раздельный учёт двух разных причин «объявление без картинок» (ревью 2026-07-13):
    #  • content_gap_cts — резолвер вернул [] (нет НИ ОДНОГО пути к файлу: пусто в Manual/<ct>,
    #    паке слепка, explicit; cross-слепок fallback выключен) → грузить нечего, это НЕ ошибка.
    #  • upload_fail_cts — пути БЫЛИ, но upload/хеши не получились → реальная ошибка загрузки.
    content_gap_cts: list[str] = []
    upload_fail_cts: list[str] = []
    for ct in {row["ct"] for row in ads_need_images}:
        try:
            paths = _creative_images_for_ct(site_type, "tp1", ct, slepok) or []
        except Exception:  # noqa: BLE001
            paths = []
        if not paths:
            # КОНТЕНТ-ГЭП: нет путей к креативам — это не upload-fail, добивать нечем.
            content_gap_cts.append(ct)
            continue
        # Репейр — сознательная повторная попытка: снимаем 15-мин blacklist ТОЧЕЧНО по путям
        # этой кампании (сброс всего логина ре-армил бы чужие свежезаблэклищенные файлы —
        # ревью 03.07), иначе delayed-repair (T+180с) попадает в окно TTL и получает None.
        try:
            from . import create_set_feeds as _csf_img
            _csf_img._reset_img_fail_cache(login, (paths or [])[:5])
        except Exception:  # noqa: BLE001
            pass
        hashes: list[str] = []
        # Цель — 5 картинок на объявление (как create-путь [:5]); дедуп по хэшу ниже даёт ≤5.
        # Потолок реально ограничен размером пула ct (модельные ct с 4 уник. останутся на 4 —
        # контент-пробел на M3, не код: баг porg-psm5h7q6).
        for path in (paths or [])[:5]:
            try:
                h = _cached_upload_image(grid, login, path)
                # БЕЗ дублей: разные файлы пула бывают одним контентом → Яндекс дедупит в один
                # hash, а дубль в imageHashes валит ВСЁ объявление MUST_NOT_CONTAIN_DUPLICATED_
                # ELEMENTS при updatedAds:[null] (лайв 03.07, camp 712139017 — «ok» без эффекта).
                if h and h not in hashes:
                    hashes.append(h)
            except Exception:  # noqa: BLE001
                pass
        if hashes:
            ct_hashes[ct] = hashes
        else:
            # UPLOAD-FAIL: пути к файлам были, но ни один не дал хеш — реальная ошибка загрузки.
            upload_fail_cts.append(ct)

    items = [
        {"id": ad["id"], "image_hashes": ct_hashes[ad["ct"]]}
        for ad in ads_need_images
        if ct_hashes.get(ad["ct"])
    ]
    if not items:
        # Ни один ct не дал хеши. Разделяем причину: upload-fail (были картинки) — hard-fail;
        # чистый контент-гэп — НЕ ошибка (грузить нечего), ok=True со списком gap-ct, чтобы
        # авто-добивка не считала это провалом и не гоняла reschedule вхолостую.
        if upload_fail_cts:
            return {"ok": False,
                    "error": ("upload-fail: не удалось загрузить картинки для ct "
                              f"{sorted(upload_fail_cts)} (пути есть, хеши не получены)"),
                    "no_image_ads": len(ads_need_images),
                    "content_gap_cts": sorted(content_gap_cts),
                    "upload_fail_cts": sorted(upload_fail_cts)}
        return {"ok": True, "ads_updated": 0, "skipped_content_gap": True,
                "note": ("контент-гэп: нет креативов для ct "
                         f"{sorted(content_gap_cts)} (нужны картинки в Manual/<ct> или паке слепка)"),
                "no_image_ads": len(ads_need_images),
                "content_gap_cts": sorted(content_gap_cts)}
    try:
        updated = _grid_update_adaptive_ads(login, items, campaign_ids=[campaign_id])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"UpdateAdaptiveTextAds: {str(e)[:200]}"}
    # ok=True если для ВСЕХ ct с доступными картинками загрузка прошла (контент-гэп — не провал);
    # ok=False только при реальном upload-fail по ct, где картинки БЫЛИ.
    out = {
        "ok": bool(updated > 0 and not upload_fail_cts), "ads_updated": updated,
        "attempted": len(items), "no_image_ads": len(ads_need_images),
        "content_gap_cts": sorted(content_gap_cts),
        "upload_fail_cts": sorted(upload_fail_cts),
    }
    if upload_fail_cts:
        out["error"] = ("upload-fail: картинки есть, но загрузка не удалась для ct "
                        f"{sorted(upload_fail_cts)}")
    return out


def _campaign_adprice_repair(login: str, campaign_id: int, ctx: dict) -> dict:
    """Проставить adPrice через _grid_set_ad_prices по прайс-кэшу из offer_prices.

    TODO: Полная реализация требует rebuilding price_map из offers + маппинга ad→brand.
    Вызывается только когда NO_ADPRICE_LIVE задетектирован live-verification.
    """
    # Stub: возвращает ok=False с ЯВНЫМ error (не молча). Планировщик/гейт больше НЕ считает
    # adprice_repair исполнимым (repair_gate.executable_adprice_repairs → все в unsupported),
    # поэтому в норме сюда не заходят; ключ error оставлен на случай прямого вызова, чтобы
    # провал не был безмолвным ("ok=False" без причины). Реализацию наполнить после live-теста
    # offer_prices API и определения структуры price_map для repair.
    return {
        "ok": False,
        "error": "adprice_repair не реализован (стаб; нужен rebuild price_map из offer_prices + ad→brand маппинг)",
        "note": "adprice_repair: не реализован (нужен rebuild price_map из offer_prices + ad→brand маппинг)",
        "campaign_id": campaign_id,
    }


def _campaign_default_text_repair(login: str, campaign_id: int, ctx: dict) -> dict:
    """Заполнить bodies ShoppingAd через GridClient.set_default_text.

    TODO: Полная реализация требует чтения shopping_ad_ids кампании + feed_id + body_text.
    Вызывается только когда EMPTY_DEFAULT_TEXT_LIVE задетектирован live-verification.
    """
    # Stub: возвращает ok=False с понятным сообщением.
    # Pipeline замкнут. Реализацию наполнить после live-теста GdSmartAd-читалки в grid_read.
    return {
        "ok": False,
        "note": "default_text_repair: не реализован (нужны shopping_ad_ids + feed_id + text для кампании)",
        "campaign_id": campaign_id,
    }


def _v5_token_for_login(login: str) -> str | None:
    """v5 OAuth-токен по логину (для keywords.delete в keyword-shift-фиксе)."""
    try:
        token, _ = _token_for_login(login, "", _direct_tokens())
        return token
    except Exception:  # noqa: BLE001
        return None


def _repair_deps() -> rex.RepairDeps:
    """Wire blueprint IO helpers into pure repair executors."""
    return rex.RepairDeps(
        account_ctx=_account_ctx,
        promo_content_lines=_promo_content_lines,
        create_account_promo_from_slepok=_create_account_promo_from_slepok,
        dedup_callouts=_dedup_callouts,
        text_content_context=_repair_text_content_context,
        shopping_content_context=_repair_shopping_content_context,
        callout_cap=_CALLOUT_PER_CAMPAIGN_CAP,
        group_keywords_context=_repair_keywords_group_context,
        campaign_images_repair=_campaign_images_repair,
        campaign_adprice_repair=_campaign_adprice_repair,
        campaign_default_text_repair=_campaign_default_text_repair,
        v5_token_for_login=_v5_token_for_login,
    )

def _delete_uac_repair_campaigns(login: str, agency: str, replacements: list[dict]) -> dict:
    """Delete specific incomplete UAC drafts before queued recreate.

    #ФИКС-C3: DRAFT-gate (как у поисковых `_delete_search_draft_campaigns`). Раньше UAC удалялись
    по id+имени tp6_/tp7_ БЕЗ проверки статуса → stale repair-plan / ручной re-run мог снести
    АКТИВНУЮ UAC. Grid `_grid_list_campaigns(login, only_draft=True)` видит скрытые от v5 Мастер/
    Товарку (см. account_service.py) → удаляем ТОЛЬКО те, что реально в DRAFT. Классификация ДО
    любого удаления: если хоть одна не DRAFT / небезопасна / Grid недоступен — не удаляем НИЧЕГО.
    """
    from . import campaign as cmc          # cookie-клиент удаления (был NameError: cmc не импортирован)
    rows = []
    failed = []
    if not replacements:
        return {"deleted": rows, "failed": failed}
    # DRAFT-gate: список UAC-черновиков Grid. Недоступен → консервативный блок (ничего не удаляем).
    try:
        draft_ids = {int(c["id"]) for c in _grid_list_campaigns(login, only_draft=True) if c.get("id")}
    except Exception as e:  # noqa: BLE001
        return {"deleted": [], "failed": [{"campaign_id": 0, "name": "",
                "error": f"Grid draft-list недоступен — UAC-удаление заблокировано: {str(e)[:160]}"}]}
    # Фаза 1: классифицируем ВСЕ (ничего не удаляем). Любой провал → аборт без удаления.
    to_delete: list[dict] = []
    for row in replacements:
        try:
            cid = int(row.get("campaign_id") or 0)
        except (TypeError, ValueError):
            cid = 0
        name = str(row.get("name") or "").strip()
        if cid <= 0 or not name.lower().startswith(("tp6_", "tp7_")) or not _is_tool_campaign(name):
            failed.append({"campaign_id": cid, "name": name, "error": "небезопасное имя UAC для replace"})
            continue
        if cid not in draft_ids:
            failed.append({"campaign_id": cid, "name": name,
                           "error": "UAC не DRAFT (или не видна в Grid) — авто-удаление заблокировано"})
            continue
        to_delete.append({"campaign_id": cid, "name": name, "issue_code": row.get("issue_code")})
    if failed:
        # Хоть одна не прошла gate → не удаляем НИЧЕГО (delete+recreate не атомарны).
        return {"deleted": [], "failed": failed}
    # Фаза 2: все в DRAFT → удаляем.
    client = None
    for it in to_delete:
        cid, name = it["campaign_id"], it["name"]
        try:
            if client is None:
                client = cmc.build_client(login, account=(agency or None))
                client.link_info("https://ya.ru")
            client.delete_campaign(str(cid))
            rows.append({"campaign_id": cid, "name": name, "issue_code": it.get("issue_code")})
        except Exception as e:  # noqa: BLE001
            failed.append({"campaign_id": cid, "name": name, "error": str(e)[:220]})
    return {"deleted": rows, "failed": failed}

def _delete_search_draft_campaigns(login: str, agency: str, delete_items: list[dict]) -> dict:
    """Delete specific ПОИСКОВЫЕ (non-UAC) campaigns before queued recreate.

    Безопасность: удаляем ТОЛЬКО если
    1) имя создано этим инструментом (_is_tool_campaign)
    2) кампания в статусе DRAFT по Grid (primaryStatus='DRAFT')
    Если хотя бы одна кампания NOT DRAFT → возвращаем blocked_non_draft, не удаляем ничего.
    """
    rows: list[dict] = []
    failed: list[dict] = []
    blocked: list[dict] = []
    if not delete_items:
        return {"deleted": rows, "failed": failed, "blocked_non_draft": blocked}
    try:
        draft_ids = {int(c["id"]) for c in _grid_list_campaigns(login, only_draft=True) if c.get("id")}
    except Exception as e:  # noqa: BLE001
        return {"deleted": [], "failed": [], "blocked_non_draft": [],
                "error": f"не удалось получить список черновиков Grid: {str(e)[:200]}"}
    ids_to_delete: list[dict] = []
    for row in delete_items:
        try:
            cid = int(row.get("campaign_id") or 0)
        except (TypeError, ValueError):
            cid = 0
        name = str(row.get("name") or "").strip()
        if cid <= 0 or not _is_tool_campaign(name):
            failed.append({"campaign_id": cid, "name": name, "error": "небезопасное имя для delete"})
            continue
        if cid not in draft_ids:
            blocked.append({"campaign_id": cid, "name": name,
                            "reason": "не DRAFT — авто-удаление заблокировано"})
            continue
        ids_to_delete.append({"campaign_id": cid, "name": name, "issue_code": row.get("issue_code")})
    if blocked:
        # Если хотя бы одна кампания не DRAFT — не удаляем ни одну (консервативно)
        return {"deleted": [], "failed": failed, "blocked_non_draft": blocked}
    if not ids_to_delete:
        return {"deleted": [], "failed": failed, "blocked_non_draft": []}
    # Ре-снимаем draft_ids непосредственно перед удалением: за время классификации выше
    # кампания могла быть опубликована (draft_ids → published; гонка). Удаляем только
    # пересечение с актуальным DRAFT-списком.
    try:
        draft_ids2 = {int(c["id"]) for c in _grid_list_campaigns(login, only_draft=True)
                      if c.get("id")}
    except Exception as _e2:  # noqa: BLE001
        return {"deleted": [], "failed": [], "blocked_non_draft": [],
                "error": f"повторная проверка DRAFT перед удалением упала: {str(_e2)[:200]}"}
    _still_draft: list[dict] = []
    for _r2 in ids_to_delete:
        if int(_r2["campaign_id"]) not in draft_ids2:
            blocked.append({"campaign_id": _r2["campaign_id"], "name": _r2["name"],
                            "reason": "при повторной проверке кампания не DRAFT — удаление заблокировано"})
        else:
            _still_draft.append(_r2)
    if blocked:
        return {"deleted": [], "failed": failed, "blocked_non_draft": blocked}
    ids_to_delete = _still_draft
    if not ids_to_delete:
        return {"deleted": [], "failed": failed, "blocked_non_draft": []}
    try:
        res = gc.GridCreateClient(login).delete_campaigns([r["campaign_id"] for r in ids_to_delete])
        deleted_ids = {int(i) for i in (res.get("deleted") or [])}
        for r in ids_to_delete:
            if int(r["campaign_id"]) in deleted_ids:
                rows.append(r)
            else:
                failed.append({**r, "error": "Grid не подтвердил удаление"})
    except Exception as e:  # noqa: BLE001
        failed.extend([{**r, "error": str(e)[:200]} for r in ids_to_delete])
    return {"deleted": rows, "failed": failed, "blocked_non_draft": []}

def _queue_recreate_repair_job(login: str, ctx: dict, plan: dict, *,
                               parent_job_id: str, saved_session: dict,
                               dedup_login: bool = True) -> dict:
    """Queue resume/recreate repair items without Direct API units."""
    def _create_job(total: int, job_login: str, body: dict, sess: dict, dedup: bool) -> str:
        # recreate/resume = добивка → приоритет: сразу, а не в конец очереди (Семён 2026-07-06)
        return _job_new(total, job_login, body, sess, dedup_login=dedup, priority=True)

    def _ahead(job_id: str) -> int:
        with _CREATE_JOBS_LOCK:
            return _create_jobs_ahead(job_id)

    return rauto.queue_recreate_repair_job(
        login,
        ctx,
        plan or {},
        parent_job_id=parent_job_id,
        saved_session=saved_session,
        delete_uac=_delete_uac_repair_campaigns,
        create_job=_create_job,
        jobs_ahead=_ahead,
        dedup_login=dedup_login,
        delete_search_draft=_delete_search_draft_campaigns,
        units_alive=_DEPS.get("_units_alive_for_login"),   # баллы живы → recreate токеном (не по куке)
    )

def _auto_queue_recreate_after_done(parent_job_id: str, job_snapshot: dict) -> dict | None:
    """Queue async recreate/UAC replace after the parent create job is terminal.

    Draft-only gate: если план содержит requires_campaign_delete (поисковые кампании
    с NO_KEYWORDS_LIVE/WRONG_AUTOTARGET), авто-recreate разрешается ТОЛЬКО когда все
    такие кампании находятся в статусе DRAFT по Grid. Это безопасно: все РК создаются
    с launch=False → всегда DRAFT; авто-удаление боевых кампаний заблокировано.
    """
    req = rauto.auto_recreate_request(parent_job_id, job_snapshot)
    if not req:
        return None

    # Если заблокировано из-за requires_campaign_delete — проверяем: все ли DRAFT?
    if req.get("queued") is False and req.get("requires_explicit_trigger"):
        result = job_snapshot.get("result") if isinstance(job_snapshot.get("result"), dict) else {}
        live = result.get("live_verification") if isinstance(result.get("live_verification"), dict) else {}
        plan = live.get("repair_plan") if isinstance(live.get("repair_plan"), dict) else {}
        delete_items = rgate.executable_recreate_delete_campaigns(plan)
        if delete_items:
            login_for_check = (job_snapshot.get("login") or result.get("login") or "").strip()
            try:
                draft_ids = {
                    int(c["id"])
                    for c in _grid_list_campaigns(login_for_check, only_draft=True)
                    if c.get("id")
                }
                all_draft = all(
                    int(it.get("campaign_id") or 0) in draft_ids for it in delete_items
                )
            except Exception:  # noqa: BLE001
                # Grid недоступен → оставляем блокировку (консервативно)
                return {**req, "draft_gate": "Grid недоступен для проверки статуса — заблокировано"}
            if not all_draft:
                return {**req, "draft_gate": "не все кампании в DRAFT — авто-удаление заблокировано"}
            # Все DRAFT → снимаем блокировку, добавив флаг в body snapshot
            modified_body = {**(job_snapshot.get("body") or {}), "_auto_recreate_with_delete": True}
            modified_snapshot = {**job_snapshot, "body": modified_body}
            req = rauto.auto_recreate_request(parent_job_id, modified_snapshot)
            if not req:
                return None

    if req.get("queued") is False:
        return req
    queued = _queue_recreate_repair_job(
        req["login"],
        req.get("ctx") or {},
        req.get("plan") or {},
        parent_job_id=req.get("parent_job_id") or parent_job_id,
        saved_session=req.get("saved_session") or {},
        dedup_login=True,
    )
    queued["source"] = req.get("source") or "auto_after_done"
    # Помечаем repair_plan как разрешённый в родительском job, чтобы UI не показывал
    # «нужна добавка» после успешной авто-докрутки (stale repair_plan).
    # job_snapshot — shallow dict(j), поэтому мутация job_snapshot["result"][...] распространяется
    # на _CREATE_JOBS[parent_job_id]["result"]; blueprint.py подберёт обновление при _job_final=dict(j)
    # и сохранит в DB вместе с auto_queued_repair (строки 2711-2727 blueprint.py).
    _new_jid = queued.get("job_id") or ""
    try:
        _res = job_snapshot.get("result") if isinstance(job_snapshot.get("result"), dict) else None
        if _res is not None:
            _lv = _res.get("live_verification") if isinstance(_res.get("live_verification"), dict) else None
            if _lv is not None:
                _rp = _lv.get("repair_plan") if isinstance(_lv.get("repair_plan"), dict) else None
                if _rp is not None:
                    _rp["status"] = "resolved"
                    if _new_jid:
                        _rp["resolved_by"] = _new_jid
    except Exception:  # noqa: BLE001
        pass
    return queued

"""Create-set tp2/tp4 text campaign builders extracted from blueprint.py."""

from __future__ import annotations

import re
import time

_DEPS: dict = {}


def configure(deps: dict) -> None:
    """Bind blueprint dependencies used by text builders."""
    _DEPS.clear()
    _DEPS.update(deps)
    globals().update(deps)


def _build_tp2_adgroups(token: str, login: str, campaign_id: int,
                        region_ids: list, groups: list,
                        feed_id: int = 0, with_shopping: bool = False,
                        apply_group_minus: bool = True,
                        autotarget: bool = False) -> dict:
    """Наполнить Поисковую (tp2) / tp5 группами БАТЧЕМ: adgroups.add → keywords.add → ads.add(TextAd).

    groups: [{name, keywords:[], minus:[], title, text, href, title2?, callout_ext_ids?}].
    feed_id + with_shopping (tp5 «Товарная галерея», как в слепках): дополнительно к TextAd в каждую
    группу — ListingAd (динамика) + ShoppingAd (товарное) по фиду → состав «Т+Л+ТОВ». Проверено live:
    v5 TEXT-кампания принимает ShoppingAd. Аддитивно: нет фида/флага → только TextAd (старое поведение).
    apply_group_minus: если False — групповые минусы НЕ вешаются (для campaign/shared_set слепков, где
    минусы уходят на уровень кампании, а не групп — как в реальных аккаунтах pavlov/kryuchkova/scherbakova).
    Анти-блок: операции идут пачками (см. _AC_CHUNK_*) с паузами, групп ≤ _AC_GROUP_CAP за проход.
    Кампания остаётся черновиком (State=OFF из оболочки). Лимиты Директа: ключей ≤200/группу,
    минус ≤4096 симв. без пробелов/группу (terehov), Title ≤56, Title2 ≤30, Text ≤81, уточнений ≤4/объявление.
    → {adgroups, keywords, ads, errors, deferred}."""
    rep = {"adgroups": 0, "keywords": 0, "ads": 0, "images_uploaded": 0, "errors": [], "deferred": 0}
    rids = [int(r) for r in (region_ids or []) if str(r).lstrip("-").isdigit()] or [225]
    if len(groups) > _AC_GROUP_CAP:                       # кап за проход (анти-блок)
        rep["deferred"] = len(groups) - _AC_GROUP_CAP
        groups = groups[:_AC_GROUP_CAP]

    # ── Фаза 0: картинки НЕ грузим через v501 заранее.
    # Живой баг 2026-06-28: массовый upload_image на token-path мог подвешивать создание кампании
    # до ads.add, в итоге кампания появлялась с adgroups, но без объявлений. Боевой fallback теперь
    # такой: ResponsiveAd создаём сразу, а image hashes добиваем post-create через Grid/куки
    # (_grid_update_adaptive_ads + GridClient.upload_image) по фактическим ad_id.

    # ── Фаза 1: adgroups.add пачками; ag_ids[i] выровнен по индексу группы (AddResults в порядке входа)
    specs = []
    for g in groups:
        ag = {"Name": (g.get("name") or "группа")[:255], "CampaignId": int(campaign_id), "RegionIds": rids,
              "TrackingParams": _UTM_TEMPLATE_TP1}      # #2 UTM на уровне группы (tp2/tp5 Поиск, v5 — проверено LIVE)
        if apply_group_minus:
            # Групповые минусы: обрезка по символьному бюджету 4096 (≤4096 симв. без пробелов/группу,
            # как у terehov — полный список без cap=100). Для campaign/shared_set слепков apply_group_minus=False.
            minus = _minus_char_budget(g.get("minus") or [], _MINUS_SHARED_SET_CHAR_BUDGET)
            if minus:
                ag["NegativeKeywords"] = {"Items": minus}
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

    # ── Фаза 2: keywords.add пачками (≤200/группу, до _AC_CHUNK_KW items за вызов)
    # autotarget=True → вместо реальных ключей вешаем спецключ "---autotargeting" (1 на группу) —
    # это и есть автотаргетинг в v5 (проверено live на боевом аккаунте porg-36k7btt7).
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

    # ── Фаза 3: ads.add пачками — КОМБИНАТОРНОЕ объявление (ResponsiveAd) через v501.
    # Замена ТГО (TextAd, отключают с 30.06): несколько заголовков/текстов в одном объявлении.
    # Уточнения наследуются на уровне группы/кампании (AdExtensions у ResponsiveAd нет).
    ad_items = []
    ad_meta = []   # параллельно ad_items — для adPrice из фида (#2)
    _acc_url = ""
    for i, g in enumerate(groups):
        if not ag_ids[i]:
            continue
        href = g.get("href") or ""
        if href and not _acc_url:
            _acc_url = re.sub(r"(https?://[^/]+).*", r"\1", href)   # https://домен — для выбора фида цен
        img_paths = g.get("image_paths") or ([g.get("image_path")] if g.get("image_path") else [])
        ra = _responsive_ad(g.get("titles") or [g.get("title"), g.get("name")],
                            g.get("texts") or [g.get("text")], href,
                            image_hashes=None)
        if not ra:
            rep["errors"].append(f"{g.get('name', '?')}: пропущено объявление (нет заголовков/текстов/href)")
            continue
        ad_items.append({"AdGroupId": int(ag_ids[i]), "ResponsiveAd": ra})
        ad_meta.append({"brand": g.get("brand") or g.get("name") or "", "href": href,
                        "seg": _ct_segment(g.get("ct") or ""),   # 'Марки' → цена = МИН по марке
                        "titles": ra.get("Titles") or [], "bodies": ra.get("Texts") or [],
                        "image_hashes": [],
                        "image_paths": img_paths[:5]})
    created_ad_meta = []
    _base = 0
    for chunk in _chunks(ad_items, _AC_CHUNK_AD):
        jd = _v501_svc("ads", "add", token, login, {"Ads": chunk})
        used_retry = ""
        if "error" in jd:
            for retry_name, retry_chunk in (
                ("без быстрых ссылок", _responsive_retry_items(chunk, drop_sitelinks=True)),
                ("без быстрых ссылок и картинок", _responsive_retry_items(chunk, drop_sitelinks=True, drop_images=True)),
            ):
                jd2 = _v501_svc("ads", "add", token, login, {"Ads": retry_chunk})
                if "error" not in jd2:
                    jd = jd2
                    used_retry = retry_name
                    rep.setdefault("warnings", []).append(f"ads.add(tp1 ResponsiveAd): retry {retry_name}")
                    break
        if "error" not in jd:
            for k, r in enumerate((jd.get("result") or {}).get("AddResults", [])):
                if r.get("Id"):
                    rep["ads"] += 1
                    gi = _base + k
                    if gi < len(ad_meta):
                        created_ad_meta.append((int(r["Id"]), ad_meta[gi]))
                for e in (r.get("Errors") or []):
                    rep["errors"].append(f"ResponsiveAd: {e.get('Message')} {e.get('Details','')}".strip())
        else:
            rep["errors"].append(f"ads.add(ResponsiveAd) {_v5_err(jd)}")
        _base += len(chunk)
        time.sleep(_AC_BATCH_SLEEP)

    # Фаза 3.4: post-create repair через Grid/куки для token-path.
    # Даже если v501 ads.add прошло, live payload в Директе может схлопнуться до 2 заголовков /
    # 1-2 картинок. Поэтому после создания всегда добиваем фактическое объявление через Grid:
    # titles + bodies + imageHashes. Это и есть fallback «если токены недогрузили — догружаем по куки».
    if created_ad_meta:
        try:
            import os as _os3
            _gc_img = gf.GridClient(login)
            _uploaded_by_name: dict[str, str] = {}
            _upd_items = []
            for ad_id, meta in created_ad_meta:
                _hashes = list(dict.fromkeys(meta.get("image_hashes") or []))
                for _pth in (meta.get("image_paths") or []):
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
                    meta["image_hashes"] = _hashes[:5]
                _upd = {"id": ad_id, "href": meta["href"], "titles": meta["titles"], "bodies": meta["bodies"]}
                if meta.get("image_hashes") is not None:
                    _upd["image_hashes"] = meta.get("image_hashes") or []
                _upd_items.append(_upd)
            if _upd_items:
                rep["ads_repaired"] = _grid_update_adaptive_ads(login, _upd_items)
                rep["image_groups"] = sum(1 for _it in _upd_items if _it.get("image_hashes"))
        except Exception as _e:  # noqa: BLE001
            rep.setdefault("warnings", []).append(f"tp2/tp4 repair: {str(_e)[:100]}")

    # Фаза 3.5: ЦЕНА из фида в комбинаторное (#2) — Grid по куке. adPrice по бренду/модели группы.
    # Цены берём из предпочтительных фидов (чистые имена офферов), а не из дефолтного (промо-префикс).
    try:
        _pmap = _account_offer_prices(login, _acc_url)
        if _pmap and created_ad_meta:
            _pitems = []
            for ad_id, meta in created_ad_meta:
                cur, old = _group_ad_price(_pmap, meta.get("brand", ""), meta.get("seg", ""))
                if cur:
                    _pitems.append({"id": ad_id, "href": meta["href"], "titles": meta["titles"],
                                    "bodies": meta["bodies"], "image_hashes": meta["image_hashes"],
                                    "current": cur, "old": old})
            rep["prices_set"] = _grid_set_ad_prices(login, _pitems)
            # ads_repaired_after_price УДАЛЁН (2026-07-02, code-review): repair_items без adPrice →
            # Grid full-replace ЗАТИРАЛ только что установленные цены (тот же баг чинили в tp1).
            # Контент цела: _grid_set_ad_prices сам шлёт titles/bodies/imageHashes полностью.
    except Exception as _e:  # noqa: BLE001
        rep.setdefault("warnings", []).append(f"adPrice: {str(_e)[:100]}")

    # ── Фаза 4 (tp5): товарные по фиду — ListingAd (динамика) + ShoppingAd (товарное) в каждую группу.
    # Состав «Т+Л+ТОВ» как в слепках. v501 add_listing_ad/add_shopping_ad (FeedId в объявлении).
    if feed_id and with_shopping:
        rep["listing_ads"], rep["shopping_ads"] = 0, 0
        v501c = cmc.DirectV501Client(token, login)
        v501c.sess.headers.update({"Authorization": f"Bearer {token}"})
        for i in range(len(groups)):
            if not ag_ids[i]:
                continue
            try:
                if v501c.add_listing_ad(int(ag_ids[i]), int(feed_id)):
                    rep["listing_ads"] += 1
            except Exception as e:  # noqa: BLE001 — товарные не критичны, TextAd уже создан
                rep["errors"].append(f"{groups[i].get('name','?')}: listing_ad {str(e)[:80]}")
            try:
                if v501c.add_shopping_ad(int(ag_ids[i]), int(feed_id)):
                    rep["shopping_ads"] += 1
            except Exception as e:  # noqa: BLE001
                rep["errors"].append(f"{groups[i].get('name','?')}: shopping_ad {str(e)[:80]}")
            time.sleep(_AC_BATCH_SLEEP)
    return rep

def _struct_cts(slepok: str, site_type: str, tp_code: str) -> list:
    """Список модель-ct для (слепок, тип сайта, tp_code) из структуры (формат groups+gc).
    Грубый формат (splits без gc) → []."""
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    struct = _json("slepki_structure.json").get("directologists", [])
    d = next((x for x in struct if x.get("key") == key), None)
    if not d:
        return []
    st = next((s for s in d.get("site_types", []) if s.get("name") == site_type), None)
    if not st:
        return []
    cts, seen = [], set()
    for tp in st.get("tp", []):
        if tp.get("code") != tp_code:
            continue
        for grp in tp.get("groups", []):          # формат terehov; у splits ключа groups нет
            for it in grp.get("items", []):
                ct = _gc_ct(it.get("gc", ""))
                if ct and ct != "ct0000" and ct not in seen:
                    seen.add(ct)
                    cts.append(ct)
    return cts

def _tp2_struct_cts(slepok: str, site_type: str) -> list:
    """Совместимость: модель-ct для tp2."""
    return _struct_cts(slepok, site_type, "tp2")

def _struct_has_tp(slepok: str, site_type: str, tp_code: str) -> bool:
    """tp ОБЪЯВЛЕН в структуре слепка — независимо от наличия модель-ct.
    _struct_cts для этого НЕ годится: у tp7/tp6 бывают только ct0000-группы («Товарка -
    Общая», «Автотаргетинг») → он даёт [] и tp ложно считался «не в слепке»
    (ложняки EXTRA_TP_NOT_IN_SLEPOK: scherbakova-tp7, pavlov-tp6, выверено 03.07.2026)."""
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    struct = _json("slepki_structure.json").get("directologists", [])
    d = next((x for x in struct if x.get("key") == key), None)
    st = next((s for s in (d or {}).get("site_types", []) if s.get("name") == site_type), None)
    return any(tp.get("code") == tp_code for tp in (st or {}).get("tp", []))

def _text_group_name(ct: str, r_code: str, model: str) -> str:
    """Кодер-имя группы текстовой кампании (tp2/tp4 Поиск) по текущему канону:
    {ct}_aon_n000_{r_code}_ct001_ag011_g00 — {model}.
    Если r_code пуст (нет контекста) — отдаём просто модель (старое поведение)."""
    if not r_code:
        return model or ct
    return f"{ct}_aon_n000_{r_code}_ct001_ag011_g00 — {model}"

def _build_text_from_pack(token: str, login: str, campaign_id: int, slepok: str,
                          site_type: str, tp_code: str, region_ids: list, href: str,
                          titles: list | None, texts: list,
                          feed_id: int = 0, with_shopping: bool = False,
                          r_code: str = "", segment: str | None = None,
                          ai_title2: str = "",
                          apply_group_minus: bool = True,
                          city: str = "", autotarget: bool = False) -> dict:
    """Наполнить текстовую кампанию (tp1/tp2/tp5): структура→модель-ct→ключи/минус/уточнения
    из пака M3 (по tp_code)→группы+объявления+callouts. Тексты — из titles/texts. Всё черновиком.

    segment ('Марки'|'Модели'|None): фильтр ct-групп по сегменту (tp4 — марки/модели разными
    кампаниями, как боевые). None → все ct (поведение tp2/tp5 неизменно).

    ⚠️ tp5 «Поиск+Динамика+Товарная галерея»: тут строится ТОЛЬКО поисковый backbone
    (TEXT_AD + ключи). Фид-объявления (LISTING_AD «динамика» / SHOPPING_AD «товарная») —
    автогенерация Яндекса из фида, НЕ через v5 ads.add; добавятся отдельным шагом."""
    cts = _struct_cts(slepok, site_type, tp_code)
    if segment:
        cts = [ct for ct in cts if _ct_segment(ct) == segment]
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    gather_key = key
    donor_note = ""
    # Фолбэк донора: сегмент задан, но у слепка нет своих ct этого сегмента (напр. Терехов tp4
    # без «Моделей») → берём ct И контент сегмента у слепка-донора («по примеру других слепков»,
    # структура слепка не теряется — его «Марки» строятся отдельной кампанией своим контентом).
    if segment and not cts:
        donor = _segment_donor(segment, tp_code, site_type, exclude=key)
        if donor:
            cts = [ct for ct in _struct_cts(donor, site_type, tp_code) if _ct_segment(ct) == segment]
            gather_key = _SLEPOK_KEY.get(donor, donor)
            donor_note = f"сегмент «{segment}» взят у донора «{donor}» (у «{slepok}» своих нет)"
    if not cts:
        return {"skipped": f"нет модель-ct в структуре для {tp_code} (грубый формат)"}
    pack = kp.gather(gather_key, site_type, tp_code)   # один ssh-вызов к M3
    if not pack:
        return {"skipped": "пак недоступен (мост M3?)"}
    text0 = (texts[0] if texts else "")[:81]
    ct_name = _ag_part1_map()                   # ct→имя из gsheet_naming (полное покрытие 318) — кодер
    ct_model = kp.feeds_ct_model()              # фид-индекс (модельные ct) — фолбэк
    _sc_titles = _slepok_campaign_content(slepok, site_type).get("titles") or []  # пул слепка — 1 раз
    # URL страниц моделей: account-level мёрж (все фиды, как цены) → покрывает марки без URL
    # в конкретном feed_id (был Баг-8: formular _model_page_href на 404). (#ФИКС-8)
    _feed_urls = _account_offer_urls(login, href)
    groups = []
    for ct in cts:
        data = pack.get(ct) or {}
        if not data.get("positive"):
            continue                            # нет ключей в паке — пропускаем модель
        model = _valid_pack_brand_name(ct, ct_name.get(ct) or ct_model.get(ct) or ct) or "Авто"
        # deep-link на страницу модели: сначала реальный URL из фида, фолбэк на формульный слаг.
        # ФИКС A: Марки → обрезаем до /auto/{brand}, Модели → полный путь (без query). (#ФИКС-A)
        _raw_feed_url = _feed_url_for_model(_feed_urls, model)
        if _raw_feed_url:
            model_href = (_brand_level_url(_raw_feed_url) if _ct_segment(ct) == "Марки"
                          else _strip_url_query(_raw_feed_url))
        else:
            model_href = _model_page_href(href, site_type, model)
        # Title: шаблон «Новые {model} в {город}. {акция}» (≤35 симв.) — фолбэк model[:56].
        title = _title_from_template(model or "Авто", city) if (not ai_title2 and model) else (model or "Авто")[:56]
        ttl2 = (ai_title2[:30] if ai_title2 else _next_title2())   # ИИ-title2 или round-robin из пула
        # В боевом create_set контент генерим ОДИН РАЗ на кампанию/item. Делать M3-вызов на
        # КАЖДУЮ ct-группу нельзя: tp1/tp5 содержат десятки групп, и создание зависает на минуты
        # ещё до первой кампании. Внутри группы используем уже готовый кампанийный набор +
        # локальную rsya-добивку/дедупликацию.
        g_titles = _rsya_titles(model, city, site_type, ai_title2=ai_title2,
                                base=list(titles or []) + [title, ttl2], pool=_sc_titles,
                                is_brand=(_ct_segment(ct) in ("Марки", "Модели")))
        g_texts = _rsya_texts(list(texts or []) + ([text0] if text0 else []), site_type, city, model)
        # Картинки: общие ct0000-ct0014 → общий пул ct0000; кузова ct0015-ct0018 → свой ct;
        # модели/марки → свой ct.
        tp2_all_images = _creative_images_for_ct(site_type, tp_code, ct, key)
        groups.append({
            "name": _text_group_name(ct, r_code, model),
            # БАГ-13: для «Марки» — убрать ключи «марка+модель» (напр. «Chery Tiggo 8 Pro»)
            "keywords": _filter_group_keywords(data.get("positive", []), _ct_segment(ct), model, city, site_type),
            "minus": data.get("minus", []),
            "ct": ct,                            # баг #5: нужен для _ct_segment→seg→adPrice по Марке
            "brand": model,                      # модель/бренд группы — для adPrice из фида (#2)
            "titles": g_titles,                  # ← Комбинаторное: список заголовков
            "texts": g_texts,                    # ← Комбинаторное: список текстов
            "title": title, "title2": ttl2,      # совместимость (в ResponsiveAd не используются)
            "text": text0 or "Официальный дилер. Тест-драйв и выгодные условия по кредиту. Авто в наличии.",
            "href": model_href,                  # deep-link страницы модели (если возможен)
            # tp2/tp4 (поиск) — картинки запрещены правилом Семёна; tp5/tp1 — разрешены.
            "image_path": (tp2_all_images[0] if tp2_all_images else None) if tp_code not in ("tp2", "tp4") else None,
            "image_paths": tp2_all_images[:5] if tp_code not in ("tp2", "tp4") else [],
            "callouts": data.get("callouts", []),   # уточнения слепка по модели (из пака)
        })
    if not groups:
        return {"skipped": f"пак пуст по {len(cts)} модель-ct"}
    # «Уточнения» (callouts) из пака: создаём общий пул AdExtensions один раз (дедуп) →
    # привязываем ≤4 на объявление каждой группы. Падение callouts не валит сборку.
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
    rep = _build_tp2_adgroups(token, login, campaign_id, region_ids, groups,
                              feed_id=feed_id, with_shopping=with_shopping,
                              apply_group_minus=apply_group_minus, autotarget=autotarget)
    rep["cts"] = len(cts)
    rep["groups_built"] = len(groups)
    rep["callouts_pool"] = len(co_pool)
    if donor_note:
        rep["donor"] = donor_note
    return rep

def _build_tp2_from_pack(token: str, login: str, campaign_id: int, slepok: str,
                         site_type: str, region_ids: list, href: str,
                         titles: list | None, texts: list) -> dict:
    """Совместимость: наполнение Поисковой (tp2)."""
    return _build_text_from_pack(token, login, campaign_id, slepok, site_type, "tp2",
                                 region_ids, href, titles, texts)

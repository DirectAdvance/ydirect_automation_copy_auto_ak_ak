"""tp6/tp7 master/product item handler for create_set."""

from __future__ import annotations

import re

from . import campaign as cmc
from . import create_set_context as _csctx  # _parse_targeting_modes (чистый хелпер, без configure)
from . import kontent_pack as kp


def run_master_product_item(deps: dict, *, it, name, href, region_ids, counter_id, goal_id,
                             cpa, launch, client, agent, eff_site, ctx,
                             tpl_titles, tpl_texts, tpl_sitelinks, rs, login,
                             _st_token, _w_agency, _stream_agent, _job, _tp7_mf):
    """tp6 (МК) / tp7 (Товарка) item handler, вынесен из api_create_set.

    Возвращает (results, _tp7_mf) — обновлённый кэш фидов возвращаем наружу.
    """
    _BU_RE = deps['_BU_RE']
    _GENERIC_AT_TITLES = deps['_GENERIC_AT_TITLES']
    _GENERIC_SITELINK_FILLERS = deps['_GENERIC_SITELINK_FILLERS']
    _GENERIC_TEXT_FILLERS = deps['_GENERIC_TEXT_FILLERS']
    _GENERIC_TITLE_FILLERS = deps['_GENERIC_TITLE_FILLERS']
    _SLEPOK_KEY = deps['_SLEPOK_KEY']
    _TP67_MIN_TEXT_LEN = deps['_TP67_MIN_TEXT_LEN']
    _TP67_OPTIMAL_CATEGORIES = deps['_TP67_OPTIMAL_CATEGORIES']
    _TP67_RELEVANCE_CATEGORIES = deps['_TP67_RELEVANCE_CATEGORIES']
    _account_model_feeds = deps['_account_model_feeds']
    _add_job_err = deps['_add_job_err']
    _audience_objects = deps['_audience_objects']
    _bad_ad_sitelink = deps['_bad_ad_sitelink']
    _bad_ad_text = deps['_bad_ad_text']
    _bad_ad_title = deps['_bad_ad_title']
    _brand_ct_from_coder = deps['_brand_ct_from_coder']
    _brand_title_set = deps['_brand_title_set']
    _build_name = deps['_build_name']
    _bump_item = deps['_bump_item']
    _bump_job = deps['_bump_job']
    _cached_campaign_content = deps['_cached_campaign_content']
    _catalog_feed = deps['_catalog_feed']
    _coherent_discounts = deps['_coherent_discounts']
    _coherent_payments = deps['_coherent_payments']
    _creative_images_for_ct = deps['_creative_images_for_ct']
    _dedup_prefix_absorb = deps['_dedup_prefix_absorb']
    _discount_pcts = deps['_discount_pcts']
    _diverse_text_offers = deps['_diverse_text_offers']
    _drop_used_car = deps['_drop_used_car']
    _enabled_minus_words = deps['_enabled_minus_words']
    _fallback_master_titles = deps['_fallback_master_titles']
    _fill_title = deps['_fill_title']
    _fill_variants = deps['_fill_variants']
    _has_number = deps['_has_number']
    _image_ct_for_content = deps['_image_ct_for_content']
    _is_bad_start = deps['_is_bad_start']
    _is_bu_site = deps['_is_bu_site']
    _is_common_ct = deps['_is_common_ct']
    _is_site_domain_name = deps['_is_site_domain_name']
    _job_db_progress = deps['_job_db_progress']
    _lines = deps['_lines']
    _match_collection = deps['_match_collection']
    _num = deps['_num']
    _own_brand_tokens = deps['_own_brand_tokens']
    _replace_emdash = deps['_replace_emdash']
    _replace_foreign_city = deps['_replace_foreign_city']
    _replace_sep_hyphen = deps['_replace_sep_hyphen']
    _resolve_region = deps['_resolve_region']
    _rsya_texts = deps['_rsya_texts']
    _rsya_titles = deps['_rsya_titles']
    _sanitize_content = deps['_sanitize_content']
    _sitelink_has_pct = deps['_sitelink_has_pct']
    _slepok_audiences_for = deps['_slepok_audiences_for']
    _slepok_campaign_content = deps['_slepok_campaign_content']
    _strip_credit_rate = deps['_strip_credit_rate']
    _title2_blocklist = deps['_title2_blocklist']
    _tp67_keywords_for = deps['_tp67_keywords_for']
    _tp67_targeting_mode = deps['_tp67_targeting_mode']
    _tp7_product_feed_filters = deps['_tp7_product_feed_filters']
    _tp7_listings_minus_filters = deps['_tp7_listings_minus_filters']
    _trim_to_word = deps['_trim_to_word']
    _variant_norm_key = deps['_variant_norm_key']
    _slepok_is_auto = deps['_slepok_is_auto']
    results = []
    # Не-авто признак: False для B2B-слепков (dmp и будущих), True для авто-директологов.
    # Влияет на базу заголовков/текстов (B2B-корпус vs _GENERIC_AT_TITLES) и number-gate.
    _is_non_auto = not _slepok_is_auto(agent or "")
    is_manual = str(it.get("variant", "")).endswith("manual")
    is_product = it.get("type") == "product"
    _raw_mode = str(it.get("targeting_mode") or (
        "keywords" if is_manual else _tp67_targeting_mode({
            "name": it.get("position_name") or name,
            "group": it.get("audience_cat") or "",
            "label": it.get("position_name") or "",
            "code": it.get("structure_code") or "",
        })
    )).strip()
    # ГИБРИД: одна tp6/tp7-позиция может нести keywords И audience одновременно (реальные слепки:
    # tp7 RA = ключи + до 8 аудиторий, tp6 RA = ключи + 1 аудитория). Парсим в НАБОР режимов.
    _modes = _csctx._parse_targeting_modes(_raw_mode)
    _want_keywords = "keywords" in _modes
    _want_audience = "audience" in _modes
    # Явный per-position источник ключей: НЕпустой → позиция ОБЯЗАНА иметь ключи (keyword_source-гейт).
    _keyword_source = str(it.get("keyword_source") or "").strip()
    # Скаляр для легаси-веток «autotarget vs ручной» (имя/socdem/relevance/minus): autotarget ТОЛЬКО
    # когда нет ни ключей, ни аудиторий; иначе ручной (гибрид → keywords-приоритет = ручной режим).
    targeting_mode = "keywords" if _want_keywords else ("audience" if _want_audience else "autotarget")
    # Ось посадки: kviz-кодер → домен/quiz, иначе обычный домен
    it_sq = it.get("sq") or "site"
    it_href = href + ("/quiz" if it_sq == "kviz" else "")
    # Нативные интересы слепка для этого tp (МК=tp6 / Товарка=tp7) — «как в слепках».
    # Приоритет: интересы КОНКРЕТНОЙ категории из плана (отдельная кампания на категорию);
    # фолбэк — объединённый список (старое поведение, если план без категорий).
    it_tp = it.get("tp") or ("tp7" if is_product else "tp6")
    native_ints = ([str(x) for x in (it.get("interest_ids") or []) if str(x).strip()]
                   or _slepok_audiences_for(agent, eff_site, it_tp))
    # Контент слепка (приоритет) + добор из шаблонов до ПОЛНЫХ слотов (Мультибренд чист от б/у):
    # заголовки до 5, тексты до 3, быстрые ссылки до 8.
    # ПРАВИЛО ПОЛЬЗОВАТЕЛЯ: контент по 4-значному ct кодера. ct=марка/модель → её
    # картинка+заголовки; ct0000/нет марки → общий текст (tp6/tp7 сейчас всегда ct0000).
    c_brand, c_ct = _brand_ct_from_coder(it)
    it_keywords: list[str] = []
    it_minus_keywords: list[str] = []
    it_targeting_warnings: list[str] = []
    if _want_keywords:
        it_keywords, it_minus_keywords = _tp67_keywords_for(
            agent, eff_site, it_tp, c_ct or str(it.get("ct") or "ct0000"), ctx.get("city") or "",
            it.get("position_name") or it.get("audience_cat") or name,
            it_sq,
        )
        if not it_keywords:
            # Ключей нет НИ в паке слепка, НИ в его собственной библиотеке реальных UAC-payload
            # (чужой слепок источником быть не может — жёсткий фильтр create_set_context._score).
            _err = (f"tp6/tp7 КС без ключей: slepok={agent}, site_type={eff_site}, "
                    f"tp={it_tp}, ct={c_ct or it.get('ct') or 'ct0000'}, "
                    f"position={(it.get('position_name') or it.get('audience_cat') or name)!r} "
                    f"(нет в паке и в СВОЕЙ библиотеке; заимствование у другого слепка запрещено)")
            if _keyword_source:
                # keyword_source-ГЕЙТ: позиция ЯВНО объявлена keyword-driven (keyword_source задан),
                # но корпус ключей пуст → НЕ уходить молча в autotarget (это баг). Блокируем позицию
                # с явной ошибкой в DoD/логе; кампанию НЕ создаём.
                _blk = (_err + f" → BLOCKED (keyword_source={_keyword_source!r}: молчаливая "
                        "подмена на autotarget запрещена)")
                _add_job_err(_job, _blk)
                results.append({"name": name, "ok": False, "error": _blk[:240], "blocked": True})
                _bump_job(_job, False)
                _bump_item(_job)
                if _job:
                    _job_db_progress(_job)
                return results, _tp7_mf
            # Обратная совместимость (keyword_source НЕ задан): старый мягкий фолбэк — снимаем keywords,
            # остаёмся на audience (если гибрид) либо на autotarget (если ключи были единственным режимом).
            _want_keywords = False
            if not _want_audience:
                targeting_mode = "autotarget"
            it_targeting_warnings.append(
                _err + " → fallback " + ("audience" if _want_audience else "autotarget"))
            _add_job_err(_job, it_targeting_warnings[-1])
    it_audiences = _audience_objects(native_ints) if _want_audience else []
    if _want_audience and not it_audiences:
        _err = (f"tp6/tp7 аудитория без аудиторий слепка: slepok={agent}, "
                f"site_type={eff_site}, tp={it_tp}, category={it.get('audience_cat') or it.get('position_name') or ''}")
        _want_audience = False
        if not _want_keywords:
            targeting_mode = "autotarget"
        it_targeting_warnings.append(
            _err + " → fallback " + ("keywords" if _want_keywords else "autotarget"))
        _add_job_err(_job, it_targeting_warnings[-1])
    if is_product and not c_brand and it_sq != "kviz":
        it_href = re.sub(r"/+$", "", href) + "/auto"
    _sc = _slepok_campaign_content(agent, eff_site)
    if c_brand:                                    # кодер несёт модель -> ведём контент по ней
        _b0 = c_brand.split()[0].lower()
        _model_t = [t for t in _sc["titles"] if _b0 in t.lower()]
        title_primary = _brand_title_set(c_brand, ctx.get("city") or "") + _model_t
        title_supp = title_primary + _sc["titles"] + tpl_titles + _GENERIC_TITLE_FILLERS
    elif is_product:
        # tp7 ct0000 (общая кампания) - заголовки ТОЛЬКО без марки/модели.
        # _sc["titles"] часто брендовые («Haval…», «Chery…») - НЕ используем как base для авто.
        # Для не-авто (dmp и будущие B2B): база = корпус слепка, авто-дженерики не подмешиваем.
        if _is_non_auto:
            _b2b_titles_p = [t for t in (_sc.get("titles") or []) if t and str(t).strip()]
            title_primary = _b2b_titles_p
            title_supp = _b2b_titles_p + tpl_titles
        else:
            title_primary = _GENERIC_AT_TITLES
            title_supp = _GENERIC_AT_TITLES + _GENERIC_TITLE_FILLERS
    else:                                          # tp6 МК ct0000 → авто: _GENERIC_AT_TITLES; не-авто: B2B-корпус
        if _is_non_auto:
            # Не-авто (dmp и будущие B2B): база = B2B-корпус слепка; авто-дженерики НЕ подмешиваем.
            # B2B-строки часто без цифр («Получайте контакты потенциальных клиентов…») —
            # поэтому number-gate ниже отключён для не-авто.
            _b2b_titles = [t for t in (_sc.get("titles") or []) if t and str(t).strip()]
            title_primary = _b2b_titles
            title_supp = _b2b_titles + tpl_titles
        else:
            # баг #8: _sc["titles"] часто содержат марки («Haval у дилера»…) — для общей МК (ct0000)
            # это неверно. Используем _GENERIC_AT_TITLES как base; _sc["titles"] только в supp-добавке.
            title_primary = _GENERIC_AT_TITLES
            title_supp = _sc["titles"] + tpl_titles + _GENERIC_TITLE_FILLERS
    # ЖЁСТКОЕ ПРАВИЛО tp6/tp7: РОВНО 5 заголовков / 3 текста / 8 быстрых ссылок.
    # ГОРОД В КОНТЕНТЕ обязан совпадать с городом аккаунта.
    _acc_city = (ctx.get("city") or "").strip()
    _, _cities_bl = _title2_blocklist()
    _cf = lambda lst: _replace_foreign_city(_drop_used_car(lst, eff_site), _acc_city, _cities_bl)  # noqa: E731
    # БАГ 1: для tp7 ct0000 - base = ТОЛЬКО общие заголовки, игнорируем it.get("titles")
    if not c_brand:
        # ct0000 (общая кампания) — tp6 МК И tp7 Товарка: base = только общие заголовки,
        # игнорируем it.get("titles") (могут содержать бренды от ИИ).
        # баг #8: фильтр брендовых токенов распространён на tp6 ct0000 (раньше только tp7).
        _t_base = list(title_primary)
        _ct_brand_tokens: set = {v.split()[0].lower() for v in kp.feeds_ct_model().values() if v}
        _foreign_brand_tokens, _foreign_city_tokens = _title2_blocklist()
    else:
        _t_base = title_primary
        _ct_brand_tokens = set()
        _foreign_brand_tokens, _foreign_city_tokens = set(), set()

    def _common_content_ok(_s: str) -> bool:
        if c_brand:
            return True
        _words = set(re.sub(r"[^\wа-яё]+", " ", str(_s or "").lower()).split())
        return not (_words & _foreign_brand_tokens) and not (_words & _foreign_city_tokens)
    # Сборка заголовков с пост-обработкой (БАГ 1+2+3+4+7+8)
    # Для не-авто (B2B): _GENERIC_TITLE_FILLERS — авто-кредитные, не добавляем в пул.
    _title_fill_pool = _cf(title_supp) + ([] if _is_non_auto else _GENERIC_TITLE_FILLERS)
    _raw_titles = _fill_variants(_cf(_t_base), _title_fill_pool, 12)
    it_titles = []
    _seen_tk: set = set()
    for _t in _raw_titles:
        if not _t or not str(_t).strip():
            continue
        _t = _sanitize_content(str(_t), max_len=56)   # БАГ 3+4+8
        _t = _fill_title(_replace_sep_hyphen(_replace_emdash(_strip_credit_rate(_t))), 45, 56)  # добить до 45-56 (#26)
        if not _t or _is_bad_start(_t) or _bad_ad_title(_t) or not _common_content_ok(_t) or (not _is_non_auto and not _has_number(_t)):
            continue  # number-gate: для авто tp6/tp7 обязан содержать цифру; для не-авто (B2B) отключён
        if c_brand:
            _own = _own_brand_tokens(c_brand)
            _tl = _t.lower()
            if _own and not any(re.search(r"(?<![a-zа-яё0-9])" + re.escape(tok) + r"(?![a-zа-яё0-9])", _tl)
                                for tok in _own):
                continue
        if _ct_brand_tokens:                           # БАГ 1: фильтр марок для ct0000
            _tl = _t.lower()
            if any(_tok in _tl for _tok in _ct_brand_tokens):
                continue
        _nk = _variant_norm_key(_t)                   # БАГ 2: дедуп по смысловому ключу
        if _nk and _nk in _seen_tk:
            continue
        if _nk:
            _seen_tk.add(_nk)
        it_titles.append(_t)
        if len(it_titles) >= 5:
            break
    if c_brand and len(it_titles) < 5:
        _own = _own_brand_tokens(c_brand)
        for _t in _rsya_titles(c_brand, _acc_city, eff_site, base=[], pool=title_supp,
                               is_brand=True, cap=5):
            _tl = _t.lower()
            if _own and not any(re.search(r"(?<![a-zа-яё0-9])" + re.escape(tok) + r"(?![a-zа-яё0-9])", _tl)
                                for tok in _own):
                continue
            if _is_bad_start(_t) or _bad_ad_title(_t) or not _has_number(_t):
                continue  # тот же number-gate/бэд-фильтр, что и основной цикл (12858) — иначе заголовок без цифры
            _nk = _variant_norm_key(_t)
            if _nk and _nk in _seen_tk:
                continue
            if _nk:
                _seen_tk.add(_nk)
            it_titles.append(_t)
            if len(it_titles) >= 5:
                break
    # Сборка текстов с пост-обработкой (БАГ 2+3+4+8)
    # баг #8: для ct0000 (авто, общая кампания) тексты из ИИ/слепка могут содержать марки →
    # при ct0000 используем только _GENERIC_TEXT_FILLERS как base (без брендовых текстов ИИ).
    # Для не-авто (dmp и будущие B2B): база = B2B-тексты корпуса слепка; авто-дженерики НЕ используем.
    if not c_brand:
        if _is_non_auto:
            _b2b_texts = [t for t in (_sc.get("texts") or []) if t and str(t).strip()]
            _x_base = _b2b_texts or _GENERIC_TEXT_FILLERS
        else:
            _x_base = _GENERIC_TEXT_FILLERS
    else:
        _x_base = _rsya_texts((_lines(it.get("texts")) or []) + (_sc["texts"] or []),
                              eff_site, ctx.get("city") or "", c_brand, cap=8)
    # Для не-авто: _GENERIC_TEXT_FILLERS авто-кредитные — не добавляем в пул.
    _text_fill_pool = tpl_texts + ([] if _is_non_auto else _GENERIC_TEXT_FILLERS)
    _raw_texts = _fill_variants(_cf(_x_base), _text_fill_pool, 8)
    it_texts = []
    _seen_xk: set = set()
    for _x in _raw_texts:
        if not _x or not str(_x).strip():
            continue
        _x = _sanitize_content(str(_x), max_len=81)   # БАГ 3+4+8
        _x = _strip_credit_rate(_x)
        _x = _trim_to_word(_x, 81).rstrip()           # БАГ 3: обрезка по целому слову
        if not _x or _is_bad_start(_x) or _bad_ad_text(_x) or not _common_content_ok(_x) or (not _is_non_auto and not _has_number(_x)):
            continue  # number-gate: для авто — обязательна цифра; для не-авто (B2B) отключён
        _nk = _variant_norm_key(_x)                   # БАГ 2
        if _nk and _nk in _seen_xk:
            continue
        if _nk:
            _seen_xk.add(_nk)
        it_texts.append(_x)
        if len(it_texts) >= 3:
            break
    # #5/#6 Когерентность скидок: одно число на кампанию.
    it_titles, it_texts = _coherent_discounts(it_titles, it_texts)
    # Финальный кап ПОСЛЕ когерентности (она могла удлинить строку). БАГ 3: по слову.
    _title_cap = 5
    it_titles = list(dict.fromkeys(_trim_to_word(t, 56).rstrip() for t in it_titles if t and t.strip()))[:_title_cap]
    it_texts = _diverse_text_offers(
        list(dict.fromkeys(_trim_to_word(t, 81).rstrip() for t in it_texts if t and t.strip())),
        3,
    )
    raw_sl = it.get("sitelinks") if isinstance(it.get("sitelinks"), list) else None
    _sl_base = ([{"title": s.get("title", ""), "description": s.get("description", "")}
                 for s in raw_sl if isinstance(s, dict) and s.get("title")] if raw_sl
                else _sc["sitelinks"])
    it_sitelinks = []
    _seen_sl_keys: set = set()
    _title_has_pct = bool(_discount_pcts(it_titles))
    for _s in _fill_variants(_sl_base, tpl_sitelinks + _GENERIC_SITELINK_FILLERS, 16):
        if not isinstance(_s, dict):
            continue
        _st = _trim_to_word(_sanitize_content(_s.get("title", ""), max_len=30), 30).rstrip()
        _sd = _trim_to_word(_sanitize_content(_s.get("description", ""), max_len=60), 60).rstrip()
        if not _is_bu_site(eff_site) and _BU_RE.search(f"{_st} {_sd}"):
            continue
        if _title_has_pct and _sitelink_has_pct({"title": _st, "description": _sd}):
            continue
        if not _st or _bad_ad_sitelink(_st, _sd) or not _common_content_ok(f"{_st} {_sd}"):
            continue
        _sk = (_st.lower(), _sd.lower())
        if _sk in _seen_sl_keys:
            continue
        _seen_sl_keys.add(_sk)
        it_sitelinks.append({"title": _st, "description": _sd})
        if len(it_sitelinks) >= 8:
            break
    if len(it_sitelinks) < 8:
        for _s in _GENERIC_SITELINK_FILLERS:
            _st = _trim_to_word(_sanitize_content(_s.get("title", ""), max_len=30), 30).rstrip()
            _sd = _trim_to_word(_sanitize_content(_s.get("description", ""), max_len=60), 60).rstrip()
            _sk = (_st.lower(), _sd.lower())
            if not _is_bu_site(eff_site) and _BU_RE.search(f"{_st} {_sd}"):
                continue
            if _title_has_pct and _sitelink_has_pct({"title": _st, "description": _sd}):
                continue
            if (_st and not _bad_ad_sitelink(_st, _sd) and _common_content_ok(f"{_st} {_sd}")
                    and _sk not in _seen_sl_keys):
                _seen_sl_keys.add(_sk)
                it_sitelinks.append({"title": _st, "description": _sd})
            if len(it_sitelinks) >= 8:
                break
    try:
        from . import ai_agents as _A
        it_titles, it_texts, _t2_unused, it_sitelinks, _utp_changed = _A.unify_utp_numbers(
            it_titles, it_texts, "", it_sitelinks
        )
    except Exception:  # noqa: BLE001
        pass
    it_titles, it_texts, it_sitelinks, _pay_changed = _coherent_payments(
        it_titles, it_texts, it_sitelinks
    )
    if len(it_titles) < 5 or len(it_texts) < 3:
        try:
            from . import ai_agents as _A
            _regen_agent = _stream_agent or _A.get_agent(agent or "")
            if _regen_agent:
                _rc = _cached_campaign_content(
                    login, _regen_agent, (agent or "").strip().lower(),
                    it, eff_site, ctx.get("city") or "", avoid=it_titles,
                ) or {}
                for _t in _lines(_rc.get("titles")):
                    if len(it_titles) >= 5:
                        break
                    _t = _sanitize_content(str(_t), max_len=56)
                    _t = _strip_credit_rate(_t)[:56].rstrip()
                    if not _t or _is_bad_start(_t) or _bad_ad_title(_t) or not _common_content_ok(_t) or (not _is_non_auto and not _has_number(_t)):
                        continue  # number-gate regen
                    if c_brand:
                        _own = _own_brand_tokens(c_brand)
                        _tl = _t.lower()
                        if _own and not any(
                            re.search(r"(?<![a-zа-яё0-9])" + re.escape(tok) + r"(?![a-zа-яё0-9])", _tl)
                            for tok in _own
                        ):
                            continue
                    if _ct_brand_tokens:
                        _tl = _t.lower()
                        if any(_tok in _tl for _tok in _ct_brand_tokens):
                            continue
                    _nk = _variant_norm_key(_t)
                    if _nk and (_nk in _seen_tk or any(_variant_norm_key(x) == _nk for x in it_titles)):
                        continue
                    if _nk:
                        _seen_tk.add(_nk)
                    it_titles.append(_t)
                for _x in _lines(_rc.get("texts")):
                    if len(it_texts) >= 3:
                        break
                    _x = _sanitize_content(str(_x), max_len=81)
                    _x = _trim_to_word(_strip_credit_rate(_x), 81).rstrip()
                    if not _x or _is_bad_start(_x) or _bad_ad_text(_x) or not _common_content_ok(_x) or (not _is_non_auto and not _has_number(_x)):
                        continue  # number-gate regen
                    _nk = _variant_norm_key(_x)
                    if _nk and (_nk in _seen_xk or any(_variant_norm_key(x) == _nk for x in it_texts)):
                        continue
                    if _nk:
                        _seen_xk.add(_nk)
                    it_texts.append(_x)
                if _rc.get("sitelinks") and len(it_sitelinks) < 8:
                    for _s in _rc.get("sitelinks") or []:
                        if len(it_sitelinks) >= 8 or not isinstance(_s, dict):
                            break
                        _st = _trim_to_word(_sanitize_content(_s.get("title", ""), max_len=30), 30).rstrip()
                        _sd = _trim_to_word(_sanitize_content(_s.get("description", ""), max_len=60), 60).rstrip()
                        _sk2 = (_st.lower(), _sd.lower())
                        if (_st and not _bad_ad_sitelink(_st, _sd)
                                and _common_content_ok(f"{_st} {_sd}")
                                and _sk2 not in _seen_sl_keys
                                and not (not _is_bu_site(eff_site) and _BU_RE.search(f"{_st} {_sd}"))):
                            _seen_sl_keys.add(_sk2)
                            it_sitelinks.append({"title": _st, "description": _sd})
        except Exception:  # noqa: BLE001
            pass
        it_titles, it_texts = _coherent_discounts(it_titles, it_texts)
        it_titles, it_texts, it_sitelinks, _pay_changed = _coherent_payments(
            it_titles, it_texts, it_sitelinks
        )
        it_titles = list(dict.fromkeys(
            _trim_to_word(t, 56).rstrip() for t in it_titles if t and t.strip()
        ))[:5]
        it_texts = _diverse_text_offers(
            list(dict.fromkeys(_trim_to_word(t, 81).rstrip() for t in it_texts if t and t.strip())),
            3,
        )
    # _fallback_master_titles возвращает авто-кредитный контент («Авто в кредит», «КАСКО»…).
    # Для не-авто (B2B dmp) — не добираем авто-фолбэком; используем что есть из B2B-корпуса.
    if len(it_titles) < 5 and not _is_non_auto:
        for _t in _fallback_master_titles(c_brand or "", _acc_city, eff_site, 5):
            if len(it_titles) >= 5:
                break
            _nk = _variant_norm_key(_t)
            if _nk and any(_variant_norm_key(x) == _nk for x in it_titles):
                continue
            it_titles.append(_t)
    # Префиксное поглощение: «…бесплатно» vs «…бесплатно при покупке в кредит» = почти-дубль
    # (один — расширение другого) → оставляем более информативную. ПОСЛЕ — добор из банка до 3.
    it_texts = [x for x in _dedup_prefix_absorb(it_texts) if len(str(x).strip()) >= _TP67_MIN_TEXT_LEN]
    if len(it_texts) < 3:
        # Для не-авто: _GENERIC_TEXT_FILLERS авто-кредитные — не добираем ими.
        _txt_fallback = [] if _is_non_auto else list(_GENERIC_TEXT_FILLERS)
        it_texts = _diverse_text_offers(
            _dedup_prefix_absorb(it_texts + _txt_fallback), 3)
    # R2-4 2026-07-10 (c1): topic/semantic-дедуп быстрых ссылок ВСЕГДА (не только при <8).
    # UAC-путь (tp6/tp7) собирал 8 сайтлинков с дедупом ТОЛЬКО по ТОЧНОЙ строке title+desc →
    # кредитный дубль «Платеж от 9 000 ₽/мес» + «Автокредит от 9 000 ₽/мес» проходил как две
    # разные строки (bucket credit≤2 их пропускал), хотя _coherent_payments свёл ОБЕ суммы к 9000
    # → семантически это ОДИН оффер (credit_pay|9000). _dedup_sitelinks (bucket-лимиты +
    # _sitelink_semantic_key) их схлопывает, но раньше вызывался ТОЛЬКО в ветке <8 → при полном
    # комплекте 8 не отрабатывал = «UAC мимо дедупа». Порядок diversify→dedup — как на tp1/tp2/tp5.
    try:
        from . import ai_agents as _A
        _dl = _A._dedup_sitelinks(_A.diversify_sitelink_utp(it_sitelinks, 8), eff_site, 8)
        if _dl:
            it_sitelinks = _dl
    except Exception:  # noqa: BLE001
        pass
    if len(it_sitelinks) < 8:
        _sl_candidates = list(it_sitelinks or [])
        for _s in _GENERIC_SITELINK_FILLERS:
            _st = _trim_to_word(_sanitize_content(_s.get("title", ""), max_len=30), 30).rstrip()
            _sd = _trim_to_word(_sanitize_content(_s.get("description", ""), max_len=60), 60).rstrip()
            if (_st and _sd and not _bad_ad_sitelink(_st, _sd)
                    and _common_content_ok(f"{_st} {_sd}")
                    and not (not _is_bu_site(eff_site) and _BU_RE.search(f"{_st} {_sd}"))):
                _sl_candidates.append({"title": _st, "description": _sd})
        try:
            from . import ai_agents as _A
            it_sitelinks = _A._dedup_sitelinks(_sl_candidates, eff_site, 8)
        except Exception:  # noqa: BLE001
            it_sitelinks = _sl_candidates[:8]
    # R2-4 2026-07-10 (c2+d): account-pass — единая сумма платежа (d, дефолт ON) и, под флагом
    # DIRECT_SITELINK_REUSE_ACCOUNT (c2, дефолт OFF), ОДИН согласованный набор сайтлинков на
    # весь аккаунт (tp1/tp2/tp5/tp6/tp7). Сайтлинки эталона re-фильтруем per-item гардами
    # (pct/BU/лимит) — несовместимый эталон → остаёмся на своём наборе (brand-first/лимиты целы).
    try:
        from . import ai_content as _AC
        # FIX6/#4 (2026-07-10): АТОМАРНЫЙ get-or-put (тот же, что в orchestrator для tp1/tp2/tp5) —
        # устраняет гонку параллельных каналов при DIRECT_PARALLEL_CHANNELS=1. tp7 (UAC) участвует в
        # ЕДИНОМ эталоне: первый канал с полным набором сеет, tp6/tp7 берут ТОТ ЖЕ → один
        # inheritableSitelinkSet на все tp. _title_has_pct вычислен выше (сборка сайтлинков).
        _reuse_sl, _is_acc = _AC._account_sitelinks_get_or_put(login, list(it_sitelinks or []))
        if _reuse_sl:
            _rf: list = []
            for _s in _reuse_sl:
                if not isinstance(_s, dict):
                    continue
                _st = str(_s.get("title") or "").strip()
                _sd = str(_s.get("description") or "").strip()
                if not _st or _bad_ad_sitelink(_st, _sd) or not _common_content_ok(f"{_st} {_sd}"):
                    continue
                if not _is_bu_site(eff_site) and _BU_RE.search(f"{_st} {_sd}"):
                    continue
                if _title_has_pct and _sitelink_has_pct({"title": _st, "description": _sd}):
                    continue
                _rf.append({"title": _st, "description": _sd})
            if len(_rf) >= 8:
                it_sitelinks = _rf[:8]
        it_titles, it_texts, it_sitelinks = _AC._account_pay_unify(
            login, it_titles, it_texts, it_sitelinks)
    except Exception:  # noqa: BLE001 — account-pass reuse не критичен: фолбэк на локальный набор
        pass
    # DEFECT 2/4 (2026-07-10): ГАРАНТ 8 сайтлинков с УНИКАЛЬНЫМИ описаниями на UAC-пути.
    # campaign._norm_sitelinks дедупит по title И description (защита от DUPLICATE_SITELINK_DESCS
    # — UAC отвергает набор с повторяющимися описаниями). Сборка выше дедупила по ПАРЕ
    # (title,desc) → два сайтлинка с одинаковым description, но разными title, проходили сборку,
    # а _norm_sitelinks их резал → в кабинет уходило <8 (psm5h7q6: 6/8). Здесь финально:
    # (1) дедуп по описанию (как _norm_sitelinks), (2) добор до 8 из _GENERIC_SITELINK_FILLERS
    # с НЕиспользованными описаниями, %-safe (при %-заголовке не берём %-филлеры).
    _title_has_pct = bool(_discount_pcts(it_titles))
    _uniq_sl: list = []
    _seen_titles: set = set()
    _seen_descs: set = set()
    for _s in (it_sitelinks or []):
        if not isinstance(_s, dict):
            continue
        _st = str(_s.get("title") or "").strip()[:30].rstrip()
        _sd = str(_s.get("description") or "").strip()[:60].rstrip()
        if not _st:
            continue
        _tk, _dk = _st.lower(), _sd.lower()
        if _tk in _seen_titles or (_dk and _dk in _seen_descs):
            continue
        _seen_titles.add(_tk)
        if _dk:
            _seen_descs.add(_dk)
        _uniq_sl.append({"title": _st, "description": _sd})
    if len(_uniq_sl) < 8:
        for _s in _GENERIC_SITELINK_FILLERS:
            if len(_uniq_sl) >= 8:
                break
            _st = _trim_to_word(_sanitize_content(_s.get("title", ""), max_len=30), 30).rstrip()
            _sd = _trim_to_word(_sanitize_content(_s.get("description", ""), max_len=60), 60).rstrip()
            _tk, _dk = _st.lower(), _sd.lower()
            if not _st or _tk in _seen_titles or (_dk and _dk in _seen_descs):
                continue
            if not _is_bu_site(eff_site) and _BU_RE.search(f"{_st} {_sd}"):
                continue
            if _title_has_pct and _sitelink_has_pct({"title": _st, "description": _sd}):
                continue
            if _bad_ad_sitelink(_st, _sd) or not _common_content_ok(f"{_st} {_sd}"):
                continue
            _seen_titles.add(_tk)
            if _dk:
                _seen_descs.add(_dk)
            _uniq_sl.append({"title": _st, "description": _sd})
    it_sitelinks = _uniq_sl[:8]
    # Картинки: порядок задаёт _creative_images_for_ct (automation_runtime.py:2700-2756) —
    # СНАЧАЛА общий пул Manual по ct (_manual/{ct}), ПОТОМ добор из пака ЭТОГО слепка
    # (read_slepok_images по канону слепка), потом explicit-ассеты вкладки «Контент».
    # Добор из слепка включается только если общего пула не хватило до limit.
    # Канон слепка (_sk) обязателен: read_slepok_images фильтрует image_slepki.txt по слепку,
    # иначе Павлов получил бы картинки Кудерко (ключ пула — тип сайта, не слепок). ВИДЕО — по логину.
    _sk = _SLEPOK_KEY.get((agent or "").lower(), (agent or "").lower())   # канон слепка
    # Per-domain картинки для не-авто слепков (dmp и будущие): нормализуем домен из ctx.
    # lower + strip + убрать схему + убрать www. + убрать trailing /
    _raw_domain = (ctx.get("domain") or "").strip()
    _norm_domain = re.sub(r"^https?://", "", _raw_domain).lower()
    _norm_domain = re.sub(r"^www\.", "", _norm_domain).rstrip("/").strip()
    _img_domain = _norm_domain if _sk == "dmp" else ""
    try:
        _candidate_image_limit = 12
        if c_ct and not _is_common_ct(c_ct):        # модель/кузов в кодере (tp6/tp7)
            img_ct = _image_ct_for_content(c_ct)
            it_images = _creative_images_for_ct(eff_site, it_tp, img_ct, _sk,
                                                domain=_img_domain,
                                                limit=_candidate_image_limit)
        else:                                      # ct0000 (Общее) → только безопасный общий пул.
            # M3 ct0000 и image_slepki сейчас содержат модельные баннеры, поэтому для общих
            # групп не используем их вообще.
            it_images = _creative_images_for_ct(eff_site, it_tp, "ct0000", _sk,
                                                domain=_img_domain,
                                                limit=_candidate_image_limit)
    except Exception:  # noqa: BLE001
        it_images = []
    # Передаём запас кандидатов: campaign.py загрузит до 5 УСПЕШНО принятых Direct файлов.
    it_images = list(dict.fromkeys(it_images))[:12]
    try:                                          # видео per-кодер (ct): per-модель → фолбэк на логин
        # brand_hint=c_brand: для «Марки»-ct feeds_ct_model()[ct]=None → brand_word="" → пул пуст
        # (D3-create 2026-07-09, зеркало tp1-аудита MEMORY 2026-07-07). Прокидываем марку кодера,
        # чтобы videos_for_ct нашёл ролики через brand-fallback (BAIC/Belgee/Haval/Москвич).
        it_videos = (kp.videos_for_ct(login, c_ct, brand_hint=(c_brand or "")) if c_ct else []) \
            or kp.videos_for_login(login)
    except Exception:  # noqa: BLE001
        it_videos = []
    it_warnings: list[str] = []
    if it_targeting_warnings:
        it_warnings.extend(it_targeting_warnings)
    if c_brand and re.search(r"(?i)\bhaval\b|хавал", c_brand) and not it_videos:
        it_warnings.append("video_missing: Haval")
    # Бюджет — из «Глобальных правил» (НЕ дефолт): cpa→budget, tcpa→cpc_budget.
    it_budget = _num(it.get("budget"), rs["cpc_budget"] if it.get("pay") == "tcpa" else rs["budget"])
    # Fix A: UAC (tp6/tp7) может получить сырой структурный слаг вместо красивого имени
    # (если items пришли не через _emit_struct/_build_name). Детектируем по шаблону
    # tp[67]_cp[ac]_(site|kviz)_ct\d+_a(on|off)_ и пересобираем через _build_name.
    # Идемпотентно: красивое имя (содержит «—») не матчит regex → не переформатируется.
    if re.match(r'^tp[67]_cp[ac]_(site|kviz)_ct\d+_a(?:on|off)_', name):
        _r_code_fix, _oblast_fix = _resolve_region(ctx.get("city") or "")
        name = _build_name(
            is_master=not is_product,   # РЕГРЕССИЯ-ФИКС R2-8: строка была случайно удалена при правке DEFECT 3
            # DEFECT 3 (2026-07-10): is_auto ТОЛЬКО для autotarget. Было (!="keywords") —
            # audience тоже попадал в автотаргет-имя (ag001/aon), рассинхрон имя↔факт (в имени
            # «Автотаргетинг», по факту ручная аудитория). Совпадает с create_set_plan.py:637
            # (is_autotarget_name == "autotarget") и комментарием create_set_plan.py:79.
            is_auto=(targeting_mode == "autotarget"),
            pay=it.get("pay") or "cpa",
            r_code=_r_code_fix,
            oblast=_oblast_fix,
            sq=it_sq,
            cat=(it.get("position_name") or it.get("audience_cat") or None),
            ct=(c_ct or it.get("ct") or "ct0000"),
        )
    # Ручная аудитория («Настроить вручную») → отражаем в кодер-имени (aoff→aon).
    disp_name = name.replace("_aoff_", "_aon_", 1) if (is_manual and "_aon_" not in name) else name
    _feed_name = (it.get("feed_name") or "").strip()
    if is_product and _feed_name and _feed_name not in disp_name and not _is_site_domain_name(_feed_name, href):
        disp_name = f"{disp_name} — {_feed_name}"
    # tp7: фид «страницы каталога» = тот же, что под товары; иначе yandex-catalog.xml.
    it_feed = (_num(it.get("feed_id"), 0) or None) if is_product else None
    it_listings = _catalog_feed(_st_token, login, it_feed or 0, _w_agency or "") if is_product else None
    # tp7 фильтр по collectionId: показываем только модель кодера (не весь фид).
    # Загружаем фиды с модельными коллекциями лениво — один раз на весь запрос.
    it_lff = []                                  # listings_feed_filters
    it_ff = []                                   # feed_filters (товарная часть)
    if is_product and it_feed:
        # ЛЮБАЯ товарная tp7 получает фильтр: брендовый ct → позитив по марке/модели + минус-марки;
        # ct0000/общая → ТОЛЬКО минус-марки из глобальных правил (правило Семёна; раньше гейт
        # c_ct != "ct0000" оставлял общие товарки ВООБЩЕ без фильтров — FEED_FILTER_MISSING_UAC).
        it_ff = _tp7_product_feed_filters(c_brand or "", c_ct or "ct0000", login=login, feed_id=it_feed or 0)
    if is_product and it_feed and c_brand and c_ct and c_ct != "ct0000" and c_ct != "ct0111":
        if _tp7_mf is None:                      # ленивая загрузка (не дёргать API на каждый item)
            _tp7_mf = _account_model_feeds(login, _w_agency or "")
        fm_entry = next((f for f in _tp7_mf if f["id"] == it_feed), None)
        feed_models = fm_entry["models"] if fm_entry else None
        coll_id = _match_collection(c_brand, feed_models) if feed_models else None
        # coll_id найден → позитивный фильтр по коллекции (брендовая tp7).
        # Без coll_id — весь фид (марка или неизвестная модель): it_lff остаётся [].
        if coll_id:
            it_lff = [{"conditions": [{"field": "collectionId", "operator": "CONTAINS",
                                       "value": f'["{coll_id}"]'}]}]
    elif is_product and it_feed:
        if not c_brand or not c_ct or c_ct == "ct0000":
            # Небрендовые группы: ct0000 «Страницы каталога» ИЛИ любая товарка без марки
            # (напр. ct0014 «Общие запросы») — БЕЗ mark-фильтров → весь каталог (все 198+ стр.).
            # Такие страницы НЕ входят в mark_* коллекции → CONTAINS(mark_*) даёт 0 результатов.
            # UAC принимает фильтр без Exception → except-retry-без-фильтра (ниже) не срабатывает.
            # Требование Семёна: небрендовым группам mark-фильтр НЕ ставим.
            it_lff = []
        else:
            # брендовая товарка (есть c_brand, но не попала в модельную ветку выше, напр. ct0111):
            # allow-list по mark_* с вычетом минус-марок.
            it_lff = _tp7_listings_minus_filters(login, it_feed, _w_agency or "")
    try:
        spec = cmc.MasterCampaignSpec(
            href=it_href, titles=it_titles, texts=it_texts, region_ids=region_ids,
            counter_id=counter_id, goal_id=goal_id, cpa=_num(it.get("cpa"), cpa),
            week_budget=it_budget,
            display_name=disp_name, sitelinks=it_sitelinks,
            image_files=it_images, video_files=it_videos,
            image_limit=5,
            # dmp (не-авто лидоген): ОТКЛЮЧАЕМ pHash-дедуп картинок в collect_image_files —
            # доменные баннеры одного шаблона (тёмный фон + разный текст) РАЗНЫЕ для Директа,
            # pHash их ошибочно схлопывал (доезжало 2 из ~50). md5-дедуп остаётся. Авто-слепки
            # (_sk != "dmp") → True: pHash-защита от клонов сохранена.
            visual_dedup=(_sk != "dmp"),
            campaign_type="product" if is_product else "master",
            feed_id=it_feed,
            listings_feed_id=(it_listings or None) if is_product else None,
            feed_filters=it_ff,                 # товарка tp7: фильтр по модели/марке, не по всему фиду
            listings_feed_filters=it_lff,        # фильтр по collectionId (tp7-only; [] = весь фид)
            keywords=it_keywords,
            # DEFECT 3 (2026-07-10, эталон Семёна по кабинету 712694741): tp7-АВТОТАРГЕТ должен
            # рендериться как «Подобрать оптимальную» (Аудитория), а не «Настроить вручную». При
            # keywords=[]/audiences=[] ЕДИНСТВЕННЫЙ ручной сигнал в payload — minus_keywords (глоб.
            # «отзывы») → UAC-товарка (product) флипает блок «Аудитория» в «Настроить вручную».
            # Требование Семёна: для tp7-автотаргета минус-слова НЕ нужны → шлём []. Ручные режимы
            # tp7 (keywords/audience) — минус-слова как есть. tp6-мастер не тронут (рендерит верно).
            minus_keywords=([] if (is_product and targeting_mode == "autotarget")
                            else list(dict.fromkeys((it_minus_keywords or []) + _enabled_minus_words()))),
            audiences=it_audiences,
            audience_interest_type="short-term",
            # #7: группа ТОЛЬКО автотаргетинг → «Подобрать оптимальную» (HAR 34): пустые
            # keywords/audiences (уже так выше) + optimal-категории + полный socdem (age_18).
            # Прочие режимы (ручные интересы/КС) — прежний набор и socdem.
            relevance_match_categories=(_TP67_OPTIMAL_CATEGORIES if targeting_mode == "autotarget"
                                        else _TP67_RELEVANCE_CATEGORIES),
            alternative_texts_enabled=False,   # #3 персонализация (адаптивные тексты) ВЫКЛ
            # Явный per-position pricing слепка имеет приоритет (PER_CLICK|PER_CONVERSION|PER_ACTION);
            # пусто/невалидно → derive из pay: tcpa=оплата за клики (PER_CLICK), cpa=за конверсии (PER_CONVERSION).
            pricing=(str(it.get("pricing")).strip().upper()
                     if str(it.get("pricing") or "").strip().upper()
                     in ("PER_CLICK", "PER_CONVERSION", "PER_ACTION")
                     else ("PER_CONVERSION" if it.get("pay") == "cpa" else "PER_CLICK")),
            # Возраст 35+ (age_35) — для ВСЕХ ручных режимов tp6/Мастер (keywords И audience):
            # DoD §3.6 требует исключить ОБА младших брекета (18-24 И 25-34) → socdem-range стартует
            # с 35-44 (age_lower=age_35, age_upper=age_inf по дефолту). Раньше стоял age_25 (исключал
            # только 18-24) — пробел DoD, исправлено 2026-07-09. Значение age_35 — та же enum-семья
            # Яндекс-socdem, что age_18/age_25 (границы брекетов 18/25/35/45/55).
            # Исключения: автотаргетинг (#7 выше — полный socdem age_18 по дизайну) и товарка tp7
            # (age_18 всегда — возраст не настраивается). ag_part6 в имени = ag011 («ручной возраст»,
            # НЕ привязан к конкретной границе) — синхронизирован тем же условием, create_set_plan.py.
            age_lower=("age_18" if (targeting_mode == "autotarget" or is_product) else "age_35"),
        )
        try:
            cid = client.create_master_campaign(spec, launch=launch)
        except Exception as _lff_err:  # noqa: BLE001
            if it_lff:
                # listings_feed_filters для ct0000 отклонён UAC (невалидный collectionId,
                # field unavailable, MUST_BE_NULL и т.п.) → ретрай без фильтра (весь каталог).
                # Принцип ERRORS_JOURNAL #TP7_LISTING_FILTER_ZERO: «лучше весь каталог
                # чем несозданная кампания».
                _lff_warn = ("listings_feed_filters отклонён UAC, кампания создана без фильтра "
                             f"(весь каталог). Причина: {str(_lff_err)[:120]}")
                spec.listings_feed_filters = []
                try:
                    cid = client.create_master_campaign(spec, launch=launch)
                except Exception:
                    raise _lff_err from None  # ретрай тоже упал — бросаем оригинал
                it_warnings.append(_lff_warn)
            else:
                raise
        _res = {"name": disp_name, "ok": True, "id": cid, "launched": launch,
                "images": len(it_images), "videos": len(it_videos),
                "sitelinks": len(it_sitelinks),
                "url": f"https://direct.yandex.ru/wizard/campaigns/{cid}/?ulogin={login}"}
        if it_warnings:
            _res["warnings"] = it_warnings
        results.append(_res)
        _bump_job(_job, True)
    except Exception as e:  # noqa: BLE001 — ошибку по кампании показываем, набор не валим
        results.append({"name": disp_name, "ok": False, "error": str(e)[:240]})
        _add_job_err(_job, str(e)[:240])
        _bump_job(_job, False)
    _bump_item(_job)                                 # item завершён (tp6/tp7 fall-through)
    if _job:
        _job_db_progress(_job)
    return results, _tp7_mf

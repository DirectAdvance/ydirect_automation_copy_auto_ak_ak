"""tp6/tp7 master/product item handler for create_set."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from ..core import campaign as cmc
from . import create_set_context as _csctx  # _parse_targeting_modes (чистый хелпер, без configure)
from .. import kontent_pack as kp
from ..link_check import resolve_or_fallback_url as _resolve_url
from ..uac_verifier import UAC_IMAGES_MIN as _UAC_IMAGES_MIN
from ..uac_verifier import images_create_min as _uac_images_create_min
from ..model_urls import (_brand_level_url, _is_degenerate_feed_url, _model_page_href,
                         _strip_site_domain_label, _strip_url_query)
from ..text_norm import _cap_first


def _site_root_href(href: str) -> str:
    """Root site URL for feed/model links even when campaign landing is /quiz."""
    raw = str(href or "").strip()
    try:
        p = urlsplit(raw)
        if p.scheme and p.netloc:
            return urlunsplit((p.scheme, p.netloc, "", "", ""))
    except Exception:  # noqa: BLE001
        pass
    return raw.rstrip("/")


def _pavlov_multibrand_titles(brand: str = "") -> list[str]:
    """Приоритетный fallback для Павлова: финансовая подпись вместо generic credit-pool."""
    b = (brand or "").strip().split()[0] if (brand or "").strip() else ""
    if b:
        return [
            f"{b} без наценки. Выгода до 900 000 ₽ в кредит",
            f"{b} в кредит. КАСКО на год и взнос 0 ₽",
            f"Купить {b}. Одобрение 98% и 3 платежа за наш счёт",
            f"{b} в наличии. Убрали наценку на новые авто",
            f"{b} трейд-ин. Выгода до 900 000 ₽ в кредит",
        ]
    return [
        "Новые авто без наценки. Выгода до 900 000 ₽",
        "Кредит на новые авто. КАСКО на год и взнос 0 ₽",
        "Одобрение 98%. Выгода до 900 000 ₽ в кредит",
        "3 платежа за наш счёт. Кредит на новые авто",
        "Выбор марок. Убрали наценку на новые авто",
    ]


def _pavlov_multibrand_texts(brand: str = "") -> list[str]:
    b = (brand or "").strip().split()[0] if (brand or "").strip() else "новое авто"
    return [
        f"Купить {b} в кредит. Убрали наценку. Выгода до 900 000 ₽. Оставьте заявку!",
        # ⚠️ ≤81 симв. по конструкции: при b="новое авто" строка была 82 симв. и `_trim_to_word`
        # рубил её до «…Одобрение 98%. Рассчитайте» (оборванный призыв в live, 2026-07-28).
        f"{_cap_first(b)} в наличии. КАСКО на год и взнос 0 ₽. Одобрение 98%. Заявка онлайн!",
        "Выберите марку в наличии. Трейд-ин выше рынка и 3 платежа за наш счёт.",
    ]


def _pavlov_multibrand_sitelinks() -> list[dict]:
    return [
        {"title": "Убрали наценку", "description": "Зафиксируйте выгоду по кредитной программе"},
        {"title": "Одобрение до 98%", "description": "Проверим заявку и подберём банк под покупку"},
        {"title": "КАСКО на год", "description": "КАСКО на 1 год при покупке нового авто в кредит"},
        {"title": "3 платежа за наш счёт", "description": "Компенсируем 3 платежа по кредитной программе"},
    ]


# Смысловая «голова» числовой добивки: без числа она сама по себе пустая («Выгода», «Платёж»).
_UAC_NUM_HEAD = r"(?:выгода|скидка|плат[её]ж|цена|одобрение|взнос|кредит)"
_UAC_DANGLING_NUM_RE = re.compile(
    r"(?i)(?:\s|^)(?:" + _UAC_NUM_HEAD + r"\s+)?(?:от|до|за|с)\s+\d[\d\s]*(?:[.,;:!—-]\s*)?$")
_UAC_DANGLING_HEAD_RE = re.compile(
    r"(?i)(?:\s|^)(?:" + _UAC_NUM_HEAD + r"\s+)?(?:от|до|за)\s*$")


def _strip_dangling_uac_tail(text: str) -> str:
    """Убрать обрезанные хвосты UAC-текстов: «от 9», «за 15», «до 900» без единицы/объекта."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if not s:
        return ""
    # Вместе с числом снимаем и его смысловую «голову» («Выгода до 900» → пусто, а не «Выгода»).
    s = re.sub(_UAC_DANGLING_NUM_RE, "", s).strip()
    # Висячая связка без числа («Выгода до», «Платёж от») — остаток обрезки числовой добивки.
    s = re.sub(_UAC_DANGLING_HEAD_RE, "", s).strip()
    return _cap_first(s.rstrip(" .,!;:—-"))


# ── Числовая «добивка» UAC-контента ────────────────────────────────────────────────────────
# ⛔ Голый фрагмент («до 45%», «от 900 000 ₽») НЕ является законченной фразой: приклеенный к
# призыву он давал живой бред «Оставьте заявку до 45%» (Мастер, 2026-07-28). Поэтому:
#   1) берём из собственного контента кампании ЦЕЛЫЙ сегмент с числом («Выгода до 45%»,
#      «Платёж от 9 000 ₽/мес»), а не regex-обрывок;
#   2) к призыву цепляем хвост, ТОЛЬКО если он согласуется с ним грамматически
#      (обстоятельство времени «за 15 минут»); иначе сегмент идёт ОТДЕЛЬНЫМ предложением;
#   3) не влезло в лимит — строка остаётся короче, но осмысленной (кандидат без числа
#      отсеется number-гейтом вызывающего кода).
_UAC_NUM_RE = re.compile(
    r"(?i)\b(?:до|от)\s+\d[\d\s]*(?:[%₽]|руб\.?|р\.|тыс\.?|млн\.?|платеж\w*|минут\w*)?"
)
# Хвост, синтаксически согласованный с «Оставьте заявку …»: только обстоятельство времени.
_UAC_CTA_COMPAT_RE = re.compile(r"(?i)^(?:за|в течение)\s+\d")
_UAC_CTA_HINT = "за 15 минут"          # согласуется с призывом («Оставьте заявку за 15 минут»)
_UAC_NUM_CLAUSE_FALLBACK = "Одобрение за 15 минут"   # законченная фраза для НЕ-призывной позиции
_UAC_NUM_CLAUSE_MAX = 34               # длиннее — это целое предложение источника, не «хвост»


def _uac_number_clause(sources) -> str:
    """Законченный сегмент с числом из собственного контента кампании («Выгода до 45%»)."""
    for _src in sources or []:
        s = re.sub(r"\s+", " ", str(_src or "")).strip()
        if not s:
            continue
        for seg in re.split(r"[.!?]+", s):
            seg = seg.strip().strip(" .,!;:—-")
            if not seg or len(seg) > _UAC_NUM_CLAUSE_MAX or not _UAC_NUM_RE.search(seg):
                continue
            return _cap_first(seg)
    return ""


def _attach_cta_hint(base: str, clause: str, limit: int) -> str:
    """«{base}. Оставьте заявку …» с грамматически совместимым хвостом (или без него)."""
    b = str(base or "").strip().rstrip(" .,!;:—-")
    if not b:
        return ""
    c = str(clause or "").strip().rstrip(" .,!;:—-")
    cands: list[str] = []
    if c and _UAC_CTA_COMPAT_RE.match(c):
        cands.append(f"{b}. Оставьте заявку {c.lower()}")
    elif c:
        cands.append(f"{b}. {_cap_first(c)}. Оставьте заявку")
    cands.append(f"{b}. Оставьте заявку {_UAC_CTA_HINT}")
    for cand in cands:
        if len(cand) <= limit:
            return cand
    return b


def _attach_number_clause(base: str, clause: str, limit: int) -> str:
    """Добить строку числом ОТДЕЛЬНЫМ предложением, а не обрывком через пробел."""
    b = str(base or "").strip().rstrip(" .,!;:—-")
    if not b:
        return ""
    c = str(clause or "").strip().rstrip(" .,!;:—-") or _UAC_NUM_CLAUSE_FALLBACK
    cand = f"{b}. {_cap_first(c)}"
    return cand if len(cand) <= limit else b


def _strip_online_word(text: str) -> str:
    s = re.sub(r"(?i)(?:\s+|^)онлайн(?=\s|[.,!?;:]|$)", " ", str(text or ""))
    s = re.sub(r"\s+([.,!?;:])", r"\1", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s.rstrip(" .,!;:—-")


def _limit_online_density(items: list[str], max_with_online: int) -> list[str]:
    """Reduce repetitive 'онлайн' in one UAC campaign without changing meaning."""
    out: list[str] = []
    used = 0
    for raw in items or []:
        s = str(raw or "").strip()
        if not s:
            continue
        if re.search(r"(?i)\bонлайн\b", s):
            if used >= max_with_online:
                s = _strip_online_word(s)
            else:
                used += 1
        out.append(s)
    return out


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
    _account_offer_urls = deps['_account_offer_urls']
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
    _drop_new_car = deps['_drop_new_car']
    _drop_used_car = deps['_drop_used_car']
    _enabled_minus_words = deps['_enabled_minus_words']
    _fallback_master_titles = deps['_fallback_master_titles']
    _first_url_feed = deps['_first_url_feed']
    _fill_title = deps['_fill_title']
    _fill_variants = deps['_fill_variants']
    _has_number = deps['_has_number']
    _image_ct_for_content = deps['_image_ct_for_content']
    _is_bad_start = deps['_is_bad_start']
    _is_bu_site = deps['_is_bu_site']
    _is_common_ct = deps['_is_common_ct']
    _job_db_progress = deps['_job_db_progress']
    _lines = deps['_lines']
    _match_collection = deps['_match_collection']
    _feed_url_for_model = deps['_feed_url_for_model']
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
    _tp7_listing_plus_filter = deps['_tp7_listing_plus_filter']
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
    # Режим позиции — ТОЛЬКО явный из структуры/плана. Регулярка по ИМЕНИ (`_tp67_targeting_mode`)
    # источником режима больше НЕ является (Д7, Семён 2026-07-19): «МК - Общая - Автотаргетинг»
    # с 416 ключами и 9 аудиториями в структуре уходила в autotarget и теряла и то, и другое.
    # Окончательный набор режимов считается ниже, ПО СОДЕРЖИМОМУ структуры (после it_tp/c_ct).
    _raw_mode = str(it.get("targeting_mode") or ("keywords" if is_manual else "")).strip()
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
    # ── Режим таргетинга tp6/tp7 ПО СОДЕРЖИМОМУ СТРУКТУРЫ (Д7, Семён 2026-07-19) ──────────────
    # «если в тп6-тп7 нет ключей и аудиторий, то это по умолчанию ---autotargeting, и не важно
    # какое будет название». Эталон берём ПРЯМО из структуры/пака (tp67_struct_expectations),
    # а не из того, что решил план: обрыв на этапе плана иначе остаётся невидимым.
    # Явный режим (_raw_mode) только ДОБАВЛЯЕТ режим — тогда keyword_source-гейт ниже
    # продолжает ловить «объявлено КС, а корпус пуст», а не молчит.
    # pos_key — СТАБИЛЬНЫЙ ключ позиции из плана. Матч по display-имени здесь промахивается на
    # позициях с плейсхолдером «ГОРОД»: план подставляет город в имя, а структура читается сырой
    # («ТК - Общая - Автотаргетинг - ГОРОД») → 0 ключей и молчаливый autotarget.
    _struct_exp = _csctx.tp67_struct_expectations(
        agent, eff_site, it_tp, c_ct or str(it.get("ct") or "ct0000"), ctx.get("city") or "",
        it.get("position_name") or it.get("audience_cat") or name, it_sq,
        pos_key=str(it.get("pos_key") or ""))
    if not _struct_exp.get("matched"):
        it_targeting_warnings_early = (
            f"tp6/tp7 позиция не сопоставлена со структурой: slepok={agent}, site_type={eff_site},"
            f" tp={it_tp}, pos_key={it.get('pos_key')!r}, "
            f"position={(it.get('position_name') or name)!r} — ключи/аудитории НЕ подтянуты")
        _add_job_err(_job, it_targeting_warnings_early)
    _modes = _csctx._tp67_modes_from_content(
        bool(_struct_exp["keywords"]), bool(_struct_exp["audiences"]), _raw_mode)
    _want_keywords = "keywords" in _modes
    _want_audience = "audience" in _modes
    targeting_mode = "keywords" if _want_keywords else ("audience" if _want_audience else "autotarget")
    # Аудитории САМОЙ позиции структуры важнее merged-фолбэка `_slepok_audiences_for` (он
    # объединяет ВСЕ категории слепка). Явный interest_ids из плана по-прежнему главнее.
    if not (it.get("interest_ids") or []) and _struct_exp["audiences"]:
        native_ints = [str(x) for x in _struct_exp["audiences"]]
    # Яндекс лимит «Интересы и поисковые запросы» в МК/UAC — 30 интересов.
    # Режем здесь (ближе к источнику), uac_client.py добавляет safety-net.
    if len(native_ints) > 30:
        _warn = (f"tp6/tp7 интересов {len(native_ints)} > 30 (лимит МК/UAC): "
                 f"slepok={agent}, site_type={eff_site}, tp={it_tp} → обрезано до 30")
        native_ints = native_ints[:30]
        _add_job_err(_job, _warn)
    it_keywords: list[str] = []
    it_minus_keywords: list[str] = []
    it_targeting_warnings: list[str] = []
    if _want_keywords:
        # Тот же group-aware поиск, что дал эталон структуры (`_struct_exp`) — иначе создание
        # читало бы ЛЕГАСИ-файл пака без gk-слага и получало 0 ключей при непустой структуре.
        it_keywords, it_minus_keywords = _struct_exp["keywords"], _struct_exp["minus"]
        if not it_keywords:
            # Ключей нет НИ в паке слепка, НИ в его собственной библиотеке реальных UAC-payload
            # (чужой слепок источником быть не может — жёсткий фильтр create_set_context._score).
            _err = (f"tp6/tp7 КС без ключей: slepok={agent}, site_type={eff_site}, "
                    f"tp={it_tp}, ct={c_ct or it.get('ct') or 'ct0000'}, "
                    f"position={(it.get('position_name') or it.get('audience_cat') or name)!r} "
                    f"(нет в паке и в СВОЕЙ библиотеке; заимствование у другого слепка запрещено)")
            if _keyword_source:
                # keyword_source-ГЕЙТ: позиция ЯВНО объявлена keyword-driven в структуре, но корпус
                # ключей пуст → НЕ уходить молча в autotarget (это баг). Блокируем позицию с явной
                # ошибкой в DoD/логе; кампанию НЕ создаём.
                # ⚠️ Ветки `or _struct_exp["keywords"]` здесь НЕТ намеренно: `it_keywords` — это и
                # есть `_struct_exp["keywords"]`, поэтому внутри `if not it_keywords` она всегда
                # ложна и создавала лишь ВИД работающей проверки. Реальный рубеж против потери
                # «структура обещала ключи, а в кабинет не доехали» — сверка UAC_STRUCT_* по
                # эталону `_res["struct"]`, а не этот гейт.
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
                f"site_type={eff_site}, tp={it_tp}, category={it.get('audience_cat') or it.get('position_name') or ''}"
                # Счётчик СТРУКТУРЫ в тексте — чтобы потеря «структура обещала N, отправили 0»
                # была видна в errors_log, а не только факт «режим audience без объектов».
                f", struct_audiences={len(_struct_exp['audiences'])}"
                + (f", не поддержано UAC-путём={_struct_exp['audiences_unsupported']}"
                   if _struct_exp["audiences_unsupported"] else ""))
        _want_audience = False
        if not _want_keywords:
            targeting_mode = "autotarget"
        it_targeting_warnings.append(
            _err + " → fallback " + ("keywords" if _want_keywords else "autotarget"))
        _add_job_err(_job, it_targeting_warnings[-1])
    if is_product and c_brand and c_ct and c_ct != "ct0000":
        # tp7 Марки/Модели: посадочная должна соответствовать кодеру группы.
        # Марки → страница марки (/auto/haval), Модели → точный URL модели из фида, при промахе
        # формульная модельная страница. Иначе UAC берёт base href и превью показывает случайный товар.
        _is_brand_only = len([p for p in re.split(r"\s+", c_brand.strip()) if p]) == 1
        _raw_feed_url = ""
        _site_href = _site_root_href(href)
        try:
            _feed_urls = _account_offer_urls(login, _site_href)
            _raw_feed_url = (_feed_url_for_model(_feed_urls, c_brand,
                                                 no_brand_fallback=(not _is_brand_only)) or "")
        except Exception:  # noqa: BLE001 — URL-фолбэк ниже
            _raw_feed_url = ""
        # AD_HREF_ROOT_INSTEAD_OF_MODEL: вырожденный URL из фида (квиз-оффер `/quiz?fid=…`,
        # голый корень) игнорируем — link_check ниже схлопнул бы его в главную.
        if _raw_feed_url and not _is_degenerate_feed_url(_raw_feed_url):
            it_href = _brand_level_url(_raw_feed_url) if _is_brand_only else _strip_url_query(_raw_feed_url)
        else:
            it_href = _model_page_href(_site_href, eff_site, c_brand)
    elif is_product and not c_brand and it_sq != "kviz":
        it_href = re.sub(r"/+$", "", href) + "/auto"
    it_href = _resolve_url(it_href)  # single-URL resolve: circuit-breaker + кэш, без overhead пула
    _sc = _slepok_campaign_content(agent, eff_site)
    _is_pavlov_multibrand = ((agent or "").strip().lower() == "pavlov"
                             and (eff_site or "").strip() == "Мультибренд")
    _pavlov_titles = _pavlov_multibrand_titles(c_brand if _is_pavlov_multibrand else "")
    _pavlov_texts = _pavlov_multibrand_texts(c_brand if _is_pavlov_multibrand else "")
    _pavlov_sitelinks = _pavlov_multibrand_sitelinks() if _is_pavlov_multibrand else []
    _llm_titles = _lines(it.get("titles"))
    _llm_texts = _lines(it.get("texts"))
    _live_generated_content = bool(_stream_agent and _llm_titles and _llm_texts and it.get("sitelinks"))
    if c_brand:                                    # кодер несёт модель -> ведём контент по ней
        _b0 = c_brand.split()[0].lower()
        _model_t = [t for t in _sc["titles"] if _b0 in t.lower()]
        title_primary = _llm_titles + _pavlov_titles + _brand_title_set(c_brand, ctx.get("city") or "") + _model_t
        title_supp = title_primary + _sc["titles"] + tpl_titles + _pavlov_titles + _GENERIC_TITLE_FILLERS
    elif is_product:
        # tp7 ct0000 (общая кампания) - заголовки ТОЛЬКО без марки/модели.
        # _sc["titles"] часто брендовые («Haval…», «Chery…») - НЕ используем как base для авто.
        # Для не-авто (dmp и будущие B2B): база = корпус слепка, авто-дженерики не подмешиваем.
        if _is_non_auto:
            _b2b_titles_p = [t for t in (_sc.get("titles") or []) if t and str(t).strip()]
            title_primary = _llm_titles + _b2b_titles_p
            title_supp = _llm_titles + _b2b_titles_p + tpl_titles
        else:
            title_primary = _llm_titles + _pavlov_titles + _GENERIC_AT_TITLES
            title_supp = _llm_titles + _pavlov_titles + _GENERIC_AT_TITLES + _GENERIC_TITLE_FILLERS
    else:                                          # tp6 МК ct0000 → авто: _GENERIC_AT_TITLES; не-авто: B2B-корпус
        if _is_non_auto:
            # Не-авто (dmp и будущие B2B): база = B2B-корпус слепка; авто-дженерики НЕ подмешиваем.
            # B2B-строки часто без цифр («Получайте контакты потенциальных клиентов…») —
            # поэтому number-gate ниже отключён для не-авто.
            _b2b_titles = [t for t in (_sc.get("titles") or []) if t and str(t).strip()]
            title_primary = _llm_titles + _b2b_titles
            title_supp = _llm_titles + _b2b_titles + tpl_titles
        else:
            # баг #8: _sc["titles"] часто содержат марки («Haval у дилера»…) — для общей МК (ct0000)
            # это неверно. Используем _GENERIC_AT_TITLES как base; _sc["titles"] только в supp-добавке.
            title_primary = _llm_titles + _pavlov_titles + _GENERIC_AT_TITLES
            title_supp = _llm_titles + _pavlov_titles + _sc["titles"] + tpl_titles + _GENERIC_TITLE_FILLERS
    # ЖЁСТКОЕ ПРАВИЛО tp6/tp7: РОВНО 5 заголовков / 3 текста / 8 быстрых ссылок.
    # ГОРОД В КОНТЕНТЕ обязан совпадать с городом аккаунта.
    _acc_city = (ctx.get("city") or "").strip()
    _, _cities_bl = _title2_blocklist()
    # Оба site_type-фильтра: `_drop_used_car` (Б/У-лексика на НОВОМ сайте) и парный ему
    # `_drop_new_car` («новые авто» на сайте «С пробегом»). Условия взаимоисключающие —
    # одновременно сработать и выкосить пул они не могут.
    _cf = lambda lst: _replace_foreign_city(  # noqa: E731
        _drop_new_car(_drop_used_car(lst, eff_site), eff_site), _acc_city, _cities_bl)
    if not c_brand:
        # ct0000 (общая кампания) — tp6 МК И tp7 Товарка: base = только общие заголовки,
        # но live LLM-заголовки идут первыми и проходят тот же фильтр брендовых токенов.
        # баг #8: фильтр брендовых токенов распространён на tp6 ct0000 (раньше только tp7).
        _t_base = list(_llm_titles if _live_generated_content else title_primary)
        _ct_brand_tokens: set = {v.split()[0].lower() for v in kp.feeds_ct_model().values() if v}
        _foreign_brand_tokens, _foreign_city_tokens = _title2_blocklist()
    else:
        _t_base = list(_llm_titles if _live_generated_content else title_primary)
        _ct_brand_tokens = set()
        _foreign_brand_tokens, _foreign_city_tokens = set(), set()

    def _common_content_ok(_s: str) -> bool:
        if c_brand:
            return True
        _words = set(re.sub(r"[^\wа-яё]+", " ", str(_s or "").lower()).split())
        return not (_words & _foreign_brand_tokens) and not (_words & _foreign_city_tokens)
    # Сборка заголовков с пост-обработкой (БАГ 1+2+3+4+7+8)
    # Для не-авто (B2B): _GENERIC_TITLE_FILLERS — авто-кредитные, не добавляем в пул.
    # `_pavlov_titles + _GENERIC_TITLE_FILLERS` приклеивались сырыми мимо `_cf` — на Б/У-сайте
    # это протаскивало «Кредит на новый авто…»/«Новые авто в наличии…». Оборачиваем в `_cf`.
    _title_fill_pool = (_cf(_llm_titles) if _live_generated_content
                        else _cf(title_supp) + ([] if _is_non_auto else _cf(_pavlov_titles + _GENERIC_TITLE_FILLERS)))
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
            _x_base = _llm_texts if _live_generated_content else (_b2b_texts or _GENERIC_TEXT_FILLERS)
        elif _is_pavlov_multibrand:
            _x_base = _llm_texts if _live_generated_content else (_pavlov_texts + _GENERIC_TEXT_FILLERS)
        else:
            _x_base = _llm_texts if _live_generated_content else _GENERIC_TEXT_FILLERS
    else:
        _x_base = _llm_texts if _live_generated_content else (
            _pavlov_texts + _rsya_texts((_llm_texts or []) + (_sc["texts"] or []),
                                        eff_site, ctx.get("city") or "", c_brand, cap=8)
        )
    # Для не-авто: _GENERIC_TEXT_FILLERS авто-кредитные — не добавляем в пул.
    # Тот же сырой хвост для текстов: `_GENERIC_TEXT_FILLERS` нёс «Кредит без первого взноса
    # на новое авто…» мимо `_cf`. Прогоняем весь пул через site_type-фильтр.
    _text_fill_pool = (_cf(_llm_texts) if _live_generated_content
                       else _cf(_pavlov_texts + tpl_texts) + ([] if _is_non_auto else _cf(_GENERIC_TEXT_FILLERS)))
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
    _sl_seed = list(_sl_base or []) if _live_generated_content else (_pavlov_sitelinks + _sl_base)
    _sl_fill = [] if _live_generated_content else (tpl_sitelinks + _pavlov_sitelinks + _GENERIC_SITELINK_FILLERS)
    for _s in _fill_variants(_sl_seed, _sl_fill, 16):
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
        for _s in _pavlov_sitelinks + _GENERIC_SITELINK_FILLERS:
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
        from .. import ai_agents as _A
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
            from .. import ai_agents as _A
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
    if len(it_titles) < 5 and not _is_non_auto and not _live_generated_content:
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
        _txt_fallback = [] if (_is_non_auto or _live_generated_content) else (_pavlov_texts + list(_GENERIC_TEXT_FILLERS))
        it_texts = _diverse_text_offers(
            _dedup_prefix_absorb(it_texts + _txt_fallback), 3)
    # R2-4 2026-07-10 (c1): topic/semantic-дедуп быстрых ссылок ВСЕГДА (не только при <8).
    # UAC-путь (tp6/tp7) собирал 8 сайтлинков с дедупом ТОЛЬКО по ТОЧНОЙ строке title+desc →
    # кредитный дубль «Платеж от 9 000 ₽/мес» + «Автокредит от 9 000 ₽/мес» проходил как две
    # разные строки (bucket credit≤2 их пропускал), хотя _coherent_payments свёл ОБЕ суммы к 9000
    # → семантически это ОДИН оффер (credit_pay|9000). _dedup_sitelinks (bucket-лимиты +
    # _sitelink_semantic_key) их схлопывает, но раньше вызывался ТОЛЬКО в ветке <8 → при полном
    # комплекте 8 не отрабатывал = «UAC мимо дедупа». Порядок diversify→dedup — как на tp1/tp2/tp5.
    _pre_semantic_sitelinks = list(it_sitelinks or [])
    try:
        from .. import ai_agents as _A
        _dl = _A._dedup_sitelinks(_A.diversify_sitelink_utp(it_sitelinks, 8), eff_site, 8)
        if _dl and ((not _live_generated_content) or len(_dl) >= 8):
            it_sitelinks = _dl
    except Exception:  # noqa: BLE001
        pass
    if len(it_sitelinks) < 8 and not _live_generated_content:
        _sl_candidates = list(it_sitelinks or [])
        for _s in _pavlov_sitelinks + _GENERIC_SITELINK_FILLERS:
            _st = _trim_to_word(_sanitize_content(_s.get("title", ""), max_len=30), 30).rstrip()
            _sd = _trim_to_word(_sanitize_content(_s.get("description", ""), max_len=60), 60).rstrip()
            if (_st and _sd and not _bad_ad_sitelink(_st, _sd)
                    and _common_content_ok(f"{_st} {_sd}")
                    and not (not _is_bu_site(eff_site) and _BU_RE.search(f"{_st} {_sd}"))):
                _sl_candidates.append({"title": _st, "description": _sd})
        try:
            from .. import ai_agents as _A
            it_sitelinks = _A._dedup_sitelinks(_sl_candidates, eff_site, 8)
        except Exception:  # noqa: BLE001
            it_sitelinks = _sl_candidates[:8]
    # R2-4 2026-07-10 (c2+d): account-pass — единая сумма платежа (d, дефолт ON) и, под флагом
    # DIRECT_SITELINK_REUSE_ACCOUNT (c2, дефолт OFF), ОДИН согласованный набор сайтлинков на
    # весь аккаунт (tp1/tp2/tp5/tp6/tp7). Сайтлинки эталона re-фильтруем per-item гардами
    # (pct/BU/лимит) — несовместимый эталон → остаёмся на своём наборе (brand-first/лимиты целы).
    try:
        from .. import ai_content as _AC
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

    def _soften_uac_sitelink_text(_s: str) -> str:
        _s = re.sub(r"(?i)\bс\s+выгод\w*\s+до\s+-?\d+\s*%", "с выгодными условиями", _s or "")
        _s = re.sub(r"(?i)\bсо\s+скидк\w*\s+до\s+-?\d+\s*%", "со спецусловиями", _s)
        _s = re.sub(r"(?i)\bскидк\w*\s+до\s+-?\d+\s*%", "Спецусловия", _s)
        _s = re.sub(r"(?i)\bвыгод\w*\s+до\s+-?\d+\s*%", "Выгодные условия", _s)
        return re.sub(r"\s{2,}", " ", _s).strip(" .")

    def _append_uac_sitelink(_title: str, _desc: str) -> None:
        if len(_uniq_sl) >= 8:
            return
        _st = _trim_to_word(_sanitize_content(_soften_uac_sitelink_text(_title), max_len=30), 30).rstrip()
        _sd = _trim_to_word(_sanitize_content(_soften_uac_sitelink_text(_desc), max_len=60), 60).rstrip()
        _tk, _dk = _st.lower(), _sd.lower()
        if not _st or not _sd or _tk in _seen_titles or (_dk and _dk in _seen_descs):
            return
        if not _is_bu_site(eff_site) and _BU_RE.search(f"{_st} {_sd}"):
            return
        if _title_has_pct and _sitelink_has_pct({"title": _st, "description": _sd}):
            return
        if _bad_ad_sitelink(_st, _sd) or not _common_content_ok(f"{_st} {_sd}"):
            return
        _seen_titles.add(_tk)
        if _dk:
            _seen_descs.add(_dk)
        _uniq_sl.append({"title": _st, "description": _sd})

    for _s in (it_sitelinks or []):
        if not isinstance(_s, dict):
            continue
        _append_uac_sitelink(str(_s.get("title") or ""), str(_s.get("description") or ""))
    if len(_uniq_sl) < 8 and _live_generated_content:
        for _s in list(_pre_semantic_sitelinks or []) + list(_sl_base or []):
            if len(_uniq_sl) >= 8:
                break
            if isinstance(_s, dict):
                _append_uac_sitelink(str(_s.get("title") or ""), str(_s.get("description") or ""))
    if len(_uniq_sl) < 8 and _live_generated_content:
        _live_descs = []
        for _x in list(it_texts or []):
            _first = re.split(r"[.!?]", str(_x or ""), maxsplit=1)[0].strip()
            if _first:
                _live_descs.append(_first)
        _live_descs.extend([
            "Оставьте заявку и получите расчёт за 15 минут",
            "Подберём условия и ответим онлайн по заявке",
            "Покажем варианты и условия покупки сегодня",
            "Проверим доступные условия по вашей заявке",
            "Расскажем детали и поможем выбрать авто",
            "Менеджер свяжется и уточнит условия сегодня",
            "Получите подборку вариантов по бюджету",
            "Сравним условия покупки и оформим заявку",
        ])
        _live_titles = []
        for _t in list(it_titles or []) + list(it_texts or []):
            _first = re.split(r"[.!?]", str(_t or ""), maxsplit=1)[0].strip()
            if _first:
                _live_titles.append(_first)
        for _i, _title in enumerate(_live_titles):
            if len(_uniq_sl) >= 8:
                break
            _desc = _live_descs[_i % len(_live_descs)] if _live_descs else "Оставьте заявку и получите расчёт онлайн"
            _append_uac_sitelink(_title, _desc)
    if len(_uniq_sl) < 8 and not _live_generated_content:
        for _s in _pavlov_sitelinks + _GENERIC_SITELINK_FILLERS:
            if len(_uniq_sl) >= 8:
                break
            _append_uac_sitelink(_s.get("title", ""), _s.get("description", ""))
    it_sitelinks = _uniq_sl[:8]
    if _live_generated_content and len(it_texts) < 3:
        _seen_live_texts = {_variant_norm_key(_x) for _x in it_texts if _variant_norm_key(_x)}

        def _uac_text_number_hint() -> str:
            return _uac_number_clause(
                list(it_titles or []) + list(_llm_titles or []) + list(_llm_texts or []))

        def _append_uac_live_text(_raw: str) -> None:
            if len(it_texts) >= 3:
                return
            _x = _sanitize_content(_soften_uac_sitelink_text(str(_raw or "")), max_len=81)
            _x = _trim_to_word(_strip_credit_rate(_x), 81).rstrip(" .")
            if _x and not _is_non_auto and not _has_number(_x):
                _x = _attach_number_clause(_x, _uac_text_number_hint(), 81)
            if (not _x or _is_bad_start(_x) or _bad_ad_text(_x) or not _common_content_ok(_x)
                    or (not _is_non_auto and not _has_number(_x))
                    or len(_x) < _TP67_MIN_TEXT_LEN):
                return
            _nk = _variant_norm_key(_x)
            if _nk and (_nk in _seen_live_texts or any(_variant_norm_key(_old) == _nk for _old in it_texts)):
                return
            if _nk:
                _seen_live_texts.add(_nk)
            it_texts.append(_x)

        for _x in list(_llm_texts or []) + list(it_texts or []):
            _append_uac_live_text(_x)
        for _s in list(_pre_semantic_sitelinks or []) + list(_sl_base or []) + list(it_sitelinks or []):
            if len(it_texts) >= 3:
                break
            if isinstance(_s, dict):
                _append_uac_live_text(_s.get("description") or _s.get("title") or "")
        for _t in list(it_titles or []) + list(_llm_titles or []):
            if len(it_texts) >= 3:
                break
            _append_uac_live_text(_t)
        it_texts = _diverse_text_offers(
            _dedup_prefix_absorb([
                _trim_to_word(_x, 81).rstrip() for _x in it_texts if _x and str(_x).strip()
            ]),
            3,
        )
    if _live_generated_content and len(it_texts) < 3:
        # Последний live-only guard перед hard-block: _diverse_text_offers может честно вернуть
        # только 2 текста, если все M3-формулировки попали в одни offer-buckets. Для live-пути
        # нельзя брать generic fallback, поэтому делаем короткие варианты из собственных
        # заголовков/ссылок этой же кампании и больше не прогоняем их через bucket-сжиматель.
        _hint = _uac_number_clause(
            list(it_titles or []) + list(_llm_titles or []) + list(_llm_texts or []))
        _emergency_seeds: list[str] = []
        for _s in list(_pre_semantic_sitelinks or []) + list(_sl_base or []) + list(it_sitelinks or []):
            if isinstance(_s, dict):
                _emergency_seeds.append(str(_s.get("description") or _s.get("title") or ""))
        _emergency_seeds.extend(str(_t or "") for _t in list(it_titles or []) + list(_llm_titles or []))
        for _seed in _emergency_seeds:
            if len(it_texts) >= 3:
                break
            _base = re.split(r"[.!?]", _soften_uac_sitelink_text(str(_seed or "")), maxsplit=1)[0].strip()
            if not _base:
                continue
            _x = _trim_to_word(_sanitize_content(_attach_cta_hint(_base, _hint, 81), max_len=81), 81).rstrip(" .")
            if (not _x or _is_bad_start(_x) or _bad_ad_text(_x) or not _common_content_ok(_x)
                    or (not _is_non_auto and not _has_number(_x))
                    or len(_x) < _TP67_MIN_TEXT_LEN):
                continue
            _nk = _variant_norm_key(_x)
            if _nk and any(_variant_norm_key(_old) == _nk for _old in it_texts):
                continue
            it_texts.append(_x)
    if _live_generated_content and len(it_titles) < 5:
        _title_hint = _uac_number_clause(
            list(it_titles or []) + list(it_texts or []) + list(_llm_titles or []) + list(_llm_texts or []))
        _title_seeds: list[str] = []
        _title_seeds.extend(str(_t or "") for _t in list(_llm_titles or []) + list(it_texts or []) + list(_llm_texts or []))
        for _s in list(_pre_semantic_sitelinks or []) + list(_sl_base or []) + list(it_sitelinks or []):
            if isinstance(_s, dict):
                _title_seeds.append(str(_s.get("title") or _s.get("description") or ""))
        _own = _own_brand_tokens(c_brand) if c_brand else set()
        _brand_prefix = (c_brand or "").strip().split()[0] if c_brand else ""
        for _seed in _title_seeds:
            if len(it_titles) >= 5:
                break
            _base = re.split(r"[.!?]", _soften_uac_sitelink_text(str(_seed or "")), maxsplit=1)[0].strip()
            if not _base:
                continue
            if _brand_prefix:
                _bl = _base.lower()
                if not any(re.search(r"(?<![a-zа-яё0-9])" + re.escape(tok) + r"(?![a-zа-яё0-9])", _bl)
                           for tok in _own):
                    _base = f"{_brand_prefix} {_base}"
            if not _is_non_auto and not _has_number(_base):
                _base = _attach_number_clause(_base, _title_hint, 56)
            _t = _sanitize_content(_base, max_len=56)
            _t = _fill_title(_replace_sep_hyphen(_replace_emdash(_strip_credit_rate(_t))), 45, 56)
            _t = _trim_to_word(_t, 56).rstrip(" .")
            if (not _t or _is_bad_start(_t) or _bad_ad_title(_t) or not _common_content_ok(_t)
                    or (not _is_non_auto and not _has_number(_t))):
                continue
            if _own:
                _tl = _t.lower()
                if not any(re.search(r"(?<![a-zа-яё0-9])" + re.escape(tok) + r"(?![a-zа-яё0-9])", _tl)
                           for tok in _own):
                    continue
            if _ct_brand_tokens:
                _tl = _t.lower()
                if any(_tok in _tl for _tok in _ct_brand_tokens):
                    continue
            _nk = _variant_norm_key(_t)
            if _nk and any(_variant_norm_key(_old) == _nk for _old in it_titles):
                continue
            it_titles.append(_t)
    if _live_generated_content and (len(it_titles) < 5 or len(it_texts) < 3 or len(it_sitelinks) < 8):
        _err = ("tp6/tp7 live-контент после UAC-фильтров неполный; шаблонный фолбэк запрещён: "
                f"заголовки {len(it_titles)}/5; тексты {len(it_texts)}/3; "
                f"быстрые ссылки {len(it_sitelinks)}/8")
        _add_job_err(_job, f"{name}: {_err}")
        results.append({"name": name, "ok": False, "error": _err, "blocked": True})
        _bump_job(_job, False)
        _bump_item(_job)
        if _job:
            _job_db_progress(_job)
        return results, _tp7_mf
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
    img_ct = "ct0000"  # инициализируем до try-блока: нужен ниже для preflight-сообщения
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
    _pool_size = len(it_images)                   # пул картинок ИМЕННО своего ct (без чужого/ct0000)
    try:                                          # видео per-кодер (ct): per-модель → фолбэк на логин
        # brand_hint=c_brand: для «Марки»-ct feeds_ct_model()[ct]=None → brand_word="" → пул пуст
        # (D3-create 2026-07-09, зеркало tp1-аудита MEMORY 2026-07-07). Прокидываем марку кодера,
        # чтобы videos_for_ct нашёл ролики через brand-fallback (BAIC/Belgee/Haval/Москвич).
        # slepok=_sk: ступень 1 «свой слепок» (kontent_pack.videos_for_ct) — ролики, залитые под
        # конкретного директолога, лежат в его папке _slepki_data/<слепок>/videos и берутся ПЕРВЫМИ;
        # общий пул — вторая ступень, чужой слепок — последняя (правило Семёна 2026-07-28).
        it_videos = (kp.videos_for_ct(login, c_ct, brand_hint=(c_brand or ""), slepok=_sk)
                     if c_ct else []) or kp.videos_for_login(login)
    except Exception:  # noqa: BLE001
        it_videos = []
    # ── Мягкий порог картинок (решение Семёна 2026-07-27) ────────────────────────────────────────
    # «Мне нужна кампания даже с 4 изображениями, это лучше чем вообще её не будет.»
    # Пул считаем ТЕМ ЖЕ каскадом (_creative_images_for_ct), что и реальная сборка → preflight и
    # факт не расходятся. Дефицит пула создание больше НЕ блокирует: кампания создаётся с тем, что
    # есть, а факт дефицита уезжает WARNING'ом в результат позиции и в `images_pool` (его читает
    # live-верификатор и НЕ считает ошибкой — см. uac_verifier.UAC_IMAGES_POOL_SHORT).
    # Цикл создать-удалить-пересоздать при этом не возвращается: при коротком пуле верификатор
    # выдаёт warn-код, которого нет ни в `repair_planner._RECREATE_CODES`, ни в
    # `repair_gate._UAC_REPLACE_CODES`, т.е. recreate-действие не планируется вовсе.
    # Единственный оставшийся блок — вырожденный: показывать нечего совсем (ни картинок, ни видео).
    # Порог блокировки задаётся env `DIRECT_UAC_IMAGES_CREATE_MIN` (дефолт 1; 0 = не блокировать
    # никогда, 5 = прежнее жёсткое поведение) — без правки кода.
    _create_min = _uac_images_create_min()
    _pool_warning = ""
    if _pool_size < _create_min and not it_videos:
        _gap = (
            f"CONTENT_GAP_NO_CREATIVE: для ct={img_ct} slepok={_sk} нет ни одной картинки "
            f"(пул {_pool_size} при пороге создания {_create_min}) и ни одного видео — "
            f"показывать в кампании нечего. Добавить PNG в Manual/{img_ct}/. "
            "Кампания не создана (нет ни одного креатива)."
        )
        _add_job_err(_job, f"{name}: {_gap}")
        results.append({
            "name": name, "ok": False, "error": _gap,
            "blocked": True, "issue_code": "CONTENT_GAP_NO_CREATIVE",
            "pool_size": _pool_size, "required": _create_min, "ct": img_ct,
        })
        _bump_job(_job, False)
        _bump_item(_job)
        if _job:
            _job_db_progress(_job)
        return results, _tp7_mf
    if _pool_size < _UAC_IMAGES_MIN:
        _pool_warning = (
            f"IMAGES_POOL_SHORT: пул картинок для ct={img_ct} slepok={_sk} — {_pool_size} шт. "
            f"(цель {_UAC_IMAGES_MIN}). Взяли все доступные; добор из чужого ct/ct0000 запрещён. "
            f"Кампания создаётся с {_pool_size} картинками — это предупреждение, не ошибка."
        )
    # ────────────────────────────────────────────────────────────────────────────────────────────
    it_warnings: list[str] = []
    if _pool_warning:
        it_warnings.append(_pool_warning)
    if it_targeting_warnings:
        it_warnings.extend(it_targeting_warnings)
    if c_brand and re.search(r"(?i)\bhaval\b|хавал", c_brand) and not it_videos:
        it_warnings.append("video_missing: Haval")
    # Бюджет — из «Глобальных правил» (НЕ дефолт): cpa→budget, tcpa→cpc_budget.
    it_budget = _num(it.get("budget"), rs["cpc_budget"] if it.get("pay") == "tcpa" else rs["budget"])
    # Fix A: UAC (tp6/tp7) может получить сырой структурный слаг вместо красивого имени
    # (если items пришли не через _emit_struct/_build_name). Детектируем по шаблону
    # tp[67]_cp[ac]_(site|kviz)_ct\d+_a(on|off)_ и пересобираем через _build_name.
    # ⚠️ ГРАБЛЯ (2026-07-21, живой дубль «Автотаргетинг» в 46 tp6/7-позициях): regex якорится
    # ТОЛЬКО в начало строки — план-имя ТОЖЕ начинается с этих кодов (формат `_build_name`),
    # « — » идёт уже ПОСЛЕ префикса, так что re.match срабатывал ВСЕГДА, а не только на сыром
    # слаге. Fix A выбрасывал уже чистое план-имя и пересобирал из недедупленного display
    # (`_slepok_struct_groups`), давая «ТК - Автотаргетинг - Общая - КС + Автотаргетинг».
    # Явная проверка отсутствия « — » (не regex-хвост) — план-имена пропускаются идемпотентно.
    if re.match(r'^tp[67]_cp[ac]_(site|kviz)_ct\d+_a(?:on|off)_', name) and " — " not in name:
        _r_code_fix, _oblast_fix = _resolve_region(ctx.get("city") or "")
        _tp_label_fix = "ТК" if is_product else "МК"
        _targeting_label_fix = _csctx.tp67_targeting_label_from_modes(_modes, "tp7" if is_product else "tp6")
        _cat_fix = _csctx.tp67_clean_position_name_for_targeting(
            it.get("position_name") or it.get("audience_cat") or "",
            _tp_label_fix,
        ) or None
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
            cat=_cat_fix,
            ct=(c_ct or it.get("ct") or "ct0000"),
            targeting_label=_targeting_label_fix,
            # Имя позиции из слепка — тот же источник, что у плана (`_emit_struct`), иначе
            # фолбэк-путь снова пересобрал бы имя по-своему и разошёлся бы со структурой.
            struct_name=it.get("position_name") or None,
        )
    # Ручная аудитория («Настроить вручную») → отражаем в кодер-имени (aoff→aon).
    disp_name = name.replace("_aoff_", "_aon_", 1) if (is_manual and "_aon_" not in name) else name
    # Метку фида для имени БЕРЁМ У ПЛАНА (`feed_label`, create_set_plan.py:1167) — ровно ту,
    # что план уже подставил в имя. Так равенство имён план↔билд — свойство КОНСТРУКЦИИ.
    # ⚠️ Пересчитывать метку здесь из `feed_name` нельзя: план считает её из URL фида, а кабинет
    #    отдаёт своё имя («Фид легковых», иной регистр, «feed-2024») → имена расходятся
    #    (FEED_FALLBACK_PLAN_VS_BUILDER_DESYNC: дубль-суффикс + рассинхрон already_in_direct).
    #    Прежнее совпадение держалось лишь на том, что имя кабинета оказывалось case-exact
    #    подстрокой имени файла — это везение данных, а не свойство кода.
    # Ключа нет (item НЕ из плана, путь «Fix A») → фолбэк: чистим имя кабинета от домена-префикса
    # (`_is_site_domain_name` ловил только ТОЧНОЕ равенство хосту → домен уезжал в имя).
    # Пусто на выходе (метка была только доменом) → суффикс не клеим.
    _feed_name = (str(it.get("feed_label") or "").strip() if "feed_label" in it
                  else _strip_site_domain_label(it.get("feed_name") or "", href))
    if is_product and _feed_name and _feed_name not in disp_name:
        disp_name = f"{disp_name} — {_feed_name}"
    # Тот же дедуп сегментов, что план применяет в `_uniq` ПОСЛЕ приклейки метки фида
    # (create_set_plan.py). Без него имя билда разошлось бы с именем плана в случае, когда
    # дедуп срезал повторный сегмент/чанк (класс FEED_FALLBACK_PLAN_VS_BUILDER_DESYNC).
    # Идемпотентно: имя из плана уже канонично → строка не меняется.
    disp_name = _csctx.dedup_name_segments(disp_name)
    # tp7: фид «страницы каталога» = тот же, что под товары; иначе yandex-catalog.xml.
    it_feed = (_num(it.get("feed_id"), 0) or None) if is_product else None
    # single_feed sentinel (feed_id=0 → it_feed=None): план не смог подтвердить профильный фид
    # на этапе плана (транзиентный сбой strict-lookup или API). Резолвим здесь, как tp5/tp3
    # делают через _resolve_single_feed_variants (create_set_feed_builders.py:907).
    # Если профильный фид найден → используем. Если нет → пропускаем tp7 (как tp5 делает
    # при feed_confirmed=False). Нормальный tp7 (it_feed!=None) этот блок не затрагивает.
    if is_product and it_feed is None:
        _job_body = (_job or {}).get("body", {}) if _job else {}
        if bool(_job_body.get("single_feed")):
            from .create_set_input import PROFILE_FEED_KEYS as _PFK
            _sf_pids: list[int] = []
            for _spk in _PFK:
                _spid = _first_url_feed(_st_token, login, _w_agency or "", strict=True, url_key=_spk)
                if _spid and _spid not in _sf_pids:
                    _sf_pids.append(_spid)
            if _sf_pids:
                it_feed = _sf_pids[0]   # найден профильный фид → используем первый
            else:
                # профильного фида нет на билде тоже; feed_confirmed=False (иначе план поставил бы
                # реальный fid вместо sentinel); пропускаем tp7 — аналог data["feeds"]=[] в tp5.
                return [{"ok": False, "name": name,
                         "error": "tp7 single_feed: профильный фид не найден на билде — пропущена"}], _tp7_mf
    it_listings = _catalog_feed(_st_token, login, it_feed or 0, _w_agency or "") if is_product else None
    # tp7 фильтр по collectionId: показываем только модель кодера (не весь фид).
    # Загружаем фиды с модельными коллекциями лениво — один раз на весь запрос.
    it_lff = []                                  # listings_feed_filters
    it_ff = []                                   # feed_filters (товарная часть)
    if is_product and it_feed:
        # Tp7 фильтруется только позитивно по марке/модели из ct. Общие ct0000-кампании
        # и позиции без реальной марки/модели не получают глобальные минус-фильтры.
        it_ff = _tp7_product_feed_filters(c_brand or "", c_ct or "ct0000", login=login, feed_id=it_feed or 0)
    if is_product and it_feed and c_brand and c_ct and c_ct != "ct0000":
        it_lff = _tp7_listing_plus_filter(login, it_feed, c_brand, c_ct, _w_agency or "")
        # collectionId is valid for listings_feed_filters only. Sending it in product
        # feed_filters makes UAC map the condition to a text operator and reject create
        # with INVALID_OPERATOR. Product feed_filters stay positive by mark/model field.
    elif is_product and it_feed:
        # Небрендовые группы и брендовые группы без точного collectionId идут без listings-фильтра.
        # Глобальные minus/allow-list фильтры здесь намеренно не ставим.
        it_lff = []
    it_titles = list(dict.fromkeys(
        _strip_dangling_uac_tail(_trim_to_word(t, 56)) for t in (it_titles or []) if t and str(t).strip()
    ))[:5]
    it_texts = _diverse_text_offers(
        [x for x in dict.fromkeys(
            _strip_dangling_uac_tail(_trim_to_word(t, 81)) for t in (it_texts or []) if t and str(t).strip()
        ) if x and len(x) >= _TP67_MIN_TEXT_LEN],
        3,
    )
    if not _is_non_auto:
        it_titles = _limit_online_density(it_titles, 1)
        it_texts = _limit_online_density(it_texts, 1)

    if len(it_texts) < 3:
        _final_seen_texts = {_variant_norm_key(_x) for _x in it_texts if _variant_norm_key(_x)}
        _num_hint = _uac_number_clause(
            list(it_titles or []) + list(_llm_titles or []) + list(_llm_texts or []))

        def _append_final_uac_text(_raw: str) -> None:
            if len(it_texts) >= 3:
                return
            _base = re.split(r"[.!?]", _soften_uac_sitelink_text(str(_raw or "")), maxsplit=1)[0].strip()
            if not _base:
                return
            if not _is_non_auto and not _has_number(_base):
                _base = _attach_cta_hint(_base, _num_hint, 81)
            _x = _sanitize_content(_base, max_len=81)
            _x = _trim_to_word(_strip_credit_rate(_x), 81).rstrip(" .")
            if (not _x or _is_bad_start(_x) or _bad_ad_text(_x) or not _common_content_ok(_x)
                    or (not _is_non_auto and not _has_number(_x))
                    or len(_x) < _TP67_MIN_TEXT_LEN):
                return
            _nk = _variant_norm_key(_x)
            if _nk and (_nk in _final_seen_texts or any(_variant_norm_key(_old) == _nk for _old in it_texts)):
                return
            if _nk:
                _final_seen_texts.add(_nk)
            it_texts.append(_x)

        _final_text_sources: list[str] = []
        _final_text_sources.extend(str(_x or "") for _x in list(_llm_texts or []) + list(_sc.get("texts") or []))
        for _s in list(_sl_base or []) + list(it_sitelinks or []) + list(_pavlov_sitelinks or []):
            if isinstance(_s, dict):
                _final_text_sources.append(str(_s.get("description") or _s.get("title") or ""))
        _final_text_sources.extend(str(_x or "") for _x in list(it_titles or []) + list(_llm_titles or []))
        if not _live_generated_content and not _is_non_auto:
            _final_text_sources.extend(str(_x or "") for _x in list(_GENERIC_TEXT_FILLERS or []))
        for _src in _final_text_sources:
            if len(it_texts) >= 3:
                break
            _append_final_uac_text(_src)

    if len(it_texts) < 3:
        _err = (f"tp6/tp7 контент неполный после финальной UAC-добивки: тексты {len(it_texts)}/3")
        _add_job_err(_job, f"{name}: {_err}")
        results.append({"name": name, "ok": False, "error": _err, "blocked": True})
        _bump_job(_job, False)
        _bump_item(_job)
        if _job:
            _job_db_progress(_job)
        return results, _tp7_mf

    try:
        spec = cmc.MasterCampaignSpec(
            href=it_href, titles=it_titles, texts=it_texts, region_ids=region_ids,
            counter_id=counter_id, goal_id=goal_id, cpa=_num(it.get("cpa"), cpa),
            week_budget=it_budget,
            display_name=disp_name, sitelinks=it_sitelinks,
            image_files=it_images, video_files=it_videos,
            content_ids=[str(x).strip() for x in (it.get("preloaded_content_ids") or []) if str(x).strip()],
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
            # DEFECT 3 (2026-07-10, эталон Семёна по кабинету 712694741): АВТОТАРГЕТ-позиция должна
            # рендериться как «Подобрать оптимальную» (Аудитория), а не «Настроить вручную». При
            # keywords=[]/audiences=[] ЕДИНСТВЕННЫЙ ручной сигнал в payload — minus_keywords (глоб.
            # «отзывы») → UAC флипает блок «Аудитория» в «Настроить вручную».
            # Требование Семёна: для автотаргета минус-слова НЕ нужны → шлём []. Ручные режимы
            # (keywords/audience) — минус-слова как есть.
            # ⚠️ 2026-07-30: правка была применена ТОЛЬКО к товарке (`is_product`), с посылкой
            # «tp6-мастер не тронут (рендерит верно)». Посылка ОПРОВЕРГНУТА живым кабинетом:
            # `porg-uy3huxcn` / `tp6_…ct001_ag001_g00 — МК - Общие запросы - Автотаргетинг` показывал
            # «Настроить вручную», а UAC-detail этой РК давал `keywords=null`, аудиторий нет и
            # `minus_keywords=["отзывы"]` — тот же единственный ручной сигнал. Условие расширено на
            # ОБА типа (мастер tp6 и товарка tp7): решает режим позиции, а не тип кампании.
            minus_keywords=([] if targeting_mode == "autotarget"
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
            # Возраст 25+ (age_25) — для ВСЕХ ручных режимов tp6/Мастер (keywords И audience):
            # исключать только брекет 18-24 → socdem-range стартует с 25-34 (age_lower=age_25).
            # Раньше стоял age_35 (исключал ОБА брекета 18-24 И 25-34) — решение Семёна 2026-07-21
            # изменено на 25+. Значение age_25 — та же enum-семья Яндекс-socdem,
            # что age_18/age_35 (границы брекетов 18/25/35/45/55).
            # Исключения: автотаргетинг (#7 выше — полный socdem age_18 по дизайну) и товарка tp7
            # (age_18 всегда — возраст не настраивается). ag_part6 в имени = ag011 («ручной возраст»,
            # НЕ привязан к конкретной границе) — синхронизирован тем же условием, create_set_plan.py.
            age_lower=("age_18" if (targeting_mode == "autotarget" or is_product) else "age_25"),
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
                # images_pool — сколько картинок СВОЕГО ct физически было доступно (без добора из
                # чужого ct/ct0000). Читает live-верификатор: «в пуле меньше цели, взяли всё» —
                # warn, а «пул полный, а в кампании меньше» — error (uac_verifier, 2026-07-27).
                "images_pool": _pool_size,
                "sitelinks": len(it_sitelinks),
                "url": f"https://direct.yandex.ru/wizard/campaigns/{cid}/?ulogin={login}",
                # Эталон СТРУКТУРЫ слепка для этой позиции → live-верификатор сверяет кабинет
                # со СТРУКТУРОЙ, а не с build'ом (build уже пустой → сверка build↔live давала 0==0
                # и молчала об обрыве на этапе плана). См. uac_verifier.UAC_STRUCT_*_MISSING.
                "struct": {"keywords": len(_struct_exp["keywords"]),
                           "audiences": len(_struct_exp["audiences"]),
                           "audiences_unsupported": _struct_exp["audiences_unsupported"],
                           "modes": _struct_exp["modes"]},
                "sent": {"keywords": len(it_keywords), "audiences": len(it_audiences)}}
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

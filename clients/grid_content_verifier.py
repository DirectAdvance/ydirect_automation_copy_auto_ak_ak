"""Pure Grid content checks for non-UAC Direct campaigns."""
from __future__ import annotations

import re
from typing import Any


_TP_RE = re.compile(r"^\s*tp(\d+)_", re.IGNORECASE)
_PAY_RE = re.compile(r"^\s*tp\d+_(cpc|cpa)_", re.IGNORECASE)
_KS_NAME_RE = re.compile(r"(^|[^а-яё])кс([^а-яё]|$)")


def _autotarget_expected_by_name(name: str) -> bool:
    """tp2/tp4: True — автотаргет ДОЛЖЕН быть включён (профиль EXACT_V2_MARK/WITHOUT_BRAND
    обязателен, WRONG_AUTOTARGET применим). False — имя кампании явно говорит «КС»/«ключев» и
    НЕ содержит «автотаргет» → автотаргет выключен НАМЕРЕННО (КС-набор без автотаргетинга) и
    флагать его как дефект нельзя. Та же логика распознавания по имени, что
    `create_set_plan._txt_targeting_mode` (без доступа к её `tgt`-фолбэку, здесь только live-имя).
    ⚠️ НЕ звать для tp5 — там профиль `search_tp2` ставится АТОМАРНО и ВСЕГДА независимо от
    планового autotarget-флага (DOD.md §1.2), имя роли не играет."""
    low = str(name or "").lower()
    has_at = "автотаргет" in low
    has_kw = bool(_KS_NAME_RE.search(low)) or "ключев" in low
    return not (has_kw and not has_at)


def _repair(name: str, cid: int | None) -> dict[str, Any]:
    return {"kind": "rebuild_missing_content", "name": name, "id": cid}


def _invariant_repair(name: str, cid: int | None) -> dict[str, Any]:
    return {"kind": "campaign_invariant_repair", "name": name, "id": cid}


def _keywords_repair(name: str, cid: int | None) -> dict[str, Any]:
    return {"kind": "keywords_repair", "name": name, "id": cid}


def _images_repair(name: str, cid: int | None) -> dict[str, Any]:
    return {"kind": "images_repair", "name": name, "id": cid}


def _adprice_repair(name: str, cid: int | None) -> dict[str, Any]:
    return {"kind": "adprice_repair", "name": name, "id": cid}


def _default_text_repair(name: str, cid: int | None) -> dict[str, Any]:
    return {"kind": "default_text_repair", "name": name, "id": cid}


def _tp(name: str) -> int | None:
    m = _TP_RE.match(str(name or ""))
    return int(m.group(1)) if m else None


def verify_grid_content(name: str, campaign_id: int | None,
                        counts: dict[str, Any],
                        expected: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(issues, repair_candidates)`` for Grid adgroup/ad counters.

    ``counts`` is the per-campaign dict from ``grid_read.campaign_content_counts``.
    Enrichment fields (keywords_count/disabled_places/is_organic_search_enabled/
    promo_extension_id/has_ad_price) are only checked when Grid actually returned
    them (``*_read`` flag true or value not None), so an unread field never yields
    a false defect. ``expected`` may carry per-item business hints
    (``minus_places`` int, ``expects_price`` bool, ``expects_promo`` bool); when
    absent, project-wide tp invariants are used as the expectation.

    ``expected["account_has_promo"]`` is the tri-state promo gate: ``True`` — the account
    library has promos (so a campaign without one is a defect), ``False``/``None`` — never
    flag ``PROMO_MISSING`` (an account with no promos at all must not produce a storm of
    false issues). It is derived by the caller from data already read, without extra API calls.
    """
    nm = str(name or "")
    cid = campaign_id
    exp = expected or {}
    counts = counts or {}
    issues: list[dict[str, Any]] = []
    repair: list[dict[str, Any]] = []
    adgroups = int(counts.get("adgroups") or 0)
    ads = int(counts.get("ads") or 0)
    tp = _tp(nm)

    # ── Existing structural checks (unchanged) ───────────────────────────────
    if adgroups <= 0:
        issues.append({"severity": "error", "code": "NO_ADGROUPS_LIVE",
                       "name": nm, "id": cid, "actual": adgroups})
        repair.append(_repair(nm, cid))
    if ads <= 0:
        issues.append({"severity": "error", "code": "NO_ADS_LIVE",
                       "name": nm, "id": cid, "actual": ads})
        repair.append(_repair(nm, cid))
    bad_names = int(counts.get("bad_adgroup_names") or 0)
    if bad_names > 0:
        issues.append({"severity": "error", "code": "ADGROUP_NAME_MISSING",
                       "name": nm, "id": cid, "actual": bad_names,
                       "examples": counts.get("bad_adgroup_name_examples") or []})
        repair.append(_repair(nm, cid))

    # ── NO_KEYWORDS_LIVE + WRONG_AUTOTARGET (auto-repair via keywords_repair) ─────────────────
    # Предпочитаем per-group чтение GroupsForEdit (groups_edit_read): оно ловит дефект даже если
    # хотя бы ОДНА поисковая группа без ключей / с неверным автотаргетом (агрегат keywords_count==0
    # ловит только когда ВСЕ группы пусты). tp2/tp4/tp5 — поисковые; tp1 РСЯ сюда не попадает, т.к.
    # groups_edit_read проставляется только для search-кампаний в grid_read._enrich_group_targeting.
    # tp1/tp3 добавлены (2026-07-18): _enrich_group_targeting теперь читает их группы из ТОГО ЖЕ
    # батч-ответа (0 новых запросов). Для tp1/tp3 группа с активным relevanceMatch и 0 ключей
    # пуста ПО ДИЗАЙНУ (режим «полный автотаргет») и в zero-kw не попадает — см. grid_read.
    if tp in (1, 2, 3, 4, 5) and counts.get("groups_edit_read"):
        zero_kw = counts.get("search_zero_kw_groups")
        wrong_at = counts.get("wrong_autotarget_groups")
        # Обрезка ответа Grid по лимиту (>10000 ключей/строк) → счётчик НЕДОсчитан → по ключам
        # НЕ судим вовсе (иначе крупный набор гарантированно даёт ложный NO_KEYWORDS_LIVE).
        _kw_trunc = bool(counts.get("keywords_truncated"))
        if isinstance(zero_kw, int) and zero_kw > 0 and not _kw_trunc:
            issues.append({"severity": "error", "code": "NO_KEYWORDS_LIVE",
                           "name": nm, "id": cid, "actual": 0, "groups": zero_kw,
                           "note": "группы без ключей (auto-repair: keywords_repair)"})
            repair.append(_keywords_repair(nm, cid))
        # WRONG_AUTOTARGET — только поисковые: профиль EXACT_V2_MARK/WITHOUT_BRAND обязателен
        # на tp2/4/5; у tp1 РСЯ автотаргет намеренно широкий (create_set_tp1_builders.py:302).
        # Severity: всегда error — ремонт in-place (keywords_repair → Grid UpdateUnifiedAdGroups,
        # repair_planner.py:162); job-status gate (bacd444e) учитывает только error-коды.
        # Прежний лаг-исключение «warn в in-job» убран: Grid-лаг устранён Phase 1.5 (2026-07-27).
        # tp2/tp4: КС-набор БЕЗ «автотаргет» в имени выключает автотаргет НАМЕРЕННО у ВСЕХ своих
        # групп — гасим ложный WRONG_AUTOTARGET по имени (_autotarget_expected_by_name), иначе
        # keywords_repair слал ложный ремонт и стирал живые ключи (инцидент 2026-07-30,
        # porg-rgwzgo57 713155623, 119 групп/15258 ключей). tp5 НЕ проверяем по имени — там
        # search_tp2 профиль ставится атомарно и ВСЕГДА, isActive=True даже у планового aoff
        # (DOD.md §1.2), имя не определяет ожидание.
        if (tp in (2, 4, 5) and isinstance(wrong_at, int) and wrong_at > 0
                and (tp == 5 or _autotarget_expected_by_name(nm))):
            _at_sev = "error"
            issues.append({"severity": _at_sev, "code": "WRONG_AUTOTARGET",
                           "name": nm, "id": cid, "groups": wrong_at,
                           "note": "неверный профиль автотаргета (нужен EXACT_V2_MARK/WITHOUT_BRAND)"})
            repair.append(_keywords_repair(nm, cid))
        # WRONG_AUTOTARGET tp1 РСЯ: проверяем isActive vs _aon_/_aoff_ в имени группы.
        # Дефект = aon→isActive=False (ВКЛ-ожидали, ВЫКЛ-реально) или aoff→isActive=True.
        # Ключ wrong_autotarget_rsya_groups проставляется grid_read только когда группы прочитаны,
        # поэтому isinstance(..., int) является fail-safe гейтом от ложного детекта.
        _wrong_rsya = counts.get("wrong_autotarget_rsya_groups")
        if tp == 1 and isinstance(_wrong_rsya, int) and _wrong_rsya > 0:
            _at_rsya_sev = "error"
            issues.append({"severity": _at_rsya_sev, "code": "WRONG_AUTOTARGET",
                           "name": nm, "id": cid, "groups": _wrong_rsya,
                           "note": "неверный isActive автотаргета tp1 РСЯ: aon→ВЫКЛ или aoff→ВКЛ"})
    else:
        # Фолбэк на агрегат keywords_count (report-only), когда per-group чтение недоступно.
        kw = counts.get("keywords_count")
        if tp in (2, 4) and ads > 0 and counts.get("keywords_read") and kw is not None and int(kw) == 0:
            issues.append({"severity": "error", "code": "NO_KEYWORDS_LIVE",
                           "name": nm, "id": cid, "actual": 0,
                           "note": "поисковая группа без ключей (report-only, per-group read недоступен)"})

    # ── DYNAMIC_PLACES_ON (report-only): tp2 must have organic search OFF ─────
    org = counts.get("is_organic_search_enabled")
    if tp == 2 and org is True:
        issues.append({"severity": "warn", "code": "DYNAMIC_PLACES_ON",
                       "name": nm, "id": cid, "actual": True,
                       "note": "динамические места на поиске включены у tp2 (report-only)"})

    # ── NAME_SEGMENT_DUPE (report-only): сегмент имени кампании повторён ──────
    # ОБОБЩЁННЫЙ детект класса «дубль подстроки в имени» (ТК - Автосалон - ТК - Автосалон,
    # МК - МК, домен фида дважды). Литералы НЕ зашиваем: сравниваем имя с результатом того же
    # дедупа, которым имя собирается (create_set_context.dedup_name_segments) — разошлись,
    # значит какой-то сегмент приклеился дважды. 0 запросов, чистая строка.
    try:
        from ..create.create_set_context import dedup_name_segments as _dedup_nm
        _nm_clean = _dedup_nm(str(nm or ""))
    except Exception:  # noqa: BLE001
        _nm_clean = str(nm or "")
    if nm and _nm_clean != str(nm):
        issues.append({"severity": "warn", "code": "NAME_SEGMENT_DUPE",
                       "name": nm, "id": cid, "expected": _nm_clean,
                       "note": "имя кампании отличается от канонического: повтор сегмента "
                               "или лишние пробелы (report-only)"})

    # ── MINUS_PLACES_MISSING (report-only): tp1 РСЯ without disabled places ───
    dp = counts.get("disabled_places")
    exp_minus = exp.get("minus_places")
    if tp == 1 and isinstance(dp, list) and len(dp) == 0 and (exp_minus is None or int(exp_minus) > 0):
        issues.append({"severity": "warn", "code": "MINUS_PLACES_MISSING",
                       "name": nm, "id": cid,
                       "expected_minus_places": exp_minus,
                       "note": "минус-площадки РСЯ пусты у tp1 (report-only)"})

    # PRICE_MISSING (report-only) заменён на NO_ADPRICE_LIVE (с repair-candidate) ниже.

    # ── Кампанийные АССЕТЫ tp1–tp5: уточнения / набор быстрых ссылок / промо ──────
    # Источник — тот же ответ CampaignsEditData, что и инвариант-галочки (grid_finalize.
    # read_campaign_invariants → grid_read._enrich_campaign_invariants): НОЛЬ новых запросов
    # к Grid/Direct API. Все три — tri-state: None = поле не прочитано → МОЛЧИМ (fail-safe,
    # ложный детект здесь дороже пропуска — журнал I).
    # Все три report-only (severity warn, БЕЗ repair-кандидата): существующий
    # campaign_invariant_repair (set_campaign_invariants) эти поля НЕ переставляет, а
    # выдумывать ремонт вслепую нельзя. Задача кодов — сделать дефект ВИДИМЫМ в отчёте
    # (инциденты DMP_CALLOUTS_NOT_PUSHED / CALLOUTS_NAMEERROR_TIME / SITELINK_SET_NULL_SILENT
    # ловились руками именно потому, что верификатор молчал).
    _assets_read = bool(counts.get("campaign_assets_read"))
    _assets_tp = tp in (1, 2, 3, 4, 5)

    # CALLOUTS_MISSING_LIVE — у кампании не привязаны уточнения.
    # ⚠️ Гейт по типу: tp6/tp7 (МК/Товарка, UAC) уточнения НЕ поддерживают
    # (ERRORS_JOURNAL: DMP_CALLOUTS_NOT_PUSHED — «UAC/tp7 пропущены — не поддерживают уточнения»)
    # → для них молчим, иначе получим гарантированный ложный флаг на каждой UAC-кампании.
    _callouts = counts.get("callout_ids")
    if _assets_tp and _assets_read and isinstance(_callouts, list) and len(_callouts) == 0:
        issues.append({"severity": "warn", "code": "CALLOUTS_MISSING_LIVE",
                       "name": nm, "id": cid, "actual": 0,
                       "note": "у кампании не привязаны уточнения (inheritableCallouts пуст; report-only)"})

    # SITELINK_SET_MISSING_LIVE — у кампании не привязан НАБОР быстрых ссылок.
    # Дополняет ad-level SITELINK_MISSING/UAC_SITELINKS_MISSING: набор живёт на КАМПАНИИ,
    # и его null (SITELINK_SET_NULL_SILENT) прежде не детектировался вообще.
    _slset = counts.get("sitelink_set_id")
    if _assets_tp and _assets_read and isinstance(_slset, str) and not _slset.strip():
        issues.append({"severity": "warn", "code": "SITELINK_SET_MISSING_LIVE",
                       "name": nm, "id": cid,
                       "note": "у кампании не привязан набор быстрых ссылок "
                               "(inheritableSitelinkSet.assetValue пуст; report-only)"})

    # PROMO_MISSING — ДВУХСТУПЕНЧАТО (требование Семёна).
    # Ступень 1: есть ли промо в БИБЛИОТЕКЕ АККАУНТА (exp["account_has_promo"], tri-state).
    #   None (неизвестно) или False (промо в аккаунте нет) → НЕ флагаем вообще: иначе на каждом
    #   аккаунте без промо получим шквал ложных ошибок.
    # Ступень 2: только при account_has_promo=True проверяем, что промо доехало в кампанию.
    # Обратная совместимость: явный exp["expects_promo"] по-прежнему уважается.
    promo_present = bool(str(counts.get("promo_extension_id") or "").strip())
    _acct_promo = exp.get("account_has_promo")
    exp_promo = exp.get("expects_promo")
    if exp_promo is None and _acct_promo is True:
        exp_promo = True
    if (_assets_tp and _assets_read and not promo_present and exp_promo is True):
        issues.append({"severity": "warn", "code": "PROMO_MISSING",
                       "name": nm, "id": cid,
                       "note": "в аккаунте есть промо-акции, но у кампании промо "
                               "не привязано (report-only)"})

    # ── NO_IMAGES_LIVE (error): tp1 адаптивные объявления без imageHashes ────────
    # Детектируется через _enrich_adaptive_images (Grid images{imageHash}).
    # Repair: images_repair (RMW + UpdateAdaptiveTextAds — in-place).
    no_img = counts.get("no_images_ads")
    if (tp == 1 and ads > 0 and counts.get("adaptive_images_read")
            and isinstance(no_img, int) and no_img > 0):
        issues.append({"severity": "error", "code": "NO_IMAGES_LIVE",
                       "name": nm, "id": cid,
                       "actual": no_img, "total_adaptive": counts.get("adaptive_total"),
                       "note": f"tp1: {no_img} объявлений без imageHashes (in-place: images_repair)"})
        repair.append(_images_repair(nm, cid))

    # ── AD_HREF_ROOT_INSTEAD_OF_MODEL (error): модельные кампании с href на корень ───────
    # Для кампаний с сегментом «Модели» или «Марки» href объявлений должен вести на страницу
    # модели/марки, а НЕ на корень домена. Детект: root_href_ads_count (число объявлений
    # у которых urlparse(href).path in ("", "/")) из того же запроса AdaptiveImages, что
    # и NO_IMAGES_LIVE (_enrich_adaptive_images) — НОЛЬ новых запросов.
    # Tri-state: молчим, если root_href_ads_read=False (данные не прочитаны → fail-safe).
    # Исключение: root_href_ok=True в expected — посадочная является корнем намеренно
    # (квизовый лендинг site.ru/ без пути, кампания-заглушка без модельных страниц).
    if (not exp.get("root_href_ok")
            and re.search(r'\bМодели\b|\bМарки\b', nm or "")
            and counts.get("root_href_ads_read")
            and isinstance(counts.get("root_href_ads_count"), int)
            and counts.get("root_href_ads_count", 0) > 0):
        _root_cnt = counts["root_href_ads_count"]
        issues.append({
            "severity": "error",
            "code": "AD_HREF_ROOT_INSTEAD_OF_MODEL",
            "name": nm, "id": cid, "ads": _root_cnt,
            "note": (f"{_root_cnt} объявлений с href на корень домена "
                     f"(ожидается страница модели/марки); repair: rebuild_missing_content"),
        })
        repair.append(_repair(nm, cid))

    # ── NO_ADPRICE_LIVE (warn): tp1 комбинаторные объявления без bannerPrice ─────
    # Более actionable версия PRICE_MISSING: несёт repair-candidate adprice_repair.
    # Детектируется только когда ad_price_read=True (bannerPrice{price} работает).
    # PRICE_MISSING (report-only) ниже не дублируем — оба бы сработали на том же условии.
    hp = counts.get("has_ad_price")
    exp_price = exp.get("expects_price")
    # Товарка-only (только ShoppingAd/ListingAd, напр. смарт-баннер): bannerPrice — поле ТОЛЬКО
    # адаптивных текстовых объявлений, у товарки цена приходит из фида → NO_ADPRICE_LIVE неприменим.
    # Признак берём из реально читаемого Grid'ом adaptive_total (_enrich_adaptive_images): ключей
    # shopping_ads/listing_ads в live-counts НЕ существует (они есть только в build-отчёте, который
    # в верификатор не передаётся) → прежнее условие всегда было False и давало ложный флаг.
    # Fail-safe: если адаптивные не прочитаны — ведём себя как раньше (флаг не глушим).
    _shop_only_live = (bool(counts.get("adaptive_images_read"))
                       and int(counts.get("adaptive_total") or 0) == 0)
    if (tp == 1 and ads > 0 and not _shop_only_live
            and counts.get("ad_price_read")
            and hp is False and (exp_price is None or bool(exp_price))):
        issues.append({"severity": "warn", "code": "NO_ADPRICE_LIVE",
                       "name": nm, "id": cid,
                       "note": "нет adPrice (bannerPrice) ни на одном объявлении tp1 (in-place: adprice_repair)"})
        repair.append(_adprice_repair(nm, cid))

    # ── EMPTY_DEFAULT_TEXT_LIVE (warn): ShoppingAd без bodies ──────────────────
    # Детектируется через _enrich_shopping_bodies (GdSmartAd fragment, best-effort).
    # Repair: default_text_repair (set_default_text — in-place).
    shop_no_bodies = counts.get("shopping_no_bodies_ads")
    if (tp in (1, 3, 5) and counts.get("shopping_bodies_read")
            and isinstance(shop_no_bodies, int) and shop_no_bodies > 0):
        issues.append({"severity": "warn", "code": "EMPTY_DEFAULT_TEXT_LIVE",
                       "name": nm, "id": cid,
                       "actual": shop_no_bodies,
                       "note": f"{shop_no_bodies} ShoppingAd без bodies (in-place: default_text_repair)"})
        repair.append(_default_text_repair(nm, cid))

    # ── Кампанийные инвариант-галочки tp1–tp5 (P0, закрытие дыры DOD §1.c) ───────
    # Читаются на уровне КАМПАНИИ (edit-view CampaignsEditData → grid_read._enrich_campaign_invariants).
    # Fail-safe: флагаем ТОЛЬКО если поле реально прочитано (campaign_invariants_read=True) И имеет
    # явный неверный булев (None=не прочитано → тишина, чтобы Grid-лаг/FieldUndefined не породил ложный
    # детект и ложный ремонт, журнал I). Все нарушения чинит ОДИН идемпотентный campaign_invariant_repair
    # (re-apply инвариант-блока финализации через UpdateCampaigns, БЕЗ баллов, РК всегда DRAFT — блик-
    # радиус ложняка = безвредный повторный UpdateCampaigns, НЕ удаление). #4 (мониторинг сайта) в
    # read-схеме Grid отсутствует → отдельно не детектируется, лишь переставляется ремонтом.
    if tp in (1, 2, 3, 4, 5) and counts.get("campaign_invariants_read"):
        _needs_invariant_fix = False
        # (флаг, ожидание, issue-code, severity) — каждый булев tri-state; None пропускается.
        _toggle_checks = (
            ("is_alternative_texts_enabled", True, "ALT_TEXTS_ENABLED_LIVE",
             "персонализация (адаптивные тексты) включена — должна быть ВЫКЛ (#3)"),
            ("has_extended_geo_targeting", True, "EXTENDED_GEO_ENABLED_LIVE",
             "расширенный географический таргетинг включён — должен быть ВЫКЛ (#5)"),
            ("is_recommendations_management_enabled", True, "RECOMMENDATIONS_ENABLED_LIVE",
             "«Директ помогает» включён — должен быть ВЫКЛ (#6)"),
            ("is_price_recommendations_management_enabled", True, "PRICE_RECOMMENDATIONS_ENABLED_LIVE",
             "ценовые рекомендации включены — должны быть ВЫКЛ"),
            ("enable_company_info", True, "COMPANY_INFO_ENABLED_LIVE",
             "Карты / список организаций (enableCompanyInfo) включены — должны быть ВЫКЛ"),
            ("yandex_maps_enabled", True, "MAPS_ENABLED_LIVE",
             "площадка «Карты» (yandexMaps) включена — должна быть ВЫКЛ"),
            ("serp_geo_wizard_enabled", True, "ORG_LIST_ENABLED_LIVE",
             "список организаций / гео-колдунщик (serpGeoWizard) включён — должен быть ВЫКЛ"),
        )
        for field, bad_value, code, note in _toggle_checks:
            val = counts.get(field)
            if isinstance(val, bool) and val is bad_value:
                issues.append({"severity": "error", "code": code, "name": nm, "id": cid,
                               "actual": val, "note": note})
                _needs_invariant_fix = True
        if _needs_invariant_fix:
            repair.append(_invariant_repair(nm, cid))
        # STRATEGY_MISMATCH_LIVE (warn, report-only): пара cpc→AVERAGE_CPA (payForConversion=False) /
        # cpa→PAY_FOR_CONVERSION (payForConversion=True). Ожидание — из pay-mode КОДЕРА в имени
        # (tp{N}_cpc/cpa_…, авторитетно, не догадка). Report-only: авто-правка стратегии in-place
        # рискованна (нужны avgCpa/недельный лимит; неверный pay-mode = баг создания → recreate, не
        # in-place) → без repair-кандидата, не раздувает inplace_cnt и не зацикливает «до нуля».
        pfc = counts.get("pay_for_conversion")
        _pm = _PAY_RE.match(nm)
        if isinstance(pfc, bool) and _pm:
            pay_mode = _pm.group(1).lower()
            expected_pfc = (pay_mode == "cpa")
            if pfc is not expected_pfc:
                issues.append({"severity": "warn", "code": "STRATEGY_MISMATCH_LIVE",
                               "name": nm, "id": cid, "actual": pfc, "expected": expected_pfc,
                               "note": (f"стратегия {pay_mode}: payForConversion={pfc}, "
                                        f"ожидалось {expected_pfc} "
                                        f"(cpc→AVERAGE_CPA / cpa→PAY_FOR_CONVERSION); report-only")})

    # ── Гео кампании (tp1–tp5): regionsInfo.regionIds из ТОГО ЖЕ GroupsForEditLite ──────
    # Поле читалось и выбрасывалось. tri-state: geo_read=False → молчим.
    if tp in (1, 2, 3, 4, 5) and counts.get("geo_read"):
        _geo_missing = counts.get("geo_missing_groups")
        if isinstance(_geo_missing, int) and _geo_missing > 0:
            issues.append({"severity": "error", "code": "GEO_MISSING_LIVE",
                           "name": nm, "id": cid, "groups": _geo_missing,
                           "note": f"{_geo_missing} групп без регионов показа (regionIds пуст)"})
        if counts.get("geo_regions_inconsistent") is True:
            issues.append({"severity": "warn", "code": "GEO_INCONSISTENT_LIVE",
                           "name": nm, "id": cid,
                           "regions": (counts.get("campaign_region_ids") or [])[:20],
                           "note": "у групп одной кампании РАЗНЫЕ наборы регионов (report-only)"})

    # ── UTM на группах (DoD #2: у tp1–tp5 метка живёт на ГРУППЕ, TrackingParams) ────────
    if tp in (1, 2, 3, 4, 5) and counts.get("group_utm_read"):
        _utm_missing = counts.get("utm_missing_groups")
        if isinstance(_utm_missing, int) and _utm_missing > 0:
            issues.append({"severity": "error", "code": "UTM_MISSING_LIVE",
                           "name": nm, "id": cid, "groups": _utm_missing,
                           "note": f"{_utm_missing} групп без UTM-метки (trackingParams пуст)"})

    # ── ОХВАТ пер-групповых проверок (анти-«проверка молча ничего не проверяет») ────────
    # trackingParams / relevanceMatch / regionsInfo приходят из фрагмента `...on GdUnifiedAdGroup`,
    # поэтому группа ДРУГОГО типа выпадает из ВСЕХ пер-групповых проверок сразу (zero-kw,
    # автотаргет, гео, UTM) — grid_read._enrich_group_targeting её пропускает. Пока сервис создаёт
    # ЕПК-группы (Grid AddUnifiedAdGroups / v501 adgroups.add на UNIFIED_CAMPAIGN) счётчик = 0 и код
    # не появляется НИКОГДА. Появился — значит часть кампании не проверяется вовсе, и это надо
    # увидеть, а не молча пропустить. Report-only (warn, без ремонта): недобор охвата — не дефект
    # кампании, а дефект проверки.
    _unsup = counts.get("groups_unsupported")
    if tp in (1, 2, 3, 4, 5) and isinstance(_unsup, int) and _unsup > 0:
        issues.append({"severity": "warn", "code": "GROUPS_TYPE_UNSUPPORTED_LIVE",
                       "name": nm, "id": cid, "groups": _unsup,
                       "note": (f"{_unsup} групп не типа GdUnifiedAdGroup — пер-групповые проверки "
                                f"(ключи/автотаргет/гео/UTM) их НЕ покрывают (report-only)")})

    issues.extend(_verify_campaign_spec(nm, cid, tp, counts))
    _b_issues, _b_repair = _verify_build_vs_live(nm, cid, counts, exp)
    issues.extend(_b_issues)
    repair.extend(_b_repair)
    return issues, repair


# ── Кампанийная СПЕЦИФИКАЦИЯ (счётчик/цель/бюджет/статус/расписание) ───────────────────
# Все поля приходят из того же ответа CampaignsEditData, что и инвариант-галочки
# (grid_finalize.read_campaign_invariants) → НОЛЬ новых запросов и баллов.
# Контракт tri-state: поле не прочитано (None / campaign_spec_read=False) → МОЛЧИМ.
# Все коды — ДЕТЕКТ без repair-кандидата: существующий campaign_invariant_repair
# (set_campaign_invariants) этих полей НЕ переставляет, а выдумывать ремонт вслепую нельзя
# (ложный ремонт дороже пропуска, журнал I). Ремонт — отдельным этапом.
_DRAFT_STATUSES = {"DRAFT", "MODERATION_DRAFT"}
# ⚠️ Флагаем НЕ «всё, что не DRAFT», а только ЯВНО известные не-черновиковые статусы. Живой Grid
# на свежесозданной кампании 712882029 подтвердил primaryStatus='DRAFT' (Семён, 2026-07-19), но
# словарь Grid может пополниться — незнакомое значение → МОЛЧИМ (tri-state fail-safe), иначе одна
# смена словаря даёт шквал ложных error на каждой кампании набора. Перечень собран по фактическим
# значениям primaryStatus, встречающимся в коде сервиса (account_service._GRID_STATE:716,
# autorules/sensors/campaign_status.py:9) — это статусы ЗАПУЩЕННОЙ кампании, а сервис публикует
# только черновики. ARCHIVED намеренно НЕ включён: архивный черновик — не «опубликована».
_NON_DRAFT_STATUSES = {"ACTIVE", "ACCEPTED", "ENDED", "STOPPED", "SUSPENDED", "PAUSED",
                       "MODERATION", "WAIT_MODERATING", "REJECTED"}


def _verify_campaign_spec(nm: str, cid: int | None, tp: int | None,
                          counts: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if tp not in (1, 2, 3, 4, 5) or not counts.get("campaign_spec_read"):
        return out

    # #1 Метрика: счётчик обязателен ВСЕГДА (DoD §3.0; гейт api_create_set отдаёт 400 без него).
    ctrs = counts.get("metrika_counters")
    if isinstance(ctrs, list) and len(ctrs) == 0:
        out.append({"severity": "error", "code": "METRIKA_COUNTER_MISSING_LIVE",
                    "name": nm, "id": cid,
                    "note": "у кампании не привязан счётчик Метрики (metrikaCounters пуст)"})

    # #1 Цель: считается заданной, если есть ЛЮБОЙ источник — ключевая цель кампании
    # (meaningfulGoals) ИЛИ цель стратегии (strategy.goalId). Флагаем только когда ОБА
    # прочитаны и ОБА пусты (иначе стратегия без goalId, напр. ручная, дала бы ложняк).
    goals = counts.get("meaningful_goal_ids")
    strat_goal = counts.get("strategy_goal_id")
    if isinstance(goals, list) and isinstance(strat_goal, str) and not goals and not strat_goal.strip():
        out.append({"severity": "error", "code": "CAMPAIGN_GOAL_MISSING_LIVE",
                    "name": nm, "id": cid,
                    "note": "нет цели ни в meaningfulGoals, ни в стратегии (strategy.goalId)"})

    # ⚠️ hasAddMetrikaTagToUrl (авторазметка Яндекса, yclid) НЕ проверяем — решение Семёна
    # 2026-07-19 по разбору прогона 2b9d58d01e28 (18 из 20 ложных METRIKA_TAG_OFF_LIVE):
    # билдер намеренно ставит False (grid_create_payloads.py:77, как и браузерный эталон
    # grid_uc_template.json:286), а наши UTM живут на ГРУППАХ (trackingParams, UTM_MISSING_LIVE
    # выше) и с yclid не пересекаются. Канон (DoD §3.0, инварианты #1/#2) авторазметки не требует.
    # bannerHrefParams не проверяем по той же причине: пустой параметр кампании — норма.

    # Недельный бюджет: сумма должна быть > 0, период — WEEK.
    bsum = counts.get("budget_sum")
    if isinstance(bsum, (int, float)) and float(bsum) <= 0:
        out.append({"severity": "error", "code": "WEEKLY_BUDGET_MISSING_LIVE",
                    "name": nm, "id": cid, "actual": float(bsum),
                    "note": "недельный бюджет стратегии ≤ 0 (strategy.budget.sum)"})
    bper = counts.get("budget_period")
    if isinstance(bper, str) and bper.strip() and bper.strip().upper() != "WEEK":
        out.append({"severity": "warn", "code": "BUDGET_PERIOD_UNEXPECTED_LIVE",
                    "name": nm, "id": cid, "actual": bper,
                    "note": "период бюджета не WEEK (report-only)"})

    # Статус: сервис НИКОГДА не публикует — созданная кампания обязана быть черновиком
    # (DoD §3.0 «Только ЧЕРНОВИК», launch=False). Судим по status.primaryStatus;
    # aggregatedStatusInfo.status кладём в отчёт как второй источник.
    prim = counts.get("status_primary")
    _prim_u = prim.strip().upper() if isinstance(prim, str) else ""
    if _prim_u and _prim_u not in _DRAFT_STATUSES and _prim_u in _NON_DRAFT_STATUSES:
        out.append({"severity": "error", "code": "CAMPAIGN_NOT_DRAFT_LIVE",
                    "name": nm, "id": cid, "actual": prim,
                    "aggregated": counts.get("aggregated_status"),
                    "note": "кампания не в статусе черновика (сервис публикует только черновики)"})

    # Расписание показов: timeBoard пуст → круглосуточный дефолт не выставлен.
    tb = counts.get("time_board")
    if isinstance(tb, list) and len(tb) == 0:
        out.append({"severity": "error", "code": "TIME_TARGET_MISSING_LIVE",
                    "name": nm, "id": cid,
                    "note": "не задано расписание показов (timeTarget.timeBoard пуст)"})
    return out


# ── Сверка «build ⇄ кабинет» ───────────────────────────────────────────────────────────
# ``build`` — то, что БИЛДЕР отчитался созданным (result["build"]/tp1_build/tp5_build); он уже
# лежит в результате джобы, поэтому сверка бесплатна (0 новых запросов). Сравниваем ТОЛЬКО
# одноимённые измерения и ТОЛЬКО в сторону НЕДОБОРА (live < build):
#   * live == 0 при build > 0 → error + rebuild_missing_content (существующий ремонтёр);
#   * 0 < live < build        → warn в in-job фазе и error в отложенной (phase="delayed").
#     ⚠️ Недобор НЕ означает «демон дольёт»: замер 2026-07-19 (прогон 2b9d58d01e28) показал, что
#     спустя ~12 ч живые счётчики идентичны in-job. Реальные причины — схлопывание Директом
#     дублей фраз (билдер считает AddResults с Id) и лаг индексации Grid на in-job-моменте.
# Перебор (live > build) НЕ флагаем: Grid-счётчик ``ads`` считает ВСЕ типы объявлений, а
# build.ads — только текстовые, поэтому «больше» — нормальная форма, а не дефект.
# Ловушки (проверено): товарная кампания c build.ads=0/shopping_ads>0 сравнивается по СУММЕ
# ads+shopping_ads+listing_ads против общего live-счётчика ads (одноимённое измерение),
# поэтому «нет текстовых объявлений» ложным недобором не становится; UAC (tp6/tp7) сюда не
# доходит — вызывающий (live_verifier) зовёт verify_grid_content только для kind != "uac".
_BUILD_DIMS = (
    # (ярлык, ключи build (суммируются), ключ live-счётчика, read-гейт, repair)
    ("группы", ("groups", "adgroups"), "adgroups", None, "rebuild"),
    ("объявления", ("ads", "shopping_ads", "listing_ads"), "ads", None, "rebuild"),
    ("ключи", ("keywords",), "keywords_count", "keywords_read", "keywords"),
)


def _build_expected(build: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    """Сумма одноимённых build-счётчиков; None если НИ ОДИН ключ не отчитан (→ молчим)."""
    total = 0
    seen = False
    for k in keys:
        if k in build:
            try:
                total += int(build.get(k) or 0)
            except (TypeError, ValueError):
                continue
            seen = True
    return total if seen else None


def _verify_build_vs_live(nm: str, cid: int | None, counts: dict[str, Any],
                          exp: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    build = exp.get("build")
    if not isinstance(build, dict) or not build:
        return [], []
    phase = str(exp.get("phase") or "in_job")
    under_sev = "error" if phase == "delayed" else "warn"
    issues: list[dict[str, Any]] = []
    repair: list[dict[str, Any]] = []
    for label, bkeys, live_key, read_flag, rkind in _BUILD_DIMS:
        expected = _build_expected(build, bkeys)
        if expected is None or expected <= 0:
            continue
        if read_flag and not counts.get(read_flag):
            continue          # измерение не прочитано → молчим (tri-state fail-safe)
        if live_key == "keywords_count" and counts.get("keywords_truncated"):
            continue          # ответ Grid обрезан по лимиту → по ключам не судим
        if live_key == "adgroups" and counts.get("adgroups_truncated"):
            continue          # выборка групп обрезана по лимиту → по группам не судим
        if live_key == "ads" and counts.get("ads_truncated"):
            continue          # выборка объявлений обрезана по лимиту → по объявлениям не судим
        live = counts.get(live_key)
        if not isinstance(live, int):
            continue
        if (live_key == "keywords_count" and live <= 0
                and int(counts.get("at_by_design_kw_groups") or 0) > 0):
            # Автотаргетинговая кампания: группы живут на relevanceMatch, реальных ключей 0 —
            # это НЕ дефект (тот же признак, что гасит NO_KEYWORDS_LIVE, grid_read.py). Без гейта
            # прогон 2b9d58d01e28 дал 3 ложных BUILD_LIVE_MISSING и 3 лишних keyword-ремонта.
            continue
        if live <= 0:
            issues.append({"severity": "error", "code": "BUILD_LIVE_MISSING",
                           "name": nm, "id": cid, "dimension": label,
                           "expected": expected, "actual": live, "phase": phase,
                           "note": f"{label}: билдер создал {expected}, в кабинете 0"})
            repair.append(_keywords_repair(nm, cid) if rkind == "keywords" else _repair(nm, cid))
        elif live < expected:
            issues.append({"severity": under_sev, "code": "BUILD_LIVE_UNDERCOUNT",
                           "name": nm, "id": cid, "dimension": label,
                           "expected": expected, "actual": live, "phase": phase,
                           "note": (f"{label}: билдер создал {expected}, в кабинете {live}"
                                    + ("" if phase == "delayed"
                                       else " (часть ключей/объявлений может схлопнуться Директом "
                                            "как дубли; на in-job возможен лаг индексации Grid)"))})
            if phase == "delayed":
                repair.append(_keywords_repair(nm, cid) if rkind == "keywords" else _repair(nm, cid))
    issues.extend(_verify_build_ads_by_type(nm, cid, counts, build, phase))
    return issues, repair


def _verify_build_ads_by_type(nm: str, cid: int | None, counts: dict[str, Any],
                              build: dict[str, Any], phase: str) -> list[dict[str, Any]]:
    """Недобор ТЕКСТОВЫХ объявлений, замаскированный суммой (ревью этапа 1, находка A5).

    Измерение «объявления» сверяет СУММУ ``ads+shopping_ads+listing_ads`` против общего
    live-счётчика ``ads``, поэтому частичный перекос по типам сходится: build 10 текстовых +
    100 товарных против факта 0 + 110 даёт 110 == 110 и молчит.

    Раздельная сверка ДЁШЕВА: ``adaptive_total`` — это уже посчитанное число GdAdaptiveTextAd из
    того же ответа ``AdaptiveImages`` (``_enrich_adaptive_images`` и так разбирает ``__typename``),
    **ноль новых запросов**. Товарку не ломает: у неё ``build.ads`` отсутствует/0 → выходим сразу.

    ⚠️ ТОЛЬКО ``warn``, без repair-кандидата. Соответствие «``build.ads`` ⇒ GdAdaptiveTextAd»
    живым прогоном НЕ подтверждено: если какой-то билдер кладёт плоский GdTextAd, ``adaptive_total``
    будет 0 при живых объявлениях и код даст ложняк. Поднимать до ``error`` — только после того,
    как живой прогон покажет чистый ноль этого кода на здоровом наборе (см. DOD.md §1.b).
    """
    if _tp(nm) in (8, 9, 10):
        # Посевы are GdPostCampaigns: build.ads counts GdPostAd, not GdAdaptiveTextAd.
        # The total ads dimension above already verifies the post-ad count.
        return []
    if not counts.get("adaptive_images_read"):
        return []                       # типы не прочитаны → молчим (tri-state fail-safe)
    if counts.get("ads_truncated"):
        return []
    try:
        expected_text = int(build.get("ads") or 0)
    except (TypeError, ValueError):
        return []
    if "ads" not in build or expected_text <= 0:
        return []                       # товарка (build.ads=0/нет) — легитимно, не сверяем
    live_text = counts.get("adaptive_total")
    if not isinstance(live_text, int) or live_text >= expected_text:
        return []
    return [{"severity": "warn", "code": "BUILD_LIVE_TEXT_ADS_UNDERCOUNT",
             "name": nm, "id": cid, "dimension": "текстовые объявления",
             "expected": expected_text, "actual": live_text, "phase": phase,
             "note": (f"текстовые объявления: билдер создал {expected_text}, "
                      f"в кабинете {live_text} адаптивных (общая сумма по типам могла сойтись) "
                      f"— report-only")}]

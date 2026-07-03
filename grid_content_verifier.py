"""Pure Grid content checks for non-UAC Direct campaigns."""
from __future__ import annotations

import re
from typing import Any


_TP_RE = re.compile(r"^\s*tp(\d+)_", re.IGNORECASE)


def _repair(name: str, cid: int | None) -> dict[str, Any]:
    return {"kind": "rebuild_missing_content", "name": name, "id": cid}


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
    if tp in (2, 4, 5) and counts.get("groups_edit_read"):
        zero_kw = counts.get("search_zero_kw_groups")
        wrong_at = counts.get("wrong_autotarget_groups")
        if isinstance(zero_kw, int) and zero_kw > 0:
            issues.append({"severity": "error", "code": "NO_KEYWORDS_LIVE",
                           "name": nm, "id": cid, "actual": 0, "groups": zero_kw,
                           "note": "поисковые группы без ключей (auto-repair: keywords_repair)"})
            repair.append(_keywords_repair(nm, cid))
        if isinstance(wrong_at, int) and wrong_at > 0:
            issues.append({"severity": "error", "code": "WRONG_AUTOTARGET",
                           "name": nm, "id": cid, "groups": wrong_at,
                           "note": "неверный профиль автотаргета (нужен EXACT_V2_MARK/WITHOUT_BRAND)"})
            repair.append(_keywords_repair(nm, cid))
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

    # ── MINUS_PLACES_MISSING (report-only): tp1 РСЯ without disabled places ───
    dp = counts.get("disabled_places")
    exp_minus = exp.get("minus_places")
    if tp == 1 and isinstance(dp, list) and len(dp) == 0 and (exp_minus is None or int(exp_minus) > 0):
        issues.append({"severity": "warn", "code": "MINUS_PLACES_MISSING",
                       "name": nm, "id": cid,
                       "expected_minus_places": exp_minus,
                       "note": "минус-площадки РСЯ пусты у tp1 (report-only)"})

    # PRICE_MISSING (report-only) заменён на NO_ADPRICE_LIVE (с repair-candidate) ниже.

    # ── PROMO_MISSING (report-only, НЕ auto-repair) ───────────────────────────
    # Фиксируем только при ЯВНОМ ожидании промо (expects_promo=True).
    # promoExtensionId FieldUndefined в Grid-схеме → promo_extension_id всегда None → не детектируем.
    promo_present = bool(str(counts.get("promo_extension_id") or "").strip())
    exp_promo = exp.get("expects_promo")
    if (tp is not None and counts.get("settings_read") and not promo_present and bool(exp_promo)):
        issues.append({"severity": "warn", "code": "PROMO_MISSING",
                       "name": nm, "id": cid,
                       "note": "у кампании не привязано промо (report-only)"})

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

    # ── NO_ADPRICE_LIVE (warn): tp1 комбинаторные объявления без bannerPrice ─────
    # Более actionable версия PRICE_MISSING: несёт repair-candidate adprice_repair.
    # Детектируется только когда ad_price_read=True (bannerPrice{price} работает).
    # PRICE_MISSING (report-only) ниже не дублируем — оба бы сработали на том же условии.
    hp = counts.get("has_ad_price")
    exp_price = exp.get("expects_price")
    # Товарка-only (только ShoppingAd/ListingAd): bannerPrice неприменим — не выдаём NO_ADPRICE_LIVE.
    # Когда читалка shopping_bodies оживёт — заменить на EMPTY_DEFAULT_TEXT_LIVE.
    _shop_only_live = (int(counts.get("shopping_ads") or 0) > 0
                       or int(counts.get("listing_ads") or 0) > 0)
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

    return issues, repair

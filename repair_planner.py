"""Repair planning for Direct post-create verification.

This module is deliberately pure: it does not call Direct, Grid, DB, or Flask.
It converts verifier issues into ordered repair actions. Execution is a later
layer; keeping the plan separate makes it possible to review what would happen
before touching client campaigns.
"""
from __future__ import annotations

from typing import Any


_COOKIE_FIRST_CODES = {
    "CAMPAIGN_NOT_FOUND_IN_GRID",
    "UAC_NOT_FOUND_IN_GRID",
    "SEARCH_NOT_FINALIZED",
    "SHOPPING_NOT_FINALIZED",
    "GRID_FINALIZE_WARN",
}
# WRONG_AUTOTARGET УБРАН из этого набора (2026-07-07): in-place repair через Grid
# UpdateUnifiedAdGroups (execute_keywords_repair) выставляет EXACT_V2_MARK+WITHOUT_BRAND
# → предпочитаем keywords_repair, recreate только если in-place попытка не помогла
# (repair_attempts > 0 в issue). Набор оставлен для возможных будущих кодов.
_SEARCH_RECREATE_CODES: set = set()
# ⛔ Автоочистка картинок в поисковых кампаниях (IMAGES_FORBIDDEN → images_forbidden_repair)
# ОТМЕНЕНА решением Семёна 2026-07-19 — см. campaign_spec_audit.SEARCH_IMAGES_FORBIDDEN_RULE_ENABLED.
SEARCH_IMAGES_FORBIDDEN_RULE_ENABLED = False
_RECREATE_CODES = {
    "RESULT_FAILED",
    "CAMPAIGN_NOT_FOUND_IN_GRID",
    "CAMPAIGN_NOT_FOUND_IN_V5",
    "UAC_NOT_FOUND_IN_GRID",
    "UAC_NOT_DRAFT",
    "UAC_COUNTER_MISSING",
    "UAC_GOAL_MISSING",
    "UAC_REGION_MISSING",
    "UAC_UTM_MISSING",
    "UAC_PRICING_MISMATCH",
    "UAC_BUDGET_MISSING",
    "UAC_LIMIT_PERIOD_MISMATCH",
    "UAC_MAPS_ENABLED",
    "UAC_ALTERNATIVE_TEXTS_ENABLED",
    "UAC_RECOMMENDATIONS_ENABLED",
    "UAC_PRICE_RECOMMENDATIONS_ENABLED",
    "UAC_TITLES_MISSING",
    "UAC_TEXTS_MISSING",
    "UAC_SITELINKS_MISSING",
    "UAC_MEDIA_MISSING",
    "UAC_IMAGES_LOW",
    "UAC_VIDEO_MISSING",   # D3-UAC 2026-07-09: видео-марка без видео → recreate с довложением видео
    "UAC_FEED_MISSING",
    "UAC_PRODUCT_MODEL_FILTER_MISSING",
    "CAMPAIGN_ARCHIVED",
} | _SEARCH_RECREATE_CODES
_CONTENT_CODES = {
    "BUILD_ERROR",
    "NO_ADGROUPS_REPORTED",
    "NO_ADS_REPORTED",
    "NO_ADGROUPS_LIVE",
    "NO_ADS_LIVE",
    "ADGROUP_NAME_MISSING",
}


def _key(action: dict[str, Any]) -> tuple:
    # A single campaign needs one scoped repair pass per action type. Multiple
    # issue codes can describe the same missing payload (for example no groups
    # and no ads), but executing duplicate repair actions would only repeat IO.
    return (
        action.get("action"),
        action.get("campaign_id"),
        action.get("name"),
    )


def _name(issue: dict[str, Any]) -> str:
    return str(issue.get("name") or issue.get("expected") or "").strip()


def _cid(issue: dict[str, Any]) -> int | None:
    try:
        raw = issue.get("id") or issue.get("campaign_id")
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _action_for_issue(issue: dict[str, Any]) -> dict[str, Any] | None:
    code = str(issue.get("code") or "")
    name = _name(issue)
    cid = _cid(issue)
    # Происхождение находки едет в действие как есть: `source="resume_skip"` (проход
    # `live_verifier` по УЖЕ СУЩЕСТВУЮЩИМ tp6/tp7) — признак «кампания не наша, разрушительное
    # действие по ней запрещено», его читает `repair_gate.executable_uac_replace_campaigns`.
    src = str(issue.get("source") or "").strip()

    if code in _RECREATE_CODES:
        action = {
            "action": "resume_or_recreate_campaign",
            "transport": "cookie_grid" if code in _COOKIE_FIRST_CODES else "cookie_grid_preferred",
            "campaign_id": cid,
            "name": name,
            "issue_code": code,
            "uses_direct_units": False,
            "note": "создать/докрутить через cookie/Grid, v5 не использовать без отдельной необходимости",
        }
        if src:
            action["source"] = src
        if code in _SEARCH_RECREATE_CODES:
            # Пустые кампании (future codes): нужен destructive recreate.
            action["requires_campaign_delete"] = True
            action["note"] = ("поисковая кампания требует пересоздания через cookie/Grid "
                              "(destructive, только под гейтом)")
        return action
    if code in {"NAME_MISMATCH", "CAMPAIGN_NAME_EMPTY"}:
        return {
            "action": "rename_campaign",
            "transport": "grid_preferred",
            "campaign_id": cid,
            "name": str(issue.get("expected") or name),
            "actual": issue.get("actual"),
            "issue_code": code,
            "uses_direct_units": False,
            "note": "переименование делать через Grid, если доступно; v5 update только как fallback",
        }
    if code in _CONTENT_CODES:
        return {
            "action": "rebuild_missing_content",
            "transport": "cookie_grid",
            "campaign_id": cid,
            "name": name,
            "issue_code": code,
            "uses_direct_units": False,
            "note": "добить группы/объявления через существующие cookie/Grid builders",
        }
    # NO_KEYWORDS_LIVE: in-place через Grid AddKeywords (execute_keywords_repair). Recreate только
    # если in-place уже провалился (repair_attempts > 0 в issue — выставляется repair-executor'ом).
    if code == "NO_KEYWORDS_LIVE":
        attempts = int(issue.get("repair_attempts") or 0)
        if attempts > 0:
            return {
                "action": "resume_or_recreate_campaign",
                "transport": "cookie_grid_preferred",
                "campaign_id": cid,
                "name": name,
                "issue_code": code,
                "requires_campaign_delete": True,
                "uses_direct_units": False,
                "note": (f"in-place keyword-repair выполнялся {attempts} раз без результата "
                         "→ удалить кампанию и пересоздать через cookie/Grid"),
            }
        return {
            "action": "keywords_repair",
            "transport": "grid",
            "campaign_id": cid,
            "name": name,
            "issue_code": code,
            "uses_direct_units": False,
            "note": "дозалить ключи через Grid AddKeywords (in-place, executable_now)",
        }

    # WRONG_AUTOTARGET: in-place через execute_keywords_repair (UpdateUnifiedAdGroups ставит
    # EXACT_V2_MARK + WITHOUT_BRAND). Recreate только если in-place уже провалился
    # (repair_attempts > 0 в issue — выставляется repair-executor'ом).
    if code == "WRONG_AUTOTARGET":
        attempts = int(issue.get("repair_attempts") or 0)
        if attempts > 0:
            return {
                "action": "resume_or_recreate_campaign",
                "transport": "cookie_grid_preferred",
                "campaign_id": cid,
                "name": name,
                "issue_code": code,
                "requires_campaign_delete": True,
                "uses_direct_units": False,
                "note": (f"in-place autotarget-repair выполнялся {attempts} раз без результата "
                         "→ удалить кампанию и пересоздать через cookie/Grid"),
            }
        return {
            "action": "keywords_repair",
            "transport": "grid",
            "campaign_id": cid,
            "name": name,
            "issue_code": code,
            "uses_direct_units": False,
            "note": "выставить relevanceMatch search_tp2 (EXACT_V2_MARK + WITHOUT_BRAND) через Grid UpdateUnifiedAdGroups (in-place)",
        }

    if code == "KEYWORDS_WRONG_GROUP":
        return {
            "action": "keywords_wrong_group_repair",
            "transport": "grid",
            "campaign_id": cid,
            "name": name,
            "adgroup_id": issue.get("adgroup_id"),
            "expected_ct": issue.get("expected_ct"),
            "found_ct": issue.get("found_ct"),
            "issue_code": code,
            "uses_direct_units": False,
            "note": "снять сдвинутые ключи чужого ct (v5 keywords.delete) и залить эталонные (Grid AddKeywords)",
        }

    if code == "IMAGES_FORBIDDEN":
        # ⛔ Правило отменено Семёном 2026-07-19 (картинки в поиске допустимы, вкладка «Смена
        # изображения» их ставит) → действие не планируем даже если issue пришёл из старого отчёта.
        if not SEARCH_IMAGES_FORBIDDEN_RULE_ENABLED:
            return None
        return {
            "action": "images_forbidden_repair",
            "transport": "grid",
            "campaign_id": cid,
            "name": name,
            "issue_code": code,
            "uses_direct_units": False,
            "note": "очистить imageHashes у поисковых объявлений (UpdateAdaptiveTextAds, allow_empty_images)",
        }

    if code == "FEED_FILTER_MISSING_UAC":
        return {
            "action": "feed_filters_uac_repair",
            "transport": "uac_cookie",
            "campaign_id": cid,
            "name": name,
            "issue_code": code,
            "uses_direct_units": False,
            "note": "проставить feed_filters товарной UAC только для марочной/модельной tp7 "
                    "(positive по марке/модели; ct0000/общие без фильтра; PATCH uac/campaign)",
        }

    if code == "NO_LISTING":
        return {
            "action": "no_listing_repair",
            "transport": "grid",
            "campaign_id": cid,
            "name": name,
            "issue_code": code,
            "uses_direct_units": False,
            "note": "создать «Страницы каталога» от товарных объявлений (Grid by-shopping, "
                    "in-place; исполняется в спека-аудите fix_no_listing)",
        }

    if code == "FEED_FILTER_MISSING_GRID":
        return {
            "action": "feed_filter_grid_repair",
            "transport": "grid",
            "campaign_id": cid,
            "name": name,
            "issue_code": code,
            "uses_direct_units": False,
            "note": "проставить минус-марки feedFilter товарным/каталожным объявлениям "
                    "(updateShoppingAds/updateListingAds; исполняется в спека-аудите "
                    "fix_feed_filters_grid)",
        }

    if code == "PLACEMENTS_WRONG":
        return {
            "action": "placements_repair",
            "transport": "grid",
            "campaign_id": cid,
            "name": name,
            "issue_code": code,
            "uses_direct_units": False,
            "note": "выставить места показа PLACEMENTS_TP5 узким UpdateCampaigns "
                    "(исполняется в спека-аудите fix_placements_wrong)",
        }

    if code == "VIDEO_MISSING":
        return {
            "action": "video_missing_repair",
            "transport": "uac_cookie_grid",
            "campaign_id": cid,
            "name": name,
            "issue_code": code,
            "uses_direct_units": False,
            "note": "догрузить видео из пула M3 и привязать RMW-апдейтом (deferred-video; "
                    "исполняется в спека-аудите fix_video_missing, до полного нуля missing)",
        }

    if code == "BUTTON_MISSING":
        return {
            "action": "button_missing_repair",
            "transport": "grid",
            "campaign_id": cid,
            "name": name,
            "issue_code": code,
            "uses_direct_units": False,
            "note": "добить кнопку «Получить скидку» RMW-апдейтом (видео сохраняется через "
                    "typedCreatives; исполняется в спека-аудите fix_button_missing)",
        }

    if code == "SHORT_TITLES":
        return {
            "action": "short_titles_repair",
            "transport": "uac_cookie",
            "campaign_id": cid,
            "name": name,
            "issue_code": code,
            "uses_direct_units": False,
            "note": "добить короткие заголовки Мастера суффиксами (cookie PATCH uac/campaign, in-place; "
                    "исполняется в спека-аудите fix_short_titles)",
        }

    if code == "CONTENT_TEXTS_LOW":
        return {
            "action": "content_texts_repair",
            "transport": "grid",
            "campaign_id": cid,
            "name": name,
            "issue_code": code,
            "uses_direct_units": False,
            "note": "добить тексты объявления до ≥3 LLM-регенерацией + Grid RMW (in-place, без баллов; "
                    "исполняется в спека-аудите fix_texts_low)",
        }

    if code == "GLOBAL_MINUS_CAMPAIGN_MISSING":
        return {
            "action": "global_minus_campaign_repair",
            "transport": "grid",
            "campaign_id": cid,
            "name": name,
            "issue_code": code,
            "uses_direct_units": False,
            "note": "добить глоб.минус-слова на уровень кампании inline Grid UpdateCampaigns "
                    "(in-place, без баллов; исполняется в спека-аудите fix_global_minus_campaign)",
        }

    if code in ("NO_IMAGES_LIVE", "IMAGE_MISSING"):
        return {
            "action": "images_repair",
            "transport": "grid",
            "campaign_id": cid,
            "name": name,
            "issue_code": code,
            "uses_direct_units": False,
            "note": "пере-заливка картинок через RMW + UpdateAdaptiveTextAds (in-place; "
                    "IMAGE_MISSING исполняется и в спека-аудите fix_image_missing)",
        }

    if code == "NO_ADPRICE_LIVE":
        return {
            "action": "adprice_repair",
            "transport": "grid",
            "campaign_id": cid,
            "name": name,
            "issue_code": code,
            "uses_direct_units": False,
            "note": "проставить adPrice через _grid_set_ad_prices по прайс-кэшу (in-place)",
        }

    if code == "EMPTY_DEFAULT_TEXT_LIVE":
        return {
            "action": "default_text_repair",
            "transport": "grid",
            "campaign_id": cid,
            "name": name,
            "issue_code": code,
            "uses_direct_units": False,
            "note": "заполнить bodies ShoppingAd через set_default_text (in-place)",
        }

    # PROMO_MISSING — ПОКА report-only (не auto-repair): Grid-read поле promoExtensionId ещё НЕ
    # подтверждено живым запросом + нет per-item expects_promo → срабатывал бы на КАЖДОЙ кампании
    # без промо и плодил дубль-промо. Вернуть в set после live-верификации схемы + плюмбинга expected.
    if code == "PROMO_NOT_ATTACHED":
        return {
            "action": "create_or_attach_promo",
            "transport": "grid_then_attach",
            "campaign_id": cid,
            "name": name,
            "issue_code": code,
            "uses_direct_units": False,
            "note": "создать промо через Grid по выбранному слепку и привязать; процент должен совпасть с контентом",
        }
    if code in {"CALLOUTS_NOT_CONFIRMED"}:
        return {
            "action": "ensure_callouts",
            "transport": "grid",
            "campaign_id": cid,
            "name": name,
            "issue_code": code,
            "uses_direct_units": False,
            "note": "создать/найти уточнения через Grid и повесить на кампанию при финализации",
        }
    if code in {"LIVE_CHECK_SKIPPED", "GRID_CHECK_SKIPPED", "UAC_DETAIL_SKIPPED"}:
        return {
            "action": "retry_live_verification",
            "transport": "grid",
            "campaign_id": cid,
            "name": name,
            "issue_code": code,
            "uses_direct_units": False,
            "note": "повторить read-only Grid проверку до любых мутаций",
        }
    if code in {"CAMPAIGN_NOT_FOUND_IN_V5"}:
        return {
            "action": "verify_with_grid",
            "transport": "grid",
            "campaign_id": cid,
            "name": name,
            "issue_code": code,
            "uses_direct_units": False,
            "note": "сначала проверить Grid: v5 может не видеть UAC и тратит баллы",
        }
    return None


def _action_for_candidate(candidate: dict[str, Any]) -> dict[str, Any] | None:
    kind = str(candidate.get("kind") or "")
    name = str(candidate.get("name") or "").strip()
    if kind == "failed_campaign":
        return {
            "action": "resume_or_recreate_campaign",
            "transport": "cookie_grid",
            "campaign_id": _cid(candidate),
            "name": name,
            "issue_code": "FAILED_CAMPAIGN",
            "uses_direct_units": False,
            "note": "повторить недосозданную кампанию через cookie/Grid, особенно при малом остатке баллов",
            "error": str(candidate.get("error") or "")[:240],
        }
    if kind == "promo_attach_or_create":
        return {
            "action": "create_or_attach_promo",
            "transport": "grid_then_attach",
            "campaign_id": _cid(candidate),
            "name": name,
            "issue_code": "PROMO_ATTACH_OR_CREATE",
            "uses_direct_units": False,
            "note": "найти/создать промо через Grid и привязать к созданным кампаниям",
        }
    if kind == "callouts_verify":
        return {
            "action": "ensure_callouts",
            "transport": "grid",
            "campaign_id": _cid(candidate),
            "name": name,
            "issue_code": "CALLOUTS_VERIFY",
            "uses_direct_units": False,
            "note": "проверить/создать уточнения через Grid",
        }
    if kind == "keywords_repair":
        # in-place keywords_repair через Grid AddKeywords (подтверждён рабочим, 2026-07-03).
        # Recreate только если in-place уже провалился (repair_attempts > 0 у кандидата).
        attempts = int(candidate.get("repair_attempts") or 0)
        if attempts > 0:
            return {
                "action": "resume_or_recreate_campaign",
                "transport": "cookie_grid_preferred",
                "campaign_id": _cid(candidate),
                "name": name,
                "issue_code": "NO_KEYWORDS_LIVE",
                "requires_campaign_delete": True,
                "uses_direct_units": False,
                "note": (f"in-place keyword-repair выполнялся {attempts} раз без результата "
                         "→ удалить кампанию и пересоздать через cookie/Grid"),
            }
        return {
            "action": "keywords_repair",
            "transport": "grid",
            "campaign_id": _cid(candidate),
            "name": name,
            "issue_code": "NO_KEYWORDS_LIVE",
            "uses_direct_units": False,
            "note": "дозалить ключи через Grid AddKeywords (in-place, executable_now)",
        }
    if kind == "images_repair":
        return {
            "action": "images_repair",
            "transport": "grid",
            "campaign_id": _cid(candidate),
            "name": name,
            "issue_code": "NO_IMAGES_LIVE",
            "uses_direct_units": False,
            "note": "пере-заливка картинок через RMW + UpdateAdaptiveTextAds (in-place)",
        }
    if kind == "adprice_repair":
        return {
            "action": "adprice_repair",
            "transport": "grid",
            "campaign_id": _cid(candidate),
            "name": name,
            "issue_code": "NO_ADPRICE_LIVE",
            "uses_direct_units": False,
            "note": "проставить adPrice через _grid_set_ad_prices по прайс-кэшу (in-place)",
        }
    if kind == "default_text_repair":
        return {
            "action": "default_text_repair",
            "transport": "grid",
            "campaign_id": _cid(candidate),
            "name": name,
            "issue_code": "EMPTY_DEFAULT_TEXT_LIVE",
            "uses_direct_units": False,
            "note": "заполнить bodies ShoppingAd через set_default_text (in-place)",
        }
    if kind == "campaign_invariant_repair":
        return {
            "action": "campaign_invariant_repair",
            "transport": "grid",
            "campaign_id": _cid(candidate),
            "name": name,
            "issue_code": "CAMPAIGN_INVARIANT_WRONG",
            "uses_direct_units": False,
            "note": ("переставить кампанийные инварианты-галочки tp1–tp5 (персонализация/расш.гео/"
                     "«Директ помогает»/Карты/организации OFF, мониторинг ON) через Grid "
                     "set_campaign_invariants — UpdateCampaigns, in-place, БЕЗ баллов, DRAFT, идемпотентно"),
        }
    return None


def build_repair_plan(report: dict[str, Any] | None) -> dict[str, Any]:
    """Build a deterministic repair plan from a verification report."""
    report = report or {}
    issues = list(report.get("issues") or [])
    actions: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        action = _action_for_issue(issue)
        if not action:
            continue
        k = _key(action)
        if k in seen:
            continue
        seen.add(k)
        actions.append(action)
    for candidate in list(report.get("repair_candidates") or []):
        if not isinstance(candidate, dict):
            continue
        action = _action_for_candidate(candidate)
        if not action:
            continue
        k = _key(action)
        if k in seen:
            continue
        seen.add(k)
        actions.append(action)

    severity_order = {
        "resume_or_recreate_campaign": 0,
        "rebuild_missing_content": 1,
        "keywords_repair": 2,
        "keywords_wrong_group_repair": 2,
        "images_forbidden_repair": 3,
        "images_repair": 3,
        "adprice_repair": 4,
        "default_text_repair": 5,
        "campaign_invariant_repair": 5,
        "create_or_attach_promo": 6,
        "ensure_callouts": 7,
        "rename_campaign": 8,
        "retry_live_verification": 9,
        "verify_with_grid": 10,
    }
    actions.sort(key=lambda a: (severity_order.get(str(a.get("action")), 99), str(a.get("name") or "")))
    return {
        "status": "actionable" if actions else "empty",
        "summary": {
            "actions": len(actions),
            "uses_direct_units": sum(1 for a in actions if a.get("uses_direct_units")),
            "cookie_grid_actions": sum(1 for a in actions if "grid" in str(a.get("transport") or "")),
        },
        "actions": actions[:120],
    }

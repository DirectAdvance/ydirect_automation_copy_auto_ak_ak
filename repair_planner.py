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
# Поисковые кампании с пустыми группами / битым автотаргетом. IN-PLACE keyword-repair невозможен:
# UpdateUnifiedAdGroups НЕ добавляет ключи в существующую группу (Яндекс отвечает success, но kw
# остаётся 0 — подтверждено уникальным ключом) + кампания упирается в лимит 10000 ключей. Единственный
# способ починить — удалить кампанию и пересоздать с корректным cap. Поэтому эти коды идут в RECREATE
# и помечаются requires_campaign_delete (кампания реально существует и её надо снести перед пересозданием).
_SEARCH_RECREATE_CODES = {
    "NO_KEYWORDS_LIVE",
    "WRONG_AUTOTARGET",
}
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
        if code in _SEARCH_RECREATE_CODES:
            # Кампания РЕАЛЬНО существует, но с пустыми поисковыми группами / битым автотаргетом.
            # IN-PLACE keyword-repair не работает (UpdateUnifiedAdGroups no-op) → её надо снести и
            # пересоздать с корректным cap. requires_campaign_delete → авто-путь такое НЕ трогает
            # (destructive), исполняется только под явным гейтом.
            action["requires_campaign_delete"] = True
            action["note"] = ("поисковая кампания с пустыми группами/битым автотаргетом: "
                              "in-place keyword-repair невозможен — удалить кампанию и пересоздать "
                              "с корректным cap через cookie/Grid (destructive, только под гейтом)")
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
    # NO_KEYWORDS_LIVE / WRONG_AUTOTARGET обрабатываются выше через _RECREATE_CODES
    # (in-place keywords_repair через UpdateUnifiedAdGroups — подтверждённый no-op, отключён).
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
        # in-place keywords_repair (UpdateUnifiedAdGroups) — подтверждённый no-op. Пустую поисковую
        # группу чинит только пересоздание кампании, поэтому кандидат маппится в recreate с гейтом
        # на удаление существующей битой кампании (см. _SEARCH_RECREATE_CODES).
        return {
            "action": "resume_or_recreate_campaign",
            "transport": "cookie_grid_preferred",
            "campaign_id": _cid(candidate),
            "name": name,
            "issue_code": "NO_KEYWORDS_LIVE",
            "requires_campaign_delete": True,
            "uses_direct_units": False,
            "note": ("поисковая группа без ключей: in-place repair невозможен — удалить кампанию "
                     "и пересоздать с корректным cap (destructive, только под гейтом)"),
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
        "create_or_attach_promo": 3,
        "ensure_callouts": 4,
        "rename_campaign": 5,
        "retry_live_verification": 6,
        "verify_with_grid": 7,
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

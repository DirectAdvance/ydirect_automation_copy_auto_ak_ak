"""Helpers for create_set repair-gate endpoints.

This module is intentionally Flask/DB-free. It normalizes stored job payloads
and request booleans so the heavy blueprint only wires HTTP, locks, and live IO.
"""
from __future__ import annotations

import json
from typing import Any

_UAC_REPLACE_CODES = {
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
}


def truthy(value: Any) -> bool:
    """Return true only for explicit true-ish values.

    Important for JSON APIs: the string ``"false"`` must not enable repair
    execution or v5 checks.
    """
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def dict_from_jsonish(value: Any) -> dict[str, Any] | None:
    """Decode dict or JSON string; return None for invalid/non-dict values."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:  # noqa: BLE001
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def normalize_job_context(job: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Build the shared context used by verification and repair endpoints."""
    body_obj = dict_from_jsonish(job.get("body")) or {}
    return {
        "login": (job.get("login") or result.get("login") or "").strip(),
        "agency": (job.get("agency") or body_obj.get("agency") or ""),
        "body": body_obj,
        "results": result.get("results") or [],
    }


def build_repair_gate_payload(*, job_id: str, job: dict[str, Any], result: dict[str, Any],
                              live_report: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """Shape a stable read-only response for the repair-gate API."""
    return {
        "ok": True,
        "execute": False,
        "job_id": job_id,
        "login": (job.get("login") or result.get("login") or "").strip(),
        "status": job.get("status"),
        "stored_verification": result.get("verification"),
        "live_verification": live_report,
        "repair_plan": plan,
    }


def executable_recreate_items(body: dict[str, Any], plan: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pick original create_set items that can be safely retried via queue.

    Only ``resume_or_recreate_campaign`` is executable in v1. Other action
    types need a narrower in-place executor and are returned as unsupported.
    """
    items = [it for it in (body.get("items") or []) if isinstance(it, dict)]
    actions = [a for a in (plan.get("actions") or []) if isinstance(a, dict)]
    selected: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _match(item_name: str, action_name: str) -> bool:
        item_name = (item_name or "").strip()
        action_name = (action_name or "").strip()
        if not item_name or not action_name:
            return False
        return (
            item_name == action_name
            or action_name.startswith(item_name + " — ")
            or item_name.startswith(action_name + " — ")
        )

    for action in actions:
        if action.get("action") != "resume_or_recreate_campaign":
            unsupported.append(action)
            continue
        action_name = str(action.get("name") or "").strip()
        if not action_name:
            unsupported.append(action)
            continue
        matches = [it for it in items if _match(str(it.get("name") or ""), action_name)]
        if not matches:
            unsupported.append(action)
            continue
        for it in matches:
            item_name = str(it.get("name") or "")
            if item_name in seen:
                continue
            seen.add(item_name)
            selected.append(dict(it))
    return selected, unsupported


def executable_uac_replace_campaigns(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Pick UAC campaigns that must be deleted before queued recreate.

    A normal resume repair would skip an existing UAC draft by name. For
    incomplete UAC payloads, delete the specific draft first, then queue the
    original item.
    """
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for action in (plan.get("actions") or []):
        if not isinstance(action, dict):
            continue
        if action.get("action") != "resume_or_recreate_campaign":
            continue
        if str(action.get("issue_code") or "") not in _UAC_REPLACE_CODES:
            continue
        try:
            cid = int(action.get("campaign_id") or 0)
        except (TypeError, ValueError):
            cid = 0
        name = str(action.get("name") or "").strip()
        if cid <= 0 or cid in seen or not name.lower().startswith(("tp6_", "tp7_")):
            continue
        seen.add(cid)
        out.append({"campaign_id": cid, "name": name, "issue_code": action.get("issue_code")})
    return out


def executable_recreate_delete_campaigns(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Pick EXISTING campaigns that a recreate action must delete first.

    ``requires_campaign_delete`` is set by the planner for search campaigns with empty groups /
    broken autotargeting (NO_KEYWORDS_LIVE / WRONG_AUTOTARGET): they cannot be fixed in place
    (UpdateUnifiedAdGroups is a confirmed no-op), so the only fix is delete + recreate. Deleting a
    live non-UAC campaign is destructive, so this is surfaced separately and gated — it is NOT part
    of the auto post-create path.
    """
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for action in (plan or {}).get("actions") or []:
        if not isinstance(action, dict):
            continue
        if action.get("action") != "resume_or_recreate_campaign":
            continue
        if not action.get("requires_campaign_delete"):
            continue
        try:
            cid = int(action.get("campaign_id") or 0)
        except (TypeError, ValueError):
            cid = 0
        name = str(action.get("name") or "").strip()
        if cid <= 0 or cid in seen:
            continue
        seen.add(cid)
        out.append({"campaign_id": cid, "name": name, "issue_code": action.get("issue_code")})
    return out


def result_campaign_ids(results: list[Any]) -> list[int]:
    """Collect successful campaign ids from a create_set result tree."""
    ids: list[int] = []
    seen: set[int] = set()

    def _push(raw: Any) -> None:
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            return
        if cid > 0 and cid not in seen:
            seen.add(cid)
            ids.append(cid)

    def _walk(row: Any) -> None:
        if not isinstance(row, dict):
            return
        if row.get("ok") or row.get("skipped"):
            _push(row.get("id") or row.get("campaign_id"))
        for child in row.get("campaigns") or []:
            _walk(child)

    for row in results or []:
        _walk(row)
    return ids


def executable_promo_campaign_ids(results: list[Any], plan: dict[str, Any]) -> tuple[list[int], list[dict[str, Any]], list[dict[str, Any]]]:
    """Pick campaign ids for create_or_attach_promo actions.

    If actions do not carry campaign ids, attach promo to all successful
    campaigns from the original result. This matches create_set finalization:
    promo is a set-level asset, not a per-item rewrite.
    """
    actions = [a for a in (plan.get("actions") or []) if isinstance(a, dict)]
    promo_actions = [a for a in actions if a.get("action") == "create_or_attach_promo"]
    unsupported = [a for a in actions if a.get("action") != "create_or_attach_promo"]
    if not promo_actions:
        return [], [], unsupported

    all_ids = result_campaign_ids(results)
    ids: list[int] = []
    seen: set[int] = set()
    for action in promo_actions:
        try:
            cid = int(action.get("campaign_id") or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid > 0 and cid not in seen:
            seen.add(cid)
            ids.append(cid)
    if not ids:
        ids = all_ids
    return ids, promo_actions, unsupported


def executable_callout_campaign_ids(results: list[Any], plan: dict[str, Any]) -> tuple[list[int], list[dict[str, Any]], list[dict[str, Any]]]:
    """Pick campaign ids for ensure_callouts actions.

    Static verification often reports callouts as a set-level warning without a
    campaign id. In that case attach selected callouts to all successful
    campaigns from the original result.
    """
    actions = [a for a in (plan.get("actions") or []) if isinstance(a, dict)]
    callout_actions = [a for a in actions if a.get("action") == "ensure_callouts"]
    unsupported = [a for a in actions if a.get("action") != "ensure_callouts"]
    if not callout_actions:
        return [], [], unsupported

    all_ids = result_campaign_ids(results)
    ids: list[int] = []
    seen: set[int] = set()
    for action in callout_actions:
        try:
            cid = int(action.get("campaign_id") or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid > 0 and cid not in seen:
            seen.add(cid)
            ids.append(cid)
    if not ids:
        ids = all_ids
    return ids, callout_actions, unsupported


def executable_rename_campaigns(plan: dict[str, Any]) -> tuple[dict[int, str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Pick campaign id -> expected name pairs for rename_campaign actions."""
    actions = [a for a in (plan.get("actions") or []) if isinstance(a, dict)]
    rename_actions = [a for a in actions if a.get("action") == "rename_campaign"]
    unsupported = [a for a in actions if a.get("action") != "rename_campaign"]
    names: dict[int, str] = {}
    executable: list[dict[str, Any]] = []
    for action in rename_actions:
        try:
            cid = int(action.get("campaign_id") or 0)
        except (TypeError, ValueError):
            cid = 0
        name = str(action.get("name") or "").strip()
        if cid <= 0 or not name:
            unsupported.append(action)
            continue
        if cid not in names:
            names[cid] = name
            executable.append(action)
    return names, executable, unsupported


def executable_content_repairs(body: dict[str, Any], plan: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pick safe in-place content repairs.

    Supports text tp2/tp4 and simple shopping-gallery tp3/tp5 repairs.
    UAC campaigns need a different API and stay unsupported.
    """
    items = [it for it in (body.get("items") or []) if isinstance(it, dict)]
    actions = [a for a in (plan.get("actions") or []) if isinstance(a, dict)]
    selected: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    seen: set[int] = set()

    def _match(item_name: str, action_name: str) -> bool:
        item_name = (item_name or "").strip()
        action_name = (action_name or "").strip()
        if not item_name or not action_name:
            return False
        return (
            item_name == action_name
            or action_name.startswith(item_name + " — ")
            or item_name.startswith(action_name + " — ")
        )

    for action in actions:
        if action.get("action") != "rebuild_missing_content":
            unsupported.append(action)
            continue
        try:
            cid = int(action.get("campaign_id") or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid <= 0 or cid in seen:
            unsupported.append(action)
            continue
        action_name = str(action.get("name") or "").strip()
        matches = [it for it in items if _match(str(it.get("name") or ""), action_name)]
        item = matches[0] if matches else None
        item_type = str((item or {}).get("type") or "")
        if item_type not in {"search_test", "search_dynamic", "search_gallery", "rsya_gallery"}:
            unsupported.append(action)
            continue
        row = dict(action)
        row["item"] = dict(item or {})
        row["campaign_id"] = cid
        row["content_kind"] = "shopping" if item_type in {"search_gallery", "rsya_gallery"} else "text"
        selected.append(row)
        seen.add(cid)
    return selected, unsupported


def _executable_cid_actions(plan: dict[str, Any],
                             action_type: str) -> tuple[list[int], list[dict[str, Any]], list[dict[str, Any]]]:
    """Generic picker: campaign ids for a given in-place action type with a concrete campaign_id."""
    actions = [a for a in (plan or {}).get("actions") or [] if isinstance(a, dict)]
    matched = [a for a in actions if a.get("action") == action_type]
    unsupported = [a for a in actions if a.get("action") != action_type]
    ids: list[int] = []
    executable: list[dict[str, Any]] = []
    seen: set[int] = set()
    for action in matched:
        try:
            cid = int(action.get("campaign_id") or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid <= 0:
            unsupported.append(action)
            continue
        if cid in seen:
            continue
        seen.add(cid)
        ids.append(cid)
        executable.append(action)
    return ids, executable, unsupported


def executable_images_repairs(plan: dict[str, Any]) -> tuple[list[int], list[dict[str, Any]], list[dict[str, Any]]]:
    """Pick campaign ids for images_repair actions (tp1 ads without imageHashes)."""
    return _executable_cid_actions(plan, "images_repair")


def executable_adprice_repairs(plan: dict[str, Any]) -> tuple[list[int], list[dict[str, Any]], list[dict[str, Any]]]:
    """Pick campaign ids for adprice_repair actions (tp1 ads with null bannerPrice)."""
    return _executable_cid_actions(plan, "adprice_repair")


def executable_default_text_repairs(plan: dict[str, Any]) -> tuple[list[int], list[dict[str, Any]], list[dict[str, Any]]]:
    """Pick campaign ids for default_text_repair actions (ShoppingAd without bodies)."""
    return _executable_cid_actions(plan, "default_text_repair")


def executable_campaign_invariant_repairs(plan: dict[str, Any]) -> tuple[list[int], list[dict[str, Any]], list[dict[str, Any]]]:
    """Pick campaign ids for campaign_invariant_repair actions (tp1–tp5 галочки: персонализация/
    расш.гео/«Директ помогает»/Карты/организации OFF, мониторинг ON). Grid-only, idempotent, no units."""
    return _executable_cid_actions(plan, "campaign_invariant_repair")


def executable_images_forbidden_repairs(plan: dict[str, Any]) -> tuple[list[int], list[dict[str, Any]], list[dict[str, Any]]]:
    """Pick campaign ids for images_forbidden_repair actions (search tp2/tp4 ads with imageHashes)."""
    return _executable_cid_actions(plan, "images_forbidden_repair")


def executable_keywords_repairs(plan: dict[str, Any]) -> tuple[list[int], list[dict[str, Any]], list[dict[str, Any]]]:
    """Pick campaign ids for keywords_repair actions (search adgroup keywords/autotarget fix).

    These are idempotent, cookie/Grid-only, per-campaign read-modify-write repairs. Actions
    without a campaign id are unsupported (the executor needs a concrete campaign to read groups).
    """
    actions = [a for a in (plan or {}).get("actions") or [] if isinstance(a, dict)]
    kw_actions = [a for a in actions if a.get("action") == "keywords_repair"]
    unsupported = [a for a in actions if a.get("action") != "keywords_repair"]
    ids: list[int] = []
    executable: list[dict[str, Any]] = []
    seen: set[int] = set()
    for action in kw_actions:
        try:
            cid = int(action.get("campaign_id") or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid <= 0:
            unsupported.append(action)
            continue
        if cid in seen:
            continue
        seen.add(cid)
        ids.append(cid)
        executable.append(action)
    return ids, executable, unsupported


def summarize_repair_gate(body: dict[str, Any], results: list[Any], plan: dict[str, Any]) -> dict[str, Any]:
    """Return a compact execute-readiness summary for UI/status responses."""
    actions = [a for a in (plan or {}).get("actions") or [] if isinstance(a, dict)]
    recreate_items, _ = executable_recreate_items(body or {}, plan or {})
    content_repairs, _ = executable_content_repairs(body or {}, plan or {})
    keyword_ids, keyword_actions, _ = executable_keywords_repairs(plan or {})
    images_ids, images_actions, _ = executable_images_repairs(plan or {})
    img_forbidden_ids, img_forbidden_actions, _ = executable_images_forbidden_repairs(plan or {})
    adprice_ids, adprice_actions, _ = executable_adprice_repairs(plan or {})
    default_text_ids, default_text_actions, _ = executable_default_text_repairs(plan or {})
    invariant_ids, invariant_actions, _ = executable_campaign_invariant_repairs(plan or {})
    promo_ids, promo_actions, _ = executable_promo_campaign_ids(results or [], plan or {})
    callout_ids, callout_actions, _ = executable_callout_campaign_ids(results or [], plan or {})
    rename_names, rename_actions, _ = executable_rename_campaigns(plan or {})
    uac_replacements = executable_uac_replace_campaigns(plan or {})
    recreate_delete = executable_recreate_delete_campaigns(plan or {})
    executable_total = (
        len(recreate_items)
        + len(content_repairs)
        + len(keyword_ids)
        + len(images_ids)
        + len(img_forbidden_ids)
        + len(adprice_ids)
        + len(default_text_ids)
        + len(invariant_ids)
        + (1 if promo_ids and promo_actions else 0)
        + (1 if callout_ids and callout_actions else 0)
        + len(rename_names)
    )
    return {
        "status": (plan or {}).get("status") or "empty",
        "actions": len(actions),
        "executable_now": executable_total,
        "queued_recreate_items": len(recreate_items),
        "recreate_delete_campaigns": len(recreate_delete),
        "in_place_content_repairs": len(content_repairs),
        # keyword_repair_campaigns: in-place через Grid AddKeywords (2026-07-03 подтверждён рабочим)
        "keyword_repair_campaigns": len(keyword_ids),
        "images_repair_campaigns": len(images_ids),
        "images_forbidden_repair_campaigns": len(img_forbidden_ids),
        "adprice_repair_campaigns": len(adprice_ids),
        "default_text_repair_campaigns": len(default_text_ids),
        "campaign_invariant_repair_campaigns": len(invariant_ids),
        "promo_campaigns": len(promo_ids) if promo_actions else 0,
        "callout_campaigns": len(callout_ids) if callout_actions else 0,
        "rename_campaigns": len(rename_names),
        "uac_replace_campaigns": len(uac_replacements),
        "uses_direct_units": bool((plan or {}).get("summary", {}).get("uses_direct_units")),
        "execute_endpoint": "/direct/api/create_set_repair",
        "execute_method": "POST",
    }


def repair_queue_body(parent_body: dict[str, Any], items: list[dict[str, Any]],
                      *, parent_job_id: str, force_names: list[str] | None = None) -> dict[str, Any]:
    """Build a create_set body for a queued repair job."""
    body = dict(parent_body or {})
    body["items"] = [dict(it) for it in items]
    body["via_cookie"] = True
    body["launch"] = False
    body["_repair_parent_job_id"] = parent_job_id
    if force_names:
        body["_repair_force_names"] = [str(n).strip() for n in force_names if str(n or "").strip()]
    body.pop("_job_id", None)
    body.pop("_resume_count", None)
    body.pop("_deferred_id", None)
    return body

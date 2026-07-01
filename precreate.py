"""Pre-create planning for Direct create_set.

This module is Flask-free and side-effect free. It describes assets that should
exist before campaign upload, so the mutable orchestration can be moved out of
``blueprint.py`` step by step without changing create behavior.
"""
from __future__ import annotations

from typing import Any


_TOKEN_TP_PREFIXES = ("tp1_", "tp2_", "tp3_", "tp4_", "tp5_")
_UAC_TP_PREFIXES = ("tp6_", "tp7_")


def _name(row: dict[str, Any] | None) -> str:
    return str((row or {}).get("name") or "").strip()


def _tp_name(item: dict[str, Any]) -> str:
    return _name(item).lower()


def _tp_type(item: dict[str, Any]) -> str:
    return str((item or {}).get("type") or "").strip().lower()


def _is_token_campaign(item: dict[str, Any]) -> bool:
    name = _tp_name(item)
    typ = _tp_type(item)
    return name.startswith(_TOKEN_TP_PREFIXES) or typ.startswith(("search", "rsya", "gallery"))


def _is_uac_campaign(item: dict[str, Any]) -> bool:
    name = _tp_name(item)
    typ = _tp_type(item)
    return name.startswith(_UAC_TP_PREFIXES) or typ.startswith(("tp6", "tp7", "master", "product"))


def _has_content(item: dict[str, Any]) -> bool:
    return bool(item.get("titles") and item.get("texts"))


def _has_images(item: dict[str, Any]) -> bool:
    return bool(item.get("image_files") or item.get("images") or item.get("image_urls"))


def build_precreate_report(*, login: str, body: dict[str, Any] | None,
                           items: list[dict[str, Any]] | None,
                           account: dict[str, Any] | None = None,
                           existing_names_count: int | None = None) -> dict[str, Any]:
    """Return a deterministic pre-create plan for a create_set request."""
    body = body or {}
    items = [it for it in (items or []) if isinstance(it, dict)]
    account = account or {}
    agent = str(body.get("agent") or "").strip()
    callouts = [str(x or "").strip() for x in (body.get("callouts") or []) if str(x or "").strip()]
    token_items = [it for it in items if _is_token_campaign(it)]
    uac_items = [it for it in items if _is_uac_campaign(it)]
    content_ready = sum(1 for it in items if _has_content(it))
    image_items = [it for it in items if _has_images(it)]

    actions: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    actions.append({
        "action": "read_existing_campaign_names",
        "status": "done" if existing_names_count is not None else "planned",
        "transport": "grid",
        "uses_direct_units": False,
        "count": existing_names_count,
        "note": "bulk read before create prevents duplicate campaigns on resume/retry",
    })
    if agent:
        actions.append({
            "action": "ensure_promo_library",
            "status": "planned",
            "transport": "grid",
            "uses_direct_units": False,
            "slepok": agent,
            "note": "promo must be created from selected slepok and later attached to created campaign ids",
        })
    else:
        warnings.append({"code": "PRECREATE_SLEPOK_MISSING", "message": "promo precreate needs selected agent/slepok"})
    if callouts:
        actions.append({
            "action": "ensure_callouts",
            "status": "planned",
            "transport": "grid",
            "uses_direct_units": False,
            "count": len(callouts),
            "note": "callouts should exist before Grid finalize/updateCampaigns",
        })
    if token_items and agent:
        actions.append({
            "action": "ensure_minus_libraries",
            "status": "planned",
            "transport": "grid_or_v5_fallback",
            "uses_direct_units": False,
            "count": len(token_items),
            "note": "minus sets/direct minus assets are derived from selected slepok before tp1-tp5 finalization",
        })
    if image_items:
        actions.append({
            "action": "prefetch_images",
            "status": "planned",
            "transport": "local_m3_cache",
            "uses_direct_units": False,
            "count": len(image_items),
            "note": "warm local image paths before UAC/Grid upload",
        })
    if body.get("stream_content"):
        actions.append({
            "action": "prefetch_ai_content",
            "status": "active_in_create_set",
            "transport": "m3",
            "uses_direct_units": False,
            "note": "create_set already prefetches nearby item content with a small worker pool",
        })
    if content_ready < len(items):
        warnings.append({
            "code": "PRECREATE_CONTENT_PARTIAL",
            "message": "not all items have titles/texts before upload",
            "ready": content_ready,
            "items": len(items),
        })

    return {
        "status": "warn" if warnings else "planned",
        "login": login,
        "site_type": account.get("site_type") or body.get("site_type") or "",
        "summary": {
            "items": len(items),
            "token_items": len(token_items),
            "uac_items": len(uac_items),
            "content_ready": content_ready,
            "image_items": len(image_items),
            "actions": len(actions),
            "warnings": len(warnings),
            "uses_direct_units": sum(1 for a in actions if a.get("uses_direct_units")),
        },
        "actions": actions,
        "warnings": warnings,
    }


def promo_content_lines(items: list[dict[str, Any]] | None) -> list[str]:
    """Collect create-set text assets used to validate account promo compatibility."""
    lines: list[str] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        lines += [str(x or "") for x in (it.get("titles") or [])]
        lines += [str(x or "") for x in (it.get("texts") or [])]
        for s in (it.get("sitelinks") or []):
            if isinstance(s, dict):
                lines.append(str(s.get("title") or ""))
                lines.append(str(s.get("description") or ""))
    return lines


def _refresh_summary(report: dict[str, Any] | None) -> None:
    if not isinstance(report, dict):
        return
    actions = [a for a in (report.get("actions") or []) if isinstance(a, dict)]
    warnings = [w for w in (report.get("warnings") or []) if isinstance(w, dict)]
    summary = report.setdefault("summary", {})
    summary["actions"] = len(actions)
    summary["warnings"] = len(warnings)
    summary["uses_direct_units"] = sum(1 for a in actions if a.get("uses_direct_units"))


def _set_action(report: dict[str, Any] | None, action: str, **fields: Any) -> None:
    if not isinstance(report, dict):
        return
    for row in report.get("actions") or []:
        if isinstance(row, dict) and row.get("action") == action:
            row.update(fields)
            return


def execute_precreate_assets(
    *,
    report: dict[str, Any] | None,
    login: str,
    items: list[dict[str, Any]],
    agent: str,
    stream_content: bool,
    token: str | None,
    client: Any,
    ctx: dict[str, Any],
    site_type: str,
    callouts: list[str],
    dedup_callouts,
    callout_cap: int,
    grid_client_factory,
    v5_get,
    promo_usable_for_content,
    create_account_promo_from_slepok,
    selected_slepok_key,
) -> dict[str, Any]:
    """Create assets that should exist before campaign upload.

    All Direct-specific operations are injected by the caller. This keeps the
    module Flask-free and prevents route code from accumulating orchestration.
    """
    promo_id = None
    promo_note = None
    promo_skipped: list[tuple[Any, str]] = []
    callout_ids: list[int] = []
    callouts_note = None

    try:
        if callouts:
            clean_callouts = dedup_callouts(callouts, cap=callout_cap)
            if clean_callouts:
                callout_ids = list(
                    grid_client_factory(login).add_callouts(clean_callouts).values()
                )[:callout_cap]
                callouts_note = (
                    f"precreate: уточнения подготовлены через Grid: {len(callout_ids)}/{len(clean_callouts)}"
                )
    except Exception as exc:  # noqa: BLE001 - callouts must not block campaign upload
        callouts_note = f"precreate: уточнения не подготовлены: {str(exc)[:160]}"

    if callouts_note:
        _set_action(
            report,
            "ensure_callouts",
            status="done" if callout_ids else "skipped",
            callout_ids=callout_ids,
        )
        if isinstance(report, dict):
            report["callouts"] = {
                "ids": callout_ids,
                "count": len(callout_ids),
                "note": callouts_note,
                "uses_direct_units": False,
            }

    try:
        all_content_ready = bool(items) and all((it.get("titles") and it.get("texts")) for it in items)
        if token and agent and all_content_ready and not stream_content:
            content_lines = promo_content_lines(items)
            jp = v5_get(
                "promotions",
                token,
                login,
                ["Id", "Type", "Name", "Description", "Amount", "AmountPrefix", "AmountUnit", "Promocode"],
                criteria={},
            )
            promos_all = [p for p in (jp.get("result") or {}).get("Promotions", []) if p.get("Id")]
            usable_promos = []
            for promo in promos_all:
                ok, why = promo_usable_for_content(promo, content_lines)
                if ok:
                    usable_promos.append(promo)
                else:
                    promo_skipped.append((promo.get("Id"), why))
            if usable_promos:
                promo_id = usable_promos[0]["Id"]
                promo_note = (
                    f"precreate: найдено пригодное промо аккаунта id {promo_id}"
                    + (f"; в аккаунте промо: {len(promos_all)}" if len(promos_all) > 1 else "")
                    + (f"; пропущено конфликтных: {len(promo_skipped)}" if promo_skipped else "")
                )
            else:
                created_id, created_note = create_account_promo_from_slepok(
                    client,
                    login,
                    token,
                    {**ctx, "site_type": site_type},
                    selected_slepok_key(agent),
                    content_lines,
                )
                if created_id:
                    promo_id = created_id
                    promo_note = (
                        ("precreate: в аккаунте не было пригодных промо; " if promos_all
                         else "precreate: в аккаунте не было промо; ")
                        + created_note
                        + (f"; пропущено конфликтных: {len(promo_skipped)}" if promo_skipped else "")
                    )
                elif promos_all:
                    promo_note = (
                        f"precreate: промо не создано, все промо аккаунта конфликтуют ({len(promos_all)}); "
                        f"{created_note}"
                    )
                else:
                    promo_note = f"precreate: промо не создано; {created_note}"
        elif isinstance(report, dict):
            report.setdefault("notes", []).append(
                "promo precreate skipped: content is not fully ready before upload or stream_content is enabled"
            )
    except Exception as exc:  # noqa: BLE001 - promo precreate must not block campaign upload
        promo_note = f"precreate: промо не подготовлено: {str(exc)[:160]}"

    if promo_note:
        _set_action(
            report,
            "ensure_promo_library",
            status="done" if promo_id else "skipped",
            promo_id=promo_id,
            uses_direct_units=True,
            transport="v5_read_grid_create",
        )
        if isinstance(report, dict):
            report["promo"] = {
                "id": promo_id,
                "note": promo_note,
                "skipped": len(promo_skipped),
                "uses_direct_units": True,
            }

    _refresh_summary(report)
    return {
        "report": report,
        "promo_id": promo_id,
        "promo_note": promo_note,
        "promo_skipped": promo_skipped,
        "callout_ids": callout_ids,
        "callouts_note": callouts_note,
    }

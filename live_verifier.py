"""Read-only live verification for Direct create_set jobs.

The static verifier checks the response shape. This module normalizes created
campaigns and compares them with already-fetched Direct/Grid state. It has no
Flask dependency and does not create, update, delete, or publish anything.
"""
from __future__ import annotations

from typing import Any

from .campaign_result import as_int, created_campaigns, result_name


def _index_by_id(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for row in rows or []:
        cid = as_int(row.get("id") or row.get("Id"))
        if cid is not None:
            out[cid] = row
    return out


def _index_by_name(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {result_name(row): row for row in rows or [] if result_name(row)}


def verify_live_create_set(*, login: str, results: list[dict[str, Any]],
                           v5_campaigns: list[dict[str, Any]] | None = None,
                           grid_campaigns: list[dict[str, Any]] | None = None,
                           grid_content_counts: dict[int, dict[str, int]] | None = None,
                           uac_details: dict[int, dict[str, Any]] | None = None,
                           prefer_grid: bool = True) -> dict[str, Any]:
    """Compare created result rows with read-only v5/Grid snapshots.

    ``v5_campaigns`` and ``grid_campaigns`` are optional. When a snapshot is not
    supplied, the corresponding checks are reported as skipped, not failed.

    ``prefer_grid=True`` is intentional for this project: Direct API units are
    scarce, while the service can read/create/finalize many entities through the
    cookie/Grid layer. When Grid data is supplied, it is accepted as the primary
    existence check even for non-UAC campaign ids.
    """
    created = created_campaigns(results or [])
    v5_by_id = _index_by_id(v5_campaigns or [])
    grid_by_id = _index_by_id(grid_campaigns or [])
    grid_by_name = _index_by_name(grid_campaigns or [])
    content_counts = grid_content_counts or {}
    uac_detail_rows = uac_details or {}
    issues: list[dict[str, Any]] = []
    repair: list[dict[str, Any]] = []
    checked = {
        "v5": bool(v5_campaigns is not None),
        "grid": bool(grid_campaigns is not None),
        "grid_content": bool(grid_content_counts is not None),
        "uac_details": bool(uac_details is not None),
    }

    for c in created:
        cid = c.get("id")
        nm = c.get("name") or ""
        kind = c.get("kind") or "unknown"
        from .local_result_verifier import verify_local_result
        issues.extend(verify_local_result(c))
        if cid is None:
            issues.append({"severity": "error", "code": "CREATED_WITHOUT_ID", "name": nm})
            repair.append({"kind": "manual_lookup_by_name", "name": nm})
            continue
        actual = None
        if kind == "uac":
            if grid_campaigns is None:
                issues.append({"severity": "warn", "code": "GRID_CHECK_SKIPPED", "name": nm, "id": cid})
            else:
                actual = grid_by_id.get(cid) or grid_by_name.get(nm)
                if not actual:
                    issues.append({"severity": "error", "code": "UAC_NOT_FOUND_IN_GRID", "name": nm, "id": cid})
                    repair.append({"kind": "recreate_or_resume_campaign", "name": nm, "id": cid})
            if uac_details is not None:
                detail = uac_detail_rows.get(int(cid)) or {}
                if not detail:
                    issues.append({"severity": "warn", "code": "UAC_DETAIL_SKIPPED", "name": nm, "id": cid})
                else:
                    from .uac_verifier import verify_uac_detail
                    uac_issues, uac_repair = verify_uac_detail(nm, int(cid), detail)
                    issues.extend(uac_issues)
                    repair.extend(uac_repair)
        else:
            if prefer_grid and grid_campaigns is not None:
                actual = grid_by_id.get(cid) or grid_by_name.get(nm)
                if not actual and v5_campaigns is not None:
                    actual = v5_by_id.get(cid)
                if not actual:
                    issues.append({"severity": "error", "code": "CAMPAIGN_NOT_FOUND_IN_GRID", "name": nm, "id": cid})
                    repair.append({"kind": "recreate_or_resume_campaign", "name": nm, "id": cid, "via": "cookie"})
            elif v5_campaigns is not None:
                actual = v5_by_id.get(cid)
                if not actual:
                    issues.append({"severity": "error", "code": "CAMPAIGN_NOT_FOUND_IN_V5", "name": nm, "id": cid})
                    repair.append({"kind": "recreate_or_resume_campaign", "name": nm, "id": cid})
            elif grid_campaigns is not None:
                actual = grid_by_id.get(cid) or grid_by_name.get(nm)
                if not actual:
                    issues.append({"severity": "error", "code": "CAMPAIGN_NOT_FOUND_IN_GRID", "name": nm, "id": cid})
                    repair.append({"kind": "recreate_or_resume_campaign", "name": nm, "id": cid})
            else:
                issues.append({"severity": "warn", "code": "LIVE_CHECK_SKIPPED", "name": nm, "id": cid})
        if actual:
            from .campaign_state_verifier import verify_campaign_state
            state_issues, state_repair = verify_campaign_state(nm, int(cid), actual)
            issues.extend(state_issues)
            repair.extend(state_repair)
        if kind != "uac" and grid_content_counts is not None:
            counts = content_counts.get(int(cid)) or {}
            from .grid_content_verifier import verify_grid_content
            grid_issues, grid_repair = verify_grid_content(nm, int(cid), counts)
            issues.extend(grid_issues)
            repair.extend(grid_repair)

    errors = sum(1 for x in issues if x.get("severity") == "error")
    warnings = sum(1 for x in issues if x.get("severity") == "warn")
    status = "fail" if errors else ("warn" if warnings else "pass")
    report = {
        "status": status,
        "login": login,
        "checked": checked,
        "prefer_grid": bool(prefer_grid),
        "summary": {
            "created_results": len(created),
            "v5_rows": len(v5_campaigns or []),
            "grid_rows": len(grid_campaigns or []),
            "grid_content_rows": len(grid_content_counts or {}),
            "uac_detail_rows": len(uac_details or {}),
            "issues": len(issues),
            "errors": errors,
            "warnings": warnings,
        },
        "campaigns": [{"id": c.get("id"), "name": c.get("name"), "kind": c.get("kind")} for c in created],
        "issues": issues[:120],
        "repair_candidates": repair[:120],
    }
    try:
        from .repair_planner import build_repair_plan
        report["repair_plan"] = build_repair_plan(report)
    except Exception:  # noqa: BLE001
        report["repair_plan"] = {"status": "error"}
    return report

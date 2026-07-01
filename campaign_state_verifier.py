"""Pure checks for live campaign row state/name consistency."""
from __future__ import annotations

from typing import Any


def _name(row: dict[str, Any] | None) -> str:
    return str((row or {}).get("name") or (row or {}).get("Name") or "").strip()


def verify_campaign_state(expected_name: str, campaign_id: int | None,
                          actual: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(issues, repair_candidates)`` for one live campaign row."""
    if not actual:
        return [], []
    nm = str(expected_name or "")
    cid = campaign_id
    issues: list[dict[str, Any]] = []
    repair: list[dict[str, Any]] = []
    actual_name = _name(actual)
    if not actual_name and nm:
        # Empty live name (including UAC): NAME_MISMATCH below is blind to this
        # because it requires a non-empty actual_name. Rename repair can fix it.
        issues.append({"severity": "error", "code": "CAMPAIGN_NAME_EMPTY", "expected": nm,
                       "actual": "", "id": cid})
        repair.append({"kind": "rename_campaign", "id": cid, "expected": nm, "actual": ""})
    elif actual_name and nm and actual_name != nm:
        issues.append({"severity": "warn", "code": "NAME_MISMATCH", "expected": nm,
                       "actual": actual_name, "id": cid})
        repair.append({"kind": "rename_campaign", "id": cid, "expected": nm, "actual": actual_name})
    state = str(actual.get("State") or actual.get("state") or actual.get("status") or "").upper()
    archived = bool(actual.get("archived") or actual.get("Archived"))
    if archived or state == "ARCHIVED":
        issues.append({"severity": "error", "code": "CAMPAIGN_ARCHIVED", "name": nm, "id": cid})
        repair.append({"kind": "recreate_or_resume_campaign", "name": nm, "id": cid})
    return issues, repair

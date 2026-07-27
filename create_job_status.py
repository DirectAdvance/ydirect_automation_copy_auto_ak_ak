"""Terminal status decisions for create queue jobs."""
from __future__ import annotations

from typing import Any


CREATE_JOB_KINDS = {"set", "slepok"}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def create_failed_error(failed: int, *, prefix: str = "создание завершилось с ошибками") -> str:
    return f"{prefix}: не создано {_as_int(failed)}"


def terminal_status_for_job(
    kind: str | None,
    data: dict[str, Any] | None,
    *,
    cancelled: bool = False,
) -> tuple[str, str | None]:
    """Return terminal status/error for worker result.

    For create-set jobs, partial failures must be red terminal state. Copy/delete
    keep their existing semantics because their UI already presents partial counts.
    """
    if cancelled:
        return "cancelled", None
    if isinstance(data, dict) and data.get("error"):
        return "error", str(data.get("error") or "")[:500]
    failed = _as_int((data or {}).get("failed") if isinstance(data, dict) else 0)
    if (kind or "") in CREATE_JOB_KINDS and failed > 0:
        return "error", create_failed_error(failed)
    return "done", None


def terminal_status_for_parent_failed(failed: int) -> tuple[str, str | None]:
    failed_i = _as_int(failed)
    if failed_i > 0:
        return "error", create_failed_error(failed_i, prefix="докрутка завершилась с ошибками")
    return "done", None


# ---------------------------------------------------------------------------
# Гейт финального статуса: has_issues breakdown
# ---------------------------------------------------------------------------

def _lv_summary_int(data: dict[str, Any], key: str) -> int:
    """Read integer field from live_verification.summary."""
    try:
        lv = data.get("live_verification") or {}
        return int((lv.get("summary") or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _ver_summary_int(data: dict[str, Any], key: str) -> int:
    """Read integer field from verification.summary (static verify_create_set report)."""
    try:
        ver = data.get("verification") or {}
        return int((ver.get("summary") or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _count_positions_with_errors(data: dict[str, Any]) -> int:
    """Count unique campaign positions with at least one error-severity issue."""
    try:
        lv = data.get("live_verification") or {}
        issues = lv.get("issues") or []
        seen: set[object] = set()
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            if issue.get("severity") == "error":
                cid = (issue.get("campaign_id")
                       or issue.get("item_id")
                       or issue.get("id"))
                seen.add(cid if cid is not None else id(issue))
        return len(seen)
    except Exception:  # noqa: BLE001
        return 0


def compute_job_issues_breakdown(
    kind: str | None,
    data: dict[str, Any] | None,
) -> dict[str, int] | None:
    """Compute issues breakdown for a create-set job that finished as 'done'.

    Returns None when the job is fully clean (no significant defects).
    Returns a breakdown dict when live_verification.summary.errors > 0.

    Gate logic (what is significant vs штатное):
    - SIGNIFICANT → live_verification.summary.errors > 0
      (error-severity findings from verifiers: missing groups, wrong config, etc.)
    - NOT significant alone → warnings, gate_skips, errors_log
      (штатные report-only warns; infrastructure connectivity issues)
    - Out of scope here → build.errors[] plural (separate task, create_set_orchestrator ⛔)
    """
    if (kind or "") not in CREATE_JOB_KINDS:
        return None
    d = data or {}
    live_errors = _lv_summary_int(d, "errors")
    ver_errors = _ver_summary_int(d, "errors")
    total_errors = live_errors + ver_errors
    if total_errors == 0:
        return None  # чистый done
    live_warnings = _lv_summary_int(d, "warnings")
    gate_skips = _as_int(d.get("gate_skips"))
    positions_with_errors = _count_positions_with_errors(d)
    return {
        "live_errors": total_errors,  # sum: live_verification + verification
        "live_warnings": live_warnings,
        "gate_skips": gate_skips,
        "positions_with_errors": positions_with_errors,
    }

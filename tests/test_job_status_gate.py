"""Tests for job-status gate: has_issues breakdown and terminal_status_for_job."""
from direct.create_job_status import compute_job_issues_breakdown, terminal_status_for_job


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _lv(errors=0, warnings=0, issues=None):
    return {
        "summary": {"errors": errors, "warnings": warnings},
        "issues": issues or [],
    }


def _data(created=5, failed=0, live_errors=0, live_warnings=0, issues=None, gate_skips=0):
    d = {"created": created, "failed": failed,
         "live_verification": _lv(live_errors, live_warnings, issues)}
    if gate_skips:
        d["gate_skips"] = gate_skips
    return d


# ---------------------------------------------------------------------------
# compute_job_issues_breakdown
# ---------------------------------------------------------------------------

def test_clean_done_no_breakdown():
    """No live errors → clean done, returns None."""
    assert compute_job_issues_breakdown("set", _data(live_errors=0, live_warnings=3)) is None


def test_live_errors_trigger_has_issues():
    """live_verification.summary.errors > 0 → has_issues breakdown."""
    result = compute_job_issues_breakdown("set", _data(live_errors=2, live_warnings=5))
    assert result is not None
    assert result["live_errors"] == 2
    assert result["live_warnings"] == 5


def test_only_warnings_no_has_issues():
    """Warnings alone are штатные — should NOT flag has_issues."""
    assert compute_job_issues_breakdown("set", _data(live_errors=0, live_warnings=9)) is None


def test_gate_skips_in_breakdown_when_errors_present():
    """gate_skips included in breakdown if live_errors > 0."""
    result = compute_job_issues_breakdown("set", _data(live_errors=1, gate_skips=3))
    assert result is not None
    assert result["gate_skips"] == 3


def test_gate_skips_alone_no_has_issues():
    """gate_skips without live_errors → NOT significant."""
    assert compute_job_issues_breakdown("set", _data(live_errors=0, gate_skips=3)) is None


def test_non_create_kind_returns_none():
    """Only set/slepok kinds are gated."""
    d = _data(live_errors=5)
    assert compute_job_issues_breakdown("copy_campaigns", d) is None
    assert compute_job_issues_breakdown(None, d) is None
    assert compute_job_issues_breakdown("delete_drafts", d) is None


def test_slepok_kind_is_gated():
    """slepok is in CREATE_JOB_KINDS — should be gated."""
    result = compute_job_issues_breakdown("slepok", _data(live_errors=1))
    assert result is not None


def test_positions_with_errors_counted():
    """Count unique campaign positions with error-severity issues."""
    issues = [
        {"severity": "error", "campaign_id": "c1"},
        {"severity": "warn", "campaign_id": "c2"},
        {"severity": "error", "campaign_id": "c1"},  # duplicate → same position
        {"severity": "error", "campaign_id": "c3"},
    ]
    result = compute_job_issues_breakdown(
        "set", _data(live_errors=2, live_warnings=1, issues=issues)
    )
    assert result is not None
    assert result["positions_with_errors"] == 2  # c1 and c3


def test_positions_with_errors_zero_when_clean():
    """When no live_errors, positions_with_errors is not computed (returns None)."""
    issues = [{"severity": "error", "campaign_id": "c1"}]
    # live_errors=0 in summary → gate returns None regardless of issues list
    assert compute_job_issues_breakdown("set", _data(live_errors=0, issues=issues)) is None


def test_no_live_verification_is_clean():
    """Missing live_verification key → clean done."""
    assert compute_job_issues_breakdown("set", {"created": 5, "failed": 0}) is None


def test_none_data_is_clean():
    """None data → clean done."""
    assert compute_job_issues_breakdown("set", None) is None


def test_breakdown_includes_all_fields():
    """Breakdown always includes live_errors, live_warnings, gate_skips, positions_with_errors."""
    result = compute_job_issues_breakdown("set", _data(live_errors=3, live_warnings=7, gate_skips=2))
    assert result is not None
    assert set(result.keys()) == {"live_errors", "live_warnings", "gate_skips", "positions_with_errors"}


# ---------------------------------------------------------------------------
# terminal_status_for_job unchanged semantics
# ---------------------------------------------------------------------------

def test_terminal_status_done_no_failed():
    st, err = terminal_status_for_job("set", {"created": 5, "failed": 0})
    assert st == "done"
    assert err is None


def test_terminal_status_error_on_failed():
    st, err = terminal_status_for_job("set", {"created": 3, "failed": 2})
    assert st == "error"
    assert err is not None


def test_terminal_status_unaffected_by_live_errors():
    """terminal_status_for_job must NOT know about has_issues — gate is separate."""
    d = {"created": 5, "failed": 0, "live_verification": {"summary": {"errors": 14}}}
    st, err = terminal_status_for_job("set", d)
    assert st == "done"  # status still "done" — has_issues is in result, not status


def test_terminal_status_cancelled():
    st, err = terminal_status_for_job("set", {"created": 0, "failed": 0}, cancelled=True)
    assert st == "cancelled"
    assert err is None

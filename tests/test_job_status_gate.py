"""Tests for job-status gate: has_issues breakdown and terminal_status_for_job."""
from direct.create_job_status import (
    annotate_job_issues,
    compute_job_issues_breakdown,
    has_verification_data,
    terminal_status_for_job,
)


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
    assert result["lv_errors"] == 2        # live_verification.summary.errors
    assert result["ver_errors"] == 0       # verification.summary.errors (none here)
    assert result["live_errors"] == 2      # JS backward-compat: sum of lv + ver
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
    """Breakdown always includes lv_errors, ver_errors, live_errors (compat), live_warnings, gate_skips, positions_with_errors."""
    result = compute_job_issues_breakdown("set", _data(live_errors=3, live_warnings=7, gate_skips=2))
    assert result is not None
    assert set(result.keys()) == {
        "lv_errors", "ver_errors",        # source breakdown (new)
        "live_errors",                     # JS backward-compat alias (= lv_errors + ver_errors)
        "live_warnings", "gate_skips", "positions_with_errors",
    }


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


# ---------------------------------------------------------------------------
# Integration: verify_create_set → job data → compute_job_issues_breakdown
# ---------------------------------------------------------------------------

def test_cpa_mismatch_reaches_has_issues_gate():
    """Integration: CPA_COUNT_PLAN_MISMATCH in verify_create_set propagates to has_issues.

    Previously, the gate only read live_verification.summary.errors and missed
    all findings from the static verify_create_set (verification.summary.errors).
    This test ensures the full chain works: detector fires → stored in job data →
    has_issues breakdown is non-None.
    """
    from direct.verifier import verify_create_set

    # Plan includes a CPA item, but results have none (silently skipped by orchestrator)
    items = [{"name": "tp5_cpa_site — Модели"}, {"name": "tp5_cpc_site — Модели"}]
    results = [{"name": "tp5_cpc_site — Модели", "ok": True, "id": 111}]

    verification = verify_create_set(login="test-integ", items=items, results=results)
    assert verification["summary"]["errors"] > 0, "sanity: CPA_COUNT_PLAN_MISMATCH must fire"
    cpa_codes = [i["code"] for i in verification["issues"]]
    assert "CPA_COUNT_PLAN_MISMATCH" in cpa_codes, "sanity: CPA_COUNT_PLAN_MISMATCH must be in issues"

    # Simulate what queue_server stores as job result data (done, failed=0, no live_verification)
    job_data = {
        "created": 1,
        "failed": 0,
        "verification": verification,
        # live_verification intentionally absent: test that verification alone triggers the gate
    }

    breakdown = compute_job_issues_breakdown("set", job_data)
    assert breakdown is not None, (
        "has_issues must fire when verification.summary.errors > 0 "
        "(regression: was None — CPA_COUNT_PLAN_MISMATCH was invisible to gate)"
    )
    assert breakdown["ver_errors"] > 0    # CPA_COUNT_PLAN_MISMATCH comes from static verifier
    assert breakdown["lv_errors"] == 0    # no live_verification in this data
    assert breakdown["live_errors"] > 0   # JS compat: sum = ver_errors + lv_errors


def test_verification_errors_sum_with_live_errors():
    """verification.summary.errors + live_verification.summary.errors sum into live_errors."""
    from direct.verifier import verify_create_set

    items = [{"name": "tp5_cpa_site — Модели"}]
    results = []
    verification = verify_create_set(login="test-sum", items=items, results=results)
    ver_errors = verification["summary"]["errors"]
    assert ver_errors > 0, "sanity"

    job_data = {
        "created": 0, "failed": 0,
        "verification": verification,
        "live_verification": {"summary": {"errors": 2, "warnings": 0}, "issues": []},
    }
    breakdown = compute_job_issues_breakdown("set", job_data)
    assert breakdown is not None
    assert breakdown["ver_errors"] == ver_errors     # from static verifier
    assert breakdown["lv_errors"] == 2               # from live_verification
    assert breakdown["live_errors"] == ver_errors + 2  # JS compat: sum


def test_verification_only_no_live_verification_clean():
    """verification.summary.errors=0 and no live_verification → None (clean done)."""
    # Empty items/results → no verification errors
    from direct.verifier import verify_create_set
    verification = verify_create_set(login="x", items=[], results=[])
    assert verification["summary"]["errors"] == 0, "sanity: empty set should be clean"
    job_data = {"created": 0, "failed": 0, "verification": verification}
    assert compute_job_issues_breakdown("set", job_data) is None


# ---------------------------------------------------------------------------
# has_verification_data: «данных нет» ≠ «ноль ошибок»
# ---------------------------------------------------------------------------

def test_verification_data_present_when_summary_exists():
    assert has_verification_data(_data(live_errors=0)) is True
    assert has_verification_data({"verification": {"summary": {"errors": 0}}}) is True


def test_verification_data_absent_without_summary():
    """Деградированный/упавший постпроцесс кладёт блок без summary — это «данных нет»."""
    assert has_verification_data(None) is False
    assert has_verification_data({"created": 5, "failed": 0}) is False
    assert has_verification_data({"error": "worker crash"}) is False
    assert has_verification_data({
        "verification": {"status": "timeboxed", "error": "postprocess timebox 601s"},
        "live_verification": {"status": "timeboxed", "prefer_grid": True},
    }) is False


# ---------------------------------------------------------------------------
# annotate_job_issues: гейт работает на ЛЮБОМ терминальном статусе, не только done
# ---------------------------------------------------------------------------

def _error_job_data_like_69a140093e78():
    """Слепок реального прогона 69a140093e78: status=error (created=25, failed=1),
    lv_errors=23, ver_errors=1, ver_warnings=22 — раньше has_issues не писался вовсе."""
    return {
        "created": 25,
        "failed": 1,
        "live_verification": {"summary": {"errors": 23, "warnings": 0},
                              "issues": [{"severity": "error", "campaign_id": f"c{i}"}
                                         for i in range(23)]},
        "verification": {"summary": {"errors": 1, "warnings": 22}, "issues": []},
    }


def test_error_job_gets_has_issues_breakdown():
    """error-джоба с непустой верификацией → breakdown посчитан, числа = источнику."""
    data = _error_job_data_like_69a140093e78()
    st, err = terminal_status_for_job("set", data)
    assert st == "error"                      # sanity: failed>0 → error, не done
    annotate_job_issues("set", data)
    hi = data.get("has_issues")
    assert hi is not None, "regression JOB_STATUS_ERROR_SKIPS_HAS_ISSUES: разбивки нет на error-джобе"
    assert hi["lv_errors"] == 23              # live_verification.summary.errors
    assert hi["ver_errors"] == 1              # verification.summary.errors
    assert hi["live_errors"] == 24            # JS-compat: сумма
    assert hi["positions_with_errors"] == 23
    assert "has_issues_unknown" not in data   # данные есть → пометки «нет данных» быть не должно


def test_cancelled_job_with_verification_gets_breakdown():
    """Гейт не зависит от статуса: cancelled с верификацией тоже получает разбивку."""
    data = _data(created=3, failed=0, live_errors=4)
    assert terminal_status_for_job("set", data, cancelled=True)[0] == "cancelled"
    annotate_job_issues("set", data)
    assert (data.get("has_issues") or {}).get("lv_errors") == 4


def test_done_job_behaviour_unchanged_clean():
    """done без ошибок: ключей не появляется — старое поведение не меняется."""
    data = _data(live_errors=0, live_warnings=3)
    annotate_job_issues("set", data)
    assert "has_issues" not in data
    assert "has_issues_unknown" not in data


def test_done_job_behaviour_unchanged_with_errors():
    """done с ошибками: тот же has_issues, что и до правки."""
    data = _data(live_errors=2, live_warnings=5)
    annotate_job_issues("set", data)
    assert data["has_issues"] == compute_job_issues_breakdown("set", _data(live_errors=2, live_warnings=5))


def test_job_without_verification_marked_unknown_not_zeros():
    """Верификация не запускалась → явная пометка «нет данных», НЕ нули в has_issues."""
    data = {"created": 0, "failed": 1, "error": "worker crash"}
    annotate_job_issues("set", data)
    assert "has_issues" not in data, "нулевую разбивку писать нельзя: данных не было"
    assert data.get("has_issues_unknown") is True


def test_degraded_postprocess_marked_unknown():
    """Timeboxed постпроцесс (блоки без summary) → «нет данных», а не «ноль ошибок»."""
    data = {
        "created": 26, "failed": 0,
        "verification": {"status": "timeboxed", "error": "postprocess timebox 601s"},
        "live_verification": {"status": "timeboxed", "prefer_grid": True},
    }
    annotate_job_issues("set", data)
    assert "has_issues" not in data
    assert data.get("has_issues_unknown") is True


def test_annotate_ignores_non_create_kinds():
    """copy/delete джобы гейтом не размечаются вовсе."""
    for kind in ("copy_campaigns", "delete_drafts", None):
        data = {"created": 1, "failed": 1}
        annotate_job_issues(kind, data)
        assert data == {"created": 1, "failed": 1}


def test_annotate_tolerates_non_dict_result():
    annotate_job_issues("set", None)  # не должно падать


# ---------------------------------------------------------------------------
# Anti-regression: вызов гейта не должен снова уехать под `if _st == "done"`
# ---------------------------------------------------------------------------

def test_queue_server_calls_gate_outside_done_branch():
    """queue_server обязан звать annotate_job_issues вне ветки `if _st == "done"`.

    Именно вложенность в done-ветку была причиной JOB_STATUS_ERROR_SKIPS_HAS_ISSUES.
    """
    import ast
    import pathlib

    src_path = pathlib.Path(__file__).resolve().parents[1] / "queue_server.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))

    def _calls(node):
        return [n for n in ast.walk(node)
                if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "annotate_job_issues"]

    assert _calls(tree), "annotate_job_issues не вызывается в queue_server.py"
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        is_done_test = (
            isinstance(test, ast.Compare)
            and getattr(test.left, "id", "") == "_st"
            and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
            and getattr(test.comparators[0], "value", None) == "done"
        )
        if not is_done_test:
            continue
        nested = [c for stmt in node.body for c in _calls(stmt)]
        assert not nested, "гейт has_issues снова вложен в `if _st == \"done\"` — error-джобы онемеют"

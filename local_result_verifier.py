"""Pure checks for local create_set result/build metadata."""
from __future__ import annotations

import re
from typing import Any


_BAD_NAME_RE = re.compile(r"\b(?:None|null|undefined)\b", re.I)


def verify_local_result(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Return issues visible in create result metadata before live reads."""
    result = (row or {}).get("result") or {}
    nm = str((row or {}).get("name") or "")
    issues: list[dict[str, Any]] = []
    build = result.get("build") or result.get("tp1_build") or result.get("tp5_build") or {}
    if isinstance(build, dict):
        if build.get("error") or build.get("skipped"):
            issues.append({"severity": "error", "code": "BUILD_ERROR", "name": nm,
                           "message": str(build.get("error") or build.get("skipped"))[:240]})
        if "groups" in build and int(build.get("groups") or 0) <= 0:
            issues.append({"severity": "error", "code": "NO_ADGROUPS_REPORTED", "name": nm})
        if "ads" in build and int(build.get("ads") or 0) <= 0:
            issues.append({"severity": "error", "code": "NO_ADS_REPORTED", "name": nm})
        if int(build.get("shopping_ads") or 0) < 0:
            issues.append({"severity": "warn", "code": "SHOPPING_COUNT_BAD", "name": nm})
    if result.get("search_finalized") is False:
        issues.append({"severity": "warn", "code": "SEARCH_NOT_FINALIZED", "name": nm})
    fin = result.get("shopping_finalized")
    if isinstance(fin, dict) and fin.get("error"):
        issues.append({"severity": "warn", "code": "SHOPPING_NOT_FINALIZED", "name": nm,
                       "message": str(fin.get("error"))[:240]})
    if result.get("grid_warn"):
        issues.append({"severity": "warn", "code": "GRID_FINALIZE_WARN", "name": nm,
                       "message": str(result.get("grid_warn"))[:240]})
    if _BAD_NAME_RE.search(nm):
        issues.append({"severity": "error", "code": "NAME_HAS_NULL_TOKEN", "name": nm})
    return issues

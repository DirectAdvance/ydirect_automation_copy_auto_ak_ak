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
        _shop_only = (int(build.get("shopping_ads") or 0) > 0
                      or int(build.get("listing_ads") or 0) > 0)
        if "ads" in build and int(build.get("ads") or 0) <= 0 and not _shop_only:
            issues.append({"severity": "error", "code": "NO_ADS_REPORTED", "name": nm})
        if int(build.get("shopping_ads") or 0) < 0:
            issues.append({"severity": "warn", "code": "SHOPPING_COUNT_BAD", "name": nm})
    # RSYA_NOT_FINALIZED — Grid-финализация tp1 (РСЯ) не прошла. Severity error, а не warn:
    # именно _finalize_rsya ставит НЕДЕЛЬНЫЙ БЮДЖЕТ (strategyData.sum) и кампанийные ассеты
    # (уточнения/набор быстрых ссылок/промо) — без неё кампания уезжает с budget=0.
    # Живой инцидент 2026-07-19 (job b0d25ad114c5, кампания 712885317): finalize упал на
    # транзиентном 500, ошибка осела в result["finalize_warn"], позиция отрапортовала ok=true,
    # failed=0, errors_log=NULL → дефект был виден ТОЛЬКО косвенно (WEEKLY_BUDGET_MISSING_LIVE,
    # и то лишь если live-чтение спецификации доехало). Теперь сигнал прямой и не зависит от live.
    # Report-only (без repair-кандидата): кампания СОЗДАНА, ей не хватает докрутки — это случай
    # для добивки, а не для удаления/пересоздания; ремонтёра вслепую не выдумываем.
    _rsya_fin = result.get("rsya_finalized")
    _rsya_failed = (_rsya_fin is False) or (isinstance(_rsya_fin, dict) and _rsya_fin.get("error"))
    if _rsya_failed:
        _msg = (result.get("finalize_warn")
                or (_rsya_fin.get("error") if isinstance(_rsya_fin, dict) else None)
                or "Grid-финализация РСЯ не прошла")
        issues.append({"severity": "error", "code": "RSYA_NOT_FINALIZED", "name": nm,
                       "id": (row or {}).get("id") or result.get("campaign_id"),
                       "message": str(_msg)[:240]})
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

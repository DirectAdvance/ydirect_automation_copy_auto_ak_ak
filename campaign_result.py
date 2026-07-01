"""Normalization helpers for create_set result rows."""
from __future__ import annotations

import re
from typing import Any


_TP_PREFIX_RE = re.compile(r"^tp(?P<tp>\d+)_(?P<pay>cpc|cpa)_(?P<surface>site|kviz)", re.I)


def as_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def result_name(row: dict[str, Any] | None) -> str:
    return str((row or {}).get("name") or (row or {}).get("Name") or "").strip()


def result_id(row: dict[str, Any]) -> int | None:
    return as_int(row.get("campaign_id") or row.get("id") or row.get("Id"))


def campaign_kind(name: str) -> str:
    m = _TP_PREFIX_RE.search(name or "")
    if not m:
        return "unknown"
    tp = int(m.group("tp"))
    if tp in (6, 7):
        return "uac"
    return "v5"


def created_campaigns(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return normalized created campaigns from create_set result rows."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[int | None, str]] = set()

    def walk(row: dict[str, Any], parent: dict[str, Any] | None = None) -> None:
        if not isinstance(row, dict):
            return
        for child in row.get("campaigns") or []:
            if isinstance(child, dict):
                walk(child, row)
        if not row.get("ok") or row.get("skipped") or row.get("defer"):
            return
        cid = result_id(row)
        if cid is None and row.get("campaigns"):
            return
        nm = result_name(row) or result_name(parent)
        key = (cid, nm)
        if key in seen:
            return
        seen.add(key)
        out.append({
            "id": cid,
            "name": nm,
            "kind": campaign_kind(nm),
            "result": row,
        })

    for row in results or []:
        walk(row)
    return out

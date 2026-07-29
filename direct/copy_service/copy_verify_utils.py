"""Shared helpers for copy_verify modules."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

_OK = "ok"
_MISMATCH = "mismatch"
_MISSING = "missing"
_UNREADABLE = "unreadable"
_EXCLUDED = "excluded_intentional"


def _nolog(_m: str) -> None:
    return None


def _rj(path: Any) -> list:
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
        return []
    except Exception:
        return []


def _rj_dict(path: Any) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_DOMAIN_RE = re.compile(r"https?://[^/?#&\s]+", re.IGNORECASE)


def _strip_domain(template: Optional[str]) -> str:
    if not template:
        return ""
    return _DOMAIN_RE.sub("{DOMAIN}", template.strip())


def _feed_filter_signature(raw: Any) -> list[str]:
    """Стабильная сигнатура FeedFilterConditions/feedFilter для source↔target diff.

    Direct API отдаёт official shape как ``{"Items": [{Operand, Operator, Arguments}]}``,
    Grid отдаёт ``{"conditions": [{field, operator, stringValue}]}``. Для проверки 1в1
    приводим обе формы к одному отсортированному списку строк.
    """
    if not raw:
        return []
    items: Any = raw
    if isinstance(raw, dict):
        if "Items" in raw:
            items = raw.get("Items") or []
        elif "conditions" in raw:
            items = raw.get("conditions") or []
        else:
            items = [raw]
    if not isinstance(items, list):
        return []

    out: list[str] = []
    for cond in items:
        if not isinstance(cond, dict):
            continue
        field = str(cond.get("Operand") or cond.get("operand") or
                    cond.get("Field") or cond.get("field") or "").strip()
        operator = str(cond.get("Operator") or cond.get("operator") or "").strip()
        values = cond.get("Arguments")
        if values is None:
            values = cond.get("arguments")
        if values is None and "stringValue" in cond:
            sv = cond.get("stringValue")
            try:
                parsed = json.loads(sv) if isinstance(sv, str) else sv
            except Exception:
                parsed = sv
            values = parsed
        if values is None:
            values = cond.get("value")
        if isinstance(values, (list, tuple, set)):
            vals = sorted(str(v).strip() for v in values if str(v).strip())
        elif values is None:
            vals = []
        else:
            vals = [str(values).strip()] if str(values).strip() else []
        if not field and not operator and not vals:
            continue
        out.append(f"{field}|{operator}|{json.dumps(vals, ensure_ascii=False, sort_keys=True)}")
    return sorted(out)


def _audience_retarg_signature(row: dict) -> str:
    """Стабильная сигнатура одной audience/retargeting-привязки Grid.

    Id условия в разных аккаунтах может отличаться, поэтому в нормальном случае сравниваем
    имя/тип/правила. Fallback на id нужен только если Grid не отдал тело условия.
    """
    cond = row.get("retargetingCondition") or {}
    rules = []
    for rule in cond.get("conditionRules") or []:
        if not isinstance(rule, dict):
            continue
        goals = []
        for goal in rule.get("goals") or []:
            if isinstance(goal, dict):
                goals.append({
                    "id": str(goal.get("id") or ""),
                    "type": str(goal.get("type") or ""),
                })
        rules.append({
            "type": str(rule.get("type") or ""),
            "interestType": str(rule.get("interestType") or ""),
            "sectionId": str(rule.get("sectionId") or ""),
            "goals": sorted(goals, key=lambda x: (x["id"], x["type"])),
        })
    payload = {
        "name": str(cond.get("name") or ""),
        "type": str(cond.get("type") or ""),
        "rules": sorted(rules, key=lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True)),
    }
    if not payload["name"] and not payload["type"] and not payload["rules"]:
        payload["fallback_id"] = str(row.get("retargetingConditionId") or "")
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _read_audience_signatures_grid(grid: Any,
                                   campaign_ids: list[int],
                                   log: Callable[[str], None] | None = None
                                   ) -> tuple[dict[int, dict[str, list[str]]] | None, bool]:
    """Прочитать audience/retargeting-привязки групп через Grid.

    Возвращает ``({campaign_id: {adgroup_id: [signature, ...]}}, True)``.
    При сбое чтения возвращает ``(None, False)``. `GdGridOfferRetargeting` намеренно
    игнорируется: это оферный ретаргетинг фида, не аудитории пользователя.
    """
    _log = log or _nolog
    ids = [int(c) for c in (campaign_ids or []) if str(c).strip().lstrip("-").isdigit()]
    ids = [c for c in dict.fromkeys(ids) if c > 0]
    if not ids:
        return {}, True
    login = str(getattr(grid, "login", "") or "")
    if not login or not hasattr(grid, "_post"):
        return None, False
    q = (
        "query CopyAudiences($login:String!,$inp:GdRetargetingsContainerInput!){"
        "client(searchBy:{login:$login}){retargetings(input:$inp){totalCount rowset{"
        "campaignId adGroupId isSuspended __typename "
        "... on GdRetargeting{retargetingConditionId retargetingCondition{"
        "retargetingConditionId name type conditionRules{type interestType sectionId goals{id type}}"
        "}}}}}}"
    )
    out: dict[int, dict[str, list[str]]] = {}
    try:
        if hasattr(grid, "_bootstrap_csrf"):
            grid._bootstrap_csrf()
        limit = 5000
        for i in range(0, len(ids), 50):
            chunk = [str(c) for c in ids[i:i + 50]]
            offset = 0
            while True:
                inp = {
                    "filter": {"campaignIdIn": chunk},
                    "statRequirements": {"preset": "TODAY"},
                    "limitOffset": {"limit": limit, "offset": offset},
                    "orderBy": [],
                }
                raw = grid._post("CopyAudiences", q, {"login": login, "inp": inp})
                data = raw.json() if hasattr(raw, "json") else raw
                if data.get("errors"):
                    _log(f"copy_verify: audience grid errors: {str(data.get('errors'))[:180]}")
                    return None, False
                rows = ((((data.get("data") or {}).get("client") or {})
                         .get("retargetings") or {}).get("rowset") or [])
                for row in rows:
                    if str(row.get("__typename") or "") != "GdRetargeting":
                        continue
                    try:
                        cid = int(row.get("campaignId") or 0)
                    except (TypeError, ValueError):
                        continue
                    gid = str(row.get("adGroupId") or "")
                    if cid <= 0 or not gid:
                        continue
                    out.setdefault(cid, {}).setdefault(gid, []).append(
                        _audience_retarg_signature(row)
                    )
                if len(rows) < limit:
                    break
                offset += limit
    except Exception as exc:  # noqa: BLE001
        _log(f"copy_verify: audience grid error: {str(exc)[:180]}")
        return None, False
    for by_group in out.values():
        for gid, sigs in list(by_group.items()):
            by_group[gid] = sorted(sigs)
    return out, True

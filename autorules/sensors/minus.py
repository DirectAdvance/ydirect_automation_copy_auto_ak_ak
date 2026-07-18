"""Сенсор: кампании без минус-фраз / без минусовки на уровне кампании.

Проверяет NegativeKeywords на уровне кампаний через Direct API v5.
Кампании без единой минус-фразы на уровне кампании считаются проблемными.
Текстовые и поисковые кампании — приоритет проверки.
"""
from __future__ import annotations

# Типы кампаний, для которых минус-фразы критичны
_SEARCH_TYPES = {
    "TEXT_CAMPAIGN",
    "SMART_CAMPAIGN",
    "DYNAMIC_TEXT_CAMPAIGN",
    "UNIFIED_CAMPAIGN",
}
# Типы кампаний, где минус-фразы не применяются (РСЯ-only, фиды)
_SKIP_TYPES = {
    "DISPLAY_CAMPAIGN",
    "MASTER_CAMPAIGN",
    "ECOMMERCE_CAMPAIGN",
}
_SKIP_STATES = {"ARCHIVED", "ENDED"}
_SKIP_STATUSES = {"DRAFT", "ARCHIVED"}


def run(login: str, ctx: dict) -> dict:
    """Проверяет наличие минус-фраз на уровне кампаний.

    Returns:
        {"found": int, "details": list, "error": str|None}
    """
    from ...yandex_gateway import v5_get, v5_err  # direct.yandex_gateway

    token = ctx.get("token")
    if not token:
        return {"found": 0, "details": [], "error": "нет токена"}

    resp = v5_get(
        "campaigns", token, login,
        ["Id", "Name", "Type", "Status", "State", "NegativeKeywords"],
        criteria={},
    )
    if "error" in resp:
        return {"found": 0, "details": [], "error": v5_err(resp)}

    campaigns = (resp.get("result") or {}).get("Campaigns") or []
    problems = []

    for c in campaigns:
        status = (c.get("Status") or "").upper()
        state = (c.get("State") or "").upper()
        ctype = (c.get("Type") or "").upper()

        if status in _SKIP_STATUSES or state in _SKIP_STATES:
            continue
        if ctype in _SKIP_TYPES:
            continue

        neg_kw = c.get("NegativeKeywords") or []
        if not neg_kw:
            problems.append({
                "campaign_id": c.get("Id"),
                "campaign_name": (c.get("Name") or "")[:80],
                "type": ctype,
                "status": status,
                "state": state,
                "note": "нет минус-фраз на уровне кампании",
            })

    return {"found": len(problems), "details": problems}

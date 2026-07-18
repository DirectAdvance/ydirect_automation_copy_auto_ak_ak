"""Сенсор: остановленные / отклонённые / на модерации кампании.

Проверяет State/Status/StatusClarification кампаний через Direct API v5.
Срабатывает при наличии кампаний с проблемными статусами.
"""
from __future__ import annotations

# Статусы, которые считаем проблемными
_BAD_STATUSES = {"REJECTED", "MODERATION", "WAIT_MODERATING"}
_BAD_STATES = {"OFF", "SUSPENDED"}

# Не интересуют архивные/DRAFT кампании (они намеренно не активны)
_SKIP_STATUSES = {"DRAFT", "ARCHIVED"}
_SKIP_STATES = {"ARCHIVED", "ENDED"}


def run(login: str, ctx: dict) -> dict:
    """Проверяет статусы кампаний.

    Returns:
        {"found": int, "details": list, "error": str|None}
    """
    from ...yandex_gateway import v5_get, v5_err  # direct.yandex_gateway

    token = ctx.get("token")
    if not token:
        return {"found": 0, "details": [], "error": "нет токена"}

    resp = v5_get(
        "campaigns", token, login,
        ["Id", "Name", "Status", "State", "StatusClarification"],
        criteria={},
    )
    if "error" in resp:
        return {"found": 0, "details": [], "error": v5_err(resp)}

    campaigns = (resp.get("result") or {}).get("Campaigns") or []
    problems = []

    for c in campaigns:
        status = (c.get("Status") or "").upper()
        state = (c.get("State") or "").upper()

        # Пропускаем намеренно неактивные
        if status in _SKIP_STATUSES or state in _SKIP_STATES:
            continue

        issues = []
        if status in _BAD_STATUSES:
            issues.append(status)
        if state in _BAD_STATES:
            issues.append(f"state:{state}")

        if issues:
            clarif = (c.get("StatusClarification") or "").strip()
            problems.append({
                "campaign_id": c.get("Id"),
                "campaign_name": c.get("Name") or "",
                "status": status,
                "state": state,
                "issues": issues,
                "clarification": clarif[:120] if clarif else "",
            })

    return {"found": len(problems), "details": problems}

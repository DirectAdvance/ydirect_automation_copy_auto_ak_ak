"""Сенсор: кампании без цели Метрики или без счётчика.

1. Если в public.metrika_goals нет настройки для аккаунта — сигнализируем.
2. Для текстовых кампаний проверяем CounterIds через TextCampaignFieldNames
   (тип-специфичный запрос v5 с extra-параметром).
"""
from __future__ import annotations

_SKIP_STATES = {"ARCHIVED", "ENDED"}
_SKIP_STATUSES = {"DRAFT", "ARCHIVED"}
# Типы, для которых счётчик критичен
_CHECK_TYPES = {"TEXT_CAMPAIGN", "SMART_CAMPAIGN", "DYNAMIC_TEXT_CAMPAIGN",
                "UNIFIED_CAMPAIGN", "CPM_BANNER_CAMPAIGN"}


def run(login: str, ctx: dict) -> dict:
    """Проверяет наличие счётчика Метрики в аккаунте и на кампаниях.

    Returns:
        {"found": int, "details": list, "error": str|None}
    """
    import requests as rqs
    from ...yandex_gateway import _headers, V5_URL  # direct.yandex_gateway

    token = ctx.get("token")
    if not token:
        return {"found": 0, "details": [], "error": "нет токена"}

    # 1. Ожидаемые счётчики из БД
    metrika_goals_for = ctx.get("metrika_goals_for")
    expected = None
    if metrika_goals_for:
        try:
            expected = metrika_goals_for(login)
        except Exception:  # noqa: BLE001
            expected = None

    expected_counters: set[int] = set((expected or {}).get("counters") or [])

    # Если в БД вообще нет настройки — это само по себе проблема
    if not expected_counters:
        return {
            "found": 1,
            "details": [{
                "login": login,
                "note": "Нет счётчика Метрики в public.metrika_goals (настройка отсутствует)",
                "expected_counters": [],
            }],
        }

    # 2. Проверяем кампании на наличие счётчика через тип-специфичный запрос
    problems = _check_campaign_counters(token, login, expected_counters, rqs, _headers, V5_URL)
    return {"found": len(problems), "details": problems}


def _check_campaign_counters(token, login, expected_counters, rqs, _headers, V5_URL):
    """Запрос кампаний с TextCampaignFieldNames для получения CounterIds."""
    params = {
        "FieldNames": ["Id", "Name", "Type", "Status", "State"],
        "TextCampaignFieldNames": ["CounterIds", "Settings"],
        "SmartCampaignFieldNames": ["CounterIds"],
        "DynamicTextCampaignFieldNames": ["CounterIds"],
        "SelectionCriteria": {},
    }
    try:
        resp = rqs.post(
            V5_URL + "campaigns",
            headers=_headers(token, login),
            json={"method": "get", "params": params},
            timeout=30,
        )
        j = resp.json()
    except Exception as exc:  # noqa: BLE001
        return [{"login": login, "note": f"API ошибка: {str(exc)[:80]}",
                 "expected_counters": sorted(expected_counters)}]

    if "error" in j:
        err = j["error"]
        err_str = (err.get("error_string") or str(err))[:120]
        return [{"login": login, "note": f"API: {err_str}",
                 "expected_counters": sorted(expected_counters)}]

    campaigns = (j.get("result") or {}).get("Campaigns") or []
    problems = []

    for c in campaigns:
        status = (c.get("Status") or "").upper()
        state = (c.get("State") or "").upper()
        ctype = (c.get("Type") or "").upper()

        if status in _SKIP_STATUSES or state in _SKIP_STATES:
            continue
        if ctype not in _CHECK_TYPES:
            continue

        # Извлекаем счётчики из тип-специфичного блока
        camp_counters = _extract_counters_from_campaign(c)

        if not camp_counters:
            problems.append({
                "campaign_id": c.get("Id"),
                "campaign_name": (c.get("Name") or "")[:80],
                "type": ctype,
                "counters": [],
                "expected_counters": sorted(expected_counters),
                "note": "нет счётчика в кампании",
            })
        elif expected_counters and not camp_counters.intersection(expected_counters):
            problems.append({
                "campaign_id": c.get("Id"),
                "campaign_name": (c.get("Name") or "")[:80],
                "type": ctype,
                "counters": sorted(camp_counters),
                "expected_counters": sorted(expected_counters),
                "note": "счётчик кампании не совпадает с metrika_goals",
            })

    return problems


def _extract_counters_from_campaign(c: dict) -> set[int]:
    counters: set[int] = set()
    for field in ("TextCampaign", "SmartCampaign", "DynamicTextCampaign",
                  "CpmBannerCampaign"):
        block = c.get(field) or {}
        ids = block.get("CounterIds") or []
        for cid in (ids if isinstance(ids, list) else [ids]):
            try:
                counters.add(int(cid))
            except (TypeError, ValueError):
                pass
    return counters

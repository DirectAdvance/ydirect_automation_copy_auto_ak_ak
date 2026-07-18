"""Сенсор: низкий баланс аккаунта.

Срабатывает, если баланс < среднего дневного расхода за последние 7 дней * THRESHOLD_DAYS.
Если данных расхода нет — фолбэк: баланс < FALLBACK_THRESHOLD рублей.

Баланс берётся через Live v4 API (AccountManagement.Get) — проверенный путь из account_service.py.
"""
from __future__ import annotations

THRESHOLD_DAYS = 2       # баланс должен покрывать не менее 2 дней расхода
FALLBACK_THRESHOLD = 500.0   # рублей — если нет данных расхода из отчётов


def run(login: str, ctx: dict) -> dict:
    """Проверяет баланс аккаунта через Live v4 AccountManagement.Get.

    Returns:
        {"found": int, "details": list, "error": str|None}
    """
    import requests as rqs

    token = ctx.get("token")
    if not token:
        return {"found": 0, "details": [], "error": "нет токена"}

    # Получаем баланс через Live v4 AccountManagement.Get (проверенный путь)
    live_v4_url = "https://api.direct.yandex.ru/live/v4/json/"
    try:
        body = {
            "method": "AccountManagement",
            "token": token,
            "param": {"Action": "Get", "SelectionCriteria": {"Logins": [login]}},
        }
        resp = rqs.post(live_v4_url, json=body, timeout=20)
        j = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"found": 0, "details": [], "error": str(exc)[:100]}

    accs = (j.get("data") or {}).get("Accounts") or []
    if not accs:
        err = j.get("error_detail") or j.get("error_str") or "аккаунт не найден"
        return {"found": 0, "details": [], "error": str(err)[:120]}

    amount = round(float(accs[0].get("Amount") or 0), 2)

    # Средний дневной расход из Victory DB за последние 7 дней
    daily_spend = _avg_daily_spend(login, ctx)

    found = 0
    details = []
    if daily_spend is not None and daily_spend > 0:
        threshold = daily_spend * THRESHOLD_DAYS
        if amount < threshold:
            found = 1
            days_left = round(amount / daily_spend, 1) if daily_spend > 0 else None
            details = [{
                "login": login,
                "balance": amount,
                "currency": "RUB",
                "daily_spend_avg": round(daily_spend, 2),
                "threshold": round(threshold, 2),
                "days_left": days_left,
                "note": f"Баланс {amount:.0f} ₽ < {THRESHOLD_DAYS} × дневной расход {daily_spend:.0f} ₽",
            }]
    elif amount < FALLBACK_THRESHOLD:
        found = 1
        details = [{
            "login": login,
            "balance": amount,
            "currency": "RUB",
            "note": f"Баланс {amount:.0f} ₽ ниже порога {FALLBACK_THRESHOLD:.0f} ₽",
        }]

    return {"found": found, "details": details}


def _avg_daily_spend(login: str, ctx: dict) -> float | None:
    """Средний дневной расход за последние 7 дней из Victory DB."""
    victory_conn = ctx.get("victory_conn")
    if not victory_conn:
        return None
    try:
        conn = victory_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(
                        SUM(total_cost)::float / NULLIF(COUNT(DISTINCT "Date"), 0),
                        0)
                    FROM public.yandex_direct_manager_reports
                    WHERE account_login = %s
                      AND "Date" >= to_char(
                            (now() AT TIME ZONE 'Europe/Moscow' - interval '7 days')::date,
                            'YYYY-MM-DD')
                      AND "Date" < to_char(
                            (now() AT TIME ZONE 'Europe/Moscow')::date,
                            'YYYY-MM-DD')
                    """,
                    (login,),
                )
                row = cur.fetchone()
                val = row[0] if row and row[0] is not None else None
                return float(val) if val is not None else None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None

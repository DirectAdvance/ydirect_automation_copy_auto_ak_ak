"""Сенсор: аномалии расхода и CPA — резкие отклонения day vs базовая линия.

Сравнивает вчерашний расход/CPA с 7-дневной средней из Victory DB
(public.yandex_direct_manager_reports). Срабатывает при отклонении > THRESHOLD_PCT.
"""
from __future__ import annotations

SPEND_THRESHOLD_PCT = 0.5    # ±50% от базовой линии → аномалия расхода
CPA_THRESHOLD_PCT = 0.7      # ±70% → аномалия CPA (конверсии нестабильны)
MIN_BASELINE_SPEND = 100.0   # не сигнализируем если базовый расход < 100 ₽/день


def run(login: str, ctx: dict) -> dict:
    """Проверяет аномалии расхода за вчерашний день vs базовую линию.

    Returns:
        {"found": int, "details": list, "error": str|None}
    """
    victory_conn = ctx.get("victory_conn")
    if not victory_conn:
        return {"found": 0, "details": [], "error": "нет подключения к Victory DB"}

    try:
        data = _fetch_data(login, victory_conn)
    except Exception as exc:  # noqa: BLE001
        return {"found": 0, "details": [], "error": str(exc)[:120]}

    if not data:
        return {"found": 0, "details": [], "error": "нет данных расхода в Victory DB"}

    yesterday = data.get("yesterday") or {}
    baseline = data.get("baseline") or {}

    spend_y = float(yesterday.get("spend") or 0)
    spend_b = float(baseline.get("spend") or 0)
    conv_y = float(yesterday.get("conversions") or 0)
    conv_b = float(baseline.get("conversions") or 0)

    if spend_b < MIN_BASELINE_SPEND:
        return {"found": 0, "details": []}  # недостаточно данных

    anomalies = []

    # Расход
    spend_delta = abs(spend_y - spend_b) / spend_b if spend_b > 0 else 0
    if spend_delta >= SPEND_THRESHOLD_PCT:
        direction = "вырос" if spend_y > spend_b else "упал"
        anomalies.append({
            "metric": "расход",
            "yesterday": round(spend_y, 2),
            "baseline_7d": round(spend_b, 2),
            "delta_pct": round(spend_delta * 100, 1),
            "note": f"Расход {direction} на {spend_delta * 100:.0f}% (вчера {spend_y:.0f} vs ср. {spend_b:.0f} ₽)",
        })

    # CPA (если есть конверсии)
    cpa_y = spend_y / conv_y if conv_y > 0 else None
    cpa_b = spend_b / conv_b if conv_b > 0 else None
    if cpa_y is not None and cpa_b is not None and cpa_b > 0:
        cpa_delta = abs(cpa_y - cpa_b) / cpa_b
        if cpa_delta >= CPA_THRESHOLD_PCT:
            direction = "вырос" if cpa_y > cpa_b else "упал"
            anomalies.append({
                "metric": "CPA",
                "yesterday": round(cpa_y, 2),
                "baseline_7d": round(cpa_b, 2),
                "delta_pct": round(cpa_delta * 100, 1),
                "note": f"CPA {direction} на {cpa_delta * 100:.0f}% (вчера {cpa_y:.0f} vs ср. {cpa_b:.0f} ₽)",
            })

    return {"found": len(anomalies), "details": anomalies}


def _fetch_data(login: str, victory_conn) -> dict:
    """Возвращает {yesterday: {spend, conversions}, baseline: {spend, conversions}}."""
    conn = victory_conn()
    try:
        with conn.cursor() as cur:
            # Вчерашние данные
            cur.execute(
                """
                SELECT COALESCE(SUM(total_cost), 0)::float,
                       COALESCE(SUM(all_forms), 0)::float
                FROM public.yandex_direct_manager_reports
                WHERE account_login = %s
                  AND "Date" = to_char(
                        (now() AT TIME ZONE 'Europe/Moscow' - interval '1 day')::date,
                        'YYYY-MM-DD')
                """,
                (login,),
            )
            row = cur.fetchone()
            yesterday = {"spend": float(row[0] or 0), "conversions": float(row[1] or 0)}

            # Базовая линия: среднее за 8–2 дня до сегодня (исключаем вчера)
            cur.execute(
                """
                SELECT COALESCE(SUM(total_cost)::float / NULLIF(COUNT(DISTINCT "Date"), 0), 0),
                       COALESCE(SUM(all_forms)::float  / NULLIF(COUNT(DISTINCT "Date"), 0), 0)
                FROM public.yandex_direct_manager_reports
                WHERE account_login = %s
                  AND "Date" >= to_char(
                        (now() AT TIME ZONE 'Europe/Moscow' - interval '8 days')::date,
                        'YYYY-MM-DD')
                  AND "Date" < to_char(
                        (now() AT TIME ZONE 'Europe/Moscow' - interval '1 day')::date,
                        'YYYY-MM-DD')
                """,
                (login,),
            )
            row = cur.fetchone()
            baseline = {"spend": float(row[0] or 0), "conversions": float(row[1] or 0)}

        return {"yesterday": yesterday, "baseline": baseline}
    finally:
        conn.close()

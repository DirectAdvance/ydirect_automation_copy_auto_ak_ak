"""
Площадки РСЯ: Reports API для просмотра + логирование для исключения.

GET  → Reports API PLACEMENT_PERFORMANCE_REPORT (AdNetworkType=AD_NETWORK).
SET  → В v5 REST API прямого endpoint для исключения площадок на уровне кампании НЕТ
       (Grid-путь требует куки — недоступен в autorules сервисе с OAuth-токенами).
       Площадки записываются в audit_log с action="exclude_site_logged",
       result="manual_required". Пользователь видит список для ручного добавления.
"""
from __future__ import annotations

import time

import requests as rqs

_REPORTS_URL  = "https://api.direct.yandex.com/json/v5/reports"
_MAX_WAIT_SEC = 90


def _report_hdr(token: str, login: str) -> dict:
    return {
        "Authorization":    f"Bearer {token}",
        "Client-Login":     login,
        "Accept-Language":  "ru",
        "returnMoneyInMicros": "false",
        "skipReportHeader":    "true",
        "skipColumnHeader":    "false",
        "skipReportSummary":   "true",
    }


# ── Получение площадок ───────────────────────────────────────────────────────

def get_placements(token: str, login: str, days: int = 7) -> dict:
    """Площадки РСЯ за N дней через Reports API.

    Возвращает:
        {
            "rows": [{"placement": str, "impressions": int, "clicks": int,
                      "cost": float, "conversions": int}],
            "total_rows": int,
            "error": str|None
        }
    """
    from datetime import datetime, timedelta, timezone

    msk       = timezone(timedelta(hours=3))
    today     = datetime.now(msk).date()
    date_from = (today - timedelta(days=int(days))).isoformat()
    date_to   = today.isoformat()

    payload = {
        "params": {
            "SelectionCriteria": {
                "DateFrom": date_from,
                "DateTo":   date_to,
                "Filter": [{
                    "Field":    "AdNetworkType",
                    "Operator": "EQUALS",
                    "Values":   ["AD_NETWORK"],
                }],
            },
            "FieldNames":    ["Placement", "Impressions", "Clicks", "Cost", "Conversions"],
            "ReportName":    f"pl_{login}_{int(time.time())}",
            "ReportType":    "PLACEMENT_PERFORMANCE_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format":        "TSV",
            "IncludeVAT":    "NO",
            "IncludeDiscount": "NO",
        }
    }

    started = time.time()
    while time.time() - started < _MAX_WAIT_SEC:
        try:
            r = rqs.post(_REPORTS_URL, headers=_report_hdr(token, login),
                         json=payload, timeout=60)
        except Exception as exc:  # noqa: BLE001
            return {"rows": [], "total_rows": 0, "error": str(exc)[:150]}

        if r.status_code == 200:
            rows = _parse_tsv(r.text)
            return {"rows": rows[:500], "total_rows": len(rows), "error": None}
        if r.status_code in (201, 202):
            retry = int(r.headers.get("retryIn", "5"))
            time.sleep(min(retry, 10))
            continue
        return {"rows": [], "total_rows": 0,
                "error": f"HTTP {r.status_code}: {r.text[:200]}"}

    return {"rows": [], "total_rows": 0, "error": "Таймаут ожидания отчёта (90 с)"}


def _parse_tsv(text: str) -> list[dict]:
    lines = text.strip().splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    rows   = []
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split("\t")
        row   = dict(zip(header, parts))
        try:
            rows.append({
                "placement":   row.get("Placement", ""),
                "impressions": int(row.get("Impressions", 0) or 0),
                "clicks":      int(row.get("Clicks", 0) or 0),
                "cost":        round(float(row.get("Cost", 0) or 0), 2),
                "conversions": int(row.get("Conversions", 0) or 0),
            })
        except (ValueError, TypeError):
            continue
    rows.sort(key=lambda x: x["cost"], reverse=True)
    return rows


# ── Логирование исключений (не реальная запись в Direct API) ─────────────────

def log_excluded_sites(sites: list[str], login: str) -> dict:
    """Не вызывает Direct API. Возвращает структуру для audit_log.

    В v5 REST API нет endpoint для исключения площадок РСЯ.
    Grid-путь требует куки, недоступны в autorules (OAuth-only).

    Действие: вернуть список площадок для ручного добавления
    через интерфейс Директа (Редактор кампании → Места показа → Исключить сайты).
    """
    sites = [s.strip() for s in (sites or []) if s.strip()]
    if not sites:
        return {"error": "площадки не переданы", "sites": [], "ok": 0}

    return {
        "error":  None,
        "sites":  sites,
        "ok":     len(sites),
        "note": (
            f"Записано в журнал: {len(sites)} площадок для исключения. "
            "Добавьте вручную через Директ → Редактор кампании → "
            "Настройки рекламной сети → Исключить площадки."
        ),
        "action": "manual_required",
    }

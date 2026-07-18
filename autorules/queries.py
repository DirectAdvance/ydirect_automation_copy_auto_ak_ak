"""
Поисковые запросы: Reports API + добавление минус-фраз на кампанию.

GET  → Reports API SEARCH_QUERY_PERFORMANCE_REPORT (polling, до 90 с)
SET  → campaigns.update NegativeKeywords для TEXT_CAMPAIGN.
       ТОЛЬКО за явным confirm из роута. Audit_log — на стороне роута.
"""
from __future__ import annotations

import time

import requests as rqs

_V5           = "https://api.direct.yandex.com/json/v5/"
_REPORTS_URL  = "https://api.direct.yandex.com/json/v5/reports"
_MAX_WAIT_SEC = 90


def _api_hdr(token: str, login: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Client-Login":  login,
        "Accept-Language": "ru",
        "Content-Type":  "application/json; charset=utf-8",
    }


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


# ── Получение отчёта ─────────────────────────────────────────────────────────

def get_search_queries(token: str, login: str, days: int = 7) -> dict:
    """Поисковые запросы за N дней через Reports API.

    Возвращает:
        {
            "rows": [{"query": str, "impressions": int, "clicks": int,
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
                    "Values":   ["SEARCH"],
                }],
            },
            "FieldNames":    ["Query", "Impressions", "Clicks", "Cost", "Conversions"],
            "ReportName":    f"sq_{login}_{int(time.time())}",
            "ReportType":    "SEARCH_QUERY_PERFORMANCE_REPORT",
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
                "query":       row.get("Query", ""),
                "impressions": int(row.get("Impressions", 0) or 0),
                "clicks":      int(row.get("Clicks", 0) or 0),
                "cost":        round(float(row.get("Cost", 0) or 0), 2),
                "conversions": int(row.get("Conversions", 0) or 0),
            })
        except (ValueError, TypeError):
            continue
    rows.sort(key=lambda x: x["cost"], reverse=True)
    return rows


# ── Добавление минус-фраз ────────────────────────────────────────────────────

def add_negative_phrases(token: str, login: str,
                         phrases: list[str],
                         campaign_ids: list[int] | None = None) -> dict:
    """Добавляет минус-фразы в NegativeKeywords TEXT_CAMPAIGN кампаний.

    Если campaign_ids не передан — применяется ко всем TEXT_CAMPAIGN аккаунта.
    ТОЛЬКО TEXT_CAMPAIGN (другие типы вернут ошибку через API и будут пропущены).
    WRITE-действие: audit_log на стороне вызывающего роута.

    Возвращает: {"results": [...], "ok": int, "failed": int, "skipped": int, "error": str|None}
    """
    phrases = [p.strip() for p in (phrases or []) if p.strip()]
    if not phrases:
        return {"results": [], "ok": 0, "failed": 0, "skipped": 0,
                "error": "фразы не переданы"}

    # Получаем TEXT_CAMPAIGN кампании с текущими минус-фразами
    try:
        sel: dict = {"States": ["ON", "OFF", "SUSPENDED"]}
        if campaign_ids:
            sel = {"Ids": [int(x) for x in campaign_ids[:100]]}

        r = rqs.post(
            _V5 + "campaigns",
            headers=_api_hdr(token, login),
            json={"method": "get", "params": {
                "SelectionCriteria": sel,
                "FieldNames": ["Id", "Type"],
                "TextCampaignFieldNames": ["NegativeKeywords"],
                "Page": {"Limit": 500},
            }},
            timeout=30,
        )
        camps = ((r.json().get("result") or {}).get("Campaigns") or [])
    except Exception as exc:  # noqa: BLE001
        return {"results": [], "ok": 0, "failed": 0, "skipped": 0, "error": str(exc)[:150]}

    # Только TEXT_CAMPAIGN
    text_camps = [c for c in camps if c.get("Type") == "TEXT_CAMPAIGN"]
    skipped    = len(camps) - len(text_camps)

    if not text_camps:
        return {"results": [], "ok": 0, "failed": 0, "skipped": skipped,
                "error": "Нет TEXT_CAMPAIGN кампаний (другие типы не поддерживают NegativeKeywords)"}

    # Обновляем — добавляем к существующим без дублей
    update_items = []
    for c in text_camps:
        existing = list((c.get("TextCampaign") or {}).get("NegativeKeywords") or [])
        seen     = set(p.lower() for p in existing)
        new_only = [p for p in phrases if p.lower() not in seen]
        merged   = (existing + new_only)[:1000]  # API limit
        if new_only:
            update_items.append({
                "Id": c["Id"],
                "TextCampaign": {"NegativeKeywords": merged},
            })

    if not update_items:
        return {"results": [], "ok": 0, "failed": 0, "skipped": skipped,
                "error": None, "note": "Все фразы уже есть в выбранных кампаниях"}

    # campaigns.update по 10 за раз
    results = []
    ok      = 0
    failed  = 0

    for i in range(0, len(update_items), 10):
        chunk = update_items[i:i + 10]
        try:
            r = rqs.post(
                _V5 + "campaigns",
                headers=_api_hdr(token, login),
                json={"method": "update", "params": {"Campaigns": chunk}},
                timeout=30,
            )
            j = r.json()
        except Exception as exc:  # noqa: BLE001
            for item in chunk:
                failed += 1
                results.append({"campaign_id": item["Id"], "ok": False,
                                 "error": str(exc)[:100]})
            continue

        api_err = j.get("error")
        if api_err:
            msg = (api_err.get("error_string") or "API error")[:120]
            for item in chunk:
                failed += 1
                results.append({"campaign_id": item["Id"], "ok": False, "error": msg})
            continue

        for upd in ((j.get("result") or {}).get("UpdateResults") or []):
            cid  = upd.get("Id")
            errs = upd.get("Errors") or []
            if errs:
                failed += 1
                results.append({"campaign_id": cid, "ok": False,
                                 "error": errs[0].get("Message", "Ошибка")[:120]})
            else:
                ok += 1
                results.append({"campaign_id": cid, "ok": True})

    return {"results": results, "ok": ok, "failed": failed, "skipped": skipped, "error": None}

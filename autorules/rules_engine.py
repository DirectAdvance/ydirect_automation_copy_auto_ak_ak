"""
Движок правил: dry-run эвалюатор условий ЕСЛИ→ТО.

Условие rule['condition_json']:
  {
    "conditions": [
      {"metric": "spend", "op": ">", "value": 1000, "period": 7}
    ],
    "logic": "AND",
    "period": 7
  }

Действие rule['action_json']:
  {
    "type": "notify" | "pause_campaign" | "resume_campaign" |
            "change_bid" | "change_budget" | "add_negative" |
            "add_excluded_site" | "change_adjustment",
    "params": {"value": ..., "phrase": ..., "site": ...}
  }

Dry-run — ТОЛЬКО ЧТЕНИЕ: метрики из Victory DB, никаких записей в Direct API.
Режим «авто» в UI существует как выбор, но фактическое исполнение действий
заблокировано флагом _AUTO_EXEC_ENABLED=False. Включать только после добро Семёна.
"""
from __future__ import annotations

_AUTO_EXEC_ENABLED = False  # 🔴 ГЕЙТ: авто-исполнение действий в кабинет ВЫКЛЮЧЕНО

SUPPORTED_METRICS: dict[str, str] = {
    "spend":      "Расход ₽",
    "cpa":        "CPA ₽",
    "drr":        "ДРР %",
    "clicks":     "Клики",
    "cr":         "CR %",
    "impressions":"Показы",
    "ctr":        "CTR %",
}

SUPPORTED_OPS = (">", ">=", "<", "<=", "=")

ACTION_LABELS: dict[str, str] = {
    "notify":            "Уведомить",
    "pause_campaign":    "Остановить кампанию",
    "resume_campaign":   "Запустить кампанию",
    "change_bid":        "Изменить ставку",
    "change_budget":     "Изменить дневной бюджет",
    "add_negative":      "Добавить минус-фразу",
    "add_excluded_site": "Добавить минус-площадку",
    "change_adjustment": "Изменить корректировку",
}


# ── Dry-run эвалюатор ────────────────────────────────────────────────────────

def dry_run_rule(rule: dict, login: str, ctx: dict) -> dict:
    """Проверяет условие правила против реальных данных аккаунта.

    Возвращает:
        {
            "matched": bool,
            "preview": {
                "metric_values": {...},
                "period_days": int,
                "condition_results": [...],
                "logic": "AND"|"OR",
                "action": {"type": str, "label": str, "params": dict},
                "would_affect": [...],
                "auto_exec_blocked": bool
            },
            "error": str|None
        }
    """
    condition = rule.get("condition_json") or {}
    action    = rule.get("action_json") or {}
    mode      = rule.get("mode", "manual")

    period = int(condition.get("period", 7))
    logic  = condition.get("logic", "AND")
    conditions = condition.get("conditions") or []

    # Реальные метрики из Victory DB
    metric_values, err = _fetch_account_metrics(login, period, ctx)
    if err:
        return {"matched": False, "preview": {}, "error": err}

    # Проверяем каждое условие
    cond_results = []
    for cond in conditions:
        metric   = cond.get("metric", "")
        op       = cond.get("op", ">")
        try:
            threshold = float(cond.get("value") or 0)
        except (TypeError, ValueError):
            threshold = 0.0

        actual = metric_values.get(metric)
        if actual is None or metric not in SUPPORTED_METRICS:
            matched_c = False
        else:
            matched_c = _eval_op(float(actual), op, threshold)

        cond_results.append({
            "metric":    metric,
            "label":     SUPPORTED_METRICS.get(metric, metric),
            "op":        op,
            "threshold": threshold,
            "actual":    actual,
            "matched":   matched_c,
            # actual is None → метрика недоступна (нет данных), а не «условие не выполнено».
            "unavailable": actual is None,
        })

    if not cond_results:
        matched = False
    elif logic == "OR":
        matched = any(r["matched"] for r in cond_results)
    else:
        matched = all(r["matched"] for r in cond_results)

    action_type = action.get("type", "")
    would_affect = _preview_action(action_type, action.get("params") or {}, login) if matched else []

    return {
        "matched": matched,
        "preview": {
            "metric_values":    metric_values,
            "period_days":      period,
            "condition_results":cond_results,
            "logic":            logic,
            "action": {
                "type":   action_type,
                "label":  ACTION_LABELS.get(action_type, action_type),
                "params": action.get("params") or {},
            },
            "would_affect":      would_affect,
            "auto_exec_blocked": not _AUTO_EXEC_ENABLED and mode == "auto",
            "mode":              mode,
        },
        "error": None,
    }


def _eval_op(actual: float, op: str, threshold: float) -> bool:
    if op == ">":  return actual > threshold
    if op == ">=": return actual >= threshold
    if op == "<":  return actual < threshold
    if op == "<=": return actual <= threshold
    if op == "=":  return actual == threshold
    return False


def _fetch_account_metrics(login: str, period_days: int, ctx: dict) -> tuple[dict, str | None]:
    """Метрики аккаунта из Victory DB за период. Возвращает (dict, error|None)."""
    victory_conn = ctx.get("victory_conn")
    if not victory_conn:
        return {}, "Victory DB недоступна"
    try:
        conn = victory_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        COALESCE(SUM(total_cost),0)::float              AS spend,
                        COALESCE(SUM(clicks),0)::float                  AS clicks,
                        COALESCE(SUM(impressions),0)::float             AS impressions,
                        CASE WHEN SUM(impressions)>0
                             THEN ROUND((SUM(clicks)::numeric
                                  /SUM(impressions))*100,2)::float
                             ELSE 0 END                                 AS ctr,
                        COALESCE(SUM(conversions),0)::float             AS conversions,
                        CASE WHEN SUM(clicks)>0
                             THEN ROUND((SUM(conversions)::numeric
                                  /SUM(clicks))*100,2)::float
                             ELSE 0 END                                 AS cr
                    FROM public.yandex_direct_manager_reports
                    WHERE account_login = %s
                      AND "Date" >= to_char(
                            (now() AT TIME ZONE 'Europe/Moscow'
                             - interval %s)::date, 'YYYY-MM-DD')
                    """,
                    (login, f"{period_days} days"),
                )
                row = cur.fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return {}, str(exc)[:150]

    if not row:
        return {
            "spend": 0, "clicks": 0, "impressions": 0,
            "ctr": 0, "cr": 0, "conversions": 0, "cpa": None, "drr": None
        }, None

    spend, clicks, impressions, ctr, conversions, cr = row
    cpa = round(float(spend) / float(conversions), 2) if conversions and conversions > 0 else None
    return {
        "spend":       round(float(spend or 0), 2),
        "clicks":      int(clicks or 0),
        "impressions": int(impressions or 0),
        "ctr":         round(float(ctr or 0), 4),
        "cr":          round(float(cr or 0), 4),
        "conversions": int(conversions or 0),
        "cpa":         cpa,
        "drr":         None,   # нет данных о доходе
    }, None


def _preview_action(action_type: str, params: dict, login: str) -> list[dict]:
    """Preview: что бы затронуло действие (только описание, без исполнения)."""
    label = ACTION_LABELS.get(action_type, action_type)
    base  = {"action_type": action_type, "label": label, "login": login}
    if action_type in ("pause_campaign", "resume_campaign",
                        "change_bid", "change_budget"):
        return [{**base, "note": f"{label} — применится ко всем активным кампаниям аккаунта"}]
    if action_type == "notify":
        return [{**base, "note": "Отправить UI-уведомление / запись в журнал"}]
    if action_type == "add_negative":
        phrase = params.get("phrase") or "(не задано)"
        return [{**base, "note": f"Добавить минус-фразу «{phrase}» во все TEXT_CAMPAIGN"}]
    if action_type == "add_excluded_site":
        site = params.get("site") or "(не задано)"
        return [{**base, "note": f"Добавить минус-площадку «{site}» → записать в журнал (Grid-шаг)"}]
    if action_type == "change_adjustment":
        seg = params.get("segment", "")
        val = params.get("value", 0)
        return [{**base, "note": f"Изменить корректировку {seg} на {val}%"}]
    return [{**base, "note": f"Действие «{action_type}»"}]

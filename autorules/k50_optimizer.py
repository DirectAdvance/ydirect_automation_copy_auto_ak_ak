"""
K50-style optimizer layer for Home autorules.

This module intentionally does not write to Yandex Direct. It builds event
templates and produces preview/report data that can later be approved manually.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any


TEMPLATES: list[dict[str, Any]] = [
    {
        "key": "monitoring_waste",
        "title": "Мониторинг расхода без заявок",
        "group": "Мониторинг",
        "description": "Находит расход без конверсий, дорогие кампании и резкие изменения к прошлому периоду.",
        "default_settings": {"period_days": 7, "data_lag_days": 1, "target_cpa": 2500, "min_spend": 2500},
        "rules": [
            {
                "priority": 10,
                "name": "Расход без конверсий",
                "level": "campaign",
                "condition_json": {"metric": "spend", "op": ">=", "value_key": "min_spend", "extra": {"conversions_eq": 0}},
                "action_json": {"type": "recommend_pause_or_reduce_budget", "params": {"change_pct": -20}},
            },
            {
                "priority": 20,
                "name": "CPA выше цели на 30%",
                "level": "campaign",
                "condition_json": {"metric": "cpa", "op": ">", "formula": "target_cpa*1.3", "extra": {"min_conversions": 1}},
                "action_json": {"type": "recommend_reduce_bid_or_cpa", "params": {"change_pct": -15}},
            },
            {
                "priority": 30,
                "name": "Расход изменился на 30% к прошлой неделе",
                "level": "account",
                "condition_json": {"metric": "spend_delta_pct", "op": ">=", "value": 30},
                "action_json": {"type": "notify", "params": {"channel": "ui"}},
            },
        ],
    },
    {
        "key": "high_cpa_bid_down",
        "title": "Снижение ставок при высоком CPA",
        "group": "Биддинг",
        "description": "K50-паттерн: если CPA выше KPI или расход есть без заявок, снижать ставку осторожным шагом.",
        "default_settings": {"period_days": 14, "data_lag_days": 1, "target_cpa": 2500, "min_spend": 3000, "change_pct": -15},
        "rules": [
            {
                "priority": 10,
                "name": "Есть заявки, CPA выше KPI",
                "level": "campaign",
                "condition_json": {"metric": "cpa", "op": ">", "formula": "target_cpa", "extra": {"min_conversions": 2}},
                "action_json": {"type": "change_bid_pct", "params": {"value_key": "change_pct", "min_bid": 0.3}},
            },
            {
                "priority": 20,
                "name": "Расход выше KPI, заявок нет",
                "level": "campaign",
                "condition_json": {"metric": "spend", "op": ">", "value_key": "min_spend", "extra": {"conversions_eq": 0}},
                "action_json": {"type": "change_bid_pct", "params": {"value_key": "change_pct", "min_bid": 0.3}},
            },
        ],
    },
    {
        "key": "cascade_cpa_cr",
        "title": "Каскад ключ → группа → кампания",
        "group": "Биддинг",
        "description": "Основа K50: считать ставку с самого узкого уровня, где хватает статистики; иначе подниматься уровнем выше.",
        "default_settings": {"period_days": 21, "data_lag_days": 1, "target_cpa": 2500, "min_clicks": 200, "min_conversions": 3, "cr_coef": 1.15},
        "rules": [
            {
                "priority": 10,
                "name": "Ключи с достаточной статистикой",
                "level": "keyword",
                "condition_json": {"metric": "clicks", "op": ">=", "value_key": "min_clicks"},
                "action_json": {"type": "set_bid_by_cr_target_cpa", "params": {"coef_key": "cr_coef"}},
            },
            {
                "priority": 20,
                "name": "Фолбэк на группу",
                "level": "adgroup",
                "condition_json": {"metric": "conversions", "op": ">=", "value_key": "min_conversions"},
                "action_json": {"type": "set_group_bid_by_cr_target_cpa", "params": {"coef_key": "cr_coef"}},
            },
            {
                "priority": 30,
                "name": "Фолбэк на кампанию",
                "level": "campaign",
                "condition_json": {"metric": "conversions", "op": ">=", "value_key": "min_conversions"},
                "action_json": {"type": "set_campaign_bid_by_cr_target_cpa", "params": {"coef_key": "cr_coef"}},
            },
        ],
    },
    {
        "key": "auto_strategy_conversions",
        "title": "Автостратегия: CPA и недельный бюджет",
        "group": "Автостратегии",
        "description": "Для оптимизации конверсий менять не ставки, а целевой CPA и бюджет по K50-логике.",
        "default_settings": {"period_days": 7, "data_lag_days": 1, "target_cpa": 2500, "budget_raise_pct": 20, "cpa_raise_pct": 10, "cpa_down_pct": -15},
        "rules": [
            {
                "priority": 10,
                "name": "Расход вырос, конверсий нет",
                "level": "campaign",
                "condition_json": {"metric": "spend", "op": ">", "formula": "target_cpa*1.5", "extra": {"conversions_eq": 0}},
                "action_json": {"type": "recommend_target_cpa_pct", "params": {"value_key": "cpa_down_pct"}},
            },
            {
                "priority": 20,
                "name": "CPA ниже KPI*0.8",
                "level": "campaign",
                "condition_json": {"metric": "cpa", "op": "<=", "formula": "target_cpa*0.8", "extra": {"min_conversions": 1}},
                "action_json": {"type": "recommend_target_cpa_pct", "params": {"value_key": "cpa_raise_pct"}},
            },
            {
                "priority": 30,
                "name": "CPA в допуске, расход упирается в бюджет",
                "level": "campaign",
                "condition_json": {"metric": "cpa", "op": "<=", "formula": "target_cpa*1.3", "extra": {"min_conversions": 1}},
                "action_json": {"type": "recommend_weekly_budget_pct", "params": {"value_key": "budget_raise_pct"}},
            },
        ],
    },
    {
        "key": "placements_cleaning",
        "title": "РСЯ: неэффективные площадки",
        "group": "Площадки",
        "description": "Искать площадки с расходом и без заявок; применение пока через отчёт/ручное подтверждение.",
        "default_settings": {"period_days": 14, "data_lag_days": 1, "min_spend": 1500},
        "rules": [
            {
                "priority": 10,
                "name": "Площадка с расходом без заявок",
                "level": "placement",
                "condition_json": {"metric": "spend", "op": ">=", "value_key": "min_spend", "extra": {"conversions_eq": 0}},
                "action_json": {"type": "recommend_exclude_placement", "params": {}},
            }
        ],
    },
    {
        "key": "autotargeting_control",
        "title": "Автотаргетинг под контроль",
        "group": "Автотаргетинг",
        "description": "Для ручных стратегий отделять autotargeting от обычных фраз и снижать его влияние.",
        "default_settings": {"period_days": 14, "data_lag_days": 1, "target_cpa": 2500, "autotarget_bid": 0.3},
        "rules": [
            {
                "priority": 10,
                "name": "Автотаргетинг без заявок",
                "level": "keyword",
                "condition_json": {"metric": "keyword_contains", "op": "=", "value": "autotargeting"},
                "action_json": {"type": "recommend_autotarget_bid", "params": {"value_key": "autotarget_bid"}},
            }
        ],
    },
    {
        "key": "creative_ctr_guard",
        "title": "РСЯ: слабые креативы",
        "group": "Креативы",
        "description": "Сравнивать CTR объявления с CTR группы; слабый креатив останавливать, если в группе есть альтернатива.",
        "default_settings": {"period_days": 14, "data_lag_days": 1, "ctr_ratio": 0.7, "min_impressions": 1000},
        "rules": [
            {
                "priority": 10,
                "name": "CTR ниже группы",
                "level": "ad",
                "condition_json": {"metric": "ctr_vs_group", "op": "<", "value_key": "ctr_ratio", "extra": {"min_impressions_key": "min_impressions"}},
                "action_json": {"type": "recommend_pause_creative", "params": {}},
            }
        ],
    },
    {
        "key": "landing_security_monitor",
        "title": "Мониторинг текстов, ссылок и посадочных",
        "group": "Безопасность",
        "description": "Проверка редиректов, 404/5xx и подозрительных замен в объявлениях.",
        "default_settings": {"period_days": 1, "data_lag_days": 0},
        "rules": [
            {
                "priority": 10,
                "name": "Битые ссылки и редиректы",
                "level": "ad",
                "condition_json": {"metric": "url_status", "op": "in", "value": ["404", "5xx", "redirect"]},
                "action_json": {"type": "notify_and_recommend_pause_ad", "params": {}},
            }
        ],
    },
]


def templates_list() -> list[dict[str, Any]]:
    """Public template metadata for UI."""
    return [
        {
            "key": t["key"],
            "title": t["title"],
            "group": t["group"],
            "description": t["description"],
            "default_settings": t["default_settings"],
            "rules": t["rules"],
            "rules_count": len(t["rules"]),
        }
        for t in TEMPLATES
    ]


def template_by_key(key: str) -> dict[str, Any] | None:
    return next((t for t in TEMPLATES if t["key"] == key), None)


def event_from_template(template_key: str, login: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    tpl = template_by_key(template_key)
    if not tpl:
        raise ValueError("template not found")
    merged = {**(tpl.get("default_settings") or {}), **(settings or {})}
    schedule = "twice_weekly" if template_key == "auto_strategy_conversions" else "weekly"
    if template_key in ("placements_cleaning", "landing_security_monitor"):
        schedule = "daily"
    return {
        "account_login": login,
        "name": tpl["title"],
        "schedule": schedule,
        "mode": "manual",
        "data_lag_days": int(merged.get("data_lag_days") or 0),
        "template_key": template_key,
        "settings_json": merged,
        "rules": tpl["rules"],
        "status": "active",
    }


def preview_event(event: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Evaluate event rules against available account/campaign stats.

    Entity-level keyword/ad/placement data is intentionally reported as
    "needs_dataset" until a Reports API loader is attached.
    """
    login = event.get("account_login") or ""
    settings = event.get("settings_json") or {}
    rules = event.get("rules") or []
    period_days = int(settings.get("period_days") or 7)
    lag_days = int(event.get("data_lag_days") if event.get("data_lag_days") is not None else settings.get("data_lag_days") or 0)

    stats, error = _fetch_stats(login, period_days, lag_days, ctx.get("victory_conn"))
    if error:
        return {"ok": False, "error": error, "event": _event_header(event), "matches": [], "summary": {}}

    matches: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    for rule in sorted(rules, key=lambda r: int(r.get("priority") or 100)):
        if rule.get("status", "active") != "active":
            continue
        level = rule.get("level") or "account"
        dataset = stats["account"] if level == "account" else stats.get(level + "s", [])
        if level not in ("account", "campaign"):
            unsupported.append(_unsupported_rule(rule, level))
            continue
        rows = dataset if isinstance(dataset, list) else [dataset]
        for row in rows:
            result = _rule_matches(rule, row, settings, stats)
            if result["matched"]:
                matches.append({
                    "rule_id": rule.get("id"),
                    "priority": int(rule.get("priority") or 100),
                    "rule_name": rule.get("name") or "Правило",
                    "level": level,
                    "entity": _entity_label(level, row),
                    "metric": result["metric"],
                    "actual": result["actual"],
                    "threshold": result["threshold"],
                    "action": _action_preview(rule.get("action_json") or {}, settings),
                    "reason": result["reason"],
                })

    matches.sort(key=lambda m: (m["priority"], m["level"], m["entity"]))
    summary = {
        "matches": len(matches),
        "unsupported_rules": len(unsupported),
        "period_days": period_days,
        "data_lag_days": lag_days,
        "date_from": stats["date_from"].isoformat(),
        "date_to": stats["date_to"].isoformat(),
        "account": stats["account"],
    }
    return {
        "ok": True,
        "event": _event_header(event),
        "summary": summary,
        "matches": matches[:500],
        "unsupported": unsupported,
        "auto_exec_blocked": True,
        "note": "Preview only: изменения в Директ не применяются.",
    }


def _event_header(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": event.get("id"),
        "name": event.get("name"),
        "account_login": event.get("account_login"),
        "schedule": event.get("schedule"),
        "mode": event.get("mode"),
        "template_key": event.get("template_key"),
    }


def _fetch_stats(login: str, period_days: int, lag_days: int, victory_conn) -> tuple[dict[str, Any], str | None]:
    if not victory_conn:
        return {}, "Victory DB недоступна"
    end = date.today() - timedelta(days=max(lag_days, 0))
    start = end - timedelta(days=max(period_days, 1) - 1)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=max(period_days, 1) - 1)
    try:
        conn = victory_conn()
        try:
            with conn.cursor() as cur:
                account = _fetch_agg(cur, login, start, end, None)
                prev_account = _fetch_agg(cur, login, prev_start, prev_end, None)
                campaigns = _fetch_campaigns(cur, login, start, end)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return {}, str(exc)[:180]

    spend_prev = float(prev_account.get("spend") or 0)
    spend_now = float(account.get("spend") or 0)
    account["spend_delta_pct"] = round(abs(spend_now - spend_prev) / spend_prev * 100, 1) if spend_prev > 0 else None
    account["prev_spend"] = round(spend_prev, 2)
    return {
        "date_from": start,
        "date_to": end,
        "account": account,
        "campaigns": campaigns,
    }, None


def _fetch_agg(cur, login: str, start: date, end: date, campaign_name: str | None) -> dict[str, Any]:
    where = 'account_login = %s AND "Date" BETWEEN %s AND %s'
    params: list[Any] = [login, start.isoformat(), end.isoformat()]
    if campaign_name is not None:
        where += ' AND "CampaignName" = %s'
        params.append(campaign_name)
    cur.execute(
        f"""
        SELECT
            COALESCE(SUM(total_cost),0)::float,
            COALESCE(SUM("Clicks"),0)::float,
            COALESCE(SUM("Impressions"),0)::float,
            COALESCE(SUM(all_forms),0)::float
        FROM public.yandex_direct_manager_reports
        WHERE {where}
        """,
        params,
    )
    row = cur.fetchone() or (0, 0, 0, 0)
    spend, clicks, impressions, conversions = [float(x or 0) for x in row]
    return _metrics(spend, clicks, impressions, conversions)


def _fetch_campaigns(cur, login: str, start: date, end: date) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT
            COALESCE("CampaignName", '') AS campaign_name,
            COALESCE(SUM(total_cost),0)::float,
            COALESCE(SUM("Clicks"),0)::float,
            COALESCE(SUM("Impressions"),0)::float,
            COALESCE(SUM(all_forms),0)::float
        FROM public.yandex_direct_manager_reports
        WHERE account_login = %s
          AND "Date" BETWEEN %s AND %s
        GROUP BY "CampaignName"
        ORDER BY COALESCE(SUM(total_cost),0) DESC
        LIMIT 200
        """,
        (login, start.isoformat(), end.isoformat()),
    )
    rows = []
    for name, spend, clicks, impressions, conversions in cur.fetchall():
        m = _metrics(float(spend or 0), float(clicks or 0), float(impressions or 0), float(conversions or 0))
        m["campaign_name"] = name or "Без названия"
        rows.append(m)
    return rows


def _metrics(spend: float, clicks: float, impressions: float, conversions: float) -> dict[str, Any]:
    return {
        "spend": round(spend, 2),
        "clicks": int(clicks),
        "impressions": int(impressions),
        "conversions": int(conversions),
        "ctr": round(clicks / impressions * 100, 2) if impressions > 0 else 0,
        "cr": round(conversions / clicks * 100, 2) if clicks > 0 else 0,
        "cpa": round(spend / conversions, 2) if conversions > 0 else None,
    }


def _rule_matches(rule: dict[str, Any], row: dict[str, Any], settings: dict[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    cond = rule.get("condition_json") or {}
    metric = cond.get("metric") or "spend"
    actual = row.get(metric)
    threshold = _threshold(cond, settings)
    extra = cond.get("extra") or {}

    if extra.get("conversions_eq") is not None and int(row.get("conversions") or 0) != int(extra["conversions_eq"]):
        return {"matched": False, "metric": metric, "actual": actual, "threshold": threshold, "reason": "conversions guard"}
    if extra.get("min_conversions") is not None and int(row.get("conversions") or 0) < int(extra["min_conversions"]):
        return {"matched": False, "metric": metric, "actual": actual, "threshold": threshold, "reason": "min conversions guard"}
    if actual is None or threshold is None:
        return {"matched": False, "metric": metric, "actual": actual, "threshold": threshold, "reason": "metric unavailable"}

    matched = _compare(float(actual), cond.get("op") or ">", float(threshold))
    return {
        "matched": matched,
        "metric": metric,
        "actual": actual,
        "threshold": threshold,
        "reason": f"{metric} {cond.get('op') or '>'} {threshold}",
    }


def _threshold(cond: dict[str, Any], settings: dict[str, Any]) -> float | None:
    if cond.get("value_key"):
        return _num(settings.get(cond["value_key"]))
    if cond.get("formula"):
        return _eval_formula(str(cond["formula"]), settings)
    if cond.get("value") is not None and not isinstance(cond.get("value"), (list, dict)):
        return _num(cond.get("value"))
    return None


def _eval_formula(expr: str, settings: dict[str, Any]) -> float | None:
    target_cpa = _num(settings.get("target_cpa")) or 0.0
    try:
        return float(eval(expr, {"__builtins__": {}}, {"target_cpa": target_cpa}))  # noqa: S307 - fixed internal formulas
    except Exception:
        return None


def _compare(actual: float, op: str, threshold: float) -> bool:
    if op == ">":
        return actual > threshold
    if op == ">=":
        return actual >= threshold
    if op == "<":
        return actual < threshold
    if op == "<=":
        return actual <= threshold
    if op == "=":
        return actual == threshold
    return False


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _entity_label(level: str, row: dict[str, Any]) -> str:
    if level == "campaign":
        return row.get("campaign_name") or "Кампания"
    return "Аккаунт"


def _action_preview(action: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    params = action.get("params") or {}
    resolved = {}
    for key, value in params.items():
        if key.endswith("_key"):
            resolved[key[:-4]] = settings.get(value)
        else:
            resolved[key] = value
    labels = {
        "notify": "Уведомить",
        "change_bid_pct": "Изменить ставку, %",
        "recommend_pause_or_reduce_budget": "Рекомендовать паузу/снижение бюджета",
        "recommend_reduce_bid_or_cpa": "Снизить ставку или CPA",
        "set_bid_by_cr_target_cpa": "Ставка = CR * целевой CPA",
        "set_group_bid_by_cr_target_cpa": "Групповая ставка от CR",
        "set_campaign_bid_by_cr_target_cpa": "Кампанейская ставка от CR",
        "recommend_target_cpa_pct": "Изменить целевой CPA, %",
        "recommend_weekly_budget_pct": "Изменить недельный бюджет, %",
        "recommend_exclude_placement": "Исключить площадку",
        "recommend_autotarget_bid": "Ставка автотаргетинга",
        "recommend_pause_creative": "Остановить слабый креатив",
        "notify_and_recommend_pause_ad": "Уведомить и рекомендовать остановку объявления",
    }
    typ = action.get("type") or "notify"
    return {"type": typ, "label": labels.get(typ, typ), "params": resolved}


def _unsupported_rule(rule: dict[str, Any], level: str) -> dict[str, Any]:
    return {
        "priority": int(rule.get("priority") or 100),
        "rule_name": rule.get("name") or "Правило",
        "level": level,
        "reason": "Для этого уровня нужен отдельный Reports API датасет; шаблон сохранён, preview покажет после подключения loader.",
        "action": _action_preview(rule.get("action_json") or {}, {}),
    }

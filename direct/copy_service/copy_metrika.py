"""Подстановка счётчика/цели Метрики в стратегию копируемых кампаний.

Вынесено из copy_engine.py (чистый code-motion, логика не изменена).
DI инъектится copy_engine.configure() фан-аутом; sibling-модули copy_* — прямой импорт (DAG, цикла нет).
"""
from __future__ import annotations

import json
from pathlib import Path

from .copy_jobs import _copy_job_log
from .copy_snapshot import _copy_read_json

# ── DI (инъектится copy_engine.configure фан-аутом; None до инъекции) ──
_v5_call = _v5_err = None


def configure(deps: dict) -> None:
    """Инъекция DI из blueprint (фан-аут из copy_engine.configure)."""
    globals().update(deps)


def _copy_rewrite_strategy_goal(strategy: dict, goal_id: int) -> dict:
    """Проставить новую цель «Все формы» во всех goal-bearing стратегиях."""
    s = json.loads(json.dumps(strategy or {}))
    for side in ("Search", "Network"):
        blk = s.get(side) or {}
        for k in ("AverageCpa", "PayForConversion", "WbMaximumConversionRate", "AverageCrr", "PayForConversionCrr", "AverageRoi"):
            if isinstance(blk.get(k), dict):
                blk[k]["GoalId"] = int(goal_id)
    return s


def _copy_apply_metrika(login: str, token: str, src_dir: Path, workdir: Path,
                        counter_id: int, goal_id: int, source_ids: set[int], job_id: str) -> dict:
    """Докрутить на скопированных кампаниях счётчик Метрики и goal_id."""
    maps = _copy_read_json(workdir / "id_maps.json") if (workdir / "id_maps.json").exists() else {}
    camp_map = maps.get("campaigns") or {}
    campaigns = [c for c in _copy_read_json(src_dir / "campaigns.json") if int(c.get("Id") or 0) in source_ids]
    updated = 0
    warned = 0
    for c in campaigns:
        src_id = str(c.get("Id"))
        tgt_id = camp_map.get(src_id)
        if not tgt_id:
            continue
        ctype = c.get("Type", "TEXT_CAMPAIGN")
        struct_key = {
            "TEXT_CAMPAIGN": "TextCampaign",
            "DYNAMIC_TEXT_CAMPAIGN": "DynamicTextCampaign",
            "SMART_CAMPAIGN": "SmartCampaign",
            "CPM_BANNER_CAMPAIGN": "CpmBannerCampaign",
        }.get(ctype)
        if not struct_key:
            warned += 1
            _copy_job_log(job_id, f"метрика: {c.get('Name') or src_id} — тип {ctype} оставлен без авто-докрутки стратегии")
            continue
        type_data = c.get(struct_key) or {}
        body = {"Id": int(tgt_id), struct_key: {}}
        if struct_key == "SmartCampaign":
            body[struct_key]["CounterId"] = int(counter_id)
        else:
            body[struct_key]["CounterIds"] = {"Items": [int(counter_id)]}
        if type_data.get("TrackingParams"):
            body[struct_key]["TrackingParams"] = type_data["TrackingParams"]
        if type_data.get("AttributionModel"):
            body[struct_key]["AttributionModel"] = type_data["AttributionModel"]
        strategy = type_data.get("BiddingStrategy") or {}
        # PAY_FOR_CONVERSION_MULTIPLE_GOALS: v5 не принимает без счётчика+целей (4000/8000).
        # Стратегия будет восстановлена через Grid в _copy_cookie_postprocess — здесь пропускаем.
        # ЛЮБАЯ *_MULTIPLE_GOALS, а не только PAY_FOR_CONVERSION: v5 не принимает их без
        # счётчика+целей и отклоняет ВЕСЬ item — вместе со счётчиком, ради которого апдейт
        # и делается. Из-за этого копии porg-c6rxuenb получили счётчик ИСТОЧНИКА (104132068)
        # вместо целевого, а шаг отчитался `updated: 12` (2026-08-07). Стратегия всё равно
        # восстанавливается позже через Grid в постпроцессе.
        _has_multi_goal = any(
            str((strategy.get(side) or {}).get("BiddingStrategyType") or "").endswith("_MULTIPLE_GOALS")
            for side in ("Search", "Network")
        )
        if strategy and not _has_multi_goal:
            body[struct_key]["BiddingStrategy"] = _copy_rewrite_strategy_goal(strategy, goal_id)
        try:
            j = _v5_call("campaigns", "update", token, login, {"Campaigns": [body]})
            if "error" in j:
                warned += 1
                _copy_job_log(job_id, f"метрика update {c.get('Name') or src_id}: {_v5_err(j)[:220]}")
                continue
            # ⚠️ v5 кладёт отказ по КАЖДОЙ кампании в UpdateResults[].Errors, а не в top-level
            # `error`. Без этой проверки отклонённый апдейт считался успешным: шаг рапортовал
            # `updated: 12`, а на кампаниях оставался счётчик источника (инцидент 2026-08-07).
            _item_errors = [
                e for _res in (((j.get("result") or {}).get("UpdateResults") or []))
                for e in (_res.get("Errors") or [])
            ]
            if _item_errors:
                warned += 1
                _first = _item_errors[0]
                _copy_job_log(job_id, f"метрика update {c.get('Name') or src_id}: отклонено v5 — "
                                      f"{_first.get('Code')} {str(_first.get('Message') or '')[:100]} "
                                      f"{str(_first.get('Details') or '')[:120]}")
                continue
            updated += 1
        except Exception as e:  # noqa: BLE001
            warned += 1
            _copy_job_log(job_id, f"метрика update {c.get('Name') or src_id}: {str(e)[:220]}")
            continue
        # Цели переносим 1:1 только при ОБЩЕМ счётчике: GoalId привязан к счётчику источника,
        # чужой счётчик → невалидные цели (4000/8000). Value — микро-единицы, как в источнике.
        pg_items = (type_data.get("PriorityGoals") or {}).get("Items") or []
        src_counters = [int(x) for x in ((type_data.get("CounterIds") or {}).get("Items") or [])
                        if str(x).strip().isdigit()]
        if not (pg_items and int(counter_id) in src_counters):
            continue
        goals = []
        for g in pg_items:
            gid = g.get("GoalId")
            if gid in (None, ""):
                continue
            # Operation только SET: ADD отвергается API (3500).
            item = {"GoalId": int(gid), "Operation": "SET"}
            if g.get("Value") is not None:
                item["Value"] = int(g["Value"])
            if g.get("IsMetrikaSourceOfValue") is not None:
                item["IsMetrikaSourceOfValue"] = g["IsMetrikaSourceOfValue"]
            goals.append(item)
        if not goals:
            continue
        # Отдельным update ПОСЛЕ основного: отказ по целям не должен утащить CounterIds/стратегию.
        try:
            j = _v5_call("campaigns", "update", token, login,
                         {"Campaigns": [{"Id": int(tgt_id), struct_key: {"PriorityGoals": {"Items": goals}}}]})
            if "error" in j:
                warned += 1
                _copy_job_log(job_id, f"цели update {c.get('Name') or src_id}: {_v5_err(j)[:220]}")
        except Exception as e:  # noqa: BLE001
            warned += 1
            _copy_job_log(job_id, f"цели update {c.get('Name') or src_id}: {str(e)[:220]}")
    return {"updated": updated, "warned": warned}

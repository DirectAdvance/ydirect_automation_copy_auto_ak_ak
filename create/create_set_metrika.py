"""Metrika counter/goal preparation for Direct create_set."""
from __future__ import annotations

from typing import Any, Callable, Optional


MetrikaGoalsGetter = Callable[[str], Optional[dict[str, Any]]]
GoalGetter = Callable[[int], tuple[Optional[int], Optional[str]]]
ForeignOwnerGetter = Callable[[Optional[int], str], Optional[str]]


def prepare_metrika(*, login: str, counter_id: int | None, goal_id: int | None,
                    via_cookie: bool, no_cpa: bool,
                    metrika_goals_for: MetrikaGoalsGetter,
                    goal_vse_formy: GoalGetter,
                    counter_foreign_owner: ForeignOwnerGetter) -> dict[str, Any]:
    """Resolve and validate Metrika counter/goal before creating campaigns."""
    counter = int(counter_id or 0)
    goal = int(goal_id or 0)
    if not counter or not goal:
        goals = metrika_goals_for(login) or {}
        counters = goals.get("counters") or []
        if not counter and counters:
            counter = int(counters[0] or 0)
        if not goal and goals.get("goal_id"):
            goal = int(goals.get("goal_id") or 0)
    if counter and not goal:
        resolved_goal, _goal_name = goal_vse_formy(counter)
        goal = int(resolved_goal or 0)

    optional = bool(via_cookie and no_cpa)
    note = None
    if optional and (not counter or not goal):
        note = ("счётчик/цель Метрики не указаны: via_cookie+no_cpa создаёт CPC-only "
                "черновики без конверсионной стратегии")
    if not counter and not optional:
        return {"ok": False, "error": "укажите счётчик Метрики", "status": 400,
                "counter_id": counter, "goal_id": goal, "metrika_note": note}
    if not goal and not optional:
        return {"ok": False, "error": "укажите цель (goal_id)", "status": 400,
                "counter_id": counter, "goal_id": goal, "metrika_note": note}

    owner = counter_foreign_owner(counter, login) if counter else None
    if owner:
        return {
            "ok": False,
            "status": 400,
            "counter_id": counter,
            "goal_id": goal,
            "metrika_note": note,
            "error": (
                f"счётчик Метрики {counter} принадлежит аккаунту «{owner}», а не «{login}» — "
                "Яндекс отклонит цель как «не найдена». Укажите счётчик и цель самого "
                f"«{login}»."
            ),
        }
    return {"ok": True, "counter_id": counter, "goal_id": goal, "metrika_note": note,
            "metrika_optional": optional}

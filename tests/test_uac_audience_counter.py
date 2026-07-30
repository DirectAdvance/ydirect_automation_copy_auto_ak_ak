"""Регрессия UAC_AUDIENCE_COUNTER_BLIND_TO_CONDITION_RULES (2026-07-30).

`_count_audiences` читала только `ca_retargeting_condition.goals`, а UAC отдаёт цели уровнем
глубже — `ca_retargeting_condition.condition_rules[].goals`. Верхнего `goals` в ответе нет →
счётчик возвращал `None` → tri-state гасил проверку → `UAC_STRUCT_AUDIENCES_MISSING` не мог
сработать НИКОГДА.

Живой случай: `porg-xjxpfxby` cid=713160868 «МК - Общая - Аудитории + Автотаргетинг».
Структура слепка ждала 9 аудиторий, кампания их РЕАЛЬНО несла, а сводка показывала «нет
данных» — из-за чего кампанию легко принять за сломанную (я сам так и сделал, пока не
разобрал сырой ответ).
"""
from __future__ import annotations

import pytest

from direct.clients.uac_read import summarize_uac_detail


def _detail(cond):
    return {"ca_retargeting_condition": cond}


def test_counts_goals_inside_condition_rules():
    """Реальная форма ответа UAC: цели внутри condition_rules[].goals."""
    row = _detail({"condition_rules": [
        {"type": "OR", "interestType": "short-term",
         "goals": [{"id": 19900000024}, {"id": 19900830072}, {"id": 2499680141}]},
    ]})
    assert summarize_uac_detail(row)["audiences"] == 3


def test_counts_across_several_rules_without_double_count():
    """Несколько правил суммируются, повтор одной цели не удваивает счёт."""
    row = _detail({"condition_rules": [
        {"goals": [{"id": 1}, {"id": 2}]},
        {"goals": [{"id": 2}, {"id": 3}]},
    ]})
    assert summarize_uac_detail(row)["audiences"] == 3


def test_top_level_goals_still_supported():
    """Запасная форма (goals на верхнем уровне) продолжает работать."""
    assert summarize_uac_detail(_detail({"goals": [{"id": 1}, {"id": 2}]}))["audiences"] == 2


def test_empty_rules_give_zero_not_none():
    """Пустые правила = РЕАЛЬНЫЙ ноль, а не «поле не отдано».

    Разница принципиальная: `None` гасит проверку (tri-state), `0` обязан её поднять.
    """
    assert summarize_uac_detail(_detail({"condition_rules": []}))["audiences"] == 0


@pytest.mark.parametrize("cond", [None, "не словарь", 42])
def test_missing_condition_stays_none(cond):
    """Поля нет/оно не словарь → None: проверка молчит, ложного «0 аудиторий» не выдаём."""
    assert summarize_uac_detail(_detail(cond))["audiences"] is None


def test_audiences_list_fallback():
    """Плоский список audiences (иная форма ответа) тоже считается."""
    assert summarize_uac_detail({"audiences": [1, 2, 3, 4]})["audiences"] == 4

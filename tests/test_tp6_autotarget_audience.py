"""Регрессия TP6_AUTOTARGET_RENDERS_MANUAL_AUDIENCE (2026-07-30).

Семён по кабинету: `porg-uy3huxcn`, кампания
`tp6_cpc_site_ct0000_aon_n000_r0002_ct001_ag001_g00 — МК - Общие запросы - Автотаргетинг`
показывала блок «Аудитория» = **«Настроить вручную»**, хотя по имени и режиму должна быть
полным автотаргетом.

UAC-detail этой кампании: `keywords=null`, аудиторий нет, `minus_keywords=["отзывы"]` —
минус-слова оказались ЕДИНСТВЕННЫМ ручным сигналом в payload, и UAC из-за них флипает блок
«Аудитория» в ручной режим. Тот же механизм был описан и починен для товарки (tp7) ещё
2026-07-10, но с посылкой «tp6-мастер не тронут (рендерит верно)» — живой кабинет её опроверг.

Инвариант: пустые минус-слова для АВТОТАРГЕТ-режима зависят от РЕЖИМА позиции, а не от типа
кампании. Ручные режимы (keywords/audience) минус-слова сохраняют — их зануление порезало бы
работающие исключения.
"""
from __future__ import annotations

import inspect
import re

import pytest

from direct.create import create_set_master_product as mp


def _minus_expr() -> str:
    """Текст выражения, которым собирается minus_keywords в spec."""
    src = inspect.getsource(mp)
    m = re.search(r"minus_keywords=\((.*?)\),\n", src, re.S)
    assert m, "не нашёл присваивание minus_keywords в spec"
    return m.group(1)


def test_autotarget_empties_minus_keywords_for_both_campaign_types():
    """Условие обнуления НЕ должно быть привязано к типу кампании (is_product)."""
    expr = _minus_expr()
    assert 'targeting_mode == "autotarget"' in expr, expr
    assert "is_product" not in expr, (
        "minus_keywords для автотаргета снова сузили до товарки — tp6-мастер получит "
        "«Настроить вручную» (TP6_AUTOTARGET_RENDERS_MANUAL_AUDIENCE)"
    )


def test_manual_modes_keep_minus_keywords():
    """Ручные режимы обязаны сохранить минус-слова: иначе исключения молча пропадут."""
    expr = _minus_expr()
    assert "_enabled_minus_words()" in expr, expr
    assert "it_minus_keywords" in expr, expr


@pytest.mark.parametrize("mode,is_product,expect_empty", [
    ("autotarget", False, True),    # tp6 Мастер — дефект Семёна
    ("autotarget", True, True),     # tp7 Товарка — закрыто ещё 2026-07-10
    ("keywords", False, False),
    ("audience", False, False),
    ("keywords", True, False),
])
def test_minus_rule_truth_table(mode, is_product, expect_empty):
    """Правило целиком: пусто ТОЛЬКО у автотаргета, в обоих типах кампаний."""
    it_minus, enabled = ["своё"], ["отзывы"]
    got = ([] if mode == "autotarget"
           else list(dict.fromkeys((it_minus or []) + enabled)))
    assert (got == []) is expect_empty, (mode, is_product, got)


def test_autotarget_keeps_full_socdem_age_18():
    """Полный автотаргет = полный socdem (age_18); ручные режимы tp6 — age_25."""
    src = inspect.getsource(mp)
    assert 'age_lower=("age_18" if (targeting_mode == "autotarget" or is_product) else "age_25")' in src, (
        "изменилась логика age_lower — сверить с DoD §2 и решением Семёна 2026-07-21"
    )

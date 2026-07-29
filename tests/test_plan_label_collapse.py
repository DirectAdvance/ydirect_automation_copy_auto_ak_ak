"""Фильтр `selected_pos` обязан понимать схлопнутые метки UI-дерева набора.

Баг 2026-07-28: UI (`automation.js:_campaignize` → `_campCollapseName`) для tp1/tp2/tp4/tp5
отдаёт метку кампании БЕЗ хвоста таргетинга («… - КС», «… - Автотаргетинг»), а
`create_set_plan` фильтровал план по сырому `camp_names` из структуры — с хвостом. Совпадений
не было, tp1/tp2/tp4/tp5 молча выпадали из плана, оставалась одна товарка (у tp7 хвоста нет),
и запуск отбивался гейтом `stale_client_product_only_stream` («получен только tp7/product»).
"""

import pytest

from direct.create.create_set_plan import _canon_camp_name, _match_camp_label


def test_no_selection_matches_everything():
    assert _match_camp_label(None, 1, "что угодно") is True


@pytest.mark.parametrize("tp_num", [1, 2, 4, 5])
@pytest.mark.parametrize("tail", ["КС", "Автотаргетинг", "КС + Автотаргетинг",
                                  "Автотаргетинг + КС", "Ключевики",
                                  "Ключевые слова", "Автотаргет"])
def test_collapsed_ui_label_matches_full_camp_name(tp_num, tail):
    """UI прислал базу без хвоста — кампания с хвостом обязана попасть в план."""
    sel = {"РСЯ - Комби - Марки"}
    assert _match_camp_label(sel, tp_num, f"РСЯ - Комби - Марки - {tail}") is True


def test_full_ui_label_still_matches():
    """Обратная совместимость: метка с хвостом (старый клиент) продолжает совпадать."""
    sel = {"РСЯ - Комби - Марки - КС"}
    assert _match_camp_label(sel, 1, "РСЯ - Комби - Марки - КС") is True


def test_segment_label_matches():
    """Фолбэк-путь: в selected_pos лежит сегмент, а не имя кампании."""
    assert _match_camp_label({"Марки"}, 1, "РСЯ - Комби - Модели", "Марки") is True


def test_foreign_label_is_not_matched():
    """Фильтр не открывается настежь: чужая метка по-прежнему отсекает кампанию."""
    sel = {"РСЯ - Комби - Марки"}
    assert _match_camp_label(sel, 1, "РСЯ - Комби - Модели - КС") is False


def test_tail_is_not_stripped_for_tp3_and_tp7():
    """Хвост режется ровно на тех tp, где его схлопывает UI (1/2/4/5) — не шире."""
    sel = {"ТГ - Фид"}
    assert _match_camp_label(sel, 3, "ТГ - Фид - КС") is False
    assert _match_camp_label(sel, 7, "ТГ - Фид - КС") is False


def test_spacing_differences_are_canonicalised():
    """UI канонизирует пробелы вокруг «-» и «+» — движок обязан сравнивать так же."""
    assert _match_camp_label({"РСЯ - Комби+Фид - Марки"}, 1,
                             "РСЯ  -  Комби + Фид  -  Марки - Автотаргетинг") is True


def test_canon_camp_name_normalises_nbsp_and_spacing():
    assert _canon_camp_name("РСЯ -  Комби+Фид") == "РСЯ - Комби + Фид"

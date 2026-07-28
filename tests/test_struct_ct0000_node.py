"""Структурный узел ЦЕЛИКОМ на ct0000 не должен раздуваться до «весь пак».

Живой дефект: узел «Агрегаторы» (kuderko / «С пробегом» / tp1, gc=ct0000_…, gk=агрегаторы_d,
camp_names «РСЯ - Комби - Агрегаторы - Аудитории») — в структуре ОДНА группа, а в кабинете
кампании 713097597/713097619 получили 27 групп: `structure_to_campaigns` намеренно не кладёт
ct0000 в `cts` (там перечисляются модель-ct), поэтому `only_cts` пустой, `_struct_items` ct0000
тоже пропускает — и билдер трактовал пустой ct-фильтр как «фильтра нет».
"""

import inspect

from direct import create_set_structure
from direct import create_set_tp1_builders


def _struct(tp_items):
    return {"directologists": [{"key": "kuderko", "site_types": [
        {"name": "С пробегом", "tp": [{"code": "tp1", "groups": [
            {"name": "Агрегаторы", "items": tp_items}]}]}]}]}


def _patch_struct(monkeypatch, tp_items):
    monkeypatch.setattr(create_set_structure, "_load_struct", lambda: _struct(tp_items),
                        raising=False)
    monkeypatch.setattr(create_set_structure, "_slepok_key", lambda s: "kuderko", raising=False)


def test_ct0000_node_yields_exactly_its_own_group(monkeypatch):
    """Узел с ОДНОЙ группой на ct0000 → ровно одна unit-строка, имя из структуры."""
    _patch_struct(monkeypatch, [
        {"t": "Агрегаторы", "gc": "ct0000_aon_n000_r0000_ct001_ag011_g00", "gk": "агрегаторы_d"},
        {"t": "Агрегаторы Моб", "gc": "ct0000_aoff_n000_r0000_ct001_ag011_g00",
         "gk": "агрегаторы_моб"},
    ])

    units = create_set_tp1_builders._struct_ct0000_units(
        "kuderko", "С пробегом", "tp1", {"агрегаторы_d"})

    assert units == [("ct0000", "агрегаторы_d", "Агрегаторы")]


def test_ct0000_units_ignore_model_ct_items(monkeypatch):
    """Обычные ct (модель-ct) в этот перечень не попадают — их ведёт `_struct_items`/only_cts."""
    _patch_struct(monkeypatch, [
        {"t": "Lada", "gc": "ct0183_aoff_n000_r0000_ct001_ag011_g00", "gk": "lada"},
        {"t": "Агрегаторы", "gc": "ct0000_aon_n000_r0000_ct001_ag011_g00", "gk": "агрегаторы_d"},
    ])

    units = create_set_tp1_builders._struct_ct0000_units(
        "kuderko", "С пробегом", "tp1", {"lada", "агрегаторы_d"})

    assert units == [("ct0000", "агрегаторы_d", "Агрегаторы")]


def test_ct0000_units_empty_without_only_gks(monkeypatch):
    """Нет camp_names-маршрута (only_gks пуст) → перечня нет, поведение прежнее."""
    _patch_struct(monkeypatch, [
        {"t": "Агрегаторы", "gc": "ct0000_aon_n000_r0000_ct001_ag011_g00", "gk": "агрегаторы_d"},
    ])

    assert create_set_tp1_builders._struct_ct0000_units(
        "kuderko", "С пробегом", "tp1", set()) == []
    assert create_set_tp1_builders._struct_ct0000_units(
        "kuderko", "С пробегом", "tp1", None) == []


def test_ct0000_units_empty_for_other_tp(monkeypatch):
    """Перечень строго по tp_code кампании."""
    _patch_struct(monkeypatch, [
        {"t": "Агрегаторы", "gc": "ct0000_aon_n000_r0000_ct001_ag011_g00", "gk": "агрегаторы_d"},
    ])

    assert create_set_tp1_builders._struct_ct0000_units(
        "kuderko", "С пробегом", "tp5", {"агрегаторы_d"}) == []


def test_ct0000_units_survive_unreadable_structure(monkeypatch):
    """Сбой чтения структуры не роняет создание — просто пустой перечень."""
    def _boom():
        raise RuntimeError("structure unavailable")

    monkeypatch.setattr(create_set_structure, "_load_struct", _boom, raising=False)
    monkeypatch.setattr(create_set_structure, "_slepok_key", lambda s: "kuderko", raising=False)

    assert create_set_tp1_builders._struct_ct0000_units(
        "kuderko", "С пробегом", "tp1", {"агрегаторы_d"}) == []


def test_whole_pack_branch_is_gated_by_ct0000_node_marker():
    """Ветка «весь пак» в `_build_tp1_from_pack` обязана стоять ПОСЛЕ ct0000-маркера.

    Функция DI-тяжёлая (Grid/v5/M3) и в unit-тесте целиком не поднимается — проверяем по
    исходнику, что маркер узла (`_og` задан, `_oc` пуст, модель-items нет) отсекает
    `sorted(pack.keys())`, а обычные ct (`_oc` непустой) идут прежним путём.
    """
    src = inspect.getsource(create_set_tp1_builders._build_tp1_from_pack)

    assert "_ct0_units = (_struct_ct0000_units(slepok, site_type, tp_code, _og)" in src
    assert "if (_og is not None and _oc is None and not _items) else [])" in src
    # «весь пак» остаётся только в else-ветке маркера
    assert src.count("_pk = sorted(pack.keys())") == 1
    _idx_marker = src.index("_ct0_units = (_struct_ct0000_units")
    assert _idx_marker < src.index("_pk = sorted(pack.keys())")

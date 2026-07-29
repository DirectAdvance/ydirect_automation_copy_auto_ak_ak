"""Имя кампании tp6/tp7 берётся из слепка, а не пересобирается движком.

Решение Семёна 2026-07-28: «имена tp7 движок должен брать из слепка так же как и имена тп6»
(и tp6 тоже — он страдал тем же). До правки `_build_name` собирал человекочитаемую часть из
очищенной категории + ХВОСТА, вычисленного по содержимому пака, и имя расходилось со структурой:

    в слепке «ТК - Общие запросы - Автотаргетинг»   → создавалось «ТК - Общее - Автотаргетинг»
    в слепке «ТК - Haval - КС»                      → создавалось «ТК - Haval - КС + Автотаргетинг»
    в слепке «ТК - Автокредит - Интересы»           → создавалось «ТК - Автокредит - Аудитории + Автотаргетинг»

Кодер, регион и метка фида собираются движком как раньше — из слепка берётся только имя.
"""

import pytest

from direct.create.create_set_plan import _build_name


def _name(**kw):
    base = dict(is_master=False, is_autotarget=True, pay="tcpa", r_code="r0002",
                oblast="Москва и область", sq="site", ct="ct0000")
    base.update(kw)
    return _build_name(**base)


@pytest.mark.parametrize("struct", [
    "ТК - Общие запросы - Автотаргетинг",
    "ТК - Haval - КС",
    "ТК - Автокредит - Интересы",
])
def test_struct_name_is_used_verbatim(struct):
    """Человекочитаемая часть = имя позиции слепка, включая хвост таргетинга."""
    got = _name(cat="Общее", targeting_label="Аудитории + Автотаргетинг", struct_name=struct)
    assert got.endswith(f" — {struct} - Москва и область")


def test_computed_tail_does_not_override_slepok_tail():
    """Хвост НЕ пересчитывается: «КС» из слепка не превращается в «КС + Автотаргетинг»."""
    got = _name(cat="Haval", targeting_label="КС + Автотаргетинг", struct_name="ТК - Haval - КС")
    assert "КС + Автотаргетинг" not in got
    assert got.endswith(" — ТК - Haval - КС - Москва и область")


def test_coder_prefix_and_region_still_built_by_engine():
    """Из слепка берётся ТОЛЬКО имя: кодер и регион по-прежнему собирает движок."""
    got = _name(ct="ct0111", struct_name="ТК - Haval - КС")
    assert got.startswith("tp7_cpc_site_ct0111_aon_n000_r0002_ct010_ag001_g00 — ")
    assert got.endswith(" - Москва и область")


def test_tp_label_prepended_when_slepok_name_lacks_it():
    """Позиция без «МК»/«ТК» в имени получает метку — её парсит UI-бейдж."""
    assert " — ТК - Общая - Москва и область" in _name(struct_name="Общая")
    assert " — МК - Общая - Москва и область" in _name(is_master=True, struct_name="Общая")


def test_existing_tp_label_is_not_duplicated():
    got = _name(struct_name="ТК - Общая")
    assert got.count("ТК") == 1


def test_without_struct_name_old_composition_is_kept():
    """Фолбэк для позиций без имени в структуре — прежняя сборка из категории и хвоста."""
    got = _name(cat="Haval", targeting_label="КС + Автотаргетинг")
    assert got.endswith(" — ТК - Haval - КС + Автотаргетинг - Москва и область")


def test_blank_struct_name_falls_back_too():
    assert _name(cat="Haval", targeting_label="Автотаргетинг", struct_name="   ") == \
        _name(cat="Haval", targeting_label="Автотаргетинг")

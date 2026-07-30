"""Регрессия X3_KS_VARIANT_KEEPS_AUTOTARGET_ONLY_GROUP (2026-07-30).

Правило Семёна: х3-кампания обязана нести группы С КЛЮЧАМИ и порождает три варианта —
«только автотаргетинг», «КС + автотаргетинг», «только ключевые слова».

Раньше все три варианта получали ОДИН список групп, поэтому группа без ключей
(в паке у неё только маркер `---autotargeting`) попадала и в чистый «КС», где автотаргет
выключен: ни ключей, ни автотаргета — группа не таргетирует ничего.
Живой случай: `porg-rgwzgo57`, `ct0000_aoff_…— Автотаргет` в кампании
«РСЯ - Комби - Общие запросы - КС».
"""
from __future__ import annotations

import pytest

from direct.create import create_set_plan as csp


@pytest.fixture()
def pack(monkeypatch):
    """Подменяем чтение пака: фразы есть только у части групп."""
    data = {
        "с_ключами": ["купить авто", "цена авто"],
        "тоже_с_ключами": ["авто в наличии"],
        "автотаргет": ["---autotargeting"],       # маркер = ключей НЕТ
        "пустая": [],
    }

    def _read(site_type, tp, ct, slepok, group=""):
        return {"positive": list(data.get(group, [])), "minus": []}

    # Патчим САМ модуль пака: `from .. import kontent_pack` берёт атрибут пакета,
    # подмена в sys.modules сюда не доходит.
    from direct import kontent_pack as kp
    monkeypatch.setattr(kp, "read_keywords", _read)
    return data


def test_keyword_groups_drop_autotarget_only(pack):
    gks = ["с_ключами", "автотаргет", "тоже_с_ключами", "пустая"]
    cts = ["ct0001", "ct0000", "ct0002", "ct0003"]
    keep_g, keep_c = csp._x3_keyword_groups("slepok", "Мультибренд", gks, cts)
    assert keep_g == ["с_ключами", "тоже_с_ключами"]
    assert keep_c == ["ct0001", "ct0002"], "ct обязан оставаться в паре со своей группой"


def test_marker_only_group_is_not_a_keyword_group(pack):
    keep_g, _ = csp._x3_keyword_groups("slepok", "Мультибренд", ["автотаргет"], ["ct0000"])
    assert keep_g == [], "`---autotargeting` — маркер автотаргета, а не ключевая фраза"


def test_all_groups_have_keywords_nothing_dropped(pack):
    gks = ["с_ключами", "тоже_с_ключами"]
    keep_g, keep_c = csp._x3_keyword_groups("slepok", "Мультибренд", gks, ["ct0001", "ct0002"])
    assert keep_g == gks and keep_c == ["ct0001", "ct0002"]


def test_empty_input_passthrough(pack):
    assert csp._x3_keyword_groups("slepok", "Мультибренд", [], []) == ([], [])


def test_pack_failure_is_fail_open(monkeypatch):
    """Сбой чтения пака НЕ должен молча выкидывать группы."""
    def _boom(*a, **k):
        raise RuntimeError("пак недоступен")

    from direct import kontent_pack as kp
    monkeypatch.setattr(kp, "read_keywords", _boom)
    gks = ["a", "b"]
    keep_g, keep_c = csp._x3_keyword_groups("slepok", "Мультибренд", gks, ["ct1", "ct2"])
    assert keep_g == gks, "при сбое чтения группы обязаны остаться (fail-open)"


def test_x3_variants_contract_unchanged():
    """Три варианта и их флаги — ровно как задал Семён."""
    from direct.create.create_set_structure import X3_VARIANTS
    got = [(v["suffix"], v["autotarget"], v["keep_keywords"]) for v in X3_VARIANTS]
    assert got == [
        ("КС", False, True),                      # только ключевые слова
        ("Автотаргетинг", True, False),           # только автотаргетинг
        ("КС + Автотаргетинг", True, True),       # КС + автотаргетинг
    ]


def test_ks_variant_is_the_only_one_filtered():
    """Фильтр применяется ТОЛЬКО к варианту без автотаргета."""
    import inspect
    src = inspect.getsource(csp)
    assert '_ks_only = not _var["autotarget"]' in src
    assert "only_gks=(_kw_gks if _ks_only else None)" in src, (
        "варианты с автотаргетом обязаны получать полный список групп"
    )

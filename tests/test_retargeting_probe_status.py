"""Пустая карта условий ретаргетинга — два РАЗНЫХ случая, и их нельзя путать.

Решение Семёна 2026-07-28. До правки `_account_retargeting` возвращал `{}` и когда кабинет
прочитан, но условий в нём нет (штатно — «если нет в кабинете, то не добавляем в кампании»),
и когда карту прочитать не удалось (нет токена / запрос упал). Оркестратор в обоих случаях
писал потерю `audiences_not_sent` и поднимал гейт `has_issues` — джоба на штатном аккаунте
получала ⚠️ на ровном месте (живой пример: porg-uy3huxcn, 100 групп, ретаргетинга в кабинете 0).
"""

import pytest

from direct.create import create_set_corrections as csc


@pytest.fixture()
def v5(monkeypatch):
    """Подменяем DI-шный `_v5_get`; вызовы записываем, ответ задаёт тест."""
    calls = []

    def _set(response=None, exc=None):
        def _fake(service, token, login, fields, **kw):
            calls.append((service, token, login, tuple(fields)))
            if exc is not None:
                raise exc
            return response or {}
        monkeypatch.setattr(csc, "_v5_get", _fake, raising=False)
    _set.calls = calls
    return _set


def test_no_token_is_reported_as_no_token(v5):
    v5(response={"result": {"RetargetingLists": [{"Id": 1, "Name": "a"}]}})
    assert csc.account_retargeting_probe("", "porg-x") == ({}, "no_token")
    assert v5.calls == []                      # без токена в API не ходим


def test_empty_cabinet_is_ok_not_failure(v5):
    """Кабинет прочитан, условий нет → статус ok. Это НЕ потеря."""
    v5(response={"result": {"RetargetingLists": []}})
    assert csc.account_retargeting_probe("tok", "porg-uy3huxcn") == ({}, "ok")


def test_missing_result_key_is_ok_too(v5):
    """Ответ без `result` — тоже успешное чтение пустого кабинета, а не сбой."""
    v5(response={})
    assert csc.account_retargeting_probe("tok", "porg-x") == ({}, "ok")


def test_api_failure_is_reported_as_error(v5):
    v5(exc=RuntimeError("152 units exhausted"))
    got_map, status = csc.account_retargeting_probe("tok", "porg-x")
    assert got_map == {}
    assert status.startswith("error:")
    assert "152" in status                     # причина видна в статусе, а не теряется


def test_conditions_are_mapped_by_name(v5):
    v5(response={"result": {"RetargetingLists": [
        {"Id": 11, "Name": "Автокредит"},
        {"Id": 12, "Name": "LAL"},
        {"Id": 13},                            # без имени — матчить не по чему, пропускаем
    ]}})
    got_map, status = csc.account_retargeting_probe("tok", "porg-x")
    assert status == "ok"
    assert got_map == {"Автокредит": 11, "LAL": 12}


def test_legacy_wrapper_still_returns_only_the_map(v5):
    """Старые вызывающие получают карту без статуса — совместимость не сломана."""
    v5(response={"result": {"RetargetingLists": [{"Id": 7, "Name": "seg"}]}})
    assert csc._account_retargeting("tok", "porg-x") == {"seg": 7}
    v5(exc=RuntimeError("boom"))
    assert csc._account_retargeting("tok", "porg-x") == {}

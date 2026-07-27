"""Метрика проверяется на шаге ПЛАНА (_metrika_alert_for), а не только при создании.

Проверка переиспользует ту же create_set_metrika.prepare_metrika, что и оркестратор создания,
поэтому тесты фиксируют оба легальных случая: optional (via_cookie+no_cpa) и автоподтягивание
цели по счётчику.
"""
import logging

from direct import create_set_plan


def _wire(monkeypatch, *, counters=None, table_goal=None, resolved_goal=None, owner=None):
    """Инъекция трёх коллбэков prepare_metrika в globals модуля плана (как это делает configure)."""
    monkeypatch.setattr(create_set_plan, "_metrika_goals_for",
                        lambda _login: {"counters": list(counters or []), "goal_id": table_goal},
                        raising=False)
    monkeypatch.setattr(create_set_plan, "_goal_vse_formy",
                        lambda _counter: (resolved_goal, "Все формы" if resolved_goal else None),
                        raising=False)
    monkeypatch.setattr(create_set_plan, "_counter_foreign_owner",
                        lambda _counter, _login: owner, raising=False)


def test_counter_and_goal_present_no_alert(monkeypatch):
    """(а) счётчик+цель есть → needed=False."""
    _wire(monkeypatch)
    alert = create_set_plan._metrika_alert_for(
        "porg-test", {"counter_id": "109986170", "goal_id": "579905467"})
    assert alert == {"needed": False, "error": None, "counter_id": 109986170,
                     "goal_id": 579905467, "metrika_note": None}


def test_missing_goal_not_optional_raises_alert(monkeypatch):
    """(б) цели нет и режим НЕ optional → needed=True с текстом ошибки."""
    _wire(monkeypatch, resolved_goal=None)
    alert = create_set_plan._metrika_alert_for(
        "porg-test", {"counter_id": "109986170", "goal_id": ""})
    assert alert["needed"] is True
    assert alert["error"] == "укажите цель (goal_id)"
    assert alert["counter_id"] == 109986170
    assert alert["goal_id"] == 0


def test_via_cookie_no_cpa_is_optional(monkeypatch):
    """(в) via_cookie+no_cpa без метрики → needed=False + metrika_note."""
    _wire(monkeypatch, resolved_goal=None)
    alert = create_set_plan._metrika_alert_for(
        "porg-test", {"counter_id": "", "goal_id": "", "via_cookie": True, "n": True})
    assert alert["needed"] is False
    assert alert["metrika_note"] and "via_cookie+no_cpa" in alert["metrika_note"]


def test_goal_autoresolved_from_counter_no_alert(monkeypatch):
    """(г) счётчик есть, цель автоподтянулась goal_vse_formy → needed=False."""
    _wire(monkeypatch, resolved_goal=579905467)
    alert = create_set_plan._metrika_alert_for(
        "porg-test", {"counter_id": "109986170", "goal_id": ""})
    assert alert["needed"] is False
    assert alert["goal_id"] == 579905467
    assert alert["error"] is None


def test_foreign_counter_owner_raises_alert(monkeypatch):
    """Чужой владелец счётчика — тоже алерт плана, а не отказ при создании."""
    _wire(monkeypatch, resolved_goal=579905467, owner="porg-other")
    alert = create_set_plan._metrika_alert_for(
        "porg-test", {"counter_id": "109986170", "goal_id": ""})
    assert alert["needed"] is True
    assert "porg-other" in alert["error"]


def test_missing_deps_do_not_block_plan(monkeypatch):
    """Проводки коллбэков нет → план не блокируем (гейт создания остаётся в оркестраторе)."""
    monkeypatch.delattr(create_set_plan, "_metrika_goals_for", raising=False)
    monkeypatch.delattr(create_set_plan, "_goal_vse_formy", raising=False)
    monkeypatch.delattr(create_set_plan, "_counter_foreign_owner", raising=False)
    assert create_set_plan._metrika_alert_for("porg-test", {"counter_id": "1"})["needed"] is False


def test_metrika_failure_does_not_break_plan(monkeypatch):
    """Сбой Метрики/БД → план обязан посчитаться (needed=False), а не упасть 500."""
    def _boom(*_args, **_kwargs):
        raise RuntimeError("metrika down")

    monkeypatch.setattr(create_set_plan, "_metrika_goals_for", _boom, raising=False)
    monkeypatch.setattr(create_set_plan, "_goal_vse_formy", _boom, raising=False)
    monkeypatch.setattr(create_set_plan, "_counter_foreign_owner", _boom, raising=False)
    assert create_set_plan._metrika_alert_for("porg-test", {})["needed"] is False


def test_metrika_failure_is_logged_with_login_and_traceback(monkeypatch, caplog):
    """Fail-open обязан быть ВИДИМЫМ: без лога «плашки нет» неотличимо от «метрика в порядке»,
    а разовый сбой Victory — от постоянного. Нужны логин и трейс."""
    def _boom(*_args, **_kwargs):
        raise RuntimeError("metrika down")

    monkeypatch.setattr(create_set_plan, "_metrika_goals_for", _boom, raising=False)
    monkeypatch.setattr(create_set_plan, "_goal_vse_formy", _boom, raising=False)
    monkeypatch.setattr(create_set_plan, "_counter_foreign_owner", _boom, raising=False)
    with caplog.at_level(logging.WARNING, logger="direct.plan"):
        assert create_set_plan._metrika_alert_for("porg-logged", {})["needed"] is False
    recs = [r for r in caplog.records if r.name == "direct.plan"]
    assert len(recs) == 1
    assert recs[0].levelno == logging.WARNING
    assert "porg-logged" in recs[0].getMessage()
    assert recs[0].exc_info is not None          # exc_info=True → трейс причины в journald

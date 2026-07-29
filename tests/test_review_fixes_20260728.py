"""Две находки код-ревью 2026-07-28.

1. Гейт «создано групп ≠ отправлено» отсутствовал в ТОКЕННОМ пути tp1: куки-путь идёт через
   `grid_create.create_full` (гейт есть), а `_build_tp1_adgroups` рапортовал только `adgroups`.
   Верификатор сравнивает `build["groups"]` с `build["groups_expected"]` — ни того, ни другого
   ключа не было, поэтому потеря групп уходила молча: не срабатывал ни
   GROUPS_CREATED_LESS_THAN_SENT, ни NO_ADGROUPS_REPORTED.

2. Кэш именованных наборов минус-фраз был процессным: закэшированный неуспех (транзиентный сбой
   v5) жил до рестарта и отдавался всем последующим прогонам того же аккаунта без обращения к API.
"""

import pytest

from direct.create import create_set_minus as csm
from direct.grid_create import _gate_groups_created
from direct.local_result_verifier import verify_local_result


def _codes(build: dict) -> set:
    """Коды находок верификатора для одной созданной tp1-кампании."""
    row = {"name": "tp1 — РСЯ", "result": {"campaign_id": 1, "tp1_build": build}}
    return {i.get("code") for i in (verify_local_result(row) or [])}


# ── 1. Гейт групп ────────────────────────────────────────────────────────────────────────────
def test_gate_reports_expected_and_shortfall():
    rep = {"groups": 13}
    _gate_groups_created(rep, 14)
    assert rep["groups_expected"] == 14
    assert rep["groups_shortfall"] == 1
    assert any("создано 13 из 14" in w for w in rep["warnings"])


def test_gate_silent_when_counts_match():
    rep = {"groups": 14}
    _gate_groups_created(rep, 14)
    assert rep["groups_expected"] == 14
    assert "groups_shortfall" not in rep
    assert not rep.get("warnings")


def test_verifier_now_sees_group_shortfall():
    """Ровно то, что раньше уходило молча: 13 групп из 14 отправленных."""
    assert "GROUPS_CREATED_LESS_THAN_SENT" in _codes(
        {"groups": 13, "groups_expected": 14, "ads": 20})


def test_verifier_silent_without_the_keys():
    """Контроль: без ключей (поведение ДО правки) верификатор слеп — находка была реальной."""
    assert "GROUPS_CREATED_LESS_THAN_SENT" not in _codes({"adgroups": 13, "ads": 20})


def test_verifier_quiet_when_nothing_lost():
    assert "GROUPS_CREATED_LESS_THAN_SENT" not in _codes(
        {"groups": 14, "groups_expected": 14, "ads": 20})


# ── 2. Кэш минус-наборов ─────────────────────────────────────────────────────────────────────
@pytest.fixture()
def ensure_calls(monkeypatch):
    """Считаем реальные обращения к ensure_named_minus_sets."""
    calls = []

    def _fake(token, login, slepok, site_type, city="", region=""):
        calls.append((login, slepok, site_type))
        return {"ok": len(calls) > 1, "ids": [len(calls)], "errors": []}

    monkeypatch.setattr(csm, "ensure_named_minus_sets", _fake, raising=False)
    monkeypatch.setattr(csm, "_SLEPOK_KEY", {}, raising=False)
    with csm._NAMED_SETS_LOCK:
        csm._NAMED_SETS_CACHE.clear()
    return calls


def test_same_job_hits_cache_once(ensure_calls):
    """Внутри одной джобы набор создаётся ОДИН раз — защита от дублей не сломана."""
    for _ in range(3):
        csm.ensure_named_minus_sets_cached("tok", "porg-x", "kuderko", "Мультибренд", job_id="j1")
    assert len(ensure_calls) == 1


def test_next_job_retries_after_failure(ensure_calls):
    """Неуспех НЕ переживает джобу: следующий прогон обращается к API заново."""
    first = csm.ensure_named_minus_sets_cached("tok", "porg-x", "kuderko", "Мультибренд", job_id="j1")
    assert first["ok"] is False
    second = csm.ensure_named_minus_sets_cached("tok", "porg-x", "kuderko", "Мультибренд", job_id="j2")
    assert second["ok"] is True
    assert len(ensure_calls) == 2


def test_different_accounts_are_separate_within_one_job(ensure_calls):
    csm.ensure_named_minus_sets_cached("tok", "porg-a", "kuderko", "Мультибренд", job_id="j1")
    csm.ensure_named_minus_sets_cached("tok", "porg-b", "kuderko", "Мультибренд", job_id="j1")
    assert len(ensure_calls) == 2


def test_cache_table_is_bounded(ensure_calls):
    for i in range(csm._NAMED_SETS_CACHE_MAX + 40):
        csm.ensure_named_minus_sets_cached("tok", "porg-x", "kuderko", "Мультибренд", job_id=f"j{i}")
    assert len(csm._NAMED_SETS_CACHE) <= csm._NAMED_SETS_CACHE_MAX


def test_empty_job_id_keeps_process_wide_behaviour(ensure_calls):
    """Внешние вызовы без job_id ведут себя как раньше — кэш на процесс."""
    csm.ensure_named_minus_sets_cached("tok", "porg-x", "kuderko", "Мультибренд")
    csm.ensure_named_minus_sets_cached("tok", "porg-x", "kuderko", "Мультибренд")
    assert len(ensure_calls) == 1

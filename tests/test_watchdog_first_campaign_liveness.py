"""Сторож «нет первой кампании» обязан смотреть на признаки работы, а не только на счётчик.

Баг 2026-07-28: живой прогон porg-pl6iavd5 (42 кампании, потоковый ИИ-режим) убит через 15 минут
с текстом «не создана ни одна кампания», хотя в этот момент грузил картинки tp2 (в логе десятки
строк images-dedup / img-cache MISS->upload) и одну кампанию уже создал. На свежем аккаунте
генерация контента и первичная заливка картинок законно занимают минуты — «кампаний ещё нет»
не равно «зависло».
"""

import time

import pytest

from direct import queue_server as qs
from direct import stage_timing as st


@pytest.fixture()
def watchdog_env(monkeypatch):
    """Сторож в изоляции: без БД, без кросс-процессных гейтов."""
    for name in ("_job_db_save", "_jobs_db_mark_stale_running", "_agency_gate_sweep",
                 "_agency_gate_release", "_schedule_delayed_content_repair_after_done"):
        monkeypatch.setattr(qs, name, lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(qs, "_CREATE_JOBS", {}, raising=False)
    monkeypatch.setattr(qs, "_CREATE_QUEUE", [], raising=False)
    monkeypatch.setattr(qs, "_CREATE_ACTIVE_AGENCIES", {}, raising=False)
    with st._PROGRESS_LOCK:
        st._PROGRESS.clear()
    return qs


def _job(*, started_ago: float, heartbeat_ago: float | None, created: int = 0, total: int = 42):
    now = time.time()
    return {
        "status": "running", "kind": "set", "created": created, "failed": 0,
        "done": 0, "total": total, "started_at": now - started_ago,
        "_heartbeat": (now - heartbeat_ago) if heartbeat_ago is not None else 0,
        "body": {"agency": "victoryagency14"},
    }


def test_stage_progress_keeps_job_alive(watchdog_env):
    """Идут стадии создания (Grid/v501/картинки) → бюджет прошёл, но джобу НЕ убиваем."""
    qs._CREATE_JOBS["j1"] = _job(started_ago=25 * 60, heartbeat_ago=None)
    st.note_progress("j1")                                  # только что была стадия
    qs._create_watchdog_tick()
    assert qs._CREATE_JOBS["j1"]["status"] == "running"


def test_item_heartbeat_keeps_job_alive(watchdog_env):
    """Второй источник признака жизни — обработанные item'ы (_heartbeat)."""
    qs._CREATE_JOBS["j2"] = _job(started_ago=25 * 60, heartbeat_ago=30)
    qs._create_watchdog_tick()
    assert qs._CREATE_JOBS["j2"]["status"] == "running"


def test_silent_job_without_campaigns_is_killed(watchdog_env):
    """Бюджет прошёл И тишина дольше порога → это зависание, убиваем."""
    qs._CREATE_JOBS["j3"] = _job(started_ago=25 * 60, heartbeat_ago=10 * 60)
    qs._create_watchdog_tick()
    job = qs._CREATE_JOBS["j3"]
    assert job["status"] == "error"
    assert "признаков работы" in job["error"]
    assert job["result"]["first_campaign_timeout"] is True


def test_silence_alone_before_budget_does_not_kill(watchdog_env):
    """Порог по времени остался нижней границей: раньше него не убиваем даже в тишине."""
    qs._CREATE_JOBS["j4"] = _job(started_ago=10 * 60, heartbeat_ago=9 * 60)
    qs._create_watchdog_tick()
    assert qs._CREATE_JOBS["j4"]["status"] == "running"


def test_created_campaign_disables_this_watchdog(watchdog_env):
    """Кампании уже пошли → этот сторож не его случай, даже в тишине."""
    qs._CREATE_JOBS["j5"] = _job(started_ago=25 * 60, heartbeat_ago=10 * 60, created=1)
    qs._create_watchdog_tick()
    assert qs._CREATE_JOBS["j5"]["status"] == "running"


def test_note_progress_is_per_job_and_survives_bad_input():
    st.note_progress("jobA")
    assert st.last_progress("jobA") > 0
    assert st.last_progress("jobB") == 0.0
    st.note_progress(None)                                  # мусор не роняет замер
    st.note_progress("")
    assert st.last_progress(None) == 0.0


def test_progress_table_is_bounded():
    """Таблица отметок не растёт бесконечно — старые вычищаются."""
    with st._PROGRESS_LOCK:
        st._PROGRESS.clear()
    for i in range(st._PROGRESS_MAX + 50):
        st.note_progress(f"job{i}")
    assert len(st._PROGRESS) <= st._PROGRESS_MAX
    assert st.last_progress(f"job{st._PROGRESS_MAX + 49}") > 0    # свежая отметка на месте

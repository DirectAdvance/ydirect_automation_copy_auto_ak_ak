from pathlib import Path


def test_copy_main_uses_current_jobs_table_for_copy_api():
    source = Path(__file__).resolve().parents[1].joinpath("copy_main.py").read_text(encoding="utf-8")

    assert "FROM direct_automation.jobs" in source
    assert "FROM public.direct_automation_jobs" not in source


def test_copy_queue_row_exposes_original_user_for_agent_board_retry(monkeypatch):
    """Retry-джоба идёт от 'agent-board-auto'; автор исходного запуска обязан доехать до UI."""
    from direct import copy_main
    from direct.core import direct_repository

    rows = [
        {
            "job_id": "retry1", "login": "porg-target", "status": "running",
            "total": 3, "done": 1, "created": 1, "failed": 0, "error": "", "created_at": None,
            "body": {
                "source_login": "porg-source", "target_login": "porg-target",
                "created_by": "agent-board-auto", "_copy_retry_original_user": "Ilyin",
            },
            "has_issues": None, "has_issues_unknown": False,
        },
        {
            "job_id": "manual1", "login": "porg-other", "status": "done",
            "total": 1, "done": 1, "created": 1, "failed": 0, "error": "", "created_at": None,
            "body": {"source_login": "porg-src2", "target_login": "porg-other", "created_by": "terehov"},
            "has_issues": None, "has_issues_unknown": False,
        },
    ]

    class FakeCursor:
        def execute(self, *_args, **_kwargs):
            return None

        def fetchall(self):
            return rows

        def close(self):
            return None

    class FakeConn:
        def cursor(self, **_kwargs):
            return FakeCursor()

        def close(self):
            return None

    monkeypatch.setattr(copy_main, "_copy_queue_allowed_directologists", lambda: None)
    monkeypatch.setattr(direct_repository, "victory_conn", lambda *a, **k: FakeConn())

    out = {job["job_id"]: job for job in copy_main._copy_queue_jobs()}

    assert out["retry1"]["created_by"] == "agent-board-auto"
    assert out["retry1"]["original_created_by"] == "Ilyin"
    # Ручной запуск не должен получить дубль самого себя во второй колонке.
    assert out["manual1"]["created_by"] == "terehov"
    assert out["manual1"]["original_created_by"] == ""

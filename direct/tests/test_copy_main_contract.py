from pathlib import Path


def test_copy_main_uses_current_jobs_table_for_copy_api():
    source = Path(__file__).resolve().parents[1].joinpath("copy_main.py").read_text(encoding="utf-8")

    assert "FROM direct_automation.jobs" in source
    assert "FROM public.direct_automation_jobs" not in source

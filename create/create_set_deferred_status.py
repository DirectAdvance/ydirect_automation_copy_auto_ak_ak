"""Deferred-row terminal status helpers for create_set resume flows."""
from __future__ import annotations


def parent_deferred_status_after_resume(*, created: int, failed: int) -> tuple[str, str]:
    """Return status/note for a resumed direct_deferred_creates parent row."""
    failed_i = int(failed or 0)
    created_i = int(created or 0)
    if failed_i > 0:
        return "error", f"докрутка по куке завершилась с ошибками: создано {created_i}, не создано {failed_i}"
    return "done", f"докручено по куке: создано {created_i}, не создано 0"

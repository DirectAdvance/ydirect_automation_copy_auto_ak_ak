"""Terminal status decisions for create queue jobs."""
from __future__ import annotations

from typing import Any


CREATE_JOB_KINDS = {"set", "slepok"}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def create_failed_error(failed: int, *, prefix: str = "создание завершилось с ошибками") -> str:
    return f"{prefix}: не создано {_as_int(failed)}"


def terminal_status_for_job(
    kind: str | None,
    data: dict[str, Any] | None,
    *,
    cancelled: bool = False,
) -> tuple[str, str | None]:
    """Return terminal status/error for worker result.

    For create-set jobs, partial failures must be red terminal state. Copy/delete
    keep their existing semantics because their UI already presents partial counts.
    """
    if cancelled:
        return "cancelled", None
    if isinstance(data, dict) and data.get("error"):
        return "error", str(data.get("error") or "")[:500]
    failed = _as_int((data or {}).get("failed") if isinstance(data, dict) else 0)
    if (kind or "") in CREATE_JOB_KINDS and failed > 0:
        return "error", create_failed_error(failed)
    return "done", None


def terminal_status_for_parent_failed(failed: int) -> tuple[str, str | None]:
    failed_i = _as_int(failed)
    if failed_i > 0:
        return "error", create_failed_error(failed_i, prefix="докрутка завершилась с ошибками")
    return "done", None

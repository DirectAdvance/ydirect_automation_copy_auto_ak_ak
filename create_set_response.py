"""Response payload helpers for create_set."""
from __future__ import annotations

from typing import Any


def build_create_set_response(*, created: int, failed: int, launch: bool,
                              results: list[dict[str, Any]], promo_note: str | None,
                              callouts_note: str | None, units_block: bool,
                              units_switched: bool, units_note: str | None,
                              units_pending: int, deferred_id: str | None,
                              deferred_at: str | None, auto_cookie_job_id: str | None = None,
                              content_source: str | None = None,
                              slepok_content_note: str | None = None,
                              metrika_note: str | None = None,
                              verification: dict[str, Any] | None = None,
                              live_verification: dict[str, Any] | None = None,
                              precreate_report: dict[str, Any] | None = None,
                              prepare_report: dict[str, Any] | None = None,
                              repair_gate_summary: dict[str, Any] | None = None,
                              auto_repair: dict[str, Any] | None = None,
                              skipped_existing: int = 0,
                              losses: list[dict[str, Any]] | None = None,
                              chan_A_wall_sec: float | None = None,
                              chan_B_wall_sec: float | None = None) -> dict[str, Any]:
    """Return the public create_set JSON payload."""
    return {
        "created": created,
        "failed": failed,
        "skipped_existing": skipped_existing,
        "launched": launch,
        "results": results,
        "promo": promo_note,
        "callouts": callouts_note,
        "units_exhausted": (units_block or units_switched),
        "units_note": units_note,
        "units_pending": units_pending if units_block else 0,
        "deferred_id": deferred_id,
        "deferred_at": deferred_at,
        "auto_cookie_job_id": auto_cookie_job_id,
        "content_source": (content_source or None),
        "slepok_content": slepok_content_note,
        "metrika_note": metrika_note,
        "verification": verification,
        "live_verification": live_verification,
        "precreate": precreate_report,
        "prepare": prepare_report,
        "repair_gate": repair_gate_summary,
        "auto_repair": auto_repair,
        # ПОТЕРИ прогона: часть плана НЕ доехала в кабинет (аудитории не отправлены, минус-фразы
        # не влезли/разошлись с кабинетом, наборы не привязались). Верификаторы это не ловят —
        # они сверяют созданное. Ключ читает create_job_status.compute_job_issues_breakdown и
        # поднимает has_issues, иначе такая джоба оставалась ЗЕЛЁНОЙ.
        "losses": list(losses or []),
        # Wall-clock таймеры параллельных каналов. None при DIRECT_PARALLEL_CHANNELS=0.
        # Базовая точка: job 446ab5bd0ab3 = 2397s, 23 A-item, один токен-поток.
        # При двух суб-потоках ожидаемый выигрыш: chan_A_wall_sec ≈ 1200s (-50%).
        "chan_A_wall_sec": chan_A_wall_sec,
        "chan_B_wall_sec": chan_B_wall_sec,
    }

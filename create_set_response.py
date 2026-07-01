"""Response payload helpers for create_set."""
from __future__ import annotations

from typing import Any


def build_create_set_response(*, created: int, failed: int, launch: bool,
                              results: list[dict[str, Any]], promo_note: str | None,
                              callouts_note: str | None, units_block: bool,
                              units_switched: bool, units_note: str | None,
                              units_pending: int, deferred_id: str | None,
                              deferred_at: str | None, content_source: str | None,
                              slepok_content_note: str | None,
                              metrika_note: str | None, verification: dict[str, Any] | None,
                              live_verification: dict[str, Any] | None,
                              precreate_report: dict[str, Any] | None,
                              repair_gate_summary: dict[str, Any] | None,
                              auto_repair: dict[str, Any] | None) -> dict[str, Any]:
    """Return the public create_set JSON payload."""
    return {
        "created": created,
        "failed": failed,
        "launched": launch,
        "results": results,
        "promo": promo_note,
        "callouts": callouts_note,
        "units_exhausted": (units_block or units_switched),
        "units_note": units_note,
        "units_pending": units_pending if units_block else 0,
        "deferred_id": deferred_id,
        "deferred_at": deferred_at,
        "content_source": (content_source or None),
        "slepok_content": slepok_content_note,
        "metrika_note": metrika_note,
        "verification": verification,
        "live_verification": live_verification,
        "precreate": precreate_report,
        "repair_gate": repair_gate_summary,
        "auto_repair": auto_repair,
    }

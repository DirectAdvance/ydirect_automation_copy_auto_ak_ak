"""Callout result helpers for create_set."""
from __future__ import annotations

from typing import Any


def build_callouts_note(*, callouts: list[Any], precreated_callout_ids: list[int],
                        precreated_callouts_note: str | None) -> str | None:
    """Return response/verifier note for callouts prepared before campaign upload."""
    if not callouts:
        return None
    if precreated_callout_ids:
        return (
            f"подтверждено уточнений: {len(precreated_callout_ids)}/{len(callouts)} — "
            "готовые ids переданы в Grid-finalize/куки-пути"
        )
    if precreated_callouts_note:
        return ""
    return None

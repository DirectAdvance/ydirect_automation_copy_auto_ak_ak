"""Pre-create orchestration for Direct create_set."""
from __future__ import annotations

from typing import Any, Callable

from . import precreate as pcreate


def run_create_set_precreate(*, login: str, body: dict[str, Any], items: list[dict[str, Any]],
                             account: dict[str, Any], agent: str, site_type: str,
                             callouts: list[str], stream_content: bool,
                             existing_names_count: int, token: str | None, client: Any,
                             dedup_callouts: Callable[[list[str]], list[str]],
                             callout_cap: int, grid_client_factory: Any,
                             v5_get: Callable[..., dict[str, Any]],
                             promo_usable_for_content: Callable[..., tuple[bool, str]],
                             create_account_promo_from_slepok: Callable[..., tuple[int | None, str]],
                             selected_slepok_key: Callable[[str], str]) -> dict[str, Any]:
    """Build and execute precreate assets without letting failures break create_set."""
    try:
        report = pcreate.build_precreate_report(
            login=login,
            body={
                **body,
                "items": items,
                "agent": agent,
                "site_type": site_type,
                "callouts": callouts,
                "stream_content": stream_content,
            },
            items=items,
            account={**account, "site_type": site_type},
            existing_names_count=existing_names_count,
        )
    except Exception as e:  # noqa: BLE001 - precreate report must not break create_set
        report = {"status": "error", "error": str(e)[:200], "uses_direct_units": False}

    try:
        executed = pcreate.execute_precreate_assets(
            report=report,
            login=login,
            items=items,
            agent=agent,
            stream_content=stream_content,
            token=token,
            client=client,
            ctx=account,
            site_type=site_type,
            callouts=callouts,
            dedup_callouts=dedup_callouts,
            callout_cap=callout_cap,
            grid_client_factory=grid_client_factory,
            v5_get=v5_get,
            promo_usable_for_content=promo_usable_for_content,
            create_account_promo_from_slepok=create_account_promo_from_slepok,
            selected_slepok_key=selected_slepok_key,
        )
        return {
            "report": executed.get("report"),
            "promo_id": executed.get("promo_id"),
            "promo_note": executed.get("promo_note"),
            "promo_skipped": executed.get("promo_skipped") or [],
            "callout_ids": executed.get("callout_ids") or [],
            "callouts_note": executed.get("callouts_note"),
        }
    except Exception as e:  # noqa: BLE001 - precreate execution must not break create_set
        if isinstance(report, dict):
            report.setdefault("notes", []).append(f"precreate execution failed: {str(e)[:160]}")
        return {
            "report": report,
            "promo_id": None,
            "promo_note": None,
            "promo_skipped": [],
            "callout_ids": [],
            "callouts_note": None,
        }

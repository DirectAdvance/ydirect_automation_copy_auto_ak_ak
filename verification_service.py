"""Orchestration layer for read-only create_set live verification.

The service has no Flask dependency. Blueprint code supplies project-specific
callbacks for Grid campaign listing and Direct token lookup; this module wires
those sources into ``live_verifier`` and repair planning.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from . import campaign as cmc
from .campaign_result import created_campaigns
from .live_verifier import verify_live_create_set


GridCampaignsGetter = Callable[[str], list[dict[str, Any]]]
TokenGetter = Callable[[str, str], Optional[str]]


def _created_ids(results: list[dict[str, Any]], *, kind: Optional[str] = None) -> list[int]:
    ids: list[int] = []
    for row in created_campaigns(results or []):
        if kind is not None and row.get("kind") != kind:
            continue
        if kind is None and row.get("kind") == "uac":
            continue
        try:
            cid = int(row.get("id") or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid > 0:
            ids.append(cid)
    return ids


def verify_create_set_live(login: str, results: list[dict[str, Any]], *,
                           agency: str = "", use_v5: bool = False,
                           grid_campaigns_getter: Optional[GridCampaignsGetter] = None,
                           token_getter: Optional[TokenGetter] = None,
                           account_has_promo_library: Optional[bool] = None,
                           phase: str = "in_job") -> dict[str, Any]:
    """Run Grid-first live verification for create_set results.

    ``account_has_promo_library`` пробрасывается как есть в ``verify_live_create_set``
    (ступень 1 гейта ``PROMO_MISSING``); ``None`` = признак не передан → фолбэк на прокси.

    ``phase`` — фаза сверки «build ⇄ кабинет» (``in_job`` → недобор = warn, ``delayed`` → error).
    """
    login = (login or "").strip()
    if not login:
        return {"status": "error", "error": "login пустой", "prefer_grid": True}

    grid_rows = None
    v5_rows = None
    grid_content_counts = None
    uac_details = None
    live_errors: list[str] = []

    try:
        grid_rows = grid_campaigns_getter(login) if grid_campaigns_getter else None
    except Exception as e:  # noqa: BLE001
        live_errors.append(f"grid: {str(e)[:180]}")

    try:
        ids = _created_ids(results or [])
        if ids:
            from .grid_read import GridReadClient
            grid_content_counts = GridReadClient(login).campaign_content_counts(ids)
        else:
            grid_content_counts = {}
    except Exception as e:  # noqa: BLE001
        live_errors.append(f"grid-content: {str(e)[:180]}")

    try:
        uac_ids = _created_ids(results or [], kind="uac")
        if uac_ids:
            from .uac_read import UacReadClient, summarize_uac_detail
            raw_details = UacReadClient(login, agency=agency).campaign_details(uac_ids)
            uac_details = {cid: summarize_uac_detail(row) for cid, row in raw_details.items()}
        else:
            uac_details = {}
    except Exception as e:  # noqa: BLE001
        live_errors.append(f"uac-detail: {str(e)[:180]}")

    if use_v5:
        try:
            token = token_getter(login, agency) if token_getter else None
            if token:
                ids = _created_ids(results or [])
                if ids:
                    v5_rows = cmc.DirectV501Client(token, login).get_campaigns(
                        ids, ["Id", "Name", "Type", "State", "Status"]
                    )
                else:
                    v5_rows = []
            else:
                live_errors.append("v5: не найден агентский токен")
        except Exception as e:  # noqa: BLE001
            live_errors.append(f"v5: {str(e)[:180]}")

    try:
        live_report = verify_live_create_set(
            login=login,
            results=results or [],
            v5_campaigns=v5_rows,
            grid_campaigns=grid_rows,
            grid_content_counts=grid_content_counts,
            uac_details=uac_details,
            prefer_grid=True,
            account_has_promo_library=account_has_promo_library,
            phase=phase,
        )
        if live_errors:
            live_report.setdefault("issues", []).append(
                {"severity": "warn", "code": "LIVE_SOURCE_ERRORS", "messages": live_errors[:5]}
            )
            live_report["summary"]["issues"] += 1
            live_report["summary"]["warnings"] += 1
            if live_report["status"] == "pass":
                live_report["status"] = "warn"
            try:
                from .repair_planner import build_repair_plan
                live_report["repair_plan"] = build_repair_plan(live_report)
            except Exception:  # noqa: BLE001
                pass
        return live_report
    except Exception as e:  # noqa: BLE001
        return {
            "status": "error",
            "error": str(e)[:240],
            "source_errors": live_errors[:5],
            "prefer_grid": True,
        }

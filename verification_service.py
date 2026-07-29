"""Orchestration layer for read-only create_set live verification.

The service has no Flask dependency. Blueprint code supplies project-specific
callbacks for Grid campaign listing and Direct token lookup; this module wires
those sources into ``live_verifier`` and repair planning.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from .core import campaign as cmc
from .core.campaign_result import created_campaigns
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


def _skipped_uac_ids(results: list[dict[str, Any]],
                     grid_rows: Optional[list[dict[str, Any]]]) -> list[int]:
    """id УЖЕ СУЩЕСТВУЮЩИХ tp6/tp7 (RESUME-SKIP со `struct`), отрезолвленные ПО ИМЕНИ из Grid-снимка.

    Зачем: `_created_ids` строится через `campaign_result.created_campaigns`, а тот отбрасывает
    строки со `skipped` (`campaign_result.py:49`). Без этого списка деталей по пропущенным
    кампаниям не запрашивается вовсе, и проход `live_verifier` по SKIP-строкам вырождается в
    счётчик + пачку `UAC_DETAIL_SKIPPED` — сверка `UAC_STRUCT_*` была инертна ПО ПОСТРОЕНИЮ.

    Резолв — тем же `_grid_rows_by_prefix` по УЖЕ прочитанному Grid-снимку (фид-суффиксы/`_vNN`),
    поэтому ноль новых запросов. Считать ОБЯЗАТЕЛЬНО ДО `UacReadClient.campaign_details`.
    """
    if not grid_rows:
        return []
    from .core.campaign_result import as_int, result_name
    from .live_verifier import _grid_rows_by_prefix, _index_by_name, _skipped_struct_rows

    by_name = _index_by_name(grid_rows)
    ids: list[int] = []
    seen: set[int] = set()
    for row in _skipped_struct_rows(results or []):
        nm = result_name(row)
        # UacReadClient обслуживает только tp6/tp7 — чужие имена в него не отправляем.
        if not nm or not nm.lower().startswith(("tp6_", "tp7_")):
            continue
        # ВСЕ РК позиции, а не первая: фан-аут по фидам даёт несколько кампаний на одну строку.
        for live in _grid_rows_by_prefix(by_name, nm):
            cid = as_int(live.get("id") or live.get("Id"))
            if cid is not None and cid > 0 and cid not in seen:
                seen.add(cid)
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
            from .clients.grid_read import GridReadClient
            grid_content_counts = GridReadClient(login).campaign_content_counts(ids)
        else:
            grid_content_counts = {}
    except Exception as e:  # noqa: BLE001
        live_errors.append(f"grid-content: {str(e)[:180]}")

    try:
        uac_ids = _created_ids(results or [], kind="uac")
        if uac_ids:
            from .clients.uac_read import UacReadClient, summarize_uac_detail
            raw_details = UacReadClient(login, agency=agency).campaign_details(uac_ids)
            uac_details = {cid: summarize_uac_detail(row) for cid, row in raw_details.items()}
        else:
            uac_details = {}
    except Exception as e:  # noqa: BLE001
        live_errors.append(f"uac-detail: {str(e)[:180]}")

    # + УЖЕ СУЩЕСТВУЮЩИЕ tp6/tp7 из RESUME-SKIP: без них проход сверки структуры в
    # `live_verifier` не получает ни одной детали (см. `_skipped_uac_ids`). `grid_rows`
    # прочитан выше, поэтому резолв по имени бесплатный.
    # ⚠️ ОТДЕЛЬНЫЙ try — НЕ в одном с нашими id: это кампании, которые МЫ НЕ СОЗДАВАЛИ (нет прав,
    # не видна, Grid отдал мусор). В общем try их отказ оставлял `uac_details = None` и глушил
    # детальную UAC-проверку ВСЕГО набора, включая свежесозданные кампании.
    if uac_details is not None:
        try:
            skip_ids = [i for i in _skipped_uac_ids(results or [], grid_rows)
                        if i not in uac_details]
            if skip_ids:
                from .clients.uac_read import UacReadClient, summarize_uac_detail
                raw_skip = UacReadClient(login, agency=agency).campaign_details(skip_ids)
                uac_details.update({cid: summarize_uac_detail(row)
                                    for cid, row in raw_skip.items()})
        except Exception as e:  # noqa: BLE001
            live_errors.append(f"uac-detail-skip: {str(e)[:180]}")

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
                from .repair.repair_planner import build_repair_plan
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

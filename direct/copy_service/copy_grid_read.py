"""Чтение выбранных Grid-кампаний источника.

Вынесено из copy_engine.py (чистый code-motion, логика не изменена).
DI инъектится copy_engine.configure() фан-аутом; sibling-модули copy_* — прямой импорт (DAG, цикла нет).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
import re
import time

from ..clients import grid_finalize as gf

# ── DI (инъектится copy_engine.configure фан-аутом; None до инъекции) ──
_grid_list_campaigns = None
_COPY_SELECTED_GRID_LIST_TIMEOUT_SEC = 25
_COPY_SELECTED_GRID_TARGETED_TIMEOUT_SEC = 25
_COPY_GRID_READ_RETRY_CHUNK = 10


def configure(deps: dict) -> None:
    """Инъекция DI из blueprint (фан-аут из copy_engine.configure)."""
    globals().update(deps)


def _copy_selected_grid_campaigns_with_meta(login: str, selected_ids: set[int]) -> tuple[list[dict], dict]:
    if not selected_ids:
        return [], {"ok": True, "selected": 0, "read": 0}
    rows = []
    list_error = ""
    try:
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            rows = executor.submit(_grid_list_campaigns, login).result(
                timeout=_COPY_SELECTED_GRID_LIST_TIMEOUT_SEC
            )
        except FuturesTimeout:
            rows = []
            list_error = f"grid_list_campaigns timeout>{_COPY_SELECTED_GRID_LIST_TIMEOUT_SEC}s"
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
    except Exception as exc:
        rows = []
        list_error = str(exc)[:240] or exc.__class__.__name__
    out = []
    for row in rows or []:
        try:
            cid = int(row.get("id") or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid in selected_ids:
            out.append(row)
    found_ids = {int(r.get("id") or 0) for r in out if str(r.get("id") or "").isdigit()}
    missing_ids = set(selected_ids) - found_ids
    if missing_ids and not list_error:
        try:
            out.extend(_copy_selected_grid_campaigns_targeted(login, missing_ids))
        except Exception:
            pass
    found_ids = {int(r.get("id") or 0) for r in out if str(r.get("id") or "").isdigit()}
    missing_ids = set(selected_ids) - found_ids
    if list_error and missing_ids:
        return out, {
            "ok": False,
            "selected": len(selected_ids),
            "read": len(out),
            "missing": len(missing_ids),
            "error": list_error,
        }
    return out, {"ok": True, "selected": len(selected_ids), "read": len(out)}


def _copy_selected_grid_campaigns(login: str, selected_ids: set[int]) -> list[dict]:
    rows, _meta = _copy_selected_grid_campaigns_with_meta(login, selected_ids)
    return rows


def _copy_selected_grid_campaigns_targeted(login: str, selected_ids: set[int]) -> list[dict]:
    """Bounded fallback for selected Grid-only campaigns missed by broad list."""
    if not selected_ids:
        return []

    def _read() -> list[dict]:
        snap = _copy_grid_read_selected(login, selected_ids)
        out: list[dict] = []
        for row in snap.get("campaigns") or []:
            cid = _safe_row_id(row, "id")
            if cid in selected_ids:
                out.append({
                    "id": str(cid),
                    "name": row.get("name") or "",
                    "typename": row.get("__typename") or row.get("typename") or row.get("type") or "",
                    "status": row.get("status") or {},
                })
        return out

    try:
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            return executor.submit(_read).result(timeout=_COPY_SELECTED_GRID_TARGETED_TIMEOUT_SEC)
        except FuturesTimeout:
            return []
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
    except Exception:
        return []


def _copy_selected_grid_campaigns_by_id(login: str, selected_ids: set[int]) -> list[dict]:
    """Read selected campaign rows by exact Grid campaignIdIn filter.

    The broad Grid list can omit hidden, archived, or Grid-only rows. This narrow query is
    used only as a classifier fallback before declaring selected ids missing.
    """
    ids = [str(int(x)) for x in selected_ids if int(x) > 0]
    if not ids:
        return []
    from ..clients.grid_read import GridReadClient

    reader = GridReadClient(login)
    inp = {
        "filter": {"campaignIdIn": ids},
        "statRequirements": {"preset": "TODAY", "goalIds": [], "useCampaignGoalIds": True},
        "limitOffset": {"limit": max(1, len(ids)), "offset": 0},
        "orderBy": [{"order": "ASC", "field": "ID"}],
    }
    query = (
        "query CopyCampRows($login:String!,$inp:GdCampaignsContainerInput!){"
        "client(searchBy:{login:$login}){campaigns(input:$inp){rowset{"
        "id name __typename status{primaryStatus archived}}}}}"
    )
    data = reader._post("CopyCampRows", query, {"login": login, "inp": inp})
    rows = ((((data.get("data") or {}).get("client") or {})
             .get("campaigns") or {}).get("rowset") or [])
    out = []
    for campaign in rows or []:
        status = campaign.get("status") or {}
        out.append({
            "id": campaign.get("id"),
            "name": campaign.get("name"),
            "typename": campaign.get("__typename"),
            "status": status.get("primaryStatus") or "",
            "archived": bool(status.get("archived")),
        })
    return out


def _copy_grid_read_selected(login: str, selected_ids: set[int]) -> dict:
    """Read selected Text/Unified campaigns with Grid cookies, without Direct API units."""
    from ..clients.grid_read import GridReadClient

    ids = sorted({int(x) for x in selected_ids if int(x) > 0})
    if not ids:
        return {"campaigns": [], "groups": [], "ads": []}
    id_strings = [str(x) for x in ids]
    reader = GridReadClient(login)
    inp_common = {
        "filter": {"campaignIdIn": id_strings},
        "statRequirements": {"preset": "TODAY", "goalIds": [], "useCampaignGoalIds": True},
        "limitOffset": {"limit": 10000, "offset": 0},
        "orderBy": [{"order": "ASC", "field": "ID"}],
    }
    q_campaigns = (
        "query CopyCamp($login:String!,$inp:GdCampaignsContainerInput!){"
        "client(searchBy:{login:$login}){campaigns(input:$inp){rowset{"
        "id name __typename status{primaryStatus archived} "
        "...on GdUnifiedCampaign{metrikaCounters placementTypes additionalData{href} "
        "minusKeywords disabledPlaces strategy{budget{sum}}}"
        "...on GdTextCampaign{metrikaCounters placementTypes additionalData{href} "
        "minusKeywords disabledPlaces strategy{budget{sum}}}}}}}"
    )
    campaigns = _copy_grid_read_campaign_rows(reader, login, q_campaigns, inp_common, ids)

    q_ads = (
        "query CopyAds($login:String!,$inp:GdAdsContainerInput!){"
        "client(searchBy:{login:$login}){ads(input:$inp){rowset{"
        "__typename id campaignId adGroupId "
        "...on GdTextAd{href title titleExtension body domain image{imageHash name} status{primaryStatus}} "
        "...on GdAdaptiveTextAd{href titles bodies images{imageHash name}} "
        "...on GdShoppingAd{id adGroupId campaignId} "
        "...on GdListingAd{id adGroupId campaignId}"
        "}}}}"
    )
    ads = _copy_grid_read_ad_rows(reader, login, q_ads, inp_common, ids)

    groups = gf.GridClient(login, cookie=reader.cookie, refresh_explicit_cookie=True).groups_for_edit(ids)
    return {"campaigns": campaigns, "groups": groups, "ads": ads}


def _copy_grid_read_selected_campaigns(login: str, selected_ids: set[int]) -> list[dict]:
    """Read only selected campaign rows via the same resilient CopyCamp path."""
    from ..clients.grid_read import GridReadClient

    ids = sorted({int(x) for x in selected_ids if int(x) > 0})
    if not ids:
        return []
    reader = GridReadClient(login)
    inp_common = {
        "filter": {"campaignIdIn": [str(x) for x in ids]},
        "statRequirements": {"preset": "TODAY", "goalIds": [], "useCampaignGoalIds": True},
        "limitOffset": {"limit": 10000, "offset": 0},
        "orderBy": [{"order": "ASC", "field": "ID"}],
    }
    q_campaigns = (
        "query CopyCamp($login:String!,$inp:GdCampaignsContainerInput!){"
        "client(searchBy:{login:$login}){campaigns(input:$inp){rowset{"
        "id name __typename status{primaryStatus archived} "
        "...on GdUnifiedCampaign{metrikaCounters placementTypes additionalData{href} "
        "minusKeywords disabledPlaces strategy{budget{sum}}}"
        "...on GdTextCampaign{metrikaCounters placementTypes additionalData{href} "
        "minusKeywords disabledPlaces strategy{budget{sum}}}}}}}"
    )
    return _copy_grid_read_campaign_rows(reader, login, q_campaigns, inp_common, ids)


def _copy_grid_read_campaign_rows(reader, login: str, query: str, inp_common: dict,
                                  ids: list[int]) -> list[dict]:
    """Read campaign rows and retry missing ids in small chunks."""
    rows = _copy_grid_read_campaign_rows_once(reader, login, query, inp_common, ids)
    expected = {int(x) for x in ids}
    got = {_safe_row_id(row, "id") for row in rows}
    missing = sorted(expected - got)
    if missing:
        rows_by_id = {
            cid: row for row in rows
            for cid in [_safe_row_id(row, "id")]
            if cid > 0
        }
        for chunk in _chunks(missing, _COPY_GRID_READ_RETRY_CHUNK):
            for row in _copy_grid_read_campaign_rows_once(reader, login, query, inp_common, chunk):
                cid = _safe_row_id(row, "id")
                if cid in expected:
                    rows_by_id[cid] = row
        still_missing = sorted(expected - set(rows_by_id))
        if still_missing:
            for cid in still_missing:
                for row in _copy_grid_read_campaign_rows_once(reader, login, query, inp_common, [cid]):
                    row_id = _safe_row_id(row, "id")
                    if row_id == cid:
                        rows_by_id[cid] = row
                        break
        rows = [rows_by_id[cid] for cid in ids if cid in rows_by_id]
    return rows


def _copy_grid_read_campaign_rows_once(reader, login: str, query: str, inp_common: dict,
                                       ids: list[int]) -> list[dict]:
    inp = _copy_grid_read_input_for_ids(inp_common, ids)
    data = reader._post("CopyCamp", query, {"login": login, "inp": inp})
    return ((((data.get("data") or {}).get("client") or {})
             .get("campaigns") or {}).get("rowset") or [])


def _copy_grid_read_ad_rows(reader, login: str, query: str, inp_common: dict,
                            ids: list[int]) -> list[dict]:
    rows_by_id: dict[int, dict] = {}
    idless: list[dict] = []
    for chunk in _chunks(ids, _COPY_GRID_READ_RETRY_CHUNK):
        inp = _copy_grid_read_input_for_ids(inp_common, chunk)
        data = reader._post("CopyAds", query, {"login": login, "inp": inp})
        rows = ((((data.get("data") or {}).get("client") or {})
                 .get("ads") or {}).get("rowset") or [])
        for row in rows:
            aid = _safe_row_id(row, "id")
            if aid > 0:
                rows_by_id[aid] = row
            else:
                idless.append(row)
    return list(rows_by_id.values()) + idless


def _copy_grid_read_input_for_ids(inp_common: dict, ids: list[int]) -> dict:
    inp = dict(inp_common)
    flt = dict(inp.get("filter") or {})
    flt["campaignIdIn"] = [str(int(x)) for x in ids if int(x) > 0]
    inp["filter"] = flt
    inp["limitOffset"] = dict(inp.get("limitOffset") or {"limit": 10000, "offset": 0})
    return inp


def _safe_row_id(row: dict, key: str) -> int:
    try:
        return int((row or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _chunks(values: list[int], size: int):
    step = max(1, int(size or 1))
    for idx in range(0, len(values), step):
        yield values[idx:idx + step]


def _copy_grid_campaign_spec(name: str, counter_id: int, goal_id: int,
                              weekly_budget: int = 7000) -> dict:
    m = re.search(r"\btp(\d+)_", str(name or ""), re.I)
    tp = int(m.group(1)) if m else 1
    search = tp in (2, 4, 5)
    gallery = tp in (3, 5)
    network = tp in (1, 3)
    return {
        "name": str(name or "")[:255],
        "counter_id": int(counter_id or 0),
        "goal_id": int(goal_id or 0),
        "cpa": 250,
        "weekly_budget": int(weekly_budget) if weekly_budget and int(weekly_budget) > 0 else 7000,
        "start_date": time.strftime("%Y-%m-%d"),
        "network": bool(network),
        "search": bool(search),
        "gallery": bool(gallery),
        "organic": bool(tp == 5),
        "pay_for_conversion": False,
    }

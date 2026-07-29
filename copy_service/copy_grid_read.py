"""Чтение выбранных Grid-кампаний источника.

Вынесено из copy_engine.py (чистый code-motion, логика не изменена).
DI инъектится copy_engine.configure() фан-аутом; sibling-модули copy_* — прямой импорт (DAG, цикла нет).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
import re
import time

from .. import grid_finalize as gf

# ── DI (инъектится copy_engine.configure фан-аутом; None до инъекции) ──
_grid_list_campaigns = None
_COPY_SELECTED_GRID_LIST_TIMEOUT_SEC = 25


def configure(deps: dict) -> None:
    """Инъекция DI из blueprint (фан-аут из copy_engine.configure)."""
    globals().update(deps)


def _copy_selected_grid_campaigns(login: str, selected_ids: set[int]) -> list[dict]:
    if not selected_ids:
        return []
    try:
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            rows = executor.submit(_grid_list_campaigns, login).result(
                timeout=_COPY_SELECTED_GRID_LIST_TIMEOUT_SEC
            )
        except FuturesTimeout:
            return []
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
    except Exception:
        return []
    out = []
    for row in rows or []:
        try:
            cid = int(row.get("id") or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid in selected_ids:
            out.append(row)
    return out


def _copy_grid_read_selected(login: str, selected_ids: set[int]) -> dict:
    """Read selected Unified campaigns with Grid cookies, without Direct API units."""
    from ..grid_read import GridReadClient

    ids = [int(x) for x in selected_ids if int(x) > 0]
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
        "minusKeywords disabledPlaces strategy{budget{sum}}}}}}"
    )
    camps_data = reader._post("CopyCamp", q_campaigns, {"login": login, "inp": inp_common})
    campaigns = ((((camps_data.get("data") or {}).get("client") or {})
                  .get("campaigns") or {}).get("rowset") or [])

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
    ads_data = reader._post("CopyAds", q_ads, {"login": login, "inp": inp_common})
    ads = ((((ads_data.get("data") or {}).get("client") or {})
            .get("ads") or {}).get("rowset") or [])

    groups = gf.GridClient(login, cookie=reader.cookie).groups_for_edit(ids)
    return {"campaigns": campaigns, "groups": groups, "ads": ads}


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

"""Read-only UAC helpers for tp6/tp7 live verification.

UAC campaigns are invisible in Direct v5 and do not expose normal Grid
adGroups/ads. This module reads details through ``/web-api/uac`` on browser
cookies and normalizes only small counters needed by repair planning. It does
not mutate campaigns.
"""
from __future__ import annotations

from typing import Any

from . import campaign as cmc


def _unwrap(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        for key in ("result", "campaign", "item"):
            value = data.get(key)
            if isinstance(value, dict):
                return value
        return data
    return {}


def _count_list(row: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = row.get(key)
        if isinstance(value, list):
            return len(value)
    return 0


def _has_feed(row: dict[str, Any]) -> bool:
    return bool(row.get("feed_id") or row.get("listings_feed_id") or row.get("ecom"))


def _iter_filter_conditions(value: Any):
    """Yield UAC feed filter condition dicts from raw detail payload.

    UAC private responses have changed shape a few times. In create payload we
    send ``feed_filters[].conditions[]`` / ``listings_feed_filters[].conditions[]``;
    details can return the same shape or a nested normalized variant. For live
    verification we only need the condition fields, so recurse conservatively.
    """
    if isinstance(value, dict):
        field = value.get("field") or value.get("operand") or value.get("name")
        if field and ("operator" in value or "value" in value or "values" in value):
            yield value
        for child in value.values():
            yield from _iter_filter_conditions(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_filter_conditions(child)


def _filter_summary(row: dict[str, Any]) -> dict[str, Any]:
    conditions = list(_iter_filter_conditions({
        "feed_filters": row.get("feed_filters"),
        "listings_feed_filters": row.get("listings_feed_filters"),
        "filter_conditions": row.get("filter_conditions"),
        "filters": row.get("filters"),
    }))
    fields = []
    for cond in conditions:
        field = str(cond.get("field") or cond.get("operand") or cond.get("name") or "").strip()
        if field:
            fields.append(field)
    norm_fields = {f.lower() for f in fields}
    return {
        "feed_filter_conditions": len(conditions),
        "feed_filter_fields": sorted(set(fields)),
        "has_model_filter": any(f in {"model", "modelname", "model_name"} for f in norm_fields),
        "has_vendor_filter": any(f in {"vendor", "vendorname", "manufacturer"} for f in norm_fields),
        "has_collection_filter": any(f in {"collectionid", "collection_id"} for f in norm_fields),
    }


def _status(row: dict[str, Any]) -> str:
    return str(row.get("status") or row.get("state") or row.get("ext_status") or "").strip().lower()


def _pricing(row: dict[str, Any]) -> str:
    return str(row.get("pricing") or row.get("payment_type") or row.get("paymentType") or "").strip().upper()


def _number(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        raw = row.get(key)
        if raw in (None, ""):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _flag(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row:
            return row.get(key)
    return None


class UacReadClient:
    """Read-only wrapper around ``campaign.UacClient``."""

    def __init__(self, login: str, agency: str | None = None):
        self.login = login
        self.agency = (agency or "").strip() or None
        self.client = cmc.build_client(login, account=self.agency)

    def campaign_detail(self, campaign_id: int | str) -> dict[str, Any]:
        """Return raw UAC campaign detail for one id."""
        cid = str(campaign_id).strip()
        if not cid:
            return {}
        if self.client.csrf is None:
            self.client.link_info("https://ya.ru")
        data = self.client._request("GET", f"/campaign/{cid}", step=f"detail:{cid}")
        return _unwrap(data)

    def campaign_details(self, campaign_ids: list[int | str]) -> dict[int, dict[str, Any]]:
        """Return ``{campaign_id: raw_detail}`` for ids that could be read."""
        out: dict[int, dict[str, Any]] = {}
        seen: set[int] = set()
        for raw in campaign_ids or []:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                continue
            if cid <= 0 or cid in seen:
                continue
            seen.add(cid)
            detail = self.campaign_detail(cid)
            if detail:
                out[cid] = detail
        return out


def summarize_uac_detail(row: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize UAC detail into counters used by live verification."""
    row = _unwrap(row or {})
    summary = {
        "status": _status(row),
        "pricing": _pricing(row),
        "titles": _count_list(row, "titles", "title_items"),
        "texts": _count_list(row, "texts", "text_items"),
        "sitelinks": _count_list(row, "sitelinks"),
        "content": _count_list(row, "content_ids", "contents", "media"),
        "images": _count_list(row, "image_content_ids", "images"),
        "videos": _count_list(row, "video_content_ids", "videos"),
        "week_limit": _number(row, "week_limit", "weekly_budget", "weekBudget"),
        "limit_period": str(row.get("limit_period") or row.get("limitPeriod") or "").strip().lower(),
        "regions": _count_list(row, "regions", "region_ids", "geo", "geos"),
        "counters": _count_list(row, "counters"),
        "goals": _count_list(row, "goals"),
        "has_tracking_params": bool(row.get("tracking_params") or row.get("href_params") or row.get("banner_href_params")),
        "has_feed": _has_feed(row),
        "yandex_maps_enabled": _flag(row, "yandex_maps_enabled", "maps_enabled", "is_yandex_maps_enabled"),
        "alternative_texts_enabled": row.get("alternative_texts_enabled"),
        "recommendations_management_enabled": row.get("recommendations_management_enabled"),
        "price_recommendations_management_enabled": row.get("price_recommendations_management_enabled"),
    }
    summary.update(_filter_summary(row))
    return summary

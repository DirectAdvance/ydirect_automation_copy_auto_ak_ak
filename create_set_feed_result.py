"""Result helpers for feed-backed create-set paths."""
from __future__ import annotations

from typing import Any


SHOPPING_AD_REQUIRED_ERROR = "фидовая кампания создана без ShoppingAd"
FEED_CAMPAIGN_NOT_CREATED_ERROR = "фидовая кампания не создана"


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def shopping_cookie_success(report: dict[str, Any] | None) -> bool:
    """True only when Grid cookie path created campaign, group and ShoppingAd."""
    rep = report or {}
    return (
        bool(rep.get("campaign_id"))
        and _as_int(rep.get("groups")) > 0
        and _as_int(rep.get("ads")) > 0
        and bool(rep.get("shopping_ad_ids") or [])
    )


def ensure_shopping_cookie_error(report: dict[str, Any] | None) -> str:
    """Return a user-facing failure reason and attach the invariant error once."""
    rep = report or {}
    errors = rep.setdefault("errors", [])
    needs_shopping_error = (
        bool(rep.get("campaign_id"))
        and _as_int(rep.get("groups")) > 0
        and (_as_int(rep.get("ads")) <= 0 or not (rep.get("shopping_ad_ids") or []))
    )
    if needs_shopping_error and not any(SHOPPING_AD_REQUIRED_ERROR in str(err) for err in errors):
        errors.append(SHOPPING_AD_REQUIRED_ERROR)
    return "; ".join(str(err) for err in errors if err)[:240] or FEED_CAMPAIGN_NOT_CREATED_ERROR

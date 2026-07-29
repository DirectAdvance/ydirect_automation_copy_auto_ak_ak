"""Фасад шагов copy-постпроцесса. Реализация разнесена по copy_*_steps.py."""
from __future__ import annotations

from .copy_context import CopyCtx, _noop_log
from .copy_step_utils import _chunks, _rj, _v5_add_err, _wj
from .copy_asset_steps import (
    source_has_network, pull_source_campaign_assets, step_age_bidmods,
    step_disabled_places, step_attach_callouts, step_attach_sitelinks,
    step_attach_promos, _existing_demographic_ages, _promo_attach_err,
)
from .copy_keyword_steps import step_keywords
from .copy_creative_steps import step_adaptive_creatives, step_videos
from .copy_price_steps import _merge_cheaper, _clean_group_brand, step_prices
from .copy_settings_steps import (
    step_fix_organic_placement, step_fix_search_campaign_invariants,
    step_settings_diff, _search_at_ok, _diff_norm, _diff_rows, _fix_v5_settings,
)

__all__ = [
    "CopyCtx", "source_has_network", "pull_source_campaign_assets",
    "step_age_bidmods", "step_disabled_places", "step_attach_callouts",
    "step_attach_sitelinks", "step_attach_promos", "step_keywords",
    "step_adaptive_creatives", "step_videos", "step_prices",
    "step_fix_organic_placement", "step_fix_search_campaign_invariants",
    "step_settings_diff", "_clean_group_brand",
]

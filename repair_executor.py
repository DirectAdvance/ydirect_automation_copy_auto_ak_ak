"""Re-export facade — backward-compat hub for all importers of repair_executor.

Actual implementations live in:
  repair_common.py   — RepairDeps, shared constants/helpers (cmc/gc/gf aliases, _unique_positive_ints)
  repair_content.py  — execute_promo_repair / execute_callouts_repair / execute_rename_repair / execute_content_repair
  repair_media.py    — execute_images_repair / execute_adprice_repair / execute_default_text_repair
                       execute_campaign_invariant_repair / execute_images_forbidden_repair
  repair_keywords.py — execute_keywords_repair / execute_keywords_wrong_group_repair

Importers:
  * ``from . import repair_executor as rex`` (automation_runtime, campaign_spec_audit, repair_auto,
    create_set_repairing) — access via rex.<name> still works because all names are bound here.
  * ``from .repair_executor import RepairDeps, execute_callouts_repair, ...``
    (create_set_apply_batches) — explicit named imports work because all symbols are imported below.
  * ``_rex._ct_of(...)`` (create_set_repairing:277) — private helper also re-exported explicitly.
"""
from __future__ import annotations

from .repair_common import (
    RepairDeps,
    _CT_RE,
    _KEYWORDS_MIN,
    _KW_MAX_PER_GROUP,
    _SEARCH_TPS,
    _TP_RE,
    _unique_positive_ints,
    cmc,
    gc,
    gf,
)
from .repair_content import (
    execute_callouts_repair,
    execute_content_repair,
    execute_promo_repair,
    execute_rename_repair,
)
from .repair_media import (
    _ADS_FOR_IMG_CLEAR_Q,
    _get_ads_with_images,
    _run_per_campaign_repair,
    execute_adprice_repair,
    execute_campaign_invariant_repair,
    execute_default_text_repair,
    execute_images_forbidden_repair,
    execute_images_repair,
)
from .repair_keywords import (
    _autotarget_ok,
    _ct_of,
    _grid_show_condition_ids,
    _SHOW_CONDITIONS_Q,
    _tp_of,
    _V5_URL,
    _v5_keywords_delete,
    execute_keywords_repair,
    execute_keywords_wrong_group_repair,
)

__all__ = [
    # Core DI container
    "RepairDeps",
    # Content domain
    "execute_promo_repair",
    "execute_callouts_repair",
    "execute_rename_repair",
    "execute_content_repair",
    # Media domain
    "execute_images_repair",
    "execute_adprice_repair",
    "execute_default_text_repair",
    "execute_campaign_invariant_repair",
    "execute_images_forbidden_repair",
    # Keywords domain
    "execute_keywords_repair",
    "execute_keywords_wrong_group_repair",
]

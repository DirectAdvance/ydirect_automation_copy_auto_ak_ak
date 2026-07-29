"""Shared imports, constants, RepairDeps and _unique_positive_ints for repair domain modules.

Domain modules (repair_content, repair_media, repair_keywords) import from here.
The facade repair_executor.py re-exports everything via explicit named imports.
No circular dependency: this module imports only campaign/grid_create/grid_finalize (leaves).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from .. import campaign as cmc  # noqa: F401 — re-exported to domain modules
from .. import grid_create as gc  # noqa: F401 — re-exported to domain modules
from .. import grid_finalize as gf  # noqa: F401 — re-exported to domain modules


# Порог идемпотентности: группа с ≥ этого числа ключей И корректным автотаргетом не трогается.
_KEYWORDS_MIN = 1
# Жёсткий лимит Яндекса: максимум 200 ключевых фраз на группу объявлений. Если отдать больше —
# Grid AddKeywords отклоняет ВСЮ пачку (KeywordDefectIds.Keyword.MAX_KEYWORDS_PER_AD_GROUP_EXCEEDED),
# и группа остаётся с 0 ключей (симптом NO_KEYWORDS_LIVE). Поэтому режем список до 200 на группу.
_KW_MAX_PER_GROUP = 200
_SEARCH_TPS = {2, 4, 5}
_CT_RE = re.compile(r"ct\d{4}", re.IGNORECASE)
_TP_RE = re.compile(r"^\s*tp(\d+)_", re.IGNORECASE)


@dataclass(frozen=True)
class RepairDeps:
    account_ctx: Callable[[str], dict | None]
    promo_content_lines: Callable[[list[dict]], list[str]]
    create_account_promo_from_slepok: Callable[..., tuple[int | None, str]]
    dedup_callouts: Callable[..., list[str]]
    text_content_context: Callable[[str, dict, dict], dict]
    shopping_content_context: Callable[[str, dict, dict], dict]
    callout_cap: int
    # AUTO-REPAIR keywords: (login, ctx, meta) -> {"keywords": [...], "seg": str, "brand": str}
    # meta несёт tp_code/ct/adgroup_name; None → keyword-repair недоступен (deps не прокинуты).
    group_keywords_context: Callable[[str, dict, dict], dict] | None = None
    # In-place image repair: (login, campaign_id, ctx) -> {"ok", "ads_updated", ...}
    # Пере-заливает картинки tp1 через RMW + UpdateAdaptiveTextAds. None → executor пропускает.
    campaign_images_repair: Callable[[str, int, dict], dict] | None = None
    # In-place adprice repair: (login, campaign_id, ctx) -> {"ok", "ads_updated", ...}
    # Проставляет adPrice через _grid_set_ad_prices по прайс-кэшу. None → executor пропускает.
    campaign_adprice_repair: Callable[[str, int, dict], dict] | None = None
    # In-place default_text repair: (login, campaign_id, ctx) -> {"ok", "ads_updated", ...}
    # Заполняет bodies ShoppingAd через set_default_text. None → executor пропускает.
    campaign_default_text_repair: Callable[[str, int, dict], dict] | None = None
    # v5 OAuth token by login: (login) -> token|None. Нужен для keywords.delete при
    # KEYWORDS_WRONG_GROUP-фиксе (Grid не удаляет ключи, только v5). None → фикс сдвига недоступен.
    v5_token_for_login: Callable[[str], str | None] | None = None


def _unique_positive_ints(values: list[Any]) -> list[int]:
    ids: list[int] = []
    for raw in values or []:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in ids:
            ids.append(value)
    return ids

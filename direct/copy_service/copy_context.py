"""Общий контекст шагов copy-постпроцесса."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


def _noop_log(_m: str) -> None:
    return None


@dataclass
class CopyCtx:
    """Единый контекст под-сервисов копировщика."""
    target_login: str
    target_agency: str
    src_dir: Path
    workdir: Path
    body: dict
    maps: dict
    grid: object = None
    promo_client: object = None
    target_token: str = ""
    log: Callable[[str], None] = _noop_log
    v5_call: Optional[Callable] = None
    enabled_minus_places: Optional[Callable] = None
    feed_offer_prices: Optional[Callable] = None
    account_offer_prices: Optional[Callable] = None
    group_ad_price: Optional[Callable] = None
    set_ad_prices: Optional[Callable] = None
    source_login: str = ""
    source_grid: object = None
    geo_pairs: Optional[list] = None
    update_adaptive_ads: Optional[Callable] = None
    video_upload_client: object = None
    video_file_resolver: Optional[Callable] = None
    cached_adaptive_src: Optional[dict] = None
    cached_adaptive_tgt: Optional[dict] = None
    cached_source_edit_rows: Optional[dict] = None
    cached_target_edit_rows: Optional[dict] = None

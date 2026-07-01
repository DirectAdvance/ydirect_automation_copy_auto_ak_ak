"""Apply campaign content from direct_slepok_content to create_set items."""
from __future__ import annotations

from typing import Any, Callable, Optional


SlepokContentGetter = Callable[[str, str, str], Optional[dict[str, Any]]]
Rotator = Callable[[list[Any], int, int], list[Any]]
UnifyUtp = Callable[[list[str], list[str], str, list[dict[str, Any]]], tuple]


def apply_slepok_campaign_content(*, items: list[dict[str, Any]], content_source: str,
                                  agent: str, site_type: str,
                                  slepok_content_get: SlepokContentGetter,
                                  rotate_window: Rotator,
                                  unify_utp_numbers: Optional[UnifyUtp] = None) -> str | None:
    """Apply campaign content from selected slepok when requested."""
    if content_source != "slepok_library":
        return None
    lib = slepok_content_get(agent, site_type, "campaign") if agent else None
    if not (isinstance(lib, dict) and (lib.get("titles") or lib.get("texts"))):
        return (f"⚠ записи слепка «{agent}» × «{site_type}» нет в direct_slepok_content — "
                "использованы шаблоны из direct_ad_templates")

    lib_titles = lib.get("titles") or []
    lib_texts = lib.get("texts") or []
    lib_sitelinks = lib.get("sitelinks") or []
    for idx, item in enumerate(items or []):
        item_titles = rotate_window(lib_titles, 5, idx)
        item_texts = rotate_window(lib_texts, 3, idx)
        item_sitelinks = rotate_window(lib_sitelinks, 8, idx)
        if unify_utp_numbers and (item_titles or item_texts or item_sitelinks):
            item_titles, item_texts, _title2, item_sitelinks, _changed = unify_utp_numbers(
                item_titles, item_texts, "", item_sitelinks)
        if not item.get("titles") and item_titles:
            item["titles"] = item_titles
        if not item.get("texts") and item_texts:
            item["texts"] = item_texts
        if not item.get("sitelinks") and item_sitelinks:
            item["sitelinks"] = item_sitelinks

    return (f"контент из БД-слепка «{agent}» × «{site_type}» "
            f"({len(lib_titles)} заголовков, {len(lib_texts)} текстов), "
            "ротация по РК включена")

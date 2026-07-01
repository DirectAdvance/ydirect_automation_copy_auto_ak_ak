"""Input normalization helpers for Direct create_set."""
from __future__ import annotations

from typing import Any, Callable


TextNormalizer = Callable[[Any], str]
SemanticKey = Callable[[str], str]
NumberParser = Callable[[Any, int], int]


def normalize_callouts(raw_callouts: list[Any], *,
                       normalize_text: TextNormalizer,
                       semantic_key: SemanticKey) -> list[str]:
    """Normalize and semantic-deduplicate create_set callouts preserving order."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_callouts or []:
        text = normalize_text(raw)
        key = semantic_key(text)
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def first_feed_items(items: list[dict[str, Any]], *, parse_number: NumberParser) -> list[dict[str, Any]]:
    """Keep items for the first non-zero feed_id, preserving items without feed_id."""
    first_feed = None
    filtered: list[dict[str, Any]] = []
    for item in items or []:
        feed_id = parse_number(item.get("feed_id"), 0)
        if feed_id:
            if first_feed is None:
                first_feed = feed_id
            if feed_id != first_feed:
                continue
        filtered.append(item)
    return filtered


def normalize_create_set_input(body: dict[str, Any], *,
                               normalize_callout_text: TextNormalizer,
                               callout_semantic_key: SemanticKey,
                               parse_number: NumberParser) -> dict[str, Any]:
    """Return normalized request fields used by api_create_set."""
    items = body.get("items") or []
    single_feed = bool(body.get("single_feed"))
    if single_feed and items:
        items = first_feed_items(items, parse_number=parse_number)
    return {
        "login": (body.get("login") or "").strip(),
        "items": items,
        "agent": (body.get("agent") or "").strip(),
        "content_source": (body.get("content_source") or "").strip(),
        "callouts": normalize_callouts(
            body.get("callouts") or [],
            normalize_text=normalize_callout_text,
            semantic_key=callout_semantic_key,
        ),
        "counter_id": parse_number(body.get("counter_id"), 0),
        "goal_id": parse_number(body.get("goal_id"), 0),
        "cpa": parse_number(body.get("cpa"), 2000),
        "no_cpa": bool(body.get("no_cpa")),
        "single_feed": single_feed,
        "via_cookie": bool(body.get("via_cookie")),
        "stream_content": bool(body.get("stream_content")),
    }

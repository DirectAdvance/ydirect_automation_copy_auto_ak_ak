"""Input normalization helpers for Direct create_set."""
from __future__ import annotations

from typing import Any, Callable


TextNormalizer = Callable[[Any], str]
SemanticKey = Callable[[str], str]
NumberParser = Callable[[Any, int], int]
SINGLE_FEED_KEY = "yandex.xml"
# Фолбэк-фид для «профильных фидов» когда ни один профильный не найден в аккаунте
# (кнопка «Продолжить с другим фидом» в feed_alert; правило Семёна 2026-07-02).
FALLBACK_SINGLE_FEED_KEY = "yandex-catalog-model-design-custom-name.xml"
# «Профильные фиды» — целевой набор для режима single_feed:
# фид-кампании (tp5/tp7/tp1-товарка) строятся по ОБОИМ, если оба есть в аккаунте.
PROFILE_FEED_KEYS = ("yandex.xml", "yandex-used-auto.xml")


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


def feed_key(value: Any) -> str:
    """Normalize feed name/path/url to the basename key used by feed rules."""
    raw = str(value or "").strip().split("?", 1)[0].rstrip("/")
    if "/" in raw:
        raw = raw.rsplit("/", 1)[-1]
    return raw.lower()


def _feed_candidate_keys(value: Any) -> set[str]:
    key = feed_key(value)
    if not key:
        return set()
    out = {key}
    if key.endswith(".xml"):
        out.add(key[:-4])
    return out


def feed_row_matches_key(row: dict[str, Any], feed_url_key: str) -> bool:
    """True when a Direct/Grid feed row points to the given feed key (e.g. yandex.xml)."""
    target = _feed_candidate_keys(feed_url_key)
    for key in ("feed_name", "feedKey", "feed_key", "name", "url", "href",
                "source", "sourceUrl", "SourceUrl", "Name"):
        raw = str((row or {}).get(key) or "").strip()
        if not raw:
            continue
        parts = [raw]
        for sep in ("—", "–", "|"):
            if sep in raw:
                parts.extend(raw.split(sep))
        for part in parts:
            if _feed_candidate_keys(part) & target:
                return True
    return False


def feed_row_matches_single_feed(row: dict[str, Any]) -> bool:
    """True when a Direct/Grid feed row points to /yandex.xml."""
    return feed_row_matches_key(row, SINGLE_FEED_KEY)


def feed_row_matches_profile_feed(row: dict[str, Any]) -> bool:
    """True when a Direct/Grid feed row points to ANY of the profile feeds
    (yandex.xml OR yandex-used-auto.xml)."""
    return any(feed_row_matches_key(row, k) for k in PROFILE_FEED_KEYS)


def prefer_single_feed_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer profile feed rows (yandex.xml / yandex-used-auto.xml) for single-feed mode;
    returns ALL matching profile rows (fan-out up to 2); fallback to the first row.
    NOTE: при вызове из _first_url_feed (strict=False) итерация возвращает первый матч."""
    rows = list(rows or [])
    preferred = [row for row in rows if feed_row_matches_profile_feed(row)]
    return preferred if preferred else rows[:1]


def prefer_single_feed_variants(variants: list[tuple]) -> list[tuple]:
    """Prefer tuples matching any profile feed by name OR url; returns ALL profile matches
    (fan-out); fallback to the first tuple.

    Кортежи _tp5_account_data = (id, name, url): имя кабинета может быть человекочитаемым
    («Основной фид»), реальный маркер — url (ревью 03.07 #12: матч только по name брал
    первый попавшийся фид вместо /yandex.xml)."""
    variants = list(variants or [])
    preferred = [v for v in variants
                 if len(v) > 1 and feed_row_matches_profile_feed(
                     {"name": v[1], "url": (v[2] if len(v) > 2 else "")})]
    return preferred if preferred else variants[:1]


def first_feed_items(items: list[dict[str, Any]], *, parse_number: NumberParser) -> list[dict[str, Any]]:
    """Keep profile feed items (yandex.xml / yandex-used-auto.xml) when present;
    fan-out по обоим профильным фидам: если оба найдены, возвращаем items для КАЖДОГО.
    Фолбэк: нет профильных по имени → возвращаем все items as-is (план уже отфильтровал
    по профильным фидам через single_feed — кастомноназванные фиды сохраняются)."""
    target_items = [item for item in (items or []) if feed_row_matches_profile_feed(item)]
    if target_items:
        target_feeds = {
            parse_number(item.get("feed_id"), 0)
            for item in target_items
            if parse_number(item.get("feed_id"), 0)
        }
        filtered = []
        for item in items or []:
            feed_id = parse_number(item.get("feed_id"), 0)
            if feed_id and feed_id not in target_feeds:
                continue
            filtered.append(item)
        return filtered

    # Нет совпадений по имени профильного фида → план уже был отфильтрован single_feed-логикой
    # на сервере (или фиды названы кастомными ярлыками). Возвращаем все items как есть:
    # fan-out по двум кастомноназванным профильным фидам сохраняется.
    return list(items or [])


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

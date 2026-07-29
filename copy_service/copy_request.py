"""Shared request validation for Direct copy UI/API routes."""
from __future__ import annotations

from typing import Callable, Literal


Surface = Literal["ui", "api"]


def parse_campaign_ids(body: dict, *, as_set: bool = False):
    """Parse campaign_ids from request body, skipping non-numeric values."""
    raw = body.get("campaign_ids") or []
    ids = [int(x) for x in raw if str(x).isdigit()]
    return set(ids) if as_set else ids


def parse_feed_map(body: dict) -> dict[str, int]:
    """Parse source_feed_id -> target_feed_id map from request body."""
    raw = body.get("feed_map")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        if str(key).strip().isdigit() and str(value).strip().isdigit() and int(value) > 0:
            out[str(int(key))] = int(value)
    return out


def parse_image_hashes(body: dict) -> list[str]:
    """Parse uploaded image hashes; only an explicit list of non-empty strings is accepted."""
    raw = body.get("image_hashes")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            return []
        out.append(value.strip())
    return out


def validate_api_campaign_ids(body: dict, *, max_ids: int = 500) -> tuple[list[int] | None, str | None]:
    """Strict campaign_ids parser for the public API.

    UI routes keep their legacy tolerant parser. External clients should get an
    explicit 400 instead of a silently changed campaign set.
    """
    raw = body.get("campaign_ids")
    if not isinstance(raw, list):
        return None, "campaign_ids: передайте список положительных целых ID"
    if not raw:
        return None, "campaign_ids: выберите хотя бы одну кампанию"
    if len(raw) > max_ids:
        return None, f"campaign_ids: максимум {max_ids} кампаний за один запрос"

    ids: list[int] = []
    seen: set[int] = set()
    for idx, value in enumerate(raw):
        if isinstance(value, bool):
            return None, f"campaign_ids[{idx}]: должен быть положительным целым ID"
        if isinstance(value, int):
            campaign_id = value
        elif isinstance(value, str) and value.strip().isdigit():
            campaign_id = int(value.strip())
        else:
            return None, f"campaign_ids[{idx}]: должен быть положительным целым ID"
        if campaign_id <= 0:
            return None, f"campaign_ids[{idx}]: должен быть положительным целым ID"
        if campaign_id in seen:
            return None, f"campaign_ids: дубль ID {campaign_id}"
        seen.add(campaign_id)
        ids.append(campaign_id)
    return ids, None


def default_geo_mode(body: dict, mode: str) -> str:
    """Keep historical defaults: other→keep, auto→replace."""
    return (body.get("geo_mode") or ("keep" if mode == "other" else "replace")).strip()


def validate_other_geo(
    body: dict,
    mode: str,
    geo_mode: str,
    *,
    geo_validate_id_func: Callable[[int], bool] | None = None,
    surface: Surface = "ui",
) -> tuple[str | None, list[int]]:
    """Validate mode/geo_mode/geo_region_ids shared by UI and public API routes.

    Returns (error_message, parsed_geo_region_ids). Empty error means validation passed.
    Error wording intentionally preserves old UI/API differences.
    """
    if mode not in ("auto", "other"):
        if surface == "api":
            return "mode допустимо: auto, other", []
        return f"mode='{mode}' не поддерживается; допустимы: auto, other", []
    if mode != "other":
        return None, []
    if geo_mode not in ("keep", "change"):
        if surface == "api":
            return "geo_mode при mode='other' допустимо: keep, change", []
        return f"geo_mode='{geo_mode}' недопустим при mode='other'; допустимы: keep, change", []
    if geo_mode != "change":
        return None, []

    raw_ids = body.get("geo_region_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return "geo_mode='change': передайте geo_region_ids (список регионов)", []
    parsed: list[int] = []
    for idx, value in enumerate(raw_ids):
        if isinstance(value, bool):
            return f"geo_region_ids[{idx}]: должен быть целым числом", []
        if isinstance(value, int):
            parsed.append(value)
            continue
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            parsed.append(int(value.strip()))
            continue
        if surface == "api":
            return f"geo_region_ids[{idx}]: должен быть целым числом", []
    if not parsed:
        return "geo_mode='change': geo_region_ids пуст после парсинга", []
    if any(x == 0 for x in parsed):
        return "geo_region_ids: нулевой id недопустим", []
    if not [x for x in parsed if x > 0]:
        return "geo_mode='change': нет ни одного включённого региона (все минус?)", []
    if geo_validate_id_func is not None:
        invalid = [x for x in parsed if not geo_validate_id_func(abs(x))]
        if invalid:
            return f"geo_region_ids: неизвестные id: {invalid[:5]}", parsed
    return None, parsed

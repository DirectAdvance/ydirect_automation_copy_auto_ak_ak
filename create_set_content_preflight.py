"""Pre-create content pack guards for create_set jobs."""

from __future__ import annotations

import re
from typing import Any


def _requires_keyword_pack(item: dict[str, Any]) -> bool:
    name = str((item or {}).get("name") or (item or {}).get("camp_key") or "").lower()
    if not name:
        return False
    has_kw = bool(re.search(r"(^|[^а-яё])кс([^а-яё]|$)", name)) or "ключев" in name
    if not has_kw:
        return False
    # Pure autotarget items can legally create without a positive keyword pack.
    if bool(item.get("autotarget")) and not bool(item.get("autotarget_keep_keywords")):
        return False
    return True


def create_set_pack_gap_note(body: dict[str, Any]) -> str:
    """Return a blocking note when initial create references missing tp2/tp4 keyword packs.

    This is the initial-create counterpart of the deferred guard: if the local/M3 source is
    readable and a planned keyword/hybrid search campaign points to empty per-group packs,
    fail before any Direct objects are created.
    """
    if not isinstance(body, dict):
        return ""
    slepok = str(body.get("agent") or "").strip()
    site_type = str(body.get("site_type") or "").strip()
    if not slepok or not site_type:
        return ""
    try:
        from . import kontent_pack as kp
    except Exception:  # noqa: BLE001
        return ""

    missing: list[str] = []
    checked = 0
    for item in body.get("items") or []:
        if not isinstance(item, dict):
            continue
        tp = str(item.get("tp") or "").strip()
        typ = str(item.get("type") or "").strip()
        if tp not in ("tp2", "tp4") and typ not in ("search_test", "search_dynamic"):
            continue
        if not _requires_keyword_pack(item):
            continue
        cts = [str(x).strip() for x in (item.get("only_cts") or []) if str(x).strip()]
        if not cts:
            continue
        gks = [str(x).strip() for x in (item.get("only_gks") or []) if str(x).strip()]
        pairs = list(zip(cts, gks)) if len(gks) == len(cts) else [(ct, "") for ct in cts]
        for ct, gk in pairs:
            checked += 1
            tp_key = tp or ("tp2" if typ == "search_test" else "tp4")
            try:
                kw = kp.read_keywords(site_type, tp_key, ct, slepok, group=gk)
                pos_n = len((kw or {}).get("positive") or [])
            except Exception:  # noqa: BLE001
                return ""
            if pos_n <= 0:
                suffix = f"/{gk}" if gk else ""
                missing.append(f"{tp_key}/{ct}{suffix}")
    if not missing:
        return ""
    head = ", ".join(missing[:18])
    more = f"; +{len(missing) - 18}" if len(missing) > 18 else ""
    return (f"content-gap preflight: local/M3 source доступен, но нет keywords для "
            f"{slepok}/{site_type}: {head}{more}. Создание не запущено; нужен top-up "
            f"keyword pack или исключение позиции из структуры (checked={checked})")

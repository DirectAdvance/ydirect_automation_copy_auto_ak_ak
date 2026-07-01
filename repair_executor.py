"""Scoped executors for create_set repair-gate actions.

The functions here perform narrow, cookie/Grid-first mutations for already
planned repair actions. Flask, DB locks, and queue management stay in
``blueprint.py``; this module only executes one bounded repair at a time.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from . import campaign as cmc
from . import grid_create as gc
from . import grid_finalize as gf


# Порог идемпотентности: группа с ≥ этого числа ключей И корректным автотаргетом не трогается.
_KEYWORDS_MIN = 1
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


def execute_promo_repair(login: str, ctx: dict, campaign_ids: list[int], deps: RepairDeps) -> tuple[dict, int]:
    """Create a matching promo via Grid and attach it to existing campaigns."""
    campaign_ids = _unique_positive_ints(campaign_ids)
    if not campaign_ids:
        return {"error": "нет campaign_id для привязки промо"}, 422
    body_obj = ctx.get("body") or {}
    slepok = (body_obj.get("agent") or "").strip()
    if not slepok:
        return {"error": "в сохранённой job нет выбранного слепка для промо"}, 422
    acc = deps.account_ctx(login)
    if not acc:
        return {"error": f"аккаунт {login} не найден в БД"}, 404
    agency = (ctx.get("agency") or body_obj.get("agency") or acc.get("agency") or "").strip()
    try:
        client = cmc.build_client(login, account=(agency or None))
    except Exception as e:  # noqa: BLE001
        return {"error": f"не удалось подобрать рабочую куку для repair-promo: {str(e)[:160]}"}, 502

    content_lines = deps.promo_content_lines(body_obj.get("items") or [])
    site_type = (body_obj.get("site_type") or acc.get("site_type") or "").strip()
    pid, note = deps.create_account_promo_from_slepok(
        client,
        login,
        None,  # no v5 verification here: keep repair free of Direct API units
        {**acc, "site_type": site_type},
        slepok,
        content_lines,
    )
    if not pid:
        return {"error": note or "промо не создано", "uses_direct_units": False}, 422
    from .promo import PromoClient
    attach = PromoClient(client, login).attach(pid, campaign_ids)
    errors = (((attach.get("data") or {}).get("updateCampaignsPromoExtension") or {})
              .get("validationResult") or {}).get("errors") or attach.get("errors")
    if errors:
        return {
            "error": "Grid отклонил привязку промо",
            "promo_id": pid,
            "details": errors,
            "uses_direct_units": False,
        }, 502
    return {
        "ok": True,
        "execute": True,
        "login": login,
        "promo_id": pid,
        "attached_campaign_ids": campaign_ids,
        "attached": len(campaign_ids),
        "transport": "grid_then_attach",
        "uses_direct_units": False,
        "note": note,
    }, 200


def execute_callouts_repair(login: str, ctx: dict, campaign_ids: list[int], deps: RepairDeps) -> tuple[dict, int]:
    """Create selected callouts through Grid and attach them to existing campaigns."""
    campaign_ids = _unique_positive_ints(campaign_ids)
    if not campaign_ids:
        return {"error": "нет campaign_id для привязки уточнений"}, 422
    body_obj = ctx.get("body") or {}
    callouts = deps.dedup_callouts(body_obj.get("callouts") or [], cap=deps.callout_cap)
    if not callouts:
        return {"error": "в сохранённой job нет выбранных уточнений", "uses_direct_units": False}, 422
    acc = deps.account_ctx(login)
    if not acc:
        return {"error": f"аккаунт {login} не найден в БД"}, 404
    agency = (ctx.get("agency") or body_obj.get("agency") or acc.get("agency") or "").strip()
    try:
        client = cmc.build_client(login, account=(agency or None))
        cookie = client.sess.headers.get("Cookie") or ""
        grid = gf.GridClient(login, cookie=cookie)
        callout_map = grid.add_callouts(callouts)
    except Exception as e:  # noqa: BLE001
        return {"error": f"не удалось создать/найти уточнения через Grid: {str(e)[:180]}",
                "uses_direct_units": False}, 502
    callout_ids = list(callout_map.values())[:deps.callout_cap]
    if not callout_ids:
        return {"error": "Grid не вернул id уточнений", "uses_direct_units": False}, 422
    try:
        updated = grid.set_campaign_callouts(campaign_ids, callout_ids)
        failed = []
    except Exception as batch_error:  # noqa: BLE001
        updated = []
        failed = []
        for cid in campaign_ids:
            try:
                updated.extend(grid.set_campaign_callouts([cid], callout_ids))
            except Exception as e:  # noqa: BLE001
                failed.append({"campaign_id": cid, "error": str(e)[:220]})
        if not updated:
            return {
                "error": "Grid отклонил привязку уточнений",
                "details": str(batch_error)[:500],
                "failed_campaigns": failed[:40],
                "callout_ids": callout_ids,
                "uses_direct_units": False,
            }, 502
    updated_ids = []
    for row in updated or []:
        try:
            cid = int((row or {}).get("id") or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid > 0 and cid not in updated_ids:
            updated_ids.append(cid)
    return {
        "ok": True,
        "execute": True,
        "login": login,
        "callouts": callouts,
        "callout_ids": callout_ids,
        "attached_campaign_ids": updated_ids or campaign_ids,
        "attached": len(updated_ids or campaign_ids),
        "failed_campaigns": failed[:40],
        "transport": "grid",
        "uses_direct_units": False,
    }, 200 if not failed else 207


def execute_rename_repair(login: str, ctx: dict, campaign_names: dict[int, str],
                          deps: RepairDeps) -> tuple[dict, int]:
    """Rename existing campaigns through Grid without touching other fields."""
    clean: dict[int, str] = {}
    for raw_id, raw_name in (campaign_names or {}).items():
        try:
            cid = int(raw_id)
        except (TypeError, ValueError):
            continue
        name = str(raw_name or "").strip()
        if cid > 0 and name and cid not in clean:
            clean[cid] = name
    if not clean:
        return {"error": "нет campaign_id/name для переименования", "uses_direct_units": False}, 422
    body_obj = ctx.get("body") or {}
    acc = deps.account_ctx(login)
    if not acc:
        return {"error": f"аккаунт {login} не найден в БД"}, 404
    agency = (ctx.get("agency") or body_obj.get("agency") or acc.get("agency") or "").strip()
    try:
        client = cmc.build_client(login, account=(agency or None))
        cookie = client.sess.headers.get("Cookie") or ""
        grid = gf.GridClient(login, cookie=cookie)
        updated = grid.set_campaign_names(clean)
        failed = []
    except Exception as batch_error:  # noqa: BLE001
        try:
            client = cmc.build_client(login, account=(agency or None))
            cookie = client.sess.headers.get("Cookie") or ""
            grid = gf.GridClient(login, cookie=cookie)
        except Exception as e:  # noqa: BLE001
            return {"error": f"не удалось подобрать рабочую куку для rename-repair: {str(e)[:160]}",
                    "uses_direct_units": False}, 502
        updated = []
        failed = []
        for cid, name in clean.items():
            try:
                updated.extend(grid.set_campaign_names({cid: name}))
            except Exception as e:  # noqa: BLE001
                failed.append({"campaign_id": cid, "name": name, "error": str(e)[:220]})
        if not updated:
            return {
                "error": "Grid отклонил переименование кампаний",
                "details": str(batch_error)[:500],
                "failed_campaigns": failed[:40],
                "uses_direct_units": False,
            }, 502
    updated_ids = []
    for row in updated or []:
        try:
            cid = int((row or {}).get("id") or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid > 0 and cid not in updated_ids:
            updated_ids.append(cid)
    return {
        "ok": True,
        "execute": True,
        "login": login,
        "renamed_campaigns": [{"campaign_id": cid, "name": name} for cid, name in clean.items()],
        "updated_campaign_ids": updated_ids or list(clean.keys()),
        "updated": len(updated_ids or clean),
        "failed_campaigns": failed[:40],
        "transport": "grid",
        "uses_direct_units": False,
    }, 200 if not failed else 207


def execute_content_repair(login: str, ctx: dict, repairs: list[dict[str, Any]],
                           deps: RepairDeps) -> tuple[dict, int]:
    """Add missing text groups/ads to existing tp2/tp4 campaigns through Grid."""
    if not repairs:
        return {"error": "нет rebuild_missing_content действий", "uses_direct_units": False}, 422
    results: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for action in repairs:
        try:
            cid = int(action.get("campaign_id") or 0)
        except (TypeError, ValueError):
            cid = 0
        item = action.get("item") if isinstance(action.get("item"), dict) else {}
        name = str(action.get("name") or item.get("name") or "").strip()
        if cid <= 0:
            failed.append({"name": name, "error": "нет campaign_id"})
            continue
        try:
            if action.get("content_kind") == "shopping":
                content = deps.shopping_content_context(login, ctx, action)
                rep = gc.add_shopping_content_to_existing(
                    login,
                    campaign_id=cid,
                    groups=content.get("groups") or [],
                    feed_id=int(content.get("feed_id") or 0),
                    region_ids=content.get("region_ids") or [],
                    body_text=content.get("body_text") or "",
                    goal_id=int(content.get("goal_id") or 0),
                )
                ok = bool(rep.get("groups")) and bool(rep.get("shopping_ads")) and not rep.get("errors")
                row = {
                    "ok": ok,
                    "campaign_id": cid,
                    "name": name,
                    "groups": rep.get("groups") or 0,
                    "shopping_ads": rep.get("shopping_ads") or 0,
                    "listing_ads": rep.get("listing_ads") or 0,
                    "adgroup_ids": rep.get("adgroup_ids") or [],
                    "shopping_ad_ids": rep.get("shopping_ad_ids") or [],
                    "listing_ad_ids": rep.get("listing_ad_ids") or [],
                    "errors": (rep.get("errors") or [])[:5],
                }
            else:
                content = deps.text_content_context(login, ctx, action)
                rep = gc.add_text_content_to_existing(
                    login,
                    campaign_id=cid,
                    groups=content.get("groups") or [],
                    region_ids=content.get("region_ids") or [],
                    href=content.get("href") or "",
                    goal_id=int(content.get("goal_id") or 0),
                    autotargeting=bool(content.get("autotargeting")),
                    search_only=True,
                    price_map=content.get("price_map") or {},
                    brand_price_fn=content.get("brand_price_fn"),
                )
                ok = bool(rep.get("groups")) and bool(rep.get("ads")) and not rep.get("errors")
                row = {
                    "ok": ok,
                    "campaign_id": cid,
                    "name": name,
                    "groups": rep.get("groups") or 0,
                    "ads": rep.get("ads") or 0,
                    "adgroup_ids": rep.get("adgroup_ids") or [],
                    "ad_ids": rep.get("ad_ids") or [],
                    "errors": (rep.get("errors") or [])[:5],
                }
            results.append(row)
            if not ok:
                failed.append({"campaign_id": cid, "name": name, "errors": row["errors"]})
        except Exception as e:  # noqa: BLE001
            failed.append({"campaign_id": cid, "name": name, "error": str(e)[:220]})
    repaired_ids = [r["campaign_id"] for r in results if r.get("ok")]
    if not repaired_ids:
        return {
            "error": "не удалось добить content ни в одной кампании",
            "results": results,
            "failed_campaigns": failed[:40],
            "transport": "grid",
            "uses_direct_units": False,
        }, 502
    return {
        "ok": not failed,
        "execute": True,
        "login": login,
        "repaired_campaign_ids": repaired_ids,
        "repaired": len(repaired_ids),
        "results": results,
        "failed_campaigns": failed[:40],
        "transport": "grid",
        "uses_direct_units": False,
    }, 200 if not failed else 207


def _tp_of(name: str) -> int | None:
    m = _TP_RE.match(str(name or ""))
    return int(m.group(1)) if m else None


def _ct_of(name: str) -> str:
    m = _CT_RE.search(str(name or ""))
    return m.group(0).lower() if m else "ct0000"


def _autotarget_ok(rm: dict | None) -> bool:
    """Профиль автотаргета поиска корректен: активен + EXACT_V2_MARK + WITHOUT_BRAND (без лишних)."""
    if not isinstance(rm, dict) or not rm.get("isActive"):
        return False
    cats = {str(x).upper() for x in (rm.get("relevanceMatchCategories") or [])}
    brands = {str(x).upper() for x in (rm.get("autotargetingBrandSettings") or [])}
    return cats == {"EXACT_V2_MARK"} and brands == {"WITHOUT_BRAND"}


def execute_keywords_repair(login: str, ctx: dict, campaign_ids: list[int],
                            deps: RepairDeps) -> tuple[dict, int]:
    """Fix two silent defects on EXISTING search campaigns via cookie/Grid (no Direct units):
    (1) search adgroup without keyword phrases; (2) wrong autotargeting profile.

    Read-modify-write through GridClient.groups_for_edit + update_unified_adgroups: the full
    group object is round-tripped (regions/minus-words/tracking preserved) and only keywords +
    relevanceMatch are corrected. Idempotent: groups already holding keywords AND the correct
    EXACT_V2_MARK/WITHOUT_BRAND profile are skipped. Only tp2/tp4/tp5 (search) campaigns are
    touched; groups carrying group-level bid modifiers or retargetings are skipped for safety."""
    campaign_ids = _unique_positive_ints(campaign_ids)
    if not campaign_ids:
        return {"error": "нет campaign_id для keyword-repair", "uses_direct_units": False}, 422
    if deps.group_keywords_context is None:
        return {"error": "group_keywords_context не прокинут в RepairDeps", "uses_direct_units": False}, 422
    body_obj = ctx.get("body") or {}
    acc = deps.account_ctx(login)
    if not acc:
        return {"error": f"аккаунт {login} не найден в БД", "uses_direct_units": False}, 404
    agency = (ctx.get("agency") or body_obj.get("agency") or acc.get("agency") or "").strip()
    try:
        client = cmc.build_client(login, account=(agency or None))
        cookie = client.sess.headers.get("Cookie") or ""
        grid = gf.GridClient(login, cookie=cookie)
    except Exception as e:  # noqa: BLE001
        return {"error": f"не удалось подобрать рабочую куку для keyword-repair: {str(e)[:160]}",
                "uses_direct_units": False}, 502

    results: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    write_items: list[dict[str, Any]] = []
    intents: dict[int, dict[str, Any]] = {}     # adgroup_id -> intent row (для отчёта)
    skipped = 0

    for cid in campaign_ids:
        try:
            groups = grid.groups_for_edit(cid)
        except Exception as e:  # noqa: BLE001
            failed.append({"campaign_id": cid, "error": f"чтение групп: {str(e)[:200]}"})
            continue
        for grp in groups:
            gid = int(grp.get("adgroup_id") or 0)
            camp_name = grp.get("campaign_name") or ""
            tp = _tp_of(camp_name)
            if tp not in _SEARCH_TPS or not grp.get("supported"):
                skipped += 1
                continue
            if grp.get("retargetings_present") or grp.get("bid_modifiers_present"):
                results.append({"ok": True, "skipped": "unsafe (retargetings/bidModifiers)",
                                "campaign_id": cid, "adgroup_id": gid})
                skipped += 1
                continue
            rm = grp.get("relevance_match")
            need_kw = int(grp.get("keyword_count") or 0) < _KEYWORDS_MIN
            need_at = not _autotarget_ok(rm)
            if not need_kw and not need_at:
                skipped += 1
                continue
            final_kw = list(grp.get("keywords") or [])
            recomputed = 0
            if need_kw:
                try:
                    meta = {"campaign_id": cid, "campaign_name": camp_name,
                            "tp_code": f"tp{tp}", "adgroup_name": grp.get("adgroup_name") or "",
                            "ct": _ct_of(grp.get("adgroup_name") or "")}
                    kw_ctx = deps.group_keywords_context(login, ctx, meta) or {}
                    new_kw = [str(k) for k in (kw_ctx.get("keywords") or []) if str(k).strip()]
                    if new_kw:
                        final_kw = new_kw
                        recomputed = len(new_kw)
                except Exception as e:  # noqa: BLE001
                    failed.append({"campaign_id": cid, "adgroup_id": gid,
                                   "error": f"пересчёт ключей: {str(e)[:180]}"})
                    continue
            target_rm = {"isActive": True, "id": (rm or {}).get("id") if isinstance(rm, dict) else None,
                         "relevanceMatchCategories": ["EXACT_V2_MARK"],
                         "autotargetingBrandSettings": ["WITHOUT_BRAND"]}
            try:
                item = grid.build_update_item(grp, keywords=final_kw, relevance_match=target_rm)
            except Exception as e:  # noqa: BLE001
                failed.append({"campaign_id": cid, "adgroup_id": gid,
                               "error": f"сборка тела: {str(e)[:180]}"})
                continue
            write_items.append(item)
            intents[gid] = {"campaign_id": cid, "adgroup_id": gid,
                            "fixed_keywords": bool(need_kw and recomputed),
                            "fixed_autotarget": bool(need_at),
                            "keywords_written": len(final_kw)}

    updated_ids: list[int] = []
    if write_items:
        try:
            updated_ids = grid.update_unified_adgroups(write_items)
        except Exception as e:  # noqa: BLE001
            return {
                "error": f"Grid отклонил UpdateUnifiedAdGroups: {str(e)[:240]}",
                "attempted": len(write_items),
                "failed_campaigns": failed[:40],
                "transport": "grid",
                "uses_direct_units": False,
            }, 502

    updated_set = set(updated_ids)
    for gid, intent in intents.items():
        intent["applied"] = gid in updated_set
        results.append(intent)
    applied = [i for i in intents.values() if i.get("applied")]
    not_applied = [i for i in intents.values() if not i.get("applied")]
    if not_applied:
        failed.extend({"campaign_id": i["campaign_id"], "adgroup_id": i["adgroup_id"],
                       "error": "группа не подтверждена в updatedAdGroupItems"} for i in not_applied)

    ok = not failed
    if not write_items and not failed:
        return {
            "ok": True,
            "execute": True,
            "login": login,
            "note": "нет групп для keyword-repair (всё уже корректно/идемпотентно)",
            "skipped_groups": skipped,
            "transport": "grid",
            "uses_direct_units": False,
        }, 200
    repaired_campaign_ids = sorted({i["campaign_id"] for i in applied})
    return {
        "ok": ok,
        "execute": True,
        "login": login,
        "repaired_adgroups": len(applied),
        "repaired_campaign_ids": repaired_campaign_ids,
        "updated_adgroup_ids": updated_ids[:80],
        "skipped_groups": skipped,
        "results": results[:80],
        "failed_campaigns": failed[:40],
        "transport": "grid",
        "uses_direct_units": False,
    }, (200 if ok else 207 if applied else 502)

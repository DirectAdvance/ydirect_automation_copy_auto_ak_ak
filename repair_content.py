"""Content-domain repair executors: promo, callouts, rename, text/shopping rebuild.

Functions here operate on campaign content through cookie/Grid (no Direct API units).
All shared state (cmc/gc/gf aliases, RepairDeps, helpers) is imported from repair_common.
"""
from __future__ import annotations

from typing import Any

from .repair_common import RepairDeps, _unique_positive_ints, cmc, gc, gf


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


def _attach_group_shortfall(row: dict[str, Any], rep: dict[str, Any]) -> list[dict[str, Any]]:
    """Прокинуть гейт «создано групп ≠ отправлено» из grid_create в строку репейра.

    `_gate_groups_created` кладёт факт в ``rep["groups_expected"]``/``["groups_shortfall"]``/
    ``["warnings"]``, но строка репейра эти поля ВЫБРАСЫВАЛА → «добито 13 групп из 14» проходило
    молча, ровно то состояние, ради которого гейт заводили. Гоняем ТОТ ЖЕ верификатор, что и
    create_full-путь, чтобы код `GROUPS_CREATED_LESS_THAN_SENT` доезжал и здесь.

    ⛔ Report-only: `row["ok"]` НЕ трогаем. 13 рабочих групп из 14 подлежат ДОБИВКЕ; перевод строки
    в failed увёл бы кампанию в пересоздание — та же ошибка, что «расхождение в rep['errors']»
    (ERRORS_JOURNAL: GRID_CREATE_RETRY_DUPLICATES_ADGROUPS, «НЕ помогло ранее»).
    """
    from .local_result_verifier import verify_local_result

    row["groups_expected"] = rep.get("groups_expected")
    if rep.get("groups_shortfall"):
        row["groups_shortfall"] = rep.get("groups_shortfall")
    warnings = [str(w) for w in (rep.get("warnings") or [])][:5]
    if warnings:
        row["warnings"] = warnings
    build = {"groups": row.get("groups") or 0, "groups_expected": rep.get("groups_expected")}
    if "ads" in row:
        build["ads"] = row.get("ads") or 0
    if "shopping_ads" in row:
        build["shopping_ads"] = row.get("shopping_ads") or 0
        build["listing_ads"] = row.get("listing_ads") or 0
    issues = verify_local_result({"name": row.get("name") or "", "id": row.get("campaign_id"),
                                  "result": {"campaign_id": row.get("campaign_id"), "build": build}})
    if issues:
        row["issues"] = issues
    return issues


def execute_content_repair(login: str, ctx: dict, repairs: list[dict[str, Any]],
                           deps: RepairDeps) -> tuple[dict, int]:
    """Add missing text groups/ads to existing tp2/tp4 campaigns through Grid."""
    if not repairs:
        return {"error": "нет rebuild_missing_content действий", "uses_direct_units": False}, 422
    results: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    verification_issues: list[dict[str, Any]] = []
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
                    # контракт ad_ids стал 1:1 с группами (None для упавших) — в отчёт компакт
                    "ad_ids": [x for x in (rep.get("ad_ids") or []) if x],
                    "errors": (rep.get("errors") or [])[:5],
                }
            verification_issues.extend(_attach_group_shortfall(row, rep))
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
            # расхождение «создано ≠ отправлено» — видимое, но НЕ разрушительное (report-only)
            "verification_issues": verification_issues[:40],
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
        "verification_issues": verification_issues[:40],
        "transport": "grid",
        "uses_direct_units": False,
    }, 200 if not failed else 207

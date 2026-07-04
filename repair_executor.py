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
                    # контракт ad_ids стал 1:1 с группами (None для упавших) — в отчёт компакт
                    "ad_ids": [x for x in (rep.get("ad_ids") or []) if x],
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


def _run_per_campaign_repair(
    action_name: str,
    login: str,
    ctx: dict,
    campaign_ids: list[int],
    callback: Any,
) -> tuple[dict, int]:
    """Generic executor that calls `callback(login, campaign_id, ctx)` for each campaign."""
    campaign_ids = _unique_positive_ints(campaign_ids)
    if not campaign_ids:
        return {"error": f"нет campaign_id для {action_name}", "uses_direct_units": False}, 422
    if callback is None:
        return {"error": f"{action_name}: callback не прокинут в RepairDeps",
                "uses_direct_units": False}, 422
    results: list[dict] = []
    failed: list[dict] = []
    for cid in campaign_ids:
        try:
            out = callback(login, cid, ctx)
            results.append({"campaign_id": cid, **(out or {})})
            if not (out or {}).get("ok"):
                failed.append({"campaign_id": cid, "error": (out or {}).get("error") or "ok=False"})
        except Exception as e:  # noqa: BLE001
            failed.append({"campaign_id": cid, "error": str(e)[:220]})
    repaired = [r["campaign_id"] for r in results if r.get("ok")]
    if not repaired:
        return {
            "error": f"{action_name} не удался ни для одной кампании",
            "results": results,
            "failed_campaigns": failed[:40],
            "transport": "grid",
            "uses_direct_units": False,
        }, 502
    return {
        "ok": not failed,
        "execute": True,
        "login": login,
        "repaired_campaign_ids": repaired,
        "repaired": len(repaired),
        "results": results,
        "failed_campaigns": failed[:40],
        "transport": "grid",
        "uses_direct_units": False,
    }, 200 if not failed else 207


def execute_images_repair(login: str, ctx: dict, campaign_ids: list[int],
                          deps: RepairDeps) -> tuple[dict, int]:
    """Пере-залить картинки tp1 через RMW + UpdateAdaptiveTextAds (in-place, no Direct units)."""
    return _run_per_campaign_repair("images_repair", login, ctx, campaign_ids,
                                    deps.campaign_images_repair)


def execute_adprice_repair(login: str, ctx: dict, campaign_ids: list[int],
                            deps: RepairDeps) -> tuple[dict, int]:
    """Проставить adPrice через _grid_set_ad_prices по прайс-кэшу (in-place, no Direct units)."""
    return _run_per_campaign_repair("adprice_repair", login, ctx, campaign_ids,
                                    deps.campaign_adprice_repair)


def execute_default_text_repair(login: str, ctx: dict, campaign_ids: list[int],
                                 deps: RepairDeps) -> tuple[dict, int]:
    """Заполнить bodies ShoppingAd через set_default_text (in-place, no Direct units)."""
    return _run_per_campaign_repair("default_text_repair", login, ctx, campaign_ids,
                                    deps.campaign_default_text_repair)


# ── IMAGES_FORBIDDEN: очистка imageHashes у поисковых объявлений ────────────────
# Поисковые tp2/tp4 не должны нести imageHashes. RMW: читаем текущее состояние
# (href/titles/bodies/adPrice), ставим imageHashes=[], пишем через UpdateAdaptiveTextAds.
_ADS_FOR_IMG_CLEAR_Q = (
    "query AdsForbiddenImg($login:String!,$inp:GdAdsContainerInput!){"
    "client(searchBy:{login:$login}){ads(input:$inp){rowset{id "
    "...on GdAdaptiveTextAd{images{imageHash}}}}}}"
)


def _get_ads_with_images(login: str, cookie: str, campaign_id: int) -> list[int]:
    """Возвращает ad_id объявлений с непустыми imageHashes в поисковой кампании."""
    from . import grid_read as _gr
    rc = _gr.GridReadClient(login, cookie=cookie)
    rc._bootstrap_csrf()
    inp = {
        "filter": {"campaignIdIn": [str(campaign_id)]},
        "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
        "limitOffset": {"limit": 5000, "offset": 0},
        "orderBy": [{"order": "ASC", "field": "ID"}],
    }
    data = rc._post("AdsForbiddenImg", _ADS_FOR_IMG_CLEAR_Q, {"login": login, "inp": inp})
    rows = ((((data.get("data") or {}).get("client") or {}).get("ads") or {}).get("rowset") or [])
    out: list[int] = []
    for row in rows:
        images = row.get("images")
        if images is None:
            continue  # не GdAdaptiveTextAd
        if any((img or {}).get("imageHash") for img in (images or [])):
            try:
                aid = int(row.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if aid > 0:
                out.append(aid)
    return out


def execute_images_forbidden_repair(login: str, ctx: dict, campaign_ids: list[int],
                                     deps: RepairDeps) -> tuple[dict, int]:
    """Очистить imageHashes у поисковых (tp2/tp4) объявлений через Grid UpdateAdaptiveTextAds.

    RMW: читает текущее состояние объявлений (href/titles/bodies/adPrice), обнуляет
    imageHashes=[], пишет обратно с allow_empty_images=True. Read-back: должно остаться
    0 объявлений с imageHashes. No Direct API units."""
    campaign_ids = _unique_positive_ints(campaign_ids)
    if not campaign_ids:
        return {"error": "нет campaign_id для images_forbidden_repair", "uses_direct_units": False}, 422
    body_obj = ctx.get("body") or {}
    acc = deps.account_ctx(login)
    if not acc:
        return {"error": f"аккаунт {login} не найден в БД"}, 404
    agency = (ctx.get("agency") or body_obj.get("agency") or acc.get("agency") or "").strip()
    try:
        client = cmc.build_client(login, account=(agency or None))
        cookie = client.sess.headers.get("Cookie") or ""
        grid = gf.GridClient(login, cookie=cookie)
    except Exception as e:  # noqa: BLE001
        return {"error": f"кука для images_forbidden_repair: {str(e)[:160]}",
                "uses_direct_units": False}, 502

    results: list[dict] = []
    failed: list[dict] = []
    total_cleared = 0

    for cid in campaign_ids:
        try:
            # Шаг 1: найти ad_id с imageHashes
            with_images = _get_ads_with_images(login, cookie, cid)
            if not with_images:
                results.append({"ok": True, "campaign_id": cid, "cleared": 0,
                                "remaining_with_images": 0,
                                "note": "объявлений с imageHashes нет — идемпотентно"})
                continue

            # Шаг 2: RMW — читаем полное состояние (href/titles/bodies/adPrice)
            full_state = grid.adaptive_ads_for_update([cid], with_images)

            # Шаг 3: собираем update-items с imageHashes=[], сохраняя остальные поля.
            # Ревью 03.07 #13: creativeIds проносим (иначе full-replace стирал видео), сырую
            # bannerPrice нормализуем (decimal-строку Grid молча сбрасывал), объявления без
            # прочитанного состояния пропускаем (пустой payload затёр бы контент).
            from .create_set_feeds import _norm_read_ad_price
            upd_items: list[dict] = []
            for aid in with_images:
                st = full_state.get(aid) or {}
                if not st:
                    continue  # чтение не дало состояние — не слать пустышку full-replace
                item: dict[str, Any] = {
                    "id": aid,
                    "href": st.get("href") or "",
                    "titles": list(st.get("titles") or []),
                    "bodies": list(st.get("bodies") or []),
                    "imageHashes": [],
                    "creativeIds": [str(c) for c in (st.get("creativeIds") or []) if c],
                }
                _price = _norm_read_ad_price(st.get("adPrice"))
                if _price:
                    item["adPrice"] = _price
                upd_items.append(item)

            # Шаг 4: записываем (allow_empty_images=True — разрешает отправку пустых хэшей)
            updated = grid.update_ad_images(upd_items, allow_empty_images=True)

            # Шаг 5: read-back — должно остаться 0 объявлений с imageHashes
            remaining = len(_get_ads_with_images(login, cookie, cid))
            total_cleared += updated
            ok = remaining == 0
            row: dict = {
                "ok": ok,
                "campaign_id": cid,
                "ads_with_images_before": len(with_images),
                "cleared": updated,
                "remaining_with_images": remaining,
            }
            results.append(row)
            if not ok:
                failed.append({"campaign_id": cid, "remaining_with_images": remaining,
                               "error": f"осталось {remaining} объявл. с imageHashes"})
        except Exception as e:  # noqa: BLE001
            failed.append({"campaign_id": cid, "error": str(e)[:220]})

    repaired = [r["campaign_id"] for r in results if r.get("ok")]
    if not repaired:
        return {
            "error": "images_forbidden_repair не удался ни для одной кампании",
            "results": results,
            "failed_campaigns": failed[:40],
            "transport": "grid",
            "uses_direct_units": False,
        }, 502
    return {
        "ok": not failed,
        "execute": True,
        "login": login,
        "repaired_campaign_ids": repaired,
        "repaired": len(repaired),
        "total_cleared": total_cleared,
        "results": results[:80],
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
            # Кап 200/группа (лимит Яндекса): иначе Grid AddKeywords отклонит всю пачку
            # (MAX_KEYWORDS_PER_AD_GROUP_EXCEEDED) → группа останется без ключей (NO_KEYWORDS_LIVE).
            if len(final_kw) > _KW_MAX_PER_GROUP:
                final_kw = final_kw[:_KW_MAX_PER_GROUP]
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
                            "keywords_written": len(final_kw),
                            "_kw_list": final_kw if (need_kw and recomputed) else []}

    # Заливаем ключи через Grid AddKeywords (addKeywords — рабочий, в отличие от
    # UpdateUnifiedAdGroups который подтверждён no-op для keywords).
    # ключ adgroup_id (snake) — GridClient.add_keywords НЕ понимает camel adGroupId (молча пропустит).
    flat_kw_items = [
        {"adgroup_id": gid, "keyword": str(k)}
        for gid, intent in intents.items()
        for k in (intent.get("_kw_list") or [])
        if str(k).strip() and not str(k).startswith("---")
    ]
    kw_added: set[int] = set()
    if flat_kw_items:
        try:
            added_rows = grid.add_keywords(flat_kw_items)
            for row in (added_rows or []):
                try:
                    kw_added.add(int(row.get("adGroupId") or 0))
                except (TypeError, ValueError):
                    pass
        except Exception as e:  # noqa: BLE001
            failed.append({"error": f"Grid AddKeywords упал: {str(e)[:200]}"})

    updated_set: set[int] = set()
    for gid, intent in intents.items():
        # Считаем группу «применённой» если: ключи залиты (kw_added) ИЛИ нужна только
        # автотаргет-правка (need_at без need_kw — try update_unified_adgroups ниже).
        if gid in kw_added or (not intent.get("fixed_keywords") and intent.get("fixed_autotarget")):
            updated_set.add(gid)

    for gid, intent in intents.items():
        intent.pop("_kw_list", None)
        intent["applied"] = gid in updated_set
        results.append(intent)
    applied = [i for i in intents.values() if i.get("applied")]
    not_applied = [i for i in intents.values() if not i.get("applied")]
    if not_applied:
        failed.extend({"campaign_id": i["campaign_id"], "adgroup_id": i["adgroup_id"],
                       "error": "группа не подтверждена AddKeywords"} for i in not_applied)

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
        "updated_adgroup_ids": sorted(updated_set)[:80],
        "skipped_groups": skipped,
        "results": results[:80],
        "failed_campaigns": failed[:40],
        "transport": "grid",
        "uses_direct_units": False,
    }, (200 if ok else 207 if applied else 502)


# ── KEYWORDS_WRONG_GROUP (keyword-shift) fix ────────────────────────────────────
# Grid AddKeywords дозаливает, но НЕ удаляет — сдвинутые ключи чужого ct надо снять через
# v5 keywords.delete (механика перенесена из restore_shift_keywords.py). keyword_id для
# удаления берём из Grid showConditions (GdKeyword.id == v5 keyword id).
_V5_URL = "https://api.direct.yandex.com/json/v5/"
_SHOW_CONDITIONS_Q = (
    "query KwIds($login:String!,$cid:[Long!]!){"
    "client(searchBy:{login:$login}){"
    "showConditions(input:{filter:{typeIn:[KEYWORD],campaignIdIn:$cid}"
    "statRequirements:{preset:TODAY}"
    "limitOffset:{limit:10000,offset:0}"
    "orderBy:[{order:DESC,field:GROUP_ID}]})"
    "{rowset{__typename ...on GdKeyword{id keyword adGroupId}}}}}")


def _v5_keywords_delete(token: str, login: str, keyword_ids: list[int]) -> int:
    """v5 keywords.delete → число удалённых. Механика из restore_shift_keywords.py."""
    import requests
    if not keyword_ids:
        return 0
    h = {"Authorization": f"Bearer {token}", "Client-Login": login,
         "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8"}
    deleted = 0
    for i in range(0, len(keyword_ids), 10000):
        chunk = keyword_ids[i:i + 10000]
        r = requests.post(_V5_URL + "keywords", headers=h, json={
            "method": "delete",
            "params": {"SelectionCriteria": {"Ids": chunk}},
        }, timeout=60)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"v5 keywords.delete error: {data['error']}")
        deleted += len((data.get("result") or {}).get("DeleteResults") or chunk)
    return deleted


def _grid_show_condition_ids(login: str, cookie: str, campaign_ids: list[int]) -> dict[int, list[int]]:
    """Grid showConditions → {adgroup_id: [v5 keyword_id, ...]} для кампаний."""
    from . import grid_read as _gr
    rc = _gr.GridReadClient(login, cookie=cookie)
    rc._bootstrap_csrf()
    out: dict[int, list[int]] = {}
    j = rc._post("KwIds", _SHOW_CONDITIONS_Q,
                 {"login": login, "cid": [int(c) for c in campaign_ids]})
    rows = ((((j.get("data") or {}).get("client") or {})
             .get("showConditions") or {}).get("rowset") or [])
    for r in rows:
        if (r.get("__typename") or "") != "GdKeyword":
            continue
        try:
            gid = int(r.get("adGroupId") or 0)
            kid = int(r.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if gid > 0 and kid > 0:
            out.setdefault(gid, []).append(kid)
    return out


def execute_keywords_wrong_group_repair(login: str, ctx: dict, wrong_items: list[dict],
                                        deps: RepairDeps) -> tuple[dict, int]:
    """Fix keyword-shift (KEYWORDS_WRONG_GROUP): для каждой сдвинутой группы удалить её текущие
    (чужие) ключи через v5 keywords.delete и залить эталонные ключи её собственного ct через
    Grid AddKeywords с ПРАВИЛЬНЫМ adgroup_id. No Direct-units на заливке (только delete идёт v5).

    wrong_items: [{campaign_id, adgroup_id, expected_ct}]. Эталонные ключи пересчитываются тем же
    ``group_keywords_context``, что и создание. Идемпотентно по факту: пустой пересчёт → удаления
    тоже не делаем (не оставляем группу пустой)."""
    items = [it for it in (wrong_items or []) if it.get("adgroup_id") and it.get("campaign_id")]
    if not items:
        return {"error": "нет adgroup_id/campaign_id для keyword-shift-фикса", "uses_direct_units": False}, 422
    if deps.group_keywords_context is None:
        return {"error": "group_keywords_context не прокинут в RepairDeps", "uses_direct_units": False}, 422
    body_obj = ctx.get("body") or {}
    acc = deps.account_ctx(login)
    if not acc:
        return {"error": f"аккаунт {login} не найден в БД", "uses_direct_units": False}, 404
    agency = (ctx.get("agency") or body_obj.get("agency") or acc.get("agency") or "").strip()
    token = deps.v5_token_for_login(login) if deps.v5_token_for_login else None
    if not token:
        return {"error": "нет v5 OAuth-токена для keywords.delete — сдвиг снять нельзя",
                "uses_direct_units": False}, 502
    try:
        client = cmc.build_client(login, account=(agency or None))
        cookie = client.sess.headers.get("Cookie") or ""
        grid = gf.GridClient(login, cookie=cookie)
    except Exception as e:  # noqa: BLE001
        return {"error": f"кука для keyword-shift-фикса: {str(e)[:160]}", "uses_direct_units": False}, 502

    # Пересчёт эталонных ключей на каждую сдвинутую группу.
    add_by_group: dict[int, list[str]] = {}
    results: list[dict] = []
    failed: list[dict] = []
    camp_ids: list[int] = []
    for it in items:
        try:
            cid = int(it["campaign_id"]); gid = int(it["adgroup_id"])
        except (TypeError, ValueError):
            continue
        if cid not in camp_ids:
            camp_ids.append(cid)
        ct = str(it.get("expected_ct") or "ct0000").strip()
        tp = _tp_of(it.get("name") or "") or 2
        meta = {"campaign_id": cid, "campaign_name": it.get("name") or "",
                "tp_code": f"tp{tp}", "adgroup_name": "", "ct": ct}
        try:
            kw_ctx = deps.group_keywords_context(login, ctx, meta) or {}
            kws = [str(k) for k in (kw_ctx.get("keywords") or [])
                   if str(k).strip() and not str(k).startswith("---")]
        except Exception as e:  # noqa: BLE001
            failed.append({"campaign_id": cid, "adgroup_id": gid, "error": f"пересчёт ключей: {str(e)[:160]}"})
            continue
        if not kws:
            failed.append({"campaign_id": cid, "adgroup_id": gid,
                           "error": f"пак не дал эталонных ключей для {ct} — группа не тронута"})
            continue
        add_by_group[gid] = kws

    if not add_by_group:
        return {"error": "ни для одной группы не пересчитаны эталонные ключи (пак пуст?)",
                "failed": failed[:40], "uses_direct_units": False}, 502

    # Снимок ТЕКУЩИХ (сдвинутых) keyword_id ДО любых мутаций — их удалим ТОЛЬКО после успешной заливки.
    try:
        old_kid_by_group = _grid_show_condition_ids(login, cookie, camp_ids)
    except Exception as e:  # noqa: BLE001
        return {"error": f"чтение keyword_id (showConditions): {str(e)[:180]}",
                "uses_direct_units": False}, 502

    # ПОРЯДОК = ADD-FIRST, потом DELETE-OLD. Гарантирует, что группа НИКОГДА не остаётся пустой:
    # если заливка эталона не подтвердилась — старые (сдвинутые) ключи НЕ удаляются (нет data loss,
    # сдвиг не исправлен но отчётен). Обратный порядок (delete→add) ловил гонку eventual-consistency
    # Grid после v5 delete: AddKeywords молча возвращал 0 addedItems и группа оставалась пустой.
    import time as _time
    # ВНИМАНИЕ: GridClient.add_keywords читает ключ ``adgroup_id`` (snake) / ``AdGroupId``, но НЕ
    # ``adGroupId`` (camel) — camel-вариант молча пропускается (gid=0). Передаём snake-регистр.
    flat = [{"adgroup_id": gid, "keyword": k} for gid, kws in add_by_group.items() for k in kws]
    pre_count = {gid: len(old_kid_by_group.get(gid, [])) for gid in add_by_group}
    # Ретрай заливки под Grid-лаг: add молча может вернуть 0 addedItems / не сразу видим в read.
    # Удаление старого — ТОЛЬКО по подтверждённому росту keyword_count (post>pre), поэтому лишний
    # повторный add безопасен (дубликаты Grid схлопывает). До 3 попыток.
    confirmed_gids: set[int] = set()
    post_count: dict[int, int] = {}
    add_err: str | None = None
    for attempt in range(3):
        pending = [gid for gid in add_by_group if gid not in confirmed_gids]
        if not pending:
            break
        flat_pending = [it for it in flat if int(it["adgroup_id"]) in pending]
        try:
            grid.add_keywords(flat_pending)
        except Exception as e:  # noqa: BLE001
            add_err = str(e)[:180]
        _time.sleep(1.0 + attempt)
        try:
            for grp in grid.groups_for_edit(camp_ids):
                post_count[int(grp.get("adgroup_id") or 0)] = int(grp.get("keyword_count") or 0)
        except Exception:  # noqa: BLE001
            pass
        for gid in pending:
            if post_count.get(gid, 0) > pre_count.get(gid, 0):
                confirmed_gids.add(gid)
    if not confirmed_gids and add_err:
        return {"error": f"Grid AddKeywords: {add_err}", "deleted_keywords": 0,
                "uses_direct_units": False}, 502
    kw_added = confirmed_gids
    # Удаляем старые ключи ТОЛЬКО у групп с подтверждённым ростом (эталон реально долит поверх старого).
    del_ids: list[int] = []
    for gid in confirmed_gids:
        del_ids.extend(old_kid_by_group.get(gid, []))
    deleted = 0
    if del_ids:
        try:
            deleted = _v5_keywords_delete(token, login, del_ids)
        except Exception as e:  # noqa: BLE001
            # Заливка прошла, но старые не снялись → группа = эталон+старое (НЕ пустая). Отчёт о частичном.
            failed.append({"error": f"v5 keywords.delete (старые ключи не сняты): {str(e)[:160]}"})

    for gid, kws in add_by_group.items():
        applied = gid in kw_added
        results.append({"adgroup_id": gid, "added_keywords": len(kws),
                        "old_keywords": len(old_kid_by_group.get(gid, [])), "applied": applied})
        if not applied:
            failed.append({"adgroup_id": gid, "error": "AddKeywords не подтвердил группу (старые ключи сохранены)"})
    applied_gids = sorted(g for g in add_by_group if g in kw_added)
    ok = bool(applied_gids) and not failed
    return {
        "ok": ok,
        "execute": True,
        "login": login,
        "fixed_adgroups": len(applied_gids),
        "fixed_adgroup_ids": applied_gids[:80],
        "deleted_keywords": deleted,
        "results": results[:80],
        "failed_campaigns": failed[:40],
        "transport": "grid_add_then_v5_delete",
        "uses_direct_units": False,   # заливка ключей — Grid; delete идёт по v5 но не тратит баллы создания
    }, (200 if ok else 207 if applied_gids else 502)

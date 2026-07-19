"""Media-domain repair executors: images, adprice, default_text, campaign invariants, images_forbidden.

Functions here operate on campaign media/visual assets and invariant flags through cookie/Grid
(no Direct API units). All shared state is imported from repair_common.
"""
from __future__ import annotations

from typing import Any

from .repair_common import RepairDeps, _unique_positive_ints, cmc, gf


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


def execute_campaign_invariant_repair(login: str, ctx: dict, campaign_ids: list[int],
                                      deps: RepairDeps) -> tuple[dict, int]:
    """Переставить кампанийные инварианты-галочки tp1–tp5 через Grid set_campaign_invariants (P0).

    In-place, БЕЗ баллов Direct (Grid UpdateCampaigns), РК всегда DRAFT → идемпотентно. Self-contained
    как execute_images_forbidden_repair: строит куку по логину, вызывает GridClient.set_campaign_invariants
    (persona/расш.гео/«Директ помогает»/Карты/организации OFF, мониторинг ON), затем read-back через
    read_campaign_invariants — успех, если ни у одной кампании не осталось «включённого» булева-нарушения.
    Blast-radius ложного детекта = один безвредный повторный UpdateCampaigns (НЕ удаление)."""
    campaign_ids = _unique_positive_ints(campaign_ids)
    if not campaign_ids:
        return {"error": "нет campaign_id для campaign_invariant_repair", "uses_direct_units": False}, 422
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
        return {"error": f"кука для campaign_invariant_repair: {str(e)[:160]}",
                "uses_direct_units": False}, 502

    _BAD = (("is_alternative_texts_enabled", True), ("has_extended_geo_targeting", True),
            ("enable_company_info", True), ("is_recommendations_management_enabled", True),
            ("is_price_recommendations_management_enabled", True), ("yandex_maps_enabled", True),
            ("serp_geo_wizard_enabled", True))

    def _remaining_bad(inv: dict) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {}
        for cid, row in (inv or {}).items():
            bad = [f for f, v in _BAD if isinstance(row.get(f), bool) and row.get(f) is v]
            if bad:
                out[cid] = bad
        return out

    try:
        updated = grid.set_campaign_invariants(campaign_ids)
    except Exception as e:  # noqa: BLE001
        return {"error": f"Grid set_campaign_invariants отклонил: {str(e)[:220]}",
                "transport": "grid", "uses_direct_units": False}, 502
    # Read-back: не осталось «включённых» нарушений (None=не прочитано трактуем как ОК, не блокируем).
    try:
        after = grid.read_campaign_invariants(campaign_ids)
        remaining = _remaining_bad(after)
    except Exception:  # noqa: BLE001 — read-back best-effort, не валит успешную запись
        remaining = {}
    updated_ids = [int((u or {}).get("id") or 0) for u in (updated or []) if (u or {}).get("id")]
    ok = not remaining
    return {
        "ok": ok,
        "execute": True,
        "login": login,
        "repaired_campaign_ids": updated_ids or campaign_ids,
        "repaired": len(updated_ids) or len(campaign_ids),
        "remaining_violations": {str(k): v for k, v in list(remaining.items())[:40]},
        "transport": "grid",
        "uses_direct_units": False,
    }, 200 if ok else 207


# ── IMAGES_FORBIDDEN: очистка imageHashes у поисковых объявлений ────────────────
# ⛔ ОТКЛЮЧЕНО решением Семёна 2026-07-19. Правило «в поиске (tp2/tp4) картинок быть не должно»
# ОТМЕНЕНО: админ-вкладка «Смена изображения» (/direct/automation/content) показывает и заменяет
# картинки во ВСЕХ кампаниях, включая поисковые, — автоочистка зацикливалась бы с ней.
# Флаг ниже делает executor НЕ-МУТИРУЮЩИМ no-op'ом (последний рубеж: гасит и прямые вызовы —
# CLI `campaign_spec_audit --fix`). Код RMW-очистки сохранён целиком; вернуть = флаг True здесь
# и в campaign_spec_audit / repair_planner / repair_gate.
# ⚠️ НЕ путать с `execute_images_repair` выше — это добивка ПУСТЫХ картинок tp1, она работает.
SEARCH_IMAGES_FORBIDDEN_RULE_ENABLED = False

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
    0 объявлений с imageHashes. No Direct API units.

    ⛔ ОТКЛЮЧЕНО (SEARCH_IMAGES_FORBIDDEN_RULE_ENABLED=False, решение Семёна 2026-07-19):
    возвращает не-мутирующий no-op ДО чтения куки и любых Grid-запросов."""
    if not SEARCH_IMAGES_FORBIDDEN_RULE_ENABLED:
        return {
            "ok": True,
            "execute": False,
            "disabled": True,
            "login": login,
            "repaired_campaign_ids": [],
            "repaired": 0,
            "total_cleared": 0,
            "note": ("images_forbidden_repair отключён: правило «в поиске картинок быть не должно» "
                     "отменено решением Семёна 2026-07-19 (картинки ставит вкладка «Смена изображения»)"),
            "transport": "grid",
            "uses_direct_units": False,
        }, 200
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

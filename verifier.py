"""Post-create verification for Direct automation.

This module is intentionally independent from Flask and API clients. It checks
the create_set outcome and returns a compact machine-readable report. Live
Direct/Grid checks and repair actions can build on top of this report later.
"""
from __future__ import annotations

import re
from typing import Any


_TP_RE = re.compile(r"^tp\d+_(?:cpc|cpa)_(?:site|kviz)(?:\b|_)")
_CODER_RE = re.compile(
    r"^tp\d+_(?:cpc|cpa)_(?:site|kviz)(?:_ct\d{4}_a(?:on|off)_n\d{3}_r\d{4}_ct\d{3}_ag\d{3}_g\d{2})?"
)


def _name(x: dict[str, Any]) -> str:
    return str((x or {}).get("name") or "").strip()


def _result_ok(r: dict[str, Any]) -> bool:
    return bool((r or {}).get("ok"))


def _created_result(r: dict[str, Any]) -> bool:
    return _result_ok(r) and not bool((r or {}).get("skipped"))


def _item_matches_result(item_name: str, result_name: str) -> bool:
    if not item_name or not result_name:
        return False
    if result_name == item_name or result_name.startswith(item_name + " - ") or result_name.startswith(item_name + " — "):
        return True
    # UAC tp6/tp7 при создании переименовывают фид-суффикс («…/имя-фида.xml» → «… — имя-фида»),
    # полное имя позиции не матчится → ложный ITEM_NOT_PROCESSED (живой кейс porg-asfbs7qe 06.07:
    # кастомный фид yandex-catalog-model-design-custom-name.xml, «ошибок 3» при created 21/21).
    # Та же нормализация, что в blueprint._position_live_in_names: без последнего « — сегмента»,
    # только tp6/tp7 с ≥2 сепараторами (иначе усечение сматчило бы любую tp).
    if item_name.startswith(("tp6_", "tp7_")) and item_name.count(" — ") >= 2:
        base = item_name.rsplit(" — ", 1)[0].strip()
        return bool(base) and (result_name == base or result_name.startswith(base + " — "))
    return False


def _content_counts(item: dict[str, Any]) -> tuple[int, int, int]:
    titles = item.get("titles") or []
    texts = item.get("texts") or []
    sitelinks = item.get("sitelinks") or []
    if not isinstance(titles, list):
        titles = []
    if not isinstance(texts, list):
        texts = []
    if not isinstance(sitelinks, list):
        sitelinks = []
    return len(titles), len(texts), len(sitelinks)


def _as_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _first_present(body: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = body.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _counter_present(body: dict[str, Any]) -> bool:
    if _as_int(_first_present(body, "counter_id", "counter")):
        return True
    counters = _first_present(body, "counter_ids", "counters")
    return bool(_as_list(counters))


def _goal_present(body: dict[str, Any]) -> bool:
    if _as_int(_first_present(body, "goal_id", "goal")):
        return True
    goals = _as_list(_first_present(body, "goals"))
    return bool(goals)


def _feed_present(item: dict[str, Any]) -> bool:
    return bool(
        _as_int(item.get("feed_id") or item.get("FeedId"))
        or item.get("feed_url")
        or item.get("feed")
    )


def _result_feed_present(result: dict[str, Any]) -> bool:
    """Return True when a created result proves a feed was resolved downstream.

    Some plan items (notably tp5/search_gallery) intentionally do not carry a
    concrete feed_id in the request body.  The builder fans out/resolves the
    actual URL feed later and records it in the result.  Static verification must
    not report ITEM_FEED_MISSING_LOCAL for such successfully-created campaigns.
    """
    if not isinstance(result, dict) or not _created_result(result):
        return False
    if _feed_present(result):
        return True
    name = _name(result).lower()
    if re.search(r"\s[—-]\s[^—-]*\.xml\b", name):
        return True
    build = result.get("build") or result.get("tp1_build") or result.get("tp5_build") or {}
    if not isinstance(build, dict):
        return False
    return any(int(build.get(key) or 0) > 0
               for key in ("shopping_ads", "listing_ads", "smart_ads", "feed_filters"))


def _item_type(item: dict[str, Any]) -> str:
    return str(item.get("type") or item.get("campaign_type") or "").strip()


def _is_uac_item(item: dict[str, Any]) -> bool:
    tp = _item_type(item)
    name = _name(item)
    return tp in {"tp6_master", "tp7_product"} or tp.startswith(("tp6", "tp7")) or name.startswith(("tp6_", "tp7_"))


def _is_feed_item(item: dict[str, Any]) -> bool:
    tp = _item_type(item)
    name = _name(item)
    return (
        "gallery" in tp
        or "product" in tp
        or "feed" in tp
        or name.startswith(("tp3_", "tp5_", "tp7_"))
    )


_POSEVY_TYPES = {"post_tp8", "post_tp9", "post_tp10"}
_STRUCT_TP_BY_TYPE = {
    "tp1_rsy": "tp1",
    "search_test": "tp2",
    "rsya_gallery": "tp3",
    "search_dynamic": "tp4",
    "search_gallery": "tp5",
    "master": "tp6",
    "product": "tp7",
}


def _is_posevy_item(item: dict[str, Any]) -> bool:
    tp = _item_type(item)
    name = _name(item)
    return (tp in _POSEVY_TYPES
            or tp.startswith(("tp8", "tp9", "tp10"))
            or name.startswith(("tp8_", "tp9_", "tp10_")))


def _verify_items_against_slepok_structure(items: list[dict[str, Any]],
                                           body: dict[str, Any] | None,
                                           issues: list[dict[str, Any]]) -> None:
    """Guard create-plan items against the same slepok structure shown in the UI.

    This intentionally checks only explicit structure keys (`camp_key`) in regular auto
    tp1-tp5 items. Full missing-parity is a live audit concern: partial/profile-gated
    create requests can legally build a subset, but a planned campaign must not point to
    a campaign key absent from the selected slepok/site/tp structure.
    """
    if not items:
        return
    body = body or {}
    agent = str(body.get("agent") or "").strip()
    site_type = str(body.get("site_type") or body.get("site") or "").strip()
    if not agent or not site_type:
        for it in items:
            agent = agent or str((it or {}).get("_plan_agent") or "").strip()
            site_type = site_type or str((it or {}).get("_plan_site_type")
                                         or (it or {}).get("_plan_struct_site_type") or "").strip()
            if agent and site_type:
                break
    if not agent or not site_type:
        return
    expected_cache: dict[str, set[str]] = {}
    try:
        from .create_set_structure import structure_to_campaigns
    except Exception as e:  # noqa: BLE001
        issues.append({"severity": "warn", "code": "STRUCTURE_PARITY_SKIPPED",
                       "message": f"structure_to_campaigns import failed: {str(e)[:160]}"})
        return

    for item in items:
        if not isinstance(item, dict):
            continue
        camp_key = str(item.get("camp_key") or "").strip()
        if not camp_key:
            continue
        tp_code = str(item.get("tp") or _STRUCT_TP_BY_TYPE.get(_item_type(item), "")).strip()
        if tp_code not in {"tp1", "tp2", "tp3", "tp4", "tp5"}:
            continue
        if tp_code not in expected_cache:
            try:
                expected_cache[tp_code] = {
                    str(c.get("name") or "").strip()
                    for c in (structure_to_campaigns(agent, site_type, tp_code) or [])
                    if str(c.get("name") or "").strip()
                }
            except Exception as e:  # noqa: BLE001
                issues.append({"severity": "warn", "code": "STRUCTURE_PARITY_SKIPPED",
                               "tp": tp_code,
                               "message": f"structure read failed: {str(e)[:160]}"})
                expected_cache[tp_code] = set()
        expected = expected_cache.get(tp_code) or set()
        if expected and camp_key not in expected:
            issues.append({"severity": "error", "code": "ITEM_NOT_IN_SLEPOK_STRUCTURE",
                           "name": _name(item), "tp": tp_code, "camp_key": camp_key,
                           "message": "planned campaign key is absent from UI slepok structure"})


def structure_preflight_issues(items: list[dict[str, Any]],
                               body: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Read-only precreate guard for the UI slepok structure contract.

    The post-create verifier uses the same helper, but create-set must reject stale
    or hand-edited payloads before any Direct objects are created.
    """
    issues: list[dict[str, Any]] = []
    _verify_items_against_slepok_structure(items or [], body or {}, issues)
    return issues


def _callouts_relevant(items: list[dict[str, Any]], results: list[dict[str, Any]]) -> bool:
    """Callouts are campaign-level EPK/Grid assets; UAC-only tp6/tp7 does not require them."""
    typed_items = [it for it in (items or []) if isinstance(it, dict) and _name(it)]
    if typed_items:
        return any(not _is_uac_item(it) for it in typed_items)
    named_results = [r for r in (results or []) if isinstance(r, dict) and _name(r)]
    if named_results:
        return any(not _name(r).startswith(("tp6_", "tp7_")) for r in named_results)
    return True


def _promo_relevant(items: list[dict[str, Any]], results: list[dict[str, Any]]) -> bool:
    """Промо-акции не привязываются к посевам (GdPostCampaign).

    Возвращает False если набор состоит ИСКЛЮЧИТЕЛЬНО из посевных типов.
    При смешанном наборе (посевы + обычные) — возвращает True (промо нужно для обычных).
    """
    typed_items = [it for it in (items or []) if isinstance(it, dict) and _name(it)]
    if typed_items:
        return any(not _is_posevy_item(it) for it in typed_items)
    named_results = [r for r in (results or []) if isinstance(r, dict) and _name(r)]
    if named_results:
        return any(not _name(r).startswith(("tp8_", "tp9_", "tp10_")) for r in named_results)
    return True  # неизвестный набор — считаем релевантным (fail-safe)


def _verify_body(body: dict[str, Any] | None, issues: list[dict[str, Any]]) -> dict[str, Any]:
    body = body or {}
    checked = {
        "body": bool(body),
        "slepok": False,
        "metrika": False,
        "goal": False,
        "items": False,
        "draft_only": True,
    }
    if not body:
        return checked

    agent = str(body.get("agent") or body.get("slepok") or body.get("slepok_key") or "").strip()
    checked["slepok"] = bool(agent)
    if not agent:
        issues.append({
            "severity": "error",
            "code": "BODY_SLEPOK_MISSING",
            "message": "не выбран слепок/agent для создания набора кампаний",
        })

    metrika_optional = bool(body.get("via_cookie")) and bool(body.get("no_cpa"))
    checked["metrika"] = _counter_present(body)
    checked["goal"] = _goal_present(body)
    if not checked["metrika"]:
        issues.append({
            "severity": "warn" if metrika_optional else "error",
            "code": "BODY_COUNTER_MISSING",
            "message": "не указан счетчик Метрики" + ("; разрешено только для via_cookie+no_cpa" if metrika_optional else ""),
        })
    if not checked["goal"]:
        issues.append({
            "severity": "warn" if metrika_optional else "error",
            "code": "BODY_GOAL_MISSING",
            "message": "не указана цель Метрики" + ("; разрешено только для via_cookie+no_cpa" if metrika_optional else ""),
        })

    body_items = _as_list(body.get("items"))
    checked["items"] = bool(body_items)
    if not body_items:
        issues.append({"severity": "error", "code": "BODY_ITEMS_EMPTY", "message": "в body нет items"})

    if bool(body.get("launch")):
        checked["draft_only"] = False
    return checked


def _promo_ok(note: str | None, created_count: int) -> tuple[bool, str]:
    if created_count <= 0:
        return True, "no created campaigns"
    s = (note or "").lower()
    if not s:
        return False, "promo note is empty"
    if "привяз" in s and "не привяз" not in s:
        return True, note or ""
    bad = ("не привяз", "не выполнено", "ошиб", "отклонил")
    if any(x in s for x in bad):
        return False, note or ""
    return ("привяз" in s), note or ""


def verify_create_set(*, login: str, items: list[dict[str, Any]], results: list[dict[str, Any]],
                      promo_note: str | None = None, callouts_note: str | None = None,
                      callouts: list[str] | None = None,
                      body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Verify create_set result shape and business invariants available locally."""
    items = items or []
    results = results or []
    created = [r for r in results if _created_result(r)]
    failed = [r for r in results if not _result_ok(r)]
    skipped = [r for r in results if r.get("skipped")]

    issues: list[dict[str, Any]] = []
    repair_candidates: list[dict[str, Any]] = []
    checked = _verify_body(body, issues)
    _verify_items_against_slepok_structure(items, body, issues)

    result_names = [_name(r) for r in results]
    result_by_item_name: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        rn = _name(r)
        if not rn:
            continue
        for item in items:
            item_name = _name(item)
            if item_name and _item_matches_result(item_name, rn):
                result_by_item_name.setdefault(item_name, []).append(r)
    for item in items:
        item_name = _name(item)
        tp = _item_type(item)
        if not item_name:
            issues.append({"severity": "error", "code": "ITEM_NAME_EMPTY", "message": "item has no name"})
            continue
        if not tp:
            issues.append({"severity": "warn", "code": "ITEM_TYPE_EMPTY", "name": item_name})
        if re.search(r"\b(?:None|null|undefined)\b", item_name, re.I):
            issues.append({"severity": "error", "code": "ITEM_NAME_HAS_NULL_TOKEN", "name": item_name})
        if not any(_item_matches_result(item_name, rn) for rn in result_names):
            issues.append({"severity": "error", "code": "ITEM_NOT_PROCESSED", "name": item_name})

        titles_n, texts_n, sitelinks_n = _content_counts(item)
        if _is_uac_item(item):
            if titles_n <= 0:
                issues.append({"severity": "warn", "code": "CONTENT_TITLES_MISSING_LOCAL", "name": item_name})
            if texts_n <= 0:
                issues.append({"severity": "warn", "code": "CONTENT_TEXTS_MISSING_LOCAL", "name": item_name})
            if titles_n and titles_n < 5:
                issues.append({"severity": "warn", "code": "CONTENT_TITLES_LOW", "name": item_name, "count": titles_n})
            if texts_n and texts_n < 3:
                issues.append({"severity": "warn", "code": "CONTENT_TEXTS_LOW", "name": item_name, "count": texts_n})
        elif titles_n <= 0 and texts_n <= 0 and not _is_feed_item(item) and not _is_posevy_item(item):
            issues.append({"severity": "warn", "code": "ITEM_CONTENT_MISSING_LOCAL", "name": item_name})
        result_feed_ok = any(_result_feed_present(r) for r in result_by_item_name.get(item_name, []))
        if _is_feed_item(item) and not _feed_present(item) and not result_feed_ok:
            issues.append({"severity": "warn", "code": "ITEM_FEED_MISSING_LOCAL", "name": item_name})
        if sitelinks_n and sitelinks_n < 8:
            issues.append({"severity": "warn", "code": "CONTENT_SITELINKS_LOW", "name": item_name, "count": sitelinks_n})

    for r in results:
        rn = _name(r)
        if not rn:
            issues.append({"severity": "error", "code": "RESULT_NAME_EMPTY", "result": r})
            continue
        if not _TP_RE.search(rn):
            issues.append({"severity": "error", "code": "CODER_PREFIX_MISSING", "name": rn})
        elif not _CODER_RE.search(rn):
            issues.append({"severity": "warn", "code": "CODER_SHAPE_SUSPICIOUS", "name": rn})
        if re.search(r"\b(?:None|null|undefined)\b", rn, re.I):
            issues.append({"severity": "error", "code": "NAME_HAS_NULL_TOKEN", "name": rn})
        if _created_result(r) and not r.get("id") and not r.get("campaign_id"):
            issues.append({"severity": "error", "code": "CREATED_WITHOUT_ID", "name": rn})
        if not _result_ok(r):
            issues.append({"severity": "error", "code": "RESULT_FAILED", "name": rn,
                           "message": str(r.get("error") or "")[:240]})
            repair_candidates.append({"kind": "failed_campaign", "name": rn, "error": str(r.get("error") or "")[:240]})
        build = r.get("build") or r.get("tp1_build") or r.get("tp5_build") or {}
        cid = _as_int(r.get("id") or r.get("campaign_id"))
        if isinstance(build, dict):
            if build.get("error") or build.get("skipped"):
                issues.append({"severity": "error", "code": "BUILD_ERROR", "name": rn, "id": cid,
                               "message": str(build.get("error") or build.get("skipped"))[:240]})
            if "groups" in build and int(build.get("groups") or 0) <= 0:
                issues.append({"severity": "error", "code": "NO_ADGROUPS_REPORTED", "name": rn, "id": cid})
            if "ads" in build and int(build.get("ads") or 0) <= 0:
                # tp5 (token-path): TextAd не создаётся намеренно (Семён 2026-07-07) — не дефект
                # если ShoppingAd есть. Гейт «без ShoppingAd» остаётся (проверяется выше в create).
                _tp5_no_text_ok = (bool(r.get("tp5_build"))
                                   and int(build.get("shopping_ads") or 0) > 0)
                if not _tp5_no_text_ok:
                    issues.append({"severity": "error", "code": "NO_ADS_REPORTED", "name": rn, "id": cid})
        if r.get("search_finalized") is False:
            issues.append({"severity": "warn", "code": "SEARCH_NOT_FINALIZED", "name": rn, "id": cid})
        fin = r.get("shopping_finalized")
        if isinstance(fin, dict) and fin.get("error"):
            issues.append({"severity": "warn", "code": "SHOPPING_NOT_FINALIZED", "name": rn, "id": cid,
                           "message": str(fin.get("error"))[:240]})
        if r.get("grid_warn"):
            issues.append({"severity": "warn", "code": "GRID_FINALIZE_WARN", "name": rn, "id": cid,
                           "message": str(r.get("grid_warn"))[:240]})

    # ── CPA_COUNT_PLAN_MISMATCH: CPA-позиции в плане, но ни одной в результатах ─────────
    # Кейс: create_set_plan.py читает только `n` при сборке плана; create_set_input.py
    # читает `no_cpa OR n`. Если body содержит only no_cpa (без n), план включает CPA-кампании,
    # но orchestrator их пропускает → позиции пропадают тихо (ITEM_NOT_PROCESSED на каждую,
    # но без агрегированного диагноза). CPA-позиция = `_cpa_` в имени.
    _CPA_NAME_RE = re.compile(r'\btp\d+_cpa_')
    _planned_cpa = [_name(it) for it in items if _CPA_NAME_RE.search(_name(it))]
    _result_cpa = [_name(r) for r in results if _CPA_NAME_RE.search(_name(r))]
    if _planned_cpa and not _result_cpa:
        issues.append({
            "severity": "error",
            "code": "CPA_COUNT_PLAN_MISMATCH",
            "planned": len(_planned_cpa),
            "processed": 0,
            "note": (f"{len(_planned_cpa)} CPA-позиц. в плане, но ни одной в результатах "
                     f"(ни created, ни skipped, ни failed). "
                     f"Вероятная причина: no_cpa=True в теле запроса без флага n."),
        })

    promo_ok, promo_message = _promo_ok(promo_note, len(created))
    if not promo_ok and _promo_relevant(items, results):
        issues.append({"severity": "warn", "code": "PROMO_NOT_ATTACHED", "message": promo_message})
        repair_candidates.append({"kind": "promo_attach_or_create", "login": login})

    if callouts and not callouts_note and _callouts_relevant(items, results):
        issues.append({"severity": "warn", "code": "CALLOUTS_NOT_CONFIRMED", "count": len(callouts)})
        repair_candidates.append({"kind": "callouts_verify", "login": login})

    error_count = sum(1 for x in issues if x.get("severity") == "error")
    warn_count = sum(1 for x in issues if x.get("severity") == "warn")
    status = "fail" if error_count else ("warn" if warn_count else "pass")
    report = {
        "status": status,
        "login": login,
        "checked": checked,
        "summary": {
            "items": len(items),
            "results": len(results),
            "created": len(created),
            "failed": len(failed),
            "skipped": len(skipped),
            "issues": len(issues),
            "errors": error_count,
            "warnings": warn_count,
        },
        "issues": issues[:80],
        "repair_candidates": repair_candidates[:80],
    }
    try:
        from .repair.repair_planner import build_repair_plan
        report["repair_plan"] = build_repair_plan(report)
    except Exception:  # noqa: BLE001
        report["repair_plan"] = {"status": "error"}
    return report

"""Campaign-level aspect batch functions for create_set finalization.

Each ``apply_<aspect>_batch`` function applies ONE campaign-level aspect to ALL
created campaigns via a single Grid mutation (+ per-item fallback on failure).

Contract:
  - idempotent: applying the same values twice has no side-effects
  - best-effort: returns {ok, applied, failed_campaigns, ...} — never raises
  - one Grid call per aspect for all campaign_ids; per-item fallback on batch error

Orchestrator ``apply_campaign_aspects`` calls aspects STRICTLY SEQUENTIALLY for
the same login (shared cookie/CSRF — parallel calls would invalidate CSRF tokens).

Aspect order (dependency-safe):
  minus_sets → disabled_places → callouts → sitelinks → promo → bidmods → rename

Phase-1 (dual-write): inlined finalization (_finalize_rsya / _finalize_search_via_grid)
still runs per-campaign. Batch idempotently rewrites the same values → parity check.
Phase-2 (future): inline removed, batch becomes the only finalization path.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from . import campaign as cmc
from . import grid_finalize as gf
from .repair_executor import (
    RepairDeps,
    execute_callouts_repair,
    execute_promo_repair,
    execute_rename_repair,
)

_log = logging.getLogger("direct.batches")

# RSYa detection: tp-number extracted by regex (no ^ anchor → robust to prefixes/versions)
_TP_SEARCH_RE = re.compile(r"tp(?P<tp>\d+)_(?:cpc|cpa)_(?:site|kviz)", re.I)


# ── helpers ──────────────────────────────────────────────────────────────────

def _unique_ids(campaign_ids: list[Any]) -> list[int]:
    """Deduplicated positive ints from a mixed-type list."""
    out: list[int] = []
    for raw in campaign_ids or []:
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            continue
        if cid > 0 and cid not in out:
            out.append(cid)
    return out


def _build_grid(login: str, agency: str = "") -> tuple[Any, gf.GridClient]:
    """Get a pooled GridClient via get_grid_client (reuses Session/CSRF). Raises on auth failure."""
    client = cmc.build_client(login, account=(agency or None))
    cookie = client.sess.headers.get("Cookie") or ""
    return client, gf.get_grid_client(login, cookie=cookie)


def _is_rsya(name: str) -> bool:
    """True if campaign name contains a tp1 network token (RSYa/network).
    Uses regex search (no ^ anchor) — robust to prefixes/versions in name."""
    m = _TP_SEARCH_RE.search(name or "")
    return m is not None and m.group("tp") == "1"


def select_campaign_ids_by_tp(created: list[dict], tp_numbers) -> list[int]:
    """id созданных кампаний, чьё ИМЯ несёт кодер ``tpN_(cpc|cpa)_(site|kviz)`` из *tp_numbers*.

    *created* — вывод ``campaign_result.created_campaigns``: [{id, name, …}, …].
    *tp_numbers* — iterable номеров tp (int или str), напр. ``(2, 3, 4, 5)``.

    Нужен для аспектов, которые ставятся НЕ на все tp: библиотечные наборы минус-фраз идут
    на tp2-tp5 и намеренно НЕ идут на tp1 (минус-фразы режут охват РСЯ — TP1_CAMPAIGN_MINUS_WRONG).
    Имя без распознаваемого кодера в выборку не попадает.
    """
    wanted = {str(t).strip() for t in (tp_numbers or []) if str(t).strip()}
    out: list[int] = []
    for row in created or []:
        if not isinstance(row, dict):
            continue
        m = _TP_SEARCH_RE.search(str(row.get("name") or ""))
        if not m or m.group("tp") not in wanted:
            continue
        try:
            cid = int(row.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if cid > 0 and cid not in out:
            out.append(cid)
    return out


def _batch_with_item_fallback(
    grid: "gf.GridClient",
    login: str,
    agency_eff: str,
    batch_fn,       # Callable[[GridClient], list]
    item_fns: list, # list of (campaign_id: int, Callable[[GridClient], list])
) -> tuple[list, list[dict]]:
    """Run batch_fn on shared grid; on exception fall back per-item with fresh pooled clients.

    Returns (rows, failed_campaigns). ``rows`` is whatever batch_fn/item_fns return
    (list of row dicts or ints depending on Grid method). Never raises.
    """
    try:
        return batch_fn(grid), []
    except Exception:  # noqa: BLE001
        rows: list = []
        failed: list[dict] = []
        for cid, fn in item_fns:
            try:
                _, g2 = _build_grid(login, agency_eff)
                rows.extend(fn(g2) or [])
            except Exception as e:  # noqa: BLE001
                failed.append({"campaign_id": cid, "error": str(e)[:200]})
        return rows, failed


# ── 1. Callouts ──────────────────────────────────────────────────────────────

def apply_callouts_batch(
    login: str,
    campaign_ids: list[int],
    deps: RepairDeps,
    *,
    callouts: list[str],
    callout_cap: int = 4,
) -> dict:
    """Attach callouts to ALL created campaigns via one Grid UpdateCampaigns.

    Reuses ``execute_callouts_repair`` which already implements batch + per-item
    fallback. Returns {ok, applied, failed_campaigns}.
    """
    ids = _unique_ids(campaign_ids)
    if not ids:
        return {"ok": True, "applied": 0, "failed_campaigns": [], "skipped": "no_campaign_ids"}
    if not callouts:
        return {"ok": True, "applied": 0, "failed_campaigns": [], "skipped": "no_callouts"}

    # Build minimal ctx expected by execute_callouts_repair
    ctx: dict = {"body": {"callouts": list(callouts)}}
    # Temporarily override callout_cap on deps via a thin wrapper
    import dataclasses
    capped_deps = dataclasses.replace(deps, callout_cap=callout_cap)

    result, _ = execute_callouts_repair(login, ctx, ids, capped_deps)
    return {
        "ok": result.get("ok", False),
        "applied": result.get("attached", 0),
        "failed_campaigns": result.get("failed_campaigns") or [],
        "callout_ids": result.get("callout_ids") or [],
    }


# ── 2. Promo ─────────────────────────────────────────────────────────────────

def apply_promo_batch(
    login: str,
    campaign_ids: list[int],
    deps: RepairDeps,
    *,
    ctx: dict,
    promo_id: int | None = None,
) -> dict:
    """Attach promo to ALL created campaigns.

    If ``promo_id`` is given (e.g. from precreate phase): just attaches without
    creating a new promo (idempotent for dual-write).
    If not given: delegates to ``execute_promo_repair`` (creates + attaches).
    Returns {ok, applied, failed_campaigns, promo_id}.
    """
    ids = _unique_ids(campaign_ids)
    if not ids:
        return {"ok": True, "applied": 0, "failed_campaigns": [], "skipped": "no_campaign_ids"}

    if promo_id:
        # Fast path: promo already exists — just attach
        body_obj = (ctx.get("body") or {})
        acc = deps.account_ctx(login)
        agency = (ctx.get("agency") or body_obj.get("agency") or (acc or {}).get("agency") or "").strip()
        try:
            client = cmc.build_client(login, account=(agency or None))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "applied": 0, "failed_campaigns": [],
                    "error": f"build_client: {str(e)[:160]}"}
        from .promo import PromoClient
        attach = PromoClient(client, login).attach(promo_id, ids)
        errors = (((attach.get("data") or {}).get("updateCampaignsPromoExtension") or {})
                  .get("validationResult") or {}).get("errors") or attach.get("errors")
        if errors:
            return {"ok": False, "applied": 0, "failed_campaigns": [],
                    "error": "Grid attach promo failed", "details": errors,
                    "promo_id": promo_id}
        return {"ok": True, "applied": len(ids), "failed_campaigns": [], "promo_id": promo_id}

    # No promo_id: create promo from slepok and attach
    body_obj = (ctx.get("body") or {})
    if not body_obj.get("agent"):
        return {"ok": True, "applied": 0, "failed_campaigns": [], "skipped": "no_slepok_agent"}
    result, status = execute_promo_repair(login, ctx, ids, deps)
    return {
        "ok": result.get("ok", False),
        "applied": result.get("attached", 0),
        "failed_campaigns": [],
        "promo_id": result.get("promo_id"),
        "status": status,
    }


# ── 3. Rename ─────────────────────────────────────────────────────────────────

def apply_rename_batch(
    login: str,
    campaign_names: dict[int, str],
    deps: RepairDeps,
    *,
    agency: str = "",
) -> dict:
    """Rename campaigns in batch via Grid (idempotent — same name → no-op).

    Reuses ``execute_rename_repair`` (batch + per-item fallback). Returns
    {ok, applied, failed_campaigns}.
    """
    if not campaign_names:
        return {"ok": True, "applied": 0, "failed_campaigns": [], "skipped": "no_campaign_names"}
    ctx: dict = {"agency": agency}
    result, _ = execute_rename_repair(login, ctx, campaign_names, deps)
    return {
        "ok": result.get("ok", False),
        "applied": result.get("updated", 0),
        "failed_campaigns": result.get("failed_campaigns") or [],
    }


# ── 4. Sitelinks ─────────────────────────────────────────────────────────────

def apply_sitelinks_batch(
    login: str,
    campaign_ids: list[int],
    deps: RepairDeps,
    *,
    sitelinks: list[dict] | None = None,
    sitelink_set_id: int | None = None,
    agency: str = "",
) -> dict:
    """Attach sitelink set to ALL created campaigns.

    If ``sitelink_set_id`` is given: attaches directly.
    If ``sitelinks`` list given but no id: creates the set then attaches.
    Batch + per-item fallback (Grid is sometimes flaky on large batches).
    Returns {ok, applied, failed_campaigns, sitelink_set_id}.
    """
    ids = _unique_ids(campaign_ids)
    if not ids:
        return {"ok": True, "applied": 0, "failed_campaigns": [], "skipped": "no_campaign_ids"}
    if not sitelink_set_id and not sitelinks:
        return {"ok": True, "applied": 0, "failed_campaigns": [], "skipped": "no_sitelinks"}

    acc = deps.account_ctx(login)
    agency_eff = agency or ((acc or {}).get("agency") or "")
    try:
        _, grid = _build_grid(login, agency_eff)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "applied": 0, "failed_campaigns": [],
                "error": f"GridClient: {str(e)[:160]}"}

    sid = sitelink_set_id
    if not sid and sitelinks:
        try:
            sid = grid.add_sitelink_set(sitelinks)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "applied": 0, "failed_campaigns": [],
                    "error": f"add_sitelink_set: {str(e)[:200]}"}
        if not sid:
            return {"ok": False, "applied": 0, "failed_campaigns": [],
                    "error": "add_sitelink_set returned None"}

    # Batch attach (+ per-item fallback via shared helper)
    updated, failed = _batch_with_item_fallback(
        grid, login, agency_eff,
        lambda g: g.set_campaign_sitelink_set(ids, sid),
        [(cid, lambda g, c=cid: g.set_campaign_sitelink_set([c], sid)) for cid in ids],
    )
    if len(failed) == len(ids):
        return {"ok": False, "applied": 0, "failed_campaigns": failed[:40],
                "error": "batch+fallback sitelinks: all campaigns failed"}

    updated_ids = []
    for row in updated or []:
        try:
            cid = int((row or {}).get("id") or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid > 0 and cid not in updated_ids:
            updated_ids.append(cid)

    n_ok = len(updated_ids) or max(0, len(ids) - len(failed))
    return {
        "ok": True,
        "applied": n_ok,
        "failed_campaigns": failed[:40],
        "sitelink_set_id": sid,
    }


# ── 5. Disabled places (RSYa only) ───────────────────────────────────────────

def apply_disabled_places_batch(
    login: str,
    campaign_ids: list[int],
    deps: RepairDeps,
    *,
    hosts: list[str],
    agency: str = "",
) -> dict:
    """Set RSYa minus-sites (disabledPlaces) on tp1 campaigns in batch.

    Wraps ``GridClient.set_campaign_disabled_places`` with batch + per-item
    fallback. Only RSYa campaigns should be in ``campaign_ids`` (caller filters).
    Returns {ok, applied, failed_campaigns}.
    """
    ids = _unique_ids(campaign_ids)
    if not ids:
        return {"ok": True, "applied": 0, "failed_campaigns": [], "skipped": "no_campaign_ids"}
    clean_hosts = [str(h).strip() for h in (hosts or []) if str(h).strip()]
    if not clean_hosts:
        return {"ok": True, "applied": 0, "failed_campaigns": [], "skipped": "no_hosts"}

    acc = deps.account_ctx(login)
    agency_eff = agency or ((acc or {}).get("agency") or "")
    try:
        _, grid = _build_grid(login, agency_eff)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "applied": 0, "failed_campaigns": [],
                "error": f"GridClient: {str(e)[:160]}"}

    updated, failed = _batch_with_item_fallback(
        grid, login, agency_eff,
        lambda g: g.set_campaign_disabled_places(ids, clean_hosts),
        [(cid, lambda g, c=cid: g.set_campaign_disabled_places([c], clean_hosts)) for cid in ids],
    )
    if len(failed) == len(ids):
        return {"ok": False, "applied": 0, "failed_campaigns": failed[:40],
                "error": "batch+fallback disabled_places: all campaigns failed"}

    updated_ids = []
    for row in updated or []:
        try:
            cid = int((row or {}).get("id") or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid > 0 and cid not in updated_ids:
            updated_ids.append(cid)

    n_ok = len(updated_ids) or max(0, len(ids) - len(failed))
    return {
        "ok": True,
        "applied": n_ok,
        "failed_campaigns": failed[:40],
    }


# ── 6. Bidmods (age demographic adjustments) ─────────────────────────────────

def apply_bidmods_batch(
    login: str,
    campaign_ids: list[int],
    deps: RepairDeps,
    *,
    ages_percent: dict[str, int],
    agency: str = "",
) -> dict:
    """Set age bid modifiers on ALL created campaigns in batch.

    ``ages_percent`` keys are Grid enum strings (``_0_17``, ``_18_24``, …),
    values are delta percent (e.g. −100 to exclude). Conversion delta→multiplier
    (100+pct, clamp 0..1300) is done inside ``set_campaign_age_bidmods``.
    Returns {ok, applied, failed_campaigns}.
    """
    ids = _unique_ids(campaign_ids)
    if not ids:
        return {"ok": True, "applied": 0, "failed_campaigns": [], "skipped": "no_campaign_ids"}
    wanted = {str(k): int(v) for k, v in (ages_percent or {}).items() if str(k).strip() and int(v) != 0}
    if not wanted:
        return {"ok": True, "applied": 0, "failed_campaigns": [], "skipped": "no_age_adjustments"}

    acc = deps.account_ctx(login)
    agency_eff = agency or ((acc or {}).get("agency") or "")
    try:
        _, grid = _build_grid(login, agency_eff)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "applied": 0, "failed_campaigns": [],
                "error": f"GridClient: {str(e)[:160]}"}

    satisfied, failed = _batch_with_item_fallback(
        grid, login, agency_eff,
        lambda g: g.set_campaign_age_bidmods(ids, wanted),
        [(cid, lambda g, c=cid: g.set_campaign_age_bidmods([c], wanted)) for cid in ids],
    )
    if len(failed) == len(ids):
        return {"ok": False, "applied": 0, "failed_campaigns": failed[:40],
                "error": "batch+fallback bidmods: all campaigns failed"}

    return {
        "ok": True,
        "applied": len(satisfied),
        "failed_campaigns": failed[:40],
        "satisfied_ids": satisfied,
    }


# ── 7. Minus keyword sets (library) ──────────────────────────────────────────

def apply_minus_sets_batch(
    login: str,
    campaign_ids: list[int],
    deps: RepairDeps,
    *,
    minus_set_ids: list[int],
    agency: str = "",
) -> dict:
    """Attach library minus keyword sets to ALL created campaigns.

    Minimal narrow RMW: reads current ``libraryMinusKeywordsIds`` via
    CampaignsEditData, merges (no duplicates), sends UpdateCampaigns.
    Batch + per-item fallback. Returns {ok, applied, failed_campaigns}.

    TODO: вынести inline GraphQL + _narrow_bases в GridClient.set_campaign_minus_sets
    (grid_finalize.py) — тогда этот метод сведётся к обёртке как у остальных аспектов.
    Ждём синхронизации с агентом, правящим grid_finalize.
    """
    ids = _unique_ids(campaign_ids)
    if not ids:
        return {"ok": True, "applied": 0, "failed_campaigns": [], "skipped": "no_campaign_ids"}
    clean_mids = [int(m) for m in (minus_set_ids or [])
                  if isinstance(m, (int, str)) and int(m) > 0]
    if not clean_mids:
        return {"ok": True, "applied": 0, "failed_campaigns": [], "skipped": "no_minus_set_ids"}

    acc = deps.account_ctx(login)
    agency_eff = agency or ((acc or {}).get("agency") or "")
    try:
        _, grid = _build_grid(login, agency_eff)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "applied": 0, "failed_campaigns": [],
                "error": f"GridClient: {str(e)[:160]}"}

    # Narrow RMW — same pattern as GridClient.set_campaign_disabled_places
    payloads = grid._read_unified_campaign_update_payloads(ids)
    q = ("mutation UpdateCampaigns($input:GdUpdateCampaignsInput!$login:String!){"
         "updateCampaigns(input:$input){updatedCampaigns{id}"
         "validationResult{errors{code params path}warnings{code params path}}}"
         "getClientMutationId(input:{login:$login}){mutationId}}")
    bases, skipped = grid._narrow_bases(payloads, ids, "batch set-minus-sets")
    for cid, why in skipped.items():
        _log.debug("batch/minus_sets: campaign %s skipped — strategy «%s»", cid, why)

    str_mids = [str(m) for m in clean_mids]
    items = []
    for cid, base in bases.items():
        existing = list(base.get("libraryMinusKeywordsIds") or [])
        seen = set(str(x) for x in existing)
        merged = existing + [s for s in str_mids if s not in seen]
        base["libraryMinusKeywordsIds"] = merged
        items.append({"unifiedCampaign": base})
    if not items:
        return {"ok": True, "applied": 0, "failed_campaigns": [],
                "skipped": "all_skipped_by_strategy"}

    def _send_items(grid_inst: gf.GridClient, batch_items: list[dict]) -> list[dict]:
        r = grid_inst._post("UpdateCampaigns", q,
                            {"login": login, "input": {"campaignUpdateItems": batch_items}})
        data = r.json()
        res = (data.get("data") or {}).get("updateCampaigns") or {}
        vr = res.get("validationResult") or {}
        if data.get("errors") or vr.get("errors"):
            raise gf.GridFinalizeError(
                "batch minus-sets: " + json.dumps(
                    data.get("errors") or vr.get("errors"), ensure_ascii=False)[:400])
        return res.get("updatedCampaigns") or []

    _item_pairs: list = []
    for _it in items:
        _cid_str = (_it.get("unifiedCampaign") or {}).get("id") or ""
        try:
            _cid_int = int(_cid_str)
        except (TypeError, ValueError):
            _cid_int = 0
        _item_pairs.append((_cid_int, lambda g, it=_it: _send_items(g, [it])))

    updated, failed = _batch_with_item_fallback(
        grid, login, agency_eff,
        lambda g: _send_items(g, items),
        _item_pairs,
    )
    if not updated:
        return {"ok": False, "applied": 0, "failed_campaigns": failed[:40],
                "error": "batch+fallback minus_sets: all items failed"}

    updated_ids: list[int] = []
    for row in updated or []:
        try:
            cid = int((row or {}).get("id") or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid > 0 and cid not in updated_ids:
            updated_ids.append(cid)

    n_ok = len(updated_ids) or max(0, len(items) - len(failed))
    return {
        "ok": True,
        "applied": n_ok,
        "failed_campaigns": failed[:40],
        "minus_set_ids": clean_mids,
    }


# ── Orchestrator ──────────────────────────────────────────────────────────────

def apply_campaign_aspects(
    login: str,
    created_campaigns: list[dict],
    spec: dict,
    deps: RepairDeps,
) -> dict:
    """Apply ALL campaign-level aspects as sequential batch Grid mutations.

    ``created_campaigns`` — output of ``campaign_result.created_campaigns(results)``:
    [{id, name, kind, result}, …].

    ``spec`` keys:
      callouts       list[str]      callout texts
      callout_cap    int            max callouts per campaign (default 4)
      promo_ctx      dict           ctx for promo repair: {body: {agent, items, …}, agency}
      promo_id       int|None       precreated promo id; if set: only attach, no re-create
      sitelinks      list[dict]|None  [{title, href, description?}, …]
      sitelink_set_id int|None      if set: attach directly, skip add_sitelink_set
      disabled_places list[str]     RSYa minus-hosts (domain only, lowercase)
      ages_percent   dict[str,int]  {Grid-age-enum: delta-percent} e.g. {"_0_17": -100}
      minus_set_ids  list[int]      library minus keyword set IDs
      agency         str            agency login for cookie/client build

    Aspects run STRICTLY SEQUENTIALLY (shared cookie/CSRF for one login).
    Order: minus_sets → disabled_places → callouts → sitelinks → promo → bidmods → rename

    Sбой отдельного аспекта: логируется, не ронит весь набор (best-effort).
    Returns {ok, aspects, campaign_count, rsya_count}.
    """
    if not created_campaigns:
        return {"ok": True, "aspects": {}, "skipped": "no_created_campaigns"}

    all_ids = [c["id"] for c in created_campaigns if c.get("id")]
    rsya_ids = [c["id"] for c in created_campaigns
                if c.get("id") and _is_rsya(c.get("name") or "")]
    campaign_names: dict[int, str] = {
        c["id"]: c["name"]
        for c in created_campaigns
        if c.get("id") and c.get("name")
    }

    agency = spec.get("agency") or ""
    aspects: dict[str, dict] = {}

    def _run(name: str, fn, *args, **kwargs) -> None:
        try:
            result = fn(*args, **kwargs)
            aspects[name] = result
            if result.get("skipped"):
                _log.info("batch/%s login=%s: skipped (%s)", name, login, result["skipped"])
            elif result.get("ok"):
                _log.info("batch/%s login=%s: ok applied=%s", name, login, result.get("applied", 0))
            else:
                _log.warning("batch/%s login=%s: NOT ok — %s", name, login,
                             result.get("error") or str(result)[:200])
        except Exception as exc:  # noqa: BLE001
            _log.error("batch/%s login=%s: exception %s", name, login, str(exc)[:300])
            aspects[name] = {"ok": False, "error": str(exc)[:200], "applied": 0,
                             "failed_campaigns": []}

    # 1. minus_sets
    minus_set_ids = [int(m) for m in (spec.get("minus_set_ids") or [])
                     if str(m).strip().lstrip("-").isdigit() and int(m) > 0]
    _run("minus_sets", apply_minus_sets_batch,
         login, all_ids, deps, minus_set_ids=minus_set_ids, agency=agency)

    # 2. disabled_places — RSYa campaigns only
    disabled_places = list(spec.get("disabled_places") or [])
    _run("disabled_places", apply_disabled_places_batch,
         login, rsya_ids, deps, hosts=disabled_places, agency=agency)

    # 3. callouts
    callouts = list(spec.get("callouts") or [])
    _run("callouts", apply_callouts_batch,
         login, all_ids, deps,
         callouts=callouts, callout_cap=int(spec.get("callout_cap") or 4))

    # 4. sitelinks
    _run("sitelinks", apply_sitelinks_batch,
         login, all_ids, deps,
         sitelinks=spec.get("sitelinks"),
         sitelink_set_id=spec.get("sitelink_set_id"),
         agency=agency)

    # 5. promo
    promo_ctx = spec.get("promo_ctx") or {}
    promo_id_spec: int | None = spec.get("promo_id")
    body_obj = promo_ctx.get("body") or {}
    if promo_id_spec or body_obj.get("agent"):
        _run("promo", apply_promo_batch,
             login, all_ids, deps,
             ctx=promo_ctx, promo_id=promo_id_spec)
    else:
        aspects["promo"] = {"ok": True, "applied": 0, "skipped": "no_promo_ctx"}

    # 6. bidmods (age demographic)
    ages_percent: dict[str, int] = dict(spec.get("ages_percent") or {})
    _run("bidmods", apply_bidmods_batch,
         login, all_ids, deps, ages_percent=ages_percent, agency=agency)

    # 7. rename — name is applied LAST (after all other aspects)
    _run("rename", apply_rename_batch,
         login, campaign_names, deps, agency=agency)

    any_failed = any(
        not v.get("ok") and not v.get("skipped")
        for v in aspects.values()
    )
    return {
        "ok": not any_failed,
        "aspects": aspects,
        "campaign_count": len(all_ids),
        "rsya_count": len(rsya_ids),
    }

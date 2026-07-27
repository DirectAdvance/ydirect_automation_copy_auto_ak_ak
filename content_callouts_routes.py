"""Content-editor callout helpers and routes.

Extracted from ``routes_content_editor.py`` (structural split). Functions:
    _normalize_callout_text / _normalize_callout_texts
    _grid_attach_responsive_ads_callouts
    _assign_callout_accountwide
    _replace_callout_grid

Routes registered via ``register_callouts_routes``:
    POST /api/content-editor/callouts/assign_async
"""
from __future__ import annotations

import json
import re
from typing import Callable

from flask import jsonify, request

from .content_editor_helpers import (
    _frag_trim,
    _grid_client,
    _match_targets,
    _grid_campaign_callout_ids,
    _clear_ad_level_asset_overrides,
)


def _normalize_callout_text(text: str) -> str:
    """Keep callout text within Direct's conservative symbol set."""
    clean = re.sub(r"[^0-9A-Za-zА-Яа-яЁё%+\- ₽]", " ", str(text or ""))
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:25]


def _normalize_callout_texts(values, *, limit: int = 10) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    if isinstance(values, str):
        raw_values = values.splitlines()
    elif isinstance(values, (list, tuple)):
        raw_values = list(values)
    else:
        raw_values = [values]
    for raw in raw_values:
        text = _normalize_callout_text(str(raw or ""))
        if not text or text in seen:
            continue
        out.append(text)
        seen.add(text)
        if len(out) >= max(1, int(limit or 10)):
            break
    return out


def _grid_attach_responsive_ads_callouts(
    login: str,
    ad_items: list[dict],
    campaign_ids: list[int],
    callout_ids: list[int],
    *,
    grid_client_factory: Callable | None = None,
) -> tuple[list[int], list[str]]:
    """Fallback для callout assign: привязка на ad-level ResponsiveAd через Grid.

    Campaign-level ``UpdateCampaigns`` падает на стратегиях ``DEFAULT`` /
    ``MULTIPLE_CPA`` из-за невалидного write-enum в самой Grid-схеме. Для
    ResponsiveAd это можно обойти: ``UpdateAdaptiveTextAds`` принимает
    ``inheritableCallouts`` прямо на объявлении и не зависит от enum стратегии
    кампании. Живой probe подтверждён 2026-07-22 на аккаунте ``porg-as46rje6``.
    """
    errors: list[str] = []
    wanted_ids: list[str] = []
    for raw in callout_ids or []:
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            continue
        if cid > 0:
            sid = str(cid)
            if sid not in wanted_ids:
                wanted_ids.append(sid)
    if not wanted_ids:
        return [], ["не найдены id уточнений для ResponsiveAd fallback"]
    grid = (grid_client_factory or _grid_client)(login)
    ad_ids: list[int] = []
    for raw in ad_items or []:
        try:
            aid = int((raw or {}).get("ad_id") or 0)
        except (TypeError, ValueError):
            continue
        if aid > 0 and aid not in ad_ids:
            ad_ids.append(aid)
    cids: list[int] = []
    for raw in campaign_ids or []:
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            continue
        if cid > 0 and cid not in cids:
            cids.append(cid)
    if not ad_ids or not cids:
        return [], ["не найдены ResponsiveAd для привязки уточнений"]

    before = grid.adaptive_ads_for_update(cids, ad_ids)
    if not before:
        return [], ["Grid не вернул snapshot ResponsiveAd для привязки уточнений"]

    payload: list[dict] = []
    by_campaign: dict[int, list[int]] = {}
    missing: list[int] = []
    for aid in ad_ids:
        item = before.get(aid)
        if not isinstance(item, dict):
            missing.append(aid)
            continue
        nxt = dict(item)
        nxt["inheritableCallouts"] = {"policy": "OVERRIDE", "calloutIds": list(wanted_ids)}
        payload.append(nxt)
        try:
            campaign_id = int(item.get("campaignId") or 0)
        except (TypeError, ValueError):
            campaign_id = 0
        if campaign_id > 0:
            by_campaign.setdefault(campaign_id, []).append(aid)
    if missing:
        errors.append(f"Grid не прочитал {len(missing)} ResponsiveAd для привязки уточнений")
    if not payload:
        return [], errors

    updated = int(grid.update_ad_images(payload, allow_empty_images=True) or 0)
    errors.extend(list(getattr(grid, "last_ad_update_errors", []) or []))

    after = grid.adaptive_ads_for_update(cids, ad_ids)
    confirmed_campaigns: list[int] = []
    for cid, aids in by_campaign.items():
        ok = True
        for aid in aids:
            state = after.get(aid) if isinstance(after, dict) else None
            got = ((state or {}).get("inheritableCallouts") or {}).get("calloutIds") or []
            got_norm = [str(x).strip() for x in got if str(x).strip()]
            if not all(co_id in got_norm for co_id in wanted_ids):
                ok = False
                break
        if ok and cid not in confirmed_campaigns:
            confirmed_campaigns.append(cid)
    if updated and not confirmed_campaigns and not errors:
        errors.append("Grid не подтвердил ad-level привязку уточнений у ResponsiveAd")
    return confirmed_campaigns, errors


def _assign_callout_accountwide(
    login: str,
    texts,
    content: dict,
    *,
    grid_client_factory: Callable | None = None,
) -> dict:
    """Создать до 10 уточнений и назначить их всем поддерживаемым кампаниям аккаунта."""
    normalized_texts = _normalize_callout_texts(texts, limit=10)
    if not normalized_texts:
        return {"replaced": 0, "errors": ["после удаления недопустимых символов список уточнений пустой"]}
    grid = (grid_client_factory or _grid_client)(login)
    campaign_types = content.get("_campaign_types") or {}
    all_campaign_ids: list[int] = []
    skipped_map: dict[int, str] = {}
    for raw in content.get("_campaign_ids") or []:
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            continue
        ctype = str(campaign_types.get(cid) or campaign_types.get(str(cid)) or "")
        if ctype in {"UNIFIED_CAMPAIGN", "UAC"}:
            skipped_map[cid] = "Мастер кампаний / Товарный мастер не поддерживает уточнения"
            continue
        if cid > 0 and cid not in all_campaign_ids:
            all_campaign_ids.append(cid)
    if not all_campaign_ids:
        return {
            "replaced": 0,
            "errors": ["в аккаунте не найдены кампании, куда можно привязать уточнения"],
            "skipped_campaign_ids": sorted(int(cid) for cid in skipped_map.keys()),
            "skipped_reasons": {str(k): str(v) for k, v in skipped_map.items()},
        }
    try:
        created = grid.add_callouts(normalized_texts)
    except Exception as e:  # noqa: BLE001
        return {"replaced": 0, "errors": [f"Grid add_callouts: {str(e)[:180]}"]}
    new_ids: list[int] = []
    missing_texts: list[str] = []
    for text in normalized_texts:
        raw_id = created.get(text)
        try:
            cid = int(raw_id or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid > 0:
            new_ids.append(cid)
        else:
            missing_texts.append(text)
    if not new_ids:
        return {"replaced": 0, "errors": ["Grid не вернул id новых уточнений"]}

    already_bound: list[int] = []
    for cid in all_campaign_ids:
        current_ids = [int(x) for x in (content.get("_campaign_callout_ids") or {}).get(cid, [])
                       if str(x).isdigit()]
        if current_ids == new_ids:
            already_bound.append(cid)

    errors: list[str] = []
    warnings: list[str] = []
    if missing_texts:
        warnings.append(f"Grid не вернул id для {len(missing_texts)} уточнени(й): {missing_texts[:5]}")
    ads_touched, ad_clear_errors = _clear_ad_level_asset_overrides(
        login, content, all_campaign_ids,
        clear_callouts=True,
        grid_client_factory=grid_client_factory,
    )
    errors.extend(ad_clear_errors)
    updated_campaigns: list[int] = []
    for cid in all_campaign_ids:
        try:
            updated = grid.set_campaign_callouts([cid], new_ids)
        except Exception as e:  # noqa: BLE001
            errors.append(f"Grid set-callouts {cid}: {str(e)[:180]}")
            continue
        for row in updated or []:
            try:
                cid = int((row or {}).get("id") or 0)
            except (TypeError, ValueError):
                cid = 0
            if cid > 0 and cid not in updated_campaigns:
                updated_campaigns.append(cid)

    missing_campaigns = [
        cid for cid in all_campaign_ids
        if cid not in updated_campaigns and cid not in skipped_map
    ]
    if missing_campaigns:
        errors.append(
            "Grid set-callouts не подтвердил campaign-level уточнения у кампаний: "
            + ", ".join(str(cid) for cid in missing_campaigns)
        )

    skipped_campaigns = sorted(int(cid) for cid in skipped_map.keys())
    changed = bool(updated_campaigns)
    if all_campaign_ids and len(updated_campaigns) < len(all_campaign_ids):
        warnings.append(
            f"Grid не подтвердил привязку уточнения у {len(all_campaign_ids) - len(updated_campaigns)} кампаний")
    return {
        "replaced": 1 if changed else 0,
        "errors": errors,
        "warnings": warnings,
        "new_callout_ids": new_ids,
        "new_texts": normalized_texts,
        "new_callout_id": (new_ids[0] if len(new_ids) == 1 else None),
        "new_text": (normalized_texts[0] if len(normalized_texts) == 1 else None),
        "campaigns_touched": len(updated_campaigns),
        "ads_touched": ads_touched,
        "updated_campaign_ids": updated_campaigns,
        "fallback_campaign_ids": [],
        "already_bound_campaign_ids": already_bound,
        "skipped_campaign_ids": skipped_campaigns,
        "skipped_reasons": {str(k): str(v) for k, v in skipped_map.items()},
    }


def _replace_callout_grid(
    token: str,
    login: str,
    old_text: str,
    new_text: str,
    content: dict,
    v5_call: Callable,
    *,
    grid_client_factory: Callable | None = None,
) -> dict:
    """Create a new callout and swap it into campaigns that used the old one."""
    old = _frag_trim(old_text)
    targets = _match_targets(content, "callout", old)
    if not targets:
        return {"replaced": 0, "errors": ["уточнение с таким текстом не найдено"]}
    old_ids = []
    for target in targets:
        try:
            eid = int(target.get("id"))
        except (TypeError, ValueError):
            continue
        if eid > 0 and eid not in old_ids:
            old_ids.append(eid)
    campaign_callouts = content.get("_campaign_callout_ids") or {}
    affected: dict[int, list[int]] = {}
    for raw_cid, raw_ids in campaign_callouts.items():
        try:
            cid = int(raw_cid)
        except (TypeError, ValueError):
            continue
        ids = []
        for raw in raw_ids or []:
            try:
                co = int(raw)
            except (TypeError, ValueError):
                continue
            if co > 0 and co not in ids:
                ids.append(co)
        if any(old_id in ids for old_id in old_ids):
            affected[cid] = ids
    if not affected:
        return {"replaced": 0, "errors": ["не найдены кампании, где привязано это уточнение"]}
    normalized_new = _normalize_callout_text(new_text)
    if not normalized_new:
        return {"replaced": 0, "errors": ["после удаления недопустимых символов текст уточнения пустой"]}
    grid = (grid_client_factory or _grid_client)(login)
    created = grid.add_callouts([normalized_new])
    new_id = created.get(normalized_new)
    if not new_id:
        return {"replaced": 0, "errors": [
            "не удалось создать новое уточнение через cookie/Grid; OAuth API fallback запрещён"
        ]}
    replaced = 0
    errors: list[str] = []
    for cid, current_ids in affected.items():
        next_ids: list[int] = []
        for co in current_ids:
            if co in old_ids:
                continue
            if co not in next_ids:
                next_ids.append(co)
        if int(new_id) not in next_ids:
            next_ids.append(int(new_id))
        try:
            updated = grid.set_campaign_callouts([cid], next_ids)
            if updated:
                replaced += 1
            else:
                errors.append(f"кампания {cid}: Grid не подтвердил обновление")
        except Exception as e:  # noqa: BLE001
            errors.append(f"кампания {cid}: {str(e)[:160]}")
    # Grid иногда отдаёт неполный inheritableCallouts.assetValue (часть кампаний без данных),
    # из-за чего первый проход видит не все привязки. Перечитываем карту и добиваем хвост.
    known_cids = [int(c) for c in campaign_callouts.keys() if str(c).isdigit()]
    for _pass in range(2):
        try:
            fresh = _grid_campaign_callout_ids(login, known_cids, grid_client_factory=grid_client_factory)
        except Exception:  # noqa: BLE001 - добивание best-effort, первый проход уже отработал
            break
        leftovers = {
            cid: ids for cid, ids in fresh.items()
            if any(old_id in (ids or []) for old_id in old_ids)
        }
        if not leftovers:
            break
        for cid, current_ids in leftovers.items():
            next_ids = [co for co in current_ids if co not in old_ids]
            if int(new_id) not in next_ids:
                next_ids.append(int(new_id))
            try:
                if grid.set_campaign_callouts([cid], next_ids):
                    replaced += 1
                else:
                    errors.append(f"кампания {cid}: Grid не подтвердил обновление (добивание)")
            except Exception as e:  # noqa: BLE001
                errors.append(f"кампания {cid}: {str(e)[:160]}")
    return {"replaced": replaced, "errors": errors, "new_callout_id": int(new_id), "new_text": normalized_new}


def register_callouts_routes(
    bp,
    access,
    *,
    _login_allowed,
    _content_tools_allowed,
    _token,
    _enqueue_content_job,
) -> None:
    """Register /api/content-editor/callouts/* endpoints."""

    @bp.route("/api/content-editor/callouts/assign_async", methods=["POST"])
    @access
    def ce_callouts_assign_async():
        body = request.json or {}
        login = (body.get("login") or "").strip()
        normalized = _normalize_callout_texts(body.get("texts"), limit=10)
        try:
            campaign_count = int(body.get("campaign_count") or 0)
        except (TypeError, ValueError):
            campaign_count = 0
        if not login:
            return jsonify({"error": "login обязателен"}), 400
        if not normalized:
            return jsonify({"error": "после удаления недопустимых символов список уточнений пустой"}), 400
        ok, scope_err = _login_allowed(login)
        if not ok:
            return jsonify({"error": scope_err}), 403
        _, agency, err = _token(login)
        if err:
            return jsonify({"error": err}), 404
        payload = json.dumps({"texts": normalized}, ensure_ascii=False)
        return _enqueue_content_job(
            login, agency, "callout_assign", payload, payload, "assign",
            campaign_count, resp_type="callout_assign")

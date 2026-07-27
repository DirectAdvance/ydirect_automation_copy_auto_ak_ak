"""Content-editor sitelink helpers and routes.

Extracted from ``routes_content_editor.py`` (structural split). Functions:
    _sitelink_item_tuple / _normalize_sitelink_items
    _REBIND_SUBTYPE_FIELDS / _SITELINK_READ_FIELDS constants
    _confirm_ads_sitelink_text
    _v5_rebind_ads_sitelink_set
    _grid_rebind_responsive_ads_sitelink_set
    _assign_uac_sitelinks
    _grid_add_sitelink_set_for_content_editor
    _assign_sitelink_items_accountwide
    _replace_sitelink_text_grid
    _replace_uac_sitelinks
    _validate_permutation
    _reorder_sitelinks

Routes registered via ``register_sitelinks_routes``:
    POST /api/content-editor/sitelinks/reorder_async
    POST /api/content-editor/sitelinks/assign_async
"""
from __future__ import annotations

import json
import time
from typing import Callable

from flask import jsonify, request

from .content_editor_helpers import (
    _frag_trim,
    _grid_client,
    _uac_client,
    _uac_patch_campaign_texts,
    _unwrap_uac_response,
    _uac_cids_from_targets,
    _v5_paginate,
    _SITELINK_FIELD,
    _content_regular_campaign_ids,
    _clear_ad_level_asset_overrides,
    _grid_clear_text_ads_overrides,
    _grid_clear_responsive_ads_overrides,
)


def _sitelink_item_tuple(x) -> tuple[str, str, str]:
    if not isinstance(x, dict):
        return ("", "", "")
    return (
        str(x.get("title") or "").strip(),
        str(x.get("href") or "").strip(),
        str(x.get("description") or "").strip(),
    )


def _normalize_sitelink_items(items) -> list[dict]:
    out: list[dict] = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        item = {
            "title": str(raw.get("title") or "").strip(),
            "href": str(raw.get("href") or "").strip(),
            "description": str(raw.get("description") or "").strip(),
        }
        if item["title"] or item["href"] or item["description"]:
            out.append(item)
    return out[:8]


_REBIND_SUBTYPE_FIELDS = {"TextAd": "TextAdFieldNames", "DynamicTextAd": "DynamicTextAdFieldNames"}
_SITELINK_READ_FIELDS = {"TextAd": "TextAdFieldNames", "DynamicTextAd": "DynamicTextAdFieldNames",
                         "ResponsiveAd": "ResponsiveAdFieldNames"}


def _confirm_ads_sitelink_text(v5_call: Callable, token: str, login: str,
                               ad_items: list[dict], field: str,
                               old: str, new: str) -> tuple[int, list[str]]:
    """Read-back после Grid findAndReplaceText: у скольких объявлений набор ссылок
    реально содержит новый текст (и не содержит старый). Grid может вернуть
    successCount, ничего не изменив (проверено live на GdTextAd) — поэтому
    доверяем только перечитке."""
    by_subtype: dict[str, list[int]] = {}
    for it in ad_items or []:
        st = str((it or {}).get("subtype") or "ResponsiveAd")
        if st in _SITELINK_READ_FIELDS:
            by_subtype.setdefault(st, []).append(int(it["ad_id"]))
    errors: list[str] = []
    set_by_ad: dict[int, int] = {}
    for st, ids in by_subtype.items():
        got, err = _v5_paginate(
            v5_call, "ads", token, login,
            {"SelectionCriteria": {"Ids": ids[:10000]}, "FieldNames": ["Id"],
             _SITELINK_READ_FIELDS[st]: ["SitelinkSetId"]},
            "Ads")
        if err:
            errors.append(f"read-back ads.get: {err}")
            continue
        for a in got:
            sid = (a.get(st) or {}).get("SitelinkSetId")
            if sid:
                set_by_ad[int(a.get("Id") or 0)] = int(sid)
    set_ok: dict[int, bool] = {}
    uniq_sets = sorted(set(set_by_ad.values()))
    for chunk in [uniq_sets[i:i + 100] for i in range(0, len(uniq_sets), 100)]:
        import json as _json
        j = v5_call("sitelinks", "get", token, login,
                    {"SelectionCriteria": {"Ids": chunk}, "FieldNames": ["Id", "Sitelinks"]})
        if j.get("error"):
            errors.append("read-back sitelinks.get: " + _json.dumps(j["error"], ensure_ascii=False)[:120])
            continue
        for s in (j.get("result") or {}).get("SitelinksSets") or []:
            key = {"description": "Description", "href": "Href"}.get(field, "Title")
            vals = [(x.get(key) or "").strip() for x in s.get("Sitelinks") or []]
            set_ok[int(s.get("Id") or 0)] = (new in vals) and (old not in vals)
    confirmed = sum(1 for aid, sid in set_by_ad.items() if set_ok.get(sid))
    unconfirmed = len(set_by_ad) - confirmed
    if unconfirmed > 0:
        errors.append(f"замена текста ссылки не подтвердилась у {unconfirmed} объявлений — "
                      "Grid не применил изменение")
    return confirmed, errors


def _v5_rebind_ads_sitelink_set(v5_call: Callable, token: str, login: str,
                                ad_items: list[dict], new_set_id: int) -> tuple[int, list[str]]:
    """v5 ads.update: перепривязать SitelinkSetId у объявлений + read-back.

    ad_items: [{ad_id, subtype}] — подтип обязателен: TextAd/DynamicTextAd идут своим
    ключом в ads.update; прочие (ResponsiveAd) v5 не обновляет — честная ошибка.
    """
    import json as _json
    errors: list[str] = []
    by_subtype: dict[str, list[int]] = {}
    for it in ad_items or []:
        try:
            aid = int((it or {}).get("ad_id") or 0)
        except (TypeError, ValueError):
            continue
        if aid > 0:
            st = str((it or {}).get("subtype") or "TextAd")
            if aid not in by_subtype.setdefault(st, []):
                by_subtype[st].append(aid)
    for st in [s for s in by_subtype if s not in _REBIND_SUBTYPE_FIELDS]:
        errors.append(f"{len(by_subtype[st])} объявлений типа {st}: "
                      "перепривязка набора быстрых ссылок через v5 не поддерживается")
        by_subtype.pop(st)

    updated_total = 0
    for subtype, ad_ids in by_subtype.items():
        ok_ids: list[int] = []
        for chunk in [ad_ids[i:i + 500] for i in range(0, len(ad_ids), 500)]:
            payload = {"Ads": [{"Id": aid, subtype: {"SitelinkSetId": new_set_id}} for aid in chunk]}
            j = {}
            for attempt in range(3):  # error 1000 «Сервис временно недоступен» — транзиент, ретраим
                j = v5_call("ads", "update", token, login, payload)
                code = (j.get("error") or {}).get("error_code")
                if code not in (1000, 1001, 1002, 52, 500):
                    break
                time.sleep(5 * (attempt + 1))
            if j.get("error"):
                errors.append("ads.update: " + _json.dumps(j["error"], ensure_ascii=False)[:160])
                continue
            results = (j.get("result") or {}).get("UpdateResults") or []
            if len(results) != len(chunk):
                errors.append(f"ads.update: получено {len(results)} результатов на {len(chunk)} объявлений")
            for res in results:
                if res.get("Errors"):
                    msg = "; ".join((e.get("Message") or "") for e in res["Errors"])
                    errors.append(f"объявление {res.get('Id')}: {msg[:120]}")
                elif res.get("Id"):
                    ok_ids.append(int(res["Id"]))
                else:
                    errors.append("ads.update: результат без Id и Errors: "
                                  + _json.dumps(res, ensure_ascii=False)[:100])
        if not ok_ids:
            continue
        confirmed: set[int] = set()
        rb_failed = False
        for rb_chunk in [ok_ids[i:i + 10000] for i in range(0, len(ok_ids), 10000)]:
            got, err = _v5_paginate(
                v5_call, "ads", token, login,
                {"SelectionCriteria": {"Ids": rb_chunk}, "FieldNames": ["Id"],
                 _REBIND_SUBTYPE_FIELDS[subtype]: ["SitelinkSetId"]},
                "Ads")
            if err:
                errors.append(f"read-back ads.get: {err}")
                rb_failed = True
                break
            confirmed |= {
                int(a.get("Id") or 0) for a in got
                if int(((a.get(subtype) or {}).get("SitelinkSetId") or 0)) == int(new_set_id)
            }
        if rb_failed:
            continue
        bad = [aid for aid in ok_ids if aid not in confirmed]
        if bad:
            errors.append(f"read-back не подтвердил новый набор у {len(bad)} объявлений")
        updated_total += len(confirmed & set(ok_ids))
    return updated_total, errors


def _grid_rebind_responsive_ads_sitelink_set(
    login: str,
    ad_items: list[dict],
    campaign_ids: list[int],
    new_set_id: int,
    *,
    grid_client_factory: Callable | None = None,
) -> tuple[int, list[str]]:
    """Перепривязать ad-level набор быстрых ссылок у ``ResponsiveAd`` через Grid RMW.

    ``findAndReplaceText`` на таких объявлениях подтверждённо хрупок: Grid может
    принять мутацию, но не применить изменение sitelink title/description/href.
    Безопасный путь — перечитать полный editable payload объявления, заменить
    только ``inheritableSitelinkSet`` и отправить ``UpdateAdaptiveTextAds``.
    """
    errors: list[str] = []
    if int(new_set_id or 0) <= 0:
        return 0, ["Grid не вернул id нового набора быстрых ссылок"]
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
        return 0, ["не найдены ResponsiveAd для перепривязки быстрых ссылок"]

    before = grid.adaptive_ads_for_update(cids, ad_ids)
    if not before:
        return 0, ["Grid не вернул snapshot ResponsiveAd для перепривязки быстрых ссылок"]

    payload: list[dict] = []
    missing: list[int] = []
    for aid in ad_ids:
        item = before.get(aid)
        if not isinstance(item, dict):
            missing.append(aid)
            continue
        nxt = dict(item)
        nxt["inheritableSitelinkSet"] = {"policy": "OVERRIDE", "sitelinkSetId": str(int(new_set_id))}
        payload.append(nxt)
    if missing:
        errors.append(f"Grid не прочитал {len(missing)} ResponsiveAd для перепривязки быстрых ссылок")
    if not payload:
        return 0, errors

    updated = int(grid.update_ad_images(payload, allow_empty_images=True) or 0)
    errors.extend(list(getattr(grid, "last_ad_update_errors", []) or []))

    after = grid.adaptive_ads_for_update(cids, ad_ids)
    confirmed = 0
    for aid in ad_ids:
        state = after.get(aid) if isinstance(after, dict) else None
        sid = ((state or {}).get("inheritableSitelinkSet") or {}).get("sitelinkSetId")
        if str(sid or "") == str(int(new_set_id)):
            confirmed += 1
    if updated and confirmed < updated:
        errors.append(f"read-back Grid не подтвердил новый набор у {updated - confirmed} ResponsiveAd")
    elif not updated and not errors:
        errors.append("Grid не обновил ни одного ResponsiveAd при перепривязке быстрых ссылок")
    return confirmed, errors


def _assign_uac_sitelinks(
    login: str,
    items: list[dict],
    campaign_ids: list[int],
    *,
    uac_client_factory: Callable | None = None,
) -> tuple[int, list[str], list[int]]:
    """Назначить ОДИН и тот же массив быстрых ссылок всем UAC-кампаниям."""
    errors: list[str] = []
    touched: list[int] = []
    client = _uac_client(login, uac_client_factory)
    want = [_sitelink_item_tuple(x) for x in items]
    for raw in campaign_ids or []:
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            continue
        if cid <= 0 or cid in touched:
            continue
        try:
            _uac_patch_campaign_texts(client, cid, "sitelinks", items)
            after = _unwrap_uac_response(
                client._request("GET", f"/campaign/{cid}", step=f"uac-sitelinks-rb:{cid}"))
            got = [_sitelink_item_tuple(x) for x in (after.get("sitelinks") or []) if isinstance(x, dict)]
            if got == want:
                touched.append(cid)
            else:
                errors.append(f"UAC {cid}: read-back не подтвердил эталонный набор быстрых ссылок")
        except Exception as e:  # noqa: BLE001
            errors.append(f"UAC {cid}: {str(e)[:180]}")
    return len(touched), errors, touched


def _grid_add_sitelink_set_for_content_editor(grid, items: list[dict]) -> int | None:
    """Create a sitelink set for content-editor preserving explicit URL fragments.

    Fake grids in tests and older helpers may still expose the legacy signature
    ``add_sitelink_set(items)`` without ``preserve_fragment``. Keep backward
    compatibility by retrying without kwargs.
    """
    try:
        return grid.add_sitelink_set(items, preserve_fragment=True)
    except TypeError:
        return grid.add_sitelink_set(items)


def _assign_sitelink_items_accountwide(
    token: str,
    login: str,
    items: list[dict],
    content: dict,
    v5_call: Callable,
    *,
    grid_client_factory: Callable | None = None,
    uac_client_factory: Callable | None = None,
) -> dict:
    """Назначить эталонный набор быстрых ссылок всему аккаунту.

    В отличие от reorder/replace, здесь целевой набор задаётся ПОЛНЫМ массивом
    ``items`` ({title, href, description}), поэтому операция может одновременно
    менять и порядок, и сами тексты/URL быстрых ссылок.
    """
    items = _normalize_sitelink_items(items)
    if not items:
        return {"replaced": 0, "errors": ["эталонный набор быстрых ссылок пуст"]}
    grid = (grid_client_factory or _grid_client)(login)
    errors: list[str] = []
    warnings: list[str] = []
    try:
        new_set_id = _grid_add_sitelink_set_for_content_editor(grid, items)
    except Exception as e:  # noqa: BLE001
        return {"replaced": 0, "errors": [f"Grid add_sitelink_set: {str(e)[:180]}"]}
    if not new_set_id:
        return {"replaced": 0, "errors": ["Grid не вернул id эталонного набора быстрых ссылок"]}

    all_campaign_ids = _content_regular_campaign_ids(content)
    updated_campaigns: list[int] = []
    skipped_campaign_ids: list[int] = []
    if all_campaign_ids:
        ads_touched, ad_clear_errors = _clear_ad_level_asset_overrides(
            login, content, all_campaign_ids,
            clear_sitelinks=True,
            grid_client_factory=grid_client_factory,
        )
        errors.extend(ad_clear_errors)
        try:
            updated = grid.set_campaign_sitelink_set(all_campaign_ids, int(new_set_id))
            for row in updated or []:
                try:
                    cid = int((row or {}).get("id") or 0)
                except (TypeError, ValueError):
                    cid = 0
                if cid > 0 and cid not in updated_campaigns:
                    updated_campaigns.append(cid)
            skipped_campaign_ids = [cid for cid in all_campaign_ids if cid not in updated_campaigns]
        except Exception as e:  # noqa: BLE001
            errors.append(f"Grid campaign rebind: {str(e)[:180]}")
    else:
        ads_touched = 0

    if skipped_campaign_ids:
        errors.append(
            "Grid campaign rebind не подтвердил campaign-level БС у кампаний: "
            + ", ".join(str(cid) for cid in skipped_campaign_ids)
        )

    uac_ok = 0
    uac_touched: list[int] = []
    uac_ids = [int(x) for x in (content.get("_uac_campaign_ids") or []) if str(x).isdigit()]
    if uac_ids:
        uac_ok, uac_errs, uac_touched = _assign_uac_sitelinks(
            login, items, uac_ids, uac_client_factory=uac_client_factory)
        errors.extend(uac_errs)

    changed = bool(updated_campaigns or ads_touched or uac_ok)
    return {
        "replaced": 1 if changed else 0,
        "errors": errors,
        "warnings": warnings,
        "new_sitelink_set_id": int(new_set_id),
        "campaigns_touched": len(updated_campaigns),
        "ads_touched": ads_touched,
        "uac_campaigns_touched": uac_ok,
        "updated_campaign_ids": updated_campaigns,
        "fallback_campaign_ids": [],
        "updated_uac_campaign_ids": uac_touched,
        "skipped_campaign_ids": skipped_campaign_ids,
    }


def _replace_uac_sitelinks(
    login: str,
    field: str,
    old_text: str,
    new_text: str,
    targets: list[dict],
    *,
    uac_client_factory: Callable | None = None,
) -> dict:
    """Заменить поле быстрой ссылки (title/description/href) в UAC-кампаниях (tp6/tp7)
    через cookie-PATCH ``/web-api/uac/campaign/{id}`` по полю ``sitelinks``.

    ``field`` — одно из ``title``/``description``/``href``. Матч по точному значению
    поля элемента; в UAC быстрые ссылки нередко имеют ОДИН общий href — тогда смена
    href меняет посадочную у всех совпавших элементов. Read-back перечитывает деталь
    кампании и подтверждает, что новое значение есть, а старого — нет."""
    old = _frag_trim(old_text)
    new = _frag_trim(new_text)
    if not old:
        return {"replaced": 0, "errors": ["старое значение быстрой ссылки пустое"]}
    if not new:
        return {"replaced": 0, "errors": ["новое значение быстрой ссылки пустое"]}
    if field not in ("title", "description", "href"):
        return {"replaced": 0, "errors": [f"неподдерживаемое поле быстрой ссылки: {field}"]}
    campaign_ids = _uac_cids_from_targets(targets)
    if not campaign_ids:
        return {"replaced": 0, "errors": ["не найдены UAC-кампании для замены быстрой ссылки"]}

    client = _uac_client(login, uac_client_factory)

    replaced = 0
    errors: list[str] = []
    updated_campaigns: list[int] = []
    for cid in campaign_ids:
        try:
            detail = _unwrap_uac_response(
                client._request("GET", f"/campaign/{cid}", step=f"uac-detail:{cid}"))
            current = detail.get("sitelinks")
            if not isinstance(current, list):
                errors.append(f"кампания {cid}: у кампании нет быстрых ссылок")
                continue
            changed = 0
            next_items: list = []
            for item in current:
                if isinstance(item, dict) and (item.get(field) or "").strip() == old:
                    nxt = dict(item)
                    nxt[field] = new
                    next_items.append(nxt)
                    changed += 1
                else:
                    next_items.append(item)
            if not changed:
                errors.append(f"кампания {cid}: значение быстрой ссылки не найдено")
                continue
            _uac_patch_campaign_texts(client, cid, "sitelinks", next_items)
            # Read-back: перечитываем деталь и проверяем, что новое значение есть, старого — нет.
            after = _unwrap_uac_response(
                client._request("GET", f"/campaign/{cid}", step=f"uac-sl-readback:{cid}"))
            after_sl = after.get("sitelinks") if isinstance(after.get("sitelinks"), list) else []
            after_vals = [(x.get(field) or "").strip() for x in after_sl if isinstance(x, dict)]
            if new in after_vals and old not in after_vals:
                replaced += changed
                updated_campaigns.append(cid)
            else:
                errors.append(f"кампания {cid}: read-back не подтвердил новое значение быстрой ссылки")
        except Exception as e:  # noqa: BLE001
            errors.append(f"кампания {cid}: {str(e)[:180]}")
    return {"replaced": replaced, "errors": errors, "updated_uac_campaigns": updated_campaigns}


def _replace_sitelink_text_grid(
    login: str,
    typ: str,
    old_text: str,
    new_text: str,
    targets: list[dict],
    *,
    token: str | None = None,
    v5_call: Callable | None = None,
    grid_client_factory: Callable | None = None,
) -> dict:
    """Replace sitelink title/description by creating new sets and repointing campaigns.

    In current EPK accounts sitelinks are campaign-level inheritable assets:
    campaign has ``inheritableSitelinkSet.assetValue`` and ads inherit it.
    Grid ``findAndReplaceText`` is unstable on hundreds of inherited ads, so
    the safe path is set-level replacement.
    """
    old = _frag_trim(old_text)
    new = _frag_trim(new_text)
    field = _SITELINK_FIELD.get(typ, "title")
    if not targets:
        return {"replaced": 0, "errors": ["набор с таким текстом быстрой ссылки не найден"]}
    if not new:
        return {"replaced": 0, "errors": ["новое значение быстрой ссылки пустое"]}
    # У title/description — лимиты Директа; у href лимит на длину не применяем.
    if field != "href":
        limit = 60 if typ == "sitelink_description" else 30
        if len(new) > limit:
            return {"replaced": 0, "errors": [f"текст быстрой ссылки длиннее {limit} символов"]}
    grid = (grid_client_factory or _grid_client)(login)
    all_campaign_ids: list[int] = []
    for target in targets or []:
        for usage in target.get("usages") or []:
            try:
                cid = int(usage.get("campaign_id"))
            except (TypeError, ValueError):
                continue
            if cid > 0 and cid not in all_campaign_ids:
                all_campaign_ids.append(cid)
    current_set_by_campaign: dict[int, int] = {}
    unsupported_by_cid: dict[int, str] = {}
    _have_current_map = False           # карта текущих наборов реально прочитана (метод Grid есть)?
    if all_campaign_ids and hasattr(grid, "_read_unified_campaign_update_payloads"):
        try:
            payloads = grid._read_unified_campaign_update_payloads(all_campaign_ids)
            _have_current_map = True
            for cid, payload in payloads.items():
                if payload.get("_unsupported_strategy"):
                    unsupported_by_cid[int(cid)] = str(payload["_unsupported_strategy"])
                raw_sid = (payload.get("inheritableSitelinkSet") or {}).get("sitelinkSetId")
                try:
                    sid = int(raw_sid or 0)
                except (TypeError, ValueError):
                    sid = 0
                if cid > 0 and sid > 0:
                    current_set_by_campaign[int(cid)] = sid
        except Exception as e:  # noqa: BLE001
            errors = [f"не удалось прочитать текущие быстрые ссылки кампаний через Grid: {str(e)[:180]}"]
            return {"replaced": 0, "errors": errors}
    replaced = 0
    errors: list[str] = []
    created_sets: list[int] = []
    touched_campaigns: set[int] = set()
    touched_ads: set[int] = set()
    for target in targets or []:
        try:
            source_set_id = int(target.get("set_id") or 0)
        except (TypeError, ValueError):
            source_set_id = 0
        items = []
        changed = False
        for item in target.get("items") or []:
            next_item = {
                "title": item.get("title") or "",
                "href": item.get("href") or "",
                "description": item.get("description") or "",
            }
            if (next_item.get(field) or "").strip() == old:
                next_item[field] = new
                changed = True
            items.append(next_item)
        if not changed:
            continue
        campaign_ids: list[int] = []
        for raw_cid in target.get("campaign_ids") or []:
            try:
                cid = int(raw_cid)
            except (TypeError, ValueError):
                continue
            if cid > 0 and cid not in campaign_ids:
                campaign_ids.append(cid)
        for usage in target.get("usages") or []:
            try:
                cid = int(usage.get("campaign_id"))
            except (TypeError, ValueError):
                continue
            if cid > 0 and cid not in campaign_ids:
                campaign_ids.append(cid)
        # Перепривязываем ТОЛЬКО кампании, у которых campaign-level набор совпадает
        # с исходным. Пустая карта = у аккаунта наборы на уровне объявлений —
        # привязывать новый набор всем кампаниям подряд нельзя.
        # Охранник «набор кампании == исходному» применяем ТОЛЬКО когда карта реально прочитана
        # (реальный GridClient её отдаёт → прод-защита цела). Метод недоступен → доверяем usages
        # из свежего /load-снимка (иначе campaign-level перепривязка молча давала replaced=0).
        is_campaign_level = (target.get("level") or "") == "campaign"
        campaign_ids = [cid for cid in campaign_ids if cid not in touched_campaigns]
        if is_campaign_level:
            campaign_ids = [
                cid for cid in campaign_ids
                if not _have_current_map or current_set_by_campaign.get(cid) == source_set_id
            ]
        # Кампании с неподдерживаемой стратегией отфильтровываем ДО мутации,
        # чтобы одна такая не завалила весь батч (набор уже был бы создан).
        for cid in [c for c in campaign_ids if c in unsupported_by_cid]:
            errors.append(f"кампания {cid}: стратегия «{unsupported_by_cid[cid]}» "
                          "не поддерживается — быстрая ссылка не заменена")
        campaign_ids = [c for c in campaign_ids if c not in unsupported_by_cid]
        # Объявления с ad-level привязкой исходного набора (обычные ЕПК/текстовые аккаунты).
        ad_items: list[dict] = []
        seen_ads: set[int] = set()
        for raw in target.get("ad_items") or [{"ad_id": x} for x in (target.get("ad_ids") or [])]:
            try:
                aid = int((raw or {}).get("ad_id") or 0)
            except (TypeError, ValueError):
                continue
            if aid > 0 and aid not in touched_ads and aid not in seen_ads:
                seen_ads.add(aid)
                ad_items.append({"ad_id": aid, "subtype": (raw or {}).get("subtype") or "TextAd"})
        # Для бывших ad-level наборов основной путь теперь такой же, как в reorder:
        # чистим override у объявлений и назначаем новый набор на campaign-level.
        # Пообъявленческий v5-rebind давал частичные «объявление None: Неверный статус
        # у объекта» и оставлял активную логику зависимой от сотен ad-status.
        v5_items = [it for it in ad_items if is_campaign_level and it.get("subtype") in ("TextAd", "DynamicTextAd")]
        responsive_items = [it for it in ad_items if is_campaign_level and it.get("subtype") == "ResponsiveAd"]
        unsupported_items = [it for it in ad_items
                             if is_campaign_level and it.get("subtype") not in ("TextAd", "DynamicTextAd", "ResponsiveAd")]
        if not campaign_ids and not v5_items and not responsive_items and not unsupported_items:
            continue
        try:
            new_set_id = None
            if campaign_ids or v5_items or responsive_items:
                # новый набор нужен для campaign/v5/ResponsiveAd rebind-веток
                new_set_id = _grid_add_sitelink_set_for_content_editor(grid, items)
                if not new_set_id:
                    errors.append(f"набор {target.get('set_id')}: Grid не вернул id нового набора быстрых ссылок")
                else:
                    created_sets.append(int(new_set_id))
            if campaign_ids and new_set_id:
                if not is_campaign_level:
                    text_items = [it for it in ad_items if it.get("subtype") in ("TextAd", "DynamicTextAd")]
                    resp_items = [it for it in ad_items if it.get("subtype") == "ResponsiveAd"]
                    cleared_text, clear_text_errs = _grid_clear_text_ads_overrides(
                        login,
                        text_items,
                        campaign_ids,
                        clear_sitelinks=True,
                        grid_client_factory=grid_client_factory,
                    )
                    cleared_resp, clear_resp_errs = _grid_clear_responsive_ads_overrides(
                        login,
                        resp_items,
                        campaign_ids,
                        clear_sitelinks=True,
                        grid_client_factory=grid_client_factory,
                    )
                    errors.extend(clear_text_errs)
                    errors.extend(clear_resp_errs)
                updated = grid.set_campaign_sitelink_set(campaign_ids, int(new_set_id))
                if updated:
                    updated_ids = []
                    for row in updated:
                        try:
                            cid = int((row or {}).get("id") or 0)
                        except (TypeError, ValueError):
                            cid = 0
                        if cid > 0:
                            updated_ids.append(cid)
                    replaced += len(updated_ids)
                    touched_campaigns.update(updated_ids)
                    if not is_campaign_level:
                        touched_ads.update(it["ad_id"] for it in ad_items)
                else:
                    errors.append(f"набор {target.get('set_id')}: Grid не подтвердил перепривязку кампаний")
            if v5_items and new_set_id:
                if not (token and v5_call):
                    errors.append(f"набор {target.get('set_id')}: нет v5-контекста для перепривязки объявлений")
                else:
                    ok_ads, ad_errs = _v5_rebind_ads_sitelink_set(v5_call, token, login, v5_items, int(new_set_id))
                    replaced += ok_ads
                    if ok_ads:
                        touched_ads.update(it["ad_id"] for it in v5_items)
                    errors.extend(ad_errs)
            if responsive_items and new_set_id:
                ok_ads, ad_errs = _grid_rebind_responsive_ads_sitelink_set(
                    login,
                    responsive_items,
                    [u.get("campaign_id") for u in (target.get("usages") or [])],
                    int(new_set_id),
                    grid_client_factory=grid_client_factory,
                )
                replaced += ok_ads
                if ok_ads:
                    touched_ads.update(it["ad_id"] for it in responsive_items)
                errors.extend(ad_errs)
            if unsupported_items:
                errors.append(f"набор {target.get('set_id')}: {len(unsupported_items)} объявлений с ad-level "
                              "быстрыми ссылками неподдерживаемого типа пропущены")
        except Exception as e:  # noqa: BLE001
            errors.append(f"набор {target.get('set_id')}: {str(e)[:180]}")
    if not replaced and not errors:
        errors.append("не найдены кампании или объявления, привязанные к набору со старым текстом")
    return {"replaced": replaced, "errors": errors, "new_sitelink_set_ids": created_sets}


def _validate_permutation(perm) -> tuple[list[int], str]:
    """Проверяет, что ``perm`` — биекция позиций 0..N-1 (N≥2), не тождественная.

    Возвращает (нормализованный список int, "") при валидности или ([], причина)."""
    try:
        p = [int(x) for x in (perm or [])]
    except (TypeError, ValueError):
        return [], "перестановка должна быть списком целых индексов позиций"
    n = len(p)
    if n < 2:
        return [], "перестановка должна содержать минимум 2 позиции"
    if sorted(p) != list(range(n)):
        return [], "перестановка должна быть биекцией позиций 0..N-1 (без повторов/пропусков)"
    if p == list(range(n)):
        return [], "перестановка тождественна — порядок не меняется"
    return p, ""


def _reorder_sitelinks(
    token: str,
    login: str,
    perm: list[int],
    content: dict,
    v5_call: Callable,
    *,
    target_set_id: str | int | None = None,
    edited_items: list[dict] | None = None,
    grid_client_factory: Callable | None = None,
    uac_client_factory: Callable | None = None,
) -> dict:
    """Позиционная перестановка (permutation по индексам) быстрых ссылок в наборах
    аккаунта: ``result[i] = items[perm[i]]`` для первых ``len(perm)`` позиций;
    хвост (позиции ≥ len(perm)) остаётся на месте.

    ``target_set_id=None`` — перестановка применяется ко ВСЕМ наборам аккаунта (как
    раньше). Если задан — обрабатывается ТОЛЬКО набор с этим set_id (сравнение по
    строке — set_id бывает int (обычный/campaign-level набор) или "uac:<cid>`).

    Наборы, где ссылок МЕНЬШЕ длины перестановки (позиция за пределами длины),
    ПРОПУСКАЮТСЯ с явным отчётом (не падаем, не режем молча). Пути записи по типам:
      • UAC (tp6/7) → PATCH массива ``sitelinks`` (осн. ссылку UAC не трогаем);
      • campaign-level (inheritableSitelinkSet) → ``add_sitelink_set`` + ``set_campaign_sitelink_set``;
      • бывшие ad-level наборы обычных кампаний → сначала очистка overrides на объявлениях,
        затем новый набор назначается campaign-level. Скрытый ad-level fallback запрещён.

    Возврат-безопасность: для RK дедуп ``add_sitelink_set`` даёт бесплатный откат к
    исходному set_id при идентичном содержимом; для UAC исходный порядок сохраняется
    в отчёте (``orig_order``) — обратная перестановка восстанавливает байт-в-байт.
    """
    perm, why = _validate_permutation(perm)
    if not perm:
        return {"replaced": 0, "errors": [why], "reports": []}
    n = len(perm)

    def _apply(items: list) -> list:
        return [items[p] for p in perm] + list(items[n:])

    def _sl_tuple(x) -> tuple:
        """Полный кортеж-идентичность быстрой ссылки: (title, href, description).
        Сравнение порядка ТОЛЬКО по title ложно-негативит swap ссылок с одинаковым
        title, но разными href/description (finding #2) — сверяем весь кортеж."""
        if not isinstance(x, dict):
            return ("", "", "")
        return (
            (x.get("title") or "").strip(),
            (x.get("href") or "").strip(),
            (x.get("description") or "").strip(),
        )

    reports: list[dict] = []
    errors: list[str] = []
    # Детализация затронутого — РАЗНЫЕ единицы, не смешивать в один счётчик (finding #3):
    campaigns_touched = 0   # кампаний перепривязано (campaign-level)
    ads_touched = 0         # объявлений перепривязано (ad-level TextAd/DynamicTextAd)
    uac_sets = 0            # UAC-наборов переставлено
    grid = None
    uac_client = None
    for s in content.get("sitelinks", []):
        if target_set_id is not None and str(s.get("set_id")) != str(target_set_id):
            continue
        items = s.get("items") or []
        set_id = s.get("set_id")
        source = s.get("source")
        level = "uac" if source == "uac" else (s.get("level") or "ad")
        before_titles = [(it.get("title") or "") for it in items]
        rep: dict = {"set_id": set_id, "set_title": s.get("set_title") or "",
                     "level": level, "before": before_titles}
        if len(items) < n:
            rep["status"] = "skipped"
            rep["reason"] = f"в наборе {len(items)} ссылок — перестановка требует {n}"
            reports.append(rep)
            continue
        # finding #5: элемент быстрой ссылки в наборе состоит РОВНО из
        # {title, href, description}. И v5 sitelinks.get (Title/Href/Description),
        # и Grid get_sitelink_sets (title/description/href), и запись
        # add_sitelink_set (title/href/description) оперируют этой же тройкой; per-item
        # `id` назначается сервером и на создании нового набора не пересылается. Поэтому
        # позиционная перестановка снимка `items` не теряет полей набора для
        # campaign-level/ad-level. (UAC-ветка отдельно перечитывает живую деталь.)
        if target_set_id is not None and edited_items is not None:
            new_items = _normalize_sitelink_items(edited_items)
            if not new_items:
                rep["status"] = "skipped"
                rep["reason"] = "итоговый набор после редактирования пуст"
                reports.append(rep)
                continue
        else:
            new_items = _apply(items)
        after_titles = [(it.get("title") or "") for it in new_items]
        rep["after"] = after_titles
        # «Изменился ли порядок» — по ПОЛНОМУ кортежу (title,href,description), не только
        # по title: swap ссылок с одинаковым title, но разными href/desc — реальное
        # изменение, которое title-сравнение проглатывает как «без изменений» (finding #2).
        if [_sl_tuple(it) for it in new_items] == [_sl_tuple(it) for it in items]:
            rep["status"] = "skipped"
            rep["reason"] = "порядок не изменился"
            reports.append(rep)
            continue
        try:
            if source == "uac":
                try:
                    cid = int(s.get("campaign_id") or 0)
                except (TypeError, ValueError):
                    cid = 0
                if cid <= 0:
                    rep["status"] = "skipped"
                    rep["reason"] = "не удалось определить UAC-кампанию"
                    reports.append(rep)
                    continue
                if uac_client is None:
                    uac_client = _uac_client(login, uac_client_factory)
                # Перечитываем деталь кампании и переставляем РЕАЛЬНЫЙ текущий массив
                # (полные элементы, не наш обрезанный снимок) — для byte-safe записи.
                detail = _unwrap_uac_response(
                    uac_client._request("GET", f"/campaign/{cid}", step=f"uac-detail:{cid}"))
                cur = detail.get("sitelinks")
                if not isinstance(cur, list) or len(cur) < n:
                    rep["status"] = "skipped"
                    rep["reason"] = f"UAC деталь: {len(cur) if isinstance(cur, list) else 0} ссылок < {n}"
                    reports.append(rep)
                    continue
                rep["orig_order"] = [((x.get("title") or "") if isinstance(x, dict) else "") for x in cur]
                reordered = _apply(cur)
                _uac_patch_campaign_texts(uac_client, cid, "sitelinks", reordered)
                after = _unwrap_uac_response(
                    uac_client._request("GET", f"/campaign/{cid}", step=f"uac-reorder-rb:{cid}"))
                after_sl = after.get("sitelinks") if isinstance(after.get("sitelinks"), list) else []
                # Порядок сверяем по ПОЛНОМУ кортежу (title,href,description), а не только
                # по title — иначе swap ссылок с одинаковым title даёт ложный «read-back
                # не подтвердил» при реально применённой перестановке (finding #2).
                after_tup = [_sl_tuple(x) for x in after_sl]
                exp_tup = [_sl_tuple(x) for x in reordered]
                cur_tup = [_sl_tuple(x) for x in cur]
                if after_tup[:len(exp_tup)] == exp_tup and after_tup != cur_tup:
                    rep["status"] = "applied"
                    uac_sets += 1
                else:
                    rep["status"] = "error"
                    rep["reason"] = "read-back не подтвердил новый порядок"
                    errors.append(f"UAC {cid}: read-back не подтвердил порядок быстрых ссылок")
                reports.append(rep)
                continue

            campaign_ids = []
            for c in s.get("campaign_ids") or []:
                try:
                    ci = int(c)
                except (TypeError, ValueError):
                    continue
                if ci > 0 and ci not in campaign_ids:
                    campaign_ids.append(ci)
            for usage in (s.get("usages") or []):
                try:
                    ci = int((usage or {}).get("campaign_id") or 0)
                except (TypeError, ValueError):
                    continue
                if ci > 0 and ci not in campaign_ids:
                    campaign_ids.append(ci)
            if not campaign_ids:
                rep["status"] = "skipped"
                rep["reason"] = "не удалось определить кампании для перевода на campaign-level"
                reports.append(rep)
                continue
            if grid is None:
                grid = (grid_client_factory or _grid_client)(login)
            new_set_id = _grid_add_sitelink_set_for_content_editor(grid, new_items)
            if not new_set_id:
                rep["status"] = "error"
                rep["reason"] = "Grid не вернул id нового набора"
                errors.append(f"набор {set_id}: Grid не вернул id нового набора")
                reports.append(rep)
                continue
            cleared_ads, clear_errs = _clear_ad_level_asset_overrides(
                login, content, campaign_ids,
                clear_sitelinks=True,
                grid_client_factory=grid_client_factory,
            )
            errors.extend(clear_errs)
            updated = grid.set_campaign_sitelink_set(campaign_ids, int(new_set_id))
            upd_ids = []
            for row in updated or []:
                try:
                    ci = int((row or {}).get("id") or 0)
                except (TypeError, ValueError):
                    ci = 0
                if ci > 0:
                    upd_ids.append(ci)
            if upd_ids:
                rep["status"] = "applied"
                rep["new_set_id"] = int(new_set_id)
                rep["campaign_ids"] = upd_ids
                if cleared_ads:
                    rep["ads_cleared"] = cleared_ads
                campaigns_touched += len(upd_ids)
                ads_touched += cleared_ads
            else:
                rep["status"] = "error"
                rep["reason"] = "Grid не подтвердил перепривязку кампаний"
                errors.append(f"набор {set_id}: перепривязка кампаний не подтверждена")
            reports.append(rep)
        except Exception as e:  # noqa: BLE001
            rep["status"] = "error"
            rep["reason"] = str(e)[:180]
            errors.append(f"набор {set_id}: {str(e)[:180]}")
            reports.append(rep)

    applied = sum(1 for r in reports if r.get("status") == "applied")
    skipped = sum(1 for r in reports if r.get("status") == "skipped")
    if not applied and not errors and not skipped:
        errors.append(f"набор {target_set_id} не найден в аккаунте" if target_set_id is not None
                      else "в аккаунте нет наборов быстрых ссылок для перестановки")
    # Основная метрика перестановки — КОЛИЧЕСТВО НАБОРОВ (applied_sets). Раньше `replaced`
    # смешивал единицы (кампании + UAC-наборы + объявления) в одно конфузное число
    # (finding #3). Теперь replaced == applied_sets (наборы), а «во что это раскрылось»
    # отдаём отдельными полями. `replaced` держим = applied_sets: воркер по нему решает
    # done/error и пишет в колонку `done`, и это теперь честная единица (наборы).
    return {"replaced": applied, "errors": errors, "reports": reports,
            "applied_sets": applied, "skipped_sets": skipped,
            "campaigns_touched": campaigns_touched,
            "ads_touched": ads_touched, "uac_sets": uac_sets}


def register_sitelinks_routes(
    bp,
    access,
    *,
    _login_allowed,
    _content_tools_allowed,
    _token,
    _enqueue_content_job,
) -> None:
    """Register /api/content-editor/sitelinks/* endpoints."""

    @bp.route("/api/content-editor/sitelinks/reorder_async", methods=["POST"])
    @access
    def ce_sitelinks_reorder_async():
        body = request.json or {}
        login = (body.get("login") or "").strip()
        perm_raw = body.get("perm")
        # target_set_id: null/отсутствует — применить ко ВСЕМ наборам аккаунта (как раньше).
        # Если задан — перестановка коснётся ТОЛЬКО этого конкретного набора (set_id может
        # быть числом (обычный/campaign-level набор) или строкой "uac:<cid>" для UAC).
        target_set_id = body.get("target_set_id")
        edited_items = _normalize_sitelink_items(body.get("edited_items"))
        if target_set_id is not None and not isinstance(target_set_id, (int, str)):
            target_set_id = None
        try:
            campaign_count = int(body.get("campaign_count") or 0)
        except (TypeError, ValueError):
            campaign_count = 0
        if not login:
            return jsonify({"error": "login обязателен"}), 400
        perm, why = _validate_permutation(perm_raw)
        if not perm:
            return jsonify({"error": why or "некорректная перестановка позиций"}), 400
        ok, scope_err = _login_allowed(login)
        if not ok:
            return jsonify({"error": scope_err}), 403
        _, agency, err = _token(login)
        if err:
            return jsonify({"error": err}), 404
        perm_json = json.dumps({
            "perm": perm,
            "target_set_id": target_set_id,
            "edited_items": edited_items or None,
        }, ensure_ascii=False)
        return _enqueue_content_job(
            login, agency, "sitelink_reorder", perm_json, perm_json, "reorder",
            campaign_count, resp_type="sitelink_reorder")

    @bp.route("/api/content-editor/sitelinks/assign_async", methods=["POST"])
    @access
    def ce_sitelinks_assign_async():
        body = request.json or {}
        login = (body.get("login") or "").strip()
        items = _normalize_sitelink_items(body.get("items"))
        source_set_id = body.get("source_set_id")
        try:
            campaign_count = int(body.get("campaign_count") or 0)
        except (TypeError, ValueError):
            campaign_count = 0
        if not login:
            return jsonify({"error": "login обязателен"}), 400
        if not items:
            return jsonify({"error": "эталонный набор быстрых ссылок пуст"}), 400
        ok, scope_err = _login_allowed(login)
        if not ok:
            return jsonify({"error": scope_err}), 403
        _, agency, err = _token(login)
        if err:
            return jsonify({"error": err}), 404
        payload = json.dumps({"items": items, "source_set_id": source_set_id}, ensure_ascii=False)
        return _enqueue_content_job(
            login, agency, "sitelink_assign", payload, payload, "assign",
            campaign_count, resp_type="sitelink_assign")

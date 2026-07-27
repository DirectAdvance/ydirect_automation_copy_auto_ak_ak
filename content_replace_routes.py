"""Content-editor replace helpers and routes.

Extracted from ``routes_content_editor.py`` (structural split). Functions:
    _replace_adaptive_ad_texts
    _replace_uac_texts
    _normalize_ad_content_values
    _assign_ad_content_accountwide
    _norm_link_path / _href_with_new_path / _v5_update_results_errors / _replace_ad_href
    _do_replace (if/elif dispatcher — transferred verbatim, NOT refactored)

Routes registered via ``register_replace_routes``:
    POST /api/content-editor/load
    POST /api/content-editor/links
    POST /api/content-editor/preview
    POST /api/content-editor/replace
    POST /api/content-editor/replace_async
    POST /api/content-editor/links/replace_async
    POST /api/content-editor/ad-content/assign_async
"""
from __future__ import annotations

import json
import re
from typing import Callable

from flask import jsonify, request

from .content_editor_helpers import (
    _frag_trim,
    _grid_client,
    _uac_client,
    _uac_replace_text_items,
    _uac_text_item_text,
    _uac_patch_campaign_texts,
    _unwrap_uac_response,
    _uac_cids_from_targets,
    _v5_paginate,
    _AD_FIELD,
    _SITELINK_TYPES,
    _SITELINK_FIELD,
    _match_targets,
    _already_applied_sitelink_result,
    _ad_href,
    _href_host_path,
    _href_scheme,
)
from .content_callouts_routes import (
    _assign_callout_accountwide,
    _replace_callout_grid,
)
from .content_sitelinks_routes import (
    _normalize_sitelink_items,
    _reorder_sitelinks,
    _assign_sitelink_items_accountwide,
    _replace_sitelink_text_grid,
    _replace_uac_sitelinks,
)


def _normalize_ad_content_values(values, *, limit: int, max_len: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    raw_values = values.splitlines() if isinstance(values, str) else list(values or [])
    for raw in raw_values:
        text = re.sub(r"\s+", " ", str(raw or "")).strip()
        if not text or text in seen:
            continue
        out.append(text[:max_len])
        seen.add(text)
        if len(out) >= max(1, int(limit or 1)):
            break
    return out


def _replace_adaptive_ad_texts(
    login: str,
    typ: str,
    old_text: str,
    new_text: str,
    targets: list[dict],
    *,
    grid_client_factory: Callable | None = None,
) -> dict:
    """Replace title/body text through cookie/Grid ``findAndReplaceText``."""
    old = _frag_trim(old_text)
    new = _frag_trim(new_text)
    if not targets:
        return {"replaced": 0, "errors": ["объявление с таким текстом не найдено"]}
    ad_ids: list[int] = []
    for target in targets:
        try:
            aid = int(target.get("ad_id"))
        except (TypeError, ValueError):
            continue
        if aid > 0 and aid not in ad_ids:
            ad_ids.append(aid)
    grid = (grid_client_factory or _grid_client)(login)
    target_type = {
        "ad_title": "TITLE",
        "ad_title2": "TITLE_EXTENSION",
        "ad_text": "BODY",
    }.get(typ)
    out = grid.find_and_replace_text(
        ad_ids,
        target_types=[target_type],
        search=old,
        replace=new,
        case_sensitive=True,
    )
    return {"replaced": int(out.get("replaced") or 0), "errors": out.get("errors") or []}


def _replace_uac_texts(
    login: str,
    typ: str,
    old_text: str,
    new_text: str,
    targets: list[dict],
    *,
    mode: str = "exact",
    uac_client_factory: Callable | None = None,
) -> dict:
    """Replace tp6/tp7 UAC title/body text through cookie PATCH."""
    old = _frag_trim(old_text)
    new = _frag_trim(new_text)
    if not old:
        return {"replaced": 0, "errors": ["старый текст пустой"]}
    if not new:
        return {"replaced": 0, "errors": ["новый текст пустой"]}
    field_key = "texts" if typ == "ad_text" else "titles"
    campaign_ids = _uac_cids_from_targets(targets)
    if not campaign_ids:
        return {"replaced": 0, "errors": ["не найдены UAC-кампании для замены"]}

    client = _uac_client(login, uac_client_factory)

    replaced = 0
    errors: list[str] = []
    updated_campaigns: list[int] = []
    for cid in campaign_ids:
        try:
            detail = _unwrap_uac_response(client._request("GET", f"/campaign/{cid}", step=f"uac-detail:{cid}"))
            field_value = detail.get(field_key)
            if not isinstance(field_value, list) and field_key == "titles":
                field_key_candidates = ["titles", "title_items"]
            elif not isinstance(field_value, list) and field_key == "texts":
                field_key_candidates = ["texts", "text_items"]
            else:
                field_key_candidates = [field_key]
            changed = 0
            patched = False
            for candidate in field_key_candidates:
                current = detail.get(candidate)
                if not isinstance(current, list):
                    continue
                next_items, candidate_changed = _uac_replace_text_items(current, old, new, mode)
                if not candidate_changed:
                    continue
                _uac_patch_campaign_texts(client, cid, candidate, next_items)
                changed += candidate_changed
                patched = True
                break
            if not patched:
                errors.append(f"кампания {cid}: текст не найден в UAC detail")
                continue
            # Read-back verifies that the target field now contains the new value.
            after = _unwrap_uac_response(client._request("GET", f"/campaign/{cid}", step=f"uac-readback:{cid}"))
            after_values = after.get(candidate) if isinstance(after.get(candidate), list) else []
            after_texts = [_uac_text_item_text(item) for item in after_values]
            if mode == "substring":
                # фрагмент заменён → старой подстроки быть не должно, новая — присутствует
                confirmed = any(new in t for t in after_texts) and not any(old in t for t in after_texts)
            else:
                confirmed = new in after_texts
            if not confirmed:
                errors.append(f"кампания {cid}: read-back не подтвердил новый текст")
                continue
            replaced += changed
            updated_campaigns.append(cid)
        except Exception as e:  # noqa: BLE001
            errors.append(f"кампания {cid}: {str(e)[:180]}")
    return {"replaced": replaced, "errors": errors, "updated_uac_campaigns": updated_campaigns}


def _assign_ad_content_accountwide(
    login: str,
    payload: dict,
    content: dict,
    *,
    grid_client_factory: Callable | None = None,
    uac_client_factory: Callable | None = None,
) -> dict:
    """Assign one title/body set to every supported ad in the account.

    This is the content-editor queue equivalent of accountwide assign for
    callouts/sitelinks. Responsive ads are updated through Grid RMW, UAC tp6/tp7
    through UAC PATCH. Unsupported legacy ad types are reported without mutation.
    """
    titles = _normalize_ad_content_values(payload.get("titles"), limit=7, max_len=56)
    texts = _normalize_ad_content_values(payload.get("texts"), limit=3, max_len=81)
    if not titles and not texts:
        return {"replaced": 0, "errors": ["пустой набор заголовков и текстов"]}

    inventory = content.get("_ads_inventory") or []
    responsive_ids: list[int] = []
    responsive_cids: list[int] = []
    unsupported: dict[str, int] = {}
    for row in inventory:
        subtype = row.get("subtype") or ""
        if subtype == "ResponsiveAd":
            try:
                aid = int(row.get("ad_id") or 0)
                cid = int(row.get("campaign_id") or 0)
            except (TypeError, ValueError):
                continue
            if aid > 0 and aid not in responsive_ids:
                responsive_ids.append(aid)
            if cid > 0 and cid not in responsive_cids:
                responsive_cids.append(cid)
        elif subtype:
            unsupported[subtype] = unsupported.get(subtype, 0) + 1

    errors: list[str] = []
    warnings: list[str] = []
    updated_responsive = 0
    if responsive_ids:
        try:
            grid = (grid_client_factory or _grid_client)(login)
            current = grid.adaptive_ads_for_update(responsive_cids, responsive_ids)
            items: list[dict] = []
            missing = 0
            for aid in responsive_ids:
                item = current.get(aid)
                if not item:
                    missing += 1
                    continue
                if titles:
                    item["titles"] = list(titles)
                if texts:
                    item["bodies"] = list(texts)
                items.append(item)
            if missing:
                warnings.append(f"Grid RMW не вернул {missing} ResponsiveAd из {len(responsive_ids)}")
            updated_responsive = int(grid.update_adaptive_text_ads(items) or 0)
            if updated_responsive < len(items):
                warnings.append(
                    f"Grid обновил {updated_responsive} ResponsiveAd из {len(items)}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"Grid assign ad content: {str(e)[:220]}")

    uac_ids: list[int] = []
    for raw in content.get("_uac_campaign_ids") or []:
        try:
            cid = int(raw or 0)
        except (TypeError, ValueError):
            continue
        if cid > 0 and cid not in uac_ids:
            uac_ids.append(cid)
    updated_uac: list[int] = []
    if uac_ids:
        warnings.append(
            f"UAC/МК пропущены для ad_content_assign: {len(uac_ids)} камп.")

    if unsupported:
        warnings.append("неподдерживаемые типы объявлений пропущены: " +
                        ", ".join(f"{k}={v}" for k, v in sorted(unsupported.items())))
    changed_units = updated_responsive + len(updated_uac)
    if changed_units <= 0 and not errors:
        errors.append("не найдено поддерживаемых объявлений для назначения контента")
    return {
        "replaced": changed_units,
        "errors": errors,
        "warnings": warnings,
        "titles": titles,
        "texts": texts,
        "responsive_ads_touched": updated_responsive,
        "responsive_ads_targeted": len(responsive_ids),
        "updated_uac_campaigns": updated_uac,
        "uac_campaigns_targeted": len(uac_ids),
        "unsupported": unsupported,
    }


def _norm_link_path(path: str) -> str:
    """Нормализует путь-суффикс для сравнения/записи: ведущий '/', без хвостовых пробелов."""
    p = str(path or "").strip()
    if p and not p.startswith("/"):
        p = "/" + p
    return p


def _href_with_new_path(href: str, new_path: str) -> str:
    """Тот же scheme+host+fragment исходного Href, но path/query = new_path.
    Host НЕ меняем (в отличие от copy_engine._copy_target_href — там смена домена).
    Через urlsplit/urlunsplit, НЕ слепой str.replace."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(href if "://" in str(href or "") else "https://" + str(href or ""))
    np = _norm_link_path(new_path)
    query = ""
    if "?" in np:
        np, query = np.split("?", 1)
    return urlunsplit((parts.scheme or "https", parts.netloc, np, query, parts.fragment))


def _v5_update_results_errors(resp: dict) -> tuple[int, list[str]]:
    """(успешно_обновлено, [ошибки]) из ответа v5/v501 ads.update."""
    if not isinstance(resp, dict):
        return 0, ["пустой ответ ads.update"]
    top = resp.get("error")
    if isinstance(top, dict):
        msg = top.get("error_string") or top.get("error_detail") or str(top)
        return 0, [f"ads.update: {str(msg)[:200]}"]
    results = (resp.get("result") or {}).get("UpdateResults") or []
    ok_n = 0
    errs: list[str] = []
    for r in results:
        r_errs = r.get("Errors") or []
        if r_errs:
            aid = r.get("Id")
            for e in r_errs:
                errs.append(f"ad {aid}: {e.get('Code')} {e.get('Message') or ''} {e.get('Details') or ''}".strip())
        elif r.get("Id"):
            ok_n += 1
    return ok_n, errs


def _replace_ad_href(token: str, login: str, old_path: str, new_path: str,
                     content: dict, v5_call: Callable, v501_svc: Callable) -> dict:
    """Массовая смена посадочной ссылки (Href) во всех объявлениях, где путь == old_path.
    Host сохраняется, меняется только суффикс. TextAd → v5 ads.update;
    ResponsiveAd → v501 ads.update (v5 отвергает весь тип, Code 3500). Dynamic/фид/UAC —
    у них Href нет, в content['links'] они отсутствуют → естественно пропускаются.
    Идемпотентно: объявления, у которых путь уже == new_path, не совпадут с old_path.
    """
    old_p = _norm_link_path(old_path)
    new_p = _norm_link_path(new_path)
    if not new_p:
        return {"replaced": 0, "errors": ["новый путь пуст"]}
    if new_p == old_p:
        return {"replaced": 0, "errors": ["новый путь совпадает со старым"]}
    # Собираем объявления с совпадающим путём, группируя по подтипу и новому Href.
    by_subtype: dict[str, list[dict]] = {"TextAd": [], "ResponsiveAd": []}
    skipped: list[str] = []
    seen_ids: set[int] = set()
    for rec in content.get("links") or []:
        if _norm_link_path(rec.get("path")) != old_p:
            continue
        try:
            aid = int(rec.get("ad_id") or 0)
        except (TypeError, ValueError):
            continue
        if aid <= 0 or aid in seen_ids:
            continue
        subtype = rec.get("type") or ""
        if subtype not in ("TextAd", "ResponsiveAd"):
            skipped.append(f"ad {aid}: тип {subtype or '—'} — Href не редактируется")
            continue
        seen_ids.add(aid)
        by_subtype[subtype].append({"id": aid, "href": _href_with_new_path(rec.get("href"), new_p)})
    if not seen_ids:
        return {"replaced": 0, "errors": ["объявлений с таким путём не найдено"], "skipped": skipped}

    replaced = 0
    # skipped (нередактируемые типы) — НЕ ошибки: возвращаем отдельным полем,
    # иначе воркер берёт errors[0] как провал задания даже при успешной замене.
    errors: list[str] = []

    def _flush(subtype: str, api_field: str, caller):
        nonlocal replaced
        items = by_subtype[subtype]
        for i in range(0, len(items), 100):
            chunk = items[i:i + 100]
            params = {"Ads": [{"Id": it["id"], api_field: {"Href": it["href"]}} for it in chunk]}
            resp = caller("ads", "update", token, login, params)
            ok_n, errs = _v5_update_results_errors(resp)
            replaced += ok_n
            errors.extend(errs)

    if by_subtype["TextAd"]:
        _flush("TextAd", "TextAd", v5_call)          # TextAd.Href — v5 update ок
    if by_subtype["ResponsiveAd"]:
        _flush("ResponsiveAd", "ResponsiveAd", v501_svc)  # ResponsiveAd.Href — только v501

    # Read-back: перечитываем Href по обновлённым ad_id (v5 GET работает для обоих типов).
    all_ids = sorted(seen_ids)
    confirmed = 0
    unconfirmed: list[int] = []
    try:
        rb, rb_err = _v5_paginate(
            v5_call, "ads", token, login,
            {"SelectionCriteria": {"Ids": all_ids},
             "FieldNames": ["Id", "Type"],
             "TextAdFieldNames": ["Href"],
             "ResponsiveAdFieldNames": ["Href"]},
            "Ads",
        )
        if rb_err:
            errors.append(f"read-back: {rb_err}")
        else:
            for a in rb:
                h = _ad_href(a)
                if _norm_link_path(_href_host_path(h)[1]) == new_p:
                    confirmed += 1
                else:
                    unconfirmed.append(int(a.get("Id") or 0))
    except Exception as e:  # noqa: BLE001
        errors.append(f"read-back упал: {str(e)[:160]}")
    if unconfirmed:
        errors.append(f"read-back не подтвердил новый путь у {len(unconfirmed)} объявл.: "
                      f"{unconfirmed[:10]}")
    return {"replaced": replaced, "confirmed": confirmed, "targets": len(all_ids),
            "errors": errors, "skipped": skipped}


def _do_replace(token: str, login: str, typ: str, old_text: str, new_text: str,
                content: dict, v5_call: Callable, v501_svc: Callable,
                *, mode: str = "exact", grid_client_factory: Callable | None = None,
                uac_client_factory: Callable | None = None) -> dict:
    """Применяет замену. Возвращает {'replaced': N, 'errors': [...]}."""
    old = _frag_trim(old_text)
    new = _frag_trim(new_text)
    if typ == "ad_href":
        return _replace_ad_href(token, login, old, new, content, v5_call, v501_svc)
    if typ == "image_replace":
        # Смена изображения: new_text — JSON {"campaign_ids": [...], "pairs": [...]}
        # (пути к временным файлам, не байты). Реализация — content_images_routes;
        # импорт ленивый, иначе циклическая зависимость модулей.
        from .content_images_routes import run_image_replace

        try:
            payload = json.loads(new_text)
        except (TypeError, ValueError):
            return {"replaced": 0, "errors": ["не удалось разобрать задание смены изображения"]}
        return run_image_replace(
            token, login, payload, v5_call,
            grid_client_factory=grid_client_factory,
            uac_client_factory=uac_client_factory,
        )
    if typ == "campaign_rename":
        # Переименование кампаний и/или групп: new_text — JSON
        # {"campaign_renames": {id: name, ...}, "adgroup_renames": {id: name, ...},
        #  "adgroup_campaign_ids": [...]}
        from .content_renames_routes import run_rename
        try:
            payload = json.loads(new_text)
        except (TypeError, ValueError):
            return {"replaced": 0, "errors": ["не удалось разобрать задание переименования"]}
        return run_rename(
            login,
            payload,
            grid_client_factory=grid_client_factory,
        )
    if typ == "sitelink_reorder":
        # Перестановка порядка: new_text — JSON {"perm": [...], "target_set_id": ..., "edited_items": [...]}
        # (target_set_id=None — применить ко ВСЕМ наборам аккаунта; иначе — только к
        # этому конкретному набору). Старый формат (голый массив perm, без обёртки) —
        # поддержан для заданий, поставленных в очередь до этой правки.
        try:
            payload = json.loads(new_text)
        except (TypeError, ValueError):
            return {"replaced": 0, "errors": ["не удалось разобрать перестановку позиций"], "reports": []}
        if isinstance(payload, list):
            perm, target_set_id, edited_items = payload, None, None
        else:
            perm, target_set_id = payload.get("perm"), payload.get("target_set_id")
            edited_items = payload.get("edited_items")
        return _reorder_sitelinks(
            token, login, perm, content, v5_call,
            target_set_id=target_set_id,
            edited_items=edited_items,
            grid_client_factory=grid_client_factory,
            uac_client_factory=uac_client_factory,
        )
    if typ == "sitelink_assign":
        try:
            payload = json.loads(new_text)
        except (TypeError, ValueError):
            return {"replaced": 0, "errors": ["не удалось разобрать эталонный набор быстрых ссылок"]}
        items = _normalize_sitelink_items((payload or {}).get("items"))
        return _assign_sitelink_items_accountwide(
            token, login, items, content, v5_call,
            grid_client_factory=grid_client_factory,
            uac_client_factory=uac_client_factory,
        )
    if typ == "callout_assign":
        payload = None
        assign_texts = new_text
        try:
            payload = json.loads(new_text)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("texts"), list):
            assign_texts = payload.get("texts")
        return _assign_callout_accountwide(
            login,
            assign_texts,
            content,
            grid_client_factory=grid_client_factory,
        )
    if typ == "ad_content_assign":
        try:
            payload = json.loads(new_text)
        except (TypeError, ValueError):
            return {"replaced": 0, "errors": ["не удалось разобрать набор заголовков/текстов"]}
        return _assign_ad_content_accountwide(
            login,
            payload if isinstance(payload, dict) else {},
            content,
            grid_client_factory=grid_client_factory,
            uac_client_factory=uac_client_factory,
        )
    if mode == "substring":
        if typ not in _AD_FIELD:
            return {"replaced": 0, "errors": ["массовая замена фрагмента доступна только для заголовков и текстов"]}
        if len(new) > len(old):
            return {"replaced": 0, "errors": [
                f"новый фрагмент ({len(new)}) длиннее старого ({len(old)}) — заголовки вырастут, замена отклонена"]}
    if typ in _AD_FIELD:
        targets = _match_targets(content, typ, old, mode=mode, new_text=new_text)
        if not targets:
            return {"replaced": 0, "errors": ["объявление с таким текстом не найдено"]}
        non_uac = [t for t in targets if t.get("source") != "uac"]
        uac_targets = [t for t in targets if t.get("source") == "uac"]
        replaced = 0
        errors: list[str] = []
        result: dict = {}
        if non_uac:
            out = _replace_adaptive_ad_texts(
                login,
                typ,
                old,
                new_text,
                non_uac,
                grid_client_factory=grid_client_factory,
            )
            replaced += int(out.get("replaced") or 0)
            errors.extend(out.get("errors") or [])
            result["grid"] = out
        if uac_targets:
            out = _replace_uac_texts(
                login,
                typ,
                old,
                new_text,
                uac_targets,
                mode=mode,
                uac_client_factory=uac_client_factory,
            )
            replaced += int(out.get("replaced") or 0)
            errors.extend(out.get("errors") or [])
            result["uac"] = out
        return {"replaced": replaced, "errors": errors, **result}

    if typ == "callout":
        return _replace_callout_grid(
            token,
            login,
            old,
            new_text,
            content,
            v5_call,
            grid_client_factory=grid_client_factory,
        )

    if typ in _SITELINK_TYPES:
        targets = _match_targets(content, typ, old)
        if not targets:
            already = _already_applied_sitelink_result(content, typ, new_text)
            if already is not None:
                return already
            return {"replaced": 0, "errors": ["набор с таким текстом быстрой ссылки не найден"]}
        uac_targets = [t for t in targets if t.get("source") == "uac"]
        grid_targets = [t for t in targets if t.get("source") != "uac"]
        replaced = 0
        errors: list[str] = []
        result: dict = {}
        if grid_targets:
            out = _replace_sitelink_text_grid(
                login,
                typ,
                old,
                new_text,
                grid_targets,
                token=token,
                v5_call=v5_call,
                grid_client_factory=grid_client_factory,
            )
            replaced += int(out.get("replaced") or 0)
            errors.extend(out.get("errors") or [])
            result["grid"] = out
        if uac_targets:
            out = _replace_uac_sitelinks(
                login,
                _SITELINK_FIELD.get(typ, "title"),
                old,
                new_text,
                uac_targets,
                uac_client_factory=uac_client_factory,
            )
            replaced += int(out.get("replaced") or 0)
            errors.extend(out.get("errors") or [])
            result["uac"] = out
        return {"replaced": replaced, "errors": errors, **result}

    return {"replaced": 0, "errors": [f"неизвестный тип: {typ}"]}


def register_replace_routes(
    bp,
    access,
    *,
    v5_call: Callable,
    v501_svc: Callable,
    _login_allowed: Callable,
    _content_tools_allowed: Callable,
    _token: Callable,
    _load_with_index: Callable,
    _enqueue_content_job: Callable,
    _norm_link_path_fn: Callable | None = None,
    _href_scheme_fn: Callable | None = None,
) -> None:
    """Register /api/content-editor/load, preview, replace, replace_async, links*, ad-content/*."""

    # Allow callers to inject alternative helpers (legacy test compat).
    _path_fn = _norm_link_path_fn or _norm_link_path
    _scheme_fn = _href_scheme_fn or _href_scheme

    @bp.route("/api/content-editor/load", methods=["POST"])
    @access
    def ce_load():
        login = ((request.json or {}).get("login") or "").strip()
        if not login:
            return jsonify({"error": "login обязателен"}), 400
        ok, scope_err = _login_allowed(login)
        if not ok:
            return jsonify({"error": scope_err}), 403
        token, agency, err = _token(login)
        if err:
            return jsonify({"error": err}), 404
        # Вкладка «Быстрые ссылки» на главном /load ДОЛЖНА видеть campaign-level наборы.
        content = _load_with_index(token, login, include_campaign_sitelinks=True)
        if content.get("error"):
            return jsonify({"error": content["error"]}), 502
        resp = {
            "login": login, "agency": agency,
            "callouts": content["callouts"],
            "sitelinks": content["sitelinks"],
            "ads": content["ads"],
        }
        # Если блок 3c ВЫПОЛНЯЛСЯ и упал — не молчим: фронт покажет заметное
        # предупреждение (замена набора уровня кампании может не примениться).
        if content.get("_grid_sitelink_error"):
            resp["_grid_sitelink_error"] = content["_grid_sitelink_error"]
        # UAC-чтение (tp6/7) и Grid-маппинг callout→кампании тоже могут упасть —
        # без этого фронт видит «нет объектов»/пустые usages вместо «чтение упало».
        if content.get("_uac_read_error"):
            resp["_uac_read_error"] = content["_uac_read_error"]
        if content.get("_grid_callout_error"):
            resp["_grid_callout_error"] = content["_grid_callout_error"]
        return jsonify(resp)

    @bp.route("/api/content-editor/links", methods=["POST"])
    @access
    def ce_links():
        body = request.json or {}
        logins = body.get("logins")
        if isinstance(logins, str):
            logins = [logins]
        if not logins:
            one = (body.get("login") or "").strip()
            logins = [one] if one else []
        logins = [str(x).strip() for x in (logins or []) if str(x).strip()]
        if not logins:
            return jsonify({"error": "login/logins обязателен"}), 400
        groups: dict[str, dict] = {}
        errors: list[dict] = []
        for login in logins:
            ok, scope_err = _login_allowed(login)
            if not ok:
                errors.append({"login": login, "error": scope_err})
                continue
            token, _agency, err = _token(login)
            if err:
                errors.append({"login": login, "error": err})
                continue
            # /links не нужны campaign-level наборы → не гоняем Grid-round-trip (блок 3c).
            content = _load_with_index(token, login, include_campaign_sitelinks=False)
            if content.get("error"):
                errors.append({"login": login, "error": content["error"]})
                continue
            for lk in content.get("links") or []:
                path = lk.get("path") or ""
                if not path:
                    continue
                g = groups.setdefault(path, {
                    "path": path,
                    # Нормализация показа: реальный host заменяем на site.ru, путь сохраняем.
                    "template_url": "https://site.ru" + path,
                    "_ads": set(), "_camps": set(), "_accounts": set(),
                    "live_count": 0, "_detail": {},
                })
                g["_ads"].add((login, lk.get("ad_id")))
                g["_camps"].add((login, lk.get("campaign_id")))
                g["_accounts"].add(login)
                is_live = str(lk.get("state") or "").upper() == "ON"
                if is_live:
                    g["live_count"] += 1
                host = lk.get("host") or ""
                # Реальная схема исходного Href (обычно https, но не хардкодим) —
                # чтобы превью «было → стало» и запись совпали по scheme.
                scheme = _scheme_fn(lk.get("href"))
                det = g["_detail"].setdefault(
                    (login, host),
                    {"login": login, "host": host, "scheme": scheme, "ads": 0, "live": 0},
                )
                det["ads"] += 1
                if is_live:
                    det["live"] += 1
        out_groups: list[dict] = []
        for _path, g in groups.items():
            out_groups.append({
                "path": g["path"],
                "template_url": g["template_url"],
                "ads_count": len(g["_ads"]),
                "campaigns_count": len(g["_camps"]),
                "accounts_count": len(g["_accounts"]),
                "live_count": g["live_count"],
                # Детализация по аккаунтам — чтобы UI собрал превью было→стало
                # с РЕАЛЬНЫМ доменом каждого аккаунта (без записи).
                "accounts": list(g["_detail"].values()),
            })
        out_groups.sort(key=lambda x: (-x["ads_count"], x["path"]))
        return jsonify({"logins": logins, "groups": out_groups, "errors": errors})

    @bp.route("/api/content-editor/preview", methods=["POST"])
    @access
    def ce_preview():
        body = request.json or {}
        login = (body.get("login") or "").strip()
        typ = (body.get("type") or "").strip()
        old_text = body.get("old_text") or ""
        new_text = body.get("new_text") or ""
        mode = (body.get("mode") or "exact").strip()
        if not login or not typ or not old_text.strip():
            return jsonify({"error": "login, type и old_text обязательны"}), 400
        ok, scope_err = _login_allowed(login)
        if not ok:
            return jsonify({"error": scope_err}), 403
        if typ not in _AD_FIELD and typ != "callout" and typ not in _SITELINK_TYPES:
            return jsonify({"error": f"неизвестный тип: {typ}"}), 400
        if mode == "substring" and typ not in _AD_FIELD:
            return jsonify({"error": "массовая замена фрагмента доступна только для заголовков и текстов"}), 400
        token, _, err = _token(login)
        if err:
            return jsonify({"error": err}), 404
        # campaign-level наборы нужны превью ТОЛЬКО для sitelink-типов.
        content = _load_with_index(
            token, login, include_campaign_sitelinks=(typ in _SITELINK_TYPES))
        if content.get("error"):
            return jsonify({"error": content["error"]}), 502
        if mode == "substring":
            old = _frag_trim(old_text)
            new = _frag_trim(new_text)
            guard_ok = len(new) <= len(old)
            hits = _match_targets(content, typ, old, mode="substring", new_text=new)
            # уникальные заголовки/тексты для визуальной проверки (before→after)
            seen: set[str] = set()
            items: list[dict] = []
            for h in hits:
                before = h.get("before") or ""
                if before in seen:
                    continue
                seen.add(before)
                after = h.get("after") or ""
                items.append({
                    "before": before, "after": after,
                    "len_before": len(before), "len_after": len(after),
                })
            items.sort(key=lambda it: it["before"].lower())
            return jsonify({
                "mode": "substring", "objects": len(hits), "distinct": len(items),
                "guard_ok": guard_ok, "old_len": len(old), "new_len": len(new),
                "items": items,
            })
        hits = _match_targets(content, typ, old_text)
        usages: list[dict] = []
        for h in hits:
            usages.extend(h.get("usages", []))
        return jsonify({"objects": len(hits), "usages_count": len(usages), "usages": usages})

    @bp.route("/api/content-editor/replace", methods=["POST"])
    @access
    def ce_replace():
        body = request.json or {}
        login = (body.get("login") or "").strip()
        typ = (body.get("type") or "").strip()
        old_text = body.get("old_text") or ""
        new_text = body.get("new_text") or ""
        mode = (body.get("mode") or "exact").strip()
        if not login or not typ or not old_text.strip() or not new_text.strip():
            return jsonify({"error": "login, type, old_text и new_text обязательны"}), 400
        ok, scope_err = _login_allowed(login)
        if not ok:
            return jsonify({"error": scope_err}), 403
        if typ not in _AD_FIELD and typ != "callout" and typ not in _SITELINK_TYPES:
            return jsonify({"error": f"неизвестный тип: {typ}"}), 400
        if mode == "substring" and len(_frag_trim(new_text)) > len(_frag_trim(old_text)):
            return jsonify({"error": "новый фрагмент длиннее старого — замена отклонена"}), 400
        token, _, err = _token(login)
        if err:
            return jsonify({"error": err}), 404
        # sitelink-замена набора уровня кампании требует блок 3c; остальным типам — нет.
        content = _load_with_index(
            token, login, include_campaign_sitelinks=(typ in _SITELINK_TYPES))
        if content.get("error"):
            return jsonify({"error": content["error"]}), 502
        out = _do_replace(token, login, typ, old_text, new_text, content, v5_call, v501_svc, mode=mode)
        return jsonify(out)

    @bp.route("/api/content-editor/replace_async", methods=["POST"])
    @access
    def ce_replace_async():
        body = request.json or {}
        login = (body.get("login") or "").strip()
        typ = (body.get("type") or "").strip()
        old_text = body.get("old_text") or ""
        new_text = body.get("new_text") or ""
        mode = (body.get("mode") or "exact").strip()
        try:
            campaign_count = int(body.get("campaign_count") or 0)
        except (TypeError, ValueError):
            campaign_count = 0
        if not login or not typ or not old_text.strip() or not new_text.strip():
            return jsonify({"error": "login, type, old_text и new_text обязательны"}), 400
        ok, scope_err = _login_allowed(login)
        if not ok:
            return jsonify({"error": scope_err}), 403
        if typ not in _AD_FIELD and typ != "callout" and typ not in _SITELINK_TYPES:
            return jsonify({"error": f"неизвестный тип: {typ}"}), 400
        if mode == "substring":
            if typ not in _AD_FIELD:
                return jsonify({"error": "массовая замена фрагмента доступна только для заголовков и текстов"}), 400
            if len(_frag_trim(new_text)) > len(_frag_trim(old_text)):
                return jsonify({"error": "новый фрагмент длиннее старого — замена отклонена"}), 400
        _, agency, err = _token(login)
        if err:
            return jsonify({"error": err}), 404
        return _enqueue_content_job(
            login, agency, typ, old_text, new_text,
            (mode if mode == "substring" else "exact"), campaign_count)

    @bp.route("/api/content-editor/links/replace_async", methods=["POST"])
    @access
    def ce_links_replace_async():
        body = request.json or {}
        login = (body.get("login") or "").strip()
        old_path = _path_fn(body.get("old_path") or "")
        new_path = _path_fn(body.get("new_path") or "")
        try:
            campaign_count = int(body.get("campaign_count") or 0)
        except (TypeError, ValueError):
            campaign_count = 0
        if not login or not old_path or not new_path:
            return jsonify({"error": "login, old_path и new_path обязательны"}), 400
        if new_path == old_path:
            return jsonify({"error": "новый путь совпадает со старым"}), 400
        ok, scope_err = _login_allowed(login)
        if not ok:
            return jsonify({"error": scope_err}), 403
        _, agency, err = _token(login)
        if err:
            return jsonify({"error": err}), 404
        return _enqueue_content_job(
            login, agency, "ad_href", old_path, new_path, "link", campaign_count,
            resp_type="ad_href")

    @bp.route("/api/content-editor/ad-content/assign_async", methods=["POST"])
    @access
    def ce_ad_content_assign_async():
        body = request.json or {}
        login = (body.get("login") or "").strip()
        titles = _normalize_ad_content_values(body.get("titles"), limit=7, max_len=56)
        texts = _normalize_ad_content_values(body.get("texts"), limit=3, max_len=81)
        try:
            campaign_count = int(body.get("campaign_count") or 0)
        except (TypeError, ValueError):
            campaign_count = 0
        if not login:
            return jsonify({"error": "login обязателен"}), 400
        if not titles and not texts:
            return jsonify({"error": "пустой набор заголовков и текстов"}), 400
        ok, scope_err = _login_allowed(login)
        if not ok:
            return jsonify({"error": scope_err}), 403
        _, agency, err = _token(login)
        if err:
            return jsonify({"error": err}), 404
        payload = json.dumps({"titles": titles, "texts": texts}, ensure_ascii=False)
        return _enqueue_content_job(
            login, agency, "ad_content_assign", payload, payload, "assign",
            campaign_count, resp_type="ad_content_assign")

"""Keyword-domain repair executors: keyword repair and keywords_wrong_group (shift) fix.

Functions here fix keyword defects on search campaigns through cookie/Grid + v5 delete
(no Direct API create units). All shared state is imported from repair_common.
"""
from __future__ import annotations

from typing import Any

from .repair_common import (
    RepairDeps,
    _unique_positive_ints,
    cmc,
    gf,
    _KEYWORDS_MIN,
    _KW_MAX_PER_GROUP,
    _SEARCH_TPS,
    _CT_RE,
    _TP_RE,
)


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
    # Отсутствие аккаунта в БД (незарегистрированные dmp-аккаунты) больше НЕ прерывает
    # автотаргет-репейр: узкий профиль поиска возвращается по куке+Grid, строка аккаунта
    # из БД тут не нужна. Куку для login резолвим по кабинету, agency берём из ctx/body
    # (или из acc, если аккаунт всё же зарегистрирован — поведение для него не меняется).
    # Реальная невозможность (нет куки/agency) отлавливается ниже как 502, не маскируется.
    acc = deps.account_ctx(login) or {}
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
    unfixable: list[dict[str, Any]] = []        # КС-группы без источника ключей (пак пуст/недоступен) — нечинимый остаток, НЕ провал докрутки
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
            # Реально заливаемые ключи: спецключ "---autotargeting" НЕ заливается через Grid
            # (фильтруется ниже) — группа только с ним эквивалентна группе без источника.
            writable_kw = [k for k in final_kw if str(k).strip() and not str(k).startswith("---")]
            if need_kw and not writable_kw and not need_at:
                # Источник контента не даёт ключей для этого ct. Два разных случая:
                # (а) «…-Автотаргетинг-…» — группа живёт на AT по дизайну, 0 ключей норма → ok.
                # (б) КС-кампания (без «автотаргетинг» в имени) — пак пуст / M3 недоступен → это
                #     НЕ провал докрутки, а нечинимый СЕЙЧАС остаток по ЭТОЙ группе (аналог
                #     video_no_pool): ключей физически нет, пока пак не появится. Кладём в
                #     ОТДЕЛЬНЫЙ bucket `unfixable`, а НЕ в `failed`, чтобы одна беспаковая группа
                #     не роняла ok/executed всей докрутки (иначе реально применённые группы
                #     репортятся как «0 действий» и уходят в reschedule вхолостую). audit↔repair
                #     не расходятся: unfixable-группы явно возвращаются в ответе (unfixable_no_pack),
                #     а если применить не удалось НИЧЕГО (только беспаковые) — ok=False ниже, т.е.
                #     «всё идемпотентно/ок» по-прежнему НЕ выдаётся. Ключи не выдумываем.
                _at_by_design = "автотаргетинг" in (camp_name or "").lower()
                if _at_by_design:
                    results.append({"ok": True, "skipped": "нет источника ключей (автотаргет активен)",
                                    "campaign_id": cid, "adgroup_id": gid})
                    skipped += 1
                else:
                    unfixable.append({"campaign_id": cid, "adgroup_id": gid,
                                      "error": ("нет ключей от pack для этого ct "
                                                "(pack недоступен/пуст; поисковая группа без ключей)")})
                continue
            # Кап 200/группа (лимит Яндекса): иначе Grid AddKeywords отклонит всю пачку
            # (MAX_KEYWORDS_PER_AD_GROUP_EXCEEDED) → группа останется без ключей (NO_KEYWORDS_LIVE).
            if len(final_kw) > _KW_MAX_PER_GROUP:
                print(f"[keywords_repair] cid={cid} gid={gid}: усечено "
                      f"{len(final_kw)}→{_KW_MAX_PER_GROUP} ключей (лимит Яндекса)", flush=True)
                final_kw = final_kw[:_KW_MAX_PER_GROUP]
            target_rm = {"isActive": True, "id": (rm or {}).get("id") if isinstance(rm, dict) else None,
                         "relevanceMatchCategories": ["EXACT_V2_MARK"],
                         "autotargetingBrandSettings": ["WITHOUT_BRAND"]}
            try:
                # keywords=[] намеренно: UpdateUnifiedAdGroups — подтверждённый no-op для ключей
                # (они живут отдельно и заливаются AddKeywords ниже; этот апдейт их НЕ добавляет и
                # НЕ удаляет). Round-trip существующих ключей группы сюда лишь ловил
                # MUST_NOT_CONTAIN_DUPLICATED_ELEMENTS на дублях фраз и рубил весь батч. Шлём пустой
                # массив → валидация коллекции ключей проходит, а relevanceMatch (EXACT_V2_MARK/
                # WITHOUT_BRAND) применяется. Живые ключи группы не трогаются.
                item = grid.build_update_item(grp, keywords=[], relevance_match=target_rm)
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

    # Обновляем relevanceMatch через UpdateUnifiedAdGroups для групп с need_at=True.
    # UpdateUnifiedAdGroups no-op для keywords → безопасно для NO_KEYWORDS_LIVE-only групп.
    # Пишем ПО ОДНОЙ КАМПАНИИ (per-cid): валидационная ошибка одной группы рубит только её
    # кампанию (~8 групп), а не весь батч из 68 групп — остальные кампании всё равно сужаются.
    at_gids = {gid for gid, intent in intents.items() if intent.get("fixed_autotarget")}
    at_failed_gids: set[int] = set()
    if at_gids and write_items:
        items_by_cid: dict[int, list[dict]] = {}
        gids_by_cid: dict[int, list[int]] = {}
        for it in write_items:
            try:
                _gid = int(it.get("adGroupId") or 0)
            except (TypeError, ValueError):
                _gid = 0
            _cid = int((intents.get(_gid) or {}).get("campaign_id") or 0)
            items_by_cid.setdefault(_cid, []).append(it)
            gids_by_cid.setdefault(_cid, []).append(_gid)
        for _cid, _items in items_by_cid.items():
            try:
                grid.update_unified_adgroups(_items)
            except Exception as e:  # noqa: BLE001
                failed.append({"campaign_id": _cid,
                               "error": f"Grid UpdateUnifiedAdGroups (autotarget): {str(e)[:200]}"})
                at_failed_gids.update(gids_by_cid.get(_cid, []))

    updated_set: set[int] = set()
    for gid, intent in intents.items():
        # Считаем группу «применённой» если: ключи залиты (kw_added) ИЛИ нужна только
        # автотаргет-правка (need_at без need_kw — update_unified_adgroups вызван выше) И её
        # кампания не упала при per-cid записи (at_failed_gids).
        if gid in kw_added or (not intent.get("fixed_keywords") and intent.get("fixed_autotarget")
                               and gid not in at_failed_gids):
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

    # ok отражает РЕАЛЬНЫЙ прогресс: True если нет настоящих провалов (failed) И есть что зачесть —
    # либо реально применённые группы (applied), либо вообще нет беспаковых остатков (штатный идемпотент).
    # Если применить не удалось НИЧЕГО, а есть только беспаковые группы (unfixable) → ok=False:
    # честно сообщаем «ничего не добили» (executed=0 у вызывающего верно), а не «всё ок».
    ok = (not failed) and (bool(applied) or not unfixable)
    if not write_items and not failed and not unfixable:
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
        "unfixable_no_pack": len(unfixable),
        "unfixable_groups": unfixable[:40],
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
         "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8",
         "Use-Operator-Units": "true"}
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
    from ..clients import grid_read as _gr
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

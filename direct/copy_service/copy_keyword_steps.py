"""Шаг дозаливки ключевых фраз copy-постпроцесса."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Optional

from .copy_context import CopyCtx, _noop_log
from .copy_step_utils import _chunks, _rj, _v5_add_err, _wj


def step_keywords(ctx: CopyCtx, grid_batch: int = 1000, v5_batch: int = 900) -> dict:
    """ФАЗА 3c п.2. Добавить ключевые фразы копии Grid-FIRST (``grid.add_keywords`` по куки, 0
    v5-баллов), v5 ``keywords.add`` — ТОЛЬКО фолбэк. Раньше базовый движок (direct_copy.phase_upload)
    слал ВСЁ через v5 (главный пожиратель баллов → 152), а Grid добирал лишь остаток. Теперь v5-путь
    в phase_upload выключен (skip_keywords=True), а этот шаг — основной.

    Инварианты переноса (сохранены):
      • group-remap: фраза привязывается к НОВОЙ adgroup-id (maps['adgroups'][src_group]);
      • ставки: v5 ``Bid``→Grid ``price`` (руб), ``ContextBid``→Grid ``priceContext`` (руб);
      • UserParam1/UserParam2: Grid ``AddKeywords`` их НЕ умеет (интроспекция GdAddKeywordsItemInput:
        только adGroupId/keyword/price/priceContext) → такие фразы идут в v5 (сохраняем UserParam;
        это малая доля, единичные баллы);
      • учёт добавленных (``keywords_done.json``) — без дублей, идемпотентно на повторном прогоне.

    Фолбэк-безопасно: нет grid → всё в v5; нет v5-токена → недобавленные честно в rep['failed'].
    Порядок: Grid по батчам (отказ одного батча не сбрасывает весь набор в v5), потом v5 для
    UserParam-фраз и не прошедших Grid."""
    rep = {"total": 0, "via_grid": 0, "via_v5": 0, "v5_userparam": 0, "failed": 0,
           "skipped_no_group": 0, "already_done": 0, "grid_failed_batches": 0, "errors": []}
    keywords = _rj(ctx.src_dir / "keywords.json")
    if not keywords:
        return rep
    done_path = ctx.workdir / "keywords_done.json"
    done_kw: set[str] = set(_rj(done_path)) if done_path.exists() else set()
    adg_map = ctx.maps.get("adgroups") or {}

    # Тип-детектор: TEXT_AD_GROUP (старый формат, v5-создание) → ключи в v5 напрямую.
    # Grid addKeywords для TextAdGroup возвращает ложный n_added==len(batch) (не пустой addedItems),
    # при этом ключи НЕ персистятся (прогон 2026-07-17, porg-lzjk6p5m tp2; run-15 с n_added-фиксом
    # тоже дал via_v5=0 — Grid врёт на уровне addedItems). Для UNIFIED_AD_GROUP (ЕПК) Grid работает.
    _src_agid_type: dict[str, str] = {}
    _adgroups_file = ctx.src_dir / "adgroups.json"
    if _adgroups_file.exists():
        try:
            _src_agid_type = {
                str(ag.get("Id") or ""): str(ag.get("Type") or "")
                for ag in (_rj(_adgroups_file) or [])
            }
        except Exception:  # noqa: BLE001
            pass  # безопасно: при ошибке adg_type пуст, все ключи идут в Grid (прежнее поведение)

    # Задача 1: гео-замена фраз ключевых слов (морфологическая, те же пары что у имён/текстов).
    # copy_geo_morph импортируется лениво (не на уровне модуля — паттерн copy_steps.py).
    _geo_pairs_kw = ctx.geo_pairs or []
    if _geo_pairs_kw:
        from . import copy_geo_morph as _cgm_kw

    grid_rows: list[dict] = []
    grid_keys: list[str] = []
    v5_rows: list[dict] = []          # фразы с UserParam → только v5 (Grid не переносит UserParam)
    v5_keys: list[str] = []
    v5_text_rows: list[dict] = []     # TEXT_AD_GROUP фразы → v5 напрямую (Grid addKeywords лжёт)
    v5_text_keys: list[str] = []
    _kw_geo_count = 0   # счётчик фраз, где применилась гео-замена
    for k in keywords:
        key = f"{k.get('AdGroupId')}|{k.get('Keyword')}"
        rep["total"] += 1
        if key in done_kw:
            rep["already_done"] += 1
            continue
        gid = adg_map.get(str(k.get("AdGroupId") or ""))
        phrase = str(k.get("Keyword") or "").strip()
        if not gid or not str(gid).isdigit() or not phrase or phrase.startswith("---"):
            rep["skipped_no_group"] += 1
            continue
        # Задача 1: применить гео-морфологию к фразе (город/область источника → целевой).
        if _geo_pairs_kw and phrase:
            _phrase_geo, _n_geo = _cgm_kw.apply_replacements(phrase, _geo_pairs_kw)
            if _n_geo:
                phrase = _phrase_geo.strip() or phrase
                _kw_geo_count += 1
        gid = int(gid)
        bid, cbid = k.get("Bid"), k.get("ContextBid")
        up1, up2 = k.get("UserParam1"), k.get("UserParam2")
        if up1 or up2:
            item = {"AdGroupId": gid, "Keyword": phrase}
            if bid is not None:
                try:
                    item["Bid"] = int(bid)
                except (TypeError, ValueError):
                    pass
            if cbid is not None:
                try:
                    item["ContextBid"] = int(cbid)
                except (TypeError, ValueError):
                    pass
            if up1 is not None:
                item["UserParam1"] = up1
            if up2 is not None:
                item["UserParam2"] = up2
            v5_rows.append(item)
            v5_keys.append(key)
        elif _src_agid_type.get(str(k.get("AdGroupId") or "")) == "TEXT_AD_GROUP":
            # TEXT_AD_GROUP (v5-created старый формат): Grid addKeywords возвращает ложный success,
            # ключи фактически не персистятся. Слать сразу в v5 keywords.add (агентские баллы).
            item = {"AdGroupId": gid, "Keyword": phrase}
            if bid is not None:
                try:
                    item["Bid"] = int(bid)
                except (TypeError, ValueError):
                    pass
            if cbid is not None:
                try:
                    item["ContextBid"] = int(cbid)
                except (TypeError, ValueError):
                    pass
            v5_text_rows.append(item)
            v5_text_keys.append(key)
        else:
            row: dict = {"adgroup_id": gid, "keyword": phrase}
            if bid is not None:
                try:
                    row["price"] = round(float(bid) / 1_000_000, 2)
                    row["_bid"] = int(bid)        # оригинал микро — для точного v5-фолбэка
                except (TypeError, ValueError):
                    pass
            if cbid is not None:
                try:
                    row["price_context"] = round(float(cbid) / 1_000_000, 2)
                    row["_context_bid"] = int(cbid)
                except (TypeError, ValueError):
                    pass
            grid_rows.append(row)
            grid_keys.append(key)

    # 1) GRID-FIRST (0 баллов). Свой батчинг, чтобы отказ одного батча не сбросил всё в v5.
    grid_failed_rows: list[dict] = []
    grid_failed_keys: list[str] = []
    if grid_rows and ctx.grid is not None and hasattr(ctx.grid, "add_keywords"):
        for rows_b, keys_b in zip(_chunks(grid_rows, grid_batch), _chunks(grid_keys, grid_batch)):
            try:
                added = ctx.grid.add_keywords(rows_b)
                n_added = len(added or [])
                # Считаем ТОЛЬКО фактически принятое Grid. Раньше стояло
                # `len(added or []) or len(rows_b)`: при пустом addedItems счётчик подставлял размер
                # ОТПРАВЛЕННОГО батча → отчёт рисовал via_grid=1396 при 0 реально залитых фраз
                # (прогон 2026-07-17, три аккаунта; провал выглядел успехом).
                rep["via_grid"] += n_added
                if n_added == len(rows_b):
                    for key in keys_b:            # полный успех → фиксируем идемпотентность
                        done_kw.add(key)
                else:
                    # Неполный батч: какие именно фразы приняты — Grid не сообщает (addedItems без
                    # текста фразы). Не помечаем done (иначе повторный прогон пропустит их как
                    # already_done и недолив станет вечным) и отдаём батч в v5-фолбэк; уже
                    # залетевшие отсеются там как дубли.
                    grid_failed_rows += rows_b
                    grid_failed_keys += keys_b
                    rep["grid_short_batches"] = rep.get("grid_short_batches", 0) + 1
                    rep["errors"].append(
                        f"grid kw batch неполный: принято {n_added} из {len(rows_b)} → v5-фолбэк")
            except Exception as e:  # noqa: BLE001
                grid_failed_rows += rows_b
                grid_failed_keys += keys_b
                rep["grid_failed_batches"] += 1
                rep["errors"].append(f"grid kw batch: {str(e)[:180]}")
        _wj(done_path, sorted(done_kw))
    elif grid_rows:
        grid_failed_rows = list(grid_rows)        # нет grid-клиента → всё в v5-фолбэк
        grid_failed_keys = list(grid_keys)

    # 2) V5-ФОЛБЭК: UserParam-фразы + TEXT_AD_GROUP (прямой маршрут) + не прошедшие Grid.
    rep["v5_userparam"] = len(v5_rows)
    rep["v5_text_adgroup"] = len(v5_text_rows)  # TEXT_AD_GROUP фразы — Grid для них лжёт
    v5_all_rows = list(v5_rows) + list(v5_text_rows)
    v5_all_keys = list(v5_keys) + list(v5_text_keys)
    for row, key in zip(grid_failed_rows, grid_failed_keys):
        item = {"AdGroupId": int(row["adgroup_id"]), "Keyword": row["keyword"]}
        if row.get("_bid") is not None:
            item["Bid"] = int(row["_bid"])
        if row.get("_context_bid") is not None:
            item["ContextBid"] = int(row["_context_bid"])
        v5_all_rows.append(item)
        v5_all_keys.append(key)

    if v5_all_rows:
        token = ctx.target_token
        if not token or not ctx.v5_call:
            rep["failed"] += len(v5_all_rows)
            rep["errors"].append(
                f"нет v5-токена/вызова — {len(v5_all_rows)} ключей не добавлены "
                f"(UserParam {len(v5_rows)}, Grid-fail {len(grid_failed_rows)})")
        else:
            for rows_b, keys_b in zip(_chunks(v5_all_rows, v5_batch), _chunks(v5_all_keys, v5_batch)):
                try:
                    j = ctx.v5_call("keywords", "add", token, ctx.target_login, {"Keywords": rows_b})
                    err = _v5_add_err(j)
                    if err:
                        rep["failed"] += len(rows_b)
                        rep["errors"].append(f"v5 keywords.add: {err[:180]}")
                        continue
                    add_results = ((j.get("result") or {}).get("AddResults") or [])
                    for key, ar in zip(keys_b, add_results):
                        item_id = ar.get("Id") if isinstance(ar, dict) else None
                        if item_id:
                            done_kw.add(key)
                            rep["via_v5"] += 1
                        else:
                            rep["failed"] += 1
                            item_errs = (ar.get("Errors") or []) if isinstance(ar, dict) else []
                            if item_errs:
                                ctx.log(f"v5 keywords.add per-item err: key={key!r} "
                                        f"err={str(item_errs[0])[:120]}")
                    # если AddResults короче батча (неожиданный API) — остаток помечаем как failed
                    if len(add_results) < len(keys_b):
                        tail = keys_b[len(add_results):]
                        rep["failed"] += len(tail)
                        ctx.log(f"v5 keywords.add: AddResults ({len(add_results)}) < batch ({len(keys_b)}) — {len(tail)} ключей не помечены done")
                except Exception as e:  # noqa: BLE001
                    rep["failed"] += len(rows_b)
                    rep["errors"].append(f"v5 keywords.add: {str(e)[:180]}")
            _wj(done_path, sorted(done_kw))

    rep["geo_replaced_phrases"] = _kw_geo_count
    ctx.log(f"ключи Grid-first (0 баллов): Grid {rep['via_grid']}, v5-фолбэк {rep['via_v5']} "
            f"(UserParam {rep['v5_userparam']}, Grid-fail батчей {rep['grid_failed_batches']}), "
            f"не добавлено {rep['failed']}, уже были {rep['already_done']}, "
            f"без группы {rep['skipped_no_group']}"
            + (f", гео-замена в фразах {_kw_geo_count}" if _kw_geo_count else ""))
    return rep

"""Repair facade and repair helpers for copy verification."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import campaign as cmc
from . import grid_finalize as gf
from . import grid_read as gr
from .copy_verify_utils import (
    _OK, _MISMATCH, _MISSING, _UNREADABLE, _EXCLUDED,
    _nolog, _rj, _rj_dict, _strip_domain,
)
from . import copy_verify_state as _state


def _repair_shared_sets(
    results: List[dict],
    *,
    src_dir: Path,
    workdir: Path,
    target_login: str,
    target_agency: str,
    repairs: list,
    errors: list,
    log: Callable[[str], None],
) -> None:
    """D3: создать/найти shared минус-наборы в целевом аккаунте и привязать к кампаниям.

    Use-Operator-Units: v5 с токеном агентства (не Grid-куки).
    Идемпотентно: maps["shared_sets"] уже содержит mapping → только привязка.
    """
    if _state._v5_call is None or _state._token_for_login is None or _state._direct_tokens is None:
        errors.append("repair_shared_sets: DI не инициализирован (configure() не вызван)")
        return

    # Читаем кампании источника
    src_camps_by_id: Dict[str, dict] = {}
    try:
        camp_path = src_dir / "campaigns.json"
        if camp_path.exists():
            for c in json.loads(camp_path.read_text(encoding="utf-8")):
                cid = str(c.get("Id") or "")
                if cid:
                    src_camps_by_id[cid] = c
    except Exception as exc:  # noqa: BLE001
        errors.append(f"repair_shared_sets: campaigns.json: {str(exc)[:150]}")
        return

    # Snapshot shared sets (Name + NegativeKeywords)
    src_sets_by_id: Dict[str, dict] = {}
    try:
        sset_path = src_dir / "negative_keyword_shared_sets.json"
        if sset_path.exists():
            for s in json.loads(sset_path.read_text(encoding="utf-8")):
                sid = str(s.get("Id") or "")
                if sid:
                    src_sets_by_id[sid] = s
    except Exception:  # noqa: BLE001
        pass  # snapshot может не содержать sets — деградируем к пустым словам

    # id_maps.json
    maps_path = workdir / "id_maps.json"
    maps: dict = {}
    try:
        if maps_path.exists():
            maps = json.loads(maps_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"repair_shared_sets: id_maps.json: {str(exc)[:150]}")
        return
    maps.setdefault("shared_sets", {})
    maps.setdefault("campaigns", {})

    # Токен для целевого аккаунта
    try:
        token, _ = _state._token_for_login(target_login, target_agency, _state._direct_tokens())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"repair_shared_sets: token: {str(exc)[:150]}")
        return
    if not token:
        errors.append("repair_shared_sets: токен пуст, пропуск")
        return

    # Существующие shared sets в целевом (кэш по имени для dedup)
    existing_by_name: Dict[str, int] = {}
    try:
        jg = _state._v5_call("negativekeywordsharedsets", "get", token, target_login, {
            "SelectionCriteria": {}, "FieldNames": ["Id", "Name"],
        })
        for s in (jg.get("result") or {}).get("NegativeKeywordSharedSets", []):
            nm = s.get("Name") or ""
            sid_raw = s.get("Id")
            if nm and sid_raw:
                existing_by_name[nm] = int(sid_raw)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"repair_shared_sets: v5 get: {str(exc)[:150]}")
        return

    maps_dirty = False
    for row in results:
        if row.get("dimension") != "shared_set_count":
            continue
        if not row.get("repairable"):
            continue
        if row.get("status") in (_OK, _EXCLUDED):
            continue
        scope = row.get("scope", "")
        if not scope.startswith("campaign:"):
            continue
        tail = scope[len("campaign:"):]
        if "→" not in tail:
            continue
        src_id, tgt_id_str = tail.split("→", 1)

        src_camp = src_camps_by_id.get(src_id) or {}
        raw_ids = src_camp.get("NegativeKeywordSharedSetIds") or {}
        if isinstance(raw_ids, dict):
            raw_ids = raw_ids.get("Items") or []
        if not raw_ids:
            continue  # источник без shared sets — ничего не привязываем

        tgt_set_ids: List[int] = []
        for sid_str in [str(x) for x in raw_ids if str(x).strip()]:
            # Уже смапирован в предыдущих проходах/phase_upload
            if sid_str in maps["shared_sets"]:
                tgt_set_ids.append(int(maps["shared_sets"][sid_str]))
                continue
            # Ищем / создаём в целевом
            src_s = src_sets_by_id.get(sid_str) or {}
            set_name = src_s.get("Name") or f"copy_set_{sid_str}"
            if set_name in existing_by_name:
                tgt_sid = existing_by_name[set_name]
            else:
                words = src_s.get("NegativeKeywords") or []
                if isinstance(words, dict):
                    words = words.get("Items") or []
                try:
                    j_add = _state._v5_call("negativekeywordsharedsets", "add", token, target_login, {
                        "NegativeKeywordSharedSets": [{
                            "Name": set_name,
                            "NegativeKeywords": words,
                        }],
                    })
                    add_results = (j_add.get("result") or {}).get("AddResults", [])
                    new_id_raw = add_results[0].get("Id") if add_results else None
                    if not new_id_raw:
                        errors.append(
                            f"repair_shared_sets: add «{set_name}» нет Id: "
                            f"{str(j_add)[:120]}"
                        )
                        continue
                    tgt_sid = int(new_id_raw)
                    existing_by_name[set_name] = tgt_sid
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"repair_shared_sets: add «{set_name}»: {str(exc)[:150]}")
                    continue
            maps["shared_sets"][sid_str] = tgt_sid
            maps_dirty = True
            tgt_set_ids.append(tgt_sid)

        if not tgt_set_ids:
            continue

        try:
            tgt_id = int(tgt_id_str)
        except (TypeError, ValueError):
            errors.append(f"repair_shared_sets: bad tgt_id «{tgt_id_str}»")
            continue

        # Привязываем наборы к целевой кампании
        try:
            j_upd = _state._v5_call("campaigns", "update", token, target_login, {
                "Campaigns": [{
                    "Id": tgt_id,
                    "NegativeKeywordSharedSetIds": {"Items": tgt_set_ids},
                }],
            })
            upd_res = (j_upd.get("result") or {}).get("UpdateResults", [])
            api_errs = (upd_res[0].get("Errors") or []) if upd_res else []
            if api_errs:
                msg = "; ".join(
                    e.get("Message") or e.get("Details") or str(e) for e in api_errs
                )
                errors.append(f"repair_shared_sets: attach camp {tgt_id}: {msg}")
                continue
            if "error" in j_upd:
                errors.append(
                    f"repair_shared_sets: attach camp {tgt_id}: {j_upd.get('error')}"
                )
                continue
            # Успех — обновляем строку отчёта in-place
            row["status"] = _OK
            row["target"] = len(tgt_set_ids)
            repairs.append({
                "scope": scope,
                "dimension": "shared_set_count",
                "action": "created_and_attached",
                "target_set_ids": tgt_set_ids,
            })
            log(f"repair_shared_sets: {scope} → привязано {len(tgt_set_ids)} наборов")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"repair_shared_sets: update camp {tgt_id}: {str(exc)[:150]}")

    if maps_dirty:
        try:
            maps_path.write_text(
                json.dumps(maps, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"repair_shared_sets: maps write: {str(exc)[:120]}")


def _repair_shopping_filters(
    results: List[dict],
    *,
    src_dir: Path,
    workdir: Path,
    grid: gf.GridClient,
    repairs: list,
    errors: list,
    log: Callable[[str], None],
) -> None:
    """D19: до-создать ShoppingAd для кампаний с неполным shopping_filter_count.

    Grid-путь без баллов (gf.GridClient.add_shopping_ads).
    Идемпотентно: объявления уже в maps["ads"] → пропуск.
    """
    # Собираем src_id кампаний, требующих ремонта
    affected: Dict[str, str] = {}  # str(src_id) → str(tgt_id)
    for row in results:
        if row.get("dimension") != "shopping_filter_count":
            continue
        if not row.get("repairable"):
            continue
        if row.get("status") in (_OK, _EXCLUDED):
            continue
        scope = row.get("scope", "")
        if scope.startswith("campaign:"):
            tail = scope[len("campaign:"):]
            if "→" in tail:
                s_id, t_id = tail.split("→", 1)
                affected[s_id] = t_id
    if not affected:
        return

    # Читаем shopping_ads.json
    shopping_ads: list = []
    try:
        sa_path = src_dir / "shopping_ads.json"
        if sa_path.exists():
            shopping_ads = json.loads(sa_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"repair_shopping: shopping_ads.json: {str(exc)[:150]}")
        return

    # Читаем id_maps
    maps_path = workdir / "id_maps.json"
    maps: dict = {}
    try:
        if maps_path.exists():
            maps = json.loads(maps_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"repair_shopping: id_maps.json: {str(exc)[:150]}")
        return
    maps.setdefault("ads", {})
    maps.setdefault("adgroups", {})
    maps.setdefault("feeds", {})

    shop_items: list = []
    shop_src_ids: list = []
    shop_camp_src_ids: list = []
    for sa in shopping_ads:
        src_camp_id = str(sa.get("CampaignId") or "")
        if src_camp_id not in affected:
            continue
        src_ad_id = str(sa.get("Id") or "")
        if src_ad_id and src_ad_id in maps["ads"]:
            continue  # идемпотентно
        gid = maps["adgroups"].get(str(sa.get("AdGroupId") or ""))
        sad = sa.get("ShoppingAd") or {}
        fid = maps["feeds"].get(str(sad.get("FeedId") or ""))
        if not gid or not fid:
            log(f"repair_shopping: пропуск ad {src_ad_id} — нет mapped adgroup/feed")
            continue
        item: dict = {"adgroup_id": int(gid), "feed_id": int(fid)}
        raw_conds = sad.get("FeedFilterConditions") or []
        if isinstance(raw_conds, dict):
            raw_conds = raw_conds.get("Items") or []
        for cond in raw_conds:
            if not isinstance(cond, dict):
                continue
            op = str(cond.get("Operand") or "")
            args = cond.get("Arguments") or []
            if op == "collectionId" and args:
                item["collection_id"] = str(args[0])
            elif op == "vendor" and args:
                item["vendor"] = str(args[0])
            elif op == "model" and args:
                item["model"] = [str(x) for x in args]
        shop_items.append(item)
        shop_src_ids.append(src_ad_id)
        shop_camp_src_ids.append(src_camp_id)

    if not shop_items:
        return

    try:
        new_ids = grid.add_shopping_ads(shop_items) or []
        added_by_camp: Dict[str, int] = {}
        for src_ad, camp_id, new_id in zip(shop_src_ids, shop_camp_src_ids, new_ids):
            if new_id:
                maps["ads"][str(src_ad)] = int(new_id)
                added_by_camp[camp_id] = added_by_camp.get(camp_id, 0) + 1

        if added_by_camp:
            maps_path.write_text(
                json.dumps(maps, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            for row in results:
                if row.get("dimension") != "shopping_filter_count":
                    continue
                scope = row.get("scope", "")
                if not scope.startswith("campaign:"):
                    continue
                tail = scope[len("campaign:"):]
                s_id = tail.split("→", 1)[0] if "→" in tail else ""
                if s_id not in added_by_camp:
                    continue
                n_added = added_by_camp[s_id]
                n_src = row.get("source") or 0
                row["target"] = n_added
                row["status"] = _OK if n_added >= n_src else _MISMATCH
                repairs.append({
                    "scope": scope,
                    "dimension": "shopping_filter_count",
                    "action": "add_shopping_ads",
                    "added": n_added,
                    "source": n_src,
                })
                log(f"repair_shopping: {scope} → {n_added}/{n_src} ShoppingAds")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"repair_shopping: add_shopping_ads: {str(exc)[:200]}")


def _repair_product_filter_signatures(
    results: List[dict],
    *,
    src_dir: Path,
    workdir: Path,
    grid: gf.GridClient,
    repairs: list,
    errors: list,
    log: Callable[[str], None],
) -> None:
    """D19-sig: проставить feedFilter товарным/каталожным, у которых сигнатура ≠ источнику.

    Отдельно от `shopping_filter_count` (там чинится ЧИСЛО объявлений, здесь — их ФИЛЬТРЫ).
    Дыра, из-за которой это понадобилось: v501 ads.add принимает FeedFilterConditions, но на ЕПК
    их не применяет — verify видел `*_filter_signature: mismatch`, а writer'а под эту размерность
    не было, и job закрывался как done с пустыми фильтрами (porg-ln7tz7xh, 2026-07-27).

    Writer — тот же, что в постпроцессе: Grid updateShoppingAds/updateListingAds, 0 баллов,
    идемпотентно (повторная запись того же фильтра безвредна)."""
    dims = ("shopping_filter_signature", "listing_filter_signature")
    broken = [
        r for r in results
        if r.get("dimension") in dims
        and r.get("repairable")
        and r.get("status") not in (_OK, _EXCLUDED)
    ]
    if not broken:
        return

    maps_path = Path(workdir) / "id_maps.json"
    if not maps_path.exists():
        errors.append("repair_filters: id_maps.json не найден")
        return
    try:
        maps = json.loads(maps_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"repair_filters: id_maps.json нечитаем: {str(exc)[:150]}")
        return

    try:
        from .copy_postprocess import _copy_apply_product_filters
        rep = _copy_apply_product_filters(Path(src_dir), maps, grid, log=log)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"repair_filters: {str(exc)[:200]}")
        return
    errors += rep.get("errors") or []
    if not (rep.get("shopping") or rep.get("listing")):
        return

    for row in broken:
        row["status"] = _OK
        repairs.append({
            "scope": row.get("scope", ""),
            "dimension": row.get("dimension"),
            "action": "set_product_feed_filters",
            "shopping": rep.get("shopping", 0),
            "listing": rep.get("listing", 0),
        })
    log(f"repair_filters: фильтры проставлены — {rep.get('shopping', 0)} товарных, "
        f"{rep.get('listing', 0)} каталожных ({len(broken)} строк verify закрыто)")


def _repair_keywords(
    results: List[dict],
    *,
    src_dir: Path,
    workdir: Path,
    target_login: str,
    target_agency: str,
    geo_pairs: Optional[list],
    repairs: list,
    errors: list,
    log: Callable[[str], None],
) -> None:
    """keyword_count: дозалить недостающие ключи по кампании через v5 (operator units).

    Причина существования: наблюдалось, что на части кампаний ключи при аплоаде не оседают,
    хотя v5 keywords.add вернул truthy Id и failed=0 (под-копирование во время создания кампании).
    verify это ловит, но auto_repair раньше НЕ имел ремонтёра keyword_count → repairs=0.
    Здесь сверяем ЖИВОЙ keywords.get по кампании с источником и дозаливаем недостающее.

    Персистентность доказана: одиночный/батчевый v5 add на этих же группах оседает; ограничение
    API — не более 1000 ключей на запрос (код 9300), поэтому батч 900. Гео-морфология фраз —
    та же (geo_pairs), что применял step_keywords, иначе дубли (морфнутая≠исходная) раздуют цель."""
    if _state._v5_call is None or _state._token_for_login is None or _state._direct_tokens is None:
        errors.append("repair_keywords: DI не инициализирован (configure() не вызван)")
        return
    rows_kw = [
        r for r in results
        if r.get("dimension") == "keyword_count" and r.get("repairable")
        and r.get("status") not in (_OK, _EXCLUDED)
    ]
    if not rows_kw:
        return
    try:
        keywords = json.loads((src_dir / "keywords.json").read_text("utf-8")) \
            if (src_dir / "keywords.json").exists() else []
        adgroups = json.loads((src_dir / "adgroups.json").read_text("utf-8")) \
            if (src_dir / "adgroups.json").exists() else []
        maps = json.loads((workdir / "id_maps.json").read_text("utf-8")) \
            if (workdir / "id_maps.json").exists() else {}
    except Exception as exc:  # noqa: BLE001
        errors.append(f"repair_keywords: чтение снапшота: {str(exc)[:150]}")
        return
    adg_map = {str(k): str(v) for k, v in (maps.get("adgroups") or {}).items()}
    g2c = {str(g.get("Id")): str(g.get("CampaignId")) for g in adgroups}
    cgm = None
    if geo_pairs:
        try:
            from . import copy_geo_morph as cgm  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            cgm = None
    try:
        token, _ = _state._token_for_login(target_login, target_agency, _state._direct_tokens())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"repair_keywords: token: {str(exc)[:150]}")
        return
    if not token:
        errors.append("repair_keywords: токен пуст, пропуск")
        return

    def _chunks(seq, n):
        for i in range(0, len(seq), n):
            yield seq[i:i + n]

    def _live_kw(tgt_cid: int) -> Dict[int, set]:
        """{tgt_gid: {phrase_lower}} — живые ключи цели по кампании (постранично)."""
        out: Dict[int, set] = {}
        offset = 0
        while True:
            j = _state._v5_call("keywords", "get", token, target_login, {
                "SelectionCriteria": {"CampaignIds": [tgt_cid]},
                "FieldNames": ["Id", "AdGroupId", "Keyword"],
                "Page": {"Limit": 10000, "Offset": offset},
            })
            batch = (j.get("result") or {}).get("Keywords") or []
            for kw in batch:
                out.setdefault(int(kw.get("AdGroupId") or 0), set()).add(
                    str(kw.get("Keyword") or "").strip().lower())
            if len(batch) < 10000:
                break
            offset += 10000
        return out

    for row in rows_kw:
        scope = row.get("scope", "")
        if not scope.startswith("campaign:") or "→" not in scope:
            continue
        src_id, tgt_id_str = scope[len("campaign:"):].split("→", 1)
        if not tgt_id_str.strip().isdigit():
            continue
        tgt_cid = int(tgt_id_str)
        # желаемые фразы по target-группам (гео-морф как в step_keywords, без autotargeting)
        desired: Dict[int, set] = {}
        for k in keywords:
            if g2c.get(str(k.get("AdGroupId"))) != src_id:
                continue
            phrase = str(k.get("Keyword") or "").strip()
            if not phrase or phrase.startswith("---"):
                continue
            tgt_g = adg_map.get(str(k.get("AdGroupId")))
            if not tgt_g or not str(tgt_g).isdigit():
                continue
            if cgm and geo_pairs:
                p2, _n = cgm.apply_replacements(phrase, geo_pairs)
                phrase = (p2 or "").strip() or phrase
            desired.setdefault(int(tgt_g), set()).add(phrase)
        if not desired:
            continue
        try:
            live = _live_kw(tgt_cid)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"repair_keywords {tgt_cid}: live get: {str(exc)[:150]}")
            continue
        to_add = [
            {"AdGroupId": gid, "Keyword": ph}
            for gid, phrases in desired.items()
            for ph in phrases
            if ph.strip().lower() not in live.get(gid, set())
        ]
        if not to_add:
            continue
        added = 0
        for batch in _chunks(to_add, 900):
            try:
                j = _state._v5_call("keywords", "add", token, target_login, {"Keywords": batch})
            except Exception as exc:  # noqa: BLE001
                errors.append(f"repair_keywords {tgt_cid}: add: {str(exc)[:150]}")
                continue
            err = (j.get("error") or {}).get("error_string") if isinstance(j.get("error"), dict) else None
            if err:
                errors.append(f"repair_keywords {tgt_cid}: v5 {err[:120]}")
                continue
            for ar in ((j.get("result") or {}).get("AddResults") or []):
                if isinstance(ar, dict) and ar.get("Id"):
                    added += 1
        if added:
            repairs.append({"scope": scope, "dimension": "keyword_count",
                            "action": "add_keywords", "added": added})
            try:
                row["target"] = int(row.get("target") or 0) + added
                if str(row.get("target")) == str(row.get("source")):
                    row["status"] = _OK
            except (TypeError, ValueError):
                pass
            log(f"repair keywords {tgt_cid}: дозалито {added} (недоставало {len(to_add)})")


def run_copy_repair(
    report: dict,
    *,
    src_dir: Any,
    workdir: Any,
    target_login: str,
    target_agency: str,
    grid: Optional[gf.GridClient] = None,
    geo_pairs: Optional[list] = None,
    log: Optional[Callable[[str], None]] = None,
) -> dict:
    """Авто-ремонт repairable=True измерений из результата run_copy_verification.

    ГАРАНТИИ (жёсткие):
    - Только ADD/SET недостающего, никогда не удаляет entity цели.
    - Идемпотентно: уже созданное/привязанное → пропуск.
    - Ошибки → rep["errors"], исключение НЕ поднимает.
    - После ремонта обновляет строки report["results"] in-place (status / target).
    - Use-Operator-Units: D3 — v5 с токеном; D19 — Grid без баллов.

    Ремонтируемые (repairable=True в verify):
        D3  shared_set_count — создать/найти shared минус-наборы + привязать (v5).
        D19 shopping_filter_count — до-создать ShoppingAd через Grid.
        D19-sig shopping_filter_signature / listing_filter_signature — проставить feedFilter
            источника на созданные Shopping/Listing через Grid.

    НЕ ремонтируем (repairable=False в verify):
        D10 audiences — сверяется через Grid, но авто-writer repair нет;
        D11 bid_modifiers — намеренно наш стандарт; D14 button_cta — нет отдельного repair.

    Returns:
        {"repairs": [{scope, dimension, action, ...}], "errors": [str, ...]}
    """
    _log = log or _nolog
    repairs: List[dict] = []
    errors: List[str] = []
    src_dir_p = Path(src_dir)
    workdir_p = Path(workdir)

    results = report.get("results") or []
    has_repairable = any(
        r.get("repairable") and r.get("status") not in (_OK, _EXCLUDED)
        for r in results
    )
    if not has_repairable:
        _log("copy_repair: нечего чинить (нет repairable=True строк с неOK/неEXCLUDED статусом)")
        return {"repairs": repairs, "errors": errors}

    # D3: shared negative keyword sets (v5 с токеном — operator units)
    _repair_shared_sets(
        results,
        src_dir=src_dir_p,
        workdir=workdir_p,
        target_login=target_login,
        target_agency=target_agency,
        repairs=repairs,
        errors=errors,
        log=_log,
    )

    # keyword_count: дозалить недостающие ключи по кампании (v5, operator units).
    _repair_keywords(
        results,
        src_dir=src_dir_p,
        workdir=workdir_p,
        target_login=target_login,
        target_agency=target_agency,
        geo_pairs=geo_pairs,
        repairs=repairs,
        errors=errors,
        log=_log,
    )

    # D19: shopping filter count (Grid без баллов)
    if grid is not None:
        _repair_shopping_filters(
            results,
            src_dir=src_dir_p,
            workdir=workdir_p,
            grid=grid,
            repairs=repairs,
            errors=errors,
            log=_log,
        )
        # D19-sig: фильтры товарных/каталожных (Grid без баллов)
        _repair_product_filter_signatures(
            results,
            src_dir=src_dir_p,
            workdir=workdir_p,
            grid=grid,
            repairs=repairs,
            errors=errors,
            log=_log,
        )
    else:
        _log("copy_repair: D19 пропущен — grid=None (GridClient цели не передан)")

    _log(
        f"copy_repair: итог — repairs={len(repairs)}, errors={len(errors)}"
    )
    return {"repairs": repairs, "errors": errors}

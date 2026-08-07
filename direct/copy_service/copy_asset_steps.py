"""Asset/settings шаги copy-постпроцесса: callouts, sitelinks, promos, bidmods, minus-places."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Callable, Optional

from .copy_context import CopyCtx, _noop_log
from .copy_step_utils import (
    _campaign_pairs,
    _chunks,
    _invalidate_target_edit_rows,
    _rj,
    _source_edit_rows,
    _v5_add_err,
    _wj,
)


_CAMPAIGN_STRUCT_KEYS = (
    "TextCampaign", "DynamicTextCampaign", "SmartCampaign",
    "CpmBannerCampaign", "UnifiedAdCampaign",
)

def source_has_network(campaign: dict) -> bool:
    """True, если у исходной кампании включена сеть (РСЯ): блок Network стратегии не SERVING_OFF.
    Работает по snapshot campaigns.json (v5). Для tp1/tp3/сетевых вернёт True, для поиск-only — False."""
    if not isinstance(campaign, dict):
        return False
    for key in _CAMPAIGN_STRUCT_KEYS:
        block = campaign.get(key) or {}
        strat = block.get("BiddingStrategy") or {}
        net = strat.get("Network") or {}
        btype = str(net.get("BiddingStrategyType") or "").strip().upper()
        if btype and btype != "SERVING_OFF":
            return True
    return False


def pull_source_campaign_assets(source_grid, source_campaign_ids, src_dir: Path,
                                *, log: Callable[[str], None] = _noop_log) -> dict:
    """Зафиксировать ИСХОДНУЮ связь campaign→[callout_ids] и campaign→promoExtensionId.

    Пишет src_dir/campaign_callouts.json и src_dir/campaign_promos.json (ключ — str(source_campaign_id)).
    Источник данных — Grid источника (inheritableCallouts/promoExtension через CampaignsEditData).
    Фолбэк callouts: если Grid недоступен — деривация из ads.json (ad-level AdExtensions CALLOUT,
    сгруппированные по CampaignId). Promo без Grid снять неоткуда → пустой файл (постпроцесс
    откатится на прежнее единичное поведение)."""
    rep = {"callouts_campaigns": 0, "promos_campaigns": 0, "source": "", "errors": []}
    src_dir = Path(src_dir)
    ids = []
    for raw in source_campaign_ids or []:
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            continue
        if cid > 0 and cid not in ids:
            ids.append(cid)

    callouts_map: dict[str, list[str]] = {}
    promos_map: dict[str, str] = {}
    sitelinks_map: dict[str, str] = {}   # str(campaign_id) → sitelinkSetId (баг 2a — быстрые ссылки)
    grid_ok = False
    if source_grid is not None and ids:
        try:
            payloads = source_grid._read_unified_campaign_update_payloads(ids)
            for cid, p in (payloads or {}).items():
                co_ids = [str(x) for x in ((p.get("inheritableCallouts") or {}).get("calloutIds") or []) if str(x).strip()]
                if co_ids:
                    callouts_map[str(cid)] = co_ids
                promo_id = p.get("promoExtensionId")
                if promo_id and str(promo_id).strip() and str(promo_id) != "0":
                    promos_map[str(cid)] = str(promo_id)
                sl_id = (p.get("inheritableSitelinkSet") or {}).get("sitelinkSetId")
                if sl_id and str(sl_id).strip() and str(sl_id) != "0":
                    sitelinks_map[str(cid)] = str(sl_id)
            grid_ok = True
            rep["source"] = "grid"
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"grid read: {str(e)[:200]}")

    if not grid_ok:
        # Фолбэк: ad-level callouts из ads.json, сгруппированные по CampaignId.
        rep["source"] = "ads_fallback"
        try:
            wanted = set(str(c) for c in ids)
            for a in _rj(src_dir / "ads.json"):
                camp = str(a.get("CampaignId") or "")
                if wanted and camp not in wanted:
                    continue
                exts = []
                for k in ("TextAd", "DynamicTextAd"):
                    exts += (a.get(k) or {}).get("AdExtensions") or []
                co = [str(e.get("AdExtensionId")) for e in exts
                      if isinstance(e, dict) and e.get("Type") == "CALLOUT" and e.get("AdExtensionId")]
                if co:
                    bucket = callouts_map.setdefault(camp, [])
                    for x in co:
                        if x not in bucket:
                            bucket.append(x)
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"ads fallback: {str(e)[:200]}")

    try:
        _wj(src_dir / "campaign_callouts.json", callouts_map)
        _wj(src_dir / "campaign_promos.json", promos_map)
        _wj(src_dir / "campaign_sitelinks.json", sitelinks_map)
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"write: {str(e)[:200]}")
    rep["callouts_campaigns"] = len(callouts_map)
    rep["promos_campaigns"] = len(promos_map)
    rep["sitelinks_campaigns"] = len(sitelinks_map)
    log(f"pull source assets ({rep['source']}): callouts у {rep['callouts_campaigns']} кампаний, "
        f"promo у {rep['promos_campaigns']} кампаний, "
        f"быстрые ссылки у {rep['sitelinks_campaigns']} кампаний")
    return rep


_AGE_TARGETS = ("AGE_0_17", "AGE_18_24")  # <18 и 18–24 («младше 25»); BidModifier=0 == −100% (v5)


_AGE_TARGETS_GRID = {"_0_17": -100, "_18_24": -100}


def step_age_bidmods(ctx: CopyCtx) -> dict:
    """П.14. Проставить −100% на «младше 18» и «младше 25» на каждую скопированную кампанию.

    ФАЗА 3b (ретро-правка): теперь GRID-FIRST — ``grid.set_campaign_age_bidmods`` через RMW
    UpdateCampaigns, БЕЗ v5-баллов. Grid percent — МУЛЬТИПЛИКАТОР 0..1300 (min=0), не знаковая
    дельта: −100% == percent:0. Здесь передаём конвенцию «дельта» (−100), а delta→multiplier
    (100+pct, clamp 0..1300) конвертирует Grid-слой в set_campaign_age_bidmods.

    v5 bidmodifiers.add остаётся ТОЛЬКО как фолбэк для кампаний, которые Grid не покрыл
    (копейки баллов — 2 корректировки; документировано). Идемпотентно на обоих путях:
    Grid пропускает уже-выставленные возрасты, v5 читает bidmodifiers.get перед add."""
    rep = {"campaigns": 0, "added": 0, "skipped_existing": 0, "via": "",
           "grid_ok": 0, "v5_fallback": 0, "errors": []}
    camp_ids = sorted({int(x) for x in (ctx.maps.get("campaigns") or {}).values() if str(x).isdigit()})
    rep["campaigns"] = len(camp_ids)
    if not camp_ids:
        return rep

    # 1) Grid-first (НОЛЬ v5-баллов). satisfied = кампании, где возраст −100% гарантированно стоит.
    grid_done: set[int] = set()
    if ctx.grid is not None and hasattr(ctx.grid, "set_campaign_age_bidmods"):
        try:
            satisfied = ctx.grid.set_campaign_age_bidmods(camp_ids, dict(_AGE_TARGETS_GRID))
            grid_done = {int(x) for x in (satisfied or []) if str(x).isdigit()}
            rep["grid_ok"] = len(grid_done)
            rep["via"] = "grid"
            ctx.log(f"возрастные −100% через Grid (0 баллов): {rep['grid_ok']}/{len(camp_ids)} кампаний")
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"grid age bidmods: {str(e)[:200]}")

    # 2) v5-фолбэк ТОЛЬКО для кампаний, которые Grid не покрыл (копейки баллов).
    remaining = [c for c in camp_ids if c not in grid_done]
    if remaining:
        token = ctx.target_token
        if not token or not ctx.v5_call:
            if rep["grid_ok"]:
                ctx.log(f"возрастные −100%: {len(remaining)} кампаний вне Grid, v5-токена нет — "
                        f"добить нечем (некритично, корректировка косметическая)")
            else:
                rep["errors"].append("нет v5-токена/вызова и Grid не покрыл — возрастные пропущены")
        else:
            for cid in remaining:
                try:
                    existing = _existing_demographic_ages(ctx, token, cid)
                    missing = [a for a in _AGE_TARGETS if a not in existing]
                    if not missing:
                        rep["skipped_existing"] += 1
                        continue
                    item = {"CampaignId": cid,
                            "DemographicsAdjustments": [{"Age": a, "BidModifier": 0} for a in missing]}
                    j = ctx.v5_call("bidmodifiers", "add", token, ctx.target_login, {"BidModifiers": [item]})
                    err = _v5_add_err(j)
                    if err:
                        rep["errors"].append(f"camp {cid} v5: {err[:200]}")
                        continue
                    rep["added"] += len(missing)
                    rep["v5_fallback"] += 1
                except Exception as e:  # noqa: BLE001
                    rep["errors"].append(f"camp {cid} v5: {str(e)[:200]}")
            if rep["v5_fallback"]:
                rep["via"] = "grid+v5" if rep["grid_ok"] else "v5"
                ctx.log(f"возрастные −100%: Grid не покрыл {len(remaining)} → v5-фолбэк добил "
                        f"{rep['v5_fallback']} (копейки баллов; уже были {rep['skipped_existing']})")
    return rep


def _existing_demographic_ages(ctx: CopyCtx, token: str, cid: int) -> set[str]:
    """Возрасты, у которых уже есть DemographicsAdjustment на кампании (чтобы не дублировать)."""
    ages: set[str] = set()
    j = ctx.v5_call("bidmodifiers", "get", token, ctx.target_login, {
        "SelectionCriteria": {"CampaignIds": [int(cid)], "Levels": ["CAMPAIGN"]},
        "FieldNames": ["Id", "CampaignId", "Type"],
        "DemographicsAdjustmentFieldNames": ["Age", "Gender", "BidModifier"],
    })
    for bm in ((j.get("result") or {}).get("BidModifiers") or []):
        da = bm.get("DemographicsAdjustment") or {}
        age = str(da.get("Age") or "").strip()
        # Учитываем только «чистые» возрастные (без пола) — наши стандартные тоже без Gender.
        if age and not da.get("Gender"):
            ages.add(age)
    return ages


def step_disabled_places(ctx: CopyCtx) -> dict:
    """П.13. Скопировать disabledPlaces источника 1в1 в target через Grid.

    Это не baseline-стандарт и не добавление площадок поверх копии: для каждой пары
    campaign source→target ставим ровно тот список, который видит редактор Директа у источника.
    Если source Grid недоступен — честный skip, без подмены стандартным списком."""
    rep = {"campaigns": 0, "updated": 0, "hosts": 0, "skipped": [], "errors": []}
    if ctx.grid is None:
        rep["errors"].append("нет grid-клиента — disabledPlaces пропущены")
        return rep
    if ctx.source_grid is None:
        rep["errors"].append("нет source grid-клиента — disabledPlaces 1в1 не прочитаны")
        return rep

    pairs = _campaign_pairs(ctx)
    if not pairs:
        return rep
    try:
        src_rows = _source_edit_rows(ctx, [s for s, _ in pairs])
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"source disabledPlaces read: {str(e)[:200]}")
        return rep
    for src_id, tgt_id in pairs:
        row = src_rows.get(src_id) or {}
        if not row:
            rep["skipped"].append({"campaign": src_id, "reason": "нет source edit row"})
            continue
        hosts = list(dict.fromkeys(str(h).strip() for h in (row.get("disabledPlaces") or []) if str(h).strip()))
        rep["campaigns"] += 1
        rep["hosts"] += len(hosts)
        try:
            updated = ctx.grid.set_campaign_disabled_places([tgt_id], hosts)
            _invalidate_target_edit_rows(ctx)
            rep["updated"] += len(updated or [tgt_id])
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"campaign {src_id}→{tgt_id}: {str(e)[:180]}")
    ctx.log(f"disabledPlaces 1в1: обновлено {rep['updated']}/{rep['campaigns']} кампаний, "
            f"площадок источника {rep['hosts']}")
    return rep


# FIX C (2026-08-05): единственное место, где живёт литерал 8 — DI-путь (copy_postprocess.py:1081
# передаёт per_campaign_cap=copy_engine._CALLOUT_PER_CAMPAIGN_CAP, сконфигурированный из
# core/automation_runtime.py:2177) переопределяет этот дефолт значением из единого источника; этот
# дефолт используется только когда шаг вызван БЕЗ DI (тесты/ручной вызов). copy_verify_source.py
# импортирует именно эту константу — не второй захардкоженный литерал.
_CALLOUT_PER_CAMPAIGN_CAP_DEFAULT = 8


def step_attach_callouts(ctx: CopyCtx, per_campaign_cap: int = _CALLOUT_PER_CAMPAIGN_CAP_DEFAULT) -> dict:
    """П.11. Привязать к КАЖДОЙ целевой кампании только ремапленные callout-id ЕЁ исходной
    кампании (по campaign_callouts.json + maps['callouts']), а не общий union всех уточнений.

    Фолбэк: если source-связь недоступна для кампании (нет файла / нет записи / пусто после
    ремапа) — вешаем union всех созданных callout-id (прежнее поведение), с логом."""
    rep = {"per_campaign": 0, "fallback_union": 0, "attached_campaigns": 0,
           "skipped_read_lag": 0, "errors": []}
    if ctx.grid is None:
        rep["errors"].append("нет grid-клиента — уточнения не привязаны")
        return rep
    callout_map = ctx.maps.get("callouts") or {}          # src_callout_id → tgt_callout_id
    camp_map = ctx.maps.get("campaigns") or {}            # src_campaign_id → tgt_campaign_id
    union_ids = list(dict.fromkeys(int(x) for x in callout_map.values() if str(x).isdigit()))
    if not camp_map:
        return rep
    src_links = _rj(ctx.src_dir / "campaign_callouts.json")
    src_links = src_links if isinstance(src_links, dict) else {}
    # Связь campaign→callouts спуллилась (файл непустой) → она полная: кампания, которой в
    # ней НЕТ, у источника реально имеет 0 уточнений — вешать union нельзя (иначе цель получит
    # уточнения, которых у источника нет: verify callout_count src=0 tgt=8). Union — только когда
    # связь вообще недоступна (файл пуст: pull грид-связи не удался) — глобальный фолбэк.
    have_link_data = bool(src_links)

    for src_cid, tgt_cid in camp_map.items():
        if not str(tgt_cid).isdigit():
            continue
        tgt_cid = int(tgt_cid)
        raw_co = src_links.get(str(src_cid)) or []
        remapped = list(dict.fromkeys(
            int(callout_map[str(x)]) for x in raw_co
            if str(x) in callout_map and str(callout_map[str(x)]).isdigit()
        ))
        if remapped:
            use_ids, mode = remapped, "per_campaign"
        elif have_link_data:
            continue                         # связь известна, у кампании 0 уточнений → ничего не вешаем
        else:
            use_ids, mode = union_ids, "fallback_union"
        if not use_ids:
            continue
        try:
            updated = _set_campaign_callouts_copy_safe(
                ctx.grid, tgt_cid, use_ids[:per_campaign_cap], ctx.log)
            if updated:
                rep["attached_campaigns"] += 1
                rep[mode] += 1
            else:
                rep["skipped_read_lag"] += 1
            if mode == "fallback_union":
                ctx.log(f"уточнения camp {tgt_cid}: source-связь недоступна → union ({len(use_ids)})")
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"camp {tgt_cid}: {str(e)[:180]}")
    ctx.log(f"уточнения по кампаниям: по исходной связи {rep['per_campaign']}, "
            f"фолбэк-union {rep['fallback_union']} (всего {rep['attached_campaigns']})")
    return rep


def _set_campaign_callouts_copy_safe(grid, campaign_id: int, callout_ids: list[int], log) -> list:
    """Attach callouts, tolerating short Grid read lag for freshly-created campaigns."""
    delays = (2, 5, 10)
    for attempt in range(len(delays) + 1):
        try:
            return grid.set_campaign_callouts([campaign_id], callout_ids)
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            if "Grid set-callouts: не удалось прочитать кампанию" not in text:
                raise
            if attempt >= len(delays):
                log(f"уточнения camp {campaign_id}: Grid read-lag, пропуск до verify/repair")
                return []
            time.sleep(delays[attempt])


def step_attach_sitelinks(ctx: CopyCtx) -> dict:
    """Перенести наборы быстрых ссылок с источника на target по исходной связи campaign→sitelinkSet.

    Источник связи — campaign_sitelinks.json (pull_source_campaign_assets: str(src_cid)→src_set_id).
    Состав набора читаем source_grid.get_sitelink_sets → title/href/description; title/href прогоняем
    через гео-морфологию (ctx.geo_pairs) и доменную трансформацию (_copy_target_href, тот же путь, что
    у объявлений). Создаём набор на target (grid.add_sitelink_set, 0 баллов) и привязываем к целевой
    кампании (set_campaign_sitelink_set). Дедуп src_set_id→tgt_set_id в maps['sitelinks'] (набор
    создаётся один раз на job). Нет source_grid / файла / связей → безопасный no-op."""
    rep = {"sets_created": 0, "attached_campaigns": 0, "skipped": 0, "errors": []}
    if ctx.grid is None:
        rep["errors"].append("нет target grid-клиента — быстрые ссылки не привязаны")
        return rep
    if ctx.source_grid is None:
        rep["errors"].append("нет source grid-клиента — состав быстрых ссылок не прочитать (пропуск)")
        return rep
    camp_map = ctx.maps.get("campaigns") or {}            # src_campaign_id → tgt_campaign_id
    if not camp_map:
        return rep
    src_links = _rj(ctx.src_dir / "campaign_sitelinks.json")
    src_links = src_links if isinstance(src_links, dict) else {}
    if not src_links:
        return rep

    set_cache: dict[str, int] = {str(k): int(v) for k, v in (ctx.maps.get("sitelinks") or {}).items()
                                 if str(v).isdigit()}
    # Состав нужных наборов читаем ОДНИМ запросом (только те, что ещё не в кэше).
    want_set_ids = [s for s in dict.fromkeys(str(v) for v in src_links.values() if str(v).strip())
                    if s not in set_cache]
    src_sets: dict[int, list[dict]] = {}
    if want_set_ids:
        try:
            src_sets = ctx.source_grid.get_sitelink_sets(want_set_ids) or {}
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"чтение source sitelink-наборов: {str(e)[:200]}")
            return rep

    # Трансформация title/href: гео-морфология + домен (тот же путь, что у объявлений).
    from . import copy_geo_morph as cgm
    try:
        from .copy_engine import _copy_target_href as _href_fn
    except Exception:  # noqa: BLE001 — доменная трансформация опциональна, гео-морф остаётся
        _href_fn = None
    pairs = ctx.geo_pairs or []
    src_domain = str((ctx.body or {}).get("_copy_source_domain") or "").strip()
    target_domain = str((ctx.body or {}).get("target_domain") or "").strip()

    def _morph(text: str) -> str:
        out, _ = cgm.apply_replacements(text, pairs)
        return out

    failed_sets: set[str] = set()   # src_set_id, чей create уже не удался — не пересоздавать на кампанию

    def _tgt_set_id(src_set_id: str) -> int | None:
        if src_set_id in set_cache:
            return set_cache[src_set_id]
        if src_set_id in failed_sets:   # ambiguous/None create уже был — второй раз НЕ дёргаем (дубли наборов)
            return None
        items = src_sets.get(int(src_set_id)) if str(src_set_id).isdigit() else None
        if not items:
            return None
        sitelinks = []
        for it in items:
            title = _morph(str(it.get("title") or "").strip())
            href = str(it.get("href") or "").strip()
            if _href_fn:
                href = _href_fn(href, src_domain, target_domain)
            href = _morph(href)
            desc = _morph(str(it.get("description") or "").strip())
            if title and href:
                sitelinks.append({"title": title, "href": href, "description": desc})
        if not sitelinks:
            return None
        try:
            new_id = ctx.grid.add_sitelink_set(sitelinks)
        except Exception as e:  # noqa: BLE001
            failed_sets.add(src_set_id)
            rep["errors"].append(f"создание sitelink-набора (src {src_set_id}): {str(e)[:180]}")
            return None
        if new_id:
            set_cache[src_set_id] = int(new_id)
            rep["sets_created"] += 1
            return int(new_id)
        failed_sets.add(src_set_id)   # create вернул пустой id — не ретраить на след. кампаниях
        return None

    for src_cid, tgt_cid in camp_map.items():
        if not str(tgt_cid).isdigit():
            continue
        src_set_id = str(src_links.get(str(src_cid)) or "").strip()
        if not src_set_id:
            continue
        tgt_set_id = _tgt_set_id(src_set_id)
        if not tgt_set_id:
            rep["skipped"] += 1
            continue
        try:
            ctx.grid.set_campaign_sitelink_set([int(tgt_cid)], int(tgt_set_id))
            rep["attached_campaigns"] += 1
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"camp {tgt_cid}: {str(e)[:180]}")

    ctx.maps["sitelinks"] = set_cache
    ctx.log(f"быстрые ссылки: наборов создано {rep['sets_created']}, "
            f"привязано к {rep['attached_campaigns']} кампаниям (пропущено {rep['skipped']})")
    return rep


def step_attach_promos(ctx: CopyCtx, created_promo_ids: list[int]) -> dict:
    """П.10. Привязать каждое промо к ЕГО исходным кампаниям через maps['promotions']+maps['campaigns']
    (updateCampaignsPromoExtension, ?ulogin=target уже в PromoClient). Работает и при 2+ промо.

    Фолбэк: если campaign_promos.json пуст/недоступен — прежнее поведение (привязать единственное
    промо ко всем кампаниям, если промо ровно одно)."""
    rep = {"mode": "", "attached_campaigns": 0, "promos_attached": 0, "errors": []}
    pc = ctx.promo_client
    if pc is None:
        rep["errors"].append("нет promo-клиента — привязка промо пропущена")
        return rep
    promo_map = ctx.maps.get("promotions") or {}          # src_promo_id → tgt_promo_id
    camp_map = ctx.maps.get("campaigns") or {}            # src_campaign_id → tgt_campaign_id
    src_links = _rj(ctx.src_dir / "campaign_promos.json")
    src_links = src_links if isinstance(src_links, dict) else {}

    # Собираем tgt_promo_id → [tgt_campaign_id] по исходной связи.
    by_promo: dict[int, list[int]] = {}
    for src_cid, src_promo in src_links.items():
        tgt_cid = camp_map.get(str(src_cid))
        tgt_promo = promo_map.get(str(src_promo))
        if not (tgt_cid and tgt_promo and str(tgt_cid).isdigit() and str(tgt_promo).isdigit()):
            continue
        by_promo.setdefault(int(tgt_promo), []).append(int(tgt_cid))

    if by_promo:
        rep["mode"] = "per_source_link"
        for tgt_promo, cids in by_promo.items():
            cids = list(dict.fromkeys(cids))
            try:
                attach = pc.attach(int(tgt_promo), cids)
                err = _promo_attach_err(attach)
                if err:
                    rep["errors"].append(f"promo {tgt_promo}: {err[:180]}")
                    continue
                rep["promos_attached"] += 1
                rep["attached_campaigns"] += len(cids)
            except Exception as e:  # noqa: BLE001
                rep["errors"].append(f"promo {tgt_promo}: {str(e)[:180]}")
        ctx.log(f"промо по исходной связи: {rep['promos_attached']} промо на "
                f"{rep['attached_campaigns']} привязок")
        return rep

    # Фолбэк: прежнее единичное поведение.
    rep["mode"] = "fallback_single"
    unique_promos = list(dict.fromkeys(int(x) for x in (created_promo_ids or []) if str(x).isdigit()))
    camp_ids = [int(x) for x in camp_map.values() if str(x).isdigit()]
    if len(unique_promos) == 1 and camp_ids:
        try:
            attach = pc.attach(unique_promos[0], camp_ids)
            err = _promo_attach_err(attach)
            if err:
                rep["errors"].append(f"promo attach: {err[:180]}")
            else:
                rep["promos_attached"] = 1
                rep["attached_campaigns"] = len(camp_ids)
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"promo attach: {str(e)[:180]}")
    else:
        ctx.log(f"промо: исходная связь недоступна и промо не единично ({len(unique_promos)}) — "
                f"привязка пропущена (фолбэк)")
    return rep


def _promo_attach_err(attach: dict) -> str:
    if not isinstance(attach, dict):
        return ""
    errors = (((attach.get("data") or {}).get("updateCampaignsPromoExtension") or {})
              .get("validationResult") or {}).get("errors") or attach.get("errors")
    return json.dumps(errors, ensure_ascii=False)[:200] if errors else ""

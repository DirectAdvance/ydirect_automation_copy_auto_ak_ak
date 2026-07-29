"""Сверка и исправление настроек кампаний в copy-постпроцессе."""
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
    _target_edit_rows,
    _v5_add_err,
    _wj,
)


_SEARCH_INVARIANTS_UPDATE_CHUNK = 10
_SEARCH_INVARIANTS_UPDATE_TRIES = 2


def _update_unified_adgroups_resilient(ctx: CopyCtx, cid: int, items: list[dict]) -> list[int]:
    """Update search invariant groups in smaller chunks with one outer retry.

    GridClient.update_unified_adgroups already retries transient transport failures, but a
    whole campaign-sized payload can still time out after those retries. Smaller chunks keep
    the copy postprocess from ending with a single per-campaign timeout warning.
    """
    updated: list[int] = []
    for pos in range(0, len(items), _SEARCH_INVARIANTS_UPDATE_CHUNK):
        chunk = items[pos:pos + _SEARCH_INVARIANTS_UPDATE_CHUNK]
        last_exc: Exception | None = None
        for attempt in range(1, _SEARCH_INVARIANTS_UPDATE_TRIES + 1):
            try:
                part = ctx.grid.update_unified_adgroups(chunk)
                updated.extend(part or [])
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt >= _SEARCH_INVARIANTS_UPDATE_TRIES:
                    break
                time.sleep(2 * attempt)
        if last_exc is not None:
            raise RuntimeError(
                f"cid={cid} chunk {pos // _SEARCH_INVARIANTS_UPDATE_CHUNK + 1}: {str(last_exc)[:180]}"
            ) from last_exc
    return updated


def step_fix_organic_placement(ctx: CopyCtx) -> dict:
    """Перенести isOrganicSearchEnabled и placementTypes из источника на копию (1:1).

    Grid-only поля: v5-путь копирования создаёт кампании с дефолтами Директа
    (organic=True, placementTypes=[ADV_GALLERY, SEARCH_PAGE]). Здесь читаем значения
    ИСТОЧНИКА через source_grid и ставим их на TARGET через narrow UpdateCampaigns.

    Кампании с DEFAULT / OPTIMIZE_CLICKS-без-лимита — в skipped (нет безопасного write-enum).
    Добавлено 2026-07-17 для исправления расхождений, найденных step_settings_diff.
    """
    rep = {"fixed": 0, "skipped": {}, "already_ok": 0, "errors": []}
    if ctx.source_grid is None or ctx.grid is None:
        rep["errors"].append("нет куки источника или цели — organic/placement перенос пропущен")
        return rep
    pairs = _campaign_pairs(ctx)
    if not pairs:
        rep["errors"].append("нет маппинга кампаний — organic/placement перенос пропущен")
        return rep

    try:
        src_rows = _source_edit_rows(ctx, [s for s, _ in pairs])
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"source grid read: {str(e)[:200]}")
        return rep
    try:
        tgt_rows = _target_edit_rows(ctx, [t for _, t in pairs])
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"target grid read: {str(e)[:200]}")
        return rep

    campaign_values: dict[int, dict] = {}
    for src_id, tgt_id in pairs:
        src = src_rows.get(src_id)
        tgt = tgt_rows.get(tgt_id)
        if not src or not tgt:
            continue
        src_organic = bool(src.get("isOrganicSearchEnabled"))
        src_pts = src.get("placementTypes") or None
        tgt_organic = bool(tgt.get("isOrganicSearchEnabled"))
        # Нормализуем для сравнения: сортировка для нечувствительности к порядку.
        src_pts_sorted = sorted(src_pts) if src_pts else []
        tgt_pts_sorted = sorted(tgt.get("placementTypes") or [])
        if src_organic == tgt_organic and src_pts_sorted == tgt_pts_sorted:
            rep["already_ok"] += 1
            continue
        campaign_values[tgt_id] = {
            "isOrganicSearchEnabled": src_organic,
            "placementTypes": src_pts,
        }

    if not campaign_values:
        ctx.log(f"organic/placement: все пары уже совпадают ({rep['already_ok']} кампаний)")
        return rep

    try:
        result = ctx.grid.set_campaign_organic_and_placement(campaign_values)
        _invalidate_target_edit_rows(ctx)
        rep["fixed"] = len(result.get("updated") or [])
        rep["skipped"] = result.get("skipped") or {}
        rep["errors"] += result.get("errors") or []
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"set_campaign_organic_and_placement: {str(e)[:220]}")

    ctx.log(f"organic/placement: исправлено {rep['fixed']}, "
            f"пропущено {len(rep['skipped'])} (стратегия), "
            f"уже верных {rep['already_ok']}, ошибок {len(rep['errors'])}")
    return rep


def _search_at_ok(rm: dict | None) -> bool:
    """Профиль автотаргета корректен: активен + EXACT_V2_MARK + WITHOUT_BRAND (без лишних).
    Инлайн-копия repair_keywords._autotarget_ok — чтобы не тянуть внешний импорт."""
    if not isinstance(rm, dict) or not rm.get("isActive"):
        return False
    cats = {str(x).upper() for x in (rm.get("relevanceMatchCategories") or [])}
    brands = {str(x).upper() for x in (rm.get("autotargetingBrandSettings") or [])}
    return cats == {"EXACT_V2_MARK"} and brands == {"WITHOUT_BRAND"}


def step_fix_search_campaign_invariants(ctx: CopyCtx) -> dict:
    """Форсировать Grid-only инварианты tp2/TEXT_CAMPAIGN ДО live_verification.

    v5 adgroups.add не пишет два Grid-only поля → Яндекс ставит дефолты:
      * enableCompanyInfo=True  → live_verification даёт COMPANY_INFO_ENABLED_LIVE
      * автотаргет — все 5 категорий + 3 бренда → WRONG_AUTOTARGET

    Что делает шаг (только TEXT_CAMPAIGN, UAC/товарка не трогаем):
      1. set_campaign_invariants(target_ids)  — ставит enableCompanyInfo=False через
         Grid UpdateCampaigns (переиспользует готовый мутатор grid_finalize; идемпотентно).
      2. groups_for_edit(cid) + build_update_item(..., relevance_match=EXACT_V2_MARK/WITHOUT_BRAND)
         + update_unified_adgroups(items) — профиль автотаргета через Grid UpdateUnifiedAdGroups
         (тот же мутатор, что repair_keywords.execute_keywords_repair; идемпотентно: пропускает
         группы, где профиль уже корректен или есть retargetings/bid_modifiers).

    Порядок: вызывается ПОСЛЕ videos и ДО live_verification в _copy_cookie_postprocess.
    Добавлено 2026-07-19 для устранения WRONG_AUTOTARGET×4 + COMPANY_INFO_ENABLED_LIVE×4 на tp2.
    """
    rep: dict = {
        "camps_found": 0,
        "invariants_updated": 0,
        "at_groups_fixed": 0,
        "at_groups_already_ok": 0,
        "at_groups_skipped": 0,
        "errors": [],
    }
    if ctx.grid is None:
        rep["errors"].append("нет grid-клиента — search invariants пропущен")
        return rep

    camp_map = ctx.maps.get("campaigns") or {}
    # Читаем snapshot-кампании источника, отбираем TEXT_CAMPAIGN → target id.
    target_text_ids: list[int] = []
    for c in _rj(ctx.src_dir / "campaigns.json"):
        if not isinstance(c, dict):
            continue
        ctype = str(c.get("Type") or "").strip()
        if ctype != "TEXT_CAMPAIGN":
            continue
        tgt = camp_map.get(str(c.get("Id") or ""))
        if tgt is None:
            continue
        try:
            target_text_ids.append(int(tgt))
        except (TypeError, ValueError):
            continue
    rep["camps_found"] = len(target_text_ids)
    if not target_text_ids:
        ctx.log("search-invariants: нет TEXT_CAMPAIGN в snapshot — пропущено")
        return rep

    # 1) enableCompanyInfo=False — через set_campaign_invariants (идемпотентный UpdateCampaigns).
    try:
        updated = ctx.grid.set_campaign_invariants(target_text_ids)
        rep["invariants_updated"] = len(updated or [])
        ctx.log(f"search-invariants: set_campaign_invariants → {rep['invariants_updated']} кампаний")
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"set_campaign_invariants: {str(e)[:220]}")

    # 2) Профиль автотаргета EXACT_V2_MARK+WITHOUT_BRAND через UpdateUnifiedAdGroups.
    #    Читаем группы ПО ОДНОЙ КАМПАНИИ (per-cid): ошибка одной кампании не рубит остальные.
    _target_rm = {
        "isActive": True,
        "id": None,
        "relevanceMatchCategories": ["EXACT_V2_MARK"],
        "autotargetingBrandSettings": ["WITHOUT_BRAND"],
    }
    try:
        _gfe_meta: dict = {}
        groups_all = ctx.grid.groups_for_edit(target_text_ids, meta=_gfe_meta)
        if _gfe_meta.get("adgroups_truncated") or _gfe_meta.get("keywords_truncated"):
            ctx.log("search-invariants: batch groups_for_edit обрезан лимитом — fallback per-campaign")
            groups_by_campaign = {}
        else:
            groups_by_campaign: dict[int, list[dict]] = {}
            for grp in groups_all:
                try:
                    groups_by_campaign.setdefault(int(grp.get("campaign_id") or 0), []).append(grp)
                except (TypeError, ValueError):
                    continue
    except Exception as e:  # noqa: BLE001
        ctx.log(f"search-invariants: batch groups_for_edit не удался ({str(e)[:160]}) — fallback per-campaign")
        groups_by_campaign = {}

    for cid in target_text_ids:
        try:
            groups = groups_by_campaign.get(cid)
            if groups is None:
                groups = ctx.grid.groups_for_edit(cid)
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"groups_for_edit cid={cid}: {str(e)[:200]}")
            continue
        write_items: list[dict] = []
        for grp in groups:
            if not grp.get("supported"):
                rep["at_groups_skipped"] += 1
                continue
            if grp.get("retargetings_present") or grp.get("bid_modifiers_present"):
                rep["at_groups_skipped"] += 1
                continue
            rm = grp.get("relevance_match")
            if _search_at_ok(rm):
                rep["at_groups_already_ok"] += 1
                continue
            # Preserve existing rm.id so Яндекс не создаёт дублирующую запись relevanceMatch.
            effective_rm = dict(_target_rm)
            if isinstance(rm, dict) and rm.get("id"):
                effective_rm["id"] = rm["id"]
            try:
                # Передаём РЕАЛЬНЫЕ ключи группы (replace-семантика: keywords=[] → 0 ключей,
                # live-проба 2026-07-19 группа 5774526683: было 62 → стало 0). grp["keywords"]
                # содержит полный текущий набор фраз (кэш groups_for_edit, лимит 10000 на
                # кампанию; для типичного tp2 ~30 групп × ~80 ключей ≪ 10000). build_update_item
                # режет до [:200]/группа — для tp2 60-80 ключей/группа это без потерь.
                item = ctx.grid.build_update_item(
                    grp, keywords=grp.get("keywords", []), relevance_match=effective_rm)
            except Exception as e:  # noqa: BLE001
                rep["errors"].append(f"build_update_item gid={grp.get('adgroup_id')}: {str(e)[:180]}")
                continue
            write_items.append(item)
        if not write_items:
            continue
        try:
            updated_gids = _update_unified_adgroups_resilient(ctx, cid, write_items)
            _invalidate_target_edit_rows(ctx)
            rep["at_groups_fixed"] += len(updated_gids or [])
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"update_unified_adgroups cid={cid}: {str(e)[:220]}")

    ctx.log(
        f"search-invariants: кампаний {rep['camps_found']}, "
        f"enableCompanyInfo обновлено {rep['invariants_updated']}, "
        f"автотаргет исправлено {rep['at_groups_fixed']} групп, "
        f"уже верных {rep['at_groups_already_ok']}, "
        f"пропущено {rep['at_groups_skipped']}, "
        f"ошибок {len(rep['errors'])}"
    )
    return rep


_DIFF_SKIP_KEYS = {
    # идентичность и время
    "id", "exportId", "name", "startDate", "endDate", "createTime", "lastShowTime",
    "reqId", "telemetryTraceId", "__typename", "source", "flowType", "brandSurveyId",
    # статусы/статистика/права — не настройки
    "status", "aggregatedStatusInfo", "access", "stat", "statistics", "groupsCount",
    "strategyLearningStatus", "isObsolete", "tags",
    # намеренно меняем
    "domain", "domains", "hasDomain", "href", "counters", "counterIds", "goals", "meaningfulGoals",
    "priorityGoals", "geo", "regionIds", "restrictedRegionIds", "images", "imageHashes",
    "isCampaignUrlEcomWithIndustry",
    # strategyId — идентификатор стратегии в аккаунте, у копии он НОВЫЙ по определению.
    # Сам ТИП стратегии (strategyName/strategyType/budget) сравниваем — он совпадать обязан.
    "strategyId",
    # наш стандарт поверх источника
    "disabledPlaces", "bidModifiers", "disabledIps",
}


_DIFF_V5_SETTINGS_FIX = {
    "hasAddMetrikaTagToUrl": "ADD_METRICA_TAG",
    "hasExtendedGeoTargeting": "ENABLE_AREA_OF_INTEREST_TARGETING",
    "isAlternativeTextsEnabled": "ALTERNATIVE_TEXTS_ENABLED",
}


def _diff_norm(val, _depth: int = 0):
    """Нормализация значения для сравнения: выкидываем skip-ключи, пустое приравниваем к None."""
    if _depth > 6:
        return "…"
    if isinstance(val, dict):
        out = {}
        for k, v in val.items():
            if k in _DIFF_SKIP_KEYS:
                continue
            nv = _diff_norm(v, _depth + 1)
            if nv in (None, {}, [], ""):
                continue
            out[k] = nv
        return out
    if isinstance(val, list):
        items = [_diff_norm(v, _depth + 1) for v in val]
        items = [i for i in items if i not in (None, {}, [], "")]
        try:
            return sorted(items, key=lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True))
        except (TypeError, ValueError):
            return items
    return val


def _diff_rows(src_row: dict, tgt_row: dict) -> list[dict]:
    """Плоский список расхождений [{path, source, target}] между двумя нормализованными строками."""
    out: list[dict] = []

    def walk(a, b, path: str):
        if isinstance(a, dict) and isinstance(b, dict):
            for k in sorted(set(a) | set(b)):
                walk(a.get(k), b.get(k), f"{path}.{k}" if path else k)
            return
        if a != b:
            sa = json.dumps(a, ensure_ascii=False)[:160]
            sb = json.dumps(b, ensure_ascii=False)[:160]
            out.append({"path": path, "source": sa, "target": sb})

    walk(_diff_norm(src_row), _diff_norm(tgt_row), "")
    return out


def _fix_v5_settings(ctx: CopyCtx, pairs: list, src_rows: dict, tgt_rows: dict, rep: dict) -> bool:
    """Автопочинка 3 опций Settings по значению ИСТОЧНИКА через v5 campaigns.update.

    Возвращает True, если хоть одна кампания реально обновлена (тогда target-строки надо перечитать).
    Только TEXT_CAMPAIGN: у прочих типов блок Settings живёт в другом блоке — вслепую не трогаем.
    Любая ошибка → в rep['fix_errors'], сверка ниже всё равно покажет остаточное расхождение."""
    if not (ctx.v5_call and ctx.target_token):
        return False
    types = {}
    for c in (_rj(ctx.src_dir / "campaigns.json") or []):
        if isinstance(c, dict):
            types[str(c.get("Id") or "")] = str(c.get("Type") or "")
    updated = False
    for src_id, tgt_id in pairs:
        s, t = src_rows.get(src_id), tgt_rows.get(tgt_id)
        if not s or not t:
            continue
        if types.get(str(src_id)) != "TEXT_CAMPAIGN":
            rep["fix_skipped"] += 1
            continue
        settings = []
        for grid_key, option in _DIFF_V5_SETTINGS_FIX.items():
            src_val, tgt_val = s.get(grid_key), t.get(grid_key)
            # Grid не отдал поле у источника → чинить не от чего (fail-safe, не гадаем).
            if not isinstance(src_val, bool) or not isinstance(tgt_val, bool) or src_val == tgt_val:
                continue
            settings.append({"Option": option, "Value": "YES" if src_val else "NO"})
        if not settings:
            continue
        try:
            j = ctx.v5_call("campaigns", "update", ctx.target_token, ctx.target_login,
                            {"Campaigns": [{"Id": int(tgt_id), "TextCampaign": {"Settings": settings}}]})
            errs = ((j.get("result") or {}).get("UpdateResults") or [{}])[0].get("Errors") or j.get("error")
            if errs:
                rep["fix_errors"].append(f"кампания {tgt_id}: {json.dumps(errs, ensure_ascii=False)[:180]}")
                continue
            rep["fixed_campaigns"] += 1
            rep["fixed_options"] += len(settings)
            updated = True
        except Exception as e:  # noqa: BLE001
            rep["fix_errors"].append(f"кампания {tgt_id}: {str(e)[:180]}")
    return updated


def step_settings_diff(ctx: CopyCtx) -> dict:
    """Сверка настроек источник ↔ копия по кукам + автопочинка того, что умеем.

    Зона ответственности (vs copy_verify.diff_profiles):
    - step_settings_diff — сырые Grid edit_rows поля (Strategy, TimeTarget, BrandSafety, ContextLimit,
      уведомления, disabledPlaces и т.д.); работает IN-COPY с кукой; ремонтирует 3 опции Settings.
    - diff_profiles (copy_verify.py) — структурные измерения контента (ключи, объявления, ассеты);
      работает POST-COPY, REPORT-ONLY.
    Пересечений нет: минус-слова в raw edit_rows (step_settings_diff) vs кол-во библиотечных наборов
    D3 (diff_profiles); стратегия в raw rows vs strategyName D12 (diff_profiles). Разные данные.

    Чиним ТОЛЬКО 3 опции v5 Settings (`_DIFF_V5_SETTINGS_FIX`) — ставим значение источника 1:1.
    Остальное — по-прежнему report-only (organic/placement чинит step_fix_organic_placement,
    промо — step_attach_promos; оба зовутся ДО этого шага). Сверка считается ПОСЛЕ починки —
    отчёт показывает остаточное расхождение, а не то, что мы уже исправили.

    Требует обе куки (source_grid + grid). Нет одной — честный skip, а не молчаливый «ок»:
    пустой отчёт не должен выглядеть как «расхождений нет»."""
    rep = {"status": "skip", "pairs": 0, "diff_campaigns": 0, "diffs": [], "errors": [],
           "fixed_campaigns": 0, "fixed_options": 0, "fix_skipped": 0, "fix_errors": []}
    if ctx.source_grid is None or ctx.grid is None:
        rep["errors"].append("нет куки источника или цели — сверка настроек не выполнена")
        return rep
    pairs = _campaign_pairs(ctx)
    if not pairs:
        rep["errors"].append("нет пар кампаний источник→цель")
        return rep
    try:
        src_rows = _source_edit_rows(ctx, [s for s, _ in pairs])
        tgt_rows = _target_edit_rows(ctx, [t for _, t in pairs])
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"чтение настроек по кукам: {str(e)[:200]}")
        return rep

    # Автопочинка ДО сверки: иначе отчёт покажет расхождения, которые мы только что закрыли.
    if _fix_v5_settings(ctx, pairs, src_rows, tgt_rows, rep):
        try:
            _invalidate_target_edit_rows(ctx)
            tgt_rows = _target_edit_rows(ctx, [t for _, t in pairs])
        except Exception as e:  # noqa: BLE001
            rep["fix_errors"].append(f"перечитывание цели после починки: {str(e)[:160]}")
        ctx.log(f"настройки Settings: починено {rep['fixed_campaigns']} кампаний "
                f"({rep['fixed_options']} опций 1:1 с источником)"
                + (f", ошибок {len(rep['fix_errors'])}" if rep["fix_errors"] else ""))

    for src_id, tgt_id in pairs:
        s, t = src_rows.get(src_id), tgt_rows.get(tgt_id)
        if not s or not t:
            rep["errors"].append(f"кампания {src_id}→{tgt_id}: нет данных Grid "
                                 f"({'источник' if not s else 'цель'})")
            continue
        rep["pairs"] += 1
        d = _diff_rows(s, t)
        if d:
            rep["diff_campaigns"] += 1
            rep["diffs"].append({"source_id": src_id, "target_id": tgt_id,
                                 "name": str(s.get("name") or "")[:70], "items": d[:20]})
    rep["status"] = "ok" if rep["pairs"] and not rep["diff_campaigns"] else (
        "diff" if rep["diff_campaigns"] else "skip")
    ctx.log(f"сверка настроек по кукам: пар {rep['pairs']}, с расхождениями {rep['diff_campaigns']}"
            + (f", ошибок {len(rep['errors'])}" if rep["errors"] else ""))
    return rep

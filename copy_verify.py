"""copy_verify facade. Implementation is split into source/target/diff/geo/repair modules."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import grid_finalize as gf
from .copy_verify_state import configure
from .copy_verify_utils import (
    _OK, _MISMATCH, _MISSING, _UNREADABLE, _EXCLUDED,
    _nolog, _rj, _rj_dict, _strip_domain,
)
from .copy_verify_source import build_source_profile
from .copy_verify_target import build_target_profile
from .copy_verify_diff import diff_profiles
from .copy_verify_geo import check_geo_kw_consistency
from .copy_verify_repair import run_copy_repair


def run_copy_verification(src_dir: Any,
                          workdir: Any,
                          target_login: str,
                          target_agency: str,
                          *,
                          geo_pairs: Optional[list] = None,
                          grid: Optional[gf.GridClient] = None,
                          source_grid: Optional[gf.GridClient] = None,
                          cached_counts: Optional[Dict[int, dict]] = None,
                          cached_edit_rows: Optional[Dict[int, dict]] = None,
                          cached_invariants: Optional[Dict[int, dict]] = None,
                          cached_adaptive_src: Optional[Dict[int, dict]] = None,
                          cached_adaptive_tgt: Optional[Dict[int, dict]] = None,
                          log: Optional[Callable[[str], None]] = None,
                          ) -> dict:
    """Верхняя точка входа: сверка source↔target после копирования РК.

    Вызывается из _copy_cookie_postprocess после завершения всех шагов.
    REPORT-ONLY: не изменяет кампании.

    Args:
        src_dir: Директория снапшота источника (phase_pull).
        workdir: Рабочая директория джобы (содержит id_maps.json).
        target_login: Логин целевого аккаунта.
        target_agency: Агентство цели (для context, не используется напрямую).
        grid: Pre-built gf.GridClient цели (из _copy_cookie_postprocess).
        source_grid: Pre-built gf.GridClient источника (для grid_snapshot adaptive).
        cached_counts / cached_edit_rows / cached_invariants: Pre-fetched Grid-читатели
            (если уже были вызваны в постпроцессе — передать сюда для DRY).
        cached_adaptive_src: Pre-fetched adaptive_ads_for_update ИСТОЧНИКА
            {int(ad_id): {...}} (titles/bodies/imageHashes/hasVideo/hasButton).
        cached_adaptive_tgt: Pre-fetched adaptive_ads_for_update ЦЕЛИ.
        log: Функция логирования (копирует в job-лог).

    Returns:
        {
            "results":  [{scope, dimension, status, source, target, repairable, repair_hint}],
            "summary":  {"ok": int, "mismatch": int, "missing": int, "unreadable": int},
        }
    """
    _log = log or _nolog
    src_dir = Path(src_dir)
    workdir = Path(workdir)

    _empty: Dict[str, Any] = {
        "results": [],
        "summary": {"ok": 0, "mismatch": 0, "missing": 0, "unreadable": 0},
    }

    # id_maps.json
    maps_path = workdir / "id_maps.json"
    id_maps: dict = {}
    try:
        if maps_path.exists():
            id_maps = json.loads(maps_path.read_text(encoding="utf-8"))
    except Exception as e:
        _log(f"copy_verify: id_maps read error: {str(e)[:200]}")
        out = dict(_empty)
        out["error"] = f"id_maps read: {str(e)[:200]}"
        return out

    # Если source_grid передан — получаем adaptive source data (titles/bodies/video/button)
    grid_snapshot: Optional[Dict[int, dict]] = cached_adaptive_src
    if grid_snapshot is None and source_grid is not None:
        # Собираем src_ad_ids из snapshot ads.json для запроса
        src_ads = _rj(src_dir / "ads.json")
        src_ad_ids: List[int] = []
        src_camp_ids_for_adaptive: List[int] = []
        for ad in src_ads:
            try:
                aid = int(ad.get("Id") or 0)
                cid = int(ad.get("CampaignId") or 0)
            except (TypeError, ValueError):
                continue
            if aid > 0 and aid not in src_ad_ids:
                src_ad_ids.append(aid)
            if cid > 0 and cid not in src_camp_ids_for_adaptive:
                src_camp_ids_for_adaptive.append(cid)
        if src_ad_ids:
            try:
                grid_snapshot = source_grid.adaptive_ads_for_update(
                    src_camp_ids_for_adaptive, src_ad_ids)
            except Exception as e:
                _log(f"copy_verify: source adaptive_ads_for_update error: {str(e)[:200]}")
                grid_snapshot = None

    _log("copy_verify: строим профиль источника…")
    try:
        src_profile = build_source_profile(
            src_dir,
            grid_snapshot=grid_snapshot,
            source_grid=source_grid,
            log=_log,
        )
    except Exception as e:
        _log(f"copy_verify: build_source_profile error: {str(e)[:200]}")
        out = dict(_empty)
        out["error"] = f"build_source_profile: {str(e)[:200]}"
        return out

    _log(f"copy_verify: source profile — {len(src_profile)} кампаний")

    _log("copy_verify: строим профиль цели…")
    try:
        tgt_profile = build_target_profile(
            target_login=target_login,
            id_maps=id_maps,
            grid=grid,
            cached_counts=cached_counts,
            cached_edit_rows=cached_edit_rows,
            cached_invariants=cached_invariants,
            cached_adaptive=cached_adaptive_tgt,
            log=_log,
            target_agency=target_agency,
        )
    except Exception as e:
        _log(f"copy_verify: build_target_profile error: {str(e)[:200]}")
        out = dict(_empty)
        out["error"] = f"build_target_profile: {str(e)[:200]}"
        return out

    _log(f"copy_verify: target profile — {len(tgt_profile)} кампаний")

    _log("copy_verify: diff profiles…")
    try:
        results = diff_profiles(src_profile, tgt_profile, id_maps)
    except Exception as e:
        _log(f"copy_verify: diff_profiles error: {str(e)[:200]}")
        out = dict(_empty)
        out["error"] = f"diff_profiles: {str(e)[:200]}"
        return out

    # Задача 1: гео-консистентность ключей/минусов (REPORT-ONLY, snapshot-based).
    try:
        # A3: v5-путь — snapshot сырой → snapshot_transformed=False → оба измерения EXCLUDED.
        geo_rows = check_geo_kw_consistency(
            src_dir, geo_pairs or [], snapshot_transformed=False, log=_log
        )
        results.extend(geo_rows)
    except Exception as e:
        _log(f"copy_verify: geo_kw_consistency error: {str(e)[:200]}")

    summary: Dict[str, int] = {"ok": 0, "mismatch": 0, "missing": 0, "unreadable": 0}
    for r in results:
        st = r.get("status", "")
        if st in summary:
            summary[st] += 1
        # excluded_intentional не считаем ошибкой — не в summary

    _log(f"copy_verify: итог — ok={summary['ok']}, mismatch={summary['mismatch']}, "
         f"missing={summary['missing']}, unreadable={summary['unreadable']}, "
         f"total={len(results)}")

    return {"results": results, "summary": summary}


__all__ = [
    "configure", "build_source_profile", "build_target_profile", "diff_profiles",
    "check_geo_kw_consistency", "run_copy_verification", "run_copy_repair",
]

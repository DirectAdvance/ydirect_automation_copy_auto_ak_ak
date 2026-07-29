"""Geo/keyword consistency checks for copy verification."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import campaign as cmc
from .. import grid_finalize as gf
from .. import grid_read as gr
from .copy_verify_utils import (
    _OK, _MISMATCH, _MISSING, _UNREADABLE, _EXCLUDED,
    _nolog, _rj, _rj_dict, _strip_domain,
)


def check_geo_kw_consistency(src_dir: Any,
                             geo_pairs: List[Tuple[str, str]],
                             *,
                             snapshot_transformed: bool = True,
                             log: Optional[Callable[[str], None]] = None,
                             ) -> List[dict]:
    """Задача 1: измерение гео-консистентности ключей/минус-слов (REPORT-ONLY, snapshot-based).

    Проверяет snapshot-файлы ИСТОЧНИКА на два признака:
    1. geo_kw_source_residual — количество фраз в keywords.json, содержащих формы
       ИСТОЧНИКА (они должны были быть заменены step_keywords гео-морфологией).
    2. geo_neg_target_blocked — количество минус-слов в adgroups.json/campaigns.json,
       содержащих формы ЦЕЛЕВОГО города (они должны были быть отфильтрованы).

    Args:
        snapshot_transformed: True (ЕПК-путь) — snapshot содержит УЖЕ ЗАМЕЩЁННЫЕ ключи;
            residual==0 означает корректную замену.
            False (v5-путь) — snapshot содержит СЫРЫЕ ключи источника (замена применена
            step_keywords к заливаемым данным, не к snapshot); остаточный подсчёт дал бы
            ЛОЖНЫЙ MISMATCH → оба измерения возвращаются _EXCLUDED с пояснением.

    Возвращает две строки [{scope="global", dimension=..., status, source, target, ...}].
    Пустой geo_pairs → оба измерения excluded_intentional (нет гео-замены — нет требования)."""
    _log = log or _nolog
    src_dir = Path(src_dir)
    rows: List[dict] = []

    def _row(dimension: str, status: str, source: Any, target: Any,
             repair_hint: str = "") -> dict:
        return {
            "scope": "global",
            "dimension": dimension,
            "status": status,
            "source": source,
            "target": target,
            "repairable": False,
            "repair_hint": repair_hint,
        }

    # A3: v5-путь → snapshot сырой, residual = ложный MISMATCH → исключаем (не считаем).
    if not snapshot_transformed:
        rows.append(_row("geo_kw_source_residual", _EXCLUDED, 0, None,
                         "v5-путь: snapshot источника сырой — гео-замена применена "
                         "step_keywords при заливке, а не к snapshot; "
                         "residual по snapshot будет ложно-положительным — измерение исключено"))
        rows.append(_row("geo_neg_target_blocked", _EXCLUDED, 0, None,
                         "v5-путь: snapshot источника сырой — фильтрация минусов по целевому гео "
                         "применена step_keywords, не к snapshot — измерение исключено"))
        return rows

    if not geo_pairs:
        rows.append(_row("geo_kw_source_residual", _EXCLUDED,
                         0, None,
                         "geo_pairs пуст (geo_mode=keep или нет гео-замены) — проверка не нужна"))
        rows.append(_row("geo_neg_target_blocked", _EXCLUDED,
                         0, None,
                         "geo_pairs пуст — фильтрация целевого гео в минусах не применялась"))
        return rows

    # Формы источника (левые части пар) и целевого гео (правые части).
    source_forms = sorted(
        {old.lower() for old, _ in geo_pairs if (old or "").strip()},
        key=len, reverse=True,
    )
    target_forms = sorted(
        {new.lower() for _, new in geo_pairs if (new or "").strip()},
        key=len, reverse=True,
    )

    # 1) geo_kw_source_residual: сколько фраз в keywords.json ещё содержат формы ИСТОЧНИКА.
    # A4: различаем «файл отсутствует» (норма) от «файл есть, но чтение упало» (EXCLUDED+ошибка).
    kw_with_src = 0
    _kw_read_err: Optional[str] = None
    try:
        kw_path = src_dir / "keywords.json"
        if kw_path.exists():
            _raw_kw = json.loads(kw_path.read_text(encoding="utf-8"))
            keywords: list = _raw_kw if isinstance(_raw_kw, list) else \
                ([_raw_kw] if isinstance(_raw_kw, dict) else [])
            for kw in keywords:
                phrase = (kw.get("Keyword") or "").lower()
                if not phrase:
                    continue
                if any(re.search(r"\b" + re.escape(sf) + r"\b", phrase, re.UNICODE)
                       for sf in source_forms):
                    kw_with_src += 1
        # else: файла нет → нечего проверять (не ошибка)
    except Exception as e:  # noqa: BLE001
        _kw_read_err = str(e)[:200]
        _log(f"geo_kw_consistency: keywords.json read error: {_kw_read_err}")

    if _kw_read_err:
        rows.append(_row(
            "geo_kw_source_residual", _EXCLUDED, 0, None,
            f"A4: ошибка чтения keywords.json (файл существует, но прочитать не удалось): {_kw_read_err}",
        ))
    else:
        rows.append(_row(
            "geo_kw_source_residual",
            _OK if kw_with_src == 0 else _MISMATCH,
            kw_with_src,
            0,
            repair_hint=(
                "v5-путь: step_keywords (copy_steps.py) применяет гео-замену через ctx.geo_pairs; "
                "ЕПК-путь: замена применена инлайн в group_specs перед create_full (copy_engine.py:1237); "
                "в snapshot пишутся уже замещённые ключи → 0 = замена корректна"
            ),
        ))

    # 2) geo_neg_target_blocked: сколько минус-слов содержат формы ЦЕЛЕВОГО города.
    # Проверяем campaigns.json (campaign-level NegativeKeywords) и adgroups.json (group-level).
    # A4: отдельные read_err для каждого файла; результат — EXCLUDED если оба прочитать не удалось.
    neg_with_tgt = 0
    _camp_read_err: Optional[str] = None
    _ag_read_err: Optional[str] = None

    try:
        camp_path = src_dir / "campaigns.json"
        if camp_path.exists():
            _raw_camps = json.loads(camp_path.read_text(encoding="utf-8"))
            camps_list: list = _raw_camps if isinstance(_raw_camps, list) else \
                ([_raw_camps] if isinstance(_raw_camps, dict) else [])
            for camp in camps_list:
                raw = camp.get("NegativeKeywords") or {}
                items = raw.get("Items") if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
                for m in (items or []):
                    low = (m or "").lower()
                    if any(re.search(r"\b" + re.escape(tf) + r"\b", low, re.UNICODE)
                           for tf in target_forms):
                        neg_with_tgt += 1
    except Exception as e:  # noqa: BLE001
        _camp_read_err = str(e)[:150]
        _log(f"geo_kw_consistency: campaigns.json read error: {_camp_read_err}")

    try:
        ag_path = src_dir / "adgroups.json"
        if ag_path.exists():
            _raw_ags = json.loads(ag_path.read_text(encoding="utf-8"))
            ags_list: list = _raw_ags if isinstance(_raw_ags, list) else \
                ([_raw_ags] if isinstance(_raw_ags, dict) else [])
            for ag in ags_list:
                raw = ag.get("NegativeKeywords") or {}
                items = raw.get("Items") if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
                for m in (items or []):
                    low = (m or "").lower()
                    if any(re.search(r"\b" + re.escape(tf) + r"\b", low, re.UNICODE)
                           for tf in target_forms):
                        neg_with_tgt += 1
    except Exception as e:  # noqa: BLE001
        _ag_read_err = str(e)[:150]
        _log(f"geo_kw_consistency: adgroups.json read error: {_ag_read_err}")

    _neg_read_err_msg = "; ".join(
        f for f in [
            (f"campaigns.json: {_camp_read_err}" if _camp_read_err else ""),
            (f"adgroups.json: {_ag_read_err}" if _ag_read_err else ""),
        ] if f
    )
    if _neg_read_err_msg and neg_with_tgt == 0:
        # Оба файла не прочитаны и счётчик 0 → не можем отличить «OK» от «ошибка чтения»
        rows.append(_row(
            "geo_neg_target_blocked", _EXCLUDED, 0, None,
            f"A4: ошибка чтения файлов минус-слов, результат ненадёжен: {_neg_read_err_msg}",
        ))
    else:
        if _neg_read_err_msg:
            _log(f"geo_kw_consistency: частичная ошибка чтения минусов, счётчик={neg_with_tgt}: {_neg_read_err_msg}")
        rows.append(_row(
            "geo_neg_target_blocked",
            _OK if neg_with_tgt == 0 else _MISMATCH,
            neg_with_tgt,
            0,
            repair_hint=(
                "ЕПК-путь: _copy_geo_filter_negatives удаляет минусы с формами ЦЕЛЕВОГО гео "
                "(copy_engine.py:1240-1243); snapshot пишется с отфильтрованными минусами → 0 = фильтрация корректна; "
                "v5-путь: campaign/group негативы заливаются direct_copy.py до наших шагов — "
                "ненулевое значение = диагностически значимо"
            ),
        ))

    _log(f"geo_kw_consistency: source_residual_kw={kw_with_src}, neg_with_target_geo={neg_with_tgt}")
    return rows

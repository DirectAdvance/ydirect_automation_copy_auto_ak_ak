"""Общие мелкие helper'ы для copy_steps.*."""
from __future__ import annotations

import json
from pathlib import Path


def _rj(path: Path):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def _wj(path: Path, data) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _v5_add_err(j: dict) -> str:
    try:
        errs = []
        for r in ((j.get("result") or {}).get("AddResults") or []):
            for e in (r.get("Errors") or []):
                errs.append(e.get("Message") or e.get("Details") or str(e))
        if errs:
            return "; ".join(errs)[:300]
        if j.get("error"):
            return str(j.get("error"))[:300]
    except Exception:  # noqa: BLE001
        pass
    return ""


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _campaign_pairs(ctx) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    for src_id, tgt_id in (ctx.maps.get("campaigns") or {}).items():
        try:
            s_id, t_id = int(src_id), int(tgt_id)
        except (TypeError, ValueError):
            continue
        if s_id > 0 and t_id > 0:
            pairs.append((s_id, t_id))
    return pairs


def _subset_rows(rows: dict | None, ids: list[int], *, require_all: bool = False) -> dict | None:
    if not isinstance(rows, dict):
        return None
    out = {}
    for cid in ids:
        row = rows.get(cid) or rows.get(str(cid))
        if row is not None:
            out[cid] = row
    if require_all and len(out) != len(ids):
        return None
    return out


def _source_edit_rows(ctx, ids: list[int]) -> dict:
    base = dict(getattr(ctx, "cached_source_edit_rows", None) or {})
    cached = _subset_rows(base, ids)
    missing = [cid for cid in ids if cid not in (cached or {})]
    if missing:
        rows = ctx.source_grid.campaigns_edit_rows(missing)
        base.update(rows or {})
        ctx.cached_source_edit_rows = base
    elif getattr(ctx, "cached_source_edit_rows", None) is None:
        ctx.cached_source_edit_rows = base
    return _subset_rows(base, ids) or {}


def _target_edit_rows(ctx, ids: list[int]) -> dict:
    base = dict(getattr(ctx, "cached_target_edit_rows", None) or {})
    cached = _subset_rows(base, ids)
    missing = [cid for cid in ids if cid not in (cached or {})]
    if missing:
        rows = ctx.grid.campaigns_edit_rows(missing)
        base.update(rows or {})
        ctx.cached_target_edit_rows = base
    elif getattr(ctx, "cached_target_edit_rows", None) is None:
        ctx.cached_target_edit_rows = base
    return _subset_rows(base, ids) or {}


def _invalidate_target_edit_rows(ctx) -> None:
    ctx.cached_target_edit_rows = None

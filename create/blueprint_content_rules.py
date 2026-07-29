"""Правила вкладки «Контент» (public.direct_content_asset_rules) + фильтрация ассетов —
вынесено из blueprint.py БЕЗ изменения логики (только перемещение + ре-экспорт).

Кластер: `_content_rules_ensure`, `_content_rules_map` (+кэш `_CONTENT_RULES_CACHE`),
`_asset_key_from_local`, `_manual_rule_lookup_key`, `_content_rule_key`, `_ct_allowed_for`,
`_content_allowed_list`, `_content_slepok_list`, `_slepok_allowed_for`, `_content_only_this_ct`,
`_filter_content_assets`, `_prioritized_content_assets`, `_explicit_content_assets_for`,
`_ahash_distance`, `_dedupe_content_assets_for_ui`.

DI (инъектятся из blueprint через configure): `_victory_conn_rw` (БД Victory, запись),
`MANUAL_CREATIVES_DIR` (mount локальных Manual-креативов), `_COMMON_IMAGE_CTS` (набор
«общих» ct). `_gc_ct` берётся напрямую из blueprint_targeting (без DI, чистый). Инвариант
wiring-hub: НЕ импортирует blueprint.
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import random
import re
import time

from .. import kontent_pack as kp
from .blueprint_targeting import _gc_ct  # ct из кодера (чистый, без DI)


# ── DI: инъектятся из blueprint ──────────────────────────────────────────────────
def _victory_conn_rw():
    raise RuntimeError("blueprint_content_rules._victory_conn_rw не инъектирован (configure)")


MANUAL_CREATIVES_DIR = "/opt/creatives/Manual"
_COMMON_IMAGE_CTS: set = set()


def configure(deps: dict) -> None:
    """Инъекция зависимостей из blueprint."""
    globals().update(deps)


def _content_rules_ensure(cur) -> None:
    cur.execute(
        "CREATE TABLE IF NOT EXISTS public.direct_content_asset_rules ("
        "asset_key text PRIMARY KEY, asset_type text NOT NULL, source_segment text NOT NULL, "
        "source_tp text NOT NULL, source_ct text NOT NULL, asset_path text NOT NULL, "
        "name text NOT NULL DEFAULT '', enabled boolean NOT NULL DEFAULT true, "
        "allowed_for jsonb NOT NULL DEFAULT '[]'::jsonb, updated_at timestamptz NOT NULL DEFAULT now())"
    )
    cur.execute("ALTER TABLE public.direct_content_asset_rules "
                "ADD COLUMN IF NOT EXISTS source_slepok text NOT NULL DEFAULT ''")
    cur.execute("ALTER TABLE public.direct_content_asset_rules "
                "ADD COLUMN IF NOT EXISTS allowed_slepki jsonb NOT NULL DEFAULT '[]'::jsonb")


_CONTENT_RULES_CACHE: dict = {"ts": 0.0, "rows": {}}


def _content_rules_map(force: bool = False) -> dict:
    now = time.monotonic()
    if not force and _CONTENT_RULES_CACHE["rows"] and now - _CONTENT_RULES_CACHE["ts"] < 60:
        return _CONTENT_RULES_CACHE["rows"]
    import psycopg2.extras
    conn = _victory_conn_rw()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _content_rules_ensure(cur)
        conn.commit()
        cur.execute(
            "SELECT asset_key, asset_type, source_segment, source_tp, source_ct, asset_path, "
            "name, enabled, allowed_for, source_slepok, allowed_slepki "
            "FROM public.direct_content_asset_rules"
        )
        rows = {str(r["asset_key"]): dict(r) for r in cur.fetchall()}
        _CONTENT_RULES_CACHE.update({"ts": now, "rows": rows})
        return rows
    finally:
        conn.close()


def _asset_key_from_local(path: str) -> str:
    return os.path.splitext(os.path.basename(str(path or "")))[0]


def _manual_rule_lookup_key(path: str, ct: str) -> tuple[str, str, str, str, str] | None:
    """Scoped-key для локального Manual-файла.

    Во вкладке «Контент» Manual хранится как remote-файл M3
    /Users/Shared/agency/creatives/Manual/{ct}/{file}.png, а в создании кампаний мы читаем
    локальный mount /opt/creatives/Manual/{ct}/{file}.png. Чтобы выключения/allowed_for работали
    одинаково, строим тот же scoped asset_key.
    """
    try:
        import os as _os
        from .. import kontent_pack as _kp
        p = str(path or "")
        # Manual-файл приходит из ДВУХ корней: sshfs-монт MANUAL_CREATIVES_DIR (легаси) и
        # ЛОКАЛЬНОЕ зеркало NEURO_PACK_MOUNT/_manual (с 2026-07-18 — основной, sshfs вешал
        # потоки). Оба ведут к одному файлу на M3 → ключ правила обязан совпадать, иначе
        # выключения/allowed_for вкладки «Контент» перестали бы применяться к Manual.
        roots = [str(MANUAL_CREATIVES_DIR).rstrip("/")]
        _mirror = getattr(_kp, "_LOCAL_MIRROR_ROOT", None)
        if _mirror:
            roots.append(_os.path.join(_mirror, "_manual").rstrip("/"))
        if not any(p.startswith(r + "/") for r in roots):
            return None
        ct_norm = _gc_ct(ct) or _gc_ct(_os.path.basename(_os.path.dirname(p))) or "ct0000"
        remote = posixpath.join(getattr(_kp, "M3_MANUAL_ROOT", "/Users/Shared/agency/creatives/Manual"),
                                ct_norm, _os.path.basename(p))
        original_key = _kp.remote_asset_key(remote)
        return ("Общее", "manual", ct_norm, original_key, "")
    except Exception:  # noqa: BLE001
        return None


def _content_rule_key(segment: str, tp: str, ct: str, asset_key: str, source_slepok: str = "") -> str:
    """Scope правила контента: тип сайта + tp + ct + слепок + файл.

    Один и тот же файл может лежать в одинаковом ct у разных типов сайтов; правило
    отключения/allowed_for не должно протекать между ними. Слепок также входит в scope,
    чтобы включение/выключение в одном слепке не меняло тот же ct у другого слепка.
    """
    raw = "|".join([
        str(segment or "").strip(),
        str(tp or "").strip(),
        (_gc_ct(ct) or str(ct or "").strip().lower()),
        str(source_slepok or "").strip().lower(),
        str(asset_key or "").strip(),
    ])
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _ct_allowed_for(rule: dict, target_ct: str) -> bool:
    target_ct = _gc_ct(target_ct) or str(target_ct or "").strip().lower()
    allowed = _content_allowed_list(rule)
    source_ct = str(rule.get("source_ct") or "").strip().lower()
    if not allowed:
        return not target_ct or target_ct == source_ct
    if "*" in allowed:
        return True
    if "common" in allowed and target_ct in _COMMON_IMAGE_CTS:
        return True
    return target_ct in allowed


def _content_allowed_list(rule: dict) -> list[str]:
    allowed = rule.get("allowed_for") or []
    if isinstance(allowed, str):
        try:
            allowed = json.loads(allowed)
        except Exception:  # noqa: BLE001
            allowed = [x.strip() for x in allowed.split(",") if x.strip()]
    return [str(x).strip().lower() for x in (allowed or []) if str(x).strip()]


def _content_slepok_list(rule: dict) -> list[str]:
    allowed = rule.get("allowed_slepki") or []
    if isinstance(allowed, str):
        try:
            allowed = json.loads(allowed)
        except Exception:  # noqa: BLE001
            allowed = [x.strip() for x in re.split(r"[,;\s]+", allowed) if x.strip()]
    return [str(x).strip().lower() for x in (allowed or []) if str(x).strip()]


def _slepok_allowed_for(rule: dict, target_slepok: str) -> bool:
    target = str(target_slepok or "").strip().lower()
    source = str(rule.get("source_slepok") or "").strip().lower()
    allowed = _content_slepok_list(rule)
    if not allowed:
        return (not source) or (not target) or source == target
    if "*" in allowed:
        return True
    if "common" in allowed and not target:
        return True
    return target in allowed


def _content_only_this_ct(rule: dict, target_ct: str) -> bool:
    target_ct = _gc_ct(target_ct) or str(target_ct or "").strip().lower()
    source_ct = str(rule.get("source_ct") or "").strip().lower()
    if source_ct != target_ct:
        return False
    allowed = _content_allowed_list(rule)
    if not allowed:
        return True
    return len(allowed) == 1 and allowed[0] == target_ct


def _filter_content_assets(paths: list, target_ct: str, *, source_segment: str = "", source_tp: str = "",
                           source_ct: str = "", target_slepok: str = "", source_slepok: str = "") -> list:
    """Применить вкладку «Контент»: выключенные ассеты режем, allowed_for ограничивает целевой ct.
    Если правила на файл нет — сохраняем старое поведение и пропускаем."""
    if not paths:
        return []
    try:
        rules = _content_rules_map()
    except Exception:  # noqa: BLE001
        return list(paths)
    out = []
    for p in paths:
        file_key = _asset_key_from_local(p)
        r = None
        manual_scope = _manual_rule_lookup_key(p, source_ct or target_ct)
        if manual_scope:
            r = rules.get(_content_rule_key(*manual_scope))
        if source_segment and source_tp and source_ct:
            r = r or rules.get(_content_rule_key(source_segment, source_tp, source_ct, file_key, source_slepok))
            if not r and source_slepok:
                r = rules.get(_content_rule_key(source_segment, source_tp, source_ct, file_key, ""))
        if not r and not (source_segment and source_tp and source_ct):
            r = rules.get(file_key)
        if r:
            if not r.get("enabled"):
                continue
            if not _ct_allowed_for(r, target_ct):
                continue
            if not _slepok_allowed_for(r, target_slepok):
                continue
        out.append(p)
    return out


def _prioritized_content_assets(paths: list, target_ct: str, *, source_segment: str, source_tp: str,
                                source_ct: str, target_slepok: str = "", source_slepok: str = "",
                                limit: int = 5) -> list:
    """Отфильтровать ассеты и поднять наверх выбранные «только этот ct».

    Если таких приоритетных ассетов больше limit, берём случайные limit штук.
    """
    if not paths:
        return []
    try:
        rules = _content_rules_map()
    except Exception:  # noqa: BLE001
        return list(dict.fromkeys(paths))[:limit]
    priority: list = []
    regular: list = []
    seen: set[str] = set()
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        file_key = _asset_key_from_local(p)
        manual_scope = _manual_rule_lookup_key(p, source_ct or target_ct)
        r = rules.get(_content_rule_key(*manual_scope)) if manual_scope else None
        r = r or rules.get(_content_rule_key(source_segment, source_tp, source_ct, file_key, source_slepok))
        if not r and source_slepok:
            r = rules.get(_content_rule_key(source_segment, source_tp, source_ct, file_key, ""))
        if r:
            if not r.get("enabled"):
                continue
            if not _ct_allowed_for(r, target_ct):
                continue
            if not _slepok_allowed_for(r, target_slepok):
                continue
            if _content_only_this_ct(r, target_ct):
                priority.append(p)
                continue
        regular.append(p)
    if len(priority) >= limit:
        return random.sample(priority, limit)
    return (priority + [p for p in regular if p not in priority])[:limit]


def _explicit_content_assets_for(target_ct: str, *, target_slepok: str = "",
                                 asset_types: set[str] | None = None, limit: int = 5) -> list:
    """Ассеты, явно разрешённые во вкладке «Контент» для другого/общего ct."""
    try:
        rules = _content_rules_map()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rules.values():
        if not r.get("enabled") or not _ct_allowed_for(r, target_ct):
            continue
        if not _slepok_allowed_for(r, target_slepok):
            continue
        if asset_types and str(r.get("asset_type") or "") not in asset_types:
            continue
        p = kp.fetch_remote_asset(r.get("asset_path") or "")
        if p and p not in out:
            out.append(p)
        if len(out) >= limit:
            break
    return out


def _ahash_distance(a: int, b: int) -> int:
    return bin(int(a) ^ int(b)).count("1")


def _dedupe_content_assets_for_ui(assets: list[dict], threshold: int = 18) -> tuple[list[dict], int]:
    """Быстрый дедуп для вкладки «Контент».

    Визуальный pHash требует скачать/декодировать каждую картинку с M3. На больших папках это
    блокирует первый экран, поэтому в интерактивном API скрываем только точные дубли по remote/token.
    Визуальную чистку дублей надо делать отдельной фоновой задачей, а не при каждом клике по ct.
    """
    kept: list[dict] = []
    seen: set[str] = set()
    hidden = 0
    for a in assets or []:
        key = str(a.get("remote") or a.get("original_asset_key") or a.get("asset_key") or a.get("token") or "")
        if key and key in seen:
            hidden += 1
            continue
        if key:
            seen.add(key)
        kept.append(a)
    return kept, hidden

"""Метрика (счётчики/цели из public.metrika_goals) + гео-справочник Директа —
вынесено из blueprint.py БЕЗ изменения логики (только перемещение + ре-экспорт).

Кластер: `_parse_counter_ids`, `_metrika_goals_for`, `_counter_foreign_owner`,
`_geo_load`, `_geo_id`, `_metrika_token`, `_goal_vse_formy`.

DI (инъектятся из blueprint через configure): `_victory_conn` (БД Victory, read),
`_direct_tokens` ({agency→token}), `_V5` (базовый URL Direct API v5). Кэш гео
(`_GEO_BY_NAME`/`_GEO_LOCK`) переезжает сюда. Инвариант wiring-hub: НЕ импортирует blueprint.
"""
from __future__ import annotations

import json
import sys
import threading

from . import campaign as cmc  # _find_secret_dir (loader path)


# ── DI: инъектятся из blueprint (заглушки-callable падают громко, если configure не отработал) ──
def _victory_conn():
    raise RuntimeError("blueprint_metrika._victory_conn не инъектирован (configure)")


def _direct_tokens() -> dict:
    raise RuntimeError("blueprint_metrika._direct_tokens не инъектирован (configure)")


_V5 = "https://api.direct.yandex.com/json/v5/"


def configure(deps: dict) -> None:
    """Инъекция зависимостей из blueprint."""
    globals().update(deps)


# ── Метрика: счётчики и цель «Все формы» ──────────────────────────────────────────
def _parse_counter_ids(text) -> list[int]:
    """'[103879503, 94543727]' → [103879503, 94543727]. Кривое/пустое → []."""
    if not text:
        return []
    try:
        arr = json.loads(text)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for x in (arr if isinstance(arr, list) else []):
        s = str(x).strip()
        if s.lstrip("-").isdigit():
            out.append(int(s))
    return out


def _metrika_goals_for(login: str):
    """Счётчики Метрики и цель «Все формы» из public.metrika_goals (внешняя таблица Victory).
    → {counters:[int,...], goal_id:int|None} либо None, если строки по логину нет."""
    if not login:
        return None
    conn = _victory_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT counter_ids, all_forms FROM public.metrika_goals "
                    "WHERE account_login=%s LIMIT 1", (login,))
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"counters": _parse_counter_ids(row[0]),
            "goal_id": int(row[1]) if row[1] is not None else None}


def _counter_foreign_owner(counter_id: int, login: str):
    """Если счётчик Метрики закреплён в public.metrika_goals за ДРУГИМ аккаунтом (не `login`) —
    вернуть логин-владельца, иначе None. Counter расшарен и на сам `login` → None (легитимно).
    Anti-footgun: ловит «вставили счётчик/цель от ДРУГОГО аккаунта» ДО трат M3 и campaigns.add."""
    if not counter_id:
        return None
    try:
        conn = _victory_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT account_login, counter_ids FROM public.metrika_goals "
                        "WHERE counter_ids LIKE %s", (f"%{int(counter_id)}%",))
            rows = cur.fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None
    owners = [lk for lk, ct in rows if int(counter_id) in _parse_counter_ids(ct)]  # точное вхождение
    if not owners or login in owners:      # ничей / принадлежит самому аккаунту → не блокируем
        return None
    return owners[0]                       # счётчик есть только у чужого аккаунта


# ── Гео-справочник Директа (GeoRegions) ───────────────────────────────────────────
_GEO_LOCK = threading.Lock()
_GEO_BY_NAME: dict = {}                       # lower(имя региона) → GeoRegionId (словарь Директа, кэш)


def _geo_load() -> dict:
    """Словарь GeoRegions Директа (имя→id), грузится один раз на процесс."""
    global _GEO_BY_NAME
    if _GEO_BY_NAME:
        return _GEO_BY_NAME
    with _GEO_LOCK:
        if _GEO_BY_NAME:
            return _GEO_BY_NAME
        import requests as _rqs
        tok = next(iter(_direct_tokens().values()), None)
        if not tok:
            return {}
        try:
            r = _rqs.post(_V5 + "dictionaries",
                          headers={"Authorization": "Bearer " + tok, "Accept-Language": "ru",
                                   "Content-Type": "application/json; charset=utf-8"},
                          json={"method": "get", "params": {"DictionaryNames": ["GeoRegions"]}}, timeout=60)
            geos = (r.json().get("result") or {}).get("GeoRegions", [])
        except Exception:  # noqa: BLE001
            return {}
        d: dict = {}
        for g in geos:                        # города идут раньше областей — приоритет точному совпадению
            nm = (g.get("GeoRegionName") or "").strip().lower()
            if nm and nm not in d:
                d[nm] = g.get("GeoRegionId")
        _GEO_BY_NAME = d
        return d


def _geo_id(city: str | None, region: str | None):
    """city → id (приоритет), иначе region → id. Возвращает (id, имя) или (None, None)."""
    d = _geo_load()
    for nm in (city, region):
        if nm:
            gid = d.get(nm.strip().lower())
            if gid:
                return gid, nm.strip()
    return None, None


def _metrika_token() -> str | None:
    sd = str(cmc._find_secret_dir())
    if sd not in sys.path:
        sys.path.insert(0, sd)
    from loader import load_yandex_metrika  # noqa: E402
    m = load_yandex_metrika()
    return m.get("oauth_token") if isinstance(m, dict) else None


def _goal_vse_formy(counter_id: int | None):
    """Цель «Все формы» счётчика Метрики → (goal_id, name) или (None, None)."""
    tok = _metrika_token()
    if not tok or not counter_id:
        return None, None
    import requests as _rqs
    try:
        r = _rqs.get(f"https://api-metrika.yandex.net/management/v1/counter/{counter_id}/goals",
                     headers={"Authorization": "OAuth " + tok}, timeout=30)
        if r.status_code != 200:
            return None, None
        for g in r.json().get("goals", []):
            if "все формы" in (g.get("name") or "").strip().lower():
                return g.get("id"), g.get("name")
    except Exception:  # noqa: BLE001
        return None, None
    return None, None

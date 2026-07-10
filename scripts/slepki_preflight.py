#!/usr/bin/env python3
"""Preflight-проверка slepki_structure.json перед деплоем.

DoD-правило (2026-07-10): слепок собирается ТОЛЬКО из реального корпуса директолога;
синтетические заглушки (одинаковый суперсет у разных слепков) ЗАПРЕЩЕНЫ. Корень бага
«все слепки дают одинаковую структуру» — байт-идентичные секции items у разных слепков.

Этот скрипт ловит регресс:
  1. CROSS-SLEPOK коллизии — секция (site_type, tp) с идентичным набором items у >1 РАЗНОГО слепка.
  2. Пустые группы/сплиты (items=[]) — дают пустые adgroup.
  3. Пустые tp, ОТКРЫТЫЕ в targeting_profile.json — риск пустой кампании.

Exit code 0 — чисто; 1 — есть нарушения (использовать в CI/перед деплоем).

Запуск: python3 scripts/check_slepki_collisions.py [путь_к_slepki_structure.json]
"""
from __future__ import annotations
import json
import sys
import collections
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def _sig(entry: dict) -> dict:
    """(site_type, tp_code) -> set of (c, t, gc) items."""
    sec: dict = collections.defaultdict(set)
    for s in entry.get("site_types", []):
        for t in s.get("tp", []):
            for cont in (t.get("groups") or []) + (t.get("splits") or []):
                for it in (cont.get("items") or []):
                    sec[(s["name"], t.get("code"))].add(
                        (it.get("c"), it.get("t"), it.get("gc"))
                    )
    return sec


def check(struct_path: Path, profile_path: Path | None = None) -> int:
    st = json.loads(struct_path.read_text(encoding="utf-8"))
    dl = st["directologists"]
    profile = {}
    if profile_path and profile_path.exists():
        profile = json.loads(profile_path.read_text(encoding="utf-8"))

    # 1. cross-slepok коллизии
    fp: dict = collections.defaultdict(set)
    for e in dl:
        for key, items in _sig(e).items():
            if items:
                fp[frozenset(items)].add((e["key"],) + key)
    cross = [v for v in fp.values() if len({x[0] for x in v}) > 1]

    # 2. пустые группы/сплиты
    empty_groups = [
        (e["key"], s["name"], t.get("code"))
        for e in dl
        for s in e.get("site_types", [])
        for t in s.get("tp", [])
        for c in ((t.get("groups") or []) + (t.get("splits") or []))
        if not (c.get("items") or [])
    ]

    # 3. пустые tp, открытые в профиле
    def in_profile(k, site, code):
        return code in profile.get(k, {}).get(site, {})

    empty_tp_in_profile = [
        f"{e['key']}/{s['name']}/{t.get('code')}"
        for e in dl
        for s in e.get("site_types", [])
        for t in s.get("tp", [])
        if sum(len(c.get("items") or []) for c in ((t.get("groups") or []) + (t.get("splits") or []))) == 0
        and in_profile(e["key"], s["name"], t.get("code"))
    ]

    ok = True
    print(f"Слепков: {len(dl)}")
    if cross:
        ok = False
        print(f"❌ CROSS-SLEPOK коллизий: {len(cross)} (синтетические дубли — ЗАПРЕЩЕНЫ)")
        for grp in cross:
            print("   ", sorted(grp))
    else:
        print("✅ cross-slepok коллизий: 0")

    if empty_groups:
        ok = False
        print(f"❌ пустых групп/сплитов: {len(empty_groups)}")
        for x in empty_groups[:20]:
            print("   ", x)
    else:
        print("✅ пустых групп/сплитов: 0")

    if empty_tp_in_profile:
        ok = False
        print(f"❌ пустых tp В ПРОФИЛЕ (риск пустой кампании): {len(empty_tp_in_profile)}")
        for x in empty_tp_in_profile:
            print("   ", x)
    else:
        print("✅ пустых tp в профиле: 0")

    return 0 if ok else 1


if __name__ == "__main__":
    struct = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "slepki_structure.json"
    prof = HERE / "targeting_profile.json"
    sys.exit(check(struct, prof))

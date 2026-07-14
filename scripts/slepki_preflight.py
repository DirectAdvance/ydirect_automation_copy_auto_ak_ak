#!/usr/bin/env python3
"""Preflight-проверка slepki_structure.json перед деплоем.

DoD-правило (2026-07-10): слепок собирается ТОЛЬКО из реального корпуса директолога;
синтетические заглушки (одинаковый суперсет у разных слепков) ЗАПРЕЩЕНЫ. Корень бага
«все слепки дают одинаковую структуру» — байт-идентичные секции items у разных слепков.

Этот скрипт ловит регресс:
  1. CROSS-SLEPOK коллизии — секция (site_type, tp) с идентичным набором items у >1 РАЗНОГО слепка.
  2. Пустые tp / группы / сплиты — дают пустые кампании или adgroup.
  3. Невалидные gc-кодеры — ломают aon/aoff-редактор и ct/segment маппинг.
  4. Пустые tp, ОТКРЫТЫЕ в targeting_profile.json — риск пустой кампании.
  5. Архивные source_manifest: ссылка/схема/уникальность кампаний/сводные числа/gc.

Exit code 0 — чисто; 1 — есть нарушения (использовать в CI/перед деплоем).

Запуск: python3 scripts/check_slepki_collisions.py [путь_к_slepki_structure.json]
"""
from __future__ import annotations
import json
import sys
import collections
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
GC_RE = re.compile(r"^ct\d{4}_(aon|aoff)_n\d{3}_r\d{4}_ct\d{3}_ag\d{3}_g\d{2}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _all_groups(t: dict) -> list:
    """Все группы tp: прямые t.groups + вложенные t.splits[].groups."""
    grps = list(t.get("groups") or [])
    for sp in (t.get("splits") or []):
        grps.extend(sp.get("groups") or [])
    return grps


def _sig(entry: dict) -> dict:
    """(site_type, tp_code) -> set of (c, t, gc) items."""
    sec: dict = collections.defaultdict(set)
    for s in entry.get("site_types", []):
        for t in s.get("tp", []):
            for grp in _all_groups(t):
                for it in (grp.get("items") or []):
                    sec[(s["name"], t.get("code"))].add(
                        (it.get("c"), it.get("t"), it.get("gc"))
                    )
    return sec


def preflight_dict(struct: dict, profile: dict | None = None) -> list[str]:
    """Чистая (без диска) preflight-проверка ПРЕДЛАГАЕМОГО состояния структуры/профиля.

    Возвращает список нарушений (пустой = чисто). Используется редактором слепков
    (direct/slepki_editor.py) ДО записи: если список непуст — правку НЕ применяем.
    Ловит те же классы регресса, что и файловый `check`:
      1. cross-slepok коллизии (байт-идентичные items у >1 РАЗНОГО слепка);
      2. пустые tp / группы / сплиты;
      3. невалидные gc-кодеры;
      4. пустые tp, ОТКРЫТЫЕ в профиле (риск пустой кампании).
    """
    profile = profile or {}
    dl = struct.get("directologists", []) or []
    out: list[str] = []

    # 1. cross-slepok коллизии
    fp: dict = collections.defaultdict(set)
    for e in dl:
        for key, items in _sig(e).items():
            if items:
                fp[frozenset(items)].add((e.get("key"),) + key)
    for v in fp.values():
        if len({x[0] for x in v}) > 1:
            out.append("CROSS_SLEPOK_COLLISION: " + "; ".join(
                f"{x[0]}/{x[1]}/{x[2]}" for x in sorted(v)))

    # 2. пустые tp и группы/сплиты
    for e in dl:
        manifest_name = e.get("source_manifest")
        if manifest_name and (Path(str(manifest_name)).is_absolute() or Path(str(manifest_name)).name != manifest_name):
            out.append(f"BAD_SOURCE_MANIFEST_PATH: {e.get('key')}/{manifest_name}")
        for s in e.get("site_types", []):
            for t in s.get("tp", []):
                groups = _all_groups(t)
                if not groups:
                    out.append(f"EMPTY_TP: {e.get('key')}/{s.get('name')}/{t.get('code')}")
                for c in groups:
                    if not (c.get("items") or []):
                        out.append(
                            f"EMPTY_GROUP: {e.get('key')}/{s.get('name')}/{t.get('code')}"
                            f"/{c.get('name') or c.get('label') or c.get('sq') or '?'}")
                    for it in (c.get("items") or []):
                        if isinstance(it, dict) and it.get("gc") and not GC_RE.match(str(it.get("gc") or "")):
                            out.append(
                                f"BAD_GC: {e.get('key')}/{s.get('name')}/{t.get('code')}"
                                f"/{it.get('t') or '?'}: {it.get('gc')}")

    # 3. пустые tp, открытые в профиле
    def _in_profile(k, site, code):
        return code in profile.get(k, {}).get(site, {})

    for e in dl:
        for s in e.get("site_types", []):
            for t in s.get("tp", []):
                n = sum(len(c.get("items") or []) for c in _all_groups(t))
                if n == 0 and _in_profile(e.get("key"), s.get("name"), t.get("code")):
                    out.append(f"EMPTY_TP_IN_PROFILE: {e.get('key')}/{s.get('name')}/{t.get('code')}")
    return out


def _source_manifest_errors(struct: dict, base_dir: Path) -> list[str]:
    """Validate deployable archive-derived manifests without needing the source XLSX files."""
    errors: list[str] = []
    for directologist in struct.get("directologists", []) or []:
        key = directologist.get("key") or "?"
        manifest_name = directologist.get("source_manifest")
        if not manifest_name:
            continue
        path = base_dir / str(manifest_name)
        if not path.is_file():
            errors.append(f"SOURCE_MANIFEST_MISSING: {key}/{manifest_name}")
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"SOURCE_MANIFEST_BAD_JSON: {key}/{manifest_name}: {exc}")
            continue
        prefix = f"{key}/{manifest_name}"
        if manifest.get("schema") != 1 or manifest.get("slepok") != key:
            errors.append(f"SOURCE_MANIFEST_IDENTITY: {prefix}")
        if Path(str(manifest.get("source") or "")).is_absolute():
            errors.append(f"SOURCE_MANIFEST_ABSOLUTE_SOURCE: {prefix}")
        site_names = {site.get("name") for site in directologist.get("site_types", [])}
        if manifest.get("site_type") not in site_names:
            errors.append(f"SOURCE_MANIFEST_SITE_TYPE: {prefix}/{manifest.get('site_type')}")
        campaigns = manifest.get("campaigns") or []
        slugs = [str(item.get("slug") or "") for item in campaigns]
        ids = [item.get("source_campaign_id") for item in campaigns]
        if not campaigns or "" in slugs or len(set(slugs)) != len(slugs):
            errors.append(f"SOURCE_MANIFEST_CAMPAIGNS: {prefix}/duplicate-or-empty-slug")
        if None in ids or len(set(ids)) != len(ids):
            errors.append(f"SOURCE_MANIFEST_CAMPAIGNS: {prefix}/duplicate-or-empty-id")
        for campaign in campaigns:
            cp = f"{prefix}/{campaign.get('slug') or '?'}"
            groups = campaign.get("groups") or []
            totals = campaign.get("source_totals") or {}
            if not groups or totals.get("groups") != len(groups):
                errors.append(f"SOURCE_MANIFEST_GROUP_COUNT: {cp}")
            computed = {
                "autotarget_groups": sum(bool(group.get("autotarget")) for group in groups),
                "keywords": sum(len(group.get("keywords") or []) for group in groups),
                "unique_keywords": len({kw for group in groups for kw in (group.get("keywords") or [])}),
                "ads": sum(len(group.get("ads") or []) for group in groups),
            }
            for field, value in computed.items():
                if totals.get(field) != value:
                    errors.append(f"SOURCE_MANIFEST_TOTAL: {cp}/{field}={totals.get(field)}!={value}")
            if not SHA256_RE.match(str(campaign.get("source_sha256") or "")):
                errors.append(f"SOURCE_MANIFEST_SHA256: {cp}")
            if campaign.get("source_ready") and campaign.get("missing"):
                errors.append(f"SOURCE_MANIFEST_READY_WITH_MISSING: {cp}")
            names = [str(group.get("name") or "").casefold() for group in groups]
            if "" in names or len(set(names)) != len(names):
                errors.append(f"SOURCE_MANIFEST_DUPLICATE_GROUP: {cp}")
            for group in groups:
                gc = str(group.get("gc") or "")
                if not gc or not GC_RE.match(gc):
                    errors.append(f"SOURCE_MANIFEST_BAD_GC: {cp}/{group.get('name') or '?'}:{gc}")
    return errors


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

    # 1b. cross-site_type дубли ВНУТРИ одного слепка (слепой угол существующей
    # cross-slepok проверки: она сравнивает только РАЗНЫЕ слепки, len({key})>1,
    # и не видит байт-идентичные секции между site_type ОДНОГО директолога).
    # Только детектор/отчёт — НЕ блокирует деплой (намеренность решает Семён).
    same_slepok_cross_site: list = []
    for e in dl:
        buckets: dict = collections.defaultdict(list)
        for (site, code), items in _sig(e).items():
            if items:
                buckets[frozenset(items)].append((site, code))
        for occ in buckets.values():
            if len({o[0] for o in occ}) > 1:
                same_slepok_cross_site.append((e["key"], sorted(occ)))

    # 2. пустые tp и группы/сплиты
    empty_tp = [
        (e["key"], s["name"], t.get("code"))
        for e in dl
        for s in e.get("site_types", [])
        for t in s.get("tp", [])
        if not _all_groups(t)
    ]
    empty_groups = [
        (e["key"], s["name"], t.get("code"))
        for e in dl
        for s in e.get("site_types", [])
        for t in s.get("tp", [])
        for c in _all_groups(t)
        if not (c.get("items") or [])
    ]
    bad_gc = [
        (e["key"], s["name"], t.get("code"), it.get("t"), it.get("gc"))
        for e in dl
        for s in e.get("site_types", [])
        for t in s.get("tp", [])
        for c in _all_groups(t)
        for it in (c.get("items") or [])
        if isinstance(it, dict) and it.get("gc") and not GC_RE.match(str(it.get("gc") or ""))
    ]
    duplicate_items = []
    for e in dl:
        for s in e.get("site_types", []):
            for t in s.get("tp", []):
                counts = collections.Counter(
                    (it.get("c"), it.get("t"), it.get("gc"))
                    for group in _all_groups(t)
                    for it in (group.get("items") or [])
                    if isinstance(it, dict)
                )
                duplicate_items.extend(
                    (e.get("key"), s.get("name"), t.get("code"), signature, count)
                    for signature, count in counts.items() if count > 1
                )
    source_manifest_errors = _source_manifest_errors(st, struct_path.parent)

    # 3. пустые tp, открытые в профиле
    def in_profile(k, site, code):
        return code in profile.get(k, {}).get(site, {})

    empty_tp_in_profile = [
        f"{e['key']}/{s['name']}/{t.get('code')}"
        for e in dl
        for s in e.get("site_types", [])
        for t in s.get("tp", [])
        if sum(len(c.get("items") or []) for c in _all_groups(t)) == 0
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

    if same_slepok_cross_site:
        extra_copies = sum(len(occ) - 1 for _key, occ in same_slepok_cross_site)
        print(f"⚠ cross-site_type дублей структуры ВНУТРИ слепка: "
              f"{len(same_slepok_cross_site)} групп / {extra_copies} лишних копий "
              f"(намеренность решает Семён; не блокируют деплой)")
        for key, occ in same_slepok_cross_site:
            print("   ", f"{key}: " + ", ".join(f"{s}/{t}" for s, t in occ))
    else:
        print("✅ cross-site_type дублей структуры внутри слепка: 0")

    if empty_tp:
        ok = False
        print(f"❌ пустых tp: {len(empty_tp)}")
        for x in empty_tp[:30]:
            print("   ", x)
    else:
        print("✅ пустых tp: 0")

    if empty_groups:
        ok = False
        print(f"❌ пустых групп/сплитов: {len(empty_groups)}")
        for x in empty_groups[:20]:
            print("   ", x)
    else:
        print("✅ пустых групп/сплитов: 0")

    if bad_gc:
        ok = False
        print(f"❌ невалидных gc: {len(bad_gc)}")
        for x in bad_gc[:30]:
            print("   ", x)
    else:
        print("✅ невалидных gc: 0")

    if empty_tp_in_profile:
        ok = False
        print(f"❌ пустых tp В ПРОФИЛЕ (риск пустой кампании): {len(empty_tp_in_profile)}")
        for x in empty_tp_in_profile:
            print("   ", x)
    else:
        print("✅ пустых tp в профиле: 0")

    if source_manifest_errors:
        ok = False
        print(f"❌ ошибок source manifest: {len(source_manifest_errors)}")
        for item in source_manifest_errors[:40]:
            print("   ", item)
    else:
        referenced = sum(bool(item.get("source_manifest")) for item in dl)
        print(f"✅ source manifest: {referenced} ссылок, ошибок 0")

    # Historical structures contain a few exact repeats across category groups. They are
    # review warnings rather than a deploy blocker: dmp intentionally repeats the same item
    # in payment splits, while automotive repeats need source evidence before deletion.
    if duplicate_items:
        extra = sum(count - 1 for *_head, count in duplicate_items)
        print(f"⚠ точных повторов items: {extra} (не блокируют; требуют сверки с источником)")
        for key, site, tp, signature, count in duplicate_items[:30]:
            print("   ", f"{key}/{site}/{tp} ×{count}: {signature[1]} [{signature[2]}]")
    else:
        print("✅ точных повторов items: 0")

    return 0 if ok else 1


if __name__ == "__main__":
    struct = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "slepki_structure.json"
    prof = HERE / "targeting_profile.json"
    sys.exit(check(struct, prof))

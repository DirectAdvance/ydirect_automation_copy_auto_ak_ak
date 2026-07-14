"""
build_slepok_structure.py — Генератор уникальных секций слепков из корпуса директологов.

Решает баг «все слепки дают одинаковую структуру»: у разных директологов секции items
были байт-в-байт идентичны (синтетический суперсет).

Алгоритм
--------
1.  Загружает slepki_structure.json (входящий файл — используется как суперсет-источник
    для group-структуры и как база для staging).
2.  Для каждого целевого слепка сканирует корпус С РАЗБИВКОЙ ПО site_type
    (site_type login-папки берётся из _logins.json → поле "type"):
      - Извлекает ct4-коды (первые 6 символов имени группы: ct####) → corpus_ct4_by_st
        ({site_type: set}).
      - Извлекает tp-коды из campaigns.jsonl → used_tps_by_st ({site_type: set строк
        типа 'tp1', 'tp2'…}).
3.  Для каждой секции site_type → tp → group → items применяет фильтр, используя наборы
    ИМЕННО этого site_type (чтобы бренды/tp разных site_type не смешивались):
      - ct0000 → всегда оставляем (общие/фид группы, не привязаны к бренду).
      - tp.code NOT IN used_tps[site_type] → все items группы обнуляются (директолог этот
        tp в этом site_type не ведёт).
      - иначе → item остаётся если ct4(item) ∈ corpus_ct4[site_type].
4.  Пересобирает раздел для каждого целевого слепка; остальные — без изменений.

Порог присутствия: ≥1 вхождение ct4-кода в adgroups.jsonl хотя бы одного login'а.
Минимальный порог выбран осознанно — единичные тесты/мусорные группы получают ≥1 и
включаются, но это безопаснее, чем потерять реальный бренд с малым числом групп.

Как извлечь ct4 из item
-----------------------
- Items с gc-полем (tp1–tp5):  ct4 = gc[:6]   ('ct0019_...' → 'ct0019')
- Items без gc (tp6, tp7):     ct4 ищем regex в c-поле  ('tp6_..._ct0000_...' → 'ct0000')

Использование
-------------
    python build_slepok_structure.py \\
        --corpus /path/to/slepki_direktologov/corpus \\
        --slepki /path/to/slepki_structure.json \\
        --slugs scherbakova:corpus/scherbakova_natalya \\
                terehov:corpus/terehov_evgenii \\
                pavlov:corpus/pavlov_aleksei \\
                kryuchkova:corpus/kryuchkova_elizaveta \\
        --output /path/to/slepki_structure.staging.json

    Или через API: см. функцию build_staging().
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Corpus scanning
# ---------------------------------------------------------------------------

_CT4_RE = re.compile(r'^(ct\d{4})_')
_CT4_IN_C_RE = re.compile(r'_(ct\d{4})_')
_TP_RE = re.compile(r'^(tp\d+)_')


# site_type для login-папки, отсутствующей в _logins.json (site_type неизвестен).
# Такие логины складываются в общий бакет и через _st_sets подмешиваются во ВСЕ
# site_type — чтобы не терять данные; при этом бренды разных ИЗВЕСТНЫХ site_type
# между собой НЕ смешиваются (в этом и был баг единого flat-set).
_UNKNOWN_ST = ""


def scan_corpus(corpus_dir: Path) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Сканирует login-папки директолога С РАЗБИВКОЙ ПО site_type.

    Возвращает (used_tps_by_st, corpus_ct4_by_st):
      used_tps_by_st   — {site_type: set(tp-кодов 'tp1'..'tp8', используемых
                         в кампаниях логинов этого site_type)}.
      corpus_ct4_by_st — {site_type: set(ct4-кодов 'ct0000'..'ct9999' из adgroups
                         логинов этого site_type; порог ≥1 = само присутствие)}.

    site_type определяется по login-папке из _logins.json (поле "type"). Логины без
    записи (site_type неизвестен) попадают в бакет _UNKNOWN_ST — см. _st_sets.

    Ранее ct4/tp собирались в один плоский set на всего директолога, из-за чего при
    пересборке бренды одного site_type (напр. «С пробегом») протекали в секции
    другого («Мультибренд»). Разбивка по site_type это устраняет.
    """
    # login → site_type из _logins.json
    login_site_type: dict[str, str] = {}
    logins_file = corpus_dir / "_logins.json"
    if logins_file.exists():
        try:
            for entry in json.loads(logins_file.read_text(encoding="utf-8")):
                login = entry.get("login")
                st = entry.get("type")
                if login and st:
                    login_site_type[login] = st
        except (json.JSONDecodeError, OSError):
            pass

    used_tps_by_st: dict[str, set[str]] = defaultdict(set)
    ct4_by_st: dict[str, set[str]] = defaultdict(set)

    for login_dir in corpus_dir.iterdir():
        if login_dir.name.startswith("_") or not login_dir.is_dir():
            continue
        site_type = login_site_type.get(login_dir.name, _UNKNOWN_ST)

        # campaigns.jsonl → tp-коды этого site_type
        camp_file = login_dir / "campaigns.jsonl"
        if camp_file.exists():
            with camp_file.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    name = obj.get("Name", "")
                    m = _TP_RE.match(name)
                    if m:
                        used_tps_by_st[site_type].add(m.group(1))

        # adgroups.jsonl → ct4-коды этого site_type
        ag_file = login_dir / "adgroups.jsonl"
        if ag_file.exists():
            with ag_file.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    name = obj.get("Name", "")
                    m = _CT4_RE.match(name)
                    if m:
                        ct4_by_st[site_type].add(m.group(1))

    return dict(used_tps_by_st), dict(ct4_by_st)


def _st_sets(by_st: dict[str, set[str]], site_type: str) -> set[str]:
    """Множество кодов конкретного site_type ∪ общий бакет неизвестных логинов."""
    return by_st.get(site_type, set()) | by_st.get(_UNKNOWN_ST, set())


# ---------------------------------------------------------------------------
# Item ct4 extraction
# ---------------------------------------------------------------------------

def item_ct4(item: dict) -> Optional[str]:
    """Извлекает ct4-код из item'а слепка.

    Для items с gc-полем (tp1–tp5): первые 6 символов gc.
    Для items без gc (tp6, tp7): ищет ct#### внутри c-поля.
    """
    gc = item.get("gc", "")
    if gc:
        m = _CT4_RE.match(gc)
        return m.group(1) if m else None
    # tp6/tp7 — ct вшит в c
    c = item.get("c", "")
    m = _CT4_IN_C_RE.search(c)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Item filtering
# ---------------------------------------------------------------------------

def filter_items(
    items: list[dict],
    tp_code: str,
    used_tps: set[str],
    corpus_ct4: set[str],
) -> list[dict]:
    """Фильтрует items одной группы для конкретного директолога.

    Правила:
    1. Если tp_code не входит в used_tps → пустой список (директолог не ведёт этот tp).
    2. item с ct4 == 'ct0000' → всегда оставляем (общие/фид элементы).
    3. иначе → оставляем только если ct4 ∈ corpus_ct4.
    """
    if tp_code not in used_tps:
        return []
    result = []
    for item in items:
        ct4 = item_ct4(item)
        if ct4 is None:
            # Не можем определить ct4 → оставляем (безопаснее)
            result.append(item)
        elif ct4 == "ct0000":
            result.append(item)
        elif ct4 in corpus_ct4:
            result.append(item)
    return result


# ---------------------------------------------------------------------------
# Section rebuild for one directologist
# ---------------------------------------------------------------------------

def rebuild_directologist_section(
    directologist: dict,
    used_tps_by_st: dict[str, set[str]],
    corpus_ct4_by_st: dict[str, set[str]],
) -> dict:
    """Перестраивает секцию одного директолога, фильтруя items в каждой группе.

    used_tps_by_st / corpus_ct4_by_st — словари {site_type: set(...)} из scan_corpus.
    Для каждой секции берётся набор кодов ИМЕННО её site_type (через _st_sets), так
    что бренды/tp одного site_type не протекают в секции другого.

    Возвращает новый dict директолога (глубокая копия с отфильтрованными items).
    """
    result = {
        "key": directologist["key"],
        "name": directologist["name"],
        "site_types": [],
    }
    for site_type in directologist.get("site_types", []):
        st_name = site_type["name"]
        st_used_tps = _st_sets(used_tps_by_st, st_name)
        st_corpus_ct4 = _st_sets(corpus_ct4_by_st, st_name)
        new_st: dict = {
            "name": st_name,
            "tp": [],
        }
        for tp in site_type.get("tp", []):
            tp_code = tp["code"]
            new_tp: dict = {
                "code": tp_code,
                "title": tp.get("title", ""),
            }
            # Копируем дополнительные поля tp (кроме groups/splits)
            for k, v in tp.items():
                if k not in ("code", "title", "groups", "splits"):
                    new_tp[k] = v

            groups_key = "groups" if "groups" in tp else "splits"
            new_groups = []
            for group in tp.get(groups_key, []):
                filtered = filter_items(
                    group.get("items", []),
                    tp_code,
                    st_used_tps,
                    st_corpus_ct4,
                )
                new_group = {k: v for k, v in group.items() if k != "items"}
                new_group["items"] = filtered
                new_groups.append(new_group)

            new_tp[groups_key] = new_groups
            new_st["tp"].append(new_tp)

        result["site_types"].append(new_st)

    return result


# ---------------------------------------------------------------------------
# Collision analysis helpers
# ---------------------------------------------------------------------------

def tp_item_fingerprint(tp_obj: dict) -> frozenset:
    """Fingerprint (frozenset) items одного tp для сравнения между директологами."""
    groups_key = "groups" if "groups" in tp_obj else "splits"
    items: list[tuple] = []
    for g in tp_obj.get(groups_key, []):
        for item in g.get("items", []):
            items.append((item.get("c",""), item.get("t",""), item.get("gc","")))
    return frozenset(items)


def count_collisions(
    directologists: list[dict],
    target_keys: set[str],
) -> dict[str, int]:
    """Для каждого (site_type_name, tp_code) считает, сколько пар directologist
    имеют одинаковый fingerprint среди target_keys.

    Возвращает dict {f'{st_name}/{tp_code}': collision_pairs_count}.
    """
    # Собираем fingerprints {skey: {dir_key: fingerprint}}
    skey_dir_fp: dict[str, dict[str, frozenset]] = {}

    for d in directologists:
        if d["key"] not in target_keys:
            continue
        for st in d.get("site_types", []):
            for tp in st.get("tp", []):
                skey = f"{st['name']}/{tp['code']}"
                if skey not in skey_dir_fp:
                    skey_dir_fp[skey] = {}
                skey_dir_fp[skey][d["key"]] = tp_item_fingerprint(tp)

    collisions: dict[str, int] = {}
    for skey, dir_fps in skey_dir_fp.items():
        fp_list = list(dir_fps.values())
        pairs = 0
        for i in range(len(fp_list)):
            for j in range(i + 1, len(fp_list)):
                if fp_list[i] == fp_list[j] and len(fp_list[i]) > 0:
                    pairs += 1
        collisions[skey] = pairs

    return collisions


# ---------------------------------------------------------------------------
# Main build function (API-style)
# ---------------------------------------------------------------------------

def build_staging(
    slepki_path: Path,
    corpus_base: Path,
    slug_to_corpus: dict[str, str],  # {slepok_key → corpus subdirectory name}
    output_path: Path,
    report_path: Optional[Path] = None,
) -> dict:
    """Строит staging-версию slepki_structure.json с уникальными секциями для
    целевых слепков.

    Параметры
    ---------
    slepki_path     : Путь к текущему slepki_structure.json (суперсет-источник).
    corpus_base     : Корень папки corpus.
    slug_to_corpus  : Маппинг {slepok_key: имя_папки_в_corpus}.
    output_path     : Куда сохранить staging-версию (НЕ перезаписывает slepki_path).
    report_path     : Если задан — туда пишем отчёт в Markdown.

    Возвращает: dict со статистикой (для логгирования/отчёта).
    """
    print(f"[build_staging] Загружаем {slepki_path}...", file=sys.stderr)
    with slepki_path.open(encoding="utf-8") as f:
        data = json.load(f)

    directologists_orig: list[dict] = data["directologists"]
    target_keys = set(slug_to_corpus.keys())

    # --- Сканируем корпус для каждого целевого слепка ---
    corpus_info: dict[str, dict] = {}  # key → {used_tps, corpus_ct4, ct4_counter}
    for slepok_key, corpus_subdir in slug_to_corpus.items():
        corpus_dir = corpus_base / corpus_subdir
        if not corpus_dir.exists():
            print(f"[WARN] Корпус не найден: {corpus_dir}", file=sys.stderr)
            corpus_info[slepok_key] = {
                "used_tps": set(),
                "corpus_ct4": set(),
                "used_tps_by_st": {},
                "corpus_ct4_by_st": {},
                "ct4_counter": Counter(),
            }
            continue
        print(f"[build_staging] Сканируем корпус {slepok_key} ({corpus_dir.name})...", file=sys.stderr)
        # Для отчёта нам нужен counter, поэтому продублируем логику
        used_tps_by_st, corpus_ct4_by_st = scan_corpus(corpus_dir)
        # Плоские агрегаты (объединение по site_type) — только для логов/отчёта;
        # фильтрация при пересборке идёт по site_type (см. rebuild_directologist_section).
        used_tps = set().union(*used_tps_by_st.values()) if used_tps_by_st else set()
        corpus_ct4 = set().union(*corpus_ct4_by_st.values()) if corpus_ct4_by_st else set()
        # Пересканируем для counter
        ct4_counter: Counter[str] = Counter()
        for login_dir in corpus_dir.iterdir():
            if login_dir.name.startswith("_") or not login_dir.is_dir():
                continue
            ag_file = login_dir / "adgroups.jsonl"
            if ag_file.exists():
                with ag_file.open(encoding="utf-8") as f2:
                    for line in f2:
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        name = obj.get("Name", "")
                        m = _CT4_RE.match(name)
                        if m:
                            ct4_counter[m.group(1)] += 1
        corpus_info[slepok_key] = {
            "used_tps": used_tps,
            "corpus_ct4": corpus_ct4,
            "used_tps_by_st": used_tps_by_st,
            "corpus_ct4_by_st": corpus_ct4_by_st,
            "ct4_counter": ct4_counter,
        }
        print(f"  → used_tps={sorted(used_tps)}, corpus_ct4={len(corpus_ct4)} кодов", file=sys.stderr)

    # --- Считаем коллизии ДО пересборки ---
    collisions_before = count_collisions(directologists_orig, target_keys)

    # --- Пересобираем секции целевых директологов ---
    new_directologists = []
    rebuild_stats: dict[str, dict] = {}

    for d in directologists_orig:
        key = d["key"]
        if key not in target_keys:
            new_directologists.append(d)
            continue

        info = corpus_info[key]
        used_tps = info["used_tps"]          # плоский агрегат — только для rebuild_stats/отчёта
        corpus_ct4 = info["corpus_ct4"]      # плоский агрегат — только для rebuild_stats/отчёта
        used_tps_by_st = info["used_tps_by_st"]
        corpus_ct4_by_st = info["corpus_ct4_by_st"]

        # Считаем суперсет (items ДО)
        superset_counts: dict[str, int] = {}  # skey → count_before
        for st in d.get("site_types", []):
            for tp in st.get("tp", []):
                skey = f"{st['name']}/{tp['code']}"
                groups_key = "groups" if "groups" in tp else "splits"
                total = sum(len(g.get("items", [])) for g in tp.get(groups_key, []))
                superset_counts[skey] = total

        new_d = rebuild_directologist_section(d, used_tps_by_st, corpus_ct4_by_st)
        new_directologists.append(new_d)

        # Считаем items ПОСЛЕ
        after_counts: dict[str, int] = {}
        for st in new_d.get("site_types", []):
            for tp in st.get("tp", []):
                skey = f"{st['name']}/{tp['code']}"
                groups_key = "groups" if "groups" in tp else "splits"
                total = sum(len(g.get("items", [])) for g in tp.get(groups_key, []))
                after_counts[skey] = total

        rebuild_stats[key] = {
            "superset_counts": superset_counts,
            "after_counts": after_counts,
            "used_tps": sorted(used_tps),
            "corpus_ct4_count": len(corpus_ct4),
        }

    # --- Считаем коллизии ПОСЛЕ пересборки ---
    collisions_after = count_collisions(new_directologists, target_keys)

    # --- Сохраняем staging ---
    staging_data = {"directologists": new_directologists}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(staging_data, f, ensure_ascii=False, indent=2)
    print(f"[build_staging] Staging сохранён: {output_path}", file=sys.stderr)

    # --- Собираем итоговую статистику ---
    stats = {
        "target_keys": sorted(target_keys),
        "corpus_info": {
            k: {"used_tps": sorted(v["used_tps"]), "corpus_ct4_count": len(v["corpus_ct4"])}
            for k, v in corpus_info.items()
        },
        "rebuild_stats": rebuild_stats,
        "collisions_before": collisions_before,
        "collisions_after": collisions_after,
    }

    # --- Генерируем отчёт ---
    if report_path:
        _write_report(report_path, stats, corpus_info, new_directologists, target_keys, directologists_orig)

    return stats


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _write_report(
    report_path: Path,
    stats: dict,
    corpus_info: dict,
    new_directologists: list[dict],
    target_keys: set[str],
    orig_directologists: list[dict],
) -> None:
    lines = ["# Phase 1 Rebuild Report\n"]
    lines.append(f"Дата: 2026-07-10\n")
    lines.append(f"Целевые слепки: {', '.join(sorted(target_keys))}\n")

    # 1. Корпус
    lines.append("\n## 1. Корпус директологов\n")
    lines.append("| Слепок | TPs в корпусе | Уникальных ct4 |\n")
    lines.append("|---|---|---|\n")
    for key in sorted(target_keys):
        info = corpus_info[key]
        lines.append(f"| `{key}` | {', '.join(sorted(info['used_tps']))} | {len(info['corpus_ct4'])} |\n")

    # 2. Статистика пересборки (до/после)
    lines.append("\n## 2. Items до/после по каждому слепку\n")
    for key in sorted(target_keys):
        rst = stats["rebuild_stats"].get(key, {})
        sc = rst.get("superset_counts", {})
        ac = rst.get("after_counts", {})
        lines.append(f"\n### {key}\n")
        lines.append("| Секция | Items ДО (суперсет) | Items ПОСЛЕ | Вырезано |\n")
        lines.append("|---|---:|---:|---:|\n")
        total_before = total_after = 0
        for skey in sorted(set(sc) | set(ac)):
            b = sc.get(skey, 0)
            a = ac.get(skey, 0)
            cut = b - a
            total_before += b
            total_after += a
            lines.append(f"| `{skey}` | {b} | {a} | {cut} |\n")
        lines.append(f"| **ИТОГО** | **{total_before}** | **{total_after}** | **{total_before - total_after}** |\n")

    # 3. Топ-бренды из корпуса
    lines.append("\n## 3. Топ-20 ct4-кодов по корпусу\n")
    for key in sorted(target_keys):
        info = corpus_info.get(key, {})
        ct4_counter = info.get("ct4_counter", Counter())
        lines.append(f"\n### {key}\n")
        lines.append("| ct4 | Кол-во групп |\n|---|---:|\n")
        for code, cnt in ct4_counter.most_common(20):
            lines.append(f"| `{code}` | {cnt} |\n")

    # 4. Хеш-коллизии до/после
    lines.append("\n## 4. Хеш-коллизии (пары слепков с идентичным набором items)\n")
    lines.append("\n### До пересборки (среди 4 целевых слепков)\n")
    collision_pairs_before = sum(1 for v in stats["collisions_before"].values() if v > 0)
    lines.append(f"Секций с коллизиями (хотя бы 1 пара): **{collision_pairs_before}**\n\n")
    for skey, pairs in sorted(stats["collisions_before"].items()):
        if pairs > 0:
            lines.append(f"- `{skey}`: {pairs} пар с одинаковым fingerprint\n")

    lines.append("\n### После пересборки\n")
    collision_pairs_after = sum(1 for v in stats["collisions_after"].values() if v > 0)
    lines.append(f"Секций с коллизиями: **{collision_pairs_after}**\n\n")
    for skey, pairs in sorted(stats["collisions_after"].items()):
        if pairs > 0:
            lines.append(f"- `{skey}`: {pairs} пар (требует проверки)\n")

    # 5. Diff пример: terehov Мультибренд/tp2
    lines.append("\n## 5. Пример diff: terehov Мультибренд/tp2\n")
    # Найдём terehov в оригинале и новом
    orig_t = next((d for d in orig_directologists if d["key"] == "terehov"), None)
    new_t = next((d for d in new_directologists if d["key"] == "terehov"), None)
    if orig_t and new_t:
        def get_tp2_items(d_obj: dict) -> list[dict]:
            for st in d_obj.get("site_types", []):
                if st["name"] == "Мультибренд":
                    for tp in st.get("tp", []):
                        if tp["code"] == "tp2":
                            groups_key = "groups" if "groups" in tp else "splits"
                            items = []
                            for g in tp.get(groups_key, []):
                                items.extend(g.get("items", []))
                            return items
            return []

        items_before = get_tp2_items(orig_t)
        items_after = get_tp2_items(new_t)
        ct4_before = {item_ct4(i) for i in items_before}
        ct4_after = {item_ct4(i) for i in items_after}
        removed = ct4_before - ct4_after

        lines.append(f"\n**terehov / Мультибренд / tp2**\n\n")
        lines.append(f"- Items ДО: {len(items_before)}\n")
        lines.append(f"- Items ПОСЛЕ: {len(items_after)}\n")
        lines.append(f"- Удалённые ct4 коды ({len(removed)}): {', '.join(sorted(removed))}\n")

        lines.append("\n**Первые 10 items ДО:**\n```\n")
        for item in items_before[:10]:
            lines.append(f"  {{'c': {item.get('c','')!r}, 't': {item.get('t','')!r}, 'gc': {item.get('gc','')!r}}}\n")
        lines.append("```\n")

        lines.append("\n**Первые 10 items ПОСЛЕ:**\n```\n")
        for item in items_after[:10]:
            lines.append(f"  {{'c': {item.get('c','')!r}, 't': {item.get('t','')!r}, 'gc': {item.get('gc','')!r}}}\n")
        lines.append("```\n")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as f:
        f.writelines(lines)
    print(f"[build_staging] Отчёт сохранён: {report_path}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Генератор уникальных секций слепков из корпуса директологов."
    )
    parser.add_argument("--corpus", required=True, help="Корень папки corpus")
    parser.add_argument("--slepki", required=True, help="Путь к slepki_structure.json")
    parser.add_argument("--output", required=True, help="Путь для staging-файла")
    parser.add_argument("--report", help="Путь для отчёта (Markdown)")
    parser.add_argument(
        "--slugs",
        nargs="+",
        required=True,
        help="Маппинги key:corpus_subdir, напр. terehov:terehov_evgenii",
    )
    args = parser.parse_args()

    slug_map: dict[str, str] = {}
    for s in args.slugs:
        if ":" not in s:
            print(f"[ERROR] --slugs должны быть в формате key:corpus_subdir: {s!r}", file=sys.stderr)
            sys.exit(1)
        k, v = s.split(":", 1)
        slug_map[k] = v

    stats = build_staging(
        slepki_path=Path(args.slepki),
        corpus_base=Path(args.corpus),
        slug_to_corpus=slug_map,
        output_path=Path(args.output),
        report_path=Path(args.report) if args.report else None,
    )
    # Краткая сводка в stdout
    print(json.dumps(stats, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()

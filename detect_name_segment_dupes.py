"""Счётчик имён кампаний с ПОВТОРОМ сегмента по всей структуре слепков (сигнатура
`NAME_SEGMENT_DUPE`, 2026-07-19). Read-only: ничего не создаёт и не пишет.

Зачем скрипт в репозитории, а не разовый прогон: цифра «988 → 0» из коммита `fff5c8d` была
померена ad-hoc и невоспроизводима, а главное — померена на ПЛАНОВЫХ именах. Для tp3/tp5
плановое имя ≠ живое: метка фида приклеивается ПОЗЖЕ `_uniq`, на билде
(`create_set_feed_builders._create_tp5_campaign` / `_create_tp3_campaign` + cookie-путь), —
то есть ровно там, где дедупа сначала не было. Этот скрипт считает ФИНАЛЬНЫЕ имена.

Что именно считается повтором: имя `nm` несёт повтор ⇔ `dedup_name_segments(nm) != nm`
(тот же критерий, что у report-only детекта `NAME_SEGMENT_DUPE` в `grid_content_verifier`).

Метка фида живёт в кабинете и офлайн НЕизвестна, поэтому tp3/tp5/tp7 считаются под ДВЕ
зонд-метки (обе прогоняются всегда):
  • `worst`     — метка = последний сегмент самого имени. Это не выдумка: ровно этот случай
                  чинил старый гард cookie-пути tp5 (метка кабинета «Товарная галерея» при
                  сегменте «Товарная галерея» в имени). Верхняя оценка класса.
  • `realistic` — метка = URL фида без схемы (`cars-example.ru/feed_lada.xml`): так метку
                  строит действующий код (`_re_fb.sub(r'^https?://', ...)`). Нижняя оценка.

Колонка «до» = имя, которое собрал бы код ДО фикса круга 2 (дедуп только в `_uniq`, метка
фида клеится сырой). Колонка «после» = действующий код (дедуп ещё раз, уже с меткой).

Запуск (прод-венв 3.11 на LXC101; локальный 3.9 не тянет синтаксис проекта):

    ssh proxmox-ts "pct exec 101 -- bash -lc 'cd /opt/scripts/home/seoadvanced && \\
        DIRECT_ROLE=web /root/venv/bin/python -m direct.detect_name_segment_dupes'"

Опции: `--oblast <строка>` (по умолчанию «Москва») — область подставляется в имя так же,
как это делает план.
"""
from __future__ import annotations

import argparse

from . import automation_runtime as ar

# Префиксы кодов, как их строит план (create_set_plan): paycode берём cpc — на повтор
# сегментов тип оплаты не влияет (он живёт в чанке кодов, а не в человеческом ярлыке).
_SEG_TP_PREFIX = {"tp1": "tp1_cpc_site", "tp2": "tp2_cpc_site", "tp3": "tp3_cpc_site",
                  "tp4": "tp4_cpc_site", "tp5": "tp5_cpc_site",
                  # Посевы (tp8-10): GdPostCampaign, только CPC-посадка (SPEC 2026-07-21)
                  "tp8": "tp8_cpc_site", "tp9": "tp9_cpc_site", "tp10": "tp10_cpc_site"}
# tp, у которых метка фида приклеивается к имени (tp7 — в плане, ДО _uniq; tp3/tp5 — на билде).
_FEED_TP = ("tp3", "tp5", "tp7")
_REALISTIC_LABEL = "cars-example.ru/feed_lada.xml"


def _last_seg(nm: str) -> str:
    """Последний человеческий сегмент имени — зонд-метка режима `worst`."""
    chunk = (nm or "").split(" — ")[-1]
    parts = [p.strip() for p in chunk.split(" - ") if p.strip()]
    return parts[-1] if parts else ""


def _collect(oblast: str) -> list[dict]:
    """[{slepok, site_type, tp, prefix, human}] — по одной записи на плановую кампанию.

    `human` — человеческая часть имени БЕЗ дедупа (сырая склейка, как её собирает план);
    `prefix` — чанк кодов до ` — ` (он уникален и на дедуп не влияет).
    """
    csc = ar._create_set_context_module()   # DI: configure() ОБЯЗАТЕЛЕН (иначе NameError _SLEPOK_KEY)
    csp = ar._create_set_plan_module()
    from .create.create_set_structure import structure_to_campaigns as _s2c

    obl_part = f" - {oblast}" if oblast else ""
    rows: list[dict] = []
    for d in ar._json("slepki_structure.json")["directologists"]:
        key = d["key"]
        for st in d.get("site_types", []):
            stn = st["name"]
            # ── tp1–tp5: КАМПАНИЯ = item.camp_names (действующий путь плана) ──
            for tp, prefix in _SEG_TP_PREFIX.items():
                for c in _s2c(key, stn, tp):
                    cname = (c.get("name") or "").strip()
                    if not cname:
                        continue
                    rows.append({"slepok": key, "site_type": stn, "tp": tp,
                                 "prefix": prefix, "human": f"{cname}{obl_part}"})
            # ── tp6/tp7: имя из _build_name (коды берём из него же, человеческую часть
            #    пересобираем сырой — иначе «до» не измерить: _build_name уже дедуплен) ──
            for tp in ("tp6", "tp7"):
                is_master = (tp == "tp6")
                tp_label = "МК" if is_master else "ТК"
                for g in csc._slepok_struct_groups(key, stn, tp):
                    cat = (g.get("name") or "").strip()
                    exp = csc.tp67_struct_expectations(
                        key, stn, tp, "ct0000", "", g.get("name"), g.get("sq"),
                        pos_key=csc._tp67_pos_key(g))
                    modes = exp["modes"]
                    is_at = not ("keywords" in modes or "audience" in modes)
                    nm = csp._build_name(is_master, is_at, "tcpa", "r0000", oblast,
                                         g.get("sq") or "site", cat, "ct0000")
                    rows.append({"slepok": key, "site_type": stn, "tp": tp,
                                 "prefix": nm.split(" — ")[0],
                                 "human": f"{tp_label}"
                                          + (f" - {cat}" if cat else "") + obl_part})
    return rows


def run(oblast: str = "Москва") -> dict:
    dedup = ar._create_set_context_module().dedup_name_segments
    rows = _collect(oblast)

    out: dict = {"oblast": oblast, "positions": len(rows), "labels": {}}
    # ── Плановые имена (без метки фида): «до» = сырая склейка, «после» = _uniq-дедуп ──
    plan_before = plan_after = 0
    for r in rows:
        raw = f"{r['prefix']} — {r['human']}"
        r["plan_after"] = dedup(raw)
        plan_before += int(dedup(raw) != raw)
        plan_after += int(dedup(r["plan_after"]) != r["plan_after"])
    out["plan"] = {"before": plan_before, "after": plan_after}

    # ── Финальные имена (метка фида приклеена) под каждую зонд-метку ──
    for mode in ("worst", "realistic"):
        before = after = 0
        per_tp: dict[str, list[int]] = {}
        buckets_before: dict[tuple, list[str]] = {}
        buckets_after: dict[tuple, list[str]] = {}
        for r in rows:
            base = r["plan_after"]                      # то, что вернул _uniq (каноничное)
            if r["tp"] in _FEED_TP:
                lbl = _last_seg(base) if mode == "worst" else _REALISTIC_LABEL
                fin_before = f"{base} — {lbl}" if lbl else base
                if r["tp"] == "tp7":
                    # tp7: метка клеится В ПЛАНЕ, до _uniq → дедуп её накрывал и ДО фикса.
                    fin_before = dedup(fin_before)
            else:
                fin_before = base
            fin_after = dedup(fin_before)
            b = int(dedup(fin_before) != fin_before)
            before += b
            after += int(dedup(fin_after) != fin_after)
            per_tp.setdefault(r["tp"], [0, 0])
            per_tp[r["tp"]][0] += b
            bk = (r["slepok"], r["site_type"], r["tp"])
            buckets_before.setdefault(bk, []).append(fin_before)
            buckets_after.setdefault(bk, []).append(fin_after)
        # Схлопывание различимых имён: сколько РАЗНЫХ имён «до» стали одинаковыми «после».
        collapsed = 0
        for bk, names_b in buckets_before.items():
            collapsed += len(set(names_b)) - len(set(buckets_after[bk]))
        out["labels"][mode] = {"before": before, "after": after,
                               "collapsed_distinct": collapsed,
                               "per_tp_before": {k: v[0] for k, v in per_tp.items() if v[0]}}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--oblast", default="Москва")
    args = ap.parse_args()
    r = run(args.oblast)
    print(f"позиций (плановых кампаний) по всей структуре: {r['positions']}   "
          f"область-зонд: {r['oblast']!r}")
    print(f"ПЛАНОВЫЕ имена — с повтором сегмента: до {r['plan']['before']} → "
          f"после {r['plan']['after']}   (норма после: 0)")
    bad = r["plan"]["after"]
    for mode, m in r["labels"].items():
        print(f"ФИНАЛЬНЫЕ имена (метка фида, зонд «{mode}») — с повтором: "
              f"до {m['before']} → после {m['after']}   (норма после: 0)")
        if m["per_tp_before"]:
            print(f"    из них «до» по tp: {m['per_tp_before']}")
        print(f"    различимых имён схлопнуто дедупом: {m['collapsed_distinct']}   (норма: 0)")
        bad += m["after"] + m["collapsed_distinct"]
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

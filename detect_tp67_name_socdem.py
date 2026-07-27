"""Детект рассинхрона «имя ⇄ socdem» на связке ПЛАН→БИЛД для tp6/tp7 (сигнатура Д7, 2026-07-19).

Зачем отдельный скрипт, а не запрос в обход `_slepok_struct_groups`: сверка обязана идти по
СВЯЗКЕ план→билд. Запрос, который считает только режим из структуры, структурно НЕ видит
рассинхрон между `create_set_plan._build_name` (ag001/ag011 в имени) и `age_lower` из
`create_set_master_product` (socdem, который реально уезжает в кабинет). Именно этот разрыв дал
частичный фикс `c0e7303`: билд починили, план оставили на регулярке по имени.

Read-only: ничего не создаёт и не пишет, только читает структуру слепков.

Запуск (прод-венв 3.11 на LXC101; локальный 3.9 не тянет синтаксис проекта):

    ssh proxmox-ts "pct exec 101 -- bash -lc 'cd /opt/scripts/home/seoadvanced && \\
        DIRECT_ROLE=web /root/venv/bin/python -m direct.detect_tp67_name_socdem'"

Эталон прогона 2026-07-19 (после `8a16855`): позиций 599 · действующий путь **0** ·
«если вернуть вывод режима по имени» **102**. Второе число — величина регрессии `c0e7303`;
оно и есть смысл контроля: пока оно >0, вывод режима ПО ИМЕНИ возвращать нельзя.
"""
from __future__ import annotations

import re

from . import automation_runtime as ar

_AGE_RE = re.compile(r"_(ag001|ag011)_")
# Допустимые пары (возраст в ИМЕНИ, socdem в БИЛДЕ). Всё остальное — рассинхрон.
_CONSISTENT = {("ag001", "age_18"), ("ag011", "age_25")}


def _age_in_name(nm: str) -> str:
    m = _AGE_RE.search(nm or "")
    return m.group(1) if m else "?"


def _age_lower_of_build(targeting_mode: str, is_product: bool) -> str:
    """Копия правила `create_set_master_product.py` — socdem, который реально уедет в кабинет."""
    return "age_18" if (targeting_mode == "autotarget" or is_product) else "age_25"


def run() -> dict[str, int]:
    csc = ar._create_set_context_module()   # DI: configure() ОБЯЗАТЕЛЕН, иначе NameError _SLEPOK_KEY
    csp = ar._create_set_plan_module()
    bad_live = 0        # рассинхрон на ДЕЙСТВУЮЩЕМ пути (обязан быть 0)
    bad_legacy = 0      # он же, если вернуть вывод режима ПО ИМЕНИ (величина регрессии c0e7303)
    total = 0
    for d in ar._json("slepki_structure.json")["directologists"]:
        for st in d.get("site_types", []):
            for tp in ("tp6", "tp7"):
                is_master = (tp == "tp6")
                for g in csc._slepok_struct_groups(d["key"], st["name"], tp):
                    total += 1
                    exp = csc.tp67_struct_expectations(
                        d["key"], st["name"], tp, "ct0000", "",
                        g.get("name"), g.get("sq"), pos_key=csc._tp67_pos_key(g))
                    modes = exp["modes"]
                    age_socdem = _age_lower_of_build("+".join(modes), not is_master)

                    # ── действующий путь: имя и socdem считаются из ОДНОГО источника ──
                    is_at = not ("keywords" in modes or "audience" in modes)
                    nm = csp._build_name(is_master, is_at, "tcpa", "r0000", "Тест",
                                         g.get("sq") or "site", g.get("name"), "ct0000")
                    if (_age_in_name(nm), age_socdem) not in _CONSISTENT:
                        bad_live += 1

                    # ── контроль: имя из РЕГУЛЯРКИ ПО ИМЕНИ, socdem из содержимого ──
                    legacy = csc._parse_targeting_modes(
                        (g.get("targeting_mode") or "").strip() or csc._tp67_targeting_mode(g))
                    nm_l = csp._build_name(is_master, legacy == ["autotarget"], "tcpa", "r0000",
                                           "Тест", g.get("sq") or "site", g.get("name"), "ct0000")
                    if (_age_in_name(nm_l), age_socdem) not in _CONSISTENT:
                        bad_legacy += 1
    return {"positions": total, "bad_live": bad_live, "bad_legacy_name_based": bad_legacy}


def main() -> int:
    r = run()
    print(f"позиций tp6+tp7: {r['positions']}")
    print(f"рассинхрон имя(ag)↔socdem(age_lower) на ДЕЙСТВУЮЩЕМ пути: {r['bad_live']}   (норма: 0)")
    print("он же, если вернуть вывод режима ПО ИМЕНИ (регрессия c0e7303): "
          f"{r['bad_legacy_name_based']}")
    return 0 if r["bad_live"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

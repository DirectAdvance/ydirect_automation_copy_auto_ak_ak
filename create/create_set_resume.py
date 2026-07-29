"""Pure resume/skip helpers for Direct create_set."""
from __future__ import annotations

import re
from typing import Any


FANOUT_SEP = " — "

# _vNN-суффикс: инструмент добавляет «_v01»…«_v09» при конфликте имён
# (format: f"{name}_v{v:02d}", v=1..99). Регулярка намеренно сужена до _v0[1-9]:
# числа v10+ (_v10, _v60, _v70 и т.д.) совпадают с моделями/поколениями
# (Volvo V60 → имя_v60, Camry V70 → имя_v70), что давало ложные already_in_direct.
# При 10+ дублях одного имени (крайне редко) суффиксы _v10+ просто не попадут сюда
# и пункт не будет пропущен — допустимый компромисс vs. гарантированные ложные матчи.
_VNN_RE = re.compile(r"\s*_v0[1-9]$")


def already_in_direct(target: str, existing_names: set[str]) -> bool:
    """Return True when target campaign or its feed fan-out variant already exists.
    Also matches existing names that carry a _vNN suffix (e.g. 'Name _v02' vs item 'Name')."""
    name = (target or "").strip()
    if not name or not existing_names:
        return False
    if name in existing_names:
        return True
    prefix = name + FANOUT_SEP
    if any(existing.startswith(prefix) for existing in existing_names):
        return True
    # _vNN-суффикс: «Имя _v02» → base «Имя» → матч по имени или fan-out-префиксу.
    for existing in existing_names:
        base = _VNN_RE.sub("", existing).strip()
        if base and base != existing and (base == name or base.startswith(prefix)):
            return True
    return False


def force_recreate(target: str, repair_force_names: set[str]) -> bool:
    """Return True when repair requested this campaign or its fan-out counterpart."""
    name = (target or "").strip()
    if not name or not repair_force_names:
        return False
    if name in repair_force_names:
        return True
    return any(force.startswith(name + FANOUT_SEP) or name.startswith(force + FANOUT_SEP)
               for force in repair_force_names)


def item_matches_result_name(item: dict[str, Any], result_name: str) -> bool:
    """Match base item name against exact/fan-out result name."""
    item_name = (item.get("name") or "").strip()
    result = (result_name or "").strip()
    return bool(item_name and (result == item_name or result.startswith(item_name + FANOUT_SEP)))


def items_for_result_names(items: list[dict[str, Any]], names: set[str]) -> list[dict[str, Any]]:
    """Return items whose base names match any exact/fan-out result name."""
    return [item for item in items or []
            if any(item_matches_result_name(item, name) for name in names)]

"""Хранилище структуры слепков ПО ФАЙЛАМ (разбиение монолита slepki_structure.json).

Зачем: единый `slepki_structure.json` разросся до ~2.5 MiB / 326k строк — неудобно править,
ревьюить и диффить (правка одного слепка = гигантский diff). Источник правды теперь —
per-slepok файлы `direct/slepki/<key>.json` + порядок в `direct/slepki/_order.json`. Единый
словарь `{"directologists": [...]}` собирается В ПАМЯТИ на лету (`assemble`), поэтому ВСЕ читатели
(`_json("slepki_structure.json")`, редактор, qa, тесты) видят ровно тот же объект, что и раньше —
монолитного файла на диске больше нет, дрейфа «части vs монолит» нет по построению.

Кэш: `assemble()` держит собранный dict и пересобирает только когда изменилась сигнатура
(mtime_ns+size) файла порядка ИЛИ любого part-файла. Дешевле, чем парсить 2.5 MiB на каждый вызов.

Запись (`write_directologists`): атомарно (temp+os.replace) пишет ТОЛЬКО изменившиеся part-файлы
и, при изменении состава/порядка, `_order.json`. Так git-diff остаётся маленьким (один слепок —
один файл), а читатель через os.replace видит либо старый, либо новый ЦЕЛЫЙ part.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
SLEPKI_DIR = _HERE / "slepki"
_ORDER_PATH = SLEPKI_DIR / "_order.json"

_LOCK = threading.RLock()
_CACHE_SIG: tuple | None = None
_CACHE_VAL: dict | None = None


def _part_path(key: str) -> Path:
    return SLEPKI_DIR / f"{key}.json"


def _is_part(stem: str) -> bool:
    """Служебные/черновые файлы НЕ слепки. Конвенция: имя с `_` в начале — служебное.

    Так покрыты и `_order.json`, и любые рабочие артефакты (`_proposed_*.json`, `_gap_*.json`),
    которые review-first workflow предписывает класть рядом со слепками: раньше исключался
    ровно один хардкоженный `_order.json`, и `_proposed_dmp.json` уезжал в структуру как
    настоящий слепок (дубль `dmp`).
    """
    return bool(stem) and not stem.startswith("_") and not stem.startswith(".")


def _order() -> list[str]:
    """Порядок ключей директологов (важен: индекс option'а в UI ↔ directologists[i]).

    Ведёт `_order.json`. part-файлы, которых нет в порядке (напр. добавили руками), дописываются
    в конец отсортированно — новый слепок не теряется, даже если забыли обновить _order.json.
    """
    try:
        listed = json.loads(_ORDER_PATH.read_text(encoding="utf-8"))
        if not isinstance(listed, list):
            listed = []
    except Exception:  # noqa: BLE001 — нет/битый порядок → строим из файлов
        listed = []
    listed = [k for k in listed if _is_part(k) and _part_path(k).exists()]
    seen = set(listed)
    extra = sorted(p.stem for p in SLEPKI_DIR.glob("*.json")
                   if _is_part(p.stem) and p.stem not in seen)
    return listed + extra


def _signature() -> tuple:
    """Сигнатура (mtime_ns,size) порядка + всех part-файлов — ключ инвалидации кэша."""
    sig: list = []
    try:
        st = _ORDER_PATH.stat()
        sig.append(("_order", st.st_mtime_ns, st.st_size))
    except OSError:
        sig.append(("_order", 0, 0))
    for key in _order():
        try:
            st = _part_path(key).stat()
            sig.append((key, st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((key, 0, 0))
    return tuple(sig)


def assemble(*, mutable: bool = False) -> dict:
    """Единый словарь `{"directologists":[...]}`, собранный из part-файлов в порядке `_order`.

    Кэшируется по сигнатуре — пересборка только при реальном изменении части/порядка.

    ⚠️ По умолчанию возвращается ОБЩИЙ КЭШИРОВАННЫЙ объект — он READ-ONLY. Мутировать его
    нельзя: правка переживёт вызов и уедет на диск следующей записью структуры (была реальная
    дыра — редактор мутировал кэш ДО preflight, и отклонённая правка оседала в памяти процесса).
    Кому нужно МЕНЯТЬ структуру — зовите `assemble(mutable=True)`: вернётся приватная глубокая
    копия. Копию не делаем по умолчанию намеренно: замер на реальной структуре (19.8 MiB JSON,
    17 директологов) — `copy.deepcopy` 92 мс против 0.3 мс отдачи кэша, а `assemble()` стоит на
    горячем пути чтения (`automation_runtime._json` при создании РК, ui_structure, pack_facts).
    """
    global _CACHE_SIG, _CACHE_VAL
    with _LOCK:                       # сигнатуру считаем под локом — иначе гонка кэша с записью
        sig = _signature()
        if _CACHE_SIG == sig and _CACHE_VAL is not None:
            return copy.deepcopy(_CACHE_VAL) if mutable else _CACHE_VAL
    dirs = []
    for key in _order():
        try:
            dirs.append(json.loads(_part_path(key).read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 — битый part не должен ронять всю структуру
            continue
    val = {"directologists": dirs}
    with _LOCK:
        _CACHE_SIG = sig
        _CACHE_VAL = val
    # свежесобранный val уже лежит в кэше → отдавать его наружу мутатору тоже нельзя
    return copy.deepcopy(val) if mutable else val


def assemble_light_for_selected(selected_key: str = "") -> dict:
    """Light UI tree: full selected slepok + shell records for the dropdown.

    Unlike ``assemble()``, this does not keep a full shared tree in memory for the caller and
    avoids deepcopying every slepok when the UI needs only one opened structure after refresh.
    """
    keys = _order()
    if not selected_key and keys:
        selected_key = keys[0]
    dirs = []
    for key in keys:
        try:
            d = json.loads(_part_path(key).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — bitый part не должен ронять всю light-структуру
            continue
        if key == selected_key:
            dirs.append(d)
            continue
        shell = {
            "key": d.get("key"),
            "name": d.get("name"),
            "ui_group": d.get("ui_group") or "auto",
            "site_types": [{"name": s.get("name")} for s in (d.get("site_types") or [])],
        }
        if "auto" in d:
            shell["auto"] = d.get("auto")
        if d.get("ruleset"):
            shell["ruleset"] = d.get("ruleset")
        dirs.append(shell)
    return {"directologists": dirs}


def invalidate() -> None:
    """Сбросить кэш принудительно (после внешней правки part-файлов)."""
    global _CACHE_SIG, _CACHE_VAL
    with _LOCK:
        _CACHE_SIG = None
        _CACHE_VAL = None


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{int(time.time() * 1000)}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)                 # атомарно в пределах ФС → читатель видит целый part


def write_directologists(dirs: list[dict]) -> list[str]:
    """Записать список директологов в part-файлы. Пишет ТОЛЬКО изменившиеся + `_order.json` при
    изменении состава/порядка. Возвращает список ключей, чьи файлы реально переписаны.

    Формат part-файла — `json.dumps(indent=1, ensure_ascii=False)` (как писал монолит редактор),
    чтобы диффы были стабильны.
    """
    SLEPKI_DIR.mkdir(exist_ok=True)
    changed: list[str] = []
    keys: list[str] = []
    for d in dirs:
        key = d.get("key")
        if not key:
            continue
        keys.append(key)
        text = json.dumps(d, ensure_ascii=False, indent=1)
        p = _part_path(key)
        try:
            old = p.read_text(encoding="utf-8")
        except OSError:
            old = None
        if old != text:
            _atomic_write(p, text)
            changed.append(key)
    # порядок обновляем только при изменении (иначе лишний mtime-скачок → пересборка кэша)
    order_text = json.dumps(keys, ensure_ascii=False, indent=1)
    try:
        old_order = _ORDER_PATH.read_text(encoding="utf-8")
    except OSError:
        old_order = None
    if old_order != order_text:
        _atomic_write(_ORDER_PATH, order_text)
    invalidate()
    return changed

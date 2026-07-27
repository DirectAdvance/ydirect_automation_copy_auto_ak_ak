"""Пер-item пер-стадийный замер времени создания РК (профиль wall-clock).

Одна строка на стадию в stdout (journald), машиночитаемо — грепается префиксом
``STAGE_TIMING``, тело — валидный JSON:

    STAGE_TIMING {"ch":"A1","item":3,"job":"4bce0676297a","login":"porg-x","ms":1234,
                  "ok":true,"stage":"grid:AddAds","tp":"tp1"}

Сборка профиля:
    journalctl -u direct-create-worker -o cat | grep '^STAGE_TIMING ' \\
      | sed 's/^STAGE_TIMING //' | jq -s 'group_by(.stage)|map({stage:.[0].stage,
        n:length, ms:(map(.ms)|add)})|sort_by(-.ms)'

Контекст item'а живёт в ``threading.local``: ``set_item(...)`` зовётся в начале
``_run_item`` (item целиком исполняется в одном потоке канала), поэтому глубокие
транспортные стадии (Grid / v501) подхватывают job/item/tp сами, без протаскивания
параметров через десяток файлов.

Контракт безопасности: замер НИКОГДА не меняет поведение — любая внутренняя ошибка
таймера/сериализации проглатывается, замеряемый блок исполняется как раньше, а
исключение ИЗ блока пробрасывается наружу (строка тайминга при этом всё равно пишется,
с ``"ok":false``).
"""

from __future__ import annotations

import contextlib
import json
import threading
import time

PREFIX = "STAGE_TIMING"

_LOCAL = threading.local()


def _chan_label() -> str:
    """Метка канала из имени потока (createset-chanA1-units → A1)."""
    try:
        nm = threading.current_thread().name or ""
    except Exception:  # noqa: BLE001
        return ""
    for _key, _lbl in (("chanA1", "A1"), ("chanA2", "A2"), ("chanA", "A"), ("chanB", "B")):
        if _key in nm:
            return _lbl
    return ""


def current_item() -> dict:
    """Текущий item-контекст этого потока ({} если вне создания item'а)."""
    try:
        return getattr(_LOCAL, "ctx", None) or {}
    except Exception:  # noqa: BLE001
        return {}


def set_item(**fields) -> None:
    """Выставить item-контекст текущего потока (пустые значения не пишем)."""
    try:
        _LOCAL.ctx = {k: v for k, v in fields.items() if v not in (None, "")}
    except Exception:  # noqa: BLE001 — замер не имеет права ломать создание
        pass


def clear_item() -> None:
    """Сбросить item-контекст (после прохода канала — чтобы postprocess не приписался item'у)."""
    try:
        _LOCAL.ctx = None
    except Exception:  # noqa: BLE001
        pass


def emit(stage_name: str, ms: float, ok: bool = True, **extra) -> None:
    """Записать одну строку тайминга. Никогда не бросает."""
    try:
        row = dict(current_item())
        _ch = _chan_label()
        if _ch:
            row["ch"] = _ch
        row["stage"] = str(stage_name)
        row["ms"] = int(round(float(ms)))
        row["ok"] = bool(ok)
        for k, v in (extra or {}).items():
            if v not in (None, ""):
                row[k] = v
        print(f"{PREFIX} {json.dumps(row, ensure_ascii=False, sort_keys=True)}", flush=True)
    except Exception:  # noqa: BLE001 — логирование best-effort
        pass


@contextlib.contextmanager
def stage(name: str, *, only_in_item: bool = False, **extra):
    """Замерить блок и записать строку тайминга.

    only_in_item=True — писать только внутри item-контекста создания набора
    (чтобы тот же транспорт в content/copy-сервисах не сыпал строками).
    Исключение из блока пробрасывается наружу; строка всё равно пишется с ok=false.
    """
    try:
        _t0 = None if (only_in_item and not current_item()) else time.perf_counter()
    except Exception:  # noqa: BLE001
        _t0 = None
    _ok = True
    try:
        yield
    except BaseException:
        _ok = False
        raise
    finally:
        if _t0 is not None:
            try:
                emit(name, (time.perf_counter() - _t0) * 1000.0, ok=_ok, **extra)
            except Exception:  # noqa: BLE001
                pass

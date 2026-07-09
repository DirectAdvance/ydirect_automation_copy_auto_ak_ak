"""Асинхронная Grid-финализация набора (Задача F) — очередь + запись/replay спеков.

Идея: цикл создания перестаёт блокироваться на самом медленном шаге — Grid-финализации
уровня кампании (`_finalize_rsya` / `_finalize_search_via_grid`: места показа, ассеты,
инварианты, минус-наборы, bidModifiers). При включённом флаге эти вызовы НЕ выполняются
в цикле, а ЗАПИСЫВАЮТСЯ (точные args/kwargs — они полностью JSON-сериализуемы) и уходят в
очередь `direct_delayed_repairs` (kind='finalize_set'). Фоновый delayed-repair-демон
REPLAY-ит их теми же функциями → «ровно тот же набор Grid-операций», только вне цикла.

Флаг: env ``DIRECT_ASYNC_FINALIZE`` (default "0"/off). OFF → `capture_finalize` мгновенно
возвращает False → обёртки в blueprint исполняют финализацию инлайн, как раньше (байт-в-байт).

Маршрутизация захвата — по ``login`` (первый позиционный аргумент финализации). Один логин
за раз обрабатывается одним воркером (agency-gate + login-dedup), поэтому глобальный реестр
`{login: recorder}` корректен и для параллельных каналов C1 (обе нити зовут финализацию с
тем же login), и для пула воркеров (разные логины — разные записи). Thread-local/contextvars
НЕ нужны (они не пробрасываются в под-нити каналов).

DoD-семантика: пока finalize-джоба не завершена, набор НЕ считается готовым. Родительская
карточка держится «running» (см. _parent_absorb_child_start в blueprint), а в result набора
стоит ``finalize_pending``. Verifier уже эмитит SEARCH_NOT_FINALIZED/… (warn, не pass). После
докрутки finalize-джоба реконсилит карточку → зелёный.

Модуль Flask-free: все внешние вызовы (БД/финализация/лог родителя) — через configure(deps).
"""
from __future__ import annotations

import json
import os
import threading
import uuid

_DEPS: dict = {}

# login → FinalizeRecorder (активные наборы, финализация которых захватывается)
_RECORDERS: dict[str, "FinalizeRecorder"] = {}
_LOCK = threading.RLock()

FINALIZE_KIND = "finalize_set"


def configure(deps: dict) -> None:
    """Привязать зависимости blueprint.

    Ожидаемые ключи:
      victory_conn_rw           → psycopg2 connection (rw) фабрика
      finalize_rsya             → РЕАЛЬНАЯ csfin._finalize_rsya (без capture-обёртки!)
      finalize_search_via_grid  → РЕАЛЬНАЯ csfin._finalize_search_via_grid
    """
    _DEPS.clear()
    _DEPS.update(deps)


def async_finalize_enabled() -> bool:
    """Флаг DIRECT_ASYNC_FINALIZE (default OFF)."""
    return os.getenv("DIRECT_ASYNC_FINALIZE", "0").strip().lower() in ("1", "true", "yes", "on")


# ── Recorder ──────────────────────────────────────────────────────────────────

class FinalizeRecorder:
    """Аккумулятор захваченных финализаций одного набора (login/job)."""

    __slots__ = ("login", "job_id", "agency", "specs", "_lock")

    def __init__(self, login: str, job_id: str, agency: str = "") -> None:
        self.login = (login or "").strip()
        self.job_id = (job_id or "").strip()
        self.agency = (agency or "").strip()
        self.specs: list[dict] = []
        self._lock = threading.Lock()

    def record(self, fn_kind: str, args: tuple, kwargs: dict) -> None:
        """Сохранить точный спек финализации (args/kwargs — сериализуемы)."""
        with self._lock:
            self.specs.append({
                "fn": fn_kind,                    # 'rsya' | 'search'
                "args": list(args or ()),
                "kwargs": dict(kwargs or {}),
            })


def register(login: str, job_id: str, agency: str = "") -> "FinalizeRecorder | None":
    """Открыть окно захвата финализации для login. При OFF — no-op (None)."""
    if not async_finalize_enabled():
        return None
    login = (login or "").strip()
    if not login:
        return None
    rec = FinalizeRecorder(login, job_id, agency)
    with _LOCK:
        _RECORDERS[login] = rec
    return rec


def get(login: str) -> "FinalizeRecorder | None":
    """Активный recorder для login (диагностика/тесты). None — окна нет."""
    with _LOCK:
        return _RECORDERS.get((login or "").strip())


def unregister(login: str) -> "FinalizeRecorder | None":
    """Закрыть окно захвата, вернуть recorder (для чтения specs и enqueue)."""
    login = (login or "").strip()
    if not login:
        return None
    with _LOCK:
        return _RECORDERS.pop(login, None)


def capture_finalize(fn_kind: str, args: tuple, kwargs: dict) -> bool:
    """Точка перехвата в blueprint-обёртках финализации.

    True  → захвачено, инлайн-исполнение НАДО пропустить (async-путь).
    False → исполнить инлайн как раньше (OFF, либо для этого login нет активного окна).

    Быстрый выход при OFF — гарантия байт-в-байт идентичности прод-поведения.
    """
    if not async_finalize_enabled():
        return False
    login = ""
    if args:
        login = str(args[0] or "").strip()
    if not login:
        login = str((kwargs or {}).get("login") or "").strip()
    if not login:
        return False
    with _LOCK:
        rec = _RECORDERS.get(login)
    if rec is None:
        return False
    rec.record(fn_kind, args, kwargs)
    return True


# ── Очередь (reuse direct_delayed_repairs, kind='finalize_set') ────────────────

def enqueue(parent_job_id: str, login: str, agency: str, specs: list[dict]) -> str | None:
    """Поставить finalize-джобу в direct_delayed_repairs. Дедуп — ON CONFLICT(parent_job_id,kind).

    specs кладём в колонку result jsonb (это ВХОДНОЙ payload replay-а; отдельная колонка не
    нужна — переиспользуем существующую схему). run_at=now() (докрутка сразу, демон ~poll).
    """
    parent_job_id = (parent_job_id or "").strip()
    login = (login or "").strip()
    if not parent_job_id or not login or not specs:
        return None
    conn_rw = _DEPS.get("victory_conn_rw")
    if not callable(conn_rw):
        return None
    did = uuid.uuid4().hex[:12]
    payload = json.dumps({"specs": specs}, ensure_ascii=False)
    try:
        conn = conn_rw()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO public.direct_delayed_repairs
                    (id, parent_job_id, login, agency, kind, status, attempts, run_at, result)
                VALUES (%s,%s,%s,%s,%s,'waiting',0, now(), %s::jsonb)
                ON CONFLICT (parent_job_id, kind) DO NOTHING
                RETURNING id
                """,
                (did, parent_job_id, login, agency or "", FINALIZE_KIND, payload))
            row = cur.fetchone()
            conn.commit()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception as _e:  # noqa: BLE001 — постановка best-effort; не молчим в лог
        print(f"[finalize-queue] enqueue {login}: {type(_e).__name__}: {str(_e)[:200]}", flush=True)
        return None


def _specs_from_row(row: dict) -> list[dict]:
    res = (row or {}).get("result")
    if isinstance(res, str):
        try:
            res = json.loads(res)
        except Exception:  # noqa: BLE001
            res = {}
    if not isinstance(res, dict):
        return []
    specs = res.get("specs")
    return specs if isinstance(specs, list) else []


def run_finalize_job(row: dict) -> dict:
    """REPLAY захваченных финализаций теми же функциями. Идемпотентно (finalize —
    UpdateCampaigns одними и теми же значениями). Возвращает summary; НЕ поднимает исключений.

    remaining>0 при частичном успехе → демон перепланирует (reschedule/attempts) до нуля.
    """
    fin_rsya = _DEPS.get("finalize_rsya")
    fin_search = _DEPS.get("finalize_search_via_grid")
    specs = _specs_from_row(row)
    applied = 0
    failed: list[dict] = []
    for spec in specs:
        fn_kind = spec.get("fn")
        args = tuple(spec.get("args") or ())
        kwargs = dict(spec.get("kwargs") or {})
        fn = fin_rsya if fn_kind == "rsya" else (fin_search if fn_kind == "search" else None)
        if fn is None or not callable(fn):
            failed.append({"fn": fn_kind, "error": "нет функции финализации в deps"})
            continue
        try:
            fn(*args, **kwargs)
            applied += 1
        except Exception as e:  # noqa: BLE001
            _cid = args[1] if len(args) > 1 else kwargs.get("campaign_id")
            failed.append({"fn": fn_kind, "campaign_id": _cid, "error": str(e)[:200]})
    remaining = len(failed)
    return {
        "ok": remaining == 0,
        "kind": FINALIZE_KIND,
        "total": len(specs),
        "applied": applied,
        "failed": failed[:40],
        "remaining": remaining,
        "uses_direct_units": False,
    }

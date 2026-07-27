"""Cross-service Direct write gate.

Serializes daytime Direct write jobs across independent queues (content-editor,
copy/create workers). The gate is deliberately small: one exclusive global lease,
with explicit release and TTL fallback. Night price-check jobs do not use it.
"""
from __future__ import annotations

import os
import threading
import time as _time
from typing import Callable


_ENSURED = False
_ENSURE_LOCK = threading.Lock()
DEFAULT_TTL_SECONDS = int(os.environ.get("DIRECT_WRITE_GATE_TTL_SECONDS") or 4 * 60 * 60)
GLOBAL_WRITE_RESOURCE_KEY = "direct-write:global"

# ── Gate circuit breaker ──────────────────────────────────────────────────────────────────
# После ПЕРВОГО отказа коннекта к Victory gate-проверки пропускаются на GATE_CB_COOLDOWN секунд,
# затем одна пробная попытка. Это предотвращает ожидание 15 с × N на каждой gate-операции
# при перемежающихся проблемах с подключением к Victory.
# Диагноз (2026-07-27): сеть LXC101→Victory ИСПРАВНА (TCP 34 мс), отказы перемежающиеся.
_GATE_CB_LOCK = threading.Lock()
_GATE_CB_OPEN_TS: float = 0.0      # 0.0 = цепь замкнута; >0 = unix ts момента размыкания
_GATE_CB_COOLDOWN = 120.0           # секунды кулдауна до пробной попытки
_GATE_CB_SKIP_COUNT: int = 0        # суммарное кол-во пропущенных gate-вызовов (process-level)


def gate_cb_should_skip() -> bool:
    """Вернуть True если цепь разомкнута → вызывающий должен сразу fail-open.

    После истечения кулдауна сбрасывает цепь (пробная попытка). При возврате True
    инкрементирует _GATE_CB_SKIP_COUNT для последующей записи в errors_log джобы.
    """
    global _GATE_CB_OPEN_TS, _GATE_CB_SKIP_COUNT
    with _GATE_CB_LOCK:
        if _GATE_CB_OPEN_TS == 0.0:
            return False
        if _time.time() - _GATE_CB_OPEN_TS < _GATE_CB_COOLDOWN:
            _GATE_CB_SKIP_COUNT += 1
            return True
        # Кулдаун истёк: сбрасываем цепь для пробной попытки
        _GATE_CB_OPEN_TS = 0.0
        return False


def gate_cb_on_failure() -> None:
    """Зафиксировать отказ коннекта → разомкнуть цепь (или продлить кулдаун)."""
    global _GATE_CB_OPEN_TS
    with _GATE_CB_LOCK:
        _GATE_CB_OPEN_TS = _time.time()


def gate_cb_on_success() -> None:
    """Зафиксировать успешный коннект → замкнуть цепь."""
    global _GATE_CB_OPEN_TS
    with _GATE_CB_LOCK:
        _GATE_CB_OPEN_TS = 0.0


def drain_skip_count() -> int:
    """Вернуть и атомарно обнулить счётчик пропущенных gate-проверок."""
    global _GATE_CB_SKIP_COUNT
    with _GATE_CB_LOCK:
        n = _GATE_CB_SKIP_COUNT
        _GATE_CB_SKIP_COUNT = 0
    return n


def _norm_agency(agency: str) -> str:
    return str(agency or "").strip().lower()


def agency_resource(agency: str) -> str:
    """Return the write resource key for the given agency.

    For a non-empty agency, returns a per-agency key so that different
    agencies can acquire the write gate concurrently (their Grid sessions /
    cookies / quotas are independent). For empty/None agency (unknown
    provenance), falls back to the global key to preserve conservative
    serialization — letting an unknown-agency job bypass the gate would risk
    concurrent access to the same Grid layer without coordination.
    """
    agency_norm = _norm_agency(agency)
    if agency_norm:
        return f"direct-write:agency:{agency_norm}"
    return GLOBAL_WRITE_RESOURCE_KEY


def ensure_table(conn_factory: Callable) -> None:
    """Create the lease table if needed.

    The table lives in Victory because create/copy jobs already coordinate there.
    Content-editor jobs live in another DB, so the gate cannot rely on joins to
    job tables; release is explicit and stale rows are reclaimed by expires_at.
    """
    global _ENSURED
    if _ENSURED:
        return
    with _ENSURE_LOCK:
        if _ENSURED:
            return
        conn = conn_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS public.direct_api_write_locks (
                        resource_key  text PRIMARY KEY,
                        agency        text NOT NULL DEFAULT '',
                        job_id        text NOT NULL,
                        job_kind      text NOT NULL DEFAULT '',
                        owner_service text NOT NULL DEFAULT '',
                        locked_at     timestamptz NOT NULL DEFAULT now(),
                        heartbeat_at  timestamptz NOT NULL DEFAULT now(),
                        expires_at    timestamptz NOT NULL
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS direct_api_write_locks_expires_idx "
                    "ON public.direct_api_write_locks(expires_at)"
                )
            conn.commit()
            _ENSURED = True
        finally:
            conn.close()


def try_acquire_agency(
    conn_factory: Callable,
    agency: str,
    job_id: str,
    *,
    job_kind: str = "",
    owner_service: str = "",
    ttl_seconds: int | None = None,
) -> bool:
    """Try to acquire the exclusive per-agency Direct write lease.

    Returns False when another live write job already holds the same resource
    key. Different agencies get independent resource keys, so they can run
    concurrently. Blank agency falls back to the global resource key for
    conservative serialisation (missing agency metadata must not bypass the
    gate).
    """
    agency_norm = _norm_agency(agency)
    resource_key = agency_resource(agency)
    ttl = int(ttl_seconds or DEFAULT_TTL_SECONDS)
    if gate_cb_should_skip():
        print(f"[write-gate] circuit-open, skip acquire ({agency_norm})", flush=True)
        return True  # fail-open: разрешаем джобе продолжить без gate enforcement
    try:
        ensure_table(conn_factory)
        try:
            conn = conn_factory()
        except Exception as e:  # noqa: BLE001  — ошибка коннекта → разомкнуть цепь
            gate_cb_on_failure()
            print(f"[write-gate] acquire fail-open ({agency_norm}): {str(e)[:160]}", flush=True)
            return True
        gate_cb_on_success()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM public.direct_api_write_locks WHERE expires_at < now()")
                cur.execute(
                    """
                    INSERT INTO public.direct_api_write_locks
                        (resource_key, agency, job_id, job_kind, owner_service, expires_at)
                    SELECT %s, %s, %s, %s, %s, now() + make_interval(secs => %s)
                    WHERE NOT EXISTS (
                        SELECT 1 FROM public.direct_api_write_locks
                         WHERE resource_key = %s AND expires_at >= now()
                    )
                    ON CONFLICT (resource_key) DO UPDATE SET
                        job_id = EXCLUDED.job_id,
                        agency = EXCLUDED.agency,
                        job_kind = EXCLUDED.job_kind,
                        owner_service = EXCLUDED.owner_service,
                        heartbeat_at = now(),
                        expires_at = EXCLUDED.expires_at
                    WHERE public.direct_api_write_locks.expires_at < now()
                    RETURNING resource_key
                    """,
                    (
                        resource_key,
                        agency_norm,
                        str(job_id or ""),
                        str(job_kind or "")[:80],
                        str(owner_service or "")[:80],
                        ttl,
                        resource_key,  # repeated for NOT EXISTS per-key check
                    ),
                )
                got = cur.fetchone() is not None
            conn.commit()
            return got
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        print(f"[write-gate] acquire fail-open ({agency_norm}): {str(e)[:160]}", flush=True)
        return True


def release_agency(conn_factory: Callable, agency: str, job_id: str) -> None:
    agency_norm = _norm_agency(agency)
    resource_key = agency_resource(agency)
    # Cover all key formats: current per-agency key, old global key (for locks
    # acquired before the per-agency migration), and the legacy "agency:" prefix
    # that was the partially-prepared format in release before the migration.
    keys = list({resource_key, GLOBAL_WRITE_RESOURCE_KEY, "agency:" + agency_norm})
    if gate_cb_should_skip():
        print(f"[write-gate] circuit-open, skip release ({agency_norm})", flush=True)
        return  # fail-open: lock истечёт по TTL
    try:
        ensure_table(conn_factory)
        try:
            conn = conn_factory()
        except Exception as e:  # noqa: BLE001
            gate_cb_on_failure()
            print(f"[write-gate] release fail-open ({agency_norm}): {str(e)[:160]}", flush=True)
            return
        gate_cb_on_success()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM public.direct_api_write_locks "
                    "WHERE resource_key = ANY(%s) AND job_id=%s",
                    (keys, str(job_id or "")),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        print(f"[write-gate] release fail-open ({agency_norm}): {str(e)[:160]}", flush=True)


def release_owner(conn_factory: Callable, owner_service: str) -> int:
    """Release stale locks from a restarted singleton owner."""
    owner = str(owner_service or "").strip()
    if not owner:
        return 0
    try:
        ensure_table(conn_factory)
        conn = conn_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM public.direct_api_write_locks WHERE owner_service=%s",
                    (owner,),
                )
                n = cur.rowcount
            conn.commit()
            return int(n or 0)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        print(f"[write-gate] owner release fail-open ({owner}): {str(e)[:160]}", flush=True)
        return 0


def cleanup_expired(conn_factory: Callable) -> int:
    if gate_cb_should_skip():
        print("[write-gate] circuit-open, skip cleanup_expired", flush=True)
        return 0  # fail-open: устаревшие строки доживут до следующего прохода
    try:
        ensure_table(conn_factory)
        try:
            conn = conn_factory()
        except Exception as e:  # noqa: BLE001
            gate_cb_on_failure()
            print(f"[write-gate] cleanup fail-open: {str(e)[:160]}", flush=True)
            return 0
        gate_cb_on_success()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM public.direct_api_write_locks WHERE expires_at < now() "
                    "RETURNING resource_key"
                )
                rows = cur.fetchall() or []
            conn.commit()
            return len(rows)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        print(f"[write-gate] cleanup fail-open: {str(e)[:160]}", flush=True)
        return 0


def cleanup_direct_automation_inactive(
    conn_factory: Callable,
    owner_services: tuple[str, ...] = (
        "direct-copy",
        "direct-create-worker",
        "direct-slepki-worker",
    ),
) -> int:
    """Release locks whose Victory queue jobs are no longer active.

    Content-editor jobs live in another DB and are intentionally not touched
    here. The owner filter prevents accidental deletion on an unlikely job_id
    collision with content jobs.
    """
    owners = [str(x or "").strip() for x in owner_services if str(x or "").strip()]
    if not owners:
        return 0
    if gate_cb_should_skip():
        print("[write-gate] circuit-open, skip cleanup_direct_automation_inactive", flush=True)
        return 0  # fail-open: неактивные блокировки доживут до следующего sweep
    try:
        ensure_table(conn_factory)
        try:
            conn = conn_factory()
        except Exception as e:  # noqa: BLE001
            gate_cb_on_failure()
            print(f"[write-gate] direct jobs cleanup fail-open: {str(e)[:160]}", flush=True)
            return 0
        gate_cb_on_success()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM public.direct_api_write_locks l
                    USING public.direct_automation_jobs j
                    WHERE l.job_id = j.job_id
                      AND l.owner_service = ANY(%s)
                      AND j.status NOT IN ('running', 'claimed', 'queued')
                    RETURNING l.resource_key
                    """,
                    (owners,),
                )
                rows = cur.fetchall() or []
            conn.commit()
            return len(rows)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        print(f"[write-gate] direct jobs cleanup fail-open: {str(e)[:160]}", flush=True)
        return 0

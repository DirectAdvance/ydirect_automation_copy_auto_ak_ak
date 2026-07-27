"""Victory PostgreSQL connection factory for Direct automation.

Independent from :mod:`direct.blueprint`: entrypoints can import database access without
loading Flask routes, queue state or Yandex transports. Callers close returned connections.
"""
from __future__ import annotations

import atexit
import os
import sys
import threading

from . import campaign as cmc


VICTORY_STATEMENT_TIMEOUT_MS = int(os.environ.get("DIRECT_VICTORY_STMT_TIMEOUT_MS", "120000"))
VICTORY_KEEPALIVE_KW = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
}
VICTORY_POOL_MIN = max(1, int(os.environ.get("DIRECT_VICTORY_POOL_MIN", "1")))
VICTORY_POOL_MAX = max(VICTORY_POOL_MIN, int(os.environ.get("DIRECT_VICTORY_POOL_MAX", "12")))

_VICTORY_RO_POOL = None
_VICTORY_RO_POOL_LOCK = threading.Lock()
_VICTORY_RO_POOL_SLOTS = threading.BoundedSemaphore(VICTORY_POOL_MAX)


def _db_config() -> dict:
    secret_dir = str(cmc._find_secret_dir())
    if secret_dir not in sys.path:
        sys.path.insert(0, secret_dir)
    from loader import load_db  # noqa: PLC0415

    return load_db("victory")


def _connect_kwargs() -> dict:
    cfg = _db_config()
    return {
        "host": cfg["host"], "port": cfg["port"], "database": cfg["database"],
        "user": cfg["user"], "password": cfg["password"], "connect_timeout": 15,
        "options": f"-c statement_timeout={VICTORY_STATEMENT_TIMEOUT_MS}",
        **VICTORY_KEEPALIVE_KW,
    }


def _readonly_pool():
    global _VICTORY_RO_POOL
    if _VICTORY_RO_POOL is not None:
        return _VICTORY_RO_POOL
    with _VICTORY_RO_POOL_LOCK:
        if _VICTORY_RO_POOL is None:
            from psycopg2.pool import ThreadedConnectionPool
            _VICTORY_RO_POOL = ThreadedConnectionPool(
                VICTORY_POOL_MIN, VICTORY_POOL_MAX, **_connect_kwargs()
            )
    return _VICTORY_RO_POOL


class _PooledReadonlyConnection:
    """Connection facade whose existing ``close()`` calls return it to the pool."""

    def __init__(self, pool, raw):
        self._pool = pool
        self._raw = raw
        self._returned = False

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def __enter__(self):
        return self

    @property
    def closed(self):
        return 1 if self._returned else self._raw.closed

    def __exit__(self, exc_type, exc, tb):
        self.close(discard=bool(exc_type))
        return False

    def close(self, discard: bool = False):
        if self._returned:
            return
        self._returned = True
        try:
            self._pool.putconn(self._raw, close=discard or bool(self._raw.closed))
        finally:
            _VICTORY_RO_POOL_SLOTS.release()

    def __del__(self):
        try:
            self.close(discard=True)
        except Exception:
            pass


def victory_conn():
    """Pooled read-only autocommit connection with finite wait/socket timeouts.

    Guards against stale pooled sockets: Postgres on Victory can silently drop an
    idle connection (SSL EOF) while ``conn.closed`` still reads 0, so a bare
    ``closed`` check passes and the first real I/O (``set_session``/query) raises
    ``OperationalError: SSL SYSCALL error: EOF detected``. In practice psycopg2
    can also surface the same dead socket as a broader ``DatabaseError`` on
    ``set_session()`` before the ping query even starts. We ping the handed-out
    connection (``SELECT 1``); on failure the dead socket is discarded (never
    returned to the pool as live) and one retry is made on a fresh connection
    before propagating.
    """
    import psycopg2

    if not _VICTORY_RO_POOL_SLOTS.acquire(timeout=15):
        raise TimeoutError("Victory read-only connection pool is busy")
    pool = None
    conn = None
    try:
        pool = _readonly_pool()
        for attempt in range(2):
            conn = pool.getconn()
            if conn.closed:
                pool.putconn(conn, close=True)
                conn = pool.getconn()
            try:
                conn.set_session(readonly=True, autocommit=True)
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            except (psycopg2.OperationalError, psycopg2.DatabaseError):
                # Stale pooled socket (server-side EOF): drop it, never reuse.
                try:
                    pool.putconn(conn, close=True)
                finally:
                    conn = None
                if attempt == 0:
                    continue
                raise
            return _PooledReadonlyConnection(pool, conn)
        raise RuntimeError("Victory read-only connection unavailable")  # unreachable
    except Exception:
        if pool is not None and conn is not None:
            try:
                pool.putconn(conn, close=True)
            except Exception:
                pass
        _VICTORY_RO_POOL_SLOTS.release()
        raise


def victory_conn_rw():
    """Read-write connection; transaction ownership stays with the caller."""
    import psycopg2

    conn = psycopg2.connect(**_connect_kwargs())
    conn.autocommit = False
    return conn


def _close_readonly_pool() -> None:
    pool = _VICTORY_RO_POOL
    if pool is not None and not pool.closed:
        pool.closeall()


atexit.register(_close_readonly_pool)


_victory_conn = victory_conn
_victory_conn_rw = victory_conn_rw

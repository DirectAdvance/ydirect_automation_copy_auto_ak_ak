from __future__ import annotations

from types import SimpleNamespace

from direct import direct_repository


class _FakeSemaphore:
    def __init__(self) -> None:
        self.acquired = 0
        self.released = 0

    def acquire(self, timeout=None) -> bool:
        self.acquired += 1
        return True

    def release(self) -> None:
        self.released += 1


class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, _sql: str) -> None:
        return None

    def fetchone(self):
        return (1,)


class _FakeConn:
    def __init__(self, *, fail_exc: type[Exception] | None = None) -> None:
        self.fail_exc = fail_exc
        self.closed = 0
        self.session_calls = 0

    def set_session(self, readonly=True, autocommit=True) -> None:
        self.session_calls += 1
        if self.fail_exc is not None:
            raise self.fail_exc("server closed the connection unexpectedly")

    def cursor(self):
        return _FakeCursor()


class _FakePool:
    def __init__(self, conns: list[_FakeConn]) -> None:
        self._conns = list(conns)
        self.puts: list[tuple[_FakeConn, bool]] = []

    def getconn(self) -> _FakeConn:
        return self._conns.pop(0)

    def putconn(self, conn: _FakeConn, close: bool = False) -> None:
        self.puts.append((conn, close))


def test_victory_conn_retries_on_database_error(monkeypatch):
    class FakeOperationalError(Exception):
        pass

    class FakeDatabaseError(Exception):
        pass

    stale = _FakeConn(fail_exc=FakeDatabaseError)
    fresh = _FakeConn()
    pool = _FakePool([stale, fresh])
    sem = _FakeSemaphore()
    fake_psycopg2 = SimpleNamespace(
        OperationalError=FakeOperationalError,
        DatabaseError=FakeDatabaseError,
    )

    monkeypatch.setattr(direct_repository, "_readonly_pool", lambda: pool)
    monkeypatch.setattr(direct_repository, "_VICTORY_RO_POOL_SLOTS", sem)
    monkeypatch.setitem(__import__("sys").modules, "psycopg2", fake_psycopg2)

    conn = direct_repository.victory_conn()
    try:
        assert conn._raw is fresh
        assert stale.session_calls == 1
        assert fresh.session_calls == 1
        assert pool.puts == [(stale, True)]
        assert sem.acquired == 1
        assert sem.released == 0
    finally:
        conn.close()

    assert pool.puts == [(stale, True), (fresh, False)]
    assert sem.released == 1

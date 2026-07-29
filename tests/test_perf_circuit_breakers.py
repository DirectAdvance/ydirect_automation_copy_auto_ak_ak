"""Tests for circuit breaker patches (2026-07-27):
  PATCH 1 — link_check.py host-level circuit breaker
  PATCH 2 — write_gate.py gate circuit breaker

Both verify:
  - After N failures circuit opens, subsequent calls skip network
  - Fail-open: original value is returned unchanged
  - Success resets the circuit (link_check only)
"""
from __future__ import annotations

import threading
import time


# ─────────────────────────────────────────────────────────────
# PATCH 1 — link_check circuit breaker
# ─────────────────────────────────────────────────────────────

from direct import link_check


def _reset_link_check_cb():
    """Clear circuit breaker state between tests."""
    with link_check._CB_LOCK:
        link_check._CB_FAILURES.clear()
        link_check._CB_OPEN_TS.clear()
    link_check._URL_CHECK_CACHE.clear()


def test_link_check_cb_opens_after_threshold(monkeypatch):
    """After _CB_THRESHOLD consecutive timeouts for a host, circuit opens."""
    _reset_link_check_cb()
    calls = []

    def fake_status(url, timeout):
        calls.append(url)
        return None  # simulate timeout

    monkeypatch.setattr(link_check, "_http_status", fake_status)

    url1 = "https://newautos-193.site/auto/haval"
    url2 = "https://newautos-193.site/auto/chery"
    url3 = "https://newautos-193.site/catalog"
    url4 = "https://newautos-193.site/finance"  # should be skipped after threshold

    assert link_check.resolve_or_fallback_url(url1) == url1  # timeout → original
    assert link_check.resolve_or_fallback_url(url2) == url2  # timeout → original
    assert link_check.resolve_or_fallback_url(url3) == url3  # timeout → opens circuit (count=3)

    # Circuit is now open: url4 must NOT call _http_status
    calls_before = len(calls)
    result4 = link_check.resolve_or_fallback_url(url4)
    assert result4 == url4, "fail-open must return original URL"
    assert len(calls) == calls_before, (
        "circuit open: _http_status must NOT be called for the 4th URL of same host"
    )
    assert link_check._CB_FAILURES.get("newautos-193.site", 0) >= link_check._CB_THRESHOLD


def test_link_check_cb_fail_open_preserves_original_href(monkeypatch):
    """When circuit is open, resolve_urls_batch returns the original URL unchanged."""
    _reset_link_check_cb()
    host = "down-host.example.com"
    # Manually open the circuit (timestamp = now so cooldown has NOT elapsed)
    with link_check._CB_LOCK:
        link_check._CB_FAILURES[host] = link_check._CB_THRESHOLD
        link_check._CB_OPEN_TS[host] = time.time()

    http_calls = []
    monkeypatch.setattr(link_check, "_http_status", lambda u, t: http_calls.append(u) or 200)

    urls = [f"https://{host}/page{i}" for i in range(5)]
    result = link_check.resolve_urls_batch(urls)
    assert http_calls == [], "circuit open: resolve_urls_batch must not call _http_status"
    for u in urls:
        assert result[u] == u, f"fail-open: {u!r} must return unchanged"


def test_link_check_cb_success_resets_failures(monkeypatch):
    """A successful HTTP response resets the failure counter."""
    _reset_link_check_cb()
    host = "flaky.example.com"
    # Two failures recorded (below threshold)
    with link_check._CB_LOCK:
        link_check._CB_FAILURES[host] = 2

    def fake_status(url, timeout):
        return 200  # success

    monkeypatch.setattr(link_check, "_http_status", fake_status)

    result = link_check.resolve_or_fallback_url(f"https://{host}/path")
    assert result == f"https://{host}/path"
    assert link_check._CB_FAILURES.get(host, 0) == 0, "success must reset failure counter"


def test_link_check_cb_no_probe_before_cooldown(monkeypatch):
    """Within cooldown window, circuit stays open: HTTP must NOT be called."""
    _reset_link_check_cb()
    host = "sticky.example.com"
    # Manually open the circuit (threshold reached, timestamp = now)
    with link_check._CB_LOCK:
        link_check._CB_FAILURES[host] = link_check._CB_THRESHOLD
        link_check._CB_OPEN_TS[host] = time.time()  # opened just now

    http_calls = []
    monkeypatch.setattr(link_check, "_http_status", lambda u, t: http_calls.append(u) or 200)

    url = f"https://{host}/cars/bmw"
    result = link_check.resolve_or_fallback_url(url)
    assert result == url, "fail-open: must return original URL"
    assert http_calls == [], "circuit open within cooldown: _http_status must NOT be called"


def test_link_check_cb_probe_after_cooldown(monkeypatch):
    """After cooldown expires, circuit half-opens: HTTP IS called (probe attempt)."""
    _reset_link_check_cb()
    host = "recovering.example.com"
    # Manually open the circuit with a timestamp in the past (cooldown elapsed)
    with link_check._CB_LOCK:
        link_check._CB_FAILURES[host] = link_check._CB_THRESHOLD
        link_check._CB_OPEN_TS[host] = time.time() - (link_check._CB_COOLDOWN + 5)

    http_calls = []
    monkeypatch.setattr(link_check, "_http_status", lambda u, t: http_calls.append(u) or 200)

    url = f"https://{host}/cars/toyota"
    result = link_check.resolve_or_fallback_url(url)
    assert result == url, "probe returned 200, must get original URL back (no fallback needed)"
    assert len(http_calls) >= 1, "after cooldown, _http_status MUST be called (probe attempt)"
    assert any(host in c for c in http_calls), "probe must target the recovering host"
    # After success, failure counter must be reset
    assert link_check._CB_FAILURES.get(host, 0) == 0, "successful probe must reset failure counter"


def test_link_check_cb_different_hosts_independent(monkeypatch):
    """Circuit breaker is per-host: opening for one host does not affect another."""
    _reset_link_check_cb()
    bad_host = "bad.example.com"
    good_host = "good.example.com"
    # Open circuit for bad_host (timestamp = now so cooldown has NOT elapsed)
    with link_check._CB_LOCK:
        link_check._CB_FAILURES[bad_host] = link_check._CB_THRESHOLD
        link_check._CB_OPEN_TS[bad_host] = time.time()

    calls = []

    def fake_status(url, timeout):
        calls.append(url)
        return 200

    monkeypatch.setattr(link_check, "_http_status", fake_status)

    r_bad = link_check.resolve_or_fallback_url(f"https://{bad_host}/page")
    r_good = link_check.resolve_or_fallback_url(f"https://{good_host}/page")

    assert r_bad == f"https://{bad_host}/page"   # fail-open, no HTTP call
    assert r_good == f"https://{good_host}/page"  # should get 200 back
    assert any(good_host in c for c in calls), "good_host must still be checked"
    assert not any(bad_host in c for c in calls), "bad_host must NOT be checked (circuit open)"


# ─────────────────────────────────────────────────────────────
# PATCH 2 — write_gate circuit breaker
# ─────────────────────────────────────────────────────────────

from direct.core import write_gate


def _reset_write_gate_cb():
    """Reset write_gate circuit breaker state between tests."""
    with write_gate._GATE_CB_LOCK:
        write_gate._GATE_CB_OPEN_TS = 0.0
        write_gate._GATE_CB_SKIP_COUNT = 0


def test_write_gate_cb_skips_after_first_connect_failure():
    """After first connection failure, gate_cb_should_skip() returns True."""
    _reset_write_gate_cb()
    assert not write_gate.gate_cb_should_skip(), "circuit must start closed"
    write_gate.gate_cb_on_failure()
    assert write_gate.gate_cb_should_skip(), "circuit must open after failure"
    assert write_gate.gate_cb_should_skip(), "subsequent calls during cooldown must also skip"


def test_write_gate_cb_skip_count_increments():
    """_GATE_CB_SKIP_COUNT increments on each skipped call."""
    _reset_write_gate_cb()
    write_gate.gate_cb_on_failure()
    # Each call to gate_cb_should_skip while open increments counter
    write_gate.gate_cb_should_skip()
    write_gate.gate_cb_should_skip()
    write_gate.gate_cb_should_skip()
    assert write_gate._GATE_CB_SKIP_COUNT == 3


def test_write_gate_drain_skip_count_resets():
    """drain_skip_count returns accumulated count and resets to zero."""
    _reset_write_gate_cb()
    write_gate.gate_cb_on_failure()
    write_gate.gate_cb_should_skip()
    write_gate.gate_cb_should_skip()
    n = write_gate.drain_skip_count()
    assert n == 2
    assert write_gate._GATE_CB_SKIP_COUNT == 0, "drain must reset counter to zero"
    assert write_gate.drain_skip_count() == 0, "second drain must return 0"


def test_write_gate_cb_success_closes_circuit():
    """gate_cb_on_success() closes the circuit."""
    _reset_write_gate_cb()
    write_gate.gate_cb_on_failure()
    assert write_gate.gate_cb_should_skip()
    write_gate.gate_cb_on_success()
    assert not write_gate.gate_cb_should_skip(), "circuit must close after success"


def test_write_gate_cb_cooldown_probe():
    """After cooldown expires, gate_cb_should_skip returns False (one probe allowed)."""
    _reset_write_gate_cb()
    write_gate.gate_cb_on_failure()
    # Simulate cooldown elapsed by backdating the open timestamp
    with write_gate._GATE_CB_LOCK:
        write_gate._GATE_CB_OPEN_TS = time.time() - (write_gate._GATE_CB_COOLDOWN + 1)
    # Should allow probe (not skip)
    assert not write_gate.gate_cb_should_skip(), "after cooldown, one probe must be allowed"
    # Circuit is now tentatively closed
    assert write_gate._GATE_CB_OPEN_TS == 0.0


def test_write_gate_try_acquire_skips_when_circuit_open():
    """try_acquire_agency returns True (fail-open) without calling conn_factory when circuit is open."""
    _reset_write_gate_cb()
    write_gate.gate_cb_on_failure()

    conn_factory_calls = []

    def _factory():
        conn_factory_calls.append(1)
        raise RuntimeError("should not be called")

    # Should fail-open immediately without calling _factory
    result = write_gate.try_acquire_agency(_factory, "test-agency", "job-123")
    assert result is True, "must fail-open"
    assert conn_factory_calls == [], "conn_factory must NOT be called when circuit is open"


def test_write_gate_try_acquire_opens_circuit_on_connect_failure(monkeypatch):
    """try_acquire_agency opens the circuit on connection failure.

    In production _ENSURED is True (table was created at startup), so ensure_table
    is a no-op and the connection failure surfaces in conn_factory() directly.
    """
    _reset_write_gate_cb()
    # Simulate post-startup state: table already ensured, skip DDL connection.
    monkeypatch.setattr(write_gate, "_ENSURED", True)

    def _factory():
        raise ConnectionRefusedError("Victory unreachable")

    result = write_gate.try_acquire_agency(_factory, "test-agency", "job-456")
    assert result is True, "must fail-open on connect error"
    assert write_gate._GATE_CB_OPEN_TS > 0, "circuit must open on connect failure"


def test_write_gate_cleanup_expired_skips_when_circuit_open():
    """cleanup_expired returns 0 (fail-open) without calling conn_factory when circuit is open."""
    _reset_write_gate_cb()
    write_gate.gate_cb_on_failure()

    calls = []

    def _factory():
        calls.append(1)
        raise RuntimeError("should not be called")

    result = write_gate.cleanup_expired(_factory)
    assert result == 0
    assert calls == [], "conn_factory must NOT be called when circuit is open"


def test_write_gate_cleanup_inactive_skips_when_circuit_open():
    """cleanup_direct_automation_inactive returns 0 without calling conn_factory when circuit open."""
    _reset_write_gate_cb()
    write_gate.gate_cb_on_failure()

    calls = []

    def _factory():
        calls.append(1)
        raise RuntimeError("should not be called")

    result = write_gate.cleanup_direct_automation_inactive(_factory)
    assert result == 0
    assert calls == [], "conn_factory must NOT be called when circuit is open"

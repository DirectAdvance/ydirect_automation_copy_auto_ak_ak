"""Tests for second token thread (channel A sub-threads) and atomic 152-flip.

Coverage:
1. _ASharedCookieState: unit tests for sync_to / flip_from / thread safety
2. _partition_a_indices: disjointness and completeness guarantees
3. DIRECT_TOKEN_THREADS feature flag behaviour (env var capping and single-thread path)
4. build_create_set_response: chan_A_wall_sec / chan_B_wall_sec fields present
"""
from __future__ import annotations

import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from direct.create_set_orchestrator import (
    _ASharedCookieState,
    _compute_token_threads,
    _partition_a_indices,
)
from direct.create_set_response import build_create_set_response


# ── _ASharedCookieState ──────────────────────────────────────────────────────

class _FakeChan:
    """Minimal fake _Chan with just the via_cookie attribute."""
    def __init__(self, via_cookie: bool = False):
        self.via_cookie = via_cookie


class TestASharedCookieState:

    def test_initial_state_false(self):
        s = _ASharedCookieState(False)
        assert s.via_cookie is False

    def test_initial_state_true(self):
        s = _ASharedCookieState(True)
        assert s.via_cookie is True

    def test_sync_to_propagates_flip(self):
        """sync_to should flip ch.via_cookie when shared state is True."""
        s = _ASharedCookieState(True)
        ch = _FakeChan(False)
        s.sync_to(ch)
        assert ch.via_cookie is True

    def test_sync_to_noop_when_shared_false(self):
        """sync_to should NOT touch ch.via_cookie when shared state is False."""
        s = _ASharedCookieState(False)
        ch = _FakeChan(False)
        s.sync_to(ch)
        assert ch.via_cookie is False

    def test_sync_to_noop_when_ch_already_true(self):
        """sync_to is idempotent: ch already True → stays True, no assignment."""
        s = _ASharedCookieState(True)
        ch = _FakeChan(True)
        s.sync_to(ch)
        assert ch.via_cookie is True  # stays True, not flipped back

    def test_flip_from_propagates_ch_true(self):
        """flip_from should set shared state True when ch.via_cookie is True."""
        s = _ASharedCookieState(False)
        ch = _FakeChan(True)
        s.flip_from(ch)
        assert s.via_cookie is True

    def test_flip_from_noop_when_ch_false(self):
        """flip_from should NOT change shared state when ch.via_cookie is False."""
        s = _ASharedCookieState(False)
        ch = _FakeChan(False)
        s.flip_from(ch)
        assert s.via_cookie is False

    def test_flip_from_idempotent(self):
        """flip_from when shared already True → stays True."""
        s = _ASharedCookieState(True)
        ch = _FakeChan(True)
        s.flip_from(ch)
        assert s.via_cookie is True

    def test_cross_thread_sync(self):
        """Thread A1 flips, Thread A2 sees it on next sync_to.

        Simulates: A1 processes item, detects 152, sets ch_A1.via_cookie=True, calls flip_from.
        A2 calls sync_to before its next item → should see via_cookie=True.
        """
        s = _ASharedCookieState(False)
        ch_a1 = _FakeChan(False)
        ch_a2 = _FakeChan(False)

        flip_done = threading.Event()
        sync_done = threading.Event()
        results = {}

        def thread_a1():
            # A1 detects 152, flips its own ch
            ch_a1.via_cookie = True
            s.flip_from(ch_a1)
            flip_done.set()

        def thread_a2():
            flip_done.wait()  # wait for A1 to flip
            s.sync_to(ch_a2)
            results["a2_via_cookie"] = ch_a2.via_cookie
            sync_done.set()

        t1 = threading.Thread(target=thread_a1)
        t2 = threading.Thread(target=thread_a2)
        t1.start(); t2.start()
        t1.join(); t2.join()

        assert results["a2_via_cookie"] is True, (
            "A2 should see via_cookie=True after A1 flipped the shared state")

    def test_concurrent_flip_no_data_race(self):
        """Multiple threads flipping simultaneously should not corrupt state."""
        s = _ASharedCookieState(False)
        errors = []

        def flipper():
            try:
                ch = _FakeChan(True)
                for _ in range(100):
                    s.flip_from(ch)
                    s.sync_to(ch)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=flipper) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Race condition errors: {errors}"
        assert s.via_cookie is True  # once flipped, stays True


# ── _partition_a_indices ─────────────────────────────────────────────────────

class TestPartitionAIndices:

    def test_disjoint(self):
        """No item index appears in both partitions."""
        idx = list(range(20))
        a1, a2 = _partition_a_indices(idx)
        assert set(a1).isdisjoint(set(a2)), "Partitions must be disjoint"

    def test_complete(self):
        """Together the two partitions contain ALL original indices."""
        idx = list(range(20))
        a1, a2 = _partition_a_indices(idx)
        assert set(a1) | set(a2) == set(idx), "Partitions must be complete (cover all indices)"

    def test_no_duplicates_within_partition(self):
        """Each partition has no duplicate indices."""
        idx = [5, 2, 8, 1, 3, 7, 11, 4, 9, 0]
        a1, a2 = _partition_a_indices(idx)
        assert len(a1) == len(set(a1)), "A1 must have no duplicates"
        assert len(a2) == len(set(a2)), "A2 must have no duplicates"

    def test_even_single_item(self):
        """Edge case: only 1 item → A1 has it, A2 is empty."""
        a1, a2 = _partition_a_indices([42])
        assert a1 == [42]
        assert a2 == []

    def test_empty(self):
        """Edge case: empty input → both partitions empty."""
        a1, a2 = _partition_a_indices([])
        assert a1 == []
        assert a2 == []

    def test_two_items(self):
        """Edge case: exactly 2 items → each partition gets exactly 1."""
        a1, a2 = _partition_a_indices([10, 20])
        assert set(a1) == {10}
        assert set(a2) == {20}

    def test_interleaved_pattern(self):
        """Positions are correctly interleaved (even/odd)."""
        idx = [0, 1, 2, 3, 4, 5]
        a1, a2 = _partition_a_indices(idx)
        assert a1 == [0, 2, 4], f"A1 should be [0,2,4], got {a1}"
        assert a2 == [1, 3, 5], f"A2 should be [1,3,5], got {a2}"

    def test_balance_tp5_tp1_mix(self):
        """Simulated realistic case: 6 tp5 (light) + 14 tp1 (heavy) sorted items.

        Each partition should get approximately equal heavy items.
        """
        # Sorted: 6 light then 14 heavy (as _item_priority would produce)
        light = list(range(6))
        heavy = list(range(100, 114))  # 14 items
        idx = light + heavy  # 20 total
        a1, a2 = _partition_a_indices(idx)

        # Disjoint & complete (already covered by other tests, but check here too)
        assert set(a1).isdisjoint(set(a2))
        assert set(a1) | set(a2) == set(idx)

        # Both partitions get 3 light and 7 heavy each
        a1_light = [x for x in a1 if x < 100]
        a2_light = [x for x in a2 if x < 100]
        a1_heavy = [x for x in a1 if x >= 100]
        a2_heavy = [x for x in a2 if x >= 100]

        assert len(a1_light) == 3, f"A1 should have 3 light items, got {len(a1_light)}"
        assert len(a2_light) == 3, f"A2 should have 3 light items, got {len(a2_light)}"
        assert len(a1_heavy) == 7, f"A1 should have 7 heavy items, got {len(a1_heavy)}"
        assert len(a2_heavy) == 7, f"A2 should have 7 heavy items, got {len(a2_heavy)}"

    def test_odd_total_items(self):
        """Odd number of items: A1 gets one more than A2 (ceiling)."""
        idx = list(range(7))  # 7 items
        a1, a2 = _partition_a_indices(idx)
        assert len(a1) == 4, f"A1 should have 4 items (ceiling), got {len(a1)}"
        assert len(a2) == 3, f"A2 should have 3 items (floor), got {len(a2)}"
        # Still disjoint and complete
        assert set(a1).isdisjoint(set(a2))
        assert set(a1) | set(a2) == set(idx)


# ── build_create_set_response: chan timing fields ────────────────────────────

class TestBuildCreateSetResponseTimingFields:

    def _minimal_kwargs(self, **overrides):
        base = dict(
            created=1, failed=0, launch=False, results=[],
            promo_note=None, callouts_note=None, units_block=False,
            units_switched=False, units_note=None, units_pending=0,
            deferred_id=None, deferred_at=None,
        )
        base.update(overrides)
        return base

    def test_chan_fields_present_when_none(self):
        """Response always has chan_A_wall_sec and chan_B_wall_sec keys."""
        resp = build_create_set_response(**self._minimal_kwargs())
        assert "chan_A_wall_sec" in resp
        assert "chan_B_wall_sec" in resp
        assert resp["chan_A_wall_sec"] is None
        assert resp["chan_B_wall_sec"] is None

    def test_chan_fields_with_values(self):
        """Timing values are passed through as-is."""
        resp = build_create_set_response(
            **self._minimal_kwargs(chan_A_wall_sec=1200.5, chan_B_wall_sec=350.2)
        )
        assert resp["chan_A_wall_sec"] == 1200.5
        assert resp["chan_B_wall_sec"] == 350.2

    def test_chan_fields_zero(self):
        """Zero timing (e.g. empty channel A) is valid."""
        resp = build_create_set_response(
            **self._minimal_kwargs(chan_A_wall_sec=0.0, chan_B_wall_sec=0.0)
        )
        assert resp["chan_A_wall_sec"] == 0.0
        assert resp["chan_B_wall_sec"] == 0.0


# ── Feature flag: DIRECT_TOKEN_THREADS env var logic ─────────────────────────

class TestTokenThreadsEnvVar:
    """Verify that _compute_token_threads() correctly caps DIRECT_TOKEN_THREADS (1 ≤ x ≤ 2).

    Tests call the REAL production function via monkeypatch.setenv so that
    os.environ reads are exercised end-to-end (previously the test mirrored
    the arithmetic locally and never exercised the actual env-var read path).
    """

    def test_default_is_2(self, monkeypatch):
        monkeypatch.setenv("DIRECT_TOKEN_THREADS", "2")
        assert _compute_token_threads() == 2

    def test_value_1_gives_1(self, monkeypatch):
        monkeypatch.setenv("DIRECT_TOKEN_THREADS", "1")
        assert _compute_token_threads() == 1

    def test_value_0_clamped_to_1(self, monkeypatch):
        monkeypatch.setenv("DIRECT_TOKEN_THREADS", "0")
        assert _compute_token_threads() == 1

    def test_value_3_clamped_to_2(self, monkeypatch):
        monkeypatch.setenv("DIRECT_TOKEN_THREADS", "3")
        assert _compute_token_threads() == 2

    def test_value_100_clamped_to_2(self, monkeypatch):
        monkeypatch.setenv("DIRECT_TOKEN_THREADS", "100")
        assert _compute_token_threads() == 2

    def test_value_2_preserved(self, monkeypatch):
        monkeypatch.setenv("DIRECT_TOKEN_THREADS", "2")
        assert _compute_token_threads() == 2

    def test_env_absent_defaults_to_2(self, monkeypatch):
        """Env var missing entirely → default 2."""
        monkeypatch.delenv("DIRECT_TOKEN_THREADS", raising=False)
        assert _compute_token_threads() == 2

    def test_warning_threshold_value_above_2_clamped(self, monkeypatch):
        """Values >2 are clamped (warning is a side-effect, not tested here)."""
        monkeypatch.setenv("DIRECT_TOKEN_THREADS", "5")
        assert _compute_token_threads() == 2


# ── V501 inter-thread rate limiter ───────────────────────────────────────────

class TestV501RateLimiter:
    """Verify _V501RateLimiter enforces per-token rate ceiling.

    Tests use a very small max_rate to keep wall time short while still
    exercising the timing logic reliably.
    """

    def test_single_thread_no_delay_first_call(self):
        """First acquire() should not sleep (no previous call to space from)."""
        from direct.direct_v501_client import _V501RateLimiter
        limiter = _V501RateLimiter(max_rate=1000.0)  # 1ms interval
        t0 = time.monotonic()
        limiter.acquire()
        elapsed = time.monotonic() - t0
        assert elapsed < 0.1, f"First acquire should not sleep, took {elapsed:.3f}s"

    def test_two_rapid_calls_are_throttled(self):
        """Two rapid acquire() calls must be separated by ≥ min_interval."""
        from direct.direct_v501_client import _V501RateLimiter
        max_rate = 20.0  # 50ms interval — fast enough for CI, slow enough to measure
        limiter = _V501RateLimiter(max_rate=max_rate)
        limiter.acquire()   # 1st call
        t1 = time.monotonic()
        limiter.acquire()   # 2nd call — must wait
        elapsed = time.monotonic() - t1
        min_expected = (1.0 / max_rate) * 0.8   # 80% tolerance for timing noise
        assert elapsed >= min_expected, (
            f"Expected throttle of ≥{min_expected:.3f}s, got {elapsed:.3f}s")

    def test_two_threads_combined_rate_below_limit(self):
        """Two concurrent threads must not exceed max_rate together."""
        from direct.direct_v501_client import _V501RateLimiter
        max_rate = 20.0   # 50ms interval
        n_calls_each = 5
        limiter = _V501RateLimiter(max_rate=max_rate)
        timestamps: list[float] = []
        lock = threading.Lock()

        def caller():
            for _ in range(n_calls_each):
                limiter.acquire()
                with lock:
                    timestamps.append(time.monotonic())

        t1 = threading.Thread(target=caller)
        t2 = threading.Thread(target=caller)
        t_start = time.monotonic()
        t1.start(); t2.start()
        t1.join(); t2.join()
        total_time = time.monotonic() - t_start

        # 10 calls total at max_rate=20/s should take ≥ (10-1)/20 = 0.45s
        min_expected_total = (n_calls_each * 2 - 1) / max_rate * 0.8
        assert total_time >= min_expected_total, (
            f"10 calls should take ≥{min_expected_total:.3f}s, got {total_time:.3f}s; "
            f"rate limiter not throttling correctly")

    def test_per_token_isolation(self):
        """Different tokens get independent limiters — no cross-token throttle."""
        from direct.direct_v501_client import _get_v501_limiter
        import uuid
        tok_a = f"test-token-{uuid.uuid4()}"
        tok_b = f"test-token-{uuid.uuid4()}"
        lim_a = _get_v501_limiter(tok_a)
        lim_b = _get_v501_limiter(tok_b)
        assert lim_a is not lim_b, "Different tokens must have independent limiters"
        # Same token returns the same instance
        assert _get_v501_limiter(tok_a) is lim_a, "Same token must return same limiter"

    def test_single_thread_no_extra_delay_between_spaced_calls(self):
        """At DIRECT_TOKEN_THREADS=1, calls are naturally spaced — no synthetic delay."""
        from direct.direct_v501_client import _V501RateLimiter
        max_rate = 4.5   # production default
        min_interval = 1.0 / max_rate   # ≈0.222s
        limiter = _V501RateLimiter(max_rate=max_rate)
        limiter.acquire()   # prime the limiter
        # Simulate single-thread: next call arrives after natural API latency (≥300ms >> 222ms)
        time.sleep(min_interval + 0.05)   # 272ms > 222ms — no throttle expected
        t0 = time.monotonic()
        limiter.acquire()
        elapsed = time.monotonic() - t0
        assert elapsed < 0.05, (
            f"Single-thread call arriving after natural gap should not be throttled, "
            f"got {elapsed:.3f}s")

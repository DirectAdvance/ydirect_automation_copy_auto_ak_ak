"""Tests for STAGE_TIMING per-item per-stage timing helper (direct/stage_timing.py).

Coverage:
1. stage() measures a real duration and emits ONE machine-readable line
2. exception inside the measured block is re-raised (NOT swallowed), line still written
3. item context (job/item/tp/login) lands in the line and is thread-local
4. only_in_item=True stays silent outside the create-set item context
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from direct import stage_timing as st


def _rows(capsys) -> list[dict]:
    """Parse STAGE_TIMING lines from captured stdout."""
    out = []
    for line in capsys.readouterr().out.splitlines():
        if line.startswith(st.PREFIX + " "):
            out.append(json.loads(line[len(st.PREFIX) + 1:]))
    return out


@pytest.fixture(autouse=True)
def _clean_ctx():
    st.clear_item()
    yield
    st.clear_item()


class TestStageDuration:

    def test_emits_single_line_with_measured_ms(self, capsys):
        with st.stage("AddAds"):
            time.sleep(0.05)
        rows = _rows(capsys)
        assert len(rows) == 1
        assert rows[0]["stage"] == "AddAds"
        assert rows[0]["ok"] is True
        assert 40 <= rows[0]["ms"] <= 5000     # ~50ms, верхняя граница щадящая для CI

    def test_fast_block_reports_small_ms(self, capsys):
        with st.stage("noop"):
            pass
        rows = _rows(capsys)
        assert len(rows) == 1
        assert rows[0]["ms"] < 50

    def test_line_is_parseable_json_after_prefix(self, capsys):
        st.set_item(job="4bce0676297a", login="porg-x", item=3, tp="tp1")
        with st.stage("grid:AddCampaigns"):
            pass
        line = [ln for ln in capsys.readouterr().out.splitlines()
                if ln.startswith(st.PREFIX + " ")][0]
        payload = json.loads(line.split(" ", 1)[1])
        assert payload["job"] == "4bce0676297a"
        assert payload["item"] == 3
        assert payload["tp"] == "tp1"
        assert payload["login"] == "porg-x"
        assert payload["stage"] == "grid:AddCampaigns"


class TestExceptionPropagation:

    def test_exception_is_not_swallowed(self, capsys):
        with pytest.raises(ValueError, match="boom"):
            with st.stage("AddGroups"):
                raise ValueError("boom")
        capsys.readouterr()

    def test_timing_line_written_even_on_exception(self, capsys):
        with pytest.raises(RuntimeError):
            with st.stage("AddKeywords"):
                time.sleep(0.02)
                raise RuntimeError("grid failed")
        rows = _rows(capsys)
        assert len(rows) == 1
        assert rows[0]["stage"] == "AddKeywords"
        assert rows[0]["ok"] is False
        assert rows[0]["ms"] >= 10

    def test_keyboardinterrupt_also_propagates_and_logs(self, capsys):
        with pytest.raises(KeyboardInterrupt):
            with st.stage("content_gen"):
                raise KeyboardInterrupt
        rows = _rows(capsys)
        assert len(rows) == 1 and rows[0]["ok"] is False


class TestItemContext:

    def test_context_is_thread_local(self, capsys):
        st.set_item(job="J", item=1)
        seen = {}

        def _worker():
            seen["ctx"] = st.current_item()

        t = threading.Thread(target=_worker, name="createset-chanB-cookie")
        t.start(); t.join()
        assert seen["ctx"] == {}                       # чужой поток контекст не видит
        assert st.current_item()["item"] == 1

    def test_channel_label_from_thread_name(self, capsys):
        def _worker():
            st.set_item(job="J", item=7)
            with st.stage("grid:AddAds"):
                pass

        t = threading.Thread(target=_worker, name="createset-chanA2-units")
        t.start(); t.join()
        rows = _rows(capsys)
        assert rows[0]["ch"] == "A2"

    def test_clear_item_drops_context(self, capsys):
        st.set_item(job="J", item=1)
        st.clear_item()
        with st.stage("postprocess"):
            pass
        assert "job" not in _rows(capsys)[0]


class TestOnlyInItemGate:

    def test_silent_outside_item_context(self, capsys):
        with st.stage("grid:AddAds", only_in_item=True):
            pass
        assert _rows(capsys) == []

    def test_emits_inside_item_context(self, capsys):
        st.set_item(job="J", item=2, tp="tp5")
        with st.stage("grid:AddAds", only_in_item=True):
            pass
        rows = _rows(capsys)
        assert len(rows) == 1 and rows[0]["item"] == 2

    def test_gate_does_not_swallow_exceptions(self, capsys):
        with pytest.raises(ValueError):
            with st.stage("grid:AddAds", only_in_item=True):
                raise ValueError("boom")
        assert _rows(capsys) == []                     # вне item-контекста строки нет

"""Tests for STAGE_TIMING per-item per-stage timing helper (direct/stage_timing.py).

Coverage:
1. stage() measures a real duration and emits ONE machine-readable line
2. exception inside the measured block is re-raised (NOT swallowed), line still written
3. item context (job/item/tp/login) lands in the line and is thread-local
4. only_in_item=True stays silent outside the create-set item context
5. вложенный Grid-_post из _reauth/_bootstrap_csrf НЕ мерится (иначе двойной счёт)
6. последовательная ветка оркестратора чистит item-контекст через finally (утечка при исключении)
"""
from __future__ import annotations

import ast
import inspect
import json
import textwrap
import threading
import time

import pytest

from direct.create import stage_timing as st


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


# ── #5: вложенный замер Grid при stale-cookie 403 → двойной счёт ────────────────────────
class _FakeResp:
    """Минимальный requests.Response для GridClient._post (без сети)."""

    def __init__(self, status: int = 200, csrf: str | None = None):
        self.status_code = status
        self.headers = {"Content-Type": "application/json"}
        if csrf:
            self.headers["Set-Cookie"] = f"_direct_csrf_token={csrf}; Path=/"
        self.cookies: dict = {}
        self.text = "{}"


class TestNestedGridPostNotDoubleCounted:
    """`_post` при 403 зовёт `_reauth` → `_bootstrap_csrf` → `_post('Callouts')`.

    Вложенная строка легла бы ВНУТРЬ внешней `grid:<op>` и агрегация
    `group_by(.stage)|map(ms|add)` посчитала бы это время дважды (сумма стадий > item_total).
    """

    def _client(self, monkeypatch, responses):
        from direct.clients import grid_finalize as gf
        cli = gf.GridClient("porg-test", cookie="Session_id=x")   # явная кука → без сети в __init__
        seq = list(responses)
        monkeypatch.setattr(cli.sess, "post", lambda *a, **k: seq.pop(0))
        return cli

    def test_reauth_bootstrap_post_is_not_measured(self, monkeypatch, capsys):
        cli = self._client(monkeypatch, [
            _FakeResp(403),                       # внешний AddAds: протухшая кука
            _FakeResp(200, csrf="tok123"),        # bootstrap изнутри _reauth
            _FakeResp(200),                       # ретрай внешнего AddAds
        ])
        cli.csrf = "old"                          # _had_csrf=True → ветка stale-cookie 403
        st.set_item(job="J", item=1, tp="tp1")
        cli._post("AddAds", "query", {})
        rows = _rows(capsys)
        assert [r["stage"] for r in rows] == ["grid:AddAds"]   # ни одной вложенной grid:Callouts
        assert cli.csrf == "tok123"               # reauth реально отработал, путь пройден

    def test_top_level_bootstrap_is_still_measured(self, monkeypatch, capsys):
        """Верхнеуровневый bootstrap (вне _post) — реальное время item'а, его мерим как раньше."""
        cli = self._client(monkeypatch, [_FakeResp(200, csrf="tok1")])
        st.set_item(job="J", item=1, tp="tp1")
        cli._bootstrap_csrf()
        assert [r["stage"] for r in _rows(capsys)] == ["grid:Callouts"]


# ── #6: утечка item-контекста в последовательном режиме ────────────────────────────────
def _find_if_not_parallel(tree: ast.AST):
    for node in ast.walk(tree):
        if (isinstance(node, ast.If) and isinstance(node.test, ast.UnaryOp)
                and isinstance(node.test.op, ast.Not)
                and isinstance(node.test.operand, ast.Name)
                and node.test.operand.id == "_PARALLEL"):
            return node
    return None


def _calls_clear_item(nodes) -> bool:
    """Именно `_timing.clear_item()`: по одному имени атрибута тест прошёл бы на чужом объекте."""
    for n in nodes:
        for sub in ast.walk(n):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "clear_item"
                    and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id == "_timing"):
                return True
    return False


class TestSequentialChannelClearsItemContext:
    """Последовательная ветка исполняется в ДОЛГОЖИВУЩЕМ потоке `_create_worker_loop`:
    исключение из `_run_item` мимо `clear_item()` оставило бы job/item/tp в thread-local,
    и постпроцесс/deferred repair писали бы строки с чужим item-контекстом."""

    def _seq_branch(self):
        from direct.create import create_set_orchestrator as orch
        src = textwrap.dedent(inspect.getsource(orch.create_set_response))
        node = _find_if_not_parallel(ast.parse(src))
        assert node is not None, "ветка `if not _PARALLEL:` не найдена"
        return node

    def test_item_loop_is_wrapped_in_try_finally_clear_item(self):
        seq = self._seq_branch()
        tries = [n for n in seq.body if isinstance(n, ast.Try)]
        assert tries, "цикл по items не обёрнут в try/finally"
        wrapping = [t for t in tries
                    if any(isinstance(x, ast.For) for b in t.body for x in ast.walk(b))]
        assert wrapping, "try/finally не оборачивает цикл по items"
        assert _calls_clear_item(wrapping[0].finalbody), "finally не зовёт _timing.clear_item()"

    def test_no_bare_clear_item_after_loop(self):
        """Старая форма (`clear_item()` голым стейтментом после цикла) не должна вернуться."""
        seq = self._seq_branch()
        assert not _calls_clear_item([n for n in seq.body if isinstance(n, ast.Expr)])

    def test_clear_item_semantics_on_exception(self, capsys):
        """Контракт, который защищает finally: после исключения контекст обязан быть пуст."""
        st.set_item(job="J", item=5, tp="tp1")
        with pytest.raises(RuntimeError):
            try:
                with st.stage("item_total"):
                    raise RuntimeError("item упал")
            finally:
                st.clear_item()
        assert st.current_item() == {}
        with st.stage("postprocess"):
            pass
        assert "item" not in _rows(capsys)[-1]

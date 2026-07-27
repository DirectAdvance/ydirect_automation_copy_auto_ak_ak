"""Реюз и батчинг наборов быстрых ссылок (sitelinks.add).

Мотив: прогон 69a140093e78 — `v501:sitelinks.add` 444.0с / 774 вызова (17.4% wall-clock).
Наборы пересоздавались на каждую группу/кампанию, даже когда содержимое совпадало.

Покрываем:
  • DirectV501Client.add_sitelinks_sets — N наборов за 1 вызов, позиционные id, чанкование;
  • DirectV501Client.add_sitelinks_set  — сигнатура/поведение 1:1 (int, raise, code=152);
  • automation_runtime._get_or_reuse_sitelink_set  — реюз ПО СОДЕРЖИМОМУ;
  • automation_runtime._get_or_reuse_sitelink_sets — батч + Grid-фолбэк на 152.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from direct.direct_v501_client import (
    DirectV501Client, DirectV501Error, _SITELINKS_SETS_BATCH_SIZE,
)
import direct.automation_runtime as ar


SL_A = [{"Title": "Кредит", "Href": "https://site.ru/lada#sl1", "Description": "от 9 000 ₽"}]
SL_B = [{"Title": "Трейд-ин", "Href": "https://site.ru/kia", "Description": "обмен авто"}]


def _make_client() -> DirectV501Client:
    cl = DirectV501Client.__new__(DirectV501Client)
    cl.client_login = "test-login"
    cl.timeout = 30
    cl.sess = MagicMock()
    return cl


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    monkeypatch.delenv("DIRECT_SITELINK_SET_CACHE", raising=False)
    ar._sitelink_set_cache_clear()
    yield
    ar._sitelink_set_cache_clear()


# ═══ DirectV501Client ════════════════════════════════════════════════════════

class TestClientBatch:
    def test_single_signature_unchanged(self, monkeypatch):
        """add_sitelinks_set(list) → int; в запросе ровно один набор под ключом SitelinksSets."""
        cl = _make_client()
        seen = []

        def fake_call(service, method, params):
            seen.append((service, method, params))
            return {"AddResults": [{"Id": 777}]}

        monkeypatch.setattr(cl, "_call", fake_call)
        assert cl.add_sitelinks_set(SL_A) == 777
        assert len(seen) == 1
        svc, meth, p = seen[0]
        assert (svc, meth) == ("sitelinks", "add")
        assert list(p.keys()) == ["SitelinksSets"]      # двойное s — не переименовывать
        assert len(p["SitelinksSets"]) == 1

    def test_single_strips_href_fragment(self, monkeypatch):
        """Служебный #якорь не уходит в живой Href."""
        cl = _make_client()
        captured = {}

        def fake_call(service, method, params):
            captured["p"] = params
            return {"AddResults": [{"Id": 1}]}

        monkeypatch.setattr(cl, "_call", fake_call)
        cl.add_sitelinks_set(SL_A)
        href = captured["p"]["SitelinksSets"][0]["Sitelinks"][0]["Href"]
        assert href == "https://site.ru/lada"

    def test_single_error_raises_with_code(self, monkeypatch):
        """Ошибка набора → DirectV501Error с исходным кодом (152 нужен для Grid-фолбэка)."""
        cl = _make_client()
        monkeypatch.setattr(cl, "_call", lambda s, m, p: {
            "AddResults": [{"Errors": [{"Code": 152, "Message": "нет баллов", "Details": ""}]}]})
        with pytest.raises(DirectV501Error) as e:
            cl.add_sitelinks_set(SL_A)
        assert e.value.code == 152

    def test_single_empty_add_results_raises(self, monkeypatch):
        cl = _make_client()
        monkeypatch.setattr(cl, "_call", lambda s, m, p: {"AddResults": []})
        with pytest.raises(DirectV501Error):
            cl.add_sitelinks_set(SL_A)

    def test_batch_one_call_positional_ids(self, monkeypatch):
        """3 разных набора → ОДИН sitelinks.add, id позиционно."""
        cl = _make_client()
        calls = []

        def fake_call(service, method, params):
            calls.append(params)
            return {"AddResults": [{"Id": 11}, {"Id": 22}, {"Id": 33}]}

        monkeypatch.setattr(cl, "_call", fake_call)
        res = cl.add_sitelinks_sets([SL_A, SL_B, SL_A])
        assert len(calls) == 1
        assert len(calls[0]["SitelinksSets"]) == 3
        assert [r["id"] for r in res] == [11, 22, 33]
        # содержимое не перепутано
        titles = [s["Sitelinks"][0]["Title"] for s in calls[0]["SitelinksSets"]]
        assert titles == ["Кредит", "Трейд-ин", "Кредит"]

    def test_batch_per_item_error_is_none(self, monkeypatch):
        """Ошибка одного набора не роняет батч: id=None + code вызывающему."""
        cl = _make_client()
        monkeypatch.setattr(cl, "_call", lambda s, m, p: {"AddResults": [
            {"Id": 11},
            {"Errors": [{"Code": 152, "Message": "нет баллов", "Details": ""}]},
        ]})
        res = cl.add_sitelinks_sets([SL_A, SL_B])
        assert res[0]["id"] == 11
        assert res[1]["id"] is None and res[1]["code"] == 152

    def test_batch_chunking(self, monkeypatch):
        cl = _make_client()
        calls = []

        def fake_call(service, method, params):
            calls.append(params)
            return {"AddResults": [{"Id": i} for i in range(len(params["SitelinksSets"]))]}

        monkeypatch.setattr(cl, "_call", fake_call)
        n = _SITELINKS_SETS_BATCH_SIZE + 3
        res = cl.add_sitelinks_sets([SL_A] * n)
        assert len(calls) == 2
        assert len(res) == n

    def test_batch_empty(self):
        assert _make_client().add_sitelinks_sets([]) == []


# ═══ automation_runtime: реюз по содержимому ═════════════════════════════════

class _FakeV5:
    """Считает вызовы add_sitelinks_set/add_sitelinks_sets и выдаёт растущие id."""

    def __init__(self, *_a, **_kw):
        pass

    calls_single = 0
    calls_batch = 0
    next_id = 100

    def add_sitelinks_set(self, sitelinks):
        type(self).calls_single += 1
        type(self).next_id += 1
        return type(self).next_id

    def add_sitelinks_sets(self, sets, **_kw):
        type(self).calls_batch += 1
        out = []
        for _ in sets:
            type(self).next_id += 1
            out.append({"id": type(self).next_id, "code": 0, "message": "", "details": ""})
        return out


@pytest.fixture
def fake_v5(monkeypatch):
    _FakeV5.calls_single = 0
    _FakeV5.calls_batch = 0
    _FakeV5.next_id = 100
    monkeypatch.setattr(ar.cmc, "DirectV501Client", _FakeV5)
    return _FakeV5


class TestReuseByContent:
    def test_identical_sets_one_api_call_one_id(self, fake_v5):
        """Одинаковые наборы → один вызов API, один переиспользованный id."""
        a = ar._get_or_reuse_sitelink_set("tok", "porg-x", SL_A)
        b = ar._get_or_reuse_sitelink_set("tok", "porg-x", list(SL_A))
        c = ar._get_or_reuse_sitelink_set("tok", "porg-x", [dict(SL_A[0])])
        assert a == b == c
        assert fake_v5.calls_single == 1

    def test_different_sets_get_own_ids(self, fake_v5):
        """Разные наборы → каждый получает свой id, содержимое не склеивается."""
        a = ar._get_or_reuse_sitelink_set("tok", "porg-x", SL_A)
        b = ar._get_or_reuse_sitelink_set("tok", "porg-x", SL_B)
        assert a != b
        assert fake_v5.calls_single == 2

    def test_href_differs_means_different_set(self, fake_v5):
        """Ключ реюза — содержимое: другой Href группы → отдельный набор."""
        a = ar._get_or_reuse_sitelink_set("tok", "porg-x", SL_A)
        other = [{**SL_A[0], "Href": "https://site.ru/haval"}]
        b = ar._get_or_reuse_sitelink_set("tok", "porg-x", other)
        assert a != b
        assert fake_v5.calls_single == 2

    def test_reuse_is_login_scoped(self, fake_v5):
        a = ar._get_or_reuse_sitelink_set("tok", "porg-x", SL_A)
        b = ar._get_or_reuse_sitelink_set("tok", "porg-y", SL_A)
        assert a != b
        assert fake_v5.calls_single == 2

    def test_kill_switch_restores_old_behaviour(self, fake_v5, monkeypatch):
        monkeypatch.setenv("DIRECT_SITELINK_SET_CACHE", "0")
        a = ar._get_or_reuse_sitelink_set("tok", "porg-x", SL_A)
        b = ar._get_or_reuse_sitelink_set("tok", "porg-x", SL_A)
        assert a != b
        assert fake_v5.calls_single == 2

    def test_failure_is_not_cached(self, monkeypatch):
        """Провал не кэшируется: следующий вызов пробует снова."""
        class _Boom:
            n = 0

            def __init__(self, *_a, **_kw):
                pass

            def add_sitelinks_set(self, sitelinks):
                _Boom.n += 1
                raise ar.cmc.DirectV501Error("sitelinks.add", 8000, "bad")

        monkeypatch.setattr(ar.cmc, "DirectV501Client", _Boom)
        assert ar._get_or_reuse_sitelink_set("tok", "porg-x", SL_A) is None
        assert ar._get_or_reuse_sitelink_set("tok", "porg-x", SL_A) is None
        assert _Boom.n == 2

    def test_152_falls_back_to_grid(self, monkeypatch):
        """Фолбэк 152 → Grid (без баллов) сохранён, результат кэшируется."""
        class _NoUnits:
            def __init__(self, *_a, **_kw):
                pass

            def add_sitelinks_set(self, sitelinks):
                raise ar.cmc.DirectV501Error("sitelinks.add", 152, "нет баллов")

        grid = MagicMock()
        grid.add_sitelink_set.return_value = 555
        monkeypatch.setattr(ar.cmc, "DirectV501Client", _NoUnits)
        monkeypatch.setattr(ar.gf, "get_grid_client", lambda *a, **kw: grid)

        assert ar._get_or_reuse_sitelink_set("tok", "porg-x", SL_A) == 555
        assert ar._get_or_reuse_sitelink_set("tok", "porg-x", SL_A) == 555
        assert grid.add_sitelink_set.call_count == 1


class TestBatchHelper:
    def test_batch_dedups_and_uses_one_call(self, fake_v5):
        """5 наборов, 2 уникальных → 1 батч-вызов, дубли получают тот же id."""
        sets = [SL_A, SL_B, SL_A, SL_B, SL_A]
        res = ar._get_or_reuse_sitelink_sets("tok", "porg-x", sets)
        assert fake_v5.calls_batch == 1
        assert fake_v5.calls_single == 0
        assert res[0] == res[2] == res[4]
        assert res[1] == res[3]
        assert res[0] != res[1]

    def test_batch_uses_existing_cache(self, fake_v5):
        first = ar._get_or_reuse_sitelink_set("tok", "porg-x", SL_A)
        res = ar._get_or_reuse_sitelink_sets("tok", "porg-x", [SL_A, SL_B])
        assert res[0] == first          # взят из кэша, повторно не создавался
        assert fake_v5.calls_batch == 1
        assert len(res) == 2

    def test_batch_152_per_item_grid_fallback(self, monkeypatch):
        class _Mixed:
            def __init__(self, *_a, **_kw):
                pass

            def add_sitelinks_sets(self, sets, **_kw):
                return [{"id": 11, "code": 0, "message": "", "details": ""},
                        {"id": None, "code": 152, "message": "нет баллов", "details": ""}]

        grid = MagicMock()
        grid.add_sitelink_set.return_value = 999
        monkeypatch.setattr(ar.cmc, "DirectV501Client", _Mixed)
        monkeypatch.setattr(ar.gf, "get_grid_client", lambda *a, **kw: grid)

        res = ar._get_or_reuse_sitelink_sets("tok", "porg-x", [SL_A, SL_B])
        assert res == [11, 999]

    def test_batch_transport_failure_falls_back_to_singles(self, monkeypatch):
        class _Broken:
            singles = 0

            def __init__(self, *_a, **_kw):
                pass

            def add_sitelinks_sets(self, sets, **_kw):
                raise RuntimeError("connection reset")

            def add_sitelinks_set(self, sitelinks):
                _Broken.singles += 1
                return 300 + _Broken.singles

        monkeypatch.setattr(ar.cmc, "DirectV501Client", _Broken)
        res = ar._get_or_reuse_sitelink_sets("tok", "porg-x", [SL_A, SL_B])
        assert res == [301, 302]
        assert _Broken.singles == 2

    def test_batch_dups_get_id_even_with_cache_off(self, fake_v5, monkeypatch):
        """Kill-switch выключает КРОСС-вызовный реюз, но не ломает дубли внутри батча:
        одинаковые наборы одного вызова обязаны получить id, а не None."""
        monkeypatch.setenv("DIRECT_SITELINK_SET_CACHE", "0")
        res = ar._get_or_reuse_sitelink_sets("tok", "porg-x", [SL_A, SL_B, SL_A])
        assert res[0] is not None and res[0] == res[2]
        assert res[1] is not None and res[1] != res[0]
        assert fake_v5.calls_batch == 1

    def test_batch_empty_and_holes(self, fake_v5):
        assert ar._get_or_reuse_sitelink_sets("tok", "porg-x", []) == []
        res = ar._get_or_reuse_sitelink_sets("tok", "porg-x", [SL_A, [], SL_B])
        assert res[1] is None
        assert res[0] and res[2] and res[0] != res[2]

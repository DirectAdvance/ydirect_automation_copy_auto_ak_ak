"""Ретрай Grid-мутаций не должен плодить дубли (RETRY_ON_NETWORK_LOSS_DUPLICATES_ADD).

Боевой инцидент: porg-nqavjicg, кампания 713102313 — 14 пустых групп-сирот
(5777472935..948) рядом с 14 полноценными (5777472963..976): первый AddUnifiedAdGroups
закоммитился, но вернул транзиент → слепой повтор в `_mutate` создал второй блок.
"""
import pytest
import requests

from direct import grid_create as gc


def _client(monkeypatch):
    monkeypatch.setattr(gc.time, "sleep", lambda *_a, **_k: None)
    return gc.GridCreateClient("porg-test", cookie="stub=1")


def _items(n: int, cid: str = "713102313"):
    return [{"campaignId": cid, "name": f"Группа {i}"} for i in range(n)]


def _transient():
    return gc.GridCreateError(
        "AddUnifiedAdGroups: Внутренняя ошибка сервера, reqId=1", transient=True)


# ── 1. транзиент ПОСЛЕ фактического коммита → повторного создания нет ───────────────────
def test_transient_after_commit_does_not_recreate_groups(monkeypatch):
    cl = _client(monkeypatch)
    calls = []

    def fake_once(items):
        calls.append(list(items))
        raise _transient()

    live = {f"Группа {i}": 5777472963 + i for i in range(14)}
    monkeypatch.setattr(cl, "_add_adgroups_once", fake_once)
    monkeypatch.setattr(cl, "_read_adgroup_name_to_id_strict", lambda cid: dict(live))

    out = cl.add_adgroups(_items(14))

    assert out == [5777472963 + i for i in range(14)]
    assert len(calls) == 1, "мутация повторена вслепую → дубли групп"


# ── 2. реальная потеря ДО коммита → объекты всё-таки создаются ──────────────────────────
def test_lost_response_before_commit_still_creates_groups(monkeypatch):
    cl = _client(monkeypatch)
    tries = {"n": 0}

    def fake_once(items):
        tries["n"] += 1
        if tries["n"] == 1:
            raise requests.ConnectionError("connection reset by peer")
        return [900 + i for i in range(len(items))]

    monkeypatch.setattr(cl, "_add_adgroups_once", fake_once)
    monkeypatch.setattr(cl, "_read_adgroup_name_to_id_strict", lambda cid: {})

    out = cl.add_adgroups(_items(4))

    assert out == [900, 901, 902, 903]
    assert tries["n"] == 2, "коммита не было — повтор обязан состояться"


# ── 3. частичный коммит → досоздаются ТОЛЬКО отсутствующие ─────────────────────────────
def test_partial_commit_creates_only_missing_groups(monkeypatch):
    cl = _client(monkeypatch)
    sent = []

    def fake_once(items):
        sent.append([it["name"] for it in items])
        if len(sent) == 1:
            raise _transient()
        return [777 + i for i in range(len(items))]

    live = {"Группа 0": 100, "Группа 1": 101, "Группа 2": 102}
    monkeypatch.setattr(cl, "_add_adgroups_once", fake_once)
    monkeypatch.setattr(cl, "_read_adgroup_name_to_id_strict", lambda cid: dict(live))

    out = cl.add_adgroups(_items(5))

    assert sent[1] == ["Группа 3", "Группа 4"]
    assert out == [100, 101, 102, 777, 778]


# ── 4. состояние не прочиталось → вслепую не пересоздаём, ошибка наружу ────────────────
def test_unreadable_state_propagates_error_without_recreate(monkeypatch):
    cl = _client(monkeypatch)
    calls = []

    def fake_once(items):
        calls.append(list(items))
        raise _transient()

    def boom(cid):
        raise gc.GridCreateError("AdGroupNames: не-JSON HTTP 502", transient=True)

    monkeypatch.setattr(cl, "_add_adgroups_once", fake_once)
    monkeypatch.setattr(cl, "_read_adgroup_name_to_id_strict", boom)

    with pytest.raises(gc.GridCreateError):
        cl.add_adgroups(_items(3))
    assert len(calls) == 1


# ── 5. ошибка ВАЛИДАЦИИ не транзиентна → прежнее поведение, сверка не запускается ──────
def test_validation_error_is_not_reconciled(monkeypatch):
    cl = _client(monkeypatch)
    reads = []

    def fake_once(items):
        raise gc.GridCreateError("AddUnifiedAdGroups validation: BAD_NAME")

    monkeypatch.setattr(cl, "_add_adgroups_once", fake_once)
    monkeypatch.setattr(cl, "_read_adgroup_name_to_id_strict",
                        lambda cid: reads.append(cid) or {})

    with pytest.raises(gc.GridCreateError):
        cl.add_adgroups(_items(2))
    assert reads == []


# ── 6. _mutate: Add* не ретраится вслепую, идемпотентные операции ретраятся как раньше ──
def _stub_response(payload: dict):
    class _R:
        status_code = 200
        headers: dict = {}
        cookies: dict = {}
        text = ""

        def json(self):
            return payload

    return _R()


def test_mutate_does_not_retry_add_operations(monkeypatch):
    cl = _client(monkeypatch)
    posts = []
    payload = {"errors": [{"message": "Внутренняя ошибка сервера, reqId=42"}]}

    def fake_post(op, query, variables):
        posts.append(op)
        return _stub_response(payload)

    cl.csrf = "tok"
    monkeypatch.setattr(cl, "_post", fake_post)

    with pytest.raises(gc.GridCreateError) as e1:
        cl._mutate("AddUnifiedAdGroups", "q", {})
    assert posts == ["AddUnifiedAdGroups"], "Add* повторён при транзиенте"
    assert getattr(e1.value, "transient", False) is True

    posts.clear()
    with pytest.raises(gc.GridCreateError):
        cl._mutate("AdGroupNames", "q", {})
    assert posts == ["AdGroupNames"] * 3, "идемпотентное чтение потеряло ретрай"

    posts.clear()
    with pytest.raises(gc.GridCreateError):      # AddKeywords: Директ схлопывает дубли фраз
        cl._mutate("AddKeywords", "q", {})
    assert posts == ["AddKeywords"] * 3


# ── 7. ретрай CAMPAIGN_NOT_FOUND (eventual consistency) продолжает работать ────────────
def test_campaign_not_found_retry_still_works(monkeypatch):
    cl = _client(monkeypatch)
    calls = {"n": 0}

    def fake_mutate(op, query, variables):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"data": {"addUnifiedAdGroups": {
                "validationResult": {"errors": [{"code": "CAMPAIGN_NOT_FOUND"}]}}}}
        return {"data": {"addUnifiedAdGroups": {
            "addedAdGroupItems": [{"adGroupId": "555"}, {"adGroupId": "556"}]}}}

    monkeypatch.setattr(cl, "_mutate", fake_mutate)

    assert cl.add_adgroups(_items(2)) == [555, 556]
    assert calls["n"] == 2


# ── 8. гейт «создано групп ≠ отправлено» ───────────────────────────────────────────────
def test_gate_groups_created_reports_mismatch():
    rep = {"groups": 12, "errors": []}
    gc._gate_groups_created(rep, 14)
    assert rep["errors"] == [
        "группы(AddUnifiedAdGroups): создано 12 из 14 отправленных"]

    ok = {"groups": 14, "errors": []}
    gc._gate_groups_created(ok, 14)
    assert ok["errors"] == []


def test_create_full_marks_group_count_mismatch(monkeypatch):
    monkeypatch.setattr(gc.time, "sleep", lambda *_a, **_k: None)

    class _FakeCl:
        def __init__(self, login, cookie=None, **_kw):
            self.login = login

        def _bootstrap_csrf(self):
            return None

        def add_campaign(self, spec):  # noqa: ARG002
            return 713102313

        def add_adgroups(self, items):  # noqa: ARG002
            return [111, 222]                     # третья группа не создана

        def _read_adgroup_name_to_id(self, cid):  # noqa: ARG002
            return {"A": 111, "B": 222}

        def add_keywords(self, items):  # noqa: ARG002
            return []

        def add_ads(self, items):  # noqa: ARG002
            return []

        def _read_ads_agid_map(self, cid):  # noqa: ARG002
            return {}

    monkeypatch.setattr(gc, "GridCreateClient", _FakeCl)
    spec = {"name": "тест", "counter_id": 1, "goal_id": 2, "cpa": 100,
            "weekly_budget": 3000, "start_date": "2026-07-28"}
    rep = gc.create_full(
        "porg-test", campaign_spec=spec,
        groups=[{"name": n, "keywords": [], "titles": ["t"], "texts": ["b"]}
                for n in ("A", "B", "C")],
        region_ids=[213], href="https://example.com")

    assert rep["groups"] == 2
    assert any("создано 2 из 3 отправленных" in e for e in rep["errors"])

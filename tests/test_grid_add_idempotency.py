"""Ретрай Grid-мутаций не должен плодить дубли (RETRY_ON_NETWORK_LOSS_DUPLICATES_ADD).

Боевой инцидент: porg-nqavjicg, кампания 713102313 — 14 пустых групп-сирот
(5777472935..948) рядом с 14 полноценными (5777472963..976): первый AddUnifiedAdGroups
закоммитился, но вернул транзиент → слепой повтор в `_mutate` создал второй блок.

Второй слой (ревью 2026-07-28): запрет слепого повтора без сверки факта = потеря позиции,
поэтому сверка есть у AddCampaigns / AddUnifiedAdGroups / AddAdaptiveTextAds / AddShoppingAds,
а расхождение «создано ≠ отправлено» видно верификатору, но НЕ сносит кампанию.
"""
import pytest
import requests

from direct import grid_create as gc
from direct import create_set_feed_builders as csfb
from direct.local_result_verifier import verify_local_result


def _client(monkeypatch):
    monkeypatch.setattr(gc.time, "sleep", lambda *_a, **_k: None)
    return gc.GridCreateClient("porg-test", cookie="stub=1")


def _items(n: int, cid: str = "713102313"):
    return [{"campaignId": cid, "name": f"Группа {i}"} for i in range(n)]


def _transient(op: str = "AddUnifiedAdGroups"):
    return gc.GridCreateError(f"{op}: Внутренняя ошибка сервера, reqId=1", transient=True)


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

    out = cl.add_adgroups(_items(14), campaign_is_new=True)

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

    out = cl.add_adgroups(_items(4), campaign_is_new=True)

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

    out = cl.add_adgroups(_items(5), campaign_is_new=True)

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
        cl.add_adgroups(_items(3), campaign_is_new=True)
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
        cl.add_adgroups(_items(2), campaign_is_new=True)
    assert reads == []


# ── 5b. Minor-4: одноимённые группы В РАЗНЫХ ЧАНКАХ одного вызова → сверка отключается ──
def test_duplicate_name_across_chunks_is_not_treated_as_created(monkeypatch):
    """Слепки с коллизиями `gk` дают ГРУППЫ С ОДИНАКОВЫМИ ИМЕНАМИ в одной кампании
    (194 таких на porg-nqavjicg). Имя, созданное чанком 1, нельзя засчитать чанку 2:
    иначе группа не создастся, а её ключи и объявления уедут на чужой adGroupId."""
    cl = _client(monkeypatch)
    monkeypatch.setattr(gc, "_GRID_MUTATION_CHUNK", 2)
    sent = []

    def fake_once(items):
        sent.append([it["name"] for it in items])
        if len(sent) == 1:
            return [100, 101]
        raise _transient()

    monkeypatch.setattr(cl, "_add_adgroups_once", fake_once)
    monkeypatch.setattr(cl, "_read_adgroup_name_to_id_strict",
                        lambda cid: {"Группа 0": 100, "Группа 1": 101})

    items = _items(3) + [{"campaignId": "713102313", "name": "Группа 0"}]
    with pytest.raises(gc.GridCreateError):
        cl.add_adgroups(items, campaign_is_new=True)
    assert len(sent) == 2, "после неоднозначной сверки не должно быть ни повтора, ни досоздания"


# ── 5c. Minor-4: имя было занято ЕЩЁ ДО мутации → сверка по имени неоднозначна ─────────
def test_name_existing_before_mutation_disables_reconcile(monkeypatch):
    cl = _client(monkeypatch)
    calls = []
    reads = {"n": 0}

    def fake_once(items):
        calls.append(list(items))
        raise _transient()

    def fake_read(cid):
        reads["n"] += 1
        return {"Группа 0": 555}       # группа с таким именем была в кампании ещё до вызова

    monkeypatch.setattr(cl, "_add_adgroups_once", fake_once)
    monkeypatch.setattr(cl, "_read_adgroup_name_to_id_strict", fake_read)

    with pytest.raises(gc.GridCreateError):
        cl.add_adgroups(_items(2))     # снимок ДО мутации снимается сам (кампания не новая)
    assert len(calls) == 1
    assert reads["n"] == 1, "после предмутационного снимка сверка не должна продолжаться"


# ── 6. _mutate: создающие мутации не ретраятся, идемпотентные ретраятся как раньше ──────
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

    posts.clear()
    with pytest.raises(gc.GridCreateError):      # Delete* идемпотентно по id
        cl._mutate("DeleteCampaigns", "q", {})
    assert posts == ["DeleteCampaigns"] * 3


def test_unknown_mutation_is_treated_as_creating(monkeypatch):
    """Minor-3: классификация — allow-list идемпотентных, а не префикс `Add`
    (как yandex_gateway._creates_objects: неизвестный метод = создающий, дубль дороже отказа)."""
    assert gc.GridCreateClient._creates_objects("CopyCampaigns") is True
    assert gc.GridCreateClient._creates_objects("CreateSitelinkSet") is True
    assert gc.GridCreateClient._creates_objects("AddCampaigns") is True
    assert gc.GridCreateClient._creates_objects("AdGroupNames") is False
    assert gc.GridCreateClient._creates_objects("AdsAgid") is False
    assert gc.GridCreateClient._creates_objects("CampaignNames") is False
    assert gc.GridCreateClient._creates_objects("AddKeywords") is False
    assert gc.GridCreateClient._creates_objects("DeleteCampaigns") is False


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

    assert cl.add_adgroups(_items(2), campaign_is_new=True) == [555, 556]
    assert calls["n"] == 2


# ── 8. гейт «создано групп ≠ отправлено»: ВИДНО, но НЕ разрушительно ───────────────────
def test_gate_groups_created_reports_mismatch_as_warning():
    rep = {"groups": 12, "errors": []}
    gc._gate_groups_created(rep, 14)
    assert rep["errors"] == [], "расхождение в errors = приговор кампании в куки-пути tp2/tp4"
    assert rep["warnings"] == [
        "группы(AddUnifiedAdGroups): создано 12 из 14 отправленных"]
    assert rep["groups_expected"] == 14
    assert rep["groups_shortfall"] == 2

    ok = {"groups": 14, "errors": []}
    gc._gate_groups_created(ok, 14)
    assert ok["errors"] == []
    assert ok.get("warnings") is None
    assert ok["groups_expected"] == 14


def test_create_full_marks_group_count_mismatch(monkeypatch):
    monkeypatch.setattr(gc.time, "sleep", lambda *_a, **_k: None)

    class _FakeCl:
        def __init__(self, login, cookie=None, **_kw):
            self.login = login

        def _bootstrap_csrf(self):
            return None

        def add_campaign(self, spec):  # noqa: ARG002
            return 713102313

        def add_adgroups(self, items, **_kw):  # noqa: ARG002
            return [111, 222]                     # третья группа не создана

        def _read_adgroup_name_to_id(self, cid):  # noqa: ARG002
            return {"A": 111, "B": 222}

        def add_keywords(self, items):  # noqa: ARG002
            return []

        def add_ads(self, items, **_kw):  # noqa: ARG002
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
    assert rep["groups_expected"] == 3
    assert any("создано 2 из 3 отправленных" in w for w in rep.get("warnings") or [])
    assert not any("отправленных" in e for e in rep["errors"])


# ── 9. КОНСЬЮМЕР: 13 из 14 → кампания НЕ удаляется, расхождение видно ──────────────────
def _stub_cookie_deps(monkeypatch, rep: dict, deleted: list):
    fake_gc = type("_GcNs", (), {"create_full": staticmethod(lambda *a, **k: rep)})
    for name, value in {
        "gc": fake_gc,
        "_account_image_map": lambda login: {},
        "_pack_groups_with_retry": lambda *a, **k: (
            [{"name": f"Группа {i}", "keywords": ["к"], "titles": ["t"], "texts": ["b"]}
             for i in range(14)], True),
        "_grid_bid_modifiers": lambda *a, **k: {},
        "_SLEPOK_MINUS_MODE": {},
        "_account_offer_prices": lambda *a, **k: {},
        "_group_ad_price": lambda *a, **k: (0, 0),
        "_delete_partial_campaign": lambda token, login, cid: deleted.append(cid),
        "_grid_callout_ids": lambda *a, **k: [],
        "_norm_sitelinks_for_v501": lambda *a, **k: [],
        "_finalize_search_via_grid": lambda *a, **k: None,
        "_search_platforms": lambda tp: {},
        "_grid_update_adaptive_ads": lambda *a, **k: 0,
        "_grid_ad_price_payload": lambda *a, **k: None,
        "_ct_segment": lambda ct: "",
    }.items():
        monkeypatch.setattr(csfb, name, value, raising=False)


def test_cookie_tp2_keeps_campaign_when_13_of_14_groups_created(monkeypatch):
    """Гейт числа групп НЕ должен сносить кампанию с 13 рабочими группами.

    `_create_text_via_cookie` при непустом rep["errors"] зовёт `_delete_partial_campaign`
    и уводит позицию в defer — поэтому расхождение живёт в warnings/groups_expected.
    """
    deleted: list = []
    rep = {"campaign_id": 713102313, "groups": 13, "ads": 13, "keywords": 500,
           "ad_ids": [], "adgroup_ids": [], "prices_set": 0, "errors": []}
    gc._gate_groups_created(rep, 14)     # ровно тот же гейт, что в create_full
    _stub_cookie_deps(monkeypatch, rep, deleted)

    res = csfb._create_text_via_cookie(
        "porg-test", "РК тест", "tp2", 1, 2, 500, 5000, [213], "https://example.com",
        "terehov", "Мультибренд", "r0000", ["t"], ["b"], pay="cpc", autotarget=False)

    assert res["ok"] is True
    assert deleted == [], "кампания с 13 рабочими группами удалена"
    assert res.get("partial_deleted") is None
    assert res.get("defer") is None
    assert res["build"]["groups"] == 13
    assert res["build"]["groups_expected"] == 14
    assert any("13 из 14" in w for w in res["build"]["warnings"])

    issues = verify_local_result({"name": res["name"], "result": res})
    codes = {(i["code"], i["severity"]) for i in issues}
    assert ("GROUPS_CREATED_LESS_THAN_SENT", "error") in codes, \
        "расхождение обязано остаться видимым для верификации/has_issues"


def test_verifier_silent_when_all_groups_created():
    row = {"name": "РК", "result": {"build": {"groups": 14, "groups_expected": 14,
                                              "ads": 14, "keywords": 10}}}
    assert [i for i in verify_local_result(row)
            if i["code"] == "GROUPS_CREATED_LESS_THAN_SENT"] == []


# ── 10. AddCampaigns: сверка факта вместо отказа/дубля ─────────────────────────────────
def test_add_campaign_reuses_committed_campaign_on_lost_response(monkeypatch):
    cl = _client(monkeypatch)
    calls = []

    def fake_once(spec):
        calls.append(spec)
        raise _transient("AddCampaigns")

    monkeypatch.setattr(cl, "_add_campaign_once", fake_once)
    monkeypatch.setattr(cl, "_read_campaign_ids_by_name_strict", lambda nm: [713102313])
    monkeypatch.setattr(cl, "_read_adgroup_name_to_id_strict", lambda cid: {})

    assert cl.add_campaign({"name": "РК тест"}) == 713102313
    assert len(calls) == 1, "кампания создана повторно → дубль"


def test_add_campaign_retries_when_nothing_committed(monkeypatch):
    cl = _client(monkeypatch)
    tries = {"n": 0}

    def fake_once(spec):
        tries["n"] += 1
        if tries["n"] == 1:
            raise requests.ConnectionError("reset")
        return 713102400

    monkeypatch.setattr(cl, "_add_campaign_once", fake_once)
    monkeypatch.setattr(cl, "_read_campaign_ids_by_name_strict", lambda nm: [])

    assert cl.add_campaign({"name": "РК тест"}) == 713102400
    assert tries["n"] == 2


@pytest.mark.parametrize("found,groups", [([1, 2], {}), ([1], {"Группа 0": 9})])
def test_add_campaign_refuses_ambiguous_readback(monkeypatch, found, groups):
    """Одноимённые кампании ИЛИ найденная кампания уже с группами (значит не наша свежая)
    → исходная ошибка наружу, вслепую не пересоздаём."""
    cl = _client(monkeypatch)
    calls = []

    def fake_once(spec):
        calls.append(spec)
        raise _transient("AddCampaigns")

    monkeypatch.setattr(cl, "_add_campaign_once", fake_once)
    monkeypatch.setattr(cl, "_read_campaign_ids_by_name_strict", lambda nm: list(found))
    monkeypatch.setattr(cl, "_read_adgroup_name_to_id_strict", lambda cid: dict(groups))

    with pytest.raises(gc.GridCreateError):
        cl.add_campaign({"name": "РК тест"})
    assert len(calls) == 1


# ── 11. AddAdaptiveTextAds / AddShoppingAds: сверка факта по adGroupId ─────────────────
def _ad_items(n: int):
    return [{"adGroupId": str(500 + i)} for i in range(n)]


def test_add_ads_does_not_duplicate_after_commit(monkeypatch):
    cl = _client(monkeypatch)
    calls = []

    def fake_once(items, save_draft=True):  # noqa: ARG001
        calls.append(list(items))
        raise _transient("AddAdaptiveTextAds")

    monkeypatch.setattr(cl, "_add_ads_once", fake_once)
    monkeypatch.setattr(cl, "_read_ads_agid_map_strict",
                        lambda cid: {"500": 11, "501": 12, "502": 13})

    assert cl.add_ads(_ad_items(3), campaign_id=713102313) == [11, 12, 13]
    assert len(calls) == 1


def test_add_ads_recreates_only_missing(monkeypatch):
    cl = _client(monkeypatch)
    sent = []

    def fake_once(items, save_draft=True):  # noqa: ARG001
        sent.append([it["adGroupId"] for it in items])
        if len(sent) == 1:
            raise _transient("AddAdaptiveTextAds")
        return [99]

    monkeypatch.setattr(cl, "_add_ads_once", fake_once)
    monkeypatch.setattr(cl, "_read_ads_agid_map_strict", lambda cid: {"500": 11, "501": 12})

    assert cl.add_ads(_ad_items(3), campaign_id=713102313) == [11, 12, 99]
    assert sent[1] == ["502"], "досоздаём только группу без объявления"


def test_add_ads_without_campaign_id_propagates_error(monkeypatch):
    """Внешний вызов без campaign_id сверить нечем → прежнее поведение (ошибка наружу,
    без слепого повтора)."""
    cl = _client(monkeypatch)
    calls = []

    def fake_once(items, save_draft=True):  # noqa: ARG001
        calls.append(list(items))
        raise _transient("AddAdaptiveTextAds")

    monkeypatch.setattr(cl, "_add_ads_once", fake_once)
    with pytest.raises(gc.GridCreateError):
        cl.add_ads(_ad_items(2))
    assert len(calls) == 1


def test_add_shopping_ads_reconciles_like_text_ads(monkeypatch):
    cl = _client(monkeypatch)
    calls = []

    def fake_once(items, save_draft=True):  # noqa: ARG001
        calls.append(list(items))
        raise _transient("AddShoppingAds")

    monkeypatch.setattr(cl, "_add_shopping_ads_once", fake_once)
    monkeypatch.setattr(cl, "_read_ads_agid_map_strict", lambda cid: {"500": 77})

    assert cl.add_shopping_ads(_ad_items(1), campaign_id=713102313) == [77]
    assert len(calls) == 1

"""Ревью-раунд 2 по коммитам dda2ec09 (минус-наборы) и 2a087591 (аудитории).

Покрывает шесть Important и четыре Minor:
  Imp1  tp2/tp4 на token-пути получают аудитории (_build_text_from_pack, не только куки-фолбэк);
  Imp2  поиск/сеть считается по КАНАЛУ кампании (spec/mode), а не по списку tp-кодов → tp5 в
        searchRetargetings;
  Imp3  >3 наборов минус-фраз склеиваются в 3 по 4096 симв. без пробелов, детерминированно,
        без тихой потери фраз;
  Imp4  усечение до 3 наборов — только на валидации Директа по наборам, транзиент → полный ретрай;
  Imp5  частичный провал привязки виден в ошибках джобы;
  Imp6  реюз набора по имени сверяет СОСТАВ с кабинетом;
  Minor 7 region доезжает до гео-гарда, 8 единая карта режимов минусов,
  Minor 10 пустая карта условий видна в итогах джобы, 11 сбой чтения структуры логируется.
"""

import inspect
import json
import pathlib
import types

import pytest

from direct import create_set_audiences as csa
from direct import create_set_minus as csm


# ── Imp2: канал кампании ─────────────────────────────────────────────────────────────────────

def test_channel_from_campaign_spec_matches_cookie_path():
    """Тот же расчёт, что grid_create.create_full: search and not network."""
    assert csa.is_search_channel({"search": True, "network": False}) is True
    assert csa.is_search_channel({"search": False, "network": True}) is False
    # спек tp5 (create_set_feed_builders.py:613, is_rsya=False)
    assert csa.is_search_channel({"network": False, "search": True, "organic": True}) is True


def test_channel_from_v501_mode():
    # tp1 — UnifiedCampaignSpec.mode
    assert csa.is_search_channel(mode="network_cpa") is False
    assert csa.is_search_channel(mode="network_payconv") is False
    # tp5/tp2/tp4 — _create_search_test_campaign(mode="search")
    assert csa.is_search_channel(mode="search") is True


def test_channel_fallback_by_tp_puts_tp5_on_search():
    """Регресс Imp2: по списку ("tp2","tp4") поисковая tp5 уезжала в СЕТЕВОЕ поле."""
    assert csa.is_search_channel(tp_code="tp5") is True
    assert csa.is_search_channel(tp_code="tp3") is True
    assert csa.is_search_channel(tp_code="tp2") is True
    assert csa.is_search_channel(tp_code="tp4") is True
    assert csa.is_search_channel(tp_code="tp1") is False


def test_spec_wins_over_mode_and_tp():
    assert csa.is_search_channel({"search": True, "network": False},
                                 mode="network_cpa", tp_code="tp1") is True


def test_tp5_audiences_land_in_search_retargetings():
    """Сквозной payload: канал tp5 = поиск → searchRetargetings, сетевое поле пустое."""
    from direct import grid_create_payloads as gcp

    on_search = csa.is_search_channel(mode="search", tp_code="tp5")
    item = gcp.build_adgroup(campaign_id=1, name="g", region_ids=[225], keywords=[],
                             retargeting_ids=["40803144"], retargeting_on_search=on_search)
    assert item["searchRetargetings"] == [{"retCondId": "40803144", "id": None}]
    assert item["retargetings"] == []


def test_tp1_audiences_stay_in_network_retargetings():
    from direct import grid_create_payloads as gcp

    on_search = csa.is_search_channel(mode="network_cpa", tp_code="tp1")
    item = gcp.build_adgroup(campaign_id=1, name="g", region_ids=[225], keywords=[],
                             retargeting_ids=["40803144"], retargeting_on_search=on_search)
    assert item["retargetings"] == [{"retCondId": "40803144", "id": None}]
    assert item["searchRetargetings"] == []


def test_token_paths_pass_campaign_mode_from_the_spec_they_created_with():
    """Каналы не угадываются в билдере: mode приходит от вызывающего, создавшего кампанию."""
    from direct import create_set_feed_builders as fb
    from direct import create_set_tp1_builders as tb

    src_tp1 = inspect.getsource(tb._create_tp1_single)
    assert "campaign_mode=mode" in src_tp1, "tp1 обязан отдать mode СВОЕЙ спеки"

    src_fb = pathlib.Path(fb.__file__).read_text(encoding="utf-8")
    assert src_fb.count('campaign_mode="search"') >= 2, "tp5 и tp2/tp4 token-пути"


# ── Imp1: аудитории tp2/tp4 на основном (token) пути ────────────────────────────────────────

def test_text_builder_attaches_audiences_by_gk(monkeypatch):
    """_build_text_from_pack обязан звать attach_to_group по gk — как token-путь tp1/tp5."""
    from direct import create_set_text_builders as t

    src = inspect.getsource(t._build_text_from_pack)
    assert "_aud_by_gk = _cs_aud.struct_audiences_by_gk(slepok, site_type, tp_code)" in src
    assert "_cs_aud.attach_to_group(_g_new, login, _aud_by_gk.get(_gk) if _gk else None)" in src
    # и полученное поле реально уезжает в payload группы
    assert 'retargeting_ids=g.get("audiences")' in inspect.getsource(t._build_tp2_adgroups)


def test_search_channel_flag_reaches_tp2_adgroups():
    from direct import create_set_text_builders as t

    sig = inspect.signature(t._build_tp2_adgroups)
    assert sig.parameters["search_channel"].default is True
    assert "search_channel=_cs_aud.is_search_channel(" in inspect.getsource(t._build_text_from_pack)


# ── Imp3: склейка наборов в 3 ────────────────────────────────────────────────────────────────

def _sets(sizes):
    """Наборы с фразами заданной длины (символы без пробелов == len слова)."""
    out = []
    for i, total in enumerate(sizes):
        # фраза «ф{номер:06d}» = 7 символов; добираем ровно total символов
        n = total // 7
        out.append({"name": f"набор{i}",
                    "phrases": [f"ф{i}{j:05d}" for j in range(n)]})
    return out


def test_pack_merges_four_sets_into_three_without_losing_phrases():
    """Живой kuderko/«С пробегом»: 4 набора 4020/1586/2415/4045 симв. → 3 набора, 0 потерь."""
    src = _sets([4020, 1586, 2415, 4045])
    total = [p for s in src for p in s["phrases"]]
    packed, leftover = csm._pack_minus_sets(src, csm._MINUS_SHARED_SET_CHAR_BUDGET,
                                            csm._MINUS_LIB_MAX_SETS_PER_CAMPAIGN)
    assert leftover == [], "фразы не должны теряться — 12 066 симв. влезают в 3×4096"
    assert len(packed) <= csm._MINUS_LIB_MAX_SETS_PER_CAMPAIGN
    got = [p for b in packed for p in b["phrases"]]
    assert sorted(got) == sorted(total)
    for b in packed:
        assert csm._set_chars(b["phrases"]) <= csm._MINUS_SHARED_SET_CHAR_BUDGET


def test_pack_is_deterministic_including_names():
    src = _sets([4020, 1586, 2415, 4045])
    a, _ = csm._pack_minus_sets(src, csm._MINUS_SHARED_SET_CHAR_BUDGET, 3)
    b, _ = csm._pack_minus_sets(_sets([4020, 1586, 2415, 4045]),
                                csm._MINUS_SHARED_SET_CHAR_BUDGET, 3)
    assert [x["name"] for x in a] == [x["name"] for x in b]
    assert [x["phrases"] for x in a] == [x["phrases"] for x in b]


def test_pack_names_are_unique_and_mention_sources():
    packed, _ = csm._pack_minus_sets(_sets([4020, 1586, 2415, 4045]),
                                     csm._MINUS_SHARED_SET_CHAR_BUDGET, 3)
    names = [b["name"] for b in packed]
    assert len(set(n.lower() for n in names)) == len(names), names
    assert all(len(n) <= 255 for n in names)
    assert any("набор0" in n for n in names)


def test_pack_dedups_phrases_across_sets():
    src = [{"name": "A", "phrases": ["один", "два"]},
           {"name": "B", "phrases": ["ДВА", "три"]},
           {"name": "C", "phrases": ["три", "четыре"]},
           {"name": "D", "phrases": ["пять"]}]
    packed, leftover = csm._pack_minus_sets(src, 4096, 3)
    got = [p for b in packed for p in b["phrases"]]
    assert leftover == []
    assert got == ["один", "два", "три", "четыре", "пять"]


def test_pack_reports_leftover_loudly_instead_of_silent_truncation():
    src = _sets([4090, 4090, 4090, 4090])   # 16 360 симв. > 3×4096
    packed, leftover = csm._pack_minus_sets(src, csm._MINUS_SHARED_SET_CHAR_BUDGET, 3)
    assert len(packed) == 3
    assert leftover, "не влезшие фразы обязаны вернуться списком, а не исчезнуть"
    assert csm._set_chars([p for b in packed for p in b["phrases"]]) <= 3 * 4096


# ── стенд DI для ensure_named_minus_sets ────────────────────────────────────────────────────

def _mk_deps(pack_root, *, existing=None, add_log=None, get_log=None):
    existing = existing if existing is not None else []
    next_id = {"v": 900001}

    def _v5_get(svc, token, login, fieldnames, criteria=None, extra=None):
        if get_log is not None:
            get_log.append((svc, list(fieldnames)))
        return {"result": {"NegativeKeywordSharedSets": list(existing)}}

    def _v5_call(svc, method, token, login, params):
        if add_log is not None:
            add_log.append((svc, method, params))
        out = []
        for s in params["NegativeKeywordSharedSets"]:
            new_id = next_id["v"]
            next_id["v"] += 1
            existing.append({"Id": new_id, "Name": s["Name"],
                             "NegativeKeywords": list(s["NegativeKeywords"])})
            out.append({"Id": new_id})
        return {"result": {"AddResults": out}}

    return {
        "_SLEPOK_KEY": {},
        "_enabled_minus_words": lambda: [],
        "_v5_call": _v5_call,
        "_v5_err": lambda j: str((j or {}).get("error")),
        "_v5_get": _v5_get,
        "kp": types.SimpleNamespace(PACK_ROOT=str(pack_root)),
    }


def _write_sets(pack_root, site_type, slug, sets):
    d = pack_root / site_type / "_minus_sets"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.json").write_text(
        json.dumps({"slug": slug, "site_type": site_type, "sets": sets}, ensure_ascii=False),
        encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_cache():
    csm._NAMED_SETS_CACHE.clear()
    yield
    csm._NAMED_SETS_CACHE.clear()


def test_four_structure_sets_create_three_library_sets(tmp_path):
    add_log = []
    csm.configure(_mk_deps(tmp_path, add_log=add_log))
    _write_sets(tmp_path, "С пробегом", "kuderko", _sets([4020, 1586, 2415, 4045]))

    res = csm.ensure_named_minus_sets("tok", "porg-x", "kuderko", "С пробегом")

    assert res["sets_in_structure"] == 4
    assert res["packed_from"] == 4 and res["packed_into"] == 3
    assert len(add_log) == 3 and len(res["ids"]) == 3
    assert "not_packed" not in res
    sent = [p for (_s, _m, p) in add_log]
    total_sent = sum(len(p["NegativeKeywordSharedSets"][0]["NegativeKeywords"]) for p in sent)
    expected = sum(len(s["phrases"]) for s in _sets([4020, 1586, 2415, 4045]))
    assert total_sent == expected, "все фразы структуры должны доехать в кабинет"


def test_second_run_after_packing_reuses_the_same_names(tmp_path):
    add_log, existing = [], []
    csm.configure(_mk_deps(tmp_path, existing=existing, add_log=add_log))
    _write_sets(tmp_path, "С пробегом", "kuderko", _sets([4020, 1586, 2415, 4045]))

    first = csm.ensure_named_minus_sets("tok", "porg-x", "kuderko", "С пробегом")
    second = csm.ensure_named_minus_sets("tok", "porg-x", "kuderko", "С пробегом")

    assert len(add_log) == 3, "повторный прогон не должен создавать дубли"
    assert second["created"] == [] and sorted(second["reused"]) == sorted(first["created"])
    assert second["ids"] == first["ids"]
    assert not second.get("mismatch"), "состав совпал — расхождения быть не должно"


def test_three_or_fewer_sets_keep_original_names(tmp_path):
    add_log = []
    csm.configure(_mk_deps(tmp_path, add_log=add_log))
    _write_sets(tmp_path, "С пробегом", "kuderko",
                [{"name": "банки", "phrases": ["тинькофф"]},
                 {"name": "Конкуренты", "phrases": ["рога"]}])

    res = csm.ensure_named_minus_sets("tok", "porg-x", "kuderko", "С пробегом")

    assert res["created"] == ["банки", "Конкуренты"]
    assert "packed_from" not in res, "склейка не должна включаться при ≤3 наборах"


def test_leftover_phrases_produce_visible_error(tmp_path):
    csm.configure(_mk_deps(tmp_path))
    _write_sets(tmp_path, "С пробегом", "kuderko", _sets([4090, 4090, 4090, 4090]))

    res = csm.ensure_named_minus_sets("tok", "porg-x", "kuderko", "С пробегом")

    assert res["ok"] is False
    assert res.get("not_packed")
    assert any("НЕ ВЛЕЗЛО" in e for e in res["errors"]), res["errors"]


# ── Imp6: реюз по имени сверяет состав ──────────────────────────────────────────────────────

def test_reuse_reports_content_mismatch_and_does_not_rewrite(tmp_path):
    """Правку в редакторе слепков нельзя терять молча: набор кабинета отличается → ошибка."""
    add_log = []
    existing = [{"Id": 555, "Name": "банки",
                 "NegativeKeywords": {"Items": ["тинькофф"]}}]
    csm.configure(_mk_deps(tmp_path, existing=existing, add_log=add_log))
    _write_sets(tmp_path, "С пробегом", "kuderko",
                [{"name": "банки", "phrases": ["тинькофф", "сбербанк", "втб"]}])

    res = csm.ensure_named_minus_sets("tok", "porg-x", "kuderko", "С пробегом")

    assert res["ids"] == [555] and res["reused"] == ["банки"]
    assert add_log == [], "содержимое чужого набора аккаунта не переписываем"
    assert res.get("mismatch") == ["банки"]
    msg = " ".join(res["errors"])
    assert "ОТЛИЧАЕТСЯ" in msg and "сбербанк" in msg


def test_reuse_is_silent_when_content_matches(tmp_path):
    existing = [{"Id": 555, "Name": "банки", "NegativeKeywords": ["тинькофф", "сбербанк"]}]
    csm.configure(_mk_deps(tmp_path, existing=existing))
    _write_sets(tmp_path, "С пробегом", "kuderko",
                [{"name": "банки", "phrases": ["тинькофф", "сбербанк"]}])

    res = csm.ensure_named_minus_sets("tok", "porg-x", "kuderko", "С пробегом")

    assert res["reused"] == ["банки"] and not res.get("mismatch")
    assert res["errors"] == []


def test_get_reads_negative_keywords_field(tmp_path):
    get_log = []
    csm.configure(_mk_deps(tmp_path, get_log=get_log))
    _write_sets(tmp_path, "С пробегом", "kuderko", [{"name": "банки", "phrases": ["тинькофф"]}])

    csm.ensure_named_minus_sets("tok", "porg-x", "kuderko", "С пробегом")

    assert get_log and "NegativeKeywords" in get_log[0][1], \
        "без состава набора сверка содержимого невозможна"


# ── Minor 7: region доезжает до гео-гарда ───────────────────────────────────────────────────

def test_region_stems_handle_multiple_oblasts():
    assert csm._region_geo_stems("Волгоградская область") == ["волгоградск"]
    assert csm._region_geo_stems("Волгоградская область, Ростовская область") == [
        "волгоградск", "ростовск"]


def test_own_region_phrases_are_stripped(tmp_path):
    add_log = []
    csm.configure(_mk_deps(tmp_path, add_log=add_log))
    _write_sets(tmp_path, "С пробегом", "kuderko",
                [{"name": "Гео", "phrases": ["авто волгоградская", "авто московская"]}])

    csm.ensure_named_minus_sets("tok", "porg-x", "kuderko", "С пробегом",
                                city="", region="Волгоградская область")

    sent = add_log[0][2]["NegativeKeywordSharedSets"][0]["NegativeKeywords"]
    assert sent == ["авто московская"], sent


def test_orchestrator_passes_region_to_named_sets():
    from direct import automation_runtime as ar

    src = pathlib.Path(ar.__file__).with_name("create_set_orchestrator.py").read_text(encoding="utf-8")
    block = src.split("_ms = _ensure_named_minus_sets(")[1][:600]
    assert "region=" in block, "гео-гард по области без region не работает"
    assert "oblasts" in block


# ── Minor 8: одна карта режимов минусов ─────────────────────────────────────────────────────

def test_minus_mode_map_has_single_source():
    from direct import automation_runtime as ar

    assert ar._SLEPOK_MINUS_MODE is csm._SLEPOK_MINUS_MODE
    # именно из-за расхождения kuderko был только в одной копии
    assert ar._create_set_orchestrator_deps()["_SLEPOK_MINUS_MODE"].get("kuderko") == "group"


# ── Imp4/Imp5: диагноз привязки ─────────────────────────────────────────────────────────────

class _FakeGrid:
    """Grid, который валит UpdateCampaigns заданной ошибкой."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def _read_unified_campaign_update_payloads(self, ids):
        return {cid: {"id": str(cid), "libraryMinusKeywordsIds": []} for cid in ids}

    def _narrow_bases(self, payloads, ids, why):
        return dict(payloads), {}

    def _post(self, name, q, variables):
        self.calls += 1
        payload = self.payload

        class _R:
            @staticmethod
            def json():
                return payload
        return _R()


def _run_attach(monkeypatch, payload, ids=(701, 702)):
    from direct import create_set_apply_batches as ab

    grid = _FakeGrid(payload)
    monkeypatch.setattr(ab, "_build_grid", lambda login, agency="": (None, grid))
    deps = type("D", (), {"account_ctx": staticmethod(lambda login: {"agency": "ag"})})()
    return ab.apply_minus_sets_batch("porg-x", list(ids), deps, minus_set_ids=[1, 2, 3, 4])


def test_minus_set_limit_is_reported_as_validation(monkeypatch):
    res = _run_attach(monkeypatch, {"data": {"updateCampaigns": {
        "updatedCampaigns": [],
        "validationResult": {"errors": [
            {"code": "LIMIT_EXCEEDED", "path": ["libraryMinusKeywordsIds"], "params": []}]}}}})
    assert res["ok"] is False
    assert res["error_kind"] == "validation"
    assert res["minus_set_validation"] is True


def test_transient_grid_failure_is_not_a_minus_set_verdict(monkeypatch):
    """Транзиент (кука/сеть) НЕ должен выглядеть как «лимит Директа по наборам»."""
    res = _run_attach(monkeypatch, {"errors": [{"message": "152 auth required"}]})
    assert res["ok"] is False
    assert res["error_kind"] == "transport"
    assert res["minus_set_validation"] is False


def test_partial_failure_keeps_ok_but_lists_failed_campaigns(monkeypatch):
    from direct import create_set_apply_batches as ab

    class _HalfGrid(_FakeGrid):
        def _post(self, name, q, variables):
            items = variables["input"]["campaignUpdateItems"]
            if len(items) > 1:
                raise RuntimeError("batch failed")      # уводим в per-item fallback
            cid = items[0]["unifiedCampaign"]["id"]
            if cid == "702":
                raise RuntimeError("item 702 failed")

            class _R:
                @staticmethod
                def json():
                    return {"data": {"updateCampaigns": {
                        "updatedCampaigns": [{"id": cid}], "validationResult": {}}}}
            return _R()

    grid = _HalfGrid({})
    monkeypatch.setattr(ab, "_build_grid", lambda login, agency="": (None, grid))
    deps = type("D", (), {"account_ctx": staticmethod(lambda login: {"agency": "ag"})})()

    res = ab.apply_minus_sets_batch("porg-x", [701, 702], deps, minus_set_ids=[555])

    assert res["ok"] is True and res["applied"] == 1
    assert [f["campaign_id"] for f in res["failed_campaigns"]] == [702]


def test_orchestrator_truncates_only_on_minus_set_validation():
    from direct import automation_runtime as ar

    src = pathlib.Path(ar.__file__).with_name("create_set_orchestrator.py").read_text(encoding="utf-8")
    block = src.split("if _ms_ids:")[1].split("if _job is not None:")[0]
    assert 'if _ms_att.get("minus_set_validation") and len(_ms_ids) > _MS_CAP:' in block, \
        "усечение до 3 наборов не должно срабатывать на любой неуспех"
    assert '_ms_report["retry"] = "full_list"' in block, "транзиент → повтор ПОЛНЫМ списком"
    assert '_ms_failed = _ms_att.get("failed_campaigns") or []' in block, \
        "частичный провал привязки обязан попадать в ошибки джобы"


# ── Minor 10/11: видимость потери аудиторий ─────────────────────────────────────────────────

def test_struct_audience_group_count_sums_tps(monkeypatch):
    calls = []

    def _fake(slepok, site_type, tp_code):
        calls.append(tp_code)
        return {"gk1": [("1", "a")]} if tp_code in ("tp1", "tp4") else {}

    monkeypatch.setattr(csa, "struct_audiences_by_gk", _fake)
    assert csa.struct_audience_group_count("kuderko", "С пробегом") == 2
    assert calls == ["tp1", "tp2", "tp4", "tp5"]


def test_empty_ret_map_is_raised_into_job_errors():
    from direct import automation_runtime as ar

    src = pathlib.Path(ar.__file__).with_name("create_set_orchestrator.py").read_text(encoding="utf-8")
    assert "if not ret_map:" in src
    assert "_cs_aud.struct_audience_group_count(agent, eff_site)" in src
    assert "аудитории структуры НЕ отправлены" in src


def test_structure_read_failure_is_logged(monkeypatch, capsys):
    """Раньше голый `out = {}` прятал сбой чтения структуры = тихую потерю всех аудиторий."""
    import direct.create_set_structure as css

    def _boom():
        raise RuntimeError("структура недоступна")

    monkeypatch.setattr(css, "_load_struct", _boom)
    out = csa.struct_audiences_by_gk("kuderko", "С пробегом", "tp1")
    assert out == {}
    printed = capsys.readouterr().out
    assert "ОШИБКА чтения структуры" in printed and "структура недоступна" in printed

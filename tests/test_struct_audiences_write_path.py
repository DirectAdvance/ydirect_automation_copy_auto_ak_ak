"""Аудитории структуры (tp1/tp2/tp4/tp5) доезжают до payload группы Grid.

Живой дефект TP1_TP5_STRUCT_AUDIENCES_NEVER_APPLIED: `item["audiences"]` не читался нигде,
`build_adgroup` жёстко слал `retargetings: []`. Формат поля — билдер интерфейса Директа
(HAR 73har): `{retCondId: <id условия>, id: <id связки|null>}`; поиск (tp2/tp4) —
`searchRetargetings`, сеть (tp1/tp5) — `retargetings`.
"""

import pytest

from direct import create_set_audiences as csa
from direct import grid_create_payloads as gcp


@pytest.fixture(autouse=True)
def _clean_account_cache():
    csa.forget_account_conditions()
    yield
    csa.forget_account_conditions()


# ── payload build_adgroup ───────────────────────────────────────────────────────────────────

def test_no_audiences_keeps_both_fields_empty():
    """Группа без аудиторий в структуре обязана получить ПУСТОЙ retargetings, как раньше."""
    item = gcp.build_adgroup(campaign_id=1, name="g", region_ids=[225], keywords=[])
    assert item["retargetings"] == []
    assert item["searchRetargetings"] == []
    assert item["audienceTargeting"] == "ALL_AUDIENCE"
    assert item["retargetingCondition"] is None


def test_network_audiences_go_to_retargetings():
    item = gcp.build_adgroup(campaign_id=1, name="g", region_ids=[225], keywords=[],
                             retargeting_ids=["40803144", "40845073"])
    assert item["retargetings"] == [{"retCondId": "40803144", "id": None},
                                    {"retCondId": "40845073", "id": None}]
    assert item["searchRetargetings"] == []
    # enum и inline-условие не трогаем (NEW_AUDIENCE требует currentAudience)
    assert item["audienceTargeting"] == "ALL_AUDIENCE"
    assert item["retargetingCondition"] is None
    assert item["caRetargetingCondition"] is None


def test_search_audiences_go_to_search_retargetings():
    item = gcp.build_adgroup(campaign_id=1, name="g", region_ids=[225], keywords=[],
                             retargeting_ids=["40803144"], retargeting_on_search=True)
    assert item["searchRetargetings"] == [{"retCondId": "40803144", "id": None}]
    assert item["retargetings"] == []


def test_payload_dedups_and_drops_blanks():
    assert csa.retargetings_payload(["1", 1, "", None, "2"]) == [
        {"retCondId": "1", "id": None}, {"retCondId": "2", "id": None}]


# ── чтение структуры ────────────────────────────────────────────────────────────────────────

def test_struct_pairs_take_condition_ids_with_names():
    it = {"audiences": ["AUDIENCE:40929593", "RETARGETING:40845073", "35488094"],
          "rl_audiences": [{"rl_id": 40929593, "name": "Интересы и привычки", "type": "AUDIENCE"},
                           {"rl_id": 40845073, "name": "krd_ml_all_10p_lal", "type": "RETARGETING"}]}
    assert csa.struct_audience_pairs(it) == [
        ("40929593", "Интересы и привычки"),
        ("40845073", "krd_ml_all_10p_lal"),
        ("35488094", ""),
    ]


def test_struct_pairs_skip_goal_entities():
    """INTERESTS/HOST/APPLICATION — goal id (путь tp6/tp7 UAC), не условия ретаргетинга."""
    it = {"audiences": ["INTERESTS:2499680510", "HOST:19000141089", "APPLICATION:1"]}
    assert csa.struct_audience_pairs(it) == []


def test_struct_by_gk_reads_items(monkeypatch):
    struct = {"directologists": [{"key": "kuderko", "site_types": [{"name": "С пробегом", "tp": [
        {"code": "tp1", "groups": [{"name": "g", "items": [
            {"gc": "ct0000_aon_n000_r0000_ct001_ag008_g00", "gk": "агрегаторы_d",
             "audiences": ["AUDIENCE:36694733"],
             "rl_audiences": [{"rl_id": 36694733, "name": "Все формы", "type": "AUDIENCE"}]},
            {"gc": "ct0011_aon_n000_r0000_ct001_ag008_g00", "gk": "lada"},
        ]}]},
    ]}]}]}
    monkeypatch.setattr("direct.create_set_structure._load_struct", lambda: struct)
    out = csa.struct_audiences_by_gk("kuderko", "С пробегом", "tp1")
    assert out == {"агрегаторы_d": [("36694733", "Все формы")]}


# ── резолв под целевой кабинет ──────────────────────────────────────────────────────────────

def test_resolve_prefers_exact_donor_id():
    csa.remember_account_conditions("porg-x", {"Все формы": 36694733, "Прочее": 40000001})
    ids, notes = csa.resolve_for_account("porg-x", [("36694733", "Все формы")])
    assert ids == ["36694733"]
    assert notes == []


def test_resolve_falls_back_to_name_in_target_account():
    csa.remember_account_conditions("porg-x", {"Интересы и привычки": 41645619})
    ids, notes = csa.resolve_for_account("porg-x", [("36694733", "Интересы и привычки")])
    assert ids == ["41645619"]           # NBSP-имя донора матчится с обычным пробелом
    assert notes == []


def test_resolve_reports_unknown_condition_and_does_not_send_it():
    csa.remember_account_conditions("porg-x", {"Все формы": 36694733})
    ids, notes = csa.resolve_for_account("porg-x", [("40845073", "krd_ml_all_10p_lal")])
    assert ids == []
    assert len(notes) == 1
    assert "не найдена" in notes[0] and "40845073" in notes[0]


def test_resolve_reports_missing_account_map():
    ids, notes = csa.resolve_for_account("porg-unknown", [("40845073", "x")])
    assert ids == []
    assert notes and "карта условий" in notes[0]


def test_empty_ret_map_is_not_cached_as_known_account():
    csa.remember_account_conditions("porg-x", {})
    assert csa.account_conditions("porg-x") is None


def test_attach_to_group_is_noop_without_structure_audiences():
    g = {"name": "g"}
    csa.attach_to_group(g, "porg-x", None)
    assert "audiences" not in g and "audiences_notes" not in g


def test_attach_to_group_sets_resolved_ids_and_notes():
    csa.remember_account_conditions("porg-x", {"Все формы": 36694733})
    g = {"name": "g"}
    csa.attach_to_group(g, "porg-x", [("36694733", "Все формы"), ("40845073", "чужое")])
    assert g["audiences"] == ["36694733"]
    assert len(g["audiences_notes"]) == 1
    assert csa.group_notes([g, {"name": "g2"}]) == g["audiences_notes"]


# ── контракт вызовов: аудитории реально долетают до payload на всех трёх путях ──────────────

def test_all_adgroup_call_sites_pass_audiences():
    """build_adgroup зовут 3 боевых пути tp1/tp2/tp4/tp5 — каждый обязан прокинуть аудитории."""
    import inspect

    from direct import create_set_text_builders, create_set_tp1_builders, grid_create

    src_tp1 = inspect.getsource(create_set_tp1_builders._build_tp1_adgroups)
    assert 'retargeting_ids=g.get("audiences")' in src_tp1
    assert 'retargeting_on_search=(tp_code in ("tp2", "tp4"))' in src_tp1

    src_tp2 = inspect.getsource(create_set_text_builders._build_tp2_adgroups)
    assert 'retargeting_ids=g.get("audiences")' in src_tp2
    assert "retargeting_on_search=True" in src_tp2       # поиск tp2/tp4

    src_cookie = inspect.getsource(grid_create.create_full)
    assert 'retargeting_ids=g.get("audiences")' in src_cookie
    assert "retargeting_on_search=search_only" in src_cookie


def test_group_sources_attach_audiences():
    """Обе фабрики групп (куки и токен) обязаны проставлять g["audiences"] по gk."""
    import inspect

    from direct import create_set_tp1_builders as b

    for fn in (b._tp1_pack_groups, b._build_tp1_from_pack):
        src = inspect.getsource(fn)
        assert "struct_audiences_by_gk(slepok, site_type, tp_code)" in src
        assert "_cs_aud.attach_to_group(" in src


def test_update_item_defaults_to_empty_lists():
    """RMW-апдейт по умолчанию НЕ ставит аудитории (иначе затрёт), но умеет принять явные."""
    import inspect

    from direct import grid_finalize

    sig = inspect.signature(grid_finalize.GridClient.build_update_item)
    assert sig.parameters["retargeting_ids"].default is None
    assert sig.parameters["retargeting_on_search"].default is False

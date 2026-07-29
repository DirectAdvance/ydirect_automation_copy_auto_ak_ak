"""План Посевов (tp8/tp9/tp10) обязан слушать выбор пользователя в дереве набора.

Баг 2026-07-28: `create_set_plan` строил посевы безусловным циклом по трём tp с захардкоженным
списком брендов → план ВСЕГДА 12 кампаний, `selected_pos` не читался ни разу.
"""

import json
from collections import Counter
from pathlib import Path

import pytest
from flask import Flask

from direct.core import automation_runtime
from direct.slepki_code import slepki_store
from direct.create import create_set_plan

DIRECT_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture()
def plan_env(monkeypatch):
    """Тот же стенд, что в test_slepki_source_manifest: БД/токены/регион замоканы."""
    automation_runtime._create_set_plan_module()   # боевой configure(): DI-зависимости плана

    class Cursor:
        def execute(self, *_args, **_kwargs):
            pass

        def fetchone(self):
            return None

    class Connection:
        def cursor(self, **_kwargs):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(create_set_plan, "_victory_conn", lambda: Connection(), raising=False)
    monkeypatch.setattr(create_set_plan, "_resolve_region", lambda _city: ("r0000", "Россия"),
                        raising=False)
    monkeypatch.setattr(create_set_plan, "_rule_sets", lambda *_args: {
        "cpa": 2000, "budget": 5000, "cpc_cpa": 2000, "cpc_budget": 5000,
    }, raising=False)
    monkeypatch.setattr(create_set_plan, "_token_for_login", lambda *_args: (None, None),
                        raising=False)
    monkeypatch.setattr(create_set_plan, "_direct_tokens", lambda: [], raising=False)
    monkeypatch.setattr(create_set_plan, "_SLEPOK_KEY", {}, raising=False)

    def _json_stub(name):
        if name == "slepki_structure.json":
            return slepki_store.assemble(mutable=True)
        return json.loads((DIRECT_DIR / name).read_text(encoding="utf-8"))

    monkeypatch.setattr(create_set_plan, "_json", _json_stub, raising=False)
    return _json_stub


def _plan(selected_pos):
    body = {
        "login": "posevy-test",
        "agent": "posevy",
        "agent_group": "posevy",
        "site_type": "Посевы",
        "variants": [],
    }
    if selected_pos is not None:
        body["selected_pos"] = selected_pos
    app = Flask(__name__)
    with app.test_request_context("/", method="POST", json=body):
        return create_set_plan._set_plan_response().get_json()


def _labels(tp_code):
    return [p["label"] for p in create_set_plan._posevy_positions("Посевы", tp_code)]


def test_posevy_positions_mirror_structure_groups(plan_env):
    """Позиции = группы структуры `posevy`: метка — имя группы, ct — из кодера, бренд — из имени."""
    assert _labels("tp10") == ["Telegram + Max", "Telegram + Max — Tenet",
                               "Telegram + Max — Lada", "Telegram + Max — Haval"]
    assert [(p["ct"], p["brand"]) for p in create_set_plan._posevy_positions("Посевы", "tp10")] == [
        ("ct0000", "Посевы"), ("ct0300", "Tenet"), ("ct0181", "Lada"), ("ct0111", "Haval"),
    ]


def test_only_tp10_selected_builds_four_campaigns(plan_env):
    data = _plan({"10": {"labels": _labels("tp10"), "groups": _labels("tp10")}})
    assert data["count"] == 4
    assert Counter(p["tp"] for p in data["plan"]) == {"tp10": 4}


@pytest.mark.parametrize("tp_num,tp_code", [(8, "tp8"), (9, "tp9")])
def test_single_tp_selected_builds_four_campaigns(plan_env, tp_num, tp_code):
    data = _plan({str(tp_num): {"labels": _labels(tp_code), "groups": _labels(tp_code)}})
    assert data["count"] == 4
    assert Counter(p["tp"] for p in data["plan"]) == {tp_code: 4}


def test_two_tp_selected_build_eight_campaigns(plan_env):
    data = _plan({
        "8":  {"labels": _labels("tp8"), "groups": _labels("tp8")},
        "10": {"labels": _labels("tp10"), "groups": _labels("tp10")},
    })
    assert data["count"] == 8
    assert Counter(p["tp"] for p in data["plan"]) == {"tp8": 4, "tp10": 4}


def test_single_row_inside_tp10_builds_one_campaign(plan_env):
    label = _labels("tp10")[1]                     # «Telegram + Max — Tenet»
    data = _plan({"10": {"labels": [label], "groups": [label]}})
    assert data["count"] == 1
    assert data["plan"][0]["tp"] == "tp10"
    assert data["plan"][0]["ct"] == "ct0300"
    assert data["plan"][0]["brand_label"] == "Tenet"


def test_all_three_tp_selected_build_twelve_campaigns(plan_env):
    data = _plan({str(n): {"labels": _labels(f"tp{n}"), "groups": _labels(f"tp{n}")}
                  for n in (8, 9, 10)})
    assert data["count"] == 12
    assert Counter(p["tp"] for p in data["plan"]) == {"tp8": 4, "tp9": 4, "tp10": 4}


@pytest.mark.parametrize("selected_pos", [None, {}])
def test_no_selection_keeps_previous_behaviour(plan_env, selected_pos):
    """Ключей 8/9/10 нет вовсе (API/retry/старый клиент) → прежние 12 кампаний."""
    data = _plan(selected_pos)
    assert data["count"] == 12
    assert Counter(p["tp"] for p in data["plan"]) == {"tp8": 4, "tp9": 4, "tp10": 4}


def test_campaign_names_match_structure_coder(plan_env):
    """Имена кампаний не поехали: кодер + бренд из структуры (регрессия по именам)."""
    data = _plan({"8": {"labels": _labels("tp8"), "groups": _labels("tp8")}})
    assert [p["name"] for p in data["plan"]] == [
        "tp8_cpc_site_ct0000_aon_n000_r0000_ct018_ag001_g00 — Посевы Telegram - Россия",
        "tp8_cpc_site_ct0300_aon_n000_r0000_ct018_ag001_g00 — Tenet Telegram - Россия",
        "tp8_cpc_site_ct0181_aon_n000_r0000_ct018_ag001_g00 — Lada Telegram - Россия",
        "tp8_cpc_site_ct0111_aon_n000_r0000_ct018_ag001_g00 — Haval Telegram - Россия",
    ]

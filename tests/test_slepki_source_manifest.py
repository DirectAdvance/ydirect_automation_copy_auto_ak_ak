import json
from pathlib import Path

from flask import Flask

from direct import blueprint, create_set_plan
from direct.scripts.slepki_preflight import _source_manifest_errors


DIRECT_DIR = Path(__file__).resolve().parents[1]


def test_gen_ses_manifest_is_complete_and_matches_archive_shape():
    structure = json.loads((DIRECT_DIR / "slepki_structure.json").read_text(encoding="utf-8"))
    manifest = json.loads((DIRECT_DIR / "gen_ses_source_manifest.json").read_text(encoding="utf-8"))

    assert _source_manifest_errors(structure, DIRECT_DIR) == []
    assert [campaign["source_campaign_id"] for campaign in manifest["campaigns"]] == [
        710461331, 710035126, 710447368, 710035444, 710446676, 710034291,
    ]
    assert [campaign["source_totals"]["groups"] for campaign in manifest["campaigns"]] == [
        55, 55, 55, 55, 4, 55,
    ]
    assert [campaign["slug"] for campaign in manifest["campaigns"] if not campaign["source_ready"]] == [
        "rsya_interests", "rsya_retargeting",
    ]


def test_gen_ses_structure_references_manifest_instead_of_copying_source_campaigns():
    structure = json.loads((DIRECT_DIR / "slepki_structure.json").read_text(encoding="utf-8"))
    gen_ses = next(item for item in structure["directologists"] if item["key"] == "gen_ses")

    assert gen_ses["source_manifest"] == "gen_ses_source_manifest.json"
    assert "source_campaigns" not in gen_ses


def test_ui_structure_expands_gen_ses_manifest_to_six_campaigns():
    ui = blueprint._slepki_structure_for_ui()
    gen_ses = next(item for item in ui["directologists"] if item["key"] == "gen_ses")
    used = next(site for site in gen_ses["site_types"] if site["name"] == "С пробегом")

    assert len(used["source_campaigns"]) == 6
    assert used["source_campaigns"][0]["source_name"] == "Интересы"
    assert used["source_campaigns"][-1]["source_name"] == "Товарная"


def test_gen_ses_plan_stops_before_drafts_and_keeps_six_source_campaigns(monkeypatch):
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
    monkeypatch.setattr(create_set_plan, "_resolve_region", lambda _city: ("r0000", "Россия"), raising=False)
    monkeypatch.setattr(create_set_plan, "_rule_sets", lambda *_args: {
        "cpa": 2000, "budget": 5000, "cpc_cpa": 2000, "cpc_budget": 5000,
    }, raising=False)
    monkeypatch.setattr(create_set_plan, "_token_for_login", lambda *_args: (None, None), raising=False)
    monkeypatch.setattr(create_set_plan, "_direct_tokens", lambda: [], raising=False)
    monkeypatch.setattr(create_set_plan, "_SLEPOK_KEY", {}, raising=False)
    monkeypatch.setattr(
        create_set_plan,
        "_json",
        lambda name: json.loads((DIRECT_DIR / name).read_text(encoding="utf-8")),
        raising=False,
    )

    app = Flask(__name__)
    with app.test_request_context("/", method="POST", json={
        "login": "gen-ses-test",
        "agent": "gen_ses",
        "site_type": "С пробегом",
        "variants": ["tp1_rsy", "search_test", "rsya_gallery"],
    }):
        response = create_set_plan._set_plan_response()
        data = response.get_json()

    assert data["pre_draft_only"] is True
    assert data["count"] == 6
    assert [item["source_campaign_slug"] for item in data["plan"]] == [
        "rsya_interests", "rsya_retargeting", "rsya_autotarget",
        "search_autotarget", "search_keywords", "product_gallery",
    ]
    assert all(item["pre_draft_only"] for item in data["plan"])
    assert any("interest_ids_by_group" in item for item in data["pre_draft_blockers"])
    assert any("retargeting_condition_ids_by_group" in item for item in data["pre_draft_blockers"])

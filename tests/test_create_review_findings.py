from direct import grid_finalize
from direct.create_job_status import terminal_status_for_job, terminal_status_for_parent_failed
from direct.create_set_deferred_status import parent_deferred_status_after_resume
from direct.create_set_feed_result import (
    FEED_CAMPAIGN_NOT_CREATED_ERROR,
    SHOPPING_AD_REQUIRED_ERROR,
    ensure_shopping_cookie_error,
    shopping_cookie_success,
)
from direct.create_set_tp1_builders import _synthesize_tp1_build_error


def test_cookie_feed_success_requires_shopping_ad():
    rep = {"campaign_id": 123, "groups": 1, "ads": 0, "errors": []}

    assert shopping_cookie_success(rep) is False
    assert SHOPPING_AD_REQUIRED_ERROR in ensure_shopping_cookie_error(rep)
    assert SHOPPING_AD_REQUIRED_ERROR in rep["errors"]


def test_cookie_feed_success_accepts_campaign_group_and_shopping_ad():
    rep = {"campaign_id": 123, "groups": 1, "ads": 1, "shopping_ad_ids": [456], "errors": []}

    assert shopping_cookie_success(rep) is True


def test_cookie_feed_error_preserves_primary_campaign_error():
    rep = {"campaign_id": None, "groups": 0, "ads": 0, "errors": ["кампания(куки): validation"]}

    assert ensure_shopping_cookie_error(rep) == "кампания(куки): validation"
    assert SHOPPING_AD_REQUIRED_ERROR not in rep["errors"]
    assert ensure_shopping_cookie_error({"errors": []}) == FEED_CAMPAIGN_NOT_CREATED_ERROR


def test_create_set_failed_count_is_error_terminal():
    status, error = terminal_status_for_job("set", {"created": 4, "failed": 1})

    assert status == "error"
    assert error == "создание завершилось с ошибками: не создано 1"


def test_copy_failed_count_keeps_existing_done_semantics():
    status, error = terminal_status_for_job("copy_campaigns", {"created": 4, "failed": 1})

    assert status == "done"
    assert error is None


def test_parent_child_failed_count_is_error_terminal():
    status, error = terminal_status_for_parent_failed(2)

    assert status == "error"
    assert error == "докрутка завершилась с ошибками: не создано 2"


def test_deferred_resume_failed_count_marks_row_error():
    status, note = parent_deferred_status_after_resume(created=3, failed=1)

    assert status == "error"
    assert "создано 3" in note
    assert "не создано 1" in note


def test_tp5_narrow_placement_fix_writes_null(monkeypatch):
    captured = {}
    client = grid_finalize.GridClient.__new__(grid_finalize.GridClient)
    client.login = "porg-test"

    monkeypatch.setattr(
        client,
        "_read_unified_campaign_update_payloads",
        lambda ids: {123: {"id": "123", "placementTypes": ["SEARCH_PAGE"]}},
    )
    monkeypatch.setattr(client, "_narrow_bases", lambda payloads, ids, op: (payloads, {}))

    def fake_post_json_retry(op, query, variables):
        captured["variables"] = variables
        return {
            "data": {
                "updateCampaigns": {
                    "updatedCampaigns": [{"id": "123"}],
                    "validationResult": {},
                }
            }
        }

    monkeypatch.setattr(client, "_post_json_retry", fake_post_json_retry)

    assert client.set_campaign_placement_types([123], None) == [{"id": "123"}]
    item = captured["variables"]["input"]["campaignUpdateItems"][0]["unifiedCampaign"]
    assert item["placementTypes"] is None


# ── _synthesize_tp1_build_error: errors(plural) → error(singular) для tp5 ────

class TestSynthesizeTp1BuildError:
    """Verify _synthesize_tp1_build_error correctly escalates tp5 keyword failures
    to a singular rep["error"] without touching informational-only errors.

    Motivation: twelve writes to rep["errors"] were never read by the verdict gate
    (which only checks rep["error"] singular). tp5 search campaigns with zero keywords
    but existing adgroups have no search traffic → structural failure, must be fatal.
    """

    def test_tp5_no_keywords_with_errors_sets_singular_error(self):
        """tp5 + adgroups>0 + keywords=0 + errors → fatal singular error synthesised."""
        rep = {
            "adgroups": 3, "keywords": 0, "ads": 0,
            "errors": ["keywords(Grid AddKeywords tp5): some API error"],
        }
        _synthesize_tp1_build_error(rep, "tp5")
        assert rep.get("error"), "singular error must be set for tp5 keyword failure"
        assert "tp5 ключи" in rep["error"]

    def test_tp5_with_keywords_does_not_set_singular_error(self):
        """tp5 + adgroups>0 + keywords>0 + errors → errors stay informational."""
        rep = {
            "adgroups": 3, "keywords": 5, "ads": 3,
            "errors": ["keywords(Grid AddKeywords tp5): partial failure"],
        }
        _synthesize_tp1_build_error(rep, "tp5")
        assert not rep.get("error"), "partial keywords: must remain informational"

    def test_tp1_no_keywords_does_not_set_singular_error(self):
        """tp1 RSY + keywords=0 + errors → RSY uses contextual, no fatal synthesis."""
        rep = {
            "adgroups": 3, "keywords": 0, "ads": 3,
            "errors": ["keywords.add some error"],
        }
        _synthesize_tp1_build_error(rep, "tp1")
        assert not rep.get("error"), "tp1 RSY keyword failure must be informational"

    def test_tp5_no_adgroups_does_not_synthesize(self):
        """tp5 + adgroups=0 + keywords=0 → already caught by adgroups gate, no synthesis."""
        rep = {
            "adgroups": 0, "keywords": 0, "ads": 0,
            "errors": ["adgroups(Grid tp5): creation failed"],
        }
        _synthesize_tp1_build_error(rep, "tp5")
        assert not rep.get("error"), "adgroups=0 is caught by existing gate, no synthesis"

    def test_tp5_shopping_error_does_not_set_singular_error(self):
        """tp5 shopping errors with working adgroups/keywords: informational."""
        rep = {
            "adgroups": 3, "keywords": 10, "ads": 3,
            "errors": ["shopping(Grid addShoppingAds): NOT_FOUND"],
        }
        _synthesize_tp1_build_error(rep, "tp5")
        assert not rep.get("error"), "shopping failure with working adgroups/keywords must be informational"

    def test_phase15_error_not_overwritten(self):
        """If rep['error'] already set by Phase 1.5 gate, synthesis must not overwrite."""
        existing_msg = "relevanceMatch(1.5): Grid error"
        rep = {
            "adgroups": 3, "keywords": 0, "ads": 0,
            "errors": ["relevanceMatch(1.5): Grid error", "keywords(Grid AddKeywords tp5): failed"],
            "error": existing_msg,
        }
        _synthesize_tp1_build_error(rep, "tp5")
        assert rep["error"] == existing_msg, "existing singular error must not be overwritten"

    def test_tp5_empty_errors_list_does_not_set_singular_error(self):
        """tp5 + keywords=0 + empty errors → nothing to synthesize."""
        rep = {"adgroups": 3, "keywords": 0, "ads": 0, "errors": []}
        _synthesize_tp1_build_error(rep, "tp5")
        assert not rep.get("error"), "empty errors must not trigger synthesis"

    def test_positional_shift_stays_informational(self):
        """tp5 + keywords present + positional shift warning: no fatal escalation."""
        rep = {
            "adgroups": 3, "keywords": 5, "ads": 3,
            "errors": ["tp5 Grid: позиционный сдвиг групп — ключи могут быть смещены"],
        }
        _synthesize_tp1_build_error(rep, "tp5")
        assert not rep.get("error"), "positional shift is informational"

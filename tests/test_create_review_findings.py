from direct import grid_finalize
from direct.create_job_status import terminal_status_for_job, terminal_status_for_parent_failed
from direct.create_set_deferred_status import parent_deferred_status_after_resume
from direct.create_set_feed_result import (
    FEED_CAMPAIGN_NOT_CREATED_ERROR,
    SHOPPING_AD_REQUIRED_ERROR,
    ensure_shopping_cookie_error,
    shopping_cookie_success,
)


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

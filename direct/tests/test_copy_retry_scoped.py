import json

from direct.core import queue_server
from direct.copy_service import copy_cleanup


def test_copy_retry_body_keeps_only_failed_campaigns_and_cleanup_targets(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "id_maps.json").write_text(
        json.dumps({"campaigns": {"11": 101, "22": 202, "33": 303}}),
        encoding="utf-8",
    )
    row = {
        "job_id": "failed1",
        "agent_board_task_id": 77,
        "body": {
            "campaign_ids": [11, 22, 33],
            "target_login": "target",
            "created_by": "scherbakova",
            "target_cleanup": "none",
        },
        "error": "",
        "result": {
            "workdir": str(workdir),
            "cookie_postprocess": {
                "errors": [
                    "verification gate: keyword_count=mismatch campaign:22→202",
                    "кампания 303: Grid restore-strategy failed",
                ]
            },
        },
    }

    body = queue_server._copy_retry_body_from_failed(row)

    assert body["campaign_ids"] == [22, 33]
    assert body["_copy_retry_all_campaign_ids"] == [11, 22, 33]
    assert body["_copy_retry_scope"] == "failed_campaigns"
    assert body["_copy_retry_cleanup_target_ids"] == [202, 303]
    assert body["target_cleanup"] == "delete_drafts"


def test_copy_retry_body_falls_back_to_all_when_errors_are_not_campaign_specific():
    row = {
        "job_id": "failed1",
        "body": {"campaign_ids": [11, 22], "target_login": "target"},
        "error": "feed-filters listing: transient top-level error",
        "result": {"error": "feed-filters listing: transient top-level error"},
    }

    body = queue_server._copy_retry_body_from_failed(row)

    assert body["campaign_ids"] == [11, 22]
    assert body["_copy_retry_scope"] == "all"
    assert "_copy_retry_cleanup_target_ids" not in body
    assert body["target_cleanup"] == "delete_drafts"


def test_copy_worker_result_preserves_rich_error_payload():
    data = queue_server._copy_worker_result_from_copy_job({
        "status": "error",
        "error": "verification gate: 7 blockers",
        "result": {
            "workdir": "/tmp/direct-copy-job",
            "id_maps": {"campaigns": {"11": 101}},
            "cookie_postprocess": {"errors": ["verification gate: 7 blockers"]},
        },
    })

    assert data["error"] == "verification gate: 7 blockers"
    assert data["workdir"] == "/tmp/direct-copy-job"
    assert data["id_maps"]["campaigns"] == {"11": 101}
    assert data["cookie_postprocess"]["errors"]


def test_copy_cleanup_delete_drafts_respects_scoped_target_ids(monkeypatch):
    calls = []
    campaigns = [
        {"Id": 101, "Status": "DRAFT", "State": "OFF"},
        {"Id": 202, "Status": "DRAFT", "State": "OFF"},
        {"Id": 303, "Status": "ACCEPTED", "State": "OFF"},
    ]

    monkeypatch.setattr(copy_cleanup, "_resolve_agency_hint", lambda _login, _default: "agency")
    monkeypatch.setattr(copy_cleanup, "_token_for_login", lambda _login, _agency, _tokens: ("token", "agency"))
    monkeypatch.setattr(copy_cleanup, "_direct_tokens", lambda: {})
    monkeypatch.setattr(copy_cleanup, "_copy_cleanup_uac_drafts", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(copy_cleanup, "_copy_job_log", lambda *_args, **_kwargs: None)

    def fake_v5_call(_service, method, _token, _login, payload):
        calls.append((method, payload))
        if method == "get":
            return {"result": {"Campaigns": campaigns}}
        if method == "delete":
            return {"result": {"DeleteResults": [{"Id": x} for x in payload["SelectionCriteria"]["Ids"]]}}
        raise AssertionError(method)

    monkeypatch.setattr(copy_cleanup, "_v5_call", fake_v5_call)
    monkeypatch.setattr(copy_cleanup, "_v5_err", lambda value: str(value))

    result = copy_cleanup._copy_target_cleanup(
        "job1", "target", "agency", "delete_drafts", campaign_ids=[202]
    )

    delete_calls = [payload for method, payload in calls if method == "delete"]
    assert delete_calls == [{"SelectionCriteria": {"Ids": [202]}}]
    assert result["deleted"] == 1

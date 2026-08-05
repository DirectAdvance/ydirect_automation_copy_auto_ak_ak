import json

from direct.core import queue_server
from direct.copy_service import copy_cleanup
from direct.copy_service import copy_jobs


def test_copy_scope_claims_only_copy_campaigns_web_jobs(monkeypatch):
    captured = {}

    class FakeCursor:
        def execute(self, sql, params=()):
            captured["sql"] = sql
            captured["params"] = params

        def fetchall(self):
            return [{"job_id": "copy1", "login": "target", "total": 1, "body": {}}]

    class FakeConn:
        def cursor(self, **_kwargs):
            return FakeCursor()

        def commit(self):
            captured["committed"] = True

        def close(self):
            captured["closed"] = True

    monkeypatch.setenv("DIRECT_ROLE", "all")
    monkeypatch.setenv("DIRECT_WORKER_SCOPE", "copy")
    monkeypatch.setattr(queue_server, "_victory_conn_rw", lambda: FakeConn())

    rows = queue_server._worker_claim_web_jobs()

    assert rows[0]["job_id"] == "copy1"
    assert captured["params"] == ()
    assert "coalesce(kind,'') = 'copy_campaigns'" in captured["sql"]
    assert "coalesce(kind,'') <> 'copy_campaigns'" not in captured["sql"]
    assert queue_server._worker_can_poll_db_queue()


def test_copy_jobs_recover_preserves_web_posted_queued_jobs(monkeypatch):
    calls = []

    class FakeCursor:
        def execute(self, sql, params=()):
            calls.append((sql, params))

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            calls.append(("COMMIT", ()))

        def close(self):
            calls.append(("CLOSE", ()))

    monkeypatch.setattr(copy_jobs, "_COPY_JOBS", {})
    monkeypatch.setattr(copy_jobs, "_victory_conn_rw", lambda: FakeConn())

    copy_jobs._copy_jobs_recover()

    statements = "\n".join(sql for sql, _params in calls)
    assert "status='running' OR (status='queued' AND coalesce(body->>'_web_posted','') <> 'true')" in statements
    assert "status='claimed'" in statements
    assert "coalesce(body->>'_web_posted','')='true'" in statements


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

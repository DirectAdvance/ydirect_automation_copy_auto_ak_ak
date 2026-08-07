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
    # Раньше здесь ожидался delete_drafts без списка id — то есть снос ВСЕХ черновиков
    # кабинета, включая удачные копии и чужие кампании. Своих target-id у этой джобы нет
    # (результат пустой), поэтому чистить нечего.
    assert body["_copy_retry_cleanup_target_ids"] == []
    assert body["target_cleanup"] == "none"


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


def test_retry_on_generic_error_cleans_only_own_copies():
    """Общая ошибка без id кампаний не должна сносить чужие и удачные черновики кабинета."""
    row = {
        "job_id": "generic1",
        "login": "porg-target",
        "body": {
            "source_login": "porg-source",
            "target_login": "porg-target",
            "campaign_ids": [101, 102, 103],
            "created_by": "Ilyin",
            "target_cleanup": "none",
        },
        "result": {
            "results": [
                {"id": 900101, "campaign_id": 900101, "ok": True},
                {"id": 900102, "campaign_id": 900102, "ok": True},
                {"id": 900103, "campaign_id": 900103, "ok": True},
            ],
        },
        "error": "grid update adaptive: обновлено 278/285 объявлений",
    }

    body = queue_server._copy_retry_body_from_failed(row)

    assert body["_copy_retry_scope"] == "all"
    # Чистятся ровно свои копии, а не все DRAFT кабинета.
    assert body["_copy_retry_cleanup_target_ids"] == [900101, 900102, 900103]
    assert body["target_cleanup"] == "delete_drafts"


def test_retry_without_known_targets_does_not_clean_anything():
    row = {
        "job_id": "generic2",
        "login": "porg-target",
        "body": {"source_login": "porg-source", "target_login": "porg-target",
                 "campaign_ids": [101], "created_by": "Ilyin"},
        "result": {},
        "error": "grid reauth не получил csrf",
    }

    body = queue_server._copy_retry_body_from_failed(row)

    assert body["_copy_retry_cleanup_target_ids"] == []
    # Ничего своего не известно → ничего не удаляем, иначе снесём чужое.
    assert body["target_cleanup"] == "none"


def test_scoped_retry_never_deletes_more_than_it_recreates():
    """Чистка обязана быть подмножеством пересоздаваемого — иначе копии теряются навсегда."""
    row = {
        "job_id": "scoped1",
        "login": "porg-target",
        "body": {"source_login": "porg-source", "target_login": "porg-target",
                 "campaign_ids": [11, 22, 33], "created_by": "Ilyin"},
        "result": {
            "id_maps": {"campaigns": {"11": 201, "22": 202, "33": 203}},
            "results": [
                {"source_id": 22, "campaign_id": 202, "ok": False, "error": "не создалась"},
                {"source_id": 11, "campaign_id": 201, "ok": True},
                {"source_id": 33, "campaign_id": 203, "ok": True},
            ],
        },
        # Текстовый разбор подхватывает и посторонние target-id из логов.
        "error": "не создалась кампания target 202; ранее в логе встречались 777 и 888",
    }

    scope = queue_server._copy_retry_failed_scope(row)

    assert scope["mode"] == "failed_campaigns"
    assert scope["campaign_ids"] == [22]
    # 777/888 не имеют пары source в этом повторе — удалять их нельзя.
    assert scope["cleanup_target_ids"] == [202]

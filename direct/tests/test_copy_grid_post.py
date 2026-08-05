from direct.copy_service import copy_engine
from direct.copy_service.copy_grid_post import _campaign_payload_from_source


def test_copy_run_job_routes_all_post_selection_to_grid_cookie(monkeypatch):
    logs = []
    upserts = []

    monkeypatch.setattr(
        copy_engine,
        "_copy_selected_grid_campaigns",
        lambda _login, _ids: [{
            "id": 713257258,
            "name": "tp8_cpc_site_ct0000_aon_n000_r0002_ct018_ag001_g00",
            "typename": "GdPostCampaign",
            "status": "DRAFT",
        }],
    )
    monkeypatch.setattr(copy_engine, "_copy_is_uac_grid_row", lambda _row: False)
    monkeypatch.setattr(copy_engine, "_token_for_login", lambda *_args, **_kwargs: ("token", "agency"))
    monkeypatch.setattr(copy_engine, "_resolve_agency_hint", lambda *_args, **_kwargs: "agency")
    monkeypatch.setattr(copy_engine, "_direct_tokens", lambda: {})
    monkeypatch.setattr(
        copy_engine,
        "_v5_call",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("post copy must not call v5")),
    )
    monkeypatch.setattr(
        copy_engine,
        "_direct_copy_module",
        lambda: (_ for _ in ()).throw(AssertionError("post copy must not load v5 pull module")),
    )
    monkeypatch.setattr(copy_engine, "_copy_job_log", lambda _job_id, msg: logs.append(msg))
    monkeypatch.setattr(copy_engine, "_copy_job_upsert", lambda job_id, **kw: upserts.append((job_id, kw)))
    monkeypatch.setattr(
        copy_engine,
        "_copy_grid_post_campaigns",
        lambda job_id, body, rows, workdir: {
            "ok": True,
            "copy_depth": "grid_cookie_post",
            "results": [{"ok": True, "source_id": rows[0]["id"], "campaign_id": 999}],
            "errors": [],
            "workdir": str(workdir),
        },
    )

    copy_engine._copy_run_job("job-post", {
        "source_login": "porg-source",
        "target_login": "porg-target",
        "campaign_ids": [713257258],
        "counter_id": 1,
        "goal_id": 2,
        "target_domain": "example.ru",
        "target_city": "Москва",
        "target_region": "Москва и область",
    })

    terminal = [kw for _job_id, kw in upserts if kw.get("status") == "done"][-1]
    assert terminal["result"]["copy_depth"] == "grid_cookie_post"
    assert any("Post campaigns без Direct API/v5 snapshot" in msg for msg in logs)


def test_post_campaign_payload_strips_grid_typenames():
    payload = _campaign_payload_from_source(
        {
            "name": "tp8_source",
            "strategy": {
                "bid": 180,
                "budget": {"sum": 11200, "__typename": "GdCampaignBudget"},
                "platforms": {"telegram": True, "__typename": "GdCampaignPlatforms"},
            },
            "notification": {
                "smsSettings": {
                    "smsTime": {
                        "__typename": "GdTimeInterval",
                        "startTime": {"hour": 9, "minute": 0, "__typename": "GdTime"},
                        "endTime": {"hour": 21, "minute": 0, "__typename": "GdTime"},
                    }
                }
            },
            "timeTarget": {"__typename": "GdTimeTarget", "timeBoard": [[100] * 24 for _ in range(7)]},
        },
        "tp8_copy",
        110881570,
        586853078,
        "test@example.ru",
    )

    assert "__typename" not in str(payload)
    assert payload["timeTarget"]["timeBoard"][0][0] == 100

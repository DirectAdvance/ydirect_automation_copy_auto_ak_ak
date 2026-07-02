from direct.main import app
from direct import campaign
from direct import routes_content_editor as content_editor


def _route(rule: str, method: str = "GET"):
    return next(
        r for r in app.url_map.iter_rules()
        if r.rule == rule and method in r.methods
    )


def test_create_set_route_points_to_api_create_set():
    rule = _route("/direct/api/create_set", "POST")

    assert rule.endpoint == "direct.api_create_set"
    assert app.view_functions[rule.endpoint].__name__ == "api_create_set"


def test_create_set_authenticated_smoke_reaches_api_create_set():
    app.testing = True
    client = app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True
        session["is_admin"] = True
        session["username"] = "route-smoke"

    response = client.post("/direct/api/create_set", json={"login": "x", "items": []})

    assert response.status_code == 400
    assert "login" in response.get_json()["error"]


def test_direct_route_map_smoke_for_extraction_groups():
    expected = {
        ("/direct/", "GET"): "index",
        ("/direct/automation", "GET"): "automation",
        ("/direct/minusphrase", "GET"): "minusphrase",
        ("/direct/api/overview", "GET"): "api_overview",
        ("/direct/api/units", "GET"): "api_units",
        ("/direct/api/deferred", "GET"): "api_deferred",
        ("/direct/api/deferred_cancel", "POST"): "api_deferred_cancel",
        ("/direct/api/deferred_resume_now", "POST"): "api_deferred_resume_now",
        ("/direct/api/slepok_callouts", "GET"): "api_slepok_callouts",
        ("/direct/api/m3_content_status", "GET"): "api_m3_content_status",
        ("/direct/api/m3-status", "GET"): "api_m3_status",
        ("/direct/api/pack_preview", "GET"): "api_pack_preview",
        ("/direct/api/accounts", "GET"): "api_accounts",
        ("/direct/api/accounts_otkrut", "GET"): "api_accounts_otkrut",
        ("/direct/api/account_info", "GET"): "api_account_info",
        ("/direct/api/account_stats", "GET"): "api_account_stats",
        ("/direct/api/account_prefill", "GET"): "api_account_prefill",
        ("/direct/api/goal_for_counter", "GET"): "api_goal_for_counter",
        ("/direct/api/balance", "POST"): "api_balance",
        ("/direct/api/account_assets", "GET"): "api_account_assets",
        ("/direct/api/account_audiences", "GET"): "api_account_audiences",
        ("/direct/api/statuses", "GET"): "api_statuses",
        ("/direct/api/feeds", "GET"): "api_feeds",
        ("/direct/api/feed-rules", "GET"): "api_feed_rules_get",
        ("/direct/api/feed-rules", "POST"): "api_feed_rules_post",
        ("/direct/api/rules", "GET"): "api_rules_get",
        ("/direct/api/rules", "POST"): "api_rules_post",
        ("/direct/api/corrections", "GET"): "api_corrections_get",
        ("/direct/api/corrections", "POST"): "api_corrections_post",
        ("/direct/api/minus-places", "GET"): "api_minus_places_get",
        ("/direct/api/minus-places", "POST"): "api_minus_places_post",
        ("/direct/api/content-tree", "GET"): "api_content_tree",
        ("/direct/api/content-assets", "GET"): "api_content_assets",
        ("/direct/api/content-preview/<token>", "GET"): "api_content_preview",
        ("/direct/api/content-thumb/<token>", "GET"): "api_content_thumb",
        ("/direct/api/content-rules", "POST"): "api_content_rules_post",
        ("/direct/api/copy_campaigns", "GET"): "api_copy_campaigns",
        ("/direct/api/copy_target_prefill", "GET"): "api_copy_target_prefill",
        ("/direct/api/copy_start", "POST"): "api_copy_start",
        ("/direct/api/copy_status/<job_id>", "GET"): "api_copy_status",
        ("/direct/api/campaigns", "GET"): "api_campaigns",
        ("/direct/api/campaigns/stop_all", "POST"): "api_stop_all",
        ("/direct/api/campaigns/delete_drafts", "POST"): "api_delete_drafts",
        ("/direct/api/campaigns/delete_drafts_async", "POST"): "api_delete_drafts_async",
        ("/direct/api/check_blocks", "POST"): "api_check_blocks",
        ("/direct/api/set_plan", "POST"): "api_set_plan",
        ("/direct/api/create", "POST"): "api_create",
        ("/direct/api/create_set_async", "POST"): "api_create_set_async",
        ("/direct/api/create_set_status", "GET"): "api_create_set_status",
        ("/direct/api/create_set_verification", "GET"): "api_create_set_verification",
        ("/direct/api/create_set_repair", "POST"): "api_create_set_repair",
        ("/direct/api/create_jobs", "GET"): "api_create_jobs",
        ("/direct/api/create_set_cancel", "POST"): "api_create_set_cancel",
        ("/direct/api/jobs/<job_id>/resume", "POST"): "api_job_resume",
        ("/direct/api/jobs/<job_id>/delete_created", "POST"): "api_job_delete_created",
        ("/direct/api/ai/status", "GET"): "api_ai_status",
        ("/direct/api/ai/chat", "POST"): "api_ai_chat",
        ("/direct/api/ai/agents", "GET"): "api_ai_agents",
        ("/direct/api/ai/promo/generate", "POST"): "api_ai_promo_generate",
        ("/direct/api/ai/campaign/generate", "POST"): "api_ai_campaign_generate",
        ("/direct/api/ai/slepok_content/seed", "POST"): "api_ai_slepok_content_seed",
        ("/direct/api/ai/slepok_content", "GET"): "api_ai_slepok_content",
        ("/direct/api/ai/promo/publish", "POST"): "api_ai_promo_publish",
    }

    for (path, method), view_name in expected.items():
        rule = _route(path, method)
        assert rule.endpoint == f"direct.{view_name}"
        assert app.view_functions[rule.endpoint].__name__ == view_name


def test_content_editor_campaigns_get_uses_only_valid_top_level_fields():
    calls = []

    def fake_v5_call(svc, method, token, login, params):
        calls.append((svc, method, params))
        if svc == "campaigns":
            assert "TextCampaignFieldNames" not in params
            return {"result": {"Campaigns": [{"Id": 1, "Name": "test", "Type": "TEXT_CAMPAIGN"}]}}
        if svc == "adgroups":
            assert params["SelectionCriteria"] == {"CampaignIds": [1]}
            return {"result": {"AdGroups": []}}
        if svc == "ads":
            assert params["SelectionCriteria"] == {"CampaignIds": [1]}
            assert params["ResponsiveAdFieldNames"] == ["Titles", "Texts", "SitelinkSetId"]
            return {"result": {"Ads": [{
                "Id": 10,
                "CampaignId": 1,
                "AdGroupId": 2,
                "Type": "TEXT_AD",
                "ResponsiveAd": {
                    "Titles": [{"Text": "Первый"}, {"Text": "Второй"}],
                    "Texts": [{"Text": "Описание"}],
                    "SitelinkSetId": 3,
                },
            }]}}
        if svc == "sitelinks":
            assert params["SelectionCriteria"] == {"Ids": [3]}
            return {"result": {"SitelinkSets": []}}
        if svc == "adextensions":
            return {"result": {"AdExtensions": []}}
        raise AssertionError(svc)

    out = content_editor._load_account("token", "login", fake_v5_call)

    assert "error" not in out
    assert calls[0][0] == "campaigns"
    assert out["ads"][0]["title"] == "Первый"
    assert out["ads"][0]["title2"] == "Второй"
    assert out["ads"][0]["text"] == "Описание"


def test_content_editor_campaign_callout_ids_does_not_call_invalid_v5_enum():
    def fail_v5_call(*args, **kwargs):
        raise AssertionError("campaigns.get must not be called for CalloutIds")

    assert content_editor._campaign_callout_ids(fail_v5_call, "token", "login", 123) is None


def test_content_editor_replace_never_writes_via_oauth_api():
    def fail_api(*args, **kwargs):
        raise AssertionError("content editor writes must use cookies/Grid, not OAuth API")

    content = {
        "ads": [{"ad_id": 10, "title": "Старый", "title2": "", "text": "Текст", "usages": []}],
        "sitelinks": [{"set_id": 3, "items": [{"title": "Ссылка", "href": "https://example.com"}], "usages": []}],
        "callouts": [],
        "_ads_by_set": {"3": [{"ad_id": 10}]},
    }

    ad_out = content_editor._do_replace(
        "token", "login", "ad_title", "Старый", "Новый", content, fail_api, fail_api
    )
    sitelink_out = content_editor._do_replace(
        "token", "login", "sitelink_title", "Ссылка", "Новая", content, fail_api, fail_api
    )

    assert ad_out["replaced"] == 0
    assert sitelink_out["replaced"] == 0
    assert "cookies/Grid" in ad_out["errors"][0]
    assert "cookies/Grid" in sitelink_out["errors"][0]


def test_load_cookie_local_accepts_yandex_direct_nested_cookie_file(tmp_path, monkeypatch):
    secret_dir = tmp_path / ".secret"
    nested = secret_dir / "yandex_direct"
    nested.mkdir(parents=True)
    (nested / "cookies.json").write_text('{"agency": "cookie=value"}', encoding="utf-8")
    monkeypatch.setattr(campaign, "_find_secret_dir", lambda start=None: secret_dir)

    assert campaign.load_cookie_local("agency") == "cookie=value"

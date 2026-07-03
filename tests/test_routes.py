from direct.main import app
from direct.content_main import app as content_app
from direct import campaign
from direct import create_set_input
from direct import grid_finalize
from direct import routes_content_editor as content_editor


def _route(rule: str, method: str = "GET"):
    return next(
        r for r in app.url_map.iter_rules()
        if r.rule == rule and method in r.methods
    )


def _route_in(flask_app, rule: str, method: str = "GET"):
    return next(
        r for r in flask_app.url_map.iter_rules()
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


def test_single_feed_prefers_yandex_xml_rows_and_items():
    rows = [
        {"Id": 1, "Name": "yandex-catalog-new.xml"},
        {"Id": 2, "Name": "/yandex.xml"},
        {"Id": 3, "Name": "credit-page-01-a.xml"},
    ]

    assert create_set_input.prefer_single_feed_rows(rows) == [rows[1]]

    items = [
        {"name": "no-feed"},
        {"name": "catalog", "feed_id": 1, "feed_name": "yandex-catalog-new.xml"},
        {"name": "target", "feed_id": 2, "feed_name": "/yandex.xml"},
    ]

    filtered = create_set_input.first_feed_items(items, parse_number=lambda value, default=0: int(value or default))

    assert [item["name"] for item in filtered] == ["no-feed", "target"]


def test_single_feed_falls_back_to_first_feed_when_yandex_xml_missing():
    rows = [
        {"Id": 1, "Name": "yandex-catalog-new.xml"},
        {"Id": 3, "Name": "credit-page-01-a.xml"},
    ]

    assert create_set_input.prefer_single_feed_rows(rows) == [rows[0]]


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


def test_content_editor_standalone_app_owns_only_content_routes():
    page = _route_in(content_app, "/direct/automation/content", "GET")
    load = _route_in(content_app, "/direct/api/content-editor/load", "POST")
    replace_async = _route_in(content_app, "/direct/api/content-editor/replace_async", "POST")
    jobs = _route_in(content_app, "/direct/api/content-editor/jobs", "GET")
    status = _route_in(content_app, "/direct/api/content-editor/status", "GET")

    assert page.endpoint == "direct.content_editor_page"
    assert load.endpoint == "direct.ce_load"
    assert replace_async.endpoint == "direct.ce_replace_async"
    assert jobs.endpoint == "direct.ce_jobs"
    assert status.endpoint == "direct.ce_job_status"
    assert not any(r.rule == "/direct/automation" for r in content_app.url_map.iter_rules())


def test_content_editor_campaigns_get_uses_only_valid_top_level_fields():
    calls = []

    class NoGrid:
        def __init__(self, login):
            self.login = login

        def _bootstrap_csrf(self):
            raise RuntimeError("grid disabled in unit test")

    class NoUac:
        def __init__(self, login):
            self.client = self

        def list_campaigns(self):
            return []

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
                    "Titles": [{"Title": "Первый"}, {"Title": "Второй"}],
                    "Texts": [{"Text": "Описание"}],
                    "SitelinkSetId": 3,
                },
            }]}}
        if svc == "sitelinks":
            assert params["SelectionCriteria"] == {"Ids": [3]}
            return {"result": {"SitelinksSets": [{
                "Id": 3,
                "Sitelinks": [{"Title": "Быстрая", "Href": "https://example.com", "Description": "Описание"}],
            }]}}
        if svc == "adextensions":
            return {"result": {"AdExtensions": []}}
        raise AssertionError(svc)

    out = content_editor._load_account(
        "token", "login", fake_v5_call,
        grid_client_factory=NoGrid,
        uac_read_client_factory=NoUac,
    )

    assert "error" not in out
    assert calls[0][0] == "campaigns"
    assert out["ads"][0]["title"] == "Первый"
    assert out["ads"][1]["title"] == "Второй"
    assert out["ads"][2]["text"] == "Описание"
    assert out["sitelinks"][0]["items"][0]["title"] == "Быстрая"


def test_content_editor_batches_campaign_ids_for_adgroups_and_ads():
    calls = []

    class NoGrid:
        def __init__(self, login):
            self.login = login

        def _bootstrap_csrf(self):
            raise RuntimeError("grid disabled in unit test")

    class NoUac:
        def __init__(self, login):
            self.client = self

        def list_campaigns(self):
            return []

    def fake_v5_call(svc, method, token, login, params):
        calls.append((svc, method, params))
        if svc == "campaigns":
            return {
                "result": {
                    "Campaigns": [
                        {"Id": cid, "Name": f"camp {cid}", "Type": "TEXT_CAMPAIGN"}
                        for cid in range(1, 13)
                    ]
                }
            }
        if svc in ("adgroups", "ads"):
            ids = params["SelectionCriteria"]["CampaignIds"]
            assert 1 <= len(ids) <= 10
            collection = "AdGroups" if svc == "adgroups" else "Ads"
            return {"result": {collection: []}}
        if svc == "adextensions":
            return {"result": {"AdExtensions": []}}
        raise AssertionError(svc)

    out = content_editor._load_account(
        "token", "login", fake_v5_call,
        grid_client_factory=NoGrid,
        uac_read_client_factory=NoUac,
    )

    assert "error" not in out
    adgroup_batches = [
        c[2]["SelectionCriteria"]["CampaignIds"]
        for c in calls
        if c[0] == "adgroups"
    ]
    ad_batches = [
        c[2]["SelectionCriteria"]["CampaignIds"]
        for c in calls
        if c[0] == "ads"
    ]
    assert adgroup_batches == [list(range(1, 11)), [11, 12]]
    assert ad_batches == [list(range(1, 11)), [11, 12]]


def test_content_editor_load_includes_cookie_only_uac_campaigns():
    class NoGrid:
        def __init__(self, login):
            self.login = login

        def _bootstrap_csrf(self):
            raise RuntimeError("grid disabled in unit test")

    class FakeUacRead:
        def __init__(self, login):
            self.login = login
            self.client = self

        def list_campaigns(self):
            return [{"id": 712112280, "display_name": "tp7"}]

        def campaign_details(self, campaign_ids):
            assert campaign_ids == [712112280]
            return {
                712112280: {
                    "id": "712112280",
                    "titles": ["Старый tp7 заголовок"],
                    "texts": ["Старый tp7 текст"],
                }
            }

    def fake_v5_call(svc, method, token, login, params):
        if svc == "campaigns":
            return {"result": {"Campaigns": []}}
        if svc == "adextensions":
            return {"result": {"AdExtensions": []}}
        raise AssertionError(f"v5 {svc} must not be called without v5 campaigns")

    out = content_editor._load_account(
        "token",
        "login",
        fake_v5_call,
        grid_client_factory=NoGrid,
        uac_read_client_factory=FakeUacRead,
    )

    assert "error" not in out
    assert out["ads"] == [
        {
            "ad_id": 0,
            "source": "uac",
            "campaign_id": 712112280,
            "title": "Старый tp7 заголовок",
            "title2": "",
            "text": "",
            "usages": [{
                "campaign_id": 712112280,
                "campaign_name": "tp7",
                "adgroup_id": 0,
                "adgroup_name": "",
            }],
        },
        {
            "ad_id": 0,
            "source": "uac",
            "campaign_id": 712112280,
            "title": "",
            "title2": "",
            "text": "Старый tp7 текст",
            "usages": [{
                "campaign_id": 712112280,
                "campaign_name": "tp7",
                "adgroup_id": 0,
                "adgroup_name": "",
            }],
        },
    ]


def test_content_editor_load_falls_back_to_grid_for_tp67_campaign_list():
    class FakeGrid:
        def __init__(self, login):
            self.login = login
            self.offsets = []

        def _bootstrap_csrf(self):
            return None

        def _post(self, op, query, variables):
            self.offsets.append(variables["inp"]["limitOffset"]["offset"])

            class Response:
                status_code = 200

                def json(self):
                    return {
                        "data": {
                            "client": {
                                "campaigns": {
                                    "rowset": [{
                                        "id": "712112280",
                                        "name": "tp7_cpc_site — yandex",
                                        "__typename": "GdUnifiedCampaign",
                                        "status": {"primaryStatus": "ACTIVE", "archived": False},
                                    }]
                                }
                            }
                        }
                    }

            return Response()

    class BrokenUacList:
        def __init__(self, login):
            self.client = self

        def list_campaigns(self):
            raise RuntimeError("HTTP 405")

        def campaign_details(self, campaign_ids):
            assert campaign_ids == [712112280]
            return {712112280: {"titles": ["tp7 title"], "texts": []}}

    def fake_v5_call(svc, method, token, login, params):
        if svc == "campaigns":
            return {"result": {"Campaigns": []}}
        if svc == "adextensions":
            return {"result": {"AdExtensions": []}}
        raise AssertionError(svc)

    out = content_editor._load_account(
        "token",
        "login",
        fake_v5_call,
        grid_client_factory=FakeGrid,
        uac_read_client_factory=BrokenUacList,
    )

    assert out["ads"][0]["source"] == "uac"
    assert out["ads"][0]["campaign_id"] == 712112280
    assert out["ads"][0]["title"] == "tp7 title"


def test_content_editor_campaign_callout_ids_does_not_call_invalid_v5_enum():
    def fail_v5_call(*args, **kwargs):
        raise AssertionError("campaigns.get must not be called for CalloutIds")

    assert content_editor._campaign_callout_ids(fail_v5_call, "token", "login", 123) is None


def test_content_editor_replace_never_writes_via_oauth_api():
    def fail_api(*args, **kwargs):
        raise AssertionError("content editor writes must use cookies/Grid, not OAuth API")

    class FakeGrid:
        def __init__(self, login):
            self.login = login

        def find_and_replace_text(self, ad_ids, *, target_types, search, replace, case_sensitive=True, **kwargs):
            if target_types == ["TITLE"]:
                assert ad_ids == [10]
                assert search == "Старый"
                assert replace == "Новый"
            elif target_types == ["SITELINK_TITLE"]:
                raise AssertionError("sitelinks must not use findAndReplaceText")
            elif target_types == ["SITELINK_DESCRIPTION"]:
                raise AssertionError("sitelinks must not use findAndReplaceText")
            else:
                raise AssertionError(target_types)
            assert case_sensitive is True
            return {"replaced": 1, "total": 1, "rowset": [{"adId": "10"}], "errors": []}

        def add_sitelink_set(self, sitelinks):
            if sitelinks[0]["title"] == "Новая":
                assert sitelinks == [{"title": "Новая", "description": "Описание", "href": "https://example.com"}]
                return 30
            if sitelinks[0]["description"] == "Новое описание":
                assert sitelinks == [{"title": "Ссылка", "description": "Новое описание", "href": "https://example.com"}]
                return 31
            raise AssertionError(sitelinks)

        def set_campaign_sitelink_set(self, campaign_ids, sitelink_set_id):
            assert campaign_ids == [1]
            assert sitelink_set_id in (30, 31)
            return [{"id": "1"}]

    content = {
        "ads": [{
            "ad_id": 10,
            "title": "Старый",
            "title2": "",
            "text": "Текст",
            "usages": [{"campaign_id": 1, "campaign_name": "camp", "adgroup_id": 2}],
        }],
        "sitelinks": [{
            "set_id": 3,
            "items": [{"title": "Ссылка", "description": "Описание", "href": "https://example.com"}],
            "usages": [{"campaign_id": 1, "campaign_name": "camp", "adgroup_id": 2}],
        }],
        "callouts": [],
        "_ads_by_set": {"3": [{"ad_id": 10}]},
    }

    ad_out = content_editor._do_replace(
        "token", "login", "ad_title", "Старый", "Новый", content, fail_api, fail_api,
        grid_client_factory=FakeGrid,
    )
    sitelink_out = content_editor._do_replace(
        "token", "login", "sitelink_title", "Ссылка", "Новая", content, fail_api, fail_api,
        grid_client_factory=FakeGrid,
    )
    sitelink_desc_out = content_editor._do_replace(
        "token", "login", "sitelink_description", "Описание", "Новое описание", content, fail_api, fail_api,
        grid_client_factory=FakeGrid,
    )

    assert ad_out["replaced"] == 1
    assert sitelink_out["replaced"] == 1
    assert sitelink_desc_out["replaced"] == 1


def test_content_editor_replaces_uac_titles_and_texts_with_patch():
    class FakeUacClient:
        def __init__(self, login):
            self.login = login
            self.csrf = "csrf"
            self.details = {
                101: {
                    "id": 101,
                    "titles": [{"text": "Старый заголовок"}, {"text": "Другой"}],
                    "texts": ["Старый текст"],
                }
            }
            self.patches = []

        def link_info(self, url):
            return {}

        def _request(self, method, path, *, params=None, json_body=None, step=""):
            cid = int(path.strip("/").split("/")[-1])
            if method == "GET":
                return {"result": dict(self.details[cid])}
            if method == "PATCH":
                self.patches.append((cid, json_body))
                self.details[cid].update(json_body)
                return {"result": dict(self.details[cid])}
            raise AssertionError(method)

    clients = []

    def factory(login):
        client = FakeUacClient(login)
        clients.append(client)
        return client

    content = {
        "ads": [
            {
                "ad_id": 0,
                "source": "uac",
                "campaign_id": 101,
                "title": "Старый заголовок",
                "title2": "",
                "text": "",
                "usages": [{"campaign_id": 101, "campaign_name": "tp6"}],
            },
            {
                "ad_id": 0,
                "source": "uac",
                "campaign_id": 101,
                "title": "",
                "title2": "",
                "text": "Старый текст",
                "usages": [{"campaign_id": 101, "campaign_name": "tp6"}],
            },
        ],
        "sitelinks": [],
        "callouts": [],
    }

    title_out = content_editor._do_replace(
        "token", "login", "ad_title", "Старый заголовок", "Новый заголовок", content,
        lambda *args: (_ for _ in ()).throw(AssertionError("no oauth")),
        lambda *args: (_ for _ in ()).throw(AssertionError("no v501")),
        uac_client_factory=factory,
    )
    text_out = content_editor._do_replace(
        "token", "login", "ad_text", "Старый текст", "Новый текст", content,
        lambda *args: (_ for _ in ()).throw(AssertionError("no oauth")),
        lambda *args: (_ for _ in ()).throw(AssertionError("no v501")),
        uac_client_factory=factory,
    )

    assert title_out["replaced"] == 1
    assert title_out["uac"]["updated_uac_campaigns"] == [101]
    assert clients[0].patches == [(
        101,
        {"titles": [{"text": "Новый заголовок"}, {"text": "Другой"}]},
    )]
    assert text_out["replaced"] == 1
    assert clients[1].patches == [(101, {"texts": ["Новый текст"]})]


def test_content_editor_uac_full_patch_omits_feed_fields_for_autotargeting():
    class FakeUacClient:
        csrf = "csrf"

        def __init__(self):
            self.patch_bodies = []
            self.detail = {
                "id": 101,
                "adv_type": "text",
                "display_name": "tp7 auto",
                "href": "https://example.com",
                "keywords": None,
                "feed_id": 123,
                "listings_feed_id": 123,
                "feed_filters": None,
                "listings_feed_filters": None,
                "titles": ["Старый"],
                "texts": ["Текст"],
            }

        def link_info(self, url):
            return {}

        def _request(self, method, path, *, params=None, json_body=None, step=""):
            if method == "GET":
                return {"result": dict(self.detail)}
            if method == "PATCH":
                self.patch_bodies.append(json_body)
                if len(self.patch_bodies) == 1:
                    raise RuntimeError("partial rejected")
                assert json_body["keywords"] == []
                assert "feed_id" not in json_body
                assert "listings_feed_id" not in json_body
                assert "feed_filters" not in json_body
                assert "listings_feed_filters" not in json_body
                self.detail.update(json_body)
                return {"result": dict(self.detail)}
            raise AssertionError(method)

    client = FakeUacClient()

    out = content_editor._uac_patch_campaign_texts(client, 101, "titles", ["Новый"])

    assert out["result"]["titles"] == ["Новый"]
    assert client.patch_bodies[0] == {"titles": ["Новый"]}
    assert client.patch_bodies[1]["titles"] == ["Новый"]


def test_content_editor_callout_replace_creates_new_and_swaps_campaign_bindings():
    calls = []

    class FakeGrid:
        def __init__(self, login):
            self.login = login

        def add_callouts(self, texts):
            assert texts == ["Новый"]
            return {"Новый": 30}

        def set_campaign_callouts(self, campaign_ids, callout_ids):
            calls.append((campaign_ids, callout_ids))
            return [{"id": str(campaign_ids[0])}]

    content = {
        "ads": [],
        "sitelinks": [],
        "callouts": [
            {"id": 10, "text": "Старый", "usages": [{"campaign_id": 1}, {"campaign_id": 2}]},
            {"id": 20, "text": "Другой", "usages": [{"campaign_id": 1}]},
        ],
        "_campaign_callout_ids": {
            1: [10, 20],
            2: [10],
            3: [20],
        },
    }

    out = content_editor._do_replace(
        "token", "login", "callout", "Старый", "Новый", content,
        lambda *args: (_ for _ in ()).throw(AssertionError("no oauth")),
        lambda *args: (_ for _ in ()).throw(AssertionError("no oauth")),
        grid_client_factory=FakeGrid,
    )

    assert out["replaced"] == 2
    assert out["new_callout_id"] == 30
    assert calls == [([1], [20, 30]), ([2], [30])]


def test_content_editor_callout_replace_never_falls_back_to_v5_create():
    calls = []

    class FakeGrid:
        def __init__(self, login):
            self.login = login

        def add_callouts(self, texts):
            assert texts == ["Новый"]
            return {}

        def set_campaign_callouts(self, campaign_ids, callout_ids):
            calls.append((campaign_ids, callout_ids))
            return [{"id": str(campaign_ids[0])}]

    content = {
        "ads": [],
        "sitelinks": [],
        "callouts": [{"id": 10, "text": "Старый", "usages": [{"campaign_id": 1}]}],
        "_campaign_callout_ids": {1: [10]},
    }

    out = content_editor._do_replace(
        "token", "login", "callout", "Старый", "Новый", content,
        lambda *args: (_ for _ in ()).throw(AssertionError("no oauth")),
        lambda *args: (_ for _ in ()).throw(AssertionError("no v501")),
        grid_client_factory=FakeGrid,
    )

    assert out["replaced"] == 0
    assert "OAuth API fallback запрещён" in out["errors"][0]
    assert calls == []


def test_content_editor_callout_replace_sanitizes_invalid_symbols_before_create():
    calls = []

    class FakeGrid:
        def __init__(self, login):
            self.login = login

        def add_callouts(self, texts):
            assert texts == ["На семью -20% Звоните"]
            return {"На семью -20% Звоните": 30}

        def set_campaign_callouts(self, campaign_ids, callout_ids):
            calls.append((campaign_ids, callout_ids))
            return [{"id": str(campaign_ids[0])}]

    content = {
        "ads": [],
        "sitelinks": [],
        "callouts": [{"id": 10, "text": "На семью -20%", "usages": [{"campaign_id": 1}]}],
        "_campaign_callout_ids": {1: [10]},
    }

    out = content_editor._do_replace(
        "token", "login", "callout", "На семью -20%", "На семью -20%. Звоните!", content,
        lambda *args: (_ for _ in ()).throw(AssertionError("no oauth")),
        lambda *args: (_ for _ in ()).throw(AssertionError("no v501")),
        grid_client_factory=FakeGrid,
    )

    assert out["replaced"] == 1
    assert out["new_text"] == "На семью -20% Звоните"
    assert calls == [([1], [30])]


def test_grid_set_campaign_callouts_sends_required_attribution_model(monkeypatch):
    client = grid_finalize.GridClient.__new__(grid_finalize.GridClient)
    client.login = "login"
    sent = {}

    monkeypatch.setattr(client, "_bootstrap_csrf", lambda: None)
    monkeypatch.setattr(client, "_read_unified_campaign_update_payloads", lambda ids: {
        123: {
            "id": "123",
            "name": "Campaign",
            "state": "COMPLETE",
            "startDate": "2026-07-03",
            "attributionModel": "AUTOMATIC",
            "inheritableCallouts": {"calloutIds": ["9"]},
        }
    })

    class Response:
        def json(self):
            return {
                "data": {
                    "updateCampaigns": {
                        "updatedCampaigns": [{"id": "123"}],
                        "validationResult": {"errors": []},
                    }
                }
            }

    def fake_post(name, query, variables):
        sent.update(variables)
        return Response()

    monkeypatch.setattr(client, "_post", fake_post)

    out = client.set_campaign_callouts([123], [10, 20])

    item = sent["input"]["campaignUpdateItems"][0]["unifiedCampaign"]
    assert out == [{"id": "123"}]
    assert item["id"] == "123"
    assert item["attributionModel"] == "AUTOMATIC"
    assert item["name"] == "Campaign"
    assert item["state"] == "COMPLETE"
    assert item["inheritableCallouts"] == {"calloutIds": ["10", "20"]}


def test_grid_set_campaign_sitelink_set_sends_required_attribution_model(monkeypatch):
    client = grid_finalize.GridClient.__new__(grid_finalize.GridClient)
    client.login = "login"
    sent = {}

    monkeypatch.setattr(client, "_bootstrap_csrf", lambda: None)
    monkeypatch.setattr(client, "_read_unified_campaign_update_payloads", lambda ids: {
        123: {
            "id": "123",
            "name": "Campaign",
            "state": "COMPLETE",
            "startDate": "2026-07-03",
            "attributionModel": "AUTOMATIC",
            "inheritableSitelinkSet": {"sitelinkSetId": "111"},
        }
    })

    class Response:
        def json(self):
            return {
                "data": {
                    "updateCampaigns": {
                        "updatedCampaigns": [{"id": "123"}],
                        "validationResult": {"errors": []},
                    }
                }
            }

    def fake_post(name, query, variables):
        sent.update(variables)
        return Response()

    monkeypatch.setattr(client, "_post", fake_post)

    out = client.set_campaign_sitelink_set([123], 456)

    item = sent["input"]["campaignUpdateItems"][0]["unifiedCampaign"]
    assert out == [{"id": "123"}]
    assert item["id"] == "123"
    assert item["attributionModel"] == "AUTOMATIC"
    assert item["name"] == "Campaign"
    assert item["state"] == "COMPLETE"
    assert item["inheritableSitelinkSet"] == {"sitelinkSetId": "456"}


def test_load_cookie_local_accepts_yandex_direct_nested_cookie_file(tmp_path, monkeypatch):
    secret_dir = tmp_path / ".secret"
    nested = secret_dir / "yandex_direct"
    nested.mkdir(parents=True)
    (nested / "cookies.json").write_text('{"agency": "cookie=value"}', encoding="utf-8")
    monkeypatch.setattr(campaign, "_find_secret_dir", lambda start=None: secret_dir)

    assert campaign.load_cookie_local("agency") == "cookie=value"

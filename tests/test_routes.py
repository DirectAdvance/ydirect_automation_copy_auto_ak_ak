import json

from direct.main import app
from direct.content_main import app as content_app
from direct.copy_service.copy_main import app as copy_app
from direct.slepki_main import app as slepki_app
from direct import campaign
from direct import create_set_input
from direct import grid_finalize
from direct import content_editor_helpers
from direct import link_check
from direct import routes_content_editor as content_editor
from direct import content_replace_routes


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


def test_create_set_async_rejects_pre_draft_source_plan():
    app.testing = True
    client = app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True
        session["is_admin"] = True
        session["username"] = "route-smoke"

    response = client.post("/direct/api/create_set_async", json={
        "login": "gen-ses-test",
        "items": [{"name": "source campaign", "pre_draft_only": True}],
    })

    assert response.status_code == 409
    assert "до этапа создания черновиков" in response.get_json()["error"]


def test_create_set_async_rejects_stream_single_feed_stale_client():
    app.testing = True
    client = app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True
        session["is_admin"] = True
        session["username"] = "route-smoke"

    response = client.post("/direct/api/create_set_async", json={
        "login": "gen-ses-test",
        "agent": "scherbakova",
        "site_type": "Мультибренд",
        "stream_content": True,
        "single_feed": True,
        "items": [{
            "name": "tp7_cpc_site — ТК - Haval - КС + Автотаргетинг",
            "_plan_agent": "scherbakova",
            "_plan_site_type": "Мультибренд",
        }],
    })

    data = response.get_json()
    assert response.status_code == 409
    assert data["stale_client_single_feed"] is True


def test_create_set_async_rejects_stream_product_only_stale_client():
    app.testing = True
    client = app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True
        session["is_admin"] = True
        session["username"] = "route-smoke"

    response = client.post("/direct/api/create_set_async", json={
        "login": "gen-ses-test",
        "agent": "scherbakova",
        "site_type": "Мультибренд",
        "stream_content": True,
        "single_feed": False,
        "items": [{
            "type": "product",
            "name": "tp7_cpc_site — ТК - Haval - КС + Автотаргетинг",
            "_plan_agent": "scherbakova",
            "_plan_site_type": "Мультибренд",
        }],
    })

    data = response.get_json()
    assert response.status_code == 409
    assert data["stale_client_product_only_stream"] is True


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
        ("/direct/api/ui_structure", "GET"): "api_ui_structure",
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


def test_copy_standalone_app_owns_copy_routes():
    expected = {
        ("/direct/api/copy_campaigns", "GET"): "api_copy_campaigns",
        ("/direct/api/copy_target_prefill", "GET"): "api_copy_target_prefill",
        ("/direct/api/copy_start", "POST"): "api_copy_start",
        ("/direct/api/copy_status/<job_id>", "GET"): "api_copy_status",
    }

    for (path, method), view_name in expected.items():
        rule = _route_in(copy_app, path, method)
        assert rule.endpoint == f"direct.{view_name}"
        assert copy_app.view_functions[rule.endpoint].__name__ == view_name

    assert not any(r.rule == "/direct/api/copy_campaigns" for r in app.url_map.iter_rules())


def test_slepki_standalone_app_owns_slepki_page_and_read_models():
    expected = {
        ("/direct/automation/slepki", "GET"): "slepki_page",
        ("/direct/api/slepki/ui_structure", "GET"): "slepki_api_ui_structure",
        ("/direct/api/slepki/minus-places", "GET"): "slepki_api_minus_places_get",
        ("/direct/api/slepki/minus-places", "POST"): "slepki_api_minus_places_post",
    }

    for (path, method), view_name in expected.items():
        rule = _route_in(slepki_app, path, method)
        assert rule.endpoint == f"direct.{view_name}"
        assert slepki_app.view_functions[rule.endpoint].__name__ == view_name

    assert not any(r.rule == "/direct/api/ui_structure" for r in slepki_app.url_map.iter_rules())


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


def test_content_editor_links_check_route_returns_url_statuses(monkeypatch):
    route = _route_in(content_app, "/direct/api/content-editor/links/check", "POST")
    assert route.endpoint == "direct.ce_links_check"

    calls = []

    def fake_status_batch(urls, *, timeout, max_workers):
        calls.append((list(urls), timeout, max_workers))
        return {
            "https://example.com/ok": {
                "url": "https://example.com/ok",
                "status": 200,
                "ok": True,
            },
            "https://example.com/missing": {
                "url": "https://example.com/missing",
                "status": 404,
                "ok": False,
            },
        }

    monkeypatch.setattr(content_replace_routes, "url_status_batch", fake_status_batch)

    content_app.testing = True
    client = content_app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True
        session["is_admin"] = True
        session["username"] = "route-smoke"

    response = client.post("/direct/api/content-editor/links/check", json={
        "urls": ["https://example.com/ok", "https://example.com/missing"],
    })

    assert response.status_code == 200
    assert calls == [(["https://example.com/ok", "https://example.com/missing"], 2.0, 8)]
    data = response.get_json()
    assert data["items"] == [
        {"url": "https://example.com/ok", "status": 200, "ok": True},
        {"url": "https://example.com/missing", "status": 404, "ok": False},
    ]


def test_link_preview_status_blocks_private_hosts(monkeypatch):
    calls = []
    monkeypatch.setattr(link_check, "_http_status", lambda *a, **k: calls.append(a) or 200)

    out = link_check.url_status("http://127.0.0.1:5020/direct")

    assert out == {
        "url": "http://127.0.0.1:5020/direct",
        "status": None,
        "ok": False,
        "error": "private_host",
    }
    assert calls == []


def test_link_preview_status_uses_checked_ip_and_does_not_follow_redirect(monkeypatch):
    connect_calls = []

    class FakeSock:
        def __init__(self):
            self.data = b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:5020/\r\n\r\n"

        def settimeout(self, _timeout):
            pass

        def sendall(self, _payload):
            pass

        def recv(self, _n):
            if not self.data:
                return b""
            out, self.data = self.data[:1], self.data[1:]
            return out

        def close(self):
            pass

    monkeypatch.setattr(link_check.socket, "getaddrinfo", lambda *a, **k: [
        (link_check.socket.AF_INET, link_check.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
    ])
    monkeypatch.setattr(link_check.socket, "create_connection",
                        lambda addr, timeout: connect_calls.append((addr, timeout)) or FakeSock())

    out = link_check.url_status("http://public.example/redirect", timeout=1.0)

    assert out == {"url": "http://public.example/redirect", "status": 302, "ok": True}
    assert connect_calls == [(("93.184.216.34", 80), 1.0)]


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
            # Href — валидное поле: нужно вкладке «Смена ссылки» (links_out).
            assert params["ResponsiveAdFieldNames"] == ["Titles", "Texts", "SitelinkSetId", "Href"]
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


def test_content_editor_retries_premature_adgroups_response(monkeypatch):
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

    monkeypatch.setattr(content_editor_helpers.time, "sleep", lambda _seconds: None)

    def fake_v5_call(svc, method, token, login, params):
        calls.append((svc, method, params))
        if svc == "campaigns":
            return {"result": {"Campaigns": [{"Id": 1, "Name": "camp 1", "Type": "TEXT_CAMPAIGN"}]}}
        if svc == "adgroups":
            adgroup_calls = [c for c in calls if c[0] == "adgroups"]
            if len(adgroup_calls) == 1:
                return {"error": {"error_string": "Response ended prematurely"}}
            return {"result": {"AdGroups": []}}
        if svc == "ads":
            return {"result": {"Ads": []}}
        if svc == "adextensions":
            return {"result": {"AdExtensions": []}}
        raise AssertionError(svc)

    out = content_editor._load_account(
        "token", "login", fake_v5_call,
        grid_client_factory=NoGrid,
        uac_read_client_factory=NoUac,
    )

    assert "error" not in out
    assert len([c for c in calls if c[0] == "adgroups"]) == 2


def test_content_editor_ad_href_executor_skips_adgroups_snapshot(monkeypatch):
    calls = []
    conn = _FakeScopeConn({"directologist": "Иванов"})

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
            return {"result": {"Campaigns": [{"Id": 1, "Name": "camp 1", "Type": "TEXT_CAMPAIGN"}]}}
        if svc == "adgroups":
            raise AssertionError("ad_href snapshot must not call adgroups.get")
        if svc == "ads":
            return {"result": {"Ads": []}}
        if svc == "adextensions":
            return {"result": {"AdExtensions": []}}
        raise AssertionError(svc)

    monkeypatch.setattr(content_editor, "_grid_client", NoGrid)
    monkeypatch.setattr(content_editor_helpers, "_uac_read_client", lambda login, factory=None: NoUac(login))
    monkeypatch.setattr(content_editor, "_do_replace", lambda *a, **k: {"replaced": 0, "errors": []})

    execute = content_editor.make_job_executor(
        victory_conn=lambda: conn,
        token_for_login=lambda *a, **k: ("tok", "agency"),
        direct_tokens=lambda: {"agency": "tok"},
        v5_call=fake_v5_call,
        v501_svc=lambda *a, **k: None,
    )

    out = execute({
        "login": "acc-login",
        "agency": "agency",
        "access_directologists": ["Иванов"],
        "type": "ad_href",
        "old_text": "/old",
        "new_text": "/new",
        "mode": "link",
    })

    assert out == {"replaced": 0, "errors": []}
    assert [c[0] for c in calls] == ["campaigns", "ads"]


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


def test_content_editor_sitelink_retry_succeeds_when_new_text_already_live():
    def fail_api(*args, **kwargs):
        raise AssertionError("already-applied retry must not write through API")

    class FailGrid:
        def __init__(self, login):
            raise AssertionError("already-applied retry must not create Grid client")

    content = {
        "sitelinks": [
            {
                "set_id": 30,
                "level": "campaign",
                "items": [{"title": "Новая", "description": "Описание", "href": "https://example.com"}],
                "campaign_ids": [1, 2],
                "usages": [
                    {"campaign_id": 1, "campaign_name": "camp1", "adgroup_id": 0},
                    {"campaign_id": 2, "campaign_name": "camp2", "adgroup_id": 0},
                ],
            },
            {
                "set_id": "uac:3",
                "source": "uac",
                "items": [{"title": "Новая", "description": "Описание", "href": "https://example.com"}],
                "campaign_id": 3,
                "usages": [{"campaign_id": 3, "campaign_name": "uac", "adgroup_id": 0}],
            },
        ],
    }

    out = content_editor._do_replace(
        "token", "login", "sitelink_title", "Старая", "Новая", content, fail_api, fail_api,
        grid_client_factory=FailGrid,
    )

    assert out["errors"] == []
    assert out["already_applied"] is True
    assert out["replaced"] == 3
    assert out["matched_new_sitelink_sets"] == 2


def test_content_editor_rebinds_responsive_ad_sitelinks_via_grid_rmw():
    class FakeGrid:
        def __init__(self, login):
            self.login = login
            self.last_ad_update_errors = []
            self.current_set_id = "3"
            self.updated_items = []

        def _read_unified_campaign_update_payloads(self, campaign_ids):
            assert campaign_ids == [1]
            return {1: {"inheritableSitelinkSet": {"sitelinkSetId": None}}}

        def add_sitelink_set(self, sitelinks):
            assert sitelinks == [{
                "title": "Новая",
                "description": "Описание",
                "href": "https://example.com",
            }]
            return 40

        def adaptive_ads_for_update(self, campaign_ids, ad_ids):
            assert campaign_ids == [1]
            assert ad_ids == [10]
            return {
                10: {
                    "id": 10,
                    "campaignId": 1,
                    "href": "https://example.com/landing",
                    "displayHref": "example.com/landing",
                    "titles": [{"text": "Заголовок"}],
                    "bodies": [{"text": "Текст"}],
                    "imageHashes": [],
                    "images": [],
                    "adPrice": None,
                    "creativeIds": [],
                    "button": None,
                    "inheritableCallouts": {"policy": "INHERIT"},
                    "inheritableSitelinkSet": {"policy": "OVERRIDE", "sitelinkSetId": self.current_set_id},
                    "permalinkId": None,
                    "phoneId": None,
                }
            }

        def update_ad_images(self, items, allow_empty_images=False):
            assert allow_empty_images is True
            assert len(items) == 1
            self.updated_items = items
            assert items[0]["inheritableSitelinkSet"] == {"policy": "OVERRIDE", "sitelinkSetId": "40"}
            self.current_set_id = "40"
            self.last_ad_update_errors = []
            return 1

    def fail_api(*args, **kwargs):
        raise AssertionError("OAuth API must not be used for ResponsiveAd sitelink replace")

    content = {
        "ads": [],
        "sitelinks": [{
            "set_id": 3,
            "items": [{"title": "Ссылка", "description": "Описание", "href": "https://example.com"}],
            "usages": [{"campaign_id": 1, "campaign_name": "camp", "adgroup_id": 2}],
        }],
        "callouts": [],
        "_ads_by_set": {"3": [{
            "ad_id": 10,
            "subtype": "ResponsiveAd",
            "campaign_name": "camp",
            "adgroup_name": "group",
        }]},
    }

    out = content_editor._do_replace(
        "token", "login", "sitelink_title", "Ссылка", "Новая", content, fail_api, fail_api,
        grid_client_factory=FakeGrid,
    )

    assert out["replaced"] == 1
    assert out["errors"] == []
    assert out["grid"]["new_sitelink_set_ids"] == [40]


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
            "name": "Campaign",
            "state": "COMPLETE",
            "startDate": "2026-07-03",
            "dayBudget": "0",
            "enableCompanyInfo": False,
            "excludePausedCompetingAds": False,
            "hasAddMetrikaTagToUrl": False,
            "hasAddOpenstatTagToUrl": False,
            "hasExtendedGeoTargeting": False,
            "attributionModel": "AUTOMATIC",
            "broadMatch": {"broadMatchFlag": False, "broadMatchGoalId": None, "broadMatchLimit": 0},
            "id": "123",
        }
    })

    class Response:
        status_code = 200
        text = "{}"

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


def test_grid_set_campaign_callouts_retries_transient_grid_errors(monkeypatch):
    client = grid_finalize.GridClient.__new__(grid_finalize.GridClient)
    client.login = "login"
    attempts = []

    monkeypatch.setattr(client, "_bootstrap_csrf", lambda: None)
    monkeypatch.setattr(client, "_read_unified_campaign_update_payloads", lambda ids: {
        123: {
            "name": "Campaign",
            "state": "COMPLETE",
            "startDate": "2026-07-03",
            "dayBudget": "0",
            "enableCompanyInfo": False,
            "excludePausedCompetingAds": False,
            "hasAddMetrikaTagToUrl": False,
            "hasAddOpenstatTagToUrl": False,
            "hasExtendedGeoTargeting": False,
            "attributionModel": "AUTOMATIC",
            "broadMatch": {"broadMatchFlag": False, "broadMatchGoalId": None, "broadMatchLimit": 0},
            "id": "123",
        }
    })

    class Response:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload
            self.text = json.dumps(payload, ensure_ascii=False)

        def json(self):
            return self._payload

    def fake_post(op, query, variables):
        attempts.append(op)
        if len(attempts) == 1:
            return Response({
                "errors": [{
                    "message": "Внутренняя ошибка сервера. При обращении в службу поддержки укажите reqid",
                }]
            })
        return Response({
            "data": {
                "updateCampaigns": {
                    "updatedCampaigns": [{"id": "123"}],
                    "validationResult": {"errors": []},
                }
            }
        })

    monkeypatch.setattr(client, "_post", fake_post)

    out = client.set_campaign_callouts([123], [10, 20])

    assert out == [{"id": "123"}]
    assert attempts == ["UpdateCampaigns", "UpdateCampaigns"]


def test_grid_set_campaign_sitelink_set_sends_required_attribution_model(monkeypatch):
    client = grid_finalize.GridClient.__new__(grid_finalize.GridClient)
    client.login = "login"
    sent = {}

    monkeypatch.setattr(client, "_bootstrap_csrf", lambda: None)
    monkeypatch.setattr(client, "_read_unified_campaign_update_payloads", lambda ids: {
        123: {
            "name": "Campaign",
            "state": "COMPLETE",
            "startDate": "2026-07-03",
            "dayBudget": "0",
            "enableCompanyInfo": False,
            "excludePausedCompetingAds": False,
            "hasAddMetrikaTagToUrl": False,
            "hasAddOpenstatTagToUrl": False,
            "hasExtendedGeoTargeting": False,
            "attributionModel": "AUTOMATIC",
            "broadMatch": {"broadMatchFlag": False, "broadMatchGoalId": None, "broadMatchLimit": 0},
            "id": "123",
        }
    })

    class Response:
        status_code = 200
        text = "{}"

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


def test_grid_set_campaign_sitelink_set_retries_transient_grid_errors(monkeypatch):
    client = grid_finalize.GridClient.__new__(grid_finalize.GridClient)
    client.login = "login"
    attempts = []

    monkeypatch.setattr(client, "_bootstrap_csrf", lambda: None)
    monkeypatch.setattr(client, "_read_unified_campaign_update_payloads", lambda ids: {
        123: {
            "name": "Campaign",
            "state": "COMPLETE",
            "startDate": "2026-07-03",
            "dayBudget": "0",
            "enableCompanyInfo": False,
            "excludePausedCompetingAds": False,
            "hasAddMetrikaTagToUrl": False,
            "hasAddOpenstatTagToUrl": False,
            "hasExtendedGeoTargeting": False,
            "attributionModel": "AUTOMATIC",
            "broadMatch": {"broadMatchFlag": False, "broadMatchGoalId": None, "broadMatchLimit": 0},
            "id": "123",
        }
    })

    class Response:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload
            self.text = json.dumps(payload, ensure_ascii=False)

        def json(self):
            return self._payload

    def fake_post(op, query, variables):
        attempts.append(op)
        if len(attempts) == 1:
            return Response({
                "errors": [{
                    "message": "Внутренняя ошибка сервера. При обращении в службу поддержки укажите reqid",
                }]
            })
        return Response({
            "data": {
                "updateCampaigns": {
                    "updatedCampaigns": [{"id": "123"}],
                    "validationResult": {"errors": []},
                }
            }
        })

    monkeypatch.setattr(client, "_post", fake_post)

    out = client.set_campaign_sitelink_set([123], 456)

    assert out == [{"id": "123"}]
    assert attempts == ["UpdateCampaigns", "UpdateCampaigns"]


def test_load_cookie_local_accepts_yandex_direct_nested_cookie_file(tmp_path, monkeypatch):
    secret_dir = tmp_path / ".secret"
    nested = secret_dir / "yandex_direct"
    nested.mkdir(parents=True)
    (nested / "cookies.json").write_text('{"agency": "cookie=value"}', encoding="utf-8")
    monkeypatch.setattr(campaign, "_find_secret_dir", lambda start=None: secret_dir)

    assert campaign.load_cookie_local("agency") == "cookie=value"


# ── Скоуп/доступ: _scope_check + снапшот в make_job_executor ──────────────────
# Характеризуют ТЕКУЩЕЕ поведение защиты доступа (было 0 тестов). _scope_check —
# модульная функция без Flask: allowed=None → полный доступ; []=нет выданных;
# иначе сверка directologist аккаунта из public.local_gsheet_sites со списком.

class _FakeScopeConn:
    """Мини-заглушка psycopg2-соединения для _scope_check/_pairs_allowed.

    row — то, что вернёт fetchone() (dict для RealDictCursor или tuple)."""

    def __init__(self, row):
        self._row = row
        self.executed = []
        self.closed = False

    def cursor(self, cursor_factory=None):
        return self

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._row

    def close(self):
        self.closed = True


def test_scope_check_full_access_allows_any_login_without_db():
    def victory_conn():
        raise AssertionError("allowed=None must short-circuit before touching DB")

    ok, err = content_editor._scope_check(victory_conn, "acc-login", None)

    assert ok is True
    assert err == ""


def test_scope_check_empty_login_rejected():
    ok, err = content_editor._scope_check(lambda: None, "  ", None)

    assert ok is False
    assert "login обязателен" in err


def test_scope_check_empty_allowed_list_denies_everything():
    def victory_conn():
        raise AssertionError("empty allowed must deny before touching DB")

    ok, err = content_editor._scope_check(victory_conn, "acc-login", [])

    assert ok is False
    assert "нет выданных директологов" in err


def test_scope_check_unknown_login_not_found_in_gsheet():
    conn = _FakeScopeConn(row=None)

    ok, err = content_editor._scope_check(lambda: conn, "acc-login", ["Иванов"])

    assert ok is False
    assert "не найден" in err
    assert conn.closed is True
    assert conn.executed[0][1] == ("acc-login",)


def test_scope_check_foreign_directologist_denied():
    conn = _FakeScopeConn(row={"directologist": "Чужой"})

    ok, err = content_editor._scope_check(lambda: conn, "acc-login", ["Иванов"])

    assert ok is False
    assert "нет доступа к аккаунту" in err
    assert conn.closed is True


def test_scope_check_own_directologist_allowed():
    conn = _FakeScopeConn(row={"directologist": "Иванов"})

    ok, err = content_editor._scope_check(lambda: conn, "acc-login", ["Иванов", "Петров"])

    assert ok is True
    assert err == ""


def test_make_job_executor_rechecks_allowed_snapshot_against_live_directologist():
    # Снапшот allowed кладётся в job при enqueue; воркер ПЕРЕПРОВЕРЯЕТ его через
    # _scope_check по живой БД — подмена чужого login в payload не проходит.
    conn = _FakeScopeConn(row={"directologist": "Чужой"})

    execute = content_editor.make_job_executor(
        victory_conn=lambda: conn,
        token_for_login=lambda *a, **k: (_ for _ in ()).throw(AssertionError("scope must fail first")),
        direct_tokens=lambda: (_ for _ in ()).throw(AssertionError("scope must fail first")),
        v5_call=lambda *a, **k: None,
        v501_svc=lambda *a, **k: None,
    )

    import pytest
    with pytest.raises(RuntimeError) as ei:
        execute({"login": "acc-login", "access_directologists": ["Иванов"], "type": "ad_title"})

    assert "нет доступа к аккаунту" in str(ei.value)


def test_make_job_executor_passes_scope_then_reaches_token_stage():
    # Позитив: снапшот совпал с живым directologist → скоуп пройден, дальше воркер
    # идёт за токенами (здесь их нет → характерная ошибка следующего этапа).
    conn = _FakeScopeConn(row={"directologist": "Иванов"})

    execute = content_editor.make_job_executor(
        victory_conn=lambda: conn,
        token_for_login=lambda *a, **k: (None, None),
        direct_tokens=lambda: [],
        v5_call=lambda *a, **k: None,
        v501_svc=lambda *a, **k: None,
    )

    import pytest
    with pytest.raises(RuntimeError) as ei:
        execute({"login": "acc-login", "access_directologists": ["Иванов"], "type": "ad_title"})

    assert "нет агентских токенов" in str(ei.value)


def test_make_job_executor_campaign_rename_skips_account_snapshot(monkeypatch):
    conn = _FakeScopeConn(row={"directologist": "Иванов"})
    calls = []

    def boom_load_account(*_args, **_kwargs):
        raise AssertionError("campaign_rename must not load full account snapshot")

    def fake_do_replace(token, login, typ, old_text, new_text, content, v5_call, v501_svc,
                        *, mode, grid_client_factory=None):
        calls.append((token, login, typ, old_text, new_text, content, mode))
        return {"replaced": 1, "errors": []}

    monkeypatch.setattr(content_editor, "_load_account", boom_load_account)
    monkeypatch.setattr(content_editor, "_do_replace", fake_do_replace)

    execute = content_editor.make_job_executor(
        victory_conn=lambda: conn,
        token_for_login=lambda *a, **k: ("tok", "agency"),
        direct_tokens=lambda: {"agency": "tok"},
        v5_call=lambda *a, **k: None,
        v501_svc=lambda *a, **k: None,
    )

    payload = json.dumps({"campaign_renames": {"1": "new"}}, ensure_ascii=False)
    out = execute({
        "login": "acc-login",
        "access_directologists": ["Иванов"],
        "type": "campaign_rename",
        "old_text": payload,
        "new_text": payload,
        "mode": "rename",
    })

    assert out == {"replaced": 1, "errors": []}
    assert calls == [("tok", "acc-login", "campaign_rename", payload, payload, {}, "rename")]


def test_make_job_executor_scopes_grid_cookie_to_job_agency(monkeypatch):
    conn = _FakeScopeConn(row={"directologist": "Иванов"})
    calls = []

    def fake_load_account(*_args, **_kwargs):
        return {"links": []}

    def fake_pick_cookie(login, *, accounts, force_refresh=False):
        calls.append(("pick", login, accounts, force_refresh))
        return "cookie-for-job-agency"

    def fake_get_grid_client(login, cookie=None, cookie_only=False):
        calls.append(("grid", login, cookie, cookie_only))
        return {"grid": login, "cookie": cookie}

    def fake_do_replace(token, login, typ, old_text, new_text, content, v5_call, v501_svc,
                        *, mode, grid_client_factory=None):
        grid = grid_client_factory(login)
        calls.append(("replace", token, login, typ, old_text, new_text, content, mode, grid))
        return {"replaced": 1, "errors": []}

    monkeypatch.setattr(content_editor, "_load_account", fake_load_account)
    monkeypatch.setattr(content_editor, "_do_replace", fake_do_replace)
    monkeypatch.setattr(campaign, "pick_working_cookie", fake_pick_cookie)
    monkeypatch.setattr(grid_finalize, "get_grid_client", fake_get_grid_client)

    execute = content_editor.make_job_executor(
        victory_conn=lambda: conn,
        token_for_login=lambda *a, **k: ("tok", "agency"),
        direct_tokens=lambda: {"agency": "tok"},
        v5_call=lambda *a, **k: None,
        v501_svc=lambda *a, **k: None,
    )

    out = execute({
        "login": "acc-login",
        "agency": "victorylotsofads1",
        "access_directologists": ["Иванов"],
        "type": "ad_href",
        "old_text": "/old",
        "new_text": "/new",
        "mode": "link",
    })

    assert out == {"replaced": 1, "errors": []}
    assert ("pick", "acc-login", ("victorylotsofads1",), True) in calls
    assert ("grid", "acc-login", "cookie-for-job-agency", False) in calls


# ── _validate_permutation ────────────────────────────────────────────────────
def test_validate_permutation_valid_swap():
    perm, why = content_editor._validate_permutation([1, 0])
    assert perm == [1, 0]
    assert why == ""


def test_validate_permutation_rejects_duplicates():
    perm, why = content_editor._validate_permutation([0, 0])
    assert perm == []
    assert "биекцией" in why


def test_validate_permutation_rejects_out_of_range():
    perm, why = content_editor._validate_permutation([0, 2])
    assert perm == []
    assert "биекцией" in why


def test_validate_permutation_rejects_too_short():
    for bad in ([], [0]):
        perm, why = content_editor._validate_permutation(bad)
        assert perm == []
        assert "минимум 2 позиции" in why


def test_validate_permutation_rejects_identity():
    perm, why = content_editor._validate_permutation([0, 1, 2])
    assert perm == []
    assert "тождественна" in why


def test_validate_permutation_rejects_non_int():
    perm, why = content_editor._validate_permutation(["a", "b"])
    assert perm == []
    assert "целых индексов" in why


# ── _reorder_sitelinks ───────────────────────────────────────────────────────
class _FakeReorderGrid:
    def __init__(self, login):
        self.login = login
        self.added = []
        self.rebinds = []

    def add_sitelink_set(self, sitelinks):
        self.added.append(list(sitelinks))
        return 900 + len(self.added)

    def set_campaign_sitelink_set(self, campaign_ids, sitelink_set_id):
        self.rebinds.append((list(campaign_ids), sitelink_set_id))
        return [{"id": str(cid)} for cid in campaign_ids]


def test_reorder_sitelinks_invalid_perm_short_circuits():
    grid_calls = []

    def factory(login):
        grid_calls.append(login)
        return _FakeReorderGrid(login)

    out = content_editor._reorder_sitelinks(
        "token", "login", [0, 1],  # тождественная — отсекается до обхода наборов
        {"sitelinks": [{"set_id": 1, "level": "campaign",
                        "campaign_ids": [1], "items": [{"title": "A"}, {"title": "B"}]}]},
        lambda *a, **k: None,
        grid_client_factory=factory,
    )

    assert out["replaced"] == 0
    assert out["reports"] == []
    assert "тождественна" in out["errors"][0]
    assert grid_calls == []  # набор ни разу не тронут


def test_reorder_sitelinks_campaign_level_applies_and_reads_back():
    grid = _FakeReorderGrid("login")
    content = {
        "sitelinks": [{
            "set_id": 55, "set_title": "Набор", "source": "grid", "level": "campaign",
            "campaign_ids": [11, 12],
            "items": [
                {"title": "Первая", "href": "https://a", "description": "d1"},
                {"title": "Вторая", "href": "https://b", "description": "d2"},
            ],
        }],
    }

    out = content_editor._reorder_sitelinks(
        "token", "login", [1, 0], content, lambda *a, **k: None,
        grid_client_factory=lambda login: grid,
    )

    assert out["replaced"] == 1
    assert out["applied_sets"] == 1
    assert out["campaigns_touched"] == 2
    rep = out["reports"][0]
    assert rep["status"] == "applied"
    assert rep["before"] == ["Первая", "Вторая"]
    assert rep["after"] == ["Вторая", "Первая"]
    assert rep["campaign_ids"] == [11, 12]
    # порядок реально применён к новому набору
    assert [it["title"] for it in grid.added[0]] == ["Вторая", "Первая"]
    assert grid.rebinds[0][0] == [11, 12]


def test_reorder_sitelinks_target_set_uses_edited_items_instead_of_raw_permutation():
    grid = _FakeReorderGrid("login")
    content = {
        "sitelinks": [{
            "set_id": 55, "set_title": "Набор", "source": "grid", "level": "campaign",
            "campaign_ids": [11],
            "items": [
                {"title": "Первая", "href": "https://a", "description": "d1"},
                {"title": "Вторая", "href": "https://b", "description": "d2"},
            ],
        }],
    }

    out = content_editor._reorder_sitelinks(
        "token", "login", [1, 0], content, lambda *a, **k: None,
        target_set_id=55,
        edited_items=[
            {"title": "Новый 1", "href": "https://x", "description": "dx"},
            {"title": "Новый 2", "href": "https://y", "description": "dy"},
        ],
        grid_client_factory=lambda login: grid,
    )

    assert out["replaced"] == 1
    assert grid.added[0] == [
        {"title": "Новый 1", "href": "https://x", "description": "dx"},
        {"title": "Новый 2", "href": "https://y", "description": "dy"},
    ]


def test_reorder_sitelinks_skips_when_order_unchanged():
    # Дубликаты-по-кортежу: perm не тождественна, но _apply даёт тот же список
    # кортежей → skip «порядок не изменился», Grid НЕ вызывается.
    grid = _FakeReorderGrid("login")
    content = {
        "sitelinks": [{
            "set_id": 7, "source": "grid", "level": "campaign", "campaign_ids": [1],
            "items": [
                {"title": "X", "href": "https://x", "description": "d"},
                {"title": "X", "href": "https://x", "description": "d"},
            ],
        }],
    }

    out = content_editor._reorder_sitelinks(
        "token", "login", [1, 0], content, lambda *a, **k: None,
        grid_client_factory=lambda login: grid,
    )

    assert out["replaced"] == 0
    assert out["skipped_sets"] == 1
    assert out["reports"][0]["status"] == "skipped"
    assert "порядок не изменился" in out["reports"][0]["reason"]
    assert grid.added == []
    assert grid.rebinds == []


def test_reorder_sitelinks_skips_set_shorter_than_perm():
    grid = _FakeReorderGrid("login")
    content = {
        "sitelinks": [{
            "set_id": 8, "source": "grid", "level": "campaign", "campaign_ids": [1],
            "items": [{"title": "Одна", "href": "https://a", "description": "d"}],
        }],
    }

    out = content_editor._reorder_sitelinks(
        "token", "login", [1, 0], content, lambda *a, **k: None,
        grid_client_factory=lambda login: grid,
    )

    assert out["replaced"] == 0
    assert out["skipped_sets"] == 1
    assert out["reports"][0]["status"] == "skipped"
    assert "перестановка требует 2" in out["reports"][0]["reason"]
    assert grid.added == []


def test_reorder_sitelinks_target_set_id_filters_other_sets():
    grid = _FakeReorderGrid("login")
    content = {
        "sitelinks": [
            {"set_id": 100, "source": "grid", "level": "campaign", "campaign_ids": [1],
             "items": [{"title": "A", "href": "https://a", "description": "d1"},
                       {"title": "B", "href": "https://b", "description": "d2"}]},
            {"set_id": 200, "source": "grid", "level": "campaign", "campaign_ids": [2],
             "items": [{"title": "C", "href": "https://c", "description": "d3"},
                       {"title": "D", "href": "https://d", "description": "d4"}]},
        ],
    }

    out = content_editor._reorder_sitelinks(
        "token", "login", [1, 0], content, lambda *a, **k: None,
        target_set_id=200,
        grid_client_factory=lambda login: grid,
    )

    assert out["applied_sets"] == 1
    assert len(out["reports"]) == 1
    assert out["reports"][0]["set_id"] == 200
    assert grid.rebinds == [([2], 901)]


def test_reorder_sitelinks_uac_branch_patches_and_confirms_read_back():
    # UAC-ветка: перечитывает живую деталь, PATCH массива sitelinks, read-back по
    # полному кортежу (title,href,description). _sl_tuple подтверждает новый порядок.
    class FakeUacClient:
        def __init__(self):
            self.csrf = "csrf"
            self.sitelinks = [
                {"title": "П1", "href": "https://1", "description": "оп1"},
                {"title": "П2", "href": "https://2", "description": "оп2"},
            ]
            self.patched = []

        def link_info(self, url):
            return {}

        def _request(self, method, path, *, params=None, json_body=None, step=""):
            if method == "GET":
                return {"result": {"id": 501, "sitelinks": [dict(x) for x in self.sitelinks]}}
            if method == "PATCH":
                self.patched.append(json_body)
                self.sitelinks = [dict(x) for x in json_body["sitelinks"]]
                return {"result": {"id": 501, "sitelinks": self.sitelinks}}
            raise AssertionError(method)

    client = FakeUacClient()
    content = {
        "sitelinks": [{
            "set_id": "uac:501", "source": "uac", "campaign_id": 501,
            "items": [
                {"title": "П1", "href": "https://1", "description": "оп1"},
                {"title": "П2", "href": "https://2", "description": "оп2"},
            ],
        }],
    }

    out = content_editor._reorder_sitelinks(
        "token", "login", [1, 0], content, lambda *a, **k: None,
        uac_client_factory=lambda login: client,
    )

    assert out["replaced"] == 1
    assert out["uac_sets"] == 1
    rep = out["reports"][0]
    assert rep["status"] == "applied"
    assert rep["orig_order"] == ["П1", "П2"]
    assert [x["title"] for x in client.patched[0]["sitelinks"]] == ["П2", "П1"]


def test_reorder_sitelinks_responsive_ad_level_uses_grid_rmw_rebind():
    class _Grid:
        def __init__(self, login):
            self.login = login
            self.added = []
            self.current_set_id = "55"
            self.last_ad_update_errors = []

        def add_sitelink_set(self, sitelinks):
            self.added.append(list(sitelinks))
            return 901

        def set_campaign_sitelink_set(self, campaign_ids, sitelink_set_id):
            assert campaign_ids == [11]
            assert sitelink_set_id == 901
            return [{"id": "11"}]

        def adaptive_ads_for_update(self, campaign_ids, ad_ids):
            assert campaign_ids == [11]
            assert ad_ids == [101]
            return {
                101: {
                    "id": 101,
                    "campaignId": 11,
                    "href": "https://example.com",
                    "displayHref": "example.com",
                    "titles": [{"text": "T1"}],
                    "bodies": [{"text": "B1"}],
                    "imageHashes": [],
                    "images": [],
                    "adPrice": None,
                    "creativeIds": [],
                    "button": None,
                    "inheritableCallouts": {"policy": "INHERIT"},
                    "inheritableSitelinkSet": {"policy": "OVERRIDE", "sitelinkSetId": self.current_set_id},
                    "permalinkId": None,
                    "phoneId": None,
                }
            }

        def update_ad_images(self, items, allow_empty_images=False):
            assert allow_empty_images is True
            assert items[0]["inheritableSitelinkSet"] == {"policy": "INHERIT"}
            self.current_set_id = None
            self.last_ad_update_errors = []
            return 1

    content = {
        "sitelinks": [{
            "set_id": 55,
            "set_title": "Набор",
            "source": "grid",
            "level": "ad",
            "campaign_ids": [],
            "usages": [{"campaign_id": 11, "adgroup_id": 22}],
            "items": [
                {"title": "Первая", "href": "https://a", "description": "d1"},
                {"title": "Вторая", "href": "https://b", "description": "d2"},
            ],
        }],
        "_ads_by_set": {"55": [{"ad_id": 101, "subtype": "ResponsiveAd"}]},
        "_ads_inventory": [{"ad_id": 101, "campaign_id": 11, "subtype": "ResponsiveAd"}],
    }

    out = content_editor._reorder_sitelinks(
        "token", "login", [1, 0], content, lambda *a, **k: None,
        grid_client_factory=lambda login: _Grid(login),
    )

    assert out["replaced"] == 1
    assert out["ads_touched"] == 1
    rep = out["reports"][0]
    assert rep["status"] == "applied"
    assert rep["new_set_id"] == 901
    assert rep["campaign_ids"] == [11]
    assert rep["ads_cleared"] == 1


def test_assign_sitelinks_accountwide_propagates_full_items_to_campaigns_responsive_and_uac():
    class _Grid:
        def __init__(self, login):
            self.login = login
            self.last_ad_update_errors = []
            self.current_resp_set_id = "55"
            self.campaign_rebinds = []

        def add_sitelink_set(self, sitelinks):
            assert sitelinks == [{
                "title": "Эталон 1",
                "href": "https://example.com/one",
                "description": "Desc 1",
            }, {
                "title": "Эталон 2",
                "href": "https://example.com/two",
                "description": "Desc 2",
            }]
            return 990

        def set_campaign_sitelink_set(self, campaign_ids, sitelink_set_id):
            self.campaign_rebinds.append((list(campaign_ids), sitelink_set_id))
            return [{"id": str(cid)} for cid in campaign_ids]

        def text_ads_for_update(self, campaign_ids, ad_ids):
            assert campaign_ids == [11, 12]
            assert ad_ids == [100]
            return {
                100: {
                    "id": 100,
                    "href": "https://example.com/text",
                    "hrefParams": "",
                    "displayHref": "",
                    "title": "Text",
                    "titleExtension": None,
                    "body": "Body",
                    "isMobile": False,
                    "erirAdDescription": None,
                    "imageHashes": [],
                    "images": [],
                    "logoImageHash": None,
                    "button": None,
                    "adPrice": None,
                    "creativeId": None,
                    "inheritableSitelinkSet": {"policy": "OVERRIDE", "sitelinkSetId": "44"},
                    "inheritableCallouts": {"policy": "INHERIT"},
                    "permalinkWithPhone": {"policy": "CLEAR"},
                    "rmw_unsafe": "",
                }
            }

        def update_text_ads(self, items, allow_empty_image_hashes=False):
            assert allow_empty_image_hashes is True
            assert items[0]["inheritableSitelinkSet"] == {"policy": "INHERIT"}
            return 1

        def adaptive_ads_for_update(self, campaign_ids, ad_ids):
            assert campaign_ids == [11, 12]
            assert ad_ids == [101]
            return {
                101: {
                    "id": 101,
                    "campaignId": 11,
                    "href": "https://example.com",
                    "displayHref": "example.com",
                    "titles": [{"text": "T1"}],
                    "bodies": [{"text": "B1"}],
                    "imageHashes": [],
                        "images": [],
                        "adPrice": None,
                        "creativeIds": [],
                        "button": None,
                        "inheritableCallouts": {"policy": "INHERIT"},
                        "inheritableSitelinkSet": {"policy": "OVERRIDE", "sitelinkSetId": self.current_resp_set_id},
                        "permalinkId": None,
                        "phoneId": None,
                    }
                }

        def update_ad_images(self, items, allow_empty_images=False):
            assert allow_empty_images is True
            assert items[0]["inheritableSitelinkSet"] == {"policy": "INHERIT"}
            self.current_resp_set_id = None
            return 1

    class _FakeUacClient:
        def __init__(self, login):
            self.login = login
            self.csrf = "csrf"
            self.details = {
                7001: {"id": 7001, "sitelinks": [{"title": "Old", "href": "https://old", "description": "old"}]},
            }

        def link_info(self, _url):
            return {}

        def _request(self, method, path, json_body=None, step=""):
            cid = int(path.rsplit("/", 1)[-1])
            if method == "GET":
                return {"result": dict(self.details[cid])}
            if method == "PATCH":
                self.details[cid]["sitelinks"] = [dict(x) for x in (json_body.get("sitelinks") or [])]
                return {"result": dict(self.details[cid])}
            raise AssertionError((method, path, step))

    def fake_v5_call(*args, **kwargs):
        raise AssertionError("v5 rebind path is not expected in this scenario")

    content = {
        "_campaign_ids": [11, 12, 7001],
        "_campaign_types": {11: "TEXT_CAMPAIGN", 12: "TEXT_CAMPAIGN", 7001: "UNIFIED_CAMPAIGN"},
        "_uac_campaign_ids": [7001],
        "_ads_inventory": [
            {"ad_id": 100, "campaign_id": 11, "subtype": "TextAd"},
            {"ad_id": 101, "campaign_id": 11, "subtype": "ResponsiveAd"},
        ],
    }
    items = [
        {"title": "Эталон 1", "href": "https://example.com/one", "description": "Desc 1"},
        {"title": "Эталон 2", "href": "https://example.com/two", "description": "Desc 2"},
    ]

    out = content_editor._assign_sitelink_items_accountwide(
        "token", "login", items, content, fake_v5_call,
        grid_client_factory=lambda login: _Grid(login),
        uac_client_factory=lambda login: _FakeUacClient(login),
    )

    assert out["replaced"] == 1
    assert out["errors"] == []
    assert out["new_sitelink_set_id"] == 990
    assert out["campaigns_touched"] == 2
    assert out["ads_touched"] == 2
    assert out["uac_campaigns_touched"] == 1


def test_assign_callout_accountwide_creates_and_binds_to_supported_campaigns():
    class _Grid:
        def __init__(self, login):
            self.login = login
            self.calls = []

        def add_callouts(self, texts):
            assert texts == ["Номера уже в CRM", "Спрос уже есть"]
            return {"Номера уже в CRM": 777, "Спрос уже есть": 778}

        def set_campaign_callouts(self, campaign_ids, callout_ids):
            self.calls.append((list(campaign_ids), list(callout_ids)))
            return [{"id": str(cid)} for cid in campaign_ids]

    content = {
        "_campaign_ids": [11, 12, 13],
        "_campaign_types": {11: "TEXT_CAMPAIGN", 12: "TEXT_CAMPAIGN", 13: "UNIFIED_CAMPAIGN"},
    }

    out = content_editor._assign_callout_accountwide(
        "login",
        ["Номера уже в CRM.", "Спрос уже есть"],
        content,
        grid_client_factory=lambda login: _Grid(login),
    )

    assert out["replaced"] == 1
    assert out["errors"] == []
    assert out["new_callout_ids"] == [777, 778]
    assert out["new_texts"] == ["Номера уже в CRM", "Спрос уже есть"]
    assert out["campaigns_touched"] == 2
    assert out["updated_campaign_ids"] == [11, 12]
    assert out["skipped_campaign_ids"] == [13]
    assert out["skipped_reasons"] == {"13": "Мастер кампаний / Товарный мастер не поддерживает уточнения"}
    assert out["warnings"] == []


def test_assign_sitelink_accountwide_clears_responsive_override_before_campaign_bind():
    class _Grid:
        def __init__(self, login):
            self.login = login
            self.current_set_id = "501"

        def add_sitelink_set(self, sitelinks):
            assert sitelinks == [{
                "title": "Новая 1",
                "href": "https://example.com/one",
                "description": "Desc 1",
            }]
            return 990

        def set_campaign_sitelink_set(self, campaign_ids, sitelink_set_id):
            assert campaign_ids == [11, 12]
            assert sitelink_set_id == 990
            return [{"id": "11"}, {"id": "12"}]

        def adaptive_ads_for_update(self, campaign_ids, ad_ids):
            assert campaign_ids == [11, 12]
            assert ad_ids == [101]
            return {
                101: {
                    "id": 101,
                    "campaignId": 12,
                    "href": "https://example.com",
                    "displayHref": "example.com",
                    "titles": [{"text": "T1"}],
                    "bodies": [{"text": "B1"}],
                    "imageHashes": [],
                    "images": [],
                    "adPrice": None,
                    "creativeIds": [],
                    "button": None,
                    "inheritableCallouts": {"policy": "INHERIT"},
                    "inheritableSitelinkSet": {"policy": "OVERRIDE", "sitelinkSetId": self.current_set_id},
                    "permalinkId": None,
                    "phoneId": None,
                }
            }

        def update_ad_images(self, items, allow_empty_images=False):
            assert allow_empty_images is True
            assert items == [{
                "id": 101,
                "campaignId": 12,
                "href": "https://example.com",
                "displayHref": "example.com",
                "titles": [{"text": "T1"}],
                "bodies": [{"text": "B1"}],
                "imageHashes": [],
                "images": [],
                "adPrice": None,
                "creativeIds": [],
                "button": None,
                "inheritableCallouts": {"policy": "INHERIT"},
                "inheritableSitelinkSet": {"policy": "INHERIT"},
                "permalinkId": None,
                "phoneId": None,
            }]
            self.current_set_id = None
            return 1

    def fake_v5_call(*args, **kwargs):
        raise AssertionError("v5 ad rebind is not expected in this scenario")

    content = {
        "_campaign_ids": [11, 12],
        "_campaign_types": {11: "TEXT_CAMPAIGN", 12: "TEXT_CAMPAIGN"},
        "_uac_campaign_ids": [],
        "_ads_inventory": [
            {"ad_id": 101, "campaign_id": 12, "subtype": "ResponsiveAd"},
        ],
    }
    items = [{"title": "Новая 1", "href": "https://example.com/one", "description": "Desc 1"}]

    out = content_editor._assign_sitelink_items_accountwide(
        "token", "login", items, content, fake_v5_call,
        grid_client_factory=lambda login: _Grid(login),
    )

    assert out["replaced"] == 1
    assert out["errors"] == []
    assert out["warnings"] == []
    assert out["campaigns_touched"] == 2
    assert out["ads_touched"] == 1
    assert out["skipped_campaign_ids"] == []


def test_assign_callout_accountwide_clears_responsive_overrides_before_campaign_bind():
    class _Grid:
        def __init__(self, login):
            self.login = login
            self.last_ad_update_errors = []
            self.callouts_policy = {"policy": "OVERRIDE", "calloutIds": ["12"]}

        def add_callouts(self, texts):
            assert texts == ["Спрос уже есть"]
            return {"Спрос уже есть": 778}

        def adaptive_ads_for_update(self, campaign_ids, ad_ids):
            assert campaign_ids == [13]
            assert ad_ids == [501]
            return {
                501: {
                    "id": 501,
                    "campaignId": "13",
                    "href": "https://example.com",
                    "displayHref": "example",
                    "titles": [{"text": "T"}],
                    "bodies": [{"text": "B"}],
                    "imageHashes": [],
                    "images": [],
                    "adPrice": None,
                    "creativeIds": [],
                    "button": None,
                    "inheritableCallouts": dict(self.callouts_policy),
                    "inheritableSitelinkSet": {"policy": "INHERIT"},
                    "permalinkId": None,
                    "phoneId": None,
                }
            }

        def update_ad_images(self, items, allow_empty_images=False):
            assert allow_empty_images is True
            assert items[0]["inheritableCallouts"] == {"policy": "INHERIT"}
            self.last_ad_update_errors = []
            self.callouts_policy = {"policy": "INHERIT"}
            return 1

        def set_campaign_callouts(self, campaign_ids, callout_ids):
            assert campaign_ids == [13]
            assert callout_ids == [778]
            return [{"id": "13"}]

    grid = _Grid("login")

    content = {
        "_campaign_ids": [13],
        "_campaign_types": {13: "TEXT_CAMPAIGN"},
        "_ads_inventory": [
            {"ad_id": 501, "campaign_id": 13, "subtype": "ResponsiveAd"},
        ],
    }

    out = content_editor._assign_callout_accountwide(
        "login",
        ["Спрос уже есть"],
        content,
        grid_client_factory=lambda login: grid,
    )

    assert out["replaced"] == 1
    assert out["errors"] == []
    assert out["updated_campaign_ids"] == [13]
    assert out["skipped_campaign_ids"] == []
    assert out["warnings"] == []
    assert out["ads_touched"] == 1


def test_assign_callout_accountwide_rejects_empty_text_after_normalization():
    out = content_editor._assign_callout_accountwide(
        "login",
        ["...", ""],
        {"_campaign_ids": [11]},
        grid_client_factory=lambda login: (_ for _ in ()).throw(AssertionError("grid should not be called")),
    )

    assert out == {
        "replaced": 0,
        "errors": ["после удаления недопустимых символов список уточнений пустой"],
    }


def test_normalize_callout_texts_dedupes_and_limits_to_10():
    out = content_editor._normalize_callout_texts([
        "  Спрос уже есть  ",
        "Спрос уже есть",
        "Номера уже в CRM.",
        "Без подрядчиков",
        "Лиды без рекламы",
        "50 номеров бесплатно",
        "Номера уже сегодня",
        "Звоните им первыми",
        "От 6 ₽ за номер",
        "Без предоплаты",
        "Горячие лиды сегодня",
        "Лишняя строка сверх лимита",
    ])

    assert out == [
        "Спрос уже есть",
        "Номера уже в CRM",
        "Без подрядчиков",
        "Лиды без рекламы",
        "50 номеров бесплатно",
        "Номера уже сегодня",
        "Звоните им первыми",
        "От 6 ₽ за номер",
        "Без предоплаты",
        "Горячие лиды сегодня",
    ]


def test_assign_callout_accountwide_skips_uac_campaigns_before_grid():
    class _Grid:
        def __init__(self, login):
            self.login = login

        def _read_unified_campaign_update_payloads(self, ids):
            assert ids == [11]
            return {11: {"inheritableCallouts": {"calloutIds": []}}}

        def _narrow_bases(self, payloads, ids, op):
            return ({11: {"inheritableCallouts": {"calloutIds": []}}}, {})

        def add_callouts(self, texts):
            return {"Спрос уже есть": 701}

        def set_campaign_callouts(self, campaign_ids, callout_ids):
            return [{"id": "11"}]

    out = content_editor._assign_callout_accountwide(
        "login",
        ["Спрос уже есть"],
        {
            "_campaign_ids": [11, 21],
            "_campaign_types": {11: "TEXT_CAMPAIGN", 21: "UAC"},
        },
        grid_client_factory=lambda login: _Grid(login),
    )

    assert out["replaced"] == 1
    assert out["updated_campaign_ids"] == [11]
    assert out["skipped_campaign_ids"] == [21]
    assert out["skipped_reasons"] == {"21": "Мастер кампаний / Товарный мастер не поддерживает уточнения"}


def test_create_set_async_allows_single_feed_when_explicitly_confirmed():
    """Одиночный фид разрешён при явном маркере намерения (Семён 2026-07-28).

    Гейт `stale_client_single_feed` ловит УСТАРЕВШИЙ клиент: живой интерфейс в полном прогоне
    шлёт single_feed=false принудительно, поэтому потоковый запрос с одиночным фидом приходит
    либо из протухшей вкладки, либо от осознанного вызова. Протухшая вкладка не знает поля
    `single_feed_confirmed` — по нему и различаем.

    Проверяем, что запрос проходит ИМЕННО этот гейт: он спотыкается уже на следующем
    (product-only), а не на single_feed.
    """
    app.testing = True
    client = app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True
        session["is_admin"] = True
        session["username"] = "route-smoke"

    response = client.post("/direct/api/create_set_async", json={
        "login": "gen-ses-test",
        "agent": "scherbakova",
        "site_type": "Мультибренд",
        "stream_content": True,
        "single_feed": True,
        "single_feed_confirmed": True,
        "items": [{
            "name": "tp7_cpc_site — ТК - Haval - КС + Автотаргетинг",
            "type": "product",
            "_plan_agent": "scherbakova",
            "_plan_site_type": "Мультибренд",
        }],
    })

    data = response.get_json()
    assert response.status_code == 409
    assert "stale_client_single_feed" not in data          # этот гейт пройден
    assert data["stale_client_product_only_stream"] is True  # споткнулись на следующем

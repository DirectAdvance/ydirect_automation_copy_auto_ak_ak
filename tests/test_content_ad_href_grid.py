from direct import content_replace_routes as repl


def test_ad_href_textad_uses_grid_rmw_not_v5_update():
    class Grid:
        last_ad_update_errors = []

        def text_ads_for_update(self, campaign_ids, ad_ids):
            assert campaign_ids == [703]
            assert ad_ids == [173]
            return {
                173: {
                    "id": 173,
                    "href": "https://example.com/old",
                    "hrefParams": "",
                    "title": "Title",
                    "body": "Body",
                    "imageHashes": [],
                    "rmw_unsafe": "",
                }
            }

        def update_text_ads(self, items, allow_empty_image_hashes=False):
            assert allow_empty_image_hashes is True
            assert items[0]["href"] == "https://example.com/new"
            return 1

    def v5_call(svc, method, token, login, params):
        assert (svc, method) == ("ads", "get")
        return {"result": {"Ads": [{
            "Id": 173,
            "Type": "TEXT_AD",
            "TextAd": {"Href": "https://example.com/new"},
        }]}}

    def v501_svc(*args, **kwargs):
        raise AssertionError("v501 ads.update must not be used for ad_href")

    out = repl._do_replace(
        "token",
        "login",
        "ad_href",
        "/old",
        "/new",
        {"links": [{
            "ad_id": 173,
            "campaign_id": 703,
            "type": "TextAd",
            "path": "/old",
            "href": "https://example.com/old",
        }]},
        v5_call,
        v501_svc,
        mode="link",
        grid_client_factory=lambda login: Grid(),
    )

    assert out["replaced"] == 1
    assert out["confirmed"] == 1
    assert out["errors"] == []


def test_ad_href_responsive_uses_grid_rmw_not_v501_update():
    class Grid:
        last_ad_update_errors = []

        def adaptive_ads_for_update(self, campaign_ids, ad_ids):
            assert campaign_ids == [704]
            assert ad_ids == [174]
            return {
                174: {
                    "id": 174,
                    "href": "https://example.com/old",
                    "titles": [{"text": "Title"}],
                    "bodies": [{"text": "Body"}],
                    "imageHashes": [],
                    "creativeIds": [],
                }
            }

        def update_adaptive_text_ads(self, items):
            assert items[0]["href"] == "https://example.com/new"
            return 1

    def v5_call(svc, method, token, login, params):
        assert (svc, method) == ("ads", "get")
        return {"result": {"Ads": [{
            "Id": 174,
            "Type": "TEXT_AD",
            "ResponsiveAd": {"Href": "https://example.com/new"},
        }]}}

    def v501_svc(*args, **kwargs):
        raise AssertionError("v501 ads.update must not be used for ad_href")

    out = repl._do_replace(
        "token",
        "login",
        "ad_href",
        "/old",
        "/new",
        {"links": [{
            "ad_id": 174,
            "campaign_id": 704,
            "type": "ResponsiveAd",
            "path": "/old",
            "href": "https://example.com/old",
        }]},
        v5_call,
        v501_svc,
        mode="link",
        grid_client_factory=lambda login: Grid(),
    )

    assert out["replaced"] == 1
    assert out["confirmed"] == 1
    assert out["errors"] == []


def test_ad_href_skips_blocked_account_before_grid_write():
    grid_called = False

    class Grid:
        def text_ads_for_update(self, *_args, **_kwargs):
            nonlocal grid_called
            grid_called = True
            return {}

    def v5_call(svc, method, token, login, params):
        assert (svc, method) == ("ads", "update")
        assert params["Ads"][0]["TextAd"]["Href"] == "https://example.com/new"
        return {
            "error": {
                "error_code": 3000,
                "error_string": "Нет доступа к API",
                "error_detail": "Аккаунт пользователя блокирован",
            }
        }

    out = repl._do_replace(
        "token",
        "blocked-login",
        "ad_href",
        "/old",
        "/new",
        {"links": [{
            "ad_id": 173,
            "campaign_id": 703,
            "type": "TextAd",
            "path": "/old",
            "href": "https://example.com/old",
        }]},
        v5_call,
        lambda *a, **k: None,
        mode="link",
        grid_client_factory=lambda login: Grid(),
    )

    assert out["blocked_account"] is True
    assert out["replaced"] == 0
    assert out["errors"] == []
    assert "заблокирован" in out["message"]
    assert grid_called is False


def test_ad_href_generic_api_denied_does_not_skip_grid_write():
    class Grid:
        last_ad_update_errors = []

        def text_ads_for_update(self, _campaign_ids, _ad_ids):
            return {
                173: {
                    "id": 173,
                    "href": "https://example.com/old",
                    "hrefParams": "",
                    "title": "Title",
                    "body": "Body",
                    "imageHashes": [],
                    "rmw_unsafe": "",
                }
            }

        def update_text_ads(self, items, allow_empty_image_hashes=False):
            return 1

    calls = []

    def v5_call(svc, method, token, login, params):
        calls.append((svc, method))
        if method == "update":
            return {
                "error": {
                    "error_code": 3000,
                    "error_string": "Нет доступа к API",
                }
            }
        return {"result": {"Ads": [{
            "Id": 173,
            "Type": "TEXT_AD",
            "TextAd": {"Href": "https://example.com/new"},
        }]}}

    out = repl._do_replace(
        "token",
        "api-denied-login",
        "ad_href",
        "/old",
        "/new",
        {"links": [{
            "ad_id": 173,
            "campaign_id": 703,
            "type": "TextAd",
            "path": "/old",
            "href": "https://example.com/old",
        }]},
        v5_call,
        lambda *a, **k: None,
        mode="link",
        grid_client_factory=lambda login: Grid(),
    )

    assert out["replaced"] == 1
    assert out["errors"] == []
    assert "blocked_account" not in out
    assert calls[0] == ("ads", "update")
    assert calls[-1] == ("ads", "get")

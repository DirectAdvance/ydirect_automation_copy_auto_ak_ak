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

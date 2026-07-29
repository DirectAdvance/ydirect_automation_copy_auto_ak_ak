from direct import campaign
from direct.clients import gateway_client


def test_pick_cookie_preserves_managing_agency_rights_error(monkeypatch):
    monkeypatch.setattr(campaign, "_ACCOUNT_COOKIE_CACHE", {})
    monkeypatch.setattr(campaign, "_AGENCY_RESOLVER", lambda _login: "agency-main")
    monkeypatch.setattr(campaign, "DEFAULT_COOKIE_ACCOUNTS", ("agency-main", "agency-other"))
    monkeypatch.setattr(campaign, "_cookie_retry_delay_seconds", lambda: 0)

    def fake_fetch(acc):
        return f"fresh-{acc}"

    def fake_local(acc):
        return f"local-{acc}"

    class FakeUacClient:
        def __init__(self, cookie, ulogin):
            self.cookie = cookie
            self.ulogin = ulogin

        def link_info(self, _url):
            if "agency-main" in self.cookie:
                raise RuntimeError('[linkinfo] HTTP 403: {"code":54,"text":"Нет прав"}')
            raise RuntimeError('[linkinfo] HTTP 401: {"code":0,"text":"No rights"}')

    monkeypatch.setattr(campaign, "fetch_cookie_glavpotok", fake_fetch)
    monkeypatch.setattr(campaign, "load_cookie_local", fake_local)
    monkeypatch.setattr(campaign, "UacClient", FakeUacClient)

    try:
        campaign._pick_working_cookie_local("porg-nxhtsz6c")
    except RuntimeError as exc:
        msg = str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert "кука управляющего агентства agency-main" in msg
    assert "porg-nxhtsz6c" in msg
    assert "Нет прав" in msg
    assert not msg.startswith("ни одна кука не подошла")


def test_pick_cookie_single_explicit_account_preserves_rights_error_without_resolver(monkeypatch):
    monkeypatch.setattr(campaign, "_ACCOUNT_COOKIE_CACHE", {})
    monkeypatch.setattr(campaign, "_AGENCY_RESOLVER", None)
    monkeypatch.setattr(campaign, "_cookie_retry_delay_seconds", lambda: 0)

    def fake_fetch(acc):
        return f"fresh-{acc}"

    def fake_local(acc):
        return f"local-{acc}"

    class FakeUacClient:
        def __init__(self, cookie, ulogin):
            self.cookie = cookie
            self.ulogin = ulogin

        def link_info(self, _url):
            raise RuntimeError('[linkinfo] HTTP 403: {"code":54,"text":"Нет прав"}')

    monkeypatch.setattr(campaign, "fetch_cookie_glavpotok", fake_fetch)
    monkeypatch.setattr(campaign, "load_cookie_local", fake_local)
    monkeypatch.setattr(campaign, "UacClient", FakeUacClient)

    try:
        campaign._pick_working_cookie_local("porg-m6atla56", ("agency-main",))
    except RuntimeError as exc:
        msg = str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert "кука управляющего агентства agency-main" in msg
    assert "porg-m6atla56" in msg
    assert "Нет прав" in msg
    assert not msg.startswith("ни одна кука не подошла")


def test_gateway_cookie_rights_error_does_not_fallback(monkeypatch):
    def fake_get(_path, _params, _timeout):
        raise gateway_client.GatewayHTTPError(
            502,
            {"error": "кука управляющего агентства agency-main не имеет web/Grid-прав к ulogin=porg-x"},
        )

    monkeypatch.setattr(gateway_client, "_get", fake_get)
    monkeypatch.setattr(
        campaign,
        "_pick_working_cookie_local",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("fallback must not run")),
    )

    try:
        gateway_client.gw_cookie("porg-x")
    except RuntimeError as exc:
        msg = str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert "не имеет web/Grid-прав" in msg

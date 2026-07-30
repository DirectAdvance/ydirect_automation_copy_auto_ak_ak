"""run_apply_pool должен пропускать заблокированный login ДО _get_ads_batch/_batch_update_prices
(гейт account_write_blocked), не трогая остальные логины того же агентства — per-login latch,
не agency_dead."""
from direct import price_check


def test_run_apply_pool_skips_blocked_login_without_touching_healthy_login(monkeypatch):
    calls = {"ad_ids_for_logins": [], "batch_update_logins": []}

    monkeypatch.setattr(
        price_check, "account_write_blocked",
        lambda login, *, agency="": ("аккаунт заблокирован (тест)" if login == "blocked-login" else ""),
    )

    def fake_ad_ids_for(_victory_conn, login, url, price_direct, oldprice_direct):
        assert login != "blocked-login", "blocked login must not reach _ad_ids_for"
        calls["ad_ids_for_logins"].append(login)
        return [999]

    def fake_get_ads_batch(v5_call, token, login, ad_ids):
        return {aid: {"Id": aid} for aid in ad_ids}

    def fake_batch_update_prices(v5_call, token, login, entries):
        calls["batch_update_logins"].append(login)
        return {e["ad_id"]: "success" for e in entries}

    monkeypatch.setattr(price_check, "_ad_ids_for", fake_ad_ids_for)
    monkeypatch.setattr(price_check, "_get_ads_batch", fake_get_ads_batch)
    monkeypatch.setattr(price_check, "_batch_update_prices", fake_batch_update_prices)
    monkeypatch.setattr(price_check, "job_control", lambda *a, **k: "")
    monkeypatch.setattr(price_check, "_job_update", lambda *a, **k: None)

    finished = {}

    def fake_job_finish(_victory_conn_rw, job_id, status, result, **fields):
        finished[job_id] = {"status": status, "result": result, **fields}

    monkeypatch.setattr(price_check, "_job_finish", fake_job_finish)

    deps = {
        "victory_conn": lambda: None,
        "victory_conn_rw": lambda: None,
        "token_for_login": lambda login, agency, tokens: ("tok", agency or "victorylotsofads1"),
        "direct_tokens": lambda: {"victorylotsofads1": "tok"},
        "v5_call": lambda *a, **k: {"result": {}},
    }
    items = [
        {"login": "blocked-login", "agency": "victorylotsofads1", "url": "/a",
         "price_direct": 100, "oldprice_direct": None, "price_feed": 90, "oldprice_feed": None},
        {"login": "healthy-login", "agency": "victorylotsofads1", "url": "/b",
         "price_direct": 100, "oldprice_direct": None, "price_feed": 90, "oldprice_feed": None},
    ]

    price_check.run_apply_pool(deps, [{"job_id": "job1", "items": items}], chain_after=False)

    assert calls["ad_ids_for_logins"] == ["healthy-login"]
    assert calls["batch_update_logins"] == ["healthy-login"]

    res = finished["job1"]["result"]
    assert {"login": "blocked-login", "url": "/a", "reason": "account_blocked"} in res["skipped"]
    assert res["ads_updated"] == 1
    assert finished["job1"]["status"] == "done"

import importlib.util
from pathlib import Path


def _load_direct_copy():
    path = Path("/opt/scripts/work/slepki_direktologov/scripts/direct_copy.py")
    spec = importlib.util.spec_from_file_location("direct_copy_transient_retry", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_direct_call_retries_api_1000(monkeypatch):
    dc = _load_direct_copy()
    calls = []

    class Response:
        status_code = 200
        text = ""

        def json(self):
            calls.append("json")
            if len(calls) == 1:
                return {
                    "error": {
                        "error_code": 1000,
                        "error_string": "Сервис временно недоступен",
                        "error_detail": "",
                    }
                }
            return {"result": {"Ads": [{"Id": 1}]}}

    post_calls = []

    def fake_post(*args, **kwargs):
        post_calls.append((args, kwargs))
        return Response()

    monkeypatch.setattr(dc.requests, "post", fake_post)
    monkeypatch.setattr(dc.time, "sleep", lambda _seconds: None)

    res = dc.direct_call(
        "ads",
        "get",
        {"SelectionCriteria": {}},
        auth=dc.AuthContext("token"),
        client_login="porg-mjyh6hjv",
        attempts=5,
    )

    assert res == {"Ads": [{"Id": 1}]}
    assert len(post_calls) == 2

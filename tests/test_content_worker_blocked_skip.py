from direct import content_worker


def test_finish_blocked_account_skip_is_done_without_agent_board(monkeypatch):
    executed = []
    notified = []

    def fake_jobs_exec(query, params=(), fetch=None):
        executed.append((query, params, fetch))

    monkeypatch.setattr(content_worker.ce, "_jobs_exec", fake_jobs_exec)
    monkeypatch.setattr(
        content_worker,
        "notify_content_job_error",
        lambda *_args, **_kwargs: notified.append(True),
    )

    content_worker._finish("ce_blocked", {
        "replaced": 0,
        "errors": [],
        "blocked_account": True,
        "message": "аккаунт blocked-login заблокирован для записи в Direct, задача пропущена",
    })

    assert notified == []
    assert executed
    query, params, _fetch = executed[0]
    assert "SET status=%s" in query
    assert params[0] == "done"
    assert params[1] == 0
    assert "заблокирован" in params[4]
    assert params[5] == "ce_blocked"

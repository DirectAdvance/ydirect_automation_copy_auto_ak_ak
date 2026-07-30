"""account_service.account_write_blocked — единый гейт "аккаунт заблокирован для записи".

Используется ДО любой мутации (content-editor executor + price-check apply pool).
Источник сигнала — Grid userFeatures BLOCKED (тот же, что уже отдаёт фронту
_check_blocks_response), здесь — синхронно для одного login."""
from direct import account_service


def test_account_write_blocked_own_agency_reports_blocked(monkeypatch):
    calls = []

    monkeypatch.setattr(account_service.cmc, "load_cookie", lambda ag: f"cookie-{ag}")
    monkeypatch.setattr(account_service, "_block_bootstrap", lambda cookie, ag: "csrf")

    def fake_block_check(cookie, csrf, login):
        calls.append((cookie, csrf, login))
        return True

    monkeypatch.setattr(account_service, "_block_check", fake_block_check)

    reason = account_service.account_write_blocked("porg-blocked", agency="victorylotsofads1")

    assert "заблокирован" in reason
    # своё агентство пробуется первым и сразу даёт однозначный ответ — остальные не нужны
    assert calls == [("cookie-victorylotsofads1", "csrf", "porg-blocked")]


def test_account_write_blocked_healthy_account_not_blocked(monkeypatch):
    monkeypatch.setattr(account_service.cmc, "load_cookie", lambda ag: f"cookie-{ag}")
    monkeypatch.setattr(account_service, "_block_bootstrap", lambda cookie, ag: "csrf")
    monkeypatch.setattr(account_service, "_block_check", lambda cookie, csrf, login: False)

    reason = account_service.account_write_blocked("porg-pl6iavd5", agency="victorylotsofads1")

    assert reason == ""


def test_account_write_blocked_falls_back_to_known_agencies_when_own_cookie_dead(monkeypatch):
    calls = []

    def fake_load_cookie(ag):
        if ag == "victorylotsofads1":
            raise RuntimeError("cookie dead")
        return f"cookie-{ag}"

    monkeypatch.setattr(account_service.cmc, "load_cookie", fake_load_cookie)
    monkeypatch.setattr(account_service, "_block_bootstrap", lambda cookie, ag: "csrf")

    def fake_block_check(cookie, csrf, login):
        calls.append(cookie)
        return True

    monkeypatch.setattr(account_service, "_block_check", fake_block_check)

    reason = account_service.account_write_blocked("porg-blocked", agency="victorylotsofads1")

    assert "заблокирован" in reason
    assert calls  # ответ пришёл от следующего агентства из _KNOWN_AGENCIES
    assert calls[0] != "cookie-victorylotsofads1"


def test_account_write_blocked_no_cookie_anywhere_fails_open(monkeypatch):
    monkeypatch.setattr(account_service.cmc, "load_cookie", lambda ag: (_ for _ in ()).throw(RuntimeError("dead")))

    reason = account_service.account_write_blocked("porg-any", agency="victorylotsofads1")

    assert reason == ""


def test_account_write_blocked_empty_login_returns_empty():
    assert account_service.account_write_blocked("", agency="victorylotsofads1") == ""

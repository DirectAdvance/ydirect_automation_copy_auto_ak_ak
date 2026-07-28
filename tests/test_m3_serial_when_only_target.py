"""Веер потоков на ЕДИНСТВЕННЫЙ M3-эндпоинт запрещён — он там только вредит.

Инцидент 2026-07-28 (porg-xjxpfxby, 6 кампаний из 28): провайдер попапа — OpenRouter, он умер
(HTTP 402 «нет кредитов» + сеть до openrouter.ai недоступна), circuit-breaker взвёлся, и весь
веер из 4 потоков свалился фолбэком на ОДИН M3. `_M3EndpointGuard` держит лок на URL и пускает
по одному: один поток проходил, три ждали `M3_ENDPOINT_LOCK_MAX_WAIT` (90с) и падали с
TimeoutError. В логе это выглядело как `titles=0 texts=0 sitelinks=0 error=` — пустой контент
без ошибки, кампании уезжали на шаблонах из БД.

Условие серийности — «M3 реально принимает запросы»: он primary ИЛИ он secondary при взведённом
OR-breaker. Пока OpenRouter жив, веер сохраняется — там параллель действительно ускоряет.
"""

import pytest

from direct import llm_providers as lp


@pytest.fixture()
def spy(monkeypatch):
    """Ловим max_workers, с которым создаётся пул, и не ходим в сеть."""
    seen = {}

    class _Pool:
        def __init__(self, max_workers=None):
            seen["max_workers"] = max_workers

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def submit(self, fn, arg):
            class _F:
                def __init__(self, v):
                    self._v = v

                def result(self):
                    return self._v
            return _F(fn(arg))

    import concurrent.futures as cf
    monkeypatch.setattr(cf, "ThreadPoolExecutor", _Pool)
    monkeypatch.setattr(cf, "as_completed", lambda futs: list(futs))
    monkeypatch.setattr(lp, "_run_with_llm_heartbeat_job",
                        lambda job, fn, *a, **kw: ("ok", None), raising=False)
    return seen


def _run(provider, tasks, spy):
    _url, _par = lp._llm_pair_for(provider)
    _par(tasks)
    return spy["max_workers"]


ONE_URL = [("http://127.0.0.1:8086", [{"role": "user", "content": "x"}], {})] * 4
TWO_URLS = [("http://127.0.0.1:8086", [{"role": "user", "content": "x"}], {}),
            ("http://127.0.0.1:8087", [{"role": "user", "content": "y"}], {})]


def test_m3_primary_single_url_is_serial(spy, monkeypatch):
    monkeypatch.setattr(lp._OR_BREAKER, "_tripped", False, raising=False)
    assert _run("m3", ONE_URL, spy) == 1


def test_openrouter_alive_keeps_fanout(spy, monkeypatch):
    """OpenRouter жив → параллель нужна, M3 в этот момент не трогаем."""
    monkeypatch.setattr(lp._OR_BREAKER, "is_tripped", lambda: False, raising=False)
    assert _run("openrouter", ONE_URL, spy) == 4


def test_openrouter_dead_falls_back_serially(spy, monkeypatch):
    """Тот самый инцидент: OR мёртв, весь веер уходит на один M3 → должен стать серийным."""
    monkeypatch.setattr(lp._OR_BREAKER, "is_tripped", lambda: True, raising=False)
    assert _run("openrouter", ONE_URL, spy) == 1


def test_several_m3_urls_keep_fanout(spy, monkeypatch):
    """Разные M3-эндпоинты — у каждого свой лок, веер осмыслен."""
    monkeypatch.setattr(lp._OR_BREAKER, "is_tripped", lambda: True, raising=False)
    assert _run("openrouter", TWO_URLS, spy) == 2


def test_guard_wait_stays_bounded():
    """Серийность не должна отменять ограниченное ожидание лока — иначе джоба зависнет."""
    assert lp._M3_ENDPOINT_LOCK_MAX_WAIT > 0

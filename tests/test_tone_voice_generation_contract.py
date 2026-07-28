import inspect
from pathlib import Path
import threading
import time

from direct import ai_agents as A
from direct import create_set_prefetch
from direct import create_set_orchestrator
from direct import create_set_feed_builders
from direct import create_set_input
from direct import create_set_tp1_builders
from direct import create_set_master_product
from direct import queue_server
from direct import llm_providers
from direct import uac_client
from direct.create_content import run_gen_campaign_content

_DIRECT_DIR = Path(__file__).resolve().parents[1]


def test_assemble_campaign_live_mode_does_not_copy_agent_corpus():
    agent = A.get_agent("pavlov")

    live_content, _ = A.assemble_campaign(
        [], [], [], agent, site_type="Мультибренд", brand="", allow_corpus_fill=False
    )
    legacy_content, _ = A.assemble_campaign(
        [], [], [], agent, site_type="Мультибренд", brand="", allow_corpus_fill=True
    )

    assert live_content == {"titles": [], "texts": [], "sitelinks": []}
    assert legacy_content["titles"] or legacy_content["texts"] or legacy_content["sitelinks"]


def test_live_generation_blocks_template_fallback_when_llm_is_empty(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        llm_providers,
        "record_content_fallback",
        lambda enabled, reason="": recorded.append((enabled, reason)),
    )

    def slepok_content_must_not_be_used(*_args, **_kwargs):
        raise AssertionError("live generation must not copy direct_slepok_content as final content")

    result = run_gen_campaign_content(
        login="porg-test",
        agent=A.get_agent("pavlov"),
        agent_key="pavlov",
        item={"name": "РСЯ - Общая", "type": "rsya", "tp": "tp1"},
        fast_mode=True,
        _bad_ad_sitelink=lambda *_a, **_k: False,
        _bad_ad_text=lambda *_a, **_k: False,
        _bad_ad_title=lambda *_a, **_k: False,
        _brand_from_coder=lambda _item: "",
        _display_brand=lambda value: value,
        _extract_text_candidates=lambda _raw: [],
        _extract_title_candidates=lambda _raw: ([], ""),
        _m3_complete_parallel=lambda _requests: [("", "llm empty"), ("", "llm empty"), ("", "llm empty")],
        _m3_complete_url=lambda *_a, **_k: ("", "llm empty"),
        _promo_ctx=lambda _login: {
            "domain": "example.ru",
            "city": "Екатеринбург",
            "site_type": "Мультибренд",
            "salon": "",
        },
        _promo_extract_json=lambda _text: {},
        _slepok_content_get=slepok_content_must_not_be_used,
        _title2_blocklist=lambda: (set(), set()),
        _variant_norm_key=lambda value: (value or "").lower(),
        _M3_LLM_REPAIR_TIMEOUT=1,
        _M3_LLM_TIMEOUT_14B=1,
        _M3_CONTENT_IDLE_TIMEOUT=1,
        _M3_LLM_URLS_14B=["u0", "u1", "u2"],
        _M3_LLM_URL_72B="u72",
        _RU_CITIES=set(),
    )

    assert result["ok"] is False
    assert result["fallback"] is True
    assert "шаблонный фолбэк запрещён" in result["error"]
    assert result["content"] == {"titles": [], "texts": [], "sitelinks": []}
    assert recorded and recorded[-1][0] is True
    assert "заголовки 0/" in recorded[-1][1]


def test_tp67_live_master_product_does_not_fill_from_templates_after_llm():
    source = inspect.getsource(create_set_master_product.run_master_product_item)

    assert "_live_generated_content" in source
    assert "шаблонный фолбэк запрещён" in source
    assert "_title_fill_pool = (_cf(_llm_titles) if _live_generated_content" in source
    assert "_text_fill_pool = (_cf(_llm_texts) if _live_generated_content" in source
    assert "_sl_fill = [] if _live_generated_content" in source
    assert "len(it_titles) < 5 and not _is_non_auto and not _live_generated_content" in source
    assert "len(_uniq_sl) < 8 and not _live_generated_content" in source


def test_m3_primary_parallel_serializes_single_endpoint(monkeypatch):
    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_m3(_url, _messages, **_kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return "ok", None

    monkeypatch.setattr(llm_providers, "_m3_preflight_ok", lambda *_a, **_k: True)
    monkeypatch.setattr(llm_providers, "_m3_complete_url", fake_m3)
    llm_providers.arm_m3_breaker("test-single-url", tripped=False)

    _, complete_parallel = llm_providers._llm_pair_for("m3")
    results = complete_parallel([
        ("http://127.0.0.1:8086", [{"role": "user", "content": "titles"}], {}),
        ("http://127.0.0.1:8086", [{"role": "user", "content": "texts"}], {}),
        ("http://127.0.0.1:8086", [{"role": "user", "content": "sitelinks"}], {}),
    ])

    assert results == [("ok", None), ("ok", None), ("ok", None)]
    assert max_active == 1


def test_m3_endpoint_guard_heartbeats_while_waiting(monkeypatch):
    calls = {"heartbeat": 0}
    url = "http://unit-test-m3-lock"

    def fake_heartbeat():
        calls["heartbeat"] += 1

    monkeypatch.setattr(llm_providers, "_touch_running_jobs_heartbeat", fake_heartbeat)
    lock = llm_providers._m3_endpoint_lock(url)
    assert lock.acquire(blocking=False)

    def release_later():
        time.sleep(0.05)
        lock.release()

    releaser = threading.Thread(target=release_later)
    releaser.start()
    with llm_providers._m3_endpoint_guard(url, wait_step=0.01):
        pass
    releaser.join(timeout=1)

    assert calls["heartbeat"] >= 2


def test_m3_parallel_propagates_heartbeat_context(monkeypatch):
    calls = []

    def fake_set(job_id):
        calls.append((job_id, threading.current_thread().name))

    def fake_m3(_url, _messages, **_kwargs):
        return "ok", None

    monkeypatch.setattr(llm_providers, "_current_llm_heartbeat_job", lambda: "job-ctx")
    monkeypatch.setattr(llm_providers, "_set_llm_heartbeat_job", fake_set)
    monkeypatch.setattr(llm_providers, "_m3_complete_url", fake_m3)

    results = llm_providers._m3_complete_parallel([
        ("http://127.0.0.1:8086", [{"role": "user", "content": "titles"}], {}),
        ("http://127.0.0.1:8087", [{"role": "user", "content": "texts"}], {}),
    ])

    assert results == [("ok", None), ("ok", None)]
    assert sum(1 for job_id, _ in calls if job_id == "job-ctx") == 2
    assert sum(1 for job_id, _ in calls if job_id is None) == 2


def test_llm_pair_parallel_propagates_heartbeat_context(monkeypatch):
    calls = []

    def fake_set(job_id):
        calls.append((job_id, threading.current_thread().name))

    monkeypatch.setattr(llm_providers, "_current_llm_heartbeat_job", lambda: "job-ctx")
    monkeypatch.setattr(llm_providers, "_set_llm_heartbeat_job", fake_set)
    monkeypatch.setattr(llm_providers, "_m3_health_or_completion_ok", lambda *_a, **_k: True)
    monkeypatch.setattr(llm_providers, "_m3_complete_url", lambda *_a, **_k: ("ok", None))
    llm_providers.arm_m3_breaker("test-heartbeat-context", tripped=False)

    _, complete_parallel = llm_providers._llm_pair_for("m3")
    results = complete_parallel([
        ("http://127.0.0.1:8086", [{"role": "user", "content": "titles"}], {}),
        ("http://127.0.0.1:8087", [{"role": "user", "content": "texts"}], {}),
    ])

    assert results == [("ok", None), ("ok", None)]
    assert sum(1 for job_id, _ in calls if job_id == "job-ctx") == 2
    assert sum(1 for job_id, _ in calls if job_id is None) == 2


def test_orchestrator_content_prefetch_propagates_heartbeat_context():
    source = inspect.getsource(create_set_orchestrator.create_set_response)

    assert "_run_content_with_heartbeat" in source
    assert "_current_llm_heartbeat_job()" in source
    assert "_content_executor.submit(\n                    _run_content_with_heartbeat" in source


def test_orchestrator_forces_m3_when_openrouter_gate_is_unavailable():
    source = inspect.getsource(create_set_orchestrator.create_set_response)

    assert 'body["llm_provider"] = "m3"' in source
    assert 'body.get("llm_provider") or "openrouter"' in source
    assert 'item_copy["llm_provider"] = _run_llm_provider' in source


def test_create_set_prepare_runs_before_creation_and_orders_fast_api_before_tp1():
    source = inspect.getsource(create_set_orchestrator.create_set_response)

    assert "prepare_job(" in source
    assert "prepare_regular_content=False" in source
    assert "preupload_tp1=False" in source
    assert "start_tp1_image_preupload(" in source
    assert "_wait_tp1_images()" in source
    assert source.index("prepare_job(") < source.index("run_create_set_precreate(")
    assert source.index("prepare_job(") < source.index("from .create_set_resume import already_in_direct")
    assert '"rsya_gallery", "search_gallery"' in source
    assert '_typ == "tp1_rsy"' in source
    assert source.index("start_tp1_image_preupload(") < source.index("if it.get(\"type\") == \"tp1_rsy\"")
    assert source.index("_wait_tp1_images()") < source.index("from .create_set_tp1 import run_create_set_tp1")


def test_prepare_stage_overlaps_image_upload_with_prices_and_content():
    source = inspect.getsource(create_set_prefetch.prepare_job)

    assert "ThreadPoolExecutor" in source
    assert "images_future = ex.submit(_prepare_images)" in source
    assert source.index("images_future = ex.submit(_prepare_images)") < source.index("prices_report =")
    assert source.index("images_future = ex.submit(_prepare_images)") < source.index("content_report =")
    assert "prepare_regular_content" in source
    assert '"deferred_to_item_creation"' in source
    assert "preupload_tp1" in source


def test_first_campaign_watchdog_has_separate_fail_fast_budget():
    source = inspect.getsource(queue_server._create_watchdog_tick)

    assert "_CREATE_FIRST_CAMPAIGN_TIMEOUT" in inspect.getsource(queue_server)
    assert "_CREATE_SET_SLA_PER_CAMPAIGN_SEC" in inspect.getsource(queue_server)
    assert "first_campaign_timeout" in source
    assert "sla_timeout" in source
    assert '"set", "slepok"' in source
    assert "* _CREATE_SET_SLA_PER_CAMPAIGN_SEC" in source
    assert "ни одной кампании за " in source
    assert source.index("_CREATE_FIRST_CAMPAIGN_TIMEOUT") < source.index("_CREATE_RUNNING_TIMEOUT")
    # Бюджет — нижняя граница, а не приговор: убиваем только вместе с ТИШИНОЙ (нет ни стадий
    # создания, ни обработанных item'ов). Иначе сторож режет живой прогон, у которого первая
    # кампания ещё не доехала (2026-07-28, porg-pl6iavd5 — грузил картинки tp2).
    assert "_CREATE_FIRST_CAMPAIGN_STALL" in source
    assert "_stage_timing.last_progress" in source


def test_common_sitelinks_do_not_call_ai_during_creation():
    feed_source = inspect.getsource(create_set_feed_builders._common_sitelinks_fast)
    tp1_source = inspect.getsource(create_set_tp1_builders)
    runtime_source = (_DIRECT_DIR / "automation_runtime.py").read_text(encoding="utf-8")

    assert "_ai_common_sitelinks(" not in feed_source
    assert "DIRECT_CREATE_AI_COMMON_SITELINKS" in runtime_source
    assert "return []" in runtime_source
    assert "sitelinks = _ai_common_sitelinks" not in tp1_source
    assert "sitelinks or _ai_common_sitelinks" not in tp1_source


def test_m3_endpoint_lock_has_bounded_wait():
    import home.seoadvanced.direct.llm_providers as llm_providers

    source = inspect.getsource(llm_providers._M3EndpointGuard.__enter__)
    module_source = inspect.getsource(llm_providers)

    assert "_M3_ENDPOINT_LOCK_MAX_WAIT" in module_source
    assert "TimeoutError" in source
    assert "M3 endpoint занят" in source


def test_create_runtime_accepts_plan_no_cpa_flag_n():
    data = create_set_input.normalize_create_set_input(
        {"login": "porg", "items": [{"name": "tp5_cpc_site"}], "n": True},
        normalize_callout_text=lambda x: str(x or "").strip(),
        callout_semantic_key=lambda x: x,
        parse_number=lambda value, default=0: int(value or default),
    )

    assert data["no_cpa"] is True


def test_worker_cancel_control_is_not_cleared_by_non_owner_process():
    source = inspect.getsource(queue_server._worker_apply_controls)

    assert "_control_applied = False" in source
    assert "_control_applied = True" in source
    assert "if not _control_applied:" in source
    assert source.index("if not _control_applied:") < source.index("UPDATE public.direct_automation_jobs SET control=NULL")


def test_uac_preloaded_content_ids_skip_file_uploads():
    class Client(uac_client.UacClient):
        def __init__(self):
            self.payload = None

        def link_info(self, url):
            return {}

        def upload_image_file(self, *args, **kwargs):
            raise AssertionError("image upload should be skipped")

        def upload_video_file(self, *args, **kwargs):
            raise AssertionError("video upload should be skipped")

        def upload_content(self, *args, **kwargs):
            raise AssertionError("URL upload should be skipped")

        def create_campaign(self, payload):
            self.payload = payload
            return "123"

    spec = uac_client.MasterCampaignSpec(
        href="https://example.test",
        titles=["Купить авто"],
        texts=["Автомобиль в наличии"],
        region_ids=[35],
        counter_id=1,
        goal_id=2,
        cpa=1000,
        week_budget=1000,
        content_ids=["cid-1"],
        image_files=["/tmp/missing.jpg"],
        video_files=["/tmp/missing.mp4"],
    )

    client = Client()
    assert client.create_master_campaign(spec) == "123"
    assert client.payload["content_ids"] == ["cid-1"]


def test_content_pipeline_health_accepts_m3_completion_when_health_blips(monkeypatch):
    monkeypatch.setattr(llm_providers, "_m3_preflight_ok", lambda *_a, **_k: False)
    monkeypatch.setattr(llm_providers, "m3_completion_preflight_ok", lambda *_a, **_k: True)
    monkeypatch.setattr(llm_providers, "_openrouter_probe", lambda *_a, **_k: False)
    monkeypatch.setattr(llm_providers, "_openrouter_completion_probe", lambda *_a, **_k: False)

    status = llm_providers.check_content_pipeline_health(or_retries=1, or_pause=0.0, or_timeout=0.01)

    assert status["m3_alive"] is True
    assert status["or_alive"] is False
    assert status["any_alive"] is True
    assert "M3 completion жив" in status["message"]


def test_content_pipeline_health_rejects_models_only_m3_when_openrouter_unavailable(monkeypatch):
    monkeypatch.setattr(llm_providers, "_m3_preflight_ok", lambda *_a, **_k: True)
    monkeypatch.setattr(llm_providers, "m3_completion_preflight_ok", lambda *_a, **_k: False)
    monkeypatch.setattr(llm_providers, "_openrouter_probe", lambda *_a, **_k: True)
    monkeypatch.setattr(llm_providers, "_openrouter_completion_probe", lambda *_a, **_k: False)

    status = llm_providers.check_content_pipeline_health(or_retries=1, or_pause=0.0, or_timeout=0.01)

    assert status["m3_models_alive"] is True
    assert status["m3_completion_alive"] is False
    assert status["m3_alive"] is False
    assert status["or_alive"] is False
    assert status["any_alive"] is False
    assert "completion" in status["message"]


def test_m3_primary_does_not_fallback_to_openrouter_when_completion_probe_ok(monkeypatch):
    calls = {"m3": 0, "openrouter": 0}

    def fake_m3(_url, _messages, **_kwargs):
        calls["m3"] += 1
        return "m3-ok", None

    def fake_openrouter(_url, _messages, **_kwargs):
        calls["openrouter"] += 1
        return None, "OpenRouter HTTP 402"

    monkeypatch.setattr(llm_providers, "_m3_preflight_ok", lambda *_a, **_k: False)
    monkeypatch.setattr(llm_providers, "m3_completion_preflight_ok", lambda *_a, **_k: True)
    monkeypatch.setattr(llm_providers, "_m3_complete_url", fake_m3)
    monkeypatch.setattr(llm_providers, "_or_complete_url", fake_openrouter)
    llm_providers.arm_m3_breaker("test-health-blip", tripped=False)

    complete_url, _ = llm_providers._llm_pair_for("m3")
    text, err = complete_url("http://127.0.0.1:8086", [{"role": "user", "content": "body"}])

    assert (text, err) == ("m3-ok", None)
    assert calls == {"m3": 1, "openrouter": 0}


def test_m3_primary_trips_breaker_when_m3_dies_midrun(monkeypatch):
    calls = {"health": 0, "openrouter": 0}

    def fake_health():
        calls["health"] += 1
        return False

    def fake_openrouter(_url, _messages, **_kwargs):
        calls["openrouter"] += 1
        return None, "OpenRouter HTTP 402"

    monkeypatch.setattr(llm_providers, "_m3_health_or_completion_ok", fake_health)
    monkeypatch.setattr(llm_providers, "_or_complete_url", fake_openrouter)
    llm_providers.arm_m3_breaker("test-m3-dies-midrun", tripped=False)

    complete_url, _ = llm_providers._llm_pair_for("m3")
    text, err = complete_url("http://127.0.0.1:8086", [{"role": "user", "content": "body"}])
    text2, err2 = complete_url("http://127.0.0.1:8086", [{"role": "user", "content": "body"}])

    assert text is None and text2 is None
    assert "M3 skip" in err
    assert "circuit-breaker" in err2
    assert llm_providers.m3_breaker_tripped() is True
    assert calls == {"health": 1, "openrouter": 2}

from pathlib import Path
from types import SimpleNamespace
import time

from flask import Blueprint, Flask

from direct import agent_board_bridge
from direct.core import queue_server
from direct.clients import grid_read, grid_finalize
from direct.create import create_set_feeds
from direct.content import content_renames_routes
from direct.copy_service import (
    copy_api,
    copy_cleanup,
    copy_engine,
    copy_feeds,
    copy_postprocess,
    copy_settings_steps,
    copy_grid_read,
    copy_uac,
)
from direct.copy_service.copy_api import register_copy_api
from direct.copy_service.copy_request import parse_feed_map, parse_image_hashes
from direct.copy_service.copy_verify_source import build_source_profile
from direct.copy_service.copy_verify_diff import diff_profiles


def test_copy_target_feed_id_prefers_preseeded_id_maps(tmp_path, monkeypatch):
    (tmp_path / "id_maps.json").write_text('{"feeds":{"11":12345}}', encoding="utf-8")
    monkeypatch.setattr(copy_feeds, "_grid_feeds", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(copy_feeds, "_filter_allowed_feed_rows", lambda rows: rows)
    monkeypatch.setattr(copy_feeds, "_feed_key", lambda value: value)

    assert copy_feeds._copy_target_feed_id("target", "agency", Path(tmp_path), "example.ru") == 12345


def test_copy_terminal_status_is_error_when_campaign_failed():
    status, error = copy_engine._copy_terminal_status_from_results([
        {"ok": False, "name": "tp7 product", "error": "нет feed_id"},
    ])

    assert status == "error"
    assert "tp7 product: нет feed_id" in error


def test_copy_expected_snapshot_excludes_selected_archived_v5_campaigns():
    selected = set(range(1, 36))
    uac_rows = [{"id": str(i)} for i in range(29, 36)]
    v5_campaigns = [
        {"Id": i, "Name": f"camp {i}", "Type": "TEXT_CAMPAIGN", "State": "ON", "Status": "ACCEPTED"}
        for i in range(1, 26)
    ] + [
        {"Id": i, "Name": f"archived {i}", "Type": "TEXT_CAMPAIGN", "State": "ARCHIVED", "Status": "MODERATION"}
        for i in range(26, 29)
    ]

    expected, skipped = copy_engine._copy_expected_snapshot_count(selected, uac_rows, v5_campaigns)

    assert expected == 25
    assert [x["Id"] for x in skipped] == [26, 27, 28]


def test_direct_copy_caps_text_ads_per_group_before_add():
    dc = copy_engine._direct_copy_module()
    ads = [
        {"Id": 1, "AdGroupId": 10, "Type": "TEXT_AD", "State": "OFF", "TextAd": {"Title": "off"}},
        {"Id": 2, "AdGroupId": 10, "Type": "TEXT_AD", "State": "ON", "TextAd": {"Title": "on 1"}},
        {"Id": 3, "AdGroupId": 10, "Type": "TEXT_AD", "State": "SUSPENDED", "TextAd": {"Title": "paused"}},
        {"Id": 4, "AdGroupId": 10, "Type": "TEXT_AD", "State": "ON", "TextAd": {"Title": "on 2"}},
        {"Id": 5, "AdGroupId": 10, "Type": "TEXT_AD", "State": "ON", "TextAd": {"Title": "on 3"}},
        {"Id": 6, "AdGroupId": 20, "Type": "TEXT_IMAGE_AD", "State": "ON", "TextImageAd": {}},
    ]

    selected, skipped = dc.select_text_ad_ids_for_add(ads, mapped_ads={})

    assert selected == {"2", "4", "5"}
    assert [x["Id"] for x in skipped] == [3, 1]


def test_direct_copy_drops_feed_filters_for_auto_mapped_target_feed():
    dc = copy_engine._direct_copy_module()
    raw = {"Items": [
        {"Operand": "name", "Operator": "CONTAINS_ANY", "Arguments": ["Hyundai"]},
    ]}

    assert dc.feed_filter_conditions_for_add(raw, keep=True) == raw["Items"]
    assert dc.feed_filter_conditions_for_add(raw, keep=False) == []


def test_copy_search_invariants_retries_small_update_chunks(monkeypatch):
    monkeypatch.setattr(copy_settings_steps, "_SEARCH_INVARIANTS_UPDATE_CHUNK", 2)
    monkeypatch.setattr(copy_settings_steps, "_SEARCH_INVARIANTS_UPDATE_TRIES", 2)

    class FakeGrid:
        def __init__(self):
            self.calls = 0

        def update_unified_adgroups(self, items):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("Read timed out")
            return [int(x["adGroupId"]) for x in items]

    grid = FakeGrid()
    ctx = SimpleNamespace(grid=grid)
    items = [{"adGroupId": "1"}, {"adGroupId": "2"}, {"adGroupId": "3"}]

    updated = copy_settings_steps._update_unified_adgroups_resilient(ctx, 123, items)

    assert updated == [1, 2, 3]
    assert grid.calls == 3


def test_copy_postprocess_executes_image_repair_for_live_fail(monkeypatch):
    actions = [{"action": "images_repair", "campaign_id": 101, "uses_direct_units": False}]
    calls = []

    monkeypatch.setattr(
        copy_postprocess.rgate,
        "executable_images_repairs",
        lambda plan: ([101], actions, []),
    )

    def fake_execute_images_repair(login, ctx, campaign_ids, deps):
        calls.append((login, ctx, campaign_ids, deps))
        return {"ok": True, "repaired": 1}, 200

    def fake_post_verify(out, login, ctx):
        out["post_repair_live_verification"] = {
            "status": "ok",
            "summary": {"errors": 0},
            "repair_plan": {"status": "empty", "actions": []},
        }

    monkeypatch.setattr(copy_postprocess.rex, "execute_images_repair", fake_execute_images_repair)

    out = copy_postprocess._copy_execute_image_repairs(
        "target-login",
        {"body": {}, "results": []},
        {"actions": actions},
        object(),
        post_verify=fake_post_verify,
    )

    assert out["ok"] is True
    assert out["executed"] == 1
    assert calls[0][0] == "target-login"
    assert calls[0][2] == [101]
    assert out["post_repair_live_verification"]["summary"]["errors"] == 0


def test_copy_timed_raises_on_step_timeout(monkeypatch):
    logs = []
    monkeypatch.setattr(copy_postprocess, "_engine", lambda: SimpleNamespace(_copy_job_log=lambda _job, msg: logs.append(msg)))

    def slow_step():
        time.sleep(0.2)
        return {"ok": True}

    started = time.monotonic()
    try:
        copy_postprocess._copy_timed("job", "slow", slow_step, timeout_sec=0.01)
    except TimeoutError as exc:
        assert "slow timeout" in str(exc)
    else:
        raise AssertionError("expected TimeoutError")

    assert time.monotonic() - started < 0.1
    assert logs and logs[0].startswith("[timing] slow:")


def test_copy_demotes_optional_source_grid_errors_only():
    rep = {
        "errors": [
            "source grid read: Grid reauth не получил CSRF для ulogin=porg-source",
            "read source adaptive: Grid reauth не получил CSRF для ulogin=porg-source",
            "promos grid: Grid reauth не получил CSRF для ulogin=porg-source",
            "shopping/listing grid: FeedDefectIds.String.FEED_STATUS_WRONG",
        ]
    }

    copy_postprocess._copy_demote_optional_source_grid_errors(rep, "porg-source")

    assert rep["errors"] == ["shopping/listing grid: FeedDefectIds.String.FEED_STATUS_WRONG"]
    assert len(rep["warnings"]) == 3


def test_copy_cleanup_bounds_optional_grid_campaign_list(monkeypatch):
    monkeypatch.setattr(copy_cleanup, "_COPY_UAC_CLEANUP_GRID_TIMEOUT_SEC", 0.01)

    def slow_grid_list(_login):
        time.sleep(0.2)
        return []

    monkeypatch.setattr(copy_cleanup, "_grid_list_campaigns", slow_grid_list)

    started = time.monotonic()
    try:
        copy_cleanup._grid_list_campaigns_bounded("login")
    except TimeoutError as exc:
        assert "grid_list_campaigns timeout" in str(exc)
    else:
        raise AssertionError("expected TimeoutError")
    assert time.monotonic() - started < 0.1


def test_copy_selected_grid_campaigns_times_out_to_empty(monkeypatch):
    monkeypatch.setattr(copy_grid_read, "_COPY_SELECTED_GRID_LIST_TIMEOUT_SEC", 0.01)

    def slow_grid_list(_login):
        time.sleep(0.2)
        return [{"id": "1"}]

    monkeypatch.setattr(copy_grid_read, "_grid_list_campaigns", slow_grid_list)

    started = time.monotonic()
    rows = copy_grid_read._copy_selected_grid_campaigns("login", {1})

    assert rows == []
    assert time.monotonic() - started < 0.1


def test_copy_cleanup_uac_list_timeout_is_non_critical(monkeypatch):
    logs = []
    monkeypatch.setattr(
        copy_cleanup,
        "_grid_list_campaigns_bounded",
        lambda _login: (_ for _ in ()).throw(TimeoutError("grid_list_campaigns timeout>25s")),
    )
    monkeypatch.setattr(copy_cleanup, "_copy_job_log", lambda _job_id, msg: logs.append(msg))

    errors = []
    deleted = copy_cleanup._copy_cleanup_uac_drafts("job", "login", errors)

    assert deleted == 0
    assert errors == []
    assert "cleanup uac list skipped" in logs[0]


def test_copy_auto_feed_map_matches_used_feeds_by_filename(monkeypatch):
    def fake_grid_feeds(login, _agency):
        if login == "source":
            return [{"id": "3418098", "name": "https://auto-bu-163.ru/used-dostup-k-rasprodazhe-01-b.xml"}]
        return [{"id": "3595433", "name": "https://budrive-novgorod.ru/used-dostup-k-rasprodazhe-01-b.xml"}]

    monkeypatch.setattr(copy_engine, "_resolve_agency_hint", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(copy_engine, "_grid_feeds", fake_grid_feeds)

    assert copy_engine._copy_auto_feed_map("source", "target") == {"3418098": 3595433}


def test_copy_auto_feed_map_uses_target_agency_hint(monkeypatch):
    calls = []

    def fake_grid_feeds(login, agency):
        calls.append((login, agency))
        if login == "source":
            return [{"id": "3418098", "name": "https://auto-bu-163.ru/used.xml"}]
        if agency == "victoryagency14":
            return [{"id": "3595536", "name": "https://budrive-nsk.ru/used.xml"}]
        return []

    monkeypatch.setattr(copy_engine, "_resolve_agency_hint", lambda _login, _hint: "wrong-agency")
    monkeypatch.setattr(copy_engine, "_grid_feeds", fake_grid_feeds)

    assert copy_engine._copy_auto_feed_map(
        "source",
        "target",
        target_agency_hint="victoryagency14",
    ) == {"3418098": 3595536}
    assert ("target", "victoryagency14") in calls


def test_copy_verify_source_callouts_match_writer_union_fallback(tmp_path):
    src_dir = tmp_path
    (src_dir / "campaigns.json").write_text(
        '[{"Id":1,"TextCampaign":{"Settings":[]}}]',
        encoding="utf-8",
    )
    (src_dir / "adgroups.json").write_text('[{"Id":10,"CampaignId":1}]', encoding="utf-8")
    (src_dir / "ads.json").write_text("[]", encoding="utf-8")
    (src_dir / "keywords.json").write_text("[]", encoding="utf-8")
    (src_dir / "callouts.json").write_text(
        '[{"Id":101,"Callout":{"CalloutText":"Гарантия"}},'
        '{"Id":102,"Callout":{"CalloutText":"Авто с ПТС"}}]',
        encoding="utf-8",
    )
    (src_dir / "campaign_callouts.json").write_text("{}", encoding="utf-8")

    profile = build_source_profile(src_dir)

    assert profile["1"]["callout_count"] == 2


def test_copy_verify_accepts_direct_three_ad_cap_for_legacy_group():
    src_profile = {
        "1": {
            "adgroup_count": 1,
            "kw_count": 0,
            "camp_neg_count": 0,
            "shared_set_count": 0,
            "has_promo": False,
            "promo_id": None,
            "ads_with_titles": 4,
            "ads_with_texts": 4,
            "callout_count": 0,
            "has_sitelinks": True,
            "campaign_has_sitelinks": False,
            "ad_sitelinks_count": 4,
            "ads_with_images": 4,
            "audiences": {},
            "bid_modifier_types": [],
            "strategy_name": "SERVING_OFF",
            "ads_with_video": 0,
            "ads_with_button": 0,
            "tracking_norm": "",
            "site_monitoring": True,
            "minus_places": [],
            "shopping_count": 0,
            "listing_count": 0,
            "shopping_filter_signatures_by_group": {},
            "listing_filter_signatures_by_group": {},
            "_ads_count": 4,
        }
    }
    tgt_profile = {
        "2": {
            "adgroup_count": 1,
            "kw_count": 0,
            "shared_set_count": 0,
            "has_promo": False,
            "promo_id": None,
            "ads_with_titles": 3,
            "ads_with_texts": 3,
            "callout_count": 0,
            "has_sitelinks": True,
            "campaign_has_sitelinks": False,
            "ad_sitelinks_count": 3,
            "ads_with_images": 3,
            "audiences": {},
            "strategy_name": "SERVING_OFF",
            "ads_with_video": 0,
            "ads_with_button": 0,
            "ad_price": None,
            "tracking_norm": "",
            "site_monitoring": True,
            "minus_places": [],
            "shopping_count": 0,
            "listing_count": 0,
            "shopping_filter_signatures_by_group": {},
            "listing_filter_signatures_by_group": {},
            "_reads_ok": {"invariants": True, "edit_rows": True, "ad_level_sitelinks": True},
        }
    }

    rows = diff_profiles(src_profile, tgt_profile, {"campaigns": {"1": 2}, "adgroups": {}})
    by_dim = {r["dimension"]: r for r in rows}

    assert by_dim["adaptive_titles_count"]["status"] == "excluded_intentional"
    assert by_dim["adaptive_bodies_count"]["status"] == "excluded_intentional"
    assert by_dim["sitelinks_ad_level_count"]["status"] == "excluded_intentional"
    assert by_dim["ads_with_images"]["status"] == "excluded_intentional"
    assert by_dim["adaptive_titles_count"]["repairable"] is False


def test_copy_auto_feed_map_falls_back_to_existing_listing_feed(monkeypatch):
    def fake_grid_feeds(login, _agency):
        if login == "source":
            return [
                {"id": "3568864", "name": "autopro-154.site — dostup-k-rasprodazhe-live-01-b",
                 "url": "https://autopro-154.site/dostup-k-rasprodazhe-live-01-b.xml"},
                {"id": "3568865", "name": "autopro-154.site — used-feed",
                 "url": "https://autopro-154.site/used-feed.xml"},
            ]
        return [
            {"id": "3612347", "name": "direct_feed.xml",
             "url": "https://budrive-nsk.ru/feed/direct_feed.xml", "listings": []},
            {"id": "3595536", "name": "budrive-nsk.ru — yandex-used-auto",
             "url": "https://budrive-nsk.ru/yandex-used-auto.xml",
             "listings": [{"id": "mark_28", "name": "Haval с пробегом"}]},
        ]

    monkeypatch.setattr(copy_engine, "_resolve_agency_hint", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(copy_engine, "_grid_feeds", fake_grid_feeds)

    assert copy_engine._copy_auto_feed_map("source", "target") == {
        "3568864": 3595536,
        "3568865": 3595536,
    }


def test_copy_auto_feed_map_prefers_listing_fallback_over_empty_exact_match(monkeypatch):
    def fake_grid_feeds(login, _agency):
        if login == "source":
            return [
                {"id": "3586212", "name": "source — live",
                 "url": "https://auto-bu-163.ru/dostup-k-rasprodazhe-live-01-b.xml"},
            ]
        return [
            {"id": "3612741", "name": "dostup-k-rasprodazhe-live-01-b.xml",
             "url": "https://budrive-nsk.ru/dostup-k-rasprodazhe-live-01-b.xml", "listings": []},
            {"id": "3595536", "name": "budrive-nsk.ru — yandex-used-auto",
             "url": "https://budrive-nsk.ru/yandex-used-auto.xml",
             "listings": [{"id": "mark_28", "name": "Haval с пробегом"}]},
        ]

    monkeypatch.setattr(copy_engine, "_resolve_agency_hint", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(copy_engine, "_grid_feeds", fake_grid_feeds)

    assert copy_engine._copy_auto_feed_map("source", "target") == {"3586212": 3595536}


def test_copy_auto_feed_map_from_snapshot_uses_pulled_v5_feed(tmp_path, monkeypatch):
    (tmp_path / "feeds.json").write_text(
        '[{"Id":3586212,"Name":"source live","UrlFeed":{"Url":"https://auto-bu-163.ru/dostup-k-rasprodazhe-live-01-b.xml"}}]',
        encoding="utf-8",
    )

    def fake_grid_feeds(login, agency):
        assert login == "target"
        assert agency == "victoryagency14"
        return [
            {"id": "3612741", "name": "dostup-k-rasprodazhe-live-01-b.xml",
             "url": "https://budrive-nsk.ru/dostup-k-rasprodazhe-live-01-b.xml", "listings": []},
            {"id": "3595536", "name": "budrive-nsk.ru — yandex-used-auto",
             "url": "https://budrive-nsk.ru/yandex-used-auto.xml",
             "listings": [{"id": "mark_28", "name": "Haval с пробегом"}]},
        ]

    monkeypatch.setattr(copy_engine, "_grid_feeds", fake_grid_feeds)

    assert copy_engine._copy_auto_feed_map_from_snapshot(
        tmp_path,
        "target",
        target_agency_hint="victoryagency14",
    ) == {"3586212": 3595536}


def test_copy_enrich_body_context_fills_agent_and_target_site_type(monkeypatch):
    def fake_account_ctx(login):
        if login == "source":
            return {"directologist": "Гордеева Наталья", "site_type": "Монобренд"}
        if login == "target":
            return {"directologist": "Терехов Евгений", "site_type": "С пробегом"}
        return {}

    body = {}
    monkeypatch.setattr(copy_engine, "_account_ctx", fake_account_ctx)

    copy_engine._copy_enrich_body_context(body, "source", "target")

    assert body["agent"] == "gordeeva"
    assert body["site_type"] == "С пробегом"


def test_copy_enrich_body_context_preserves_explicit_values(monkeypatch):
    monkeypatch.setattr(copy_engine, "_account_ctx", lambda _login: {})
    body = {"agent": "pavlov", "site_type": "Мультибренд"}

    copy_engine._copy_enrich_body_context(body, "source", "target")

    assert body == {"agent": "pavlov", "site_type": "Мультибренд"}


def test_copy_agent_board_task_description_mentions_auto_retry():
    desc = agent_board_bridge._copy_job_task_description({
        "job_id": "copy123",
        "login": "target-login",
        "agency": "agency",
        "error": "boom",
        "body": {
            "source_login": "source-login",
            "target_login": "target-login",
            "campaign_ids": [1, 2, 3],
            "counter_id": 10,
            "goal_id": 20,
        },
        "result": {"cleanup": {"deleted": 1}},
    })

    assert "source-login" in desc
    assert "target-login" in desc
    assert "После `done` copy-service автоматически поставит повторную" in desc
    assert "direct-copy.service" in desc


def test_notify_copy_job_error_creates_agent_board_task(monkeypatch):
    row = {
        "job_id": "failed-copy",
        "login": "target-login",
        "agency": "agency",
        "status": "error",
        "kind": "copy_campaigns",
        "error": "boom",
        "agent_board_task_id": None,
        "body": {
            "source_login": "source-login",
            "target_login": "target-login",
            "campaign_ids": [11, 22],
            "created_by": "tester",
        },
        "result": {"phase": "upload"},
    }
    calls = {"task": None, "update": None}

    class FakeCursor:
        def __init__(self):
            self._select = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            if "SELECT * FROM public.direct_automation_jobs" in sql:
                self._select = True
            if "UPDATE public.direct_automation_jobs SET agent_board_task_id" in sql:
                calls["update"] = params

        def fetchone(self):
            return row if self._select else None

    class FakeConn:
        def cursor(self, *args, **kwargs):
            return FakeCursor()

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(agent_board_bridge, "ensure_copy_job_agent_columns", lambda _factory: None)
    monkeypatch.setattr(
        agent_board_bridge,
        "_create_agent_task",
        lambda title, desc, *, requested_by: calls.update(
            task=(title, desc, requested_by)
        ) or 123,
    )

    task_id = agent_board_bridge.notify_copy_job_error(lambda: FakeConn(), "failed-copy")

    assert task_id == 123
    assert calls["update"] == (123, "failed-copy")
    title, desc, requested_by = calls["task"]
    assert "source-login" in title
    assert "target-login" in title
    assert requested_by == "tester"
    assert "После `done` copy-service автоматически поставит повторную" in desc


def test_copy_jobs_ready_for_agent_retry_reads_done_tasks_separately(monkeypatch):
    rows = [
        {
            "job_id": "failed-copy-1",
            "login": "target-login",
            "kind": "copy_campaigns",
            "status": "error",
            "agent_board_task_id": 77,
            "copy_retry_job_id": None,
            "copy_retry_status": None,
            "body": {"target_login": "target-login"},
        },
        {
            "job_id": "failed-copy-2",
            "login": "target-login-2",
            "kind": "copy_campaigns",
            "status": "error",
            "agent_board_task_id": 88,
            "copy_retry_job_id": None,
            "copy_retry_status": None,
            "body": {"target_login": "target-login-2"},
        },
    ]
    executed = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            executed.append(sql)

        def fetchall(self):
            return rows

    class FakeConn:
        def cursor(self, *args, **kwargs):
            return FakeCursor()

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(agent_board_bridge, "ensure_copy_job_agent_columns", lambda _factory: None)
    monkeypatch.setattr(
        agent_board_bridge,
        "_agent_board_done_task_meta",
        lambda task_ids: {77: {"id": 77, "status": "done"}},
    )

    ready = agent_board_bridge.copy_jobs_ready_for_agent_retry(lambda: FakeConn(), limit=5)

    assert [r["job_id"] for r in ready] == ["failed-copy-1"]
    assert "JOIN agent_board.tasks" not in executed[0]
    assert "LEFT JOIN public.direct_automation_jobs r" in executed[0]


def test_mark_copy_retry_started_allows_replacing_interrupted_retry(monkeypatch):
    executed = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            executed.append((sql, params))

        def fetchone(self):
            return ("failed-copy",)

    class FakeConn:
        def cursor(self, *args, **kwargs):
            return FakeCursor()

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(agent_board_bridge, "ensure_copy_job_agent_columns", lambda _factory: None)

    ok = agent_board_bridge.mark_copy_retry_started(lambda: FakeConn(), "failed-copy", "retry-copy-2")

    assert ok is True
    sql, params = executed[0]
    assert "r.status = 'interrupted'" in sql
    assert params == ("retry-copy-2", "failed-copy")


def test_copy_retry_body_strips_old_job_markers_and_cleans_drafts():
    row = {
        "job_id": "failed1",
        "login": "target-login",
        "agent_board_task_id": 77,
        "total": 2,
        "body": {
            "_kind": "copy_campaigns",
            "_job_id": "failed1",
            "_web_posted": True,
            "_dedup_existing": True,
            "_session_snapshot": {"logged_in": True},
            "_copy_api_idempotency_key": "old-key",
            "_copy_api_payload_hash": "old-hash",
            "source_login": "source-login",
            "target_login": "target-login",
            "campaign_ids": [11, 22],
            "target_cleanup": "none",
        },
    }

    body = queue_server._copy_retry_body_from_failed(row)

    assert body["_kind"] == "copy_campaigns"
    assert body["login"] == "target-login"
    assert body["target_login"] == "target-login"
    assert body["created_by"] == "agent-board-auto"
    assert body["_copy_retry_of"] == "failed1"
    assert body["_copy_retry_agent_board_task_id"] == 77
    assert body["target_cleanup"] == "delete_drafts"
    assert body["campaign_ids"] == [11, 22]
    assert "_job_id" not in body
    assert "_web_posted" not in body
    assert "_session_snapshot" not in body
    assert "_copy_api_idempotency_key" not in body
    assert "_copy_api_payload_hash" not in body


def test_copy_uac_filter_list_remaps_source_auto_ru_fields_to_target_feed(monkeypatch):
    monkeypatch.setattr(
        create_set_feeds,
        "_feed_filter_fields",
        lambda _login, _feed_id: frozenset({"vendor", "model", "name"}),
    )
    monkeypatch.setattr(
        create_set_feeds,
        "_resolve_feed_field",
        lambda _login, _feed_id, semantic: {"brand": "vendor", "model": "model", "name": "name"}[semantic],
    )
    raw = [{
        "conditions": [
            {"fieldName": "mark_id", "operator": "NOT_CONTAINS", "values": ["uaz"]},
            {"field": "folder_id", "operator": "CONTAINS", "value": "[\"Tiggo\"]"},
        ]
    }]

    normalized = copy_uac._copy_uac_filter_list(raw, target_login="target", target_feed_id=123)

    assert normalized == [{
        "conditions": [
            {"field": "vendor", "operator": "NOT_CONTAINS", "values": ["uaz"], "value": "[\"uaz\"]"},
            {"field": "model", "operator": "CONTAINS", "values": ["Tiggo"], "value": "[\"Tiggo\"]"},
        ]
    }]


def test_copy_uac_campaign_href_preserves_quiz_path_on_target_domain():
    href = copy_uac._copy_uac_campaign_href(
        {"href": "https://geelycars-krd.site/quiz?utm=old#form"},
        fallback_href="https://geelybase-196.ru",
        source_domain="geelycars-krd.site",
        target_domain="geelybase-196.ru",
    )

    assert href == "https://geelybase-196.ru/quiz?utm=old#form"


def test_copy_uac_campaign_href_preserves_model_path_when_source_domain_unknown():
    href = copy_uac._copy_uac_campaign_href(
        {"href": "https://geelycars-krd.site/auto/geely/monjaro/i-rest/suv-5d"},
        fallback_href="https://geelybase-196.ru",
        source_domain="",
        target_domain="geelybase-196.ru",
    )

    assert href == "https://geelybase-196.ru/auto/geely/monjaro/i-rest/suv-5d"


def test_copy_uac_content_media_urls_uses_ordered_source_urls_only():
    row = {
        "contents": [
            {
                "type": "image",
                "source_url": "https://avatars.mds.yandex.net/get-direct/first/orig",
                "preview_url": "https://avatars.mds.yandex.net/get-direct/preview/first",
            },
            {
                "type": "text",
                "source_url": "https://avatars.mds.yandex.net/get-direct/not-image/orig",
            },
            {
                "type": "image",
                "source_url": "https://cdn.example.ru/car-2.jpg?size=full",
                "thumb": {"url": "https://cdn.example.ru/thumb-2.jpg"},
            },
            {
                "type": "image",
                "source_url": "https://cdn.example.ru/car-2.jpg?size=full",
            },
        ],
        "media": [{"url": "https://cdn.example.ru/recursive-wrong.jpg"}],
    }

    assert copy_uac._copy_uac_content_media_urls(row, want="image") == [
        "https://avatars.mds.yandex.net/get-direct/first/orig",
        "https://cdn.example.ru/car-2.jpg?size=full",
    ]


def test_copy_uac_extracts_source_content_ids_and_image_hashes_in_order():
    row = {
        "contents": [
            {"id": "c1", "type": "image", "direct_image_hash": "h1"},
            {"id": "c2", "type": "video", "direct_image_hash": ""},
            {"id": "c1", "type": "image", "direct_image_hash": "h1"},
            {"id": "c3", "type": "image", "image_hash": "h3"},
        ]
    }

    assert copy_uac._copy_uac_content_ids(row) == ["c1", "c3"]
    assert copy_uac._copy_uac_image_hashes(row) == ["h1", "h3"]


def test_copy_uac_target_images_use_urls_not_source_content_ids():
    row = {
        "contents": [
            {
                "id": "source-account-content-id",
                "type": "image",
                "direct_image_hash": "source-hash",
                "source_url": "https://avatars.mds.yandex.net/get-direct/source/orig",
            },
        ],
    }

    assert copy_uac._copy_uac_content_ids(row) == ["source-account-content-id"]
    assert copy_uac._copy_uac_target_image_urls(row) == [
        "https://avatars.mds.yandex.net/get-direct/source/orig",
    ]


def test_copy_uac_rejects_video_extension_content_id_without_type():
    row = {
        "contents": [
            {"id": "image-content-id", "type": "image", "direct_image_hash": "h1"},
            {
                "id": "video-content-id",
                "meta": {
                    "creative_id": 1163658513,
                    "vast": '<VAST><MediaFile type="video/mp4">https://cdn.example.ru/car.mp4</MediaFile></VAST>',
                },
            },
        ]
    }

    assert copy_uac._copy_uac_content_ids(row) == ["image-content-id"]
    assert copy_uac._copy_uac_image_hashes(row) == ["h1"]
    assert copy_uac._copy_uac_media_urls(row, want="video") == ["https://cdn.example.ru/car.mp4"]


def test_copy_uac_geo_strings_and_sitelinks_use_target_geo():
    pairs = [
        ("Краснодарский край", "Свердловская область"),
        ("Краснодаре", "Екатеринбурге"),
        ("Краснодар", "Екатеринбург"),
    ]

    assert copy_uac._copy_uac_geo_strings(["geely monjaro краснодар"], pairs) == [
        "geely monjaro екатеринбург",
    ]
    assert copy_uac._copy_uac_sitelinks(
        [{"title": "Дилер в Краснодаре", "description": "Краснодарский край", "href": "https://src.ru/quiz"}],
        source_domain="src.ru",
        target_domain="dst.ru",
        geo_pairs=pairs,
    ) == [{
        "title": "Дилер в Екатеринбурге",
        "description": "Свердловская область",
        "href": "https://dst.ru/quiz",
    }]


def test_copy_uac_geo_strings_extract_phrase_keyword_dicts():
    pairs = [("Новосибирска", "Саратова"), ("Новосибирск", "Саратов")]
    detail = {"keywords": [{"phrase": "купить авто из Новосибирска"}]}

    keywords = copy_uac._copy_uac_geo_strings(
        copy_uac._copy_uac_strings(detail, "keywords", limit=20),
        pairs,
    )

    assert keywords == ["купить авто из Саратова"]
    assert copy_uac._copy_uac_geo_guard("tp6_r0076", keywords, pairs, "r0076") == []


def test_copy_uac_geo_guard_does_not_match_city_form_inside_region_adjective():
    pairs = [("Новосибирска", "Саратова"), ("Новосибирск", "Саратов")]

    assert copy_uac._copy_uac_geo_guard(
        "tp6_cpc_site_ct0014_aon_n000_r0076_ct001_ag011_g00",
        ["ключи Новосибирская область"],
        pairs,
        "r0076",
    ) == []


def test_copy_uac_geo_guard_rejects_source_region_residuals():
    pairs = [("Краснодарский край", "Свердловская область"), ("Краснодар", "Екатеринбург")]

    errors = copy_uac._copy_uac_geo_guard(
        "tp6_cpc_site_ct0097_aon_n000_r0088_ct001_ag011_g00",
        ["geely monjaro краснодар"],
        pairs,
        "r0121",
    )

    assert "r0088" in errors[0]
    assert "Краснодар" in errors[1]


def test_copy_uac_geo_guard_accepts_target_region_and_r_code():
    pairs = [("Краснодарский край", "Свердловская область"), ("Краснодар", "Екатеринбург")]
    name = "tp6_cpc_site_ct0097_aon_n000_r0121_ct001_ag011_g00"

    assert copy_uac._copy_uac_geo_guard(name, ["geely monjaro Екатеринбург"], pairs, "r0121") == []


def test_copy_uac_create_live_guard_accepts_persisted_target_spec():
    spec = SimpleNamespace(
        display_name="tp6_r0121",
        href="https://target.ru/quiz",
        titles=["Title"],
        texts=["Text"],
        keywords=["geely екатеринбург"],
        content_ids=["content-1"],
    )

    assert copy_uac._copy_uac_create_live_guard(
        {"contents": [{"id": "content-1", "type": "image", "direct_image_hash": "hash-1"}]},
        {
            "display_name": "tp6_r0121",
            "href": "https://target.ru/quiz",
            "titles": ["Title"],
            "texts": ["Text"],
            "keywords": ["geely екатеринбург"],
            "contents": [{"id": "content-1", "type": "image", "direct_image_hash": "hash-1"}],
        },
        spec,
    ) == []


def test_copy_uac_create_live_guard_rejects_persisted_mismatch():
    spec = SimpleNamespace(
        display_name="tp6_r0121",
        href="https://target.ru",
        titles=["Title"],
        texts=["Text"],
        keywords=["geely екатеринбург"],
        content_ids=["content-1"],
    )

    errors = copy_uac._copy_uac_create_live_guard(
        {"contents": [{"id": "content-1", "type": "image", "direct_image_hash": "hash-1"}]},
        {
            "display_name": "tp6_r0088",
            "href": "https://target.ru/auto",
            "titles": ["Title"],
            "texts": ["Text"],
            "keywords": ["geely екатеринбург"],
            "contents": [{"id": "content-1", "type": "image", "direct_image_hash": "hash-2"}],
        },
        spec,
    )

    assert "display_name mismatch" in errors[0]
    assert "href mismatch" in errors[1]
    assert "image_hashes mismatch" in errors[2]


def test_uac_client_uploads_video_urls_when_image_content_ids_are_preseeded(monkeypatch):
    from direct.clients import uac_client

    spec = uac_client.MasterCampaignSpec(
        href="https://example.ru",
        titles=["Title"],
        texts=["Text"],
        region_ids=[172],
        counter_id=1,
        goal_id=2,
        cpa=100,
        week_budget=1000,
        content_ids=["image-content-id"],
        video_urls=["https://cdn.example.ru/video.mp4"],
    )
    client = object.__new__(uac_client.UacClient)
    calls = []

    client.link_info = lambda href: calls.append(("link", href))
    client.upload_content = lambda url, typ, adv_type: calls.append((url, typ, adv_type)) or "video-content-id"
    client.upload_video_file = lambda path, adv_type: "unused"
    client.build_payload = lambda _spec, ids: {"content_ids": ids}
    client.create_campaign = lambda payload: calls.append(("create", payload)) or "123"
    client.launch_campaign = lambda cid: None
    monkeypatch.setattr(uac_client.time, "sleep", lambda _seconds: None)

    cid = client.create_master_campaign(spec, launch=False)

    assert cid == "123"
    assert ("https://cdn.example.ru/video.mp4", "video", "text") in calls
    assert ("create", {"content_ids": ["image-content-id", "video-content-id"]}) in calls


def test_parse_feed_map_keeps_numeric_mapping_only():
    assert parse_feed_map({"feed_map": {"011": "222", "bad": "333", "44": "0", "55": 666}}) == {
        "11": 222,
        "55": 666,
    }


def test_parse_image_hashes_requires_list_and_strips_values():
    assert parse_image_hashes({"image_hashes": "abc"}) == []
    assert parse_image_hashes({"image_hashes": [" abc ", "def"]}) == ["abc", "def"]
    assert parse_image_hashes({"image_hashes": ["abc", "", None, 123]}) == []


def test_copy_api_result_summary_counts_current_verify_results():
    summary = copy_api._copy_api_result_summary({
        "ok": True,
        "results": [{"ok": True}],
        "copy_verify": {
            "ok": False,
            "summary": {"total": 1},
            "results": [{"dimension": "ads", "ok": False}],
        },
    })

    assert summary["verification"]["diff_count"] == 1


def test_copy_verify_source_accepts_direct_items_dict_for_minus_places(tmp_path):
    (tmp_path / "campaigns.json").write_text(
        '[{"Id": 11, "ExcludedSites": {"Items": ["a.ru", ""]}, '
        '"ExcludedSitesForVideoAds": ["b.ru"]}]',
        encoding="utf-8",
    )
    for name in [
        "adgroups.json",
        "ads.json",
        "shopping_ads.json",
        "keywords.json",
        "bidmodifiers.json",
        "adimages.json",
    ]:
        (tmp_path / name).write_text("[]", encoding="utf-8")

    profile = build_source_profile(tmp_path)

    assert profile["11"]["minus_places"] == ["a.ru", "b.ru"]


def test_copy_verify_source_falls_back_to_text_ad_when_grid_row_has_no_payload(tmp_path):
    (tmp_path / "campaigns.json").write_text('[{"Id": 11}]', encoding="utf-8")
    (tmp_path / "adgroups.json").write_text('[{"Id": 101, "CampaignId": 11}]', encoding="utf-8")
    (tmp_path / "ads.json").write_text(
        '[{"Id": 1001, "CampaignId": 11, "AdGroupId": 101, "Type": "TEXT_AD", '
        '"TextAd": {"Title": "Title", "Text": "Text", "AdImageHash": "hash"}}]',
        encoding="utf-8",
    )
    for name in [
        "shopping_ads.json",
        "keywords.json",
        "bidmodifiers.json",
        "adimages.json",
    ]:
        (tmp_path / name).write_text("[]", encoding="utf-8")

    profile = build_source_profile(tmp_path, grid_snapshot={1001: {"id": 1001, "campaignId": 11}})

    assert profile["11"]["ads_with_titles"] == 1
    assert profile["11"]["ads_with_texts"] == 1
    assert profile["11"]["ads_with_images"] == 1


def _weekly_clicks_edit_row(avg_bid=None):
    return {
        "id": 712903434,
        "name": "tp3 weekly clicks",
        "primaryStatus": "DRAFT",
        "strategy": {
            "strategyType": "OPTIMIZE_CLICKS",
            "avgBid": avg_bid,
            "clicksLimit": None,
            "budget": {"sum": 300, "period": "WEEK"},
            "platforms": {"gallery": True, "search": True, "organic": True},
        },
    }


def test_grid_weekly_clicks_uses_har_avg_click_strategy_with_existing_avg_bid():
    payload = grid_finalize.GridClient._unified_campaign_update_from_edit_row(
        _weekly_clicks_edit_row(avg_bid=123)
    )
    strategy = payload["biddingStategyWithPlatforms"]

    assert "_unsupported_strategy" not in payload
    assert strategy["strategyName"] == "AUTOBUDGET_AVG_CLICK"
    assert strategy["strategyData"]["avgBid"] == "123"
    assert strategy["strategyData"]["sum"] == "300"
    assert strategy["strategyData"]["budgetType"] == "WEEKLY"


def test_grid_weekly_clicks_uses_har_ui_default_avg_bid_when_grid_row_is_empty():
    payload = grid_finalize.GridClient._unified_campaign_update_from_edit_row(
        _weekly_clicks_edit_row(avg_bid=None)
    )
    strategy = payload["biddingStategyWithPlatforms"]

    assert "_unsupported_strategy" not in payload
    assert strategy["strategyName"] == "AUTOBUDGET_AVG_CLICK"
    assert strategy["strategyData"]["avgBid"] == "100"
    assert strategy["strategyData"]["sum"] == "300"
    assert strategy["strategyData"]["budgetType"] == "WEEKLY"


def test_grid_weekly_clicks_without_budget_stays_unsupported():
    row = _weekly_clicks_edit_row(avg_bid=None)
    row["strategy"]["budget"] = {"sum": 0, "period": "WEEK"}

    payload = grid_finalize.GridClient._unified_campaign_update_from_edit_row(row)

    assert payload["_unsupported_strategy"] == "Максимум кликов (без лимита кликов/avgBid/бюджета)"


def test_grid_default_strategy_uses_write_enum_without_skip():
    row = _weekly_clicks_edit_row()
    row["strategy"] = {
        "strategyType": "DEFAULT",
        "budget": {"sum": 20000, "period": "WEEK"},
        "platforms": {"search": True},
    }

    payload = grid_finalize.GridClient._unified_campaign_update_from_edit_row(row)

    assert "_unsupported_strategy" not in payload
    assert payload["biddingStategyWithPlatforms"]["strategyName"] == "DEFAULT_"


def test_grid_multiple_cpa_strategy_uses_write_enum_without_skip():
    row = _weekly_clicks_edit_row()
    row["strategy"] = {
        "strategyType": "MULTIPLE_CPA",
        "budget": {"sum": 30000, "period": "WEEK"},
        "platforms": {"search": True},
    }

    payload = grid_finalize.GridClient._unified_campaign_update_from_edit_row(row)

    assert "_unsupported_strategy" not in payload
    assert payload["biddingStategyWithPlatforms"]["strategyName"] == "AUTOBUDGET_MULTIPLE_CPA"


def test_grid_campaign_rename_uses_idempotent_post(monkeypatch):
    client = grid_finalize.GridClient.__new__(grid_finalize.GridClient)
    client.login = "login"
    called = {}

    monkeypatch.setattr(client, "_bootstrap_csrf", lambda: None)
    monkeypatch.setattr(client, "_read_broad_match_map", lambda ids: {ids[0]: True})
    monkeypatch.setattr(client, "_narrow_campaign_base", lambda cid, bm: {"id": str(cid)})

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"data": {"updateCampaigns": {
                "updatedCampaigns": [{"id": "123"}],
                "validationResult": {},
            }}}

    def fake_post(op, query, variables):
        called["op"] = op
        called["variables"] = variables
        return Resp()

    monkeypatch.setattr(client, "post_idempotent", fake_post)
    monkeypatch.setattr(client, "_post", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("_post must not be used")))

    updated = client.set_campaign_names({123: "new name"})

    assert updated == [{"id": "123"}]
    assert called["op"] == "UpdateCampaigns"
    assert called["variables"]["input"]["campaignUpdateItems"][0]["unifiedCampaign"]["name"] == "new name"


def test_grid_adgroup_rename_update_uses_idempotent_post(monkeypatch):
    client = grid_finalize.GridClient.__new__(grid_finalize.GridClient)
    client.login = "login"
    called = {}

    monkeypatch.setattr(client, "_bootstrap_csrf", lambda: None)

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"data": {"updateUnifiedAdGroups": {
                "updatedAdGroupItems": [{"adGroupId": "456"}],
                "validationResult": {},
            }}}

    def fake_post(op, query, variables):
        called["op"] = op
        called["variables"] = variables
        return Resp()

    monkeypatch.setattr(client, "post_idempotent", fake_post)

    updated = client.update_unified_adgroups([{"adGroupId": "456", "adGroupName": "new group"}])

    assert updated == [456]
    assert called["op"] == "UpdateUnifiedAdGroups"
    assert called["variables"]["unifiedUpdateInput"][0]["adGroupName"] == "new group"


def test_grid_post_reauths_on_html_login_page():
    client = grid_finalize.GridClient.__new__(grid_finalize.GridClient)
    client.login = "login"
    client.cookie = "old-cookie"
    client.csrf = "old-csrf"
    client._reauth = lambda: setattr(client, "cookie", "new-cookie")
    posts = []

    class Resp:
        def __init__(self, text, content_type):
            self.status_code = 200
            self.text = text
            self.headers = {"Content-Type": content_type}
            self.cookies = {}

    class FakeSession:
        def post(self, url, json, headers, timeout):
            posts.append({"url": url, "headers": dict(headers), "json": json, "timeout": timeout})
            if len(posts) == 1:
                return Resp("<html><head><title>Log in</title></head></html>", "text/html")
            return Resp('{"data":{}}', "application/json")

    client.sess = FakeSession()

    resp = client._post("Operation", "query", {"x": 1})

    assert resp.text == '{"data":{}}'
    assert len(posts) == 2
    assert posts[0]["headers"]["Cookie"] == "old-cookie"
    assert posts[1]["headers"]["Cookie"] == "new-cookie"


def test_grid_post_reauth_login_page_does_not_recurse():
    client = grid_finalize.GridClient.__new__(grid_finalize.GridClient)
    client.login = "login"
    client.cookie = "old-cookie"
    client.csrf = "old-csrf"
    client._explicit_cookie = True
    client._reauth_depth = 0
    posts = []

    class Resp:
        status_code = 200
        text = "<html><head><title>Log in</title></head></html>"
        headers = {"Content-Type": "text/html"}
        cookies = {}

    class FakeSession:
        def post(self, url, json, headers, timeout):
            posts.append({"url": url, "headers": dict(headers), "json": json, "timeout": timeout})
            return Resp()

    client.sess = FakeSession()

    try:
        client._post("Operation", "query", {"x": 1})
    except RuntimeError as exc:
        assert "не получил CSRF" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert len(posts) == 2


def test_grid_read_post_force_refreshes_on_html_login_page(monkeypatch):
    client = grid_read.GridReadClient.__new__(grid_read.GridReadClient)
    client.login = "login"
    client.cookie = "old-cookie"
    client.csrf = None
    posts = []

    monkeypatch.setattr(grid_read.cmc, "pick_working_cookie", lambda login, force_refresh=False: "new-cookie")

    class Resp:
        def __init__(self, text, content_type):
            self.status_code = 200
            self.text = text
            self.headers = {"Content-Type": content_type}
            self.cookies = {}

        def json(self):
            return {"data": {"ok": True}}

    class FakeSession:
        def post(self, url, json, headers, timeout):
            posts.append({"headers": dict(headers), "json": json, "timeout": timeout})
            if len(posts) == 1:
                return Resp("<html><head><title>Log in</title></head></html>", "text/html")
            return Resp('{"data":{"ok":true}}', "application/json")

    client.sess = FakeSession()

    data = client._post("Operation", "query", {"x": 1})

    assert data == {"data": {"ok": True}}
    assert len(posts) == 2
    assert posts[0]["headers"]["Cookie"] == "old-cookie"
    assert posts[1]["headers"]["Cookie"] == "new-cookie"


def test_run_rename_falls_back_to_single_campaign_updates_after_bulk_grid_error():
    calls = []

    class FakeGrid:
        def set_campaign_names(self, names):
            calls.append(dict(names))
            if len(names) > 1:
                raise grid_finalize.GridFinalizeError("Grid set-names: internal")
            return [{"id": str(next(iter(names)))}]

    result = content_renames_routes.run_rename(
        "login",
        {"campaign_renames": {"1": "one", "2": "two"}},
        grid_client_factory=lambda _login: FakeGrid(),
    )

    assert result == {"replaced": 2, "errors": []}
    assert calls == [{1: "one", 2: "two"}, {1: "one"}, {2: "two"}]


def test_run_rename_uses_v5_fallback_when_grid_campaign_update_fails():
    v5_calls = []

    class FakeGrid:
        def set_campaign_names(self, _names):
            raise grid_finalize.GridFinalizeError("Grid set-names: internal")

    def fake_v5_call(service, method, token, login, params):
        v5_calls.append((service, method, token, login, params))
        return {"result": {"UpdateResults": [{"Id": 1}]}}

    result = content_renames_routes.run_rename(
        "login",
        {"campaign_renames": {"1": "one"}},
        grid_client_factory=lambda _login: FakeGrid(),
        token="token",
        v5_call=fake_v5_call,
    )

    assert result == {"replaced": 1, "errors": []}
    assert v5_calls == [
        ("campaigns", "update", "token", "login", {"Campaigns": [{"Id": 1, "Name": "one"}]})
    ]


def test_run_rename_campaigns_uses_v5_before_grid_when_token_available():
    v5_calls = []

    class FakeGrid:
        def set_campaign_names(self, _names):
            raise AssertionError("campaign rename with token must use v5 first")

    def fake_v5_call(service, method, token, login, params):
        v5_calls.append((service, method, token, login, params))
        return {"result": {"UpdateResults": [{"Id": params["Campaigns"][0]["Id"]}]}}

    result = content_renames_routes.run_rename(
        "login",
        {"campaign_renames": {"1": "one", "2": "two"}},
        grid_client_factory=lambda _login: FakeGrid(),
        token="token",
        v5_call=fake_v5_call,
    )

    assert result == {"replaced": 2, "errors": []}
    assert v5_calls == [
        ("campaigns", "update", "token", "login", {"Campaigns": [{"Id": 1, "Name": "one"}]}),
        ("campaigns", "update", "token", "login", {"Campaigns": [{"Id": 2, "Name": "two"}]}),
    ]


def test_public_copy_api_rejects_upload_image_mode_without_hashes(monkeypatch):
    app = Flask(__name__)
    bp = Blueprint("copy_api_test", __name__, url_prefix="/api/v1/copy")
    called = {"ensure": False}

    def fake_get(key, default=""):
        if key == "COPY_API_KEY":
            return "secret"
        if key == "COPY_API_CORS_ORIGINS":
            return ""
        return default

    def ensure_worker(_app):
        called["ensure"] = True

    monkeypatch.setattr(copy_api, "_get", fake_get)
    register_copy_api(
        bp,
        ensure_create_worker=ensure_worker,
        job_new=lambda *_args, **_kwargs: "job",
        copy_job_upsert=lambda *_args, **_kwargs: None,
        create_jobs_ahead=lambda *_args, **_kwargs: 0,
        create_jobs={},
        create_jobs_lock=type("Lock", (), {"__enter__": lambda self: self, "__exit__": lambda self, *args: None})(),
        copy_jobs={},
        copy_jobs_lock=type("Lock", (), {"__enter__": lambda self: self, "__exit__": lambda self, *args: None})(),
        resolve_agency_hint=lambda *_args, **_kwargs: "",
        copy_default_feed_path="/feed.xml",
        counter_foreign_owner=lambda *_args, **_kwargs: None,
        geo_validate_id_func=lambda _id: True,
    )
    app.register_blueprint(bp)

    response = app.test_client().post(
        "/api/v1/copy/start",
        headers={"X-API-Key": "secret"},
        json={
            "source_login": "porg-src",
            "target_login": "porg-tgt",
            "campaign_ids": [1],
            "target_domain": "example.ru",
            "counter_id": 123,
            "goal_id": 456,
            "mode": "other",
            "image_mode": "upload",
        },
    )

    assert response.status_code == 400
    assert "image_mode='upload'" in response.get_json()["error"]
    assert called["ensure"] is False


def test_public_copy_api_rejects_mixed_upload_image_hashes(monkeypatch):
    app = Flask(__name__)
    bp = Blueprint("copy_api_mixed_upload_test", __name__, url_prefix="/api/v1/copy")
    called = {"ensure": False}

    def fake_get(key, default=""):
        if key == "COPY_API_KEY":
            return "secret"
        if key == "COPY_API_CORS_ORIGINS":
            return ""
        return default

    def ensure_worker(_app):
        called["ensure"] = True

    monkeypatch.setattr(copy_api, "_get", fake_get)
    register_copy_api(
        bp,
        ensure_create_worker=ensure_worker,
        job_new=lambda *_args, **_kwargs: "job",
        copy_job_upsert=lambda *_args, **_kwargs: None,
        create_jobs_ahead=lambda *_args, **_kwargs: 0,
        create_jobs={},
        create_jobs_lock=type("Lock", (), {"__enter__": lambda self: self, "__exit__": lambda self, *args: None})(),
        copy_jobs={},
        copy_jobs_lock=type("Lock", (), {"__enter__": lambda self: self, "__exit__": lambda self, *args: None})(),
        resolve_agency_hint=lambda *_args, **_kwargs: "",
        copy_default_feed_path="/feed.xml",
        counter_foreign_owner=lambda *_args, **_kwargs: None,
        geo_validate_id_func=lambda _id: True,
    )
    app.register_blueprint(bp)

    response = app.test_client().post(
        "/api/v1/copy/start",
        headers={"X-API-Key": "secret"},
        json={
            "source_login": "porg-src",
            "target_login": "porg-tgt",
            "campaign_ids": [1],
            "target_domain": "example.ru",
            "counter_id": 123,
            "goal_id": 456,
            "mode": "other",
            "image_mode": "upload",
            "image_hashes": ["hash-1", 7],
        },
    )

    assert response.status_code == 400
    assert "image_hashes как список" in response.get_json()["error"]
    assert called["ensure"] is False


def test_public_copy_api_normalizes_upload_image_hashes(monkeypatch):
    app = Flask(__name__)
    bp = Blueprint("copy_api_upload_test", __name__, url_prefix="/api/v1/copy")
    captured = {}

    def fake_get(key, default=""):
        if key == "COPY_API_KEY":
            return "secret"
        if key == "COPY_API_CORS_ORIGINS":
            return ""
        return default

    def job_new(total, login, body, _saved_session, **_kwargs):
        captured["total"] = total
        captured["login"] = login
        captured["body"] = body
        return "job-1"

    monkeypatch.setattr(copy_api, "_get", fake_get)
    register_copy_api(
        bp,
        ensure_create_worker=lambda _app: None,
        job_new=job_new,
        copy_job_upsert=lambda *_args, **_kwargs: None,
        create_jobs_ahead=lambda *_args, **_kwargs: 0,
        create_jobs={},
        create_jobs_lock=type("Lock", (), {"__enter__": lambda self: self, "__exit__": lambda self, *args: None})(),
        copy_jobs={},
        copy_jobs_lock=type("Lock", (), {"__enter__": lambda self: self, "__exit__": lambda self, *args: None})(),
        resolve_agency_hint=lambda *_args, **_kwargs: "",
        copy_default_feed_path="/feed.xml",
        counter_foreign_owner=lambda *_args, **_kwargs: None,
        geo_validate_id_func=lambda _id: True,
    )
    app.register_blueprint(bp)

    response = app.test_client().post(
        "/api/v1/copy/start",
        headers={"X-API-Key": "secret"},
        json={
            "source_login": "porg-src",
            "target_login": "porg-tgt",
            "campaign_ids": [1],
            "target_domain": "example.ru",
            "counter_id": 123,
            "goal_id": 456,
            "mode": "other",
            "geo_mode": "keep",
            "image_mode": "upload",
            "image_hashes": [" hash-1 "],
        },
    )

    assert response.status_code == 200
    assert captured["total"] == 1
    assert captured["login"] == "porg-tgt"
    assert captured["body"]["_kind"] == "copy_campaigns"
    assert captured["body"]["image_hashes"] == ["hash-1"]


def test_public_copy_api_normalizes_feed_map_before_queue(monkeypatch):
    app = Flask(__name__)
    bp = Blueprint("copy_api_feed_map_test", __name__, url_prefix="/api/v1/copy")
    captured = {}

    def fake_get(key, default=""):
        if key == "COPY_API_KEY":
            return "secret"
        if key == "COPY_API_CORS_ORIGINS":
            return ""
        return default

    def job_new(total, login, body, _saved_session, **_kwargs):
        captured["total"] = total
        captured["login"] = login
        captured["body"] = body
        return "job-1"

    monkeypatch.setattr(copy_api, "_get", fake_get)
    register_copy_api(
        bp,
        ensure_create_worker=lambda _app: None,
        job_new=job_new,
        copy_job_upsert=lambda *_args, **_kwargs: None,
        create_jobs_ahead=lambda *_args, **_kwargs: 0,
        create_jobs={},
        create_jobs_lock=type("Lock", (), {"__enter__": lambda self: self, "__exit__": lambda self, *args: None})(),
        copy_jobs={},
        copy_jobs_lock=type("Lock", (), {"__enter__": lambda self: self, "__exit__": lambda self, *args: None})(),
        resolve_agency_hint=lambda *_args, **_kwargs: "",
        copy_default_feed_path="/feed.xml",
        counter_foreign_owner=lambda *_args, **_kwargs: None,
        geo_validate_id_func=lambda _id: True,
    )
    app.register_blueprint(bp)

    response = app.test_client().post(
        "/api/v1/copy/start",
        headers={"X-API-Key": "secret"},
        json={
            "source_login": "porg-src",
            "target_login": "porg-tgt",
            "campaign_ids": [1],
            "target_domain": "example.ru",
            "counter_id": 123,
            "goal_id": 456,
            "mode": "other",
            "feed_map": {"011": "222", "bad": "333", "44": "0", "55": 666},
        },
    )

    assert response.status_code == 200
    assert captured["body"]["feed_map"] == {"11": 222, "55": 666}


def test_public_copy_api_drops_geo_region_ids_when_geo_mode_keep(monkeypatch):
    app = Flask(__name__)
    bp = Blueprint("copy_api_geo_keep_test", __name__, url_prefix="/api/v1/copy")
    captured = {}

    def fake_get(key, default=""):
        if key == "COPY_API_KEY":
            return "secret"
        if key == "COPY_API_CORS_ORIGINS":
            return ""
        return default

    def job_new(total, login, body, _saved_session, **_kwargs):
        captured["body"] = body
        return "job-1"

    monkeypatch.setattr(copy_api, "_get", fake_get)
    register_copy_api(
        bp,
        ensure_create_worker=lambda _app: None,
        job_new=job_new,
        copy_job_upsert=lambda *_args, **_kwargs: None,
        create_jobs_ahead=lambda *_args, **_kwargs: 0,
        create_jobs={},
        create_jobs_lock=type("Lock", (), {"__enter__": lambda self: self, "__exit__": lambda self, *args: None})(),
        copy_jobs={},
        copy_jobs_lock=type("Lock", (), {"__enter__": lambda self: self, "__exit__": lambda self, *args: None})(),
        resolve_agency_hint=lambda *_args, **_kwargs: "",
        copy_default_feed_path="/feed.xml",
        counter_foreign_owner=lambda *_args, **_kwargs: None,
        geo_validate_id_func=lambda _id: True,
    )
    app.register_blueprint(bp)

    response = app.test_client().post(
        "/api/v1/copy/start",
        headers={"X-API-Key": "secret"},
        json={
            "source_login": "porg-src",
            "target_login": "porg-tgt",
            "campaign_ids": [1],
            "target_domain": "example.ru",
            "counter_id": 123,
            "goal_id": 456,
            "mode": "other",
            "geo_mode": "keep",
            "geo_region_ids": ["bad", "213"],
        },
    )

    assert response.status_code == 200
    assert captured["body"]["geo_mode"] == "keep"
    assert "geo_region_ids" not in captured["body"]


def test_public_copy_api_rejects_mixed_geo_region_ids_for_change(monkeypatch):
    app = Flask(__name__)
    bp = Blueprint("copy_api_geo_mixed_test", __name__, url_prefix="/api/v1/copy")
    called = {"ensure": False}

    def fake_get(key, default=""):
        if key == "COPY_API_KEY":
            return "secret"
        if key == "COPY_API_CORS_ORIGINS":
            return ""
        return default

    def ensure_worker(_app):
        called["ensure"] = True

    monkeypatch.setattr(copy_api, "_get", fake_get)
    register_copy_api(
        bp,
        ensure_create_worker=ensure_worker,
        job_new=lambda *_args, **_kwargs: "job",
        copy_job_upsert=lambda *_args, **_kwargs: None,
        create_jobs_ahead=lambda *_args, **_kwargs: 0,
        create_jobs={},
        create_jobs_lock=type("Lock", (), {"__enter__": lambda self: self, "__exit__": lambda self, *args: None})(),
        copy_jobs={},
        copy_jobs_lock=type("Lock", (), {"__enter__": lambda self: self, "__exit__": lambda self, *args: None})(),
        resolve_agency_hint=lambda *_args, **_kwargs: "",
        copy_default_feed_path="/feed.xml",
        counter_foreign_owner=lambda *_args, **_kwargs: None,
        geo_validate_id_func=lambda _id: True,
    )
    app.register_blueprint(bp)

    response = app.test_client().post(
        "/api/v1/copy/start",
        headers={"X-API-Key": "secret"},
        json={
            "source_login": "porg-src",
            "target_login": "porg-tgt",
            "campaign_ids": [1],
            "target_domain": "example.ru",
            "counter_id": 123,
            "goal_id": 456,
            "mode": "other",
            "geo_mode": "change",
            "geo_region_ids": ["213", "bad"],
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error_code"] == "INVALID_GEO"
    assert "geo_region_ids[1]" in response.get_json()["error"]
    assert called["ensure"] is False

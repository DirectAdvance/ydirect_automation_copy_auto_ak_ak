from pathlib import Path
from types import SimpleNamespace
import ast
import inspect
import time

from flask import Blueprint, Flask

from direct import agent_board_bridge, uac_verifier
from direct.core import queue_server
from direct.clients import grid_read, grid_finalize
from direct.create import create_set_feeds
from direct.content import content_renames_routes
from direct.copy_service import (
    copy_api,
    copy_asset_steps,
    copy_cleanup,
    copy_engine,
    copy_feeds,
    copy_postprocess,
    copy_price_steps,
    copy_settings_steps,
    copy_steps,
    copy_grid_read,
    copy_snapshot,
    copy_grid_unified,
    copy_uac,
)
from direct.copy_service.copy_api import register_copy_api
from direct.copy_service.copy_request import parse_feed_map, parse_image_hashes
from direct.copy_service.copy_verify_source import build_source_profile
from direct.copy_service.copy_verify_diff import diff_profiles
from direct.copy_service.copy_verify_target import build_target_profile
from direct.copy_service import copy_verify_state


def test_copy_timed_heartbeats_while_waiting(monkeypatch):
    upserts = []
    logs = []

    monkeypatch.setattr(copy_postprocess, "_COPY_TIMED_HEARTBEAT_SEC", 0.01)
    monkeypatch.setattr(
        copy_postprocess,
        "_engine",
        lambda: SimpleNamespace(
            _copy_job_log=lambda _job_id, msg: logs.append(msg),
            _copy_job_upsert=lambda _job_id, **fields: upserts.append(fields) or {},
        ),
    )

    result = copy_postprocess._copy_timed(
        "copy-job",
        "slow-grid-step",
        lambda: time.sleep(0.04) or {"ok": True},
        timeout_sec=1,
    )

    assert result == {"ok": True}
    assert len(upserts) >= 2
    assert all(fields == {} for fields in upserts)
    assert logs[0] == "[timing] slow-grid-step: start"
    assert logs[-1].startswith("[timing] slow-grid-step:")


def test_copy_target_feed_id_prefers_preseeded_id_maps(tmp_path, monkeypatch):
    (tmp_path / "id_maps.json").write_text('{"feeds":{"11":12345}}', encoding="utf-8")
    monkeypatch.setattr(copy_feeds, "_grid_feeds", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(copy_feeds, "_filter_allowed_feed_rows", lambda rows: rows)
    monkeypatch.setattr(copy_feeds, "_feed_key", lambda value: value)

    assert copy_feeds._copy_target_feed_id("target", "agency", Path(tmp_path), "example.ru") == 12345


def test_copy_target_feed_id_falls_back_to_existing_listing_feed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        copy_feeds,
        "_grid_feeds",
        lambda *_args, **_kwargs: [
            {
                "id": "3595536",
                "name": "buauto196.site — yandex-used-auto",
                "url": "https://buauto196.site/yandex-used-auto.xml",
                "listings": [{"id": "mark_28", "name": "Haval с пробегом"}],
            },
        ],
    )
    monkeypatch.setattr(copy_feeds, "_filter_allowed_feed_rows", lambda _rows: [])
    monkeypatch.setattr(copy_feeds, "_feed_key", lambda value: str(value or "").lower())

    assert copy_feeds._copy_target_feed_id("target", "agency", Path(tmp_path), "buauto196.site") == 3595536


def test_copy_uac_product_uses_single_feed_map_target_when_detail_has_no_feed():
    assert copy_uac._copy_uac_effective_feed_id(
        None,
        None,
        {"3568864": 3595536, "3568865": 3595536},
        is_product=True,
    ) == 3595536


def test_copy_uac_product_does_not_guess_ambiguous_feed_map_target():
    assert copy_uac._copy_uac_effective_feed_id(
        None,
        None,
        {"3568864": 3595536, "3568865": 3595537},
        is_product=True,
    ) is None


def test_copy_target_feed_blocker_for_uac_reports_empty_target_account(monkeypatch):
    monkeypatch.setattr(copy_engine, "_grid_feeds", lambda _login, _agency: [])

    msg = copy_engine._copy_target_feed_blocker_for_uac(
        "porg-qxmt4z2y",
        "y-direct-victory",
        [{"name": "tp7_cpa_site_ct0000"}],
    )

    assert "на аккаунте porg-qxmt4z2y нет фидов" in msg


def test_copy_target_feed_blocker_for_uac_ignores_non_product_uac(monkeypatch):
    monkeypatch.setattr(copy_engine, "_grid_feeds", lambda _login, _agency: [])

    assert copy_engine._copy_target_feed_blocker_for_uac(
        "target",
        "agency",
        [{"name": "tp6_cpa_site_ct0000"}],
    ) == ""


def test_copy_missing_target_feeds_error_does_not_create_agent_task():
    row = {
        "error": "на аккаунте porg-qxmt4z2y нет фидов: товарные tp7/UAC-кампании нельзя скопировать",
        "result": {},
    }

    assert agent_board_bridge._copy_error_is_user_blocker(row) is True


def test_copy_unreachable_source_login_does_not_create_agent_task():
    """Недоступный логин источника кодом не чинится → терминальная ошибка очереди, не борд-задача."""
    bruteforce = {
        "error": ("источник porg-5ri2mjj: ни токен, ни перебор агентских кук "
                  "(victoryagency-direct1618440, victorylotsofads1) не дал доступа"),
        "result": {},
    }
    engine = {
        "error": "",
        "result": {"error": "!! Ни один токен/кука не дал доступ к 'porg-5ri2mjj'."},
    }

    assert agent_board_bridge._copy_error_is_user_blocker(bruteforce) is True
    assert agent_board_bridge._copy_error_is_user_blocker(engine) is True
    # Реальные баги копирования по-прежнему уходят на Agent Board.
    assert agent_board_bridge._copy_error_is_user_blocker(
        {"error": "verification gate: 10 незакрытых дефектов", "result": {}}
    ) is False


def test_copy_terminal_status_is_error_when_campaign_failed():
    status, error = copy_engine._copy_terminal_status_from_results([
        {"ok": False, "name": "tp7 product", "error": "нет feed_id"},
    ])

    assert status == "error"
    assert "tp7 product: нет feed_id" in error


def test_copy_terminal_status_includes_postprocess_errors():
    status, error = copy_engine._copy_terminal_status_from_postprocess(
        [{"ok": True, "name": "campaign"}],
        {"errors": ["verification gate: 1 незакрытых дефектов"]},
    )

    assert status == "error"
    assert "verification gate" in error


def test_copy_uac_only_without_v5_snapshot_skips_cookie_postprocess():
    assert copy_engine._copy_is_uac_only_without_v5_snapshot(
        {"campaigns": 0},
        [{"id": "712042120"}, {"id": "712098943"}],
        {712042120, 712098943},
    ) is True


def test_copy_uac_mixed_with_v5_snapshot_keeps_cookie_postprocess():
    assert copy_engine._copy_is_uac_only_without_v5_snapshot(
        {"campaigns": 1},
        [{"id": "712042120"}],
        {712042120, 712215402},
    ) is False


def test_copy_run_job_skip_accumulators_initialized_before_try():
    src = inspect.getsource(copy_engine._copy_run_job)
    tree = ast.parse(src)
    fn = tree.body[0]
    first_try_idx = next(i for i, stmt in enumerate(fn.body) if isinstance(stmt, ast.Try))
    assigned_before_try = set()
    for stmt in fn.body[:first_try_idx]:
        if isinstance(stmt, ast.Assign):
            assigned_before_try.update(
                target.id for target in stmt.targets if isinstance(target, ast.Name)
            )
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            assigned_before_try.add(stmt.target.id)

    assert {
        "skipped_missing_source",
        "skipped_grid_snapshot",
        "skipped_v5_snapshot",
    }.issubset(assigned_before_try)


def test_copy_demotes_nonfatal_source_image_upload_errors():
    rep = {
        "errors": [
            "source image RAvUo2CZkUvp: target upload returned empty hash",
            "source image wrenWHX0MZUS: target preview upload failed: boom",
            "verification gate: 1 незакрытых дефектов",
        ],
    }

    copy_postprocess._copy_demote_nonfatal_image_upload_errors(rep)

    assert rep["errors"] == ["verification gate: 1 незакрытых дефектов"]
    assert rep["warnings"] == [
        "source image RAvUo2CZkUvp: target upload returned empty hash",
        "source image wrenWHX0MZUS: target preview upload failed: boom",
    ]


def test_copy_uac_result_rows_carry_images_pool_for_live_verifier():
    """Copy UAC result row несёт `images_pool` → короткий пул целевых картинок
    даёт warn UAC_IMAGES_POOL_SHORT, а не hard UAC_IMAGES_LOW (иначе verification gate
    роняет job porg-7hr5pmk6→porg-m6m222et: 5×repair_plan:UAC_IMAGES_LOW).

    Тянем поле ровно так, как это делает live_verifier (created_campaigns → c['result']),
    чтобы правка copy_uac и её чтение в верификаторе не разъехались.
    """
    from direct.core.campaign_result import created_campaigns
    from direct import uac_verifier

    src = Path(__file__).resolve().parents[1].joinpath(
        "copy_service/copy_uac.py").read_text(encoding="utf-8")
    # Имя локальной переменной пула — деталь реализации; тест сторожит КОНТРАКТ: ключ
    # `images_pool` физически попадает в result-строку UAC. Раньше здесь стояло имя из
    # первой редакции патча (`img_pool_by_cidx`), а опубликована была версия с `_images_pool`
    # — тест падал на живом коде, который работает правильно.
    assert '"images_pool": int(' in src

    name = "tp6_cpc_site — МК - Общие - Автотаргетинг - Свердловская область"
    row = {"ok": True, "id": 713359541, "campaign_id": 713359541,
           "name": name, "kind": "uac", "source_id": 712882538, "images_pool": 3}
    c = created_campaigns([row])[0]
    assert c["kind"] == "uac"
    pool = (c.get("result") or {}).get("images_pool")
    assert pool == 3

    detail = {"status": "draft", "pricing": "PER_CLICK", "titles": 5, "texts": 3,
              "sitelinks": 8, "content": 3, "images": 3, "week_limit": 1000,
              "limit_period": "week", "regions": 1, "counters": 1, "goals": 1,
              "has_tracking_params": True}
    issues, repair = uac_verifier.verify_uac_detail(
        name, 713359541, detail, {"images_pool": pool})
    codes = {i["code"] for i in issues}
    assert "UAC_IMAGES_LOW" not in codes
    assert "UAC_IMAGES_POOL_SHORT" in codes
    assert not [r for r in repair if r.get("kind") == "recreate_or_resume_campaign"]


def test_copy_attach_callouts_retries_grid_campaign_edit_row_lag(tmp_path, monkeypatch):
    calls = []
    sleeps = []
    logs = []

    class FakeGrid:
        def set_campaign_callouts(self, campaign_ids, callout_ids):
            calls.append((list(campaign_ids), list(callout_ids)))
            if len(calls) == 1:
                raise RuntimeError("Grid set-callouts: не удалось прочитать кампанию 713203571")
            return [{"id": str(campaign_ids[0])}]

    src_dir = tmp_path / "source"
    src_dir.mkdir()
    (src_dir / "campaign_callouts.json").write_text("{}", encoding="utf-8")
    ctx = copy_steps.CopyCtx(
        target_login="target",
        target_agency="agency",
        src_dir=src_dir,
        workdir=tmp_path,
        body={},
        maps={"campaigns": {"713062861": 713203571}, "callouts": {"43730780": 43798726}},
        grid=FakeGrid(),
        log=logs.append,
    )
    monkeypatch.setattr(copy_asset_steps.time, "sleep", lambda delay: sleeps.append(delay))

    out = copy_steps.step_attach_callouts(ctx)

    assert out["attached_campaigns"] == 1
    assert out["errors"] == []
    assert calls == [([713203571], [43798726]), ([713203571], [43798726])]
    assert sleeps == [2]
    assert logs[-1].startswith("уточнения по кампаниям:")


def test_copy_live_gate_blocks_adprice_warning():
    rep = {
        "errors": [],
        "live_verification": {
            "summary": {"errors": 0, "warnings": 1},
            "repair_plan": {
                "actions": [{
                    "action": "adprice_repair",
                    "issue_code": "NO_ADPRICE_LIVE",
                    "campaign_id": 101,
                }]
            },
        },
        "copy_verify": {"results": [], "summary": {"ok": 1}},
    }

    copy_postprocess._copy_apply_verification_gate(rep)

    assert rep["verification_gate"]["ok"] is False
    assert "NO_ADPRICE_LIVE" in str(rep["verification_gate"]["blockers"])
    assert rep["errors"]


def test_copy_demotes_verified_adaptive_partial_error():
    rep = {
        "errors": ["grid update adaptive: обновлено 278/285 объявлений"],
        "live_verification": {"status": "pass", "issues": []},
        "copy_verify": {"results": [], "summary": {"ok": 3}},
    }

    copy_postprocess._copy_demote_verified_adaptive_partial_errors(rep)
    copy_postprocess._copy_apply_verification_gate(rep)

    assert rep["errors"] == []
    assert rep["warnings"] == ["grid update adaptive: обновлено 278/285 объявлений"]
    assert rep["verification_gate"]["ok"] is True


def test_copy_verify_gate_blocks_media_and_listing_mismatches():
    verify_result = {
        "results": [
            {"scope": "campaign:1→2", "dimension": "ads_with_images",
             "status": "mismatch", "source": 5, "target": 4, "repairable": True},
            {"scope": "campaign:1→2", "dimension": "listing_filter_signature",
             "status": "mismatch", "source": {"g1": ["a"]}, "target": {"g2": ["b"]},
             "repairable": False},
            {"scope": "campaign:1→2", "dimension": "ad_price",
             "status": "excluded_intentional", "source": None, "target": None},
        ],
        "summary": {"ok": 0, "mismatch": 2},
    }

    blockers = copy_postprocess._copy_verify_blockers(verify_result)

    assert [b["dimension"] for b in blockers] == ["ads_with_images", "listing_filter_signature"]


def test_copy_verify_gate_keeps_known_uac_id_map_gap_report_only():
    verify_result = {
        "results": [{
            "scope": "campaign:11→MISSING",
            "dimension": "campaign_exists",
            "status": "missing",
            "source": 11,
            "target": None,
            "repairable": False,
            "repair_hint": "UAC tp6/tp7 не пишутся в id_maps['campaigns']",
        }],
        "summary": {"missing": 1},
    }

    assert copy_postprocess._copy_verify_blockers(verify_result) == []


def test_copy_verify_gate_blocks_top_level_verify_error():
    blockers = copy_postprocess._copy_verify_blockers({"error": "build_target_profile: boom"})

    assert blockers[0]["dimension"] == "VERIFY_ERROR"


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


def test_copy_expected_snapshot_excludes_uac_rows_with_dunder_typename():
    selected = set(range(1, 49))
    uac_rows = [
        {"id": str(i), "__typename": "GdUacCampaign", "name": f"МК {i}"}
        for i in range(23, 49)
    ]

    selected_uac = [row for row in uac_rows if copy_uac._copy_is_uac_grid_row(row)]
    expected, skipped = copy_engine._copy_expected_snapshot_count(selected, selected_uac, [
        {"Id": i, "Name": f"camp {i}", "Type": "TEXT_CAMPAIGN", "State": "ON", "Status": "ACCEPTED"}
        for i in range(1, 23)
    ])

    assert len(selected_uac) == 26
    assert expected == 22
    assert skipped == []


def test_copy_marks_grid_post_campaigns_as_unsupported_skips():
    rows = [
        {
            "id": "713257258",
            "name": "tp8_cpc_site_ct0000_aon_n000_r0002_ct018_ag001_g00 — Посевы Telegram",
            "typename": "GdPostCampaign",
            "status": "DRAFT",
        },
        {
            "id": "713254333",
            "name": "tp2_cpc_site — Поиск",
            "typename": "GdUnifiedCampaign",
            "status": "DRAFT",
        },
        {
            "id": "713254324",
            "name": "tp7_cpc_site_ct0000_aon_n000_r0002_ct010_ag001_g00 — ТК",
            "typename": "GdTextCampaign",
            "status": "DRAFT",
        },
    ]

    skipped = copy_engine._copy_unsupported_grid_only_skips(
        rows,
        {713257258, 713254333, 713254324},
        {713254333},
    )

    assert [x["Id"] for x in skipped] == [713257258]
    assert skipped[0]["reason"] == "unsupported_grid_post"


def test_copy_run_job_routes_only_grid_post_campaign_to_post_copy(monkeypatch):
    upserts = []
    logs = []
    row = {
        "id": "713257258",
        "name": "tp8_cpc_site_ct0000_aon_n000_r0002_ct018_ag001_g00 — Посевы Telegram",
        "typename": "GdPostCampaign",
        "status": "DRAFT",
    }

    monkeypatch.setattr(copy_engine, "_copy_job_upsert", lambda _job_id, **fields: upserts.append(fields) or fields)
    monkeypatch.setattr(copy_engine, "_copy_job_log", lambda _job_id, msg: logs.append(msg))
    monkeypatch.setattr(copy_engine, "_copy_selected_grid_campaigns", lambda _login, _ids: [row])
    monkeypatch.setattr(copy_engine, "_resolve_agency_hint", lambda _login, _agency='': "victoryagency14")
    monkeypatch.setattr(copy_engine, "_direct_tokens", lambda: {"victoryagency14": "token"})
    monkeypatch.setattr(copy_engine, "_token_for_login", lambda *_args: ("token", "victoryagency14"))
    monkeypatch.setattr(copy_engine, "_v5_call", lambda *_args, **_kwargs: {"result": {"Campaigns": []}})
    monkeypatch.setattr(
        copy_engine,
        "_direct_copy_module",
        lambda: (_ for _ in ()).throw(AssertionError("post copy must not load v5 pull module")),
    )
    monkeypatch.setattr(
        copy_engine,
        "_copy_grid_post_campaigns",
        lambda job_id, body, rows, workdir: {
            "ok": True,
            "copy_depth": "grid_cookie_post",
            "results": [{"ok": True, "source_id": rows[0]["id"], "campaign_id": 999}],
            "errors": [],
            "workdir": str(workdir),
        },
    )

    copy_engine._copy_run_job("job-post", {
        "source_login": "porg-4ealp4ry",
        "target_login": "porg-4ealp4ry",
        "campaign_ids": [713257258],
        "agency": "victoryagency14",
    })

    final = upserts[-1]
    assert final["status"] == "done"
    assert final["result"]["copy_depth"] == "grid_cookie_post"
    assert final["result"]["results"][0]["campaign_id"] == 999
    assert any("Post campaigns без Direct API/v5 snapshot" in msg for msg in logs)


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


def test_copy_postprocess_executes_adprice_repair_for_live_warning(monkeypatch):
    calls = []

    def fake_step_prices(ctx, campaign_ids=None):
        calls.append((ctx, campaign_ids))
        return {"priced": 3, "errors": []}

    monkeypatch.setattr(copy_steps, "step_prices", fake_step_prices)
    cstep_ctx = SimpleNamespace(target_login="target-login")
    plan = {"actions": [{"action": "adprice_repair", "campaign_id": 101}]}

    out = copy_postprocess._copy_execute_adprice_repairs(
        cstep_ctx,
        plan,
        {"body": {}, "results": []},
    )

    assert out["ok"] is True
    assert out["executed"] == 1
    assert calls[0][1] == [101]


def test_copy_price_segment_detects_common_from_campaign_name():
    assert copy_price_steps._price_segment_from_names("Автокредит", "РСЯ - Общее - КС") == "Общее"
    assert copy_price_steps._price_segment_from_names("01 | Changan", "РСЯ - Марки - КС") == "Марки"
    assert copy_price_steps._price_segment_from_names("01 | Changan Uni-K", "РСЯ - Модели - КС") == "Модели"
    assert copy_price_steps._price_segment_from_names("01 | Changan Uni-K", "legacy name") == "Модели"


def test_copy_prices_use_feed_minimum_for_non_mark_model_segment(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "ads.json").write_text(
        '[{"Id": "1", "AdGroupId": "10"}]',
        encoding="utf-8",
    )
    (src_dir / "adgroups.json").write_text(
        '[{"Id": "10", "CampaignId": "20", "Name": "Автокредит"}]',
        encoding="utf-8",
    )
    (src_dir / "campaigns.json").write_text(
        '[{"Id": "20", "Name": "РСЯ - Общее - КС"}]',
        encoding="utf-8",
    )

    class FakeGrid:
        def adaptive_ads_for_update(self, campaign_ids, ad_ids):
            assert campaign_ids == [200]
            assert ad_ids == [100]
            return {100: {"titles": ["t"], "bodies": ["b"], "href": "https://target.test"}}

    calls = []
    written = []

    def fake_group_ad_price(prices, brand, segment):
        calls.append((prices, brand, segment))
        return (777000, 0) if segment == "Общее" else (0, 0)

    ctx = SimpleNamespace(
        target_login="target-login",
        src_dir=src_dir,
        workdir=tmp_path,
        body={},
        maps={"feeds": {"11": 123}, "campaigns": {"20": 200}, "ads": {"1": 100}},
        grid=FakeGrid(),
        feed_offer_prices=lambda login, feed_id: {"lada": (777000, 0), "haval": (990000, 0)},
        account_offer_prices=None,
        group_ad_price=fake_group_ad_price,
        set_ad_prices=lambda login, items, apply_combo_button=False: written.extend(items) or len(items),
        log=lambda _m: None,
    )

    out = copy_price_steps.step_prices(ctx)

    assert calls == [({"lada": (777000, 0), "haval": (990000, 0)}, "Автокредит", "Общее")]
    assert out["priced"] == 1
    assert out["by_min_fallback"] == 1
    assert out["no_price"] == 0
    assert written[0]["current"] == 777000


def test_copy_adaptive_creatives_remaps_multicards(monkeypatch, tmp_path):
    class FakeSourceGrid:
        def adaptive_ads_for_update(self, _campaign_ids, _ad_ids):
            return {
                11: {
                    "titles": ["Заголовок"],
                    "bodies": ["Текст"],
                    "imageHashes": ["src-main"],
                    "multicards": [
                        {"imageHash": "src-card", "currency": None, "href": None,
                         "price": None, "priceOld": None, "text": None}
                    ],
                }
            }

    calls = []

    def fake_update(_login, items, campaign_ids):
        calls.append((items, campaign_ids))
        return len(items)

    ctx = copy_steps.CopyCtx(
        target_login="target-login",
        target_agency="",
        src_dir=Path(tmp_path),
        workdir=Path(tmp_path),
        body={},
        maps={
            "ads": {"11": 22},
            "campaigns": {"101": 202},
            "images": {"src-main": "tgt-main", "src-card": "tgt-card"},
        },
        grid=object(),
        source_grid=FakeSourceGrid(),
        update_adaptive_ads=fake_update,
    )

    out = copy_steps.step_adaptive_creatives(ctx)

    assert out["updated"] == 1
    assert out["multicards_remapped"] == 1
    assert calls[0][0][0]["multicards"] == [
        {"imageHash": "tgt-card", "currency": None, "href": None,
         "price": None, "priceOld": None, "text": None}
    ]


def test_copy_adaptive_creatives_partial_grid_update_is_warning(tmp_path):
    class FakeSourceGrid:
        def adaptive_ads_for_update(self, _campaign_ids, _ad_ids):
            return {
                11: {"titles": ["Заголовок"], "bodies": ["Текст"]},
                12: {"titles": ["Заголовок 2"], "bodies": ["Текст 2"]},
            }

    def fake_update(_login, _items, _campaign_ids):
        raise RuntimeError("обновлено 1/2 объявлений")

    ctx = copy_steps.CopyCtx(
        target_login="target-login",
        target_agency="",
        src_dir=Path(tmp_path),
        workdir=Path(tmp_path),
        body={},
        maps={
            "ads": {"11": 22, "12": 23},
            "campaigns": {"101": 202},
            "images": {},
        },
        grid=object(),
        source_grid=FakeSourceGrid(),
        update_adaptive_ads=fake_update,
    )

    out = copy_steps.step_adaptive_creatives(ctx)

    assert out["updated"] == 1
    assert out["errors"] == []
    assert "grid update adaptive partial" in out["warnings"][0]


def test_grid_update_adaptive_ads_preserves_multicards(monkeypatch):
    payloads = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "data": {
                    "updateAdaptiveTextAds": {
                        "updatedAds": [{"id": "22"}],
                        "validationResult": {"errors": []},
                    }
                }
            }

    class FakeGrid:
        def adaptive_ads_for_update(self, _campaign_ids, _ad_ids):
            return {
                22: {
                    "href": "https://example.test",
                    "titles": ["Old title"],
                    "bodies": ["Old body"],
                    "imageHashes": ["tgt-main"],
                    "creativeIds": [],
                    "multicards": [
                        {"imageHash": "tgt-card", "currency": None, "href": None,
                         "price": None, "priceOld": None, "text": None}
                    ],
                }
            }

        def _bootstrap_csrf(self):
            return None

        def _post(self, _op, _query, variables):
            payloads.append(variables)
            return FakeResponse()

    monkeypatch.setattr(
        create_set_feeds,
        "gf",
        SimpleNamespace(get_grid_client=lambda _login: FakeGrid()),
        raising=False,
    )

    updated = create_set_feeds._grid_update_adaptive_ads(
        "target-login",
        [{"id": 22, "titles": ["New title"], "bodies": ["New body"]}],
        campaign_ids=[202],
        apply_combo_button=False,
    )

    assert updated == 1
    item = payloads[0]["updateInput"]["adUpdateItems"][0]
    assert item["multicards"][0]["imageHash"] == "tgt-card"


def test_grid_update_adaptive_ads_chunks_and_retries_partial_updated_ads(monkeypatch):
    posted_batches = []

    class FakeResponse:
        status_code = 200

        def __init__(self, rows):
            self._rows = rows

        def json(self):
            return {
                "data": {
                    "updateAdaptiveTextAds": {
                        "updatedAds": self._rows,
                        "validationResult": None,
                    }
                }
            }

    class FakeGrid:
        def adaptive_ads_for_update(self, _campaign_ids, ad_ids):
            return {
                int(aid): {
                    "href": "https://example.test",
                    "titles": ["Old title"],
                    "bodies": ["Old body"],
                    "imageHashes": [],
                    "creativeIds": [],
                }
                for aid in ad_ids
            }

        def _bootstrap_csrf(self):
            return None

        def _post(self, _op, _query, variables):
            items = variables["updateInput"]["adUpdateItems"]
            ids = [str(it["id"]) for it in items]
            posted_batches.append(ids)
            if len(ids) == 35:
                return FakeResponse([{"id": aid} for aid in ids[:28]] + [None for _ in ids[28:]])
            return FakeResponse([{"id": aid} for aid in ids])

    monkeypatch.setattr(
        create_set_feeds,
        "gf",
        SimpleNamespace(get_grid_client=lambda _login: FakeGrid()),
        raising=False,
    )

    items = [{"id": i, "titles": [f"Title {i}"], "bodies": [f"Body {i}"]}
             for i in range(1, 286)]
    updated = create_set_feeds._grid_update_adaptive_ads(
        "target-login", items, campaign_ids=[202], apply_combo_button=False)

    assert updated == 285
    assert [len(batch) for batch in posted_batches[:6]] == [50, 50, 50, 50, 50, 35]
    assert posted_batches[6:] == [[str(i)] for i in range(279, 286)]


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


def test_copy_grid_create_full_strips_nested_graphql_typenames(monkeypatch):
    captured = []
    dirty_variables = {
        "input": {
            "campaignAddItems": [{
                "unifiedCampaign": {
                    "__typename": "GdUnifiedCampaign",
                    "notification": {
                        "smsSettings": {
                            "smsTime": {
                                "__typename": "GdTimeInterval",
                                "startTime": {"__typename": "GdTime", "hour": 9, "minute": 0},
                                "endTime": {"__typename": "GdTime", "hour": 21, "minute": 0},
                            }
                        }
                    },
                }
            }]
        }
    }

    def fake_base_mutate(self, op, query, variables):  # noqa: ARG001
        captured.append(variables)
        return {"ok": True}

    def fake_create_full(login, **_kwargs):
        cl = copy_grid_unified.gc.GridCreateClient(login, cookie="stub=1")
        return cl._mutate("AddCampaigns", "q", dirty_variables)

    monkeypatch.setattr(copy_grid_unified.gc.GridCreateClient, "_mutate", fake_base_mutate)
    monkeypatch.setattr(copy_grid_unified.gc, "create_full", fake_create_full)

    assert copy_grid_unified._copy_create_full_sanitized(
        "porg-test", campaign_spec={}, groups=[], region_ids=[], href="") == {"ok": True}
    sent = captured[0]["input"]["campaignAddItems"][0]["unifiedCampaign"]
    assert "__typename" not in str(sent)
    assert sent["notification"]["smsSettings"]["smsTime"]["endTime"] == {"hour": 21, "minute": 0}


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


def test_build_target_profile_strategy_name_uses_v5_not_grid_write_enum(monkeypatch):
    """D12 strategy_name сверяется на v5-поверхности, как источник.

    Регрессия porg-x7wkhs7d→porg-c6rxuenb (2026-08): цель брала strategy_name из Grid
    strategyData.strategyName (write-enum AUTOBUDGET/AUTOBUDGET_AVG_CPA/DEFAULT_), а источник —
    из v5 BiddingStrategyType (SERVING_OFF). Разные номенклатуры → verification-gate осаживал
    исправно скопированные РСЯ-кампании ложным strategy_name mismatch. Цель обязана брать v5.
    """
    from unittest.mock import MagicMock

    tgt_cid = 713362669

    def fake_v5_call(service, method, token, login, params):
        if service == "campaigns" and method == "get":
            return {"result": {"Campaigns": [{
                "Id": tgt_cid,
                "Type": "TEXT_CAMPAIGN",
                "TextCampaign": {"BiddingStrategy": {
                    "Search": {"BiddingStrategyType": "SERVING_OFF"},
                    "Network": {"BiddingStrategyType": "AVERAGE_CPC"},
                }},
            }]}}
        return {"result": {}}

    saved = (copy_verify_state._v5_call,
             copy_verify_state._token_for_login,
             copy_verify_state._direct_tokens)
    copy_verify_state.configure({
        "_v5_call": fake_v5_call,
        "_token_for_login": lambda *_a, **_k: ("tok", ""),
        "_direct_tokens": lambda: {},
    })
    try:
        profile = build_target_profile(
            "porg-c6rxuenb",
            {"campaigns": {"713336659": tgt_cid}, "ads": {}},
            grid=MagicMock(),
            cached_counts={tgt_cid: {}},
            # Grid write-enum, который РАНЬШЕ протекал в профиль и ломал сверку:
            cached_edit_rows={tgt_cid: {"strategyData": {"strategyName": "AUTOBUDGET"}}},
            cached_invariants={tgt_cid: {}},
            cached_adaptive={},
        )
    finally:
        copy_verify_state.configure({
            "_v5_call": saved[0],
            "_token_for_login": saved[1],
            "_direct_tokens": saved[2],
        })

    # v5 BiddingStrategyType (Search-first) — та же поверхность, что у источника.
    assert profile[str(tgt_cid)]["strategy_name"] == "SERVING_OFF"


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


def test_copy_fill_missing_target_geo_from_account_context(monkeypatch):
    monkeypatch.setattr(
        copy_engine,
        "_copy_ctx",
        lambda login: {"city": "Уфа", "region": "Республика Башкортостан"} if login == "porg-c6rxuenb" else {},
    )
    body = {"target_city": "", "target_region": ""}

    city, region, filled = copy_engine._copy_fill_missing_target_geo(
        body, "porg-c6rxuenb", "auto", "replace"
    )

    assert (city, region, filled) == ("Уфа", "Республика Башкортостан", True)
    assert body["target_city"] == "Уфа"
    assert body["target_region"] == "Республика Башкортостан"


def test_copy_fill_missing_target_geo_keeps_other_keep_empty(monkeypatch):
    calls = []
    monkeypatch.setattr(copy_engine, "_copy_ctx", lambda login: calls.append(login) or {"city": "Уфа"})
    body = {}

    city, region, filled = copy_engine._copy_fill_missing_target_geo(
        body, "porg-c6rxuenb", "other", "keep"
    )

    assert (city, region, filled) == ("", "", False)
    assert calls == []
    assert "target_city" not in body


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
            if "SELECT * FROM victoryads_direct_automation.jobs" in sql:
                self._select = True
            if "UPDATE victoryads_direct_automation.jobs SET agent_board_task_id" in sql:
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
        lambda title, desc, *, requested_by, initiated_by="": calls.update(
            task=(title, desc, requested_by, initiated_by)
        ) or 123,
    )

    task_id = agent_board_bridge.notify_copy_job_error(lambda: FakeConn(), "failed-copy")

    assert task_id == 123
    assert calls["update"] == (123, "failed-copy")
    title, desc, requested_by, initiated_by = calls["task"]
    assert "source-login" in title
    assert "target-login" in title
    assert requested_by == "tester"
    assert initiated_by == "tester"
    assert "Инициатор копирования: tester" in desc
    assert "После `done` copy-service автоматически поставит повторную" in desc


def test_copy_initiator_prefers_original_user_over_retry_robot():
    """В цепочке авто-повторов created_by = робот, имя человека живёт в _copy_retry_original_user."""
    retry_row = {"body": {"created_by": "agent-board-auto", "_copy_retry_original_user": "terehov"}}
    assert agent_board_bridge._copy_initiator(retry_row) == "terehov"
    assert agent_board_bridge._copy_initiator({"body": {"created_by": "terehov"}}) == "terehov"
    assert agent_board_bridge._copy_initiator({"body": {"created_by": "agent-board-auto"}}) == ""


def test_copy_jobs_ready_for_agent_retry_waits_for_published_task(monkeypatch):
    """Повтор копирования ставится только по ОПУБЛИКОВАННОЙ задаче Board, а не по `done`.

    Раньше тест сторожил `_agent_board_done_task_meta`. Условие ужесточили осознанно:
    у #167 задача была `done`, а публикация патча заблокирована конфликтом — повторы уходили
    на НЕИЗМЕНЁННЫЙ код и падали той же ошибкой по кругу (2026-08-06).
    """
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
        "_agent_board_published_copy_task_meta",
        lambda task_ids: {77: {"id": 77, "status": "published"}},
    )

    ready = agent_board_bridge.copy_jobs_ready_for_agent_retry(lambda: FakeConn(), limit=5)

    assert [r["job_id"] for r in ready] == ["failed-copy-1"]
    select_sql = next(sql for sql in executed if "FROM candidates j" in sql)
    assert "JOIN agent_board.tasks" not in select_sql
    assert "LEFT JOIN victoryads_direct_automation.jobs r" in select_sql


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
            "created_by": "scherbakova",
            "campaign_ids": [11, 22],
            "target_cleanup": "none",
        },
    }

    body = queue_server._copy_retry_body_from_failed(row)

    assert body["_kind"] == "copy_campaigns"
    assert body["login"] == "target-login"
    assert body["target_login"] == "target-login"
    assert body["created_by"] == "agent-board-auto"
    assert body["_copy_retry_original_user"] == "scherbakova"
    assert body["_copy_retry_of"] == "failed1"
    assert body["_copy_retry_agent_board_task_id"] == 77
    # ⛔ Своих target-id не известно → чистить НЕЧЕГО. Раньше тут ожидался `delete_drafts`,
    # и ровно это поведение 2026-08-06 снесло в кабинете клиента ВСЕ черновики: удачные копии
    # и чужую «Системную кампанию eLama». Чистка обязана быть адресной.
    assert body["target_cleanup"] == "none"
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


def test_copy_uac_limits_titles_after_geo_replacement():
    title = "Купить BAIC в Нижнем Новгороде. Цена от 5 351 ₽/мес. Звоните!"

    limited = copy_uac._copy_uac_limit_strings([title], 56)

    assert limited == ["Купить BAIC в Нижнем Новгороде. Цена от 5 351 ₽/мес"]
    assert len(limited[0]) <= 56


def test_copy_uac_pads_short_title_and_text_payloads_to_live_minimums():
    titles = copy_uac._copy_uac_pad_strings(
        ["Купить Chery в Екатеринбурге"],
        copy_uac._UAC_FALLBACK_TITLES,
        need=5,
        max_len=copy_uac._UAC_TITLE_MAX,
    )
    texts = copy_uac._copy_uac_pad_strings(
        [],
        copy_uac._UAC_FALLBACK_TEXTS,
        need=3,
        max_len=copy_uac._UAC_TEXT_MAX,
    )

    assert len(titles) == 5
    assert len(texts) == 3
    assert len({t.casefold() for t in titles}) == 5
    issues, _repair = uac_verifier.verify_uac_detail(
        "tp6_cpa_site — РСЯ - Общие - Ключевики - Свердловская область",
        713321700,
        {
            "status": "draft",
            "pricing": "PER_CONVERSION",
            "week_limit": 5000,
            "limit_period": "week",
            "counters": 1,
            "goals": 1,
            "regions": 1,
            "has_tracking_params": True,
            "titles": len(titles),
            "texts": len(texts),
            "sitelinks": 8,
            "images": 5,
            "content": 5,
        },
        {"images_pool": 5},
    )
    codes = {i["code"] for i in issues}
    assert "UAC_TITLES_MISSING" not in codes
    assert "UAC_TEXTS_MISSING" not in codes


def test_copy_uac_sanitizes_inline_minus_keywords():
    keywords, minus_keywords = copy_uac._copy_uac_sanitize_keywords(
        ["купить baic -авто -машина -новый -автомобиль", "-отзывы"],
        ["-бу", "кредит отзывы"],
    )

    assert keywords == ["купить baic"]
    assert minus_keywords == ["авто", "машина", "новый", "автомобиль", "отзывы", "бу", "кредит"]


def test_copy_uac_limits_keyword_words_after_geo_replacement():
    keywords, minus_keywords = copy_uac._copy_uac_sanitize_keywords(
        ["авито нижний новгород нижегородская область авто +с пробегом купить"],
        [],
    )

    assert keywords == ["авито нижний новгород нижегородская область +с пробегом"]
    assert len(keywords[0].split()) == 7
    assert minus_keywords == []


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


def test_copy_snapshot_preflight_accepts_other_change_region_ids_without_text_geo(tmp_path):
    (tmp_path / "campaigns.json").write_text(
        '[{"Id": 713336659, "Name": "campaign", "Type": "TEXT_CAMPAIGN"}]',
        encoding="utf-8",
    )
    (tmp_path / "adgroups.json").write_text(
        '[{"Id": 1, "Name": "group", "RegionIds": [54]}]',
        encoding="utf-8",
    )
    (tmp_path / "ads.json").write_text(
        '[{"Id": 2, "AdGroupId": 1}]',
        encoding="utf-8",
    )
    (tmp_path / "shopping_ads.json").write_text("[]", encoding="utf-8")

    audit = copy_snapshot._copy_snapshot_preflight(
        tmp_path,
        target_feed_url="",
        target_city="",
        target_region="",
        geo_mode="change",
        geo_region_ids=[54],
    )

    assert "целевое гео пустое" not in audit["critical"]


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


def test_copy_uac_rebuilds_client_once_on_linkinfo_need_reset(monkeypatch):
    from direct.clients import uac_read

    class FakeReader:
        def __init__(self, login):
            self.login = login

        def campaign_detail(self, _cid):
            return {
                "href": "https://source.example/car",
                "titles": ["Title"],
                "texts": ["Text"],
                "keywords": ["keyword"],
                "minus_keywords": [],
                "contents": [],
            }

    class ExpiredClient:
        def create_master_campaign(self, _spec, *, launch=False):
            raise RuntimeError('[linkinfo] HTTP 401: {"text":"need_reset","description":"Истек срок действия сессии"}')

    class FreshClient:
        def create_master_campaign(self, _spec, *, launch=False):
            return "713"

        def _request(self, *_args, **_kwargs):
            return {"result": {}}

    calls = []

    def fake_build_client(login, *, account=None, force_refresh=False):
        calls.append((login, account, force_refresh))
        return FreshClient() if force_refresh else ExpiredClient()

    monkeypatch.setattr(uac_read, "UacReadClient", FakeReader)
    monkeypatch.setattr(copy_uac.cmc, "build_client", fake_build_client)
    monkeypatch.setattr(copy_uac, "_copy_uac_create_live_guard", lambda *_args, **_kwargs: [])

    rep = copy_uac._copy_uac_campaigns(
        "source",
        "target",
        "agency-main",
        [{"id": 712, "name": "tp6_test", "typename": "uac"}],
        {"target_domain": "target.example", "_copy_source_domain": "source.example"},
        target_href="https://target.example",
        region_ids=[121],
        counter_id=1,
        goal_id=2,
        target_feed_id=None,
    )

    assert rep["errors"] == []
    assert rep["created"] == 1
    assert rep["results"][0]["campaign_id"] == 713
    assert calls == [
        ("target", "agency-main", False),
        ("target", "agency-main", True),
    ]


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
        "cookie_postprocess": {
            "verification_gate": {
                "ok": False,
                "blockers": [{"dimension": "ads_with_images", "status": "mismatch"}],
            }
        },
    })

    assert summary["verification"]["diff_count"] == 1
    assert summary["verification_gate"]["ok"] is False
    assert summary["verification_gate"]["blockers_count"] == 1


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


def test_copy_verify_source_does_not_count_shopping_grid_payload_as_adaptive(tmp_path):
    (tmp_path / "campaigns.json").write_text('[{"Id": 11}]', encoding="utf-8")
    (tmp_path / "adgroups.json").write_text('[{"Id": 101, "CampaignId": 11}]', encoding="utf-8")
    (tmp_path / "ads.json").write_text(
        '[{"Id": 1001, "CampaignId": 11, "AdGroupId": 101, "Type": "SHOPPING_AD"}]',
        encoding="utf-8",
    )
    (tmp_path / "shopping_ads.json").write_text(
        '[{"Id": 1001, "CampaignId": 11, "AdGroupId": 101, "Type": "SHOPPING_AD", '
        '"ShoppingAd": {"FeedFilterConditions": []}}]',
        encoding="utf-8",
    )
    for name in [
        "keywords.json",
        "bidmodifiers.json",
        "adimages.json",
    ]:
        (tmp_path / name).write_text("[]", encoding="utf-8")

    profile = build_source_profile(
        tmp_path,
        grid_snapshot={
            1001: {
                "id": 1001,
                "campaignId": 11,
                "titles": ["Chery"],
                "bodies": ["В наличии"],
                "imageHashes": ["hash"],
            },
        },
    )

    assert profile["11"]["ads_with_titles"] == 0
    assert profile["11"]["ads_with_texts"] == 0
    assert profile["11"]["ads_with_images"] == 0
    assert profile["11"]["shopping_count"] == 1


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


def test_grid_client_accepts_refresh_explicit_cookie_and_refreshes(monkeypatch):
    calls = []

    def fake_pick(login, force_refresh=False):
        calls.append((login, force_refresh))
        return "fresh-cookie"

    monkeypatch.setattr(grid_finalize.cmc, "pick_working_cookie", fake_pick)
    client = grid_finalize.GridClient(
        "login",
        cookie="old-cookie",
        refresh_explicit_cookie=True,
    )
    client._bootstrap_csrf = lambda: setattr(client, "csrf", "fresh-csrf")

    client._reauth()

    assert client.cookie == "fresh-cookie"
    assert calls == [("login", True)]


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


def test_rmw_update_drops_duplicated_titles_and_bodies():
    """Grid валит ВЕСЬ item на дубле заголовка — дубли обязаны отсеиваться до отправки."""
    from direct.create import create_set_feeds

    assert create_set_feeds._dedup_grid_texts(
        ["Купить BMW", "Купить BMW", " Купить BMW ", "Кредит"]
    ) == ["Купить BMW", "Кредит"]
    # Регистр не приводим: для Директа это разные заголовки.
    assert create_set_feeds._dedup_grid_texts(["BMW X5", "bmw x5"]) == ["BMW X5", "bmw x5"]
    assert create_set_feeds._dedup_grid_texts(["", None, "  "]) == []


def test_rmw_update_sends_deduplicated_titles(monkeypatch):
    from direct.create import create_set_feeds

    sent: list[dict] = []

    class FakeGrid:
        def adaptive_ads_for_update(self, _cids, _ad_ids):
            return {11: {"href": "https://target.ru", "titles": ["Дубль", "Дубль"],
                         "bodies": ["Текст", "Текст"], "imageHashes": []}}

        def _bootstrap_csrf(self):
            return None

        def _post(self, _op, _query, variables):
            batch = variables["updateInput"]["adUpdateItems"]
            sent.extend(batch)

            class R:
                status_code = 200

                @staticmethod
                def json():
                    return {"data": {"updateAdaptiveTextAds": {
                        "updatedAds": [{"id": item["id"]} for item in batch],
                        "validationResult": {"errors": []},
                    }}}

            return R()

    monkeypatch.setattr(
        create_set_feeds,
        "gf",
        SimpleNamespace(get_grid_client=lambda _login: FakeGrid()),
        raising=False,
    )

    updated = create_set_feeds._grid_update_adaptive_ads(
        "target-login", [{"id": 11}], campaign_ids=[202], apply_combo_button=False
    )

    assert updated == 1
    assert sent[0]["titles"] == ["Дубль"]
    assert sent[0]["bodies"] == ["Текст"]


def test_agent_board_tasks_go_straight_to_claude_opus(monkeypatch):
    """Задачи из очередей Директа создаются сразу на Claude: у Codex лимит под 95%."""
    from direct import agent_board_bridge

    captured = {}

    class FakeAgentDb:
        @staticmethod
        def init_tables():
            return None

        @staticmethod
        def create_task(**kwargs):
            captured.update(kwargs)
            return {"id": 777}

    import sys
    import types

    module = types.ModuleType("agent_board")
    module.db = FakeAgentDb
    monkeypatch.setitem(sys.modules, "agent_board", module)
    monkeypatch.setitem(sys.modules, "agent_board.db", FakeAgentDb)

    task_id = agent_board_bridge._create_agent_task("t", "d", requested_by="Ilyin")

    assert task_id == 777
    assert captured["assigned_agent"] == "claude-code"
    assert captured["model"] == "opus"
    assert captured["requested_by"] == "Ilyin"


def test_copy_patches_direct_copy_fallback_for_average_cpa_multiple_goals():
    """v5 отвергает *_MULTIPLE_GOALS в campaigns.add → id_maps пустой → «not mapped»."""
    def original(strategy):
        return strategy, False

    dc = SimpleNamespace(strategy_fallback=original)

    assert copy_engine._copy_patch_direct_copy_strategy_fallback(dc) is True
    safe, downgraded = dc.strategy_fallback({
        "Search": {
            "BiddingStrategyType": "AVERAGE_CPA_MULTIPLE_GOALS",
            "AverageCpaMultipleGoals": {
                "WeeklySpendLimit": 5_000_000_000,
                "ExplorationBudget": {
                    "MinimumExplorationBudget": 5_000_000_000,
                    "IsMinimumExplorationBudgetCustom": "NO",
                },
            },
        },
        "Network": {"BiddingStrategyType": "NETWORK_DEFAULT"},
    })

    assert downgraded is True
    assert safe["Search"] == {
        "BiddingStrategyType": "WB_MAXIMUM_CLICKS",
        "WbMaximumClicks": {"WeeklySpendLimit": 5_000_000_000},
    }
    # Network без multi-goal не трогаем; повторный патч — no-op (идемпотентность).
    assert safe["Network"] == {"BiddingStrategyType": "NETWORK_DEFAULT"}
    assert copy_engine._copy_patch_direct_copy_strategy_fallback(dc) is False

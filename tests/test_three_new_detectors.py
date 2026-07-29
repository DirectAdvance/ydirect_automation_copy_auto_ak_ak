"""Tests for three new defect detectors + WRONG_AUTOTARGET severity elevation.

Detectors:
  - TP7_CATALOG_FILTER_EMPTY  (uac_read._filter_summary + uac_verifier.verify_uac_detail)
  - AD_HREF_ROOT_INSTEAD_OF_MODEL  (grid_read._enrich_adaptive_images + grid_content_verifier)
  - CPA_COUNT_PLAN_MISMATCH  (verifier.verify_create_set)
  - WRONG_AUTOTARGET severity=error  (grid_content_verifier, both tp2/4/5 and tp1 РСЯ paths)
"""
from __future__ import annotations

import pytest

from direct.clients.uac_read import _filter_summary
from direct.uac_verifier import verify_uac_detail
from direct.clients.grid_content_verifier import verify_grid_content
from direct.verifier import verify_create_set
from direct.live_verifier import verify_live_create_set


# ──────────────────────────────────────────────────────────────────────────────
# Detector 1: TP7_CATALOG_FILTER_EMPTY
# ──────────────────────────────────────────────────────────────────────────────

class TestFilterSummaryCatalogFilterHasValues:
    """Unit tests for _filter_summary() new catalog_filter_has_values field."""

    def _row(self, conditions):
        return {"listings_feed_filters": [{"conditions": conditions}]}

    def test_no_collection_filter_returns_none(self):
        row = {"listings_feed_filters": [{"conditions": [
            {"field": "model", "operator": "EQUALS", "values": ["Lada"]}
        ]}]}
        result = _filter_summary(row)
        assert result["has_collection_filter"] is False
        assert result["catalog_filter_has_values"] is None

    def test_collection_filter_with_values_returns_true(self):
        row = {"listings_feed_filters": [{"conditions": [
            {"field": "collectionId", "operator": "EQUALS", "values": ["col-123"]}
        ]}]}
        result = _filter_summary(row)
        assert result["has_collection_filter"] is True
        assert result["catalog_filter_has_values"] is True

    def test_collection_filter_empty_values_list_returns_false(self):
        row = {"listings_feed_filters": [{"conditions": [
            {"field": "collectionId", "operator": "EQUALS", "values": []}
        ]}]}
        result = _filter_summary(row)
        assert result["has_collection_filter"] is True
        assert result["catalog_filter_has_values"] is False

    def test_collection_filter_no_values_key_returns_false(self):
        row = {"listings_feed_filters": [{"conditions": [
            {"field": "collectionId", "operator": "EQUALS"}
        ]}]}
        result = _filter_summary(row)
        assert result["has_collection_filter"] is True
        assert result["catalog_filter_has_values"] is False

    def test_collection_id_underscore_variant_recognized(self):
        row = {"listings_feed_filters": [{"conditions": [
            {"field": "collection_id", "operator": "EQUALS", "values": ["abc"]}
        ]}]}
        result = _filter_summary(row)
        assert result["has_collection_filter"] is True
        assert result["catalog_filter_has_values"] is True

    def test_collection_filter_with_single_value_string_returns_true(self):
        row = {"listings_feed_filters": [{"conditions": [
            {"field": "collectionId", "operator": "EQUALS", "value": "col-42"}
        ]}]}
        result = _filter_summary(row)
        assert result["has_collection_filter"] is True
        assert result["catalog_filter_has_values"] is True


class TestTP7CatalogFilterEmpty:
    """verify_uac_detail fires TP7_CATALOG_FILTER_EMPTY when collection filter has no values."""

    def _run(self, detail_extra, nm="tp7_cpc_site — Товарная — Модели"):
        detail = {
            "has_feed": True,
            "has_model_filter": True,
            "has_collection_filter": True,
            **detail_extra,
        }
        issues, _ = verify_uac_detail(
            name=nm, campaign_id=999, detail=detail, expected={}
        )
        return issues

    def test_fires_when_catalog_filter_has_values_false(self):
        issues = self._run({"catalog_filter_has_values": False})
        codes = [i["code"] for i in issues]
        assert "TP7_CATALOG_FILTER_EMPTY" in codes

    def test_error_severity(self):
        issues = self._run({"catalog_filter_has_values": False})
        emp = [i for i in issues if i["code"] == "TP7_CATALOG_FILTER_EMPTY"]
        assert emp and emp[0]["severity"] == "error"

    def test_does_not_fire_when_values_present(self):
        issues = self._run({"catalog_filter_has_values": True})
        codes = [i["code"] for i in issues]
        assert "TP7_CATALOG_FILTER_EMPTY" not in codes

    def test_does_not_fire_when_no_collection_filter(self):
        detail = {
            "has_feed": True,
            "has_model_filter": True,
            "has_collection_filter": False,
            "catalog_filter_has_values": None,
        }
        issues, _ = verify_uac_detail(
            name="tp7_cpc_site — X", campaign_id=999, detail=detail, expected={}
        )
        codes = [i["code"] for i in issues]
        assert "TP7_CATALOG_FILTER_EMPTY" not in codes

    def test_does_not_fire_for_tp6(self):
        # tp6 is detected by name prefix; TP7_CATALOG_FILTER_EMPTY requires tp==7
        detail = {
            "has_feed": True,
            "has_model_filter": True,
            "has_collection_filter": True,
            "catalog_filter_has_values": False,
        }
        issues, _ = verify_uac_detail(
            name="tp6_cpc_site — МК — Модели", campaign_id=999, detail=detail, expected={}
        )
        codes = [i["code"] for i in issues]
        assert "TP7_CATALOG_FILTER_EMPTY" not in codes

    def test_does_not_fire_when_catalog_filter_has_values_none(self):
        """catalog_filter_has_values=None means no collection filter → silence."""
        issues = self._run({"catalog_filter_has_values": None})
        codes = [i["code"] for i in issues]
        assert "TP7_CATALOG_FILTER_EMPTY" not in codes


# ──────────────────────────────────────────────────────────────────────────────
# Detector 2: AD_HREF_ROOT_INSTEAD_OF_MODEL
# ──────────────────────────────────────────────────────────────────────────────

class TestAdHrefRootInsteadOfModel:
    """verify_grid_content fires AD_HREF_ROOT_INSTEAD_OF_MODEL for Модели/Марки campaigns."""

    def _run(self, name, counts_extra=None):
        counts = {
            "adgroups": 1, "ads": 3,
            "adaptive_images_read": True, "root_href_ads_read": True,
            **(counts_extra or {}),
        }
        issues, repair = verify_grid_content(name=name, campaign_id=100, counts=counts,
                                             expected={})
        return issues, repair

    def test_fires_for_modeli_segment_with_root_hrefs(self):
        issues, repair = self._run(
            name="tp5_cpc_site — Товарная галерея — Модели",
            counts_extra={"root_href_ads_count": 3},
        )
        codes = [i["code"] for i in issues]
        assert "AD_HREF_ROOT_INSTEAD_OF_MODEL" in codes

    def test_fires_for_marki_segment(self):
        issues, _ = self._run(
            name="tp5_cpc_site — Товарная галерея — Марки",
            counts_extra={"root_href_ads_count": 1},
        )
        codes = [i["code"] for i in issues]
        assert "AD_HREF_ROOT_INSTEAD_OF_MODEL" in codes

    def test_error_severity(self):
        issues, _ = self._run(
            name="tp5_cpc_site — Товарная галерея — Модели",
            counts_extra={"root_href_ads_count": 2},
        )
        hit = [i for i in issues if i["code"] == "AD_HREF_ROOT_INSTEAD_OF_MODEL"]
        assert hit and hit[0]["severity"] == "error"
        assert hit[0]["ads"] == 2

    def test_does_not_fire_when_root_href_count_zero(self):
        issues, _ = self._run(
            name="tp5_cpc_site — Товарная галерея — Модели",
            counts_extra={"root_href_ads_count": 0},
        )
        codes = [i["code"] for i in issues]
        assert "AD_HREF_ROOT_INSTEAD_OF_MODEL" not in codes

    def test_does_not_fire_without_read_flag(self):
        """Tri-state: если root_href_ads_read не выставлен — молчим."""
        counts = {"adgroups": 1, "ads": 3, "root_href_ads_count": 5}
        # root_href_ads_read not set
        issues, _ = verify_grid_content(
            name="tp5_cpc_site — Товарная галерея — Модели",
            campaign_id=100, counts=counts, expected={}
        )
        codes = [i["code"] for i in issues]
        assert "AD_HREF_ROOT_INSTEAD_OF_MODEL" not in codes

    def test_does_not_fire_for_non_model_segment(self):
        """Кампания без сегмента «Модели»/«Марки» — не детектируем."""
        issues, _ = self._run(
            name="tp5_cpc_site — Товарная галерея — Общее",
            counts_extra={"root_href_ads_count": 3},
        )
        codes = [i["code"] for i in issues]
        assert "AD_HREF_ROOT_INSTEAD_OF_MODEL" not in codes

    def test_does_not_fire_for_tp1_general(self):
        issues, _ = self._run(
            name="tp1_cpa_site — РСЯ — Общее",
            counts_extra={"root_href_ads_count": 5},
        )
        codes = [i["code"] for i in issues]
        assert "AD_HREF_ROOT_INSTEAD_OF_MODEL" not in codes

    def test_has_repair_candidate(self):
        _, repair = self._run(
            name="tp5_cpc_site — Товарная галерея — Модели",
            counts_extra={"root_href_ads_count": 1},
        )
        kinds = [r.get("kind") for r in repair]
        assert "rebuild_missing_content" in kinds

    def test_does_not_fire_when_root_href_ok_true(self):
        """root_href_ok=True → квизовый лендинг или заглушка без модельных страниц → молчим."""
        counts = {
            "adgroups": 1, "ads": 3,
            "adaptive_images_read": True, "root_href_ads_read": True,
            "root_href_ads_count": 5,
        }
        issues, repair = verify_grid_content(
            name="tp5_cpc_site — Товарная галерея — Марки",
            campaign_id=100, counts=counts,
            expected={"root_href_ok": True},
        )
        codes = [i["code"] for i in issues]
        assert "AD_HREF_ROOT_INSTEAD_OF_MODEL" not in codes

    def test_does_not_fire_when_root_href_ok_true_modeli(self):
        """root_href_ok=True also suppresses for Модели segment."""
        counts = {
            "adgroups": 2, "ads": 6,
            "adaptive_images_read": True, "root_href_ads_read": True,
            "root_href_ads_count": 6,
        }
        issues, _ = verify_grid_content(
            name="tp5_cpc_site — Товарная галерея — Модели",
            campaign_id=200, counts=counts,
            expected={"root_href_ok": True},
        )
        codes = [i["code"] for i in issues]
        assert "AD_HREF_ROOT_INSTEAD_OF_MODEL" not in codes

    def test_fires_when_root_href_ok_false(self):
        """root_href_ok=False (явно False) — детектор работает штатно."""
        counts = {
            "adgroups": 1, "ads": 3,
            "adaptive_images_read": True, "root_href_ads_read": True,
            "root_href_ads_count": 3,
        }
        issues, _ = verify_grid_content(
            name="tp5_cpc_site — Товарная галерея — Марки",
            campaign_id=100, counts=counts,
            expected={"root_href_ok": False},
        )
        codes = [i["code"] for i in issues]
        assert "AD_HREF_ROOT_INSTEAD_OF_MODEL" in codes

    def test_fires_when_root_href_ok_absent(self):
        """root_href_ok отсутствует в expected (None → falsy) → детектор работает."""
        counts = {
            "adgroups": 1, "ads": 3,
            "adaptive_images_read": True, "root_href_ads_read": True,
            "root_href_ads_count": 2,
        }
        issues, _ = verify_grid_content(
            name="tp5_cpc_site — Товарная галерея — Марки",
            campaign_id=100, counts=counts,
            expected={},  # root_href_ok not set
        )
        codes = [i["code"] for i in issues]
        assert "AD_HREF_ROOT_INSTEAD_OF_MODEL" in codes

    def test_live_path_never_sets_root_href_ok(self):
        """Интеграция: live_verifier → verify_grid_content НЕ передаёт root_href_ok.

        root_href_ok не выводится из доступного контекста (_build содержит только счётчики,
        не отдельные href групп; имя кампании не различает «баг» и «заглушку без модельных
        страниц»). Детектор всегда срабатывает через live-путь при наличии root_href_ads_count.
        Это ожидаемое поведение — false suppression опаснее false positive.
        """
        cid = 999
        counts_by_id = {
            cid: {
                "adgroups": 1, "ads": 3,
                "adaptive_images_read": True,
                "root_href_ads_read": True,
                "root_href_ads_count": 2,
            }
        }
        results = [{"name": "tp1_cpa_site — Марки", "id": cid, "ok": True, "kind": "tp1"}]
        report = verify_live_create_set(
            login="test-login",
            results=results,
            grid_content_counts=counts_by_id,
        )
        codes = [i.get("code") for i in report.get("issues", [])]
        assert "AD_HREF_ROOT_INSTEAD_OF_MODEL" in codes, (
            "Детектор должен сработать через live-путь: root_href_ok не выставляется "
            "автоматически (нет доступного сигнала), подавления нет."
        )


# ──────────────────────────────────────────────────────────────────────────────
# Detector 3: CPA_COUNT_PLAN_MISMATCH
# ──────────────────────────────────────────────────────────────────────────────

class TestCpaCountPlanMismatch:
    """verify_create_set fires CPA_COUNT_PLAN_MISMATCH when plan has CPA items but results don't."""

    def _run(self, items, results):
        return verify_create_set(login="test-login", items=items, results=results)

    def _cpc_item(self, name="tp5_cpc_site — X"):
        return {"name": name}

    def _cpa_item(self, name="tp5_cpa_site — X"):
        return {"name": name}

    def _cpc_result(self, name="tp5_cpc_site — X"):
        return {"name": name, "ok": True, "id": 111}

    def _cpa_result(self, name="tp5_cpa_site — X"):
        return {"name": name, "ok": True, "id": 222}

    def test_fires_when_plan_has_cpa_but_results_empty_on_cpa(self):
        items = [self._cpc_item(), self._cpa_item()]
        results = [self._cpc_result()]  # CPA item silently absent
        report = self._run(items, results)
        codes = [i["code"] for i in report["issues"]]
        assert "CPA_COUNT_PLAN_MISMATCH" in codes

    def test_error_severity(self):
        items = [self._cpa_item()]
        results = []
        report = self._run(items, results)
        hit = [i for i in report["issues"] if i["code"] == "CPA_COUNT_PLAN_MISMATCH"]
        assert hit and hit[0]["severity"] == "error"

    def test_planned_count_in_issue(self):
        items = [self._cpa_item("tp5_cpa_site — A"), self._cpa_item("tp5_cpa_site — B")]
        results = []
        report = self._run(items, results)
        hit = [i for i in report["issues"] if i["code"] == "CPA_COUNT_PLAN_MISMATCH"]
        assert hit and hit[0]["planned"] == 2
        assert hit[0]["processed"] == 0

    def test_does_not_fire_when_cpa_in_results(self):
        items = [self._cpc_item(), self._cpa_item()]
        results = [self._cpc_result(), self._cpa_result()]
        report = self._run(items, results)
        codes = [i["code"] for i in report["issues"]]
        assert "CPA_COUNT_PLAN_MISMATCH" not in codes

    def test_does_not_fire_when_cpa_in_results_as_failed(self):
        """CPA item in results as failed → still processed → no mismatch."""
        items = [self._cpa_item()]
        results = [{"name": "tp5_cpa_site — X", "ok": False, "error": "timeout"}]
        report = self._run(items, results)
        codes = [i["code"] for i in report["issues"]]
        assert "CPA_COUNT_PLAN_MISMATCH" not in codes

    def test_does_not_fire_when_no_cpa_items_in_plan(self):
        items = [self._cpc_item(), self._cpc_item("tp5_cpc_site — Y")]
        results = [self._cpc_result(), self._cpc_result("tp5_cpc_site — Y")]
        report = self._run(items, results)
        codes = [i["code"] for i in report["issues"]]
        assert "CPA_COUNT_PLAN_MISMATCH" not in codes

    def test_does_not_fire_on_empty_plan(self):
        report = self._run(items=[], results=[])
        codes = [i["code"] for i in report["issues"]]
        assert "CPA_COUNT_PLAN_MISMATCH" not in codes


# ──────────────────────────────────────────────────────────────────────────────
# WRONG_AUTOTARGET severity elevation
# ──────────────────────────────────────────────────────────────────────────────

class TestWrongAutotargetSeverityAlwaysError:
    """WRONG_AUTOTARGET must be error in BOTH in-job and delayed phases."""

    def _run_tp2(self, phase="in_job"):
        counts = {"adgroups": 5, "ads": 5,
                  "groups_edit_read": True, "wrong_autotarget_groups": 2}
        expected = {"phase": phase}
        issues, _ = verify_grid_content(
            name="tp2_cpc_site — Поиск — Общее",
            campaign_id=200, counts=counts, expected=expected
        )
        return [i for i in issues if i["code"] == "WRONG_AUTOTARGET"]

    def _run_tp1_rsya(self, phase="in_job"):
        counts = {"adgroups": 3, "ads": 3,
                  "groups_edit_read": True, "wrong_autotarget_rsya_groups": 2}
        expected = {"phase": phase}
        issues, _ = verify_grid_content(
            name="tp1_cpa_site — РСЯ — Общее",
            campaign_id=201, counts=counts, expected=expected
        )
        return [i for i in issues if i["code"] == "WRONG_AUTOTARGET"]

    def test_tp2_in_job_phase_is_error(self):
        hits = self._run_tp2(phase="in_job")
        assert hits and hits[0]["severity"] == "error"

    def test_tp2_delayed_phase_is_error(self):
        hits = self._run_tp2(phase="delayed")
        assert hits and hits[0]["severity"] == "error"

    def test_tp1_rsya_in_job_phase_is_error(self):
        hits = self._run_tp1_rsya(phase="in_job")
        assert hits and hits[0]["severity"] == "error"

    def test_tp1_rsya_delayed_phase_is_error(self):
        hits = self._run_tp1_rsya(phase="delayed")
        assert hits and hits[0]["severity"] == "error"

    def test_wrong_autotarget_has_repair_candidate(self):
        hits = self._run_tp2()
        assert hits  # repair checked separately via repair list
        counts = {"adgroups": 5, "ads": 5,
                  "groups_edit_read": True, "wrong_autotarget_groups": 2}
        _, repair = verify_grid_content(
            name="tp2_cpc_site — Поиск — Общее",
            campaign_id=200, counts=counts, expected={}
        )
        kinds = [r.get("kind") for r in repair]
        assert "keywords_repair" in kinds

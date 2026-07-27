import types

from flask import Flask

from direct import create_set_plan
from direct import create_set_structure
from direct import create_set_feeds
from direct import create_set_text_builders
from direct import create_set_tp1_builders
from direct import create_set_master_product
from direct import create_set_context
from direct import create_set_structure
from direct import create_set_content_preflight
from direct import campaign_spec_audit
from direct import city_morph
from direct import grid_content_verifier
from direct import kontent_pack
from direct import link_check
from direct import repair_gate
from direct import repair_planner
from direct import text_gen
from direct import uac_read
from direct import uac_verifier
from direct import verifier


def test_link_check_falls_back_from_single_segment_404_to_root(monkeypatch):
    seen = []

    def fake_status(url, timeout):
        seen.append(url)
        return 404 if url.endswith("/auto") else 200

    monkeypatch.setattr(link_check, "_http_status", fake_status)
    link_check._URL_CHECK_CACHE.clear()

    assert link_check.resolve_or_fallback_url("https://bucars-kuban.site/auto") == "https://bucars-kuban.site"
    assert seen == ["https://bucars-kuban.site/auto", "https://bucars-kuban.site"]


def test_tp7_product_filters_are_positive_only(monkeypatch):
    monkeypatch.setattr(create_set_feeds, "_resolve_feed_field", lambda *_args: "mark_id")
    monkeypatch.setattr(create_set_feeds, "_minus_marks_enabled", lambda: ["omoda"])
    monkeypatch.setattr(create_set_feeds, "_minus_models_enabled", lambda: ["RX9"])

    filters = create_set_feeds._tp7_product_feed_filters(
        "Haval", "ct0111", login="porg-test", feed_id=123
    )
    conditions = filters[0]["conditions"]

    assert any(c["field"] == "mark_id" and c["operator"] == "CONTAINS" for c in conditions)
    assert not any(c["field"] == "collectionId" for c in conditions)
    assert not any("NOT_CONTAINS" in c["operator"] for c in conditions)


def test_tp7_common_campaign_has_no_feed_filters(monkeypatch):
    monkeypatch.setattr(create_set_feeds, "_resolve_feed_field", lambda *_args: "mark_id")
    monkeypatch.setattr(create_set_feeds, "_minus_marks_enabled", lambda: ["omoda"])
    monkeypatch.setattr(create_set_feeds, "_minus_models_enabled", lambda: ["RX9"])

    assert create_set_feeds._tp7_product_feed_filters("", "ct0000", login="porg-test", feed_id=123) == []


def test_tp7_common_campaign_without_feed_filters_is_not_audit_issue():
    issues = campaign_spec_audit._audit_uac_feed_filters(
        "porg-test",
        123,
        "tp7_cpc_site_ct0000_aon_n000_r0002_ct010_ag001_g00 — ТК - Общие запросы - Автотаргетинг",
        None,
        detail={"feed_id": 456, "feed_filters": [], "ecom": True},
    )

    assert issues == []


def test_posevy_build_ads_are_not_checked_as_adaptive_text_ads():
    issues, repair = grid_content_verifier.verify_grid_content(
        "tp8_cpc_site_ct0000_aon_n000_r0002_ct018_ag011_g00 — Посевы Telegram",
        123,
        {
            "adgroups": 3,
            "ads": 3,
            "adaptive_total": 0,
            "adaptive_images_read": True,
        },
        {"build": {"adgroups": 3, "ads": 3}, "phase": "in_job"},
    )

    assert not any(i.get("code") == "BUILD_LIVE_TEXT_ADS_UNDERCOUNT" for i in issues)
    assert repair == []


def test_posevy_spec_audit_uses_posevy_structure(monkeypatch):
    monkeypatch.setitem(
        campaign_spec_audit._DEPS,
        "_struct_has_tp",
        lambda slepok, site_type, tp: (
            slepok == "posevy" and site_type == "Мультибренд" and tp in {"tp8", "tp9", "tp10"}
        ),
    )

    issues = campaign_spec_audit._audit_plan_vs_slepok(
        [
            {"id": 1, "name": "tp8_cpc_site_ct0000_aon_n000_r0002_ct018_ag011_g00 — Посевы Telegram"},
            {"id": 2, "name": "tp9_cpc_site_ct0000_aon_n000_r0002_ct018_ag011_g00 — Посевы Max"},
            {"id": 3, "name": "tp10_cpc_site_ct0000_aon_n000_r0002_ct018_ag011_g00 — Посевы Telegram+Max"},
        ],
        "pavlov",
        "Посевы",
    )

    assert not any(i.get("code") == "EXTRA_TP_NOT_IN_SLEPOK" for i in issues)


def test_verifier_rejects_plan_campaign_absent_from_slepok_structure(monkeypatch):
    monkeypatch.setattr(
        create_set_structure,
        "structure_to_campaigns",
        lambda agent, site_type, tp: [{"name": "Поиск - Марки - КС"}] if tp == "tp2" else [],
    )

    report = verifier.verify_create_set(
        login="porg-test",
        body={"agent": "scherbakova", "site_type": "Мультибренд", "counter_id": 1, "goal_id": 2},
        items=[{
            "type": "search_test",
            "tp": "tp2",
            "name": "tp2_cpc_site — Поиск - Модели - КС - Москва",
            "camp_key": "Поиск - Модели - КС",
        }],
        results=[],
    )

    assert any(x.get("code") == "ITEM_NOT_IN_SLEPOK_STRUCTURE" for x in report["issues"])


def test_structure_preflight_rejects_stale_create_payload(monkeypatch):
    monkeypatch.setattr(
        create_set_structure,
        "structure_to_campaigns",
        lambda agent, site_type, tp: [{"name": "Поиск - Марки - КС"}] if tp == "tp2" else [],
    )

    issues = verifier.structure_preflight_issues(
        [{
            "type": "search_test",
            "tp": "tp2",
            "name": "tp2_cpc_site — Поиск - Модели - КС - Москва",
            "camp_key": "Поиск - Модели - КС",
        }],
        {"agent": "scherbakova", "site_type": "Мультибренд"},
    )

    assert any(x.get("code") == "ITEM_NOT_IN_SLEPOK_STRUCTURE" for x in issues)


def test_structure_campaigns_follow_tree_not_raw_camp_names(monkeypatch):
    struct = {
        "directologists": [{
            "key": "scherbakova",
            "site_types": [{
                "name": "Мультибренд",
                "tp": [{
                    "code": "tp2",
                    "groups": [{"items": [
                        {
                            "t": "Haval",
                            "gc": "ct0101_aon_n000_r0000_ct010_ag001_g00",
                            "gk": "haval",
                            "camp_names": [
                                "Поиск - Марки - КС",
                                "Поиск - Марки - Автотаргетинг",
                                "Поиск - Марки - КС + Автотаргетинг",
                            ],
                        },
                        {
                            "t": "Haval M6",
                            "gc": "ct0201_aon_n000_r0000_ct010_ag001_g00",
                            "gk": "haval_m6",
                            "camp_names": [
                                "Поиск - Модели - Автотаргетинг",
                                "Поиск - Модели - КС",
                                "Поиск - Модели - КС + Автотаргетинг",
                            ],
                        },
                    ]}],
                }],
            }],
        }],
    }
    monkeypatch.setattr(create_set_structure, "_load_struct", lambda: struct)
    monkeypatch.setattr(create_set_structure, "_seg_map", lambda: {
        "ct0101": "Марки",
        "ct0201": "Модели",
    })
    monkeypatch.setattr(create_set_structure, "_non_auto", lambda: set())
    monkeypatch.setattr(
        create_set_structure,
        "_pack_targeting_for_group",
        lambda _slepok, _site, _tp, _ct, _gk, fallback: fallback,
    )

    camps = create_set_structure.structure_to_campaigns("scherbakova", "Мультибренд", "tp2")

    assert [c["name"] for c in camps] == [
        "Поиск - Марки - КС + Автотаргетинг",
        "Поиск - Модели - КС + Автотаргетинг",
    ]
    assert camps[0]["gks"] == ["haval"]
    assert camps[1]["gks"] == ["haval_m6"]


def test_group_href_uses_feed_url_when_campaign_href_is_quiz(monkeypatch):
    monkeypatch.setattr(create_set_text_builders, "_ct_segment", lambda _ct: "Модели", raising=False)
    monkeypatch.setattr(
        create_set_text_builders,
        "_feed_url_for_model",
        lambda _feed_urls, _brand, no_brand_fallback=False: (
            "https://newautos-193.site/auto/belgee/x70/i/suv-5d?utm_source=yandex"
        ),
        raising=False,
    )
    monkeypatch.setattr(create_set_text_builders, "_strip_url_query", lambda url: url.split("?", 1)[0], raising=False)

    href = create_set_text_builders._pack_group_href(
        "ct0201",
        "Belgee X70",
        "Belgee X70",
        {"belgee x70": "https://newautos-193.site/auto/belgee/x70/i/suv-5d"},
        "https://newautos-193.site/quiz",
        "Мультибренд",
    )

    assert href == "https://newautos-193.site/auto/belgee/x70/i/suv-5d"


def test_group_href_formula_fallback_strips_quiz_base(monkeypatch):
    monkeypatch.setattr(create_set_tp1_builders, "_ct_segment", lambda _ct: "Марки", raising=False)
    monkeypatch.setattr(create_set_tp1_builders, "_feed_url_for_model", lambda *_args, **_kwargs: "", raising=False)
    monkeypatch.setattr(
        create_set_tp1_builders,
        "_model_page_href",
        lambda base, _site_type, brand: base.rstrip("/") + "/auto/" + brand.lower(),
        raising=False,
    )

    href = create_set_tp1_builders._pack_group_href(
        "ct0101",
        "Haval",
        {},
        "https://newautos-193.site/quiz",
        "Мультибренд",
    )

    assert href == "https://newautos-193.site/auto/haval"


def test_x3_requires_registry_tag_not_hybrid_name():
    camp = {
        "name": "РСЯ - Марки - КС + Автотаргетинг",
        "tp": "tp1",
        "items": [],
    }

    assert create_set_structure.X3_TAG not in create_set_structure.detect_protected_tags(camp, None)
    assert create_set_structure.X3_TAG in create_set_structure.detect_protected_tags(
        camp,
        {create_set_structure.X3_TAG},
    )


def test_create_set_pack_gap_preflight_blocks_keyword_hybrid_before_create(monkeypatch):
    monkeypatch.setattr(
        kontent_pack,
        "read_keywords",
        lambda *_args, **_kwargs: {"positive": [], "minus": []},
    )

    note = create_set_content_preflight.create_set_pack_gap_note({
        "agent": "scherbakova",
        "site_type": "Мультибренд",
        "items": [{
            "type": "search_dynamic",
            "name": "tp4_cpc_site — Поиск + Динамика - Марки - КС + Автотаргетинг - Москва",
            "autotarget": True,
            "autotarget_keep_keywords": True,
            "only_cts": ["ct0179"],
            "only_gks": ["knewstar_001"],
        }],
    })

    assert "tp4/ct0179/knewstar_001" in note


def test_create_set_pack_gap_preflight_skips_pure_autotarget(monkeypatch):
    monkeypatch.setattr(
        kontent_pack,
        "read_keywords",
        lambda *_args, **_kwargs: {"positive": [], "minus": []},
    )

    note = create_set_content_preflight.create_set_pack_gap_note({
        "agent": "scherbakova",
        "site_type": "Мультибренд",
        "items": [{
            "type": "search_dynamic",
            "name": "tp4_cpc_site — Поиск + Динамика - Марки - Автотаргетинг - Москва",
            "autotarget": True,
            "autotarget_keep_keywords": False,
            "only_cts": ["ct0179"],
            "only_gks": ["knewstar_001"],
        }],
    })

    assert note == ""


def test_tp4_read_keywords_falls_back_to_exact_tp2_group(tmp_path, monkeypatch):
    monkeypatch.setattr(kontent_pack, "PACK_ROOT", str(tmp_path))
    kd = tmp_path / "Мультибренд" / "tp2" / "ct0179" / "keywords"
    kd.mkdir(parents=True)
    (kd / "scherbakova__knewstar_001.txt").write_text("купить knewstar\n---autotargeting\n", encoding="utf-8")

    kw = kontent_pack.read_keywords("Мультибренд", "tp4", "ct0179", "scherbakova", group="knewstar_001")

    assert "купить knewstar" in kw["positive"]


def test_tp2_read_keywords_falls_back_to_exact_tp1_group(tmp_path, monkeypatch):
    monkeypatch.setattr(kontent_pack, "PACK_ROOT", str(tmp_path))
    kd = tmp_path / "Мультибренд" / "tp1" / "ct0179" / "keywords"
    kd.mkdir(parents=True)
    (kd / "scherbakova__knewstar_001.txt").write_text("купить knewstar из рся\n", encoding="utf-8")

    kw = kontent_pack.read_keywords("Мультибренд", "tp2", "ct0179", "scherbakova", group="knewstar_001")

    assert kw["positive"] == ["купить knewstar из рся"]


def test_tp4_read_keywords_falls_back_to_tp1_when_tp2_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(kontent_pack, "PACK_ROOT", str(tmp_path))
    kd = tmp_path / "Мультибренд" / "tp1" / "ct0179" / "keywords"
    kd.mkdir(parents=True)
    (kd / "scherbakova__knewstar_001.txt").write_text("купить knewstar из рся\n", encoding="utf-8")

    kw = kontent_pack.read_keywords("Мультибренд", "tp4", "ct0179", "scherbakova", group="knewstar_001")

    assert kw["positive"] == ["купить knewstar из рся"]


def test_tp4_gather_merges_missing_groups_from_tp2(monkeypatch):
    def fake_gather_once(slepok, segment, tp, timeout=40.0):
        if tp == "tp4":
            return {"ct0179": {"positive": [], "minus": [], "callouts": [], "_groups": {}}}
        if tp == "tp1":
            return {}
        return {
            "ct0179": {
                "positive": [],
                "minus": [],
                "callouts": [],
                "_groups": {
                    "knewstar_001": {
                        "positive": ["купить knewstar"],
                        "minus": ["бесплатно"],
                        "callouts": ["В наличии"],
                    }
                },
            }
        }

    monkeypatch.setattr(kontent_pack, "_gather_once", fake_gather_once)

    pack = kontent_pack.gather("scherbakova", "Мультибренд", "tp4")

    assert pack["ct0179"]["_groups"]["knewstar_001"]["positive"] == ["купить knewstar"]


def test_tp2_gather_merges_missing_groups_from_tp1(monkeypatch):
    def fake_gather_once(slepok, segment, tp, timeout=40.0):
        if tp == "tp2":
            return {"ct0179": {"positive": [], "minus": [], "callouts": [], "_groups": {}}}
        return {
            "ct0179": {
                "positive": [],
                "minus": [],
                "callouts": [],
                "_groups": {
                    "knewstar_001": {
                        "positive": ["купить knewstar из рся"],
                        "minus": ["бесплатно"],
                        "callouts": ["В наличии"],
                    }
                },
            }
        }

    monkeypatch.setattr(kontent_pack, "_gather_once", fake_gather_once)

    pack = kontent_pack.gather("scherbakova", "Мультибренд", "tp2")

    assert pack["ct0179"]["_groups"]["knewstar_001"]["positive"] == ["купить knewstar из рся"]


def test_tp4_gather_merges_missing_groups_from_tp1_when_tp2_empty(monkeypatch):
    def fake_gather_once(slepok, segment, tp, timeout=40.0):
        if tp in ("tp4", "tp2"):
            return {"ct0179": {"positive": [], "minus": [], "callouts": [], "_groups": {}}}
        return {
            "ct0179": {
                "positive": [],
                "minus": [],
                "callouts": [],
                "_groups": {
                    "knewstar_001": {
                        "positive": ["купить knewstar из рся"],
                        "minus": ["бесплатно"],
                        "callouts": ["В наличии"],
                    }
                },
            }
        }

    monkeypatch.setattr(kontent_pack, "_gather_once", fake_gather_once)

    pack = kontent_pack.gather("scherbakova", "Мультибренд", "tp4")

    assert pack["ct0179"]["_groups"]["knewstar_001"]["positive"] == ["купить knewstar из рся"]


def test_uac_online_density_is_limited():
    items = [
        "Купить авто онлайн сегодня",
        "Решение банка онлайн за 30 минут",
        "Оформить кредит онлайн быстро",
    ]

    out = create_set_master_product._limit_online_density(items, 1)

    assert out[0] == "Купить авто онлайн сегодня"
    assert "онлайн" not in out[1].lower()
    assert "онлайн" not in out[2].lower()


def test_city_replace_does_not_touch_word_prefixes():
    out = city_morph._replace_foreign_city(
        ["Рассчитайте платёж по новому авто"],
        "Москва",
        {"расс"},
    )

    assert out == ["Рассчитайте платёж по новому авто"]


def test_city_replace_still_replaces_real_foreign_city():
    out = city_morph._replace_foreign_city(
        ["Автомобиль в Краснодаре по выгодной цене"],
        "Москва",
        {"краснодар"},
    )

    assert out == ["Автомобиль в Москве по выгодной цене"]


def test_uac_contents_count_images_and_videos_separately():
    summary = uac_read.summarize_uac_detail(
        {
            "contents": [
                {"type": "image"},
                {"type": "image"},
                {"type": "image"},
                {"type": "image"},
                {"type": "video"},
                {"type": "video"},
            ]
        }
    )

    assert summary["content"] == 6
    assert summary["images"] == 4
    assert summary["videos"] == 2


def test_uac_images_low_is_recreate_issue_and_video_does_not_count():
    issues, repair = uac_verifier.verify_uac_detail(
        "tp7_cpc_site_ct0111_aon_n000_r0002_ct010_ag001_g00 — ТК - Haval - КС + Автотаргетинг",
        123,
        {
            "status": "draft",
            "pricing": "PER_CLICK",
            "titles": 5,
            "texts": 3,
            "sitelinks": 8,
            "content": 6,
            "images": 4,
            "videos": 2,
            "week_limit": 1000,
            "limit_period": "week",
            "regions": 1,
            "counters": 1,
            "goals": 1,
            "has_tracking_params": True,
            "has_feed": True,
            "has_model_filter": True,
        },
    )

    codes = {issue["code"] for issue in issues}
    assert "UAC_IMAGES_LOW" in codes
    assert any(item["kind"] == "recreate_or_resume_campaign" for item in repair)
    assert "UAC_IMAGES_LOW" in repair_planner._RECREATE_CODES
    assert "UAC_IMAGES_LOW" in repair_gate._UAC_REPLACE_CODES


def test_feed_listings_query_declares_graphql_variables_with_comma():
    assert "$login:String!,$feedId:String!" in create_set_feeds._FEED_LISTINGS_QUERY


def test_tp67_targeting_labels_always_include_autotargeting():
    assert (
        create_set_context.tp67_targeting_label_from_modes(["audience"], "tp6")
        == "Аудитории + Автотаргетинг"
    )
    assert (
        create_set_context.tp67_targeting_label_from_modes(["keywords", "audience"], "tp7")
        == "КС + Аудитории + Автотаргетинг"
    )


def test_installment_keywords_are_kept_for_common_groups(monkeypatch):
    monkeypatch.setattr(text_gen, "_drop_used_car", lambda items, _site_type: list(items))
    monkeypatch.setattr(text_gen, "_drop_foreign_city_keywords", lambda items, _city: list(items))

    out = text_gen._filter_group_keywords(
        ["авто в рассрочку", "купить автомобиль в рассрочку"],
        "Общее",
        "",
        "Краснодар",
        "С пробегом",
    )

    assert "авто в рассрочку" in out
    assert "купить автомобиль в рассрочку" in out


def test_tp1_products_only_fallback_preserves_segments(monkeypatch):
    class Cursor:
        def execute(self, *_args, **_kwargs):
            pass

        def fetchone(self):
            return {
                "city": "Краснодар",
                "site_type": "С пробегом",
                "agency_account": "",
                "domain": "bucars-kuban.site",
            }

    class Connection:
        def cursor(self, *_args, **_kwargs):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(create_set_plan, "_victory_conn", lambda: Connection(), raising=False)
    monkeypatch.setattr(create_set_plan, "_resolve_region", lambda _city: ("r0088", "Краснодарский край"), raising=False)
    monkeypatch.setattr(create_set_plan, "_rule_sets", lambda *_args: {
        "cpa": 2000, "budget": 5000, "cpc_cpa": 2000, "cpc_budget": 5000,
    }, raising=False)
    monkeypatch.setattr(create_set_plan, "_token_for_login", lambda *_args: (None, None), raising=False)
    monkeypatch.setattr(create_set_plan, "_direct_tokens", lambda: [], raising=False)
    monkeypatch.setattr(create_set_plan, "_source_campaign_manifest", lambda *_args: None, raising=False)
    monkeypatch.setattr(create_set_plan, "_resolve_struct_site_type", lambda _a, site_type: site_type, raising=False)
    monkeypatch.setattr(create_set_plan, "_slepok_struct_groups", lambda *_args: [], raising=False)
    monkeypatch.setattr(create_set_structure, "structure_to_campaigns", lambda *_args: [], raising=False)
    monkeypatch.setattr(create_set_plan, "_tp1_plan_names", lambda *_args: [
        {"gc": "mark"}, {"gc": "model"}, {"gc": "common"},
    ], raising=False)
    monkeypatch.setattr(create_set_plan, "_ct_segment", lambda gc: {
        "mark": "Марки", "model": "Модели", "common": "Общее",
    }[gc], raising=False)
    monkeypatch.setattr(create_set_plan, "_tp_seg_modes", lambda *_args: None, raising=False)
    monkeypatch.setattr(create_set_plan, "_tp_seg_name_override", lambda *_args: None, raising=False)

    def slepok_modes(_agent, _site_type, _tp, segment):
        return ["Автотаргет"] if segment in ("Смарт-Баннер", "Фиды") else ["КС"]

    monkeypatch.setattr(create_set_plan, "_slepok_tp_modes", slepok_modes, raising=False)

    app = Flask(__name__)
    with app.test_request_context("/", method="POST", json={
        "login": "porg-xjxpfxby",
        "agent": "scherbakova",
        "site_type": "С пробегом",
        "variants": ["tp1_rsy"],
    }):
        response = create_set_plan._set_plan_response()
        data = response.get_json()

    products_only = [item for item in data["plan"] if item.get("products_only")]

    assert len(products_only) == 6
    assert {item["tp1_segment"] for item in products_only} == {"Марки", "Модели", "Общее"}
    assert all(" - None -" not in item["name"] for item in products_only)


# ── tp1 РСЯ: инверсия автотаргетинга (Phase 1.5 fix) ────────────────────────────────────────────

def _make_tp1_test_deps():
    """Минимальный набор deps для тестирования _build_tp1_adgroups (фокус: Phase 1.5 + Phase 2).

    v5 adgroups.add возвращает Id=1001 для первой группы.
    v5 keywords.add возвращает Id=2001 для каждого ключа.
    Grid update_unified_adgroups захватывает вызовы в update_calls[].
    keywords.add захватывает все тела запросов в kw_calls[].
    """
    update_calls = []
    kw_calls = []

    class _FakeGridClient:
        def update_unified_adgroups(self, items):
            update_calls.append(items)
            return [int(it["adGroupId"]) for it in items]

    gf_mock = types.SimpleNamespace(
        get_grid_client=lambda login, cookie=None: _FakeGridClient(),
    )

    def fake_v5_call(svc, method, token, login, body):
        if svc == "adgroups" and method == "add":
            groups_body = body.get("AdGroups", [])
            return {"result": {"AddResults": [{"Id": 1001 + i} for i in range(len(groups_body))]}}
        if svc == "keywords" and method == "add":
            kw_calls.append(body.get("Keywords", []))
            kws = body.get("Keywords", [])
            return {"result": {"AddResults": [{"Id": 2001 + i} for i in range(len(kws))]}}
        return {"result": {"AddResults": []}}

    deps = {
        "_v5_call": fake_v5_call,
        "_v5_err": lambda r: "",
        "gf": gf_mock,
        "gc": None,
        "_AUTOTARGET_KW": "---autotargeting",
        "_kw_clean": lambda kws, limit: [k for k in (kws or []) if k][:limit],
        "_chunks": lambda lst, n: ([lst[i:i + n] for i in range(0, len(lst), n)] if lst else []),
        "_AC_CHUNK_AG": 50,
        "_AC_CHUNK_KW": 200,
        "_AC_CHUNK_AD": 100,
        "_AC_BATCH_SLEEP": 0,
        "_AC_GROUP_CAP": 500,
        "_UTM_TEMPLATE_TP1": "utm_source=yandex",
        "_responsive_ad": lambda *_a, **_kw: None,   # пропустить Phase 3 (ads)
        "_rsya_titles": lambda *_a, **_kw: [],
        "_rsya_texts": lambda *_a, **_kw: [],
        "_feed_url_for_model": lambda *_a, **_kw: None,
        "_brand_level_url": lambda u: u,
        "_strip_url_query": lambda u: u,
        "_model_page_href": lambda *_a, **_kw: "",
        "_ct_segment": lambda ct: ct,
        "_apply_global_feed_minus_for_site": lambda st: False,
        "cmc": types.SimpleNamespace(UTM_TEMPLATE="utm_source=yandex"),
    }
    return deps, update_calls, kw_calls


def _run_tp1(autotarget: bool, keep_keywords: bool = False, keywords=None):  # list | None — py3.10+
    """Запустить _build_tp1_adgroups с одной группой, вернуть (rep, update_calls, kw_calls)."""
    deps, update_calls, kw_calls = _make_tp1_test_deps()
    create_set_tp1_builders.configure(deps)

    groups = [{"name": "тест-группа", "keywords": keywords or [], "titles": [], "texts": []}]
    rep = create_set_tp1_builders._build_tp1_adgroups(
        token="tok",
        login="porg-test",
        campaign_id=999,
        region_ids=[213],
        href="https://example.com",
        groups=groups,
        autotarget=autotarget,
        keep_keywords=keep_keywords,
        tp_code="tp1",
    )
    return rep, update_calls, kw_calls


def test_tp1_aon_phase15_sets_isactive_true():
    """Случай A: autotarget=True → Phase 1.5 должна задать isActive=True (не ВЫКЛ)."""
    _rep, update_calls, _kw = _run_tp1(autotarget=True)

    assert update_calls, "Phase 1.5: update_unified_adgroups не вызвана"
    rm = update_calls[0][0]["relevanceMatch"]
    assert rm["isActive"] is True, f"aon → isActive должен быть True, получили {rm['isActive']}"


def test_tp1_aoff_phase15_sets_isactive_false():
    """Случай B: autotarget=False → Phase 1.5 должна задать isActive=False (не ВКЛ-дефолт)."""
    _rep, update_calls, _kw = _run_tp1(autotarget=False, keywords=["купить авто"])

    assert update_calls, "Phase 1.5: update_unified_adgroups не вызвана"
    rm = update_calls[0][0]["relevanceMatch"]
    assert rm["isActive"] is False, f"aoff → isActive должен быть False, получили {rm['isActive']}"


def test_tp1_aon_no_autotarget_pseudokey_in_keywords():
    """Псевдоключ '---autotargeting' больше НЕ должен уходить в keywords.add."""
    _rep, _upd, kw_calls = _run_tp1(autotarget=True)

    all_kw_texts = [kw["Keyword"] for batch in kw_calls for kw in batch]
    assert "---autotargeting" not in all_kw_texts, (
        f"Псевдоключ всё ещё в keywords.add: {all_kw_texts}"
    )


def test_tp1_aon_keep_keywords_preserves_real_keywords():
    """autotarget=True + keep_keywords=True → реальные ключи сохраняются (нет регрессии)."""
    real_kws = ["купить авто", "новый автомобиль"]
    _rep, _upd, kw_calls = _run_tp1(autotarget=True, keep_keywords=True, keywords=real_kws)

    all_kw_texts = [kw["Keyword"] for batch in kw_calls for kw in batch]
    assert "купить авто" in all_kw_texts, "Реальный ключ 'купить авто' не попал в keywords.add"
    assert "новый автомобиль" in all_kw_texts, "Реальный ключ 'новый автомобиль' не попал в keywords.add"
    assert "---autotargeting" not in all_kw_texts, "Псевдоключ всё ещё в keywords.add"


def test_tp7_listing_plus_filter_uses_equals_not_contains(monkeypatch):
    """_tp7_listings_plus_filter возвращает EQUALS+values+value (эталон: HAR entry 59,
    PATCH /web-api/uac/campaign/713081184). CONTAINS без values — не рабочий вариант.
    """
    mark_col = {"id": "mark_6", "name": "Haval", "offers": 42}
    monkeypatch.setattr(create_set_feeds, "_feed_collections",
                        lambda *_args, **_kw: [mark_col])
    monkeypatch.setattr(create_set_feeds, "_brand_level_collection_id",
                        lambda base, cols: "mark_6")
    monkeypatch.setattr(create_set_feeds, "_coder_name_real_brand",
                        lambda name: True)

    result = create_set_feeds._tp7_listing_plus_filter(
        "porg-test", 3593963, "Haval", "ct0006", ""
    )

    assert result, "Функция вернула пустой список — фильтр не построен"
    conditions = result[0]["conditions"]
    assert len(conditions) == 1, f"Ожидалось 1 условие, получили {len(conditions)}"
    cond = conditions[0]

    # Оператор — строго EQUALS (не CONTAINS, не EQUALS_ANY)
    assert cond["operator"] == "EQUALS", (
        f"operator должен быть 'EQUALS', получили '{cond['operator']}'"
    )
    # values — обычный массив (не JSON-строка)
    assert "values" in cond, "Поле 'values' отсутствует в условии"
    assert cond["values"] == ["mark_6"], (
        f"values должен быть ['mark_6'], получили {cond['values']}"
    )
    # value — JSON-строка (обратная совместимость)
    assert "value" in cond, "Поле 'value' отсутствует в условии"
    import json as _json
    assert _json.loads(cond["value"]) == ["mark_6"], (
        f"value должен декодироваться в ['mark_6'], получили '{cond['value']}'"
    )
    # field — collectionId
    assert cond["field"] == "collectionId", (
        f"field должен быть 'collectionId', получили '{cond['field']}'"
    )

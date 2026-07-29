import types

from flask import Flask

from direct.create import create_set_plan
from direct.create import create_set_structure
from direct.create import create_set_feeds
from direct.create import create_set_text_builders
from direct.create import create_set_tp1_builders
from direct.create import create_set_master_product
from direct.create import create_set_assets
from direct.create import create_set_context
from direct.create import create_set_structure
from direct.create import create_set_content_preflight
from direct import campaign_spec_audit
from direct import city_morph
from direct import grid_content_verifier
from direct import kontent_pack
from direct import link_check
from direct.repair import repair_gate
from direct.repair import repair_planner
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


def test_is_degenerate_feed_url_table():
    from direct import model_urls

    # Вырожденные: пусто, корень домена, /quiz в любом виде
    assert model_urls._is_degenerate_feed_url("") is True
    assert model_urls._is_degenerate_feed_url("https://newautos-193.site") is True
    assert model_urls._is_degenerate_feed_url("https://newautos-193.site/") is True
    assert model_urls._is_degenerate_feed_url("https://newautos-193.site/?utm=x") is True
    assert model_urls._is_degenerate_feed_url(
        "https://newautos-193.site/quiz?fid=95713#x7-kunlun-i-suv-5d") is True
    assert model_urls._is_degenerate_feed_url("https://newautos-193.site/QUIZ/step-2") is True
    # схема в верхнем регистре: strip_quiz_url её нормализует и схлопнет URL в корень — гард обязан ловить
    assert model_urls._is_degenerate_feed_url("HTTPS://NEWAUTOS-193.SITE/quiz?fid=1") is True
    assert model_urls._is_degenerate_feed_url("HTTPS://NEWAUTOS-193.SITE/") is True
    # Нормальные фид-URL: гард молчит
    assert model_urls._is_degenerate_feed_url("https://newautos-193.site/auto/kaiyi") is False
    assert model_urls._is_degenerate_feed_url(
        "https://newautos-193.site/auto/belgee/x70/i/suv-5d?fid=1") is False
    assert model_urls._is_degenerate_feed_url("https://newautos-193.site/auto") is False


def _patch_text_builder_urls(monkeypatch, segment, feed_url):
    monkeypatch.setattr(create_set_text_builders, "_ct_segment", lambda _ct: segment, raising=False)
    monkeypatch.setattr(
        create_set_text_builders,
        "_feed_url_for_model",
        lambda *_a, **_kw: feed_url,
        raising=False,
    )
    monkeypatch.setattr(create_set_text_builders, "_strip_url_query",
                        lambda url: url.split("?", 1)[0], raising=False)
    monkeypatch.setattr(create_set_text_builders, "_brand_level_url",
                        lambda url: url, raising=False)
    monkeypatch.setattr(
        create_set_text_builders,
        "_model_page_href",
        lambda base, _site_type, name: base.rstrip("/") + "/auto/" + name.lower().replace(" ", "-"),
        raising=False,
    )


def test_tp2_group_href_ignores_quiz_only_feed_offer(monkeypatch):
    """Kaiyi: единственный оффер аккаунта — из квиз-фида → href обязан быть страницей марки."""
    _patch_text_builder_urls(monkeypatch, "Марки",
                             "https://newautos-193.site/quiz?fid=95713#x7-kunlun-i-suv-5d")

    href = create_set_text_builders._pack_group_href(
        "ct0154", "KAIYI", "KAIYI", {"kaiyi": "https://newautos-193.site/quiz?fid=95713"},
        "https://newautos-193.site", "Мультибренд",
    )

    assert href == "https://newautos-193.site/auto/kaiyi"


def test_tp2_group_href_keeps_catalog_feed_offer(monkeypatch):
    """Каталог-фид (не квиз) — поведение прежнее, гард не вмешивается."""
    _patch_text_builder_urls(monkeypatch, "Модели",
                             "https://newautos-193.site/auto/belgee/x70/i/suv-5d?fid=1")

    href = create_set_text_builders._pack_group_href(
        "ct0201", "Belgee X70", "Belgee X70", {"belgee x70": "x"},
        "https://newautos-193.site", "Мультибренд",
    )

    assert href == "https://newautos-193.site/auto/belgee/x70/i/suv-5d"


def test_tp2_group_href_keeps_site_root_for_non_brand_group(monkeypatch):
    """BUTTON_404_GENERIC_AVTO: тема, а не марка → формулу НЕ зовём, остаётся корень сайта.

    Достижимый случай tp2/tp4 (`_multi`): реальный ct с не-брендовым структурным именем
    («Автокредит Купить Авто В Автокредит», «Авито», «Авто»). `_valid_pack_brand_name` такое имя
    отвергает → `real_brand` пуст. Формула по теме дала бы `/auto/avtokredit-...` / `/auto/avto`
    (404 → `_parent_path` → brandless `/auto` + лишние HEAD-запросы), поэтому она запрещена.
    """
    _patch_text_builder_urls(monkeypatch, "Марки", "https://newautos-193.site/quiz?fid=1")
    _formula_calls: list[str] = []
    monkeypatch.setattr(
        create_set_text_builders,
        "_model_page_href",
        lambda base, _site_type, name: _formula_calls.append(name) or (base.rstrip("/") + "/auto/x"),
        raising=False,
    )

    for _name in ("Автокредит Купить Авто В Автокредит", "Авто"):
        # real_brand="" — ровно то, что вернул _valid_pack_brand_name для такого имени (ct реальный,
        # не ct0000: в tp2/tp4 ct0000 отфильтрован в _struct_cts).
        href = create_set_text_builders._pack_group_href(
            "ct0031", _name, "", {"x": "y"}, "https://newautos-193.site", "Мультибренд",
        )
        assert href == "https://newautos-193.site"

    assert _formula_calls == []


def test_tp1_non_multi_group_href_ignores_quiz_feed_offer(monkeypatch):
    """tp1/tp5 без _multi: обход `_multi and _uname` не работает → гард обязан отработать сам."""
    monkeypatch.setattr(create_set_tp1_builders, "_ct_segment", lambda _ct: "Марки", raising=False)
    monkeypatch.setattr(create_set_tp1_builders, "_feed_url_for_model",
                        lambda *_a, **_kw: "https://newautos-193.site/quiz?fid=1", raising=False)
    monkeypatch.setattr(create_set_tp1_builders, "_brand_level_url", lambda url: url, raising=False)
    monkeypatch.setattr(
        create_set_tp1_builders,
        "_model_page_href",
        lambda base, _site_type, brand: base.rstrip("/") + "/auto/" + brand.lower(),
        raising=False,
    )

    href = create_set_tp1_builders._pack_group_href(
        "ct0154", "Kaiyi", {"kaiyi": "https://newautos-193.site/quiz?fid=1"},
        "https://newautos-193.site", "Мультибренд",
    )

    assert href == "https://newautos-193.site/auto/kaiyi"


def test_master_product_feed_branch_has_quiz_guard():
    """tp6/tp7 (`run_master_product_item`) — фид-first ветка закрыта тем же общим гардом.

    Проверка по исходнику: функция DI-тяжёлая и целиком в unit-тесте не поднимается.
    """
    import inspect

    src = inspect.getsource(create_set_master_product.run_master_product_item)
    assert "if _raw_feed_url and not _is_degenerate_feed_url(_raw_feed_url):" in src


def test_uaz_formula_fallback_to_auto_survives_guard(monkeypatch):
    """UAZ нет в фидах: формула /auto/uaz → 404 → /auto (легальный фолбэк), гард не мешает."""
    monkeypatch.setattr(create_set_text_builders, "_ct_segment", lambda _ct: "Марки", raising=False)
    monkeypatch.setattr(create_set_text_builders, "_feed_url_for_model",
                        lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(
        create_set_text_builders,
        "_model_page_href",
        lambda base, _site_type, name: base.rstrip("/") + "/auto/" + name.lower(),
        raising=False,
    )

    raw = create_set_text_builders._pack_group_href(
        "ct0256", "UAZ", "UAZ", {}, "https://newautos-193.site", "Мультибренд",
    )
    assert raw == "https://newautos-193.site/auto/uaz"

    monkeypatch.setattr(link_check, "_http_status",
                        lambda url, timeout: 404 if url.endswith("/auto/uaz") else 200)
    link_check._URL_CHECK_CACHE.clear()
    assert link_check.resolve_or_fallback_url(raw) == "https://newautos-193.site/auto"


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


# ── tp1 РСЯ: автотаргет ставится АТОМАРНО при создании группы (Grid AddUnifiedAdGroups) ────────

def _make_tp1_test_deps(responsive: bool = False):
    """Минимальный набор deps для _build_tp1_adgroups (фокус: Фаза 1 Grid + Фаза 2 ключи).

    `gc` — РЕАЛЬНЫЕ payload-фабрики grid_create (build_adgroup/unique_keyword_ids), чтобы тест
    проверял фактический payload, а не свою копию. Подменён только транспорт GridCreateClient:
    add_adgroups пишет отправленные items в grid_calls[], add_keywords — в kw_calls[].
    responsive=True → `_responsive_ad` отдаёт словарь (Фаза 3 доходит до ads.add), иначе None.
    """
    from direct import grid_create as _real_gc

    grid_calls = []
    kw_calls = []
    ad_calls = []

    class _FakeGridCreateClient:
        def __init__(self, login, cookie=None, **_kw):
            self.login = login

        def add_adgroups(self, items, **_kw):   # **_kw: реальный клиент принимает campaign_is_new
            grid_calls.append(items)
            return [1001 + i for i in range(len(items))]

        def add_keywords(self, items):
            kw_calls.append(items)
            # Живой Grid сам режет служебные "---" фразы (grid_create.add_keywords:234).
            return [{"id": 2001 + i, "adGroupId": it["adGroupId"]}
                    for i, it in enumerate(items)
                    if not str(it.get("keyword") or "").startswith("---")]

        def _read_adgroup_name_to_id(self, campaign_id):  # noqa: ARG002
            return {}

    gc_mock = types.SimpleNamespace(
        GridCreateClient=_FakeGridCreateClient,
        GridCreateError=_real_gc.GridCreateError,
        build_adgroup=_real_gc.build_adgroup,
        unique_keyword_ids=_real_gc.unique_keyword_ids,
    )

    class _FakeGridClient:
        def upload_image(self, *_a, **_kw):
            return None

    gf_mock = types.SimpleNamespace(
        get_grid_client=lambda login, cookie=None: _FakeGridClient(),
    )

    def fake_v501_svc(svc, method, token, login, body):
        if svc == "ads" and method == "add":
            ads = body.get("Ads", [])
            ad_calls.append(ads)
            return {"result": {"AddResults": [{"Id": 3001 + i} for i in range(len(ads))]}}
        return {"result": {"AddResults": []}}

    deps = {
        "_v5_call": lambda *_a, **_kw: {"result": {"AddResults": []}},
        "_v501_svc": fake_v501_svc,
        "_v5_err": lambda r: "",
        "gf": gf_mock,
        "gc": gc_mock,
        "_AUTOTARGET_KW": "---autotargeting",
        "_kw_clean": lambda kws, limit: [k for k in (kws or []) if k][:limit],
        "_chunks": lambda lst, n: ([lst[i:i + n] for i in range(0, len(lst), n)] if lst else []),
        "_AC_CHUNK_AG": 50,
        "_AC_CHUNK_KW": 200,
        "_AC_CHUNK_AD": 100,
        "_AC_BATCH_SLEEP": 0,
        "_AC_GROUP_CAP": 500,
        "_UTM_TEMPLATE_TP1": "utm_source=yandex",
        "_responsive_ad": ((lambda titles, texts, href, **_kw: {
            "Titles": ["з"], "Texts": ["т"], "Href": href})
            if responsive else (lambda *_a, **_kw: None)),
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
    return deps, grid_calls, kw_calls, ad_calls


def _run_tp1(autotarget: bool, keep_keywords: bool = False, keywords=None, tp_code: str = "tp1"):
    """Запустить _build_tp1_adgroups с одной группой, вернуть (rep, grid_calls, kw_calls)."""
    deps, grid_calls, kw_calls, _ads = _make_tp1_test_deps()
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
        tp_code=tp_code,
    )
    return rep, grid_calls, kw_calls


def test_tp1_aon_grid_creates_group_with_active_relevance_match():
    """Случай A: autotarget=True → relevanceMatch.isActive=True уходит В САМОМ AddUnifiedAdGroups."""
    rep, grid_calls, _kw = _run_tp1(autotarget=True)

    assert grid_calls, "Фаза 1: Grid add_adgroups не вызвана (tp1 всё ещё на v501?)"
    rm = grid_calls[0][0]["relevanceMatch"]
    assert rm["isActive"] is True, f"aon → isActive должен быть True, получили {rm['isActive']}"
    assert "EXACT_V2_MARK" in rm["relevanceMatchCategories"]
    assert rep.get("relevance_match_set") == rep.get("adgroups") == 1


def test_tp1_aoff_grid_creates_group_with_inactive_relevance_match():
    """Случай B: autotarget=False → relevanceMatch.isActive=False при создании (не дефолт Яндекса)."""
    _rep, grid_calls, _kw = _run_tp1(autotarget=False, keywords=["купить авто"])

    assert grid_calls, "Фаза 1: Grid add_adgroups не вызвана"
    rm = grid_calls[0][0]["relevanceMatch"]
    assert rm["isActive"] is False, f"aoff → isActive должен быть False, получили {rm['isActive']}"
    assert rm["relevanceMatchCategories"] == []
    assert rm["autotargetingBrandSettings"] == []


def test_tp1_relevance_match_isactive_equals_autotarget_flag():
    """`relevanceMatch.isActive` в payload создания == bool(autotarget) для обоих режимов."""
    for flag in (True, False):
        _rep, grid_calls, _kw = _run_tp1(autotarget=flag, keep_keywords=True,
                                         keywords=["купить авто"])
        assert grid_calls[0][0]["relevanceMatch"]["isActive"] is flag


def test_tp1_aon_sends_no_keywords_and_no_pseudokey():
    """Чистый автотаргет: реальных ключей нет, псевдоключ '---autotargeting' не шлётся."""
    _rep, _grid, kw_calls = _run_tp1(autotarget=True, keywords=["купить авто"])

    all_kw = [it["keyword"] for batch in kw_calls for it in batch]
    assert all_kw == [], f"у aon-группы не должно появляться ключей, получили {all_kw}"
    assert "---autotargeting" not in all_kw, "Псевдоключ всё ещё уходит в AddKeywords"


def test_tp1_aoff_keeps_real_keywords():
    """Чистый КС: реальные ключи не пропадают при переходе на Grid-транспорт."""
    rep, _grid, kw_calls = _run_tp1(autotarget=False, keywords=["купить авто", "новый автомобиль"])

    all_kw = [it["keyword"] for batch in kw_calls for it in batch]
    assert all_kw == ["купить авто", "новый автомобиль"], f"ключи потерялись: {all_kw}"
    assert all(str(it["adGroupId"]) == "1001" for batch in kw_calls for it in batch)
    assert rep["keywords"] == 2


def test_tp1_aon_keep_keywords_preserves_real_keywords():
    """autotarget=True + keep_keywords=True → реальные ключи сохраняются (нет регрессии)."""
    real_kws = ["купить авто", "новый автомобиль"]
    _rep, _grid, kw_calls = _run_tp1(autotarget=True, keep_keywords=True, keywords=real_kws)

    all_kw = [it["keyword"] for batch in kw_calls for it in batch]
    assert "купить авто" in all_kw, "Реальный ключ 'купить авто' не попал в AddKeywords"
    assert "новый автомобиль" in all_kw, "Реальный ключ 'новый автомобиль' не попал в AddKeywords"
    assert "---autotargeting" not in all_kw, "Псевдоключ всё ещё уходит в AddKeywords"


def test_tp1_grid_adgroups_failure_leaves_zero_adgroups():
    """Сбой Grid-создания групп → adgroups=0 → вызывающий (:1531) снесёт черновик.

    Раньше эту роль играл rep['error'] от Phase 1.5; теперь автотаргет неотделим от создания:
    группы нет → и неверного isActive быть не может.
    """
    from direct import grid_create as _real_gc

    deps, _grid_calls, _kw_calls, _ads = _make_tp1_test_deps()

    class _FailingGridCreateClient:
        def __init__(self, login, cookie=None, **_kw):
            self.login = login

        def add_adgroups(self, items, **_kw):  # noqa: ARG002
            raise _real_gc.GridCreateError("AddUnifiedAdGroups validation: сбой")

    deps["gc"] = types.SimpleNamespace(
        GridCreateClient=_FailingGridCreateClient,
        GridCreateError=_real_gc.GridCreateError,
        build_adgroup=_real_gc.build_adgroup,
        unique_keyword_ids=_real_gc.unique_keyword_ids,
    )
    create_set_tp1_builders.configure(deps)

    groups = [{"name": "тест-группа", "keywords": [], "titles": [], "texts": []}]
    rep = create_set_tp1_builders._build_tp1_adgroups(
        token="tok",
        login="porg-test",
        campaign_id=999,
        region_ids=[213],
        href="https://example.com",
        groups=groups,
        autotarget=False,
        keep_keywords=False,
        tp_code="tp1",
    )

    assert not rep.get("adgroups"), "при сбое Grid групп быть не должно"
    assert any("adgroups(Grid tp1)" in str(e) for e in (rep.get("errors") or [])), (
        f"причина сбоя должна быть в rep['errors'], получили {rep.get('errors')!r}"
    )


def test_tp1_sitelink_sets_created_by_single_batch_call():
    """Пре-пасс быстрых ссылок: N групп → ОДИН батч-вызов, id не перепутаны по содержимому."""
    deps, _grid, _kw, ad_calls = _make_tp1_test_deps(responsive=True)
    batch_calls = []
    single_calls = []

    def fake_batch(token, login, sets, warns=None):  # noqa: ARG001
        batch_calls.append(sets)
        return [7001 + i for i in range(len(sets))]

    deps["_get_or_reuse_sitelink_sets"] = fake_batch
    deps["_get_or_reuse_sitelink_set"] = (
        lambda token, login, sls, warns=None: single_calls.append(sls))  # noqa: ARG005
    create_set_tp1_builders.configure(deps)

    groups = [
        {"name": "g1", "keywords": [], "href": "https://example.com/auto/lada"},
        {"name": "g2", "keywords": [], "href": "https://example.com/auto/lada"},
        {"name": "g3", "keywords": [], "href": "https://example.com/auto/kia"},
    ]
    create_set_tp1_builders._build_tp1_adgroups(
        token="tok", login="porg-test", campaign_id=999, region_ids=[213],
        href="https://example.com", groups=groups, autotarget=True,
        base_sitelinks=[{"Title": "Кредит", "Href": "https://example.com"}],
        tp_code="tp1",
    )

    assert len(batch_calls) == 1, f"ожидался один батч-вызов, получили {len(batch_calls)}"
    assert not single_calls, "поштучный _get_or_reuse_sitelink_set не должен вызываться"
    # одинаковый href двух групп → ОДИН набор в батче; разный href → свой набор
    assert len(batch_calls[0]) == 2, f"дедуп по href не сработал: {batch_calls[0]!r}"
    assert [s[0]["Href"] for s in batch_calls[0]] == [
        "https://example.com/auto/lada", "https://example.com/auto/kia"]
    ads = [a["ResponsiveAd"] for batch in ad_calls for a in batch]
    assert [a["SitelinkSetId"] for a in ads] == [7001, 7001, 7002], (
        f"id наборов раздались неверно: {[a.get('SitelinkSetId') for a in ads]}")


def test_tp1_sitelink_batch_failure_falls_back_to_single_calls():
    """Батч не отдал id → поштучный путь по-прежнему работает (наборы не теряются)."""
    deps, _grid, _kw, ad_calls = _make_tp1_test_deps(responsive=True)
    single_calls = []

    def fake_single(token, login, sls, warns=None):  # noqa: ARG001
        single_calls.append(sls)
        return 9100 + len(single_calls)

    deps["_get_or_reuse_sitelink_sets"] = lambda token, login, sets, warns=None: []  # noqa: ARG005
    deps["_get_or_reuse_sitelink_set"] = fake_single
    create_set_tp1_builders.configure(deps)

    groups = [{"name": "g1", "keywords": [], "href": "https://example.com/auto/lada"}]
    create_set_tp1_builders._build_tp1_adgroups(
        token="tok", login="porg-test", campaign_id=999, region_ids=[213],
        href="https://example.com", groups=groups, autotarget=True,
        base_sitelinks=[{"Title": "Кредит", "Href": "https://example.com"}],
        tp_code="tp1",
    )

    assert len(single_calls) == 1, "фолбэк на поштучное создание набора не сработал"
    ads = [a["ResponsiveAd"] for batch in ad_calls for a in batch]
    assert ads and ads[0]["SitelinkSetId"] == 9101


def test_tp5_autotarget_stays_on_regardless_of_plan_flag():
    """tp5 — поисковая кампания: автотаргет выключить нельзя, профиль search_tp2 идёт ВСЕГДА.

    Общий Grid-путь tp1/tp5 не должен «выключать» автотаргет у tp5 при плановом autotarget=False:
    живое isActive=True у таких групп — норма Директа, а не дефект (Семён, 2026-07-27).
    """
    for flag in (True, False):
        _rep, grid_calls, _kw = _run_tp1(autotarget=flag, keep_keywords=True,
                                         keywords=["купить авто"], tp_code="tp5")
        rm = grid_calls[0][0]["relevanceMatch"]
        assert rm["isActive"] is True, f"tp5 autotarget={flag} → isActive должен остаться True"
        assert rm["relevanceMatchCategories"] == ["EXACT_V2_MARK"]
        assert rm["autotargetingBrandSettings"] == ["WITHOUT_BRAND"]


def _run_tp1_with_silent_zero_keywords(tp_code: str):
    """Прогон с Grid-клиентом, чей add_keywords ТИХО отдаёт [] (валидатор отклонил пачку).

    grid_create.add_keywords:246-254 при validationResult.errors НЕ бросает — печатает в stderr
    и возвращает []. Раньше это давало rep['keywords']=0 при ПУСТОМ rep['errors'].
    """
    from direct import grid_create as _real_gc

    deps, _grid_calls, kw_calls, _ads = _make_tp1_test_deps()

    class _SilentZeroKwClient:
        def __init__(self, login, cookie=None, **_kw):
            self.login = login

        def add_adgroups(self, items, **_kw):   # **_kw: реальный клиент принимает campaign_is_new
            return [1001 + i for i in range(len(items))]

        def add_keywords(self, items):
            kw_calls.append(items)
            return []   # валидатор Grid отклонил всю пачку, исключения нет

        def _read_adgroup_name_to_id(self, campaign_id):  # noqa: ARG002
            return {}

    deps["gc"] = types.SimpleNamespace(
        GridCreateClient=_SilentZeroKwClient,
        GridCreateError=_real_gc.GridCreateError,
        build_adgroup=_real_gc.build_adgroup,
        unique_keyword_ids=_real_gc.unique_keyword_ids,
    )
    create_set_tp1_builders.configure(deps)

    groups = [{"name": "тест-группа", "keywords": ["купить авто"], "titles": [], "texts": []}]
    rep = create_set_tp1_builders._build_tp1_adgroups(
        token="tok", login="porg-test", campaign_id=999, region_ids=[213],
        href="https://example.com", groups=groups, autotarget=False,
        keep_keywords=False, tp_code=tp_code)
    return rep, kw_calls


def test_tp1_silent_zero_keywords_lands_in_errors():
    """kw_items>0, создано 0 ключей без исключения → причина обязана быть в rep['errors']."""
    rep, kw_calls = _run_tp1_with_silent_zero_keywords("tp1")

    assert kw_calls, "ключи не отправлялись вовсе — тест не проверяет гейт"
    assert rep["keywords"] == 0
    assert any("ключи(AddKeywords tp1)" in str(e) for e in (rep.get("errors") or [])), (
        f"тихий ноль ключей обязан попасть в rep['errors'], получили {rep.get('errors')!r}")


def test_tp5_silent_zero_keywords_is_fatal():
    """Тот же тихий ноль на поисковой tp5 → синтез singular error → кампания не считается ok."""
    rep, _kw_calls = _run_tp1_with_silent_zero_keywords("tp5")

    assert rep["keywords"] == 0
    assert any("ключи(AddKeywords tp5)" in str(e) for e in (rep.get("errors") or []))
    assert rep.get("error"), (
        f"tp5 без ключей — структурный провал, ожидался singular error; rep={rep!r}")
    assert "tp5 ключи" in rep["error"]


def test_tp1_all_feeds_group_relevance_match_follows_plan_flag():
    """Фаза 4a «Товарная галерея · <фид>» (tp1) создаётся Grid-ом с isActive == bool(autotarget).

    Раньше группа шла через v501 adgroups.add БЕЗ relevanceMatch → Директ ставил дефолт ACTIVE,
    в том числе в кампании планового `aoff`. Имя группы несёт кодер-префикс
    `ct0000_{aon|aoff}_n000_{r_code}_ct009_ag001_g00 — Товарная галерея · <фид>`
    (TP1_ALL_FEEDS_GALLERY_GROUPS_NO_CODER, ERRORS_JOURNAL.md) — так детектор grid_read.py:356-362
    видит isActive-токен `_aon_`/`_aoff_` и для этих групп.
    """
    for flag in (True, False):
        deps, grid_calls, _kw, _ads = _make_tp1_test_deps()
        v5_calls = []

        def _fake_v5_call(*a, **_kw):
            v5_calls.append(a)
            return {"result": {"AddResults": []}}

        deps["_v5_call"] = _fake_v5_call
        create_set_tp1_builders.configure(deps)

        groups = [{"name": "тест-группа", "keywords": [], "titles": [], "texts": []}]
        create_set_tp1_builders._build_tp1_adgroups(
            token="tok", login="porg-test", campaign_id=999, region_ids=[213],
            href="https://example.com", groups=groups, autotarget=flag,
            keep_keywords=False, tp_code="tp1", products_only=True,
            all_feeds_list=[(555, "Основной фид")], r_code="r0088")

        af_items = [it for call in grid_calls for it in call
                    if "Товарная галерея" in str(it.get("name") or "")]
        assert len(af_items) == 1, f"группа «все фиды» не создана Grid-ом: {grid_calls!r}"
        _expect_aud = "aon" if flag else "aoff"
        assert af_items[0]["name"] == (
            f"ct0000_{_expect_aud}_n000_r0088_ct009_ag001_g00 — Товарная галерея · Основной фид"
        ), f"кодер-префикс потерян: {af_items[0]['name']!r}"
        assert af_items[0]["relevanceMatch"]["isActive"] is flag, (
            f"autotarget={flag} → isActive должен быть {flag}, "
            f"получили {af_items[0]['relevanceMatch']['isActive']}")
        assert af_items[0].get("trackingParams"), "UTM группы потерян при переходе на Grid"
        assert not v5_calls, "v501 adgroups.add всё ещё используется для группы «все фиды»"


def test_tp5_group_name_keeps_aon_hardcode():
    """tp5+shopping всегда `_aon_`: это единственное корректное значение, а не хардкод-баг."""
    for flag in (True, False):
        name = create_set_tp1_builders._tp1_group_name(
            "tp5_cpc_site_ct0146", "r0002", "Haval", with_shopping=True,
            autotarget=flag, tp_code="tp5")
        assert "_aon_" in name and "_aoff_" not in name, name


def test_tp1_rsya_verifier_detects_aon_isactive_false():
    """Верификатор выдаёт WRONG_AUTOTARGET для tp1 группы с _aon_ в имени и isActive=False."""
    issues, repair = grid_content_verifier.verify_grid_content(
        "tp1_cpc_site_ct0146_aon_n000_r0002_ct001_ag011_g00 — Haval",
        713,
        {
            "adgroups": 1,
            "ads": 1,
            "groups_edit_read": True,
            "wrong_autotarget_rsya_groups": 1,  # aon+isActive=False — дефект
        },
        {"phase": "delayed"},
    )

    codes = [i.get("code") for i in issues]
    assert "WRONG_AUTOTARGET" in codes, (
        f"aon+isActive=False должен детектироваться как WRONG_AUTOTARGET, issues={issues!r}"
    )
    at_issue = next(i for i in issues if i.get("code") == "WRONG_AUTOTARGET")
    assert at_issue.get("severity") == "error", (
        f"В фазе delayed severity должен быть error, получили {at_issue.get('severity')!r}"
    )


def test_tp1_rsya_verifier_detects_aoff_isactive_true():
    """Верификатор выдаёт WRONG_AUTOTARGET для tp1 группы с _aoff_ в имени и isActive=True."""
    issues, repair = grid_content_verifier.verify_grid_content(
        "tp1_cpc_site_ct0301_aoff_n000_r0002_ct001_ag011_g00 — KIA",
        714,
        {
            "adgroups": 1,
            "ads": 1,
            "groups_edit_read": True,
            "wrong_autotarget_rsya_groups": 1,  # aoff+isActive=True — дефект
        },
        {"phase": "delayed"},
    )

    codes = [i.get("code") for i in issues]
    assert "WRONG_AUTOTARGET" in codes, (
        f"aoff+isActive=True должен детектироваться как WRONG_AUTOTARGET, issues={issues!r}"
    )


def test_tp1_rsya_verifier_no_false_positive_correct_combinations():
    """Верификатор молчит при корректных комбинациях aon+True и aoff+False."""
    for wrong_rsya in (0, None):
        issues, repair = grid_content_verifier.verify_grid_content(
            "tp1_cpc_site_ct0146_aon_n000_r0002_ct001_ag011_g00 — Haval",
            715,
            {
                "adgroups": 1,
                "ads": 1,
                "groups_edit_read": True,
                "wrong_autotarget_rsya_groups": wrong_rsya,
            },
            {"phase": "delayed"},
        )
        at_issues = [i for i in issues if i.get("code") == "WRONG_AUTOTARGET"]
        assert not at_issues, (
            f"wrong_autotarget_rsya_groups={wrong_rsya!r}: ложный WRONG_AUTOTARGET, issues={at_issues!r}"
        )


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


# ── ЧУЖАЯ МАРКА в ключах марочной группы (боевой факт 2026-07-28: 63 группы / 13 кампаний) ──
_FOREIGN_BRAND_CT_MAP = {
    "ct0238": "Volkswagen",
    "ct0181": "Lada",
    "ct0111": "Haval",
    "ct0300": "Автокредит",      # тема под марочным сегментом → маркой считаться не должна
    "ct0000": "Авто",
}


def _brand_guard_env(monkeypatch):
    """Оффлайн-окружение фильтра чужих марок: справочник ct + реальный _brand_canon (кир↔лат)."""
    monkeypatch.setattr(text_gen, "_drop_used_car", lambda items, _site_type: list(items))
    monkeypatch.setattr(text_gen, "_drop_foreign_city_keywords", lambda items, _city: list(items))
    monkeypatch.setattr(text_gen, "_ag_part1_map", lambda: dict(_FOREIGN_BRAND_CT_MAP))
    monkeypatch.setattr(text_gen, "_ct_segment",
                        lambda ct: "Общее" if ct in ("ct0000", "ct0300") else "Марки")
    monkeypatch.setattr(text_gen, "_brand_canon", create_set_feeds._brand_canon)
    monkeypatch.setattr(text_gen, "_BRAND_CANON_UNIVERSE_CACHE", None)


def _brand_group_keywords(kws):
    return text_gen._filter_group_keywords(kws, "Марки", "Volkswagen", "Краснодар", "С пробегом",
                                           model="Volkswagen")


def test_foreign_brand_keywords_dropped_from_brand_group(monkeypatch):
    _brand_guard_env(monkeypatch)
    out = _brand_group_keywords([
        "авто лада +с пробегом",          # чужая марка (кириллица) → дроп
        "авито вазы бу",                  # чужая марка через алиас ваз→lada + словоформа → дроп
        "lada granta купить",             # чужая марка латиницей → дроп
        "купить авто с пробегом",         # общая фраза без марки → остаётся
        "автосалон краснодар",            # общая фраза без марки → остаётся
        "фольксваген поло бу",            # СВОЯ марка кириллицей → остаётся
        "volkswagen купить",              # своя марка латиницей → остаётся
    ])
    assert "авто лада +с пробегом" not in out
    assert "авито вазы бу" not in out
    assert "lada granta купить" not in out
    assert "купить авто с пробегом" in out
    assert "автосалон краснодар" in out
    assert "фольксваген поло бу" in out
    assert "volkswagen купить" in out


def test_foreign_brand_in_minus_word_is_not_foreign(monkeypatch):
    _brand_guard_env(monkeypatch)
    out = _brand_group_keywords(["купить авто с пробегом -лада", "авито бу -ваз"])
    assert out == ["купить авто с пробегом -лада", "авито бу -ваз"]


def test_foreign_brand_filter_not_applied_to_common_segment(monkeypatch):
    _brand_guard_env(monkeypatch)
    calls = []
    monkeypatch.setattr(text_gen, "_drop_foreign_brand_keywords",
                        lambda kws, *names: calls.append(names) or list(kws))
    text_gen._filter_group_keywords(["автокредит краснодар"], "Общее", "Авто", "Краснодар",
                                    "С пробегом")
    assert calls == []


def test_foreign_brand_filter_never_falls_back_to_foreign_keywords(monkeypatch, capsys):
    _brand_guard_env(monkeypatch)
    out = _brand_group_keywords(["авто лада +с пробегом", "авито вазы бу", "лада бу краснодар"])
    assert out == []                                   # тихого фолбэка на чужие ключи нет
    assert "ЧУЖАЯ МАРКА" in capsys.readouterr().err    # обнуление видно в логе


def test_own_brand_unknown_disables_foreign_brand_filter(monkeypatch):
    """Группа без опознаваемой марки («Авто»/тема) — фильтр выключен, набор не выкашивается."""
    _brand_guard_env(monkeypatch)
    out = text_gen._filter_group_keywords(["авто лада +с пробегом"], "Марки", "Автокредит",
                                          "Краснодар", "С пробегом", model="")
    assert out == ["авто лада +с пробегом"]


# ── ⛔ РАССРОЧКА в контенте + заглавная первая буква (боевой факт 2026-07-28, porg-pl6iavd5) ──
def test_title_tails_do_not_offer_installment():
    """Хвост-добивка заголовка/текста больше не подставляет «рассрочку» (источник дефекта)."""
    assert not [t for t in text_gen._TITLE_TAILS if "рассрочк" in t.lower()]
    filled = text_gen._fill_title("Tenet в кредит в Краснодаре. Первый взнос 0 ₽", 45, 56)
    assert "рассрочк" not in filled.lower()


def test_ad_line_strips_installment_and_capitalizes_first_letter():
    line = create_set_assets._trim_ad_line(
        "Tenet в кредит в Краснодаре. Первый взнос 0 ₽. Рассрочка", 56)
    assert "рассрочк" not in line.lower()
    assert "в кредит" in line and "Первый взнос 0 ₽" in line   # легальные УТП сохранены
    assert create_set_assets._trim_ad_line(
        "новое авто в кредит. Первый взнос 0 ₽", 56).startswith("Новое авто в кредит")
    # регистр внутри строки не трогаем: марки, аббревиатуры, «трейд-ин», «₽/мес»
    keep = "KIA Rio в кредит. КАСКО в подарок. Трейд-ин. Платеж от 9 000 ₽/мес"
    assert create_set_assets._trim_ad_line(keep, 81) == keep


def test_upgrade_credit_titles_generic_anchor_is_not_used_as_brand(monkeypatch):
    monkeypatch.setattr(create_set_assets, "_drop_new_car", lambda items, _st: list(items))
    monkeypatch.setattr(create_set_assets, "_is_bu_site", lambda _st: False)
    monkeypatch.setitem(create_set_assets.__dict__, "_fill_title", text_gen._fill_title)

    out = create_set_assets._upgrade_credit_titles(
        ["Купить новое авто в кредит. Первый взнос 0 ₽"], 7, "Новые")

    assert out, "набор заголовков не должен обнуляться"
    assert all(t[:1] == t[:1].upper() for t in out), out
    assert not [t for t in out if "рассрочк" in t.lower()], out
    assert not [t for t in out if t.lower().startswith("новый новое")], out


def test_ai_agents_data_has_no_installment_content():
    """Источник контента не должен ОТДАВАТЬ рассрочку — фильтр на выходе LLM его не поймает."""
    from direct import ai_agents_data

    bad: list = []

    def _walk(node):
        if isinstance(node, str):
            if "рассрочк" in node.lower():
                bad.append(node)
        elif isinstance(node, dict):
            for k, v in node.items():
                _walk(k)
                _walk(v)
        elif isinstance(node, (list, tuple, set)):
            for v in node:
                _walk(v)

    for name, value in vars(ai_agents_data).items():
        if not name.startswith("__"):
            _walk(value)
    assert bad == [], bad


# ── ⛔ «Кредитное решение» — единый список запретов + маркер типа сайта «С пробегом» ──
def test_banned_content_covers_credit_decision_and_installment():
    from direct import text_norm, ai_agents
    for bad in ("Одобрение за 30 минут. Кредитное решение. Выгодно",
                "Кредитного решения за 5 минут", "Кредитные решения от 15 банков",
                "Первый взнос 0 ₽. Рассрочка"):
        assert text_norm.mentions_banned_content(bad), bad
        assert ai_agents.has_installment(bad), bad          # тот же список, не вторая копия
        assert "решени" not in text_norm.strip_banned_content(bad).lower()
        assert "рассрочк" not in text_norm.strip_banned_content(bad).lower()
    keep = "Авто в кредит. Одобрение за 30 минут. Первый взнос 0 ₽. Платеж от 9 000 ₽/мес"
    assert not text_norm.mentions_banned_content(keep)
    assert text_norm.strip_banned_content(keep) == keep
    assert create_set_assets._trim_ad_line(
        "Одобрение за 30 минут. Кредитное решение. Выгодно", 56).lower().find("решени") == -1


def _bu_assets_env(monkeypatch, bu: bool):
    monkeypatch.setattr(create_set_assets, "_drop_new_car", lambda items, _st: list(items))
    monkeypatch.setattr(create_set_assets, "_is_bu_site", lambda st: bu)
    monkeypatch.setitem(create_set_assets.__dict__, "_fill_title", text_gen._fill_title)


_BU_MARKER_RE = __import__("re").compile(r"(?i)с\s+пробегом|(?<![а-яё])б\s*/?\s*у(?![а-яё])")


def test_used_car_site_type_is_visible_in_content(monkeypatch):
    _bu_assets_env(monkeypatch, True)
    titles = create_set_assets._upgrade_credit_titles(["Авто в кредит от 9 000 ₽/мес"], 7,
                                                      "С пробегом")
    texts = create_set_assets._upgrade_credit_texts(["Авто в кредит от 9 000 ₽/мес"], 3,
                                                    "С пробегом")
    assert any(_BU_MARKER_RE.search(t) for t in titles), titles
    assert any(_BU_MARKER_RE.search(x) for x in texts), texts
    assert all(len(t) <= create_set_assets._RA_TITLE_MAX for t in titles), titles
    assert all(len(x) <= create_set_assets._RA_TEXT_MAX for x in texts), texts
    assert not [t for t in titles if "новое авто с пробегом" in t.lower()], titles


def test_new_car_site_type_never_gets_used_car_marker(monkeypatch):
    _bu_assets_env(monkeypatch, False)
    titles = create_set_assets._upgrade_credit_titles(["Авто в кредит от 9 000 ₽/мес"], 7, "Мульти")
    texts = create_set_assets._upgrade_credit_texts(["Авто в кредит от 9 000 ₽/мес"], 3, "Мульти")
    assert not [t for t in titles if _BU_MARKER_RE.search(t)], titles
    assert not [x for x in texts if _BU_MARKER_RE.search(x)], texts

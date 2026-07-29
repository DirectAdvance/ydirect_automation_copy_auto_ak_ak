"""Unit-тест: _bid_mod_dem строится с campaignId после AddCampaigns.

Bug 2 Critical: Grid-схема bidModifierDemographics требует campaignId (NonNull Long!).
Предыдущий фикс использовал {"adjustments": dem_adj} без campaignId → Grid-ошибка.
Этот тест проверяет правильную структуру payload и порядок вычислений.

Примечание: create_set_tp8_10 → ai_agents → ai_agents_data используют только stdlib
(json, re, urllib.parse, datetime) — заглушки flask/requests/psycopg2 не нужны.
"""

# ── Импортируем только чистые хелперы (без I/O зависимостей) ─────────────────
from direct.create import create_set_tp8_10 as post_module
from direct.clients import grid_finalize
from direct.create.create_set_tp8_10 import (
    _dem_adjustments_for_corr,
    _ag_code_for_corr,
    _normalize_post_markup,
    _post_href_for_label,
    _prepare_post_body,
    _strip_post_markup,
    _trim_post_body_preserve_phone,
)


# ── Тесты ────────────────────────────────────────────────────────────────────

MOCK_CORR_POSITIVE = {
    "demographic": [
        {"kind": "age", "key": "AGE_25_34", "pct": 50},
        {"kind": "age", "key": "AGE_35_44", "pct": 50},
        {"kind": "age", "key": "AGE_0_17",  "pct": -100},   # посевы: clamp to -50 -> multiplier 50
    ]
}

MOCK_CORR_ZERO_ONLY = {
    "demographic": [
        {"kind": "age", "key": "AGE_25_34", "pct": 0},
        {"kind": "gender", "key": "GENDER_MALE", "pct": 0},
    ]
}

MOCK_CORR_NEGATIVE_ONLY = {
    "demographic": [
        {"kind": "age", "key": "AGE_0_17",  "pct": -100},
        {"kind": "age", "key": "AGE_18_24", "pct": -75},
        {"kind": "gender", "key": "GENDER_FEMALE", "pct": -25},
    ]
}

MOCK_CAMPAIGN_ID = 712965328


def _build_bid_mod_dem(dem_adj: list, campaign_id: int):
    """Зеркало логики из run_create_set_post после AddCampaigns (строки 551-557)."""
    return (
        {"campaignId": str(campaign_id), "enabled": True,
         "adjustments": dem_adj, "type": "DEMOGRAPHY_MULTIPLIER"}
        if dem_adj else None
    )


class TestBidModDemStructure:
    """campaignId присутствует в _bid_mod_dem при ненулевых корректировках."""

    def test_dem_adj_nonempty_for_positive_pct(self):
        dem_adj = _dem_adjustments_for_corr(MOCK_CORR_POSITIVE)
        assert len(dem_adj) == 3, f"ожидали 3 adj (pct!=0), получили {dem_adj}"

    def test_negative_pct_converted_to_grid_multiplier(self):
        dem_adj = _dem_adjustments_for_corr(MOCK_CORR_POSITIVE)
        underage = [a for a in dem_adj if a.get("age") == "_0_17"]
        assert underage == [{"percent": 50, "id": None, "age": "_0_17", "gender": None}]

    def test_positive_pct_converted_to_grid_multiplier(self):
        dem_adj = _dem_adjustments_for_corr(MOCK_CORR_POSITIVE)
        ages = {a.get("age"): a["percent"] for a in dem_adj if a.get("age")}
        assert ages["_25_34"] == 150
        assert ages["_35_44"] == 150

    def test_negative_only_corr_builds_bid_modifier(self):
        dem_adj = _dem_adjustments_for_corr(MOCK_CORR_NEGATIVE_ONLY)
        assert [a["percent"] for a in dem_adj] == [50, 50, 75]

    def test_bid_mod_dem_has_campaign_id(self):
        """Главная проверка: campaignId присутствует и является строкой числа."""
        dem_adj = _dem_adjustments_for_corr(MOCK_CORR_POSITIVE)
        bid_mod_dem = _build_bid_mod_dem(dem_adj, MOCK_CAMPAIGN_ID)
        assert bid_mod_dem is not None
        assert "campaignId" in bid_mod_dem, "campaignId отсутствует в _bid_mod_dem"
        assert bid_mod_dem["campaignId"] is not None, "campaignId = None (был баг Bug2)"
        assert bid_mod_dem["campaignId"] == str(MOCK_CAMPAIGN_ID)

    def test_bid_mod_dem_has_required_fields(self):
        """enabled и type тоже обязательны по Grid-схеме."""
        dem_adj = _dem_adjustments_for_corr(MOCK_CORR_POSITIVE)
        bid_mod_dem = _build_bid_mod_dem(dem_adj, MOCK_CAMPAIGN_ID)
        assert bid_mod_dem["enabled"] is True
        assert bid_mod_dem["type"] == "DEMOGRAPHY_MULTIPLIER"
        assert isinstance(bid_mod_dem["adjustments"], list)
        assert len(bid_mod_dem["adjustments"]) == 3

    def test_bid_mod_dem_none_when_zero_corr(self):
        """При нулевых корректировках _bid_mod_dem = None."""
        dem_adj = _dem_adjustments_for_corr(MOCK_CORR_ZERO_ONLY)
        bid_mod_dem = _build_bid_mod_dem(dem_adj, MOCK_CAMPAIGN_ID)
        assert dem_adj == []
        assert bid_mod_dem is None

    def test_bid_mod_dem_none_when_corr_none(self):
        dem_adj = _dem_adjustments_for_corr(None)
        bid_mod_dem = _build_bid_mod_dem(dem_adj, MOCK_CAMPAIGN_ID)
        assert bid_mod_dem is None

    def test_ag_code_ag011_for_positive_age_corr(self):
        assert _ag_code_for_corr(MOCK_CORR_POSITIVE) == "ag011"

    def test_ag_code_ag001_for_zero_corr(self):
        assert _ag_code_for_corr(MOCK_CORR_ZERO_ONLY) == "ag001"

    def test_ag_code_ag011_for_negative_corr(self):
        assert _ag_code_for_corr(MOCK_CORR_NEGATIVE_ONLY) == "ag011"

    def test_ag_code_ag001_for_none(self):
        assert _ag_code_for_corr(None) == "ag001"

    def test_strip_post_markup_before_add_post_ads(self):
        body = ":i:Акция недели:ii:\n:b:При оформлении::bb: — КАСКО."
        assert _strip_post_markup(body) == "Акция недели\nПри оформлении: — КАСКО."

    def test_normalize_post_markup_preserves_valid_highlights(self):
        body = ":i:Акция недели:ii:\n:b:Tenet Gentra:bb: — от 7 355 ₽/мес"
        assert _normalize_post_markup(body) == body

    def test_normalize_post_markup_closes_orphan_open_tag(self):
        body = "Специальные предложения действуют до конца месяца! :i:"
        assert _normalize_post_markup(body).endswith(":i::ii:")

    def test_prepare_post_body_removes_domain_and_site_word(self):
        body = "Оставьте заявку на сайте bucars-kuban.site — перезвоним в течение часа."
        prepared = _prepare_post_body(body, href="https://bucars-kuban.site/path", brand_label="Tenet")
        assert "bucars-kuban.site" not in prepared
        assert "сайт" not in prepared.lower()
        assert "Оставьте заявку" in prepared

    def test_prepare_post_body_highlights_models_and_utp(self):
        body = "Tenet Gentra 2017-2020 — от 7 355 ₽/мес\n— КАСКО в подарок"
        prepared = _prepare_post_body(body, href="", brand_label="Tenet")
        assert ":b:Tenet Gentra 2017-2020:bb: — от 7 355 ₽/мес" in prepared
        assert "— :b:КАСКО в подарок:bb:" in prepared

    def test_prepare_post_body_removes_empty_inventory_block(self):
        body = (
            "Успейте купить новый Tenet по цене б/у!\n\n"
            ":b:В наличии::bb:\n\n"
            ":b:Бонусы при покупке в этом месяце::bb:\n"
            "— КАСКО от лучших страховых компаний\n"
            "— Трейд-ин с выгодой до 50 000 ₽"
        )
        prepared = _prepare_post_body(body, href="", brand_label="Tenet")
        assert "В наличии" not in _strip_post_markup(prepared)
        assert "Бонусы при покупке" in prepared

    def test_post_body_extension_keeps_phone_as_final_line(self):
        body = (
            "Успейте купить новый автомобиль по выгодной цене перед новым завозом!\n"
            "Скидка до 25% на все модели Lada в наличии.\n\n"
            ":b:Бонусы при покупке в этом месяце::bb:\n"
            "— КАСКО в подарок\n"
            "— 0 ₽ первый взнос + до 2 месяцев без платежей\n\n"
            ":b:Оставьте заявку::bb: — зафиксируем условия и перезвоним в течение часа!\n\n"
            "Подробности по телефону: +79999999991\n"
            "— Рассчитаем трейд-ин и возможные подарки при покупке."
        )
        extended = post_module._extend_post_body_after_finalize(body, "Lada", "https://autopark777.site/auto/lada")
        lines = [ln.strip() for ln in extended.splitlines() if ln.strip()]
        assert lines[-1] == "Подробности по телефону: +79999999991"
        assert "Рассчитаем трейд-ин" not in extended

    def test_post_body_extension_uses_free_space_before_phone(self):
        body = (
            "Успейте купить новый Tenet по цене б/у! Распродажа перед завозом — скидка "
            "до 30% на все модели Tenet. Спешите, акция действует только до конца месяца!\n\n"
            "В наличии:\n\n"
            "При оформлении в этом месяце — подарки на выбор:\n"
            "— КАСКО от лучших страховых компаний\n"
            "— Второй комплект зимних шин в подарок\n"
            "— 0 ₽ первый взнос + до 2 месяцев без платежей\n"
            "— Господдержка и нулевой утильсбор\n"
            "— Трейд-ин с выгодой до 50 000 ₽\n\n"
            "Оставьте заявку: — зафиксируем условия и перезвоним в течение часа.\n"
            "Спешите, количество автомобилей ограничено!\n\n"
            "Можно сравнить комплектации и платежи до визита в автосалон.\n\n"
            "Подробности по телефону: +79999999991"
        )
        extended = post_module._extend_post_body_after_finalize(body, "Tenet", "https://autopark777.site/auto/tenet")
        lines = [ln.strip() for ln in extended.splitlines() if ln.strip()]
        assert lines[-1] == "Подробности по телефону: +79999999991"
        assert len(extended) >= post_module.POST_BODY_MAX - 30

    def test_trim_post_body_preserves_existing_phone_line(self):
        body = (
            "Успейте купить авто с выгодой до 500 000 ₽. Предложение действует до конца месяца.\n\n"
            "Бонусы при покупке:\n"
            "— КАСКО на год в подарок\n"
            "— Трейд-ин выше рыночной стоимости\n\n"
            "Сверим наличие и комплектацию до визита, чтобы расчёт был привязан к реальному автомобилю.\n\n"
            "Подберём кредитную программу под ваш бюджет и заранее расскажем, какие бонусы доступны по выбранной модели.\n\n"
            "Можно сравнить комплектации и платежи до визита в автосалон.\n\n"
            "Перед визитом сверим условия и поможем выбрать удобное время для звонка.\n\n"
            "Проверим одобрение в банках-партнёрах без визита в салон.\n\n"
            "Зафиксируем условия акции до визита.\n\n"
            "Уточним детали до визита.\n\n"
            "Покажем варианты по платежу.\n\n"
            "Подробности по телефону: +79999999991"
        )
        trimmed = _trim_post_body_preserve_phone(body, post_module.POST_BODY_MAX)
        lines = [ln.strip() for ln in trimmed.splitlines() if ln.strip()]
        assert lines[-1] == "Подробности по телефону: +79999999991"
        assert len(trimmed) <= post_module.POST_BODY_MAX

    def test_post_body_dedupes_kasko_and_normalizes_new_auto(self):
        body = (
            "Успейте купить новый авто Haval с выгодой в кредит.\n\n"
            "Бонусы при покупке:\n"
            "— КАСКО на год в подарок\n"
            "— КАСКО включено в кредит — без доплаты первого года.\n"
            "— Трейд-ин с выгодой до 50 000 ₽"
        )
        prepared = _prepare_post_body(body, href="", brand_label="Haval")
        plain = _strip_post_markup(prepared)
        assert "новое авто" in plain
        assert plain.lower().count("каско") == 1

    def test_post_href_brand_label_uses_brand_page_from_feed_model_url(self, monkeypatch):
        urls = {"haval": "https://autopark777.site/auto/haval/m6/ii/suv-5d?fid=1"}

        monkeypatch.setattr(post_module, "_post_feed_url_map", lambda *_args, **_kw: urls)
        monkeypatch.setattr(post_module, "_post_feed_url_for_label", lambda u, label: u.get(label.lower()))
        monkeypatch.setattr(post_module, "_post_ct_segment", lambda _ct: "Марки")

        href = _post_href_for_label(
            "porg-test",
            "https://autopark777.site/",
            "Haval",
            ct="ct0111",
            site_type="Мультибренд",
        )

        assert href == "https://autopark777.site/auto/haval"

    def test_post_href_model_label_keeps_model_page(self, monkeypatch):
        urls = {"haval m6": "https://autopark777.site/auto/haval/m6/ii/suv-5d?fid=1"}

        monkeypatch.setattr(post_module, "_post_feed_url_map", lambda *_args, **_kw: urls)
        monkeypatch.setattr(post_module, "_post_feed_url_for_label", lambda u, label: u.get(label.lower()))
        monkeypatch.setattr(post_module, "_post_ct_segment", lambda _ct: "Модели")

        href = _post_href_for_label(
            "porg-test",
            "https://autopark777.site/",
            "Haval M6",
            ct="ct0120",
            site_type="Мультибренд",
        )

        assert href == "https://autopark777.site/auto/haval/m6/ii/suv-5d"

    def test_post_href_ignores_quiz_only_feed_offer(self, monkeypatch):
        """Посевы: единственный оффер марки — из квиз-фида → кнопка ведёт на страницу марки."""
        urls = {"kaiyi": "https://newautos-193.site/quiz?fid=95713#x7-kunlun-i-suv-5d"}

        monkeypatch.setattr(post_module, "_post_feed_url_map", lambda *_args, **_kw: urls)
        monkeypatch.setattr(post_module, "_post_feed_url_for_label", lambda u, label: u.get(label.lower()))
        monkeypatch.setattr(post_module, "_post_ct_segment", lambda _ct: "Марки")

        href = _post_href_for_label(
            "porg-test",
            "https://newautos-193.site/",
            "Kaiyi",
            ct="ct0154",
            site_type="Мультибренд",
        )

        assert href == "https://newautos-193.site/auto/kaiyi"

    def test_post_href_single_brand_fallback_uses_brand_page_when_segment_missing(self, monkeypatch):
        urls = {"haval": "https://autopark777.site/auto/haval/m6/ii/suv-5d?fid=1"}

        monkeypatch.setattr(post_module, "_post_feed_url_map", lambda *_args, **_kw: urls)
        monkeypatch.setattr(post_module, "_post_feed_url_for_label", lambda u, label: u.get(label.lower()))
        monkeypatch.setattr(post_module, "_post_ct_segment", lambda _ct: "")

        href = _post_href_for_label(
            "porg-test",
            "https://autopark777.site/",
            "Haval",
            ct="ct0111",
            site_type="Мультибренд",
        )

        assert href == "https://autopark777.site/auto/haval"


def test_run_create_set_post_fails_when_post_ads_underfilled(monkeypatch):
    class Resp:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class FakeClient:
        def _bootstrap_csrf(self):
            return None

        def upload_image(self, path):
            return f"hash-{path}"

        def _post(self, op, _query, variables):
            if op == "AddCampaigns":
                return Resp({"data": {"addCampaigns": {"addedCampaigns": [{"id": "777"}]}}})
            if op == "AddPostAdGroups":
                gid = f"g{len(group_calls)}"
                group_calls.append(variables)
                return Resp({"data": {"addPostAdGroups": {"addedAdGroupItems": [{"adGroupId": gid}]}}})
            if op == "AddPostAds":
                return Resp({"data": {"addPostAds": {"addedAds": [{"id": "a1"}, {"id": "a2"}]}}})
            raise AssertionError(op)

    group_calls = []
    monkeypatch.setattr(post_module, "_fetch_notification_email", lambda *_args, **_kw: "ok@example.com")
    monkeypatch.setattr(post_module, "_posevy_images_for_ct", lambda *_args, **_kw: ["1", "2", "3"])
    monkeypatch.setattr(post_module, "_post_feed_url_map", lambda *_args, **_kw: {})
    monkeypatch.setattr(post_module, "_post_allowed_models_from_feed", lambda *_args, **_kw: [])
    monkeypatch.setattr(post_module, "_post_href_for_label", lambda *_args, **_kw: "https://example.com/")
    monkeypatch.setattr(grid_finalize, "get_grid_client", lambda *_args, **_kw: FakeClient())

    result = post_module.run_create_set_post(
        it={"title": "Тест", "body": "Тестовое объявление"},
        name="tp8_cpc_site_ct0000_aon_n000_r0002_ct018_ag001_g00 — Посевы Telegram - Москва",
        login="porg-test",
        slepok="pavlov",
        site_type="Мультибренд",
        href="https://example.com/",
        region_ids=[213],
        counter_id=None,
        goal_id=None,
        grid_cookie=None,
        tp_code="tp8",
        r_code="r0002",
        oblast="Москва",
    )[0]

    assert result["ok"] is False
    assert result["partial"] is True
    assert result["campaign_id"] == "777"
    assert result["ad_ids"] == ["a1", "a2"]
    assert result["build"] == {"adgroups": 3, "ads": 3}
    assert "недобор объявлений 2/3" in result["error"]

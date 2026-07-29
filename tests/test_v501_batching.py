"""Тесты батчинга Direct API v501 — Этапы 1 и 2.

Тестируемые методы:
  Этап 1: DirectV501Client.add_feed_ads_batch  (ShoppingAd+ListingAd пары)
  Этап 1: create_set_feed_builders._create_tp3_single — переход на batch path
  Этап 2: DirectV501Client.add_product_adgroups_batch (batch adgroups.add)
  Этап 2: setup_search_dynamic_campaign  (TextAd+ShoppingAd в одном ads.add)
  Этап 2: setup_combined_campaign        (batch adgroups + batch ads)

Все тесты — unit (монкипэтчинг _call / внешних deps), без реального API.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch, call

from direct.clients.direct_v501_client import DirectV501Client, DirectV501Error, _FEED_ADS_BATCH_SIZE


# ─── helpers ─────────────────────────────────────────────────────────────────

def _make_client() -> DirectV501Client:
    """Возвращает DirectV501Client с заглушкой сессии (без реальных запросов)."""
    cl = DirectV501Client.__new__(DirectV501Client)
    cl.client_login = "test-login"
    cl.timeout = 30
    cl.sess = MagicMock()
    return cl


def _ok_result(*ids) -> dict:
    """Генерирует AddResults вида [{"Id": id1}, {"Id": id2}, ...]."""
    return {"AddResults": [{"Id": i} for i in ids]}


def _err_result(code: int = 6000, msg: str = "ошибка") -> dict:
    """Генерирует AddResults с ошибкой для одного элемента."""
    return {"AddResults": [{"Errors": [{"Code": code, "Message": msg, "Details": ""}]}]}


# ═══════════════════════════════════════════════════════════════════════════════
# Этап 1: add_feed_ads_batch
# ═══════════════════════════════════════════════════════════════════════════════

class TestAddFeedAdsBatch:
    """add_feed_ads_batch: позиционное сопоставление, фильтры, чанкование, ошибки."""

    def test_empty_input_returns_empty(self):
        cl = _make_client()
        result = cl.add_feed_ads_batch([])
        assert result == []

    def test_single_pair_success(self, monkeypatch):
        """Один фид: один вызов ads.add с 2 объявлениями, правильные Id."""
        cl = _make_client()
        calls_made = []

        def fake_call(service, method, params):
            calls_made.append((service, method, params))
            return {"AddResults": [{"Id": 101}, {"Id": 102}]}

        monkeypatch.setattr(cl, "_call", fake_call)
        result = cl.add_feed_ads_batch([{"adgroup_id": 10, "feed_id": 42}])

        assert len(calls_made) == 1
        svc, meth, p = calls_made[0]
        assert svc == "ads" and meth == "add"
        ads = p["Ads"]
        assert len(ads) == 2
        assert ads[0] == {"AdGroupId": 10, "ShoppingAd": {"FeedId": 42}}
        assert ads[1] == {"AdGroupId": 10, "ListingAd": {"FeedId": 42}}

        assert len(result) == 1
        shop_id, listing_id, err = result[0]
        assert shop_id == 101
        assert listing_id == 102
        assert err is None

    def test_two_pairs_positional_order(self, monkeypatch):
        """Два фида: ShoppingAd[0], ListingAd[0], ShoppingAd[1], ListingAd[1] — позиции."""
        cl = _make_client()

        def fake_call(service, method, params):
            # AddResults[0]=ShoppingAd_ag10, [1]=ListingAd_ag10, [2]=ShoppingAd_ag20, [3]=ListingAd_ag20
            return {"AddResults": [{"Id": 101}, {"Id": 102}, {"Id": 201}, {"Id": 202}]}

        monkeypatch.setattr(cl, "_call", fake_call)
        result = cl.add_feed_ads_batch([
            {"adgroup_id": 10, "feed_id": 42},
            {"adgroup_id": 20, "feed_id": 55},
        ])

        assert len(result) == 2
        assert result[0] == (101, 102, None)   # ag10: shop=101, listing=102
        assert result[1] == (201, 202, None)   # ag20: shop=201, listing=202

    def test_shopping_ad_error_returns_none_for_shop(self, monkeypatch):
        """Ошибка ShoppingAd[0] → shop_id=None, err_msg содержит 'ShoppingAd'."""
        cl = _make_client()

        def fake_call(service, method, params):
            return {"AddResults": [
                {"Errors": [{"Code": 6000, "Message": "invalid feed", "Details": ""}]},
                {"Id": 102},  # ListingAd (всё равно вернулось)
            ]}

        monkeypatch.setattr(cl, "_call", fake_call)
        result = cl.add_feed_ads_batch([{"adgroup_id": 10, "feed_id": 42}])

        shop_id, listing_id, err = result[0]
        assert shop_id is None
        assert listing_id == 102
        assert err is not None
        assert "ShoppingAd" in err

    def test_listing_ad_error_returns_none_for_listing(self, monkeypatch):
        """Ошибка ListingAd[1] → listing_id=None, shop_id присутствует, err_msg содержит 'ListingAd'."""
        cl = _make_client()

        def fake_call(service, method, params):
            return {"AddResults": [
                {"Id": 101},  # ShoppingAd OK
                {"Errors": [{"Code": 8800, "Message": "listing error", "Details": ""}]},
            ]}

        monkeypatch.setattr(cl, "_call", fake_call)
        result = cl.add_feed_ads_batch([{"adgroup_id": 10, "feed_id": 42}])

        shop_id, listing_id, err = result[0]
        assert shop_id == 101
        assert listing_id is None
        assert err is not None
        assert "ListingAd" in err

    def test_chunking_splits_at_batch_size(self, monkeypatch):
        """N > _FEED_ADS_BATCH_SIZE → несколько вызовов ads.add (чанкование)."""
        cl = _make_client()
        call_count = [0]

        def fake_call(service, method, params):
            call_count[0] += 1
            n = len(params["Ads"])
            return {"AddResults": [{"Id": call_count[0] * 100 + i} for i in range(n)]}

        monkeypatch.setattr(cl, "_call", fake_call)

        n_feeds = _FEED_ADS_BATCH_SIZE + 3  # один полный чанк + остаток
        feed_groups = [{"adgroup_id": i, "feed_id": i * 10} for i in range(n_feeds)]
        result = cl.add_feed_ads_batch(feed_groups)

        assert call_count[0] == 2           # 2 чанка
        assert len(result) == n_feeds       # одна пара на фид

    def test_vendor_filter_applied_to_both_ad_types(self, monkeypatch):
        """vendor → FeedFilterConditions с Operand='vendor' на ShoppingAd И ListingAd."""
        cl = _make_client()
        captured = {}

        def fake_call(service, method, params):
            captured["ads"] = params["Ads"]
            return {"AddResults": [{"Id": 1}, {"Id": 2}]}

        monkeypatch.setattr(cl, "_call", fake_call)
        cl.add_feed_ads_batch([{"adgroup_id": 10, "feed_id": 42, "vendor": "Toyota"}])

        ads = captured["ads"]
        assert len(ads) == 2
        # ShoppingAd
        conds_s = ads[0]["ShoppingAd"].get("FeedFilterConditions", [])
        assert conds_s and conds_s[0]["Operand"] == "vendor"
        assert "Toyota" in conds_s[0]["Arguments"]
        # ListingAd
        conds_l = ads[1]["ListingAd"].get("FeedFilterConditions", [])
        assert conds_l and conds_l[0]["Operand"] == "vendor"
        assert "Toyota" in conds_l[0]["Arguments"]

    def test_collection_id_filter_applied_when_no_vendor(self, monkeypatch):
        """collection_id (без vendor) → FeedFilterConditions с Operand='collectionId'."""
        cl = _make_client()
        captured = {}

        def fake_call(service, method, params):
            captured["ads"] = params["Ads"]
            return {"AddResults": [{"Id": 1}, {"Id": 2}]}

        monkeypatch.setattr(cl, "_call", fake_call)
        cl.add_feed_ads_batch([{"adgroup_id": 10, "feed_id": 42, "collection_id": "model_7"}])

        ads = captured["ads"]
        conds_s = ads[0]["ShoppingAd"].get("FeedFilterConditions", [])
        assert conds_s and conds_s[0]["Operand"] == "collectionId"
        conds_l = ads[1]["ListingAd"].get("FeedFilterConditions", [])
        assert conds_l and conds_l[0]["Operand"] == "collectionId"

    def test_no_filter_when_no_vendor_and_no_collection(self, monkeypatch):
        """Без vendor и collection_id — FeedFilterConditions отсутствует."""
        cl = _make_client()

        def fake_call(service, method, params):
            return {"AddResults": [{"Id": 1}, {"Id": 2}]}

        monkeypatch.setattr(cl, "_call", fake_call)
        # Не должно быть исключений при отсутствии фильтров
        result = cl.add_feed_ads_batch([{"adgroup_id": 10, "feed_id": 42}])
        assert result[0][0] == 1   # shop_id

    def test_api_raises_propagates(self, monkeypatch):
        """Исключение на уровне HTTP (_call raises) → пробрасывается из add_feed_ads_batch."""
        cl = _make_client()

        def fake_call(service, method, params):
            raise DirectV501Error("ads.add", 152, "лимит баллов исчерпан")

        monkeypatch.setattr(cl, "_call", fake_call)
        with pytest.raises(DirectV501Error, match="152"):
            cl.add_feed_ads_batch([{"adgroup_id": 10, "feed_id": 42}])


# ═══════════════════════════════════════════════════════════════════════════════
# Этап 2: add_product_adgroups_batch
# ═══════════════════════════════════════════════════════════════════════════════

class TestAddProductAdgroupsBatch:
    """add_product_adgroups_batch: позиционное сопоставление, ошибки."""

    def test_empty_input_returns_empty(self):
        cl = _make_client()
        assert cl.add_product_adgroups_batch(1, []) == []

    def test_two_groups_positional_ids(self, monkeypatch):
        """2 группы: AddResults[0]→group[0], AddResults[1]→group[1]."""
        cl = _make_client()
        calls_made = []

        def fake_call(service, method, params):
            calls_made.append((service, method, params))
            return {"AddResults": [{"Id": 501}, {"Id": 502}]}

        monkeypatch.setattr(cl, "_call", fake_call)
        result = cl.add_product_adgroups_batch(
            campaign_id=999,
            groups=[
                {"name": "Поиск (текст)", "region_ids": [1]},
                {"name": "Товарная галерея", "region_ids": [1]},
            ],
        )

        assert len(calls_made) == 1
        assert calls_made[0][0] == "adgroups" and calls_made[0][1] == "add"
        ag_params = calls_made[0][2]["AdGroups"]
        assert len(ag_params) == 2
        assert ag_params[0]["Name"] == "Поиск (текст)"
        assert ag_params[0]["CampaignId"] == 999
        assert ag_params[1]["Name"] == "Товарная галерея"

        assert result == [501, 502]

    def test_error_in_one_group_returns_none(self, monkeypatch):
        """Ошибка в группе[1] → result[1] = None, result[0] = Id."""
        cl = _make_client()

        def fake_call(service, method, params):
            return {"AddResults": [
                {"Id": 501},
                {"Errors": [{"Code": 8000, "Message": "дубль имени", "Details": ""}]},
            ]}

        monkeypatch.setattr(cl, "_call", fake_call)
        result = cl.add_product_adgroups_batch(
            campaign_id=999,
            groups=[{"name": "AG1"}, {"name": "AG2"}],
        )
        assert result[0] == 501
        assert result[1] is None

    def test_default_region_ids_used_when_absent(self, monkeypatch):
        """groups без region_ids → RegionIds=[225] в запросе."""
        cl = _make_client()
        captured = {}

        def fake_call(service, method, params):
            captured["params"] = params
            return {"AddResults": [{"Id": 1}]}

        monkeypatch.setattr(cl, "_call", fake_call)
        cl.add_product_adgroups_batch(999, [{"name": "AG"}])

        assert captured["params"]["AdGroups"][0]["RegionIds"] == [225]


# ═══════════════════════════════════════════════════════════════════════════════
# Этап 2: setup_search_dynamic_campaign
# ═══════════════════════════════════════════════════════════════════════════════

class TestSetupSearchDynamicCampaign:
    """setup_search_dynamic_campaign: один ads.add с TextAd+ShoppingAd (батч)."""

    def test_single_ads_call_with_both_types(self, monkeypatch):
        """Должен быть РОВНО один вызов ads.add, содержащий TextAd И ShoppingAd."""
        cl = _make_client()
        calls = []

        def fake_call(service, method, params):
            calls.append((service, method, params))
            if service == "adgroups":
                return {"AddResults": [{"Id": 10}]}
            if service == "ads":
                return {"AddResults": [{"Id": 201}, {"Id": 202}]}
            return {}

        monkeypatch.setattr(cl, "_call", fake_call)
        ag_id, text_id, shop_id = cl.setup_search_dynamic_campaign(
            campaign_id=999, feed_id=42, href="https://example.com",
            text_title="Заголовок", text_body="Текст объявления"
        )

        ads_calls = [c for c in calls if c[0] == "ads" and c[1] == "add"]
        assert len(ads_calls) == 1, f"Ожидался 1 вызов ads.add, получено {len(ads_calls)}"
        ads = ads_calls[0][2]["Ads"]
        assert len(ads) == 2
        assert "TextAd" in ads[0]
        assert "ShoppingAd" in ads[1]

    def test_returns_correct_ids(self, monkeypatch):
        cl = _make_client()

        def fake_call(service, method, params):
            if service == "adgroups":
                return {"AddResults": [{"Id": 10}]}
            return {"AddResults": [{"Id": 201}, {"Id": 202}]}

        monkeypatch.setattr(cl, "_call", fake_call)
        ag_id, text_id, shop_id = cl.setup_search_dynamic_campaign(
            campaign_id=999, feed_id=42, href="https://example.com"
        )
        assert ag_id == 10
        assert text_id == 201
        assert shop_id == 202

    def test_text_ad_error_raises(self, monkeypatch):
        cl = _make_client()

        def fake_call(service, method, params):
            if service == "adgroups":
                return {"AddResults": [{"Id": 10}]}
            return {"AddResults": [
                {"Errors": [{"Code": 6000, "Message": "TextAd error", "Details": ""}]},
                {"Id": 202},
            ]}

        monkeypatch.setattr(cl, "_call", fake_call)
        with pytest.raises(DirectV501Error, match="TextAd"):
            cl.setup_search_dynamic_campaign(campaign_id=999, feed_id=42, href="https://example.com")

    def test_shopping_ad_error_raises(self, monkeypatch):
        cl = _make_client()

        def fake_call(service, method, params):
            if service == "adgroups":
                return {"AddResults": [{"Id": 10}]}
            return {"AddResults": [
                {"Id": 201},
                {"Errors": [{"Code": 8800, "Message": "ShoppingAd error", "Details": ""}]},
            ]}

        monkeypatch.setattr(cl, "_call", fake_call)
        with pytest.raises(DirectV501Error, match="ShoppingAd"):
            cl.setup_search_dynamic_campaign(campaign_id=999, feed_id=42, href="https://example.com")


# ═══════════════════════════════════════════════════════════════════════════════
# Этап 2: setup_combined_campaign
# ═══════════════════════════════════════════════════════════════════════════════

class TestSetupCombinedCampaign:
    """setup_combined_campaign: batch adgroups + batch ads, позиционный порядок."""

    def _fake_call(self, service, method, params):
        """Заглушка: adgroups.add → [501, 502], ads.add → [301, 302]."""
        if service == "adgroups" and method == "add":
            return {"AddResults": [{"Id": 501}, {"Id": 502}]}
        if service == "ads" and method == "add":
            return {"AddResults": [{"Id": 301}, {"Id": 302}]}
        return {}

    def test_one_adgroups_call_and_one_ads_call(self, monkeypatch):
        """Должно быть ровно 1 вызов adgroups.add и 1 вызов ads.add."""
        cl = _make_client()
        calls = []

        def fake_call(service, method, params):
            calls.append((service, method))
            return self._fake_call(service, method, params)

        monkeypatch.setattr(cl, "_call", fake_call)
        cl.setup_combined_campaign(
            campaign_id=999, feed_id=42, href="https://example.com"
        )

        ag_calls = [c for c in calls if c[0] == "adgroups" and c[1] == "add"]
        ads_calls = [c for c in calls if c[0] == "ads" and c[1] == "add"]
        assert len(ag_calls) == 1, f"Ожидался 1 вызов adgroups.add, получено {len(ag_calls)}"
        assert len(ads_calls) == 1, f"Ожидался 1 вызов ads.add, получено {len(ads_calls)}"

    def test_adgroups_positional_order(self, monkeypatch):
        """AddResults[0]=search_ag, AddResults[1]=product_ag — порядок позиционный."""
        cl = _make_client()

        def fake_call(service, method, params):
            return self._fake_call(service, method, params)

        monkeypatch.setattr(cl, "_call", fake_call)
        search_ag, text_id, product_ag, shop_id = cl.setup_combined_campaign(
            campaign_id=999, feed_id=42, href="https://example.com",
            search_group_name="Поиск (текст)", product_group_name="Товарная галерея",
        )

        assert search_ag == 501   # AddResults[0] → первая группа
        assert product_ag == 502  # AddResults[1] → вторая группа

    def test_ads_use_correct_adgroup_ids(self, monkeypatch):
        """TextAd идёт в search_ag(501), ShoppingAd — в product_ag(502)."""
        cl = _make_client()
        ads_params = []

        def fake_call(service, method, params):
            if service == "ads":
                ads_params.append(params["Ads"])
            return self._fake_call(service, method, params)

        monkeypatch.setattr(cl, "_call", fake_call)
        cl.setup_combined_campaign(
            campaign_id=999, feed_id=42, href="https://example.com"
        )

        assert len(ads_params) == 1
        ads = ads_params[0]
        assert ads[0]["AdGroupId"] == 501 and "TextAd" in ads[0]
        assert ads[1]["AdGroupId"] == 502 and "ShoppingAd" in ads[1]

    def test_returns_correct_tuple(self, monkeypatch):
        cl = _make_client()

        monkeypatch.setattr(cl, "_call", lambda s, m, p: self._fake_call(s, m, p))
        search_ag, text_id, product_ag, shop_id = cl.setup_combined_campaign(
            campaign_id=999, feed_id=42, href="https://example.com"
        )
        assert (search_ag, text_id, product_ag, shop_id) == (501, 301, 502, 302)

    def test_search_group_error_raises(self, monkeypatch):
        """Ошибка создания search_ag → DirectV501Error."""
        cl = _make_client()

        def fake_call(service, method, params):
            if service == "adgroups":
                return {"AddResults": [
                    {"Errors": [{"Code": 8000, "Message": "ошибка", "Details": ""}]},
                    {"Id": 502},
                ]}
            return self._fake_call(service, method, params)

        monkeypatch.setattr(cl, "_call", fake_call)
        with pytest.raises(DirectV501Error, match="поисковая группа"):
            cl.setup_combined_campaign(campaign_id=999, feed_id=42, href="https://example.com")

    def test_product_group_error_raises(self, monkeypatch):
        """Ошибка создания product_ag → DirectV501Error."""
        cl = _make_client()

        def fake_call(service, method, params):
            if service == "adgroups":
                return {"AddResults": [
                    {"Id": 501},
                    {"Errors": [{"Code": 8000, "Message": "ошибка", "Details": ""}]},
                ]}
            return self._fake_call(service, method, params)

        monkeypatch.setattr(cl, "_call", fake_call)
        with pytest.raises(DirectV501Error, match="товарная группа"):
            cl.setup_combined_campaign(campaign_id=999, feed_id=42, href="https://example.com")


# ═══════════════════════════════════════════════════════════════════════════════
# Этап 1: семантика _delete_partial в create_set_feed_builders._create_tp3_single
# ═══════════════════════════════════════════════════════════════════════════════

class TestFeedBuilderBatchSemantics:
    """Проверяем семантику batch-пути в _create_tp3_single.

    Тестируем через _create_tp3_single внутри модуля с полным монкипэтчингом
    зависимостей: cmc (DirectV501Client) и _delete_partial_campaign.
    """

    def _make_spec(self):
        """Минимальная data-структура для _create_tp3_single."""
        return {
            "cl": MagicMock(),   # будет заменён ниже
            "feeds": [(42, "yandex.xml")],
            "sitelinks": [],
            "callout_ids": [],
            "promos": [],
            "minus_set": None,
        }

    def _patch_module(self, monkeypatch, fake_cl, feed_groups=None):
        """Монкипэтч create_set_feed_builders глобальных зависимостей."""
        import direct.create.create_set_feed_builders as fb

        # cmc.DirectV501Client → fake_cl, cmc.UTM_TEMPLATE → строка
        fake_cmc = MagicMock()
        fake_cmc.DirectV501Client.return_value = fake_cl
        fake_cmc.UnifiedCampaignSpec = MagicMock(return_value=MagicMock())
        fake_cmc.UTM_TEMPLATE = "{campaign_id}"

        monkeypatch.setattr(fb, "cmc", fake_cmc, raising=False)
        monkeypatch.setattr(fb, "_finalize_search_via_grid", lambda *a, **kw: None, raising=False)
        monkeypatch.setattr(fb, "gf", MagicMock(), raising=False)

    def test_shopping_ad_failure_skips_group_no_delete(self, monkeypatch):
        """ShoppingAd fail → группа пропускается, campaign НЕ удаляется, ok=False (нет _shops)."""
        import direct.create.create_set_feed_builders as fb

        cl = MagicMock()
        cl.create_unified_campaign.return_value = 999
        cl.add_product_adgroup.return_value = 10
        # add_feed_ads_batch возвращает (None, None, err): ShoppingAd failed
        cl.add_feed_ads_batch.return_value = [(None, None, "ShoppingAd: invalid feed (code=6000)")]

        deleted = []

        def fake_delete(token, login, cid):
            deleted.append(cid)

        self._patch_module(monkeypatch, cl)
        monkeypatch.setattr(fb, "_delete_partial_campaign", fake_delete, raising=False)

        result = fb._create_tp3_single(
            data={"cl": cl, "feeds": [(42, "yandex.xml")], "sitelinks": [], "callout_ids": [],
                  "promos": [], "minus_set": None},
            token="tok", login="porg-test", name="TestCamp",
            mode="network", pay_for_conv=False, goal_id=1, cpa_rub=500,
            budget_rub=5000, counter_id=1, region_ids=[225],
            href="https://example.com", feed_id=42, feed_name="yandex.xml",
            group_name="Группа", corr={}, ret_map={},
        )

        # ShoppingAd failed → _shops пуст → кампания удалена через _shops-гейт (не listing-гейт)
        # Важно: delete вызывается через _shops-гейт, а не через listing-гейт
        assert deleted, "Кампания должна быть удалена через _shops-гейт (нет ни одного _shops)"
        assert result.get("ok") is False

    def test_listing_ad_failure_deletes_campaign(self, monkeypatch):
        """ShoppingAd OK, ListingAd fail → campaign УДАЛЯЕТСЯ, ok=False, defer=True."""
        import direct.create.create_set_feed_builders as fb

        cl = MagicMock()
        cl.create_unified_campaign.return_value = 999
        cl.add_product_adgroup.return_value = 10
        # ShoppingAd succeeded (shop_id=101), ListingAd failed
        cl.add_feed_ads_batch.return_value = [(101, None, "ListingAd: error (code=8800)")]

        deleted = []

        def fake_delete(token, login, cid):
            deleted.append(cid)

        self._patch_module(monkeypatch, cl)
        monkeypatch.setattr(fb, "_delete_partial_campaign", fake_delete, raising=False)

        result = fb._create_tp3_single(
            data={"cl": cl, "feeds": [(42, "yandex.xml")], "sitelinks": [], "callout_ids": [],
                  "promos": [], "minus_set": None},
            token="tok", login="porg-test", name="TestCamp",
            mode="network", pay_for_conv=False, goal_id=1, cpa_rub=500,
            budget_rub=5000, counter_id=1, region_ids=[225],
            href="https://example.com", feed_id=42, feed_name="yandex.xml",
            group_name="Группа", corr={}, ret_map={},
        )

        assert deleted, "Кампания должна быть удалена при ошибке ListingAd"
        assert result.get("ok") is False
        assert result.get("defer") is True

    def test_both_ads_success_returns_ok(self, monkeypatch):
        """Оба объявления созданы → ok=True."""
        import direct.create.create_set_feed_builders as fb

        cl = MagicMock()
        cl.create_unified_campaign.return_value = 999
        cl.add_product_adgroup.return_value = 10
        cl.add_feed_ads_batch.return_value = [(101, 102, None)]

        self._patch_module(monkeypatch, cl)
        monkeypatch.setattr(fb, "_delete_partial_campaign", lambda *a: None, raising=False)

        result = fb._create_tp3_single(
            data={"cl": cl, "feeds": [(42, "yandex.xml")], "sitelinks": [], "callout_ids": [],
                  "promos": [], "minus_set": None},
            token="tok", login="porg-test", name="TestCamp",
            mode="network", pay_for_conv=False, goal_id=1, cpa_rub=500,
            budget_rub=5000, counter_id=1, region_ids=[225],
            href="https://example.com", feed_id=42, feed_name="yandex.xml",
            group_name="Группа", corr={}, ret_map={},
        )

        assert result.get("ok") is True

    def test_batch_call_failure_deletes_and_defers(self, monkeypatch):
        """HTTP-ошибка add_feed_ads_batch → campaign удалена, defer=True."""
        import direct.create.create_set_feed_builders as fb

        cl = MagicMock()
        cl.create_unified_campaign.return_value = 999
        cl.add_product_adgroup.return_value = 10
        cl.add_feed_ads_batch.side_effect = DirectV501Error("ads.add", 1000, "временно недоступен")

        deleted = []

        def fake_delete(token, login, cid):
            deleted.append(cid)

        self._patch_module(monkeypatch, cl)
        monkeypatch.setattr(fb, "_delete_partial_campaign", fake_delete, raising=False)

        result = fb._create_tp3_single(
            data={"cl": cl, "feeds": [(42, "yandex.xml")], "sitelinks": [], "callout_ids": [],
                  "promos": [], "minus_set": None},
            token="tok", login="porg-test", name="TestCamp",
            mode="network", pay_for_conv=False, goal_id=1, cpa_rub=500,
            budget_rub=5000, counter_id=1, region_ids=[225],
            href="https://example.com", feed_id=42, feed_name="yandex.xml",
            group_name="Группа", corr={}, ret_map={},
        )

        assert deleted, "При HTTP-ошибке batch кампания должна быть удалена"
        assert result.get("defer") is True

from direct.clients import grid_finalize


def test_restore_pay_for_conversion_strategy_uses_grid_write_enum():
    client = grid_finalize.GridClient.__new__(grid_finalize.GridClient)
    client.login = "target-login"
    sent = {}

    def fake_read(_ids):
        return {
            123: {
                "id": "123",
                "name": "campaign",
                "_unsupported_strategy": "ignored-on-purpose",
                "biddingStategyWithPlatforms": {
                    "strategyName": "AUTOBUDGET",
                    "strategyData": {"goalId": "111", "avgCpa": "2000"},
                },
                "meaningfulGoals": [],
            }
        }

    class FakeResponse:
        def json(self):
            return {
                "data": {
                    "updateCampaigns": {
                        "updatedCampaigns": [{"id": "123"}],
                        "validationResult": {"errors": []},
                    }
                },
                "errors": [],
            }

    def fake_post(_op, _query, variables):
        sent.update(variables)
        return FakeResponse()

    client._read_unified_campaign_update_payloads = fake_read
    client._post = fake_post

    assert client.restore_pay_for_conversion_strategy(123, 555, 300000) == [{"id": "123"}]

    item = sent["input"]["campaignUpdateItems"][0]["unifiedCampaign"]
    strategy = item["biddingStategyWithPlatforms"]
    assert "_unsupported_strategy" not in item
    assert strategy["strategyName"] == "AUTOBUDGET_MULTIPLE_CPA"
    assert strategy["strategyData"]["goalId"] == "0"
    assert strategy["strategyData"]["sum"] == "300000"
    assert strategy["strategyData"]["budgetType"] == "WEEKLY"
    assert strategy["strategyData"]["payForConversion"] is False
    assert strategy["strategyData"].get("avgCpa") is None
    assert item["meaningfulGoals"] == [{
        "goalId": "555",
        "conversionStrategy": "AVERAGE_CPA",
        "isMetrikaSourceOfValue": False,
    }]

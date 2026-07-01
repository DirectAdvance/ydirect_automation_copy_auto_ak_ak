"""
Промоакции Я.Директа через приватный grid/api (GraphQL) — публикация в кабинет клиента.

Зачем grid, а не v5: официальный `promotions.add` заблокирован (код 8000, ЕРИР-гейт).
Веб-интерфейс создаёт промо через grid/api — его и используем, на тех же куках
главпотока, что и мастер-кампании (direct/campaign.py → UacClient с .sess/.csrf).

⚠⚠ КРИТИЧНО: в URL grid/api ОБЯЗАТЕЛЕН `?ulogin=<login>`. Без него мутация уходит в
АГЕНТСКИЙ кабинет (промо создастся не у клиента!). После add — проверяем через
официальный OAuth `promotions.get` на клиенте (делает blueprint).

Порт референса .claude/skills/yandex-direct/creating_master_campaigns/promotions.py
под наш UacClient. Отличие: startDate/finishDate необязательны (промо без срока —
показывается всегда).
"""
from __future__ import annotations
import json

GRID = "https://direct.yandex.ru/web-api/grid/api"


class PromoClient:
    """Тонкая обёртка над cmc.UacClient (у него есть .sess, .csrf, _absorb_csrf)."""

    def __init__(self, uac_client, ulogin: str):
        self.c = uac_client
        self.ulogin = ulogin

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json; charset=utf-8",
             "Referer": "https://direct.yandex.ru/", "x-direct-api": "1"}
        if getattr(self.c, "csrf", None):
            h["x-csrf-token"] = self.c.csrf
        return h

    def gql(self, query: str, variables: dict | None = None, *, agency: bool = False) -> dict:
        url = GRID if agency else f"{GRID}?ulogin={self.ulogin}"
        r = self.c.sess.post(url, json={"query": query, "variables": variables or {}},
                             headers=self._headers(), timeout=60)
        if hasattr(self.c, "_absorb_csrf"):
            self.c._absorb_csrf(r)
        return r.json()

    def add(self, *, type: str, description: str, href: str,
            amount: int | None = None, unit: str | None = None, prefix: str | None = None,
            promocode: str | None = None, start: str | None = None,
            finish: str | None = None) -> tuple[int | None, str | None]:
        """Создать промоакцию в библиотеке клиента. → (id, error_text).

        description ≤ ~25 симв (длиннее → TEXT_LENGTH...). Для DISCOUNT/PROFIT:
        amount + unit (PCT/RUB) + prefix (TO=«до»). Name Директ собирает сам."""
        item = {"type": type, "description": description, "href": href}
        if amount is not None:
            item["amount"] = amount
        if unit:
            item["unit"] = unit
        if prefix:
            item["prefix"] = prefix
        if promocode:
            item["promocode"] = promocode
        if start:
            item["startDate"] = start
        if finish:
            item["finishDate"] = finish
        mut = ("mutation($input: GdAddPromoExtensionsInputInput!){"
               "addPromoExtensions(input:$input){mutationResults{id} "
               "validationResult{errors{code path}}}}")
        d = self.gql(mut, {"input": {"promoExtensions": [item]}})
        res = (d.get("data") or {}).get("addPromoExtensions") or {}
        mr = (res.get("mutationResults") or [{}])[0]
        if mr.get("id"):
            return int(mr["id"]), None
        errs = (res.get("validationResult") or {}).get("errors") or d.get("errors")
        return None, json.dumps(errs, ensure_ascii=False)[:300]

    def attach(self, promo_id: int, campaign_ids: list[int]) -> dict:
        """Привязать промо к кампаниям (updateCampaignsPromoExtension). Пока не используется в UI."""
        mut = ("mutation($input: GdUpdateCampaignsPromoExtensionInput!){"
               "updateCampaignsPromoExtension(input:$input){updatedCampaigns{id} "
               "validationResult{errors{code path}}}}")
        return self.gql(mut, {"input": {"campaignIds": [str(x) for x in campaign_ids],
                                        "promoExtensionId": int(promo_id)}})

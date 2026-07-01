"""
Промоакции (скидки/акции) Я.Директа через приватный grid/api (GraphQL).

Зачем отдельно: официальный API v5 `promotions.add` блокирован (`8000`, пустой detail —
ЕРИР-гейт). Веб-интерфейс создаёт промо через grid/api — его и используем, на тех же
куках главпотока, что и мастер-кампании (см. create_master_campaign.build_client).

⚠⚠ КРИТИЧНО: в URL grid/api ОБЯЗАТЕЛЕН `?ulogin=<login>`. Без него мутация уходит в
АГЕНТСКИЙ кабинет (промо создастся не у клиента!). Всегда проверяй результат через
официальный OAuth `promotions.get` на клиенте.

grid-интроспекция включена — любой метод/тип/enum достаётся так:
    {__schema{mutationType{fields{name}}}}                       # список мутаций
    {__type(name:"<Type>"){inputFields{name type{...}}}}         # поля input-типа
    {__type(name:"<Enum>"){enumValues{name}}}                    # значения enum

Использование:
    from create_master_campaign import build_client
    from promotions import PromoClient
    c = build_client("porg-XXXX"); c.link_info("https://site.ru")
    pc = PromoClient(c, "porg-XXXX")
    pid = pc.add(type="DISCOUNT", amount=45, unit="PCT", prefix="TO",
                 description="Новые авто", href="https://site.ru/quiz",
                 start="2026-06-18", finish="2026-07-31")
    pc.attach(pid, campaign_ids=[710809928, 710813618])
"""
from __future__ import annotations
import json

GRID = "https://direct.yandex.ru/web-api/grid/api"

# Значения enum (снято интроспекцией grid, 2026-06):
TYPES = ["DISCOUNT", "PROFIT", "CASHBACK", "GIFT", "FREE", "INSTALLMENT", "STOCK"]
UNITS = ["RUB", "PCT", "BYN", "USD", "EUR", "KZT", "TRY", "CHF", "UZS", "AED", "CNY", "UAH"]
PREFIXES = ["FROM", "TO"]   # TO = «до»


class PromoClient:
    """Тонкая обёртка над сессией UacClient для промоакций через grid/api."""

    def __init__(self, uac_client, ulogin: str):
        self.c = uac_client          # экземпляр UacClient (есть .sess и .csrf)
        self.ulogin = ulogin

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json; charset=utf-8",
             "Referer": "https://direct.yandex.ru/", "x-direct-api": "1"}
        if getattr(self.c, "csrf", None):
            h["x-csrf-token"] = self.c.csrf
        return h

    def gql(self, query: str, variables: dict | None = None, *, agency: bool = False) -> dict:
        """POST grid/api. agency=True → БЕЗ ulogin (агентский кабинет; нужно только для чистки своих ошибок)."""
        url = GRID if agency else f"{GRID}?ulogin={self.ulogin}"
        r = self.c.sess.post(url, json={"query": query, "variables": variables or {}},
                             headers=self._headers(), timeout=60)
        if hasattr(self.c, "_absorb_csrf"):
            self.c._absorb_csrf(r)
        return r.json()

    def introspect_mutations(self) -> list[str]:
        d = self.gql("query{__schema{mutationType{fields{name}}}}")
        return [f["name"] for f in d["data"]["__schema"]["mutationType"]["fields"]]

    def add(self, *, type: str, description: str, href: str, start: str, finish: str,
            amount: int | None = None, unit: str | None = None, prefix: str | None = None,
            promocode: str | None = None) -> int | None:
        """Создать промоакцию. Возвращает id или None (ошибка печатается).

        description ≤ ~25 символов (длиннее → TEXT_LENGTH...CANNOT_BE_MORE_THAN_MAX).
        Для DISCOUNT: amount + unit (PCT/RUB...) + prefix (TO=«до»). Name генерится сам.
        """
        item = {"type": type, "description": description, "href": href,
                "startDate": start, "finishDate": finish}
        if amount is not None:
            item["amount"] = amount
        if unit:
            item["unit"] = unit
        if prefix:
            item["prefix"] = prefix
        if promocode:
            item["promocode"] = promocode
        mut = ("mutation($input: GdAddPromoExtensionsInputInput!){"
               "addPromoExtensions(input:$input){mutationResults{id} "
               "validationResult{errors{code path}}}}")
        d = self.gql(mut, {"input": {"promoExtensions": [item]}})
        res = (d.get("data") or {}).get("addPromoExtensions") or {}
        mr = (res.get("mutationResults") or [{}])[0]
        if mr.get("id"):
            return int(mr["id"])
        errs = (res.get("validationResult") or {}).get("errors") or d.get("errors")
        print("promo.add failed:", json.dumps(errs, ensure_ascii=False))
        return None

    def attach(self, promo_id: int, campaign_ids: list[int]) -> dict:
        """Привязать промоакцию к кампаниям (updateCampaignsPromoExtension).
        CAMPAIGN_NOT_FOUND обычно = забыт ulogin (но здесь он зашит)."""
        mut = ("mutation($input: GdUpdateCampaignsPromoExtensionInput!){"
               "updateCampaignsPromoExtension(input:$input){updatedCampaigns{id} "
               "validationResult{errors{code path}}}}")
        return self.gql(mut, {"input": {"campaignIds": [str(x) for x in campaign_ids],
                                        "promoExtensionId": int(promo_id)}})

    def delete(self, promo_ids: list[int], *, agency: bool = False) -> dict:
        """Удалить промоакции. agency=True — если по ошибке создал в агентском (без ulogin)."""
        mut = ("mutation($input: GdDeletePromoExtensionsInputInput!){"
               "deletePromoExtensions(input:$input){mutationResults "
               "validationResult{errors{code path}}}}")  # mutationResults = [ID]! (без под-выборки)
        return self.gql(mut, {"input": {"ids": [int(x) for x in promo_ids]}}, agency=agency)

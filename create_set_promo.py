"""Post-create promo attach/create orchestration for create_set."""
from __future__ import annotations

from typing import Any, Callable

from .promo import PromoClient


PROMO_FIELDS = ["Id", "Type", "Name", "Description", "Amount", "AmountPrefix", "AmountUnit", "Promocode"]


def attach_or_create_promo(*, login: str, items: list[dict[str, Any]], results: list[dict[str, Any]],
                           token: str | None, client: Any, account: dict[str, Any],
                           site_type: str, agent: str, precreated_promo_id: int | None,
                           precreated_promo_note: str | None,
                           v5_get: Callable[..., dict[str, Any]],
                           promo_content_lines: Callable[[list[dict[str, Any]]], list[str]],
                           promo_usable_for_content: Callable[[dict[str, Any], list[str]], tuple[bool, str]],
                           create_account_promo_from_slepok: Callable[..., tuple[int | None, str]],
                           selected_slepok_key: Callable[[str], str]) -> str | None:
    """Attach a matching promo to created campaigns or create one from selected slepok."""
    # #5 review GAP B: tp2/tp4-возврат по куке несёт campaign_id без ключа id → раньше выпадал из
    # created_ids и промо к нему не привязывалось. Берём id ИЛИ campaign_id.
    created_ids = [(row.get("id") or row.get("campaign_id")) for row in results
                   if row.get("ok") and (row.get("id") or row.get("campaign_id"))]
    if not created_ids or not token:
        return None
    try:
        promo_client = PromoClient(client, login)
        if precreated_promo_id:
            promo_client.attach(precreated_promo_id, created_ids)
            return f"{precreated_promo_note}; привязано к {len(created_ids)} кампаниям"

        jp = v5_get("promotions", token, login, PROMO_FIELDS, criteria={})
        promos_all = [p for p in (jp.get("result") or {}).get("Promotions", []) if p.get("Id")]
        content_lines = promo_content_lines(items)
        usable_promos = []
        skipped_promos = []
        for promo in promos_all:
            ok, why = promo_usable_for_content(promo, content_lines)
            if ok:
                usable_promos.append(promo)
            else:
                skipped_promos.append((promo.get("Id"), why))
        if usable_promos:
            promo_client.attach(usable_promos[0]["Id"], created_ids)
            return (
                f"привязано промо аккаунта (id {usable_promos[0]['Id']}) к {len(created_ids)} кампаниям"
                + (f"; в аккаунте промо: {len(promos_all)}" if len(promos_all) > 1 else "")
                + (f"; пропущено кривых/конфликтных: {len(skipped_promos)}" if skipped_promos else "")
            )

        auto_slepok = selected_slepok_key(agent)
        pid, note = create_account_promo_from_slepok(
            client,
            login,
            token,
            {**account, "site_type": site_type},
            auto_slepok,
            content_lines,
        )
        if pid:
            promo_client.attach(pid, created_ids)
            base = "в аккаунте не было пригодных промо" if promos_all else "в аккаунте не было промо"
            return (
                f"{base}; {note}; привязано к {len(created_ids)} кампаниям"
                + (f"; пропущено кривых/конфликтных: {len(skipped_promos)}" if skipped_promos else "")
            )
        if promos_all:
            return (
                "промо аккаунта не привязаны: все конфликтуют с контентом или выглядят криво "
                f"({len(promos_all)}); автосоздание не выполнено: {note}"
            )
        return f"в аккаунте нет промо; автосоздание не выполнено: {note}"
    except Exception as exc:  # noqa: BLE001 - promo must not block campaign upload
        return f"промо не привязалось: {str(exc)[:140]}"

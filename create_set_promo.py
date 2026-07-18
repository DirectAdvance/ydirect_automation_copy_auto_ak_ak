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
                           selected_slepok_key: Callable[[str], str]) -> tuple[str | None, bool | None]:
    """Attach a matching promo to created campaigns or create one from selected slepok.

    Возвращает ``(note, account_has_promo_library)``. Второй элемент — tri-state признак
    «в БИБЛИОТЕКЕ аккаунта есть промо-акции», взятый из УЖЕ выполненного здесь v5
    ``promotions.get`` (строка ниже) — **без единого дополнительного запроса и балла**.
    Он прокидывается в live-верификатор как ступень 1 гейта ``PROMO_MISSING``:
    ``True`` — библиотека непуста (кампания без промо = дефект, в т.ч. случай 0/N, когда
    промо не доехало НИ ДО ОДНОЙ кампании), ``False`` — библиотека пуста (молчим),
    ``None`` — неизвестно (верификатор падает на свой прокси-фолбэк).
    """
    # #5 review GAP B: tp2/tp4-возврат по куке несёт campaign_id без ключа id → раньше выпадал из
    # created_ids и промо к нему не привязывалось. Берём id ИЛИ campaign_id.
    created_ids = [(row.get("id") or row.get("campaign_id")) for row in results
                   if row.get("ok") and (row.get("id") or row.get("campaign_id"))]
    if not created_ids or not token:
        return None, None
    try:
        promo_client = PromoClient(client, login)
        if precreated_promo_id:
            # precreate уже нашёл пригодное промо в библиотеке ИЛИ создал новое → библиотека непуста.
            promo_client.attach(precreated_promo_id, created_ids)
            return f"{precreated_promo_note}; привязано к {len(created_ids)} кампаниям", True

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
            ), True

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
            # Промо только что создано в библиотеке клиента → она непуста.
            return (
                f"{base}; {note}; привязано к {len(created_ids)} кампаниям"
                + (f"; пропущено кривых/конфликтных: {len(skipped_promos)}" if skipped_promos else "")
            ), True
        if promos_all:
            return (
                "промо аккаунта не привязаны: все конфликтуют с контентом или выглядят криво "
                f"({len(promos_all)}); автосоздание не выполнено: {note}"
            ), True
        return f"в аккаунте нет промо; автосоздание не выполнено: {note}", False
    except Exception as exc:  # noqa: BLE001 - promo must not block campaign upload
        return f"промо не привязалось: {str(exc)[:140]}", None

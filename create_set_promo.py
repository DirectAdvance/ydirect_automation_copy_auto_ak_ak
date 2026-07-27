"""Post-create promo attach/create orchestration for create_set."""
from __future__ import annotations

from typing import Any, Callable

from .promo import PromoClient


PROMO_FIELDS = ["Id", "Type", "Name", "Description", "Amount", "AmountPrefix", "AmountUnit", "Promocode"]

# Бизнес-правило: промоакция НЕЛЬЗЯ привязывать к МК (tp6) и Товарным кампаниям (tp7).
# Имена tp6/tp7 всегда начинаются с этих префиксов (см. create_set_plan._build_name).
_UAC_TP_PREFIXES = ("tp6_", "tp7_", "tp8_", "tp9_", "tp10_")


def _parse_attach_response(resp: dict, attempted: list) -> tuple[list[int], list, str]:
    """Parse updateCampaignsPromoExtension response → (confirmed_ids, errors, note_suffix).

    ``resp`` — сырой dict, который вернул ``PromoClient.attach()``.
    ``attempted`` — список id, переданных в attach (уже БЕЗ tp6/tp7).
    """
    if not isinstance(resp, dict):
        return [], [], f"привязка: нет ответа API ({len(attempted)} кампаний)"
    res = ((resp.get("data") or {}).get("updateCampaignsPromoExtension") or {})
    confirmed_ids = [int(c["id"]) for c in (res.get("updatedCampaigns") or []) if c.get("id")]
    errors = ((res.get("validationResult") or {}).get("errors") or
              resp.get("errors") or [])
    n_ok = len(confirmed_ids)
    n_att = len(attempted)
    if n_ok == n_att:
        suffix = f"привязано к {n_ok} кампаниям"
    elif n_ok:
        suffix = f"привязано к {n_ok} из {n_att} кампаний"
    else:
        suffix = f"привязка не подтверждена API ({n_att} попыток)"
    if errors:
        suffix += f"; ошибки API: {str(errors)[:120]}"
    return confirmed_ids, errors, suffix


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
    # Бизнес-правило: МК (tp6) и Товарные кампании (tp7) — прomo привязывать НЕЛЬЗЯ.
    # Имя кампании определяет тип: tp6_*/tp7_* — исключаем из привязки.
    created_ids = [(row.get("id") or row.get("campaign_id")) for row in results
                   if row.get("ok") and (row.get("id") or row.get("campaign_id"))
                   and not str(row.get("name") or "").startswith(_UAC_TP_PREFIXES)]
    if not created_ids or not token:
        return None, None
    try:
        promo_client = PromoClient(client, login)
        if precreated_promo_id:
            # precreate уже нашёл пригодное промо в библиотеке ИЛИ создал новое → библиотека непуста.
            _attach_resp = promo_client.attach(precreated_promo_id, created_ids)
            _, _, _attach_sfx = _parse_attach_response(_attach_resp, created_ids)
            return f"{precreated_promo_note}; {_attach_sfx}", True

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
            _attach_resp = promo_client.attach(usable_promos[0]["Id"], created_ids)
            _, _, _attach_sfx = _parse_attach_response(_attach_resp, created_ids)
            return (
                f"привязано промо аккаунта (id {usable_promos[0]['Id']}): {_attach_sfx}"
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
            _attach_resp = promo_client.attach(pid, created_ids)
            _, _, _attach_sfx = _parse_attach_response(_attach_resp, created_ids)
            base = "в аккаунте не было пригодных промо" if promos_all else "в аккаунте не было промо"
            # Промо только что создано в библиотеке клиента → она непуста.
            return (
                f"{base}; {note}; {_attach_sfx}"
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

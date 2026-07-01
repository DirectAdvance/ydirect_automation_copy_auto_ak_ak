"""tp2/tp4 текстовые кампании (Поиск / Поиск+Динамика) для Direct create_set.

Вынесено из `api_create_set` (blueprint.py). Решение #1 (Семёна): tp2/tp4 создаём ВСЕГДА по
cookie/Grid — только cookie-путь ставит relevanceMatch группы («Целевые запросы» EXACT_V2_MARK +
«Запросы без бренда» WITHOUT_BRAND), места показа, ключи/минуса/контент/корректировки. v5/v501
relevanceMatch не умеет. В исходной ветке v5-путь стоял ниже за `if True: ... continue` и был
недостижим («сохранён для справки») — при выносе он опущен как мёртвый код (история — в git).

После cookie-создания восстанавливаем привязку минусов УРОВНЯ КАМПАНИИ/общего набора для слепков
с режимом campaign-direct (pavlov/kryuchkova) / shared-set (scherbakova); best-effort — на 152
деградирует до групповых минусов, кампанию не валит. Helper'ы инъектируются (DI).
"""
from __future__ import annotations

from typing import Any, Callable, Optional


def run_create_set_text(*, it: dict[str, Any], name: str, tp_code: str,
                        # context
                        login: str, slepok: str, site_type: str, r_code: str, city: str,
                        href: str, region_ids: list[int], counter_id: int, goal_id: int,
                        st_token: str,
                        # content / rules
                        tpl_titles: list[str], tpl_texts: list[str], rs: dict[str, Any],
                        corr: Any, ret_map: Any, callouts: Any, callout_ids: Any,
                        job: Optional[dict[str, Any]],
                        # injected helpers (DI)
                        lines: Callable[[Any], list[str]],
                        num: Callable[..., Any],
                        create_text_via_cookie: Callable[..., dict[str, Any]],
                        slepok_minus_mode: dict[str, str],
                        apply_campaign_direct_minus: Callable[..., Any],
                        get_or_create_minus_set: Callable[..., Any],
                        attach_minus_set_to_text_campaign: Callable[..., Any],
                        add_job_err: Callable[..., Any],
                        bump_job: Callable[..., Any],
                        job_db_progress: Callable[[dict[str, Any]], Any],
                        bump_item: Callable[..., Any],
                        precreated_promo_id: Optional[int] = None,
                        ) -> list[dict[str, Any]]:
    """Создать tp2 (search_test) / tp4 (search_dynamic) по cookie. tp_code ∈ {'tp2','tp4'}."""
    results: list[dict[str, Any]] = []
    _it_pay = it.get("pay") or "cpa"
    cookie_kwargs = dict(
        login=login, name=name, tp_code=tp_code, counter_id=counter_id, goal_id=goal_id,
        cpa_rub=num(it.get("cpa"), rs["cpc_cpa"] if _it_pay == "tcpa" else rs["cpa"]),
        budget_rub=num(it.get("budget"), rs["cpc_budget"] if _it_pay == "tcpa" else rs["budget"]),
        region_ids=region_ids, href=href, slepok=slepok, site_type=site_type,
        r_code=r_code, titles=(lines(it.get("titles")) or tpl_titles),
        texts=(lines(it.get("texts")) or tpl_texts),
        pay=_it_pay, city=(city or ""), autotarget=bool(it.get("autotarget")),
        corr=corr, ret_map=ret_map,
        token=(st_token or ""), callout_texts=callouts,
        callout_ids=callout_ids,
        precreated_promo_id=precreated_promo_id,
    )
    res = create_text_via_cookie(**cookie_kwargs)
    # #8: cookie-путь ставит ГРУППОВЫЕ минуса (через пак). Для слепков с режимом campaign-direct
    # (pavlov/kryuchkova) / shared-set (scherbakova) восстанавливаем привязку минусов УРОВНЯ
    # КАМПАНИИ/общего набора (v5; работает при наличии баллов). Best-effort: на 152 деградирует
    # до групповых минусов, кампанию не валит.
    if res.get("ok") and res.get("campaign_id") and st_token:
        _mm = slepok_minus_mode.get(slepok, "group")
        try:
            if _mm == "campaign":
                _cd = apply_campaign_direct_minus(
                    st_token, login, res["campaign_id"], slepok, site_type, tp_code)
                res["minus_campaign_note"] = f"campaign-direct: {_cd}" if _cd else "campaign-direct OK"
            elif _mm == "shared_set":
                _msid = get_or_create_minus_set(st_token, login, slepok, site_type, tp_code)
                if _msid:
                    _mse = attach_minus_set_to_text_campaign(st_token, login, res["campaign_id"], _msid)
                    res["minus_set_id"] = _msid
                    res["minus_set_note"] = (f"shared-set {_msid} привязка упала: {_mse}"
                                             if _mse else f"shared-set {_msid} OK")
        except Exception as _me:  # noqa: BLE001 — минусовка best-effort, кампанию не валим
            res.setdefault("warnings", []).append(f"campaign/shared минусы упали: {str(_me)[:120]}")
    results.append(res)
    if not res.get("ok") and not res.get("defer"):   # defer (M3-пуст) — НЕ ошибка
        add_job_err(job, res)
    bump_job(job, bool(res.get("ok")))
    if job:
        job_db_progress(job)
    bump_item(job)
    return results

"""tp5/tp3 «Товарная галерея» creation branches for Direct create_set.

Вынесено из `api_create_set` (blueprint.py) без изменения поведения. Обе галереи (tp5
«Поиск+Товарная галерея» и tp3 «Товарная галерея РСЯ») делят один control-flow: cookie-путь
при 152/без токена, api-путь с api→cookie fallback на 152 и fan-out под-кампаний в плоский список.
Различие — tp_code, дефолты cpa/budget и функция создания. Helper'ы инъектируются (DI).
"""
from __future__ import annotations

from typing import Any, Callable, Optional


def run_create_set_gallery(*, kind: str, it: dict[str, Any], name: str,
                           # context
                           login: str, slepok: str, site_type: str, w_agency: str,
                           city: str, r_code: str, href: str, region_ids: list[int],
                           counter_id: int, goal_id: int,
                           # flags / creds
                           st_token: str, via_cookie: bool, no_cpa: bool, single_feed: bool,
                           grid_cookie: Any,
                           # content / rules
                           tpl_titles: list[str], tpl_texts: list[str], rs: dict[str, Any],
                           corr: Any, ret_map: Any, callouts: Any, callout_ids: Any,
                           job: Optional[dict[str, Any]],
                           # injected helpers (DI)
                           lines: Callable[[Any], list[str]],
                           num: Callable[..., Any],
                           create_tp5_campaign: Callable[..., dict[str, Any]],
                           create_tp3_campaign: Callable[..., dict[str, Any]],
                           create_shopping_via_cookie: Callable[..., dict[str, Any]],
                           units_in_result: Callable[[dict[str, Any]], bool],
                           add_job_err: Callable[..., Any],
                           bump_job: Callable[..., Any],
                           job_db_progress: Callable[[dict[str, Any]], Any],
                           bump_item: Callable[..., Any],
                           ) -> list[dict[str, Any]]:
    """Создать tp5 (search_gallery) или tp3 (rsya_gallery). kind ∈ {'tp5','tp3'}."""
    results: list[dict[str, Any]] = []
    # Дефолты cpa/budget по типу (tp5 — cpa/budget правил; tp3 — cpc-семейство).
    if kind == "tp5":
        cpa_val = num(it.get("cpa"), rs["cpa"])
        budget_val = num(it.get("budget"), rs["budget"])
    else:  # tp3
        cpa_val = num(it.get("cpa"), rs["cpc_cpa"])
        budget_val = num(it.get("budget"), rs.get("cpc_budget") or 0)
    body_text = ((lines(it.get("texts")) or tpl_texts or [""])[0]
                 if (it.get("texts") or tpl_texts) else "")
    cookie_kwargs = dict(
        login=login, name=name, tp_code=kind, counter_id=counter_id, goal_id=goal_id,
        cpa_rub=cpa_val, budget_rub=budget_val,
        region_ids=region_ids, href=href, agency=(w_agency or ""),
        body_text=body_text,
        feed_id=num(it.get("feed_id"), 0), corr=corr, ret_map=ret_map,
        token=(st_token or ""), slepok=slepok, site_type=site_type,
        city=(city or ""),                         # #7: нужен для cookie-фолбэка сайтлинков (_ai_common_sitelinks)
        feed_name=(it.get("feed_name") or ""),
        single_feed=single_feed,   # при feed_id=0 cookie-путь должен предпочесть /yandex.xml, а не первый фид
        callout_texts=callouts,
        callout_ids=callout_ids,
        ct=(it.get("ct") or "ct0000"), r_code=r_code,   # #5: кодер-имя группы tp5/tp3
    )

    # Согласие через попап (152) → товарная галерея по куке (HAR17).
    if via_cookie or not st_token:
        res = create_shopping_via_cookie(**cookie_kwargs)
        results.append(res)
        if not res.get("ok") and not res.get("defer"):   # defer (M3-пуст) — НЕ ошибка
            add_job_err(job, res)
        bump_job(job, bool(res.get("ok")))
        if job:
            job_db_progress(job)
        bump_item(job)
        return results

    try:
        if kind == "tp5":
            res = create_tp5_campaign(
                token=st_token, login=login, base_name=name,
                counter_id=counter_id, goal_id=goal_id,
                cpa_rub=cpa_val, budget_rub=budget_val,
                region_ids=region_ids, href=href, slepok=slepok,
                site_type=site_type, r_code=r_code, corr=corr, ret_map=ret_map,
                titles=lines(it.get("titles")) or tpl_titles,
                job=job, agency=(w_agency or ""),
                city=(city or ""),
                segment=it.get("tp5_segment"), autotarget=bool(it.get("autotarget")),
                products_only=bool(it.get("products_only")), no_cpa=no_cpa,
                single_feed=single_feed, grid_cookie=grid_cookie)
        else:  # tp3
            res = create_tp3_campaign(
                token=st_token, login=login, base_name=name,
                counter_id=counter_id, goal_id=goal_id,
                cpa_rub=cpa_val, budget_rub=budget_val,
                region_ids=region_ids, href=href, slepok=slepok,
                site_type=site_type, r_code=r_code, corr=corr, ret_map=ret_map,
                job=job, no_cpa=no_cpa, single_feed=single_feed, agency=(w_agency or ""))
        if (not res.get("ok")) and units_in_result(res):
            res = create_shopping_via_cookie(**cookie_kwargs)
            if res.get("ok"):
                res.setdefault("warnings", []).append("api->cookie fallback after 152")
        # FAN-OUT: разворачиваем под-кампании в плоский список (счётчики/промо/«K из N» консистентны)
        results.extend(res.get("campaigns") or [res])
    except Exception as e:  # noqa: BLE001
        results.append({"name": name, "ok": False, "error": str(e)[:240]})
        add_job_err(job, str(e)[:240])
        bump_job(job, False)
    bump_item(job)
    return results

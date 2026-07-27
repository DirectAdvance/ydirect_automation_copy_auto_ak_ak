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
                           deferred_save: Optional[Callable[..., Optional[str]]] = None,
                           next_units_reset_utc: Optional[Callable[[], Any]] = None,
                           units_alive: Optional[Callable[..., Any]] = None,
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
    # «Текст по умолчанию» ShoppingAd/ListingAd: единый переиспользуемый, заполненный под лимит.
    # НЕ берём texts[0] — там может быть аварийный короткий fallback (дефект psm5h7q6 2026-07-10).
    # Источник правды — SHOPPING_DEFAULT_TEXT (create_set_assets.py). Fail-safe: texts[0] если
    # константа по какой-то причине не импортируется (граница DI; на практике всегда импортируется).
    try:
        from .create_set_assets import SHOPPING_DEFAULT_TEXT as _SDT
        body_text = _SDT
    except Exception:  # noqa: BLE001
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
        # tp5 с сегментом (Марки/Модели/Общее) требует бренд-групп из M3-пака.
        # _create_shopping_via_cookie не принимает segment и создаёт одну generic
        # ct0000-группу «Товарная галерея» для ВСЕХ сегментов — тихий fallback маскирует
        # ошибку как «успех» (инцидент 2026-07-06: 5 одинаковых tp5 porg-psm5h7q6).
        # → явный провал NO_BRAND_SEGMENTS_AVAILABLE; retry нужен с API-токеном.
        # camp_names tp5 (only_gks/only_cts) — как сегментная: НЕ падать на cookie-ct0000-пустышку при 152.
        _seg5 = (it.get("tp5_segment") or it.get("only_gks") or it.get("only_cts")) if kind == "tp5" else None
        if _seg5:
            # РЕЗЮМ ТОКЕНОМ, но мы оказались на cookie-пути (пустой st_token или units-152 форсили
            # via_cookie). Куку НЕ пробуем (сегменты она не умеет → NO_BRAND повторялся бы вечно) и
            # deferred НЕ перепланируем: self-reference — дедуп в _deferred_save нашёл бы ЭТУ же
            # резюмящуюся строку (status='resumed', то же имя позиции) и вернул её id → финал джобы
            # пометил бы её done → сегментный tp5 теряется молча (инцидент 08.07: deferred
            # 721641cad7c1 / job 23677e1473d1, porg-psm5h7q6, сегменты Марки+Общее). Явный «отложено»
            # + флаг defer_keep → finalizer оставит строку waiting, демон повторит ТОКЕНОМ, когда
            # появятся агентский токен и баллы.
            if (job or {}).get("body", {}).get("_resume_via_token"):
                res = {"ok": False, "name": name, "defer_keep": True,
                       "error": (f"TOKEN_NOT_READY: сегментный tp5 «{_seg5}» ждёт агентский "
                                 "токен+баллы — докрутка отложена (куку не пробуем: сегменты "
                                 "требуют M3/API-токен)")}
                results.append(res)
                add_job_err(job, res)
                bump_job(job, False)
                if job:
                    job_db_progress(job)
                bump_item(job)
                return results
            # Первичный 152 в ОСНОВНОМ прогоне (в body ещё нет _resume_via_token) → планируем ПЕРВЫЙ
            # токен-ретрай: создаём новый deferred с _resume_via_token=True на сброс баллов.
            res = {"ok": False, "name": name,
                   "error": (f"NO_BRAND_SEGMENTS_AVAILABLE: tp5 сегмент «{_seg5}» "
                              "требует M3-пак — cookie-путь не поддерживает сегментацию; "
                              "переключитесь на API-токен (error 152 → retry позже)")}
            # Планируем retry ТОКЕНОМ на сброс суточного лимита баллов вместо бесконечного
            # повтора по куке (она сегменты не умеет, NO_BRAND_SEGMENTS_AVAILABLE повторится
            # вечно — Семён 2026-07-06). НЕ требуем st_token здесь (фикс 2026-07-06: в
            # добивочном контексте st_token бывал пуст → деферред НЕ создавался ВООБЩЕ, tp5
            # терялся молча — прогон 12:24Z df7f70e7605f/d342e768ae87): resume-демон сам
            # резолвит токен через _token_for_login на момент докрутки.
            if deferred_save and job and job.get("body"):
                try:
                    _def_body = dict(job["body"])
                    _def_body["items"] = [it]
                    _def_body["_resume_via_token"] = True   # резюм пойдёт ТОКЕНОМ, не по куке
                    _def_body.pop("via_cookie", None)       # иначе резюм опять форсит куку
                    # Токен-ретрай — НОВАЯ цепочка: не наследуем указатель на родительскую (cookie)
                    # deferred-строку, иначе финал той джобы пометил бы уже НАШУ строку done.
                    _cur_did = _def_body.pop("_deferred_id", None)   # id текущей резюмящейся строки (если резюм)
                    # Семён 2026-07-07 (никаких ночных отложек): «баллы первичны» — если баллы ЖИВЫ,
                    # добиваем сегментный tp5 ТОКЕНОМ СРАЗУ (resume_at=None → now(), демон ~2 мин).
                    # Реальный 152 (баллы исчерпаны) — только тогда ждём сброс (физич. невозможность).
                    _alive = units_alive(login, (w_agency or "")) if units_alive else None
                    if _alive:
                        _resume_at = None
                    else:
                        _resume_at = next_units_reset_utc().isoformat() if next_units_reset_utc else None
                    # resume_count НАСЛЕДУЕМ (ревью 06.07): хардкод 0 обнулял счётчик на каждом
                    # цикле → _RESUME_MAX никогда не срабатывал → вечный суточный цикл деферредов
                    # у аккаунтов, где токен так и не находится.
                    _rc_gr = int(_def_body.get("_resume_count") or 0)
                    _rid = deferred_save(login, (w_agency or _def_body.get("agency") or ""),
                                         _def_body, [it], job.get("_id") or job.get("job_id"),
                                         resume_count=_rc_gr, resume_at=_resume_at,
                                         exclude_id=_cur_did)   # не self-reference на резюмящуюся строку
                    if _rid:
                        res["error"] += f" — докрутка токеном запланирована ({_rid})"
                        res["deferred_no_cookie"] = _rid
                    else:
                        # deferred_save вернул None (ошибка БД проглочена внутри) — НЕ молчим:
                        # иначе tp5 теряется без следа (инцидент 2026-07-06).
                        res["error"] += " — ⚠️ деферред НЕ создан (deferred_save=None), пункт потерян"
                except Exception as _de:  # noqa: BLE001 — планирование best-effort, отказ уже записан
                    res["error"] += f" — ⚠️ деферред НЕ создан ({str(_de)[:80]})"
            else:
                res["error"] += " — ⚠️ деферред НЕ создан (нет job/body в контексте)"
            results.append(res)
            add_job_err(job, res)
            bump_job(job, False)
            if job:
                job_db_progress(job)
            bump_item(job)
            return results
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
                keep_keywords=bool(it.get("autotarget_keep_keywords")),
                products_only=bool(it.get("products_only")), no_cpa=no_cpa,
                single_feed=single_feed, grid_cookie=grid_cookie,
                only_gks=(set(it.get("only_gks") or ()) or None),
                only_cts=(set(it.get("only_cts") or ()) or None),
                all_feeds=bool(it.get("tp5_all_feeds")))
        else:  # tp3
            res = create_tp3_campaign(
                token=st_token, login=login, base_name=name,
                counter_id=counter_id, goal_id=goal_id,
                cpa_rub=cpa_val, budget_rub=budget_val,
                region_ids=region_ids, href=href, slepok=slepok,
                site_type=site_type, r_code=r_code, corr=corr, ret_map=ret_map,
                job=job, no_cpa=no_cpa, single_feed=single_feed, agency=(w_agency or ""),
                only_cts=(set(it.get("only_cts") or ()) or None),
                only_gks=(set(it.get("only_gks") or ()) or None),
                all_feeds=bool(it.get("tp3_all_feeds")))
        # camp_names tp5 (only_gks/only_cts) — как сегментная: НЕ падать на cookie-ct0000-пустышку при 152.
        _seg5 = (it.get("tp5_segment") or it.get("only_gks") or it.get("only_cts")) if kind == "tp5" else None
        if (not res.get("ok")) and units_in_result(res) and not _seg5:
            # tp5 без сегмента (products_only/Фиды) — обычный fallback на cookie.
            res = create_shopping_via_cookie(**cookie_kwargs)
            if res.get("ok"):
                res.setdefault("warnings", []).append("api->cookie fallback after 152")
        elif (not res.get("ok")) and units_in_result(res) and _seg5:
            # сегментная tp5 (Марки/Модели/Общее): fallback на cookie создал бы
            # ct0000-пустышку — НЕ делаем. 152 propagates as-is; retry с API-токеном.
            pass
        # FAN-OUT: разворачиваем под-кампании в плоский список (счётчики/промо/«K из N» консистентны)
        results.extend(res.get("campaigns") or [res])
    except Exception as e:  # noqa: BLE001
        results.append({"name": name, "ok": False, "error": str(e)[:240]})
        add_job_err(job, str(e)[:240])
        bump_job(job, False)
    bump_item(job)
    return results

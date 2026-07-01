"""tp1 РСЯ (ЕПК v501 network_cpa) creation branch for Direct create_set.

Вынесено из `api_create_set` (blueprint.py) без изменения поведения: одна pure-функция
`run_create_set_tp1(...)` строит tp1_rsy/tp1_shopping с fan-out по фидам аккаунта.
Helper'ы Direct/Grid/прогресса инъектируются вызовом (DI) — модуль тестируется без Flask/Direct.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from .create_set_resume import already_in_direct, force_recreate


def _skip_domain_fname(f_name: str, href: str) -> bool:
    """True если f_name совпадает с hostname аккаунта → не добавлять в имя кампании.
    Защищает от вставки домена (напр. «autos-kemerovo.site») вместо контентного имени фида."""
    if not f_name or not href:
        return False
    nm = f_name.strip().lower()
    if nm.startswith("www."):
        nm = nm[4:]
    h = href.lower()
    for pfx in ("https://www.", "http://www.", "https://", "http://"):
        if h.startswith(pfx):
            h = h[len(pfx):]
            break
    host = h.split("/")[0].split("?")[0]
    return bool(host and nm == host)


def run_create_set_tp1(*, it: dict[str, Any], name: str,
                       # account/context
                       login: str, slepok: str, site_type: str, w_agency: str,
                       city: str, r_code: str, href: str, region_ids: list[int],
                       counter_id: int, goal_id: int,
                       # flags / creds
                       st_token: str, via_cookie: bool, no_cpa: bool, single_feed: bool,
                       grid_cookie: Any,
                       # content / templates / rules
                       tpl_titles: list[str], tpl_texts: list[str], rs: dict[str, Any],
                       corr: Any, ret_map: Any, callouts: Any, callout_ids: Any,
                       # resume-skip state
                       existing_names: set, repair_force_names: set,
                       job: Optional[dict[str, Any]],
                       # injected helpers (DI)
                       lines: Callable[[Any], list[str]],
                       num: Callable[..., Any],
                       slepok_uses_shopping: Callable[[str, str], bool],
                       account_model_feeds: Callable[[str, str], list[dict[str, Any]]],
                       first_url_feed: Callable[[str, str, str], Any],
                       create_tp1_via_cookie: Callable[..., dict[str, Any]],
                       create_tp1_campaign: Callable[..., dict[str, Any]],
                       units_in_result: Callable[[dict[str, Any]], bool],
                       apply_corrections: Callable[..., tuple[int, Any]],
                       job_db_progress: Callable[[dict[str, Any]], Any],
                       add_job_err: Callable[..., Any],
                       bump_job: Callable[..., Any],
                       bump_item: Callable[..., Any],
                       ) -> list[dict[str, Any]]:
    """Создать tp1_rsy/tp1_shopping (fan-out по фидам). Возвращает список result-записей."""
    results: list[dict[str, Any]] = []
    if not st_token and not via_cookie:        # куки-путь токен v5 не использует
        results.append({"name": name, "ok": False,
                        "error": "нет рабочего агентского токена для создания tp1"})
        return results
    it_texts = lines(it.get("texts")) or tpl_texts
    cpc_cpa_val = num(it.get("cpa"), rs["cpc_cpa"])    # целевой CPA из правил
    tp1_budget = int(num(it.get("budget"), rs.get("cpc_budget") or 0))
    # Товарные/динамика в группах tp1 — как в слепке (Щербакова): нужен XML-фид аккаунта.
    # Смарт-Баннер/Фиды (products_only) — товарные ОБЯЗАТЕЛЬНЫ (это их суть), форсим shopping.
    tp1_products_only = bool(it.get("products_only"))
    tp1_shopping = slepok_uses_shopping(slepok, "tp1") or tp1_products_only
    # FAN-OUT (CODER.md: фидовые tp мультиплицируются по ВСЕМ фидам аккаунта, имя += фид).
    # Товарные с фильтром по модели — фиды С модельными коллекциями (listings 'model_N').
    tp1_mf = account_model_feeds(login, w_agency or "") if tp1_shopping else []
    if tp1_shopping and tp1_mf:
        feed_variants = [(int(f["id"]), f.get("name") or "", f.get("models")) for f in tp1_mf]
    elif tp1_shopping:
        ff = first_url_feed(st_token, login, w_agency or "")   # фид (v5 или по куке) без модельных коллекций
        feed_variants = [(int(ff), "", None)] if ff else [(0, "", None)]
    else:
        feed_variants = [(0, "", None)]                # tp1 без товарных (не Щербакова) → одна РСЯ
    if single_feed and feed_variants:
        feed_variants = feed_variants[:1]             # «по одному фиду» — только первый
    multi = sum(1 for fv in feed_variants if fv[0]) > 1
    for f_id, f_name, f_models in feed_variants:
        if job and job.get("cancel"):            # отмена: стоп ПЕРЕД следующим фидом fan-out
            break                                 # (текущая фид-кампания уже достроена)
        f_shopping = tp1_shopping and bool(f_id)
        # Имя кампании несёт название фида (мультиплицирование по фидам).
        # ФИКС C: пропускаем f_name если он совпадает с доменом аккаунта (не контентный суффикс). (#ФИКС-C)
        nm = f"{name} — {f_name}" if (f_name and not _skip_domain_fname(f_name, href) and (multi or f_shopping)) else name
        # RESUME-SKIP ПОФИДОВО: эта фид-кампания уже есть в Директе → пропускаем именно её
        # (а не весь пункт), чтобы недосозданные фиды дозаполнились при докрутке.
        if already_in_direct(nm, existing_names) and not force_recreate(nm, repair_force_names):
            results.append({"ok": True, "name": nm, "skipped": True,
                            "note": "фид-кампания уже создана — пропущена при докрутке"})
            if job:
                job_db_progress(job)   # skipped не бампает created/failed (см. skipped_existing)
            continue
        try:
            _tp1_cookie_kwargs = dict(
                login=login, name=nm, counter_id=counter_id, goal_id=goal_id,
                cpc_cpa=cpc_cpa_val, region_ids=region_ids, href=href, slepok=slepok,
                site_type=site_type, r_code=r_code,
                titles=lines(it.get("titles")) or tpl_titles, texts=it_texts,
                budget_rub=tp1_budget, segment=it.get("tp1_segment"),
                ai_title2=(it.get("title2") or ""), city=(city or ""),
                autotarget=bool(it.get("autotarget")), no_cpa=no_cpa,
                token=st_token or "", corr=corr, ret_map=ret_map,
                callout_texts=callouts,
                callout_ids=callout_ids,
                sitelinks=(it.get("sitelinks") if isinstance(it.get("sitelinks"), list) else None),
                feed_id=f_id, with_shopping=f_shopping, feed_models=f_models,
                job=job,                          # отмена: проверка cancel между cpc/cpa внутри
            )
            if via_cookie or not st_token:
                # Cookie-first: либо принудительно, либо токена нет вовсе.
                res = create_tp1_via_cookie(**_tp1_cookie_kwargs)
            else:
                res = create_tp1_campaign(
                    token=st_token, login=login, name=nm,
                    counter_id=counter_id, goal_id=goal_id, cpc_cpa=cpc_cpa_val,
                    region_ids=region_ids, href=href, slepok=slepok,
                    site_type=site_type, r_code=r_code,
                    titles=lines(it.get("titles")) or tpl_titles, texts=it_texts,
                    feed_id=f_id, with_shopping=f_shopping, feed_models=f_models,
                    budget_rub=tp1_budget, segment=it.get("tp1_segment"),
                    ai_title2=(it.get("title2") or ""),
                    sitelinks=(it.get("sitelinks") if isinstance(it.get("sitelinks"), list) else None),
                    callout_texts=callouts,
                    callout_ids=callout_ids,
                    city=(city or ""),
                    autotarget=bool(it.get("autotarget")),
                    products_only=tp1_products_only,
                    no_cpa=no_cpa,
                    grid_cookie=grid_cookie,
                    job=job,                      # отмена: проверка cancel между cpc/cpa
                )
                if (not res.get("ok")) and units_in_result(res):
                    res = create_tp1_via_cookie(**_tp1_cookie_kwargs)
                    if res.get("ok"):
                        res.setdefault("warnings", []).append("api->cookie fallback after 152")
            # Применяем корректировки ставок к созданной кампании (через v5 — стоит баллов;
            # на куки-пути при 152 пропускаем, иначе вызов всё равно упадёт на лимите).
            if res.get("ok") and not via_cookie:
                _targets = [c for c in (res.get("campaigns") or []) if c.get("campaign_id")]
                if not _targets and res.get("campaign_id"):
                    _targets = [res]
                _mods_total = 0
                _mods_warns = []
                for _t in _targets:
                    nmod, cerr = apply_corrections(st_token, login, _t["campaign_id"], corr, ret_map)
                    _t["modifiers_set"] = nmod
                    _mods_total += nmod
                    if cerr:
                        _t.setdefault("warnings", []).append(f"корректировки упали: {cerr}")
                        _mods_warns.append(cerr)
                res["modifiers_set"] = _mods_total
                if _mods_warns:
                    for _mw in dict.fromkeys(_mods_warns):
                        res.setdefault("warnings", []).append(f"корректировки упали: {_mw}")
                res["id"] = res.get("campaign_id")      # совместимость с created_ids (промо)
            if f_name:
                res["feed"] = f_name
            results.append(res)
            if not res.get("ok") and not res.get("defer"):   # defer (M3-пуст) — НЕ ошибка
                add_job_err(job, res)
            bump_job(job, bool(res.get("ok")))   # live-счётчик: каждая РСЯ-копия по фиду
        except Exception as e:  # noqa: BLE001
            results.append({"name": nm, "ok": False, "error": str(e)[:240]})
            add_job_err(job, str(e)[:240])
            bump_job(job, False)
        if job:
            job_db_progress(job)
    bump_item(job)                               # item завершён (все фиды обработаны)
    return results

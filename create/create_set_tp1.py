"""tp1 РСЯ (ЕПК v501 network_cpa) creation branch for Direct create_set.

Вынесено из `api_create_set` (blueprint.py) без изменения поведения: одна pure-функция
`run_create_set_tp1(...)` строит tp1_rsy/tp1_shopping с fan-out по фидам аккаунта.
Helper'ы Direct/Grid/прогресса инъектируются вызовом (DI) — модуль тестируется без Flask/Direct.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from .create_set_resume import already_in_direct, force_recreate


def _feed_name_label(f_name: str, href: str) -> str:
    """Метка фида для имени кампании: домен аккаунта срезан, остаток — контентная часть.

    Было `_skip_domain_fname` — резал только ТОЧНОЕ равенство имени хосту, а в кабинете имя
    фида идёт с доменом-ПРЕФИКСОМ («carsklad-126.site — yandex-catalog-model-…») → домен
    проезжал в имя кампании. Пусто на выходе = имя было только доменом → суффикс не добавлять.
    """
    from ..model_urls import _strip_site_domain_label
    return _strip_site_domain_label(f_name, href)


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
                       auth_error_in_result: Optional[Callable[[dict[str, Any]], bool]] = None,
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
    # Сортируем по убыванию длины: texts[0] → самый длинный → лучший «Текст по умолчанию»
    # для ShoppingAd (set_default_text в create_set_tp1_builders, лимит 81 симв).
    # _rsya_texts для текстовых объявлений переупорядочивает по своей логике → порядок не важен.
    it_texts = sorted(lines(it.get("texts")) or tpl_texts, key=len, reverse=True)
    cpc_cpa_val = num(it.get("cpa"), rs["cpc_cpa"])    # целевой CPA из правил
    tp1_budget = int(num(it.get("budget"), rs.get("cpc_budget") or 0))
    # Товарные/динамика в группах tp1 — как в слепке (Щербакова): нужен XML-фид аккаунта.
    # Смарт-Баннер/Фиды (products_only) — товарные ОБЯЗАТЕЛЬНЫ (это их суть), форсим shopping.
    tp1_products_only = bool(it.get("products_only"))
    # camp_names-маршрутизация (задача 7): группы кампании из structure_to_campaigns (gk/ct set).
    # Пусто → прежнее сегмент-поведение (segment-фильтр по tp1_segment).
    _only_gks = set(it.get("tp1_only_gks") or ()) or None
    _only_cts = set(it.get("tp1_only_cts") or ()) or None
    # tp1_catalog: тег «каталоги» вручную назначен на кампанию → форсить ShoppingAd+ListingAd.
    # Catalog-role гейт (catalog_only=True в account_model_feeds) блокирует лендинг/оффер-фиды — guard цел.
    # ПРАВИЛО: shopping-решение принимается per-кампания (tp1_catalog), а НЕ по общему slepok-флагу.
    # slepok_uses_shopping("tp1") специально убран: он форсил shopping на ВСЕ tp1 слепка-Щербакова
    # независимо от tp1_catalog, из-за чего «Комби» (без Фид, tp1_catalog=None) получал 3 объявл. вместо 1.
    tp1_shopping = tp1_products_only or bool(it.get("tp1_catalog"))
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
    # «Все фиды» (тег all_feeds): collapse fan-out → ОДНА кампания, group PER feed внутри.
    # Применяем ДО single_feed-коллапса: all_feeds и single_feed взаимоисключают (all_feeds = все фиды).
    _tp1_all_feeds = bool(it.get("tp1_all_feeds")) and tp1_shopping and not single_feed
    _all_feeds_list: list | None = None
    if _tp1_all_feeds and feed_variants:
        # Все разрешённые фиды в ONE кампанию; feed_id=0, with_shopping через all_feeds_list
        _all_feeds_list = [(int(fv[0]), str(fv[1]) if len(fv) > 1 and fv[1] else "")
                           for fv in feed_variants if fv[0]]
        feed_variants = [(0, "", None)]  # ONE iteration, no per-feed name suffix
    if single_feed and feed_variants:
        # «Профильные фиды» → tp1-товарка по { yandex.xml, yandex-used-auto.xml } ∩ аккаунт.
        # Для tp1 catalog_only-список может не содержать сырой yandex.xml (только model-фиды),
        # поэтому каждый профильный фид находим через first_url_feed(strict=True, url_key=pk).
        if tp1_shopping:
            from .create_set_input import PROFILE_FEED_KEYS
            _profile_fvs: list = []
            for _pk in PROFILE_FEED_KEYS:
                _ff = first_url_feed(st_token, login, w_agency or "", strict=True, url_key=_pk)
                if _ff and not any(int(fv[0]) == int(_ff) for fv in _profile_fvs):
                    _profile_fvs.append((int(_ff), _pk, None))
            if _profile_fvs:
                feed_variants = _profile_fvs
            else:
                from .create_set_input import prefer_single_feed_variants
                feed_variants = prefer_single_feed_variants(feed_variants)
    multi = sum(1 for fv in feed_variants if fv[0]) > 1
    for f_id, f_name, f_models in feed_variants:
        if job and job.get("cancel"):            # отмена: стоп ПЕРЕД следующим фидом fan-out
            break                                 # (текущая фид-кампания уже достроена)
        f_shopping = tp1_shopping and bool(f_id)
        # Имя кампании несёт название фида (мультиплицирование по фидам).
        # ФИКС C: домен аккаунта в суффикс не пускаем — срезаем его из метки (не только точное равенство).
        _f_label = _feed_name_label(f_name, href)
        nm = f"{name} — {_f_label}" if (_f_label and (multi or f_shopping)) else name
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
                keep_keywords=bool(it.get("autotarget_keep_keywords")),
                only_gks=_only_gks, only_cts=_only_cts,
                token=st_token or "", corr=corr, ret_map=ret_map,
                callout_texts=callouts,
                callout_ids=callout_ids,
                sitelinks=(it.get("sitelinks") if isinstance(it.get("sitelinks"), list) else None),
                feed_id=f_id, with_shopping=f_shopping, feed_models=f_models,
                products_only=tp1_products_only,
                job=job,                          # отмена: проверка cancel между cpc/cpa внутри
                all_feeds_list=_all_feeds_list,   # «все фиды»: per-feed shopping groups в одной кампании
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
                    keep_keywords=bool(it.get("autotarget_keep_keywords")),
                    products_only=tp1_products_only,
                    no_cpa=no_cpa,
                    grid_cookie=grid_cookie,
                    job=job,                      # отмена: проверка cancel между cpc/cpa
                    only_gks=_only_gks, only_cts=_only_cts,
                    all_feeds_list=_all_feeds_list,  # «все фиды»
                )
                if (not res.get("ok")) and (
                    units_in_result(res)
                    or (auth_error_in_result and auth_error_in_result(res))
                ):
                    res = create_tp1_via_cookie(**_tp1_cookie_kwargs)
                    if res.get("ok"):
                        res.setdefault("warnings", []).append("api->cookie fallback after 152/53")
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

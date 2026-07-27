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
                        create_text_via_token: Optional[Callable[..., dict[str, Any]]] = None,
                        api_first: bool = False,
                        via_cookie: bool = False,
                        ) -> list[dict[str, Any]]:
    """Создать tp2 (search_test) / tp4 (search_dynamic). tp_code ∈ {'tp2','tp4'}.

    Маршрут (Решение #B, DIRECT_API_FIRST):
    - OFF (api_first=False) ИЛИ via_cookie ИЛИ нет токена → cookie/Grid (без баллов) — прежнее поведение.
    - ON (api_first=True) + не-via_cookie + есть токен → ТОКЕН/API (баллы) через create_text_via_token;
      на реальном 152 / сбое API токен-путь возвращает ok=False (+удаляет недоделанную РК) → грациозный
      фолбэк на _create_text_via_cookie (набор не валим)."""
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
        keep_keywords=bool(it.get("autotarget_keep_keywords")),
        # Сегмент (Марки/Модели/Общее) плана хранится под ключом "tp4_segment" для ОБОИХ tp2/tp4
        # (create_set_plan.py:413,475) — раньше здесь терялся (не читался из it вообще), из-за чего
        # _pack_groups_with_retry строил группы БЕЗ фильтра по сегменту → марки и модели попадали
        # в ОДНУ и ту же кампанию вперемешку (живой баг 2026-07-06, porg-lzjk6p5m/terehov; у tp1 тот
        # же segment корректно прокидывается через _create_tp1_via_cookie, см. create_set_tp1_builders.py).
        segment=it.get("tp4_segment"),
        # only_cts (split-driven слепки, напр. dmp/tp2): ct-коды ИМЕННО этого split-блока из плана
        # (create_set_plan: "tp2_split_cts"). None для авто-слепков → поведение сегмент-пути неизменно.
        # Без него cookie-путь (_tp1_pack_groups) брал весь пул слепка (34 ct) в КАЖДУЮ из 4 кампаний.
        # Задача 7: camp_names-кампании передают маршрутизацию через generic only_cts/only_gks
        # (fallback на tp2_split_cts — dmp split-путь). only_gks — per-adgroup (token-путь).
        only_cts=(it.get("only_cts") or it.get("tp2_split_cts")),
        only_gks=(set(it.get("only_gks") or ()) or None),
        corr=corr, ret_map=ret_map,
        token=(st_token or ""), callout_texts=callouts,
        callout_ids=callout_ids,
        precreated_promo_id=precreated_promo_id,
    )
    # Роутинг token-vs-cookie. token_kwargs == cookie_kwargs (одинаковая сигнатура функций).
    _use_token = bool(api_first and not via_cookie and st_token and callable(create_text_via_token))
    if _use_token:
        res = create_text_via_token(**cookie_kwargs)
        if not res.get("ok"):
            # token 152 / сбой API → грациозный фолбэк на куку (недоделанная РК уже удалена token-путём).
            from .create_set_units import is_units_exhausted as _is_units
            # ВАЖНО (FALSE_152_DEFER_LABEL): units-меткой считаем ТОЛЬКО реальный 152 в тексте ошибки.
            # res.get("defer") ставится и на пустой/недоступный контент-пак ("пак пуст" — НЕ 152), и его
            # НЕЛЬЗЯ приравнивать к units — иначе orchestrator накопит ложный 152-streak и напишет
            # «Баллы коммандера исчерпаны». defer при этом по-прежнему уводит пункт в докрутку ниже
            # (логика defer/докрутки не тронута — меняется только МЕТКА units).
            _tok_units = bool(_is_units(res.get("error")))
            res = create_text_via_cookie(**cookie_kwargs)
            if _tok_units:
                # Метка для orchestrator: token упёрся в РЕАЛЬНЫЕ баллы (даже если кука добила ok) —
                # считается в строгий 152-streak (после N подряд весь остаток набора уходит на куку).
                res["token_units_fallback"] = True
    else:
        res = create_text_via_cookie(**cookie_kwargs)
    # #8: cookie-путь ставит минуса УРОВНЯ КАМПАНИИ через spec (create_set_feed_builders:
    # "minus_keywords" ВСЕГДА = _enabled_minus_words(), Grid-cookie без баллов).
    # campaign-mode: v5 _apply_campaign_direct_minus УБРАН (дубль поверх spec → против бюджета 20k).
    # shared-set (scherbakova): Grid libraryMinusKeywordsIds — привязка через _finalize_search_via_grid.
    if res.get("ok") and res.get("campaign_id") and st_token:
        _mm = slepok_minus_mode.get(slepok, "group")
        try:
            if _mm == "shared_set":
                # Grid-путь (libraryMinusKeywordsIds) уже выполнен внутри _create_text_via_cookie
                # через _finalize_search_via_grid. v5 NegativeKeywordSharedSetIds отклоняется
                # для ЕПК-кампаний («неизвестный параметр») — пропускаем если Grid уже сработал.
                _grid_minus = ((res.get("search_finalized") or {}).get("minus_set_grid") or [])
                if _grid_minus:
                    res["minus_set_id"] = _grid_minus[0]
                    res["minus_set_note"] = f"shared-set {_grid_minus[0]} OK (Grid)"
                elif st_token:
                    # Grid не нашёл набор по имени → пробуем v5 как fallback
                    _msid = get_or_create_minus_set(st_token, login, slepok, site_type, tp_code, city=city)
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

"""Create-set orchestration for Direct automation."""

from __future__ import annotations

from flask import jsonify, request

from . import campaign as cmc
from . import grid_finalize as gf


def create_set_response(deps: dict):
    """Создание набора кампаний через UAC-движок. Только переданные items.
    ⛔ ПРАВИЛО: ВСЕ кампании создаются ТОЛЬКО ЧЕРНОВИКАМИ (launch принудительно False для всех типов,
    включая UAC tp6/tp7). Сервис НИКОГДА не публикует автоматически — публикация = ручной шаг в
    Директе после проверки. Кнопка «Создать и опубликовать» отличается лишь источником контента
    (M3/ИИ поитемно, stream_content), но РК всё равно DRAFT.
    content_source='slepok_library' → контент из direct_slepok_content (БД-слепок) вместо M3/ИИ."""
    _CALLOUT_PER_CAMPAIGN_CAP = deps['_CALLOUT_PER_CAMPAIGN_CAP']
    _CONTENT_CACHE = deps['_CONTENT_CACHE']
    _CONTENT_CACHE_LOCK = deps['_CONTENT_CACHE_LOCK']
    _CREATE_JOBS = deps['_CREATE_JOBS']
    _RESUME_MAX = deps['_RESUME_MAX']
    _SLEPOK_MINUS_MODE = deps['_SLEPOK_MINUS_MODE']
    _TOKEN_ONLY_TYPES = deps['_TOKEN_ONLY_TYPES']
    _account_ctx = deps['_account_ctx']
    _account_model_feeds = deps['_account_model_feeds']
    _account_retargeting = deps['_account_retargeting']
    _add_job_err = deps['_add_job_err']
    _apply_campaign_direct_minus = deps['_apply_campaign_direct_minus']
    _apply_corrections = deps['_apply_corrections']
    _attach_minus_set_to_text_campaign = deps['_attach_minus_set_to_text_campaign']
    _attach_post_repair_verification = deps['_attach_post_repair_verification']
    _bump_item = deps['_bump_item']
    _bump_job = deps['_bump_job']
    _busy_response = deps['_busy_response']
    _cached_campaign_content = deps['_cached_campaign_content']
    _callout_semantic_key = deps['_callout_semantic_key']
    _content_cache_key = deps['_content_cache_key']
    _content_copy = deps['_content_copy']
    _counter_foreign_owner = deps['_counter_foreign_owner']
    _create_account_promo_from_slepok = deps['_create_account_promo_from_slepok']
    _create_set_live_verification = deps['_create_set_live_verification']
    _create_shopping_via_cookie = deps['_create_shopping_via_cookie']
    _create_text_via_cookie = deps['_create_text_via_cookie']
    _create_tp1_campaign = deps['_create_tp1_campaign']
    _create_tp1_via_cookie = deps['_create_tp1_via_cookie']
    _create_tp3_campaign = deps['_create_tp3_campaign']
    _create_tp5_campaign = deps['_create_tp5_campaign']
    _dedup_callouts = deps['_dedup_callouts']
    _deferred_save = deps['_deferred_save']
    _deferred_set_status = deps['_deferred_set_status']
    _first_url_feed = deps['_first_url_feed']
    _get_or_create_minus_set = deps['_get_or_create_minus_set']
    _goal_vse_formy = deps['_goal_vse_formy']
    _grid_list_campaigns = deps['_grid_list_campaigns']
    _ints = deps['_ints']
    _job_db_progress = deps['_job_db_progress']
    _lines = deps['_lines']
    _load_corrections = deps['_load_corrections']
    _metrika_goals_for = deps['_metrika_goals_for']
    _next_units_reset_utc = deps['_next_units_reset_utc']
    _normalize_callout_text = deps['_normalize_callout_text']
    _num = deps['_num']
    _preflight_creds = deps['_preflight_creds']
    _promo_content_lines = deps['_promo_content_lines']
    _promo_usable_for_content = deps['_promo_usable_for_content']
    _pull_begin = deps['_pull_begin']
    _pull_end = deps['_pull_end']
    _repair_deps = deps['_repair_deps']
    _resolve_region = deps['_resolve_region']
    _rotated_content_window = deps['_rotated_content_window']
    _rule_sets = deps['_rule_sets']
    _run_master_product_item = deps['_run_master_product_item']
    _selected_slepok_key = deps['_selected_slepok_key']
    _slepok_content_get = deps['_slepok_content_get']
    _slepok_uses_shopping = deps['_slepok_uses_shopping']
    _templates_for = deps['_templates_for']
    _auth_error_in_result = deps.get('_auth_error_in_result')
    _units_in_result = deps['_units_in_result']
    _v5_get = deps['_v5_get']
    _job_new = deps.get('_job_new')              # немедленная постановка куки-джобы (может отсутствовать)
    body = request.json or {}
    from .create_set_input import normalize_create_set_input
    _input = normalize_create_set_input(
        body,
        normalize_callout_text=_normalize_callout_text,
        callout_semantic_key=_callout_semantic_key,
        parse_number=_num,
    )
    login = _input["login"]
    items = _input["items"]
    agent = _input["agent"]            # ключ слепка — для привязки нативных интересов
    content_source = _input["content_source"]  # "slepok_library" → БД-контент без M3
    callouts = _input["callouts"]
    # ⛔ ПРАВИЛО: сервис создаёт ВСЕ кампании ТОЛЬКО ЧЕРНОВИКАМИ (никогда не публикует
    # автоматически — публикация = ручной шаг в Директе после проверки). launch принудительно
    # False для ВСЕХ типов, включая UAC tp6/tp7 (раньше они уходили на показы при «Создать и
    # опубликовать»). ЕПК tp1–tp5 и так всегда DRAFT. body.get("launch") игнорируется намеренно.
    launch = False
    counter_id = _input["counter_id"]
    goal_id = _input["goal_id"]
    cpa = _input["cpa"]
    # Галочка «под стиль сайта» (по умолчанию ВКЛ). Снята → no_cpa=True: НЕ создаём CPA-кампании
    # (оплата за конверсии): tp1/tp5 — только cpc-вариант пары; tp2/tp4 pay=cpa — пропускаем.
    no_cpa = _input["no_cpa"]
    # Галочка «загружать кампании по одному фиду»: вместо фан-аута по ВСЕМ фидам аккаунта —
    # только /yandex.xml (fallback: первый доступный фид). Для tp1/tp5 режем список фидов ниже;
    # для tp7 (раскрыт по фидам ещё в плане) — оставляем item'ы выбранного feed_id.
    single_feed = _input["single_feed"]
    # via_cookie: принудительный cookie-first режим для всего набора.
    # Если via_cookie=False, token-типы всё равно идут API-first и АВТОМАТИЧЕСКИ переключаются
    # на cookie-path при error 152 (баллы Директа закончились).
    via_cookie = _input["via_cookie"]
    # stream_content: путь «Создать и опубликовать» БЕЗ предпросмотра — ИИ-контент М3 генерим
    # ПОИТЕМНО прямо здесь, перед созданием каждой РК (контент 1 РК → создаём 1 РК → следующая),
    # а НЕ всю пачку заранее во фронте. Прогресс виден сразу, при 152/сбое уже созданные сохранены.
    stream_content = _input["stream_content"]
    _stream_agent = None
    if stream_content:
        try:
            from . import ai_agents as _A
            _stream_agent = _A.get_agent(body.get("agent") or "")
        except Exception:  # noqa: BLE001
            _stream_agent = None
    if not login or not items:
        return jsonify({"error": "login и items обязательны"}), 400
    from .create_set_metrika import prepare_metrika
    _metrika = prepare_metrika(
        login=login,
        counter_id=counter_id,
        goal_id=goal_id,
        via_cookie=via_cookie,
        no_cpa=no_cpa,
        metrika_goals_for=_metrika_goals_for,
        goal_vse_formy=_goal_vse_formy,
        counter_foreign_owner=_counter_foreign_owner,
    )
    counter_id = _metrika.get("counter_id") or 0
    goal_id = _metrika.get("goal_id") or 0
    metrika_note = _metrika.get("metrika_note")
    metrika_optional = bool(_metrika.get("metrika_optional"))
    precreate_report = None
    precreated_promo_id = None
    precreated_promo_note = None
    precreated_promo_skipped = []
    precreated_callout_ids = []
    precreated_callouts_note = None
    if not _metrika.get("ok"):
        return jsonify({"error": _metrika.get("error")}), int(_metrika.get("status") or 400)

    # Воркер-путь (есть _job_id): конкуренцию контролирует ПУЛ с лимитом по агентству
    # воркеров — глобальный _PULL_LOCK тут НЕ берём (иначе 2-й параллельный аккаунт получил бы 429).
    # Ручной/синхронный путь (без _job_id) — как раньше, под глобальным локом.
    _worker_path = bool(body.get("_job_id"))
    if not _worker_path:
        ok, reason, wait = _pull_begin(f"createset:{login}", 10.0)
        if not ok:
            return _busy_response(reason, wait)
    try:
        from .create_set_account import prepare_create_set_account, validate_create_set_content
        _account = prepare_create_set_account(
            login=login,
            body=body,
            account_ctx=_account_ctx,
            templates_for=_templates_for,
            parse_region_ids=_ints,
        )
        if not _account.get("ok"):
            return jsonify({"error": _account.get("error")}), int(_account.get("status") or 400)
        ctx = _account["ctx"]
        eff_site = _account["site_type"]   # ручной override типа сайта
        tpl_titles = _account["tpl_titles"]
        tpl_texts = _account["tpl_texts"]
        tpl_sitelinks = _account["tpl_sitelinks"]
        # content_source=slepok_library → подставляем контент из direct_slepok_content (БД-слепок).
        # Если записи нет → честный фолбэк на шаблоны по типу сайта + пометка в ответе.
        try:
            from . import ai_agents as _A
            _unify_utp = _A.unify_utp_numbers
        except Exception:  # noqa: BLE001
            _unify_utp = None
        from .create_set_slepok_content import apply_slepok_campaign_content
        slepok_content_note = apply_slepok_campaign_content(
            items=items,
            content_source=content_source,
            agent=agent,
            site_type=eff_site,
            slepok_content_get=_slepok_content_get,
            rotate_window=_rotated_content_window,
            unify_utp_numbers=_unify_utp,
        )
        # Контент берём из item (сгенерированный ИИ-агентом/из слепка/отредактированный), иначе —
        # шаблоны по типу сайта. Хард-фейл только если нет НИ ИИ-контента, НИ шаблонов.
        _content_check = validate_create_set_content(
            items=items,
            tpl_titles=tpl_titles,
            tpl_texts=tpl_texts,
            site_type=eff_site,
        )
        if not _content_check.get("ok"):
            return jsonify({"error": _content_check.get("error")}), int(_content_check.get("status") or 400)
        href = _account["href"]
        region_ids = _account["region_ids"]

        # ПРЕДПОЛЁТ: ДО создания РК проверяем «тот ли токен/куку используем» — лёгкие read-only
        # вызовы с таймаутами. Битые/протухшие креды → быстрый явный отказ (а не тихий висяк на
        # пути создания). Кука обязательна только если в наборе есть grid/UAC-типы (tp5/tp6/tp7).
        _need_cookie = any((it.get("type") or "") not in _TOKEN_ONLY_TYPES for it in items)
        _pf = _preflight_creds(login, body.get("agency") or ctx["agency"] or "", _need_cookie)
        if not _pf["ok"]:
            return jsonify({"error": f"предполётная проверка кредов: {_pf['error']}"}), 502
        _st_token, _w_agency = _pf["token"], _pf["agency"]
        if _pf.get("cookie_only"):
            via_cookie = True   # нет токена (error 53) — весь набор через cookie-путь (без API-баллов)
        # UAC-клиент на куке ТОГО ЖЕ агентства (предполёт уже подтвердил, что кука жива).
        try:
            client = cmc.build_client(login, account=(_w_agency or None))
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"не удалось подобрать рабочую куку: {str(e)[:160]}"}), 502

        # Корректировки ставок из «Глобальных правил» по городу аккаунта (с фолбэком на глобальные '*').
        # Применяются ТОЛЬКО к tp1–tp5 (поисковые семейства). МК(tp6)/Товарка(tp7) — без корректировок.
        corr = _load_corrections(ctx.get("city") or "*")
        ret_map = _account_retargeting(_st_token, login) if _st_token else {}

        # CPA/бюджет из «Глобальных правил» (для tp1 — cpc_cpa целевой CPA):
        rs = _rule_sets(eff_site, ctx.get("city") or "*")
        # r_code и oblast — для правильного кодер-имени групп tp1
        r_code_ctx, _ = _resolve_region(ctx.get("city"))

        results = []
        _tp7_mf = None                                   # ленивый кэш фидов с коллекциями (tp7 фильтр)
        _job = _CREATE_JOBS.get(body.get("_job_id")) if body.get("_job_id") else None
        _units_block = False                             # сработал лимит баллов Директа (error 152)
        _units_pending = 0                               # сколько пунктов плана НЕ создано из-за лимита
        _units_from = None                               # индекс ПЕРВОГО несозданного пункта (для остатка/докрутки)
        _units_seen = False                              # 152 встречался хоть раз (даже если inline-cookie спас пункт)
        _units_switched = False                          # на 152 ВЕСЬ остаток переведён на куки-путь (бесшовно)
        _scan_i = 0                                      # курсор скана results на маркер 152 (учёт continue-веток)
        # RESUME-SKIP (по требованию): ОДИН раз bulk-читаем имена кампаний аккаунта (Grid, без баллов)
        # и дальше пропускаем пункты, чья кампания УЖЕ существует в Директе. Так «Продолжить» докручивает
        # только недостающее (вкл. ранее упавшие — их в Директе нет → создадутся), а не гонит набор с нуля.
        # НЕ пер-item API: одна выборка имён. Имя пункта (it["name"]) строится тем же кодером, что и при
        # создании; fan-out по фиду добавляет ' — {feed}' → считаем существующим и его.
        _existing_names: set = set()
        try:
            _existing_names = {(c.get("name") or "").strip()
                               for c in _grid_list_campaigns(login) if c.get("name")}
        except Exception:  # noqa: BLE001 — нет куки/Grid
            pass
        if _st_token and not _existing_names:
            # Фолбэк на v5 когда Grid недоступен (протухла кука): v5 не видит черновики/UAC/DRAFT,
            # но лучше частичного set, чем пустого (пустой = дубли всего при докрутке).
            try:
                _v5r = _v5_get("campaigns", _st_token, login, ["Name"], criteria={})
                _existing_names = {(c.get("Name") or "").strip()
                                   for c in (_v5r.get("result") or {}).get("Campaigns", [])
                                   if c.get("Name")}
            except Exception:  # noqa: BLE001
                pass
        if not _existing_names and body.get("_repair_force_names"):
            # В контексте «Продолжить» пустой _existing_names = Grid и v5 недоступны.
            # already_in_direct вернёт False для всех пунктов → весь набор пересоздаётся
            # → гарантированные дубли. АБОРТ обязателен — НЕ продолжаем создание.
            return jsonify({
                "error": ("[resume-abort] _existing_names пуст (Grid+v5 недоступны) — "
                          "докрутка остановлена во избежание дублей. "
                          "Проверьте сессию и куки аккаунта."),
                "abort_reason": "empty_existing_names_in_resume",
            }), 503
        from .create_set_precreate import run_create_set_precreate
        _precreated = run_create_set_precreate(
            login=login,
            body=body,
            items=items,
            account=ctx,
            agent=agent,
            site_type=eff_site,
            callouts=callouts,
            stream_content=stream_content,
            existing_names_count=len(_existing_names),
            token=_st_token,
            client=client,
            dedup_callouts=_dedup_callouts,
            callout_cap=_CALLOUT_PER_CAMPAIGN_CAP,
            grid_client_factory=gf.GridClient,
            v5_get=_v5_get,
            promo_usable_for_content=_promo_usable_for_content,
            create_account_promo_from_slepok=_create_account_promo_from_slepok,
            selected_slepok_key=_selected_slepok_key,
        )
        precreate_report = _precreated.get("report")
        precreated_promo_id = _precreated.get("promo_id")
        precreated_promo_note = _precreated.get("promo_note")
        precreated_promo_skipped = _precreated.get("promo_skipped") or []
        precreated_callout_ids = _precreated.get("callout_ids") or []
        precreated_callouts_note = _precreated.get("callouts_note")
        from .create_set_resume import already_in_direct, force_recreate, items_for_result_names
        _repair_force_names = {str(n).strip() for n in (body.get("_repair_force_names") or [])
                               if str(n or "").strip()}

        _content_executor = None
        _content_futures: dict[int, object] = {}
        _content_futures_by_key: dict[tuple, object] = {}
        _generated_content_by_key: dict[tuple, dict] = {}

        def _stream_content_item(src: dict) -> dict:
            return dict(src or {})

        def _prefetch_content(idx: int) -> None:
            if not (stream_content and _stream_agent and 0 <= idx < len(items)):
                return
            src = items[idx]
            if src.get("titles") and src.get("texts") and src.get("sitelinks"):
                return
            if idx in _content_futures:
                return
            _ckey = _content_cache_key((agent or "").strip().lower(), eff_site, ctx.get("city") or "", src)
            with _CONTENT_CACHE_LOCK:
                _cached_ready = _CONTENT_CACHE.get(_ckey)
            if _cached_ready:
                return
            if _ckey in _content_futures_by_key:
                _content_futures[idx] = _content_futures_by_key[_ckey]
                return
            nonlocal _content_executor
            if _content_executor is None:
                from concurrent.futures import ThreadPoolExecutor
                _content_executor = ThreadPoolExecutor(max_workers=2)
            fut = _content_executor.submit(
                _cached_campaign_content,
                login,
                _stream_agent,
                (agent or "").strip().lower(),
                _stream_content_item(src),
                eff_site,
                ctx.get("city") or "",
                [],
                True,
            )
            _content_futures[idx] = fut
            _content_futures_by_key[_ckey] = fut

        def _take_prefetched_content(idx: int, src: dict) -> dict | None:
            if not (stream_content and _stream_agent):
                return None
            fut = _content_futures.pop(idx, None)
            if fut is not None:
                try:
                    return fut.result()
                except Exception:  # noqa: BLE001
                    return None
            return _cached_campaign_content(
                login,
                _stream_agent,
                (agent or "").strip().lower(),
                _stream_content_item(src),
                eff_site,
                ctx.get("city") or "",
                [],
                True,
            )

        for _pref_i in range(min(3, len(items))):
            _prefetch_content(_pref_i)

        # Типы пунктов → tp-код (для safety-net гейта строгого соответствия слепку ниже).
        _TYPE_TO_TP = {"tp1_rsy": "tp1", "search_test": "tp2", "rsya_gallery": "tp3",
                       "search_dynamic": "tp4", "search_gallery": "tp5"}
        for _ci, it in enumerate(items):
            # M3-гейт (Семён 03.07, скрин #92): без ИИ на M3 контент не сгенерить — вместо
            # брака/массового deferred ПАУЗИМ набор (6×10 мин + 1 час, heartbeat внутри).
            # Не дождались → останавливаемся; созданное цело, остаток доберёт повторный запуск.
            _m3_gate = deps.get('_m3_gate_wait')
            if callable(_m3_gate) and not _m3_gate(_job):
                _add_job_err(_job, "ИИ на M3 недоступен (ждали 6×10мин + 1ч) — создание "
                                   "остановлено; остаток набора доберёт повторный запуск")
                results.append({"ok": False, "name": it.get("name") or "",
                                "error": "ИИ на M3 недоступен — набор остановлен на этом пункте"})
                break
            _prefetch_content(_ci + 1)
            _prefetch_content(_ci + 2)
            # Исчерпание суточного лимита баллов Директа (error 152): при ПЕРВОМ же маркере 152
            # БЕСШОВНО переводим ВЕСЬ остаток набора на куки-путь (Grid/UAC, без баллов) — НЕ рвём
            # цикл и НЕ отправляем массово в deferred-до-полуночи (это и было «висит без движения»).
            # Куки-типы (tp1–tp7) создаются без баллов. Пункт, на котором сорвало, мог восстановиться
            # inline-cookie (тогда он ok); если нет — остаётся failed, дубля не будет (повторный набор
            # пропустит уже созданные через set_plan). Это убирает требование ручного попапа-согласия
            # для системной/фоновой докрутки: 152 = автоматический переход на куки.
            while _scan_i < len(results):
                _r = results[_scan_i]; _scan_i += 1
                if _units_in_result(_r) or (
                    _auth_error_in_result and (not _r.get("ok")) and _auth_error_in_result(_r)
                ):
                    _units_seen = True
                    if not _r.get("ok"):
                        _units_block = True
            if _units_seen and not via_cookie:
                via_cookie = True            # с этого пункта и до конца набора — только по куке (без баллов)
                _units_switched = True
                _units_block = False         # 152 больше НЕ блокирующий стоп: продолжаем по куке, не break
            if _job and _job.get("cancel"):              # отмена: стоп ПОСЛЕ текущей (не рвём кампанию на полпути)
                break
            # Примечание: явные CPA-пункты (pay=cpa: tp2/tp4/tp6/tp7) гейтит ПРЕВЬЮ (галочка «под стиль
            # сайта» снимает их отметки → во фронт не уходят), поэтому здесь их НЕ пропускаем — уважаем
            # ручной выбор пользователя. no_cpa тут гасит только cpa-половину пар-движков tp1/tp5
            # (у них отдельной строки в превью нет).
            if _job:                                     # done = обработано ПУНКТОВ плана; created/failed —
                _job["done"] = _ci                       # по ФАКТУ каждой созданной кампании (fan-out даёт
                _job_db_progress(_job)                   # N кампаний на 1 пункт), бампается ниже _bump_job().
            name = it.get("name") or ""
            # Строгое соответствие слепку (ревью A4, баг porg-psm5h7q6): ПРЕВЬЮ гейтит план, но путь
            # СОЗДАНИЯ берёт items из тела как есть (флоу «без предпросмотра», deferred/resume, повтор
            # API) — дублируем гейт, иначе тип вне боевого профиля слепка просочился бы в набор.
            _excl_tp = deps.get('_slepok_profile_excludes_tp')
            _it_tp = _TYPE_TO_TP.get(it.get("type") or "")
            if callable(_excl_tp) and _it_tp and _excl_tp(agent, eff_site, _it_tp):
                print(f"[strict-slepok] {_it_tp} пропущен (нет в боевом профиле {agent}): {name}", flush=True)
                _bump_item(_job)
                continue
            # RESUME-SKIP: кампания пункта УЖЕ есть в Директе → не пересоздаём (и не тратим M3-генерацию).
            # Пропускаем БЫСТРО (heartbeat тикает в _bump_item → watchdog не считает джобу зависшей).
            # ⚠ tp1_rsy — МУЛЬТИ-ФИД fan-out ({name} — {feed1}, {name} — {feed2}): item-level prefix-skip
            # пропустил бы ВЕСЬ пункт, если создана ХОТЬ ОДНА фид-кампания → недосозданные фиды потерялись
            # бы. Для tp1_rsy item-skip НЕ делаем — внутри его цикла skip ПОФИДОВО (по полному имени nm).
            if (it.get("type") != "tp1_rsy"
                    and already_in_direct(name, _existing_names)
                    and not force_recreate(name, _repair_force_names)):
                results.append({"ok": True, "name": name, "skipped": True,
                                "note": "уже создана в Директе — пропущена при докрутке"})
                _bump_item(_job)
                if _job:
                    _job_db_progress(_job)
                continue
            _it_ckey = _content_cache_key((agent or "").strip().lower(), eff_site, ctx.get("city") or "", it)

            # Для парных кампаний и дублей с тем же st/ct/brand в рамках ОДНОГО набора не
            # генерируем заново: вторая кампания получает ТОЧНО тот же готовый контентный набор.
            if not (it.get("titles") and it.get("texts") and it.get("sitelinks")):
                _prev_content = _generated_content_by_key.get(_it_ckey)
                if _prev_content:
                    if _prev_content.get("titles") and not it.get("titles"):
                        it["titles"] = list(_prev_content["titles"])
                    if _prev_content.get("texts") and not it.get("texts"):
                        it["texts"] = list(_prev_content["texts"])
                    if _prev_content.get("sitelinks") and not it.get("sitelinks"):
                        it["sitelinks"] = _content_copy({"sitelinks": _prev_content["sitelinks"]}).get("sitelinks", [])
                    if _prev_content.get("title2") and not it.get("title2"):
                        it["title2"] = _prev_content["title2"]

            # ПОТОКОВАЯ ГЕНЕРАЦИЯ (publish без предпросмотра): контент М3 для ЭТОЙ РК — прямо
            # перед её созданием (1 РК: сгенерили → создаём → следующая). Если контент уже есть
            # в item (ручной путь с предпросмотром) — не трогаем. Сбой генерации → создатель сам
            # упадёт на слепок/шаблоны (фолбэк), набор не валим.
            if stream_content and _stream_agent and not (it.get("titles") and it.get("texts") and it.get("sitelinks")):
                if _job:
                    _job["step"] = "generating"          # UI: «генерирую контент…»
                try:
                    _c = _take_prefetched_content(_ci, it) or {}
                    if _c.get("titles") and not it.get("titles"):
                        it["titles"] = _c["titles"]
                    if _c.get("texts") and not it.get("texts"):
                        it["texts"] = _c["texts"]
                    if _c.get("sitelinks") and not it.get("sitelinks"):
                        it["sitelinks"] = _c["sitelinks"]
                    if _c.get("title2") and not it.get("title2"):
                        it["title2"] = _c["title2"]
                except Exception:  # noqa: BLE001 — генерация не критична: фолбэк на слепок/шаблоны
                    pass
                if _job:
                    _job["step"] = "creating"            # UI: «создаю кампанию…»

            if it.get("titles") and it.get("texts") and it.get("sitelinks"):
                _generated_content_by_key[_it_ckey] = {
                    "titles": list(it.get("titles") or []),
                    "texts": list(it.get("texts") or []),
                    "sitelinks": _content_copy({"sitelinks": it.get("sitelinks") or []}).get("sitelinks", []),
                    "title2": it.get("title2") or "",
                }

            # ── tp1 РСЯ: ЕПК v501 mode=network_cpa с бренд-группами из пака M3 ──────
            if it.get("type") == "tp1_rsy":
                from .create_set_tp1 import run_create_set_tp1
                results.extend(run_create_set_tp1(
                    it=it, name=name,
                    login=login, slepok=agent, site_type=eff_site, w_agency=(_w_agency or ""),
                    city=(ctx.get("city") or ""), r_code=r_code_ctx, href=href, region_ids=region_ids,
                    counter_id=counter_id, goal_id=goal_id,
                    st_token=_st_token, via_cookie=via_cookie, no_cpa=no_cpa, single_feed=single_feed,
                    grid_cookie=_pf.get("cookie"),
                    tpl_titles=tpl_titles, tpl_texts=tpl_texts, rs=rs,
                    corr=corr, ret_map=ret_map, callouts=callouts, callout_ids=precreated_callout_ids,
                    existing_names=_existing_names, repair_force_names=_repair_force_names, job=_job,
                    lines=_lines, num=_num,
                    slepok_uses_shopping=_slepok_uses_shopping,
                    # tp1 множит товарку ТОЛЬКО по catalog-фидам (лендинги → пустой ListingAd → фейл).
                    # tp7/product зовут _account_model_feeds БЕЗ флага (все enabled-фиды) — см. 11939/11206.
                    account_model_feeds=(lambda _l, _a: _account_model_feeds(_l, _a, catalog_only=True)),
                    first_url_feed=_first_url_feed,
                    create_tp1_via_cookie=_create_tp1_via_cookie,
                    create_tp1_campaign=_create_tp1_campaign,
                    units_in_result=_units_in_result,
                    auth_error_in_result=_auth_error_in_result,
                    apply_corrections=_apply_corrections,
                    job_db_progress=_job_db_progress,
                    add_job_err=_add_job_err,
                    bump_job=_bump_job,
                    bump_item=_bump_item,
                ))
                continue

            if it.get("type") == "rsya_gallery":
                from .create_set_gallery import run_create_set_gallery
                results.extend(run_create_set_gallery(
                    kind="tp3",
                    it=it, name=name,
                    login=login, slepok=agent, site_type=eff_site, w_agency=(_w_agency or ""),
                    city=(ctx.get("city") or ""), r_code=r_code_ctx, href=href, region_ids=region_ids,
                    counter_id=counter_id, goal_id=goal_id,
                    st_token=_st_token, via_cookie=via_cookie, no_cpa=no_cpa, single_feed=single_feed,
                    grid_cookie=_pf.get("cookie"),
                    tpl_titles=tpl_titles, tpl_texts=tpl_texts, rs=rs,
                    corr=corr, ret_map=ret_map, callouts=callouts, callout_ids=precreated_callout_ids,
                    job=_job,
                    lines=_lines, num=_num,
                    create_tp5_campaign=_create_tp5_campaign,
                    create_tp3_campaign=_create_tp3_campaign,
                    create_shopping_via_cookie=_create_shopping_via_cookie,
                    units_in_result=_units_in_result,
                    add_job_err=_add_job_err,
                    bump_job=_bump_job,
                    job_db_progress=_job_db_progress,
                    bump_item=_bump_item,
                ))
                continue

            # ── tp5 «Поиск + Товарная галерея»: комбинированная (TextAd+ListingAd+ShoppingAd, эталон Щербаковой) ──
            if it.get("type") == "search_gallery":
                from .create_set_gallery import run_create_set_gallery
                results.extend(run_create_set_gallery(
                    kind="tp5",
                    it=it, name=name,
                    login=login, slepok=agent, site_type=eff_site, w_agency=(_w_agency or ""),
                    city=(ctx.get("city") or ""), r_code=r_code_ctx, href=href, region_ids=region_ids,
                    counter_id=counter_id, goal_id=goal_id,
                    st_token=_st_token, via_cookie=via_cookie, no_cpa=no_cpa, single_feed=single_feed,
                    grid_cookie=_pf.get("cookie"),
                    tpl_titles=tpl_titles, tpl_texts=tpl_texts, rs=rs,
                    corr=corr, ret_map=ret_map, callouts=callouts, callout_ids=precreated_callout_ids,
                    job=_job,
                    lines=_lines, num=_num,
                    create_tp5_campaign=_create_tp5_campaign,
                    create_tp3_campaign=_create_tp3_campaign,
                    create_shopping_via_cookie=_create_shopping_via_cookie,
                    units_in_result=_units_in_result,
                    add_job_err=_add_job_err,
                    bump_job=_bump_job,
                    job_db_progress=_job_db_progress,
                    bump_item=_bump_item,
                ))
                continue

            # Текстовые кампании v5 TextCampaign: tp2 Поиск + tp4 Поиск+Динамика (тот же движок;
            # LIVE Кудерко: tp4 = TEXT_CAMPAIGN, Search=AVERAGE_CPA, Network=OFF — как tp2).
            _TEXT_ENGINE = {"search_test": ("tp2", "search"), "search_dynamic": ("tp4", "search")}
            if it.get("type") in _TEXT_ENGINE:
                # tp2/tp4 создаём ВСЕГДА по cookie/Grid (#1). Историческая v5/v501-ветка была за
                # `if True: … continue` (недостижима) — при выносе опущена как мёртвый код (см. git).
                from .create_set_text import run_create_set_text
                results.extend(run_create_set_text(
                    it=it, name=name, tp_code=_TEXT_ENGINE[it["type"]][0],
                    login=login, slepok=agent, site_type=eff_site, r_code=r_code_ctx,
                    city=(ctx.get("city") or ""), href=href, region_ids=region_ids,
                    counter_id=counter_id, goal_id=goal_id, st_token=_st_token,
                    tpl_titles=tpl_titles, tpl_texts=tpl_texts, rs=rs,
                    corr=corr, ret_map=ret_map, callouts=callouts, callout_ids=precreated_callout_ids,
                    precreated_promo_id=precreated_promo_id,
                    job=_job,
                    lines=_lines, num=_num,
                    create_text_via_cookie=_create_text_via_cookie,
                    slepok_minus_mode=_SLEPOK_MINUS_MODE,
                    apply_campaign_direct_minus=_apply_campaign_direct_minus,
                    get_or_create_minus_set=_get_or_create_minus_set,
                    attach_minus_set_to_text_campaign=_attach_minus_set_to_text_campaign,
                    add_job_err=_add_job_err,
                    bump_job=_bump_job,
                    job_db_progress=_job_db_progress,
                    bump_item=_bump_item,
                ))
                continue
            _prod_results, _tp7_mf = _run_master_product_item(
                it=it, name=name, href=href, region_ids=region_ids, counter_id=counter_id,
                goal_id=goal_id, cpa=cpa, launch=launch, client=client, agent=agent,
                eff_site=eff_site, ctx=ctx, tpl_titles=tpl_titles, tpl_texts=tpl_texts,
                tpl_sitelinks=tpl_sitelinks, rs=rs, login=login, _st_token=_st_token,
                _w_agency=_w_agency, _stream_agent=_stream_agent, _job=_job, _tp7_mf=_tp7_mf)
            results.extend(_prod_results)
        if _content_executor is not None:
            try:
                _content_executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                _content_executor.shutdown(wait=False)
        # 152-докрутка ИМЕНОВАННО (а не «хвостом»): после Goal-1 флипа цикл НЕ прерывается — все пункты
        # обрабатываются, несозданные из-за 152 РАЗБРОСАНЫ по results. Собираем их по ИМЕНИ — работает на
        # ЛЮБОМ пути, ВКЛЮЧАЯ via_cookie-резюм (keywords.add ещё стоит units → 152 возможен и тут; раньше
        # под `not via_cookie` остаток терялся молча → кампании пропадали). _units_block выводим из факта.
        from .create_set_units import count_created, count_failed, count_skipped_existing, units_failed_names
        _units_failed_names = units_failed_names(results)
        _units_block = bool(_units_failed_names)
        # skipped (RESUME-SKIP: уже есть в Директе) не считаем НОВО-созданными.
        created = count_created(results)
        skipped_existing = count_skipped_existing(results)
        # Провал по лимиту баллов (152) — это НЕ «ошибка кампании», а стоп: не считаем его в failed.
        # defer (M3-пак пуст/недоступен) — тоже НЕ failed: пункт уходит на отложенную докрутку.
        failed = count_failed(results)
        if _job:                                         # финал прогресса: ВСЕ items обработаны (цикл не рвётся)
            _job["done"] = len(items)
            _job["created"] = created
            _job["failed"] = failed
        # ОТЛОЖЕННАЯ ДОКРУТКА пунктов с пустым/недоступным M3-паком (defer): НЕ permanent-fail —
        # сохраняем их в deferred, демон докрутит по куке позже (когда пак/M3 восстановится). Дубля нет
        # (set_plan/RESUME-SKIP пропустит уже созданные). resume_at=now() → подхват в ближайший поллинг.
        _defer_names = {(r.get("name") or "") for r in results if r.get("defer")}
        if _defer_names:
            _defer_items = items_for_result_names(items, _defer_names)
            _rc_def = int(body.get("_resume_count") or 0)
            if _defer_items and _rc_def < _RESUME_MAX:
                _ddid = _deferred_save(login, (_w_agency or body.get("agency") or ""),
                                       body, _defer_items, body.get("_job_id"), resume_count=_rc_def)
                if _ddid:
                    _add_job_err(_job, f"M3-пак пуст у {len(_defer_items)} пунктов → отложено на докрутку ({_ddid})")
        # Промо: сначала берём пригодное из библиотеки аккаунта. Если промо нет (или все конфликтуют
        # с контентом), создаём одно промо по слепку/M3 в библиотеке клиента и сразу привязываем
        # к созданным кампаниям. Создание промо НЕ публикует кампании: РК остаются черновиками.
        from .create_set_promo import attach_or_create_promo
        promo_note = attach_or_create_promo(
            login=login,
            items=items,
            results=results,
            token=_st_token,
            client=client,
            account=ctx,
            site_type=eff_site,
            agent=agent,
            precreated_promo_id=precreated_promo_id,
            precreated_promo_note=precreated_promo_note,
            v5_get=_v5_get,
            promo_content_lines=_promo_content_lines,
            promo_usable_for_content=_promo_usable_for_content,
            create_account_promo_from_slepok=_create_account_promo_from_slepok,
            selected_slepok_key=_selected_slepok_key,
        )
        # «Уточнения» (callouts): обещаем подтверждение только если precreate реально дал id.
        # Live 2026-07-01: текущая Grid-схема не поддерживает AddCallouts, поэтому при новом
        # аккаунте precreate может безопасно вернуть пустой id-пул. В этом случае verifier должен
        # честно поставить warning/repair-кандидат, а не считать callouts подтверждёнными.
        from .create_set_callouts import build_callouts_note
        callouts_note = build_callouts_note(
            callouts=callouts,
            precreated_callout_ids=precreated_callout_ids,
            precreated_callouts_note=precreated_callouts_note,
        )
        from .create_set_postprocess import run_create_set_postprocess
        post = run_create_set_postprocess(
            login=login,
            items=items,
            results=results,
            body=body,
            agent=agent,
            counter_id=counter_id,
            goal_id=goal_id,
            site_type=eff_site,
            agency=(_w_agency or body.get("agency") or ""),
            promo_note=promo_note,
            callouts_note=callouts_note,
            callouts=callouts,
            live_verification=lambda _login, _results: _create_set_live_verification(
                _login,
                _results,
                agency=(_w_agency or body.get("agency") or ""),
                use_v5=False,
            ),
            repair_deps=_repair_deps,
            post_verify=_attach_post_repair_verification,
        )
        verification = post.get("verification")
        live_verification = post.get("live_verification")
        repair_gate_summary = post.get("repair_gate")
        auto_repair = post.get("auto_repair")
        # Лимит баллов Директа (152): человекочитаемое предупреждение + сколько НЕ создано.
        units_note = None
        deferred_id = None
        deferred_at = None
        _auto_cookie_jid = None                          # id немедленной куки-джобы (возвращается в ответе)
        if _units_block:
            # Остаток = ИМЕННО пункты, чей результат нёс 152 и НЕ создан (по имени, с fan-out-префиксом).
            _remaining = items_for_result_names(items, _units_failed_names)
            _pend = len(_remaining)
            _units_pending = _pend                       # для ответа units_pending
            _tail = (f"; не создано пунктов плана: {_pend}" if _pend else "")
            _rc = int(body.get("_resume_count") or 0)
            if _remaining:
                # п.1: немедленно ставим куки-джобу — не ждём демона (~10 мин) и не блокируемся на
                # _RESUME_MAX (cookie-путь не тратит баллов; дублей нет — RESUME-SKIP пропустит созданные).
                if _job_new:
                    try:
                        _cb = dict(body)
                        _cb.pop("_job_id", None)
                        _cb["items"] = _remaining
                        _cb["via_cookie"] = True
                        _cb["_resume_count"] = _rc + 1
                        _cb["_deferred_id"] = None       # новая цепочка; parent_did закрывается ниже
                        _sess = {"logged_in": True, "is_admin": True, "_resume": True}
                        _auto_cookie_jid = _job_new(len(_remaining), login, _cb, _sess)
                    except Exception:  # noqa: BLE001
                        _auto_cookie_jid = None
                # Fallback: демон-deferred (если _job_new недоступен или упал).
                # При _rc >= _RESUME_MAX сохраняем с resume_count=0 (сброс) — UI получит deferred_id
                # для кнопки «куки сейчас»; демон не зациклится (resume_at=полночь, не now()).
                if not _auto_cookie_jid:
                    _def_rc = 0 if _rc >= _RESUME_MAX else _rc
                    deferred_id = _deferred_save(
                        login, (_w_agency or body.get("agency") or ""),
                        body, _remaining, body.get("_job_id"), resume_count=_def_rc)
                    if deferred_id:
                        deferred_at = _next_units_reset_utc().isoformat()
            if _auto_cookie_jid:
                units_note = (f"⛔ Баллы коммандера исчерпаны (error 152). Создано: {created}{_tail}. "
                              f"Остаток ({_pend} пунктов) автоматически поставлен в очередь по куке "
                              f"(джоба {_auto_cookie_jid}) — ничего делать не нужно. Дублей не будет.")
            elif deferred_id:
                units_note = (f"⛔ Суточный лимит баллов Яндекс.Директа исчерпан (error 152). "
                              f"Создано кампаний: {created}{_tail}. Остаток ({_pend} пунктов) "
                              f"поставлен на докрутку по куке — повторно кликать не нужно. Дублей не будет.")
            else:
                _no_rem = ("нет несозданного остатка — всё создано или добито" if not _pend
                           else f"не создано {_pend} пунктов; повторите после сброса баллов (полночь МСК)")
                units_note = f"⛔ Баллы коммандера исчерпаны (error 152). Создано: {created}. {_no_rem}."
        elif _units_switched and not units_note:
            # 152 случился в середине набора → остаток БЕСШОВНО создан по куке (без баллов).
            units_note = (f"Баллы коммандера исчерпаны (error 152) во время набора — остаток автоматически "
                          f"создан по куке (Grid/UAC, без баллов). Создано кампаний: {created}.")
        # Если это была ДОКРУТКА осиротевшего остатка (resume по куке): помечаем родительскую строку
        # direct_deferred_creates терминально, чтобы рестарт не реанимировал её повторно (анти-цикл).
        _parent_did = body.get("_deferred_id")
        if _parent_did:
            try:
                if deferred_id:
                    _deferred_set_status(_parent_did, "done", f"остаток перенесён → {deferred_id}")
                else:
                    _deferred_set_status(_parent_did, "done",
                                         f"докручено по куке: создано {created}, не создано {failed}")
            except Exception:  # noqa: BLE001
                pass
        from .create_set_response import build_create_set_response
        return jsonify(build_create_set_response(
            created=created,
            failed=failed,
            launch=launch,
            results=results,
            promo_note=promo_note,
            callouts_note=callouts_note,
            units_block=_units_block,
            units_switched=_units_switched,
            units_note=units_note,
            units_pending=_units_pending,
            deferred_id=deferred_id,
            deferred_at=deferred_at,
            auto_cookie_job_id=_auto_cookie_jid,
            content_source=content_source,
            slepok_content_note=slepok_content_note,
            metrika_note=metrika_note,
            verification=verification,
            live_verification=live_verification,
            precreate_report=precreate_report,
            repair_gate_summary=repair_gate_summary,
            auto_repair=auto_repair,
        ))
    finally:
        if not _worker_path:
            _pull_end(f"createset:{login}")

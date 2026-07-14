"""Create-set orchestration for Direct automation."""

from __future__ import annotations

import contextlib
import os
import threading

from flask import jsonify, request

from . import campaign as cmc
from . import grid_finalize as gf


def _api_first_enabled() -> bool:
    """Флаг DIRECT_API_FIRST (default OFF): ON → tp2/tp4 создаются ТОКЕНОМ (баллы) с фолбэком на
    куку, и 152-флип строже (только по подтверждённому исчерпанию баллов). OFF → прежнее поведение
    байт-в-байт (tp2/tp4 по куке, флип по первому units-маркеру). Читается на каждый набор —
    можно менять env без перекомпиляции модуля."""
    return os.getenv("DIRECT_API_FIRST", "0").strip().lower() in ("1", "true", "yes", "on")


# Порог строгого 152-флипа при API-first: столько РЕАЛЬНЫХ 152-провалов подряд (или подтверждённый
# units≈0) до перевода остатка набора на куку. Мелкий/транзиентный/ложный 152 при живых баллах не
# уводит на куку зря (у агентства баллов много — одиночный 152 обычно транзиент).
_API_FIRST_FLIP_STREAK = 2


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
    _account_content_get = deps.get('_account_content_get')   # #16: account-level reuse (в пределах прохода)
    _account_content_put = deps.get('_account_content_put')
    _content_copy = deps['_content_copy']
    _counter_foreign_owner = deps['_counter_foreign_owner']
    _create_account_promo_from_slepok = deps['_create_account_promo_from_slepok']
    _create_set_live_verification = deps['_create_set_live_verification']
    _create_shopping_via_cookie = deps['_create_shopping_via_cookie']
    _create_text_via_cookie = deps['_create_text_via_cookie']
    _create_text_via_token = deps.get('_create_text_via_token')   # DIRECT_API_FIRST: tp2/tp4 через баллы
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
    _units_alive_for_login = deps.get('_units_alive_for_login')
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
    _v5_call = deps.get('_v5_call')      # None → callouts пропускаются (graceful degrade)
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
    # Авто-подтяжка уточнений слепка, когда попап ничего не передал (headless: test_client /
    # воркер без UI → body["callouts"] пуст, т.к. никто не отмечал уточнения вручную). Берём
    # уточнения СОЗДАВАЕМОГО слепка из public.direct_slepok_callouts — тот же источник и SELECT,
    # что UI-роут /api/slepok_callouts (routes_pack.py). Так библиотека callouts аккаунта
    # наполняется на precreate и в UI-, и в headless-пути (иначе dmp-набор шёл с 0 уточнений).
    # UI-путь НЕ трогаем: если пользователь отметил конкретные уточнения (callouts непусто) —
    # уважаем его выбор, подтяжку не делаем. Только slepok текущего набора (agent) — чужие не
    # заливаем. Кап = _CALLOUT_PER_CAMPAIGN_CAP (лимит уточнений на кампанию): >кап → берём
    # первые N по usage_count (порядок ORDER BY = приоритет частоты использования).
    if not callouts and (agent or "").strip():
        _slepok_for_callouts = _selected_slepok_key(agent or "")
        if _slepok_for_callouts:
            try:
                from .direct_repository import victory_conn as _victory_conn_callouts
                _cco_conn = _victory_conn_callouts()
                try:
                    _cco_cur = _cco_conn.cursor()
                    _cco_cur.execute(
                        "SELECT text FROM public.direct_slepok_callouts WHERE slepok=%s "
                        "ORDER BY usage_count DESC, accounts_count DESC, text",
                        (_slepok_for_callouts,))
                    _cco_rows = [row[0] for row in _cco_cur.fetchall()]
                finally:
                    _cco_conn.close()
                from .create_set_input import normalize_callouts as _normalize_callouts_db
                callouts = _normalize_callouts_db(
                    _cco_rows,
                    normalize_text=_normalize_callout_text,
                    semantic_key=_callout_semantic_key,
                )[:_CALLOUT_PER_CAMPAIGN_CAP]
            except Exception:  # noqa: BLE001 — подтяжка уточнений НЕ должна ронять создание набора
                pass
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
    # DIRECT_API_FIRST (default OFF): ON → tp2/tp4 создаём ТОКЕНОМ (баллы), фолбэк на куку по 152;
    # 152-флип строже (только по подтверждённому исчерпанию). OFF → прежнее поведение байт-в-байт.
    _API_FIRST = _api_first_enabled() and callable(_create_text_via_token)
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
        elif _st_token and not via_cookie and callable(_units_alive_for_login):
            # B3 (preflight-152): есть токен, но БАЛЛЫ Директа могут быть исчерпаны. Дёргаем остаток
            # ДО старта — если баллов нет, сразу переводим ВЕСЬ набор на куки-путь (Grid/UAC, без
            # баллов), не дожидаясь 152 в СЕРЕДИНЕ набора (там частичная РК удаляется + попап/deferred).
            # None (не смогли прочитать остаток) → не мешаем: идём токеном, 152 отработает по-старому.
            try:
                if _units_alive_for_login(login, (_w_agency or "")) is False:
                    via_cookie = True
                    print(f"[preflight-units] {login}: баллы Директа исчерпаны на старте — "
                          f"весь набор идёт по куке (без баллов)", flush=True)
            except Exception:  # noqa: BLE001 — предполёт баллов best-effort, не критичен
                pass
        # UAC-клиент на куке ТОГО ЖЕ агентства (предполёт уже подтвердил, что кука жива).
        try:
            client = cmc.build_client(login, account=(_w_agency or None))
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"не удалось подобрать рабочую куку: {str(e)[:160]}"}), 502

        # ── Гейт здоровья контент-пайплайна ─────────────────────────────────────────────
        # Только для stream_content (ИИ-генерация поитемно). slepok_library / «ручной» контент
        # (items уже содержат titles/texts/sitelinks) LLM не используют → гейт не нужен.
        # Позиция: ДО run_create_set_precreate → ни одного объекта в Директе ещё не создано,
        # abort чист без сирот. finally → _pull_end отработает штатно при любом return.
        # Anti-false-positive: M3 проверяется 3×3 с (≥1 OK → жив); OR — 2×5 с (≥1 OK → жив);
        # оба параллельно. Блокируем лишь при уверенно-мёртвых ОБОИХ (все попытки упали).
        if stream_content and _stream_agent:
            from .llm_providers import (check_content_pipeline_health as _cphealth,
                                        arm_m3_breaker as _arm_m3_breaker,
                                        m3_completion_preflight_ok as _m3_gen_preflight)
            _cp = _cphealth()
            # Completion-preflight: health GET (/v1/models) жив ≠ /chat/completions жив — живой баг
            # 09.07: M3 не выдаёт НИ ОДНОГО токена, idle-таймаут срабатывает через 30-120с, до 4
            # обращений на РК впустую. Реальный 1-токенный тест генерации ОДИН раз на набор → взводим
            # circuit-breaker (весь контент → OpenRouter, БЕЗ повторных idle на мёртвый M3). Выбор
            # провайдера в попапе сохранён: breaker — страховка поверх, не замена (юзер выбрал M3 и
            # он мёртв → авто-фолбэк на OpenRouter на весь набор).
            _m3_gen_ok = bool(_cp["m3_alive"]) and _m3_gen_preflight()
            _arm_m3_breaker(body.get("_job_id") or login, tripped=(not _m3_gen_ok))
            _or_ok = bool(_cp["or_alive"])
            # Абортим ТОЛЬКО когда генерировать реально нечем: M3 completion мёртв И OpenRouter мёртв.
            # (Раньше гейт смотрел any_alive по health GET — пропускал набор в 90с-таймауты на висящем
            # completion. Теперь M3-completion-мёртв считается как мёртвый M3.)
            if (not _m3_gen_ok) and (not _or_ok):
                _cp_msg = (
                    f"контент-пайплайн недоступен: M3 completion висит/мёртв И "
                    f"OpenRouter недоступен ({_cp['message']}) — набор НЕ создан, повторите когда восстановится"
                )
                print(f"[content-pipeline-gate] СТОП — {_cp_msg}", flush=True)
                _gate_job = (
                    _CREATE_JOBS.get(body.get("_job_id")) if body.get("_job_id") else None
                )
                if _gate_job:
                    _add_job_err(_gate_job, _cp_msg)
                return jsonify({"error": _cp_msg, "abort_reason": "content_pipeline_dead"}), 503
            if not _m3_gen_ok:
                print("[m3-completion-preflight] M3 completion висит/мёртв (health GET мог быть жив) → "
                      "circuit-breaker ВЗВЕДЁН, весь набор идёт на OpenRouter", flush=True)
            else:
                print("[m3-completion-preflight] M3 генерирует (1-токенный тест OK)", flush=True)
            print(f"[content-pipeline-gate] OK — {_cp['message']}", flush=True)

        # Корректировки ставок из «Глобальных правил» по городу аккаунта (с фолбэком на глобальные '*').
        # Применяются ТОЛЬКО к tp1–tp5 (поисковые семейства). МК(tp6)/Товарка(tp7) — без корректировок.
        corr = _load_corrections(ctx.get("city") or "*")
        ret_map = _account_retargeting(_st_token, login) if _st_token else {}

        # CPA/бюджет из «Глобальных правил» (для tp1 — cpc_cpa целевой CPA):
        rs = _rule_sets(eff_site, ctx.get("city") or "*")
        # r_code и oblast — для правильного кодер-имени групп tp1
        r_code_ctx, _ = _resolve_region(ctx.get("city"))

        results = []
        _job = _CREATE_JOBS.get(body.get("_job_id")) if body.get("_job_id") else None
        _units_block = False                             # сработал лимит баллов Директа (error 152)
        _units_pending = 0                               # сколько пунктов плана НЕ создано из-за лимита
        _units_from = None                               # индекс ПЕРВОГО несозданного пункта (для остатка/докрутки)
        _units_switched = False                          # на 152 ВЕСЬ остаток переведён на куки-путь (бесшовно)

        # ── C1 (Фаза 2): параллельные НЕконкурирующие каналы создания (фича-флаг, дефолт OFF) ──
        # ИДЕЯ: пока Канал A создаёт РК через Direct API v5 (тратит агентские БАЛЛЫ/units) — Канал B
        # параллельно создаёт РК по КУКЕ/Grid/UAC (баллы НЕ тратит). Ресурсы не конкурируют →
        # общий wall-clock ≈ max(A,B) вместо A+B. Внутри КАЖДОГО канала — строго ПОСЛЕДОВАТЕЛЬНО
        # (гонка за баллами/152/rate-limit недопустима).
        #   Канал A (units/API): tp1_rsy, rsya_gallery(tp3), search_gallery(tp5) — идут через
        #     create_tp1_campaign / create_tp5_campaign / create_tp3_campaign (v501/token) когда есть
        #     токен и not via_cookie; при 152 сами падают на куку. Пары (tp1 cpc+cpa, tp5) — ВНУТРИ
        #     одного item-раннера, между каналами НЕ рвутся.
        #   Канал B (без баллов): search_test(tp2), search_dynamic(tp4) — ВСЕГДА cookie/Grid
        #     (run_create_set_text → create_text_via_cookie), и master/product tp6/tp7 — UAC/cookie.
        # OFF (дефолт): единый последовательный проход одним каналом = ровно прежнее поведение.
        _PARALLEL = os.environ.get("DIRECT_PARALLEL_CHANNELS", "0").strip().lower() in (
            "1", "true", "on", "yes")
        # _guard: сериализует мутации ОБЩЕГО состояния (job-счётчики, _generated_content_by_key,
        # prefetch-словари). ON → RLock (реентерабельный: нет дедлока при случайной вложенности);
        # OFF → nullcontext (нулевой оверхед, поведение байт-в-байт прежнее).
        _guard = threading.RLock() if _PARALLEL else contextlib.nullcontext()
        if _PARALLEL:
            def _bj(job, ok: bool = True, n: int = 1):
                with _guard:
                    _bump_job(job, ok, n)

            def _bi(job):
                with _guard:
                    _bump_item(job)

            def _aje(job, err):
                with _guard:
                    _add_job_err(job, err)

            def _jdp(job):
                with _guard:
                    _job_db_progress(job)
        else:
            # OFF: те же объекты-функции → путь исполнения идентичен прежнему (без обёрток/локов).
            _bj, _bi, _aje, _jdp = _bump_job, _bump_item, _add_job_err, _job_db_progress

        class _Chan:
            """Изолированное состояние ОДНОГО канала: свой список результатов, курсор скана 152,
            флаги units и локальный via_cookie. Раздельные списки результатов исключают гонку
            append между потоками и изолируют скан-152 канала A от результатов канала B."""
            __slots__ = ("results", "scan_i", "units_seen", "units_block",
                         "units_switched", "via_cookie", "tp7_mf", "stop", "units_fail_streak")

            def __init__(self, results_list, via_cookie_init):
                self.results = results_list
                self.scan_i = 0
                self.units_seen = False
                self.units_block = False
                self.units_switched = False
                self.via_cookie = via_cookie_init
                self.units_fail_streak = 0               # API-first: подряд идущих РЕАЛЬНЫХ 152-провалов
                self.tp7_mf = None                       # ленивый кэш фидов с коллекциями (tp7 фильтр)
                self.stop = False                        # cancel/M3-гейт: стоп ПОСЛЕ текущей в этом канале

        _UNITS_TYPES = {"tp1_rsy", "rsya_gallery", "search_gallery"}
        # API-first: tp2/tp4 тоже начинают тратить баллы (token) → канал A (при параллельных каналах
        # C1). OFF — множество прежнее (tp2/tp4 в канале B, cookie). Влияет ТОЛЬКО на распределение по
        # каналам при DIRECT_PARALLEL_CHANNELS; при OFF-параллели порядок один и тот же.
        _units_types_eff = (_UNITS_TYPES | {"search_test", "search_dynamic"}) if _API_FIRST else _UNITS_TYPES

        def _channel_of(_it) -> str:
            # A = тратит баллы (token/v501, 152→cookie фолбэк); B = всегда cookie/Grid/UAC (без баллов).
            return "A" if (_it.get("type") or "") in _units_types_eff else "B"
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
            v5_call=_v5_call,
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
            nonlocal _content_executor
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
            # _guard: при ON два потребителя (Канал A/B) могут префетчить одновременно — мутации
            # _content_futures / _content_futures_by_key / lazy-init executor сериализуем; дедуп по
            # _ckey исключает двойной submit. OFF: _guard = nullcontext → идентично прежнему.
            with _guard:
                if _ckey in _content_futures_by_key:
                    _content_futures[idx] = _content_futures_by_key[_ckey]
                    return
                if _content_executor is None:
                    from concurrent.futures import ThreadPoolExecutor
                    # Q3 (2026-07-08): 2→3 — глубже прячем LLM-латентность за Grid-I/O.
                    # 4 рискует упереться в rate-limit OpenRouter; 3 — консервативный шаг.
                    _content_executor = ThreadPoolExecutor(max_workers=3)
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
            # Забираем future ПОД локом (dict.pop), но .result() (может блокировать на LLM) — ВНЕ лока,
            # чтобы не сериализовать каналы на ожидании генерации. idx у каналов не пересекаются
            # (партиция по индексу), но кросс-канальный префетч возможен → pop защищаем.
            with _guard:
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

        # ── Набор-level ПРЕД-ЗАЛИВКА картинок tp1 в фоне (не блокирует цикл) ─────────────────
        # Собираем все уникальные пути картинок tp1 набора и грузим их в библиотеку аккаунта
        # ОДИН раз параллельно ДО/во время цикла (прогрев процесс-глобального _GRID_IMG_HASH_CACHE).
        # Per-РК заливка в _build_tp1_adgroups(Фаза 3.4)/_create_tp1_via_cookie переиспользует
        # хэши БЕЗ повторной сети; кэш-промах → штатная per-РК заливка (грациозно). Демон: любой
        # сбой прогрева НЕ трогает джобу. Только при наличии tp1_rsy (helper сам себя фильтрует).
        try:
            # ВАЖНО: через blueprint-обёртку (deps), а НЕ сырьём `from .create_set_tp1_builders import …`.
            # Обёртка проходит _create_set_tp1_builder_module()→configure() → инъектирует DI-глобалы
            # (_SLEPOK_KEY/kp/gf/_ct_segment/_creative_images_for_ct/_parallel_upload_images) в модуль.
            # Сырой импорт в фон-потоке обходил ленивый configure → NameError на первом глобале
            # (инцидент IMG_PREUPLOAD_SLEPOK_KEY_UNDEF, 2026-07-09).
            _preup_imgs = deps.get('_preupload_tp1_images')
            if callable(_preup_imgs) and any((_it.get("type") or "") == "tp1_rsy" for _it in items):
                threading.Thread(
                    target=lambda: _preup_imgs(login, items, eff_site, agent,
                                               grid_cookie=_pf.get("cookie")),
                    name="createset-preupload-images", daemon=True,
                ).start()
        except Exception:  # noqa: BLE001 — прогрев картинок best-effort, не критичен
            pass

        # Типы пунктов → tp-код (для safety-net гейта строгого соответствия слепку ниже).
        _TYPE_TO_TP = {"tp1_rsy": "tp1", "search_test": "tp2", "rsya_gallery": "tp3",
                       "search_dynamic": "tp4", "search_gallery": "tp5"}

        def _run_item(_ci, it, ch):
            # Обработка ОДНОГО пункта плана в контексте канала `ch`. break-точки прежнего цикла →
            # ch.stop=True + return; continue → return. Всё общее состояние (results/units/via_cookie/
            # tp7-кэш) — на ch (изолировано по каналу); job-счётчики/prefetch/контент-кэш — под _guard.
            # M3-гейт (Семён 09.07): если ИИ (M3 и OpenRouter) недоступен, НЕ висим часами —
            # гейт делает одну короткую перепроверку и возвращает True, создание продолжается
            # на контенте из слепка (детерминированный фолбэк). False возвращается ТОЛЬКО при
            # отмене джобы (job.cancel) → тогда штатно останавливаемся, созданное цело.
            _m3_gate = deps.get('_m3_gate_wait')
            if callable(_m3_gate) and not _m3_gate(_job):
                _aje(_job, "Набор отменён (job.cancel) на M3-гейте — создание "
                           "остановлено; остаток доберёт повторный запуск")
                ch.results.append({"ok": False, "name": it.get("name") or "",
                                   "error": "Набор отменён — создание остановлено на этом пункте"})
                ch.stop = True
                return
            _prefetch_content(_ci + 1)
            _prefetch_content(_ci + 2)
            # Исчерпание суточного лимита баллов Директа (error 152): при ПЕРВОМ же маркере 152
            # БЕСШОВНО переводим ВЕСЬ остаток набора на куки-путь (Grid/UAC, без баллов) — НЕ рвём
            # цикл и НЕ отправляем массово в deferred-до-полуночи (это и было «висит без движения»).
            # Куки-типы (tp1–tp7) создаются без баллов. Пункт, на котором сорвало, мог восстановиться
            # inline-cookie (тогда он ok); если нет — остаётся failed, дубля не будет (повторный набор
            # пропустит уже созданные через set_plan). Это убирает требование ручного попапа-согласия
            # для системной/фоновой докрутки: 152 = автоматический переход на куки.
            _new_units_fail = False          # РЕАЛЬНЫЙ (failed/fallback) 152 (баллы) среди новых результатов
            _new_auth_fail = False           # ошибка 53 (auth): токен не открывает аккаунт (НЕ про баллы)
            while ch.scan_i < len(ch.results):
                _r = ch.results[ch.scan_i]; ch.scan_i += 1
                # token_units_fallback: token-путь tp2/tp4 упёрся в баллы и добился кукой (res ok) —
                # это подтверждённый 152, засчитываем в строгий streak (иначе fallback-успех скрыл бы 152).
                if _r.get("token_units_fallback"):
                    ch.units_seen = True
                    _new_units_fail = True
                _r_units = bool(_units_in_result(_r))
                _r_auth = bool(_auth_error_in_result and (not _r.get("ok")) and _auth_error_in_result(_r))
                if _r_units or _r_auth:
                    ch.units_seen = True
                    if not _r.get("ok"):
                        ch.units_block = True
                        # 152 (units) → проверяется остаток баллов оператора перед куки-флипом (R2-2).
                        # 53 (auth, БЕЗ 152) → баллы ни при чём: токен не открывает аккаунт, куки-путь —
                        # единственный шанс, флипаем без проверки остатка (как раньше).
                        if _r_units:
                            _new_units_fail = True
                        else:
                            _new_auth_fail = True
            # streak (ON): _API_FIRST_FLIP_STREAK считает ТОЛЬКО ПОДРЯД идущие реальные 152.
            # Пункт, завершившийся БЕЗ нового units-маркера (успех token-пути / не-152), ОБРЫВАЕТ
            # серию → сбрасываем счётчик в 0. Иначе два транзиентных 152, разделённых успешными
            # token-созданиями, накопили бы streak. После реального флипа via_cookie уже True →
            # инкремент/сброс роли не играют. Гейт _API_FIRST: при OFF ветка инкремента не
            # исполняется, streak=0 всегда → куки-флип решается ТОЛЬКО жёсткой проверкой остатка
            # баллов оператора (_confirmed_dead), см. блок ниже.
            if _API_FIRST and not _new_units_fail:
                ch.units_fail_streak = 0
            if ch.units_seen and not ch.via_cookie:
                if _new_auth_fail:
                    # Ошибка 53 (auth): токен не открывает аккаунт — остаток баллов ни при чём.
                    # Куки-путь (Grid/UAC, без баллов) — единственный шанс. Флипаем как раньше,
                    # БЕЗ проверки остатка баллов (её и не прочитать: тот же битый токен).
                    ch.via_cookie = True     # с этого пункта и до конца набора — только по куке (без баллов)
                    ch.units_switched = True
                    ch.units_block = False
                else:
                    # 152 (units). R2-2: ЖЁСТКАЯ проверка реального остатка баллов ОПЕРАТОРА перед
                    # куки-флипом — ОБЕ ветки (OFF-дефолт и ON/DIRECT_API_FIRST). У оператора могут
                    # быть десятки млн баллов (напр. victorylotsofads1 = 40.5M) → одиночный/транзиентный
                    # 152 ЛОЖНЫЙ, уводить весь набор на куку (→ NO_BRAND_SEGMENTS/детур/риск потери)
                    # НЕЛЬЗЯ. Флипаем на куку ТОЛЬКО при ПОДТВЕРЖДЁННОМ исчерпании:
                    #   (а) остаток прочитан УСПЕШНО и rest < порога → _units_alive_for_login = False;
                    #   (б) ON-режим: N реальных 152 подряд (страховка от упорного read-fail — не держать
                    #       набор вечно на token, если баллы физически не читаются).
                    # Живые баллы (True) → ложный 152 → НЕ флипать. None (не прочитали: сеть/блип/нет
                    # токена) → НЕ трактовать как «мертвы» → НЕ флипать (token-retry). Пункт, реально
                    # упавший по 152, остаётся failed → добирается по имени в token-deferred/resume
                    # (units_failed_names, resume_at=след. сброс) — семантика сохранения остатка цела,
                    # cookie-фолбэк для РЕАЛЬНОГО исчерпания сохранён ветвью _confirmed_dead.
                    _units_state = None
                    if _new_units_fail:
                        if _API_FIRST:
                            ch.units_fail_streak += 1
                        try:
                            if callable(_units_alive_for_login):
                                _units_state = _units_alive_for_login(login, (_w_agency or ""))
                        except Exception:  # noqa: BLE001 — чтение остатка best-effort
                            _units_state = None
                    _confirmed_dead = (_units_state is False)     # прочитано УСПЕШНО И rest < порога
                    _streak_confirmed = _API_FIRST and ch.units_fail_streak >= _API_FIRST_FLIP_STREAK
                    if _confirmed_dead or _streak_confirmed:
                        ch.via_cookie = True
                        ch.units_switched = True
                        ch.units_block = False
                    else:
                        # Живые баллы / неизвестность (None) / недобор streak → НЕ флипаем.
                        # Сбрасываем sticky-флаги — следующий пункт переоценивается заново.
                        ch.units_seen = False
                        ch.units_block = False
            if _job and _job.get("cancel"):              # отмена: стоп ПОСЛЕ текущей (не рвём кампанию на полпути)
                ch.stop = True
                return
            # Примечание: явные CPA-пункты (pay=cpa: tp2/tp4/tp6/tp7) гейтит ПРЕВЬЮ (галочка «под стиль
            # сайта» снимает их отметки → во фронт не уходят), поэтому здесь их НЕ пропускаем — уважаем
            # ручной выбор пользователя. no_cpa тут гасит только cpa-половину пар-движков tp1/tp5
            # (у них отдельной строки в превью нет).
            if _job:                                     # done = обработано ПУНКТОВ плана; created/failed —
                with _guard:                             # по ФАКТУ каждой созданной кампании (fan-out даёт
                    _job["done"] = _ci                   # N кампаний на 1 пункт), бампается ниже _bj().
                _jdp(_job)
            name = it.get("name") or ""
            # Строгое соответствие слепку (ревью A4, баг porg-psm5h7q6): ПРЕВЬЮ гейтит план, но путь
            # СОЗДАНИЯ берёт items из тела как есть (флоу «без предпросмотра», deferred/resume, повтор
            # API) — дублируем гейт, иначе тип вне боевого профиля слепка просочился бы в набор.
            _excl_tp = deps.get('_slepok_profile_excludes_tp')
            _it_tp = _TYPE_TO_TP.get(it.get("type") or "")
            if callable(_excl_tp) and _it_tp and _excl_tp(agent, eff_site, _it_tp):
                print(f"[strict-slepok] {_it_tp} пропущен (нет в боевом профиле {agent}): {name}", flush=True)
                _bi(_job)
                return
            # RESUME-SKIP: кампания пункта УЖЕ есть в Директе → не пересоздаём (и не тратим M3-генерацию).
            # Пропускаем БЫСТРО (heartbeat тикает в _bump_item → watchdog не считает джобу зависшей).
            # ⚠ tp1_rsy — МУЛЬТИ-ФИД fan-out ({name} — {feed1}, {name} — {feed2}): item-level prefix-skip
            # пропустил бы ВЕСЬ пункт, если создана ХОТЬ ОДНА фид-кампания → недосозданные фиды потерялись
            # бы. Для tp1_rsy item-skip НЕ делаем — внутри его цикла skip ПОФИДОВО (по полному имени nm).
            if (it.get("type") != "tp1_rsy"
                    and already_in_direct(name, _existing_names)
                    and not force_recreate(name, _repair_force_names)):
                ch.results.append({"ok": True, "name": name, "skipped": True,
                                   "note": "уже создана в Директе — пропущена при докрутке"})
                _bi(_job)
                if _job:
                    _jdp(_job)
                return
            _it_ckey = _content_cache_key((agent or "").strip().lower(), eff_site, ctx.get("city") or "", it)

            # force-recreate/repair: НЕ берём контент из account-кэша — при пересоздании
            # дефектной РК нужен свежий контент, а не старый из кэша (per-набор кэш свежий —
            # его reuse для пары cpc/cpa recreate допустим).
            _force_recreate_item = bool(force_recreate(name, _repair_force_names))

            # Для парных кампаний и дублей с тем же st/ct/brand в рамках ОДНОГО набора не
            # генерируем заново: вторая кампания получает ТОЧНО тот же готовый контентный набор.
            # #16: при промахе набор-кэша — фолбэк в account-кэш (та же марка/модель ct+brand
            # генерится 1 раз на аккаунт, переиспользуется во всех наборах прохода). Порядок:
            # набор-кэш → account-кэш → генерация. recreate/repair обходит account-кэш.
            if not (it.get("titles") and it.get("texts") and it.get("sitelinks")):
                _prev_content = _generated_content_by_key.get(_it_ckey)
                if (not _prev_content and not _force_recreate_item
                        and callable(_account_content_get)):
                    try:
                        _prev_content = _account_content_get(login, _it_ckey)
                    except Exception:  # noqa: BLE001 — reuse не критичен: фолбэк на генерацию
                        _prev_content = None
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
                    with _guard:
                        _job["step"] = "generating"      # UI: «генерирую контент…»
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
                    with _guard:
                        _job["step"] = "creating"        # UI: «создаю кампанию…»

            if it.get("titles") and it.get("texts") and it.get("sitelinks"):
                _content_snapshot = {
                    "titles": list(it.get("titles") or []),
                    "texts": list(it.get("texts") or []),
                    "sitelinks": _content_copy({"sitelinks": it.get("sitelinks") or []}).get("sitelinks", []),
                    "title2": it.get("title2") or "",
                }
                with _guard:
                    _generated_content_by_key[_it_ckey] = _content_snapshot
                # #16: пишем в оба кэша. account-кэш (свой lock внутри helper) — ВНЕ _guard,
                # чтобы не вкладывать локи. Внутри — гейт _content_complete (только полный 5/3/8).
                if callable(_account_content_put):
                    try:
                        _account_content_put(login, _it_ckey, _content_snapshot)
                    except Exception:  # noqa: BLE001 — кэширование не критично
                        pass

            # R2-4 2026-07-10 (c2+d): account-pass — ЕДИНАЯ сумма платежа (d, дефолт ON) и, под
            # флагом DIRECT_SITELINK_REUSE_ACCOUNT (c2, дефолт OFF), ОДИН согласованный набор
            # сайтлинков на весь аккаунт (текстовые tp1/tp2/tp5 идут этим путём; tp6/tp7 UAC —
            # в create_set_master_product). Применяем к готовому контенту ПЕРЕД созданием.
            if it.get("titles") and it.get("texts"):
                try:
                    from . import ai_content as _AC
                    # FIX6/#4 (2026-07-10): АТОМАРНЫЙ get-or-put — устраняет гонку параллельных каналов
                    # (`DIRECT_PARALLEL_CHANNELS=1`): раньше get→генерация→put не атомарны, каждый канал
                    # видел пустой кэш → свой набор → разные inheritableSitelinkSet у tp1/tp2/tp5/tp7.
                    # Первый канал с полным (≥8) набором сеет эталон, ОСТАЛЬНЫЕ берут ТОТ ЖЕ → один набор.
                    _reuse_sl, _is_acc = _AC._account_sitelinks_get_or_put(
                        login, list(it.get("sitelinks") or []))
                    if _reuse_sl and len(_reuse_sl) >= 8 and it.get("sitelinks"):
                        # pct-safety: не подставляем эталон с «%», если заголовки item несут «%»
                        # (per-item инвариант «нет %-сайтлинка при %-заголовке»).
                        from . import text_gen as _tg2
                        _title_pct = bool(_tg2._discount_pcts(list(it.get("titles") or [])))
                        _reuse_has_pct = any(
                            "%" in f"{s.get('title','')} {s.get('description','')}"
                            for s in _reuse_sl if isinstance(s, dict))
                        if not (_title_pct and _reuse_has_pct):
                            it["sitelinks"] = _content_copy(
                                {"sitelinks": _reuse_sl}).get("sitelinks", [])
                    _nt, _nx, _nsl = _AC._account_pay_unify(
                        login, list(it.get("titles") or []), list(it.get("texts") or []),
                        list(it.get("sitelinks") or []))
                    it["titles"], it["texts"] = _nt, _nx
                    if _nsl:
                        it["sitelinks"] = _nsl
                except Exception:  # noqa: BLE001 — account-pass reuse не критичен: локальный набор
                    pass

            # ── tp1 РСЯ: ЕПК v501 mode=network_cpa с бренд-группами из пака M3 ──────
            if it.get("type") == "tp1_rsy":
                from .create_set_tp1 import run_create_set_tp1
                ch.results.extend(run_create_set_tp1(
                    it=it, name=name,
                    login=login, slepok=agent, site_type=eff_site, w_agency=(_w_agency or ""),
                    city=(ctx.get("city") or ""), r_code=r_code_ctx, href=href, region_ids=region_ids,
                    counter_id=counter_id, goal_id=goal_id,
                    st_token=_st_token, via_cookie=ch.via_cookie, no_cpa=no_cpa, single_feed=single_feed,
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
                    job_db_progress=_jdp,
                    add_job_err=_aje,
                    bump_job=_bj,
                    bump_item=_bi,
                ))
                return

            if it.get("type") == "rsya_gallery":
                from .create_set_gallery import run_create_set_gallery
                ch.results.extend(run_create_set_gallery(
                    kind="tp3",
                    it=it, name=name,
                    login=login, slepok=agent, site_type=eff_site, w_agency=(_w_agency or ""),
                    city=(ctx.get("city") or ""), r_code=r_code_ctx, href=href, region_ids=region_ids,
                    counter_id=counter_id, goal_id=goal_id,
                    st_token=_st_token, via_cookie=ch.via_cookie, no_cpa=no_cpa, single_feed=single_feed,
                    grid_cookie=_pf.get("cookie"),
                    tpl_titles=tpl_titles, tpl_texts=tpl_texts, rs=rs,
                    corr=corr, ret_map=ret_map, callouts=callouts, callout_ids=precreated_callout_ids,
                    job=_job,
                    lines=_lines, num=_num,
                    create_tp5_campaign=_create_tp5_campaign,
                    create_tp3_campaign=_create_tp3_campaign,
                    create_shopping_via_cookie=_create_shopping_via_cookie,
                    units_in_result=_units_in_result,
                    add_job_err=_aje,
                    bump_job=_bj,
                    job_db_progress=_jdp,
                    bump_item=_bi,
                    deferred_save=_deferred_save,
                    next_units_reset_utc=_next_units_reset_utc,
                    units_alive=_units_alive_for_login,
                ))
                return

            # ── tp5 «Поиск + Товарная галерея»: комбинированная (TextAd+ListingAd+ShoppingAd, эталон Щербаковой) ──
            if it.get("type") == "search_gallery":
                from .create_set_gallery import run_create_set_gallery
                ch.results.extend(run_create_set_gallery(
                    kind="tp5",
                    it=it, name=name,
                    login=login, slepok=agent, site_type=eff_site, w_agency=(_w_agency or ""),
                    city=(ctx.get("city") or ""), r_code=r_code_ctx, href=href, region_ids=region_ids,
                    counter_id=counter_id, goal_id=goal_id,
                    st_token=_st_token, via_cookie=ch.via_cookie, no_cpa=no_cpa, single_feed=single_feed,
                    grid_cookie=_pf.get("cookie"),
                    tpl_titles=tpl_titles, tpl_texts=tpl_texts, rs=rs,
                    corr=corr, ret_map=ret_map, callouts=callouts, callout_ids=precreated_callout_ids,
                    job=_job,
                    lines=_lines, num=_num,
                    create_tp5_campaign=_create_tp5_campaign,
                    create_tp3_campaign=_create_tp3_campaign,
                    create_shopping_via_cookie=_create_shopping_via_cookie,
                    units_in_result=_units_in_result,
                    add_job_err=_aje,
                    bump_job=_bj,
                    job_db_progress=_jdp,
                    bump_item=_bi,
                    deferred_save=_deferred_save,
                    next_units_reset_utc=_next_units_reset_utc,
                    units_alive=_units_alive_for_login,
                ))
                return

            # Текстовые кампании v5 TextCampaign: tp2 Поиск + tp4 Поиск+Динамика (тот же движок;
            # LIVE Кудерко: tp4 = TEXT_CAMPAIGN, Search=AVERAGE_CPA, Network=OFF — как tp2).
            _TEXT_ENGINE = {"search_test": ("tp2", "search"), "search_dynamic": ("tp4", "search")}
            if it.get("type") in _TEXT_ENGINE:
                # tp2/tp4: OFF → cookie/Grid (#1, прежнее поведение). DIRECT_API_FIRST ON → token/API
                # (баллы) с грациозным фолбэком на куку по 152 — маршрут выбирает run_create_set_text.
                from .create_set_text import run_create_set_text
                ch.results.extend(run_create_set_text(
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
                    create_text_via_token=_create_text_via_token,
                    api_first=_API_FIRST,
                    via_cookie=ch.via_cookie,
                    slepok_minus_mode=_SLEPOK_MINUS_MODE,
                    apply_campaign_direct_minus=_apply_campaign_direct_minus,
                    get_or_create_minus_set=_get_or_create_minus_set,
                    attach_minus_set_to_text_campaign=_attach_minus_set_to_text_campaign,
                    add_job_err=_aje,
                    bump_job=_bj,
                    job_db_progress=_jdp,
                    bump_item=_bi,
                ))
                return
            _prod_results, ch.tp7_mf = _run_master_product_item(
                it=it, name=name, href=href, region_ids=region_ids, counter_id=counter_id,
                goal_id=goal_id, cpa=cpa, launch=launch, client=client, agent=agent,
                eff_site=eff_site, ctx=ctx, tpl_titles=tpl_titles, tpl_texts=tpl_texts,
                tpl_sitelinks=tpl_sitelinks, rs=rs, login=login, _st_token=_st_token,
                _w_agency=_w_agency, _stream_agent=_stream_agent, _job=_job, _tp7_mf=ch.tp7_mf)
            ch.results.extend(_prod_results)

        # ── Драйвер: OFF = последовательный проход одним каналом (прежнее поведение);
        #    ON = два НЕконкурирующих канала в двух потоках, общий wall-clock ≈ max(A,B). ─────────
        if not _PARALLEL:
            # OFF: единственный канал получает СУЩЕСТВУЮЩИЙ список results (тот же объект) → все
            # append/extend попадают прямо в него; порядок и семантика (152→via_cookie флип, cancel,
            # skip, fan-out, deferred) идентичны прежнему циклу for _ci, it in enumerate(items).
            _chan = _Chan(results, via_cookie)
            for _ci, it in enumerate(items):
                _run_item(_ci, it, _chan)
                if _chan.stop:                           # прежний break (cancel / M3-гейт-отмена)
                    break
            _units_switched = _chan.units_switched
        else:
            # ON: партиция по индексу — Канал A (units) и Канал B (cookie) не пересекаются по items.
            # Раздельные списки результатов → нет гонки append и скан-152 канала A видит ТОЛЬКО свои
            # результаты (результаты канала B на via_cookie/units не влияют). 152 в A: остаток A с
            # этого пункта уходит по куке (ch_A.via_cookie=True) — БЕЗ гонки с B (B уже cookie и
            # игнорирует via_cookie); финальный сбор остатка в deferred/авто-куки-джобу — как прежде,
            # по именам из общего results. Каждый канал внутри себя строго ПОСЛЕДОВАТЕЛЕН.
            ch_A = _Chan([], via_cookie)
            ch_B = _Chan([], via_cookie)
            _idx_A = [i for i, _it in enumerate(items) if _channel_of(_it) == "A"]
            _idx_B = [i for i, _it in enumerate(items) if _channel_of(_it) == "B"]

            def _run_channel(ch, idxs):
                for _ci in idxs:
                    if ch.stop:
                        break
                    try:
                        _run_item(_ci, items[_ci], ch)
                    except Exception as _e:              # noqa: BLE001 — падение пункта не валит канал/набор
                        _aje(_job, f"[parallel-channel] пункт #{_ci} упал: {str(_e)[:200]}")
                        ch.results.append({"ok": False, "name": (items[_ci].get("name") or ""),
                                           "error": f"исключение канала создания: {str(_e)[:200]}"})
                    if ch.stop:
                        break

            _tA = threading.Thread(target=_run_channel, args=(ch_A, _idx_A),
                                   name="createset-chanA-units", daemon=True)
            _tB = threading.Thread(target=_run_channel, args=(ch_B, _idx_B),
                                   name="createset-chanB-cookie", daemon=True)
            _tA.start(); _tB.start()
            _tA.join(); _tB.join()
            # Слияние: считалки (created/failed/skipped) порядко-независимы; собираем в исходном
            # порядке пунктов для стабильного отчёта (A-пункты и B-пункты по возрастанию индекса).
            results.extend(ch_A.results)
            results.extend(ch_B.results)
            _units_switched = ch_A.units_switched or ch_B.units_switched

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
            _ddid = None
            if _defer_items and _rc_def < _RESUME_MAX:
                _ddid = _deferred_save(login, (_w_agency or body.get("agency") or ""),
                                       body, _defer_items, body.get("_job_id"), resume_count=_rc_def)
                if _ddid:
                    _add_job_err(_job, f"{len(_defer_items)} пунктов отложено на докрутку ({_ddid})")
            if _defer_items and not _ddid:
                # Кап докруток (_RESUME_MAX) или сбой сохранения деферреда → НЕ теряем молча
                # (ревью 06.07: пункт исчезал без failed и без ошибки): честный failed + запись.
                failed += len(_defer_items)
                if _job:
                    _job["failed"] = failed
                _add_job_err(_job, f"докрутка НЕ запланирована для {len(_defer_items)} пунктов "
                                   f"(resume_count={_rc_def}/{_RESUME_MAX} или сбой сохранения) — "
                                   f"нужен ручной перезапуск набора")
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
        # ── BATCH АСПЕКТЫ (dual-write, фаза 1) ─────────────────────────────────────────
        # Гейт: DIRECT_BATCH_ASPECTS=off (дефолт) — батч не запускается.
        #        dual  — батч + инлайн (parity-проверка). only — (будущее) только батч.
        # Инлайн-финализация (_finalize_rsya / _finalize_search_via_grid) уже применила те
        # же кампаний-уровневые аспекты пер-кампанию. Батч повторяет идемпотентно — один
        # Grid-вызов на ВСЕ созданные РК вместо N вызовов. Фаза 1 = дуал-райт (parity).
        # Сбой батч-фазы НЕ роняет набор: ошибки логируются best-effort.
        # _enabled_minus_places / _grid_minus_pack_id пока нет в orchestrator deps →
        # disabled_places/minus_set_ids пусты до добавления в _create_set_orchestrator_deps.
        import os as _os
        _BATCH_MODE = _os.environ.get("DIRECT_BATCH_ASPECTS", "off").lower().strip()
        _batch_report: dict | None = None
        if _BATCH_MODE in ("dual", "only"):
            try:
                from .campaign_result import created_campaigns as _extract_created
                from .create_set_apply_batches import apply_campaign_aspects
                _batch_created = _extract_created(results)
                if _batch_created:
                    # Sitelinks: первый item с непустым списком быстрых ссылок
                    _batch_sitelinks = None
                    for _bi in (items or []):
                        _sl = _bi.get("sitelinks")
                        if isinstance(_sl, list) and _sl:
                            _batch_sitelinks = _sl
                            break
                    # Ages (bidmods): из corr.demographic — все ненулевые (включая -100 для исключений)
                    _GRID_AGE_MAP = {
                        "AGE_0_17": "_0_17", "AGE_18_24": "_18_24",
                        "AGE_25_34": "_25_34", "AGE_35_44": "_35_44",
                        "AGE_45_54": "_45_54", "AGE_55": "_55_",
                    }
                    _batch_ages: dict = {}
                    for _d in (corr.get("demographic") or []):
                        if _d.get("kind") == "age" and int(_d.get("pct") or 0) != 0:
                            _ga = _GRID_AGE_MAP.get(_d.get("key") or "")
                            if _ga:
                                _batch_ages[_ga] = int(_d["pct"])
                    # Optional deps: disabled_places и minus_set_ids (не в текущих orchestrator deps)
                    _batch_dp_fn = deps.get("_enabled_minus_places")
                    _batch_disabled = _batch_dp_fn() if callable(_batch_dp_fn) else []
                    _batch_mp_fn = deps.get("_grid_minus_pack_id")
                    _batch_minus: list = []
                    if callable(_batch_mp_fn):
                        _mpid = _batch_mp_fn(login)
                        if _mpid:
                            _batch_minus = [_mpid]
                    _batch_spec = {
                        "callouts": callouts,
                        "callout_cap": _CALLOUT_PER_CAMPAIGN_CAP,
                        "promo_ctx": {
                            "body": body,
                            "agency": (_w_agency or body.get("agency") or ""),
                        },
                        "promo_id": (precreated_promo_id or None),
                        "sitelinks": _batch_sitelinks,
                        "disabled_places": _batch_disabled,
                        "ages_percent": _batch_ages,
                        "minus_set_ids": _batch_minus,
                        "agency": (_w_agency or body.get("agency") or ""),
                    }
                    _batch_report = apply_campaign_aspects(
                        login, _batch_created, _batch_spec, _repair_deps()
                    )
                    import logging as _blog
                    _blog.getLogger("direct.batches").info(
                        "[batch-aspects] login=%s campaigns=%s aspects=%s",
                        login,
                        _batch_report.get("campaign_count", 0),
                        {k: v.get("applied", v.get("skipped", "?"))
                         for k, v in (_batch_report.get("aspects") or {}).items()},
                    )
            except Exception as _batch_ex:
                import logging as _blog
                _blog.getLogger("direct.batches").error(
                    "[batch-aspects] login=%s exception: %s", login, str(_batch_ex)[:300]
                )
        # ── конец batch-аспектов ─────────────────────────────────────────────────────────
        # Лимит баллов Директа (152): человекочитаемое предупреждение + сколько НЕ создано.
        units_note = None
        deferred_id = None
        deferred_at = None
        _auto_cookie_jid = None                          # id немедленной куки-джобы (возвращается в ответе)
        _false152_unplaced = False                       # ложный 152 при ЖИВЫХ баллах + token-ретрай упал технически → остаток НЕ размещён
        if _units_block:
            # Остаток = ИМЕННО пункты, чей результат нёс 152 и НЕ создан (по имени, с fan-out-префиксом).
            _remaining = items_for_result_names(items, _units_failed_names)
            _pend = len(_remaining)
            _units_pending = _pend                       # для ответа units_pending
            _tail = (f"; не создано пунктов плана: {_pend}" if _pend else "")
            _rc = int(body.get("_resume_count") or 0)
            # R2-2: перед куки-докруткой остатка ЖЁСТКО читаем реальный остаток баллов ОПЕРАТОРА.
            # Живые (True) / нечитаемые (None) баллы → 152 ложный/транзиентный → остаток добиваем
            # ТОКЕНОМ (deferred с _resume_via_token=True, resume_at=now() — демон повторит API-путём
            # немедленно), а НЕ уводим на куку (лишний детур + NO_BRAND для сегментного tp5). Куки-
            # remainder — ТОЛЬКО при ПОДТВЕРЖДЁННО мёртвых баллах (False) ИЛИ при исчерпании токен-
            # ретраев (_rc >= _RESUME_MAX): тогда кука — законный фолбэк для реального 152.
            _units_dead_confirmed = False
            if _remaining:
                try:
                    if callable(_units_alive_for_login):
                        _units_dead_confirmed = (
                            _units_alive_for_login(login, (_w_agency or body.get("agency") or "")) is False)
                except Exception:  # noqa: BLE001 — чтение остатка best-effort, не критично
                    _units_dead_confirmed = False
            _token_retry_did = None
            if _remaining and not _units_dead_confirmed and _rc < _RESUME_MAX:
                # Ложный/транзиентный 152 при живых (или нечитаемых) баллах → token-ретрай СРАЗУ.
                # _resume_via_token=True обязателен: демон _resume_one_deferred иначе форсит via_cookie.
                try:
                    _tb = dict(body)
                    _tb["_resume_via_token"] = True
                    _tb.pop("via_cookie", None)
                    # exclude_id: этот прогон мог САМ быть резюмом (_deferred_id) — исключаем его строку
                    # из дедупа, иначе self-reference (дедуп вернул бы parent-id, финал пометил бы его
                    # done, новая token-строка не создалась → остаток теряется, инцидент SEGMENT_TP5).
                    _tb_parent = _tb.pop("_deferred_id", None)
                    _token_retry_did = _deferred_save(
                        login, (_w_agency or body.get("agency") or ""),
                        _tb, _remaining, body.get("_job_id"), resume_count=_rc,
                        exclude_id=_tb_parent)
                except Exception:  # noqa: BLE001
                    _token_retry_did = None
                if _token_retry_did:
                    deferred_id = _token_retry_did
                    units_note = (f"⚠️ Транзиентный/ложный 152 при ЖИВЫХ баллах оператора — остаток "
                                  f"({_pend} пунктов) добивается ТОКЕНОМ (не по куке){_tail}. "
                                  f"Создано: {created}. Дублей не будет.")
            if _remaining and not _token_retry_did and (_units_dead_confirmed or _rc >= _RESUME_MAX):
                # ПОДТВЕРЖДЁННОЕ исчерпание баллов (_units_dead_confirmed) ИЛИ исчерпан токен-ретрай
                # (_rc >= _RESUME_MAX) → куки-remainder (реальный 152). Гейт ЖЁСТКО требует одно из двух:
                # без него «_token_retry_did is None» из-за ТЕХНИЧЕСКОГО падения token-ретрая при ЖИВЫХ
                # баллах ложно уводил бы на куку + текст «баллы исчерпаны» (инцидент porg-asfbs7qe 13.07).
                # п.1: немедленно ставим куки-джобу — не ждём демона (~10 мин) и не блокируемся на
                # _RESUME_MAX (cookie-путь не тратит баллов; дублей нет — RESUME-SKIP пропустит созданные).
                if _job_new:
                    try:
                        _parent_jid_now = body.get("_job_id")
                        _cb = dict(body)
                        _cb.pop("_job_id", None)
                        _cb["items"] = _remaining
                        _cb["via_cookie"] = True
                        _cb["_resume_count"] = _rc + 1
                        _cb["_deferred_id"] = None       # новая цепочка; parent_did закрывается ниже
                        # Семён 2026-07-06: добивка — сразу (не в конец очереди) и без НОВОЙ карточки;
                        # _resume_of → воркер вольёт created/failed докрутки в родительскую джобу.
                        _cb["_resume_of"] = _parent_jid_now
                        _sess = {"logged_in": True, "is_admin": True, "_resume": True}
                        _auto_cookie_jid = _job_new(len(_remaining), login, _cb, _sess, priority=True)
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
            elif _remaining and not _token_retry_did and not _units_dead_confirmed and _rc < _RESUME_MAX:
                # ЛОЖНЫЙ 152 при ЖИВЫХ/нечитаемых баллах + token-ретрай (_deferred_save) упал ТЕХНИЧЕСКОЙ
                # ошибкой (DB-хиккап и т.п., НЕ исчерпание баллов). НЕ уводим на куку (потеряли бы
                # сегментный tp5 → NO_BRAND_SEGMENTS_AVAILABLE) и НЕ пишем ложное «баллы исчерпаны».
                # Инвариант «пункты не теряются»: ещё раз кладём остаток в TOKEN-деферред
                # (_resume_via_token=True, БЕЗ via_cookie) — демон повторит API-путём. Не вышло → честный
                # note про ручной перезапуск + parent НЕ гасим в done (waiting, см. parent-handling ниже).
                try:
                    _tb2 = dict(body)
                    _tb2["_resume_via_token"] = True
                    _tb2.pop("via_cookie", None)
                    _tb2_parent = _tb2.pop("_deferred_id", None)   # self-reference-guard (см. SEGMENT_TP5)
                    deferred_id = _deferred_save(
                        login, (_w_agency or body.get("agency") or ""),
                        _tb2, _remaining, body.get("_job_id"), resume_count=_rc,
                        exclude_id=_tb2_parent)
                except Exception:  # noqa: BLE001
                    deferred_id = None
                if deferred_id:
                    units_note = (f"⚠️ Ложный/транзиентный 152 при ЖИВЫХ баллах оператора; первая "
                                  f"попытка постановки остатка ({_pend} пунктов) упала техошибкой — "
                                  f"остаток повторно поставлен на докрутку ТОКЕНОМ (не по куке){_tail}. "
                                  f"Создано: {created}. Дублей не будет.")
                else:
                    _false152_unplaced = True
                    units_note = (f"⚠️ Ложный/транзиентный 152: баллы у оператора ЕСТЬ, но остаток "
                                  f"({_pend} пунктов) не удалось поставить на докрутку — техническая "
                                  f"ошибка. Требуется РУЧНОЙ перезапуск остатка{_tail}. "
                                  f"Создано: {created}. Баллы НЕ исчерпаны (это НЕ реальный 152).")
            if _auto_cookie_jid:
                units_note = (f"⛔ Баллы коммандера исчерпаны (error 152). Создано: {created}{_tail}. "
                              f"Остаток ({_pend} пунктов) автоматически поставлен в очередь по куке "
                              f"(джоба {_auto_cookie_jid}) — ничего делать не нужно. Дублей не будет.")
            elif deferred_id and units_note is None:
                # token-retry ветвь уже выставила units_note; сюда попадаем только на реальном 152 (кука).
                units_note = (f"⛔ Суточный лимит баллов Яндекс.Директа исчерпан (error 152). "
                              f"Создано кампаний: {created}{_tail}. Остаток ({_pend} пунктов) "
                              f"поставлен на докрутку по куке — повторно кликать не нужно. Дублей не будет.")
            elif units_note is None:
                if not _pend:
                    # _pend=0 → несозданного остатка ПО БАЛЛАМ нет (всё создано/добито inline).
                    # Реальное исчерпание в этой ветке не подтверждено (_units_dead_confirmed ставится
                    # только при непустом _remaining). НЕ пишем ложное «Баллы исчерпаны»: если пункты
                    # ушли в docrutka-defer (пустой/недоступный контент-пак — НЕ 152), честно называем
                    # причину. (FALSE_152_DEFER_LABEL)
                    if _defer_names:
                        units_note = (f"Создано: {created}. Часть пунктов отложена на докрутку "
                                      f"(контент-пак пуст/недоступен) — это не исчерпание баллов.")
                    else:
                        units_note = (f"Создано: {created}. Несозданного остатка нет — "
                                      f"всё создано или добито.")
                else:
                    # Реальный остаток ПО БАЛЛАМ не размещён → корректное сообщение про баллы (не ломаем).
                    units_note = (f"⛔ Баллы коммандера исчерпаны (error 152). Создано: {created}. "
                                  f"не создано {_pend} пунктов; повторите после сброса баллов (полночь МСК).")
        elif _units_switched and not units_note:
            # 152 случился в середине набора → остаток БЕСШОВНО создан по куке (без баллов).
            units_note = (f"Баллы коммандера исчерпаны (error 152) во время набора — остаток автоматически "
                          f"создан по куке (Grid/UAC, без баллов). Создано кампаний: {created}.")
        # Если это была ДОКРУТКА осиротевшего остатка (resume по куке): помечаем родительскую строку
        # direct_deferred_creates терминально, чтобы рестарт не реанимировал её повторно (анти-цикл).
        _parent_did = body.get("_deferred_id")
        if _parent_did:
            # defer_keep: сегментный tp5 в токен-докрутке НЕ смог пойти токеном (нет токена/баллов) и
            # НАМЕРЕННО не создавался по куке (сегменты кука не умеет). Нельзя гасить строку в done
            # без реального прогресса — иначе tp5 теряется молча (инцидент 08.07 721641cad7c1 /
            # job 23677e1473d1, porg-psm5h7q6). Возвращаем строку в waiting: демон повторит ТОКЕНОМ,
            # когда появятся токен+баллы (_resume_one_deferred сам сделает бэкофф). Уже созданные в
            # этой же строке пункты не задублируются — set_plan/RESUME-SKIP пропустит их по имени.
            _defer_keep = any((r or {}).get("defer_keep") for r in results)
            try:
                if deferred_id:
                    _deferred_set_status(_parent_did, "done", f"остаток перенесён → {deferred_id}")
                elif _defer_keep or _false152_unplaced:
                    _deferred_set_status(_parent_did, "waiting",
                                         "токен/баллы не готовы — отложено, демон повторит токеном")
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
            skipped_existing=skipped_existing,
        ))
    finally:
        if not _worker_path:
            _pull_end(f"createset:{login}")

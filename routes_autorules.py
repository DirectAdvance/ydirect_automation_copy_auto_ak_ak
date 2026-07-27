"""Autorules page routes + Phase 2 API (overview, goals, balance, sensors)."""

from __future__ import annotations

import threading

from flask import jsonify, render_template, request


# ─── Сенсор-реестр ────────────────────────────────────────────────────────────
# sensor_key → (display_name, module_path_suffix)
_SENSORS: dict[str, str] = {
    "balance":         "autorules.sensors.balance",
    "campaign_status": "autorules.sensors.campaign_status",
    "url_check":       "autorules.sensors.url_check",
    "minus":           "autorules.sensors.minus",
    "goals":           "autorules.sensors.goals",
    "anomalies":       "autorules.sensors.anomalies",
}

_TOKEN_LOCK = threading.Lock()  # защита от параллельного token_for_login из UI
_TOKEN_CACHE: dict[str, tuple] = {}  # login → (token, agency)
_TOKEN_TTL = 300   # секунд


def register_autorules_routes(
    bp,
    access,
    *,
    deps: dict | None = None,
) -> None:
    """Register autorules page + API routes on the given blueprint.

    deps (injected by autorules_main.create_app):
        direct_tokens       — callable() → {agency: oauth_token}
        token_for_login     — callable(login, agency, tokens) → (token, agency)
        victory_conn        — callable() → psycopg2 connection к Victory DB
        metrika_goals_for   — callable(login) → {counters, goal_id}|None
        sensor_runs_append  — callable(login, key, found, details) → id
        LIVE_V4_URL         — str, URL live v4 API (для баланса)
    """
    _deps = deps or {}

    def _tokens():
        fn = _deps.get("direct_tokens")
        return fn() if fn else {}

    def _resolve_token(login: str, agency: str = "") -> tuple[str | None, str | None]:
        """Возвращает (token, agency) для логина с кэшем 5 минут."""
        import time
        fn = _deps.get("token_for_login")
        if not fn:
            return None, None
        now = time.monotonic()
        with _TOKEN_LOCK:
            cached = _TOKEN_CACHE.get(login)
            if cached and (now - cached[2]) < _TOKEN_TTL:
                return cached[0], cached[1]
        tok, ag = fn(login, agency, _tokens())
        with _TOKEN_LOCK:
            _TOKEN_CACHE[login] = (tok, ag, time.monotonic())
        return tok, ag

    def _victory_conn():
        fn = _deps.get("victory_conn")
        return fn() if fn else None

    def _metrika_goals_for(login: str):
        fn = _deps.get("metrika_goals_for")
        return fn(login) if fn else None

    def _sensor_runs_append(login, key, found, details):
        fn = _deps.get("sensor_runs_append")
        if fn:
            try:
                fn(login, key, found, details)
            except Exception:  # noqa: BLE001
                pass

    def _live_v4_url() -> str:
        return _deps.get("LIVE_V4_URL") or "https://api.direct.yandex.ru/live/v4/json/"

    def _accounts_source(mode: str) -> dict:
        """Выбор источника аккаунтов по режиму Work/Home.

        Work  → Victory DB (агентские аккаунты, полностью функционален).
        Home  → TODO: личные кабинеты Директа (источник в настройке).
                Возвращает available=False; подключить через home/yandex_direct/ когда будет готов.
        """
        if mode == "home":
            # TODO: подключить home-источник когда будет готов (home/yandex_direct/)
            return {
                "available": False,
                "message": "Домашние проекты: источник аккаунтов в настройке",
            }
        return {"available": True}  # work → Victory DB через _victory_conn()

    # ── Страница ────────────────────────────────────────────────────────────────

    @bp.route("/autorules")
    @access
    def autorules():
        """Страница «Автоправила Директа»."""
        return render_template(
            "direct/autorules.html",
            active_section="services",
            active_page="direct_autorules",
            hide_scope=request.args.get("hide_scope") == "1",
        )

    # ── ЗАДАЧА A: Обзор ─────────────────────────────────────────────────────────

    @bp.route("/api/ar/overview")
    @access
    def api_ar_overview():
        """Список аккаунтов из local_gsheet_sites + расход-факт текущего месяца.

        Параметры (все опциональные):
          login        — конкретный логин (вернём 1 запись + попробуем подтянуть баланс)
          directologist — фильтр по директологу
          city          — фильтр по городу
          salon         — фильтр по салону
          with_balance  — "1" → подтянуть баланс одиночного аккаунта (только если login задан)
        """
        import psycopg2.extras

        mode = request.args.get("mode", "work")
        src = _accounts_source(mode)
        if not src["available"]:
            return jsonify({
                "rows": [], "total": 0,
                "directologists": [], "cities": [],
                "balance": None,
                "note": src["message"],
            })

        login_q = (request.args.get("login") or "").strip()
        dir_q   = (request.args.get("directologist") or "").strip()
        city_q  = (request.args.get("city") or "").strip()
        salon_q = (request.args.get("salon") or "").strip()
        with_balance = request.args.get("with_balance") == "1"

        conn = _victory_conn()
        if conn is None:
            return jsonify({"error": "Victory DB недоступна"}), 503

        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

            # Фильтры
            conditions = ["g.direction = 'Авто'", "g.login_key IS NOT NULL",
                          "char_length(g.login_key) > 4"]
            params: list = []
            if login_q:
                conditions.append("g.login_key = %s")
                params.append(login_q)
            if dir_q:
                conditions.append("g.directologist ILIKE %s")
                params.append(f"%{dir_q}%")
            if city_q:
                conditions.append("g.city ILIKE %s")
                params.append(f"%{city_q}%")
            if salon_q:
                conditions.append("g.salon ILIKE %s")
                params.append(f"%{salon_q}%")

            where = " AND ".join(conditions)
            cur.execute(
                f"""
                SELECT g.login_key AS login, g.domain, g.salon, g.city,
                       g.directologist, g.status, g.agency_account,
                       g.site_type, g.crm,
                       COALESCE(r.spend, 0)::float AS otkrut_fact
                FROM public.local_gsheet_sites g
                LEFT JOIN (
                    SELECT account_login,
                           SUM(total_cost) AS spend
                    FROM public.yandex_direct_manager_reports
                    WHERE left("Date", 7) = to_char(now(), 'YYYY-MM')
                    GROUP BY account_login
                ) r ON r.account_login = g.login_key
                WHERE {where}
                ORDER BY g.domain NULLS LAST
                LIMIT 500
                """,
                params,
            )
            rows = [dict(r) for r in cur.fetchall()]

            # Фильтры-подсказки (уникальные значения)
            cur.execute(
                "SELECT DISTINCT directologist FROM public.local_gsheet_sites "
                "WHERE direction = 'Авто' AND directologist IS NOT NULL "
                "ORDER BY directologist LIMIT 100"
            )
            directologists = [r["directologist"] for r in cur.fetchall()]

            cur.execute(
                "SELECT DISTINCT city FROM public.local_gsheet_sites "
                "WHERE direction = 'Авто' AND city IS NOT NULL "
                "ORDER BY city LIMIT 200"
            )
            cities = [r["city"] for r in cur.fetchall()]

        finally:
            conn.close()

        # Опциональный баланс одиночного аккаунта
        balance_data = None
        if login_q and with_balance and rows:
            balance_data = _fetch_single_balance(login_q, rows[0].get("agency_account") or "")

        return jsonify({
            "rows": rows,
            "total": len(rows),
            "directologists": directologists,
            "cities": cities,
            "balance": balance_data,
        })

    @bp.route("/api/ar/balance")
    @access
    def api_ar_balance():
        """Баланс одного аккаунта через Direct API (Live v4 AccountManagement.Get)."""
        mode = request.args.get("mode", "work")  # noqa: F841 — зарезервировано для home-источника
        login = (request.args.get("login") or "").strip()
        agency = (request.args.get("agency") or "").strip()
        if not login:
            return jsonify({"error": "login обязателен"}), 400

        data = _fetch_single_balance(login, agency)
        return jsonify(data)

    def _fetch_single_balance(login: str, agency: str = "") -> dict:
        import requests as rqs
        tok, ag = _resolve_token(login, agency)
        if not tok:
            return {"balance": None, "error": "нет токена для этого логина"}
        try:
            body = {
                "method": "AccountManagement",
                "token": tok,
                "param": {"Action": "Get", "SelectionCriteria": {"Logins": [login]}},
            }
            j = rqs.post(_live_v4_url(), json=body, timeout=20).json()
            accs = (j.get("data") or {}).get("Accounts") or []
            if accs:
                amount = round(float(accs[0].get("Amount") or 0), 2)
                return {"balance": amount, "currency": "RUB", "login": login}
            return {"balance": None, "error": "аккаунт не найден в AgencyAPI"}
        except Exception as exc:  # noqa: BLE001
            return {"balance": None, "error": str(exc)[:100]}

    # ── Цели Метрики ────────────────────────────────────────────────────────────

    @bp.route("/api/ar/goals")
    @access
    def api_ar_goals():
        """Цели Метрики для аккаунта из public.metrika_goals."""
        mode = request.args.get("mode", "work")  # noqa: F841 — зарезервировано для home-источника
        login = (request.args.get("login") or "").strip()
        if not login:
            return jsonify({"error": "login обязателен"}), 400

        goals_data = _metrika_goals_for(login)
        if goals_data is None:
            return jsonify({
                "login": login,
                "counters": [],
                "goal_id": None,
                "goals": [],
                "note": "нет записей в metrika_goals для этого логина",
            })

        goals = []
        goal_id = goals_data.get("goal_id")
        if goal_id:
            goals.append({"id": goal_id, "name": "Все формы"})

        return jsonify({
            "login": login,
            "counters": goals_data.get("counters") or [],
            "goal_id": goal_id,
            "goals": goals,
        })

    # ── ЗАДАЧА B: Сенсоры ───────────────────────────────────────────────────────

    @bp.route("/api/ar/sensors/run", methods=["POST"])
    @access
    def api_ar_sensors_run():
        """Запуск выбранных сенсоров для аккаунта.

        Body (JSON):
            login    — логин клиента
            agency   — (опционально) логин агентства
            sensors  — список ключей сенсоров (["balance", "campaign_status", ...])
                       Если пусто — прогоняет все сенсоры.
        """
        body = request.json or {}
        login = (body.get("login") or "").strip()
        agency = (body.get("agency") or "").strip()
        mode = (body.get("mode") or "work")  # noqa: F841 — зарезервировано для home-источника
        sensor_keys = body.get("sensors") or list(_SENSORS.keys())

        if not login:
            return jsonify({"error": "login обязателен"}), 400

        # Токен для Direct API
        tok, ag = _resolve_token(login, agency)
        ctx = {
            "token": tok,
            "agency": ag or agency,
            "victory_conn": _deps.get("victory_conn"),
            "metrika_goals_for": _metrika_goals_for,
        }

        results = []
        for key in sensor_keys:
            if key not in _SENSORS:
                results.append({"sensor_key": key, "found": 0, "details": [],
                                 "error": f"неизвестный сенсор: {key}"})
                continue

            sensor_result = _run_single_sensor(key, _SENSORS[key], login, ctx)
            results.append({"sensor_key": key, **sensor_result})

            # Запись в БД (кэш прогонов)
            if "error" not in sensor_result or sensor_result.get("found", 0) >= 0:
                _sensor_runs_append(
                    login, key,
                    sensor_result.get("found", 0),
                    sensor_result.get("details") or [],
                )

        total_found = sum(r.get("found") or 0 for r in results)
        return jsonify({
            "login": login,
            "agency": ag or agency,
            "total_found": total_found,
            "results": results,
        })

    def _run_single_sensor(key: str, module_path: str, login: str, ctx: dict) -> dict:
        """Импортирует и запускает один сенсор. Перехватывает ошибки."""
        import importlib
        try:
            mod = importlib.import_module(f".{module_path}", package="direct")
            result = mod.run(login, ctx)
            if not isinstance(result, dict):
                return {"found": 0, "details": [], "error": "неверный формат ответа сенсора"}
            return result
        except Exception as exc:  # noqa: BLE001
            return {"found": 0, "details": [], "error": str(exc)[:200]}

    # ── Фильтры для дропдаунов Обзора ──────────────────────────────────────────

    @bp.route("/api/ar/filter-options")
    @access
    def api_ar_filter_options():
        """Уникальные значения фильтров (директолог/город/салон) для дропдаунов UI."""
        mode = request.args.get("mode", "work")
        src = _accounts_source(mode)
        if not src["available"]:
            return jsonify({"directologists": [], "cities": [], "salons": [], "note": src["message"]})

        conn = _victory_conn()
        if conn is None:
            return jsonify({"error": "Victory DB недоступна"}), 503
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT DISTINCT directologist FROM public.local_gsheet_sites "
                "WHERE direction='Авто' AND directologist IS NOT NULL ORDER BY 1 LIMIT 200"
            )
            dirs = [r[0] for r in cur.fetchall()]
            cur.execute(
                "SELECT DISTINCT city FROM public.local_gsheet_sites "
                "WHERE direction='Авто' AND city IS NOT NULL ORDER BY 1 LIMIT 500"
            )
            cities = [r[0] for r in cur.fetchall()]
            cur.execute(
                "SELECT DISTINCT salon FROM public.local_gsheet_sites "
                "WHERE direction='Авто' AND salon IS NOT NULL ORDER BY 1 LIMIT 500"
            )
            salons = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
        return jsonify({"directologists": dirs, "cities": cities, "salons": salons})

    # ── ЗАДАЧА A: Автоправила (Rules) ───────────────────────────────────────────

    def _rules_list(login=None):
        fn = _deps.get("rules_list")
        return fn(login) if fn else []

    def _rules_get(rule_id):
        fn = _deps.get("rules_get")
        return fn(rule_id) if fn else None

    def _rules_create(**kw):
        fn = _deps.get("rules_create")
        return fn(**kw) if fn else None

    def _rules_update(rule_id, **kw):
        fn = _deps.get("rules_update")
        return fn(rule_id, **kw) if fn else False

    def _rules_delete(rule_id):
        fn = _deps.get("rules_delete")
        return fn(rule_id) if fn else False

    def _rule_runs_append(rule_id, login, decision, applied=False):
        fn = _deps.get("rule_runs_append")
        try:
            if fn:
                fn(rule_id, login, decision, applied)
        except Exception:  # noqa: BLE001
            pass

    def _audit_log_append(login, entity, found, action, result, source=None):
        fn = _deps.get("audit_log_append")
        try:
            if fn:
                fn(login, entity, found, action, result, source)
        except Exception:  # noqa: BLE001
            pass

    def _audit_log_list(login=None, limit=100):
        fn = _deps.get("audit_log_list")
        return fn(login, limit) if fn else []

    @bp.route("/api/ar/rules")
    @access
    def api_ar_rules_list():
        """GET: список правил. ?login= — фильтр по аккаунту."""
        login = (request.args.get("login") or "").strip() or None
        rows = _rules_list(login)
        return jsonify({"rules": rows})

    @bp.route("/api/ar/rules", methods=["POST"])
    @access
    def api_ar_rules_create():
        """POST: создать правило.

        Body: {name, condition_json, action_json, mode, schedule, account_login}
        """
        body = request.json or {}
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name обязателен"}), 400

        condition = body.get("condition_json") or {}
        action    = body.get("action_json") or {}
        mode      = body.get("mode") or "manual"
        schedule  = body.get("schedule") or None
        login     = (body.get("account_login") or "").strip() or None

        try:
            rule_id = _rules_create(
                name=name,
                condition_json=condition,
                action_json=action,
                mode=mode,
                schedule=schedule,
                status="active",
                account_login=login,
            )
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)[:200]}), 500

        _audit_log_append(
            login or "", "rule", {"name": name, "mode": mode},
            {"type": "create_rule", "rule_id": rule_id}, "created",
            source="rules_ui",
        )
        return jsonify({"rule_id": rule_id, "ok": True})

    @bp.route("/api/ar/rules/<int:rule_id>", methods=["PUT"])
    @access
    def api_ar_rules_update(rule_id):
        """PUT: изменить статус (active/paused) или другие поля правила."""
        body = request.json or {}
        updates = {}
        if "status" in body:
            st = body["status"]
            if st not in ("active", "paused"):
                return jsonify({"error": "status: active|paused"}), 400
            updates["status"] = st
        if not updates:
            return jsonify({"error": "нет допустимых полей для обновления"}), 400

        ok = _rules_update(rule_id, **updates)
        if not ok:
            return jsonify({"error": "правило не найдено"}), 404
        return jsonify({"ok": True})

    @bp.route("/api/ar/rules/<int:rule_id>", methods=["DELETE"])
    @access
    def api_ar_rules_delete(rule_id):
        """DELETE: удалить правило."""
        ok = _rules_delete(rule_id)
        if not ok:
            return jsonify({"error": "правило не найдено"}), 404
        return jsonify({"ok": True})

    @bp.route("/api/ar/rules/dryrun", methods=["POST"])
    @access
    def api_ar_rules_dryrun():
        """POST: dry-run правила против данных аккаунта.

        Body:
          rule_id      — int: использовать сохранённое правило
          OR
          rule         — dict: временное правило (из конструктора)
          login        — str: логин аккаунта (обязателен)
        """
        from direct.autorules.rules_engine import dry_run_rule

        body  = request.json or {}
        login = (body.get("login") or "").strip()
        if not login:
            return jsonify({"error": "login обязателен"}), 400

        mode = (body.get("mode") or "work")
        src  = _accounts_source(mode)
        if not src["available"]:
            return jsonify({"error": src["message"]}), 400

        # Правило: из БД или из тела запроса
        rule = None
        if body.get("rule_id"):
            rule = _rules_get(int(body["rule_id"]))
            if not rule:
                return jsonify({"error": "правило не найдено"}), 404
        elif body.get("rule"):
            rule = body["rule"]
        else:
            return jsonify({"error": "rule_id или rule обязателен"}), 400

        ctx = {
            "token":        None,   # dry-run берёт данные из Victory DB, токен не нужен
            "victory_conn": _deps.get("victory_conn"),
        }

        result = dry_run_rule(rule, login, ctx)

        # Записываем прогон в БД и audit_log
        decision = {
            "dry_run": True,
            "matched": result.get("matched"),
            "preview": result.get("preview"),
        }
        rule_id_val = rule.get("id")
        _rule_runs_append(rule_id_val, login, decision, applied=False)
        _audit_log_append(
            login, "rule_dryrun",
            {"rule_name": rule.get("name", ""), "matched": result.get("matched")},
            {"type": "dryrun", "rule_id": rule_id_val},
            "dryrun_ok" if not result.get("error") else f"dryrun_error: {result['error']}",
            source="rules_ui",
        )
        return jsonify(result)

    # ── ЗАДАЧА B: Корректировки ─────────────────────────────────────────────────

    @bp.route("/api/ar/adjust")
    @access
    def api_ar_adjust_get():
        """GET: текущие корректировки аккаунта (пол/возраст/устройства).

        Параметры: login (обязателен), mode.
        """
        from direct.autorules.corrections import get_bid_modifiers

        mode  = request.args.get("mode", "work")
        login = (request.args.get("login") or "").strip()
        if not login:
            return jsonify({"error": "login обязателен"}), 400

        src = _accounts_source(mode)
        if not src["available"]:
            return jsonify({**{"gender": [], "age": [], "device": []},
                            "note": src["message"]})

        tok, _ = _resolve_token(login)
        if not tok:
            return jsonify({"error": "нет токена для этого логина"}), 400

        data = get_bid_modifiers(tok, login)
        return jsonify(data)

    @bp.route("/api/ar/adjust", methods=["POST"])
    @access
    def api_ar_adjust_set():
        """POST: установить корректировки ставок.

        Body:
            login       — str (обязателен)
            modifiers   — list[{type, segment, adjustment}]
            campaign_ids— list[int] (опционально; если пусто — все кампании)
            confirmed   — bool (ОБЯЗАТЕЛЕН: защита от случайной записи)
        """
        from direct.autorules.corrections import get_campaign_ids, set_bid_modifiers

        body    = request.json or {}
        login   = (body.get("login") or "").strip()
        confirmed = bool(body.get("confirmed", False))
        modifiers = body.get("modifiers") or []
        campaign_ids = body.get("campaign_ids") or []

        if not login:
            return jsonify({"error": "login обязателен"}), 400
        if not confirmed:
            return jsonify({"error": "confirmed=true обязателен для записи в аккаунт"}), 400
        if not modifiers:
            return jsonify({"error": "modifiers не переданы"}), 400

        mode = (body.get("mode") or "work")
        src  = _accounts_source(mode)
        if not src["available"]:
            return jsonify({"error": src["message"]}), 400

        tok, _ = _resolve_token(login)
        if not tok:
            return jsonify({"error": "нет токена для этого логина"}), 400

        # Если campaign_ids не переданы — берём все
        if not campaign_ids:
            try:
                campaign_ids = get_campaign_ids(tok, login)[:50]
            except Exception as exc:  # noqa: BLE001
                return jsonify({"error": f"ошибка получения кампаний: {exc}"}), 500

        if not campaign_ids:
            return jsonify({"error": "в аккаунте нет кампаний"}), 400

        result = set_bid_modifiers(tok, login, modifiers, campaign_ids)

        # Audit log
        _audit_log_append(
            login, "bid_modifiers",
            {"modifiers": modifiers, "campaign_ids": list(campaign_ids[:5])},
            {"type": "set_bid_modifiers"},
            f"ok={result.get('ok')} failed={result.get('failed')}",
            source="adjust_ui",
        )
        return jsonify(result)

    # ── ЗАДАЧА C: Поисковые запросы ─────────────────────────────────────────────

    @bp.route("/api/ar/queries")
    @access
    def api_ar_queries_get():
        """GET: поисковые запросы аккаунта (Reports API, может занять до 90 с).

        Параметры: login (обязателен), days (7|14|30), mode.
        """
        from direct.autorules.queries import get_search_queries

        mode  = request.args.get("mode", "work")
        login = (request.args.get("login") or "").strip()
        days  = int(request.args.get("days", 7))
        if not login:
            return jsonify({"error": "login обязателен"}), 400

        src = _accounts_source(mode)
        if not src["available"]:
            return jsonify({"rows": [], "total_rows": 0, "note": src["message"]})

        tok, _ = _resolve_token(login)
        if not tok:
            return jsonify({"error": "нет токена для этого логина"}), 400

        data = get_search_queries(tok, login, days)
        return jsonify(data)

    @bp.route("/api/ar/queries/add-minus", methods=["POST"])
    @access
    def api_ar_queries_add_minus():
        """POST: добавить отмеченные запросы в минус-фразы.

        Body:
            login        — str (обязателен)
            phrases      — list[str] (запросы для добавления)
            campaign_ids — list[int] (опционально; если пусто — все TEXT_CAMPAIGN)
            confirmed    — bool (ОБЯЗАТЕЛЕН)
        """
        from direct.autorules.queries import add_negative_phrases

        body      = request.json or {}
        login     = (body.get("login") or "").strip()
        confirmed = bool(body.get("confirmed", False))
        phrases   = body.get("phrases") or []
        campaign_ids = body.get("campaign_ids") or []

        if not login:
            return jsonify({"error": "login обязателен"}), 400
        if not confirmed:
            return jsonify({"error": "confirmed=true обязателен"}), 400
        if not phrases:
            return jsonify({"error": "phrases не переданы"}), 400

        mode = (body.get("mode") or "work")
        src  = _accounts_source(mode)
        if not src["available"]:
            return jsonify({"error": src["message"]}), 400

        tok, _ = _resolve_token(login)
        if not tok:
            return jsonify({"error": "нет токена для этого логина"}), 400

        result = add_negative_phrases(tok, login, phrases, campaign_ids or None)

        _audit_log_append(
            login, "negative_keywords",
            {"phrases": phrases[:20], "campaign_ids": campaign_ids[:5]},
            {"type": "add_negative_phrases", "count": len(phrases)},
            f"ok={result.get('ok')} failed={result.get('failed')} skipped={result.get('skipped')}",
            source="queries_ui",
        )
        return jsonify(result)

    # ── ЗАДАЧА D: Площадки ──────────────────────────────────────────────────────

    @bp.route("/api/ar/placements")
    @access
    def api_ar_placements_get():
        """GET: площадки РСЯ аккаунта (Reports API, может занять до 90 с).

        Параметры: login (обязателен), days (7|14|30), mode.
        """
        from direct.autorules.placements import get_placements

        mode  = request.args.get("mode", "work")
        login = (request.args.get("login") or "").strip()
        days  = int(request.args.get("days", 7))
        if not login:
            return jsonify({"error": "login обязателен"}), 400

        src = _accounts_source(mode)
        if not src["available"]:
            return jsonify({"rows": [], "total_rows": 0, "note": src["message"]})

        tok, _ = _resolve_token(login)
        if not tok:
            return jsonify({"error": "нет токена для этого логина"}), 400

        data = get_placements(tok, login, days)
        return jsonify(data)

    @bp.route("/api/ar/placements/exclude", methods=["POST"])
    @access
    def api_ar_placements_exclude():
        """POST: отметить площадки для исключения.

        Записывает в audit_log (v5 API не поддерживает прямое исключение площадок).
        Пользователь получает список + инструкцию для ручного добавления.

        Body:
            login     — str (обязателен)
            sites     — list[str]
            confirmed — bool (ОБЯЗАТЕЛЕН)
        """
        from direct.autorules.placements import log_excluded_sites

        body      = request.json or {}
        login     = (body.get("login") or "").strip()
        confirmed = bool(body.get("confirmed", False))
        sites     = body.get("sites") or []

        if not login:
            return jsonify({"error": "login обязателен"}), 400
        if not confirmed:
            return jsonify({"error": "confirmed=true обязателен"}), 400
        if not sites:
            return jsonify({"error": "sites не переданы"}), 400

        mode = (body.get("mode") or "work")
        src  = _accounts_source(mode)
        if not src["available"]:
            return jsonify({"error": src["message"]}), 400

        result = log_excluded_sites(sites, login)

        _audit_log_append(
            login, "excluded_sites",
            {"sites": sites[:20]},
            {"type": "log_excluded_sites", "count": len(sites), "action": "manual_required"},
            "logged_only",
            source="placements_ui",
        )
        return jsonify(result)

    # ── ЗАДАЧА E: Журнал ────────────────────────────────────────────────────────

    @bp.route("/api/ar/log")
    @access
    def api_ar_log():
        """GET: audit_log сервиса.

        Параметры: login (фильтр по аккаунту, опционально), limit (max 500).
        """
        login = (request.args.get("login") or "").strip() or None
        limit = min(int(request.args.get("limit", 100)), 500)

        rows = _audit_log_list(login, limit)
        return jsonify({"rows": rows, "total": len(rows)})

    # ── Копирование 1:1 внутри аккаунта ─────────────────────────────────────────

    @bp.route("/api/ar/copy/campaigns")
    @access
    def api_ar_copy_campaigns():
        """Список кампаний аккаунта для вкладки «Копирование».

        Параметры: login (обязателен), mode (work|home).
        Возвращает {campaigns: [{id, name, type, state, status}], error: str|None}.
        """
        from direct.autorules.copy import list_campaigns as _list_camps

        mode = request.args.get("mode", "work")
        login = (request.args.get("login") or "").strip()
        if not login:
            return jsonify({"error": "login обязателен"}), 400

        src = _accounts_source(mode)
        if not src["available"]:
            return jsonify({"campaigns": [], "note": src["message"]})

        tok, _ag = _resolve_token(login)
        if not tok:
            return jsonify({"campaigns": [], "error": "нет токена для этого логина"}), 400

        result = _list_camps(tok, login)
        return jsonify(result)

    @bp.route("/api/ar/copy/run", methods=["POST"])
    @access
    def api_ar_copy_run():
        """Дублировать выбранные кампании 1:1 как черновики State=OFF в том же аккаунте.

        Body (JSON):
            login        — логин клиента (источник = приёмник = тот же аккаунт)
            campaign_ids — список int ID кампаний
            mode         — work|home
            dry_run      — bool (default False); True = показать что создастся без API-записи

        🔴 ИНВАРИАНТ: создаются ТОЛЬКО черновики (State=OFF/DRAFT).
           Никогда не включать кампании, не менять существующие, не удалять.
        """
        from direct.autorules.copy import clone_campaigns_1to1 as _clone

        body = request.json or {}
        login = (body.get("login") or "").strip()
        mode = (body.get("mode") or "work")
        dry_run = bool(body.get("dry_run", False))
        raw_ids = body.get("campaign_ids") or []
        campaign_ids = [int(x) for x in raw_ids if str(x).lstrip("-").isdigit() and int(x) > 0]

        if not login:
            return jsonify({"error": "login обязателен"}), 400
        if not campaign_ids:
            return jsonify({"error": "выберите хотя бы одну кампанию"}), 400

        src = _accounts_source(mode)
        if not src["available"]:
            return jsonify({"error": src["message"]}), 400

        tok, _ag = _resolve_token(login)
        if not tok:
            return jsonify({"error": "нет токена для этого логина"}), 400

        result = _clone(tok, login, campaign_ids, dry_run=dry_run)
        return jsonify(result)

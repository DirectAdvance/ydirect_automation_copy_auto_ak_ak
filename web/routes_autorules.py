"""Autorules page routes + Phase 2 API (overview, goals, balance, sensors)."""

from __future__ import annotations

import threading
import json
import os

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


def _home_balance_accounts() -> list[dict]:
    """Home-mode Direct accounts shared with the Delta dashboard."""
    try:
        from delta.service import BALANCE_ACCOUNTS  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return []
    return [dict(row) for row in BALANCE_ACCOUNTS]


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

    def _saved_home_accounts() -> list[dict]:
        fn = _deps.get("home_accounts_list")
        if not fn:
            return []
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return []

    def _all_home_accounts() -> list[dict]:
        merged: dict[str, dict] = {}
        for account in _home_balance_accounts():
            login = str(account.get("login") or "").strip()
            if not login:
                continue
            merged[login.lower()] = {
                "label": account.get("label") or login,
                "login": login,
                "account_id": account.get("account_id"),
                "token_env": account.get("token_env"),
                "agency_login": "skuderko1",
                "builtin": True,
            }
        for account in _saved_home_accounts():
            login = str(account.get("login") or "").strip()
            if not login:
                continue
            prev = merged.get(login.lower(), {})
            merged[login.lower()] = {
                **prev,
                "label": account.get("label") or prev.get("label") or login,
                "login": login,
                "account_id": account.get("account_id") or prev.get("account_id"),
                "token_env": prev.get("token_env"),
                "agency_login": account.get("agency_login") or "skuderko1",
                "builtin": bool(prev.get("builtin")),
                "saved": True,
            }
        return list(merged.values())

    def _home_account(login: str) -> dict | None:
        login_norm = (login or "").strip().lower()
        for account in _all_home_accounts():
            if str(account.get("login") or "").strip().lower() == login_norm:
                return account
        return None

    def _resolve_token_for_mode(login: str, mode: str = "work", agency: str = "") -> tuple[str | None, str | None]:
        if mode == "home":
            account = _home_account(login)
            if not account:
                return None, None
            token_env = account.get("token_env")
            if token_env:
                try:
                    from loader import _get  # noqa: PLC0415
                    token = _get(token_env)
                except Exception:  # noqa: BLE001
                    token = None
                return token, "home" if token else None
            return _resolve_token(login, account.get("agency_login") or agency or "skuderko1")
        return _resolve_token(login, agency)

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
        Home  → локальный список личных аккаунтов из Delta dashboard.
        """
        if mode == "home":
            return {
                "available": True,
                "home": True,
                "accounts": _all_home_accounts(),
            }
        return {"available": True}  # work → Victory DB через _victory_conn()

    # ── Страница ────────────────────────────────────────────────────────────────

    @bp.route("/autorules", defaults={"page_mode": "work"})
    @bp.route("/autorules/home", defaults={"page_mode": "home"})
    @access
    def autorules(page_mode: str):
        """Страница «Автоправила Директа»."""
        return render_template(
            "direct/autorules.html",
            active_section="services",
            active_page="direct_autorules",
            hide_scope=request.args.get("hide_scope") == "1",
            page_mode=page_mode,
            home_accounts=_all_home_accounts(),
        )

    @bp.route("/api/ar/home-accounts")
    @access
    def api_ar_home_accounts():
        """Home-mode account list: built-ins + saved accounts."""
        return jsonify({"accounts": _all_home_accounts()})

    @bp.route("/api/ar/home-accounts", methods=["POST"])
    @access
    def api_ar_home_accounts_save():
        """Save a Home-mode account. Secrets are not stored here."""
        body = request.json or {}
        login = (body.get("login") or "").strip()
        if not login:
            return jsonify({"error": "login обязателен"}), 400
        label = (body.get("label") or login).strip()
        raw_account_id = str(body.get("account_id") or "").strip()
        account_id = int(raw_account_id) if raw_account_id.isdigit() else None
        agency_login = (body.get("agency_login") or "skuderko1").strip() or "skuderko1"
        fn = _deps.get("home_account_upsert")
        if not fn:
            return jsonify({"error": "сохранение Home-аккаунтов не настроено"}), 503
        try:
            row = fn(login=login, label=label, account_id=account_id, agency_login=agency_login)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)[:200]}), 500
        return jsonify({"ok": True, "account": row, "accounts": _all_home_accounts()})

    @bp.route("/api/ar/access-status")
    @access
    def api_ar_access_status():
        """Check whether selected logins have an active token and cookie."""
        logins = [
            item.strip()
            for item in (request.args.get("logins") or "").split(",")
            if item.strip()
        ]
        login_q = (request.args.get("login") or "").strip()
        if login_q and login_q not in logins:
            logins.append(login_q)
        if not logins:
            return jsonify({"error": "login/logins обязателен"}), 400
        mode = request.args.get("mode", "work")
        agency = (request.args.get("agency") or "").strip()
        force = request.args.get("force") == "1"
        return jsonify({
            "rows": [_check_access_status(login, mode=mode, agency=agency, force=force) for login in logins[:20]],
        })

    def _check_access_status(login: str, mode: str = "work", agency: str = "", force: bool = False) -> dict:
        token_ok = False
        token_error = None
        token_agency = None
        try:
            if mode == "home":
                probe = _fetch_single_balance(login, "", mode=mode)
                if probe.get("balance") is not None:
                    token_ok = True
                    token_agency = (_home_account(login) or {}).get("agency_login") or "skuderko1"
                else:
                    token_error = probe.get("error") or "баланс не получен"
            else:
                from direct.clients.yandex_gateway import v5_get, v5_err  # noqa: PLC0415
                tok, token_agency = _resolve_token_for_mode(login, mode, agency)
                if not tok:
                    token_error = "нет токена"
                else:
                    probe = v5_get(
                        "campaigns",
                        tok,
                        login,
                        ["Id"],
                        criteria={},
                        extra={"Page": {"Limit": 1}},
                    )
                    if "error" in probe:
                        token_error = v5_err(probe)[:160]
                    else:
                        token_ok = True
        except Exception as exc:  # noqa: BLE001
            token_error = str(exc)[:160]

        cookie_ok = False
        cookie_error = None
        cookie_agency = None
        try:
            from direct.core import campaign as cmc  # noqa: PLC0415
            account = _home_account(login) if mode == "home" else None
            cookie_agency = (
                (account or {}).get("agency_login")
                or token_agency
                or agency
                or "skuderko1"
            )
            accounts = (cookie_agency,) if cookie_agency else tuple()
            cookie = cmc.pick_working_cookie(login, accounts=accounts, force_refresh=force)
            cookie_ok = bool(cookie)
            if not cookie_ok:
                cookie_error = "кука не найдена"
        except Exception as exc:  # noqa: BLE001
            cookie_error = str(exc)[:160]

        return {
            "login": login,
            "token": {"ok": token_ok, "agency": token_agency, "error": token_error},
            "cookie": {"ok": cookie_ok, "agency": cookie_agency, "error": cookie_error},
        }

    @bp.route("/api/ar/cookies", methods=["POST"])
    @access
    def api_ar_cookie_save():
        """Save a Yandex Direct cookie string in the local secret cookie store."""
        body = request.json or {}
        raw = (body.get("cookie") or "").strip()
        if not raw:
            return jsonify({"error": "cookie обязателен"}), 400
        try:
            agency_login, cookie = _parse_cookie_input(raw, body.get("agency_login") or "skuderko1")
            path = _save_local_cookie(agency_login, cookie)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)[:200]}), 500
        return jsonify({"ok": True, "agency_login": agency_login, "path": str(path.name)})

    def _parse_cookie_input(raw: str, fallback_agency: str = "skuderko1") -> tuple[str, str]:
        text = (raw or "").strip()
        if text.startswith("#"):
            text = text[1:].strip()
        agency = (fallback_agency or "skuderko1").strip()
        cookie = text
        if "/" in text:
            left, right = text.split("/", 1)
            if left.strip():
                agency = left.strip().lstrip("#").strip()
            cookie = right.strip()
        if not agency:
            raise ValueError("не удалось определить логин куки")
        if "=" not in cookie or ";" not in cookie:
            raise ValueError("строка куки выглядит неполной")
        return agency, cookie

    def _save_local_cookie(agency_login: str, cookie: str):
        from direct.core import campaign as cmc  # noqa: PLC0415

        secret_dir = cmc._find_secret_dir()
        cookie_path = secret_dir / "cookies.json"
        try:
            data = json.loads(cookie_path.read_text(encoding="utf-8")) if cookie_path.exists() else {}
        except json.JSONDecodeError:
            data = {}
        data[agency_login] = cookie
        tmp_path = cookie_path.with_suffix(cookie_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, cookie_path)
        # Drop cached cookie probes in this process; the next check must use the saved value.
        try:
            cmc._ACCOUNT_COOKIE_CACHE.clear()
        except Exception:  # noqa: BLE001
            pass
        return cookie_path

    # ── ЗАДАЧА A: Обзор ─────────────────────────────────────────────────────────

    @bp.route("/api/ar/overview")
    @access
    def api_ar_overview():
        """Список аккаунтов из local_gsheet_sites + расход-факт текущего месяца.

        Параметры (все опциональные):
          login        — конкретный логин (вернём 1 запись + попробуем подтянуть баланс)
          logins       — Home: список логинов через запятую
          directologist — фильтр по директологу
          city          — фильтр по городу
          salon         — фильтр по салону
          with_balance  — "1" → подтянуть баланс
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
        logins_q = [
            item.strip()
            for item in (request.args.get("logins") or "").split(",")
            if item.strip()
        ]
        dir_q   = (request.args.get("directologist") or "").strip()
        city_q  = (request.args.get("city") or "").strip()
        salon_q = (request.args.get("salon") or "").strip()
        with_balance = request.args.get("with_balance") == "1"

        if src.get("home"):
            accounts = src.get("accounts") or []
            wanted = set(logins_q or ([login_q] if login_q else []))
            if wanted:
                accounts = [a for a in accounts if str(a.get("login") or "") in wanted]
            rows = []
            for account in accounts:
                rows.append({
                    "login": account.get("login") or "",
                    "domain": "Строительный Двор",
                    "salon": account.get("label") or "",
                    "city": "",
                    "directologist": "Home",
                    "status": "работает",
                    "agency_account": "",
                    "site_type": "Стройдвор",
                    "otkrut_fact": 0,
                })
            balance_data = None
            balances = {}
            if with_balance and rows:
                for row in rows:
                    balance = _fetch_single_balance(row["login"], "", mode=mode)
                    balances[row["login"]] = balance
                    if login_q and row["login"] == login_q:
                        balance_data = balance
            return jsonify({
                "rows": rows,
                "total": len(rows),
                "directologists": [],
                "cities": [],
                "balance": balance_data,
                "balances": balances,
                "home_accounts": src.get("accounts") or [],
            })

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
            balance_data = _fetch_single_balance(login_q, rows[0].get("agency_account") or "", mode=mode)

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
        mode = request.args.get("mode", "work")
        login = (request.args.get("login") or "").strip()
        agency = (request.args.get("agency") or "").strip()
        if not login:
            return jsonify({"error": "login обязателен"}), 400

        data = _fetch_single_balance(login, agency, mode=mode)
        return jsonify(data)

    def _fetch_single_balance(login: str, agency: str = "", mode: str = "work") -> dict:
        import requests as rqs
        tok, ag = _resolve_token_for_mode(login, mode, agency)
        if not tok:
            return {"balance": None, "error": "нет токена для этого логина"}
        try:
            account = _home_account(login) if mode == "home" else None
            account_id = account.get("account_id") if account else None
            criteria = (
                {"AccountIDS": [int(account_id)]}
                if account_id else
                {"Logins": [login]}
            )
            body = {
                "method": "AccountManagement",
                "token": tok,
                "param": {"Action": "Get", "SelectionCriteria": criteria},
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
        mode = request.args.get("mode", "work")  # noqa: F841 — цели Home подключим отдельно
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
        mode = (body.get("mode") or "work")
        sensor_keys = body.get("sensors") or list(_SENSORS.keys())

        if not login:
            return jsonify({"error": "login обязателен"}), 400

        # Токен для Direct API
        tok, ag = _resolve_token_for_mode(login, mode, agency)
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
        if src.get("home"):
            return jsonify({
                "directologists": [],
                "cities": [],
                "salons": [],
                "home_accounts": src.get("accounts") or [],
            })

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

    def _optimizer_events_list(login=None):
        fn = _deps.get("optimizer_events_list")
        return fn(login) if fn else []

    def _optimizer_event_get(event_id):
        fn = _deps.get("optimizer_event_get")
        return fn(event_id) if fn else None

    def _optimizer_event_create(**kw):
        fn = _deps.get("optimizer_event_create")
        if not fn:
            raise RuntimeError("optimizer_event_create dependency is not configured")
        return fn(**kw)

    def _optimizer_event_update(event_id, **kw):
        fn = _deps.get("optimizer_event_update")
        return fn(event_id, **kw) if fn else False

    def _optimizer_event_delete(event_id):
        fn = _deps.get("optimizer_event_delete")
        return fn(event_id) if fn else False

    def _optimizer_event_runs_append(event_id, login, preview, applied=False):
        fn = _deps.get("optimizer_event_runs_append")
        if fn:
            try:
                return fn(event_id, login, preview, applied)
            except Exception:  # noqa: BLE001
                pass
        return None

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

    # ── HOME: K50-style optimizer events ─────────────────────────────────────

    @bp.route("/api/ar/home/optimizer/templates")
    @access
    def api_ar_home_optimizer_templates():
        """List K50-style templates for Home optimizer."""
        from direct.autorules.k50_optimizer import templates_list

        return jsonify({"templates": templates_list()})

    @bp.route("/api/ar/home/optimizer/events")
    @access
    def api_ar_home_optimizer_events_list():
        """List saved Home optimizer events for selected account."""
        login = (request.args.get("login") or "").strip() or None
        rows = _optimizer_events_list(login)
        return jsonify({"events": rows, "total": len(rows)})

    @bp.route("/api/ar/home/optimizer/events", methods=["POST"])
    @access
    def api_ar_home_optimizer_events_create():
        """Create a Home optimizer event from K50-style template or explicit rules."""
        from direct.autorules.k50_optimizer import event_from_template

        body = request.json or {}
        login = (body.get("login") or body.get("account_login") or "").strip()
        if not login:
            return jsonify({"error": "login обязателен"}), 400
        if login not in {a["login"] for a in _all_home_accounts()}:
            return jsonify({"error": "Home-аккаунт не найден"}), 404

        try:
            if body.get("template_key"):
                event = event_from_template(body["template_key"], login, body.get("settings") or {})
            else:
                event = {
                    "account_login": login,
                    "name": (body.get("name") or "").strip(),
                    "schedule": body.get("schedule") or "weekly",
                    "mode": body.get("mode") or "manual",
                    "data_lag_days": int(body.get("data_lag_days") or 1),
                    "template_key": body.get("template_key") or None,
                    "settings_json": body.get("settings_json") or body.get("settings") or {},
                    "rules": body.get("rules") or [],
                    "status": body.get("status") or "active",
                }
            if not event["name"]:
                return jsonify({"error": "name обязателен"}), 400
            event_id = _optimizer_event_create(**event)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)[:200]}), 400

        _audit_log_append(
            login,
            "optimizer_event",
            {"template_key": event.get("template_key"), "rules": len(event.get("rules") or [])},
            {"type": "create_optimizer_event", "event_id": event_id},
            "created",
            source="home_optimizer",
        )
        return jsonify({"ok": True, "event_id": event_id, "events": _optimizer_events_list(login)})

    @bp.route("/api/ar/home/optimizer/events/<int:event_id>", methods=["PUT"])
    @access
    def api_ar_home_optimizer_events_update(event_id):
        """Update event status/mode/schedule/lags."""
        body = request.json or {}
        updates = {}
        for key in ("name", "schedule", "mode", "data_lag_days", "status", "settings_json"):
            if key in body:
                updates[key] = body[key]
        if "status" in updates and updates["status"] not in ("active", "paused"):
            return jsonify({"error": "status: active|paused"}), 400
        if "mode" in updates and updates["mode"] not in ("dryrun", "manual", "auto"):
            return jsonify({"error": "mode: dryrun|manual|auto"}), 400
        ok = _optimizer_event_update(event_id, **updates)
        if not ok:
            return jsonify({"error": "event not found"}), 404
        return jsonify({"ok": True})

    @bp.route("/api/ar/home/optimizer/events/<int:event_id>", methods=["DELETE"])
    @access
    def api_ar_home_optimizer_events_delete(event_id):
        ok = _optimizer_event_delete(event_id)
        if not ok:
            return jsonify({"error": "event not found"}), 404
        return jsonify({"ok": True})

    @bp.route("/api/ar/home/optimizer/events/<int:event_id>/preview", methods=["POST"])
    @access
    def api_ar_home_optimizer_events_preview(event_id):
        """Preview saved event, no Direct writes."""
        from direct.autorules.k50_optimizer import preview_event

        event = _optimizer_event_get(event_id)
        if not event:
            return jsonify({"error": "event not found"}), 404
        ctx = {"victory_conn": _deps.get("victory_conn")}
        preview = preview_event(event, ctx)
        _optimizer_event_runs_append(event_id, event.get("account_login") or "", preview, applied=False)
        _audit_log_append(
            event.get("account_login") or "",
            "optimizer_preview",
            {"event_id": event_id, "matches": (preview.get("summary") or {}).get("matches")},
            {"type": "preview_optimizer_event", "event_id": event_id},
            "preview_ok" if preview.get("ok") else "preview_error",
            source="home_optimizer",
        )
        return jsonify(preview)

    @bp.route("/api/ar/home/optimizer/preview", methods=["POST"])
    @access
    def api_ar_home_optimizer_preview_template():
        """Preview a template without saving it."""
        from direct.autorules.k50_optimizer import event_from_template, preview_event

        body = request.json or {}
        login = (body.get("login") or "").strip()
        template_key = (body.get("template_key") or "").strip()
        if not login or not template_key:
            return jsonify({"error": "login и template_key обязательны"}), 400
        if login not in {a["login"] for a in _all_home_accounts()}:
            return jsonify({"error": "Home-аккаунт не найден"}), 404
        try:
            event = event_from_template(template_key, login, body.get("settings") or {})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)[:160]}), 400
        return jsonify(preview_event(event, {"victory_conn": _deps.get("victory_conn")}))

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

        tok, _ = _resolve_token_for_mode(login, mode)
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

        tok, _ = _resolve_token_for_mode(login, mode)
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

        tok, _ = _resolve_token_for_mode(login, mode)
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

        tok, _ = _resolve_token_for_mode(login, mode)
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

        tok, _ = _resolve_token_for_mode(login, mode)
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

        tok, _ag = _resolve_token_for_mode(login, mode)
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

        tok, _ag = _resolve_token_for_mode(login, mode)
        if not tok:
            return jsonify({"error": "нет токена для этого логина"}), 400

        result = _clone(tok, login, campaign_ids, dry_run=dry_run)
        return jsonify(result)

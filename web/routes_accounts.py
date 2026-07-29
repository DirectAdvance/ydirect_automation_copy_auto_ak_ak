"""Read-only account routes for Direct automation."""

from __future__ import annotations

import os
import threading
import time
from typing import Callable

from flask import jsonify, request


_ACCOUNTS_CACHE: dict[str, tuple[float, list[dict]]] = {}
_ACCOUNTS_CACHE_LOCK = threading.Lock()
_ACCOUNTS_CACHE_TTL = max(5, int(os.environ.get("DIRECT_ACCOUNTS_CACHE_TTL", "60")))


def register_account_routes(
    bp,
    access,
    *,
    victory_conn: Callable,
    parse_counter_ids: Callable,
    parse_number: Callable,
    metrika_goals_for: Callable,
    resolve_region: Callable,
    account_prefill_func: Callable,
    account_assets_func: Callable,
    account_audiences_func: Callable,
    balance_func: Callable,
    pull_begin: Callable,
    pull_end: Callable,
    busy_response: Callable,
    cooldowns: dict,
    goal_vse_formy: Callable,
    account_cols: list[str],
    default_status: str,
    exclude_directologs: list[str],
) -> None:
    @bp.route("/api/statuses")
    @access
    def api_statuses():
        """Список статусов direction='Авто' для фильтра (с количеством)."""
        import psycopg2.extras

        conn = victory_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT status, count(*) AS n FROM public.local_gsheet_sites "
                "WHERE direction='Авто' AND status IS NOT NULL AND status<>'' "
                "GROUP BY status ORDER BY n DESC"
            )
            return jsonify({"default": default_status, "statuses": cur.fetchall()})
        finally:
            conn.close()

    @bp.route("/api/accounts")
    @access
    def api_accounts():
        """Аккаунты из local_gsheet_sites (direction='Авто') с фильтром статуса и поиском."""
        import psycopg2.extras

        from ..account_filters import base_account_where
        status = (request.args.get("status") or default_status).strip()
        q = (request.args.get("q") or "").strip()
        cache_key = status if not q else ""
        if cache_key:
            with _ACCOUNTS_CACHE_LOCK:
                cached = _ACCOUNTS_CACHE.get(cache_key)
            if cached and (time.monotonic() - cached[0]) < _ACCOUNTS_CACHE_TTL:
                return jsonify({"rows": cached[1], "cached": True})
        where = list(base_account_where()) + [
            "(directologist IS NULL OR directologist <> ALL(%s))",
        ]
        params: list = [exclude_directologs]
        if status and status != "__all__":
            where.append("status=%s")
            params.append(status)
        if q:
            where.append(
                "(domain ILIKE %s OR salon ILIKE %s OR login_key ILIKE %s "
                "OR directologist ILIKE %s OR city ILIKE %s OR site_type ILIKE %s)"
            )
            params += [f"%{q}%"] * 6

        cols_sel = ", ".join("s." + c for c in account_cols)
        sql = (
            f"SELECT {cols_sel}, mg.counter_ids AS mg_counter_ids, mg.all_forms AS mg_all_forms "
            f"FROM public.local_gsheet_sites s "
            f"LEFT JOIN public.metrika_goals mg ON mg.account_login = s.login_key "
            f"WHERE {' AND '.join(where)} ORDER BY s.domain LIMIT 2000"
        )
        conn = victory_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql, params)
            rows = cur.fetchall()
        finally:
            conn.close()
        for r in rows:
            r["mg_counters"] = parse_counter_ids(r.pop("mg_counter_ids", None))
            r["mg_goal"] = r.pop("mg_all_forms", None)
        if cache_key:
            with _ACCOUNTS_CACHE_LOCK:
                _ACCOUNTS_CACHE[cache_key] = (time.monotonic(), rows)
        return jsonify({"rows": rows, "cached": False})

    @bp.route("/api/accounts_otkrut")
    @access
    def api_accounts_otkrut():
        """Открут_Факт по аккаунтам за текущий месяц."""
        conn = victory_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT account_login, sum(total_cost)::double precision "
                "FROM public.yandex_direct_manager_reports "
                "WHERE left(\"Date\", 7) = to_char(now(), 'YYYY-MM') GROUP BY account_login"
            )
            fact = {r[0]: r[1] for r in cur.fetchall() if r[0]}
        finally:
            conn.close()
        return jsonify({"fact": fact})

    @bp.route("/api/account_info")
    @access
    def api_account_info():
        """Лёгкая инфа по логину из БД — для подсказки типа сайта при вводе."""
        import psycopg2.extras

        login = (request.args.get("login") or "").strip()
        if not login:
            return jsonify({})
        conn = victory_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT domain, salon, city, site_type, counter_number, agency_account, directologist "
                "FROM public.local_gsheet_sites WHERE login_key=%s AND direction='Авто' LIMIT 1",
                (login,),
            )
            info = cur.fetchone() or {}
        finally:
            conn.close()
        mg = metrika_goals_for(login)
        info["mg_counters"] = mg["counters"] if mg else []
        r_code, oblast = resolve_region(info.get("city"))
        info["r_code"] = r_code
        info["oblast"] = oblast
        return jsonify(info)

    @bp.route("/api/account_stats")
    @access
    def api_account_stats():
        """Статистика по аккаунту помесячно из public.yandex_direct_manager_reports."""
        import psycopg2.extras

        login = (request.args.get("login") or "").strip()
        if not login:
            return jsonify({"error": "login обязателен"}), 400
        conn = victory_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT date_trunc('month', "Date"::date)::date            AS month,
                       ROUND(SUM(COALESCE(total_cost,0))::numeric, 2)     AS total_cost,
                       COALESCE(SUM(all_forms),0)::bigint                 AS all_forms,
                       CASE WHEN SUM(all_forms) > 0
                            THEN ROUND(SUM(COALESCE(total_cost,0))::numeric / NULLIF(SUM(all_forms),0), 2)
                            ELSE NULL END                                  AS cpl
                FROM public.yandex_direct_manager_reports
                WHERE account_login = %s
                GROUP BY date_trunc('month', "Date"::date)
                ORDER BY month
                """,
                (login,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        months = [
            {
                "month": row["month"].isoformat(),
                "total_cost": float(row["total_cost"]),
                "all_forms": int(row["all_forms"]),
                "cpl": float(row["cpl"]) if row["cpl"] is not None else None,
            }
            for row in rows
        ]
        return jsonify({"login": login, "months": months})

    @bp.route("/api/account_prefill")
    @access
    def api_account_prefill():
        """Значения для формы по логину."""
        return account_prefill_func()

    @bp.route("/api/account_assets")
    @access
    def api_account_assets():
        """Фиды, аудитории и промоакции, заведённые на аккаунте."""
        return account_assets_func()

    @bp.route("/api/account_audiences")
    @access
    def api_account_audiences():
        """Аудитории типа RETARGETING для корректировок ставок."""
        return account_audiences_func()

    @bp.route("/api/balance", methods=["POST"])
    @access
    def api_balance():
        """Баланс по логинам через официальный API."""
        import requests as requests_module
        from concurrent.futures import ThreadPoolExecutor, as_completed

        ok, reason, wait = pull_begin("balance", cooldowns["balance"])
        if not ok:
            return busy_response(reason, wait)
        try:
            return balance_func(requests_module, ThreadPoolExecutor, as_completed)
        finally:
            pull_end("balance")

    @bp.route("/api/goal_for_counter")
    @access
    def api_goal_for_counter():
        """Цель «Все формы» по номеру счётчика Метрики."""
        counter = parse_number(request.args.get("counter"), 0)
        if not counter:
            return jsonify({"error": "counter обязателен"})
        gid, gname = goal_vse_formy(counter)
        if not gid:
            return jsonify({"error": "цель «Все формы» не найдена в счётчике (или нет доступа)"})
        return jsonify({"goal_id": gid, "goal_name": gname})

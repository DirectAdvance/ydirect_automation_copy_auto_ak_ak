"""Units and deferred-create routes for Direct automation."""

from __future__ import annotations

from typing import Callable

from flask import current_app, jsonify, request


def register_deferred_routes(
    bp,
    access,
    *,
    direct_tokens: Callable,
    token_for_login: Callable,
    v5_units: Callable,
    units_per_campaign: int,
    ensure_resume_daemon: Callable,
    victory_conn: Callable,
    deferred_set_status: Callable,
    deferred_enqueue_now: Callable,
    create_jobs_lock,
    create_jobs_ahead: Callable,
) -> None:
    @bp.route("/api/units")
    @access
    def api_units():
        """Остаток баллов Директа по логину + грубая оценка «на сколько РК хватит»."""
        login = (request.args.get("login") or "").strip()
        if not login:
            return jsonify({"error": "login обязателен"}), 400
        tok, ag = token_for_login(login, (request.args.get("agency") or "").strip(), direct_tokens())
        if not tok:
            return jsonify({"error": "не найден агентский токен, открывающий этот аккаунт"}), 404
        u = v5_units(tok, login)
        if not u:
            return jsonify({"error": "не удалось прочитать остаток баллов (Units)"}), 502
        u["agency"] = ag
        u["est_campaigns"] = u["rest"] // units_per_campaign
        u["per_campaign"] = units_per_campaign
        return jsonify(u)

    @bp.route("/api/deferred")
    @access
    def api_deferred():
        """Список ожидающих авто-докруток."""
        ensure_resume_daemon(current_app._get_current_object())
        login = (request.args.get("login") or "").strip()
        out = []
        try:
            import psycopg2.extras
            conn = victory_conn()
            try:
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                if login:
                    cur.execute("SELECT id, login, agency, n_items, status, resume_count, resume_at, note "
                                "FROM public.direct_deferred_creates WHERE status='waiting' AND login=%s "
                                "ORDER BY resume_at LIMIT 50", (login,))
                else:
                    cur.execute("SELECT id, login, agency, n_items, status, resume_count, resume_at, note "
                                "FROM public.direct_deferred_creates WHERE status='waiting' "
                                "ORDER BY resume_at LIMIT 50")
                for r in cur.fetchall():
                    out.append({"id": r["id"], "login": r["login"], "agency": r["agency"],
                                "n_items": r["n_items"], "resume_count": r["resume_count"],
                                "resume_at": r["resume_at"].isoformat() if r["resume_at"] else None,
                                "note": r["note"]})
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            pass
        return jsonify({"deferred": out})

    @bp.route("/api/deferred_cancel", methods=["POST"])
    @access
    def api_deferred_cancel():
        """Отменить ожидающую авто-докрутку."""
        did = ((request.json or {}).get("id") or "").strip()
        if not did:
            return jsonify({"error": "id обязателен"}), 400
        deferred_set_status(did, "cancelled", "отменено пользователем")
        return jsonify({"ok": True})

    @bp.route("/api/deferred_resume_now", methods=["POST"])
    @access
    def api_deferred_resume_now():
        """Поставить отложенный остаток в общую очередь немедленно."""
        did = ((request.json or {}).get("id") or "").strip()
        if not did:
            return jsonify({"error": "id обязателен"}), 400
        app = current_app._get_current_object()
        res = deferred_enqueue_now(app, did)
        if not res:
            return jsonify({"error": "не удалось поставить остаток в очередь (запись не найдена/пуста)"}), 404
        jid, total, login, agency = res
        with create_jobs_lock:
            ahead = create_jobs_ahead(jid)
        return jsonify({"job_id": jid, "total": total, "login": login, "agency": agency, "ahead": ahead})

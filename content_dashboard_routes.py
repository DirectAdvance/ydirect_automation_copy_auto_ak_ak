"""Read-only дашборд-роуты редактора контента Директа.

Вынесено из ``routes_content_editor.py`` (D5 декомпозиции монолита) без изменения
поведения: URL-пути, имена эндпоинтов (Flask берёт имя из имени функции), декоратор
доступа, форма JSON-ответов и условия доступа сохранены 1:1. Хендлеры — вложенные
замыкания над зависимостями, которые передаются в :func:`register_content_dashboards`.
"""

from __future__ import annotations

from typing import Callable

from flask import jsonify, request


def register_content_dashboards(
    bp,
    access,
    *,
    victory_conn: Callable,
    _allowed_directologists: Callable,
    _admin_allowed: Callable,
    default_status: str,
    exclude_directologs: list[str],
) -> None:
    """Регистрирует read-only дашборды (директологи / аккаунты / 404) на ``bp``."""

    @bp.route("/api/content-editor/admin/directologists")
    @access
    def ce_admin_directologists():
        # Дропдаун директологов для «Сверки цен». Модель как «Обзор»:
        # full access → все директологи; обычный юзер → только свои (allowed).
        import psycopg2.extras
        allowed = _allowed_directologists()
        if allowed is not None and not allowed:
            return jsonify({"rows": []})
        where = ["direction='Авто'", "directologist IS NOT NULL", "btrim(directologist)<>''"]
        params: list = [default_status]
        if allowed is not None:
            where.append("directologist = ANY(%s)")
            params.append(allowed)
        if exclude_directologs:
            where.append("directologist <> ALL(%s)")
            params.append(exclude_directologs)
        conn = victory_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT directologist, count(*) AS accounts, "
                "count(*) FILTER (WHERE status=%s) AS active_accounts "
                "FROM public.local_gsheet_sites "
                f"WHERE {' AND '.join(where)} "
                "GROUP BY directologist ORDER BY directologist",
                params,
            )
            return jsonify({"rows": cur.fetchall()})
        finally:
            conn.close()

    # ── Аккаунты для умного поиска ─────────────────────────────────────────────
    @bp.route("/api/content-editor/accounts")
    @access
    def ce_accounts():
        import psycopg2.extras

        from .account_filters import base_account_where
        status = (request.args.get("status") or default_status).strip()
        q = (request.args.get("q") or "").strip()
        where = list(base_account_where())
        params: list = []
        if exclude_directologs:
            where.append("(directologist IS NULL OR directologist <> ALL(%s))")
            params.append(exclude_directologs)
        if status and status != "__all__":
            where.append("status=%s")
            params.append(status)
        if q:
            where.append("(domain ILIKE %s OR login_key ILIKE %s OR city ILIKE %s "
                         "OR site_type ILIKE %s OR salon ILIKE %s OR directologist ILIKE %s)")
            params += [f"%{q}%"] * 6
        allowed = _allowed_directologists()
        if allowed is not None:
            if not allowed:
                return jsonify({"rows": []})
            where.append("directologist = ANY(%s)")
            params.append(allowed)
        sql = (
            "SELECT domain, salon, city, site_type, login_key, counter_number, "
            "client_id, agency_account, status, directologist "
            "FROM public.local_gsheet_sites "
            f"WHERE {' AND '.join(where)} ORDER BY domain LIMIT 2000"
        )
        conn = victory_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql, params)
            rows = cur.fetchall()
        finally:
            conn.close()
        return jsonify({"rows": rows})

    # ── 404-ошибки (только is_admin) ──────────────────────────────────────────
    @bp.route("/api/content-editor/four04")
    @access
    def ce_four04():
        if not _admin_allowed():
            return jsonify({"error": "Forbidden"}), 403
        import psycopg2.extras
        sql = """
            SELECT
                e.site,
                e.url,
                e.page_title,
                TO_CHAR(e.visit_date, 'YYYY-MM-DD') AS visit_date,
                e.utm_campaign,
                e."№ кампании" AS campaign_no,
                e.utm_content,
                e."№ группы" AS group_no,
                s.directologist,
                s.site_type,
                s.city,
                s.salon,
                s.status
            FROM public.yandex_direct_404_errors e
            INNER JOIN (
                SELECT DISTINCT ON (lower(regexp_replace(domain, '^www\\.', '')))
                    domain,
                    directologist,
                    site_type,
                    city,
                    salon,
                    status
                FROM public.local_gsheet_sites
                ORDER BY lower(regexp_replace(domain, '^www\\.', ''))
            ) s ON lower(regexp_replace(s.domain, '^www\\.', ''))
                 = lower(regexp_replace(e.site, '^www\\.', ''))
            ORDER BY e.visit_date DESC, e.site
        """
        conn = victory_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql)
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
        return jsonify({"rows": rows})

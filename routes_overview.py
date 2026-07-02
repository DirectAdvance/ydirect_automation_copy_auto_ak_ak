"""Overview routes for Direct automation."""

from __future__ import annotations

from typing import Callable

from flask import jsonify, request


def register_overview_routes(bp, access, *, victory_conn: Callable) -> None:
    @bp.route("/api/overview")
    @access
    def api_overview():
        """Обзор по директологу из общей таблицы public.gsheet_sites."""
        import psycopg2.extras

        dirq = (request.args.get("directologist") or "").strip()
        statusq = [s.strip() for s in (request.args.get("status") or "").split(",") if s.strip()]
        conn = victory_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            if statusq:
                cur.execute("SELECT directologist, count(*) FILTER (WHERE status = ANY(%s)) AS n "
                            "FROM public.gsheet_sites WHERE directologist IS NOT NULL AND directologist <> '' "
                            "GROUP BY directologist ORDER BY n DESC, directologist", (statusq,))
            else:
                cur.execute("SELECT directologist, count(*) AS n FROM public.gsheet_sites "
                            "WHERE directologist IS NOT NULL AND directologist <> '' "
                            "GROUP BY directologist ORDER BY n DESC, directologist")
            directologists = [{"name": r["directologist"], "n": r["n"]} for r in cur.fetchall()]
            rows = []
            if dirq:
                cur.execute(
                    "SELECT g.domain, g.salon, g.city, g.site_type, g.login_key, g.crm, g.template, g.status, "
                    "       COALESCE(r.fact, 0)::double precision AS otkrut_fact "
                    "FROM public.gsheet_sites g "
                    "LEFT JOIN (SELECT account_login, sum(total_cost) AS fact "
                    "           FROM public.yandex_direct_manager_reports "
                    "           WHERE left(\"Date\", 7) = to_char(now(), 'YYYY-MM') "
                    "           GROUP BY account_login) r ON r.account_login = g.login_key "
                    "WHERE g.directologist = %s ORDER BY g.domain NULLS LAST", (dirq,))
                rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
        return jsonify({"directologist": dirq, "directologists": directologists, "rows": rows})

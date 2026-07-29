"""Read-only дашборд-роуты редактора контента Директа.

Вынесено из ``routes_content_editor.py`` (D5 декомпозиции монолита) без изменения
поведения: URL-пути, имена эндпоинтов (Flask берёт имя из имени функции), декоратор
доступа, форма JSON-ответов и условия доступа сохранены 1:1. Хендлеры — вложенные
замыкания над зависимостями, которые передаются в :func:`register_content_dashboards`.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Callable

from flask import Response, jsonify, request


def _xlsx_xesc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _xlsx_col(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _xlsx_sheet_name(name: str) -> str:
    nm = re.sub(r'[:\\/?*\[\]]', " ", str(name or "Лист1")).strip() or "Лист1"
    return nm[:31]


def _xlsx_bytes(sheet_name: str, headers: list[str], rows: list[list[str]]) -> bytes:
    def _row_xml(r_idx: int, cells: list[str]) -> str:
        out = [f'<row r="{r_idx}">']
        for c_idx, val in enumerate(cells, start=1):
            ref = f"{_xlsx_col(c_idx)}{r_idx}"
            out.append(
                f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">'
                f'{_xlsx_xesc(val)}</t></is></c>'
            )
        out.append("</row>")
        return "".join(out)

    body = [_row_xml(1, headers)]
    for i, row in enumerate(rows, start=2):
        body.append(_row_xml(i, row))
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>" + "".join(body) + "</sheetData></worksheet>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{_xlsx_xesc(_xlsx_sheet_name(sheet_name))}" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buf.getvalue()


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


def _ascii_slug(s: str) -> str:
    out = []
    for ch in str(s or "").lower():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isascii() and (ch.isalnum() or ch in "-_"):
            out.append(ch)
        else:
            out.append("_")
    slug = re.sub(r"_+", "_", "".join(out)).strip("_")
    return slug or "export"


def _f404_search_normalize(value) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    return (raw.replace("http://", "", 1).replace("https://", "", 1)
            .removeprefix("www.")
            .split("?", 1)[0]
            .split("#", 1)[0]
            .rstrip("/"))


def _f404_search_variants(value) -> list[str]:
    raw = str(value or "").strip().lower()
    normalized = _f404_search_normalize(raw)
    return list(dict.fromkeys([v for v in (raw, normalized) if v]))


def register_content_dashboards(
    bp,
    access,
    *,
    victory_conn: Callable,
    _allowed_directologists: Callable,
    _admin_allowed: Callable,
    _content_tools_allowed: Callable,
    default_status: str,
    exclude_directologs: list[str],
) -> None:
    """Регистрирует read-only дашборды (директологи / аккаунты / 404) на ``bp``."""

    def _load_four404_rows() -> list[dict]:
        import psycopg2.extras

        allowed = _allowed_directologists()
        if allowed is not None and not allowed:
            return []
        params: list = []
        scope_sql = ""
        if allowed is not None:
            scope_sql = " AND s.directologist = ANY(%s)"
            params.append(allowed)
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
            WHERE COALESCE(s.directologist, '') <> 'О-Лидер'
              AND COALESCE(btrim(e.utm_campaign), '') <> ''
            {scope_sql}
            ORDER BY e.visit_date DESC, e.site
        """.format(scope_sql=scope_sql)
        conn = victory_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def _filter_four404_rows(rows: list[dict]) -> list[dict]:
        q = request.args.get("q") or ""
        dir_name = (request.args.get("directologist") or request.args.get("dir") or "").strip()
        site = (request.args.get("site_type") or request.args.get("site") or "").strip()
        city = (request.args.get("city") or "").strip()
        salon = (request.args.get("salon") or "").strip()
        status = (request.args.get("status") or "").strip()
        queries = _f404_search_variants(q)
        filtered = []
        for row in rows:
            if dir_name and (row.get("directologist") or "") != dir_name:
                continue
            if site and (row.get("site_type") or "") != site:
                continue
            if city and (row.get("city") or "") != city:
                continue
            if salon and (row.get("salon") or "") != salon:
                continue
            if status and (row.get("status") or "") != status:
                continue
            if queries:
                hay = " ".join(
                    str(v or "").lower()
                    for v in [
                        row.get("site"),
                        row.get("url"),
                        row.get("page_title"),
                        row.get("directologist"),
                        row.get("site_type"),
                        row.get("city"),
                        row.get("salon"),
                        row.get("status"),
                        row.get("utm_campaign"),
                        row.get("utm_content"),
                        row.get("campaign_no"),
                        row.get("group_no"),
                        _f404_search_normalize(row.get("site")),
                        _f404_search_normalize(row.get("url")),
                    ]
                )
                if not any(needle in hay for needle in queries):
                    continue
            filtered.append(row)
        return filtered

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

    @bp.route("/api/content-editor/copy_accounts")
    @access
    def ce_copy_accounts():
        """Полный список аккаунтов для copy-вкладки у допущенных content-user."""
        import psycopg2.extras

        from .account_filters import base_account_where
        if not _content_tools_allowed():
            return jsonify({"error": "Forbidden"}), 403
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

    # ── 404-ошибки: full-access видит всё, обычный content-user — только своих директологов.
    @bp.route("/api/content-editor/four404")
    @bp.route("/api/content-editor/four04")
    @access
    def ce_four404():
        if not _content_tools_allowed():
            return jsonify({"error": "Forbidden"}), 403
        return jsonify({"rows": _filter_four404_rows(_load_four404_rows())})

    @bp.route("/api/content-editor/four404/export_xlsx")
    @bp.route("/api/content-editor/four04/export_xlsx")
    @access
    def ce_four404_export_xlsx():
        if not _content_tools_allowed():
            return jsonify({"error": "Forbidden"}), 403
        rows = _filter_four404_rows(_load_four404_rows())
        headers = [
            "Сайт", "Битый URL", "Заголовок страницы", "Директолог", "Тип сайта", "Город",
            "Салон", "Статус", "UTM-кампания", "№ кампании", "UTM-content", "№ группы", "Дата визита",
        ]
        data_rows = [[
            str(row.get("site") or ""),
            str(row.get("url") or ""),
            str(row.get("page_title") or ""),
            str(row.get("directologist") or ""),
            str(row.get("site_type") or ""),
            str(row.get("city") or ""),
            str(row.get("salon") or ""),
            str(row.get("status") or ""),
            str(row.get("utm_campaign") or ""),
            str(row.get("campaign_no") or ""),
            str(row.get("utm_content") or ""),
            str(row.get("group_no") or ""),
            str(row.get("visit_date") or ""),
        ] for row in rows]
        q = request.args.get("q") or ""
        sheet_name = _xlsx_sheet_name("404 ошибки")
        data = _xlsx_bytes(sheet_name, headers, data_rows)
        suffix = _ascii_slug(q) if q.strip() else "all"
        fname = f"direct_404_errors_{suffix}.xlsx"
        return Response(
            data,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

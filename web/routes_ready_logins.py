"""Вкладка «Готовые логины» — реестр аккаунтов с загруженными кампаниями.

Пополняется воркером на done create-джобы (blueprint._ready_logins_track);
логин уходит из списка при удалении черновиков нашим сервисом (delete_drafts done).
Здесь — чтение/удаление/очистка/экспорт (CSV для Excel, UTF-8 BOM + ';').
"""

from __future__ import annotations

from typing import Callable

from flask import Response, jsonify, request

_COLS = ("id", "loaded_at", "campaigns", "specialist", "city", "login",
         "domain", "site_type", "slepok", "content_source", "elapsed_seconds")

_CSV_HEADERS = ("№", "Дата загрузки", "Кампаний", "Специалист", "Город", "Логин",
                "Домен", "Тип сайта", "Слепок", "Контент", "Время создания")


def _fmt_elapsed(sec) -> str:
    try:
        s = int(sec or 0)
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    return ("%d мин %d с" % (s // 60, s % 60)) if s >= 60 else ("%d с" % s)


def register_ready_logins_routes(bp, access, *, victory_conn: Callable,
                                 victory_conn_rw: Callable, db_init: Callable) -> None:
    @bp.route("/api/ready_logins")
    @access
    def api_ready_logins():
        """Список готовых логинов (сортировка — на клиенте)."""
        import psycopg2.extras
        db_init()
        conn = victory_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""
                SELECT id, to_char(loaded_at,'DD.MM.YYYY HH24:MI') AS loaded_at,
                       campaigns, specialist, city, login, domain, site_type,
                       slepok, content_source, elapsed_seconds
                  FROM public.direct_ready_logins
                 ORDER BY loaded_at DESC, id DESC
            """)
            return jsonify({"rows": cur.fetchall()})
        finally:
            conn.close()

    @bp.route("/api/ready_logins/delete", methods=["POST"])
    @access
    def api_ready_logins_delete():
        """Удалить одну строку по id."""
        rid = (request.json or {}).get("id")
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            return jsonify({"error": "id обязателен"}), 400
        conn = victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM public.direct_ready_logins WHERE id=%s", (rid,))
            conn.commit()
            return jsonify({"ok": True, "deleted": cur.rowcount})
        finally:
            conn.close()

    @bp.route("/api/ready_logins/clear", methods=["POST"])
    @access
    def api_ready_logins_clear():
        """Полностью обнулить список готовых логинов."""
        conn = victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM public.direct_ready_logins")
            conn.commit()
            return jsonify({"ok": True, "deleted": cur.rowcount})
        finally:
            conn.close()

    @bp.route("/api/ready_logins/export")
    @access
    def api_ready_logins_export():
        """CSV для Excel: UTF-8 BOM + ';' (русская локаль Excel открывает без мастера импорта)."""
        import csv
        import io
        db_init()
        conn = victory_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT to_char(loaded_at,'DD.MM.YYYY HH24:MI'), campaigns, specialist, city,
                       login, domain, site_type, slepok, content_source, elapsed_seconds
                  FROM public.direct_ready_logins
                 ORDER BY loaded_at DESC, id DESC
            """)
            rows = cur.fetchall()
        finally:
            conn.close()
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
        w.writerow(_CSV_HEADERS)
        for i, r in enumerate(rows, 1):
            w.writerow([i, r[0], r[1], r[2] or "", r[3] or "", r[4] or "", r[5] or "",
                        r[6] or "", r[7] or "", r[8] or "", _fmt_elapsed(r[9])])
        data = "\ufeff" + buf.getvalue()
        return Response(data, mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=ready_logins.csv"})

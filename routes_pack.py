"""Slepok, M3 status, and pack preview routes."""

from __future__ import annotations

import logging
import time
from typing import Callable

from flask import jsonify, request

_log = logging.getLogger(__name__)


def register_pack_routes(
    bp,
    access,
    *,
    victory_conn: Callable,
    victory_conn_rw: Callable | None = None,
    slepok_key_map: dict,
    callout_limits: dict,
    m3_content_status: Callable,
    m3_status_response: Callable,
    pack_preview_response: Callable,
    slepok_segment_counts_response: Callable | None = None,
    cookies_status_response: Callable | None = None,
) -> None:
    @bp.route("/api/slepok_callouts")
    @access
    def api_slepok_callouts():
        """Уточнения выбранного слепка из public.direct_slepok_callouts."""
        raw = (request.args.get("slepok") or "").strip()
        slepok = slepok_key_map.get(raw.lower(), raw.lower())
        out = {"callouts": [], **callout_limits}
        if not slepok:
            return jsonify(out)
        try:
            conn = victory_conn()
        except Exception as e:  # noqa: BLE001
            return jsonify({**out, "error": str(e)[:160]})
        try:
            cur = conn.cursor()
            cur.execute("SELECT text, usage_count, accounts_count, char_len "
                        "FROM public.direct_slepok_callouts WHERE slepok=%s "
                        "ORDER BY usage_count DESC, accounts_count DESC, text", (slepok,))
            out["callouts"] = [{"text": t, "usage": u, "accounts": a, "len": l}
                               for t, u, a, l in cur.fetchall()]
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)[:160]
        finally:
            conn.close()
        return jsonify(out)

    @bp.route("/api/m3_content_status")
    @access
    def api_m3_content_status():
        """Подсказка в UI: читаем ли мы контент с M3."""
        return jsonify(m3_content_status())

    @bp.route("/api/m3-status")
    @access
    def api_m3_status():
        """Лёгкий health M3 для индикатора в сайдбаре."""
        return m3_status_response()

    @bp.route("/api/cookies-status")
    @access
    def api_cookies_status():
        """Health агентских кук главпотока — бейдж в сайдбаре под M3."""
        if cookies_status_response is None:
            return jsonify({"ok": False, "detail": "not configured"})
        return cookies_status_response()

    @bp.route("/api/pack_preview")
    @access
    def api_pack_preview():
        """Предпросмотр M3-пака для слепка и типа сайта."""
        return pack_preview_response()

    @bp.route("/api/slepok_segment_counts")
    @access
    def api_slepok_segment_counts():
        """Фактические счётчики групп по сегментам для слепка×типа сайта из живого M3-пака.
        ?slepok=<key>&site_type=<name> → {counts: {tp1: {Марки: N, Модели: M, ...}, ...}}"""
        if slepok_segment_counts_response is None:
            return jsonify({"error": "not configured"})
        return slepok_segment_counts_response()

    @bp.route("/api/sync-gsheet-sites", methods=["POST"])
    @access
    def api_sync_gsheet_sites():
        """TRUNCATE+INSERT local_gsheet_sites из FDW gsheet_sites (оба в ad_analytics_bi).

        Одна транзакция RW. Guard: если gsheet_sites пуста (FDW недоступен) — не трогаем
        local_gsheet_sites. Возвращает {ok, src, dst, took_ms} или {ok: false, error}.
        """
        if victory_conn_rw is None:
            return jsonify({"ok": False, "error": "victory_conn_rw not configured"})

        t0 = time.monotonic()
        conn = None
        try:
            conn = victory_conn_rw()
            cur = conn.cursor()

            # Динамически собрать пересечение колонок по именам (защита от позиционного сдвига)
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'gsheet_sites'
                INTERSECT
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'local_gsheet_sites'
                ORDER BY column_name
                """
            )
            cols = [row[0] for row in cur.fetchall()]
            if not cols:
                conn.rollback()
                return jsonify({"ok": False, "error": "Не удалось получить список колонок — таблицы не найдены"})

            cols_sql = ", ".join(f'"{c}"' for c in cols)

            # GUARD: FDW-0-guard — если источник недоступен или пуст, не трогаем local
            cur.execute("SELECT count(*) FROM public.gsheet_sites")
            src_count = cur.fetchone()[0]
            if src_count == 0:
                conn.rollback()
                return jsonify({
                    "ok": False,
                    "error": "gsheet_sites вернула 0 строк — FDW недоступен или источник пуст. local_gsheet_sites не тронута.",
                })

            cur.execute("TRUNCATE public.local_gsheet_sites")
            cur.execute(
                f"INSERT INTO public.local_gsheet_sites ({cols_sql}) "
                f"SELECT {cols_sql} FROM public.gsheet_sites"
            )

            cur.execute("SELECT count(*) FROM public.local_gsheet_sites")
            dst_count = cur.fetchone()[0]

            conn.commit()
            took_ms = round((time.monotonic() - t0) * 1000)
            _log.info("sync-gsheet-sites: src=%d dst=%d took=%dms cols=%d", src_count, dst_count, took_ms, len(cols))
            return jsonify({"ok": True, "src": src_count, "dst": dst_count, "took_ms": took_ms})

        except Exception as e:  # noqa: BLE001
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
            _log.exception("sync-gsheet-sites failed")
            return jsonify({"ok": False, "error": str(e)[:300]})
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

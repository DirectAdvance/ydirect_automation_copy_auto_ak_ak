"""Slepok, M3 status, and pack preview routes."""

from __future__ import annotations

from typing import Callable

from flask import jsonify, request


def register_pack_routes(
    bp,
    access,
    *,
    victory_conn: Callable,
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

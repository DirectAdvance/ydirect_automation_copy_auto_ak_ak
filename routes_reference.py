"""Reference-data routes for the Direct automation UI."""

from __future__ import annotations

from typing import Callable

from flask import jsonify, request


def register_reference_routes(
    bp,
    access,
    *,
    list_feeds_for_site: Callable,
    load_json: Callable[[str], dict],
    load_audiences: Callable[[], list[dict]],
    victory_conn: Callable,
    ag_part1_map: Callable[[], dict],
    ui_structure_payload: Callable[[], dict],
) -> None:
    @bp.route("/api/ui_structure")
    @access
    def api_ui_structure():
        """Heavy slepok/coder maps, fetched lazily by create/structure panels.

        no-cache + ETag: браузер обязан ревалидировать, но пока структура не менялась
        получает дешёвый 304 вместо мегабайтов. Без этого ответ кэшировался эвристически
        и вкладка показывала структуру многочасовой давности.
        """
        payload = ui_structure_payload()
        resp = jsonify(payload)
        sig = payload.get("sig")
        if sig:
            resp.set_etag(sig)
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
            return resp.make_conditional(request)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @bp.route("/api/feeds")
    @access
    def api_feeds():
        site = (request.args.get("site") or "").strip()
        if not site:
            return jsonify({"error": "site обязателен"}), 400
        return jsonify(list_feeds_for_site(site, load_json("feeds_catalog.json")))

    @bp.route("/api/audiences")
    @access
    def api_audiences():
        return jsonify(load_audiences())

    @bp.route("/api/ad_template_sites")
    @access
    def api_ad_template_sites():
        """Типы сайтов с числом шаблонов — для выпадающего списка в форме."""
        conn = victory_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT site_type, count(*) FROM public.direct_ad_templates "
                "WHERE enabled GROUP BY site_type ORDER BY site_type"
            )
            return jsonify({"sites": [{"site_type": r[0], "n": r[1]} for r in cur.fetchall()]})
        finally:
            conn.close()

    @bp.route("/api/ad_templates")
    @access
    def api_ad_templates():
        """Заголовки/тексты по типу сайта: {site_type, titles:[...], texts:[...]}."""
        st = (request.args.get("site_type") or "").strip()
        if not st:
            return jsonify({"error": "site_type обязателен"}), 400
        conn = victory_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT kind, content FROM public.direct_ad_templates "
                "WHERE enabled AND site_type=%s ORDER BY kind, id",
                (st,),
            )
            titles, texts = [], []
            for kind, content in cur.fetchall():
                (titles if kind == "title" else texts).append(content)
            return jsonify({"site_type": st, "titles": titles, "texts": texts})
        finally:
            conn.close()

    @bp.route("/api/cities")
    @access
    def api_cities():
        """Список городов direction='Авто' из local_gsheet_sites."""
        conn = victory_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT DISTINCT city FROM public.local_gsheet_sites "
                "WHERE direction='Авто' AND city IS NOT NULL AND city<>'' ORDER BY city"
            )
            return jsonify({"cities": [row[0] for row in cur.fetchall()]})
        finally:
            conn.close()

    @bp.route("/api/ct-list")
    @access
    def api_ct_list():
        """Список ст-кодов и имён (ag_part1) для UI выбора среза при редактировании минус-слов.
        → {ct_list: [{code, name}, ...]} отсортированный по code."""
        m = ag_part1_map()
        ct_list = sorted(
            [{"code": k, "name": v} for k, v in m.items()],
            key=lambda x: x["code"],
        )
        return jsonify({"ct_list": ct_list})

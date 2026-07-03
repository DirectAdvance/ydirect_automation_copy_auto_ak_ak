"""Settings routes for feed allow-list and global minus places."""

from __future__ import annotations

from typing import Callable

from flask import jsonify, request


def register_settings_routes(
    bp,
    access,
    *,
    global_feed_rules: Callable[[], list[dict]],
    feed_key: Callable[[str], str],
    feed_rules_ensure: Callable,
    global_minus_places: Callable[[], list[dict]],
    minus_places_ensure: Callable,
    place_host: Callable[[str], str],
    global_minus_marks: Callable[[], list[dict]],
    minus_marks_ensure: Callable,
    known_brand_canons: Callable[[], list[str]],
    victory_conn: Callable,
    victory_conn_rw: Callable,
) -> None:
    @bp.route("/api/rules")
    @access
    def api_rules_get():
        """Правила РК из public.direct_automation_rules с гео-слиянием."""
        import psycopg2.extras
        city = (request.args.get("city") or "*").strip() or "*"
        cols = ("site_type, goal_type, cpa::numeric AS cpa, budget::numeric AS budget, adjustment_pct, "
                "cpc_goal_type, cpc_cpa::numeric AS cpc_cpa, cpc_budget::numeric AS cpc_budget")

        def _rule(r, is_override):
            return {"site_type": r["site_type"], "goal_type": r["goal_type"],
                    "cpa": float(r["cpa"]), "budget": float(r["budget"]),
                    "cpc_goal_type": r.get("cpc_goal_type") or r["goal_type"],
                    "cpc_cpa": float(r["cpc_cpa"]) if r.get("cpc_cpa") is not None else float(r["cpa"]),
                    "cpc_budget": float(r["cpc_budget"]) if r.get("cpc_budget") is not None else float(r["budget"]),
                    "adjustment_pct": r["adjustment_pct"], "is_override": is_override}

        conn = victory_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            if city == "*":
                cur.execute("SELECT " + cols + " FROM public.direct_automation_rules WHERE city='*' ORDER BY site_type")
                rules = [_rule(r, False) for r in cur.fetchall()]
            else:
                cur.execute("SELECT " + cols + " FROM public.direct_automation_rules WHERE city='*' ORDER BY site_type")
                defaults = {r["site_type"]: dict(r) for r in cur.fetchall()}
                cur.execute("SELECT " + cols + " FROM public.direct_automation_rules WHERE city=%s", (city,))
                overrides = {r["site_type"]: dict(r) for r in cur.fetchall()}
                rules = [_rule(overrides[st], True) if st in overrides else _rule(d, False)
                         for st, d in sorted(defaults.items())]
        finally:
            conn.close()
        return jsonify({"city": city, "rules": rules})

    @bp.route("/api/rules", methods=["POST"])
    @access
    def api_rules_post():
        """Сохранить правила РК."""
        body = request.json or {}
        city = (body.get("city") or "*").strip() or "*"
        rules = body.get("rules") or []
        if not rules:
            return jsonify({"ok": False, "error": "rules обязательны"}), 400
        conn = victory_conn_rw()
        saved = 0
        try:
            cur = conn.cursor()
            for rule in rules:
                st = (rule.get("site_type") or "").strip()
                if not st:
                    continue
                goal_type = (rule.get("goal_type") or "Все формы").strip()
                cpc_goal_type = (rule.get("cpc_goal_type") or goal_type).strip()
                try:
                    cpa = float(rule.get("cpa") or 0)
                    budget = float(rule.get("budget") or 0)
                    adjustment_pct = int(rule.get("adjustment_pct") or -100)
                    cpc_cpa = float(rule.get("cpc_cpa") if rule.get("cpc_cpa") is not None else cpa)
                    cpc_budget = float(rule.get("cpc_budget") if rule.get("cpc_budget") is not None else budget)
                except (TypeError, ValueError):
                    continue
                cur.execute(
                    "INSERT INTO public.direct_automation_rules "
                    "(site_type, city, goal_type, cpa, budget, adjustment_pct, "
                    "cpc_goal_type, cpc_cpa, cpc_budget, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
                    "ON CONFLICT (site_type, city) DO UPDATE "
                    "SET goal_type=EXCLUDED.goal_type, cpa=EXCLUDED.cpa, "
                    "budget=EXCLUDED.budget, adjustment_pct=EXCLUDED.adjustment_pct, "
                    "cpc_goal_type=EXCLUDED.cpc_goal_type, cpc_cpa=EXCLUDED.cpc_cpa, "
                    "cpc_budget=EXCLUDED.cpc_budget, updated_at=now()",
                    (st, city, goal_type, cpa, budget, adjustment_pct, cpc_goal_type, cpc_cpa, cpc_budget)
                )
                saved += cur.rowcount
            conn.commit()
        except Exception:  # noqa: BLE001
            conn.rollback()
            raise
        finally:
            conn.close()
        return jsonify({"ok": True, "saved": saved})

    @bp.route("/api/corrections")
    @access
    def api_corrections_get():
        """Корректировки аудиторий и демографические по гео."""
        import psycopg2.extras
        city = (request.args.get("city") or "*").strip() or "*"
        conn = victory_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT name, description, pct, sort FROM public.direct_audience_corrections "
                "WHERE city='*' ORDER BY sort, name"
            )
            aud_defaults = {r["name"]: dict(r) for r in cur.fetchall()}
            aud_overrides: dict = {}
            if city != "*":
                cur.execute(
                    "SELECT name, pct FROM public.direct_audience_corrections "
                    "WHERE city=%s ORDER BY name", (city,)
                )
                aud_overrides = {r["name"]: r["pct"] for r in cur.fetchall()}
            audiences = []
            for name, d in sorted(aud_defaults.items(), key=lambda x: (x[1]["sort"] or 0, x[0])):
                if city != "*" and name in aud_overrides:
                    audiences.append({"name": name, "description": d["description"],
                                      "pct": aud_overrides[name], "is_override": True})
                else:
                    audiences.append({"name": name, "description": d["description"],
                                      "pct": d["pct"], "is_override": False})

            cur.execute(
                "SELECT kind, key, label, pct, sort FROM public.direct_demographic_corrections "
                "WHERE city='*' ORDER BY kind, sort, key"
            )
            dem_defaults = {(r["kind"], r["key"]): dict(r) for r in cur.fetchall()}
            dem_overrides: dict = {}
            if city != "*":
                cur.execute(
                    "SELECT kind, key, pct FROM public.direct_demographic_corrections "
                    "WHERE city=%s", (city,)
                )
                dem_overrides = {(r["kind"], r["key"]): r["pct"] for r in cur.fetchall()}
            demographic = []
            for (kind, key), d in sorted(dem_defaults.items(), key=lambda x: (x[1]["kind"], x[1]["sort"] or 0, x[0][1])):
                ovr_key = (kind, key)
                if city != "*" and ovr_key in dem_overrides:
                    demographic.append({"kind": kind, "key": key, "label": d["label"],
                                        "pct": dem_overrides[ovr_key], "is_override": True})
                else:
                    demographic.append({"kind": kind, "key": key, "label": d["label"],
                                        "pct": d["pct"], "is_override": False})
        finally:
            conn.close()
        return jsonify({"city": city, "audiences": audiences, "demographic": demographic})

    @bp.route("/api/corrections", methods=["POST"])
    @access
    def api_corrections_post():
        """Сохранить корректировки."""
        body = request.json or {}
        city = (body.get("city") or "*").strip() or "*"
        audiences = body.get("audiences") or []
        demographic = body.get("demographic") or []
        if not audiences and not demographic:
            return jsonify({"ok": False, "error": "audiences или demographic обязательны"}), 400
        conn = victory_conn_rw()
        saved = 0
        try:
            cur = conn.cursor()
            for aud in audiences:
                name = (aud.get("name") or "").strip()
                if not name:
                    continue
                try:
                    pct = int(aud.get("pct") if aud.get("pct") is not None else -100)
                except (TypeError, ValueError):
                    continue
                cur.execute(
                    "INSERT INTO public.direct_audience_corrections(city, name, pct, updated_at) "
                    "VALUES(%s, %s, %s, now()) "
                    "ON CONFLICT(city, name) DO UPDATE SET pct=EXCLUDED.pct, updated_at=now()",
                    (city, name, pct)
                )
                saved += cur.rowcount
            for dem in demographic:
                kind = (dem.get("kind") or "").strip()
                key = (dem.get("key") or "").strip()
                if not kind or not key:
                    continue
                try:
                    pct = int(dem.get("pct") if dem.get("pct") is not None else -100)
                except (TypeError, ValueError):
                    continue
                cur.execute(
                    "INSERT INTO public.direct_demographic_corrections(city, kind, key, pct, updated_at) "
                    "VALUES(%s, %s, %s, %s, now()) "
                    "ON CONFLICT(city, kind, key) DO UPDATE SET pct=EXCLUDED.pct, updated_at=now()",
                    (city, kind, key, pct)
                )
                saved += cur.rowcount
            conn.commit()
        except Exception:  # noqa: BLE001
            conn.rollback()
            raise
        finally:
            conn.close()
        return jsonify({"ok": True, "saved": saved})

    @bp.route("/api/feed-rules")
    @access
    def api_feed_rules_get():
        rows = global_feed_rules()
        return jsonify({"feeds": rows})

    @bp.route("/api/feed-rules", methods=["POST"])
    @access
    def api_feed_rules_post():
        body = request.json or {}
        feeds = body.get("feeds") or []
        if not isinstance(feeds, list):
            return jsonify({"ok": False, "error": "feeds должен быть массивом"}), 400
        conn = victory_conn_rw()
        saved = 0
        try:
            cur = conn.cursor()
            feed_rules_ensure(cur)
            for i, row in enumerate(feeds, 1):
                name = (row.get("name") or row.get("url") or "").strip()
                url = (row.get("url") or name).strip()
                if not name or not url:
                    continue
                key = feed_key(url or name)
                if not key:
                    continue
                cur.execute(
                    "INSERT INTO public.direct_global_feed_rules(feed_key, name, url, enabled, sort, updated_at) "
                    "VALUES(%s, %s, %s, %s, %s, now()) "
                    "ON CONFLICT(feed_key) DO UPDATE SET name=EXCLUDED.name, url=EXCLUDED.url, "
                    "enabled=EXCLUDED.enabled, sort=EXCLUDED.sort, updated_at=now()",
                    (key, name, url, bool(row.get("enabled")), i),
                )
                if "role" in row:
                    role_val = "catalog" if str(row.get("role") or "").strip().lower() == "catalog" else "landing"
                    cur.execute(
                        "UPDATE public.direct_global_feed_rules SET role=%s, updated_at=now() WHERE feed_key=%s",
                        (role_val, key),
                    )
                saved += 1
            conn.commit()
        except Exception:  # noqa: BLE001
            conn.rollback()
            raise
        finally:
            conn.close()
        return jsonify({"ok": True, "saved": saved})

    @bp.route("/api/minus-places")
    @access
    def api_minus_places_get():
        """Список минус-площадок РСЯ (#21). → {places:[{url,enabled,sort}]}."""
        return jsonify({"places": global_minus_places()})

    @bp.route("/api/minus-places", methods=["POST"])
    @access
    def api_minus_places_post():
        """Сохранить минус-площадки (replace-all из textarea)."""
        body = request.json or {}
        if "places" not in body:
            return jsonify({"ok": False, "error": "нет ключа 'places' — replace-all не выполняется"}), 400
        raw = body.get("places")
        if not isinstance(raw, list):
            return jsonify({"ok": False, "error": "places должен быть массивом"}), 400
        if not raw and not bool(body.get("confirm_clear")):
            try:
                cur_cnt = len(global_minus_places())
            except Exception:  # noqa: BLE001
                cur_cnt = 0
            if cur_cnt:
                return jsonify({
                    "ok": False,
                    "needs_confirm": True,
                    "error": f"список пуст — для полной очистки {cur_cnt} площадок передай confirm_clear=true",
                }), 409

        seen: set[str] = set()
        items: list[tuple[str, bool]] = []
        for r in raw:
            url = (r.get("url") if isinstance(r, dict) else r) or ""
            url = place_host(url)
            if not url or url in seen:
                continue
            seen.add(url)
            enabled = bool(r.get("enabled", True)) if isinstance(r, dict) else True
            items.append((url, enabled))

        conn = victory_conn_rw()
        try:
            cur = conn.cursor()
            minus_places_ensure(cur)
            cur.execute("DELETE FROM public.direct_global_minus_places")
            for i, (url, enabled) in enumerate(items, 1):
                cur.execute(
                    "INSERT INTO public.direct_global_minus_places(url, enabled, sort, updated_at) "
                    "VALUES(%s, %s, %s, now()) ON CONFLICT(url) DO UPDATE SET enabled=EXCLUDED.enabled, "
                    "sort=EXCLUDED.sort, updated_at=now()",
                    (url, enabled, i),
                )
            conn.commit()
        except Exception:  # noqa: BLE001
            conn.rollback()
            raise
        finally:
            conn.close()
        return jsonify({"ok": True, "saved": len(items)})

    @bp.route("/api/minus-marks")
    @access
    def api_minus_marks_get():
        """Минус-марки (фид): полный список марок из фидов + состояние галочек.
        По умолчанию все сняты (в БД строк нет → enabled=false). → {marks:[{mark,enabled}]}."""
        try:
            saved = {str(r.get("mark") or "").strip().lower(): bool(r.get("enabled"))
                     for r in (global_minus_marks() or [])}
        except Exception:  # noqa: BLE001
            saved = {}
        try:
            catalog = [str(m).strip() for m in (known_brand_canons() or []) if str(m).strip()]
        except Exception:  # noqa: BLE001
            catalog = []
        # Полный список = марки-каталог ∪ ранее сохранённые (на случай марки не из каталога).
        seen: set[str] = set()
        marks: list[dict] = []
        for m in catalog + list(saved.keys()):
            key = m.lower()
            if not m or key in seen:
                continue
            seen.add(key)
            marks.append({"mark": m, "enabled": saved.get(key, False)})
        marks.sort(key=lambda x: x["mark"].lower())
        return jsonify({"marks": marks})

    @bp.route("/api/minus-marks", methods=["POST"])
    @access
    def api_minus_marks_post():
        """Сохранить минус-марки (replace-all). Пишем ТОЛЬКО отмеченные (enabled=true) — снятые
        отсутствуют в таблице (дефолт = не минусовать). Body: {marks:[{mark,enabled}]} | [str,...]."""
        body = request.json or {}
        raw = body.get("marks") if isinstance(body, dict) else body
        if not isinstance(raw, list):
            return jsonify({"ok": False, "error": "marks должен быть массивом"}), 400
        seen: set[str] = set()
        items: list[str] = []
        for r in raw:
            if isinstance(r, dict):
                mark = str(r.get("mark") or "").strip()
                enabled = bool(r.get("enabled", True))
            else:
                mark = str(r or "").strip()
                enabled = True
            if not mark or not enabled or mark.lower() in seen:
                continue
            seen.add(mark.lower())
            items.append(mark)
        conn = victory_conn_rw()
        try:
            cur = conn.cursor()
            minus_marks_ensure(cur)
            cur.execute("DELETE FROM public.direct_global_minus_marks")
            for mark in items:
                cur.execute(
                    "INSERT INTO public.direct_global_minus_marks(mark, enabled, updated_at) "
                    "VALUES(%s, true, now()) ON CONFLICT(mark) DO UPDATE SET enabled=true, "
                    "updated_at=now()",
                    (mark,),
                )
            conn.commit()
        except Exception:  # noqa: BLE001
            conn.rollback()
            raise
        finally:
            conn.close()
        return jsonify({"ok": True, "saved": len(items)})

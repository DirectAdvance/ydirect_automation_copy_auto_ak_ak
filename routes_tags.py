"""Роуты «редактируемые теги кампаний» (Вариант C — реестр в БД).

Читают/пишут СИНХРОННО в БД seoadvanced (LXC 101), схема direct_automation:
  - tag_registry        — реестр тегов (label уникален, цвет),
  - campaign_tags       — привязка тега к кампании (slepok,site_type,tp,camp_key),
  - campaign_hidden_auto — скрытые авто-теги кампании.

Теги мелкие — пишем прямо в БД (не через очередь воркера). Auth — тот же
@access, что у /api/slepki/*; правки (POST/PUT/DELETE) — только админ (session.is_admin).
"""
from __future__ import annotations

import re
from typing import Callable

from flask import jsonify, request, session

import psycopg2
import psycopg2.extras


_TAG_REGISTRY = "direct_automation.tag_registry"
_CAMPAIGN_TAGS = "direct_automation.campaign_tags"
_HIDDEN_AUTO = "direct_automation.campaign_hidden_auto"

_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_DEFAULT_COLOR = "#8b7cff"


def _tags_db():
    from telegram_parsing.db import get_db
    return get_db()


def _tags_exec(query: str, params: tuple = (), fetch: str | None = None):
    """Синхронный SQL к seoadvanced (direct_automation). fetch: 'one'|'all'|None."""
    conn = _tags_db()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query, params)
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                return None
    finally:
        conn.close()


def ensure_tags_tables() -> None:
    """Идемпотентно создать таблицы реестра/привязок тегов (схема direct_automation)."""
    ddl = [
        f"""CREATE TABLE IF NOT EXISTS {_TAG_REGISTRY}(
              id SERIAL PRIMARY KEY, label TEXT NOT NULL UNIQUE,
              color TEXT NOT NULL DEFAULT '{_DEFAULT_COLOR}',
              created_at TIMESTAMPTZ DEFAULT now())""",
        f"""CREATE TABLE IF NOT EXISTS {_CAMPAIGN_TAGS}(
              slepok TEXT NOT NULL, site_type TEXT NOT NULL, tp TEXT NOT NULL,
              camp_key TEXT NOT NULL,
              tag_id INT NOT NULL REFERENCES {_TAG_REGISTRY}(id) ON DELETE CASCADE,
              PRIMARY KEY(slepok,site_type,tp,camp_key,tag_id))""",
        f"""CREATE TABLE IF NOT EXISTS {_HIDDEN_AUTO}(
              slepok TEXT NOT NULL, site_type TEXT NOT NULL, tp TEXT NOT NULL,
              camp_key TEXT NOT NULL, auto_label TEXT NOT NULL,
              PRIMARY KEY(slepok,site_type,tp,camp_key,auto_label))""",
    ]
    for stmt in ddl:
        _tags_exec(stmt)


def register_tags_routes(bp, access: Callable) -> None:
    def _admin() -> bool:
        return bool(session.get("is_admin"))

    def _need_admin():
        return jsonify({"error": "только администратор"}), 403

    def _valid_color(c: str) -> bool:
        return bool(_COLOR_RE.match((c or "").strip()))

    def _camp_scope(b: dict):
        """Достать (slepok,site_type,tp,camp_key) из тела; None,err при пустом поле."""
        slepok = (b.get("slepok") or "").strip()
        site_type = (b.get("site_type") or "").strip()
        tp = (b.get("tp") or "").strip()
        camp_key = (b.get("camp_key") or "").strip()
        if not (slepok and site_type and tp and camp_key):
            return None, (jsonify({"error": "нужны slepok/site_type/tp/camp_key"}), 400)
        return (slepok, site_type, tp, camp_key), None

    # ── реестр тегов ──────────────────────────────────────────────────────────
    @bp.route("/api/tags/registry", methods=["GET"])
    @access
    def tags_registry_list():
        rows = _tags_exec(
            f"SELECT id, label, color FROM {_TAG_REGISTRY} ORDER BY label",
            fetch="all",
        ) or []
        return jsonify({"tags": [dict(r) for r in rows]})

    @bp.route("/api/tags/registry", methods=["POST"])
    @access
    def tags_registry_create():
        if not _admin():
            return _need_admin()
        b = request.get_json(silent=True) or {}
        label = (b.get("label") or "").strip()
        color = (b.get("color") or "").strip() or _DEFAULT_COLOR
        if not label:
            return jsonify({"error": "label обязателен"}), 400
        if not _valid_color(color):
            return jsonify({"error": "color должен быть #hex"}), 400
        try:
            row = _tags_exec(
                f"INSERT INTO {_TAG_REGISTRY}(label, color) VALUES (%s, %s) "
                f"RETURNING id, label, color",
                (label, color), fetch="one",
            )
        except psycopg2.errors.UniqueViolation:
            return jsonify({"error": f"тег «{label}» уже существует"}), 409
        return jsonify(dict(row))

    @bp.route("/api/tags/registry/<int:tag_id>", methods=["PUT"])
    @access
    def tags_registry_update(tag_id: int):
        if not _admin():
            return _need_admin()
        b = request.get_json(silent=True) or {}
        sets, params = [], []
        if "label" in b:
            label = (b.get("label") or "").strip()
            if not label:
                return jsonify({"error": "label не может быть пустым"}), 400
            sets.append("label=%s")
            params.append(label)
        if "color" in b:
            color = (b.get("color") or "").strip()
            if not _valid_color(color):
                return jsonify({"error": "color должен быть #hex"}), 400
            sets.append("color=%s")
            params.append(color)
        if not sets:
            return jsonify({"error": "нечего обновлять (нет label/color)"}), 400
        params.append(tag_id)
        try:
            row = _tags_exec(
                f"UPDATE {_TAG_REGISTRY} SET {', '.join(sets)} WHERE id=%s "
                f"RETURNING id",
                tuple(params), fetch="one",
            )
        except psycopg2.errors.UniqueViolation:
            return jsonify({"error": "тег с таким label уже существует"}), 409
        if not row:
            return jsonify({"error": "тег не найден"}), 404
        return jsonify({"ok": True})

    @bp.route("/api/tags/registry/<int:tag_id>", methods=["DELETE"])
    @access
    def tags_registry_delete(tag_id: int):
        if not _admin():
            return _need_admin()
        _tags_exec(f"DELETE FROM {_TAG_REGISTRY} WHERE id=%s", (tag_id,))
        return jsonify({"ok": True})

    # ── привязка тегов к кампании ─────────────────────────────────────────────
    @bp.route("/api/tags/campaign", methods=["GET"])
    @access
    def tags_campaign_get():
        scope, err = _camp_scope(request.args)
        if err:
            return err
        slepok, site_type, tp, camp_key = scope
        assigned = _tags_exec(
            f"SELECT r.id, r.label, r.color FROM {_CAMPAIGN_TAGS} ct "
            f"JOIN {_TAG_REGISTRY} r ON r.id=ct.tag_id "
            f"WHERE ct.slepok=%s AND ct.site_type=%s AND ct.tp=%s AND ct.camp_key=%s "
            f"ORDER BY r.label",
            (slepok, site_type, tp, camp_key), fetch="all",
        ) or []
        hidden = _tags_exec(
            f"SELECT auto_label FROM {_HIDDEN_AUTO} "
            f"WHERE slepok=%s AND site_type=%s AND tp=%s AND camp_key=%s "
            f"ORDER BY auto_label",
            (slepok, site_type, tp, camp_key), fetch="all",
        ) or []
        return jsonify({
            "assigned": [dict(r) for r in assigned],
            "hidden_auto": [r["auto_label"] for r in hidden],
        })

    @bp.route("/api/tags/campaign/assign", methods=["POST"])
    @access
    def tags_campaign_assign():
        if not _admin():
            return _need_admin()
        b = request.get_json(silent=True) or {}
        scope, err = _camp_scope(b)
        if err:
            return err
        slepok, site_type, tp, camp_key = scope
        try:
            tag_id = int(b.get("tag_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "нужен числовой tag_id"}), 400
        try:
            _tags_exec(
                f"INSERT INTO {_CAMPAIGN_TAGS}(slepok,site_type,tp,camp_key,tag_id) "
                f"VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (slepok, site_type, tp, camp_key, tag_id),
            )
        except psycopg2.errors.ForeignKeyViolation:
            return jsonify({"error": f"тег id={tag_id} не найден в реестре"}), 404
        return jsonify({"ok": True})

    @bp.route("/api/tags/campaign/unassign", methods=["POST"])
    @access
    def tags_campaign_unassign():
        if not _admin():
            return _need_admin()
        b = request.get_json(silent=True) or {}
        scope, err = _camp_scope(b)
        if err:
            return err
        slepok, site_type, tp, camp_key = scope
        try:
            tag_id = int(b.get("tag_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "нужен числовой tag_id"}), 400
        _tags_exec(
            f"DELETE FROM {_CAMPAIGN_TAGS} "
            f"WHERE slepok=%s AND site_type=%s AND tp=%s AND camp_key=%s AND tag_id=%s",
            (slepok, site_type, tp, camp_key, tag_id),
        )
        return jsonify({"ok": True})

    @bp.route("/api/tags/campaign/hide_auto", methods=["POST"])
    @access
    def tags_campaign_hide_auto():
        if not _admin():
            return _need_admin()
        b = request.get_json(silent=True) or {}
        scope, err = _camp_scope(b)
        if err:
            return err
        slepok, site_type, tp, camp_key = scope
        auto_label = (b.get("auto_label") or "").strip()
        if not auto_label:
            return jsonify({"error": "auto_label обязателен"}), 400
        hidden = bool(b.get("hidden"))
        if hidden:
            _tags_exec(
                f"INSERT INTO {_HIDDEN_AUTO}(slepok,site_type,tp,camp_key,auto_label) "
                f"VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (slepok, site_type, tp, camp_key, auto_label),
            )
        else:
            _tags_exec(
                f"DELETE FROM {_HIDDEN_AUTO} "
                f"WHERE slepok=%s AND site_type=%s AND tp=%s AND camp_key=%s AND auto_label=%s",
                (slepok, site_type, tp, camp_key, auto_label),
            )
        return jsonify({"ok": True})

"""Роуты редактора структуры/ключей слепков (вкладка «Структура слепков» → редактируемая).

Все правки — АДМИН-ONLY (архитектура F) и идут в ОБЩУЮ очередь создания РК как edit-джобы
(_kind ∈ slepki_editor._EDIT_KINDS) через job_new → воркер применяет СЕРИЙНО (slepki_editor.handle_job).
UI не применяет правку сразу: enqueue + показ статуса в очереди (та же карточка, что у create-джоб).

Read-only просмотр ключей (GET /keywords) — синхронно в web-процессе (чтение пака, без записи).
"""
from __future__ import annotations

from typing import Callable

from flask import jsonify, request, session


# Компонент-энумы кодера для мастера add-ct (СЫРОЙ gc не вводится — только сборка из этих значений).
# Значения — реальный корпус из slepki_structure.json (aon/aoff, форматы, возраст, пол, частые регионы).
_MODE_OPTS = [{"code": "aon", "name": "Автотаргет (aon)"}, {"code": "aoff", "name": "КС (aoff)"}]
_FMT_OPTS = [
    {"code": "ct001", "name": "ТГО (только TextAd)"},
    {"code": "ct010", "name": "Комбинированный (TextAd+Listing+Shopping)"},
    {"code": "ct009", "name": "Товарное (Shopping+Listing без TextAd)"},
    {"code": "ct003", "name": "Товарное/Фид"},
]
_AGE_OPTS = [{"code": "ag011", "name": "24-55+"}, {"code": "ag001", "name": "Все"}]
_GENDER_OPTS = [{"code": "g00", "name": "Все"}, {"code": "g01", "name": "Мужчины"}, {"code": "g02", "name": "Женщины"}]
_INTEREST_OPTS = [{"code": "n000", "name": "Без интереса"}]
# Регион в структуре — плейсхолдер r0000 (на боевом → реальный по городу аккаунта).
_REGION_OPTS = [
    {"code": "r0000", "name": "Плейсхолдер (по городу на боевом)"},
    {"code": "r0100", "name": "Кемеровская обл."},
    {"code": "r0002", "name": "регион 0002"},
]


def register_slepki_edit_routes(
    bp,
    access,
    *,
    slepki_editor,
    job_new: Callable,
    ag_part1_map: Callable[[], dict],
) -> None:
    def _admin() -> bool:
        return bool(session.get("is_admin"))

    def _enqueue(kind: str, spec: dict):
        """Поставить edit-джобу в ОБЩУЮ очередь (web-роль: уходит в БД, воркер применит серийно)."""
        body = {"_kind": kind, "spec": spec,
                "_actor": session.get("username") or session.get("login") or "admin"}
        jid = job_new(1, "slepki-edit", body, dict(session), False, True)  # priority=True (правка быстрее наборов)
        return jsonify({"queued": True, "job_id": jid, "kind": kind})

    # ── просмотр ключей группы (read-only, синхронно) ─────────────────────────
    @bp.route("/api/slepki/keywords", methods=["GET"])
    @access
    def slepki_keywords():
        if not _admin():
            return jsonify({"error": "только администратор"}), 403
        slepok = (request.args.get("slepok") or "").strip()
        site_type = (request.args.get("site_type") or "").strip()
        tp = (request.args.get("tp") or "").strip()
        ct = (request.args.get("ct") or "").strip()
        if not (slepok and site_type and tp and ct):
            return jsonify({"error": "нужны slepok/site_type/tp/ct"}), 400
        data = slepki_editor.read_group_keywords(site_type, tp, ct, slepok)
        return jsonify({"slepok": slepok, "site_type": site_type, "tp": tp, "ct": ct, **data})

    # ── компоненты кодера для мастера add-ct ──────────────────────────────────
    @bp.route("/api/slepki/coder_components", methods=["GET"])
    @access
    def slepki_coder_components():
        if not _admin():
            return jsonify({"error": "только администратор"}), 403
        m = ag_part1_map()
        ct_list = sorted(({"code": k, "name": v} for k, v in m.items()), key=lambda x: x["code"])
        return jsonify({
            "ct_list": ct_list,          # ТОЛЬКО зарегистрированные ct кодера (ag_part1)
            "mode": _MODE_OPTS, "fmt": _FMT_OPTS, "age": _AGE_OPTS,
            "gender": _GENDER_OPTS, "interest": _INTEREST_OPTS, "region": _REGION_OPTS,
        })

    # ── правки → очередь (admin-only) ─────────────────────────────────────────
    @bp.route("/api/slepki/edit_keywords", methods=["POST"])
    @access
    def slepki_edit_keywords():
        if not _admin():
            return jsonify({"error": "только администратор"}), 403
        b = request.get_json(silent=True) or {}
        spec = {
            "slepok": b.get("slepok"), "site_type": b.get("site_type"),
            "tp": b.get("tp"), "ct": b.get("ct"),
            "positive": b.get("positive") or [], "minus": b.get("minus") or [],
        }
        # Библиотечные минусы — редактируются, только если ключ прислан (иначе файл не трогаем).
        # SCOPE строго этот (slepok, site_type, tp, ct); имя файла с префиксом {slepok}_ →
        # чужие слепки физически недостижимы (см. slepki_editor.apply_edit_keywords).
        if "minus_shared" in b:
            spec["minus_shared"] = b.get("minus_shared") or []
        return _enqueue("edit_keywords", spec)

    @bp.route("/api/slepki/edit_callouts", methods=["POST"])
    @access
    def slepki_edit_callouts():
        if not _admin():
            return jsonify({"error": "только администратор"}), 403
        b = request.get_json(silent=True) or {}
        # Уточнения (callouts) кампании — durable dual-write (DST + M3) через edit-джобу.
        # ct = репрезентативный ct кампании (UI). Файл callouts/{slepok}.txt.
        return _enqueue("edit_callouts", {
            "slepok": b.get("slepok"), "site_type": b.get("site_type"),
            "tp": b.get("tp"), "ct": b.get("ct"),
            "callouts": b.get("callouts") or [],
        })

    @bp.route("/api/slepki/toggle_aon_aoff", methods=["POST"])
    @access
    def slepki_toggle_aon_aoff():
        if not _admin():
            return jsonify({"error": "только администратор"}), 403
        b = request.get_json(silent=True) or {}
        return _enqueue("toggle_aon_aoff", {
            "slepok": b.get("slepok"), "site_type": b.get("site_type"),
            "tp": b.get("tp"), "segment": b.get("segment"), "mode": b.get("mode"),
        })

    @bp.route("/api/slepki/add_ct_group", methods=["POST"])
    @access
    def slepki_add_ct_group():
        if not _admin():
            return jsonify({"error": "только администратор"}), 403
        b = request.get_json(silent=True) or {}
        return _enqueue("add_ct_group", {
            "slepok": b.get("slepok"), "site_type": b.get("site_type"), "tp": b.get("tp"),
            "ct": b.get("ct"), "mode": b.get("mode") or "aon",
            "region": b.get("region") or "r0000", "fmt": b.get("fmt") or "ct001",
            "age": b.get("age") or "ag011", "gender": b.get("gender") or "g00",
            "interest": b.get("interest") or "n000", "desc": b.get("desc") or "",
        })

    @bp.route("/api/slepki/remove_ct_group", methods=["POST"])
    @access
    def slepki_remove_ct_group():
        if not _admin():
            return jsonify({"error": "только администратор"}), 403
        b = request.get_json(silent=True) or {}
        return _enqueue("remove_ct_group", {
            "slepok": b.get("slepok"), "site_type": b.get("site_type"),
            "tp": b.get("tp"), "gc": b.get("gc"),
        })

    @bp.route("/api/slepki/set_name_override", methods=["POST"])
    @access
    def slepki_set_name_override():
        if not _admin():
            return jsonify({"error": "только администратор"}), 403
        b = request.get_json(silent=True) or {}
        return _enqueue("set_name_override", {
            "slepok": b.get("slepok"), "site_type": b.get("site_type"),
            "tp": b.get("tp"), "segment": b.get("segment"), "mode": b.get("mode") or "",
            "name_override": b.get("name_override") or "",
        })

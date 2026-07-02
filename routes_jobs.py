"""Create-set queue and job status routes for Direct automation."""

from __future__ import annotations

from typing import Callable

from flask import current_app, jsonify, request, session


def register_job_routes(
    bp,
    access,
    danger_access,
    *,
    parse_number: Callable,
    metrika_goals_for: Callable,
    counter_foreign_owner: Callable,
    resolve_agency_hint: Callable,
    ensure_create_worker: Callable,
    job_new: Callable,
    create_jobs_ahead: Callable,
    create_watchdog_tick: Callable,
    jobs_purge_old: Callable,
    job_agency: Callable,
    job_db_save: Callable,
    job_db_delete: Callable,
    delete_drafts_core: Callable,
    create_jobs: dict,
    create_jobs_lock,
    create_queue: list,
    job_terminal: tuple,
    job_db_last: dict,
) -> None:
    @bp.route("/api/create_set_async", methods=["POST"])
    @access
    def api_create_set_async():
        """Старт create_set в ФОНЕ — большой набор не упирается в nginx proxy_read_timeout."""
        body = dict(request.json or {})
        items = body.get("items") or []
        login = (body.get("login") or "").strip()
        if not items:
            return jsonify({"error": "items обязательны"}), 400

        _cid_pf = parse_number(body.get("counter_id"), 0)
        if not _cid_pf:
            _mg_pf = metrika_goals_for(login)
            if _mg_pf and _mg_pf["counters"]:
                _cid_pf = _mg_pf["counters"][0]
        _owner_pf = counter_foreign_owner(_cid_pf, login) if _cid_pf else None
        if _owner_pf:
            return jsonify({"error": f"счётчик Метрики {_cid_pf} принадлежит аккаунту «{_owner_pf}», "
                                     f"а не «{login}» — Яндекс отклонит цель как «не найдена». "
                                     f"Укажите счётчик и цель самого «{login}»."}), 400

        resolved_ag = resolve_agency_hint(login, (body.get("agency") or "").strip())
        if resolved_ag:
            body["agency"] = resolved_ag
        app = current_app._get_current_object()
        ensure_create_worker(app)
        with create_jobs_lock:
            for _jid, _j in create_jobs.items():
                if _j.get("status") not in job_terminal and (_j.get("login") or "").strip() == login:
                    return jsonify({
                        "job_id": _jid, "total": int(_j.get("total") or len(items)),
                        "login": login, "ahead": create_jobs_ahead(_jid),
                        "existing": True,
                        "note": "для аккаунта уже есть активная задача; дубль не создан",
                    })
        saved_session = dict(session)
        with create_jobs_lock:
            existing_ids = set(create_jobs.keys())
        job_id = job_new(len(items), login, body, saved_session, dedup_login=True)
        body["_job_id"] = job_id
        deduped = job_id in existing_ids
        with create_jobs_lock:
            ahead = create_jobs_ahead(job_id)
        resp = {"job_id": job_id, "total": len(items), "login": login, "ahead": ahead}
        if deduped:
            resp["existing"] = True
            resp["note"] = "для аккаунта уже есть активная задача; дубль не создан"
        return jsonify(resp)

    @bp.route("/api/create_set_status", methods=["GET"])
    @access
    def api_create_set_status():
        """Прогресс/результат async-джобы create_set."""
        jid = (request.args.get("job_id") or "").strip()
        ensure_create_worker(current_app._get_current_object())
        create_watchdog_tick()
        with create_jobs_lock:
            j = create_jobs.get(jid)
            if not j:
                return jsonify({"error": "job не найдена (возможно, устарела)"}), 404
            ahead = create_jobs_ahead(jid) if j["status"] == "queued" else 0
            return jsonify({"status": j["status"], "login": j.get("login", ""),
                            "agency": job_agency(j), "done": j["done"], "total": j["total"],
                            "created": j["created"], "failed": j["failed"],
                            "set_done": j.get("set_done", 0), "set_total": j.get("set_total", j["total"]),
                            "ahead": ahead, "error": j["error"], "elapsed": j.get("elapsed"),
                            "step": j.get("step"),
                            "stream_content": bool(j.get("stream_content")),
                            "result": j["result"] if j["status"] in job_terminal else None})

    @bp.route("/api/create_jobs", methods=["GET"])
    @access
    def api_create_jobs():
        """Живая очередь создания РК — серверный источник правды."""
        active_only = request.args.get("active") in ("1", "true")
        ensure_create_worker(current_app._get_current_object())
        create_watchdog_tick()
        jobs_purge_old()
        out = []
        with create_jobs_lock:
            for jid, j in create_jobs.items():
                if active_only and j["status"] in job_terminal:
                    continue
                ahead = create_jobs_ahead(jid) if j["status"] == "queued" else 0
                out.append({"job_id": jid, "status": j["status"], "login": j.get("login", ""),
                            "agency": job_agency(j),
                            "done": j.get("done", 0), "total": j.get("total", 0),
                            "created": j.get("created", 0), "failed": j.get("failed", 0),
                            "set_done": j.get("set_done", 0), "set_total": j.get("set_total", j.get("total", 0)),
                            "kind": j.get("kind", "set"), "publish": bool(j.get("publish")),
                            "ahead": ahead, "error": j.get("error"), "elapsed": j.get("elapsed"),
                            "step": j.get("step"),
                            "stream_content": bool(j.get("stream_content")),
                            "result": j.get("result") if j["status"] in job_terminal else None})
        order = {"running": 0, "queued": 1}
        out.sort(key=lambda x: order.get(x["status"], 2))
        return jsonify({"jobs": out})

    @bp.route("/api/create_set_cancel", methods=["POST"])
    @access
    def api_create_set_cancel():
        """Отмена джобы создания или удаление завершённой карточки из очереди."""
        jid = ((request.json or {}).get("job_id") or "").strip()
        with create_jobs_lock:
            j = create_jobs.get(jid)
            if not j:
                return jsonify({"error": "job не найдена"}), 404
            if j["status"] in job_terminal:
                create_jobs.pop(jid, None)
                job_db_last.pop(jid, None)
                job_db_delete(jid)
                return jsonify({"ok": True, "status": j["status"], "removed": True, "note": "убрана из очереди"})
            j["cancel"] = True
            if j["status"] == "queued" and jid in create_queue:
                create_queue.remove(jid)
                j["status"] = "cancelled"
            snap = dict(j)
        job_db_save(jid, snap, full=True)
        return jsonify({"ok": True, "status": snap["status"]})

    @bp.route("/api/jobs/<job_id>/resume", methods=["POST"])
    @access
    def api_job_resume(job_id: str):
        """Возобновление джобы, прерванной рестартом сервиса."""
        jid = job_id.strip()
        with create_jobs_lock:
            j = create_jobs.get(jid)
            if not j:
                return jsonify({"error": "job не найдена (возможно, уже убрана)"}), 404
            if j.get("status") != "interrupted":
                return jsonify({"error": f"job не прервана (статус: {j.get('status')})"}), 400
            body = j.get("body")
            login = j.get("login") or ""
            done_idx = int(j.get("done") or 0)
        if not body:
            return jsonify({"error": "body джобы не сохранён — невозможно возобновить автоматически. "
                                     "Запустите создание вручную через форму."}), 422
        if not body.get("items"):
            return jsonify({"error": "items в body пусты — нечего создавать"}), 422
        app = current_app._get_current_object()
        ensure_create_worker(app)
        saved_session = dict(session)
        new_body = dict(body)
        new_body.pop("_job_id", None)
        new_body.pop("_resume_count", None)
        items_list = body.get("items") or []
        if body.get("single_feed") and items_list:
            from .create_set_input import first_feed_items
            items_list = first_feed_items(items_list, parse_number=parse_number)
        if 0 <= done_idx < len(items_list):
            partial_name = (items_list[done_idx].get("name") or "").strip()
            if partial_name:
                force = [str(n).strip() for n in (new_body.get("_repair_force_names") or [])
                         if str(n or "").strip()]
                if partial_name not in force:
                    force.append(partial_name)
                new_body["_repair_force_names"] = force
        new_job_id = job_new(len(new_body["items"]), login, new_body, saved_session)
        with create_jobs_lock:
            ahead = create_jobs_ahead(new_job_id)
        return jsonify({"ok": True, "new_job_id": new_job_id,
                        "total": len(new_body["items"]), "login": login, "ahead": ahead})

    @bp.route("/api/jobs/<job_id>/delete_created", methods=["POST"])
    @danger_access
    def api_job_delete_created(job_id: str):
        """Удалить черновики аккаунта из прерванной/завершённой джобы."""
        jid = job_id.strip()
        with create_jobs_lock:
            j = create_jobs.get(jid)
            if not j:
                return jsonify({"error": "job не найдена (возможно, уже убрана)"}), 404
            login = j.get("login") or ""
            agency = j.get("agency") or ""
            status = j.get("status") or ""
        if not login:
            return jsonify({"error": "login не сохранён в джобе"}), 422
        if status not in ("interrupted", "error", "done", "cancelled"):
            return jsonify({"error": f"Удаление доступно только для завершённых/прерванных джоб (статус: {status})"}), 400
        try:
            result = delete_drafts_core(login, agency)
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(e)[:300]}), 500
        return jsonify({"ok": result.get("ok", True),
                        "deleted": result.get("deleted", 0),
                        "by_v5": result.get("by_v5", 0),
                        "by_uac": result.get("by_uac", 0),
                        "by_cookie": result.get("by_cookie", 0),
                        "errors": result.get("errors") or []})

"""Create-set verification and repair routes."""

from __future__ import annotations

from typing import Callable

from flask import current_app, jsonify, request, session


def register_create_set_routes(
    bp,
    access,
    *,
    create_set_response: Callable,
    legacy_create_response: Callable,
    repair_gate,
    repair_auto,
    create_set_job_context: Callable,
    create_set_live_verification: Callable,
    repair_deps: Callable,
    queue_recreate_repair_job: Callable,
    attach_post_repair_verification: Callable,
    ensure_create_worker: Callable,
    create_jobs: dict,
    create_jobs_lock,
    job_terminal: tuple,
) -> None:
    @bp.route("/api/create_set", methods=["POST"])
    @access
    def api_create_set():
        """Создание набора кампаний."""
        return create_set_response()

    @bp.route("/api/create", methods=["POST"])
    @access
    def api_create():
        """Legacy single campaign create endpoint."""
        return legacy_create_response()

    @bp.route("/api/create_set_verification", methods=["GET"])
    @access
    def api_create_set_verification():
        """Read-only verification report for a finished create_set job."""
        jid = (request.args.get("job_id") or "").strip()
        live = request.args.get("live") in ("1", "true", "yes")
        use_v5 = request.args.get("v5") in ("1", "true", "yes")
        job, result, ctx, err = create_set_job_context(jid)
        if err:
            return jsonify(err[0]), err[1]
        out = {
            "job_id": jid,
            "login": ctx.get("login") or "",
            "status": job.get("status"),
            "stored": result.get("verification"),
        }
        if not live:
            return jsonify(out)

        login = (ctx.get("login") or "").strip()
        if not login:
            return jsonify({**out, "live": {"status": "error", "error": "login не сохранён в job"}}), 422
        live_report = create_set_live_verification(login, ctx.get("results") or [],
                                                   agency=ctx.get("agency") or "", use_v5=use_v5)
        return jsonify({**out, "live": live_report})

    @bp.route("/api/create_set_repair", methods=["POST"])
    @access
    def api_create_set_repair():
        """Build or execute a scoped repair plan for a completed create_set job."""
        body = request.get_json(silent=True) or {}
        jid = (body.get("job_id") or request.args.get("job_id") or "").strip()
        execute = repair_gate.truthy(body.get("execute")) or repair_gate.truthy(request.args.get("execute"))
        use_v5 = repair_gate.truthy(body.get("v5")) or repair_gate.truthy(request.args.get("v5"))
        job, result, ctx, err = create_set_job_context(jid)
        if err:
            return jsonify(err[0]), err[1]
        login = (ctx.get("login") or "").strip()
        if not login:
            return jsonify({"error": "login не сохранён в job", "job_id": jid}), 422
        live_report = create_set_live_verification(login, ctx.get("results") or [],
                                                   agency=ctx.get("agency") or "", use_v5=use_v5)
        plan = (live_report or {}).get("repair_plan")
        if not plan:
            try:
                from .repair.repair_planner import build_repair_plan
                plan = build_repair_plan(live_report)
            except Exception:  # noqa: BLE001
                plan = {"status": "error"}
        if execute:
            deps = repair_deps()
            with create_jobs_lock:
                recreate_preflight = repair_auto.recreate_queue_preflight(
                    login,
                    ctx.get("body") or {},
                    plan or {},
                    create_jobs,
                    job_terminal,
                )
            items = recreate_preflight.get("items") or []
            unsupported = recreate_preflight.get("unsupported_actions") or []
            if items:
                if recreate_preflight.get("conflict"):
                    return jsonify({
                        **recreate_preflight["conflict"],
                        "job_id": jid,
                    }), 409
                app = current_app._get_current_object()
                ensure_create_worker(app)
                saved_session = dict(session)
                queued = queue_recreate_repair_job(
                    login,
                    ctx,
                    plan or {},
                    parent_job_id=jid,
                    saved_session=saved_session,
                    dedup_login=True,
                )
                if not queued.get("queued"):
                    out, status = repair_auto.recreate_queue_failure_response(jid, login, queued, plan)
                    return jsonify(out), status
                return jsonify(repair_auto.recreate_queue_success_response(
                    jid,
                    login,
                    queued,
                    plan,
                    unsupported,
                ))

            in_place = repair_auto.execute_next_in_place(
                login,
                ctx,
                plan or {},
                deps,
                job_id=jid,
                post_verify=attach_post_repair_verification,
            )
            if in_place:
                out, status = in_place
                return jsonify(out), status

            if not items:
                return jsonify(repair_auto.no_safe_action_response(jid, plan, unsupported)), 422
        return jsonify(repair_gate.build_repair_gate_payload(
            job_id=jid, job=job, result=result, live_report=live_report, plan=plan,
        ))

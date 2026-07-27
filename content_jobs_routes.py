"""Content-editor job queue routes: /jobs /status /cancel.

Extracted from ``routes_content_editor.py`` (structural split). No Flask routes
are defined at module level — all routes are registered via ``register_jobs_routes``
which receives closure functions from ``routes_content_editor.register_content_editor_routes``.
"""
from __future__ import annotations

from flask import jsonify, request, session

from .content_editor_helpers import _content_job_public, CE_JOBS_TABLE, _jobs_exec


def register_jobs_routes(
    bp,
    access,
    *,
    _content_full_access,
    _queued_ahead_map,
    _job_owned,
) -> None:
    """Register /api/content-editor/jobs, /status, /cancel endpoints."""

    @bp.route("/api/content-editor/jobs")
    @access
    def ce_jobs():
        # Показываем ВСЁ, что лежит в таблице: задание пропадает только после кнопки
        # «Очистить очередь» (завершённые старше 3 суток), а не само по времени.
        #
        # dismissed=true ставится, когда юзер закрывает карточку завершённого задания
        # на рабочей вкладке («Тексты»/«Заголовки»). Это скрывает только КАРТОЧКУ —
        # вкладка «Очередь» (?include_dismissed=1) обязана показывать такие задания,
        # иначе закрыл карточку → задание пропало из очереди совсем.
        include_dismissed = request.args.get("include_dismissed") == "1"
        where = "TRUE" if include_dismissed else "NOT dismissed"
        params: tuple = ()
        if not _content_full_access():
            where += " AND username=%s"
            params = ((session.get("username") or "").strip(),)
        rows = _jobs_exec(
            f"SELECT * FROM {CE_JOBS_TABLE} WHERE {where} ORDER BY created_at DESC LIMIT 500",
            params, "all") or []
        ahead = _queued_ahead_map()
        jobs = []
        for r in rows:
            j = _content_job_public(r)
            if j["status"] == "queued":
                j["ahead"] = ahead.get(j["job_id"], 0)
            jobs.append(j)
        order = {"running": 0, "queued": 1, "error": 2, "done": 3, "cancelled": 4}
        jobs.sort(key=lambda j: (order.get(j["status"], 9), -(j.get("created_at") or 0)))
        return jsonify({"jobs": jobs})

    @bp.route("/api/content-editor/status")
    @access
    def ce_job_status():
        job_id = (request.args.get("job_id") or "").strip()
        row = _jobs_exec(f"SELECT * FROM {CE_JOBS_TABLE} WHERE job_id=%s", (job_id,), "one")
        if not row or not _job_owned(row):
            return jsonify({"error": "job not found"}), 404
        out = _content_job_public(row)
        if out["status"] == "queued":
            out["ahead"] = _queued_ahead_map().get(job_id, 0)
        return jsonify(out)

    @bp.route("/api/content-editor/cancel", methods=["POST"])
    @access
    def ce_job_cancel():
        job_id = ((request.json or {}).get("job_id") or "").strip()
        row = _jobs_exec(f"SELECT status, username FROM {CE_JOBS_TABLE} WHERE job_id=%s", (job_id,), "one")
        if not row:
            return jsonify({"ok": True, "removed": True})
        if not _job_owned(row):
            return jsonify({"error": "Forbidden"}), 403
        if row["status"] in ("done", "error", "cancelled"):
            _jobs_exec(f"UPDATE {CE_JOBS_TABLE} SET dismissed=true WHERE job_id=%s", (job_id,))
            return jsonify({"ok": True, "removed": True})
        _jobs_exec(
            f"UPDATE {CE_JOBS_TABLE} SET cancel_requested=true, "
            "status = CASE WHEN status='queued' THEN 'cancelled' ELSE status END, "
            "error = CASE WHEN status='queued' THEN 'отменено' ELSE error END, "
            "finished_at = CASE WHEN status='queued' THEN now() ELSE finished_at END "
            "WHERE job_id=%s", (job_id,))
        return jsonify({"ok": True})

"""Copy-джобы: in-memory состояние очереди копирования + зеркало в create-карточку.

Вынесено из copy_engine.py (чистый code-motion, логика не изменена).
DI инъектится copy_engine.configure() фан-аутом; sibling-модули copy_* — прямой импорт (DAG, цикла нет).
"""
from __future__ import annotations

import threading
import time

# ── DI (инъектится copy_engine.configure фан-аутом; None до инъекции) ──
_CREATE_JOBS = _CREATE_JOBS_LOCK = _JOB_TERMINAL = _job_db_save = _job_touch = _victory_conn_rw = None


def configure(deps: dict) -> None:
    """Инъекция DI из blueprint (фан-аут из copy_engine.configure)."""
    globals().update(deps)


_COPY_JOBS: dict = {}


_COPY_JOBS_LOCK = threading.Lock()


def _copy_job_upsert(job_id: str, **fields) -> dict:
    with _COPY_JOBS_LOCK:
        job = _COPY_JOBS.setdefault(job_id, {"job_id": job_id, "log": [], "created_at": time.time()})
        job.update(fields)
        job["updated_at"] = time.time()
        snap = dict(job)
    _copy_mirror_create_job(job_id, snap)
    return snap


def _copy_mirror_create_job(job_id: str, copy_job: dict) -> None:
    """Mirror copy-flow progress into the shared create queue card."""
    snap = None
    with _CREATE_JOBS_LOCK:
        j = _CREATE_JOBS.get(job_id)
        if not j or j.get("kind") != "copy_campaigns":
            return
        status = copy_job.get("status")
        total = int(copy_job.get("total") or j.get("total") or 0)
        progress = int(copy_job.get("progress") or 0)
        result = copy_job.get("result") if isinstance(copy_job.get("result"), dict) else {}
        rows = result.get("results") or []
        created = sum(1 for r in rows if isinstance(r, dict) and r.get("ok"))
        failed = sum(1 for r in rows if isinstance(r, dict) and r.get("ok") is False)
        if status:
            j["status"] = status
        j["total"] = total
        j["set_total"] = total
        j["done"] = total if status in _JOB_TERMINAL else (min(total, round(total * progress / 100)) if total else progress)
        j["set_done"] = j["done"]
        j["created"] = created or int((result.get("uac_copy") or {}).get("created") or j.get("created") or 0)
        j["failed"] = failed
        if copy_job.get("error"):
            j["error"] = copy_job.get("error")
        if copy_job.get("result") is not None:
            j["result"] = copy_job.get("result")
        _job_touch(j)
        snap = dict(j)
    if snap:
        _job_db_save(job_id, snap, full=status in _JOB_TERMINAL)


def _copy_job_log(job_id: str, message: str) -> None:
    with _COPY_JOBS_LOCK:
        job = _COPY_JOBS.setdefault(job_id, {"job_id": job_id, "log": [], "created_at": time.time()})
        log = job.setdefault("log", [])
        log.append(str(message)[:400])
        if len(log) > 200:
            del log[:-200]
        job["updated_at"] = time.time()
        snap = dict(job)
    _copy_mirror_create_job(job_id, snap)


def _copy_jobs_recover() -> None:
    """Старт copy-сервиса (direct-copy.service): осиротевшие copy-джобы → interrupted.

    Web-posted queued сохраняем: DB-поллер direct-copy.service заберёт их после старта.
    Трогает ТОЛЬКО kind='copy_campaigns' — очередь создания РК в direct.service не задета.
    Авто-докрутку не делаем: повторный «Копировать» сам пропустит уже созданное (суффикс _vNN)."""
    try:
        with _COPY_JOBS_LOCK:
            live_ids = {
                str(job_id)
                for job_id, job in _COPY_JOBS.items()
                if (job or {}).get("status") in ("queued", "running")
            }
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            if live_ids:
                cur.execute(
                    "UPDATE victoryads_direct_automation.jobs SET status='interrupted', updated_at=now() "
                    "WHERE kind='copy_campaigns' "
                    "AND (status='running' OR (status='queued' AND coalesce(body->>'_web_posted','') <> 'true')) "
                    "AND NOT (job_id = ANY(%s))",
                    (list(live_ids),),
                )
            else:
                cur.execute("UPDATE victoryads_direct_automation.jobs SET status='interrupted', updated_at=now() "
                            "WHERE kind='copy_campaigns' "
                            "AND (status='running' OR (status='queued' AND coalesce(body->>'_web_posted','') <> 'true'))")
            cur.execute("UPDATE victoryads_direct_automation.jobs SET status='queued', updated_at=now() "
                        "WHERE kind='copy_campaigns' AND status='claimed' "
                        "AND coalesce(body->>'_web_posted','')='true'")
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass

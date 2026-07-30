"""Воркер Postgres-очереди редактора контента (direct-content-worker.service).

Забирает задания из direct_automation_content_jobs (БД seoadvanced, LXC 101) и выполняет их:
    python -m direct.content_worker

Правила параллельности:
  • глобально CE_WORKER_THREADS потоков (по умолчанию 4);
  • не более 1 выполняющегося задания на аккаунт (login);
  • не более CE_AGENCY_PARALLEL (по умолчанию 2) на одно агентство —
    задания разных агентств параллелятся своими токенами/куками;
  • fairness: приоритет пользователям, у которых сейчас ничего не выполняется,
    внутри — FIFO по created_at.

Рестарт-восстановление: на старте все 'running' возвращаются в 'queued'
(attempts не сбрасывается, при attempts >= CE_MAX_ATTEMPTS задание падает в error).
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _p in Path(__file__).resolve().parents:
    if (_p / ".secret" / "loader.py").exists():
        sys.path.insert(0, str(_p / ".secret"))
        break

os.environ.setdefault("DIRECT_REGISTER_CONTENT_EDITOR", "0")

from direct.core import direct_repository as repository  # noqa: E402
from direct.clients import yandex_gateway as yandex  # noqa: E402
from direct.web import routes_content_editor as ce  # noqa: E402
from direct.agent_board_bridge import notify_content_job_error, notify_unreported_content_errors  # noqa: E402

WORKER_THREADS = int(os.environ.get("CE_WORKER_THREADS") or 4)
AGENCY_PARALLEL = int(os.environ.get("CE_AGENCY_PARALLEL") or 2)
MAX_ATTEMPTS = int(os.environ.get("CE_MAX_ATTEMPTS") or 3)
POLL_SECONDS = float(os.environ.get("CE_POLL_SECONDS") or 2.0)
CONTENT_AGENT_RETRY_POLL = int(os.environ.get("CE_AGENT_RETRY_POLL") or 60)
WORKER_NAME = f"{socket.gethostname()}:{os.getpid()}"

T = ce.CE_JOBS_TABLE

_execute = ce.make_job_executor(
    victory_conn=repository.victory_conn,
    token_for_login=yandex.token_for_login,
    direct_tokens=yandex.direct_tokens,
    v5_call=yandex.v5_call,
    v501_svc=yandex.v501_svc,
)


def _log(msg: str) -> None:
    print(f"[content-worker] {msg}", flush=True)


def _ensure_text_norm_exports() -> None:
    """Refresh long-lived worker imports after code updates.

    The content worker can run for days. If ``direct.text_norm`` was imported before
    new helpers were added on disk, later sibling imports can fail against the cached
    old module object. Reloading only when required keeps the normal hot path intact.
    """
    from direct import text_norm

    required = ("mentions_banned_content", "strip_banned_content")
    if not all(hasattr(text_norm, name) for name in required):
        importlib.reload(text_norm)
    missing = [name for name in required if not hasattr(text_norm, name)]
    if missing:
        raise RuntimeError(f"direct.text_norm missing exports: {', '.join(missing)}")


def _requeue_orphans() -> None:
    ce._jobs_exec(
        f"UPDATE {T} SET status='queued', worker='' WHERE status='running'")


def _claim_job() -> dict | None:
    """Атомарно забрать следующее задание с учётом лимитов и fairness."""
    import psycopg2.extras
    conn = ce._jobs_db()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # клеймы сериализуем advisory-локом: иначе два потока могут
                # одновременно превысить лимит агентства
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('ce_jobs_claim'))")
                cur.execute(f"""
                    SELECT j.* FROM {T} j
                    WHERE j.status='queued'
                      AND NOT EXISTS (SELECT 1 FROM {T} r
                                      WHERE r.status='running' AND r.login=j.login)
                      AND (SELECT count(*) FROM {T} r2
                           WHERE r2.status='running' AND r2.agency=j.agency) < %s
                    ORDER BY
                      (SELECT count(*) FROM {T} r3
                       WHERE r3.status='running' AND r3.username=j.username),
                      j.created_at
                    LIMIT 1
                    FOR UPDATE OF j SKIP LOCKED
                """, (AGENCY_PARALLEL,))
                row = cur.fetchone()
                if not row:
                    return None
                if (row.get("attempts") or 0) >= MAX_ATTEMPTS:
                    cur.execute(
                        f"UPDATE {T} SET status='error', done=1, finished_at=now(), "
                        "error=%s WHERE job_id=%s",
                        (f"превышен лимит попыток ({MAX_ATTEMPTS})", row["job_id"]))
                    return None
                cur.execute(
                    f"UPDATE {T} SET status='running', attempts=attempts+1, "
                    "started_at=now(), finished_at=NULL, worker=%s WHERE job_id=%s",
                    (WORKER_NAME, row["job_id"]))
                job = dict(row)
                return job
    finally:
        conn.close()


def _is_cancelled(job_id: str) -> bool:
    row = ce._jobs_exec(
        f"SELECT cancel_requested FROM {T} WHERE job_id=%s", (job_id,), "one")
    return bool(row and row.get("cancel_requested"))


def _finish(job_id: str, out: dict) -> None:
    import json as _json
    if out.get("cancelled"):
        ce._jobs_exec(
            f"UPDATE {T} SET status='cancelled', done=0, error='отменено', "
            "finished_at=now() WHERE job_id=%s", (job_id,))
        return
    errors = out.get("errors") or []
    replaced = int(out.get("replaced") or 0)
    if out.get("blocked_account"):
        status = "done"
        error = out.get("message") or out.get("blocked_reason") or "аккаунт заблокирован, задача пропущена"
    else:
        status = "done" if (not errors or replaced) else "error"
        error = "" if status == "done" else (errors or ["замена не выполнена"])[0]
    ce._jobs_exec(
        f"UPDATE {T} SET status=%s, done=1, replaced=%s, errors=%s::jsonb, "
        "result=%s::jsonb, error=%s, finished_at=now() WHERE job_id=%s",
        (status, replaced,
         _json.dumps(errors, ensure_ascii=False),
         _json.dumps(out, ensure_ascii=False, default=str),
         str(error)[:500], job_id))
    if status == "error":
        try:
            task_id = notify_content_job_error(ce._jobs_exec, T, job_id)
            if task_id:
                _log(f"agent-board task #{task_id} created for {job_id}")
        except Exception as e:  # noqa: BLE001
            _log(f"agent-board notify failed {job_id}: {str(e)[:160]}")


def _fail(job_id: str, e: Exception) -> None:
    ce._jobs_exec(
        f"UPDATE {T} SET status='error', done=1, error=%s, finished_at=now() "
        "WHERE job_id=%s", (str(e)[:500], job_id))
    try:
        task_id = notify_content_job_error(ce._jobs_exec, T, job_id)
        if task_id:
            _log(f"agent-board task #{task_id} created for {job_id}")
    except Exception as notify_exc:  # noqa: BLE001
        _log(f"agent-board notify failed {job_id}: {str(notify_exc)[:160]}")


def _run_one(job: dict) -> None:
    job_id = job["job_id"]
    login = job.get("login")
    _log(f"start {job_id} login={login} type={job.get('type')} user={job.get('username')} "
         f"attempt={job.get('attempts', 0) + 1}")
    if _is_cancelled(job_id):
        ce._jobs_exec(
            f"UPDATE {T} SET status='cancelled', done=0, error='отменено до запуска', "
            "finished_at=now() WHERE job_id=%s", (job_id,))
        return
    try:
        _ensure_text_norm_exports()
        out = _execute(job, is_cancelled=lambda: _is_cancelled(job_id))
        _finish(job_id, out or {})
        _log(f"done  {job_id} replaced={int((out or {}).get('replaced') or 0)} "
             f"errors={len((out or {}).get('errors') or [])}")
    except Exception as e:  # noqa: BLE001
        _fail(job_id, e)
        _log(f"error {job_id}: {str(e)[:200]}")


def _worker_loop(idx: int) -> None:
    while True:
        try:
            job = _claim_job()
        except Exception as e:  # noqa: BLE001
            _log(f"claim error (thread {idx}): {str(e)[:200]}")
            time.sleep(5)
            continue
        if not job:
            time.sleep(POLL_SECONDS)
            continue
        try:
            _run_one(job)
        except Exception as e:  # noqa: BLE001 — падение финализации (например БД) не должно убить поток
            _log(f"run error (thread {idx}, job {job.get('job_id')}): {str(e)[:200]}")
            time.sleep(5)


def _acquire_singleton():
    """Session-scoped advisory lock: второй процесс воркера не стартует.

    Возвращает соединение (держим открытым весь срок жизни) или None.
    """
    conn = ce._jobs_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT pg_try_advisory_lock(hashtext('ce_worker_singleton'))")
        if cur.fetchone()[0]:
            return conn
    except Exception as e:  # noqa: BLE001
        _log(f"singleton lock error: {str(e)[:200]}")
    conn.close()
    return None


def _requeue_stale() -> None:
    """Watchdog: running старше 2 часов — задание зависло (поток умер/HTTP завис) → в очередь.
    attempts не сбрасывается: CE_MAX_ATTEMPTS остановит вечный цикл."""
    ce._jobs_exec(
        f"UPDATE {T} SET status='queued', worker='' "
        "WHERE status='running' AND started_at < now() - interval '2 hours'")


def _content_retry_insert_from_failed(jobs_exec, table: str, row: dict) -> str:
    """Build a fresh content_jobs INSERT from a failed row (mirrors _copy_retry_body_from_failed,
    but content_jobs stores flat columns, not one body jsonb — see agent_board_bridge.py note)."""
    import json
    import uuid

    job_id = "ce_" + uuid.uuid4().hex[:12]
    access = row.get("access_directologists")
    jobs_exec(
        f"""INSERT INTO {table}
            (job_id, username, login, agency, type, old_text, new_text, mode,
             campaign_count, access_directologists)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (job_id, "agent-board-auto", row["login"], row.get("agency") or "",
         row["type"], row["old_text"], row["new_text"], row.get("mode") or "exact",
         row.get("campaign_count") or 1,
         json.dumps(access, ensure_ascii=False) if access is not None else None),
    )
    return job_id


def _content_login_has_active_job(jobs_exec, table: str, login: str) -> bool:
    row = jobs_exec(
        f"SELECT 1 AS x FROM {table} WHERE login=%s AND status IN ('queued','running') LIMIT 1",
        (login,), "one",
    )
    return bool(row)


def _content_agent_retry_daemon_loop() -> None:
    """When a linked Agent Board task is done, requeue the failed content job once.

    Mirrors _copy_agent_retry_daemon_loop (queue_server.py), but content_jobs has no
    'interrupted' status, so the retry is one-shot (content_retry_job_id IS NULL gate,
    no re-attempt). CE_DAILY_JOB_CAP is intentionally bypassed here, same as copy does."""
    from .agent_board_bridge import (
        ensure_content_retry_columns,
        content_jobs_ready_for_agent_retry,
        mark_content_retry_started,
    )
    while True:
        try:
            ensure_content_retry_columns(ce._jobs_exec, T)
            rows = content_jobs_ready_for_agent_retry(ce._jobs_exec, T, limit=5)
        except Exception as e:  # noqa: BLE001
            _log(f"[content-agent-retry] scan failed: {str(e)[:160]}")
            rows = []
        for row in rows:
            failed_jid = row.get("job_id")
            login = row.get("login") or ""
            if not failed_jid or not login:
                continue
            if _content_login_has_active_job(ce._jobs_exec, T, login):
                continue
            try:
                retry_jid = _content_retry_insert_from_failed(ce._jobs_exec, T, row)
            except Exception as e:  # noqa: BLE001
                _log(f"[content-agent-retry] insert failed {failed_jid}: {str(e)[:160]}")
                continue
            try:
                mark_content_retry_started(ce._jobs_exec, T, failed_jid, retry_jid)
            except Exception as e:  # noqa: BLE001
                _log(f"[content-agent-retry] mark failed {failed_jid}->{retry_jid}: {str(e)[:160]}")
            _log(f"[content-agent-retry] {failed_jid} -> {retry_jid} login={login}")
        time.sleep(max(10, CONTENT_AGENT_RETRY_POLL))


def main() -> None:
    guard = _acquire_singleton()
    if guard is None:
        _log("другой процесс воркера уже держит ce_worker_singleton — выходим")
        return
    try:
        ce.ensure_jobs_table()
        notify_unreported_content_errors(ce._jobs_exec, T, limit=10)
    except Exception as e:  # noqa: BLE001 — гонка каталога при одновременном старте с flask
        _log(f"ensure_jobs_table: {str(e)[:200]}")
    _requeue_orphans()
    threading.Thread(target=_content_agent_retry_daemon_loop, daemon=True,
                      name="ce-agent-retry").start()
    _log(f"started: threads={WORKER_THREADS}, agency_parallel={AGENCY_PARALLEL}, "
         f"daily_cap={ce.CE_DAILY_JOB_CAP}, worker={WORKER_NAME}")
    threads = [threading.Thread(target=_worker_loop, args=(i,), daemon=True, name=f"ce-worker-{i}")
               for i in range(WORKER_THREADS)]
    for t in threads:
        t.start()
    while True:
        time.sleep(60)
        try:
            _requeue_stale()
            notify_unreported_content_errors(ce._jobs_exec, T, limit=10)
        except Exception as e:  # noqa: BLE001
            _log(f"watchdog error: {str(e)[:150]}")


if __name__ == "__main__":
    main()

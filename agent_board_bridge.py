"""Bridge failed Direct content jobs to Agent Board."""
from __future__ import annotations

import json
import traceback
from typing import Any, Callable


PROJECT_PATH = "/opt/scripts/home/seoadvanced"


def _short(value: Any, limit: int = 1200) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return (text or "")[:limit]


def ensure_content_job_agent_column(jobs_exec: Callable, table: str) -> None:
    jobs_exec(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS agent_board_task_id bigint")


def ensure_price_job_agent_column(victory_conn_rw: Callable) -> None:
    conn = victory_conn_rw()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE public.direct_price_check_jobs "
                "ADD COLUMN IF NOT EXISTS agent_board_task_id bigint"
            )
        conn.commit()
    finally:
        conn.close()


def ensure_copy_job_agent_columns(victory_conn_rw: Callable) -> None:
    conn = victory_conn_rw()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE public.direct_automation_jobs "
                "ADD COLUMN IF NOT EXISTS agent_board_task_id bigint"
            )
            cur.execute(
                "ALTER TABLE public.direct_automation_jobs "
                "ADD COLUMN IF NOT EXISTS copy_retry_job_id text"
            )
            cur.execute(
                "ALTER TABLE public.direct_automation_jobs "
                "ADD COLUMN IF NOT EXISTS copy_retry_started_at timestamptz"
            )
        conn.commit()
    finally:
        conn.close()


def _create_agent_task(title: str, description: str, *, requested_by: str = "direct-content") -> int | None:
    try:
        from agent_board import db as agent_db

        agent_db.init_tables()
        task = agent_db.create_task(
            title=title,
            description=description,
            priority=3,
            model="gpt-5.5",
            model_reason="direct-content failed job auto-triage",
            project_path=PROJECT_PATH,
            requested_by=requested_by,
            status="queued",
        )
        return int(task["id"])
    except Exception:  # noqa: BLE001 - failed job must not fail again because board is down
        traceback.print_exc()
        return None


def notify_content_job_error(jobs_exec: Callable, table: str, job_id: str) -> int | None:
    """Create one Agent Board task for a terminal content-editor error."""
    ensure_content_job_agent_column(jobs_exec, table)
    row = jobs_exec(
        f"SELECT * FROM {table} WHERE job_id=%s AND status='error'",
        (job_id,),
        "one",
    )
    if not row or row.get("agent_board_task_id"):
        return int(row["agent_board_task_id"]) if row and row.get("agent_board_task_id") else None

    title = f"Direct content-editor: исправить упавшую задачу {job_id}"
    description = f"""Автоматически создано из очереди content-editor.

Исходная задача:
- table: {table}
- job_id: {job_id}
- username: {row.get('username') or ''}
- login: {row.get('login') or ''}
- type: {row.get('type') or ''}
- mode: {row.get('mode') or ''}
- old_text: {_short(row.get('old_text'), 500)}
- new_text: {_short(row.get('new_text'), 500)}
- attempts: {row.get('attempts')}
- error: {_short(row.get('error'), 1000)}
- errors: {_short(row.get('errors'), 2000)}
- result: {_short(row.get('result'), 3000)}

Что нужно сделать:
1. Воспроизвести причину ошибки по этой job.
2. Исправить код/данные так, чтобы такая ошибка больше не повторялась.
3. Переисполнить или добить исходную операцию до terminal success без ошибок там, где это разрешено активными объектами Direct.
4. Проверить live-состояние через сервис/Direct.
5. Обновить `direct/ERRORS_JOURNAL.md` и `direct/STATE.md`.

Не делать destructive-действий без явной необходимости. Архивные кампании не восстанавливать вручную без отдельного решения Семёна.
"""
    task_id = _create_agent_task(title, description, requested_by=row.get("username") or "direct-content")
    if task_id:
        jobs_exec(
            f"UPDATE {table} SET agent_board_task_id=%s WHERE job_id=%s AND agent_board_task_id IS NULL",
            (task_id, job_id),
        )
    return task_id


def _copy_job_task_description(row: dict[str, Any]) -> str:
    body = row.get("body") or {}
    result = row.get("result") or {}
    campaign_ids = body.get("campaign_ids") or []
    return f"""Автоматически создано из очереди копирования кампаний.

Исходная copy job:
- table: public.direct_automation_jobs
- job_id: {row.get('job_id') or ''}
- source_login: {body.get('source_login') or ''}
- target_login: {body.get('target_login') or row.get('login') or ''}
- agency: {row.get('agency') or body.get('agency') or ''}
- campaigns: {len(campaign_ids)} ids
- campaign_ids: {_short(campaign_ids, 2000)}
- target_domain: {body.get('target_domain') or ''}
- target_city/region: {body.get('target_city') or ''} / {body.get('target_region') or ''}
- counter_id/goal_id: {body.get('counter_id') or ''} / {body.get('goal_id') or ''}
- error: {_short(row.get('error'), 1200)}
- result: {_short(result, 3000)}

Что нужно сделать:
1. Воспроизвести причину ошибки по этой copy job.
2. Исправить код/данные так, чтобы такая ошибка больше не повторялась.
3. Задеплоить исправление в `direct-copy.service` и проверить сервис.
4. Перевести эту Agent Board задачу в `done`, когда исправление готово.
5. После `done` copy-service автоматически поставит повторную `copy_campaigns` job с тем же source/target/campaign_ids и доведёт копирование обычной очередью. Не создавай дубль вручную без отдельной причины.
6. Если повтор опять упадёт, будет создана новая Agent Board задача по новой failed copy job.

Не смешивать с сервисом создания кампаний: это очередь копирования `direct-copy.service`.
"""


def notify_copy_job_error(victory_conn_rw: Callable, job_id: str) -> int | None:
    """Create one Agent Board task for a terminal copy_campaigns error."""
    ensure_copy_job_agent_columns(victory_conn_rw)
    import psycopg2.extras

    conn = victory_conn_rw()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"direct_copy_agent_task:{job_id}",))
            cur.execute(
                "SELECT * FROM public.direct_automation_jobs "
                "WHERE job_id=%s AND kind='copy_campaigns' AND status='error'",
                (job_id,),
            )
            row = cur.fetchone()
            if not row or row.get("agent_board_task_id"):
                conn.commit()
                return int(row["agent_board_task_id"]) if row and row.get("agent_board_task_id") else None

            body = row.get("body") or {}
            title = (
                "Direct copy: исправить упавшее копирование "
                f"{body.get('source_login') or '?'} → {body.get('target_login') or row.get('login') or '?'}"
            )
            task_id = _create_agent_task(
                title,
                _copy_job_task_description(dict(row)),
                requested_by=(body.get("created_by") or row.get("login") or "direct-copy"),
            )
            if task_id:
                cur.execute(
                    "UPDATE public.direct_automation_jobs SET agent_board_task_id=%s "
                    "WHERE job_id=%s AND agent_board_task_id IS NULL",
                    (task_id, job_id),
                )
        conn.commit()
        return task_id
    finally:
        conn.close()


def _agent_board_done_task_meta(task_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Read Agent Board task status from its own home_server DB, not Victory."""
    ids = sorted({int(x) for x in task_ids if x})
    if not ids:
        return {}
    try:
        import psycopg2.extras
        from agent_board import db as agent_db

        with agent_db._connect() as conn:  # Agent Board owns a separate seoadvanced DB.
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, status, updated_at, finished_at FROM tasks "
                    "WHERE id = ANY(%s) AND status = 'done'",
                    (ids,),
                )
                return {int(r["id"]): dict(r) for r in cur.fetchall() or []}
    except Exception:  # noqa: BLE001 - retry daemon must keep polling if board DB is unavailable
        traceback.print_exc()
        return {}


def copy_jobs_ready_for_agent_retry(victory_conn_rw: Callable, *, limit: int = 5) -> list[dict[str, Any]]:
    """Failed copy jobs whose Agent Board task is done and whose retry needs to run."""
    ensure_copy_job_agent_columns(victory_conn_rw)
    import psycopg2.extras

    conn = victory_conn_rw()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT j.*, r.status AS copy_retry_status
                   FROM public.direct_automation_jobs j
                   LEFT JOIN public.direct_automation_jobs r ON r.job_id = j.copy_retry_job_id
                   WHERE j.kind='copy_campaigns'
                     AND j.status='error'
                     AND j.agent_board_task_id IS NOT NULL
                     AND (
                           j.copy_retry_job_id IS NULL
                           OR r.status = 'interrupted'
                         )
                   ORDER BY j.updated_at
                   LIMIT %s""",
                (max(1, int(limit)) * 5,),
            )
            rows = [dict(r) for r in cur.fetchall() or []]
        conn.commit()
    finally:
        conn.close()
    done_tasks = _agent_board_done_task_meta([
        int(r["agent_board_task_id"]) for r in rows if r.get("agent_board_task_id")
    ])
    ready: list[dict[str, Any]] = []
    for row in rows:
        task_id = int(row.get("agent_board_task_id") or 0)
        if task_id not in done_tasks:
            continue
        row["_agent_board_task"] = done_tasks[task_id]
        ready.append(row)
        if len(ready) >= max(1, int(limit)):
            break
    return ready


def mark_copy_retry_started(victory_conn_rw: Callable, failed_job_id: str, retry_job_id: str) -> bool:
    ensure_copy_job_agent_columns(victory_conn_rw)
    conn = victory_conn_rw()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.direct_automation_jobs "
                "SET copy_retry_job_id=%s, copy_retry_started_at=now(), updated_at=now() "
                "WHERE job_id=%s AND kind='copy_campaigns' AND status='error' "
                "  AND (copy_retry_job_id IS NULL OR EXISTS ("
                "      SELECT 1 FROM public.direct_automation_jobs r "
                "      WHERE r.job_id = public.direct_automation_jobs.copy_retry_job_id "
                "        AND r.status = 'interrupted'"
                "  )) "
                "RETURNING job_id",
                (retry_job_id, failed_job_id),
            )
            ok = cur.fetchone() is not None
        conn.commit()
        return ok
    finally:
        conn.close()


def notify_unreported_content_errors(jobs_exec: Callable, table: str, *, limit: int = 5) -> list[int]:
    """Create an Agent Board task only for the newest terminal content error.

    This watchdog runs on worker start and every minute. It must not backfill old
    historical failures in batches: the board should show only the latest broken
    content job that still needs manual attention.
    """
    ensure_content_job_agent_column(jobs_exec, table)
    row = jobs_exec(
        f"SELECT job_id, agent_board_task_id FROM {table} "
        "WHERE status='error' "
        "ORDER BY finished_at DESC NULLS LAST, created_at DESC LIMIT 1",
        (),
        "one",
    )
    if not row:
        return []
    if row.get("agent_board_task_id"):
        return [int(row["agent_board_task_id"])]
    task_id = notify_content_job_error(jobs_exec, table, row["job_id"])
    return [task_id] if task_id else []


def notify_price_job_error(victory_conn_rw: Callable, job_id: str) -> int | None:
    """Create one Agent Board task for a terminal price-check/apply error."""
    ensure_price_job_agent_column(victory_conn_rw)
    import psycopg2.extras

    conn = victory_conn_rw()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM public.direct_price_check_jobs "
                "WHERE job_id=%s AND status='error'",
                (job_id,),
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    if not row or row.get("agent_board_task_id"):
        return int(row["agent_board_task_id"]) if row and row.get("agent_board_task_id") else None

    title = f"Direct pricecheck: исправить упавшую задачу {job_id}"
    description = f"""Автоматически создано из очереди сверки/заливки цен.

Исходная задача:
- table: public.direct_price_check_jobs
- job_id: {job_id}
- kind: {row.get('kind') or ''}
- created_by: {row.get('created_by') or ''}
- logins: {_short(row.get('logins'), 2000)}
- params: {_short(row.get('params'), 3000)}
- done/total: {row.get('done')}/{row.get('total')}
- message: {_short(row.get('message'), 1000)}
- error: {_short(row.get('error'), 1000)}
- result: {_short(row.get('result'), 3000)}

Что нужно сделать:
1. Воспроизвести причину ошибки по этой job.
2. Исправить код/данные так, чтобы такая ошибка больше не повторялась.
3. Переисполнить или добить исходную заливку цен до terminal success без ошибок.
4. Проверить live-состояние через сервис/Direct.
5. Обновить `direct/ERRORS_JOURNAL.md` и `direct/STATE.md`.

Заливку цен из очереди вручную запускать только если действие выполняется от admin-контекста или через безопасный серверный recovery этой задачи.
"""
    task_id = _create_agent_task(title, description, requested_by=row.get("created_by") or "direct-pricecheck")
    if task_id:
        conn = victory_conn_rw()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE public.direct_price_check_jobs SET agent_board_task_id=%s "
                    "WHERE job_id=%s AND agent_board_task_id IS NULL",
                    (task_id, job_id),
                )
            conn.commit()
        finally:
            conn.close()
    return task_id


def notify_unreported_price_errors(victory_conn_rw: Callable, *, limit: int = 5) -> list[int]:
    """Backfill Agent Board tasks for terminal price errors without a linked task."""
    ensure_price_job_agent_column(victory_conn_rw)
    conn = victory_conn_rw()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT job_id FROM public.direct_price_check_jobs "
                "WHERE status='error' AND agent_board_task_id IS NULL "
                "ORDER BY finished_at DESC NULLS LAST, created_at DESC LIMIT %s",
                (int(limit),),
            )
            rows = cur.fetchall() or []
        conn.commit()
    finally:
        conn.close()
    out: list[int] = []
    for (job_id,) in rows:
        task_id = notify_price_job_error(victory_conn_rw, job_id)
        if task_id:
            out.append(task_id)
    return out

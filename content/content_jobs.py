"""Postgres queue helpers for the Direct content editor."""
from __future__ import annotations

import os
import time


CE_JOBS_TABLE = "direct_automation.content_jobs"
CE_DAILY_JOB_CAP = int(os.environ.get("CE_DAILY_JOB_CAP") or 50)
CE_EKB_DAY_SQL = (
    "(date_trunc('day', now() AT TIME ZONE 'Asia/Yekaterinburg') "
    "AT TIME ZONE 'Asia/Yekaterinburg')"
)


def _jobs_db():
    from telegram_parsing.db import get_db

    return get_db()


def _jobs_exec(query: str, params: tuple = (), fetch: str | None = None):
    import psycopg2.extras

    conn = _jobs_db()
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


def ensure_jobs_table() -> None:
    _jobs_exec(f"""
        CREATE TABLE IF NOT EXISTS {CE_JOBS_TABLE} (
            job_id text PRIMARY KEY,
            username text NOT NULL DEFAULT '',
            login text NOT NULL,
            agency text NOT NULL DEFAULT '',
            type text NOT NULL,
            old_text text NOT NULL,
            new_text text NOT NULL,
            campaign_count int NOT NULL DEFAULT 0,
            access_directologists jsonb,
            status text NOT NULL DEFAULT 'queued',
            cancel_requested boolean NOT NULL DEFAULT false,
            dismissed boolean NOT NULL DEFAULT false,
            attempts int NOT NULL DEFAULT 0,
            done int NOT NULL DEFAULT 0,
            total int NOT NULL DEFAULT 1,
            replaced int NOT NULL DEFAULT 0,
            error text NOT NULL DEFAULT '',
            errors jsonb NOT NULL DEFAULT '[]'::jsonb,
            result jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            started_at timestamptz,
            finished_at timestamptz,
            worker text NOT NULL DEFAULT '',
            agent_board_task_id bigint
        )""")
    for ddl in (
        f"ALTER TABLE {CE_JOBS_TABLE} ADD COLUMN IF NOT EXISTS mode text NOT NULL DEFAULT 'exact'",
        f"ALTER TABLE {CE_JOBS_TABLE} ADD COLUMN IF NOT EXISTS agent_board_task_id bigint",
        f"CREATE INDEX IF NOT EXISTS {CE_JOBS_TABLE}_status_idx ON {CE_JOBS_TABLE}(status)",
        f"CREATE INDEX IF NOT EXISTS {CE_JOBS_TABLE}_login_day_idx ON {CE_JOBS_TABLE}(login, created_at)",
    ):
        try:
            _jobs_exec(ddl)
        except Exception:  # noqa: BLE001
            pass


def _content_job_public(row: dict) -> dict:
    def _ts(v):
        return v.timestamp() if v is not None else None

    status = row.get("status") or ""
    started = _ts(row.get("started_at")) or _ts(row.get("created_at")) or time.time()
    if status == "running":
        elapsed = time.time() - started
    elif status in ("done", "error", "cancelled"):
        elapsed = (_ts(row.get("finished_at")) or time.time()) - started
    else:
        elapsed = 0
    return {
        "job_id": row.get("job_id"),
        "login": row.get("login"),
        "username": row.get("username") or "",
        "type": row.get("type"),
        "campaign_count": int(row.get("campaign_count") or 0),
        "status": status,
        "done": row.get("done") or 0,
        "total": row.get("total") or 1,
        "replaced": row.get("replaced") or 0,
        "error": row.get("error") or "",
        "errors": row.get("errors") or [],
        "result": row.get("result") or {},
        "ahead": 0,
        "elapsed": elapsed,
        "created_at": _ts(row.get("created_at")),
    }

"""
БД-слой схемы direct_autorules (PostgreSQL, БД seoadvanced).

Подключение через configure()-DI: conn-фабрика инъецируется из autorules_main.py
(там же строится DSN из load_db("home_server")). Репозиторий не знает о loader.py.

Таблицы:
  rules        — правила: условие (condition_json) + действие (action_json) + расписание
  sensor_runs  — история прогонов сенсоров (что нашли)
  rule_runs    — история прогонов правил (решение + applied)
  alerts       — алерты для UI (seen/unseen)
  audit_log    — полный аудит: что нашли, что применили, результат
"""
from __future__ import annotations

from typing import Callable

import psycopg2.extras

# ── Dependency injection ───────────────────────────────────────────────────────

_conn_factory: Callable | None = None


def configure(conn_factory: Callable) -> None:
    """Инъецировать conn-фабрику. Вызывается из autorules_main.create_app()."""
    global _conn_factory
    _conn_factory = conn_factory


def _connect():
    if _conn_factory is None:
        raise RuntimeError(
            "direct.autorules.repository not configured; "
            "call configure(conn_factory) before use."
        )
    return _conn_factory()


# ── Schema DDL ────────────────────────────────────────────────────────────────

_DDL = """
CREATE SCHEMA IF NOT EXISTS direct_autorules;

SET search_path TO direct_autorules;

CREATE TABLE IF NOT EXISTS rules (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    condition_json  JSONB NOT NULL DEFAULT '{}',
    action_json     JSONB NOT NULL DEFAULT '{}',
    mode            TEXT NOT NULL DEFAULT 'manual',
    schedule        TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
    account_login   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sensor_runs (
    id              BIGSERIAL PRIMARY KEY,
    account_login   TEXT NOT NULL,
    sensor_key      TEXT NOT NULL,
    found           INTEGER NOT NULL DEFAULT 0,
    details_json    JSONB NOT NULL DEFAULT '[]',
    ran_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sensor_runs_login_key
    ON sensor_runs (account_login, sensor_key, ran_at DESC);

CREATE TABLE IF NOT EXISTS rule_runs (
    id              BIGSERIAL PRIMARY KEY,
    rule_id         INTEGER REFERENCES rules(id) ON DELETE SET NULL,
    account_login   TEXT NOT NULL,
    decision_json   JSONB NOT NULL DEFAULT '{}',
    applied         BOOLEAN NOT NULL DEFAULT FALSE,
    ran_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rule_runs_login
    ON rule_runs (account_login, ran_at DESC);

CREATE TABLE IF NOT EXISTS alerts (
    id              BIGSERIAL PRIMARY KEY,
    account_login   TEXT NOT NULL,
    kind            TEXT NOT NULL,
    payload_json    JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    seen            BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_alerts_login_seen
    ON alerts (account_login, seen, created_at DESC);

CREATE TABLE IF NOT EXISTS audit_log (
    id              BIGSERIAL PRIMARY KEY,
    account_login   TEXT NOT NULL,
    entity          TEXT NOT NULL,
    source          TEXT,
    found_json      JSONB NOT NULL DEFAULT '{}',
    action_json     JSONB NOT NULL DEFAULT '{}',
    result          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_log_login
    ON audit_log (account_login, created_at DESC);
"""


def ensure_schema() -> None:
    """Идемпотентно создаёт схему и все таблицы. Вызывается при старте сервиса."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_DDL)
        conn.commit()
    finally:
        conn.close()


# ── Rules CRUD ────────────────────────────────────────────────────────────────

def rules_list(account_login: str | None = None) -> list[dict]:
    """Список правил. Если account_login указан — только для него + глобальные (NULL)."""
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if account_login:
                cur.execute(
                    "SELECT id, name, condition_json, action_json, mode, schedule, status, "
                    "account_login, created_at::text FROM rules "
                    "WHERE account_login = %s OR account_login IS NULL ORDER BY id",
                    (account_login,),
                )
            else:
                cur.execute(
                    "SELECT id, name, condition_json, action_json, mode, schedule, status, "
                    "account_login, created_at::text FROM rules ORDER BY id"
                )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def rules_get(rule_id: int) -> dict | None:
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, name, condition_json, action_json, mode, schedule, status, "
                "account_login, created_at::text FROM rules WHERE id = %s",
                (rule_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def rules_create(
    name: str,
    condition_json: dict,
    action_json: dict,
    mode: str = "manual",
    schedule: str | None = None,
    status: str = "active",
    account_login: str | None = None,
) -> int:
    """Создаёт правило, возвращает id."""
    import json
    sql = """
    INSERT INTO rules (name, condition_json, action_json, mode, schedule, status, account_login)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    RETURNING id
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (name, json.dumps(condition_json), json.dumps(action_json),
                 mode, schedule, status, account_login),
            )
            rule_id = cur.fetchone()[0]
        conn.commit()
        return rule_id
    finally:
        conn.close()


def rules_update(rule_id: int, **fields) -> bool:
    """Обновляет переданные поля правила. Возвращает True если строка найдена."""
    import json
    allowed = {"name", "condition_json", "action_json", "mode", "schedule", "status", "account_login"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    # Сериализуем dict-значения в JSON
    params = {}
    for k, v in updates.items():
        params[k] = json.dumps(v) if isinstance(v, (dict, list)) else v
    set_clause = ", ".join(f"{k} = %({k})s" for k in params)
    params["rule_id"] = rule_id
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE rules SET {set_clause} WHERE id = %(rule_id)s", params)
            updated = cur.rowcount > 0
        conn.commit()
        return updated
    finally:
        conn.close()


def rules_delete(rule_id: int) -> bool:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM rules WHERE id = %s", (rule_id,))
            deleted = cur.rowcount > 0
        conn.commit()
        return deleted
    finally:
        conn.close()


# ── Append helpers ────────────────────────────────────────────────────────────

def sensor_runs_append(
    account_login: str,
    sensor_key: str,
    found: int,
    details: list | dict,
) -> int:
    """Записывает результат прогона сенсора, возвращает id строки."""
    import json
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sensor_runs (account_login, sensor_key, found, details_json) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (account_login, sensor_key, found, json.dumps(details)),
            )
            row_id = cur.fetchone()[0]
        conn.commit()
        return row_id
    finally:
        conn.close()


def rule_runs_append(
    rule_id: int | None,
    account_login: str,
    decision: dict,
    applied: bool = False,
) -> int:
    """Записывает результат прогона правила, возвращает id строки."""
    import json
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO rule_runs (rule_id, account_login, decision_json, applied) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (rule_id, account_login, json.dumps(decision), applied),
            )
            row_id = cur.fetchone()[0]
        conn.commit()
        return row_id
    finally:
        conn.close()


def audit_log_append(
    account_login: str,
    entity: str,
    found: dict,
    action: dict,
    result: str,
    source: str | None = None,
) -> int:
    """Добавляет запись в аудит-лог, возвращает id строки."""
    import json
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_log "
                "(account_login, entity, source, found_json, action_json, result) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (account_login, entity, source,
                 json.dumps(found), json.dumps(action), result),
            )
            row_id = cur.fetchone()[0]
        conn.commit()
        return row_id
    finally:
        conn.close()


def audit_log_list(
    account_login: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Последние записи журнала действий. Если login задан — только по нему."""
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if account_login:
                cur.execute(
                    "SELECT id, account_login, entity, source, "
                    "found_json, action_json, result, created_at::text "
                    "FROM audit_log WHERE account_login = %s "
                    "ORDER BY created_at DESC LIMIT %s",
                    (account_login, limit),
                )
            else:
                cur.execute(
                    "SELECT id, account_login, entity, source, "
                    "found_json, action_json, result, created_at::text "
                    "FROM audit_log ORDER BY created_at DESC LIMIT %s",
                    (limit,),
                )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def alerts_append(
    account_login: str,
    kind: str,
    payload: dict,
) -> int:
    """Добавляет алерт для UI, возвращает id строки."""
    import json
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO alerts (account_login, kind, payload_json) "
                "VALUES (%s, %s, %s) RETURNING id",
                (account_login, kind, json.dumps(payload)),
            )
            row_id = cur.fetchone()[0]
        conn.commit()
        return row_id
    finally:
        conn.close()

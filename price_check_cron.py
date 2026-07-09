"""Крон-триггеры сверки/заливки цен Директ↔фиды (расписание в TZ Екатеринбурга).

Сервер LXC 101 в TZ +05 (Екб) → cron работает в локальном времени, конвертация в UTC
не нужна. Записи в root crontab:
    0 2  * * *  .../python3 -m direct.price_check_cron check   # 02:00 Екб — ночная сверка ВСЕХ активных
    0 20 * * *  .../python3 -m direct.price_check_cron apply   # 20:00 Екб — заливка очереди заявок

  • check — снимок Direct + фиды + сравнение по ВСЕМ активным Авто-логинам (created_by='cron-02').
  • apply — берёт из direct_price_check_jobs все заявки status='queued' (kind='apply'), поставленные
    за день кнопкой «Отправить на изменения», и выполняет их (ads.update). Что не в очереди — не трогает.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for _p in Path(__file__).resolve().parents:
    if (_p / ".secret" / "loader.py").exists():
        sys.path.insert(0, str(_p / ".secret"))
        break

os.environ.setdefault("DIRECT_REGISTER_CONTENT_EDITOR", "0")

from direct import blueprint as direct_legacy  # noqa: E402
from direct import price_check as pc  # noqa: E402


def _log(msg: str) -> None:
    print(f"[price-check-cron] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def _deps() -> dict:
    return {
        "victory_conn": direct_legacy._victory_conn,
        "victory_conn_rw": direct_legacy._victory_conn_rw,
        "token_for_login": direct_legacy._token_for_login,
        "direct_tokens": direct_legacy._direct_tokens,
        "v5_call": direct_legacy._v5_call,
    }


DEFAULT_STATUS = getattr(direct_legacy, "DEFAULT_STATUS", "Контекст активно")
EXCLUDE = getattr(direct_legacy, "_EXCLUDE_DIRECTOLOGS", []) or []


def run_check_all() -> str:
    """Ночная сверка (02:00): снимок+сравнение по всем активным логинам. Возвращает job_id."""
    pc.ensure_price_check_tables(direct_legacy._victory_conn_rw)
    pc.reconcile_stuck_jobs(direct_legacy._victory_conn_rw)
    items = pc.active_logins(direct_legacy._victory_conn, status=DEFAULT_STATUS, exclude=EXCLUDE)
    if not items:
        _log("check: активных логинов не найдено — выход")
        return ""
    job_id = pc.new_job_id()
    pc._job_insert(direct_legacy._victory_conn_rw, job_id, "check", "cron-02",
                   [it["login"] for it in items], {"trigger": "cron-02:00"}, len(items))
    _log(f"check: старт job={job_id}, логинов={len(items)}")
    pc.run_check_job(_deps(), job_id, items)
    row = pc.job_public(direct_legacy._victory_conn, job_id)
    _log(f"check: done job={job_id} status={row and row.get('status')} msg={row and row.get('message')}")
    return job_id


def run_apply_queue() -> int:
    """Заливка очереди (20:00): выполняет все queued apply-заявки за день. Возвращает число обработанных."""
    pc.ensure_price_check_tables(direct_legacy._victory_conn_rw)
    pc.reconcile_stuck_jobs(direct_legacy._victory_conn_rw)
    jobs = pc.queued_apply_jobs(direct_legacy._victory_conn)
    if not jobs:
        _log("apply: очередь заявок пуста — выход")
        return 0
    _log(f"apply: заявок в очереди={len(jobs)}")
    done = 0
    for j in jobs:
        job_id = j["job_id"]
        params = j.get("params") or {}
        items = params.get("items") or []
        if not items:
            pc._job_finish(direct_legacy._victory_conn_rw, job_id, "done", {"items": 0},
                           message="пустая заявка")
            continue
        # queued → running (+ started_at для корректного elapsed) перед выполнением
        pc.mark_running(direct_legacy._victory_conn_rw, job_id, "выполняется (крон 20:00)")
        _log(f"apply: старт заявки job={job_id}, позиций={len(items)}")
        pc.run_apply_job(_deps(), job_id, items)
        row = pc.job_public(direct_legacy._victory_conn, job_id)
        _log(f"apply: заявка job={job_id} status={row and row.get('status')} msg={row and row.get('message')}")
        done += 1
    _log(f"apply: обработано заявок={done}")
    return done


def main() -> None:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
    if mode == "check":
        run_check_all()
    elif mode == "apply":
        run_apply_queue()
    else:
        print("usage: python -m direct.price_check_cron {check|apply}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()

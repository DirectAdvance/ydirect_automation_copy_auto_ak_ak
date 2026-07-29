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

from direct import account_service as accounts  # noqa: E402
from direct.core import direct_repository as repository  # noqa: E402
from direct.clients import yandex_gateway as yandex  # noqa: E402
from direct import price_check as pc  # noqa: E402


def _log(msg: str) -> None:
    print(f"[price-check-cron] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def _deps() -> dict:
    return {
        "victory_conn": repository.victory_conn,
        "victory_conn_rw": repository.victory_conn_rw,
        "token_for_login": yandex.token_for_login,
        "direct_tokens": yandex.direct_tokens,
        "v5_call": yandex.v5_call,
    }


DEFAULT_STATUS = accounts.DEFAULT_STATUS
EXCLUDE = accounts._EXCLUDE_DIRECTOLOGS


def run_check_all() -> str:
    """Ночная сверка (02:00): снимок+сравнение по всем активным логинам. Возвращает job_id."""
    pc.ensure_price_check_tables(repository.victory_conn_rw)
    pc.reconcile_stuck_jobs(repository.victory_conn_rw)
    items = pc.active_logins(repository.victory_conn, status=DEFAULT_STATUS, exclude=EXCLUDE)
    if not items:
        _log("check: активных логинов не найдено — выход")
        return ""
    job_id = pc.new_job_id()
    pc._job_insert(repository.victory_conn_rw, job_id, "check", "cron-02",
                   [it["login"] for it in items], {"trigger": "cron-02:00"}, len(items))
    _log(f"check: старт job={job_id}, логинов={len(items)}")
    pc.run_check_job(_deps(), job_id, items)
    row = pc.job_public(repository.victory_conn, job_id)
    _log(f"check: done job={job_id} status={row and row.get('status')} msg={row and row.get('message')}")
    return job_id


def run_apply_queue() -> int:
    """Заливка очереди (20:00): сливает ВСЕ queued apply-заявки за день в ОДИН пул и
    гонит их вместе (разные агентства параллельно, одно агентство — 1 поток на токен).
    Раньше заявки шли строго по одной (заявка №2 ждала полного финиша №1), из-за чего
    разные агентства разных специалистов не параллелились. Возвращает число заявок."""
    pc.ensure_price_check_tables(repository.victory_conn_rw)
    pc.reconcile_stuck_jobs(repository.victory_conn_rw)
    jobs = pc.queued_apply_jobs(repository.victory_conn)
    if not jobs:
        _log("apply: очередь заявок пуста — выход")
        return 0
    specs = []
    for j in jobs:
        job_id = j["job_id"]
        items = (j.get("params") or {}).get("items") or []
        if not items:
            pc._job_finish(repository.victory_conn_rw, job_id, "done", {"items": 0},
                           message="пустая заявка")
            continue
        # queued → running (+ started_at для корректного elapsed) перед выполнением.
        # mark_running с guard'ом status='queued': если заявку удалили между SELECT и сейчас,
        # вернётся False — тогда в пул её НЕ берём (иначе залили бы «удалённые» цены).
        if pc.mark_running(repository.victory_conn_rw, job_id, "выполняется (крон 20:00)"):
            specs.append({"job_id": job_id, "items": items})
    if not specs:
        _log("apply: непустых заявок нет — выход")
        return 0
    total_pos = sum(len(s["items"]) for s in specs)
    _log(f"apply: слитый пул из {len(specs)} заявок, позиций всего={total_pos}")
    # chain_after=False: крон — одноразовый процесс; цепочку run_now подхватит сервис/след. крон.
    pc.run_apply_pool(_deps(), specs, chain_after=False)
    for s in specs:
        row = pc.job_public(repository.victory_conn, s["job_id"])
        _log(f"apply: заявка job={s['job_id']} status={row and row.get('status')} "
             f"msg={row and row.get('message')}")
    _log(f"apply: обработано заявок={len(specs)}")
    return len(specs)


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

"""
Worker entrypoint for the Direct create-set queue (Phase 2).

Run as a dedicated systemd service (direct-create-worker.service):
    DIRECT_ROLE=worker python -m direct.worker_main

This process OWNS campaign creation. It polls Victory DB for web-posted jobs
(status='queued', body._web_posted=true), atomically claims them (queued→claimed
RETURNING), and executes create_set in its OWN memory — with the full worker pool
and every daemon (watchdog / resume / delayed-repair) plus per-job prefetch warm-up.

The web process (direct-create.service, DIRECT_ROLE=web) only POSTS jobs to the DB and READS
status/queue. Therefore restarting the UI on deploy never kills in-flight jobs — the
whole point of splitting the worker out.

SIGTERM → drain: stop claiming NEW jobs (workers exit as soon as their current item
finishes), wait until no running job or a soft deadline (< systemd TimeoutStopSec),
then exit cleanly. Any job still 'running' in the DB at exit is marked 'interrupted'
by _jobs_db_recover on the next worker start (reusing the existing restart-recovery),
so its remainder can be resumed. Nothing new to invent for crash-safety.
"""
import os
import signal
import sys
import time
from pathlib import Path

from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# load credentials from .secret/.env (same bootstrap as main.py / content_main.py)
for _p in Path(__file__).resolve().parents:
    if (_p / ".secret" / "loader.py").exists():
        sys.path.insert(0, str(_p / ".secret"))
        break

# Role must be set before queue/runtime initialization.
os.environ.setdefault("DIRECT_ROLE", "worker")

from loader import _get  # noqa: E402
from direct.core import automation_runtime as _runtime  # noqa: E402,F401
from direct.core import queue_server as _bp  # noqa: E402


_STOP = {"on": False}
_DRAIN_DEADLINE_SEC = 540   # < systemd TimeoutStopSec (600): выйти ДО SIGKILL


def create_worker_app() -> Flask:
    """Minimal request-context host for background create-set execution."""
    app = Flask(__name__)
    app.secret_key = _get("FLASK_SECRET_KEY")
    return app


def _handle_term(signum, _frame) -> None:
    print(f"[direct-worker] signal {signum} → drain: перестаём брать новые джобы", flush=True)
    _STOP["on"] = True
    try:
        _bp._worker_request_drain()   # workers: _claim_next_job() вернёт None → треды завершатся
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    role = _bp._direct_role()
    if role != "worker":
        print(f"[direct-worker] WARNING: DIRECT_ROLE={role!r} (ожидался 'worker'); "
              f"продолжаю, но БД-поллер стартует только при role=worker", flush=True)

    app = create_worker_app()             # worker needs request/session context, not the web blueprint
    _bp._worker_bootstrap(app)            # jobs_db_init + recover + watchdog + пул воркеров + демоны + БД-поллер

    n_threads = int(_bp._CREATE_WORKERS or _bp._create_workers_count())
    print(f"[direct-worker] worker started, {n_threads} threads, "
          f"poll={_bp._WORKER_POLL_SEC}s, role={role}", flush=True)

    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)

    while not _STOP["on"]:
        time.sleep(1)

    # drain: дать воркерам завершить текущий item; выйти как только in-flight=0 либо по дедлайну.
    # ВАЖНО (2026-07-08): считаем РЕАЛЬНУЮ занятость воркеров через _CREATE_ACTIVE_AGENCIES
    # (инкремент при claim blueprint.py:2356, декремент в finally воркера :2475), а НЕ
    # status=="running" в _CREATE_JOBS. status="running" перегружен — он ставится на
    # РОДИТЕЛЬСКУЮ карточку как UI-флаг фоновой добивки (delayed-repair в daemon-треде, вне
    # пула воркеров: _parent_absorb_child_start blueprint.py:1569). Из-за этого drain видел
    # фантомный running≥1 и досиживал весь дедлайн 540с при КАЖДОМ рестарте с активной
    # добивкой. Прерывание фоновой добивки безопасно by design (persist в
    # direct_delayed_repairs/deferred, running→interrupted на recover).
    t0 = time.time()
    while time.time() - t0 < _DRAIN_DEADLINE_SEC:
        try:
            running = sum(_bp._CREATE_ACTIVE_AGENCIES.values())
        except Exception:  # noqa: BLE001
            running = 0
        if running == 0:
            break
        time.sleep(2)
    print("[direct-worker] drain complete, exit", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

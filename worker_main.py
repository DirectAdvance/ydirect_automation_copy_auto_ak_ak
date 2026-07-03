"""
Worker entrypoint for the Direct create-set queue (Phase 2).

Run as a dedicated systemd service (direct-worker.service):
    DIRECT_ROLE=worker python -m direct.worker_main

This process OWNS campaign creation. It polls Victory DB for web-posted jobs
(status='queued', body._web_posted=true), atomically claims them (queued→claimed
RETURNING), and executes create_set in its OWN memory — with the full worker pool
and every daemon (watchdog / resume / delayed-repair) plus per-job prefetch warm-up.

The web process (direct.service, DIRECT_ROLE=web) only POSTS jobs to the DB and READS
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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# load credentials from .secret/.env (same bootstrap as main.py / content_main.py)
for _p in Path(__file__).resolve().parents:
    if (_p / ".secret" / "loader.py").exists():
        sys.path.insert(0, str(_p / ".secret"))
        break

# Role must be set BEFORE importing direct.main (its module-level create_app() reads it).
os.environ.setdefault("DIRECT_ROLE", "worker")

from direct import main as direct_main  # noqa: E402
from direct import blueprint as _bp      # noqa: E402


_STOP = {"on": False}
_DRAIN_DEADLINE_SEC = 540   # < systemd TimeoutStopSec (600): выйти ДО SIGKILL


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

    app = direct_main.app                 # переиспользуем уже созданный create_app() (без второй инициализации)
    _bp._worker_bootstrap(app)            # jobs_db_init + recover + watchdog + пул воркеров + демоны + БД-поллер

    n_threads = int(_bp._CREATE_WORKERS or _bp._create_workers_count())
    print(f"[direct-worker] worker started, {n_threads} threads, "
          f"poll={_bp._WORKER_POLL_SEC}s, role={role}", flush=True)

    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)

    while not _STOP["on"]:
        time.sleep(1)

    # drain: дать воркерам завершить текущий item; выйти как только running=0 либо по дедлайну.
    t0 = time.time()
    while time.time() - t0 < _DRAIN_DEADLINE_SEC:
        try:
            running = sum(1 for j in list(_bp._CREATE_JOBS.values())
                          if j.get("status") == "running")
        except Exception:  # noqa: BLE001
            running = 0
        if running == 0:
            break
        time.sleep(2)
    print("[direct-worker] drain complete, exit", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

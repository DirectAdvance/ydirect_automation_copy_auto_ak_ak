"""
Worker entrypoint для очереди правок СЛЕПКОВ (Фаза 2 разделения).

Запуск как отдельный systemd-сервис (direct-slepki-worker.service):
    DIRECT_ROLE=worker DIRECT_WORKER_SCOPE=slepki python -m direct.slepki_worker_main

Процесс ВЛАДЕЕТ применением edit-джоб слепков (kind ∈ _EDIT_KINDS: edit_keywords, edit_callouts,
save_assets, save_minus_sets, toggle_aon_aoff, add_ct_group, remove_ct_group, set_name_override).
Он поллит общую таблицу Victory direct_automation_jobs, атомарно клеймит ТОЛЬКО edit-джобы
(queued→claimed RETURNING, scope-фильтр в _worker_claim_web_jobs) и исполняет их в своей памяти
через ту же ветку _is_edit_job → _sed.handle_job воркер-цикла.

Веб (direct-slepki.service, DIRECT_ROLE=web на :5023) только КЛАДЁТ edit-джобу в БД и читает
статус. Create-worker (direct-create-worker.service) с 2026-07-17 НЕ клеймит edit-виды (его
claim/recover их исключают), поэтому:
  • деплой кода slepki_editor.py рестартит ТОЛЬКО этот процесс — очередь создания РК не тронута;
  • рестарт create-worker не трогает применение правок слепков.

SIGTERM → drain: перестать клеймить новые edit-джобы, дать текущей доработать, затем выйти.
Любая edit-джоба, оставшаяся 'running'/'claimed' на выходе, чинится _slepki_jobs_recover при
следующем старте (running→interrupted, claimed→queued). Правка атомарна (temp+os.replace), поэтому
полу-применения не бывает — файл структуры либо старый, либо новый целый.
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

# load credentials from .secret/.env (тот же bootstrap, что worker_main.py / content_main.py)
for _p in Path(__file__).resolve().parents:
    if (_p / ".secret" / "loader.py").exists():
        sys.path.insert(0, str(_p / ".secret"))
        break

# Роль и scope должны быть выставлены ДО инициализации queue/runtime (читаются на импорте).
os.environ.setdefault("DIRECT_ROLE", "worker")
os.environ.setdefault("DIRECT_WORKER_SCOPE", "slepki")

from loader import _get  # noqa: E402
from direct import automation_runtime as _runtime  # noqa: E402,F401
from direct import queue_server as _bp  # noqa: E402


_STOP = {"on": False}
_DRAIN_DEADLINE_SEC = 60    # edit-джобы применяются за секунды → короткий drain-дедлайн


def create_worker_app() -> Flask:
    """Минимальный request-context host для фонового применения edit-джоб."""
    app = Flask(__name__)
    app.secret_key = _get("FLASK_SECRET_KEY")
    return app


def _handle_term(signum, _frame) -> None:
    print(f"[direct-slepki-worker] signal {signum} → drain: перестаём брать новые edit-джобы",
          flush=True)
    _STOP["on"] = True
    try:
        _bp._worker_request_drain()
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    role = _bp._direct_role()
    scope = _bp._worker_scope()
    if role != "worker" or scope != "slepki":
        print(f"[direct-slepki-worker] WARNING: DIRECT_ROLE={role!r} DIRECT_WORKER_SCOPE={scope!r} "
              f"(ожидались 'worker'/'slepki'); поллер edit-джоб стартует только при этой паре",
              flush=True)

    app = create_worker_app()
    _bp._slepki_worker_bootstrap(app)     # jobs_db_init + slepki recover + пул воркеров + БД-поллер

    n_threads = int(_bp._CREATE_WORKERS or _bp._create_workers_count())
    print(f"[direct-slepki-worker] started, {n_threads} threads, poll={_bp._WORKER_POLL_SEC}s, "
          f"role={role}, scope={scope}", flush=True)

    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)

    while not _STOP["on"]:
        time.sleep(1)

    # drain: дать воркерам завершить текущий item; выйти как только in-flight=0 либо по дедлайну.
    t0 = time.time()
    while time.time() - t0 < _DRAIN_DEADLINE_SEC:
        try:
            running = sum(_bp._CREATE_ACTIVE_AGENCIES.values())
        except Exception:  # noqa: BLE001
            running = 0
        if running == 0:
            break
        time.sleep(1)
    print("[direct-slepki-worker] drain complete, exit", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

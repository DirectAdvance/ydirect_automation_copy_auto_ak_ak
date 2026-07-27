"""Create-set queue lifecycle, workers, watchdog and deferred repair daemons.

Owns all mutable _CREATE_* state. Persistence is delegated to job_repository; this module
never imports blueprint.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid

from flask import session

from . import repair_auto as rauto
from . import repair_gate as rgate
from . import slepki_editor as _sed
from . import write_gate as _write_gate
from .copy_engine import (
    _copy_run_job, _copy_jobs_recover, _COPY_JOBS, _COPY_JOBS_LOCK,
)
from .direct_repository import (
    victory_conn as _victory_conn,
    victory_conn_rw as _victory_conn_rw,
    victory_conn_rw_gate as _victory_conn_rw_gate,
)
from .job_repository import (
    _JOB_DB_LAST,
    _jobs_db_init, _job_db_save, _job_db_get, _job_db_active_by_login,
    _job_db_set_status, _job_control_set, _jobs_db_mark_stale_running,
    _deferred_db_init, _deferred_set_status, _deferred_bump_resume_at,
    _delayed_repair_db_init, _delayed_repair_set_status, _delayed_content_repair_save,
    _supersede_delayed_repairs_for_login, _ready_logins_db_init, _ready_login_upsert,
    _ready_login_remove,
    _next_units_reset_utc,
)
from .create_job_status import (
    terminal_status_for_job, terminal_status_for_parent_failed, compute_job_issues_breakdown,
)
from .yandex_gateway import (
    direct_tokens as _direct_tokens, token_for_login as _token_for_login,
    units_alive_for_login as _units_alive_for_login, grid_list_campaigns as _grid_list_campaigns,
)


_CREATE_JOBS: dict = {}
_CREATE_JOBS_LOCK = threading.Lock()
_CREATE_COND = threading.Condition(_CREATE_JOBS_LOCK)
_CREATE_QUEUE: list = []
_CREATE_WORKER: dict = {"started": False}
_CREATE_WATCHDOG: dict = {"started": False}
_JOB_TERMINAL = ("done", "error", "cancelled", "interrupted")
_CREATE_WORKERS = 0
_CREATE_POOL_PAUSE = 15
_CREATE_MAX_PER_AGENCY = 1
_CREATE_ACTIVE_AGENCIES: dict[str, int] = {}
_CREATE_RUNNING_TIMEOUT = int(os.environ.get("DIRECT_CREATE_RUNNING_TIMEOUT", "900"))
_CREATE_FIRST_CAMPAIGN_TIMEOUT = int(os.environ.get("DIRECT_CREATE_FIRST_CAMPAIGN_TIMEOUT", "900"))
# M3 content generation plus first-run image preupload can legitimately take >90s per campaign
# while still heartbeating. Keep a bounded overall SLA, but leave enough budget for warm caches.
_CREATE_SET_SLA_PER_CAMPAIGN_SEC = int(os.environ.get("DIRECT_CREATE_SET_SLA_PER_CAMPAIGN_SEC", "240"))
_CREATE_SET_SLA_MIN_SEC = int(os.environ.get("DIRECT_CREATE_SET_SLA_MIN_SEC", "900"))
_CREATE_FINALIZE_TIMEOUT = int(os.environ.get("DIRECT_CREATE_FINALIZE_TIMEOUT", "900"))
_CREATE_WATCHDOG_POLL = 30
_DCR_DETACH_PARENT = os.environ.get("DIRECT_DCR_DETACH_PARENT", "1") not in ("0", "false", "False", "no")
_CREATE_DRAIN = {"on": False}
_WORKER_POLLER = {"started": False}
_WORKER_POLL_SEC = 2
_JOB_MUT_LOCK = threading.Lock()
_JOB_HISTORY_TTL = 86400
_RESUME_DAEMON = {"started": False}
_RESUME_MAX = 3
_RESUME_POLL = 120
_COPY_AGENT_RETRY_DAEMON = {"started": False}
_COPY_AGENT_RETRY_POLL = int(os.environ.get("DIRECT_COPY_AGENT_RETRY_POLL", "60"))
_DEFERRED_STALE_HOURS = 3
_DELAYED_REPAIR_DAEMON = {"started": False}
_DELAYED_REPAIR_POLL = 60
# ПОВТОРНЫЙ проход добивки (reschedule :744 + поле run_after_seconds :989) = 300с (решение Семёна
# 2026-07-18: «повтор через 5 мин»). ⛔ НЕ «унифицировать» с job_repository.
# _DELAYED_CONTENT_REPAIR_DELAY_SECONDS (180с) — числа РАЗНЫЕ НАМЕРЕННО: там ПЕРВЫЙ запуск, здесь
# ПОВТОРНЫЙ (первый проход уже что-то починил, остатку нужно больше времени на оседание).
# Почему не меньше: контент оседает в кабинете 5-10+ мин (STATE.md:155-180) — ранний проход чинит
# то, что и так привяжется, и выносит ложный вердикт «не починилось».
# Демон поллит раз в 60с (_DELAYED_REPAIR_POLL) → фактическая задержка = 300с + до 60с = 300-360с.
_DELAYED_CONTENT_REPAIR_DELAY_SECONDS = 300
_DELAYED_FULL_REPAIR_MAX_ITERATIONS = 2
_DELAYED_REPAIR_STUCK_TIMEOUT_SECONDS = 1800
_DELAYED_REPAIR_TIME_BUDGET_SECONDS = 1200
_DELAYED_REPAIR_MAX_RESCHEDULES = 1
# Авто-реквью content_repair убитой watchdog'ом (Баг B 2026-07-22): max 2 попытки — если
# Баг A (IMAGE_NO_POOL) не перехватил все нечинимые случаи, кап страхует от вечного цикла.
_DELAYED_REPAIR_WATCHDOG_REQUEUE_MAX = 2
# ДЕФЕКТ-ФИКС (2026-07-20, инцидент 404c320fc32e): reconciler-маркер auto_requeue_missing БОЛЬШЕ
# не блокирует доставку навсегда одним проходом. Если loose-матч по имени ложно счёл реально
# отсутствующие позиции «живыми» (устаревшее состояние кабинета / регион-суффикс), они выпадали из
# missing и терялись молча без шанса на повтор. Теперь маркер несёт attempts и допускает до N
# свежих проходов (missing пересчитывается по ЖИВОМУ кабинету → штатно опустеет и остановит цикл
# сам; кап страхует от вечного повтора на нечинимом остатке).
_REQUEUE_MISSING_MAX_ATTEMPTS = int(os.environ.get("DIRECT_REQUEUE_MISSING_MAX_ATTEMPTS", "3"))
_CLAIMED_WATCHDOG_TS = {"t": 0.0}
# Монитор зависшей edit-очереди слепков (find #3 код-ревью): если direct-slepki-worker упал,
# edit-джобы копятся в 'queued' без исполнения. create-worker (всегда живой) это замечает и алертит.
_EDIT_STUCK_TS = {"check": 0.0, "last_alert": 0.0}
_EDIT_STUCK_AGE_SEC = 180          # edit-джоба в 'queued' дольше 3 мин = slepki-worker не разбирает
_EDIT_STUCK_ALERT_THROTTLE = 900   # Telegram-алерт не чаще раза в 15 мин
_REPAIR_NONFIXABLE_FIELD_MARKERS = (
    "UNAVAILABLE_FIELD", "UNKNOWN_FIELD", "MINUS_MARKS_FILTER_MISSING",
    "FIELD_NOT_ALLOWED", "INVALID_FIELD",
)


def _missing(*_args, **_kwargs):
    raise RuntimeError("queue_server dependency is not configured")


def configure(deps: dict) -> None:
    globals().update(deps)


_sweep_empty_drafts = _create_set_job_context = _repair_deps = _missing
_create_set_live_verification = _run_spec_audit_and_fix = _finalize_queue_module = _missing
_delete_drafts_core = _create_set_response = _auto_queue_recreate_after_done = _missing
_prefetch_start = _missing
_set_llm_heartbeat_job = _missing


def _direct_role() -> str:
    r = (os.environ.get("DIRECT_ROLE") or "all").strip().lower()
    return r if r in ("web", "worker", "all") else "all"


def _worker_scope() -> str:
    """Какие джобы обслуживает воркер-процесс (Фаза 2 разделения слепков):
      'create' (дефолт) — создание РК/докрутка/delete_drafts (direct-create-worker.service);
      'slepki'          — ТОЛЬКО edit-джобы структуры/контента слепков (direct-slepki-worker.service).
    Разделяет claim/recover ОДНОЙ общей таблицы direct_automation_jobs между двумя воркерами, чтобы
    деплой кода слепков рестартил только slepki-worker, а create-worker не трогал очередь слепков и
    наоборот (структурная изоляция, как kind<>'copy_campaigns' у direct-copy.service)."""
    s = (os.environ.get("DIRECT_WORKER_SCOPE") or "create").strip().lower()
    return s if s in ("create", "slepki") else "create"


_EDIT_KINDS_SQL: list = sorted(_sed._EDIT_KINDS)   # неизменный набор → считаем один раз на импорте


def _edit_kinds_list() -> list:
    """edit-виды джоб слепков (kind-колонка) для SQL-фильтров claim/recover (psycopg2 = ANY(%s))."""
    return _EDIT_KINDS_SQL

def _worker_request_drain() -> None:
    """SIGTERM handler в worker_main: включить drain и разбудить всех ждущих воркеров."""
    _CREATE_DRAIN["on"] = True
    try:
        with _CREATE_COND:
            _CREATE_COND.notify_all()
    except Exception:  # noqa: BLE001
        pass

def _worker_is_draining() -> bool:
    return bool(_CREATE_DRAIN.get("on"))

def _job_agency(job: dict) -> str:
    """Ключ агентства джобы — партиционирование очереди.

    api_create_set_async разрешает реальное агентство ДО постановки в очередь
    (_resolve_agency_hint: кэш БД + local_gsheet_sites, без API-вызовов к Яндексу),
    поэтому body["agency"] уже содержит физическое название агентства (не "").
    Фолбэк «» сохранён консервативно: для пустого ключа действует тот же лимит параллельности."""
    return ((job.get("body") or {}).get("agency") or "").strip().lower()

def _job_touch(job: dict | None) -> None:
    """Локальный heartbeat джобы для watchdog'а."""
    if not job:
        return
    job["_heartbeat"] = time.time()

def _bump_job(job, ok: bool = True, n: int = 1) -> None:
    """Инкремент счётчиков по ФАКТУ созданной кампании (fan-out даёт N кампаний на 1 пункт плана)."""
    if not job:
        return
    with _JOB_MUT_LOCK:
        if ok:
            job["created"] = int(job.get("created") or 0) + n
        else:
            job["failed"] = int(job.get("failed") or 0) + n
        job["_heartbeat"] = time.time()   # watchdog: прогресс по ЛЮБОЙ кампании (создание/ошибка) = живой

def _bump_item(job) -> None:
    """Инкремент set_done: вызывать ОДИН РАЗ после завершения каждого item набора (не за каждую кампанию fan-out)."""
    if not job:
        return
    with _JOB_MUT_LOCK:
        job["set_done"] = int(job.get("set_done") or 0) + 1
        job["_heartbeat"] = time.time()   # watchdog: каждый обработанный item (вкл. skip/пропуск) = живой

def _add_job_err(job, err) -> None:
    """Добавить ошибку в job['errors_log'] (лимит 100). err — строка или dict с ключом 'error'."""
    if not job:
        return
    msg = (err if isinstance(err, str)
           else (err.get("error") or "; ".join(err.get("errors") or [])))
    if not msg:
        return
    log = job.setdefault("errors_log", [])
    log.append(str(msg)[:300])
    if len(log) > 100:
        del log[:-100]

def _jobs_db_recover() -> None:
    """При старте сервиса: поднять недавние джобы в память для ПРОСМОТРА; незавершённые
    (queued/running) пометить 'interrupted' — worker-очередь после рестарта пуста, авто-докрутку
    не делаем (защита от дублей: повторный клик «Создать» сам пропустит уже созданные через set_plan)."""
    _interrupted_logins: list = []
    _deferred_db_init()                                  # таблица остатков должна существовать до UPDATE ниже
    _delayed_repair_db_init()
    try:
        import psycopg2.extras
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            # ЛОГИНЫ прерванных джоб — для авто-очистки их пустышек (кампания создана, рестарт убил
            # процесс до наполнения групп → 0 групп). Берём ДО UPDATE, пока статус ещё running/queued.
            # kind='copy_campaigns' исключаем: эти джобы принадлежат отдельному процессу
            # direct-copy.service — их recover/sweep не наш (иначе затрём чужой статус и снесём
            # свежесозданные черновики копирования). См. _ensure_copy_worker/_copy_jobs_recover.
            # kind-гарды: copy_campaigns принадлежит direct-copy.service; edit-виды слепков —
            # direct-slepki-worker.service (Фаза 2). create-recover НЕ трогает ни то, ни другое:
            # иначе (а) затрём чужой статус, (б) синтетический login='slepki-edit' попал бы в
            # sweep пустых черновиков (он не реальный аккаунт Директа).
            _ek = _edit_kinds_list()
            cur.execute("SELECT DISTINCT login FROM public.direct_automation_jobs "
                        "WHERE status IN ('queued','running') AND login IS NOT NULL "
                        "  AND coalesce(kind,'') <> 'copy_campaigns' "
                        "  AND NOT (coalesce(kind,'') = ANY(%s)) "
                        "  AND updated_at > now() - interval '6 hours'", (_ek,))
            _interrupted_logins = [r["login"] for r in cur.fetchall() if r.get("login")]
            # битые running/queued → interrupted (single UPDATE).
            # ВАЖНО: web-posted queued-джобы (_web_posted=true) НЕ трогаем — их ещё не начинал
            # исполнять ни один воркер, они ждут клейма поллером. Пометив их interrupted, мы бы
            # потеряли постановку сразу после рестарта воркера. Гасим только «свои» in-memory queued
            # (их в БД пишет _job_new всех ролей кроме web) и любые running.
            cur.execute("UPDATE public.direct_automation_jobs SET status='interrupted', updated_at=now() "
                        "WHERE (status='running' "
                        "       OR (status='queued' AND coalesce(body->>'_web_posted','') <> 'true')) "
                        "  AND coalesce(kind,'') <> 'copy_campaigns' "
                        "  AND NOT (coalesce(kind,'') = ANY(%s))", (_ek,))
            # 'claimed' — web-posted джоба, которую поллер забрал из БД, но воркер упал ДО того, как
            # завёл её в in-memory очередь (окно миллисекунды). body ещё содержит items+session →
            # безопасно вернуть в 'queued' для повторного клейма (дубля нет: set_plan пропустит созданное).
            # kind-гард как у соседних стейтментов: recover создания НЕ трогает чужую очередь
            # копирования (иначе рестарт create-воркера вернул бы claimed copy-джобу в queued).
            cur.execute("UPDATE public.direct_automation_jobs SET status='queued', updated_at=now() "
                        "WHERE status='claimed' "
                        "  AND coalesce(kind,'') <> 'copy_campaigns' "
                        "  AND NOT (coalesce(kind,'') = ANY(%s))", (_ek,))
            # CRASH-SAFETY ОСТАТКОВ: 'resumed'-остаток (докрутка по куке поставлена в очередь), который
            # завис дольше N часов без финала — джоба умерла при рестарте, остаток осиротел. Возвращаем
            # в waiting+resume_at=now(), чтобы демон подхватил его ПО КУКЕ. Дубля нет: set_plan пропустит
            # уже созданные кампании; финал докрутки пометит строку done (не зациклится на рестартах).
            # resume_count += 1 + кап < _RESUME_MAX: «ядовитый» набор (всегда падает) не перезапускается
            # бесконечно при каждом рестарте — после _RESUME_MAX оживлений остаётся 'resumed' (брошен).
            cur.execute("UPDATE public.direct_deferred_creates "
                        "SET status='waiting', resume_at=now(), updated_at=now(), "
                        "    resume_count = resume_count + 1 "
                        "WHERE status='resumed' AND updated_at < now() - make_interval(hours => %s) "
                        "  AND COALESCE(resume_count,0) < %s",
                        (int(_DEFERRED_STALE_HOURS), int(_RESUME_MAX)))
            # СТАРУЮ историю не храним: завершённые джобы старше TTL — удаляем сразу при старте.
            cur.execute("DELETE FROM public.direct_automation_jobs WHERE status = ANY(%s) "
                        "AND updated_at < now() - make_interval(secs => %s)",
                        (list(_JOB_TERMINAL), _JOB_HISTORY_TTL))
            conn.commit()
            # поднять только СВЕЖИЕ джобы (активные + завершённые за последние TTL) — историю не копим
            cur.execute("SELECT * FROM public.direct_automation_jobs "
                        "WHERE status NOT IN ('done','error','cancelled','interrupted') "
                        "   OR updated_at > now() - make_interval(secs => %s) "
                        "ORDER BY updated_at DESC LIMIT 50", (_JOB_HISTORY_TTL,))
            for r in cur.fetchall():
                jid = r["job_id"]
                if jid in _CREATE_JOBS:
                    continue
                # finished_at терминальной джобы = когда она реально завершилась (из updated_at),
                # чтобы карточка ушла ровно через TTL после завершения, а не после рестарта.
                fin = None
                if r["status"] in _JOB_TERMINAL:
                    try:
                        fin = r["updated_at"].timestamp()
                    except Exception:  # noqa: BLE001
                        fin = time.time()
                # body/agency восстанавливаем из БД — нужны для resume прерванных джоб
                saved_body = r.get("body")   # psycopg2 RealDictCursor уже десериализует jsonb → dict
                _CREATE_JOBS[jid] = {"status": r["status"], "login": r.get("login"),
                                     "done": r.get("done") or 0, "total": r.get("total") or 0,
                                     "created": r.get("created") or 0, "failed": r.get("failed") or 0,
                                     "result": r.get("result"), "error": r.get("error"),
                                     "cancel": False, "kind": r.get("kind"),
                                     "publish": bool(r.get("publish")), "_id": jid,
                                     "finished_at": fin, "body": saved_body,
                                     "agency": r.get("agency"),
                                     "session": None,
                                     "step": None, "stream_content": False}   # step/stream не хранятся в БД
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass
    # АВТО-ОЧИСТКА ПУСТЫШЕК: для аккаунтов прерванных джоб удаляем пустые ЕПК-черновики (0 групп —
    # кампания создалась, но рестарт убил сборку). В фоне (не блокируем старт) и ТОЛЬКО при старте,
    # когда активного создания ещё нет (гонок с наполнением групп нет). По куке, без баллов.
    if _interrupted_logins:
        # Собрать прерванные джобы для reconciler — _CREATE_JOBS уже заполнен SELECT'ом выше.
        # Исключаем requeue-джобы (_requeue_of != '') — они сами по себе уже доставка,
        # внучки не ставим (gate внутри _requeue_missing_positions_once тоже проверяет это).
        _interrupted_jobs: list = []  # [(job_id, login, body), ...]
        _interrupted_login_set = set(_interrupted_logins)
        with _CREATE_JOBS_LOCK:
            for _jid, _jdata in _CREATE_JOBS.items():
                if (_jdata.get("status") == "interrupted"
                        and _jdata.get("login") in _interrupted_login_set):
                    _ijbody = _jdata.get("body") or {}
                    if (_ijbody.get("items")                         # есть позиции для доставки
                            and not str(_ijbody.get("_requeue_of") or "").strip()):  # не сама доставка
                        _interrupted_jobs.append((_jid, str(_jdata["login"]), _ijbody))

        def _bg_sweep(logins, interrupted_jobs):
            time.sleep(8)                                # дать сервису и воркеру подняться
            for lg in logins:
                try:
                    n = _sweep_empty_drafts(lg)
                    if n:
                        print(f"[startup-sweep] {lg}: удалено пустых ЕПК-черновиков: {n}", flush=True)
                except Exception:  # noqa: BLE001
                    pass
            # RECONCILER: после сноса пустышек сверяем план vs. кабинет для каждой прерванной
            # джобы и доставляем недостающие позиции повторной джобой. Гейты внутри
            # _requeue_missing_positions_once: (1) _requeue_of → без внучек;
            # (2) auto_requeue_missing → без дублей при повторных рестартах;
            # (3) _job_db_active_by_login → не конкурируем с текущей активной джобой логина.
            # Порядок важен: sweep сначала (пустые UAC-оболочки удалены), тогда Grid покажет
            # реальное отсутствие позиций, которые были удалены до обрыва.
            time.sleep(5)                                # Grid: пауза после sweep для стабилизации
            for job_id, lg, body in interrupted_jobs:
                try:
                    # Доставка может поставить ДВЕ джобы (cookie-часть + units-часть) — логируем
                    # все, иначе в следе рестарта видна только первая (ревью ретрая, находка Б2).
                    new_jids = _requeue_missing_positions_once(job_id, lg, body) or []
                    if new_jids:
                        print(f"[startup-reconcile] {lg}: восстановление прерванных позиций "
                              f"→ джобы {','.join(new_jids)} (родитель {job_id})", flush=True)
                except Exception:  # noqa: BLE001
                    pass
        threading.Thread(target=_bg_sweep, args=(list(_interrupted_logins), _interrupted_jobs), daemon=True).start()

def _jobs_purge_old() -> None:
    """Удалить завершённые джобы старше TTL — из памяти и из БД. Историю не храним (по требованию)."""
    now = time.time()
    with _CREATE_JOBS_LOCK:
        stale = [k for k, v in _CREATE_JOBS.items()
                 if v.get("status") in _JOB_TERMINAL
                 and (now - (v.get("finished_at") or 0)) > _JOB_HISTORY_TTL]
        for k in stale:
            _CREATE_JOBS.pop(k, None)
            _JOB_DB_LAST.pop(k, None)
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM public.direct_automation_jobs WHERE status = ANY(%s) "
                        "AND updated_at < now() - make_interval(secs => %s)",
                        (list(_JOB_TERMINAL), _JOB_HISTORY_TTL))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass

def _create_watchdog_tick() -> None:
    """Одиночный проход watchdog: локальные зависшие running-джобы и stale running в БД."""
    timed_out: list[tuple[str, dict]] = []
    finalize_stuck: list[tuple[str, dict]] = []   # done>=total + зависшая финализация → done + delayed repair
    now = time.time()
    with _CREATE_COND:
        for jid, job in list(_CREATE_JOBS.items()):
            if job.get("status") != "running":
                continue
            heartbeat = max(float(job.get("_heartbeat") or 0), float(job.get("started_at") or 0))
            if not heartbeat:
                continue
            _stuck = now - heartbeat
            _started = float(job.get("started_at") or 0)
            _kind = str(job.get("kind") or "")
            _total = int(job.get("total") or 0)
            if (_started
                    and _kind in ("set", "slepok")
                    and _total > 0
                    and int(job.get("done") or 0) < _total):
                _sla = max(_CREATE_SET_SLA_MIN_SEC, _total * _CREATE_SET_SLA_PER_CAMPAIGN_SEC)
                if (now - _started) > _sla:
                    job["status"] = "error"
                    job["error"] = (
                        "watchdog: create-set превысил SLA "
                        f"{int(_sla // 60)} мин ({_total} кампаний × "
                        f"{int(_CREATE_SET_SLA_PER_CAMPAIGN_SEC)}с)"
                    )
                    job["result"] = {"error": job["error"], "sla_timeout": True}
                    job["finished_at"] = now
                    job["_watchdog_done"] = True
                    job["cancel"] = True
                    timed_out.append((jid, dict(job)))
                    agency = _job_agency(job)
                    active = max(0, int(_CREATE_ACTIVE_AGENCIES.get(agency, 0)) - 1)
                    if active:
                        _CREATE_ACTIVE_AGENCIES[agency] = active
                    else:
                        _CREATE_ACTIVE_AGENCIES.pop(agency, None)
                    _agency_gate_release(agency, jid)
                    continue
            if (_started
                    and _kind in ("set", "slepok")
                    and int(job.get("created") or 0) <= 0
                    and (now - _started) > _CREATE_FIRST_CAMPAIGN_TIMEOUT):
                job["status"] = "error"
                job["error"] = (
                    "watchdog: за "
                    f"{int(_CREATE_FIRST_CAMPAIGN_TIMEOUT // 60)} мин не создана ни одна кампания"
                )
                job["result"] = {"error": job["error"], "first_campaign_timeout": True}
                job["finished_at"] = now
                job["_watchdog_done"] = True
                job["cancel"] = True
                timed_out.append((jid, dict(job)))
                agency = _job_agency(job)
                active = max(0, int(_CREATE_ACTIVE_AGENCIES.get(agency, 0)) - 1)
                if active:
                    _CREATE_ACTIVE_AGENCIES[agency] = active
                else:
                    _CREATE_ACTIVE_AGENCIES.pop(agency, None)
                _agency_gate_release(agency, jid)
                continue
            # done>=total — цикл создания завершён; heartbeat тикает на каждый обработанный item
            # (_bump_item/_bump_job), при done>=total он заморожен на последнем item → _stuck растёт
            # ровно на время фазы ФИНАЛИЗАЦИИ. Раньше эта фаза была БЕЗУСЛОВНО освобождена от
            # watchdog'а → любой зависший сетевой read / лок / мёртвая Victory-DB в promo/postprocess/
            # build_response вешал джобу running НАВСЕГДА (job e05fbc86e8ca, done=14/14, >33мин).
            # Теперь — свой КОНЕЧНЫЙ бюджет _CREATE_FINALIZE_TIMEOUT (> postprocess-бюджета 600с): на
            # куки-бэкфилле массовый skip укладывается в него, а реальный фриз финализации терминируется.
            if int(job.get("done") or 0) >= int(job.get("total") or 0) > 0:
                # Активные дочерние добивки (dcr: delayed content_repair / fin: finalize) держат
                # родителя running с done>=total ЛЕГИТИМНО (absorb_child_start → status=running) —
                # ими управляют K1/F watchdog'и, НЕ этот финализ-таймаут. Не убиваем их досрочно.
                _res_now = job.get("result")
                if isinstance(_res_now, dict) and _res_now.get("_active_children"):
                    continue
                if _stuck <= _CREATE_FINALIZE_TIMEOUT:
                    continue
                # Финализация зависла > бюджета → освобождаем воркер/слот. Кампании УЖЕ созданы
                # (done>=total) → терминал = done (не error); добивку контента подхватит delayed
                # content_repair (ставим ниже best-effort, т.к. done-блок осиротевшего воркера мог
                # не отработать). Орфан-поток дожмёт свой bounded-таймаут (HTTP ≤180с/DB ≤120с) и
                # при пробуждении увидит _watchdog_done → не перепишет статус.
                job["status"] = "done"
                job["error"] = None
                _res = job.get("result") if isinstance(job.get("result"), dict) else {}
                _res["finalize_timeboxed"] = {
                    "stuck_seconds": int(_stuck),
                    "budget_seconds": int(_CREATE_FINALIZE_TIMEOUT),
                    "note": ("финализация набора зависла > бюджета — воркер освобождён watchdog'ом; "
                             "созданные кампании целы, добивку подхватит delayed content_repair"),
                }
                job["result"] = _res
                job["finished_at"] = now
                job["_watchdog_done"] = True
                job["cancel"] = True
                snap = dict(job)
                timed_out.append((jid, snap))
                finalize_stuck.append((jid, snap))
                agency = _job_agency(job)
                active = max(0, int(_CREATE_ACTIVE_AGENCIES.get(agency, 0)) - 1)
                if active:
                    _CREATE_ACTIVE_AGENCIES[agency] = active
                else:
                    _CREATE_ACTIVE_AGENCIES.pop(agency, None)
                _agency_gate_release(agency, jid)
                continue
            if _stuck <= _CREATE_RUNNING_TIMEOUT:
                continue
            job["status"] = "error"
            job["error"] = f"watchdog: running без прогресса > {int(_CREATE_RUNNING_TIMEOUT // 60)} мин"
            job["result"] = {"error": job["error"]}
            job["finished_at"] = now
            job["_watchdog_done"] = True
            job["cancel"] = True
            timed_out.append((jid, dict(job)))
            agency = _job_agency(job)
            active = max(0, int(_CREATE_ACTIVE_AGENCIES.get(agency, 0)) - 1)
            if active:
                _CREATE_ACTIVE_AGENCIES[agency] = active
            else:
                _CREATE_ACTIVE_AGENCIES.pop(agency, None)
            _agency_gate_release(agency, jid)             # кросс-процессный слот (watchdog-таймаут)
        # Проверка awaiting_feed_decision: дедлайн истёк — запускаем без фида
        _expired_feed_awaiting = False
        for jid, job in list(_CREATE_JOBS.items()):
            if job.get("status") != "awaiting_feed_decision":
                continue
            _dl = float(job.get("feed_deadline") or 0)
            if not _dl or now <= _dl:
                continue
            _body = job.get("body") or {}
            _body["_skip_feed_types"] = ["product"]   # master (tp6) фид не требует — не скипать (Семён 2026-07-11)
            job["status"] = "queued"
            _CREATE_QUEUE.append(jid)
            _expired_feed_awaiting = True
        if timed_out or _expired_feed_awaiting:
            _CREATE_COND.notify_all()
    if timed_out:
        # Диагностика зависаний (2026-07-02): watchdog убивает джобу, но БЕЗ стека виновника
        # причину не найти (jobs 9126bf12fb3a/ac6d98864aa4 — «тишина 24 мин»). Дампим стеки ВСЕХ
        # тредов в /tmp — файл переживает джобу, py-spy пост-фактум уже бесполезен (тред вернулся в пул).
        try:
            import faulthandler
            _tr_path = f"/tmp/direct_stall_{int(now)}.trace"
            with open(_tr_path, "w") as _fh:
                _fh.write(f"watchdog kill: {[j for j, _ in timed_out]} at {time.ctime(now)}\n\n")
                faulthandler.dump_traceback(file=_fh, all_threads=True)
            import logging as _lg
            _lg.getLogger("direct.watchdog").warning(
                "watchdog kill %s — стеки тредов: %s", [j for j, _ in timed_out], _tr_path)
        except Exception:  # noqa: BLE001
            pass
    for jid, snap in timed_out:
        _job_db_save(jid, snap, full=True)
    # Финализ-стак: воркер осиротел на зависшей финализации → его done-блок (delayed content_repair,
    # blueprint:_create_set_response worker) мог не отработать (поток может так и не разблокироваться).
    # Планируем добивку контента здесь best-effort ВНЕ _CREATE_COND (schedule берёт _CREATE_JOBS_LOCK).
    # Идемпотентно: _delayed_content_repair_save дедупит по parent_job_id, absorb_child — по child_jid;
    # если проснувшийся воркер тоже вызовет _schedule (видит status=done) — повтор безвреден.
    for jid, snap in finalize_stuck:
        try:
            _schedule_delayed_content_repair_after_done(jid, snap)
        except Exception:  # noqa: BLE001 — watchdog не должен падать на постановке добивки
            pass
    _jobs_db_mark_stale_running(_CREATE_RUNNING_TIMEOUT)
    _agency_gate_sweep()                                  # освободить слоты агентств крашнутых/терминальных джоб

def _create_watchdog_loop() -> None:
    while True:
        try:
            _create_watchdog_tick()
            _jobs_purge_old()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(_CREATE_WATCHDOG_POLL)

def _ensure_create_watchdog() -> None:
    with _CREATE_JOBS_LOCK:
        if _CREATE_WATCHDOG["started"]:
            return
        _CREATE_WATCHDOG["started"] = True
    threading.Thread(target=_create_watchdog_loop, daemon=True).start()

def _repair_failures_nonfixable(failed_actions: list) -> bool:
    """True если ВСЕ провалившиеся in-place действия несут field-ошибку Grid (нечинимо для этой
    схемы/фида) — тогда повторять/перепланировать бессмысленно. Пусто → False (нечего оценивать).

    Проверяем ТОЛЬКО структурированные коды ошибок (validationResult.errors[].code и
    extensions.code из top-level errors) — НЕ весь сериализованный blob. Если структура
    ошибки неоднородна (plain exception-строка, нет dict-result) — считаем fixable (ретраить
    безопаснее, чем ошибочно бросить)."""
    if not failed_actions:
        return False
    for fa in failed_actions:
        result = fa.get("result") if isinstance(fa, dict) else None
        if not isinstance(result, dict):
            # Структура неизвестна (plain-exception или нет result) → считаем fixable
            return False
        # IMAGE_NO_POOL (Баг A 2026-07-22): структурный контент-гэп картинок — нет пула для ct.
        # Не ретраебл-ошибка Grid/сети (те → upload_fail_cts, image_no_pool не выставляется).
        # Аналог VIDEO_NO_POOL у видео: нечинимо in-place, повтор бессмыслен.
        if result.get("image_no_pool"):
            continue  # nonfixable — продолжаем проверку оставшихся actions
        # Собираем коды ошибок из двух источников:
        # 1) top-level errors[].extensions.code (транспортные/авторизационные ошибки Grid)
        # 2) validationResult.errors[].code (валидационные ошибки схемы/фида)
        codes: list[str] = []
        for e in (result.get("errors") or []):
            c = (e.get("extensions") or {}).get("code") if isinstance(e, dict) else None
            if c:
                codes.append(str(c).upper())
        for e in (result.get("validationResult") or {}).get("errors") or []:
            c = e.get("code") if isinstance(e, dict) else None
            if c:
                codes.append(str(c).upper())
        if not codes:
            # Нет структурированных кодов → неизвестная ошибка, считаем fixable
            return False
        if not any(any(m in code for m in _REPAIR_NONFIXABLE_FIELD_MARKERS) for code in codes):
            return False   # хотя бы один код НЕ field-ошибка → возможно чинимо
    return True

def _ready_logins_track(jid: str, job: dict) -> None:
    """Хук финализации воркера: пополнить/убрать логин в реестре «Готовые логины»."""
    try:
        if (job or {}).get("status") != "done":
            return
        login = (job.get("login") or "").strip()
        kind = job.get("kind") or ""
        body = job.get("body") or {}
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except Exception:  # noqa: BLE001
                body = {}
        if kind == "delete_drafts":
            _ready_login_remove(login)
            return
        if kind not in ("set", "slepok"):
            return
        created = int(job.get("created") or 0)
        if created <= 0:
            return
        _ready_logins_db_init()
        result = rgate.dict_from_jsonish(job.get("result")) or {}
        src = str(result.get("content_source") or body.get("content_source") or "").strip()
        content_label = ("М3/слепок" if src in ("slepok_library", "m3", "slepok")
                         else ("OpenRouter" if body.get("stream_content") else (src or "М3")))
        _ready_login_upsert(
            login,
            campaigns=created,
            slepok=str(body.get("agent") or "").strip(),
            content_source=content_label,
            elapsed_seconds=int(result.get("elapsed_seconds") or job.get("elapsed") or 0),
            add=bool(str(body.get("_requeue_of") or "").strip()),   # доставка → прибавляем
        )
    except Exception:  # noqa: BLE001
        pass

def _requeue_missing_positions_once(parent_job_id: str, login: str, body: dict) -> str | None:
    """Доставить потерянные позиции набора повторной create-джобой (ОДИН раз на родителя).

    Гейты: (1) у родителя failed>0 или created<total; (2) в result родителя ещё нет маркера
    auto_requeue_missing; (3) сама джоба-доставка (_requeue_of в body) внучек не ставит.
    Тело — то же (без runtime-ключей): RESUME-SKIP в оркестраторе пропустит уже созданные.
    Возвращает СПИСОК job_id поставленных джоб (их может быть две — cookie-часть и units-часть,
    см. разделение ниже) или None, если доставка не ставилась."""
    try:
        if str((body or {}).get("_requeue_of") or "").strip():
            return None                              # джоба-доставка → без внучек
        pj = _job_db_get(parent_job_id) or {}
        if not pj:
            return None
        total = int(pj.get("total") or 0)
        created = int(pj.get("created") or 0)
        failed = int(pj.get("failed") or 0)
        if failed <= 0 and created >= total:
            return None                              # состав полный — доставка не нужна
        p_res = rgate.dict_from_jsonish(pj.get("result"))
        if not isinstance(p_res, dict):
            p_res = {}
        _arm = p_res.get("auto_requeue_missing")
        if isinstance(_arm, dict):
            # One-shot маркер больше не блокирует НАВСЕГДА: предыдущая доставка могла НЕ покрыть
            # план целиком (loose-матч false-positive исключил реально отсутствующие позиции из
            # missing — инцидент 404c320fc32e: 4 позиции потеряны молча). Пропускаем на свежий
            # проход, пока не исчерпан кап: ниже missing пересчитывается по ЖИВОМУ кабинету, и
            # `if not missing: return None` штатно останавливает цикл, когда всё реально создано
            # (нормальный успешный кейс НЕ зацикливается — missing пуст). Кап — от вечного повтора
            # на нечинимом остатке. Дубли в окне in-flight отсекает _job_db_active_by_login (:620).
            if int(_arm.get("attempts") or 1) >= _REQUEUE_MISSING_MAX_ATTEMPTS:
                return None                          # исчерпан кап попыток доставки
        elif _arm:
            return None                              # старый формат маркера (не-dict) — как раньше
        rbody = {k: v for k, v in dict(body or {}).items()
                 if not str(k).startswith("_")
                 and k not in ("feed_alert", "feed_confirmed", "status", "result", "error")}
        # ДЕФЕКТ-ФИКС (2026-07-20, инцидент 404c320fc32e): feed_confirmed стрипается как транзиентный
        # UI-флаг awaiting_feed_decision, но он же несёт ПОДТВЕРЖДЕНИЕ фолбэк-фида, которое читает
        # tp5/tp3-гейт билда (_resolve_single_feed_variants → create_set_feed_builders.py:926) и
        # plan-гейт (create_set_plan.py:474). Без него requeue-ребёнок без профильного фида
        # (/yandex.xml|/yandex-used-auto.xml) молча роняет ВСЕ tp5 («single_feed: целевой фид не
        # найден»). Транслируем в single_feed_fallback — durable plan-ключ (НЕ стрипается,
        # принимается обоими гейтами), при этом feed_alert/feed_confirmed остаются вырезаны →
        # ребёнок не входит в awaiting_feed_decision заново.
        if (body or {}).get("feed_confirmed") or (body or {}).get("single_feed_fallback"):
            rbody["single_feed_fallback"] = True
        items = rbody.get("items") or []
        if not items:
            return None
        # ТОЛЬКО реально отсутствующие позиции (сверка по кабинету): полное тело создало бы
        # ДУБЛИ tp6/tp7 — их live-имена переименованы UAC и RESUME-SKIP их не матчит
        # (живой кейс: «ТК_AT_tcpa …_v02» дубли). Grid недоступен → НЕ доставляем (риск дублей).
        rows = _grid_list_campaigns(login) or []
        names = {str(r.get("name") or "").strip() for r in rows if r.get("name")}
        if not names:
            return None
        missing = [it for it in items
                   if not _position_live_in_names(str((it or {}).get("name") or ""), names)]
        if not missing:
            return None                              # состав фактически полный — доставка не нужна
        rbody["items"] = missing
        rbody["_requeue_of"] = parent_job_id
        # Требование Семёна: ретрай по ошибкам — ТОЛЬКО по кукам (0 баллов). Раньше тело копировалось
        # от родителя как есть (via_cookie=false) → доставка жгла баллы через v501.
        # ИСКЛЮЧЕНИЕ: сегментный tp5 кукой физически НЕ создаётся — cookie-путь не принимает segment и
        # молча лепит generic ct0000-группу, поэтому в create_set_gallery.py:82-109 он захардкожен в
        # явный провал NO_BRAND_SEGMENTS_AVAILABLE.
        # РАЗДЕЛЕНИЕ (решение Семёна 2026-07-18): флаг via_cookie — SET-уровневый (create_set_input.py:154
        # → orchestrator:168, per-item транспорта нет), поэтому один смешанный набор гнал бы за баллы
        # ВЕСЬ состав из-за одной сегментной tp5. Ставим ДВЕ джобы: cookie-capable → via_cookie=True
        # (0 баллов), сегментные tp5 → прежний (унаследованный от родителя) транспорт. Пустой набор —
        # джобу не создаём. Обе несут один _requeue_of → учёт родителя мульти-child-безопасен
        # (_resume_children — dict по child_jid, _active_children — список; родитель станет терминальным
        # только когда закроются ОБЕ). Гонки за аккаунт нет: _CREATE_MAX_PER_AGENCY=1 + кросс-процессный
        # _agency_gate_claim (UNIQUE по agency) → сёстры одного агентства исполняются строго по очереди.
        _units_items = [it for it in missing if _position_needs_units(it)]
        _cookie_items = [it for it in missing if not _position_needs_units(it)]
        # Активная джоба логина? Тогда доставку НЕ ставим и маркер НЕ сжигаем (ревью 06.07:
        # dedup_login=True возвращал ЧУЖОЙ job_id, rbody выбрасывался, а одноразовый маркер
        # auto_requeue_missing сгорал → позиции не доставлялись никогда). Доставим на следующем
        # финале delayed-repair, когда логин освободится.
        if _job_db_active_by_login(login):
            print(f"[requeue-missing] {login}: у логина активная джоба — доставка отложена, "
                  f"маркер не проставлен", flush=True)
            return None
        new_jids = []
        _parts = [(p, vc) for p, vc in ((_cookie_items, True), (_units_items, False)) if p]
        for _part, _via_cookie in _parts:
            jbody = dict(rbody)
            jbody["items"] = _part
            if _via_cookie:
                jbody["via_cookie"] = True
            # else: via_cookie НЕ трогаем — остаётся унаследованное от родителя значение (прежний
            # транспорт), кука для сегментного tp5 гарантированно уронила бы позиции.
            # доставка остатка = добивка → приоритет (Семён 2026-07-06: сразу, не в конец очереди)
            _jid = _job_new_web(len(_part), login, jbody, {}, False, priority=True)
            if not _jid:
                continue
            new_jids.append(_jid)
            print(f"[requeue-missing] {login}: джоба {_jid} — {len(_part)} позиц., "
                  f"транспорт={'кука (0 баллов)' if _via_cookie else 'прежний (баллы, сегментный tp5)'}",
                  flush=True)
        if len(new_jids) < len(_parts):
            # Часть джоб не создалась (_job_new_web вернул None). Маркер auto_requeue_missing
            # ОДНОРАЗОВЫЙ (гейт :577) — проставив его сейчас, мы бы навсегда закрыли доставку
            # непоставленной части: cookie-позиции потерялись бы, хотя units-сестра ушла (ревью
            # ретрая, находка Б1). Не ставим → следующий финал dcr повторит попытку; уже
            # созданные позиции к тому моменту будут живы в кабинете и из missing выпадут.
            print(f"[requeue-missing] {login}: создано {len(new_jids)} джоб из {len(_parts)} — "
                  f"маркер НЕ проставлен, доставка будет повторена", flush=True)
            return None
        if not new_jids:
            return None
        _prev_attempts = (int((_arm or {}).get("attempts") or 0)
                          if isinstance(_arm, dict) else 0)
        p_res["auto_requeue_missing"] = {"job_id": new_jids[0], "job_ids": new_jids,
                                         "was_created": created,
                                         "was_failed": failed, "total": total,
                                         "attempts": _prev_attempts + 1,
                                         "last_missing": len(missing)}
        pj["result"] = p_res
        _job_db_save(parent_job_id, pj, full=True)
        with _CREATE_JOBS_LOCK:
            mem = _CREATE_JOBS.get(parent_job_id)
            if mem is not None and isinstance(mem.get("result"), dict):
                mem["result"]["auto_requeue_missing"] = p_res["auto_requeue_missing"]
        print(f"[requeue-missing] {login}: доставка недостающих позиций джобами {','.join(new_jids)} "
              f"(родитель {parent_job_id}: created={created}/{total}, failed={failed})", flush=True)
        return new_jids
    except Exception:  # noqa: BLE001 — доставка best-effort
        return None

def _position_needs_units(it: dict) -> bool:
    """Позиция, которую cookie-путь создать НЕ может → нужен API-токен/баллы.

    Единственный такой класс — сегментный tp5: `_create_shopping_via_cookie` не принимает segment
    и создаёт одну generic ct0000-группу на ВСЕ сегменты (инцидент 2026-07-06: 5 одинаковых tp5
    porg-psm5h7q6), поэтому cookie-путь для него захардкожен в явный NO_BRAND_SEGMENTS_AVAILABLE
    (create_set_gallery.py:82-109). Признак сегментности — тот же, что там: tp5_segment (сегментный
    путь) либо only_gks/only_cts (camp_names-путь). tp5 «Фиды»/products_only сегмента не имеет →
    кукой создаётся штатно."""
    it = it or {}
    if str(it.get("tp") or "").strip() != "tp5":
        return False
    return bool(it.get("tp5_segment") or it.get("only_gks") or it.get("only_cts"))

def _position_live_in_names(nm: str, names: set) -> bool:
    """Позиция плана жива? already_in_direct + UAC-нормализация: tp6/tp7 при создании
    переименовывают фид-суффикс («…site/yandex.xml» → «…site — yandex»), поэтому полный
    item-name не матчится — пробуем без последнего « — сегмента» (только для tp6/tp7 с
    ≥2 сепараторами, иначе усечение до 'tp1_cpc_site' сматчило бы ЛЮБУЮ tp1)."""
    from .create_set_resume import already_in_direct
    nm = (nm or "").strip()
    if not nm:
        return True
    if already_in_direct(nm, names):
        return True
    if nm.startswith(("tp6_", "tp7_")) and nm.count(" — ") >= 2:
        base = nm.rsplit(" — ", 1)[0].strip()
        if base and already_in_direct(base, names):
            return True
    return False

def _plan_positions_all_live(login: str, body: dict) -> bool | None:
    """Каждая позиция плана имеет живую кампанию в кабинете? (префикс-матч как RESUME-SKIP).
    None — Grid недоступен (консервативно: НЕ реконсилировать). Live-сверка результатов слепа
    к НЕсозданным позициям (видит только results) — этот чек закрывает дыру."""
    try:
        rows = _grid_list_campaigns(login) or []
        names = {str(r.get("name") or "").strip() for r in rows if r.get("name")}
        if not names:
            return None
        for it in ((body or {}).get("items") or []):
            if not _position_live_in_names(str(it.get("name") or ""), names):
                return False
        return True
    except Exception:  # noqa: BLE001
        return None

def _reconcile_parent_job_counters(parent_job_id: str, last_live: dict, last_summ: dict,
                                   *, login: str = "", body: dict | None = None) -> bool:
    """После УСПЕШНОЙ добивки карточка не должна показывать «создано N · ❌ M», если ошибок
    реально нет (требование Семёна 2026-07-05). СТРОГО: обновляем счётчики ТОЛЬКО когда
    live-сверка по кабинету дала errors=0, очередь пересоздания пуста И (при переданных
    login+body) КАЖДАЯ позиция плана жива в кабинете — иначе не трогаем
    (честность важнее красивой карточки). failed→0, created→total, пометка в result."""
    try:
        live_errors = int(((last_live or {}).get("summary") or {}).get("errors") or 0)
        queued_rec = int((last_summ or {}).get("queued_recreate_items") or 0)
        if live_errors > 0 or queued_rec > 0:
            return False
        if login and body:
            if _plan_positions_all_live(login, body) is not True:
                return False                   # позиция плана отсутствует в кабинете / Grid недоступен
        job = _job_db_get(parent_job_id) or {}
        if not job:
            return False
        total = int(job.get("total") or 0)
        created = int(job.get("created") or 0)
        failed = int(job.get("failed") or 0)
        result = rgate.dict_from_jsonish(job.get("result"))
        if not isinstance(result, dict):
            result = {}
        live_clean = live_errors == 0 and (last_live or {}).get("status") in ("pass", "warn")
        if live_clean:
            result["live_verification"] = last_live
            result["live_verification_reconciled_by_repair"] = {
                "note": "post-repair live-сверка заменила stale-снимок parent job",
                "status": (last_live or {}).get("status"),
                "live_errors": live_errors,
            }
        if failed <= 0 and created >= total:
            job["result"] = result
            _job_db_save(parent_job_id, job, full=True)
            with _CREATE_JOBS_LOCK:
                mem = _CREATE_JOBS.get(parent_job_id)
                if mem is not None and isinstance(mem.get("result"), dict):
                    if live_clean:
                        mem["result"]["live_verification"] = last_live
                        mem["result"]["live_verification_reconciled_by_repair"] = (
                            result["live_verification_reconciled_by_repair"]
                        )
                    _job_touch(mem)
            return live_clean                   # счётчики уже чистые, но live мог быть stale
        result["counters_reconciled_by_repair"] = {
            "was_created": created, "was_failed": failed,
            "live_errors": live_errors, "note": "добивка подтвердила: все кампании набора живы",
        }
        job["created"] = max(created, total)
        job["failed"] = 0
        job["error"] = None
        job["result"] = result
        _job_db_save(parent_job_id, job, full=True)
        with _CREATE_JOBS_LOCK:
            mem = _CREATE_JOBS.get(parent_job_id)
            if mem is not None:
                mem["created"] = job["created"]
                mem["failed"] = 0
                mem["error"] = None
                if isinstance(mem.get("result"), dict):
                    if live_clean:
                        mem["result"]["live_verification"] = last_live
                        mem["result"]["live_verification_reconciled_by_repair"] = (
                            result["live_verification_reconciled_by_repair"]
                        )
                    mem["result"]["counters_reconciled_by_repair"] = result["counters_reconciled_by_repair"]
                _job_touch(mem)
        return True
    except Exception:  # noqa: BLE001 — реконсиляция best-effort, добивка уже записана
        return False

def _delayed_repair_reschedule(did: str, row: dict, remaining: int) -> bool:
    """Вернуть partial-строку добивки в waiting для следующего цикла («до нуля»).
    attempts++ при каждом повторе; после _DELAYED_REPAIR_MAX_RESCHEDULES — стоп (нечинимый
    остаток не должен крутить демона вечно). True — перепланировано."""
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("""
                UPDATE public.direct_delayed_repairs
                   SET status='waiting', attempts=attempts+1,
                       run_at=now() + (%s || ' seconds')::interval,
                       note='повтор добивки (остаток ' || %s || ')',
                       updated_at=now()
                 WHERE id=%s AND attempts < %s
            """, (str(_DELAYED_CONTENT_REPAIR_DELAY_SECONDS), str(int(remaining)),
                  did, _DELAYED_REPAIR_MAX_RESCHEDULES))
            conn.commit()
            return bool(cur.rowcount)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — повтор best-effort, partial-статус уже записан
        return False

def _delayed_content_repair_requeue_after_watchdog(did: str) -> bool:
    """Авто-реквью content_repair-строки, убитой watchdog'ом (Баг B 2026-07-22).

    Строка уже помечена status='failed'. Возвращаем её в 'waiting' с бэкоффом и attempts+1
    если не исчерпан кап _DELAYED_REPAIR_WATCHDOG_REQUEUE_MAX. Не создаём новую строку —
    обновляем ту же (уникальный индекс по parent_job_id+kind → конфликта нет при UPDATE).

    Кап защищает от вечного цикла если Баг A (IMAGE_NO_POOL) не перехватил все нечинимые случаи.
    Бэкофф — _DELAYED_CONTENT_REPAIR_DELAY_SECONDS (300с): дать время Grid осесть перед повтором.
    """
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("""
                UPDATE public.direct_delayed_repairs
                   SET status     = 'waiting',
                       attempts   = attempts + 1,
                       run_at     = now() + (%s || ' seconds')::interval,
                       note       = 'авто-реквью после watchdog-убийства (попытка '
                                    || (attempts + 1)::text || ')',
                       updated_at = now()
                 WHERE id = %s AND attempts < %s
            """, (str(_DELAYED_CONTENT_REPAIR_DELAY_SECONDS), did,
                  _DELAYED_REPAIR_WATCHDOG_REQUEUE_MAX))
            conn.commit()
            return bool(cur.rowcount)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — реквью best-effort, failed-статус уже записан
        return False

def _child_parent_ref(body) -> str:
    """job_id родителя для ЛЮБОЙ дочерней джобы: докрутка 152/резерв (_resume_of), доставка
    недостающих (_requeue_of), recreate-починка (_repair_parent_job_id). '' — джоба самостоятельная."""
    b = body or {}
    for k in ("_resume_of", "_requeue_of", "_repair_parent_job_id"):
        v = str((b.get(k) or "")).strip()
        if v:
            return v
    return ""

def _parent_update(parent_jid: str, mutate) -> bool:
    """Прочитать родительскую джобу (БД → истина), применить mutate(job,result), записать
    в БД и in-memory. mutate возвращает False → изменений нет (не пишем). Best-effort:
    родителя нет (TTL/убран) → тихо пропускаем (Семён 2026-07-07: дочерняя работает без карточки)."""
    job = _job_db_get(parent_jid) or {}
    if not job:
        return False
    result = rgate.dict_from_jsonish(job.get("result"))
    if not isinstance(result, dict):
        result = {}
    job["result"] = result
    if mutate(job, result) is False:
        return False
    _job_db_save(parent_jid, job, full=True)
    with _CREATE_JOBS_LOCK:
        mem = _CREATE_JOBS.get(parent_jid)
        if mem is not None:
            for _k in ("created", "failed", "done", "total", "set_total", "status",
                       "error", "finished_at"):
                if _k in job:
                    mem[_k] = job[_k]
            mem["result"] = result
            _job_touch(mem)
    return True

def _parent_absorb_child_start(parent_jid: str, child_jid: str, child_total: int) -> None:
    """Старт дочерней добивки → родитель снова «в работе»: done -= объём добивки (total остаётся),
    прогресс-бар падает (было 14/14 → стало 9/14 при добивке 5 шт.), добиваемые пункты покидают
    bucket «не создано» (failed -= child_total; при неуспехе вернутся дельтой через
    _parent_absorb_child_progress). Идемпотентно по child_jid (ре-клейм после рестарта не
    задваивает)."""
    if not parent_jid or not child_jid or parent_jid == child_jid:
        return
    def _m(job, result):
        children = result.setdefault("_resume_children", {})
        if child_jid in children:
            return False                                 # уже учтён — не задваиваем
        children[child_jid] = {"c": 0, "f": 0, "d": 0}
        active = result.setdefault("_active_children", [])
        if child_jid not in active:
            active.append(child_jid)
        ct = int(child_total or 0)
        job["done"] = max(0, int(job.get("done") or 0) - ct)
        job["failed"] = max(0, int(job.get("failed") or 0) - ct)
        job["status"] = "running"
        job["error"] = None
        job["finished_at"] = None
        return True
    try:
        _parent_update(parent_jid, _m)
    except Exception:  # noqa: BLE001 — вливание best-effort
        pass

def _parent_absorb_child_progress(parent_jid: str, child_jid: str, created: int,
                                  failed: int, done_units: int, *, final: bool = False) -> None:
    """Влить ЖИВОЙ прогресс дочерней добивки в родителя дельтами (без задвоения при повторных
    вызовах — база хранится в result['_resume_children'][child_jid]). created/failed/done
    родителя пополняются по мере добивки; при final последний ребёнок → карточка снова
    терминальная (done, бар 100%)."""
    if not parent_jid or not child_jid or parent_jid == child_jid:
        return
    # ТРЕК A: под детачем dcr НЕ трекается на родителе (absorb_start пропущен) → ЕГО терминальный
    # absorb_progress(final=True) не должен ни двигать бар, ни ВОСКРЕШАТЬ уже-терминального родителя
    # (done/cancelled/error/interrupted) в `done`. dcr крутится демоном независимо. Реальные дети
    # (recreate/resume, child_jid=job_id) и finalize (`fin:`) сюда не попадают — их child_jid не `dcr:`.
    if _DCR_DETACH_PARENT and child_jid.startswith("dcr:"):
        return
    def _m(job, result):
        children = result.setdefault("_resume_children", {})
        base = children.get(child_jid)
        if base is None:                                 # start-хук не отработал → учитываем с нуля
            base = {"c": 0, "f": 0, "d": 0}
            children[child_jid] = base
        dc = int(created or 0) - int(base.get("c") or 0)
        df = int(failed or 0) - int(base.get("f") or 0)
        dd = int(done_units or 0) - int(base.get("d") or 0)
        job["created"] = max(0, min(int(job.get("total") or 0), int(job.get("created") or 0) + dc))
        job["failed"] = max(0, min(int(job.get("total") or 0), int(job.get("failed") or 0) + df))
        job["done"] = min(int(job.get("total") or 0), int(job.get("done") or 0) + dd)
        base["c"] = int(created or 0)
        base["f"] = int(failed or 0)
        base["d"] = int(done_units or 0)
        if final:
            hist = result.get("resume_merged")
            if not isinstance(hist, list):
                hist = []
            hist.append({"job_id": child_jid, "created": int(created or 0),
                         "failed": int(failed or 0)})
            result["resume_merged"] = hist[-10:]
            active = result.setdefault("_active_children", [])
            if child_jid in active:
                active.remove(child_jid)
            if not active:                               # все добивки закрыты → карточка терминальна
                job["done"] = int(job.get("total") or 0)
                job["status"], job["error"] = terminal_status_for_parent_failed(job.get("failed") or 0)
                job["finished_at"] = time.time()
        else:
            job["status"] = "running"
        return True
    try:
        _parent_update(parent_jid, _m)
    except Exception:  # noqa: BLE001 — вливание best-effort
        pass

def _merge_resume_into_parent(jid: str, job_final: dict, body: dict) -> None:
    """Финальное вливание дочерней добивки (докрутка/доставка/recreate) в родительскую карточку
    (Семён 2026-07-06/07: «по карточке видно сколько создалось/добилось/готово»). Дельтами через
    _parent_absorb_child_progress — согласовано с live-прогрессом (start-хук + периодический sync),
    без задвоения. Саму дочернюю джобу /api/create_jobs НЕ отдаёт отдельной карточкой."""
    parent_jid = _child_parent_ref(body)
    if not parent_jid or parent_jid == jid:
        return
    _du = int(job_final.get("set_done") or job_final.get("done") or job_final.get("total") or 0)
    _parent_absorb_child_progress(
        parent_jid, jid, int(job_final.get("created") or 0),
        int(job_final.get("failed") or 0), _du, final=True)

def _cancel_children_of(parent_jid: str) -> int:
    """Отмена родителя каскадом гасит его активные дочерние джобы (докрутка/доставка/recreate)
    того же логина (Семён 2026-07-07). queued/claimed/awaiting → cancelled; running → control=cancel
    (worker остановит после текущей кампании). → число погашенных дочерних."""
    parent_jid = (parent_jid or "").strip()
    if not parent_jid:
        return 0
    rows = []
    try:
        import psycopg2.extras
        conn = _victory_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""
                SELECT job_id, status FROM public.direct_automation_jobs
                 WHERE status NOT IN ('done','error','cancelled','interrupted')
                   AND (body->>'_resume_of'=%s OR body->>'_requeue_of'=%s
                        OR body->>'_repair_parent_job_id'=%s)
            """, (parent_jid, parent_jid, parent_jid))
            rows = cur.fetchall() or []
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        rows = []
    n = 0
    for r in rows:
        cjid = r["job_id"]
        st = (r.get("status") or "").strip()
        try:
            if st in ("queued", "claimed", "awaiting_feed_decision"):
                _job_db_set_status(cjid, "cancelled", "отменено вместе с родителем")
            else:                                        # running → команда worker'у
                _job_control_set(cjid, "cancel")
            with _CREATE_JOBS_LOCK:
                mem = _CREATE_JOBS.get(cjid)
                if mem is not None:
                    mem["cancel"] = True
                    if mem.get("status") in ("queued", "claimed") and cjid in _CREATE_QUEUE:
                        _CREATE_QUEUE.remove(cjid)
                        mem["status"] = "cancelled"
            n += 1
        except Exception:  # noqa: BLE001
            pass
    return n

def _record_delayed_content_repair(parent_job_id: str, row: dict) -> None:
    job = _job_db_get(parent_job_id) or {}
    if not job:
        return
    result = rgate.dict_from_jsonish(job.get("result"))
    if not isinstance(result, dict):
        result = {}
    history = result.get("delayed_content_repair")
    if not isinstance(history, list):
        history = [] if history is None else [history]
    history.append(row)
    result["delayed_content_repair"] = history[-5:]
    job["result"] = result
    _job_db_save(parent_job_id, job, full=True)
    with _CREATE_JOBS_LOCK:
        mem = _CREATE_JOBS.get(parent_job_id)
        if mem is not None and isinstance(mem.get("result"), dict):
            mem["result"] = result
            _job_touch(mem)

def _record_auto_repair_full(parent_job_id: str, payload: dict) -> None:
    """Write the top-level ``auto_repair_full`` summary into the parent job result (mem + DB).

    UI (_renderJobVerification) reads this key to show «✅ авто-добивка: исполнено X действий».
    """
    job = _job_db_get(parent_job_id) or {}
    if not job:
        return
    result = rgate.dict_from_jsonish(job.get("result"))
    if not isinstance(result, dict):
        result = {}
    result["auto_repair_full"] = payload
    job["result"] = result
    _job_db_save(parent_job_id, job, full=True)
    with _CREATE_JOBS_LOCK:
        mem = _CREATE_JOBS.get(parent_job_id)
        if mem is not None and isinstance(mem.get("result"), dict):
            mem["result"]["auto_repair_full"] = payload
            _job_touch(mem)

def _schedule_delayed_content_repair_after_done(parent_job_id: str, job_snapshot: dict) -> dict | None:
    req = rauto.delayed_content_repair_request(parent_job_id, job_snapshot)
    if not req:
        return None
    if req.get("scheduled") is False:
        return req
    did = _delayed_content_repair_save(
        parent_job_id,
        req.get("login") or "",
        req.get("agency") or "",
        kind="content_repair_post_recreate" if req.get("post_recreate") else "content_repair",
    )
    if did and not _DCR_DETACH_PARENT:
        # (legacy, реверс env=0) Родитель уже «done» — возвращаем в running: delayed-repair ещё не
        # завершён. child_total=0 → done/failed не трогаем, только status=running + _active_children.
        # ⚠️ Держит родителя running пока dcr жив (watchdog:868 щадит) → dcr, застрявший в partial,
        # висел ~час. Под детачем (дефолт) — НЕ вливаем: родитель остаётся терминальным (`done`).
        _parent_absorb_child_start(parent_job_id, f"dcr:{did}", 0)
    out = {
        "scheduled": bool(did),
        "delayed_repair_id": did,
        "parent_detached": bool(did) and _DCR_DETACH_PARENT,
        "source": req.get("source") or "delayed_after_done",
        "content_repairs": req.get("content_repairs") or 0,
        "run_after_seconds": _DELAYED_CONTENT_REPAIR_DELAY_SECONDS,
        "uses_direct_units": False,
    }
    if not did:
        out["note"] = "delayed content repair уже был запланирован или не сохранён"
    return out

def _run_delayed_content_repair(row: dict) -> None:
    """Delayed FULL in-place repair cycle after a create job is done.

    Runs OFF the worker thread (in the delayed-repair daemon) on a job whose status is already
    ``done`` and ``finished_at`` is set → the watchdog (_create_watchdog_tick) only touches
    ``running`` jobs, so no heartbeat bump is needed here.

    Cycle: fresh Grid-first live verification (Grid has caught up after the delay) → execute ALL
    executable in-place actions (content/promo/callouts/rename) via the SAME executors as the
    manual «План добивки» button (rauto.execute_all_in_place) → re-verify. Up to
    _DELAYED_FULL_REPAIR_MAX_ITERATIONS iterations; stop early if nothing progresses (anti
    ping-pong). Recreate/UAC-replace stays with _auto_queue_recreate_after_done.
    """
    did = (row.get("id") or "").strip()
    parent_job_id = (row.get("parent_job_id") or "").strip()
    _delayed_repair_set_status(did, "running", "повторная Grid-first проверка перед авто-добивкой")
    job, result, ctx, err = _create_set_job_context(parent_job_id)
    if err:
        out = {"ok": False, "error": err[0].get("error"), "uses_direct_units": False}
        _delayed_repair_set_status(did, "error", out["error"], out)
        _record_delayed_content_repair(parent_job_id, {"id": did, "status": "error", **out})
        _parent_absorb_child_progress(parent_job_id, f"dcr:{did}", 0, 0, 0, final=True)
        return
    login = (ctx.get("login") or row.get("login") or "").strip()
    if not login:
        out = {"ok": False, "error": "login не сохранён в job", "uses_direct_units": False}
        _delayed_repair_set_status(did, "error", out["error"], out)
        _record_delayed_content_repair(parent_job_id, {"id": did, "status": "error", **out})
        _parent_absorb_child_progress(parent_job_id, f"dcr:{did}", 0, 0, 0, final=True)
        return
    agency = ctx.get("agency") or row.get("agency") or ""
    results_tree = ctx.get("results") or []
    body = ctx.get("body") or {}
    deps = _repair_deps()

    def _live_plan() -> tuple[dict, dict, int, dict]:
        # phase="delayed": этот проход бежит в dcr-демоне ПОСЛЕ выдержки (180-240с) — Grid уже
        # осел, поэтому недобор «build ⇄ кабинет» здесь дефект (error + repair-кандидат), а не
        # «демон ещё доливает» (warn in-job). Без явного phase дефолт "in_job" глушил бы
        # BUILD_LIVE_UNDERCOUNT до warn во ВСЕЙ отложенной ветке (ревью этапа 1, находка A1-б).
        lv = _create_set_live_verification(login, results_tree, agency=agency, use_v5=False,
                                           phase="delayed")
        pl = (lv or {}).get("repair_plan") or {}
        summ = rgate.summarize_repair_gate(body, results_tree, pl)
        # ВСЕ in-place действия, которые реально исполняет execute_all_in_place (keywords_repair /
        # adprice_repair / images_repair / images_forbidden / content / default_text / promo /
        # callout / rename) = executable_now минус recreate-очередь (recreate/UAC-replace — НЕ in-place,
        # уходят в _auto_queue_recreate_after_done). Раньше cnt считал только content+promo+callout+
        # rename → keywords_repair и adprice_repair НЕ добивались авто (gate inplace_cnt<=0 → break,
        # execute_all_in_place не вызывался) — «поисковые группы без ключей» оставались навсегда.
        cnt = int(summ.get("executable_now") or 0) - int(summ.get("queued_recreate_items") or 0)
        return lv, pl, cnt, summ

    all_executed: list[dict] = []
    all_failed: list[dict] = []
    all_outputs: list[dict] = []
    units_gated: list[dict] = []
    iterations = 0
    last_live: dict = {}
    last_summ: dict = {}
    remaining = 0
    _repair_started = time.time()          # B2: старт бюджета времени repair-джобы
    _budget_exhausted = False
    _nonfixable_stop = False
    try:
        for _ in range(_DELAYED_FULL_REPAIR_MAX_ITERATIONS):
            # B2: бюджет времени исчерпан → выходим ЧИСТО (partial, без reschedule), не давая
            # watchdog-у (1800с) убить джобу. remaining держит последний известный остаток.
            if time.time() - _repair_started > _DELAYED_REPAIR_TIME_BUDGET_SECONDS:
                _budget_exhausted = True
                # Свежий пересчёт remaining: предыдущее значение было взято ДО execute_all_in_place
                # последней итерации → финальный отчёт должен отражать реальное состояние после неё.
                try:
                    last_live, _fp, remaining, last_summ = _live_plan()
                except Exception:  # noqa: BLE001 — best-effort, не сбиваем бюджет-break
                    pass
                break
            live_report, plan, inplace_cnt, last_summ = _live_plan()
            last_live = live_report
            remaining = inplace_cnt         # актуальный остаток (на случай budget-break на след. итерации)
            if inplace_cnt <= 0:
                remaining = 0
                break
            iterations += 1
            # Живой прогресс в note (иначе «повторная Grid-first проверка» висит замороженной
            # 10+ мин и выглядит как зависание) + бамп updated_at защищает от watchdog-а.
            _delayed_repair_set_status(
                did, "running",
                f"авто-добивка: итерация {iterations}, план {inplace_cnt} действ., "
                f"исполнено {len(all_executed)}")
            # post_verify не передаём: цикл сам делает свежую live-сверку через _live_plan()
            # перед следующим проходом и в конце — иначе был бы лишний Grid-запрос на итерацию.
            res = rauto.execute_all_in_place(login, ctx, plan, deps)
            all_executed.extend(res.get("executed_actions") or [])
            all_failed.extend(res.get("failed_actions") or [])
            all_outputs.extend(res.get("results") or [])
            units_gated.extend(res.get("units_gated") or [])
            if not (res.get("executed") or 0):
                # ничего не исполнилось за проход → повторная попытка бессмысленна (anti ping-pong)
                # B1: если ВСЕ провалы — field-ошибки Grid (UNAVAILABLE_FIELD/UNKNOWN_FIELD/…),
                # проблема нечинима in-place: НЕ считаем её остатком и НЕ перепланируем (иначе цикл
                # долбит по кругу на одном флаге до watchdog kill). Иначе — обычная сверка остатка.
                if _repair_failures_nonfixable(res.get("failed_actions") or []):
                    _nonfixable_stop = True
                    remaining = 0
                    break
                last_live, _fp, remaining, last_summ = _live_plan()
                break
        else:
            # исчерпали лимит итераций → финальная сверка остатка
            last_live, _fp, remaining, last_summ = _live_plan()
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": str(e)[:240], "uses_direct_units": False,
               "auto_repair_full": {"executed": all_executed[:40], "failed": all_failed[:20],
                                    "iterations": iterations, "remaining_actions": remaining}}
        _delayed_repair_set_status(did, "error", out["error"], out)
        _record_delayed_content_repair(parent_job_id, {"id": did, "status": "error", **out})
        _record_auto_repair_full(parent_job_id, out["auto_repair_full"])
        _parent_absorb_child_progress(parent_job_id, f"dcr:{did}", 0, 0, 0, final=True)
        return

    # Declarative spec-audit (keyword-shift/images-forbidden/plan⊆slepok) + auto-fix of
    # KEYWORDS_WRONG_GROUP. Runs after the standard in-place actions; failures never break the
    # delayed-repair cycle (best-effort, no Direct create units).
    spec_audit: dict = {}
    _is_post_recreate = (row.get("kind") or "") == "content_repair_post_recreate"
    try:
        spec_audit = _run_spec_audit_and_fix(login, ctx, skip_recreate=_is_post_recreate)
    except Exception as e:  # noqa: BLE001
        spec_audit = {"error": str(e)[:220]}

    # Видео недогружено (still_missing из spec-audit) → считаем это остатком, чтобы «до нуля»
    # reschedule перезапустил докрутку: per-ct breaker + 2 ретрая аплоада добьют видео на следующем
    # цикле, когда Grid догонит edit-view lag. video_no_pool в still_missing НЕ входит (fixable=False)
    # → нечинимый «нет ролика в паке» не создаёт вечный цикл. (Семён 2026-07-08: «чтобы загружалось».)
    _video_still = int((spec_audit.get("video_missing_fix") or {}).get("still_missing_total") or 0)
    if _video_still > 0:
        remaining = int(remaining) + _video_still

    afr = {
        "executed": all_executed[:40],
        "failed": all_failed[:20],
        "iterations": iterations,
        "remaining_actions": int(remaining),
        "units_gated": units_gated[:10],
        "results": all_outputs[:20],
        "spec_audit": spec_audit,
        "budget_exhausted": _budget_exhausted,        # B2
        "nonfixable_stop": _nonfixable_stop,          # B1
    }
    ok = (not all_failed) and int(remaining) == 0
    if not all_executed and not all_failed:
        final_status = "skipped" if int(remaining) == 0 else "partial"
    elif ok:
        final_status = "done"
    else:
        final_status = "partial"
    out = {
        "ok": ok,
        "auto_repair_full": afr,
        "delayed_repair_id": did,
        "parent_job_id": parent_job_id,
        "live_verification": last_live,
        "uses_direct_units": False,
    }
    # Собираем campaigns_fixed из всех sub-fix в spec_audit для правдивого note (ПРАВКА A)
    _sa_fixed = sum(
        int((spec_audit.get(k) or {}).get("campaigns_fixed") or 0)
        for k in (spec_audit or {})
        if isinstance((spec_audit or {}).get(k), dict)
    )
    _delayed_repair_set_status(
        did, final_status,
        (f"авто-добивка: исполнено {len(all_executed)}, остаток {remaining}, итераций {iterations}"
         + (f", spec_audit={_sa_fixed}" if _sa_fixed else "")
         + (" · бюджет времени исчерпан (partial без reschedule)" if _budget_exhausted else "")
         + (" · остаток нечиним in-place (field-ошибка Grid) — reschedule отменён"
            if _nonfixable_stop else "")),
        out,
    )
    _record_delayed_content_repair(parent_job_id, {"id": did, "status": final_status, **out})
    _record_auto_repair_full(parent_job_id, afr)
    # «ДО НУЛЯ» (требование Семёна 2026-07-05): partial с остатком → вернуть ЭТУ ЖЕ строку в
    # waiting — демон прогонит цикл ещё раз (Grid к тому времени догонит edit-view lag).
    # Кап _DELAYED_REPAIR_MAX_RESCHEDULES защищает от вечного цикла на нечинимом остатке.
    # B1/B2: НЕ перепланируем если остаток нечиним (field-ошибки Grid) или исчерпан бюджет времени —
    # это не даёт циклу долбить по кругу и упереться в watchdog kill.
    if (final_status == "partial" and int(remaining) > 0
            and not _nonfixable_stop and not _budget_exhausted):
        _delayed_repair_reschedule(did, row, remaining)
    elif final_status in ("done", "skipped"):
        # Реконсиляция счётчиков карточки (требование Семёна 2026-07-05): после добивки НЕ должно
        # оставаться «создано 13 · ❌ 1», ЕСЛИ ошибок ДЕЙСТВИТЕЛЬНО нет. Только при подтверждённом
        # нуле: live-сверка по кабинету errors=0, in-place остаток 0, очередь пересоздания пуста.
        _reconcile_parent_job_counters(parent_job_id, last_live, last_summ,
                                       login=login, body=body)
        # Требование «в итоге все кампании созданы»: если ЭТА добивка — по джобе-доставке
        # (_requeue_of), и её live-сверка чистая — реконсилируем и ИСХОДНУЮ джобу.
        _rq_parent = str((body or {}).get("_requeue_of") or "").strip()
        if _rq_parent:
            _reconcile_parent_job_counters(_rq_parent, last_live, last_summ,
                                           login=login, body=body)
    # «ДО НУЛЯ» по СОСТАВУ НАБОРА (кейс 2026-07-05: «tp1(куки): partial-кампания удалена —
    # объявления не созданы» — позиция терялась НАВСЕГДА: ни deferred, ни auto-recreate её не
    # подхватывали, live-сверка видит только СОЗДАННЫЕ результаты). Если у родительской джобы
    # failed>0 / created<total — доставляем ОДНОЙ повторной джобой с тем же телом: RESUME-SKIP
    # оркестратора пропустит уже созданные кампании (tp1_rsy — пофидово), создастся только
    # недостающее. Один уровень: джоба-доставка сама внучек не плодит (_requeue_of-гейт).
    _requeue_missing_positions_once(parent_job_id, login, body)
    # Возвращаем родителя в терминальный статус после завершения delayed-repair.
    # Если repair перепланирован (partial + remaining>0) — родитель остаётся running до
    # следующего прохода демона. Во всех остальных случаях (done/skipped/partial-без-остатка/
    # error/исключение — они handled выше через return) — убираем dcr:{did} из _active_children;
    # если active пусто → карточка снова «done» (статус/done=total/finished_at).
    if not (final_status == "partial" and int(remaining) > 0):
        _parent_absorb_child_progress(parent_job_id, f"dcr:{did}", 0, 0, 0, final=True)

def _run_delayed_finalize(row: dict) -> None:
    """Задача F: REPLAY захваченной Grid-финализации набора (kind='finalize_set').

    Демон-путь: те же функции _finalize_rsya/_finalize_search_via_grid, что и инлайн — «ровно
    тот же набор Grid-операций», только вне цикла создания. Идемпотентно (UpdateCampaigns теми
    же значениями). remaining>0 → reschedule (attempts cap). done → карточка снова терминальна
    (закрываем child fin:{did}) + снимаем finalize_pending. Баллы Директа НЕ тратит."""
    did = (row.get("id") or "").strip()
    parent_job_id = (row.get("parent_job_id") or "").strip()
    _delayed_repair_set_status(did, "running", "async-финализация: replay Grid-финализаций")
    try:
        out = _finalize_queue_module().run_finalize_job(row)
    except Exception as e:  # noqa: BLE001 — весь replay best-effort, карточку не вешаем
        out = {"ok": False, "error": str(e)[:240], "remaining": 1, "uses_direct_units": False}
    remaining = int(out.get("remaining") or 0)
    ok = bool(out.get("ok")) and remaining == 0
    final_status = "done" if ok else ("partial" if remaining > 0 else "error")
    _delayed_repair_set_status(
        did, final_status,
        f"async-финализация: применено {out.get('applied', 0)}/{out.get('total', 0)}, "
        f"остаток {remaining}",
        out)
    _record_delayed_content_repair(parent_job_id, {"id": did, "status": final_status,
                                                   "kind": "finalize_set", **out})
    # reschedule до нуля (attempts cap защищает от вечного цикла на нечинимом остатке).
    _rescheduled = False
    if final_status == "partial" and remaining > 0:
        _rescheduled = _delayed_repair_reschedule(did, row, remaining)
    if not _rescheduled:
        # Терминал (done/error или исчерпан лимит reschedule): снимаем finalize_pending и
        # закрываем child → карточка снова терминальна. При error оставляем в result отметку.
        try:
            def _clear_pending(job, result):
                if isinstance(result.get("finalize_pending"), dict):
                    result["finalize_finished"] = {"status": final_status,
                                                    "applied": out.get("applied", 0),
                                                    "remaining": remaining}
                    result.pop("finalize_pending", None)
                    return True
                return False
            _parent_update(parent_job_id, _clear_pending)
        except Exception:  # noqa: BLE001
            pass
        _parent_absorb_child_progress(parent_job_id, f"fin:{did}", 0, 0, 0, final=True)

def _delayed_repair_daemon_loop(app) -> None:
    import psycopg2.extras
    while True:
        # ПРАВКА P2: watchdog — строки в status='running' дольше порога → помечать failed.
        # Только реально просроченные по updated_at (активная строка обновляется set_status).
        _wd_failed_finalize: list[tuple] = []
        _wd_failed_content: list[tuple] = []
        try:
            _wconn = _victory_conn_rw()
            try:
                _wcur = _wconn.cursor()
                _wcur.execute("""
                    UPDATE public.direct_delayed_repairs
                       SET status='failed',
                           note='watchdog: stuck running >' || %s || ' мин',
                           updated_at=now()
                     WHERE status='running'
                       AND updated_at < now() - (%s || ' seconds')::interval
                    RETURNING id, parent_job_id, kind
                """, (str(_DELAYED_REPAIR_STUCK_TIMEOUT_SECONDS // 60),
                      str(_DELAYED_REPAIR_STUCK_TIMEOUT_SECONDS)))
                _wd_all = _wcur.fetchall() or []
                _wd_failed_finalize = [(r[0], r[1]) for r in _wd_all
                                       if (r[2] or "") == "finalize_set"]
                _wd_failed_content = [(r[0], r[1]) for r in _wd_all
                                      if (r[2] or "").startswith("content_repair")]
                _wconn.commit()
            finally:
                _wconn.close()
        except Exception:  # noqa: BLE001
            pass
        # Задача F (DIRECT_ASYNC_FINALIZE): watchdog пометил застрявшую finalize-строку failed, но
        # child fin:{did} остаётся ОТКРЫТ (не пройден терминальный путь _run_delayed_finalize) →
        # карточка вечно «running» с невыставленными инвариантами (Карты OFF / места показа #3-#6).
        # Закрываем child как терминальный + снимаем finalize_pending (тот же терминал, что в
        # _run_delayed_finalize:2018-2032) — иначе набор виснет навсегда. Строки kind='finalize_set'
        # существуют ТОЛЬКО при DIRECT_ASYNC_FINALIZE=ON (создаются capture-путём) → при OFF список
        # пуст, no-op (нормальный dcr-путь не трогаем).
        if _wd_failed_finalize:
            def _clear_pending_wd(job, result):
                if isinstance(result.get("finalize_pending"), dict):
                    result["finalize_finished"] = {"status": "failed",
                                                    "note": "watchdog: stuck running"}
                    result.pop("finalize_pending", None)
                    return True
                return False
            for _fdid, _fparent in _wd_failed_finalize:
                if not _fparent:
                    continue
                try:
                    _parent_update(_fparent, _clear_pending_wd)
                except Exception:  # noqa: BLE001
                    pass
                _parent_absorb_child_progress(_fparent, f"fin:{_fdid}", 0, 0, 0, final=True)
        # К1 (2026-07-09): watchdog пометил застрявшую content_repair-строку failed (напр. spec_audit-
        # фиксер завис на мёртвом M3 до фикса idle/circuit-breaker — тогда весь delayed-repair
        # цикл вис, строка не доходила до терминала), но child dcr:{did} остаётся ОТКРЫТ →
        # карточка вечно «running» (осиротевший delayed-repair). Закрываем child как терминальный
        # (тот же вызов, что все терминальные ветки _run_delayed_content_repair:
        # _parent_absorb_child_progress final=True) + фиксируем провал в result-хвосте. Иначе
        # delayed content_repair не доходит до терминала. content_repair_post_recreate покрыт
        # startswith. finalize_set сюда НЕ попадает (обработан выше отдельным блоком).
        if _wd_failed_content:
            for _cdid, _cparent in _wd_failed_content:
                if not _cparent:
                    continue
                try:
                    _record_delayed_content_repair(_cparent, {
                        "id": _cdid, "status": "failed", "uses_direct_units": False,
                        "error": ("watchdog: content_repair stuck running >"
                                  f"{_DELAYED_REPAIR_STUCK_TIMEOUT_SECONDS // 60} мин без прогресса")})
                except Exception:  # noqa: BLE001
                    pass
                _parent_absorb_child_progress(_cparent, f"dcr:{_cdid}", 0, 0, 0, final=True)
                # Баг B (2026-07-22): watchdog убил content_repair с остатком действий.
                # Возвращаем строку в waiting с бэкоффом, если не исчерпан кап попыток.
                # С фиксом Бага A (IMAGE_NO_POOL) повтор быстро завершится нечинимым стопом.
                try:
                    _delayed_content_repair_requeue_after_watchdog(_cdid)
                except Exception:  # noqa: BLE001
                    pass
        rows = []
        try:
            conn = _victory_conn_rw()
            try:
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute("""
                    SELECT * FROM public.direct_delayed_repairs
                     WHERE status='waiting' AND run_at <= now()
                     ORDER BY run_at LIMIT 3
                """)
                rows = cur.fetchall()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            rows = []
        for row in rows:
            try:
                if (row.get("kind") or "") == "finalize_set":
                    _run_delayed_finalize(row)               # Задача F: async-финализация
                else:
                    _run_delayed_content_repair(row)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(_DELAYED_REPAIR_POLL)

def _ensure_delayed_repair_daemon(app) -> None:
    with _CREATE_JOBS_LOCK:
        if _DELAYED_REPAIR_DAEMON["started"]:
            return
        _DELAYED_REPAIR_DAEMON["started"] = True
    _delayed_repair_db_init()
    threading.Thread(target=_delayed_repair_daemon_loop, args=(app,), daemon=True).start()

def _resume_one_deferred(app, row) -> None:
    """Докрутить один остаток ПО КУКЕ (без баллов): поставить новую джобу с via_cookie=True.
    152 = автоматический переход на куки, поэтому ждать сброса баллов НЕ нужно — Grid/UAC создают
    черновики без units. Дубля нет: set_plan пропустит уже созданные кампании."""
    did = row["id"]
    login = row.get("login") or ""
    body = row.get("body") or {}
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:  # noqa: BLE001
            _deferred_set_status(did, "error", "битый body"); return
    items = body.get("items") or []
    if not items:
        _deferred_set_status(did, "done", "нет пунктов"); return
    _gap_note = _deferred_pack_gap_note(body)
    if _gap_note:
        _deferred_bump_resume_at(did, 6)
        _deferred_set_status(did, "waiting", _gap_note)
        return
    # Агентство для партиционирования очереди. Для обычной cookie-докрутки agency — это ключ
    # cookie-аккаунта и его нельзя подменять token-owner'ом из `_token_for_login()`: tp2/tp4
    # cookie-deferred иначе снова уходят в y-direct-victory вместо исходного victoryagency14.
    requested_ag = (row.get("agency") or body.get("agency") or "").strip()
    _tok, ag = "", requested_ag
    body["_resume_count"] = int(row.get("resume_count") or 0) + 1
    # _resume_via_token: пункты, которые в принципе НЕ создать по куке (NO_BRAND_SEGMENTS_AVAILABLE —
    # сегментный tp5 требует M3/токен) — сохранены с этим флагом; куку им НЕ навязываем.
    if body.get("_resume_via_token"):
        _tok, ag = _token_for_login(login, requested_ag, _direct_tokens())
        body["agency"] = ag or requested_ag
        # Токен-докрутку СТАВИМ В ОЧЕРЕДЬ ТОЛЬКО когда есть И токен, И баллы. Иначе воркер уйдёт на
        # cookie-путь (пустой токен ИЛИ preflight-152 форсит via_cookie) → NO_BRAND → self-reference-
        # дедуп → финал гасит строку в done → сегментный tp5 теряется (инцидент 08.07 721641cad7c1 /
        # job 23677e1473d1, porg-psm5h7q6). Нет кредов → НЕ ставим джобу, оставляем строку waiting с
        # бэкоффом; демон повторит. Строка НЕ будет помечена done несуществующим финалом джобы.
        if not _tok:
            _deferred_bump_resume_at(did, 1)
            _deferred_set_status(did, "waiting",
                                 "токен-докрутка сегментного tp5 ждёт агентский токен (не найден) — повтор через 1ч")
            return
        _alive = _units_alive_for_login(login, ag or "")
        if _alive is False:
            from datetime import datetime, timezone
            _now = datetime.now(timezone.utc)
            _reset = _next_units_reset_utc()
            _secs = (_reset - _now).total_seconds()
            _hrs = max(1, int(_secs // 3600) + (1 if _secs % 3600 else 0))
            _deferred_bump_resume_at(did, _hrs)
            _deferred_set_status(did, "waiting",
                                 f"токен есть, баллы Директа исчерпаны — ждём сброс ({_reset.isoformat()})")
            return
        # токен + баллы есть → добиваем ТОКЕНОМ (via_cookie НЕ ставим: сегментный tp5 пойдёт API-путём)
    else:
        body["agency"] = requested_ag
        body["via_cookie"] = True                          # докрутка ПО КУКЕ (без баллов) — не ждём полночь
    body["_deferred_id"] = did                             # финал джобы пометит остаток done (анти-цикл)
    # Семён 2026-07-06: добивка — сразу (не в конец очереди) и без НОВОЙ карточки; _resume_of →
    # воркер вольёт created/failed докрутки в родительскую джобу (row["job_id"] = исходная джоба).
    body["_resume_of"] = row.get("job_id")
    sess = {"logged_in": True, "is_admin": True, "_resume": True}   # системная докрутка — авторизована заранее
    try:
        _ensure_create_worker(app)
        jid = _job_new(len(items), login, body, sess, priority=True)
        body["_job_id"] = jid                              # как в api_create_set_async: воркер-путь + прогресс джобы
        _path = "токеном" if body.get("_resume_via_token") else "по куке"
        _deferred_set_status(did, "resumed", f"докрутка {_path} #{body['_resume_count']} поставлена в очередь (приоритет)")
    except Exception as e:  # noqa: BLE001
        _deferred_bump_resume_at(did, 1)
        _deferred_set_status(did, "waiting", f"ошибка постановки: {str(e)[:120]}")


def _deferred_pack_gap_note(body: dict) -> str:
    """Fail-closed preflight for cookie-deferred search tails.

    A deferred row can outlive the structure/content state it was built from. If its body still
    references missing ``only_cts``/``only_gks`` while the local M3 mirror is available, queueing
    the job just creates a deterministic 0/N or partial campaign and another toxic deferred loop.
    Block only tp2/tp4 search tails, where every requested ct/gk must have real pack keywords.
    """
    if not isinstance(body, dict) or body.get("_resume_via_token"):
        return ""
    try:
        from .create_set_content_preflight import create_set_pack_gap_note
    except Exception:  # noqa: BLE001
        return ""
    note = create_set_pack_gap_note(body)
    if not note:
        return ""
    return note.replace("Создание не запущено", "Deferred не запущен")

def _deferred_enqueue_now(app, did: str) -> tuple | None:
    """On-demand: поставить остаток отложенного набора в ОЧЕРЕДЬ СЕЙЧАС (кнопка «создать через
    куки») — БЕЗ ожидания сброса баллов и БЕЗ units-гейта (пользователь явно выбрал «сейчас»).
    По куке Мастер/Товарка создадутся без баллов; текстовые/РСЯ при 152 снова уйдут на докрутку.
    → (jid, total, login, agency) | None."""
    import psycopg2.extras
    row = None
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM public.direct_deferred_creates WHERE id=%s", (did,))
            row = cur.fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None
    if not row:
        return None
    login = row.get("login") or ""
    body = row.get("body") or {}
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:  # noqa: BLE001
            return None
    items = body.get("items") or []
    if not items:
        _deferred_set_status(did, "done", "нет пунктов"); return None
    _gap_note = _deferred_pack_gap_note(body)
    if _gap_note:
        _deferred_bump_resume_at(did, 6)
        _deferred_set_status(did, "waiting", _gap_note)
        return None
    body["_resume_count"] = int(row.get("resume_count") or 0) + 1
    ag = row.get("agency") or body.get("agency") or ""
    body["agency"] = ag                                   # ключ партиционирования очереди
    # По куке ЭТИ пункты создать нельзя (см. _resume_via_token в _resume_one_deferred) — кнопка
    # «сейчас» тут бессильна раньше сброса баллов, поэтому куку им не навязываем (тот же отказ).
    if not body.get("_resume_via_token"):
        body["via_cookie"] = True                         # ЯВНОЕ согласие пользователя (попап) → token-типы по куке
    body["_deferred_id"] = did                            # финал джобы пометит остаток done (анти-цикл)
    body["_resume_of"] = row.get("job_id")                # → воркер вольёт created/failed в родительскую джобу
    sess = {"logged_in": True, "is_admin": True, "_resume": True}   # системная докрутка — авторизована
    _ensure_create_worker(app)
    jid = _job_new(len(items), login, body, sess, priority=True)   # _job_new сам проставит body["_job_id"]
    _deferred_set_status(did, "resumed", "запущено вручную (куки/сейчас) — поставлено в очередь (приоритет)")
    return jid, len(items), login, ag

def _resume_daemon_loop(app) -> None:
    """Фоновый демон: раз в ~10 мин докручивает остатки, у которых наступил resume_at и есть баллы."""
    import psycopg2.extras
    while True:
        rows = []
        try:
            conn = _victory_conn_rw()
            try:
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute("SELECT * FROM public.direct_deferred_creates "
                            "WHERE status='waiting' AND resume_at <= now() ORDER BY resume_at LIMIT 5")
                rows = cur.fetchall()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            rows = []
        _busy_launched: set = set()
        for row in rows:
            try:
                _rlogin = str((row or {}).get("login") or "").strip()
                # ГАРД активного логина (2026-07-11): демон брал до 5 waiting-строк и стартовал
                # _resume_one_deferred для каждой БЕЗ проверки → на логине с уже активной create-джобой
                # (или с несколькими своими deferred) поднималось 3 джобы разом → гонка/дубли/конфликт
                # баллов. Не стартуем докрутку, если у логина есть активная create-джоба (queued/running/
                # claimed/resumed) ИЛИ мы уже подняли resume для него в этом батче. Занят → просто
                # пропускаем (resume_at НЕ сдвигаем: строка остаётся waiting, подхватится следующим поллингом).
                if _rlogin and (_rlogin in _busy_launched or _job_db_active_by_login(_rlogin)):
                    continue
                _res = _resume_one_deferred(app, row)
                if _rlogin and _res:
                    _busy_launched.add(_rlogin)
            except Exception:  # noqa: BLE001
                pass
        try:
            _jobs_purge_old()                            # бэкстоп-чистка истории джоб (память+БД)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(_RESUME_POLL)

def _ensure_resume_daemon(app) -> None:
    """Лениво поднимает демон авто-докрутки (1 раз)."""
    with _CREATE_JOBS_LOCK:
        if _RESUME_DAEMON["started"]:
            return
        _RESUME_DAEMON["started"] = True
    _deferred_db_init()
    threading.Thread(target=_resume_daemon_loop, args=(app,), daemon=True).start()

def _job_kind(body: dict | None) -> str:
    b = body or {}
    if b.get("_kind") in _sed._EDIT_KINDS:
        return b.get("_kind")                         # edit_keywords/toggle_aon_aoff/add|remove_ct_group
    if b.get("_kind") == "delete_drafts":
        return "delete_drafts"
    if b.get("_kind") == "copy_campaigns":
        return "copy_campaigns"
    if b.get("content_source") == "slepok_library":
        return "slepok"
    return "set"

def _job_new_web(total: int, login: str, body: dict, saved_session: dict,
                 dedup_login: bool, priority: bool = False) -> str:
    """web-роль: постановка джобы ТОЛЬКО в БД (обычно status='queued', _web_posted=true, session в body).
    Воркер-процесс заберёт её клеймом из БД. In-memory очередь web-процесса не используется.

    priority=True — добивка/доставка остатка: body['_priority']=true → воркер клеймит такие
    джобы РАНЬШЕ обычных (см. _worker_claim_web_jobs) и ставит в НАЧАЛО in-memory очереди
    (_worker_adopt_job). Семён 2026-07-06: «добивка сразу, а не в конец очереди»."""
    if dedup_login:
        existing = _job_db_active_by_login(login)
        if existing:
            if body is not None:
                body["_job_id"] = existing
                body["_dedup_existing"] = True
            return existing
    jid = uuid.uuid4().hex[:12]
    initial_status = "queued"
    if body is not None:
        initial_status = str(body.pop("_initial_status", "queued") or "queued")
        if initial_status not in ("queued", "awaiting_feed_decision"):
            initial_status = "queued"
        body.pop("_dedup_existing", None)
        body["_job_id"] = jid
        body["_web_posted"] = True                       # маркер: поллер воркера забирает только такие
        if priority:
            body["_priority"] = True                     # добивка: клейм и очередь — впереди обычных
        body["_session_snapshot"] = dict(saved_session or {})   # нужен для test_request_context в воркере
    job = {"status": initial_status, "login": login, "done": 0,
           "total": int(total), "created": 0, "failed": 0,
           "set_done": 0, "set_total": int(total),
           "result": None, "error": None, "cancel": False,
           "kind": _job_kind(body), "publish": bool((body or {}).get("launch")),
           "stream_content": bool((body or {}).get("stream_content")),
           "step": None, "_id": jid, "body": body,
           "session": None, "agency": (body or {}).get("agency")}
    _job_db_save(jid, job)                                # INSERT: пишет body (с session+маркерами)+agency
    if dedup_login:   # дедуп не сработал (старая джоба уже terminal) → это ПЕРЕЗАПУСК на тот же login
        _supersede_delayed_repairs_for_login(login)
    return jid

def _job_new(total: int, login: str, body: dict, saved_session: dict,
             dedup_login: bool = False, priority: bool = False) -> str:
    """Регистрирует джобу в статусе 'queued' и ставит её в глобальную очередь.

    dedup_login=True (пользовательский submit) — АТОМАРНЫЙ дедуп: если по этому логину уже есть
    НЕзавершённая джоба (queued/running), второй джоб НЕ создаём, а возвращаем существующий job_id.
    Проверка+вставка под ОДНИМ _CREATE_JOBS_LOCK → закрывает гонку двух сабмитов подряд (TOCTOU:
    раньше эндпоинт сканировал и ОТПУСКАЛ лок до _job_new, два запроса успевали вставить обе копии).
    Внутренние постановки (докрутка/resume/delete_drafts) идут с dedup_login=False (намеренные).

    priority=True — докрутка/остаток (152, resume): встаёт В НАЧАЛО очереди, а не в конец
    (Семён 2026-07-06: «добивка сразу, а не в конец очереди»), НЕ ждёт своей очереди за новыми
    наборами. web-роль: приоритет уезжает в БД флагом body['_priority'] (см. _job_new_web).

    web-роль: НЕ трогаем in-memory очередь — джоба уходит только в БД (её заберёт worker-процесс)."""
    if _direct_role() == "web":
        return _job_new_web(total, login, body, saved_session, dedup_login, priority)
    jid = uuid.uuid4().hex[:12]
    with _CREATE_JOBS_LOCK:
        if dedup_login:
            _login = (login or "").strip()
            for _ejid, _ej in _CREATE_JOBS.items():
                if _ej.get("status") not in _JOB_TERMINAL and (_ej.get("login") or "").strip() == _login:
                    if body is not None:
                        body["_job_id"] = _ejid           # прогресс/отмена смотрят на СУЩЕСТВУЮЩУЮ джобу
                    return _ejid                          # дубль не создаём — отдаём активный job_id
        # _job_id ДОЛЖЕН быть в body ДО notify (и под этим же локом): иначе воркер (его будит
        # _CREATE_COND.notify ниже) успевает забрать body и сериализовать его в JSON ДО того, как
        # вызывающий код проставит body["_job_id"] → внутри create_set _job=None → прогресс/счётчик
        # «создано K из N» застывает на 0, хотя кампании реально создаются (гонка). Ставим здесь.
        if body is not None:
            body["_job_id"] = jid
        _is_stream = bool((body or {}).get("stream_content"))
        job = {"status": "queued", "login": login, "done": 0,
               "total": int(total), "created": 0, "failed": 0,
               "set_done": 0, "set_total": int(total),
               "result": None, "error": None, "cancel": False,
               "kind": _job_kind(body),
               "publish": bool((body or {}).get("launch")),
               "stream_content": _is_stream,   # stream=True → фаза generating перед creating
               "step": None,                   # текущая фаза: None/generating/creating (только при stream)
               "_id": jid, "body": body, "session": saved_session,
               "_heartbeat": time.time()}
        _CREATE_JOBS[jid] = job
        if priority:
            _CREATE_QUEUE.insert(0, jid)
        else:
            _CREATE_QUEUE.append(jid)
        # лёгкая чистка СТАРЫХ ЗАВЕРШЁННЫХ джоб (активные/очередь не трогаем), держим ~40
        terminal = [k for k, v in _CREATE_JOBS.items() if v["status"] in _JOB_TERMINAL]
        if len(terminal) > 40:
            for old in terminal[:-40]:
                _CREATE_JOBS.pop(old, None)
                _JOB_DB_LAST.pop(old, None)
        _CREATE_COND.notify()
    _job_db_save(jid, job)                                # серверная персистентность (видна с любого устройства)
    if dedup_login:   # дедуп не сработал (старая джоба уже terminal) → это ПЕРЕЗАПУСК на тот же login
        _supersede_delayed_repairs_for_login(login)
    return jid

def _create_jobs_ahead(jid: str) -> int:
    """Сколько джоб впереди (выполняется + ждут раньше в очереди) — для «в очереди, перед вами N»."""
    running = sum(1 for v in _CREATE_JOBS.values() if v["status"] == "running")
    try:
        idx = _CREATE_QUEUE.index(jid)
    except ValueError:
        return 0
    return running + idx


def _copy_retry_body_from_failed(row: dict) -> dict:
    """Build a fresh copy_campaigns body from a failed persisted copy job."""
    body = dict(row.get("body") or {})
    for key in (
        "_job_id",
        "_web_posted",
        "_dedup_existing",
        "_session_snapshot",
        "_copy_api_idempotency_key",
        "_copy_api_payload_hash",
    ):
        body.pop(key, None)
    target_login = (body.get("target_login") or row.get("login") or "").strip()
    body["_kind"] = "copy_campaigns"
    body["login"] = target_login
    body["target_login"] = target_login
    body["created_by"] = "agent-board-auto"
    body["_copy_retry_of"] = row.get("job_id") or ""
    body["_copy_retry_agent_board_task_id"] = row.get("agent_board_task_id")
    # Failed copy attempts can leave partial Direct drafts. The retry is allowed to clean drafts
    # before re-copying, but it still does not archive or touch accepted campaigns.
    body["target_cleanup"] = "delete_drafts"
    return body


def _copy_agent_retry_daemon_loop(app) -> None:
    """When a linked Agent Board task is done, requeue the failed copy job once."""
    while True:
        try:
            with app.app_context():
                try:
                    from .agent_board_bridge import (
                        copy_jobs_ready_for_agent_retry,
                        mark_copy_retry_started,
                    )
                    rows = copy_jobs_ready_for_agent_retry(_victory_conn_rw, limit=5)
                except Exception as e:  # noqa: BLE001
                    print(f"[copy-agent-retry] scan failed: {str(e)[:160]}", flush=True)
                    rows = []
                for row in rows:
                    failed_jid = str(row.get("job_id") or "")
                    body = _copy_retry_body_from_failed(row)
                    login = (body.get("target_login") or body.get("login") or "").strip()
                    if not failed_jid or not login:
                        continue
                    if _job_db_active_by_login(login):
                        continue
                    try:
                        total = len(body.get("campaign_ids") or []) or int(row.get("total") or 0)
                    except Exception:  # noqa: BLE001
                        total = int(row.get("total") or 0)
                    retry_jid = _job_new(total, login, body, {}, dedup_login=True, priority=True)
                    if retry_jid == failed_jid:
                        continue
                    try:
                        mark_copy_retry_started(_victory_conn_rw, failed_jid, retry_jid)
                    except Exception as e:  # noqa: BLE001
                        print(f"[copy-agent-retry] mark failed {failed_jid}->{retry_jid}: {str(e)[:160]}", flush=True)
                    print(f"[copy-agent-retry] {failed_jid} -> {retry_jid} login={login}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[copy-agent-retry] loop failed: {str(e)[:200]}", flush=True)
        time.sleep(max(10, int(_COPY_AGENT_RETRY_POLL or 60)))


def _ensure_copy_agent_retry_daemon(app) -> None:
    with _CREATE_JOBS_LOCK:
        if _COPY_AGENT_RETRY_DAEMON["started"]:
            return
        _COPY_AGENT_RETRY_DAEMON["started"] = True
    threading.Thread(target=_copy_agent_retry_daemon_loop, args=(app,), daemon=True).start()


def _write_gate_owner(job_kind: str) -> str:
    kind = str(job_kind or "")
    if kind == "copy_campaigns":
        return "direct-copy"
    if kind in _sed._EDIT_KINDS or _worker_scope() == "slepki":
        return "direct-slepki-worker"
    return "direct-create-worker"

def _agency_gate_claim(agency: str, job_id: str, job_kind: str = "") -> bool:
    """Занять кросс-процессный слот агентства. True = слот наш / не применимо; False = занят другим процессом."""
    if _write_gate.gate_cb_should_skip():
        print(f"[agency-gate] circuit-open, skip claim ({agency})", flush=True)
        return True  # fail-open: разрешаем джобе стартовать без кросс-процессной координации
    try:
        try:
            conn = _victory_conn_rw_gate()   # короткий таймаут коннекта (3 с) для gate-операции
        except Exception as e:  # noqa: BLE001
            _write_gate.gate_cb_on_failure()
            print(f"[agency-gate] claim fail-open ({agency}): {str(e)[:120]}", flush=True)
            return True
        _write_gate.gate_cb_on_success()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO public.direct_agency_active (agency, job_id, started_at) "
                "VALUES (%s, %s, now()) ON CONFLICT (agency) DO NOTHING RETURNING agency",
                (agency, job_id))
            got = cur.fetchone() is not None
            conn.commit()
            if not got:
                return False
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — FAIL-OPEN
        print(f"[agency-gate] claim fail-open ({agency}): {str(e)[:120]}", flush=True)
        return True
    if not _write_gate.try_acquire_agency(
        _victory_conn_rw_gate,
        agency,
        job_id,
        job_kind=job_kind or "direct_automation",
        owner_service=_write_gate_owner(job_kind),
    ):
        _agency_gate_release(agency, job_id)
        return False
    return True

def _agency_gate_release(agency: str, job_id: str) -> None:
    """Освободить СВОЙ слот агентства (идемпотентно, только своя job_id). FAIL-OPEN."""
    if _write_gate.gate_cb_should_skip():
        print(f"[agency-gate] circuit-open, skip release ({agency}) — lock истечёт по TTL", flush=True)
        return
    try:
        try:
            conn = _victory_conn_rw_gate()
        except Exception as e:  # noqa: BLE001
            _write_gate.gate_cb_on_failure()
            print(f"[agency-gate] release fail-open ({agency}): {str(e)[:120]}", flush=True)
            return
        _write_gate.gate_cb_on_success()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM public.direct_agency_active WHERE agency=%s AND job_id=%s",
                        (agency, job_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        print(f"[agency-gate] release fail-open ({agency}): {str(e)[:120]}", flush=True)
    _write_gate.release_agency(_victory_conn_rw_gate, agency, job_id)

def _agency_gate_sweep() -> None:
    """Backstop (из watchdog): освободить слоты, чей job больше не running/claimed
    (терминальный/пропал/краш процесса — после того как watchdog пометил его interrupted)."""
    if _write_gate.gate_cb_should_skip():
        print("[agency-gate] circuit-open, skip sweep", flush=True)
        return  # fail-open: sweep выполнится на следующем тике watchdog
    try:
        try:
            conn = _victory_conn_rw_gate()
        except Exception as e:  # noqa: BLE001
            _write_gate.gate_cb_on_failure()
            print(f"[agency-gate] sweep fail-open: {str(e)[:120]}", flush=True)
            return
        _write_gate.gate_cb_on_success()
        try:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM public.direct_agency_active a WHERE NOT EXISTS ("
                "  SELECT 1 FROM public.direct_automation_jobs j "
                "   WHERE j.job_id = a.job_id AND j.status IN ('running','claimed'))")
            conn.commit()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        print(f"[agency-gate] sweep fail-open: {str(e)[:120]}", flush=True)
    _write_gate.cleanup_direct_automation_inactive(_victory_conn_rw_gate)
    _write_gate.cleanup_expired(_victory_conn_rw_gate)

def _claim_next_job():
    """Берёт из очереди следующую джобу, если по агентству ещё не достигнут лимит параллельности.
    Ждёт, если очередь пуста ИЛИ все доступные джобы упёрлись в лимит агентства.
    Возвращает (jid, job, body, saved) и увеличивает счётчик агентства. Снятые отмены — пропускает."""
    with _CREATE_COND:
        while True:
            if _CREATE_DRAIN.get("on"):
                return None                               # drain: воркер завершает работу (worker_main SIGTERM)
            pick = None
            for i, q_jid in enumerate(_CREATE_QUEUE):
                q_job = _CREATE_JOBS.get(q_jid)
                if q_job is None:
                    _CREATE_QUEUE.pop(i)
                    pick = "retry"; break
                if q_job.get("cancel"):                   # отменили, пока ждал в очереди
                    _CREATE_QUEUE.pop(i)
                    q_job["status"] = "cancelled"; q_job["finished_at"] = time.time()
                    _job_db_save(q_jid, q_job, full=True)
                    pick = "retry"; break
                active = _CREATE_ACTIVE_AGENCIES.get(_job_agency(q_job), 0)
                if active >= _CREATE_MAX_PER_AGENCY:
                    continue                              # лимит по агентству исчерпан (в этом процессе) — ждёт
                # Кросс-процессный гейт: агентство может быть занято ДРУГИМ процессом (copy↔create) —
                # тогда не берём, ждём (не жжём куки/баллы одного агентства параллельно). FAIL-OPEN внутри.
                if not _agency_gate_claim(_job_agency(q_job), q_jid, str(q_job.get("kind") or "")):
                    continue
                # подходит: по агентству есть свободный слот (и локально, и кросс-процессно)
                _CREATE_QUEUE.pop(i)
                q_job["status"] = "running"
                q_job["started_at"] = time.time()         # старт прогона — для «ушло времени» в итоге
                _job_touch(q_job)
                _CREATE_ACTIVE_AGENCIES[_job_agency(q_job)] = active + 1
                return q_jid, q_job, q_job["body"], q_job["session"]
            if pick == "retry":
                continue                                  # снятую/битую убрали — пересканируем
            _CREATE_COND.wait(timeout=_WORKER_POLL_SEC)   # внешний write-gate отпускают без notify

def _create_worker_loop(app):
    """Worker пула создания: параллелит аккаунты, но держит лимит на агентство.
    После УСПЕШНОГО полного аккаунта — пауза _CREATE_POOL_PAUSE сек."""
    while True:
        claimed = _claim_next_job()
        if claimed is None:                               # drain (SIGTERM воркеру): завершаем тред
            return
        jid, job, body, saved = claimed
        agency = _job_agency(job)
        final_status = "error"
        # delete_drafts НЕ проходит create-постпроцесс (verify/finalize/delayed-repair): удалять
        # нечего верифицировать/финализировать. Флаг гейтит done-блок ниже (иначе воркер морозится
        # на Grid-финализации/добивке несуществующей РК — R2-5, 2026-07-10).
        _is_delete_drafts = (body or {}).get("_kind") == "delete_drafts"
        # Edit-джобы (правки структуры/ключей слепков) — как delete_drafts, НЕ проходят
        # create-постпроцесс (verify/finalize/delayed-repair): создавать/финализировать нечего.
        _is_edit_job = (body or {}).get("_kind") in _sed._EDIT_KINDS
        # Задача F (DIRECT_ASYNC_FINALIZE): открыть окно захвата финализации набора (по login).
        # OFF → register вернёт None (no-op). Снятие — в finally (гарантированно, даже при падении).
        _fin_login = str((body or {}).get("login") or "").strip()
        _finalize_queue_module().register(_fin_login, jid, agency)   # окно захвата (OFF → no-op)
        try:
            _set_llm_heartbeat_job(jid)
            _job_touch(job)
            _job_db_save(jid, job)                        # → 'running' в БД
            # Дочерняя добивка (докрутка/доставка/recreate) стартовала → родитель снова «в работе»:
            # его total растёт на объём добивки, прогресс-бар был 100% → снижается (Семён 2026-07-07).
            _parent_ref = _child_parent_ref(body)
            if _parent_ref and _parent_ref != jid:
                _parent_absorb_child_start(_parent_ref, jid, int(job.get("total") or 0))
            # сам прогон — ВНЕ lock'а (долгий), прогресс джоба обновляет по ссылке внутри ядра
            if _is_edit_job:
                # Правка структуры/ключей слепков — СЕРИЙНО в очереди (не гоняет со чтением
                # структуры воркером при создании РК). Ядро в slepki_editor.handle_job.
                try:
                    data = _sed.handle_job(body)
                except Exception as e:  # noqa: BLE001
                    data = {"error": str(e)[:300]}
            elif (body or {}).get("_kind") == "delete_drafts":
                # Удаление черновиков в ОБЩЕЙ очереди — то же ядро, что и синхронный эндпоинт,
                # но с прогрессом джобы (карточка показывает «удалено N · обработка набора N/M»).
                try:
                    data = _delete_drafts_core(body.get("login", ""), body.get("agency", ""), job=job)
                except Exception as e:  # noqa: BLE001
                    data = {"error": str(e)[:300]}
            elif (body or {}).get("_kind") == "copy_campaigns":
                try:
                    _copy_run_job(jid, body)
                    with _COPY_JOBS_LOCK:
                        cj = dict(_COPY_JOBS.get(jid) or {})
                    data = cj.get("result") if cj.get("status") == "done" else {"error": cj.get("error") or "копирование не завершилось"}
                except Exception as e:  # noqa: BLE001
                    data = {"error": str(e)[:300]}
            else:
                with app.test_request_context("/direct/api/create_set", method="POST", json=body):
                    try:
                        session.update(saved)                 # _direct_access увидит права
                        resp = _create_set_response()
                        obj = resp[0] if isinstance(resp, tuple) else resp
                        data = obj.get_json(silent=True) if hasattr(obj, "get_json") else None
                        if data is None:                      # редирект/HTML (нет прав) → честная ошибка
                            data = {"error": "фоновое создание не выполнено (нет JSON-ответа; проверьте права/сессию)"}
                    except Exception as e:  # noqa: BLE001
                        import traceback as _tb
                        print(f"[worker-tb] {_tb.format_exc()}", flush=True)
                        data = {"error": str(e)[:300]}
            _job_final = None
            with _CREATE_JOBS_LOCK:
                j = _CREATE_JOBS.get(jid)
                if j is not None:
                    if j.get("_watchdog_done"):
                        final_status = j["status"]
                        _job_final = dict(j)
                        j = None
                if j is not None:
                    j["result"] = data
                    # Счётчик gate-проверок, пропущенных из-за circuit-breaker за время этой джобы.
                    # Fail-open молча пропускал проверки — теперь это видно в errors_log/result.
                    _gate_skips = _write_gate.drain_skip_count()
                    if _gate_skips:
                        _add_job_err(
                            j,
                            f"[gate-cb] {_gate_skips} gate check(s) skipped"
                            " (Victory connect timeout, circuit-breaker open)",
                        )
                        if isinstance(data, dict):
                            data["gate_skips"] = _gate_skips
                    if data:
                        j["created"] = data.get("created", j["created"])
                        j["failed"] = data.get("failed", j["failed"])
                    _st, _err = terminal_status_for_job(j.get("kind"), data, cancelled=bool(j.get("cancel")))
                    j["status"] = _st
                    if _err:
                        j["error"] = _err
                    if _st == "done":
                        j["done"] = j["total"]
                        # Гейт финального статуса: если live-верификатор нашёл ошибки —
                        # фиксируем разбивку в result["has_issues"]. Статус остаётся "done"
                        # (кампании созданы), но карточка покажет предупреждение.
                        _issues_bd = compute_job_issues_breakdown(
                            j.get("kind"), data if isinstance(data, dict) else None
                        )
                        if _issues_bd and isinstance(data, dict):
                            data["has_issues"] = _issues_bd
                    # «Сколько ушло времени» — от старта прогона до терминала (сек). Кладём и в result,
                    # чтобы итоговый баннер показал длительность даже после рестарта (хранится в result jsonb).
                    if j.get("started_at"):
                        _el = max(0, int(time.time() - j["started_at"]))
                        j["elapsed"] = _el
                        if isinstance(data, dict):
                            data.setdefault("elapsed_seconds", _el)
                    _job_touch(j)
                    j["finished_at"] = time.time()         # момент завершения → карточка уйдёт через TTL
                    final_status = j["status"]
                    _job_final = dict(j)                   # снимок под lock'ом для DB-записи вне lock'а
            if _job_final is not None:
                _job_db_save(jid, _job_final, full=True)   # финальный статус + result в БД
                if final_status == "error" and (body or {}).get("_kind") == "copy_campaigns":
                    try:
                        from .agent_board_bridge import notify_copy_job_error
                        task_id = notify_copy_job_error(_victory_conn_rw, jid)
                        if task_id:
                            print(f"[copy-agent-board] task #{task_id} created for {jid}", flush=True)
                    except Exception as e:  # noqa: BLE001
                        print(f"[copy-agent-board] notify failed {jid}: {str(e)[:160]}", flush=True)
                _ready_logins_track(jid, _job_final)       # вкладка «Готовые логины» (add/remove)
                _merge_resume_into_parent(jid, _job_final, body)
                if final_status == "done" and not _is_delete_drafts and not _is_edit_job:
                    auto_queued = _auto_queue_recreate_after_done(jid, _job_final)
                    delayed_content = _schedule_delayed_content_repair_after_done(jid, _job_final)
                    # Задача F: захваченные финализации → очередь finalize_set. Пока не докручены,
                    # набор НЕ готов: держим карточку «running» (child fin:{did}) + finalize_pending
                    # в result (summary не зелёный). Демон REPLAY-нёт → реконсиляция → зелёный.
                    _finalize_enqueued = None
                    _finalize_inline = None
                    try:
                        _rec = _finalize_queue_module().unregister(_fin_login) if _fin_login else None
                        if _rec is not None and _rec.specs:
                            _finalize_enqueued = _finalize_queue_module().enqueue(
                                jid, _fin_login, agency, _rec.specs)
                            if _finalize_enqueued:
                                _parent_absorb_child_start(jid, f"fin:{_finalize_enqueued}", 0)
                            else:
                                # enqueue вернул None (ошибка БД / нет коннекта / ON CONFLICT): захваченную
                                # финализацию НЕ терять — в синхронном пути она бы отработала. Inline-replay
                                # ТЕМИ ЖЕ функциями, что delayed-демон (run_finalize_job → finalize_rsya/
                                # finalize_search_via_grid), синхронно здесь. Идемпотентно (finalize —
                                # UpdateCampaigns одними значениями). remaining>0 → ниже пометим finalize_pending.
                                _finalize_inline = _finalize_queue_module().run_finalize_job(
                                    {"result": {"specs": _rec.specs}})
                                print(f"[finalize-queue] enqueue=None → inline-replay {_fin_login}: "
                                      f"applied={_finalize_inline.get('applied')} "
                                      f"remaining={_finalize_inline.get('remaining')}", flush=True)
                    except Exception as _fe:  # noqa: BLE001 — постановка finalize best-effort
                        print(f"[finalize-queue] done-enqueue {_fin_login}: {str(_fe)[:200]}", flush=True)
                    post_done_changed = False
                    if auto_queued:
                        with _CREATE_JOBS_LOCK:
                            j = _CREATE_JOBS.get(jid)
                            if j is not None and isinstance(j.get("result"), dict):
                                j["result"]["auto_queued_repair"] = auto_queued
                                _lv = j["result"].get("live_verification")
                                if isinstance(_lv, dict):
                                    _rp = _lv.get("repair_plan")
                                    if isinstance(_rp, dict):
                                        _rp["status"] = "resolved"
                                        _rp["resolved_by"] = auto_queued.get("job_id", "")
                                _job_touch(j)
                                _job_final = dict(j)
                                post_done_changed = True
                    if delayed_content:
                        with _CREATE_JOBS_LOCK:
                            j = _CREATE_JOBS.get(jid)
                            if j is not None and isinstance(j.get("result"), dict):
                                j["result"]["delayed_content_repair_scheduled"] = delayed_content
                                _job_touch(j)
                                _job_final = dict(j)
                                post_done_changed = True
                    if _finalize_enqueued:
                        # DoD: набор ещё не финализирован → summary НЕ зелёный (finalize_pending).
                        with _CREATE_JOBS_LOCK:
                            j = _CREATE_JOBS.get(jid)
                            if j is not None and isinstance(j.get("result"), dict):
                                j["result"]["finalize_pending"] = {
                                    "delayed_repair_id": _finalize_enqueued,
                                    "specs": len(_rec.specs) if _rec else 0,
                                }
                                _job_touch(j)
                                _job_final = dict(j)
                                post_done_changed = True
                    if _finalize_inline is not None and _finalize_inline.get("remaining"):
                        # Inline-replay (enqueue вернул None) отработал ЧАСТИЧНО → набор финализирован
                        # не полностью: summary НЕ зелёный, помечаем finalize_pending + ошибку, чтобы
                        # повторный проход/ручная докрутка это подобрали (не выдаём невыполненную
                        # финализацию за успех). remaining==0 → всё применено inline, зелёный корректен.
                        with _CREATE_JOBS_LOCK:
                            j = _CREATE_JOBS.get(jid)
                            if j is not None and isinstance(j.get("result"), dict):
                                j["result"]["finalize_pending"] = {
                                    "inline_replay": True,
                                    "applied": _finalize_inline.get("applied", 0),
                                    "remaining": _finalize_inline.get("remaining", 0),
                                    "failed": _finalize_inline.get("failed", []),
                                    "error": "enqueue finalize вернул None; inline-replay выполнен частично",
                                }
                                _job_touch(j)
                                _job_final = dict(j)
                                post_done_changed = True
                    if post_done_changed and _job_final is not None:
                        _job_db_save(jid, _job_final, full=True)
        finally:
            try:
                _set_llm_heartbeat_job(None)
            except Exception:  # noqa: BLE001
                pass
            # Задача F: гарантированно закрыть окно захвата (при error/cancel done-блок не отработал →
            # иначе recorder висит в реестре и глотает финализацию следующего набора того же login).
            # Идемпотентно: если done-блок уже снял — pop вернёт None.
            if _fin_login:
                try:
                    _finalize_queue_module().unregister(_fin_login)
                except Exception:  # noqa: BLE001
                    pass
            if not _is_delete_drafts and not _is_edit_job and (body or {}).get("_kind") != "copy_campaigns":
                try:
                    from .create_set_prefetch import cleanup_job_cache as _cleanup_create_prepare_cache
                    _cleanup_create_prepare_cache(jid)
                except Exception:  # noqa: BLE001
                    pass
            # освобождаем слот агентства и будим пул
            with _CREATE_COND:
                active = max(0, int(_CREATE_ACTIVE_AGENCIES.get(agency, 0)) - 1)
                if active:
                    _CREATE_ACTIVE_AGENCIES[agency] = active
                else:
                    _CREATE_ACTIVE_AGENCIES.pop(agency, None)
                _CREATE_COND.notify_all()
            _agency_gate_release(agency, jid)             # кросс-процессный слот (вне _CREATE_COND — DB I/O не держит лок)
        if final_status == "done":                        # пауза ТОЛЬКО после успешного полного аккаунта
            time.sleep(_CREATE_POOL_PAUSE)

def _create_workers_count() -> int:
    """Количество worker'ов = число известных агентств, минимум 2."""
    try:
        n = len([k for k in (_direct_tokens() or {}).keys() if str(k).strip()])
    except Exception:  # noqa: BLE001
        n = 0
    return max(2, n or 2)

def _ensure_create_worker(app):
    """Лениво поднимает ПУЛ воркеров (при первом async-запросе):
    инициализирует таблицу персистентности и поднимает недавние джобы из БД (для просмотра).

    web-роль: воркеры/демоны/recover НЕ стартуем (их держит worker-процесс). Делаем только
    _jobs_db_init — чтобы таблица и колонка control существовали для постановки/статуса/команд.
    recover в web-роли ЗАПРЕЩЁН: он бы пометил web-posted queued-джобы interrupted и убил очередь."""
    with _CREATE_JOBS_LOCK:
        if _CREATE_WORKER["started"]:
            return
        _CREATE_WORKER["started"] = True
    _jobs_db_init()
    if _direct_role() == "web":
        return                                            # web: только схема БД, никаких фоновых тредов
    # СТОРОННИЙ процесс (ручной скрипт/агент, импортировавший blueprint вне systemd) НЕ должен
    # выполнять recover и поднимать воркеров/демонов: его recover помечал running-джобы ЖИВОГО
    # воркера 'interrupted' и рвал прогоны. Кейс 2026-07-06 (контроль №2 53fd086ef597, скрипт
    # с ролью-дефолтом 'all') чинили гейтом «DIRECT_ROLE ИЛИ INVOCATION_ID» — недостаточно:
    # 2026-07-19/20 живая джоба 404c320fc32e (running, прогресс есть) помечена interrupted
    # чужим одноразовым скриптом, который просто ЯВНО выставил DIRECT_ROLE (worker/all) —
    # без запуска под systemd. DIRECT_ROLE тривиально подделать в любом ad-hoc скрипте
    # (copy-paste из env соседнего сервиса), INVOCATION_ID systemd проставляет только РЕАЛЬНЫМ
    # управляемым юнитам и подделать его вручную нельзя. Признак сервиса — ТОЛЬКО INVOCATION_ID.
    if not os.environ.get("INVOCATION_ID"):
        return
    _jobs_db_recover()
    _ensure_create_watchdog()
    _create_watchdog_tick()
    workers = int(_CREATE_WORKERS or _create_workers_count())
    for _ in range(workers):                              # параллельно по разным агентствам
        threading.Thread(target=_create_worker_loop, args=(app,), daemon=True).start()
    _ensure_resume_daemon(app)                            # демон авто-докрутки остатка после сброса баллов
    _ensure_delayed_repair_daemon(app)                    # guarded content repair после Grid lag

def _ensure_copy_worker(app):
    """Воркер-пул отдельного copy-сервиса (direct-copy.service). Владеет ТОЛЬКО copy_campaigns
    в собственной in-memory очереди этого процесса.

    Умышленно НЕ поднимает create-set инфраструктуру: НЕТ _jobs_db_recover (деструктивен для
    общей таблицы), НЕТ startup-sweep пустых черновиков, НЕТ resume/delayed-repair демонов и НЕТ
    web-posted поллера. Поэтому рестарт этого сервиса НИКОГДА не трогает очередь создания РК, а
    рестарт direct.service не трогает копирование (его recover исключает kind='copy_campaigns')."""
    with _CREATE_JOBS_LOCK:
        if _CREATE_WORKER["started"]:
            return
        _CREATE_WORKER["started"] = True
    _jobs_db_init()                                       # схема таблицы (mirror прогресса копирования)
    _copy_jobs_recover()                                  # crash-cleanup ТОЛЬКО своих copy-джоб
    _ensure_create_watchdog()                             # heartbeat зависших джоб (по in-memory этого процесса)
    _ensure_copy_agent_retry_daemon(app)                  # Agent Board done → повтор failed copy job
    _create_watchdog_tick()
    workers = int(_CREATE_WORKERS or _create_workers_count())
    for _ in range(workers):                              # параллельно по разным агентствам
        threading.Thread(target=_create_worker_loop, args=(app,), daemon=True).start()


def _slepki_jobs_recover() -> None:
    """Crash-recovery ТОЛЬКО edit-джоб слепков (scope='slepki'). Аналог claimed→queued /
    running→interrupted из _jobs_db_recover, но узко по своему scope и БЕЗ create-специфики
    (sweep пустышек / reconcile / resume — там нечего досоздавать, правка атомарна).
      • running edit → interrupted: процесс упал в момент применения; повторно НЕ гоняем
        (запись temp+os.replace атомарна: файл либо старый, либо новый целый — полу-правки нет);
      • claimed edit → queued: поллер заклеймил, но процесс умер до adopt (body цел) → ре-клейм."""
    try:
        _ek = _edit_kinds_list()
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE public.direct_automation_jobs SET status='interrupted', updated_at=now() "
                        "WHERE status='running' AND coalesce(kind,'') = ANY(%s)", (_ek,))
            cur.execute("UPDATE public.direct_automation_jobs SET status='queued', updated_at=now() "
                        "WHERE status='claimed' AND coalesce(kind,'') = ANY(%s)", (_ek,))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _ensure_slepki_worker(app):
    """Воркер-пул отдельного direct-slepki-worker.service (scope='slepki'). Владеет ТОЛЬКО
    edit-джобами слепков (kind ∈ _EDIT_KINDS) через общий БД-поллер с scope-фильтром claim.

    Умышленно НЕ поднимает create-set инфраструктуру: НЕТ полного _jobs_db_recover (деструктивен,
    делает sweep/reconcile создания), НЕТ resume/delayed-repair демонов. Поэтому рестарт/деплой
    кода слепков НИКОГДА не трогает очередь создания РК, а рестарт create-worker не трогает слепки
    (его recover/claim исключают edit-виды). Исполнение — та же _is_edit_job → _sed.handle_job
    ветка воркер-цикла; в очередь этого процесса попадают только edit-джобы, т.к. claim scoped."""
    with _CREATE_JOBS_LOCK:
        if _CREATE_WORKER["started"]:
            return
        _CREATE_WORKER["started"] = True
    _jobs_db_init()                                       # схема таблицы (общая)
    _slepki_jobs_recover()                                # crash-cleanup ТОЛЬКО своих edit-джоб
    _ensure_create_watchdog()                             # heartbeat зависших (по in-memory этого процесса)
    _create_watchdog_tick()
    workers = int(_CREATE_WORKERS or _create_workers_count())
    for _ in range(workers):
        threading.Thread(target=_create_worker_loop, args=(app,), daemon=True).start()


def _slepki_worker_bootstrap(app) -> None:
    """Точка входа slepki_worker_main: пул воркеров (scope=slepki) + БД-поллер edit-джоб."""
    _ensure_slepki_worker(app)
    _ensure_worker_poller(app)                            # поллер: claim scoped на edit-виды


def _worker_claim_web_jobs() -> list:
    """Атомарно клеймит web-posted queued-джобы: queued→claimed RETURNING (защита от двойного клейма)."""
    import psycopg2.extras
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            # scope='slepki' → берём ТОЛЬКО edit-джобы слепков (kind ∈ _EDIT_KINDS);
            # scope='create' (дефолт) → берём всё КРОМЕ copy_campaigns и edit-видов слепков.
            # ⛔ kind <> 'copy_campaigns': очередь КОПИРОВАНИЯ принадлежит direct-copy.service и
            # исполняется им же (DIRECT_ROLE=all). Без этого фильтра воркер СОЗДАНИЯ забирал
            # copy-джобы себе — со своим стейл-кэшем модуля direct_copy и своим in-memory
            # статусом, из-за чего /api/copy_status вечно показывал queued (факт: 7 copy-джоб с
            # _web_posted=true исполнены create-воркером до 2026-07-17 03:21 UTC).
            # ⛔ NOT (kind = ANY edit): edit-джобы слепков с 2026-07-17 обслуживает отдельный
            # direct-slepki-worker.service (Фаза 2). Раньше изоляция держалась ТОЛЬКО на env
            # DIRECT_ROLE; теперь она структурная — по kind в общей таблице.
            _ek = _edit_kinds_list()
            if _worker_scope() == "slepki":
                _kind_pred = "AND coalesce(kind,'') = ANY(%s) "
            else:
                _kind_pred = "AND coalesce(kind,'') <> 'copy_campaigns' AND NOT (coalesce(kind,'') = ANY(%s)) "
            cur.execute(
                "UPDATE public.direct_automation_jobs SET status='claimed', updated_at=now() "
                "WHERE job_id IN ("
                "    SELECT job_id FROM public.direct_automation_jobs "
                "     WHERE status='queued' AND coalesce(body->>'_web_posted','')='true' "
                "       " + _kind_pred +
                "     ORDER BY (coalesce(body->>'_priority','')='true') DESC, created_at "
                "     LIMIT 10 FOR UPDATE SKIP LOCKED) "
                "RETURNING job_id, login, total, body", (_ek,))
            rows = cur.fetchall() or []
            conn.commit()
            return rows
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return []

def _worker_adopt_job(app, row) -> None:
    """Завести заклеймленную web-джобу в in-memory очередь воркера (status back → 'queued').

    ⚠️ Гейт «уже в памяти» проверяет РЕАЛЬНОЕ участие (в _CREATE_QUEUE или running), а не голое
    наличие в _CREATE_JOBS: стартовый загрузчик истории (см. ~строка 716) поднимает из БД ВСЕ
    незавершённые джобы как записи-карточки БЕЗ постановки в очередь → старый гейт `jid in
    _CREATE_JOBS` молча пропускал адопт и джоба зависала в 'claimed' НАВСЕГДА (root-cause
    инцидента f64fc17a3ae5, 2026-07-06: воспроизводилось при КАЖДОМ рестарте с queued web-джобой
    в БД). Стале-запись перезаписываем и ставим в очередь."""
    jid = row["job_id"]
    _term = None
    with _CREATE_JOBS_LOCK:
        _mem = _CREATE_JOBS.get(jid)
        if _mem is not None and (jid in _CREATE_QUEUE or _mem.get("status") == "running"):
            return                                        # реально в очереди/исполняется
        if _mem is not None and _mem.get("status") in _JOB_TERMINAL:
            _term = dict(_mem)
    if _term is not None:
        # Джоба УЖЕ terminal в ЭТОМ процессе (done/error/cancelled), а в БД остался стале
        # 'queued'/'claimed' (сбой финального _job_db_save / cancel без сейва) → НЕ переисполнять
        # (повторный прогон = ДУБЛИ кампаний в кабинете клиента, ревью 06.07), а досинхронизировать
        # терминальный статус в БД, чтобы поллер перестал её клеймить.
        _job_db_save(jid, _term)
        return
    body = row.get("body") or {}
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:  # noqa: BLE001
            _job_db_set_status(jid, "error", "битый body web-джобы"); return
    login = row.get("login") or ""
    items = body.get("items") or []
    total = int(row.get("total") or len(items))
    saved_session = body.get("_session_snapshot") or {"logged_in": True, "is_admin": True}
    body["_job_id"] = jid
    with _CREATE_JOBS_LOCK:
        job = {"status": "queued", "login": login, "done": 0,
               "total": total, "created": 0, "failed": 0,
               "set_done": 0, "set_total": total,
               "result": None, "error": None, "cancel": False,
               "kind": _job_kind(body), "publish": bool(body.get("launch")),
               "stream_content": bool(body.get("stream_content")),
               "step": None, "_id": jid, "body": body, "session": saved_session,
               "agency": body.get("agency"), "_heartbeat": time.time()}
        _CREATE_JOBS[jid] = job
        if body.get("_priority"):
            _CREATE_QUEUE.insert(0, jid)                  # добивка/доставка — впереди обычных наборов
        else:
            _CREATE_QUEUE.append(jid)
        _CREATE_COND.notify()
    _job_db_save(jid, job)                                # claimed → queued (running проставит воркер)
    if body.get("_kind") not in _sed._EDIT_KINDS:         # edit-джобам не нужен прогрев логина/куки
        try:
            _prefetch_start(login, body)                 # Фаза 1: греем кэши процесса-ИСПОЛНИТЕЛЯ
        except Exception:  # noqa: BLE001
            pass

def _worker_expire_awaiting_feed() -> None:
    """web-роль поставила ожидание решения по фиду; дедлайн истёк → запускаем без фида (worker-время)."""
    # awaiting_feed_decision бывает ТОЛЬКО у создания РК — slepki-worker не трогает эти строки
    # (Фаза 2: изоляция scope, иначе slepki-процесс писал бы в чужие create-джобы каждые 2с).
    if _worker_scope() == "slepki":
        return
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE public.direct_automation_jobs "
                "SET status='queued', "
                "    body = jsonb_set(body - '_feed_deadline', '{_skip_feed_types}', "
                "                     '[\"product\",\"master\"]'::jsonb), "
                "    updated_at=now() "
                "WHERE status='awaiting_feed_decision' "
                "  AND coalesce((body->>'_feed_deadline')::double precision, 0) > 0 "
                "  AND (body->>'_feed_deadline')::double precision < extract(epoch from now())")
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass

def _worker_apply_controls() -> None:
    """Применить команды web→worker из колонки control (сейчас: 'cancel' running-джобы) и обнулить её.

    scope-фильтр ОБЯЗАТЕЛЕН (Фаза 2): без него slepki-worker читал бы control ЧУЖИХ create-джоб,
    не находил их в своей памяти (j=None) и всё равно обнулял control → create-worker не видел
    cancel и отменённая джоба продолжала исполняться (гонка двух поллеров). Каждый воркер трогает
    только control джоб СВОЕГО scope: slepki → edit-виды; create → всё кроме copy_campaigns/edit."""
    import psycopg2.extras
    _ek = _edit_kinds_list()
    if _worker_scope() == "slepki":
        _ctl_pred = "AND coalesce(kind,'') = ANY(%s)"
    else:
        _ctl_pred = "AND coalesce(kind,'') <> 'copy_campaigns' AND NOT (coalesce(kind,'') = ANY(%s))"
    rows = []
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT job_id, control FROM public.direct_automation_jobs "
                        "WHERE control IS NOT NULL " + _ctl_pred, (_ek,))
            rows = cur.fetchall() or []
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return
    for r in rows:
        jid = r["job_id"]
        ctrl = (r.get("control") or "").strip()
        _control_applied = False
        if ctrl == "cancel":
            _cancelled = None
            with _CREATE_COND:
                j = _CREATE_JOBS.get(jid)
                if j is not None:
                    _control_applied = True
                    j["cancel"] = True                    # стоп после текущей кампании item'а
                    if j.get("status") == "queued" and jid in _CREATE_QUEUE:
                        _CREATE_QUEUE.remove(jid)
                        j["status"] = "cancelled"; j["finished_at"] = time.time()
                        _cancelled = dict(j)
                _CREATE_COND.notify_all()
            if _cancelled is not None:
                # Персистим отмену в БД (ревью 06.07): без этого строка остаётся 'queued'
                # (_web_posted) → поллер ре-клеймит её и отменённая джоба ИСПОЛНЯЕТСЯ.
                _job_db_save(jid, _cancelled)
        # feed-решения web-роль применяет напрямую (status flip в БД), поэтому здесь только 'cancel'.
        if not _control_applied:
            continue
        try:
            conn = _victory_conn_rw()
            try:
                cur = conn.cursor()
                cur.execute("UPDATE public.direct_automation_jobs SET control=NULL WHERE job_id=%s", (jid,))
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            pass

def _worker_reclaim_stuck_claimed() -> None:
    """Watchdog: джоба, заклеймленная (queued→claimed), но НЕ заведённая в in-memory очередь
    (исключение в _worker_adopt_job / рестарт между клеймом и адоптом), зависает в 'claimed'
    НАВСЕГДА: клейм берёт только status='queued', а стартовое рекавери claimed→queued работает
    лишь при рестарте воркера. Живой кейс 2026-07-06: f64fc17a3ae5 (доставка остатка Щербаковой,
    7 tp5) висела в claimed без прогресса. Возвращаем в 'queued' claimed старше 5 мин, которых
    НЕТ в _CREATE_JOBS этого процесса (есть в памяти → доведёт адопт/исполнение, не трогаем).
    Троттл 60с — не дёргать Victory каждый 2-секундный тик поллера."""
    if time.time() - _CLAIMED_WATCHDOG_TS["t"] < 60:
        return
    _CLAIMED_WATCHDOG_TS["t"] = time.time()
    try:
        with _CREATE_JOBS_LOCK:
            # «знакомые» = реально в работе (в очереди или исполняются); голая запись-карточка
            # из стартового загрузчика истории — НЕ работа (см. гейт в _worker_adopt_job)
            known = {j for j, v in _CREATE_JOBS.items()
                     if j in _CREATE_QUEUE or (v or {}).get("status") == "running"}
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            # scope-гард: возвращаем в очередь ТОЛЬКО claimed СВОЕГО scope, чтобы create-worker и
            # slepki-worker не дёргали чужие зависшие claimed (иначе гонка «вернул→чужой ре-клеймит»).
            _ek = _edit_kinds_list()
            if _worker_scope() == "slepki":
                _stale_pred = "AND coalesce(kind,'') = ANY(%s)"
            else:
                _stale_pred = "AND coalesce(kind,'') <> 'copy_campaigns' AND NOT (coalesce(kind,'') = ANY(%s))"
            cur.execute("SELECT job_id FROM public.direct_automation_jobs "
                        "WHERE status='claimed' AND updated_at < now() - interval '5 minutes' "
                        + _stale_pred, (_ek,))
            stale = [r[0] for r in (cur.fetchall() or []) if r[0] not in known]
            if stale:
                cur.execute("UPDATE public.direct_automation_jobs SET status='queued', "
                            "updated_at=now() WHERE status='claimed' AND job_id = ANY(%s)", (stale,))
                conn.commit()
                print(f"[claimed-watchdog] зависшие claimed возвращены в очередь: {stale}", flush=True)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — watchdog best-effort, поллер не валим
        pass

def _tg_alert(text: str) -> None:
    """Best-effort Telegram-алерт Direct automation через профиль .secret TG_DIRECT_*."""
    try:
        from loader import load_telegram, send_telegram_message  # noqa: PLC0415
        tg = load_telegram("direct")
        tok, chat = tg.get("bot_token"), tg.get("chat_id")
        if tok and chat:
            send_telegram_message(tok, chat, text)
    except Exception:  # noqa: BLE001 — алерт не критичен, не валим поллер
        pass


def _monitor_stuck_edit_queue() -> None:
    """create-worker (всегда живой) следит: edit-джобы слепков, зависшие в 'queued' дольше
    _EDIT_STUCK_AGE_SEC — признак что direct-slepki-worker упал/не поднялся (иначе правки слепков
    молча копятся без исполнения — find #3 код-ревью). Лог WARNING + троттлённый Telegram-алерт.
    Только scope=create: slepki-worker сам себя мониторить не может (если он мёртв — некому)."""
    if _worker_scope() != "create":
        return
    if time.time() - _EDIT_STUCK_TS["check"] < 60:      # проверяем раз в минуту, не каждый тик
        return
    _EDIT_STUCK_TS["check"] = time.time()
    try:
        _ek = _edit_kinds_list()
        # Свежий коннект (как _worker_reclaim_stuck_claimed рядом), НЕ пул _victory_conn():
        # пулled read-only в поллер-треде занятого воркера отдаёт сбой/таймаут, который глотался
        # except'ом → монитор молчал. _rw-коннект в этой же функции проверенно работает.
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM public.direct_automation_jobs "
                        "WHERE status='queued' AND coalesce(body->>'_web_posted','')='true' "
                        "  AND coalesce(kind,'') = ANY(%s) "
                        "  AND updated_at < now() - make_interval(secs => %s)",
                        (_ek, int(_EDIT_STUCK_AGE_SEC)))
            n = int(cur.fetchone()[0] or 0)
        finally:
            conn.close()
        if n > 0:
            print(f"[edit-queue-monitor] ⚠️ {n} edit-джоб(ы) слепков зависли в 'queued' >"
                  f"{_EDIT_STUCK_AGE_SEC}с — direct-slepki-worker не разбирает очередь?", flush=True)
            if time.time() - _EDIT_STUCK_TS["last_alert"] > _EDIT_STUCK_ALERT_THROTTLE:
                _EDIT_STUCK_TS["last_alert"] = time.time()
                _tg_alert(f"⚠️ Нейродиректолог: {n} правок слепков зависли в очереди "
                          f">{_EDIT_STUCK_AGE_SEC // 60} мин. Проверь direct-slepki-worker.service "
                          f"(systemctl status direct-slepki-worker).")
    except Exception:  # noqa: BLE001 — монитор best-effort, поллер не валим
        pass


def _worker_poll_once(app) -> None:
    _monitor_stuck_edit_queue()
    _worker_expire_awaiting_feed()
    for row in _worker_claim_web_jobs():
        try:
            _worker_adopt_job(app, row)
        except Exception as _ae:  # noqa: BLE001
            # НЕ молчим (фикс 2026-07-06): проглоченный адопт оставлял джобу в 'claimed' навсегда
            # (кейс f64fc17a3ae5). След в журнале + вернёт claimed-watchdog ниже.
            print(f"[worker-adopt] job {row.get('job_id')}: {type(_ae).__name__}: {str(_ae)[:200]}",
                  flush=True)
    _worker_reclaim_stuck_claimed()
    _worker_apply_controls()

def _worker_poll_loop(app) -> None:
    while not _CREATE_DRAIN.get("on"):
        try:
            _worker_poll_once(app)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(_WORKER_POLL_SEC)

def _ensure_worker_poller(app) -> None:
    """Стартует БД-поллер web-posted джоб. Только worker-роль (в 'all' постановка идёт in-memory,
    web-posted джоб нет; в 'web' воркеров нет)."""
    if _direct_role() != "worker":
        return
    with _CREATE_JOBS_LOCK:
        if _WORKER_POLLER["started"]:
            return
        _WORKER_POLLER["started"] = True
    threading.Thread(target=_worker_poll_loop, args=(app,), daemon=True).start()

def _worker_bootstrap(app) -> None:
    """Точка входа worker_main: поднять пул воркеров, все демоны и БД-поллер."""
    _ensure_create_worker(app)                            # jobs_db_init + recover + watchdog + воркеры + демоны
    _ensure_worker_poller(app)                            # + поллер web-posted джоб из БД

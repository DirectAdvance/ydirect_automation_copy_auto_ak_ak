"""PostgreSQL persistence for create/deferred/repair jobs.

No worker threads or Flask routes live here; queue_server owns lifecycle and mutable queue state.
"""
from __future__ import annotations

import json
import time
import uuid

from .direct_repository import victory_conn as _victory_conn, victory_conn_rw as _victory_conn_rw


_JOB_DB_LAST: dict = {}
_JOB_TERMINAL = ("done", "error", "cancelled", "interrupted")
_CREATE_RUNNING_TIMEOUT = 1200
# ПЕРВЫЙ запуск добивки контента = 180с (решение Семёна 2026-07-18: «первая добивка через 3 мин»).
# ⛔ НЕ «унифицировать» с queue_server._DELAYED_CONTENT_REPAIR_DELAY_SECONDS (300с) — числа РАЗНЫЕ
# НАМЕРЕННО: здесь первый проход, там повторный. Почему не меньше: контент (объявления/ключи/
# картинки) оседает в кабинете 5-10+ мин (STATE.md:155-180) — слишком ранний проход «чинит» то,
# что и так привяжется само, и выносит ложный вердикт «не починилось».
# Демон поллит раз в 60с (_DELAYED_REPAIR_POLL) → фактическая задержка = 180с + до 60с = 180-240с.
_DELAYED_CONTENT_REPAIR_DELAY_SECONDS = 180


def _job_touch(job: dict | None) -> None:
    if job:
        job["_heartbeat"] = time.time()


def _job_agency(job: dict) -> str:
    return ((job.get("body") or {}).get("agency") or "").strip().lower()


def _missing(*_args, **_kwargs):
    raise RuntimeError("job_repository dependency is not configured")


_child_parent_ref = _parent_absorb_child_progress = _account_ctx = _missing


def configure(deps: dict) -> None:
    globals().update(deps)


def _jobs_db_init() -> None:
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS public.direct_automation_jobs (
                    job_id     text PRIMARY KEY,
                    login      text,
                    status     text,
                    total      int DEFAULT 0,
                    done       int DEFAULT 0,
                    created     int DEFAULT 0,
                    failed     int DEFAULT 0,
                    kind       text,
                    publish    boolean DEFAULT false,
                    error      text,
                    result     jsonb,
                    body       jsonb,
                    agency     text,
                    control    text,
                    created_at timestamptz DEFAULT now(),
                    updated_at timestamptz DEFAULT now()
                )""")
            # миграция: добавить колонки если таблица уже существовала без них
            cur.execute("ALTER TABLE public.direct_automation_jobs ADD COLUMN IF NOT EXISTS body jsonb")
            cur.execute("ALTER TABLE public.direct_automation_jobs ADD COLUMN IF NOT EXISTS agency text")
            cur.execute("ALTER TABLE public.direct_automation_jobs ADD COLUMN IF NOT EXISTS errors_log jsonb")
            # control: команда web→worker (сейчас используется 'cancel' для running-джоб; worker её NULL-ит)
            cur.execute("ALTER TABLE public.direct_automation_jobs ADD COLUMN IF NOT EXISTS control text")
            # Кросс-процессный per-agency гейт (create-worker ↔ copy-worker делят куки/баллы агентства).
            cur.execute("""
                CREATE TABLE IF NOT EXISTS public.direct_agency_active (
                    agency     text PRIMARY KEY,
                    job_id     text NOT NULL,
                    started_at timestamptz NOT NULL DEFAULT now()
                )""")
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass

def _job_db_save(jid: str, job: dict, *, full: bool = False) -> None:
    """UPSERT строки джобы. full=True пишет result (на терминальном статусе).
    body/agency сохраняются только при INSERT (не перетираются при обновлении прогресса)."""
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            res_json = json.dumps(job.get("result"), ensure_ascii=False) if (full and job.get("result")) else None
            body_raw = job.get("body")
            body_json = json.dumps(body_raw, ensure_ascii=False) if body_raw else None
            agency_val = (job.get("agency") or _job_agency(job) or None)
            err_log = job.get("errors_log")
            err_log_json = json.dumps(err_log, ensure_ascii=False) if err_log else None
            cur.execute("""
                INSERT INTO public.direct_automation_jobs
                    (job_id, login, status, total, done, created, failed, kind, publish, error, result, body, agency, errors_log, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                ON CONFLICT (job_id) DO UPDATE SET
                    status=EXCLUDED.status, total=EXCLUDED.total, done=EXCLUDED.done,
                    created=EXCLUDED.created, failed=EXCLUDED.failed, error=EXCLUDED.error,
                    result=COALESCE(EXCLUDED.result, public.direct_automation_jobs.result),
                    body=COALESCE(public.direct_automation_jobs.body, EXCLUDED.body),
                    agency=COALESCE(public.direct_automation_jobs.agency, EXCLUDED.agency),
                    errors_log=COALESCE(EXCLUDED.errors_log, public.direct_automation_jobs.errors_log),
                    updated_at=now()
            """, (jid, job.get("login"), job.get("status"), int(job.get("total") or 0),
                  int(job.get("done") or 0), int(job.get("created") or 0), int(job.get("failed") or 0),
                  job.get("kind"), bool(job.get("publish")), (job.get("error") or None)[:500] if job.get("error") else None,
                  res_json, body_json, agency_val, err_log_json))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass

def _job_db_delete(jid: str) -> None:
    """Удалить строку джобы из БД немедленно (ручная «отмена» завершённой карточки — без ожидания TTL)."""
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM public.direct_automation_jobs WHERE job_id=%s", (jid,))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass

def _job_db_get(jid: str) -> dict | None:
    """Прочитать сохранённую джобу из БД, включая terminal result."""
    if not jid:
        return None
    try:
        conn = _victory_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM public.direct_automation_jobs WHERE job_id=%s", (jid,))
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None

def _job_db_active_by_login(login: str) -> str | None:
    """job_id активной (не завершённой) джобы этого логина в БД — дедуп на web-роли."""
    lg = (login or "").strip()
    if not lg:
        return None
    try:
        conn = _victory_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT job_id FROM public.direct_automation_jobs "
                        "WHERE login=%s AND status NOT IN ('done','error','cancelled','interrupted') "
                        "ORDER BY updated_at DESC LIMIT 1", (lg,))
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None

def _job_db_set_status(jid: str, status: str, error: str | None = None) -> None:
    """Прямая смена статуса джобы в БД (web-роль: отмена queued/awaiting, resolve feed)."""
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE public.direct_automation_jobs "
                        "SET status=%s, error=COALESCE(%s,error), updated_at=now() WHERE job_id=%s",
                        (status, (error[:500] if error else None), jid))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass

def _job_control_set(jid: str, control: str) -> None:
    """Записать команду web→worker в колонку control (worker применит и обнулит)."""
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE public.direct_automation_jobs SET control=%s, updated_at=now() "
                        "WHERE job_id=%s", (control, jid))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass

def _job_db_web_await_feed(jid: str, deadline: float) -> None:
    """web-роль: перевести свежую queued web-джобу в ожидание решения по фиду (дедлайн в body)."""
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE public.direct_automation_jobs "
                "SET status='awaiting_feed_decision', "
                "    body = jsonb_set(COALESCE(body,'{}'::jsonb), '{_feed_deadline}', to_jsonb(%s::double precision)), "
                "    updated_at=now() "
                "WHERE job_id=%s AND status='queued'", (float(deadline), jid))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass

def _job_db_web_resolve_feed(jid: str, decision: str) -> None:
    """web-роль: ответ пользователя по фиду. run_without_feed → _skip_feed_types, затем status='queued'
    (worker подхватит клеймом). _feed_deadline очищаем."""
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            if decision == "run_without_feed":
                cur.execute(
                    "UPDATE public.direct_automation_jobs "
                    "SET status='queued', "
                    "    body = jsonb_set(body - '_feed_deadline', '{_skip_feed_types}', "
                    "                     '[\"product\",\"master\"]'::jsonb), "
                    "    updated_at=now() "
                    "WHERE job_id=%s AND status='awaiting_feed_decision'", (jid,))
            else:  # run_all
                cur.execute(
                    "UPDATE public.direct_automation_jobs "
                    "SET status='queued', body = (body - '_feed_deadline' - '_skip_feed_types'), "
                    "    updated_at=now() "
                    "WHERE job_id=%s AND status='awaiting_feed_decision'", (jid,))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass

def _job_db_list_recent(active_only: bool = False) -> list[dict]:
    """web-роль: живая очередь из БД (аналог in-memory _CREATE_JOBS для обзора)."""
    import psycopg2.extras
    try:
        conn = _victory_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            if active_only:
                cur.execute("SELECT * FROM public.direct_automation_jobs "
                            "WHERE status NOT IN ('done','error','cancelled','interrupted') "
                            "ORDER BY created_at DESC LIMIT 50")
            else:
                cur.execute("SELECT * FROM public.direct_automation_jobs "
                            "ORDER BY updated_at DESC LIMIT 50")
            return list(cur.fetchall() or [])
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return []

def _job_db_ahead(jid: str) -> int:
    """web-роль: сколько активных джоб «впереди» (created_at раньше). Приблизительно, для UI."""
    try:
        conn = _victory_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT count(*) FROM public.direct_automation_jobs a "
                "JOIN public.direct_automation_jobs b ON b.job_id=%s "
                "WHERE a.status IN ('queued','claimed','running','awaiting_feed_decision') "
                "  AND a.created_at < b.created_at", (jid,))
            row = cur.fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return 0

def _job_db_progress(job: dict) -> None:
    """Лёгкий троттлинг-флеш прогресса в БД (не чаще ~4 c на джобу)."""
    jid = job.get("_id")
    if not jid:
        return
    _job_touch(job)
    now = time.monotonic()
    if now - _JOB_DB_LAST.get(jid, 0.0) < 4.0:
        return
    _JOB_DB_LAST[jid] = now
    _job_db_save(jid, job)
    # Live-вливание прогресса дочерней добивки в родительскую карточку (троттлинг тот же ≤4 c).
    _pref = _child_parent_ref(job.get("body"))
    if _pref and _pref != jid:
        _parent_absorb_child_progress(
            _pref, jid, int(job.get("created") or 0), int(job.get("failed") or 0),
            int(job.get("set_done") or job.get("done") or 0), final=False)

def _jobs_db_mark_stale_running(timeout_sec: int = _CREATE_RUNNING_TIMEOUT) -> list[str]:
    """Битые running-джобы в БД (без heartbeat слишком долго) → interrupted.

    Нужен как бэкстоп: после reload/restart или зависшего внешнего cookie/UAC-вызова в UI не
    должно оставаться вечных 'running'.
    """
    stuck: list[str] = []
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE public.direct_automation_jobs
                   SET status='interrupted',
                       error=CASE
                           WHEN coalesce(error,'')='' THEN %s
                           ELSE error
                       END,
                       updated_at=now()
                 WHERE status='running'
                   AND updated_at < now() - make_interval(secs => %s)
                RETURNING job_id
                """,
                (f"watchdog: running без прогресса > {int(timeout_sec // 60)} мин", int(timeout_sec)),
            )
            stuck = [r[0] for r in (cur.fetchall() or [])]
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return []
    return stuck

def _next_units_reset_utc():
    """Следующий сброс суточных баллов Директа = полночь МСК (UTC+3) + буфер 15 мин → aware UTC datetime.
    Полночь МСК = 21:00 UTC. Если уже позже — переносим на завтра."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    target = now.replace(hour=21, minute=15, second=0, microsecond=0)
    if now >= target:
        target = target + timedelta(days=1)
    return target

def _deferred_db_init() -> None:
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS public.direct_deferred_creates (
                    id          text PRIMARY KEY,
                    login       text,
                    agency      text,
                    job_id      text,
                    body        jsonb,
                    n_items     int DEFAULT 0,
                    status      text DEFAULT 'waiting',
                    resume_count int DEFAULT 0,
                    resume_at   timestamptz,
                    note        text,
                    created_at  timestamptz DEFAULT now(),
                    updated_at  timestamptz DEFAULT now()
                )""")
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass

def _deferred_save(login: str, agency: str, body: dict, remaining_items: list,
                   job_id: str | None, resume_count: int = 0,
                   resume_at: str | None = None, exclude_id: str | None = None) -> str | None:
    """Сохранить остаток набора для авто-докрутки после сброса баллов. → id или None.

    resume_at=None (по умолчанию) → докрутка по куке СРАЗУ (now()), как раньше.
    resume_at=ISO-строка (напр. _next_units_reset_utc().isoformat()) — докрутка не раньше
    этого момента: нужна пунктам, которые в принципе НЕ создать по куке (напр. NO_BRAND_
    SEGMENTS_AVAILABLE — сегментный tp5 требует ТОКЕН/M3, не Grid-куку) — тогда caller
    также должен положить body['_resume_via_token']=True (см. _resume_one_deferred)."""
    if not remaining_items:
        return None
    b = dict(body or {})
    b["items"] = remaining_items
    b.pop("_job_id", None)
    did = uuid.uuid4().hex[:12]
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            # ДЕДУП (2026-07-07): одна позиция НЕ должна висеть в двух активных деферредах —
            # двойная постановка (guardrail сработал в двух джобах подряд) давала ДУБЛИ кампаний
            # при докрутке (кейс psm: 8 деферредов на 4 позиции → 8 лишних tp5). Если ЛЮБОЕ из
            # имён remaining_items уже в waiting/resumed деферреде этого логина — возвращаем
            # существующий id, второй не создаём.
            # exclude_id: НЕ дедупить по самой резюмящейся строке. Токен-ретрай сегментного tp5
            # планируется ИЗ resume-джобы, чья строка (body._deferred_id) уже 'resumed' и содержит
            # тот же item → без исключения дедуп вернул бы ЕЁ id (self-reference) → финал пометил бы
            # её done, а новая токен-строка не создалась → tp5 теряется (инцидент 08.07 721641cad7c1).
            # СЛЕПОК В КЛЮЧЕ (2026-07-11): имена item'ов слепок-АГНОСТИЧНЫ (кодируют tp/сегмент/город,
            # НЕ слепок) → на мульти-слепок single-login прогоне defer одного слепка (zubakin) схлопывался
            # в уже созданный deferred другого (gordeeva) по совпавшему имени и ТЕРЯЛСЯ. Дедупим только
            # при совпадении login И слепка (body->>'agent'); COALESCE('') — легаси-строки без agent.
            _slepok_key = str((body or {}).get("agent") or "")
            for _it in remaining_items:
                _nm = str((_it or {}).get("name") or "").strip()
                if not _nm:
                    continue
                if exclude_id:
                    cur.execute(
                        "SELECT id FROM public.direct_deferred_creates "
                        "WHERE login=%s AND status IN ('waiting','resumed') AND id <> %s "
                        "AND COALESCE(body->>'agent','')=%s "
                        "AND body->'items' @> %s::jsonb LIMIT 1",
                        (login, exclude_id, _slepok_key,
                         json.dumps([{"name": _nm}], ensure_ascii=False)))
                else:
                    cur.execute(
                        "SELECT id FROM public.direct_deferred_creates "
                        "WHERE login=%s AND status IN ('waiting','resumed') "
                        "AND COALESCE(body->>'agent','')=%s "
                        "AND body->'items' @> %s::jsonb LIMIT 1",
                        (login, _slepok_key,
                         json.dumps([{"name": _nm}], ensure_ascii=False)))
                _dup = cur.fetchone()
                if _dup:
                    print(f"[deferred-save] {login}: позиция «{_nm[:60]}» уже в активном "
                          f"деферреде {_dup[0]} — дубль не создаём", flush=True)
                    return str(_dup[0])
            if resume_at:
                cur.execute(
                    "INSERT INTO public.direct_deferred_creates "
                    "(id, login, agency, job_id, body, n_items, status, resume_count, resume_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,'waiting',%s,%s::timestamptz)",
                    (did, login, agency, job_id, json.dumps(b, ensure_ascii=False),
                     len(remaining_items), int(resume_count), resume_at))
            else:
                cur.execute(
                    "INSERT INTO public.direct_deferred_creates "
                    "(id, login, agency, job_id, body, n_items, status, resume_count, resume_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,'waiting',%s, now())",   # resume_at=now(): докрутка по куке СРАЗУ
                    (did, login, agency, job_id, json.dumps(b, ensure_ascii=False),
                     len(remaining_items), int(resume_count)))
            conn.commit()
        finally:
            conn.close()
        return did
    except Exception as _e:  # noqa: BLE001
        # НЕ молчим (фикс 2026-07-06): немой None здесь = потерянный набор без следа
        # (guardrail NO_BRAND_SEGMENTS_AVAILABLE обещал докрутку токеном, а деферред не создавался).
        print(f"[deferred-save] {login}: ошибка сохранения остатка ({len(remaining_items)} шт): "
              f"{type(_e).__name__}: {str(_e)[:200]}", flush=True)
        return None

def _deferred_set_status(did: str, status: str, note: str | None = None) -> None:
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE public.direct_deferred_creates SET status=%s, note=COALESCE(%s,note), "
                        "updated_at=now() WHERE id=%s", (status, note, did))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass

def _deferred_bump_resume_at(did: str, hours: int = 1) -> None:
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE public.direct_deferred_creates SET resume_at=now()+(%s||' hours')::interval, "
                        "updated_at=now() WHERE id=%s", (str(int(hours)), did))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass

def _delayed_repair_db_init() -> None:
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS public.direct_delayed_repairs (
                    id            text PRIMARY KEY,
                    parent_job_id text,
                    login         text,
                    agency        text,
                    kind          text,
                    status        text DEFAULT 'waiting',
                    attempts      int DEFAULT 0,
                    run_at        timestamptz,
                    note          text,
                    result        jsonb,
                    created_at    timestamptz DEFAULT now(),
                    updated_at    timestamptz DEFAULT now()
                )""")
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS direct_delayed_repairs_parent_kind_uq
                ON public.direct_delayed_repairs(parent_job_id, kind)
            """)
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass

def _delayed_repair_set_status(did: str, status: str, note: str | None = None,
                               result: dict | None = None) -> None:
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            result_json = json.dumps(result, ensure_ascii=False) if result is not None else None
            cur.execute("""
                UPDATE public.direct_delayed_repairs
                   SET status=%s,
                       note=COALESCE(%s,note),
                       result=COALESCE(%s::jsonb,result),
                       updated_at=now()
                 WHERE id=%s
            """, (status, note, result_json, did))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass

def _delayed_content_repair_save(parent_job_id: str, login: str, agency: str,
                                 *, delay_seconds: int = _DELAYED_CONTENT_REPAIR_DELAY_SECONDS,
                                 kind: str = "content_repair") -> str | None:
    parent_job_id = (parent_job_id or "").strip()
    login = (login or "").strip()
    if not parent_job_id or not login:
        return None
    did = uuid.uuid4().hex[:12]
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO public.direct_delayed_repairs
                    (id, parent_job_id, login, agency, kind, status, attempts, run_at)
                VALUES (%s,%s,%s,%s,%s,'waiting',0,
                        now() + (%s || ' seconds')::interval)
                ON CONFLICT (parent_job_id, kind) DO NOTHING
                RETURNING id
            """, (did, parent_job_id, login, agency or "", kind, str(int(delay_seconds))))
            row = cur.fetchone()
            conn.commit()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None

def _supersede_delayed_repairs_for_login(login: str) -> None:
    """При пользовательском ПЕРЕЗАПУСКЕ набора на тот же login (dedup_login=True, старая джоба
    уже terminal → дедуп не сработал → новая джоба пересоздаёт кампании под новыми campaign_id) —
    старые waiting/running добивки от ПРЕДЫДУЩЕГО прогона гоняют удалённые campaign_id и падают
    с CAMPAIGN_NOT_FOUND, жгут попытки впустую (живой кейс 06.07.2026, porg-7bqj56f4). Best-effort,
    fail-open: сбой БД НЕ должен блокировать постановку новой джобы."""
    login = (login or "").strip()
    if not login:
        return
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("""
                UPDATE public.direct_delayed_repairs
                   SET status='superseded', note='login пересоздан новым набором', updated_at=now()
                 WHERE login=%s AND status IN ('waiting','running')
            """, (login,))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass

def _ready_logins_db_init() -> None:
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS public.direct_ready_logins (
                    id            serial PRIMARY KEY,
                    login         text UNIQUE NOT NULL,
                    loaded_at     timestamptz DEFAULT now(),
                    campaigns     int DEFAULT 0,
                    specialist    text, city text, domain text, site_type text,
                    slepok        text, content_source text, elapsed_seconds int
                )
            """)
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — best-effort, реестр не валит воркер
        pass

def _ready_login_upsert(login: str, *, campaigns: int, slepok: str, content_source: str,
                        elapsed_seconds: int, add: bool = False) -> None:
    """UPSERT строки реестра. add=True (джоба-доставка) — кампании ПРИБАВЛЯЮТСЯ к существующим."""
    login = (login or "").strip()
    if not login:
        return
    acc = _account_ctx(login) or {}
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO public.direct_ready_logins
                    (login, loaded_at, campaigns, specialist, city, domain, site_type,
                     slepok, content_source, elapsed_seconds)
                VALUES (%s, now(), %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (login) DO UPDATE SET
                    loaded_at=now(),
                    campaigns=CASE WHEN %s THEN public.direct_ready_logins.campaigns + EXCLUDED.campaigns
                                   ELSE EXCLUDED.campaigns END,
                    specialist=EXCLUDED.specialist, city=EXCLUDED.city, domain=EXCLUDED.domain,
                    site_type=EXCLUDED.site_type, slepok=EXCLUDED.slepok,
                    content_source=EXCLUDED.content_source,
                    elapsed_seconds=CASE WHEN %s THEN COALESCE(public.direct_ready_logins.elapsed_seconds,0)
                                              + COALESCE(EXCLUDED.elapsed_seconds,0)
                                         ELSE EXCLUDED.elapsed_seconds END
            """, (login, int(campaigns or 0), (acc.get("directologist") or "").strip(),
                  (acc.get("city") or "").strip(), (acc.get("domain") or "").strip(),
                  (acc.get("site_type") or "").strip(), (slepok or "").strip(),
                  (content_source or "").strip(), int(elapsed_seconds or 0),
                  bool(add), bool(add)))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass

def _ready_login_remove(login: str) -> None:
    login = (login or "").strip()
    if not login:
        return
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM public.direct_ready_logins WHERE login=%s", (login,))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass

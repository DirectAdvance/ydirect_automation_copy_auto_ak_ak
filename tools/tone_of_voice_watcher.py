#!/usr/bin/env python3
"""tone_of_voice_watcher.py — отвязанный watcher тон-войс проверки.

ЗАЧЕМ: после КАЖДОЙ завершённой set-джобы автоматически проверить, сгенерирован ли
контент в голосе слепка, и прислать вердикт в личный Telegram — НЕ трогая горячий
путь создания РК. Падение/тормоз проверки не влияет на создание кампаний: это
отдельный процесс, только читающий БД и кабинеты.

КАК:
  * раз в POLL_INTERVAL сек выбирает из public.direct_automation_jobs новые
    kind='set' status='done' джобы с updated_at > курсор и ещё не проверенные;
  * по каждой вызывает check_tone_of_voice.run_check (v5/Grid read + LLM + telegram);
  * помечает обработанные в public.direct_tone_checks (дедуп → без повторов Семёну);
  * курсор (last_seen updated_at) персистится в файле рядом со скриптом.

АНТИ-СПАМ ПРИ ПЕРВОМ СТАРТЕ: если курсора ещё нет — ставим его в NOW() и историю НЕ
проверяем (обрабатываем только джобы, завершившиеся ПОСЛЕ старта watcher).

Запуск (systemd, LXC101):
    /root/venv/bin/python3 -m direct.tools.tone_of_voice_watcher
Одноразовый проход (для отладки):
    /root/venv/bin/python3 -m direct.tools.tone_of_voice_watcher --once
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from pathlib import Path

_THIS = Path(__file__).resolve()
_PKG_ROOT = _THIS.parents[2]          # .../home/seoadvanced
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))
for _p in [_PKG_ROOT, *_PKG_ROOT.parents]:
    if (_p / ".secret" / "loader.py").exists():
        if str(_p / ".secret") not in sys.path:
            sys.path.insert(0, str(_p / ".secret"))
        break

from direct.tools import check_tone_of_voice as chk  # noqa: E402

POLL_INTERVAL = int(os.environ.get("TONE_WATCHER_INTERVAL", "90"))   # сек
STATE_PATH = Path(os.environ.get(
    "TONE_WATCHER_STATE", str(_THIS.parent / ".tone_watcher_cursor")))
# Пометка [TEST] в telegram — по умолчанию боевой режим (без пометки).
TEST_TAG = os.environ.get("TONE_WATCHER_TEST", "0") == "1"


def _log(msg: str) -> None:
    print(f"[tone-watcher] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def _ensure_dedup_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS public.direct_tone_checks ("
            " job_id text PRIMARY KEY,"
            " login text,"
            " agent text,"
            " voice_score int,"
            " verdict text,"
            " checked_at timestamptz NOT NULL DEFAULT now())")
    conn.commit()


def _already_checked(conn, job_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM public.direct_tone_checks WHERE job_id=%s", (job_id,))
        return cur.fetchone() is not None


def _mark_checked(conn, job_id: str, login: str, agent: str,
                  score: int | None, verdict: str | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.direct_tone_checks (job_id, login, agent, voice_score, verdict) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (job_id) DO NOTHING",
            (job_id, login, agent, score, verdict))
    conn.commit()


def _load_cursor() -> str | None:
    try:
        return STATE_PATH.read_text(encoding="utf-8").strip() or None
    except Exception:  # noqa: BLE001
        return None


def _save_cursor(ts_iso: str) -> None:
    try:
        STATE_PATH.write_text(ts_iso, encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        _log(f"cursor save failed: {e}")


def _init_cursor_if_needed(conn) -> str:
    cur_val = _load_cursor()
    if cur_val:
        return cur_val
    with conn.cursor() as cur:
        cur.execute("SELECT now()")
        now_iso = cur.fetchone()[0].isoformat()
    _save_cursor(now_iso)
    _log(f"первый старт: курсор установлен на NOW()={now_iso}, историю не проверяем")
    return now_iso


def _fetch_new_jobs(conn, cursor_iso: str) -> list[tuple]:
    """Новые done set-джобы после курсора, ещё не в direct_tone_checks."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT j.job_id, j.login, j.updated_at "
            "FROM public.direct_automation_jobs j "
            "LEFT JOIN public.direct_tone_checks c ON c.job_id = j.job_id "
            "WHERE j.kind='set' AND j.status='done' "
            "  AND j.updated_at > %s AND c.job_id IS NULL "
            "ORDER BY j.updated_at ASC LIMIT 20", (cursor_iso,))
        return cur.fetchall()


def poll_once() -> int:
    """Один проход. Возвращает число обработанных джоб."""
    conn = chk._victory_cursor()
    processed = 0
    try:
        _ensure_dedup_table(conn)
        cursor_iso = _init_cursor_if_needed(conn)
        jobs = _fetch_new_jobs(conn, cursor_iso)
    finally:
        conn.close()

    if not jobs:
        return 0

    max_ts = cursor_iso
    for job_id, login, updated_at in jobs:
        ts_iso = updated_at.isoformat()
        # повторная страховка от гонок
        conn2 = chk._victory_cursor()
        try:
            if _already_checked(conn2, job_id):
                if ts_iso > max_ts:
                    max_ts = ts_iso
                continue
        finally:
            conn2.close()

        _log(f"проверяю job {job_id} (login {login})")
        score = None
        verdict = None
        agent = ""
        try:
            res = chk.run_check(job_id=job_id, test=TEST_TAG, send=True)
            job = res.get("job") or {}
            agent = job.get("agent") or ""
            v = res.get("verdict") or {}
            score = v.get("voice_score")
            verdict = v.get("verdict") or ("no_content" if res.get("error") == "no content"
                                           else res.get("error"))
            _log(f"job {job_id}: ok={res.get('ok')} score={score} verdict={verdict} "
                 f"alert={res.get('alert')} sent={res.get('sent')}")
        except Exception as e:  # noqa: BLE001
            _log(f"job {job_id} FAILED: {type(e).__name__}: {str(e)[:160]}")
            traceback.print_exc()
            verdict = f"error:{type(e).__name__}"

        # помечаем обработанной в ЛЮБОМ исходе — чтобы не долбить одну джобу и не спамить
        conn3 = chk._victory_cursor()
        try:
            _mark_checked(conn3, job_id, login or "", agent, score, verdict)
        except Exception as e:  # noqa: BLE001
            _log(f"mark_checked failed for {job_id}: {e}")
        finally:
            conn3.close()

        processed += 1
        if ts_iso > max_ts:
            max_ts = ts_iso

    _save_cursor(max_ts)
    return processed


def main() -> int:
    ap = argparse.ArgumentParser(description="Watcher тон-войс проверки set-джоб")
    ap.add_argument("--once", action="store_true", help="один проход и выход")
    args = ap.parse_args()

    if args.once:
        n = poll_once()
        _log(f"once: обработано {n}")
        return 0

    _log(f"старт: интервал {POLL_INTERVAL}с, курсор-файл {STATE_PATH}, test-tag={TEST_TAG}")
    while True:
        try:
            n = poll_once()
            if n:
                _log(f"обработано {n} джоб")
        except Exception as e:  # noqa: BLE001
            _log(f"poll error: {type(e).__name__}: {str(e)[:160]}")
            traceback.print_exc()
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())

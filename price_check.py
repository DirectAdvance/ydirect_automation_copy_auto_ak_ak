"""Сверка и заливка цен Яндекс.Директ ↔ фиды сайтов (веб-версия).

Веб-аналог cron-скриптов ``work/victory_direct_script/yandex_direct_changed price/``:
  • text_in_direct_zapic_v_BD.py        — СВЕРКА (Direct ads.get PriceExtension + YML-фиды);
  • text_in_goodle_zapic_in_direct.py   — ЗАЛИВКА (ads.update PriceExtension).

Отличия от cron-версии:
  • НЕ трогает старые таблицы izmeneniye_tsen_text_in_* (это остаётся у cron-скрипта);
    свои НОВЫЕ таблицы в ad_analytics_bi (Victory): direct_price_check_*.
  • БЕЗ Telegram: расхождения показываются в UI-таблице с фильтром по директологу.
  • Резолв токена агентства — через blueprint._token_for_login (кэш direct_agency_overrides).
  • Параллель: скачивание фидов — свободно; Direct API (ads.get) и заливка (ads.update) —
    только по разным агентским токенам (по образцу _do_balance, ~5 потоков = 5 агентств),
    внутри агентства — троттлинг-паузы (не убираем защиту от 152).
  • Google-таблица НЕ пишется: веб-функционал и старый cron-скрипт — два независимых
    потока с раздельным состоянием. _write_diff_to_sheet оставлена как dead-but-off код
    (нигде не вызывается; PC_WRITE_SHEET по умолчанию выключен).

Задания (проверка/заливка) выполняются фоновым потоком внутри direct-content.service,
статус персистится в таблице direct_price_check_jobs (Victory) — тот же паттерн job_id +
polling, что у редактора контента, но изолированно (не трогает content_jobs/воркер).
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable


# ─────────────────────────────── конфигурация ────────────────────────────────

# Целевая Google-таблица (переходный паритет со старым скриптом). Совпадает с
# TARGET_SPREADSHEET_ID в text_in_direct_zapic_v_BD.py.
PC_SHEET_ID = "1zF8b9Eg6KLEFbl2YUuHUMA9YwiSqWUN3kxjWjykg2Dc"

# Запись в Google-таблицу ОТКЛЮЧЕНА ПОЛНОСТЬЮ. Веб-функционал и старый cron-скрипт
# (text_in_direct_zapic_v_BD.py) — ДВА НЕЗАВИСИМЫХ потока с раздельным состоянием;
# веб-версия НЕ пишет в общий боевой лист директологов ни при каких условиях по умолчанию.
# Функция _write_diff_to_sheet ниже оставлена как dead-but-off код (не вызывается нигде).
# Даже если кто-то выставит PRICE_CHECK_WRITE_SHEET=1 — вызова из run_check_job больше нет.
PC_WRITE_SHEET = (os.environ.get("PRICE_CHECK_WRITE_SHEET", "0").strip().lower()
                  in ("1", "true", "yes", "on"))

# Единый источник маппинга директолог → Telegram-тег (был скопирован в 3 местах).
# ВНИМАНИЕ: используется ТОЛЬКО как подпись/фильтр в UI — никуда не отправляется.
DIRECTOLOG_TELEGRAM = {
    "Гордеева Наталья":    "@TerraIncognita911",
    "Кудерко Семен":       "@SemenKu",
    "Щербакова Наталья":   "@NataliaScherbakova",
    "Караваев Михаил":     "@Lone1y_A1one",
    "Логинов Юрий":        "@geodaliz",
    "Терехов Евгений":     "@terya28",
    "Зубакин Алексей":     "@adv_context",
    "Павлов Алексей":      "@alexito_tmn",
    "Крючкова Елизавета":  "@liza_kryuchkova",
    "Саламахин Иван":      "@Ivanandreevich337",
    "Тумашенко Евгений":   "@etumashenko",
    "Чепелев Никита":      "@AVRITEL",
    "Сергеев Алексей":     "@fr0stx",
}

# Параллель по агентствам (как _do_balance).
PC_AGENCY_WORKERS = int(os.environ.get("PRICE_CHECK_AGENCY_WORKERS") or 5)
# Троттлинг внутри одного агентства между кампаниями/объявлениями (защита от 152/лимитов).
PC_THROTTLE = float(os.environ.get("PRICE_CHECK_THROTTLE") or 0.4)
# Заливка: ретраи (5×10с затем 5×20с — как в старом скрипте).
PC_APPLY_ATTEMPTS_FAST = 5
PC_APPLY_DELAY_FAST = 10
PC_APPLY_ATTEMPTS_SLOW = 5
PC_APPLY_DELAY_SLOW = 20

_ADS_FIELDS = ["Id", "CampaignId", "AdGroupId", "Status", "State", "Type"]
_TEXT_AD_FIELDS = ["Title", "Title2", "Text", "Href", "VCardId", "SitelinkSetId", "DisplayUrlPath"]
_PRICE_EXT_FIELDS = ["Price", "OldPrice", "PriceQualifier", "PriceCurrency"]


# ───────────────────────────── работа с фидами ───────────────────────────────

def _feed_urls_for_domain(domain: str) -> list[str]:
    """URL-шаблоны фидов сайта (см. text_in_direct_zapic_v_BD.py :837-864)."""
    domain = (domain or "").strip().strip("/")
    if not domain:
        return []
    # если в БД лежит уже с http(s) — нормализуем в голый хост
    domain = domain.replace("https://", "").replace("http://", "").split("/")[0]
    return [
        f"https://{domain}/yandex-catalog-custom-name.xml",
        f"https://{domain}/yandex-catalog-model-design-custom-name.xml",
        f"https://{domain}/quiz-dostup-k-rasprodazhe-01-a.xml",
    ]


def _download_feed(feed_url: str, total_timeout: int = 60) -> bytes | None:
    """Скачивает XML-фид с жёстким лимитом суммарного времени (порт download_feed)."""
    import requests
    try:
        deadline = time.time() + total_timeout
        with requests.get(feed_url, timeout=(10, 30), stream=True) as r:
            if r.status_code != 200:
                return None
            chunks = []
            for chunk in r.iter_content(chunk_size=65536):
                if time.time() > deadline:
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except Exception:  # noqa: BLE001
        return None


def _parse_yml_feed(xml_content: bytes) -> list[tuple]:
    """Парсит YML-фид → [(offer_id, url, price, oldprice), ...] (порт parse_yml_feed)."""
    out: list[tuple] = []
    try:
        root = ET.fromstring(xml_content)
    except Exception:  # noqa: BLE001
        return out
    for offer in root.findall(".//offer"):
        url_tag = offer.find("url")
        price_tag = offer.find("price")
        oldprice_tag = offer.find("oldprice")
        url = url_tag.text if url_tag is not None else None
        if url and "?fid=" in url:
            url = url.split("?fid=")[0]
        if not url:
            continue
        offer_id = (offer.get("id") or "").strip() or None

        def _num(tag):
            if tag is None or not tag.text:
                return None
            try:
                return float(tag.text)
            except ValueError:
                return None

        out.append((offer_id, url, _num(price_tag), _num(oldprice_tag)))
    return out


def _fetch_feeds_for_login(domain: str) -> list[tuple]:
    """Скачивает все фиды домена и возвращает офферы (offer_id, url, price, oldprice)."""
    offers: list[tuple] = []
    main_urls = _feed_urls_for_domain(domain)
    if not main_urls:
        return offers
    # Первые два — «основной» фид (берём первый ответивший), третий — доп. (quiz).
    main_candidates = main_urls[:2]
    quiz_url = main_urls[2] if len(main_urls) > 2 else None
    for u in main_candidates:
        content = _download_feed(u)
        if content:
            offers.extend(_parse_yml_feed(content))
            break
    if quiz_url:
        content = _download_feed(quiz_url)
        if content:
            offers.extend(_parse_yml_feed(content))
    return offers


# ───────────────────────────── Direct API (read) ─────────────────────────────

def _api_err_is_152(j: dict) -> bool:
    e = j.get("error")
    if isinstance(e, dict):
        return str(e.get("error_code")) == "152" or e.get("error_code") == 152
    return False


def _fetch_account_ads(v5_call: Callable, token: str, login: str) -> tuple[list[tuple], str | None]:
    """Снимок объявлений аккаунта с ценами PriceExtension.

    Возвращает (rows, error). rows — кортежи для direct_price_check_direct.
    Порт логики get_all_campaigns/get_adgroups/get_ads из cron-скрипта, но
    кампании батчатся по 10 в ads.get/adgroups.get с пагинацией по LimitedBy.
    """
    camp = v5_call("campaigns", "get", token, login,
                   {"SelectionCriteria": {}, "FieldNames": ["Id", "Name", "State", "Status", "Type"]})
    if camp.get("error"):
        return [], _fmt_err(camp)
    campaigns = (camp.get("result") or {}).get("Campaigns") or []
    campaigns = [c for c in campaigns if isinstance(c, dict) and c.get("State") != "ARCHIVED"]
    if not campaigns:
        return [], None
    name_by_id = {c.get("Id"): c.get("Name") or "" for c in campaigns}
    camp_ids = [c.get("Id") for c in campaigns if c.get("Id")]

    # имена групп (батч по 10)
    adgroup_name: dict = {}
    for i in range(0, len(camp_ids), 10):
        chunk = camp_ids[i:i + 10]
        rows, err = _paginate(v5_call, "adgroups", token, login,
                              {"SelectionCriteria": {"CampaignIds": chunk},
                               "FieldNames": ["Id", "Name", "CampaignId"]}, "AdGroups")
        if err:
            return [], err
        for g in rows:
            adgroup_name[g.get("Id")] = g.get("Name") or "Без названия"
        time.sleep(PC_THROTTLE)

    result: list[tuple] = []
    for i in range(0, len(camp_ids), 10):
        chunk = camp_ids[i:i + 10]
        ads, err = _paginate(v5_call, "ads", token, login, {
            "SelectionCriteria": {"CampaignIds": chunk},
            "FieldNames": _ADS_FIELDS,
            "TextAdFieldNames": ["Title", "Title2", "Text", "Href"],
            "TextAdPriceExtensionFieldNames": _PRICE_EXT_FIELDS,
        }, "Ads")
        if err:
            return [], err
        for ad in ads:
            if not isinstance(ad, dict) or ad.get("State") == "ARCHIVED":
                continue
            if ad.get("Type") != "TEXT_AD" or "TextAd" not in ad:
                continue
            text_ad = ad.get("TextAd")
            if not isinstance(text_ad, dict):
                continue
            price_qual = price = old_price = None
            pe = text_ad.get("PriceExtension")
            if isinstance(pe, dict):
                price_qual = pe.get("PriceQualifier", "NONE")
                pr = pe.get("Price", 0)
                price = pr / 1_000_000 if pr else None
                op = pe.get("OldPrice", 0)
                old_price = op / 1_000_000 if op else None
            result.append((
                login, ad.get("CampaignId"), name_by_id.get(ad.get("CampaignId"), ""),
                ad.get("AdGroupId"), adgroup_name.get(ad.get("AdGroupId"), "Без названия"),
                ad.get("Id"), text_ad.get("Title", ""), text_ad.get("Title2", ""),
                text_ad.get("Text", ""), text_ad.get("Href", ""), price_qual,
                price, old_price, ad.get("Type", ""), ad.get("Status", ""), ad.get("State", ""),
            ))
        time.sleep(PC_THROTTLE)
    return result, None


def _paginate(v5_call: Callable, svc: str, token: str, login: str,
              params: dict, collection: str) -> tuple[list, str | None]:
    """GET сервиса v5 с пагинацией по LimitedBy."""
    rows: list = []
    offset = 0
    for _ in range(200):
        p = dict(params)
        page = dict(p.get("Page") or {})
        page.setdefault("Limit", 10000)
        if offset:
            page["Offset"] = offset
        p["Page"] = page
        j = v5_call(svc, "get", token, login, p)
        if j.get("error"):
            return rows, _fmt_err(j)
        res = j.get("result") or {}
        rows.extend(res.get(collection) or [])
        limited_by = res.get("LimitedBy")
        if not limited_by:
            break
        offset = int(limited_by)
    return rows, None


def _fmt_err(j: dict) -> str:
    e = j.get("error")
    if isinstance(e, dict):
        s = e.get("error_string") or e.get("error_detail") or str(e)
        if e.get("error_detail") and e.get("error_string"):
            s = f"{e['error_string']}: {e['error_detail']}"
        return str(s)
    return str(e)


# ─────────────────────────── схема таблиц (Victory) ───────────────────────────

def ensure_price_check_tables(victory_conn_rw: Callable) -> None:
    """CREATE TABLE IF NOT EXISTS для всех таблиц сверки цен (ad_analytics_bi, public)."""
    conn = victory_conn_rw()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS public.direct_price_check_direct (
                    login text NOT NULL,
                    campaign_id bigint,
                    campaign_name text,
                    adgroup_id bigint,
                    adgroup_name text,
                    ad_id bigint NOT NULL,
                    title text,
                    title2 text,
                    text text,
                    url text,
                    price_qualifier text,
                    price numeric,
                    old_price numeric,
                    ad_type text,
                    status text,
                    state text,
                    checked_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (login, ad_id)
                )""")
            cur.execute("CREATE INDEX IF NOT EXISTS direct_price_check_direct_login_url_idx "
                        "ON public.direct_price_check_direct (login, url)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS public.direct_price_check_feed (
                    id bigserial PRIMARY KEY,
                    login text NOT NULL,
                    offer_id text,
                    url text NOT NULL,
                    price numeric,
                    old_price numeric,
                    checked_at timestamptz NOT NULL DEFAULT now()
                )""")
            cur.execute("CREATE INDEX IF NOT EXISTS direct_price_check_feed_login_url_idx "
                        "ON public.direct_price_check_feed (login, url)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS public.direct_price_check_diff (
                    login text NOT NULL,
                    directologist text,
                    url text NOT NULL,
                    active_campaigns int,
                    active_adgroups int,
                    active_ads int,
                    price_direct numeric,
                    oldprice_direct numeric,
                    price_feed numeric,
                    oldprice_feed numeric,
                    price_diff numeric,
                    oldprice_diff numeric,
                    checked_at timestamptz NOT NULL DEFAULT now()
                )""")
            cur.execute("CREATE INDEX IF NOT EXISTS direct_price_check_diff_login_idx "
                        "ON public.direct_price_check_diff (login)")
            cur.execute("CREATE INDEX IF NOT EXISTS direct_price_check_diff_dir_idx "
                        "ON public.direct_price_check_diff (directologist)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS public.direct_price_check_jobs (
                    job_id text PRIMARY KEY,
                    kind text NOT NULL,
                    status text NOT NULL DEFAULT 'queued',
                    created_by text NOT NULL DEFAULT '',
                    logins jsonb NOT NULL DEFAULT '[]'::jsonb,
                    params jsonb NOT NULL DEFAULT '{}'::jsonb,
                    done int NOT NULL DEFAULT 0,
                    total int NOT NULL DEFAULT 0,
                    message text NOT NULL DEFAULT '',
                    error text NOT NULL DEFAULT '',
                    result jsonb NOT NULL DEFAULT '{}'::jsonb,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    started_at timestamptz,
                    finished_at timestamptz
                )""")
            # control — сигнал воркеру между аккаунтами: '' | 'pause' | 'cancel'.
            # status может принимать значение 'paused' (пауза очереди пользователем).
            cur.execute("ALTER TABLE public.direct_price_check_jobs "
                        "ADD COLUMN IF NOT EXISTS control text NOT NULL DEFAULT ''")
        conn.commit()
    except Exception:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        conn.close()


# ───────────────────────────── job-хранилище ─────────────────────────────────

def _job_insert(victory_conn_rw: Callable, job_id: str, kind: str, created_by: str,
                logins: list, params: dict, total: int, status: str = "running") -> None:
    """Создаёт строку job'а. status='running' — сразу выполняется (фон-поток);
    status='queued' — заявка в очередь (ждёт крон-обработчика 20:00, started_at=NULL)."""
    started = "now()" if status == "running" else "NULL"
    conn = victory_conn_rw()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO public.direct_price_check_jobs "
                "(job_id, kind, status, created_by, logins, params, total, started_at) "
                f"VALUES (%s,%s,%s,%s,%s,%s,%s, {started})",
                (job_id, kind, status, created_by, json.dumps(logins, ensure_ascii=False),
                 json.dumps(params, ensure_ascii=False), total))
        conn.commit()
    finally:
        conn.close()


def _job_update(victory_conn_rw: Callable, job_id: str, **fields) -> None:
    if not fields:
        return
    cols, vals = [], []
    for k, v in fields.items():
        if k in ("result", "logins", "params"):
            cols.append(f"{k}=%s::jsonb")
            vals.append(json.dumps(v, ensure_ascii=False, default=str))
        else:
            cols.append(f"{k}=%s")
            vals.append(v)
    vals.append(job_id)
    conn = victory_conn_rw()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE public.direct_price_check_jobs SET {', '.join(cols)} "
                        f"WHERE job_id=%s", tuple(vals))
        conn.commit()
    finally:
        conn.close()


def mark_running(victory_conn_rw: Callable, job_id: str, message: str = "") -> None:
    """queued→running с проставлением started_at (для корректного elapsed). См. пункт 5 code-review."""
    conn = victory_conn_rw()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.direct_price_check_jobs SET status='running', "
                "started_at=COALESCE(started_at, now()), message=%s WHERE job_id=%s",
                (message[:500], job_id))
        conn.commit()
    finally:
        conn.close()


def reconcile_stuck_jobs(victory_conn_rw: Callable, minutes: int = 45) -> int:
    """Помечает джобы, застрявшие в 'running' дольше N минут (рестарт сервиса/зависание фон-потока),
    как 'interrupted' — чтобы не висели вечно. См. пункт 8 code-review. Возвращает число помеченных."""
    conn = victory_conn_rw()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.direct_price_check_jobs SET status='interrupted', finished_at=now(), "
                "error=CASE WHEN error='' THEN 'прервано: сервис перезапущен или задача зависла' ELSE error END "
                "WHERE status='running' "
                "AND COALESCE(started_at, created_at) < now() - make_interval(mins => %s)",
                (int(minutes),))
            n = cur.rowcount
        conn.commit()
        return n
    except Exception:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0
    finally:
        conn.close()


def _job_finish(victory_conn_rw: Callable, job_id: str, status: str,
                result: dict, message: str = "", error: str = "") -> None:
    conn = victory_conn_rw()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.direct_price_check_jobs SET status=%s, result=%s::jsonb, "
                "message=%s, error=%s, finished_at=now() WHERE job_id=%s",
                (status, json.dumps(result, ensure_ascii=False, default=str),
                 message[:500], error[:500], job_id))
        conn.commit()
    finally:
        conn.close()


def job_public(victory_conn: Callable, job_id: str) -> dict | None:
    import psycopg2.extras
    conn = victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT job_id, kind, status, done, total, message, error, result, "
                    "extract(epoch FROM created_at) AS created_at, "
                    "extract(epoch FROM coalesce(finished_at, now()) - coalesce(started_at, created_at)) AS elapsed "
                    "FROM public.direct_price_check_jobs WHERE job_id=%s", (job_id,))
        return cur.fetchone()
    finally:
        conn.close()


def jobs_recent(victory_conn: Callable, limit: int = 30) -> list[dict]:
    import psycopg2.extras
    conn = victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT job_id, kind, status, done, total, message, error, created_by, "
                    "extract(epoch FROM created_at) AS created_at "
                    "FROM public.direct_price_check_jobs ORDER BY created_at DESC LIMIT %s", (limit,))
        return cur.fetchall() or []
    finally:
        conn.close()


def job_control(victory_conn: Callable, job_id: str) -> str:
    """Текущий control-сигнал воркеру ('' | 'pause' | 'cancel'). Дешёвый SELECT
    (вызывается воркером между аккаунтами)."""
    conn = victory_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT control FROM public.direct_price_check_jobs WHERE job_id=%s",
                        (job_id,))
            row = cur.fetchone()
            return (row[0] if row else "") or ""
    finally:
        conn.close()


def request_pause(victory_conn_rw: Callable, job_id: str) -> bool:
    """Пауза задания. queued → сразу 'paused' (крон не заберёт); running → control='pause'
    (воркер остановится на безопасной точке МЕЖДУ аккаунтами и сам переведёт в 'paused').
    Возвращает True если задание было активным (queued/running)."""
    conn = victory_conn_rw()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.direct_price_check_jobs SET "
                "status = CASE WHEN status='queued' THEN 'paused' ELSE status END, "
                "control = CASE WHEN status='running' THEN 'pause' ELSE control END, "
                "message = CASE WHEN status='queued' THEN 'на паузе (снято из очереди)' ELSE message END "
                "WHERE job_id=%s AND status IN ('queued','running')",
                (job_id,))
            n = cur.rowcount
        conn.commit()
        return n > 0
    finally:
        conn.close()


def request_resume(victory_conn_rw: Callable, job_id: str) -> bool:
    """Снять с паузы: 'paused' → 'queued' (крон/воркер снова выполнит), control сброшен.
    Возвращает True если задание было на паузе."""
    conn = victory_conn_rw()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.direct_price_check_jobs SET status='queued', control='', "
                "started_at=NULL, finished_at=NULL, message='возобновлено, ожидает выполнения' "
                "WHERE job_id=%s AND status='paused'",
                (job_id,))
            n = cur.rowcount
        conn.commit()
        return n > 0
    finally:
        conn.close()


def request_delete(victory_conn_rw: Callable, job_id: str) -> bool:
    """Удалить задание из очереди — ТОЛЬКО если оно на паузе (status='paused').
    Переводит в 'cancelled' (скрывается из активной очереди). Возвращает True если удалено,
    False если задание не на паузе (нельзя удалить активное — сначала пауза)."""
    conn = victory_conn_rw()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.direct_price_check_jobs SET status='cancelled', control='', "
                "finished_at=now(), "
                "error=CASE WHEN error='' THEN 'удалено из очереди' ELSE error END "
                "WHERE job_id=%s AND status='paused'",
                (job_id,))
            n = cur.rowcount
        conn.commit()
        return n > 0
    finally:
        conn.close()


def last_check_run(victory_conn: Callable) -> dict | None:
    """Последний успешно завершённый пересчёт сверки (kind='check').

    Возвращает {finished_at: epoch, status} последнего check-job'а со статусом
    done / done_with_errors, либо None если пересчёт ещё ни разу не завершался."""
    import psycopg2.extras
    conn = victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT status, extract(epoch FROM coalesce(finished_at, created_at)) AS finished_at "
            "FROM public.direct_price_check_jobs "
            "WHERE kind='check' AND status IN ('done','done_with_errors') "
            "ORDER BY coalesce(finished_at, created_at) DESC LIMIT 1")
        return cur.fetchone()
    finally:
        conn.close()


# ─────────────────────────── резолв логинов/агентств ─────────────────────────

def logins_for(victory_conn: Callable, *, logins: list[str] | None,
               directologist: str | None, status: str, exclude: list[str],
               all_active: bool = False) -> list[dict]:
    """Возвращает [{login, domain, directologist, agency}] из local_gsheet_sites.

    Явный список logins → берём их. Иначе по директологу. all_active=True → ВСЕ
    активные (для ночной сверки/кнопки «Пересчитать сейчас»). Без всего — [] (защита
    от случайного запуска по всем аккаунтам из ручного UI)."""
    import psycopg2.extras
    from .account_filters import base_account_where
    where = list(base_account_where())
    params: list = []
    if status and status != "__all__":
        where.append("status=%s")
        params.append(status)
    if exclude:
        where.append("(directologist IS NULL OR directologist <> ALL(%s))")
        params.append(exclude)
    if logins:
        where.append("login_key = ANY(%s)")
        params.append(list({l.strip() for l in logins if l and l.strip()}))
    elif directologist:
        where.append("directologist = %s")
        params.append(directologist)
    elif not all_active:
        return []
    conn = victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT DISTINCT ON (login_key) login_key AS login, domain, directologist, "
            "agency_account AS agency FROM public.local_gsheet_sites "
            f"WHERE {' AND '.join(where)} ORDER BY login_key, domain",
            tuple(params))
        return cur.fetchall() or []
    finally:
        conn.close()


def active_logins(victory_conn: Callable, *, status: str, exclude: list[str]) -> list[dict]:
    """ВСЕ активные Авто-логины (для ночной сверки 02:00 и кнопки «Пересчитать сейчас»)."""
    return logins_for(victory_conn, logins=None, directologist=None,
                      status=status, exclude=exclude, all_active=True)


def enqueue_apply(victory_conn_rw: Callable, created_by: str, items: list[dict]) -> str:
    """Ставит заявку на заливку в очередь (status='queued'). НЕ вызывает ads.update —
    реальное применение делает крон-обработчик в 20:00 Екб. Возвращает job_id."""
    job_id = new_job_id()
    logins = sorted({(it.get("login") or "").strip() for it in items if it.get("login")})
    _job_insert(victory_conn_rw, job_id, "apply", created_by, logins,
                {"items": items}, len(items), status="queued")
    return job_id


def queued_apply_jobs(victory_conn: Callable) -> list[dict]:
    """Заявки на заливку в статусе queued (для крон-обработчика 20:00)."""
    import psycopg2.extras
    conn = victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT job_id, params FROM public.direct_price_check_jobs "
                    "WHERE kind='apply' AND status='queued' ORDER BY created_at")
        return cur.fetchall() or []
    finally:
        conn.close()


def apply_queue_for_ui(victory_conn: Callable, limit: int = 100) -> list[dict]:
    """Заявки на изменение цен (kind='apply') для объединённой вкладки «Очередь».

    Ночные автосверки (kind='check'/'auto') НЕ включаем — они фоновые, в очереди не нужны.
    """
    import psycopg2.extras
    conn = victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT job_id, kind, status, created_by, done, total, message, error, control, "
            "extract(epoch FROM created_at) AS created_at, "
            "extract(epoch FROM coalesce(finished_at, now()) - coalesce(started_at, created_at)) AS elapsed "
            "FROM public.direct_price_check_jobs "
            "WHERE kind='apply' AND status <> 'cancelled' "     # удалённые из очереди скрываем
            "AND (status IN ('queued','running','paused') "
            "OR created_at > now() - interval '48 hours') "
            "ORDER BY created_at DESC LIMIT %s", (limit,))
        return cur.fetchall() or []
    finally:
        conn.close()


def _group_by_agency(items: list[dict], token_for_login: Callable, direct_tokens: Callable):
    """Группирует записи по реальному агентскому токену (token_for_login).

    Возвращает {agency: [(item, token), ...]} + список записей без токена."""
    tokens = direct_tokens()
    by_agency: dict = {}
    no_token: list[dict] = []
    for it in items:
        login = (it.get("login") or "").strip()
        token, agency = token_for_login(login, (it.get("agency") or ""), tokens)
        if not token:
            no_token.append(it)
            continue
        by_agency.setdefault(agency or "?", []).append((it, token))
    return by_agency, no_token


# ─────────────────────────── СВЕРКА (job) ────────────────────────────────────

def run_check_job(deps: dict, job_id: str, items: list[dict]) -> None:
    """Фоновая задача сверки: снимок Direct + фиды → таблицы → diff → (Sheet)."""
    victory_conn_rw = deps["victory_conn_rw"]
    token_for_login = deps["token_for_login"]
    direct_tokens = deps["direct_tokens"]
    v5_call = deps["v5_call"]

    logins = [it["login"] for it in items]
    dir_by_login = {it["login"]: (it.get("directologist") or "") for it in items}
    result = {"logins": len(logins), "ads": 0, "offers": 0, "diffs": 0, "errors": []}
    counters = {"done": 0}
    lock = threading.Lock()

    by_agency, no_token = _group_by_agency(items, token_for_login, direct_tokens)
    for it in no_token:
        result["errors"].append(f"{it['login']}: не найден агентский токен")

    # ── ЭТАП 1: фиды параллельно, ОТДЕЛЬНЫМ неограниченным пулом по уникальным доменам ──
    # Скачивание фида — чистый HTTP (не тратит баллы) и медленный/мёртвый фид не должен
    # тормозить троттлинг-очередь агентства. Кэшируем по домену (два логина могут шарить домен).
    domains = sorted({(it.get("domain") or "").strip() for it in items if (it.get("domain") or "").strip()})
    feed_cache: dict[str, list] = {}
    if domains:
        _job_update(victory_conn_rw, job_id, message=f"скачивание фидов: 0/{len(domains)} доменов")
        fdone = {"n": 0}
        with ThreadPoolExecutor(max_workers=min(32, max(4, len(domains)))) as fex:
            futs = {fex.submit(_fetch_feeds_for_login, d): d for d in domains}
            for f in as_completed(futs):
                dom = futs[f]
                try:
                    feed_cache[dom] = f.result()
                except Exception:  # noqa: BLE001
                    feed_cache[dom] = []
                with lock:
                    fdone["n"] += 1

    # ── ЭТАП 2: Direct API (ads.get) + снимок + diff — параллель ТОЛЬКО по агентствам ──
    def _process_login(it: dict, token: str) -> None:
        login = it["login"]
        domain = (it.get("domain") or "").strip()
        try:
            ads_rows, err = _fetch_account_ads(v5_call, token, login)
            offers = feed_cache.get(domain, [])          # фид уже скачан на этапе 1
            _write_snapshot(victory_conn_rw, login, ads_rows, offers)
            diffs = _compute_and_store_diff(victory_conn_rw, login, dir_by_login.get(login, ""))
            with lock:
                result["ads"] += len(ads_rows)
                result["offers"] += len(offers)
                result["diffs"] += diffs
                if err:
                    result["errors"].append(f"{login}: Direct API — {err}")
        except Exception as e:  # noqa: BLE001
            with lock:
                result["errors"].append(f"{login}: {e}")
        finally:
            with lock:
                counters["done"] += 1
            _job_update(victory_conn_rw, job_id, done=counters["done"],
                        message=f"обработано {counters['done']}/{len(logins)}")

    def _agency_worker(pairs: list[tuple]) -> None:
        # внутри одного агентства — последовательно (троттлинг общий на токен)
        for it, token in pairs:
            _process_login(it, token)
            time.sleep(PC_THROTTLE)

    try:
        with ThreadPoolExecutor(max_workers=PC_AGENCY_WORKERS) as ex:
            futs = [ex.submit(_agency_worker, pairs) for pairs in by_agency.values()]
            for f in as_completed(futs):
                f.result()

        # Google-таблица НЕ пишется: веб-поток независим от старого cron-скрипта.
        # (_write_diff_to_sheet — dead-but-off, вызов намеренно удалён.)
        status = "done" if not result["errors"] else "done_with_errors"
        msg = (f"сверка: {result['ads']} объявлений, {result['offers']} офферов, "
               f"{result['diffs']} строк расхождений")
        _job_finish(victory_conn_rw, job_id, status, result, message=msg)
    except Exception as e:  # noqa: BLE001
        _job_finish(victory_conn_rw, job_id, "error", result,
                    error=str(e), message="сверка прервана: " + str(e)[:200])
        traceback.print_exc()


def _write_snapshot(victory_conn_rw: Callable, login: str,
                    ads_rows: list[tuple], offers: list[tuple]) -> None:
    """Идемпотентно: удаляем прошлый снимок логина и вставляем новый."""
    from psycopg2.extras import execute_values
    conn = victory_conn_rw()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM public.direct_price_check_direct WHERE login=%s", (login,))
            if ads_rows:
                execute_values(cur,
                    "INSERT INTO public.direct_price_check_direct "
                    "(login, campaign_id, campaign_name, adgroup_id, adgroup_name, ad_id, "
                    "title, title2, text, url, price_qualifier, price, old_price, ad_type, "
                    "status, state) VALUES %s "
                    "ON CONFLICT (login, ad_id) DO NOTHING", ads_rows)
            cur.execute("DELETE FROM public.direct_price_check_feed WHERE login=%s", (login,))
            if offers:
                feed_rows = [(login, oid, url, price, old) for (oid, url, price, old) in offers]
                execute_values(cur,
                    "INSERT INTO public.direct_price_check_feed "
                    "(login, offer_id, url, price, old_price) VALUES %s", feed_rows)
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
        raise
    finally:
        conn.close()


# SQL сравнения Direct vs Feed — порт get_report_data(), но per-login (account=login),
# читает НОВЫЕ таблицы и матчит фид только этого же логина (temp table из его офферов).
_DIFF_QUERY = """
WITH
url_price_metrics AS (
    SELECT login AS account, url,
        SPLIT_PART(SPLIT_PART(url, '?', 1), '#', 1) AS url_display,
        price AS price_direct, old_price AS oldprice_direct,
        COUNT(DISTINCT CASE WHEN state='ON' THEN campaign_id END) AS active_campaigns,
        COUNT(DISTINCT CASE WHEN state='ON' THEN adgroup_id END) AS active_adgroups,
        COUNT(DISTINCT CASE WHEN state='ON' THEN ad_id END) AS active_ads
    FROM public.direct_price_check_direct
    WHERE login=%(login)s AND (price IS NOT NULL OR old_price IS NOT NULL)
    GROUP BY login, url, price, old_price
),
direct_parsed AS (
    SELECT DISTINCT url,
        RTRIM(url, '/') AS url_rtrim,
        SPLIT_PART(url, '#', 1) AS url_clean,
        SPLIT_PART(url, '#', 2) AS fragment,
        RTRIM(SPLIT_PART(SPLIT_PART(url, '?', 1), '#', 1), '/') AS url_no_utm_rtrim,
        SPLIT_PART(SPLIT_PART(url, '?', 1), '#', 1) AS url_no_utm,
        price AS price_direct, old_price AS oldprice_direct
    FROM public.direct_price_check_direct
    WHERE login=%(login)s AND (price IS NOT NULL OR old_price IS NOT NULL)
),
direct_with_parents AS (
    SELECT *,
        REGEXP_REPLACE(fragment, '[^/]+/?$', '') AS p1,
        REGEXP_REPLACE(REGEXP_REPLACE(fragment, '[^/]+/?$', ''), '[^/]+/?$', '') AS p2,
        REGEXP_REPLACE(REGEXP_REPLACE(REGEXP_REPLACE(fragment, '[^/]+/?$', ''), '[^/]+/?$', ''), '[^/]+/?$', '') AS p3,
        (SPLIT_PART(url, '#', 1) ~ '^https?://[^/]+/?$') AS is_root_url
    FROM direct_parsed
),
url_feed_prices AS (
    SELECT d.url AS url_direct, d.price_direct, d.oldprice_direct,
        COALESCE(
            CASE WHEN d.is_root_url THEN (SELECT MIN(f.price) FROM _pc_fid f
                WHERE f.url_base LIKE REGEXP_REPLACE(d.url_clean, '/?$', '') || '%%') END,
            (SELECT MIN(f.price) FROM _pc_fid f WHERE f.url_rtrim = d.url_rtrim),
            (SELECT MIN(f.price) FROM _pc_fid f WHERE f.url_rtrim = d.url_no_utm_rtrim),
            CASE WHEN d.p1 <> '' THEN (SELECT MIN(f.price) FROM _pc_fid f
                WHERE f.url_base = d.url_clean AND f.url_fragment LIKE d.p1 || '%%') END,
            CASE WHEN d.p2 <> '' THEN (SELECT MIN(f.price) FROM _pc_fid f
                WHERE f.url_base = d.url_clean AND f.url_fragment LIKE d.p2 || '%%') END,
            CASE WHEN d.p3 <> '' THEN (SELECT MIN(f.price) FROM _pc_fid f
                WHERE f.url_base = d.url_clean AND f.url_fragment LIKE d.p3 || '%%') END,
            (SELECT MIN(f.price) FROM _pc_fid f WHERE f.url_base = d.url_clean),
            (SELECT MIN(f.price) FROM _pc_fid f WHERE f.url_base LIKE RTRIM(d.url_clean, '/') || '/%%'),
            (SELECT MIN(f.price) FROM _pc_fid f WHERE f.url_clean LIKE d.url_no_utm_rtrim || '/%%')
        ) AS price_feed,
        COALESCE(
            CASE WHEN d.is_root_url THEN (SELECT MIN(f.oldprice) FROM _pc_fid f
                WHERE f.url_base LIKE REGEXP_REPLACE(d.url_clean, '/?$', '') || '%%') END,
            (SELECT MIN(f.oldprice) FROM _pc_fid f WHERE f.url_rtrim = d.url_rtrim),
            (SELECT MIN(f.oldprice) FROM _pc_fid f WHERE f.url_rtrim = d.url_no_utm_rtrim),
            CASE WHEN d.p1 <> '' THEN (SELECT MIN(f.oldprice) FROM _pc_fid f
                WHERE f.url_base = d.url_clean AND f.url_fragment LIKE d.p1 || '%%') END,
            CASE WHEN d.p2 <> '' THEN (SELECT MIN(f.oldprice) FROM _pc_fid f
                WHERE f.url_base = d.url_clean AND f.url_fragment LIKE d.p2 || '%%') END,
            CASE WHEN d.p3 <> '' THEN (SELECT MIN(f.oldprice) FROM _pc_fid f
                WHERE f.url_base = d.url_clean AND f.url_fragment LIKE d.p3 || '%%') END,
            (SELECT MIN(f.oldprice) FROM _pc_fid f WHERE f.url_base = d.url_clean),
            (SELECT MIN(f.oldprice) FROM _pc_fid f WHERE f.url_base LIKE RTRIM(d.url_clean, '/') || '/%%'),
            (SELECT MIN(f.oldprice) FROM _pc_fid f WHERE f.url_clean LIKE d.url_no_utm_rtrim || '/%%')
        ) AS oldprice_feed
    FROM direct_with_parents d
)
SELECT m.account, m.url_display AS url, m.active_campaigns, m.active_adgroups, m.active_ads,
    m.price_direct, m.oldprice_direct, f.price_feed, f.oldprice_feed,
    CASE WHEN f.price_feed IS NOT NULL AND m.price_direct IS NOT NULL
         THEN f.price_feed - m.price_direct ELSE NULL END AS price_diff,
    CASE WHEN f.oldprice_feed IS NOT NULL AND m.oldprice_direct IS NOT NULL
         THEN f.oldprice_feed - m.oldprice_direct ELSE NULL END AS oldprice_diff
FROM url_price_metrics m
LEFT JOIN url_feed_prices f
    ON m.url = f.url_direct AND m.price_direct = f.price_direct
    AND (m.oldprice_direct = f.oldprice_direct OR (m.oldprice_direct IS NULL AND f.oldprice_direct IS NULL))
ORDER BY m.url, m.price_direct, m.oldprice_direct
"""


def _compute_and_store_diff(victory_conn_rw: Callable, login: str, directologist: str) -> int:
    """Считает расхождения Direct↔Feed для логина и пишет в direct_price_check_diff."""
    from psycopg2.extras import execute_values
    conn = victory_conn_rw()
    try:
        with conn.cursor() as cur:
            # temp table фида ТОЛЬКО этого логина (изоляция матчинга по домену)
            cur.execute("DROP TABLE IF EXISTS _pc_fid")
            cur.execute("""
                CREATE TEMP TABLE _pc_fid AS
                SELECT url, price, old_price AS oldprice,
                    RTRIM(url, '/') AS url_rtrim,
                    SPLIT_PART(url, '#', 1) AS url_base,
                    SPLIT_PART(SPLIT_PART(url, '?', 1), '#', 1) AS url_clean,
                    SPLIT_PART(url, '#', 2) AS url_fragment
                FROM public.direct_price_check_feed WHERE login=%s""", (login,))
            cur.execute("CREATE INDEX ON _pc_fid(url_rtrim)")
            cur.execute("CREATE INDEX ON _pc_fid(url_rtrim text_pattern_ops)")
            cur.execute("CREATE INDEX ON _pc_fid(url_base)")
            cur.execute("CREATE INDEX ON _pc_fid(url_base text_pattern_ops)")
            cur.execute("CREATE INDEX ON _pc_fid(url_clean text_pattern_ops)")
            cur.execute(_DIFF_QUERY, {"login": login})
            rows = cur.fetchall()
            cur.execute("DELETE FROM public.direct_price_check_diff WHERE login=%s", (login,))
            if rows:
                out = [(login, directologist, r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10])
                       for r in rows]
                execute_values(cur,
                    "INSERT INTO public.direct_price_check_diff "
                    "(login, directologist, url, active_campaigns, active_adgroups, active_ads, "
                    "price_direct, oldprice_direct, price_feed, oldprice_feed, price_diff, oldprice_diff) "
                    "VALUES %s", out)
            cur.execute("DROP TABLE IF EXISTS _pc_fid")
        conn.commit()
        return len(rows)
    except Exception:  # noqa: BLE001
        conn.rollback()
        raise
    finally:
        conn.close()


def diff_rows(victory_conn: Callable, *, directologist: str | None,
              login: str | None, only_mismatch: bool, limit: int = 5000) -> list[dict]:
    """Строки расхождений для UI (фильтр по директологу/логину)."""
    import psycopg2.extras
    where = ["1=1"]
    params: list = []
    if directologist:
        where.append("directologist=%s")
        params.append(directologist)
    if login:
        where.append("login=%s")
        params.append(login)
    if only_mismatch:
        where.append("((price_diff IS NOT NULL AND price_diff <> 0) "
                     "OR (oldprice_diff IS NOT NULL AND oldprice_diff <> 0))")
    conn = victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT login, directologist, url, active_campaigns, active_adgroups, active_ads, "
            "price_direct, oldprice_direct, price_feed, oldprice_feed, price_diff, oldprice_diff, "
            "extract(epoch FROM checked_at) AS checked_at "
            f"FROM public.direct_price_check_diff WHERE {' AND '.join(where)} "
            "ORDER BY directologist, login, url LIMIT %s", tuple(params) + (limit,))
        return cur.fetchall() or []
    finally:
        conn.close()


# ─────────────────────────── ЗАЛИВКА (job) ───────────────────────────────────

def _ad_ids_for(victory_conn: Callable, login: str, url: str,
                price_direct, oldprice_direct) -> list[int]:
    """ad_id для заливки: строки снимка с совпадением url/price/old_price
    (порт get_ad_ids_for_update — динамический WHERE по заполненным полям).

    URL матчим по ОЧИЩЕННОМУ виду (без ?query/#fragment), т.к. в diff хранится
    url_display (SPLIT_PART по '?' и '#'), а в снимке — сырой Href; иначе Href с
    трейлинг '?'/utm/фрагментом никогда не совпал бы с очищенным url из UI."""
    parts = ["SELECT DISTINCT ad_id FROM public.direct_price_check_direct WHERE login=%s "
             "AND SPLIT_PART(SPLIT_PART(url, '?', 1), '#', 1) = %s"]
    params: list = [login, url]
    if price_direct is not None:
        parts.append("AND price=%s")
        params.append(price_direct)
    if oldprice_direct is not None:
        parts.append("AND old_price=%s")
        params.append(oldprice_direct)
    conn = victory_conn()
    try:
        cur = conn.cursor()
        cur.execute(" ".join(parts), tuple(params))
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def _get_ad_data(v5_call: Callable, token: str, login: str, ad_id: int) -> dict | None:
    j = v5_call("ads", "get", token, login, {
        "SelectionCriteria": {"Ids": [ad_id]},
        "FieldNames": _ADS_FIELDS,
        "TextAdFieldNames": _TEXT_AD_FIELDS,
        "TextAdPriceExtensionFieldNames": _PRICE_EXT_FIELDS,
    })
    if j.get("error"):
        return None
    ads = (j.get("result") or {}).get("Ads") or []
    return ads[0] if ads else None


def _to_micro(v) -> int:
    """Цена → микроединицы Директа с корректным округлением (не обрезать копейки).
    Decimal + ROUND_HALF_UP: 199999.995 ₽ → 200000000000 мкед, а не 199999994999 (как int())."""
    from decimal import Decimal, ROUND_HALF_UP
    return int((Decimal(str(v)) * 1_000_000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _update_ad_price(v5_call: Callable, token: str, login: str, ad_data: dict,
                     new_price, new_oldprice) -> str:
    """Одна попытка ads.update PriceExtension. → 'success'|'archived'|'failed'|'no_units'.

    Баллы: v5_call шлёт заголовок ``Use-Operator-Units: true`` + агентский токен
    (blueprint._token_for_login) → списывается с пула ОПЕРАТОРА-АГЕНТСТВА (миллионы баллов),
    НЕ с клиента. Поэтому 152 (нехватка баллов) на заливке цен практически недостижима;
    ветка 'no_units' — чисто защитная (если бы вдруг подставился клиентский пул).
    Цена → микроединицы с округлением (_to_micro), а не обрезанием int()."""
    ad_id = ad_data["Id"]
    text_ad = ad_data.get("TextAd") or {}
    pe = text_ad.get("PriceExtension") or {}
    price_api = _to_micro(new_price) if new_price is not None else pe.get("Price", 0)
    oldprice_api = _to_micro(new_oldprice) if new_oldprice is not None else pe.get("OldPrice", 0)
    updated_pe = {
        "Price": price_api,
        "OldPrice": oldprice_api,
        "PriceQualifier": pe.get("PriceQualifier", "FROM"),
        "PriceCurrency": pe.get("PriceCurrency", "RUB"),
    }
    text_ad_update = {
        "Title": text_ad.get("Title"),
        "Title2": text_ad.get("Title2"),
        "Text": text_ad.get("Text"),
        "Href": text_ad.get("Href"),
        "PriceExtension": updated_pe,
    }
    for opt in ("VCardId", "SitelinkSetId", "DisplayUrlPath"):
        if text_ad.get(opt):
            text_ad_update[opt] = text_ad[opt]
    j = v5_call("ads", "update", token, login,
                {"Ads": [{"Id": ad_id, "TextAd": text_ad_update}]})
    if _api_err_is_152(j):
        return "no_units"
    if j.get("error"):
        return "failed"
    upd = (j.get("result") or {}).get("UpdateResults") or []
    if not upd:
        return "failed"
    errors = upd[0].get("Errors") or []
    if errors:
        for e in errors:
            if e.get("Code") == 8300:  # заархивированная кампания
                return "archived"
        return "failed"
    return "success"


def _apply_one_ad(v5_call: Callable, token: str, login: str, ad_id: int,
                  new_price, new_oldprice) -> str:
    """get + update с ретраями (5×10с затем 5×20с). → итоговый статус."""
    ad_data = _get_ad_data(v5_call, token, login, ad_id)
    if not ad_data:
        return "failed"
    schedule = ([PC_APPLY_DELAY_FAST] * PC_APPLY_ATTEMPTS_FAST
                + [PC_APPLY_DELAY_SLOW] * PC_APPLY_ATTEMPTS_SLOW)
    for i, delay in enumerate(schedule):
        res = _update_ad_price(v5_call, token, login, ad_data, new_price, new_oldprice)
        if res in ("success", "archived", "no_units"):
            return res
        if i < len(schedule) - 1:
            time.sleep(delay)
    return "failed"


def run_apply_job(deps: dict, job_id: str, items: list[dict]) -> None:
    """Фоновая заливка: по выбранным строкам расхождений пишем фидовую цену в Директ.

    items: [{login, url, price_direct, oldprice_direct, price_feed, oldprice_feed}, ...].
    """
    victory_conn = deps["victory_conn"]
    victory_conn_rw = deps["victory_conn_rw"]
    token_for_login = deps["token_for_login"]
    direct_tokens = deps["direct_tokens"]
    v5_call = deps["v5_call"]

    # skipped — позиции, НЕ применённые из-за остановки агентства по 152 (отличимы от
    # применённых: список {login,url}, а не только агрегат). См. пункт 1 code-review.
    result = {"items": len(items), "ads_updated": 0, "ads_archived": 0,
              "ads_failed": 0, "no_units_agencies": [], "skipped": [], "errors": []}
    counters = {"done": 0}
    lock = threading.Lock()
    # Сигнал паузы/отмены пользователем: проверяется МЕЖДУ аккаунтами (не рвём ads.update
    # посреди одного аккаунта). None | 'pause' | 'cancel'.
    control_state = {"signal": None}

    # каждому item нужен свой логин → группируем по агентству
    keyed = []
    for it in items:
        keyed.append({"login": (it.get("login") or "").strip(), "agency": it.get("agency") or "",
                      "_item": it})
    by_agency, no_token = _group_by_agency(keyed, token_for_login, direct_tokens)
    for k in no_token:
        result["errors"].append(f"{k['login']}: не найден агентский токен")
        result["skipped"].append({"login": k["login"], "url": (k["_item"].get("url") or ""),
                                   "reason": "no_token"})

    def _agency_worker(agency: str, pairs: list[tuple]) -> None:
        # Баллы агентские (Use-Operator-Units в v5_call) → 152 практически недостижима.
        # Но если она всё же случится — стопим агентство и ЯВНО фиксируем пропущенные позиции
        # (не растворяем их в общем счётчике done).
        stop = False
        for keyed_it, token in pairs:
            it = keyed_it["_item"]
            login = keyed_it["login"]
            # ── пауза/отмена: проверяем ПЕРЕД началом обработки аккаунта ──
            if control_state["signal"] is None:
                try:
                    ctl = job_control(victory_conn, job_id)
                except Exception:  # noqa: BLE001
                    ctl = ""
                if ctl in ("pause", "cancel"):
                    with lock:
                        control_state["signal"] = ctl
            if control_state["signal"] is not None:
                break   # остановка МЕЖДУ аккаунтами — этот аккаунт ещё не тронут
            if stop:
                with lock:
                    result["skipped"].append({"login": login, "url": (it.get("url") or ""),
                                               "reason": "agency_no_units"})
                    counters["done"] += 1
                continue
            new_price = it.get("price_feed")
            new_oldprice = it.get("oldprice_feed")
            try:
                ad_ids = _ad_ids_for(victory_conn, login, it.get("url") or "",
                                     it.get("price_direct"), it.get("oldprice_direct"))
                for ad_id in ad_ids:
                    res = _apply_one_ad(v5_call, token, login, ad_id, new_price, new_oldprice)
                    with lock:
                        if res == "success":
                            result["ads_updated"] += 1
                        elif res == "archived":
                            result["ads_archived"] += 1
                        elif res == "no_units":
                            if agency not in result["no_units_agencies"]:
                                result["no_units_agencies"].append(agency)
                            result["errors"].append(f"{login}: баллы агентства {agency} исчерпаны (152)")
                            result["skipped"].append({"login": login, "url": (it.get("url") or ""),
                                                       "reason": "agency_no_units", "ad_id": ad_id})
                            stop = True
                        else:
                            result["ads_failed"] += 1
                    if stop:
                        break
                    time.sleep(PC_THROTTLE)
            except Exception as e:  # noqa: BLE001
                with lock:
                    result["errors"].append(f"{login}: {e}")
                    result["skipped"].append({"login": login, "url": (it.get("url") or ""),
                                               "reason": f"error: {str(e)[:80]}"})
            finally:
                with lock:
                    counters["done"] += 1
                _job_update(victory_conn_rw, job_id, done=counters["done"],
                            message=f"обработано {counters['done']}/{len(items)} строк")

    try:
        with ThreadPoolExecutor(max_workers=PC_AGENCY_WORKERS) as ex:
            futs = [ex.submit(_agency_worker, ag, pairs) for ag, pairs in by_agency.items()]
            for f in as_completed(futs):
                f.result()
        # ── остановлено пользователем ────────────────────────────────────────
        if control_state["signal"] == "pause":
            _job_update(victory_conn_rw, job_id, status="paused", control="",
                        done=counters["done"],
                        message=f"пауза на {counters['done']}/{len(items)} строк")
            return
        if control_state["signal"] == "cancel":
            _job_finish(victory_conn_rw, job_id, "cancelled", result,
                        message=f"отменено на {counters['done']}/{len(items)} строк")
            return
        status = "done" if not result["errors"] else "done_with_errors"
        skip = len(result["skipped"])
        msg = (f"заливка: обновлено {result['ads_updated']}, архивных {result['ads_archived']}, "
               f"ошибок {result['ads_failed']}"
               + (f", пропущено позиций {skip}" if skip else ""))
        _job_finish(victory_conn_rw, job_id, status, result, message=msg)
    except Exception as e:  # noqa: BLE001
        _job_finish(victory_conn_rw, job_id, "error", result,
                    error=str(e), message="заливка прервана: " + str(e)[:200])
        traceback.print_exc()


# ─────────────────────────── Google Sheet (паритет) ──────────────────────────

def _pc_service_account_path() -> Path | None:
    """service_account.json: ищем .secret/service_account.json и .secret/google/…"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        sd = parent / ".secret"
        if sd.exists():
            for cand in (sd / "service_account.json", sd / "google" / "service_account.json"):
                if cand.exists():
                    return cand
    return None


def _write_diff_to_sheet(victory_conn: Callable, logins: list[str]) -> str:
    """Пишет расхождения в целевую Google-таблицу (по вкладке на директолога).

    Переходный паритет со старым скриптом; отключается флагом PC_WRITE_SHEET.
    Fail-safe: любой сбой (нет gspread/creds/сети) → строка-предупреждение, job не падает.
    """
    if not PC_WRITE_SHEET:
        return ""
    sa_path = _pc_service_account_path()
    if not sa_path:
        return "Sheet пропущен: нет service_account.json"
    try:
        import gspread
        from oauth2client.service_account import ServiceAccountCredentials
    except Exception:  # noqa: BLE001
        return "Sheet пропущен: gspread/oauth2client не установлены"

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(str(sa_path), scope)
    client = gspread.authorize(creds)

    # берём diff только по логинам этого прогона, группируем по директологу
    import psycopg2.extras
    conn = victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT login, directologist, url, active_campaigns, active_adgroups, active_ads, "
            "price_direct, oldprice_direct, price_feed, oldprice_feed, price_diff, oldprice_diff "
            "FROM public.direct_price_check_diff WHERE login = ANY(%s) "
            "ORDER BY directologist, login, url", (list(logins),))
        rows = cur.fetchall() or []
    finally:
        conn.close()

    grouped: dict = {}
    for r in rows:
        grouped.setdefault(r["directologist"] or "Неизвестный", []).append(r)
    if not grouped:
        return "Sheet: нет строк для записи"

    headers = ["Аккаунт", "URL", "Активных кампаний", "Активных групп", "Активных объявлений",
               "Price (Direct)", "Old Price (Direct)", "Price (Feed)", "Old Price (Feed)",
               "Разница Price", "Разница Old Price"]
    ss = client.open_by_key(PC_SHEET_ID)
    # чистим лишние вкладки (кроме первой)
    for ws in ss.worksheets()[1:]:
        try:
            ss.del_worksheet(ws)
        except Exception:  # noqa: BLE001
            pass
    first = True
    written = 0
    for responsible, drows in grouped.items():
        title = (responsible or "Неизвестный")[:90]
        if first:
            sheet = ss.sheet1
            try:
                sheet.update_title(title)
            except Exception:  # noqa: BLE001
                pass
            first = False
        else:
            sheet = ss.add_worksheet(title=title, rows=max(10, len(drows) + 5), cols=11)
        sheet.clear()
        values = [headers]
        for r in drows:
            def _n(v):
                return float(v) if v is not None else ""
            values.append([
                r["login"] or "", r["url"] or "", r["active_campaigns"] or 0,
                r["active_adgroups"] or 0, r["active_ads"] or 0,
                _n(r["price_direct"]), _n(r["oldprice_direct"]),
                _n(r["price_feed"]), _n(r["oldprice_feed"]),
                _n(r["price_diff"]), _n(r["oldprice_diff"]),
            ])
        sheet.update(values=values, range_name="A1")
        written += len(drows)
        time.sleep(2)  # анти-429
    return f"Google Sheet обновлён ({written} строк, {len(grouped)} вкл.)"


# ─────────────────────── запуск фоновых задач ────────────────────────────────

def launch_background(target: Callable, *args) -> None:
    t = threading.Thread(target=target, args=args, daemon=True, name="price-check-job")
    t.start()


def new_job_id() -> str:
    return "pc_" + uuid.uuid4().hex[:12]

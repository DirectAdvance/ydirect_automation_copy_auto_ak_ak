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
# Батч ads.update/ads.get: конвенция v5 для add/update/delete — потолок 1000 элементов
# в массиве; берём с запасом (согласовано 2026-07-10 после инцидента с Тумашенко).
PC_BATCH_SIZE = 900
# Heartbeat заливки: минимальный интервал (сек) между записями message в БД. Крупная заявка
# закрывает первую url-позицию за 10-15 мин (все ad_id всех логинов) → done стоит на 0, шкала
# кажется зависшей. Heartbeat пишет ad-level прогресс в message не чаще раза в интервал, чтобы
# не спамить БД (< 6с UI-полла, но >> per-request rate). started_at НЕ трогаем — reconcile не задет.
PC_HEARTBEAT_SECS = float(os.environ.get("PRICE_CHECK_HEARTBEAT_SECS") or 4)

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
            # run_now — заявка queued отмечена кнопкой «Запустить» (не ждать крон 20:00);
            # см. request_start_now/_claim_next_run_now_apply.
            cur.execute("ALTER TABLE public.direct_price_check_jobs "
                        "ADD COLUMN IF NOT EXISTS run_now boolean NOT NULL DEFAULT false")
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


def mark_running(victory_conn_rw: Callable, job_id: str, message: str = "") -> bool:
    """queued→running с проставлением started_at (для корректного elapsed). См. пункт 5 code-review.

    Guard `status='queued'` обязателен: между SELECT очереди и этим апдейтом заявку могли
    удалить (queued→cancelled кнопкой) — без guard'а мы бы воскресили её обратно в running
    и залили «удалённые» цены. Возвращает True, если заявка реально переведена (была queued)."""
    conn = victory_conn_rw()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.direct_price_check_jobs SET status='running', "
                "started_at=COALESCE(started_at, now()), message=%s "
                "WHERE job_id=%s AND status='queued'",
                (message[:500], job_id))
            n = cur.rowcount
        conn.commit()
        return n > 0
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


def jobs_recent(victory_conn: Callable, limit: int = 30,
                created_by: str | None = None) -> list[dict]:
    """Последние задания сверки цен. created_by=None → все (админ); строка →
    только задания этого пользователя (обычный юзер видит лишь свои)."""
    import psycopg2.extras
    where = ""
    params: list = []
    if created_by is not None:
        where = "WHERE created_by=%s "
        params.append(created_by)
    params.append(limit)
    conn = victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT job_id, kind, status, done, total, message, error, created_by, "
                    "extract(epoch FROM created_at) AS created_at "
                    f"FROM public.direct_price_check_jobs {where}"
                    "ORDER BY created_at DESC LIMIT %s", tuple(params))
        return cur.fetchall() or []
    finally:
        conn.close()


def job_created_by(victory_conn: Callable, job_id: str) -> str | None:
    """created_by задания (для проверки владения на не-админских эндпоинтах).
    None → задание не найдено."""
    conn = victory_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT created_by FROM public.direct_price_check_jobs "
                        "WHERE job_id=%s", (job_id,))
            row = cur.fetchone()
            return (row[0] or "") if row else None
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
    """Удалить задание из очереди — на паузе ИЛИ ещё не стартовавшее (status='queued').
    Активное (running) удалить нельзя — сначала пауза. Переводит в 'cancelled'
    (скрывается из активной очереди). Возвращает True если удалено."""
    conn = victory_conn_rw()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.direct_price_check_jobs SET status='cancelled', control='', "
                "run_now=false, finished_at=now(), "
                "error=CASE WHEN error='' THEN 'удалено из очереди' ELSE error END "
                "WHERE job_id=%s AND status IN ('paused','queued')",
                (job_id,))
            n = cur.rowcount
        conn.commit()
        return n > 0
    finally:
        conn.close()


def cleanup_old_jobs(victory_conn_rw: Callable, keep_days: int = 3) -> int:
    """Удаляет ЗАВЕРШЁННЫЕ задания (done/done_with_errors/error/cancelled) старше
    keep_days суток. Активные (queued/running/paused) не трогает независимо от
    возраста. Возвращает число удалённых строк."""
    conn = victory_conn_rw()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM public.direct_price_check_jobs "
                "WHERE status IN ('done','done_with_errors','error','cancelled') "
                "AND created_at < now() - (%s || ' days')::interval",
                (keep_days,))
            n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def request_start_now(victory_conn_rw: Callable, job_id: str) -> tuple[str, list] | None:
    """Кнопка «Запустить»: queued-заявка не ждёт крон 20:00.

    Если сейчас НЕ выполняется другая apply-заявка — запускается немедленно (status→'running',
    вызывающий обязан launch_background(run_apply_job,...) с возвращёнными items).
    Если уже что-то выполняется — заявка остаётся 'queued', но помечается run_now=true и
    встаёт в конец очереди на автостарт: _chain_next_apply подхватит её сразу, как только
    текущая заливка закончится (не дожидаясь общего крона). Обычные (не отмеченные run_now)
    queued-заявки эта логика не трогает — они как и раньше ждут крона 20:00.
    Возвращает (status, items) или None, если заявки нет / она уже не в 'queued'."""
    import psycopg2.extras
    conn = victory_conn_rw()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Сериализуем решение «стартовать сейчас или встать в очередь»: без этого два
            # одновременных клика (или клик ∥ крон 20:00) оба увидят «running нет» и уйдут
            # в running параллельно — два прогона ударят один агентский токен >1 потоком
            # (152/троттлинг). Лок xact-scoped, снимается на commit ниже.
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('pc_apply_singleton'))")
            cur.execute(
                "UPDATE public.direct_price_check_jobs SET "
                "run_now=true, "
                "status = CASE WHEN NOT EXISTS (SELECT 1 FROM public.direct_price_check_jobs r "
                "                                WHERE r.kind='apply' AND r.status='running') "
                "             THEN 'running' ELSE status END, "
                "started_at = CASE WHEN NOT EXISTS (SELECT 1 FROM public.direct_price_check_jobs r "
                "                                    WHERE r.kind='apply' AND r.status='running') "
                "                  THEN now() ELSE started_at END, "
                "message = CASE WHEN NOT EXISTS (SELECT 1 FROM public.direct_price_check_jobs r "
                "                                 WHERE r.kind='apply' AND r.status='running') "
                "               THEN 'выполняется (запущено вручную)' "
                "               ELSE 'запустится сразу после текущей заливки' END "
                "WHERE job_id=%s AND status='queued' AND kind='apply' "
                "RETURNING status, params",
                (job_id,))
            row = cur.fetchone()
        conn.commit()
        if not row:
            return None
        items = (row["params"] or {}).get("items") or []
        return row["status"], items
    finally:
        conn.close()


def _claim_next_run_now_apply(victory_conn_rw: Callable) -> tuple[str, list] | None:
    """Атомарно забирает следующую run_now-заявку (FIFO по created_at) и переводит
    в 'running'. Вызывается из _chain_next_apply после финиша ручного запуска."""
    import psycopg2.extras
    conn = victory_conn_rw()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Тот же singleton-лок: не забираем следующую run_now, если сейчас уже что-то
            # выполняется (например кроновый пул 20:00 в параллельном процессе).
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('pc_apply_singleton'))")
            cur.execute(
                "UPDATE public.direct_price_check_jobs SET status='running', started_at=now(), "
                "message='выполняется (следующая в ручной очереди)' "
                "WHERE job_id = (SELECT job_id FROM public.direct_price_check_jobs "
                "                 WHERE kind='apply' AND status='queued' AND run_now=true "
                "                 AND NOT EXISTS (SELECT 1 FROM public.direct_price_check_jobs r "
                "                                 WHERE r.kind='apply' AND r.status='running') "
                "                 ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED) "
                "RETURNING job_id, params")
            row = cur.fetchone()
        conn.commit()
        if not row:
            return None
        items = (row["params"] or {}).get("items") or []
        return row["job_id"], items
    finally:
        conn.close()


def _chain_next_apply(deps: dict) -> None:
    """После финиша apply-заявки проверяет, не встала ли за это время в очередь на
    автостарт ('Запустить', run_now=true) другая заявка — и если да, сразу её стартует,
    вместо того чтобы заставлять ждать общего крона 20:00."""
    nxt = _claim_next_run_now_apply(deps["victory_conn_rw"])
    if nxt:
        next_job_id, items = nxt
        launch_background(run_apply_job, deps, next_job_id, items)


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
               all_active: bool = False,
               allowed: list[str] | None = None) -> list[dict]:
    """Возвращает [{login, domain, directologist, agency}] из local_gsheet_sites.

    Явный список logins → берём их. Иначе по директологу. all_active=True → ВСЕ
    активные (для ночной сверки/кнопки «Пересчитать сейчас»). Без всего — [] (защита
    от случайного запуска по всем аккаунтам из ручного UI).

    allowed=None → полный доступ (скоуп не применяется). allowed=[...] → жёсткий
    скоуп по директологу текущего пользователя (как «Обзор»); пустой список → []
    (нет выданных доступов). Скоуп применяется поверх ЛЮБОГО режима (logins/
    directologist/all_active), чтобы обычный юзер не мог достать чужой аккаунт."""
    import psycopg2.extras
    from .account_filters import base_account_where
    if allowed is not None and not allowed:
        return []
    where = list(base_account_where())
    params: list = []
    if status and status != "__all__":
        where.append("status=%s")
        params.append(status)
    if exclude:
        where.append("(directologist IS NULL OR directologist <> ALL(%s))")
        params.append(exclude)
    if allowed is not None:
        where.append("directologist = ANY(%s)")
        params.append(allowed)
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


def active_logins(victory_conn: Callable, *, status: str, exclude: list[str],
                  allowed: list[str] | None = None) -> list[dict]:
    """ВСЕ активные Авто-логины (для ночной сверки 02:00 и кнопки «Пересчитать сейчас»).

    allowed=None → все; allowed=[...] → только аккаунты своих директологов."""
    return logins_for(victory_conn, logins=None, directologist=None,
                      status=status, exclude=exclude, all_active=True,
                      allowed=allowed)


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


def apply_queue_for_ui(victory_conn: Callable, limit: int = 100,
                       created_by: str | None = None) -> list[dict]:
    """Заявки на изменение цен (kind='apply') для объединённой вкладки «Очередь».

    Ночные автосверки (kind='check'/'auto') НЕ включаем — они фоновые, в очереди не нужны.
    created_by=None → все (админ); строка → только заявки этого пользователя.
    """
    import psycopg2.extras
    # Показываем ВСЁ, что лежит в таблице: заявка уходит из UI только после кнопки
    # «Очистить очередь» (завершённые старше 3 суток), а не сама по времени.
    where = "WHERE kind='apply' AND status <> 'cancelled' "      # удалённые из очереди скрываем
    params: list = []
    if created_by is not None:
        where += "AND created_by=%s "
        params.append(created_by)
    params.append(limit)
    conn = victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT job_id, kind, status, created_by, done, total, message, error, control, run_now, "
            "extract(epoch FROM created_at) AS created_at, "
            "extract(epoch FROM coalesce(finished_at, now()) - started_at) AS elapsed "
            "FROM public.direct_price_check_jobs "
            f"{where}"
            "ORDER BY created_at DESC LIMIT %s", tuple(params))
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
    result = {"logins": len(logins), "ads": 0, "offers": 0,
              "diffs": 0, "compared": 0, "errors": []}
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
            compared, diffs = _compute_and_store_diff(victory_conn_rw, login, dir_by_login.get(login, ""))
            with lock:
                result["ads"] += len(ads_rows)
                result["offers"] += len(offers)
                result["diffs"] += diffs
                result["compared"] += compared
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
               f"{result['diffs']} расхождений ({result['compared']} строк сверено)")
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


def _compute_and_store_diff(victory_conn_rw: Callable, login: str,
                            directologist: str) -> tuple[int, int]:
    """Считает сверку Direct↔Feed для логина и пишет в direct_price_check_diff.

    Возвращает (compared, mismatches): compared — все сопоставленные строки (в т.ч. с
    равной ценой и без матча в фиде), mismatches — только строки с реальным расхождением
    (price_diff<>0 или oldprice_diff<>0), т.е. то, что реально уходит в заливку."""
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
        # r[9]=price_diff, r[10]=oldprice_diff (порядок колонок _DIFF_QUERY SELECT)
        mismatches = sum(1 for r in rows
                         if (r[9] is not None and r[9] != 0)
                         or (r[10] is not None and r[10] != 0))
        conn.commit()
        return len(rows), mismatches
    except Exception:  # noqa: BLE001
        conn.rollback()
        raise
    finally:
        conn.close()


def diff_rows(victory_conn: Callable, *, directologist: str | None,
              login: str | None, only_mismatch: bool, limit: int = 5000,
              allowed: list[str] | None = None) -> list[dict]:
    """Строки расхождений для UI (фильтр по директологу/логину).

    allowed=None → полный доступ; allowed=[...] → жёсткий скоуп по директологу
    текущего пользователя (пустой список → []). Скоуп применяется поверх любых
    UI-фильтров, поэтому обычный юзер не увидит чужие строки даже задав чужой
    directologist/login в запросе."""
    import psycopg2.extras
    if allowed is not None and not allowed:
        return []
    where = ["1=1"]
    params: list = []
    if allowed is not None:
        where.append("directologist = ANY(%s)")
        params.append(allowed)
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


def _get_ads_batch(v5_call: Callable, token: str, login: str, ad_ids: list[int]) -> dict[int, dict]:
    """ads.get одним вызовом на пачку (до PC_BATCH_SIZE Id) вместо запроса на каждое
    объявление. Возвращает {ad_id: ad_data} только для реально найденных объявлений."""
    out: dict[int, dict] = {}
    for i in range(0, len(ad_ids), PC_BATCH_SIZE):
        chunk = ad_ids[i:i + PC_BATCH_SIZE]
        j = v5_call("ads", "get", token, login, {
            "SelectionCriteria": {"Ids": chunk},
            "FieldNames": _ADS_FIELDS,
            "TextAdFieldNames": _TEXT_AD_FIELDS,
            "TextAdPriceExtensionFieldNames": _PRICE_EXT_FIELDS,
        })
        if j.get("error"):
            continue
        for ad in (j.get("result") or {}).get("Ads") or []:
            if isinstance(ad, dict) and ad.get("Id") is not None:
                out[ad["Id"]] = ad
        time.sleep(PC_THROTTLE)
    return out


def _to_micro(v) -> int:
    """Цена → микроединицы Директа с корректным округлением (не обрезать копейки).
    Decimal + ROUND_HALF_UP: 199999.995 ₽ → 200000000000 мкед, а не 199999994999 (как int())."""
    from decimal import Decimal, ROUND_HALF_UP
    return int((Decimal(str(v)) * 1_000_000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _price_extension_for(ad_data: dict, new_price, new_oldprice) -> tuple[dict, bool]:
    """Целевой PriceExtension для одного объявления. → (extension, needs_two_step).

    needs_two_step=True — когда фид просит price_feed >= oldprice_feed («скидки»
    больше нет). Директ жёстко требует OldPrice > Price (код 5005) и не снимает
    OldPrice частичным апдейтом (пропуск ключа / OldPrice=0 — отклоняются, старое
    значение остаётся) — единственный подтверждённый способ: сначала обнулить весь
    PriceExtension (None), затем отдельным вызовом задать только Price (verified
    2026-07-10, porg-pjvmysez/17766710787)."""
    text_ad = ad_data.get("TextAd") or {}
    pe = text_ad.get("PriceExtension") or {}
    if new_price is not None and new_oldprice is not None and new_price >= new_oldprice:
        return {
            "Price": _to_micro(new_price),
            "PriceQualifier": "NONE",
            "PriceCurrency": pe.get("PriceCurrency", "RUB"),
        }, True
    price_api = _to_micro(new_price) if new_price is not None else pe.get("Price", 0)
    oldprice_api = _to_micro(new_oldprice) if new_oldprice is not None else pe.get("OldPrice", 0)
    return {
        "Price": price_api,
        "OldPrice": oldprice_api,
        "PriceQualifier": pe.get("PriceQualifier", "FROM"),
        "PriceCurrency": pe.get("PriceCurrency", "RUB"),
    }, False


def _build_ads_payload(pairs: list[tuple[dict, dict | None]]) -> list[dict]:
    """pairs: [(entry, price_extension), ...] → Ads[] для ads.update.
    entry — {"ad_id", "ad_data", ...}."""
    ads = []
    for e, pe_target in pairs:
        text_ad = e["ad_data"].get("TextAd") or {}
        text_ad_update = {
            "Title": text_ad.get("Title"),
            "Title2": text_ad.get("Title2"),
            "Text": text_ad.get("Text"),
            "Href": text_ad.get("Href"),
            "PriceExtension": pe_target,
        }
        for opt in ("VCardId", "SitelinkSetId", "DisplayUrlPath"):
            if text_ad.get(opt):
                text_ad_update[opt] = text_ad[opt]
        ads.append({"Id": e["ad_id"], "TextAd": text_ad_update})
    return ads


def _parse_batch_update(j: dict, entries: list[dict]) -> dict[int, str]:
    """Разбирает ответ ads.update на батч → {ad_id: outcome}.

    success | archived (код 8300, кампания в архиве — нормально, чинить нечего) |
    rejected (любая другая структурная ошибка Директа вроде 5005 — детерминированная,
    повтор не поможет, fail-fast) | no_units (баллы 152) |
    failed (нет ответа/сетевая ошибка — транзиентная, стоит повторить)."""
    if _api_err_is_152(j):
        return {e["ad_id"]: "no_units" for e in entries}
    if j.get("error"):
        return {e["ad_id"]: "failed" for e in entries}
    upd = (j.get("result") or {}).get("UpdateResults") or []
    out: dict[int, str] = {}
    for e, r in zip(entries, upd):
        errors = (r or {}).get("Errors") or []
        if not errors:
            out[e["ad_id"]] = "success"
        elif any(er.get("Code") == 8300 for er in errors):
            out[e["ad_id"]] = "archived"
        else:
            out[e["ad_id"]] = "rejected"
    for e in entries[len(upd):]:
        out[e["ad_id"]] = "failed"
    return out


def _batch_update_prices(v5_call: Callable, token: str, login: str,
                         entries: list[dict]) -> dict[int, str]:
    """Обновляет цены пачкой (до PC_BATCH_SIZE объявлений за вызов ads.update) вместо
    одного объявления за раз. entries: [{"ad_id","ad_data","new_price","new_oldprice"},...].
    Возвращает {ad_id: outcome}. Записи, где нужно снять OldPrice (needs_two_step),
    идут отдельным под-батчем в 2 шага (см. _price_extension_for)."""
    out: dict[int, str] = {}
    for i in range(0, len(entries), PC_BATCH_SIZE):
        chunk = entries[i:i + PC_BATCH_SIZE]
        normal, no_discount = [], []
        for e in chunk:
            pe, needs_two_step = _price_extension_for(e["ad_data"], e["new_price"], e["new_oldprice"])
            (no_discount if needs_two_step else normal).append((e, pe))

        if normal:
            j = v5_call("ads", "update", token, login, {"Ads": _build_ads_payload(normal)})
            out.update(_parse_batch_update(j, [e for e, _ in normal]))
            time.sleep(PC_THROTTLE)

        if no_discount:
            clear_pairs = [(e, None) for e, _ in no_discount]
            j1 = v5_call("ads", "update", token, login, {"Ads": _build_ads_payload(clear_pairs)})
            step1 = _parse_batch_update(j1, [e for e, _ in clear_pairs])
            time.sleep(PC_THROTTLE)
            step2_pairs = [(e, pe) for e, pe in no_discount if step1.get(e["ad_id"]) == "success"]
            if step2_pairs:
                j2 = v5_call("ads", "update", token, login, {"Ads": _build_ads_payload(step2_pairs)})
                step1.update(_parse_batch_update(j2, [e for e, _ in step2_pairs]))
                time.sleep(PC_THROTTLE)
            out.update(step1)

        # баллы агентства исчерпаны — оставшиеся ещё не отправленные чанки бессмысленны
        if any(v == "no_units" for v in out.values()):
            for e in entries[i + len(chunk):]:
                out.setdefault(e["ad_id"], "no_units")
            break
    return out


def run_apply_pool(deps: dict, job_specs: list[dict], chain_after: bool = True) -> None:
    """Единый движок заливки для ОДНОЙ или НЕСКОЛЬКИХ заявок сразу.

    job_specs: [{"job_id": str, "items": [...]}, ...], где item —
    {login, url, price_direct, oldprice_direct, price_feed, oldprice_feed}.

    chain_after: после финиша подхватить следующую run_now-заявку (_chain_next_apply).
    True в долгоживущем Flask-сервисе (кнопка «Запустить»). В КРОНЕ передавать False:
    крон — одноразовый процесс, и запущенный цепочкой daemon-поток был бы убит на выходе
    (заявка зависла бы в running); плюс крон и так забирает все queued за один прогон.

    Все позиции всех заявок сливаются в общий пул и группируются по агентству ОДИН раз:
      • разные агентства  → льются ПАРАЛЛЕЛЬНО (до PC_AGENCY_WORKERS потоков);
      • одно агентство    → 1 поток на токен (лимит Директа ≤2 на токен, берём 1),
        внутри — батчами ПО ЛОГИНУ: Client-Login у ads.get/update скоупит вызов на
        конкретный клиентский аккаунт, поэтому мешать логины в один батч нельзя
        (иначе вернутся только объявления первого логина — верифицировано 2026-07-11).

    Прогресс (done/message), пауза/отмена (control) и финальный статус — раздельно
    по каждому job_id. run_apply_job (одиночная заявка) = пул из одной.

    Батчами (до PC_BATCH_SIZE за вызов) вместо одного объявления за раз — инцидент
    2026-07-10 (Тумашенко): 700+ отдельных вызовов растягивали заливку на часы."""
    victory_conn = deps["victory_conn"]
    victory_conn_rw = deps["victory_conn_rw"]
    token_for_login = deps["token_for_login"]
    direct_tokens = deps["direct_tokens"]
    v5_call = deps["v5_call"]

    lock = threading.Lock()
    # Состояние на КАЖДУЮ заявку: свой result/счётчик/сигнал паузы-отмены.
    jst: dict[str, dict] = {}
    for spec in job_specs:
        jid = spec["job_id"]
        n = len(spec.get("items") or [])
        jst[jid] = {
            "total": n, "done": 0, "signal": None,
            "ads_done": 0,      # ad-level счётчик (гранулярный heartbeat, НЕ пишется в done)
            "last_hb": 0.0,     # monotonic-время последней записи message (throttle heartbeat)
            "result": {"items": n, "ads_updated": 0, "ads_archived": 0, "ads_rejected": 0,
                       "ads_failed": 0, "no_units_agencies": [], "skipped": [], "errors": []},
        }

    def _check_control(jid: str) -> str | None:
        st = jst[jid]
        if st["signal"] is not None:
            return st["signal"]
        try:
            ctl = job_control(victory_conn, jid)
        except Exception:  # noqa: BLE001
            ctl = ""
        if ctl in ("pause", "cancel"):
            with lock:
                st["signal"] = ctl
            return ctl
        return None

    def _mark_item_done(jid: str) -> None:
        st = jst[jid]
        with lock:
            st["done"] += 1
            done = st["done"]
            st["last_hb"] = time.monotonic()   # message только что записан → сдвигаем окно heartbeat
        _job_update(victory_conn_rw, jid, done=done,
                    message=f"обработано {done}/{st['total']} строк")

    def _heartbeat(jid: str, note: str = "") -> None:
        """Живой статус в поле message между закрытиями url-позиций (throttled в БД).

        Пока не закрыта первая позиция (done=0), пишем ad-level прогресс, чтобы шкала не
        выглядела зависшей. Пишет НЕ чаще раза в PC_HEARTBEAT_SECS (защита от спама БД),
        трогает ТОЛЬКО message — started_at не меняется, reconcile_stuck_jobs не задет.
        Ошибку глушим: heartbeat не должен ронять заливку."""
        st = jst[jid]
        now = time.monotonic()
        with lock:
            if (now - st["last_hb"]) < PC_HEARTBEAT_SECS:
                return
            st["last_hb"] = now
            done, total, ads = st["done"], st["total"], st["ads_done"]
        msg = f"идёт заливка: закрыто {done}/{total} позиций"
        if ads:
            msg += f", объявлений обработано {ads}"
        if note:
            msg += f" · {note}"
        try:
            _job_update(victory_conn_rw, jid, message=msg)
        except Exception:  # noqa: BLE001
            pass

    def _agency_worker(agency: str, pairs: list[tuple]) -> None:
        token = pairs[0][1]
        # Внутри агентства группируем ПО ЛОГИНУ (Client-Login у ads.get/update — на клиента).
        by_login: dict[str, list] = {}
        for keyed_it, _tok in pairs:
            by_login.setdefault(keyed_it["login"], []).append(keyed_it)

        agency_dead = False   # баллы агентства кончились (152) — остальные логины пропускаем
        for login, group in by_login.items():
            if agency_dead:
                for keyed_it in group:
                    jid = keyed_it["job_id"]
                    with lock:
                        jst[jid]["result"]["skipped"].append(
                            {"login": login, "url": (keyed_it["_item"].get("url") or ""),
                             "reason": "agency_no_units"})
                    _mark_item_done(jid)
                continue

            pending_left: dict[int, set] = {}   # id(item) -> оставшиеся ad_id этой строки
            tasks: list[dict] = []
            for keyed_it in group:
                jid = keyed_it["job_id"]
                it = keyed_it["_item"]
                if _check_control(jid) is not None:   # заявка встала на паузу/отмену ещё до старта
                    with lock:
                        jst[jid]["result"]["skipped"].append(
                            {"login": login, "url": (it.get("url") or ""), "reason": "interrupted"})
                    _mark_item_done(jid)
                    continue
                try:
                    ad_ids = _ad_ids_for(victory_conn, login, it.get("url") or "",
                                         it.get("price_direct"), it.get("oldprice_direct"))
                except Exception as e:  # noqa: BLE001
                    with lock:
                        jst[jid]["result"]["errors"].append(f"{login}: {e}")
                        jst[jid]["result"]["skipped"].append(
                            {"login": login, "url": (it.get("url") or ""), "reason": f"error: {str(e)[:80]}"})
                    _mark_item_done(jid)
                    continue
                if not ad_ids:
                    _mark_item_done(jid)
                    continue
                pending_left[id(it)] = set(ad_ids)
                for ad_id in ad_ids:
                    tasks.append({"job_id": jid, "item": it, "login": login, "ad_id": ad_id,
                                  "new_price": it.get("price_feed"), "new_oldprice": it.get("oldprice_feed")})

            def _finish(task: dict) -> None:
                it = task["item"]
                with lock:
                    jst[task["job_id"]]["ads_done"] += 1   # ad-level прогресс для heartbeat
                left = pending_left.get(id(it))
                if left is not None:
                    left.discard(task["ad_id"])
                    if not left:
                        del pending_left[id(it)]
                        _mark_item_done(task["job_id"])

            schedule = ([PC_APPLY_DELAY_FAST] * PC_APPLY_ATTEMPTS_FAST
                        + [PC_APPLY_DELAY_SLOW] * PC_APPLY_ATTEMPTS_SLOW)
            stop_agency = False
            pending_tasks = tasks
            for round_i, delay in enumerate([0] + schedule):
                if not pending_tasks:
                    break
                if round_i > 0:
                    time.sleep(delay)
                # per-job пауза/отмена: снимаем задачи заявок, которые встали, не трогая остальные
                active = []
                for t in pending_tasks:
                    if _check_control(t["job_id"]) is not None:
                        with lock:
                            jst[t["job_id"]]["result"]["skipped"].append(
                                {"login": t["login"], "url": (t["item"].get("url") or ""),
                                 "reason": "interrupted", "ad_id": t["ad_id"]})
                        _finish(t)
                    else:
                        active.append(t)
                pending_tasks = active
                if not pending_tasks:
                    break
                # ── батч-get: один вызов (до PC_BATCH_SIZE Id) на ad_id ЭТОГО логина ──
                ad_data_by_id = _get_ads_batch(v5_call, token, login,
                                               [t["ad_id"] for t in pending_tasks])
                entries = []
                for t in pending_tasks:
                    ad_data = ad_data_by_id.get(t["ad_id"])
                    if not ad_data:
                        with lock:
                            jst[t["job_id"]]["result"]["ads_failed"] += 1
                        _finish(t)
                        continue
                    entries.append({"ad_id": t["ad_id"], "ad_data": ad_data,
                                    "new_price": t["new_price"], "new_oldprice": t["new_oldprice"],
                                    "_task": t})
                if not entries:
                    pending_tasks = []
                    continue
                outcomes = _batch_update_prices(v5_call, token, login, entries)
                next_round = []
                for e in entries:
                    t = e["_task"]
                    res = jst[t["job_id"]]["result"]
                    outcome = outcomes.get(e["ad_id"], "failed")
                    if outcome == "success":
                        with lock:
                            res["ads_updated"] += 1
                        _finish(t)
                    elif outcome == "archived":
                        with lock:
                            res["ads_archived"] += 1
                        _finish(t)
                    elif outcome == "rejected":
                        with lock:
                            res["ads_rejected"] += 1
                        _finish(t)
                    elif outcome == "no_units":
                        with lock:
                            if agency not in res["no_units_agencies"]:
                                res["no_units_agencies"].append(agency)
                            res["errors"].append(f"{t['login']}: баллы агентства {agency} исчерпаны (152)")
                            res["skipped"].append({"login": t["login"], "url": (t["item"].get("url") or ""),
                                                   "reason": "agency_no_units", "ad_id": t["ad_id"]})
                        stop_agency = True
                        _finish(t)
                    elif round_i == len(schedule):   # последняя попытка исчерпана
                        with lock:
                            res["ads_failed"] += 1
                        _finish(t)
                    else:
                        next_round.append(t)         # транзиентная ошибка — повторим
                pending_tasks = next_round
                # heartbeat: живой статус в message (throttled) — пока первая позиция не закрыта,
                # done стоит на 0; показываем ad-level прогресс, чтобы шкала не казалась зависшей.
                for _hb_jid in {e["_task"]["job_id"] for e in entries}:
                    _heartbeat(_hb_jid, f"агентство {agency}, попытка {round_i + 1}")
                if stop_agency:
                    break

            for t in pending_tasks:   # прервано паузой/отменой/152 раньше исчерпания ретраев
                with lock:
                    jst[t["job_id"]]["result"]["skipped"].append(
                        {"login": t["login"], "url": (t["item"].get("url") or ""),
                         "reason": "agency_no_units" if stop_agency else "interrupted", "ad_id": t["ad_id"]})
                _finish(t)
            if stop_agency:
                agency_dead = True   # остальные логины этого агентства пропустим (баллы кончились)

    any_paused = False
    finalized: set = set()   # уже финализированные job_id — чтобы except не финалил повторно
    try:
        # ── setup ВНУТРИ try: _group_by_agency бьёт по сети (token_for_login);
        #    если бросит — заявки не должны остаться навечно в 'running' ──
        keyed = []
        for spec in job_specs:
            jid = spec["job_id"]
            for it in (spec.get("items") or []):
                keyed.append({"job_id": jid, "login": (it.get("login") or "").strip(),
                              "agency": it.get("agency") or "", "_item": it})
        by_agency, no_token = _group_by_agency(keyed, token_for_login, direct_tokens)
        for k in no_token:
            res = jst[k["job_id"]]["result"]
            with lock:
                res["errors"].append(f"{k['login']}: не найден агентский токен")
                res["skipped"].append({"login": k["login"], "url": (k["_item"].get("url") or ""),
                                       "reason": "no_token"})
            _mark_item_done(k["job_id"])

        with ThreadPoolExecutor(max_workers=PC_AGENCY_WORKERS) as ex:
            futs = [ex.submit(_agency_worker, ag, pairs) for ag, pairs in by_agency.items()]
            for f in as_completed(futs):
                f.result()
        # ── финализируем КАЖДУЮ заявку раздельно по её сигналу/результату ──
        for jid, st in jst.items():
            res, done, total = st["result"], st["done"], st["total"]
            if st["signal"] == "pause":
                any_paused = True
                _job_update(victory_conn_rw, jid, status="paused", control="",
                            done=done, message=f"пауза на {done}/{total} строк")
            elif st["signal"] == "cancel":
                _job_finish(victory_conn_rw, jid, "cancelled", res,
                            message=f"отменено на {done}/{total} строк")
            else:
                status = "done" if not res["errors"] else "done_with_errors"
                skip = len(res["skipped"])
                msg = (f"заливка: обновлено {res['ads_updated']}, архивных {res['ads_archived']}, "
                       f"отклонено {res['ads_rejected']}, ошибок {res['ads_failed']}"
                       + (f", пропущено позиций {skip}" if skip else ""))
                _job_finish(victory_conn_rw, jid, status, res, message=msg)
            finalized.add(jid)
    except Exception as e:  # noqa: BLE001
        # общий сбой пула — ещё не финализированные заявки помечаем error (не висят в running)
        for jid, st in jst.items():
            if jid in finalized:
                continue
            _job_finish(victory_conn_rw, jid, "error", st["result"],
                        error=str(e), message="заливка прервана: " + str(e)[:200])
        traceback.print_exc()
    finally:
        # пауза — сознательная остановка, run_now-заявку НЕ дёргаем. В кроне chain_after=False
        # (одноразовый процесс — daemon-поток цепочки умер бы на выходе, оставив заявку в running).
        if chain_after and not any_paused:
            _chain_next_apply(deps)


def run_apply_job(deps: dict, job_id: str, items: list[dict]) -> None:
    """Заливка одной заявки — обёртка над пулом из одной (см. run_apply_pool)."""
    run_apply_pool(deps, [{"job_id": job_id, "items": items}])


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

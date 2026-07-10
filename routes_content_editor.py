"""Редактор контента Direct — массовый поиск и замена AI-текстов.

Отдельная изолированная страница ``/direct/automation/content`` и её API
(``/direct/api/content-editor/*``). Назначение: контент (уточнения, быстрые
ссылки, заголовки/тексты объявлений) генерирует M3-пак и иногда выдаёт неверные
фразы. Этот сервис нужен для МАССОВОЙ коррекции — найти паттерн ошибки поиском
и заменить его во ВСЕХ кампаниях/группах аккаунта за один проход.

Загрузка использует официальный Direct API v5 как read-only источник снимка и дополняет
часть данных cookie/Grid-чтением. Запись через OAuth API v5/v501 запрещена:
массовые правки контента в кампаниях идут только через cookie/Grid writer.

Порядок правки:
  1) POST /load    — прочитать весь контент аккаунта + где что используется;
  2) POST /preview — сколько объектов затронет замена old_text→new_text (без записи);
  3) POST /replace — применить замену.

Замена по типам:
  • ad_title / ad_title2 / ad_text — ``UpdateAdaptiveTextAds`` по cookies/Grid;
  • sitelink_title — будущий cookie/Grid flow: AddSitelinkSets + переназначение объявлений;
  • callout — Grid/cookie: создать новый callout, убрать старый id из кампаний и привязать новый.
"""

from __future__ import annotations

import os
import time
import uuid
import re
import json
from pathlib import Path
from typing import Callable

from flask import jsonify, render_template, request, session


# ─────────────────────────── v5 low-level helpers ────────────────────────────

def _result(j: dict) -> dict:
    return j.get("result") or {}


def _v5_paginate(v5_call: Callable, svc: str, token: str, login: str,
                 params: dict, collection: str) -> tuple[list, str | None]:
    """GET всех объектов сервиса v5 с пагинацией по ``LimitedBy``.

    Возвращает ``(rows, error)``. ``collection`` — ключ массива в ``result``
    (``Campaigns`` / ``AdGroups`` / ``Ads`` / ``SitelinkSets`` / ``AdExtensions``).
    """
    rows: list = []
    offset = 0
    for _ in range(200):  # жёсткий предохранитель от бесконечного цикла
        p = dict(params)
        page = dict(p.get("Page") or {})
        page.setdefault("Limit", 10000)
        if offset:
            page["Offset"] = offset
        p["Page"] = page
        j = v5_call(svc, "get", token, login, p)
        if j.get("error"):
            e = j["error"]
            msg = e.get("error_string") if isinstance(e, dict) else str(e)
            if isinstance(e, dict) and e.get("error_detail"):
                msg = f"{msg}: {e['error_detail']}"
            return rows, str(msg)
        res = _result(j)
        rows.extend(res.get(collection) or [])
        limited_by = res.get("LimitedBy")
        if not limited_by:
            break
        offset = int(limited_by)
    return rows, None


def _v5_paginate_campaign_batches(
    v5_call: Callable,
    svc: str,
    token: str,
    login: str,
    params: dict,
    collection: str,
    campaign_ids: list[int],
    *,
    batch_size: int = 10,
) -> tuple[list, str | None]:
    """GET v5 rows for services whose ``SelectionCriteria.CampaignIds`` is capped.

    ``adgroups.get`` rejects more than 10 CampaignIds, so the content editor must
    fan out account-wide reads into small requests and then merge the pages.
    """
    rows: list = []
    for i in range(0, len(campaign_ids), batch_size):
        chunk = campaign_ids[i:i + batch_size]
        p = dict(params or {})
        criteria = dict(p.get("SelectionCriteria") or {})
        criteria["CampaignIds"] = chunk
        p["SelectionCriteria"] = criteria
        batch_rows, err = _v5_paginate(v5_call, svc, token, login, p, collection)
        rows.extend(batch_rows)
        if err:
            return rows, err
    return rows, None


def _strip_campaign_subfield_names(params: dict) -> dict:
    """Remove invalid campaign subtype field lists from legacy payloads.

    Direct API v5 currently accepts only a narrow set of ``TextCampaignFieldNames``.
    Older versions of this page requested fields such as ``CounterIds`` or
    ``CalloutIds`` there, which makes ``campaigns.get`` fail before the editor can
    load. The content editor needs only campaign ``Id/Name/Type`` for /load.
    """
    cleaned = dict(params or {})
    for key in (
        "TextCampaignFieldNames",
        "MobileAppCampaignFieldNames",
        "DynamicTextCampaignFieldNames",
        "CpmBannerCampaignFieldNames",
        "SmartCampaignFieldNames",
        "UnifiedCampaignFieldNames",
    ):
        cleaned.pop(key, None)
    return cleaned


# ─────────────────────────── Grid read/write helpers ─────────────────────────

def _grid_client(login: str):
    from .grid_finalize import GridClient

    return GridClient(login)


# ──────────────── async content jobs: Postgres-очередь ───────────────────────
# Задания лежат в таблице direct_content_jobs БД seoadvanced (LXC 101), выполняются
# отдельным сервисом direct-content-worker.service. Переживают рестарты обоих сервисов.

CE_JOBS_TABLE = "direct_automation.content_jobs"
CE_DAILY_JOB_CAP = int(os.environ.get("CE_DAILY_JOB_CAP") or 50)  # заданий на аккаунт в сутки (Екб)
# начало текущих суток по Екатеринбургу (timestamptz)
CE_EKB_DAY_SQL = "(date_trunc('day', now() AT TIME ZONE 'Asia/Yekaterinburg') AT TIME ZONE 'Asia/Yekaterinburg')"


def _jobs_db():
    from telegram_parsing.db import get_db
    return get_db()


def _jobs_exec(query: str, params: tuple = (), fetch: str | None = None):
    import psycopg2.extras
    conn = _jobs_db()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query, params)
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                return None
    finally:
        conn.close()


def ensure_jobs_table() -> None:
    _jobs_exec(f"""
        CREATE TABLE IF NOT EXISTS {CE_JOBS_TABLE} (
            job_id text PRIMARY KEY,
            username text NOT NULL DEFAULT '',
            login text NOT NULL,
            agency text NOT NULL DEFAULT '',
            type text NOT NULL,
            old_text text NOT NULL,
            new_text text NOT NULL,
            campaign_count int NOT NULL DEFAULT 0,
            access_directologists jsonb,
            status text NOT NULL DEFAULT 'queued',
            cancel_requested boolean NOT NULL DEFAULT false,
            dismissed boolean NOT NULL DEFAULT false,
            attempts int NOT NULL DEFAULT 0,
            done int NOT NULL DEFAULT 0,
            total int NOT NULL DEFAULT 1,
            replaced int NOT NULL DEFAULT 0,
            error text NOT NULL DEFAULT '',
            errors jsonb NOT NULL DEFAULT '[]'::jsonb,
            result jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            started_at timestamptz,
            finished_at timestamptz,
            worker text NOT NULL DEFAULT ''
        )""")
    # два сервиса могут стартовать одновременно — IF NOT EXISTS не спасает от гонки в каталоге
    for ddl in (
        # 'exact' — точечная замена целого поля; 'substring' — массовая замена фрагмента
        f"ALTER TABLE {CE_JOBS_TABLE} ADD COLUMN IF NOT EXISTS mode text NOT NULL DEFAULT 'exact'",
        f"CREATE INDEX IF NOT EXISTS {CE_JOBS_TABLE}_status_idx ON {CE_JOBS_TABLE}(status)",
        f"CREATE INDEX IF NOT EXISTS {CE_JOBS_TABLE}_login_day_idx ON {CE_JOBS_TABLE}(login, created_at)",
    ):
        try:
            _jobs_exec(ddl)
        except Exception:  # noqa: BLE001
            pass


def _content_job_public(row: dict) -> dict:
    def _ts(v):
        return v.timestamp() if v is not None else None

    status = row.get("status") or ""
    started = _ts(row.get("started_at")) or _ts(row.get("created_at")) or time.time()
    if status == "running":
        elapsed = time.time() - started
    elif status in ("done", "error", "cancelled"):
        elapsed = (_ts(row.get("finished_at")) or time.time()) - started
    else:
        elapsed = 0
    return {
        "job_id": row.get("job_id"),
        "login": row.get("login"),
        "username": row.get("username") or "",
        "type": row.get("type"),
        "campaign_count": int(row.get("campaign_count") or 0),
        "status": status,
        "done": row.get("done") or 0,
        "total": row.get("total") or 1,
        "replaced": row.get("replaced") or 0,
        "error": row.get("error") or "",
        "errors": row.get("errors") or [],
        "result": row.get("result") or {},
        "ahead": 0,
        "elapsed": elapsed,
        "created_at": _ts(row.get("created_at")),
    }


def _scope_check(victory_conn: Callable, login: str, allowed: list[str] | None) -> tuple[bool, str]:
    """Доступ к аккаунту по списку директологов (None = полный доступ). Без Flask."""
    login = (login or "").strip()
    if not login:
        return False, "login обязателен"
    if allowed is None:
        return True, ""
    if not allowed:
        return False, "нет выданных директологов для редактора контента"
    import psycopg2.extras
    conn = victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT directologist FROM public.local_gsheet_sites "
            "WHERE direction='Авто' AND login_key=%s LIMIT 1",
            (login,),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return False, f"аккаунт {login} не найден"
    if (row.get("directologist") or "") not in allowed:
        return False, f"нет доступа к аккаунту {login}"
    return True, ""


def make_job_executor(*, victory_conn, token_for_login, direct_tokens, v5_call, v501_svc):
    """Исполнитель задания замены без Flask-контекста — используется воркером очереди."""

    def execute(job: dict, is_cancelled=lambda: False) -> dict:
        allowed = job.get("access_directologists")
        ok, err = _scope_check(victory_conn, job["login"], allowed)
        if not ok:
            raise RuntimeError(err)
        tokens = direct_tokens()
        if not tokens:
            raise RuntimeError("нет агентских токенов (loader.load_yandex_direct вернул пусто)")
        token, _agency = token_for_login(job["login"], "", tokens)
        if not token:
            raise RuntimeError(f"ни один агентский токен не открывает аккаунт {job['login']}")
        # campaign-level sitelinks нужны ТОЛЬКО заданиям замены набора уровня кампании
        # (sitelink_title/description). Для ad_title/ad_text/callout/ad_href не гоняем
        # лишний Grid-round-trip (см. блок 3c в _load_account).
        _need_cl_sitelinks = (job.get("type") or "") in _SITELINK_JOB_TYPES
        content = _load_account(
            token, job["login"], v5_call,
            include_campaign_sitelinks=_need_cl_sitelinks,
        )
        if content.get("error"):
            raise RuntimeError(content["error"])
        if is_cancelled():
            return {"cancelled": True}
        return _do_replace(token, job["login"], job["type"], job["old_text"], job["new_text"],
                           content, v5_call, v501_svc, mode=(job.get("mode") or "exact"))

    return execute


def _grid_campaign_callout_ids(
    login: str,
    campaign_ids: list[int],
    *,
    grid_client_factory: Callable | None = None,
) -> dict[int, list[int]]:
    """Read campaign → inheritable callout ids through cookie/Grid.

    Direct API v5 does not expose campaign CalloutIds in the enum set accepted by
    current ``campaigns.get``. This read is best-effort; callers can continue
    with empty usages when Grid schema/cookie is unavailable.
    """
    ids: list[int] = []
    for raw in campaign_ids or []:
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            continue
        if cid > 0 and cid not in ids:
            ids.append(cid)
    if not ids:
        return {}
    grid = (grid_client_factory or _grid_client)(login)
    grid._bootstrap_csrf()
    q = ("query CampaignCallouts($login:String!,$inp:GdCampaignsContainerInput!){"
         "client(searchBy:{login:$login}){campaigns(input:$inp){rowset{id name "
         "status{archived} "
         "...on GdUnifiedCampaign{inheritableCallouts{assetValue}}"
         "}}}}")
    out: dict[int, list[int]] = {}
    for chunk in [ids[i:i + 100] for i in range(0, len(ids), 100)]:
        inp = {
            "filter": {"campaignIdIn": [str(cid) for cid in chunk]},
            "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
            "limitOffset": {"limit": 5000, "offset": 0},
            "orderBy": [{"order": "ASC", "field": "ID"}],
        }
        r = grid._post("CampaignCallouts", q, {"login": login, "inp": inp})
        if r.status_code == 403:
            r = grid._post("CampaignCallouts", q, {"login": login, "inp": inp})
        data = r.json()
        if data.get("errors"):
            raise RuntimeError(str(data.get("errors"))[:300])
        rows = ((((data.get("data") or {}).get("client") or {})
                 .get("campaigns") or {}).get("rowset") or [])
        for row in rows:
            try:
                cid = int(row.get("id"))
            except (TypeError, ValueError):
                continue
            if (row.get("status") or {}).get("archived"):
                continue  # архивные кампании менять нельзя — не показываем их привязки
            raw_callouts = (row.get("inheritableCallouts") or {}).get("assetValue") or []
            clean: list[int] = []
            for co in raw_callouts:
                try:
                    co_id = int(co)
                except (TypeError, ValueError):
                    continue
                if co_id > 0 and co_id not in clean:
                    clean.append(co_id)
            out[cid] = clean
    return out


def _grid_tp67_campaigns(
    login: str,
    *,
    grid_client_factory: Callable | None = None,
) -> list[dict]:
    """Read tp6/tp7 campaigns through Grid when UAC list endpoint returns 405."""
    grid = (grid_client_factory or _grid_client)(login)
    grid._bootstrap_csrf()
    q = ("query ContentEditorCampaigns($login:String!,$inp:GdCampaignsContainerInput!){"
         "client(searchBy:{login:$login}){campaigns(input:$inp){rowset{id name "
         "__typename status{primaryStatus archived}}}}}")
    out: list[dict] = []
    offset = 0
    while True:
        inp = {
            "filter": {},
            "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
            "limitOffset": {"limit": 200, "offset": offset},
            "orderBy": [{"order": "ASC", "field": "STATUS"}],
        }
        r = grid._post("ContentEditorCampaigns", q, {"login": login, "inp": inp})
        if r.status_code == 403:
            r = grid._post("ContentEditorCampaigns", q, {"login": login, "inp": inp})
        data = r.json()
        if data.get("errors"):
            raise RuntimeError(str(data.get("errors"))[:300])
        rows = ((((data.get("data") or {}).get("client") or {})
                 .get("campaigns") or {}).get("rowset") or [])
        for row in rows:
            name = row.get("name") or ""
            if not re.match(r"^\s*tp[67]_", name, flags=re.I):
                continue
            status = row.get("status") or {}
            if status.get("archived"):
                continue
            out.append({
                "id": row.get("id"),
                "name": name,
                "typename": row.get("__typename") or "",
                "status": status.get("primaryStatus") or "",
            })
        if len(rows) < 200:
            break
        offset += 200
    return out


# ─────────────────────────── content extraction ──────────────────────────────

def _ad_texts(ad: dict) -> dict:
    """Достаёт title/title2/text из TextAd или ResponsiveAd + SitelinkSetId."""
    body = ad.get("TextAd") or ad.get("ResponsiveAd") or ad.get("DynamicTextAd") or {}
    titles = body.get("Titles") or []
    texts = body.get("Texts") or []
    return {
        "title": body.get("Title") or ((titles[0] or {}).get("Title") or (titles[0] or {}).get("Text") if titles else "") or "",
        "title2": body.get("Title2") or ((titles[1] or {}).get("Text") if len(titles) > 1 else "") or "",
        "text": body.get("Text") or ((texts[0] or {}).get("Text") if texts else "") or "",
        "sitelink_set_id": body.get("SitelinkSetId"),
    }


def _ad_href(ad: dict) -> str:
    """Ссылка объявления (Href). Живёт на TextAd/ResponsiveAd. DynamicTextAd
    посадочную из фида не имеет — её Href мы не запрашиваем и не трогаем."""
    for key in ("TextAd", "ResponsiveAd"):
        body = ad.get(key)
        if isinstance(body, dict) and body.get("Href"):
            return str(body.get("Href") or "").strip()
    return ""


def _href_host_path(href: str) -> tuple[str, str]:
    """(host, path) из Href. host — через copy_engine._copy_domain_from_href
    (единый парсер хоста). path — суффикс: путь + query, без схемы и хоста."""
    from urllib.parse import urlsplit

    from .copy_engine import _copy_domain_from_href

    host = _copy_domain_from_href(href)
    s = urlsplit(href if "://" in href else "https://" + str(href or ""))
    path = s.path or ""
    if s.query:
        path = f"{path}?{s.query}"
    return host, path


def _href_scheme(href: str) -> str:
    """Схема исходного Href (http/https). Fallback — https, если схема не задана."""
    from urllib.parse import urlsplit

    s = urlsplit(str(href or ""))
    return s.scheme or "https"


def _ad_content_rows(ad: dict) -> list[dict]:
    """Rows for content editor search.

    Responsive ads store multiple independent titles/texts on one ad. The
    editor must index each candidate separately so a generated phrase is not
    hidden just because it is not the first item in the v5 array.
    """
    body = ad.get("ResponsiveAd") or {}
    if not body:
        return [_ad_texts(ad)]
    ssid = body.get("SitelinkSetId")
    rows: list[dict] = []
    for item in body.get("Titles") or []:
        title = (item.get("Title") or item.get("Text") or "").strip()
        if title:
            rows.append({"title": title, "title2": "", "text": "", "sitelink_set_id": ssid})
    for item in body.get("Texts") or []:
        text = (item.get("Text") or "").strip()
        if text:
            rows.append({"title": "", "title2": "", "text": text, "sitelink_set_id": ssid})
    return rows or [_ad_texts(ad)]


def _extract_uac_text_list(row: dict, *keys: str) -> list[str]:
    """Pull a deduplicated list of text strings from a raw UAC campaign detail dict.

    UAC responses are inconsistent — items can be plain strings or dicts with a
    ``text`` / ``title`` / ``value`` key. Normalised the same way blueprint
    ``_copy_uac_strings`` does, without importing the whole blueprint module.
    """
    for key in keys:
        value = row.get(key)
        if not isinstance(value, list):
            continue
        seen: set[str] = set()
        result: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = ""
                for k in ("text", "title", "value", "body", "name"):
                    text = str(item.get(k) or "").strip()
                    if text:
                        break
            else:
                text = str(item or "").strip()
            if text and text not in seen:
                seen.add(text)
                result.append(text)
        return result
    return []


def _unwrap_uac_response(data) -> dict:
    """Unwrap common UAC response envelopes into the campaign dict."""
    if isinstance(data, dict):
        for key in ("result", "campaign", "item"):
            value = data.get(key)
            if isinstance(value, dict):
                return value
        return data
    return {}


def _uac_text_item_text(item) -> str:
    """Return display text from a UAC string/list-item preserving legacy shapes."""
    if isinstance(item, dict):
        for key in ("text", "title", "value", "body", "name"):
            text = str(item.get(key) or "").strip()
            if text:
                return text
        return ""
    return str(item or "").strip()


# Stray leading/trailing whitespace in the fragment fields silently bloats titles:
# a random leading space in the "new" fragment turns "Jetour2026" into "Jetour 2026"
# (brand glued to fragment gets an extra gap). Trim BOTH fragments before match /
# replace / length-guard. str.strip() already removes ASCII + Unicode spaces (incl.
# NBSP), but NOT invisible zero-width chars (ZWSP U+200B, ZWNJ/ZWJ, word-joiner
# U+2060, BOM U+FEFF) — those must be trimmed explicitly. Internal spaces are never
# touched: strip() only affects the ends.
_FRAG_INVISIBLE = "\u200b\u200c\u200d\u2060\ufeff\u180e"


def _frag_trim(s) -> str:
    """Trim leading/trailing whitespace incl. invisible/zero-width chars from a fragment."""
    return ("" if s is None else str(s)).strip().strip(_FRAG_INVISIBLE).strip()


def _uac_replace_text_items(value, old_text: str, new_text: str,
                            mode: str = "exact") -> tuple[list, int]:
    """Replace UAC text items while preserving dict item shape.

    ``mode="exact"`` matches the whole item text (legacy behaviour).
    ``mode="substring"`` replaces the ``old`` fragment inside each item that
    contains it, mirroring Grid's ``FIND_AND_REPLACE`` (all occurrences).
    """
    out: list = []
    changed = 0
    old = _frag_trim(old_text)
    new = _frag_trim(new_text)

    def _hit(cur: str) -> bool:
        return (old in cur) if mode == "substring" else (cur.strip() == old)

    def _apply(cur: str) -> str:
        return cur.replace(old, new) if mode == "substring" else new

    for item in value if isinstance(value, list) else []:
        cur_text = _uac_text_item_text(item)
        if _hit(cur_text):
            changed += 1
            if isinstance(item, dict):
                next_item = dict(item)
                replaced_key = None
                for key in ("text", "title", "value", "body", "name"):
                    key_val = str(next_item.get(key) or "").strip()
                    if key_val and _hit(key_val):
                        next_item[key] = _apply(key_val)
                        replaced_key = key
                        break
                if replaced_key is None:
                    next_item["text"] = _apply(cur_text)
                out.append(next_item)
            else:
                out.append(_apply(cur_text))
        else:
            out.append(item)
    return out, changed


_UAC_PATCH_FULL_KEYS = (
    "adv_type", "alternative_texts_enabled", "counters", "crr", "device_types",
    "display_name", "ecom", "erir_ad_description", "feed_filters", "feed_id",
    "field_to_use_as_body", "field_to_use_as_name", "goals", "href",
    "hyperlocal_geo_segments", "keywords", "limit_period", "listings_feed_filters",
    "listings_feed_id", "minus_keywords", "minus_regions", "ml_banners_enabled",
    "price_recommendations_management_enabled", "pricing",
    "recommendations_management_enabled", "regions", "relevance_match",
    "show_title_and_body", "sitelinks", "socdem", "texts", "time_target",
    "titles", "tracking_params", "use_discounts", "week_limit",
    "yandex_maps_enabled",
)


def _uac_campaign_patch_payload(detail: dict, field_key: str, values: list) -> dict:
    """Build the browser-shaped full UAC PATCH body, dropping read-only fields."""
    payload = {k: detail.get(k) for k in _UAC_PATCH_FULL_KEYS if k in detail}
    payload[field_key] = values
    # tp6/tp7 autotargeting details can contain feed ids from read model, while
    # the save endpoint validates them as MUST_BE_NULL. Browser sends keywords:[]
    # for this mode and omits feed fields.
    if payload.get("keywords") is None:
        payload["keywords"] = []
        for key in ("ecom", "feed_id", "listings_feed_id", "feed_filters", "listings_feed_filters"):
            payload.pop(key, None)
    return payload


def _uac_patch_campaign_texts(client, campaign_id: int, field_key: str, values: list) -> dict:
    """PATCH UAC campaign text fields through the private cookie API.

    The UAC endpoint has used both partial and full-update payloads in browser
    flows. Try the narrow payload first; if the schema rejects it, retry with a
    current detail copy that only changes the requested text field.
    """
    if getattr(client, "csrf", None) is None:
        client.link_info("https://ya.ru")
    partial = {field_key: values}
    try:
        return client._request(
            "PATCH",
            f"/campaign/{campaign_id}",
            json_body=partial,
            step=f"uac-patch-text:{campaign_id}",
        )
    except Exception:
        detail = client._request("GET", f"/campaign/{campaign_id}", step=f"uac-detail:{campaign_id}")
        detail = _unwrap_uac_response(detail)
        detail = _uac_campaign_patch_payload(detail, field_key, values)
        return client._request(
            "PATCH",
            f"/campaign/{campaign_id}",
            json_body=detail,
            step=f"uac-patch-full:{campaign_id}",
        )


def _load_account(
    token: str,
    login: str,
    v5_call: Callable,
    *,
    grid_client_factory: Callable | None = None,
    uac_read_client_factory: Callable | None = None,
    include_campaign_sitelinks: bool = True,
) -> dict:
    """Читает кампании, группы, объявления, наборы ссылок и уточнения аккаунта."""
    # 1) Кампании: только Id и Name (CalloutIds недоступны через TextCampaignFieldNames в v5).
    camps, err = _v5_paginate(
        v5_call, "campaigns", token, login,
        _strip_campaign_subfield_names(
            {"SelectionCriteria": {}, "FieldNames": ["Id", "Name", "Type"]}
        ),
        "Campaigns",
    )
    if err:
        return {"error": f"campaigns.get: {err}"}
    camp_name: dict[int, str] = {}
    camp_type: dict[int, str] = {}
    for c in camps:
        cid = int(c.get("Id") or 0)
        if cid:
            camp_name[cid] = c.get("Name") or ""
            camp_type[cid] = c.get("Type") or ""
    v5_campaign_ids = sorted(camp_name)
    # UAC/tp6/tp7 campaigns are not reliably visible in v5. Add them from the
    # cookie-only UAC list so editor replacements can target PATCH /uac/campaign.
    uac_read_error: str | None = None
    uac_detail_client = None
    try:
        if uac_read_client_factory is None:
            from .uac_read import UacReadClient

            uac_detail_client = UacReadClient(login)
        else:
            uac_detail_client = uac_read_client_factory(login)
        for row in uac_detail_client.client.list_campaigns():
            if not isinstance(row, dict):
                continue
            try:
                cid = int(row.get("id") or row.get("direct_id") or row.get("campaign_id") or 0)
            except (TypeError, ValueError):
                continue
            if cid <= 0:
                continue
            camp_name.setdefault(
                cid,
                row.get("display_name") or row.get("name") or row.get("title") or f"UAC {cid}",
            )
            camp_type[cid] = "UAC"
    except Exception as e:  # noqa: BLE001 - UAC read is enrichment; load can continue with v5 data
        uac_read_error = str(e)[:200]
        try:
            for row in _grid_tp67_campaigns(login, grid_client_factory=grid_client_factory):
                try:
                    cid = int(row.get("id") or 0)
                except (TypeError, ValueError):
                    continue
                if cid <= 0:
                    continue
                camp_name.setdefault(cid, row.get("name") or f"UAC {cid}")
                camp_type[cid] = "UAC"
        except Exception as grid_e:  # noqa: BLE001
            uac_read_error = f"{uac_read_error}; Grid tp6/tp7: {str(grid_e)[:160]}"
    campaign_ids = sorted(camp_name)
    if not campaign_ids:
        out = {"callouts": [], "sitelinks": [], "ads": [], "links": [], "_ads_by_set": {}}
        if uac_read_error:
            out["_uac_read_error"] = uac_read_error
        return out
    # Маппинг callout → кампании строим через Grid после получения extension-ids.
    callout_to_camps: dict[str, list[int]] = {}
    campaign_callout_ids: dict[int, list[int]] = {}

    # 2) Группы: имя + принадлежность кампании.
    groups, err = _v5_paginate_campaign_batches(
        v5_call, "adgroups", token, login,
        {"FieldNames": ["Id", "Name", "CampaignId"]},
        "AdGroups",
        v5_campaign_ids,
    )
    if err:
        return {"error": f"adgroups.get: {err}"}
    ag_info: dict[int, dict] = {
        int(g.get("Id") or 0): {"name": g.get("Name") or "",
                                "campaign_id": int(g.get("CampaignId") or 0)}
        for g in groups
    }

    # 3) Объявления: заголовки/тексты + ссылка на набор быстрых ссылок.
    ads, err = _v5_paginate_campaign_batches(
        v5_call, "ads", token, login,
        {"FieldNames": ["Id", "CampaignId", "AdGroupId", "Type", "State"],
         "TextAdFieldNames": ["Title", "Title2", "Text", "SitelinkSetId", "Href"],
         "ResponsiveAdFieldNames": ["Titles", "Texts", "SitelinkSetId", "Href"],
         "DynamicTextAdFieldNames": ["Text", "SitelinkSetId"]},
        "Ads",
        v5_campaign_ids,
    )
    if err:
        return {"error": f"ads.get: {err}"}

    def _usage_for(cid: int, agid: int) -> dict:
        return {
            "campaign_id": cid,
            "campaign_name": camp_name.get(cid, ""),
            "adgroup_id": agid,
            "adgroup_name": (ag_info.get(agid) or {}).get("name", ""),
        }

    ads_out: list[dict] = []
    links_out: list[dict] = []               # ссылки объявлений (Href) для вкладки «Смена ссылки»
    uac_sitelinks_out: list[dict] = []       # быстрые ссылки UAC (tp6/tp7) — source="uac"
    sitelink_usages: dict[str, list[dict]] = {}
    ads_by_set: dict[str, list[dict]] = {}   # set_id → [{ad_id}] для переназначения на replace
    for a in ads:
        cid = int(a.get("CampaignId") or 0)
        agid = int(a.get("AdGroupId") or 0)
        ad_id = int(a.get("Id") or 0)
        usage = _usage_for(cid, agid)
        # Ссылка объявления (Href) — вкладка «Смена ссылки». Объявления без Href
        # (DynamicTextAd/фидовые/Shopping/UAC) пропускаем.
        href = _ad_href(a)
        if href:
            host, path = _href_host_path(href)
            if path:
                subtype = next((k for k in ("TextAd", "ResponsiveAd") if a.get(k)), "")
                links_out.append({
                    "ad_id": ad_id,
                    "campaign_id": cid,
                    "campaign_name": camp_name.get(cid, ""),
                    "type": subtype or (a.get("Type") or ""),
                    "state": a.get("State") or "",
                    "host": host,
                    "path": path,
                    "href": href,   # исходный Href целиком — для реконструкции scheme+host на записи
                })
        content_rows = _ad_content_rows(a)
        for t in content_rows:
            ads_out.append({
                "ad_id": ad_id,
                "title": t["title"], "title2": t["title2"], "text": t["text"],
                "usages": [usage],
            })
        ssid = (content_rows[0] if content_rows else _ad_texts(a))["sitelink_set_id"]
        if ssid:
            sitelink_usages.setdefault(str(ssid), []).append(usage)
            subtype = next((k for k in ("TextAd", "DynamicTextAd", "ResponsiveAd") if a.get(k)), "TextAd")
            ads_by_set.setdefault(str(ssid), []).append({"ad_id": ad_id, "subtype": subtype})

    # 3b) UAC-кампании (tp6/tp7, Type=UNIFIED_CAMPAIGN) — через UAC web-api.
    # v5 ads.get не возвращает объявления UAC. Читаем titles/texts напрямую через UacReadClient;
    # запись в replace идёт только по cookie PATCH /web-api/uac/campaign/{id}.
    uac_ids = [
        cid for cid in campaign_ids
        if camp_type.get(cid) in {"UNIFIED_CAMPAIGN", "UAC"}
    ]
    if uac_ids:
        try:
            if uac_detail_client is None:
                if uac_read_client_factory is None:
                    from .uac_read import UacReadClient

                    uac_detail_client = UacReadClient(login)
                else:
                    uac_detail_client = uac_read_client_factory(login)
            uac_details = uac_detail_client.campaign_details(uac_ids)
            for cid, raw in uac_details.items():
                usage = _usage_for(int(cid), 0)
                for t in _extract_uac_text_list(raw, "titles", "title_items"):
                    ads_out.append({
                        "ad_id": 0, "source": "uac", "campaign_id": int(cid),
                        "title": t, "title2": "", "text": "",
                        "usages": [usage],
                    })
                for t in _extract_uac_text_list(raw, "texts", "text_items"):
                    ads_out.append({
                        "ad_id": 0, "source": "uac", "campaign_id": int(cid),
                        "title": "", "title2": "", "text": t,
                        "usages": [usage],
                    })
                # Быстрые ссылки UAC (title/href/description) — из детали кампании.
                # Один синтетический «набор» на кампанию (set_id="uac:<cid>"); запись
                # href/title/description идёт cookie-PATCH sitelinks, а не Grid-набором.
                uac_sl_items = [
                    {"title": (it.get("title") or "").strip(),
                     "href": (it.get("href") or "").strip(),
                     "description": (it.get("description") or "").strip()}
                    for it in (raw.get("sitelinks") or [])
                    if isinstance(it, dict)
                ]
                uac_sl_items = [it for it in uac_sl_items if it["title"] or it["href"] or it["description"]]
                if uac_sl_items:
                    uac_sitelinks_out.append({
                        "set_id": f"uac:{int(cid)}",
                        "set_title": uac_sl_items[0]["title"] or f"UAC {cid}",
                        "items": uac_sl_items,
                        "usages": [usage],
                        "level": "uac",
                        "campaign_ids": [int(cid)],
                        "source": "uac",
                        "campaign_id": int(cid),
                    })
        except Exception as e:  # noqa: BLE001 — UAC read is enrichment; must not block load
            uac_read_error = str(e)[:200]

    # 3c) Наборы быстрых ссылок УРОВНЯ КАМПАНИИ (inheritableSitelinkSet). В ЕПК
    # unified-кампаниях быстрые ссылки часто привязаны к КАМПАНИИ, а объявления их
    # НАСЛЕДУЮТ — v5 ads.get такие наборы у объявлений не отдаёт (SitelinkSetId пуст).
    # Без этого шага campaign-level наборы невидимы редактору, а замена целится в
    # пустой список ad_ids и Grid ничего не применяет («не подтвердилась у N объявлений»).
    # Читаем набор каждой кампании через Grid и добавляем campaign-usage → replace идёт
    # campaign-level путём (set_campaign_sitelink_set), а не ad-level find/replace.
    # ⚠️ Блок 3c — лишний Grid-round-trip. Гоняем его ТОЛЬКО когда campaign-level
    # наборы реально нужны (главный /load и sitelink-замены). Для замен текста/
    # заголовка/callout/href и для /links / preview НЕ-sitelink типов include=False —
    # иначе на каждой загрузке лишний Grid-запрос замедляет hot-path и на суб-аккаунтах
    # без рабочей куки управляющего агентства тихо роняет весь load в except.
    campaign_ids_by_set: dict[str, list[int]] = {}
    grid_sitelink_error = None
    if include_campaign_sitelinks:
        try:
            _cl_grid = (grid_client_factory or _grid_client)(login)
            _cl_payloads = _cl_grid._read_unified_campaign_update_payloads(v5_campaign_ids)
            for _cl_cid, _cl_payload in (_cl_payloads or {}).items():
                _cl_raw = (_cl_payload.get("inheritableSitelinkSet") or {}).get("sitelinkSetId")
                try:
                    _cl_sid = int(_cl_raw or 0)
                    _cl_cid_i = int(_cl_cid)
                except (TypeError, ValueError):
                    continue
                if _cl_cid_i <= 0 or _cl_sid <= 0:
                    continue
                _cl_list = campaign_ids_by_set.setdefault(str(_cl_sid), [])
                if _cl_cid_i not in _cl_list:
                    _cl_list.append(_cl_cid_i)
                _cl_usages = sitelink_usages.setdefault(str(_cl_sid), [])
                if not any(u.get("campaign_id") == _cl_cid_i and int(u.get("adgroup_id") or 0) == 0
                           for u in _cl_usages):
                    _cl_usages.append(_usage_for(_cl_cid_i, 0))
        except Exception as e:  # noqa: BLE001 - read-only enrichment must not break editor load
            grid_sitelink_error = f"Grid campaign-level sitelinks: {str(e)[:200]}"

    # 4) Наборы быстрых ссылок. Direct API requires explicit set ids.
    sitelink_set_ids = sorted(int(sid) for sid in sitelink_usages if str(sid).isdigit())
    sets = []
    if sitelink_set_ids:
        sets, err = _v5_paginate(
            v5_call, "sitelinks", token, login,
            {"SelectionCriteria": {"Ids": sitelink_set_ids}, "FieldNames": ["Id", "Sitelinks"]},
            "SitelinksSets",
        )
        if err:
            return {"error": f"sitelinks.get: {err}"}
    sitelinks_out: list[dict] = []
    for s in sets:
        sid = str(s.get("Id") or "")
        items = [{"title": it.get("Title") or "",
                  "href": it.get("Href") or "",
                  "description": it.get("Description") or ""}
                 for it in (s.get("Sitelinks") or [])]
        title = items[0]["title"] if items else f"Набор {sid}"
        camp_ids = campaign_ids_by_set.get(sid, [])
        sitelinks_out.append({
            "set_id": int(s.get("Id") or 0),
            "set_title": title,
            "items": items,
            "usages": sitelink_usages.get(sid, []),
            # level="campaign" — набор привязан на уровне кампании (наследуется
            # объявлениями); replace идёт через set_campaign_sitelink_set, а не
            # ad-level find/replace. level="ad" — обычный ad-level override.
            "level": "campaign" if camp_ids else "ad",
            "campaign_ids": camp_ids,
        })
    # UAC (tp6/tp7) быстрые ссылки — синтетические наборы уровня кампании (source="uac").
    sitelinks_out.extend(uac_sitelinks_out)

    # 5) Уточнения (callouts) — adextensions type CALLOUT.
    exts, err = _v5_paginate(
        v5_call, "adextensions", token, login,
        {"SelectionCriteria": {"Types": ["CALLOUT"]},
         "FieldNames": ["Id", "Type"], "CalloutFieldNames": ["CalloutText"]},
        "AdExtensions",
    )
    if err:
        return {"error": f"adextensions.get: {err}"}
    try:
        campaign_callout_ids = _grid_campaign_callout_ids(
            login,
            campaign_ids,
            grid_client_factory=grid_client_factory,
        )
        for cid, callout_ids in campaign_callout_ids.items():
            for eid in callout_ids:
                callout_to_camps.setdefault(str(eid), []).append(cid)
    except Exception as e:  # noqa: BLE001 - read-only enrichment must not break editor load
        campaign_callout_ids = {}
        grid_callout_error = f"Grid callout-usages: {str(e)[:200]}"
    else:
        grid_callout_error = None
    callouts_out: list[dict] = []
    for e in exts:
        eid = str(e.get("Id") or "")
        text = (e.get("Callout") or {}).get("CalloutText") or ""
        usages = [_usage_for(cid, 0) for cid in callout_to_camps.get(eid, [])]
        callouts_out.append({"id": int(e.get("Id") or 0), "text": text, "usages": usages})

    out = {"callouts": callouts_out, "sitelinks": sitelinks_out, "ads": ads_out,
           "links": links_out,
           "_ads_by_set": ads_by_set, "_campaign_callout_ids": campaign_callout_ids}
    if grid_callout_error:
        out["_grid_callout_error"] = grid_callout_error
    if grid_sitelink_error:
        out["_grid_sitelink_error"] = grid_sitelink_error
    if uac_read_error:
        out["_uac_read_error"] = uac_read_error
    return out


# ───────────────────────────── replace / preview ─────────────────────────────

_AD_FIELD = {"ad_title": "title", "ad_title2": "title2", "ad_text": "text"}
_AD_API_FIELD = {"ad_title": "Title", "ad_title2": "Title2", "ad_text": "Text"}
_SITELINK_TYPES = {"sitelink_title", "sitelink_description", "sitelink_href"}
# Тип замены → поле элемента быстрой ссылки. sitelink_href правит САМ URL (Href)
# элемента — приоритет UAC (tp6/tp7), где посадочная ссылка живёт в детали кампании.
_SITELINK_FIELD = {
    "sitelink_title": "title",
    "sitelink_description": "description",
    "sitelink_href": "href",
}
# Типы заданий, которым нужен блок 3c (_load_account campaign-level наборы): и точечные
# замены поля быстрой ссылки, и позиционная перестановка порядка (sitelink_reorder).
_SITELINK_JOB_TYPES = _SITELINK_TYPES | {"sitelink_reorder"}


def _normalize_callout_text(text: str) -> str:
    """Keep callout text within Direct's conservative symbol set."""
    clean = re.sub(r"[^0-9A-Za-zА-Яа-яЁё%+\- ₽]", " ", str(text or ""))
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:25]


def _match_targets(content: dict, typ: str, old_text: str,
                   mode: str = "exact", new_text: str = "") -> list[dict]:
    """Список объектов, где встречается ``old_text`` (для preview и replace).

    ``mode="exact"`` — совпадение целого поля (legacy: точечная замена одного
    заголовка/текста). ``mode="substring"`` — поле СОДЕРЖИТ ``old_text``: массовая
    замена фрагмента (структуры) во всех заголовках/текстах разом; в каждый hit
    кладётся ``before``/``after`` для превью. Подстрочный режим — только ad-поля.
    """
    old = _frag_trim(old_text)
    new = _frag_trim(new_text)
    hits: list[dict] = []
    if typ in _AD_FIELD:
        fld = _AD_FIELD[typ]
        for ad in content.get("ads", []):
            val = ad.get(fld) or ""
            if mode == "substring":
                if not old or old not in val:
                    continue
                extra = {"before": val, "after": val.replace(old, new)}
            else:
                if val.strip() != old:
                    continue
                extra = {}
            hits.append({
                "ad_id": ad["ad_id"],
                "campaign_id": ad.get("campaign_id"),
                "usages": ad.get("usages", []),
                "source": ad.get("source"),  # "uac" for tp6/tp7 entries
                **extra,
            })
    elif mode == "substring":
        # Подстрочная массовая замена поддержана только для заголовков/текстов.
        return hits
    elif typ == "callout":
        for co in content.get("callouts", []):
            if (co.get("text") or "").strip() == old:
                hits.append({"id": co["id"], "usages": co.get("usages", [])})
    elif typ in _SITELINK_TYPES:
        field = _SITELINK_FIELD.get(typ, "title")
        for s in content.get("sitelinks", []):
            if not any((it.get(field) or "").strip() == old for it in s.get("items", [])):
                continue
            if s.get("source") == "uac":
                # UAC (tp6/tp7): быстрые ссылки живут в детали кампании; запись
                # идёт cookie-PATCH /web-api/uac/campaign/{id}, а не через Grid-набор.
                hits.append({
                    "set_id": s.get("set_id"),
                    "items": s.get("items", []),
                    "usages": s.get("usages", []),
                    "source": "uac",
                    "campaign_id": s.get("campaign_id"),
                })
            else:
                hits.append({
                    "set_id": s["set_id"],
                    "items": s.get("items", []),
                    "usages": s.get("usages", []),
                    "ad_ids": _ads_using_set(content, int(s.get("set_id") or 0)),
                    "ad_items": content.get("_ads_by_set", {}).get(str(s.get("set_id")), []),
                })
    return hits


def _replace_adaptive_ad_texts(
    login: str,
    typ: str,
    old_text: str,
    new_text: str,
    targets: list[dict],
    *,
    grid_client_factory: Callable | None = None,
) -> dict:
    """Replace title/body text through cookie/Grid ``findAndReplaceText``."""
    old = _frag_trim(old_text)
    new = _frag_trim(new_text)
    if not targets:
        return {"replaced": 0, "errors": ["объявление с таким текстом не найдено"]}
    ad_ids: list[int] = []
    for target in targets:
        try:
            aid = int(target.get("ad_id"))
        except (TypeError, ValueError):
            continue
        if aid > 0 and aid not in ad_ids:
            ad_ids.append(aid)
    grid = (grid_client_factory or _grid_client)(login)
    target_type = {
        "ad_title": "TITLE",
        "ad_title2": "TITLE_EXTENSION",
        "ad_text": "BODY",
    }.get(typ)
    out = grid.find_and_replace_text(
        ad_ids,
        target_types=[target_type],
        search=old,
        replace=new,
        case_sensitive=True,
    )
    return {"replaced": int(out.get("replaced") or 0), "errors": out.get("errors") or []}


def _replace_uac_texts(
    login: str,
    typ: str,
    old_text: str,
    new_text: str,
    targets: list[dict],
    *,
    mode: str = "exact",
    uac_client_factory: Callable | None = None,
) -> dict:
    """Replace tp6/tp7 UAC title/body text through cookie PATCH."""
    old = _frag_trim(old_text)
    new = _frag_trim(new_text)
    if not old:
        return {"replaced": 0, "errors": ["старый текст пустой"]}
    if not new:
        return {"replaced": 0, "errors": ["новый текст пустой"]}
    field_key = "texts" if typ == "ad_text" else "titles"
    campaign_ids: list[int] = []
    for target in targets or []:
        raw_cid = target.get("campaign_id")
        if not raw_cid:
            for usage in target.get("usages") or []:
                raw_cid = usage.get("campaign_id")
                if raw_cid:
                    break
        try:
            cid = int(raw_cid)
        except (TypeError, ValueError):
            continue
        if cid > 0 and cid not in campaign_ids:
            campaign_ids.append(cid)
    if not campaign_ids:
        return {"replaced": 0, "errors": ["не найдены UAC-кампании для замены"]}

    if uac_client_factory is None:
        from .uac_read import UacReadClient

        client = UacReadClient(login).client
    else:
        client = uac_client_factory(login)

    replaced = 0
    errors: list[str] = []
    updated_campaigns: list[int] = []
    for cid in campaign_ids:
        try:
            detail = _unwrap_uac_response(client._request("GET", f"/campaign/{cid}", step=f"uac-detail:{cid}"))
            field_value = detail.get(field_key)
            if not isinstance(field_value, list) and field_key == "titles":
                field_key_candidates = ["titles", "title_items"]
            elif not isinstance(field_value, list) and field_key == "texts":
                field_key_candidates = ["texts", "text_items"]
            else:
                field_key_candidates = [field_key]
            changed = 0
            patched = False
            for candidate in field_key_candidates:
                current = detail.get(candidate)
                if not isinstance(current, list):
                    continue
                next_items, candidate_changed = _uac_replace_text_items(current, old, new, mode)
                if not candidate_changed:
                    continue
                _uac_patch_campaign_texts(client, cid, candidate, next_items)
                changed += candidate_changed
                patched = True
                break
            if not patched:
                errors.append(f"кампания {cid}: текст не найден в UAC detail")
                continue
            # Read-back verifies that the target field now contains the new value.
            after = _unwrap_uac_response(client._request("GET", f"/campaign/{cid}", step=f"uac-readback:{cid}"))
            after_values = after.get(candidate) if isinstance(after.get(candidate), list) else []
            after_texts = [_uac_text_item_text(item) for item in after_values]
            if mode == "substring":
                # фрагмент заменён → старой подстроки быть не должно, новая — присутствует
                confirmed = any(new in t for t in after_texts) and not any(old in t for t in after_texts)
            else:
                confirmed = new in after_texts
            if not confirmed:
                errors.append(f"кампания {cid}: read-back не подтвердил новый текст")
                continue
            replaced += changed
            updated_campaigns.append(cid)
        except Exception as e:  # noqa: BLE001
            errors.append(f"кампания {cid}: {str(e)[:180]}")
    return {"replaced": replaced, "errors": errors, "updated_uac_campaigns": updated_campaigns}


def _replace_uac_sitelinks(
    login: str,
    field: str,
    old_text: str,
    new_text: str,
    targets: list[dict],
    *,
    uac_client_factory: Callable | None = None,
) -> dict:
    """Заменить поле быстрой ссылки (title/description/href) в UAC-кампаниях (tp6/tp7)
    через cookie-PATCH ``/web-api/uac/campaign/{id}`` по полю ``sitelinks``.

    ``field`` — одно из ``title``/``description``/``href``. Матч по точному значению
    поля элемента; в UAC быстрые ссылки нередко имеют ОДИН общий href — тогда смена
    href меняет посадочную у всех совпавших элементов. Read-back перечитывает деталь
    кампании и подтверждает, что новое значение есть, а старого — нет."""
    old = _frag_trim(old_text)
    new = _frag_trim(new_text)
    if not old:
        return {"replaced": 0, "errors": ["старое значение быстрой ссылки пустое"]}
    if not new:
        return {"replaced": 0, "errors": ["новое значение быстрой ссылки пустое"]}
    if field not in ("title", "description", "href"):
        return {"replaced": 0, "errors": [f"неподдерживаемое поле быстрой ссылки: {field}"]}
    campaign_ids: list[int] = []
    for target in targets or []:
        raw_cid = target.get("campaign_id")
        if not raw_cid:
            for usage in target.get("usages") or []:
                raw_cid = usage.get("campaign_id")
                if raw_cid:
                    break
        try:
            cid = int(raw_cid)
        except (TypeError, ValueError):
            continue
        if cid > 0 and cid not in campaign_ids:
            campaign_ids.append(cid)
    if not campaign_ids:
        return {"replaced": 0, "errors": ["не найдены UAC-кампании для замены быстрой ссылки"]}

    if uac_client_factory is None:
        from .uac_read import UacReadClient

        client = UacReadClient(login).client
    else:
        client = uac_client_factory(login)

    replaced = 0
    errors: list[str] = []
    updated_campaigns: list[int] = []
    for cid in campaign_ids:
        try:
            detail = _unwrap_uac_response(
                client._request("GET", f"/campaign/{cid}", step=f"uac-detail:{cid}"))
            current = detail.get("sitelinks")
            if not isinstance(current, list):
                errors.append(f"кампания {cid}: у кампании нет быстрых ссылок")
                continue
            changed = 0
            next_items: list = []
            for item in current:
                if isinstance(item, dict) and (item.get(field) or "").strip() == old:
                    nxt = dict(item)
                    nxt[field] = new
                    next_items.append(nxt)
                    changed += 1
                else:
                    next_items.append(item)
            if not changed:
                errors.append(f"кампания {cid}: значение быстрой ссылки не найдено")
                continue
            _uac_patch_campaign_texts(client, cid, "sitelinks", next_items)
            # Read-back: перечитываем деталь и проверяем, что новое значение есть, старого — нет.
            after = _unwrap_uac_response(
                client._request("GET", f"/campaign/{cid}", step=f"uac-sl-readback:{cid}"))
            after_sl = after.get("sitelinks") if isinstance(after.get("sitelinks"), list) else []
            after_vals = [(x.get(field) or "").strip() for x in after_sl if isinstance(x, dict)]
            if new in after_vals and old not in after_vals:
                replaced += changed
                updated_campaigns.append(cid)
            else:
                errors.append(f"кампания {cid}: read-back не подтвердил новое значение быстрой ссылки")
        except Exception as e:  # noqa: BLE001
            errors.append(f"кампания {cid}: {str(e)[:180]}")
    return {"replaced": replaced, "errors": errors, "updated_uac_campaigns": updated_campaigns}


_REBIND_SUBTYPE_FIELDS = {"TextAd": "TextAdFieldNames", "DynamicTextAd": "DynamicTextAdFieldNames"}
_SITELINK_READ_FIELDS = {"TextAd": "TextAdFieldNames", "DynamicTextAd": "DynamicTextAdFieldNames",
                         "ResponsiveAd": "ResponsiveAdFieldNames"}


def _confirm_ads_sitelink_text(v5_call: Callable, token: str, login: str,
                               ad_items: list[dict], field: str,
                               old: str, new: str) -> tuple[int, list[str]]:
    """Read-back после Grid findAndReplaceText: у скольких объявлений набор ссылок
    реально содержит новый текст (и не содержит старый). Grid может вернуть
    successCount, ничего не изменив (проверено live на GdTextAd) — поэтому
    доверяем только перечитке."""
    by_subtype: dict[str, list[int]] = {}
    for it in ad_items or []:
        st = str((it or {}).get("subtype") or "ResponsiveAd")
        if st in _SITELINK_READ_FIELDS:
            by_subtype.setdefault(st, []).append(int(it["ad_id"]))
    errors: list[str] = []
    set_by_ad: dict[int, int] = {}
    for st, ids in by_subtype.items():
        got, err = _v5_paginate(
            v5_call, "ads", token, login,
            {"SelectionCriteria": {"Ids": ids[:10000]}, "FieldNames": ["Id"],
             _SITELINK_READ_FIELDS[st]: ["SitelinkSetId"]},
            "Ads")
        if err:
            errors.append(f"read-back ads.get: {err}")
            continue
        for a in got:
            sid = (a.get(st) or {}).get("SitelinkSetId")
            if sid:
                set_by_ad[int(a.get("Id") or 0)] = int(sid)
    set_ok: dict[int, bool] = {}
    uniq_sets = sorted(set(set_by_ad.values()))
    for chunk in [uniq_sets[i:i + 100] for i in range(0, len(uniq_sets), 100)]:
        j = v5_call("sitelinks", "get", token, login,
                    {"SelectionCriteria": {"Ids": chunk}, "FieldNames": ["Id", "Sitelinks"]})
        if j.get("error"):
            errors.append("read-back sitelinks.get: " + json.dumps(j["error"], ensure_ascii=False)[:120])
            continue
        for s in (j.get("result") or {}).get("SitelinksSets") or []:
            key = {"description": "Description", "href": "Href"}.get(field, "Title")
            vals = [(x.get(key) or "").strip() for x in s.get("Sitelinks") or []]
            set_ok[int(s.get("Id") or 0)] = (new in vals) and (old not in vals)
    confirmed = sum(1 for aid, sid in set_by_ad.items() if set_ok.get(sid))
    unconfirmed = len(set_by_ad) - confirmed
    if unconfirmed > 0:
        errors.append(f"замена текста ссылки не подтвердилась у {unconfirmed} объявлений — "
                      "Grid не применил изменение")
    return confirmed, errors


def _v5_rebind_ads_sitelink_set(v5_call: Callable, token: str, login: str,
                                ad_items: list[dict], new_set_id: int) -> tuple[int, list[str]]:
    """v5 ads.update: перепривязать SitelinkSetId у объявлений + read-back.

    ad_items: [{ad_id, subtype}] — подтип обязателен: TextAd/DynamicTextAd идут своим
    ключом в ads.update; прочие (ResponsiveAd) v5 не обновляет — честная ошибка.
    """
    errors: list[str] = []
    by_subtype: dict[str, list[int]] = {}
    for it in ad_items or []:
        try:
            aid = int((it or {}).get("ad_id") or 0)
        except (TypeError, ValueError):
            continue
        if aid > 0:
            st = str((it or {}).get("subtype") or "TextAd")
            if aid not in by_subtype.setdefault(st, []):
                by_subtype[st].append(aid)
    for st in [s for s in by_subtype if s not in _REBIND_SUBTYPE_FIELDS]:
        errors.append(f"{len(by_subtype[st])} объявлений типа {st}: "
                      "перепривязка набора быстрых ссылок через v5 не поддерживается")
        by_subtype.pop(st)

    updated_total = 0
    for subtype, ad_ids in by_subtype.items():
        ok_ids: list[int] = []
        for chunk in [ad_ids[i:i + 500] for i in range(0, len(ad_ids), 500)]:
            payload = {"Ads": [{"Id": aid, subtype: {"SitelinkSetId": new_set_id}} for aid in chunk]}
            j = {}
            for attempt in range(3):  # error 1000 «Сервис временно недоступен» — транзиент, ретраим
                j = v5_call("ads", "update", token, login, payload)
                code = (j.get("error") or {}).get("error_code")
                if code not in (1000, 1001, 1002, 52, 500):
                    break
                time.sleep(5 * (attempt + 1))
            if j.get("error"):
                errors.append("ads.update: " + json.dumps(j["error"], ensure_ascii=False)[:160])
                continue
            results = (j.get("result") or {}).get("UpdateResults") or []
            if len(results) != len(chunk):
                errors.append(f"ads.update: получено {len(results)} результатов на {len(chunk)} объявлений")
            for res in results:
                if res.get("Errors"):
                    msg = "; ".join((e.get("Message") or "") for e in res["Errors"])
                    errors.append(f"объявление {res.get('Id')}: {msg[:120]}")
                elif res.get("Id"):
                    ok_ids.append(int(res["Id"]))
                else:
                    errors.append("ads.update: результат без Id и Errors: "
                                  + json.dumps(res, ensure_ascii=False)[:100])
        if not ok_ids:
            continue
        confirmed: set[int] = set()
        rb_failed = False
        for rb_chunk in [ok_ids[i:i + 10000] for i in range(0, len(ok_ids), 10000)]:
            got, err = _v5_paginate(
                v5_call, "ads", token, login,
                {"SelectionCriteria": {"Ids": rb_chunk}, "FieldNames": ["Id"],
                 _REBIND_SUBTYPE_FIELDS[subtype]: ["SitelinkSetId"]},
                "Ads")
            if err:
                errors.append(f"read-back ads.get: {err}")
                rb_failed = True
                break
            confirmed |= {
                int(a.get("Id") or 0) for a in got
                if int(((a.get(subtype) or {}).get("SitelinkSetId") or 0)) == int(new_set_id)
            }
        if rb_failed:
            continue
        bad = [aid for aid in ok_ids if aid not in confirmed]
        if bad:
            errors.append(f"read-back не подтвердил новый набор у {len(bad)} объявлений")
        updated_total += len(confirmed & set(ok_ids))
    return updated_total, errors


def _replace_sitelink_text_grid(
    login: str,
    typ: str,
    old_text: str,
    new_text: str,
    targets: list[dict],
    *,
    token: str | None = None,
    v5_call: Callable | None = None,
    grid_client_factory: Callable | None = None,
) -> dict:
    """Replace sitelink title/description by creating new sets and repointing campaigns.

    In current EPK accounts sitelinks are campaign-level inheritable assets:
    campaign has ``inheritableSitelinkSet.assetValue`` and ads inherit it.
    Grid ``findAndReplaceText`` is unstable on hundreds of inherited ads, so
    the safe path is set-level replacement.
    """
    old = _frag_trim(old_text)
    new = _frag_trim(new_text)
    field = _SITELINK_FIELD.get(typ, "title")
    if not targets:
        return {"replaced": 0, "errors": ["набор с таким текстом быстрой ссылки не найден"]}
    if not new:
        return {"replaced": 0, "errors": ["новое значение быстрой ссылки пустое"]}
    # У title/description — лимиты Директа; у href лимит на длину не применяем.
    if field != "href":
        limit = 60 if typ == "sitelink_description" else 30
        if len(new) > limit:
            return {"replaced": 0, "errors": [f"текст быстрой ссылки длиннее {limit} символов"]}
    grid = (grid_client_factory or _grid_client)(login)
    all_campaign_ids: list[int] = []
    for target in targets or []:
        for usage in target.get("usages") or []:
            try:
                cid = int(usage.get("campaign_id"))
            except (TypeError, ValueError):
                continue
            if cid > 0 and cid not in all_campaign_ids:
                all_campaign_ids.append(cid)
    current_set_by_campaign: dict[int, int] = {}
    unsupported_by_cid: dict[int, str] = {}
    if all_campaign_ids and hasattr(grid, "_read_unified_campaign_update_payloads"):
        try:
            payloads = grid._read_unified_campaign_update_payloads(all_campaign_ids)
            for cid, payload in payloads.items():
                if payload.get("_unsupported_strategy"):
                    unsupported_by_cid[int(cid)] = str(payload["_unsupported_strategy"])
                raw_sid = (payload.get("inheritableSitelinkSet") or {}).get("sitelinkSetId")
                try:
                    sid = int(raw_sid or 0)
                except (TypeError, ValueError):
                    sid = 0
                if cid > 0 and sid > 0:
                    current_set_by_campaign[int(cid)] = sid
        except Exception as e:  # noqa: BLE001
            errors = [f"не удалось прочитать текущие быстрые ссылки кампаний через Grid: {str(e)[:180]}"]
            return {"replaced": 0, "errors": errors}
    replaced = 0
    errors: list[str] = []
    created_sets: list[int] = []
    touched_campaigns: set[int] = set()
    touched_ads: set[int] = set()
    for target in targets or []:
        try:
            source_set_id = int(target.get("set_id") or 0)
        except (TypeError, ValueError):
            source_set_id = 0
        items = []
        changed = False
        for item in target.get("items") or []:
            next_item = {
                "title": item.get("title") or "",
                "href": item.get("href") or "",
                "description": item.get("description") or "",
            }
            if (next_item.get(field) or "").strip() == old:
                next_item[field] = new
                changed = True
            items.append(next_item)
        if not changed:
            continue
        campaign_ids: list[int] = []
        for usage in target.get("usages") or []:
            try:
                cid = int(usage.get("campaign_id"))
            except (TypeError, ValueError):
                continue
            if cid > 0 and cid not in campaign_ids:
                campaign_ids.append(cid)
        # Перепривязываем ТОЛЬКО кампании, у которых campaign-level набор совпадает
        # с исходным. Пустая карта = у аккаунта наборы на уровне объявлений —
        # привязывать новый набор всем кампаниям подряд нельзя.
        campaign_ids = [
            cid for cid in campaign_ids
            if current_set_by_campaign.get(cid) == source_set_id and cid not in touched_campaigns
        ]
        # Кампании с неподдерживаемой стратегией отфильтровываем ДО мутации,
        # чтобы одна такая не завалила весь батч (набор уже был бы создан).
        for cid in [c for c in campaign_ids if c in unsupported_by_cid]:
            errors.append(f"кампания {cid}: стратегия «{unsupported_by_cid[cid]}» "
                          "не поддерживается — быстрая ссылка не заменена")
        campaign_ids = [c for c in campaign_ids if c not in unsupported_by_cid]
        # Объявления с ad-level привязкой исходного набора (обычные ЕПК/текстовые аккаунты).
        ad_items: list[dict] = []
        seen_ads: set[int] = set()
        for raw in target.get("ad_items") or [{"ad_id": x} for x in (target.get("ad_ids") or [])]:
            try:
                aid = int((raw or {}).get("ad_id") or 0)
            except (TypeError, ValueError):
                continue
            if aid > 0 and aid not in touched_ads and aid not in seen_ads:
                seen_ads.add(aid)
                ad_items.append({"ad_id": aid, "subtype": (raw or {}).get("subtype") or "TextAd"})
        # TextAd/DynamicTextAd перепривязываются на новый набор через v5 ads.update;
        # комбинаторные (ResponsiveAd) и прочие — через куки/Grid findAndReplaceText:
        # правит тексты ссылок прямо в объявлениях, БЕЗ баллов v5.
        v5_items = [it for it in ad_items if it.get("subtype") in ("TextAd", "DynamicTextAd")]
        grid_fr_items = [it for it in ad_items if it.get("subtype") not in ("TextAd", "DynamicTextAd")]
        if not campaign_ids and not v5_items and not grid_fr_items:
            continue
        try:
            new_set_id = None
            if campaign_ids or v5_items:
                # новый набор нужен только campaign- и v5-веткам; findAndReplace создаёт свой сам
                new_set_id = grid.add_sitelink_set(items)
                if not new_set_id:
                    errors.append(f"набор {target.get('set_id')}: Grid не вернул id нового набора быстрых ссылок")
                else:
                    created_sets.append(int(new_set_id))
            if campaign_ids and new_set_id:
                updated = grid.set_campaign_sitelink_set(campaign_ids, int(new_set_id))
                if updated:
                    updated_ids = []
                    for row in updated:
                        try:
                            cid = int((row or {}).get("id") or 0)
                        except (TypeError, ValueError):
                            cid = 0
                        if cid > 0:
                            updated_ids.append(cid)
                    replaced += len(updated_ids)
                    touched_campaigns.update(updated_ids)
                else:
                    errors.append(f"набор {target.get('set_id')}: Grid не подтвердил перепривязку кампаний")
            if v5_items and new_set_id:
                if not (token and v5_call):
                    errors.append(f"набор {target.get('set_id')}: нет v5-контекста для перепривязки объявлений")
                else:
                    ok_ads, ad_errs = _v5_rebind_ads_sitelink_set(v5_call, token, login, v5_items, int(new_set_id))
                    replaced += ok_ads
                    if ok_ads:
                        touched_ads.update(it["ad_id"] for it in v5_items)
                    errors.extend(ad_errs)
            if grid_fr_items:
                fr_ids = [it["ad_id"] for it in grid_fr_items]
                fr_target = {"sitelink_description": "SITELINK_DESCRIPTION",
                             "sitelink_href": "SITELINK_HREF"}.get(typ, "SITELINK_TITLE")
                fr = grid.find_and_replace_text(
                    fr_ids, target_types=[fr_target],
                    search=old, replace=new, case_sensitive=True)
                missed = int(fr.get("total") or 0) - int(fr.get("replaced") or 0)
                if missed > 0:
                    errors.append(f"Grid findAndReplace (комбинаторные): не заменено у {missed} объявлений")
                # successCount Грида не доказательство (может «успешно» ничего не менять) —
                # считаем заменёнными только объявления, подтверждённые перечиткой наборов.
                if not (token and v5_call):
                    errors.append("нет v5-контекста для read-back комбинаторных объявлений")
                else:
                    confirmed, rb_errs = _confirm_ads_sitelink_text(
                        v5_call, token, login, grid_fr_items, field, old, new)
                    replaced += confirmed
                    if confirmed:
                        touched_ads.update(fr_ids)
                    errors.extend(rb_errs)
        except Exception as e:  # noqa: BLE001
            errors.append(f"набор {target.get('set_id')}: {str(e)[:180]}")
    if not replaced and not errors:
        errors.append("не найдены кампании или объявления, привязанные к набору со старым текстом")
    return {"replaced": replaced, "errors": errors, "new_sitelink_set_ids": created_sets}


def _validate_permutation(perm) -> tuple[list[int], str]:
    """Проверяет, что ``perm`` — биекция позиций 0..N-1 (N≥2), не тождественная.

    Возвращает (нормализованный список int, "") при валидности или ([], причина)."""
    try:
        p = [int(x) for x in (perm or [])]
    except (TypeError, ValueError):
        return [], "перестановка должна быть списком целых индексов позиций"
    n = len(p)
    if n < 2:
        return [], "перестановка должна содержать минимум 2 позиции"
    if sorted(p) != list(range(n)):
        return [], "перестановка должна быть биекцией позиций 0..N-1 (без повторов/пропусков)"
    if p == list(range(n)):
        return [], "перестановка тождественна — порядок не меняется"
    return p, ""


def _reorder_sitelinks(
    token: str,
    login: str,
    perm: list[int],
    content: dict,
    v5_call: Callable,
    *,
    grid_client_factory: Callable | None = None,
    uac_client_factory: Callable | None = None,
) -> dict:
    """Позиционная перестановка (permutation по индексам) быстрых ссылок во ВСЕХ
    наборах аккаунта: ``result[i] = items[perm[i]]`` для первых ``len(perm)`` позиций;
    хвост (позиции ≥ len(perm)) остаётся на месте.

    Наборы, где ссылок МЕНЬШЕ длины перестановки (позиция за пределами длины),
    ПРОПУСКАЮТСЯ с явным отчётом (не падаем, не режем молча). Пути записи по типам:
      • UAC (tp6/7) → PATCH массива ``sitelinks`` (осн. ссылку UAC не трогаем);
      • campaign-level (inheritableSitelinkSet) → ``add_sitelink_set`` + ``set_campaign_sitelink_set``;
      • ad-level TextAd/DynamicTextAd → ``add_sitelink_set`` + v5 rebind;
      • ad-level ResponsiveAd → честный skip «не поддерживается» (хрупкость Grid).

    Возврат-безопасность: для RK дедуп ``add_sitelink_set`` даёт бесплатный откат к
    исходному set_id при идентичном содержимом; для UAC исходный порядок сохраняется
    в отчёте (``orig_order``) — обратная перестановка восстанавливает байт-в-байт.
    """
    perm, why = _validate_permutation(perm)
    if not perm:
        return {"replaced": 0, "errors": [why], "reports": []}
    n = len(perm)

    def _apply(items: list) -> list:
        return [items[p] for p in perm] + list(items[n:])

    def _sl_tuple(x) -> tuple:
        """Полный кортеж-идентичность быстрой ссылки: (title, href, description).
        Сравнение порядка ТОЛЬКО по title ложно-негативит swap ссылок с одинаковым
        title, но разными href/description (finding #2) — сверяем весь кортеж."""
        if not isinstance(x, dict):
            return ("", "", "")
        return (
            (x.get("title") or "").strip(),
            (x.get("href") or "").strip(),
            (x.get("description") or "").strip(),
        )

    reports: list[dict] = []
    errors: list[str] = []
    # Детализация затронутого — РАЗНЫЕ единицы, не смешивать в один счётчик (finding #3):
    campaigns_touched = 0   # кампаний перепривязано (campaign-level)
    ads_touched = 0         # объявлений перепривязано (ad-level TextAd/DynamicTextAd)
    uac_sets = 0            # UAC-наборов переставлено
    grid = None
    uac_client = None
    for s in content.get("sitelinks", []):
        items = s.get("items") or []
        set_id = s.get("set_id")
        source = s.get("source")
        level = "uac" if source == "uac" else (s.get("level") or "ad")
        before_titles = [(it.get("title") or "") for it in items]
        rep: dict = {"set_id": set_id, "set_title": s.get("set_title") or "",
                     "level": level, "before": before_titles}
        if len(items) < n:
            rep["status"] = "skipped"
            rep["reason"] = f"в наборе {len(items)} ссылок — перестановка требует {n}"
            reports.append(rep)
            continue
        # finding #5: элемент быстрой ссылки в наборе состоит РОВНО из
        # {title, href, description}. И v5 sitelinks.get (Title/Href/Description),
        # и Grid get_sitelink_sets (title/description/href), и запись
        # add_sitelink_set (title/href/description) оперируют этой же тройкой; per-item
        # `id` назначается сервером и на создании нового набора не пересылается. Поэтому
        # позиционная перестановка снимка `items` не теряет полей набора для
        # campaign-level/ad-level. (UAC-ветка отдельно перечитывает живую деталь.)
        new_items = _apply(items)
        after_titles = [(it.get("title") or "") for it in new_items]
        rep["after"] = after_titles
        # «Изменился ли порядок» — по ПОЛНОМУ кортежу (title,href,description), не только
        # по title: swap ссылок с одинаковым title, но разными href/desc — реальное
        # изменение, которое title-сравнение проглатывает как «без изменений» (finding #2).
        if [_sl_tuple(it) for it in new_items] == [_sl_tuple(it) for it in items]:
            rep["status"] = "skipped"
            rep["reason"] = "порядок не изменился"
            reports.append(rep)
            continue
        try:
            if source == "uac":
                try:
                    cid = int(s.get("campaign_id") or 0)
                except (TypeError, ValueError):
                    cid = 0
                if cid <= 0:
                    rep["status"] = "skipped"
                    rep["reason"] = "не удалось определить UAC-кампанию"
                    reports.append(rep)
                    continue
                if uac_client is None:
                    if uac_client_factory is None:
                        from .uac_read import UacReadClient

                        uac_client = UacReadClient(login).client
                    else:
                        uac_client = uac_client_factory(login)
                # Перечитываем деталь кампании и переставляем РЕАЛЬНЫЙ текущий массив
                # (полные элементы, не наш обрезанный снимок) — для byte-safe записи.
                detail = _unwrap_uac_response(
                    uac_client._request("GET", f"/campaign/{cid}", step=f"uac-detail:{cid}"))
                cur = detail.get("sitelinks")
                if not isinstance(cur, list) or len(cur) < n:
                    rep["status"] = "skipped"
                    rep["reason"] = f"UAC деталь: {len(cur) if isinstance(cur, list) else 0} ссылок < {n}"
                    reports.append(rep)
                    continue
                rep["orig_order"] = [((x.get("title") or "") if isinstance(x, dict) else "") for x in cur]
                reordered = _apply(cur)
                _uac_patch_campaign_texts(uac_client, cid, "sitelinks", reordered)
                after = _unwrap_uac_response(
                    uac_client._request("GET", f"/campaign/{cid}", step=f"uac-reorder-rb:{cid}"))
                after_sl = after.get("sitelinks") if isinstance(after.get("sitelinks"), list) else []
                # Порядок сверяем по ПОЛНОМУ кортежу (title,href,description), а не только
                # по title — иначе swap ссылок с одинаковым title даёт ложный «read-back
                # не подтвердил» при реально применённой перестановке (finding #2).
                after_tup = [_sl_tuple(x) for x in after_sl]
                exp_tup = [_sl_tuple(x) for x in reordered]
                cur_tup = [_sl_tuple(x) for x in cur]
                if after_tup[:len(exp_tup)] == exp_tup and after_tup != cur_tup:
                    rep["status"] = "applied"
                    uac_sets += 1
                else:
                    rep["status"] = "error"
                    rep["reason"] = "read-back не подтвердил новый порядок"
                    errors.append(f"UAC {cid}: read-back не подтвердил порядок быстрых ссылок")
                reports.append(rep)
                continue

            if level == "campaign":
                campaign_ids = []
                for c in s.get("campaign_ids") or []:
                    try:
                        ci = int(c)
                    except (TypeError, ValueError):
                        continue
                    if ci > 0 and ci not in campaign_ids:
                        campaign_ids.append(ci)
                if not campaign_ids:
                    rep["status"] = "skipped"
                    rep["reason"] = "нет кампаний, привязанных к набору уровня кампании"
                    reports.append(rep)
                    continue
                if grid is None:
                    grid = (grid_client_factory or _grid_client)(login)
                new_set_id = grid.add_sitelink_set(new_items)
                if not new_set_id:
                    rep["status"] = "error"
                    rep["reason"] = "Grid не вернул id нового набора"
                    errors.append(f"набор {set_id}: Grid не вернул id нового набора")
                    reports.append(rep)
                    continue
                updated = grid.set_campaign_sitelink_set(campaign_ids, int(new_set_id))
                upd_ids = []
                for row in updated or []:
                    try:
                        ci = int((row or {}).get("id") or 0)
                    except (TypeError, ValueError):
                        ci = 0
                    if ci > 0:
                        upd_ids.append(ci)
                if upd_ids:
                    rep["status"] = "applied"
                    rep["new_set_id"] = int(new_set_id)
                    rep["campaign_ids"] = upd_ids
                    campaigns_touched += len(upd_ids)
                else:
                    rep["status"] = "error"
                    rep["reason"] = "Grid не подтвердил перепривязку кампаний"
                    errors.append(f"набор {set_id}: перепривязка кампаний не подтверждена")
                reports.append(rep)
                continue

            # level == "ad": объявления с ad-level SitelinkSetId.
            ad_items = content.get("_ads_by_set", {}).get(str(set_id), [])
            v5_items = [it for it in ad_items if (it or {}).get("subtype") in ("TextAd", "DynamicTextAd")]
            resp_items = [it for it in ad_items if (it or {}).get("subtype") not in ("TextAd", "DynamicTextAd")]
            if resp_items:
                rep["responsive_skipped"] = len(resp_items)
            if not v5_items:
                rep["status"] = "skipped"
                rep["reason"] = (
                    f"ad-level ResponsiveAd ({len(resp_items)}) — перестановка порядка не "
                    "поддерживается (хрупкость Grid)"
                    if resp_items else "нет объявлений, ссылающихся на этот набор")
                reports.append(rep)
                continue
            if grid is None:
                grid = (grid_client_factory or _grid_client)(login)
            new_set_id = grid.add_sitelink_set(new_items)
            if not new_set_id:
                rep["status"] = "error"
                rep["reason"] = "Grid не вернул id нового набора"
                errors.append(f"набор {set_id}: Grid не вернул id нового набора")
                reports.append(rep)
                continue
            ok_ads, ad_errs = _v5_rebind_ads_sitelink_set(v5_call, token, login, v5_items, int(new_set_id))
            ads_touched += ok_ads
            errors.extend(ad_errs)
            if ok_ads:
                rep["status"] = "applied"
                rep["new_set_id"] = int(new_set_id)
                rep["ads"] = ok_ads
                if resp_items:
                    rep["reason"] = f"ResponsiveAd ({len(resp_items)}) пропущены — не поддерживается"
            else:
                rep["status"] = "error"
                rep["reason"] = ("; ".join(ad_errs)[:200]
                                 or "перепривязка объявлений не подтверждена")
            reports.append(rep)
        except Exception as e:  # noqa: BLE001
            rep["status"] = "error"
            rep["reason"] = str(e)[:180]
            errors.append(f"набор {set_id}: {str(e)[:180]}")
            reports.append(rep)

    applied = sum(1 for r in reports if r.get("status") == "applied")
    skipped = sum(1 for r in reports if r.get("status") == "skipped")
    if not applied and not errors and not skipped:
        errors.append("в аккаунте нет наборов быстрых ссылок для перестановки")
    # Основная метрика перестановки — КОЛИЧЕСТВО НАБОРОВ (applied_sets). Раньше `replaced`
    # смешивал единицы (кампании + UAC-наборы + объявления) в одно конфузное число
    # (finding #3). Теперь replaced == applied_sets (наборы), а «во что это раскрылось»
    # отдаём отдельными полями. `replaced` держим = applied_sets: воркер по нему решает
    # done/error и пишет в колонку `done`, и это теперь честная единица (наборы).
    return {"replaced": applied, "errors": errors, "reports": reports,
            "applied_sets": applied, "skipped_sets": skipped,
            "campaigns_touched": campaigns_touched,
            "ads_touched": ads_touched, "uac_sets": uac_sets}


def _replace_callout_grid(
    token: str,
    login: str,
    old_text: str,
    new_text: str,
    content: dict,
    v5_call: Callable,
    *,
    grid_client_factory: Callable | None = None,
) -> dict:
    """Create a new callout and swap it into campaigns that used the old one."""
    old = _frag_trim(old_text)
    targets = _match_targets(content, "callout", old)
    if not targets:
        return {"replaced": 0, "errors": ["уточнение с таким текстом не найдено"]}
    old_ids = []
    for target in targets:
        try:
            eid = int(target.get("id"))
        except (TypeError, ValueError):
            continue
        if eid > 0 and eid not in old_ids:
            old_ids.append(eid)
    campaign_callouts = content.get("_campaign_callout_ids") or {}
    affected: dict[int, list[int]] = {}
    for raw_cid, raw_ids in campaign_callouts.items():
        try:
            cid = int(raw_cid)
        except (TypeError, ValueError):
            continue
        ids = []
        for raw in raw_ids or []:
            try:
                co = int(raw)
            except (TypeError, ValueError):
                continue
            if co > 0 and co not in ids:
                ids.append(co)
        if any(old_id in ids for old_id in old_ids):
            affected[cid] = ids
    if not affected:
        return {"replaced": 0, "errors": ["не найдены кампании, где привязано это уточнение"]}
    normalized_new = _normalize_callout_text(new_text)
    if not normalized_new:
        return {"replaced": 0, "errors": ["после удаления недопустимых символов текст уточнения пустой"]}
    grid = (grid_client_factory or _grid_client)(login)
    created = grid.add_callouts([normalized_new])
    new_id = created.get(normalized_new)
    if not new_id:
        return {"replaced": 0, "errors": [
            "не удалось создать новое уточнение через cookie/Grid; OAuth API fallback запрещён"
        ]}
    replaced = 0
    errors: list[str] = []
    for cid, current_ids in affected.items():
        next_ids: list[int] = []
        for co in current_ids:
            if co in old_ids:
                continue
            if co not in next_ids:
                next_ids.append(co)
        if int(new_id) not in next_ids:
            next_ids.append(int(new_id))
        try:
            updated = grid.set_campaign_callouts([cid], next_ids)
            if updated:
                replaced += 1
            else:
                errors.append(f"кампания {cid}: Grid не подтвердил обновление")
        except Exception as e:  # noqa: BLE001
            errors.append(f"кампания {cid}: {str(e)[:160]}")
    # Grid иногда отдаёт неполный inheritableCallouts.assetValue (часть кампаний без данных),
    # из-за чего первый проход видит не все привязки. Перечитываем карту и добиваем хвост.
    known_cids = [int(c) for c in campaign_callouts.keys() if str(c).isdigit()]
    for _pass in range(2):
        try:
            fresh = _grid_campaign_callout_ids(login, known_cids, grid_client_factory=grid_client_factory)
        except Exception:  # noqa: BLE001 - добивание best-effort, первый проход уже отработал
            break
        leftovers = {
            cid: ids for cid, ids in fresh.items()
            if any(old_id in (ids or []) for old_id in old_ids)
        }
        if not leftovers:
            break
        for cid, current_ids in leftovers.items():
            next_ids = [co for co in current_ids if co not in old_ids]
            if int(new_id) not in next_ids:
                next_ids.append(int(new_id))
            try:
                if grid.set_campaign_callouts([cid], next_ids):
                    replaced += 1
                else:
                    errors.append(f"кампания {cid}: Grid не подтвердил обновление (добивание)")
            except Exception as e:  # noqa: BLE001
                errors.append(f"кампания {cid}: {str(e)[:160]}")
    return {"replaced": replaced, "errors": errors, "new_callout_id": int(new_id), "new_text": normalized_new}


def _norm_link_path(path: str) -> str:
    """Нормализует путь-суффикс для сравнения/записи: ведущий '/', без хвостовых пробелов."""
    p = str(path or "").strip()
    if p and not p.startswith("/"):
        p = "/" + p
    return p


def _href_with_new_path(href: str, new_path: str) -> str:
    """Тот же scheme+host+fragment исходного Href, но path/query = new_path.
    Host НЕ меняем (в отличие от copy_engine._copy_target_href — там смена домена).
    Через urlsplit/urlunsplit, НЕ слепой str.replace."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(href if "://" in str(href or "") else "https://" + str(href or ""))
    np = _norm_link_path(new_path)
    query = ""
    if "?" in np:
        np, query = np.split("?", 1)
    return urlunsplit((parts.scheme or "https", parts.netloc, np, query, parts.fragment))


def _v5_update_results_errors(resp: dict) -> tuple[int, list[str]]:
    """(успешно_обновлено, [ошибки]) из ответа v5/v501 ads.update."""
    if not isinstance(resp, dict):
        return 0, ["пустой ответ ads.update"]
    top = resp.get("error")
    if isinstance(top, dict):
        msg = top.get("error_string") or top.get("error_detail") or str(top)
        return 0, [f"ads.update: {str(msg)[:200]}"]
    results = (resp.get("result") or {}).get("UpdateResults") or []
    ok_n = 0
    errs: list[str] = []
    for r in results:
        r_errs = r.get("Errors") or []
        if r_errs:
            aid = r.get("Id")
            for e in r_errs:
                errs.append(f"ad {aid}: {e.get('Code')} {e.get('Message') or ''} {e.get('Details') or ''}".strip())
        elif r.get("Id"):
            ok_n += 1
    return ok_n, errs


def _replace_ad_href(token: str, login: str, old_path: str, new_path: str,
                     content: dict, v5_call: Callable, v501_svc: Callable) -> dict:
    """Массовая смена посадочной ссылки (Href) во всех объявлениях, где путь == old_path.
    Host сохраняется, меняется только суффикс. TextAd → v5 ads.update;
    ResponsiveAd → v501 ads.update (v5 отвергает весь тип, Code 3500). Dynamic/фид/UAC —
    у них Href нет, в content['links'] они отсутствуют → естественно пропускаются.
    Идемпотентно: объявления, у которых путь уже == new_path, не совпадут с old_path.
    """
    old_p = _norm_link_path(old_path)
    new_p = _norm_link_path(new_path)
    if not new_p:
        return {"replaced": 0, "errors": ["новый путь пуст"]}
    if new_p == old_p:
        return {"replaced": 0, "errors": ["новый путь совпадает со старым"]}
    # Собираем объявления с совпадающим путём, группируя по подтипу и новому Href.
    by_subtype: dict[str, list[dict]] = {"TextAd": [], "ResponsiveAd": []}
    skipped: list[str] = []
    seen_ids: set[int] = set()
    for rec in content.get("links") or []:
        if _norm_link_path(rec.get("path")) != old_p:
            continue
        try:
            aid = int(rec.get("ad_id") or 0)
        except (TypeError, ValueError):
            continue
        if aid <= 0 or aid in seen_ids:
            continue
        subtype = rec.get("type") or ""
        if subtype not in ("TextAd", "ResponsiveAd"):
            skipped.append(f"ad {aid}: тип {subtype or '—'} — Href не редактируется")
            continue
        seen_ids.add(aid)
        by_subtype[subtype].append({"id": aid, "href": _href_with_new_path(rec.get("href"), new_p)})
    if not seen_ids:
        return {"replaced": 0, "errors": ["объявлений с таким путём не найдено"], "skipped": skipped}

    replaced = 0
    # skipped (нередактируемые типы) — НЕ ошибки: возвращаем отдельным полем,
    # иначе воркер берёт errors[0] как провал задания даже при успешной замене.
    errors: list[str] = []

    def _flush(subtype: str, api_field: str, caller):
        nonlocal replaced
        items = by_subtype[subtype]
        for i in range(0, len(items), 100):
            chunk = items[i:i + 100]
            params = {"Ads": [{"Id": it["id"], api_field: {"Href": it["href"]}} for it in chunk]}
            resp = caller("ads", "update", token, login, params)
            ok_n, errs = _v5_update_results_errors(resp)
            replaced += ok_n
            errors.extend(errs)

    if by_subtype["TextAd"]:
        _flush("TextAd", "TextAd", v5_call)          # TextAd.Href — v5 update ок
    if by_subtype["ResponsiveAd"]:
        _flush("ResponsiveAd", "ResponsiveAd", v501_svc)  # ResponsiveAd.Href — только v501

    # Read-back: перечитываем Href по обновлённым ad_id (v5 GET работает для обоих типов).
    all_ids = sorted(seen_ids)
    confirmed = 0
    unconfirmed: list[int] = []
    try:
        rb, rb_err = _v5_paginate(
            v5_call, "ads", token, login,
            {"SelectionCriteria": {"Ids": all_ids},
             "FieldNames": ["Id", "Type"],
             "TextAdFieldNames": ["Href"],
             "ResponsiveAdFieldNames": ["Href"]},
            "Ads",
        )
        if rb_err:
            errors.append(f"read-back: {rb_err}")
        else:
            for a in rb:
                h = _ad_href(a)
                if _norm_link_path(_href_host_path(h)[1]) == new_p:
                    confirmed += 1
                else:
                    unconfirmed.append(int(a.get("Id") or 0))
    except Exception as e:  # noqa: BLE001
        errors.append(f"read-back упал: {str(e)[:160]}")
    if unconfirmed:
        errors.append(f"read-back не подтвердил новый путь у {len(unconfirmed)} объявл.: "
                      f"{unconfirmed[:10]}")
    return {"replaced": replaced, "confirmed": confirmed, "targets": len(all_ids),
            "errors": errors, "skipped": skipped}


def _do_replace(token: str, login: str, typ: str, old_text: str, new_text: str,
                content: dict, v5_call: Callable, v501_svc: Callable,
                *, mode: str = "exact", grid_client_factory: Callable | None = None,
                uac_client_factory: Callable | None = None) -> dict:
    """Применяет замену. Возвращает {'replaced': N, 'errors': [...]}."""
    old = _frag_trim(old_text)
    new = _frag_trim(new_text)
    if typ == "ad_href":
        return _replace_ad_href(token, login, old, new, content, v5_call, v501_svc)
    if typ == "sitelink_reorder":
        # Перестановка порядка: целевой порядок позиций лежит JSON-массивом в new_text
        # (список исходных индексов). Применяется к КАЖДОМУ набору по позициям.
        try:
            perm = json.loads(new_text)
        except (TypeError, ValueError):
            return {"replaced": 0, "errors": ["не удалось разобрать перестановку позиций"], "reports": []}
        return _reorder_sitelinks(
            token, login, perm, content, v5_call,
            grid_client_factory=grid_client_factory,
            uac_client_factory=uac_client_factory,
        )
    if mode == "substring":
        if typ not in _AD_FIELD:
            return {"replaced": 0, "errors": ["массовая замена фрагмента доступна только для заголовков и текстов"]}
        if len(new) > len(old):
            return {"replaced": 0, "errors": [
                f"новый фрагмент ({len(new)}) длиннее старого ({len(old)}) — заголовки вырастут, замена отклонена"]}
    if typ in _AD_FIELD:
        targets = _match_targets(content, typ, old, mode=mode, new_text=new_text)
        if not targets:
            return {"replaced": 0, "errors": ["объявление с таким текстом не найдено"]}
        non_uac = [t for t in targets if t.get("source") != "uac"]
        uac_targets = [t for t in targets if t.get("source") == "uac"]
        replaced = 0
        errors: list[str] = []
        result: dict = {}
        if non_uac:
            out = _replace_adaptive_ad_texts(
                login,
                typ,
                old,
                new_text,
                non_uac,
                grid_client_factory=grid_client_factory,
            )
            replaced += int(out.get("replaced") or 0)
            errors.extend(out.get("errors") or [])
            result["grid"] = out
        if uac_targets:
            out = _replace_uac_texts(
                login,
                typ,
                old,
                new_text,
                uac_targets,
                mode=mode,
                uac_client_factory=uac_client_factory,
            )
            replaced += int(out.get("replaced") or 0)
            errors.extend(out.get("errors") or [])
            result["uac"] = out
        return {"replaced": replaced, "errors": errors, **result}

    if typ == "callout":
        return _replace_callout_grid(
            token,
            login,
            old,
            new_text,
            content,
            v5_call,
            grid_client_factory=grid_client_factory,
        )

    if typ in _SITELINK_TYPES:
        targets = _match_targets(content, typ, old)
        if not targets:
            return {"replaced": 0, "errors": ["набор с таким текстом быстрой ссылки не найден"]}
        uac_targets = [t for t in targets if t.get("source") == "uac"]
        grid_targets = [t for t in targets if t.get("source") != "uac"]
        replaced = 0
        errors: list[str] = []
        result: dict = {}
        if grid_targets:
            out = _replace_sitelink_text_grid(
                login,
                typ,
                old,
                new_text,
                grid_targets,
                token=token,
                v5_call=v5_call,
                grid_client_factory=grid_client_factory,
            )
            replaced += int(out.get("replaced") or 0)
            errors.extend(out.get("errors") or [])
            result["grid"] = out
        if uac_targets:
            out = _replace_uac_sitelinks(
                login,
                _SITELINK_FIELD.get(typ, "title"),
                old,
                new_text,
                uac_targets,
                uac_client_factory=uac_client_factory,
            )
            replaced += int(out.get("replaced") or 0)
            errors.extend(out.get("errors") or [])
            result["uac"] = out
        return {"replaced": replaced, "errors": errors, **result}

    return {"replaced": 0, "errors": [f"неизвестный тип: {typ}"]}


def _campaign_callout_ids(v5_call: Callable, token: str, login: str, cid: int):
    """Актуальные CalloutIds кампании.

    Kept as a compatibility hook for the later Grid implementation. Do not call
    ``campaigns.get`` with ``TextCampaignFieldNames=["CalloutIds"]`` here:
    Direct API rejects that enum and breaks the whole editor load/replace flow.
    """
    return None


def _ads_using_set(content: dict, set_id: int) -> list[int]:
    """ad_id всех объявлений, ссылающихся на набор (из свежего /load-снимка)."""
    # content хранит usages по набору, но не ad_id — переиспользуем ads-снимок.
    return [a["ad_id"] for a in content.get("_ads_by_set", {}).get(str(set_id), [])]


# ───────────────────────────── registration ─────────────────────────────────

def register_content_editor_routes(
    bp,
    access,
    *,
    victory_conn: Callable,
    token_for_login: Callable,
    direct_tokens: Callable,
    v5_call: Callable,
    v501_svc: Callable,
    default_status: str = "Контекст активно",
    exclude_directologs: list[str] | None = None,
    balance_response: Callable | None = None,
    check_blocks_response: Callable | None = None,
    victory_conn_rw: Callable | None = None,
) -> None:
    """Регистрирует изолированную страницу редактора контента и её API."""
    exclude_directologs = exclude_directologs or []

    def _resolve_access_cfg_path() -> Path:
        """Файл доступа редактора контента = plaintext-пароли → живёт в .secret/ (вне git и
        вне Mutagen; на LXC101 своя копия). Ищем .secret/ вверх по дереву (как loader.py).
        Fallback — старое место рядом с модулем (совместимость на время переноса)."""
        here = Path(__file__).resolve()
        for parent in here.parents:
            cand = parent / ".secret" / "content_editor_access.json"
            if cand.exists():
                return cand
        return here.parent / "content_editor_access.json"

    access_cfg_path = _resolve_access_cfg_path()

    def _access_cfg() -> dict:
        if not access_cfg_path.exists():
            return {"users": []}
        try:
            data = json.loads(access_cfg_path.read_text(encoding="utf-8"))
        except Exception:
            return {"users": []}
        if not isinstance(data, dict):
            return {"users": []}
        users = []
        for row in data.get("users") or []:
            if not isinstance(row, dict):
                continue
            username = str(row.get("username") or "").strip()
            if not username:
                continue
            status = "blocked" if row.get("status") == "blocked" else "active"
            users.append({
                "username": username,
                "fio": str(row.get("fio") or "").strip(),
                "password": str(row.get("password") or ""),
                "status": status,
                "directologists": [
                    str(x).strip()
                    for x in (row.get("directologists") or [])
                    if str(x).strip()
                ],
            })
        data["users"] = users
        return data

    def _save_access_cfg(data: dict) -> None:
        access_cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _content_full_access() -> bool:
        return bool(session.get("is_admin") or session.get("content_admin"))

    def _content_user_record() -> dict | None:
        username = (session.get("username") or "").strip()
        if not username:
            return None
        for row in _access_cfg().get("users") or []:
            if (row.get("username") or "").strip() == username:
                return row
        return None

    def _allowed_directologists() -> list[str] | None:
        if _content_full_access():
            return None
        row = _content_user_record()
        if not row:
            return []
        if row.get("status") == "blocked":
            return []
        return [str(x).strip() for x in (row.get("directologists") or []) if str(x).strip()]

    def _login_allowed_for(login: str, allowed: list[str] | None) -> tuple[bool, str]:
        return _scope_check(victory_conn, login, allowed)

    def _login_allowed(login: str) -> tuple[bool, str]:
        return _login_allowed_for(login, _allowed_directologists())

    def _admin_allowed() -> bool:
        return bool(session.get("is_admin"))

    def _token(login: str):
        tokens = direct_tokens()
        if not tokens:
            return None, None, "нет агентских токенов (loader.load_yandex_direct вернул пусто)"
        token, agency = token_for_login(login, "", tokens)
        if not token:
            return None, None, (f"ни один агентский токен не открывает аккаунт {login} — "
                                f"проверьте доступ агентства и актуальность OAuth-токенов")
        return token, agency, None

    def _load_with_index(token: str, login: str, *,
                         include_campaign_sitelinks: bool = True) -> dict:
        # _load_account уже строит индекс _ads_by_set (без второго запроса ads.get).
        # include_campaign_sitelinks=False пропускает Grid-round-trip блока 3c —
        # для /links и preview/replace НЕ-sitelink типов campaign-level наборы не нужны.
        return _load_account(token, login, v5_call,
                             include_campaign_sitelinks=include_campaign_sitelinks)

    # Исполнение заданий вынесено в direct-content-worker.service (make_job_executor).
    try:
        ensure_jobs_table()
    except Exception as e:  # noqa: BLE001
        print(f"[content-editor] ensure_jobs_table failed: {e}", flush=True)

    # ── Страница ──────────────────────────────────────────────────────────────
    @bp.route("/automation/content")
    @access
    def content_editor_page():
        return render_template("direct/content_editor.html")

    @bp.route("/automation/content/admin")
    @access
    def content_editor_admin_page():
        if not _admin_allowed():
            return jsonify({"error": "Forbidden"}), 403
        return render_template("direct/content_admin.html")

    @bp.route("/api/content-editor/me")
    @access
    def ce_me():
        username = (session.get("username") or "").strip()
        # is_admin — РЕАЛЬНЫЙ админ (не content_admin): для фич уровня «только is_admin»
        # (сверка цен). full_access = is_admin OR content_admin — для остальной админки.
        is_admin = bool(session.get("is_admin"))
        if _content_full_access():
            return jsonify({
                "username": username or "admin",
                "fio": "Администратор",
                "full_access": True,
                "is_admin": is_admin,
                "directologists": None,
            })
        row = _content_user_record() or {}
        return jsonify({
            "username": username,
            "fio": str(row.get("fio") or "").strip(),
            "full_access": False,
            "is_admin": is_admin,
            "directologists": [
                str(x).strip()
                for x in (row.get("directologists") or [])
                if str(x).strip()
            ],
        })

    def _pairs_allowed() -> tuple[bool, str]:
        """Пары {login, agency} из запроса — только свои аккаунты (по директологам)."""
        allowed = _allowed_directologists()
        if allowed is None:
            return True, ""
        if not allowed:
            return False, "Доступы не выданы"
        logins = [str((p or {}).get("login") or "").strip()
                  for p in (request.json or {}).get("pairs") or []]
        logins = sorted({x for x in logins if x})
        if not logins:
            return True, ""
        conn = victory_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT count(DISTINCT login_key), "
                "count(DISTINCT login_key) FILTER "
                "(WHERE directologist IS NULL OR directologist <> ALL(%s)) "
                "FROM public.local_gsheet_sites "
                "WHERE direction='Авто' AND login_key = ANY(%s)",
                (allowed, logins),
            )
            found, cnt_foreign = cur.fetchone()
        finally:
            conn.close()
        if cnt_foreign:
            return False, "в запросе чужие аккаунты"
        if found < len(logins):
            # неизвестный логин ≠ свой: иначе можно читать балансы любых аккаунтов агентств
            return False, "в запросе неизвестные аккаунты"
        return True, ""

    if balance_response is not None:
        @bp.route("/api/content-editor/balance", methods=["POST"])
        @access
        def ce_balance():
            ok, err = _pairs_allowed()
            if not ok:
                return jsonify({"error": err}), 403
            return balance_response()

    if check_blocks_response is not None:
        @bp.route("/api/content-editor/check_blocks", methods=["POST"])
        @access
        def ce_check_blocks():
            ok, err = _pairs_allowed()
            if not ok:
                return jsonify({"error": err}), 403
            return check_blocks_response()

    @bp.route("/api/content-editor/admin/directologists")
    @access
    def ce_admin_directologists():
        if not _admin_allowed():
            return jsonify({"error": "Forbidden"}), 403
        import psycopg2.extras
        where = ["direction='Авто'", "directologist IS NOT NULL", "btrim(directologist)<>''"]
        params: list = [default_status]
        if exclude_directologs:
            where.append("directologist <> ALL(%s)")
            params.append(exclude_directologs)
        conn = victory_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT directologist, count(*) AS accounts, "
                "count(*) FILTER (WHERE status=%s) AS active_accounts "
                "FROM public.local_gsheet_sites "
                f"WHERE {' AND '.join(where)} "
                "GROUP BY directologist ORDER BY directologist",
                params,
            )
            return jsonify({"rows": cur.fetchall()})
        finally:
            conn.close()

    @bp.route("/api/content-editor/admin/access", methods=["GET"])
    @access
    def ce_admin_access_get():
        if not _admin_allowed():
            return jsonify({"error": "Forbidden"}), 403
        return jsonify(_access_cfg())

    @bp.route("/api/content-editor/admin/access", methods=["POST"])
    @access
    def ce_admin_access_save():
        if not _admin_allowed():
            return jsonify({"error": "Forbidden"}), 403
        data = request.json or {}
        users = []
        for row in data.get("users") or []:
            username = str(row.get("username") or "").strip()
            if not username:
                continue
            password = str(row.get("password") or "")
            status = "blocked" if row.get("status") == "blocked" else "active"
            directologists = []
            for d in row.get("directologists") or []:
                d = str(d or "").strip()
                if d and d not in directologists:
                    directologists.append(d)
            users.append({
                "username": username,
                "fio": str(row.get("fio") or "").strip(),
                "password": password,
                "status": status,
                "directologists": directologists,
            })
        _save_access_cfg({"users": users})
        return jsonify({"ok": True, "users": users})

    # ── Сверка цен (admin-only): Direct ↔ фиды → расхождения → заливка ─────────
    if victory_conn_rw is not None:
        from . import price_check as pc

        try:
            pc.ensure_price_check_tables(victory_conn_rw)
            # На старте сервиса: джобы, застрявшие в 'running' (рестарт в середине) → 'interrupted'.
            pc.reconcile_stuck_jobs(victory_conn_rw)
        except Exception as e:  # noqa: BLE001
            print(f"[price-check] ensure_price_check_tables failed: {e}", flush=True)

        _pc_deps = {
            "victory_conn": victory_conn,
            "victory_conn_rw": victory_conn_rw,
            "token_for_login": token_for_login,
            "direct_tokens": direct_tokens,
            "v5_call": v5_call,
        }

        @bp.route("/api/content-editor/admin/pricecheck/run", methods=["POST"])
        @access
        def ce_pc_run():
            if not _admin_allowed():
                return jsonify({"error": "Forbidden"}), 403
            body = request.json or {}
            logins = [str(x).strip() for x in (body.get("logins") or []) if str(x).strip()]
            directologist = (body.get("directologist") or "").strip() or None
            status = (body.get("status") or default_status).strip()
            all_active = bool(body.get("all_active"))
            if all_active:
                items = pc.active_logins(victory_conn, status=status, exclude=exclude_directologs)
            else:
                if not logins and not directologist:
                    return jsonify({"error": "укажите логины или директолога"}), 400
                items = pc.logins_for(victory_conn, logins=logins or None,
                                      directologist=directologist, status=status,
                                      exclude=exclude_directologs)
            if not items:
                return jsonify({"error": "по фильтру не найдено ни одного аккаунта"}), 404
            job_id = pc.new_job_id()
            try:
                pc._job_insert(victory_conn_rw, job_id, "check",
                               (session.get("username") or "").strip(),
                               [it["login"] for it in items], {"directologist": directologist},
                               len(items))
            except Exception as e:  # noqa: BLE001
                return jsonify({"error": f"не удалось создать задание: {e}"}), 500
            pc.launch_background(pc.run_check_job, _pc_deps, job_id, items)
            return jsonify({"ok": True, "job_id": job_id, "total": len(items)})

        @bp.route("/api/content-editor/admin/pricecheck/status")
        @access
        def ce_pc_status():
            if not _admin_allowed():
                return jsonify({"error": "Forbidden"}), 403
            pc.reconcile_stuck_jobs(victory_conn_rw)   # зависшие running → interrupted
            job_id = (request.args.get("job_id") or "").strip()
            row = pc.job_public(victory_conn, job_id)
            if not row:
                return jsonify({"error": "job not found"}), 404
            return jsonify(row)

        @bp.route("/api/content-editor/admin/pricecheck/jobs")
        @access
        def ce_pc_jobs():
            if not _admin_allowed():
                return jsonify({"error": "Forbidden"}), 403
            return jsonify({"jobs": pc.jobs_recent(victory_conn, 30)})

        @bp.route("/api/content-editor/admin/pricecheck/results")
        @access
        def ce_pc_results():
            if not _admin_allowed():
                return jsonify({"error": "Forbidden"}), 403
            directologist = (request.args.get("directologist") or "").strip() or None
            login = (request.args.get("login") or "").strip() or None
            only_mismatch = (request.args.get("only_mismatch") or "1").strip() not in ("0", "false", "")
            rows = pc.diff_rows(victory_conn, directologist=directologist, login=login,
                                only_mismatch=only_mismatch)
            try:
                last_run = pc.last_check_run(victory_conn)
            except Exception:  # noqa: BLE001
                last_run = None
            return jsonify({"rows": rows, "count": len(rows), "last_run": last_run})

        @bp.route("/api/content-editor/admin/pricecheck/apply", methods=["POST"])
        @access
        def ce_pc_apply():
            if not _admin_allowed():
                return jsonify({"error": "Forbidden"}), 403
            body = request.json or {}
            raw = body.get("items") or []
            items = []
            for r in raw:
                login = str((r or {}).get("login") or "").strip()
                url = str((r or {}).get("url") or "").strip()
                if not login or not url:
                    continue

                def _num(v):
                    try:
                        return float(v) if v not in (None, "") else None
                    except (TypeError, ValueError):
                        return None

                items.append({
                    "login": login, "url": url, "agency": (r or {}).get("agency") or "",
                    "price_direct": _num((r or {}).get("price_direct")),
                    "oldprice_direct": _num((r or {}).get("oldprice_direct")),
                    "price_feed": _num((r or {}).get("price_feed")),
                    "oldprice_feed": _num((r or {}).get("oldprice_feed")),
                })
            if not items:
                return jsonify({"error": "не выбрано ни одной строки для заливки"}), 400
            # Ставим в ОЧЕРЕДЬ (status='queued'); реальный ads.update — крон 20:00 Екб.
            try:
                job_id = pc.enqueue_apply(victory_conn_rw,
                                          (session.get("username") or "").strip(), items)
            except Exception as e:  # noqa: BLE001
                return jsonify({"error": f"не удалось поставить в очередь: {e}"}), 500
            return jsonify({"ok": True, "job_id": job_id, "total": len(items), "queued": True})

        @bp.route("/api/content-editor/admin/pricecheck/queue")
        @access
        def ce_pc_queue():
            if not _admin_allowed():
                return jsonify({"error": "Forbidden"}), 403
            pc.reconcile_stuck_jobs(victory_conn_rw)   # зависшие running → interrupted
            return jsonify({"jobs": pc.apply_queue_for_ui(victory_conn, 100)})

        # ── Управление заданием очереди: пауза / старт / удаление ─────────────
        @bp.route("/api/content-editor/admin/pricecheck/pause", methods=["POST"])
        @access
        def ce_pc_pause():
            if not _admin_allowed():
                return jsonify({"error": "Forbidden"}), 403
            job_id = ((request.json or {}).get("job_id") or "").strip()
            if not job_id:
                return jsonify({"error": "job_id обязателен"}), 400
            ok = pc.request_pause(victory_conn_rw, job_id)
            if not ok:
                return jsonify({"error": "задание не активно (уже завершено/на паузе)"}), 409
            return jsonify({"ok": True})

        @bp.route("/api/content-editor/admin/pricecheck/resume", methods=["POST"])
        @access
        def ce_pc_resume():
            if not _admin_allowed():
                return jsonify({"error": "Forbidden"}), 403
            job_id = ((request.json or {}).get("job_id") or "").strip()
            if not job_id:
                return jsonify({"error": "job_id обязателен"}), 400
            ok = pc.request_resume(victory_conn_rw, job_id)
            if not ok:
                return jsonify({"error": "задание не на паузе"}), 409
            return jsonify({"ok": True})

        @bp.route("/api/content-editor/admin/pricecheck/delete", methods=["POST"])
        @access
        def ce_pc_delete():
            if not _admin_allowed():
                return jsonify({"error": "Forbidden"}), 403
            job_id = ((request.json or {}).get("job_id") or "").strip()
            if not job_id:
                return jsonify({"error": "job_id обязателен"}), 400
            ok = pc.request_delete(victory_conn_rw, job_id)
            if not ok:
                # нельзя удалить активное — сначала пауза
                return jsonify({"error": "удалить можно только задание на паузе"}), 409
            return jsonify({"ok": True, "removed": True})

    # ── Аккаунты для умного поиска ─────────────────────────────────────────────
    @bp.route("/api/content-editor/accounts")
    @access
    def ce_accounts():
        import psycopg2.extras

        from .account_filters import base_account_where
        status = (request.args.get("status") or default_status).strip()
        q = (request.args.get("q") or "").strip()
        where = list(base_account_where())
        params: list = []
        if exclude_directologs:
            where.append("(directologist IS NULL OR directologist <> ALL(%s))")
            params.append(exclude_directologs)
        if status and status != "__all__":
            where.append("status=%s")
            params.append(status)
        if q:
            where.append("(domain ILIKE %s OR login_key ILIKE %s OR city ILIKE %s "
                         "OR site_type ILIKE %s OR salon ILIKE %s OR directologist ILIKE %s)")
            params += [f"%{q}%"] * 6
        allowed = _allowed_directologists()
        if allowed is not None:
            if not allowed:
                return jsonify({"rows": []})
            where.append("directologist = ANY(%s)")
            params.append(allowed)
        sql = (
            "SELECT domain, salon, city, site_type, login_key, counter_number, "
            "client_id, agency_account, status, directologist "
            "FROM public.local_gsheet_sites "
            f"WHERE {' AND '.join(where)} ORDER BY domain LIMIT 2000"
        )
        conn = victory_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql, params)
            rows = cur.fetchall()
        finally:
            conn.close()
        return jsonify({"rows": rows})

    # ── Загрузка всего контента аккаунта ───────────────────────────────────────
    @bp.route("/api/content-editor/load", methods=["POST"])
    @access
    def ce_load():
        login = ((request.json or {}).get("login") or "").strip()
        if not login:
            return jsonify({"error": "login обязателен"}), 400
        ok, scope_err = _login_allowed(login)
        if not ok:
            return jsonify({"error": scope_err}), 403
        token, agency, err = _token(login)
        if err:
            return jsonify({"error": err}), 404
        # Вкладка «Быстрые ссылки» на главном /load ДОЛЖНА видеть campaign-level наборы.
        content = _load_with_index(token, login, include_campaign_sitelinks=True)
        if content.get("error"):
            return jsonify({"error": content["error"]}), 502
        resp = {
            "login": login, "agency": agency,
            "callouts": content["callouts"],
            "sitelinks": content["sitelinks"],
            "ads": content["ads"],
        }
        # Если блок 3c ВЫПОЛНЯЛСЯ и упал — не молчим: фронт покажет заметное
        # предупреждение (замена набора уровня кампании может не примениться).
        if content.get("_grid_sitelink_error"):
            resp["_grid_sitelink_error"] = content["_grid_sitelink_error"]
        return jsonify(resp)

    # ── Смена ссылки (Фаза 1): чтение ссылок объявлений, дедуп по пути ──────────
    # Только ЧТЕНИЕ. Запись (замена Href) — Фаза 3, здесь не подключена.
    @bp.route("/api/content-editor/links", methods=["POST"])
    @access
    def ce_links():
        body = request.json or {}
        logins = body.get("logins")
        if isinstance(logins, str):
            logins = [logins]
        if not logins:
            one = (body.get("login") or "").strip()
            logins = [one] if one else []
        logins = [str(x).strip() for x in (logins or []) if str(x).strip()]
        if not logins:
            return jsonify({"error": "login/logins обязателен"}), 400
        groups: dict[str, dict] = {}
        errors: list[dict] = []
        for login in logins:
            ok, scope_err = _login_allowed(login)
            if not ok:
                errors.append({"login": login, "error": scope_err})
                continue
            token, _agency, err = _token(login)
            if err:
                errors.append({"login": login, "error": err})
                continue
            # /links не нужны campaign-level наборы → не гоняем Grid-round-trip (блок 3c).
            content = _load_with_index(token, login, include_campaign_sitelinks=False)
            if content.get("error"):
                errors.append({"login": login, "error": content["error"]})
                continue
            for lk in content.get("links") or []:
                path = lk.get("path") or ""
                if not path:
                    continue
                g = groups.setdefault(path, {
                    "path": path,
                    # Нормализация показа: реальный host заменяем на site.ru, путь сохраняем.
                    "template_url": "https://site.ru" + path,
                    "_ads": set(), "_camps": set(), "_accounts": set(),
                    "live_count": 0, "_detail": {},
                })
                g["_ads"].add((login, lk.get("ad_id")))
                g["_camps"].add((login, lk.get("campaign_id")))
                g["_accounts"].add(login)
                is_live = str(lk.get("state") or "").upper() == "ON"
                if is_live:
                    g["live_count"] += 1
                host = lk.get("host") or ""
                # Реальная схема исходного Href (обычно https, но не хардкодим) —
                # чтобы превью «было → стало» и запись совпали по scheme.
                scheme = _href_scheme(lk.get("href"))
                det = g["_detail"].setdefault(
                    (login, host),
                    {"login": login, "host": host, "scheme": scheme, "ads": 0, "live": 0},
                )
                det["ads"] += 1
                if is_live:
                    det["live"] += 1
        out_groups: list[dict] = []
        for _path, g in groups.items():
            out_groups.append({
                "path": g["path"],
                "template_url": g["template_url"],
                "ads_count": len(g["_ads"]),
                "campaigns_count": len(g["_camps"]),
                "accounts_count": len(g["_accounts"]),
                "live_count": g["live_count"],
                # Детализация по аккаунтам — чтобы UI собрал превью было→стало
                # с РЕАЛЬНЫМ доменом каждого аккаунта (без записи).
                "accounts": list(g["_detail"].values()),
            })
        out_groups.sort(key=lambda x: (-x["ads_count"], x["path"]))
        return jsonify({"logins": logins, "groups": out_groups, "errors": errors})

    # ── Preview: сколько объектов затронет замена (без записи) ──────────────────
    @bp.route("/api/content-editor/preview", methods=["POST"])
    @access
    def ce_preview():
        body = request.json or {}
        login = (body.get("login") or "").strip()
        typ = (body.get("type") or "").strip()
        old_text = body.get("old_text") or ""
        new_text = body.get("new_text") or ""
        mode = (body.get("mode") or "exact").strip()
        if not login or not typ or not old_text.strip():
            return jsonify({"error": "login, type и old_text обязательны"}), 400
        ok, scope_err = _login_allowed(login)
        if not ok:
            return jsonify({"error": scope_err}), 403
        if typ not in _AD_FIELD and typ != "callout" and typ not in _SITELINK_TYPES:
            return jsonify({"error": f"неизвестный тип: {typ}"}), 400
        if mode == "substring" and typ not in _AD_FIELD:
            return jsonify({"error": "массовая замена фрагмента доступна только для заголовков и текстов"}), 400
        token, _, err = _token(login)
        if err:
            return jsonify({"error": err}), 404
        # campaign-level наборы нужны превью ТОЛЬКО для sitelink-типов.
        content = _load_with_index(
            token, login, include_campaign_sitelinks=(typ in _SITELINK_TYPES))
        if content.get("error"):
            return jsonify({"error": content["error"]}), 502
        if mode == "substring":
            old = _frag_trim(old_text)
            new = _frag_trim(new_text)
            guard_ok = len(new) <= len(old)
            hits = _match_targets(content, typ, old, mode="substring", new_text=new)
            # уникальные заголовки/тексты для визуальной проверки (before→after)
            seen: set[str] = set()
            items: list[dict] = []
            for h in hits:
                before = h.get("before") or ""
                if before in seen:
                    continue
                seen.add(before)
                after = h.get("after") or ""
                items.append({
                    "before": before, "after": after,
                    "len_before": len(before), "len_after": len(after),
                })
            items.sort(key=lambda it: it["before"].lower())
            return jsonify({
                "mode": "substring", "objects": len(hits), "distinct": len(items),
                "guard_ok": guard_ok, "old_len": len(old), "new_len": len(new),
                "items": items,
            })
        hits = _match_targets(content, typ, old_text)
        usages: list[dict] = []
        for h in hits:
            usages.extend(h.get("usages", []))
        return jsonify({"objects": len(hits), "usages_count": len(usages), "usages": usages})

    # ── Replace: применить замену во всех объектах ──────────────────────────────
    @bp.route("/api/content-editor/replace", methods=["POST"])
    @access
    def ce_replace():
        body = request.json or {}
        login = (body.get("login") or "").strip()
        typ = (body.get("type") or "").strip()
        old_text = body.get("old_text") or ""
        new_text = body.get("new_text") or ""
        mode = (body.get("mode") or "exact").strip()
        if not login or not typ or not old_text.strip() or not new_text.strip():
            return jsonify({"error": "login, type, old_text и new_text обязательны"}), 400
        ok, scope_err = _login_allowed(login)
        if not ok:
            return jsonify({"error": scope_err}), 403
        if typ not in _AD_FIELD and typ != "callout" and typ not in _SITELINK_TYPES:
            return jsonify({"error": f"неизвестный тип: {typ}"}), 400
        # substring доступен только для заголовков/текстов (как в preview и replace_async).
        # Раньше sync-replace этот гард пропускал (finding #4) — выравниваем.
        if mode == "substring" and typ not in _AD_FIELD:
            return jsonify({"error": "массовая замена фрагмента доступна только для заголовков и текстов"}), 400
        if mode == "substring" and len(_frag_trim(new_text)) > len(_frag_trim(old_text)):
            return jsonify({"error": "новый фрагмент длиннее старого — замена отклонена"}), 400
        token, _, err = _token(login)
        if err:
            return jsonify({"error": err}), 404
        # sitelink-замена набора уровня кампании требует блок 3c; остальным типам — нет.
        content = _load_with_index(
            token, login, include_campaign_sitelinks=(typ in _SITELINK_TYPES))
        if content.get("error"):
            return jsonify({"error": content["error"]}), 502
        out = _do_replace(token, login, typ, old_text, new_text, content, v5_call, v501_svc, mode=mode)
        return jsonify(out)

    @bp.route("/api/content-editor/replace_async", methods=["POST"])
    @access
    def ce_replace_async():
        body = request.json or {}
        login = (body.get("login") or "").strip()
        typ = (body.get("type") or "").strip()
        old_text = body.get("old_text") or ""
        new_text = body.get("new_text") or ""
        mode = (body.get("mode") or "exact").strip()
        try:
            campaign_count = int(body.get("campaign_count") or 0)
        except (TypeError, ValueError):
            campaign_count = 0
        if not login or not typ or not old_text.strip() or not new_text.strip():
            return jsonify({"error": "login, type, old_text и new_text обязательны"}), 400
        ok, scope_err = _login_allowed(login)
        if not ok:
            return jsonify({"error": scope_err}), 403
        if typ not in _AD_FIELD and typ != "callout" and typ not in _SITELINK_TYPES:
            return jsonify({"error": f"неизвестный тип: {typ}"}), 400
        if mode == "substring":
            if typ not in _AD_FIELD:
                return jsonify({"error": "массовая замена фрагмента доступна только для заголовков и текстов"}), 400
            if len(_frag_trim(new_text)) > len(_frag_trim(old_text)):
                return jsonify({"error": "новый фрагмент длиннее старого — замена отклонена"}), 400
        _, agency, err = _token(login)
        if err:
            return jsonify({"error": err}), 404
        day_cnt = _jobs_exec(
            f"SELECT count(*) AS c FROM {CE_JOBS_TABLE} "
            f"WHERE login=%s AND status<>'cancelled' AND created_at >= {CE_EKB_DAY_SQL}",
            (login,), "one")["c"]
        if day_cnt >= CE_DAILY_JOB_CAP:
            return jsonify({"error": f"лимит {CE_DAILY_JOB_CAP} заданий на аккаунт в сутки "
                                     f"(уже поставлено {day_cnt})"}), 429
        job_id = "ce_" + uuid.uuid4().hex[:12]
        allowed = _allowed_directologists()
        _jobs_exec(
            f"INSERT INTO {CE_JOBS_TABLE} "
            "(job_id, username, login, agency, type, old_text, new_text, mode, campaign_count, access_directologists) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (job_id, (session.get("username") or "").strip(), login, agency or "", typ,
             old_text, new_text, (mode if mode == "substring" else "exact"), campaign_count,
             json.dumps(allowed, ensure_ascii=False) if allowed is not None else None))
        ahead = _jobs_exec(
            f"SELECT count(*) AS c FROM {CE_JOBS_TABLE} WHERE status='queued' "
            f"AND created_at < (SELECT created_at FROM {CE_JOBS_TABLE} WHERE job_id=%s)",
            (job_id,), "one")["c"]
        return jsonify({
            "ok": True, "job_id": job_id, "login": login, "agency": agency,
            "status": "queued", "total": 1, "ahead": ahead,
            "campaign_count": campaign_count,
        })

    # ── Смена ссылки (Фаза 3): постановка задачи смены Href в очередь ───────────
    @bp.route("/api/content-editor/links/replace_async", methods=["POST"])
    @access
    def ce_links_replace_async():
        body = request.json or {}
        login = (body.get("login") or "").strip()
        old_path = _norm_link_path(body.get("old_path") or "")
        new_path = _norm_link_path(body.get("new_path") or "")
        try:
            campaign_count = int(body.get("campaign_count") or 0)
        except (TypeError, ValueError):
            campaign_count = 0
        if not login or not old_path or not new_path:
            return jsonify({"error": "login, old_path и new_path обязательны"}), 400
        if new_path == old_path:
            return jsonify({"error": "новый путь совпадает со старым"}), 400
        ok, scope_err = _login_allowed(login)
        if not ok:
            return jsonify({"error": scope_err}), 403
        _, agency, err = _token(login)
        if err:
            return jsonify({"error": err}), 404
        day_cnt = _jobs_exec(
            f"SELECT count(*) AS c FROM {CE_JOBS_TABLE} "
            f"WHERE login=%s AND status<>'cancelled' AND created_at >= {CE_EKB_DAY_SQL}",
            (login,), "one")["c"]
        if day_cnt >= CE_DAILY_JOB_CAP:
            return jsonify({"error": f"лимит {CE_DAILY_JOB_CAP} заданий на аккаунт в сутки "
                                     f"(уже поставлено {day_cnt})"}), 429
        job_id = "ce_" + uuid.uuid4().hex[:12]
        allowed = _allowed_directologists()
        _jobs_exec(
            f"INSERT INTO {CE_JOBS_TABLE} "
            "(job_id, username, login, agency, type, old_text, new_text, mode, campaign_count, access_directologists) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (job_id, (session.get("username") or "").strip(), login, agency or "", "ad_href",
             old_path, new_path, "link", campaign_count,
             json.dumps(allowed, ensure_ascii=False) if allowed is not None else None))
        ahead = _jobs_exec(
            f"SELECT count(*) AS c FROM {CE_JOBS_TABLE} WHERE status='queued' "
            f"AND created_at < (SELECT created_at FROM {CE_JOBS_TABLE} WHERE job_id=%s)",
            (job_id,), "one")["c"]
        return jsonify({
            "ok": True, "job_id": job_id, "login": login, "agency": agency,
            "type": "ad_href", "status": "queued", "total": 1, "ahead": ahead,
            "campaign_count": campaign_count,
        })

    # ── Перестановка порядка быстрых ссылок (sitelink_reorder) ──────────────────
    # Позиционная перестановка (вариант A): целевой порядок позиций (drag-and-drop)
    # применяется к КАЖДОМУ набору выбранного аккаунта по позициям. Одна задача на
    # аккаунт в ту же очередь content_jobs — как остальные замены.
    @bp.route("/api/content-editor/sitelinks/reorder_async", methods=["POST"])
    @access
    def ce_sitelinks_reorder_async():
        body = request.json or {}
        login = (body.get("login") or "").strip()
        perm_raw = body.get("perm")
        try:
            campaign_count = int(body.get("campaign_count") or 0)
        except (TypeError, ValueError):
            campaign_count = 0
        if not login:
            return jsonify({"error": "login обязателен"}), 400
        perm, why = _validate_permutation(perm_raw)
        if not perm:
            return jsonify({"error": why or "некорректная перестановка позиций"}), 400
        ok, scope_err = _login_allowed(login)
        if not ok:
            return jsonify({"error": scope_err}), 403
        _, agency, err = _token(login)
        if err:
            return jsonify({"error": err}), 404
        day_cnt = _jobs_exec(
            f"SELECT count(*) AS c FROM {CE_JOBS_TABLE} "
            f"WHERE login=%s AND status<>'cancelled' AND created_at >= {CE_EKB_DAY_SQL}",
            (login,), "one")["c"]
        if day_cnt >= CE_DAILY_JOB_CAP:
            return jsonify({"error": f"лимит {CE_DAILY_JOB_CAP} заданий на аккаунт в сутки "
                                     f"(уже поставлено {day_cnt})"}), 429
        job_id = "ce_" + uuid.uuid4().hex[:12]
        allowed = _allowed_directologists()
        perm_json = json.dumps(perm)
        _jobs_exec(
            f"INSERT INTO {CE_JOBS_TABLE} "
            "(job_id, username, login, agency, type, old_text, new_text, mode, campaign_count, access_directologists) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (job_id, (session.get("username") or "").strip(), login, agency or "", "sitelink_reorder",
             perm_json, perm_json, "reorder", campaign_count,
             json.dumps(allowed, ensure_ascii=False) if allowed is not None else None))
        ahead = _jobs_exec(
            f"SELECT count(*) AS c FROM {CE_JOBS_TABLE} WHERE status='queued' "
            f"AND created_at < (SELECT created_at FROM {CE_JOBS_TABLE} WHERE job_id=%s)",
            (job_id,), "one")["c"]
        return jsonify({
            "ok": True, "job_id": job_id, "login": login, "agency": agency,
            "type": "sitelink_reorder", "status": "queued", "total": 1, "ahead": ahead,
            "campaign_count": campaign_count,
        })

    def _queued_ahead_map() -> dict:
        rows = _jobs_exec(
            f"SELECT job_id FROM {CE_JOBS_TABLE} WHERE status='queued' ORDER BY created_at",
            (), "all") or []
        return {r["job_id"]: i for i, r in enumerate(rows)}

    @bp.route("/api/content-editor/jobs")
    @access
    def ce_jobs():
        where = "NOT dismissed AND (status IN ('queued','running') OR created_at > now() - interval '48 hours')"
        params: tuple = ()
        if not _content_full_access():
            where += " AND username=%s"
            params = ((session.get("username") or "").strip(),)
        rows = _jobs_exec(
            f"SELECT * FROM {CE_JOBS_TABLE} WHERE {where} ORDER BY created_at DESC LIMIT 200",
            params, "all") or []
        ahead = _queued_ahead_map()
        jobs = []
        for r in rows:
            j = _content_job_public(r)
            if j["status"] == "queued":
                j["ahead"] = ahead.get(j["job_id"], 0)
            jobs.append(j)
        order = {"running": 0, "queued": 1, "error": 2, "done": 3, "cancelled": 4}
        jobs.sort(key=lambda j: (order.get(j["status"], 9), -(j.get("created_at") or 0)))
        return jsonify({"jobs": jobs})

    def _job_owned(row: dict) -> bool:
        if _content_full_access():
            return True
        return (row.get("username") or "") == (session.get("username") or "").strip()

    @bp.route("/api/content-editor/status")
    @access
    def ce_job_status():
        job_id = (request.args.get("job_id") or "").strip()
        row = _jobs_exec(f"SELECT * FROM {CE_JOBS_TABLE} WHERE job_id=%s", (job_id,), "one")
        if not row or not _job_owned(row):
            return jsonify({"error": "job not found"}), 404
        out = _content_job_public(row)
        if out["status"] == "queued":
            out["ahead"] = _queued_ahead_map().get(job_id, 0)
        return jsonify(out)

    @bp.route("/api/content-editor/cancel", methods=["POST"])
    @access
    def ce_job_cancel():
        job_id = ((request.json or {}).get("job_id") or "").strip()
        row = _jobs_exec(f"SELECT status, username FROM {CE_JOBS_TABLE} WHERE job_id=%s", (job_id,), "one")
        if not row:
            return jsonify({"ok": True, "removed": True})
        if not _job_owned(row):
            return jsonify({"error": "Forbidden"}), 403
        if row["status"] in ("done", "error", "cancelled"):
            _jobs_exec(f"UPDATE {CE_JOBS_TABLE} SET dismissed=true WHERE job_id=%s", (job_id,))
            return jsonify({"ok": True, "removed": True})
        _jobs_exec(
            f"UPDATE {CE_JOBS_TABLE} SET cancel_requested=true, "
            "status = CASE WHEN status='queued' THEN 'cancelled' ELSE status END, "
            "error = CASE WHEN status='queued' THEN 'отменено' ELSE error END, "
            "finished_at = CASE WHEN status='queued' THEN now() ELSE finished_at END "
            "WHERE job_id=%s", (job_id,))
        return jsonify({"ok": True})

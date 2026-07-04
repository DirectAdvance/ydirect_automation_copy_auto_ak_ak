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

CE_JOBS_TABLE = "direct_content_jobs"
CE_DAILY_JOB_CAP = int(os.environ.get("CE_DAILY_JOB_CAP") or 15)  # заданий на аккаунт в сутки (Екб)
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
        content = _load_account(token, job["login"], v5_call)
        if content.get("error"):
            raise RuntimeError(content["error"])
        if is_cancelled():
            return {"cancelled": True}
        return _do_replace(token, job["login"], job["type"], job["old_text"], job["new_text"],
                           content, v5_call, v501_svc)

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


def _uac_replace_text_items(value, old_text: str, new_text: str) -> tuple[list, int]:
    """Replace exact UAC text items while preserving dict item shape."""
    out: list = []
    changed = 0
    old = (old_text or "").strip()
    new = (new_text or "").strip()
    for item in value if isinstance(value, list) else []:
        if _uac_text_item_text(item) == old:
            changed += 1
            if isinstance(item, dict):
                next_item = dict(item)
                replaced_key = None
                for key in ("text", "title", "value", "body", "name"):
                    if str(next_item.get(key) or "").strip() == old:
                        next_item[key] = new
                        replaced_key = key
                        break
                if replaced_key is None:
                    next_item["text"] = new
                out.append(next_item)
            else:
                out.append(new)
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
        out = {"callouts": [], "sitelinks": [], "ads": [], "_ads_by_set": {}}
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
        {"FieldNames": ["Id", "CampaignId", "AdGroupId", "Type"],
         "TextAdFieldNames": ["Title", "Title2", "Text", "SitelinkSetId"],
         "ResponsiveAdFieldNames": ["Titles", "Texts", "SitelinkSetId"],
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
    sitelink_usages: dict[str, list[dict]] = {}
    ads_by_set: dict[str, list[dict]] = {}   # set_id → [{ad_id}] для переназначения на replace
    for a in ads:
        cid = int(a.get("CampaignId") or 0)
        agid = int(a.get("AdGroupId") or 0)
        ad_id = int(a.get("Id") or 0)
        usage = _usage_for(cid, agid)
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
        except Exception as e:  # noqa: BLE001 — UAC read is enrichment; must not block load
            uac_read_error = str(e)[:200]

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
        sitelinks_out.append({
            "set_id": int(s.get("Id") or 0),
            "set_title": title,
            "items": items,
            "usages": sitelink_usages.get(sid, []),
        })

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
           "_ads_by_set": ads_by_set, "_campaign_callout_ids": campaign_callout_ids}
    if grid_callout_error:
        out["_grid_callout_error"] = grid_callout_error
    if uac_read_error:
        out["_uac_read_error"] = uac_read_error
    return out


# ───────────────────────────── replace / preview ─────────────────────────────

_AD_FIELD = {"ad_title": "title", "ad_title2": "title2", "ad_text": "text"}
_AD_API_FIELD = {"ad_title": "Title", "ad_title2": "Title2", "ad_text": "Text"}
_SITELINK_TYPES = {"sitelink_title", "sitelink_description"}


def _normalize_callout_text(text: str) -> str:
    """Keep callout text within Direct's conservative symbol set."""
    clean = re.sub(r"[^0-9A-Za-zА-Яа-яЁё%+\- ₽]", " ", str(text or ""))
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:25]


def _match_targets(content: dict, typ: str, old_text: str) -> list[dict]:
    """Список объектов, где встречается ``old_text`` (для preview и replace)."""
    old = (old_text or "").strip()
    hits: list[dict] = []
    if typ in _AD_FIELD:
        fld = _AD_FIELD[typ]
        for ad in content.get("ads", []):
            if (ad.get(fld) or "").strip() == old:
                hits.append({
                    "ad_id": ad["ad_id"],
                    "campaign_id": ad.get("campaign_id"),
                    "usages": ad.get("usages", []),
                    "source": ad.get("source"),  # "uac" for tp6/tp7 entries
                })
    elif typ == "callout":
        for co in content.get("callouts", []):
            if (co.get("text") or "").strip() == old:
                hits.append({"id": co["id"], "usages": co.get("usages", [])})
    elif typ in _SITELINK_TYPES:
        field = "description" if typ == "sitelink_description" else "title"
        for s in content.get("sitelinks", []):
            if any((it.get(field) or "").strip() == old for it in s.get("items", [])):
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
    old = (old_text or "").strip()
    new = (new_text or "").strip()
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
    uac_client_factory: Callable | None = None,
) -> dict:
    """Replace tp6/tp7 UAC title/body text through cookie PATCH."""
    old = (old_text or "").strip()
    new = (new_text or "").strip()
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
                next_items, candidate_changed = _uac_replace_text_items(current, old, new)
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
            if new not in after_texts:
                errors.append(f"кампания {cid}: read-back не подтвердил новый текст")
                continue
            replaced += changed
            updated_campaigns.append(cid)
        except Exception as e:  # noqa: BLE001
            errors.append(f"кампания {cid}: {str(e)[:180]}")
    return {"replaced": replaced, "errors": errors, "updated_uac_campaigns": updated_campaigns}


_REBIND_SUBTYPE_FIELDS = {"TextAd": "TextAdFieldNames", "DynamicTextAd": "DynamicTextAdFieldNames"}


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
    old = (old_text or "").strip()
    new = (new_text or "").strip()
    field = "description" if typ == "sitelink_description" else "title"
    if not targets:
        return {"replaced": 0, "errors": ["набор с таким текстом быстрой ссылки не найден"]}
    if not new:
        return {"replaced": 0, "errors": ["новый текст быстрой ссылки пустой"]}
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
        if not campaign_ids and not ad_items:
            continue
        try:
            new_set_id = grid.add_sitelink_set(items)
            if not new_set_id:
                errors.append(f"набор {target.get('set_id')}: Grid не вернул id нового набора быстрых ссылок")
                continue
            created_sets.append(int(new_set_id))
            if campaign_ids:
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
            if ad_items:
                if not (token and v5_call):
                    errors.append(f"набор {target.get('set_id')}: нет v5-контекста для перепривязки объявлений")
                else:
                    ok_ads, ad_errs = _v5_rebind_ads_sitelink_set(v5_call, token, login, ad_items, int(new_set_id))
                    replaced += ok_ads
                    if ok_ads:
                        touched_ads.update(it["ad_id"] for it in ad_items)
                    errors.extend(ad_errs)
        except Exception as e:  # noqa: BLE001
            errors.append(f"набор {target.get('set_id')}: {str(e)[:180]}")
    if not replaced and not errors:
        errors.append("не найдены кампании или объявления, привязанные к набору со старым текстом")
    return {"replaced": replaced, "errors": errors, "new_sitelink_set_ids": created_sets}


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
    old = (old_text or "").strip()
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


def _do_replace(token: str, login: str, typ: str, old_text: str, new_text: str,
                content: dict, v5_call: Callable, v501_svc: Callable,
                *, grid_client_factory: Callable | None = None,
                uac_client_factory: Callable | None = None) -> dict:
    """Применяет замену. Возвращает {'replaced': N, 'errors': [...]}."""
    old = (old_text or "").strip()
    if typ in _AD_FIELD:
        targets = _match_targets(content, typ, old)
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
        return _replace_sitelink_text_grid(
            login,
            typ,
            old,
            new_text,
            targets,
            token=token,
            v5_call=v5_call,
            grid_client_factory=grid_client_factory,
        )

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

    def _load_with_index(token: str, login: str) -> dict:
        # _load_account уже строит индекс _ads_by_set (без второго запроса ads.get).
        return _load_account(token, login, v5_call)

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
        if _content_full_access():
            return jsonify({
                "username": username or "admin",
                "fio": "Администратор",
                "full_access": True,
                "directologists": None,
            })
        row = _content_user_record() or {}
        return jsonify({
            "username": username,
            "fio": str(row.get("fio") or "").strip(),
            "full_access": False,
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

    # ── Аккаунты для умного поиска ─────────────────────────────────────────────
    @bp.route("/api/content-editor/accounts")
    @access
    def ce_accounts():
        import psycopg2.extras

        status = (request.args.get("status") or default_status).strip()
        q = (request.args.get("q") or "").strip()
        where = [
            "direction='Авто'",
            "login_key IS NOT NULL",
            "login_key<>''",
            "lower(btrim(login_key)) NOT IN ('нет', 'авито')",
            "btrim(login_key) !~ '^-+$'",
        ]
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
        content = _load_with_index(token, login)
        if content.get("error"):
            return jsonify({"error": content["error"]}), 502
        return jsonify({
            "login": login, "agency": agency,
            "callouts": content["callouts"],
            "sitelinks": content["sitelinks"],
            "ads": content["ads"],
        })

    # ── Preview: сколько объектов затронет замена (без записи) ──────────────────
    @bp.route("/api/content-editor/preview", methods=["POST"])
    @access
    def ce_preview():
        body = request.json or {}
        login = (body.get("login") or "").strip()
        typ = (body.get("type") or "").strip()
        old_text = body.get("old_text") or ""
        if not login or not typ or not old_text.strip():
            return jsonify({"error": "login, type и old_text обязательны"}), 400
        ok, scope_err = _login_allowed(login)
        if not ok:
            return jsonify({"error": scope_err}), 403
        if typ not in _AD_FIELD and typ != "callout" and typ not in _SITELINK_TYPES:
            return jsonify({"error": f"неизвестный тип: {typ}"}), 400
        token, _, err = _token(login)
        if err:
            return jsonify({"error": err}), 404
        content = _load_with_index(token, login)
        if content.get("error"):
            return jsonify({"error": content["error"]}), 502
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
        if not login or not typ or not old_text.strip() or not new_text.strip():
            return jsonify({"error": "login, type, old_text и new_text обязательны"}), 400
        ok, scope_err = _login_allowed(login)
        if not ok:
            return jsonify({"error": scope_err}), 403
        if typ not in _AD_FIELD and typ != "callout" and typ not in _SITELINK_TYPES:
            return jsonify({"error": f"неизвестный тип: {typ}"}), 400
        token, _, err = _token(login)
        if err:
            return jsonify({"error": err}), 404
        content = _load_with_index(token, login)
        if content.get("error"):
            return jsonify({"error": content["error"]}), 502
        out = _do_replace(token, login, typ, old_text, new_text, content, v5_call, v501_svc)
        return jsonify(out)

    @bp.route("/api/content-editor/replace_async", methods=["POST"])
    @access
    def ce_replace_async():
        body = request.json or {}
        login = (body.get("login") or "").strip()
        typ = (body.get("type") or "").strip()
        old_text = body.get("old_text") or ""
        new_text = body.get("new_text") or ""
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
            "(job_id, username, login, agency, type, old_text, new_text, campaign_count, access_directologists) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (job_id, (session.get("username") or "").strip(), login, agency or "", typ,
             old_text, new_text, campaign_count,
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

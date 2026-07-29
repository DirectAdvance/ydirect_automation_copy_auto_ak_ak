"""Shared helpers for the Direct content-editor modules.

Extracted from ``routes_content_editor.py`` (structural split). No Flask routes
here — only pure helpers used by content_sitelinks_routes, content_callouts_routes,
content_jobs_routes, content_replace_routes and routes_content_editor itself.

``make_job_executor`` intentionally stays in ``routes_content_editor`` because
test_routes.py monkeypatches ``content_editor._load_account`` / ``_do_replace``
and then creates an executor — that only works when the closure is defined in the
same module whose names are being patched.
"""
from __future__ import annotations

import os
import re
import time
from typing import Callable

# Re-export so callers that do ``from .agent_board_bridge import ...`` stay unchanged.
from .agent_board_bridge import ensure_content_job_agent_column  # noqa: F401


# ─────────────────────────── v5 low-level helpers ────────────────────────────

def _result(j: dict) -> dict:
    return j.get("result") or {}


def _v5_error_message(e) -> str:
    msg = e.get("error_string") if isinstance(e, dict) else str(e)
    if isinstance(e, dict) and e.get("error_detail"):
        msg = f"{msg}: {e['error_detail']}"
    return str(msg)


def _v5_error_is_transient(j: dict) -> bool:
    """True for transport/temporary v5 failures that are safe to repeat for get."""
    try:
        from .yandex_gateway import is_transient

        return is_transient(j)
    except Exception:  # noqa: BLE001 - keep content editor helpers usable in tests
        msg = _v5_error_message((j or {}).get("error")).lower()
        return any(marker in msg for marker in (
            "timeout", "timed out", "connection", "premature", "temporar",
            "429", "503", "502", "unavailable", "gateway",
            "временно недоступ", "сервис недоступен", "сервер недоступен",
            "повторите", "попробуйте позже", "сервер занят",
        ))


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
        j: dict = {}
        for attempt in range(3):
            j = v5_call(svc, "get", token, login, p)
            if not (j.get("error") and _v5_error_is_transient(j)):
                break
            if attempt < 2:
                time.sleep((1, 3)[min(attempt, 1)])
        if j.get("error"):
            e = j["error"]
            return rows, _v5_error_message(e)
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
            worker text NOT NULL DEFAULT '',
            agent_board_task_id bigint
        )""")
    # два сервиса могут стартовать одновременно — IF NOT EXISTS не спасает от гонки в каталоге
    for ddl in (
        # 'exact' — точечная замена целого поля; 'substring' — массовая замена фрагмента
        f"ALTER TABLE {CE_JOBS_TABLE} ADD COLUMN IF NOT EXISTS mode text NOT NULL DEFAULT 'exact'",
        f"ALTER TABLE {CE_JOBS_TABLE} ADD COLUMN IF NOT EXISTS agent_board_task_id bigint",
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

    from .copy_service.copy_engine import _copy_domain_from_href

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
_FRAG_INVISIBLE = "​‌‍⁠﻿᠎"


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
    "reserve_landing_id",
    "show_title_and_body", "sitelinks", "socdem", "texts", "time_target",
    "titles", "tracking_params", "use_discounts", "week_limit",
    "yandex_maps_enabled",
)


def _uac_campaign_patch_payload(detail: dict, field_key: str, values: list) -> dict:
    """Build the browser-shaped full UAC PATCH body, dropping read-only fields."""
    payload = {k: detail.get(k) for k in _UAC_PATCH_FULL_KEYS if k in detail}
    # Full PATCH = replace: креативы читаются как `contents`, а пишутся как `content_ids` —
    # без переноса патч любого другого поля обнуляет картинки кампании.
    _content_ids = [
        str(c.get("id")) for c in (detail.get("contents") or [])
        if isinstance(c, dict) and c.get("id")
    ]
    if _content_ids:
        payload["content_ids"] = _content_ids
    # Автотаргетинг — вторая асимметрия имён того же класса, что `contents`/`content_ids`:
    # ЧИТАЕТСЯ как `relevance_match_categories` (объекты с флагами), ПИШЕТСЯ как
    # `relevance_match` (плоские списки выбранных имён). Без переноса full PATCH любого
    # другого поля сбрасывал бы автотаргетинг кампании.
    # Эталон — HAR `direct.yandex.ru.65har.har` entry 755 (PATCH 707934116, 200):
    # write-имена категорий совпадают с read-именами 1:1 (`EXACT_V2_MARK`, `NARROW_MARK`),
    # трансляция имён НЕ нужна; `brand_settings` пишется тем же плоским списком.
    # Берём ровно то, что помечено `selected`, ничего не достраивая. Флаг `disabled`
    # НЕ фильтрует: он про доступность переключателя в интерфейсе, а не про включённость,
    # и выброс выбранной-но-заблокированной категории изменил бы настройку кампании.
    _rmc = detail.get("relevance_match_categories")
    if isinstance(_rmc, dict):
        _rm: dict = {
            "active": bool(_rmc.get("active")),
            "categories": [str(c.get("relevance_match_category"))
                           for c in (_rmc.get("categories") or [])
                           if isinstance(c, dict) and c.get("selected")
                           and c.get("relevance_match_category")],
        }
        _brands = [str(b.get("autotargeting_brand_settings"))
                   for b in (_rmc.get("brand_settings") or [])
                   if isinstance(b, dict) and b.get("selected")
                   and b.get("autotargeting_brand_settings")]
        if _brands:
            _rm["brand_settings"] = _brands
        payload["relevance_match"] = _rm
    # Условие ретаргетинга — третья асимметрия read/write того же класса.
    # ЧИТАЕТСЯ богатым: `condition_rules[].goals[]` = объекты с `id` ЧИСЛОМ плюс
    # `name`/`type`/`description`/`segmentInfo`/`time`/`platformId`/`bundleId`.
    # ПИШЕТСЯ урезанным: `goals[]` = только `{"id": "<строка>"}`; обёртка
    # `condition_rules[].type`/`interestType` переносится как есть; верхнеуровневые
    # `name`/`id` (в detail оба `null`) браузер не шлёт — не добавляем.
    # Эталоны: HAR-65 entry 755 (МК 707934116) и HAR-6 entry 16 (МК 710852886, 10 целей),
    # оба PATCH 200 — write-форма в обоих идентична по устройству.
    # Источник — `ca_retargeting_condition`: его имя совпадает с write-ключом, т.е.
    # соответствие прямое, а не выведенное. Дубль `retargeting_condition` сверен по
    # всем 20 detail из HAR (`/result`), содержимое совпадает 1:1 и ключи всегда
    # присутствуют парой — берём его только как фолбэк, если ca-ключа нет вовсе.
    # ⚠️ Ключ шлём ТОЛЬКО когда условие реально задано: у той же кампании в entry 611
    # значение было `null` и браузер ключ не слал. Отправка пустой структуры под
    # REPLACE-семантикой равносильна ОЧИСТКЕ условия — поэтому «пусто → нет ключа».
    _crc = detail.get("ca_retargeting_condition")
    if _crc is None:
        _crc = detail.get("retargeting_condition")
    if isinstance(_crc, dict):
        _rules = []
        for _rule in (_crc.get("condition_rules") or []):
            if not isinstance(_rule, dict):
                continue
            _goals = [
                {"id": str(g.get("id"))} for g in (_rule.get("goals") or [])
                if isinstance(g, dict) and g.get("id") is not None
            ]
            if not _goals:
                continue
            _out_rule = {}
            for _k in ("type", "interestType"):
                if _rule.get(_k) is not None:
                    _out_rule[_k] = _rule[_k]
            _out_rule["goals"] = _goals
            _rules.append(_out_rule)
        if _rules:
            payload["ca_retargeting_condition"] = {"condition_rules": _rules}
    payload[field_key] = values
    # tp6/tp7 autotargeting details can contain feed ids from read model, while
    # the save endpoint validates them as MUST_BE_NULL. Browser sends keywords:[]
    # for this mode and omits feed fields. `ecom` НЕ входит в feed-семейство —
    # браузер шлёт его всегда (`ecom: false` в эталоне _har/UAC_image_replace.json).
    if payload.get("keywords") is None:
        payload["keywords"] = []
        # Товарные кампании (tp7, `ecom: true`) шлют `keywords: []` И непустой фид ОДНОВРЕМЕННО —
        # там `feedId` NON_NULL (эталон _har/UAC_ecom_feed_replace.json: feed_id=listings_feed_id=
        # 2611255, feed_filters=listings_feed_filters=[{"conditions": []}]). Живой detail товарки
        # отдаёт `keywords: null`, поэтому эта ветка срабатывает и на товарке — но обнулять фид
        # здесь НЕЛЬЗЯ, иначе PATCH падает `feedId CANNOT_BE_NULL` (сигнатура
        # UAC_PATCH_TP7_FEED_ID_REQUIRED). Обнуление фида (MUST_BE_NULL) осмысленно только у
        # НЕ-товарных МК (`ecom: false`, эталон _har/UAC_image_replace.json: feed_id=None), где
        # detail может нести id из read-модели. Различаем ветки по `ecom` — тому же флагу, по
        # которому браузер решает, слать ли фид. Пустые фид-поля товарки не трогаются; у МК их
        # всё равно уберёт следующий блок (они None).
        if not detail.get("ecom"):
            for key in ("feed_id", "listings_feed_id", "feed_filters", "listings_feed_filters"):
                if key != field_key:
                    payload.pop(key, None)
    # Маппинг полей фида на текст объявления осмыслен только у товарных кампаний
    # (tp7, там значения непустые); у обычной МК браузер эти ключи не шлёт вовсе.
    # Опускаем ТОЛЬКО пустые: под REPLACE-семантикой full PATCH выброс None-ключа
    # ничего не обнуляет, а непустое значение товарки сохраняется как есть.
    # Пустые feed-поля выбрасываем ВСЕГДА, а не только в ветке `keywords is None`:
    # у МК с `keywords: []` (эталон HAR-64 entry 611 / HAR-65 entry 755, кампания
    # 707934116) ветка не срабатывает, и в payload уезжали 4 лишних `null`, которых
    # браузер не шлёт. Аргумент безопасности тот же: под REPLACE выброс None-ключа
    # обнулять нечего; непустой фид товарки (tp7) не трогается.
    for key in ("field_to_use_as_body", "field_to_use_as_name",
                "feed_id", "listings_feed_id", "feed_filters", "listings_feed_filters"):
        if key != field_key and payload.get(key) is None:
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
    except Exception as exc:  # noqa: BLE001
        print(f"[content-editor] uac-patch-text:{campaign_id} partial PATCH failed ({exc!r}), "
              f"falling back to full PATCH", flush=True)
        detail = client._request("GET", f"/campaign/{campaign_id}", step=f"uac-detail:{campaign_id}")
        detail = _unwrap_uac_response(detail)
        detail = _uac_campaign_patch_payload(detail, field_key, values)
        return client._request(
            "PATCH",
            f"/campaign/{campaign_id}",
            json_body=detail,
            step=f"uac-patch-full:{campaign_id}",
        )


def _uac_read_client(login: str, factory: Callable | None):
    """Обёртка ``UacReadClient`` (есть ``.client`` и ``.campaign_details``).

    ``factory`` (тест-инъекция) возвращает такую же обёртку целиком."""
    if factory is None:
        from .uac_read import UacReadClient

        return UacReadClient(login)
    return factory(login)


def _uac_client(login: str, factory: Callable | None):
    """Низкоуровневый UAC web-api клиент (``._request``) для replace/reorder.

    ``factory`` (тест-инъекция) возвращает уже сам клиент, а не обёртку."""
    if factory is None:
        from .uac_read import UacReadClient

        return UacReadClient(login).client
    return factory(login)


def _uac_cids_from_targets(targets: list[dict] | None) -> list[int]:
    """Уникальные положительные campaign_id из targets (прямой или из usages)."""
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
    return campaign_ids


def _load_account(
    token: str,
    login: str,
    v5_call: Callable,
    *,
    grid_client_factory: Callable | None = None,
    uac_read_client_factory: Callable | None = None,
    include_adgroups: bool = True,
    include_campaign_sitelinks: bool = True,
    include_uac_campaigns: bool = True,
    include_callouts: bool = True,
) -> dict:
    """Читает кампании, группы, объявления, наборы ссылок и уточнения аккаунта."""
    # 1) Кампании: архивные исключаем из write-preview. Direct не даёт менять объявления
    # в архивных кампаниях (ACTION_IN_ARCHIVED_CAMPAIGN), поэтому их нельзя считать
    # обычными целями массовой замены.
    camps, err = _v5_paginate(
        v5_call, "campaigns", token, login,
        _strip_campaign_subfield_names(
            {"SelectionCriteria": {}, "FieldNames": ["Id", "Name", "Type", "State"]}
        ),
        "Campaigns",
    )
    if err:
        return {"error": f"campaigns.get: {err}"}
    camp_name: dict[int, str] = {}
    camp_type: dict[int, str] = {}
    camp_state: dict[int, str] = {}
    for c in camps:
        cid = int(c.get("Id") or 0)
        if cid:
            state = str(c.get("State") or "").upper()
            if state == "ARCHIVED":
                camp_state[cid] = state
                continue
            camp_name[cid] = c.get("Name") or ""
            camp_type[cid] = c.get("Type") or ""
            camp_state[cid] = state
    v5_campaign_ids = sorted(camp_name)
    # UAC/tp6/tp7 campaigns are not reliably visible in v5. Add them from the
    # cookie-only UAC list so editor replacements can target PATCH /uac/campaign.
    uac_read_error: str | None = None
    uac_detail_client = None
    if include_uac_campaigns:
        try:
            uac_detail_client = _uac_read_client(login, uac_read_client_factory)
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
            print(f"[content-editor] UAC list_campaigns failed login={login}: {e!r}", flush=True)
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
                print(f"[content-editor] Grid tp6/tp7 fallback failed login={login}: {grid_e!r}", flush=True)
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
    if include_adgroups:
        groups, err = _v5_paginate_campaign_batches(
            v5_call, "adgroups", token, login,
            {"FieldNames": ["Id", "Name", "CampaignId"]},
            "AdGroups",
            v5_campaign_ids,
        )
        if err:
            return {"error": f"adgroups.get: {err}"}
    else:
        groups = []
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
    ads_inventory: list[dict] = []          # все v5-объявления с поддерживаемым sitelink write-path
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
        ssid = (content_rows[0] if content_rows else _ad_texts(a))["sitelink_set_id"]
        subtype = next((k for k in ("TextAd", "DynamicTextAd", "ResponsiveAd") if a.get(k)), "")
        if subtype:
            ads_inventory.append({
                "ad_id": ad_id,
                "campaign_id": cid,
                "adgroup_id": agid,
                "subtype": subtype,
                "campaign_name": usage["campaign_name"],
                "adgroup_name": usage["adgroup_name"],
                "sitelink_set_id": ssid,
            })
        for t in content_rows:
            ads_out.append({
                "ad_id": ad_id,
                "title": t["title"], "title2": t["title2"], "text": t["text"],
                "usages": [usage],
            })
        if ssid:
            sitelink_usages.setdefault(str(ssid), []).append(usage)
            subtype = subtype or "TextAd"
            ads_by_set.setdefault(str(ssid), []).append({
                "ad_id": ad_id, "subtype": subtype,
                "campaign_name": usage["campaign_name"], "adgroup_name": usage["adgroup_name"],
            })

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
                uac_detail_client = _uac_read_client(login, uac_read_client_factory)
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
            print(f"[content-editor] UAC campaign_details read failed login={login}: {e!r}", flush=True)
            _uac_detail_err = str(e)[:200]
            uac_read_error = (
                f"{uac_read_error}; UAC details: {_uac_detail_err}"
                if uac_read_error else _uac_detail_err
            )

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
            print(f"[content-editor] Grid campaign-level sitelinks failed login={login}: {e!r}", flush=True)
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
        # Для замены текста быстрых ссылок ad-level ResponsiveAd теперь ПОДДЕРЖАН:
        # создаём новый набор и перепривязываем его к объявлениям через Grid RMW.
        # Поэтому старое UI-предупреждение «ResponsiveAd не изменится» больше не
        # актуально; служебные поля оставляем нулевыми для обратной совместимости с
        # фронтом до полного вычищения legacy-ветки.
        responsive_count = 0
        responsive_examples: list[dict] = []
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
            "responsive_count": responsive_count,
            "responsive_examples": responsive_examples,
        })
    # UAC (tp6/tp7) быстрые ссылки — синтетические наборы уровня кампании (source="uac").
    sitelinks_out.extend(uac_sitelinks_out)

    # 5) Уточнения (callouts) — adextensions type CALLOUT.
    exts = []
    grid_callout_error = None
    if include_callouts:
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
            print(f"[content-editor] Grid callout-usages failed login={login}: {e!r}", flush=True)
            campaign_callout_ids = {}
            grid_callout_error = f"Grid callout-usages: {str(e)[:200]}"
    callouts_out: list[dict] = []
    for e in exts:
        eid = str(e.get("Id") or "")
        text = (e.get("Callout") or {}).get("CalloutText") or ""
        usages = [_usage_for(cid, 0) for cid in callout_to_camps.get(eid, [])]
        callouts_out.append({"id": int(e.get("Id") or 0), "text": text, "usages": usages})

    out = {"callouts": callouts_out, "sitelinks": sitelinks_out, "ads": ads_out,
           "links": links_out,
           "_ads_by_set": ads_by_set, "_campaign_callout_ids": campaign_callout_ids,
           "_ads_inventory": ads_inventory, "_campaign_ids": campaign_ids,
           "_campaign_types": camp_type, "_campaign_states": camp_state,
           "_uac_campaign_ids": uac_ids}
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
# замены поля быстрой ссылки, и операции над целым набором.
_SITELINK_JOB_TYPES = _SITELINK_TYPES | {"sitelink_reorder", "sitelink_assign"}


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
                    "level": s.get("level") or "ad",
                    "campaign_ids": s.get("campaign_ids") or [],
                    "ad_ids": _ads_using_set(content, int(s.get("set_id") or 0)),
                    "ad_items": content.get("_ads_by_set", {}).get(str(s.get("set_id")), []),
                })
    return hits


def _already_applied_sitelink_result(content: dict, typ: str, new_text: str) -> dict | None:
    """Idempotent retry: old value is gone, but requested sitelink value is already live."""
    targets = _match_targets(content, typ, _frag_trim(new_text))
    if not targets:
        return None
    campaign_ids: set[int] = set()
    ad_ids: set[int] = set()
    fallback_sets = 0
    for target in targets:
        touched = False
        raw_campaign_ids = []
        if target.get("source") == "uac":
            raw_campaign_ids.append(target.get("campaign_id"))
        raw_campaign_ids.extend(target.get("campaign_ids") or [])
        for usage in target.get("usages") or []:
            raw_campaign_ids.append((usage or {}).get("campaign_id"))
        for raw_cid in raw_campaign_ids:
            try:
                cid = int(raw_cid or 0)
            except (TypeError, ValueError):
                continue
            if cid > 0:
                campaign_ids.add(cid)
                touched = True
        for raw in target.get("ad_items") or [{"ad_id": x} for x in (target.get("ad_ids") or [])]:
            try:
                aid = int((raw or {}).get("ad_id") or 0)
            except (TypeError, ValueError):
                continue
            if aid > 0:
                ad_ids.add(aid)
                touched = True
        if not touched:
            fallback_sets += 1
    replaced = len(campaign_ids) + len(ad_ids) + fallback_sets
    return {
        "replaced": replaced,
        "errors": [],
        "already_applied": True,
        "matched_new_sitelink_sets": len(targets),
        "matched_new_campaigns": len(campaign_ids),
        "matched_new_ads": len(ad_ids),
    }


def _content_regular_campaign_ids(content: dict) -> list[int]:
    campaign_types = content.get("_campaign_types") or {}
    out: list[int] = []
    for raw in content.get("_campaign_ids") or []:
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            continue
        ctype = str(campaign_types.get(cid) or campaign_types.get(str(cid)) or "")
        if cid > 0 and ctype not in {"UNIFIED_CAMPAIGN", "UAC"} and cid not in out:
            out.append(cid)
    return out


def _content_inventory_for_campaigns(content: dict, campaign_ids: list[int]) -> list[dict]:
    wanted = {int(cid) for cid in campaign_ids if str(cid).isdigit()}
    out: list[dict] = []
    seen: set[int] = set()
    for row in (content.get("_ads_inventory") or []):
        try:
            aid = int((row or {}).get("ad_id") or 0)
            cid = int((row or {}).get("campaign_id") or 0)
        except (TypeError, ValueError):
            continue
        if aid <= 0 or cid not in wanted or aid in seen:
            continue
        seen.add(aid)
        out.append(row)
    return out


def _grid_clear_text_ads_overrides(
    login: str,
    ad_items: list[dict],
    campaign_ids: list[int],
    *,
    clear_callouts: bool = False,
    clear_sitelinks: bool = False,
    grid_client_factory: Callable | None = None,
) -> tuple[int, list[str]]:
    errors: list[str] = []
    grid = (grid_client_factory or _grid_client)(login)
    ad_ids: list[int] = []
    cids: list[int] = []
    for raw in ad_items or []:
        try:
            aid = int((raw or {}).get("ad_id") or 0)
        except (TypeError, ValueError):
            continue
        subtype = str((raw or {}).get("subtype") or "")
        if subtype == "TextAd" and aid > 0 and aid not in ad_ids:
            ad_ids.append(aid)
    for raw in campaign_ids or []:
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            continue
        if cid > 0 and cid not in cids:
            cids.append(cid)
    if not ad_ids or not cids or (not clear_callouts and not clear_sitelinks):
        return 0, errors
    before = grid.text_ads_for_update(cids, ad_ids)
    payload: list[dict] = []
    changed_ids: list[int] = []
    for aid in ad_ids:
        item = before.get(aid)
        if not isinstance(item, dict):
            continue
        nxt = dict(item)
        changed = False
        if clear_callouts and ((item.get("inheritableCallouts") or {}).get("policy") or "").upper() != "INHERIT":
            nxt["inheritableCallouts"] = {"policy": "INHERIT"}
            changed = True
        if clear_sitelinks and ((item.get("inheritableSitelinkSet") or {}).get("policy") or "").upper() != "INHERIT":
            nxt["inheritableSitelinkSet"] = {"policy": "INHERIT"}
            changed = True
        if changed:
            payload.append(nxt)
            changed_ids.append(aid)
    if not payload:
        return 0, errors
    updated = int(grid.update_text_ads(payload, allow_empty_image_hashes=True) or 0)
    errors.extend(list(getattr(grid, "last_ad_update_errors", []) or []))
    after = grid.text_ads_for_update(cids, changed_ids)
    confirmed = 0
    for aid in changed_ids:
        state = after.get(aid) if isinstance(after, dict) else None
        callouts_ok = (not clear_callouts
                       or ((state or {}).get("inheritableCallouts") or {}).get("policy") == "INHERIT")
        sitelinks_ok = (not clear_sitelinks
                        or ((state or {}).get("inheritableSitelinkSet") or {}).get("policy") == "INHERIT")
        if callouts_ok and sitelinks_ok:
            confirmed += 1
    if updated and confirmed < min(updated, len(changed_ids)):
        errors.append(f"Grid не подтвердил очистку override у {len(changed_ids) - confirmed} TextAd")
    return confirmed, errors


def _grid_clear_responsive_ads_overrides(
    login: str,
    ad_items: list[dict],
    campaign_ids: list[int],
    *,
    clear_callouts: bool = False,
    clear_sitelinks: bool = False,
    grid_client_factory: Callable | None = None,
) -> tuple[int, list[str]]:
    errors: list[str] = []
    grid = (grid_client_factory or _grid_client)(login)
    ad_ids: list[int] = []
    cids: list[int] = []
    for raw in ad_items or []:
        try:
            aid = int((raw or {}).get("ad_id") or 0)
        except (TypeError, ValueError):
            continue
        subtype = str((raw or {}).get("subtype") or "")
        if subtype == "ResponsiveAd" and aid > 0 and aid not in ad_ids:
            ad_ids.append(aid)
    for raw in campaign_ids or []:
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            continue
        if cid > 0 and cid not in cids:
            cids.append(cid)
    if not ad_ids or not cids or (not clear_callouts and not clear_sitelinks):
        return 0, errors
    before = grid.adaptive_ads_for_update(cids, ad_ids)
    payload: list[dict] = []
    changed_ids: list[int] = []
    for aid in ad_ids:
        item = before.get(aid)
        if not isinstance(item, dict):
            continue
        nxt = dict(item)
        changed = False
        if clear_callouts and ((item.get("inheritableCallouts") or {}).get("policy") or "").upper() != "INHERIT":
            nxt["inheritableCallouts"] = {"policy": "INHERIT"}
            changed = True
        if clear_sitelinks and ((item.get("inheritableSitelinkSet") or {}).get("policy") or "").upper() != "INHERIT":
            nxt["inheritableSitelinkSet"] = {"policy": "INHERIT"}
            changed = True
        if changed:
            payload.append(nxt)
            changed_ids.append(aid)
    if not payload:
        return 0, errors
    updated = int(grid.update_ad_images(payload, allow_empty_images=True) or 0)
    errors.extend(list(getattr(grid, "last_ad_update_errors", []) or []))
    after = grid.adaptive_ads_for_update(cids, changed_ids)
    confirmed = 0
    for aid in changed_ids:
        state = after.get(aid) if isinstance(after, dict) else None
        callouts_ok = (not clear_callouts
                       or ((state or {}).get("inheritableCallouts") or {}).get("policy") == "INHERIT")
        sitelinks_ok = (not clear_sitelinks
                        or ((state or {}).get("inheritableSitelinkSet") or {}).get("policy") == "INHERIT")
        if callouts_ok and sitelinks_ok:
            confirmed += 1
    if updated and confirmed < min(updated, len(changed_ids)):
        errors.append(f"Grid не подтвердил очистку override у {len(changed_ids) - confirmed} ResponsiveAd")
    return confirmed, errors


def _clear_ad_level_asset_overrides(
    login: str,
    content: dict,
    campaign_ids: list[int],
    *,
    clear_callouts: bool = False,
    clear_sitelinks: bool = False,
    grid_client_factory: Callable | None = None,
) -> tuple[int, list[str]]:
    inventory = _content_inventory_for_campaigns(content, campaign_ids)
    text_ok, text_errs = _grid_clear_text_ads_overrides(
        login, inventory, campaign_ids,
        clear_callouts=clear_callouts,
        clear_sitelinks=clear_sitelinks,
        grid_client_factory=grid_client_factory,
    )
    resp_ok, resp_errs = _grid_clear_responsive_ads_overrides(
        login, inventory, campaign_ids,
        clear_callouts=clear_callouts,
        clear_sitelinks=clear_sitelinks,
        grid_client_factory=grid_client_factory,
    )
    return text_ok + resp_ok, [*text_errs, *resp_errs]

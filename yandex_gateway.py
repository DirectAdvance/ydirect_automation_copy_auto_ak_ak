"""Yandex Direct transports and agency credential resolution.

OAuth v5/v501 and cookie-backed Grid remain deliberately separate transports.  This
module contains no Flask routes and never imports :mod:`direct.blueprint`.
"""
from __future__ import annotations

import concurrent.futures as _cf
import json
import re
import sys
import time

from . import campaign as cmc
from .create.create_set_units import is_units_exhausted
from .direct_repository import victory_conn, victory_conn_rw


V5_URL = "https://api.direct.yandex.com/json/v5/"
V501_URL = "https://api.direct.yandex.com/json/v501/"
GRID_URL = "https://direct.yandex.ru/web-api/grid/api"
LIVE_V4_URL = "https://api.direct.yandex.ru/live/v4/json/"
UNITS_PER_CAMPAIGN = 2500
HTTP_HARD_TIMEOUT = 90
_HTTP_EXECUTOR = _cf.ThreadPoolExecutor(max_workers=32, thread_name_prefix="v5http")
_TRANSIENT_MARKERS = (
    "timeout", "timed out", "connection", "premature", "temporar", "rate limit",
    "too many request", "429", "503", "502", "unavailable", "gateway",
    # РУССКИЕ формулировки Директа (Accept-Language: ru) — без них «Сервис временно недоступен»
    # не считался транзиентным и позиция падала целиком (ads.add одним чанком на 96 групп).
    # Только реально повторяемые: ошибки валидации (4000/DefectIds/«цель не найдена») сюда НЕ попадают.
    "временно недоступ", "сервис недоступен", "сервер недоступен",
    "повторите", "попробуйте позже", "попробуйте ещё раз", "попробуйте еще раз",
    "превышено количество запросов", "слишком много запросов", "сервер занят",
)
# Ретраи транзиентных ответов: 3 попытки, backoff 2с/5с. Держим коротким — чанк большой,
# джоба и так идёт десятками минут; бесконечный цикл недопустим.
_HTTP_RETRY_TRIES = 3
_HTTP_RETRY_BACKOFF = (2, 5)


def _creates_objects(body: dict) -> bool:
    """True для методов, СОЗДАЮЩИХ объекты (``add``): повтор после обрыва связи даёт дубли.

    Остальные методы Директа идемпотентны по Id (``get``/``update``/``delete``/``suspend``/
    ``resume``/``archive``) — их повтор безопасен даже когда неизвестно, доехал ли запрос.
    Метод не распознан → считаем создающим (консервативно, дубль дороже отказа)."""
    method = str((body or {}).get("method") or "").strip().lower()
    return not method or method.startswith("add")


def direct_tokens() -> dict:
    """Return ``{agency_account: oauth_token}`` from the project secret loader."""
    secret_dir = str(cmc._find_secret_dir())
    if secret_dir not in sys.path:
        sys.path.insert(0, secret_dir)
    from loader import load_yandex_direct  # noqa: PLC0415

    out = {}
    for agency, info in (load_yandex_direct().get("tokens") or {}).items():
        token = info.get("oauth_token") if isinstance(info, dict) else info
        if token:
            out[agency] = token
    return out


def _headers(token: str, login: str) -> dict:
    return {
        "Authorization": "Bearer " + token,
        "Client-Login": login,
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
        "Use-Operator-Units": "true",
    }


def v5_get(svc: str, token: str, login: str, fieldnames: list[str], criteria=None,
           extra: dict | None = None) -> dict:
    import requests

    params: dict = {"FieldNames": fieldnames}
    if criteria is not None:
        params["SelectionCriteria"] = criteria
    if extra:
        params.update(extra)
    try:
        return requests.post(
            V5_URL + svc,
            headers=_headers(token, login),
            json={"method": "get", "params": params},
            timeout=30,
        ).json()
    except Exception as exc:  # noqa: BLE001
        return {"error": {"error_string": str(exc)[:120]}}


def v5_units(token: str, login: str) -> dict | None:
    import requests

    body = {"method": "get", "params": {
        "FieldNames": ["Id"], "SelectionCriteria": {}, "Page": {"Limit": 1},
    }}
    try:
        response = requests.post(V5_URL + "campaigns", headers=_headers(token, login),
                                 json=body, timeout=20)
        parts = [int(value) for value in (response.headers.get("Units") or "").split("/")
                 if value.strip().lstrip("-").isdigit()]
        if len(parts) == 3:
            spent, rest, limit = parts
            return {"spent": spent, "rest": max(0, rest), "limit": limit}
    except Exception:  # noqa: BLE001
        pass
    return None


def bounded_post(url: str, headers: dict, body: dict, tries: int = _HTTP_RETRY_TRIES) -> dict:
    """POST в Директ с ретраями ТОЛЬКО на транзиентные ответы (``is_transient``).

    Различаем ДВА источника транзиентной ошибки:

    * **Ответ Директа** — пришёл разобранный JSON с верхнеуровневым ``error``. Это отказ запроса
      ЦЕЛИКОМ (ни одна операция чанка не применена), повтор безопасен для любого метода.
    * **Локальный сбой** — обрыв связи или hard timeout: ответа нет, и применился ли запрос
      НЕИЗВЕСТНО (сервер мог принять его и не успеть ответить). Здесь повторяем только
      идемпотентные методы; ``add`` не повторяем — иначе дубли на весь чанк.

    Пер-элементные ошибки лежат в ``result.AddResults`` и ретрай не вызывают. Ошибки валидации
    (4000, DefectIds, «цель не найдена») детерминированы — маркеров не содержат и не повторяются."""
    import requests

    def _do():
        return requests.post(url, headers=headers, json=body, timeout=60).json()

    attempts = max(1, int(tries or 1))
    unsafe_after_local_failure = _creates_objects(body)
    payload: dict = {}
    for attempt in range(attempts):
        local_failure = False
        try:
            payload = _HTTP_EXECUTOR.submit(_do).result(timeout=HTTP_HARD_TIMEOUT)
        except _cf.TimeoutError:
            local_failure = True
            payload = {"error": {"error_string": f"hard timeout {HTTP_HARD_TIMEOUT}s exceeded"}}
        except Exception as exc:  # noqa: BLE001
            local_failure = True
            payload = {"error": {"error_string": str(exc)[:120]}}
        if not isinstance(payload, dict) or not is_transient(payload):
            return payload
        if local_failure and unsafe_after_local_failure:
            return payload
        if attempt + 1 >= attempts:
            break
        time.sleep(_HTTP_RETRY_BACKOFF[min(attempt, len(_HTTP_RETRY_BACKOFF) - 1)])
    return payload


def v5_call(svc: str, method: str, token: str, login: str, params: dict) -> dict:
    return bounded_post(V5_URL + svc, _headers(token, login), {"method": method, "params": params})


def v501_call(method: str, token: str, login: str, params: dict) -> dict:
    return bounded_post(V501_URL + "campaigns", _headers(token, login),
                        {"method": method, "params": params})


def v501_svc(svc: str, method: str, token: str, login: str, params: dict) -> dict:
    return bounded_post(V501_URL + svc, _headers(token, login), {"method": method, "params": params})


def v5_err(payload: dict) -> str:
    error = payload.get("error")
    if not isinstance(error, dict):
        return str(error)
    parts = [error.get("error_string") or ""]
    detail = error.get("error_detail") or error.get("message") or ""
    if detail and detail not in parts:
        parts.append(str(detail))
    return " — ".join(part for part in parts if part)


def is_transient(payload: dict) -> bool:
    return "error" in payload and any(marker in v5_err(payload).lower()
                                      for marker in _TRANSIENT_MARKERS)


def agency_override_get(login: str) -> str | None:
    try:
        conn = victory_conn()
    except Exception:  # noqa: BLE001
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT agency_account FROM public.direct_agency_overrides "
                        "WHERE login_key = %s", (login,))
            row = cur.fetchone()
        return row[0] if row and row[0] else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        conn.close()


def resolve_agency_hint(login: str, agency_hint: str) -> str:
    agency = (agency_hint or "").strip().lower()
    if agency and agency != "none":
        return agency
    if not login:
        return agency
    cached = agency_override_get(login)
    if cached:
        return cached.strip().lower()
    try:
        conn = victory_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT agency_account FROM public.local_gsheet_sites "
                            "WHERE login_key=%s AND direction='Авто' LIMIT 1", (login,))
                row = cur.fetchone()
        finally:
            conn.close()
        db_agency = row[0] if row and row[0] else None
        if db_agency and db_agency.strip().lower() not in ("none", ""):
            return db_agency.strip().lower()
    except Exception:  # noqa: BLE001
        pass
    return agency


def agency_override_save(login: str, agency: str) -> None:
    if not login or not agency:
        return
    try:
        conn = victory_conn_rw()
    except Exception:  # noqa: BLE001
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS public.direct_agency_overrides ("
                "login_key text PRIMARY KEY, agency_account text NOT NULL, "
                "updated_at timestamptz NOT NULL DEFAULT now())")
            cur.execute(
                "INSERT INTO public.direct_agency_overrides (login_key, agency_account, updated_at) "
                "VALUES (%s, %s, now()) ON CONFLICT (login_key) DO UPDATE SET "
                "agency_account=EXCLUDED.agency_account, updated_at=now()", (login, agency))
        conn.commit()
    except Exception:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        conn.close()


def token_for_login(login: str, agency: str, tokens: dict) -> tuple[str | None, str | None]:
    seen: set[str] = set()
    for candidate in (agency_override_get(login), agency if agency and agency != "None" else None):
        if not candidate or candidate in seen or not tokens.get(candidate):
            continue
        seen.add(candidate)
        result = v5_get("campaigns", tokens[candidate], login, ["Id"], criteria={})
        if "error" not in result:
            return tokens[candidate], candidate
        if is_transient(result) or is_units_exhausted(result.get("error")):
            return tokens[candidate], candidate
    for candidate, token in tokens.items():
        if candidate in seen:
            continue
        result = v5_get("campaigns", token, login, ["Id"], criteria={})
        if "error" not in result or is_units_exhausted(result.get("error")):
            agency_override_save(login, candidate)
            return token, candidate
    return None, None


def units_alive_for_login(login: str, agency: str = "") -> bool | None:
    try:
        token, _ = token_for_login(login, agency, direct_tokens())
        units = v5_units(token, login) if token else None
        return None if not units else int(units.get("rest") or 0) >= UNITS_PER_CAMPAIGN
    except Exception:  # noqa: BLE001
        return None


def grid_list_campaigns(login: str, only_draft: bool = False) -> list[dict]:
    """List all campaign families through cookie-backed Grid, including UAC drafts."""
    import requests

    cookie = cmc.pick_working_cookie(login)
    if not cookie:
        raise RuntimeError("нет рабочей куки для grid")
    session = requests.Session()
    session.verify = False
    csrf = {"token": None}

    def _post(operation, query, variables):
        headers = {
            "Cookie": cookie, "dna-operation-name": operation, "x-direct-api": "1",
            "x-detected-locale": "ru", "Content-Type": "application/json",
            "User-Agent": cmc.USER_AGENT, "Origin": "https://direct.yandex.ru",
            "Referer": f"https://direct.yandex.ru/dna/grid/campaigns?ulogin={login}",
        }
        if csrf["token"]:
            headers["x-csrf-token"] = csrf["token"]
        url = f"{GRID_URL}?operationName={operation}&ulogin={login}"
        payload = {"operationName": operation, "query": query, "variables": variables}
        response = session.post(url, json=payload, headers=headers, timeout=40)
        if response.status_code == 403:
            match = re.search(r"_direct_csrf_token=([^;,\s]+)", response.headers.get("Set-Cookie", ""))
            token = response.cookies.get("_direct_csrf_token") or (match.group(1) if match else None)
            if token:
                csrf["token"] = token
                headers["x-csrf-token"] = token
                response = session.post(url, json=payload, headers=headers, timeout=40)
        return response

    _post("Callouts", "query Callouts($login:String!){callouts(input:{searchBy:{login:$login}"
          "filter:{deleted:false}}){id}}", {"login": login})
    query = ("query C($login:String!,$inp:GdCampaignsContainerInput!){client(searchBy:{login:$login}){"
             "campaigns(input:$inp){rowset{id name __typename status{primaryStatus archived}}}}}")
    out, offset = [], 0
    while True:
        inp = {
            "filter": {},
            "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
            "limitOffset": {"limit": 200, "offset": offset},
            "orderBy": [{"order": "ASC", "field": "STATUS"}],
        }
        payload = _post("C", query, {"login": login, "inp": inp}).json()
        if payload.get("errors"):
            raise RuntimeError("grid campaigns: " + json.dumps(payload["errors"], ensure_ascii=False)[:200])
        rows = (((payload.get("data") or {}).get("client") or {}).get("campaigns") or {}).get("rowset") or []
        for campaign in rows:
            status = campaign.get("status") or {}
            out.append({
                "id": campaign.get("id"), "name": campaign.get("name"),
                "typename": campaign.get("__typename"),
                "status": status.get("primaryStatus") or "", "archived": bool(status.get("archived")),
            })
        if len(rows) < 200:
            break
        offset += 200
    return [campaign for campaign in out if campaign["status"] == "DRAFT"] if only_draft else out


_BLOCK_QUERY = (
    "query CampaignsTotal($login:String! $campaignInput:GdCampaignsContainerInput!){"
    "userFeatures client(searchBy:{login:$login}){"
    "campaigns(input:$campaignInput){totalCampaigns{totalSumRest}}}}"
)
_BLOCK_INPUT = {
    "filter": {},
    "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
    "limitOffset": {"limit": 1, "offset": 0},
    "orderBy": [{"order": "ASC", "field": "STATUS"}],
}


def grid_post(cookie: str, csrf, login: str):
    import requests

    headers = {"Cookie": cookie, "dna-operation-name": "CampaignsTotal", "x-direct-api": "1",
               "x-detected-locale": "ru", "Content-Type": "application/json", "User-Agent": cmc.USER_AGENT}
    if csrf:
        headers["x-csrf-token"] = csrf
    try:
        return requests.post(
            f"{GRID_URL}?operationName=CampaignsTotal&ulogin={login}",
            json={"operationName": "CampaignsTotal", "query": _BLOCK_QUERY,
                  "variables": {"login": login, "campaignInput": _BLOCK_INPUT}},
            headers=headers, timeout=40, verify=False,
        )
    except Exception:  # noqa: BLE001
        return None


def grid_csrf(response):
    if response is None:
        return None
    token = response.cookies.get("_direct_csrf_token")
    if token:
        return token
    match = re.search(r"_direct_csrf_token=([^;,\s]+)", response.headers.get("Set-Cookie", ""))
    return match.group(1) if match else None


def block_bootstrap(cookie: str, agency_login: str):
    response = grid_post(cookie, None, agency_login)
    if response is None:
        return None
    if response.status_code == 200:
        return grid_csrf(response) or ""
    return grid_csrf(response) if response.status_code == 403 else None


def block_check(cookie: str, csrf, login: str):
    response = grid_post(cookie, csrf or None, login)
    if response is not None and response.status_code == 403 and not csrf:
        refreshed = grid_csrf(response)
        if refreshed:
            response = grid_post(cookie, refreshed, login)
    if response is None or response.status_code != 200:
        return None
    try:
        data = response.json()
    except Exception:  # noqa: BLE001
        return None
    if data.get("errors"):
        return None
    top = data.get("data") or {}
    return "BLOCKED" in (top["userFeatures"] or []) if "userFeatures" in top else None


# Historical private names remain available while route modules migrate.
_direct_tokens = direct_tokens
_v5_get = v5_get
_v5_units = v5_units
_bounded_post = bounded_post
_v5_call = v5_call
_v501_call = v501_call
_v501_svc = v501_svc
_v5_err = v5_err
_is_transient = is_transient
_agency_override_get = agency_override_get
_resolve_agency_hint = resolve_agency_hint
_agency_override_save = agency_override_save
_token_for_login = token_for_login
_units_alive_for_login = units_alive_for_login
_grid_list_campaigns = grid_list_campaigns
_grid_post = grid_post
_grid_csrf = grid_csrf
_block_bootstrap = block_bootstrap
_block_check = block_check

cmc.set_agency_resolver(lambda login: resolve_agency_hint(login, ""))

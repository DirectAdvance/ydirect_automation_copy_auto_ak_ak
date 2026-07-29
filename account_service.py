"""Account-facing Direct application service without blueprint imports."""
from __future__ import annotations

import re
import threading
import time
from flask import current_app, jsonify, request, session

from . import campaign as cmc
from . import grid_create as gc
from .create_set_units import is_units_exhausted as _is_units_exhausted
from .direct_repository import victory_conn as _victory_conn
from .yandex_gateway import (
    LIVE_V4_URL as _LIVE_V4, direct_tokens as _direct_tokens,
    token_for_login as _token_for_login, v5_get as _v5_get, v5_call as _v5_call,
    v5_err as _v5_err, grid_list_campaigns as _grid_list_campaigns,
    block_bootstrap as _block_bootstrap, block_check as _block_check,
    grid_csrf as _grid_csrf, resolve_agency_hint as _resolve_agency_hint,
    GRID_URL as _GRID_URL,
)


def _missing(*_args, **_kwargs):
    raise RuntimeError("account_service dependency is not configured")


# ── Дефолтный per-process pull-lock (анти-блок аккаунта) ────────────────────────
# Самодостаточная реализация «одна тяжёлая выгрузка за раз + кулдаун», зеркало
# automation_runtime._pull_begin/_pull_end/_busy_response. Нужна процессам, которые
# импортируют account_service, но НЕ проходят полный automation_runtime.configure()
# (direct-content: только balance + check_blocks). В direct-create эти три ключа
# перезаписываются через configure() → там используется единый лок движка (без изменений).
_PULL_LOCK = threading.Lock()
_PULL_LAST: dict = {}                       # ключ действия → monotonic время последнего запуска
_PULL_OWNER: dict = {"key": None, "since": 0.0}


def _default_pull_begin(key: str, cooldown: float) -> tuple[bool, str, int]:
    """Захватить право на выгрузку. (ok, reason, wait_sec).
    reason: '' | 'cooldown' (рано повторять) | 'busy' (идёт другая выгрузка)."""
    now = time.monotonic()
    wait = cooldown - (now - _PULL_LAST.get(key, 0.0))
    if wait > 0:
        return False, "cooldown", int(wait) + 1
    if not _PULL_LOCK.acquire(blocking=False):
        return False, "busy", int(now - _PULL_OWNER.get("since", now))
    _PULL_OWNER["key"] = key
    _PULL_OWNER["since"] = now
    return True, "", 0


def _default_pull_end(key: str) -> None:
    """Освободить лок и отметить время (вызывать ТОЛЬКО если _pull_begin вернул ok)."""
    _PULL_LAST[key] = time.monotonic()
    _PULL_OWNER["key"] = None
    try:
        _PULL_LOCK.release()
    except RuntimeError:
        pass


def _default_busy_response(reason: str, wait: int):
    if reason == "cooldown":
        msg = f"Подождите ещё ~{wait} c перед повторной выгрузкой (защита аккаунта от блокировки)."
    else:
        msg = "Сейчас уже идёт выгрузка (возможно, в другой вкладке). Дождитесь её завершения."
    return jsonify({"error": msg, "locked": True, "reason": reason, "wait": wait}), 429


_pull_begin, _pull_end, _busy_response = _default_pull_begin, _default_pull_end, _default_busy_response
_global_feed_rules = _filter_allowed_feed_rows = _grid_feeds = _missing
_account_ctx = _metrika_goals_for = _goal_vse_formy = _missing
_load_corrections = _corrections_by_segment = _missing
_bump_job = _job_db_progress = _job_db_save = _job_new = _create_jobs_ahead = _missing
_ensure_create_worker = _missing
_CREATE_JOBS_LOCK = _missing
_COOLDOWN = {"balance": 60.0, "assets": 20.0}
_TOOL_CAMPAIGN_RE = re.compile(r"^\s*tp\d+_(cpc|cpa)_(site|kviz)[\s_—–]", re.IGNORECASE)


def configure(deps: dict) -> None:
    globals().update(deps)


def _is_tool_campaign(name: str | None) -> bool:
    """True, если имя кампании похоже на созданное этим сервисом (кодер tpN_{cpc|cpa}_{site|kviz}_…)."""
    return bool(_TOOL_CAMPAIGN_RE.match(str(name or "")))

def _grid_delete_one(login: str, cid: int) -> bool:
    """Удалить одну кампанию через Grid deleteCampaigns (cookie). → True если удалена.
    Используется для GdPostCampaign (Посевы tp8/tp9/tp10) — невидимы в v5 и не-UAC.
    """
    try:
        cid = int(cid)  # защита от строкового cid: "712972696" in {712972696} → False без приведения
        res = gc.GridCreateClient(login).delete_campaigns([cid])
        return cid in {int(x) for x in (res.get("deleted") or [])}
    except Exception:  # noqa: BLE001
        return False


def _grid_draft_contains(login: str, cid: int) -> bool:
    """True если cid ещё виден в Grid-черновиках (fallback-проверка после delete).

    Используется как fail-safe: если _grid_delete_one вернул False — проверяем,
    действительно ли кампания осталась, или просто выпала из Grid-листа быстрее delete.
    При ошибке чтения возвращаем True (fail-safe: считаем «ещё есть»).
    """
    try:
        return any(
            int(c["id"]) == cid
            for c in _grid_list_campaigns(login, only_draft=True)
            if c.get("id")
        )
    except Exception:  # noqa: BLE001
        return True  # fail-safe


def _delete_drafts_core(login: str, agency: str, job: dict | None = None) -> dict:
    """Ядро удаления черновиков (DRAFT) аккаунта, СОЗДАННЫХ ЭТИМ МОДУЛЕМ (по кодеру в имени).
    Чужие/ручные DRAFT-кампании НЕ трогаются (фильтр _is_tool_campaign) — защита от сноса чужого.
    Используется и синхронным эндпоинтом,
    и воркером общей очереди (job ≠ None → прогресс done/created в карточке очереди).

    DRAFT-кампании делятся на три слоя:
    - ЕПК (tp1–tp5, UNIFIED_CAMPAIGN через v5): видны в v5 с State=OFF + Status=DRAFT → v5 delete.
    - UAC (tp6 Мастер, tp7 Товарка): НЕВИДИМЫ в v5; список даёт Grid (_grid_list_campaigns),
      удаляем через DELETE /web-api/uac/campaign/{id}/ (DRAFT удаляется напрямую).
    - Посевы (tp8/tp9/tp10, GdPostCampaign): НЕВИДИМЫ в v5 и не-UAC; список даёт Grid,
      удаляем через Grid deleteCampaigns (cookie, без баллов v5).
    НЕОБРАТИМО. → {ok, deleted, by_v5, by_uac, by_cookie, errors, created, failed, kind}."""
    token, ag = _token_for_login(login, agency, _direct_tokens())
    if not token:
        return {"ok": False, "error": "нет рабочего агентского токена для этого логина",
                "created": 0, "failed": 1, "deleted": 0, "kind": "delete_drafts"}

    deleted, by_v5, by_uac, by_cookie, errors = 0, 0, 0, 0, []
    skipped_foreign = 0                                   # чужие/ручные DRAFT — НЕ наши, не удаляем
    v5_done: set = set()                                  # id, удалённые через v5 (чтобы слой 2 их не трогал)
    epk_cookie: list = []                                 # ЕПК-черновики на удаление ПО КУКЕ (v5 не смог: 152)
    done = 0

    def _adv(ok: bool):                                   # прогресс джобы: created/failed + done
        nonlocal done
        done += 1
        _bump_job(job, ok)
        if job is not None:
            job["done"] = done
            _job_db_progress(job)

    # --- Слой 1 (сбор): ЕПК через v5 (State=OFF + Status=DRAFT, не UNIFIED) ---
    # v5-чтение стоит баллов: при 152 (исчерпан лимит) НЕ падаем — добираем список по куке (Grid)
    # и дальше удаляем тоже по куке (молча). Пользователю об этом не сообщаем — только финал.
    draft_v5: list = []
    _units_out = False
    jg = _v5_call("campaigns", "get", token, login,
                  {"SelectionCriteria": {"States": ["OFF"]},
                   "FieldNames": ["Id", "Name", "Type", "Status"]})
    if "error" in jg:
        if _is_units_exhausted(_v5_err(jg)):
            _units_out = True                             # баллы кончились → весь путь уходит на куки (молча)
        else:
            errors.append("v5 get: " + _v5_err(jg))
    else:
        _all_v5 = [c for c in (jg.get("result") or {}).get("Campaigns", [])
                   if c.get("Status") == "DRAFT" and c.get("Type") != "UNIFIED_CAMPAIGN"]
        draft_v5 = [c["Id"] for c in _all_v5 if _is_tool_campaign(c.get("Name"))]
        skipped_foreign += len(_all_v5) - len(draft_v5)   # чужие ручные черновики — не трогаем

    # --- Слой 2 (сбор): UAC/ЕПК-черновики через Grid (видит скрытые от v5 Мастер/Товарка) ---
    grid_drafts: list = []
    try:
        _all_grid = [c for c in _grid_list_campaigns(login, only_draft=True)
                     if c.get("id") and int(c["id"]) not in set(draft_v5)]
        grid_drafts = [c for c in _all_grid if _is_tool_campaign(c.get("name"))]
        skipped_foreign += len(_all_grid) - len(grid_drafts)   # чужие → не трогаем
    except Exception as e:  # noqa: BLE001
        errors.append(f"Grid-список недоступен: {str(e)[:90]}")

    # total известен ДО удаления — карточка очереди сразу показывает «обработка набора 0/N»
    if job is not None:
        job["total"] = len(draft_v5) + len(grid_drafts)
        _job_db_progress(job)

    # --- Слой 1 (удаление): пачками по 100 (v5; при 152 чанк уходит на куки) ---
    for i in range(0, len(draft_v5), 100):
        if job is not None and job.get("cancel"):
            break
        chunk = draft_v5[i:i + 100]
        jd = _v5_call("campaigns", "delete", token, login, {"SelectionCriteria": {"Ids": chunk}})
        if "error" in jd:
            if _is_units_exhausted(_v5_err(jd)):
                _units_out = True; epk_cookie.extend(chunk)   # 152 → удалим по куке ниже (молча)
            else:
                errors.append("v5 delete: " + _v5_err(jd))
                for _ in chunk:
                    _adv(False)
            continue
        for rr in (jd.get("result") or {}).get("DeleteResults", []):
            if rr.get("Id") and not rr.get("Errors"):
                deleted += 1; by_v5 += 1; v5_done.add(rr["Id"]); _adv(True)
            else:
                errors.append(str(rr.get("Errors"))[:120]); _adv(False)

    # --- Слой 2 (удаление): роутинг по типу (ЕПК → v5, при 152 → куки; UAC → uac.delete по куке) ---
    uac = None
    for c in grid_drafts:
        if job is not None and job.get("cancel"):
            break
        cid = int(c["id"])
        if cid in v5_done:
            continue
        tn = c.get("typename") or ""
        try:
            if tn == "GdUnifiedCampaign":                # ЕПК — через v5/v501 (при 152 → копим на куки)
                if _units_out:                           # баллы уже кончились → сразу по куке (не тратим вызов)
                    epk_cookie.append(cid); continue
                jd = _v5_call("campaigns", "delete", token, login, {"SelectionCriteria": {"Ids": [cid]}})
                if "error" in jd and _is_units_exhausted(_v5_err(jd)):
                    _units_out = True; epk_cookie.append(cid); continue   # 152 → на куки (молча)
                rr = ((jd.get("result") or {}).get("DeleteResults") or [{}])[0]
                if rr.get("Id") and not rr.get("Errors"):
                    deleted += 1; by_v5 += 1; _adv(True)
                elif _is_units_exhausted(str(rr.get("Errors"))):
                    epk_cookie.append(cid)               # per-id 152 → на куки (молча)
                else:
                    errors.append(f"ЕПК delete {cid}: {(_v5_err(jd) if 'error' in jd else rr.get('Errors'))}"[:120])
                    _adv(False)
            elif tn == "GdPostCampaign":                 # Посевы tp8/tp9/tp10 — Grid cookie deleteCampaigns
                ok = _grid_delete_one(login, cid)
                if not ok and _grid_draft_contains(login, cid):
                    errors.append(f"Post delete {cid}: не удалён"); _adv(False)
                    continue
                deleted += 1; by_cookie += 1; _adv(True)
            else:                                        # UAC Мастер/Товарка — приватный uac/campaign/{id} (по куке)
                if uac is None:
                    uac = cmc.build_client(login, account=(ag or None))
                    uac.link_info("https://ya.ru")       # bootstrap CSRF
                uac.delete_campaign(str(cid))
                deleted += 1; by_uac += 1; _adv(True)
        except Exception as e:  # noqa: BLE001
            errors.append(f"delete {cid}: {str(e)[:80]}"); _adv(False)

    # --- Слой 3 (фолбэк по куке): ЕПК-черновики, которые v5 не смог удалить из-за 152 (нет баллов).
    # Удаляем через Grid deleteCampaigns на куке агентства — без баллов, молча. Сообщаем только финал.
    if epk_cookie and not (job is not None and job.get("cancel")):
        try:
            cl = gc.GridCreateClient(login)              # сам подберёт рабочую куку агентства для login
            for i in range(0, len(epk_cookie), 100):
                if job is not None and job.get("cancel"):
                    break
                chunk = epk_cookie[i:i + 100]
                res = cl.delete_campaigns(chunk)
                ok_ids = set(res.get("deleted") or [])
                for cid in chunk:
                    if cid in ok_ids:
                        deleted += 1; by_cookie += 1; _adv(True)
                    else:
                        errors.append(f"куки delete {cid}: не удалён"); _adv(False)
        except Exception as e:  # noqa: BLE001
            for cid in epk_cookie:
                errors.append(f"куки delete {cid}: {str(e)[:70]}"); _adv(False)

    return {"ok": True, "deleted": deleted, "by_v5": by_v5, "by_uac": by_uac, "by_cookie": by_cookie,
            "errors": errors[:5], "created": deleted, "failed": len(errors),
            "skipped_foreign": skipped_foreign,          # чужие/ручные черновики — пропущены (не наши)
            "kind": "delete_drafts"}

def _grid_empty_unified_drafts(login: str, agency: str) -> list:
    """ЕПК-черновики (GdUnifiedCampaign) с 0 групп = ПУСТЫШКИ (кампания создалась, сборка не дошла —
    напр. рестарт убил процесс на середине). Только НАШИ (имя с 'tp'). UAC (tp6/tp7) НЕ трогаем —
    у них 0 grid-групп штатно (структура через UAC, не adGroups). → [campaign_id, ...]."""
    import requests as _rqs
    try:
        drafts = [c for c in _grid_list_campaigns(login, only_draft=True)
                  if c.get("typename") == "GdUnifiedCampaign"
                  and str(c.get("name") or "").strip().lower().startswith("tp") and c.get("id")]
    except Exception:  # noqa: BLE001
        return []
    if not drafts:
        return []
    ids = [str(c["id"]) for c in drafts]
    try:
        cookie = cmc.load_cookie(agency)
    except Exception:  # noqa: BLE001
        cookie = None
    if not cookie:
        return []
    csrf = _block_bootstrap(cookie, agency)
    h = {"Cookie": cookie, "dna-operation-name": "AG", "x-direct-api": "1", "x-detected-locale": "ru",
         "Content-Type": "application/json", "User-Agent": cmc.USER_AGENT}
    if csrf:
        h["x-csrf-token"] = csrf
    AG = ("query AG($login:String!,$inp:GdAdGroupsContainerInput!){client(searchBy:{login:$login}){"
          "adGroups(input:$inp){rowset{id campaignId}}}}")
    inp = {"filter": {"campaignIdIn": ids},
           "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
           "limitOffset": {"limit": 5000, "offset": 0}, "orderBy": [{"order": "ASC", "field": "ID"}]}
    try:
        r = _rqs.post(f"{_GRID_URL}?operationName=AG&ulogin={login}",
                      json={"operationName": "AG", "query": AG, "variables": {"login": login, "inp": inp}},
                      headers=h, timeout=40, verify=False)
        if r.status_code == 403:
            c2 = _grid_csrf(r)
            if c2:
                h["x-csrf-token"] = c2
                r = _rqs.post(f"{_GRID_URL}?operationName=AG&ulogin={login}",
                              json={"operationName": "AG", "query": AG, "variables": {"login": login, "inp": inp}},
                              headers=h, timeout=40, verify=False)
        d = r.json()
        ags = (((d.get("data") or {}).get("client") or {}).get("adGroups") or {}).get("rowset") or []
    except Exception:  # noqa: BLE001
        return []
    have = {str(a.get("campaignId")) for a in ags}
    return [int(i) for i in ids if i not in have]   # нет ни одной группы → пустышка

def _sweep_empty_drafts(login: str, agency: str = "") -> int:
    """Авто-очистка: удалить пустые ЕПК-черновики (0 групп) аккаунта по куке. → число удалённых.
    Безопасно ТОЛЬКО когда нет активного создания (вызывать при старте после рестарта)."""
    ag = agency or _resolve_agency_hint(login, "") or ""
    empties = _grid_empty_unified_drafts(login, ag)
    if not empties:
        return 0
    try:
        res = gc.GridCreateClient(login).delete_campaigns(empties)
        return len(res.get("deleted") or [])
    except Exception:  # noqa: BLE001
        return 0

def _delete_partial_campaign(token: str, login: str, campaign_id: int | str | None) -> bool:
    """Удалить один недособранный черновик: v5 сначала, Grid-cookie как фолбэк при лимитах/типах."""
    if not campaign_id:
        return False
    try:
        cmc.DirectV501Client(token, login).delete_campaigns([int(campaign_id)])
        return True
    except Exception:  # noqa: BLE001
        try:
            deleted = gc.GridCreateClient(login).delete_campaigns([campaign_id]).get("deleted") or []
            return int(campaign_id) in {int(x) for x in deleted}
        except Exception:  # noqa: BLE001
            return False

def _delete_drafts_response():
    """Синхронное удаление черновиков (обратная совместимость). Тело: {login, agency}."""
    body = request.json or {}
    login = (body.get("login") or "").strip()
    agency = (body.get("agency") or "").strip()
    if not login:
        return jsonify({"error": "login обязателен"}), 400
    ok, reason, wait = _pull_begin(f"deldrafts:{login}", 20.0)
    if not ok:
        return _busy_response(reason, wait)
    try:
        return jsonify(_delete_drafts_core(login, agency))
    finally:
        _pull_end(f"deldrafts:{login}")

def _delete_drafts_async_response():
    """Удаление черновиков ФОНОВОЙ джобой в ОБЩЕЙ очереди создания (та же карточка, что и создание РК).
    Возврат {job_id} сразу; прогресс — через /api/create_set_status. Тело: {login, agency}."""
    body = dict(request.json or {})
    login = (body.get("login") or "").strip()
    if not login:
        return jsonify({"error": "login обязателен"}), 400
    body["_kind"] = "delete_drafts"                       # маркер для воркера (ветка удаления)
    resolved_ag = _resolve_agency_hint(login, (body.get("agency") or "").strip())
    if resolved_ag:
        body["agency"] = resolved_ag                     # ключ партиционирования очереди (как у создания)
    app = current_app._get_current_object()
    _ensure_create_worker(app)
    saved_session = dict(session)
    job_id = _job_new(0, login, body, saved_session)     # total уточнит воркер после подсчёта черновиков
    with _CREATE_JOBS_LOCK:
        ahead = _create_jobs_ahead(job_id)
    return jsonify({"job_id": job_id, "total": 0, "login": login, "ahead": ahead})


_ACCOUNT_COLS = ["domain", "salon", "city", "site_type", "login_key", "counter_number",
                 "client_id", "agency_account", "directologist", "status"]
DEFAULT_STATUS = "Контекст активно"
# Директологи-исключения (агентства/субподряд — не нужны в таблице)
_EXCLUDE_DIRECTOLOGS = ["Аксиома", "О-Лидер", "Медиа-Актив", "Ниндзя Илья"]

_TOKEN_ONLY_TYPES = {"search_test", "search_dynamic"}

_STATE_ORDER = {"ON": 0, "SUSPENDED": 1, "OFF": 2, "ENDED": 3, "CONVERTED": 4, "ARCHIVED": 5}

_KNOWN_AGENCIES = ["victorylotsofads1", "victoryagency-direct1618440", "victoryagency14", "y-direct-victory", "victoryagencydirect", "useful-call-agency"]

def _do_balance(_rqs, ThreadPoolExecutor, as_completed):
    pairs = (request.json or {}).get("pairs") or []
    by_agency: dict[str, list[str]] = {}
    for p in pairs:
        lg = (p.get("login") or "").strip()
        ag = (p.get("agency") or "").strip()
        if lg and ag and ag != "None":
            by_agency.setdefault(ag, []).append(lg)

    tokens = _direct_tokens()
    balances: dict = {}

    def _fetch(tok: str, chunk: list[str], out: dict) -> None:
        """AccountManagement.Get с дроблением: один битый логин роняет весь батч (501),
        поэтому при ошибке делим пополам и изолируем плохой логин."""
        if not chunk:
            return
        body = {"method": "AccountManagement", "token": tok,
                "param": {"Action": "Get", "SelectionCriteria": {"Logins": chunk}}}
        try:
            j = _rqs.post(_LIVE_V4, json=body, timeout=30).json()
        except Exception:  # noqa: BLE001
            j = {"error_code": "net"}
        accs = (j.get("data") or {}).get("Accounts")
        if accs is not None and not j.get("error_code"):
            for acc in accs:
                out[acc.get("Login")] = round(float(acc.get("Amount") or 0), 2)
            return
        if len(chunk) == 1:           # одиночный битый логин — пропускаем
            return
        mid = len(chunk) // 2
        _fetch(tok, chunk[:mid], out)
        _fetch(tok, chunk[mid:], out)

    def _batch(ag: str, logins: list[str]) -> dict:
        tok = tokens.get(ag)
        if not tok:
            return {}
        out: dict = {}
        for i in range(0, len(logins), 50):            # начальные батчи по 50
            _fetch(tok, logins[i:i + 50], out)
        return out

    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(_batch, ag, lgs): ag for ag, lgs in by_agency.items()}
        for f in as_completed(futs):
            balances.update(f.result())

    # Фолбэк: колонка agency_account в БД бывает устаревшей/None — логин реально
    # управляется ДРУГИМ агентством, и AccountManagement.Get под записанным
    # токеном его не вернёт. Добираем недостающие перебором всех токенов
    # (тот же приём, что в проверке блокировок). Баланс 0 ₽ не дёргаем повторно.
    all_logins = []
    for p in pairs:
        lg = (p.get("login") or "").strip()
        if lg:
            all_logins.append(lg)
    missing = [lg for lg in all_logins if balances.get(lg) is None]
    if missing:
        for tok in tokens.values():
            if not missing:
                break
            out: dict = {}
            for i in range(0, len(missing), 50):
                _fetch(tok, missing[i:i + 50], out)
            balances.update({k: v for k, v in out.items() if v is not None})
            missing = [lg for lg in missing if balances.get(lg) is None]

    # логины без ответа → null
    for lg in all_logins:
        balances.setdefault(lg, None)
    return jsonify({"balances": balances})

def _preflight_creds(login: str, agency_hint: str, need_cookie: bool) -> dict:
    """ПРЕДПОЛЁТНАЯ проверка кредов ДО создания РК — «какой токен/куку реально использовать».

    Делает лёгкие read-only вызовы (с таймаутами: v5 GET 30c, grid 40c), чтобы при битых/
    протухших кредах упасть БЫСТРО и ЯВНО, а не уйти в тихий висяк на пути создания:
      1) токен агентства, реально открывающий ``login`` (через ``_token_for_login`` — внутри
         проба ``campaigns.get(Id)``; перебор всех агентских токенов с persist находки);
      2) если набор содержит grid/UAC-типы (tp5/tp6/tp7) — self-probe куки агентства в grid.

    Возвращает ``{ok, token, agency, cookie, error}``. Кука нужна только при ``need_cookie``;
    для чисто токенных наборов (tp1/tp2/tp3/tp4) мёртвая кука НЕ блокирует."""
    tokens = _direct_tokens()
    if not tokens:
        return {"ok": False, "token": None, "agency": None, "cookie": None,
                "error": "нет агентских токенов (loader.load_yandex_direct вернул пусто)"}
    requested_agency = (agency_hint or "").strip()
    # Для create/deferred job agency_hint — это не просто подсказка для OAuth-токена, а ключ
    # очереди и cookie-аккаунт Grid/UAC. `_token_for_login()` может вернуть другой token-owner из
    # кэша/перебора токенов (например, если один OAuth открывает клиента), но Grid-cookie при этом
    # должна проверяться строго под agency из job. Иначе deferred сохраняется с чужим agency и
    # докрутка потом идёт через неправильную куку: 403 "Нет прав" / "создано 0".
    if need_cookie and requested_agency:
        cookie = None
        try:
            # Explicit job agency is a hard routing decision for Grid/UAC. Do not accept
            # a stale per-process/gateway cache here: preflight must reflect the current
            # Glavpotok session and current rights for this exact ulogin.
            cookie = cmc.pick_working_cookie(login, accounts=(requested_agency,), force_refresh=True)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "token": None, "agency": requested_agency, "cookie": None,
                    "error": (f"кука агентства {requested_agency} не прошла UAC/linkinfo "
                              f"для {login}: {str(e)[:140]}")}
        if not cookie:
            return {"ok": False, "token": None, "agency": requested_agency, "cookie": None,
                    "error": f"нет куки агентства {requested_agency} — grid/uac-типы создать нельзя"}
        if _block_bootstrap(cookie, requested_agency) is None:
            return {"ok": False, "token": None, "agency": requested_agency, "cookie": cookie,
                    "error": (f"кука агентства {requested_agency} не отвечает в grid "
                              f"(протухла/нет доступа) — обновите куки; grid/uac-типы создать нельзя")}
        cmc.remember_working_cookie(login, cookie)
        token, _token_agency = _token_for_login(login, requested_agency, tokens)
        return {"ok": True, "token": token or "", "agency": requested_agency, "cookie": cookie,
                "cookie_only": not bool(token), "error": None}

    token, agency = _token_for_login(login, agency_hint, tokens)
    if not token:
        # Нет рабочего токена (error 53 / аккаунт porg-* без агентского OAuth) — пробуем
        # cookie-only-путь: Grid/UAC создаёт РК и ставит цены без API-баллов (token="" в builders).
        # Fallback только при need_cookie=True; token-only типы (search_test/dynamic) — без fallback.
        if need_cookie:
            try:
                _fb_cookie = cmc.pick_working_cookie(login)
            except Exception:  # noqa: BLE001
                _fb_cookie = None
            if _fb_cookie:
                cmc.remember_working_cookie(login, _fb_cookie)
                return {"ok": True, "token": "", "agency": None, "cookie": _fb_cookie,
                        "cookie_only": True, "error": None}
        return {"ok": False, "token": None, "agency": None, "cookie": None,
                "error": (f"ни один агентский токен не открывает аккаунт {login} — проверьте "
                          f"доступ агентства к клиенту и актуальность OAuth-токенов")}
    cookie = None
    if need_cookie:
        try:
            cookie = cmc.pick_working_cookie(login, accounts=(agency,))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "token": token, "agency": agency, "cookie": None,
                    "error": f"кука агентства {agency} не загрузилась: {str(e)[:140]}"}
        if not cookie:
            return {"ok": False, "token": token, "agency": agency, "cookie": None,
                    "error": f"нет куки агентства {agency} — grid/uac-типы создать нельзя"}
        if _block_bootstrap(cookie, agency) is None:     # None = кука мертва/нет ответа grid
            return {"ok": False, "token": token, "agency": agency, "cookie": cookie,
                    "error": (f"кука агентства {agency} не отвечает в grid (протухла/нет доступа) — "
                              f"обновите куки; grid/uac-типы создать нельзя")}
        # ВАЖНО: downstream Grid/UAC-клиенты ниже по create-path вызывают pick_working_cookie(login)
        # без знания конкретной агентской куки из preflight. Если не запомнить уже проверенную куку,
        # они могут взять другую/битую и словить HTML Login вместо JSON на addShoppingAds/finalize.
        cmc.remember_working_cookie(login, cookie)
    return {"ok": True, "token": token, "agency": agency, "cookie": cookie, "error": None}

def _account_assets_response():
    """Что РЕАЛЬНО заведено на аккаунте (живьём, офиц. v5): фиды / аудитории / промоакции.

    ?login=<login>&agency=<agency_account>. Ответ:
      {feeds:[{id,name,business_type,source_type}], audiences:[{id,name,type,scope}],
       promos:[{id,name,type,description,amount,unit,prefix,promocode,href,start,end}], errors:{}}.
    """
    login = (request.args.get("login") or "").strip()
    agency = (request.args.get("agency") or "").strip()
    if not login:
        return jsonify({"error": "login обязателен"}), 400

    ok, reason, wait = _pull_begin(f"assets:{login}", _COOLDOWN["assets"])
    if not ok:
        return _busy_response(reason, wait)
    try:
        return _do_assets(login, agency)
    finally:
        _pull_end(f"assets:{login}")

def _do_assets(login: str, agency: str):
    tokens = _direct_tokens()
    token, agency_used = _token_for_login(login, agency, tokens)
    out: dict = {"login": login, "agency": agency_used, "feeds": [], "audiences": [], "promos": [], "errors": {}}
    if not token:
        out["errors"]["all"] = "нет рабочего агентского токена для этого логина"
        return jsonify(out)

    jf = _v5_get("feeds", token, login, ["Id", "Name", "BusinessType", "SourceType"])
    if "error" in jf:
        out["errors"]["feeds"] = jf["error"].get("error_string")
    else:
        raw_feeds = (jf.get("result") or {}).get("Feeds", [])
        out["feeds"] = [{"id": f["Id"], "name": f.get("Name"), "business_type": f.get("BusinessType"),
                         "source_type": f.get("SourceType")} for f in raw_feeds]
        # Количество разрешённых URL-фидов для предпланового бейджа tp3/tp5/tp7 (fan-out по фидам).
        # v5 feeds.get НЕ отдаёт URL фида → матч в _filter_allowed_feed_rows идёт по Name. Если фид в
        # кабинете назван ЯРЛЫКОМ (не URL), матч по имени даёт ЛОЖНЫЙ 0 (напр. porg-zv6tyvg4). Реальное
        # создание (create_set_plan.py:385-393) при пустом v5-матче фолбэчит на Grid (читает настоящие URL
        # по куке, без баллов) — зеркалим ту же логику здесь, чтобы счётчик совпадал с созданием.
        _allowed = len(_filter_allowed_feed_rows(raw_feeds))
        if _allowed == 0:
            try:
                _allowed = len(_filter_allowed_feed_rows(_grid_feeds(login, agency_used or "")))
            except Exception:  # noqa: BLE001 — Grid может вернуть пусто/упасть → честный 0
                _allowed = 0
        out["allowed_feeds_count"] = _allowed

    ja = _v5_get("retargetinglists", token, login, ["Id", "Name", "Type", "Scope"], criteria={})
    if "error" in ja:
        out["errors"]["audiences"] = ja["error"].get("error_string")
    else:
        # только раздел RETARGETING (исключаем AUDIENCE «Интересы и привычки» и пр.)
        out["audiences"] = [{"id": a["Id"], "name": a.get("Name"), "type": a.get("Type"),
                             "scope": a.get("Scope")}
                            for a in (ja.get("result") or {}).get("RetargetingLists", [])
                            if a.get("Type") == "RETARGETING"]

    jp = _v5_get("promotions", token, login,
                 ["Id", "Type", "Name", "Description", "Amount", "AmountPrefix", "AmountUnit",
                  "Promocode", "Href", "StartDate", "EndDate"], criteria={})
    if "error" in jp:
        out["errors"]["promos"] = jp["error"].get("error_string")
    else:
        out["promos"] = [{"id": p["Id"], "name": p.get("Name"), "type": p.get("Type"),
                          "description": p.get("Description"), "amount": p.get("Amount"),
                          "unit": p.get("AmountUnit"), "prefix": p.get("AmountPrefix"),
                          "promocode": p.get("Promocode"), "href": p.get("Href"),
                          "start": p.get("StartDate"), "end": p.get("EndDate")}
                         for p in (jp.get("result") or {}).get("Promotions", [])]
    return jsonify(out)

def _account_audiences_response():
    """Аудитории типа RETARGETING (пригодные для корректировок ставок) на аккаунте.

    ?login=<login>&agency=<agency_account>
    Ответ: {"audiences":[{"id":<int>,"name":<str>}], "error":<str, опционально>}.
    Фильтр: Type==RETARGETING И Scope==FOR_TARGETS_AND_ADJUSTMENTS (списки AUDIENCE/
    FOR_TARGETS_ONLY корректировку bidmodifiers НЕ принимают).
    """
    login = (request.args.get("login") or "").strip()
    agency = (request.args.get("agency") or "").strip()
    if not login:
        return jsonify({"audiences": [], "error": "login обязателен"}), 400

    tokens = _direct_tokens()
    token, _ = _token_for_login(login, agency, tokens)
    if not token:
        return jsonify({"audiences": [], "error": "нет рабочего агентского токена для этого логина"})

    ja = _v5_get("retargetinglists", token, login, ["Id", "Name", "Type", "Scope"], criteria={})
    if "error" in ja:
        err_str = (ja.get("error") or {}).get("error_string") or str(ja.get("error"))
        return jsonify({"audiences": [], "error": err_str})

    audiences = [
        {"id": a["Id"], "name": a.get("Name")}
        for a in (ja.get("result") or {}).get("RetargetingLists", [])
        if a.get("Type") == "RETARGETING" and a.get("Scope") == "FOR_TARGETS_AND_ADJUSTMENTS"
    ]
    # Процент корректировки для каждой аудитории берём из «Глобальных правил» по городу
    # аккаунта (матчинг geo_X→<город>, self_X→self). Нет правила → adj=None (фронт ставит дефолт).
    ctx = _account_ctx(login)
    corr = _load_corrections((ctx or {}).get("city") or "*")
    seg_pct = _corrections_by_segment(corr.get("audiences", []), [a.get("name") or "" for a in audiences])
    for a in audiences:
        a["adj"] = seg_pct.get(a.get("name") or "")   # int% из правил (с кросс-кл. фолбэком), либо None
    return jsonify({"audiences": audiences})

def _account_prefill_response():
    """Значения для формы по логину: href/тип сайта/регион/счётчик/цель «Все формы»/тексты из БД."""
    import psycopg2.extras
    login = (request.args.get("login") or "").strip()
    if not login:
        return jsonify({"error": "login обязателен"}), 400

    conn = _victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT domain, city, region, site_type, counter_number, agency_account "
                    "FROM public.local_gsheet_sites WHERE login_key=%s AND direction='Авто' LIMIT 1", (login,))
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"error": f"аккаунт {login} не найден в local_gsheet_sites (Авто)"}), 404

    warnings: list[str] = []
    domain = (row["domain"] or "").strip()
    site_type = (row["site_type"] or "").strip()
    cc = (row["counter_number"] or "").strip()
    counter_id = int(cc) if cc.isdigit() else None

    # Счётчик/цель из metrika_goals (Victory): если в таблице сайтов счётчик не
    # заполнен — берём counter_ids; цель goal_id — из all_forms этой же таблицы.
    mg = _metrika_goals_for(login)
    counter_options = mg["counters"] if mg else []
    if not counter_id and counter_options:
        counter_id = counter_options[0]
    if not counter_id:
        warnings.append("счётчик Метрики не найден ни в таблице, ни в metrika_goals")

    # Резолвим geoid ОБЛАСТИ (не города): city → Область через БД → geoid словаря Директа.
    # Та же логика что _account_ctx (create_set_context.py); для мультигород-аккаунтов → 225.
    acc_ctx = _account_ctx(login) or {}
    region_id = acc_ctx.get("geoid") or 225
    region_used = acc_ctx.get("oblast") or ("Россия" if region_id == 225 else None)
    if region_id == 225 and row.get("city"):
        warnings.append("регион не распознан по городу — поставил Россия (225)")

    # goal_id: приоритет — all_forms из metrika_goals; иначе цель «Все формы» из API Метрики
    goal_id = mg["goal_id"] if mg else None
    goal_name = "Все формы" if goal_id else None
    if not goal_id and counter_id:
        goal_id, goal_name = _goal_vse_formy(counter_id)
    if counter_id and not goal_id:
        warnings.append("цель «Все формы» не найдена (нет в metrika_goals и в счётчике)")

    titles: list[str] = []
    texts: list[str] = []
    if site_type:
        c2 = _victory_conn()
        try:
            cur = c2.cursor()
            # site_type здесь — строка из записи аккаунта (local_gsheet_sites), всегда базовый тип;
            # split-вкладки («Монобренд · Lada») не пишутся в accounts и не хранятся в ad_templates.
            cur.execute("SELECT kind, content FROM public.direct_ad_templates "
                        "WHERE enabled AND site_type=%s ORDER BY kind, id", (site_type,))
            for kind, content in cur.fetchall():
                (titles if kind == "title" else texts).append(content)
        finally:
            c2.close()
    if site_type and not titles and not texts:
        warnings.append(f"нет шаблонных текстов для типа сайта «{site_type}»")

    # Правила РК по (site_type, city аккаунта) с фолбэком на (site_type, '*')
    rule_goal_type = rule_cpa = rule_budget = rule_adjustment_pct = None
    acc_city = (row.get("city") or "").strip()
    if site_type:
        c3 = _victory_conn()
        try:
            cur = c3.cursor()
            r_rule = None
            # site_type здесь — из записи аккаунта (базовый тип), нормализация не нужна:
            # direct_automation_rules хранит только базовые типы без split-суффикса.
            # Приоритет: правило для конкретного города аккаунта
            if acc_city:
                cur.execute("SELECT goal_type, cpa::numeric, budget::numeric, adjustment_pct "
                            "FROM public.direct_automation_rules "
                            "WHERE site_type=%s AND city=%s LIMIT 1", (site_type, acc_city))
                r_rule = cur.fetchone()
            # Фолбэк: дефолтное правило (city='*')
            if not r_rule:
                cur.execute("SELECT goal_type, cpa::numeric, budget::numeric, adjustment_pct "
                            "FROM public.direct_automation_rules "
                            "WHERE site_type=%s AND city='*' LIMIT 1", (site_type,))
                r_rule = cur.fetchone()
            if r_rule:
                rule_goal_type = r_rule[0]
                rule_cpa = float(r_rule[1])
                rule_budget = float(r_rule[2])
                rule_adjustment_pct = int(r_rule[3])
        except Exception:  # noqa: BLE001  — таблица может отсутствовать в dev-окружении
            pass
        finally:
            c3.close()

    resp: dict = {
        "login": login, "domain": domain, "href": ("https://" + domain) if domain else "",
        "site_type": site_type, "city": row.get("city"), "region": row.get("region"),
        "region_id": region_id, "region_used": region_used,
        "counter_id": counter_id, "counter_options": counter_options,
        "goal_id": goal_id, "goal_name": goal_name,
        "titles": titles, "texts": texts, "agency": row.get("agency_account"), "warnings": warnings,
    }
    if rule_goal_type is not None:
        resp["rule_goal_type"] = rule_goal_type
        resp["rule_cpa"] = rule_cpa
        resp["rule_budget"] = rule_budget
        resp["rule_adjustment_pct"] = rule_adjustment_pct
    return jsonify(resp)

def _campaigns_response():
    """Кампании аккаунта (офиц. v5 campaigns.get): id + название + статус."""
    login = (request.args.get("login") or "").strip()
    agency = (request.args.get("agency") or "").strip()
    if not login:
        return jsonify({"error": "login обязателен"}), 400
    token, ag = _token_for_login(login, agency, _direct_tokens())
    if not token:
        return jsonify({"error": "нет рабочего агентского токена для этого логина", "campaigns": []})
    j = _v5_get("campaigns", token, login, ["Id", "Name", "Type", "State", "Status"], criteria={})
    # v5-чтение стоит баллов: при 152 (нет баллов) НЕ выходим с ошибкой — список добираем по
    # КУКЕ через Grid (без баллов), как «Показать РК» и должно работать на исчерпанном аккаунте.
    v5_err = j["error"].get("error_string") if "error" in j else None
    camps = ([] if v5_err else
             [{"id": c["Id"], "name": c.get("Name"), "type": c.get("Type"),
               "state": c.get("State"), "status": c.get("Status"), "src": "v5"}
              for c in (j.get("result") or {}).get("Campaigns", [])])
    # Grid видит ВСЕ типы (text/unified/UAC) — добираем всё, чего нет в v5 (без дублей).
    # Это и есть «часть по апи (v5) + часть по куки (grid)». Статус мапим из primaryStatus/archived,
    # иначе архивная/черновик показывались как «идёт» (была эта ошибка).
    _GRID_STATE = {"DRAFT": "DRAFT", "ARCHIVED": "ARCHIVED", "ENDED": "ENDED",
                   "STOPPED": "SUSPENDED", "SUSPENDED": "SUSPENDED", "PAUSED": "SUSPENDED"}
    uac_added = 0
    grid_err = None
    try:
        seen = {str(c["id"]) for c in camps}
        for g in _grid_list_campaigns(login):
            if str(g.get("id")) in seen:
                continue
            gstatus = (g.get("status") or "").upper()
            state = "ARCHIVED" if g.get("archived") else _GRID_STATE.get(gstatus, "ON")
            camps.append({"id": g["id"], "name": g.get("name"), "type": g.get("typename"),
                          "state": state, "status": g.get("status"), "src": "grid"})
            uac_added += 1
    except Exception as e:  # noqa: BLE001 — grid недоступен (часто протухшая кука) → показываем хотя бы v5
        grid_err = str(e)
    camps.sort(key=lambda c: (_STATE_ORDER.get(c["state"], 9), str(c["name"] or "")))
    out = {"login": login, "agency": ag, "campaigns": camps, "uac_added": uac_added}
    if v5_err:
        # v5 не отдал (обычно 152 — нет баллов): список добираем по куке (Grid). Если и Grid пуст —
        # причина чаще НЕ баллы, а ПРОТУХШАЯ кука на главпотоке (need_reset) → показываем именно это,
        # иначе «Недостаточно баллов» вводит в заблуждение (видно на скрине Семёна).
        if camps:
            out["note"] = f"баллы исчерпаны ({v5_err}) — список по куке (Grid); текстовые/РСЯ из v5 могут быть не все"
        elif grid_err and any(s in grid_err for s in ("протухла", "need_reset", "Истек", "Истёк")):
            out["error"] = f"баллы исчерпаны + кука протухла на главпотоке: {grid_err[:240]}"
        elif grid_err:
            out["error"] = f"{v5_err} (кука тоже не отдала список: {grid_err[:140]})"
        else:
            # Grid отработал БЕЗ ошибки и отдал 0 кампаний → аккаунт реально пуст (напр. после
            # «Удалить черновики»). Красная «Недостаточно баллов» тут вводила в заблуждение
            # (скрин Семёна 03.07 #84) — это не ошибка чтения, а честная пустота.
            out["note"] = f"кампаний в аккаунте нет (проверено по куке/Grid); v5 недоступен: {v5_err}"
    return jsonify(out)

def _stop_all_response():
    """Остановить ВСЕ активные (State=ON) кампании аккаунта через v5 campaigns.suspend.

    Тело: {"login": "...", "agency": "..."}. Обратимо (resume в Директе)."""
    body = request.json or {}
    login = (body.get("login") or "").strip()
    agency = (body.get("agency") or "").strip()
    if not login:
        return jsonify({"error": "login обязателен"}), 400

    ok, reason, wait = _pull_begin(f"stopall:{login}", 15.0)
    if not ok:
        return _busy_response(reason, wait)
    try:
        token, ag = _token_for_login(login, agency, _direct_tokens())
        if not token:
            return jsonify({"error": "нет рабочего агентского токена для этого логина"})
        jg = _v5_call("campaigns", "get", token, login,
                      {"SelectionCriteria": {"States": ["ON"]}, "FieldNames": ["Id", "Name", "Type"]})
        if "error" in jg:
            return jsonify({"error": _v5_err(jg)})
        camps = (jg.get("result") or {}).get("Campaigns", [])
        if not camps:
            return jsonify({"ok": True, "stopped": 0, "total": 0,
                            "message": "активных (ON) кампаний нет — останавливать нечего"})
        # Мастер кампании (UNIFIED_CAMPAIGN) v5 не глушит — стопаем нативным UAC API (куки).
        unified = [c["Id"] for c in camps if c.get("Type") == "UNIFIED_CAMPAIGN"]
        standard = [c["Id"] for c in camps if c.get("Type") != "UNIFIED_CAMPAIGN"]
        stopped, by_v5, by_uac, errors = 0, 0, 0, []

        for i in range(0, len(standard), 100):       # обычные → v5 suspend
            js = _v5_call("campaigns", "suspend", token, login,
                          {"SelectionCriteria": {"Ids": standard[i:i + 100]}})
            if "error" in js:
                errors.append(_v5_err(js))
                continue
            for rr in (js.get("result") or {}).get("SuspendResults", []):
                if rr.get("Id") and not rr.get("Errors"):
                    stopped += 1
                    by_v5 += 1
                elif rr.get("Errors"):
                    errors.append(str(rr["Errors"])[:120])

        if unified:                                   # Мастер → UAC set_status=stopped (куки)
            try:
                uac = cmc.build_client(login, account=(ag or None))
                uac.link_info("https://ya.ru")        # bootstrap CSRF
                for cid in unified:
                    try:
                        uac.set_status(str(cid), "stopped")
                        stopped += 1
                        by_uac += 1
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"мастер {cid}: {str(e)[:80]}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"UAC-куки недоступны: {str(e)[:90]}")

        return jsonify({"ok": True, "stopped": stopped, "total": len(camps),
                        "by_v5": by_v5, "by_uac": by_uac, "masters": len(unified),
                        "errors": errors[:5]})
    finally:
        _pull_end(f"stopall:{login}")

def _check_blocks_response():
    """Блокировки аккаунтов (Grid userFeatures на агентских куках). Только переданные логины.

    Своё агентство из строки пробуем первым; если нет прав/ошибка — перебираем
    остальные агентские куки (как check_block_direct), пока не получим ответ."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    pairs = (request.json or {}).get("pairs") or []
    ok, reason, wait = _pull_begin("blocks", 60.0)
    if not ok:
        return _busy_response(reason, wait)
    try:
        # Список агентств: из строк + известные (на случай неверного agency_account).
        agencies: list[str] = []
        for p in pairs:
            ag = (p.get("agency") or "").strip()
            if ag and ag != "None" and ag not in agencies:
                agencies.append(ag)
        for ag in _KNOWN_AGENCIES:
            if ag not in agencies:
                agencies.append(ag)

        # Одна сессия (cookie+csrf) на агентство — поднимаем один раз.
        sessions: dict[str, tuple] = {}
        for ag in agencies:
            try:
                cookie = cmc.load_cookie(ag)
            except Exception:  # noqa: BLE001
                cookie = None
            if not cookie:
                continue
            csrf = _block_bootstrap(cookie, ag)
            if csrf is None:
                continue
            sessions[ag] = (cookie, csrf)

        def check_one(login: str, own: str):
            order = ([own] if own in sessions else []) + [a for a in sessions if a != own]
            for ag in order:
                cookie, csrf = sessions[ag]
                res = _block_check(cookie, csrf, login)
                if res is not None:
                    return res
            return None

        items = [((p.get("login") or "").strip(), (p.get("agency") or "").strip()) for p in pairs]
        items = [(lg, ag) for lg, ag in items if lg]
        blocks: dict = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(check_one, lg, ag): lg for lg, ag in items}
            for f in as_completed(futs):
                blocks[futs[f]] = f.result()
        for p in pairs:
            blocks.setdefault((p.get("login") or "").strip(), None)
        return jsonify({"blocks": blocks})
    finally:
        _pull_end("blocks")

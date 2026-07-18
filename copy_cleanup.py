"""Инфо о кампаниях цели и очистка черновиков перед копированием.

Вынесено из copy_engine.py (чистый code-motion, логика не изменена).
DI инъектится copy_engine.configure() фан-аутом; sibling-модули copy_* — прямой импорт (DAG, цикла нет).
"""
from __future__ import annotations

from . import grid_create as gc

from .copy_jobs import _copy_job_log
from .copy_uac import _copy_is_uac_grid_row

# ── DI (инъектится copy_engine.configure фан-аутом; None до инъекции) ──
_direct_tokens = _grid_list_campaigns = _resolve_agency_hint = _token_for_login = _v5_call = _v5_err = None


def configure(deps: dict) -> None:
    """Инъекция DI из blueprint (фан-аут из copy_engine.configure)."""
    globals().update(deps)


def _copy_target_campaigns_info(login: str) -> dict:
    """Read-only снимок кампаний целевого аккаунта для UI выбора очистки.

    Возвращает {total, draft_count, non_draft_count, archivable_count, breakdown}.
    DRAFT не могут быть заархивированы (v5 API error 8303) →
    archivable_count = non_draft_count (не-черновики, не архивированные).
    campaigns.get без фильтра по умолчанию не возвращает ARCHIVED-кампании.
    """
    try:
        agency = _resolve_agency_hint(login, "")
        token, _ = _token_for_login(login, agency, _direct_tokens())
        if not token:
            return {"error": "нет рабочего токена для этого логина"}
        j = _v5_call("campaigns", "get", token, login, {
            "FieldNames": ["Id", "State", "Status"],
            "SelectionCriteria": {},
        })
        if "error" in j:
            err_text = (_v5_err(j) if callable(_v5_err) else str(j.get("error")))
            return {"error": (err_text or "API error")[:200]}
        campaigns = (j.get("result") or {}).get("Campaigns", [])
        draft_count = sum(1 for c in campaigns if c.get("Status") == "DRAFT")
        non_draft_count = len(campaigns) - draft_count
        breakdown: dict[str, int] = {}
        for c in campaigns:
            key = f"{c.get('State') or '?'}/{c.get('Status') or '?'}"
            breakdown[key] = breakdown.get(key, 0) + 1
        return {
            "total": len(campaigns),
            "draft_count": draft_count,
            "non_draft_count": non_draft_count,
            # DRAFT нельзя архивировать (v5 8303); archivable = только не-черновики
            "archivable_count": non_draft_count,
            "breakdown": breakdown,
        }
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


def _copy_cleanup_uac_drafts(job_id: str, login: str, errors: list[str]) -> int:
    """Удалить UAC/МК-ЧЕРНОВИКИ целевого логина по кукам (v5 их не видит → дубли +3 за прогон).

    Удаляем ТОЛЬКО status=DRAFT и не-archived: запущенные кампании не трогаем.
    Сбой кук — не критичен (v5-ветка уже отработала): пишем в errors, копирование не рвём.
    """
    try:
        rows = _grid_list_campaigns(login) or []
    except Exception as e:  # noqa: BLE001
        errors.append(f"cleanup uac list: {str(e)[:120]}")
        return 0
    ids: list[int] = []
    for row in rows:
        if str(row.get("status") or "").upper() != "DRAFT" or row.get("archived"):
            continue
        if not _copy_is_uac_grid_row(row):
            continue
        try:
            cid = int(row.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if cid > 0:
            ids.append(cid)
    if not ids:
        return 0
    _copy_job_log(job_id, f"cleanup delete_drafts: МК-черновиков по кукам {len(ids)} → удаляю")
    try:
        res = gc.GridCreateClient(login).delete_campaigns(ids)
    except Exception as e:  # noqa: BLE001
        errors.append(f"cleanup uac delete: {str(e)[:120]}")
        return 0
    for err in (res.get("errors") or []):
        errors.append(f"uac delete: {str(err)[:100]}")
    return len(res.get("deleted") or [])


def _copy_target_cleanup(job_id: str, login: str, agency: str, mode: str) -> dict:
    """Очистить целевой аккаунт ДО копирования.

    mode='delete_drafts': удаляет только кампании со Status=DRAFT (v5 campaigns.delete,
        без archive-шага — DRAFT удаляются напрямую, подтверждено тестом 2026-07-17).
    mode='archive':       архивирует все non-DRAFT кампании. ON-кампании сначала
        suspend, потом archive. DRAFT пропускаются (v5 8303).

    Возвращает {ok, deleted, archived, skipped_non_draft|skipped_draft, errors}.
    Поднимает RuntimeError при критической ошибке API (не per-item) — _copy_run_job
    тогда прерывает копирование, не льёт РК в неочищенный аккаунт.
    """
    if mode not in ("delete_drafts", "archive"):
        return {"ok": True, "deleted": 0, "archived": 0, "skipped": 0, "errors": []}

    ag = agency or _resolve_agency_hint(login, "")
    token, _ = _token_for_login(login, ag, _direct_tokens())
    if not token:
        raise RuntimeError(f"cleanup: нет рабочего токена для {login!r}")

    _copy_job_log(job_id, f"cleanup {mode}: получаю кампании {login}")
    j = _v5_call("campaigns", "get", token, login, {
        "FieldNames": ["Id", "Name", "State", "Status"],
        "SelectionCriteria": {},
    })
    if "error" in j:
        raise RuntimeError(f"cleanup campaigns.get: {(_v5_err(j) if callable(_v5_err) else str(j.get('error')))[:200]}")

    campaigns = (j.get("result") or {}).get("Campaigns", [])
    errors: list[str] = []

    if mode == "delete_drafts":
        drafts = [c["Id"] for c in campaigns if c.get("Status") == "DRAFT"]
        non_draft_skip = len(campaigns) - len(drafts)
        _copy_job_log(job_id, f"cleanup delete_drafts: к удалению {len(drafts)}, пропускаем не-черновики {non_draft_skip}")
        deleted = 0
        for i in range(0, len(drafts), 100):
            chunk = drafts[i:i + 100]
            jd = _v5_call("campaigns", "delete", token, login, {"SelectionCriteria": {"Ids": chunk}})
            if "error" in jd:
                raise RuntimeError(f"cleanup delete batch: {(_v5_err(jd) if callable(_v5_err) else str(jd.get('error')))[:200]}")
            for rr in (jd.get("result") or {}).get("DeleteResults", []):
                if rr.get("Id") and not rr.get("Errors"):
                    deleted += 1
                else:
                    errors.append(f"delete {rr.get('Id')}: {str(rr.get('Errors') or 'unknown')[:100]}")
        # МК/tp6 невидимы для v5 (campaigns.get их не отдаёт) → копятся дублями: чистим по кукам.
        deleted += _copy_cleanup_uac_drafts(job_id, login, errors)
        _copy_job_log(job_id, f"cleanup delete_drafts: удалено {deleted}, ошибок {len(errors)}, пропущено {non_draft_skip}")
        return {"ok": True, "deleted": deleted, "archived": 0,
                "skipped_non_draft": non_draft_skip, "errors": errors}

    # mode == "archive"
    non_draft = [c for c in campaigns if c.get("Status") != "DRAFT"]
    draft_skip = len(campaigns) - len(non_draft)
    on_ids = [c["Id"] for c in non_draft if c.get("State") == "ON"]
    archive_ids = [c["Id"] for c in non_draft]
    _copy_job_log(job_id, f"cleanup archive: к архивации {len(archive_ids)}, suspend ON {len(on_ids)}, пропускаем DRAFT {draft_skip}")

    if on_ids:
        for i in range(0, len(on_ids), 100):
            chunk = on_ids[i:i + 100]
            js = _v5_call("campaigns", "suspend", token, login, {"SelectionCriteria": {"Ids": chunk}})
            if "error" in js:
                raise RuntimeError(f"cleanup suspend: {(_v5_err(js) if callable(_v5_err) else str(js.get('error')))[:200]}")
            for rr in (js.get("result") or {}).get("SuspendResults", []):
                if rr.get("Errors"):
                    errors.append(f"suspend {rr.get('Id')}: {str(rr['Errors'])[:80]}")

    archived = 0
    for i in range(0, len(archive_ids), 100):
        chunk = archive_ids[i:i + 100]
        ja = _v5_call("campaigns", "archive", token, login, {"SelectionCriteria": {"Ids": chunk}})
        if "error" in ja:
            raise RuntimeError(f"cleanup archive batch: {(_v5_err(ja) if callable(_v5_err) else str(ja.get('error')))[:200]}")
        for rr in (ja.get("result") or {}).get("ArchiveResults", []):
            if rr.get("Id") and not rr.get("Errors"):
                archived += 1
            else:
                errors.append(f"archive {rr.get('Id')}: {str(rr.get('Errors') or 'unknown')[:100]}")

    _copy_job_log(job_id, f"cleanup archive: заархивировано {archived}, пропущено DRAFT {draft_skip}, ошибок {len(errors)}")
    return {"ok": True, "deleted": 0, "archived": archived,
            "skipped_draft": draft_skip, "errors": errors}

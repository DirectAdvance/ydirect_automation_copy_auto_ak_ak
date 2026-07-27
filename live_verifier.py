"""Read-only live verification for Direct create_set jobs.

The static verifier checks the response shape. This module normalizes created
campaigns and compares them with already-fetched Direct/Grid state. It has no
Flask dependency and does not create, update, delete, or publish anything.
"""
from __future__ import annotations

from typing import Any

from .campaign_result import as_int, created_campaigns, result_name


def _index_by_id(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for row in rows or []:
        cid = as_int(row.get("id") or row.get("Id"))
        if cid is not None:
            out[cid] = row
    return out


def _index_by_name(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {result_name(row): row for row in rows or [] if result_name(row)}


def _skipped_struct_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """SKIP-строки результата, несущие эталон структуры (`struct`) — рекурсивно, как
    `created_campaigns`. Именно они описывают tp6/tp7, которые УЖЕ существуют в кабинете и
    поэтому не пересоздавались: без отдельного прохода их структура не проверяется вообще."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(row: dict[str, Any]) -> None:
        if not isinstance(row, dict):
            return
        for child in row.get("campaigns") or []:
            walk(child)
        if not row.get("skipped") or not isinstance(row.get("struct"), dict):
            return
        nm = result_name(row)
        if nm and nm not in seen:
            seen.add(nm)
            out.append(row)

    for row in results or []:
        walk(row)
    return out


def _grid_rows_by_prefix(grid_by_name: dict[str, dict[str, Any]], nm: str) -> list[dict[str, Any]]:
    """ВСЕ кампании кабинета для позиции: имя с фид-суффиксом/_vNN («…site — yandex», «… _v02»).

    Та же нормализация, что у RESUME-SKIP (`queue_server._position_live_in_names`), иначе
    tp6/tp7 fan-out по фидам не резолвится и сверка структуры молча пропускается.
    ⚠️ Именно СПИСОК, а не первое совпадение: одна позиция разворачивается по фидам в НЕСКОЛЬКО
    РК («— feedA», «— feedB»), и при выборе одной потерянные ключи остальных не видны.
    Точное совпадение имени — первым (приоритет прежнего поведения)."""
    from .create_set_resume import FANOUT_SEP, _VNN_RE
    pref = nm + FANOUT_SEP
    exact: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []
    for cand, row in grid_by_name.items():
        base = _VNN_RE.sub("", cand).strip()
        if cand == nm or base == nm:
            exact.append(row)
        elif cand.startswith(pref) or base.startswith(pref):
            out.append(row)
    return exact + out


def _verify_skipped_struct(nm: str, lid: int, struct: dict[str, Any],
                           uac_detail_rows: dict[int, dict[str, Any]],
                           issues: list[dict[str, Any]]) -> None:
    """Сверка структуры ОДНОЙ уже существующей (RESUME-SKIP) tp6/tp7-кампании. Report-only."""
    detail = uac_detail_rows.get(int(lid))
    if not detail:
        issues.append({"severity": "warn", "code": "UAC_DETAIL_SKIPPED", "name": nm, "id": lid})
        return
    from .uac_verifier import verify_uac_detail
    uac_issues, _uac_repair = verify_uac_detail(nm, int(lid), detail, {"struct": struct})
    # 🛑 ПРЕДОХРАНИТЕЛЬ (2026-07-19). Эти кампании УЖЕ существуют в кабинете и намеренно НЕ
    # пересоздавались (`already_in_direct`-skip). Полный набор проверок для них НЕПРИМЕНИМ:
    # мы их не собирали, поэтому «недостача» титулов/сайтлинков/медиа/счётчика — это НЕ наш
    # дефект, а свойство пред-существующей кампании. А коды `UAC_*` из этого набора ∈ `repair_gate.
    # _UAC_REPLACE_CODES` → `repair_auto.queue_recreate_repair_job` → `delete_uac(...)`, то есть
    # skip «не трогать существующее» превратился бы в «удалить существующее». Живой DRAFT-gate
    # в `create_set_repairing._delete_uac_repair_campaigns` спасает только от удаления ЗАПУЩЕННЫХ;
    # пред-существующий ЧЕРНОВИК он пропускает — его закрывает только этот фильтр.
    # Пропускаем ТОЛЬКО `UAC_STRUCT_*` (сверка «структура слепка → кабинет»: потерянные ключи и
    # аудитории), и НИ ОДНОГО repair-кандидата: проход строго report-only.
    for _iss in uac_issues:
        if not str(_iss.get("code") or "").startswith("UAC_STRUCT_"):
            continue
        issues.append({**_iss, "source": "resume_skip"})


def _account_has_promo(content_counts: dict[int, dict[str, Any]] | None) -> bool | None:
    """ФОЛБЭК ступени 1 промо-гейта — прокси по кампаниям набора (tri-state).

    ⚠️ Используется, ТОЛЬКО когда настоящий признак библиотеки аккаунта не передан
    (``account_has_promo_library is None``): например, вызов верификатора не из штатного
    потока создания. В штатном потоке приоритет у ``account_has_promo_library``, который
    приходит из v5 ``promotions.get``, уже выполненного в ``create_set_promo`` /
    ``precreate`` — там признак точный и покрывает случай 0/N.

    Прокси считается ИЗ УЖЕ ПРОЧИТАННЫХ данных (``promo_extension_id`` из того же ответа
    CampaignsEditData) → **ноль дополнительных обращений** к Grid/Direct API.

    * ``None``  — ассеты ни у одной кампании не прочитаны → неизвестно → верификатор молчит.
    * ``True``  — хотя бы у одной кампании набора промо привязано ⇒ библиотека непуста ⇒
      кампании без промо в том же наборе — дефект (класс «промо доехало не до всех»).
    * ``False`` — ассеты прочитаны, промо нет НИ У КОГО.

    Известное ограничение прокси (почему он лишь фолбэк): полный провал доставки 0/N
    неотличим от «библиотека пуста» → вернётся ``False`` и код промолчит. Именно этот
    слепой угол закрывает проброшенный ``account_has_promo_library``.
    """
    rows = [r for r in (content_counts or {}).values() if r and r.get("campaign_assets_read")]
    if not rows:
        return None
    return any(str(r.get("promo_extension_id") or "").strip() for r in rows)


def verify_live_create_set(*, login: str, results: list[dict[str, Any]],
                           v5_campaigns: list[dict[str, Any]] | None = None,
                           grid_campaigns: list[dict[str, Any]] | None = None,
                           grid_content_counts: dict[int, dict[str, int]] | None = None,
                           uac_details: dict[int, dict[str, Any]] | None = None,
                           prefer_grid: bool = True,
                           account_has_promo_library: bool | None = None,
                           phase: str = "in_job") -> dict[str, Any]:
    """Compare created result rows with read-only v5/Grid snapshots.

    ``v5_campaigns`` and ``grid_campaigns`` are optional. When a snapshot is not
    supplied, the corresponding checks are reported as skipped, not failed.

    ``account_has_promo_library`` — ступень 1 гейта ``PROMO_MISSING``: tri-state признак
    «в БИБЛИОТЕКЕ аккаунта есть промо-акции», прочитанный в штатном потоке создания
    (v5 ``promotions.get``, 0 новых запросов). ``None`` → фолбэк на прокси
    ``_account_has_promo`` (прежнее поведение).

    ``phase`` — фаза проверки для сверки «build ⇄ кабинет»: ``"in_job"`` (по умолчанию) даёт
    НЕДОБОРУ контента severity ``warn``, потому что in-job верификация структурно НЕ видит
    контент, который доливает отложенный демон (dcr стартует через +180с ПОСЛЕ статуса done);
    ``"delayed"`` — отложенный проход, там недобор уже ``error`` с repair-кандидатом.
    Полное отсутствие (live==0 при build>0) — ``error`` в обеих фазах.

    ``prefer_grid=True`` is intentional for this project: Direct API units are
    scarce, while the service can read/create/finalize many entities through the
    cookie/Grid layer. When Grid data is supplied, it is accepted as the primary
    existence check even for non-UAC campaign ids.
    """
    created = created_campaigns(results or [])
    v5_by_id = _index_by_id(v5_campaigns or [])
    grid_by_id = _index_by_id(grid_campaigns or [])
    grid_by_name = _index_by_name(grid_campaigns or [])
    content_counts = grid_content_counts or {}
    uac_detail_rows = uac_details or {}
    # Ступень 1 промо-гейта — один раз на весь набор (данные уже прочитаны, 0 новых запросов).
    # Приоритет — настоящий признак библиотеки аккаунта из потока создания; прокси по кампаниям
    # набора остаётся ФОЛБЭКОМ на случай, когда признак не проброшен (None).
    account_promo = (account_has_promo_library if account_has_promo_library is not None
                     else _account_has_promo(content_counts))
    issues: list[dict[str, Any]] = []
    repair: list[dict[str, Any]] = []
    checked = {
        "v5": bool(v5_campaigns is not None),
        "grid": bool(grid_campaigns is not None),
        "grid_content": bool(grid_content_counts is not None),
        "uac_details": bool(uac_details is not None),
    }

    for c in created:
        cid = c.get("id")
        nm = c.get("name") or ""
        kind = c.get("kind") or "unknown"
        from .local_result_verifier import verify_local_result
        issues.extend(verify_local_result(c))
        if cid is None:
            issues.append({"severity": "error", "code": "CREATED_WITHOUT_ID", "name": nm})
            repair.append({"kind": "manual_lookup_by_name", "name": nm})
            continue
        actual = None
        if kind == "uac":
            if grid_campaigns is None:
                issues.append({"severity": "warn", "code": "GRID_CHECK_SKIPPED", "name": nm, "id": cid})
            else:
                actual = grid_by_id.get(cid) or grid_by_name.get(nm)
                if not actual:
                    issues.append({"severity": "error", "code": "UAC_NOT_FOUND_IN_GRID", "name": nm, "id": cid})
                    repair.append({"kind": "recreate_or_resume_campaign", "name": nm, "id": cid})
            if uac_details is not None:
                detail = uac_detail_rows.get(int(cid)) or {}
                if not detail:
                    issues.append({"severity": "warn", "code": "UAC_DETAIL_SKIPPED", "name": nm, "id": cid})
                else:
                    from .uac_verifier import verify_uac_detail
                    # struct — эталон СТРУКТУРЫ слепка для позиции, положенный создателем в
                    # result-строку (create_set_master_product `_res["struct"]`). 0 новых запросов.
                    _uac_row = c.get("result") or {}
                    _uac_struct = _uac_row.get("struct") if isinstance(_uac_row, dict) else None
                    # images_pool — сколько картинок СВОЕГО ct физически было доступно при сборке
                    # (create_set_master_product `_res["images_pool"]`). Без него verifier не может
                    # отличить «в пуле меньше 5, взяли всё» (не дефект) от «пул полный, а картинки
                    # потерялись по дороге» (дефект). 0 новых запросов.
                    _uac_expected: dict[str, Any] = {}
                    if isinstance(_uac_struct, dict):
                        _uac_expected["struct"] = _uac_struct
                    _uac_pool = _uac_row.get("images_pool") if isinstance(_uac_row, dict) else None
                    if isinstance(_uac_pool, int) and not isinstance(_uac_pool, bool):
                        _uac_expected["images_pool"] = _uac_pool
                    uac_issues, uac_repair = verify_uac_detail(
                        nm, int(cid), detail, _uac_expected or None)
                    issues.extend(uac_issues)
                    repair.extend(uac_repair)
        else:
            if prefer_grid and grid_campaigns is not None:
                actual = grid_by_id.get(cid) or grid_by_name.get(nm)
                if not actual and v5_campaigns is not None:
                    actual = v5_by_id.get(cid)
                if not actual:
                    issues.append({"severity": "error", "code": "CAMPAIGN_NOT_FOUND_IN_GRID", "name": nm, "id": cid})
                    repair.append({"kind": "recreate_or_resume_campaign", "name": nm, "id": cid, "via": "cookie"})
            elif v5_campaigns is not None:
                actual = v5_by_id.get(cid)
                if not actual:
                    issues.append({"severity": "error", "code": "CAMPAIGN_NOT_FOUND_IN_V5", "name": nm, "id": cid})
                    repair.append({"kind": "recreate_or_resume_campaign", "name": nm, "id": cid})
            elif grid_campaigns is not None:
                actual = grid_by_id.get(cid) or grid_by_name.get(nm)
                if not actual:
                    issues.append({"severity": "error", "code": "CAMPAIGN_NOT_FOUND_IN_GRID", "name": nm, "id": cid})
                    repair.append({"kind": "recreate_or_resume_campaign", "name": nm, "id": cid})
            else:
                issues.append({"severity": "warn", "code": "LIVE_CHECK_SKIPPED", "name": nm, "id": cid})
        if actual:
            from .campaign_state_verifier import verify_campaign_state
            state_issues, state_repair = verify_campaign_state(nm, int(cid), actual)
            issues.extend(state_issues)
            repair.extend(state_repair)
        if kind != "uac" and grid_content_counts is not None:
            counts = content_counts.get(int(cid)) or {}
            # build — отчёт БИЛДЕРА по ЭТОЙ кампании (уже в результате джобы, 0 новых запросов).
            # Привязан к развёрнутой кампании, а не к позиции плана, поэтому фан-аут по фидам и
            # тег «х3» (одна позиция плана → несколько РК) сверку не ломают.
            _row = c.get("result") or {}
            _build = (_row.get("build") or _row.get("tp1_build") or _row.get("tp5_build") or {})
            from .grid_content_verifier import verify_grid_content
            # root_href_ok намеренно НЕ передаётся: доступного контекста для автодетекта нет.
            # _build (tp1_build/tp5_build) содержит только счётчики (adgroups/ads/keywords),
            # но не href отдельных групп — различить «баг» и «заглушку без модельных страниц»
            # нельзя. Квизовые кампании детектор не задевает (href = domain/quiz, не корень).
            # Если признак появится — добавить root_href_ok в expected здесь.
            grid_issues, grid_repair = verify_grid_content(
                nm, int(cid), counts,
                {"account_has_promo": account_promo,
                 "build": _build if isinstance(_build, dict) else {},
                 "phase": phase})
            issues.extend(grid_issues)
            repair.extend(grid_repair)

    # ── Сверка структуры по УЖЕ СУЩЕСТВУЮЩИМ tp6/tp7 (RESUME-SKIP) ───────────────────────────
    # `created_campaigns` отбрасывает строки со `skipped` → кампании, созданные ДО починки, под
    # теми же именами дают skip и НИКОГДА не попадали под UAC_STRUCT_*: их потерянные ключи и
    # аудитории оставались потерянными молча (Д7 2026-07-19). Проверяем их отдельным проходом,
    # id резолвим ПО ИМЕНИ из уже прочитанного Grid-снимка → ноль новых запросов.
    skipped_struct = 0
    for row in _skipped_struct_rows(results or []):
        nm = result_name(row)
        _struct = row.get("struct")
        if not nm or not isinstance(_struct, dict):
            continue
        skipped_struct += 1
        # ВСЕ РК позиции: фан-аут по фидам даёт несколько кампаний на одну строку плана.
        _lids: list[int] = []
        for _live in _grid_rows_by_prefix(grid_by_name, nm):
            _v = as_int(_live.get("id") or _live.get("Id"))
            if _v is not None and _v > 0 and _v not in _lids:
                _lids.append(_v)
        if not _lids:
            issues.append({"severity": "warn", "code": "UAC_STRUCT_SKIP_NOT_RESOLVED", "name": nm})
            continue
        for _lid in _lids:
            _verify_skipped_struct(nm, _lid, _struct, uac_detail_rows, issues)

    errors = sum(1 for x in issues if x.get("severity") == "error")
    warnings = sum(1 for x in issues if x.get("severity") == "warn")
    status = "fail" if errors else ("warn" if warnings else "pass")
    report = {
        "status": status,
        "login": login,
        "checked": checked,
        "phase": str(phase or "in_job"),
        "prefer_grid": bool(prefer_grid),
        "summary": {
            "created_results": len(created),
            "skipped_struct_checked": skipped_struct,
            "v5_rows": len(v5_campaigns or []),
            "grid_rows": len(grid_campaigns or []),
            "grid_content_rows": len(grid_content_counts or {}),
            "uac_detail_rows": len(uac_details or {}),
            "issues": len(issues),
            "errors": errors,
            "warnings": warnings,
        },
        "campaigns": [{"id": c.get("id"), "name": c.get("name"), "kind": c.get("kind")} for c in created],
        "issues": issues[:120],
        "repair_candidates": repair[:120],
    }
    try:
        from .repair_planner import build_repair_plan
        report["repair_plan"] = build_repair_plan(report)
    except Exception:  # noqa: BLE001
        report["repair_plan"] = {"status": "error"}
    return report

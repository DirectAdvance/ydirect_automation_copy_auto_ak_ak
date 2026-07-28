"""Terminal status decisions for create queue jobs."""
from __future__ import annotations

from typing import Any


CREATE_JOB_KINDS = {"set", "slepok"}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def create_failed_error(failed: int, *, prefix: str = "создание завершилось с ошибками") -> str:
    return f"{prefix}: не создано {_as_int(failed)}"


def terminal_status_for_job(
    kind: str | None,
    data: dict[str, Any] | None,
    *,
    cancelled: bool = False,
) -> tuple[str, str | None]:
    """Return terminal status/error for worker result.

    For create-set jobs, partial failures must be red terminal state. Copy/delete
    keep their existing semantics because their UI already presents partial counts.
    """
    if cancelled:
        return "cancelled", None
    if isinstance(data, dict) and data.get("error"):
        return "error", str(data.get("error") or "")[:500]
    failed = _as_int((data or {}).get("failed") if isinstance(data, dict) else 0)
    if (kind or "") in CREATE_JOB_KINDS and failed > 0:
        return "error", create_failed_error(failed)
    return "done", None


def terminal_status_for_parent_failed(failed: int) -> tuple[str, str | None]:
    failed_i = _as_int(failed)
    if failed_i > 0:
        return "error", create_failed_error(failed_i, prefix="докрутка завершилась с ошибками")
    return "done", None


# ---------------------------------------------------------------------------
# Гейт финального статуса: has_issues breakdown
# ---------------------------------------------------------------------------

def _lv_summary_int(data: dict[str, Any], key: str) -> int:
    """Read integer field from live_verification.summary."""
    try:
        lv = data.get("live_verification") or {}
        return int((lv.get("summary") or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _ver_summary_int(data: dict[str, Any], key: str) -> int:
    """Read integer field from verification.summary (static verify_create_set report)."""
    try:
        ver = data.get("verification") or {}
        return int((ver.get("summary") or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _count_positions_with_errors(data: dict[str, Any]) -> int:
    """Count unique campaign positions with at least one error-severity issue."""
    try:
        lv = data.get("live_verification") or {}
        issues = lv.get("issues") or []
        seen: set[object] = set()
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            if issue.get("severity") == "error":
                cid = (issue.get("campaign_id")
                       or issue.get("item_id")
                       or issue.get("id"))
                seen.add(cid if cid is not None else id(issue))
        return len(seen)
    except Exception:  # noqa: BLE001
        return 0


def _losses_count(data: dict[str, Any]) -> int:
    """Сколько ПОТЕРЬ зафиксировал сам прогон (`result["losses"]`).

    Потеря = часть плана НЕ доехала в кабинет: аудитории не отправлены (пустая карта условий),
    минус-фразы не влезли в наборы (`not_packed`), набор в кабинете разошёлся со слепком,
    наборы не привязались к части кампаний. Верификаторы этого не видят (они сверяют то, что
    создано, а не то, что не отправлено), поэтому потери — САМОСТОЯТЕЛЬНЫЙ значимый сигнал.
    """
    try:
        return len([x for x in (data.get("losses") or []) if x])
    except (TypeError, AttributeError):
        return 0


def has_verification_data(data: dict[str, Any] | None) -> bool:
    """True, если хотя бы один верификатор реально отработал и отдал summary.

    Нужно, чтобы отличить «верификация прошла чисто» (0 ошибок — данные ЕСТЬ)
    от «верификация не запускалась/упала» (данных НЕТ). Во втором случае нули
    в разбивке были бы враньём: отсутствие данных ≠ отсутствие дефектов.
    Деградированный постпроцесс (`create_set_postprocess._degraded_postprocess`)
    кладёт `verification`/`live_verification` БЕЗ `summary` — это «данных нет».
    """
    d = data or {}
    for key in ("live_verification", "verification"):
        block = d.get(key)
        if isinstance(block, dict) and isinstance(block.get("summary"), dict):
            return True
    return False


def compute_job_issues_breakdown(
    kind: str | None,
    data: dict[str, Any] | None,
) -> dict[str, int] | None:
    """Compute issues breakdown for a create-set job that reached a terminal status.

    Returns None when the job is fully clean (no significant defects).
    Returns a breakdown dict when live_verification.summary.errors > 0.

    Gate logic (what is significant vs штатное):
    - SIGNIFICANT → live_verification.summary.errors > 0
      (error-severity findings from verifiers: missing groups, wrong config, etc.)
    - SIGNIFICANT → result["losses"] непустой (часть плана НЕ доехала: аудитории не
      отправлены, минус-фразы не влезли, наборы не привязались). Верификаторы это НЕ ловят:
      они сверяют созданное, а не отправленное, поэтому джоба оставалась зелёной.
    - NOT significant alone → warnings, gate_skips, errors_log
      (штатные report-only warns; infrastructure connectivity issues)
    - Out of scope here → build.errors[] plural (separate task, create_set_orchestrator ⛔)
    """
    if (kind or "") not in CREATE_JOB_KINDS:
        return None
    d = data or {}
    lv_errors = _lv_summary_int(d, "errors")    # live_verification.summary.errors
    ver_errors = _ver_summary_int(d, "errors")   # verification.summary.errors (static verifier)
    total_errors = lv_errors + ver_errors
    losses = _losses_count(d)
    if total_errors == 0 and losses == 0:
        return None  # чистый done
    live_warnings = _lv_summary_int(d, "warnings")
    gate_skips = _as_int(d.get("gate_skips"))
    positions_with_errors = _count_positions_with_errors(d)
    return {
        "lv_errors": lv_errors,           # live_verification.summary.errors
        "ver_errors": ver_errors,          # verification.summary.errors
        "live_errors": total_errors,       # сумма lv_errors + ver_errors; JS backward-compat (automation_jobs.js:582)
        "live_warnings": live_warnings,
        "gate_skips": gate_skips,
        "positions_with_errors": positions_with_errors,
        "losses": losses,                  # потери прогона (result["losses"]), не от верификаторов
    }


def annotate_job_issues(kind: str | None, data: dict[str, Any] | None) -> None:
    """Записать в result джобы итог гейта дефектов. Мутирует `data` на месте.

    Вызывается на ЛЮБОМ терминальном статусе, который проходит через воркер
    (`done` / `error` / `cancelled`), а не только на `done`: именно на `error`
    (failed>0) молчание опаснее всего — прогон `69a140093e78` закончился
    `error` с `lv_errors=23`, и разбивки в result не было вовсе
    (ERRORS_JOURNAL: `JOB_STATUS_ERROR_SKIPS_HAS_ISSUES`).

    Три РАЗНЫХ исхода, которые нельзя смешивать:
    - есть дефекты          → `has_issues` = разбивка (как и раньше);
    - верификация чистая    → ключей нет вовсе (поведение `done` не меняется);
    - верификации не было   → `has_issues_unknown = True` (НЕ нули в `has_issues`).

    Флаги СОВМЕСТИМЫ и считаются НЕЗАВИСИМО (раньше был `elif`): разбивка теперь может
    появиться и от потерь прогона (`result["losses"]`) при отсутствующей верификации —
    и тогда «есть потери» не должно затирать «верификации не было».
    Для старых путей поведение не меняется: там разбивка непуста только если есть
    `*.summary.errors`, а это и есть наличие данных верификации.
    """
    if not isinstance(data, dict) or (kind or "") not in CREATE_JOB_KINDS:
        return
    breakdown = compute_job_issues_breakdown(kind, data)
    if breakdown:
        data["has_issues"] = breakdown
    if not has_verification_data(data):
        data["has_issues_unknown"] = True

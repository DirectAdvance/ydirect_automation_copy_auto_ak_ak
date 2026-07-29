"""Post-create verification and safe repair orchestration for create_set."""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

from ..repair import repair_auto as rauto
from ..repair import repair_gate as rgate
from ..verifier import verify_create_set


LiveVerification = Callable[[str, list[dict[str, Any]]], dict[str, Any]]
RepairDepsFactory = Callable[[], dict[str, Any]]
PostVerify = Callable[[dict[str, Any], str, dict[str, Any]], dict[str, Any]]

# Задача #17 (робастность): постпроцесс (verify + live-verification по куке + safe-repair) делает
# СЕТЕВЫЕ вызовы (Grid/UAC HTTP, Victory-DB). Даже с тайт-таймаутами на самих операциях суммарная
# деградация может тянуться минутами и морозить поток воркера → джоба висит running, done=N/N не
# флипается в done. Таймбокс = последний предохранитель: постпроцесс исполняется в daemon-потоке,
# основной поток ждёт join(budget). Не уложились → возвращаем degraded-результат и ОТПУСКАЕМ воркер
# (созданные РК уже есть; добивку/верификацию подхватит delayed-репэйр демон свежей Grid-проверкой).
# Orphan-поток дожимает свой bounded-таймаут (HTTP ≤180с, DB statement_timeout/keepalives ≤~120с) и
# умирает сам. Бюджет env-tunable; дефолт 600с — потолок для здорового-но-медленного постпроцесса.
_POSTPROCESS_TIME_BUDGET_SECONDS = float(
    os.environ.get("DIRECT_POSTPROCESS_BUDGET_SEC", "600"))


def _degraded_postprocess(status: str, note: str) -> dict[str, Any]:
    """Безопасный degraded-результат: все 4 ключа на месте (orchestrator читает через .get()),
    live_verification без repair_plan → авто-recreate не запускается, delayed-демон переверит позже."""
    return {
        "verification": {"status": status, "error": note},
        "live_verification": {"status": status, "prefer_grid": True, "error": note},
        "repair_gate": {"status": status, "error": note},
        "auto_repair": None,
    }


def run_create_set_postprocess(*, login: str, items: list[dict[str, Any]],
                               results: list[dict[str, Any]], body: dict[str, Any],
                               agent: str, counter_id: int | None, goal_id: int | None,
                               site_type: str, agency: str, promo_note: str | None,
                               callouts_note: str | None, callouts: list[str],
                               live_verification: LiveVerification,
                               repair_deps: RepairDepsFactory,
                               post_verify: PostVerify) -> dict[str, Any]:
    """Timeboxed wrapper: постпроцесс НИКОГДА не морозит поток воркера дольше бюджета."""
    box: dict[str, Any] = {}

    def _work() -> None:
        try:
            box["out"] = _run_create_set_postprocess_body(
                login=login, items=items, results=results, body=body, agent=agent,
                counter_id=counter_id, goal_id=goal_id, site_type=site_type, agency=agency,
                promo_note=promo_note, callouts_note=callouts_note, callouts=callouts,
                live_verification=live_verification, repair_deps=repair_deps,
                post_verify=post_verify,
            )
        except Exception as e:  # noqa: BLE001 — постпроцесс не должен ронять набор
            box["out"] = _degraded_postprocess("error", str(e)[:200])

    _t0 = time.time()
    t = threading.Thread(target=_work, name="create-set-postprocess", daemon=True)
    t.start()
    t.join(_POSTPROCESS_TIME_BUDGET_SECONDS)
    if t.is_alive():
        waited = int(time.time() - _t0)
        print(f"[postprocess-timebox] {login}: постпроцесс завис >{waited}с "
              f"(budget={int(_POSTPROCESS_TIME_BUDGET_SECONDS)}с) → отпускаю воркер, degraded",
              flush=True)
        out = _degraded_postprocess("timeboxed", f"postprocess timebox {waited}s")
        out["postprocess_timeboxed"] = {
            "waited_seconds": waited,
            "budget_seconds": int(_POSTPROCESS_TIME_BUDGET_SECONDS),
        }
        return out
    return box.get("out") or _degraded_postprocess("error", "postprocess produced no result")


def _run_create_set_postprocess_body(*, login: str, items: list[dict[str, Any]],
                                     results: list[dict[str, Any]], body: dict[str, Any],
                                     agent: str, counter_id: int | None, goal_id: int | None,
                                     site_type: str, agency: str, promo_note: str | None,
                                     callouts_note: str | None, callouts: list[str],
                                     live_verification: LiveVerification,
                                     repair_deps: RepairDepsFactory,
                                     post_verify: PostVerify) -> dict[str, Any]:
    """Build static/live verification, repair-gate and safe post-create repair."""
    try:
        verification = verify_create_set(
            login=login,
            items=items,
            results=results,
            promo_note=promo_note,
            callouts_note=callouts_note,
            callouts=callouts,
            body={
                **body,
                "items": items,
                "agent": agent,
                "counter_id": counter_id,
                "goal_id": goal_id,
                "site_type": site_type,
            },
        )
    except Exception as e:  # noqa: BLE001 - verification must not break create_set
        verification = {"status": "error", "error": str(e)[:200]}

    try:
        live_report = live_verification(login, results)
    except Exception as e:  # noqa: BLE001 - live verification must not break create_set
        live_report = {"status": "error", "error": str(e)[:200], "prefer_grid": True}

    try:
        repair_gate_summary = rgate.summarize_repair_gate(
            body,
            results,
            (live_report or {}).get("repair_plan") or {},
        )
    except Exception as e:  # noqa: BLE001 - summary must not break create_set
        repair_gate_summary = {"status": "error", "error": str(e)[:200]}

    auto_repair = None
    try:
        plan = (live_report or {}).get("repair_plan") or {}
        can_auto = (
            not body.get("_repair_parent_job_id")
            and not body.get("_deferred_id")
            and not body.get("_skip_auto_post_repair")
            and int((repair_gate_summary or {}).get("executable_now") or 0) > 0
        )
        if can_auto:
            repair_ctx = {
                "login": login,
                "agency": agency,
                "body": body,
                "results": results,
            }
            auto_repair = rauto.execute_safe_post_create(
                login,
                repair_ctx,
                plan,
                repair_deps(),
                post_verify=post_verify,
            )
            post_live = (auto_repair or {}).get("post_repair_live_verification")
            if post_live:
                live_report = post_live
                repair_gate_summary = rgate.summarize_repair_gate(
                    body,
                    results,
                    (live_report or {}).get("repair_plan") or {},
                )
    except Exception as e:  # noqa: BLE001 - auto repair must not break create_set
        auto_repair = {"ok": False, "error": str(e)[:220], "uses_direct_units": False}

    return {
        "verification": verification,
        "live_verification": live_report,
        "repair_gate": repair_gate_summary,
        "auto_repair": auto_repair,
    }

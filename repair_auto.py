"""Orchestration helpers for post-create repair execution.

This layer keeps the Flask blueprint from knowing the exact action ordering.
It still receives IO callbacks/deps from the blueprint, so imports stay light
and testable.
"""
from __future__ import annotations

from typing import Any, Callable

from . import repair_executor as rex
from . import repair_gate as rgate


PostVerify = Callable[[dict, str, dict], dict]
DeleteUac = Callable[[str, str, list[dict[str, Any]]], dict[str, Any]]
DeleteSearchDraft = Callable[[str, str, list[dict[str, Any]]], dict[str, Any]]
CreateJob = Callable[[int, str, dict[str, Any], dict[str, Any], bool], str]
JobsAhead = Callable[[str], int]


def _with_common(out: dict, *, job_id: str, plan: dict[str, Any],
                 executed: list[dict[str, Any]], unsupported: list[dict[str, Any]]) -> dict:
    out.update({
        "job_id": job_id,
        "repair_plan": plan,
        "executed_actions": executed[:40],
        "unsupported_actions": unsupported[:40],
    })
    return out


def execute_next_in_place(login: str, ctx: dict, plan: dict[str, Any], deps: rex.RepairDeps,
                          *, job_id: str = "", post_verify: PostVerify | None = None) -> tuple[dict, int] | None:
    """Execute one supported in-place repair action type.

    Order matches the existing endpoint contract: content first, then promo,
    callouts, rename. Recreate/UAC replace is intentionally not handled here
    because it is async queue work.
    """
    body = ctx.get("body") or {}
    results = ctx.get("results") or []

    content_repairs, content_unsupported = rgate.executable_content_repairs(body, plan or {})
    if content_repairs:
        out, status = rex.execute_content_repair(login, ctx, content_repairs, deps)
        if post_verify:
            post_verify(out, login, ctx)
        return _with_common(
            out,
            job_id=job_id,
            plan=plan,
            executed=[{k: v for k, v in a.items() if k != "item"} for a in content_repairs],
            unsupported=content_unsupported,
        ), status

    # keywords_repair: заливаем через Grid AddKeywords (НЕ UpdateUnifiedAdGroups — тот no-op для ключей).
    # Для NO_KEYWORDS_LIVE пробуем дозалить ключи in-place; recreate только если не помогло.
    keyword_ids, keyword_actions, keyword_unsupported = rgate.executable_keywords_repairs(plan or {})
    if keyword_ids and keyword_actions:
        out, status = rex.execute_keywords_repair(login, ctx, keyword_ids, deps)
        if post_verify:
            post_verify(out, login, ctx)
        return _with_common(
            out,
            job_id=job_id,
            plan=plan,
            executed=[{k: v for k, v in a.items()} for a in keyword_actions],
            unsupported=keyword_unsupported,
        ), status

    images_ids, images_actions, images_unsupported = rgate.executable_images_repairs(plan or {})
    if images_ids and images_actions:
        out, status = rex.execute_images_repair(login, ctx, images_ids, deps)
        if post_verify:
            post_verify(out, login, ctx)
        return _with_common(
            out,
            job_id=job_id,
            plan=plan,
            executed=[{k: v for k, v in a.items()} for a in images_actions],
            unsupported=images_unsupported,
        ), status

    img_forbidden_ids, img_forbidden_actions, img_forbidden_unsupported = rgate.executable_images_forbidden_repairs(plan or {})
    if img_forbidden_ids and img_forbidden_actions:
        out, status = rex.execute_images_forbidden_repair(login, ctx, img_forbidden_ids, deps)
        if post_verify:
            post_verify(out, login, ctx)
        return _with_common(
            out,
            job_id=job_id,
            plan=plan,
            executed=[{k: v for k, v in a.items()} for a in img_forbidden_actions],
            unsupported=img_forbidden_unsupported,
        ), status

    adprice_ids, adprice_actions, adprice_unsupported = rgate.executable_adprice_repairs(plan or {})
    if adprice_ids and adprice_actions:
        out, status = rex.execute_adprice_repair(login, ctx, adprice_ids, deps)
        if post_verify:
            post_verify(out, login, ctx)
        return _with_common(
            out,
            job_id=job_id,
            plan=plan,
            executed=[{k: v for k, v in a.items()} for a in adprice_actions],
            unsupported=adprice_unsupported,
        ), status

    dt_ids, dt_actions, dt_unsupported = rgate.executable_default_text_repairs(plan or {})
    if dt_ids and dt_actions:
        out, status = rex.execute_default_text_repair(login, ctx, dt_ids, deps)
        if post_verify:
            post_verify(out, login, ctx)
        return _with_common(
            out,
            job_id=job_id,
            plan=plan,
            executed=[{k: v for k, v in a.items()} for a in dt_actions],
            unsupported=dt_unsupported,
        ), status

    promo_ids, promo_actions, promo_unsupported = rgate.executable_promo_campaign_ids(results, plan or {})
    if promo_ids and promo_actions:
        out, status = rex.execute_promo_repair(login, ctx, promo_ids, deps)
        if post_verify:
            post_verify(out, login, ctx)
        return _with_common(
            out,
            job_id=job_id,
            plan=plan,
            executed=promo_actions,
            unsupported=promo_unsupported,
        ), status

    callout_ids, callout_actions, callout_unsupported = rgate.executable_callout_campaign_ids(results, plan or {})
    if callout_ids and callout_actions:
        out, status = rex.execute_callouts_repair(login, ctx, callout_ids, deps)
        if post_verify:
            post_verify(out, login, ctx)
        return _with_common(
            out,
            job_id=job_id,
            plan=plan,
            executed=callout_actions,
            unsupported=callout_unsupported,
        ), status

    rename_names, rename_actions, rename_unsupported = rgate.executable_rename_campaigns(plan or {})
    if rename_names and rename_actions:
        out, status = rex.execute_rename_repair(login, ctx, rename_names, deps)
        if post_verify:
            post_verify(out, login, ctx)
        return _with_common(
            out,
            job_id=job_id,
            plan=plan,
            executed=rename_actions,
            unsupported=rename_unsupported,
        ), status

    return None


def execute_safe_post_create(login: str, ctx: dict, plan: dict[str, Any], deps: rex.RepairDeps,
                             *, post_verify: PostVerify | None = None) -> dict:
    """Execute idempotent post-create repairs once.

    Content rebuild is excluded here: immediately after create_set, Grid can
    lag and a false ``NO_ADS_LIVE`` could duplicate groups. Recreate/UAC replace
    also stays in queue/manual repair because it deletes or creates campaigns.
    """
    body = ctx.get("body") or {}
    results = ctx.get("results") or []
    executed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []

    promo_ids, promo_actions, _ = rgate.executable_promo_campaign_ids(results, plan or {})
    if promo_ids and promo_actions:
        out, status = rex.execute_promo_repair(login, ctx, promo_ids, deps)
        outputs.append({"action": "create_or_attach_promo", "status": status, "result": out})
        if 200 <= int(status) < 300 and out.get("ok"):
            executed.extend(promo_actions)
        else:
            failed.append({"action": "create_or_attach_promo", "status": status, "result": out})

    callout_ids, callout_actions, _ = rgate.executable_callout_campaign_ids(results, plan or {})
    if callout_ids and callout_actions:
        out, status = rex.execute_callouts_repair(login, ctx, callout_ids, deps)
        outputs.append({"action": "ensure_callouts", "status": status, "result": out})
        if 200 <= int(status) < 300 and out.get("ok"):
            executed.extend(callout_actions)
        else:
            failed.append({"action": "ensure_callouts", "status": status, "result": out})

    # keywords_repair удалён из safe-post-create пути: UpdateUnifiedAdGroups — подтверждённый no-op
    # (ключи не добавляются). NO_KEYWORDS_LIVE → resume_or_recreate_campaign (плановый recreate,
    # не in-place). execute_keywords_repair в repair_executor сохранён, но авто-вызов убран.

    rename_names, rename_actions, _ = rgate.executable_rename_campaigns(plan or {})
    if rename_names and rename_actions:
        out, status = rex.execute_rename_repair(login, ctx, rename_names, deps)
        outputs.append({"action": "rename_campaign", "status": status, "result": out})
        if 200 <= int(status) < 300 and out.get("ok"):
            executed.extend(rename_actions)
        else:
            failed.append({"action": "rename_campaign", "status": status, "result": out})

    summary = {
        "ok": not failed,
        "executed": len(executed),
        "failed": len(failed),
        "executed_actions": executed[:40],
        "failed_actions": failed[:20],
        "results": outputs[:20],
        "skipped_actions": [
            "rebuild_missing_content",   # отложено на delayed-цикл (Grid-лаг после create)
            "resume_or_recreate_campaign",
            "keywords_repair",    # отложено на delayed-цикл (в safe-post-create Grid ещё не достиг консистентности)
            "images_repair",
            "adprice_repair",
            "default_text_repair",
            "campaign_invariant_repair",   # отложено на delayed-цикл (Grid-лаг кампанийных полей после create)
        ],
        "uses_direct_units": False,
    }
    if outputs and post_verify:
        post_verify(summary, login, ctx)
    if not outputs:
        summary["ok"] = True
        summary["note"] = "нет безопасных post-create in-place действий"
    return summary


def _plan_actions_of(plan: dict[str, Any], action_type: str) -> list[dict[str, Any]]:
    return [
        a for a in (plan or {}).get("actions") or []
        if isinstance(a, dict) and a.get("action") == action_type
    ]


def _actions_use_units(actions: list[dict[str, Any]]) -> bool:
    return any(rgate.truthy(a.get("uses_direct_units")) for a in actions or [])


def execute_all_in_place(login: str, ctx: dict, plan: dict[str, Any], deps: rex.RepairDeps,
                         *, post_verify: PostVerify | None = None) -> dict:
    """Execute ALL executable in-place repair action types once from a single plan snapshot.

    Unlike ``execute_safe_post_create`` this ALSO performs ``rebuild_missing_content``: it is
    meant for the delayed post-done cycle, when Grid has already caught up (the create-time
    Grid-lag hazard that keeps content repair out of the synchronous path no longer applies).
    Recreate/UAC-replace stays with the async queue path (``_auto_queue_recreate_after_done``).

    ``uses_direct_units`` actions are gated: they are recorded in ``units_gated`` and skipped,
    never executed automatically (in-place executors are Grid-only, so this is defensive).
    """
    body = ctx.get("body") or {}
    results = ctx.get("results") or []
    executed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    units_gated: list[dict[str, Any]] = []

    content_repairs, _ = rgate.executable_content_repairs(body, plan or {})
    if content_repairs:
        if _actions_use_units(_plan_actions_of(plan, "rebuild_missing_content")):
            units_gated.append({"action": "rebuild_missing_content", "reason": "uses_direct_units"})
        else:
            out, status = rex.execute_content_repair(login, ctx, content_repairs, deps)
            outputs.append({"action": "rebuild_missing_content", "status": status, "result": out})
            if 200 <= int(status) < 300 and out.get("ok"):
                executed.extend([{k: v for k, v in a.items() if k != "item"} for a in content_repairs])
            else:
                failed.append({"action": "rebuild_missing_content", "status": status, "result": out})

    keyword_ids, keyword_actions, _ = rgate.executable_keywords_repairs(plan or {})
    if keyword_ids and keyword_actions:
        if _actions_use_units(keyword_actions):
            units_gated.append({"action": "keywords_repair", "reason": "uses_direct_units"})
        else:
            out, status = rex.execute_keywords_repair(login, ctx, keyword_ids, deps)
            outputs.append({"action": "keywords_repair", "status": status, "result": out})
            if 200 <= int(status) < 300 and out.get("ok"):
                executed.extend([{k: v for k, v in a.items()} for a in keyword_actions])
            else:
                failed.append({"action": "keywords_repair", "status": status, "result": out})

    images_ids, images_actions, _ = rgate.executable_images_repairs(plan or {})
    if images_ids and images_actions:
        if _actions_use_units(images_actions):
            units_gated.append({"action": "images_repair", "reason": "uses_direct_units"})
        else:
            out, status = rex.execute_images_repair(login, ctx, images_ids, deps)
            outputs.append({"action": "images_repair", "status": status, "result": out})
            if 200 <= int(status) < 300 and out.get("ok"):
                executed.extend([{k: v for k, v in a.items()} for a in images_actions])
            else:
                failed.append({"action": "images_repair", "status": status, "result": out})

    img_forbidden_ids, img_forbidden_actions, _ = rgate.executable_images_forbidden_repairs(plan or {})
    if img_forbidden_ids and img_forbidden_actions:
        if _actions_use_units(img_forbidden_actions):
            units_gated.append({"action": "images_forbidden_repair", "reason": "uses_direct_units"})
        else:
            out, status = rex.execute_images_forbidden_repair(login, ctx, img_forbidden_ids, deps)
            outputs.append({"action": "images_forbidden_repair", "status": status, "result": out})
            if 200 <= int(status) < 300 and out.get("ok"):
                executed.extend([{k: v for k, v in a.items()} for a in img_forbidden_actions])
            else:
                failed.append({"action": "images_forbidden_repair", "status": status, "result": out})

    adprice_ids, adprice_actions, _ = rgate.executable_adprice_repairs(plan or {})
    if adprice_ids and adprice_actions:
        if _actions_use_units(adprice_actions):
            units_gated.append({"action": "adprice_repair", "reason": "uses_direct_units"})
        else:
            out, status = rex.execute_adprice_repair(login, ctx, adprice_ids, deps)
            outputs.append({"action": "adprice_repair", "status": status, "result": out})
            if 200 <= int(status) < 300 and out.get("ok"):
                executed.extend([{k: v for k, v in a.items()} for a in adprice_actions])
            else:
                failed.append({"action": "adprice_repair", "status": status, "result": out})

    dt_ids, dt_actions, _ = rgate.executable_default_text_repairs(plan or {})
    if dt_ids and dt_actions:
        if _actions_use_units(dt_actions):
            units_gated.append({"action": "default_text_repair", "reason": "uses_direct_units"})
        else:
            out, status = rex.execute_default_text_repair(login, ctx, dt_ids, deps)
            outputs.append({"action": "default_text_repair", "status": status, "result": out})
            if 200 <= int(status) < 300 and out.get("ok"):
                executed.extend([{k: v for k, v in a.items()} for a in dt_actions])
            else:
                failed.append({"action": "default_text_repair", "status": status, "result": out})

    inv_ids, inv_actions, _ = rgate.executable_campaign_invariant_repairs(plan or {})
    if inv_ids and inv_actions:
        if _actions_use_units(inv_actions):
            units_gated.append({"action": "campaign_invariant_repair", "reason": "uses_direct_units"})
        else:
            out, status = rex.execute_campaign_invariant_repair(login, ctx, inv_ids, deps)
            outputs.append({"action": "campaign_invariant_repair", "status": status, "result": out})
            if 200 <= int(status) < 300 and out.get("ok"):
                executed.extend([{k: v for k, v in a.items()} for a in inv_actions])
            else:
                failed.append({"action": "campaign_invariant_repair", "status": status, "result": out})

    promo_ids, promo_actions, _ = rgate.executable_promo_campaign_ids(results, plan or {})
    if promo_ids and promo_actions:
        if _actions_use_units(promo_actions):
            units_gated.append({"action": "create_or_attach_promo", "reason": "uses_direct_units"})
        else:
            out, status = rex.execute_promo_repair(login, ctx, promo_ids, deps)
            outputs.append({"action": "create_or_attach_promo", "status": status, "result": out})
            if 200 <= int(status) < 300 and out.get("ok"):
                executed.extend(promo_actions)
            else:
                failed.append({"action": "create_or_attach_promo", "status": status, "result": out})

    callout_ids, callout_actions, _ = rgate.executable_callout_campaign_ids(results, plan or {})
    if callout_ids and callout_actions:
        if _actions_use_units(callout_actions):
            units_gated.append({"action": "ensure_callouts", "reason": "uses_direct_units"})
        else:
            out, status = rex.execute_callouts_repair(login, ctx, callout_ids, deps)
            outputs.append({"action": "ensure_callouts", "status": status, "result": out})
            if 200 <= int(status) < 300 and out.get("ok"):
                executed.extend(callout_actions)
            else:
                failed.append({"action": "ensure_callouts", "status": status, "result": out})

    rename_names, rename_actions, _ = rgate.executable_rename_campaigns(plan or {})
    if rename_names and rename_actions:
        if _actions_use_units(rename_actions):
            units_gated.append({"action": "rename_campaign", "reason": "uses_direct_units"})
        else:
            out, status = rex.execute_rename_repair(login, ctx, rename_names, deps)
            outputs.append({"action": "rename_campaign", "status": status, "result": out})
            if 200 <= int(status) < 300 and out.get("ok"):
                executed.extend(rename_actions)
            else:
                failed.append({"action": "rename_campaign", "status": status, "result": out})

    summary = {
        "ok": not failed,
        "executed": len(executed),
        "failed": len(failed),
        "executed_actions": executed[:40],
        "failed_actions": failed[:20],
        "results": outputs[:20],
        "units_gated": units_gated[:10],
        "uses_direct_units": False,
    }
    if outputs and post_verify:
        post_verify(summary, login, ctx)
    return summary


def recreate_force_names(items: list[dict[str, Any]], uac_replacements: list[dict[str, Any]],
                         search_deletions: list[dict[str, Any]] | None = None) -> list[str]:
    """Build force-name list for UAC replace items and deleted search campaigns.

    UAC replace names force-recreate existing items whose current name matches the replaced
    campaign. Search deletion names are added so that even if Grid cache still shows the
    deleted campaign, the resume step does not skip it.
    """
    repl_names = [
        str(r.get("name") or "").strip()
        for r in uac_replacements or []
        if str(r.get("name") or "").strip()
    ]
    del_names = [
        str(r.get("name") or "").strip()
        for r in search_deletions or []
        if str(r.get("name") or "").strip()
    ]
    force_names = list(repl_names) + del_names
    for it in items or []:
        item_name = str((it or {}).get("name") or "").strip()
        if item_name and any(
            rn == item_name
            or rn.startswith(item_name + " — ")
            or item_name.startswith(rn + " — ")
            for rn in repl_names
        ):
            force_names.append(item_name)
    return force_names


def build_recreate_queue_body(body: dict[str, Any], items: list[dict[str, Any]],
                              uac_replacements: list[dict[str, Any]], *,
                              parent_job_id: str,
                              search_deletions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build the async recreate repair body used by manual and automatic queueing."""
    repair_body = rgate.repair_queue_body(
        body,
        items,
        parent_job_id=parent_job_id,
        force_names=recreate_force_names(items, uac_replacements, search_deletions),
    )
    repair_body["_skip_auto_post_repair"] = True
    repair_body["_skip_auto_queued_repair"] = True
    return repair_body


def auto_recreate_request(parent_job_id: str, job_snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Return queue inputs for automatic recreate after a parent job is done."""
    body = job_snapshot.get("body") if isinstance(job_snapshot.get("body"), dict) else {}
    if (
        body.get("_repair_parent_job_id")
        or body.get("_deferred_id")
        or body.get("_skip_auto_queued_repair")
    ):
        return None

    result = job_snapshot.get("result") if isinstance(job_snapshot.get("result"), dict) else {}
    if not result or result.get("error"):
        return None

    live = result.get("live_verification") if isinstance(result.get("live_verification"), dict) else {}
    plan = live.get("repair_plan") if isinstance(live.get("repair_plan"), dict) else {}
    if not plan or plan.get("status") != "actionable":
        return None

    try:
        summary = rgate.summarize_repair_gate(body, result.get("results") or [], plan)
    except Exception:  # noqa: BLE001
        summary = {}
    if int((summary or {}).get("queued_recreate_items") or 0) <= 0:
        return None

    # Если в плане есть requires_campaign_delete (NO_KEYWORDS_LIVE / WRONG_AUTOTARGET) — авто-удаление
    # существующей поисковой кампании рискованно без явного подтверждения. Такие элементы видны в
    # repair_plan.actions (recreate_delete_campaigns в summarize) и исполняются только при
    # body._auto_recreate_with_delete=true (или через явный POST на /create_set_repair).
    if int((summary or {}).get("recreate_delete_campaigns") or 0) > 0:
        if not rgate.truthy(body.get("_auto_recreate_with_delete")):
            return {
                "queued": False,
                "source": "auto_after_done",
                "requires_explicit_trigger": True,
                "recreate_delete_campaigns": int((summary or {}).get("recreate_delete_campaigns") or 0),
                "note": ("авто-recreate заблокирован: план содержит requires_campaign_delete "
                         "(NO_KEYWORDS_LIVE/WRONG_AUTOTARGET) — нужен явный триггер "
                         "или body._auto_recreate_with_delete=true"),
                "uses_direct_units": False,
            }

    login = (job_snapshot.get("login") or result.get("login") or "").strip()
    if not login:
        return {
            "queued": False,
            "source": "auto_after_done",
            "error": "login не сохранён в job",
            "uses_direct_units": False,
        }

    return {
        "source": "auto_after_done",
        "login": login,
        "ctx": {
            "login": login,
            "agency": (job_snapshot.get("agency") or body.get("agency") or ""),
            "body": body,
            "results": result.get("results") or [],
        },
        "plan": plan,
        "parent_job_id": parent_job_id,
        "saved_session": job_snapshot.get("session") or {},
        "summary": summary,
    }


def delayed_content_repair_request(parent_job_id: str, job_snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Return scheduling inputs for guarded delayed content repair.

    ПРАВКА A: для нерекрейт-оригинальных джоб всегда планирует один пост-create проход,
    даже при inplace_actions==0 — spec_audit (кнопка/видео/короткие заголовки/фильтр каталога)
    найдёт спек-дефекты независимо от repair_plan.status. Гейты plan.status/inplace_actions
    сняты. Дедуп обеспечен ON CONFLICT (parent_job_id, kind) DO NOTHING в _delayed_content_repair_save.
    """
    body = job_snapshot.get("body") if isinstance(job_snapshot.get("body"), dict) else {}
    # Ранний пропуск: deferred/skip-флаги — сохранены без изменений (ПРАВКА A).
    # _repair_parent_job_id (recreate-джобы) — РАЗРЕШЁН: планируем ОДИН ре-аудит
    # (kind='content_repair_post_recreate') чтобы подтвердить фикс. fix_generic_fallback_group
    # при этом пропускается (skip_recreate=True в _run_delayed_content_repair) — антицикл.
    if (
        body.get("_deferred_id")
        or body.get("_skip_auto_post_repair")
        or body.get("_skip_delayed_content_repair")
    ):
        return None
    is_post_recreate = bool(body.get("_repair_parent_job_id"))

    result = job_snapshot.get("result") if isinstance(job_snapshot.get("result"), dict) else {}
    if not result or result.get("error"):
        return None

    # inplace_actions — метрика для лога, НЕ гейт планирования: добивка планируется ВСЕГДА,
    # spec_audit найдёт спек-дефекты даже когда plan.status != 'actionable'.
    live = result.get("live_verification") if isinstance(result.get("live_verification"), dict) else {}
    plan = live.get("repair_plan") if isinstance(live.get("repair_plan"), dict) else {}
    try:
        summary = rgate.summarize_repair_gate(body, result.get("results") or [], plan)
    except Exception:  # noqa: BLE001
        summary = {}
    inplace_actions = (
        int((summary or {}).get("in_place_content_repairs") or 0)
        + int((summary or {}).get("keyword_repair_campaigns") or 0)
        + int((summary or {}).get("images_repair_campaigns") or 0)
        + int((summary or {}).get("images_forbidden_repair_campaigns") or 0)
        + int((summary or {}).get("adprice_repair_campaigns") or 0)
        + int((summary or {}).get("default_text_repair_campaigns") or 0)
        + (1 if (summary or {}).get("promo_campaigns") else 0)
        + (1 if (summary or {}).get("callout_campaigns") else 0)
        + int((summary or {}).get("rename_campaigns") or 0)
    )

    login = (job_snapshot.get("login") or result.get("login") or "").strip()
    if not login:
        return {
            "scheduled": False,
            "source": "delayed_after_done",
            "error": "login не сохранён в job",
            "uses_direct_units": False,
        }

    return {
        "source": "delayed_after_done",
        "login": login,
        "agency": (job_snapshot.get("agency") or body.get("agency") or ""),
        "parent_job_id": parent_job_id,
        "content_repairs": int((summary or {}).get("in_place_content_repairs") or 0),
        "inplace_actions": inplace_actions,
        "summary": summary,
        "post_recreate": is_post_recreate,
        "uses_direct_units": False,
    }


def queue_recreate_repair_job(login: str, ctx: dict[str, Any], plan: dict[str, Any], *,
                              parent_job_id: str, saved_session: dict[str, Any],
                              delete_uac: DeleteUac, create_job: CreateJob,
                              jobs_ahead: JobsAhead, dedup_login: bool = True,
                              delete_search_draft: "DeleteSearchDraft | None" = None,
                              units_alive: "Callable[..., Any] | None" = None) -> dict[str, Any]:
    """Queue resume/recreate repair items. Транспорт (Семён 2026-07-07, «баллы первичны»):
    если баллы ЖИВЫ — recreate идёт ТОКЕНОМ (via_cookie снимается; на 152 сам упадёт в cookie),
    иначе (реальный 152 / токен не читается) — по куке, как раньше."""
    body = ctx.get("body") or {}
    items, unsupported = rgate.executable_recreate_items(body, plan or {})
    if not items:
        return {"queued": False, "reason": "no_recreate_items", "unsupported_actions": unsupported[:40]}

    uac_replacements = rgate.executable_uac_replace_campaigns(plan or {})
    deleted_uac = {"deleted": [], "failed": []}
    if uac_replacements:
        deleted_uac = delete_uac(
            login,
            (ctx.get("agency") or body.get("agency") or ""),
            uac_replacements,
        )
        if deleted_uac.get("failed"):
            return {
                "queued": False,
                "error": "не удалось удалить неполный UAC draft перед recreate",
                "failed_campaigns": deleted_uac.get("failed")[:40],
                "deleted_campaigns": deleted_uac.get("deleted")[:40],
                "uses_direct_units": False,
            }

    # Поисковые кампании с requires_campaign_delete (NO_KEYWORDS_LIVE/WRONG_AUTOTARGET):
    # удаляются ТОЛЬКО если callback передан и campaign находится в статусе DRAFT.
    search_deletions = rgate.executable_recreate_delete_campaigns(plan or {})
    deleted_search: dict[str, Any] = {"deleted": [], "failed": [], "blocked_non_draft": []}
    if search_deletions and delete_search_draft is not None:
        deleted_search = delete_search_draft(
            login,
            (ctx.get("agency") or body.get("agency") or ""),
            search_deletions,
        )
        # blocked_non_draft → ничего не удалено (консервативный блок) → безопасный аборт.
        # failed без deleted → удаления не произошло вовсе → безопасный аборт.
        # failed при непустом deleted → часть кампаний УЖЕ удалена → recreate ОБЯЗАН идти:
        #   возврат queued:False здесь привёл бы к безвозвратной потере удалённых кампаний
        #   (delete+recreate не атомарны). Продолжаем; deleted names попадут в _repair_force_names
        #   через build_recreate_queue_body/recreate_force_names(search_deletions=...).
        _safe_to_abort = bool(
            deleted_search.get("blocked_non_draft")
            or (deleted_search.get("failed") and not deleted_search.get("deleted"))
        )
        if _safe_to_abort:
            return {
                "queued": False,
                "error": "не удалось удалить поисковые DRAFT-кампании перед recreate",
                "failed_campaigns": (deleted_search.get("failed") or [])[:40],
                "blocked_non_draft": (deleted_search.get("blocked_non_draft") or [])[:40],
                "deleted_search_campaigns": (deleted_search.get("deleted") or [])[:40],
                "uses_direct_units": False,
            }

    repair_body = build_recreate_queue_body(
        body,
        items,
        uac_replacements,
        parent_job_id=parent_job_id,
        search_deletions=search_deletions if (search_deletions and delete_search_draft) else None,
    )
    # «Баллы первичны» (Семён 2026-07-07): при живых баллах recreate идёт ТОКЕНОМ — снимаем
    # принудительный via_cookie (build_recreate_queue_body ставит его по умолчанию). На 152
    # оркестратор сам переключит остаток на куку. None/False (реальный 152 / нет токена) → по куке.
    _alive = None
    try:
        _alive = units_alive(login, (ctx.get("agency") or body.get("agency") or "")) if units_alive else None
    except Exception:  # noqa: BLE001
        _alive = None
    if _alive:
        repair_body.pop("via_cookie", None)
    _transport = "token_v501" if _alive else "cookie_grid"
    new_job_id = create_job(len(items), login, repair_body, saved_session, dedup_login)
    return {
        "queued": True,
        "new_job_id": new_job_id,
        "queued_items": len(items),
        "ahead": jobs_ahead(new_job_id),
        "transport": _transport,
        "uses_direct_units": bool(_alive),
        "deleted_uac_campaigns": deleted_uac.get("deleted")[:40],
        "deleted_search_campaigns": (deleted_search.get("deleted") or [])[:40],
        "unsupported_actions": unsupported[:40],
    }


def recreate_queue_preflight(login: str, body: dict[str, Any], plan: dict[str, Any],
                             active_jobs: dict[str, dict[str, Any]],
                             terminal_statuses: set[str]) -> dict[str, Any]:
    """Return recreate items and an active-job conflict, without mutating state."""
    items, unsupported = rgate.executable_recreate_items(body or {}, plan or {})
    conflict = None
    if items:
        for job_id, job in (active_jobs or {}).items():
            if (
                job.get("status") not in terminal_statuses
                and (job.get("login") or "").strip() == login
            ):
                conflict = {
                    "error": f"у аккаунта {login} уже есть активная job — repair не поставлен",
                    "active_job_id": job_id,
                }
                break
    return {
        "items": items,
        "unsupported_actions": unsupported[:40],
        "conflict": conflict,
    }


def recreate_queue_failure_response(job_id: str, login: str, queued: dict[str, Any],
                                    plan: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Format the failed recreate queue response for the repair endpoint."""
    status = 502 if queued.get("error") else 422
    return {
        "error": queued.get("error") or "repair recreate не поставлен",
        "job_id": job_id,
        "login": login,
        "repair_plan": plan,
        **queued,
    }, status


def recreate_queue_success_response(job_id: str, login: str, queued: dict[str, Any],
                                    plan: dict[str, Any],
                                    unsupported: list[dict[str, Any]]) -> dict[str, Any]:
    """Format the successful recreate queue response for the repair endpoint."""
    return {
        "ok": True,
        "execute": True,
        "job_id": job_id,
        "new_job_id": queued.get("new_job_id"),
        "login": login,
        "queued_items": queued.get("queued_items"),
        "ahead": queued.get("ahead"),
        "transport": queued.get("transport"),
        "uses_direct_units": queued.get("uses_direct_units"),
        "deleted_uac_campaigns": queued.get("deleted_uac_campaigns", []),
        "unsupported_actions": queued.get("unsupported_actions", unsupported[:40]),
        "repair_plan": plan,
    }


def no_safe_action_response(job_id: str, plan: dict[str, Any],
                            unsupported: list[dict[str, Any]]) -> dict[str, Any]:
    """Format the no-executable-action response for the repair endpoint."""
    return {
        "error": "в плане нет действий, которые repair executor умеет безопасно выполнить",
        "job_id": job_id,
        "execute": False,
        "repair_plan": plan,
        "unsupported_actions": unsupported[:40],
    }

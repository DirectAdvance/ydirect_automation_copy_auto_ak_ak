"""Flask composition root for Direct Automation.

Domain services, queue state, repositories and Yandex transports live outside this
module. This file owns access policy, Blueprint construction and route registration.
Compatibility exports delegate to automation_runtime during migration.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

from flask import Blueprint

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auth import _service_required_any  # noqa: E402
from . import automation_runtime as _runtime  # noqa: E402

globals().update({
    name: getattr(_runtime, name)
    for name in dir(_runtime)
    if not name.startswith("__")
})

_direct_access = _service_required_any("work", "work:direct")
_direct_minusphrase_access = _service_required_any(
    "work", "work:direct", "work:direct:minusphrase"
)
_direct_danger = _service_required_any("work:direct:danger")

bp = Blueprint(
    "direct",
    __name__,
    url_prefix="/direct",
    template_folder=str(Path(__file__).resolve().parents[1] / "templates"),
)

def init_direct() -> None:
    """Compatibility hook; runtime wiring is import-idempotent."""
    return None

def __getattr__(name: str):
    return getattr(_runtime, name)

from .routes_pages import register_page_routes

register_page_routes(
    bp,
    _direct_access,
    _direct_minusphrase_access,
    render_page=_render_page,
)

from .routes_reference import register_reference_routes

from .routes_settings import register_settings_routes

from .routes_accounts import register_account_routes

from .routes_content import register_content_routes

from .routes_content_editor import register_content_editor_routes

from .routes_ai import register_ai_routes

from .routes_copy import register_copy_routes

from .routes_jobs import register_job_routes

from .routes_create_set import register_create_set_routes

from .routes_overview import register_overview_routes

from .routes_deferred import register_deferred_routes

from .routes_pack import register_pack_routes

from .routes_campaigns import register_campaign_routes

from .routes_set_plan import register_set_plan_routes

from .routes_ready_logins import register_ready_logins_routes

from .routes_slepki_edit import register_slepki_edit_routes

register_slepki_edit_routes(
    bp,
    _direct_access,
    slepki_editor=_sed,
    job_new=_job_new,
    ag_part1_map=_ag_part1_map,
)

register_reference_routes(
    bp,
    _direct_access,
    list_feeds_for_site=cmc.list_feeds_for_site,
    load_json=_json,
    load_audiences=_load_audiences,
    victory_conn=_victory_conn,
    ag_part1_map=_ag_part1_map,
    ui_structure_payload=_ui_structure_payload,
)

register_overview_routes(
    bp,
    _direct_access,
    victory_conn=_victory_conn,
)

register_ready_logins_routes(
    bp,
    _direct_access,
    victory_conn=_victory_conn,
    victory_conn_rw=lambda: _victory_conn_rw(),
    db_init=_ready_logins_db_init,
)

register_settings_routes(
    bp,
    _direct_access,
    global_feed_rules=_global_feed_rules,
    feed_key=_feed_key,
    feed_rules_ensure=_feed_rules_ensure,
    global_minus_places=_global_minus_places,
    minus_places_ensure=_minus_places_ensure,
    place_host=_place_host,
    minus_words_slice=_global_minus_words_slice,
    minus_words_ensure=_minus_words_ensure,
    global_minus_marks=_global_minus_marks,
    minus_marks_ensure=_minus_marks_ensure,
    known_brand_canons=lambda: sorted(_known_brand_canons()),
    global_minus_models=_global_minus_models,
    minus_models_ensure=_minus_models_ensure,
    load_brand_models_catalog=_load_brand_models_catalog,
    victory_conn=_victory_conn,
    victory_conn_rw=_victory_conn_rw,
)

register_deferred_routes(
    bp,
    _direct_access,
    direct_tokens=_direct_tokens,
    token_for_login=_token_for_login,
    v5_units=_v5_units,
    units_per_campaign=_UNITS_PER_CAMPAIGN,
    ensure_resume_daemon=_ensure_resume_daemon,
    victory_conn=_victory_conn,
    deferred_set_status=_deferred_set_status,
    deferred_enqueue_now=_deferred_enqueue_now,
    create_jobs_lock=_CREATE_JOBS_LOCK,
    create_jobs_ahead=_create_jobs_ahead,
)

register_content_routes(
    bp,
    _direct_access,
    kp=kp,
    m3_content_status=_m3_content_status,
    content_rules_map=_content_rules_map,
    content_rule_key=_content_rule_key,
    dedupe_content_assets_for_ui=_dedupe_content_assets_for_ui,
    content_rules_ensure=_content_rules_ensure,
    gc_ct=_gc_ct,
    victory_conn_rw=_victory_conn_rw,
    content_rules_cache=_CONTENT_RULES_CACHE,
)

register_pack_routes(
    bp,
    _direct_access,
    victory_conn=_victory_conn,
    slepok_key_map=_SLEPOK_KEY,
    callout_limits={
        "max_each": _CALLOUT_MAX_EACH,
        "max_total_desktop": _CALLOUT_MAX_TOTAL_DESKTOP,
        "max_total_mobile": _CALLOUT_MAX_TOTAL_MOBILE,
    },
    m3_content_status=_m3_content_status,
    m3_status_response=_m3_status_response,
    pack_preview_response=_pack_preview_response,
    slepok_segment_counts_response=_slepok_segment_counts_response,
    cookies_status_response=_cookies_status_response,
)

register_campaign_routes(
    bp,
    _direct_access,
    _direct_danger,
    campaigns_response=_campaigns_response,
    stop_all_response=_stop_all_response,
    delete_drafts_response=_delete_drafts_response,
    delete_drafts_async_response=_delete_drafts_async_response,
    check_blocks_response=_check_blocks_response,
)

register_account_routes(
    bp,
    _direct_access,
    victory_conn=_victory_conn,
    parse_counter_ids=_parse_counter_ids,
    parse_number=lambda val, default: _num(val, default),
    metrika_goals_for=_metrika_goals_for,
    resolve_region=_resolve_region,
    account_prefill_func=_account_prefill_response,
    account_assets_func=_account_assets_response,
    account_audiences_func=_account_audiences_response,
    balance_func=_do_balance,
    pull_begin=_pull_begin,
    pull_end=_pull_end,
    busy_response=_busy_response,
    cooldowns=_COOLDOWN,
    goal_vse_formy=_goal_vse_formy,
    account_cols=_ACCOUNT_COLS,
    default_status=DEFAULT_STATUS,
    exclude_directologs=_EXCLUDE_DIRECTOLOGS,
)

if os.environ.get("DIRECT_REGISTER_CONTENT_EDITOR", "1") != "0":
    register_content_editor_routes(
        bp,
        _service_required_any("work", "work:direct", "direct:content", "direct"),
        victory_conn=_victory_conn,
        token_for_login=_token_for_login,
        direct_tokens=_direct_tokens,
        v5_call=_v5_call,
        v501_svc=_v501_svc,
        default_status=DEFAULT_STATUS,
        exclude_directologs=_EXCLUDE_DIRECTOLOGS,
    )

register_set_plan_routes(
    bp,
    _direct_access,
    set_plan_response=_set_plan_response,
)

def _wire_copy_routes(target_bp, *, ensure_worker):
    """Единая проводка copy-роутов — вызывается и основным процессом (bp), и отдельным
    copy_main.py (свой fresh bp). ensure_worker разный: _ensure_create_worker в общем
    процессе, _ensure_copy_worker в direct-copy.service (изолированная copy-очередь)."""
    register_copy_routes(
        target_bp,
        _direct_access,
        api_campaigns_func=_campaigns_response,
        account_prefill_func=_account_prefill_response,
        metrika_goals_for=_metrika_goals_for,
        parse_number=_num,
        copy_default_feed_path=_COPY_DEFAULT_FEED_PATH,
        counter_foreign_owner=_counter_foreign_owner,
        resolve_agency_hint=_resolve_agency_hint,
        ensure_create_worker=ensure_worker,
        job_new=_job_new,
        copy_job_upsert=_copy_job_upsert,
        create_jobs_ahead=_create_jobs_ahead,
        create_jobs=_CREATE_JOBS,
        create_jobs_lock=_CREATE_JOBS_LOCK,
        copy_jobs=_COPY_JOBS,
        copy_jobs_lock=_COPY_JOBS_LOCK,
        feeds_preview_func=_copy_feeds_preview,
    )

if os.environ.get("DIRECT_REGISTER_COPY", "1") != "0":
    _wire_copy_routes(bp, ensure_worker=_ensure_create_worker)

register_job_routes(
    bp,
    _direct_access,
    _direct_danger,
    parse_number=_num,
    metrika_goals_for=_metrika_goals_for,
    counter_foreign_owner=_counter_foreign_owner,
    resolve_agency_hint=_resolve_agency_hint,
    ensure_create_worker=_ensure_create_worker,
    job_new=_job_new,
    create_jobs_ahead=_create_jobs_ahead,
    create_watchdog_tick=_create_watchdog_tick,
    jobs_purge_old=_jobs_purge_old,
    job_agency=_job_agency,
    job_db_save=_job_db_save,
    job_db_delete=_job_db_delete,
    delete_drafts_core=_delete_drafts_core,
    create_jobs=_CREATE_JOBS,
    create_jobs_lock=_CREATE_JOBS_LOCK,
    create_cond=_CREATE_COND,
    create_queue=_CREATE_QUEUE,
    job_terminal=_JOB_TERMINAL,
    job_db_last=_JOB_DB_LAST,
    start_prefetch=_prefetch_start,
    role_is_web=lambda: _direct_role() == "web",
    job_db_get=_job_db_get,
    job_db_ahead=_job_db_ahead,
    job_db_set_status=_job_db_set_status,
    job_control_set=_job_control_set,
    job_db_web_await_feed=_job_db_web_await_feed,
    job_db_web_resolve_feed=_job_db_web_resolve_feed,
    job_db_active_by_login=_job_db_active_by_login,
    job_db_list_recent=_job_db_list_recent,
    cancel_children=_cancel_children_of,
)

register_create_set_routes(
    bp,
    _direct_access,
    create_set_response=_create_set_response,
    legacy_create_response=_legacy_create_response,
    repair_gate=rgate,
    repair_auto=rauto,
    create_set_job_context=_create_set_job_context,
    create_set_live_verification=_create_set_live_verification,
    repair_deps=_repair_deps,
    queue_recreate_repair_job=_queue_recreate_repair_job,
    attach_post_repair_verification=_attach_post_repair_verification,
    ensure_create_worker=_ensure_create_worker,
    create_jobs=_CREATE_JOBS,
    create_jobs_lock=_CREATE_JOBS_LOCK,
    job_terminal=_JOB_TERMINAL,
)

register_ai_routes(
    bp,
    _direct_access,
    ai_agents=_ai_agents_routes,
    campaign_module=cmc,
    promo_client_cls=_PromoClientRoutes,
    m3_llm_url=_M3_LLM_URL,
    m3_llm_timeout=_M3_LLM_TIMEOUT,
    m3_complete=_m3_complete,
    promo_ctx=_promo_ctx,
    promo_extract_json=_promo_extract_json,
    promo_from_slepok=_promo_from_slepok,
    promo_preview=_promo_preview,
    promo_validate=_promo_validate,
    promo_amount_steps=_promo_amount_steps,
    gen_campaign_content=_gen_campaign_content,
    seed_slepok_content=_seed_slepok_content,
    victory_conn=_victory_conn,
    direct_tokens=_direct_tokens,
    token_for_login=_token_for_login,
    pull_begin=_pull_begin,
    pull_end=_pull_end,
    busy_response=_busy_response,
    v5_get=_v5_get,
)


class _CompatibilityModule(types.ModuleType):
    """Mirror legacy monkeypatches/assignments into the extracted runtime.

    Several maintenance scripts and characterization tests still patch underscore
    helpers on ``direct.blueprint``.  Re-exporting the function object is enough for
    reads, but assignments must update the function's real globals as well.
    """

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name.startswith("_") and name != "_runtime" and hasattr(_runtime, name):
            setattr(_runtime, name, value)


sys.modules[__name__].__class__ = _CompatibilityModule

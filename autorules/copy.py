"""Direct Autorules — 1:1 campaign duplication within the same account.

Uses v5 API directly (same transport layer as copy_engine / direct_copy.py).

Copies campaign-level structure only (settings, strategy, targeting, negative keywords).
Groups and ads are NOT copied — MVP limitation; full structural copy requires the
copy_engine orchestration layer (pull → rewrite → upload pipeline).

Field name constants and strategy_sanitize logic mirror work/slepki_direktologov/
scripts/direct_copy.py (_COPY_SETTINGS_WHITELIST, CAMPAIGN_TYPE_STRUCT,
strategy_sanitize) — reusing the same API knowledge base without importing the
orchestration engine (which has heavy DI requirements).

State=OFF is enforced: all copies are created as drafts and NEVER published.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import requests as rqs

V5_URL = "https://api.direct.yandex.com/json/v5/"
V501_URL = "https://api.direct.yandex.com/json/v501/"

# Campaign base fields for v5 campaigns.get
_CAMPAIGNS_FIELDS = [
    "Id", "Name", "Type", "Status", "State", "StartDate", "EndDate",
    "DailyBudget", "TimeTargeting", "TimeZone", "BlockedIps", "ExcludedSites",
    "NegativeKeywords",
]

# Type-specific field name params for v5 campaigns.get
_CAMPAIGN_TYPE_FIELDNAMES: dict[str, list[str]] = {
    "TextCampaignFieldNames": [
        "CounterIds", "RelevantKeywords", "Settings", "BiddingStrategy",
        "PriorityGoals", "TrackingParams", "AttributionModel",
        "NegativeKeywordSharedSetIds",
    ],
    "DynamicTextCampaignFieldNames": [
        "CounterIds", "Settings", "BiddingStrategy", "TrackingParams",
        "AttributionModel", "NegativeKeywordSharedSetIds",
    ],
    "SmartCampaignFieldNames": [
        "CounterId", "BiddingStrategy", "Settings", "TrackingParams",
        "PriorityGoals", "AttributionModel",
    ],
    "CpmBannerCampaignFieldNames": [
        "CounterIds", "FrequencyCap", "VideoTarget", "Settings",
        "BiddingStrategy", "PriorityGoals", "ExcludedSitesForVideoAds",
    ],
    # UnifiedAdCampaignFieldNames — does not exist in API v5
    # McBannerCampaignFieldNames  — does not exist in API v5
}

# Maps API campaign Type string → struct key for type-specific block
_CAMPAIGN_TYPE_STRUCT: dict[str, str] = {
    "TEXT_CAMPAIGN": "TextCampaign",
    "DYNAMIC_TEXT_CAMPAIGN": "DynamicTextCampaign",
    "SMART_CAMPAIGN": "SmartCampaign",
    "CPM_BANNER_CAMPAIGN": "CpmBannerCampaign",
    "MC_BANNER_CAMPAIGN": "McBannerCampaign",
    "UNIFIED_AD_CAMPAIGN": "UnifiedAdCampaign",
}

# Options that campaigns.add actually accepts (enumerated from Yandex API, 2026-07-17).
# Read-only options returned by campaigns.get (DAILY_BUDGET_ALLOWED, SHARED_ACCOUNT_ENABLED)
# are excluded — passing them in add causes 8000 "unknown parameter" and drops the whole request.
_COPY_SETTINGS_WHITELIST = frozenset({
    "EXCLUDE_PAUSED_COMPETING_ADS", "ADD_OPENSTAT_TAG", "ADD_METRICA_TAG", "ADD_TO_FAVORITES",
    "ENABLE_AREA_OF_INTEREST_TARGETING", "ENABLE_CURRENT_AREA_TARGETING",
    "ENABLE_REGULAR_AREA_TARGETING", "ENABLE_SITE_MONITORING", "ENABLE_BEHAVIORAL_TARGETING",
    "ENABLE_AUTOFOCUS", "ENABLE_RELATED_KEYWORDS", "ENABLE_EXTENDED_AD_TITLE",
    "MAINTAIN_NETWORK_CPC", "ENABLE_COMPANY_INFO", "CAMPAIGN_EXACT_PHRASE_MATCHING_ENABLED",
    "ALTERNATIVE_TEXTS_ENABLED",
})


def _headers(token: str, login: str) -> dict:
    return {
        "Authorization": "Bearer " + token,
        "Client-Login": login,
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
        "Use-Operator-Units": "true",
    }


def _v5_post(url: str, token: str, login: str, body: dict, timeout: int = 30) -> dict:
    try:
        resp = rqs.post(url, headers=_headers(token, login), json=body, timeout=timeout)
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": {"error_string": str(exc)[:200]}}


def _err_str(payload: dict) -> str:
    err = payload.get("error")
    if isinstance(err, dict):
        parts = [err.get("error_string") or "", err.get("error_detail") or ""]
        return " — ".join(p for p in parts if p)
    return str(err or "unknown error")[:300]


def _tomorrow_msk() -> str:
    """Tomorrow by Moscow time — StartDate for copied campaigns.

    Yandex Direct validates dates by MSK. Using "today" at night crosses midnight
    and triggers error 5005 "StartDate cannot be less than current date".
    Tomorrow avoids the whole class of timezone/midnight edge cases.
    """
    return (datetime.now(timezone(timedelta(hours=3))) + timedelta(days=1)).strftime("%Y-%m-%d")


def _today_msk() -> str:
    return datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m-%d")


def _strategy_sanitize(strategy: dict) -> dict:
    """Remove sub-fields that campaigns.get returns but campaigns.add rejects.

    BudgetType lives inside the strategy block (e.g. Search.WbMaximumClicks.BudgetType)
    and causes 8000 "unknown parameter" in add (observed 2026-07-17, 3 out of 5 campaigns
    failed because of this).
    """
    s = json.loads(json.dumps(strategy))
    for side in ("Search", "Network"):
        block = s.get(side)
        if not isinstance(block, dict):
            continue
        for v in block.values():
            if isinstance(v, dict):
                v.pop("BudgetType", None)
        block.pop("BudgetType", None)
    return s


def list_campaigns(token: str, login: str) -> dict:
    """List campaigns for an account using v5 campaigns.get.

    Returns:
        {"campaigns": [{"id", "name", "type", "state", "status"}, ...], "error": str|None}
    """
    j = _v5_post(
        V5_URL + "campaigns",
        token,
        login,
        {
            "method": "get",
            "params": {
                "FieldNames": ["Id", "Name", "Type", "State", "Status"],
                "SelectionCriteria": {},
            },
        },
    )
    if "error" in j:
        return {"campaigns": [], "error": _err_str(j)}
    camps = (j.get("result") or {}).get("Campaigns") or []
    return {
        "campaigns": [
            {
                "id": c["Id"],
                "name": c.get("Name") or "",
                "type": c.get("Type") or "",
                "state": c.get("State") or "",
                "status": c.get("Status") or "",
            }
            for c in camps
        ],
        "error": None,
    }


def clone_campaigns_1to1(
    token: str,
    login: str,
    campaign_ids: list[int],
    *,
    dry_run: bool = False,
    name_suffix: str = " (копия)",
) -> dict:
    """Duplicate selected campaigns within the same account as State=OFF drafts.

    Args:
        token:        OAuth token for the account's managing agency.
        login:        Client login (source = target = same account).
        campaign_ids: IDs of campaigns to clone.
        dry_run:      If True, returns what WOULD be created without actually calling the API.
        name_suffix:  Appended to each copied campaign name to avoid duplicate-name errors.

    Returns:
        {
            "dry_run": bool,
            "results": [
                {
                    "src_id": int,
                    "name":   str,    # new name (original + suffix)
                    "ok":     bool|None,  # None for dry_run
                    "new_id": int|None,
                    "error":  str|None,
                    "type":   str,         # campaign type
                }
            ],
            "created": int,
            "failed":  int,
        }

    IMPORTANT invariant: ALL created campaigns have State=OFF (DRAFT).
    The API omits State from campaigns.add (Yandex creates drafts by default),
    which is equivalent to State=OFF/DRAFT.
    """
    id_set = set(campaign_ids)

    # -- 1. Pull full campaign structure ------------------------------------------
    pull_params: dict = {
        "FieldNames": _CAMPAIGNS_FIELDS,
        "SelectionCriteria": {"Ids": sorted(id_set)},
    }
    pull_params.update(_CAMPAIGN_TYPE_FIELDNAMES)
    j_pull = _v5_post(
        V5_URL + "campaigns",
        token,
        login,
        {"method": "get", "params": pull_params},
    )
    if "error" in j_pull:
        return {
            "dry_run": dry_run,
            "results": [],
            "created": 0,
            "failed": 0,
            "error": f"не удалось получить кампании: {_err_str(j_pull)}",
        }

    campaigns = (j_pull.get("result") or {}).get("Campaigns") or []
    if not campaigns:
        return {
            "dry_run": dry_run,
            "results": [],
            "created": 0,
            "failed": 0,
            "note": "выбранные кампании не найдены (возможно, уже удалены или не принадлежат аккаунту)",
        }

    # -- 2. Dry-run: return intent without API writes ----------------------------
    if dry_run:
        return {
            "dry_run": True,
            "results": [
                {
                    "src_id": c["Id"],
                    "name": (c.get("Name") or "") + name_suffix,
                    "ok": None,
                    "new_id": None,
                    "error": None,
                    "type": c.get("Type") or "",
                }
                for c in campaigns
            ],
            "created": 0,
            "failed": 0,
            "note": "dry_run=True — кампании НЕ созданы; показано что БЫЛО БЫ создано",
        }

    # -- 3. Existing campaign names for conflict resolution ----------------------
    j_exist = _v5_post(
        V5_URL + "campaigns",
        token,
        login,
        {
            "method": "get",
            "params": {"FieldNames": ["Id", "Name", "State"], "SelectionCriteria": {}},
        },
    )
    existing_names: set[str] = set()
    if "result" in j_exist:
        for ec in (j_exist["result"] or {}).get("Campaigns") or []:
            if ec.get("State") != "ARCHIVED":
                existing_names.add(ec.get("Name") or "")

    today = _today_msk()
    results: list[dict] = []
    created = 0
    failed = 0

    # -- 4. Add each campaign ----------------------------------------------------
    for c in campaigns:
        src_id = int(c.get("Id") or 0)
        src_name = c.get("Name") or f"campaign-{src_id}"
        camp_type = c.get("Type") or "TEXT_CAMPAIGN"
        struct_key = _CAMPAIGN_TYPE_STRUCT.get(camp_type, "TextCampaign")
        type_data = c.get(struct_key) or {}

        # Resolve name conflict (append _v01…_v99 if duplicate)
        candidate = src_name + name_suffix
        base_candidate = candidate
        ver = 0
        while candidate in existing_names:
            ver += 1
            candidate = f"{base_candidate}_v{ver:02d}"
        existing_names.add(candidate)  # reserve for subsequent iterations

        # Campaign-level payload
        body: dict = {
            "Name": candidate,
            # Always tomorrow (MSK) — never copy source StartDate (may be in the past
            # → error 5005; see _tomorrow_msk() docstring).
            "StartDate": _tomorrow_msk(),
        }
        if c.get("EndDate") and c["EndDate"] >= today:
            body["EndDate"] = c["EndDate"]
        for field in ("TimeTargeting", "TimeZone", "DailyBudget"):
            if c.get(field):
                body[field] = c[field]
        if (c.get("BlockedIps") or {}).get("Items"):
            body["BlockedIps"] = {"Items": c["BlockedIps"]["Items"]}
        if (c.get("ExcludedSites") or {}).get("Items"):
            body["ExcludedSites"] = {"Items": c["ExcludedSites"]["Items"]}
        if (c.get("NegativeKeywords") or {}).get("Items"):
            body["NegativeKeywords"] = {"Items": c["NegativeKeywords"]["Items"]}

        # Type-specific struct
        strategy_raw = _strategy_sanitize(type_data.get("BiddingStrategy") or {})
        type_body: dict = {"BiddingStrategy": strategy_raw}
        for fld in ("TrackingParams", "AttributionModel", "RelevantKeywords"):
            val = type_data.get(fld)
            if val:
                type_body[fld] = val
        if type_data.get("Settings"):
            settings = [
                s for s in type_data["Settings"]
                if s.get("Option") in _COPY_SETTINGS_WHITELIST
            ]
            if settings:
                type_body["Settings"] = settings
        if camp_type == "DYNAMIC_TEXT_CAMPAIGN":
            for fld in ("DomainUrls", "AutoTargetingCategories"):
                if type_data.get(fld):
                    type_body[fld] = type_data[fld]
        body[struct_key] = type_body

        # UNIFIED_AD_CAMPAIGN uses v501 endpoint; all others use v5
        api_url = V501_URL + "campaigns" if camp_type == "UNIFIED_AD_CAMPAIGN" else V5_URL + "campaigns"

        add_err: str | None = None

        # Attempt 1: full payload
        j_add = _v5_post(api_url, token, login, {"method": "add", "params": {"Campaigns": [body]}})
        if "error" not in j_add:
            add_results = (j_add.get("result") or {}).get("AddResults") or []
            if add_results and add_results[0].get("Id"):
                results.append({
                    "src_id": src_id,
                    "name": candidate,
                    "ok": True,
                    "new_id": int(add_results[0]["Id"]),
                    "error": None,
                    "type": camp_type,
                })
                created += 1
                continue
            add_err = str((add_results[0] or {}).get("Errors") or "неизвестная ошибка add")
        else:
            add_err = _err_str(j_add)

        # Attempt 2 (fallback): strip Settings (invalid enum value kills the whole add)
        body.get(struct_key, {}).pop("Settings", None)
        j_add2 = _v5_post(api_url, token, login, {"method": "add", "params": {"Campaigns": [body]}})
        if "error" not in j_add2:
            add_results2 = (j_add2.get("result") or {}).get("AddResults") or []
            if add_results2 and add_results2[0].get("Id"):
                results.append({
                    "src_id": src_id,
                    "name": candidate,
                    "ok": True,
                    "new_id": int(add_results2[0]["Id"]),
                    "error": f"без Settings (fallback): исходная ошибка: {add_err}",
                    "type": camp_type,
                })
                created += 1
                continue
            add_err = str((add_results2[0] or {}).get("Errors") or add_err)
        else:
            add_err = _err_str(j_add2)

        results.append({
            "src_id": src_id,
            "name": candidate,
            "ok": False,
            "new_id": None,
            "error": add_err,
            "type": camp_type,
        })
        failed += 1

    return {
        "dry_run": False,
        "results": results,
        "created": created,
        "failed": failed,
    }

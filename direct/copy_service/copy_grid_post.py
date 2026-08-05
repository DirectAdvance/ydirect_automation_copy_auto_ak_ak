"""Grid-cookie copy path for GdPostCampaign (tp8/tp9/tp10 posevy)."""
from __future__ import annotations

import json
import re
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from ..clients import grid_finalize as gf
from ..clients.grid_read import GridReadClient
from ..core import campaign as cmc
from ..create.create_set_tp8_10 import (
    POST_BID,
    POST_BUDGET_SUM,
    POST_DEFAULT_BUTTON,
    _ALL_PLATFORM_KEYS,
    _Q_ADD_CAMPAIGNS,
    _Q_ADD_POST_ADS,
    _Q_ADD_POST_AD_GROUPS,
    _TIME_BOARD_24x7,
    _UTM_CAMPAIGN_LEVEL,
    _fetch_notification_email,
    _grid_vr_errors,
)


def _engine():
    from . import copy_engine as ce  # lazy to avoid import-time cycle
    return ce


def configure(_deps: dict) -> None:
    return None


def _copy_is_post_grid_row(row: dict) -> bool:
    typename = str((row or {}).get("typename") or (row or {}).get("type") or "")
    name = str((row or {}).get("name") or "").lower()
    return typename == "GdPostCampaign" or name.startswith(("tp8_", "tp9_", "tp10_"))


def _copy_grid_post_campaigns(job_id: str, body: dict, selected_grid_rows: list[dict],
                              workdir: Path) -> dict:
    """Copy selected POST campaigns through Grid cookies, without v5 snapshot/pull."""
    ce = _engine()
    source_login = (body.get("source_login") or "").strip()
    target_login = (body.get("target_login") or "").strip()
    selected_ids = sorted({
        int(r.get("id") or 0)
        for r in (selected_grid_rows or [])
        if _copy_is_post_grid_row(r) and str(r.get("id") or "").isdigit()
    })
    if not selected_ids:
        raise RuntimeError("post copy: не выбраны GdPostCampaign")

    counter_id = int(body.get("counter_id") or 0)
    goal_id = int(body.get("goal_id") or 0)
    target_domain = (body.get("target_domain") or "").strip()
    target_city = (body.get("target_city") or "").strip()
    target_region = ce._copy_canonical_region_name((body.get("target_region") or "").strip())
    target_agency = body.get("agency") or ce._resolve_agency_hint(target_login, "")
    target_cookie_accounts = (str(target_agency).strip(),) if str(target_agency or "").strip() else None
    target_cookie = None
    if target_cookie_accounts:
        target_cookie = cmc.pick_working_cookie(target_login, accounts=target_cookie_accounts)
        cmc.remember_working_cookie(target_login, target_cookie)
    source_agency = body.get("source_agency") or body.get("sourceAgency") or ce._resolve_agency_hint(source_login, "")
    source_cookie_accounts = (str(source_agency).strip(),) if str(source_agency or "").strip() else None
    source_cookie = None
    if source_cookie_accounts:
        source_cookie = cmc.pick_working_cookie(source_login, accounts=source_cookie_accounts)
        cmc.remember_working_cookie(source_login, source_cookie)

    source_grid = gf.GridClient(source_login, cookie=source_cookie)
    target_grid = gf.GridClient(target_login, cookie=target_cookie)
    source_grid._bootstrap_csrf()
    target_grid._bootstrap_csrf()
    target_email = _fetch_notification_email(target_grid, target_login)
    if not target_email:
        raise RuntimeError("post copy: не удалось получить email аккаунта для notification")

    mode = (body.get("mode") or "auto").strip()
    geo_mode = (body.get("geo_mode") or "replace").strip()
    region_ids = _post_target_region_ids(body, mode, geo_mode, target_city, target_region)
    target_r_code = ce._copy_target_region_code(target_city, target_region) if mode != "other" else ""
    source_ctx = ce._copy_ctx(source_login)
    target_ctx = ce._copy_ctx(target_login)
    source_domain = (source_ctx.get("domain") or "").strip()

    ce._copy_job_log(job_id, f"post grid snapshot источника {source_login}: {len(selected_ids)} кампаний")
    campaign_rows = source_grid.campaigns_edit_rows(selected_ids)
    post_rows = {
        cid: row for cid, row in (campaign_rows or {}).items()
        if int(cid) in selected_ids and row.get("__typename") == "GdPostCampaign"
    }
    if len(post_rows) != len(selected_ids):
        raise RuntimeError(f"post grid snapshot неполный: выбрано {len(selected_ids)}, прочитано {len(post_rows)}")

    reader = GridReadClient(source_login, cookie=(source_cookie or source_grid.cookie))
    groups = _post_read_groups(reader, source_login, selected_ids)
    ads = _post_read_ads(reader, source_login, selected_ids)
    if not source_domain:
        for ad in ads:
            source_domain = _domain_from_href(ad.get("href"))
            if source_domain:
                break

    groups_by_campaign: dict[int, list[dict]] = {}
    for group in groups:
        cid = _safe_int(group.get("campaignId"))
        if cid in selected_ids:
            groups_by_campaign.setdefault(cid, []).append(group)
    ads_by_group: dict[int, list[dict]] = {}
    for ad in ads:
        gid = _safe_int(ad.get("adGroupId"))
        if gid > 0:
            ads_by_group.setdefault(gid, []).append(ad)

    source_hashes_allowed = source_login == target_login
    provided_hashes = [str(h).strip() for h in (body.get("image_hashes") or []) if str(h).strip()]
    results: list[dict] = []
    errors: list[dict] = []
    warnings: list[str] = []
    id_maps = {"campaigns": {}, "adgroups": {}, "ads": {}}

    for cid in selected_ids:
        row = post_rows.get(cid) or {}
        try:
            created = _copy_one_post_campaign(
                target_grid=target_grid,
                source_campaign=row,
                source_groups=groups_by_campaign.get(cid) or [],
                ads_by_group=ads_by_group,
                counter_id=counter_id,
                goal_id=goal_id,
                target_href_base=target_domain,
                source_domain=source_domain,
                target_email=target_email,
                region_ids=region_ids,
                target_r_code=target_r_code,
                source_hashes_allowed=source_hashes_allowed,
                provided_hashes=provided_hashes,
                job_id=job_id,
            )
            results.append(created)
            id_maps["campaigns"][str(cid)] = int(created["campaign_id"])
            for old_gid, new_gid in created.get("adgroup_map", {}).items():
                id_maps["adgroups"][str(old_gid)] = int(new_gid)
            for old_aid, new_aid in created.get("ad_map", {}).items():
                id_maps["ads"][str(old_aid)] = str(new_aid)
        except Exception as exc:  # noqa: BLE001
            err = {"ok": False, "kind": "post", "source_id": cid, "error": str(exc)[:400]}
            results.append(err)
            errors.append(err)

    if not source_hashes_allowed and not provided_hashes:
        warnings.append("post copy: source/target разные, imageHash источника не переносился; объявления созданы без карточек")

    ce._copy_write_json(workdir / "id_maps.json", id_maps)
    created_campaign_ids = [int(r["campaign_id"]) for r in results if r.get("ok") and r.get("campaign_id")]
    verification = {}
    if created_campaign_ids:
        try:
            verification = GridReadClient(target_login, cookie=(target_cookie or target_grid.cookie)).campaign_content_counts(created_campaign_ids)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"post live verification недоступна: {str(exc)[:160]}")

    return {
        "ok": not errors,
        "source_login": source_login,
        "target_login": target_login,
        "selected": len(selected_ids),
        "created": len(created_campaign_ids),
        "failed": len(errors),
        "uses_direct_units": False,
        "copy_depth": "grid_cookie_post",
        "results": results,
        "errors": errors,
        "warnings": warnings,
        "id_maps": id_maps,
        "created_campaign_ids": created_campaign_ids,
        "live_verification": verification,
        "source_context": {"domain": source_domain},
        "target_context": {
            "domain": target_domain,
            "city": target_city,
            "region": target_region,
            "region_ids": region_ids,
            "account_domain": target_ctx.get("domain") or "",
        },
        "workdir": str(workdir),
    }


def _post_target_region_ids(body: dict, mode: str, geo_mode: str, target_city: str,
                            target_region: str) -> list[int]:
    ce = _engine()
    if mode == "other" and geo_mode == "change":
        raw = body.get("geo_region_ids") or []
        if not raw:
            scalar = _safe_int(body.get("geo_region_id"))
            raw = [scalar] if scalar else []
        ids = [int(x) for x in raw if str(x).lstrip("-").isdigit() and int(x) != 0]
        if not ids:
            raise RuntimeError("post copy: geo_region_ids не задан")
        return ids
    if mode == "other" and geo_mode == "keep":
        return [225]
    local_gid, _local_name = ce._copy_geo_id_for_target(target_city, target_region)
    if not local_gid:
        raise RuntimeError(f"post copy: не найден GeoRegionId для цели city={target_city!r}, region={target_region!r}")
    return [int(local_gid)]


def _copy_one_post_campaign(*, target_grid, source_campaign: dict, source_groups: list[dict],
                            ads_by_group: dict[int, list[dict]], counter_id: int, goal_id: int,
                            target_href_base: str, source_domain: str, target_email: str,
                            region_ids: list[int], target_r_code: str,
                            source_hashes_allowed: bool, provided_hashes: list[str],
                            job_id: str) -> dict:
    ce = _engine()
    source_id = _safe_int(source_campaign.get("id"))
    source_name = str(source_campaign.get("name") or f"post-{source_id}").strip()
    target_name = _copy_post_name(source_name, target_r_code, job_id)
    campaign_payload = _campaign_payload_from_source(
        source_campaign, target_name, counter_id, goal_id, target_email
    )
    campaign_resp = _post_json(target_grid, "AddCampaigns", _Q_ADD_CAMPAIGNS, {
        "input": {"campaignAddItems": [{"postCampaign": campaign_payload}]}
    })
    errs = _grid_vr_errors(campaign_resp, "addCampaigns")
    if errs:
        raise RuntimeError("AddCampaigns errors: " + json.dumps(errs, ensure_ascii=False)[:300])
    added = (((campaign_resp.get("data") or {}).get("addCampaigns") or {}).get("addedCampaigns") or [])
    if not added or not added[0].get("id"):
        raise RuntimeError("AddCampaigns: нет addedCampaigns в ответе")
    new_cid = int(added[0]["id"])

    source_groups = sorted(source_groups or [], key=lambda g: _safe_int(g.get("id")))
    if not source_groups:
        source_groups = [{"id": 0, "name": f"{target_name} — group"}]
    adgroup_map: dict[int, int] = {}
    new_group_ids: list[int] = []
    for idx, group in enumerate(source_groups):
        old_gid = _safe_int(group.get("id"))
        group_name = _copy_post_name(str(group.get("name") or f"{target_name} — group {idx + 1}"), target_r_code, job_id="")
        grp_resp = _post_json(target_grid, "AddPostAdGroups", _Q_ADD_POST_AD_GROUPS, {
            "postAddInput": [{
                "name": group_name[:255],
                "campaignId": str(new_cid),
                "regionIds": region_ids or [225],
                "bidModifiers": {"bidModifierDemographics": None},
                "useBidModifiers": False,
                "useAllTelegramCategories": True,
                "customTelegramCategories": [],
                "brief": None,
            }],
        })
        errs = _grid_vr_errors(grp_resp, "addPostAdGroups")
        if errs:
            raise RuntimeError("AddPostAdGroups errors: " + json.dumps(errs, ensure_ascii=False)[:300])
        added_g = (((grp_resp.get("data") or {}).get("addPostAdGroups") or {}).get("addedAdGroupItems") or [])
        if not added_g or not added_g[0].get("adGroupId"):
            raise RuntimeError("AddPostAdGroups: нет adGroupId в ответе")
        new_gid = int(added_g[0]["adGroupId"])
        new_group_ids.append(new_gid)
        if old_gid:
            adgroup_map[old_gid] = new_gid

    ad_items: list[dict] = []
    ad_sources: list[int] = []
    for group in source_groups:
        old_gid = _safe_int(group.get("id"))
        new_gid = adgroup_map.get(old_gid)
        if not new_gid:
            continue
        source_ads = sorted(ads_by_group.get(old_gid) or [], key=lambda a: _safe_int(a.get("id")))
        if not source_ads:
            source_ads = [{}]
        for ad_idx, ad in enumerate(source_ads):
            href = ce._copy_target_href(ad.get("href"), source_domain, target_href_base)
            ad_items.append({
                "adGroupId": str(new_gid),
                "href": href,
                "domain": None,
                "body": _post_text(ad.get("body"), "Узнайте актуальные предложения и оставьте заявку."),
                "title": _post_text(ad.get("title"), "Актуальные предложения"),
                "titleExtension": ad.get("titleExtension") or None,
                "creativeId": None,
                "button": _post_button(ad.get("button"), href),
                "isMobile": False,
                "multicards": _post_multicards(ad, source_hashes_allowed, provided_hashes, ad_idx),
                "inheritableCallouts": None,
                "inheritableSitelinkSet": None,
            })
            ad_sources.append(_safe_int(ad.get("id")))
    if not ad_items:
        raise RuntimeError("AddPostAds: не найдено объявлений источника")
    ads_resp = _post_json(target_grid, "AddPostAds", _Q_ADD_POST_ADS, {
        "addPostInput": {"adAddItems": ad_items, "saveDraft": True},
    })
    errs = _grid_vr_errors(ads_resp, "addPostAds")
    if errs:
        raise RuntimeError("AddPostAds errors: " + json.dumps(errs, ensure_ascii=False)[:300])
    added_ads = (((ads_resp.get("data") or {}).get("addPostAds") or {}).get("addedAds") or [])
    ad_ids = [str(a.get("id")) for a in added_ads if a.get("id")]
    if len(ad_ids) != len(ad_items):
        raise RuntimeError(f"AddPostAds: недобор объявлений {len(ad_ids)}/{len(ad_items)}")
    ad_map = {old: new for old, new in zip(ad_sources, ad_ids) if old}

    ce._copy_job_log(job_id, f"post copy: {source_id} → {new_cid}, groups={len(new_group_ids)}, ads={len(ad_ids)}")
    return {
        "ok": True,
        "kind": "post",
        "name": target_name,
        "source_id": source_id,
        "campaign_id": new_cid,
        "new_id": new_cid,
        "ad_group_ids": new_group_ids,
        "ad_ids": ad_ids,
        "adgroup_map": adgroup_map,
        "ad_map": ad_map,
        "build": {"adgroups": len(new_group_ids), "ads": len(ad_ids)},
        "save_draft": True,
    }


def _campaign_payload_from_source(row: dict, target_name: str, counter_id: int,
                                  goal_id: int, target_email: str) -> dict:
    strategy = row.get("strategy") if isinstance(row.get("strategy"), dict) else {}
    platforms = _platforms_from_strategy(strategy, target_name)
    budget = strategy.get("budget") if isinstance(strategy.get("budget"), dict) else {}
    bid = _safe_int(strategy.get("bid")) or _safe_int(strategy.get("avgBid")) or POST_BID
    budget_sum = _safe_int(budget.get("sum")) or POST_BUDGET_SUM
    notification = row.get("notification") if isinstance(row.get("notification"), dict) else {}
    sms_settings = (notification.get("smsSettings") if isinstance(notification, dict) else {}) or {}
    sms_time = sms_settings.get("smsTime") or {
        "startTime": {"hour": 9, "minute": 0},
        "endTime": {"hour": 21, "minute": 0},
    }
    payload = {
        "name": target_name[:255],
        "isS2sTrackingEnabled": bool(row.get("isS2sTrackingEnabled")),
        "biddingStategyWithPlatforms": {
            "platforms": platforms,
            "strategyName": "AUTOBUDGET",
            "strategyData": {
                "goalId": str(goal_id) if goal_id else None,
                "bid": str(bid),
                "payForShows": bool(strategy.get("payForShows")),
                "sum": str(budget_sum),
                "budgetType": "WEEKLY",
            },
        },
        "attributionModel": "AUTOMATIC",
        "metrikaCounters": [int(counter_id)] if counter_id else [],
        "meaningfulGoals": [],
        "startDate": date.today().isoformat(),
        "endDate": None,
        "disabledPlaces": list(row.get("disabledPlaces") or []),
        "bannerHrefParams": row.get("bannerHrefParams") or _UTM_CAMPAIGN_LEVEL,
        "broadMatch": {"broadMatchFlag": False, "broadMatchGoalId": None, "broadMatchLimit": 0},
        "dayBudget": "0",
        "enableCompanyInfo": False,
        "excludePausedCompetingAds": True,
        "hasAddMetrikaTagToUrl": False,
        "hasAddOpenstatTagToUrl": False,
        "hasExtendedGeoTargeting": False,
        "hasSiteMonitoring": False,
        "hasTitleSubstitute": False,
        "notification": {
            "smsSettings": {"smsTime": sms_time, "enableEvents": []},
            "emailSettings": {"stopByReachDailyBudget": False, "email": target_email},
        },
        "timeTarget": row.get("timeTarget") or {
            "enabledHolidaysMode": False,
            "holidaysSettings": None,
            "idTimeZone": "130",
            "timeBoard": _TIME_BOARD_24x7,
            "useWorkingWeekends": True,
        },
    }
    return gf._strip_graphql_typenames(payload)


def _platforms_from_strategy(strategy: dict, name: str) -> dict[str, bool]:
    raw = strategy.get("platforms") if isinstance(strategy.get("platforms"), dict) else {}
    out = {k: bool(raw.get(k)) for k in _ALL_PLATFORM_KEYS}
    if any(out.values()):
        return out
    low = str(name or "").lower()
    out["telegram"] = low.startswith(("tp8_", "tp10_"))
    out["maxMessenger"] = low.startswith(("tp9_", "tp10_"))
    return out


def _post_read_groups(reader: GridReadClient, login: str, campaign_ids: list[int]) -> list[dict]:
    q = (
        "query AdGroups($login:String!,$inp:GdAdGroupsContainerInput!){"
        "client(searchBy:{login:$login}){adGroups(input:$inp){rowset{id campaignId name}}}}"
    )
    data = reader._post("AdGroups", q, {"login": login, "inp": _grid_input(campaign_ids)})
    return ((((data.get("data") or {}).get("client") or {}).get("adGroups") or {}).get("rowset") or [])


def _post_read_ads(reader: GridReadClient, login: str, campaign_ids: list[int]) -> list[dict]:
    q_text = (
        "query AdaptiveImages($login:String!,$inp:GdAdsContainerInput!){"
        "client(searchBy:{login:$login}){ads(input:$inp){rowset{id campaignId adGroupId __typename "
        "...on GdPostAd{href title body titleExtension button{action href}}}}}}"
    )
    q_cards = (
        "query AdaptiveImages($login:String!,$inp:GdAdsContainerInput!){"
        "client(searchBy:{login:$login}){ads(input:$inp){rowset{id campaignId __typename "
        "...on GdPostAd{multicards{id image{imageHash name}}}}}}}"
    )
    inp = _grid_input(campaign_ids)
    text_data = reader._post("AdaptiveImages", q_text, {"login": login, "inp": inp})
    card_data = reader._post("AdaptiveImages", q_cards, {"login": login, "inp": inp})
    text_rows = ((((text_data.get("data") or {}).get("client") or {}).get("ads") or {}).get("rowset") or [])
    card_rows = ((((card_data.get("data") or {}).get("client") or {}).get("ads") or {}).get("rowset") or [])
    cards_by_id = {_safe_int(row.get("id")): row for row in card_rows}
    out: list[dict] = []
    for row in text_rows:
        if row.get("__typename") != "GdPostAd":
            continue
        merged = dict(row)
        cards = cards_by_id.get(_safe_int(row.get("id"))) or {}
        merged["multicards"] = cards.get("multicards") or []
        out.append(merged)
    return out


def _grid_input(campaign_ids: list[int]) -> dict:
    return {
        "filter": {"campaignIdIn": [str(int(cid)) for cid in campaign_ids]},
        "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
        "limitOffset": {"limit": 5000, "offset": 0},
        "orderBy": [{"order": "ASC", "field": "ID"}],
    }


def _post_multicards(ad: dict, source_hashes_allowed: bool, provided_hashes: list[str],
                     ad_idx: int) -> list[dict]:
    if provided_hashes:
        return [{"imageHash": provided_hashes[ad_idx % len(provided_hashes)]}]
    if not source_hashes_allowed:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for card in ad.get("multicards") or []:
        image = card.get("image") if isinstance(card.get("image"), dict) else {}
        image_hash = str(card.get("imageHash") or image.get("imageHash") or "").strip()
        if image_hash and image_hash not in seen:
            seen.add(image_hash)
            out.append({"imageHash": image_hash})
    return out


def _post_button(button: dict | None, href: str) -> dict:
    action = POST_DEFAULT_BUTTON
    if isinstance(button, dict) and button.get("action"):
        action = str(button.get("action") or POST_DEFAULT_BUTTON)
    return {"action": action, "href": href}


def _post_text(value, fallback: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text or fallback


def _copy_post_name(name: str, target_r_code: str, job_id: str) -> str:
    ce = _engine()
    out = ce._copy_remap_region_code(str(name or "").strip(), target_r_code) if target_r_code else str(name or "").strip()
    if job_id:
        suffix = f" — copy {job_id[:6]} {int(time.time()) % 100000}"
        if suffix not in out:
            out = (out[: max(1, 255 - len(suffix))] + suffix)
    return out[:255]


def _post_json(grid, op: str, query: str, variables: dict) -> dict:
    resp = grid._post(op, query, variables)
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"{op}: bad json {str(exc)[:120]} HTTP {getattr(resp, 'status_code', '?')}") from exc
    if resp.status_code >= 400:
        raise RuntimeError(f"{op}: HTTP {resp.status_code} {json.dumps(data, ensure_ascii=False)[:300]}")
    return data


def _domain_from_href(href: str | None) -> str:
    try:
        return (urlsplit(str(href or "")).netloc or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _safe_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

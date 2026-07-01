"""Read-only Grid helpers for Direct live verification.

This module keeps browser-cookie GraphQL reads out of ``blueprint.py`` and
does not mutate campaigns. It is used by post-create verification to check the
actual number of ad groups and ads after the create response is saved.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

import requests
import urllib3

from . import campaign as cmc
from .grid_finalize import GRID_URL

urllib3.disable_warnings()


class GridReadError(RuntimeError):
    pass


class GridReadClient:
    def __init__(self, login: str, cookie: str | None = None):
        self.login = login
        self.cookie = cookie or cmc.pick_working_cookie(login)
        self.csrf: str | None = None
        self.sess = requests.Session()
        self.sess.verify = False

    def _post(self, op: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Cookie": self.cookie,
            "dna-operation-name": op,
            "x-direct-api": "1",
            "x-detected-locale": "ru",
            "Content-Type": "application/json",
            "User-Agent": cmc.USER_AGENT,
            "Origin": "https://direct.yandex.ru",
            "Referer": f"https://direct.yandex.ru/dna/grid/campaigns?ulogin={self.login}",
        }
        if self.csrf:
            headers["x-csrf-token"] = self.csrf
        url = f"{GRID_URL}?operationName={op}&ulogin={self.login}"
        r = self.sess.post(
            url,
            json={"operationName": op, "query": query, "variables": variables},
            headers=headers,
            timeout=40,
        )
        m = re.search(r"_direct_csrf_token=([^;,\s]+)", r.headers.get("Set-Cookie", ""))
        token = r.cookies.get("_direct_csrf_token") or (m.group(1) if m else None)
        if token:
            self.csrf = token
        if r.status_code == 403 and self.csrf:
            headers["x-csrf-token"] = self.csrf
            r = self.sess.post(
                url,
                json={"operationName": op, "query": query, "variables": variables},
                headers=headers,
                timeout=40,
            )
        try:
            data = r.json()
        except Exception as e:  # noqa: BLE001
            raise GridReadError(f"Grid {op}: bad json {str(e)[:120]}") from e
        if data.get("errors"):
            raise GridReadError(f"Grid {op}: {str(data.get('errors'))[:300]}")
        return data

    def _bootstrap_csrf(self) -> None:
        q = ("query Callouts($login:String!){callouts(input:{searchBy:{login:$login}"
             "filter:{deleted:false}}){id}}")
        try:
            self._post("Callouts", q, {"login": self.login})
        except GridReadError:
            pass

    @staticmethod
    def _bad_adgroup_name(name: Any) -> bool:
        text = str(name or "").strip()
        if not text:
            return True
        if re.search(r"\b(?:None|null|undefined)\b", text, re.I):
            return True
        return bool(re.search(r"(?:^|\s)[—-]\s*$", text))

    # ── Enrichment reads (guarded) ───────────────────────────────────────────
    # These are *best-effort* Grid reads for post-create defect detection. Field
    # names on the read types (GdUnifiedCampaign / GdAdaptiveTextAd / keywords)
    # mirror the mutation-input names used in grid_create/grid_finalize, but they
    # are NOT independently verified against a live Grid schema here. Any read
    # that raises leaves the corresponding value as ``None`` and records a note in
    # ``enrich_errors`` — it must never break the core adgroups/ads counters.
    def _enrich_campaign_settings(self, id_strings: list[str], counts: dict[int, dict[str, Any]]) -> None:
        """Fill disabled_places / is_organic_search_enabled / promo_extension_id."""
        q = ("query CampSettings($login:String!,$inp:GdCampaignsContainerInput!){"
             "client(searchBy:{login:$login}){campaigns(input:$inp){rowset{id "
             "...on GdUnifiedCampaign{disabledPlaces isOrganicSearchEnabled promoExtensionId}}}}}")
        inp = {
            "filter": {"campaignIdIn": id_strings},
            "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
            "limitOffset": {"limit": 5000, "offset": 0},
            "orderBy": [{"order": "ASC", "field": "ID"}],
        }
        data = self._post("CampSettings", q, {"login": self.login, "inp": inp})
        rows = ((((data.get("data") or {}).get("client") or {})
                 .get("campaigns") or {}).get("rowset") or [])
        for row in rows:
            try:
                cid = int(row.get("id"))
            except (TypeError, ValueError):
                continue
            if cid not in counts:
                continue
            dp = row.get("disabledPlaces")
            counts[cid]["disabled_places"] = list(dp) if isinstance(dp, list) else None
            org = row.get("isOrganicSearchEnabled")
            counts[cid]["is_organic_search_enabled"] = bool(org) if isinstance(org, bool) else None
            promo = row.get("promoExtensionId")
            counts[cid]["promo_extension_id"] = str(promo) if promo not in (None, "") else None
            counts[cid]["settings_read"] = True

    def _enrich_keyword_counts(self, id_strings: list[str], counts: dict[int, dict[str, Any]]) -> None:
        """Fill keywords_count (sum of keyword phrases across the campaign's groups)."""
        q = ("query Kw($login:String!,$inp:GdKeywordsContainerInput!){"
             "client(searchBy:{login:$login}){keywords(input:$inp){rowset{id campaignId}}}}")
        inp = {
            "filter": {"campaignIdIn": id_strings},
            "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
            "limitOffset": {"limit": 5000, "offset": 0},
            "orderBy": [{"order": "ASC", "field": "ID"}],
        }
        data = self._post("Kw", q, {"login": self.login, "inp": inp})
        rows = ((((data.get("data") or {}).get("client") or {})
                 .get("keywords") or {}).get("rowset") or [])
        kw_counts: dict[int, int] = defaultdict(int)
        for row in rows:
            try:
                kw_counts[int(row.get("campaignId"))] += 1
            except (TypeError, ValueError):
                continue
        for cid in counts:
            if str(cid) in id_strings:
                counts[cid]["keywords_count"] = int(kw_counts.get(cid, 0))
                counts[cid]["keywords_read"] = True

    def _enrich_group_targeting(self, id_strings: list[str], counts: dict[int, dict[str, Any]]) -> None:
        """Fill search_zero_kw_groups / wrong_autotarget_groups per SEARCH campaign (tp2/tp4/tp5).

        Reuses GridClient.groups_for_edit (grid_finalize) with the SAME validated cookie. Per group
        the campaign's tp is taken from the read campaign name; only tp2/tp4/tp5 (search) campaigns
        get the two flags, so tp1 RSYA autotarget-only groups are never falsely flagged. Guarded like
        the other enrichments — any failure records an ``enrich_errors`` note and leaves flags None."""
        from .grid_finalize import GridClient

        import re as _re
        _tp_re = _re.compile(r"^\s*tp(\d+)_", _re.IGNORECASE)
        grid = GridClient(self.login, cookie=self.cookie)
        rows = grid.groups_for_edit([int(s) for s in id_strings])
        zero_kw: dict[int, int] = defaultdict(int)
        wrong_at: dict[int, int] = defaultdict(int)
        seen_search: set[int] = set()
        for grp in rows:
            cid = grp.get("campaign_id")
            if cid not in counts:
                continue
            m = _tp_re.match(str(grp.get("campaign_name") or ""))
            tp = int(m.group(1)) if m else None
            if tp not in (2, 4, 5) or not grp.get("supported"):
                continue
            seen_search.add(int(cid))
            if int(grp.get("keyword_count") or 0) == 0:
                zero_kw[int(cid)] += 1
            rm = grp.get("relevance_match") or {}
            cats = {str(x).upper() for x in (rm.get("relevanceMatchCategories") or [])}
            brands = {str(x).upper() for x in (rm.get("autotargetingBrandSettings") or [])}
            if not (rm.get("isActive") and cats == {"EXACT_V2_MARK"} and brands == {"WITHOUT_BRAND"}):
                wrong_at[int(cid)] += 1
        for cid in counts:
            if cid in seen_search:
                counts[cid]["search_zero_kw_groups"] = int(zero_kw.get(cid, 0))
                counts[cid]["wrong_autotarget_groups"] = int(wrong_at.get(cid, 0))
                counts[cid]["groups_edit_read"] = True

    def _enrich_ad_price(self, id_strings: list[str], counts: dict[int, dict[str, Any]]) -> None:
        """Fill has_ad_price / ad_price_count for adaptive text ads with adPrice set."""
        q = ("query AdPrice($login:String!,$inp:GdAdsContainerInput!){"
             "client(searchBy:{login:$login}){ads(input:$inp){rowset{id campaignId "
             "...on GdAdaptiveTextAd{adPrice{price}}}}}}")
        inp = {
            "filter": {"campaignIdIn": id_strings},
            "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
            "limitOffset": {"limit": 5000, "offset": 0},
            "orderBy": [{"order": "ASC", "field": "ID"}],
        }
        data = self._post("AdPrice", q, {"login": self.login, "inp": inp})
        rows = ((((data.get("data") or {}).get("client") or {})
                 .get("ads") or {}).get("rowset") or [])
        price_counts: dict[int, int] = defaultdict(int)
        for row in rows:
            try:
                cid = int(row.get("campaignId"))
            except (TypeError, ValueError):
                continue
            ap = row.get("adPrice") or {}
            if isinstance(ap, dict) and ap.get("price") not in (None, "", 0, "0"):
                price_counts[cid] += 1
        for cid in counts:
            if str(cid) in id_strings:
                cnt = int(price_counts.get(cid, 0))
                counts[cid]["ad_price_count"] = cnt
                counts[cid]["has_ad_price"] = cnt > 0
                counts[cid]["ad_price_read"] = True

    def campaign_content_counts(self, campaign_ids: list[int | str]) -> dict[int, dict[str, Any]]:
        """Return actual Grid counts: ``{campaign_id: {adgroups, ads, ...}}``.

        Core counters (``adgroups``/``ads``/``bad_adgroup_names``) are always
        present. Enrichment fields for post-create defect detection are added
        best-effort and are ``None`` when Grid did not return them cheaply:
        ``keywords_count``, ``disabled_places``, ``is_organic_search_enabled``,
        ``promo_extension_id``, ``has_ad_price``/``ad_price_count``. The
        ``*_read`` flags say whether that enrichment query actually succeeded, so
        callers can tell "read and empty" apart from "not read". Failures are
        collected in ``enrich_errors``.

        UAC campaigns may legitimately have zero Grid adGroups/ads, so callers
        should apply these counts only to Unified/Text campaign families where
        Grid entities are expected.
        """
        ids: list[int] = []
        for raw in campaign_ids or []:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                continue
            if cid > 0 and cid not in ids:
                ids.append(cid)
        if not ids:
            return {}
        self._bootstrap_csrf()

        def _blank() -> dict[str, Any]:
            return {
                "adgroups": 0, "ads": 0, "bad_adgroup_names": 0, "bad_adgroup_name_examples": [],
                "keywords_count": None, "keywords_read": False,
                "disabled_places": None, "is_organic_search_enabled": None,
                "promo_extension_id": None, "settings_read": False,
                "has_ad_price": None, "ad_price_count": None, "ad_price_read": False,
                "search_zero_kw_groups": None, "wrong_autotarget_groups": None,
                "groups_edit_read": False,
                "enrich_errors": [],
            }

        counts: dict[int, dict[str, Any]] = {cid: _blank() for cid in ids}
        for chunk in [ids[i:i + 100] for i in range(0, len(ids), 100)]:
            id_strings = [str(cid) for cid in chunk]
            ag_counts = defaultdict(int)
            ad_counts = defaultdict(int)
            bad_ag_counts = defaultdict(int)
            bad_ag_examples: dict[int, list[str]] = defaultdict(list)
            ag_q = ("query AdGroups($login:String!,$inp:GdAdGroupsContainerInput!){"
                    "client(searchBy:{login:$login}){adGroups(input:$inp){rowset{id campaignId name}}}}")
            ads_q = ("query Ads($login:String!,$inp:GdAdsContainerInput!){"
                     "client(searchBy:{login:$login}){ads(input:$inp){rowset{id campaignId}}}}")
            common = {
                "filter": {"campaignIdIn": id_strings},
                "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
                "limitOffset": {"limit": 5000, "offset": 0},
                "orderBy": [{"order": "ASC", "field": "ID"}],
            }
            ag_data = self._post("AdGroups", ag_q, {"login": self.login, "inp": common})
            ag_rows = ((((ag_data.get("data") or {}).get("client") or {})
                        .get("adGroups") or {}).get("rowset") or [])
            for row in ag_rows:
                try:
                    cid = int(row.get("campaignId"))
                except (TypeError, ValueError):
                    continue
                ag_counts[cid] += 1
                name = row.get("name")
                if self._bad_adgroup_name(name):
                    bad_ag_counts[cid] += 1
                    if len(bad_ag_examples[cid]) < 5:
                        bad_ag_examples[cid].append(str(name or ""))
            ads_data = self._post("Ads", ads_q, {"login": self.login, "inp": common})
            ad_rows = ((((ads_data.get("data") or {}).get("client") or {})
                        .get("ads") or {}).get("rowset") or [])
            for row in ad_rows:
                try:
                    ad_counts[int(row.get("campaignId"))] += 1
                except (TypeError, ValueError):
                    continue
            for cid in chunk:
                counts[cid]["adgroups"] = int(ag_counts[cid])
                counts[cid]["ads"] = int(ad_counts[cid])
                counts[cid]["bad_adgroup_names"] = int(bad_ag_counts[cid])
                counts[cid]["bad_adgroup_name_examples"] = bad_ag_examples.get(cid, [])[:5]

            # Enrichment reads — each guarded independently so a schema mismatch on
            # one field never blocks the core counters or the other enrichments.
            for label, fn in (
                ("settings", self._enrich_campaign_settings),
                ("keywords", self._enrich_keyword_counts),
                ("ad_price", self._enrich_ad_price),
                ("group_targeting", self._enrich_group_targeting),
            ):
                try:
                    fn(id_strings, counts)
                except Exception as e:  # noqa: BLE001
                    note = f"{label}: {str(e)[:140]}"
                    for cid in chunk:
                        counts[cid]["enrich_errors"].append(note)
        return counts

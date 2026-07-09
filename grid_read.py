"""Read-only Grid helpers for Direct live verification.

This module keeps browser-cookie GraphQL reads out of ``blueprint.py`` and
does not mutate campaigns. It is used by post-create verification to check the
actual number of ad groups and ads after the create response is saved.
"""
from __future__ import annotations

import re
import time
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
        _payload = {"operationName": op, "query": query, "variables": variables}

        def _do_post(h: dict) -> requests.Response:
            _exc: Exception | None = None
            for _try in range(3):
                if _try:
                    time.sleep(0.6 * _try)
                try:
                    return self.sess.post(url, json=_payload, headers=h, timeout=40)
                except (requests.exceptions.ConnectionError,
                        requests.exceptions.ChunkedEncodingError) as _te:
                    _exc = _te
            raise _exc  # type: ignore[misc]

        r = _do_post(headers)
        m = re.search(r"_direct_csrf_token=([^;,\s]+)", r.headers.get("Set-Cookie", ""))
        token = r.cookies.get("_direct_csrf_token") or (m.group(1) if m else None)
        if token:
            self.csrf = token
        if r.status_code == 403 and self.csrf:
            headers["x-csrf-token"] = self.csrf
            r = _do_post(headers)
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

    # Реальный счётчик ключей через Grid showConditions (сущности GdKeyword) — авторитетный,
    # в отличие от groups_for_edit.keyword_count (edit-view, лагает → ложный NO_KEYWORDS_LIVE).
    _KW_COUNT_Q = (
        "query KwCount($login:String!,$cid:[Long!]!,$off:Int!){"
        "client(searchBy:{login:$login}){"
        "showConditions(input:{filter:{typeIn:[KEYWORD],campaignIdIn:$cid}"
        "statRequirements:{preset:TODAY}"
        "limitOffset:{limit:10000,offset:$off}"
        "orderBy:[{order:DESC,field:GROUP_ID}]})"
        "{rowset{__typename ...on GdKeyword{id adGroupId}}}}}")

    def _show_condition_kw_counts(self, campaign_ids) -> dict[int, int] | None:
        """{adgroup_id: keyword_count} по showConditions (реальные ключи). Возвращает dict при
        успехе (0 у групп без ключей — их просто нет в ответе) или None при сбое Grid → вызывающий
        откатывается на edit-view keyword_count.

        ВАЖНО: читаем КАЖДУЮ кампанию ОТДЕЛЬНЫМ запросом. Grid showConditions отдаёт максимум
        10000 строк на запрос, а offset-пагинация за 10000 НЕ работает. При чтении нескольких РК
        одним запросом (campaignIdIn=список) combined-ответ обрезался на 10000, orderBy GROUP_ID DESC
        оставлял в окне только поздние РК (больший group_id), а раньше созданные выпадали целиком →
        их группы читались как 0 → ложный NO_KEYWORDS_LIVE (напр. tp2 «Марки» при живых 9677 ключей).
        Одна РК ≤10000 ключей влезает в окно. TODO: РК с >10000 ключей потребует пагинации по
        диапазонам GROUP_ID (пока усечётся — отдельная задача)."""
        cids = [int(c) for c in (campaign_ids or [])]
        if not cids:
            return {}
        self._bootstrap_csrf()
        out: dict[int, int] = defaultdict(int)
        for cid in cids:
            try:
                j = self._post("KwCount", self._KW_COUNT_Q,
                               {"login": self.login, "cid": [cid], "off": 0})
            except GridReadError:
                return None
            rows = ((((j.get("data") or {}).get("client") or {})
                     .get("showConditions") or {}).get("rowset") or [])
            for r in rows:
                if (r.get("__typename") or "") != "GdKeyword":
                    continue
                try:
                    out[int(r.get("adGroupId") or 0)] += 1
                except (TypeError, ValueError):
                    pass
        return out

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
        """Fill disabled_places / is_organic_search_enabled.
        NOTE: promoExtensionId убран — поле FieldUndefined в текущей Grid-схеме (2026-07-03).
        promo_extension_id остаётся None; settings_read=True выставляется по другим полям."""
        q = ("query CampSettings($login:String!,$inp:GdCampaignsContainerInput!){"
             "client(searchBy:{login:$login}){campaigns(input:$inp){rowset{id "
             "...on GdUnifiedCampaign{disabledPlaces isOrganicSearchEnabled}}}}}")
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
            # promo_extension_id: поле promoExtensionId FieldUndefined в Grid → остаётся None
            counts[cid]["settings_read"] = True

    def _enrich_campaign_invariants(self, id_strings: list[str], counts: dict[int, dict[str, Any]]) -> None:
        """Fill campaign-level DoD invariant галочки tp1–tp5 (P0 — закрытие дыры DOD §1.c).

        Читает через ``GridClient.read_campaign_invariants`` (edit-view CampaignsEditData — единственная
        Grid read-схема, где эти поля есть; live rowset их не отдаёт). Тот же валидированный cookie, что
        у прочих enrichment-ов. Заполняет tri-state поля + ``campaign_invariants_read=True`` только для
        РЕАЛЬНО прочитанных кампаний. Fail-safe: непрочитанная кампания остаётся с None-полями и
        ``campaign_invariants_read=False`` → verifier по ней НЕ выдаёт кампанийных инвариант-issue
        (Grid-лаг/ошибка/FieldUndefined не порождают ложный детект, журнал I). Guarded извне
        (campaign_content_counts) — исключение уходит в enrich_errors, поля остаются None."""
        from .grid_finalize import GridClient
        grid = GridClient(self.login, cookie=self.cookie)
        inv = grid.read_campaign_invariants([int(s) for s in id_strings])
        for cid, row in (inv or {}).items():
            if cid not in counts:
                continue
            for key in ("is_alternative_texts_enabled", "has_extended_geo_targeting",
                        "enable_company_info", "is_recommendations_management_enabled",
                        "is_price_recommendations_management_enabled", "yandex_maps_enabled",
                        "serp_geo_wizard_enabled", "pay_for_conversion"):
                counts[cid][key] = row.get(key)
            counts[cid]["campaign_invariants_read"] = True

    def _enrich_keyword_counts(self, id_strings: list[str], counts: dict[int, dict[str, Any]]) -> None:
        """keywords_count вычисляется в _enrich_group_targeting (из groups_for_edit).
        GdKeywordsContainerInput (UnknownType) недоступен в текущей Grid-схеме (2026-07-03).
        Этот метод — no-op; оставлен чтобы не ломать позиции в enrichment-цикле."""

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
        # 1-й проход: отобрать поисковые группы (tp2/4/5, supported) и их кампании
        search_groups: list[tuple[int, int, int, dict]] = []   # (cid, gid, edit_kw, rm)
        search_cids: set[int] = set()
        at_by_design: set[int] = set()   # АТ-кампании: группы живут на relevanceMatch, 0 ключей — норма
        for grp in rows:
            cid = grp.get("campaign_id")
            if cid not in counts:
                continue
            camp_name = str(grp.get("campaign_name") or "")
            m = _tp_re.match(camp_name)
            tp = int(m.group(1)) if m else None
            if tp not in (2, 4, 5) or not grp.get("supported"):
                continue
            # «… - Автотаргетинг - …» (нейминг набора): группы БЕЗ ключей по дизайну →
            # zero-kw для них не дефект (живой ложняк NO_KEYWORDS_LIVE: tp5 «Марки - Автотаргетинг»
            # porg-7bqj56f4 06.07 — добивка вечно «нужна», починить нечем).
            if "автотаргетинг" in camp_name.lower():
                at_by_design.add(int(cid))
            gid = int(grp.get("adgroup_id") or 0)
            search_groups.append((int(cid), gid, int(grp.get("keyword_count") or 0),
                                  grp.get("relevance_match") or {}))
            search_cids.add(int(cid))
        # Авторитетный счётчик ключей (showConditions). None → откат на edit-view keyword_count.
        real_kw = self._show_condition_kw_counts(search_cids)
        zero_kw: dict[int, int] = defaultdict(int)
        wrong_at: dict[int, int] = defaultdict(int)
        kw_total: dict[int, int] = defaultdict(int)   # для keywords_count
        seen_search: set[int] = set()
        for cid, gid, edit_kw, rm in search_groups:
            seen_search.add(cid)
            # max(showConditions, edit-view): группа считается пустой только если ОБА источника дали 0.
            # showConditions авторитетен (живые GdKeyword), но при пустом/частичном/лаг-ответе edit-view
            # страхует от ложного zero → ложного NO_KEYWORDS_LIVE. Оба источника только недосчитывают
            # (лаг), поэтому max безопасен.
            grp_kw = max(int(real_kw.get(gid, 0)), int(edit_kw)) if real_kw is not None else int(edit_kw)
            kw_total[cid] += grp_kw
            if grp_kw == 0 and cid not in at_by_design:
                zero_kw[cid] += 1
            cats = {str(x).upper() for x in (rm.get("relevanceMatchCategories") or [])}
            brands = {str(x).upper() for x in (rm.get("autotargetingBrandSettings") or [])}
            if not (rm.get("isActive") and cats == {"EXACT_V2_MARK"} and brands == {"WITHOUT_BRAND"}):
                wrong_at[cid] += 1
        for cid in counts:
            if cid in seen_search:
                counts[cid]["search_zero_kw_groups"] = int(zero_kw.get(cid, 0))
                counts[cid]["wrong_autotarget_groups"] = int(wrong_at.get(cid, 0))
                counts[cid]["groups_edit_read"] = True
                # keywords_count заполняется здесь (GdKeywordsContainerInput FieldUndefined)
                counts[cid]["keywords_count"] = int(kw_total.get(cid, 0))
                counts[cid]["keywords_read"] = True

    def _enrich_ad_price(self, id_strings: list[str], counts: dict[int, dict[str, Any]]) -> None:
        """Fill has_ad_price / ad_price_count for adaptive text ads with bannerPrice set.
        NOTE: поле adPrice FieldUndefined в GdAdaptiveTextAd; используем bannerPrice (2026-07-03).
        bannerPrice читается так же как в adaptive_ads_for_update (тот работает live)."""
        q = ("query AdPrice($login:String!,$inp:GdAdsContainerInput!){"
             "client(searchBy:{login:$login}){ads(input:$inp){rowset{id campaignId "
             "...on GdAdaptiveTextAd{bannerPrice{price}}}}}}")
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
            bp = row.get("bannerPrice") or {}
            if isinstance(bp, dict) and bp.get("price") not in (None, "", 0, "0", "0.00", "0.0"):
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
                # Adaptive images enrichment (NO_IMAGES_LIVE detect)
                "adaptive_total": None, "no_images_ads": None, "adaptive_images_read": False,
                # Shopping bodies enrichment (EMPTY_DEFAULT_TEXT_LIVE detect)
                "shopping_no_bodies_ads": None, "shopping_bodies_read": False,
                # Campaign-level invariant галочки tp1–tp5 (P0, edit-view CampaignsEditData).
                # Все булевы — tri-state (None=не прочитано → verifier не флагает, fail-safe).
                "is_alternative_texts_enabled": None, "has_extended_geo_targeting": None,
                "enable_company_info": None, "is_recommendations_management_enabled": None,
                "is_price_recommendations_management_enabled": None,
                "yandex_maps_enabled": None, "serp_geo_wizard_enabled": None,
                "pay_for_conversion": None, "campaign_invariants_read": False,
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
                ("keywords", self._enrich_keyword_counts),   # no-op, data filled by group_targeting
                ("ad_price", self._enrich_ad_price),
                ("group_targeting", self._enrich_group_targeting),   # also fills keywords_count
                ("adaptive_images", self._enrich_adaptive_images),
                ("shopping_bodies", self._enrich_shopping_bodies),
                ("campaign_invariants", self._enrich_campaign_invariants),
            ):
                try:
                    fn(id_strings, counts)
                except Exception as e:  # noqa: BLE001
                    note = f"{label}: {str(e)[:140]}"
                    for cid in chunk:
                        counts[cid]["enrich_errors"].append(note)
        return counts

    def _enrich_adaptive_images(self, id_strings: list[str], counts: dict[int, dict[str, Any]]) -> None:
        """Fill no_images_ads / adaptive_total for tp1 adaptive text ads (NO_IMAGES_LIVE detect).
        NOTE: creativeIds FieldUndefined в Grid ads-query (2026-07-03) — поле не читаем, NO_VIDEO_LIVE
        не детектируется через Grid-read; creativeIds сохраняется только через _VIDEO_CREATIVE_CACHE."""
        q = ("query AdaptiveImages($login:String!,$inp:GdAdsContainerInput!){"
             "client(searchBy:{login:$login}){ads(input:$inp){rowset{id campaignId __typename "
             "...on GdAdaptiveTextAd{images{imageHash}}}}}}")
        inp = {
            "filter": {"campaignIdIn": id_strings},
            "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
            "limitOffset": {"limit": 5000, "offset": 0},
            "orderBy": [{"order": "ASC", "field": "ID"}],
        }
        data = self._post("AdaptiveImages", q, {"login": self.login, "inp": inp})
        rows = ((((data.get("data") or {}).get("client") or {})
                 .get("ads") or {}).get("rowset") or [])
        no_images: dict[int, int] = defaultdict(int)
        total_adaptive: dict[int, int] = defaultdict(int)
        for row in rows:
            try:
                cid = int(row.get("campaignId"))
            except (TypeError, ValueError):
                continue
            # Различаем по __typename, НЕ по наличию images: Grid отдаёт images:null (не [])
            # для ГОЛОГО адаптивного объявления (live 03.07.2026: 420/420 без картинок = null)
            # — идиом «images is None → не адаптивное» пропускал ровно целевые объявления,
            # поэтому NO_IMAGES_LIVE никогда не флагал полностью голые и добивка не запускалась.
            if row.get("__typename") != "GdAdaptiveTextAd":
                continue
            total_adaptive[cid] += 1
            hashes = [img.get("imageHash") for img in (row.get("images") or []) if img.get("imageHash")]
            if not hashes:
                no_images[cid] += 1
        for cid in counts:
            if str(cid) in id_strings:
                counts[cid]["adaptive_total"] = int(total_adaptive.get(cid, 0))
                counts[cid]["no_images_ads"] = int(no_images.get(cid, 0))
                counts[cid]["adaptive_images_read"] = True

    def _enrich_shopping_bodies(self, id_strings: list[str], counts: dict[int, dict[str, Any]]) -> None:
        """Fill shopping_no_bodies_ads for ShoppingAd without bodies (EMPTY_DEFAULT_TEXT_LIVE detect).
        Использует GdSmartAd-фрагмент (best-effort: если тип не тот — данные просто не придут)."""
        q = ("query ShoppingBodies($login:String!,$inp:GdAdsContainerInput!){"
             "client(searchBy:{login:$login}){ads(input:$inp){rowset{id campaignId "
             "...on GdSmartAd{bodies}}}}}")
        inp = {
            "filter": {"campaignIdIn": id_strings},
            "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
            "limitOffset": {"limit": 5000, "offset": 0},
            "orderBy": [{"order": "ASC", "field": "ID"}],
        }
        data = self._post("ShoppingBodies", q, {"login": self.login, "inp": inp})
        rows = ((((data.get("data") or {}).get("client") or {})
                 .get("ads") or {}).get("rowset") or [])
        no_bodies: dict[int, int] = defaultdict(int)
        for row in rows:
            try:
                cid = int(row.get("campaignId"))
            except (TypeError, ValueError):
                continue
            bodies = row.get("bodies")
            if bodies is None:
                continue   # not a GdSmartAd (fragment didn't match)
            if not bodies or not any(str(b).strip() for b in bodies):
                no_bodies[cid] += 1
        for cid in counts:
            if str(cid) in id_strings:
                counts[cid]["shopping_no_bodies_ads"] = int(no_bodies.get(cid, 0))
                counts[cid]["shopping_bodies_read"] = True

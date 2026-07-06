"""Declarative per-tp campaign spec + live auditor + fixers.

This module is deliberately Flask-free. Like the ``create_set_*`` modules it takes
its blueprint IO helpers through ``configure(deps)`` (globals injection). Grid /
UAC reads are done through the already-standalone ``grid_finalize`` /
``grid_read`` / ``uac_read`` / ``campaign`` modules, so the auditor can run both
inside ``digest.service`` (delayed-repair cycle) and from the command line.

Idea
----
A campaign of a given ``tp`` has a DECLARATIVE contract of "what MUST be there and
what MUST NOT be there". ``audit_campaign`` reads the live campaign through Grid/UAC
and compares it to :data:`SPEC`, emitting ``issues`` in the exact same shape the
existing verifier uses, so they flow into ``repair_planner`` / ``repair_executor``
unchanged. The existing live detects (NO_KEYWORDS / NO_IMAGES / NO_ADPRICE and the
group/ad counters) are NOT duplicated here — this module adds the checks the counters
cannot see:

* ``KEYWORDS_WRONG_GROUP`` — a search adgroup whose keyword phrases mathematically
  belong to a DIFFERENT ct than the group's own ct (the "keyword shift" incident).
  The reference keyword set per ct is recomputed with the SAME code path as creation
  (M3 pack → ``_filter_group_keywords``); ct discrimination is done by unique tokens,
  so a rotation/offset of keywords across groups is caught by counting, not heuristics.
* ``IMAGES_FORBIDDEN`` — a search (tp2/tp4) ad that carries ``imageHashes`` (search
  ResponsiveAds must have empty images in this project).
* ``FEED_FILTER_WRONG_CT`` — a tp1 shopping / tp7 UAC group whose feed filter does not
  positively scope to its ct's brand/model, or a general/service ct missing the global
  minus-marks.
* ``EXTRA_TP_NOT_IN_SLEPOK`` — an account carrying a tp that the chosen slepok's
  structure does not declare (e.g. a tp6 Master campaign for a slepok without tp6).
* ``SHORT_TITLES`` — a tp6/tp7 Master (UAC) campaign whose titles waste length budget
  (≥2 titles ≤45 chars of the 56 limit). Auto-fixed in place: each short title is
  extended with a neutral suffix (``ai_agents.extend_title_to_max``) via the same
  cookie ``PATCH /web-api/uac/campaign/{id}`` path the content editor uses.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from typing import Any

# These sibling modules are Flask-free and safe to import directly (they build their
# own cookie clients via ``campaign.build_client`` / ``pick_working_cookie``).
from . import grid_finalize as gf
from . import grid_read as gr
from . import uac_read as ur
from . import repair_executor as rex

_DEPS: dict = {}


def configure(deps: dict) -> None:
    """Bind blueprint dependencies used by the auditor (globals injection)."""
    _DEPS.clear()
    _DEPS.update(deps)
    globals().update(deps)


# ── declarative spec ────────────────────────────────────────────────────────────
# Documentation-grade constant: the human-readable contract each tp must satisfy.
# The auditor functions below implement exactly these clauses. Keeping the contract
# next to the code makes review of "what is checked" a single read.
SPEC: dict[str, dict[str, Any]] = {
    "tp2": {  # Поиск
        "keywords_match_group": "каждая КС-группа: ключи ⊆ эталон её ct (пак); чужой ct → shift",
        "images_forbidden": "объявления: imageHashes = []",
        "sitelinks_present": "быстрые ссылки присутствуют (покрыто общим верификатором)",
        "callouts_present": "уточнения присутствуют (покрыто общим верификатором)",
    },
    "tp4": {  # Поиск + Динамика — те же поисковые инварианты, что tp2
        "keywords_match_group": "как tp2 (динамические группы без своего ct не проверяются)",
        "images_forbidden": "объявления: imageHashes = []",
    },
    "tp5": {  # Поиск + Динамика + ТГ — поисковые группы = как tp2
        "keywords_match_group": "как tp2 для поисковых групп",
        "listing_present": "товарные группы имеют «Страницы каталога» (NO_LISTING → by-shopping)",
        "feed_filter_present": "товарные/каталожные объявления имеют feedFilter — минимум "
                               "минус-марки (FEED_FILTER_MISSING_GRID → авто-добивка per-feed)",
        "placements_manual": "места показа = Ручная настройка PLACEMENTS_TP5 "
                             "(PLACEMENTS_WRONG → узкий UpdateCampaigns)",
    },
    "tp1": {  # РСЯ / товарка
        "images_required": "комбинаторные объявления имеют imageHashes "
                           "(IMAGE_MISSING → добивка боевым images_repair; live-детект NO_IMAGES_LIVE)",
        "adprice_where_feed_price": "bannerPrice у групп с маркой в прайс-кэше (покрыто NO_ADPRICE_LIVE)",
        "feed_filter_by_ct": "товарные группы: сегмент Марки/Модели → позитивный фильтр по марке/модели; "
                             "Общее/служебные → присутствуют глобальные минус-марки",
        "default_text_present": "bodies ShoppingAd непустые (покрыто EMPTY_DEFAULT_TEXT_LIVE)",
        "button_present": "комбинаторные объявления с валидным href имеют кнопку GET_DISCOUNT "
                          "(BUTTON_MISSING → авто-добивка RMW-апдейтом; видео сохраняется через typedCreatives)",
        "video_present": "брендовые объявления имеют видео, если ролики есть в пуле M3 "
                         "(VIDEO_MISSING → deferred-video добивка до полного нуля; "
                         "при создании видео НЕ грузится — каркас не ждёт медиа)",
    },
    "tp6": {  # Мастер (UAC)
        "titles_full_length": "заголовки добиты до 48–56 симв (≥2 коротких ≤45 → SHORT_TITLES, авто-добивка)",
    },
    "tp7": {  # Товарка (UAC)
        "feed_filters": "feed_filters по тому же ct-правилу, что tp1-товарка (detail через uac_read)",
        "titles_full_length": "как tp6 (SHORT_TITLES, авто-добивка суффиксами)",
    },
    "plan": {
        "tp_subset_of_slepok": "типы кампаний аккаунта ⊆ структуре слепка (лишний tp → EXTRA_TP_NOT_IN_SLEPOK)",
    },
}

_CT_RE = re.compile(r"ct\d{4}", re.IGNORECASE)
_TP_RE = re.compile(r"^\s*tp(\d+)_", re.IGNORECASE)
_SEARCH_TPS = {2, 4, 5}
# generic автотематические токены, которые НЕ различают ct (кредит/купить/цена/…): не дают
# им становиться «дискриминативными» — но фильтровать вручную не нужно, т.к. они встречаются
# в >1 ct и алгоритм disc-токенов их естественно отбрасывает. Список — только для читаемости отчёта.
_GENERIC_TOKENS = {
    "купить", "цена", "цены", "кредит", "авто", "автомобиль", "автомобили", "машина",
    "машины", "новый", "новые", "цвет", "рублей", "москва", "россия", "год", "года",
    "продажа", "автосалон", "салон", "дилер",
}


def _tp_of_name(name: Any) -> int | None:
    m = _TP_RE.match(str(name or ""))
    return int(m.group(1)) if m else None


def _ct_of_name(name: Any) -> str:
    m = _CT_RE.search(str(name or ""))
    return m.group(0).lower() if m else "ct0000"


def _norm(s: Any) -> str:
    return str(s or "").strip().lower().replace("ё", "е")  # ё→е


def _kw_tokens(phrase: Any) -> set[str]:
    """Значимые токены фразы: lower/ё→е, срезаны операторы (+ ! [ ] " «»), минус-слова
    (``-token``) отброшены, токены длиной ≥2. Транслитерация не делается."""
    text = _norm(phrase)
    tokens: set[str] = set()
    for raw in re.split(r"[\s,]+", text):
        if not raw or raw.startswith("-"):
            continue
        tok = re.sub(r"^[+!\[\]\"«»]+|[+!\[\]\"«»]+$", "", raw)
        tok = re.sub(r"[^0-9a-zа-я]", "", tok)
        if len(tok) >= 2:
            tokens.add(tok)
    return tokens


# ── expected keyword sets from the M3 pack (reference for shift math) ─────────────
def _selected_slepok_key(slepok: str) -> str:
    key_map = _DEPS.get("_SLEPOK_KEY") or {}
    return key_map.get((slepok or "").lower(), (slepok or "").lower())


def _expected_keywords_by_ct(login: str, slepok: str, site_type: str, city: str,
                             tp_code: str, cts: set[str]) -> dict[str, list[str]]:
    """Recompute the expected keyword phrases per ct with the SAME derivation as create:
    ``kp.gather`` once → per-ct positive → project keyword guard ``_filter_group_keywords``.
    Returns ``{ct: [phrase,...]}`` (empty when M3 is down — caller falls back to model tokens)."""
    kp = _DEPS.get("kp")
    _filter = _DEPS.get("_filter_group_keywords")
    _ct_segment = _DEPS.get("_ct_segment")
    _ag_part1 = _DEPS.get("_ag_part1_map")
    _valid_brand = _DEPS.get("_valid_pack_brand_name")
    if not (kp and _filter and _ct_segment):
        return {}
    if not slepok:
        return {}
    try:
        pack = kp.gather(_selected_slepok_key(slepok), site_type, tp_code) or {}
    except Exception:  # noqa: BLE001
        return {}
    if not pack:
        return {}
    ct_name = {}
    try:
        ct_name = _ag_part1() if _ag_part1 else {}
    except Exception:  # noqa: BLE001
        ct_name = {}
    ct_model = {}
    try:
        ct_model = kp.feeds_ct_model()
    except Exception:  # noqa: BLE001
        ct_model = {}
    out: dict[str, list[str]] = {}
    for ct in cts:
        pos = (pack.get(ct) or {}).get("positive") or []
        if not pos:
            continue
        seg = _ct_segment(ct)
        raw_brand = ct_name.get(ct) or ct_model.get(ct) or ct
        brand = (_valid_brand(ct, raw_brand) if _valid_brand else raw_brand) or "Авто"
        try:
            kws = _filter(pos, seg, brand, city, site_type)
        except Exception:  # noqa: BLE001
            kws = list(pos)
        out[ct] = [str(k) for k in (kws or []) if str(k).strip() and not str(k).startswith("---")]
    return out


def _phrase_key(phrase: Any) -> str:
    """Каноничный ключ фразы для сравнения множеств: значимые токены (без операторов/минус-слов,
    ё→е, lower) отсортированы и склеены. «dfm mage»==«mage dfm»; операторный шум игнорируется."""
    return " ".join(sorted(_kw_tokens(phrase)))


def _phrase_keys(phrases: list[str]) -> set[str]:
    return {k for k in (_phrase_key(p) for p in (phrases or [])) if k}


# ── search keyword-shift audit (KEYWORDS_WRONG_GROUP) ─────────────────────────────
def _audit_search_keywords(groups: list[dict], login: str, slepok: str, site_type: str,
                           city: str, tp_code: str) -> list[dict]:
    """Detect keyword shift: a MODEL group holding NONE of its own ct's эталон keywords while its
    keywords belong to ANOTHER ct's эталон.

    Spec rule ("ключи ⊆ эталон её ct; допускай подмножество, но НЕ пересечение с чужим"):
    the эталон per ct is recomputed with the create-time code (M3 pack → ``_filter_group_keywords``),
    reduced to canonical phrase keys, then set-compared:

    * ``own_hits`` = live phrases present in this ct's эталон (subset is fine — cap may cut count);
    * if ``own_hits > 0`` the group holds its own content → NOT a shift (this is what makes
      near-duplicate/brand-sibling cts safe: they legitimately overlap);
    * only when ``own_hits == 0`` AND another ct's эталон contains ≥2 of the group's live phrases
      is it a genuine shift (the wrong model's keywords landed in this group wholesale).

    Restricted to ``Модели`` groups (their keywords must be model-specific; brand/general groups
    share vocabulary and are not decidable this way) and to cts whose эталон is known (non-empty).
    """
    search = [g for g in groups if g.get("supported") and (_tp_of_name(g.get("campaign_name")) in _SEARCH_TPS)]
    if not search:
        return []
    _ct_segment = _DEPS.get("_ct_segment")
    cts: set[str] = {_ct_of_name(g.get("adgroup_name")) for g in search}
    if len(cts) < 2:
        return []  # без ≥2 ct сдвиг не определим
    expected = _expected_keywords_by_ct(login, slepok, site_type, city, tp_code, cts)
    if not expected:
        return []  # пак недоступен → эталон неизвестен, не флагаем вслепую (только пак — источник истины)
    exp_keys: dict[str, set[str]] = {ct: _phrase_keys(kws) for ct, kws in expected.items()}
    issues: list[dict] = []
    for g in search:
        own_ct = _ct_of_name(g.get("adgroup_name"))
        gid = int(g.get("adgroup_id") or 0)
        cid = g.get("campaign_id")
        live = list(g.get("keywords") or [])
        if not live or gid <= 0:
            continue  # пустые группы ловит NO_KEYWORDS_LIVE, не дублируем
        # Только «Модели»: их ключи обязаны быть про конкретную модель. Бренд/Общее делят лексику.
        if _ct_segment and _ct_segment(own_ct) != "Модели":
            continue
        own_set = exp_keys.get(own_ct)
        if not own_set:
            continue  # эталон своего ct неизвестен → судить нельзя
        live_keys = [_phrase_key(k) for k in live]
        live_keys = [k for k in live_keys if k]
        if not live_keys:
            continue
        own_hits = sum(1 for k in live_keys if k in own_set)
        if own_hits > 0:
            continue  # держит хоть часть своего эталона (подмножество) → это НЕ сдвиг
        # own_hits == 0: ни один ключ не из своего эталона. Куда они относятся?
        foreign: dict[str, int] = defaultdict(int)
        for k in live_keys:
            for ct, kset in exp_keys.items():
                if ct != own_ct and k in kset:
                    foreign[ct] += 1
        if not foreign:
            continue  # ключи не совпали ни с чьим эталоном (кастомные/site) → не флагаем
        found_ct = max(foreign, key=lambda c: foreign[c])
        found_hits = foreign[found_ct]
        if found_hits >= 2:
            issues.append({
                "code": "KEYWORDS_WRONG_GROUP",
                "id": cid,
                "campaign_id": cid,
                "name": str(g.get("campaign_name") or ""),
                "adgroup_id": gid,
                "expected_ct": own_ct,
                "found_ct": found_ct,
                "own_hits": own_hits,
                "found_hits": found_hits,
                "live_keyword_count": len(live),
                "severity": "high",
                "detail": (f"группа ct={own_ct} (adgroup {gid}) не содержит НИ ОДНОГО ключа своего "
                           f"эталона, но {found_hits} её ключей = эталон ct={found_ct} (сдвиг)"),
            })
    return issues


# ── search images-forbidden audit (IMAGES_FORBIDDEN) ─────────────────────────────
def _audit_search_images(rc: gr.GridReadClient, login: str, campaign_id: int,
                         campaign_name: str) -> list[dict]:
    """Search ResponsiveAds must not carry imageHashes. Flag any tp2/tp4 ad with images."""
    q = ("query SpecImg($login:String!,$inp:GdAdsContainerInput!){"
         "client(searchBy:{login:$login}){ads(input:$inp){rowset{id campaignId "
         "...on GdAdaptiveTextAd{images{imageHash}}}}}}")
    inp = {
        "filter": {"campaignIdIn": [str(campaign_id)]},
        "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
        "limitOffset": {"limit": 5000, "offset": 0},
        "orderBy": [{"order": "ASC", "field": "ID"}],
    }
    try:
        data = rc._post("SpecImg", q, {"login": login, "inp": inp})
    except Exception:  # noqa: BLE001
        return []
    rows = ((((data.get("data") or {}).get("client") or {}).get("ads") or {}).get("rowset") or [])
    with_images = 0
    for row in rows:
        images = row.get("images")
        if images is None:
            continue
        if any(img.get("imageHash") for img in (images or [])):
            with_images += 1
    if with_images <= 0:
        return []
    return [{
        "code": "IMAGES_FORBIDDEN",
        "id": campaign_id,
        "campaign_id": campaign_id,
        "name": campaign_name,
        "ads_with_images": with_images,
        "severity": "medium",
        "detail": f"поисковая кампания: {with_images} объявл. с imageHashes (должно быть пусто)",
    }]


# ── product feed-filter audit (FEED_FILTER_MISSING_GRID) ──────────────────────────
def _audit_product_feed_filters(rc: gr.GridReadClient, login: str, campaign_id: int,
                                campaign_name: str, groups: list | None = None) -> list[dict]:
    """tp1/tp5 товарные (ShoppingAd) и каталожные (ListingAd) объявления: feedFilter не должен
    быть null. ShoppingAd: флагаем только при включённых минус-марках. ListingAd «Страницы
    каталога»: флагаем null feedFilter НЕЗАВИСИМО от минус-марок (ПРАВКА 3, DETECT ONLY —
    фиксер для каталожного фильтра требуется отдельно; НЕ путать с минус-марками).
    Схема чтения подтверждена live 03.07.2026: feedFilter{tab conditions{field operator
    stringValue}}; null видели на tp5-fallback «Товарная галерея» (скрин #91, camp 712120488).
    Флагаем ТОЛЬКО null/пустые conditions — позитивные фильтры сегментов не трогаем."""
    _minus = _DEPS.get("_enabled_minus_marks")
    try:
        marks = list(_minus() or []) if callable(_minus) else []
    except Exception:  # noqa: BLE001
        marks = []
    if not marks:
        return []      # минус-марки выключены — ни ShoppingAd, ни ListingAd фиксить нечем, не флагаем
    q = ("query SpecPFF($login:String!,$inp:GdAdsContainerInput!){"
         "client(searchBy:{login:$login}){ads(input:$inp){rowset{id adGroupId __typename "
         "...on GdShoppingAd{bodies feed{id} feedFilter{tab conditions{field}}} "
         "...on GdListingAd{bodies feed{id} feedFilter{tab conditions{field}}}}}}}")
    inp = {
        "filter": {"campaignIdIn": [str(campaign_id)]},
        "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
        "limitOffset": {"limit": 5000, "offset": 0},
        "orderBy": [{"order": "ASC", "field": "ID"}],
    }
    try:
        data = rc._post("SpecPFF", q, {"login": login, "inp": inp})
    except Exception:  # noqa: BLE001
        return []
    rows = ((((data.get("data") or {}).get("client") or {}).get("ads") or {}).get("rowset") or [])
    agid_to_ct = {str(g.get("adgroup_id") or ""): _ct_of_name(g.get("adgroup_name"))
                  for g in (groups or []) if g.get("adgroup_id")}
    ads_missing: list[dict] = []
    total_product = 0
    for r in rows:
        tn = str(r.get("__typename") or "")
        if tn not in ("GdShoppingAd", "GdListingAd"):
            continue
        total_product += 1
        ff = r.get("feedFilter")
        if tn == "GdShoppingAd":
            # ShoppingAd: детект только при включённых минус-марках (исходное поведение)
            if not marks:
                continue
            if ff and (ff.get("conditions") or []):
                continue
            if ff and str(ff.get("tab") or "") not in ("", "CONDITION"):
                continue   # tree/категорийный фильтр: conditions=null легитимен
        else:
            # GdListingAd «Страницы каталога»: null feedFilter — дефект, но фиксер
            # fix_feed_filters_grid ставит фильтр по имени каталога ТОЛЬКО при включённых
            # минус-марках (иначе early-return campaigns_fixed:0). Без марок ставить нечего →
            # НЕ флагаем, иначе набор вечно висит в «нужна добивка» (ПРАВКА 3, согласовано с фиксером).
            if not marks:
                continue
            if ff:
                continue   # любой feedFilter → ok для ListingAd
        ads_missing.append({
            "ad_id": str(r.get("id")),
            "listing": tn == "GdListingAd",
            "feed_id": str((r.get("feed") or {}).get("id") or ""),
            "bodies": list(r.get("bodies") or []),
            "ct": agid_to_ct.get(str(r.get("adGroupId") or "")) or "",
        })
    if not ads_missing:
        return []
    return [{
        "code": "FEED_FILTER_MISSING_GRID",
        "id": campaign_id,
        "campaign_id": campaign_id,
        "name": campaign_name,
        "ads": ads_missing,
        "ads_total": total_product,
        "severity": "medium",
        "detail": (f"{len(ads_missing)}/{total_product} товарных/каталожных объявл. без "
                   f"feedFilter — добить минус-марками (поле бренда per-feed)"),
    }]


# ── tp5 placements audit (PLACEMENTS_WRONG) ───────────────────────────────────────
def _audit_placements(rc: gr.GridReadClient, login: str, campaign_id: int,
                      campaign_name: str) -> list[dict]:
    """tp5: «Места показа» = Ручная настройка ровно PLACEMENTS_TP5 (Товарная галерея +
    Продвижение в поисковой выдаче). finalize ставит их при создании, но падает на server
    error Яндекса → кампания остаётся с дефолтом (лишние «Динамические места» и «РСЯ» —
    скрин #90, camp 712120488). placementTypes читаем у GdUnifiedCampaign (live 03.07)."""
    q = ("query SpecPlc($login:String!,$inp:GdCampaignsContainerInput!){"
         "client(searchBy:{login:$login}){campaigns(input:$inp){rowset{id __typename "
         "...on GdUnifiedCampaign{placementTypes strategy{platforms{network}}}}}}}")
    inp = {
        "filter": {"campaignIdIn": [str(campaign_id)]},
        "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
        "limitOffset": {"limit": 5, "offset": 0},
        "orderBy": [{"order": "ASC", "field": "ID"}],
    }
    try:
        data = rc._post("SpecPlc", q, {"login": login, "inp": inp})
    except Exception:  # noqa: BLE001
        return []
    rows = ((((data.get("data") or {}).get("client") or {}).get("campaigns") or {}).get("rowset") or [])
    row = next((r for r in rows if str(r.get("id")) == str(campaign_id)), None)
    if not row or str(row.get("__typename") or "") != "GdUnifiedCampaign":
        return []
    cur = sorted(set(row.get("placementTypes") or []))
    want = sorted(set(gf.PLACEMENTS_TP5))
    network_on = bool(((row.get("strategy") or {}).get("platforms") or {}).get("network"))
    if cur == want and not network_on:
        return []
    parts = []
    if cur != want:
        parts.append(f"места показа {cur or 'дефолт (все)'} вместо {want} — добить узким UpdateCampaigns")
    if network_on:
        # РСЯ у tp5 = ошибка спеки (скрин #90); чинится только полным finalize (strategyData) —
        # авто-фикс не трогает стратегию, только репортим
        parts.append("РСЯ (platforms.network) ВКЛЮЧЕНА — эталон tp5: network=False (report-only)")
    return [{
        "code": "PLACEMENTS_WRONG",
        "id": campaign_id,
        "campaign_id": campaign_id,
        "name": campaign_name,
        "current": cur,
        "expected": want,
        "network_on": network_on,
        "placements_ok": cur == want,
        "severity": "medium",
        "detail": "; ".join(parts),
    }]


# ── plan ⊆ slepok audit (EXTRA_TP_NOT_IN_SLEPOK) ─────────────────────────────────
def _audit_plan_vs_slepok(account_campaigns: list[dict], slepok: str, site_type: str) -> list[dict]:
    """Типы кампаний аккаунта ⊆ структуре слепка. Лишний tp (напр. tp6 у слепка без tp6) → issue.

    Проверка объявленности tp — через ``_struct_has_tp`` (наличие tp-блока в структуре), НЕ через
    ``_struct_cts``: тот отдаёт только модель-ct и у tp6/tp7 с чисто ct0000-группами давал []
    → ложные EXTRA_TP_NOT_IN_SLEPOK (scherbakova-tp7, pavlov-tp6)."""
    _struct_has_tp = _DEPS.get("_struct_has_tp")
    if not slepok:
        return []
    present_tps: set[int] = set()
    tp_example: dict[int, dict] = {}
    for c in account_campaigns:
        tp = _tp_of_name(c.get("name"))
        if tp:
            present_tps.add(tp)
            tp_example.setdefault(tp, c)
    if not present_tps or not _struct_has_tp:
        return []
    key = _selected_slepok_key(slepok)
    tp_map = {1: "tp1", 2: "tp2", 3: "tp3", 4: "tp4", 5: "tp5", 6: "tp6", 7: "tp7"}
    issues: list[dict] = []
    for tp in sorted(present_tps):
        code = tp_map.get(tp)
        if not code:
            continue
        try:
            declared = bool(_struct_has_tp(key, site_type, code))
        except Exception:  # noqa: BLE001
            continue  # структура нечитаема → не флагаем
        if not declared:
            ex = tp_example.get(tp) or {}
            issues.append({
                "code": "EXTRA_TP_NOT_IN_SLEPOK",
                "id": ex.get("id"),
                "campaign_id": ex.get("id"),
                "name": str(ex.get("name") or ""),
                "tp": tp,
                "slepok": slepok,
                "severity": "medium",
                "detail": f"аккаунт содержит {code}, которого нет в структуре слепка {slepok}",
            })
    return issues


# ── tp1 combo-ads audit (BUTTON_MISSING + SHORT_TITLES grid) ─────────────────────
def _ct_has_pool_video(ct: str) -> bool:
    """Есть ли в пуле РЕАЛЬНО СУЩЕСТВУЮЩЕЕ валидное видео для ct.

    Проверяем через videos_pool_for_ct (резолв локального _video_pool, лёгкий чек exists+size,
    без ffprobe). Возвращает True ТОЛЬКО если есть хотя бы один валидный ролик в сжатом пуле.
    Это предотвращает эмиссию VIDEO_MISSING для ct без реально пригодного видео (бракованные
    или отсутствующие файлы не попадут в добивку → нет вечного цикла и HTTP 400 от Яндекса)."""
    kp_mod = _DEPS.get("kp")
    if not kp_mod:
        return False
    try:
        ct_norm = (ct or "").strip().lower()
        if not ct_norm:
            return False
        # БЕЗ pre-check по точному ключу индекса: у ct без СВОЕГО ключа (ct0026 Belgee X50)
        # videos_pool_for_ct находит видео через brand-fallback — строгий pre-check
        # `Video|video|<ct>` отсекал такие ct → VIDEO_MISSING не эмитился → видео не прикреплялось
        # (живой кейс 2026-07-05, porg-psm5h7q6). videos_pool_for_ct сам делает чек exists+size.
        paths = kp_mod.videos_pool_for_ct(ct_norm, limit=1)
        return bool(paths)
    except Exception:  # noqa: BLE001
        return False


def _audit_tp1_adaptive(rc: gr.GridReadClient, login: str, campaign_id: int,
                        campaign_name: str, groups: list | None = None) -> list[dict]:
    """tp1 РСЯ, один Grid-запрос — два детекта по комбинаторным объявлениям:

    * BUTTON_MISSING — объявление с валидным href без кнопки «Получить скидку»
      (_apply_combo_button при создании best-effort: 29/50 без кнопки, инцидент 03.07.2026);
    * SHORT_TITLES (transport=grid) — объявление с ≥2 заголовками ≤45 симв из 56
      (скрин #78: группа BAIC с запасом 8–18). Фикс — RMW, видео сохраняется (typedCreatives).
    """
    q = ("query SpecTp1($login:String!,$inp:GdAdsContainerInput!){"
         "client(searchBy:{login:$login}){ads(input:$inp){rowset{id campaignId adGroupId "
         "__typename ...on GdAdaptiveTextAd{href hasButton hasVideo titles images{imageHash}}}}}}")
    inp = {
        "filter": {"campaignIdIn": [str(campaign_id)]},
        "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
        "limitOffset": {"limit": 5000, "offset": 0},
        "orderBy": [{"order": "ASC", "field": "ID"}],
    }
    try:
        data = rc._post("SpecTp1", q, {"login": login, "inp": inp})
    except Exception:  # noqa: BLE001
        return []
    rows = ((((data.get("data") or {}).get("client") or {}).get("ads") or {}).get("rowset") or [])
    issues: list[dict] = []
    missing = [str(r.get("id")) for r in rows
               if r.get("hasButton") is False
               and re.match(r"https?://", str(r.get("href") or ""))]
    if missing:
        issues.append({
            "code": "BUTTON_MISSING",
            "id": campaign_id,
            "campaign_id": campaign_id,
            "name": campaign_name,
            "ad_ids": missing,
            "ads_total": len(rows),
            "severity": "low",
            "detail": f"РСЯ: {len(missing)}/{len(rows)} объявл. без кнопки «Получить скидку» — добить RMW",
        })
    # VIDEO_MISSING: видео вынесено из создания в добивку (03.07.2026) — объявление брендовой
    # группы без hasVideo при наличии роликов в пуле M3 для его ct. Цель Семёна: ВСЕ видео
    # в итоге загружены — детект идемпотентен, каждый цикл аудита двигает остаток к нулю.
    agid_to_ct = {}
    for g in (groups or []):
        gid = str(g.get("adgroup_id") or "")
        if gid:
            agid_to_ct[gid] = _ct_of_name(g.get("adgroup_name"))
    video_missing = []   # [{ad_id, ct}]
    if agid_to_ct:
        for r in rows:
            if r.get("hasVideo") is not False:
                continue
            ct = agid_to_ct.get(str(r.get("adGroupId") or ""))
            if not ct or ct == "ct0000":
                continue
            if _ct_has_pool_video(ct):
                video_missing.append({"ad_id": str(r.get("id")), "ct": ct})
    if video_missing:
        issues.append({
            "code": "VIDEO_MISSING",
            "id": campaign_id,
            "campaign_id": campaign_id,
            "name": campaign_name,
            "ads": video_missing,
            "ads_total": len(rows),
            "severity": "low",
            "detail": (f"РСЯ: {len(video_missing)}/{len(rows)} объявл. без видео при наличии "
                       f"роликов в пуле — добить загрузкой (deferred-video)"),
        })
    # IMAGE_MISSING: комбинаторное РСЯ-объявление без единого imageHash. Причина (лайв 03.07):
    # upload_image молча падал при создании → часть объявлений голая (8/24 в camp 712119904).
    # Фикс — боевой images_repair (ct→пул→upload→RMW), сам перечитывает кампанию.
    # ⚠️ Детект по __typename, НЕ по «images is not None»: Grid отдаёт images:null (не [])
    # для голого адаптивного объявления (live 03.07: 420/420) — фильтр по наличию ключа
    # пропускал ровно целевые объявления.
    img_missing = [str(r.get("id")) for r in rows
                   if r.get("__typename") == "GdAdaptiveTextAd"
                   and not any((im or {}).get("imageHash") for im in (r.get("images") or []))]
    if img_missing:
        issues.append({
            "code": "IMAGE_MISSING",
            "id": campaign_id,
            "campaign_id": campaign_id,
            "name": campaign_name,
            "ad_ids": img_missing,
            "ads_total": len(rows),
            "severity": "medium",
            "detail": (f"РСЯ: {len(img_missing)}/{len(rows)} объявл. без картинок — "
                       f"добить images_repair (пул ct → upload → RMW)"),
        })
    short_ads = []
    for r in rows:
        titles = [str(t) for t in (r.get("titles") or []) if str(t or "").strip()]
        if not titles:
            continue
        n_short = sum(1 for t in titles if len(t) <= _TITLE_SHORT_LEN)
        if n_short >= 2:
            short_ads.append(str(r.get("id")))
    if short_ads:
        issues.append({
            "code": "SHORT_TITLES",
            "id": campaign_id,
            "campaign_id": campaign_id,
            "name": campaign_name,
            "transport": "grid",
            "ad_ids": short_ads,
            "ads_total": len(rows),
            "severity": "low",
            "detail": (f"РСЯ: {len(short_ads)}/{len(rows)} объявл. с ≥2 заголовками "
                       f"≤{_TITLE_SHORT_LEN} симв (лимит 56) — добить суффиксами (RMW)"),
        })
    return issues


# ── listing audit (NO_LISTING): группы с товарным объявлением без «Страниц каталога» ──
def _audit_no_listing(rc: gr.GridReadClient, login: str, campaign_id: int,
                      campaign_name: str) -> list[dict]:
    """Фид-кампании (tp1/tp3/tp5): каждая группа с GdShoppingAd должна иметь GdListingAd.
    Инцидент 03.07: сломанная выборка мутации давала ListingAd=0 «graceful» на всех новых
    кампаниях; плюс кампании больше не удаляются при 0 листингов — добивка обязана их закрыть.
    Fix: add_listing_ads_by_shopping_ads (листинг наследует текст+фильтр товарного)."""
    q = ("query SpecLst($login:String!,$inp:GdAdsContainerInput!){"
         "client(searchBy:{login:$login}){ads(input:$inp){rowset{id adGroupId __typename}}}}")
    inp = {
        "filter": {"campaignIdIn": [str(campaign_id)]},
        "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
        "limitOffset": {"limit": 5000, "offset": 0},
        "orderBy": [{"order": "ASC", "field": "ID"}],
    }
    try:
        data = rc._post("SpecLst", q, {"login": login, "inp": inp})
    except Exception:  # noqa: BLE001
        return []
    rows = ((((data.get("data") or {}).get("client") or {}).get("ads") or {}).get("rowset") or [])
    shop_by_group: dict[str, str] = {}
    groups_with_listing: set = set()
    for r in rows:
        agid = str(r.get("adGroupId") or "")
        tn = str(r.get("__typename") or "")
        if tn == "GdShoppingAd" and agid:
            shop_by_group.setdefault(agid, str(r.get("id")))
        elif tn == "GdListingAd" and agid:
            groups_with_listing.add(agid)
    missing_shop_ids = [sid for agid, sid in shop_by_group.items()
                        if agid not in groups_with_listing]
    if not missing_shop_ids:
        return []
    return [{
        "code": "NO_LISTING",
        "id": campaign_id,
        "campaign_id": campaign_id,
        "name": campaign_name,
        "shopping_ad_ids": missing_shop_ids,
        "groups_total": len(shop_by_group),
        "severity": "medium",
        "detail": (f"{len(missing_shop_ids)}/{len(shop_by_group)} товарных групп без "
                   f"«Страниц каталога» (ListingAd) — добить by-shopping"),
    }]


# ── UAC master short-titles audit (SHORT_TITLES) ─────────────────────────────────
# Заголовок «короткий», если ≤45 из 56: запас ≥11 гарантирует, что хотя бы самый короткий
# суффикс банка («В наличии», 9+2 сепаратор) влезет — фикс всегда достижим, ре-флага нет.
_TITLE_SHORT_LEN = 45


def _uac_item_text(item: Any) -> str:
    """Текст UAC title/text-элемента (строка или dict {text|title|value|body|name})."""
    if isinstance(item, dict):
        for key in ("text", "title", "value", "body", "name"):
            text = str(item.get(key) or "").strip()
            if text:
                return text
        return ""
    return str(item or "").strip()


def _uac_titles_field(detail: dict) -> tuple[str, list]:
    """(имя поля, список элементов) заголовков из UAC detail — 'titles' или 'title_items'."""
    for key in ("titles", "title_items"):
        value = (detail or {}).get(key)
        if isinstance(value, list) and value:
            return key, value
    return "", []


def _audit_uac_short_titles(login: str, campaign_id: int, campaign_name: str,
                            agency: str | None, detail: dict | None = None) -> list[dict]:
    """tp6/tp7 Мастер: ≥2 заголовков ≤45 симв (из лимита 56) → SHORT_TITLES (авто-добивка)."""
    if detail is None:
        try:
            rc = ur.UacReadClient(login, agency=agency)
            detail = rc.campaign_detail(campaign_id)
        except Exception:  # noqa: BLE001
            return []
    if not detail:
        return []
    field_key, items = _uac_titles_field(detail)
    if not items:
        return []
    texts = [t for t in (_uac_item_text(it) for it in items) if t]
    short = [t for t in texts if len(t) <= _TITLE_SHORT_LEN]
    if len(short) < 2:
        return []  # 1 слегка короткий — не трогаем (апдейт кампании того не стоит)
    return [{
        "code": "SHORT_TITLES",
        "id": campaign_id,
        "campaign_id": campaign_id,
        "name": campaign_name,
        "field_key": field_key,
        "short_count": len(short),
        "titles_total": len(texts),
        "severity": "low",
        "detail": (f"Мастер: {len(short)}/{len(texts)} заголовков ≤{_TITLE_SHORT_LEN} симв "
                   f"(лимит 56) — добить суффиксами"),
    }]


# ── tp7 UAC feed-filter audit (report-only) ──────────────────────────────────────
def _audit_uac_feed_filters(login: str, campaign_id: int, campaign_name: str,
                            agency: str | None, detail: dict | None = None) -> list[dict]:
    """tp7 товарная UAC: должен присутствовать хотя бы один feed-фильтр (модель/вендор).
    Detail читается через uac_read (best-effort) или берётся переданный."""
    if detail is None:
        try:
            rc = ur.UacReadClient(login, agency=agency)
            detail = rc.campaign_detail(campaign_id)
        except Exception:  # noqa: BLE001
            return []
    if not detail:
        return []
    summ = ur.summarize_uac_detail(detail)
    if not summ.get("has_feed"):
        return []  # не товарная UAC
    if int(summ.get("feed_filter_conditions") or 0) <= 0:
        # Правило Семёна 03.07.2026: у некоторых фидов НЕТ поля фильтра (brand-синонима в
        # fieldsForUseAs) — такие пропускаем, минус-марки там не проставить никак.
        feed_id = 0
        try:
            feed_id = int(detail.get("feed_id") or 0)
        except (TypeError, ValueError):
            feed_id = 0
        if feed_id:
            try:
                from . import create_set_feeds as csf
                if csf._resolve_feed_field(login, feed_id, "brand") is None:
                    return []  # у фида нет поля фильтра → не флагаем
            except Exception:  # noqa: BLE001
                pass
        return [{
            "code": "FEED_FILTER_MISSING_UAC",
            "id": campaign_id,
            "campaign_id": campaign_id,
            "name": campaign_name,
            "feed_id": feed_id,
            "severity": "medium",
            "detail": "товарная UAC без feed-фильтров (нужен позитивный фильтр по марке/модели)",
        }]
    return []


# ── public API ────────────────────────────────────────────────────────────────
def _audit_sitelinks(grid: gf.GridClient, login: str, campaign_id: int,
                     camp_name: str, tp_code: str) -> list[dict]:
    """SITELINK_MISSING: у ЕПК-кампании нет прикреплённого набора быстрых ссылок
    (inheritableSitelinkSet пуст). Быстрые ссылки — обязательный ассет (#7). Чинится
    in-place (add_sitelink_set + set_campaign_sitelink_set, БЕЗ баллов) — см. fix_sitelinks_missing."""
    try:
        pl = (grid._read_unified_campaign_update_payloads([int(campaign_id)]) or {}).get(int(campaign_id)) or {}
    except Exception:  # noqa: BLE001
        return []
    if not pl:
        # payload пуст → кампания не ЕПК/GdUnifiedCampaign (напр. legacy GdTextCampaign) ИЛИ не
        # прочиталась. inheritableSitelinkSet к ней неприменим → НЕ флагаем (иначе ложный SITELINK_MISSING).
        return []
    sl = pl.get("inheritableSitelinkSet") or {}
    set_id = sl.get("sitelinkSetId") or sl.get("assetValue")
    if set_id:
        return []
    return [{"severity": "warn", "code": "SITELINK_MISSING", "id": int(campaign_id),
             "campaign_id": int(campaign_id), "name": camp_name, "tp_code": tp_code,
             "transport": "grid"}]


def audit_campaign(login: str, campaign_id: int, tp: int, ctx: dict,
                   *, grid: gf.GridClient | None = None,
                   read_client: gr.GridReadClient | None = None) -> list[dict]:
    """Audit ONE campaign against :data:`SPEC`. Returns verifier-shaped ``issues``.

    ``ctx`` should carry ``body`` with ``agent`` (slepok) and ``site_type`` so the pack
    эталон can be recomputed; ``agency`` is used for UAC reads. Grid/read clients are
    created on demand from the account's working cookie when not supplied.
    """
    body = (ctx or {}).get("body") or {}
    acc = None
    _account_ctx = _DEPS.get("_account_ctx")
    try:
        acc = _account_ctx(login) if _account_ctx else None
    except Exception:  # noqa: BLE001
        acc = None
    acc = acc or {}
    slepok = (body.get("agent") or "").strip()
    site_type = (body.get("site_type") or acc.get("site_type") or "").strip()
    city = (acc.get("city") or "")
    agency = (ctx.get("agency") or body.get("agency") or acc.get("agency") or "").strip() or None
    tp_code = f"tp{tp}"
    issues: list[dict] = []
    if tp in _SEARCH_TPS:
        g = grid or gf.GridClient(login)
        try:
            groups = g.groups_for_edit(int(campaign_id))
        except Exception:  # noqa: BLE001
            groups = []
        if groups:
            camp_name = next((x.get("campaign_name") for x in groups if x.get("campaign_name")), "")
            issues += _audit_search_keywords(groups, login, slepok, site_type, city, tp_code)
            rc = read_client or gr.GridReadClient(login)
            issues += _audit_search_images(rc, login, int(campaign_id), camp_name)
            issues += _audit_sitelinks(g, login, int(campaign_id), camp_name, tp_code)   # #7 быстрые ссылки
            if tp == 5:   # tp5 = фид-кампания: листинги + фильтры + места показа
                issues += _audit_no_listing(rc, login, int(campaign_id), camp_name)
                issues += _audit_product_feed_filters(rc, login, int(campaign_id), camp_name, groups)
                issues += _audit_placements(rc, login, int(campaign_id), camp_name)
            elif tp in (2, 4):
                # tp2/Поиск и tp4/Поиск+Динамика используют те же GdAdaptiveTextAd, что и tp1 —
                # кнопка «Получить скидку» (BUTTON_MISSING) там так же применима и её ставит
                # _apply_combo_button при создании best-effort, но раньше никто не проверял и не
                # добивал остаток (живой гэп 2026-07-06: ozge tp2 210/450, psm tp2 70/210 без
                # кнопки). groups=None гасит VIDEO_MISSING (agid_to_ct пуст без groups); IMAGE_MISSING/
                # SHORT_TITLES отфильтрованы — на поиске текстовые объявления без картинок/видео
                # by design, добивать их images_repair/video не нужно.
                issues += [i for i in _audit_tp1_adaptive(rc, login, int(campaign_id), camp_name, groups=None)
                           if i.get("code") == "BUTTON_MISSING"]
    elif tp == 1:
        g = grid or gf.GridClient(login)
        try:
            groups = g.groups_for_edit(int(campaign_id))
        except Exception:  # noqa: BLE001
            groups = []
        rc = read_client or gr.GridReadClient(login)
        camp_name = str((ctx or {}).get("campaign_name") or "")
        issues += _audit_product_feed_filters(rc, login, int(campaign_id), camp_name, groups)
        issues += _audit_tp1_adaptive(rc, login, int(campaign_id), camp_name, groups=groups)
        issues += _audit_no_listing(rc, login, int(campaign_id), camp_name)
    elif tp in (6, 7):
        # один GET detail на кампанию — и для feed-фильтров (tp7), и для коротких заголовков
        detail = None
        try:
            detail = ur.UacReadClient(login, agency=agency).campaign_detail(int(campaign_id))
        except Exception:  # noqa: BLE001
            detail = None
        camp_name = str((ctx or {}).get("campaign_name") or body.get("name") or "")
        if detail:
            if tp == 7:
                issues += _audit_uac_feed_filters(login, int(campaign_id), camp_name, agency, detail=detail)
            issues += _audit_uac_short_titles(login, int(campaign_id), camp_name, agency, detail=detail)
    return issues


def audit_account_jobs(login: str, job_result: dict) -> dict:
    """Audit every tool-created campaign of an account and return a report.

    Reads the full campaign list through Grid, classifies by tp from the name, runs
    :func:`audit_campaign` per campaign, and adds the plan⊆slepok check. The report
    reuses the verifier ``issues`` shape and attaches a ``repair_plan`` via the existing
    planner so the fixes flow through the standard executor chain.
    """
    _grid_list = _DEPS.get("_grid_list_campaigns")
    _is_tool = _DEPS.get("_is_tool_campaign")
    _account_ctx = _DEPS.get("_account_ctx")
    result = job_result or {}
    body = result.get("body") if isinstance(result.get("body"), dict) else {}
    slepok = (body.get("agent") or "").strip()
    acc = {}
    try:
        acc = (_account_ctx(login) if _account_ctx else None) or {}
    except Exception:  # noqa: BLE001
        acc = {}
    site_type = (body.get("site_type") or acc.get("site_type") or "").strip()
    agency = (result.get("agency") or body.get("agency") or acc.get("agency") or "").strip()
    ctx = {"login": login, "agency": agency, "body": {**body, "site_type": site_type}}
    try:
        campaigns = _grid_list(login) if _grid_list else []
    except Exception as e:  # noqa: BLE001
        return {"login": login, "error": f"grid list: {str(e)[:200]}", "issues": [], "campaigns": 0}
    tool = [c for c in campaigns if (not _is_tool) or _is_tool(c.get("name"))]
    grid = gf.GridClient(login)
    rc = gr.GridReadClient(login)
    all_issues: list[dict] = []
    per_tp = defaultdict(int)
    audited = 0
    for c in tool:
        if c.get("archived"):
            continue
        tp = _tp_of_name(c.get("name"))
        if not tp:
            continue
        try:
            cid = int(c.get("id"))
        except (TypeError, ValueError):
            continue
        per_tp[tp] += 1
        audited += 1
        try:
            all_issues += audit_campaign(login, cid, tp, {**ctx, "campaign_name": c.get("name")},
                                         grid=grid, read_client=rc)
        except Exception as e:  # noqa: BLE001
            all_issues.append({"code": "AUDIT_ERROR", "id": cid, "campaign_id": cid,
                               "name": str(c.get("name") or ""), "detail": str(e)[:200]})
    all_issues += _audit_plan_vs_slepok(tool, slepok, site_type)
    report = {
        "login": login,
        "slepok": slepok,
        "site_type": site_type,
        "campaigns": len(tool),
        "audited": audited,
        "per_tp": {f"tp{k}": v for k, v in sorted(per_tp.items())},
        "issues": all_issues,
        "counts": _count_by_code(all_issues),
    }
    try:
        from . import repair_planner as rp
        report["repair_plan"] = rp.build_repair_plan(report)
    except Exception:  # noqa: BLE001
        pass
    return report


def _count_by_code(issues: list[dict]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for it in issues or []:
        out[str(it.get("code") or "?")] += 1
    return dict(out)


# ── fixer orchestration (KEYWORDS_WRONG_GROUP) ───────────────────────────────────
def fix_keywords_wrong_group(login: str, ctx: dict, issues: list[dict], deps=None) -> dict:
    """Fix KEYWORDS_WRONG_GROUP issues in-place via ``repair_executor``: delete the group's
    wrong keywords (v5 keywords.delete) and re-add the correct эталонные ключи for its own ct
    (Grid AddKeywords). ``deps`` is a ``RepairDeps``; taken from ``_DEPS['_repair_deps']`` if None.
    """
    wrong = [it for it in (issues or []) if it.get("code") == "KEYWORDS_WRONG_GROUP"]
    if not wrong:
        return {"ok": True, "note": "нет KEYWORDS_WRONG_GROUP", "fixed": 0}
    if deps is None:
        rd = _DEPS.get("_repair_deps")
        deps = rd() if callable(rd) else None
    if deps is None:
        return {"ok": False, "error": "RepairDeps не прокинут (нет _repair_deps в deps)"}
    items = [{"campaign_id": it.get("campaign_id"), "adgroup_id": it.get("adgroup_id"),
              "expected_ct": it.get("expected_ct")} for it in wrong
             if it.get("adgroup_id") and it.get("campaign_id")]
    out, code = rex.execute_keywords_wrong_group_repair(login, ctx, items, deps)
    out["http_status"] = code
    return out


# ── fixer (SITELINK_MISSING): доливка быстрых ссылок ЕПК in-place (#7) ────────────
def fix_sitelinks_missing(login: str, ctx: dict, issues: list[dict]) -> dict:
    """Прикрепить набор быстрых ссылок к ЕПК-кампаниям без сайтлинков (#7), in-place через Grid
    (add_sitelink_set + set_campaign_sitelink_set, БЕЗ баллов). Один набор на аккаунт: собираем
    ссылки через _common_sitelinks_fast (БД-слепок → LLM → детерминированный резерв с href)."""
    sl_issues = [it for it in (issues or []) if it.get("code") == "SITELINK_MISSING"]
    if not sl_issues:
        return {"ok": True, "note": "нет SITELINK_MISSING", "campaigns_fixed": 0}
    cids = sorted({int(it.get("campaign_id") or 0) for it in sl_issues if it.get("campaign_id")})
    if not cids:
        return {"ok": True, "campaigns_fixed": 0}
    body = (ctx or {}).get("body") or {}
    slepok = (body.get("agent") or "").strip()
    _account_ctx = _DEPS.get("_account_ctx")
    acc = {}
    try:
        acc = (_account_ctx(login) if _account_ctx else None) or {}
    except Exception:  # noqa: BLE001
        acc = {}
    site_type = (body.get("site_type") or acc.get("site_type") or "").strip()
    city = (acc.get("city") or "")
    domain = (acc.get("domain") or "").strip()
    href = ("https://" + domain) if domain else ""
    try:
        from .create_set_feed_builders import _common_sitelinks_fast, _sitelinks_fallback_with_href
        # _common_sitelinks_fast не подставляет статик-резерв сам (чтобы не затенять v5-ассеты на
        # creation-пути) → здесь, в самодобивке, резерв обязателен: БД-слепок → LLM → детерминированный.
        sitelinks = _common_sitelinks_fast(login, slepok, site_type, city, "tp5", href=href) \
            or _sitelinks_fallback_with_href(href)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"сборка сайтлинков: {str(e)[:160]}", "campaigns_fixed": 0}
    gcl = gf.GridClient(login)
    try:
        set_id = gcl.add_sitelink_set(sitelinks)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"add_sitelink_set: {str(e)[:160]}", "campaigns_fixed": 0}
    if not set_id:
        return {"ok": False, "error": "не создан SitelinkSet (нет href/пустые ссылки?)", "campaigns_fixed": 0}
    errors: list[str] = []
    try:
        gcl.set_campaign_sitelink_set(cids, set_id)
    except Exception as e:  # noqa: BLE001
        errors.append(str(e)[:160])
    return {"ok": not errors, "campaigns_fixed": (0 if errors else len(cids)),
            "set_id": set_id, "campaigns": cids, "errors": errors[:5]}


# ── fixer (SHORT_TITLES): добивка коротких заголовков Мастера in-place ───────────
def fix_short_titles(login: str, ctx: dict, issues: list[dict]) -> dict:
    """Extend short Master (UAC) titles in place: read detail → extend each ≤45-char title
    with a neutral suffix (``ai_agents.extend_title_to_max``) → cookie PATCH (the content
    editor's ``_uac_patch_campaign_texts`` path) → read-back count of remaining short titles.
    Никаких пересозданий и баллов API — только кука."""
    short_issues = [it for it in (issues or []) if it.get("code") == "SHORT_TITLES"]
    if not short_issues:
        return {"ok": True, "note": "нет SHORT_TITLES", "campaigns_fixed": 0}
    from . import ai_agents as A  # Flask-free
    fixed, extended_total = [], 0
    errors: list[str] = []

    # ── grid-ветка (tp1 комбинаторные): RMW UpdateAdaptiveTextAds, видео сохраняется ──
    grid_issues = [it for it in short_issues if it.get("transport") == "grid"]
    if grid_issues:
        from . import create_set_feeds as csf  # самодостаточные Grid-хелперы
        gcl = gf.GridClient(login)
        rcl = gr.GridReadClient(login)
        for it in grid_issues:
            try:
                cid = int(it.get("campaign_id") or 0)
            except (TypeError, ValueError):
                continue
            ad_ids = [int(a) for a in (it.get("ad_ids") or []) if str(a).strip()]
            if cid <= 0 or not ad_ids:
                continue
            try:
                cur_map = gcl.adaptive_ads_for_update([cid], ad_ids)
                items, changed = [], 0
                for aid, cur in cur_map.items():
                    titles = [str(t) for t in (cur.get("titles") or [])]
                    used: set = set()
                    new_titles = [A.extend_title_to_max(t, idx=i, used=used)
                                  for i, t in enumerate(titles)]
                    if new_titles != titles:
                        items.append({"id": aid, "titles": new_titles})
                        changed += sum(1 for a, b in zip(titles, new_titles) if a != b)
                if items:
                    csf._grid_update_adaptive_ads(login, items, campaign_ids=[cid])
                after = [i for i in _audit_tp1_adaptive(rcl, login, cid, str(it.get("name") or ""))
                         if i.get("code") == "SHORT_TITLES"]
                still = len((after[0].get("ad_ids") or [])) if after else 0
                extended_total += changed
                fixed.append({"campaign_id": cid, "transport": "grid", "extended": changed,
                              "ads_updated": len(items), "still_short_ads": still})
            except Exception as e:  # noqa: BLE001
                errors.append(f"кампания {cid} (grid): {str(e)[:180]}")

    # ── UAC-ветка (tp6/tp7 Мастера): cookie PATCH ────────────────────────────────
    uac_issues = [it for it in short_issues if it.get("transport") != "grid"]
    if not uac_issues:
        return {"ok": not errors, "campaigns_fixed": len(fixed), "titles_extended": extended_total,
                "campaigns": fixed, "errors": errors}
    # PATCH-хелпер контент-редактора (лениво: routes_content_editor тянет flask на импорте)
    from .routes_content_editor import _uac_patch_campaign_texts, _unwrap_uac_response
    agency = ((ctx or {}).get("agency") or "").strip() or None
    try:
        client = ur.UacReadClient(login, agency=agency).client
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"uac client: {str(e)[:160]}", "campaigns_fixed": len(fixed),
                "titles_extended": extended_total, "campaigns": fixed, "errors": errors}
    for it in uac_issues:
        try:
            cid = int(it.get("campaign_id") or 0)
        except (TypeError, ValueError):
            continue
        if cid <= 0:
            continue
        try:
            detail = _unwrap_uac_response(client._request("GET", f"/campaign/{cid}",
                                                          step=f"uac-detail:{cid}"))
            field_key, items = _uac_titles_field(detail)
            if not items:
                errors.append(f"кампания {cid}: заголовки не найдены в detail")
                continue
            used: set = set()
            new_items, changed = [], 0
            for i, item in enumerate(items):
                text = _uac_item_text(item)
                ext = A.extend_title_to_max(text, idx=i, used=used) if text else text
                if ext and ext != text:
                    changed += 1
                    if isinstance(item, dict):
                        d = dict(item)
                        for key in ("text", "title", "value", "body", "name"):
                            if str(d.get(key) or "").strip() == text:
                                d[key] = ext
                                break
                        new_items.append(d)
                    else:
                        new_items.append(ext)
                else:
                    new_items.append(item)
            if not changed:
                errors.append(f"кампания {cid}: расширяемых заголовков нет (не влез ни один суффикс)")
                continue
            _uac_patch_campaign_texts(client, cid, field_key, new_items)
            # read-back: сколько коротких осталось
            after = _unwrap_uac_response(client._request("GET", f"/campaign/{cid}",
                                                         step=f"uac-readback:{cid}"))
            _, after_items = _uac_titles_field(after)
            still_short = sum(1 for x in (_uac_item_text(a) for a in after_items)
                              if x and len(x) <= _TITLE_SHORT_LEN)
            extended_total += changed
            fixed.append({"campaign_id": cid, "extended": changed, "still_short": still_short})
        except Exception as e:  # noqa: BLE001
            errors.append(f"кампания {cid}: {str(e)[:180]}")
    return {"ok": not errors, "campaigns_fixed": len(fixed), "titles_extended": extended_total,
            "campaigns": fixed, "errors": errors}


# ── fixer (BUTTON_MISSING): добивка кнопки «Получить скидку» RMW-апдейтом ─────────
def fix_button_missing(login: str, ctx: dict, issues: list[dict]) -> dict:
    """Re-apply the combo button on ads that lost it: a bare RMW ``_grid_update_adaptive_ads``
    call re-sends the current payload untouched (href/titles/bodies/images/adPrice/creativeIds —
    видео сохраняется через typedCreatives) and its встроенный ``_apply_combo_button`` ставит
    кнопку по href. Read-back — пересчёт hasButton."""
    btn_issues = [it for it in (issues or []) if it.get("code") == "BUTTON_MISSING"]
    if not btn_issues:
        return {"ok": True, "note": "нет BUTTON_MISSING", "campaigns_fixed": 0}
    from . import create_set_feeds as csf  # самодостаточные Grid-хелперы (без configure-глобалей)
    rc = gr.GridReadClient(login)
    fixed, errors = [], []
    for it in btn_issues:
        try:
            cid = int(it.get("campaign_id") or 0)
        except (TypeError, ValueError):
            continue
        ad_ids = [a for a in (it.get("ad_ids") or []) if a]
        if cid <= 0 or not ad_ids:
            continue
        try:
            n = csf._grid_update_adaptive_ads(login, [{"id": a} for a in ad_ids],
                                              campaign_ids=[cid])
            after = [i for i in _audit_tp1_adaptive(rc, login, cid, str(it.get("name") or ""))
                     if i.get("code") == "BUTTON_MISSING"]
            still = len((after[0].get("ad_ids") or [])) if after else 0
            rec = {"campaign_id": cid, "updated": n, "was_missing": len(ad_ids),
                   "still_missing": still}
            if still == 0:
                fixed.append(rec)
            else:
                # ПРАВКА 2/7: Grid read-back не подтвердил кнопку → не считать исправленной
                errors.append(f"кампания {cid}: read-back still_missing={still}/{len(ad_ids)}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"кампания {cid}: {str(e)[:180]}")
    return {"ok": not errors, "campaigns_fixed": len(fixed), "campaigns": fixed, "errors": errors}


# ── fixer (FEED_FILTER_MISSING_UAC): проставить feed_filters товарной UAC ─────────
def fix_feed_filters_uac(login: str, ctx: dict, issues: list[dict]) -> dict:
    """PATCH ``feed_filters`` на товарные UAC без фильтров: брендовый ct → позитив по
    марке/модели + минус-марки, ct0000/общая → только глобальные минус-марки. Условия
    строит боевой ``_tp7_product_feed_filters`` (создание и починка — один код), запись —
    generic ``_uac_patch_campaign_texts`` (PATCH /uac/campaign/{id}), read-back по счётчику
    conditions. Закрывает и старые UAC (report-only ранее), и недо-созданные."""
    ff_issues = [it for it in (issues or []) if it.get("code") == "FEED_FILTER_MISSING_UAC"]
    if not ff_issues:
        return {"ok": True, "note": "нет FEED_FILTER_MISSING_UAC", "campaigns_fixed": 0}
    from . import create_set_feeds as csf   # configure()-модуль: blueprint конфигурирует на импорте
    from .routes_content_editor import _unwrap_uac_response
    _ag_part1 = _DEPS.get("_ag_part1_map")
    _valid_brand = _DEPS.get("_valid_pack_brand_name")
    ct_name = {}
    try:
        ct_name = _ag_part1() if _ag_part1 else {}
    except Exception:  # noqa: BLE001
        ct_name = {}
    agency = ((ctx or {}).get("agency") or "").strip() or None
    try:
        rc = ur.UacReadClient(login, agency=agency)
        client = rc.client
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"uac client: {str(e)[:160]}", "campaigns_fixed": 0}
    fixed, errors, skipped = [], [], []
    for it in ff_issues:
        try:
            cid = int(it.get("campaign_id") or 0)
        except (TypeError, ValueError):
            continue
        if cid <= 0:
            continue
        try:
            detail = _unwrap_uac_response(client._request("GET", f"/campaign/{cid}",
                                                          step=f"uac-detail:{cid}"))
            feed_id = int(detail.get("feed_id") or 0)
            if not feed_id:
                errors.append(f"кампания {cid}: feed_id не найден в detail")
                continue
            # У фида нет поля фильтра (brand-синонима) → пропуск без ошибки (правило Семёна
            # 03.07.2026: «такие фиды пропускаем, минус-марки там не проставить никак»).
            if csf._resolve_feed_field(login, feed_id, "brand") is None:
                skipped.append({"campaign_id": cid, "reason": "у фида нет поля фильтра"})
                continue
            ct = _ct_of_name(it.get("name") or detail.get("title") or "")
            raw_brand = ct_name.get(ct) or ""
            brand = (_valid_brand(ct, raw_brand) if (_valid_brand and raw_brand) else raw_brand) or ""
            filters = csf._tp7_product_feed_filters(brand, ct, login=login, feed_id=feed_id)
            if not filters:
                skipped.append({"campaign_id": cid, "reason": "условия пустые (минус-марки выключены?)"})
                continue
            # ПРЯМОЙ partial PATCH (НЕ _uac_patch_campaign_texts: его full-detail retry
            # «успешно» шлёт весь detail, UAC молча игнорит feed_filters → MUST_BE_NULL терялся).
            try:
                client._request("PATCH", f"/campaign/{cid}",
                                json_body={"feed_filters": filters}, step=f"uac-ff:{cid}")
            except Exception as pe:  # noqa: BLE001
                if "MUST_BE_NULL" in str(pe):
                    # UAC запрещает фид-поля этому типу кампании (feedFilters MUST_BE_NULL,
                    # live 712112280) → фильтр непроставим, пропуск без ошибки.
                    skipped.append({"campaign_id": cid, "reason": "UAC: feedFilters MUST_BE_NULL"})
                    continue
                raise
            after = ur.summarize_uac_detail(rc.campaign_detail(cid))
            n_after = int(after.get("feed_filter_conditions") or 0)
            fixed.append({"campaign_id": cid, "ct": ct, "brand": brand or None,
                          "conditions_set": n_after})
            if n_after <= 0:
                errors.append(f"кампания {cid}: read-back не подтвердил фильтры")
        except Exception as e:  # noqa: BLE001
            errors.append(f"кампания {cid}: {str(e)[:180]}")
    return {"ok": not errors, "campaigns_fixed": len(fixed), "campaigns": fixed,
            "skipped": skipped, "errors": errors}


# ── fixer (VIDEO_MISSING): загрузка+attach видео ПОСЛЕ создания (deferred-video) ──
def fix_video_missing(login: str, ctx: dict, issues: list[dict]) -> dict:
    """Догрузить видео на комбинаторные объявления tp1: боевой ``_tp1_video_ads``
    (upload_video_creative → RMW-attach creativeIds; кэш per-ct, heartbeat, circuit-breaker
    на таймауте). Требование Семёна 03.07: ВСЕ видео в итоге загружены — до 3 проходов за
    вызов (пока есть прогресс), остаток идемпотентно доберут следующие циклы аудита
    (hasVideo=false никуда не денется)."""
    vm_issues = [it for it in (issues or []) if it.get("code") == "VIDEO_MISSING"]
    if not vm_issues:
        return {"ok": True, "note": "нет VIDEO_MISSING", "campaigns_fixed": 0}
    _video_ads = _DEPS.get("_tp1_video_ads")
    if not callable(_video_ads):
        return {"ok": False, "error": "_tp1_video_ads не прокинут в deps", "campaigns_fixed": 0}
    fixed, errors = [], []
    for it in vm_issues:
        try:
            cid = int(it.get("campaign_id") or 0)
        except (TypeError, ValueError):
            continue
        ads = [a for a in (it.get("ads") or []) if a.get("ad_id") and a.get("ct")]
        if cid <= 0 or not ads:
            continue
        try:
            total_attached, total_uploaded, passes = 0, 0, 0
            remaining = list(ads)
            warns: list = []
            while remaining and passes < 3:
                passes += 1
                meta = [{"id": a["ad_id"], "meta": {"ct": a["ct"]}} for a in remaining]
                vr = _video_ads(login, meta, grid_cookie=None, campaign_id=cid) or {}
                total_attached += int(vr.get("videos_attached") or 0)
                total_uploaded += int(vr.get("videos_uploaded") or 0)
                warns.extend(vr.get("warnings") or [])
                # read-back: какие объявления всё ещё без видео
                cur = gf.GridClient(login).adaptive_ads_for_update(
                    [cid], [int(a["ad_id"]) for a in remaining])
                still = [a for a in remaining
                         if not (cur.get(int(a["ad_id"])) or {}).get("hasVideo")]
                if len(still) >= len(remaining):
                    break   # прогресса нет (таймауты/пустой пул) — добьёт следующий цикл аудита
                remaining = still
            fixed.append({"campaign_id": cid, "was_missing": len(ads),
                          "attached": total_attached, "uploaded": total_uploaded,
                          "still_missing": len(remaining), "passes": passes,
                          "warnings": warns[:4]})
        except Exception as e:  # noqa: BLE001
            errors.append(f"кампания {cid}: {str(e)[:180]}")
    return {"ok": not errors, "campaigns_fixed": len(fixed), "campaigns": fixed, "errors": errors}


# ── fixer (NO_LISTING): добить «Страницы каталога» by-shopping ────────────────────
def fix_no_listing(login: str, ctx: dict, issues: list[dict]) -> dict:
    """Создать ListingAd от существующих ShoppingAd (Grid by-shopping, без баллов; листинг
    наследует текст и фильтр товарного — live-проверено 03.07: GdListingAd появился, id
    вернулся). Read-back — повторный детект; остаток идемпотентно доберут следующие циклы."""
    nl_issues = [it for it in (issues or []) if it.get("code") == "NO_LISTING"]
    if not nl_issues:
        return {"ok": True, "note": "нет NO_LISTING", "campaigns_fixed": 0}
    gcl = gf.GridClient(login)
    rc = gr.GridReadClient(login)
    fixed, errors = [], []
    for it in nl_issues:
        try:
            cid = int(it.get("campaign_id") or 0)
        except (TypeError, ValueError):
            continue
        shop_ids = [s for s in (it.get("shopping_ad_ids") or []) if s]
        if cid <= 0 or not shop_ids:
            continue
        try:
            rows = gcl.add_listing_ads_by_shopping_ads(shop_ids) or []
            after = _audit_no_listing(rc, login, cid, str(it.get("name") or ""))
            still = len((after[0].get("shopping_ad_ids") or [])) if after else 0
            fixed.append({"campaign_id": cid, "was_missing": len(shop_ids),
                          "added": len(rows), "still_missing": still})
            if still:
                errors.append(f"кампания {cid}: осталось {still} групп без листинга")
        except Exception as e:  # noqa: BLE001
            errors.append(f"кампания {cid}: {str(e)[:180]}")
    return {"ok": not errors, "campaigns_fixed": len(fixed), "campaigns": fixed, "errors": errors}


# ── fixer (IMAGE_MISSING): добить картинки боевым images_repair ───────────────────
def fix_image_missing(login: str, ctx: dict, issues: list[dict]) -> dict:
    """Догрузить картинки на голые комбинаторные объявления tp1: боевой
    ``execute_images_repair`` (ct → пул слепка → ``_cached_upload_image`` → RMW
    UpdateAdaptiveTextAds, без баллов). Репейр сам перечитывает кампанию и правит
    только объявления без imageHash — идемпотентно, остаток доберут следующие циклы."""
    im_issues = [it for it in (issues or []) if it.get("code") == "IMAGE_MISSING"]
    if not im_issues:
        return {"ok": True, "note": "нет IMAGE_MISSING", "campaigns_fixed": 0}
    rd = _DEPS.get("_repair_deps")
    deps = rd() if callable(rd) else None
    if deps is None:
        return {"ok": False, "error": "RepairDeps не прокинут в deps", "campaigns_fixed": 0}
    cids = sorted({int(it.get("campaign_id") or 0) for it in im_issues
                   if it.get("campaign_id")})
    cids = [c for c in cids if c > 0]
    if not cids:
        return {"ok": True, "note": "нет campaign_id", "campaigns_fixed": 0}
    out, code = rex.execute_images_repair(login, ctx, cids, deps)
    # 207 = часть кампаний упала — это НЕ ok (иначе мониторинг по флагу пропустит системный отказ)
    return {"ok": code == 200, "http": code,
            "campaigns_fixed": int(out.get("repaired") or 0),
            "results": (out.get("results") or [])[:20],
            "errors": [f"{f.get('campaign_id')}: {f.get('error')}"
                       for f in (out.get("failed_campaigns") or [])[:10]]}


# ── fixer (FEED_FILTER_MISSING_GRID): минус-марки на товарные/каталожные ──────────
def fix_feed_filters_grid(login: str, ctx: dict, issues: list[dict]) -> dict:
    """Проставить feedFilter с глобальными минус-марками на товарные (updateShoppingAds)
    и каталожные (updateListingAds) объявления без фильтра. Поле бренда резолвится per-feed
    (yandex.xml → mark_id, YML → vendor); фид без бренд-поля пропускается (правило Семёна —
    как MUST_BE_NULL у UAC). Оператор NOT_CONTAINS_ALL live-подтверждён 03.07 (camp 712120488)."""
    ff_issues = [it for it in (issues or []) if it.get("code") == "FEED_FILTER_MISSING_GRID"]
    if not ff_issues:
        return {"ok": True, "note": "нет FEED_FILTER_MISSING_GRID", "campaigns_fixed": 0}
    _minus = _DEPS.get("_enabled_minus_marks")
    try:
        marks = list(_minus() or []) if callable(_minus) else []
    except Exception:  # noqa: BLE001
        marks = []
    if not marks:
        return {"ok": True, "note": "минус-марки выключены", "campaigns_fixed": 0}
    from . import create_set_feeds as _csf
    gcl = gf.GridClient(login)
    rc = gr.GridReadClient(login)
    fixed, errors = [], []
    _brand_field_cache: dict[str, str | None] = {}
    for it in ff_issues:
        try:
            cid = int(it.get("campaign_id") or 0)
        except (TypeError, ValueError):
            continue
        ads = [a for a in (it.get("ads") or []) if a.get("ad_id") and a.get("feed_id")]
        if cid <= 0 or not ads:
            continue
        try:
            updated, skipped_no_field, attempted = 0, 0, 0
            for listing_flag in (False, True):
                items = []
                for a in ads:
                    if bool(a.get("listing")) != listing_flag:
                        continue
                    fid = str(a["feed_id"])
                    if fid not in _brand_field_cache:
                        try:
                            _brand_field_cache[fid] = _csf._resolve_feed_field(
                                login, int(fid), "brand")
                        except Exception:  # noqa: BLE001
                            _brand_field_cache[fid] = None
                    # ЛИСТИНГИ фильтруются ТОЛЬКО по имени каталога: поле бренда для них
                    # UNAVAILABLE_FIELD (live 03.07, все 6 кампаний) — как в set_listing_name_filters.
                    bf = "name" if listing_flag else _brand_field_cache[fid]
                    if not bf:
                        skipped_no_field += 1
                        continue          # у фида нет поля бренда — пропускаем (не ошибка)
                    items.append({
                        "id": a["ad_id"], "feed_id": fid,
                        # Канон create-пути: одно условие НА КАЖДУЮ марку (AND → исключить,
                        # если совпала любая). ОДНО условие со всеми марками имело бы другую
                        # семантику NOT_CONTAINS_ALL (ревью 03.07, 3 угла сошлись).
                        "conditions": _csf._minus_marks_grid_conditions(brand_field=bf),
                        "bodies": a.get("bodies") or [],
                    })
                if items:
                    attempted += len(items)
                    try:
                        updated += gcl.set_product_feed_filters(items, listing=listing_flag)
                    except gf.GridFinalizeError as _ffe:
                        if "UNKNOWN_FIELD" in str(_ffe):
                            # _resolve_feed_field при сбое probe отдаёт фолбэк 'vendor' —
                            # у фида такого поля может не быть: это skip, не ошибка цикла
                            skipped_no_field += len(items)
                        else:
                            raise
            after = _audit_product_feed_filters(rc, login, cid, str(it.get("name") or ""))
            still = len((after[0].get("ads") or [])) if after else 0
            fixed.append({"campaign_id": cid, "was_missing": len(ads), "updated": updated,
                          "skipped_no_brand_field": skipped_no_field,
                          "still_missing": still})
            if attempted and not updated:
                errors.append(f"кампания {cid}: 0 из {attempted} фильтров применилось "
                              f"(см. журнал set_product_feed_filters)")
        except Exception as e:  # noqa: BLE001
            errors.append(f"кампания {cid}: {str(e)[:180]}")
    return {"ok": not errors, "campaigns_fixed": len(fixed), "campaigns": fixed, "errors": errors}


# ── fixer (PLACEMENTS_WRONG): места показа tp5 узким UpdateCampaigns ──────────────
def fix_placements_wrong(login: str, ctx: dict, issues: list[dict]) -> dict:
    """Выставить «Ручную настройку» мест показа PLACEMENTS_TP5 узкой мутацией (шаблон
    set_campaign_sitelink_set: базовый скелет + эхо broadMatch). Read-back — повторный детект."""
    pw_issues = [it for it in (issues or []) if it.get("code") == "PLACEMENTS_WRONG"]
    if not pw_issues:
        return {"ok": True, "note": "нет PLACEMENTS_WRONG", "campaigns_fixed": 0}
    gcl = gf.GridClient(login)
    rc = gr.GridReadClient(login)
    fixed, errors = [], []
    for it in pw_issues:
        try:
            cid = int(it.get("campaign_id") or 0)
        except (TypeError, ValueError):
            continue
        if cid <= 0:
            continue
        if it.get("placements_ok"):
            continue   # только network_on (report-only) — placementTypes уже эталонные
        try:
            gcl.set_campaign_placement_types([cid], list(gf.PLACEMENTS_TP5))
            after = _audit_placements(rc, login, cid, str(it.get("name") or ""))
            still_bad = bool(after) and not (after[0].get("placements_ok"))
            fixed.append({"campaign_id": cid, "was": it.get("current"),
                          "fixed": not still_bad})
            if still_bad:
                errors.append(f"кампания {cid}: места показа не применились "
                              f"(сейчас {after[0].get('current')})")
        except Exception as e:  # noqa: BLE001
            errors.append(f"кампания {cid}: {str(e)[:180]}")
    return {"ok": not errors, "campaigns_fixed": len(fixed), "campaigns": fixed, "errors": errors}


# ── CLI ─────────────────────────────────────────────────────────────────────────
def _cli_bootstrap() -> None:
    """Import blueprint on LXC101 and wire audit deps (blueprint owns the IO helpers).

    Under ``python3 -m direct.campaign_spec_audit`` this file runs as ``__main__`` while
    blueprint's ``_configure_spec_audit`` configures the *imported* ``direct.campaign_spec_audit``
    — a different module object. So we explicitly ``configure`` THIS module too (populates
    ``__main__._DEPS``), otherwise the CLI would see empty deps and audit 0 campaigns.
    """
    _SCRIPTS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _SCRIPTS not in sys.path:
        sys.path.insert(0, _SCRIPTS)
    from direct import blueprint as bp  # type: ignore
    bp._configure_spec_audit()
    configure(bp._spec_audit_deps())


def _recover_body(login: str, agent: str | None, site_type: str | None) -> dict:
    """Recover a job body (agent/site_type) for CLI: prefer explicit args, else latest
    ``direct_deferred_creates`` row for the login (best-effort; may be empty)."""
    body: dict = {}
    if not agent:
        try:
            from direct import blueprint as bp  # type: ignore
            import psycopg2.extras
            conn = bp._victory_conn_rw()
            try:
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute("SELECT body FROM public.direct_deferred_creates WHERE login=%s "
                            "ORDER BY updated_at DESC LIMIT 1", (login,))
                row = cur.fetchone()
                if row and isinstance(row.get("body"), dict):
                    body = dict(row["body"])
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            body = {}
    if not agent and not body.get("agent"):
        # Восстановить слепок из директолога аккаунта: "Караваев Михаил" → "слепок_караваев" → key.
        try:
            _acc = _DEPS.get("_account_ctx")
            acc = (_acc(login) if _acc else None) or {}
            director = str(acc.get("directologist") or acc.get("director") or "").strip()
            surname = director.split()[0].lower() if director else ""
            key_map = _DEPS.get("_SLEPOK_KEY") or {}
            if surname:
                slepok_key = key_map.get(f"слепок_{surname}")
                if slepok_key:
                    body["agent"] = slepok_key
                    body.setdefault("site_type", acc.get("site_type") or "")
        except Exception:  # noqa: BLE001
            pass
    if agent:
        body["agent"] = agent
    if site_type:
        body["site_type"] = site_type
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Spec-audit кампаний аккаунта (Direct)")
    parser.add_argument("login", help="Yandex Direct login (porg-...)")
    parser.add_argument("--fix", action="store_true", help="исполнить фиксеры (KEYWORDS_WRONG_GROUP)")
    parser.add_argument("--agent", help="слепок (если не восстановить из БД)")
    parser.add_argument("--site-type", dest="site_type", help="тип сайта (override)")
    parser.add_argument("--campaign", type=int, help="только одну кампанию (fix-режим)")
    args = parser.parse_args(argv)

    _cli_bootstrap()
    body = _recover_body(args.login, args.agent, args.site_type)
    job_result = {"body": body}
    print(f"[spec-audit] login={args.login} slepok={body.get('agent') or '?'} "
          f"site_type={body.get('site_type') or '?'} fix={args.fix}")
    report = audit_account_jobs(args.login, job_result)
    if report.get("error"):
        print(f"[ERROR] {report['error']}")
        return 2
    print(f"  кампаний: {report['campaigns']}  audited: {report['audited']}  per_tp: {report['per_tp']}")
    print(f"  issues по кодам: {report['counts'] or '{}'}")
    for it in report["issues"][:80]:
        print(f"   - {it.get('code'):24} camp={it.get('campaign_id')} "
              f"adg={it.get('adgroup_id') or '-'} :: {it.get('detail') or ''}")
    if not args.fix:
        print("  (report-only; для исправления добавь --fix)")
        return 0

    ctx = {"login": args.login, "agency": report.get("agency") or "",
           "body": {**body, "site_type": report.get("site_type")}}
    issues = report["issues"]
    if args.campaign:
        issues = [it for it in issues if int(it.get("campaign_id") or 0) == args.campaign]
    fix_res = fix_keywords_wrong_group(args.login, ctx, issues)
    print(f"\n[fix] KEYWORDS_WRONG_GROUP → {fix_res}")
    fix_st = fix_short_titles(args.login, ctx, issues)
    print(f"[fix] SHORT_TITLES → {fix_st}")
    fix_btn = fix_button_missing(args.login, ctx, issues)
    print(f"[fix] BUTTON_MISSING → {fix_btn}")
    fix_ff = fix_feed_filters_uac(args.login, ctx, issues)
    print(f"[fix] FEED_FILTER_MISSING_UAC → {fix_ff}")
    fix_vm = fix_video_missing(args.login, ctx, issues)
    print(f"[fix] VIDEO_MISSING → {fix_vm}")
    fix_nl = fix_no_listing(args.login, ctx, issues)
    print(f"[fix] NO_LISTING → {fix_nl}")
    fix_im = fix_image_missing(args.login, ctx, issues)
    print(f"[fix] IMAGE_MISSING → {fix_im}")
    fix_ffg = fix_feed_filters_grid(args.login, ctx, issues)
    print(f"[fix] FEED_FILTER_MISSING_GRID → {fix_ffg}")
    fix_pw = fix_placements_wrong(args.login, ctx, issues)
    print(f"[fix] PLACEMENTS_WRONG → {fix_pw}")
    img_ids = sorted({int(i.get("campaign_id") or 0) for i in issues
                      if i.get("code") == "IMAGES_FORBIDDEN" and i.get("campaign_id")})
    if img_ids:
        rd = _DEPS.get("_repair_deps")
        deps = rd() if callable(rd) else None
        if deps is None:
            print("[fix] IMAGES_FORBIDDEN → пропуск: RepairDeps не прокинут")
        else:
            out_img, code_img = rex.execute_images_forbidden_repair(args.login, ctx, img_ids, deps)
            print(f"[fix] IMAGES_FORBIDDEN → http={code_img} {str(out_img)[:400]}")

    # read-back: пере-аудит затронутых кампаний
    touched = sorted({int(it.get("campaign_id") or 0) for it in issues
                      if it.get("code") == "KEYWORDS_WRONG_GROUP" and it.get("campaign_id")})
    if touched:
        grid = gf.GridClient(args.login)
        rc = gr.GridReadClient(args.login)
        print("\n[read-back] пере-аудит затронутых кампаний:")
        for cid in touched:
            tp = None
            try:
                groups = grid.groups_for_edit(cid)
                tp = _tp_of_name(next((x.get("campaign_name") for x in groups if x.get("campaign_name")), ""))
            except Exception:  # noqa: BLE001
                groups = []
            after = audit_campaign(args.login, cid, tp or 2, ctx, grid=grid, read_client=rc)
            wrong_after = [i for i in after if i.get("code") == "KEYWORDS_WRONG_GROUP"]
            print(f"   camp={cid}: KEYWORDS_WRONG_GROUP осталось {len(wrong_after)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

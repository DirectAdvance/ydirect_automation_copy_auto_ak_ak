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
* ``SHORT_TITLES`` — a tp1/tp2/tp4 adaptive or tp6/tp7 Master (UAC) campaign whose titles
  waste length budget (any title ≤47 of the 56 limit). Fixed by **LLM regeneration**
  (``content_quality.regen_titles`` через тот же ``_llm_pair_for``): regenerate → check
  length (≥48) → retry up to 4 → HARD-FAIL ``SHORT_TITLES_UNFIXABLE`` instead of a silent
  suffix pad. Written back via Grid RMW (grid) or cookie PATCH (UAC).
* ``BRAND_NOT_FIRST`` — adaptive tp1/tp2/tp4 ad whose group brand/model is NOT before the
  first "." of some title (breaks Yandex autotarget). Fixed by ``content_quality.regen_titles``
  with ``need_brand_first=True``; HARD-FAIL ``BRAND_NOT_FIRST_UNFIXABLE``.
"""
from __future__ import annotations

import argparse
import json
import logging
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
_TEXTS_MIN = 3   # DoD §2: у объявления должно быть ≥3 текста (CONTENT_TEXTS_LOW при меньшем)
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
            kws = _filter(pos, seg, brand, city, site_type, model=brand)
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
        # «Модели» И «Общее»/тема: их ключи обязаны быть по адресу (модельные — про свою модель;
        # тема/«Общее» — общая авто/финанс-лексика, НЕ модельные запросы). «Марки» пропускаем —
        # марочная группа легитимно делит лексику со своими моделями (D8 2026-07-09: снято
        # ограничение только-«Модели»; группа «Дром»/ct0010 получала ключи «ситирей»).
        if _ct_segment and _ct_segment(own_ct) not in ("Модели", "Общее"):
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

    # ── Детект ЧАСТИЧНОГО загрязнения чужемодельными ключами (FOREIGN_MODEL_KEYWORDS) ────────────
    # KEYWORDS_WRONG_GROUP ловит только полный сдвиг (own_hits==0). При частичном загрязнении
    # (группа CS35Plus имеет часть своих ключей + ключи CS75) own_hits>0 → выше не флагируется.
    # Новый детектор: дискриминирующие токены чужих моделей той же марки (из brand_models_catalog)
    # проверяются против КАЖДОГО живого ключа группы — чужемодельные ключи помечаются к удалению.
    _ag_part1 = _DEPS.get("_ag_part1_map")
    _valid_brand = _DEPS.get("_valid_pack_brand_name")
    kp_mod = _DEPS.get("kp")
    try:
        from .text_gen import (_foreign_model_discriminators as _fmd,
                               _model_subtokens as _mst,
                               _auto_brand_tokens as _abt)
        ct_name_fm: dict = {}
        ct_model_fm: dict = {}
        try:
            ct_name_fm = _ag_part1() if _ag_part1 else {}
        except Exception:  # noqa: BLE001
            pass
        try:
            ct_model_fm = kp_mod.feeds_ct_model() if kp_mod else {}
        except Exception:  # noqa: BLE001
            pass
        # Тема/«Общее»: набор ВСЕХ марка/модель-токенов (латиница + кириллич. транслиты)
        # — любой такой токен в тема-группе = чужемодельный ключ (D8 2026-07-09).
        try:
            all_brand_toks = set(_abt() or set())
        except Exception:  # noqa: BLE001
            all_brand_toks = set()
        already_full_shift = {it.get("adgroup_id") for it in issues
                              if it.get("code") == "KEYWORDS_WRONG_GROUP"}
        for g in search:
            own_ct = _ct_of_name(g.get("adgroup_name"))
            gid = int(g.get("adgroup_id") or 0)
            if gid in already_full_shift:
                continue  # полный сдвиг уже флаг — не дублируем
            seg_fm = _ct_segment(own_ct) if _ct_segment else "Марки"
            if seg_fm not in ("Модели", "Общее"):
                continue  # «Марки» пропускаем (легитимно делят лексику с моделями)
            live_kws = list(g.get("keywords") or [])
            if not live_kws:
                continue
            if seg_fm == "Общее":
                # Тема-группа не должна нести НИ ОДНОГО модель/марка-запроса. Дискриминатор —
                # общий набор _auto_brand_tokens (word-boundary матч по токену). fail-safe:
                # набор пуст (kp/feeds недоступны) → не флагаем вслепую.
                if not all_brand_toks:
                    continue
                foreign_kws = []
                for kw in live_kws:
                    if _mst(str(kw)) & all_brand_toks:
                        foreign_kws.append(kw)
                brand_fm = "тема/Общее"
                if foreign_kws:
                    cid_fm = g.get("campaign_id")
                    issues.append({
                        "code": "FOREIGN_MODEL_KEYWORDS",
                        "id": cid_fm,
                        "campaign_id": cid_fm,
                        "name": str(g.get("campaign_name") or ""),
                        "adgroup_id": gid,
                        "model": brand_fm,
                        "foreign_kws": [str(k) for k in foreign_kws[:50]],
                        "foreign_count": len(foreign_kws),
                        "live_count": len(live_kws),
                        "severity": "medium",
                        "detail": (f"тема-группа ct={own_ct}: {len(foreign_kws)}/{len(live_kws)} ключей "
                                   f"содержат токены марок/моделей — не для «Общей» группы, удалить "
                                   f"v5 keywords.delete"),
                    })
                continue
            raw_brand = ct_name_fm.get(own_ct) or ct_model_fm.get(own_ct) or ""
            brand_fm = ((_valid_brand(own_ct, raw_brand) if _valid_brand else raw_brand)
                        if raw_brand else "")
            if not brand_fm or brand_fm == "Авто":
                continue
            disc = _fmd(brand_fm)
            if not disc:
                continue  # единственная модель марки — нет чужих
            foreign_kws = [kw for kw in live_kws if _mst(str(kw)) & disc]
            if foreign_kws:
                cid_fm = g.get("campaign_id")
                issues.append({
                    "code": "FOREIGN_MODEL_KEYWORDS",
                    "id": cid_fm,
                    "campaign_id": cid_fm,
                    "name": str(g.get("campaign_name") or ""),
                    "adgroup_id": gid,
                    "model": brand_fm,
                    "foreign_kws": [str(k) for k in foreign_kws[:50]],
                    "foreign_count": len(foreign_kws),
                    "live_count": len(live_kws),
                    "severity": "medium",
                    "detail": (f"группа ct={own_ct} ({brand_fm}): {len(foreign_kws)}/{len(live_kws)} ключей "
                               f"содержат токены чужих моделей той же марки — удалить v5 keywords.delete"),
                })
    except Exception:  # noqa: BLE001 — дискриминатор опционален, не ломаем аудит
        pass

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
    # Не early-return по marks: ListingAd позитивный name-фильтр (fix-3) проверяется независимо.
    # ShoppingAd и ListingAd (минус) пропускают строки при marks=[] в теле цикла — поведение прежнее.
    q = ("query SpecPFF($login:String!,$inp:GdAdsContainerInput!){"
         "client(searchBy:{login:$login}){ads(input:$inp){rowset{id adGroupId __typename "
         "...on GdShoppingAd{bodies feed{id} feedFilter{tab conditions{field}}} "
         "...on GdListingAd{bodies feed{id} feedFilter{tab conditions{field operator}}}}}}}")
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
    agid_to_name = {str(g.get("adgroup_id") or ""): str(g.get("adgroup_name") or "")
                    for g in (groups or []) if g.get("adgroup_id")}
    _ct_segment_fn = _DEPS.get("_ct_segment")
    ads_missing: list[dict] = []
    listing_pos_missing: list[dict] = []   # ListingAd без позитивного name-фильтра в «Марки»-группе
    total_product = 0
    for r in rows:
        tn = str(r.get("__typename") or "")
        if tn not in ("GdShoppingAd", "GdListingAd"):
            continue
        total_product += 1
        ff = r.get("feedFilter")
        agid_str = str(r.get("adGroupId") or "")
        if tn == "GdShoppingAd":
            # ShoppingAd: детект только при включённых минус-марках (исходное поведение)
            if not marks:
                continue
            if ff and (ff.get("conditions") or []):
                continue
            if ff and str(ff.get("tab") or "") not in ("", "CONDITION"):
                continue   # tree/категорийный фильтр: conditions=null легитимен
            ads_missing.append({
                "ad_id": str(r.get("id")),
                "listing": False,
                "feed_id": str((r.get("feed") or {}).get("id") or ""),
                "bodies": list(r.get("bodies") or []),
                "ct": agid_to_ct.get(agid_str) or "",
            })
        else:
            # GdListingAd «Страницы каталога»:
            # (A) Минус-марки: только при включённых марках (FEED_FILTER_MISSING_GRID, как раньше).
            # (B) Позитивный name-фильтр (fix-3): брендовые «Марки»-группы, независимо от marks.
            ct = agid_to_ct.get(agid_str) or ""
            seg = _ct_segment_fn(ct) if callable(_ct_segment_fn) else ""
            # (A) null feedFilter + включены марки → нужны минус-марки
            if marks and not ff:
                ads_missing.append({
                    "ad_id": str(r.get("id")),
                    "listing": True,
                    "feed_id": str((r.get("feed") or {}).get("id") or ""),
                    "bodies": list(r.get("bodies") or []),
                    "ct": ct,
                })
            # (B) брендовая «Марки»-группа без позитивного CONTAINS_ANY на name → весь фид в каталоге
            if seg == "Марки":
                _pos_name = any(
                    c.get("field") == "name"
                    and "CONTAINS" in str(c.get("operator") or "")
                    and "NOT" not in str(c.get("operator") or "")
                    for c in ((ff or {}).get("conditions") or [])
                )
                if not _pos_name:
                    _grp_name = agid_to_name.get(agid_str, "")
                    _brand = _grp_name.split(" — ", 1)[1].strip() if " — " in _grp_name else ""
                    listing_pos_missing.append({
                        "ad_id": str(r.get("id")),
                        "listing": True,
                        "feed_id": str((r.get("feed") or {}).get("id") or ""),
                        "bodies": list(r.get("bodies") or []),
                        "ct": ct,
                        "brand": _brand,
                    })
    out_issues: list[dict] = []
    if ads_missing:
        out_issues.append({
            "code": "FEED_FILTER_MISSING_GRID",
            "id": campaign_id,
            "campaign_id": campaign_id,
            "name": campaign_name,
            "ads": ads_missing,
            "ads_total": total_product,
            "severity": "medium",
            "detail": (f"{len(ads_missing)}/{total_product} товарных/каталожных объявл. без "
                       f"feedFilter — добить минус-марками (поле бренда per-feed)"),
        })
    if listing_pos_missing:
        out_issues.append({
            "code": "LISTING_POSITIVE_FILTER_MISSING",
            "id": campaign_id,
            "campaign_id": campaign_id,
            "name": campaign_name,
            "ads": listing_pos_missing,
            "ads_total": total_product,
            "severity": "high",
            "detail": (f"{len(listing_pos_missing)}/{total_product} каталожных объявл. (ListingAd) "
                       f"без позитивного name-фильтра в брендовой «Марки»-группе — весь фид в каталоге"),
        })
    return out_issues


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


# ── tp5 generic fallback group audit (GENERIC_FALLBACK_GROUP) ────────────────────
def _audit_generic_fallback_group(groups: list, campaign_id: int, campaign_name: str) -> list[dict]:
    """tp5: одна generic ct0000-группа «Товарная галерея» вместо бренд-сегментных групп.

    Признак: ровно 1 группа, имя содержит «Товарная галерея» и _ct_of_name → ct0000,
    при этом имя КАМПАНИИ содержит сегментный маркер (Марки/Модели/Общее) —
    значит кампания должна была получить бренд-группы из M3, но получила пустышку.

    Инцидент 2026-07-06: 5 tp5 porg-psm5h7q6 Щербакова созданы через
    _create_shopping_via_cookie без поддержки segment → у каждой 1 группа ct0000.
    НЕ флагаем: tp5-Фиды/products_only (сегмента нет в имени), корректные multi-group."""
    if len(groups) != 1:
        return []
    g = groups[0]
    ag_name = (g.get("adgroup_name") or "").strip()
    if _ct_of_name(ag_name) != "ct0000" or "Товарная галерея" not in ag_name:
        return []
    camp_seg = next((s for s in ("Марки", "Модели", "Общее") if s in campaign_name), "")
    if not camp_seg:
        return []   # несегментная tp5 (Фиды/Автотаргет) — одна generic-группа нормальна
    return [{
        "code": "GENERIC_FALLBACK_GROUP",
        "id": campaign_id,
        "campaign_id": campaign_id,
        "name": campaign_name,
        "severity": "high",
        "segment": camp_seg,
        "group_name": ag_name,
        "detail": (f"tp5 сегмент «{camp_seg}»: 1 generic ct0000-группа «{ag_name}» "
                   "вместо бренд-сегментных групп из M3-пака — "
                   "кампания создана без поддержки сегментации (cookie-путь); "
                   "требует пересоздания с API-токеном"),
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


def _audit_group_count_vs_slepok(grid: gf.GridClient, tool: list[dict], slepok: str,
                                 site_type: str) -> list[dict]:
    """GROUP_COUNT_BELOW_SLEPOK (D10 2026-07-09, **report-only warn**): по каждому tp (1/2/4/5)
    аккаунт должен покрывать НЕ МЕНЬШЕ модель-ct, чем объявлено в структуре слепка.

    ``_audit_plan_vs_slepok`` проверял только НАЛИЧИЕ типа, не число групп. Здесь — агрегатное
    покрытие по аккаунту: distinct не-ct0000 модель-ct в живых группах tp vs ``_struct_cts``.
    Осознанное упрощение: сравнение АГРЕГАТНОЕ (по всем кампаниям tp), НЕ per-campaign/per-сегмент
    (segment-per-campaign надёжно не маппится); tp5 feed-driven — его brands могут отличаться от
    slepok-ct, поэтому warn+report-only, БЕЗ авто-фиксера (иначе детект без ремонта зациклил бы
    reschedule «до нуля», журнал I). Fail-safe: структура пуста / группы не прочитались → [].
    Групп читаем через ``groups_for_edit`` (edit-view) — но это НЕ триггерит destructive-ремонт,
    поэтому edit-view лаг максимум даёт лишний warn в отчёте, ничего не удаляет."""
    _struct = _DEPS.get("_struct_cts")
    if not (slepok and _struct):
        return []
    tp_map = {1: "tp1", 2: "tp2", 4: "tp4", 5: "tp5"}
    # tp → set ожидаемых модель-ct из структуры слепка
    expected_by_tp: dict[int, set] = {}
    for tp, code in tp_map.items():
        try:
            cts = {str(c).lower() for c in (_struct(slepok, site_type, code) or [])
                   if c and str(c).lower() != "ct0000"}
        except Exception:  # noqa: BLE001
            cts = set()
        if cts:
            expected_by_tp[tp] = cts
    if not expected_by_tp:
        return []
    # Живые cid'ы по нужным tp
    cid_tp: dict[int, int] = {}
    for c in tool:
        tp = _tp_of_name(c.get("name"))
        if tp in expected_by_tp:
            try:
                cid_tp[int(c.get("id"))] = tp
            except (TypeError, ValueError):
                continue
    if not cid_tp:
        return []
    try:
        groups = grid.groups_for_edit(list(cid_tp.keys())) or []
    except Exception:  # noqa: BLE001
        return []
    if not groups:
        return []   # fail-safe: не прочитали группы → не судим
    live_by_tp: dict[int, set] = defaultdict(set)
    for g in groups:
        tp = _tp_of_name(g.get("campaign_name")) or cid_tp.get(int(g.get("campaign_id") or 0))
        if tp not in expected_by_tp:
            continue
        ct = _ct_of_name(g.get("adgroup_name"))
        if ct and ct != "ct0000":
            live_by_tp[tp].add(ct)
    issues: list[dict] = []
    for tp, exp in expected_by_tp.items():
        if tp not in {_tp_of_name(c.get("name")) for c in tool}:
            continue   # tp вообще нет в аккаунте — это забота _audit_plan_vs_slepok, не наша
        live = live_by_tp.get(tp, set())
        if len(live) < len(exp):
            missing = sorted(exp - {c.lower() for c in live})
            issues.append({
                "code": "GROUP_COUNT_BELOW_SLEPOK",
                "tp": tp,
                "slepok": slepok,
                "expected_ct_count": len(exp),
                "live_ct_count": len(live),
                "missing_cts": missing[:40],
                "severity": "warn",
                "fixable": False,
                "detail": (f"tp{tp}: живых модель-групп {len(live)} < слепка {len(exp)} "
                           f"(не хватает ct: {missing[:12]}...) — report-only, добить пересозданием "
                           f"недостающих позиций"),
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
        #
        # brand_hint: для «Марки»-ct (напр. ct0111 Haval) feeds_ct_model() не содержит записи
        # (нет фид-картинки с именем модели) → brand_word в videos_pool_for_ct оставался бы ""
        # → brand-fallback полностью пропускался → пул пуст → VIDEO_MISSING не эмитился
        # (ложно-зелёный still_missing:0). Резолвим из gsheet_naming.ag_part1 (уже в _DEPS).
        brand_hint = ""
        _ag_part1 = _DEPS.get("_ag_part1_map")
        if _ag_part1:
            try:
                full_name = (_ag_part1() or {}).get(ct_norm, "")
                brand_hint = full_name.strip().split()[0] if full_name else ""
            except Exception:  # noqa: BLE001
                pass
        paths = kp_mod.videos_pool_for_ct(ct_norm, limit=1, brand_hint=brand_hint)
        return bool(paths)
    except Exception:  # noqa: BLE001
        return False


def _audit_tp1_adaptive(rc: gr.GridReadClient, login: str, campaign_id: int,
                        campaign_name: str, groups: list | None = None) -> list[dict]:
    """tp1 РСЯ, один Grid-запрос — два детекта по комбинаторным объявлениям:

    * BUTTON_MISSING — объявление с валидным href без кнопки «Получить скидку»
      (_apply_combo_button при создании best-effort: 29/50 без кнопки, инцидент 03.07.2026);
    * SHORT_TITLES (transport=grid) — объявление с ЛЮБЫМ заголовком <48 симв из 56
      (скрин #78: группа BAIC с запасом 8–18). Фикс — RMW, видео сохраняется (typedCreatives).
    """
    q = ("query SpecTp1($login:String!,$inp:GdAdsContainerInput!){"
         "client(searchBy:{login:$login}){ads(input:$inp){rowset{id campaignId adGroupId "
         "__typename ...on GdAdaptiveTextAd{href hasButton hasVideo titles bodies images{imageHash}}}}}}")
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
    # BUTTON_MISSING_NO_HREF: объявления без кнопки И без валидного href.
    # fix_button_missing/_apply_combo_button требует href в объявлении — без него кнопку
    # поставить нельзя через RMW. Detect-only: делаем видимым в аудите вместо тихого пропуска.
    no_href = [str(r.get("id")) for r in rows
               if r.get("hasButton") is False
               and not re.match(r"https?://", str(r.get("href") or ""))]
    if no_href:
        issues.append({
            "code": "BUTTON_MISSING_NO_HREF",
            "id": campaign_id,
            "campaign_id": campaign_id,
            "name": campaign_name,
            "ad_ids": no_href,
            "ads_total": len(rows),
            "severity": "low",
            "fixable": False,
            "detail": (f"РСЯ: {len(no_href)}/{len(rows)} объявл. без кнопки и без href"
                       " — автофикс невозможен, требует ручной проверки (detect-only)"),
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
    video_no_pool: list[str] = []   # ct без роликов в пуле M3 (детерминированный пропуск — норма)
    if agid_to_ct:
        for r in rows:
            if r.get("hasVideo") is not False:
                continue
            ct = agid_to_ct.get(str(r.get("adGroupId") or ""))
            if not ct or ct == "ct0000":
                continue
            if _ct_has_pool_video(ct):
                # brand_hint: резолв из ag_part1 (зеркально _ct_has_pool_video) —
                # нужен фетчеру videos_for_ct для «Марки»-ct без записи в feeds_ct_model()
                _brand_h = ""
                _ag1 = _DEPS.get("_ag_part1_map")
                if _ag1:
                    try:
                        _ct_n = (ct or "").strip().lower()
                        _fn = (_ag1() or {}).get(_ct_n, "")
                        _brand_h = _fn.strip().split()[0] if _fn else ""
                    except Exception:  # noqa: BLE001
                        pass
                video_missing.append({"ad_id": str(r.get("id")), "ct": ct, "brand": _brand_h})
            elif ct not in video_no_pool:
                # Пул M3 пуст для этого ct → пропуск детерминированный (не дефект добивки).
                # Логируем чтобы отличать «пула нет» от «репар не доработал» (п.4 fix 2.3).
                video_no_pool.append(ct)
    if video_missing:
        _issue_vm: dict = {
            "code": "VIDEO_MISSING",
            "id": campaign_id,
            "campaign_id": campaign_id,
            "name": campaign_name,
            "ads": video_missing,
            "ads_total": len(rows),
            "severity": "low",
            "detail": (f"РСЯ: {len(video_missing)}/{len(rows)} объявл. без видео при наличии "
                       f"роликов в пуле — добить загрузкой (deferred-video)"),
        }
        if video_no_pool:
            _issue_vm["video_no_pool_cts"] = video_no_pool   # ct с пустым пулом — не ложный missing
        issues.append(_issue_vm)
    elif video_no_pool:
        # Только «нет пула» (без fixable missing): делаем видимым в аудите — отдельный info-issue.
        issues.append({
            "code": "VIDEO_NO_POOL",
            "id": campaign_id,
            "campaign_id": campaign_id,
            "name": campaign_name,
            "no_pool_cts": video_no_pool,
            "severity": "info",
            "fixable": False,
            "detail": (f"РСЯ: {len(video_no_pool)} ct без роликов в пуле M3 "
                       "(hasVideo=false, но видео класть некуда — detect-only, не дефект добивки)"),
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
        if n_short >= 1:
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
            "detail": (f"РСЯ: {len(short_ads)}/{len(rows)} объявл. с ЛЮБЫМ заголовком "
                       f"<{_TITLE_SHORT_LEN + 1} симв (лимит 56) — добить суффиксами (RMW)"),
        })
    # CONTENT_TEXTS_LOW (D9 2026-07-09): адаптивное объявление с <3 текстами (bodies). DoD §2
    # требует ≥3 текста. Fail-safe: bodies is None (Grid не отдал поле) → пропускаем объявление
    # (не флагаем вслепую); считаем только по объявлениям с реально прочитанными bodies.
    low_text_ads = []
    for r in rows:
        if r.get("__typename") != "GdAdaptiveTextAd":
            continue
        bodies_field = r.get("bodies")
        if bodies_field is None:
            continue   # поле не прочитано → fail-safe, не судим
        bodies = [str(b) for b in bodies_field if str(b or "").strip()]
        if len(bodies) < _TEXTS_MIN:
            low_text_ads.append(str(r.get("id")))
    if low_text_ads:
        issues.append({
            "code": "CONTENT_TEXTS_LOW",
            "id": campaign_id,
            "campaign_id": campaign_id,
            "name": campaign_name,
            "transport": "grid",
            "ad_ids": low_text_ads,
            "ads_total": len(rows),
            "severity": "low",
            "detail": (f"РСЯ: {len(low_text_ads)}/{len(rows)} объявл. с <{_TEXTS_MIN} текстами — "
                       f"добить регенерацией/филлерами (RMW)"),
        })
    return issues


# ── ct-slepok images audit (CT_SLEPOK_IMAGES_EMPTY, D5 minimum) ──────────────────
def _audit_ct_slepok_images(campaign_id: int, campaign_name: str, groups: list | None,
                            slepok: str, site_type: str) -> list[dict]:
    """CT_SLEPOK_IMAGES_EMPTY (D5 2026-07-09, **report-only warn, minimum**): брендовая/модельная
    tp1-группа, для чьего ct в СЛЕПКЕ нет ни одной картинки (`kp.has_slepok_images`==False) →
    группа неизбежно берёт картинки только из ОБЩЕГО пула, не из своей ct.

    Осознанное упрощение (задача разрешила «минимум»): проверяем НАЛИЧИЕ ct-картинок в манифесте
    слепка (лёгкий чек без скачивания байтов), а НЕ «сколько живых картинок объявления из ct-папки»
    (для последнего нужен per-image reverse-lookup, дорого). Это ловит корень «пустой слепок по ct /
    маппинг в ct0000». Report-only, БЕЗ авто-фиксера: наполнение слепка картинками — задача контента
    (слепки-мастер), не кода создания. Fail-safe: нет kp/слепка/site_type или чек упал → [].
    """
    if not (groups and slepok and site_type):
        return []
    kp_mod = _DEPS.get("kp")
    _ct_segment = _DEPS.get("_ct_segment")
    if not (kp_mod and hasattr(kp_mod, "has_slepok_images")):
        return []
    slepok_key = _selected_slepok_key(slepok)
    checked: set = set()
    empty_cts: list[str] = []
    for g in groups:
        ct = _ct_of_name(g.get("adgroup_name"))
        if not ct or ct == "ct0000" or ct in checked:
            continue
        seg = _ct_segment(ct) if callable(_ct_segment) else "Марки"
        if seg not in ("Марки", "Модели"):
            continue   # только брендовые/модельные группы (общие берут общий пул by design)
        checked.add(ct)
        try:
            has = kp_mod.has_slepok_images(site_type, "tp1", ct, slepok_key)
        except Exception:  # noqa: BLE001
            continue   # fail-safe: чек упал → не судим
        if not has:
            empty_cts.append(ct)
    if not empty_cts:
        return []
    return [{
        "code": "CT_SLEPOK_IMAGES_EMPTY",
        "id": int(campaign_id),
        "campaign_id": int(campaign_id),
        "name": campaign_name,
        "cts": sorted(empty_cts)[:40],
        "severity": "warn",
        "fixable": False,
        "detail": (f"tp1: {len(empty_cts)} брендовых ct без картинок в слепке {slepok} "
                   f"(берут только общий пул): {sorted(empty_cts)[:12]} — наполнить слепок (контент)"),
    }]


# ── brand-first audit (BRAND_NOT_FIRST): марка/модель ДО первой точки заголовка ──────
def _audit_brand_not_first(rc: gr.GridReadClient, login: str, campaign_id: int,
                           campaign_name: str, groups: list | None) -> list[dict]:
    """BRAND_NOT_FIRST: адаптивное объявление, где марка/модель ЕГО ГРУППЫ не стоит ДО
    первой точки хотя бы одного заголовка (нарушение brand-first под автотаргет Яндекса —
    он матчит по началу заголовка). Работает ТОЛЬКО там, где группа завязана на конкретную
    марку/модель (ct != ct0000 и марка резолвится через ``_ag_part1_map``). Общие/аудиторные
    группы (ct0000) не флагаем — им нечего ставить в начало. Фикс — LLM-регенерация
    заголовков c ``need_brand_first=True`` (``fix_brand_not_first``)."""
    if not groups:
        return []
    _ag_part1 = _DEPS.get("_ag_part1_map")
    try:
        ct_name = (_ag_part1() if _ag_part1 else {}) or {}
    except Exception:  # noqa: BLE001
        ct_name = {}
    if not ct_name:
        return []
    agid_to_brand: dict[str, str] = {}
    for g in groups:
        gid = str(g.get("adgroup_id") or "")
        ct = _ct_of_name(g.get("adgroup_name"))
        if not gid or not ct or ct == "ct0000":
            continue
        nm = str(ct_name.get(ct.strip().lower()) or "").strip()
        if nm:
            agid_to_brand[gid] = nm
    if not agid_to_brand:
        return []
    q = ("query SpecBrandFirst($login:String!,$inp:GdAdsContainerInput!){"
         "client(searchBy:{login:$login}){ads(input:$inp){rowset{id adGroupId __typename "
         "...on GdAdaptiveTextAd{titles}}}}}")
    inp = {
        "filter": {"campaignIdIn": [str(campaign_id)]},
        "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
        "limitOffset": {"limit": 5000, "offset": 0},
        "orderBy": [{"order": "ASC", "field": "ID"}],
    }
    try:
        data = rc._post("SpecBrandFirst", q, {"login": login, "inp": inp})
    except Exception:  # noqa: BLE001
        return []
    rows = ((((data.get("data") or {}).get("client") or {}).get("ads") or {}).get("rowset") or [])
    from .content_quality import brand_head_ok
    bad: list[dict] = []   # [{ad_id, brand}]
    for r in rows:
        brand = agid_to_brand.get(str(r.get("adGroupId") or ""))
        if not brand:
            continue
        titles = [str(t) for t in (r.get("titles") or []) if str(t or "").strip()]
        if not titles:
            continue
        if any(not brand_head_ok(t, brand) for t in titles):
            bad.append({"ad_id": str(r.get("id")), "brand": brand})
    if not bad:
        return []
    return [{
        "code": "BRAND_NOT_FIRST",
        "id": campaign_id,
        "campaign_id": campaign_id,
        "name": campaign_name,
        "transport": "grid",
        "ads": bad,
        "ads_total": len(rows),
        "severity": "low",
        "detail": (f"{len(bad)}/{len(rows)} объявл. с заголовком, где марка НЕ до первой точки — "
                   f"регенерация brand-first (LLM)"),
    }]


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
# Заголовок «короткий», если <48 (≤47) из 56 (требование Семёна: остаток ≤8 у КАЖДОГО).
# Для ≤46 фикс суффиксом всегда достижим (46+". "+гарантия(8)=56); 47 — best-effort
# (зависит от разделителя: заголовок на '.' → sep=" " → 47+1+8=56 ✓, иначе 57>56).
_TITLE_SHORT_LEN = 47


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
    """tp6/tp7 Мастер: ЛЮБОЙ заголовок <48 симв (из лимита 56) → SHORT_TITLES (авто-добивка)."""
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
    if not short:
        return []  # ни одного короткого — всё в порядке
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


# ── UAC video-brand audit (UAC_VIDEO_MISSING) ────────────────────────────────────
# Марки с видео-пулом (BAIC/Belgee/Haval/Москвич) — как в tp1-добивке (журнал VIDEO_NO_POOL).
_UAC_VIDEO_BRANDS: tuple = (
    ("baic", "баик", "baic"),
    ("belgee", "белджи", "belgee"),
    ("haval", "хавал", "haval"),
    ("moskvich", "москвич", "moskvich"),
)


def _uac_video_brand(name: str) -> str:
    """Если имя UAC-кампании относится к видео-марке (BAIC/Belgee/Haval/Москвич) — вернуть
    brand_hint для резолва видео-пула, иначе ''. Матч по токену (латиница+кириллица)."""
    low = str(name or "").lower()
    for lat, cyr, hint in _UAC_VIDEO_BRANDS:
        if re.search(r"(?<![a-zа-яё0-9])(?:" + re.escape(lat) + "|" + re.escape(cyr)
                     + r")(?![a-zа-яё0-9])", low):
            return hint
    return ""


def _audit_uac_video_missing(login: str, campaign_id: int, campaign_name: str,
                             agency: str | None, detail: dict | None = None) -> list[dict]:
    """tp6/tp7 UAC: видео-марка (BAIC/Belgee/Haval/Москвич) без единого видео → UAC_VIDEO_MISSING.

    D3-UAC 2026-07-09: до этого пост-аудит UAC вообще не проверял видео (только feed-фильтры +
    короткие заголовки). Ремонт — через существующий UAC recreate (`UAC_VIDEO_MISSING` в
    `_RECREATE_CODES` планировщика → resume_or_recreate_campaign с довложением видео).

    Fail-safe (журнал I/J — не флагать вслепую):
    * detail не прочитан → [] (нет данных);
    * имя не относится к видео-марке → [] (нечего проверять);
    * медиа-блок не прочитан (images==0 И content==0) → [] (не знаем, есть ли видео);
    * videos>0 → [] (видео уже есть);
    * пул для марки пуст (`videos_pool_for_ct` brand_hint) → [] (VIDEO_NO_POOL — не дефект).
    Флаг только когда: видео-марка И медиа реально прочитано И videos==0 И пул НЕ пуст.
    """
    if detail is None:
        try:
            detail = ur.UacReadClient(login, agency=agency).campaign_detail(campaign_id)
        except Exception:  # noqa: BLE001
            return []
    if not detail:
        return []
    brand_hint = _uac_video_brand(campaign_name)
    if not brand_hint:
        return []
    summ = ur.summarize_uac_detail(detail)
    videos = int(summ.get("videos") or 0)
    if videos > 0:
        return []
    # Медиа-блок реально прочитан? Если ни картинок, ни контента — detail мог не отдать медиа →
    # не знаем, есть ли видео → НЕ флагаем (fail-safe против ложного детекта на неполном detail).
    if int(summ.get("images") or 0) <= 0 and int(summ.get("content") or 0) <= 0:
        return []
    # Пул для марки существует? Пусто → VIDEO_NO_POOL, не дефект.
    kp_mod = _DEPS.get("kp")
    try:
        pool = kp_mod.videos_pool_for_ct("", limit=1, brand_hint=brand_hint) if kp_mod else []
    except Exception:  # noqa: BLE001
        pool = []
    if not pool:
        return []
    return [{
        "code": "UAC_VIDEO_MISSING",
        "id": campaign_id,
        "campaign_id": campaign_id,
        "name": campaign_name,
        "brand": brand_hint,
        "severity": "low",
        "detail": (f"UAC видео-марка ({brand_hint}) без видео (videos=0, пул есть) — "
                   f"довложить видео через recreate"),
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


def _audit_callouts(grid: gf.GridClient, login: str, campaign_id: int,
                    camp_name: str, tp_code: str) -> list[dict]:
    """CALLOUTS_MISSING: ЕПК-кампания без прикреплённых уточнений (inheritableCallouts пуст).

    Уточнения — campaign-level Grid-ассет (inheritableCallouts.calloutIds). Не применимо к
    UAC (tp6/tp7) — вызывающая сторона обязана не передавать эти tp. Payload читается тем же
    путём что _audit_sitelinks (_read_unified_campaign_update_payloads); если кампания не
    GdUnifiedCampaign или payload не прочитался — не флагаем (возвращаем []). Severity low:
    недоделка, не showstopper (как VIDEO_MISSING/BUTTON_MISSING)."""
    try:
        pl = (grid._read_unified_campaign_update_payloads([int(campaign_id)]) or {}).get(int(campaign_id)) or {}
    except Exception:  # noqa: BLE001
        return []
    if not pl:
        # payload пуст → кампания не ЕПК/GdUnifiedCampaign → inheritableCallouts неприменимо
        return []
    co = pl.get("inheritableCallouts") or {}
    callout_ids = co.get("calloutIds") or []
    if callout_ids:
        return []
    return [{"severity": "low", "code": "CALLOUTS_MISSING", "id": int(campaign_id),
             "campaign_id": int(campaign_id), "name": camp_name, "tp_code": tp_code,
             "detail": "кампания без уточнений (inheritableCallouts пуст) — добить set_campaign_callouts"}]


def _audit_global_minus_campaign(grid: gf.GridClient, login: str, campaign_id: int,
                                 camp_name: str, tp_code: str) -> list[dict]:
    """GLOBAL_MINUS_CAMPAIGN_MISSING (D6 2026-07-09): у поисковых кампаний (tp2/tp4/tp5) на
    уровне КАМПАНИИ должны стоять глобальные минус-слова («отзывы», `_enabled_minus_words`).

    Read: unified-payload (тот же путь, что _audit_sitelinks) — inline ``minusKeywords`` +
    ``libraryMinusKeywordsIds`` (shared-set). Fail-safe (журнал I — не флагать вслепую):
    * payload не прочитан / не GdUnifiedCampaign → [] (не судим);
    * ``libraryMinusKeywordsIds`` НЕ пуст → [] (слова могут быть в shared-set, содержимое
      которого мы дёшево не резолвим — предполагаем покрытие, не флагаем ложно);
    * набор `_enabled_minus_words` пуст → [] (нечего требовать).
    Флаг ТОЛЬКО когда shared-set нет И inline-minus не содержит все требуемые слова.
    Ремонт (`fix_global_minus_campaign`) добавляет их inline (Grid UpdateCampaigns, без баллов),
    что консистентно с детектом (после ремонта inline содержит слова → детект молчит, без цикла)."""
    _enabled = _DEPS.get("_enabled_minus_words")
    try:
        want = [str(w).strip() for w in (_enabled() or [])] if callable(_enabled) else []
    except Exception:  # noqa: BLE001
        want = []
    want = [w for w in want if w]
    if not want:
        return []
    try:
        pl = (grid._read_unified_campaign_update_payloads([int(campaign_id)]) or {}).get(int(campaign_id)) or {}
    except Exception:  # noqa: BLE001
        return []
    if not pl:
        return []   # не ЕПК/не прочитано → не судим
    if pl.get("libraryMinusKeywordsIds"):
        return []   # есть shared-set минусов → считаем покрытым (fail-safe)
    inline = {str(m).strip().lower() for m in (pl.get("minusKeywords") or [])}
    missing = [w for w in want if w.lower() not in inline]
    if not missing:
        return []
    return [{
        "severity": "warn",
        "code": "GLOBAL_MINUS_CAMPAIGN_MISSING",
        "id": int(campaign_id),
        "campaign_id": int(campaign_id),
        "name": camp_name,
        "tp_code": tp_code,
        "missing_words": missing,
        "transport": "grid",
        "detail": (f"кампания без глоб.минус-слов на уровне кампании: нет {missing} "
                   f"(ни inline, ни shared-set) — добить UpdateCampaigns"),
    }]


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
            issues += _audit_callouts(g, login, int(campaign_id), camp_name, tp_code)    # #8 уточнения
            # D6: глоб.минус-слова на уровне КАМПАНИИ (tp2/tp4/tp5) — 1.4 DoD
            issues += _audit_global_minus_campaign(g, login, int(campaign_id), camp_name, tp_code)
            if tp == 5:   # tp5 = фид-кампания: листинги + фильтры + места показа
                issues += _audit_generic_fallback_group(groups, int(campaign_id), camp_name)
                issues += _audit_no_listing(rc, login, int(campaign_id), camp_name)
                issues += _audit_product_feed_filters(rc, login, int(campaign_id), camp_name, groups)
                issues += _audit_placements(rc, login, int(campaign_id), camp_name)
                # tp5 (ЕПК) несёт TextAd (GdAdaptiveTextAd) — комбинированный «Т+Л+ТОВ» (DoD §3.5, 7
                # заголовков), поэтому brand-first + длина заголовков применимы как на tp1/tp2/tp4
                # (D2 2026-07-09: ветка tp5 их не вызывала → ложное «чисто»). groups=None гасит
                # VIDEO_MISSING/IMAGE_MISSING (на поиске не нужны); _audit_tp1_adaptive сам режет
                # заголовки только у GdAdaptiveTextAd → ShoppingAd/ListingAd (без titles) не трогаются
                # (tp5-специфика ShoppingAd сохранена).
                issues += [i for i in _audit_tp1_adaptive(rc, login, int(campaign_id), camp_name, groups=None)
                           if i.get("code") in ("BUTTON_MISSING", "SHORT_TITLES")]
                issues += _audit_brand_not_first(rc, login, int(campaign_id), camp_name, groups)
            elif tp in (2, 4):
                # tp2/Поиск и tp4/Поиск+Динамика используют те же GdAdaptiveTextAd, что и tp1 —
                # кнопка «Получить скидку» (BUTTON_MISSING) + длина заголовков (SHORT_TITLES) там
                # так же применимы. groups=None гасит VIDEO_MISSING (agid_to_ct пуст без groups);
                # IMAGE_MISSING отфильтрован — на поиске текстовые объявления без картинок by design,
                # добивать images_repair не нужно. SHORT_TITLES ВКЛЮЧЁН (2026-07-09, задача A): длина
                # заголовка важна и на поиске; фикс — LLM-регенерация (не суффикс).
                issues += [i for i in _audit_tp1_adaptive(rc, login, int(campaign_id), camp_name, groups=None)
                           if i.get("code") in ("BUTTON_MISSING", "SHORT_TITLES")]
                # brand-first (марка до первой точки) применима к поисковым адаптивным tp2/tp4:
                # тут groups есть → марка группы резолвится, автотаргет матчит по началу заголовка.
                issues += _audit_brand_not_first(rc, login, int(campaign_id), camp_name, groups)
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
        issues += _audit_brand_not_first(rc, login, int(campaign_id), camp_name, groups)
        issues += _audit_no_listing(rc, login, int(campaign_id), camp_name)
        issues += _audit_callouts(g, login, int(campaign_id), camp_name, tp_code)    # #8 уточнения
        # D5: брендовый ct без картинок в слепке (report-only) — берёт только общий пул
        issues += _audit_ct_slepok_images(int(campaign_id), camp_name, groups, slepok, site_type)
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
            # D3-UAC: видео-марки (BAIC/Belgee/Haval/Москвич) без видео → UAC_VIDEO_MISSING
            issues += _audit_uac_video_missing(login, int(campaign_id), camp_name, agency, detail=detail)
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
    all_issues += _audit_group_count_vs_slepok(grid, tool, slepok, site_type)   # D10 report-only
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


# ── fixer (FOREIGN_MODEL_KEYWORDS): удалить чужемодельные ключи v5 keywords.delete ──
def fix_foreign_model_keywords(login: str, ctx: dict, issues: list[dict]) -> dict:
    """Delete keywords containing foreign-model discriminating tokens from MODEL groups.

    FOREIGN_MODEL_KEYWORDS issue carries ``foreign_kws`` (list of phrase strings) and
    ``adgroup_id`` / ``campaign_id``. Механика:
    1. v5 keywords.get по кампаниям → phrase→id маппинг.
    2. Сопоставить foreign_kws из issue → keyword_id.
    3. v5 keywords.delete этих id.
    Не трогает собственные ключи группы. Баллов не тратит (keywords.delete бесплатен)."""
    import requests as _req
    fm_issues = [it for it in (issues or []) if it.get("code") == "FOREIGN_MODEL_KEYWORDS"]
    if not fm_issues:
        return {"ok": True, "note": "нет FOREIGN_MODEL_KEYWORDS", "deleted": 0}
    # v5 OAuth-токен
    _v5_tok = _DEPS.get("_v5_token_for_login")
    token = _v5_tok(login) if callable(_v5_tok) else None
    if not token:
        return {"ok": False, "error": "нет v5 OAuth-токена для keywords.delete", "deleted": 0}
    _V5 = "https://api.direct.yandex.com/json/v5/"
    _hdrs = {"Authorization": f"Bearer {token}", "Client-Login": login,
             "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8",
             "Use-Operator-Units": "true"}
    # Собрать уникальные campaign_id
    camp_ids = list({int(it["campaign_id"]) for it in fm_issues if it.get("campaign_id")})
    # Прочитать все keyword id+phrase по этим кампаниям
    phrase_to_ids: dict[str, list[int]] = {}  # нормализованная фраза → [keyword_id, ...]
    try:
        for cid in camp_ids:
            r = _req.post(_V5 + "keywords", headers=_hdrs, json={
                "method": "get",
                "params": {"SelectionCriteria": {"CampaignIds": [cid]},
                           "FieldNames": ["Id", "AdGroupId", "Keyword"]},
            }, timeout=60)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                return {"ok": False, "error": f"v5 keywords.get camp {cid}: {data['error']}", "deleted": 0}
            for kw in ((data.get("result") or {}).get("Keywords") or []):
                kid = int(kw.get("Id") or 0)
                phrase = str(kw.get("Keyword") or "").strip().lower()
                if kid > 0 and phrase:
                    phrase_to_ids.setdefault(phrase, []).append(kid)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"v5 keywords.get: {str(e)[:180]}", "deleted": 0}
    # Найти id для чужемодельных фраз
    del_ids: list[int] = []
    results: list[dict] = []
    for it in fm_issues:
        gid = int(it.get("adgroup_id") or 0)
        foreign_phrases = [str(p).strip().lower() for p in (it.get("foreign_kws") or [])]
        found_ids: list[int] = []
        for ph in foreign_phrases:
            found_ids.extend(phrase_to_ids.get(ph, []))
        del_ids.extend(found_ids)
        results.append({"adgroup_id": gid, "model": it.get("model"), "to_delete": len(found_ids),
                        "foreign_phrases_count": len(foreign_phrases)})
    del_ids = list(dict.fromkeys(del_ids))  # дедуп, сохраняя порядок
    if not del_ids:
        return {"ok": True, "note": "keyword_id не найдены (возможно уже удалены)", "deleted": 0,
                "results": results}
    # v5 keywords.delete
    try:
        deleted = 0
        for i in range(0, len(del_ids), 10000):
            chunk = del_ids[i:i + 10000]
            r = _req.post(_V5 + "keywords", headers=_hdrs, json={
                "method": "delete",
                "params": {"SelectionCriteria": {"Ids": chunk}},
            }, timeout=60)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                return {"ok": False, "error": f"v5 keywords.delete: {data['error']}",
                        "deleted": deleted, "results": results}
            deleted += len((data.get("result") or {}).get("DeleteResults") or chunk)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"v5 keywords.delete: {str(e)[:180]}",
                "deleted": 0, "results": results}
    return {"ok": True, "deleted": deleted, "results": results,
            "adgroups_fixed": len({r["adgroup_id"] for r in results if r["to_delete"] > 0})}


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


# ── helper: gen-контекст для LLM-регенерации (agent/site_type/city/domain/provider) ──
def _regen_ctx(login: str, ctx: dict) -> tuple:
    """(agent_dict|None, gen_ctx, provider) для content_quality-регенерации.

    Восстанавливает слепок-агента и параметры сайта (тип/город/домен) из ``ctx.body`` +
    ``_account_ctx``; провайдер LLM — из ``body.llm_provider`` (дефолт openrouter, как в
    боевой генерации). Если слепок не резолвится (agent=None) — вызывающая сторона обязана
    трактовать регенерацию как невозможную (hard-fail без тихого фолбэка)."""
    from . import ai_agents as A
    body = (ctx or {}).get("body") or {}
    slepok = (body.get("agent") or "").strip()
    acc = {}
    _account_ctx = _DEPS.get("_account_ctx")
    try:
        acc = (_account_ctx(login) if _account_ctx else None) or {}
    except Exception:  # noqa: BLE001
        acc = {}
    agent = None
    try:
        agent = A.get_agent(slepok) if slepok else None
    except Exception:  # noqa: BLE001
        agent = None
    site_type = (body.get("site_type") or acc.get("site_type") or "").strip()
    gen_ctx = {
        "site_type": site_type,
        "city": acc.get("city") or "",
        "domain": (acc.get("domain") or "").strip(),
    }
    provider = str(body.get("llm_provider") or "openrouter").strip().lower()
    return agent, gen_ctx, provider


# ── fixer (SHORT_TITLES): РЕГЕНЕРАЦИЯ коротких заголовков через LLM (тот же _llm_pair_for) ──
def fix_short_titles(login: str, ctx: dict, issues: list[dict]) -> dict:
    """Перегенерировать короткие заголовки через LLM (content_quality.regen_titles) —
    единый паттерн «регенерация → проверка длины (≥48) → повтор до 4 попыток → HARD-FAIL».

    ⚠️ БОЛЬШЕ НЕ добиваем суффиксами (``extend_title_to_max``): если после всех попыток
    заголовки остались короткими — заводим терминальный ``SHORT_TITLES_UNFIXABLE``
    (severity error, fixable:False) вместо тихого прохождения. Две транспортные ветки:
    grid (tp1/tp2/tp4 адаптивные, RMW UpdateAdaptiveTextAds) и UAC (tp6/tp7, cookie PATCH).
    Никаких пересозданий и баллов API — только кука."""
    short_issues = [it for it in (issues or []) if it.get("code") == "SHORT_TITLES"]
    if not short_issues:
        return {"ok": True, "note": "нет SHORT_TITLES", "campaigns_fixed": 0}
    from . import content_quality as CQ  # Flask-free
    agent, gen_ctx, provider = _regen_ctx(login, ctx)
    _min_len = _TITLE_SHORT_LEN + 1   # валидный минимум = 48 (audit флагает ≤47)
    fixed, extended_total = [], 0
    errors: list[str] = []
    terminal: list[dict] = []   # SHORT_TITLES_UNFIXABLE — регенерация не дала валидную длину

    if agent is None:
        # Слепок не восстановлен → LLM-регенерация невозможна. НЕ падаем суффиксом — hard-fail.
        for it in short_issues:
            cid = int(it.get("campaign_id") or 0)
            terminal.append({
                "code": "SHORT_TITLES_UNFIXABLE", "id": cid, "campaign_id": cid,
                "name": str(it.get("name") or ""), "severity": "error", "fixable": False,
                "detail": "короткие заголовки: слепок не восстановлен → LLM-регенерация невозможна",
            })
        return {"ok": False, "campaigns_fixed": 0, "titles_extended": 0,
                "campaigns": [], "errors": ["слепок-агент не восстановлен для регенерации"],
                "terminal": terminal}

    # ── grid-ветка (tp1/tp2/tp4 адаптивные): регенерация → RMW UpdateAdaptiveTextAds ──
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
                unfixable_ads: list[int] = []
                for aid, cur in cur_map.items():
                    titles = [str(t) for t in (cur.get("titles") or [])]
                    if not any(len(t) <= _TITLE_SHORT_LEN for t in titles):
                        continue   # у этого объявления коротких нет — не трогаем
                    res = CQ.regen_titles(agent, gen_ctx, brand="", old_titles=titles,
                                          n=len(titles), min_len=_min_len,
                                          need_brand_first=False, provider=provider)
                    if res.get("ok") and res.get("value"):
                        new_titles = list(res["value"])
                        items.append({"id": aid, "titles": new_titles})
                        changed += sum(1 for a, b in zip(titles, new_titles) if a != b)
                    else:
                        unfixable_ads.append(int(aid))
                if items:
                    csf._grid_update_adaptive_ads(login, items, campaign_ids=[cid])
                after = [i for i in _audit_tp1_adaptive(rcl, login, cid, str(it.get("name") or ""))
                         if i.get("code") == "SHORT_TITLES"]
                still = len((after[0].get("ad_ids") or [])) if after else 0
                extended_total += changed
                fixed.append({"campaign_id": cid, "transport": "grid", "regenerated": changed,
                              "ads_updated": len(items), "still_short_ads": still,
                              "unfixable_ads": unfixable_ads})
                if unfixable_ads or still:
                    terminal.append({
                        "code": "SHORT_TITLES_UNFIXABLE", "id": cid, "campaign_id": cid,
                        "name": str(it.get("name") or ""), "transport": "grid",
                        "ad_ids": unfixable_ads, "still_short_ads": still,
                        "severity": "error", "fixable": False,
                        "detail": (f"grid: регенерация не дала заголовки ≥{_min_len} симв "
                                   f"после {CQ._REGEN_MAX_ATTEMPTS} попыток "
                                   f"(unfixable={len(unfixable_ads)}, still_short={still})"),
                    })
            except Exception as e:  # noqa: BLE001
                errors.append(f"кампания {cid} (grid): {str(e)[:180]}")

    # ── UAC-ветка (tp6/tp7 Мастера): cookie PATCH ────────────────────────────────
    uac_issues = [it for it in short_issues if it.get("transport") != "grid"]
    if not uac_issues:
        return {"ok": not errors and not terminal, "campaigns_fixed": len(fixed),
                "titles_extended": extended_total, "campaigns": fixed, "errors": errors,
                "terminal": terminal}
    # PATCH-хелпер контент-редактора (лениво: routes_content_editor тянет flask на импорте)
    from .routes_content_editor import _uac_patch_campaign_texts, _unwrap_uac_response
    agency = ((ctx or {}).get("agency") or "").strip() or None
    try:
        client = ur.UacReadClient(login, agency=agency).client
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"uac client: {str(e)[:160]}", "campaigns_fixed": len(fixed),
                "titles_extended": extended_total, "campaigns": fixed, "errors": errors,
                "terminal": terminal}

    def _set_uac_item_text(item, new_text):
        """Заменить видимый текст UAC-элемента, сохранив структуру (dict-метаданные)."""
        if isinstance(item, dict):
            d = dict(item)
            for key in ("text", "title", "value", "body", "name"):
                if str(d.get(key) or "").strip():
                    d[key] = new_text
                    return d
            d["text"] = new_text
            return d
        return new_text

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
            old_titles = [_uac_item_text(item) for item in items]
            # РЕГЕНЕРАЦИЯ всего набора заголовков (n=len) — единый паттерн, без суффиксов.
            res = CQ.regen_titles(agent, gen_ctx, brand="", old_titles=old_titles,
                                  n=len(items), min_len=_min_len,
                                  need_brand_first=False, provider=provider)
            if not (res.get("ok") and res.get("value")):
                terminal.append({
                    "code": "SHORT_TITLES_UNFIXABLE", "id": cid, "campaign_id": cid,
                    "name": str(it.get("name") or ""), "severity": "error", "fixable": False,
                    "detail": (f"UAC: регенерация не дала {len(items)} заголовков ≥{_min_len} симв "
                               f"после {CQ._REGEN_MAX_ATTEMPTS} попыток: {res.get('reason') or ''}"),
                })
                continue
            new_titles = list(res["value"])
            new_items = [_set_uac_item_text(item, new_titles[i]) for i, item in enumerate(items)]
            changed = sum(1 for o, n in zip(old_titles, new_titles) if o != n)
            _uac_patch_campaign_texts(client, cid, field_key, new_items)
            # read-back: сколько коротких осталось
            after = _unwrap_uac_response(client._request("GET", f"/campaign/{cid}",
                                                         step=f"uac-readback:{cid}"))
            _, after_items = _uac_titles_field(after)
            still_short = sum(1 for x in (_uac_item_text(a) for a in after_items)
                              if x and len(x) <= _TITLE_SHORT_LEN)
            extended_total += changed
            fixed.append({"campaign_id": cid, "regenerated": changed, "still_short": still_short})
            if still_short:
                terminal.append({
                    "code": "SHORT_TITLES_UNFIXABLE", "id": cid, "campaign_id": cid,
                    "name": str(it.get("name") or ""), "severity": "error", "fixable": False,
                    "detail": f"UAC: после регенерации осталось {still_short} коротких заголовков",
                })
        except Exception as e:  # noqa: BLE001
            errors.append(f"кампания {cid}: {str(e)[:180]}")
    return {"ok": not errors and not terminal, "campaigns_fixed": len(fixed),
            "titles_extended": extended_total, "campaigns": fixed, "errors": errors,
            "terminal": terminal}


# ── fixer (CONTENT_TEXTS_LOW): добить тексты объявления до ≥3 через LLM-регенерацию ──
def fix_texts_low(login: str, ctx: dict, issues: list[dict]) -> dict:
    """Догенерировать тексты (bodies) адаптивных объявлений с <3 текстами до ``_TEXTS_MIN`` —
    LLM-регенерация (content_quality._regen_texts, тот же _llm_pair_for), затем Grid RMW
    UpdateAdaptiveTextAds (bodies; видео/цена сохраняются через typedCreatives). Только grid-
    транспорт (tp1/tp2/tp4/tp5 адаптивные); UAC (tp6/tp7 texts<3) чинит recreate (UAC_TEXTS_MISSING).
    Баллов не тратит (кука). Read-back — повторный детект; остаток доберут следующие циклы."""
    tl_issues = [it for it in (issues or []) if it.get("code") == "CONTENT_TEXTS_LOW"
                 and it.get("transport") == "grid"]
    if not tl_issues:
        return {"ok": True, "note": "нет CONTENT_TEXTS_LOW (grid)", "campaigns_fixed": 0}
    from . import content_quality as CQ  # Flask-free
    from . import create_set_feeds as csf
    agent, gen_ctx, provider = _regen_ctx(login, ctx)
    if agent is None:
        return {"ok": False, "campaigns_fixed": 0, "texts_added": 0, "campaigns": [],
                "errors": ["слепок-агент не восстановлен для регенерации текстов"]}
    gcl = gf.GridClient(login)
    rcl = gr.GridReadClient(login)
    fixed, errors, added_total = [], [], 0
    for it in tl_issues:
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
                bodies = [str(b) for b in (cur.get("bodies") or []) if str(b or "").strip()]
                if len(bodies) >= _TEXTS_MIN:
                    continue   # уже добито (edit-view лаг/параллельный фикс) — не трогаем
                res = CQ._regen_texts(agent, gen_ctx, brand="", old_texts=bodies,
                                      n=_TEXTS_MIN, provider=provider)
                if res.get("ok") and res.get("value"):
                    new_bodies = [str(b) for b in res["value"] if str(b or "").strip()][:max(_TEXTS_MIN, len(bodies))]
                    if len(new_bodies) >= _TEXTS_MIN:
                        items.append({"id": aid, "bodies": new_bodies})
                        changed += 1
            if items:
                csf._grid_update_adaptive_ads(login, items, campaign_ids=[cid])
            after = [i for i in _audit_tp1_adaptive(rcl, login, cid, str(it.get("name") or ""))
                     if i.get("code") == "CONTENT_TEXTS_LOW"]
            still = len((after[0].get("ad_ids") or [])) if after else 0
            added_total += changed
            fixed.append({"campaign_id": cid, "ads_updated": len(items),
                          "regenerated": changed, "still_low_ads": still})
            if still:
                errors.append(f"кампания {cid}: осталось {still} объявл. с <{_TEXTS_MIN} текстами")
        except Exception as e:  # noqa: BLE001
            errors.append(f"кампания {cid}: {str(e)[:180]}")
    return {"ok": not errors, "campaigns_fixed": len(fixed), "texts_added": added_total,
            "campaigns": fixed, "errors": errors}


# ── fixer (GLOBAL_MINUS_CAMPAIGN_MISSING): добить глоб.минус на кампанию (in-place, без баллов) ──
def fix_global_minus_campaign(login: str, ctx: dict, issues: list[dict]) -> dict:
    """Добавить глобальные минус-слова на уровень кампании inline через Grid UpdateCampaigns
    (`set_campaign_minus_keywords`, БЕЗ баллов, РК DRAFT, идемпотентно). D6 2026-07-09.
    Слова берём из `_enabled_minus_words` (источник истины), не из issue (чтобы не рассинхрониться)."""
    gm_issues = [it for it in (issues or []) if it.get("code") == "GLOBAL_MINUS_CAMPAIGN_MISSING"]
    if not gm_issues:
        return {"ok": True, "note": "нет GLOBAL_MINUS_CAMPAIGN_MISSING", "campaigns_fixed": 0}
    _enabled = _DEPS.get("_enabled_minus_words")
    try:
        words = [str(w).strip() for w in (_enabled() or [])] if callable(_enabled) else []
    except Exception:  # noqa: BLE001
        words = []
    words = [w for w in words if w]
    if not words:
        return {"ok": True, "note": "_enabled_minus_words пуст — нечего добавлять", "campaigns_fixed": 0}
    gcl = gf.GridClient(login)
    fixed, errors = [], []
    for it in gm_issues:
        try:
            cid = int(it.get("campaign_id") or 0)
        except (TypeError, ValueError):
            continue
        if cid <= 0:
            continue
        try:
            upd = gcl.set_campaign_minus_keywords([cid], words) or []
            fixed.append({"campaign_id": cid, "updated": len(upd), "words": words})
        except Exception as e:  # noqa: BLE001
            errors.append(f"кампания {cid}: {str(e)[:180]}")
    return {"ok": not errors, "campaigns_fixed": len(fixed), "campaigns": fixed, "errors": errors}


# ── fixer (BRAND_NOT_FIRST): регенерация заголовков с маркой в начале (brand-first) ──
def fix_brand_not_first(login: str, ctx: dict, issues: list[dict]) -> dict:
    """Перегенерировать заголовки объявлений, где марка не стоит до первой точки —
    LLM-регенерация с ``need_brand_first=True`` (единый паттерн: регенерация → проверка →
    повтор до 4 попыток → HARD-FAIL ``BRAND_NOT_FIRST_UNFIXABLE``). Grid RMW, видео/цена
    сохраняются (typedCreatives)."""
    bn_issues = [it for it in (issues or []) if it.get("code") == "BRAND_NOT_FIRST"]
    if not bn_issues:
        return {"ok": True, "note": "нет BRAND_NOT_FIRST", "campaigns_fixed": 0}
    from . import content_quality as CQ  # Flask-free
    from . import create_set_feeds as csf
    agent, gen_ctx, provider = _regen_ctx(login, ctx)
    fixed, errors, terminal = [], [], []
    _min_len = _TITLE_SHORT_LEN + 1
    if agent is None:
        for it in bn_issues:
            cid = int(it.get("campaign_id") or 0)
            terminal.append({
                "code": "BRAND_NOT_FIRST_UNFIXABLE", "id": cid, "campaign_id": cid,
                "name": str(it.get("name") or ""), "severity": "error", "fixable": False,
                "detail": "brand-first: слепок не восстановлен → LLM-регенерация невозможна",
            })
        return {"ok": False, "campaigns_fixed": 0, "campaigns": [],
                "errors": ["слепок-агент не восстановлен для регенерации"], "terminal": terminal}
    gcl = gf.GridClient(login)
    rcl = gr.GridReadClient(login)
    for it in bn_issues:
        try:
            cid = int(it.get("campaign_id") or 0)
        except (TypeError, ValueError):
            continue
        ads = it.get("ads") or []
        brand_by_ad = {int(a["ad_id"]): str(a.get("brand") or "")
                       for a in ads if str(a.get("ad_id") or "").strip()}
        ad_ids = list(brand_by_ad.keys())
        if cid <= 0 or not ad_ids:
            continue
        try:
            cur_map = gcl.adaptive_ads_for_update([cid], ad_ids)
            items, changed, unfixable = [], 0, []
            for aid, cur in cur_map.items():
                brand = brand_by_ad.get(int(aid)) or ""
                titles = [str(t) for t in (cur.get("titles") or [])]
                if not titles:
                    continue
                res = CQ.regen_titles(agent, gen_ctx, brand=brand, old_titles=titles,
                                      n=len(titles), min_len=_min_len,
                                      need_brand_first=True, provider=provider)
                if res.get("ok") and res.get("value"):
                    new_titles = list(res["value"])
                    items.append({"id": aid, "titles": new_titles})
                    changed += sum(1 for a, b in zip(titles, new_titles) if a != b)
                else:
                    unfixable.append(int(aid))
            if items:
                csf._grid_update_adaptive_ads(login, items, campaign_ids=[cid])
            # read-back: перечитать группы и пере-аудитить brand-first
            try:
                groups = gcl.groups_for_edit(cid)
            except Exception:  # noqa: BLE001
                groups = []
            after = _audit_brand_not_first(rcl, login, cid, str(it.get("name") or ""), groups)
            still = len((after[0].get("ads") or [])) if after else 0
            fixed.append({"campaign_id": cid, "regenerated": changed, "ads_updated": len(items),
                          "still_bad_ads": still, "unfixable_ads": unfixable})
            if unfixable or still:
                terminal.append({
                    "code": "BRAND_NOT_FIRST_UNFIXABLE", "id": cid, "campaign_id": cid,
                    "name": str(it.get("name") or ""), "transport": "grid",
                    "ad_ids": unfixable, "still_bad_ads": still,
                    "severity": "error", "fixable": False,
                    "detail": (f"регенерация brand-first не удалась после {CQ._REGEN_MAX_ATTEMPTS} "
                               f"попыток (unfixable={len(unfixable)}, still_bad={still})"),
                })
        except Exception as e:  # noqa: BLE001
            errors.append(f"кампания {cid}: {str(e)[:180]}")
    return {"ok": not errors and not terminal, "campaigns_fixed": len(fixed),
            "campaigns": fixed, "errors": errors, "terminal": terminal}


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
                meta = [{"id": a["ad_id"], "meta": {"ct": a["ct"], "brand": a.get("brand") or ""}} for a in remaining]
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
    # Агрегируем still_missing_total: если > 0 → логируем явно и ставим requeue_needed.
    # Это отличает «не всё загружено» от «всё ок»; следующий цикл spec-аудита доберёт.
    still_missing_total = sum(int(c.get("still_missing") or 0) for c in fixed)
    if still_missing_total > 0:
        logging.warning(
            "fix_video_missing: login=%s still_missing_total=%d по %d кампаниям "
            "— VIDEO_MISSING остаток, requeue_needed",
            (ctx or {}).get("login") or login, still_missing_total, len(fixed),
        )
    result = {"ok": not errors, "campaigns_fixed": len(fixed), "campaigns": fixed,
              "errors": errors, "still_missing_total": still_missing_total}
    if still_missing_total > 0:
        result["requeue_needed"] = True
    return result


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
    _model_field_cache: dict[str, str | None] = {}
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
                    if fid not in _model_field_cache:
                        try:
                            _model_field_cache[fid] = _csf._resolve_feed_field(
                                login, int(fid), "model")
                        except Exception:  # noqa: BLE001
                            _model_field_cache[fid] = None
                    # ЛИСТИНГИ фильтруются ТОЛЬКО по имени каталога: поле бренда для них
                    # UNAVAILABLE_FIELD (live 03.07, все 6 кампаний) — как в set_listing_name_filters.
                    bf = "name" if listing_flag else _brand_field_cache[fid]
                    mf = _model_field_cache[fid] or "model"
                    if not bf:
                        skipped_no_field += 1
                        continue          # у фида нет поля бренда — пропускаем (не ошибка)
                    items.append({
                        "id": a["ad_id"], "feed_id": fid,
                        # Канон create-пути (с 2026-07-06): ОДНО условие на поле со всеми
                        # значениями массивом — NOT_CONTAINS_ALL [A,B] = «не содержит ни одной»
                        # (значения внутри условия объединяются ИЛИ, дока yard.yandex.ru
                        # filtry-v-fidah; лимит «до 22 условий, объединённых И»). Прежний вывод
                        # ревью 03.07 «другая семантика» неверен; по-условию-на-значение при
                        # 86 минусах ломало Grid-лимит 30 условий и UAC-дедуп.
                        # Листинги: поле 'model' физически отсутствует в Grid feedFilter для ListingAd
                        # (UNAVAILABLE_FIELD live 2026-07-07) — фильтруем ТОЛЬКО по 'name'.
                        # ShoppingAd: оба поля (brand + model) передаются без изменений.
                        "conditions": _csf._minus_marks_grid_conditions(
                            brand_field=bf,
                            model_field=None if listing_flag else mf,
                        ),
                        "bodies": a.get("bodies") or [],
                    })
                if items:
                    attempted += len(items)
                    try:
                        updated += gcl.set_product_feed_filters(items, listing=listing_flag)
                    except gf.GridFinalizeError as _ffe:
                        if "UNKNOWN_FIELD" in str(_ffe) or "UNAVAILABLE_FIELD" in str(_ffe):
                            # _resolve_feed_field при сбое probe отдаёт фолбэк 'vendor' —
                            # у фида такого поля может не быть: это skip, не ошибка цикла.
                            # UNAVAILABLE_FIELD: Grid сигнализирует что поле недоступно для
                            # данного типа объявления (напр. 'model' на ListingAd) — тоже skip.
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


# ── fixer (LISTING_POSITIVE_FILTER_MISSING): позитивный name-фильтр на ListingAd ──
def fix_listing_positive_filter(login: str, ctx: dict, issues: list[dict]) -> dict:
    """Поставить позитивный name CONTAINS_ANY [brand] на GdListingAd в «Марки»-группах.

    Переиспользует _listing_name_value (create_set_feeds:1189) + set_listing_name_filters
    (grid_finalize:1348) — те же функции что и create-путь (fix-2, HAR36).
    Пропускает записи без brand (парсинг из adgroup_name мог не найти марку)."""
    from . import create_set_feeds as _csf
    lp_issues = [it for it in (issues or []) if it.get("code") == "LISTING_POSITIVE_FILTER_MISSING"]
    if not lp_issues:
        return {"ok": True, "note": "нет LISTING_POSITIVE_FILTER_MISSING", "campaigns_fixed": 0}
    gcl = gf.GridClient(login)
    fixed, errors = [], []
    for it in lp_issues:
        try:
            cid = int(it.get("campaign_id") or 0)
        except (TypeError, ValueError):
            continue
        ads = [a for a in (it.get("ads") or [])
               if a.get("ad_id") and a.get("feed_id") and a.get("brand")]
        if cid <= 0 or not ads:
            continue
        try:
            items = []
            skipped_no_brand = 0
            for a in ads:
                brand = (a.get("brand") or "").strip()
                name_val = _csf._listing_name_value(brand, "Марки") if brand else None
                if not name_val:
                    skipped_no_brand += 1
                    continue
                items.append({
                    "id": a["ad_id"],
                    "feed_id": str(a["feed_id"]),
                    "value": name_val,
                    "bodies": list(a.get("bodies") or []),
                })
            updated = gcl.set_listing_name_filters(items) if items else 0
            fixed.append({"campaign_id": cid, "updated": updated,
                          "skipped_no_brand": skipped_no_brand})
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


# ── fixer (GENERIC_FALLBACK_GROUP): удалить DRAFT-пустышку + deferred токеном ───
def fix_generic_fallback_group(login: str, ctx: dict, issues: list[dict]) -> dict:
    """Удалить DRAFT tp5 с generic ct0000-группой и поставить deferred с _resume_via_token=True.

    Пересоздание пойдёт ТОКЕНОМ (с сегментными бренд-группами из M3-пака) на сброс баллов.

    Порядок: проверить DRAFT → dedup deferred → delete_campaigns(cookie) → _deferred_save.

    - Только DRAFT-кампании: live-проверка через _grid_list_campaigns(login, only_draft=True).
    - Не-DRAFT → skip с reason (удаление боевых заблокировано).
    - Защита от дублей: если по (login, item name) уже есть waiting/resumed deferred → skip.
    - item матчится по it["name"] == campaign_name (точный == из body["items"]).
    """
    gfb_issues = [it for it in (issues or []) if it.get("code") == "GENERIC_FALLBACK_GROUP"]
    if not gfb_issues:
        return {"ok": True, "note": "нет GENERIC_FALLBACK_GROUP", "campaigns_fixed": 0}

    _grid_list = _DEPS.get("_grid_list_campaigns")
    _def_save = _DEPS.get("_deferred_save")
    _units_reset = _DEPS.get("_next_units_reset_utc")
    _conn_rw = _DEPS.get("_victory_conn_rw")

    body = (ctx or {}).get("body") or {}
    agency = ((ctx or {}).get("agency") or body.get("agency") or "").strip()
    job_id = (ctx or {}).get("job_id") or body.get("_job_id")
    items_plan: list[dict] = list(body.get("items") or [])

    # --- Собрать live DRAFT id-шники ---
    draft_ids: set[int] = set()
    try:
        if _grid_list:
            for c in (_grid_list(login, only_draft=True) or []):
                cid = c.get("id")
                if cid:
                    draft_ids.add(int(cid))
    except Exception as _e:  # noqa: BLE001
        return {"ok": False, "error": f"grid_list DRAFT: {str(_e)[:160]}",
                "campaigns_fixed": 0, "deferred_ids": [], "skipped": [], "errors": []}

    from . import grid_create as _gc_mod  # Flask-free, лениво

    fixed: list[dict] = []
    deferred_ids: list[str] = []
    skipped: list[dict] = []
    errors: list[str] = []

    for issue in gfb_issues:
        try:
            cid = int(issue.get("campaign_id") or 0)
        except (TypeError, ValueError):
            continue
        camp_name = (issue.get("name") or "").strip()
        if cid <= 0:
            continue

        # 1) Только DRAFT — удаление боевых заблокировано
        if cid not in draft_ids:
            skipped.append({"campaign_id": cid, "name": camp_name,
                            "reason": "не DRAFT — удаление заблокировано"})
            continue

        # 2) Найти item в плане по имени кампании
        item_match = next(
            (x for x in items_plan if (x.get("name") or "").strip() == camp_name), None)
        if item_match is None:
            skipped.append({"campaign_id": cid, "name": camp_name,
                            "reason": "item не найден в body['items'] — план потерян"})
            continue

        # 2b) ЗАЩИТА от ложного детекта на edit-view lag (живой кейс 2026-07-06: аудит сразу
        #     после создания видел «1 группу» у ПОЛНОЦЕННОЙ tp5 (35 групп/3609 ключей) и фиксер
        #     УДАЛЯЛ её). Пустышка = НЕТ живых ключей; ключи читаем через showConditions
        #     (_show_condition_kw_counts — надёжный источник, НЕ edit-view). Ключи есть →
        #     кампания живая, детект ложный, пропускаем.
        try:
            _kw_map = gr.GridReadClient(login)._show_condition_kw_counts([cid]) or {}
            _kw_live = int(_kw_map.get(int(cid)) or _kw_map.get(cid) or 0)
        except Exception as _e:  # noqa: BLE001
            errors.append(f"кампания {cid}: guard-проверка живых ключей упала "
                          f"({str(_e)[:120]}) — фикс пропущен (безопасность)")
            continue
        if _kw_live > 0:
            skipped.append({"campaign_id": cid, "name": camp_name,
                            "reason": f"живые ключи ({_kw_live}) — НЕ пустышка (edit-view lag), пропуск"})
            continue

        # 3) Защита от дублей deferred
        if _conn_rw:
            try:
                _conn = _conn_rw()
                try:
                    _cur = _conn.cursor()
                    _cur.execute(
                        "SELECT id FROM public.direct_deferred_creates "
                        "WHERE login=%s AND status IN ('waiting','resumed') "
                        "AND body->'items' @> %s::jsonb LIMIT 1",
                        (login, json.dumps([{"name": camp_name}])))
                    _row = _cur.fetchone()
                finally:
                    _conn.close()
                if _row:
                    skipped.append({"campaign_id": cid, "name": camp_name,
                                    "reason": f"deferred уже есть ({_row[0]}) — повтор не нужен"})
                    continue
            except Exception as _e:  # noqa: BLE001
                errors.append(f"кампания {cid}: dedup-check: {str(_e)[:160]}")
                continue

        # 4) СНАЧАЛА deferred токеном (ревью 06.07: если ставить его ПОСЛЕ удаления, did=None
        #    от _deferred_save — он глотает ошибки БД — означал «черновик удалён, докрутки нет,
        #    ok:True» = молчаливая потеря пункта). Деферред без удаления безопасен: дубля не
        #    будет — RESUME-SKIP пропустит живую кампанию, а generic-пустышку удалим ниже.
        if not (_def_save and _units_reset):
            skipped.append({"campaign_id": cid, "name": camp_name,
                            "reason": "deferred_save/_next_units_reset_utc не сконфигурированы"})
            continue
        did: str | None = None
        try:
            _def_body = dict(body)
            _def_body["_resume_via_token"] = True   # резюм пойдёт ТОКЕНОМ, не по куке
            # иначе резюм опять форсит куку → тот же NO_BRAND_SEGMENTS по кругу (ревью 06.07;
            # парный фикс — create_set_gallery.py)
            _def_body.pop("via_cookie", None)
            # Семён 2026-07-07 (никаких ночных отложек): «баллы первичны» — если баллы ЖИВЫ,
            # добиваем ТОКЕНОМ СРАЗУ (resume_at=None → now(), демон ~2 мин). Реальный 152 (баллы
            # исчерпаны) — только тогда ждём сброс (физическая невозможность, не расписание).
            _units_alive = _DEPS.get("_units_alive_for_login")
            _alive = _units_alive(login, agency) if _units_alive else None
            _resume_at = None if _alive else _units_reset().isoformat()
            did = _def_save(login, agency, _def_body, [item_match], job_id,
                            resume_count=int(_def_body.get("_resume_count") or 0),
                            resume_at=_resume_at)
        except Exception as _e:  # noqa: BLE001
            errors.append(f"кампания {cid}: deferred_save: {str(_e)[:180]}")
            continue
        if not did:
            errors.append(f"кампания {cid}: deferred_save вернул None (сбой БД?) — "
                          "удаление НЕ выполняем, пункт не потерян")
            continue
        deferred_ids.append(did)

        # 5) Удалить DRAFT-пустышку по куке (без баллов v5). Свежая пере-проверка DRAFT
        #    НЕПОСРЕДСТВЕННО перед delete (как в create_set_repairing:463-478): за время
        #    dedup-check/предыдущих итераций кампанию могли опубликовать — боевые не трогаем.
        try:
            _draft_now = {int(c["id"]) for c in (_grid_list(login, only_draft=True) or [])
                          if c.get("id")}
        except Exception as _e:  # noqa: BLE001
            errors.append(f"кампания {cid}: повторная DRAFT-проверка упала: {str(_e)[:160]} — "
                          f"удаление пропущено (deferred {did} поставлен)")
            continue
        if cid not in _draft_now:
            skipped.append({"campaign_id": cid, "name": camp_name, "deferred_id": did,
                            "reason": "при повторной проверке не DRAFT — удаление заблокировано"})
            continue
        try:
            _gc_cl = _gc_mod.GridCreateClient(login)
            _del_res = _gc_cl.delete_campaigns([cid])
        except Exception as _e:  # noqa: BLE001
            errors.append(f"кампания {cid}: delete_campaigns: {str(_e)[:180]} "
                          f"(deferred {did} поставлен — пустышка удалится следующим аудитом)")
            continue
        _del_errs = _del_res.get("errors") or []
        _del_ok = cid in {int(x) for x in (_del_res.get("deleted") or []) if x}
        if _del_errs or not _del_ok:
            errors.append(f"кампания {cid}: Grid не подтвердил удаление "
                          f"({(_del_errs[0] if _del_errs else 'нет в deleted')!r:.160}) "
                          f"(deferred {did} поставлен)")
            continue

        fixed.append({"campaign_id": cid, "name": camp_name, "deferred_id": did})

    return {
        "ok": not errors,
        "campaigns_fixed": len(fixed),
        "deferred_ids": deferred_ids,
        "campaigns": fixed,
        "skipped": skipped,
        "errors": errors,
    }


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
    fix_bf = fix_brand_not_first(args.login, ctx, issues)
    print(f"[fix] BRAND_NOT_FIRST → {fix_bf}")
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

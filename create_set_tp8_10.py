"""Create-set engine for tp8/tp9/tp10 (Посевы) — Grid GdPostCampaign mutations.

Три мутации строго в порядке (SPEC §4.1, HAR porg-gcegsszl 2026-07-21):
  1. AddCampaigns      → campaignId       (login — ТОЛЬКО ?ulogin= URL-param через GridClient._post)
  2. AddPostAdGroups   → adGroupId[] (1-3 группы по числу найденных картинок)
  3. AddPostAds        → adId[]      (1 объявление на группу)

Отличие tp8/tp9/tp10 — ТОЛЬКО platform-флаги в AddCampaigns (SPEC §1a):
  tp8  → telegram:true,  maxMessenger:false
  tp9  → telegram:false, maxMessenger:true
  tp10 → telegram:true,  maxMessenger:true
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import date

# Константы лимитов формата — единый источник правды в ai_agents.py.
# Импортируем здесь; повторно не объявляем, чтобы не разъехались при будущей правке.
from .ai_agents import (  # noqa: E402
    POST_TITLE_MAX, POST_TITLE_WORD_MAX,
    POST_BODY_MAX, POST_BODY_WORD_MAX,
)

# ── Остальные константы формата (SPEC §2.9 / §1b) ────────────────────────────
POST_BUDGET_SUM = 10_000     # недельный бюджет ₽ (SPEC §2.9)
POST_BID = 200               # ставка ₽ (SPEC §2.9)
POST_DEFAULT_BUTTON = "GO_TO_WEBSITE"
POST_IMAGE_LIMIT = 3         # максимум групп на кампанию (SPEC §2.10)
_FMT_CODE = "ct018"          # формат «Посевы/Telegram» в кодере (SPEC §1a)
_AG_CODE = "ag001"           # код группы: POST-группа (не охватная ag011)

# Platform-флаги по коду tp (все прочие ~15 площадок = false, SPEC §2.2)
_TP_PLATFORMS: dict[str, dict[str, bool]] = {
    "tp8":  {"telegram": True,  "maxMessenger": False},
    "tp9":  {"telegram": False, "maxMessenger": True},
    "tp10": {"telegram": True,  "maxMessenger": True},
}

# Полный список platform-полей Grid (из HAR porg-gcegsszl 2026-07-21)
_ALL_PLATFORM_KEYS = [
    "gallery", "network", "search", "telegram", "maxMessenger", "taxi", "pillar",
    "cityBusDisplay", "showcaseScreen", "mediafacade", "supersite", "billboard",
    "cityboard", "cityformat", "organic", "serpGeoWizard", "yandexMaps",
]

# 7 дней × 24 часа × 100% — круглосуточная схема (как в HAR)
_TIME_BOARD_24x7 = [[100] * 24 for _ in range(7)]

# Константы кодера для имени кампании / группы
_AUD_CODE = "aon"            # всегда aon (SPEC §1a)
_N_CODE = "n000"
_G_CODE = "g00"              # суффикс для первой группы; g01/g02 для остальных
_PAY_CODE = "cpc"            # посевы — только CPC
_SQ_CODE = "site"

# Общие (не-брендовые) ct-коды — для fallback'а картинок (SPEC §2.11 п.4)
_COMMON_IMAGE_CTS = (
    "ct0000", "ct0001", "ct0002", "ct0003", "ct0004", "ct0005", "ct0006",
    "ct0007", "ct0008", "ct0009", "ct0010", "ct0013", "ct0014",
    "ct0015", "ct0016", "ct0017", "ct0018",
)

# Site-types для cross-slepok image fallback (без Квиза — у него нет авто-баннеров)
_AUTO_SITE_TYPES_FOR_IMAGES = ("Монобренд", "Мультибренд", "С пробегом")

# UTM-шаблон на уровне кампании (как у tp6/tp7, SPEC §4.1)
_UTM_CAMPAIGN_LEVEL = (
    "utm_source=s:{source}&utm_medium=cpc&utm_campaign={campaign_id}|{campaign_name}"
    "&utm_term={keyword}&utm_content=g:{gbid}|geoname:{region_name}"
    "|geoid:{region_id}|dev:{device_type}|r:{retargeting_id}|cor:{coef_goal_context_id}"
)

_DEPS: dict = {}


def configure(deps: dict) -> None:
    """Bind blueprint dependencies (DI-паттерн, как у create_set_tp1_builders)."""
    _DEPS.clear()
    _DEPS.update(deps)
    globals().update(deps)


# ── GraphQL-запросы (структура из HAR porg-gcegsszl 2026-07-21) ─────────────

# READ: дефолтный email аккаунта для notification.emailSettings.email.
# Grid требует непустой email для GdPostCampaign (в отличие от TEXT_CAMPAIGN).
# campaignTypes=[UNIFIED] — email аккаунтный, не зависит от типа кампании.
_Q_POST_DEFAULT_EMAIL = """
query PostDefaultEmail($login: String!) {
  client(searchBy: {login: $login}) {
    defaultCampaignNotifications(campaignTypes: [UNIFIED]) {
      emailSettings { email }
    }
  }
}
""".strip()

_Q_ADD_CAMPAIGNS = """
mutation AddCampaigns($input: GdAddCampaignsInput!) {
  addCampaigns(input: $input) {
    addedCampaigns { id }
    validationResult { errors { code params path } warnings { code params path } }
  }
}
""".strip()

_Q_ADD_POST_AD_GROUPS = """
mutation AddPostAdGroups($postAddInput: [GdAddTelegramAdGroupItemInput!]!) {
  addPostAdGroups(input: {addItems: $postAddInput}) {
    addedAdGroupItems { adGroupId }
    validationResult { errors { code params path } warnings { code params path } }
  }
}
""".strip()

_Q_ADD_POST_ADS = """
mutation AddPostAds($addPostInput: GdAddPostAdsInput!) {
  addPostAds(input: $addPostInput) {
    addedAds { id }
    validationResult { errors { code params path } warnings { code params path } }
  }
}
""".strip()


# ── Helpers ──────────────────────────────────────────────────────────────────

# Grid-коды возраста для GdPostCampaign bidModifierDemographics (по аналогии с
# create_set_corrections._GRID_AGE_MAP). Grid GdAgeTypeInput не содержит AGE_0_17.
_GRID_AGE_MAP_TP810 = {
    "AGE_0_17":  "_0_17",
    "AGE_18_24": "_18_24",
    "AGE_25_34": "_25_34",
    "AGE_35_44": "_35_44",
    "AGE_45_54": "_45_54",
    "AGE_55":    "_55_",
}


def _dem_adjustments_for_corr(corr: dict | None) -> list:
    """Список demographic-adjustments для Grid AddPostAdGroups.bidModifierDemographics.

    По аналогии с create_set_corrections._correction_bidmodifiers (строки 144-178).
    pct <= 0 пропускается — Grid валидирует percent > 0.
    """
    if not corr:
        return []
    dem_adj: list = []
    for d in corr.get("demographic", []):
        pct = int(d.get("pct") or 0)
        if pct <= 0:
            continue
        adj_entry: dict = {"percent": pct, "id": None}
        if d.get("kind") == "age":
            _grid_age = _GRID_AGE_MAP_TP810.get(d["key"])
            if not _grid_age:
                continue
            adj_entry["age"] = _grid_age
            adj_entry["gender"] = None
        elif d.get("kind") == "gender":
            adj_entry["age"] = None
            adj_entry["gender"] = d["key"]
        else:
            continue
        dem_adj.append(adj_entry)
    return dem_adj


def _ag_code_for_corr(corr: dict | None) -> str:
    """Кодер ag-сегмента для GdPostCampaign/Group.

    ag011 — возрастная/гендерная корректировка есть (24-55+ / пол)
    ag001 — корректировок нет (Все аудитории)
    """
    return "ag011" if _dem_adjustments_for_corr(corr) else "ag001"


def _build_platforms(tp_code: str) -> dict:
    """Все ~17 platform-флагов для AddCampaigns; специфичные для tp устанавливаются в True."""
    base = {k: False for k in _ALL_PLATFORM_KEYS}
    base.update(_TP_PLATFORMS.get(tp_code, {}))
    return base


def _campaign_name(tp_code: str, ct: str, r_code: str, oblast: str,
                   brand_label: str = "Посевы", ag_code: str = _AG_CODE) -> str:
    """Имя кампании по кодеру (SPEC §4.1).

    Формат: tp8_cpc_site_{ct}_aon_n000_{r_code}_ct018_{ag_code}_g00 — {brand_label} Telegram - {oblast}
    brand_label: "Посевы" (мультибренд, дефолт), "Tenet", "Lada", "Haval" (монобренд).
    ag_code: "ag001" (Все) / "ag011" (возрастная/гендерная корректировка).
    """
    _tp_labels = {"tp8": "Telegram", "tp9": "Max", "tp10": "Telegram+Max"}
    label = _tp_labels.get(tp_code, tp_code.upper())
    codes = f"{tp_code}_{_PAY_CODE}_{_SQ_CODE}_{ct}_{_AUD_CODE}_{_N_CODE}_{r_code}_{_FMT_CODE}_{ag_code}_{_G_CODE}"
    return f"{codes} — {brand_label} {label} - {oblast}"


def _group_name(ct: str, r_code: str, idx: int, brand_label: str = "Посевы",
                ag_code: str = _AG_CODE) -> str:
    """Имя группы объявлений по кодеру (SPEC §2.12, live-test §9.1).

    Формат (1:1 с _tp1_group_name):
    {ct}_aon_n000_{r_code}_ct018_{ag_code}_g{idx:02d} — {brand_label} v{idx+1}

    idx=0 → g00/v1, idx=1 → g01/v2, idx=2 → g02/v3 (0-indexed).
    ag_code: "ag001" (Все) / "ag011" (возрастная/гендерная корректировка).
    """
    g_code = f"g{idx:02d}"
    return f"{ct}_{_AUD_CODE}_{_N_CODE}_{r_code}_{_FMT_CODE}_{ag_code}_{g_code} — {brand_label} v{idx+1}"


def _grid_vr_errors(resp_data: dict, mutation_key: str) -> list:
    """Список ошибок из GraphQL-ответа: errors верхнего уровня + validationResult.errors."""
    top_errs = resp_data.get("errors") or []
    mr = (resp_data.get("data") or {}).get(mutation_key) or {}
    vr = mr.get("validationResult") or {}
    return list(top_errs) + list(vr.get("errors") or [])


def _fetch_notification_email(cli, login: str) -> str:
    """Получить дефолтный email аккаунта для notification.emailSettings.email.

    Grid требует непустой email при создании GdPostCampaign (NonNull).
    Один лёгкий READ-запрос до AddCampaigns; результат кэшируется вызывающим
    между тремя tp8/tp9/tp10 через передачу в аргумент.
    → email-строка или '' (при ошибке — вызывающий должен _fail).
    """
    try:
        r = cli._post("PostDefaultEmail", _Q_POST_DEFAULT_EMAIL, {"login": login})
        data = r.json()
        notifs = (
            ((data.get("data") or {}).get("client") or {})
            .get("defaultCampaignNotifications") or []
        )
        for n in notifs:
            email = (n.get("emailSettings") or {}).get("email") or ""
            if email:
                return email
    except Exception:  # noqa: BLE001 — возвращаем '' → _fail в вызывающем
        pass
    return ""


def _posevy_images_for_ct(img_ct: str, limit: int = POST_IMAGE_LIMIT) -> list[str]:
    """Резолвинг картинок для «Посевов» (SPEC §2.11).

    Алгоритм:
      1. Manual-пул _manual_creative_paths(img_ct) — ПЕРВЫЙ (из DI)
      2. Фолбэк: все директологские подпапки _image_store/slepki/*/
         через kp.read_any_slepok_images (БЕЗ фильтра site_type)
      3. Фолбэк: общие ct0000-ct0018 (тот же алгоритм п.1-2)
      4. 0 картинок — НЕ блокируем (API принимает multicards:[], SPEC §2.11 п.5)
    """
    try:
        from . import kontent_pack as kp  # noqa: PLC0415
    except ImportError:
        import kontent_pack as kp  # type: ignore[no-redef]  # noqa: PLC0415

    imgs: list[str] = []
    seen: set[str] = set()

    def _add(paths: list[str]) -> None:
        for p in paths:
            if p and p not in seen:
                seen.add(p)
                imgs.append(p)

    # Step 1: Manual (DI function _manual_creative_paths)
    manual_fn = _DEPS.get("_manual_creative_paths")
    if callable(manual_fn):
        try:
            _add(manual_fn(img_ct) or [])
        except Exception:  # noqa: BLE001
            pass

    if len(imgs) >= limit:
        return imgs[:limit]

    # Step 2: all-slepok fallback across all auto site_types (SPEC: без фильтра site_type)
    for st in _AUTO_SITE_TYPES_FOR_IMAGES:
        for tp in ("tp1", "tp6"):
            try:
                _add(kp.read_any_slepok_images(st, tp, img_ct))
            except Exception:  # noqa: BLE001
                pass
            if len(imgs) >= limit:
                return imgs[:limit]

    if len(imgs) >= limit:
        return imgs[:limit]

    # Step 3: fallback to common ct codes if brand-ct had no images (SPEC §2.11 п.4)
    if img_ct not in _COMMON_IMAGE_CTS:
        for common_ct in _COMMON_IMAGE_CTS:
            if callable(manual_fn):
                try:
                    _add(manual_fn(common_ct) or [])
                except Exception:  # noqa: BLE001
                    pass
            for st in _AUTO_SITE_TYPES_FOR_IMAGES:
                try:
                    _add(kp.read_any_slepok_images(st, "tp1", common_ct))
                except Exception:  # noqa: BLE001
                    pass
            if len(imgs) >= limit:
                return imgs[:limit]

    return imgs[:limit]  # может быть [], не блокируем


def _resolve_img_ct(ct: str) -> str:
    """Определяет img_ct для поиска картинок (как _image_ct_for_content, SPEC §2.11 п.1)."""
    img_ct_fn = _DEPS.get("_image_ct_for_content")
    if callable(img_ct_fn):
        try:
            return img_ct_fn(ct)
        except Exception:  # noqa: BLE001
            pass
    # Fallback inline: общие ct → ct0000
    _common = {
        "ct0000", "ct0001", "ct0002", "ct0003", "ct0004", "ct0005", "ct0006",
        "ct0007", "ct0008", "ct0009", "ct0010", "ct0013", "ct0014",
    }
    c = (ct or "").strip().lower()
    return "ct0000" if c in _common else c


# ── Основная функция движка ──────────────────────────────────────────────────

def run_create_set_post(
    *,
    it: dict,
    name: str,
    login: str,
    slepok: str,
    site_type: str,
    href: str,
    region_ids: list,
    counter_id: int | str | None,
    goal_id: int | str | None,
    grid_cookie: str | None,
    tp_code: str,          # "tp8" | "tp9" | "tp10"
    r_code: str,           # регион-код кодера (напр. "r0034")
    oblast: str,           # человекочитаемое название области
    ct: str = "ct0000",    # ct для резолвинга картинок и имён групп
    brand_label: str = "Посевы",  # человекочитаемый ярлык бренда/группы
    corr: dict | None = None,     # корректировки ставок (демография) — из Victory БД
    save_draft: bool = True,
    avoid: list | None = None,   # заголовки из соседних tp8/9/10 — для avoid-регенерации
    job: dict | None = None,
    add_job_err=None,
    bump_job=None,
    bump_item=None,
    job_db_progress=None,
    **_kw,
) -> list[dict]:
    """Создаёт одну GdPostCampaign: AddCampaigns → AddPostAdGroups×N → AddPostAds×N.

    Возвращает список из одного result-dict (ok/name/campaign_id/... или ok=False/error).
    N = число найденных картинок (1-3), минимум 1 группа даже при 0 картинок.
    """
    try:
        from . import grid_finalize as gf  # noqa: PLC0415
    except ImportError:
        import grid_finalize as gf  # type: ignore[no-redef]  # noqa: PLC0415

    _aje = add_job_err or (lambda _j, _m: None)
    _bj = bump_job or (lambda _j: None)
    _bi = bump_item or (lambda _j: None)
    _jdp = job_db_progress or (lambda _j: None)

    def _fail(err: str, **extra) -> list[dict]:
        _aje(job, f"{name}: {err}")
        return [{"ok": False, "name": name, "error": err, **extra}]

    # ── GridClient (переиспользуемый, потокобезопасный — SPEC §1b) ────────
    try:
        cli = gf.get_grid_client(login, cookie=grid_cookie)
        # CSRF-bootstrap: обязателен перед первым _post (не вызывается get_grid_client).
        # Если CSRF уже есть (повторный вызов того же клиента), _bootstrap_csrf — no-op.
        # Без этого первый _post (PostDefaultEmail) получает bootstrap-ответ вместо данных,
        # _fetch_notification_email возвращает "", tp8 падает (tp9/tp10 — нет, CSRF уже есть).
        cli._bootstrap_csrf()
    except Exception as e:
        return _fail(f"GridClient init: {e!s:.120}")

    # ── Email аккаунта для notification (Grid NonNull — обязателен для GdPostCampaign) ──
    # Один READ-запрос PostDefaultEmail; result переиспользуется в notification ниже.
    _notif_email = _fetch_notification_email(cli, login)
    if not _notif_email:
        return _fail("PostDefaultEmail: не удалось получить email аккаунта для notification")

    # ── Картинки: резолвинг + загрузка ─────────────────────────────────────
    img_ct = _resolve_img_ct(ct)
    img_paths = _posevy_images_for_ct(img_ct, limit=POST_IMAGE_LIMIT)
    print(f"[post-engine] {login} {tp_code}: img_ct={img_ct!r} paths_found={len(img_paths)}", flush=True)

    image_hashes: list[str] = []
    for path in img_paths:
        try:
            h = cli.upload_image(path)
            if h and h not in image_hashes:
                image_hashes.append(h)
        except Exception as e:  # noqa: BLE001
            print(f"[post-engine] {login} {tp_code}: upload_image failed {path!r}: {e!s:.80}", flush=True)

    # 0 хэшей допустимо (API принимает multicards:[], SPEC §2.11 п.5)
    n_groups = max(1, len(image_hashes))
    print(f"[post-engine] {login} {tp_code}: hashes={image_hashes!r} n_groups={n_groups}", flush=True)

    # ── Контент (title / body) ──────────────────────────────────────────────
    # Приоритет 1: явно переданные в item (ручной override — оставляем путь).
    # Приоритет 2: generate_post_ad_content (ai_content, 7a8c1ee) — ToV слепка + M3.
    # Приоритет 3: generic-fallback строки (никогда не блокируем).
    title_text = (it.get("title") or it.get("post_title") or "").strip()
    body_text  = (it.get("body")  or it.get("post_body")  or "").strip()

    if not title_text or not body_text:
        try:
            from .ai_content import generate_post_ad_content as _gpac  # noqa: PLC0415
        except ImportError:
            from ai_content import generate_post_ad_content as _gpac  # type: ignore[no-redef]  # noqa: PLC0415
        try:
            _domain = urllib.parse.urlparse(href or "").netloc or ""
            # brand_label: если "Посевы" (дефолт) → нет конкретного бренда → передаём ""
            _brand = brand_label if (brand_label and brand_label != "Посевы") else ""
            _content = _gpac(
                slepok=slepok,
                site_type=site_type or "Монобренд",
                brand=_brand,
                city=oblast or "",
                domain=_domain,
                avoid=avoid or [],
            )
            if not title_text:
                title_text = _content.get("title", "")
            if not body_text:
                body_text = _content.get("body", "")
        except Exception as _ce:  # noqa: BLE001
            print(f"[post-engine] {login} {tp_code}: generate_post_ad_content failed: {_ce!s:.120}",
                  flush=True)

    # Финальная страховка лимитов (generate_post_ad_content их уже применяет; fallback — ниже)
    if not title_text:
        title_text = "Широкий выбор автомобилей"
    if not body_text:
        body_text = "Узнайте актуальные предложения и запишитесь на тест-драйв."
    title_text = title_text[:POST_TITLE_MAX]
    body_text  = body_text[:POST_BODY_MAX]

    # ── ag_code, dem_adj — ДО AddCampaigns (нужны для name и useBidModifiers) ──
    # _bid_mod_dem строится ПОСЛЕ AddCampaigns: требует campaignId (NonNull в Grid-схеме).
    ag_code = _ag_code_for_corr(corr)
    dem_adj = _dem_adjustments_for_corr(corr)
    # Пересчитываем name с правильным ag_code (план строил имя с жёстким ag001)
    name = _campaign_name(tp_code, ct, r_code, oblast, brand_label, ag_code)

    # ── Шаг 1: AddCampaigns ────────────────────────────────────────────────
    platforms = _build_platforms(tp_code)
    add_camp_vars = {
        "input": {
            "campaignAddItems": [{
                "postCampaign": {
                    "name": name,
                    "isS2sTrackingEnabled": False,
                    "biddingStategyWithPlatforms": {
                        "platforms": platforms,
                        "strategyName": "AUTOBUDGET",
                        "strategyData": {
                            "goalId": str(goal_id) if goal_id else None,
                            "bid": str(POST_BID),
                            "payForShows": False,
                            "sum": str(POST_BUDGET_SUM),
                            "budgetType": "WEEKLY",
                        },
                    },
                    "attributionModel": "AUTOMATIC",
                    "metrikaCounters": [int(counter_id)] if counter_id else [],
                    "meaningfulGoals": [],
                    "startDate": date.today().isoformat(),
                    "endDate": None,
                    "disabledPlaces": [],
                    "bannerHrefParams": _UTM_CAMPAIGN_LEVEL,
                    "broadMatch": {
                        "broadMatchFlag": False,
                        "broadMatchGoalId": None,
                        "broadMatchLimit": 0,
                    },
                    "dayBudget": "0",
                    "enableCompanyInfo": False,
                    "excludePausedCompetingAds": True,
                    "hasAddMetrikaTagToUrl": False,
                    "hasAddOpenstatTagToUrl": False,
                    "hasExtendedGeoTargeting": False,  # инвариант #5 (SPEC §8)
                    "hasSiteMonitoring": False,
                    "hasTitleSubstitute": False,
                    # notification — обязателен (NonNull) для GdPostCampaign.
                    # email берём из PostDefaultEmail pre-fetch (_notif_email, выше).
                    "notification": {
                        "smsSettings": {
                            "smsTime": {
                                "startTime": {"hour": 9,  "minute": 0},
                                "endTime":   {"hour": 21, "minute": 0},
                            },
                            "enableEvents": [],
                        },
                        "emailSettings": {
                            "stopByReachDailyBudget": False,
                            "email": _notif_email,
                        },
                    },
                    "timeTarget": {
                        "enabledHolidaysMode": False,
                        "holidaysSettings": None,
                        "idTimeZone": "130",
                        "timeBoard": _TIME_BOARD_24x7,
                        "useWorkingWeekends": True,
                    },
                },
            }],
        },
        # ⚠ login идёт ТОЛЬКО через URL-параметр ?ulogin= (GridClient._post), не в variables!
    }

    try:
        r = cli._post("AddCampaigns", _Q_ADD_CAMPAIGNS, add_camp_vars)
        resp = r.json()
        errs = _grid_vr_errors(resp, "addCampaigns")
        if errs:
            return _fail(f"AddCampaigns errors: {json.dumps(errs, ensure_ascii=False)[:300]}")
        added = ((resp.get("data") or {}).get("addCampaigns") or {}).get("addedCampaigns") or []
        if not added:
            return _fail("AddCampaigns: нет addedCampaigns в ответе")
        campaign_id = added[0].get("id")
    except Exception as e:
        return _fail(f"AddCampaigns exception: {e!s:.200}")

    print(f"[post-engine] {login} {tp_code}: campaign_id={campaign_id} name={name!r}", flush=True)

    # ── Корректировки возраста: _bid_mod_dem строится ПОСЛЕ AddCampaigns ──
    # campaignId известен только здесь; Grid-схема требует NonNull Long! (Bug 2 Critical).
    # Структура по аналогии с create_set_corrections.py:176-178.
    _bid_mod_dem = (
        {"campaignId": str(campaign_id), "enabled": True,
         "adjustments": dem_adj, "type": "DEMOGRAPHY_MULTIPLIER"}
        if dem_adj else None
    )

    # ── Шаг 2: AddPostAdGroups × n_groups ─────────────────────────────────
    ad_group_ids: list = []

    for idx in range(n_groups):
        grp_name = _group_name(ct, r_code, idx, brand_label, ag_code)
        grp_vars = {
            "postAddInput": [{
                "name": grp_name,
                "campaignId": str(campaign_id),
                "regionIds": region_ids or [],
                "bidModifiers": {"bidModifierDemographics": _bid_mod_dem},
                "useBidModifiers": bool(dem_adj),
                "useAllTelegramCategories": True,   # SPEC §2.5 — всегда все категории
                "customTelegramCategories": [],
                "brief": None,
            }],
        }
        try:
            r = cli._post("AddPostAdGroups", _Q_ADD_POST_AD_GROUPS, grp_vars)
            resp = r.json()
            errs = _grid_vr_errors(resp, "addPostAdGroups")
            if errs:
                print(f"[post-engine] {login} {tp_code}: group[{idx}] errors: "
                      f"{json.dumps(errs, ensure_ascii=False)[:200]}", flush=True)
                _aje(job, f"{name}: group[{idx}] error: {json.dumps(errs[:1], ensure_ascii=False)[:100]}")
                continue  # не прерываем — остальные группы могут пройти
            added_g = (((resp.get("data") or {}).get("addPostAdGroups") or {})
                       .get("addedAdGroupItems") or [])
            if not added_g:
                print(f"[post-engine] {login} {tp_code}: group[{idx}]: no adGroupId", flush=True)
                continue
            gid = added_g[0].get("adGroupId")
            ad_group_ids.append(gid)
            print(f"[post-engine] {login} {tp_code}: group[{idx}] id={gid} name={grp_name!r}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[post-engine] {login} {tp_code}: group[{idx}] exception: {e!s:.120}", flush=True)

    if not ad_group_ids:
        return _fail("AddPostAdGroups: все группы не созданы",
                     campaign_id=campaign_id, partial=True)

    # ── Шаг 3: AddPostAds × len(ad_group_ids) ─────────────────────────────
    ad_items = []
    for gidx, gid in enumerate(ad_group_ids):
        multicard = []
        if gidx < len(image_hashes):
            multicard = [{"imageHash": image_hashes[gidx]}]
        ad_items.append({
            "adGroupId":      str(gid),
            "href":           href,
            "domain":         None,
            "body":           body_text,
            "title":          title_text,
            "titleExtension": None,
            "creativeId":     None,
            "button":         {"action": POST_DEFAULT_BUTTON, "href": href},
            "isMobile":       False,
            "multicards":     multicard,
            "inheritableCallouts":   None,
            "inheritableSitelinkSet": None,
        })

    ads_vars = {
        "addPostInput": {
            "adAddItems": ad_items,
            "saveDraft":  save_draft,
        },
    }

    ad_ids: list = []
    try:
        r = cli._post("AddPostAds", _Q_ADD_POST_ADS, ads_vars)
        resp = r.json()
        errs = _grid_vr_errors(resp, "addPostAds")
        if errs:
            return _fail(f"AddPostAds errors: {json.dumps(errs, ensure_ascii=False)[:300]}",
                         campaign_id=campaign_id, ad_group_ids=ad_group_ids, partial=True)
        added_a = (((resp.get("data") or {}).get("addPostAds") or {}).get("addedAds") or [])
        ad_ids = [a.get("id") for a in added_a if a.get("id")]
        print(f"[post-engine] {login} {tp_code}: {len(ad_ids)} ads created ids={ad_ids}", flush=True)
    except Exception as e:
        return _fail(f"AddPostAds exception: {e!s:.200}",
                     campaign_id=campaign_id, ad_group_ids=ad_group_ids, partial=True)

    _bi(job)
    _bj(job)
    _jdp(job)

    return [{
        "ok":           True,
        "name":         name,
        "campaign_id":  campaign_id,
        "ad_group_ids": ad_group_ids,
        "ad_ids":       ad_ids,
        "tp":           tp_code,
        "n_groups":     len(ad_group_ids),
        "images_used":  len(image_hashes),
        "save_draft":   save_draft,
        "title":        title_text,   # для накопления avoid между tp8/9/10 одного набора
    }]

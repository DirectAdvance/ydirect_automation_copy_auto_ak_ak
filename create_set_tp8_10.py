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
import re
import urllib.error
import urllib.parse
import urllib.request
import gzip
import html
import subprocess
import warnings
import zlib
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
_G_CODE = "g00"              # пол: g00=Все; g01/g02 только при gender-корректировке
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
_PHONE_CACHE: dict[str, str] = {}
_SITE_TEXT_CACHE: dict[str, str] = {}
_PHONE_FETCH_UA = "Mozilla/5.0"

_POST_BODY_FILLERS = (
    "Сверим наличие и комплектацию до визита, чтобы расчёт был привязан к реальному автомобилю.",
    "Подберём кредитную программу под ваш бюджет и заранее расскажем, какие бонусы доступны по выбранной модели.",
    "Можно сравнить комплектации и платежи до визита в автосалон.",
    "Менеджер уточнит наличие, комплектацию и ориентировочный ежемесячный платёж по выбранному автомобилю.",
    "Если модель уже выбрана, проверим наличие и предложим близкие варианты по цене, году и комплектации.",
    "Расскажем, какие документы нужны для заявки, и поможем заранее оценить комфортный срок кредита.",
    "Подскажем, какие автомобили подходят под выбранный платёж, и покажем варианты без лишнего ожидания.",
    "Зафиксируем условия обращения и передадим заявку менеджеру, чтобы быстрее вернуться с расчётом.",
    "Перед визитом сверим условия и поможем выбрать удобное время для звонка.",
    "Уточним детали до визита.",
    "Покажем доступные варианты по платежу.",
)
_POST_BODY_TARGET_FREE = 30


def configure(deps: dict) -> None:
    """Bind blueprint dependencies (DI-паттерн, как у create_set_tp1_builders)."""
    _DEPS.clear()
    _DEPS.update(deps)
    globals().update(deps)


def _post_disabled_places_for_geo(geo: str) -> list[str]:
    """Минус-площадки Посевов: общий geo='*' + geo конкретной кампании."""
    fn = _DEPS.get("_enabled_post_minus_places")
    if not callable(fn):
        return []
    try:
        return list(fn(geo) or [])
    except Exception as exc:  # noqa: BLE001
        print(f"[post-engine] disabledPlaces load failed geo={geo!r}: {exc!s:.120}", flush=True)
        return []


def _post_feed_url_map(login: str, href: str) -> dict:
    fn = _DEPS.get("_account_offer_urls")
    if not callable(fn):
        return {}
    try:
        return dict(fn(login, href) or {})
    except Exception as exc:  # noqa: BLE001
        print(f"[post-engine] feed urls load failed login={login!r}: {exc!s:.120}", flush=True)
        return {}


def _post_feed_price_map(login: str, href: str) -> dict:
    """DI-wrapper for account offer prices: {key(lower): (current, old)}.

    Пробует DI-ключ '_account_offer_prices' (инъектируется оркестратором).
    Если DI не настроен — прямой импорт из create_set_feeds (fallback без оркестратора).
    """
    fn = _DEPS.get("_account_offer_prices")
    if not callable(fn):
        # Прямой импорт как fallback — работает до обновления оркестратора
        try:
            from .create_set_feeds import _account_offer_prices as fn  # noqa: PLC0415
        except ImportError:
            try:
                from create_set_feeds import _account_offer_prices as fn  # type: ignore[no-redef]  # noqa: PLC0415
            except ImportError:
                return {}
    try:
        return dict(fn(login, href) or {})
    except Exception as exc:  # noqa: BLE001
        print(f"[post-engine] feed prices load failed login={login!r}: {exc!s:.120}", flush=True)
        return {}


def _post_feed_url_for_label(urls: dict, label: str) -> str:
    if not urls:
        return ""
    fn = _DEPS.get("_feed_url_for_model")
    if callable(fn):
        try:
            return str(fn(urls, label, no_brand_fallback=(" " in str(label or "").strip())) or "")
        except TypeError:
            try:
                return str(fn(urls, label) or "")
            except Exception:  # noqa: BLE001
                return ""
        except Exception:  # noqa: BLE001
            return ""
    key = re.sub(r"\s+", " ", str(label or "").strip().lower())
    return str(urls.get(key) or "")


def _post_ct_segment(ct: str) -> str:
    fn = _DEPS.get("_ct_segment") or globals().get("_ct_segment")
    if callable(fn):
        try:
            return str(fn(ct) or "")
        except Exception:  # noqa: BLE001
            pass
    return ""


def _strip_url_query_local(u: str) -> str:
    try:
        p = urllib.parse.urlsplit(str(u or "").strip())
        return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path.rstrip("/") or "/", "", ""))
    except Exception:  # noqa: BLE001
        return str(u or "").strip()


def _post_label_is_brand_level(label: str, ct: str, urls: dict) -> bool:
    seg = _post_ct_segment(ct)
    if seg == "Марки":
        return True
    if seg == "Модели":
        return False
    label_norm = re.sub(r"\s+", " ", str(label or "").strip().lower())
    return bool(label_norm and " " not in label_norm and label_norm in (urls or {}))


def _post_href_for_label(login: str, base_href: str, label: str,
                         ct: str = "", site_type: str = "") -> str:
    """Deep-link post button to brand/model URL from feed when available.

    Посевы бывают brand-level и model-level. Для brand-level нельзя оставлять URL первого
    товара марки (`/auto/haval/m6/...`): кнопка должна вести на страницу марки (`/auto/haval`).
    """
    if not label or str(label).lower() == "посевы":
        return base_href.rstrip("/")
    try:
        from .model_urls import _brand_level_url, _is_degenerate_feed_url, _model_page_href  # noqa: PLC0415
    except ImportError:
        from model_urls import (_brand_level_url, _is_degenerate_feed_url,  # type: ignore[no-redef]  # noqa: PLC0415
                                _model_page_href)

    urls = _post_feed_url_map(login, base_href)
    raw = _post_feed_url_for_label(urls, label)
    # AD_HREF_ROOT_INSTEAD_OF_MODEL: вырожденный URL из фида (квиз-оффер `/quiz?fid=…`, голый
    # корень) игнорируем — кнопка поста иначе уходит на главную вместо страницы марки/модели.
    if raw and not _is_degenerate_feed_url(raw):
        if _post_label_is_brand_level(label, ct, urls):
            return _brand_level_url(raw)
        return _strip_url_query_local(raw)
    fallback = _model_page_href(base_href, site_type, label)
    return fallback.rstrip("/") if fallback else base_href.rstrip("/")


def _title_case_vehicle_name(s: str) -> str:
    parts = []
    for p in re.split(r"\s+", str(s or "").strip()):
        if not p:
            continue
        parts.append(p.upper() if re.search(r"\d", p) else p[:1].upper() + p[1:])
    return " ".join(parts)


def _post_allowed_models_from_feed(login: str, base_href: str, label: str, limit: int = 6) -> list[str]:
    """Whitelist vehicle names for LLM. Feed keys only; no generated names."""
    urls = _post_feed_url_map(login, base_href)
    if not urls:
        return []
    label_norm = re.sub(r"\s+", " ", str(label or "").strip().lower())
    generic = not label_norm or label_norm == "посевы"
    out: list[str] = []
    seen: set[str] = set()
    for key in urls.keys():
        k = re.sub(r"\s+", " ", str(key or "").strip().lower())
        if not k or len(k) < 3:
            continue
        if not generic and not (k == label_norm or k.startswith(label_norm + " ")):
            continue
        # Для брендовой кампании не отдаём один только бренд как "модель" в списке.
        if not generic and k == label_norm:
            continue
        val = _title_case_vehicle_name(k)
        if val.lower() in seen:
            continue
        seen.add(val.lower())
        out.append(val)
        if len(out) >= limit:
            break
    return out


_BRAND_OFFER_DIVISOR = 84   # простое деление: платёж = цена ÷ 84, без ставки и аннуитета


def _fmt_payment(amount: int) -> str:
    """Форматировать целое число в русский формат с пробелами-разделителями тысяч."""
    return f"{amount:,}".replace(",", " ")  # ASCII space (U+0020), не NNBSP — типографский стандарт


def _build_brand_offer_block(login: str, href: str, label: str, ct: str, limit: int = 5) -> str:
    """Детерминированный блок марочных оферов для посевов (ct-сегмент «Марки»).

    Возвращает строки вида «Haval Jolion – от 7 355 ₽/мес» (ровно до *limit* штук),
    отсортированные по возрастанию платежа. Пустая строка, если:
    - это не марочная кампания (seg != «Марки»);
    - цены из фида недоступны;
    - ни одна позиция не имеет валидной цены (правило: не подставлять 0 / не выдумывать).

    Расчёт: цена (актуальная) ÷ {BRAND_OFFER_DIVISOR}; округление вниз (math.floor).
    % ставки кредита НЕ печатается (правило Семёна, закреплено в text_gen.py:758).
    """
    if not label or str(label).lower() == "посевы":
        return ""
    if _post_ct_segment(ct) != "Марки":
        return ""

    prices = _post_feed_price_map(login, href)
    if not prices:
        return ""

    brand_lc = re.sub(r"\s+", " ", str(label or "").strip().lower())

    # Шаг 1: берём модели из _post_allowed_models_from_feed (до 2×limit = запас)
    model_names = _post_allowed_models_from_feed(login, href, label, limit=limit * 2)

    candidates: list[tuple[int, str]] = []   # (payment, display_name)
    seen_keys: set[str] = set()

    def _try_add(name_display: str, name_lc: str) -> bool:
        """Найти цену для имени, посчитать платёж, добавить в candidates. True при успехе."""
        if name_lc in seen_keys:
            return False
        price_tup = prices.get(name_lc)
        if price_tup is None:
            # Попробовать без года: «haval jolion 2025» → «haval jolion»
            ny = re.sub(r"\s*\b20\d\d\b", " ", name_lc).strip()
            if ny != name_lc:
                price_tup = prices.get(ny)
        if not price_tup or price_tup[0] <= 0:
            return False
        current_price = price_tup[0]          # актуальная (новая) цена из фида
        payment = current_price // _BRAND_OFFER_DIVISOR
        if payment <= 0:
            return False
        seen_keys.add(name_lc)
        candidates.append((payment, name_display))
        return True

    for model in model_names:
        _try_add(model, model.strip().lower())

    # Шаг 2: если < limit — добираем из price map по ключам с префиксом бренда
    if len(candidates) < limit:
        brand_prefix = brand_lc + " "
        for key in sorted(prices.keys()):
            if len(candidates) >= limit * 2:
                break
            if not key.startswith(brand_prefix):
                continue
            if len(key.split()) < 2:
                continue
            _try_add(_title_case_vehicle_name(key), key)

    if not candidates:
        return ""

    # Шаг 3: сортировка по возрастанию платежа, берём top-limit
    candidates.sort(key=lambda x: x[0])
    lines = [
        f"{name} – от {_fmt_payment(pay)} ₽/мес"   # «–» = en-dash (U+2013)
        for pay, name in candidates[:limit]
    ]
    return "\n".join(lines)


def _replace_post_model_list(body: str, brand_block: str) -> str:
    """Заменить LLM-сгенерированный список модель→цена детерминированным блоком.

    Алгоритм:
    1. Считает ₽/мес-строки по ВСЕМ параграфам тела.
    2. Если суммарно ≥2 — нашли список, даже если LLM разбил каждую модель отдельным
       параграфом. Заменяем параграф с наибольшим числом ценовых строк (при равенстве —
       первый совпавший); сохраняем заголовок «В наличии:». Параграфы, где ВСЕ непустые
       строки содержат ₽/мес (чисто-ценовые), отбрасываем — иначе в посте два блока
       с РАЗНЫМИ суммами на одни и те же машины.
    3. Если суммарно <2 (одно случайное упоминание цены или его нет) — ищем параграф
       с 2+ ценовыми строками (старая логика). Не найден → вставляем блок после первого
       параграфа.
    Гасит дубль «LLM-список + детерминированный блок» на стороне сборки.
    """
    if not brand_block:
        return body

    price_re = re.compile(r"₽/мес|₽\s*/\s*мес")
    paragraphs = (body or "").split("\n\n")

    def _para_price_count(para: str) -> int:
        return sum(1 for ln in para.splitlines() if price_re.search(ln))

    def _is_price_only(para: str) -> bool:
        """Все непустые строки параграфа содержат ₽/мес."""
        lines = [ln for ln in para.splitlines() if ln.strip()]
        return bool(lines) and all(price_re.search(ln) for ln in lines)

    def _extract_header(para: str) -> str:
        """Строка-заголовок: оканчивается на «:», не содержит цен."""
        for ln in para.splitlines():
            plain = re.sub(r":(?:b|bb|i|ii|s|ss):", "", ln).strip()
            if plain.endswith(":") and not price_re.search(ln):
                return ln
        return ""

    total_price_lines = sum(_para_price_count(p) for p in paragraphs)
    new_paras: list[str] = []
    inserted = False

    if total_price_lines >= 2:
        # Список обнаружен глобально — возможно, разбит на отдельные параграфы.
        # Выбираем параграф с максимальным числом ценовых строк (первый при равенстве).
        best_idx = -1
        best_count = 0
        for i, para in enumerate(paragraphs):
            cnt = _para_price_count(para)
            if cnt > best_count:
                best_count = cnt
                best_idx = i

        for i, para in enumerate(paragraphs):
            if not inserted and i == best_idx:
                header = _extract_header(para)
                replacement = (header + "\n" + brand_block) if header else brand_block
                new_paras.append(replacement)
                inserted = True
            elif _is_price_only(para):
                # Чисто-ценовой параграф (не целевой) — отбрасываем, чтобы не
                # оставить в посте конкурирующие LLM-суммы.
                pass
            else:
                new_paras.append(para)
    else:
        # Суммарно <2 ценовых строк — одиночное упоминание цены или его нет.
        # Старая логика: ищем параграф с 2+ ценовыми строками.
        for para in paragraphs:
            if inserted:
                new_paras.append(para)
                continue
            if _para_price_count(para) >= 2:
                header = _extract_header(para)
                replacement = (header + "\n" + brand_block) if header else brand_block
                new_paras.append(replacement)
                inserted = True
            else:
                new_paras.append(para)

    if not inserted:
        # Список не найден — вставить блок после первого параграфа.
        if len(new_paras) > 1:
            new_paras.insert(1, brand_block)
        elif new_paras:
            new_paras.append(brand_block)
        else:
            new_paras = [brand_block]

    result = "\n\n".join(new_paras)
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def _extend_post_body_after_finalize(text: str, brand_label: str, href: str) -> str:
    """Fill remaining body space before CTA/phone with safe generic facts."""
    try:
        from . import ai_agents as A  # noqa: PLC0415
    except ImportError:
        import ai_agents as A  # type: ignore[no-redef]  # noqa: PLC0415
    body = _ensure_post_phone_last(_trim_post_body_preserve_phone(text, POST_BODY_MAX))
    if len(body) >= A.POST_BODY_TARGET_MIN:
        return body
    before_phone, phone_line, tail = _split_post_phone_line(body)
    base = before_phone if phone_line else body
    if tail and _should_keep_post_tail(base, tail):
        candidate = _normalize_post_body_structure(_insert_post_paragraph_before_cta(base, tail))
        if len(candidate) + (len("\n\n") + len(phone_line) if phone_line else 0) <= POST_BODY_MAX:
            base = candidate
    brand = "" if str(brand_label or "").lower() == "посевы" else str(brand_label or "").strip()
    subject = brand or "авто в наличии"
    supplements = [
        f"Подберём {subject} под комфортный ежемесячный платёж и заранее сверим наличие.",
        "Проверим одобрение в банках-партнёрах без визита в салон.",
        "Зафиксируем условия акции до визита.",
        ":b:Что можно уточнить по заявке::bb:\n"
        "— актуальное наличие и комплектацию;\n"
        "— размер первого взноса и срок кредита;\n"
        "— платёж с учётом трейд-ин и бонусов.",
        "Оставьте контакт — подготовим расчёт под ваш бюджет и покажем доступные варианты без лишнего ожидания.",
        ":b:Почему лучше обратиться сейчас::bb:\n"
        "— условия акции могут измениться после обновления склада;\n"
        "— часть автомобилей доступна в ограниченном количестве;\n"
        "— предварительный расчёт ускоряет выбор программы.",
        "Уточним детали до визита.",
        "Покажем варианты по платежу.",
    ]
    existing_topics = {_post_benefit_topic(line) for line in base.splitlines()}
    existing_topics.discard("")
    for sup in supplements:
        current = _append_existing_post_phone_line(base, phone_line) if phone_line else base
        if len(current) >= A.POST_BODY_TARGET_MIN:
            break
        sup_topic = _post_benefit_topic(sup)
        if sup_topic and sup_topic in existing_topics:
            continue
        candidate_base = _normalize_post_body_structure(_insert_post_paragraph_before_cta(base, sup))
        candidate = _append_existing_post_phone_line(candidate_base, phone_line) if phone_line else candidate_base
        if len(candidate) > POST_BODY_MAX or len(candidate) <= len(current):
            continue
        base = candidate_base
        if sup_topic:
            existing_topics.add(sup_topic)
    return _append_existing_post_phone_line(base, phone_line) if phone_line else _trim_post_body(base, POST_BODY_MAX)


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

# Grid-коды возраста для GdPostCampaign bidModifierDemographics.
# Post-группы принимают младший возраст как "_0_17".
_GRID_AGE_MAP_TP810 = {
    "AGE_0_17":  "_0_17",
    "AGE_18_24": "_18_24",
    "AGE_25_34": "_25_34",
    "AGE_35_44": "_35_44",
    "AGE_45_54": "_45_54",
    "AGE_55":    "_55_",
}

_POST_DEMOGRAPHY_MIN_DELTA = -50
_POST_DEMOGRAPHY_MIN_MULTIPLIER = 50
_POST_DEMOGRAPHY_MAX_MULTIPLIER = 1300


def _dem_adjustments_for_corr(corr: dict | None) -> list:
    """Список demographic-adjustments для Grid AddPostAdGroups.bidModifierDemographics.

    ``corr`` приходит в v5-конвенции дельты. Для посевов Direct не даёт уменьшать
    ставку сильнее чем на 50%, поэтому -100/-75 сначала зажимаем до -50. Grid
    ``percent`` для demographics — мультипликатор: -50 -> 50, +30 -> 130.
    Нулевую дельту пропускаем как нейтральную.
    """
    if not corr:
        return []
    dem_adj: list = []
    for d in corr.get("demographic", []):
        pct = int(d.get("pct") or 0)
        if pct == 0:
            continue
        pct = max(_POST_DEMOGRAPHY_MIN_DELTA, pct)
        multiplier = max(
            _POST_DEMOGRAPHY_MIN_MULTIPLIER,
            min(_POST_DEMOGRAPHY_MAX_MULTIPLIER, 100 + pct),
        )
        adj_entry: dict = {"percent": multiplier, "id": None}
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


def _g_code_for_corr(corr: dict | None) -> str:
    """Кодер пола для post-групп: индекс картинки/варианта не влияет на `g`.

    g00 = все, g01 = мужчины, g02 = женщины. Если в одной группе правил есть
    несколько gender-корректировок, имя остаётся общим g00: один кодер не может
    честно выразить два разных пола одновременно.
    """
    genders: set[str] = set()
    for d in (corr or {}).get("demographic", []):
        if d.get("kind") != "gender" or int(d.get("pct") or 0) == 0:
            continue
        key = str(d.get("key") or "").upper()
        if "MALE" in key and "FEMALE" not in key:
            genders.add("g01")
        elif "FEMALE" in key:
            genders.add("g02")
    return next(iter(genders)) if len(genders) == 1 else _G_CODE


def _strip_post_markup(text: str) -> str:
    """Remove all Direct post formatting tags.

    Kept for tests/backward compatibility. Runtime uses _prepare_post_body()
    so valid highlights remain in created posts.
    """
    text = str(text or "")
    return re.sub(r":(?:b|bb|i|ii|s|ss):", "", text)


def _normalize_post_markup(text: str) -> str:
    """Keep only balanced Direct post formatting tags.

    Grid rejects malformed markup in ``GdPostAd.body`` with INVALID_MARKUP.
    This parser removes orphan closers/duplicate openers and auto-closes open
    tags at the end, while preserving valid :b:/:i:/:s: highlights.
    """
    text = re.sub(r"(?m)(^|[\s(])([bis]):", r"\1:\2:", str(text or ""))
    pairs = {"b": "bb", "i": "ii", "s": "ss"}
    closers = {v: k for k, v in pairs.items()}
    tokens = re.split(r"(:(?:b|bb|i|ii|s|ss):)", text)
    open_stack: list[str] = []
    out: list[str] = []
    for tok in tokens:
        m = re.fullmatch(r":(b|bb|i|ii|s|ss):", tok or "")
        if not m:
            out.append(tok)
            continue
        tag = m.group(1)
        if tag in pairs:
            if tag in open_stack:
                continue
            open_stack.append(tag)
            out.append(tok)
            continue
        opener = closers[tag]
        if opener not in open_stack:
            continue
        while open_stack:
            cur = open_stack.pop()
            out.append(f":{pairs[cur]}:")
            if cur == opener:
                break
    while open_stack:
        out.append(f":{pairs[open_stack.pop()]}:")
    return "".join(out)


def _strip_post_italic_markup(text: str) -> str:
    """Post body read-path is fragile on italic wrapped around bold highlights."""
    text = re.sub(r":(?:i|ii):", "", str(text or ""))
    text = re.sub(r"(?m)(^|[\s(])i:", r"\1", text)
    return text


def _finalize_post_markup(text: str) -> str:
    """Normalize supported post markup and keep only safe bold/strike tags."""
    return _normalize_post_markup(_strip_post_italic_markup(_normalize_post_markup(text)))


def _post_plain_line(line: str) -> str:
    return re.sub(r"\s+", " ", _strip_post_markup(line).strip())


def _is_post_phone_line(line: str) -> bool:
    plain = _post_plain_line(line)
    return bool(re.fullmatch(r"(?i)Подробности\s+по\s+телефону\s*:\s*\+?\d[\d\s()\-]{7,}", plain))


def _post_section_or_cta_starts(line: str) -> bool:
    plain = _post_plain_line(line).lower()
    if not plain:
        return False
    if plain.endswith(":"):
        return True
    return bool(re.match(
        r"^(?:при\s+оформлении|бонусы|оставьте\s+заявк|оставляйте\s+заявк|подбер[её]м|"
        r"можно\s+сравнить|подробности\s+по\s+телефону)\b",
        plain,
    ))


def _collapse_post_blank_lines(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", str(text or ""))
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _remove_empty_post_inventory_sections(text: str) -> str:
    """Drop heading-only inventory blocks such as ``В наличии:`` with no items below."""
    lines = str(text or "").splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        plain = _post_plain_line(line).lower()
        if re.fullmatch(r"в\s+наличии:?", plain):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j >= len(lines) or _post_section_or_cta_starts(lines[j]):
                i = j
                continue
        out.append(line)
        i += 1
    return _collapse_post_blank_lines("\n".join(out))


def _post_benefit_topic(line: str) -> str:
    plain = _post_plain_line(line).lower()
    if "каско" in plain:
        return "kasko"
    if re.search(r"\b(?:трейд-?ин|trade-?in)\b", plain):
        return "tradein"
    if re.search(r"\b(?:шин|резин)", plain):
        return "tires"
    if re.search(r"перв(?:ый|ого)\s+взнос|0\s*₽\s+перв", plain):
        return "downpayment"
    if "одобрени" in plain or "банк" in plain:
        return "approval"
    return ""


def _dedupe_post_benefit_lines(text: str) -> str:
    """Keep one visible benefit per topic so late fillers do not become separate duplicates."""
    seen: set[str] = set()
    out: list[str] = []
    for line in str(text or "").splitlines():
        topic = _post_benefit_topic(line)
        if topic and topic in seen:
            continue
        if topic:
            seen.add(topic)
        out.append(line)
    return _collapse_post_blank_lines("\n".join(out))


def _should_keep_post_tail(base: str, tail: str) -> bool:
    """Keep moved phone-tail only when it is not a duplicate standalone benefit."""
    tail = str(tail or "").strip()
    if not tail:
        return False
    topic = _post_benefit_topic(tail)
    if not topic:
        return True
    if re.match(r"^\s*[—-]", _strip_post_markup(tail)):
        return False
    existing_topics = {_post_benefit_topic(line) for line in str(base or "").splitlines()}
    existing_topics.discard("")
    return topic not in existing_topics


def _normalize_post_language(text: str) -> str:
    text = str(text or "")
    text = re.sub(r"(?i)\bновый\s+авто\b", "новое авто", text)
    return text


def _split_post_phone_line(text: str) -> tuple[str, str, str]:
    """Return text before phone, the phone line, and any tail after it."""
    lines = str(text or "").splitlines()
    phone_idx = -1
    for idx, line in enumerate(lines):
        if _is_post_phone_line(line):
            phone_idx = idx
    if phone_idx < 0:
        return str(text or "").rstrip(), "", ""
    before = "\n".join(lines[:phone_idx]).rstrip()
    phone = lines[phone_idx].strip()
    after_lines = [ln for ln in lines[phone_idx + 1:] if ln.strip() and not _is_post_phone_line(ln)]
    return before, phone, "\n".join(after_lines).strip()


def _insert_post_paragraph_before_cta(src: str, paragraph: str) -> str:
    paragraph = str(paragraph or "").strip()
    if not paragraph:
        return str(src or "").strip()
    parts = str(src or "").strip().split("\n\n") if str(src or "").strip() else []
    if parts and re.search(r"(?i)^\s*(?::b:)?остав(?:ьте|ляйте)\s+заявк", _strip_post_markup(parts[-1]).strip()):
        parts.insert(len(parts) - 1, paragraph)
        return "\n\n".join(parts)
    return f"{src.rstrip()}\n\n{paragraph}" if str(src or "").strip() else paragraph


def _append_existing_post_phone_line(base: str, phone_line: str) -> str:
    base = _trim_post_body(base, POST_BODY_MAX)
    phone_line = str(phone_line or "").strip()
    if not phone_line:
        return base
    sep = "\n\n" if base else ""
    if len(base) + len(sep) + len(phone_line) <= POST_BODY_MAX:
        return _finalize_post_markup(f"{base}{sep}{phone_line}")
    room = POST_BODY_MAX - len(sep) - len(phone_line)
    if room <= 0:
        return phone_line[:POST_BODY_MAX]
    prefix = _trim_post_body(base, room)
    return _finalize_post_markup(f"{prefix}{sep}{phone_line}")


def _normalize_post_body_structure(text: str) -> str:
    text = _normalize_post_language(text)
    text = _remove_empty_post_inventory_sections(text)
    text = _dedupe_post_benefit_lines(text)
    return _collapse_post_blank_lines(_finalize_post_markup(text))


def _ensure_post_phone_last(text: str) -> str:
    before, phone_line, tail = _split_post_phone_line(text)
    if not phone_line:
        return _normalize_post_body_structure(text)
    before = _normalize_post_body_structure(before)
    tail = _normalize_post_body_structure(tail)
    if tail and _should_keep_post_tail(before, tail):
        candidate = _insert_post_paragraph_before_cta(before, tail)
        candidate = _normalize_post_body_structure(candidate)
        if len(candidate) + len("\n\n") + len(phone_line) <= POST_BODY_MAX:
            before = candidate
    return _append_existing_post_phone_line(before, phone_line)


def _strip_dangling_post_tail(text: str) -> str:
    """Drop CTA fragments that became incomplete after LLM generation/trimming."""
    text = str(text or "").rstrip()
    text = re.sub(
        r"(?is)([.!?…])\s+(?:не\s+упустите|успейте|спешите|оставьте\s+заявку)\b[^.!?…]*$",
        r"\1",
        text,
    ).rstrip()
    lines = text.splitlines()
    last_idx = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None)
    if last_idx is not None:
        last = lines[last_idx].strip()
        last_plain = re.sub(r":(?:b|bb|i|ii|s|ss):", "", last).strip()
        if last_plain.endswith(":") and "Подробности по телефону" not in last:
            candidate = "\n".join(lines[:last_idx]).rstrip()
            if len(candidate) >= 20:
                text = candidate
                lines = text.splitlines()
                last_idx = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None)
                last = lines[last_idx].strip() if last_idx is not None else ""
        if (
            "Подробности по телефону" not in last
            and not re.search(r"[.!?…:]$", last)
            and re.search(r"(?i)\b(оставьте|перезвоним|зафиксируем|не\s+упустите|спешите|успейте)\b", last)
        ):
            candidate = "\n".join(lines[:last_idx]).rstrip()
            if len(candidate) >= 80:
                text = candidate
    text = re.sub(
        r"(?i)(?:\s+(?:и\s+)?(?:перезвоним|зафиксируем)(?:\s+\S+){0,6})$",
        "",
        text,
    ).rstrip()
    text = re.sub(
        r"(?i)(?:\s+(?:в|во|на|и|с|со|за|для|по|до|от|без|при|о|об|к|ко|не|мы|вы|уже|сейчас))+$",
        "",
        text,
    )
    return text.rstrip(" \t\n,;—-")


def _trim_post_body(text: str, max_len: int = POST_BODY_MAX) -> str:
    """Trim post body without leaving a dangling word, phrase, or broken markup."""
    text = _strip_dangling_post_tail(text)
    if len(text) <= max_len:
        return _finalize_post_markup(text)
    prefix = text[:max_len].rstrip()
    min_keep = max(80, int(max_len * 0.62))
    sentence_ends = [m.end() for m in re.finditer(r"[.!?…](?=\s|$)", prefix)]
    paragraph_ends = [m.start() for m in re.finditer(r"\n\n", prefix)]
    candidates = [pos for pos in sentence_ends + paragraph_ends if pos >= min_keep]
    if candidates:
        prefix = prefix[:max(candidates)].rstrip()
    else:
        lines = prefix.splitlines()
        while len(lines) > 1 and lines[-1].strip() and not re.search(r"[.!?…:]$", lines[-1].strip()):
            candidate = "\n".join(lines[:-1]).rstrip()
            if len(candidate) < min_keep:
                break
            lines = lines[:-1]
            prefix = candidate
        cut = prefix.rfind(" ")
        if cut > min_keep:
            prefix = prefix[:cut].rstrip()
    return _finalize_post_markup(_strip_dangling_post_tail(prefix))


def _trim_post_body_preserve_phone(text: str, max_len: int = POST_BODY_MAX) -> str:
    """Trim body while preserving an existing phone line as the final line."""
    before, phone_line, tail = _split_post_phone_line(text)
    if not phone_line:
        return _trim_post_body(text, max_len)
    base = _normalize_post_body_structure(before)
    if tail and _should_keep_post_tail(base, tail):
        candidate = _normalize_post_body_structure(_insert_post_paragraph_before_cta(base, tail))
        if len(candidate) + len("\n\n") + len(phone_line) <= max_len:
            base = candidate
    return _append_existing_post_phone_line(base, phone_line)


def _remove_post_site_mentions(text: str, href: str = "") -> str:
    """Remove website/domain mentions from post copy.

    The URL still goes into the button href; post body should not spell out the
    website or domain.
    """
    text = str(text or "")
    netloc = urllib.parse.urlparse(href or "").netloc.lower()
    domains = {netloc, netloc.removeprefix("www.")} if netloc else set()
    for domain in [d for d in domains if d]:
        text = re.sub(
            rf"(?i)\b(?:https?://)?(?:www\.)?{re.escape(domain)}(?:/[^\s]*)?",
            "",
            text,
        )
    text = re.sub(r"(?i)\b(?:https?://)?(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/[^\s]*)?", "", text)
    text = re.sub(r"(?i)\bперей(?:ти|дите)\s+на\s+сайт\b", "оставить заявку", text)
    text = re.sub(r"(?i)\bна\s+сайт(?:е|)\b", "по кнопке", text)
    text = re.sub(r"(?i)\bсайт(?:е|а|у|ом)?\b", "", text)
    text = re.sub(r"[ \t]+([,.;!?]|:(?!(?:b|bb|i|ii|s|ss):))", r"\1", text)
    text = re.sub(r"([—-])\s*([—-])+", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(" \t\n,.;—-")


def _ensure_post_highlights(text: str, brand_label: str = "") -> str:
    """Add conservative bold highlights for model lines and UTP lines."""
    text = str(text or "")
    utp_re = re.compile(
        r"(?i)\b(КАСКО|трейд-?ин|перв(?:ый|ого)\s+взнос|одобрени[ея]|платеж(?:а|ей|и)?|"
        r"подар(?:ок|ки)|шин[ыа]?|резин[аы]|кредит|без\s+переплат)\b"
    )
    brand_words = [w for w in re.split(r"\s+", brand_label or "") if len(w) > 2 and brand_label != "Посевы"]
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or ":b:" in stripped:
            out.append(line)
            continue
        model_match = re.match(r"^([•\-—]?\s*[A-ZА-ЯЁ][\wА-Яа-яЁё-]*(?:\s+[A-ZА-ЯЁ0-9][\wА-Яа-яЁё./-]*){1,5})(\s+[—-]\s+.+)$", stripped)
        if model_match and (re.search(r"\d", model_match.group(2)) or any(w in stripped for w in brand_words)):
            prefix = line[:len(line) - len(line.lstrip())]
            out.append(f"{prefix}:b:{model_match.group(1).strip()}:bb:{model_match.group(2)}")
            continue
        if stripped.startswith(("—", "-")) and utp_re.search(stripped):
            prefix = line[:len(line) - len(line.lstrip())]
            marker = stripped[0]
            rest = stripped[1:].strip()
            out.append(f"{prefix}{marker} :b:{rest}:bb:")
            continue
        out.append(line)
    highlighted = "\n".join(out)
    if brand_words and ":b:" not in highlighted:
        pattern = re.compile(rf"\b({re.escape(brand_words[0])}(?:\s+[A-ZА-ЯЁ0-9][\wА-Яа-яЁё./-]*)?)\b")
        highlighted = pattern.sub(r":b:\1:bb:", highlighted, count=1)
    return highlighted


# ── Детерминированная нормализация текста посевов на выходе (правила 1-11) ──────
# Промпт генерации НЕ трогаем — только постобработка готового LLM-текста.

_NORM_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF"   # Misc Symbols and Pictographs, Emoticons, Transport...
    "\U00002600-\U000027BF"    # Misc Symbols (☀ ✅ ⚡ ✔ ⚠ ❤ etc.)
    "\U0001F1E6-\U0001F1FF"    # Regional Indicator symbols (flags)
    "\U00002190-\U000021FF"    # Arrows
    "\U00002B00-\U00002BFF"    # Misc Symbols and Arrows
    "︀-️"            # Variation Selectors 1-16 (incl. U+FE0F)
    "‍"                   # Zero Width Joiner (ZWJ-последовательности)
    "❤"                   # Heavy Black Heart ❤ (U+2764)
    "]+",
    re.UNICODE,
)
_NORM_MARKUP_START_RE = re.compile(r"^:(?:b|bb|i|ii|s|ss):")
_NORM_BULLET_RE = re.compile(r"^(?:[-—–•*])\s*")
_NORM_STEP_RE = re.compile(r"^(?:->|-->|→|=>)\s*")


def _norm_apply_currency(line: str) -> str:
    """Rules 3+4: валюта/процент/период платежа."""
    # Rule 3a: пробел перед ₽ (500 000₽ → 500 000 ₽)
    line = re.sub(r"(\d)\s*₽", r"\1 ₽", line)
    # Rule 3b: процент прижат к числу (30 % → 30%)
    line = re.sub(r"(\d)\s+%", r"\1%", line)
    # Rule 4: период платежа → ₽/мес без пробелов
    line = re.sub(r"₽\s*(?:/\s*мес(?:яц)?|в\s+месяц)\b", "₽/мес", line)
    return line


def _normalize_post_body_line(line: str) -> str:
    """Per-line нормализация правилами 1-9."""
    stripped_raw = line.strip()
    if not stripped_raw:
        return ""
    # Строки, начинающиеся с markup-токена — НЕ трогаем как буллет/шаг
    starts_with_markup = bool(_NORM_MARKUP_START_RE.match(stripped_raw))
    # Проверяем шаг/буллет ДО снятия эмодзи: символы → и • входят в emoji-диапазоны
    # (U+2192 Arrow, U+2022 Bullet), и их нужно распознать как маркеры раньше очистки.
    step_m = None if starts_with_markup else _NORM_STEP_RE.match(stripped_raw)
    bullet_m = None if starts_with_markup else _NORM_BULLET_RE.match(stripped_raw)
    if step_m:
        # Rules 6, 7, 8: шаг → "-> Тело"
        body = stripped_raw[step_m.end():]
        body = _NORM_EMOJI_RE.sub("", body).lstrip()   # Rule 1: emoji из тела
        body = re.sub(r"[ \t]{2,}", " ", body)          # Rule 2
        body = _norm_apply_currency(body)
        body = body.rstrip(".,;")                        # Rule 8
        body = (body[:1].upper() + body[1:]) if body else body  # Rule 7
        return f"-> {body}"
    if bullet_m:
        # Rules 5, 7, 8: буллет → "— Тело"
        body = stripped_raw[bullet_m.end():]
        body = _NORM_EMOJI_RE.sub("", body).lstrip()   # Rule 1: emoji из тела
        body = re.sub(r"[ \t]{2,}", " ", body)          # Rule 2
        body = _norm_apply_currency(body)
        body = body.rstrip(".,;")                        # Rule 8
        body = (body[:1].upper() + body[1:]) if body else body  # Rule 7
        return f"— {body}"
    # Обычная строка / заголовок блока
    # Rule 1: убрать эмодзи, затем ведущий пробел
    line = _NORM_EMOJI_RE.sub("", stripped_raw).lstrip()
    # Rule 2: схлопнуть 2+ пробелов/табов
    line = re.sub(r"[ \t]{2,}", " ", line)
    if not line.strip():
        return ""
    line = _norm_apply_currency(line.strip())
    # Rule 9: убрать пробел перед : (кроме markup-токенов :b: :bb: :i: :ii: :s: :ss:)
    line = re.sub(r"\s+:(?!(?:b|bb|i|ii|s|ss):)", ":", line)
    return line


def normalize_post_body_text(text: str) -> str:
    """Детерминированная нормализация тела поста посева (правила 1-11).

    Вызывается ПЕРВОЙ в цепочке постобработки — до обрезки по POST_BODY_MAX,
    чтобы эмодзи и двойные пробелы не съедали лимит.
    Только форматирование: не меняет смысл, порядок блоков и markup-токены.
    """
    lines = [_normalize_post_body_line(ln) for ln in str(text or "").splitlines()]
    # Rule 10: удалить строку «Подробности по телефону» без цифр (оборванный хвост)
    out_lines = [
        ln for ln in lines
        if not (
            ln.strip().lower().startswith("подробности по телефону")
            and not re.search(r"\d{5,}", ln)
        )
    ]
    result = "\n".join(out_lines)
    # Rule 11: 3+ переводов строки → 2, обрезать края
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def _prepare_post_body(text: str, href: str = "", brand_label: str = "") -> str:
    """Final body sanitizer before AddPostAds."""
    text = normalize_post_body_text(text)  # нормализация ПЕРВОЙ, до обрезки
    text = _remove_post_site_mentions(text, href)
    text = _ensure_post_highlights(text, brand_label)
    text = _normalize_post_body_structure(text)
    return _ensure_post_phone_last(text)


def _normalize_phone_candidate(raw: str, context: str = "") -> str:
    digits = re.sub(r"\D+", "", urllib.parse.unquote(str(raw or "")))
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) != 11 or not digits.startswith("7"):
        return ""
    if digits in {"79000000000", "79999999999", "71111111111", "70000000000"}:
        return ""
    if re.search(r"(?i)\b(?:placeholder|js-phone-mask|name=['\"]telephone|маск[аи]|пример)\b", context or ""):
        return ""
    return "+" + digits


def _extract_phone_from_html(page_html: str) -> str:
    """Return the first real phone from tel-href or visible page text."""
    src = html.unescape(str(page_html or ""))
    for m in re.finditer(r'''(?is)(?:href\s*=\s*["']\s*)?tel:\s*([^"'\s<>]+)''', src):
        phone = _normalize_phone_candidate(m.group(1), src[max(0, m.start() - 80):m.end() + 80])
        if phone:
            return phone

    visible = re.sub(r"(?is)<(?:script|style)\b[^>]*>.*?</(?:script|style)>", " ", src)
    visible = re.sub(r"(?is)<input\b[^>]*>", " ", visible)
    visible = re.sub(r"(?is)<[^>]+>", " ", visible)
    visible = re.sub(r"\s+", " ", visible)
    for m in re.finditer(r"(?:\+7|8)\s*(?:\(\s*\d{3}\s*\)|\d{3})\s*\d{3}[\s-]*\d{2}[\s-]*\d{2}", visible):
        phone = _normalize_phone_candidate(m.group(0), visible[max(0, m.start() - 80):m.end() + 80])
        if phone:
            return phone
    return ""


def _decode_phone_page_response(resp) -> str:
    raw = resp.read(700_000)
    enc = (resp.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip":
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        raw = zlib.decompress(raw)
    return raw.decode(resp.headers.get_content_charset() or "utf-8", "ignore")


def _fetch_phone_page_html(fetch_url: str) -> str:
    try:
        import requests  # noqa: PLC0415
        session = requests.Session()
        session.trust_env = False
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            r = session.get(
                fetch_url,
                headers={"User-Agent": _PHONE_FETCH_UA},
                timeout=(3, 5),
                verify=False,
            )
        if r.text:
            return r.text
    except Exception:  # noqa: BLE001 - curl fallback below
        pass

    try:
        r = subprocess.run(
            [
                "curl", "-k", "-L", "--connect-timeout", "3", "--max-time", "5",
                "-A", _PHONE_FETCH_UA, "-sS", fetch_url,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
            timeout=7,
        )
        if r.stdout:
            return r.stdout
    except Exception:  # noqa: BLE001 - urllib fallback below
        pass

    req = urllib.request.Request(
        fetch_url,
        headers={"Accept-Encoding": "identity", "User-Agent": _PHONE_FETCH_UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:  # noqa: S310 - user-owned landing page
            return _decode_phone_page_response(resp)
    except urllib.error.HTTPError as e:
        try:
            return _decode_phone_page_response(e)
        except Exception:  # noqa: BLE001 - fail-open without phone
            pass
    except Exception:  # noqa: BLE001 - fail-open without phone
        pass
    return ""


def _phone_from_site(href: str) -> str:
    """Read the landing page and return the first phone as +digits."""
    parsed = urllib.parse.urlparse(href or "")
    if not parsed.scheme or not parsed.netloc:
        return ""
    cache_key = parsed.netloc.lower()
    if cache_key in _PHONE_CACHE:
        return _PHONE_CACHE[cache_key]
    alt_scheme = "http" if parsed.scheme == "https" else "https"
    paths = []
    original_path = parsed.path or "/"
    path_order = (original_path, "/", "/auto", "/contacts", "/credit")
    if original_path == "/":
        path_order = ("/auto", "/contacts", "/credit", "/")
    for path in path_order:
        if path not in paths:
            paths.append(path)
    urls = []
    for scheme in (parsed.scheme, alt_scheme):
        for path in paths:
            url = urllib.parse.urlunparse((scheme, parsed.netloc, path, "", "", ""))
            if url not in urls:
                urls.append(url)
    phone = ""
    for fetch_url in urls:
        page_html = _fetch_phone_page_html(fetch_url)
        phone = _extract_phone_from_html(page_html)
        if phone:
            break
    _PHONE_CACHE[cache_key] = phone
    return phone


def _norm_brand_text(text: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", " ", str(text or "").lower()).strip()


def _brand_label_matches(label: str, ref: str) -> bool:
    """Loose match for brand/model label against coder/site reference."""
    lab = _norm_brand_text(label)
    target = _norm_brand_text(ref)
    if not lab or not target:
        return False
    if lab == target or target.startswith(lab + " ") or lab.startswith(target + " "):
        return True
    lab_first = lab.split()[0] if lab.split() else ""
    target_first = target.split()[0] if target.split() else ""
    return bool(lab_first and len(lab_first) >= 3 and lab_first == target_first)


def _coder_brand_for_ct(ct: str) -> str:
    """Return real brand/model name from ag_part1 for post ct, if ct is a real auto entity."""
    ctn = str(ct or "").strip().lower()
    if not re.fullmatch(r"ct\d{4}", ctn) or ctn == "ct0000":
        return ""
    try:
        from .campaign_naming import _ag_part1_map, _coder_name_real_brand  # noqa: PLC0415
    except ImportError:
        from campaign_naming import _ag_part1_map, _coder_name_real_brand  # type: ignore[no-redef]  # noqa: PLC0415
    try:
        name = str((_ag_part1_map() or {}).get(ctn) or "").strip()
        if name and _coder_name_real_brand(name):
            return name
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _site_text_for_brand_guard(href: str) -> str:
    parsed = urllib.parse.urlparse(href or "")
    if not parsed.scheme or not parsed.netloc:
        return ""
    cache_key = f"{parsed.scheme}://{parsed.netloc}".lower()
    if cache_key in _SITE_TEXT_CACHE:
        return _SITE_TEXT_CACHE[cache_key]
    paths: list[str] = []
    original_path = parsed.path or "/"
    for path in (original_path, "/", "/auto", "/catalog", "/cars", "/used", "/contacts"):
        if path not in paths:
            paths.append(path)
    chunks: list[str] = []
    for path in paths:
        url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
        page_html = _fetch_phone_page_html(url)
        if page_html:
            visible = re.sub(r"(?is)<(?:script|style)\b[^>]*>.*?</(?:script|style)>", " ", page_html)
            visible = re.sub(r"(?is)<[^>]+>", " ", html.unescape(visible))
            chunks.append(visible)
        if sum(len(x) for x in chunks) >= 500_000:
            break
    text = re.sub(r"\s+", " ", " ".join(chunks)).strip().lower()
    _SITE_TEXT_CACHE[cache_key] = text
    return text


def _site_mentions_brand(href: str, brand_label: str) -> bool:
    text = _site_text_for_brand_guard(href)
    if not text:
        return False
    words = [w for w in _norm_brand_text(brand_label).split() if len(w) >= 2]
    return bool(words) and all(re.search(rf"(?<![a-zа-яё0-9]){re.escape(w)}(?![a-zа-яё0-9])", text)
                               for w in words)


def _safe_post_brand_label(brand_label: str, ct: str, href: str) -> str:
    """Allow post brand only when it is backed by coder ct or visible site text."""
    label = re.sub(r"\s+", " ", str(brand_label or "").strip())
    if not label or label.lower() == "посевы":
        return "Посевы"
    coder_brand = _coder_brand_for_ct(ct)
    if coder_brand and _brand_label_matches(label, coder_brand):
        return coder_brand
    if _site_mentions_brand(href, label):
        return label
    print(
        f"[post-engine] brand_label {label!r} is not present in coder ct={ct!r} or site; "
        "fallback to generic Посевы",
        flush=True,
    )
    return "Посевы"


def _sanitize_post_brand_content(title: str, body: str, brand_label: str) -> tuple[str, str]:
    """Drop foreign auto brands from a branded post ad and keep own brand in title."""
    brand = "" if str(brand_label or "").lower() == "посевы" else str(brand_label or "").strip()
    if not brand:
        return title, body
    try:
        from .text_gen import _brand_in_text, _drop_foreign_brand_mentions  # noqa: PLC0415
    except ImportError:
        from text_gen import _brand_in_text, _drop_foreign_brand_mentions  # type: ignore[no-redef]  # noqa: PLC0415
    title_rows = _drop_foreign_brand_mentions([title], brand)
    clean_title = (title_rows[0] if title_rows else "").strip()
    if not clean_title or not _brand_in_text(clean_title, brand):
        clean_title = f"{brand} в кредит. Первый взнос 0 ₽"
    body_rows = _drop_foreign_brand_mentions(str(body or "").splitlines(), brand)
    clean_body = "\n".join(body_rows).strip()
    return clean_title, clean_body or body


def _append_post_phone_line(text: str, href: str = "") -> str:
    """Append required phone line and still use body room if phone is unavailable."""
    phone = _phone_from_site(href)
    line = f"Подробности по телефону: {phone}" if phone else ""
    existing_base, existing_phone, existing_tail = _split_post_phone_line(text)
    base = _normalize_post_body_structure(existing_base if existing_phone else text)
    if existing_tail and _should_keep_post_tail(base, existing_tail):
        candidate = _normalize_post_body_structure(_insert_post_paragraph_before_cta(base, existing_tail))
        if len(candidate) + (len("\n\n") + len(line or existing_phone) if (line or existing_phone) else 0) <= POST_BODY_MAX:
            base = candidate
    base = _expand_post_body_before_phone(base, line)
    if not phone:
        return _ensure_post_phone_last(base)
    return _append_existing_post_phone_line(base, line)


def _expand_post_body_before_phone(text: str, phone_line: str) -> str:
    """Use meaningful free body room before the required phone line."""
    base = _trim_post_body(text)
    def _reserved_len(src: str) -> int:
        if not phone_line:
            return 0
        return len(("\n\n" if src else "") + phone_line)

    if POST_BODY_MAX - (len(base) + _reserved_len(base)) < _POST_BODY_TARGET_FREE:
        return base

    existing_topics = {_post_benefit_topic(line) for line in base.splitlines()}
    existing_topics.discard("")
    for paragraph in _POST_BODY_FILLERS:
        if paragraph.lower() in base.lower():
            continue
        topic = _post_benefit_topic(paragraph)
        if topic and topic in existing_topics:
            continue
        candidate = _normalize_post_body_structure(_insert_post_paragraph_before_cta(base, paragraph))
        if len(candidate) + _reserved_len(candidate) <= POST_BODY_MAX:
            base = candidate
            if topic:
                existing_topics.add(topic)
        if POST_BODY_MAX - (len(base) + _reserved_len(base)) < _POST_BODY_TARGET_FREE:
            break
    return _trim_post_body(base)


def _build_platforms(tp_code: str) -> dict:
    """Все ~17 platform-флагов для AddCampaigns; специфичные для tp устанавливаются в True."""
    base = {k: False for k in _ALL_PLATFORM_KEYS}
    base.update(_TP_PLATFORMS.get(tp_code, {}))
    return base


def _campaign_name(tp_code: str, ct: str, r_code: str, oblast: str,
                   brand_label: str = "Посевы", ag_code: str = _AG_CODE,
                   g_code: str = _G_CODE) -> str:
    """Имя кампании по кодеру (SPEC §4.1).

    Формат: tp8_cpc_site_{ct}_aon_n000_{r_code}_ct018_{ag_code}_{g_code} — {brand_label} Telegram - {oblast}
    brand_label: "Посевы" (мультибренд, дефолт), "Tenet", "Lada", "Haval" (монобренд).
    ag_code: "ag001" (Все) / "ag011" (возрастная/гендерная корректировка).
    """
    _tp_labels = {"tp8": "Telegram", "tp9": "Max", "tp10": "Telegram+Max"}
    label = _tp_labels.get(tp_code, tp_code.upper())
    codes = f"{tp_code}_{_PAY_CODE}_{_SQ_CODE}_{ct}_{_AUD_CODE}_{_N_CODE}_{r_code}_{_FMT_CODE}_{ag_code}_{g_code}"
    return f"{codes} — {brand_label} {label} - {oblast}"


def _group_name(ct: str, r_code: str, idx: int, brand_label: str = "Посевы",
                ag_code: str = _AG_CODE, g_code: str = _G_CODE) -> str:
    """Имя группы объявлений по кодеру (SPEC §2.12, live-test §9.1).

    Формат (1:1 с _tp1_group_name):
    {ct}_aon_n000_{r_code}_ct018_{ag_code}_{g_code} — {brand_label} v{idx+1}

    ag_code: "ag001" (Все) / "ag011" (возрастная/гендерная корректировка).
    """
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
    raw_brand_label = re.sub(r"\s+", " ", str(brand_label or "").strip())
    brand_label = _safe_post_brand_label(raw_brand_label, ct, href)
    feed_url_map = _post_feed_url_map(login, href)
    if (
        raw_brand_label
        and raw_brand_label.lower() != "посевы"
        and brand_label == "Посевы"
        and _post_feed_url_for_label(feed_url_map, raw_brand_label)
    ):
        brand_label = raw_brand_label
    post_href = _post_href_for_label(login, href, brand_label, ct=ct, site_type=site_type)
    allowed_models = _post_allowed_models_from_feed(login, href, brand_label)

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

    image_hashes: list[str] = [
        str(h).strip() for h in (it.get("preloaded_post_image_hashes") or []) if str(h).strip()
    ]
    if not image_hashes:
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
            _domain = urllib.parse.urlparse(post_href or href or "").netloc or ""
            # brand_label: если "Посевы" (дефолт) → нет конкретного бренда → передаём ""
            _brand = brand_label if (brand_label and brand_label != "Посевы") else ""
            _content = _gpac(
                slepok=slepok,
                site_type=site_type or "Монобренд",
                brand=_brand,
                city=oblast or "",
                domain=_domain,
                avoid=avoid or [],
                allowed_models=allowed_models,
            )
            if not title_text:
                title_text = _content.get("title", "")
            if not body_text:
                body_text = _content.get("body", "")
        except Exception as _ce:  # noqa: BLE001
            print(f"[post-engine] {login} {tp_code}: generate_post_ad_content failed: {_ce!s:.120}",
                  flush=True)
    title_text, body_text = _sanitize_post_brand_content(title_text, body_text, brand_label)

    # Финальная страховка лимитов (generate_post_ad_content их уже применяет; fallback — ниже)
    if not title_text:
        title_text = "Широкий выбор автомобилей"
    if not body_text:
        body_text = "Узнайте актуальные предложения и запишитесь на тест-драйв."
    body_text = _prepare_post_body(body_text, post_href, brand_label)
    # Детерминированный блок марочных оферов: заменяет LLM-сгенерированный список
    # (гасит дубль на стороне сборки — промпт не трогаем).
    _brand_block = _build_brand_offer_block(login, post_href, brand_label, ct)
    if _brand_block:
        body_text = _replace_post_model_list(body_text, _brand_block)
    body_text = _append_post_phone_line(body_text, post_href)
    title_text = title_text[:POST_TITLE_MAX]
    body_text = _trim_post_body_preserve_phone(body_text, POST_BODY_MAX)
    body_text = _finalize_post_markup(body_text)
    body_text = _extend_post_body_after_finalize(body_text, brand_label, post_href)
    body_text = _finalize_post_markup(_trim_post_body_preserve_phone(body_text, POST_BODY_MAX))

    # ── dem_adj — ДО AddCampaigns (нужны для useBidModifiers) ──
    # _bid_mod_dem строится ПОСЛЕ AddCampaigns: требует campaignId (NonNull в Grid-схеме).
    dem_adj = _dem_adjustments_for_corr(corr)
    # Имена Посевов должны совпадать со структурой `direct/slepki/posevy.json` и CODER.md:
    # POST-семья кодируется как ct018_ag001_g00. Демографические bid modifiers применяются
    # отдельно и не переписывают structural name/code.
    ag_code = _AG_CODE
    g_code = _G_CODE

    # ── Шаг 1: AddCampaigns ────────────────────────────────────────────────
    platforms = _build_platforms(tp_code)
    disabled_places = _post_disabled_places_for_geo(oblast)
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
                    "disabledPlaces": disabled_places,
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
        grp_name = _group_name(ct, r_code, idx, brand_label, ag_code, g_code)
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
    if len(ad_group_ids) != n_groups:
        return _fail(
            f"AddPostAdGroups: недобор групп {len(ad_group_ids)}/{n_groups}",
            campaign_id=campaign_id,
            ad_group_ids=ad_group_ids,
            partial=True,
            build={"adgroups": n_groups, "ads": n_groups},
        )

    # ── Шаг 3: AddPostAds × len(ad_group_ids) ─────────────────────────────
    ad_items = []
    for gidx, gid in enumerate(ad_group_ids):
        multicard = []
        if gidx < len(image_hashes):
            multicard = [{"imageHash": image_hashes[gidx]}]
        ad_items.append({
            "adGroupId":      str(gid),
            "href":           post_href,
            "domain":         None,
            "body":           body_text,
            "title":          title_text,
            "titleExtension": None,
            "creativeId":     None,
            "button":         {"action": POST_DEFAULT_BUTTON, "href": post_href},
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
        if len(ad_ids) != len(ad_items):
            return _fail(
                f"AddPostAds: недобор объявлений {len(ad_ids)}/{len(ad_items)}",
                campaign_id=campaign_id,
                ad_group_ids=ad_group_ids,
                ad_ids=ad_ids,
                partial=True,
                build={"adgroups": len(ad_group_ids), "ads": len(ad_items)},
            )
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
        "build":        {"adgroups": len(ad_group_ids), "ads": len(ad_ids)},
        "images_used":  len(image_hashes),
        "save_draft":   save_draft,
        "title":        title_text,   # для накопления avoid между tp8/9/10 одного набора
    }]

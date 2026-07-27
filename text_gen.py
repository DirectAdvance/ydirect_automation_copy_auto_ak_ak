"""Генерация текстов/заголовков объявлений (RSYA + Master) — вынесено из blueprint.py.

Чистое ядро text-shaping: пулы промо/текстов, ротация Title2, дедуп/варьирование,
дропы бренд/модель-ключей, когерентность скидок/платежей (вкл. _bad_credit_payment_range —
источник для инъекции в text_norm), фолбэк-заголовки.

Инвариант wiring-hub: НЕ импортирует blueprint. Sibling-модули (text_norm/city_morph/
campaign_naming/kontent_pack) импортируются напрямую; blueprint-зависимости — через configure().
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_HERE_TG = Path(__file__).resolve().parent

from .text_norm import (
    _replace_emdash, _replace_sep_hyphen, _is_bad_start, _trim_to_word,
    _trim_clean, _sanitize_content,
    _normalize_numeric_suffixes_bp,
    _strip_credit_rate, _cap_first, _sentence_case, _RSYA_TEXT_MAX, _split_utp,
    _has_stamp, _alternate_rhythm, _dedup_by_first_word, _has_number,
    _bad_ad_title, _bad_ad_text,
)
from .city_morph import (
    _city_locative, _content_city, _replace_foreign_city, _drop_foreign_city_keywords,
)
from .campaign_naming import _ag_part1_map, _title2_blocklist
from . import kontent_pack as kp


# ── DI: инъектятся из blueprint (заглушки/None — падают громко, если configure не отработал) ──
def _drop_used_car(*a, **k):
    raise RuntimeError("text_gen._drop_used_car не инъектирован (configure)")


def _drop_new_car(*a, **k):
    raise RuntimeError("text_gen._drop_new_car не инъектирован (configure)")


def _is_bu_site(*a, **k):
    raise RuntimeError("text_gen._is_bu_site не инъектирован (configure)")


def _brand_canon(*a, **k):
    raise RuntimeError("text_gen._brand_canon не инъектирован (configure)")


def _ct_segment(*a, **k):
    raise RuntimeError("text_gen._ct_segment не инъектирован (configure)")


_GENERIC_TITLE_FILLERS: list = []   # DI из blueprint
_GENERIC_AT_TITLES: list = []       # DI из blueprint
_RA_TITLES_CAP: int = 7             # DI из blueprint
_RA_TEXTS_CAP: int = 3              # DI из blueprint
_NON_AUTO_SITE_TYPES: set = set()   # DI из blueprint: site_type не-авто слепков (B2B) — без авто-фильтра ключей
_INSTALLMENT_RE = re.compile(r"(?i)\bрассрочк\w*\b(?:\s+без\s+переплат)?")


def configure(deps: dict) -> None:
    """Инъекция blueprint-зависимостей (globals().update)."""
    globals().update(deps)


def _strip_installment_text(s: str) -> str:
    s = _INSTALLMENT_RE.sub(" ", s or "")
    s = re.sub(r"\s+([.,;:!?])", r"\1", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" .,;:!?")
    return s


# Акционные фразы для шаблона Title (ротация round-robin при формировании групп).
_TITLE_PROMO_POOL = [
    "Распродаем -45%",
    "Выгода до 45%",
    "Скидки на новые авто",
    "Специальные условия",
]

_TITLE_PROMO_IDX = 0

def _next_title_promo() -> str:
    """Следующая акционная фраза из пула (round-robin)."""
    global _TITLE_PROMO_IDX
    phrase = _TITLE_PROMO_POOL[_TITLE_PROMO_IDX % len(_TITLE_PROMO_POOL)]
    _TITLE_PROMO_IDX += 1
    return phrase

# Словарь предложного падежа для реальных городов аккаунтов (direction='Авто').
# Несклоняемые (Кемерово, Тольятти) — стоят как есть. Составные (Нижний Новгород,
# Ростов-на-Дону, Южно-Сахалинск, Санкт-Петербург) — прописаны явно.
# Источник: SELECT DISTINCT city FROM local_gsheet_sites WHERE direction='Авто' (2026-06-22).

# Слепки, для которых город НЕ вставляется в шаблонные заголовки (template-based).
# Влияет на _title_from_template и _rsya_titles/_brand_title_set (via slepok= param).
# Решение Семёна #15, 2026-07-11. Город всё равно передаётся в LLM-промпт (Город: city),
# но явные шаблоны «{brand} в {city_loc}» для этих слепков строятся без города.
_SLEPOKS_NO_CITY_TITLES: frozenset[str] = frozenset({"terehov"})


def _title_from_template(brand: str, city: str = "", slepok: str = "", site_type: str = "") -> str:
    """Сформировать Title по эталонному шаблону «Новые {brand} в {город}. {акция}».
    Лимит Директа для ЕПК TextAd — 35 символов (поле Title). Обрезаем аккуратно.

    Если brand без пробела (просто марка «BAIC») — «Новые BAIC в Краснодаре. Выгода до 45%».
    Если brand с пробелом (марка+модель «BAIC BJ40») — «Новые BAIC BJ40 в Краснодаре. …».
    city — город из аккаунта (ctx.city); пустой → без города («Новые {brand}. {акция}»).
    Город подставляется в предложном падеже через _city_locative().
    Фолбэк: если шаблон не влезает даже без акции — возвращаем brand[:35].

    site_type «С пробегом» (Б/У) — префикс «Новые» ВРЁТ про товар, поэтому заменяется
    нейтральным «{brand} с пробегом …» (не влез — просто «{brand} …»), а из промо-пула
    выбрасывается «Скидки на новые авто». Фильтровать это `_drop_new_car`/`_NEW_RE` НЕЛЬЗЯ:
    (а) результат уходит прямо в ЕПК Title мимо `_cf`; (б) «Новые BAIC» регулярка и не поймает —
    после «новые» идёт МАРКА, а не слово «авто» (расширять регулярку на `нов\\w+\\s+[A-Z]`
    нельзя — она начнёт резать легитимное «Новый Haval» на сайтах НОВЫХ авто).
    """
    if slepok in _SLEPOKS_NO_CITY_TITLES:                 # terehov: город в заголовках запрещён (#15)
        city = ""
    city = _content_city(city)                            # мультигород (через запятую) → без города
    city_loc = _city_locative(city) if city else ""
    bu = _is_bu_site(site_type)
    promo = _next_title_promo()
    if bu:
        # Промо-пул писан под новые авто («Скидки на новые авто»): на Б/У прокручиваем
        # round-robin до первой строки без новое-авто-лексики. Не нашлось — идём без акции.
        for _ in range(len(_TITLE_PROMO_POOL)):
            if _drop_new_car([promo], site_type):
                break
            promo = _next_title_promo()
        else:
            promo = ""
    lead = f"{brand} с пробегом" if bu else f"Новые {brand}"
    if promo:
        full = f"{lead} в {city_loc}. {promo}" if city_loc else f"{lead}. {promo}"
    else:
        full = f"{lead} в {city_loc}" if city_loc else lead
    if len(full) <= 35:
        return full
    # Попробуем без акционной фразы
    short = f"{lead} в {city_loc}" if city_loc else lead
    if len(short) <= 35:
        return short[:35]
    # Б/У: «{brand} с пробегом …» не влез — снимаем уточнение, но НЕ возвращаем «Новые …»
    if bu:
        bare = f"{brand} в {city_loc}" if city_loc else str(brand)
        if len(bare) <= 35:
            return bare[:35]
    # Фолбэк: просто бренд
    return brand[:35]

_RUB_DISC_RE = re.compile(r"(\d[\d\s ]*\d|\d)\s*(₽|руб\.?)", re.IGNORECASE)

_PCT_DISC_RE = re.compile(r"(\d{1,3})\s*%")

_RUB_DISCOUNT_CTX_RE = re.compile(r"(?i)(скидк|выгод|господдерж|госпрограмм|подар)")

def _fmt_thousands(digits: str) -> str:
    """'890000' → '890 000' (узкий неразрывный пробел как разделитель тысяч, как в Директе)."""
    d = re.sub(r"\D", "", digits or "")
    if not d:
        return digits
    out = []
    for i, ch in enumerate(reversed(d)):
        if i and i % 3 == 0:
            out.append(" ")
        out.append(ch)
    return "".join(reversed(out))

def _coherent_discounts(titles: list, texts: list) -> tuple:
    """Согласовать скидки/выгоды в заголовках и текстах ОДНОЙ кампании: одно ₽-число и один %-число
    на всю кампанию (эталон = САМОЕ ЧАСТОЕ значение в контенте — без выдумывания новых цифр).
    Лечит #6 (в заголовке X% → и в тексте X%) и #5 (890/860 «выгода» → единое число → дедуп). → (titles, texts)."""
    from collections import Counter
    alls = [s for s in (list(titles or []) + list(texts or [])) if isinstance(s, str)]
    rub = Counter(
        re.sub(r"[\s ]", "", m.group(1))
        for s in alls
        if _RUB_DISCOUNT_CTX_RE.search(s or "")
        for m in _RUB_DISC_RE.finditer(s)
    )
    pct = Counter(m.group(1) for s in alls for m in _PCT_DISC_RE.finditer(s))
    canon_rub = rub.most_common(1)[0][0] if rub else None
    canon_pct = pct.most_common(1)[0][0] if pct else None
    if not canon_rub and not canon_pct:
        return list(titles or []), list(texts or [])
    fr = _fmt_thousands(canon_rub) if canon_rub else None

    def _fix(s):
        if not isinstance(s, str):
            return s
        if fr:
            if _RUB_DISCOUNT_CTX_RE.search(s):
                s = _RUB_DISC_RE.sub(lambda m: f"{fr} {m.group(2)}", s)
        if canon_pct:
            s = _PCT_DISC_RE.sub(f"{canon_pct}%", s)
        return s
    return [_fix(t) for t in (titles or [])], [_fix(t) for t in (texts or [])]

def _variant_norm_key(x) -> str:
    """Нормализованный ключ для дедупа вариантов контента (заголовки/тексты/быстрые ссылки).
    Схлопывает ЧИСЛА в один маркер «#», поэтому «…скидки до 890 000 ₽…» и «…до 860 000 ₽…»
    считаются ОДНИМ вариантом (отличие только в цифре = по сути дубль). Плюс схлоп пробелов."""
    s = (x.get("title", "") if isinstance(x, dict) else str(x)).strip().lower()
    s = re.sub(r"(?i)\bplug[-\s]?in\s+hybrid\b", "", s)
    s = re.sub(r"(?i)^\s*(купить|новые?|оформите)\s+", "", s)
    s = re.sub(r"\d[\d\s ]*\d", "#", s)             # 2+ значные числа/цены (45%, 890 000) -> маркер
    s = re.sub(r"\s+", " ", s)                            # схлоп пробелов (модели X35/F7 сохраняем)
    return s.strip()

def _lead_utp_bucket(t: str) -> str:
    """Смысловой ключ ведущего УТП финального текста РСЯ: первый сегмент до '. ', без цифр/₽/%,
    первые 2 смысловых слова (lower, ё→е). Используется для дедупа лида в _rsya_texts.
    «Первый взнос 0 ₽. КАСКО…» → «первый взнос», «Автокредит от 9 000 руб.…» → «автокредит от»."""
    first_seg = str(t or "").split(". ")[0]
    base = re.sub(r"[0-9₽%]+", " ", first_seg.lower().replace("ё", "е"))
    return " ".join(base.split()[:2])

def _text_norm_tokens(x) -> list:
    """Нормализованное ядро текста в токены для префиксного дедупа: lower, ё→е, убрана
    пунктуация/валюта/проценты, схлоп пробелов. «Каско на 1 год бесплатно.» →
    ['каско','на','1','год','бесплатно']."""
    s = (x.get("title", "") if isinstance(x, dict) else str(x or "")).lower().replace("ё", "е")
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)     # пунктуация/₽/% → пробел
    return [t for t in s.split() if t]

def _dedup_prefix_absorb(items: list, min_tokens: int = 4) -> list:
    """Схлопнуть пары, где нормализованное ядро одной строки — ПРЕФИКС (надмножество с хвостом)
    другой: «Первый взнос 0 ₽. КАСКО на 1 год бесплатно.» поглощается строкой
    «…бесплатно при покупке в кредит». Оставляем БОЛЕЕ информативную (длинную). Короткую с
    <min_tokens токенами не трогаем (чтобы не схлопнуть по 1-2 общим словам). Реально разные
    оферы (расходятся с начала) — оба сохраняются. Порядок исходных строк сохранён."""
    toks = [(_text_norm_tokens(x), x) for x in (items or [])]
    drop: set = set()
    for i in range(len(toks)):
        if i in drop:
            continue
        ti = toks[i][0]
        if len(ti) < min_tokens:
            continue
        for j in range(len(toks)):
            if i == j or j in drop:
                continue
            tj = toks[j][0]
            # ti — префикс tj (ti короче или равно), хвост tj = «расширение» → поглощаем короткую ti
            if len(ti) <= len(tj) and tj[:len(ti)] == ti:
                drop.add(i)
                break
    return [x for k, (_t, x) in enumerate(toks) if k not in drop]

def _fill_variants(primary: list, supplement: list, need: int) -> list:
    """Контент слепка (primary) в приоритете + добор из supplement до need штук.
    Дедуп по НОРМАЛИЗОВАННОМУ ключу (числа схлопнуты) — чтобы не грузить почти-одинаковые
    заголовки/тексты, отличающиеся только цифрой (см. _variant_norm_key)."""
    seen, out = set(), []
    for x in list(primary) + list(supplement):
        k = _variant_norm_key(x)
        if k and k not in seen:
            seen.add(k)
            out.append(x)
        if len(out) >= need:
            break
    return out

def _rotated_content_window(items: list, need: int, offset: int) -> list:
    """Взять до need элементов с ротацией по offset, сохраняя порядок и дедуп.
    Для быстрых черновиков по слепку это даёт не однотипный контент между РК,
    но источник остаётся тем же слепком."""
    src = [x for x in (items or []) if x]
    if not src:
        return []
    out, seen = [], set()
    n = len(src)
    for i in range(n):
        x = src[(offset + i) % n]
        k = _variant_norm_key(x)
        if k and k not in seen:
            seen.add(k)
            out.append(x)
        if len(out) >= need:
            break
    return out

def _drop_brand_model_keys(keywords: list, brand: str) -> list:
    """БАГ-13: для кампании ПО МАРКЕ (ct_type='Марки') — исключить ключи, содержащие марку И модель
    одновременно. Ключи «Chery» — оставить. Ключи «Chery Tiggo 8 Pro» — убрать.

    Механика: если ключ содержит бренд + ещё одно слово-не-предлог (не артикль/союз) — это
    ключ марка+модель. brand — название марки из ct (напр. «Chery», «Haval»).
    Пустой brand или brand из одного слова ≥4 букв = безопасный фолбэк (не фильтруем).
    Нечувствителен к регистру."""
    brand = (brand or "").strip()
    if not brand:
        return list(keywords)
    # Стоп-слова (предлоги/союзы/частицы): их наличие рядом с маркой ≠ признак модели
    _STOP = {"в", "и", "на", "от", "до", "для", "с", "по", "за", "к", "у", "о", "или", "не",
             "авто", "официальный", "официальные", "дилер", "купить", "цена", "цены", "новые", "новый"}
    brand_re = re.compile(r"(?i)\b" + re.escape(brand) + r"\b")
    result = []
    for kw in keywords:
        s = str(kw).strip()
        if not brand_re.search(s):
            result.append(kw)   # нет марки → оставляем
            continue
        # Убираем марку и смотрим, есть ли рядом содержательные слова (модель)
        without_brand = brand_re.sub("", s).strip()
        extra_words = [w for w in re.split(r"\W+", without_brand) if w and w.lower() not in _STOP and len(w) >= 3]
        if extra_words:
            continue     # есть слово вне стоп-списка → это ключ «марка+модель» → выкидываем
        result.append(kw)
    return result

_BRAND_MODEL_TOKEN_SET: set | None = None

def _brand_model_token_set() -> set:
    """Канон-токены ВСЕХ марок и моделей (сегменты «Марки»/«Модели» классификатора) — чтобы отсеять
    модельные ключи из ОБЩИХ групп (Дром/Авто/ct0001…): общая группа не должна нести ключ конкретной
    модели (Cityray/Monjaro/Tiggo…). В набор кладём и сырой токен, и его канон (кир→лат). Кэш."""
    global _BRAND_MODEL_TOKEN_SET
    if _BRAND_MODEL_TOKEN_SET is not None:
        return _BRAND_MODEL_TOKEN_SET
    _STOP = {"авто", "автомобиль", "автомобили", "машина", "машины", "new", "plus", "pro",
             "max", "sport", "купить", "цена", "новый", "новые"}
    toks: set = set()
    for ct, nm in _ag_part1_map().items():
        if _ct_segment(ct) in ("Марки", "Модели"):
            for w in re.findall(r"[a-zа-яё0-9]+", str(nm or "").lower()):
                if len(w) >= 3 and w not in _STOP:
                    toks.add(w)
                    toks.add(_brand_canon(w) or w)
    _BRAND_MODEL_TOKEN_SET = toks
    return toks

def _drop_model_keys_common(keywords: list) -> list:
    """Из ключей ОБЩЕЙ группы («Общее») убрать те, что содержат токен конкретной марки/модели.
    Общая группа (Авто/Дром/ct0001) несёт только общие запросы (автокредит, авто в кредит), а не
    «Geely Cityray цена». Ловит латиницу и канон кириллицы (хавал→haval, джили→geely)."""
    toks = _brand_model_token_set()
    if not toks:
        return list(keywords or [])
    out = []
    for kw in (keywords or []):
        words = set()
        for w in re.findall(r"[a-zа-яё0-9]+", str(kw).lower()):
            words.add(w)
            words.add(_brand_canon(w) or w)
        if words & toks:
            continue
        out.append(kw)
    return out

# ── Защита «чужая модель той же марки» для сегмента «Модели» ──────────────────────────────────
# Токены-дискриминаторы чужих моделей вычисляются из brand_models_catalog.json (статичный JSON).
# Кэш на процесс: каталог не меняется в рантайме.

_DISC_STOP: frozenset = frozenset({"новый", "новые", "new", "нов"})   # общие модификаторы ≠ дискриминаторы
_FOREIGN_DISCRIMINATORS_CACHE: dict = {}

# Кузов/модификация пишется в РАЗНЫХ алфавитах: имя группы в структуре латиницей
# («Lada Granta Liftback», gk=lada_granta_liftback), а brand_models_catalog — кириллицей
# («Granta Лифтбек»). Без сшивки токен «лифтбек» считался бы дискриминатором ЧУЖОЙ модели
# и выкашивал ВСЕ ключи собственной группы (регресс per-adgroup: группа уезжала с 0 ключей).
_MODEL_TOKEN_ALIASES: dict = {
    "лифтбек": "liftback", "лифтбэк": "liftback",
    "хэтчбек": "hatchback", "хетчбек": "hatchback", "хэтчбэк": "hatchback",
    "седан": "sedan", "универсал": "universal", "кросс": "cross", "кроссовер": "crossover",
    "спорт": "sport", "купе": "coupe", "комби": "combi",
}
_MODEL_TOKEN_ALIASES_REV: dict = {
    lat: frozenset(c for c, l in _MODEL_TOKEN_ALIASES.items() if l == lat)
    for lat in set(_MODEL_TOKEN_ALIASES.values())
}


def _expand_model_tokens(toks) -> frozenset:
    """Токены + их кир↔лат эквиваленты кузова/модификации (лифтбек↔liftback, седан↔sedan)."""
    out = set(toks or ())
    for t in set(toks or ()):
        lat = _MODEL_TOKEN_ALIASES.get(t)
        if lat:
            out.add(lat)
        out.update(_MODEL_TOKEN_ALIASES_REV.get(t) or ())
    return frozenset(out)


def _model_subtokens(name: str) -> frozenset:
    """Токены строки с разбивкой на границах буква↔цифра; len≤1 отсеиваются.
    'CS35Plus' → {'cs','35','plus'}, 'cs75' → {'cs','75'}, 'UNI-K' → {'uni'} (k len=1 → дроп).
    Применяется и к каталожным именам моделей, и к ключевым фразам при дискриминации."""
    raw = re.findall(r"[a-zа-яё0-9]+", (name or "").lower())
    toks: set = set()
    for part in raw:
        for sub in re.findall(r"[a-zа-яё]+|[0-9]+", part):
            if len(sub) > 1:
                toks.add(sub)
    return frozenset(toks)


def _kw_positive_tokens(kw: str) -> frozenset:
    """Токены ключа БЕЗ минус-частей фразы: «лада гранта лифтбек -спорт» → {лада,гранта,лифтбек}.
    Минус-слово внутри ключа наоборот ЗАПРЕЩАЕТ показ по нему, поэтому считать его признаком
    чужой модели нельзя — иначе легальный ключ группы дропался из-за собственного минуса."""
    return _model_subtokens(" ".join(w for w in str(kw or "").split() if not w.startswith("-")))


def _foreign_model_discriminators(model: str) -> frozenset:
    """Дискриминирующие токены ЧУЖИХ моделей той же марки относительно own-модели model.

    Алгоритм: own_toks = _model_subtokens(model); для каждой другой модели в brand_models_catalog
    той же марки (первое слово model): disc += _model_subtokens(other) − own_toks − _DISC_STOP.

    Пример: model='Changan CS35Plus' → own={changan,cs,35,plus}; CS75→{cs,75}→disc {75};
    CS35Plus Новый→{cs,35,plus,новый}→{новый} но в _DISC_STOP → исключается.
    Ключ «changan cs75 plus» содержит '75' ∈ disc → дроп.

    Кэш на процесс. При ошибке / модели нет в каталоге → frozenset() (фильтрация отключена)."""
    model = (model or "").strip()
    if not model:
        return frozenset()
    if model in _FOREIGN_DISCRIMINATORS_CACHE:
        return _FOREIGN_DISCRIMINATORS_CACHE[model]
    try:
        cat = json.loads((_HERE_TG / "brand_models_catalog.json").read_text(encoding="utf-8"))
        brands: dict = cat.get("brands") or {}
        brand_key = model.split()[0].lower()
        brand_entry: dict = brands.get(brand_key) or {}
        all_models: list = brand_entry.get("models") or []
    except Exception:  # noqa: BLE001 — нет файла / битый JSON → без фильтрации
        _FOREIGN_DISCRIMINATORS_CACHE[model] = frozenset()
        return frozenset()
    if not all_models:
        _FOREIGN_DISCRIMINATORS_CACHE[model] = frozenset()
        return frozenset()
    own_toks = _expand_model_tokens(_model_subtokens(model))
    disc: set = set()
    for other_name in all_models:
        other_toks = _expand_model_tokens(_model_subtokens(other_name))
        disc.update((other_toks - own_toks) - _DISC_STOP)
    result = frozenset(disc)
    _FOREIGN_DISCRIMINATORS_CACHE[model] = result
    return result


_AUTOTARGET_KW = "---autotargeting"


def _real_kw_count(keywords: list) -> int:
    """Сколько РЕАЛЬНЫХ ключей в списке. Спецключ «---autotargeting» живёт в relevanceMatch и при
    заливке пропускается (grid_create.add_keywords) → он НЕ делает список непустым. Без этого
    анти-пустой фолбэк (`or kws`) обманывался спецключом и группа уезжала с 0 ключей в кабинете."""
    return sum(1 for k in (keywords or [])
               if not str(k or "").strip().startswith("---"))


def _filter_group_keywords(positive: list, seg: str, brand: str, city: str, site_type: str,
                           model: str = "") -> list:
    """Единый отбор ключей группы по сегменту ct: 'Марки' → убрать «марка+модель»; 'Общее' → убрать
    любые модельные/марочные ключи (cityray/monjaro в общей группе — баг); 'Модели' → оставить как есть
    (модельные ключи — суть группы), + защита от ключей ЧУЖОЙ модели той же марки при model != ''.
    Предварительно всегда: б/у и чужой город.

    model: полное имя модели группы из ct-кодера (напр. 'Changan CS35Plus'). При непустом значении
    для seg='Модели' дропаются ключи с дискриминирующими токенами других моделей той же марки."""
    kws = _drop_used_car(_drop_foreign_city_keywords(positive or [], city), site_type)
    # НЕ-АВТО слепок (B2B-лидоген dmp и будущие): авто-фильтр «убрать марка+модель» / «оставить только
    # авто-термины» ЛОМАЕТ B2B-ключи (сегмент «Общее» → _keep_general_common выкинет «купить лиды» и
    # уронит в авто-фолбэк). Для не-авто site_type — только базовые дропы (б/у + чужой город), ключи as-is.
    if site_type in _NON_AUTO_SITE_TYPES:
        return kws
    if seg == "Модели":
        if model:
            disc = _foreign_model_discriminators(model)
            if disc:
                filtered = []
                for kw in kws:
                    kw_toks = _kw_positive_tokens(str(kw))
                    if kw_toks & disc:
                        continue  # ключ содержит токен чужой модели → дроп
                    filtered.append(kw)
                # Анти-пустой гейт: фильтр чужих моделей не должен обнулять группу (пак-модель
                # могла разойтись с каталогом) — считаем по РЕАЛЬНЫМ ключам, спецключ не в счёт.
                return filtered if _real_kw_count(filtered) else kws
        return kws                                    # модельные ключи — суть группы «Модели»
    if seg == "Марки":
        # Марки: убрать «марка+модель»; фолбэк на kws допустим (там марочная лексика, не чужая).
        _brand_only = _drop_brand_model_keys(kws, brand)
        return _brand_only if _real_kw_count(_brand_only) else kws
    # seg == "Общее": убрать ЛЮБЫЕ марка/модель-ключи.
    out = _drop_model_keys_common(kws)
    # #6-доводка: blacklist моделей — по ЛАТ-токенам ct-имён, но кириллический транслит модели
    # («икс рей» = Lada X-Ray) не ловится → протекает в «Общую» группу. Вторичный фильтр: в ОБЩЕЙ
    # группе оставляем только ключи с ОБЩИМ авто/финанс-термином; чистые модель-запросы (без общего
    # слова) выкидываем — они не по адресу в общей группе (место модельным — в группе «Модели»).
    out = _keep_general_common(out)
    if _real_kw_count(out):
        return out
    # Баг #6: если дроп опустошил набор (пул общей ct несёт ТОЛЬКО модельные ключи, напр. ct0014
    # «Авто/Автомобили/Машины») — НЕЛЬЗЯ возвращать kws: это вернёт ЧУЖИЕ модельные ключи («лада x рей»)
    # в ОБЩУЮ группу мультибренда (регресс сдвига). Даём общий город-фолбэк (без марки/модели).
    return _generic_common_keywords(city)


_GENERAL_COMMON_STEMS = (
    "кредит", "рассрочк", "лизинг", "взнос", "господдержк", "автосалон", "салон", "дилер",
    "trade", "трейд", "авито", "avito", "дром", "drom", "auto", "авто", "автомобил", "машин",
    "купить", "продаж", "налич", "цена", "новый", "новые", "подержан", "каталог", "акци",
    "скидк", "выгод", "госпрограмм", "utility", "утильсбор",
)


def _keep_general_common(keywords: list) -> list:
    """Для ОБЩЕЙ группы: оставить только ключи, где есть ОБЩИЙ авто/финанс-термин. Чистый
    модель-запрос без общего слова («икс рей», «икс рей 2024») — не для общей группы, дропаем.
    ПРАВКА 6: перед keep-по-стему дропать ключ, если он содержит токен известной марки/модели
    (включая Кириллические транслиты «икс», «рей», «монжаро») — иначе «икс рей автосалон»
    проходил через стем «автосалон» и попадал в ОБЩУЮ группу (#6-fix2)."""
    model_toks = _auto_brand_tokens()
    out = []
    for kw in (keywords or []):
        low = str(kw).lower()
        if model_toks:
            kw_words = set(re.findall(r"[a-zа-яё0-9]+", low))
            if kw_words & model_toks:
                continue  # ключ содержит модельный/марочный токен → не для ОБЩЕЙ группы
        if any(s in low for s in _GENERAL_COMMON_STEMS):
            out.append(kw)
    return out


def _generic_common_keywords(city: str) -> list:
    """Общие (без марки/модели) ключи для группы «Общее», когда её пул несёт ТОЛЬКО модельные.
    Замена бага #6 (возврат модельных ключей в общую группу). Гео-привязка по городу группы."""
    c = (_content_city(city) or "").strip()          # мультигород (через запятую) → без города (не «...Краснодар, Н.Новгород»)
    base = ["авто в кредит", "автокредит", "купить авто в кредит", "машина в кредит",
            "новый автомобиль в кредит", "авто в рассрочку", "автомобиль в кредит"]
    return ([f"{b} {c}" for b in base] + base) if c else list(base)

_AUTO_BRAND_TOKEN_CACHE: set[str] | None = None

# Кириллические транслиты токенов моделей — не выводимы из Latin имён фида автоматически.
# «икс» + «рей» = Lada X-Ray, «монжаро» = Haval Monjaro / Changan Mongaro и т.п.
_AUTO_BRAND_CYRILLIC_EXTRA: frozenset[str] = frozenset({
    "икс", "рей", "рэй", "монжаро", "бестарн", "кулун", "атлас", "туксон", "солярис",
    # «ситирей» = Geely Cityray (city-ray → сити-рей → ситирей), «кулрей» = Coolray:
    # кириллический транслит НЕ выводим из Latin-имени фида → добавлен явно, иначе
    # чистый модель-запрос «ситирей купить» протекал в тема/«Общее»-группу («Дром»),
    # D8 2026-07-09 (tp5 712608932 ct0010-Дром получила ключи «ситирей»).
    # R2-4 2026-07-10: в кабинет попал вариант через «э» (U+044D) — «ситирэй»/«кулрэй»
    # (tp5 «Общее» ct0010-Дром 712650300, прогон e05fbc86e8ca). «рей»(е)≠«рэй»(э) —
    # разные Unicode-точки, оба написания встречаются у Geely Cityray/Coolray → оба явно.
    "ситирей", "ситирэй", "кулрей", "кулрэй",
})

def _auto_brand_tokens() -> set[str]:
    """Канонические токены марок И ВСЕХ токенов моделей из фид-индекса + Кириллические транслиты.
    ПРАВКА 6: добавлены все токены feeds_ct_model() (не только первый) + _AUTO_BRAND_CYRILLIC_EXTRA,
    чтобы «икс рей автосалон» отсекался в _keep_general_common по токену «икс»/«рей»."""
    global _AUTO_BRAND_TOKEN_CACHE
    if _AUTO_BRAND_TOKEN_CACHE is not None:
        return _AUTO_BRAND_TOKEN_CACHE
    base: set[str] = {
        "lada", "лада", "baic", "belgee", "changan", "chery", "dongfeng", "exeed", "faw",
        "gac", "geely", "haval", "hyundai", "jac", "jaecoo", "kaiyi", "kia", "livan",
        "mazda", "moskvich", "москвич", "omoda", "renault", "skoda", "tank", "toyota",
        "voyah",
    }
    base.update(_AUTO_BRAND_CYRILLIC_EXTRA)
    _SKIP_GENERIC = {"pro", "max", "plus", "new", "sport", "auto", "авто", "car"}
    try:
        for model in kp.feeds_ct_model().values():
            for t in re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", str(model or "")):
                lt = t.lower()
                # Все токены модели (не только первый): «Lada X-Ray» → «lada» + «ray»
                if not lt.isdigit() and lt not in _SKIP_GENERIC:
                    base.add(lt)
    except Exception:  # noqa: BLE001
        pass
    _AUTO_BRAND_TOKEN_CACHE = {x for x in base if len(x) >= 2}
    return _AUTO_BRAND_TOKEN_CACHE

def _own_brand_tokens(brand: str) -> set[str]:
    toks = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", str(brand or ""))
    own = {toks[0].lower()} if toks else set()
    if "lada" in own:
        own.add("лада")
    if "лада" in own:
        own.add("lada")
    if "moskvich" in own:
        own.add("москвич")
    if "москвич" in own:
        own.add("moskvich")
    return own

def _drop_foreign_brand_mentions(items: list, brand: str) -> list:
    """Для группы BAIC/Haval/... выкинуть тексты с чужой маркой, например LADA в группе BAIC."""
    own = _own_brand_tokens(brand)
    if not own:
        return list(items or [])
    foreign = _auto_brand_tokens() - own
    out = []
    for x in items or []:
        s = str(x)
        sl = s.lower()
        if any(re.search(r"(?<![a-zа-яё0-9])" + re.escape(tok) + r"(?![a-zа-яё0-9])", sl)
               for tok in foreign):
            continue
        out.append(s)
    return out

def _brand_text_set(brand: str, city: str = "") -> list[str]:
    """Брендовые УТП для РСЯ-группы, чтобы добивка не брала тексты про чужую марку."""
    city = _content_city(city)                            # мультигород (через запятую) → без города
    brand = _display_brand(brand)
    if not brand:
        return []
    city_loc = _city_locative((city or "").strip()) if (city or "").strip() else ""
    loc = f" в {city_loc}" if city_loc else ""
    return [
        f"Купить {brand} в кредит{loc}. Первый взнос 0 ₽ и КАСКО на 1 год бесплатно",
        f"{brand} в наличии{loc}. Одобрение за 30 минут и подбор от 15 банков",
        f"Кредит на {brand}{loc}. Трейд-ин выше рынка и заявка онлайн",
        f"{brand} в кредит{loc}. Платеж от 9 000 ₽/мес и оформление онлайн",
        f"{brand}{loc}. Выгода при покупке в кредит и быстрое одобрение",
    ]

def _display_brand(brand: str) -> str:
    """Название марки/модели для текстов: без slash-технических склеек вроде UNI-S/CS55Plus."""
    return re.sub(r"\s+", " ", str(brand or "").replace("/", " ")).strip()

def _brand_in_text(text: str, brand: str) -> bool:
    """Есть ли в строке явное упоминание текущей марки/модели."""
    own = _own_brand_tokens(brand)
    if not own:
        return False
    low = str(text or "").lower()
    return any(re.search(r"(?<![a-zа-яё0-9])" + re.escape(tok) + r"(?![a-zа-яё0-9])", low) for tok in own)

def _brand_isolated_first_phrase(title: str, brand: str) -> bool:
    """Bad title start: the first sentence is only the brand/model, e.g. ``KAIYI.``."""
    if not brand:
        return False
    head = str(title or "").split(".")[0].strip()
    if not head:
        return False
    brand_norm = re.sub(r"\s+", " ", _display_brand(brand)).strip().lower()
    head_norm = re.sub(r"\s+", " ", head).strip().lower()
    if head_norm == brand_norm:
        return True
    own = _own_brand_tokens(brand)
    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", head_norm)
    return bool(words) and all(w in own for w in words)

# Слова-модификаторы, при которых марка ОСТАЁТСЯ подлежащим (brand-first уже соблюдён):
# «Новый BAIC …», «Купить BAIC …» — реордер НЕ нужен.
_BRAND_LEAD_OK_PREFIX = {
    "новый", "новая", "новые", "новое",
    "купить", "купите", "купи",
    "закажите", "закажи", "заказать",
    "оформить", "оформите", "оформи",
    "возьмите", "возьми", "взять", "бери", "берите",
}
# Соединительные слова/предлоги — обрезаем с хвоста «осиротевшего» префикса при реордере
# («Кредит на» → «Кредит»).
_REORDER_TAIL_STRIP = {"на", "в", "во", "по", "для", "от", "с", "со", "за", "к", "ко", "у", "о", "об", "и", "а"}


def _brand_first_reorder(title: str, brand: str) -> str:
    """ДЕТЕРМИНИРОВАННО поставить марку/модель в НАЧАЛО заголовка (подлежащее, до первой точки).

    Не полагаемся на LLM: даже если генерация/фиксер дали «Кредит на BAIC …» или «Платеж … BAIC»,
    переставляем сегмент с маркой в начало.
      «Кредит на BAIC в Кемерово. Первый взнос 0 ₽» → «BAIC в Кемерово. Кредит. Первый взнос 0 ₽»
      «Платеж от 9 000 ₽/мес. BAIC в Кемерово»      → «BAIC в Кемерово. Платеж от 9 000 ₽/мес»
    Если марка уже ведёт (или ведёт с допустимым модификатором «Новый/Купить …») — без изменений.
    Если марку не нашли вовсе — возвращаем как есть (страховкой служит LLM-регенерация
    ``fix_brand_not_first``). Реордер НЕ применять к сегменту «Общее»/ct0000 (там brand пуст)."""
    t = str(title or "").strip()
    if not t or not brand:
        return t
    own = _own_brand_tokens(brand)
    if not own:
        return t
    if _brand_isolated_first_phrase(t, brand):
        parts0 = [p.strip().rstrip(".") for p in re.split(r"\s*\.\s+", t) if p.strip()]
        if len(parts0) >= 2:
            low_next = parts0[1].lower()
            if low_next.startswith("кредит"):
                parts0[0] = f"{parts0[0]} в кредит"
            elif low_next.startswith("платеж") or low_next.startswith("платёж"):
                parts0[0] = f"{parts0[0]} с платежом"
            elif low_next.startswith("каско"):
                parts0[0] = f"{parts0[0]} с КАСКО"
            elif low_next.startswith("выгода"):
                parts0[0] = f"{parts0[0]} с выгодой"
            elif low_next.startswith("одобрение"):
                parts0[0] = f"{parts0[0]} с одобрением"
            else:
                parts0[0] = f"{parts0[0]} в наличии"
            t = ". ".join(parts0)
    tok_re = re.compile(
        r"(?<![a-zа-яё0-9])(?:" + "|".join(re.escape(x) for x in own) + r")(?![a-zа-яё0-9])",
        re.IGNORECASE)
    m = tok_re.search(t)
    if not m:
        return t                                    # марки нет вовсе → не наша забота (LLM-бэкап)
    pre_words = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", t[:m.start()])
    # марка уже ведёт (пустой префикс) ИЛИ префикс = только допустимые модификаторы → brand-first OK
    if not pre_words or all(w.lower() in _BRAND_LEAD_OK_PREFIX for w in pre_words):
        return t
    # ── нужен реордер: делим на сегменты по «. » и находим сегмент с маркой ──
    parts = [p for p in re.split(r"\s*\.\s+", t) if p.strip()]
    b_idx = next((i for i, p in enumerate(parts) if tok_re.search(p)), -1)
    if b_idx < 0:
        return t
    seg = parts[b_idx]
    sm = tok_re.search(seg)
    seg_pre = seg[:sm.start()]                       # текст ДО марки внутри её сегмента
    brand_onwards = seg[sm.start():].strip()         # марка и всё после неё в этом сегменте
    if not brand_onwards:
        return t
    # осиротевший внутрисегментный префикс: чистим хвостовые предлоги/союзы, капитализируем
    pw = re.findall(r"[A-Za-zА-Яа-яЁё0-9%₽/]+", seg_pre)
    while pw and pw[-1].lower() in _REORDER_TAIL_STRIP:
        pw.pop()
    leftover = " ".join(pw).strip()
    new_parts = [brand_onwards]
    if leftover and leftover.lower() not in _REORDER_TAIL_STRIP:
        new_parts.append(_cap_first(leftover))
    new_parts += [p for i, p in enumerate(parts) if i != b_idx]
    out = ". ".join(x.strip().rstrip(".") for x in new_parts if x and x.strip())
    out = _cap_first(out)
    if _brand_isolated_first_phrase(out, brand):
        p2 = [p.strip().rstrip(".") for p in re.split(r"\s*\.\s+", out) if p.strip()]
        if len(p2) >= 2:
            low_next = p2[1].lower()
            if low_next.startswith("кредит"):
                p2[0] = f"{p2[0]} в кредит"
            elif low_next.startswith("платеж") or low_next.startswith("платёж"):
                p2[0] = f"{p2[0]} с платежом"
            elif low_next.startswith("каско"):
                p2[0] = f"{p2[0]} с КАСКО"
            elif low_next.startswith("выгода"):
                p2[0] = f"{p2[0]} с выгодой"
            elif low_next.startswith("одобрение"):
                p2[0] = f"{p2[0]} с одобрением"
            else:
                p2[0] = f"{p2[0]} в наличии"
            out = ". ".join(p2)
    # безопасность: реордер не должен родить «плохой» заголовок — иначе оставляем оригинал
    if not out or _is_bad_start(out) or _bad_ad_title(out):
        return t
    return out

# Чистые УТП-тексты для РСЯ (≤56, ОДНА мысль/предложение) — приоритетнее «кашеобразных» текстов
# слепка («Господдержка … Нулевой утильсбор … Распродаём стоянку -45%. Звоните!»). Правило пользователя.
# ⛔ БЕЗ %-ставки кредита/рассрочки (правило Семёна). Полные, «вкусные», грамотные УТП-предложения
# (с заглавной, законченная мысль) — чтобы при склейке к 81 не было «дермовых» огрызков.
_RSYA_TEXT_POOL = [
    "Кредит на новый авто. Одобрение за 30 минут онлайн",
    "Автокредит от 9 000 руб. Одобрение за 30 минут онлайн",
    "КАСКО в подарок и оценка авто в трейд-ин при кредите",
    "Первый взнос 0 руб. Подберем условия от банков",
    "Господдержка и выгодный кредит. Заявка онлайн",
    "Новые авто в наличии. Выгодные условия и подарки",
    "Тест-драйв сегодня. Запишитесь онлайн за пару минут",
    "Авто в наличии. Выгода месяца и подарки при покупке",
]

def _rsya_texts(incoming: list, site_type: str, city: str,
                brand: str = "", cap: int = _RA_TEXTS_CAP) -> list:
    """≤cap чётких УТП-текстов РСЯ (≤56, ОДНА мысль). СОХРАНЯЕМ контент M3/слепка: короткие тексты —
    как есть, длинные «кашеобразные» — РАЗБИВАЕМ на отдельные УТП (`_split_utp`). Чистый пул
    `_RSYA_TEXT_POOL` — только добивка, если своих не хватило. Чистка: не-Б/У сайт → без «б/у»;
    чужой город → город аккаунта. Когерентность с заголовками — в _responsive_ad."""
    # B2B-лидоген (dmp): вместо авто-текстов — B2B-корпус из агентского словаря.
    # Авто-шаблоны (_brand_text_set, _RSYA_TEXT_POOL, pad_tails) для dmp неприменимы.
    if site_type == "dmp":
        from . import ai_agents as _A  # lazy import во избежание кругового импорта
        _dmp_ads = (_A.AGENT_ADS.get("dmp") or {})
        _dmp_corpus = list(_dmp_ads.get("texts") or [])
        _b2b_text_fillers = [
            "Предоставляем контакты клиентов, готовых купить прямо сейчас — без холодных звонков",
            "Горячая база клиентов для вашей ниши. Получите доступ к заинтересованным покупателям",
            "Платформа лидогенерации: контакты реальных клиентов для роста продаж вашего бизнеса",
            "Идентифицируем заинтересованных клиентов в вашей нише и передаём контакты онлайн",
            "Горячие лиды для бизнеса: контакты людей, которые ищут ваши товары прямо сейчас",
        ]
        _b2b_base = [t for t in (list(incoming or []) + _dmp_corpus) if t]
        _all = list(dict.fromkeys(_b2b_base + _b2b_text_fillers))
        return [t for t in _all if t][:cap]
    _, _cities_bl = _title2_blocklist()
    acc_city = (city or "").strip()

    def _cf(lst):
        return _replace_foreign_city(
            _drop_new_car(_drop_used_car(list(lst or []), site_type), site_type),
            acc_city, _cities_bl)

    raw_incoming = _cf(list(incoming or []))
    branded_incoming = [t for t in raw_incoming if _brand_in_text(t, brand)] if brand else []
    generic_incoming = [t for t in raw_incoming if t not in branded_incoming]
    pieces = []
    source_items = ((branded_incoming + generic_incoming) if not brand else (branded_incoming + generic_incoming[:1]))
    for t in source_items:
        t = _strip_credit_rate(str(t))               # ⛔ убрать %-ставку кредита (правило Семёна)
        if len(t) <= _RSYA_TEXT_MAX:
            pieces.append(t)                         # уже чёткий ≤56 — как есть
        else:
            pieces += _split_utp(t)                  # длинный → отдельные УТП ≤56
    brand_fillers = _brand_text_set(brand, city) if brand else []
    pieces = _drop_foreign_brand_mentions(brand_fillers + pieces, brand) if brand else pieces
    # Чистка КАЖДОГО куска: убрать %-ставку, заглавная буква, ВЫКИНУТЬ огрызки (начинается с предлога
    # «до/от/у…» = середина предложения → «дермовый» текст). Дедуп. Чистый пул — в добивку.
    seen, uniq, stamp_fallback = set(), [], []
    # ⚠️ Пул-добивка идёт мимо `_cf` (он чистит только incoming), а в пуле есть строки про НОВЫЕ
    # авто («Кредит на новый авто…», «Новые авто в наличии…») — на Б/У-сайте они и протекали.
    for p in (pieces + _drop_new_car(list(_RSYA_TEXT_POOL), site_type)):
        # БАГ 4→исправлен: тире -> точка; дефис-разделитель → точка; БАГ 8: капитализация; %-ставка убирается
        p = _normalize_numeric_suffixes_bp(
            _sentence_case(_cap_first(_replace_sep_hyphen(_replace_emdash(_strip_credit_rate(p)))))
        )
        p = _strip_installment_text(p)
        # ⚠️ НЕ чистим числовой хвост у входящего контента: «Выгода до 300 000» без ₽ —
        # валидное УТП донора, безусловная чистка его ампутирует (ревью 06.07). Обрыв
        # НАШЕЙ обрезки лечится в _trim_clean на точках эмиссии; финал — _sanitize_content.
        # БАГ 8: предлог в начале / маленькая буква / слишком коротко -> выкидываем
        if len(p) < 15 or _is_bad_start(p):
            continue
        if _bad_ad_text(p):
            continue
        # БАГ 2: дедупликация по смысловому ключу (_variant_norm_key схлопывает числа)
        k = _variant_norm_key(p)
        if k and k not in seen:
            seen.add(k)
            # АНТИ-AI ПРАВИЛО 1: штампы идут в резерв — используются только если чистых не хватает
            if _has_stamp(p):
                stamp_fallback.append(p)
            else:
                uniq.append(p)
    # Докидываем штамп-резерв только если чистых текстов недостаточно
    if len(uniq) < cap:
        uniq += stamp_fallback
    # АНТИ-AI ПРАВИЛО 4: тексты с цифрами — первыми (конкретика важнее абстракций).
    uniq = [t for t in uniq if _has_number(t)] + [t for t in uniq if not _has_number(t)]
    # ЖАДНАЯ СКЛЕЙКА УТП в ПОЛНЫЙ текст <=81 — добиваем как можно ближе к 81.
    # БАГ 3: склеенный текст обрезаем по последнему целому слову (_trim_to_word), не жёстко.
    _TXT_MAX = 81
    out, i = [], 0
    while i < len(uniq) and len(out) < cap:
        cur = uniq[i].rstrip(".!?")
        i += 1
        while i < len(uniq):
            nxt = _cap_first(uniq[i].rstrip(".!?"))   # заглавная у каждой склеиваемой части
            if len(cur) + 2 + len(nxt) <= _TXT_MAX:
                cur = cur + ". " + nxt
                i += 1
            else:
                break
        # БАГ 3: обрезка по целому слову, не посреди
        out.append(_sentence_case(_trim_to_word(cur.strip(), _TXT_MAX).rstrip()))
    # Дедуп по ведущему УТП: не допускать двух текстов с одинаковым смысловым лидом
    # («Первый взнос 0 ₽. …» ×2 — _variant_norm_key не схлопывает однозначный «0»).
    _seen_lead: set[str] = set()
    _dedup_lead_out: list[str] = []
    for _t in out:
        _lb = _lead_utp_bucket(_t)
        if _lb not in _seen_lead:
            _seen_lead.add(_lb)
            _dedup_lead_out.append(_t)
    out = _dedup_lead_out
    # Практическое требование по tp1: первый текст не должен оставаться коротким однофразником,
    # если в лимите 81 есть место для нормального второго УТП.
    pad_tails = [
        "Одобрение за 30 минут",
        "КАСКО в подарок",
        "Трейд-ин выше рынка",
        "Запись на тест-драйв",
        "Заявка онлайн",
        # Короткие хвосты ≤10 симв — закрывают «мёртвую зону» 10-14 свободных
        # (аналог фикса заголовков: минимальный длинный хвост 13+2 не влезал).
        "Тест-драйв",
        "Рассрочка",
        "Трейд-ин",
        "Гарантия",
    ]
    padded: list[str] = []
    used_tail_buckets: set[str] = set()

    def _tail_bucket(s: str) -> str:
        x = str(s or "").lower()
        if "одобр" in x:
            return "approval"
        if "каско" in x or "подар" in x:
            return "gift"
        if "трейд" in x:
            return "tradein"
        if "тест" in x:
            return "testdrive"
        if "дилер" in x:
            return "dealer"
        return x

    for text in out:
        cur = str(text or "").rstrip(".!?")
        for tail in pad_tails:
            if len(cur) >= 75:
                break
            if tail.lower() in cur.lower():
                continue
            tb = _tail_bucket(tail)
            if tb in used_tail_buckets:
                continue
            nxt = _cap_first(tail.rstrip(".!?"))
            if len(cur) + 2 + len(nxt) <= _TXT_MAX:
                cur = f"{cur}. {nxt}"
                used_tail_buckets.add(tb)
        padded.append(_normalize_numeric_suffixes_bp(_sanitize_content(cur, _TXT_MAX)))
    out = padded
    return out[:cap]

def _text_offer_buckets(s: str) -> set[str]:
    x = str(s or "").lower()
    out: set[str] = set()
    if "кредит" in x or "автокредит" in x:
        out.add("credit")
    if "одобр" in x:
        out.add("approval")
    if re.search(r"плат[её]ж|/мес|\bот\s+\d[\d\s]*\s*(?:₽|руб)", x):
        out.add("payment")
    if re.search(r"перв\w*\s+взнос|без\s+перв", x):
        out.add("first_payment")
    if "каско" in x or "подар" in x or "шин" in x or "комплект" in x:
        out.add("gift")
    if "трейд" in x or "обмен" in x:
        out.add("tradein")
    if "господдерж" in x or "госпрограм" in x:
        out.add("support")
    if "скид" in x or "выгод" in x or "%" in x:
        out.add("discount")
    if "тест" in x:
        out.add("testdrive")
    if "налич" in x:
        out.add("availability")
    return out

def _diverse_text_offers(candidates: list[str], limit: int = 3) -> list[str]:
    """Выбрать тексты без повторения одинаковых УТП внутри одного комбинаторного объявления."""
    clean = [str(x or "").strip() for x in (candidates or []) if str(x or "").strip()]
    out: list[str] = []
    used: set[str] = set()
    for x in clean:
        b = _text_offer_buckets(x)
        if b and used.intersection(b):
            continue
        out.append(x)
        used.update(b)
        if len(out) >= limit:
            return out
    fallback = [
        "Кредит на авто от 9 000 ₽/мес. Подберем условия от 15 банков. Одобрение за 1 час.",
        "Кредит без первого взноса на новое авто. Одобрение за 1 день. 15 банков онлайн.",
        "КАСКО на 1 год бесплатно при покупке в кредит. Ключи в день покупки. Одобрение.",
        "Трейд-ин выше рынка. Оценим авто за 30 минут и зачтем в счет нового кредита.",
    ]
    for x in fallback:
        b = _text_offer_buckets(x)
        if b and used.intersection(b):
            continue
        out.append(x)
        used.update(b)
        if len(out) >= limit:
            return out
    for x in clean:
        if x not in out:
            out.append(x)
        if len(out) >= limit:
            break
    return out

# ЭТАЖ-ГАРАНТ tp6/tp7: заголовки, нейтральные к типу сайта — БЕЗ Б/У-лексики и БЕЗ «новых авто»,
# поэтому не режутся ни `_drop_used_car`, ни `_drop_new_car` ни на одном site_type. Все с цифрой
# (number-gate), с кредитным углом, 50-51 симв. Используются, только когда фильтры вычистили всё.
_NEUTRAL_CREDIT_TITLES = [
    "Авто в кредит от 9 000 ₽/мес. Одобрение за 30 минут",
    "Кредит на авто. Первый взнос 0 ₽. Ключи за 1 день",
    "Автокредит от 15 банков. Решение за 30 минут онлайн",
    "Трейд-ин выше рынка. Платеж от 9 000 ₽/мес в кредит",
    "Одобрение за 30 минут. Кредит на авто от 15 банков",
]

# Текстовый близнец `_NEUTRAL_CREDIT_TITLES` — для финальной сборки адаптива
# (`create_set_assets._upgrade_credit_texts`), где хардкод-варианты «Новые авто в кредит…» /
# «Выгода до 45% на новые авто…» вычищаются на Б/У-сайте и набор надо чем-то добрать.
# Те же инварианты: без Б/У-лексики и без «новых авто» (не режутся ни одним из двух фильтров),
# кредитный угол в каждой строке, ≤81 симв. (_RA_TEXT_MAX), без слова «автокредит» (в ТЕКСТАХ
# оно под запретом, в отличие от заголовков).
_NEUTRAL_CREDIT_TEXTS = [
    "Авто в кредит. Первый взнос 0 ₽. КАСКО на 1 год в подарок. Оставьте заявку.",
    "Кредит от 15 банков. Одобрение за 30 минут онлайн. Трейд-ин выше рынка.",
    "Господдержка 2026. Платеж от 9 000 ₽/мес. Заявка на кредит за 1 минуту.",
]


def _fallback_master_titles(brand: str, city: str, site_type: str, limit: int = 5) -> list[str]:
    """Безопасный добор для tp6/tp7, если строгие фильтры вычистили все заголовки."""
    brand = str(brand or "").strip()
    city = str(city or "").strip()
    if brand:
        raw = [
            # все строки с цифрой; «автокредит» в заголовках не блокируется, но в текстах — да
            f"{brand} в кредит. Первый взнос 0 ₽",
            f"Купить {brand} в кредит от 15 банков",
            f"{brand} с КАСКО на 1 год бесплатно",
            f"{brand} в наличии. Кредит от 9 000 ₽/мес",
            f"Одобрение за 30 минут онлайн. {brand} в кредит",
            f"{brand} по госпрограмме 2026. Взнос 0 ₽",
        ]
    else:
        raw = [
            # все строки с цифрой; разные УТП-бакеты
            "Авто в кредит. Первый взнос 0 ₽",
            "Новые авто в наличии. Кредит от 9 000 ₽/мес",
            "Автокредит от 15 банков. Решение онлайн",
            "КАСКО на 1 год бесплатно при кредите",
            "Господдержка 2026. Авто в кредит от 15 банков",
            "Трейд-ин выше рынка. Платеж от 9 000 ₽/мес",
        ]
    _, cities_bl = _title2_blocklist()
    out: list[str] = []
    seen: set[str] = set()

    def _collect(src: list) -> None:
        """Прогнать кандидатов через ПОЛНУЮ валидацию и добрать в `out` (до `limit`).
        Единственный путь наполнения — и для обычных кандидатов, и для floor'а: заголовок
        не может уехать в Директ, минуя site_type-фильтры, санитайз, обрезку по слову,
        стоп-листы и дедуп."""
        for t in _replace_foreign_city(
                _drop_new_car(_drop_used_car(list(src), site_type), site_type), city, cities_bl):
            if len(out) >= limit:
                return
            s = _trim_to_word(_sanitize_content(_strip_credit_rate(str(t)), 56), 56).rstrip()
            if not s or _is_bad_start(s) or _bad_ad_title(s):
                continue
            nk = _variant_norm_key(s)
            if nk and nk in seen:
                continue
            if nk:
                seen.add(nk)
            out.append(s)

    _collect(raw)
    # Этаж-гарант: если бренд-вариант вычистился в ноль (бренд-токен абсурден, напр. источник
    # «Авито»/«Дром» по ошибке прилетел как brand) — отдаём БЕЗ-брендовые общие заголовки.
    # Они всегда проходят фильтры → tp6/tp7 НИКОГДА не падает «нужен хотя бы один заголовок».
    if not out and brand:
        return _fallback_master_titles("", city, site_type, limit)
    # АНТИ-ПУСТОЙ ГЕЙТ: это последний рубеж tp6/tp7 — уехать пустым нельзя (позиция без заголовков
    # = кампания не создаётся / создаётся битой). Нейтральный пул не режется ни одним из двух
    # site_type-фильтров, поэтому floor держится на ЛЮБОМ типе сайта, включая «С пробегом».
    # ⚠️ Floor идёт ЧЕРЕЗ ТУ ЖЕ `_collect` (2026-07-19, находка ревью): раньше он возвращался
    # голым `return list(_NEUTRAL_CREDIT_TITLES)[:limit]` и обходил санитайз/стоп-листы — правка
    # любой строки пула не была бы поймана ничем перед отправкой в Директ.
    # Гарантия непустоты держится, пока валиден ХОТЯ БЫ ОДИН заголовок пула (сегодня валидны все 5);
    # если сломать разом все — floor осознанно вернёт [], т.е. ГРОМКИЙ отказ создания вместо
    # тихой отправки невалидного заголовка в live.
    if not out:
        _collect(list(_NEUTRAL_CREDIT_TITLES))
    return out

# «Хвосты»-УТП для добивки коротких заголовков до 45-56 симв. (правило Семёна: заполнять по максимуму).
# ⛔ БЕЗ %-ставки кредита/рассрочки (правило: «нельзя указывать % ставку кредита»).
# Sorted longest-first: при добивке до 48+ сначала пробуем самые длинные хвосты (плотнее заполняем).
_TITLE_TAILS = ("одобрение за 5 минут", "трейд-ин выше рынка", "без первого взноса",
                "подарки от салона", "одобрение онлайн", "КАСКО в подарок",
                "авто в наличии", "выгода до 45%", "господдержка",
                # Короткие хвосты (≤10 симв, баг #1): добивают заголовки 43-47 симв, куда длинные
                # УТП-хвосты не влезают (len+2+хвост>56) → раньше оставались с 9-13 пустыми символами.
                "рассрочка", "тест-драйв", "гарантия", "трейд-ин",
                # Сверхкороткие (≤7, 2026-07-07): остаток 9 симв (заголовок 47) с разделителем
                # «. » вмещает только хвост ≤7 — минимальный «гарантия» (8) не влезал, 1136
                # заголовков застревали на 47 симв (кейс psm: «…Первый взнос 0 ₽»=47).
                "выгодно", "онлайн")

_LOW_MONTHLY_PAYMENT_RE = re.compile(r"(?i)(?:от\s*)?(\d[\d\s\u00a0]{2,})\s*(?:₽|руб)?\s*/\s*мес")

_LOW_PAYMENT_TEXT_RE = re.compile(r"(?i)плат[её]ж\w*\s+от\s+(\d[\d\s\u00a0]{2,})\s*(?:₽|руб)")

_PAYMENT_VALUE_RE = re.compile(
    r"(?i)((?:ежемесячн\w+\s+)?плат[её]ж\w*\s+от\s+|(?:авто)?кредит\w*\s+от\s+|от\s+)"
    r"(\d[\d\s\u00a0]{2,})"
    r"(\s*(?:₽|руб\.?|рублей)?(?:\s*/\s*мес|\s+в\s+месяц)?)"
)

_CREDIT_PAYMENT_RANGE_RE = re.compile(
    r"(?i)\b((?:авто)?кредит\w*|плат[её]ж\w*)\b"
    r"[^.!?\n]{0,24}?\bот\s+(\d[\d\s\u00a0]{2,})\s*(?:₽|руб\.?|рублей)?\s*(?:/\s*мес|\bв\s+месяц\b)"
)

def _payment_value(m) -> int:
    return int(re.sub(r"\D", "", m.group(2)) or 0)

def _bad_credit_payment_range(s: str) -> bool:
    """Ежемесячный кредитный платёж в объявлениях держим в реалистичном коридоре 9-15 тыс."""
    for m in _CREDIT_PAYMENT_RANGE_RE.finditer(str(s or "")):
        n = int(re.sub(r"\D", "", m.group(2)) or 0)
        if n and not (9000 <= n <= 15000):
            return True
    return False

def _payment_amounts(lines: list[str]) -> list[str]:
    vals: list[str] = []
    for s in lines or []:
        for m in _CREDIT_PAYMENT_RANGE_RE.finditer(str(s or "")):
            n = int(re.sub(r"\D", "", m.group(2)) or 0)
            if 9000 <= n <= 15000:
                vals.append(m.group(2))
    return vals

def _apply_payment_amount(s: str, pay: str | None) -> str:
    if not pay or not s:
        return s

    def _rp(m):
        n = int(re.sub(r"\D", "", m.group(2)) or 0)
        if 9000 <= n <= 15000:
            return m.group(0).replace(m.group(2), pay, 1)
        return m.group(0)

    return _CREDIT_PAYMENT_RANGE_RE.sub(_rp, str(s))

def _coherent_payments(titles: list, texts: list, sitelinks: list) -> tuple[list, list, list, bool]:
    """Один ежемесячный платеж на всю UAC-кампанию: заголовки + тексты + быстрые ссылки."""
    flat = [str(x or "") for x in (titles or [])] + [str(x or "") for x in (texts or [])]
    for s in sitelinks or []:
        if isinstance(s, dict):
            flat.append(str(s.get("title") or ""))
            flat.append(str(s.get("description") or ""))
    vals = _payment_amounts(flat)
    if not vals:
        return titles, texts, sitelinks, False
    canon = vals[0]
    nt = [_apply_payment_amount(t, canon) for t in (titles or [])]
    nx = [_apply_payment_amount(x, canon) for x in (texts or [])]
    ns = [{"title": _apply_payment_amount(s.get("title", ""), canon),
           "description": _apply_payment_amount(s.get("description", ""), canon)}
          for s in (sitelinks or []) if isinstance(s, dict)]
    changed = (nt != list(titles or [])) or (nx != list(texts or [])) or (ns != list(sitelinks or []))
    return nt, nx, ns, changed

def _discount_pcts(lines: list[str]) -> list[str]:
    """Процентные скидки/выгоды в контенте. Для дублей расширений достаточно числа процента."""
    out: list[str] = []
    for s in lines or []:
        for m in _PCT_DISC_RE.finditer(str(s or "")):
            v = m.group(1)
            if v not in out:
                out.append(v)
    return out

def _dominant_discount_pct(lines: list[str]) -> str:
    """Most frequent percent in generated content, preserving first-seen tie order."""
    vals = []
    for s in lines or []:
        vals.extend(m.group(1) for m in _PCT_DISC_RE.finditer(str(s or "")))
    if not vals:
        return ""
    counts = {v: vals.count(v) for v in dict.fromkeys(vals)}
    return max(counts, key=counts.get)

def _fill_title(t: str, lo: int = 45, hi: int = 56) -> str:
    """Дотянуть заголовок до hi-2..hi симв., подклеивая УТП-хвосты БЕЗ обрезки слов и БЕЗ
    повтора уже упомянутого УТП. Если ни один хвост не влезает — оставляем как есть (но ≤hi).
    Целевой минимум = hi-2 (при hi=56 → 54, правило Семёна: остаток ≤2 у каждого заголовка).
    Разделитель смарт: заголовок на «.!?…» → « » (1 симв, добор для 47-симв. заголовков);
    иначе «. » (2 симв, правило Кудерко: дефис как разделитель недопустим)."""
    # _trim_clean: обрезка по слову + чистка оборванного хвоста («Одобрение за 30» —
    # live-кейс psm/ozge); числовой хвост чистится ТОЛЬКО если обрезали сами (см. text_norm).
    t = _trim_clean(t, hi)
    for f in _TITLE_TAILS:
        if len(t) >= hi - 2:          # правило Семёна: свободно ≤2 симв (hi-2 = 54 при hi=56)
            break
        kw = f.split()[0].lower().rstrip("%")
        if kw and kw in t.lower():
            continue                                 # не дублируем уже упомянутое (кредит/КАСКО/трейд-ин…)
        sep = " " if (t and t[-1] in ".!?…") else ". "  # смарт-разделитель: без двойной точки
        if len(t) + len(sep) + len(f) <= hi:
            t = t + sep + _cap_first(f)
    return _normalize_numeric_suffixes_bp(_trim_clean(t, hi))

def _brand_title_set(brand: str, city: str) -> list:
    """≤7 ПОЛНЫХ «вкусных» заголовков для группы по МАРКЕ - КАЖДЫЙ содержит марку И УТП (кредит,
    трейд-ин, КАСКО, скидка, выгодный кредит), а не бледное «BAIC». ЗАПОЛНЯЕМ длину 45-56 (правило Семёна:
    не оставлять места) - короткие добиваем УТП-хвостами через _fill_title.
    БАГ 9: кредитные УТП приоритетом (первый взнос, платеж, ставка, господдержка).
    БАГ 4→исправлен: разделитель «. » (точка), не дефис. БАГ 7: «0%» убрано (strip_credit_rate уберёт 0%).
    Не пишем «официального дилера» (правило пользователя). Длина ≤56.
    Мультигород (город через запятую) → без города (_content_city)."""
    city = _content_city(city)
    brand = _display_brand(brand)
    city_loc = _city_locative((city or "").strip()) if (city or "").strip() else ""
    loc = f" в {city_loc}" if city_loc else ""
    # Баг #2: МАРКА/МОДЕЛЬ первой (под автотаргетинг Яндекса — он матчит по началу заголовка),
    # затем кредит, затем остальное. Бренд-первые кандидаты идут вверх списка.
    cand = [
        f"{brand} в кредит{loc}. Первый взнос 0 ₽",              # марка первой + кредит
        f"{brand} с платежом от 9 000 ₽/мес{loc}. Кредит онлайн", # марка первой + платёж
        f"{brand} с КАСКО на 1 год{loc}. Трейд-ин в подарок",    # марка первой + подарки
        f"{brand} в трейд-ин{loc}. Оценка авто за 30 минут",     # бренд-подлежащее
        f"{brand} по госпрограмме{loc}. Господдержка 2026",      # бренд-подлежащее
        f"{brand} с выгодой до 45%{loc}. Покупка в кредит",      # марка первой + выгода/кредит
        f"Купить {brand}{loc}. КАСКО на 1 год бесплатно",        # «Купить»
        # Был «Новый {brand}{loc}. Одобрение за 30 минут» — на Б/У-сайте «Новый Haval» ВРЁТ.
        # Фильтром это не снять: `_NEW_RE` требует хвост «авто»/«автомобил», а тут МАРКА.
        # Site_type-параметра у функции нет намеренно — её зовёт и tp6-путь
        # (`create_set_master_product.py:175`), который site_type сюда не передаёт. Поэтому
        # кандидат сделан нейтральным БЕЗУСЛОВНО: «Новый» не нёс УТП (продаёт «Одобрение
        # за 30 минут»), а brand-first от снятия слова только выигрывает (DoD §2.1).
        f"{brand} с одобрением онлайн{loc}. Решение за 30 минут", # brand-first, нейтрально к site_type
    ]
    out: list = []
    for t in cand:
        ft = _fill_title(_strip_credit_rate(t), 45, 56)   # убрать %-ставку, потом добить до 45-56
        if ft and ft not in out:
            out.append(ft)
    # Последний рубеж: без «Новые {brand}» — тот же site_type-независимый мотив, что и выше.
    return out[:8] or [f"{brand}{loc}"[:56], brand]         # 8 = все #23-шаблоны, вкл. «Госпрограмма»

# Стемы крупных городов РФ (для матча в склонениях: «москв»→москва/москве; «казан»→казань/казани).
# Дополняет города из local_gsheet_sites — ловит ключи слепка с городами, где нет наших аккаунтов.
def _rsya_titles(brand: str, city: str, site_type: str, ai_title2: str = "",
                 base: list | None = None, pool: list | None = None, is_brand: bool = True,
                 cap: int = _RA_TITLES_CAP, slepok: str = "") -> list:
    """≤cap заголовков комбинаторного РСЯ. Группа по МАРКЕ (is_brand) → ВСЕ заголовки ПОЛНЫЕ и с
    маркой (`_brand_title_set`), без бледных дженериков; пул слепка — лишь добивка если не хватило.
    Группа «Общее» (тема, не марка) → бренд-шаблоны НЕ применяем, ведём пулом слепка. Чистка:
    не-Б/У сайт → без «б/у»; чужой город → город аккаунта; длина ≤56. Когерентность — в _responsive_ad."""
    # B2B-лидоген (dmp): вместо авто-заголовков — B2B-заголовки из агентского корпуса.
    # Авто-шаблоны (_brand_title_set, _GENERIC_TITLE_FILLERS) для dmp неприменимы.
    if site_type == "dmp":
        from . import ai_agents as _A  # lazy import во избежание кругового импорта
        _dmp_ads = (_A.AGENT_ADS.get("dmp") or {})
        _dmp_corpus = list(_dmp_ads.get("titles") or [])
        _b2b_fillers = [
            "До 150% горячих лидов для вашего бизнеса сегодня",
            "50+ горячих контактов для компании. Попробуй сейчас",
            "Получите 50+ горячих лидов для бизнеса за 24 часа",
            "Идентификация 1000+ клиентов для вашего бизнеса онлайн",
            "Контакты 100% заинтересованных клиентов за 24 часа",
            "Рост продаж на 150%. Горячие контакты для компании",
            "Источник клиентов. 50+ заявок в день для бизнеса",
        ]
        # base/pool из слепка тоже берём (ai_title2 — из M3 для этой конкретной группы)
        _b2b_base = [t for t in (list(base or []) + _dmp_corpus) if t]
        if ai_title2:
            _b2b_base = [str(ai_title2)[:56]] + _b2b_base
        _all = list(dict.fromkeys(_b2b_base + _b2b_fillers))
        return [t for t in _all if t][:cap]

    # terehov (#15, Семён 2026-07-11): город в шаблонных заголовках запрещён.
    # Обнуляем city ДО всех дальнейших операций, в т.ч. _brand_title_set и _replace_foreign_city.
    if slepok in _SLEPOKS_NO_CITY_TITLES:
        city = ""

    _, _cities_bl = _title2_blocklist()
    acc_city = (city or "").strip()

    def _cf(lst):
        return _replace_foreign_city(
            _drop_new_car(_drop_used_car(list(lst or []), site_type), site_type),
            acc_city, _cities_bl)

    if brand and is_brand:
        branded_base = [t for t in (list(base or [])) if t and _brand_in_text(t, brand)]
        branded_pool = [t for t in (list(pool or [])) if t and _brand_in_text(t, brand)]
        branded_ai = [str(ai_title2)[:56]] if (ai_title2 and _brand_in_text(ai_title2, brand)) else []
        primary = _brand_title_set(brand, city) + branded_base + branded_ai
        supp = branded_pool + _GENERIC_TITLE_FILLERS   # только если шаблонов < cap после дедупа
    else:
        primary = [t for t in (list(base or [])) if t]
        if ai_title2:
            primary.append(str(ai_title2)[:56])
        supp = list(pool or []) + _GENERIC_TITLE_FILLERS
    primary = _drop_foreign_brand_mentions(primary, brand)
    supp = _drop_foreign_brand_mentions(supp, brand)
    # Хвост `_GENERIC_TITLE_FILLERS` — сырой резерв мимо `_cf`: на Б/У-сайте протаскивал
    # «Кредит на новый авто…»/«Новые авто в наличии…». Прогоняем через `_cf` (site_type-фильтр).
    titles = _fill_variants(_cf(primary), _cf(supp) + _cf(_GENERIC_TITLE_FILLERS), cap)
    # убрать %-ставку кредита + тире/дефис-разделитель → точка + добить до 45-56; дедуп по норм-ключу (БАГ 2).
    out: list = []
    seen_keys: set = set()
    for t in titles:
        if not (t and str(t).strip()):
            continue
        if _bad_ad_title(str(t)):
            continue
        # БАГ 8: не берём заголовки начинающиеся с предлога или маленькой буквы
        if _is_bad_start(str(t)):
            continue
        _t0 = str(t)
        if brand and is_brand:
            # ДЕТЕРМИНИРОВАННЫЙ brand-first (§2.1): даже если базовый/пуловый AI-заголовок
            # «Кредит на BAIC …»/«Платеж … BAIC» — марка принудительно в начало ДО первой точки.
            _t0 = _brand_first_reorder(_t0, brand)
        s = _normalize_numeric_suffixes_bp(
            _fill_title(_replace_sep_hyphen(_replace_emdash(_strip_credit_rate(_t0))), 45, 56)
        )
        s = _strip_installment_text(s)
        if not s:
            continue
        # TITLE_TARGET_MIN gate (= 48): _fill_title может не добить до 48 когда ни один хвост
        # не вмещается (короткий бренд + город). Фильтруем — brand_fillers ниже компенсируют.
        if len(s) < 48:
            continue
        # БАГ 2: дедупликация по смысловому ключу (схлопывает числа: 57%==35%==скидка_до_#%)
        nk = _variant_norm_key(s)
        if nk and nk in seen_keys:
            continue
        if nk:
            seen_keys.add(nk)
        if s not in out:
            out.append(s)
    if brand and is_brand:
        own = _own_brand_tokens(brand)
        def _has_own_brand(v: str) -> bool:
            vl = str(v or "").lower()
            return any(re.search(r"(?<![a-zа-яё0-9])" + re.escape(tok) + r"(?![a-zа-яё0-9])", vl)
                       for tok in own)
        out = [t for t in out if _has_own_brand(t)]
    # АНТИ-AI ПРАВИЛО 1: фильтр штампов — пропускаем «широкий выбор», «надёжный» и т.п.
    # Если после фильтра не хватает — докидываем обратно нефильтрованные (не обнуляем набор).
    clean = [t for t in out if not _has_stamp(t)]
    if len(clean) < cap and len(clean) < len(out):
        stamps = [t for t in out if _has_stamp(t)]
        clean = (clean + stamps)[:cap]
    out = clean
    # АНТИ-AI ПРАВИЛО 3: не более 1 заголовка с одинаковым первым словом.
    out = _dedup_by_first_word(out)
    # АНТИ-AI ПРАВИЛО 2: чередование ритма (коротких/длинных) — только если набор полный (cap штук).
    if len(out) >= cap and not (brand and is_brand):
        out = _alternate_rhythm(out[:cap])
    if not (brand and is_brand) and len(out) < cap:
        # Финальный добор тонкой группы. Источник инцидента kryuchkova (Б/У): сырой
        # `_GENERIC_AT_TITLES` нёс «Купить новое авто…»/«Выгода до 45% на новые авто…».
        # `_cf` (=_drop_new_car∘_drop_used_car∘_replace_foreign_city) чистит по site_type;
        # после фильтра остаётся ≥5 нейтральных строк → пустым набор не станет.
        for cand in _cf(_GENERIC_AT_TITLES + _GENERIC_TITLE_FILLERS):
            s = _normalize_numeric_suffixes_bp(
                _fill_title(_replace_sep_hyphen(_replace_emdash(_strip_credit_rate(str(cand)))), 45, 56)
            )
            s = _strip_installment_text(s)
            if not s or _bad_ad_title(s) or _is_bad_start(s):
                continue
            nk = _variant_norm_key(s)
            if any(_variant_norm_key(x) == nk for x in out):
                continue
            out.append(s)
            if len(out) >= cap:
                break
    # Брендовая/модельная группа: первый заголовок обязан быть с этой маркой/моделью, а набор
    # должен добиваться до cap даже если AI-контент уровня кампании не содержал бренд и был
    # полностью вычищен фильтрами. Иначе tp1/tp2 могут схлопнуться до `[title, brand]`.
    if brand and is_brand:
        own = _own_brand_tokens(brand)

        def _has_own_brand_final(v: str) -> bool:
            vl = str(v or "").lower()
            return any(re.search(r"(?<![a-zа-яё0-9])" + re.escape(tok) + r"(?![a-zа-яё0-9])", vl)
                       for tok in own)

        brand_fillers = _brand_title_set(brand, city) + [
            f"{brand} в наличии. Кредит и оценка авто в трейд-ин",
            f"Оформите {brand} в кредит. Первый взнос 0 ₽",
            f"{brand} с выгодой по кредиту. Заявка онлайн",
            f"Купить {brand} в кредит. Решение банка за 30 минут",
            f"{brand} в кредит. КАСКО на 1 год при покупке",
        ]

        seed = ""
        for cand in brand_fillers:
            s = _normalize_numeric_suffixes_bp(
                _fill_title(_replace_sep_hyphen(_replace_emdash(
                    _strip_credit_rate(_brand_first_reorder(str(cand), brand)))), 45, 56)
            )
            if s and len(s) >= 48 and _has_own_brand_final(s) and not _bad_ad_title(s) and not _has_stamp(s):
                seed = s
                break

        if seed:
            seed_key = _variant_norm_key(seed)
            out = [seed] + [t for t in out if t != seed and _variant_norm_key(t) != seed_key]

        seen_keys = {_variant_norm_key(x) for x in out if _variant_norm_key(x)}
        for cand in brand_fillers:
            s = _normalize_numeric_suffixes_bp(
                _fill_title(_replace_sep_hyphen(_replace_emdash(
                    _strip_credit_rate(_brand_first_reorder(str(cand), brand)))), 45, 56)
            )
            if (not s or len(s) < 48 or _bad_ad_title(s) or _is_bad_start(s) or not _has_own_brand_final(s)):
                continue
            nk = _variant_norm_key(s)
            if nk and nk in seen_keys:
                continue
            out.append(s)
            if nk:
                seen_keys.add(nk)
            if len(out) >= cap:
                break
    return out[:cap]

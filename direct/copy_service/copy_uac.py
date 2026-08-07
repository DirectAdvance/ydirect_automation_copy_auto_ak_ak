"""UAC (Мастер кампаний / Товарные) копирование: чтение и сборка.

Вынесено из copy_engine.py (чистый code-motion, логика не изменена).
DI инъектится copy_engine.configure() фан-аутом; sibling-модули copy_* — прямой импорт (DAG, цикла нет).
"""
from __future__ import annotations

import json
import re
import base64
import time
from concurrent.futures import ThreadPoolExecutor

from ..core import campaign as cmc

from .copy_geo import _COPY_R_CODE_RE, _copy_apply_geo_replacements, _copy_normalize_campaign_name, _copy_target_href
from .copy_keyword_phrase import clean_keyword_phrase

# ── DI (инъектится copy_engine.configure фан-аутом; None до инъекции) ──
_direct_tokens = _resolve_agency_hint = _token_for_login = _v501_svc = None
_UAC_TITLE_MAX = 56
_UAC_TEXT_MAX = 81
_UAC_KEYWORD_MAX_POSITIVE_WORDS = 7
_UAC_FALLBACK_TITLES = [
    "Автомобили в наличии",
    "Выгода на авто",
    "Официальный дилер",
    "Авто с гарантией",
    "Подберите автомобиль",
]
_UAC_FALLBACK_TEXTS = [
    "Подберите автомобиль с выгодой. Оставьте заявку на сайте.",
    "Актуальные предложения на новые автомобили в наличии.",
    "Получите расчет и условия покупки у официального дилера.",
]


def configure(deps: dict) -> None:
    """Инъекция DI из blueprint (фан-аут из copy_engine.configure)."""
    globals().update(deps)


def _copy_is_uac_grid_row(row: dict) -> bool:
    typ = str(row.get("typename") or row.get("__typename") or row.get("type") or "").lower()
    name = str(row.get("name") or "").lower()
    return "uac" in typ or "tp6_" in name or "tp7_" in name


def _copy_uac_value(row: dict, *keys, default=None):
    for key in keys:
        val = row.get(key)
        if val not in (None, "", []):
            return val
    return default


def _copy_uac_strings(value, *keys: str, limit: int = 8) -> list[str]:
    """Extract UAC text arrays from either strings or browser dict rows."""
    vals = []
    if isinstance(value, dict):
        raw = _copy_uac_value(value, *keys, default=[])
    else:
        raw = value
    for item in (raw or []):
        if isinstance(item, dict):
            text = ""
            for key in ("text", "title", "value", "body", "name", "phrase", "keyword", "query"):
                val = item.get(key)
                if isinstance(val, (dict, list)):
                    continue
                text = str(val or "").strip()
                if text:
                    break
        else:
            text = str(item or "").strip()
        if text and text not in vals:
            vals.append(text)
        if len(vals) >= limit:
            break
    return vals


def _copy_uac_geo_text(text: str | None, geo_pairs: list[tuple[str, str]] | None) -> str:
    return _copy_apply_geo_replacements(text, list(geo_pairs or [])).strip()


def _copy_uac_geo_strings(values: list[str], geo_pairs: list[tuple[str, str]] | None) -> list[str]:
    out: list[str] = []
    for val in values or []:
        text = _copy_uac_geo_text(val, geo_pairs)
        if text and text not in out:
            out.append(text)
    return out


def _copy_uac_keyword_strings(values: list[str], geo_pairs: list[tuple[str, str]] | None) -> list[str]:
    out: list[str] = []
    for val in values or []:
        text = clean_keyword_phrase(
            _copy_uac_geo_text(val, geo_pairs),
            geo_pairs,
            max_positive_words=_UAC_KEYWORD_MAX_POSITIVE_WORDS,
        )
        if text and text not in out:
            out.append(text)
    return out


def _copy_uac_limit_strings(values: list[str], max_len: int) -> list[str]:
    out: list[str] = []
    for val in values or []:
        text = str(val or "").strip()
        if len(text) > max_len:
            cut = text[:max_len].rstrip()
            boundary = max(
                cut.rfind("."),
                cut.rfind("!"),
                cut.rfind("?"),
                cut.rfind(";"),
            )
            if boundary >= int(max_len * 0.55):
                cut = cut[:boundary].rstrip()
            else:
                space = cut.rfind(" ")
                if space >= int(max_len * 0.55):
                    cut = cut[:space].rstrip()
            text = cut.rstrip(" .,!?:;")
        if text and text not in out:
            out.append(text)
    return out


def _copy_uac_pad_strings(values: list[str], fallback: list[str], *, need: int, max_len: int) -> list[str]:
    """Fill UAC text arrays to verifier-required minimums without duplicates."""
    out = _copy_uac_limit_strings(values or [], max_len)
    seen = {v.casefold() for v in out}
    for raw in fallback or []:
        if len(out) >= need:
            break
        for val in _copy_uac_limit_strings([raw], max_len):
            key = val.casefold()
            if key and key not in seen:
                seen.add(key)
                out.append(val)
                break
    return out[:need]


def _copy_uac_keyword_tokens(phrase: str) -> tuple[str, list[str]]:
    """Split inline minus-words out of a UAC keyword phrase."""
    keep: list[str] = []
    minus: list[str] = []
    for raw in str(phrase or "").split():
        token = raw.strip()
        if not token:
            continue
        if token.startswith("-"):
            word = token.lstrip("-").strip(".,;:!?()[]{}\"'«»")
            if word:
                minus.append(word)
            continue
        keep.append(token)
    return " ".join(keep).strip(), minus


def _copy_uac_sanitize_keywords(
    keywords: list[str],
    minus_keywords: list[str],
) -> tuple[list[str], list[str]]:
    clean_keywords: list[str] = []
    clean_minus: list[str] = []
    seen_keywords: set[str] = set()
    seen_minus: set[str] = set()

    def add_keyword(value: str) -> None:
        text = clean_keyword_phrase(
            str(value or "").strip(),
            [],
            max_positive_words=_UAC_KEYWORD_MAX_POSITIVE_WORDS,
        )
        key = text.lower()
        if text and key not in seen_keywords:
            seen_keywords.add(key)
            clean_keywords.append(text)

    def add_minus(value: str) -> None:
        text = str(value or "").lstrip("-").strip(".,;:!?()[]{}\"'«»")
        key = text.lower()
        if text and " " not in text and key not in seen_minus:
            seen_minus.add(key)
            clean_minus.append(text)

    for phrase in keywords or []:
        cleaned, inline_minus = _copy_uac_keyword_tokens(str(phrase or ""))
        add_keyword(cleaned)
        for word in inline_minus:
            add_minus(word)

    for phrase in minus_keywords or []:
        cleaned, inline_minus = _copy_uac_keyword_tokens(str(phrase or ""))
        if inline_minus:
            for word in inline_minus:
                add_minus(word)
            continue
        if cleaned and cleaned == str(phrase or "").strip():
            for token in cleaned.split():
                add_minus(token)

    return clean_keywords, clean_minus


def _copy_uac_sitelinks(value, *, source_domain: str, target_domain: str,
                        geo_pairs: list[tuple[str, str]] | None = None) -> list[dict]:
    out = []
    for item in (value or []):
        if not isinstance(item, dict):
            continue
        title = _copy_uac_geo_text(item.get("title") or item.get("Title") or item.get("text") or "", geo_pairs)
        href = str(item.get("href") or item.get("Href") or item.get("url") or "").strip()
        desc = _copy_uac_geo_text(item.get("description") or item.get("Description") or "", geo_pairs)
        if not title:
            continue
        out.append({"title": title, "href": _copy_target_href(href, source_domain, target_domain), "description": desc})
    return out


def _copy_uac_geo_guard(name: str, text_fields: list[str], geo_pairs: list[tuple[str, str]] | None,
                        target_r_code: str = "") -> list[str]:
    """Fail before creating UAC if geo/r-code normalization did not actually land."""
    errors: list[str] = []
    target_r_code = str(target_r_code or "").strip()
    if target_r_code:
        bad_codes = sorted({m.group(0) for m in _COPY_R_CODE_RE.finditer(str(name or ""))
                            if m.group(0) != target_r_code})
        if bad_codes:
            errors.append(f"name содержит чужой r-код {bad_codes[0]} вместо {target_r_code}")

    source_terms: list[str] = []
    for old, new in geo_pairs or []:
        old_s = str(old or "").strip()
        new_s = str(new or "").strip()
        if old_s and old_s.casefold() != new_s.casefold() and old_s not in source_terms:
            source_terms.append(old_s)
    if not source_terms:
        return errors

    haystack = "\n".join([str(name or "")] + [str(x or "") for x in text_fields or []])
    for term in source_terms:
        if re.search(r"\b" + re.escape(term) + r"\b", haystack, re.IGNORECASE | re.UNICODE):
            errors.append(f"осталось исходное гео {term!r}")
            break
    return errors


def _copy_uac_create_live_guard(source_detail: dict, target_detail: dict, spec) -> list[str]:
    """Verify what UAC actually persisted after create, not only the pre-create spec."""
    errors: list[str] = []
    target_name = str(target_detail.get("display_name") or target_detail.get("name") or "").strip()
    expected_name = str(getattr(spec, "display_name", "") or "").strip()
    if expected_name and target_name != expected_name:
        errors.append(f"display_name mismatch: {target_name!r} != {expected_name!r}")

    target_href = str(target_detail.get("href") or "").strip()
    expected_href = str(getattr(spec, "href", "") or "").strip()
    if expected_href and target_href != expected_href:
        errors.append(f"href mismatch: {target_href!r} != {expected_href!r}")

    for key in ("titles", "texts", "keywords"):
        expected = list(getattr(spec, key, None) or [])
        if not expected:
            continue
        actual = _copy_uac_strings(target_detail, key, limit=max(200, len(expected)))
        if actual != expected:
            errors.append(f"{key} mismatch")

    source_hashes = _copy_uac_image_hashes(source_detail)
    target_hashes = _copy_uac_image_hashes(target_detail)
    if source_hashes and getattr(spec, "content_ids", None) and target_hashes != source_hashes:
        errors.append("image_hashes mismatch")
    return errors


# Какую долю отправленных минус-слов кабинет обязан сохранить, чтобы перенос считался
# состоявшимся. НЕ 100%: Директ приводит минус-слова к своим формам и СХЛОПЫВАЕТ совпавшие,
# поэтому их всегда чуть меньше отправленного (живой замер: 60→58, 31→30, 29→28 — 95-97%).
# Порог ловит настоящий отказ переноса (ноль вместо сорока, половина вместо всех), а не
# штатное схлопывание форм.
_UAC_MINUS_KEEP_RATIO = 0.7


def _copy_uac_verify_rows(source_detail: dict, live_detail: dict, spec, *,
                          source_id, target_id) -> list[dict]:
    """Измерения сверки для одной скопированной UAC-кампании (tp6/tp7).

    Зачем отдельно от ``_copy_uac_create_live_guard``: тот решает, принять ли создание
    (несовпало — кампания падает), но НИЧЕГО не оставляет в ``copy_verify``. Для v5-пути
    строится 25 измерений D1-D19, для чистого UAC — ноль, и джоба показывала «сверка не
    выполнялась» даже там, где guard всё проверил. Эти строки делают проверку видимой и
    добавляют то, чего guard не смотрит: счётчик, цель, регионы, фид, бюджет.

    Сверяем НАМЕРЕНИЕ (``spec``) с тем, что UAC реально сохранил (``live_detail``), а не
    источник с целью: счётчик, цель, гео и фид у копии намеренно ДРУГИЕ — это и есть смысл
    копирования в новый кабинет. С источником сравниваем только контент (картинки).

    Поле, которого нет в ответе UAC, даёт ``unreadable`` — fail-safe: отсутствие данных не
    выдаётся за расхождение (и не поднимает has_issues).
    """
    scope = f"uac:{target_id or source_id}"
    rows: list[dict] = []

    def _row(dimension: str, status: str, source, target, hint: str = "") -> None:
        rows.append({
            "scope": scope, "dimension": dimension, "status": status,
            "source": source, "target": target, "repairable": False,
            "repair_hint": hint,
        })

    def _cmp(dimension: str, expected, actual, *, hint: str = "") -> None:
        if actual is None:
            _row(dimension, "unreadable", expected, None,
                 hint or "поле не пришло в ответе UAC /campaign/<id>")
            return
        _row(dimension, "ok" if expected == actual else "mismatch", expected, actual, hint)

    def _num(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _ids(value):
        if not isinstance(value, list):
            return None
        out = []
        for item in value:
            num = _num(item)
            if num is not None:
                out.append(num)
        return sorted(out)

    # ── контент: то же, что проверяет guard, но записанное как измерение ──
    # ОБЕ стороны прогоняем через _copy_uac_strings: он схлопывает дубли и режет по limit.
    # Сырой spec против нормализованного live давал ложное расхождение на любом списке
    # с повтором — сравнивать нужно одинаково нормализованные значения.
    for key in ("titles", "texts", "keywords"):
        raw_expected = list(getattr(spec, key, None) or [])
        cap = max(200, len(raw_expected))
        expected = _copy_uac_strings(raw_expected, limit=cap)
        actual = _copy_uac_strings(live_detail, key, limit=cap) if live_detail else None
        _cmp(f"uac_{key}", expected, actual)

    source_hashes = _copy_uac_image_hashes(source_detail)
    target_hashes = _copy_uac_image_hashes(live_detail)
    # картинки — единственное, что сверяется с ИСТОЧНИКОМ: они переносятся 1:1 по хэшам
    _cmp("uac_images", len(source_hashes), len(target_hashes) if live_detail else None,
         hint="хэши картинок переносятся 1:1; расхождение = часть не перезалилась")

    # ── то, чего guard не смотрел ──
    # Формы полей взяты из живого ответа UAC /campaign/<id> (probe 2026-08-05, porg-rgwzgo57):
    # counters=[110881389] — СПИСОК; goals=[{goal_id:…, goal_type:…}] — список словарей;
    # feed_id=null у МК (фид есть только у товарных). Скалярное чтение давало unreadable
    # на всех 26 кампаниях прогона a7c535bd9ba6.
    counters_live = _copy_uac_value(live_detail, "counters", "counter_ids", default=None)
    expected_counter = _num(getattr(spec, "counter_id", None))
    if isinstance(counters_live, list):
        counter_ids = _ids(counters_live) or []
        _cmp("uac_counter", True, expected_counter in counter_ids if expected_counter else None,
             hint=f"счётчики кампании: {counter_ids}")
    else:
        _cmp("uac_counter", expected_counter, _num(counters_live))

    goals_live = _copy_uac_value(live_detail, "goals", default=None)
    expected_goal = _num(getattr(spec, "goal_id", None))
    if isinstance(goals_live, list):
        goal_ids = sorted({
            _num(g.get("goal_id") if isinstance(g, dict) else g)
            for g in goals_live
            if _num(g.get("goal_id") if isinstance(g, dict) else g) is not None
        })
        _cmp("uac_goal", True, expected_goal in goal_ids if expected_goal else None,
             hint=f"цели кампании: {goal_ids}")
    else:
        _cmp("uac_goal", expected_goal, _num(goals_live))

    _cmp("uac_regions", _ids(getattr(spec, "region_ids", None) or []),
         _ids(_copy_uac_value(live_detail, "regions", "region_ids", "regionIds")))

    # Фид: у МК (tp6) его нет вовсе — spec и live оба пустые, это НЕ «не прочитано».
    expected_feed = _num(getattr(spec, "feed_id", None))
    live_feed = _num(_copy_uac_value(live_detail, "feed_id", "listings_feed_id", "feedId"))
    if not expected_feed and live_feed is None:
        _row("uac_feed", "ok", None, None, "кампания без фида (МК) — сверять нечего")
    else:
        _cmp("uac_feed", expected_feed, live_feed)
    # Минус-слова сверяем ПО ОБЪЁМУ, а не пословно: сопоставить строки нельзя в принципе.
    # Живой замер (porg-rgwzgo57, 2026-08-05): «екатеринбург» хранится как «екатеринбурге»,
    # «машины» → «машина», «электроскутеры» → «электроскутер», «новый» → «нова»/«ново»,
    # «Лада» → «лад»; стоп-слова «в»/«на»/«от» → «!в»/«!на»/«!от». Нормализация Директа
    # непрозрачна и схлопывает совпавшие формы, поэтому и побуквенное сравнение (13 ложных
    # «потерь» из 375), и сравнение по общей основе (порог в 5 символов не берёт «нов»)
    # дают ложную тревогу на КАЖДОЙ кампании — а измерение, которое всегда красное,
    # перестают читать. Ловим то, что действительно проверяемо: минус-слова доехали и их
    # примерно столько же.
    raw_minus = _copy_uac_strings(list(getattr(spec, "minus_keywords", None) or []), limit=500)
    if live_detail is None:
        _cmp("uac_minus_keywords", len(raw_minus), None)
    else:
        stored = len(_copy_uac_strings(live_detail, "minus_keywords", limit=500))
        enough = stored >= round(len(raw_minus) * _UAC_MINUS_KEEP_RATIO)
        _row("uac_minus_keywords", "ok" if enough else "mismatch", len(raw_minus), stored,
             "Директ хранит минус-слова в своих формах и схлопывает совпавшие — точного "
             f"равенства не ждём; порог переноса {int(_UAC_MINUS_KEEP_RATIO * 100)}% отправленных")
    return rows


def _copy_uac_media_urls(row: dict, *, want: str) -> list[str]:
    """Find reusable media URLs in unstable UAC detail payloads."""
    rx = re.compile(r"https?://[^\s\"'<>]+", re.I)
    urls: list[str] = []
    seen: set[str] = set()
    image_ext = (".jpg", ".jpeg", ".png", ".webp", ".gif")
    video_ext = (".mp4", ".mov", ".webm")
    preferred_keys = {
        "image": ("images", "image_urls", "media", "contents", "content"),
        "video": ("videos", "video_urls", "media", "contents", "content"),
    }.get(want, ())

    def ok(url: str) -> bool:
        low = url.lower().split("?", 1)[0]
        if want == "video":
            return low.endswith(video_ext)
        return low.endswith(image_ext) or any(x in low for x in ("/image/", "/img/", "avatars.mds.yandex.net"))

    def walk(node):
        if isinstance(node, dict):
            for key, val in node.items():
                key_l = str(key).lower()
                if key_l in {"url", "href", "source_url", "preview_url", "download_url"} and isinstance(val, str):
                    for u in rx.findall(val):
                        if ok(u) and u not in seen:
                            seen.add(u); urls.append(u)
                else:
                    walk(val)
        elif isinstance(node, list):
            for val in node:
                walk(val)
        elif isinstance(node, str):
            for u in rx.findall(node):
                if ok(u) and u not in seen:
                    seen.add(u); urls.append(u)

    for key in preferred_keys:
        walk(row.get(key))
    return urls[:5 if want == "image" else 2]


def _copy_uac_content_media_urls(row: dict, *, want: str) -> list[str]:
    """Prefer explicit UAC content source URLs in the same order as the source campaign."""
    urls: list[str] = []
    seen: set[str] = set()
    image_ext = (".jpg", ".jpeg", ".png", ".webp", ".gif")
    video_ext = (".mp4", ".mov", ".webm")
    type_keys = ("type", "content_type", "contentType", "media_type", "mediaType")
    url_keys = ("source_url", "sourceUrl", "original_url", "originalUrl", "download_url", "url")
    limit = 5 if want == "image" else 2

    def ok_url(url: str) -> bool:
        low = url.lower().split("?", 1)[0]
        if want == "video":
            return low.endswith(video_ext)
        return low.endswith(image_ext) or any(x in low for x in ("/image/", "/img/", "avatars.mds.yandex.net"))

    def content_items(value):
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield item
        elif isinstance(value, dict):
            yield value

    for key in ("contents", "content"):
        for item in content_items(row.get(key)):
            raw_type = ""
            for type_key in type_keys:
                raw_type = str(item.get(type_key) or "").strip().lower()
                if raw_type:
                    break
            if raw_type and want not in raw_type:
                continue
            for url_key in url_keys:
                url = str(item.get(url_key) or "").strip()
                if url and ok_url(url) and url not in seen:
                    seen.add(url)
                    urls.append(url)
                    break
            if len(urls) >= limit:
                return urls
    return urls


def _copy_uac_campaign_href(row: dict, *, fallback_href: str, source_domain: str, target_domain: str) -> str:
    src_href = str(_copy_uac_value(
        row, "href", "target_url", "targetUrl", "direct_url", "directUrl", default="") or "").strip()
    if not src_href:
        return fallback_href
    copied = _copy_target_href(src_href, source_domain, target_domain)
    return copied or fallback_href


def _copy_uac_is_video_content(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    type_text = " ".join(
        str(item.get(k) or "").strip().lower()
        for k in ("type", "content_type", "contentType", "media_type", "mediaType")
    )
    if "video" in type_text:
        return True
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    if meta.get("creative_id") or meta.get("video_meta_id"):
        return True
    vast = str(meta.get("vast") or "")
    if "<vast" in vast.lower() or "video/mp4" in vast.lower():
        return True
    for key in ("source_url", "sourceUrl", "original_url", "originalUrl", "download_url", "url", "thumb"):
        url = str(item.get(key) or "").strip().lower().split("?", 1)[0]
        if url.endswith((".mp4", ".mov", ".webm")):
            return True
    return False


def _copy_uac_content_ids(row: dict) -> list[str]:
    """Source UAC content ids from campaign detail.

    Video content ids are account-local UAC extensions. Reusing a source video id
    in the target account makes create fail with VIDEO_EXTENSION_NOT_FOUND, so
    videos must go through ``video_urls`` and be uploaded by the target client.

    Image ids are account-local too for copy purposes. They are exposed as
    direct image hashes in some UAC errors, but passing them to another account
    can still fail with BannerDefectIds.Gen.IMAGE_NOT_FOUND. Copy creation must
    upload images into the target account instead of reusing this list.
    """
    ids: list[str] = []
    for item in (row.get("contents") or []):
        if not isinstance(item, dict):
            continue
        if _copy_uac_is_video_content(item):
            continue
        cid = str(item.get("id") or "").strip()
        if cid and cid not in ids:
            ids.append(cid)
    return ids


def _copy_uac_target_image_urls(row: dict) -> list[str]:
    """Image URLs to upload into the target account for UAC copy.

    Source ``content_id`` values are not portable across accounts. Prefer the
    original image URL from campaign detail; fall back to broader media scan.
    """
    return _copy_uac_content_media_urls(row, want="image") or _copy_uac_media_urls(row, want="image")


def _copy_uac_image_hashes(row: dict) -> list[str]:
    hashes: list[str] = []
    for item in (row.get("contents") or []):
        if not isinstance(item, dict):
            continue
        if _copy_uac_is_video_content(item):
            continue
        if str(item.get("type") or "").strip().lower() != "image":
            continue
        h = str(item.get("direct_image_hash") or item.get("image_hash") or item.get("hash") or "").strip()
        if h and h not in hashes:
            hashes.append(h)
    return hashes


def _copy_uac_filter_list(value, *, target_login: str = "", target_feed_id: int | None = None,
                          listing: bool = False) -> list[dict]:
    if isinstance(value, list):
        rows = value
    else:
        return []
    if not target_login or not target_feed_id:
        return rows

    try:
        from ..create import create_set_feeds as _feeds
        fields = set(_feeds._feed_filter_fields(target_login, int(target_feed_id)) or [])
        brand_field = _feeds._resolve_feed_field(target_login, int(target_feed_id), "brand") or "vendor"
        model_field = _feeds._resolve_feed_field(target_login, int(target_feed_id), "model") or "model"
        name_field = _feeds._resolve_feed_field(target_login, int(target_feed_id), "name") or "name"
    except Exception:  # noqa: BLE001 - best-effort normalization
        fields = set()
        brand_field, model_field, name_field = "vendor", "model", "name"

    def _values(cond: dict):
        vals = cond.get("values")
        if vals is None:
            vals = cond.get("value")
        if vals is None:
            vals = cond.get("stringValue")
        if vals is None:
            vals = cond.get("Arguments")
        if isinstance(vals, str):
            try:
                parsed = json.loads(vals)
                vals = parsed
            except Exception:  # noqa: BLE001
                pass
        if isinstance(vals, (list, tuple, set)):
            return [v for v in vals if str(v).strip()]
        return [vals] if vals not in (None, "") else []

    def _target_field(raw: str) -> str:
        field = str(raw or "").strip()
        low = field.lower()
        if listing:
            return "collectionId" if low in ("collectionid", "collection_id") else field
        if low in ("mark_id", "vendor", "vendorname", "manufacturer", "brand", "make"):
            return brand_field
        if low in ("folder_id", "model", "modelname", "model_name", "modification"):
            return model_field
        if low == "name":
            return name_field
        return field

    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        conditions: list[dict] = []
        for cond in row.get("conditions") or []:
            if not isinstance(cond, dict):
                continue
            src_field = cond.get("field") or cond.get("fieldName") or cond.get("operand") or cond.get("name")
            field = _target_field(str(src_field or ""))
            if not field:
                continue
            # Если Grid отдал fieldsForUseAs target-фида, не тащим заведомо несовместимое поле
            # в UAC create: иначе получаем HTTP 400 UNKNOWN_FIELD и теряем всю tp7-кампанию.
            if fields and field not in fields and field != "collectionId":
                continue
            values = _values(cond)
            if not values:
                continue
            operator = str(cond.get("operator") or cond.get("Operator") or "").strip() or "CONTAINS"
            norm = {"field": field, "operator": operator}
            norm["values"] = values
            norm["value"] = json.dumps(values, ensure_ascii=False)
            conditions.append(norm)
        if conditions:
            out.append({"conditions": conditions})
    return out


def _copy_uac_effective_feed_id(src_feed_raw, target_feed_id: int | None, feed_map: dict | None,
                                *, is_product: bool) -> int | None:
    if not is_product:
        return None
    eff_target_feed = target_feed_id
    try:
        source_feed_key = str(int(src_feed_raw)) if src_feed_raw not in (None, "") else ""
    except (TypeError, ValueError):
        source_feed_key = ""
    if feed_map and source_feed_key and source_feed_key in feed_map:
        eff_target_feed = feed_map[source_feed_key]
    if not eff_target_feed and feed_map and not source_feed_key:
        targets = []
        seen: set[int] = set()
        for raw in feed_map.values():
            try:
                fid = int(raw)
            except (TypeError, ValueError):
                continue
            if fid > 0 and fid not in seen:
                seen.add(fid)
                targets.append(fid)
        if len(targets) == 1:
            eff_target_feed = targets[0]
    return int(eff_target_feed or 0) or None


def _copy_uac_campaigns(source_login: str, target_login: str, target_agency: str,
                        selected_grid_rows: list[dict], body: dict, *,
                        target_href: str, region_ids: list[int], counter_id: int,
                        goal_id: int, target_feed_id: int | None,
                        feed_map: dict | None = None,
                        geo_pairs: list[tuple[str, str]] | None = None,
                        target_r_code: str = "") -> dict:
    """Recreate selected UAC/tp6/tp7 campaigns from source detail into target account."""
    rep = {"created": 0, "results": [], "errors": [], "uses_direct_units": False}
    rows = [r for r in selected_grid_rows if _copy_is_uac_grid_row(r)]
    if not rows:
        return rep
    try:
        from ..clients.uac_read import UacReadClient
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"uac init: {str(e)[:220]}")
        return rep

    default_cpa = int(body.get("cpa") or 2000)
    default_budget = int(body.get("week_budget") or body.get("budget") or 5000)

    def _uac_auth_expired(exc: Exception) -> bool:
        text = (str(getattr(exc, "body", "") or exc) or "").lower()
        return (
            "need_reset" in text
            or "passport.yandex" in text
            or "истек срок" in text
            or "истёк срок" in text
            or "<title>log in</title>" in text
        )

    # image_mode=upload: картинки берём из ЦЕЛЕВОГО аккаунта (уже залиты copy_other._copy_images_upload).
    # Иначе upload_content качает файл источника → одинаковый хэш и бренд-тема источника во всех копиях.
    tgt_img_urls: list[str] = []
    if str(body.get("image_mode") or "") == "upload":
        hashes = [str(h).strip() for h in (body.get("image_hashes") or []) if str(h).strip()]
        if hashes:
            try:
                _tt, _ = _token_for_login(
                    target_login, target_agency or _resolve_agency_hint(target_login, ""), _direct_tokens())
            except Exception:  # noqa: BLE001
                _tt = None
            for i in range(0, len(hashes), 100) if _tt else []:
                try:
                    data = _v501_svc("adimages", "get", _tt, target_login,
                                     {"SelectionCriteria": {"AdImageHashes": hashes[i:i + 100]},
                                      "FieldNames": ["AdImageHash", "OriginalUrl"]})
                except Exception:  # noqa: BLE001
                    continue
                for im in ((data.get("result") or {}).get("AdImages") or []):
                    u = str(im.get("OriginalUrl") or "").strip()
                    if u and u not in tgt_img_urls:
                        tgt_img_urls.append(u)

    def _read_detail(row: dict) -> tuple[int, dict | None, str]:
        try:
            src_id = int(row.get("id") or 0)
        except (TypeError, ValueError):
            src_id = 0
        if src_id <= 0:
            return src_id, None, "source_id пуст"
        try:
            # Отдельный reader на worker: не шарим cookie/session между потоками.
            return src_id, UacReadClient(source_login).campaign_detail(src_id), ""
        except Exception as e:  # noqa: BLE001
            return src_id, None, str(e)[:220]

    details_by_src: dict[int, dict] = {}
    detail_errors: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=min(2, len(rows))) as pool:
        for src_id, detail, err in pool.map(_read_detail, rows):
            if detail is not None:
                details_by_src[src_id] = detail
            elif src_id > 0:
                detail_errors[src_id] = err or "detail пуст"

    pending_creates: list[tuple[int, int, str, object]] = []
    early_results: dict[int, dict] = {}

    for cidx, row in enumerate(rows):
        try:
            src_id = int(row.get("id") or 0)
        except (TypeError, ValueError):
            src_id = 0
        raw_name = str(row.get("name") or "").strip() or f"copy-uac-{src_id}"
        name = _copy_normalize_campaign_name(raw_name, list(geo_pairs or []), target_r_code)
        if src_id <= 0:
            continue
        try:
            if src_id in detail_errors:
                raise RuntimeError(f"uac detail: {detail_errors[src_id]}")
            d = details_by_src.get(src_id)
            if not d:
                raise RuntimeError("uac detail пуст")
            source_domain = str(body.get("_copy_source_domain") or "").strip()
            target_domain = str(body.get("target_domain") or "").strip()
            titles = _copy_uac_limit_strings(
                _copy_uac_geo_strings(_copy_uac_strings(d, "titles", "title_items", limit=5), geo_pairs),
                _UAC_TITLE_MAX,
            )
            texts = _copy_uac_limit_strings(
                _copy_uac_geo_strings(_copy_uac_strings(d, "texts", "text_items", limit=3), geo_pairs),
                _UAC_TEXT_MAX,
            )
            sitelinks = _copy_uac_sitelinks(_copy_uac_value(d, "sitelinks", default=[]) or [],
                                            source_domain=source_domain, target_domain=target_domain,
                                            geo_pairs=geo_pairs)
            keywords = _copy_uac_keyword_strings(_copy_uac_strings(d, "keywords", limit=200), geo_pairs)
            minus_keywords = _copy_uac_geo_strings(_copy_uac_strings(d, "minus_keywords", limit=200), geo_pairs)
            keywords, minus_keywords = _copy_uac_sanitize_keywords(keywords, minus_keywords)
            audiences = _copy_uac_value(d, "audiences", "interest_ids", default=[]) or []
            pricing = str(_copy_uac_value(d, "pricing", "payment_type", "paymentType", default="PER_CLICK") or "PER_CLICK")
            week_limit = _copy_uac_value(d, "week_limit", "weekly_budget", "weekBudget", default=default_budget)
            cpa = default_cpa
            goals = _copy_uac_value(d, "goals", default=[]) or []
            if isinstance(goals, list) and goals and isinstance(goals[0], dict):
                cpa = int(goals[0].get("cpa") or cpa)
            titles = _copy_uac_pad_strings(titles, _UAC_FALLBACK_TITLES, need=5, max_len=_UAC_TITLE_MAX)
            texts = _copy_uac_pad_strings(texts, _UAC_FALLBACK_TEXTS, need=3, max_len=_UAC_TEXT_MAX)
            geo_guard_errors = _copy_uac_geo_guard(
                name,
                titles + texts + keywords + minus_keywords
                + [str(s.get("title") or "") for s in sitelinks if isinstance(s, dict)]
                + [str(s.get("description") or "") for s in sitelinks if isinstance(s, dict)],
                geo_pairs,
                target_r_code,
            )
            if geo_guard_errors:
                raise RuntimeError("uac geo guard: " + "; ".join(geo_guard_errors[:3]))
            src_feed_raw = _copy_uac_value(d, "feed_id", "listings_feed_id")
            is_product = name.lower().startswith("tp7_") or bool(src_feed_raw)
            feed_id = _copy_uac_effective_feed_id(
                src_feed_raw, target_feed_id, feed_map, is_product=is_product)
            # Детерминированный round-robin: у каждой i-й МК свои 5 картинок из архива цели.
            if tgt_img_urls:
                _img = [tgt_img_urls[(cidx * 5 + k) % len(tgt_img_urls)] for k in range(5)]
                _content_ids: list[str] = []
            else:
                _content_ids = []
                _img = _copy_uac_target_image_urls(d)
            copy_href = _copy_uac_campaign_href(
                d, fallback_href=target_href, source_domain=source_domain, target_domain=target_domain)
            # socdem источника, иначе датакласс молча подставит дефолт age_18 вместо возраста источника.
            _sd = _copy_uac_value(d, "socdem", default={}) or {}
            if not isinstance(_sd, dict):
                _sd = {}
            # Таргетинг-поля источника: не передашь — датакласс молча подставит свой дефолт (как было с socdem).
            _dev = _copy_uac_value(d, "device_types", default=[]) or []
            _dev = [str(x).strip() for x in _dev if str(x).strip()] if isinstance(_dev, list) else []
            _mreg_raw = _copy_uac_value(d, "minus_regions", "minus_region_ids", default=[]) or []
            _mreg: list[int] = []
            for _r in (_mreg_raw if isinstance(_mreg_raw, list) else []):
                try:
                    _mreg.append(int(_r))
                except (TypeError, ValueError):
                    continue
            _rm = _copy_uac_value(d, "relevance_match", default={}) or {}
            if not isinstance(_rm, dict):
                _rm = {}
            _rm_cats = [str(c).strip() for c in (_rm.get("categories") or []) if str(c).strip()]
            _ttg = _copy_uac_value(d, "time_target", default={}) or {}
            if not isinstance(_ttg, dict):
                _ttg = {}
            try:
                _tz = int(_ttg.get("id_time_zone") or 130)
            except (TypeError, ValueError):
                _tz = 130
            _extra = {"relevance_match_categories": _rm_cats} if _rm_cats else {}
            spec = cmc.MasterCampaignSpec(
                href=copy_href,
                titles=titles[:5],
                texts=texts[:3],
                region_ids=region_ids,
                counter_id=int(counter_id),
                goal_id=int(goal_id),
                cpa=cpa,
                week_budget=float(week_limit or default_budget),
                campaign_type=("product" if is_product else "master"),
                feed_id=feed_id,
                listings_feed_id=feed_id,
                feed_filters=_copy_uac_filter_list(
                    d.get("feed_filters"), target_login=target_login, target_feed_id=feed_id, listing=False),
                listings_feed_filters=_copy_uac_filter_list(
                    d.get("listings_feed_filters"), target_login=target_login, target_feed_id=feed_id, listing=True),
                display_name=name,
                pricing=pricing,
                keywords=keywords,
                minus_keywords=minus_keywords or ["отзывы"],
                sitelinks=sitelinks,
                content_ids=_content_ids,
                image_urls=_img,
                video_urls=_copy_uac_content_media_urls(d, want="video") or _copy_uac_media_urls(d, want="video"),
                audiences=audiences if isinstance(audiences, list) else [],
                genders=_sd.get("genders") or ["female", "male"],
                age_lower=str(_sd.get("age_lower") or "age_18"),
                age_upper=str(_sd.get("age_upper") or "age_inf"),
                limit_period=str(_copy_uac_value(d, "limit_period", "limitPeriod", default="week") or "week"),
                device_types=_dev or ["all"],
                minus_regions=_mreg,
                id_time_zone=_tz,
                # Наш стандарт tp6/tp7 (create_set_master_product.py:692, коды uac_verifier
                # UAC_ALTERNATIVE_TEXTS_ENABLED / UAC_MAPS_ENABLED) — сильнее «копии 1:1».
                alternative_texts_enabled=False,
                ml_banners_enabled=False,
                yandex_maps_enabled=False,
                utm_template=cmc.UTM_TEMPLATE,
                **_extra,
            )
            pending_creates.append((cidx, src_id, name, spec))
        except Exception as e:  # noqa: BLE001
            msg = str(e)[:260]
            rep["errors"].append(f"{name}: {msg}")
            early_results[cidx] = {"ok": False, "name": name, "source_id": src_id, "error": msg}

    def _create_one(item: tuple[int, int, str, object]) -> tuple[int, dict, str]:
        cidx, src_id, name, spec = item
        try:
            # Отдельный UAC client на worker: не шарим session/csrf между потоками.
            client = cmc.build_client(target_login, account=(target_agency or None))
            try:
                cid = client.create_master_campaign(spec, launch=False)
            except Exception as e:  # noqa: BLE001
                if not _uac_auth_expired(e):
                    raise
                client = cmc.build_client(
                    target_login,
                    account=(target_agency or None),
                    force_refresh=True,
                )
                cid = client.create_master_campaign(spec, launch=False)
            try:
                from ..clients.uac_read import _unwrap as _uac_unwrap
            except Exception:  # noqa: BLE001
                _uac_unwrap = lambda data: (data.get("result") if isinstance(data, dict) else {}) or data or {}
            live_errors: list[str] = []
            for attempt in range(3):
                live = _uac_unwrap(client._request(
                    "GET", f"/campaign/{int(cid)}", step=f"uac-copy-live-guard:{int(cid)}"))
                live_errors = _copy_uac_create_live_guard(details_by_src.get(src_id) or {}, live, spec)
                if not live_errors:
                    break
                if attempt < 2:
                    time.sleep(2.0)
            if live_errors:
                raise RuntimeError("uac live guard: " + "; ".join(live_errors[:3]))
            # Сколько РАЗНЫХ картинок физически было доступно позиции — эталон пула для
            # live-верификатора (uac_verifier.verify_uac_detail: `images_pool`, читается из
            # result-строки в live_verifier). В upload-режиме round-robin из tgt_img_urls даёт
            # spec.image_urls из 5 url, но уникальных ровно len(tgt_img_urls); в фолбэке —
            # уникальные из spec.image_urls. Без пула копия UAC с коротким пулом целевых картинок
            # (<5 уникальных → Директ схлопывает дубли) отбивается жёстким UAC_IMAGES_LOW и роняет
            # verification gate, хотя «взяли всё, что было» — не дефект (симметрично create-пути
            # create_set_master_product `_res["images_pool"]`).
            _images_pool = (
                len(tgt_img_urls) if tgt_img_urls
                else len({str(u).strip() for u in (getattr(spec, "image_urls", None) or []) if str(u).strip()})
            )
            return cidx, {
                "ok": True,
                "id": int(cid),
                "campaign_id": int(cid),
                "name": name,
                "kind": "uac",
                "images_pool": int(_images_pool),
                "source_id": src_id,
                # измерения сверки этой кампании — без них чистый UAC-прогон выглядит
                # как «сверка не выполнялась», хотя live guard выше всё проверил
                "verify_rows": _copy_uac_verify_rows(
                    details_by_src.get(src_id) or {}, live, spec,
                    source_id=src_id, target_id=int(cid)),
            }, ""
        except Exception as e:  # noqa: BLE001
            msg = str(e)[:260]
            return cidx, {"ok": False, "name": name, "source_id": src_id, "error": msg}, f"{name}: {msg}"

    if pending_creates:
        with ThreadPoolExecutor(max_workers=min(2, len(pending_creates))) as pool:
            for cidx, result, err in pool.map(_create_one, pending_creates):
                early_results[cidx] = result
                if result.get("ok"):
                    rep["created"] += 1
                if err:
                    rep["errors"].append(err)
    rep["results"] = [early_results[i] for i in sorted(early_results)]
    return rep

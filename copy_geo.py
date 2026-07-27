"""Гео-слой копирования: гео-замены, ремап r-кода кодера, нормализация имён/href.

Вынесено из copy_engine.py (чистый code-motion, логика не изменена).
DI инъектится copy_engine.configure() фан-аутом; sibling-модули copy_* — прямой импорт (DAG, цикла нет).
"""
from __future__ import annotations

import re

from .llm_providers import _m3_complete, _m3_llm_probe

# ── DI (инъектится copy_engine.configure фан-аутом; None до инъекции) ──
_account_ctx = _geo_id = _resolve_region = None

# Алиасы регионов (для гео-замены в текстах). Не раздувать: только явные из реальных РК.
_REGION_ALIASES: dict[str, list[str]] = {
    "кемеровская область": ["Кузбасс"],
    "ханты-мансийский автономный округ — югра": ["Югра", "Ханты-Мансийский округ"],
    "ямало-ненецкий автономный округ": ["Ямал"],
}
# Яндекс-API может вернуть тире (–/-/―) вместо em-dash из dict-ключа → нормализуем при lookup.
_REGION_ALIAS_DASH_RE = re.compile(r"[‒–—―]")


def _norm_region_alias_key(s: str) -> str:
    """lower + ё→е + любой тире→дефис-минус (совместимость ключей API с dict-ключами)."""
    return _REGION_ALIAS_DASH_RE.sub("-", s.lower().replace("ё", "е"))


_REGION_ALIASES_NORM: dict[str, list[str]] = {
    _norm_region_alias_key(k): v for k, v in _REGION_ALIASES.items()
}


def configure(deps: dict) -> None:
    """Инъекция DI из blueprint (фан-аут из copy_engine.configure)."""
    globals().update(deps)


def _copy_canonical_region_name(region: str) -> str:
    """Normalize Direct/Victory region labels for copied campaign names."""
    text = (region or "").strip()
    low = text.lower().replace("ё", "е")
    if low in {"башкортостан, республика", "республика башкортостан"}:
        return "Республика Башкортостан"
    return text


def _copy_geo_id_for_target(city: str | None, region: str | None) -> tuple[int | None, str | None]:
    """Geo for copy-flow.

    For campaign copy we target the account's business region, not the city
    prefill. Example: Ufa accounts must copy to Republic of Bashkortostan
    (11111), while generic create_set still keeps city-first _geo_id().
    """
    region_name = _copy_canonical_region_name(region or "")
    # Страновой таргетинг: РФ/Россия/Russia → GeoRegionId России (225). Мультигород-аккаунты
    # префилл сводит к 225 (account_service.py:623, «регион не распознан по городу → Россия»),
    # но текст региона уходит как «РФ»/«Россия» — резолвим его тем же id, иначе city-мультистрока
    # («Краснодар, Нижний Новгород, …») не матчится и копирование падает (не найден GeoRegionId).
    if (region_name or "").strip().lower().replace("ё", "е") in {
        "рф", "россия", "russia", "ru", "российская федерация",
    }:
        return 225, "Россия"
    if region_name:
        gid, used = _geo_id(None, region_name)
        if gid:
            return gid, used
    return _geo_id(city, region_name or region)


def _copy_ctx(login: str) -> dict:
    try:
        ctx = _account_ctx(login) or {}
    except Exception:  # noqa: BLE001 - source login may be outside local_gsheet_sites
        ctx = {}
    return {
        "domain": (ctx.get("domain") or "").strip(),
        "city": (ctx.get("city") or "").strip(),
        "region": (ctx.get("oblast") or ctx.get("region") or "").strip(),
        "geoid": ctx.get("geoid"),
    }


def _copy_m3_decliner():
    """Callable для copy_geo_morph: messages -> (text, err). temperature=0, короткий таймаут,
    2 попытки (склонение — быстрая задача, долго ждать M3 в copy-job'е не нужно)."""
    def _call(messages):
        return _m3_complete(messages, max_tokens=220, temperature=0.0, tries=2, backoff=4.0, timeout=60)
    return _call


def _copy_build_geo(source_ctx: dict, target_city: str, target_region: str, log=None):
    """Строит морфологические пары гео-замены (все падежи) через M3 + метадату.

    Возвращает (pairs, meta). pairs — list[(old, new)] по падежам, отсортировано по длине убыв.
    meta — {m3_used, m3_failed, source_forms (для residual), pairs_count}. Фолбзк на именительный
    (по границам слов) внутри copy_geo_morph, если M3 недоступен/невалиден — job не падает."""
    from . import copy_geo_morph as cgm
    target_city = (target_city or "").strip()
    target_region = (target_region or "").strip()
    src_city = (source_ctx.get("city") or "").strip()
    src_region = (source_ctx.get("region") or "").strip()
    geo_map: list[tuple[str, str]] = []
    if src_city:
        geo_map.append((src_city, target_city or target_region))
    if src_region:
        geo_map.append((src_region, target_region or target_city))
        # Задача 2 (Minor #14-M1): неофициальные/разговорные алиасы региона источника.
        # При копировании из Кузбасса слово «Кузбасс» остаётся в именах, т.к. морф знает
        # только официальную «Кемеровская область». Добавляем алиасы явной парой.
        _src_region_low = _norm_region_alias_key(src_region)
        for _alias in (_REGION_ALIASES_NORM.get(_src_region_low) or []):
            geo_map.append((_alias, target_region or target_city))
    # Частый случай: источник вне local_gsheet_sites, но в названиях/текстах реально фигурирует Краснодар.
    if "краснодар" not in f"{target_city} {target_region}".lower():
        geo_map.append(("Краснодар", target_city or target_region))
        geo_map.append(("Краснодарский край", target_region or target_city))
    _log = log or (lambda _m: None)
    # Пробуем M3 только если LLM жива (иначе paradigm_for отдаст закэшированное, а несозданное — фолбэк).
    try:
        m3 = _copy_m3_decliner() if _m3_llm_probe() else None
    except Exception:  # noqa: BLE001
        m3 = None
    return cgm.build_geo_pairs(geo_map, m3_complete=m3, log=_log)


def _copy_geo_replacements(source_ctx: dict, target_city: str, target_region: str, log=None) -> list[tuple[str, str]]:
    pairs, _meta = _copy_build_geo(source_ctx, target_city, target_region, log=log)
    return pairs


def _copy_apply_geo_replacements(text: str | None, replacements: list[tuple[str, str]]) -> str:
    from . import copy_geo_morph as cgm
    out, _n = cgm.apply_replacements(text, replacements or [])
    return out


_COPY_R_CODE_RE = re.compile(r"(?<=_)r\d{4}(?=_)")


def _copy_target_region_code(target_city: str, target_region: str) -> str:
    """Целевой r-код кодера (ag_part4) по гео target-аккаунта. Один источник для кампаний и групп.

    Использует DI'd _resolve_region(city) -> (r_code, oblast) (create_set_plan). Возвращает валидный
    r#### ТОЛЬКО если он определён и не плейсхолдер r0000 — иначе '' (ремап пропускается, чтобы не
    затирать исходный код неопределённым плейсхолдером). None-DI (standalone без wiring) → ''."""
    if not _resolve_region:
        return ""
    for probe in (target_city, target_region):
        probe = (probe or "").strip()
        if not probe:
            continue
        try:
            r_code, _oblast = _resolve_region(probe)
        except Exception:  # noqa: BLE001 — резолв региона best-effort, ремап не критичен для create
            r_code = ""
        r_code = str(r_code or "").strip()
        if re.fullmatch(r"r\d{4}", r_code) and r_code != "r0000":
            return r_code
    return ""


def _copy_remap_region_code(name: str | None, target_r_code: str) -> str:
    """Перекодировать r-сегмент кодера (`_r0300_` → `_<target_r_code>_`) в имени кампании/группы.

    Баги 1/4: гео-морфология меняет только словоформы (\\b), но регион в кодере зашит КОДОМ
    (`ag_part4`, напр. r0300=Краснодарский край) — код словами не задеть. Здесь ремапим сам код на
    r-код target-региона. target_r_code пуст/невалиден → имя без изменений (безопасно)."""
    text = str(name or "")
    if not text or not re.fullmatch(r"r\d{4}", str(target_r_code or "")):
        return text
    return _COPY_R_CODE_RE.sub(target_r_code, text)


def _copy_remap_snapshot_region_code(src_dir, target_r_code: str) -> dict:
    """Переписать r-сегмент кодера в именах ГРУПП и КАМПАНИЙ снимка (v5-ветка копирования).

    Гео-переписывание снимка меняет только словоформы; регион в кодере зашит КОДОМ (`ag_part4`)
    и словами не задевается, а direct_copy.phase_upload льёт имя группы как есть
    (direct_copy.py:1453) → на цели оставался r ИСТОЧНИКА. Здесь правим снимок ДО upload:
    единственный писатель имён в v5-ветке — это файлы снимка.

    → {"adgroups": N, "campaigns": M} — сколько имён реально изменено. Невалидный
    target_r_code / нечитаемый файл → {} (безопасный no-op, копирование не валим)."""
    import json as _json
    from pathlib import Path as _Path

    if not re.fullmatch(r"r\d{4}", str(target_r_code or "")):
        return {}
    out: dict[str, int] = {}
    for fname, key in (("adgroups.json", "adgroups"), ("campaigns.json", "campaigns")):
        path = _Path(src_dir) / fname
        if not path.exists():
            continue
        try:
            rows = _json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — снимок нечитаем: ремап best-effort, не валим копирование
            continue
        if not isinstance(rows, list):
            continue
        changed = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            old = row.get("Name")
            if not isinstance(old, str) or not old:
                continue
            new = _copy_remap_region_code(old, target_r_code)
            if new != old:
                row["Name"] = new
                changed += 1
        if changed:
            try:
                path.write_text(_json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
            except Exception:  # noqa: BLE001
                continue
        out[key] = changed
    return out


def _copy_normalize_campaign_name(name: str | None, replacements: list[tuple[str, str]],
                                  target_r_code: str = "") -> str:
    out = _copy_apply_geo_replacements(name, replacements).strip()
    out = re.sub(r"^\s*Копия\s+ХАВАЛ\s+", "Haval ", out, flags=re.I)
    out = re.sub(r"^\s*Копия\s+", "", out, flags=re.I)
    out = out.replace("Башкортостан, республика", "Республика Башкортостан")
    out = out.replace("ХАВАЛ", "Haval")
    # Баг 4: r-сегмент кодера в ИМЕНИ кампании (tp6/tp7 — весь кодер в имени) → target r-код.
    out = _copy_remap_region_code(out, target_r_code)
    return out.strip()


def _copy_domain_from_href(href: str | None) -> str:
    m = re.match(r"^https?://([^/?#]+)", str(href or "").strip(), re.I)
    return (m.group(1).lower() if m else "").strip()


def _copy_target_href(href: str | None, source_domain: str, target_domain: str) -> str:
    """Доменно-агностичная трансформация URL объявления/ссылки в целевой домен (баг 3).

    Раньше: наивный ``href.replace(source_domain, target)`` по ОДНОМУ инференс-домену — если href
    указывал на ДРУГОЙ хост (quiz-поддомен, турбо-страница, яндексовая «Подборка», маркетплейс), он
    уезжал в target без замены. Теперь заменяем ЛЮБОЙ хост, не равный target, на target-хост:
      • хост источника или его ПОДДОМЕН (тот же бизнес, сменил домен) → target-хост + path/query/fragment;
      • ЧУЖОЙ хост (Яндекс-«Подборка»/турбо/маркетплейс — path на клиентском домене не существует) →
        голый target (без мусорного пути, иначе 404).
    Относительный URL (без хоста) и пустой target — не трогаем."""
    from urllib.parse import urlsplit, urlunsplit
    href = str(href or "").strip()
    target = str(target_domain or "").strip()
    # target-хост: срезаем возможную схему/путь, берём чистый netloc.
    t_split = urlsplit(target if "://" in target else "https://" + target)
    target_host = (t_split.netloc or t_split.path.strip("/").split("/", 1)[0]).strip().strip("/")
    target_abs = ("https://" + target_host) if target_host else ""
    if not href:
        return target_abs
    if not target_host:
        return href
    parts = urlsplit(href)
    if not parts.netloc:            # относительный URL (нет хоста) — оставляем как есть
        return href
    host = parts.netloc.lower()
    if host == target_host.lower():
        return href                 # уже целевой хост
    scheme = parts.scheme or "https"
    src = str(source_domain or "").strip().lower().lstrip(".")
    # свой домен/поддомен источника → перенос пути; чужой (подборка/турбо/маркетплейс) → голый target.
    same_business = bool(src) and (host == src or host.endswith("." + src))
    if src and not same_business:
        return target_abs
    return urlunsplit((scheme, target_host, parts.path, parts.query, parts.fragment))

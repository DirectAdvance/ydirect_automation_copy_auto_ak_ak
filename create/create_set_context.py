"""Create-set context/targeting helpers extracted from blueprint.py."""

from __future__ import annotations

import json
import re

_DEPS: dict = {}


def configure(deps: dict) -> None:
    """Bind blueprint dependencies used by context/targeting helpers."""
    _DEPS.clear()
    _DEPS.update(deps)
    globals().update(deps)


def dedup_name_segments(name: str) -> str:
    """ОБЩИЙ дедуп сегментов имени кампании (одна точка для всех tp, любых литералов).

    Имя кампании собирается склейкой независимых человеческих ярлыков: имя позиции структуры
    (`group.name`), ярлык таргетинга позиции (`item.t` / `camp_names`), область, метка фида.
    Эти ярлыки СПЛОШЬ пересекаются в данных слепков (220 позиций из 14894 на 2026-07-19:
    «ТК - Автосалон» + «ТК - Автосалон - Автотаргетинг», «МК» + «МК - Общая - …»,
    «Lada» + «ТК - Lada - …») — это нормальные ярлыки кабинета, а не порча данных, и каждый
    новый харвест их воспроизведёт. Поэтому чинится СКЛЕЙКА, а не литерал: раньше на каждый
    новый литерал заводилось частное условие («Мастер кампаний», домен фида, «ТК»).

    Правило: сегмент (разделитель ` - `, чанки — ` — `) не приклеивается второй раз, если
    точно такой же уже есть в собираемом имени (сравнение регистро- и пробело-независимое).
    Порядок остальных сегментов сохраняется, ПЕРВОЕ вхождение остаётся на месте.

    ⚠️ Осознанная потеря: срезается именно ПОВТОРНОЕ вхождение, где бы оно ни стояло — в том
    числе ХВОСТОВОЕ. `ТК - Москва - Автотаргетинг - Москва` → `ТК - Москва - Автотаргетинг`
    (регион как хвост исчезает; такие хвосты в данных реальны — «Краснодарский Край»,
    «Волгоградская Область», «Ханты-Мансийский автономный округ»). Так же теряется хвостовой
    ярлык из `item.t`: `ТК - Дилер - Ключевики - ТК - Общая - КС - Дилер` →
    `ТК - Дилер - Ключевики - Общая - КС`. Это ПРИНЯТОЕ поведение, а не «режем только мусор»:
    имена остаются различимыми (коллизий на всей структуре нет), регион в кабинет уезжает не
    из имени, а через `r_code` кодера, UI-бейдж парсит `groupName`. Функция ИДЕМПОТЕНТНА.
    """
    if not name:
        return name
    # Пробелы нормализуем ДО разбиения: в слепках встречаются NBSP (\xa0) перед тире и двойные
    # пробелы («Товарная галерея  - Товарная») — без нормализации разделитель не матчится и
    # дубль проезжает мимо дедупа (13 позиций terehov/zubakin на 2026-07-19).
    norm_name = re.sub(r"\s+", " ", str(name)).strip()
    seen: set[str] = set()
    chunks: list[str] = []
    for chunk in norm_name.split(" — "):
        segs: list[str] = []
        for seg in chunk.split(" - "):
            s = seg.strip()
            if not s:
                continue
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            segs.append(s)
        if segs:
            chunks.append(" - ".join(segs))
    return " — ".join(chunks) or name


def _account_ctx(login: str):
    """Контекст для создания: domain, site_type, agency, geoid ОБЛАСТИ (таргетинг — область, не город)."""
    import psycopg2.extras
    conn = _victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT domain, city, site_type, agency_account, directologist FROM public.local_gsheet_sites "
                    "WHERE login_key=%s AND direction='Авто' LIMIT 1", (login,))
        row = cur.fetchone()
        if not row:
            return None
        # #ФИКС-5: город может быть списком через запятую («Краснодар, Сочи») — точный матч
        # всей строки не находил область → geoid=225 (вся РФ). Резолвим КАЖДЫЙ город, union областей.
        oblast = None
        oblasts: list[str] = []
        _unresolved: list[str] = []
        _raw_city = (row.get("city") or "").strip()
        if _raw_city:
            _cities = [c.strip() for c in re.split(r"[,;/]", _raw_city) if c.strip()]
            for _city in _cities:
                cur.execute('SELECT "Область" AS o FROM public.local_gsheet_yandex_direct_id_location '
                            "WHERE \"GeoRegionType\"='City' AND lower(btrim(location))=lower(btrim(%s)) LIMIT 1",
                            (_city,))
                r = cur.fetchone()
                _o = (r["o"] if r else None)
                if _o and _o not in oblasts:
                    oblasts.append(_o)
                elif not _o:
                    _unresolved.append(_city)
            oblast = oblasts[0] if oblasts else None
    finally:
        conn.close()
    # geoids — union geoid всех разрешённых областей; geoid — первый (обратная совместимость).
    geoids: list[int] = []
    for _ob in oblasts:
        gid = _geo_load().get(_ob.strip().lower())
        if gid and int(gid) not in geoids:
            geoids.append(int(gid))
    geoid = geoids[0] if geoids else 225                 # таргет — geoid ОБЛАСТИ (через словарь Директа)
    _raw_city = (row.get("city") or "").strip()
    if _raw_city and not geoids:
        # Город задан, но НЕ разрешён ни в одну область — НЕ молчим (иначе таргет = вся РФ).
        print(f"WARNING _account_ctx: город {_raw_city!r} (login={login}) не найден в справочнике "
              f"локаций → geoid=225 (вся РФ). Проверьте написание города.", file=__import__("sys").stderr)
    return {"domain": (row.get("domain") or "").strip(), "site_type": (row.get("site_type") or "").strip(),
            "agency": row.get("agency_account"), "geoid": geoid, "geoids": (geoids or [geoid]),
            "oblast": oblast, "oblasts": oblasts, "geo_unresolved": _unresolved,
            "city": _raw_city,
            "directologist": (row.get("directologist") or "").strip()}

def _base_site_type(site_type: str) -> str:
    """«Монобренд · Lada» → «Монобренд» (витринный сплит, kontent_pack.base_site_type).

    Все справочники в БД (`direct_ad_templates`, `direct_slepok_audiences`,
    `direct_slepok_content`, `campaign_tags`) заведены на БАЗОВЫЕ типы сайта — марочные
    вкладки существуют только в структуре слепка и в UI. Без нормализации запрос вернёт
    пусто, а вызывающий код примет это за «шаблонов нет» и остановит создание РК.
    """
    from .. import kontent_pack as kp  # noqa: PLC0415 — локальный импорт, модуль тяжелее этого
    return kp.base_site_type(site_type)


def _templates_for(site_type: str):
    """→ (titles, texts, sitelinks[{title,description}]) по типу сайта."""
    site_type = _base_site_type(site_type)
    conn = _victory_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT kind, content FROM public.direct_ad_templates "
                    "WHERE enabled AND site_type=%s ORDER BY kind, id", (site_type,))
        titles, texts, sitelinks = [], [], []
        for kind, content in cur.fetchall():
            if kind == "title":
                titles.append(content)
            elif kind == "text":
                texts.append(content)
            elif kind == "sitelink":
                try:
                    d = json.loads(content)
                    sitelinks.append({"title": d.get("title", ""), "description": d.get("description", "")})
                except Exception:  # noqa: BLE001
                    pass
        return titles, texts, sitelinks
    finally:
        conn.close()

def _slepok_audiences_for(slepok: str, site_type: str, tp: str) -> list[str]:
    """Нативные интересы слепка для (slepok × site_type × tp) → объединённый список id (str).
    Источник: public.direct_slepok_audiences (kind in_market/interests). Пусто → []."""
    if not (slepok and site_type and tp):
        return []
    site_type = _base_site_type(site_type)   # split-вкладки в БД не заведены
    try:
        conn = _victory_conn()
    except Exception:  # noqa: BLE001
        return []
    try:
        cur = conn.cursor()
        cur.execute("SELECT interest_ids FROM public.direct_slepok_audiences "
                    "WHERE slepok=%s AND site_type=%s AND tp=%s", (slepok, site_type, tp))
        ids: set = set()
        for (arr,) in cur.fetchall():
            for x in (arr or []):
                if str(x).strip():
                    ids.add(str(x))
        return sorted(ids)
    except Exception:  # noqa: BLE001
        return []
    finally:
        conn.close()

def _norm_slepok_audience_category(x: str | None) -> str:
    s = re.sub(r"\s+", " ", (x or "").strip().lower())
    if s in ("", "общая"):
        return "(общая)"
    return s

def _tp67_targeting_mode(g: dict) -> str:
    """Новый канон tp6/tp7: keywords / autotarget / audience. Старые RA/RC-коды не поддерживаем."""
    text = " ".join(str(g.get(k) or "") for k in ("name", "group", "label", "code")).lower()
    label = str(g.get("label") or "").lower()
    if ("ключев" in text) or ("ключи" in text) or re.search(r"\bкс\b", text):
        return "keywords"   # «МК Ключи» (t/name) тоже → keywords (было: имя «Ключи» не матчилось, падало в autotarget)
    if re.search(r"автотаргет|автоматическ", text):
        return "autotarget"
    if re.search(r"интерес|автокредит|авито|дром|auto\.ru|авто ру|конкурент", text):
        return "audience"
    return "autotarget"

_AUTOTARGET_MARKER_RE = re.compile(r"-{2,}\s*autotargeting", re.I)

def _real_keywords(words) -> list[str]:
    """Реальные фразы из списка ключей. ``---autotargeting`` — МАРКЕР автотаргета
    (``automation_runtime._AUTOTARGET_KW``), а не ключевое слово: позиция, где он единственный,
    ключей НЕ имеет. Ключи и автотаргетинг при этом совместимы — маркер остаётся в наборе."""
    return [w for w in (words or [])
            if str(w).strip() and not _AUTOTARGET_MARKER_RE.search(str(w))]

def _tp67_modes_from_content(has_keywords: bool, has_audience: bool,
                             explicit: str = "") -> list[str]:
    """Режим таргетинга tp6/tp7 по СОДЕРЖИМОМУ структуры слепка, а НЕ по имени позиции.

    Правило Семёна (2026-07-19): «если в тп6-тп7 нет ключей и аудиторий, то это по умолчанию
    ---autotargeting, и не важно какое будет название». Имя позиции на режим не влияет —
    регулярка по имени (`_tp67_targeting_mode`) источником режима больше НЕ является.

    ``explicit`` — явный ``targeting_mode`` позиции структуры: он только ДОБАВЛЯЕТ режимы
    (объединение), но не отменяет найденное содержимое. Это сохраняет keyword_source-гейт:
    позиция, явно объявленная keyword-driven с пустым корпусом, остаётся ``keywords`` и
    ловится проверкой вместо молчаливой деградации в autotarget.
    """
    modes: list[str] = []
    if has_keywords:
        modes.append("keywords")
    if has_audience:
        modes.append("audience")
    for m in (_parse_targeting_modes(explicit) if str(explicit or "").strip() else []):
        if m != "autotarget" and m not in modes:
            modes.append(m)
    return modes or ["autotarget"]

def tp67_targeting_label_from_modes(modes, tp: str | int | None = None) -> str:
    """Человеческая метка tp6/tp7 по фактическим режимам позиции.

    Это хвост имени кампании и UI-бейдж. Старый текст ``item.t`` здесь не участвует.
    Для tp6/tp7 ручные режимы не показываются отдельно: кампании остаются с автотаргетингом,
    поэтому метка всегда ``... + Автотаргетинг``.
    """
    parsed = _parse_targeting_modes(modes)
    has_kw = "keywords" in parsed
    has_aud = "audience" in parsed
    if has_kw and has_aud:
        return "КС + Аудитории + Автотаргетинг"
    if has_kw:
        return "КС + Автотаргетинг"
    if has_aud:
        return "Аудитории + Автотаргетинг"
    return "Автотаргетинг"

_TP67_TARGETING_TAIL_RE = re.compile(
    r"^(?:"
    r"кс|ключи|ключевики|ключевые\s+слова|"
    r"аудитории?|интересы?|"
    r"автотаргет(?:инг)?|автоматическ(?:ий|ая)|"
    r"at|auto|manual|ot|"
    r"кс\s*[+/]\s*аудитории?|кс\s*[+/]\s*автотаргет(?:инг)?|"
    r"аудитории?\s*[+/]\s*автотаргет(?:инг)?|"
    r"кс\s*[+/]\s*аудитории?\s*[+/]\s*автотаргет(?:инг)?|"
    # «X + Автотаргетинг» — X из символов \w, пробелов, em-dash (—); покрывает паттерны
    # «Дилер + Авто», «КС — Sedan + Авто», «СR + Авто», «Мастер + Авто» и т.п.
    r"[\w\s—]+\s*\+\s*автотаргет(?:инг)?|"
    # «Ручная КС/аудитория …» — весь блок таргетинга без явного сегмента (chepelev)
    r"ручн(?:ой|ая|ое)\s+(?:кс|аудитор)[\w\s+/]*"
    r")$",
    re.I,
)

# Паттерны для нормализации устаревших форматов без разделителей « - » (tumashenko)
_TP67_MАСТЕР_SEG_RE = re.compile(r"^мастер\s+(?!\+)(.+?)\s+автотаргет(?:инг)?$", re.I)
_TP67_МАСТЕР_PLUS_RE = re.compile(r"^мастер\s*\+\s*автотаргет(?:инг)?$", re.I)

def tp67_clean_position_name_for_targeting(raw: str, tp_label: str = "") -> str:
    """Убрать устаревший targeting-хвост из ``item.t`` перед добавлением фактической метки.

    ``item.t`` пришёл из харвеста и может говорить ``КС``/``Автотаргетинг``/``Интересы`` уже
    после того, как ключи или аудитории позиции изменились. Категорию оставляем, хвост заменяем.
    Также срезает tracking-суффикс вида ``[tk_pervaya]``/``[tk_kras_zqf]``/``[tk_render]`` из
    camp_names — когда разделитель `` - `` не используется, tail-regexp не срабатывает сам.

    Расширения (2026-07-21):
    - «X + Автотаргетинг» (em-dash OK) — «Дилер + Авто», «КС — Sedan + Авто», «СR + Авто»
    - «ot»/«OT» — аббревиатура таргетинг-режима (terehov tp7)
    - «Ручная КС/аудитория …» — полный блок без явного сегмента (chepelev tp7)
    - «Мастер X Автотаргетинг» / «Мастер + Автотаргетинг» — нормализация без « - » (tumashenko)
    - Prefix «МК»/«ТК» срезается всегда, независимо от tp_label — они никогда не бывают сегментом
    """
    # Срезаем tracking-суффикс [tk_xxx] до любого разбора — защита для всех вызывающих.
    raw_s = re.sub(r'\s*\[[\w_\-]+\]\s*$', '', str(raw or "").strip())
    # Нормализация старых форматов без разделителей « - »:
    # «Мастер Общая Автотаргетинг» → «МК - Общая - Автотаргетинг» (сегмент сохраняется)
    # «Мастер + Автотаргетинг» → «МК - Общая - Автотаргетинг» (нет сегмента → Общая)
    _m = _TP67_MАСТЕР_SEG_RE.match(raw_s)
    if _m:
        raw_s = f"МК - {_m.group(1).strip()} - Автотаргетинг"
    elif _TP67_МАСТЕР_PLUS_RE.match(raw_s):
        raw_s = "МК - Общая - Автотаргетинг"
    parts = [p.strip() for p in re.split(r"\s+-\s+", raw_s) if p.strip()]
    while parts and _TP67_TARGETING_TAIL_RE.match(parts[-1].replace("ё", "е")):
        parts.pop()
    # Срезаем тип-префикс «МК»/«ТК» — он не является сегментом. Делаем независимо от tp_label,
    # чтобы корректно обрабатывать tp7-позиции с «МК» в item.t (chepelev) и наоборот.
    if parts and parts[0].strip().lower() in ("мк", "тк"):
        parts = parts[1:]
    base = " - ".join(parts).strip()
    return _tp67_common_name(base) or base

def _tp67_common_name(text: str | None) -> str:
    """Единое видимое имя общей tp6/tp7 позиции."""
    raw = re.sub(r"\s+", " ", str(text or "").replace("\xa0", " ")).strip()
    s = raw.lower()
    if s in ("общая", "общие", "общее", "общие запросы"):
        return "Общее"
    if s.startswith("общие запросы "):
        return re.sub(r"^общие\s+запросы\b", "Общее", raw, flags=re.I).strip()
    return ""

def _parse_targeting_modes(raw) -> list[str]:
    """Компонует явный per-position targeting_mode в НАБОР режимов {keywords,audience,autotarget}.

    Поддерживает ГИБРИД: одна tp6/tp7-позиция может нести keywords И audience одновременно
    (реальные слепки: tp7 RA = ключи + до 8 аудиторий, tp6 RA = ключи + 1 аудитория).
    Вход — строка ('keywords+audience', 'keywords,audience', 'ключи + интересы') ИЛИ список.
    Пусто/неизвестно → ['autotarget'] (обратная совместимость: старый одиночный режим сохраняется,
    т.к. 'keywords'/'audience'/'autotarget' парсятся в один элемент)."""
    if isinstance(raw, (list, tuple, set)):
        toks = [str(x) for x in raw]
    else:
        toks = re.split(r"[+,/&;\s]+", str(raw or ""))
    out: list[str] = []
    for t in toks:
        tl = t.strip().lower()
        if not tl:
            continue
        if tl in ("keywords", "keyword", "kw", "кс", "ключи", "ключевые"):
            m = "keywords"
        elif tl in ("audience", "audiences", "интересы", "интерес", "аудитория", "аудитории"):
            m = "audience"
        elif tl in ("autotarget", "auto", "автотаргет", "автотаргетинг"):
            m = "autotarget"
        else:
            continue                                         # неизвестный токен игнорируем
        if m not in out:
            out.append(m)
    return out or ["autotarget"]

def _tp67_audience_category_candidates(g: dict) -> list[str]:
    """Категории только внутри конкретного слепка; aliases нужны для старых подписей структуры."""
    text = " ".join(str(g.get(k) or "") for k in ("name", "group", "label")).lower()
    raw = [g.get("group"), g.get("name"), g.get("label")]
    out: list[str] = []
    for x in raw:
        nx = _norm_slepok_audience_category(str(x or ""))
        if nx and nx not in out:
            out.append(nx)
    if "общие запрос" in text:
        out.append("общие запросы")
    if "дилер интерес" in text:
        out.append("дилер интересы")
    if "дилер" in text:
        out.append("дилер")
    if "интерес" in text:
        out.append("интересы")
    if re.search(r"общая|товарная|модели|марки|автокредит|кредит|авито|дром|авто ру|auto\.ru", text):
        out.extend(["(общая)", "(нестандарт)"])
    dedup: list[str] = []
    for x in out:
        nx = _norm_slepok_audience_category(x)
        if nx and nx not in dedup:
            dedup.append(nx)
    return dedup

def _slepok_audience_cats(slepok: str, site_type: str, tp: str) -> list[dict]:
    """Аудитории слепка ПО КАТЕГОРИЯМ (БЕЗ мёржа) — как в слепках: отдельная кампания на категорию.
    → [{"category": str, "interest_ids": [str,...]}] из public.direct_slepok_audiences.
    Пустые категории отбрасываем. Источник тот же, что у _slepok_audiences_for, но без объединения."""
    if not (slepok and site_type and tp):
        return []
    site_type = _base_site_type(site_type)   # split-вкладки в БД не заведены
    try:
        conn = _victory_conn()
    except Exception:  # noqa: BLE001
        return []
    try:
        cur = conn.cursor()
        cur.execute("SELECT category, interest_ids FROM public.direct_slepok_audiences "
                    "WHERE slepok=%s AND site_type=%s AND tp=%s ORDER BY category", (slepok, site_type, tp))
        out = []
        for cat, arr in cur.fetchall():
            ids = sorted({str(x) for x in (arr or []) if _tp67_audience_id_supported(x)})
            if ids:
                out.append({"category": (cat or "(общая)"), "interest_ids": ids})
        return out
    except Exception:  # noqa: BLE001
        return []
    finally:
        conn.close()

# Типы аудиторий структуры, которые UAC-путь умеет отправлять как ca_retargeting_condition.goals
# (automation_runtime._audience_object_for_id). AUDIENCE:/RETARGETING: — ДРУГИЕ сущности Директа
# (сегменты Яндекс.Аудиторий / условия ретаргетинга), их id в goals слать нельзя → считаем
# отдельно и предупреждаем, а не тащим молча. HOST — домены/сайты из UAC-харвеста: их не
# показываем в слепках и не отправляем при создании.
_STRUCT_AUD_SUPPORTED = {"INTERESTS", "APPLICATION"}

def _tp67_audience_id_supported(aid: str | int | None) -> bool:
    sid = str(aid or "").strip()
    return bool(sid and sid.isdigit() and not sid.startswith("190"))

def _struct_audience_split(it: dict, g: dict) -> tuple[list[str], int]:
    """Аудитории позиции структуры → (поддержанные id, СКОЛЬКО ПОТЕРЯНО).

    Структура хранит их двумя формами: ``[{"id","name","type"}]`` и типизированными строками
    ``"INTERESTS:249…"`` / ``"AUDIENCE:409…"``; часть — голые числовые id.

    «Потеряно» считается ПОЭЛЕМЕНТНО (тип не goals-сущность / битый id), а НЕ как
    ``len(raw) - len(ids)``: `ids` дедуплицируются, поэтому разностью дубликат одного и того же
    INTERESTS-id давал ложное «не поддержано UAC-путём=N» на полностью корректной позиции.
    """
    ids, seen = [], set()
    unsupported = 0
    for x in (it.get("audiences") or g.get("audiences") or []):
        if isinstance(x, dict):
            aid = str(x.get("id") or x.get("rl_id") or "").strip()
            atype = str(x.get("type") or "").strip().upper()
        else:
            s = str(x or "").strip()
            atype, _, aid = s.partition(":") if ":" in s else ("", "", s)
            atype = atype.strip().upper()
            aid = aid.strip()
        if not aid or not aid.isdigit():
            unsupported += 1
            continue
        if atype == "HOST" or aid.startswith("190"):
            continue
        if atype and atype not in _STRUCT_AUD_SUPPORTED:
            unsupported += 1                          # AUDIENCE:/RETARGETING: — не goals-сущность
            continue
        if aid not in seen:                           # дубликат — не потеря, просто дедуп
            seen.add(aid)
            ids.append(aid)
    return ids, unsupported

def _struct_audience_ids(it: dict, g: dict) -> list[str]:
    """id аудиторий, ЗАПИСАННЫХ В СТРУКТУРЕ позиции (item > group)."""
    return _struct_audience_split(it, g)[0]

def _struct_audience_unsupported(it: dict, g: dict) -> int:
    """Сколько аудиторий позиции UAC-путь отправить НЕ может (AUDIENCE:/RETARGETING:/битый id).
    Нужен, чтобы потеря была ВИДИМОЙ в предупреждениях плана, а не молчаливой."""
    return _struct_audience_split(it, g)[1]

def _tp67_struct_display_score(rec: dict) -> int:
    text = " ".join(str(rec.get(k) or "") for k in ("name", "group", "label", "targeting_mode")).lower()
    score = 0
    if rec.get("audiences"):
        score += 2
    if re.search(r"(^|[^0-9a-zа-яё])кс([^0-9a-zа-яё]|$)|ключев", text):
        score += 2
    if "аудитори" in text or str(rec.get("targeting_mode") or "").strip().lower() == "audience":
        score += 2
    return score

def _tp67_struct_merge_mode(rec: dict) -> str:
    text = " ".join(str(rec.get(k) or "") for k in ("name", "group", "label", "targeting_mode")).lower()
    explicit = str(rec.get("targeting_mode") or "").strip().lower()
    has_kw = bool(re.search(r"(^|[^0-9a-zа-яё])кс([^0-9a-zа-яё]|$)|ключев|keywords?", text + " " + explicit))
    has_aud = bool(rec.get("audiences")) or "аудитори" in text or explicit == "audience"
    if has_kw and has_aud:
        return "keywords+audience"
    if has_kw:
        return "keywords"
    if has_aud:
        return "audience"
    return "autotarget"

def _tp67_common_autotarget_rec(tp: str, sq: str = "site", ct: str = "ct0000") -> dict:
    is_tp7 = str(tp or "") == "tp7"
    prefix = "ТК" if is_tp7 else "МК"
    ct = str(ct or "").strip() or "ct0000"
    gc = f"{ct}_aon_n000_r0000_ct010_ag001_g00" if is_tp7 else f"{ct}_aon_n000_r0000_ct001_ag011_g00"
    gk = "tk_common_autotarget" if is_tp7 else "mk_common_autotarget"
    name = f"{prefix} - Общее - Автотаргетинг"
    return {"name": name, "group": "Общее", "label": name, "sq": sq or "site", "is_auto": True,
            "code": "", "gc": gc, "gk": gk, "merged_gks": [], "targeting_mode": "autotarget",
            "audiences": [], "audiences_unsupported": 0, "keyword_source": "", "pricing": "",
            "feed_role": "", "feed_id": None, "feed_key": "", "camp_names": [name],
            "pos_key": f"{sq or 'site'}|Общее|{name}"}


_CT4_TOKEN_RE = re.compile(r"ct\d{4}")


def _tp67_common_ct_for_segment(site_type: str, sq: str, merged: list[dict]) -> str:
    """ct для синтетической «Общее - Автотаргетинг» позиции tp6/tp7.

    `ct0000` («полное отсутствие бренда», CODER.md) легитимен только для «Мультибренд» и
    «Монобренд · Общая». На конкретной брендовой вкладке («Монобренд · Haval») он давал
    generic-контент вместо бренда (MONOBRAND_SYNTHETIC_CT0000_HARDCODE). Резолвим ct бренда:
    1) мажоритарный валидный ct среди соседних позиций этого же (sq, tp) в структуре слепка —
       они все несут один и тот же брендовый ct; 2) если items нет — по имени бренда через
       `local_gsheet_naming.ag_part1` (`_ct_for_name`).
    """
    from .. import kontent_pack as kp  # noqa: PLC0415 — локальный импорт, как в _base_site_type
    base = kp.base_site_type(site_type)
    if base != "Монобренд":
        return "ct0000"
    m = re.search(r"·\s*(.+)$", str(site_type or ""))
    brand = (m.group(1).strip() if m else "")
    if not brand or brand.lower() == "общая":
        return "ct0000"
    counts: dict[str, int] = {}
    for rec in merged:
        if (str(rec.get("sq") or "site").strip() or "site") != (sq or "site"):
            continue
        tok = _CT4_TOKEN_RE.search(str(rec.get("gc") or ""))
        if tok and tok.group(0) != "ct0000":
            counts[tok.group(0)] = counts.get(tok.group(0), 0) + 1
    if counts:
        return max(counts.items(), key=lambda kv: kv[1])[0]
    return _ct_for_name(brand) or "ct0000"

def _slepok_struct_groups(slepok: str, site_type: str, tp: str) -> list[dict]:
    """Позиции СТРУКТУРЫ слепка для (slepok, site_type, tp6|tp7).

    Источник — slepki_structure.json (ТОТ ЖЕ, что рисует вкладки «Структура»/«Создание РК»),
    чтобы план создания совпадал с показом. is_auto берём из таргетинга группы (item.t):
    есть «КС»/«ключев…» → ручной (manual, ключи), иначе автотаргетинг."""
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    d = next((x for x in _json("slepki_structure.json").get("directologists", []) if x.get("key") == key), None)
    if not d:
        return []
    is_auto_pack = (d.get("ui_group") or "auto") == "auto"
    st = next((s for s in d.get("site_types", []) if s.get("name") == site_type), None)
    if not st:
        return []
    out: list[dict] = []
    for t in st.get("tp", []):
        if t.get("code") != tp:
            continue
        blocks = t.get("splits") or ([{"sq": "site", "groups": t.get("groups", [])}] if t.get("groups") else [])
        for sp in blocks:
            sq = sp.get("sq") or "site"
            for g in sp.get("groups", []):
                gname = (g.get("name") or "").strip()
                if gname.lower() in ("(общая)", "общая"):
                    gname = "Общая"
                items = [it for it in (g.get("items") or []) if isinstance(it, dict)] or [{}]
                for idx, it in enumerate(items):
                    label = (it.get("t") or "").strip()
                    label_clean = "" if label in ("", "—", "-") else label
                    camp_names = [str(x).strip() for x in (it.get("camp_names") or []) if str(x).strip()]
                    merged_gks = [str(x).strip() for x in (it.get("merged_gks") or []) if str(x).strip()]
                    camp_display = camp_names[0] if camp_names else ""
                    tl = label.lower()
                    is_auto = not (("ключев" in tl) or re.search(r"\bкс\b", tl))
                    # display = camp_names[0] (как /slepki UI); item.t остаётся label/pos_key
                    # для матча контента, потому что это старая структурная метка позиции.
                    # Фолбэк на gname — только когда нет ни camp_names, ни item.t.
                    display = camp_display or label_clean or gname
                    out.append({"name": display or label_clean or gname or None,
                                "group": gname, "label": label_clean,
                                "sq": sq, "is_auto": is_auto,
                                "code": it.get("c") or it.get("code") or "",
                                "gc": it.get("gc") or "",                       # кодер-строка item'а → источник ct в плане
                                # gk — group-key ПАКА (per-adgroup раскладка `{slepok}__{slug}.txt`,
                                # kontent_pack._group_slug). Без него создание читало ЛЕГАСИ-файл
                                # `{slepok}.txt`, которого у per-group слепков нет → 0 ключей при
                                # 416 реальных в `pavlov__mk.txt` (Д7, обрыв 3).
                                "gk": it.get("gk") or "",
                                # tp6/tp7: несколько исходных строк слепка могут давать одно
                                # фактическое имя кампании, но иметь разные per-group ключи.
                                # merged_gks хранит эти исходные группы; создание/проверка читают
                                # union, а не теряют семантику при схлопывании дублей в структуре.
                                "merged_gks": merged_gks,
                                "targeting_mode": (it.get("targeting_mode") or "").strip(),  # явный режим item'а (важно для не-авто: «Конкуренты»→keywords)
                                # Аудитории, ЗАПИСАННЫЕ В СТРУКТУРЕ позиции (item > group). Без этого
                                # переноса они не доезжали до плана вообще (обрыв 2, Д7 2026-07-19):
                                # pavlov/«С пробегом»/tp6 нёс 9 INTERESTS в item["audiences"], а план
                                # получал пустой список и уходил в autotarget.
                                "audiences": _struct_audience_ids(it, g),
                                "audiences_unsupported": _struct_audience_unsupported(it, g),
                                # Явные per-position атрибуты слепка (item > group). Пусто → дефолты движка.
                                # keyword_source: источник корпуса ключей; НЕпустой = позиция ОБЯЗАНА иметь ключи
                                # → пустой корпус блокирует позицию (НЕ молчаливый autotarget). См. _emit_struct.
                                "keyword_source": str(it.get("keyword_source") or g.get("keyword_source") or "").strip(),
                                "pricing": str(it.get("pricing") or g.get("pricing") or "").strip(),  # PER_CLICK|PER_CONVERSION|PER_ACTION; пусто → derive из pay
                                # Явно заданный в структуре позиции фид (feed_role/feed_id/feed_key):
                                # item имеет приоритет над group. Пусто → tp7 fan-out по всем разрешённым
                                # фидам (безопасный дефолт, обратная совместимость). См. create_set_plan._emit_struct.
                                "feed_role": str(it.get("feed_role") or g.get("feed_role") or "").strip(),
                                "feed_id": it.get("feed_id") or g.get("feed_id") or None,
                                "feed_key": str(it.get("feed_key") or it.get("feed_name")
                                                or g.get("feed_key") or "").strip(),
                                # camp_names: список реальных имён кампаний (из харвеста).
                                # Для tp6/tp7 = одно имя — используется в UI вместо устаревшего it.t.
                                "camp_names": camp_names,
                                "pos_key": f"{sq}|{gname}|{label or idx}"})
    merged: list[dict] = []
    by_key: dict[tuple[str, str], dict] = {}
    for rec in out:
        # Several harvested rows can represent one visible tp6/tp7 campaign but point
        # to different keyword slugs. Merge in memory so creation uses a union corpus.
        mkey = (str(rec.get("sq") or "site").strip().lower(),
                _tp67_merge_name_key(rec.get("name") or rec.get("group") or ""),
                _tp67_struct_merge_mode(rec))
        if not mkey[1]:
            merged.append(rec)
            continue
        prev = by_key.get(mkey)
        if not prev:
            if rec.get("gk"):
                rec["merged_gks"] = [
                    str(x).strip() for x in (rec.get("merged_gks") or [])
                    if str(x).strip() and str(x).strip() != str(rec.get("gk") or "").strip()
                ]
            by_key[mkey] = rec
            merged.append(rec)
            continue
        gks = []
        for src in (prev, rec):
            if src.get("gk"):
                gks.append(str(src.get("gk") or "").strip())
            gks.extend(str(x).strip() for x in (src.get("merged_gks") or []) if str(x).strip())
        uniq_gks = list(dict.fromkeys(x for x in gks if x))
        prev["gk"] = uniq_gks[0] if uniq_gks else (prev.get("gk") or "")
        prev["merged_gks"] = uniq_gks[1:]
        prev["audiences"] = list(dict.fromkeys(
            [str(x) for x in (prev.get("audiences") or []) if str(x).strip()]
            + [str(x) for x in (rec.get("audiences") or []) if str(x).strip()]
        ))
        prev["audiences_unsupported"] = int(prev.get("audiences_unsupported") or 0) \
            + int(rec.get("audiences_unsupported") or 0)
        prev["camp_names"] = list(dict.fromkeys(
            [str(x) for x in (prev.get("camp_names") or []) if str(x).strip()]
            + [str(x) for x in (rec.get("camp_names") or []) if str(x).strip()]
        ))
        if _tp67_struct_display_score(rec) > _tp67_struct_display_score(prev):
            for fld in ("name", "group", "label"):
                if str(rec.get(fld) or "").strip():
                    prev[fld] = rec.get(fld)
        for fld in ("keyword_source", "targeting_mode", "pricing", "feed_role", "feed_key"):
            if not str(prev.get(fld) or "").strip() and str(rec.get(fld) or "").strip():
                prev[fld] = rec.get(fld)
        if not prev.get("feed_id") and rec.get("feed_id"):
            prev["feed_id"] = rec.get("feed_id")
    if is_auto_pack and tp in ("tp6", "tp7") and merged:
        sqs = sorted({str(x.get("sq") or "site").strip() or "site" for x in merged})
        for sq in sqs:
            has_common_at = any(
                (str(x.get("sq") or "site").strip() or "site") == sq
                and _tp67_merge_name_key(x.get("name") or x.get("group") or "") == "общее"
                and _tp67_struct_merge_mode(x) == "autotarget"
                for x in merged
            )
            if not has_common_at:
                _common_ct = _tp67_common_ct_for_segment(site_type, sq, merged)
                merged.append(_tp67_common_autotarget_rec(tp, sq, _common_ct))
    return merged

def _tp67_keywords_for_groups(slepok: str, site_type: str, tp: str, ct: str, city: str,
                              position_name: str | None = None, sq: str | None = None,
                              groups=None) -> tuple[list[str], list[str]]:
    """Union keywords for one merged tp6/tp7 structure item.

    ``groups`` starts with canonical ``gk`` and may include ``merged_gks`` from the
    structure. Each group is resolved by the normal M3→real-library path, then the
    combined corpus is de-duplicated with the same caps as a single group.
    """
    if isinstance(groups, str):
        group_list = [groups]
    else:
        group_list = list(groups or [])
    if not group_list:
        return _tp67_keywords_for(slepok, site_type, tp, ct, city, position_name, sq, group="")
    pos_all: list[str] = []
    neg_all: list[str] = []
    seen_groups: set[str] = set()
    for gk in group_list:
        g = str(gk or "").strip()
        if g in seen_groups:
            continue
        seen_groups.add(g)
        pos, neg = _tp67_keywords_for(slepok, site_type, tp, ct, city, position_name, sq, group=g)
        pos_all.extend(pos or [])
        neg_all.extend(neg or [])
    return _kw_clean(pos_all, 200), _kw_clean(neg_all, 100)

def _tp67_pos_key(g: dict) -> str:
    """СТАБИЛЬНЫЙ ключ позиции структуры — поле ``pos_key`` (`{sq}|{группа}|{label|idx}`,
    проставляется в `_slepok_struct_groups`).

    Почему не display-имя: план подставляет город в `g["name"]/["group"]/["label"]`
    (`create_set_plan._emit_struct`), а `pos_key` он НЕ трогает — и в плане, и в свежепрочитанной
    структуре ключ одинаков (с плейсхолдером «ГОРОД» внутри). Матч по имени на таких позициях
    промахивался (`avtolajt_bu`/tp7 «ТК - Общая - Автотаргетинг - ГОРОД») → легаси-файл пака →
    0 ключей → autotarget.

    Фолбэк `gc`/`code` — для dict'ов из старых планов без `pos_key`. Оба item-level, т.е.
    идентифицируют ПОЗИЦИЮ. `gk` из фолбэка убран (2026-07-19): это слаг ГРУППЫ пака, общий для
    нескольких item'ов → матч по нему брал первый совпавший, т.е. мог подставить эталон соседней
    позиции. Промах фолбэка честнее: даёт `matched=False`, а вызывающий обязан предупредить
    (`tp67_struct_expectations`), а не проглотить чужие ожидания."""
    for k in ("pos_key", "gc", "code"):
        v = str(g.get(k) or "").strip()
        if v:
            return v
    return ""

def tp67_struct_expectations(slepok: str, site_type: str, tp: str, ct: str, city: str,
                             position_name: str | None, sq: str | None = None,
                             pos_key: str | None = None) -> dict:
    """Что СТРУКТУРА СЛЕПКА обещает для одной позиции tp6/tp7: ключи и аудитории.

    Считается ПРЯМО из структуры/пака, НЕзависимо от того, что движок решил по дороге
    (режим, variant, interest_ids). Именно поэтому годится как эталон для проверки
    «структура → кабинет»: обрыв на этапе плана её не обманывает — сверка build↔live
    была слепа ровно потому, что build уже пустой (0 == 0).

    ``pos_key`` — СТАБИЛЬНЫЙ ключ позиции (фолбэк `gc`/`code`, см. `_tp67_pos_key`). Он ГЛАВНЕЕ
    display-имени: имя в плане уже с подставленным городом («ТК · Краснодар»), а в структуре
    остаётся плейсхолдер («ТК · ГОРОД») → матч по имени промахивался, позиция молча уходила в
    легаси-файл пака (0 ключей) и в autotarget. При промахе обоих матчей возвращаем
    ``matched=False`` — вызывающий обязан предупредить, а не проглотить.

    → {"keywords": [...], "audiences": [...], "audiences_unsupported": int, "modes": [...],
       "matched": bool}
    """
    pos = str(position_name or "").strip()
    pkey = str(pos_key or "").strip().lower()
    g_match: dict = {}
    _groups = _slepok_struct_groups(slepok, site_type, tp)   # ОДИН проход (было: до 2 вызовов)
    if pkey:
        for g in _groups:
            if _tp67_pos_key(g).lower() == pkey:
                g_match = g
                break
    if not g_match and pos:
        low = pos.lower()
        _by_group: dict = {}
        for g in _groups:
            if str(g.get("name") or "").strip().lower() == low:
                g_match = g
                break
            if not _by_group and str(g.get("group") or "").strip().lower() == low:
                _by_group = g
        g_match = g_match or _by_group
    # gk/gc позиции → per-group файл пака. Без него читался бы легаси-файл (обрыв 3).
    _grp = str(g_match.get("gk") or g_match.get("gc") or "").strip()
    _groups = [_grp] + [str(x).strip() for x in (g_match.get("merged_gks") or []) if str(x).strip()]
    _kw_raw, _neg = _tp67_keywords_for_groups(slepok, site_type, tp, ct or "ct0000", city or "",
                                              pos or None, sq, groups=_groups)
    kws = _real_keywords(_kw_raw)
    auds = [str(x) for x in (g_match.get("audiences") or []) if _tp67_audience_id_supported(x)]
    if not auds and g_match and "audience" in _tp67_struct_merge_mode(g_match):
        # БД-аудитории берём ТОЛЬКО при совпавшей КАТЕГОРИИ позиции. merged-фолбэк
        # (`source == "fallback"` — объединение ВСЕХ категорий слепка) сюда не годится: он
        # непустой почти всегда и сделал бы audience-позицией каждую позицию структуры
        # (замер: 345 позиций и 20103 аудитории вместо реальных). Это не «содержимое позиции».
        _ids, _src = _slepok_interest_for_struct(slepok, site_type, tp, g_match)
        if _src not in ("fallback", "none"):
            auds = [str(x) for x in (_ids or []) if _tp67_audience_id_supported(x)]
    return {"keywords": kws, "minus": _neg, "audiences": auds, "group": _grp,
            "matched": bool(g_match), "pos_key": _tp67_pos_key(g_match) if g_match else "",
            "audiences_unsupported": int(g_match.get("audiences_unsupported") or 0),
            "modes": _tp67_modes_from_content(bool(kws), bool(auds),
                                              g_match.get("targeting_mode") or "")}

def _slepok_interest_for_cat(slepok: str, site_type: str, tp: str, cat: str | None) -> list:
    """interest_ids слепка для категории структурной группы (если совпала с категорией аудиторий
    direct_slepok_audiences). Нет совпадения → [] (create_set фолбэкнет на объединённый список)."""
    if not cat:
        return []
    low = cat.strip().lower()
    for c in _slepok_audience_cats(slepok, site_type, tp):
        if (c.get("category") or "").strip().lower() == low:
            return c.get("interest_ids") or []
    return []

def _slepok_interest_for_struct(slepok: str, site_type: str, tp: str, g: dict) -> tuple[list[str], str]:
    """Аудитории строго по текущему слепку; no cross-slepok/global merge."""
    cats = _slepok_audience_cats(slepok, site_type, tp)
    by_cat = {_norm_slepok_audience_category(c.get("category")): c.get("interest_ids") or [] for c in cats}
    for cand in _tp67_audience_category_candidates(g):
        ids = by_cat.get(cand)
        if ids:
            return ids, cand
    merged = sorted({str(x) for ids in by_cat.values() for x in ids if _tp67_audience_id_supported(x)})
    return merged, "fallback" if merged else "none"

def _tp67_kw_position_key(text: str | None) -> str:
    """Нормализованный ключ позиции для fallback-библиотеки реальных UAC keywords."""
    s = re.sub(r"\[[^\]]*\]", " ", str(text or "").replace("\xa0", " "))
    s = re.sub(r"\b(мк|тк|ключевики|кс)\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(автотаргетинг|автоматическая)\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"[·—–_-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return "общие запросы" if s in ("общая", "общие", "общие запросы") else s


def _tp67_merge_name_key(text: str | None) -> str:
    """Canonical visible name for merging duplicate tp6/tp7 rows."""
    cleaned = tp67_clean_position_name_for_targeting(text or "")
    s = re.sub(r"\[[^\]]*\]", " ", str(cleaned or text or "").replace("\xa0", " "))
    s = re.sub(r"\b(мк|тк|ключевики)\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"[·—–_-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    generic = {
        "", "кс", "автотаргетинг", "кс автотаргетинг",
        "общая", "общие", "общее", "общие запросы",
        "общая кс", "общая автотаргетинг", "общая кс автотаргетинг",
        "общее кс", "общее автотаргетинг", "общее кс автотаргетинг",
        "общие запросы кс", "общие запросы автотаргетинг", "общие запросы кс автотаргетинг",
        "общая аудитории автотаргетинг", "общее аудитории автотаргетинг",
        "общие запросы аудитории автотаргетинг",
        "общая интересы автотаргетинг", "общее интересы автотаргетинг",
        "общие запросы интересы автотаргетинг",
    }
    return "общее" if s in generic else s

def _tp67_real_keyword_items() -> list[dict]:
    try:
        return _json("tp67_real_keywords.json").get("items") or []
    except Exception:  # noqa: BLE001
        return []

def _tp67_keywords_from_real_library(slepok: str, site_type: str, tp: str, ct: str,
                                     city: str, position_name: str | None,
                                     sq: str | None = None) -> tuple[list[str], list[str]]:
    """Fallback: реальные keywords из cookie-payload UAC, когда M3-пак пустой.

    Приоритет точный: слепок + ст + tp + sq + ct/позиция. Разные позиции ct0000
    (Автосалон/Дилер/Общие запросы) не схлопываем, потому что в реальных аккаунтах
    у них разные keyword lists.

    ⛔ ТОЛЬКО СВОЙ СЛЕПОК: чужой директолог источником ключей быть не может (см. _score).
    Своего набора нет → ([], []) и вызывающий явно сообщает «КС без ключей», а не подменяет
    молча чужой семантикой.
    """
    skey = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    pos_key = _tp67_kw_position_key(position_name)
    ct_key = (ct or "").strip().lower()
    sq_key = (sq or "").strip().lower()
    items = _tp67_real_keyword_items()

    def _score(it: dict) -> tuple[int, int, int, int, int, int] | None:
        if it.get("tp") != tp:
            return None
        if not (it.get("keywords") or []):
            return None                                  # пустой decoy-item (напр. dmp position='конкуренты' 0кл) не должен затмевать реальный набор по позиции
        if it.get("slepok") != skey:
            # ЖЁСТКИЙ slepok-фильтр (анти-bleed, 2026-07-18). Библиотека — срез РЕАЛЬНЫХ кабинетов
            # разных директологов; ранжирование по same_slepok (было первым элементом ранга, но БЕЗ
            # отсечения) при отсутствии своего набора отдавало позиции набор ЧУЖОГО слепка — семантика
            # одного дилера уезжала в кабинет другого (terehov/tp7 «Общие запросы - КС» → ключи pavlov).
            # Своих ключей нет → возвращаем ПУСТО, а вызывающий (create_set_master_product.py:130)
            # логирует «tp6/tp7 КС без ключей» в errors_log + per-position warning и блокирует позицию
            # при явном keyword_source. Молчаливой подмены чужим набором быть не должно.
            return None
        same_slepok = 1
        site_score = 1 if (not site_type or it.get("site_type") == site_type) else 0
        sq_score = 1 if (not sq_key or it.get("sq") == sq_key) else 0
        ct_score = 1 if (ct_key and it.get("ct") == ct_key) else 0
        pos_score = 1 if (pos_key and it.get("position") == pos_key) else 0
        if not (ct_score or pos_score):
            return None
        # Приоритет ВНУТРИ своего слепка: site/sq, затем позиция, затем ct.
        # (same_slepok оставлен в кортеже для читаемости ранга — он теперь всегда 1.)
        return (same_slepok, site_score, sq_score, pos_score, ct_score, len(it.get("keywords") or []))

    best = None
    best_score = None
    for it in items:
        sc = _score(it)
        if sc is not None and (best_score is None or sc > best_score):
            best = it
            best_score = sc
    if not best:
        return [], []
    pos = _kw_clean(_drop_used_car(_drop_foreign_city_keywords(best.get("keywords") or [], city), site_type), 200)
    neg = _kw_clean(best.get("minus") or [], 100)
    return pos, neg

def _tp67_keywords_for(slepok: str, site_type: str, tp: str, ct: str, city: str,
                       position_name: str | None = None, sq: str | None = None,
                       group: str = "") -> tuple[list[str], list[str]]:
    """Ключи из keyword pack текущего слепка.

    Если keyword pack пуст/недоступен (local/M3 source alive=False) — fallback из реальных
    UAC payload по кукам; если источник жив, но конкретных ключей нет, это content-gap данных,
    а не проблема media (картинки/видео читаются из local mirror LXC101).

    ``group`` — gk/gc позиции (per-adgroup раскладка пака ``{slepok}__{slug}.txt``). Пусто →
    прежнее ЛЕГАСИ-поведение (``{slepok}.txt``). Без него tp6/tp7 с per-group паком читали
    несуществующий легаси-файл и молча уходили в автотаргет (Д7 2026-07-19: pavlov/«С пробегом»/
    tp6 — 0 вместо 416 фраз из ``pavlov__mk.txt``).
    """
    skey = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    ct_key = ct or "ct0000"

    def _pack_keywords(tp_key: str) -> tuple[list[str], list[str]]:
        kw = kp.read_keywords(site_type, tp_key, ct_key, skey, group=group)
        pos = _kw_clean(_drop_used_car(_drop_foreign_city_keywords(kw.get("positive") or [], city), site_type), 200)
        neg = _kw_clean(kw.get("minus") or [], 100)
        return pos, neg

    pos, neg = _pack_keywords(tp)
    if pos:
        return pos, neg

    pos, neg = _tp67_keywords_from_real_library(slepok, site_type, tp, ct_key, city, position_name, sq)
    if pos:
        return pos, neg

    # tp7 «Товарка» по интенту близка к tp6 «Мастер кампаний»: если отдельный tp7-пул пуст,
    # берём ключи tp6 по тому же ct/позиции, чтобы не терять кампанию.
    if tp == "tp7":
        pos, neg = _pack_keywords("tp6")
        if pos:
            return pos, neg
        return _tp67_keywords_from_real_library(slepok, site_type, "tp6", ct_key, city, position_name, sq)

    return [], []

# Сверено LIVE grid 2026-06-21: Щербакова tp1 = товарные всегда; Павлов/Крючкова (wide=Модели) tp1 = нет.
# tp5 («Поиск + Динамика + Товарная Галерея») — товарные у ВСЕХ слепков (это его суть).
_SHOPPING_RULE = {"tp1": {"scherbakova"}, "tp5": {"scherbakova", "kryuchkova", "pavlov", "karavaev"}}

def _slepok_uses_shopping(slepok: str, tp: str) -> bool:
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    return key in _SHOPPING_RULE.get(tp, set())

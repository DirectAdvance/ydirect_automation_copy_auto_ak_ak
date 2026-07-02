"""Create-set creative asset and responsive-ad helpers extracted from blueprint.py."""

from __future__ import annotations

import re

_DEPS: dict = {}

MANUAL_CREATIVES_DIR = "/opt/creatives/Manual"


def configure(deps: dict) -> None:
    """Bind blueprint dependencies used by asset helpers."""
    _DEPS.clear()
    _DEPS.update(deps)
    globals().update(deps)


def _manual_creative_paths(ct_code: str) -> list:
    """Manual-креативы для ct как локальные пути на LXC.

    Источник правды теперь тот же, что и у вкладки «Контент»:
    `kontent_pack` индексирует external_assets из M3 (`/Users/Shared/agency/creatives/Manual/...`),
    а здесь мы лениво скачиваем эти файлы в локальный cache через `fetch_remote_asset`.

    Legacy-fallback `/opt/creatives/Manual/{ct}/` оставлен только если такой mount реально есть.
    """
    import os as _os
    ct = (ct_code or "").strip().lower()
    if not ct:
        return []
    out: list[str] = []

    # 1) Старый локальный mount, если он вообще существует на текущем LXC.
    folder = _os.path.join(MANUAL_CREATIVES_DIR, ct)
    try:
        if _os.path.isdir(folder):
            out.extend(sorted(
                _os.path.join(folder, f)
                for f in _os.listdir(folder)
                if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
            ))
    except Exception:  # noqa: BLE001
        pass

    # 2) Актуальный путь: Manual-ассеты из M3 index -> локальный cache path.
    try:
        idx = kp._load_index() or {}
        ext_key = f"Manual|manual|{ct}"
        rows = (idx.get("external_assets") or {}).get(ext_key) or []
        for rec in rows:
            if str(rec.get("kind") or "") != "image_manual":
                continue
            remote = str(rec.get("remote") or "").strip()
            if not remote:
                continue
            local = kp.fetch_remote_asset(remote)
            if local:
                out.append(local)
    except Exception:  # noqa: BLE001
        pass

    return sorted(dict.fromkeys(out))


# ── Анти-блок: широкая структура = десятки-сотни групп на кампанию. Пофайловый цикл
#    (3 вызова/группу) = сотни запросов → риск 429/блокировки. Лечим БАТЧИНГОМ (один
#    adgroups.add берёт до 1000 групп) + паузами между пачками + капом групп за проход.
_AC_GROUP_CAP = 150           # макс. групп на кампанию за один проход (остальное → deferred)
_AC_CHUNK_AG = 100            # групп в одном adgroups.add
_AC_CHUNK_KW = 1000           # ключей в одном keywords.add
_AC_CHUNK_AD = 100            # объявлений в одном ads.add
_AC_BATCH_SLEEP = 0.4         # пауза между батч-вызовами (троттл, сек)
# Комбинаторное объявление (RESPONSIVE_AD) — замена ТГО (TextAd), которое отключают с 30.06.2026.
# Создаётся ТОЛЬКО через v501 ads.add {ResponsiveAd:{Titles[],Texts[],Href,AdImageHashes[],...}}.
# Несколько заголовков/текстов в ОДНОМ объявлении (Яндекс комбинирует). Уточнения наследуются
# на уровне группы/кампании (поле AdExtensions у ResponsiveAd НЕ поддерживается).
_RA_TITLE_MAX = 56            # лимит длины заголовка
_RA_TEXT_MAX = 81            # лимит длины текста
_RA_TITLES_CAP = 7           # макс. заголовков в комбинаторном (как в UI Директа «… из 7»)
_RA_TEXTS_CAP = 3            # макс. текстов в комбинаторном (Яндекс: Texts от 1 до 3 — 5 = ошибка ads.add)


def _dedup_cap(items, maxlen: int, cap: int) -> list:
    """Обрезать по длине, выкинуть пустые/дубли, ограничить количеством. Дедуп — по
    НОРМАЛИЗОВАННОМУ ключу (_variant_norm_key: числа схлопнуты), чтобы «…стоянку - 45%» и
    «…стоянку - 40%» не уходили оба в одно объявление (Комбинаторное: ≤5 заголовков / ≤3 текста)."""
    out: list = []
    seen: set = set()
    for it in items or []:
        s = (str(it) or "").strip()[:maxlen]
        if not s:
            continue
        k = _variant_norm_key(s)
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
        if len(out) >= cap:
            break
    return out


def _combo_fill_titles(items: list, cap: int = _RA_TITLES_CAP) -> list:
    """Добор заголовков ResponsiveAd до 7 без ломания бренда/модели в первом заголовке."""
    src = [str(x or "").strip() for x in (items or []) if str(x or "").strip()]
    out = list(src)
    anchor = (src[0].split(".")[0].strip() if src else "Авто в кредит").rstrip(" ,.")
    if len(anchor) > 34:
        sp = anchor[:34].rfind(" ")
        anchor = anchor[:sp if sp > 15 else 34].rstrip(" ,.")
    tails = [
        "Кредит от 9 000 ₽/мес",
        "Одобрение за 30 минут",
        "КАСКО в подарок",
        "Трейд-ин выше рынка",
        "Первый взнос 0 ₽",
        "Господдержка на авто",
        "15 банков-партнеров",
        "Авто в наличии",
    ]
    for tail in tails:
        if len(out) >= cap:
            break
        cand = f"{anchor}. {tail}" if anchor else tail
        if len(cand) > _RA_TITLE_MAX:
            cand = f"{anchor} {tail}"
        if len(cand) > _RA_TITLE_MAX:
            cand = cand[:_RA_TITLE_MAX].rsplit(" ", 1)[0].rstrip(" ,.")
        # Правило Семёна: свободно ≤8 симв. Если кандидат короче hi-8 — добиваем хвостами.
        if cand and len(cand) < _RA_TITLE_MAX - 8:
            cand = _fill_title(cand, _RA_TITLE_MAX - 8, _RA_TITLE_MAX)
        if cand and cand not in out:
            out.append(cand)
    return out


def _combo_fill_texts(items: list, cap: int = _RA_TEXTS_CAP) -> list:
    """Добор текстов ResponsiveAd до 3, с разными УТП и длиной до 81.
    #26: используем _GENERIC_TEXT_FILLERS (76-81 симв) вместо коротких строк;
    _trim_to_word вместо [:max].rsplit — не срезает последнее слово у коротких текстов."""
    out = [str(x or "").strip() for x in (items or []) if str(x or "").strip()]
    for cand in _GENERIC_TEXT_FILLERS:
        if len(out) >= cap:
            break
        cand = _trim_to_word(cand, _RA_TEXT_MAX).rstrip(" ,.")
        if cand and cand not in out:
            out.append(cand)
    return out[:cap]   # seed из items мог уже быть >cap — держим контракт (ads.add: Texts ≤3)


def _credit_title_bucket(s: str) -> str:
    x = (s or "").lower()
    if re.search(r"9\s*000|/мес|плат[её]ж", x):
        return "payment"
    if re.search(r"0\s*₽|0\s*руб|первый\s+взнос", x):
        return "first_payment"
    if re.search(r"каско|1\s*год", x):
        return "kasko"
    if re.search(r"30\s*мин|одобр", x):
        return "approval"
    if re.search(r"15\s*банк|банк", x):
        return "banks"
    if re.search(r"45\s*%|скидк|выгод", x):
        return "discount"
    if re.search(r"150\s*%|трейд", x):
        return "tradein"
    if re.search(r"2026|госпрограмм|господдерж", x):
        return "state"
    if re.search(r"1\s*мин|заявк", x):
        return "apply"
    return "other"


def _credit_title_anchor(items: list[str]) -> tuple[str, str]:
    first = str((items or [""])[0] or "").strip()
    anchor = (first.split(".")[0].strip() if first else "Авто в кредит").rstrip(" ,.")
    anchor = re.sub(r"(?i)^(кредит\s+на|купить)\s+", "", anchor).strip()
    anchor = re.sub(r"(?i)\s+в\s+кредит\b", "", anchor).strip()
    brand = anchor
    m = re.match(r"(.+?)\s+в\s+[А-ЯA-ZЁ0-9]", anchor)
    if m:
        brand = m.group(1).strip()
    brand = brand.rstrip(" ,.")
    return anchor, brand or anchor


def _valid_pack_brand_name(ct: str, raw_name: str) -> str:
    name = str(raw_name or "").strip()
    low = name.lower()
    if (ct or "").strip().lower() == "ct0000":
        return ""
    if not name:
        return ""
    if low.startswith("кластер запросов не определен") or low == "полное отсутствие ключей":
        return ""
    if not _coder_name_real_brand(name):   # «Авито»/«Дром»/«Автосалон» (сегмент Общее) — НЕ марка:
        return ""                          # иначе _brand_text_set лепит «Купить Авито в кредит» в тексты
    return name


def _pack_group_display_name(ct: str, raw_name: str, brand: str = "") -> str:
    """Человекочитаемый суффикс имени группы. Для общих ct бренд остаётся пустым
    (чтобы не попасть в тексты/фильтры), но в имени группы показываем тему."""
    b = str(brand or "").strip()
    if b:
        return b
    c = (ct or "").strip().lower()
    name = str(raw_name or "").strip()
    low = name.lower()
    if c == "ct0000" or low == "полное отсутствие ключей":
        return "Общая"
    if low.startswith("кластер запросов не определен"):
        return "Общая"
    return name or "Общая"


def _trim_ad_line(s: str, maxlen: int) -> str:
    s = str(s or "").strip()
    if len(s) <= maxlen:
        return s
    cut = s[:maxlen]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.")


_DANGLING_TEXT_TAIL_RE = re.compile(
    r"(?i)(?:\s+|^)(?:и|в|во|на|по|при|с|со|для|от|без|"
    r"каско\s+на\s+1|каско\s+на|первый\s+взнос|одобрение\s+за|платеж\s+от)\s*$"
)


def _finalize_text_line(s: str, maxlen: int = _RA_TEXT_MAX, minlen: int = 73) -> str:
    """Clean generated ad text: no dangling tails and no large unused character budget."""
    line = _trim_ad_line(s, maxlen).rstrip(" ,.")
    while True:
        m = _DANGLING_TEXT_TAIL_RE.search(line)
        if not m:
            break
        head = line[:m.start()].rstrip(" ,.;:-")
        if len(head) < 35:
            break
        line = head
    if len(line) >= minlen:
        return line
    for tail in ("Звоните", "Звоните!", "Оставьте заявку.", "Узнайте условия."):
        sep = " " if line.endswith((".", "!", "?")) else ". "
        cand = f"{line}{sep}{tail}".strip()
        if len(cand) <= maxlen:
            line = cand
            if len(line) >= minlen:
                break
    return line


def _needs_credit_title_upgrade(items: list[str]) -> bool:
    seq = [str(x or "").strip() for x in (items or []) if str(x or "").strip()]
    if not seq:
        return True
    buckets = {_credit_title_bucket(x) for x in seq}
    first_words = [x.split()[0].lower().rstrip(".,!?") for x in seq if x.split()]
    same_prefix = max((first_words.count(w) for w in set(first_words)), default=0)
    missing_numbers = sum(1 for x in seq if not re.search(r"\d", x))
    return len(buckets - {"other"}) < 5 or same_prefix >= 4 or missing_numbers > 0


def _upgrade_credit_titles(items: list[str], cap: int = _RA_TITLES_CAP) -> list[str]:
    seq = [str(x or "").strip() for x in (items or []) if str(x or "").strip()]
    if not _needs_credit_title_upgrade(seq):
        return seq[:cap]
    anchor, brand = _credit_title_anchor(seq)
    brand_low = (brand or "").strip().lower()
    if not brand or brand_low.startswith("авто"):
        variants = [
            "Новые авто в кредит. Первый взнос 0 ₽",
            "Купить новое авто. КАСКО на 1 год бесплатно",
            "Платеж от 9 000 ₽/мес. Новые авто в наличии",
            "Одобрение за 30 минут онлайн. Новые авто",
            "Кредит от 15 банков онлайн. Подбор авто",
            "Выгода до 45% на новые авто. Узнайте условия",
            "Трейд-ин до 150% цены авто. Оценка онлайн",
            "Госпрограмма 2026. Кредит на новые авто",
        ]
    else:
        variants = [
            f"Кредит на {anchor}. Первый взнос 0 ₽",
            f"Купить {anchor}. КАСКО на 1 год бесплатно",
            f"Платеж от 9 000 ₽/мес. {anchor}",
            f"Одобрение за 30 минут онлайн. {anchor}",
            f"Кредит от 15 банков онлайн. {anchor}",
            f"Выгода до 45% при покупке. {anchor}",
            f"Трейд-ин до 150% цены авто. {anchor}",
            f"Госпрограмма 2026 и кредит. {brand}",
            f"Заявка на кредит за 1 минуту. {brand}",
        ]
    out: list[str] = []
    seen: set[str] = set()
    for cand in variants + seq:
        line = _trim_ad_line(cand, _RA_TITLE_MAX)
        if not line:
            continue
        low = line.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(line)
        if len(out) >= cap:
            break
    return out


def _upgrade_credit_texts(items: list[str], cap: int = _RA_TEXTS_CAP) -> list[str]:
    seq = [str(x or "").strip() for x in (items or []) if str(x or "").strip()]
    anchor, brand = _credit_title_anchor(seq or ["Авто в кредит"])
    brand_low = (brand or "").strip().lower()
    if not brand or brand_low.startswith("авто"):
        variants = [
            "Новые авто в кредит. Первый взнос 0 ₽. КАСКО на 1 год. Оставьте заявку.",
            "Платеж от 9 000 ₽/мес. Одобрение за 30 минут. Подберем условия от 15 банков.",
            "Выгода до 45% на новые авто. Трейд-ин до 150% цены автомобиля. Узнайте условия.",
        ]
    else:
        variants = [
            f"Кредит на {brand}. Первый взнос 0 ₽. КАСКО на 1 год. Заявка онлайн.",
            f"{anchor}. Платеж от 9 000 ₽/мес. Одобрение за 30 минут. Узнайте условия.",
            f"{brand} в кредит от 15 банков. Трейд-ин до 150% цены авто. Выберите авто.",
        ]
    out: list[str] = []
    seen: set[str] = set()
    for cand in seq + variants:
        line = _finalize_text_line(cand, _RA_TEXT_MAX)
        if not line:
            continue
        low = line.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(line)
        if len(out) >= cap:
            break
    if len(out) < cap:
        for cand in variants:
            line = _finalize_text_line(cand, _RA_TEXT_MAX)
            low = line.lower()
            if line and low not in seen:
                seen.add(low)
                out.append(line)
            if len(out) >= cap:
                break
    return out[:cap]


def _responsive_ad(titles, texts, href: str, image_hashes=None, display_path: str = "") -> dict | None:
    """Собрать ResponsiveAd (Комбинаторное): несколько заголовков + текстов в одном объявлении.
    → dict для ads.add ИЛИ None, если нет обязательных Titles/Texts/Href."""
    # #5/#6 Когерентность скидок в ОДНОМ объявлении (заголовки+тексты): одно ₽/%-значение (эталон —
    # самое частое) → заголовок и текст согласованы, почти-дубли с разными суммами схлопнутся дедупом.
    titles, texts = _coherent_discounts(list(titles or []), list(texts or []))
    titles = _upgrade_credit_titles(list(titles or []), _RA_TITLES_CAP)
    texts = _upgrade_credit_texts(list(texts or []), _RA_TEXTS_CAP)
    t = _dedup_cap(_combo_fill_titles(titles), _RA_TITLE_MAX, _RA_TITLES_CAP)
    x = _dedup_cap(_combo_fill_texts(texts), _RA_TEXT_MAX, _RA_TEXTS_CAP)
    if len(t) < _RA_TITLES_CAP:
        t = _dedup_cap(t + _combo_fill_titles(t), _RA_TITLE_MAX, _RA_TITLES_CAP)
    if len(x) < _RA_TEXTS_CAP:
        x = _dedup_cap(x + _combo_fill_texts(x), _RA_TEXT_MAX, _RA_TEXTS_CAP)
    if not (t and x and href):
        return None
    ad: dict = {"Titles": t, "Texts": x, "Href": href}
    imgs = [h for h in (image_hashes or []) if h]
    if imgs:
        # v501 ResponsiveAd expects raw JSON array of hashes.
        ad["AdImageHashes"] = imgs[:5]
    if display_path:
        ad["DisplayUrlPath"] = display_path[:20]
    return ad


def _responsive_image_hashes(ra: dict | None) -> list[str]:
    """Return ResponsiveAd image hashes from either v501 payload or legacy raw-list payload."""
    val = (ra or {}).get("AdImageHashes")
    if isinstance(val, dict):
        return [h for h in (val.get("Items") or []) if h]
    if isinstance(val, list):
        return [h for h in val if h]
    return []


def _responsive_retry_items(items: list[dict], *, drop_sitelinks: bool = False,
                            drop_images: bool = False) -> list[dict]:
    out = []
    for it in items or []:
        it2 = {"AdGroupId": it.get("AdGroupId"), "ResponsiveAd": dict(it.get("ResponsiveAd") or {})}
        if drop_sitelinks:
            it2["ResponsiveAd"].pop("SitelinkSetId", None)
        if drop_images:
            it2["ResponsiveAd"].pop("AdImageHashes", None)
        out.append(it2)
    return out
_CALLOUT_POOL_CAP = 200       # макс. уникальных «Уточнений» (AdExtensions) на проход
# Автотаргетинг в v5: спецключ "---autotargeting" в группе (вместо реальных ключей). НЕ ресурс
# relevancematch (его в v5 нет — 404). Проверено live на боевом аккаунте porg-36k7btt7.
_AUTOTARGET_KW = "---autotargeting"


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _normalize_callout_text(text: str) -> str:
    """Нормализовать уточнение до создания ассета Директа."""
    s = str(text or "").strip()
    # В кредитных автосвязках нужен КАСКО; ОСАГО в уточнениях было ошибкой контента.
    s = re.sub(r"(?i)\bосаго\b", "КАСКО", s)
    # Исправление типичных M3-опечаток (LLM иногда роняет букву):
    # «3 латежа» → «3 платежа»  (пропущена «п» в «платеж»)
    s = re.sub(r"(?i)\bлатеж",
               lambda m: ("П" if m.group()[0].isupper() else "п") + "латеж", s)
    # «3 платеда» → «3 платежа»  (опечатка «д» вместо «ж»)
    s = re.sub(r"(?i)\bплатед",
               lambda m: ("П" if m.group()[0].isupper() else "п") + "латеж", s)
    # «Рапродаем/рапродаём» → «Распродаем/распродаём»  (пропущена «с» в «распродаж»)
    s = re.sub(r"(?i)\bрапрода",
               lambda m: ("Р" if m.group()[0].isupper() else "р") + "аспрода", s)
    return s[:_CALLOUT_MAX_EACH].strip()


def _callout_semantic_key(text: str) -> str:
    """Смысловой ключ уточнения: не даём двум УТП одного смысла пройти как разные строки.
    Нормализует ценовые «<кредит/платёж> от N р/мес» (любая сумма → один ключ) и
    «освобождаем склад/склады/стоянку -45%» (стемминг склад*/стоянк* + нормализация
    дефисов/двоеточий «-45% / --45% / -45:» → один ключ). Иначе десятки почти-дублей с разной
    цифрой/окончанием уходят как «разные»."""
    s = str(text or "").lower().replace("ё", "е")
    s = re.sub(r"[-–—]+", "-", s)                    # любые тире (--/–/—) → один дефис
    # «Освобождаем склад/склады/стоянку -45% / --45% / -45:» → один ключ (стемминг + дефис/двоеточие)
    if re.search(r"освобожда", s) and re.search(r"(склад|стоянк|сток)", s):
        return "free_stock"
    if "шин" in s or "резин" in s:
        return "tires"
    if "каско" in s:
        return "kasko"
    # «Платеж/взнос от N р/мес» — уже схлопывались по слову; ценовой «автокредит от N р/мес» — нет.
    if "платеж" in s:
        return "payment"
    if "взнос" in s:
        return "first_payment"
    if "трейд" in s:
        return "tradein"
    if "одобр" in s:
        return "approval"
    # «Автокредит/кредит от N руб/мес» (любая сумма) → один смысловой ключ. НЕ трогаем
    # «кредит от 15 банков» (нет руб/мес → остаётся отдельным офером).
    if (re.search(r"\b(авто)?кредит\b", s) and re.search(r"\bот\b", s)
            and re.search(r"(руб|р\s*/?\s*мес|₽|/\s*мес|в\s*мес)", s)):
        return "credit_monthly"
    return re.sub(r"\s+", " ", s).strip()


# Разумный максимум показываемых уточнений на кампанию: Яндекс выводит ограниченное число,
# десятки почти-дублей бессмысленны. Пул AdExtensions на аккаунт — отдельный кап (_CALLOUT_POOL_CAP).
_CALLOUT_PER_CAMPAIGN_CAP = 8


def _dedup_callouts(texts, cap: int = _CALLOUT_PER_CAMPAIGN_CAP) -> list:
    """Семантический дедуп уточнений + кап. Один смысловой ключ (_callout_semantic_key) → одно
    уточнение; ценовые «от N р/мес» и склад/склады/стоянку схлопываются. Возвращает ≤cap строк
    (нормализованных через _normalize_callout_text). Разные смысловые оферы — сохраняются."""
    seen: set = set()
    out: list = []
    for t in texts or []:
        t = _normalize_callout_text(t)
        if not t:
            continue
        k = _callout_semantic_key(t)
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
        if len(out) >= cap:
            break
    return out


def _dedup_callout_ids(co_map: dict, cap: int = 8) -> list:
    """Нормализовать тексты из {text: id}, семантически дедупить, вернуть ≤cap id.
    Предотвращает «ОСАГО» вместо «КАСКО» и два «шины»/«шиномонтаж» в одном наборе (#24)."""
    norm_to_id: dict = {}
    for t, cid in (co_map or {}).items():
        nt = _normalize_callout_text(str(t))
        if nt and nt not in norm_to_id:
            norm_to_id[nt] = cid
    clean_texts = _dedup_callouts(list(norm_to_id), cap=cap)
    return [norm_to_id[t] for t in clean_texts if t in norm_to_id]


def _ensure_callout_exts(token: str, login: str, texts: list) -> dict:
    """Создать «Уточнения» (Callout AdExtensions) для уникальных текстов → {text: ext_id}.
    Дедуп (регистронезависимо), ≤25 симв., кап пула. Упавшие молча пропускаем (callouts необяз.)."""
    clean = _dedup_callouts(texts, cap=_CALLOUT_POOL_CAP)   # единый семантический дедуп + кап пула
    pool: dict = {}
    for chunk in _chunks(clean, 50):
        j = _v5_call("adextensions", "add", token, login,
                     {"AdExtensions": [{"Callout": {"CalloutText": t}} for t in chunk]})
        res = (j.get("result") or {}).get("AddResults", [])
        for t, r in zip(chunk, res):
            if r.get("Id"):
                pool[t] = r["Id"]
        time.sleep(_AC_BATCH_SLEEP)
    return pool

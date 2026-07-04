"""Анти-AI санитайзеры текста — вынесено из blueprint.py.

Чистые строковые нормализаторы/валидаторы (без БД/сети). Инвариант wiring-hub: НЕ импортирует
blueprint. Единственная внешняя зависимость — `_bad_credit_payment_range` (использует
blueprint-локальный `_CREDIT_PAYMENT_RANGE_RE`) инъектится через configure(); по умолчанию —
заглушка. `ai_agents` импортируется ОТЛОЖЕННО внутри функций `_bad_ad_*` (без цикла).
"""
from __future__ import annotations

import re


# ── DI: проверка кредитного платёжного коридора 9–15k (инъектится из blueprint) ──
def _bad_credit_payment_range(s: str) -> bool:
    """Заглушка. blueprint инъектит реальную (через _CREDIT_PAYMENT_RANGE_RE) в configure."""
    return False


def configure(deps: dict) -> None:
    """Инъекция зависимостей из blueprint."""
    globals().update(deps)


# ── БЛОК 1: строковые санитайзеры ─────────────────────────────────────────────────
# ── БАГ 4: замена длинного тире на точку ──────────────────────────────────────
_EMDASH_RE = re.compile(r"[—–]")   # — (U+2014), – (U+2013)
# Дефис как РАЗДЕЛИТЕЛЬ смысловых частей фразы: « - » (пробел-дефис-пробел).
# Правило Кудерко: разделитель частей заголовка — точка, не дефис.
# ТОЛЬКО пробельный дефис; внутрисловной дефис (трейд-ин, тест-драйв) НЕ трогаем.
_SEP_HYPHEN_RE = re.compile(r" - ")


def _replace_emdash(s: str) -> str:
    """Заменить длинное/короткое тире на «. » с заглавной первой буквой после точки."""
    s = str(s or "")
    parts = _EMDASH_RE.split(s)
    result = parts[0].rstrip()
    for part in parts[1:]:
        part = part.lstrip()
        capped = (part[:1].upper() + part[1:]) if part else part
        result = result + ". " + capped
    return result


def _replace_sep_hyphen(s: str) -> str:
    """Заменить дефис-разделитель « - » на «. » с заглавной буквой первого слова после точки
    (правило Кудерко: не дефис между частями фразы, а точка как разделитель предложений)."""
    def _cap_after(m: re.Match) -> str:
        # следующий символ после «. » — заглавная буква
        return ". "
    s = str(s or "")
    # Заменяем « - » на «. » и делаем заглавной букву после точки
    parts = _SEP_HYPHEN_RE.split(s)
    result = parts[0]
    for part in parts[1:]:
        capped = (part[:1].upper() + part[1:]) if part else part
        result = result + ". " + capped
    return result


# ── БАГ 8: предлоги/маленькая буква в начале текста ───────────────────────────
_FRAG_LEAD_ALL = ("до ", "с ", "за ", "по ", "из ", "от ", "в ", "на ", "у ", "к ", "о ", "со ", "и ", "а ")


def _is_bad_start(s: str) -> bool:
    """Текст начинается с предлога/союза (огрызок) или с маленькой буквы (БАГ 8)."""
    s = (s or "").strip()
    if not s:
        return True
    sl = s.lower()
    if any(sl.startswith(p) for p in _FRAG_LEAD_ALL):
        return True
    return bool(s[0].islower())


# ── БАГ 3: обрезка текста по целому слову ──────────────────────────────────────
def _trim_to_word(s: str, max_len: int) -> str:
    """Обрезать строку до max_len по последнему целому слову (БАГ 3)."""
    if len(s) <= max_len:
        return s
    cut = s[:max_len]
    sp = cut.rfind(" ")
    return (cut[:sp].rstrip(" .,!?-") if sp > max_len // 2 else cut).rstrip()


_DANGLING_NUM_TAIL_RE = re.compile(
    r"(?i)(?:[.!?]\s*)?(?:[А-Яа-яЁёA-Za-z-]+\s+){0,3}(?:за|до|от)\s+\d[\d\s\u00a0]*$"
)
_DANGLING_WORD_TAIL_RE = re.compile(
    r"(?i)(?:\s+|^)(?:в|во|на|по|при|с|со|для|от|и|или|а|но|к|ко|за|без|"
    r"выгодный|выгодные|комфортный|низкий|новый|новые|первый|кре|кредитн|"
    r"господдержк|покупк)\s*$"
)


def _strip_dangling_num_tail(s: str) -> str:
    """Убрать обрыв после обрезки: «Одобрение за 30», «платеж от 8 000» без единицы/валюты."""
    s = str(s or "").rstrip()
    m = _DANGLING_NUM_TAIL_RE.search(s)
    if not m:
        return s
    head = s[:m.start()].rstrip(" .,;:!?-")
    return head if len(head) >= 20 else s


def _strip_dangling_word_tail(s: str) -> str:
    """Убрать хвост после обрезки: предлог/незавершенное прилагательное в конце строки."""
    s = str(s or "").rstrip(" .,;:!?-")
    while True:
        m = _DANGLING_WORD_TAIL_RE.search(s)
        if not m:
            return s
        head = s[:m.start()].rstrip(" .,;:!?-")
        if len(head) < 20:
            return s
        s = head


def _sanitize_content(s: str, max_len: int = 0) -> str:
    """Единая пост-обработка: БАГ 4→исправлен (тире->точка), БАГ 8 (капитализация), БАГ 3 (обрезка по слову)."""
    s = _replace_emdash(str(s or ""))
    s = re.sub(r"(?i)\bтрейд-?ин\s+при\s+покупк[еаи]\b", "трейд-ин при оформлении кредита", s)
    s = re.sub(r"(?i)\bкредит\s+и\s+трейд-?ин\s+при\s+покупк[еаи]\b", "кредит и оценка авто в трейд-ин", s)
    s = re.sub(r"(?i)\b(?:прямо\s+)?у\s+дилера\b", "", s)
    s = re.sub(r"(?i)\bот\s+дилера\b", "", s)
    s = re.sub(r"(?i)\bдилер[а-яё]*\b", "", s)
    s = re.sub(r"\s+([,.!?])", r"\1", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" ,.")
    s = re.sub(r'(\.\s+)([а-яёa-z])', lambda m: m.group(1) + m.group(2).upper(), s)
    s = _cap_first(s)
    if max_len:
        s = _trim_to_word(s, max_len)
        s = _strip_dangling_num_tail(s)
        s = _strip_dangling_word_tail(s)
    return s.strip()


_PREFIX_RUB_RE_BP = re.compile(r"₽\s*(\d[\d\s\u00a0]*)")
_PREFIX_PCT_RE_BP = re.compile(r"%\s*(\d[\d\s\u00a0]*)")


def _normalize_numeric_suffixes_bp(s: str) -> str:
    """Нормализует порядок знаков вокруг числа: ₽9000 -> 9000₽, %45 -> 45%, 0 руб. -> 0 ₽."""
    x = str(s or "")
    x = _PREFIX_RUB_RE_BP.sub(lambda m: f"{m.group(1)}₽", x)
    x = _PREFIX_PCT_RE_BP.sub(lambda m: f"{m.group(1)}%", x)
    x = re.sub(r"(?i)(\d[\d\s\u00a0]*)\s*(?:р\.|руб\.?|рублей)\b", lambda m: f"{m.group(1)} ₽", x)
    x = re.sub(r"(\d)\s+₽\b", r"\1₽", x)
    x = re.sub(r"(\d)\s+%", r"\1%", x)
    return x


_CREDIT_RATE_RE1 = re.compile(r"(?i)(кредит\w*|рассрочк\w*|ставк\w*|переплат\w*)\s*(?:всего\s+|от\s+|под\s+)?\d+[.,]?\d*\s*%")
_CREDIT_RATE_RE2 = re.compile(r"(?i)\b(?:от\s+|под\s+)?\d+[.,]?\d*\s*%\s*годовых")
_CREDIT_RATE_RE3 = re.compile(r"(?i)\bпод\s+\d+[.,]?\d*\s*%")


def _strip_credit_rate(s: str) -> str:
    """Убрать %-СТАВКУ кредита/рассрочки (правило Семёна).
    «кредит 0%»->«кредит», «рассрочка 0%»->«рассрочка», «0% годовых»->'', «под 5%»->''. Скидки/выгоды
    в % НЕ трогаем (запрещена только ставка кредита)."""
    s = _CREDIT_RATE_RE1.sub(r"\1", str(s or ""))
    s = _CREDIT_RATE_RE2.sub("", s)
    s = _CREDIT_RATE_RE3.sub("", s)
    s = re.sub(r"(?i)\s+годовых\b", "", s)           # осиротевшее «годовых» после снятия ставки
    return re.sub(r"\s{2,}", " ", s).strip(" -—–·,.")


def _cap_first(s: str) -> str:
    """Заглавная первая буква (фикс «У дилера. авто…» → «У дилера. Авто…» при склейке)."""
    s = str(s).strip()
    return (s[:1].upper() + s[1:]) if s else s


_SENTENCE_CASE_RE_BP = re.compile(r"(^|[.!?]\s+)([a-zа-яё])")


def _sentence_case(s: str) -> str:
    """Поднять первую букву у каждого предложения."""
    return _SENTENCE_CASE_RE_BP.sub(lambda m: m.group(1) + m.group(2).upper(), str(s or ""))


# Предлоги/союзы в начале → это ОГРЫЗОК из середины предложения (после _split_utp), выкидываем.
_FRAG_LEAD = ("до ", "от ", "и ", "с ", "в ", "на ", "по ", "за ", "у ", "из ", "к ", "о ", "со ")
_RSYA_TEXT_MAX = 56          # тексты РСЯ режем жёстче (≤56), чтобы были чёткие УТП, а не каша


def _split_utp(s: str) -> list:
    """Разбить «кашеобразный» текст («А до 925000₽. Б утильсбор. В -45%. Звоните!») на ОТДЕЛЬНЫЕ
    чёткие УТП ≤56 (правило пользователя: «структура предложений»). Слабые огрызки (<8 симв,
    «Звоните!») выкидываем."""
    parts = re.split(r"(?<=[.!?])\s+|\s+[—–]\s+", str(s or ""))
    out = []
    for p in parts:
        p = p.strip().rstrip(".!?").strip()
        if 8 <= len(p) <= _RSYA_TEXT_MAX:
            out.append(p)
    return out


# ── БЛОК 2: анти-AI правила ───────────────────────────────────────────────────────
# ── АНТИ-AI ПРАВИЛА (4 штуки) ────────────────────────────────────────────────
# ПРАВИЛО 1: Блэклист AI-штампов — слова, делающие текст «генерёнкой».
_AI_STAMP_WORDS = {
    "широкий выбор", "большой выбор", "удобный", "надёжный", "уникальный",
    "инновационный", "безупречный", "высокое качество", "лучший выбор",
    "не упустите", "узнайте больше", "идеальный", "профессиональный",
    "современный", "передовой", "исключительный", "выгодное предложение",
}


def _has_stamp(text: str) -> bool:
    """True если текст содержит хотя бы один AI-штамп (регистронезависимо)."""
    tl = str(text).lower()
    return any(w in tl for w in _AI_STAMP_WORDS)


# ПРАВИЛО 2: Пул коротких ударных заголовков (<5 слов) для чередования ритма.
_SHORT_TITLE_POOL = [
    "Автомобили в кредит. Одобрение за 30 минут. КАСКО",   # 49 симв; б/у «без переплат» убрано
    "Новые авто. Выгодный кредит",
    "Трейд-ин на новое авто. Оценка машины за 30 минут",   # 49 симв; б/у «Сдай старый» убрано
    "Авто в наличии. Выгодно",
    "Автомобили в наличии. Первый взнос 0 ₽. Выбор онлайн",  # 52 симв; б/у «Купи авто» убрано
]


def _alternate_rhythm(titles: list) -> list:
    """Если все заголовки одной длины (±1 слово) — переставить для чередования
    коротких (<5 слов) и длинных (>6 слов). Если коротких нет — добрать из
    _SHORT_TITLE_POOL. Возвращает список той же длины (порядок может измениться)."""
    if len(titles) < 2:
        return titles
    counts = [len(t.split()) for t in titles]
    mn, mx = min(counts), max(counts)
    if mx - mn > 1:          # уже разнобой — не трогаем
        return titles
    # все одной длины — нужно чередование
    short = [t for t in titles if len(t.split()) < 5]
    long_ = [t for t in titles if len(t.split()) >= 5]
    # добрать коротких из пула если не хватает
    for s in _SHORT_TITLE_POOL:
        if len(short) >= (len(titles) // 2):
            break
        if s not in titles and not _bad_ad_title(s) and not _is_bad_start(s):
            short.append(s)
    # чередуем: short, long, short, long …
    result, si, li = [], 0, 0
    toggle = True
    while len(result) < len(titles):
        if toggle and si < len(short):
            result.append(short[si]); si += 1
        elif not toggle and li < len(long_):
            result.append(long_[li]); li += 1
        elif si < len(short):
            result.append(short[si]); si += 1
        elif li < len(long_):
            result.append(long_[li]); li += 1
        else:
            break
        toggle = not toggle
    # если итог короче (пул не смог добить) — дополнить оригиналами
    used = set(id(x) for x in result)
    for t in titles:
        if len(result) >= len(titles):
            break
        if id(t) not in used:
            result.append(t)
    return result[:len(titles)]


def _dedup_by_first_word(titles: list) -> list:
    """ПРАВИЛО 3: Не более 2 заголовков с одинаковым первым словом.
    Бренд-заголовки (BAIC/Haval/Lada/…) естественно начинаются с марки — жёсткое ≤1
    ограничивало набор до 3-4 вместо 7. Лимит 2 сохраняет разнообразие и не выкидывает
    половину бренд-шаблонов."""
    seen_first: dict = {}   # first_word → count
    out = []
    for t in titles:
        first = str(t).split()[0].lower().rstrip(".,!?") if t else ""
        cnt = seen_first.get(first, 0)
        if first and cnt >= 2:
            continue
        if first:
            seen_first[first] = cnt + 1
        out.append(t)
    return out


def _has_number(text: str) -> bool:
    """ПРАВИЛО 4: True если в тексте есть хотя бы одна цифра."""
    return bool(re.search(r"\d", str(text)))


# ── БЛОК 3: регэкспы плохих объявлений ────────────────────────────────────────────
_BAD_AD_TITLE_RE = re.compile(
    r"(?i)(авито|автосалон/салон|/(?!\s*мес)|низкая\s+ставка|"
    r"скидк\w*\s+до\s+-?\d+\s*%|"
    r"выгод\w*\s+до\s+\d[\d\s\u00a0]*\s*(?:₽|руб))"
)
_BAD_AD_TEXT_RE = re.compile(
    r"(?i)(автокредит|скидк\w*\s+до\s+-?\d+\s*%|выгод\w*\s+до\s+-?\d+\s*%|госпрограмм\w*\s+до\s+-?\d+\s*%|"
    r"выгод\w*\s+до\s+\d[\d\s\u00a0]*\s*(?:₽|руб)|скидк\w*\s+до\s+\d[\d\s\u00a0]*\s*(?:₽|руб))"
)
_BAD_CONTENT_RE = re.compile(
    r"(?i)(\bосаго\b|комисс\w*\s+по\s+кредит|скрыт\w*\s+комисс|"
    r"(?:госпрограмм|господдержк)\w*.*в\s+подарок|в\s+подарок.*(?:госпрограмм|господдержк)\w*|"
    r"\bот\s*0\s*(?:₽|руб\.?|рублей)?\s*(?:/мес|в\s+месяц)|"
    r"нов\w+\s+авто\s+от\s*0\s*(?:₽|руб\.?|рублей)?\s+в\s+месяц|"
    r"перв\w*\s+взнос\s+9\s*000)"
)


# ── БЛОК 4: предикаты плохих объявлений ───────────────────────────────────────────
def _bad_ad_title(s: str) -> bool:
    """Фразы, которые не должны попадать в заголовки: названия общих тем, проценты/0₽/слэши."""
    s = str(s or "")
    from . import ai_agents as A
    if A.has_forbidden_claim(s):
        return True
    if _BAD_CONTENT_RE.search(s):
        return True
    if _bad_credit_payment_range(s):
        return True
    if re.search(r"(?i)\bбез\s+переплат\b", s):
        return True
    if re.search(r"(?i)\bкассов\w*\s+взрыв\w*\b", s):
        return True
    if re.search(r"(?i)\bтрейд-?ин\b[^.]{0,24}\b(?:1[0-9]{2}|[2-9][0-9]{2})\s*%", s):
        return True
    if re.search(r"(?i)\bбез\s+документ", s):
        return True
    if re.search(r"\s[-–—]\s|(?<!\d)-\d+\s*%", s):
        return True
    if re.search(r"(?i)госпрограмм\w*.*в\s+подарок|в\s+подарок.*госпрограмм\w*", s):
        return True
    if re.search(r"(?i)\bкредит\w*\b[^.]{0,24}\bдо\s+\d{1,2}\s*%\s+скидк", s):
        return True
    if re.search(r"(?i)\bкредит\w*\b[^.]{0,28}\bдо\s+\d{1,2}\s*%(?!\s*(?:год|лет|месяц))", s):
        return True
    if re.search(r"(?i)\bкредит\s+на\b[^.]{0,36}\bдо\s+\d{1,2}\s*%", s):
        return True
    if re.search(r"(?i)\b(?:безопасн\w+\s+сделк|взнос\s+отсутствует)\b", s):
        return True
    if re.search(r"(?i)\bусловия\s+кредитован\w*\b[^.]{0,24}\bдо\s+\d[\d\s\u00a0]{4,}\s*(?:₽|руб)", s):
        return True
    if re.search(r"(?i)\bкредит\s+и\s+шин\w+\s+на\s+1\s+сезон\b", s):
        return True
    if re.search(r"(?i)\b(?:резин\w*|шин\w+)\b[^.]{0,28}\bна\s+1\s+сезон\b", s):
        return True
    if re.search(r"(?i)\bтрейд-?ин\s+при\s+покупк[еаи]\b", s):
        return True
    if re.search(r"(?i)\bтрейд-?ин\b.*\bпри\s+покупк[еаи]\b", s):
        return True
    if re.search(r"(?i)(^|[.!?]\s*)(со\s+скидк\w*|скидки\s+месяца|акци[яи])\s*$", s.strip()):
        return True
    return bool(_BAD_AD_TITLE_RE.search(s))


def _bad_ad_text(s: str) -> bool:
    """Фразы, которые не должны попадать в тексты: непроверяемые скидки/выгоды до N%/N руб."""
    s = str(s or "")
    from . import ai_agents as A
    return (A.has_forbidden_claim(s) or _bad_credit_payment_range(s)
            or bool(re.search(r"(?i)\bбез\s+переплат\b", s))
            or bool(re.search(r"(?i)\bкассов\w*\s+взрыв\w*\b", s))
            or bool(re.search(r"(?i)\bсрочно\s+прода[её]м\b|\bпозвоните\s+за\s+скидк", s))
            or bool(re.search(r"(?i)\b(?:безопасн\w+\s+сделк|взнос\s+отсутствует)\b", s))
            or bool(re.search(r"(?i)\bкредит\w*\b[^.]{0,28}\bдо\s+\d{1,2}\s*%(?!\s*(?:год|лет|месяц))", s))
            or bool(re.search(r"(?i)перв(?:ый|ого)\s+взнос\w*\s+0\s*%", s))
            or bool(re.search(r"(?i)ваш\s+нов\w+\s+автомобил\w+\s+жд[её]т|распродаж\w+\s+месяц\w+\s+стартовал", s))
            or bool(_BAD_CONTENT_RE.search(s)) or bool(_BAD_AD_TEXT_RE.search(s)))


def _bad_ad_sitelink(title: str, description: str = "") -> bool:
    """Фразы, которые не должны попадать в быстрые ссылки UAC/ЕПК."""
    s = f"{title or ''} {description or ''}"
    from . import ai_agents as A
    if A.has_forbidden_claim(s):
        return True
    if _BAD_CONTENT_RE.search(s):
        return True
    if re.search(r"(?i)\bбез\s+переплат\b", s):
        return True
    title_l = (title or "").strip().lower()
    desc_l = (description or "").strip().lower()
    if re.search(r"(?i)\b(запишитесь|записаться|запись)\b.*тест-драйв|тест-драйв.*\b(запишитесь|записаться)\b", title_l):
        return True
    if title_l in {"тест-драйв", "запишитесь на тест-драйв", "запись на тест-драйв",
                   "тест-драйв онлайн", "тест драйв онлайн"}:
        return True
    if title_l.startswith("запишитесь") and "тест-драйв" in desc_l:
        return True
    if re.search(r"(?i)\bкредит\s+и\s+шин\w+\s+на\s+1\s+сезон\b", s):
        return True
    if re.search(r"(?i)\b(?:резин\w*|шин\w+)\b[^.]{0,28}\bна\s+1\s+сезон\b", s):
        return True
    return bool(re.search(
        r"(?i)(авито|автосалон/салон|/(?!\s*мес)|низкая\s+ставка|"
        r"выгод\w*\s+до\s+\d[\d\s\u00a0]*\s*(?:₽|руб)|"
        r"скидк\w*\s+до\s+\d[\d\s\u00a0]*\s*(?:₽|руб)|кешбэк|cashback)",
        s,
    ))


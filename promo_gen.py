"""Генерация/валидация промоакций (ИИ-контент в стиле слепка) — вынесено из blueprint.py.

Извлечение JSON из ответа модели, кандидаты заголовков/текстов, валидация промо под правила
ai_agents, шаги сумм, превью, контекст промо по аккаунту. Инвариант wiring-hub: НЕ импортирует
blueprint. `_victory_conn` (БД Victory) инъектится через configure(); `ai_agents` — отложенный
sibling-импорт внутри функций; `_has/_strip_error_leak` — из llm_providers.
"""
from __future__ import annotations

import json
import re

from .llm_providers import _has_error_leak, _strip_error_leak  # чистка draft-ошибок M3


# ── DI: соединение с БД Victory (инъектится из blueprint) ──
def _victory_conn():
    """Заглушка — падает громко, если configure не отработал (а не тихо возвращает None)."""
    raise RuntimeError("promo_gen._victory_conn не инъектирован (нужен configure из blueprint)")


def configure(deps: dict) -> None:
    """Инъекция зависимостей из blueprint."""
    globals().update(deps)


# ── Промо: извлечение/валидация/превью ────────────────────────────────────────────
_MONTHS_RU = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def _promo_extract_json(text: str) -> dict:
    """Достаём первый {...}-блок из ответа модели (бывает в ```json или с болтовнёй)."""
    import re
    if not text:
        return {}
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return {}


def _extract_title_candidates(raw: dict) -> tuple[list[str], str]:
    """Нормализовать ответ M3 по заголовкам.

    Модель иногда нарушает контракт и возвращает:
    {"titles":[{"title1":"...","title2":"..."}, ...]}
    вместо ожидаемого:
    {"titles":["..."], "title2":"..."}.
    Здесь приводим оба формата к единому виду, чтобы не терять валидные заголовки.
    """
    titles: list[str] = []
    title2 = ""
    if not isinstance(raw, dict):
        return titles, title2
    raw_title2 = raw.get("title2")
    if isinstance(raw_title2, str) and raw_title2.strip():
        title2 = raw_title2.strip()
    for it in (raw.get("titles") or []):
        if isinstance(it, str):
            txt = it.strip()
            if txt:
                titles.append(txt)
            continue
        if not isinstance(it, dict):
            continue
        txt = str(
            it.get("title1")
            or it.get("title")
            or it.get("headline")
            or it.get("text")
            or ""
        ).strip()
        t2 = str(
            it.get("title2")
            or it.get("subtitle")
            or it.get("subTitle")
            or ""
        ).strip()
        if txt:
            titles.append(txt)
        if not title2 and t2:
            title2 = t2
    return titles, title2


def _extract_text_candidates(raw: dict) -> list[str]:
    """Нормализовать тексты объявлений из строкового или объектного ответа M3."""
    out: list[str] = []
    if not isinstance(raw, dict):
        return out
    for it in (raw.get("texts") or []):
        if isinstance(it, str):
            txt = it.strip()
        elif isinstance(it, dict):
            txt = str(
                it.get("text")
                or it.get("body")
                or it.get("description")
                or it.get("copy")
                or ""
            ).strip()
        else:
            txt = ""
        if txt:
            out.append(txt)
    return out


def _promo_validate(d: dict, agent: dict, site_type: str = "") -> tuple[dict, list[str]]:
    """Нормализуем/клампим промо под лимиты Директа. → (promo, warnings).
    site_type — для гарда «не та лексика типу сайта» (б/у на сайте про НОВЫЕ авто)."""
    from . import ai_agents as A
    p = agent["promo"]
    warns: list[str] = []

    typ = str(d.get("type") or "").upper()
    if typ not in A.PROMO_TYPES:
        typ = p["type"]
    if typ == "CASHBACK":          # ⛔ глобальный запрет кешбэка → заменяем тип на дефолт стиля
        typ = p["type"] if p["type"] != "CASHBACK" else "DISCOUNT"
        warns.append("кешбэк запрещён — тип акции заменён")
    unit = str(d.get("unit") or "").upper()
    if unit not in A.PROMO_UNITS:
        unit = p["unit"]
    if typ == "GIFT":          # подарок всегда в рублях (до клампа, чтобы не срезать сумму по %-капу)
        unit = "RUB"

    amount = d.get("amount")
    try:
        amount = int(float(amount))
    except (TypeError, ValueError):
        amount = (p["amount_min"] + p["amount_max"]) // 2
    cap = A.AMOUNT_MAX_PCT if unit == "PCT" else A.AMOUNT_MAX_RUB
    if amount < 1:
        amount = 1
    if amount > cap:
        amount = cap
        warns.append(f"размер обрезан до {cap} ({unit})")
    # Реалистичность размера ПОД ТИП акции. Подарок — стоимость в ₽ (десятки–сотни тыс.),
    # а НЕ процентное число, ошибочно прочитанное как рубли («Подарок до 58 ₽» — баг).
    import random as _rnd
    if typ == "GIFT":
        unit = "RUB"
        if not (A.GIFT_AMOUNT_MIN <= amount <= A.GIFT_AMOUNT_MAX):
            amount = _rnd.choice(A.GIFT_STEPS)
            warns.append("сумма подарка приведена к реалистичной (₽)")

    prefix = str(d.get("prefix") or "").upper()
    if prefix not in A.PROMO_PREFIXES:
        prefix = p.get("prefix")          # может быть None — это ок

    promocode = (d.get("promocode") or "").strip()[:A.PROMOCODE_MAX] or None

    desc = (d.get("description") or "").strip().strip('"').replace("\n", " ")
    # подстраховка: если служебная ошибка драфт-модели просочилась ВНУТРЬ JSON-описания — режем её
    if _has_error_leak(desc):
        desc = _strip_error_leak(desc).strip()
        warns.append("убран обрывок служебной ошибки M3 из описания")
    # бан-фраза «закрытие …» → подменяем дефолтом стиля агента
    if any(b in desc.lower() for b in A.BANNED_SUBSTR):
        desc = (p.get("examples") or ["спецпредложение"])[0]
        warns.append("убрана запрещённая формулировка («закрытие автосалона»)")
    if A.has_forbidden_claim(desc):
        desc = (p.get("examples") or ["спецпредложение"])[0]
        warns.append("убрана запрещённая формулировка («гарантия»)")
    # ⛔ глобальный запрет кешбэка: вычищаем упоминание из описания
    if A.has_cashback(desc):
        fixed = A.strip_cashback(desc)
        desc = fixed if len(fixed.strip()) >= 3 else (p.get("examples") or ["спецпредложение"])[0]
        warns.append("убрано упоминание кешбэка (запрещён)")
    # ГАРД ТИПА САЙТА: на сайте про НОВЫЕ авто (Монобренд/Мультибренд/Квиз) лексика
    # «с пробегом / б/у» неуместна → вычищаем её из описания (зеркало бан-листа для б/у).
    if (site_type or "").strip() in A.NEW_ONLY_SITE_TYPES and A._bad_for_new(desc):
        fixed = A.strip_used_words(desc)
        if A.eff_len(fixed) >= 3:
            desc = fixed
        else:   # после чистки почти ничего не осталось — берём дефолт стиля агента
            desc = (p.get("examples") or ["на новые автомобили"])[0]
        warns.append("убрана лексика «с пробегом/б/у» — сайт про новые авто")
    # Грамматическая чистка ДО обрезки: двойной предлог «на при покупке» → «при покупке»,
    # приклеенный второй обрывок с заглавной («…авто Распродаём склад») → режем, висящие хвосты.
    desc = A.fix_promo_desc(desc)
    if re.search(r"(?<![\d])\d{4,}(?![\d\s]*(?:₽|руб|/мес))", desc):
        desc = (p.get("examples") or ["спецпредложение"])[0]
        warns.append("убрано техническое число из описания промо")
    # Длина описания «как режет ГРИД»: считаются ВСЕ символы (пробелы И знаки препинания).
    # Жёсткий лимит грида ≤ 25 (A.DESCRIPTION_MAX) — без template-маркера. Длиннее → grid
    # отклоняет промо (TEXT_LENGTH_WITHOUT_TEMPLATE_MARKER_CANNOT_BE_MORE_THAN).
    lim = A.DESCRIPTION_MAX
    if len(desc) > lim:
        words = desc.split(" ")
        while len(words) > 1 and len(" ".join(words)) > lim:
            words.pop()
        desc = " ".join(words)
        while len(desc) > lim:   # одно длинное слово — режем посимвольно
            desc = desc[:-1]
        desc = desc.rstrip()
        # после обрезки мог остаться висящий предлог в конце («…новый Haval при») — срезаем
        desc = A._promo_strip_tail(desc)
        warns.append(f"описание обрезано до {lim} симв. (лимит грида, считаются все символы)")
    if len(desc.strip()) < 3:
        desc = (p.get("examples") or ["спецпредложение"])[0]
        warns.append("описание было пустым — подставлен дефолт стиля")
    # с МАЛЕНЬКОЙ буквы (описание — продолжение фразы), кроме брендов-аббревиатур
    fw = desc.split()[0] if desc.split() else ""
    if fw and fw[0].isalpha() and not (len(fw) >= 2 and fw.isupper()):
        desc = desc[0].lower() + desc[1:]

    # Дату окончания акции НЕ указываем (по требованию) — промо показывается всегда.
    return ({"type": typ, "amount": amount, "unit": unit, "prefix": prefix,
             "description": desc, "promocode": promocode, "finishDate": None}, warns)


def _promo_amount_steps(p: dict, unit: str, promo_type: str = "") -> list[int]:
    """«Красивые» шаги размера в диапазоне агента — для вариативности при регенерации.
    Для подарка (GIFT) — реалистичные суммы стоимости подарка, а не процентный диапазон агента."""
    from . import ai_agents as A
    if (promo_type or "").upper() == "GIFT":
        return list(A.GIFT_STEPS)
    pool = ([40, 43, 45, 48, 50, 53, 55, 57, 60, 63] if unit == "PCT"
            else [700_000, 800_000, 900_000, 1_000_000, 1_200_000, 1_300_000, 1_500_000])
    lo, hi = int(p.get("amount_min") or 1), int(p.get("amount_max") or (100 if unit == "PCT" else 1_000_000))
    steps = [x for x in pool if lo <= x <= hi]
    return steps or [(lo + hi) // 2]


def _promo_preview(promo: dict) -> str:
    """Эмуляция итогового Name Директа: «{Тип} {преф} {размер} {описание} до {дата}»."""
    from . import ai_agents as A
    parts = [A.TYPE_WORD.get(promo["type"], promo["type"])]
    if promo.get("prefix"):
        parts.append(A.PREFIX_WORD.get(promo["prefix"], ""))
    if promo.get("amount") is not None:
        parts.append(f"{promo['amount']}%" if promo["unit"] == "PCT" else f"{promo['amount']:,} ₽".replace(",", " "))
    if promo.get("description"):
        parts.append(promo["description"])
    if promo.get("promocode"):
        parts.append(f"промокод {promo['promocode']}")
    txt = " ".join([x for x in parts if x])
    f = promo.get("finishDate")
    if f:
        try:
            y, mo, da = f.split("-")
            txt += f" до {int(da)} {_MONTHS_RU[int(mo)]}"
        except Exception:  # noqa: BLE001
            pass
    return txt


def _promo_ctx(login: str) -> dict | None:
    """Лёгкий контекст салона для генерации: domain/salon/city/site_type/agency."""
    import psycopg2.extras
    conn = _victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT domain, salon, city, site_type, agency_account FROM public.local_gsheet_sites "
                    "WHERE login_key=%s AND direction='Авто' LIMIT 1", (login,))
        return cur.fetchone()
    finally:
        conn.close()




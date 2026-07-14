"""Кодер-имена кампаний (марка/модель из ct-кодера) + ротатор Title2 — вынесено из blueprint.py.

DI (инъектятся из blueprint через configure): `_victory_conn` (БД Victory), `_ct_segment`
(сегмент по ct), `_is_brand_canon`/`_brand_canon` (канонизация бренда, обёртки create_set_feeds).
Кэши `_AG1_*`/`_TITLE2_*` переезжают сюда — единый источник мемоизации и round-robin Title2.
Инвариант wiring-hub: НЕ импортирует blueprint.
"""
from __future__ import annotations

import re


# ── DI: инъектятся из blueprint (заглушки падают громко, если configure не отработал) ──
def _victory_conn():
    raise RuntimeError("campaign_naming._victory_conn не инъектирован (configure)")


def _ct_segment(*a, **k):
    raise RuntimeError("campaign_naming._ct_segment не инъектирован (configure)")


def _is_brand_canon(*a, **k):
    raise RuntimeError("campaign_naming._is_brand_canon не инъектирован (configure)")


def _brand_canon(*a, **k):
    raise RuntimeError("campaign_naming._brand_canon не инъектирован (configure)")


def configure(deps: dict) -> None:
    """Инъекция зависимостей из blueprint."""
    globals().update(deps)


# ── Бренд/ct из кодера + ротатор Title2 ───────────────────────────────────────────
_AG1_NAME_CACHE: dict | None = None
_CT4_RE = re.compile(r"ct\d{4}")


def _ag_part1_map() -> dict:
    """ct-код → имя из gsheet_naming (ag_part1) + leadgen_ct_naming (ct0800+, dmp).

    Два источника:
    1. public.gsheet_naming type='ag_part1' — авто-слепки (ct0001–ct0319).
    2. public.leadgen_ct_naming — leadgen-слепок dmp. Структура dmp сейчас использует
       ct0800–ct0834 (tp2-темы 0800–0833 + МК Конкуренты 0834). Коды из авто-источника НЕ
       перезаписываются: если ct уже есть в gsheet_naming — leadgen-запись игнорируется.
       ⚠️ Поэтому ct0032/ct0084 (совпадают с авто Changan CS55 / FAW) РЕЗОЛВЯТСЯ В АВТО-ИМЯ,
       а leadgen-строки «Бренд»/«Конкуренты» мертвы → для dmp Бренд/Конкуренты применять
       ВЫДЕЛЕННЫЕ ct08xx вне авто-пространства (Конкуренты=ct0834). ⚠️ Часть ct0822–ct0834
       ещё БЕЗ имени в leadgen_ct_naming (naming gap) — резолвятся в '' (общий B2B-контент).
    Кэш на процесс."""
    global _AG1_NAME_CACHE
    if _AG1_NAME_CACHE is not None:
        return _AG1_NAME_CACHE
    m: dict = {}
    try:
        conn = _victory_conn()
        try:
            cur = conn.cursor()
            # 1) авто-кодер (основной источник)
            cur.execute("SELECT code, name FROM public.gsheet_naming WHERE type='ag_part1'")
            for code, name in cur.fetchall():
                if code and name:
                    m[str(code).strip()] = str(name).strip()
            # 2) leadgen-кодер dmp (ct0800+): добавляем ТОЛЬКО коды, которых нет в авто-источнике
            cur.execute("SELECT ct, name FROM public.leadgen_ct_naming")
            for ct, name in cur.fetchall():
                ct = str(ct).strip()
                name = str(name).strip()
                if ct and name and ct not in m:
                    m[ct] = name
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — БД недоступна → без бренда (общий контент)
        pass
    _AG1_NAME_CACHE = m
    return m


_AG1_REV_CACHE: dict | None = None


def _ag_part1_rev() -> dict:
    """Обратная карта имя_модели(lower) → ct (ag_part1). Для tp6/tp7, где модель — в НАЗВАНИИ
    группы («Haval Jolion»), а не в ct кодера: по имени достаём ct модели (ct0119) для
    кодера/картинки/текста. Берём только реальные марки/модели (пропускаем темы/общие)."""
    global _AG1_REV_CACHE
    if _AG1_REV_CACHE is not None:
        return _AG1_REV_CACHE
    rev: dict = {}
    for ct, name in _ag_part1_map().items():
        nm = (name or "").strip().lower()
        if not nm or nm.startswith("кластер запросов не определен") or nm == "полное отсутствие ключей":
            continue
        rev.setdefault(nm, ct)
    _AG1_REV_CACHE = rev
    return rev


def _ct_for_name(name: str) -> str:
    """ct модели по её имени (название группы tp6/tp7). Нет совпадения → '' (общий контент)."""
    raw = (name or "").strip()
    low = raw.lower()
    rev = _ag_part1_rev()
    if low in rev:
        return rev[low]
    base = re.split(r"\s+-\s+", low, maxsplit=1)[0].strip()
    if base in rev:
        return rev[base]
    norm = re.sub(r"[^a-zа-яё0-9]+", " ", base).strip()
    if not norm:
        return ""
    # Фолбэк для структурных подписей вида "Lada Granta - Ключевики":
    # ищем самую длинную модель, входящую целиком в начало/текст названия.
    for nm, ct in sorted(rev.items(), key=lambda x: len(x[0]), reverse=True):
        nn = re.sub(r"[^a-zа-яё0-9]+", " ", nm).strip()
        if nn and (norm == nn or norm.startswith(nn + " ") or (" " + nn + " ") in (" " + norm + " ")):
            return ct
    return ""


# ── Title2: загрузка из БД и выбор по кругу ───────────────────────────────────
_TITLE2_CACHE: list | None = None
_TITLE2_IDX: int = 0


_TITLE2_BLOCK_CACHE: tuple | None = None


def _title2_blocklist() -> tuple[set, set]:
    """Слова, которых НЕ должно быть в обобщённом Title2: названия марок/моделей (сегменты
    Марки/Модели из gsheet_naming — НЕ темы «Авто/Автосалон/Авито») + города (local_gsheet_sites).
    Пул Title2 общий на ВСЕ аккаунты → бренд/город конкретного слепка туда попадать не должны
    (иначе «Автосалон Lada в Тольятти» бледит в группу BAIC/Кемерово). Кэш на процесс."""
    global _TITLE2_BLOCK_CACHE
    if _TITLE2_BLOCK_CACHE is not None:
        return _TITLE2_BLOCK_CACHE
    brands: set = set()
    try:
        for ct, nm in _ag_part1_map().items():
            if _ct_segment(ct) in ("Марки", "Модели"):
                w = (nm or "").strip().lower().split()
                if w and len(w[0]) >= 3:                 # ведущее слово = марка (lada, baic, chery…)
                    brands.add(w[0])
    except Exception:  # noqa: BLE001
        brands = set()
    cities: set = set()
    try:
        conn = _victory_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT lower(city) FROM public.local_gsheet_sites "
                        "WHERE direction='Авто' AND city IS NOT NULL AND city<>''")
            cities = {r[0].strip() for r in cur.fetchall() if r[0] and r[0].strip() and len(r[0].strip()) >= 4}
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        cities = set()
    _TITLE2_BLOCK_CACHE = (brands, cities)
    return _TITLE2_BLOCK_CACHE


def _title2_is_generic(text: str, brands: set, cities: set) -> bool:
    """True, если Title2 НЕ содержит конкретной марки/модели и города (обобщённый УТП)."""
    words = set(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())
    return not (words & brands) and not (words & cities)


def _load_title2_pool() -> list[str]:
    """Загрузить пул Title2 из public.direct_title2 (Victory). Кэш на процесс.
    Фолбэк-список встроен — сервис работает даже без БД. Авто-фильтр: строки с конкретной
    маркой/городом отсеиваются (Title2 обязан быть обобщённым, без чужого бренда/города)."""
    global _TITLE2_CACHE
    if _TITLE2_CACHE is not None:
        return _TITLE2_CACHE
    fallback = [
        "Авто в наличии", "Официальный дилер", "Кредит с господдержкой",
        "Trade-in в день обращения", "Тест-драйв без записи",
        "Выгода до 200 000 руб.", "КАСКО на 1 год",
        "Выгодные условия покупки", "Быстрое оформление за 1 час",
        "Приятные бонусы",
    ]
    try:
        conn = _victory_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT text FROM public.direct_title2 "
                "WHERE site_type='all' ORDER BY usage_count DESC, id"
            )
            rows = [r[0].strip() for r in cur.fetchall() if r[0] and r[0].strip()]
        finally:
            conn.close()
        # Авто-фильтр: убрать Title2 с конкретной маркой/городом (чужой бренд не должен бледить).
        br, ci = _title2_blocklist()
        if br or ci:
            rows = [t for t in rows if _title2_is_generic(t, br, ci)]
        _TITLE2_CACHE = rows if rows else fallback
    except Exception:  # noqa: BLE001
        _TITLE2_CACHE = fallback
    return _TITLE2_CACHE


def _next_title2() -> str:
    """Выбрать следующий Title2 по кругу из пула (round-robin).
    Round-robin даёт разнообразие в рамках одного пакетного прогона."""
    global _TITLE2_IDX
    pool = _load_title2_pool()
    if not pool:
        return ""
    t2 = pool[_TITLE2_IDX % len(pool)]
    _TITLE2_IDX += 1
    return t2


# ── Бренд из кодера (реальная марка) ──────────────────────────────────────────────
def _coder_name_real_brand(name: str) -> bool:
    """True если имя ag_part1 — РЕАЛЬНАЯ марка/модель авто (а не «Общее»-метка: Авито/Дром/Дзен/
    Автокредит/Trade-in). Без этой защиты источник/тема трактуется как бренд → заголовки требуют
    токен бренда («авито») → все отбиты → 0 валидных → краш «нужен заголовок» (и товарный feedFilter
    field=model=[«Дром»] → UNKNOWN_FIELD). Тот же справочник, что разводит ct на Марки/Модели/Общее."""
    nm = (name or "").strip()
    if not nm:
        return False
    return _is_brand_canon(_brand_canon(re.split(r"[\s/]+", nm.lower())[0]))


def _brand_ct_from_coder(item: dict) -> tuple[str, str]:
    """(марка/модель, ct) из КОДЕРА: ПЕРВЫЙ ct#### (≠ct0000), у которого ag_part1 — реальная
    марка/модель. Источник — gc (tp1-5) / code|c (tp6/7) / name. Правило пользователя:
    есть марка/модель в ct → контент по модели; ct0000/нет/НЕ-марка → общий. → ('','') если нет."""
    if not isinstance(item, dict):
        return "", ""
    explicit_ct = str(item.get("coder_ct") or item.get("ct") or "").strip()
    if explicit_ct:
        name = _ag_part1_map().get(explicit_ct)
        if (explicit_ct == "ct0000" or not name or name.startswith("кластер запросов не определен")
                or name == "полное отсутствие ключей" or not _coder_name_real_brand(name)):
            return "", explicit_ct
        return name, explicit_ct
    for key in ("coder_ct", "ct"):
        ct = str(item.get(key) or "").strip()
        if ct and ct != "ct0000":
            name = _ag_part1_map().get(ct)
            if (name and not name.startswith("кластер запросов не определен")
                    and name != "полное отсутствие ключей" and _coder_name_real_brand(name)):
                return name, ct
    direct_brand = str(item.get("coder_brand") or item.get("brand") or "").strip()
    if direct_brand:
        direct_ct = _ct_for_name(direct_brand)
        if direct_ct and direct_ct != "ct0000":
            _nm = _ag_part1_map().get(direct_ct, direct_brand)
            return (_nm if _coder_name_real_brand(_nm) else ""), direct_ct
    for key in ("gc", "code", "c", "name"):
        code = str(item.get(key) or "")
        all_ct = _CT4_RE.findall(code)
        if len(set(all_ct)) > 1:
            return "", explicit_ct or ""
        for mt in _CT4_RE.finditer(code):
            ct = mt.group(0)
            if ct == "ct0000":
                continue
            name = _ag_part1_map().get(ct)
            if (name and not name.startswith("кластер запросов не определен")
                    and name != "полное отсутствие ключей" and _coder_name_real_brand(name)):
                return name, ct
    return "", ""


def _brand_from_coder(item: dict) -> str:
    """Марка/модель кампании из КОДЕРА: ПЕРВЫЙ ct с 4 цифрами (ct####) → имя ag_part1.
    Источник — групповой кодер gc (tp1-5) или кампанийный c/code (tp6/7). ct0000 / нет → ''.
    Прямое поле item['brand'] имеет приоритет (если фронт уже прислал марку)."""
    if not isinstance(item, dict):
        return ""
    direct = str(item.get("brand") or "").strip()
    if direct:
        return direct
    return _brand_ct_from_coder(item)[0]


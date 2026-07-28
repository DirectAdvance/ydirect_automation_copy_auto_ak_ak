"""Create-set minus-keyword helpers extracted from blueprint.py."""

from __future__ import annotations

import json
import os
import re
import threading

_DEPS: dict = {}


def configure(deps: dict) -> None:
    """Bind blueprint dependencies used by minus helpers."""
    _DEPS.clear()
    _DEPS.update(deps)
    globals().update(deps)


_MINUS_SET_NAME_MARKER = "Минуса общие"  # маркер имени, как у слепков Щербаковой

# ── Geo-strip guard: не допускаем собственный город аккаунта в минус-слова ──────────────────────
# Если город аккаунта (или его склонная/адъективная форма) попадёт в минус-список кампании,
# она заминусует собственный трафик.  Применяется ко ВСЕМ трём источникам минусов
# (global, shared-set, pack) сразу перед финальным _minus_char_budget.

_OP_STRIP_CHARS = "!+-[]\""   # операторные символы Яндекс.Директа (токен под оператором)


def _city_geo_stems(city_name: str) -> list[str]:
    """Prefix stems для всех падежных/адъективных форм русского названия города.

    Возвращает список lowercase-стемов.  Минус-фраза ОТСЕИВАЕТСЯ, если хотя бы
    один её токен (после снятия операторов) начинается с любого из стемов.

    Алгоритм:
    • Noun stem: снимаем флексию существительного (а/я/ь/е/о) → корень падежных форм.
      Используем root как stем только если len(root) ≥ 5 (иначе ложные срабатывания);
      при коротком root берём полную именительную форму (как fallback-стем).
      ⚠ Для КОРОТКИХ городов (root < 5) именительная форма как стем НЕ покрывает склонения —
      «уфе/уфы» не начинаются с «уфа».  Падежные формы коротких городов закрывает
      _city_geo_declensions (точное совпадение токена), вызываемая из _strip_account_geo.
    • Adj stem: root + «ск» — покрывает ский/ская/ского/ской/ском и т.д.

    Примеры:
      «волгоград» → ['волгоград', 'волгоградск']
      «самара»    → ['самар', 'самарск']
      «казань»    → ['казан', 'казанск']
      «пермь»     → ['пермь', 'пермск']   # root «перм» = 4 < 5; «перми» поймает _city_geo_declensions
      «уфа»       → ['уфа']              # adj_stem «уфск» < 5 → не добавляем; «уфе/уфы» → declensions
    """
    base = city_name.strip().lower()
    if not base:
        return []
    # Вычисляем declension root (без флексии)
    if base.endswith(("а", "я", "е", "о")):
        root = base[:-1]
    elif base.endswith("ь"):
        root = base[:-1]
    else:
        root = base   # согласный финаль: волгоград, новосибирск, краснодар

    stems: list[str] = []
    # Noun stem (для падежных форм)
    noun_stem = root if len(root) >= 5 else base
    stems.append(noun_stem)
    # Adj stem = root + «ск» (волгоградск*, самарск*, казанск*, пермск*)
    if root.endswith("ск"):
        adj_stem = root          # уже оканчивается на «ск» (редко)
    else:
        adj_stem = root + "ск"
    if len(adj_stem) >= 5 and adj_stem not in stems:
        stems.append(adj_stem)
    return stems


# ── Exact declension forms for SHORT cities (root < 5) ───────────────────────────────────────────
# Irregular short cities whose genitive/dative/prepositional forms can't be safely
# derived by mechanical suffix-stripping (e.g. Орёл→Орла involves vowel alternation).
_CITY_EXACT_IRREGULAR: dict[str, set[str]] = {
    # Орёл: ё/о alternation in declension (Орёл→Орла, not Орёла)
    "орёл": {"орёл", "орла", "орлу", "орле", "орлом", "орлах"},
    "орел": {"орел", "орла", "орлу", "орле", "орлом", "орлах"},  # ascii variant
}


def _city_geo_declensions(city_name: str) -> set[str] | None:
    """Return exact declension forms for SHORT cities where prefix-stem matching is unsafe.

    Returns None for cities with root >= 5 chars (prefix stems from _city_geo_stems suffice).
    For short cities returns a set of lowercase exact word forms (nominative + common
    declensions) to be matched as WHOLE TOKEN EQUALITY (core == form), NOT as a prefix.

    This prevents two failure modes:
    1. «уфе/уфы» not caught by prefix «уфа» (short-city declined forms miss).
    2. «ковровое покрытие» falsely caught by prefix «ковров» when account is NOT in Ковров.
       (Ковров has root len=6 → stays in stem mode; exact mode isn't used for it.)

    Declension derivation:
      -а ending (Уфа):    base + ы/е/у/ой/ою  (standard first-declension feminine)
      -ь ending (Пермь/Тверь): root + и/ью     (third-declension feminine)
      consonant ending:   hardcoded irregular table or nominative only (safe fallback)

    Examples:
      «уфа»   → {'уфа','уфы','уфе','уфу','уфой','уфою'}
      «пермь» → {'пермь','перми','пермью'}
      «тверь» → {'тверь','твери','тверью'}
      «орёл»  → {'орёл','орла','орлу','орле','орлом','орлах'}  # irregular
      «самара»→ None  (root «самар» len=5 ≥ 5 → prefix stem mode)
    """
    if not city_name:
        return None
    b = city_name.strip().lower()
    if not b:
        return None

    # Compute root (same logic as _city_geo_stems)
    if b.endswith(("а", "я", "е", "о")):
        root = b[:-1]
    elif b.endswith("ь"):
        root = b[:-1]
    else:
        root = b

    if len(root) >= 5:
        return None  # long city — prefix stem mode is sufficient

    # Irregular table first
    if b in _CITY_EXACT_IRREGULAR:
        return set(_CITY_EXACT_IRREGULAR[b])

    forms: set[str] = {b}  # nominative always included

    if b.endswith(("а", "я")):
        r = b[:-1]
        # Standard first-declension: gen=ы, dat=е, acc=у, instr=ой/ою, prep=е
        forms.update({r + "ы", r + "е", r + "у", r + "ой", r + "ою"})
    elif b.endswith("ь"):
        r = b[:-1]
        # Third-declension feminine: gen/dat/prep=и, instr=ью
        forms.update({r + "и", r + "ью"})
    # else: short consonant-ending city not in irregular table → nominative only (safe fallback)

    return forms


def _region_geo_stems(region: str) -> list[str]:
    """Извлечь adj-стемы из строки области (напр. «Волгоградская область» → «волгоградск»).

    Строка может нести НЕСКОЛЬКО областей через запятую («Волгоградская область, Ростовская
    область» — мультирегиональный аккаунт, `_account_ctx` возвращает `oblasts` списком), поэтому
    разбираем каждую часть, как `_strip_account_geo` уже делает с городами.  У каждой части берём
    первое слово (прилагательное), снимаем адъективное окончание и восстанавливаем стем на «ск».
    Возвращает [] если строка пустая или ни одна часть не распознаётся как прилагательное.
    """
    if not region:
        return []
    stems: list[str] = []
    for part in re.split(r"[,;/]", str(region)):
        first = part.strip().lower().split()[0] if part.strip() else ""
        if not first:
            continue
        for sfx in ("ская", "ский", "ское", "ские", "ских", "ского", "ской", "ском", "скому", "скими"):
            if first.endswith(sfx) and len(first) > len(sfx):
                root = first[: -len(sfx)]
                stem = root + "ск"
                if len(stem) >= 5 and stem not in stems:
                    stems.append(stem)
                break
    return stems


def _strip_account_geo(minus_words: list[str], city: str, region: str = "") -> list[str]:
    """Убрать из minus_words фразы, содержащие собственный город/регион аккаунта.

    *city*   — строка города аккаунта (может быть «Самара, Тольятти» — мультигород).
    *region* — строка области/региона («Волгоградская область»), опционально.

    Для каждой фразы из minus_words: если хотя бы один токен (после снятия операторов
    !+-[]") начинается с вычисленного города/региона стема — фраза отсеивается.
    Фразы без гео-вхождений остаются нетронутыми.

    Returns: отфильтрованный список в исходном порядке.
    """
    if not minus_words or (not city and not region):
        return minus_words

    all_stems: list[str] = []
    # all_exact: whole-token equality set for short cities (root < 5 chars).
    # Short cities use exact declension matching instead of prefix matching to avoid
    # both missed forms («уфе» not caught by prefix «уфа») and over-stripping
    # («уфавтодор» falsely caught by prefix «уфа»).
    all_exact: set[str] = set()

    for part in city.split(","):
        p = part.strip()
        if not p:
            continue
        for s in _city_geo_stems(p):
            if s and s not in all_stems:
                all_stems.append(s)
        forms = _city_geo_declensions(p)
        if forms:
            all_exact.update(forms)

    for s in _region_geo_stems(region):
        if s and s not in all_stems:
            all_stems.append(s)

    if not all_stems and not all_exact:
        return minus_words

    def _tok_core(tok: str) -> str:
        return tok.strip(_OP_STRIP_CHARS).lower()

    def _phrase_has_own_geo(phrase: str) -> bool:
        for tok in phrase.lower().split():
            core = _tok_core(tok)
            if not core:
                continue
            # Whole-token equality for short cities (declension set)
            if core in all_exact:
                return True
            # Prefix-stem match for long cities and adjective forms (…ская область)
            for stem in all_stems:
                if core.startswith(stem):
                    return True
        return False

    stripped = [w for w in minus_words if not _phrase_has_own_geo(w)]
    return stripped
# Лимиты Директа, символы БЕЗ пробелов (офиц. дока + v5 ref, см. CODER.md):
_MINUS_SHARED_SET_CHAR_BUDGET = 4_096    # библиотечный набор (negativekeywordsharedsets) — как группа
_MINUS_CAMPAIGN_CHAR_BUDGET = 20_000     # минусы НАПРЯМУЮ на кампании (NegativeKeywords кампании)
# Карта механизма привязки минусов по слепку (как в РЕАЛЬНЫХ аккаунтах — live-аудит):
#   campaign   → NegativeKeywords прямо на кампании (≤20 000 симв. без пробелов) — pavlov, kryuchkova
#   shared_set → переиспользовать/создать набор «Минуса общие», привязать через NegativeKeywordSharedSetIds — scherbakova
#   group      → NegativeKeywords на каждой группе объявлений (≤4 096 симв./группа) — terehov
# Default для неизвестного слепка — "group" (безопасно, текущее поведение).
_SLEPOK_MINUS_MODE: dict[str, str] = {
    "pavlov": "campaign",
    "kryuchkova": "campaign",
    "scherbakova": "shared_set",
    "terehov": "group",
    "karavaev": "group",
    # kuderko — ЯВНО «group» (2026-07-28). Раньше слепка тут не было и он молча падал в дефолт
    # «group»; запись поведение НЕ меняет, она снимает молчаливый дефолт. Именно «group», а не
    # «shared_set»/«campaign», потому что у kuderko минуса живут ПО ГРУППАМ (118 per-group файлов
    # {slepok}__<slug>_minus.txt в tp2, живой кабинет: 331 группа из 720 с минусами), а кампанийных
    # источников нет ФИЗИЧЕСКИ: {slepok}_minus_shared.txt = 0 файлов и ct-уровневый {slepok}_minus.txt
    # = 0 файлов → `_collect_pack_minus` даёт 0 фраз в ЛЮБОМ режиме. Смена режима на не-group только
    # СНЯЛА бы минуса с групп (`apply_group_minus`), ничего не добавив.
    "kuderko": "group",
}
# Остальные слепки (_SLEPOK_CANONICAL в pack_resolver.py) в карте НЕ перечислены и работают в
# дефолтном «group»: salamahin, gordeeva, zubakin, chepelev, tumashenko, piterkina, avto_sk,
# avtolajt_bu, sk_krs, gen_ses, dmp.

# ── ИМЕНОВАННЫЕ наборы минус-фраз слепка → БИБЛИОТЕКА минус-фраз аккаунта ────────────────────────
# Источник: {site_type}/_minus_sets/{slepok}.json в паке (пишет slepki_editor.apply_save_minus_sets,
# читает UI slepki_editor.read_minus_sets). Решение Семёна 2026-07-28: эти наборы должны попадать
# в библиотеку минус-фраз аккаунта и привязываться к кампаниям tp2-tp5.
# Путь НЕ зависит от {slepok}_minus_shared.txt / ct-уровневого {slepok}_minus.txt: у kuderko их нет
# вовсе (0 файлов), а наборы есть (4 набора, 1635 фраз).
#
# ЛИМИТЫ ДИРЕКТА (офиц. справка, .claude/skills/yandex-direct/docs/keywords/negative-keywords-library.md):
#   :12  «Вы можете создать до 30 таких наборов»                → на аккаунт
#   :22/:28/:30 «Максимально допустимое количество символов — 4096 без учета пробелов» → на набор
#        (это ровно _MINUS_SHARED_SET_CHAR_BUDGET выше — тот же лимит, переиспользуем его)
#   :12/:41 «К одной группе можно привязать до трех наборов» / «выберите до трех наборов» → на кампанию
#   docs/keywords/negative-keywords.md — «Количество слов для одной минус-фразы — не более 7».
_MINUS_LIB_MAX_SETS_ACCOUNT = 30
_MINUS_LIB_MAX_SETS_PER_CAMPAIGN = 3

# Кэш результата ensure_named_minus_sets на (login, slepok, site_type): набор создаётся ОДИН раз
# на прогон. Без него каждая кампания делала бы свой get+add, а два токен-потока (DIRECT_TOKEN_THREADS=2)
# могли бы одновременно не увидеть чужой набор и создать ДУБЛЬ с тем же именем.
_NAMED_SETS_CACHE: dict[tuple, dict] = {}
_NAMED_SETS_LOCK = threading.Lock()
_NAMED_SETS_CACHE_MAX = 200          # ключ пер-джобный → таблицу надо ограничивать, иначе течёт


def _read_slepok_minus_sets(slepok: str, site_type: str) -> list[dict]:
    """Прочитать ИМЕНОВАННЫЕ наборы минус-фраз слепка из локального зеркала пака.

    Путь — зеркало slepki_editor._pack_rel_minus_sets: {PACK_ROOT}/{site_type}/_minus_sets/{slug}.json.
    Формат файла: {"slug","site_type","sets":[{"name","phrases":[...]}]} ИЛИ голый список наборов.
    Файла нет / битый JSON → [] (не падаем: слепок без наборов — легитимный случай).
    """
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    site_type = (site_type or "").strip()
    if not key or not site_type:
        return []
    path = os.path.join(kp.PACK_ROOT, site_type, "_minus_sets", f"{key}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    raw = data.get("sets") if isinstance(data, dict) else (data if isinstance(data, list) else None)
    out: list[dict] = []
    for it in (raw or []):
        if not isinstance(it, dict):
            continue
        name = re.sub(r"\s+", " ", str(it.get("name") or "").strip())
        phrases = [str(p) for p in (it.get("phrases") or []) if str(p).strip()]
        if name or phrases:
            out.append({"name": name, "phrases": phrases})
    return out


def _clean_minus_set_phrases(phrases: list, city: str = "", region: str = "") -> tuple[list[str], int]:
    """Нормализовать фразы набора: trim, дедуп caseless, ≤7 слов, гео-чистка своего города.

    Возвращает (фразы, сколько отброшено по лимиту «7 слов»). Гео-чистка обязательна:
    собственный город аккаунта в минусах заминусовал бы свой же трафик (_strip_account_geo).
    """
    out: list[str] = []
    seen: set[str] = set()
    too_long = 0
    for p in phrases or []:
        w = re.sub(r"\s+", " ", str(p).strip())
        if not w:
            continue
        if len(w.split()) > 7:
            too_long += 1
            continue
        k = w.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(w)
    return _strip_account_geo(out, city, region), too_long


def _set_chars(words) -> int:
    """Символы набора БЕЗ пробелов — ровно так лимит 4096 считает Директ."""
    return sum(len(str(w).replace(" ", "")) for w in (words or []))


def _norm_minus_phrase(w) -> str:
    """Единый нормализатор минус-фразы для СВЕРКИ состава: trim + схлопнуть пробелы + lower.

    Обязан применяться к ОБЕИМ сторонам сравнения. Раньше сторона «кабинет» нормализовалась,
    а сторона «слепок» — нет, поэтому фраза с двойным пробелом всегда числилась отсутствующей.
    """
    return re.sub(r"\s+", " ", str(w or "").strip()).lower()


# Лимит длины Name у `negativekeywordsharedsets` в справке Директа НЕ подтверждён: в
# negative-keywords-library.md документирован только лимит 4096 символов на СОСТАВ набора.
# Поэтому берём заведомо консервативные 100 символов — новая схема имён
# («kuderko · С пробегом — минуса 1/3») укладывается в ~35 символов, запас кратный.
_MINUS_SET_NAME_MAX = 100


def _pack_minus_set_name(prefix: str, part_no: int, parts_total: int) -> str:
    """Имя склеенного набора: «{слепок} · {тип сайта} — минуса N/M».

    Имя НЕ зависит от СОСТАВА наборов слепка — ни от имён исходных наборов, ни от того, как
    фразы легли по корзинам. Это обязательное свойство: прежнее имя было конкатенацией имён
    исходных наборов + «(часть N/M)», поэтому ЛЮБАЯ правка набора в слепке двигала границу
    корзины и меняла имя. Реюз по имени промахивался, старый набор навсегда оставался сиротой
    в библиотеке аккаунта (лимит 30 наборов на аккаунт, автоуборки нет).

    M фиксировано (`_MINUS_LIB_MAX_SETS_PER_CAMPAIGN`), а не «сколько корзин реально вышло»:
    иначе имя снова поехало бы при переходе 3 корзины → 2. Заодно снимается прежняя путаница
    нумерации, где корзина с ХВОСТОМ набора могла называться «часть 1/2».
    """
    name = (f"{prefix} — минуса {part_no}/{parts_total}" if prefix
            else f"Минуса слепка {part_no}/{parts_total}")
    return name[:_MINUS_SET_NAME_MAX]


def _pack_minus_sets(sets: list[dict], budget: int, max_sets: int,
                     *, name_prefix: str = "") -> tuple[list[dict], list[str]]:
    """Склеить именованные наборы слепка в ≤``max_sets`` наборов по ``budget`` симв. без пробелов.

    Зачем: Директ разрешает привязать к одной кампании не более ТРЁХ наборов
    (docs/keywords/negative-keywords-library.md:41). Решение Семёна 2026-07-28 — не отбрасывать
    лишние наборы (у kuderko/«С пробегом» 4-й набор «Марки и модели авто» = 685 фраз = 41 % объёма),
    а СКЛЕИВАТЬ фразы в три набора по бюджету 4096.

    Детерминированность (обязательна, иначе реюз по имени сломается):
      • порядок наборов и фраз — как в файле структуры, никакой сортировки и множеств;
      • глобальный дедуп по caseless-ключу, побеждает ПЕРВОЕ вхождение;
      • раскладка — first-fit: фраза кладётся в ПЕРВЫЙ уже открытый набор, куда влезает,
        иначе открывается следующий (пока их < max_sets).
    Один и тот же вход даёт один и тот же выход. ИМЕНА при этом от входа не зависят вовсе
    (`_pack_minus_set_name`: позиция корзины + фиксированное M) — реюз по имени стабилен
    по построению, даже если состав наборов в слепке правили.

    Возвращает (наборы, непоместившиеся фразы). НЕ обрезает молча: всё, что не влезло
    в max_sets×budget, отдаётся вызывающему списком — тот обязан сообщить ГРОМКО.
    """
    bins: list[dict] = []
    leftover: list[str] = []
    seen: set[str] = set()
    for s in sets or []:
        src = str(s.get("name") or "")
        for p in (s.get("phrases") or []):
            w = str(p)
            k = w.lower()
            if k in seen:
                continue
            seen.add(k)
            cost = len(w.replace(" ", ""))
            placed = False
            for b in bins:
                if b["chars"] + cost <= budget:
                    b["phrases"].append(w)
                    b["chars"] += cost
                    if src and src not in b["src"]:
                        b["src"].append(src)
                    placed = True
                    break
            if placed:
                continue
            if len(bins) < max_sets and cost <= budget:
                bins.append({"src": ([src] if src else []), "phrases": [w], "chars": cost})
            else:
                leftover.append(w)   # не влезло ни в один набор — вернём вызывающему
    # Имена — по ПОЗИЦИИ корзины и фиксированному max_sets. Уникальны по построению, поэтому
    # прежняя страховка от коллизии имён после обрезки больше не нужна. Исходные имена наборов
    # слепка остаются в `src` (для отчёта/лога), но в ИМЯ набора не попадают — см. _pack_minus_set_name.
    packed: list[dict] = []
    for i, b in enumerate(bins):
        packed.append({"name": _pack_minus_set_name(name_prefix, i + 1, max_sets),
                       "phrases": b["phrases"], "src": list(b["src"])})
    return packed, leftover


def _minus_set_live_phrases(entry: dict) -> list[str]:
    """Фразы набора из ответа v5 `negativekeywordsharedsets.get` (список или {"Items": [...]})."""
    nk = (entry or {}).get("NegativeKeywords")
    if isinstance(nk, dict):
        nk = nk.get("Items")
    return [str(x) for x in (nk or [])]


def ensure_named_minus_sets(token: str, login: str, slepok: str, site_type: str,
                            city: str = "", region: str = "") -> dict:
    """Создать/переиспользовать в БИБЛИОТЕКЕ минус-фраз аккаунта именованные наборы слепка.

    ИДЕМПОТЕНТНО: сначала `negativekeywordsharedsets.get`; набор с ТЕМ ЖЕ именем (caseless)
    переиспользуется, повторный прогон дублей не плодит. Создаётся только недостающее, через
    `negativekeywordsharedsets.add`, с сохранением имени.

    СВЕРКА СОДЕРЖИМОГО при реюзе: `get` читает и `NegativeKeywords`, состав набора в кабинете
    сравнивается с составом из слепка. Расхождение = в кабинете НЕТ каких-то фраз слепка
    (набор-надмножество, куда директолог дописал своё, — законное состояние, НЕ ошибка) →
    ГРОМКОЕ предупреждение в errors, каких фраз не хватает. Содержимое чужого набора НЕ
    переписываем намеренно: набор — общий объект АККАУНТА, он может висеть и на кампаниях
    директолога, которых мы не создавали; тихий `update` порезал бы их показы. Это та же
    политика, что у матчинга ТОЛЬКО по точному имени (см. PACK_MINUS_PER_GROUP_LOST).

    СКЛЕЙКА (решение Семёна 2026-07-28): наборов в слепке больше трёх (лимит Директа на
    кампанию, negative-keywords-library.md:41) → фразы детерминированно укладываются в ТРИ
    набора по 4096 симв. без пробелов (`_pack_minus_sets`), чтобы ни одна фраза не потерялась.
    Не влезло даже в 3×4096 → видимая ошибка со списком, без тихой обрезки. Имя склеенного
    набора — «{слепок} · {тип сайта} — минуса N/3», от СОСТАВА не зависит: правка набора в
    слепке переиспользует тот же набор кабинета, а не плодит сирот под новым именем.

    Возврат: {"ok", "ids", "created", "reused", "errors", "sets_in_structure", "skipped",
              "packed_from", "packed_into", "mismatch", "not_packed"}.
    """
    res: dict = {"ok": True, "ids": [], "created": [], "reused": [], "errors": [],
                 "sets_in_structure": 0, "skipped": ""}
    sets = _read_slepok_minus_sets(slepok, site_type)
    res["sets_in_structure"] = len(sets)
    if not sets:
        res["skipped"] = "в структуре слепка нет именованных наборов минус-фраз"
        return res
    if not token:
        res["ok"] = False
        res["errors"].append("нет токена v5 — библиотечные наборы минус-фраз НЕ созданы")
        return res
    # NegativeKeywords читаем СРАЗУ: без состава набора реюз по имени не может заметить, что
    # набор в кабинете разошёлся со слепком (правки в редакторе слепков не доезжали молча).
    jm = _v5_get("negativekeywordsharedsets", token, login, ["Id", "Name", "NegativeKeywords"])
    if not isinstance(jm, dict) or "error" in jm:
        res["ok"] = False
        res["errors"].append("negativekeywordsharedsets.get: "
                             + (_v5_err(jm) if isinstance(jm, dict) else "нет ответа"))
        return res
    _rows = ((jm.get("result") or {}).get("NegativeKeywordSharedSets") or [])
    existing = [(s.get("Id"), s.get("Name") or "") for s in _rows]
    by_name: dict[str, int] = {}
    live_phrases: dict[int, list[str]] = {}
    for row in _rows:
        sid = row.get("Id")
        k = re.sub(r"\s+", " ", str(row.get("Name") or "").strip()).lower()
        if not sid:
            continue
        live_phrases[int(sid)] = _minus_set_live_phrases(row)
        if k and k not in by_name:
            by_name[k] = int(sid)
    free_slots = _MINUS_LIB_MAX_SETS_ACCOUNT - len(existing)

    # ── Нормализация + гео-чистка ДО реюза/склейки: и сверка содержимого, и укладка обязаны
    # сравнивать ровно то, что реально уехало бы в кабинет.
    cleaned: list[dict] = []
    for s in sets:
        name = s.get("name") or ""
        if not name:
            res["ok"] = False
            res["errors"].append("набор без имени в структуре слепка — пропущен")
            continue
        words, too_long = _clean_minus_set_phrases(s.get("phrases"), city, region)
        if too_long:
            res["errors"].append(f"«{name}»: {too_long} фраз(ы) длиннее 7 слов — не вошли в набор")
        if not words:
            res["ok"] = False
            res["errors"].append(f"«{name}»: после нормализации и гео-чистки не осталось фраз — набор НЕ создан")
            continue
        cleaned.append({"name": name, "phrases": words})
    if not cleaned:
        return res

    # ── Склейка в ≤3 набора, если наборов больше лимита Директа на кампанию ──────────────────
    if len(cleaned) > _MINUS_LIB_MAX_SETS_PER_CAMPAIGN:
        _total_chars = _set_chars([p for c in cleaned for p in c["phrases"]])
        _cap = _MINUS_LIB_MAX_SETS_PER_CAMPAIGN * _MINUS_SHARED_SET_CHAR_BUDGET
        # Префикс имени — только (слепок, тип сайта): от СОСТАВА наборов имя зависеть не должно,
        # иначе правка набора в слепке рождает новый набор в кабинете, а старый остаётся сиротой.
        _name_prefix = f"{_SLEPOK_KEY.get((slepok or '').lower(), (slepok or '').lower())} · {site_type}".strip()
        packed, leftover = _pack_minus_sets(cleaned, _MINUS_SHARED_SET_CHAR_BUDGET,
                                            _MINUS_LIB_MAX_SETS_PER_CAMPAIGN,
                                            name_prefix=_name_prefix)
        res["packed_from"] = len(cleaned)
        res["packed_into"] = len(packed)
        if leftover:
            res["ok"] = False
            res["not_packed"] = leftover
            _sample = "; ".join(leftover[:20]) + (" …" if len(leftover) > 20 else "")
            _msg = (f"НЕ ВЛЕЗЛО в {_MINUS_LIB_MAX_SETS_PER_CAMPAIGN} набора × "
                    f"{_MINUS_SHARED_SET_CHAR_BUDGET} симв.: {len(leftover)} фраз "
                    f"(всего {_total_chars} симв. без пробелов при потолке {_cap}) — {_sample}")
            res["errors"].append(_msg)
            print(f"[minus-sets] {login}/{slepok}/{site_type}: {_msg}", flush=True)
        cleaned = packed

    for s in cleaned:
        name = s.get("name") or ""
        words = list(s.get("phrases") or [])
        hit = by_name.get(name.lower())
        if hit:
            if hit not in res["ids"]:
                res["ids"].append(hit)
            res["reused"].append(name)
            # Реюз по имени БЕЗ сверки состава был тихой потерей правок слепка: набор
            # переиспользовался как есть, новые фразы не доезжали и в отчёте не всплывали.
            # Обе стороны — через ОДИН нормализатор (_norm_minus_phrase). Расхождение =
            # ТОЛЬКО непустой `_missing`: набор-надмножество (директолог дописал фраз руками)
            # — законное состояние кабинета, а не ошибка. Прежнее `len(_live) != len(_want)`
            # давало вечное «не хватает 0» на любом надмножестве.
            _live = {_norm_minus_phrase(w) for w in live_phrases.get(hit, []) if str(w).strip()}
            _missing = [w for w in words if _norm_minus_phrase(w) not in _live]
            if _missing:
                _sample = "; ".join(_missing[:10]) + (" …" if len(_missing) > 10 else "")
                _msg = (f"«{name}»: набор в кабинете ОТЛИЧАЕТСЯ от слепка — в кабинете "
                        f"{len(_live)} фраз, в слепке {len(words)}, не хватает {len(_missing)}; "
                        f"содержимое НЕ обновлено (набор — общий объект аккаунта, перезапись "
                        f"порезала бы чужие кампании) — обновите набор вручную"
                        + (f": {_sample}" if _sample else ""))
                res.setdefault("mismatch", []).append(name)
                res["errors"].append(_msg)
                print(f"[minus-sets] {login}: {_msg}", flush=True)
            continue
        chars = _set_chars(words)
        if chars > _MINUS_SHARED_SET_CHAR_BUDGET:
            res["ok"] = False
            res["errors"].append(
                f"«{name}»: {chars} симв. без пробелов при лимите Директа "
                f"{_MINUS_SHARED_SET_CHAR_BUDGET} ({len(words)} фраз) — набор НЕ создан "
                f"(тихая обрезка запрещена, набор надо разделить в структуре слепка)")
            continue
        if free_slots <= 0:
            res["ok"] = False
            res["errors"].append(
                f"«{name}»: в библиотеке аккаунта уже {len(existing)} наборов при лимите "
                f"{_MINUS_LIB_MAX_SETS_ACCOUNT} — набор НЕ создан")
            continue
        j_add = _v5_call("negativekeywordsharedsets", "add", token, login, {
            "NegativeKeywordSharedSets": [{"Name": name[:255], "NegativeKeywords": words}]
        })
        add_res = ((j_add.get("result") or {}).get("AddResults") or []) if isinstance(j_add, dict) else []
        errs = (add_res[0].get("Errors") or []) if add_res else []
        try:
            new_id = int((add_res[0].get("Id") or 0) if add_res and not errs else 0)
        except (TypeError, ValueError):
            new_id = 0
        if new_id > 0:
            res["ids"].append(new_id)
            res["created"].append(name)
            by_name[name.lower()] = new_id
            free_slots -= 1
        else:
            res["ok"] = False
            msg = ("; ".join(str(e.get("Message") or e.get("Details") or e) for e in errs)
                   or (_v5_err(j_add) if isinstance(j_add, dict) and "error" in j_add else "пустой ответ add"))
            res["errors"].append(f"«{name}»: negativekeywordsharedsets.add — {str(msg)[:160]}")
    return res


def ensure_named_minus_sets_cached(token: str, login: str, slepok: str, site_type: str,
                                   city: str = "", region: str = "", job_id: str = "") -> dict:
    """ensure_named_minus_sets один раз на (job_id, login, slepok, site_type).

    Лок обязателен: два токен-потока набора (DIRECT_TOKEN_THREADS=2) иначе могли бы одновременно
    получить пустой `negativekeywordsharedsets.get` и создать ДВА набора с одним именем.

    ⚠️ Кэш ПЕР-ДЖОБНЫЙ, а не процессный (ревью 2026-07-28). Кэшируем и неуспех тоже — повторять
    падающий v5-вызов на каждую кампанию бессмысленно, — но без `job_id` в ключе этот неуспех жил
    до рестарта: один транзиентный сбой Direct отравлял ВСЕ последующие прогоны того же аккаунта,
    возвращая им ошибку без единого обращения к API. Симметрично и с успехом: директолог мог
    поменять набор в кабинете, а процесс продолжал отдавать старый снимок.
    Пустой `job_id` (внешние вызовы) → прежнее поведение, кэш на процесс.
    """
    ckey = (str(job_id or ""),
            (login or "").lower(),
            _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower()),
            (site_type or "").strip())
    with _NAMED_SETS_LOCK:
        hit = _NAMED_SETS_CACHE.get(ckey)
        if hit is not None:
            return hit
        res = ensure_named_minus_sets(token, login, slepok, site_type, city=city, region=region)
        _NAMED_SETS_CACHE[ckey] = res
        if len(_NAMED_SETS_CACHE) > _NAMED_SETS_CACHE_MAX:      # ключей стало больше — чистим старые
            for _old in list(_NAMED_SETS_CACHE)[:len(_NAMED_SETS_CACHE) - _NAMED_SETS_CACHE_MAX]:
                _NAMED_SETS_CACHE.pop(_old, None)
        return res


def _collect_pack_minus(slepok: str, site_type: str, tp_code: str) -> list[str]:
    """Собрать ПОЛНЫЙ список минус-фраз из пака M3 для (slepok, site_type, tp_code).

    Обходит все ct-папки пака по данному tp, объединяет {slepok}_minus.txt +
    {slepok}_minus_shared.txt, дедуплицирует (case-insensitive), фильтрует ≤7 слов.
    Возвращает список (не обрезанный по символам).

    PACK_MINUS_PER_GROUP_LOST: у ct с per-adgroup-раскладкой (kontent_pack второй проход)
    минуса лежат в ct["_groups"][gk]["minus"], а top-level ct["minus"] заполняется только из
    {slepok}_minus_shared.txt. Нет _minus_shared (pavlov) → tp5 давал 0 фраз.

    Per-group минуса поднимаем на кампанию ТОЛЬКО ПЕРЕСЕЧЕНИЕМ, а НЕ объединением, и считаем
    его по ВСЕМ носителям минусов данного tp: каждой группе (ct с per-adgroup-раскладкой) И
    каждому легаси-ct без групп (его top-level minus). Фраза, лежащая у КАЖДОГО носителя, и так
    режет каждую группу → её подъём на кампанию эквивалентен и новых блокировок не вносит.
    Фраза одного носителя обычно дискриминатор (чужая модель/марка) и на кампании выбила бы
    СВОИ ключи соседей. Замер на паке (позитивы, срезаемые собственными ключами):
      • объединение: kryuchkova/Мультибренд/tp5 блокировало 56 938 своих ключей из 60 435;
      • пересечение только по группам: легаси-ct kryuchkova/Монобренд/tp5 терял 7 508 из 18 290;
      • пересечение по группам И легаси-ct (этот код): прирост блокировок 0 везде,
        при этом pavlov/tp5 = 558 фраз (цель фикса, у него все 9 per-group файлов идентичны).
    """
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    pack = kp.gather(key, site_type, tp_code)  # {ctNNNN: {"minus":[...], "_groups": {...}}}
    _carriers: list[list[str]] = []
    for _ct in pack.values():
        _cg = (_ct.get("_groups") or {})
        if _cg:
            _carriers += [list(_g.get("minus") or []) for _g in _cg.values()]
        else:                                  # легаси-ct без групп — тоже носитель
            _carriers.append(list(_ct.get("minus") or []))
    _common: set[str] = set()
    if _carriers:
        _common = set.intersection(*({str(w).strip().lower() for w in _c if str(w).strip()}
                                     for _c in _carriers))
    seen: set[str] = set()
    result: list[str] = []
    for ct_data in pack.values():
        # top-level ct["minus"] — как раньше, объединением (легаси-поведение, не трогаем);
        # per-group — только общие для всех групп фразы.
        _ct_words = list(ct_data.get("minus") or [])
        for _grp in (ct_data.get("_groups") or {}).values():
            _ct_words += [w for w in (_grp.get("minus") or [])
                          if str(w).strip().lower() in _common]
        for w in _ct_words:
            w = re.sub(r"\s+", " ", str(w).strip())
            if not w or len(w.split()) > 7:
                continue
            k = w.lower()
            if k not in seen:
                seen.add(k)
                result.append(w)
    return result


def _minus_char_budget(words: list[str], budget: int = _MINUS_CAMPAIGN_CHAR_BUDGET) -> list[str]:
    """Обрезать список минус-фраз по символьному бюджету (БЕЗ пробелов).

    Директ считает символы каждой фразы без пробелов (официальная дока).
    Добавляем фразы пока сумма не превысит бюджет.
    """
    total, out = 0, []
    for w in words:
        cost = len(w.replace(" ", ""))
        if total + cost > budget:
            break
        total += cost
        out.append(w)
    return out


def _get_or_create_minus_set(token: str, login: str,
                              slepok: str, site_type: str, tp_code: str,
                              city: str = "", region: str = "") -> int | None:
    """Вернуть id shared минус-набора для tp2/tp4 (зеркалит путь tp1/tp5).

    1. Берём существующий НАШ набор по маркеру имени «Минуса общие» — как _tp5_account_data.
       Если есть — возвращаем сразу, без чтения пака. Слепого фолбэка на первый попавшийся
       набор аккаунта (msets[0][0]) НЕТ: на кабинете директолога с собственными наборами
       минус-слов он прицепил бы к нашей кампании ЧУЖОЙ набор и порезал показы.
    2. Если НАШЕГО набора нет — собираем минусы из пака M3
       (все ct данного tp, объединить+дедуп), обрезаем по 20 000 симв. без пробелов,
       создаём новый набор через v5 negativekeywordsharedsets.add.
    3. None при любой ошибке (не валит создание кампании).
    """
    try:
        jm = _v5_get("negativekeywordsharedsets", token, login, ["Id", "Name"])
        msets = [(s["Id"], s.get("Name") or "")
                 for s in (jm.get("result") or {}).get("NegativeKeywordSharedSets", [])]
        # Путь tp1/tp5: ТОЛЬКО набор с нашим маркером в имени. Чужие наборы аккаунта не берём.
        minus_set = next((mid for mid, nm in msets if _MINUS_SET_NAME_MARKER in nm), None)
        if minus_set:
            return minus_set
        # Нашего набора в аккаунте нет: собираем НОВЫЙ library-набор из глоб. вкладки «Минус-слова»
        # + библиотечный слепок {slepok}_minus_shared (+ per-ct _minus) из пака M3.
        # (#9 SLEPOK_MINUS_MISSING_ONLY_GLOBAL: слепковый минус должен попасть в library-набор,
        #  а не только глобальные слова — иначе снапшот слепка не долетает до кампании.)
        words = list(_enabled_minus_words() or [])
        try:
            _pack_minus = _collect_pack_minus(slepok, site_type, tp_code)
        except Exception:  # noqa: BLE001 — пак M3 недоступен → деградируем к глоб. словам
            _pack_minus = []
        _seen = {w.lower() for w in words}
        for _w in _pack_minus:
            if _w.lower() not in _seen:
                _seen.add(_w.lower())
                words.append(_w)
        # GEO-GUARD: убрать собственный город/регион аккаунта — иначе заминусует свой трафик
        words = _strip_account_geo(words, city, region)
        words = _minus_char_budget(words, _MINUS_SHARED_SET_CHAR_BUDGET)  # набор ≤4096, не 20000
        if not words:
            return None
        j_add = _v5_call("negativekeywordsharedsets", "add", token, login, {
            "NegativeKeywordSharedSets": [{
                "Name": f"{_MINUS_SET_NAME_MARKER} {tp_code}",
                "NegativeKeywords": words,
            }]
        })
        add_res = (j_add.get("result") or {}).get("AddResults", [])
        new_id = (add_res[0].get("Id") if add_res else None)
        return new_id or None
    except Exception:  # noqa: BLE001 — мягкая деградация, не валим кампанию
        return None


def _attach_minus_set_to_text_campaign(token: str, login: str,
                                        campaign_id: int, minus_set_id: int) -> str | None:
    """Привязать shared минус-набор к v5 TEXT_CAMPAIGN через campaigns.update.

    NegativeKeywordSharedSetIds — поле верхнего уровня кампании (не внутри TextCampaign).
    Возвращает None при успехе, текст ошибки при неудаче.
    """
    try:
        j = _v5_call("campaigns", "update", token, login, {
            "Campaigns": [{
                "Id": campaign_id,
                "NegativeKeywordSharedSetIds": {"Items": [int(minus_set_id)]},
            }]
        })
        upd_res = (j.get("result") or {}).get("UpdateResults", [])
        errs = (upd_res[0].get("Errors") or []) if upd_res else []
        if errs:
            return "; ".join(e.get("Message") or e.get("Details") or str(e) for e in errs)
        if "error" in j:
            return _v5_err(j)
        return None
    except Exception as e:  # noqa: BLE001
        return str(e)[:120]


def _apply_campaign_direct_minus(token: str, login: str,
                                  campaign_id: int,
                                  slepok: str, site_type: str, tp_code: str,
                                  city: str = "", region: str = "") -> str | None:
    """Повесить минусы campaign-direct (pavlov/kryuchkova) напрямую на кампанию.

    Механизм: campaigns.update с NegativeKeywords: {"Items": [...]}.
    Лимит: ≤20 000 символов без пробелов (NegativeKeywords кампании, офиц. дока).
    Мягкая деградация: при ошибке возвращает текст ошибки, кампанию НЕ откатывает.
    Возвращает None при успехе, строку ошибки при неудаче.
    """
    try:
        # Источники минус-фраз КАМПАНИИ: глоб. вкладка «Минус-слова» + библиотечный слепок
        # {slepok}_minus_shared (+ per-ct _minus) из пака M3 (#9 SLEPOK_MINUS_MISSING_ONLY_GLOBAL —
        # раньше слепковый минус до кампании не долетал, ставились только глобальные слова).
        # Исключения:
        #  • group-режим (terehov/karavaev): паковые минусы уже висят на ГРУППАХ
        #    (_build_tp2_adgroups g["minus"]) → на кампанию НЕ дублируем (экономим бюджет 20k);
        #  • tp1 (РСЯ): минус-слова режут охват сети без пользы (там же намеренно снят групповой
        #    минус _build_tp1_adgroups) → на РСЯ оставляем ТОЛЬКО глобальные слова.
        words = list(_enabled_minus_words() or [])
        if tp_code != "tp1" and _SLEPOK_MINUS_MODE.get(slepok, "group") != "group":
            try:
                _pack_minus = _collect_pack_minus(slepok, site_type, tp_code)
            except Exception:  # noqa: BLE001 — пак M3 недоступен → деградируем к глоб. словам
                _pack_minus = []
            _seen = {w.lower() for w in words}
            for _w in _pack_minus:
                if _w.lower() not in _seen:
                    _seen.add(_w.lower())
                    words.append(_w)
        # GEO-GUARD: убрать собственный город/регион аккаунта из всех источников минусов
        words = _strip_account_geo(words, city, region)
        words = _minus_char_budget(words, _MINUS_CAMPAIGN_CHAR_BUDGET)  # ≤20 000 симв.
        if not words:
            return "нет минус-слов (ни вкладка «Минус-слова», ни пак слепка) — campaign-direct пропущен"
        j = _v5_call("campaigns", "update", token, login, {
            "Campaigns": [{
                "Id": campaign_id,
                "NegativeKeywords": {"Items": words},
            }]
        })
        upd_res = (j.get("result") or {}).get("UpdateResults", [])
        errs = (upd_res[0].get("Errors") or []) if upd_res else []
        if errs:
            return "; ".join(e.get("Message") or e.get("Details") or str(e) for e in errs)
        if "error" in j:
            return _v5_err(j)
        return None  # успех
    except Exception as e:  # noqa: BLE001
        return str(e)[:120]

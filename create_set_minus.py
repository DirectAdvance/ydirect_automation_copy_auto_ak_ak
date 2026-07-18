"""Create-set minus-keyword helpers extracted from blueprint.py."""

from __future__ import annotations

import re

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
    """Извлечь adj-стем из строки области (напр. «Волгоградская область» → «волгоградск»).

    Обрабатывает только первое слово строки (прилагательное), снимает адъективное
    окончание и восстанавливает стем на «ск».  Возвращает [] если строка пустая
    или первое слово не распознаётся как прилагательное.
    """
    if not region:
        return []
    first = region.strip().lower().split()[0] if region.strip() else ""
    if not first:
        return []
    for sfx in ("ская", "ский", "ское", "ские", "ских", "ского", "ской", "ском", "скому", "скими"):
        if first.endswith(sfx) and len(first) > len(sfx):
            root = first[: -len(sfx)]
            stem = root + "ск"
            return [stem] if len(stem) >= 5 else []
    return []


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
}


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

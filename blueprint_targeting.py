"""Классификатор ct → сегмент (Марки/Модели/Общее) + профиль таргетинга слепков —
вынесено из blueprint.py БЕЗ изменения логики (только перемещение + ре-экспорт).

Кластер: `_gc_ct`, `_ct_is_model_map`, `_ct_segment_map`, `_ct_segment`, `_seg_canon`,
`_model_cts`, `_segment_donor`, `_targeting_profile`, `_slepok_tp_modes`,
`_slepok_profile_excludes_tp`, `_slepki_structure_for_ui`, `_donor_tp4_models_map`,
`_pack_for_item`.

DI (инъектятся из blueprint через configure): `_json` (загрузка json-файла пакета),
`_ag_part1_map` (ct → имя марки/модели, из campaign_naming), `_SLEPOK_KEY` (label→key),
`_struct_cts` (ct слепка из slepki_structure). Кэши классификатора переезжают сюда —
единый источник мемоизации. Инвариант wiring-hub: НЕ импортирует blueprint.
"""
from __future__ import annotations

import os
import re

from . import kontent_pack as kp  # чтение контент-пака (read_keywords/callouts/images/feeds)


# ── DI: инъектятся из blueprint (заглушки-callable падают громко, если configure не отработал) ──
def _json(*a, **k):
    raise RuntimeError("blueprint_targeting._json не инъектирован (configure)")


def _ag_part1_map(*a, **k):
    raise RuntimeError("blueprint_targeting._ag_part1_map не инъектирован (configure)")


def _struct_cts(*a, **k):
    raise RuntimeError("blueprint_targeting._struct_cts не инъектирован (configure)")


_SLEPOK_KEY: dict = {}


def configure(deps: dict) -> None:
    """Инъекция зависимостей из blueprint."""
    globals().update(deps)


def _gc_ct(gc: str) -> str:
    """Первый ctNNNN из кодера группы (gc) = ag_part1 = бренд/модель."""
    m = re.search(r"ct\d{4}", gc or "")
    return m.group(0) if m else ""


_CT_MODEL_CACHE: dict | None = None


def _ct_is_model_map() -> dict:
    """ct → True если МОДЕЛЬ (бренд+модель), False если МАРКА или ТЕМА.

    Модель = существует более короткое имя ag_part1, являющееся СЛОВЕСНЫМ префиксом
    данного («BAIC» → «BAIC X35», «Great Wall» → «Great Wall Poer»). Бренды («BAIC»)
    и темы («Авито», «Автокредит/кредит», «Седаны», «кластер запросов…») своего
    бренда-префикса не имеют → Марки. Источник — gsheet_naming(ag_part1), кэш на процесс.
    Это РОВНО та раскладка, что в боевых аккаунтах Щербаковой (РСЯ-Марки / РСЯ-Модели)."""
    global _CT_MODEL_CACHE
    if _CT_MODEL_CACHE is not None:
        return _CT_MODEL_CACHE
    low = {ct: (nm or "").strip().lower() for ct, nm in _ag_part1_map().items()}
    vals = set(low.values())
    out: dict = {}
    for ct, ln in low.items():
        toks = ln.split()
        out[ct] = any(" ".join(toks[:i]) in vals for i in range(1, len(toks)))
    _CT_MODEL_CACHE = out
    return out


_CT_SEG_CACHE: dict | None = None
def _ct_segment_map() -> dict:
    """ct → сегмент: 'Модели' | 'Марки' | 'Общее' (как в БОЕВЫХ аккаунтах: Поиск/РСЯ делятся на
    Марки / Модели / Общее). Робастная классификация по справочнику gsheet_naming(ag_part1):
      • БРЕНД (Марки)  = слово ведёт ≥2 модельных имён ИЛИ есть как одиночная категория и ведёт ≥1
        (ловит и бренды без отдельной ct-категории: «Jac», «Solaris»).
      • МОДЕЛЬ (Модели) = многословное имя, чьё ПЕРВОЕ слово — бренд («BAIC X35», «Jac J7»).
      • ТЕМА (Общее)   = не бренд и не модель («Авито», «Автосалон/салон/Дилер», «Авто/Автомобили»).
    Кэш на процесс."""
    global _CT_SEG_CACHE
    if _CT_SEG_CACHE is not None:
        return _CT_SEG_CACHE
    from collections import Counter
    low = {ct: (nm or "").strip().lower() for ct, nm in _ag_part1_map().items()}
    lead: Counter = Counter()
    single: set = set()
    for ln in low.values():
        parts = ln.split()
        if len(parts) >= 2:
            lead[parts[0]] += 1
        elif ln:
            single.add(ln)

    def _is_brand(tok: str) -> bool:
        return lead.get(tok, 0) >= 2 or (lead.get(tok, 0) >= 1 and tok in single)

    out: dict = {}
    for ct, ln in low.items():
        parts = ln.split()
        if len(parts) >= 2 and _is_brand(parts[0]):
            out[ct] = "Модели"
        elif ln and _is_brand(ln):
            out[ct] = "Марки"
        else:
            out[ct] = "Общее"
    _CT_SEG_CACHE = out
    return out


def _ct_segment(ct: str) -> str:
    """Сегмент группы по её ct/кодеру: 'Модели' | 'Марки' | 'Общее' (единый источник — _ct_segment_map)."""
    return _ct_segment_map().get(_gc_ct(ct), "Марки")


# ── Семейства правил создания (ruleset) ───────────────────────────────────────────────────────
# ЕДИНЫЙ источник «разных правил создания» для двух семейств слепков. Раньше эти же различия были
# размазаны булевым признаком `auto` по 4+ местам движка (сегментация, контент-голос, фильтр
# ключей, источник картинок). Теперь ветки читают ИМЕНОВАННЫЙ ruleset — новое семейство
# добавляется данными (флаг в структуре), а не правкой движка.
#
#   auto   — стандартные авто-директологи (автосалоны): сегменты Марки/Модели/Общее из
#            ct-справочника, авто-корпус контента с числами, фильтр «марка+модель», общий пул картинок.
#   custom — прочие/B2B-слепки, собранные по скринам кабинета (dmp и будущие): структура по splits
#            (реальные темы), B2B-голос без числового гейта, без фильтра ключей, только свои картинки.
_RULESETS: dict[str, dict] = {
    "auto": {
        "segmentation": "ct_spravochnik",   # tp1/2/4/5 — сегменты из ct-справочника
        "content_voice": "auto_numeric",    # заголовки/тексты авто-корпуса + generic-филлеры
        "require_number_in_ad": True,       # number-gate объявлений
        "generic_fillers": True,
        "key_filter": "drop_brand_model",   # text_gen режет «марка+модель»
        "images": "shared_pool",            # manual/M3/feed общий пул
    },
    "custom": {
        "segmentation": "splits_as_is",     # структура = реальные темы кабинета (splits)
        "content_voice": "custom_b2b",
        "require_number_in_ad": False,
        "generic_fillers": False,
        "key_filter": "none",
        "images": "own_only",               # только собственные картинки слепка
    },
}


def _ruleset_name_of(x: dict) -> str:
    """Семейство по ЗАПИСИ директолога: явное "ruleset" (если валидно) → иначе из флага "auto"."""
    rn = x.get("ruleset")
    if rn in _RULESETS:
        return rn
    return "auto" if x.get("auto", True) is not False else "custom"


def ruleset_name(slepok: str) -> str:
    """Имя семейства правил слепка: 'auto' | 'custom'.

    Источник — директолог в slepki_structure.json: явное поле "ruleset" (если валидно) имеет
    приоритет, иначе выводится из флага "auto" (auto:false → 'custom'). Пусто/сбой/неизвестный → 'auto'.
    """
    if not slepok:
        return "auto"
    try:
        for x in (_json("slepki_structure.json").get("directologists") or []):
            if x.get("key") == slepok:
                return _ruleset_name_of(x)
    except Exception:  # noqa: BLE001
        pass
    return "auto"


def ruleset_for(slepok: str) -> dict:
    """Именованный ruleset слепка (dict полей выше) — ЕДИНЫЙ источник правил создания auto vs custom."""
    return _RULESETS[ruleset_name(slepok)]


# ── Тонкие читатели ruleset (обратная совместимость call-sites движка) ─────────────────────────
# Признак семейства хранится в slepki_structure.json (флаг "auto" / опц. "ruleset"). Для custom
# сегментация Марки/Модели/Общее НЕ применяется — структура по splits, контент B2B-голосом.
def _slepok_is_auto(slepok: str) -> bool:
    """True для авто-директологов (семейство 'auto'), False для 'custom'. == ruleset_name=='auto'."""
    return ruleset_name(slepok) == "auto"


def _non_auto_slepki() -> list:
    """key всех custom-слепков — для UI (рендер splits вместо Марки/Модели) и контент-роутинга."""
    try:
        return [x.get("key") for x in (_json("slepki_structure.json").get("directologists") or [])
                if x.get("key") and _ruleset_name_of(x) == "custom"]
    except Exception:  # noqa: BLE001
        return []


def _non_auto_site_types() -> set:
    """Имена site_type всех custom-слепков — для text_gen: не применять авто-фильтр ключей
    (drop «марка+модель») к B2B-группам. Custom-слепки используют уникальные site_type (напр. dmp)."""
    out: set = set()
    try:
        for x in (_json("slepki_structure.json").get("directologists") or []):
            if _ruleset_name_of(x) == "custom":
                for st in (x.get("site_types") or []):
                    if st.get("name"):
                        out.add(st["name"])
    except Exception:  # noqa: BLE001
        pass
    return out


def _seg_canon(s: str) -> str:
    """Канон сегмента для сверки классификатора с профилем: общие темы → 'общая'
    (классификатор даёт «Общее», профиль из живых имён — «общая»/«Общие запросы»)."""
    s = (s or "").strip().lower()
    return "общая" if s.startswith("общ") else s


def _model_cts() -> list:
    """Список модельных ct (совместимость; новый единый источник — _ct_segment_map)."""
    return [ct for ct, seg in _ct_segment_map().items() if seg == "Модели"]


# Слепок-донор сегмента: если у целевого слепка НЕТ своих ct сегмента (напр. Терехов tp4 без
# «Моделей») — берём структуру и контент сегмента у донора («как в других слепках»). Щербакова —
# самый полный модельный слепок (tp4 = 138 модельных ct). Расширяемо при необходимости.
_SEGMENT_DONORS = {"Модели": ["scherbakova"]}


def _segment_donor(segment: str, tp_code: str, site_type: str, exclude: str = "") -> str | None:
    """Первый донор, у которого ЕСТЬ ct данного сегмента для (tp_code, site_type). Иначе None."""
    for donor in _SEGMENT_DONORS.get(segment, []):
        if donor == exclude:
            continue
        if any(_ct_segment(ct) == segment for ct in _struct_cts(donor, site_type, tp_code)):
            return donor
    return None


_TARGETING_PROFILE_CACHE: dict | None = None


def _targeting_profile() -> dict:
    """Профиль таргетинга слепков из боевых аккаунтов: {slepok:{site_type:{tp:{segment:{mode:cnt}}}}}.
    Источник — targeting_profile.json (сгенерён из raw_grid). Кэшируется."""
    global _TARGETING_PROFILE_CACHE
    if _TARGETING_PROFILE_CACHE is None:
        _TARGETING_PROFILE_CACHE = _json("targeting_profile.json") or {}
    return _TARGETING_PROFILE_CACHE


def _slepok_tp_modes(slepok: str, site_type: str, tp: str, segment: str) -> list | None:
    """Какие режимы таргетинга (КС/Автотаргет) реально ведёт слепок для (site_type, tp, segment).

    None  → нет данных (слепка нет в профиле ИЛИ этого tp нет у слепка) → дефолт (как раньше).
    []    → tp у слепка ЕСТЬ, но именно ЭТОГО сегмента нет → НЕ строить (гейт-вниз, «не лишнее»).
    [...] → строить ровно эти режимы (в порядке КС, Автотаргет).
    """
    skey = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    prof = _targeting_profile()
    if skey not in prof:
        return None
    tps = prof.get(skey, {}).get(site_type, {}) or {}
    if tp not in tps:                       # нет данных по этому tp у слепка → дефолт, не гейт
        return None
    # Сегмент сверяем КАНОНИЧЕСКИ: «Общее» (классификатор) ↔ «общая» (профиль из живых имён).
    seg_tps = tps.get(tp, {}) or {}
    sc = _seg_canon(segment)
    modes = next((v for k, v in seg_tps.items() if _seg_canon(k) == sc), {}) or {}
    return [m for m in ("КС", "Автотаргет") if m in modes]


def _slepok_profile_excludes_tp(slepok: str, site_type: str, tp: str) -> bool:
    """True, если у слепка ЕСТЬ боевой профиль для site_type, но данного tp в нём НЕТ.

    Смысл — «строгое соответствие набору слепка» (баг porg-psm5h7q6: просочился tp4).
    Профиль (targeting_profile.json) — слепок РЕАЛЬНЫХ боевых аккаунтов; если он есть, он
    АВТОРИТЕТЕН по составу типов. Структура (slepki_structure.json) может содержать tp для
    ДОНОРСКИХ целей (напр. scherbakova держит tp4 как донор «Моделей» для др. слепков,
    _SEGMENT_DONORS), но сам слепок его не ведёт → строить его в СВОЁМ аккаунте нельзя.
    Слепка/типа сайта нет в профиле → False (профиль не авторитетен, поведение как раньше —
    не ломаем слепки без профиля, напр. Терехов).
    """
    skey = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    st = _targeting_profile().get(skey, {}).get(site_type)
    if not st:
        return False
    return tp not in st


def _slepki_structure_for_ui() -> dict:
    """Копия slepki_structure для чекбоксов набора в UI с ФИЛЬТРОМ по боевому профилю:
    у слепка, у которого есть targeting_profile для site_type, скрываем tp, которых в профиле
    НЕТ (донорские tp — напр. scherbakova держит tp4 «Модели» как донор, но в СВОЙ аккаунт его
    не ведёт; gate _slepok_profile_excludes_tp его всё равно молча режет → нельзя предлагать в UI).
    Слепок без профиля (напр. Терехов) — не трогаем, tp остаются (он реально их создаёт).
    ВАЖНО: донорская логика (_donor_tp4_models_map / _segment_donor / _struct_cts) читает
    slepki_structure.json С ДИСКА напрямую — этот фильтр её НЕ затрагивает."""
    import copy
    out = copy.deepcopy(_json("slepki_structure.json"))
    for d in out.get("directologists", []):
        key = d.get("key") or ""
        source_manifest = d.get("source_manifest")
        source_campaigns_by_site: dict[str, list] = {}
        if source_manifest:
            try:
                manifest = _json(source_manifest)
                if manifest.get("slepok") == key:
                    source_campaigns_by_site[manifest.get("site_type") or ""] = copy.deepcopy(
                        manifest.get("campaigns") or [])
            except Exception:  # noqa: BLE001 — broken manifest is reported by preflight, UI still opens
                source_campaigns_by_site = {}
        for st in d.get("site_types", []):
            stype = st.get("name") or ""
            st["tp"] = [t for t in st.get("tp", [])
                        if not _slepok_profile_excludes_tp(key, stype, t.get("code") or "")]
            if source_campaigns_by_site.get(stype):
                st["source_campaigns"] = source_campaigns_by_site[stype]
    return out


# Маркер автотаргет-пака (та же семантика, что клиентский isAuto в slepki_ui.js:504 и
# _slCountKeywords): строка «---autotargeting» = псевдо-ключ автотаргетинга, НЕ реальный ключ.
_PACK_AUTO_RE = re.compile(r"-{2,}\s*autotargeting", re.I)
# tp, у которых бейдж таргетинга считается по ФАКТУ ключей пака (kwRecompute в slepki_ui.js).
# tp3/tp6/tp7 ДОБАВЛЕНЫ осознанно: клиентский _slCountKeywords обходил ВСЕ tp (в т.ч. МК-кампании
# tp6 и товарку tp7) и сам засеивал ими _SL_PACK_CACHE. Раз счётчик ключей и бейджи больше НЕ
# ходят по HTTP, а сидят этим предрасчётом, без них молча пропали бы и счётчик, и факт-бейдж.
# Список = ровно те tp, что встречаются в структуре, т.е. ровно то, что клиент считал раньше.
_PACK_FACT_TPS = ("tp1", "tp2", "tp3", "tp4", "tp5", "tp6", "tp7")


def _pack_read_local(path: str) -> tuple[list, bool]:
    """Быстрое ЛОКАЛЬНОЕ чтение пач-файла для BULK-предрасчёта → (строки, найден_ли).

    Осознанно НЕ через kp._read_lines: тот под NEURO_PACK_MOUNT шеллит ``timeout cat`` в
    subprocess НА КАЖДЫЙ файл (safety для настоящего sshfs-монта) → на ~13k файлов это ~36 с и
    держит ui_structure-запрос. В проде NEURO_PACK_MOUNT = ЛОКАЛЬНОЕ зеркало (ночной синк с M3,
    sync_content_m3.py), поэтому bulk читаем обычным open() (~1.7 с). Фильтр строк идентичен
    kp._read_lines (strip, без пустых, без ``#``-комментариев) → контент ровно тот же, что отдаёт
    live-эндпоинт /keywords (сверено: 0 расхождений бейджа на 400 парах). Лениво-пер-групповой
    путь /keywords НЕ трогаем — он остаётся на subprocess-cat (одна группа за запрос — дёшево)."""
    try:
        if not os.path.isfile(path):
            return [], False
        with open(path, encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")], True
    except Exception:  # noqa: BLE001 — битый/недоступный файл = пусто (как kp._read_lines)
        return [], False


def _pack_group_fact(slepok: str, site_type: str, tp: str, ct: str, gk: str) -> dict:
    """Факт ключей ОДНОЙ группы (пер-групповой пак ct~gk) → {real:int, auto:bool}.

    Разрешение файла — РОВНО как в slepki_editor.read_group_keywords (per-group
    ``{slepok}__{slug}.txt`` с фолбэком на легаси ``{slepok}.txt``). Считаем:
      real = число НЕ-маркерных непустых строк (реальные КС),
      auto = есть ли строка-маркер ``---autotargeting``.
    Это ровно то, что клиентский _slCountKeywords кладёт в _SL_PACK_CACHE, а
    _slRecomputeKwBadges по этому факту (UNION по группам) собирает метку кампании."""
    kd = os.path.join(kp._ct_dir(site_type, tp, ct), "keywords")
    slug = kp._group_slug(gk)
    if slug:
        pos, found = _pack_read_local(os.path.join(kd, f"{slepok}__{slug}.txt"))
        if not found:
            pos, _ = _pack_read_local(os.path.join(kd, f"{slepok}.txt"))
    else:
        pos, _ = _pack_read_local(os.path.join(kd, f"{slepok}.txt"))
    real = 0
    auto = False
    reals: list = []
    for line in pos:
        s = (line or "").strip()
        if not s:
            continue
        if _PACK_AUTO_RE.search(s):
            auto = True
        else:
            real += 1
            reals.append(s)
    # sig — подпись СОДЕРЖИМОГО пака (кол-во + первый/последний реальный ключ), 1:1 с клиентским
    # `_sig` в _slCountKeywords. Нужна для дедупа счётчика: группы без своего per-group файла
    # падают на ОДИН ct-агрегат и должны быть посчитаны один раз. Наружу (в pack_facts) НЕ уходит.
    sig = f"{real}:" + (f"{reals[0]}…{reals[-1]}" if reals else "")
    return {"real": real, "auto": auto, "sig": sig}


def _slepki_pack_facts(struct: dict) -> dict:
    """Пер-групповой факт ключей для бейджей таргетинга tp1/tp2/tp4/tp5 → предрасчёт на СЕРВЕРЕ.

    Ключ = ``{slepok}|{site_type}|{tp}|{ct}|{gk}`` (ровно ключ клиентского _SL_PACK_CACHE),
    значение = {real:int, auto:bool}. Клиент сидит этим словарём _SL_PACK_CACHE ДО первого
    рендера → _slRecomputeKwBadges синхронно ставит верную метку сразу (без «прыжка» с эвристики).

    Обход = та же структура, что уходит в UI (struct = _slepki_structure_for_ui()), только
    ветка обычных tp (source_campaigns/архив бейджей по ключам не считает). Пара (ct,gk) берётся
    из it.gc/it.gk item'а — ровно то, из чего клиент строит data-kwgrps (ct~gk). Каждую пару
    считаем ОДИН раз и эмитим ВСЕГДА (даже при пустом/отсутствующем файле → real:0,auto:false),
    иначе allLoaded не станет true и кампания осталась бы на fallback-эвристике.

    Возвращает ``{"facts": {...}, "kw_totals": {"{slepok}|{site}": N}}``. ``kw_totals`` — счётчик
    «≈N ключевых слов» карточки обзора, посчитанный ЗДЕСЬ вместо клиента: раньше UI ради одной
    цифры дёргал /direct/api/slepki/keywords ПО ЗАПРОСУ НА ГРУППУ (замер: 522 запроса / ~21 МБ
    на одно открытие страницы), при том что все нужные файлы этот предрасчёт и так читает.
    Дедуп — по подписи СОДЕРЖИМОГО пака в пределах (slepok,site,tp,ct), 1:1 с клиентским
    `_ctSeen`: группы, упавшие на общий ct-агрегат, считаются один раз."""
    facts: dict = {}
    totals: dict = {}
    seen_sigs: dict = {}
    for d in (struct.get("directologists") or []):
        slepok = d.get("key") or ""
        if not slepok:
            continue
        for st in (d.get("site_types") or []):
            site = st.get("name") or ""
            for t in (st.get("tp") or []):
                code = t.get("code") or ""
                if code not in _PACK_FACT_TPS:
                    continue
                blocks = t.get("splits") or [{"groups": t.get("groups") or []}]
                for sp in blocks:
                    for g in (sp.get("groups") or []):
                        for it in (g.get("items") or []):
                            if not isinstance(it, dict):
                                continue
                            ct = _gc_ct(it.get("gc") or "")
                            if not ct:
                                continue
                            gk = it.get("gk") or ""
                            key = f"{slepok}|{site}|{code}|{ct}|{gk}"
                            if key in facts:
                                continue
                            fact = _pack_group_fact(slepok, site, code, ct, gk)
                            facts[key] = {"real": fact["real"], "auto": fact["auto"]}
                            # счётчик обзора: суммируем только НОВУЮ подпись пака внутри (tp,ct)
                            seen = seen_sigs.setdefault(f"{slepok}|{site}|{code}|{ct}", set())
                            if fact["sig"] not in seen:
                                seen.add(fact["sig"])
                                tkey = f"{slepok}|{site}"
                                totals[tkey] = totals.get(tkey, 0) + fact["real"]
    return {"facts": facts, "kw_totals": totals}


def _donor_tp4_models_map() -> dict:
    """{slepok_key: [site_type,...]} — где у слепка НЕТ своих tp4-«Моделей», но донор их покрывает.
    UI по этой карте показывает донорский чекбокс «Модели» для tp4 (напр. Терехов)."""
    out: dict = {}
    for d in _json("slepki_structure.json").get("directologists", []):
        key = d.get("key")
        if not key:
            continue
        for st in d.get("site_types", []):
            stype = st.get("name")
            if not any(t.get("code") == "tp4" for t in st.get("tp", [])):
                continue
            own_models = any(_ct_segment(ct) == "Модели" for ct in _struct_cts(key, stype, "tp4"))
            if not own_models and _segment_donor("Модели", "tp4", stype, exclude=key):
                out.setdefault(key, []).append(stype)
    return out


def _pack_for_item(slepok: str, site_type: str, tp: str, gc: str) -> dict:
    """Контент пака для одной группы набора (по нашему ct из gc).

    → {ct, model, keywords, minus, callouts, images, from}.
    ct0000/пусто → from='fallback' (берём корпус слепка вне пака)."""
    ct = _gc_ct(gc)
    kw = kp.read_keywords(site_type, tp, ct, slepok)
    co = kp.read_callouts(site_type, tp, ct, slepok)
    im = kp.read_images(site_type, tp, ct)
    has = bool(kw["positive"] or kw["minus"] or co or im)
    return {"ct": ct, "model": kp.feeds_ct_model().get(ct, ""),
            "keywords": kw["positive"], "minus": kw["minus"],
            "callouts": co, "images": im,
            "from": "pack" if has else "fallback"}

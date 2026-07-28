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

    def _is_standalone_auto_brand(ct: str, name: str) -> bool:
        """Singleton auto brands without model ct children are still Марки.

        The prefix heuristic above catches brands with child models (Haval → Haval Jolion), but
        legacy/rare brands such as Lifan/Volvo/Lexus may exist only as one ag_part1 row. Keep
        leadgen ct08xx and topic buckets (Авито/Дром/Автокредит, slash names) as Общее.
        """
        try:
            n = int(str(ct or "")[2:])
        except Exception:  # noqa: BLE001
            return False
        parts = str(name or "").strip().split()
        if not (19 <= n <= 318) or len(parts) != 1:
            return False
        raw = parts[0]
        return ("/" not in raw) and bool(re.search(r"[A-Za-z]", raw))

    out: dict = {}
    for ct, ln in low.items():
        parts = ln.split()
        if len(parts) >= 2 and _is_brand(parts[0]):
            out[ct] = "Модели"
        elif ln and (_is_brand(ln) or _is_standalone_auto_brand(ct, ln)):
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
    # Donor-fallback tp4→tp2→tp1, tp2→tp1 — зеркало slepki_editor.read_group_keywords:580-588.
    # Если в родном паке real==0, но donor-tp содержит реальные ключи — берём их.
    # auto сохраняем из родного пака (маркер ---autotargeting остаётся без изменений).
    if real == 0 and tp in ("tp2", "tp4"):
        donors: tuple = ("tp2", "tp1") if tp == "tp4" else ("tp1",)
        for donor_tp in donors:
            d_kd = os.path.join(kp._ct_dir(site_type, donor_tp, ct), "keywords")
            if slug:
                d_pos, d_found = _pack_read_local(os.path.join(d_kd, f"{slepok}__{slug}.txt"))
                if not d_found:
                    d_pos, _ = _pack_read_local(os.path.join(d_kd, f"{slepok}.txt"))
            else:
                d_pos, _ = _pack_read_local(os.path.join(d_kd, f"{slepok}.txt"))
            d_real = 0
            d_reals: list = []
            for line in d_pos:
                s = (line or "").strip()
                if not s:
                    continue
                if not _PACK_AUTO_RE.search(s):
                    d_real += 1
                    d_reals.append(s)
            if d_real > 0:
                real = d_real
                reals = d_reals
                break
    # sig — подпись СОДЕРЖИМОГО пака (кол-во + первый/последний реальный ключ), 1:1 с клиентским
    # `_sig` в _slCountKeywords. Нужна для дедупа счётчика: группы без своего per-group файла
    # падают на ОДИН ct-агрегат и должны быть посчитаны один раз. Наружу (в pack_facts) НЕ уходит.
    sig = f"{real}:" + (f"{reals[0]}…{reals[-1]}" if reals else "")
    return {"real": real, "auto": auto, "sig": sig}


def _tp67_group_fact_with_real_library(
    slepok: str,
    site_type: str,
    tp: str,
    ct: str,
    gk: str,
    position_name: str,
    sq: str,
    base_fact: dict,
    real_items: list | dict | None = None,
    cctx=None,
) -> dict:
    """tp6/tp7 fact for UI names/badges, including the same real-library fallback as creation.

    Plain pack facts see only M3 files. UAC positions can legitimately get keywords from
    ``tp67_real_keywords.json`` through ``create_set_context._tp67_keywords_for``; the detail
    card already uses that path. Without it the tree can display an old ``Автотаргетинг`` name
    while the campaign will be created as keyword-driven.
    """
    if (base_fact.get("real") or 0) > 0:
        return base_fact
    try:
        if cctx is None:
            try:
                from . import automation_runtime as ar  # lazy: gets the configured DI module in web services
                cctx = ar._create_set_context_module()
            except Exception:  # noqa: BLE001
                from . import create_set_context as cctx
        items = real_items if real_items is not None else cctx._tp67_real_keyword_items()

        def _real_library(tp_key: str) -> list:
            skey = cctx._SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
            pos_key = cctx._tp67_kw_position_key(position_name)
            ct_key = (ct or "").strip().lower()
            sq_key = (sq or "").strip().lower()
            best = None
            best_score = None
            if isinstance(items, dict):
                candidates = []
                if pos_key:
                    candidates.extend(items.get("by_pos", {}).get((tp_key, skey, pos_key), []))
                if ct_key:
                    candidates.extend(items.get("by_ct", {}).get((tp_key, skey, ct_key), []))
            else:
                candidates = items or []
            seen_ids: set[int] = set()
            for it in candidates:
                obj_id = id(it)
                if obj_id in seen_ids:
                    continue
                seen_ids.add(obj_id)
                if it.get("tp") != tp_key or not (it.get("keywords") or []):
                    continue
                if it.get("slepok") != skey:
                    continue
                site_score = 1 if (not site_type or it.get("site_type") == site_type) else 0
                sq_score = 1 if (not sq_key or it.get("sq") == sq_key) else 0
                ct_score = 1 if (ct_key and it.get("ct") == ct_key) else 0
                p_score = 1 if (pos_key and it.get("position") == pos_key) else 0
                if not (ct_score or p_score):
                    continue
                score = (1, site_score, sq_score, p_score, ct_score, len(it.get("keywords") or []))
                if best_score is None or score > best_score:
                    best = it
                    best_score = score
            if not best:
                return []
            return cctx._kw_clean(
                cctx._drop_used_car(
                    cctx._drop_foreign_city_keywords(best.get("keywords") or [], ""),
                    site_type,
                ),
                200,
            )

        pos = _real_library(tp)
        if not pos and tp == "tp7":
            pos = _real_library("tp6")
    except Exception:  # noqa: BLE001 — UI fact is best-effort; keep pack-only fallback
        return base_fact
    real = 0
    auto = bool(base_fact.get("auto"))
    reals: list = []
    for line in pos or []:
        s = (line or "").strip()
        if not s:
            continue
        if _PACK_AUTO_RE.search(s):
            auto = True
        else:
            real += 1
            reals.append(s)
    if real <= 0 and auto == bool(base_fact.get("auto")):
        return base_fact
    sig = f"{real}:" + (f"{reals[0]}…{reals[-1]}" if reals else "")
    return {"real": real, "auto": auto, "sig": sig}


def _slepki_pack_signature(struct: dict) -> str:
    """Cheap signature for cached pack-derived facts in `/api/ui_structure`.

    Per-file stat of ~15k keyword candidates is too expensive on LXC/M3 mirrors. Pack edits made
    through the slepki worker touch this marker, so the web processes can invalidate cached
    `pack_facts`/`kw_totals` with one local stat.
    """
    marker = os.path.join(os.path.dirname(__file__), "slepki_pack_cache.marker")
    try:
        st = os.stat(marker)
        return f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return "missing"


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
    tp67_real_items: dict | None = None
    tp67_context = None
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
                            merged_gks = [str(x).strip() for x in (it.get("merged_gks") or []) if str(x).strip()]
                            all_gks = [gk] + [x for x in merged_gks if x and x != gk]
                            key = f"{slepok}|{site}|{code}|{ct}|{gk}"
                            if key in facts:
                                continue
                            fact = _pack_group_fact(slepok, site, code, ct, gk)
                            if code in ("tp6", "tp7"):
                                if tp67_context is None:
                                    try:
                                        from . import automation_runtime as ar
                                        tp67_context = ar._create_set_context_module()
                                    except Exception:  # noqa: BLE001
                                        from . import create_set_context as tp67_context
                                if tp67_real_items is None:
                                    try:
                                        from . import automation_runtime as ar
                                        raw_items = ar._json("tp67_real_keywords.json").get("items") or []
                                    except Exception:  # noqa: BLE001
                                        raw_items = []
                                    by_pos: dict = {}
                                    by_ct: dict = {}
                                    for ri in raw_items:
                                        if not isinstance(ri, dict) or not (ri.get("keywords") or []):
                                            continue
                                        rk = str(ri.get("slepok") or "")
                                        rtp = str(ri.get("tp") or "")
                                        rpos = str(ri.get("position") or "")
                                        rct = str(ri.get("ct") or "").lower()
                                        if rpos:
                                            by_pos.setdefault((rtp, rk, rpos), []).append(ri)
                                        if rct:
                                            by_ct.setdefault((rtp, rk, rct), []).append(ri)
                                    tp67_real_items = {"by_pos": by_pos, "by_ct": by_ct}
                                position_name = ""
                                camp_names = it.get("camp_names") or []
                                if camp_names:
                                    position_name = str(camp_names[0] or "").strip()
                                position_name = position_name or str(it.get("t") or g.get("name") or "").strip()
                                if merged_gks:
                                    real = 0
                                    auto = False
                                    sig_parts: list[str] = []
                                    seen_fact_sigs: set[str] = set()
                                    for one_gk in all_gks:
                                        one = _pack_group_fact(slepok, site, code, ct, one_gk)
                                        one = _tp67_group_fact_with_real_library(
                                            slepok, site, code, ct, one_gk, position_name, sp.get("sq") or "", one,
                                            real_items=tp67_real_items, cctx=tp67_context,
                                        )
                                        if one["sig"] not in seen_fact_sigs:
                                            seen_fact_sigs.add(one["sig"])
                                            real += int(one.get("real") or 0)
                                            sig_parts.append(one["sig"])
                                        auto = auto or bool(one.get("auto"))
                                    fact = {"real": real, "auto": auto, "sig": "|".join(sig_parts)}
                                else:
                                    fact = _tp67_group_fact_with_real_library(
                                        slepok, site, code, ct, gk, position_name, sp.get("sq") or "", fact,
                                        real_items=tp67_real_items, cctx=tp67_context,
                                    )
                                try:
                                    pos_key = f"{sp.get('sq') or 'site'}|{g.get('name') or ''}|{it.get('t') or ''}"
                                    exp = tp67_context.tp67_struct_expectations(
                                        slepok, site, code, ct, "", position_name, sp.get("sq") or "",
                                        pos_key=pos_key,
                                    )
                                    modes = exp.get("modes") or []
                                    kws = exp.get("keywords") or []
                                    fact["real"] = len(kws)
                                    fact["sig"] = f"{len(kws)}:" + (f"{kws[0]}…{kws[-1]}" if kws else "")
                                    fact["audiences"] = len(exp.get("audiences") or [])
                                    fact["target_label"] = tp67_context.tp67_targeting_label_from_modes(modes, code)
                                except Exception:  # noqa: BLE001 — UI label is best-effort; keep keyword fact
                                    pass
                            out_fact = {"real": fact["real"], "auto": fact["auto"]}
                            if code in ("tp6", "tp7"):
                                out_fact["audiences"] = int(fact.get("audiences") or 0)
                                if fact.get("target_label"):
                                    out_fact["target_label"] = str(fact.get("target_label") or "")
                            facts[key] = out_fact
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

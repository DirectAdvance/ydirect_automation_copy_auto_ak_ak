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
        for st in d.get("site_types", []):
            stype = st.get("name") or ""
            st["tp"] = [t for t in st.get("tp", [])
                        if not _slepok_profile_excludes_tp(key, stype, t.get("code") or "")]
    return out


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

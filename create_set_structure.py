"""Единый контракт «Структура слепков → Создание РК 1:1» (задача 7).

Зеркало UI-группировки `routes_slepki_edit._build_export_rows`: КАМПАНИЯ = значение из
`item.camp_names` (а не сегмент-коллапс Марки/Модели/Общее). Один item (=группа) может
принадлежать НЕСКОЛЬКИМ кампаниям (camp_names — список) → эмитится в каждую.

Здесь ТОЛЬКО чистая логика группировки + чтение защищённых тегов («х3» / «все фиды») из БД
`campaign_tags`. Никаких API/Grid/баллов. Используется:
  • `create_set_plan._set_plan_response` — построение plan[] по camp_names (tp1–tp5);
  • превью структуры (parity дерева и создания).

Load-bearing: возвращаемые `gks`/`cts`/`segment` — маршрутизация контента per-group (ct/gk) в
движках (`_build_tp1_from_pack`/`_tp1_pack_groups` фильтруют по `only_gks`/`only_cts`), иначе
«0/184 групп». Fallback camp_names ИДЕНТИЧЕН `_build_export_rows` (split-label → сегмент → имя).
"""
from __future__ import annotations

import json
import os
import re

# Управляющие теги (единственные, что влияют на СОЗДАНИЕ — CREATION_PROTECTED_RULES.md).
X3_TAG = "х3"
ALL_FEEDS_TAG = "все фиды"
# «каталоги» — форсит ShoppingAd+ListingAd в tp1-кампаниях (для tp3/tp5/tp7 no-op: листинги всегда).
CATALOG_TAG = "каталоги"

# Три кампании тега «х3» на tp1 (КАЖДОЙ — полный бюджет; порядок КС → Автотаргет → КС+Автотаргет).
X3_VARIANTS = [
    {"suffix": "КС", "autotarget": False, "keep_keywords": True},
    {"suffix": "Автотаргетинг", "autotarget": True, "keep_keywords": False},
    {"suffix": "КС + Автотаргетинг", "autotarget": True, "keep_keywords": True},
]

_CT_RE = re.compile(r"ct\d{4}")
_AUD_CTS = {"ct0002", "ct0003", "ct0004", "ct0005"}
_STATE_RE = re.compile(r"_(aon|aoff)_")
# КС-токен в имени (для х3-эвристики)
_KW_NAME_RE = re.compile(r"(^|[^а-яё])кс([^а-яё]|$)")


def _first_ct(gc: str) -> str:
    m = _CT_RE.search(gc or "")
    return m.group(0) if m else ""


def _state(gc: str) -> str:
    m = _STATE_RE.search(gc or "")
    return m.group(1) if m else ""


def _strip_prefix(t: str) -> str:
    return re.sub(r"^РСЯ\s+", "", re.sub(r"^Поиск\s+", "", str(t or "")))


def _slepok_key(slepok: str) -> str:
    """Слуг слепка → канонический key структуры (как в движках)."""
    try:
        from .pack_resolver import _SLEPOK_KEY
    except Exception:  # noqa: BLE001
        _SLEPOK_KEY = {}
    return _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())


def _load_struct() -> dict:
    # Структура разбита на per-slepok файлы (direct/slepki/) — собираем через slepki_store
    # (у него свой кэш по сигнатуре mtime+size частей, ~16.5k items не перечитываются зря).
    from . import slepki_store as _ss
    try:
        return _ss.assemble()
    except Exception:  # noqa: BLE001
        return {}


def _gk_of(it: dict) -> str:
    """Слуг группы item'а: авторитетное поле gk ИЛИ выведенный из gc (как _struct_items)."""
    gk = (it.get("gk") or "").strip()
    if gk:
        return gk
    try:
        from . import kontent_pack as kp
        return kp._group_slug(it.get("gc") or "")
    except Exception:  # noqa: BLE001
        return ""


def _iter_items(t: dict):
    """(split_label, item) по всем контейнерам tp: плоские groups + splits.groups (== _ex_iter_items)."""
    for g in (t.get("groups") or []):
        for it in (g.get("items") or []):
            if isinstance(it, dict):
                yield "", it
    for sp in (t.get("splits") or []):
        lbl = sp.get("label") or ""
        for g in (sp.get("groups") or []):
            for it in (g.get("items") or []):
                if isinstance(it, dict):
                    yield lbl, it


def _tp_num(code: str) -> int:
    m = re.search(r"\d+", str(code or ""))
    return int(m.group(0)) if m else 0


def _seg_map():
    from . import blueprint_targeting as _btg
    try:
        return _btg._ct_segment_map()
    except Exception:  # noqa: BLE001
        return {}


def _non_auto():
    from . import blueprint_targeting as _btg
    try:
        return set(_btg._non_auto_slepki())
    except Exception:  # noqa: BLE001
        return set()


def _seg_tgt(tpn: int, gc: str) -> str:
    """Таргетинг сегментной группы (== _ex_seg_tgt без baked; baked-метка — не для маршрутизации)."""
    if tpn == 3:
        return "Товарная галерея"
    aud = _first_ct(gc) in _AUD_CTS
    st = _state(gc)
    if aud:
        lbl = "КС+аудитории" if st == "aoff" else "аудитории"
    else:
        lbl = "КС" if st == "aoff" else "автотаргетинг"
    if tpn in (2, 4) and lbl == "автотаргетинг":  # tp2/tp4 aon = КС
        lbl = "КС"
    return lbl


def structure_to_campaigns(slepok: str, site_type: str, tp_code: str) -> list[dict]:
    """Кампании tp из структуры слепка, СГРУППИРОВАННЫЕ по item.camp_names (зеркало _build_export_rows).

    Возвращает список кампаний в порядке первого появления:
      {name, tp, segment (доминантный|None), segments[list], gks[list], cts[list],
       targeting, n_groups}
    Пусто → нет tp/структуры/парс-сбой (caller делает fallback на прежний сегмент-путь).

    name = camp_key для тегов (campaign_tags.camp_key). gks/cts — маршрутизация контента в движке.
    """
    key = _slepok_key(slepok)
    struct = _load_struct()
    dl = next((x for x in (struct.get("directologists") or []) if x.get("key") == key), None)
    if not dl:
        return []
    st = next((s for s in (dl.get("site_types") or []) if s.get("name") == site_type), None)
    if not st:
        return []
    tp = next((t for t in (st.get("tp") or []) if (t.get("code") or "").strip() == tp_code), None)
    if not tp:
        return []
    tpn = _tp_num(tp_code)
    seg_map = _seg_map()
    non_auto = _non_auto()

    def _seg_of(gc: str) -> str:
        return seg_map.get(_first_ct(gc), "Марки")

    # tp6/tp7: каждая группа = кампания (имя из item.t) — для parity превью (создание идёт _emit_struct).
    if tpn in (6, 7):
        camps: list[dict] = []
        seen_c: dict = {}
        for _lbl, it in _iter_items(tp):
            # tp6/7 имя = сырое item.t (НЕ префикс-стрип: зеркало _build_export_rows `camp = it.get("t") or nm`;
            # в структуре есть tp6/7-имена с «РСЯ - …», их стрипать нельзя). Прежний `or _strip_prefix(...)` был мёртв.
            nm = (it.get("t") or "").strip()
            if not nm or nm in seen_c:
                continue
            gc = it.get("gc") or ""
            c = {"name": nm, "tp": tp_code, "segment": None, "segments": [],
                 "gks": [g for g in [_gk_of(it)] if g], "cts": [c for c in [_first_ct(gc)] if c],
                 "targeting": "", "n_groups": 1, "items": [it]}
            seen_c[nm] = c
            camps.append(c)
        return camps

    # сегментные tp1/2/4/5 + tp3: КАМПАНИЯ = item.camp_names (fallback split-label / сегмент / имя).
    seg_tp = (tpn in (1, 2, 4, 5)) and not (tpn == 2 and key in non_auto)
    order: list[str] = []
    by_name: dict[str, dict] = {}
    seen_group: set = set()  # (name, gk) — дедуп группы в кампании (== _build_export_rows seen)
    for split_label, it in _iter_items(tp):
        gc = it.get("gc") or ""
        if not gc:
            continue
        nm = _strip_prefix(it.get("t") or "")
        grp = nm or gc
        gk = _gk_of(it)
        ct = _first_ct(gc)
        seg = _seg_of(gc)
        cn_list = it.get("camp_names") or []
        if not cn_list:
            if split_label:
                cn_list = [split_label]
            elif seg_tp:
                cn_list = [seg]
            else:
                cn_list = [nm or "Общее"]
        tgt = _seg_tgt(tpn, gc)
        for cn0 in cn_list:
            cn = cn0 or "Общее"
            dedup_key = (cn, grp)
            if dedup_key in seen_group:
                continue
            seen_group.add(dedup_key)
            c = by_name.get(cn)
            if c is None:
                c = {"name": cn, "tp": tp_code, "segment": None, "segments": [],
                     "gks": [], "cts": [], "targeting": tgt, "n_groups": 0,
                     "_segcount": {}, "items": []}
                by_name[cn] = c
                order.append(cn)
            if gk and gk not in c["gks"]:
                c["gks"].append(gk)
            if ct and ct != "ct0000" and ct not in c["cts"]:
                c["cts"].append(ct)
            c["_segcount"][seg] = c["_segcount"].get(seg, 0) + 1
            c["n_groups"] += 1
            c["items"].append(it)

    out: list[dict] = []
    for cn in order:
        c = by_name[cn]
        segcount = c.pop("_segcount", {})
        c["segments"] = sorted(segcount)
        # доминантный сегмент (для adPrice/fallback-фильтра в движке); один сегмент → он, иначе None
        c["segment"] = (max(segcount, key=segcount.get) if len(segcount) == 1 else None)
        out.append(c)
    return out


# ── защищённые теги («х3» / «все фиды») из БД campaign_tags ─────────────────────────────────────

# Кэш тегов на (slepok, site_type): один план строит tp1/tp3/tp5 → 3 fresh-соединения к БД за прогон.
# Читаем ВСЕ tp одним запросом, кэшируем на короткий TTL (правки тегов через UI редки → подхватятся
# на след. плане). Ключ — frozenset(slepok_vals) чтобы slug/key-варианты били в один слот.
_TAGS_CACHE: dict = {}
_TAGS_TTL = 5.0  # сек


def _tags_all_tp(slepok: str, site_type: str) -> dict[str, dict[str, set]]:
    """{tp → {camp_key → {labels}}} по (slepok, site_type), одним запросом. Сбой/нет таблицы → {}."""
    import time
    key = _slepok_key(slepok)
    slepok_vals = sorted({slepok, key})
    ck = (tuple(slepok_vals), site_type)
    now = time.monotonic()
    hit = _TAGS_CACHE.get(ck)
    if hit is not None and (now - hit[0]) < _TAGS_TTL:
        return hit[1]
    try:
        import psycopg2.extras
        from telegram_parsing.db import get_db
        conn = get_db()
    except Exception:  # noqa: BLE001
        return {}
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT ct.tp, ct.camp_key, r.label FROM direct_automation.campaign_tags ct "
                    "JOIN direct_automation.tag_registry r ON r.id = ct.tag_id "
                    "WHERE ct.slepok = ANY(%s) AND ct.site_type = %s",
                    (slepok_vals, site_type),
                )
                rows = cur.fetchall() or []
    except Exception:  # noqa: BLE001
        return {}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    by_tp: dict[str, dict[str, set]] = {}
    for r in rows:
        by_tp.setdefault(r["tp"], {}).setdefault(r["camp_key"], set()).add((r["label"] or "").strip())
    _TAGS_CACHE[ck] = (now, by_tp)
    return by_tp


def campaign_protected_tags_bulk(slepok: str, site_type: str, tp_code: str) -> dict[str, set]:
    """{camp_key → {labels}} управляющих тегов кампаний (slepok, site_type, tp).

    Читает direct_automation.campaign_tags JOIN tag_registry. Только «х3»/«все фиды» релевантны
    созданию, но возвращаем все назначенные метки (caller фильтрует). Любой сбой (нет таблицы,
    БД недоступна) → {} (мягкая деградация — теги display-only не должны валить план)."""
    return dict(_tags_all_tp(slepok, site_type).get(tp_code, {}))


def _is_feed_fanout_camp(camp: dict) -> bool:
    """UI-эвристика «все фиды»: кампания фид-фанаута = имя про фид/смарт-баннер ИЛИ ≥3 позиций с
    ЯВНЫМ фидом (feed_role/feed_id/feed_key). НЕ по формату-ct (ct010 у ВСЕХ групп tp5 → ложные)."""
    low = (camp.get("name") or "").lower()
    if ("фид" in low) or ("смарт-баннер" in low) or ("смарт-банер" in low) or ("все фиды" in low):
        return True
    n = 0
    for it in (camp.get("items") or []):
        if it.get("feed_role") or it.get("feed_id") or it.get("feed_key"):
            n += 1
            if n >= 3:
                return True
    return False


def _base_target(nm: str) -> str:
    """Базовое имя кампании без хвостового таргетинг-сегмента дэш-стиля («… - КС»/«… - Автотаргетинг/КС»).
    Нужно для сверки «есть ли уже отдельные КС/Автотаргет-кампании того же базового имени»."""
    parts = re.split(r"\s+-\s+", nm or "")
    if len(parts) >= 2:
        tail = parts[-1].lower()
        if ("автотаргет" in tail) or bool(_KW_NAME_RE.search(tail)) or ("ключев" in tail):
            return " - ".join(parts[:-1]).strip().lower()
    return (nm or "").strip().lower()


def _x3_split_already_present(camp_name: str, siblings) -> bool:
    """True, если в наборе УЖЕ есть отдельная КС-only И отдельная Автотаргет-only кампания того же
    базового имени, что гибрид → тройной сплит уже материализован в структуре, х3 создаст ДУБЛИ.

    Реальный (и единственный на 2026-07-15) случай: terehov/С пробегом «РСЯ - Общие запросы -
    {КС | Автотаргетинг | Автотаргетинг/КС}» — гибрид «Автотаргетинг/КС» тут = уже отдельная третья
    кампания, а не одиночный гибрид, который нужно троить."""
    if not siblings:
        return False
    hb = _base_target(camp_name)
    ks_only = at_only = False
    for s in siblings:
        sl = (s or "").lower()
        if not sl or _base_target(s) != hb:
            continue
        s_at = "автотаргет" in sl
        s_kw = bool(_KW_NAME_RE.search(sl)) or "ключев" in sl
        if s_kw and not s_at:
            ks_only = True
        elif s_at and not s_kw:
            at_only = True
    return ks_only and at_only


def detect_protected_tags(camp: dict, registry_tags=None, siblings=None) -> set:
    """Управляющие теги кампании {«х3», «все фиды»}: РЕЕСТР OVERRIDE → UI-эвристика fallback.

    Семён 2026-07-15: «что видно в UI — то и создаётся», реестр может переопределить.
    • реестр (`campaign_tags`) назначен для кампании → он авторитетен (берём только control-теги);
    • реестра нет → эвристика бейджей:
        – «х3» (только tp1): имя несёт И «автотаргет», И «кс/ключев» (гибрид → 3 РК),
          НО не когда структура уже содержит отдельные КС-only+Автотаргет-only кампании того же
          базового имени (`siblings`) — иначе троение гибрида задублирует их (см. `_x3_split_already_present`);
        – «все фиды» (tp1/tp3/tp5): фид-фанаут (`_is_feed_fanout_camp`).

    `siblings` — имена всех кампаний того же tp-набора (для guard х3); None → guard не применяется
    (совместимо с caller'ами tp3/tp5, где х3 не эмитится вовсе)."""
    reg = {t for t in (registry_tags or ()) if t}
    if reg:                                              # реестр OVERRIDE
        return {t for t in reg if t in (X3_TAG, ALL_FEEDS_TAG, CATALOG_TAG)}
    out: set = set()
    tp = camp.get("tp")
    low = (camp.get("name") or "").lower()
    if (tp == "tp1" and ("автотаргет" in low) and (bool(_KW_NAME_RE.search(low)) or "ключев" in low)
            and not _x3_split_already_present(camp.get("name") or "", siblings)):
        out.add(X3_TAG)
    if tp in ("tp1", "tp3", "tp5") and _is_feed_fanout_camp(camp):
        out.add(ALL_FEEDS_TAG)
    return out

"""Единый контракт «Структура слепков → Создание РК 1:1» (задача 7).

Зеркало UI-группировки `routes_slepki_edit._build_export_rows`: КАМПАНИЯ = логическая
кампания дерева структуры, а не сырой список `item.camp_names`. Для tp1/tp2/tp4/tp5
хвосты таргетинга (`КС`, `Автотаргетинг`, `КС + Автотаргетинг`) схлопываются в один узел
дерева, а итоговая метка таргетинга рассчитывается по фактическому составу групп. Один
item (=группа) может принадлежать нескольким логическим кампаниям → эмитится в каждую.

Здесь ТОЛЬКО чистая логика группировки + чтение защищённых тегов («х3» / «все фиды») из БД
`campaign_tags`. Никаких API/Grid/баллов. Используется:
  • `create_set_plan._set_plan_response` — построение plan[] по дереву структуры;
  • превью структуры (parity дерева и создания).

Load-bearing: возвращаемые `gks`/`cts`/`segment` — маршрутизация контента per-group (ct/gk) в
движках (`_build_tp1_from_pack`/`_tp1_pack_groups` фильтруют по `only_gks`/`only_cts`), иначе
«0/184 групп». Fallback ИДЕНТИЧЕН `_build_export_rows` (split-label → сегмент → имя).
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
_COMMON_CTS = {"ct0000", "ct0008", "ct0009", "ct0010", "ct0013", "ct0014"}
_STATE_RE = re.compile(r"_(aon|aoff)_")
# КС-токен в имени (для х3-эвристики)
_KW_NAME_RE = re.compile(r"(^|[^а-яё])кс([^а-яё]|$)")
_TRAILING_ORD_RE = re.compile(r"\s+(?:(?:1|2|3)|20\d{2})$")


def _first_ct(gc: str) -> str:
    m = _CT_RE.search(gc or "")
    return m.group(0) if m else ""


def _state(gc: str) -> str:
    m = _STATE_RE.search(gc or "")
    return m.group(1) if m else ""


def _strip_prefix(t: str) -> str:
    return re.sub(r"^РСЯ\s+", "", re.sub(r"^Поиск\s+", "", str(t or "")))


def _camp_segment_filter(name: str) -> set[str]:
    """Explicit segment named by a harvested campaign.

    ``camp_names`` may contain broad segment campaigns and concrete brand/model campaigns at once.
    Only explicit segment words are restrictive. Concrete names such as ``РСЯ - Lada - КС`` are not
    parsed here and remain accepted for backward compatibility.
    """
    s = re.sub(r"\s+", " ", str(name or "").strip().lower().replace("ё", "е"))
    out: set[str] = set()
    if re.search(r"\bмарк[аи]\b", s):
        out.add("Марки")
    if re.search(r"\b(?:модел[ьи]|все модели)\b", s):
        out.add("Модели")
    if re.search(r"\bобщ(?:ая|ее|ие|ие запросы)\b", s) or "авто/автомобили/машины" in s:
        out.add("Общее")
    return out


def camp_name_matches_group_segment(name: str, segment: str) -> bool:
    """Whether a harvested campaign name can contain a group of ``segment``.

    Empty filter means the campaign is concrete/format-specific and should not be filtered by the
    Марки/Модели/Общее classifier.
    """
    allowed = _camp_segment_filter(name)
    return not allowed or (segment in allowed)


def camp_name_matches_tp(name: str, tpn: int) -> bool:
    s = str(name or "").strip()
    if tpn == 1:
        return bool(re.match(r"^\s*РСЯ(?![А-Яа-яЁёA-Za-z0-9])", s, flags=re.I))
    if tpn == 2:
        return bool(re.match(r"^\s*Поиск\s+-", s, flags=re.I))
    if tpn == 3:
        return bool(re.match(r"^\s*(?:Товарная\s+галерея|ТГ(?![А-Яа-яЁёA-Za-z0-9])|ТГ_|Поиск_ТГ)",
                             s, flags=re.I))
    if tpn == 4:
        return bool(re.match(r"^\s*Поиск\s*\+\s*Динамика\s+-", s, flags=re.I))
    if tpn == 5:
        return bool(re.match(r"^\s*Поиск\s*\+\s*Динамика\s*\+\s*(?:ТГ|Товарная\s+галерея)\s+-",
                             s, flags=re.I))
    return True


def filtered_camp_names_for_group(names, segment: str, tpn: int | None = None) -> list[str]:
    """Drop segment-crossed ``camp_names`` while preserving order and uniqueness."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in names or []:
        cn = str(raw or "").strip()
        if not cn or cn in seen:
            continue
        if tpn is not None and not camp_name_matches_tp(cn, int(tpn or 0)):
            continue
        if camp_name_matches_group_segment(cn, segment):
            seen.add(cn)
            out.append(cn)
    return out


def canonical_campaign_name(name: str, targeting: str = "", *, from_camp_names: bool = True) -> str:
    """Canonical campaign key used by structure UI and create-plan grouping.

    ``item.camp_names`` is the authoritative campaign name harvested into the slepok structure.
    For those rows we must not rewrite the visible targeting suffix from keyword-pack facts:
    structure → plan → account must stay 1:1 by the campaign name shown in the UI.

    Fallback/generated names may still receive a targeting suffix.
    """
    base = str(name or "").replace("\u00a0", " ")
    base = re.sub(r"\s+", " ", base).strip()
    base = re.sub(r"\s*\+\s*", " + ", base)
    base = re.sub(r"\s*-\s*", " - ", base)
    if not base:
        return "Общее"
    if from_camp_names:
        return base
    return _rename_target_tail(base, targeting)


def duplicate_group_base(name: str) -> str:
    """Normalize harvested ordinal duplicates: ``Lada 1``/``Lada 2`` -> ``lada``.

    The base is used only when another unsuffixed group with the same campaign+gc exists, so real
    model numbers are not collapsed just because they end with a digit.
    """
    return _TRAILING_ORD_RE.sub("", re.sub(r"\s+", " ", str(name or "").strip().lower())).strip()


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
        lbl = "КС" if st == "aoff" else "Автотаргетинг"
    if tpn in (2, 4) and lbl == "Автотаргетинг":  # tp2/tp4 aon = КС
        lbl = "КС"
    return lbl


def _pack_targeting_for_group(slepok: str, site_type: str, tp_code: str, ct: str, gk: str,
                              fallback: str) -> str:
    """Targeting mode from actual keyword pack for tp1/tp2/tp4/tp5 campaign names."""
    try:
        from . import kontent_pack as kp  # local import keeps module usable in scripts
        kw = kp.read_keywords(site_type, tp_code, ct, slepok, group=gk)
    except Exception:  # noqa: BLE001
        return fallback
    pos = [str(x or "").strip() for x in (kw.get("positive") or []) if str(x or "").strip()]
    has_auto = any(x.lower() == "---autotargeting" for x in pos)
    has_real = any(x.lower() != "---autotargeting" for x in pos)
    if has_real and has_auto:
        return "КС + Автотаргетинг"
    if has_real:
        return "КС"
    if has_auto:
        return "Автотаргетинг"
    return fallback


_TARGET_TAIL_RE = re.compile(
    r"\s+-\s*(?:КС\s+CPA|КС\s*\+\s*Автотаргетинг|Автотаргетинг\s*\+\s*КС|"
    r"КС|Ключевики|Ключевые\s+слова|Автотаргетинг|Автотаргет)\s*$",
    re.IGNORECASE,
)


def _rename_target_tail(name: str, targeting: str) -> str:
    base = str(name or "").strip()
    tgt = str(targeting or "").strip()
    if not base or not tgt:
        return base
    while True:
        nxt = _TARGET_TAIL_RE.sub("", base).strip()
        if nxt == base:
            break
        base = nxt
    return f"{base} - {tgt}"


def _target_tail_from_name(name: str) -> str:
    m = _TARGET_TAIL_RE.search(str(name or "").strip())
    if not m:
        return ""
    return _seg_tgt_label(m.group(0).lstrip(" -"))


def tree_campaign_base_name(name: str, tpn: int) -> str:
    """Campaign node name in the visible structure tree before the final targeting badge.

    Harvested `camp_names` can contain separate rows for `... - КС`, `... - Автотаргетинг` and
    `... - КС + Автотаргетинг`. The UI tree and create-plan must treat them as one logical campaign
    node for segment TPs. tp3/tp6/tp7 keep raw campaign semantics.
    """
    raw = canonical_campaign_name(name or "Общее", "", from_camp_names=True)
    if int(tpn or 0) not in (1, 2, 4, 5):
        return raw
    base = raw
    while True:
        nxt = _TARGET_TAIL_RE.sub("", base).strip()
        if nxt == base:
            break
        base = nxt
    return base or raw or "Общее"


def _seg_tgt_label(text: str) -> str:
    s = re.sub(r"\s+", " ", str(text or "").strip().lower().replace("ё", "е"))
    if "кс" in s and "автотаргет" in s:
        return "КС + Автотаргетинг"
    if "автотаргет" in s:
        return "Автотаргетинг"
    if "кс" in s or "ключ" in s:
        return "КС"
    return str(text or "").strip()


def _merge_targeting_labels(left: str, right: str) -> str:
    have = {str(left or ""), str(right or "")}
    if "КС + Автотаргетинг" in have:
        return "КС + Автотаргетинг"
    if "Автотаргетинг" in have and "КС" in have:
        return "КС + Автотаргетинг"
    return str(left or right or "")


def structure_to_campaigns(slepok: str, site_type: str, tp_code: str) -> list[dict]:
    """Кампании tp из структуры слепка, сгруппированные по видимому дереву.

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

    def _seg_of(gc: str, it: dict | None = None) -> str:
        ct = _first_ct(gc)
        seg = seg_map.get(ct)
        if seg:
            return seg
        if ct in _COMMON_CTS:
            return "Общее"
        inferred: set[str] = set()
        if it:
            for cn in (it.get("camp_names") or []):
                inferred.update(_camp_segment_filter(cn))
        if len(inferred) == 1:
            return next(iter(inferred))
        return "Марки"

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
            gks = [g for g in [_gk_of(it)] if g]
            for mgk in (it.get("merged_gks") or []):
                mgk = str(mgk or "").strip()
                if mgk and mgk not in gks:
                    gks.append(mgk)
            c = {"name": nm, "tp": tp_code, "segment": None, "segments": [],
                 "gks": gks, "cts": [c for c in [_first_ct(gc)] if c],
                 "targeting": "", "n_groups": 1, "items": [it]}
            seen_c[nm] = c
            camps.append(c)
        return camps

    # сегментные tp1/2/4/5 + tp3: КАМПАНИЯ = видимый узел дерева (fallback split-label / сегмент / имя).
    seg_tp = (tpn in (1, 2, 4, 5)) and not (tpn == 2 and key in non_auto)
    order: list[str] = []
    by_name: dict[str, dict] = {}
    # First pass: raw rows with segment-crossed camp_names removed. We keep this separate from the
    # final grouping so ordinal duplicates can see whether an unsuffixed base exists.
    rows: list[dict] = []
    bases_by_camp_gc: dict[tuple[str, str, str], set[str]] = {}
    counts_by_camp_gc_base: dict[tuple[str, str, str], int] = {}
    for split_label, it in _iter_items(tp):
        gc = it.get("gc") or ""
        if not gc:
            continue
        nm = _strip_prefix(it.get("t") or "")
        grp = nm or gc
        gk = _gk_of(it)
        gks = [g for g in [gk] if g]
        for mgk in (it.get("merged_gks") or []):
            mgk = str(mgk or "").strip()
            if mgk and mgk not in gks:
                gks.append(mgk)
        ct = _first_ct(gc)
        seg = _seg_of(gc, it)
        cn_list = filtered_camp_names_for_group(it.get("camp_names") or [], seg, tpn)
        from_camp_names = bool(cn_list)
        if not cn_list:
            if split_label:
                cn_list = [split_label]
            elif seg_tp:
                cn_list = [seg]
            else:
                cn_list = [nm or "Общее"]
        tgt = _seg_tgt(tpn, gc)
        for cn0 in cn_list:
            cn_src = canonical_campaign_name(cn0 or "Общее", "", from_camp_names=from_camp_names)
            cn = tree_campaign_base_name(cn_src, tpn) if (from_camp_names and seg_tp) else cn_src
            name_tgt = _target_tail_from_name(cn_src)
            actual_tgt = name_tgt or _pack_targeting_for_group(key, site_type, tp_code, ct, gk, tgt)
            if not (from_camp_names and seg_tp):
                cn = canonical_campaign_name(cn, actual_tgt, from_camp_names=from_camp_names)
            rows.append({"cn": cn, "grp": grp, "gk": gk, "gks": gks, "ct": ct, "seg": seg, "gc": gc,
                         "targeting": actual_tgt, "item": it})
            base = duplicate_group_base(grp)
            exact = re.sub(r"\s+", " ", grp.strip().lower()).strip()
            counts_by_camp_gc_base[(cn, gc, base)] = counts_by_camp_gc_base.get((cn, gc, base), 0) + 1
            if base == exact:
                bases_by_camp_gc.setdefault((cn, gc, base), set()).add(grp)

    seen_group: set = set()
    for row in rows:
        cn = row["cn"]
        grp = row["grp"]
        gc = row["gc"]
        base = duplicate_group_base(grp)
        # Collapse harvested ordinal clones only when the unsuffixed base exists in the same
        # campaign+gc bucket: Lada + Lada 1 => one logical group. This avoids merging genuine
        # numbered models where only the numbered names exist.
        bkey = (cn, gc, base)
        has_base = bool(bases_by_camp_gc.get(bkey))
        has_ordinal_clones = counts_by_camp_gc_base.get(bkey, 0) > 1
        grp_key = base if (has_base or has_ordinal_clones) else re.sub(r"\s+", " ", grp.strip().lower()).strip()
        dedup_key = (cn, gc, grp_key)
        if dedup_key in seen_group:
            c = by_name.get(cn)
            if c is not None:
                gks = row.get("gks") or [row.get("gk")]
                ct = row["ct"]
                for gk in gks:
                    if gk and gk not in c["gks"]:
                        c["gks"].append(gk)
                if ct and ct != "ct0000" and ct not in c["cts"]:
                    c["cts"].append(ct)
                c["targeting"] = _merge_targeting_labels(c.get("targeting") or "", row.get("targeting") or "")
            continue
        seen_group.add(dedup_key)
        gks = row.get("gks") or [row.get("gk")]
        ct = row["ct"]
        seg = row["seg"]
        tgt = row["targeting"]
        it = row["item"]
        c = by_name.get(cn)
        if c is None:
            c = {"name": cn, "_base_name": cn, "tp": tp_code, "segment": None, "segments": [],
                 "gks": [], "cts": [], "targeting": tgt, "n_groups": 0,
                 "_segcount": {}, "items": []}
            by_name[cn] = c
            order.append(cn)
        for gk in gks:
            if gk and gk not in c["gks"]:
                c["gks"].append(gk)
        if ct and ct != "ct0000" and ct not in c["cts"]:
            c["cts"].append(ct)
        if c.get("targeting") != tgt:
            c["targeting"] = _merge_targeting_labels(c.get("targeting") or "", tgt or "")
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
        if seg_tp:
            c["name"] = canonical_campaign_name(c.pop("_base_name", c.get("name") or cn),
                                                c.get("targeting") or "",
                                                from_camp_names=False)
        else:
            c.pop("_base_name", None)
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
    from . import kontent_pack as kp  # noqa: PLC0415 — как в остальном модуле, ленивый импорт
    # Теги («х3», «все фиды») заведены на БАЗОВЫЙ тип сайта; витринный сплит их бы не нашёл,
    # и состав создаваемого набора тихо поменялся бы. Нормализуем ДО ключа кэша.
    site_type = kp.base_site_type(site_type)
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
    • «х3» — только явный управляющий тег из реестра, имя кампании не должно размножать РК;
    • реестра нет → эвристика остаётся только для «все фиды» (tp1/tp3/tp5): фид-фанаут
      (`_is_feed_fanout_camp`).

    `siblings` — имена всех кампаний того же tp-набора (для guard х3); None → guard не применяется
    (совместимо с caller'ами tp3/tp5, где х3 не эмитится вовсе)."""
    reg = {t for t in (registry_tags or ()) if t}
    if reg:                                              # реестр OVERRIDE
        return {t for t in reg if t in (X3_TAG, ALL_FEEDS_TAG, CATALOG_TAG)}
    out: set = set()
    tp = camp.get("tp")
    if tp in ("tp1", "tp3", "tp5") and _is_feed_fanout_camp(camp):
        out.add(ALL_FEEDS_TAG)
    return out

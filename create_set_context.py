"""Create-set context/targeting helpers extracted from blueprint.py."""

from __future__ import annotations

import json
import re

_DEPS: dict = {}


def configure(deps: dict) -> None:
    """Bind blueprint dependencies used by context/targeting helpers."""
    _DEPS.clear()
    _DEPS.update(deps)
    globals().update(deps)


def _account_ctx(login: str):
    """Контекст для создания: domain, site_type, agency, geoid ОБЛАСТИ (таргетинг — область, не город)."""
    import psycopg2.extras
    conn = _victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT domain, city, site_type, agency_account, directologist FROM public.local_gsheet_sites "
                    "WHERE login_key=%s AND direction='Авто' LIMIT 1", (login,))
        row = cur.fetchone()
        if not row:
            return None
        oblast = None
        if row.get("city"):
            cur.execute('SELECT "Область" AS o FROM public.local_gsheet_yandex_direct_id_location '
                        "WHERE \"GeoRegionType\"='City' AND lower(btrim(location))=lower(btrim(%s)) LIMIT 1",
                        (row["city"],))
            r = cur.fetchone()
            oblast = (r["o"] if r else None)
    finally:
        conn.close()
    geoid = 225                                          # таргет — geoid ОБЛАСТИ (через словарь Директа)
    if oblast:
        gid = _geo_load().get(oblast.strip().lower())
        if gid:
            geoid = int(gid)
    return {"domain": (row.get("domain") or "").strip(), "site_type": (row.get("site_type") or "").strip(),
            "agency": row.get("agency_account"), "geoid": geoid, "oblast": oblast,
            "city": (row.get("city") or "").strip(),
            "directologist": (row.get("directologist") or "").strip()}

def _templates_for(site_type: str):
    """→ (titles, texts, sitelinks[{title,description}]) по типу сайта."""
    conn = _victory_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT kind, content FROM public.direct_ad_templates "
                    "WHERE enabled AND site_type=%s ORDER BY kind, id", (site_type,))
        titles, texts, sitelinks = [], [], []
        for kind, content in cur.fetchall():
            if kind == "title":
                titles.append(content)
            elif kind == "text":
                texts.append(content)
            elif kind == "sitelink":
                try:
                    d = json.loads(content)
                    sitelinks.append({"title": d.get("title", ""), "description": d.get("description", "")})
                except Exception:  # noqa: BLE001
                    pass
        return titles, texts, sitelinks
    finally:
        conn.close()

def _slepok_audiences_for(slepok: str, site_type: str, tp: str) -> list[str]:
    """Нативные интересы слепка для (slepok × site_type × tp) → объединённый список id (str).
    Источник: public.direct_slepok_audiences (kind in_market/interests). Пусто → []."""
    if not (slepok and site_type and tp):
        return []
    try:
        conn = _victory_conn()
    except Exception:  # noqa: BLE001
        return []
    try:
        cur = conn.cursor()
        cur.execute("SELECT interest_ids FROM public.direct_slepok_audiences "
                    "WHERE slepok=%s AND site_type=%s AND tp=%s", (slepok, site_type, tp))
        ids: set = set()
        for (arr,) in cur.fetchall():
            for x in (arr or []):
                if str(x).strip():
                    ids.add(str(x))
        return sorted(ids)
    except Exception:  # noqa: BLE001
        return []
    finally:
        conn.close()

def _norm_slepok_audience_category(x: str | None) -> str:
    s = re.sub(r"\s+", " ", (x or "").strip().lower())
    if s in ("", "общая"):
        return "(общая)"
    return s

def _tp67_targeting_mode(g: dict) -> str:
    """Новый канон tp6/tp7: keywords / autotarget / audience. Старые RA/RC-коды не поддерживаем."""
    text = " ".join(str(g.get(k) or "") for k in ("name", "group", "label", "code")).lower()
    label = str(g.get("label") or "").lower()
    if ("ключев" in label) or re.search(r"\bкс\b", label):
        return "keywords"
    if re.search(r"автотаргет|автоматическ", text):
        return "autotarget"
    if re.search(r"интерес|автокредит|авито|дром|auto\.ru|авто ру|конкурент", text):
        return "audience"
    return "autotarget"

def _tp67_audience_category_candidates(g: dict) -> list[str]:
    """Категории только внутри конкретного слепка; aliases нужны для старых подписей структуры."""
    text = " ".join(str(g.get(k) or "") for k in ("name", "group", "label")).lower()
    raw = [g.get("group"), g.get("name"), g.get("label")]
    out: list[str] = []
    for x in raw:
        nx = _norm_slepok_audience_category(str(x or ""))
        if nx and nx not in out:
            out.append(nx)
    if "общие запрос" in text:
        out.append("общие запросы")
    if "дилер интерес" in text:
        out.append("дилер интересы")
    if "дилер" in text:
        out.append("дилер")
    if "интерес" in text:
        out.append("интересы")
    if re.search(r"общая|товарная|модели|марки|автокредит|кредит|авито|дром|авто ру|auto\.ru", text):
        out.extend(["(общая)", "(нестандарт)"])
    dedup: list[str] = []
    for x in out:
        nx = _norm_slepok_audience_category(x)
        if nx and nx not in dedup:
            dedup.append(nx)
    return dedup

def _slepok_audience_cats(slepok: str, site_type: str, tp: str) -> list[dict]:
    """Аудитории слепка ПО КАТЕГОРИЯМ (БЕЗ мёржа) — как в слепках: отдельная кампания на категорию.
    → [{"category": str, "interest_ids": [str,...]}] из public.direct_slepok_audiences.
    Пустые категории отбрасываем. Источник тот же, что у _slepok_audiences_for, но без объединения."""
    if not (slepok and site_type and tp):
        return []
    try:
        conn = _victory_conn()
    except Exception:  # noqa: BLE001
        return []
    try:
        cur = conn.cursor()
        cur.execute("SELECT category, interest_ids FROM public.direct_slepok_audiences "
                    "WHERE slepok=%s AND site_type=%s AND tp=%s ORDER BY category", (slepok, site_type, tp))
        out = []
        for cat, arr in cur.fetchall():
            ids = sorted({str(x) for x in (arr or []) if str(x).strip()})
            if ids:
                out.append({"category": (cat or "(общая)"), "interest_ids": ids})
        return out
    except Exception:  # noqa: BLE001
        return []
    finally:
        conn.close()

def _slepok_struct_groups(slepok: str, site_type: str, tp: str) -> list[dict]:
    """Позиции СТРУКТУРЫ слепка для (slepok, site_type, tp6|tp7).

    Источник — slepki_structure.json (ТОТ ЖЕ, что рисует вкладки «Структура»/«Создание РК»),
    чтобы план создания совпадал с показом. is_auto берём из таргетинга группы (item.t):
    есть «КС»/«ключев…» → ручной (manual, ключи), иначе автотаргетинг."""
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    d = next((x for x in _json("slepki_structure.json").get("directologists", []) if x.get("key") == key), None)
    if not d:
        return []
    st = next((s for s in d.get("site_types", []) if s.get("name") == site_type), None)
    if not st:
        return []
    out: list[dict] = []
    for t in st.get("tp", []):
        if t.get("code") != tp:
            continue
        blocks = t.get("splits") or ([{"sq": "site", "groups": t.get("groups", [])}] if t.get("groups") else [])
        for sp in blocks:
            sq = sp.get("sq") or "site"
            for g in sp.get("groups", []):
                gname = (g.get("name") or "").strip()
                if gname.lower() in ("(общая)", "общая"):
                    gname = "Общая"
                items = [it for it in (g.get("items") or []) if isinstance(it, dict)] or [{}]
                for idx, it in enumerate(items):
                    label = (it.get("t") or "").strip()
                    label_clean = "" if label in ("", "—", "-") else label
                    tl = label.lower()
                    is_auto = not (("ключев" in tl) or re.search(r"\bкс\b", tl))
                    display = gname
                    if label_clean and label_clean.lower() not in gname.lower():
                        display = f"{gname} - {label_clean}" if gname else label_clean
                    out.append({"name": display or label_clean or gname or None,
                                "group": gname, "label": label_clean,
                                "sq": sq, "is_auto": is_auto,
                                "code": it.get("c") or it.get("code") or "",
                                "pos_key": f"{sq}|{gname}|{label or idx}"})
    return out

def _slepok_interest_for_cat(slepok: str, site_type: str, tp: str, cat: str | None) -> list:
    """interest_ids слепка для категории структурной группы (если совпала с категорией аудиторий
    direct_slepok_audiences). Нет совпадения → [] (create_set фолбэкнет на объединённый список)."""
    if not cat:
        return []
    low = cat.strip().lower()
    for c in _slepok_audience_cats(slepok, site_type, tp):
        if (c.get("category") or "").strip().lower() == low:
            return c.get("interest_ids") or []
    return []

def _slepok_interest_for_struct(slepok: str, site_type: str, tp: str, g: dict) -> tuple[list[str], str]:
    """Аудитории строго по текущему слепку; no cross-slepok/global merge."""
    cats = _slepok_audience_cats(slepok, site_type, tp)
    by_cat = {_norm_slepok_audience_category(c.get("category")): c.get("interest_ids") or [] for c in cats}
    for cand in _tp67_audience_category_candidates(g):
        ids = by_cat.get(cand)
        if ids:
            return ids, cand
    merged = sorted({str(x) for ids in by_cat.values() for x in ids if str(x).strip()})
    return merged, "fallback" if merged else "none"

def _tp67_kw_position_key(text: str | None) -> str:
    """Нормализованный ключ позиции для fallback-библиотеки реальных UAC keywords."""
    s = re.sub(r"\[[^\]]*\]", " ", str(text or "").replace("\xa0", " "))
    s = re.sub(r"\b(мк|тк|ключевики|кс)\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(автотаргетинг|автоматическая)\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"[·—–_-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return "общие запросы" if s in ("общая", "общие", "общие запросы") else s

def _tp67_real_keyword_items() -> list[dict]:
    try:
        return _json("tp67_real_keywords.json").get("items") or []
    except Exception:  # noqa: BLE001
        return []

def _tp67_keywords_from_real_library(slepok: str, site_type: str, tp: str, ct: str,
                                     city: str, position_name: str | None,
                                     sq: str | None = None) -> tuple[list[str], list[str]]:
    """Fallback: реальные keywords из cookie-payload UAC, когда M3-пак пустой.

    Приоритет точный: слепок + ст + tp + sq + ct/позиция. Разные позиции ct0000
    (Автосалон/Дилер/Общие запросы) не схлопываем, потому что в реальных аккаунтах
    у них разные keyword lists.
    """
    skey = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    pos_key = _tp67_kw_position_key(position_name)
    ct_key = (ct or "").strip().lower()
    sq_key = (sq or "").strip().lower()
    items = _tp67_real_keyword_items()

    def _score(it: dict) -> tuple[int, int, int, int, int, int] | None:
        if it.get("tp") != tp:
            return None
        same_slepok = 1 if it.get("slepok") == skey else 0
        site_score = 1 if (not site_type or it.get("site_type") == site_type) else 0
        sq_score = 1 if (not sq_key or it.get("sq") == sq_key) else 0
        ct_score = 1 if (ct_key and it.get("ct") == ct_key) else 0
        pos_score = 1 if (pos_key and it.get("position") == pos_key) else 0
        if not (ct_score or pos_score):
            return None
        # Приоритет: тот же слепок/site/sq, затем позиция, затем ct.
        # Если точного слепка нет в partial live-reference, берём лучший реальный набор
        # по той же позиции/ct из другого слепка вместо падения "КС без ключей".
        return (same_slepok, site_score, sq_score, pos_score, ct_score, len(it.get("keywords") or []))

    best = None
    best_score = None
    for it in items:
        sc = _score(it)
        if sc is not None and (best_score is None or sc > best_score):
            best = it
            best_score = sc
    if not best:
        return [], []
    pos = _kw_clean(_drop_used_car(_drop_foreign_city_keywords(best.get("keywords") or [], city), site_type), 200)
    neg = _kw_clean(best.get("minus") or [], 100)
    return pos, neg

def _tp67_keywords_for(slepok: str, site_type: str, tp: str, ct: str, city: str,
                       position_name: str | None = None, sq: str | None = None) -> tuple[list[str], list[str]]:
    """Ключи из M3-пака текущего слепка; если M3 пустой — fallback из реальных UAC payload по кукам."""
    skey = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    ct_key = ct or "ct0000"

    def _pack_keywords(tp_key: str) -> tuple[list[str], list[str]]:
        kw = kp.read_keywords(site_type, tp_key, ct_key, skey)
        pos = _kw_clean(_drop_used_car(_drop_foreign_city_keywords(kw.get("positive") or [], city), site_type), 200)
        neg = _kw_clean(kw.get("minus") or [], 100)
        return pos, neg

    pos, neg = _pack_keywords(tp)
    if pos:
        return pos, neg

    pos, neg = _tp67_keywords_from_real_library(slepok, site_type, tp, ct_key, city, position_name, sq)
    if pos:
        return pos, neg

    # tp7 «Товарка» по интенту близка к tp6 «Мастер кампаний»: если отдельный tp7-пул пуст,
    # берём ключи tp6 по тому же ct/позиции, чтобы не терять кампанию.
    if tp == "tp7":
        pos, neg = _pack_keywords("tp6")
        if pos:
            return pos, neg
        return _tp67_keywords_from_real_library(slepok, site_type, "tp6", ct_key, city, position_name, sq)

    return [], []

# Сверено LIVE grid 2026-06-21: Щербакова tp1 = товарные всегда; Павлов/Крючкова (wide=Модели) tp1 = нет.
# tp5 («Поиск + Динамика + Товарная Галерея») — товарные у ВСЕХ слепков (это его суть).
_SHOPPING_RULE = {"tp1": {"scherbakova"}, "tp5": {"scherbakova", "kryuchkova", "pavlov", "karavaev"}}

def _slepok_uses_shopping(slepok: str, tp: str) -> bool:
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    return key in _SHOPPING_RULE.get(tp, set())

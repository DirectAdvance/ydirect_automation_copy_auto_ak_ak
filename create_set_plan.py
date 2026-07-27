"""Create-set plan/name service extracted from blueprint.py."""

from __future__ import annotations

import logging
import re

from flask import jsonify, request

from . import create_set_context as _csctx  # _parse_targeting_modes (чистый хелпер, без configure)
from .model_urls import _strip_site_domain_label as _strip_dom_plan

_DEPS: dict = {}


def configure(deps: dict) -> None:
    """Bind blueprint dependencies used by the extracted planning helpers."""
    _DEPS.clear()
    _DEPS.update(deps)
    globals().update(deps)


def _resolve_region(city: str | None):
    """город → (r_code, область словами). Не нашлось → ('r0000', область|'Россия').

    Мультигородская строка (city с запятой — комбо-аккаунты вроде cardealer-rus.ru, 10 аккаунтов
    с одной и той же строкой из 6 городов) резолвится ПО-ДРУГОМУ: каждый город → своя область,
    множество областей ищется в `local_gsheet_naming` (type='ag_part4') среди готовых комбо-кодов
    (r0131/r0134/r0135 — уже существующий механизм для наборов из нескольких областей), матч —
    ТОЧНОЕ совпадение множества (без него было: одиночный exact-match всей строки целиком не
    находил ничего → всегда r0000/«Россия», живой баг 2026-07-06, porg-lzjk6p5m и 9 других)."""
    if not city or not city.strip():
        return "r0000", "Россия"
    cities = [c.strip() for c in city.split(",") if c.strip()]
    conn = _victory_conn()
    try:
        cur = conn.cursor()
        if len(cities) > 1:
            oblasts: set[str] = set()
            for c in cities:
                cur.execute('SELECT "Область" FROM public.local_gsheet_yandex_direct_id_location '
                            "WHERE \"GeoRegionType\"='City' AND lower(btrim(location))=lower(btrim(%s)) LIMIT 1", (c,))
                row = cur.fetchone()
                if row and row[0]:
                    oblasts.add(row[0].strip().lower())
            if oblasts:
                cur.execute("SELECT code, name FROM public.local_gsheet_naming "
                            "WHERE type='ag_part4' AND name LIKE '%,%'")
                for code, name in cur.fetchall() or []:
                    combo = {p.strip().lower() for p in (name or "").split(",") if p.strip()}
                    if combo == oblasts:
                        return code, name
            return "r0000", "Россия"
        cur.execute('SELECT "Область" FROM public.local_gsheet_yandex_direct_id_location '
                    "WHERE \"GeoRegionType\"='City' AND lower(btrim(location))=lower(btrim(%s)) LIMIT 1", (city,))
        row = cur.fetchone()
        oblast = row[0] if row else None
        if not oblast:
            return "r0000", "Россия"
        cur.execute("SELECT code FROM public.local_gsheet_naming WHERE type='ag_part4' "
                    "AND lower(btrim(name))=lower(btrim(%s)) LIMIT 1", (oblast,))
        r = cur.fetchone()
        return (r[0] if r else "r0000"), oblast
    finally:
        conn.close()

# Плейсхолдер города в именах позиций slepki_structure.json (город-агностичный шаблон слепка).
# Подставляется городом аккаунта в _emit_struct (tp6/tp7). Токен согласован с Семёном 2026-07-16.
_CITY_PLACEHOLDER = "ГОРОД"
_TP1_TARGET_TAIL_RE = re.compile(
    r"\s+-\s*(?:"
    r"КС\s*\+\s*Автотаргетинг|Автотаргетинг\s*\+\s*КС|"
    r"КС|Ключевики|Ключевые\s+слова|Автотаргетинг|Автотаргет"
    r")\s*$",
    re.IGNORECASE,
)


def _strip_tp1_target_tail(name: str) -> str:
    """Remove an already harvested targeting suffix before adding x3 variants."""
    out = str(name or "").strip()
    while out:
        nxt = _TP1_TARGET_TAIL_RE.sub("", out).strip()
        if nxt == out:
            return out
        out = nxt
    return out

def _build_name(is_master: bool, is_autotarget: bool, pay: str, r_code: str, oblast: str,
                sq: str = "site", cat: str | None = None, ct: str = "ct0000",
                targeting_label: str | None = None) -> str:
    """Имя кампании по спеке: {коды} — {МК|ТК}_{AT|RA}_{pay}[_kviz][ - {категория}] - {область}.
    sq: 'site' (посадка = домен) | 'kviz' (посадка = домен/quiz).
    cat: категория/модель группы (Haval Jolion/Интересы/…) — отдельная кампания на неё.
    ct: 1-й код кодера. Для tp6 ПО МОДЕЛИ — ct модели (ct0119 для Haval Jolion), иначе ct0000.
        Это даёт «контент по кодеру»: движок видит модель в ct и берёт её картинку+заголовки."""
    tp = "tp6" if is_master else "tp7"
    paycode = "cpc" if pay == "tcpa" else "cpa"          # сегмент оплаты в кодах
    sqcode = "kviz" if sq == "kviz" else "site"          # ось посадки в кодах
    # Формат (ag_part5): tp6 МК → ct001 (ТГО). tp7 Товарка = Каталог+ТГО+Фид (комбинированное:
    # ListingAd по каталогу + ShoppingAd по фиду + товарное ТГО) → ct010, НЕ ct009 (ct009 = товарное
    # БЕЗ ТГО). Правило пользователя: tp7 нейминг = ct010.
    fmt = "ct001" if is_master else "ct010"              # формат: ТГО / Каталог+ТГО+Фид
    # Возраст 25+ (ag011; socdem age_lower=age_25, решение Семёна 2026-07-21) — tp6/Мастер в РУЧНЫХ режимах
    # (keywords И audience), кроме
    # автотаргетинга (там полный socdem age_18/ag001 по дизайну, #7 в create_set_master_product.py)
    # и товарки tp7 (возраст не настраивается → всегда «Все»). is_autotarget=True ТОЛЬКО для
    # targeting_mode=='autotarget' — раньше сюда приходил is_auto=(targeting_mode!='keywords'),
    # из-за чего audience тоже попадал в ag001 (живой баг 2026-07-06, porg-lzjk6p5m/terehov) —
    # рассинхрон с age_lower в create_set_master_product.py, который чинился той же логикой.
    age = "ag001" if (not is_master or is_autotarget) else "ag011"
    codes = f"{tp}_{paycode}_{sqcode}_{ct or 'ct0000'}_aon_n000_{r_code}_{fmt}_{age}_g00"
    tp_label = "МК" if is_master else "ТК"  # #6: канон CODER.md (было «Мастер кампаний»/«Товарка», 2026-07-19)
    cat_part = f" - {cat}" if cat else ""                 # категория аудитории в человекочитаемое имя (как в слепках)
    tgt_part = f" - {targeting_label}" if targeting_label else ""
    # Дедуп сегментов — ОБЩИЙ механизм (_csctx.dedup_name_segments), не частное условие на
    # литерал. cat = item.t после tp67_clean (структурная метка, targeting-хвост срезан).
    # Сегменты всё равно могут пересекаться с tp_label/targeting_label («ТК», «Автотаргетинг»),
    # поэтому дедуп обязателен для всех cat-значений.
    return _csctx.dedup_name_segments(f"{codes} — {tp_label}{cat_part}{tgt_part} - {oblast}")

def _rule_sets(site_type: str, city: str) -> dict:
    """Наборы бюджет/CPA из direct_automation_rules по (site_type, city)→'*':
    {'cpa','budget'} — оплата за конверсии (CPA), {'cpc_cpa','cpc_budget'} — оплата за клики (CPC).
    Дефолт 2000/5000. cpc_* фолбэчат на cpa/budget, если NULL."""
    d = {"cpa": 2000, "budget": 5000, "cpc_cpa": 2000, "cpc_budget": 5000}
    st = (site_type or "").strip()
    if not st:
        return d
    sql = ("SELECT cpa::numeric, budget::numeric, cpc_cpa::numeric, cpc_budget::numeric "
           "FROM public.direct_automation_rules WHERE site_type=%s AND city=%s LIMIT 1")
    try:
        conn = _victory_conn()
        try:
            cur = conn.cursor()
            r = None
            if city and city != "*":
                cur.execute(sql, (st, city))
                r = cur.fetchone()
            if not r:
                cur.execute(sql, (st, "*"))
                r = cur.fetchone()
            if r:
                d["cpa"] = int(float(r[0])); d["budget"] = int(float(r[1]))
                d["cpc_cpa"] = int(float(r[2])) if r[2] is not None else d["cpa"]
                d["cpc_budget"] = int(float(r[3])) if r[3] is not None else d["budget"]
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — таблица/колонки могут отсутствовать в dev-окружении
        pass
    return d

def _resolve_struct_site_type(slepok: str, requested_site_type: str) -> str:
    """Резолвит тип сайта для чтения СТРУКТУРЫ/КОНТЕНТ-ПАКА слепка.

    Нормальный путь: requested_site_type есть в структуре → возвращает его 1:1.
    Фолбэк: нет → берёт первый доступный site_type слепка с контентом (tp или
    source_campaigns), возвращает его имя.
    Нет ни одного → возвращает requested_site_type (поведение прежнее: пусто).

    ВАЖНО: применять ТОЛЬКО для чтения структуры/пака. НЕ применять к _rule_sets,
    бюджету, CPA, региону, городу — там нужен именно запрошенный тип сайта из формы."""
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    struct = _json("slepki_structure.json").get("directologists", [])
    d = next((x for x in struct if x.get("key") == key), None)
    if not d:
        return requested_site_type
    site_types = d.get("site_types") or []
    # Нормальный путь: запрошенный тип найден → возвращаем как есть
    if any(s.get("name") == requested_site_type for s in site_types):
        return requested_site_type
    # Фолбэк: первый тип с реальным контентом (tp или source_campaigns)
    for s in site_types:
        if (s.get("tp") and len(s["tp"]) > 0) or (
                s.get("source_campaigns") and len(s["source_campaigns"]) > 0):
            return s.get("name") or requested_site_type
    # Вообще нет доступного типа → прежнее поведение (пусто)
    return requested_site_type


def _tp_plan_names(slepok: str, site_type: str, tp_code: str) -> list[dict]:
    """Позиции tp из структуры слепка — одна запись на каждый item (per-кампания).

    Канон CODER.md: каждая позиция (item) = отдельная кампания. Используется для item-level
    tp, где кампании дробятся по таргетингу/марке внутри одной группы:
      tp1 (РСЯ по моделям/марке), tp4 (Поиск+Динамика по маркам/темам).
    item.t — полное имя таргетинга («РСЯ BAIC BJ40», «Поиск+Динамика Haval марка», …).
    Имя кампании строится в api_set_plan: tp{N}_cpc_site — {item.t}.

    Возвращает [{"label": item.t, "gc": item.gc}, …] или [] если нет данных.
    Дедуп по label (item.t) — на случай дублей в структуре."""
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    struct = _json("slepki_structure.json").get("directologists", [])
    d = next((x for x in struct if x.get("key") == key), None)
    if not d:
        return []
    _st_key = _resolve_struct_site_type(slepok, site_type)
    st = next((s for s in d.get("site_types", []) if s.get("name") == _st_key), None)
    if not st:
        return []
    result: list[dict] = []
    seen: set = set()
    for tp in st.get("tp", []):
        if tp.get("code") != tp_code:
            continue
        blocks = tp.get("splits") or [{"groups": tp.get("groups", [])}]
        for sp in blocks:
            for grp in sp.get("groups", []):
                for item in grp.get("items", []):
                    if not isinstance(item, dict):
                        continue
                    label = (item.get("t") or "").strip()
                    if not label or label in seen:
                        continue
                    seen.add(label)
                    result.append({"label": label, "gc": item.get("gc", "")})
    return result

def _tp1_plan_names(slepok: str, site_type: str, r_code: str) -> list[dict]:
    """Обёртка совместимости: позиции tp1 (см. _tp_plan_names)."""
    return _tp_plan_names(slepok, site_type, "tp1")


# site_type'ы с АВТОМОБИЛЬНОЙ сегментной классификацией (Марки/Модели/Общее по справочнику
# ag_part1). Слепки НЕ из этого списка (напр. «dmp» / «Прочее») — split-driven: их ct нет в
# _ct_segment_map, поэтому _ct_segment() вырождается в дефолт «Марки» для ВСЕХ групп и склеивает
# их в одну кампанию. Для таких — билдер идёт по splits[] (одна кампания на блок, имя = label).
_AUTO_SEGMENT_SITE_TYPES = {"Мультибренд", "Монобренд", "С пробегом", "Мульти + БУ", "Квиз"}


def _tp_seg_name_override(slepok: str, site_type: str, tp_code: str, seg: str, mode: str) -> str | None:
    """Переопределённый label кампании (часть после «—», без области) для (слепок, тип, tp, сегмент, режим).

    Читает из tp.name_overrides['{seg}|{mode}'] или tp.name_overrides[seg] в slepki_structure.json.
    mode = 'КС' или 'Автотаргет' (как приходит из _slepok_tp_modes). Возвращает None если нет override."""
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    struct = _json("slepki_structure.json").get("directologists", [])
    d = next((x for x in struct if x.get("key") == key), None)
    if not d:
        return None
    _st_key = _resolve_struct_site_type(slepok, site_type)
    st = next((s for s in d.get("site_types", []) if s.get("name") == _st_key), None)
    if not st:
        return None
    tp = next((t for t in st.get("tp", []) if t.get("code") == tp_code), None)
    if not tp:
        return None
    overrides = tp.get("name_overrides") or {}
    return overrides.get(f"{seg}|{mode}") or overrides.get(seg) or None


def _tp_seg_modes(slepok: str, site_type: str, tp_code: str, seg: str) -> list | None:
    """Режимы таргетинга сегмента ИЗ СТРУКТУРЫ слепка (tp.seg_modes[seg]) — приоритетный источник.

    Для гибридных слепков задаёт разведение на 3 варианта: {'Автотаргет','КС','КС+Автотаргет'}
    (полный автотаргет / только ключи / ключи + автотаргет одной кампанией).
    None → в структуре не задано (или пусто/битое) → caller делает fallback на боевой профиль
    _slepok_tp_modes (не-гибридные слепки НЕ меняются). Порядок режимов — как в структуре.
    Читается аналогично _tp_seg_name_override (тот же tp-узел slepki_structure.json)."""
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    struct = _json("slepki_structure.json").get("directologists", [])
    d = next((x for x in struct if x.get("key") == key), None)
    if not d:
        return None
    _st_key = _resolve_struct_site_type(slepok, site_type)
    st = next((s for s in d.get("site_types", []) if s.get("name") == _st_key), None)
    if not st:
        return None
    tp = next((t for t in st.get("tp", []) if t.get("code") == tp_code), None)
    if not tp:
        return None
    sm = tp.get("seg_modes") or {}
    raw = sm.get(seg)
    if not isinstance(raw, list):
        return None
    valid = [m for m in raw if m in ("Автотаргет", "КС", "КС+Автотаргет")]
    return valid or None


def _tp_splits(slepok: str, site_type: str, tp_code: str) -> list[dict] | None:
    """splits[] tp-блока слепка (каждый: {sq,label,groups}) или None (нет splits / нет tp).

    Отличается от _tp_plan_names: сохраняет ДЕЛЕНИЕ на блоки (label/sq/groups), не разворачивает
    в плоский список items. Нужно split-driven слепкам (dmp): 1 tp.splits[]-блок = 1 кампания."""
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    struct = _json("slepki_structure.json").get("directologists", [])
    d = next((x for x in struct if x.get("key") == key), None)
    if not d:
        return None
    _st_key = _resolve_struct_site_type(slepok, site_type)
    st = next((s for s in d.get("site_types", []) if s.get("name") == _st_key), None)
    if not st:
        return None
    tp = next((t for t in st.get("tp", []) if t.get("code") == tp_code), None)
    if not tp:
        return None
    return tp.get("splits") or None


def _source_campaign_manifest(slepok: str, site_type: str) -> dict | None:
    """Authoritative archive-derived campaign layout, when a slepok declares one.

    The ordinary ``tp/groups/items`` structure remains available as the coder/content
    foundation.  A source manifest is a higher-level description of the real campaigns
    and must not be collapsed back into generic segment campaigns.
    """
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    directologist = next((item for item in (_json("slepki_structure.json").get("directologists") or [])
                          if item.get("key") == key), None)
    manifest_name = (directologist or {}).get("source_manifest")
    if not manifest_name:
        return None
    manifest = _json(manifest_name)
    _st_key = _resolve_struct_site_type(slepok, site_type)
    if manifest.get("slepok") != key or manifest.get("site_type") != _st_key:
        return None
    return manifest

def _feed_role_of(feed: dict) -> str:
    """Роль фида по нормализованному ключу: catalog (модельные листинги) / landing (лендинг/оффер).
    Использует инжектированные `_feed_key` и `_CATALOG_FEED_KEYS` (см. automation_runtime._create_set_plan_deps)."""
    key = _feed_key((feed or {}).get("url") or (feed or {}).get("name") or "")
    return "catalog" if key in _CATALOG_FEED_KEYS else "landing"

def _explicit_feed_subset(g: dict, feeds: list[dict]):
    """Явно заданный в структуре tp7-позиции фид → подмножество `feeds`.

    Возвращает:
      • None — фид в позиции НЕ задан (feed_role/feed_id/feed_key пусты) → безопасный дефолт: fan-out.
      • list  — совпавшие фиды (по id → по ключу/имени → по роли catalog/landing). Может быть пустым,
                если явно указанного фида НЕТ среди разрешённых фидов аккаунта (тогда позиция пропускается,
                чтобы НЕ создавать товарку на нерелевантном фиде)."""
    fid = g.get("feed_id")
    fkey = (g.get("feed_key") or "").strip()
    frole = (g.get("feed_role") or "").strip().lower()
    if not (fid or fkey or frole):
        return None
    if fid:
        try:
            fid_i = int(fid)
        except (TypeError, ValueError):
            fid_i = 0
        return [f for f in feeds if int((f or {}).get("id") or 0) == fid_i] if fid_i else []
    if fkey:
        want = _feed_key(fkey)
        return [f for f in feeds
                if _feed_key((f or {}).get("url") or (f or {}).get("name") or "") == want]
    return [f for f in feeds if _feed_role_of(f) == frole]

_KW_NAME_RE = re.compile(r"(^|[^а-яё])кс([^а-яё]|$)")


def _txt_targeting_mode(name: str, tgt: str) -> tuple[bool, bool]:
    """(autotarget, keep_keywords) для tp2/tp4/tp5 из ИМЕНИ camp_names → gc fallback."""
    low = (name or "").lower()
    has_at = "автотаргет" in low
    has_kw = bool(_KW_NAME_RE.search(low)) or "ключев" in low
    if has_at and has_kw:
        return True, True
    if has_at:
        return True, False
    if has_kw:
        return False, True
    return ((True, False) if str(tgt) == "автотаргетинг" else (False, True))


def _txt_autotarget(name: str, tgt: str) -> bool:
    """Back-compat bool view for callers that do not need keep_keywords."""
    return _txt_targeting_mode(name, tgt)[0]


def _metrika_alert_for(login: str, body: dict) -> dict:
    """Счётчик/цель Метрики на шаге ПЛАНА — тем же `prepare_metrika`, что и создание.

    Раньше единственная валидация стояла в `create_set_orchestrator` (шаг СОЗДАНИЯ), и о
    недостающей метрике пользователь узнавал только после нажатия «Создать». Вторую проверку
    рядом НЕ пишем — зовём ту же функцию, иначе логика разъедется.

    → {needed, error, counter_id, goal_id, metrika_note}. План НЕ блокируется (ответ всегда 200):
    `needed=True` — сигнал фронту показать плашку и погасить кнопку «Создать».
    Легальные случаи `prepare_metrika` сохраняются как есть:
      • via_cookie+no_cpa → optional: `ok=True` + `metrika_note`, алерт НЕ поднимаем;
      • counter без goal → цель доподтягивает `goal_vse_formy`; алерт только если и после этого нет.
    Зависимости берём из globals() (их кладёт туда `configure`, тесты — monkeypatch'ем):
    проводки нет → тихо не блокируем, гейт создания в оркестраторе остаётся на месте.
    """
    empty = {"needed": False, "error": None, "counter_id": 0, "goal_id": 0, "metrika_note": None}
    g = globals()
    goals_for = g.get("_metrika_goals_for")
    goal_vse_formy = g.get("_goal_vse_formy")
    foreign_owner = g.get("_counter_foreign_owner")
    if not (callable(goals_for) and callable(goal_vse_formy) and callable(foreign_owner)):
        return empty

    def _int(val) -> int:
        try:
            return int(str(val if val is not None else "").strip() or 0)
        except Exception:  # noqa: BLE001
            return 0
    from .create_set_metrika import prepare_metrika
    try:
        res = prepare_metrika(
            login=login,
            counter_id=_int(body.get("counter_id")),
            goal_id=_int(body.get("goal_id")),
            via_cookie=bool(body.get("via_cookie")),
            no_cpa=bool(body.get("no_cpa") or body.get("n")),
            metrika_goals_for=goals_for,
            goal_vse_formy=goal_vse_formy,
            counter_foreign_owner=foreign_owner,
        )
    except Exception:  # noqa: BLE001 — план обязан посчитаться даже при сбое Метрики/БД
        # Fail-open молча = «плашки нет» неотличимо от «метрика в порядке»: разовый сбой Victory
        # и постоянный выглядят одинаково. Логируем с трейсом и логином, поведение не меняем.
        logging.getLogger("direct.plan").warning(
            "metrika_alert: prepare_metrika упал для login=%s — план отдаём без алерта",
            login, exc_info=True)
        return empty
    return {"needed": not bool(res.get("ok")), "error": res.get("error"),
            "counter_id": _int(res.get("counter_id")), "goal_id": _int(res.get("goal_id")),
            "metrika_note": res.get("metrika_note")}


def _set_plan_response():
    """План набора (предпросмотр, БЕЗ создания): какие кампании и с какими именами создадутся."""
    import psycopg2.extras
    body = request.json or {}
    login = (body.get("login") or "").strip()
    agent = (body.get("agent") or "").strip()            # ключ слепка-директолога (для аудиторий tp6/tp7, структуры tp1)
    agent_group = (body.get("agent_group") or "").strip()  # пакет UI (posevy → tp8/9/10)
    variants = body.get("variants") or []                # master_auto/master_manual/product_auto/product_manual
    tp_sq = body.get("tp_sq") or {}                       # {"6":["site","kviz"], "7":["site"]} — оси посадки из набора
    # selected_pos: {tp_num_str: {labels:[...], groups:[...]}} — пер-позиционный выбор с фронта.
    # Если пришёл — фильтруем план по нему. Не пришёл — поведение прежнее (все позиции).
    selected_pos: dict = body.get("selected_pos") or {}
    # #4 (Семён 2026-07-12): галочка «под стиль сайта» (n) управляет cpc/cpa для НЕ-авто (dmp):
    # стоит (n=False) → только cpc; снята (n=True) → cpc + cpa (×2). Заменяет копирование split.pay.
    no_cpa = bool(body.get("n"))
    def _sel_labels(tp_num: int) -> set | None:
        """Выбранные label'ы для tp (tp1). None = нет ограничений."""
        sp = selected_pos.get(str(tp_num)) or selected_pos.get(tp_num)
        if sp is None:
            return None
        labs = sp.get("labels") or []
        return set(labs) if labs else None
    def _sel_groups(tp_num: int) -> set | None:
        """Выбранные группы для tp (tp2/5/6/7). None = нет ограничений."""
        sp = selected_pos.get(str(tp_num)) or selected_pos.get(tp_num)
        if sp is None:
            return None
        grps = sp.get("groups") or []
        return set(grps) if grps else None
    def _sq_for(tp_num: str) -> list:                     # какие посадки (site/kviz) создавать для tp
        v = tp_sq.get(tp_num) or tp_sq.get(f"tp{tp_num}")
        return [s for s in (v or []) if s in ("site", "kviz")] or ["site"]
    if not login:
        return jsonify({"error": "login обязателен"}), 400
    # Метрика проверяется ЗДЕСЬ, на шаге плана (а не только при создании) — пользователь узнаёт
    # о недостающем счётчике/цели сразу. План при этом считается и отдаётся всегда (200).
    metrika_alert = _metrika_alert_for(login, body)
    ov_site = (body.get("site_type") or "").strip()      # ручной override типа сайта (правится в форме)
    ov_city = (body.get("city") or "").strip()           # ручной override города
    ov_domain = (body.get("domain") or "").strip()       # ручной override домена (для новых аккаунтов без БД-записи)

    conn = _victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT city, site_type, agency_account, domain FROM public.local_gsheet_sites "
                    "WHERE login_key=%s AND direction='Авто' LIMIT 1", (login,))
        row = cur.fetchone()
    finally:
        conn.close()
    _row_missing = not row
    if _row_missing:
        # Мягкая деградация: аккаунт ещё не занесён в local_gsheet_sites →
        # продолжаем план на значениях из формы (site_type/city/domain).
        row = {"domain": ov_domain}                      # домен из формы доступен замыканию _emit_struct

    site_type = ov_site or (row.get("site_type") or "").strip()   # override приоритетнее БД (правка ошибки в БД)
    city = ov_city or (row.get("city") or "")
    r_code, oblast = _resolve_region(city)
    # Наборы бюджет/CPA из «Глобальных правил». pay=cpa → CPA-набор (оплата за конверсии),
    # pay=tcpa → CPC-набор (оплата за клики). НЕ из формы.
    rs = _rule_sets(site_type, city)
    cpa, budget = rs["cpa"], rs["budget"]                # для resolved (read-only справка в форме)

    def _bud(pay):                                       # бюджет недели по типу оплаты
        return rs["budget"] if pay == "cpa" else rs["cpc_budget"]

    def _cpa_for(pay):                                   # целевой CPA по типу оплаты
        return rs["cpa"] if pay == "cpa" else rs["cpc_cpa"]
    warnings: list[str] = []
    if _row_missing:
        warnings.append("аккаунт не найден в local_gsheet_sites — используются значения из формы")
    if r_code == "r0000":
        warnings.append("регион не определён — r0000")
    # Фолбэк структуры: если у слепка нет site_type из формы — используем первый доступный.
    # Бюджет/CPA/регион/цель — по-прежнему по запрошенному site_type (из формы).
    struct_site_type = _resolve_struct_site_type(agent, site_type)
    posevy_struct_alias = (
        (agent_group == "posevy" or (agent or "").lower() == "posevy")
        and site_type == "Посевы"
        and struct_site_type == "Мультибренд"
    )
    if struct_site_type != site_type and not posevy_struct_alias:
        warnings.append(f"нет структуры для «{site_type}» — набор построен из «{struct_site_type}»")

    token, _ = _token_for_login(login, row.get("agency_account") or "", _direct_tokens())
    existing = set()
    if token:
        jc = _v5_get("campaigns", token, login, ["Name"], criteria={})
        existing = {(c.get("Name") or "") for c in (jc.get("result") or {}).get("Campaigns", [])}
        # v5 не видит черновики (State=OFF; UNIFIED/UAC-черновики v5 не отдаёт вовсе) →
        # дополняем именами из Grid (видит ВСЕ кампании, включая DRAFT и UAC). Иначе повторное
        # «Создать набор» по тому же аккаунту плодит дубли черновиков (П.4). Мягкая деградация при сбое куки.
        try:
            existing |= {(c.get("name") or "") for c in _grid_list_campaigns(login)}
        except Exception as e:  # noqa: BLE001
            warnings.append(f"Grid-список имён недоступен — дубли черновиков возможны: {str(e)[:80]}")
    else:
        warnings.append("нет агентского токена — проверка дублей имён недоступна")

    feeds = []
    _sf_fallback_id = 0                # фолбэк-фид для feed_alert (виден и когда product не выбран)
    _sf_fallback_name = ""             # имя реального фолбэка для кнопки в модалке
    if any(str(v).startswith("product") for v in variants):
        # tp7 (Товарка) размножается по фидам — но ТОЛЬКО по тем, что разрешены в «Глобальных
        # правилах» (тот же allow-list, что и tp1/tp5: _filter_allowed_feed_rows). Раньше фильтра
        # тут не было → tp7 плодил кампанию на КАЖДЫЙ фид аккаунта (вкл. неотмеченные). Фильтруем
        # СЫРЫЕ строки (у них есть name/Name → совпадает с feed_key глобальных правил), затем мапим.
        if token:
            # ⚠️ НЕ добавлять "Url" в FieldNames: v5 feeds.get его НЕ знает (валидны Id/Name/BusinessType/
            # SourceType/Source/UpdateStatus) → весь запрос падает «Некорректный запрос» → молчаливый
            # фолбэк на Grid. URL URL-фида лежит в nested Source; здесь он не нужен (single_feed /yandex.xml
            # резолвится через Grid ниже), поэтому url="" — его подхватит Grid-фолбэк.
            jf = _v5_get("feeds", token, login, ["Id", "Name", "SourceType"])
            _raw = [f for f in (jf.get("result") or {}).get("Feeds", []) if f.get("SourceType") == "URL"]
            feeds = [{"id": f["Id"], "name": f.get("Name"), "url": f.get("Url") or ""} for f in _filter_allowed_feed_rows(_raw)]
        if not feeds:
            # v5 пусто (часто 152 — нет баллов): фиды есть, но v5-чтение стоит баллов → читаем
            # список по КУКЕ через Grid (без баллов), иначе товарные не спланируются на исчерпанном аккаунте.
            try:
                _raw = _filter_allowed_feed_rows(_grid_feeds(login, row.get("agency_account") or ""))
                feeds = [{"id": int(f["id"]), "name": f.get("name"), "url": f.get("url") or ""} for f in _raw if f.get("id")]
            except Exception:  # noqa: BLE001
                feeds = []
        if not feeds:
            warnings.append("у аккаунта нет РАЗРЕШЁННЫХ фидов в «Глобальных правилах» — товарные не создадутся")
        # Галочка «профильные фиды» (single_feed): план строим по профильному набору
        # { yandex.xml, yandex-used-auto.xml } ∩ разрешённые в аккаунте.
        # Оба фида есть → tp7/tp5/tp1-товарка ×2 (fan-out); один → ×1; ни одного → feed_alert.
        # len(feeds) >= 1 (не >1): единственный ЧУЖОЙ фид тоже должен проходить strict-проверку.
        if bool(body.get("single_feed")) and len(feeds) >= 1:
            from .create_set_input import (PROFILE_FEED_KEYS, FALLBACK_SINGLE_FEED_KEY,
                                           prefer_single_feed_rows)
            # ⚠️ НЕ импортировать _first_url_feed из create_set_feeds напрямую: тот модуль требует
            # configure() (инъекции _filter_allowed_feed_rows и др.) → NameError на свежем процессе.
            # Берём инжектированную blueprint-обёртку из наших deps (globals().update(deps)).
            # _first_url_feed видит полные Grid-объекты с URL-полями — feeds здесь уже усечены до
            # {id, name} и prefer_single_feed_rows не находит совпадения по имени.
            # Подтверждение фолбэк-фида: UI шлёт feed_confirmed (кнопка «Продолжить с другим фидом»),
            # старый путь читал single_feed_fallback → рассинхрон. Принимаем ОБА ключа.
            _sf_fb_confirmed = bool(body.get("single_feed_fallback") or body.get("feed_confirmed"))
            # Collect IDs for ALL present profile feeds (strict per-key lookup via API+Grid).
            _profile_ids: list[int] = []
            _profile_keys: list[str] = []   # реально совпавшие ключи (для честного текста warning)
            for _pk in PROFILE_FEED_KEYS:
                _pid = _first_url_feed(token, login, row.get("agency_account") or "",
                                       strict=True, url_key=_pk)
                if _pid and _pid not in _profile_ids:
                    _profile_ids.append(_pid)
                    _profile_keys.append(_pk)
            if _profile_ids:
                # Profile feeds found → filter plan to those feeds only (fan-out по 1-2 фидам)
                _sf_list = [f for f in feeds if int(f.get("id") or 0) in _profile_ids]
                if _sf_list:
                    feeds = _sf_list
                    if not _sf_fb_confirmed:
                        warnings.append(
                            f"«профильные фиды»: план и создание — {len(feeds)} фид(а): "
                            + ", ".join(f"/{k}" for k in _profile_keys))
                else:
                    feeds = prefer_single_feed_rows(feeds)
                    warnings.append(
                        f"«профильные фиды»: целевые фиды (ids={_profile_ids}) "
                        f"не в allow-list — взяты первые доступные")
            else:
                # Ни одного профильного фида → ищем фолбэк (кнопка «Продолжить с другим фидом»)
                _sf_fallback_id = _first_url_feed(token, login, row.get("agency_account") or "",
                                                  strict=True, url_key=FALLBACK_SINGLE_FEED_KEY)
                if not _sf_fallback_id and feeds:
                    # канонического фолбэка тоже нет → предлагаем ПЕРВЫЙ разрешённый фид аккаунта
                    # (правило Семёна 03.07 #86: выбор второго фида должен быть всегда)
                    _sf_fallback_id = int(feeds[0].get("id") or 0)
                if _sf_fallback_id:
                    _sf_fallback_name = next((str(f.get("name") or "") for f in feeds
                                              if int(f.get("id") or 0) == _sf_fallback_id), "")
                if _sf_fallback_id and _sf_fb_confirmed:
                    # пользователь подтвердил фолбэк-фид (кнопка в feed_alert)
                    _sf_list = [f for f in feeds if int(f.get("id") or 0) == _sf_fallback_id]
                    feeds = _sf_list if _sf_list else prefer_single_feed_rows(feeds)
                    _sf_fallback_id = 0   # feed_alert не нужен (уже подтверждено)
                    warnings.append(
                        f"«профильные фиды»: профильных нет — по решению пользователя "
                        f"используется фолбэк-фид {FALLBACK_SINGLE_FEED_KEY}")
                else:
                    # strict-поиск не нашёл ни одного профильного фида → предупреждение.
                    # ⚠️ ИСТОРИЧЕСКИ здесь стояло `feeds = []`, что убивало tp7 из плана
                    # при транзиентном сбое strict-lookup (API/152). tp5/tp3 выживали, потому
                    # что их фид резолвится позже, на билде (create_set_feed_builders.py:917),
                    # через повторный _resolve_single_feed_variants.
                    # FIX (2026-07-22): tp7 тоже резолвим на билде — не хардкодим feeds=[]
                    # при неподтверждённом фоллбэке. Sentinel feed_id=0 сохраняет product-items
                    # в плане; build (create_set_master_product.py) выполняет повторный
                    # strict-lookup и пропускает tp7 если профильного фида нет и там тоже.
                    warnings.append(
                        f"⚠️ профильные фиды ({', '.join('/' + k for k in PROFILE_FEED_KEYS)}) "
                        f"не найдены в аккаунте на этапе плана — tp7 будет резолвиться на билде. "
                        f"Если профильный фид не найдётся и на билде, tp7 пропустится. "
                        f"Подтвердите фолбэк-фид для использования нестандартного фида.")
                    # sentinel: единственная запись с id=0 сигнализирует билду о deferred-резолве.
                    # _emit_struct создаёт по 1 plan-item на позицию (без fan-out) без feed-метки.
                    feeds = [{"id": 0, "name": None, "url": ""}]

    used: set = set()

    def _uniq(name: str):
        """Уникализация имени: занято (в аккаунте или в наборе) → +_v01…_v99.

        ЕДИНАЯ воронка ПЛАНОВЫХ имён всех tp (tp1–tp7) — здесь же применяем общий дедуп
        сегментов (`_csctx.dedup_name_segments`), уже ПОСЛЕ приклейки области. Идемпотентно:
        `_build_name` уже вернул каноничное имя, повторный вызов строку не меняет.

        ⚠️ Метка ФИДА в план НЕ входит: для tp3/tp5 она приклеивается позже, на билде
        (`create_set_feed_builders._create_tp5_campaign`/`_create_tp3_campaign` + cookie-путь),
        поэтому живое имя tp3/tp5 ≠ плановое. Дедуп там применён ОТДЕЛЬНО, на той же функции —
        не полагаться на то, что `_uniq` покрыл финальное имя товарных.
        """
        name = _csctx.dedup_name_segments(name)
        if name not in existing and name not in used:
            used.add(name)
            return name, False
        # Развести два РАЗНЫХ конфликта (ERRORS_JOURNAL: UNIQ_EXISTING_COLLISION_MINTS_DUP):
        # (б) имя занято ТОЛЬКО живой кампанией кабинета (existing), но НЕ второй позицией этого
        #     плана (used) → это RESUME-SKIP, а не новая позиция. Вернуть имя НЕТРОНУТЫМ, чтобы
        #     `already_in_direct(base, {base})` дал точный матч и штатно пропустил. Минтить `_v01`
        #     тут = плодить дубль `base_v01` поверх живого `base` (инцидент porg-ozge4ntu, 11 шт.).
        if name not in used:
            used.add(name)
            return name, False
        # (а) имя занято ДРУГОЙ позицией ТЕКУЩЕГО плана (used) → две разные позиции на одно базовое
        #     имя → легитимно минтим `_v01`/`_v02` (в т.ч. поверх занятого именем в кабинете).
        for v in range(1, 100):
            cand = f"{name}_v{v:02d}"
            if cand not in existing and cand not in used:
                used.add(cand)
                return cand, True
        used.add(name)
        return name, True

    def _stamp_plan_context(items: list[dict]) -> list[dict]:
        for item in items:
            if not isinstance(item, dict):
                continue
            item["_plan_agent"] = agent
            item["_plan_site_type"] = site_type
            item["_plan_struct_site_type"] = struct_site_type
            item["_plan_login"] = login
        return items

    def _drop_server_versioned(items: list[dict]) -> list[dict]:
        kept: list[dict] = []
        by_name: dict[str, dict] = {}
        merged: list[str] = []
        dropped: list[str] = []

        def _base_name(name: str) -> str:
            return re.sub(r"_v\d{2}(?:\b|$)", "", str(name or "")).strip()

        def _merge_unique(dst: dict, src: dict, key: str) -> None:
            vals = list(dst.get(key) or [])
            seen_vals = {str(v) for v in vals}
            for val in (src.get(key) or []):
                sval = str(val)
                if sval and sval not in seen_vals:
                    vals.append(val)
                    seen_vals.add(sval)
            if vals:
                dst[key] = vals

        def _compatible(dst: dict, src: dict) -> bool:
            for key in ("type", "variant", "pay", "feed_id", "feed_name"):
                if (dst.get(key) or None) != (src.get(key) or None):
                    return False
            return True

        def _merge_into(dst: dict, src: dict) -> None:
            for key in ("tp1_only_gks", "tp1_only_cts", "only_gks", "only_cts"):
                _merge_unique(dst, src, key)
            for key in ("tp1_all_feeds", "tp1_catalog", "products_only", "autotarget",
                        "autotarget_keep_keywords"):
                if src.get(key):
                    dst[key] = src.get(key)
            dst["renamed"] = False

        for item in items:
            name = str((item or {}).get("name") or "")
            if (item or {}).get("renamed") or re.search(r"_v\d{2}(?:\b|$)", name):
                base = _base_name(name)
                target = by_name.get(base)
                if target is not None and _compatible(target, item or {}):
                    _merge_into(target, item or {})
                    merged.append(name or str((item or {}).get("t") or "?"))
                else:
                    dropped.append(name or str((item or {}).get("t") or "?"))
                continue
            kept.append(item)
            by_name[name] = item
        if merged:
            warnings.append(
                "⚠️ схлопнуты серверные дубли _vNN в базовые кампании: "
                + "; ".join(merged[:8])
                + (f"; … ещё {len(merged) - 8}" if len(merged) > 8 else "")
            )
        if dropped:
            warnings.append(
                "⚠️ убраны кампании с серверными версиями _vNN: "
                + "; ".join(dropped[:8])
                + (f"; … ещё {len(dropped) - 8}" if len(dropped) > 8 else "")
                + ". Исправьте коллизии имён в структуре слепка — версии при создании запрещены."
            )
        return kept

    # gen_ses and future archive-backed slepki: preview the REAL campaign split from the
    # manifest.  The legacy tp tree is still useful for ct/gc mapping, but must not turn the
    # six source campaigns into generic "Марки/Модели/Общее" pairs.  These rows are
    # deliberately pre-draft-only until the source-aware builders are enabled; the async
    # endpoint rejects them as a second safety gate.
    source_manifest = _source_campaign_manifest(agent, site_type)
    if source_manifest:
        selected_slugs: set[str] = set()
        selection_was_sent = False
        for tp_selection in selected_pos.values():
            if not isinstance(tp_selection, dict):
                continue
            selection_was_sent = True
            selected_slugs.update(str(value) for value in (tp_selection.get("labels") or []))
            selected_slugs.update(str(value) for value in (tp_selection.get("groups") or []))
        source_plan = []
        blockers: list[str] = [
            "source-aware builder ещё не включён: остановка выполнена до создания черновиков"
        ]
        for campaign in source_manifest.get("campaigns") or []:
            slug = str(campaign.get("slug") or "")
            if selection_was_sent and slug not in selected_slugs:
                continue
            label = str(campaign.get("source_name") or slug)
            name, renamed = _uniq(
                f"{campaign.get('tp')}_cpc_site — {label}" + (f" - {oblast}" if oblast else "")
            )
            missing = list(campaign.get("missing") or [])
            blockers.extend(f"{label}: отсутствует {field}" for field in missing)
            source_plan.append({
                "type": campaign.get("engine_type"),
                "variant": campaign.get("engine_type"),
                "tp": campaign.get("tp"),
                "pay": "tcpa",
                "name": name,
                "renamed": renamed,
                "budget": campaign.get("weekly_budget"),
                "cpa": rs.get("cpc_cpa") or rs.get("cpa"),
                "source_campaign_slug": slug,
                "source_campaign_id": campaign.get("source_campaign_id"),
                "source_group_count": len(campaign.get("groups") or []),
                "source_ready": bool(campaign.get("source_ready")),
                "missing": missing,
                "placement": campaign.get("placement"),
                "bid_modifiers": campaign.get("bid_modifiers") or [],
                "pre_draft_only": True,
            })
        source_plan = _stamp_plan_context(_drop_server_versioned(source_plan))
        return jsonify({
            "login": login, "site_type": site_type, "struct_site_type": struct_site_type,
            "r_code": r_code, "oblast": oblast,
            "feeds": 0, "count": len(source_plan), "resolved_cpa": cpa,
            "resolved_budget": budget, "renamed": sum(1 for item in source_plan if item["renamed"]),
            "plan": source_plan, "warnings": warnings,
            "source_manifest": source_manifest.get("source"),
            "pre_draft_only": True,
            "pre_draft_blockers": list(dict.fromkeys(blockers)),
            "feed_alert": {"needed": False, "missing": [], "will_skip_types": [],
                           "fallback_feed": None},
            "metrika_alert": metrika_alert,
        })

    # #4 (Семён 2026-07-12): галочка «под стиль сайта» (n) — ЕДИНО для ВСЕХ слепков (авто и dmp):
    # активна → cpc+cpa; снята (no_cpa) → только cpc. Влияет на всех, кто эмитит per-pay (tp2/tp4/МК).
    pays = ["tcpa"] if no_cpa else ["tcpa", "cpa"]
    # Посевы-пакет: фронт сигналит через agent_group="posevy" (вместо tp8+-чекбокса, которого нет).
    # Добавляем маркер "posevy" в список вариантов, чтобы сработал обработчик ниже (строка "posevy").
    if agent_group == "posevy" and "posevy" not in {str(v) for v in variants}:
        variants = list(variants) + ["posevy"]
    plan = []
    want_master = want_product = False                    # tp6/tp7 строим из структуры после цикла variants
    want_tp3 = False                                       # tp3 (ТГ-Фид) тоже требует URL-фид на аккаунте
    want_tp5_gallery = False                               # tp5 (Поиск+Динамика+ТГ) — товарная галерея потребляет single feed
    # Текстовые движки: один элемент-кампания на tp (наполняется моделями из пака внутри).
    # tp1_rsy → ЕПК РСЯ v501 mode=network_cpa (правильный путь из CODER.md + CAMPAIGN_INVARIANTS.md)
    _TEXT_PLAN = {"search_test": "Поиск (тест)", "tp1_rsy": "РСЯ", "search_gallery": "Поиск + Динамика + ТГ",
                  "search_dynamic": "Поиск + Динамика", "rsya_gallery": "Товарная галерея (РСЯ)"}
    for v in variants:
        # ── Посевы (tp8/tp9/tp10) — 4 кампании на tp: мультибренд + 3 монобренда ──
        # Решение Семёна 2026-07-22: вместо 3 кампаний теперь 12 (3 tp × 4 варианта):
        # мультибренд (ct0000) + Tenet (ct0300) + Lada (ct0181) + Haval (ct0111).
        # Список брендов фиксированный для всех аккаунтов независимо от ассортимента.
        if str(v) == "posevy":
            from .create_set_tp8_10 import _campaign_name as _post_camp_name  # noqa: PLC0415
            _POSEVY_TP_LABELS = {
                "tp8":  "Посевы Telegram",
                "tp9":  "Посевы Max",
                "tp10": "Посевы Telegram+Max",
            }
            # Монобренды (фиксированный список по решению Семёна 2026-07-22)
            _POSEVY_MONO_BRANDS = [
                ("Tenet", "ct0300"),
                ("Lada",  "ct0181"),
                ("Haval", "ct0111"),
            ]
            for _post_tp in ("tp8", "tp9", "tp10"):
                # 1. Мультибренд-кампания (ct0000, brand_label="Посевы") — как раньше
                _post_name_raw = _post_camp_name(_post_tp, "ct0000", r_code, oblast, "Посевы")
                _post_nm, _post_renamed = _uniq(_post_name_raw)
                plan.append({
                    "type":        f"post_{_post_tp}",
                    "variant":     "posevy",
                    "tp":          _post_tp,
                    "pay":         None,
                    "name":        _post_nm,
                    "renamed":     _post_renamed,
                    "budget":      10_000,    # SPEC §2.9
                    "cpa":         None,
                    "r_code":      r_code,
                    "oblast":      oblast,
                    "ct":          "ct0000",  # мультибренд
                    "brand_label": "Посевы",
                    "save_draft":  True,
                    "t":           _POSEVY_TP_LABELS[_post_tp],  # метка для UI
                })
                # 2. Монобрендовые кампании (Tenet / Lada / Haval)
                for _brand_name, _brand_ct in _POSEVY_MONO_BRANDS:
                    _brand_name_raw = _post_camp_name(_post_tp, _brand_ct, r_code, oblast, _brand_name)
                    _brand_nm, _brand_renamed = _uniq(_brand_name_raw)
                    plan.append({
                        "type":        f"post_{_post_tp}",
                        "variant":     "posevy",
                        "tp":          _post_tp,
                        "pay":         None,
                        "name":        _brand_nm,
                        "renamed":     _brand_renamed,
                        "budget":      10_000,
                        "cpa":         None,
                        "r_code":      r_code,
                        "oblast":      oblast,
                        "ct":          _brand_ct,
                        "brand_label": _brand_name,
                        "save_draft":  True,
                        "t":           _POSEVY_TP_LABELS[_post_tp],
                    })
            continue
        if str(v) in _TEXT_PLAN:
            # tp1_rsy: имя кампании строим по канону CODER.md из структуры слепка.
            # Каждый item структуры tp1 = отдельная кампания (item.t = имя таргетинга/кампании).
            if str(v) == "tp1_rsy":
                # ЗАДАЧА 7: КАМПАНИЯ = item.camp_names (1:1 со «Структурой слепков»), НЕ сегмент-коллапс.
                # Источник — structure_to_campaigns (зеркало _build_export_rows). Управляющие теги
                # «х3»/«все фиды» — ТОЛЬКО из campaign_tags (не seg_modes, DoD 7.4/7.6).
                from .create_set_structure import (
                    structure_to_campaigns as _s2c, campaign_protected_tags_bulk as _cptags,
                    detect_protected_tags as _detect_tags,
                    X3_TAG as _X3, ALL_FEEDS_TAG as _ALLF, X3_VARIANTS as _X3V,
                    CATALOG_TAG as _CAT)
                sel_tp1 = _sel_labels(1)
                camps1 = _s2c(agent, site_type, "tp1")
                _cn_words = " ".join((c.get("name") or "") for c in camps1).lower()

                def _tp1_mode(nm: str, tgt: str) -> tuple:
                    """(autotarget, keep_keywords). Явная метка режима В ИМЕНИ кампании — авторитетна
                    (1:1 с реальным именем; «КС» → автотаргет ВЫКЛ, не угадываем по gc-состоянию).
                    Имя без метки → падаем на таргетинг из кодера (gc-состояние)."""
                    low = (nm or "").lower()
                    has_at_name = "автотаргет" in low
                    has_kw_name = bool(re.search(r"(^|[^а-яё])кс([^а-яё]|$)", low)) or "ключев" in low
                    if has_at_name and has_kw_name:
                        return True, True                       # «КС + Автотаргетинг»
                    if has_at_name:
                        return True, False                      # «Автотаргетинг»
                    if has_kw_name:
                        return False, True                      # «КС» — автотаргет ВЫКЛ (как имя)
                    # имя без явного режима → по таргетингу кодера (gc-состояние aon/aoff)
                    return (True, False) if str(tgt) == "автотаргетинг" else (False, True)

                if camps1:
                    tags1 = _cptags(agent, site_type, "tp1")
                    _sib1 = [x.get("name") or "" for x in camps1]  # guard х3: сверка КС/Автотаргет-сиблингов
                    for c in camps1:
                        cname = c.get("name") or ""
                        # фильтр выбранных позиций: по имени кампании ИЛИ по её доминантному сегменту
                        if sel_tp1 is not None and cname not in sel_tp1 and (c.get("segment") not in sel_tp1):
                            continue
                        # управляющие теги: реестр OVERRIDE → UI-эвристика (х3/все фиды)
                        _ctags = _detect_tags(c, tags1.get(cname), siblings=_sib1)
                        _og = list(c.get("gks") or [])          # маршрутизация контента per-group
                        _oc = list(c.get("cts") or [])
                        _all_feeds = _ALLF in _ctags            # tp1-РСЯ: все фиды группами (флаг движку)
                        _low_cn = cname.lower()
                        # «Комби+Фид»/«Комби+Смарт-Баннер»: НЕ products_only — TextAd сохраняется,
                        # tp1_catalog форсит ShoppingAd+ListingAd (не через products_only).
                        # Чистый «Фид»/«Смарт-Баннер» (без «комби» в имени) → products_only (без TextAd).
                        _is_combi = "комби" in _low_cn
                        _has_feed_or_smart = ("фид" in _low_cn) or ("смарт-баннер" in _low_cn) or ("смарт-банер" in _low_cn)
                        _prod_only = (not _is_combi) and _has_feed_or_smart

                        def _emit_tp1(label_body: str, at: bool, keep: bool):
                            label = label_body + (f" - {oblast}" if oblast else "")
                            nm, renamed = _uniq(f"tp1_cpc_site — {label}")
                            _p = {"type": "tp1_rsy", "variant": v, "pay": None, "feed_id": None,
                                  "feed_name": None, "name": nm, "renamed": renamed,
                                  "budget": rs["cpc_budget"], "cpa": rs["cpc_cpa"],
                                  "tp1_segment": None, "tp1_label": label,
                                  "autotarget": (at or _prod_only), "autotarget_keep_keywords": keep,
                                  "tp1_only_gks": _og, "tp1_only_cts": _oc,
                                  "tp1_all_feeds": _all_feeds, "camp_key": cname}
                            if _prod_only:
                                _p["products_only"] = True
                            # тег «каталоги»: форсит shopping/листинги в tp1 (SchoppingAd+ListingAd).
                            # Для tp3/tp5/tp7 листинги эмитируются ВСЕГДА — тег no-op, не проставляем.
                            if _CAT in _ctags:
                                _p["tp1_catalog"] = True
                            # «Комби+Фид»/«Комби+Смарт-Баннер»: force tp1_catalog БЕЗ products_only →
                            # ShoppingAd+ListingAd форсированы, TextAd сохраняется (_skip_text_ads=False).
                            if _is_combi and _has_feed_or_smart:
                                _p["tp1_catalog"] = True
                            plan.append(_p)

                        if _X3 in _ctags:
                            # тег «х3» → 3 кампании (КС / автотаргет / КС+автотаргет), КАЖДОЙ ПОЛНЫЙ бюджет
                            _x3_base_name = _strip_tp1_target_tail(cname)
                            for _var in _X3V:
                                _emit_tp1(f"{_x3_base_name} - {_var['suffix']}",
                                          _var["autotarget"], _var["keep_keywords"])
                        else:
                            _at, _keep = _tp1_mode(cname, c.get("targeting") or "")
                            _emit_tp1(cname, _at, _keep)
                else:
                    # FALLBACK (нет camp_names/парс-сбой): прежний сегмент-путь — слепки без данных не ломаем.
                    tp1_items = _tp1_plan_names(agent, site_type, r_code)
                    if not tp1_items:
                        warnings.append("tp1 (РСЯ): нет в структуре слепка — пропущен")
                        continue
                    segs_present = []
                    for pos in tp1_items:
                        seg = _ct_segment(pos.get("gc", ""))
                        if seg not in segs_present:
                            segs_present.append(seg)
                    segs_present = [s for s in ("Марки", "Модели", "Общее") if s in segs_present] or ["Марки"]
                    for seg in segs_present:
                        if sel_tp1 is not None and seg not in sel_tp1:
                            continue
                        modes = _tp_seg_modes(agent, site_type, "tp1", seg)
                        if modes is None:
                            modes = _slepok_tp_modes(agent, site_type, "tp1", seg)
                        if modes is None:
                            modes = ["КС"]
                        for mode in modes:
                            if mode == "КС+Автотаргет":
                                at, keep_kw, suffix = True, True, "КС + Автотаргетинг"
                            elif mode == "Автотаргет":
                                at, keep_kw, suffix = True, False, "Автотаргетинг"
                            else:
                                at, keep_kw, suffix = False, True, "КС"
                            _ov1 = _tp_seg_name_override(agent, site_type, "tp1", seg, mode)
                            label = (_ov1 or f"РСЯ - {seg} - {suffix}") + (f" - {oblast}" if oblast else "")
                            nm, renamed = _uniq(f"tp1_cpc_site — {label}")
                            plan.append({"type": "tp1_rsy", "variant": v, "pay": None, "feed_id": None,
                                         "feed_name": None, "name": nm, "renamed": renamed,
                                         "budget": rs["cpc_budget"], "cpa": rs["cpc_cpa"],
                                         "tp1_segment": seg, "tp1_label": label, "autotarget": at,
                                         "autotarget_keep_keywords": keep_kw})
                # Смарт-Баннер / Фиды — товарные БЕЗ ТГО + автотаргет (процедурная добавка, load-bearing).
                # Пропускаем формат, если он УЖЕ покрыт camp_names-кампанией (нет двойной эмиссии).
                _fmt_segments = (segs_present if not camps1 else [None])
                for fmt in ("Смарт-Баннер", "Фиды"):
                    if ("фид" if fmt == "Фиды" else "смарт") in _cn_words:
                        continue                        # формат уже есть как отдельная camp_names-кампания
                    if "Автотаргет" not in (_slepok_tp_modes(agent, site_type, "tp1", fmt) or []):
                        continue                        # формат есть только как автотаргет (как боевые)
                    for _fmt_seg in _fmt_segments:
                        if sel_tp1 is not None and fmt not in sel_tp1 and _fmt_seg not in sel_tp1:
                            continue
                        _seg_part = f" - {_fmt_seg}" if _fmt_seg else ""
                        label = f"РСЯ - {fmt}{_seg_part} - Автотаргетинг" + (f" - {oblast}" if oblast else "")
                        nm, renamed = _uniq(f"tp1_cpc_site — {label}")
                        plan.append({"type": "tp1_rsy", "variant": v, "pay": None, "feed_id": None,
                                     "feed_name": None, "name": nm, "renamed": renamed,
                                     "budget": rs["cpc_budget"], "cpa": rs["cpc_cpa"],
                                     "tp1_segment": _fmt_seg, "tp1_label": label,
                                     "autotarget": True, "products_only": True})
                continue
            # tp4 «Поиск + Динамика» — поисковые ТЕКСТ-кампании (движок tp2), но item-level по
            # маркам/темам (LIVE Кудерко porg-mgrauofh: TEXT_CAMPAIGN, Search=AVERAGE_CPA, Network=OFF).
            if str(v) == "search_dynamic":
                # Строгое соответствие слепку: если боевой профиль слепка НЕ ведёт tp4 —
                # не строим, даже если tp4 есть в структуре (structure держит его как донор).
                if _slepok_profile_excludes_tp(agent, site_type, "tp4"):
                    warnings.append("tp4 (Поиск+Динамика): нет в боевом профиле слепка — пропущен (строгое соответствие)")
                    continue
                # Сегменты «Марки»/«Модели» (как боевые), бренды/модели — ГРУППЫ внутри.
                tp4_items = _tp_plan_names(agent, site_type, "tp4")
                if not tp4_items:
                    warnings.append("tp4 (Поиск+Динамика): нет в структуре слепка — пропущен")
                    continue
                segs4 = []
                for pos in tp4_items:
                    seg = _ct_segment(pos.get("gc", ""))
                    if seg not in segs4:
                        segs4.append(seg)
                segs4 = [s for s in ("Марки", "Модели", "Общее") if s in segs4] or ["Марки"]
                sel4 = _sel_labels(4)
                # ЗАДАЧА 7: КАМПАНИЯ = item.camp_names (не сегмент-коллапс). pays + profile-гейт целы.
                from .create_set_structure import structure_to_campaigns as _s2c4
                camps4 = _s2c4(agent, site_type, "tp4")
                if camps4:
                    for c in camps4:
                        cname = c.get("name") or ""
                        if sel4 is not None and cname not in sel4 and (c.get("segment") not in sel4):
                            continue
                        _at4, _keep4 = _txt_targeting_mode(cname, c.get("targeting") or "")
                        _og4 = list(c.get("gks") or [])
                        _oc4 = list(c.get("cts") or [])
                        for pay in pays:
                            paycode = "cpc" if pay == "tcpa" else "cpa"
                            label = cname + (f" - {oblast}" if oblast else "")
                            nm, renamed = _uniq(f"tp4_{paycode}_site — {label}")
                            plan.append({"type": "search_dynamic", "variant": v, "pay": pay,
                                         "feed_id": None, "feed_name": None, "name": nm, "renamed": renamed,
                                         "budget": _bud(pay), "cpa": _cpa_for(pay), "tp": "tp4",
                                         "tp4_segment": None, "tp4_label": label, "autotarget": _at4,
                                         "autotarget_keep_keywords": _keep4,
                                         "only_gks": _og4, "only_cts": _oc4, "camp_key": cname})
                else:
                    # FALLBACK — прежний сегмент-путь (нет camp_names)
                    for seg in segs4:
                        if sel4 is not None and seg not in sel4:
                            continue
                        for pay in pays:
                            paycode = "cpc" if pay == "tcpa" else "cpa"
                            label = f"Поиск + Динамика - {seg} - КС" + (f" - {oblast}" if oblast else "")
                            nm, renamed = _uniq(f"tp4_{paycode}_site — {label}")
                            plan.append({"type": "search_dynamic", "variant": v, "pay": pay,
                                         "feed_id": None, "feed_name": None, "name": nm, "renamed": renamed,
                                         "budget": _bud(pay), "cpa": _cpa_for(pay), "tp": "tp4",
                                         "tp4_segment": seg, "tp4_label": label})
                # Донор-сегмент «Модели» (LOAD-BEARING, Терехов): у слепка нет своих «Моделей» в tp4 →
                # добавляем «Модели» от донора отдельной кампанией (segment-путь: контент от донора).
                if ("Модели" not in segs4 and _segment_donor("Модели", "tp4", site_type)
                        and (sel4 is None or "Модели" in sel4)):
                    for pay in pays:
                        paycode = "cpc" if pay == "tcpa" else "cpa"
                        label = f"Поиск + Динамика - Модели - КС" + (f" - {oblast}" if oblast else "")
                        nm, renamed = _uniq(f"tp4_{paycode}_site — {label}")
                        plan.append({"type": "search_dynamic", "variant": v, "pay": pay,
                                     "feed_id": None, "feed_name": None, "name": nm, "renamed": renamed,
                                     "budget": _bud(pay), "cpa": _cpa_for(pay), "tp": "tp4",
                                     "tp4_segment": "Модели", "tp4_label": label})
                continue
            if str(v) == "rsya_gallery":
                if _slepok_profile_excludes_tp(agent, site_type, "tp3"):
                    warnings.append("tp3 (ТГ РСЯ): нет в боевом профиле слепка — пропущен (строгое соответствие)")
                    continue
                tp3_items = _tp_plan_names(agent, site_type, "tp3")
                if not tp3_items:
                    warnings.append("tp3 пропущен: в выбранном слепке нет tp3 для этого типа сайта")
                    continue
                sel3 = _sel_labels(3)
                want_tp3 = True                            # используется ниже для feed_alert (нужен URL-фид)
                # ЗАДАЧА 7: КАМПАНИЯ = item.camp_names (не 1 РК «ТГ - Фид (товары)»). При ОДНОМ фиде
                # каждая camp_names-кампания = 1 РК с 1 фид-группой (fan-out не размножает). «все фиды»
                # (детектор) → все разрешённые фиды ГРУППАМИ в ОДНОЙ РК.
                from .create_set_structure import (structure_to_campaigns as _s2c3,
                                                   campaign_protected_tags_bulk as _cptags3,
                                                   detect_protected_tags as _detect3,
                                                   ALL_FEEDS_TAG as _ALLF3)
                camps3 = _s2c3(agent, site_type, "tp3")
                if camps3:
                    tags3 = _cptags3(agent, site_type, "tp3")
                    for c in camps3:
                        cname = c.get("name") or ""
                        # НАХОДКА 2: старый substring-матч cname↔posName ненадёжен:
                        # posName в UI = «grpName - item.t» (generic-else, tp3), а cname = camp_names-значение
                        # (напр. «Товарная галерея - Марка - Автотаргетинг») → пересечения нет → кампании
                        # пропускались целиком. Исправление: кампания включается, если sel3=None (нет
                        # фильтра) ИЛИ хотя бы один item.t кампании является суффиксом какого-либо posName
                        # в sel3 (posName заканчивается на item.t, т.к. posName = «grpName - item.t»).
                        # Фильтруем only_cts: оставляем только ct выбранных items.
                        _all_cts3 = list(c.get("cts") or [])
                        if sel3 is not None and c.get("items"):
                            _keep3: set = set()
                            for _it3 in c["items"]:
                                _it3t = (_it3.get("t") or "").strip()
                                if _it3t and any(_lbl.endswith(_it3t) for _lbl in sel3):
                                    _ct3 = _gc_ct(_it3.get("gc") or "")
                                    if _ct3 and _ct3 != "ct0000":
                                        _keep3.add(_ct3)
                            if not _keep3:
                                continue   # ни один item не выбран → пропустить кампанию
                            _all_cts3 = [ct for ct in _all_cts3 if ct in _keep3]
                        _af3 = _ALLF3 in _detect3(c, tags3.get(cname))
                        nm, renamed = _uniq(f"tp3_cpc_site — {cname}" + (f" - {oblast}" if oblast else ""))
                        plan.append({"type": "rsya_gallery", "variant": v, "pay": None, "feed_id": None,
                                     "feed_name": None, "name": nm, "renamed": renamed,
                                     "budget": rs.get("cpc_budget") or rs["budget"],
                                     "cpa": rs.get("cpc_cpa") or rs["cpa"], "tp": "tp3",
                                     "only_gks": list(c.get("gks") or []), "only_cts": _all_cts3,
                                     "tp3_all_feeds": _af3, "camp_key": cname})
                else:
                    # FALLBACK — прежний путь (нет camp_names): одна «ТГ - Фид (товары)» (fan-out по фидам)
                    if sel3 is not None:
                        tp3_items = [pos for pos in tp3_items
                                     if any((pos.get("label") or "") in s for s in sel3)]
                    if not tp3_items:
                        continue
                    label = "ТГ - Фид (товары)" + (f" - {oblast}" if oblast else "")
                    nm, renamed = _uniq(f"tp3_cpc_site — {label}")
                    plan.append({"type": "rsya_gallery", "variant": v, "pay": None, "feed_id": None,
                                 "feed_name": None, "name": nm, "renamed": renamed,
                                 "budget": rs.get("cpc_budget") or rs["budget"],
                                 "cpa": rs.get("cpc_cpa") or rs["cpa"], "tp": "tp3",
                                 "tp3_selected": [pos.get("label") for pos in tp3_items]})
                continue
            # tp2 «Поиск» — сегментные ТЕКСТ-кампании (как боевые: Марки/Модели × {КС, Автотаргет},
            # бренды/модели — ГРУППЫ внутри). Режимы — по профилю слепка (гейт: ровно что есть, не лишнее).
            if str(v) == "search_test":
                if _slepok_profile_excludes_tp(agent, site_type, "tp2"):
                    warnings.append("tp2 (Поиск): нет в боевом профиле слепка — пропущен (строгое соответствие)")
                    continue
                # SPLIT-DRIVEN слепки (не авто-сегментные, напр. dmp): сегментная авто-
                # классификация (Марки/Модели/Общее по ag_part1) к их ct неприменима → идём по
                # tp.splits[]: одна кампания на блок, имя = буквально label блока (Идентификация /
                # Маркетинговые инструменты / …), БЕЗ суффиксов Марки/Модели/КС/Автотаргет и БЕЗ
                # pay-дублирования. Триггер узкий: site_type НЕ в авто-типах И у tp2 есть splits
                # (сегодня это только dmp — авто-слепки splits на tp2 не держат, регрессия исключена).
                _tp2_splits = _tp_splits(agent, site_type, "tp2")
                if site_type not in _AUTO_SEGMENT_SITE_TYPES and _tp2_splits:
                    sel2 = _sel_labels(2)
                    for sp in _tp2_splits:
                        label = (sp.get("label") or sp.get("sq") or "").strip()
                        if not label:
                            continue
                        if sel2 is not None and label not in sel2:
                            continue
                        grp_names = [(g.get("name") or g.get("group") or "").strip()
                                     for g in sp.get("groups", [])]
                        # ct-коды ИМЕННО этого split-блока (из gc групп) — прокидываются в наполнение
                        # как only_cts, чтобы каждая split-кампания получила ТОЛЬКО свои группы, а не
                        # весь пул слепка (34 ct у dmp/tp2). _struct_cts для splits-формата даёт [] —
                        # поэтому фильтруем ct-кодами плана, а не именами групп (см. _build_text_from_pack
                        # / _tp1_pack_groups only_cts-ветку).
                        grp_cts: list[str] = []
                        for g in sp.get("groups", []):
                            for gi in g.get("items", []):
                                _ct = _gc_ct(gi.get("gc", "")) if isinstance(gi, dict) else ""
                                if _ct and _ct != "ct0000" and _ct not in grp_cts:
                                    grp_cts.append(_ct)
                        # #4 (Семён 2026-07-12): тип оплаты НЕ из split.pay, а по галочке «под стиль
                        # сайта» — единый механизм pays (активна → cpc+cpa; снята → только cpc).
                        for _pay in pays:
                            _paycode = "cpc" if _pay == "tcpa" else "cpa"
                            nm, renamed = _uniq(f"tp2_{_paycode}_site — {label}")
                            plan.append({"type": "search_test", "variant": v, "pay": _pay,
                                         "feed_id": None, "feed_name": None, "name": nm, "renamed": renamed,
                                         "budget": _bud(_pay), "cpa": _cpa_for(_pay), "tp": "tp2",
                                         "tp4_segment": None, "autotarget": False,
                                         "tp2_split_sq": sp.get("sq"), "tp2_split_label": label,
                                         "tp2_split_groups": grp_names, "tp2_split_cts": grp_cts})
                    continue
                tp2_items = _tp_plan_names(agent, site_type, "tp2")
                if not tp2_items:
                    warnings.append("tp2 (Поиск): нет в структуре слепка — пропущен")
                    continue
                sel2 = _sel_labels(2)
                # ЗАДАЧА 7: КАМПАНИЯ = item.camp_names (не сегмент-коллапс). pays + profile-гейт целы.
                from .create_set_structure import structure_to_campaigns as _s2c2
                camps2 = _s2c2(agent, site_type, "tp2")
                if camps2:
                    for c in camps2:
                        cname = c.get("name") or ""
                        if sel2 is not None and cname not in sel2 and (c.get("segment") not in sel2):
                            continue
                        _at2, _keep2 = _txt_targeting_mode(cname, c.get("targeting") or "")
                        _og2 = list(c.get("gks") or [])
                        _oc2 = list(c.get("cts") or [])
                        for pay in pays:
                            paycode = "cpc" if pay == "tcpa" else "cpa"
                            label = cname + (f" - {oblast}" if oblast else "")
                            nm, renamed = _uniq(f"tp2_{paycode}_site — {label}")
                            plan.append({"type": "search_test", "variant": v, "pay": pay,
                                         "feed_id": None, "feed_name": None, "name": nm, "renamed": renamed,
                                         "budget": _bud(pay), "cpa": _cpa_for(pay), "tp": "tp2",
                                         "tp4_segment": None, "autotarget": _at2,
                                         "autotarget_keep_keywords": _keep2,
                                         "only_gks": _og2, "only_cts": _oc2, "camp_key": cname})
                    continue
                # FALLBACK — прежний сегмент-путь (нет camp_names)
                segs2 = []
                for pos in tp2_items:
                    seg = _ct_segment(pos.get("gc", ""))
                    if seg not in segs2:
                        segs2.append(seg)
                segs2 = [s for s in ("Марки", "Модели", "Общее") if s in segs2] or ["Марки"]
                for seg in segs2:
                    if sel2 is not None and seg not in sel2:
                        continue
                    modes = _slepok_tp_modes(agent, site_type, "tp2", seg)
                    if modes is None:
                        modes = ["КС"]
                    for mode in modes:
                        at = mode == "Автотаргет"
                        suffix = "Автотаргетинг" if at else "КС"
                        for pay in pays:
                            paycode = "cpc" if pay == "tcpa" else "cpa"
                            _ov2 = _tp_seg_name_override(agent, site_type, "tp2", seg, mode)
                            label = (_ov2 or f"Поиск - {seg} - {suffix}") + (f" - {oblast}" if oblast else "")
                            nm, renamed = _uniq(f"tp2_{paycode}_site — {label}")
                            plan.append({"type": "search_test", "variant": v, "pay": pay,
                                         "feed_id": None, "feed_name": None, "name": nm, "renamed": renamed,
                                         "budget": _bud(pay), "cpa": _cpa_for(pay), "tp": "tp2",
                                         "tp4_segment": seg, "autotarget": at})
                continue
            # tp5 «Поиск + Динамика + ТГ» — сегментные кампании Марки/Модели × {КС, Автотаргет}
            # по профилю слепка (как боевые; бренды/модели — ГРУППЫ внутри). Имя — cpc-канон;
            # движок _create_tp5_campaign сам делает пару cpc+cpa и FAN-OUT по фидам, поэтому pay=None.
            if str(v) == "search_gallery":
                if _slepok_profile_excludes_tp(agent, site_type, "tp5"):
                    warnings.append("tp5 (Поиск+Динамика+ТГ): нет в боевом профиле слепка — пропущен (строгое соответствие)")
                    continue
                tp5_items = _tp_plan_names(agent, site_type, "tp5")
                if not tp5_items:
                    warnings.append("tp5 (Поиск+Динамика+ТГ): нет в структуре слепка — пропущен")
                    continue
                want_tp5_gallery = True                    # tp5 в наборе → товарная галерея требует URL-фид (feed_alert)
                sel5 = _sel_labels(5)
                # ЗАДАЧА 7: КАМПАНИЯ = item.camp_names. «все фиды» — реестр/эвристика (детектор).
                from .create_set_structure import (structure_to_campaigns as _s2c5,
                                                   campaign_protected_tags_bulk as _cptags5,
                                                   detect_protected_tags as _detect5,
                                                   ALL_FEEDS_TAG as _ALLF5)
                camps5 = _s2c5(agent, site_type, "tp5")
                if camps5:
                    tags5 = _cptags5(agent, site_type, "tp5")
                    for c in camps5:
                        cname = c.get("name") or ""
                        if sel5 is not None and cname not in sel5 and (c.get("segment") not in sel5):
                            continue
                        _at5, _keep5 = _txt_targeting_mode(cname, c.get("targeting") or "")
                        _af5 = _ALLF5 in _detect5(c, tags5.get(cname))
                        nm, renamed = _uniq(f"tp5_cpc_site — {cname}" + (f" - {oblast}" if oblast else ""))
                        plan.append({"type": "search_gallery", "variant": v, "pay": None, "feed_id": None,
                                     "feed_name": None, "name": nm, "renamed": renamed,
                                     "budget": rs["cpc_budget"], "cpa": rs["cpc_cpa"], "tp": "tp5",
                                     "tp5_segment": None, "autotarget": _at5,
                                     "autotarget_keep_keywords": _keep5,
                                     "only_gks": list(c.get("gks") or []), "only_cts": list(c.get("cts") or []),
                                     "tp5_all_feeds": _af5, "camp_key": cname})
                else:
                    # FALLBACK — прежний сегмент-путь (нет camp_names)
                    segs5 = []
                    for pos in tp5_items:
                        seg = _ct_segment(pos.get("gc", ""))
                        if seg not in segs5:
                            segs5.append(seg)
                    segs5 = [s for s in ("Марки", "Модели", "Общее") if s in segs5] or ["Марки"]
                    for seg in segs5:
                        if sel5 is not None and seg not in sel5:
                            continue
                        modes = _slepok_tp_modes(agent, site_type, "tp5", seg)
                        if modes is None:
                            modes = ["КС"]
                        for mode in modes:
                            at = mode == "Автотаргет"
                            suffix = "Автотаргетинг" if at else "КС"
                            _ov5 = _tp_seg_name_override(agent, site_type, "tp5", seg, mode)
                            label = (_ov5 or f"Поиск + Динамика + ТГ - {seg} - {suffix}") + (f" - {oblast}" if oblast else "")
                            nm, renamed = _uniq(f"tp5_cpc_site — {label}")
                            plan.append({"type": "search_gallery", "variant": v, "pay": None, "feed_id": None,
                                         "feed_name": None, "name": nm, "renamed": renamed,
                                         "budget": rs["cpc_budget"], "cpa": rs["cpc_cpa"], "tp": "tp5",
                                         "tp5_segment": seg, "autotarget": at})
                # tp5 Фиды — товарные БЕЗ ТГО + автотаргет (как боевые pavlov), если профиль ведёт.
                # Пропускаем, если «Фиды» уже покрыты camp_names-кампанией (нет двойной эмиссии).
                _tp5_cn_words = " ".join((c.get("name") or "") for c in (camps5 or [])).lower()
                if ("фид" not in _tp5_cn_words
                        and "Автотаргет" in (_slepok_tp_modes(agent, site_type, "tp5", "Фиды") or [])
                        and (sel5 is None or "Фиды" in sel5)):
                    label = f"Поиск + Динамика + ТГ - Фиды - Автотаргетинг" + (f" - {oblast}" if oblast else "")
                    nm, renamed = _uniq(f"tp5_cpc_site — {label}")
                    plan.append({"type": "search_gallery", "variant": v, "pay": None, "feed_id": None,
                                 "feed_name": None, "name": nm, "renamed": renamed,
                                 "budget": rs["cpc_budget"], "cpa": rs["cpc_cpa"], "tp": "tp5",
                                 "tp5_segment": None, "autotarget": True, "products_only": True})
                continue
        # tp6 (Мастер) / tp7 (Товарка) строим НЕ здесь, а из СТРУКТУРЫ слепка (после цикла) —
        # чтобы предпросмотр/создание 1:1 совпадали с вкладками «Структура»/«Создание РК».
        if str(v).startswith("master"):
            want_master = True
        elif str(v).startswith("product"):
            want_product = True

    # ── tp6/tp7: источник — slepki_structure.json (как верх). 1 кампания на (группа × оплата). ──
    # Без взрыва по фидам: товарной (UAC product) нужен ОДИН feed_id (первый XML-фид аккаунта).
    feed0 = feeds[0] if feeds else None

    emitted_tp67: set[tuple] = set()

    def _emit_struct(tp_code: str, is_master: bool):
        tp_num = 6 if is_master else 7
        groups = _slepok_struct_groups(agent, site_type, tp_code)
        if not groups:
            # НЕТ tp6/tp7 в структуре слепка → НЕ создавать (правило Семёна 2026-07-03:
            # у Щербаковой нет tp6, а фолбэк «одна кампания без разреза» создавал их).
            warnings.append(f"{tp_code}: в структуре слепка «{agent}»/{site_type} нет — пропущен")
            return
        # Фильтр по выбранным позициям кампаний (tp6/tp7 — это кампании, НЕ группы).
        sel_pos = _sel_labels(tp_num) or _sel_groups(tp_num)
        if sel_pos is not None:
            groups = [g for g in groups
                      if (g.get("name") or "") in sel_pos or (g.get("group") or "") in sel_pos]
        # Плейсхолдер города в именах позиций структуры (сейчас — avtolajt_bu tp7 «ТК · ГОРОД»):
        # шаблон слепка город-агностичен, подставляем город аккаунта. СТРОГО ПОСЛЕ фильтра sel_pos —
        # пользователь в UI выбирает позицию по шаблонному имени (с «ГОРОД»), фильтр обязан матчить его.
        # Гард: пустой city → НЕ заменяем (иначе имя «ТК · » без города); плейсхолдер остаётся видимым
        # маркером + предупреждение в план. Мультигородская строка («Краснодар, Ростов») → первый город
        # (как ключ имени; область в имя всё равно кладёт _build_name через oblast).
        _city_one = (city.split(",")[0].strip() if city else "")
        if _city_one:
            for g in groups:
                for _k in ("name", "group", "label"):
                    _v = g.get(_k)
                    if isinstance(_v, str) and _CITY_PLACEHOLDER in _v:
                        g[_k] = _v.replace(_CITY_PLACEHOLDER, _city_one)
        elif any(_CITY_PLACEHOLDER in str(g.get(_k) or "")
                 for g in groups for _k in ("name", "group", "label")):
            warnings.append(f"{tp_code}: у аккаунта не определён город — плейсхолдер "
                            f"«{_CITY_PLACEHOLDER}» в именах позиций НЕ подставлен")
        allowed = _sq_for("6" if is_master else "7")
        _fanout_logged = False                            # tp7 fan-out по всем фидам логируем ОДИН раз на _emit_struct
        for g in groups:
            if g["sq"] not in allowed:                   # уважать выбранные оси посадки (site/kviz) из набора
                continue
            cat = g["name"]
            cat_base = (g.get("group") or cat or "").strip()
            interest_cat = g.get("group") or cat
            # Clean the visible structure name before resolving ct. Otherwise labels like
            # "МК - Chery - КС" fall through to item gc=ct0000 and produce generic content.
            _tp_label = "МК" if is_master else "ТК"
            _raw_name_cat = g.get("name") or g.get("label") or cat
            _name_cat = _csctx.tp67_clean_position_name_for_targeting(_raw_name_cat, _tp_label)
            if not _name_cat.strip() and not (g.get("label") or "").strip():
                _name_cat = g.get("name") or cat
            # Если название группы — РЕАЛЬНАЯ марка/модель (tp6 Мастер: «Haval Jolion»), берём её ct
            # (ct0119) в КОДЕР → движок выберет картинку+заголовки этой модели. Тема/общее → ct0000.
            cat_ct = (_ct_for_name(_name_cat) or _ct_for_name(cat_base) or _ct_for_name(cat) or _gc_ct(g.get("gc") or "") or _gc_ct(g.get("code") or "") or "ct0000")  # gc item'а (кодер) как fallback: даёт «Конкуренты»→ct0084 отдельно от «Ключи»→ct0000
            # ── Режим таргетинга — ПО СОДЕРЖИМОМУ структуры, а не по имени (Д7, Семён 2026-07-19) ──
            # Тот же расчёт, что делает создание (`create_set_master_product`). Раньше здесь стояла
            # регулярка по имени `_tp67_targeting_mode(g)` — и после починки создания план начал
            # расходиться с билдом: имя ставило `ag001` (все возрасты, метка «автотаргетинг»),
            # а кампания уезжала с socdem 35+ и реальными КС. Источник режима теперь ОДИН.
            _pos_key = _csctx._tp67_pos_key(g)
            _exp = _csctx.tp67_struct_expectations(agent, site_type, tp_code, cat_ct, city,
                                                   cat, g["sq"], pos_key=_pos_key)
            if not _exp.get("matched"):
                # Промах матча = позиция молча уехала бы в легаси-файл пака (0 ключей) и в
                # autotarget. Раньше это не давало даже warning'а — дефект возвращался тихо.
                warnings.append(f"{tp_code}: позиция «{g.get('name') or cat}» не сопоставлена со "
                                f"структурой (pos_key={_pos_key!r}) — ключи/аудитории НЕ подтянуты")
            _modes = _exp["modes"]                       # ГИБРИД: keywords+audience в одной позиции
            targeting_mode = "+".join(_modes)
            targeting_label = _csctx.tp67_targeting_label_from_modes(_modes, tp_code)
            _has_kw = "keywords" in _modes
            _has_aud = "audience" in _modes
            is_autotarget_name = not (_has_kw or _has_aud)  # чистый автотаргет → авто-имя (ag001); гибрид → ручное
            if int(g.get("audiences_unsupported") or 0) > 0:
                warnings.append(f"{tp_code}: позиция «{g.get('name') or cat}» — "
                                f"{int(g.get('audiences_unsupported'))} аудитор. структуры не "
                                f"поддержаны UAC-путём (AUDIENCE:/RETARGETING:) и не отправлены")
            # Аудитории — из того же эталона структуры, что и режим. merged-фолбэк
            # `_slepok_interest_for_struct` источником «есть аудитории» НЕ делаем (он непустой
            # почти всегда) — внутри `tp67_struct_expectations` он уже отфильтрован по source.
            ints = [str(x) for x in _exp["audiences"]] if _has_aud else []
            ints_source = ("structure" if ints else "not-audience")
            # tp7 (Товарка) фидовый → кампании по фидам, имя += фид. tp6 (Мастер) — без фида (одна запись).
            # Приоритет: явно заданный в структуре позиции фид (feed_role/feed_id/feed_key) → только он.
            # Не задан → безопасный дефолт FAN-OUT по ВСЕМ разрешённым фидам аккаунта (CODER.md) + явный лог.
            if is_master:
                feed_list = [(None, None, None)]
            else:
                _sub = _explicit_feed_subset(g, feeds)
                if _sub is None:                          # фид в позиции не указан → fan-out (обратная совместимость)
                    feed_list = [((f or {}).get("id"), (f or {}).get("name"), (f or {}).get("url") or "")
                                 for f in feeds]
                    if feeds and not _fanout_logged:
                        warnings.append(f"{tp_code}: позиции без явного feed_role/feed_id — товарка "
                                        f"размножается по всем {len(feeds)} разрешённым фидам (fan-out по умолчанию)")
                        _fanout_logged = True
                elif _sub:                                # явный фид найден среди разрешённых → только он
                    feed_list = [((f or {}).get("id"), (f or {}).get("name"), (f or {}).get("url") or "")
                                 for f in _sub]
                else:                                     # явный фид указан, но его нет среди разрешённых → НЕ на чужом
                    warnings.append(f"{tp_code}: позиция «{g.get('name') or cat}» указывает фид "
                                    f"(feed_role/feed_id/feed_key), которого нет среди разрешённых фидов — пропущена")
                    continue
            # #4 (Семён 2026-07-12): МК — тоже по галочке «под стиль сайта», единый механизм pays
            # (активна → cpc+cpa; снята → только cpc) для ВСЕХ слепков. Было: не-авто МК всегда cpa, 1 РК.
            _pays_here = pays
            for f_id, f_name, f_url in feed_list:
                for pay in _pays_here:
                    base_nm = _build_name(is_master, is_autotarget_name, pay, r_code, oblast,
                                          g["sq"], _name_cat, ct=cat_ct,
                                          targeting_label=targeting_label)
                    # Bug D fix: используем URL фида (без https://) вместо короткого имени из кабинета.
                    # Гард — по реально подставляемой метке (label), с срезкой домен-префикса.
                    # 2026-07-19, круг доработки: строку случайно откатила параллельная сессия
                    # (b35caf3, переименование МК/ТК), приняв импорт _strip_dom_plan за чужой —
                    # домен «carsklad-126.site» снова полез в имя tp7/tp5 (CAMPAIGN_NAME_DOMAIN_AND_TP_LABEL_DUPES).
                    _f_lbl = _strip_dom_plan(
                        (re.sub(r'^https?://', '', f_url) if f_url else f_name),
                        row.get("domain") or "")
                    if _f_lbl:
                        base_nm = f"{base_nm} — {_f_lbl}"
                    payload_sig = (
                        "master" if is_master else "product",
                        tp_code,
                        pay,
                        g["sq"],
                        f_id or 0,
                        cat_ct,
                        targeting_mode,
                        # Дедуп — по СТАБИЛЬНОМУ ключу позиции + по ФАКТИЧЕСКОМУ содержимому
                        # (Д7 2026-07-19). Было: ключ по display-имени + старый режим по имени.
                        # После перевода режима на содержимое две позиции с разными ключами могли
                        # схлопнуться в одну (имя и режим совпадали, а корпус ключей — нет).
                        _pos_key or _tp67_kw_position_key(cat or interest_cat or ""),
                        len(_exp["keywords"]),
                        tuple(str(x) for x in (ints or [])),
                    )
                    if payload_sig in emitted_tp67:
                        continue
                    emitted_tp67.add(payload_sig)
                    nm, renamed = _uniq(base_nm)
                    plan.append({"type": "master" if is_master else "product",
                                 "variant": ("master_" if is_master else "product_") + ("manual" if _has_kw else "auto"),
                                 "pay": pay, "sq": g["sq"], "tp": tp_code,
                                 "feed_id": f_id, "feed_name": f_name,
                                 # feed_label — ТА САМАЯ метка, что план подставил в имя выше.
                                 # Билд (create_set_master_product.py:670) обязан клеить её же,
                                 # иначе имена план↔билд расходятся (FEED_FALLBACK_PLAN_VS_BUILDER_DESYNC):
                                 # план считает метку из URL фида, кабинет отдаёт своё имя
                                 # («Фид легковых», иной регистр) — совпадение было случайным.
                                 "feed_label": _f_lbl, "ct": cat_ct,
                                 "coder_ct": cat_ct, "coder_brand": _ag_part1_map().get(cat_ct, ""),
                                 "name": nm, "renamed": renamed, "budget": _bud(pay), "cpa": _cpa_for(pay),
                                 "audience_cat": interest_cat, "position_name": cat,
                                 # pos_key — стабильный ключ позиции для повторного матча со
                                 # структурой на этапе создания (имя там уже с подставленным
                                 # городом и по нему не матчится).
                                 "pos_key": _pos_key, "gk": g.get("gk") or "",
                                 # Эталон структуры → его же читает live-верификатор (UAC_STRUCT_*).
                                 "struct_keywords": len(_exp["keywords"]),
                                 "struct_audiences": len(_exp["audiences"]),
                                 "targeting_mode": targeting_mode, "targeting_label": targeting_label,
                                 "audience_source": ints_source,
                                 # Явные per-position атрибуты слепка → движок применяет их, не угадывает.
                                 "keyword_source": g.get("keyword_source") or "", "pricing": g.get("pricing") or "",
                                 "structure_code": g.get("code") or "", "interest_ids": ints})

    # Предупреждение: структура слепка содержит tp6/tp7, но соответствующий вариант НЕ выбран
    # в запросе → блок создания не запустится, пользователь узнаёт только постфактум.
    # Только предупреждение (не блокирует): create продолжается, НЕ fatal.
    if not want_master:
        _tp6_struct = _slepok_struct_groups(agent, site_type, "tp6")
        if _tp6_struct:
            warnings.append(
                f"⚠️ структура слепка содержит tp6 (МК, {len(_tp6_struct)} позиций), "
                f"но master-вариант не выбран в запросе — tp6 не будет создан")
    if not want_product:
        _tp7_struct = _slepok_struct_groups(agent, site_type, "tp7")
        if _tp7_struct:
            warnings.append(
                f"⚠️ структура слепка содержит tp7 (ТК, {len(_tp7_struct)} позиций), "
                f"но product-вариант не выбран в запросе — tp7 не будет создан")
    if want_master:
        _emit_struct("tp6", True)
    if want_product:
        _emit_struct("tp7", False)
    # Мастер кампаний (tp6) фид НЕ требует — master-items всегда строятся с feed_list=[(None,None,None)]
    # (см. выше). Фид нужен только Товарке (tp7 product) и динамике по фиду (tp3). want_master
    # СОЗНАТЕЛЬНО исключён из условия (Семён 2026-07-11: ложный диалог «tp6 не создать без фида»).
    # tp5 (search_gallery) ТОЖЕ потребляет single feed (инцидент SINGLE_FEED_TP5_TP3_WRONG_FEED):
    # без URL-фида товарная галерея tp5 молча пропадала → поп-ап feed_alert обязан всплыть и для tp5,
    # чтобы пользователь выбрал фолбэк-фид, а не потерял tp5 тихо (want_tp5_gallery добавлен 2026-07-13).
    _fal_needed = len(feeds) == 0 and (want_product or want_tp3 or want_tp5_gallery)
    from .create_set_input import FALLBACK_SINGLE_FEED_KEY as _FB_KEY, PROFILE_FEED_KEYS as _PROFILE_KEYS
    plan = _stamp_plan_context(_drop_server_versioned(plan))
    return jsonify({"login": login, "site_type": site_type, "struct_site_type": struct_site_type,
                    "r_code": r_code, "oblast": oblast,
                    "feeds": len(feeds), "count": len(plan),
                    "resolved_cpa": cpa, "resolved_budget": budget,   # бюджет/CPA из правил (для read-only + создания)
                    "renamed": sum(1 for p in plan if p["renamed"]), "plan": plan, "warnings": warnings,
                    "feed_alert": {
                        "needed": _fal_needed,
                        "missing": (list(_PROFILE_KEYS) if body.get("single_feed") else ["yandex.xml"]) if _fal_needed else [],
                        "will_skip_types": ((["product"] if want_product else []) + (["tp3"] if want_tp3 else []) + (["tp5"] if want_tp5_gallery else [])) if _fal_needed else [],
                        # найден фолбэк-фид → фронт показывает кнопку «Продолжить с другим фидом»
                        # (повторный set_plan с single_feed_fallback=true строит план на нём).
                        # Имя — реального фолбэка (не всегда канонический _FB_KEY: при его
                        # отсутствии предлагается первый разрешённый фид аккаунта).
                        "fallback_feed": ({"id": _sf_fallback_id,
                                           "name": (_sf_fallback_name or _FB_KEY)}
                                          if (_fal_needed and _sf_fallback_id) else None),
                    },
                    "metrika_alert": metrika_alert})

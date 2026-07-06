"""Create-set plan/name service extracted from blueprint.py."""

from __future__ import annotations

from flask import jsonify, request

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

def _build_name(is_master: bool, is_autotarget: bool, pay: str, r_code: str, oblast: str,
                sq: str = "site", cat: str | None = None, ct: str = "ct0000") -> str:
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
    # Возраст 24-55+ (ag011) — tp6/Мастер в РУЧНЫХ режимах (keywords И audience), кроме
    # автотаргетинга (там полный socdem age_18/ag001 по дизайну, #7 в create_set_master_product.py)
    # и товарки tp7 (возраст не настраивается → всегда «Все»). is_autotarget=True ТОЛЬКО для
    # targeting_mode=='autotarget' — раньше сюда приходил is_auto=(targeting_mode!='keywords'),
    # из-за чего audience тоже попадал в ag001 (живой баг 2026-07-06, porg-lzjk6p5m/terehov) —
    # рассинхрон с age_lower в create_set_master_product.py, который чинился той же логикой.
    age = "ag001" if (not is_master or is_autotarget) else "ag011"
    codes = f"{tp}_{paycode}_{sqcode}_{ct or 'ct0000'}_aon_n000_{r_code}_{fmt}_{age}_g00"
    tp_label = "Мастер кампаний" if is_master else "Товарка"  # #6: канон CODER.md (было МК_AT_tcpa)
    cat_part = f" - {cat}" if cat else ""                 # категория аудитории в человекочитаемое имя (как в слепках)
    return f"{codes} — {tp_label}{cat_part} - {oblast}"

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
    st = next((s for s in d.get("site_types", []) if s.get("name") == site_type), None)
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

def _set_plan_response():
    """План набора (предпросмотр, БЕЗ создания): какие кампании и с какими именами создадутся."""
    import psycopg2.extras
    body = request.json or {}
    login = (body.get("login") or "").strip()
    agent = (body.get("agent") or "").strip()            # ключ слепка-директолога (для аудиторий tp6/tp7, структуры tp1)
    variants = body.get("variants") or []                # master_auto/master_manual/product_auto/product_manual
    tp_sq = body.get("tp_sq") or {}                       # {"6":["site","kviz"], "7":["site"]} — оси посадки из набора
    # selected_pos: {tp_num_str: {labels:[...], groups:[...]}} — пер-позиционный выбор с фронта.
    # Если пришёл — фильтруем план по нему. Не пришёл — поведение прежнее (все позиции).
    selected_pos: dict = body.get("selected_pos") or {}
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
    ov_site = (body.get("site_type") or "").strip()      # ручной override типа сайта (правится в форме)
    ov_city = (body.get("city") or "").strip()           # ручной override города

    conn = _victory_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT city, site_type, agency_account, domain FROM public.local_gsheet_sites "
                    "WHERE login_key=%s AND direction='Авто' LIMIT 1", (login,))
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"error": f"аккаунт {login} не найден в local_gsheet_sites (Авто)"}), 404

    site_type = ov_site or (row["site_type"] or "").strip()   # override приоритетнее БД (правка ошибки в БД)
    city = ov_city or (row.get("city") or "")
    r_code, oblast = _resolve_region(city)
    # Наборы бюджет/CPA из «Глобальных правил». pay=cpa → CPA-набор (оплата за конверсии),
    # pay=tcpa → CPC-набор (оплата за клики). НЕ из формы.
    rs = _rule_sets(site_type, city)
    cpa, budget = rs["cpa"], rs["budget"]                # для resolved (read-only справка в форме)

    def _bud(pay):                                       # бюджет недели по типу оплаты
        return rs["cpa"] * 10 if pay == "cpa" else rs["cpc_budget"]

    def _cpa_for(pay):                                   # целевой CPA по типу оплаты
        return rs["cpa"] if pay == "cpa" else rs["cpc_cpa"]
    warnings: list[str] = []
    if r_code == "r0000":
        warnings.append("регион не определён — r0000")

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
            jf = _v5_get("feeds", token, login, ["Id", "Name", "SourceType", "Url"])
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
        # Галочка «по одному фиду» (single_feed): план тоже строим по /yandex.xml, чтобы
        # предпросмотр/счётчик совпадали с реальным созданием (иначе превью показывало все фиды,
        # а создавался один → выглядело как «галочка не работает»).
        # len(feeds) >= 1 (не >1): единственный ЧУЖОЙ фид (credit-page и т.п.) тоже должен
        # проходить strict-проверку /yandex.xml — иначе галочка «по одному фиду» игнорировалась.
        if bool(body.get("single_feed")) and len(feeds) >= 1:
            from .create_set_input import (SINGLE_FEED_KEY, FALLBACK_SINGLE_FEED_KEY,
                                           feed_row_matches_single_feed, prefer_single_feed_rows)
            # ⚠️ НЕ импортировать _first_url_feed из create_set_feeds напрямую: тот модуль требует
            # configure() (инъекции _filter_allowed_feed_rows и др.) → NameError на свежем процессе.
            # Берём инжектированную blueprint-обёртку (сама вызывает configure) из наших deps.
            # Резолвим /yandex.xml через API+Grid (как tp1): _first_url_feed видит полные Grid-объекты
            # с URL-полями, тогда как feeds здесь уже усечены до {id, name} → prefer_single_feed_rows
            # не находит совпадения по имени и молча берёт первый фид (credit-page-01-a.xml и т.п.).
            _sf_id = _first_url_feed(token, login, row.get("agency_account") or "", strict=True)
            if not _sf_id:
                # /yandex.xml нет → ищем фолбэк-фид (кнопка «Продолжить с другим фидом»)
                _sf_fallback_id = _first_url_feed(token, login, row.get("agency_account") or "",
                                                  strict=True, url_key=FALLBACK_SINGLE_FEED_KEY)
                if not _sf_fallback_id and feeds:
                    # канонического фолбэка тоже нет → предлагаем ПЕРВЫЙ разрешённый фид аккаунта
                    # (правило Семёна 03.07 #86: выбор второго фида должен быть всегда, когда
                    # в аккаунте есть хоть один фид — иначе кнопка пропадала из модалки)
                    _sf_fallback_id = int(feeds[0].get("id") or 0)
                if _sf_fallback_id:
                    _sf_fallback_name = next((str(f.get("name") or "") for f in feeds
                                              if int(f.get("id") or 0) == _sf_fallback_id), "")
                if _sf_fallback_id and bool(body.get("single_feed_fallback")):
                    _sf_id = _sf_fallback_id                   # пользователь подтвердил фолбэк
                    _sf_fallback_id = 0
                    warnings.append(f"«по одному фиду»: /{SINGLE_FEED_KEY} нет — по решению пользователя "
                                    f"используется фолбэк-фид {FALLBACK_SINGLE_FEED_KEY}")
            if _sf_id:
                _sf_list = [f for f in feeds if int(f.get("id") or 0) == _sf_id]
                if _sf_list:
                    feeds = _sf_list
                    if not body.get("single_feed_fallback"):
                        warnings.append(f"«по одному фиду»: план и создание — только /{SINGLE_FEED_KEY}")
                else:
                    feeds = prefer_single_feed_rows(feeds)
                    warnings.append(f"«по одному фиду»: целевой фид не в allow-list (id={_sf_id}), взят первый доступный")
            else:
                # strict-поиск не нашёл /yandex.xml ни через API, ни через Grid
                warnings.append(f"⚠️ /{SINGLE_FEED_KEY} не найден в аккаунте — товарные кампании (tp7) не будут созданы")
                feeds = []  # убрать product из плана (_emit_struct выдаёт кампании только по feeds)

    used: set = set()

    def _uniq(name: str):
        """Уникализация имени: занято (в аккаунте или в наборе) → +_v01…_v99."""
        if name not in existing and name not in used:
            used.add(name)
            return name, False
        for v in range(1, 100):
            cand = f"{name}_v{v:02d}"
            if cand not in existing and cand not in used:
                used.add(cand)
                return cand, True
        used.add(name)
        return name, True

    pays = ["tcpa", "cpa"]
    plan = []
    want_master = want_product = False                    # tp6/tp7 строим из структуры после цикла variants
    # Текстовые движки: один элемент-кампания на tp (наполняется моделями из пака внутри).
    # tp1_rsy → ЕПК РСЯ v501 mode=network_cpa (правильный путь из CODER.md + CAMPAIGN_INVARIANTS.md)
    _TEXT_PLAN = {"search_test": "Поиск (тест)", "tp1_rsy": "РСЯ", "search_gallery": "Поиск + Динамика + ТГ",
                  "search_dynamic": "Поиск + Динамика", "rsya_gallery": "Товарная галерея (РСЯ)"}
    for v in variants:
        if str(v) in _TEXT_PLAN:
            # tp1_rsy: имя кампании строим по канону CODER.md из структуры слепка.
            # Каждый item структуры tp1 = отдельная кампания (item.t = имя таргетинга/кампании).
            if str(v) == "tp1_rsy":
                # СЕГМЕНТЫ, как в боевых аккаунтах Щербаковой: 1 РСЯ-кампания на «Марки» и
                # 1 на «Модели» (бренды/модели — ГРУППЫ ВНУТРИ, не отдельные кампании).
                # Сегмент позиции структуры определяем по первому ct её группового кодера (gc).
                # cpc+cpa-пара строится внутри движка (_create_tp1_campaign).
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
                # Фильтр по выбранным сегментам (selected_pos[1].labels = ["Марки","Модели"]).
                sel_tp1 = _sel_labels(1)
                for seg in segs_present:
                    if sel_tp1 is not None and seg not in sel_tp1:
                        continue
                    # Режимы (КС/Автотаргет) — РОВНО как у реального аккаунта слепка (профиль).
                    # None (нет профиля, напр. Терехов) → КС, как раньше. [] → не строить (нет у слепка).
                    modes = _slepok_tp_modes(agent, site_type, "tp1", seg)
                    if modes is None:
                        modes = ["КС"]
                    for mode in modes:
                        at = mode == "Автотаргет"
                        suffix = "Автотаргетинг" if at else "КС"
                        label = f"РСЯ - {seg} - {suffix}" + (f" - {oblast}" if oblast else "")
                        nm, renamed = _uniq(f"tp1_cpc_site — {label}")
                        plan.append({"type": "tp1_rsy", "variant": v, "pay": None, "feed_id": None,
                                     "feed_name": None, "name": nm, "renamed": renamed,
                                     "budget": rs["cpc_budget"], "cpa": rs["cpc_cpa"],
                                     "tp1_segment": seg, "tp1_label": label, "autotarget": at})
                # Смарт-Баннер / Фиды — товарные объявления БЕЗ ТГО + автотаргет (как боевые),
                # отдельной кампанией если профиль слепка их ведёт. В боевых КС-варианта нет.
                for fmt in ("Смарт-Баннер", "Фиды"):
                    if sel_tp1 is not None and fmt not in sel_tp1:
                        continue
                    if "Автотаргет" not in (_slepok_tp_modes(agent, site_type, "tp1", fmt) or []):
                        continue                        # формат есть только как автотаргет (как боевые)
                    label = f"РСЯ - {fmt} - Автотаргетинг" + (f" - {oblast}" if oblast else "")
                    nm, renamed = _uniq(f"tp1_cpc_site — {label}")
                    plan.append({"type": "tp1_rsy", "variant": v, "pay": None, "feed_id": None,
                                 "feed_name": None, "name": nm, "renamed": renamed,
                                 "budget": rs["cpc_budget"], "cpa": rs["cpc_cpa"],
                                 "tp1_segment": None, "tp1_label": label,
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
                # Донор-сегмент: у слепка нет своих «Моделей» в tp4 (напр. Терехов) → добавляем
                # «Модели» от донора, чтобы структура совпала с другими слепками (контент в fill
                # возьмёт _build_text_from_pack у донора). Только если донор реально покрывает site_type.
                if "Модели" not in segs4 and _segment_donor("Модели", "tp4", site_type):
                    segs4.append("Модели")
                sel4 = _sel_labels(4)
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
                if sel3 is not None:
                    # tp3 — НЕ-сегментный tp: фронт шлёт data-desc=posName с префиксом имени группы
                    # ("ТГ · товары (фид) — из слепка - ТГ - Фид (товары)"), а pos["label"] = голый
                    # item.t ("ТГ - Фид (товары)"). Точное равенство никогда не совпадало → tp3
                    # молча выпадал из плана (живой баг 2026-07-06, porg-lzjk6p5m). label — всегда
                    # суффикс posName, матчим по вхождению.
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
                tp2_items = _tp_plan_names(agent, site_type, "tp2")
                if not tp2_items:
                    warnings.append("tp2 (Поиск): нет в структуре слепка — пропущен")
                    continue
                segs2 = []
                for pos in tp2_items:
                    seg = _ct_segment(pos.get("gc", ""))
                    if seg not in segs2:
                        segs2.append(seg)
                segs2 = [s for s in ("Марки", "Модели", "Общее") if s in segs2] or ["Марки"]
                sel2 = _sel_labels(2)
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
                            label = f"Поиск - {seg} - {suffix}" + (f" - {oblast}" if oblast else "")
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
                segs5 = []
                for pos in tp5_items:
                    seg = _ct_segment(pos.get("gc", ""))
                    if seg not in segs5:
                        segs5.append(seg)
                segs5 = [s for s in ("Марки", "Модели", "Общее") if s in segs5] or ["Марки"]
                sel5 = _sel_labels(5)
                for seg in segs5:
                    if sel5 is not None and seg not in sel5:
                        continue
                    modes = _slepok_tp_modes(agent, site_type, "tp5", seg)
                    if modes is None:
                        modes = ["КС"]
                    for mode in modes:
                        at = mode == "Автотаргет"
                        suffix = "Автотаргетинг" if at else "КС"
                        label = f"Поиск + Динамика + ТГ - {seg} - {suffix}" + (f" - {oblast}" if oblast else "")
                        nm, renamed = _uniq(f"tp5_cpc_site — {label}")
                        plan.append({"type": "search_gallery", "variant": v, "pay": None, "feed_id": None,
                                     "feed_name": None, "name": nm, "renamed": renamed,
                                     "budget": rs["cpc_budget"], "cpa": rs["cpc_cpa"], "tp": "tp5",
                                     "tp5_segment": seg, "autotarget": at})
                # tp5 Фиды — товарные БЕЗ ТГО + автотаргет (как боевые pavlov), если профиль ведёт.
                if "Автотаргет" in (_slepok_tp_modes(agent, site_type, "tp5", "Фиды") or []) and (sel5 is None or "Фиды" in sel5):
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
        allowed = _sq_for("6" if is_master else "7")
        for g in groups:
            if g["sq"] not in allowed:                   # уважать выбранные оси посадки (site/kviz) из набора
                continue
            cat = g["name"]
            targeting_mode = _tp67_targeting_mode(g)
            is_autotarget_name = targeting_mode == "autotarget"
            cat_base = (g.get("group") or cat or "").strip()
            interest_cat = g.get("group") or cat
            ints, ints_source = (_slepok_interest_for_struct(agent, site_type, tp_code, g)
                                 if targeting_mode == "audience" else ([], "not-audience"))
            # Если название группы — РЕАЛЬНАЯ марка/модель (tp6 Мастер: «Haval Jolion»), берём её ct
            # (ct0119) в КОДЕР → движок выберет картинку+заголовки этой модели. Тема/общее → ct0000.
            cat_ct = (_ct_for_name(cat_base) or _ct_for_name(cat) or _gc_ct(g.get("code") or "") or "ct0000")
            # FAN-OUT (CODER.md): tp7 (Товарка) фидовый → каждый фид своя кампания, имя += фид.
            # tp6 (Мастер кампаний) — без фида (одна запись).
            feed_list = ([(None, None, None)] if is_master
                         else [((f or {}).get("id"), (f or {}).get("name"), (f or {}).get("url") or "")
                               for f in feeds])
            for f_id, f_name, f_url in feed_list:
                for pay in pays:
                    base_nm = _build_name(is_master, is_autotarget_name, pay, r_code, oblast, g["sq"], cat, ct=cat_ct)
                    # Bug D fix: используем URL фида (без https://) вместо короткого имени из кабинета.
                    import re as _re_plan
                    _f_lbl = (_re_plan.sub(r'^https?://', '', f_url) if f_url else f_name)
                    if _f_lbl and not _is_site_domain_name(f_name, row.get("domain") or ""):
                        base_nm = f"{base_nm} — {_f_lbl}"
                    payload_sig = (
                        "master" if is_master else "product",
                        tp_code,
                        pay,
                        g["sq"],
                        f_id or 0,
                        cat_ct,
                        targeting_mode,
                        _tp67_kw_position_key(cat or interest_cat or ""),
                        tuple(str(x) for x in (ints or [])),
                    )
                    if payload_sig in emitted_tp67:
                        continue
                    emitted_tp67.add(payload_sig)
                    nm, renamed = _uniq(base_nm)
                    plan.append({"type": "master" if is_master else "product",
                                 "variant": ("master_" if is_master else "product_") + ("manual" if targeting_mode == "keywords" else "auto"),
                                 "pay": pay, "sq": g["sq"], "tp": tp_code,
                                 "feed_id": f_id, "feed_name": f_name, "ct": cat_ct,
                                 "coder_ct": cat_ct, "coder_brand": _ag_part1_map().get(cat_ct, ""),
                                 "name": nm, "renamed": renamed, "budget": _bud(pay), "cpa": _cpa_for(pay),
                                 "audience_cat": interest_cat, "position_name": cat,
                                 "targeting_mode": targeting_mode, "audience_source": ints_source,
                                 "structure_code": g.get("code") or "", "interest_ids": ints})

    if want_master:
        _emit_struct("tp6", True)
    if want_product:
        _emit_struct("tp7", False)
    _fal_needed = len(feeds) == 0 and (want_product or want_master)
    from .create_set_input import FALLBACK_SINGLE_FEED_KEY as _FB_KEY
    return jsonify({"login": login, "site_type": site_type, "r_code": r_code, "oblast": oblast,
                    "feeds": len(feeds), "count": len(plan),
                    "resolved_cpa": cpa, "resolved_budget": budget,   # бюджет/CPA из правил (для read-only + создания)
                    "renamed": sum(1 for p in plan if p["renamed"]), "plan": plan, "warnings": warnings,
                    "feed_alert": {
                        "needed": _fal_needed,
                        "missing": ["yandex.xml"] if _fal_needed else [],
                        "will_skip_types": (["product"] if want_product else []) + (["master"] if want_master else []) if _fal_needed else [],
                        # найден фолбэк-фид → фронт показывает кнопку «Продолжить с другим фидом»
                        # (повторный set_plan с single_feed_fallback=true строит план на нём).
                        # Имя — реального фолбэка (не всегда канонический _FB_KEY: при его
                        # отсутствии предлагается первый разрешённый фид аккаунта).
                        "fallback_feed": ({"id": _sf_fallback_id,
                                           "name": (_sf_fallback_name or _FB_KEY)}
                                          if (_fal_needed and _sf_fallback_id) else None),
                    }})

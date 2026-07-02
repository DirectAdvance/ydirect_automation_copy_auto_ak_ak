"""Редактор контента Direct — массовый поиск и замена AI-текстов.

Отдельная изолированная страница ``/direct/automation/content`` и её API
(``/direct/api/content-editor/*``). Назначение: контент (уточнения, быстрые
ссылки, заголовки/тексты объявлений) генерирует M3-пак и иногда выдаёт неверные
фразы. Этот сервис нужен для МАССОВОЙ коррекции — найти паттерн ошибки поиском
и заменить его во ВСЕХ кампаниях/группах аккаунта за один проход.

Загрузка сейчас read-only и временно использует официальный Direct API v5 по OAuth-токену
агентства как источник снимка. Запись через OAuth API v5/v501 запрещена: массовые правки
контента в кампаниях должны идти только через cookie/Grid writer после отдельного
HAR-подтверждения нужных мутаций.

Порядок правки:
  1) POST /load    — прочитать весь контент аккаунта + где что используется;
  2) POST /preview — сколько объектов затронет замена old_text→new_text (без записи);
  3) POST /replace — применить замену.

Замена по типам:
  • ad_title / ad_title2 / ad_text — только будущий ``UpdateAdaptiveTextAds`` по cookies/Grid;
  • sitelink_title — только будущий cookie/Grid flow: AddSitelinkSets + переназначение объявлений;
  • callout — только будущий cookie/Grid flow campaign→inheritableCallouts.
"""

from __future__ import annotations

from typing import Callable

from flask import jsonify, render_template, request


# ─────────────────────────── v5 low-level helpers ────────────────────────────

def _result(j: dict) -> dict:
    return j.get("result") or {}


def _v5_paginate(v5_call: Callable, svc: str, token: str, login: str,
                 params: dict, collection: str) -> tuple[list, str | None]:
    """GET всех объектов сервиса v5 с пагинацией по ``LimitedBy``.

    Возвращает ``(rows, error)``. ``collection`` — ключ массива в ``result``
    (``Campaigns`` / ``AdGroups`` / ``Ads`` / ``SitelinkSets`` / ``AdExtensions``).
    """
    rows: list = []
    offset = 0
    for _ in range(200):  # жёсткий предохранитель от бесконечного цикла
        p = dict(params)
        page = dict(p.get("Page") or {})
        page.setdefault("Limit", 10000)
        if offset:
            page["Offset"] = offset
        p["Page"] = page
        j = v5_call(svc, "get", token, login, p)
        if j.get("error"):
            e = j["error"]
            msg = e.get("error_string") if isinstance(e, dict) else str(e)
            if isinstance(e, dict) and e.get("error_detail"):
                msg = f"{msg}: {e['error_detail']}"
            return rows, str(msg)
        res = _result(j)
        rows.extend(res.get(collection) or [])
        limited_by = res.get("LimitedBy")
        if not limited_by:
            break
        offset = int(limited_by)
    return rows, None


def _strip_campaign_subfield_names(params: dict) -> dict:
    """Remove invalid campaign subtype field lists from legacy payloads.

    Direct API v5 currently accepts only a narrow set of ``TextCampaignFieldNames``.
    Older versions of this page requested fields such as ``CounterIds`` or
    ``CalloutIds`` there, which makes ``campaigns.get`` fail before the editor can
    load. The content editor needs only campaign ``Id/Name/Type`` for /load.
    """
    cleaned = dict(params or {})
    for key in (
        "TextCampaignFieldNames",
        "MobileAppCampaignFieldNames",
        "DynamicTextCampaignFieldNames",
        "CpmBannerCampaignFieldNames",
        "SmartCampaignFieldNames",
        "UnifiedCampaignFieldNames",
    ):
        cleaned.pop(key, None)
    return cleaned


# ─────────────────────────── content extraction ──────────────────────────────

def _ad_texts(ad: dict) -> dict:
    """Достаёт title/title2/text из TextAd или ResponsiveAd + SitelinkSetId."""
    body = ad.get("TextAd") or ad.get("ResponsiveAd") or ad.get("DynamicTextAd") or {}
    titles = body.get("Titles") or []
    texts = body.get("Texts") or []
    return {
        "title": body.get("Title") or ((titles[0] or {}).get("Text") if titles else "") or "",
        "title2": body.get("Title2") or ((titles[1] or {}).get("Text") if len(titles) > 1 else "") or "",
        "text": body.get("Text") or ((texts[0] or {}).get("Text") if texts else "") or "",
        "sitelink_set_id": body.get("SitelinkSetId"),
    }


def _load_account(token: str, login: str, v5_call: Callable) -> dict:
    """Читает кампании, группы, объявления, наборы ссылок и уточнения аккаунта."""
    # 1) Кампании: только Id и Name (CalloutIds недоступны через TextCampaignFieldNames в v5).
    camps, err = _v5_paginate(
        v5_call, "campaigns", token, login,
        _strip_campaign_subfield_names(
            {"SelectionCriteria": {}, "FieldNames": ["Id", "Name", "Type"]}
        ),
        "Campaigns",
    )
    if err:
        return {"error": f"campaigns.get: {err}"}
    camp_name: dict[int, str] = {}
    for c in camps:
        cid = int(c.get("Id") or 0)
        if cid:
            camp_name[cid] = c.get("Name") or ""
    campaign_ids = sorted(camp_name)
    if not campaign_ids:
        return {"callouts": [], "sitelinks": [], "ads": [], "_ads_by_set": {}}
    # Маппинг callout → кампании строим через adextensions после получения extension-ids.
    callout_to_camps: dict[str, list[int]] = {}

    # 2) Группы: имя + принадлежность кампании.
    groups, err = _v5_paginate(
        v5_call, "adgroups", token, login,
        {"SelectionCriteria": {"CampaignIds": campaign_ids},
         "FieldNames": ["Id", "Name", "CampaignId"]},
        "AdGroups",
    )
    if err:
        return {"error": f"adgroups.get: {err}"}
    ag_info: dict[int, dict] = {
        int(g.get("Id") or 0): {"name": g.get("Name") or "",
                                "campaign_id": int(g.get("CampaignId") or 0)}
        for g in groups
    }

    # 3) Объявления: заголовки/тексты + ссылка на набор быстрых ссылок.
    ads, err = _v5_paginate(
        v5_call, "ads", token, login,
         {"SelectionCriteria": {"CampaignIds": campaign_ids},
         "FieldNames": ["Id", "CampaignId", "AdGroupId", "Type"],
         "TextAdFieldNames": ["Title", "Title2", "Text", "SitelinkSetId"],
         "ResponsiveAdFieldNames": ["Titles", "Texts", "SitelinkSetId"],
         "DynamicTextAdFieldNames": ["Text", "SitelinkSetId"]},
        "Ads",
    )
    if err:
        return {"error": f"ads.get: {err}"}

    def _usage_for(cid: int, agid: int) -> dict:
        return {
            "campaign_id": cid,
            "campaign_name": camp_name.get(cid, ""),
            "adgroup_id": agid,
            "adgroup_name": (ag_info.get(agid) or {}).get("name", ""),
        }

    ads_out: list[dict] = []
    sitelink_usages: dict[str, list[dict]] = {}
    ads_by_set: dict[str, list[dict]] = {}   # set_id → [{ad_id}] для переназначения на replace
    for a in ads:
        cid = int(a.get("CampaignId") or 0)
        agid = int(a.get("AdGroupId") or 0)
        ad_id = int(a.get("Id") or 0)
        t = _ad_texts(a)
        ads_out.append({
            "ad_id": ad_id,
            "title": t["title"], "title2": t["title2"], "text": t["text"],
            "usages": [_usage_for(cid, agid)],
        })
        ssid = t["sitelink_set_id"]
        if ssid:
            sitelink_usages.setdefault(str(ssid), []).append(_usage_for(cid, agid))
            ads_by_set.setdefault(str(ssid), []).append({"ad_id": ad_id})

    # 4) Наборы быстрых ссылок. Direct API requires explicit set ids.
    sitelink_set_ids = sorted(int(sid) for sid in sitelink_usages if str(sid).isdigit())
    sets = []
    if sitelink_set_ids:
        sets, err = _v5_paginate(
            v5_call, "sitelinks", token, login,
            {"SelectionCriteria": {"Ids": sitelink_set_ids}, "FieldNames": ["Id", "Sitelinks"]},
            "SitelinkSets",
        )
        if err:
            return {"error": f"sitelinks.get: {err}"}
    sitelinks_out: list[dict] = []
    for s in sets:
        sid = str(s.get("Id") or "")
        items = [{"title": it.get("Title") or "",
                  "href": it.get("Href") or "",
                  "description": it.get("Description") or ""}
                 for it in (s.get("Sitelinks") or [])]
        title = items[0]["title"] if items else f"Набор {sid}"
        sitelinks_out.append({
            "set_id": int(s.get("Id") or 0),
            "set_title": title,
            "items": items,
            "usages": sitelink_usages.get(sid, []),
        })

    # 5) Уточнения (callouts) — adextensions type CALLOUT.
    exts, err = _v5_paginate(
        v5_call, "adextensions", token, login,
        {"SelectionCriteria": {"Types": ["CALLOUT"]},
         "FieldNames": ["Id", "Type"], "CalloutFieldNames": ["CalloutText"]},
        "AdExtensions",
    )
    if err:
        return {"error": f"adextensions.get: {err}"}
    callouts_out: list[dict] = []
    for e in exts:
        eid = str(e.get("Id") or "")
        text = (e.get("Callout") or {}).get("CalloutText") or ""
        usages = [_usage_for(cid, 0) for cid in callout_to_camps.get(eid, [])]
        callouts_out.append({"id": int(e.get("Id") or 0), "text": text, "usages": usages})

    return {"callouts": callouts_out, "sitelinks": sitelinks_out, "ads": ads_out,
            "_ads_by_set": ads_by_set}


# ───────────────────────────── replace / preview ─────────────────────────────

_AD_FIELD = {"ad_title": "title", "ad_title2": "title2", "ad_text": "text"}
_AD_API_FIELD = {"ad_title": "Title", "ad_title2": "Title2", "ad_text": "Text"}


def _match_targets(content: dict, typ: str, old_text: str) -> list[dict]:
    """Список объектов, где встречается ``old_text`` (для preview и replace)."""
    old = (old_text or "").strip()
    hits: list[dict] = []
    if typ in _AD_FIELD:
        fld = _AD_FIELD[typ]
        for ad in content.get("ads", []):
            if (ad.get(fld) or "").strip() == old:
                hits.append({"ad_id": ad["ad_id"], "usages": ad.get("usages", [])})
    elif typ == "callout":
        for co in content.get("callouts", []):
            if (co.get("text") or "").strip() == old:
                hits.append({"id": co["id"], "usages": co.get("usages", [])})
    elif typ == "sitelink_title":
        for s in content.get("sitelinks", []):
            if any((it.get("title") or "").strip() == old for it in s.get("items", [])):
                hits.append({"set_id": s["set_id"], "items": s.get("items", []),
                             "usages": s.get("usages", [])})
    return hits


def _do_replace(token: str, login: str, typ: str, old_text: str, new_text: str,
                content: dict, v5_call: Callable, v501_svc: Callable) -> dict:
    """Применяет замену. Возвращает {'replaced': N, 'errors': [...]}."""
    old = (old_text or "").strip()
    cookie_only_error = (
        "запись через OAuth Direct API отключена: редактор контента должен менять кампании "
        "только по cookies/Grid. Нужен cookie/Grid writer для этого типа объекта."
    )

    if typ in _AD_FIELD:
        targets = _match_targets(content, typ, old)
        if not targets:
            return {"replaced": 0, "errors": ["объявление с таким текстом не найдено"]}
        return {"replaced": 0, "errors": [cookie_only_error]}

    if typ == "callout":
        targets = _match_targets(content, "callout", old)
        if not targets:
            return {"replaced": 0, "errors": ["уточнение с таким текстом не найдено"]}
        return {
            "replaced": 0,
            "errors": [
                "массовая подмена уточнений временно отключена: Direct API v5 не отдаёт "
                "CalloutIds через campaigns.get/TextCampaignFieldNames; нужен Grid-путь "
                "campaign→inheritableCallouts",
            ],
        }

    if typ == "sitelink_title":
        targets = _match_targets(content, "sitelink_title", old)
        if not targets:
            return {"replaced": 0, "errors": ["набор с такой ссылкой не найден"]}
        return {"replaced": 0, "errors": [cookie_only_error]}

    return {"replaced": 0, "errors": [f"неизвестный тип: {typ}"]}


def _campaign_callout_ids(v5_call: Callable, token: str, login: str, cid: int):
    """Актуальные CalloutIds кампании.

    Kept as a compatibility hook for the later Grid implementation. Do not call
    ``campaigns.get`` with ``TextCampaignFieldNames=["CalloutIds"]`` here:
    Direct API rejects that enum and breaks the whole editor load/replace flow.
    """
    return None


def _ads_using_set(content: dict, set_id: int) -> list[int]:
    """ad_id всех объявлений, ссылающихся на набор (из свежего /load-снимка)."""
    # content хранит usages по набору, но не ad_id — переиспользуем ads-снимок.
    return [a["ad_id"] for a in content.get("_ads_by_set", {}).get(str(set_id), [])]


# ───────────────────────────── registration ─────────────────────────────────

def register_content_editor_routes(
    bp,
    access,
    *,
    victory_conn: Callable,
    token_for_login: Callable,
    direct_tokens: Callable,
    v5_call: Callable,
    v501_svc: Callable,
    default_status: str = "Контекст активно",
    exclude_directologs: list[str] | None = None,
) -> None:
    """Регистрирует изолированную страницу редактора контента и её API."""
    exclude_directologs = exclude_directologs or []

    def _token(login: str):
        tokens = direct_tokens()
        if not tokens:
            return None, None, "нет агентских токенов (loader.load_yandex_direct вернул пусто)"
        token, agency = token_for_login(login, "", tokens)
        if not token:
            return None, None, (f"ни один агентский токен не открывает аккаунт {login} — "
                                f"проверьте доступ агентства и актуальность OAuth-токенов")
        return token, agency, None

    def _load_with_index(token: str, login: str) -> dict:
        # _load_account уже строит индекс _ads_by_set (без второго запроса ads.get).
        return _load_account(token, login, v5_call)

    # ── Страница ──────────────────────────────────────────────────────────────
    @bp.route("/automation/content")
    @access
    def content_editor_page():
        return render_template("direct/content_editor.html")

    # ── Аккаунты для умного поиска ─────────────────────────────────────────────
    @bp.route("/api/content-editor/accounts")
    @access
    def ce_accounts():
        import psycopg2.extras

        status = (request.args.get("status") or default_status).strip()
        q = (request.args.get("q") or "").strip()
        where = [
            "direction='Авто'",
            "login_key IS NOT NULL",
            "login_key<>''",
            "lower(btrim(login_key)) NOT IN ('нет', 'авито')",
            "btrim(login_key) !~ '^-+$'",
        ]
        params: list = []
        if exclude_directologs:
            where.append("(directologist IS NULL OR directologist <> ALL(%s))")
            params.append(exclude_directologs)
        if status and status != "__all__":
            where.append("status=%s")
            params.append(status)
        if q:
            where.append("(domain ILIKE %s OR login_key ILIKE %s OR city ILIKE %s "
                         "OR site_type ILIKE %s)")
            params += [f"%{q}%"] * 4
        sql = (
            "SELECT login_key, domain, city, site_type, counter_number "
            "FROM public.local_gsheet_sites "
            f"WHERE {' AND '.join(where)} ORDER BY domain LIMIT 2000"
        )
        conn = victory_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql, params)
            rows = cur.fetchall()
        finally:
            conn.close()
        return jsonify({"rows": rows})

    # ── Загрузка всего контента аккаунта ───────────────────────────────────────
    @bp.route("/api/content-editor/load", methods=["POST"])
    @access
    def ce_load():
        login = ((request.json or {}).get("login") or "").strip()
        if not login:
            return jsonify({"error": "login обязателен"}), 400
        token, agency, err = _token(login)
        if err:
            return jsonify({"error": err}), 404
        content = _load_with_index(token, login)
        if content.get("error"):
            return jsonify({"error": content["error"]}), 502
        return jsonify({
            "login": login, "agency": agency,
            "callouts": content["callouts"],
            "sitelinks": content["sitelinks"],
            "ads": content["ads"],
        })

    # ── Preview: сколько объектов затронет замена (без записи) ──────────────────
    @bp.route("/api/content-editor/preview", methods=["POST"])
    @access
    def ce_preview():
        body = request.json or {}
        login = (body.get("login") or "").strip()
        typ = (body.get("type") or "").strip()
        old_text = body.get("old_text") or ""
        if not login or not typ or not old_text.strip():
            return jsonify({"error": "login, type и old_text обязательны"}), 400
        if typ not in _AD_FIELD and typ not in ("callout", "sitelink_title"):
            return jsonify({"error": f"неизвестный тип: {typ}"}), 400
        token, _, err = _token(login)
        if err:
            return jsonify({"error": err}), 404
        content = _load_with_index(token, login)
        if content.get("error"):
            return jsonify({"error": content["error"]}), 502
        hits = _match_targets(content, typ, old_text)
        usages: list[dict] = []
        for h in hits:
            usages.extend(h.get("usages", []))
        return jsonify({"objects": len(hits), "usages_count": len(usages), "usages": usages})

    # ── Replace: применить замену во всех объектах ──────────────────────────────
    @bp.route("/api/content-editor/replace", methods=["POST"])
    @access
    def ce_replace():
        body = request.json or {}
        login = (body.get("login") or "").strip()
        typ = (body.get("type") or "").strip()
        old_text = body.get("old_text") or ""
        new_text = body.get("new_text") or ""
        if not login or not typ or not old_text.strip() or not new_text.strip():
            return jsonify({"error": "login, type, old_text и new_text обязательны"}), 400
        if typ not in _AD_FIELD and typ not in ("callout", "sitelink_title"):
            return jsonify({"error": f"неизвестный тип: {typ}"}), 400
        token, _, err = _token(login)
        if err:
            return jsonify({"error": err}), 404
        content = _load_with_index(token, login)
        if content.get("error"):
            return jsonify({"error": content["error"]}), 502
        out = _do_replace(token, login, typ, old_text, new_text, content, v5_call, v501_svc)
        return jsonify(out)

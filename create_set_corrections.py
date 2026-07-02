"""Create-set correction and bid modifier helpers extracted from blueprint.py."""

from __future__ import annotations

_DEPS: dict = {}


def configure(deps: dict) -> None:
    """Bind blueprint dependencies used by correction helpers."""
    _DEPS.clear()
    _DEPS.update(deps)
    globals().update(deps)


def _load_corrections(city: str) -> dict:
    """Корректировки ставок из «Глобальных правил» (мерж город→'*', город приоритетнее).
    → {"audiences":[{name,pct}], "demographic":[{kind,key,pct}]}."""
    city = (city or "*").strip() or "*"
    out = {"audiences": [], "demographic": []}
    try:
        conn = _victory_conn()
    except Exception:  # noqa: BLE001
        return out
    try:
        cur = conn.cursor()
        cur.execute("SELECT name, pct FROM public.direct_audience_corrections WHERE city='*'")
        ad = {n: p for n, p in cur.fetchall()}
        if city != "*":
            cur.execute("SELECT name, pct FROM public.direct_audience_corrections WHERE city=%s", (city,))
            for n, p in cur.fetchall():
                ad[n] = p
        out["audiences"] = [{"name": n, "pct": int(p or 0)} for n, p in ad.items()]
        cur.execute("SELECT kind, key, pct FROM public.direct_demographic_corrections WHERE city='*'")
        dm = {(k, key): p for k, key, p in cur.fetchall()}
        if city != "*":
            cur.execute("SELECT kind, key, pct FROM public.direct_demographic_corrections WHERE city=%s", (city,))
            for k, key, p in cur.fetchall():
                dm[(k, key)] = p
        out["demographic"] = [{"kind": k, "key": key, "pct": int(p or 0)} for (k, key), p in dm.items()]
    except Exception:  # noqa: BLE001
        pass
    finally:
        conn.close()
    return out

def _account_retargeting(token: str, login: str) -> dict:
    """{имя_условия → retargeting_condition_id} аккаунта — для матчинга аудиторных корректировок."""
    if not token:
        return {}
    try:
        j = _v5_get("retargetinglists", token, login, ["Id", "Name"])
        return {a["Name"]: a["Id"] for a in (j.get("result") or {}).get("RetargetingLists", []) if a.get("Name")}
    except Exception:  # noqa: BLE001
        return {}

def _seg_key(name: str) -> tuple:
    """Ключ матчинга сегмента: (класс, остаток). Первый токен имени — это ИСТОЧНИК:
    `geo` в Глобальных правилах = плейсхолдер города → на аккаунте заменён кодом города
    (geo_all_visit_none_lal → kem_all_visit_none_lal). `self` — литерал, остаётся `self`.
    Поэтому класс = 'self' если префикс 'self', иначе 'geo' (любой код города)."""
    pfx, _, rest = (name or "").partition("_")
    return ("self" if pfx == "self" else "geo", rest)

def _corrections_by_segment(corr_audiences: list, seg_names: list) -> dict:
    """Для каждого сегмента аккаунта → процент корректировки из «Глобальных правил».
    Матчинг: сначала точный по (класс, остаток); если там пусто/0, а правило С ТЕМ ЖЕ
    остатком в ДРУГОМ классе имеет ненулевой pct — берём самое сильное (по |pct|).
    Это чинит кейс, когда исключение задано как `self_ms_all_none_minus=-100`, а на аккаунте
    сегмент `kem_ms_all_none_minus` (гео-класс): −100 всё равно доезжает. Явный ненулевой
    pct своего класса НЕ перетирается (self остаётся self). → {имя_сегмента: pct|None}."""
    by_classrest: dict = {}                       # (класс, остаток) → pct
    by_rest: dict = {}                            # остаток → [ненулевые pct]
    for a in corr_audiences:
        k = _seg_key(a.get("name") or "")
        p = int(a.get("pct") or 0)
        by_classrest[k] = p
        if p != 0:
            by_rest.setdefault(k[1], []).append(p)
    out: dict = {}
    for nm in seg_names:
        k = _seg_key(nm)
        p = by_classrest.get(k)                   # точный по классу
        if not p:                                 # None или 0 → пробуем ненулевое кросс-классом
            alt = by_rest.get(k[1])
            if alt:
                p = max(alt, key=abs)
        out[nm] = p
    return out

def _correction_bidmodifiers(campaign_id: int, corr: dict, ret_map: dict) -> list:
    """BidModifiers-items (Demographics + Retargeting) для bidmodifiers.add — только pct≠0.
    pct → BidModifier: clamp(0,1300, 100+pct). −100% → 0 (исключение).
    Аудитории матчатся по (класс, остаток): `geo_X` правила ↔ `<город>_X` аккаунта; `self_X` ↔ `self_X`."""
    items = []
    dem = []
    for d in corr.get("demographic", []):
        pct = int(d.get("pct") or 0)
        if pct == 0:
            continue
        bm = max(0, min(1300, 100 + pct))
        if d["kind"] == "age":
            dem.append({"Age": d["key"], "BidModifier": bm})
        elif d["kind"] == "gender":
            dem.append({"Gender": d["key"], "BidModifier": bm})
    if dem:
        items.append({"CampaignId": int(campaign_id), "DemographicsAdjustments": dem})
    # Идём ОТ сегментов аккаунта: для каждого — процент из правил (с кросс-классовым
    # фолбэком для исключений вроде ms). Применяем только ненулевые.
    seg_pct = _corrections_by_segment(corr.get("audiences", []), list(ret_map.keys()))
    ret = []
    for nm, rid in ret_map.items():
        pct = seg_pct.get(nm)
        if not pct:                              # None или 0 → корректировку не вешаем
            continue
        ret.append({"RetargetingConditionId": int(rid), "BidModifier": max(0, min(1300, 100 + int(pct)))})
    if ret:
        items.append({"CampaignId": int(campaign_id), "RetargetingAdjustments": ret})
    return items

def _grid_bid_modifiers(campaign_id: int, corr: dict, ret_map: dict) -> dict:
    """Корректировки ставок для GRID (bidModifiers объект ЕПК) — БЕЗ баллов. Реверс:
    HAR21 (AddCampaigns, retargeting) + JS-код из HAR23 entry 163 (demographic/demography).
    На куки-пути v5 bidmodifiers.add недоступен (стоит баллов) → ставим через Grid.

    ВАЖНО: в Grid `percent` = ДЕЛЬТА корректировки напрямую (НЕ 100+pct как в v5 BidModifier).
    age — Grid GdAgeTypeInput в живой схеме 2026-06-25: "_0_17"/"_18_24"/"_25_34"/"_35_44"/
    "_45_54"/"_55_" (не совпадает с v5/БД-ключами "AGE_*"). Поэтому здесь маппим БД-ключи
    вида AGE_18_24 → _18_24 перед отправкой в Grid, иначе AddCampaigns падает validation error.
    gender=None → корректировка для обоих полов сразу.
    → {} если нечего ставить."""
    result: dict = {}
    # ── retargeting ──────────────────────────────────────────────────────────────
    seg_pct = _corrections_by_segment(corr.get("audiences", []), list(ret_map.keys()))
    ret_adj = []
    for nm, rid in (ret_map or {}).items():
        pct = seg_pct.get(nm)
        if not pct or int(pct) <= 0:                 # Grid принимает только положительный percent
            continue
        ret_adj.append({"percent": int(pct), "retargetingConditionId": str(rid)})
    if ret_adj:
        result["bidModifierRetargeting"] = {
            "campaignId": str(campaign_id), "enabled": True,
            "adjustments": ret_adj, "type": "RETARGETING_MULTIPLIER"}
    # ── demographic (age/gender) ─────────────────────────────────────────────────
    # Реверс JS-кода HAR23/entry163: bidModifierDemographics.adjustments[].{percent,age,gender,id}.
    # percent = дельта (как retargeting); age/gender — строки Grid enum.
    # ВАЖНО: Grid GdAgeTypeInput НЕ содержит AGE_0_17 (есть в v5, нет в Grid) — пропускаем.
    # id=None → Grid сам назначит id при создании (для новых корректировок).
    _GRID_AGE_MAP = {
        "AGE_0_17": "_0_17",
        "AGE_18_24": "_18_24",
        "AGE_25_34": "_25_34",
        "AGE_35_44": "_35_44",
        "AGE_45_54": "_45_54",
        "AGE_55": "_55_",
    }
    dem_adj = []
    for d in corr.get("demographic", []):
        pct = int(d.get("pct") or 0)
        if pct <= 0:                                 # Grid Add/UpdateCampaigns валидирует percent > 0
            continue
        adj_entry: dict = {"percent": pct, "id": None}
        if d.get("kind") == "age":
            _grid_age = _GRID_AGE_MAP.get(d["key"])
            if not _grid_age:
                continue
            adj_entry["age"] = _grid_age
            adj_entry["gender"] = None          # оба пола
        elif d.get("kind") == "gender":
            adj_entry["age"] = None             # все возраста
            adj_entry["gender"] = d["key"]
        else:
            continue
        dem_adj.append(adj_entry)
    if dem_adj:
        result["bidModifierDemographics"] = {
            "campaignId": str(campaign_id), "enabled": True,
            "adjustments": dem_adj, "type": "DEMOGRAPHY_MULTIPLIER"}
    return result

def _apply_corrections(token: str, login: str, campaign_id: int, corr: dict, ret_map: dict) -> tuple:
    """Применить корректировки «Глобальных правил» к кампании (bidmodifiers.add). → (кол-во, ошибка|None)."""
    items = _correction_bidmodifiers(campaign_id, corr, ret_map)
    if not items:
        return 0, None
    j = _v5_call("bidmodifiers", "add", token, login, {"BidModifiers": items})
    if "error" in j:
        return 0, _v5_err(j)
    n = 0
    for r in (j.get("result") or {}).get("AddResults", []):
        n += len(r.get("Ids") or [])
    return n, None

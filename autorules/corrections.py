"""
Корректировки ставок по полу / возрасту / устройствам через v5 bidmodifiers API.

GET  → bidmodifiers.get на все кампании аккаунта; агрегирует в UI-формат.
SET  → bidmodifiers.set на переданные campaign_ids.
       ТОЛЬКО за явным confirm из роута. Пишет в audit_log на стороне роута.
"""
from __future__ import annotations

import requests as rqs

_V5 = "https://api.direct.yandex.com/json/v5/"

# YD API → UI-метка
_GENDER_LABELS: dict[str, str] = {
    "MALE":    "Мужчины",
    "FEMALE":  "Женщины",
    "UNKNOWN": "Неопределён",
}
_AGE_LABELS: dict[str, str] = {
    "AGE_0_17":  "0–17",
    "AGE_18_24": "18–24",
    "AGE_25_34": "25–34",
    "AGE_35_44": "35–44",
    "AGE_45_54": "45–54",
    "AGE_55":    "55+",
}
_DEVICE_LABELS: dict[str, str] = {
    "MOBILE":  "Смартфоны",
    "TABLET":  "Планшеты",
    "DESKTOP": "Десктоп",
}

_GENDER_ORDER  = ["MALE", "FEMALE"]
_AGE_ORDER     = ["AGE_18_24", "AGE_25_34", "AGE_35_44", "AGE_45_54", "AGE_55"]
_DEVICE_ORDER  = ["MOBILE", "TABLET", "DESKTOP"]


def _hdr(token: str, login: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Client-Login":  login,
        "Accept-Language": "ru",
        "Content-Type":  "application/json; charset=utf-8",
    }


def _get_campaign_ids(token: str, login: str) -> list[int]:
    """Первые 1000 ID кампаний аккаунта (активных + остановленных)."""
    r = rqs.post(
        _V5 + "campaigns",
        headers=_hdr(token, login),
        json={"method": "get", "params": {
            "SelectionCriteria": {"States": ["ON", "OFF", "SUSPENDED"]},
            "FieldNames": ["Id"],
            "Page": {"Limit": 1000},
        }},
        timeout=30,
    )
    j = r.json()
    return [c["Id"] for c in ((j.get("result") or {}).get("Campaigns") or [])]


# Публичный алиас: роут /api/ar/adjust импортирует get_campaign_ids.
get_campaign_ids = _get_campaign_ids


# ── GET корректировок ────────────────────────────────────────────────────────

def get_bid_modifiers(token: str, login: str) -> dict:
    """Возвращает текущие корректировки аккаунта в UI-формате.

    {
      "gender":  [{"segment": "MALE", "label": "Мужчины",  "adjustment": 0, ...}],
      "age":     [{"segment": "AGE_18_24", "label": "18–24", "adjustment": 0, ...}],
      "device":  [{"segment": "MOBILE", "label": "Смартфоны", "adjustment": 0, ...}],
      "campaign_ids": [...],
      "total_campaigns": int,
      "error": str|None
    }
    """
    try:
        campaign_ids = _get_campaign_ids(token, login)
    except Exception as exc:  # noqa: BLE001
        return _empty_result(error=str(exc)[:150])

    if not campaign_ids:
        return _empty_result(note="В аккаунте нет кампаний")

    # bidmodifiers.get по 100 кампаний за раз
    all_mods: list[dict] = []
    for i in range(0, min(len(campaign_ids), 500), 100):
        chunk = campaign_ids[i:i + 100]
        try:
            r = rqs.post(
                _V5 + "bidmodifiers",
                headers=_hdr(token, login),
                json={"method": "get", "params": {
                    "SelectionCriteria": {"CampaignIds": chunk},
                    "FieldNames": ["CampaignId", "DemographicsAdjustments",
                                   "MobileAdjustment", "DesktopAdjustment"],
                }},
                timeout=30,
            )
            mods = ((r.json().get("result") or {}).get("BidModifiers") or [])
            all_mods.extend(mods)
        except Exception as exc:  # noqa: BLE001
            return _empty_result(error=str(exc)[:150])

    # Агрегируем: берём первое ненулевое значение для каждого сегмента
    gender_found: dict[str, int]  = {}
    age_found:    dict[str, int]  = {}
    device_found: dict[str, int]  = {}

    for mod in all_mods:
        for da in (mod.get("DemographicsAdjustments") or []):
            if da.get("Enabled") == "NO":
                continue
            bm = da.get("BidModifier", 0)
            if bm is None:
                bm = 0
            g = da.get("Gender")
            a = da.get("Age")
            if g and g in _GENDER_LABELS and g not in gender_found and bm != 0:
                gender_found[g] = bm
            if a and a in _AGE_LABELS and a not in age_found and bm != 0:
                age_found[a] = bm

        mob = mod.get("MobileAdjustment")
        if mob and mob.get("Enabled") != "NO" and "MOBILE" not in device_found:
            bm = mob.get("BidModifier", 0) or 0
            if bm != 0:
                device_found["MOBILE"] = bm

        desk = mod.get("DesktopAdjustment")
        if desk and desk.get("Enabled") != "NO" and "DESKTOP" not in device_found:
            bm = desk.get("BidModifier", 0) or 0
            if bm != 0:
                device_found["DESKTOP"] = bm

    return {
        "gender":  [{"segment": s, "label": _GENDER_LABELS[s],
                     "adjustment": gender_found.get(s, 0)} for s in _GENDER_ORDER],
        "age":     [{"segment": s, "label": _AGE_LABELS[s],
                     "adjustment": age_found.get(s, 0)}    for s in _AGE_ORDER],
        "device":  [{"segment": s, "label": _DEVICE_LABELS[s],
                     "adjustment": device_found.get(s, 0)} for s in _DEVICE_ORDER],
        "campaign_ids":      campaign_ids[:10],
        "total_campaigns":   len(campaign_ids),
        "error": None,
    }


def _empty_result(error: str | None = None, note: str | None = None) -> dict:
    return {
        "gender":  [{"segment": s, "label": _GENDER_LABELS[s], "adjustment": 0} for s in _GENDER_ORDER],
        "age":     [{"segment": s, "label": _AGE_LABELS[s],    "adjustment": 0} for s in _AGE_ORDER],
        "device":  [{"segment": s, "label": _DEVICE_LABELS[s], "adjustment": 0} for s in _DEVICE_ORDER],
        "campaign_ids": [],
        "total_campaigns": 0,
        "error": error,
        "note":  note,
    }


# ── SET корректировок ────────────────────────────────────────────────────────

def set_bid_modifiers(token: str, login: str,
                      modifiers: list[dict], campaign_ids: list[int]) -> dict:
    """Устанавливает корректировки ставок на переданных кампаниях.

    modifiers:
        [{"type": "gender"|"age"|"device", "segment": str, "adjustment": int}, ...]

    Если adjustment == 0 — устанавливает Enabled=NO для этого сегмента.

    Возвращает: {"results": [...], "ok": int, "failed": int, "error": str|None}
    """
    if not modifiers:
        return {"results": [], "ok": 0, "failed": 0, "error": "нет корректировок"}
    if not campaign_ids:
        return {"results": [], "ok": 0, "failed": 0, "error": "нет кампаний"}

    # Строим payload BidModifiers для каждой кампании
    payload_items: list[dict] = []
    for cid in campaign_ids[:50]:  # не более 50 кампаний за раз
        item: dict = {"CampaignId": int(cid)}
        dem_adj = []
        for m in modifiers:
            seg = m.get("segment", "")
            adj = int(m.get("adjustment", 0))
            tp  = m.get("type", "")
            enabled = "YES" if adj != 0 else "NO"

            if tp == "gender" and seg in _GENDER_LABELS:
                dem_adj.append({"Gender": seg, "BidModifier": adj if adj != 0 else 100,
                                 "Enabled": enabled})
            elif tp == "age" and seg in _AGE_LABELS:
                dem_adj.append({"Age": seg, "BidModifier": adj if adj != 0 else 100,
                                 "Enabled": enabled})
            elif tp == "device" and seg == "MOBILE":
                item["MobileAdjustment"] = {"BidModifier": adj if adj != 0 else 100,
                                             "Enabled": enabled}
            elif tp == "device" and seg == "DESKTOP":
                item["DesktopAdjustment"] = {"BidModifier": adj if adj != 0 else 100,
                                              "Enabled": enabled}

        if dem_adj:
            item["DemographicsAdjustments"] = dem_adj
        if len(item) > 1:  # есть что-то кроме CampaignId
            payload_items.append(item)

    if not payload_items:
        return {"results": [], "ok": 0, "failed": 0, "error": "нет данных для отправки"}

    try:
        r = rqs.post(
            _V5 + "bidmodifiers",
            headers=_hdr(token, login),
            json={"method": "set", "params": {"BidModifiers": payload_items}},
            timeout=30,
        )
        j = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"results": [], "ok": 0, "failed": len(payload_items), "error": str(exc)[:150]}

    api_err = j.get("error")
    if api_err:
        msg = (api_err.get("error_string") or api_err.get("error_detail") or "API error")[:200]
        return {"results": [], "ok": 0, "failed": len(payload_items), "error": msg}

    set_results = ((j.get("result") or {}).get("SetResults") or [])
    results = []
    ok = 0
    failed = 0

    for item in set_results:
        cid  = item.get("CampaignId") or item.get("Id")
        errs = item.get("Errors") or []
        if errs:
            failed += 1
            results.append({"campaign_id": cid, "ok": False,
                             "error": errs[0].get("Message", "Ошибка")[:120]})
        else:
            ok += 1
            results.append({"campaign_id": cid, "ok": True})

    if not set_results:
        ok = len(payload_items)

    return {"results": results, "ok": ok, "failed": failed, "error": None}

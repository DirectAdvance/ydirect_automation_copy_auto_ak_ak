"""Ремонт коротких заголовков (<48 симв.) TEXT_AD-объявлений аккаунта porg-psm5h7q6.

Запуск на LXC 101:
  cd /opt/scripts
  DIRECT_ROLE=web python3 home/seoadvanced/direct/fix_short_titles_psm.py [--dry-run]

Механизм:
  1. v5 campaigns.get → все TEXT_CAMPAIGN-кампании логина.
  2. v5 ads.get → TextAd (Title) + ResponsiveAd (Titles[]) по этим кампаниям.
  3. extend_title_to_max (ai_agents) → попытка добить каждый Title <48 суффиксом до 48..56.
  4. TextAd: v5 ads.update (Title). ResponsiveAd: Grid UpdateAdaptiveTextAds (titles array).
  5. Read-back: подсчёт остатков, вывод bucket-статистики ДО/ПОСЛЕ.

НЕ рестартует сервисы. НЕ трогает UAC/Мастер (tp6/tp7). НЕ генерирует новый контент.
Баллов API не тратит: ads.update (TextAd) и Grid-мутация бесплатны.
"""
from __future__ import annotations

import sys
import os
import time
import argparse

_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LOGIN = "porg-psm5h7q6"
V5_URL = "https://api.direct.yandex.com/json/v5/"
_TITLE_MIN = 48   # требование Семёна
_TITLE_MAX = 56   # лимит Директа


# ── helpers ──────────────────────────────────────────────────────────────────

def _get_infra(login: str):
    """(token, cookie) для логина через существующую инфраструктуру."""
    from direct import campaign as cmc
    from direct import blueprint as bp
    tokens = bp._direct_tokens()
    token, _ = bp._token_for_login(login, "", tokens)
    cookie = cmc.pick_working_cookie(login)
    return token, cookie


def _v5_get(token: str, login: str, service: str, params: dict) -> list:
    """Пагинированный v5.get → [item, ...] (собирает все страницы)."""
    h = {"Authorization": f"Bearer {token}", "Client-Login": login,
         "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8",
         "Use-Operator-Units": "true"}
    items = []
    p = dict(params)
    p.setdefault("Page", {}).update({"Limit": 10000, "Offset": 0})
    while True:
        r = requests.post(V5_URL + service, headers=h,
                          json={"method": "get", "params": p}, timeout=90)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"v5 {service}.get: {data['error']}")
        result = data.get("result") or {}
        # разные ключи ответа: Campaigns / Ads / etc.
        page_items = next((v for v in result.values() if isinstance(v, list)), [])
        items.extend(page_items)
        limited = result.get("LimitedBy")
        if not limited or len(page_items) == 0:
            break
        p["Page"]["Offset"] = int(limited)
    return items


def _bucket(n: int) -> str:
    if n <= 8:
        return "0-8 (цель)"
    if n <= 15:
        return "9-15"
    return "16+"


def _extend(title: str, idx: int = 0, used: set | None = None) -> str:
    """extend_title_to_max из ai_agents (target_min=48)."""
    from direct.ai_agents import extend_title_to_max
    return extend_title_to_max(title, idx=idx, used=used)


# ── Grid-хелпер для ResponsiveAd ─────────────────────────────────────────────

# Mutation string (идентична _UPD_ADAPTIVE_Q из create_set_feeds.py)
_UPD_ADAPTIVE_Q = (
    "mutation UpdateAdaptiveTextAds($updateInput:GdUpdateAdaptiveTextAdsInput!){"
    "updateAdaptiveTextAds(input:$updateInput){updatedAds{id}validationResult{"
    "errors{code path params}warnings{code path params}}}}"
)


def _grid_update_responsive_direct(login: str,
                                    items: list[dict],
                                    camp_ids: list[int]) -> int:
    """Один CSRF-сеанс: RMW-чтение + UpdateAdaptiveTextAds в рамках одного GridClient.
    Избегаем двойного _bootstrap_csrf (CSRF-конфликт при двух независимых gc в одном процессе).
    items: [{id, titles}] — id как int, titles как список строк."""
    from direct import grid_finalize as gf
    gc = gf.GridClient(login)
    gc._bootstrap_csrf()

    # 1. RMW-чтение: получаем href, bodies, images, price для full-replace
    ad_ids = [int(it["id"]) for it in items if it.get("id")]
    current_map = gc.adaptive_ads_for_update(camp_ids, ad_ids)
    print(f"    [Grid] RMW current_map: {len(current_map)} ключей")

    # 2. Собираем payload: titles из items, всё остальное из RMW
    import json as _json
    upd = []
    skipped = 0
    for it in items:
        aid = int(it["id"])
        cur = current_map.get(aid)
        if not cur:
            skipped += 1
            continue
        href = cur.get("href") or ""
        bodies = list(cur.get("bodies") or [])
        image_hashes = list(dict.fromkeys(cur.get("imageHashes") or []))
        creative_ids = [str(c) for c in (cur.get("creativeIds") or []) if c]
        ad_price = cur.get("adPrice")
        payload: dict = {
            "href": href,
            "hrefParams": "",
            "titles": list(it["titles"]),   # наши расширенные заголовки
            "bodies": bodies,
            "imageHashes": image_hashes,
            "creativeIds": creative_ids,
            "id": str(aid),
        }
        if ad_price:
            payload["adPrice"] = ad_price
        _btn = cur.get("button")
        if _btn and _btn.get("action") and _btn.get("href"):
            payload["button"] = {"action": _btn["action"], "href": _btn["href"]}
        upd.append(payload)
    if skipped:
        print(f"    [Grid] skipped (нет в RMW): {skipped}")
    if not upd:
        return 0

    # 3. UpdateAdaptiveTextAds — тот же gc, тот же CSRF
    v = {"updateInput": {"adUpdateItems": upd, "saveDraft": True}}
    r = gc._post("UpdateAdaptiveTextAds", _UPD_ADAPTIVE_Q, v)
    if r.status_code == 403:
        r = gc._post("UpdateAdaptiveTextAds", _UPD_ADAPTIVE_Q, v)
    j = r.json()
    res = ((j.get("data") or {}).get("updateAdaptiveTextAds") or {})
    n = len([x for x in (res.get("updatedAds") or []) if x])
    val_errs = ((res.get("validationResult") or {}).get("errors") or [])
    if val_errs:
        print(f"    [Grid] validationResult: {len(val_errs)} ошибок, применено {n}/{len(upd)}: "
              f"{_json.dumps(val_errs[:3], ensure_ascii=False)[:300]}")
    return n


# ── main ─────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False) -> None:
    print(f"[init] login={LOGIN}, dry_run={dry_run}")
    token, cookie = _get_infra(LOGIN)
    if not token:
        print("[FATAL] нет OAuth-токена. Запуск только с DIRECT_ROLE=web.")
        sys.exit(1)

    # 1. Все TEXT_CAMPAIGN кампании
    print("[1] Получаю TEXT_CAMPAIGN кампании…")
    campaigns = _v5_get(token, LOGIN, "campaigns", {
        "SelectionCriteria": {"Types": ["TEXT_CAMPAIGN"]},
        "FieldNames": ["Id", "Name", "Type"],
    })
    camp_ids = [int(c["Id"]) for c in campaigns if c.get("Id")]
    print(f"    кампаний: {len(camp_ids)}")
    if not camp_ids:
        print("[STOP] нет TEXT_CAMPAIGN кампаний.")
        return

    # 2. Все объявления (батч по 10 кампаний, чтобы не превысить лимит ответа)
    print("[2] Читаю объявления…")
    all_ads: list[dict] = []
    CHUNK = 10
    for i in range(0, len(camp_ids), CHUNK):
        chunk = camp_ids[i:i + CHUNK]
        try:
            ads = _v5_get(token, LOGIN, "ads", {
                "SelectionCriteria": {"CampaignIds": chunk},
                "FieldNames": ["Id", "CampaignId", "AdGroupId", "Type"],
                "TextAdFieldNames": ["Title", "Title2"],
                "ResponsiveAdFieldNames": ["Titles"],
            })
            all_ads.extend(ads)
        except Exception as e:
            print(f"    [WARN] camp chunk {chunk}: {e}")
        time.sleep(0.2)
    print(f"    объявлений всего: {len(all_ads)}")

    # 3. Разобрать заголовки, подсчитать ДО
    text_ad_short: list[dict] = []   # {ad_id, title, new_title}
    resp_ad_short: list[dict] = []   # {ad_id, campaign_id, titles_new}

    before_counts: list[int] = []    # остаток (56-len) для коротких заголовков
    after_counts: list[int] = []

    for ad in all_ads:
        aid = int(ad.get("Id") or 0)
        cid = int(ad.get("CampaignId") or 0)
        if ad.get("TextAd"):
            title = str((ad["TextAd"].get("Title") or "")).strip()
            if not title:
                continue
            before_counts.append(56 - len(title))
            if len(title) < _TITLE_MIN:
                used: set = set()
                new_t = _extend(title, idx=0, used=used)
                after_counts.append(56 - len(new_t))
                if new_t != title:
                    text_ad_short.append({"ad_id": aid, "title": title, "new_title": new_t})
            else:
                after_counts.append(56 - len(title))  # не меняется
        elif ad.get("ResponsiveAd"):
            t_items = ad["ResponsiveAd"].get("Titles") or []
            used2: set = set()
            new_items = []
            changed = False
            for idx2, item in enumerate(t_items):
                t = str(item.get("Title") or item.get("Text") or "").strip()
                before_counts.append(56 - len(t))
                if t and len(t) < _TITLE_MIN:
                    new_t = _extend(t, idx=idx2, used=used2)
                    after_counts.append(56 - len(new_t))
                    if new_t != t:
                        changed = True
                        new_items.append({**item, "Title": new_t, "Text": new_t})
                        continue
                else:
                    after_counts.append(56 - len(t))
                new_items.append(item)
            if changed:
                # собрать список строк (Grid принимает список строк, не дикт)
                titles_str = [str(it.get("Title") or it.get("Text") or "").strip()
                              for it in new_items]
                resp_ad_short.append({"ad_id": aid, "campaign_id": cid, "titles": titles_str})

    print(f"\n[ДО]  коротких заголовков (<{_TITLE_MIN} симв.): "
          f"{sum(1 for x in before_counts if x > _TITLE_MAX - _TITLE_MIN)}")

    def _dist(counts: list[int]) -> str:
        b = {k: 0 for k in ["0-8 (цель)", "9-15", "16+"]}
        for c in counts:
            b[_bucket(c)] += 1
        return "  ".join(f"{k}: {v}" for k, v in b.items())

    print(f"    bucket остатка (56-len):  {_dist(before_counts)}")
    print(f"\n[ПЛАН] TextAd к правке: {len(text_ad_short)}, ResponsiveAd к правке: {len(resp_ad_short)}")

    if dry_run:
        if text_ad_short:
            print("\n[DRY] примеры TextAd:")
            for ex in text_ad_short[:5]:
                print(f"    {ex['title']!r} ({len(ex['title'])}) → {ex['new_title']!r} ({len(ex['new_title'])})")
        if resp_ad_short:
            print("\n[DRY] примеры ResponsiveAd (первый заголовок):")
            for ex in resp_ad_short[:3]:
                print(f"    ad_id={ex['ad_id']}, titles[0]={ex['titles'][0]!r} ({len(ex['titles'][0])})")
        print("\n[DRY] Реальных изменений нет.")
        return

    # 4. Обновить TextAd через v5 ads.update
    h = {"Authorization": f"Bearer {token}", "Client-Login": LOGIN,
         "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8",
         "Use-Operator-Units": "true"}
    text_updated, text_errors = 0, 0
    BATCH = 10
    for i in range(0, len(text_ad_short), BATCH):
        batch = text_ad_short[i:i + BATCH]
        payload = {"Ads": [
            {"Id": b["ad_id"], "TextAd": {"Title": b["new_title"]}}
            for b in batch
        ]}
        try:
            r = requests.post(V5_URL + "ads", headers=h,
                              json={"method": "update", "params": payload}, timeout=60)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                print(f"    [ERR] ads.update (TextAd): {data['error']}")
                text_errors += len(batch)
            else:
                for res in ((data.get("result") or {}).get("UpdateResults") or []):
                    errs = res.get("Errors") or []
                    if not errs:
                        text_updated += 1
                    else:
                        text_errors += 1
        except Exception as e:
            print(f"    [ERR] ads.update chunk: {e}")
            text_errors += len(batch)
        time.sleep(0.3)
    print(f"\n[3] TextAd обновлено: {text_updated}, ошибок: {text_errors}")

    # 5. Обновить ResponsiveAd через Grid (одна CSRF-сессия: RMW + update)
    resp_updated, resp_errors = 0, 0
    if resp_ad_short:
        resp_items = [{"id": item["ad_id"], "titles": item["titles"]} for item in resp_ad_short]
        camp_ids_resp = list({item["campaign_id"] for item in resp_ad_short if item.get("campaign_id")})
        print(f"\n[4] Grid ResponsiveAd: {len(resp_items)} items, {len(camp_ids_resp)} кампаний…")
        try:
            resp_updated = _grid_update_responsive_direct(LOGIN, resp_items, camp_ids_resp)
        except Exception as e:
            print(f"    [ERR] Grid ResponsiveAd batch: {e}")
            import traceback; traceback.print_exc()
            resp_errors = len(resp_items)
    print(f"[4] ResponsiveAd обновлено: {resp_updated}, ошибок: {resp_errors}")

    # 6. Read-back: перечитать и подсчитать остаток
    print("\n[5] Read-back (перечитываю объявления)…")
    time.sleep(2.0)   # Grid eventual-consistency lag
    all_ads2: list[dict] = []
    for i in range(0, len(camp_ids), CHUNK):
        chunk = camp_ids[i:i + CHUNK]
        try:
            ads2 = _v5_get(token, LOGIN, "ads", {
                "SelectionCriteria": {"CampaignIds": chunk},
                "FieldNames": ["Id", "Type"],
                "TextAdFieldNames": ["Title"],
                "ResponsiveAdFieldNames": ["Titles"],
            })
            all_ads2.extend(ads2)
        except Exception as e:
            print(f"    [WARN] read-back chunk {chunk}: {e}")
        time.sleep(0.2)

    after_rb: list[int] = []
    for ad in all_ads2:
        if ad.get("TextAd"):
            t = str(ad["TextAd"].get("Title") or "").strip()
            if t:
                after_rb.append(56 - len(t))
        elif ad.get("ResponsiveAd"):
            for item in (ad["ResponsiveAd"].get("Titles") or []):
                t = str(item.get("Title") or item.get("Text") or "").strip()
                if t:
                    after_rb.append(56 - len(t))

    still_short = sum(1 for x in after_rb if x > _TITLE_MAX - _TITLE_MIN)
    print(f"\n[ПОСЛЕ] коротких заголовков (<{_TITLE_MIN} симв.): {still_short}")
    print(f"    bucket остатка: {_dist(after_rb)}")
    print(f"\n[ИТОГО] исправлено TextAd: {text_updated}, ResponsiveAd: {resp_updated}")
    if still_short > 0:
        print(f"[INFO] {still_short} заголовков остались <{_TITLE_MIN} — не поддались суффиксу (47 симв. + \". \" + любой суффикс > 56).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fix short titles (<48) for psm account")
    parser.add_argument("--dry-run", action="store_true", help="только диагностика, не писать")
    args = parser.parse_args()
    run(dry_run=args.dry_run)

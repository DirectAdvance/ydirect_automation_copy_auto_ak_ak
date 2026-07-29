"""Restore-скрипт: устранение сдвига ключей в tp2/tp4-кампаниях ночного прогона 2026-07-03.

Запуск на LXC 101:
  cd /opt/scripts
  python3 home/seoadvanced/direct/restore_shift_keywords.py <login> <campaign_id> [--dry-run]

Пример для одной кампании (диагноз + правка):
  python3 home/seoadvanced/direct/restore_shift_keywords.py porg-psm5h7q6 712106824

Только диагностика (не пишет):
  python3 home/seoadvanced/direct/restore_shift_keywords.py porg-psm5h7q6 712106824 --dry-run

Механизм:
  1. groups_for_edit(campaign_id) → live groups + current keywords + adgroup_id
  2. showConditions с id → получаем keyword_id всех ключей кампании
  3. v5 keywords.delete(ids всех ключей кампании) — чистый лист
  4. group_keywords_context(group_name) → правильные ключи из пака
  5. Grid add_keywords → заливаем правильные ключи
  6. Read-back: groups_for_edit ещё раз, проверяем соответствие name→keywords

НЕ рестартует сервисы. НЕ трогает объявления или цены.
"""
from __future__ import annotations

import sys
import os
import json
import time
import argparse
import re

# LXC 101: проект лежит в /opt/scripts
# Добавляем корень чтобы импорты работали
# Корень пакета = /opt/scripts/home/seoadvanced (пакет `direct` лежит внутри него)
_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── конфигурация ────────────────────────────────────────────────────────────────
GRID_URL = "https://direct.yandex.ru/web-api/grid/api"
V5_URL   = "https://api.direct.yandex.com/json/v5/"

_SHOW_CONDITIONS_Q = (
    "query KwIds($login:String!,$cid:[String!]!){"
    "client(searchBy:{login:$login}){"
    "showConditions(input:{filter:{typeIn:[\"KEYWORD\"],campaignIdIn:$cid}"
    "statRequirements:{preset:\"TODAY\"}"
    "limitOffset:{limit:10000,offset:0}"
    "orderBy:[{order:\"DESC\",field:\"GROUP_ID\"}]})"
    "{rowset{__typename ...on GdKeyword{id keyword adGroupId}}}}}")

_ADD_KEYWORDS_Q = (
    "mutation AddKeywords($input:GdAddKeywordsInput!){"
    "addKeywords(input:$input){addedItems{adGroupId keywordId}"
    "validationResult{errors{code params path}warnings{code params path}}}}")


def _get_token_and_cookie(login: str) -> tuple[str | None, str | None]:
    """Получить (oauth_token, cookie) для login через project infrastructure."""
    try:
        from direct.core import campaign as cmc
        from direct.create import blueprint as bp  # type: ignore[attr-defined]
        tokens = bp._direct_tokens()
        token, _ = bp._token_for_login(login, "", tokens)
        cookie = cmc.pick_working_cookie(login)
        return token, cookie
    except Exception as e:
        print(f"[WARN] Не удалось получить token/cookie: {e}")
        return None, None


_GC_CACHE: dict = {}


def _grid_post(cookie: str, op: str, query: str, variables: dict) -> dict:
    """Grid-запрос через боевой GridClient (CSRF bootstrap + ретраи) — самодельный
    HTTP ловил 403 Forbidden (нет x-csrf-token)."""
    login = variables.get("login") or ""
    gc = _GC_CACHE.get(login)
    if gc is None:
        from direct.clients import grid_finalize as gf
        gc = gf.GridClient(login)
        gc._bootstrap_csrf()
        _GC_CACHE[login] = gc
    r = gc._post(op, query, variables)
    r.raise_for_status()
    return r.json()


def _v5_keywords_get(token: str, login: str, campaign_id: int) -> list[dict]:
    """v5 keywords.get → [{Id, AdGroupId, Keyword}, ...]."""
    h = {"Authorization": f"Bearer {token}", "Client-Login": login,
         "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8",
         "Use-Operator-Units": "true"}
    r = requests.post(V5_URL + "keywords", headers=h, json={
        "method": "get",
        "params": {
            "SelectionCriteria": {"CampaignIds": [campaign_id]},
            "FieldNames": ["Id", "AdGroupId", "Keyword"],
        }
    }, timeout=60)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"v5 keywords.get error: {data['error']}")
    return (data.get("result") or {}).get("Keywords") or []


def _v5_keywords_delete(token: str, login: str, keyword_ids: list[int]) -> int:
    """v5 keywords.delete → число удалённых."""
    h = {"Authorization": f"Bearer {token}", "Client-Login": login,
         "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8",
         "Use-Operator-Units": "true"}
    r = requests.post(V5_URL + "keywords", headers=h, json={
        "method": "delete",
        "params": {"SelectionCriteria": {"Ids": keyword_ids}},
    }, timeout=60)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"v5 keywords.delete error: {data['error']}")
    return len((data.get("result") or {}).get("DeleteResults") or keyword_ids)


def _grid_read_keywords(cookie: str, login: str, campaign_id: int) -> list[dict]:
    """Grid showConditions → [{id, keyword, adGroupId}] для кампании."""
    j = _grid_post(cookie, "KwIds", _SHOW_CONDITIONS_Q,
                   {"login": login, "cid": [str(campaign_id)]})
    rows = ((j.get("data") or {}).get("client") or {}).get("showConditions", {}).get("rowset") or []
    return [r for r in rows if r.get("__typename") == "GdKeyword"]


def _grid_add_keywords(cookie: str, login: str, kw_items: list[dict]) -> list[dict]:
    """Grid AddKeywords → addedItems [{adGroupId, keywordId}]."""
    # Чистим: убираем ---autotargeting и пустые.
    clean = [
        {"adGroupId": str(it["adGroupId"]), "keyword": str(it["keyword"])}
        for it in kw_items
        if str(it.get("keyword") or "").strip()
           and not str(it.get("keyword") or "").startswith("---")
           and str(it.get("adGroupId") or "").strip()
    ]
    if not clean:
        return []
    # Нужен CSRF. Используем bootstrap через Callouts query.
    _csrf = None
    _bootstrap_q = ("query Callouts($login:String!){callouts(input:{searchBy:{login:$login}"
                    "filter:{deleted:false}}){id}}")
    headers = {
        "Cookie": cookie, "dna-operation-name": "Callouts", "x-direct-api": "1",
        "x-detected-locale": "ru", "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Origin": "https://direct.yandex.ru",
    }
    r0 = requests.post(f"{GRID_URL}?operationName=Callouts&ulogin={login}",
                       json={"operationName": "Callouts", "query": _bootstrap_q, "variables": {"login": login}},
                       headers=headers, verify=False, timeout=30)
    m = re.search(r"_direct_csrf_token=([^;,\s]+)", r0.headers.get("Set-Cookie", ""))
    _csrf = r0.cookies.get("_direct_csrf_token") or (m.group(1) if m else None)

    out_items = []
    for i in range(0, len(clean), 1000):
        chunk = clean[i:i + 1000]
        hdr = dict(headers)
        hdr["dna-operation-name"] = "AddKeywords"
        if _csrf:
            hdr["x-csrf-token"] = _csrf
        r = requests.post(f"{GRID_URL}?operationName=AddKeywords&ulogin={login}",
                          json={"operationName": "AddKeywords", "query": _ADD_KEYWORDS_Q,
                                "variables": {"input": {"addItems": chunk}}},
                          headers=hdr, verify=False, timeout=60)
        r.raise_for_status()
        m2 = re.search(r"_direct_csrf_token=([^;,\s]+)", r.headers.get("Set-Cookie", ""))
        _csrf = r.cookies.get("_direct_csrf_token") or (m2.group(1) if m2 else _csrf)
        res = (r.json().get("data") or {}).get("addKeywords") or {}
        out_items.extend(res.get("addedItems") or [])
        time.sleep(0.1)
    return out_items


def _group_keywords_from_pack(login: str, campaign_name: str, adgroup_name: str) -> list[str]:
    """Вычислить правильные ключи из пака через group_keywords_context."""
    try:
        from direct.create import blueprint as bp  # type: ignore[attr-defined]
        ctx: dict = {}
        meta = {
            "campaign_id": 0,
            "campaign_name": campaign_name,
            "tp_code": "tp4" if "tp4" in (campaign_name or "").lower() else "tp2",
            "adgroup_name": adgroup_name,
            "ct": re.sub(r".*\b(ct\d+)\b.*", r"\1", adgroup_name or "") or adgroup_name,
        }
        kw_ctx = bp._group_keywords_context_fn(login, ctx, meta) or {}
        return [str(k) for k in (kw_ctx.get("keywords") or []) if str(k).strip()]
    except Exception as e:
        return []


def run(login: str, campaign_id: int, dry_run: bool = False) -> None:
    print(f"\n{'='*60}")
    print(f"RESTORE SHIFT KEYWORDS: {login} / campaign {campaign_id}")
    print(f"mode: {'DRY-RUN (только диагностика)' if dry_run else 'WRITE'}")
    print('='*60)

    token, cookie = _get_token_and_cookie(login)
    if not cookie:
        print("[ERROR] Нет cookie — прерываем.")
        return
    if not token and not dry_run:
        print("[WARN] Нет oauth_token — удаление ключей через v5 недоступно, только диагностика.")
        dry_run = True

    # ── Шаг 1: читаем live-ключи кампании через Grid showConditions ──
    print(f"\n[1] Читаем live-ключи кампании {campaign_id} через Grid…")
    kw_rows = _grid_read_keywords(cookie, login, campaign_id)
    print(f"    Найдено ключей: {len(kw_rows)}")

    # Строим: adgroup_id → [keywords], adgroup_id → [keyword_id]
    kw_by_group: dict[str, list[str]] = {}
    kid_by_group: dict[str, list[int]] = {}
    for row in kw_rows:
        gid = str(row.get("adGroupId") or "")
        phrase = str(row.get("keyword") or "").strip()
        kid = row.get("id")
        if gid and phrase:
            kw_by_group.setdefault(gid, []).append(phrase)
            if kid:
                try:
                    kid_by_group.setdefault(gid, []).append(int(kid))
                except (TypeError, ValueError):
                    pass

    # ── Шаг 2: читаем live-группы через groups_for_edit ──
    print(f"\n[2] Читаем группы через groups_for_edit…")
    try:
        from direct.clients import grid_finalize as gf
        grid_cl = gf.GridClient(login, cookie=cookie)
        groups = grid_cl.groups_for_edit(campaign_id)
    except Exception as e:
        print(f"[ERROR] groups_for_edit упал: {e}")
        return

    print(f"    Групп найдено: {len(groups)}")

    # ── Шаг 3: диагностика — показываем текущее состояние ──
    print(f"\n[3] Диагностика ключей (первые 10 групп со ссылкой на модель):")
    shown = 0
    for grp in groups:
        agid = str(grp.get("adgroup_id") or "")
        aname = grp.get("adgroup_name") or ""
        live_kw = kw_by_group.get(agid) or grp.get("keywords") or []
        if not live_kw:
            continue
        # Быстрая эвристика: первый ключ должен содержать часть имени группы (модель)
        ct_match = re.search(r"ct\d+\s+(.*?)(?:\s*\(|$)", aname)
        model_hint = (ct_match.group(1) if ct_match else aname).lower()
        first_kw = (live_kw[0] if live_kw else "").lower()
        looks_wrong = model_hint and len(model_hint) > 3 and model_hint not in first_kw
        marker = "  [WRONG?]" if looks_wrong else ""
        if shown < 10 or looks_wrong:
            print(f"    adg {agid} | {aname[:45]:45} | kw[0]={live_kw[0][:40]}{marker}")
            shown += 1

    # ── Шаг 4 (если не dry-run): удалить все ключи → пересчитать из пака ──
    if dry_run:
        print(f"\n[DRY-RUN] Удаление пропущено. Всего keyword_ids доступно: "
              f"{sum(len(v) for v in kid_by_group.values())} (v5 ids из Grid showConditions)")
        print("Для реального запуска: python3 restore_shift_keywords.py {login} {campaign_id}")
        return

    # Шаг 4a: собрать все keyword_ids
    all_kw_ids: list[int] = []
    for ids in kid_by_group.values():
        all_kw_ids.extend(ids)

    if token and all_kw_ids:
        print(f"\n[4a] Удаляем {len(all_kw_ids)} ключей через v5 keywords.delete…")
        try:
            deleted = _v5_keywords_delete(token, login, all_kw_ids)
            print(f"     Удалено: {deleted}")
        except Exception as e:
            print(f"[ERROR] v5 keywords.delete: {e}")
            print("        Прерываем — ключи не удалены, пишем остановку.")
            return
    elif not token:
        print("[WARN] Нет token — пропускаем удаление. Ключи будут дублированы!")
    else:
        print("[4a] Нет ключей для удаления (кампания уже чистая).")

    time.sleep(1.0)

    # Шаг 4b: пересчитать правильные ключи из пака и залить через Grid AddKeywords
    print(f"\n[4b] Добавляем правильные ключи из пака…")
    kw_items: list[dict] = []
    camp_name = groups[0].get("campaign_name") or "" if groups else ""
    for grp in groups:
        agid = str(grp.get("adgroup_id") or "")
        if not agid:
            continue
        aname = grp.get("adgroup_name") or ""
        correct_kw = _group_keywords_from_pack(login, camp_name, aname)
        for k in correct_kw:
            if k.strip() and not k.startswith("---"):
                kw_items.append({"adGroupId": agid, "keyword": k})

    if not kw_items:
        print("[WARN] group_keywords_context вернул 0 ключей — возможно пак недоступен.")
        print("       Попробуй вызвать execute_keywords_repair через API после ручной очистки.")
        return

    print(f"     Планируем залить: {len(kw_items)} ключей в {len(groups)} групп")
    added = _grid_add_keywords(cookie, login, kw_items)
    print(f"     AddKeywords вернул: {len(added)} adde dItems")

    time.sleep(1.5)

    # ── Шаг 5: read-back — проверяем X35-группу ──
    print(f"\n[5] Read-back: проверяем ключи после заливки…")
    kw_rows2 = _grid_read_keywords(cookie, login, campaign_id)
    kw_by_group2: dict[str, list[str]] = {}
    for row in kw_rows2:
        gid = str(row.get("adGroupId") or "")
        phrase = str(row.get("keyword") or "").strip()
        if gid and phrase:
            kw_by_group2.setdefault(gid, []).append(phrase)

    print(f"    Ключей после: {len(kw_rows2)}")
    # Показываем те же группы что были неверными
    ok_count, wrong_count = 0, 0
    for grp in groups[:20]:
        agid = str(grp.get("adgroup_id") or "")
        aname = grp.get("adgroup_name") or ""
        live_kw = kw_by_group2.get(agid) or []
        ct_match = re.search(r"ct\d+\s+(.*?)(?:\s*\(|$)", aname)
        model_hint = (ct_match.group(1) if ct_match else aname).lower()
        first_kw = (live_kw[0] if live_kw else "").lower()
        looks_wrong = model_hint and len(model_hint) > 3 and model_hint not in first_kw
        if looks_wrong:
            print(f"    [STILL WRONG] adg {agid} | {aname[:40]} | kw[0]={live_kw[0][:40] if live_kw else 'EMPTY'}")
            wrong_count += 1
        elif live_kw:
            ok_count += 1

    print(f"\n    OK: {ok_count}  WRONG/EMPTY: {wrong_count}")
    if wrong_count == 0:
        print("  RESTORE SUCCEEDED")
    else:
        print("  PARTIAL — дорасследовать вручную оставшиеся группы")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Restore shifted keywords in tp2/tp4 campaigns")
    parser.add_argument("login", help="Yandex Direct login (porg-psm5h7q6 etc.)")
    parser.add_argument("campaign_id", type=int, help="Campaign ID to restore")
    parser.add_argument("--dry-run", action="store_true", help="Только диагностика, без записи")
    args = parser.parse_args()
    run(args.login, args.campaign_id, dry_run=args.dry_run)

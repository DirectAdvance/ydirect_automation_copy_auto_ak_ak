"""Ремонт уточнений (callouts) для porg-psm5h7q6.

Задача: создать пул уточнений из слепка scherbakova + привязать ко всем
не-UAC кампаниям аккаунта (tp7/UAC уточнения не поддерживают — пропускаем).

Запуск на LXC 101:
  cd /opt/scripts
  DIRECT_ROLE=web python3 home/seoadvanced/direct/fix_callouts_psm.py [--dry-run]

Алгоритм:
  1. Читаем тексты уточнений через kp.gather(slepok, site_type, tp) для tp1/tp2/tp5.
  2. Нормализуем + trim по слову до ≤25 символов; длинные отбрасываем.
  3. v5 adextensions.get → дедуп по тексту (case-insensitive) с уже существующими.
  4. v5 adextensions.add → создаём недостающие (частичные ошибки пропускаем).
  5. Семантический дедуп пула + кап 20 id.
  6. ДО: считаем кампании с calloutIds через Grid.
  7. GridClient.set_campaign_callouts → привязываем к не-UAC кампаниям (v5-видимым).
  8. ПОСЛЕ: повторный Grid read → count.

НЕ рестартует сервисы. НЕ трогает UAC/tp7.
"""
from __future__ import annotations

import sys
import os
import argparse

_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LOGIN = "porg-psm5h7q6"
SLEPOK = "scherbakova"
V5_URL = "https://api.direct.yandex.com/json/v5/"
_CALLOUT_MAX = 25     # лимит символов на уточнение (Яндекс)
_POOL_CAP = 20        # максимум id в пуле


# ── helpers ──────────────────────────────────────────────────────────────────

def _get_infra(login: str):
    """(token, cookie, ctx) для логина."""
    from direct import campaign as cmc
    from direct import blueprint as bp
    tokens = bp._direct_tokens()
    token, _ = bp._token_for_login(login, "", tokens)
    cookie = cmc.pick_working_cookie(login)
    ctx = bp._account_ctx(login)
    return token, cookie, ctx


def _v5_call(token: str, login: str, svc: str, method: str, params: dict) -> dict:
    h = {"Authorization": f"Bearer {token}", "Client-Login": login,
         "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8",
         "Use-Operator-Units": "true"}
    r = requests.post(V5_URL + svc, headers=h,
                      json={"method": method, "params": params}, timeout=60)
    r.raise_for_status()
    return r.json()


def _collect_pack_callouts(site_type: str) -> list[str]:
    """Собрать уточнения из слепка scherbakova для всех tp, нормализовать."""
    from direct import kontent_pack as kp
    from direct.text_norm import _trim_clean

    all_texts: list[str] = []
    for tp in ("tp1", "tp2", "tp5"):
        pack = kp.gather(SLEPOK, site_type, tp)
        for ct, data in pack.items():
            for c in data.get("callouts") or []:
                if c and c not in all_texts:
                    all_texts.append(c)

    # Нормализация + trim до ≤25 симв по слову; длинные отброс
    normed: list[str] = []
    seen: set[str] = set()
    for t in all_texts:
        t = str(t or "").strip()
        if not t:
            continue
        if len(t) > _CALLOUT_MAX:
            t = _trim_clean(t, _CALLOUT_MAX).strip()
        if not t or len(t) > _CALLOUT_MAX:
            continue
        lk = t.lower()
        if lk in seen:
            continue
        seen.add(lk)
        normed.append(t)
    return normed


def _get_existing_callouts(token: str, login: str) -> dict[str, int]:
    """Существующие уточнения аккаунта → {lower_text: AdExtensionId}."""
    out: dict[str, int] = {}
    offset = 0
    while True:
        j = _v5_call(token, login, "adextensions", "get", {
            "SelectionCriteria": {"Types": ["CALLOUT"]},
            "FieldNames": ["Id", "Type"],
            "CalloutFieldNames": ["CalloutText"],
            "Page": {"Limit": 1000, "Offset": offset},
        })
        res = j.get("result") or {}
        for ext in res.get("AdExtensions", []):
            txt = ((ext.get("Callout") or {}).get("CalloutText") or "").strip()
            if txt:
                out[txt.lower()] = int(ext["Id"])
        limited = res.get("LimitedBy")
        if not limited:
            break
        offset = int(limited)
    return out


def _create_missing_callouts(token: str, login: str,
                              texts: list[str], existing: dict[str, int],
                              dry_run: bool) -> dict[str, int]:
    """Создать недостающие уточнения; вернуть обновлённый словарь existing."""
    to_create = [t for t in texts if t.lower() not in existing]
    print(f"  переиспользуем: {len(texts) - len(to_create)}, создаём: {len(to_create)}")
    if not to_create:
        return existing

    if dry_run:
        print(f"  [DRY] создали бы {len(to_create)} уточнений: {to_create[:5]}")
        return existing

    created = 0
    errors = 0
    for i in range(0, len(to_create), 50):
        chunk = to_create[i:i + 50]
        j = _v5_call(token, login, "adextensions", "add", {
            "AdExtensions": [{"Callout": {"CalloutText": t}} for t in chunk]
        })
        for t, r in zip(chunk, (j.get("result") or {}).get("AddResults", [])):
            if isinstance(r, dict) and r.get("Id"):
                existing[t.lower()] = int(r["Id"])
                created += 1
            else:
                errs = (r or {}).get("Errors") or []
                if errs:
                    print(f"    SKIP '{t}': {errs[0].get('Message', '')[:60]}")
                errors += 1
    print(f"  создано: {created}, ошибок пропущено: {errors}")
    return existing


def _simple_dedup_ids(texts: list[str], existing: dict[str, int], cap: int) -> list[int]:
    """Простой дедуп по lower-тексту → список id ≤cap.
    Использует lower() без _callout_semantic_key (нет globals-зависимости)."""
    seen: set[str] = set()
    ids: list[int] = []
    for t in texts:
        lk = t.lower()
        if lk in seen:
            continue
        seen.add(lk)
        if lk in existing:
            ids.append(existing[lk])
        if len(ids) >= cap:
            break
    return ids


def _count_campaigns_with_callouts(gc, cids: list[int]) -> int:
    """Сколько кампаний из cids имеют непустой inheritableCallouts.calloutIds."""
    payloads = gc._read_unified_campaign_update_payloads(cids)
    count = 0
    for _, p in payloads.items():
        co = (p.get("inheritableCallouts") or {}).get("calloutIds") or []
        if co:
            count += 1
    return count


def main():
    ap = argparse.ArgumentParser(description="Repair callouts for porg-psm5h7q6")
    ap.add_argument("--dry-run", action="store_true", help="не применять изменения")
    args = ap.parse_args()
    dry_run = args.dry_run

    print(f"[init] login={LOGIN}, slepok={SLEPOK}, dry_run={dry_run}")

    # ── инфраструктура ──────────────────────────────────────────────────────
    print("\n[1] Получаю token + cookie + ctx...")
    token, cookie, ctx = _get_infra(LOGIN)
    if not token:
        print("  ERROR: нет токена — выход")
        sys.exit(1)
    if not cookie:
        print("  WARN: нет куки — Grid-операции недоступны")
    site_type = ctx.get("site_type") or "Мультибренд"
    print(f"  site_type={site_type}, token=ok, cookie={'ok' if cookie else 'НЕТ'}")

    # ── тексты из пака ──────────────────────────────────────────────────────
    print("\n[2] Читаю тексты уточнений из пака scherbakova...")
    pack_texts = _collect_pack_callouts(site_type)
    print(f"  из пака: {len(pack_texts)} текстов (после norm/trim)")
    if not pack_texts:
        print("  ERROR: пак вернул 0 текстов — выход (пак недоступен или пуст)")
        sys.exit(1)
    for t in pack_texts[:8]:
        print(f"    [{len(t)}] {t}")
    if len(pack_texts) > 8:
        print(f"    ... ещё {len(pack_texts) - 8}")

    # ── существующие уточнения аккаунта ─────────────────────────────────────
    print("\n[3] Читаю существующие уточнения аккаунта...")
    existing = _get_existing_callouts(token, LOGIN)
    print(f"  в аккаунте: {len(existing)} уточнений")

    # ── создаём недостающие ─────────────────────────────────────────────────
    print("\n[4] Создаю недостающие уточнения...")
    existing = _create_missing_callouts(token, LOGIN, pack_texts, existing, dry_run)

    # ── семантический дедуп пула ─────────────────────────────────────────────
    print("\n[5] Дедуп + кап пула...")
    pool_ids = _simple_dedup_ids(pack_texts, existing, cap=_POOL_CAP)
    print(f"  пул: {len(pool_ids)} id → {pool_ids[:8]}...")

    if not pool_ids:
        print("  ERROR: пустой пул id — выход")
        sys.exit(1)

    # ── список не-UAC кампаний ──────────────────────────────────────────────
    print("\n[6] Получаю не-UAC кампании (v5)...")
    j = _v5_call(token, LOGIN, "campaigns", "get", {
        "FieldNames": ["Id", "Name", "Type"],
        "SelectionCriteria": {},
        "Page": {"Limit": 1000}
    })
    campaigns = (j.get("result") or {}).get("Campaigns", [])
    cids = [c["Id"] for c in campaigns]
    print(f"  не-UAC кампаний: {len(campaigns)} (UAC невидимы в v5 — пропускаем)")
    for c in campaigns[:3]:
        print(f"    Id {c['Id']} {c['Type']} {c['Name'][:50]}")

    if not cids or not cookie:
        print("  ERROR: нет кампаний или куки — выход")
        sys.exit(1)

    from direct.grid_finalize import GridClient
    gc = GridClient(LOGIN, cookie=cookie)

    # ── ДО ─────────────────────────────────────────────────────────────────
    print("\n[7] Читаю состояние ДО (Grid calloutIds)...")
    before = _count_campaigns_with_callouts(gc, cids)
    print(f"  ДО: {before}/{len(cids)} кампаний с calloutIds")

    # ── привязка ────────────────────────────────────────────────────────────
    print(f"\n[8] {'[DRY] ' if dry_run else ''}Привязываю {len(pool_ids)} уточнений к {len(cids)} кампаниям...")
    if not dry_run:
        updated = gc.set_campaign_callouts(cids, pool_ids)
        print(f"  updatedCampaigns: {len(updated)}")
    else:
        print(f"  [DRY] set_campaign_callouts({len(cids)} cids, {pool_ids[:5]}...)")

    # ── ПОСЛЕ ───────────────────────────────────────────────────────────────
    if not dry_run:
        print("\n[9] Читаю состояние ПОСЛЕ (Grid calloutIds)...")
        after = _count_campaigns_with_callouts(gc, cids)
        print(f"  ПОСЛЕ: {after}/{len(cids)} кампаний с calloutIds")
    else:
        after = before  # dry-run: нет изменений

    # ── итог ────────────────────────────────────────────────────────────────
    print("\n══════════════════════════════════════════")
    print(f"  ДО:    {before}/{len(cids)} с уточнениями")
    print(f"  ПОСЛЕ: {after}/{len(cids)} с уточнениями")
    print(f"  Пул:   {len(pool_ids)} callout id")
    print(f"  UAC (tp7): {23 - len(cids)} кампаний пропущено")
    print("══════════════════════════════════════════")


if __name__ == "__main__":
    main()

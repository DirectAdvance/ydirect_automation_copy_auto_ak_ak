"""Чистка чужемодельных ключей в tp2/tp5 Модели-группах аккаунта porg-psm5h7q6.

Запуск на LXC 101:
  cd /opt/scripts
  DIRECT_ROLE=web python3 home/seoadvanced/direct/fix_foreign_model_kw_psm.py [--dry-run]

Механизм:
  1. Grid groups_for_edit → все группы кампаний psm.
  2. Фильтр: тип ПОИСК (tp2/tp5), сегмент «Модели» (из имени группы ct*).
  3. Для каждой группы: модель ct → _foreign_model_discriminators → дискриминирующие токены
     других моделей той же марки.
  4. v5 keywords.get (кампания) → phrase→id маппинг.
  5. Найти ключи из группы, содержащие хотя бы один дискриминирующий токен.
  6. v5 keywords.delete этих keyword_id.
  7. Read-back: подсчёт удалённых по группам, контрольный пример ДО/ПОСЛЕ.

НЕ трогает: группы «Марки», «Общее», UAC, non-psm аккаунты.
Баллов API не тратит: keywords.delete бесплатен.
"""
from __future__ import annotations

import sys
import os
import time
import argparse
import re
from collections import defaultdict

_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LOGIN = "porg-psm5h7q6"
V5_URL = "https://api.direct.yandex.com/json/v5/"

# Regexp для определения tp из имени кампании (совпадает с _TP_RE в campaign_spec_audit.py)
_TP_RE = re.compile(r"^\s*tp(\d+)_", re.IGNORECASE)   # «tp2_cpc_site …» или «tp5_cpa_site …»
_CT_RE = re.compile(r"\bct\d+", re.IGNORECASE)   # без trailing \b: ct0031_ тоже матчится
_SEARCH_TPS = {2, 5}


def _tp_of(name: str) -> int | None:
    m = _TP_RE.match(str(name or ""))  # match (начало строки)
    return int(m.group(1)) if m else None


def _ct_of(name: str) -> str:
    m = _CT_RE.search(str(name or ""))
    return m.group(0).lower() if m else "ct0000"


def _get_infra(login: str):
    from direct import campaign as cmc
    from direct import blueprint as bp
    tokens = bp._direct_tokens()
    token, _ = bp._token_for_login(login, "", tokens)
    cookie = cmc.pick_working_cookie(login)
    return token, cookie


def _v5_search_camp_ids(token: str, login: str) -> list[int]:
    """v5 campaigns.get → все TEXT_CAMPAIGN id логина."""
    h = {"Authorization": f"Bearer {token}", "Client-Login": login,
         "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8",
         "Use-Operator-Units": "true"}
    r = requests.post(V5_URL + "campaigns", headers=h, json={
        "method": "get",
        "params": {"SelectionCriteria": {"Types": ["TEXT_CAMPAIGN"]},
                   "FieldNames": ["Id"]},
    }, timeout=60)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"v5 campaigns.get: {data['error']}")
    return [int(c["Id"]) for c in ((data.get("result") or {}).get("Campaigns") or []) if c.get("Id")]


def _grid_groups(login: str, camp_ids: list[int]) -> list[dict]:
    """Grid groups_for_edit для указанных кампаний."""
    from direct import grid_finalize as gf
    gc = gf.GridClient(login)
    gc._bootstrap_csrf()
    return gc.groups_for_edit(camp_ids) or []


def _v5_keywords_for_camps(token: str, login: str, camp_ids: list[int]) -> dict[int, list[dict]]:
    """v5 keywords.get → {adgroup_id: [{Id, Keyword}, ...]}."""
    h = {"Authorization": f"Bearer {token}", "Client-Login": login,
         "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8",
         "Use-Operator-Units": "true"}
    result: dict[int, list[dict]] = defaultdict(list)
    for cid in camp_ids:
        try:
            r = requests.post(V5_URL + "keywords", headers=h, json={
                "method": "get",
                "params": {"SelectionCriteria": {"CampaignIds": [cid]},
                           "FieldNames": ["Id", "AdGroupId", "Keyword"]},
            }, timeout=60)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                print(f"    [WARN] keywords.get camp {cid}: {data['error']}")
                continue
            for kw in ((data.get("result") or {}).get("Keywords") or []):
                gid = int(kw.get("AdGroupId") or 0)
                kid = int(kw.get("Id") or 0)
                phrase = str(kw.get("Keyword") or "").strip()
                if gid and kid and phrase:
                    result[gid].append({"id": kid, "phrase": phrase})
        except Exception as e:
            print(f"    [WARN] keywords.get camp {cid}: {e}")
        time.sleep(0.2)
    return result


def _v5_keywords_delete(token: str, login: str, keyword_ids: list[int]) -> int:
    h = {"Authorization": f"Bearer {token}", "Client-Login": login,
         "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8",
         "Use-Operator-Units": "true"}
    deleted = 0
    for i in range(0, len(keyword_ids), 10000):
        chunk = keyword_ids[i:i + 10000]
        r = requests.post(V5_URL + "keywords", headers=h, json={
            "method": "delete",
            "params": {"SelectionCriteria": {"Ids": chunk}},
        }, timeout=60)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"v5 keywords.delete: {data['error']}")
        deleted += len((data.get("result") or {}).get("DeleteResults") or chunk)
    return deleted


def run(dry_run: bool = False) -> None:
    print(f"[init] login={LOGIN}, dry_run={dry_run}")

    token, cookie = _get_infra(LOGIN)
    if not token:
        print("[FATAL] нет OAuth-токена. Запуск только с DIRECT_ROLE=web.")
        sys.exit(1)

    # Импорт дискриминатора из text_gen
    from direct.text_gen import _foreign_model_discriminators as fmd, _model_subtokens as mst

    # Импорт ct_name маппинга через blueprint (оно уже проинициализировано при импорте)
    from direct import blueprint as bp
    # blueprint не экспортирует эти функции как глобальные — используем kontent_pack напрямую
    ct_name: dict = {}
    ct_model: dict = {}
    try:
        from direct import kontent_pack as kp_mod
        ct_name = kp_mod._ag_part1_map() if hasattr(kp_mod, "_ag_part1_map") else {}
    except Exception as e:
        print(f"    [WARN] ag_part1_map: {e}")
    try:
        from direct import kontent_pack as kp_mod2
        ct_model = kp_mod2.feeds_ct_model() if hasattr(kp_mod2, "feeds_ct_model") else {}
    except Exception as e:
        print(f"    [WARN] feeds_ct_model: {e}")
    # fallback: возьмём brand_models_catalog для маппинга ct→model
    _brand_catalog: dict = {}
    try:
        import json
        from pathlib import Path
        _cat_path = Path(__file__).parent / "brand_models_catalog.json"
        _brand_catalog = json.loads(_cat_path.read_text("utf-8"))
    except Exception as e:
        print(f"    [WARN] brand_models_catalog: {e}")

    def _model_of(ct: str) -> str:
        raw = ct_name.get(ct) or ct_model.get(ct) or ""
        if raw and raw != "Авто":
            return raw
        return ""

    # 1. Получаем campaign_ids через v5, затем groups через Grid
    print("[1] Получаю кампании через v5…")
    camp_ids_all = _v5_search_camp_ids(token, LOGIN)
    print(f"    TEXT_CAMPAIGN: {len(camp_ids_all)}")

    print("[1b] Читаю группы через Grid…")
    groups = _grid_groups(LOGIN, camp_ids_all)
    print(f"    групп всего: {len(groups)}")

    # 2. Фильтр: поисковые Модели-группы
    search_model: list[dict] = []
    for g in groups:
        tp = _tp_of(g.get("campaign_name") or "")
        if tp not in _SEARCH_TPS:
            continue
        ct = _ct_of(g.get("adgroup_name") or "")
        # Используем ct_segment из blueprint если доступен
        try:
            from direct.blueprint import _ct_segment
            seg = _ct_segment(ct)
        except Exception:
            # fallback: ct0000 → Общее, иначе Модели (упрощённо)
            seg = "Общее" if ct == "ct0000" else "Модели"
        if seg != "Модели":
            continue
        model = _model_of(ct)
        if not model:
            continue
        disc = fmd(model)
        if not disc:
            continue  # единственная модель марки — нечему дискриминировать
        search_model.append({**g, "_ct": ct, "_model": model, "_disc": disc})

    print(f"    Модели-групп с дискриминаторами: {len(search_model)}")
    if not search_model:
        print("[STOP] нет подходящих групп.")
        return

    # 3. Читаем keyword id+phrase для задействованных кампаний
    camp_ids_needed = list({int(g.get("campaign_id") or 0)
                            for g in search_model if g.get("campaign_id")})
    print(f"[2] Читаю ключи для {len(camp_ids_needed)} кампаний через v5 keywords.get…")
    kw_by_group = _v5_keywords_for_camps(token, LOGIN, camp_ids_needed)

    # 4. Для каждой группы найти чужемодельные ключи
    to_delete: list[int] = []
    report_rows: list[dict] = []

    for g in search_model:
        gid = int(g.get("adgroup_id") or 0)
        disc = g["_disc"]
        model = g["_model"]
        ct = g["_ct"]
        kws = kw_by_group.get(gid, [])
        if not kws:
            continue
        foreign = [kw for kw in kws if mst(kw["phrase"]) & disc]
        if not foreign:
            continue
        foreign_ids = [kw["id"] for kw in foreign]
        to_delete.extend(foreign_ids)
        own = [kw for kw in kws if kw not in foreign]
        report_rows.append({
            "adgroup_id": gid,
            "campaign_id": int(g.get("campaign_id") or 0),
            "campaign_name": g.get("campaign_name") or "",
            "model": model,
            "ct": ct,
            "total_kws": len(kws),
            "foreign_count": len(foreign),
            "own_count": len(own),
            "foreign_examples": [kw["phrase"] for kw in foreign[:5]],
        })

    print(f"\n[ПЛАН] групп с чужемодельными ключами: {len(report_rows)}")
    print(f"        keyword_id к удалению: {len(set(to_delete))}")
    if not report_rows:
        print("[STOP] чужемодельных ключей не найдено.")
        return

    # Вывод по группам
    for row in report_rows:
        print(f"\n  camp={row['campaign_id']} gid={row['adgroup_id']} {row['model']} ct={row['ct']}")
        print(f"    всего ключей: {row['total_kws']}, чужемодельных: {row['foreign_count']}, своих: {row['own_count']}")
        print(f"    примеры чужих: {row['foreign_examples']}")

    if dry_run:
        print("\n[DRY] Реальных удалений нет.")
        return

    # 5. v5 keywords.delete
    to_delete_dedup = list(dict.fromkeys(to_delete))
    print(f"\n[3] Удаляю {len(to_delete_dedup)} ключей через v5 keywords.delete…")
    try:
        deleted = _v5_keywords_delete(token, LOGIN, to_delete_dedup)
        print(f"    удалено: {deleted}")
    except Exception as e:
        print(f"    [FATAL] delete: {e}")
        sys.exit(1)

    # 6. Read-back: проверяем остаток
    print("\n[4] Read-back (пауза 2с)…")
    time.sleep(2.0)
    kw_after = _v5_keywords_for_camps(token, LOGIN, camp_ids_needed)

    print("\n[ПОСЛЕ]")
    total_foreign_after = 0
    for row in report_rows:
        gid = row["adgroup_id"]
        disc = fmd(row["model"])
        kws_after = kw_after.get(gid, [])
        foreign_after = [kw for kw in kws_after if mst(kw["phrase"]) & disc]
        total_foreign_after += len(foreign_after)
        print(f"  gid={gid} {row['model']}: ключей={len(kws_after)}, "
              f"чужих={len(foreign_after)} (было {row['foreign_count']})")

    print(f"\n[ИТОГО] удалено keyword_id: {deleted}  |  чужемодельных после: {total_foreign_after}")
    if total_foreign_after > 0:
        print("[WARN] часть чужемодельных ключей осталась (не попали в keywords.get или already deleted).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Delete foreign-model keywords from psm MODEL groups")
    parser.add_argument("--dry-run", action="store_true", help="только диагностика")
    args = parser.parse_args()
    run(dry_run=args.dry_run)

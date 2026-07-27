"""Шаг переноса adPrice для copy-постпроцесса."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Optional

from .copy_context import CopyCtx, _noop_log
from .copy_step_utils import _chunks, _rj, _v5_add_err, _wj


def _merge_cheaper(prev: tuple | None, new: tuple) -> tuple:
    """Мердж пар (current, old) из нескольких целевых фидов: побеждает МИНИМАЛЬНЫЙ current
    (цена «от X»), при равном — больший old (зачёркнутая). Локальный аналог create_set_feeds._merge_price
    — чтобы не тянуть ещё одну инъекцию ради 3 строк."""
    try:
        nc, no = int(new[0] or 0), int(new[1] or 0)
    except (TypeError, ValueError, IndexError):
        return prev if prev is not None else (0, 0)
    if prev is None:
        return (nc, no)
    pc, po = int(prev[0] or 0), int(prev[1] or 0)
    if nc < pc or (nc == pc and no > po):
        return (nc, no)
    return (pc, po)


_KODER_SEGMENT_RE = re.compile(r"_a(?:on|off)_n\d{3}_r\d{4}_", re.I)


def _clean_group_brand(name: str) -> str:
    """Бренд/модель группы из её имени для матча с ключами прайс-карты фида.
    Реальные слепки называют группы по-разному («01 | Changan Uni-K | Москва», «Changan | С пробегом»,
    «Автокредит»): берём ПЕРВЫЙ содержательный сегмент (пропуская чисто-порядковые «01» и срезая ведущий
    индекс внутри сегмента) — обычно это «<Марка> <Модель>» без города/условия (они в следующих
    сегментах). Дальше матч делает group_ad_price/_ad_price_for_brand (полное имя → без года → первое
    слово=марка). Марки в имени нет («Автокредит») → в фиде её не будет → фолбэк-минимум группы.

    Фикс дефект-3: create-движок именует группы «<КОДЕР> — <Бренд>» (кодер первый). Кодер-сегмент
    распознаётся по шаблону `_a(on|off)_n###_r####_` и пропускается → берём следующий сегмент = бренд."""
    for p in re.split(r"[|/·—–]", (name or "")):
        p = p.strip()
        if not p or re.fullmatch(r"\d{1,3}[.)]?", p):
            continue                                   # пустой / чисто-порядковый сегмент («01», «12.»)
        if _KODER_SEGMENT_RE.search(p):
            continue                                   # кодер-сегмент ct####_aoff_n000_r####_ → пропускаем
        p = re.sub(r"^\s*\d{1,3}[.)]?\s+", "", p)      # ведущий индекс внутри сегмента («01 Changan»)
        return re.sub(r"\s+", " ", p).strip()
    return ""


def step_prices(ctx: CopyCtx) -> dict:
    """П.8. Проставить НОВЫЕ РЕАЛЬНЫЕ цены из ФИДА ЦЕЛЕВОГО аккаунта на созданные копированием
    адаптивные (комбинаторные) объявления через Grid adPrice (UpdateAdaptiveTextAds, куки target,
    без баллов). Старые цены НЕ переносим — читаем офферы из target-фида.

    Механика (тот же контур, что create-set, но контент/бренд читаем из снапшота+Grid, а не из
    in-memory meta): (1) целевые фиды = значения maps['feeds'] (пофидовая замена feed_map учтена
    предзасевом id_maps); (2) прайс-карта — _grid_feed_offer_prices по каждому целевому фиду, мердж
    (мин цена); пусто → фолбэк account_offer_prices (мердж всех фидов target); (3) tgt_ad → бренд по
    снапшоту (maps['ads']→ads.json.AdGroupId→adgroups.json.Name); (4) читаем созданные адаптивные
    объявления target через Grid adaptive_ads_for_update (id/href/titles/bodies/imageHashes);
    (5) цена = group_ad_price(prices, brand, 'Модели'); нет марки/модели в фиде → цена ПУСТАЯ (тумблер
    выключен), adPrice не выставляется; (6) _grid_set_ad_prices.

    Идемпотентно/безопасно: нет grid/хелперов/прайса/адаптивных ads → пропуск с отчётом, job не падает.
    ShoppingAd тут не трогаем — товарные берут цену из фида нативно; adPrice применим к адаптивным."""
    rep = {"feeds": [], "ads_scanned": 0, "priced": 0, "by_brand": 0,
           "no_price": 0, "errors": []}
    if ctx.grid is None:
        rep["errors"].append("нет grid-клиента — adPrice пропущены")
        return rep
    if not (ctx.feed_offer_prices and ctx.group_ad_price and ctx.set_ad_prices):
        rep["errors"].append("нет прайс-хелперов (инъекция) — adPrice пропущены")
        return rep
    login = ctx.target_login

    # 1) Целевые фиды: значения maps['feeds'] — целевые FeedId после копирования/пофидовой замены.
    feed_ids: list[int] = []
    for v in (ctx.maps.get("feeds") or {}).values():
        try:
            fid = int(v)
        except (TypeError, ValueError):
            continue
        if fid > 0 and fid not in feed_ids:
            feed_ids.append(fid)

    # 2) Прайс-карта из ФИДА target-аккаунта (мердж целевых фидов; на конфликте — минимум).
    prices: dict = {}
    for fid in feed_ids:
        try:
            fp = ctx.feed_offer_prices(login, fid) or {}
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"feed {fid}: {str(e)[:150]}")
            continue
        if fp:
            rep["feeds"].append({"feed_id": fid, "offers": len(fp)})
            for k, val in fp.items():
                prices[k] = _merge_cheaper(prices.get(k), val)
    if not prices and ctx.account_offer_prices:
        # Фолбэк: мердж ВСЕХ фидов target-аккаунта (тот же источник — target, не source).
        href = str((ctx.body or {}).get("target_domain") or "")
        try:
            prices = ctx.account_offer_prices(login, href) or {}
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"account prices: {str(e)[:150]}")
            prices = {}
        if prices:
            rep["feeds"].append({"feed_id": "account_merge", "offers": len(prices)})
    if not prices:
        ctx.log("adPrice: прайс из target-фида пуст — шаг пропущен (товарные берут цену из фида нативно)")
        return rep

    # 3) tgt_ad_id → бренд (имя исходной группы) по снапшоту.
    rev_ads: dict[int, str] = {}
    for src_id, tgt_id in (ctx.maps.get("ads") or {}).items():
        if str(tgt_id).isdigit():
            rev_ads[int(tgt_id)] = str(src_id)
    src_ad_group: dict[str, str] = {}
    for a in _rj(ctx.src_dir / "ads.json"):
        aid, gid = str(a.get("Id") or ""), str(a.get("AdGroupId") or "")
        if aid and gid:
            src_ad_group[aid] = gid
    group_name: dict[str, str] = {}
    for g in _rj(ctx.src_dir / "adgroups.json"):
        gid = str(g.get("Id") or "")
        if gid:
            group_name[gid] = str(g.get("Name") or "").strip()
    brand_for_ad: dict[int, str] = {}
    for tgt_ad_id, src_ad_id in rev_ads.items():
        gid = src_ad_group.get(src_ad_id)
        brand_for_ad[tgt_ad_id] = _clean_group_brand(group_name.get(gid, "")) if gid else ""

    # 4) Созданные адаптивные объявления target (id/href/titles/bodies/imageHashes) через Grid.
    camp_ids = [int(v) for v in (ctx.maps.get("campaigns") or {}).values() if str(v).isdigit()]
    ad_ids = list(rev_ads.keys())
    if not camp_ids or not ad_ids:
        ctx.log("adPrice: нет целевых campaign/ad id — пропуск")
        return rep
    try:
        live_ads = ctx.grid.adaptive_ads_for_update(camp_ids, ad_ids) or {}
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"read adaptive ads: {str(e)[:180]}")
        return rep

    # 5) Маппинг ad→бренд→цена (нет марки/модели в фиде → цена пустая), собираем payload.
    items: list[dict] = []
    for aid, st in live_ads.items():
        if not (st.get("titles") and st.get("bodies")):
            continue                                   # не адаптивное / без контента — adPrice неприменим
        rep["ads_scanned"] += 1
        brand = brand_for_ad.get(int(aid), "")
        cur, old, mode = 0, 0, ""
        if brand:
            try:
                cur, old = ctx.group_ad_price(prices, brand, "Модели")
            except Exception:  # noqa: BLE001
                cur, old = 0, 0
            if cur:
                mode = "by_brand"
        if not cur:
            rep["no_price"] += 1
            continue
        rep[mode] += 1
        item = {"id": int(aid), "href": st.get("href") or "",
                "titles": st.get("titles") or [], "bodies": st.get("bodies") or [],
                "image_hashes": st.get("imageHashes") or [],
                "current": int(cur), "old": int(old or 0)}
        if isinstance(st.get("button"), dict) and st["button"].get("action"):
            item["button"] = st["button"]
        items.append(item)

    # 6) Проставляем adPrice пачкой через Grid (куки target, без баллов).
    if items:
        try:
            rep["priced"] = int(ctx.set_ad_prices(login, items, apply_combo_button=False) or 0)
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"set prices: {str(e)[:180]}")
    ctx.log(f"adPrice из target-фида: проставлено {rep['priced']}/{len(items)} адаптивных "
            f"(по марке {rep['by_brand']}, без цены {rep['no_price']}; фидов {len(rep['feeds'])})")
    return rep

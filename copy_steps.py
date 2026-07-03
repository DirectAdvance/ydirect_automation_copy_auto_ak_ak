"""copy_steps — под-сервисы постобработки копировщика РК (ФАЗА 1).

Каждый пункт доработки = отдельная функция-шаг с ЕДИНЫМ контрактом:
    step_*(ctx: CopyCtx, ...) -> dict   # отчёт-dict, НИКОГДА не бросает наружу.

Контракт ctx несёт всё, что нужно шагу: workdir/src_dir, id_maps, target_login/agency,
target-токен v5, target Grid-клиент (по куке), PromoClient, а также инъекции хелперов из
blueprint (log, v5_call, enabled_minus_places) — чтобы НЕ импортировать blueprint (иначе
циклический импорт). Каждый шаг идемпотентный, обёрнут в try/except и пишет ошибки в отчёт,
не роняя весь job. При любой неудаче — фолбэк на прежнее поведение (описан в шаге).

Пункты:
  П.14 step_age_bidmods       — стандартные возрастные корректировки −100% (<18, 18–24) через v5.
  П.13 step_disabled_places   — наш стандартный минус-список площадок (disabledPlaces) на РСЯ через Grid.
  П.11 step_attach_callouts   — уточнения по ИСХОДНОЙ связи campaign→callouts (не blind union).
  П.10 step_attach_promos     — привязка промо по исходной связи campaign→promo (в т.ч. при 2+ промо).
  pull_source_campaign_assets — pull-фаза: снять с источника campaign→callouts/promo (Grid), сохранить.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


def _noop_log(_m: str) -> None:
    return None


def _rj(path: Path):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def _wj(path: Path, data) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


@dataclass
class CopyCtx:
    """Единый контекст под-сервисов копировщика."""
    target_login: str
    target_agency: str
    src_dir: Path
    workdir: Path
    body: dict
    maps: dict
    grid: object = None                 # target GridClient (по куке target)
    promo_client: object = None         # promo.PromoClient(target)
    target_token: str = ""              # v5 токен target-аккаунта
    log: Callable[[str], None] = _noop_log
    v5_call: Optional[Callable] = None  # blueprint._v5_call(svc, method, token, login, params)
    enabled_minus_places: Optional[Callable] = None  # blueprint._enabled_minus_places()
    # П.8 (adPrice): прайс-хелперы из create_set_feeds (через blueprint-обёртки, уже configure()).
    feed_offer_prices: Optional[Callable] = None   # (login, feed_id) -> {ключ: (cur, old)}
    account_offer_prices: Optional[Callable] = None  # (login, href) -> {ключ: (cur, old)} (мердж всех фидов)
    group_ad_price: Optional[Callable] = None      # (prices, brand, seg) -> (cur, old)
    set_ad_prices: Optional[Callable] = None       # (login, items) -> число обновлённых
    # ФАЗА 3b (п.4 адаптивы / п.12 видео): source-Grid для чтения состава с ИСТОЧНИКА, гео-пары
    # job'а, RMW-апдейтер адаптивов (сохраняет target href/adPrice/видео), видео-аплоуд по куки.
    source_login: str = ""
    source_grid: object = None                      # source GridClient (куки источника, чтение)
    geo_pairs: Optional[list] = None                # [(old,new)] морфопары гео-замены (те же, что job)
    update_adaptive_ads: Optional[Callable] = None  # _grid_update_adaptive_ads(login, items, camp_ids) RMW
    video_upload_client: object = None              # UacClient target (upload_video_creative по куки)
    video_file_resolver: Optional[Callable] = None  # (meta) -> локальный путь к mp4 источника | None


# ── Определение «есть ли сеть» у исходной кампании (для П.13) ──────────────────

_CAMPAIGN_STRUCT_KEYS = (
    "TextCampaign", "DynamicTextCampaign", "SmartCampaign",
    "CpmBannerCampaign", "UnifiedAdCampaign",
)


def source_has_network(campaign: dict) -> bool:
    """True, если у исходной кампании включена сеть (РСЯ): блок Network стратегии не SERVING_OFF.
    Работает по snapshot campaigns.json (v5). Для tp1/tp3/сетевых вернёт True, для поиск-only — False."""
    if not isinstance(campaign, dict):
        return False
    for key in _CAMPAIGN_STRUCT_KEYS:
        block = campaign.get(key) or {}
        strat = block.get("BiddingStrategy") or {}
        net = strat.get("Network") or {}
        btype = str(net.get("BiddingStrategyType") or "").strip().upper()
        if btype and btype != "SERVING_OFF":
            return True
    return False


# ── pull-фаза: снять с источника campaign→callouts и campaign→promo ────────────

def pull_source_campaign_assets(source_grid, source_campaign_ids, src_dir: Path,
                                *, log: Callable[[str], None] = _noop_log) -> dict:
    """Зафиксировать ИСХОДНУЮ связь campaign→[callout_ids] и campaign→promoExtensionId.

    Пишет src_dir/campaign_callouts.json и src_dir/campaign_promos.json (ключ — str(source_campaign_id)).
    Источник данных — Grid источника (inheritableCallouts/promoExtension через CampaignsEditData).
    Фолбэк callouts: если Grid недоступен — деривация из ads.json (ad-level AdExtensions CALLOUT,
    сгруппированные по CampaignId). Promo без Grid снять неоткуда → пустой файл (постпроцесс
    откатится на прежнее единичное поведение)."""
    rep = {"callouts_campaigns": 0, "promos_campaigns": 0, "source": "", "errors": []}
    src_dir = Path(src_dir)
    ids = []
    for raw in source_campaign_ids or []:
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            continue
        if cid > 0 and cid not in ids:
            ids.append(cid)

    callouts_map: dict[str, list[str]] = {}
    promos_map: dict[str, str] = {}
    grid_ok = False
    if source_grid is not None and ids:
        try:
            payloads = source_grid._read_unified_campaign_update_payloads(ids)
            for cid, p in (payloads or {}).items():
                co_ids = [str(x) for x in ((p.get("inheritableCallouts") or {}).get("calloutIds") or []) if str(x).strip()]
                if co_ids:
                    callouts_map[str(cid)] = co_ids
                promo_id = p.get("promoExtensionId")
                if promo_id and str(promo_id).strip() and str(promo_id) != "0":
                    promos_map[str(cid)] = str(promo_id)
            grid_ok = True
            rep["source"] = "grid"
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"grid read: {str(e)[:200]}")

    if not grid_ok:
        # Фолбэк: ad-level callouts из ads.json, сгруппированные по CampaignId.
        rep["source"] = "ads_fallback"
        try:
            wanted = set(str(c) for c in ids)
            for a in _rj(src_dir / "ads.json"):
                camp = str(a.get("CampaignId") or "")
                if wanted and camp not in wanted:
                    continue
                exts = []
                for k in ("TextAd", "DynamicTextAd"):
                    exts += (a.get(k) or {}).get("AdExtensions") or []
                co = [str(e.get("AdExtensionId")) for e in exts
                      if isinstance(e, dict) and e.get("Type") == "CALLOUT" and e.get("AdExtensionId")]
                if co:
                    bucket = callouts_map.setdefault(camp, [])
                    for x in co:
                        if x not in bucket:
                            bucket.append(x)
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"ads fallback: {str(e)[:200]}")

    try:
        _wj(src_dir / "campaign_callouts.json", callouts_map)
        _wj(src_dir / "campaign_promos.json", promos_map)
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"write: {str(e)[:200]}")
    rep["callouts_campaigns"] = len(callouts_map)
    rep["promos_campaigns"] = len(promos_map)
    log(f"pull source assets ({rep['source']}): callouts у {rep['callouts_campaigns']} кампаний, "
        f"promo у {rep['promos_campaigns']} кампаний")
    return rep


# ── П.14: стандартные возрастные корректировки −100% (<18, 18–24) ──────────────

_AGE_TARGETS = ("AGE_0_17", "AGE_18_24")  # <18 и 18–24 («младше 25»); BidModifier=0 == −100% (v5)
# Grid GdAgeTypeInput enum (не совпадает с v5 AGE_*). Конвенция значения тут — «дельта» (−100 == −100%);
# Grid-слой конвертирует в мультипликатор 0..1300 (−100 → percent:0 == исключить возраст).
_AGE_TARGETS_GRID = {"_0_17": -100, "_18_24": -100}


def step_age_bidmods(ctx: CopyCtx) -> dict:
    """П.14. Проставить −100% на «младше 18» и «младше 25» на каждую скопированную кампанию.

    ФАЗА 3b (ретро-правка): теперь GRID-FIRST — ``grid.set_campaign_age_bidmods`` через RMW
    UpdateCampaigns, БЕЗ v5-баллов. Grid percent — МУЛЬТИПЛИКАТОР 0..1300 (min=0), не знаковая
    дельта: −100% == percent:0. Здесь передаём конвенцию «дельта» (−100), а delta→multiplier
    (100+pct, clamp 0..1300) конвертирует Grid-слой в set_campaign_age_bidmods.

    v5 bidmodifiers.add остаётся ТОЛЬКО как фолбэк для кампаний, которые Grid не покрыл
    (копейки баллов — 2 корректировки; документировано). Идемпотентно на обоих путях:
    Grid пропускает уже-выставленные возрасты, v5 читает bidmodifiers.get перед add."""
    rep = {"campaigns": 0, "added": 0, "skipped_existing": 0, "via": "",
           "grid_ok": 0, "v5_fallback": 0, "errors": []}
    camp_ids = sorted({int(x) for x in (ctx.maps.get("campaigns") or {}).values() if str(x).isdigit()})
    rep["campaigns"] = len(camp_ids)
    if not camp_ids:
        return rep

    # 1) Grid-first (НОЛЬ v5-баллов). satisfied = кампании, где возраст −100% гарантированно стоит.
    grid_done: set[int] = set()
    if ctx.grid is not None and hasattr(ctx.grid, "set_campaign_age_bidmods"):
        try:
            satisfied = ctx.grid.set_campaign_age_bidmods(camp_ids, dict(_AGE_TARGETS_GRID))
            grid_done = {int(x) for x in (satisfied or []) if str(x).isdigit()}
            rep["grid_ok"] = len(grid_done)
            rep["via"] = "grid"
            ctx.log(f"возрастные −100% через Grid (0 баллов): {rep['grid_ok']}/{len(camp_ids)} кампаний")
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"grid age bidmods: {str(e)[:200]}")

    # 2) v5-фолбэк ТОЛЬКО для кампаний, которые Grid не покрыл (копейки баллов).
    remaining = [c for c in camp_ids if c not in grid_done]
    if remaining:
        token = ctx.target_token
        if not token or not ctx.v5_call:
            if rep["grid_ok"]:
                ctx.log(f"возрастные −100%: {len(remaining)} кампаний вне Grid, v5-токена нет — "
                        f"добить нечем (некритично, корректировка косметическая)")
            else:
                rep["errors"].append("нет v5-токена/вызова и Grid не покрыл — возрастные пропущены")
        else:
            for cid in remaining:
                try:
                    existing = _existing_demographic_ages(ctx, token, cid)
                    missing = [a for a in _AGE_TARGETS if a not in existing]
                    if not missing:
                        rep["skipped_existing"] += 1
                        continue
                    item = {"CampaignId": cid,
                            "DemographicsAdjustments": [{"Age": a, "BidModifier": 0} for a in missing]}
                    j = ctx.v5_call("bidmodifiers", "add", token, ctx.target_login, {"BidModifiers": [item]})
                    err = _v5_add_err(j)
                    if err:
                        rep["errors"].append(f"camp {cid} v5: {err[:200]}")
                        continue
                    rep["added"] += len(missing)
                    rep["v5_fallback"] += 1
                except Exception as e:  # noqa: BLE001
                    rep["errors"].append(f"camp {cid} v5: {str(e)[:200]}")
            if rep["v5_fallback"]:
                rep["via"] = "grid+v5" if rep["grid_ok"] else "v5"
                ctx.log(f"возрастные −100%: Grid не покрыл {len(remaining)} → v5-фолбэк добил "
                        f"{rep['v5_fallback']} (копейки баллов; уже были {rep['skipped_existing']})")
    return rep


def _existing_demographic_ages(ctx: CopyCtx, token: str, cid: int) -> set[str]:
    """Возрасты, у которых уже есть DemographicsAdjustment на кампании (чтобы не дублировать)."""
    ages: set[str] = set()
    j = ctx.v5_call("bidmodifiers", "get", token, ctx.target_login, {
        "SelectionCriteria": {"CampaignIds": [int(cid)], "Levels": ["CAMPAIGN"]},
        "FieldNames": ["Id", "CampaignId", "Type"],
        "DemographicsAdjustmentFieldNames": ["Age", "Gender", "BidModifier"],
    })
    for bm in ((j.get("result") or {}).get("BidModifiers") or []):
        da = bm.get("DemographicsAdjustment") or {}
        age = str(da.get("Age") or "").strip()
        # Учитываем только «чистые» возрастные (без пола) — наши стандартные тоже без Gender.
        if age and not da.get("Gender"):
            ages.add(age)
    return ages


def _v5_add_err(j: dict) -> str:
    """Ошибка из ответа v5 add (общая error ИЛИ per-item Errors). '' если всё ок."""
    if not isinstance(j, dict):
        return "нераспознанный ответ"
    e = j.get("error")
    if isinstance(e, dict):
        return e.get("error_string") or e.get("error_detail") or json.dumps(e, ensure_ascii=False)[:200]
    for it in ((j.get("result") or {}).get("AddResults") or []):
        errs = it.get("Errors") or []
        if errs:
            return "; ".join(str(x.get("Message") or x.get("Details") or x) for x in errs)[:220]
    return ""


# ── П.13: наш стандартный минус-список площадок (disabledPlaces) на РСЯ ─────────

def step_disabled_places(ctx: CopyCtx) -> dict:
    """П.13. Применить наш стандартный disabledPlaces (как в create-set для tp1/РСЯ) к
    скопированным кампаниям, у которых есть сеть (network). Поиск-only кампании не трогаем.
    Через Grid (куки target). Фолбэк: пустой стандартный список / нет grid → шаг пропускается."""
    rep = {"network_campaigns": 0, "updated": 0, "hosts": 0, "skipped": [], "errors": []}
    if ctx.grid is None:
        rep["errors"].append("нет grid-клиента — disabledPlaces пропущены")
        return rep
    if not ctx.enabled_minus_places:
        rep["errors"].append("нет источника минус-площадок — пропуск")
        return rep
    try:
        hosts = list(ctx.enabled_minus_places() or [])
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"minus places: {str(e)[:180]}")
        hosts = []
    rep["hosts"] = len(hosts)
    if not hosts:
        ctx.log("disabledPlaces: стандартный минус-список площадок пуст — пропуск")
        return rep

    camp_map = ctx.maps.get("campaigns") or {}
    target_ids = []
    for c in _rj(ctx.src_dir / "campaigns.json"):
        src_id = str(c.get("Id") or "")
        tgt = camp_map.get(src_id)
        if not tgt or not str(tgt).isdigit():
            continue
        if not source_has_network(c):
            rep["skipped"].append({"campaign": src_id, "reason": "search-only (нет сети)"})
            continue
        rep["network_campaigns"] += 1
        target_ids.append(int(tgt))
    if not target_ids:
        ctx.log("disabledPlaces: сетевых кампаний среди скопированных нет — пропуск")
        return rep
    try:
        updated = ctx.grid.set_campaign_disabled_places(target_ids, hosts)
        rep["updated"] = len(updated or target_ids)
        ctx.log(f"disabledPlaces: {rep['hosts']} площадок на {rep['updated']} РСЯ-кампаний")
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"grid set: {str(e)[:220]}")
    return rep


# ── П.11: уточнения по ИСХОДНОЙ связи campaign→callouts (не blind union) ────────

def step_attach_callouts(ctx: CopyCtx, per_campaign_cap: int = 8) -> dict:
    """П.11. Привязать к КАЖДОЙ целевой кампании только ремапленные callout-id ЕЁ исходной
    кампании (по campaign_callouts.json + maps['callouts']), а не общий union всех уточнений.

    Фолбэк: если source-связь недоступна для кампании (нет файла / нет записи / пусто после
    ремапа) — вешаем union всех созданных callout-id (прежнее поведение), с логом."""
    rep = {"per_campaign": 0, "fallback_union": 0, "attached_campaigns": 0, "errors": []}
    if ctx.grid is None:
        rep["errors"].append("нет grid-клиента — уточнения не привязаны")
        return rep
    callout_map = ctx.maps.get("callouts") or {}          # src_callout_id → tgt_callout_id
    camp_map = ctx.maps.get("campaigns") or {}            # src_campaign_id → tgt_campaign_id
    union_ids = list(dict.fromkeys(int(x) for x in callout_map.values() if str(x).isdigit()))
    if not camp_map:
        return rep
    src_links = _rj(ctx.src_dir / "campaign_callouts.json")
    src_links = src_links if isinstance(src_links, dict) else {}

    for src_cid, tgt_cid in camp_map.items():
        if not str(tgt_cid).isdigit():
            continue
        tgt_cid = int(tgt_cid)
        raw_co = src_links.get(str(src_cid)) or []
        remapped = list(dict.fromkeys(
            int(callout_map[str(x)]) for x in raw_co
            if str(x) in callout_map and str(callout_map[str(x)]).isdigit()
        ))
        use_ids, mode = (remapped, "per_campaign") if remapped else (union_ids, "fallback_union")
        if not use_ids:
            continue
        try:
            ctx.grid.set_campaign_callouts([tgt_cid], use_ids[:per_campaign_cap])
            rep["attached_campaigns"] += 1
            rep[mode] += 1
            if mode == "fallback_union":
                ctx.log(f"уточнения camp {tgt_cid}: source-связь недоступна → union ({len(use_ids)})")
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"camp {tgt_cid}: {str(e)[:180]}")
    ctx.log(f"уточнения по кампаниям: по исходной связи {rep['per_campaign']}, "
            f"фолбэк-union {rep['fallback_union']} (всего {rep['attached_campaigns']})")
    return rep


# ── П.10: привязка промо по исходной связи (в т.ч. при 2+ промо) ───────────────

def step_attach_promos(ctx: CopyCtx, created_promo_ids: list[int]) -> dict:
    """П.10. Привязать каждое промо к ЕГО исходным кампаниям через maps['promotions']+maps['campaigns']
    (updateCampaignsPromoExtension, ?ulogin=target уже в PromoClient). Работает и при 2+ промо.

    Фолбэк: если campaign_promos.json пуст/недоступен — прежнее поведение (привязать единственное
    промо ко всем кампаниям, если промо ровно одно)."""
    rep = {"mode": "", "attached_campaigns": 0, "promos_attached": 0, "errors": []}
    pc = ctx.promo_client
    if pc is None:
        rep["errors"].append("нет promo-клиента — привязка промо пропущена")
        return rep
    promo_map = ctx.maps.get("promotions") or {}          # src_promo_id → tgt_promo_id
    camp_map = ctx.maps.get("campaigns") or {}            # src_campaign_id → tgt_campaign_id
    src_links = _rj(ctx.src_dir / "campaign_promos.json")
    src_links = src_links if isinstance(src_links, dict) else {}

    # Собираем tgt_promo_id → [tgt_campaign_id] по исходной связи.
    by_promo: dict[int, list[int]] = {}
    for src_cid, src_promo in src_links.items():
        tgt_cid = camp_map.get(str(src_cid))
        tgt_promo = promo_map.get(str(src_promo))
        if not (tgt_cid and tgt_promo and str(tgt_cid).isdigit() and str(tgt_promo).isdigit()):
            continue
        by_promo.setdefault(int(tgt_promo), []).append(int(tgt_cid))

    if by_promo:
        rep["mode"] = "per_source_link"
        for tgt_promo, cids in by_promo.items():
            cids = list(dict.fromkeys(cids))
            try:
                attach = pc.attach(int(tgt_promo), cids)
                err = _promo_attach_err(attach)
                if err:
                    rep["errors"].append(f"promo {tgt_promo}: {err[:180]}")
                    continue
                rep["promos_attached"] += 1
                rep["attached_campaigns"] += len(cids)
            except Exception as e:  # noqa: BLE001
                rep["errors"].append(f"promo {tgt_promo}: {str(e)[:180]}")
        ctx.log(f"промо по исходной связи: {rep['promos_attached']} промо на "
                f"{rep['attached_campaigns']} привязок")
        return rep

    # Фолбэк: прежнее единичное поведение.
    rep["mode"] = "fallback_single"
    unique_promos = list(dict.fromkeys(int(x) for x in (created_promo_ids or []) if str(x).isdigit()))
    camp_ids = [int(x) for x in camp_map.values() if str(x).isdigit()]
    if len(unique_promos) == 1 and camp_ids:
        try:
            attach = pc.attach(unique_promos[0], camp_ids)
            err = _promo_attach_err(attach)
            if err:
                rep["errors"].append(f"promo attach: {err[:180]}")
            else:
                rep["promos_attached"] = 1
                rep["attached_campaigns"] = len(camp_ids)
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"promo attach: {str(e)[:180]}")
    else:
        ctx.log(f"промо: исходная связь недоступна и промо не единично ({len(unique_promos)}) — "
                f"привязка пропущена (фолбэк)")
    return rep


def _promo_attach_err(attach: dict) -> str:
    if not isinstance(attach, dict):
        return ""
    errors = (((attach.get("data") or {}).get("updateCampaignsPromoExtension") or {})
              .get("validationResult") or {}).get("errors") or attach.get("errors")
    return json.dumps(errors, ensure_ascii=False)[:200] if errors else ""


# ── П.8: adPrice из ФИДА TARGET-аккаунта на созданные адаптивные объявления ─────

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


def _clean_group_brand(name: str) -> str:
    """Бренд/модель группы из её имени для матча с ключами прайс-карты фида.
    Реальные слепки называют группы по-разному («01 | Changan Uni-K | Москва», «Changan | С пробегом»,
    «Автокредит»): берём ПЕРВЫЙ содержательный сегмент (пропуская чисто-порядковые «01» и срезая ведущий
    индекс внутри сегмента) — обычно это «<Марка> <Модель>» без города/условия (они в следующих
    сегментах). Дальше матч делает group_ad_price/_ad_price_for_brand (полное имя → без года → первое
    слово=марка). Марки в имени нет («Автокредит») → в фиде её не будет → фолбэк-минимум группы."""
    for p in re.split(r"[|/·—–]", (name or "")):
        p = p.strip()
        if not p or re.fullmatch(r"\d{1,3}[.)]?", p):
            continue                                   # пустой / чисто-порядковый сегмент («01», «12.»)
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
    (5) цена = group_ad_price(prices, brand, 'Модели'); нет марки/модели в фиде → фолбэк минимальная
    цена группы group_ad_price(prices,'','') (как в create-set для Общее); (6) _grid_set_ad_prices.

    Идемпотентно/безопасно: нет grid/хелперов/прайса/адаптивных ads → пропуск с отчётом, job не падает.
    ShoppingAd тут не трогаем — товарные берут цену из фида нативно; adPrice применим к адаптивным."""
    rep = {"feeds": [], "ads_scanned": 0, "priced": 0, "by_brand": 0,
           "by_min_fallback": 0, "no_price": 0, "errors": []}
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

    # 5) Маппинг ad→бренд→цена (+ фолбэк на минимум группы), собираем payload.
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
        if not cur:                                    # марки/модели нет в фиде → минимум группы (как create-set Общее)
            try:
                cur, old = ctx.group_ad_price(prices, "", "")
            except Exception:  # noqa: BLE001
                cur, old = 0, 0
            if cur:
                mode = "by_min_fallback"
        if not cur:
            rep["no_price"] += 1
            continue
        rep[mode] += 1
        items.append({"id": int(aid), "href": st.get("href") or "",
                      "titles": st.get("titles") or [], "bodies": st.get("bodies") or [],
                      "image_hashes": st.get("imageHashes") or [],
                      "current": int(cur), "old": int(old or 0)})

    # 6) Проставляем adPrice пачкой через Grid (куки target, без баллов).
    if items:
        try:
            rep["priced"] = int(ctx.set_ad_prices(login, items) or 0)
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"set prices: {str(e)[:180]}")
    ctx.log(f"adPrice из target-фида: проставлено {rep['priced']}/{len(items)} адаптивных "
            f"(по марке {rep['by_brand']}, фолбэк-минимум {rep['by_min_fallback']}, "
            f"без цены {rep['no_price']}; фидов {len(rep['feeds'])})")
    return rep


# ── ФАЗА 3c п.2: КЛЮЧЕВЫЕ СЛОВА Grid-first (экономия баллов 152), v5 — только фолбэк ─────────

def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def step_keywords(ctx: CopyCtx, grid_batch: int = 1000, v5_batch: int = 200) -> dict:
    """ФАЗА 3c п.2. Добавить ключевые фразы копии Grid-FIRST (``grid.add_keywords`` по куки, 0
    v5-баллов), v5 ``keywords.add`` — ТОЛЬКО фолбэк. Раньше базовый движок (direct_copy.phase_upload)
    слал ВСЁ через v5 (главный пожиратель баллов → 152), а Grid добирал лишь остаток. Теперь v5-путь
    в phase_upload выключен (skip_keywords=True), а этот шаг — основной.

    Инварианты переноса (сохранены):
      • group-remap: фраза привязывается к НОВОЙ adgroup-id (maps['adgroups'][src_group]);
      • ставки: v5 ``Bid``→Grid ``price`` (руб), ``ContextBid``→Grid ``priceContext`` (руб);
      • UserParam1/UserParam2: Grid ``AddKeywords`` их НЕ умеет (интроспекция GdAddKeywordsItemInput:
        только adGroupId/keyword/price/priceContext) → такие фразы идут в v5 (сохраняем UserParam;
        это малая доля, единичные баллы);
      • учёт добавленных (``keywords_done.json``) — без дублей, идемпотентно на повторном прогоне.

    Фолбэк-безопасно: нет grid → всё в v5; нет v5-токена → недобавленные честно в rep['failed'].
    Порядок: Grid по батчам (отказ одного батча не сбрасывает весь набор в v5), потом v5 для
    UserParam-фраз и не прошедших Grid."""
    rep = {"total": 0, "via_grid": 0, "via_v5": 0, "v5_userparam": 0, "failed": 0,
           "skipped_no_group": 0, "already_done": 0, "grid_failed_batches": 0, "errors": []}
    keywords = _rj(ctx.src_dir / "keywords.json")
    if not keywords:
        return rep
    done_path = ctx.workdir / "keywords_done.json"
    done_kw: set[str] = set(_rj(done_path)) if done_path.exists() else set()
    adg_map = ctx.maps.get("adgroups") or {}

    grid_rows: list[dict] = []
    grid_keys: list[str] = []
    v5_rows: list[dict] = []          # фразы с UserParam → только v5 (Grid не переносит UserParam)
    v5_keys: list[str] = []
    for k in keywords:
        key = f"{k.get('AdGroupId')}|{k.get('Keyword')}"
        rep["total"] += 1
        if key in done_kw:
            rep["already_done"] += 1
            continue
        gid = adg_map.get(str(k.get("AdGroupId") or ""))
        phrase = str(k.get("Keyword") or "").strip()
        if not gid or not str(gid).isdigit() or not phrase or phrase.startswith("---"):
            rep["skipped_no_group"] += 1
            continue
        gid = int(gid)
        bid, cbid = k.get("Bid"), k.get("ContextBid")
        up1, up2 = k.get("UserParam1"), k.get("UserParam2")
        if up1 or up2:
            item = {"AdGroupId": gid, "Keyword": phrase}
            if bid is not None:
                try:
                    item["Bid"] = int(bid)
                except (TypeError, ValueError):
                    pass
            if cbid is not None:
                try:
                    item["ContextBid"] = int(cbid)
                except (TypeError, ValueError):
                    pass
            if up1 is not None:
                item["UserParam1"] = up1
            if up2 is not None:
                item["UserParam2"] = up2
            v5_rows.append(item)
            v5_keys.append(key)
        else:
            row: dict = {"adgroup_id": gid, "keyword": phrase}
            if bid is not None:
                try:
                    row["price"] = round(float(bid) / 1_000_000, 2)
                    row["_bid"] = int(bid)        # оригинал микро — для точного v5-фолбэка
                except (TypeError, ValueError):
                    pass
            if cbid is not None:
                try:
                    row["price_context"] = round(float(cbid) / 1_000_000, 2)
                    row["_context_bid"] = int(cbid)
                except (TypeError, ValueError):
                    pass
            grid_rows.append(row)
            grid_keys.append(key)

    # 1) GRID-FIRST (0 баллов). Свой батчинг, чтобы отказ одного батча не сбросил всё в v5.
    grid_failed_rows: list[dict] = []
    grid_failed_keys: list[str] = []
    if grid_rows and ctx.grid is not None and hasattr(ctx.grid, "add_keywords"):
        for rows_b, keys_b in zip(_chunks(grid_rows, grid_batch), _chunks(grid_keys, grid_batch)):
            try:
                added = ctx.grid.add_keywords(rows_b)
                for key in keys_b:                # Grid raise-on-error → успех = батч принят
                    done_kw.add(key)
                rep["via_grid"] += len(added or []) or len(rows_b)
            except Exception as e:  # noqa: BLE001
                grid_failed_rows += rows_b
                grid_failed_keys += keys_b
                rep["grid_failed_batches"] += 1
                rep["errors"].append(f"grid kw batch: {str(e)[:180]}")
        _wj(done_path, sorted(done_kw))
    elif grid_rows:
        grid_failed_rows = list(grid_rows)        # нет grid-клиента → всё в v5-фолбэк
        grid_failed_keys = list(grid_keys)

    # 2) V5-ФОЛБЭК: UserParam-фразы + не прошедшие Grid (перепаковка grid-row → v5 keyword item).
    rep["v5_userparam"] = len(v5_rows)
    v5_all_rows = list(v5_rows)
    v5_all_keys = list(v5_keys)
    for row, key in zip(grid_failed_rows, grid_failed_keys):
        item = {"AdGroupId": int(row["adgroup_id"]), "Keyword": row["keyword"]}
        if row.get("_bid") is not None:
            item["Bid"] = int(row["_bid"])
        if row.get("_context_bid") is not None:
            item["ContextBid"] = int(row["_context_bid"])
        v5_all_rows.append(item)
        v5_all_keys.append(key)

    if v5_all_rows:
        token = ctx.target_token
        if not token or not ctx.v5_call:
            rep["failed"] += len(v5_all_rows)
            rep["errors"].append(
                f"нет v5-токена/вызова — {len(v5_all_rows)} ключей не добавлены "
                f"(UserParam {len(v5_rows)}, Grid-fail {len(grid_failed_rows)})")
        else:
            for rows_b, keys_b in zip(_chunks(v5_all_rows, v5_batch), _chunks(v5_all_keys, v5_batch)):
                try:
                    j = ctx.v5_call("keywords", "add", token, ctx.target_login, {"Keywords": rows_b})
                    err = _v5_add_err(j)
                    if err:
                        rep["failed"] += len(rows_b)
                        rep["errors"].append(f"v5 keywords.add: {err[:180]}")
                        continue
                    add_results = ((j.get("result") or {}).get("AddResults") or [])
                    for key, ar in zip(keys_b, add_results):
                        item_id = ar.get("Id") if isinstance(ar, dict) else None
                        if item_id:
                            done_kw.add(key)
                            rep["via_v5"] += 1
                        else:
                            rep["failed"] += 1
                            item_errs = (ar.get("Errors") or []) if isinstance(ar, dict) else []
                            if item_errs:
                                ctx.log(f"v5 keywords.add per-item err: key={key!r} "
                                        f"err={str(item_errs[0])[:120]}")
                    # если AddResults короче батча (неожиданный API) — остаток помечаем как failed
                    if len(add_results) < len(keys_b):
                        tail = keys_b[len(add_results):]
                        rep["failed"] += len(tail)
                        ctx.log(f"v5 keywords.add: AddResults ({len(add_results)}) < batch ({len(keys_b)}) — {len(tail)} ключей не помечены done")
                except Exception as e:  # noqa: BLE001
                    rep["failed"] += len(rows_b)
                    rep["errors"].append(f"v5 keywords.add: {str(e)[:180]}")
            _wj(done_path, sorted(done_kw))

    ctx.log(f"ключи Grid-first (0 баллов): Grid {rep['via_grid']}, v5-фолбэк {rep['via_v5']} "
            f"(UserParam {rep['v5_userparam']}, Grid-fail батчей {rep['grid_failed_batches']}), "
            f"не добавлено {rep['failed']}, уже были {rep['already_done']}, "
            f"без группы {rep['skipped_no_group']}")
    return rep


# ── П.4 (ФАЗА 3b): адаптивные креативы 1:1 по куки (Grid), гео в тексте, БЕЗ v5-баллов ─────

def step_adaptive_creatives(ctx: CopyCtx) -> dict:
    """П.4. Сделать контент target-адаптивов 1:1 с ИСТОЧНИКОМ (заголовки/тексты/картинки),
    БЕЗ переиспользования исходного CreativeId и БЕЗ v5-баллов.

    Механика (всё по куки/Grid):
      1) читаем состав адаптивного объявления с ИСТОЧНИКА — ``source_grid.adaptive_ads_for_update``
         (GdAdaptiveTextAd: titles/bodies/images/button/typedCreatives), по куки источника;
      2) картинки ремапим source→target (``maps['images']`` — уже перезалитые хэши); непереносимые
         (нет в маппинге) выкидываем (raw source-hash межаккаунтно невалиден);
      3) к тексту (заголовки/тексты) применяем морфологическую гео-замену
         ``copy_geo_morph.apply_replacements`` теми же парами, что и остальной контент job
         (креатив 1:1, но город/область — новые, с падежами);
      4) пишем в target-объявления через RMW ``update_adaptive_ads`` (Grid UpdateAdaptiveTextAds):
         RMW сохраняет target-``href`` (целевой домен), уже проставленную ``adPrice`` и видео
         (creativeIds) — перезаписываем ТОЛЬКО titles/bodies/imageHashes.

    НЕ переносим исходный CreativeId и НЕ трогаем кнопку из источника (её ``href`` нёс бы
    source-домен; RMW сохраняет target-кнопку). Идемпотентно, фолбэк-безопасно: нет source/target
    grid, апдейтера или маппинга — пропуск с отчётом, job не падает."""
    rep = {"src_ads_read": 0, "candidates": 0, "updated": 0, "geo_applied": 0,
           "images_remapped": 0, "no_target": 0, "no_content": 0, "errors": []}
    if ctx.grid is None:
        rep["errors"].append("нет target grid — адаптивы пропущены")
        return rep
    if ctx.source_grid is None:
        rep["errors"].append("нет source grid (куки источника) — адаптивы пропущены")
        return rep
    if not ctx.update_adaptive_ads:
        rep["errors"].append("нет update_adaptive_ads (инъекция) — адаптивы пропущены")
        return rep
    ads_map = ctx.maps.get("ads") or {}          # src_ad_id → tgt_ad_id
    camp_map = ctx.maps.get("campaigns") or {}   # src_camp_id → tgt_camp_id
    img_map = ctx.maps.get("images") or {}       # src_hash → tgt_hash
    if not ads_map or not camp_map:
        ctx.log("адаптивы: нет ads/campaigns маппинга — пропуск")
        return rep

    src_camp_ids = [int(x) for x in camp_map.keys() if str(x).isdigit()]
    src_ad_ids = [int(x) for x in ads_map.keys() if str(x).isdigit()]
    tgt_camp_ids = [int(v) for v in camp_map.values() if str(v).isdigit()]
    try:
        src_ads = ctx.source_grid.adaptive_ads_for_update(src_camp_ids, src_ad_ids) or {}
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"read source adaptive: {str(e)[:200]}")
        return rep
    rep["src_ads_read"] = len(src_ads)
    if not src_ads:
        ctx.log("адаптивы: у источника нет адаптивных объявлений (GdAdaptiveTextAd) — пропуск")
        return rep

    from . import copy_geo_morph as cgm
    pairs = ctx.geo_pairs or []
    items: list[dict] = []
    for src_ad_id, comp in src_ads.items():
        tgt_ad_id = ads_map.get(str(src_ad_id))
        if not tgt_ad_id or not str(tgt_ad_id).isdigit():
            rep["no_target"] += 1
            continue
        titles = list(comp.get("titles") or [])
        bodies = list(comp.get("bodies") or [])
        if not titles and not bodies:
            rep["no_content"] += 1
            continue
        rep["candidates"] += 1
        n_geo = 0
        new_titles = []
        for t in titles:
            out, n = cgm.apply_replacements(t, pairs)
            n_geo += n
            new_titles.append(out[:56])                # лимит заголовка ≤56
        new_bodies = []
        for b in bodies:
            out, n = cgm.apply_replacements(b, pairs)
            n_geo += n
            new_bodies.append(out[:81])                # лимит текста ≤81
        if n_geo:
            rep["geo_applied"] += 1
        new_imgs = []
        for h in (comp.get("imageHashes") or []):
            th = img_map.get(h)
            if th:
                new_imgs.append(th)
                rep["images_remapped"] += 1
        item = {"id": int(tgt_ad_id), "titles": new_titles, "bodies": new_bodies}
        if new_imgs:
            item["image_hashes"] = new_imgs            # RMW: пустой → сохранит target-картинки
        items.append(item)

    if not items:
        ctx.log("адаптивы: нет объявлений с маппингом/контентом — пропуск")
        return rep
    try:
        # RMW по tgt_camp_ids: сохраняет target href/adPrice/creativeIds(видео), меняет только текст+картинки.
        rep["updated"] = int(ctx.update_adaptive_ads(ctx.target_login, items, tgt_camp_ids) or 0)
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"grid update adaptive: {str(e)[:200]}")
    ctx.log(f"адаптивы 1:1 (Grid, 0 баллов): контент обновлён у {rep['updated']}/{len(items)} "
            f"объявлений (гео у {rep['geo_applied']}, картинок ремаплено {rep['images_remapped']}, "
            f"без target {rep['no_target']}, без контента {rep['no_content']})")
    return rep


# ── П.12 (ФАЗА 3b): видео 1:1 по куки (детект + аплоуд-хук), БЕЗ v5-баллов ─────────────────

def step_videos(ctx: CopyCtx) -> dict:
    """П.12. Перенести видео-креативы источника 1:1 на target-объявления по куки (БЕЗ v5-баллов).

    Полный контур (когда доступен ``video_file_resolver``):
      детект  — ``source_grid.adaptive_ads_for_update`` → typedCreatives VIDEO_ADDITION / hasVideo;
      скачать — ``video_file_resolver(meta) -> путь_к_mp4`` (файл источника);
      залить  — ``video_upload_client.upload_video_creative(mp4)`` (куки /content → meta.creative_id);
      привязать — ``update_adaptive_ads(login,[{id, creative_ids:[new]}], camp_ids)`` (RMW: сохраняет
                  контент/цену/картинки, добавляет видео). Содержимое видео НЕ трогаем (гео внутри
                  ролика не меняем).

    ⚠ ЧЕСТНОЕ ОГРАНИЧЕНИЕ (диагностировано): Grid/куки НЕ отдают скачиваемый mp4-URL для
    VIDEO_ADDITION-креатива источника — ``adaptive_ads_for_update`` даёт лишь непереносимый
    account-scoped ``creativeId``; официальный v5 видео вообще не умеет (ни read, ни upload).
    Поэтому фактическое СКАЧИВАНИЕ исходника вынесено в инъектируемый ``video_file_resolver``.
    Без него (по умолчанию None) шаг ДЕТЕКТИРУЕТ видео и ЧЕСТНО ОТЧИТЫВАЕТСЯ, но не переносит —
    не роняя job. Аплоуд/привязка (target-сторона) уже реализованы и сработают, как только
    resolver появится. Если у источника видео нет — шаг тихо пропускается с отчётом."""
    rep = {"src_ads_with_video": 0, "uploaded": 0, "attached": 0, "no_source_file": 0,
           "no_target": 0, "note": "", "errors": []}
    if ctx.source_grid is None:
        rep["errors"].append("нет source grid — видео пропущено")
        return rep
    ads_map = ctx.maps.get("ads") or {}
    camp_map = ctx.maps.get("campaigns") or {}
    src_camp_ids = [int(x) for x in camp_map.keys() if str(x).isdigit()]
    src_ad_ids = [int(x) for x in ads_map.keys() if str(x).isdigit()]
    tgt_camp_ids = [int(v) for v in camp_map.values() if str(v).isdigit()]
    if not src_ad_ids:
        return rep
    try:
        src_ads = ctx.source_grid.adaptive_ads_for_update(src_camp_ids, src_ad_ids) or {}
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"read source video: {str(e)[:200]}")
        return rep
    video_ads = {aid: c for aid, c in src_ads.items()
                 if (c.get("creativeIds") or c.get("hasVideo"))}
    rep["src_ads_with_video"] = len(video_ads)
    if not video_ads:
        ctx.log("видео: у источника нет видео-креативов — шаг пропущен")
        return rep

    if not (ctx.video_upload_client and ctx.update_adaptive_ads and ctx.video_file_resolver):
        rep["note"] = ("видео найдено у источника, но перенос 1:1 не выполнен: Grid/куки не отдают "
                       "скачиваемый mp4 VIDEO_ADDITION-креатива (нужен video_file_resolver); "
                       "аплоуд/привязка target-стороны готовы и сработают при его наличии")
        ctx.log(f"видео: обнаружено {len(video_ads)} объявлений с видео у источника — перенос требует "
                f"скачивания исходника (Grid не отдаёт mp4-URL); не перенесено (report-only)")
        return rep

    # Полный путь (resolver внедрён): скачать → залить по куки → привязать RMW.
    upload_cache: dict[str, str] = {}          # src_creative_id → new target creative_id (дедуп аплоуда)
    for src_ad_id, comp in video_ads.items():
        tgt_ad_id = ads_map.get(str(src_ad_id))
        if not tgt_ad_id or not str(tgt_ad_id).isdigit():
            rep["no_target"] += 1
            continue
        new_cids: list[str] = []
        for src_cid in (comp.get("creativeIds") or []):
            src_cid = str(src_cid)
            if src_cid in upload_cache:
                new_cids.append(upload_cache[src_cid])
                continue
            try:
                path = ctx.video_file_resolver(
                    {"src_ad_id": src_ad_id, "creative_id": src_cid, "comp": comp})
            except Exception as e:  # noqa: BLE001
                rep["errors"].append(f"resolve {src_cid}: {str(e)[:150]}")
                path = None
            if not path:
                rep["no_source_file"] += 1
                continue
            try:
                new_cid = str(ctx.video_upload_client.upload_video_creative(path))
                upload_cache[src_cid] = new_cid
                new_cids.append(new_cid)
                rep["uploaded"] += 1
            except Exception as e:  # noqa: BLE001
                rep["errors"].append(f"upload {src_cid}: {str(e)[:150]}")
        if not new_cids:
            continue
        try:
            n = ctx.update_adaptive_ads(
                ctx.target_login, [{"id": int(tgt_ad_id), "creative_ids": new_cids}], tgt_camp_ids)
            rep["attached"] += int(n or 0)
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"attach {tgt_ad_id}: {str(e)[:150]}")
    ctx.log(f"видео 1:1 (куки, 0 баллов): залито {rep['uploaded']}, привязано к {rep['attached']} "
            f"объявлениям (без файла {rep['no_source_file']}, без target {rep['no_target']})")
    return rep

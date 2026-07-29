"""Cookie/Grid postprocess for Direct copy jobs."""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path

from .. import campaign as cmc
from .. import grid_finalize as gf
from .. import repair_auto as rauto
from .. import repair_executor as rex
from .. import repair_gate as rgate
from .copy_step_utils import _invalidate_target_edit_rows, _source_edit_rows, _subset_rows


def _engine():
    from . import copy_engine as ce  # lazy to avoid import-time cycle
    return ce


def configure(_deps: dict) -> None:
    return None


def _copy_timed(job_id: str, label: str, fn, *, timeout_sec: int | None = None):
    """Обёртка-таймер фазы постпроцесса: логирует `[timing] <label>: Ns`.

    Замер #23 (профиль скорости копирования): постпроцесс+verify — ~76% времени копии,
    но лог фаз без таймстампов не показывал ВНУТРЕННИЙ хог. Тайминг лёгкий (monotonic),
    остаётся навсегда — видно, какую фазу распараллеливать/батчить."""
    _t = time.monotonic()
    ce = _engine()
    try:
        ce._copy_job_log(job_id, f"[timing] {label}: start")
        if not timeout_sec:
            return fn()
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            fut = executor.submit(fn)
            return fut.result(timeout=timeout_sec)
        except FuturesTimeout as exc:
            raise TimeoutError(f"{label} timeout>{timeout_sec}s") from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
    finally:
        ce._copy_job_log(job_id, f"[timing] {label}: {time.monotonic() - _t:.0f}s")

def _copy_execute_image_repairs(login: str, ctx: dict, plan: dict, deps,
                                post_verify=None) -> dict:
    """Run Grid-only image repairs for copy live verification failures."""
    ids, actions, unsupported = rgate.executable_images_repairs(plan or {})
    out = {
        "action": "images_repair",
        "ok": True,
        "executed": 0,
        "failed": 0,
        "campaign_ids": ids,
        "executed_actions": [],
        "unsupported_actions": unsupported[:40],
    }
    if not ids or not actions:
        out["note"] = "нет executable images_repair"
        return out
    repair_out, status = rex.execute_images_repair(login, ctx, ids, deps)
    out.update({"status": status, "result": repair_out})
    if 200 <= int(status) < 300 and repair_out.get("ok"):
        out["executed"] = len(ids)
        out["executed_actions"] = [{k: v for k, v in a.items()} for a in actions]
    else:
        out["ok"] = False
        out["failed"] = len(ids)
    if post_verify:
        post_verify(out, login, ctx)
    return out


def _copy_demote_optional_source_grid_errors(rep: dict, source_login: str) -> None:
    """Move source Grid read failures from hard errors to report-only warnings.

    These reads enrich copy fidelity (promos/settings/disabledPlaces/adaptive/video source
    introspection). If the source agency cookie cannot read Grid but the v5 copy and target
    live verification are healthy, the job should not be marked as a failed copy.
    """
    source_login = (source_login or "").strip()
    errors = list(rep.get("errors") or [])
    if not errors or not source_login:
        return
    warning_prefixes = (
        "promos grid:",
        "source grid read:",
        "чтение настроек по кукам:",
        "source disabledPlaces read:",
        "read source adaptive:",
        "read source video:",
    )
    kept: list[str] = []
    warnings = list(rep.get("warnings") or [])
    source_marker = f"ulogin={source_login}"
    for err in errors:
        text = str(err)
        if text.startswith(warning_prefixes) and (
            source_marker in text
            or "source" in text.lower()
            or "источник" in text.lower()
        ):
            warnings.append(text)
        else:
            kept.append(text)
    rep["errors"] = kept
    if warnings:
        rep["warnings"] = warnings

def _prefetch_copy_verify_grid_cache(target_login: str, target_agency: str, grid, maps: dict, ctx, log) -> dict:
    """Read target verify snapshots concurrently with thread-local Grid clients."""
    del target_agency  # агентство здесь не нужно: читаем по уже рабочей target-cookie
    camp_ids = []
    for raw in (maps.get("campaigns") or {}).values():
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            continue
        if cid > 0 and cid not in camp_ids:
            camp_ids.append(cid)
    ad_ids = []
    for raw in (maps.get("ads") or {}).values():
        try:
            aid = int(raw)
        except (TypeError, ValueError):
            continue
        if aid > 0 and aid not in ad_ids:
            ad_ids.append(aid)
    if not camp_ids:
        return {}
    base_cookie = getattr(grid, "cookie", "") or ""

    def _target_grid():
        return gf.GridClient(target_login, cookie=base_cookie)

    def _counts():
        from .. import grid_read as gr  # lazy sibling import
        return gr.GridReadClient(target_login, cookie=base_cookie).campaign_content_counts(camp_ids)

    def _edit_rows():
        cached = _subset_rows(getattr(ctx, "cached_target_edit_rows", None), camp_ids, require_all=True)
        if cached is not None:
            return cached
        rows = _target_grid().campaigns_edit_rows(camp_ids)
        base = dict(getattr(ctx, "cached_target_edit_rows", None) or {})
        base.update(rows or {})
        ctx.cached_target_edit_rows = base
        return _subset_rows(base, camp_ids) or {}

    def _invariants():
        return _target_grid().read_campaign_invariants(camp_ids)

    def _adaptive_tgt():
        if not ad_ids:
            return None
        return _target_grid().adaptive_ads_for_update(camp_ids, ad_ids)

    tasks = {"counts": _counts, "edit_rows": _edit_rows, "invariants": _invariants}
    if ad_ids:
        tasks["adaptive_tgt"] = _adaptive_tgt
    out = {}
    with ThreadPoolExecutor(max_workers=min(4, len(tasks))) as pool:
        futs = {pool.submit(fn): name for name, fn in tasks.items()}
        for fut, name in list(futs.items()):
            try:
                value = fut.result()
            except Exception as exc:  # noqa: BLE001
                log(f"copy_verify cache {name}: {str(exc)[:180]}")
                continue
            if value is not None:
                out[name] = value
    ctx.cached_adaptive_tgt = out.get("adaptive_tgt")
    if out:
        log("copy_verify cache: " + ", ".join(sorted(out)))
    return out


def _v5_conditions_to_grid(raw) -> list[dict]:
    """v5 FeedFilterConditions → Grid feedFilter.conditions.

    v5: {"Items":[{"Operand":"url","Operator":"CONTAINS_ANY","Arguments":["GAC"]}]} (или сразу список).
    Grid: [{"field":"url","operator":"CONTAINS_ANY","stringValue":"[\\"GAC\\"]"}] — stringValue
    это JSON-массив СТРОКОЙ (форма подтверждена чтением живого источника, 2026-07-27)."""
    if isinstance(raw, dict):
        raw = raw.get("Items") or []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for cond in raw:
        if not isinstance(cond, dict):
            continue
        field = str(cond.get("Operand") or "").strip()
        operator = str(cond.get("Operator") or "").strip()
        args = [str(x) for x in (cond.get("Arguments") or []) if str(x).strip()]
        if not field or not operator or not args:
            continue
        out.append({"field": field, "operator": operator,
                    "stringValue": json.dumps(args, ensure_ascii=False)})
    return out


def _copy_apply_product_filters(src_dir: Path, maps: dict, grid, log=None) -> dict:
    """Перенести feedFilter источника на созданные в цели ShoppingAd/ListingAd (Grid).

    Источник фильтра для ОБОИХ типов — ShoppingAd той же исходной группы: v5 не отдаёт
    детальных полей ListingAd, а в слепках фид и фильтр у пары идут вместе (та же логика,
    что в direct_copy.phase_upload при создании ListingAd).
    → {"shopping": N, "listing": M, "errors": [...]}."""
    ce = _engine()
    rep = {"shopping": 0, "listing": 0, "errors": []}
    shopping_ads = ce._copy_read_json(src_dir / "shopping_ads.json") or []
    if not shopping_ads:
        return rep

    # src AdGroupId → (conditions, bodies, src_feed_id) по ShoppingAd источника.
    by_group: dict[str, dict] = {}
    shop_items: list[dict] = []
    for sa in shopping_ads:
        sad = sa.get("ShoppingAd") or {}
        conds = _v5_conditions_to_grid(sad.get("FeedFilterConditions"))
        if not conds:
            continue
        new_fid = maps.get("feeds", {}).get(str(sad.get("FeedId") or ""))
        if not new_fid:
            continue
        bodies = [str(t) for t in (sad.get("DefaultTexts") or []) if str(t or "").strip()]
        entry = {"conditions": conds, "bodies": bodies, "feed_id": int(new_fid)}
        by_group[str(sa.get("AdGroupId") or "")] = entry
        new_ad_id = maps.get("ads", {}).get(str(sa.get("Id") or ""))
        if new_ad_id:
            shop_items.append({"id": int(new_ad_id), **entry})

    listing_items: list[dict] = []
    for ad in (ce._copy_read_json(src_dir / "ads.json") or []):
        if str(ad.get("Type") or "") != "LISTING_AD":
            continue
        entry = by_group.get(str(ad.get("AdGroupId") or ""))
        new_ad_id = maps.get("ads", {}).get(str(ad.get("Id") or ""))
        if entry and new_ad_id:
            listing_items.append({"id": int(new_ad_id), **entry})

    for items, listing, key in ((shop_items, False, "shopping"), (listing_items, True, "listing")):
        if not items:
            continue
        try:
            rep[key] = grid.set_product_feed_filters(items, listing=listing)
        except Exception as e:  # noqa: BLE001 — UNKNOWN_FIELD (схема целевого фида) не должна валить job
            rep["errors"].append(f"feed-filters {key}: {str(e)[:220]}")
    if log:
        log(f"фильтры товарных/каталожных: {rep['shopping']}/{len(shop_items)} товарных, "
            f"{rep['listing']}/{len(listing_items)} каталожных")
    return rep


def _copy_cookie_postprocess(job_id: str, target_login: str, target_agency: str,
                             src_dir: Path, workdir: Path, body: dict) -> dict:
    """Cookie/Grid fallback after direct_copy upload: callouts, ShoppingAd, ListingAd, verification, repair."""
    ce = _engine()
    rep = {
        "callouts_created_or_found": 0,
        "callouts_attached_campaigns": 0,
        "keywords_added": 0,
        "promos_created": 0,
        "promos_attached_campaigns": 0,
        "shopping_added": 0,
        "listing_added": 0,
        "skipped": [],
        "errors": [],
        "uses_direct_units": False,
    }
    maps_path = workdir / "id_maps.json"
    maps = ce._copy_read_json(maps_path) if maps_path.exists() else {}
    maps.setdefault("campaigns", {})
    maps.setdefault("adgroups", {})
    maps.setdefault("ads", {})
    maps.setdefault("feeds", {})
    maps.setdefault("callouts", {})
    maps.setdefault("promotions", {})

    try:
        client = cmc.build_client(target_login, account=(target_agency or None))
        cookie = client.sess.headers.get("Cookie") or ""
        grid = gf.GridClient(target_login, cookie=cookie)
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"cookie init: {str(e)[:220]}")
        return rep

    # ФАЗА 1 под-сервисы (copy_steps): единый контекст для шагов П.10/П.11/П.13/П.14.
    from . import copy_steps as csteps
    try:
        _tgt_token, _ = ce._token_for_login(
            target_login, target_agency or ce._resolve_agency_hint(target_login, ""), ce._direct_tokens())
    except Exception:  # noqa: BLE001
        _tgt_token = ""
    cstep_ctx = csteps.CopyCtx(
        target_login=target_login, target_agency=target_agency or "",
        src_dir=src_dir, workdir=workdir, body=body, maps=maps,
        grid=grid, target_token=_tgt_token or "",
        log=(lambda m: ce._copy_job_log(job_id, m)),
        v5_call=ce._v5_call,
        # П.8: прайс-хелперы (create_set_feeds через blueprint-обёртки — configure() внутри).
        feed_offer_prices=ce._grid_feed_offer_prices, account_offer_prices=ce._account_offer_prices,
        group_ad_price=ce._group_ad_price, set_ad_prices=ce._grid_set_ad_prices,
    )

    # ФАЗА 3b (п.4 адаптивы / п.12 видео): source-Grid (куки источника, чтение состава), гео-пары
    # job'а, RMW-апдейтер адаптивов (сохраняет target href/adPrice/видео), видео-аплоуд по куки.
    cstep_ctx.update_adaptive_ads = ce._grid_update_adaptive_ads
    cstep_ctx.video_upload_client = client          # UacClient target: upload_video_creative по куки
    cstep_ctx.video_file_resolver = None            # заполним ниже, если source_grid поднялся (см. п.12)
    try:
        _src_login = (body.get("source_login") or "").strip()
        cstep_ctx.source_login = _src_login
        if _src_login:
            _src_ag = ce._resolve_agency_hint(_src_login, "")
            _src_cli2 = cmc.build_client(_src_login, account=(_src_ag or None))
            cstep_ctx.source_grid = gf.GridClient(_src_login, cookie=(_src_cli2.sess.headers.get("Cookie") or ""))
            _src_ctx = ce._copy_ctx(_src_login)
            _geo_mode_pp = (body.get("geo_mode") or "replace").strip()
            _mode_pp = (body.get("mode") or "auto").strip()
            if _geo_mode_pp == "keep":
                # geo_mode="keep": никакой гео-замены — источник остаётся как есть.
                # target_city/region не переданы — ce._copy_geo_replacements с пустыми строками
                # строил бы пары вида ("Краснодар", "") → стирал города из быстрых ссылок.
                cstep_ctx.geo_pairs = []
            elif _mode_pp == "other" and _geo_mode_pp == "change":
                # mode="other" + geo_mode="change": имя резолвим по ID из справочника.
                # target_city/region пусты — нельзя их использовать. Морфология только при
                # одном плюс-регионе без исключений, как в основной v5-ветке _copy_run_job.
                _pp_geo_raw = body.get("geo_region_ids") or []
                if not _pp_geo_raw:
                    _pp_scalar = int(body.get("geo_region_id") or 0)
                    _pp_geo_raw = [_pp_scalar] if _pp_scalar else []
                _pp_geo_ids = [int(x) for x in _pp_geo_raw
                               if str(x).lstrip("-").isdigit() and int(x) != 0]
                _pp_pos = [x for x in _pp_geo_ids if x > 0]
                _pp_neg = [x for x in _pp_geo_ids if x < 0]
                _pp_geo_rid = _pp_pos[0] if len(_pp_pos) == 1 and not _pp_neg else 0
                _pp_rname = (ce._geo_name_by_id(_pp_geo_rid) if ce._geo_name_by_id and _pp_geo_rid else "") or ""
                cstep_ctx.geo_pairs = ce._copy_geo_replacements(
                    _src_ctx, "", _pp_rname,
                    log=(lambda m: ce._copy_job_log(job_id, m))) if _pp_rname else []
            else:
                # mode="auto": target_city/region пришли из body (валидированы роутом).
                cstep_ctx.geo_pairs = ce._copy_geo_replacements(
                    _src_ctx, body.get("target_city") or "", body.get("target_region") or "",
                    log=(lambda m: ce._copy_job_log(job_id, m)))
            # ФАЗА 3c п.12: скачиваемый URL исходного видео найден Grid-интроспекцией —
            # GdVideoAdditionCreative.originalUrl = прямой mp4 (storage.mds.yandex.net, HTTP 200
            # video/mp4 без авторизации, проверено live 2026-07-03). Строим resolver: creative_id
            # источника → скачать mp4 → отдать в step_videos (аплоуд по куки + RMW-привязка).
            cstep_ctx.video_file_resolver = ce._copy_make_video_resolver(
                job_id, cstep_ctx.source_grid, maps, workdir)
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"adaptive/video ctx init: {str(e)[:180]}")

    # 1) Уточнения: если v5 adextensions.add не создал часть ids, добираем через Grid и
    # прикрепляем на campaign-level, чтобы объявления получили наследуемые callouts.
    callouts = ce._copy_read_json(src_dir / "callouts.json")
    callout_texts = []
    for c in callouts:
        txt = str(((c.get("Callout") or {}).get("CalloutText")) or "").strip()
        if txt:
            callout_texts.append(txt)
    if callout_texts:
        try:
            callout_map = grid.add_callouts(callout_texts)
            for c in callouts:
                src_id = str(c.get("Id") or "")
                txt = str(((c.get("Callout") or {}).get("CalloutText")) or "").strip()
                if src_id and txt and callout_map.get(txt):
                    maps["callouts"][src_id] = int(callout_map[txt])
            rep["callouts_created_or_found"] = len(callout_map)
            # П.11: вешаем на КАЖДУЮ кампанию только её ремапленные callout-id (по исходной связи),
            # а не общий union. Фолбэк на union — внутри шага, если source-связь недоступна.
            co_report = csteps.step_attach_callouts(cstep_ctx, per_campaign_cap=ce._CALLOUT_PER_CAMPAIGN_CAP)
            _invalidate_target_edit_rows(cstep_ctx)
            rep["callouts_attached_campaigns"] = co_report.get("attached_campaigns") or 0
            rep["callouts_per_campaign"] = co_report
            rep["errors"] += co_report.get("errors") or []
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"callouts grid: {str(e)[:220]}")

    # 1a-sitelinks) Быстрые ссылки по ИСХОДНОЙ связи campaign→sitelinkSet (Grid, 0 баллов).
    # Синхронно ДО live_verification — иначе verify видит target sitelinks_present=0.
    # Контент набора читаем source_grid.get_sitelink_sets (не sitelinks.json).
    # Идемпотентно: maps["sitelinks"] кэш (src_set_id→tgt_set_id) исключает дубли при retry.
    # «Голые» кампании (source без sitelinks в campaign_sitelinks.json) не трогаются.
    try:
        _sl_sync_rep = csteps.step_attach_sitelinks(cstep_ctx)
        _invalidate_target_edit_rows(cstep_ctx)
        rep["sitelinks_attached_campaigns"] = _sl_sync_rep.get("attached_campaigns") or 0
        rep["sitelinks_per_campaign"] = _sl_sync_rep
        rep["errors"] += _sl_sync_rep.get("errors") or []
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"sitelinks grid: {str(e)[:220]}")

    # 1a) Промоакции: official promotions.add заблокирован (8000/ЕРИР) → переносим через Grid
    # addPromoExtensions. КЛЮЧЕВОЕ (root-cause привязки): создаём промо ИЗ source
    # campaigns_edit_rows (promoExtension), тогда maps["promotions"] ключуется по promoExtension.id,
    # совпадающему с campaign_promos.json → step_attach_promos привязывает per-campaign даже при
    # 2+ промо. Раньше создавали из promotions.json (ключ = promotions.json.Id) ≠ promoExtension.id
    # из campaign_promos.json → by_promo пуст → fallback_single → при 10 промо не привязывало.
    # Фолбэк (нет source_grid / нет промо в edit_rows): прежний путь из promotions.json.
    promotions = ce._copy_read_json(src_dir / "promotions.json")
    created_promo_ids = []
    if promotions or cstep_ctx.source_grid is not None:
        try:
            from ..promo import PromoClient
            pc = PromoClient(client, target_login)
            _src_dom = str(body.get("_copy_source_domain") or "")
            _tgt_dom = str(body.get("target_domain") or "")
            _used_edit_rows = False
            if cstep_ctx.source_grid is not None:
                _src_cids = [int(x) for x in (maps.get("campaigns") or {}).keys() if str(x).isdigit()]
                _src_rows = _source_edit_rows(cstep_ctx, _src_cids) if _src_cids else {}
                _promo_defs: dict[str, dict] = {}
                _cp_links: dict[str, str] = {}
                for _cid, _row in (_src_rows or {}).items():
                    _pe = _row.get("promoExtension") or {}
                    _pid = str(_pe.get("id") or "")
                    if not _pid or _pid == "0":
                        continue
                    _cp_links[str(_cid)] = _pid
                    if _pe.get("description"):
                        _promo_defs[_pid] = _pe
                if _promo_defs:
                    _used_edit_rows = True
                    for _src_pid, _pdef in _promo_defs.items():
                        if str(_src_pid) in maps["promotions"]:
                            created_promo_ids.append(int(maps["promotions"][str(_src_pid)]))
                            continue
                        _npid, _perr = pc.add(
                            type=_pdef.get("type") or "DISCOUNT",
                            description=_pdef.get("description") or "акция",
                            href=ce._copy_target_href(_pdef.get("href"), _src_dom, _tgt_dom),
                            amount=_pdef.get("amount"), unit=_pdef.get("unit"),
                            prefix=_pdef.get("prefix"), promocode=_pdef.get("promocode"),
                            start=_pdef.get("startDate"), finish=_pdef.get("finishDate"))
                        if _npid:
                            maps["promotions"][str(_src_pid)] = int(_npid)
                            created_promo_ids.append(int(_npid))
                        elif _perr:
                            rep["errors"].append(f"promo {_src_pid}: {_perr[:180]}")
                    if _cp_links:
                        (src_dir / "campaign_promos.json").write_text(
                            json.dumps(_cp_links, ensure_ascii=False, indent=1), encoding="utf-8")
                        ce._copy_job_log(job_id, f"промо: создано из source edit_rows "
                                              f"({len(_promo_defs)} промо, {len(_cp_links)} связей)")
            if not _used_edit_rows:
                for p in (promotions or []):
                    src_id = str(p.get("Id") or "")
                    if src_id and src_id in maps["promotions"]:
                        created_promo_ids.append(int(maps["promotions"][src_id]))
                        continue
                    pid, perr = pc.add(
                        type=p.get("Type") or "DISCOUNT",
                        description=p.get("Description") or p.get("Name") or "акция",
                        href=ce._copy_target_href(p.get("Href"), _src_dom, _tgt_dom),
                        amount=p.get("Amount"), unit=p.get("AmountUnit"),
                        prefix=p.get("AmountPrefix"), promocode=p.get("Promocode"),
                        start=p.get("StartDate"), finish=p.get("EndDate"))
                    if pid:
                        maps["promotions"][src_id] = int(pid)
                        created_promo_ids.append(int(pid))
                    elif perr:
                        rep["errors"].append(f"promo {src_id}: {perr[:180]}")
            rep["promos_created"] = len(created_promo_ids)
            cstep_ctx.promo_client = pc
            promo_report = csteps.step_attach_promos(cstep_ctx, created_promo_ids)
            _invalidate_target_edit_rows(cstep_ctx)
            rep["promos_attached_campaigns"] = promo_report.get("attached_campaigns") or 0
            rep["promos_per_campaign"] = promo_report
            rep["errors"] += promo_report.get("errors") or []
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"promos grid: {str(e)[:220]}")

    # 1b) КЛЮЧЕВЫЕ СЛОВА Grid-FIRST (ФАЗА 3c п.2). phase_upload теперь skip_keywords=True → v5 ключи
    # не жёг (152). step_keywords добавляет ВСЕ фразы через Grid (0 баллов), v5 — только фолбэк
    # (UserParam-фразы + не прошедшие Grid). group-remap/ставки/UserParam/done-учёт — внутри шага.
    try:
        kw_rep = _copy_timed(job_id, "keywords", lambda: csteps.step_keywords(cstep_ctx), timeout_sec=600)
        rep["keywords"] = kw_rep
        rep["keywords_added"] = int(kw_rep.get("via_grid") or 0) + int(kw_rep.get("via_v5") or 0)
        if int(kw_rep.get("via_v5") or 0) > 0:
            rep["uses_direct_units"] = True     # v5-фолбэк (UserParam/Grid-fail) тратит баллы
        rep["errors"] += kw_rep.get("errors") or []
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"keywords grid-first: {str(e)[:220]}")

    # 2) ShoppingAd fallback по куки. direct_copy пытается v501 ads.add; если баллов не хватило
    # или v501 отклонил, source ShoppingAd останется без maps['ads'][src_id].
    shopping_ads = ce._copy_read_json(src_dir / "shopping_ads.json")
    shop_items = []
    shop_sources = []
    for sa in shopping_ads:
        src_ad_id = str(sa.get("Id") or "")
        if src_ad_id and src_ad_id in maps["ads"]:
            continue
        gid = maps["adgroups"].get(str(sa.get("AdGroupId") or ""))
        sad = sa.get("ShoppingAd") or {}
        fid = maps["feeds"].get(str(sad.get("FeedId") or ""))
        if not gid or not fid:
            rep["skipped"].append({"shopping_ad": src_ad_id, "reason": "no mapped adgroup/feed"})
            continue
        item = {"adgroup_id": int(gid), "feed_id": int(fid)}
        # Preserve simple official API filters where possible.
        raw_conds = sad.get("FeedFilterConditions") or []
        if isinstance(raw_conds, dict):
            raw_conds = raw_conds.get("Items") or []
        for cond in raw_conds:
            if not isinstance(cond, dict):
                continue
            op = str(cond.get("Operand") or "")
            args = cond.get("Arguments") or []
            if op == "collectionId" and args:
                item["collection_id"] = str(args[0])
            elif op == "vendor" and args:
                item["vendor"] = str(args[0])
            elif op == "model" and args:
                item["model"] = [str(x) for x in args]
        try:
            from .. import create_set_feeds as _csf_ff
            item["brand_field"] = _csf_ff._resolve_feed_field(target_login, int(fid), "brand") or "vendor"
            item["model_field"] = _csf_ff._resolve_feed_field(target_login, int(fid), "model") or "model"
        except Exception:  # noqa: BLE001
            item["brand_field"] = "vendor"
            item["model_field"] = "model"
        shop_items.append(item)
        shop_sources.append(src_ad_id)
    if shop_items:
        try:
            new_ids = grid.add_shopping_ads(shop_items)
            mapped_new_ids = []
            for src_id, new_id in zip(shop_sources, new_ids):
                if new_id:
                    maps["ads"][str(src_id)] = int(new_id)
                    mapped_new_ids.append(new_id)
            rep["shopping_added"] = len(mapped_new_ids)
            if mapped_new_ids:
                listing = grid.add_listing_ads_by_shopping_ads(mapped_new_ids)
                rep["listing_added"] = len(listing or [])
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"shopping/listing grid: {str(e)[:220]}")

    ce._copy_write_json(maps_path, maps)

    # 2b) ФИЛЬТРЫ товарных/каталожных объявлений (feedFilter) — Grid-мутацией.
    # v501 ads.add ПРИНИМАЕТ FeedFilterConditions в теле, но на ЕПК ShoppingAd/ListingAd их
    # молча не применяет: объявления создаются, на цели FeedFilterConditions=null и feedFilter=null
    # (живой баг porg-mjyh6hjv→porg-ln7tz7xh 2026-07-27: 204 товарных/каталожных, фильтров 0 —
    # верификатор дал shopping_filter_signature=mismatch, но авторемонт не поднялся).
    # Единственный подтверждённый писатель — updateShoppingAds/updateListingAds (grid_finalize
    # set_product_feed_filters). Шаг идёт ПОСЛЕ fallback-блока выше, поэтому покрывает и
    # объявления, созданные v5, и созданные по куке.
    try:
        filters_rep = _copy_apply_product_filters(src_dir, maps, grid,
                                                  log=(lambda m: ce._copy_job_log(job_id, m)))
        rep["shopping_filters_set"] = filters_rep.get("shopping", 0)
        rep["listing_filters_set"] = filters_rep.get("listing", 0)
        rep["errors"] += filters_rep.get("errors") or []
    except Exception as e:  # noqa: BLE001 — объявления уже созданы; фильтры чиним отдельно
        rep["errors"].append(f"product filters grid: {str(e)[:220]}")

    # П.15: восстановление стратегии PAY_FOR_CONVERSION_MULTIPLE_GOALS через Grid.
    # Кампании с этой стратегией создавались с WB_MAXIMUM_CLICKS (v5 не принимает PFCMG).
    # Здесь восстанавливаем реальную стратегию: payForConversion=True + goalId + sum (рубли).
    try:
        src_campaigns_all = ce._copy_read_json(src_dir / "campaigns.json")
        _pfcmg_restored = 0
        _pfcmg_errors = []
        goal_id_body = int(body.get("goal_id") or 0)
        for _sc in src_campaigns_all:
            _src_id = str(_sc.get("Id") or "")
            _tgt_id = maps.get("campaigns", {}).get(_src_id)
            if not _tgt_id:
                continue
            # Ищем PAY_FOR_CONVERSION_MULTIPLE_GOALS в любом из struct_key.BiddingStrategy.Search/Network
            _weekly_rub = None
            for _struct_key in ("TextCampaign", "DynamicTextCampaign", "UnifiedAdCampaign"):
                _td = _sc.get(_struct_key) or {}
                _bs = _td.get("BiddingStrategy") or {}
                for _side in ("Search", "Network"):
                    _sb = _bs.get(_side) or {}
                    if _sb.get("BiddingStrategyType") == "PAY_FOR_CONVERSION_MULTIPLE_GOALS":
                        _blk = _sb.get("PayForConversionMultipleGoals") or {}
                        _weekly_micro = int(_blk.get("WeeklySpendLimit") or 300_000_000_000)
                        _weekly_rub = _weekly_micro / 1_000_000  # микро → рубли
                        break
                if _weekly_rub is not None:
                    break
            if _weekly_rub is None:
                continue
            _g_id = goal_id_body or 0
            ce._copy_job_log(job_id, f"restore PFCMG стратегии: кампания {_src_id}→{_tgt_id} "
                                  f"goal={_g_id} weekly_rub={int(_weekly_rub)}")
            try:
                _updated = grid.restore_pay_for_conversion_strategy(
                    int(_tgt_id), _g_id, _weekly_rub)
                if _updated:
                    _pfcmg_restored += 1
                    ce._copy_job_log(job_id, f"PFCMG стратегия восстановлена: {_tgt_id}")
                else:
                    _pfcmg_errors.append(f"кампания {_tgt_id}: Grid вернул пустой updatedCampaigns")
            except Exception as _e:  # noqa: BLE001
                _pfcmg_errors.append(f"кампания {_tgt_id}: {str(_e)[:220]}")
        rep["strategy_restore"] = {"restored": _pfcmg_restored, "errors": _pfcmg_errors}
        rep["errors"] += _pfcmg_errors
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"strategy restore: {str(e)[:200]}")

    # Перенос Grid-only настроек (isOrganicSearchEnabled / placementTypes) 1:1 из источника.
    # v5-путь создания не трогает эти поля → они остаются на дефолтах Директа. Добавлено 2026-07-17.
    try:
        rep["organic_placement"] = _copy_timed(
            job_id, "organic_placement", lambda: csteps.step_fix_organic_placement(cstep_ctx), timeout_sec=180)
        rep["errors"] += rep["organic_placement"].get("errors") or []
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"organic placement: {str(e)[:200]}")

    # ФИНАЛ: сверка настроек источник ↔ копия ПО КУКАМ (report-only, 0 v5-баллов).
    # Идёт ПОСЛЕДНЕЙ — после всех добивок (цены/видео/адаптивы/стратегия), иначе сравнивали бы
    # промежуточное состояние. Ловит молчаливые потери, которых v5 не показывает (минус-слова
    # кампаний, brandSafety, временной таргетинг, contextLimit и пр.). Расхождения — в отчёт джоба;
    # автопочинка тут НЕ делается: чинит adjacent-шаг repair, а этот честно показывает факт.
    try:
        rep["settings_diff"] = _copy_timed(
            job_id, "settings_diff", lambda: csteps.step_settings_diff(cstep_ctx), timeout_sec=300)
        rep["errors"] += (rep["settings_diff"].get("errors") or [])
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"settings diff: {str(e)[:200]}")

    # П.14: стандартные возрастные корректировки −100% (<18, 18–24) через v5.
    try:
        rep["age_bidmods"] = _copy_timed(
            job_id, "age_bidmods", lambda: csteps.step_age_bidmods(cstep_ctx), timeout_sec=120)
        rep["errors"] += rep["age_bidmods"].get("errors") or []
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"age bidmods: {str(e)[:200]}")
    # П.13: disabledPlaces копируются 1в1 из источника в target (Grid).
    try:
        rep["disabled_places"] = _copy_timed(
            job_id, "disabled_places", lambda: csteps.step_disabled_places(cstep_ctx), timeout_sec=300)
        rep["errors"] += rep["disabled_places"].get("errors") or []
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"disabled places: {str(e)[:200]}")
    # П.4 (ФАЗА 3b): адаптивные креативы 1:1 по куки (Grid) — заголовки/тексты/картинки источника,
    # гео в тексте с падежами; БЕЗ исходного CreativeId и БЕЗ v5-баллов. ДО step_prices, чтобы
    # adPrice лёг на уже приведённый 1:1 контент (RMW step_prices его сохранит).
    try:
        rep["adaptive_creatives"] = _copy_timed(
            job_id, "adaptive_creatives", lambda: csteps.step_adaptive_creatives(cstep_ctx), timeout_sec=300)
        rep["errors"] += rep["adaptive_creatives"].get("errors") or []
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"adaptive creatives: {str(e)[:200]}")

    # П.8: НОВЫЕ РЕАЛЬНЫЕ цены из ФИДА target-аккаунта на созданные адаптивные объявления (Grid adPrice).
    try:
        rep["prices"] = _copy_timed(job_id, "prices", lambda: csteps.step_prices(cstep_ctx), timeout_sec=120)
        rep["errors"] += rep["prices"].get("errors") or []
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"prices: {str(e)[:200]}")

    # П.12 (ФАЗА 3b/3c): видео 1:1 по куки — ПОСЛЕ prices (attach через RMW сохраняет контент/цену,
    # а step_prices через ce._grid_set_ad_prices слал creativeIds=[] → до него видео стерлось бы).
    # ФАЗА 3c: video_file_resolver теперь заполнен (originalUrl из Grid-интроспекции) → видео
    # реально переносится (скачать mp4 → аплоуд по куки → RMW-привязка). Нет URL/скачивания —
    # честный report-only (внутри step_videos).
    try:
        rep["videos"] = _copy_timed(job_id, "videos", lambda: csteps.step_videos(cstep_ctx), timeout_sec=120)
        rep["errors"] += rep["videos"].get("errors") or []
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"videos: {str(e)[:200]}")

    # Grid-only инварианты tp2/TEXT_CAMPAIGN: enableCompanyInfo=False + EXACT_V2_MARK/WITHOUT_BRAND.
    # Форсируется ДО live_verification, чтобы verify увидел правильные значения (WRONG_AUTOTARGET=0,
    # COMPANY_INFO_ENABLED_LIVE=0). Отложенный delayed_repairs остаётся страховкой.
    try:
        rep["search_invariants"] = _copy_timed(
            job_id, "search_invariants",
            lambda: csteps.step_fix_search_campaign_invariants(cstep_ctx),
            timeout_sec=900)
        rep["errors"] += rep["search_invariants"].get("errors") or []
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"search invariants: {str(e)[:200]}")

    # 3) Grid-first live verification + safe auto-repair.
    results = ce._copy_build_results(src_dir, workdir) + list(body.get("_copy_uac_results") or [])
    copy_body = {
        **body,
        "items": [{"name": r.get("name"), "type": "copy"} for r in results if r.get("name")],
        "callouts": callout_texts,
        "_skip_auto_queued_repair": True,
    }
    try:
        live = ce._create_set_live_verification(target_login, results, agency=target_agency, use_v5=False)
        rep["live_verification"] = live
        try:
            gate = rgate.summarize_repair_gate(copy_body, results, (live or {}).get("repair_plan") or {})
        except Exception as e:  # noqa: BLE001
            gate = {"status": "error", "error": str(e)[:180]}
        rep["repair_gate"] = gate
        if int((gate or {}).get("executable_now") or 0) > 0:
            ctx = {"login": target_login, "agency": target_agency, "body": copy_body, "results": results}
            auto = rauto.execute_safe_post_create(
                target_login,
                ctx,
                (live or {}).get("repair_plan") or {},
                ce._repair_deps(),
                post_verify=ce._attach_post_repair_verification,
            )
            rep["auto_repair"] = auto
            if (auto or {}).get("post_repair_live_verification"):
                rep["live_verification"] = auto["post_repair_live_verification"]
            current_live = rep.get("live_verification") if isinstance(rep.get("live_verification"), dict) else live
            current_plan = (current_live or {}).get("repair_plan") or {}
            image_ids, _image_actions, _ = rgate.executable_images_repairs(current_plan)
            live_errors = int(((current_live or {}).get("summary") or {}).get("errors") or 0)
            if live_errors > 0 and image_ids:
                image_auto = _copy_execute_image_repairs(
                    target_login,
                    ctx,
                    current_plan,
                    ce._repair_deps(),
                    post_verify=ce._attach_post_repair_verification,
                )
                rep["copy_image_repair"] = image_auto
                if image_auto.get("post_repair_live_verification"):
                    rep["live_verification"] = image_auto["post_repair_live_verification"]
                    try:
                        rep["repair_gate"] = rgate.summarize_repair_gate(
                            copy_body,
                            results,
                            (rep["live_verification"] or {}).get("repair_plan") or {},
                        )
                    except Exception as e:  # noqa: BLE001
                        rep["repair_gate"] = {"status": "error", "error": str(e)[:180]}
                if not image_auto.get("ok"):
                    rep["errors"].append(f"images repair: {str(image_auto.get('result') or image_auto)[:220]}")
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"verification/repair: {str(e)[:220]}")

    # Обязательная сверка source↔target после создания (REPORT-ONLY, движок copy_verify).
    _t_verify = time.monotonic()
    try:
        from . import copy_verify as cv
        _verify_cache = _prefetch_copy_verify_grid_cache(
            target_login, target_agency, grid, maps, cstep_ctx, lambda m: ce._copy_job_log(job_id, m)
        )
        verify_result = cv.run_copy_verification(
            src_dir=src_dir, workdir=workdir,
            target_login=target_login, target_agency=target_agency,
            grid=grid, source_grid=cstep_ctx.source_grid,
            geo_pairs=cstep_ctx.geo_pairs or [],
            cached_counts=_verify_cache.get("counts"),
            cached_edit_rows=_verify_cache.get("edit_rows"),
            cached_invariants=_verify_cache.get("invariants"),
            cached_adaptive_src=cstep_ctx.cached_adaptive_src,
            cached_adaptive_tgt=cstep_ctx.cached_adaptive_tgt or _verify_cache.get("adaptive_tgt"),
            log=(lambda m: ce._copy_job_log(job_id, m)),
        )
        ce._copy_job_log(job_id, f"[timing] copy_verify: {time.monotonic() - _t_verify:.0f}s")
        rep["copy_verify"] = verify_result
        _s = verify_result.get("summary") or {}
        ce._copy_job_log(job_id, f"copy_verify: ok={_s.get('ok')}, mismatch={_s.get('mismatch')}, "
                              f"missing={_s.get('missing')}, unreadable={_s.get('unreadable')}")
    except Exception as _ve:  # noqa: BLE001
        rep["errors"].append(f"copy_verify: {str(_ve)[:200]}")

    # B1: авто-ремонт repairable=True измерений (shared_sets D3 + shopping D19).
    # Обновляет report["results"] in-place; ошибки → rep["copy_repair"]["errors"].
    try:
        from . import copy_verify as cv
        _cv_report = rep.get("copy_verify") or {}
        if _cv_report.get("results"):
            _t_repair = time.monotonic()
            _repair_result = cv.run_copy_repair(
                _cv_report,
                src_dir=src_dir,
                workdir=workdir,
                target_login=target_login,
                target_agency=target_agency,
                grid=grid,
                geo_pairs=(cstep_ctx.geo_pairs or []),
                log=(lambda m: ce._copy_job_log(job_id, m)),
            )
            rep["copy_repair"] = _repair_result
            _rr = _repair_result
            ce._copy_job_log(
                job_id,
                f"copy_repair: repairs={len(_rr.get('repairs') or [])}, "
                f"errors={len(_rr.get('errors') or [])}",
            )
            ce._copy_job_log(job_id, f"[timing] copy_repair: {time.monotonic() - _t_repair:.0f}s")
            # sitelinks_present: те же под-копированные кампании иногда не получают набор быстрых
            # ссылок при первом проходе (flux во время создания). step_attach_sitelinks идемпотентен
            # (дедуп через maps["sitelinks"]) — повторный вызов дозакрывает пропущенные кампании.
            try:
                _sl_miss = [
                    r for r in (_cv_report.get("results") or [])
                    if r.get("dimension") == "sitelinks_present"
                    and r.get("status") not in ("ok", "excluded_intentional")
                ]
                if _sl_miss and cstep_ctx.source_grid is not None and grid is not None:
                    _sl_rep = csteps.step_attach_sitelinks(cstep_ctx)
                    ce._copy_job_log(
                        job_id,
                        f"copy_repair sitelinks-retry: наборов {_sl_rep.get('sets_created', 0)}, "
                        f"привязано {_sl_rep.get('attached_campaigns', 0)}, "
                        f"пропущено {_sl_rep.get('skipped', 0)}",
                    )
            except Exception as _sle:  # noqa: BLE001
                rep["errors"].append(f"copy_repair sitelinks-retry: {str(_sle)[:150]}")
            # Дыра «verify нашёл — никто не починил — job done»: строки, которые помечены
            # repairable=True, но после авторемонта всё ещё не ok, раньше нигде не всплывали
            # (repair_gate пустой, статус done). Выносим их явно — в отчёт и в лог джобы.
            _unresolved = [
                {"scope": r.get("scope"), "dimension": r.get("dimension"),
                 "status": r.get("status")}
                for r in (_cv_report.get("results") or [])
                if r.get("repairable") and r.get("status") not in ("ok", "excluded_intentional")
            ]
            if _unresolved:
                rep["verify_unresolved"] = _unresolved
                _dims = sorted({str(u["dimension"]) for u in _unresolved})
                _msg = (f"verify: {len(_unresolved)} расхождений НЕ закрыто авторемонтом "
                        f"({', '.join(_dims[:6])})")
                rep["errors"].append(_msg)
                ce._copy_job_log(job_id, f"⚠️ {_msg}")
    except Exception as _re:  # noqa: BLE001
        rep["errors"].append(f"copy_repair: {str(_re)[:200]}")

    _copy_demote_optional_source_grid_errors(rep, body.get("source_login") or "")
    rep["results"] = results
    return rep

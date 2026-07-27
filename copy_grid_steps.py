"""Grid-докрутка скопированных кампаний: callouts-мост, шаги, видео-резолвер.

Вынесено из copy_engine.py (чистый code-motion, логика не изменена).
DI инъектится copy_engine.configure() фан-аутом; sibling-модули copy_* — прямой импорт (DAG, цикла нет).
"""
from __future__ import annotations

from pathlib import Path

from . import campaign as cmc
from . import grid_finalize as gf

from .copy_geo import _copy_target_href
from .copy_jobs import _copy_job_log
from .copy_snapshot import _copy_read_json, _copy_write_json

# ── DI (инъектится copy_engine.configure фан-аутом; None до инъекции) ──
_CALLOUT_PER_CAMPAIGN_CAP = _account_offer_prices = _direct_tokens = _enabled_baseline_minus_places = _grid_feed_offer_prices = _grid_set_ad_prices = _grid_update_adaptive_ads = _group_ad_price = _resolve_agency_hint = _token_for_login = _v5_call = None


def configure(deps: dict) -> None:
    """Инъекция DI из blueprint (фан-аут из copy_engine.configure)."""
    globals().update(deps)


def _copy_grid_bridge_callouts(source_grid, target_grid, src_dir: Path, maps: dict,
                               *, log=lambda m: None) -> None:
    """Перенести уточнения источника на target ПО ТЕКСТУ (Grid, 0 баллов) и заполнить
    maps['callouts'] = {src_callout_id: tgt_callout_id}.

    Связь campaign→callout_ids даёт campaign_callouts.json (pull_source_campaign_assets), а тексты —
    source_grid.get_callouts() ({текст: id}, инвертируем в {id: текст}). Затем target_grid.add_callouts
    создаёт (с дедупом) те же тексты на target.

    Баг 2b: раньше при source_grid=None (сбой куки источника) — ТИХИЙ no-op, уточнения молча терялись.
    Теперь: если исходные callout-id ЕСТЬ, а source/target grid недоступен — поднимаем ошибку (caller
    вынесет её в rep['errors']). Нет исходных id — реально нечего переносить, тихо ок."""
    links = _copy_read_json(src_dir / "campaign_callouts.json")
    links = links if isinstance(links, dict) else {}
    wanted_ids = {str(x) for co_ids in links.values() for x in (co_ids or []) if str(x).strip()}
    if not wanted_ids:
        return
    if target_grid is None:
        raise RuntimeError("нет target grid-клиента — уточнения не перенесены")
    if source_grid is None:
        raise RuntimeError(
            f"нет source grid-клиента (куки источника) — {len(wanted_ids)} уточнений не перенесены")
    src_text_by_id: dict[str, str] = {}
    try:
        for text, cid in (source_grid.get_callouts() or {}).items():
            src_text_by_id[str(cid)] = text
    except Exception as e:  # noqa: BLE001
        log(f"уточнения: чтение текстов источника не удалось ({str(e)[:150]})")
        return
    texts = list(dict.fromkeys(
        src_text_by_id[i] for i in wanted_ids if i in src_text_by_id and str(src_text_by_id[i]).strip()))
    if not texts:
        return
    try:
        tgt_map = target_grid.add_callouts(texts) or {}   # {текст: tgt_id}
    except Exception as e:  # noqa: BLE001
        log(f"уточнения: создание на target не удалось ({str(e)[:150]})")
        return
    maps.setdefault("callouts", {})
    for src_id in wanted_ids:
        text = src_text_by_id.get(src_id)
        if text and tgt_map.get(text):
            maps["callouts"][str(src_id)] = int(tgt_map[text])
    log(f"уточнения перенесены на target: {len(maps['callouts'])} id (из {len(wanted_ids)} исходных)")


def _copy_grid_unified_steps(job_id: str, body: dict, target_login: str, target_agency: str,
                             src_domain: str, replacements, maps: dict,
                             src_dir: Path, workdir: Path) -> dict:
    """copy_steps-постобработка ЕПК-ветки (cookie/Grid, НОЛЬ v5-баллов).

    Применяет к комбинированным кампаниям, созданным create_full, те же под-сервисы, что и
    v5-snapshot путь (_copy_cookie_postprocess):
      • step_age_bidmods    (п.14) — возраст −100% (<18/18–24) через Grid set_campaign_age_bidmods;
      • step_disabled_places(п.13) — копирование disabledPlaces источника 1в1;
      • step_attach_callouts(п.11) — уточнения по исходной связи (text-bridge источник→target);
      • step_attach_promos  (п.10) — формально (нет source-promo-def reader → безопасный no-op);
      • step_prices         (п.8)  — новые цены из ФИДА target-аккаунта на комбинаторные объявления;
      • step_videos         (п.12) — видео 1:1 по куке (resolver originalUrl из Grid-интроспекции).

    СОЗНАТЕЛЬНО НЕ вызываем (иначе двойная работа — см. ДОРАБОТКА 3):
      • step_keywords — create_full УЖЕ залил ключи по Grid (0 баллов); повтор = дубли фраз;
      • step_adaptive_creatives — create_full УЖЕ собрал комбинированное объявление 1:1
        (заголовки/тексты/картинки источника + гео-склонения применены в _copy_apply_geo_replacements);
        картинки ремапятся ДО create_full (_copy_image_remapper: as-is или переаплоад в target по
        кукам, недоступные дропаются) — повторная запись здесь избыточна.
    """
    from . import copy_steps as csteps
    # [geo-честность] keywords: гео-замена применена ИНЛАЙН в group_specs[:1237] перед create_full;
    # step_keywords скипается — повтор создал бы дубли фраз. Snapshot keywords.json записывается
    # _copy_grid_unified_campaigns с уже замещёнными ключами → check_geo_kw_consistency даёт реальную метрику.
    rep: dict = {"skipped": ["step_keywords (гео-замена применена инлайн в group_specs[:1237]"
                              " перед create_full; повтор = дубли фраз)",
                             "adaptive_creatives (create_full собрал 1:1)"], "errors": []}
    source_login = (body.get("source_login") or "").strip()
    # Баг 2a/3: source-домен для доменной трансформации href быстрых ссылок (step_attach_sitelinks).
    if src_domain and not body.get("_copy_source_domain"):
        body["_copy_source_domain"] = src_domain
    try:
        tgt_uac = cmc.build_client(target_login, account=(target_agency or None))
        tgt_cookie = tgt_uac.sess.headers.get("Cookie") or ""
        grid = gf.GridClient(target_login, cookie=tgt_cookie)
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"target cookie init: {str(e)[:200]}")
        return rep
    source_grid = None
    if source_login:
        try:
            _src_ag = _resolve_agency_hint(source_login, "")
            _src_uac = cmc.build_client(source_login, account=(_src_ag or None))
            source_grid = gf.GridClient(source_login, cookie=(_src_uac.sess.headers.get("Cookie") or ""))
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"source cookie init: {str(e)[:180]}")
    try:
        _tgt_token, _ = _token_for_login(
            target_login, target_agency or _resolve_agency_hint(target_login, ""), _direct_tokens())
    except Exception:  # noqa: BLE001
        _tgt_token = ""

    # Исходные связи campaign→callouts/promo (Grid источника) → src_dir/campaign_callouts.json + promos.
    src_camp_ids = [int(x) for x in (maps.get("campaigns") or {}).keys() if str(x).isdigit()]
    try:
        csteps.pull_source_campaign_assets(
            source_grid, src_camp_ids, src_dir, log=(lambda m: _copy_job_log(job_id, m)))
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"pull source assets: {str(e)[:180]}")
    # Уточнения: тексты source-callout id → создать на target → maps['callouts'].
    try:
        _copy_grid_bridge_callouts(
            source_grid, grid, src_dir, maps, log=(lambda m: _copy_job_log(job_id, m)))
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"callouts bridge: {str(e)[:180]}")

    ctx = csteps.CopyCtx(
        target_login=target_login, target_agency=target_agency or "",
        src_dir=src_dir, workdir=workdir, body=body, maps=maps,
        grid=grid, target_token=_tgt_token or "",
        log=(lambda m: _copy_job_log(job_id, m)),
        v5_call=_v5_call,
        feed_offer_prices=_grid_feed_offer_prices, account_offer_prices=_account_offer_prices,
        group_ad_price=_group_ad_price, set_ad_prices=_grid_set_ad_prices,
    )
    ctx.source_login = source_login
    ctx.source_grid = source_grid
    ctx.geo_pairs = replacements or []
    ctx.update_adaptive_ads = _grid_update_adaptive_ads
    ctx.video_upload_client = tgt_uac
    ctx.video_file_resolver = _copy_make_video_resolver(job_id, source_grid, maps, workdir)
    try:
        from .promo import PromoClient
        ctx.promo_client = PromoClient(tgt_uac, target_login)
    except Exception:  # noqa: BLE001
        ctx.promo_client = None

    # Промоакции: читаем определения промо из сырых строк CampaignsEditData источника и
    # создаём их на target. campaigns_edit_rows возвращает promoExtension{id type prefix amount
    # unit description startDate finishDate href promocode} — полный объект для воссоздания.
    # После этого maps["promotions"] заполнен → step_attach_promos привяжет промо по связи.
    created_promo_ids: list[int] = []
    if source_grid is not None and src_camp_ids and ctx.promo_client is not None:
        try:
            _src_raw = source_grid.campaigns_edit_rows(src_camp_ids)
            _promo_defs: dict[str, dict] = {}
            for _cid, _row in (_src_raw or {}).items():
                _promo = _row.get("promoExtension") or {}
                _pid = str(_promo.get("id") or "")
                if not _pid or _pid == "0":
                    continue
                if not _promo.get("description"):
                    # Задача 3 (Minor #14-M2): явный лог вместо тихого пропуска.
                    _copy_job_log(job_id,
                                  f"промо {_pid} пропущено: нет description "
                                  f"(тип={_promo.get('type')!r}, кампания={_cid})")
                else:
                    _promo_defs[_pid] = _promo
            _src_domain_for_promo = (body.get("_copy_source_domain") or src_domain or "").strip()
            _tgt_domain_for_promo = (body.get("target_domain") or "").strip()
            for _src_pid, _pdef in _promo_defs.items():
                if str(_src_pid) in maps["promotions"]:
                    created_promo_ids.append(int(maps["promotions"][str(_src_pid)]))
                    continue
                _new_pid, _perr = ctx.promo_client.add(
                    type=_pdef.get("type") or "DISCOUNT",
                    description=_pdef.get("description") or "акция",
                    href=_copy_target_href(_pdef.get("href"),
                                          _src_domain_for_promo, _tgt_domain_for_promo),
                    amount=_pdef.get("amount"),
                    unit=_pdef.get("unit"),
                    prefix=_pdef.get("prefix"),
                    promocode=_pdef.get("promocode"),
                    start=_pdef.get("startDate"),
                    finish=_pdef.get("finishDate"),
                )
                if _new_pid:
                    maps["promotions"][str(_src_pid)] = int(_new_pid)
                    created_promo_ids.append(int(_new_pid))
                elif _perr:
                    rep["errors"].append(f"promo {_src_pid}: {_perr[:180]}")
            rep["promos_created"] = len(created_promo_ids)
            _copy_job_log(job_id, f"промо: создано {len(created_promo_ids)} из {len(_promo_defs)} источника")
        except Exception as _e:  # noqa: BLE001
            rep["errors"].append(f"promo defs read/create: {str(_e)[:200]}")

    for name, fn in (
        ("age_bidmods", lambda: csteps.step_age_bidmods(ctx)),
        ("disabled_places", lambda: csteps.step_disabled_places(ctx)),
        ("attach_callouts", lambda: csteps.step_attach_callouts(ctx, per_campaign_cap=_CALLOUT_PER_CAMPAIGN_CAP)),
        # Баг 2a: быстрые ссылки по исходной связи campaign→sitelinkSet (source-grid read → target set).
        ("attach_sitelinks", lambda: csteps.step_attach_sitelinks(ctx)),
        # maps["promotions"] заполнен выше → created_promo_ids содержит реальные target-id.
        ("attach_promos", lambda: csteps.step_attach_promos(ctx, list(created_promo_ids))),
        ("prices", lambda: csteps.step_prices(ctx)),
        ("videos", lambda: csteps.step_videos(ctx)),
    ):
        try:
            r = fn()
            rep[name] = r
            if isinstance(r, dict):
                rep["errors"] += r.get("errors") or []
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"{name}: {str(e)[:200]}")
    _copy_write_json(workdir / "id_maps.json", maps)

    # [geo-честность] check_geo_kw_consistency для ЕПК-пути: читает keywords.json и adgroups.json
    # из синтетического snapshot (уже гео-заменённые+отфильтрованные данные).
    # geo_kw_source_residual == 0 → замена убрала формы источника; geo_neg_target_blocked == 0 →
    # фильтрация убрала минусы с целевым гео. MISMATCH = реальная проблема замены/фильтрации.
    try:
        from . import copy_verify as cv
        _geo_rows = cv.check_geo_kw_consistency(
            src_dir, replacements or [],
            log=(lambda m: _copy_job_log(job_id, m)))
        rep["geo_kw_consistency"] = _geo_rows
    except Exception as _gce:  # noqa: BLE001
        rep["errors"].append(f"geo_kw_consistency: {str(_gce)[:200]}")

    return rep


def _copy_make_video_resolver(job_id: str, source_grid, maps: dict, workdir: Path):
    """ФАЗА 3c п.12: resolver mp4 исходного видео по Grid-интроспекции (originalUrl).

    Один prefetch по куки ИСТОЧНИКА (source_grid.video_creative_urls на src camp/ad из maps) →
    {src_creative_id: originalUrl}. Затем на каждый вызов (meta.creative_id) скачивает mp4 в
    workdir/_video_cache/<cid>.mp4 (кэш — один и тот же ролик у нескольких объявлений качаем раз) и
    отдаёт путь; None — если URL нет или скачать не удалось (step_videos тогда честно репортит).

    Скачиваемость доказана live 2026-07-03: originalUrl отдаёт HTTP 200 video/mp4 без авторизации.
    Приоритет URL: originalUrl (исходник 1:1) → livePreviewUrl (рендер-превью) как запасной."""
    src_camp_ids = [int(x) for x in (maps.get("campaigns") or {}).keys() if str(x).isdigit()]
    src_ad_ids = [int(x) for x in (maps.get("ads") or {}).keys() if str(x).isdigit()]
    url_map: dict[str, dict] = {}
    if source_grid is not None and src_ad_ids:
        try:
            url_map = source_grid.video_creative_urls(src_camp_ids, src_ad_ids) or {}
            if url_map:
                _copy_job_log(job_id, f"видео: Grid-интроспекция дала {len(url_map)} скачиваемых mp4-URL (originalUrl)")
        except Exception as e:  # noqa: BLE001
            _copy_job_log(job_id, f"видео: чтение URL источника не удалось ({str(e)[:150]})")
            url_map = {}
    if not url_map:
        return None
    cache_dir = Path(workdir) / "_video_cache"

    def _fetch_one(cid: str):
        """Скачать mp4 источника в кэш (идемпотентно). → путь | None. Потокобезопасно:
        каждый cid пишет в свой файл, cache_dir.mkdir(exist_ok=True)."""
        cid = str(cid or "").strip()
        info = url_map.get(cid) or {}
        url = info.get("original_url") or info.get("live_preview_url") or ""
        if not cid or not url:
            return None
        cache_dir.mkdir(parents=True, exist_ok=True)
        dst = cache_dir / f"{cid}.mp4"
        if dst.exists() and dst.stat().st_size > 0:
            return str(dst)
        import requests as _rqs
        try:
            with _rqs.get(url, stream=True, timeout=90, verify=False) as r:
                if r.status_code != 200:
                    _copy_job_log(job_id, f"видео {cid}: originalUrl HTTP {r.status_code} — пропуск")
                    return None
                ct = (r.headers.get("Content-Type") or "").lower()
                if "video" not in ct and "octet-stream" not in ct and "mp4" not in ct:
                    _copy_job_log(job_id, f"видео {cid}: неожиданный content-type {ct!r} — пропуск")
                    return None
                with open(dst, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            fh.write(chunk)
        except Exception as e:  # noqa: BLE001
            _copy_job_log(job_id, f"видео {cid}: скачивание не удалось ({str(e)[:120]})")
            try:
                if dst.exists():
                    dst.unlink()
            except OSError:
                pass
            return None
        if dst.stat().st_size <= 0:
            return None
        return str(dst)

    # #23 ускорение: prefetch всех mp4 КОНКУРЕНТНО (независимый сетевой I/O). Раньше step_videos
    # качал по одному лениво (~17с × N ≈ 6.5 мин на 23 ролика — второй по величине хог копии).
    # Резолвер после prefetch отдаёт из кэша мгновенно. Скачивание в разные файлы → потокобезопасно.
    try:
        import time as _time
        from concurrent.futures import ThreadPoolExecutor
        _cids = [str(c) for c in url_map.keys()]
        if _cids:
            _t0 = _time.monotonic()
            with ThreadPoolExecutor(max_workers=min(8, len(_cids))) as _ex:
                _ok = sum(1 for _r in _ex.map(_fetch_one, _cids) if _r)
            _copy_job_log(job_id, f"видео prefetch: {_ok}/{len(_cids)} mp4 за {_time.monotonic()-_t0:.0f}s (параллельно)")
    except Exception as _e:  # noqa: BLE001
        _copy_job_log(job_id, f"видео prefetch пропущен ({str(_e)[:100]}) — ленивое скачивание")

    def _resolver(meta: dict):
        return _fetch_one(str((meta or {}).get("creative_id") or "").strip())

    return _resolver

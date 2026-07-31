"""Creative/video шаги copy-постпроцесса."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Optional

from .copy_context import CopyCtx, _noop_log
from .copy_step_utils import _chunks, _rj, _v5_add_err, _wj


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
           "images_remapped": 0, "images_filled": 0, "multicards_remapped": 0,
           "no_target": 0, "no_content": 0, "errors": []}
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
        src_ads = ctx.cached_adaptive_src
        if src_ads is None:
            src_ads = ctx.source_grid.adaptive_ads_for_update(src_camp_ids, src_ad_ids) or {}
            ctx.cached_adaptive_src = src_ads
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"read source adaptive: {str(e)[:200]}")
        return rep
    rep["src_ads_read"] = len(src_ads)
    if not src_ads:
        ctx.log("адаптивы: у источника нет адаптивных объявлений (GdAdaptiveTextAd) — пропуск")
        return rep

    from . import copy_geo_morph as cgm
    from ..text_norm import _trim_clean
    pairs = ctx.geo_pairs or []
    # image_mode=upload: v5 переносит на объявление только ОДИН AdImageHash, остальные 4 живут лишь в
    # Grid → доливаем картинки ЦЕЛЕВОГО аккаунта round-robin до 5 (максимум Директа). Пул пуст → не трогаем.
    img_pool = ([str(h).strip() for h in (ctx.body.get("image_hashes") or []) if str(h).strip()]
                if str(ctx.body.get("image_mode") or "") == "upload" else [])
    pool_pos = 0
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
            # гео-замена могла УДЛИНИТЬ строку (Москве→Нижневартовске) — обрезка по слову
            # + чистка оборванного хвоста, а не жёсткий срез посреди слова (ревью 06.07).
            # Только при ПРЕВЫШЕНИИ лимита: _trim_clean безусловно срезает хвостовую пунктуацию
            # (rstrip " .,;:!?-") и у копии 1:1 съедал легитимный «!» из текста источника.
            new_titles.append(out if len(out) <= 56 else _trim_clean(out, 56))    # лимит заголовка ≤56
        new_bodies = []
        for b in bodies:
            out, n = cgm.apply_replacements(b, pairs)
            n_geo += n
            new_bodies.append(out if len(out) <= 81 else _trim_clean(out, 81))    # лимит текста ≤81
        if n_geo:
            rep["geo_applied"] += 1
        new_imgs = []
        for h in (comp.get("imageHashes") or []):
            th = img_map.get(h)
            if th:
                new_imgs.append(th)
                rep["images_remapped"] += 1
        if img_pool:
            for _ in range(len(img_pool)):
                if len(new_imgs) >= 5:
                    break
                h = img_pool[pool_pos % len(img_pool)]
                pool_pos += 1
                if h not in new_imgs:
                    new_imgs.append(h)
                    rep["images_filled"] += 1
        item = {"id": int(tgt_ad_id), "titles": new_titles, "bodies": new_bodies}
        if new_imgs:
            item["image_hashes"] = new_imgs            # RMW: пустой → сохранит target-картинки
        new_multicards = []
        for card in (comp.get("multicards") or []):
            if not isinstance(card, dict):
                continue
            src_hash = str(card.get("imageHash") or "").strip()
            tgt_hash = img_map.get(src_hash)
            if not tgt_hash:
                continue
            new_multicards.append({
                "imageHash": tgt_hash,
                "currency": card.get("currency") or None,
                "href": card.get("href") or None,
                "price": card.get("price") or None,
                "priceOld": card.get("priceOld") or None,
                "text": card.get("text") or None,
            })
        if new_multicards:
            item["multicards"] = new_multicards
            rep["multicards_remapped"] += len(new_multicards)
        # отображаемая ссылка источника (linkTail) — часть контента 1:1; иначе на target останется
        # то, что переживёт full-replace (у копий это null)
        if comp.get("displayHref"):
            item["display_href"] = comp["displayHref"]
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
            f"долито {rep['images_filled']}, каруселей-карточек {rep['multicards_remapped']}, "
            f"без target {rep['no_target']}, без контента {rep['no_content']})")
    return rep


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
        src_ads = ctx.cached_adaptive_src
        if src_ads is None:
            src_ads = ctx.source_grid.adaptive_ads_for_update(src_camp_ids, src_ad_ids) or {}
            ctx.cached_adaptive_src = src_ads
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

    # Полный путь (resolver внедрён): скачать → залить по куки → привязать RMW одним батчем.
    upload_cache: dict[str, str] = {}          # src_creative_id → new target creative_id (дедуп аплоуда)
    batch: list[dict] = []                     # накопленные обновления для одного вызова update_adaptive_ads
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
        batch.append({"id": int(tgt_ad_id), "creative_ids": new_cids})
    # Один вызов на весь список — вместо O(N) чтений всего аккаунта по одному объявлению.
    if batch:
        try:
            n = ctx.update_adaptive_ads(
                ctx.target_login, batch, tgt_camp_ids)
            rep["attached"] += int(n or 0)
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"attach batch({len(batch)}): {str(e)[:150]}")
    ctx.log(f"видео 1:1 (куки, 0 баллов): залито {rep['uploaded']}, привязано к {rep['attached']} "
            f"объявлениям (без файла {rep['no_source_file']}, без target {rep['no_target']})")
    return rep

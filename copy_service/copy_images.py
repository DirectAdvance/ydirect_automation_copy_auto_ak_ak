"""Ремап картинок между кабинетами (v501 + Grid хэши).

Вынесено из copy_engine.py (чистый code-motion, логика не изменена).
DI инъектится copy_engine.configure() фан-аутом; sibling-модули copy_* — прямой импорт (DAG, цикла нет).
"""
from __future__ import annotations

from pathlib import Path

from .. import grid_finalize as gf

# ── DI (инъектится copy_engine.configure фан-аутом; None до инъекции) ──
_direct_tokens = _resolve_agency_hint = _token_for_login = _v501_svc = None


def configure(deps: dict) -> None:
    """Инъекция DI из blueprint (фан-аут из copy_engine.configure)."""
    globals().update(deps)


def _copy_grid_ad_image_hashes(ad: dict) -> list[str]:
    out: list[str] = []
    img = ad.get("image")
    if isinstance(img, dict) and img.get("imageHash"):
        out.append(str(img["imageHash"]))
    for item in ad.get("images") or []:
        if isinstance(item, dict) and item.get("imageHash"):
            out.append(str(item["imageHash"]))
    return list(dict.fromkeys(x for x in out if x))


def _copy_v501_ad_image_hashes(login: str, campaign_ids: set[int], agency_hint: str = "") -> dict[int, list[str]]:
    """Best-effort source image read: {adGroupId: [imageHash, ...]}.

    Grid read often returns ``GdTextAd.image`` as null, while v501 exposes
    legacy ``TextAd.AdImageHash`` and responsive ``AdImages``. This is read-only
    and used only to preserve source creatives during cookie copy when available.
    """
    ids = [int(x) for x in (campaign_ids or []) if int(x) > 0]
    if not ids:
        return {}
    try:
        token, _agency = _token_for_login(login, agency_hint or _resolve_agency_hint(login, ""), _direct_tokens())
    except Exception:
        token = None
    if not token:
        return {}
    out: dict[int, list[str]] = {}

    def _add(gid: int, value) -> None:
        if not gid or not value:
            return
        vals = out.setdefault(int(gid), [])
        if isinstance(value, dict):
            value = value.get("Items") or []
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    h = item.get("ImageHash") or item.get("AdImageHash") or item.get("Hash")
                else:
                    h = item
                h = str(h or "").strip()
                if h and h not in vals:
                    vals.append(h)
        else:
            h = str(value or "").strip()
            if h and h not in vals:
                vals.append(h)

    for i in range(0, len(ids), 10):
        params = {
            "SelectionCriteria": {"CampaignIds": ids[i:i + 10]},
            "FieldNames": ["Id", "CampaignId", "AdGroupId", "Type"],
            "TextAdFieldNames": ["AdImageHash"],
            "ResponsiveAdFieldNames": ["AdImages"],
            "Page": {"Limit": 10000, "Offset": 0},
        }
        try:
            data = _v501_svc("ads", "get", token, login, params)
        except Exception:
            continue
        if data.get("error"):
            continue
        for ad in ((data.get("result") or {}).get("Ads") or []):
            try:
                gid = int(ad.get("AdGroupId") or 0)
            except (TypeError, ValueError):
                continue
            if ad.get("TextAd"):
                _add(gid, (ad.get("TextAd") or {}).get("AdImageHash"))
            if ad.get("ResponsiveAd"):
                _add(gid, (ad.get("ResponsiveAd") or {}).get("AdImages"))
    return out


def _copy_image_remapper(source_login: str, source_agency: str, target_login: str,
                         target_agency: str, all_source_hashes, maps: dict, workdir: Path,
                         *, log=lambda m: None, provided_hashes: list | None = None):
    """Build ``fn(src_hashes) -> [target-valid image hashes]`` для ЕПК-ветки копировщика (по кукам).

    Image-хэши в Яндекс.Директе привязаны к АККАУНТУ: source-хэш валиден в target только если такая
    же картинка уже загружена в target (контент-хэш совпал). Иначе AddAdaptiveTextAds падает
    ``BannerDefectIds.Gen.IMAGE_NOT_FOUND`` и роняет ВЕСЬ ad-add кампании (живой инцидент job
    b344eafcdad8: src 712117605/712117626 → 2 битые оболочки).

    Стратегия (п.12 «картинки 1:1», 0 v5-баллов):
      • хэш уже есть в target (v501 ``adimages.get`` target) → используем как есть;
      • иначе скачиваем оригинал источника (v501 ``adimages.get`` source → ``OriginalUrl``, публичный
        avatars-URL) и ПЕРЕАПЛОАДИМ в target по кукам (``gf.GridClient.upload_image`` →
        web-api/image/upload, 0 баллов) → target-хэш, кэшируем в ``maps['images']`` (src→tgt);
      • картинку не удалось скачать/залить → ДРОПАЕМ этот хэш (лог), НЕ роняем ad-add
        (объявление без 1 картинки лучше, чем падение всей кампании).

    mode="other" + provided_hashes: вместо ремапа из источника подставляем загруженные хэши
    ПО КРУГУ (вызов i → hash[i % len(hashes)], детерминировано по порядку вызовов).
    """
    # mode="other": предзагруженные хэши уже в target-аккаунте → round-robin по вызовам.
    if provided_hashes:
        _ph = [str(h).strip() for h in provided_hashes if str(h).strip()]
        if _ph:
            _counter = [0]

            def _remap_provided(src_hashes):  # noqa: ARG001 — src_hashes игнорируется
                idx = _counter[0]
                _counter[0] += 1
                return [_ph[idx % len(_ph)]]

            return _remap_provided

    import requests as _rqs
    maps.setdefault("images", {})
    img_cache = maps["images"]  # src_hash -> tgt_hash (persist across all campaigns of the job)

    # 1) существующие хэши target — их можно ставить как есть (1:1, без переаплоада).
    target_hashes: set[str] = set()
    try:
        tgt_token, _ = _token_for_login(
            target_login, target_agency or _resolve_agency_hint(target_login, ""), _direct_tokens())
    except Exception:  # noqa: BLE001
        tgt_token = None
    if tgt_token:
        data = _v501_svc("adimages", "get", tgt_token, target_login,
                         {"SelectionCriteria": {}, "FieldNames": ["AdImageHash"]})
        for im in ((data.get("result") or {}).get("AdImages") or []):
            h = str(im.get("AdImageHash") or "").strip()
            if h:
                target_hashes.add(h)

    # 2) OriginalUrl источника для хэшей, которых НЕТ в target (кандидаты на переаплоад).
    need = [h for h in {str(x).strip() for x in (all_source_hashes or []) if str(x).strip()}
            if h not in target_hashes]
    src_url_by_hash: dict[str, str] = {}
    if need:
        try:
            src_token, _ = _token_for_login(
                source_login, source_agency or _resolve_agency_hint(source_login, ""), _direct_tokens())
        except Exception:  # noqa: BLE001
            src_token = None
        if src_token:
            for i in range(0, len(need), 100):
                data = _v501_svc("adimages", "get", src_token, source_login,
                                 {"SelectionCriteria": {"AdImageHashes": need[i:i + 100]},
                                  "FieldNames": ["AdImageHash", "OriginalUrl"]})
                for im in ((data.get("result") or {}).get("AdImages") or []):
                    h = str(im.get("AdImageHash") or "").strip()
                    u = str(im.get("OriginalUrl") or "").strip()
                    if h and u:
                        src_url_by_hash[h] = u
        log(f"картинки: target уже имеет {len(target_hashes)} хэшей, к переаплоаду {len(need)} "
            f"(получено URL источника: {len(src_url_by_hash)})")

    cache_dir = Path(workdir) / "_image_cache"
    tgt_grid_holder: dict = {}

    def _tgt_grid():
        if "cli" not in tgt_grid_holder:
            tgt_grid_holder["cli"] = gf.GridClient(target_login)
        return tgt_grid_holder["cli"]

    def _remap(src_hashes):
        out: list[str] = []
        for h in [str(x).strip() for x in (src_hashes or []) if str(x).strip()]:
            if h in target_hashes:                 # уже валиден в target — 1:1 без переаплоада
                out.append(h)
                continue
            if h in img_cache:                     # уже переаплоадили ранее в этом job
                out.append(img_cache[h])
                continue
            url = src_url_by_hash.get(h)
            if not url:
                log(f"картинка {h[:12]}…: нет OriginalUrl источника — дроп (ad-add не падает)")
                continue
            cache_dir.mkdir(parents=True, exist_ok=True)
            dst = cache_dir / f"{h}.img"
            try:
                if not (dst.exists() and dst.stat().st_size > 0):
                    with _rqs.get(url, stream=True, timeout=60, verify=False) as r:
                        if r.status_code != 200:
                            log(f"картинка {h[:12]}…: скачивание HTTP {r.status_code} — дроп")
                            continue
                        with open(dst, "wb") as fh:
                            for chunk in r.iter_content(chunk_size=1 << 16):
                                if chunk:
                                    fh.write(chunk)
                if dst.stat().st_size <= 0:
                    log(f"картинка {h[:12]}…: пустой файл — дроп")
                    continue
            except Exception as e:  # noqa: BLE001
                log(f"картинка {h[:12]}…: скачивание не удалось ({str(e)[:120]}) — дроп")
                continue
            try:
                tgt_hash = _tgt_grid().upload_image(str(dst))
            except Exception as e:  # noqa: BLE001
                log(f"картинка {h[:12]}…: переаплоад в target не удался ({str(e)[:120]}) — дроп")
                tgt_hash = None
            if tgt_hash:
                img_cache[h] = tgt_hash
                target_hashes.add(tgt_hash)
                out.append(tgt_hash)
            else:
                log(f"картинка {h[:12]}…: upload_image вернул пусто — дроп")
        return list(dict.fromkeys(out))

    return _remap

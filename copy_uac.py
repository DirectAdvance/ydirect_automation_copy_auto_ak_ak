"""UAC (Мастер кампаний / Товарные) копирование: чтение и сборка.

Вынесено из copy_engine.py (чистый code-motion, логика не изменена).
DI инъектится copy_engine.configure() фан-аутом; sibling-модули copy_* — прямой импорт (DAG, цикла нет).
"""
from __future__ import annotations

import re

from . import campaign as cmc

from .copy_geo import _copy_target_href

# ── DI (инъектится copy_engine.configure фан-аутом; None до инъекции) ──
_direct_tokens = _resolve_agency_hint = _token_for_login = _v501_svc = None


def configure(deps: dict) -> None:
    """Инъекция DI из blueprint (фан-аут из copy_engine.configure)."""
    globals().update(deps)


def _copy_is_uac_grid_row(row: dict) -> bool:
    typ = str(row.get("typename") or row.get("type") or "").lower()
    name = str(row.get("name") or "").lower()
    return "uac" in typ or "tp6_" in name or "tp7_" in name


def _copy_uac_value(row: dict, *keys, default=None):
    for key in keys:
        val = row.get(key)
        if val not in (None, "", []):
            return val
    return default


def _copy_uac_strings(value, *keys: str, limit: int = 8) -> list[str]:
    """Extract UAC text arrays from either strings or browser dict rows."""
    vals = []
    if isinstance(value, dict):
        raw = _copy_uac_value(value, *keys, default=[])
    else:
        raw = value
    for item in (raw or []):
        if isinstance(item, dict):
            text = ""
            for key in ("text", "title", "value", "body", "name"):
                text = str(item.get(key) or "").strip()
                if text:
                    break
        else:
            text = str(item or "").strip()
        if text and text not in vals:
            vals.append(text)
        if len(vals) >= limit:
            break
    return vals


def _copy_uac_sitelinks(value, *, source_domain: str, target_domain: str) -> list[dict]:
    out = []
    for item in (value or []):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("Title") or item.get("text") or "").strip()
        href = str(item.get("href") or item.get("Href") or item.get("url") or "").strip()
        desc = str(item.get("description") or item.get("Description") or "").strip()
        if not title:
            continue
        out.append({"title": title, "href": _copy_target_href(href, source_domain, target_domain), "description": desc})
    return out


def _copy_uac_media_urls(row: dict, *, want: str) -> list[str]:
    """Find reusable media URLs in unstable UAC detail payloads."""
    rx = re.compile(r"https?://[^\s\"'<>]+", re.I)
    urls: list[str] = []
    seen: set[str] = set()
    image_ext = (".jpg", ".jpeg", ".png", ".webp", ".gif")
    video_ext = (".mp4", ".mov", ".webm")
    preferred_keys = {
        "image": ("images", "image_urls", "media", "contents", "content"),
        "video": ("videos", "video_urls", "media", "contents", "content"),
    }.get(want, ())

    def ok(url: str) -> bool:
        low = url.lower().split("?", 1)[0]
        if want == "video":
            return low.endswith(video_ext)
        return low.endswith(image_ext) or any(x in low for x in ("/image/", "/img/", "avatars.mds.yandex.net"))

    def walk(node):
        if isinstance(node, dict):
            for key, val in node.items():
                key_l = str(key).lower()
                if key_l in {"url", "href", "source_url", "preview_url", "download_url"} and isinstance(val, str):
                    for u in rx.findall(val):
                        if ok(u) and u not in seen:
                            seen.add(u); urls.append(u)
                else:
                    walk(val)
        elif isinstance(node, list):
            for val in node:
                walk(val)
        elif isinstance(node, str):
            for u in rx.findall(node):
                if ok(u) and u not in seen:
                    seen.add(u); urls.append(u)

    for key in preferred_keys:
        walk(row.get(key))
    return urls[:5 if want == "image" else 2]


def _copy_uac_filter_list(value) -> list[dict]:
    if isinstance(value, list):
        return value
    return []


def _copy_uac_campaigns(source_login: str, target_login: str, target_agency: str,
                        selected_grid_rows: list[dict], body: dict, *,
                        target_href: str, region_ids: list[int], counter_id: int,
                        goal_id: int, target_feed_id: int | None,
                        feed_map: dict | None = None) -> dict:
    """Recreate selected UAC/tp6/tp7 campaigns from source detail into target account."""
    rep = {"created": 0, "results": [], "errors": [], "uses_direct_units": False}
    rows = [r for r in selected_grid_rows if _copy_is_uac_grid_row(r)]
    if not rows:
        return rep
    try:
        from .uac_read import UacReadClient
        source_reader = UacReadClient(source_login)
        target_client = cmc.build_client(target_login, account=(target_agency or None))
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"uac init: {str(e)[:220]}")
        return rep

    default_cpa = int(body.get("cpa") or 2000)
    default_budget = int(body.get("week_budget") or body.get("budget") or 5000)

    # image_mode=upload: картинки берём из ЦЕЛЕВОГО аккаунта (уже залиты copy_other._copy_images_upload).
    # Иначе upload_content качает файл источника → одинаковый хэш и бренд-тема источника во всех копиях.
    tgt_img_urls: list[str] = []
    if str(body.get("image_mode") or "") == "upload":
        hashes = [str(h).strip() for h in (body.get("image_hashes") or []) if str(h).strip()]
        if hashes:
            try:
                _tt, _ = _token_for_login(
                    target_login, target_agency or _resolve_agency_hint(target_login, ""), _direct_tokens())
            except Exception:  # noqa: BLE001
                _tt = None
            for i in range(0, len(hashes), 100) if _tt else []:
                try:
                    data = _v501_svc("adimages", "get", _tt, target_login,
                                     {"SelectionCriteria": {"AdImageHashes": hashes[i:i + 100]},
                                      "FieldNames": ["AdImageHash", "OriginalUrl"]})
                except Exception:  # noqa: BLE001
                    continue
                for im in ((data.get("result") or {}).get("AdImages") or []):
                    u = str(im.get("OriginalUrl") or "").strip()
                    if u and u not in tgt_img_urls:
                        tgt_img_urls.append(u)

    for cidx, row in enumerate(rows):
        try:
            src_id = int(row.get("id") or 0)
        except (TypeError, ValueError):
            src_id = 0
        name = str(row.get("name") or "").strip() or f"copy-uac-{src_id}"
        if src_id <= 0:
            continue
        try:
            d = source_reader.campaign_detail(src_id)
            source_domain = str(body.get("_copy_source_domain") or "").strip()
            target_domain = str(body.get("target_domain") or "").strip()
            titles = _copy_uac_strings(d, "titles", "title_items", limit=5)
            texts = _copy_uac_strings(d, "texts", "text_items", limit=3)
            sitelinks = _copy_uac_sitelinks(_copy_uac_value(d, "sitelinks", default=[]) or [],
                                            source_domain=source_domain, target_domain=target_domain)
            keywords = _copy_uac_strings(d, "keywords", limit=200)
            minus_keywords = _copy_uac_strings(d, "minus_keywords", limit=200)
            audiences = _copy_uac_value(d, "audiences", "interest_ids", default=[]) or []
            pricing = str(_copy_uac_value(d, "pricing", "payment_type", "paymentType", default="PER_CLICK") or "PER_CLICK")
            week_limit = _copy_uac_value(d, "week_limit", "weekly_budget", "weekBudget", default=default_budget)
            cpa = default_cpa
            goals = _copy_uac_value(d, "goals", default=[]) or []
            if isinstance(goals, list) and goals and isinstance(goals[0], dict):
                cpa = int(goals[0].get("cpa") or cpa)
            if not titles:
                titles = ["Автомобили в наличии", "Выгода на авто", "Официальный дилер"]
            if not texts:
                texts = ["Подберите автомобиль с выгодой. Оставьте заявку на сайте."]
            src_feed_raw = _copy_uac_value(d, "feed_id", "listings_feed_id")
            is_product = name.lower().startswith("tp7_") or bool(src_feed_raw)
            # Пофидовая замена: если исходный фид кампании есть в feed_map — берём целевой из карты,
            # иначе фолбэк на общий target_feed_id (прежнее поведение).
            eff_target_feed = target_feed_id
            try:
                _sf = str(int(src_feed_raw)) if src_feed_raw not in (None, "") else ""
            except (TypeError, ValueError):
                _sf = ""
            if feed_map and _sf and _sf in feed_map:
                eff_target_feed = feed_map[_sf]
            feed_id = int(eff_target_feed or 0) if is_product else None
            # Детерминированный round-robin: у каждой i-й МК свои 5 картинок из архива цели.
            if tgt_img_urls:
                _img = [tgt_img_urls[(cidx * 5 + k) % len(tgt_img_urls)] for k in range(5)]
            else:
                _img = _copy_uac_media_urls(d, want="image")
            # socdem источника, иначе датакласс молча подставит дефолт age_18 вместо возраста источника.
            _sd = _copy_uac_value(d, "socdem", default={}) or {}
            if not isinstance(_sd, dict):
                _sd = {}
            # Таргетинг-поля источника: не передашь — датакласс молча подставит свой дефолт (как было с socdem).
            _dev = _copy_uac_value(d, "device_types", default=[]) or []
            _dev = [str(x).strip() for x in _dev if str(x).strip()] if isinstance(_dev, list) else []
            _mreg_raw = _copy_uac_value(d, "minus_regions", "minus_region_ids", default=[]) or []
            _mreg: list[int] = []
            for _r in (_mreg_raw if isinstance(_mreg_raw, list) else []):
                try:
                    _mreg.append(int(_r))
                except (TypeError, ValueError):
                    continue
            _rm = _copy_uac_value(d, "relevance_match", default={}) or {}
            if not isinstance(_rm, dict):
                _rm = {}
            _rm_cats = [str(c).strip() for c in (_rm.get("categories") or []) if str(c).strip()]
            _ttg = _copy_uac_value(d, "time_target", default={}) or {}
            if not isinstance(_ttg, dict):
                _ttg = {}
            try:
                _tz = int(_ttg.get("id_time_zone") or 130)
            except (TypeError, ValueError):
                _tz = 130
            _extra = {"relevance_match_categories": _rm_cats} if _rm_cats else {}
            spec = cmc.MasterCampaignSpec(
                href=target_href,
                titles=titles[:5],
                texts=texts[:3],
                region_ids=region_ids,
                counter_id=int(counter_id),
                goal_id=int(goal_id),
                cpa=cpa,
                week_budget=float(week_limit or default_budget),
                campaign_type=("product" if is_product else "master"),
                feed_id=feed_id,
                listings_feed_id=feed_id,
                feed_filters=_copy_uac_filter_list(d.get("feed_filters")),
                listings_feed_filters=_copy_uac_filter_list(d.get("listings_feed_filters")),
                display_name=name,
                pricing=pricing,
                keywords=keywords,
                minus_keywords=minus_keywords or ["отзывы"],
                sitelinks=sitelinks,
                image_urls=_img,
                video_urls=_copy_uac_media_urls(d, want="video"),
                audiences=audiences if isinstance(audiences, list) else [],
                genders=_sd.get("genders") or ["female", "male"],
                age_lower=str(_sd.get("age_lower") or "age_18"),
                age_upper=str(_sd.get("age_upper") or "age_inf"),
                limit_period=str(_copy_uac_value(d, "limit_period", "limitPeriod", default="week") or "week"),
                device_types=_dev or ["all"],
                minus_regions=_mreg,
                id_time_zone=_tz,
                # Наш стандарт tp6/tp7 (create_set_master_product.py:692, коды uac_verifier
                # UAC_ALTERNATIVE_TEXTS_ENABLED / UAC_MAPS_ENABLED) — сильнее «копии 1:1».
                alternative_texts_enabled=False,
                ml_banners_enabled=False,
                yandex_maps_enabled=False,
                utm_template=cmc.UTM_TEMPLATE,
                **_extra,
            )
            cid = target_client.create_master_campaign(spec, launch=False)
            res = {"ok": True, "id": int(cid), "campaign_id": int(cid), "name": name, "kind": "uac", "source_id": src_id}
            rep["results"].append(res)
            rep["created"] += 1
        except Exception as e:  # noqa: BLE001
            msg = str(e)[:260]
            rep["errors"].append(f"{name}: {msg}")
            rep["results"].append({"ok": False, "name": name, "source_id": src_id, "error": msg})
    return rep

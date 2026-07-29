"""Prepare/warm-up helpers for queued create_set jobs.

Пока джоба создания набора ждёт в очереди (`status='queued'`), фоновый поток
греет ВСЁ тяжёлое В ТОМ ЖЕ ПРОЦЕССЕ, чтобы воркер, забрав джобу, сразу создавал
кампании, а не блокировался на:
  1) генерации M3-контента (тот же `_cached_campaign_content` + общий `_CONTENT_CACHE`);
  2) скачивании видео (`kp.videos_pool_for_ct` — качает mp4 в локальный кэш LXC);
  3) мердже цен по аккаунту (`_account_offer_prices` — кэш ключёван по login);
  4) валидации рабочей куки (`pick_working_cookie` — раннее выявление протухшей сессии).

Flask-free: все зависимости blueprint инъектятся через ``configure(deps)`` по
образцу create_set_* модулей. ``start_prefetch`` остался best-effort warm-up.
``prepare_job`` делает только короткий обязательный минимум перед первой
Direct-мутацией: цены и медиа для UAC/посевов. Тяжёлые картинки tp1 греются
отдельным фоном уже параллельно созданию tp2-tp7, а regular AI-контент
генерируется по item перед созданием кампании.
"""
from __future__ import annotations

import logging
import json
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

_log = logging.getLogger("direct.prefetch")

_DEPS: dict = {}

# Греется НЕ БОЛЕЕ ОДНОЙ джобы одновременно (прогрев тяжёлый по CPU/сети).
# Блокирующее взятие лока сериализует прогревы: вторая queued-джоба ждёт своей
# очереди за первой. Как только она уходит в running (или отменена) — is_cancelled
# вернёт True сразу после взятия лока, и поток выйдет без лишней работы.
_PREFETCH_LOCK = threading.Lock()

# Суммарный бюджет прогрева видео (сек). Остаток догрузится уже внутри джобы.
_VIDEO_BUDGET_SEC = 60.0
# Максимум ожидания лока прогрева (защита от бесконечного накопления потоков).
_LOCK_WAIT_SEC = 1800.0

_CT_RE = re.compile(r"ct\d{4}")
_JOB_CACHE_TABLE = "public.direct_create_set_job_cache"
_COMMON_IMAGE_CTS = {
    "ct0000", "ct0001", "ct0002", "ct0003", "ct0004", "ct0005", "ct0006",
    "ct0007", "ct0008", "ct0009", "ct0010", "ct0013", "ct0014",
    "ct0015", "ct0016", "ct0017", "ct0018",
}


def configure(deps: dict) -> None:
    """Bind blueprint dependencies used by prefetch (no Flask imports here)."""
    _DEPS.clear()
    _DEPS.update(deps)


def _dep(name: str) -> Any:
    return _DEPS.get(name)


def _job_id(body: dict) -> str:
    return str((body or {}).get("_job_id") or "").strip()


def _jsonable(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001
        return {"value": str(value)}


def _cache_key(kind: str, key_parts) -> str:
    try:
        raw = json.dumps([kind, key_parts], ensure_ascii=False, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        raw = f"{kind}:{key_parts!r}"
    import hashlib
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _ensure_job_cache(cur) -> None:
    cur.execute(
        f"CREATE TABLE IF NOT EXISTS {_JOB_CACHE_TABLE} ("
        " job_id text NOT NULL,"
        " kind text NOT NULL,"
        " cache_key text NOT NULL,"
        " payload jsonb NOT NULL,"
        " created_at timestamptz NOT NULL DEFAULT now(),"
        " PRIMARY KEY(job_id, kind, cache_key))"
    )


def _save_job_cache(job_id: str, kind: str, key_parts, payload) -> bool:
    if not job_id:
        return False
    try:
        from ..core.direct_repository import victory_conn_rw  # noqa: PLC0415
        conn = victory_conn_rw()
    except Exception:  # noqa: BLE001
        return False
    try:
        cur = conn.cursor()
        _ensure_job_cache(cur)
        cur.execute(
            f"INSERT INTO {_JOB_CACHE_TABLE}(job_id, kind, cache_key, payload, created_at) "
            "VALUES (%s, %s, %s, %s::jsonb, now()) "
            "ON CONFLICT(job_id, kind, cache_key) DO UPDATE SET "
            "payload=EXCLUDED.payload, created_at=now()",
            (job_id, kind, _cache_key(kind, key_parts),
             json.dumps(_jsonable(payload), ensure_ascii=False)),
        )
        conn.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        _log.warning("job-cache save failed job=%s kind=%s: %s", job_id, kind, str(exc)[:160])
        return False
    finally:
        conn.close()


def cleanup_job_cache(job_id: str) -> int:
    """Delete temporary prepare rows for one create-set job."""
    job_id = str(job_id or "").strip()
    if not job_id:
        return 0
    try:
        from ..core.direct_repository import victory_conn_rw  # noqa: PLC0415
        conn = victory_conn_rw()
    except Exception:  # noqa: BLE001
        return 0
    try:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {_JOB_CACHE_TABLE} WHERE job_id=%s", (job_id,))
        n = int(cur.rowcount or 0)
        conn.commit()
        return n
    except Exception:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0
    finally:
        conn.close()


def _resolve_site_city(login: str, body: dict) -> tuple[dict | None, str, str, str]:
    """Тот же вывод site_type/city/href, что prepare_create_set_account —
    чтобы ключ контент-кэша прогрева СОВПАДАЛ с ключом, который использует джоба."""
    account_ctx = _dep("account_ctx")
    ctx = account_ctx(login) if account_ctx else None
    if not ctx:
        return None, "", "", ""
    site_type = (body.get("site_type") or "").strip() or (ctx.get("site_type") or "")
    city = ctx.get("city") or ""
    domain = ctx.get("domain") or ""
    href = ("https://" + domain) if domain else ""
    return ctx, site_type, city, href


def _warm_content(login: str, body: dict, site_type: str, city: str,
                  is_cancelled: Callable[[], bool]) -> int:
    """Прогрев M3-контента через тот же _cached_campaign_content (пишет в общий
    _CONTENT_CACHE, fast_mode=False). Ключ считается тем же _content_cache_key и
    с той же RAW city, что и в orchestrator — иначе джоба сгенерит заново."""
    agent_key = (body.get("agent") or "").strip()
    items = [it for it in (body.get("items") or []) if isinstance(it, dict)]
    if not agent_key or not items:
        return 0
    get_agent = _dep("get_agent")
    cached_content = _dep("cached_campaign_content")
    content_cache_key = _dep("content_cache_key")
    cache = _dep("content_cache")
    cache_lock = _dep("content_cache_lock")
    if not (get_agent and cached_content and content_cache_key):
        return 0
    try:
        agent_obj = get_agent(agent_key)
    except Exception:  # noqa: BLE001
        agent_obj = None
    if not agent_obj:
        _log.warning("prefetch content: слепок %r не найден — пропуск", agent_key)
        return 0
    ak = agent_key.strip().lower()
    warmed = 0
    seen: set = set()
    for it in items:
        if is_cancelled():
            break
        # Ручной путь: контент уже в item — генерировать нечего.
        if it.get("titles") and it.get("texts") and it.get("sitelinks"):
            continue
        try:
            key = content_cache_key(ak, site_type, city, it)
        except Exception:  # noqa: BLE001
            continue
        if key in seen:
            continue
        seen.add(key)
        # Уже в кэше (парная кампания/повтор набора) — не греем повторно.
        if cache is not None:
            if cache_lock is not None:
                with cache_lock:
                    hit = bool(cache.get(key))
            else:
                hit = bool(cache.get(key))
            if hit:
                continue
        try:
            cached_content(login, agent_obj, ak, it, site_type, city, [])
            warmed += 1
        except Exception as e:  # noqa: BLE001
            _log.warning("prefetch content warm failed key=%s: %s", key, str(e)[:160])
    return warmed


def _collect_cts(body: dict) -> list[str]:
    """ct всех items (+ brand ct кодера). Модельные ct брендов НЕ разворачиваем
    вручную: videos_pool_for_ct сам делает brand-fallback (брендовый ct → ролики
    модельных ct того же бренда)."""
    brand_ct = _dep("brand_ct_from_coder")
    items = [it for it in (body.get("items") or []) if isinstance(it, dict)]
    seen: set = set()
    cts: list[str] = []

    def _push(ct: str) -> None:
        ct = (ct or "").strip()
        if ct and ct != "ct0000" and ct not in seen:
            seen.add(ct)
            cts.append(ct)

    for it in items:
        if brand_ct:
            try:
                _, ct = brand_ct(it)
                _push(ct)
            except Exception:  # noqa: BLE001
                pass
        for k in ("coder_ct", "ct", "gc", "code", "c", "name"):
            for m in _CT_RE.findall(str(it.get(k) or "")):
                _push(m)
    return cts


def _warm_videos(body: dict, is_cancelled: Callable[[], bool]) -> tuple[int, int]:
    """Скачать mp4 в локальный кэш LXC для каждого уникального ct. Лимит по времени:
    не более _VIDEO_BUDGET_SEC суммарно — остаток догрузится в джобе."""
    videos_pool = _dep("videos_pool_for_ct")
    if not videos_pool:
        return 0, 0
    cts = _collect_cts(body)
    warmed = 0
    t0 = time.time()
    for ct in cts:
        if is_cancelled() or (time.time() - t0) > _VIDEO_BUDGET_SEC:
            break
        try:
            videos_pool(ct, 2)
            warmed += 1
        except Exception as e:  # noqa: BLE001
            _log.warning("prefetch video warm failed ct=%s: %s", ct, str(e)[:160])
    return warmed, len(cts)


def _warm_prices(login: str, href: str) -> bool:
    """Прогрев мерджа цен аккаунта. Кэш _OFFER_PRICE_CACHE ключёван по (login,url) —
    прогрев = один вызов, функция сама кладёт результат в кэш."""
    fn = _dep("account_offer_prices")
    if not (fn and href):
        return False
    try:
        return bool(fn(login, href))
    except Exception as e:  # noqa: BLE001
        _log.warning("prefetch price warm failed login=%s: %s", login, str(e)[:160])
        return False


def _prepare_prices(login: str, href: str, body: dict) -> dict:
    fn = _dep("account_offer_prices")
    if not (fn and href):
        return {"ok": False, "entries": 0}
    try:
        price_map = dict(fn(login, href) or {})
    except Exception as e:  # noqa: BLE001
        _log.warning("prepare price failed login=%s: %s", login, str(e)[:160])
        return {"ok": False, "entries": 0, "error": str(e)[:200]}
    rep = {"ok": True, "entries": len(price_map), "href": href}
    _save_job_cache(_job_id(body), "prices", (login, href), price_map)
    return rep


def _warm_cookie(login: str) -> bool:
    """Ранняя валидация рабочей куки. Нет куки → warning (куки-путь может отказать)."""
    fn = _dep("pick_working_cookie")
    if not fn:
        return False
    try:
        cookie = fn(login)
    except Exception as e:  # noqa: BLE001
        _log.warning("prefetch cookie warm failed login=%s: %s", login, str(e)[:160])
        return False
    if not cookie:
        _log.warning("prefetch: рабочей куки для %s нет — куки-путь может отказать", login)
        return False
    return True


def _content_apply(item: dict, content: dict | None) -> bool:
    if not isinstance(content, dict):
        return False
    changed = False
    for key in ("titles", "texts", "sitelinks"):
        if content.get(key) and not item.get(key):
            item[key] = content[key]
            changed = True
    if content.get("title2") and not item.get("title2"):
        item["title2"] = content["title2"]
        changed = True
    return changed


def _prepare_ai_content(login: str, body: dict, site_type: str, city: str,
                        is_cancelled: Callable[[], bool]) -> dict:
    """Generate all regular campaign AI content before creating any Direct object."""
    if not body.get("stream_content"):
        return {"items": 0, "generated": 0, "skipped": "stream_content_off"}
    agent_key = (body.get("agent") or "").strip()
    items = [it for it in (body.get("items") or []) if isinstance(it, dict)]
    get_agent = _dep("get_agent")
    cached_content = _dep("cached_campaign_content")
    content_cache_key = _dep("content_cache_key")
    account_put = _dep("account_content_put")
    if not (agent_key and get_agent and cached_content and content_cache_key):
        return {"items": len(items), "generated": 0, "skipped": "deps_missing"}
    try:
        agent_obj = get_agent(agent_key)
    except Exception:  # noqa: BLE001
        agent_obj = None
    if not agent_obj:
        return {"items": len(items), "generated": 0, "skipped": "agent_missing"}
    llm_provider = str(body.get("llm_provider") or "").strip().lower()
    generated = reused = failed = 0
    seen: dict[tuple, dict] = {}
    for it in items:
        if is_cancelled():
            break
        if it.get("type") in ("post_tp8", "post_tp9", "post_tp10"):
            continue
        if it.get("titles") and it.get("texts") and it.get("sitelinks"):
            continue
        try:
            key = content_cache_key(agent_key.lower(), site_type, city, it)
        except Exception:  # noqa: BLE001
            key = tuple()
        if key and key in seen:
            if _content_apply(it, seen[key]):
                reused += 1
            continue
        src = dict(it)
        if llm_provider and not str(src.get("llm_provider") or "").strip():
            src["llm_provider"] = llm_provider
        try:
            content = cached_content(login, agent_obj, agent_key.lower(), src, site_type, city, [], False)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            _log.warning("prepare content failed login=%s key=%r: %s", login, key, str(exc)[:160])
            continue
        if content:
            if key:
                seen[key] = content
                if callable(account_put):
                    try:
                        account_put(login, key, content)
                    except Exception:  # noqa: BLE001
                        pass
            _content_apply(it, content)
            _save_job_cache(_job_id(body), "content", key or it.get("name") or "", content)
            generated += 1
        else:
            failed += 1
    return {"items": len(items), "generated": generated, "reused": reused, "failed": failed}


def _prepare_post_content(login: str, body: dict, site_type: str, href: str,
                          is_cancelled: Callable[[], bool]) -> dict:
    """Generate tp8/tp9/tp10 post title/body before campaign creation."""
    items = [it for it in (body.get("items") or []) if isinstance(it, dict)]
    post_items = [it for it in items if it.get("type") in ("post_tp8", "post_tp9", "post_tp10")]
    if not post_items:
        return {"items": 0, "generated": 0}
    generated = failed = 0
    avoid: list[str] = []
    try:
        from ..ai_content import generate_post_ad_content as gpac  # noqa: PLC0415
        from .create_set_tp8_10 import (
            _post_allowed_models_from_feed, _post_feed_url_map, _post_href_for_label,
            _safe_post_brand_label,
        )
    except Exception as exc:  # noqa: BLE001
        return {"items": len(post_items), "generated": 0, "failed": len(post_items), "error": str(exc)[:200]}
    feed_url_map = {}
    try:
        feed_url_map = _post_feed_url_map(login, href)
    except Exception:  # noqa: BLE001
        feed_url_map = {}
    for it in post_items:
        if is_cancelled():
            break
        if (it.get("title") or it.get("post_title")) and (it.get("body") or it.get("post_body")):
            continue
        raw_brand = re.sub(r"\s+", " ", str(it.get("brand_label") or "Посевы").strip())
        ct = str(it.get("ct") or "ct0000")
        brand_label = _safe_post_brand_label(raw_brand, ct, href)
        if raw_brand and raw_brand.lower() != "посевы" and brand_label == "Посевы":
            # Keep the same recovery rule as run_create_set_post when feed URL proves the label.
            if any(raw_brand.lower() in str(v).lower() for v in (feed_url_map or {}).values()):
                brand_label = raw_brand
        try:
            post_href = _post_href_for_label(login, href, brand_label, ct=ct, site_type=site_type)
            allowed_models = _post_allowed_models_from_feed(login, href, brand_label)
            domain = urllib.parse.urlparse(post_href or href or "").netloc or ""
            content = gpac(
                slepok=(body.get("agent") or "").strip(),
                site_type=site_type or "Монобренд",
                brand=brand_label if brand_label and brand_label != "Посевы" else "",
                city=str(it.get("oblast") or ""),
                domain=domain,
                avoid=avoid,
                allowed_models=allowed_models,
            )
            if content.get("title") and not (it.get("title") or it.get("post_title")):
                it["post_title"] = content["title"]
            if content.get("body") and not (it.get("body") or it.get("post_body")):
                it["post_body"] = content["body"]
            if content.get("title"):
                avoid.append(content["title"])
            _save_job_cache(_job_id(body), "post_content", it.get("name") or raw_brand or ct, content)
            generated += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            _log.warning("prepare post content failed login=%s item=%s: %s",
                         login, it.get("name") or it.get("brand_label"), str(exc)[:160])
    return {"items": len(post_items), "generated": generated, "failed": failed}


def _prepare_tp1_images(login: str, body: dict, site_type: str, slepok: str, grid_cookie: str | None) -> dict:
    preup = _dep("preupload_tp1_images")
    items = [it for it in (body.get("items") or []) if isinstance(it, dict)]
    if not any((it.get("type") or "") == "tp1_rsy" for it in items):
        return {"ok": True, "skipped": "no_tp1"}
    if not callable(preup):
        return {"ok": False, "skipped": "deps_missing"}
    try:
        rep = preup(login, items, site_type, slepok, grid_cookie=grid_cookie) or {}
        _save_job_cache(_job_id(body), "images_tp1", login, rep)
        return {"ok": True, **rep}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


def _prepare_uac_assets(login: str, body: dict, site_type: str, ctx: dict,
                        grid_cookie: str | None, is_cancelled: Callable[[], bool]) -> dict:
    items = [it for it in (body.get("items") or []) if isinstance(it, dict)]
    uac_items = [it for it in items if it.get("type") in ("master_campaign", "product_campaign")
                 or str(it.get("tp") or "") in ("tp6", "tp7")]
    if not uac_items:
        return {"items": 0, "uploaded": 0}
    brand_ct = _dep("brand_ct_from_coder")
    image_ct = _dep("image_ct_for_content")
    creative = _dep("creative_images_for_ct")
    pick_cookie = _dep("pick_working_cookie")
    if not (brand_ct and image_ct and creative and pick_cookie):
        return {"items": len(uac_items), "uploaded": 0, "skipped": "deps_missing"}
    cookie = grid_cookie or pick_cookie(login)
    if not cookie:
        return {"items": len(uac_items), "uploaded": 0, "skipped": "cookie_missing"}
    try:
        from ..core import campaign as cmc  # noqa: PLC0415
        cli = cmc.UacClient(cookie, login)
    except Exception as exc:  # noqa: BLE001
        return {"items": len(uac_items), "uploaded": 0, "error": str(exc)[:200]}
    skey = (body.get("agent") or "").strip().lower()
    norm_domain = re.sub(r"^https?://", "", (ctx.get("domain") or "")).lower()
    norm_domain = re.sub(r"^www\.", "", norm_domain).rstrip("/").strip()
    img_domain = norm_domain if skey == "dmp" else ""
    uploaded = failed = 0
    for it in uac_items:
        if is_cancelled():
            break
        if it.get("preloaded_content_ids"):
            continue
        try:
            c_brand, c_ct = brand_ct(it)
            it_tp = it.get("tp") or ("tp7" if it.get("type") == "product_campaign" else "tp6")
            img_ct = image_ct(c_ct) if c_ct and c_ct not in _COMMON_IMAGE_CTS else "ct0000"
            paths = list(dict.fromkeys(creative(site_type, it_tp, img_ct, skey, domain=img_domain, limit=12) or []))[:12]
        except Exception:  # noqa: BLE001
            paths = []
        content_ids: list[str] = []
        for p in paths:
            if len([x for x in content_ids if x]) >= 5:
                break
            try:
                cid = cli.upload_image_file(p)
                if cid and cid not in content_ids:
                    content_ids.append(cid)
            except Exception:  # noqa: BLE001
                failed += 1
            time.sleep(0.3)
        try:
            c_brand, c_ct = brand_ct(it)
            videos = []
            try:
                from .. import kontent_pack as kp  # noqa: PLC0415
                # slepok=skey: свой слепок → общий пул → чужой слепок (правило Семёна 2026-07-28).
                videos = (kp.videos_for_ct(login, c_ct, brand_hint=(c_brand or ""), slepok=skey)
                          if c_ct else []) or kp.videos_for_login(login)
            except Exception:  # noqa: BLE001
                videos = []
            for vp in (videos or [])[:2]:
                try:
                    cid = cli.upload_video_file(vp)
                    if cid and cid not in content_ids:
                        content_ids.append(cid)
                except Exception:  # noqa: BLE001
                    failed += 1
                time.sleep(0.3)
        except Exception:  # noqa: BLE001
            pass
        if content_ids:
            it["preloaded_content_ids"] = content_ids
            uploaded += len(content_ids)
            _save_job_cache(_job_id(body), "uac_content_ids", it.get("name") or str(it), content_ids)
    return {"items": len(uac_items), "uploaded": uploaded, "failed_uploads": failed}


def _prepare_post_images(login: str, body: dict, grid_cookie: str | None,
                         is_cancelled: Callable[[], bool]) -> dict:
    post_items = [it for it in (body.get("items") or []) if isinstance(it, dict)
                  and it.get("type") in ("post_tp8", "post_tp9", "post_tp10")]
    if not post_items:
        return {"items": 0, "uploaded": 0}
    try:
        from ..clients import grid_finalize as gf  # noqa: PLC0415
        from .create_set_tp8_10 import _posevy_images_for_ct, _resolve_img_ct, POST_IMAGE_LIMIT
        cli = gf.get_grid_client(login, cookie=grid_cookie)
        cli._bootstrap_csrf()
    except Exception as exc:  # noqa: BLE001
        return {"items": len(post_items), "uploaded": 0, "error": str(exc)[:200]}
    uploaded = failed = 0
    for it in post_items:
        if is_cancelled():
            break
        if it.get("preloaded_post_image_hashes"):
            continue
        img_ct = _resolve_img_ct(str(it.get("ct") or "ct0000"))
        hashes: list[str] = []
        for path in _posevy_images_for_ct(img_ct, limit=POST_IMAGE_LIMIT):
            try:
                h = cli.upload_image(path)
                if h and h not in hashes:
                    hashes.append(h)
            except Exception:  # noqa: BLE001
                failed += 1
        if hashes:
            it["preloaded_post_image_hashes"] = hashes
            uploaded += len(hashes)
            _save_job_cache(_job_id(body), "post_image_hashes", it.get("name") or img_ct, hashes)
    return {"items": len(post_items), "uploaded": uploaded, "failed_uploads": failed}


def start_tp1_image_preupload(login: str, body: dict, *, site_type: str = "",
                              slepok: str = "", grid_cookie: str | None = None):
    """Start tp1 image preupload in a background future.

    The executor is owned by the future and is shut down by a done callback.
    Caller should await this future before the first tp1 item.
    """
    from concurrent.futures import ThreadPoolExecutor

    ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="direct-tp1-preupload")
    fut = ex.submit(_prepare_tp1_images, login, body, site_type, slepok, grid_cookie)

    def _shutdown(_fut) -> None:
        ex.shutdown(wait=False)

    fut.add_done_callback(_shutdown)
    return fut


def start_uac_asset_preupload(login: str, body: dict, *, site_type: str = "",
                              ctx: dict | None = None, grid_cookie: str | None = None,
                              is_cancelled: Callable[[], bool] | None = None):
    """Предзагрузка контента/картинок/видео tp6-tp7 в ФОНЕ (конвейер, решение Семёна 2026-07-29).

    Раньше `_prepare_uac_assets` шёл блокирующе внутри `prepare_job`: на наборе из 82 позиций
    (товарка ×9 фидов) заливка ассетов занимала больше 15 минут, и watchdog
    «ни одной кампании за 15 мин» валил джобу ДО первой кампании
    (`WATCHDOG_FIRST_CAMPAIGN_FALSE_POSITIVE_ON_BIG_SET`).

    Теперь ассеты грузятся параллельно созданию: tp1/tp2/tp4/tp5 создаются сразу, а к моменту
    первого tp6/tp7 их `preloaded_content_ids` обычно уже готовы. Ждать future НЕ обязательно —
    `uac_client` при пустом `content_ids` грузит сам (есть img-cache, повтор дёшев).
    Executor принадлежит future и гасится done-callback'ом — как в start_tp1_image_preupload.
    """
    from concurrent.futures import ThreadPoolExecutor

    ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="direct-uac-preupload")
    fut = ex.submit(_prepare_uac_assets, login, body, site_type, ctx or {}, grid_cookie,
                    is_cancelled or (lambda: False))

    def _shutdown(_fut) -> None:
        ex.shutdown(wait=False)

    fut.add_done_callback(_shutdown)
    return fut


def prepare_job(login: str, body: dict, *, ctx: dict | None = None,
                site_type: str = "", city: str = "", href: str = "",
                grid_cookie: str | None = None, job: dict | None = None,
                job_db_progress: Callable[[dict], None] | None = None,
                add_job_err: Callable[[dict | None, str], None] | None = None,
                is_cancelled: Callable[[], bool] = lambda: False,
                prepare_regular_content: bool = True,
                preupload_tp1: bool = True,
                preupload_uac: bool = True) -> dict:
    """Blocking create-set prepare stage. It runs before the first campaign is created.

    For live create jobs the orchestrator passes prepare_regular_content=False and
    preupload_tp1=False so first campaigns can start without waiting for the whole
    account content set or the heavy tp1 image batch.
    """
    login = (login or "").strip()
    body = body or {}
    if not login or not body.get("items"):
        return {"ok": True, "skipped": "empty"}
    got = _PREFETCH_LOCK.acquire(timeout=_LOCK_WAIT_SEC)
    if not got:
        return {"ok": False, "error": f"prepare lock busy >{_LOCK_WAIT_SEC:.0f}s"}
    t0 = time.time()
    try:
        if is_cancelled():
            return {"ok": False, "cancelled": True}
        if job is not None:
            job["step"] = "preparing"
            if callable(job_db_progress):
                job_db_progress(job)
        if not (ctx and site_type and href):
            ctx2, site_type2, city2, href2 = _resolve_site_city(login, body)
            ctx = ctx or ctx2 or {}
            site_type = site_type or site_type2
            city = city or city2
            href = href or href2
        def _prepare_images() -> dict:
            return {
                "tp1": (
                    _prepare_tp1_images(login, body, site_type, body.get("agent") or "", grid_cookie)
                    if preupload_tp1 else {"ok": True, "skipped": "background_tp1"}
                ),
                "uac": (
                    _prepare_uac_assets(login, body, site_type, ctx or {}, grid_cookie, is_cancelled)
                    if preupload_uac else {"ok": True, "skipped": "background_uac"}
                ),
                "post": _prepare_post_images(login, body, grid_cookie, is_cancelled),
            }

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="direct-create-prepare") as ex:
            images_future = ex.submit(_prepare_images)
            prices_report = _prepare_prices(login, href, body)
            if prepare_regular_content:
                content_report = _prepare_ai_content(login, body, site_type, city, is_cancelled)
            else:
                content_report = {
                    "items": len([it for it in (body.get("items") or []) if isinstance(it, dict)]),
                    "generated": 0,
                    "skipped": "deferred_to_item_creation",
                }
            post_content_report = _prepare_post_content(login, body, site_type, href, is_cancelled)
            images_report = images_future.result()
        rep = {
            "ok": True,
            "prices": prices_report,
            "content": content_report,
            "post_content": post_content_report,
            "images_tp1": images_report.get("tp1"),
            "images_uac": images_report.get("uac"),
            "images_post": images_report.get("post"),
        }
        rep["elapsed"] = round(time.time() - t0, 2)
        _save_job_cache(_job_id(body), "prepare_report", login, rep)
        if callable(add_job_err):
            failed_content = int((rep.get("content") or {}).get("failed") or 0)
            failed_post = int((rep.get("post_content") or {}).get("failed") or 0)
            if failed_content or failed_post:
                add_job_err(job, f"prepare: content failed regular={failed_content}, post={failed_post}")
        _log.info("prepare done login=%s за %.1fs: %s", login, time.time() - t0, rep)
        return rep
    except Exception as exc:  # noqa: BLE001
        _log.warning("prepare failed login=%s: %s", login, str(exc)[:200])
        return {"ok": False, "error": str(exc)[:300]}
    finally:
        _PREFETCH_LOCK.release()


def prefetch_job(login: str, body: dict, *,
                 is_cancelled: Callable[[], bool] = lambda: False) -> None:
    """Синхронный прогрев одной queued-джобы. Всё под глобальным локом (не более
    одной джобы одновременно). Любой сбой = warning, джоба не затрагивается."""
    login = (login or "").strip()
    body = body or {}
    if not login or not body.get("items"):
        return
    got = _PREFETCH_LOCK.acquire(timeout=_LOCK_WAIT_SEC)
    if not got:
        _log.info("prefetch skipped (лок занят >%.0fs) login=%s", _LOCK_WAIT_SEC, login)
        return
    t0 = time.time()
    try:
        if is_cancelled():
            _log.info("prefetch skipped (джоба уже не queued) login=%s", login)
            return
        ctx, site_type, city, href = _resolve_site_city(login, body)
        if ctx is None:
            _log.warning("prefetch: нет ctx аккаунта %s — пропуск прогрева", login)
            return
        content_n = _warm_content(login, body, site_type, city, is_cancelled)
        vids_n = vids_total = 0
        if not is_cancelled():
            vids_n, vids_total = _warm_videos(body, is_cancelled)
        prices_ok = False
        if not is_cancelled():
            prices_ok = _warm_prices(login, href)
        cookie_ok = False
        if not is_cancelled():
            cookie_ok = _warm_cookie(login)
        _log.info(
            "prefetch done login=%s за %.1fs: content=%d videos=%d/%d prices=%s cookie=%s",
            login, time.time() - t0, content_n, vids_n, vids_total,
            "ok" if prices_ok else "miss", "ok" if cookie_ok else "miss",
        )
    except Exception as e:  # noqa: BLE001 — прогрев не смеет ронять/трогать джобу
        _log.warning("prefetch failed login=%s: %s", login, str(e)[:200])
    finally:
        _PREFETCH_LOCK.release()


def start_prefetch(login: str, body: dict, *,
                   is_cancelled: Callable[[], bool] = lambda: False) -> None:
    """Запустить прогрев в daemon-потоке (не блокирует постановку джобы).
    body копируется — чтобы последующая мутация body в роутере не гонялась с прогревом."""
    try:
        threading.Thread(
            target=prefetch_job,
            args=(login, dict(body or {})),
            kwargs={"is_cancelled": is_cancelled},
            name="direct-prefetch",
            daemon=True,
        ).start()
    except Exception as e:  # noqa: BLE001
        _log.warning("prefetch thread start failed login=%s: %s", login, str(e)[:160])

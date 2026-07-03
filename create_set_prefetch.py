"""Background prefetch (warm-up) for queued create_set jobs — Phase 1.

Пока джоба создания набора ждёт в очереди (`status='queued'`), фоновый поток
греет ВСЁ тяжёлое В ТОМ ЖЕ ПРОЦЕССЕ, чтобы воркер, забрав джобу, сразу создавал
кампании, а не блокировался на:
  1) генерации M3-контента (тот же `_cached_campaign_content` + общий `_CONTENT_CACHE`);
  2) скачивании видео (`kp.videos_pool_for_ct` — качает mp4 в локальный кэш LXC);
  3) мердже цен по аккаунту (`_account_offer_prices` — кэш ключёван по login);
  4) валидации рабочей куки (`pick_working_cookie` — раннее выявление протухшей сессии).

Flask-free: все зависимости blueprint инъектятся через ``configure(deps)`` по
образцу create_set_* модулей. Любой сбой прогрева = warning в лог и НИКАК не
влияет на саму джобу (прогрев — чистая оптимизация, не источник правды).

Фаза 2 перенесёт весь контур создания в воркер целиком; тогда прогрев в том же
процессе и создание сольются. Пока прогрев валиден именно как in-process warm.
"""
from __future__ import annotations

import logging
import re
import threading
import time
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


def configure(deps: dict) -> None:
    """Bind blueprint dependencies used by prefetch (no Flask imports here)."""
    _DEPS.clear()
    _DEPS.update(deps)


def _dep(name: str) -> Any:
    return _DEPS.get(name)


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

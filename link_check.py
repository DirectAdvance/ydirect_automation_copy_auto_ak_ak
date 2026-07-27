"""HTTP-проверка URL объявления с фолбэком на родительский путь при 404.

Алгоритм (правило Семёна 2026-07-21):
- Голый домен без пути (https://site/) — НЕ проверяем, считаем всегда валидным.
- URL с путём — HEAD-запрос.
- 2xx / 3xx  → рабочий, возвращаем как есть.
- 404        → откусить последний сегмент пути, повторить цепочку.
- 404 на одноcегментном пути (/auto, /catalog) → fallback на корень домена.
- Таймаут / 5xx / сетевая ошибка → НЕ уходим в фолбэк, возвращаем ИСХОДНЫЙ URL.
- Кэш module-level: TTL 60 мин, максимум 2000 ключей.

Чистый модуль: только stdlib, без DI, без импорта проектных модулей.
"""
from __future__ import annotations

import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor
from urllib.parse import urlsplit, urlunsplit

# ── Кэш ────────────────────────────────────────────────────────────────────────────────
_URL_CHECK_CACHE: dict[str, tuple[str, float]] = {}   # исходный_url → (resolved_url, ts)
_URL_CHECK_CACHE_LOCK = threading.Lock()
_URL_CHECK_CACHE_TTL = 60 * 60    # 60 минут — перекрывает длительность одного job-прогона
_URL_CHECK_CACHE_MAX = 2000       # мягкое ограничение; при переполнении чистим 10% старейших

# ── Circuit breaker per host ──────────────────────────────────────────────────────────────
# Если N подряд запросов к одному хосту завершились таймаутом — дальнейшие URL того же
# хоста сразу возвращаются как есть (fail-open), без ожидания.
# Через _CB_COOLDOWN секунд после размыкания — одна пробная попытка (half-open):
# успех → счётчик сбрасывается, цепь замыкается; таймаут → цепь снова разомкнута.
_CB_FAILURES: dict[str, int] = {}    # hostname → кол-во подряд идущих таймаутов
_CB_OPEN_TS: dict[str, float] = {}   # hostname → time.time() когда цепь разомкнулась
_CB_LOCK = threading.Lock()
_CB_THRESHOLD = 3                     # порог подряд идущих таймаутов для размыкания цепи
_CB_COOLDOWN = 120.0                  # секунды кулдауна до пробной попытки (half-open)


# ── Вспомогательные функции ──────────────────────────────────────────────────────────

def strip_quiz_url(url: str) -> str:
    """Project rule: generated Direct ads/buttons never link to /quiz; use site root instead."""
    raw = (url or "").strip()
    if not raw:
        return raw
    try:
        p = urlsplit(raw)
        if p.scheme and p.netloc and re.match(r"^/quiz(?:/|$)", p.path or "", re.I):
            return urlunsplit((p.scheme, p.netloc, "", "", ""))
    except Exception:  # noqa: BLE001
        pass
    return raw

def _has_meaningful_path(url: str) -> bool:
    """True если URL содержит непустой путь (не просто схема + домен).

    Примеры:
      https://site.com          → False (голый домен)
      https://site.com/         → False (корень — приравниваем к голому домену)
      https://site.com/auto     → True
      https://site.com/auto/lada/granta → True
    """
    m = re.match(r"https?://[^/?#]+(/.+)?$", url or "")
    if not m:
        return False
    path = (m.group(1) or "").rstrip("/")
    return bool(path)


def _parent_path(url: str) -> str | None:
    """Откусить последний сегмент пути, вернуть родительский URL.

    Возвращает None, если откусывать некуда (URL уже без path).

    Примеры:
      https://site/auto/renault/sandero/ii/hatchback-5d → https://site/auto/renault/sandero/ii
      https://site/auto/renault/sandero/ii              → https://site/auto/renault/sandero
      https://site/auto/renault                         → https://site/auto
      https://site/auto                                 → https://site
    """
    u = url.rstrip("/")
    m = re.match(r"(https?://[^/]+)(/[^?#]*)?$", u)
    if not m:
        return None
    origin = m.group(1)
    path = (m.group(2) or "").rstrip("/")
    if not path:
        return None
    segments = [s for s in path.split("/") if s]
    if len(segments) <= 1:
        # Остался только один сегмент (/auto, /catalog): при 404 уводим на корень.
        return origin
    parent_path = "/" + "/".join(segments[:-1])
    return origin + parent_path


def _http_status(url: str, timeout: float) -> int | None:
    """HEAD-запрос к URL. Возвращает HTTP-статус или None при ошибке/таймауте."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", "Mozilla/5.0 (compatible; NeyroDirectLinkCheck/1.0)")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:   # noqa: BLE001 — таймаут/DNS/сеть → None
        return None


# ── Публичный API ──────────────────────────────────────────────────────────────────────

def resolve_or_fallback_url(url: str, *, timeout: float = 3.0) -> str:
    """Вернуть рабочий URL — исходный или ближайший рабочий предок при 404.

    Используется при конструировании href объявления: если URL ведёт на 404
    (модель/поколение убрана из каталога), автоматически поднимаемся по пути
    до первого не-404 ответа.

    Args:
        url:     Абсолютный HTTP(S) URL объявления.
        timeout: Таймаут одного HEAD-запроса в секундах (по умолчанию 3.0).

    Returns:
        url без изменений, если:
          — URL пустой / без пути (голый домен);
          — запрос вернул 2xx / 3xx;
          — таймаут / ошибка сети / 5xx (не подменяем при сбое сайта);
          — цепочка фолбэков исчерпана.
        Иначе: ближайший предок, вернувший не-404.

    Thread-safe. Кэш TTL 60 мин (перекрывает длительность прогона создания).
    """
    url = strip_quiz_url(url)
    if not url:
        return url
    if not _has_meaningful_path(url):
        return url   # голый домен — не проверяем

    # Кэш-hit
    with _URL_CHECK_CACHE_LOCK:
        hit = _URL_CHECK_CACHE.get(url)
        if hit is not None and (time.time() - hit[1]) < _URL_CHECK_CACHE_TTL:
            return hit[0]

    resolved = _do_resolve(url, timeout)

    # Кэш-запись с ограничением размера
    with _URL_CHECK_CACHE_LOCK:
        if len(_URL_CHECK_CACHE) >= _URL_CHECK_CACHE_MAX:
            # Удалить 10% старейших записей
            n_drop = max(1, _URL_CHECK_CACHE_MAX // 10)
            oldest = sorted(_URL_CHECK_CACHE.items(), key=lambda kv: kv[1][1])[:n_drop]
            for k, _ in oldest:
                del _URL_CHECK_CACHE[k]
        _URL_CHECK_CACHE[url] = (resolved, time.time())

    return resolved


def resolve_urls_batch(urls: list[str], timeout: float = 3.0) -> dict[str, str]:
    """Параллельная HEAD-проверка списка URL с фолбэком (до 6 потоков).

    Cache-hit URL возвращаются мгновенно из module-level кэша; cache-miss URL
    резолвятся параллельно через ThreadPoolExecutor и затем записываются в кэш.
    Возвращает {исходный_url: resolved_url}.

    Предназначено для пред-прогрева кэша перед циклом формирования групп — тогда
    каждый последующий вызов resolve_or_fallback_url() внутри цикла будет cache-hit.

    TODO (следующий проход): в builder-функциях create_set_text_builders._build_text_from_pack
    и create_set_tp1_builders._build_tp1_from_pack / _tp1_pack_groups — вынести вычисление
    model_href в отдельный pre-pass ДО основного цикла групп, передать список в эту функцию,
    и тогда основной цикл будет бить только по кэшу. Сейчас call-sites вызывают
    resolve_or_fallback_url() по одному в цикле (N последовательных HEAD при cache-miss).
    """
    # Фильтруем пустые и голые домены (без пути) — они не проверяются, как в resolve_or_fallback_url
    unique = list(dict.fromkeys(u for u in (urls or []) if u and _has_meaningful_path(u)))
    if not unique:
        return {}

    now = time.time()
    result: dict[str, str] = {}
    uncached: list[str] = []

    with _URL_CHECK_CACHE_LOCK:
        for u in unique:
            hit = _URL_CHECK_CACHE.get(u)
            if hit is not None and (now - hit[1]) < _URL_CHECK_CACHE_TTL:
                result[u] = hit[0]
            else:
                uncached.append(u)

    if uncached:
        # Circuit-breaker pre-filter: хосты с открытой цепью — сразу fail-open, без обращения к сети.
        # Если кулдаун истёк — сбрасываем счётчик и пускаем URL на пробную попытку (half-open).
        _cb_pass: list[str] = []
        with _CB_LOCK:
            _now = time.time()
            for u in uncached:
                _h = urlsplit(u).netloc
                if _CB_FAILURES.get(_h, 0) >= _CB_THRESHOLD:
                    ts = _CB_OPEN_TS.get(_h, 0.0)
                    if _now - ts < _CB_COOLDOWN:
                        result[u] = u   # fail-open: возвращаем исходный без сетевого вызова
                    else:
                        _CB_FAILURES[_h] = 0   # кулдаун истёк → пробная попытка
                        _cb_pass.append(u)
                else:
                    _cb_pass.append(u)
        uncached = _cb_pass

    if uncached:
        n_workers = min(len(uncached), 6)
        with _ThreadPoolExecutor(max_workers=n_workers) as ex:
            fut_to_url = {ex.submit(_do_resolve, u, timeout): u for u in uncached}
            for fut, u in fut_to_url.items():
                resolved = fut.result()
                result[u] = resolved
                with _URL_CHECK_CACHE_LOCK:
                    if len(_URL_CHECK_CACHE) >= _URL_CHECK_CACHE_MAX:
                        n_drop = max(1, _URL_CHECK_CACHE_MAX // 10)
                        oldest = sorted(_URL_CHECK_CACHE.items(), key=lambda kv: kv[1][1])[:n_drop]
                        for k, _ in oldest:
                            del _URL_CHECK_CACHE[k]
                    _URL_CHECK_CACHE[u] = (resolved, time.time())

    return result


def _do_resolve(url: str, timeout: float) -> str:
    """Основная логика цепочки фолбэков (без кэша). Вызывается только на cache miss."""
    _hostname = urlsplit(url).netloc
    # Circuit breaker: если хост уже набрал N подряд таймаутов — не ждём, сразу fail-open.
    # Через _CB_COOLDOWN секунд — одна пробная попытка (half-open): сбрасываем счётчик,
    # чтобы HTTP-вызов прошёл; если он снова упадёт — счётчик вырастет и цепь снова откроется.
    with _CB_LOCK:
        if _CB_FAILURES.get(_hostname, 0) >= _CB_THRESHOLD:
            ts = _CB_OPEN_TS.get(_hostname, 0.0)
            if time.time() - ts < _CB_COOLDOWN:
                print(
                    f"[link-check] circuit-breaker: {_hostname!r} open,"
                    f" skipping {url!r}",
                    flush=True,
                )
                return url
            # Кулдаун истёк: сбрасываем счётчик для пробной попытки
            _CB_FAILURES[_hostname] = 0
            print(
                f"[link-check] circuit-breaker: {_hostname!r} cooldown elapsed, probe attempt",
                flush=True,
            )

    candidate = url
    while candidate is not None:
        status = _http_status(candidate, timeout)
        if status is None:
            # Таймаут или сетевая ошибка — фиксируем отказ хоста, возвращаем ИСХОДНЫЙ
            with _CB_LOCK:
                _CB_FAILURES[_hostname] = _CB_FAILURES.get(_hostname, 0) + 1
                if _CB_FAILURES[_hostname] == _CB_THRESHOLD:
                    _CB_OPEN_TS[_hostname] = time.time()
                    print(
                        f"[link-check] circuit-breaker opened for {_hostname!r}"
                        f" after {_CB_THRESHOLD} consecutive timeouts",
                        flush=True,
                    )
            print(f"[link-check] timeout/net-error for {candidate!r}, keeping original {url!r}", flush=True)
            return url
        # Успешный ответ — сбрасываем счётчик отказов хоста
        with _CB_LOCK:
            if _CB_FAILURES.get(_hostname, 0) > 0:
                _CB_FAILURES[_hostname] = 0
        if status >= 500:
            # 5xx — сайт может быть временно недоступен, не подменяем
            print(f"[link-check] {status} for {candidate!r}, keeping original {url!r}", flush=True)
            return url
        if status != 404:
            # 2xx или 3xx — рабочий URL
            if candidate != url:
                print(f"[link-check] 404 fallback: {url!r} → {candidate!r} (status={status})", flush=True)
            return candidate
        # 404 → пробуем родителя
        parent = _parent_path(candidate)
        candidate = parent
    # Цепочка исчерпана (добрались до однозначного пути) — возвращаем исходный
    print(f"[link-check] fallback chain exhausted for {url!r}, keeping original", flush=True)
    return url

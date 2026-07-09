"""LLM-провайдеры нейродиректолога — вынесено из blueprint.py.

M3 (mlx локально, порт 8086) + OpenRouter (DeepSeek V4 Flash) с ДВУСТОРОННИМ фолбэком
(_llm_pair_for). Чистый I/O-лист: модуль НЕ импортирует blueprint. Единственная внешняя
зависимость — heartbeat очереди (`_touch_running_jobs_heartbeat`, зависит от _CREATE_JOBS)
инъектится из blueprint через configure(); по умолчанию — безопасная заглушка.

blueprint импортирует отсюда символы обратно (deps-словари/внутренние вызовы) и зовёт
configure({"_touch_running_jobs_heartbeat": ...}) после определения heartbeat.
"""
from __future__ import annotations

import os
import threading

# ── Прокси для OpenRouter (mihomo на LXC101, DE/NL/FR ноды, поднят home_vpn 2026-07-07) ──
# Прямой IP LXC101 заблокирован Cloudflare WAF (403). Прокси только для OR-запросов.
_OR_PROXIES = {"https": "http://127.0.0.1:7891", "http": "http://127.0.0.1:7891"}


# ── DI: heartbeat очереди (устанавливается configure из blueprint) ────────────────
def _touch_running_jobs_heartbeat() -> None:
    """Заглушка. blueprint инъектит реальную (бампает _heartbeat running-джоб) через configure."""


def configure(deps: dict) -> None:
    """Инъекция зависимостей из blueprint (heartbeat очереди)."""
    globals().update(deps)


# ── Константы M3 / OpenRouter ─────────────────────────────────────────────────────
_M3_LLM_URL = os.environ.get("M3_LLM_URL", "http://127.0.0.1:8086").rstrip("/")
_M3_LLM_TIMEOUT = float(os.environ.get("M3_LLM_TIMEOUT", "480"))   # 72B медленнее 14B (под nginx 500с)
_M3_LLM_URLS_14B: list = [
    s.strip().rstrip("/") for s in os.environ.get("M3_LLM_URLS_14B", "").split(",") if s.strip()
] or ["http://127.0.0.1:8086"]
_M3_LLM_URL_72B: str = os.environ.get("M3_LLM_URL_72B", "http://127.0.0.1:8086").rstrip("/")
# Таймауты откалиброваны под ОДНУ 72B (~6.5 ток/с, 03.07): 3 сегмента бьют в один mlx и
# СЕРИАЛИЗУЮТСЯ — таймаут третьего тикает включая очередь двух чужих генераций (~2×90с).
_M3_LLM_TIMEOUT_14B = float(os.environ.get("M3_LLM_TIMEOUT_14B", "360"))
# repair 35с был под 14B: 72B генерит 240-440 токенов за 40-70с — 35с не хватало НИКОГДА
# (до 12 мёртвых вызовов на пункт, ревью 03.07).
_M3_LLM_REPAIR_TIMEOUT = float(os.environ.get("M3_LLM_REPAIR_TIMEOUT", "120"))

# ── СТРИМ-таймауты: liveness по потоку токенов (задача E, 2026-07-09) ────────────────
# Раньше был ОДИН wall-clock read-timeout: он тикал ПОКА модель генерит и рвал РАБОЧУЮ,
# но долгую генерацию (72B ~6.5 ток/с, 3 сегмента сериализуются в один mlx) как «зависание».
# Теперь стрим (stream=true) + IDLE-таймаут: считаем LLM мёртвой ТОЛЬКО если между токенами
# тишина > idle_gap ИЛИ соединение оборвалось. Токены идут → генерация длится сколько нужно.
_M3_LLM_CONNECT_TIMEOUT = float(os.environ.get("M3_LLM_CONNECT_TIMEOUT", "10"))  # установка соединения
# Макс. допустимая ТИШИНА между чанками (не общий wall-clock). Параметр `timeout` вызовов
# переопределяет именно это (idle-gap), сохраняя сигнатуру.
_M3_LLM_IDLE_TIMEOUT = float(os.environ.get("M3_LLM_IDLE_TIMEOUT", "45"))
# Предохранитель от БЕСКОНЕЧНОГО потока: очень большой, НЕ должен рвать нормальную генерацию.
_M3_LLM_HARD_CAP = float(os.environ.get("M3_LLM_HARD_CAP", "1200"))

# ── IDLE-таймаут КОНТЕНТ-вызовов (задача «убрать налог на висящий M3», 2026-07-09) ──────
# Со стримингом (E) idle = пауза МЕЖДУ токенами. Живой M3 стримит ~6.5 ток/с (гэп <1с), поэтому
# idle=30с НЕ бросит рабочий M3, только реально ВИСЯЩИЙ (0 токенов). Раньше content-вызовы висели
# 90с (14B fast) / 120с (72B repair) / 360с (14B) — до 4 обращений на РК = главный тормоз при
# мёртвом completion (health GET жив, генерация мертва — живой баг 09.07). Env-конфиг, не хардкод.
_M3_CONTENT_IDLE_TIMEOUT = float(os.environ.get("M3_CONTENT_IDLE_TIMEOUT", "30"))

# ── Circuit-breaker M3 НА НАБОР (задача «убрать налог на висящий M3», 2026-07-09) ───────
# completion-preflight: 1-токенный тест ГЕНЕРАЦИИ (не health GET) — короткий idle-таймаут.
_M3_COMPLETION_PREFLIGHT_TIMEOUT = float(os.environ.get("M3_COMPLETION_PREFLIGHT_TIMEOUT", "9"))
_M3_COMPLETION_PREFLIGHT_RETRIES = int(os.environ.get("M3_COMPLETION_PREFLIGHT_RETRIES", "2"))
# Сколько РЕАЛЬНЫХ зависаний M3 по ходу набора взводят breaker (весь остаток → OpenRouter).
_M3_BREAKER_TIMEOUT_THRESHOLD = int(os.environ.get("M3_BREAKER_TIMEOUT_THRESHOLD", "2"))


# ── health-probe M3 ───────────────────────────────────────────────────────────────
def _m3_llm_probe(timeout: float = 3.0) -> bool:
    """Быстрый health LLM: GET /v1/models на первый 14B-эндпоинт (тот же, что _m3_complete).
    True = mlx-сервер отвечает. ОТДЕЛЬНО от контент-индекса: sshfs-мост может жить, а LLM лежать
    (инцидент 2026-07-02: туннель 22022 пережил reboot Mac, а туннели 18082-18086 — нет, и бейдж
    молча зеленел, хотя генерация РК падала)."""
    import requests as _rqs
    try:
        r = _rqs.get(f"{_M3_LLM_URLS_14B[0]}/v1/models", timeout=timeout)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


# ── маркеры служебных ошибок draft-модели + чистка ────────────────────────────────
_M3_LEAK_MARKERS = (
    "M3 недоступна", "Connection aborted", "RemoteDisconnected", "Remote end closed",
    "ConnectionError", "Max retries exceeded", "Traceback (most recent call",
    "HTTPConnectionPool", "Failed to establish a new connection",
)


def _strip_error_leak(text: str) -> str:
    """Срезает текст по первому маркеру служебной ошибки (обрыв драфт-модели). → чистый префикс."""
    if not text:
        return text or ""
    low = text.lower()
    cut = len(text)
    for mk in _M3_LEAK_MARKERS:
        i = low.find(mk.lower())
        if i != -1 and i < cut:
            cut = i
    return text[:cut].rstrip(" \n\t·-—–:(") if cut < len(text) else text


def _has_error_leak(text: str) -> bool:
    low = (text or "").lower()
    return any(mk.lower() in low for mk in _M3_LEAK_MARKERS)


# ── preflight M3: 3 попытки health-check перед генерацией ────────────────────────────
def _m3_preflight_ok(retries: int = 3, pause: float = 3.0) -> bool:
    """True = M3 жив (хотя бы одна из retries попыток GET /v1/models прошла за 3с).
    False = все упали → caller должен сразу уходить в OpenRouter, не ждать полный read-timeout.
    Добавлено 2026-07-07: интермиттентный лаг туннеля 8086 давал серию 90-480с read-timeout'ов
    вместо быстрого фолбэка, что будило watchdog."""
    import time as _time
    for attempt in range(retries):
        if _m3_llm_probe(timeout=3.0):
            return True
        if attempt < retries - 1:
            _time.sleep(pause)
    return False


# ── вызовы M3 / OpenRouter / fan-out / двусторонний фолбэк ─────────────────────────
# ── общий сборщик SSE-стрима (OpenAI-совместимый чат: M3 mlx и OpenRouter) ──────────
def _consume_sse_stream(resp, hard_cap: float, started: float) -> tuple[str, bool]:
    """Читает SSE-поток /v1/chat/completions, аккумулирует delta.content по чанкам.
    HEARTBEAT НА КАЖДОМ ЧАНКЕ — watchdog джобы видит живость по РЕАЛЬНОМУ потоку токенов,
    а не думает что джоба зависла во время долгой (но рабочей) генерации.
    → (собранный_текст, hit_cap). Исключения requests (idle read-timeout / обрыв) НЕ ловит —
    их ловит вызывающий и трактует как «зависание LLM». hard_cap — предохранитель от
    бесконечного потока (не рвёт нормальную генерацию)."""
    import json as _json
    import time as _time
    parts: list = []
    hit_cap = False
    # decode_unicode=False → байты: декодируем UTF-8 САМИ. text/event-stream без charset requests
    # трактует как latin-1 → кириллица бьётся в мойибаке (OpenRouter, живой баг 2026-07-09).
    for raw_b in resp.iter_lines(decode_unicode=False):
        _touch_running_jobs_heartbeat()   # реальный токен = живость джобы (liveness по потоку)
        if hard_cap and (_time.time() - started) > hard_cap:
            hit_cap = True
            break
        if not raw_b:
            continue
        line = (raw_b.decode("utf-8", "replace") if isinstance(raw_b, bytes) else raw_b).strip()
        if not line or line.startswith(":"):   # SSE-комментарий (OpenRouter keep-alive ": PROCESSING")
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            obj = _json.loads(data)
            delta = (obj.get("choices") or [{}])[0].get("delta") or {}
            piece = delta.get("content")
            if piece:
                parts.append(piece)
        except (ValueError, KeyError, IndexError, TypeError):
            continue
    return "".join(parts), hit_cap


def _m3_complete(messages: list, max_tokens: int = 400, temperature: float = 0.8,
                 top_p: float | None = None, repetition_penalty: float | None = None,
                 tries: int = 3, backoff: float = 5.0, timeout: float | None = None) -> tuple[str | None, str | None]:
    """Один вызов M3 (mlx /v1/chat/completions). → (text, error). model не шлём.
    top_p/repetition_penalty — анти-зацикливание (14B на длинном промпте иначе уходит в повтор).
    tries/backoff — ретраи на ТРАНСПОРТНОМ обрыве и «утечке» служебной ошибки в текст: mlx на M3
    под нехваткой RAM (speculative decoding) падает в процессе генерации (RemoteDisconnected),
    watchdog поднимает его за ~5с — поэтому между попытками ждём `backoff` секунд.
    timeout — переопределение IDLE-таймаута (тишина между токенами; для сид-прохода: быстрый
    фейл-фаст). СТРИМИНГ: idle-таймаут тикает МЕЖДУ чанками, а не весь wall-clock — рабочая, но
    долгая генерация НЕ обрывается «как зависание»; зависание = read-timeout/обрыв."""
    import time as _time
    import requests as _rqs
    idle = float(timeout) if timeout is not None else _M3_LLM_IDLE_TIMEOUT
    payload = {"messages": messages, "max_tokens": int(max_tokens),
               "temperature": float(temperature), "stream": True}
    if top_p is not None:
        payload["top_p"] = float(top_p)
    if repetition_penalty is not None:
        payload["repetition_penalty"] = float(repetition_penalty)
    last_err = "M3 недоступна"
    n = max(1, tries)
    for attempt in range(n):
        last = (attempt == n - 1)
        _touch_running_jobs_heartbeat()   # LLM-вызов = прогресс (анти-watchdog при медленном M3)
        started = _time.time()
        try:
            r = _rqs.post(f"{_M3_LLM_URL}/v1/chat/completions", json=payload,
                          stream=True, timeout=(_M3_LLM_CONNECT_TIMEOUT, idle))
        except Exception as e:  # noqa: BLE001
            last_err = f"M3 недоступна: {str(e)[:160]}"
            if not last:
                _time.sleep(backoff)   # обрыв (RemoteDisconnected) → ждём перезапуск mlx watchdog'ом
            continue
        if r.status_code != 200:
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            r.close()
            if not last:
                _time.sleep(backoff)
            continue
        try:
            content, hit_cap = _consume_sse_stream(r, _M3_LLM_HARD_CAP, started)
        except Exception as e:  # noqa: BLE001
            # тишина > idle_gap ИЛИ обрыв соединения = ЗАВИСАНИЕ (и только это) → ретрай/фолбэк
            last_err = f"M3 зависла (нет токенов > {idle:.0f}с/обрыв): {str(e)[:160]}"
            if not last:
                _time.sleep(backoff)
            continue
        finally:
            r.close()
        if hit_cap:
            print(f"[llm-m3] hard-cap {_M3_LLM_HARD_CAP:.0f}с — стрим обрезан предохранителем", flush=True)
        if not (content or "").strip():
            last_err = "пустой ответ модели"
            if not last:
                _time.sleep(backoff)
            continue
        # Драфт-модель вклеила служебную ошибку в текст → чистим. Если после чистки осталась
        # осмысленная часть — отдаём её; иначе это мусор, повторяем генерацию.
        if _has_error_leak(content):
            cleaned = _strip_error_leak(content)
            if len(cleaned.strip()) >= 12 and last:
                return cleaned, None   # последняя попытка — спасаем осмысленный префикс
            last_err = "M3 вернула обрывок (сбой драфт-модели)"
            if not last:
                _time.sleep(backoff)
            continue
        return content, None
    return None, last_err


def _m3_complete_url(url: str, messages: list, max_tokens: int = 400, temperature: float = 0.8,
                     top_p: float | None = None, repetition_penalty: float | None = None,
                     tries: int = 2, backoff: float = 5.0, timeout: float | None = None) -> tuple:
    """Как _m3_complete, но с явным URL — для fan-out на разные порты 14B/72B.
    СТРИМИНГ + IDLE-таймаут (см. _m3_complete): рабочая долгая генерация не рвётся, зависание =
    read-timeout между токенами / обрыв. `timeout` = переопределение idle-gap."""
    import time as _time
    import requests as _rqs
    idle = float(timeout) if timeout is not None else _M3_LLM_IDLE_TIMEOUT
    payload = {"messages": messages, "max_tokens": int(max_tokens),
               "temperature": float(temperature), "stream": True}
    if top_p is not None:
        payload["top_p"] = float(top_p)
    if repetition_penalty is not None:
        payload["repetition_penalty"] = float(repetition_penalty)
    last_err = f"M3 ({url}) недоступна"
    n = max(1, tries)
    for attempt in range(n):
        last = (attempt == n - 1)
        _touch_running_jobs_heartbeat()   # LLM-вызов = прогресс (анти-watchdog при медленном M3)
        started = _time.time()
        try:
            r = _rqs.post(f"{url}/v1/chat/completions", json=payload,
                          stream=True, timeout=(_M3_LLM_CONNECT_TIMEOUT, idle))
        except Exception as e:  # noqa: BLE001
            last_err = f"M3 ({url}) недоступна: {str(e)[:120]}"
            if not last:
                _time.sleep(backoff)
            continue
        if r.status_code != 200:
            last_err = f"HTTP {r.status_code}: {r.text[:160]}"
            r.close()
            if not last:
                _time.sleep(backoff)
            continue
        try:
            content, hit_cap = _consume_sse_stream(r, _M3_LLM_HARD_CAP, started)
        except Exception as e:  # noqa: BLE001
            last_err = f"M3 ({url}) зависла (нет токенов > {idle:.0f}с/обрыв): {str(e)[:120]}"
            if not last:
                _time.sleep(backoff)
            continue
        finally:
            r.close()
        if hit_cap:
            print(f"[llm-m3] {url}: hard-cap {_M3_LLM_HARD_CAP:.0f}с — стрим обрезан", flush=True)
        if not (content or "").strip():
            last_err = "пустой ответ модели"
            if not last:
                _time.sleep(backoff)
            continue
        if _has_error_leak(content):
            cleaned = _strip_error_leak(content)
            if len(cleaned.strip()) >= 12 and last:
                return cleaned, None
            last_err = "M3 вернула обрывок (сбой драфт-модели)"
            if not last:
                _time.sleep(backoff)
            continue
        return content, None
    return None, last_err


def _m3_complete_parallel(tasks: list) -> list:
    """Параллельный вызов нескольких mlx-инстансов через ThreadPoolExecutor.
    tasks: [(url, messages, kwargs_dict), ...] — каждый task идёт на свой порт.
    Возвращает list[(text|None, error|None)] в том же порядке что tasks."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _call(idx_task):
        idx, (url, msgs, kw) = idx_task
        return idx, _m3_complete_url(url, msgs, **kw)

    results = [None] * len(tasks)
    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        futs = [ex.submit(_call, (i, t)) for i, t in enumerate(tasks)]
        for f in as_completed(futs):
            idx, res = f.result()
            results[idx] = res
    return results


# ── OpenRouter — платная генерация (попап создания) + двусторонний фолбэк (Семён 03.07) ──
# A/B 03.07 (пункт Павлова, боевой пайплайн): качество DeepSeek V4 Flash ≈ M3-14B, сырьё чище
# (вдвое меньше брака в фильтрах), цена ≈ $0.2 за набор ~25 кампаний.
# «Падение одного — переключение на другого»: композиция фолбэка в _llm_pair_for, сами
# _or_/_m3_-функции ЧИСТЫЕ (без взаимных вызовов — иначе рекурсия).
_OPENROUTER_LLM_MODEL = os.environ.get("OPENROUTER_LLM_MODEL", "deepseek/deepseek-v4-flash")
_OPENROUTER_KEY_CACHE: dict = {}
# СТРИМ-таймауты OpenRouter (см. M3: connect / idle-между-токенами / предохранитель), задача E.
_OPENROUTER_CONNECT_TIMEOUT = float(os.environ.get("OPENROUTER_CONNECT_TIMEOUT", "15"))
_OPENROUTER_IDLE_TIMEOUT = float(os.environ.get("OPENROUTER_IDLE_TIMEOUT", "45"))
_OPENROUTER_HARD_CAP = float(os.environ.get("OPENROUTER_HARD_CAP", "900"))


def _openrouter_api_key() -> str:
    """OPENROUTER_API_KEY через .secret/loader.load_openrouter (кэш процесса).
    ⚠ НЕ load_secrets() — легаси-сводный dict этот ключ не отдаёт (грабли 03.07)."""
    if "key" in _OPENROUTER_KEY_CACHE:
        return _OPENROUTER_KEY_CACHE["key"]
    key = ""
    try:
        import pathlib
        import sys as _sys
        for parent in pathlib.Path(__file__).resolve().parents:
            cand = parent / ".secret" / "loader.py"
            if cand.exists():
                if str(cand.parent) not in _sys.path:
                    _sys.path.insert(0, str(cand.parent))
                from loader import load_openrouter  # type: ignore
                key = str((load_openrouter() or {}).get("api_key") or "")
                break
    except Exception as _ke:  # noqa: BLE001
        # НЕ кэшируем пустоту: транзиентный сбой чтения .secret не должен отключать
        # платный контур до рестарта (ревью 03.07). Ошибку показываем, не глотаем.
        print(f"[llm-or] ключ не прочитан (повторим при следующем вызове): {str(_ke)[:120]}", flush=True)
        return ""
    if key:
        _OPENROUTER_KEY_CACHE["key"] = key
    return key


def _openrouter_probe(timeout: float = 5.0) -> bool:
    """Жив ли платный контур: ключ есть И авторизованный эндпоинт отвечает.
    Используется M3-гейтом: M3 лёг, OpenRouter жив → НЕ паузим, фолбэк переключит сам."""
    key = _openrouter_api_key()
    if not key:
        return False
    import requests as _rqs
    try:
        r = _rqs.get("https://openrouter.ai/api/v1/key",
                     headers={"Authorization": f"Bearer {key}"}, timeout=timeout,
                     proxies=_OR_PROXIES)
        if r.status_code != 200:
            return False
        # 200 = ключ валиден, но НЕ платёжеспособность: при исчерпанном лимите completions
        # отвечает 402 → гейт пропустил бы набор в массовый брак (ревью 03.07). Проверяем остаток.
        d = (r.json() or {}).get("data") or {}
        lim, usage = d.get("limit"), d.get("usage")
        if lim is not None and usage is not None and float(usage) >= float(lim):
            print("[llm-or] лимит OpenRouter исчерпан (usage>=limit) — контур считаем недоступным",
                  flush=True)
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


def check_content_pipeline_health(
    or_retries: int = 2,
    or_pause: float = 2.0,
    or_timeout: float = 5.0,
) -> dict:
    """Проверяет оба провайдера контент-пайплайна ПАРАЛЛЕЛЬНО.

    M3:  _m3_preflight_ok(retries=3, pause=3.0) — 3 попытки GET /v1/models, 3 с между ними.
          Итого до ~15 с если M3 мёртв; хотя бы одна успешная → жив.
    OR:  _openrouter_probe × or_retries — ключ + остаток лимита; or_retries попыток,
          or_pause секунд между ними.

    Anti-false-positive:
    - Оба запускаются параллельно → суммарное ожидание ≤ max(~15 с, ~12 с) ≈ 15 с.
    - Транзиентный 1-блип сети: M3 — пройдёт (достаточно 1 из 3); OR — хватит 1 из 2.
    - Блокируем ТОЛЬКО при уверенно-мёртвых обоих (все попытки провалились).
    - Если ХОТЯ БЫ ОДИН жив → any_alive=True → caller продолжает (фолбэк сработает сам).

    Returns dict: m3_alive, or_alive, any_alive, message.
    """
    import time as _time
    from concurrent.futures import ThreadPoolExecutor

    def _check_m3() -> bool:
        return _m3_preflight_ok(retries=3, pause=3.0)

    def _check_or() -> bool:
        for attempt in range(or_retries):
            if _openrouter_probe(timeout=or_timeout):
                return True
            if attempt < or_retries - 1:
                _time.sleep(or_pause)
        return False

    with ThreadPoolExecutor(max_workers=2) as _ex:
        _fm3 = _ex.submit(_check_m3)
        _for = _ex.submit(_check_or)
        m3_alive = _fm3.result()
        or_alive = _for.result()

    if m3_alive and or_alive:
        message = "M3 жив, OpenRouter жив"
    elif m3_alive:
        message = "M3 жив (OpenRouter недоступен — фолбэк не нужен)"
    elif or_alive:
        message = "OpenRouter жив (M3 недоступен — фолбэк включится автоматически)"
    else:
        message = (
            "M3 не отвечает (3/3 health-check провалились) "
            "и OpenRouter не отвечает (2/2 probe провалились)"
        )
    return {"m3_alive": m3_alive, "or_alive": or_alive,
            "any_alive": m3_alive or or_alive, "message": message}


def _or_complete_url(url: str, messages: list, max_tokens: int = 400, temperature: float = 0.8,
                     top_p: float | None = None, repetition_penalty: float | None = None,
                     tries: int = 2, backoff: float = 2.0, timeout: float | None = None) -> tuple:
    """OpenRouter-двойник _m3_complete_url (DI-совместимая сигнатура; url M3 игнорируется).
    ЧИСТЫЙ: (None, err) при сбое — фолбэк на M3 добавляет _llm_pair_for, не эта функция."""
    import time as _time
    import requests as _rqs
    key = _openrouter_api_key()
    if not key:
        return None, "OPENROUTER_API_KEY пуст (.secret/.env)"
    idle = float(timeout) if timeout is not None else _OPENROUTER_IDLE_TIMEOUT
    payload = {"model": _OPENROUTER_LLM_MODEL, "messages": messages,
               "max_tokens": int(max_tokens), "temperature": float(temperature), "stream": True}
    if top_p is not None:
        payload["top_p"] = float(top_p)
    last_err = "OpenRouter: нет ответа"
    n = max(1, tries)
    for attempt in range(n):
        last = (attempt == n - 1)
        _touch_running_jobs_heartbeat()   # LLM-вызов = прогресс (анти-watchdog)
        started = _time.time()
        try:
            r = _rqs.post("https://openrouter.ai/api/v1/chat/completions", json=payload,
                          headers={"Authorization": f"Bearer {key}"}, stream=True,
                          timeout=(_OPENROUTER_CONNECT_TIMEOUT, idle), proxies=_OR_PROXIES)
        except Exception as e:  # noqa: BLE001
            last_err = f"OpenRouter недоступен: {str(e)[:120]}"
            if not last:
                _time.sleep(backoff)
            continue
        if r.status_code != 200:
            last_err = f"OpenRouter HTTP {r.status_code}: {r.text[:160]}"
            r.close()
            if not last:
                _time.sleep(backoff)
            continue
        try:
            content, hit_cap = _consume_sse_stream(r, _OPENROUTER_HARD_CAP, started)
        except Exception as e:  # noqa: BLE001
            last_err = f"OpenRouter завис (нет токенов > {idle:.0f}с/обрыв): {str(e)[:120]}"
            if not last:
                _time.sleep(backoff)
            continue
        finally:
            r.close()
        if hit_cap:
            print(f"[llm-or] hard-cap {_OPENROUTER_HARD_CAP:.0f}с — стрим обрезан", flush=True)
        if (content or "").strip():
            return content, None
        last_err = "OpenRouter: пустой content"
        if not last:
            _time.sleep(backoff)
    return None, last_err


# ── Circuit-breaker M3 НА НАБОР (thread-safe: prefetch ThreadPoolExecutor 3w, каналы C1) ──
class _M3CircuitBreaker:
    """Per-run предохранитель: пока ВЗВЕДЁН — M3 не дёргаем, весь контент идёт на OpenRouter
    (не платим idle-таймаут снова на мёртвый M3). Взводится: (а) completion-preflight провалился
    ИЛИ (б) N реальных зависаний M3 по ходу набора. Сбрасывается на НОВЫЙ набор (arm с новым
    run_key). Состояние глобальное (один воркер-процесс, наборы идут ПОСЛЕДОВАТЕЛЬНО — «закон
    одного окна»); внутри набора потоки (prefetch/C1) шарят его потокобезопасно через Lock.
    Управляет ТОЛЬКО тем, стоит ли вообще дёргать M3-сторону фолбэка — выбор провайдера в попапе
    (llm_provider) НЕ трогает: это СТРАХОВКА поверх выбора, не замена."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run_key = None
        self._tripped = False
        self._timeouts = 0

    def arm(self, run_key, tripped: bool = False) -> None:
        """Начало набора: сброс состояния под новый run_key. tripped=True — предвзведён
        (completion-preflight уже показал мёртвый M3-completion)."""
        with self._lock:
            self._run_key = run_key
            self._tripped = bool(tripped)
            self._timeouts = 0

    def is_tripped(self) -> bool:
        with self._lock:
            return self._tripped

    def record_m3_timeout(self) -> bool:
        """Зафиксировать РЕАЛЬНОЕ зависание M3. Взводит breaker при достижении порога.
        → True если breaker взведён (сейчас или раньше)."""
        with self._lock:
            if self._tripped:
                return True
            self._timeouts += 1
            if self._timeouts >= _M3_BREAKER_TIMEOUT_THRESHOLD:
                self._tripped = True
            return self._tripped


_M3_BREAKER = _M3CircuitBreaker()


def arm_m3_breaker(run_key, tripped: bool = False) -> None:
    """Публичная обёртка: взвести/сбросить circuit-breaker M3 на новый набор."""
    _M3_BREAKER.arm(run_key, tripped=tripped)


def m3_breaker_tripped() -> bool:
    """True = M3 признан мёртвым на текущий набор (весь контент идёт на OpenRouter)."""
    return _M3_BREAKER.is_tripped()


def _is_m3_hang(err) -> bool:
    """Ошибка M3-вызова = именно ЗАВИСАНИЕ (нет токенов > idle / обрыв), а не HTTP/пустой ответ.
    Только зависания копятся в breaker — транзиентный HTTP/обрыв не должен флипать набор."""
    low = (err or "").lower()
    return ("завис" in low) or ("нет токенов" in low)


def m3_completion_preflight_ok(retries: int | None = None, timeout: float | None = None) -> bool:
    """Реальный тест ГЕНЕРАЦИИ M3: 1-токенный completion с жёстким коротким idle-таймаутом.
    ОТЛИЧАЕТСЯ от _m3_preflight_ok (только GET /v1/models — жив ли эндпоинт): ловит «health GET
    отвечает, а /chat/completions висит» (живой баг 09.07: до 4 обращений × 90-120с впустую).
    True = M3 выдал хотя бы 1 токен за timeout хотя бы в одной из retries попыток.
    Вызывать ОДИН раз в начале набора (не на каждой РК)."""
    n = retries if retries is not None else _M3_COMPLETION_PREFLIGHT_RETRIES
    to = timeout if timeout is not None else _M3_COMPLETION_PREFLIGHT_TIMEOUT
    for _ in range(max(1, n)):
        text, _err = _m3_complete_url(
            _M3_LLM_URLS_14B[0], [{"role": "user", "content": "ok"}],
            max_tokens=1, temperature=0.0, tries=1, backoff=0.0, timeout=to)
        if text and text.strip():
            return True
    return False


def _llm_pair_for(provider: str) -> tuple:
    """(complete_url, complete_parallel) для провайдера из попапа с ДВУСТОРОННИМ фолбэком:
    m3 → при сбое OpenRouter; openrouter → при сбое M3. Переключение видно в журнале
    ([llm-fallback]) — «падение одного — переключение на другого» (Семён 03.07).
    Circuit-breaker (_M3_BREAKER) управляет ТЕМ, дёргать ли M3-сторону вообще: взведён → M3
    пропускается (не платим idle снова), контент идёт на OpenRouter. Выбор провайдера в попапе
    сохранён — breaker это СТРАХОВКА поверх, не замена."""
    provider = (provider or "").strip().lower()
    if provider == "openrouter":
        primary, secondary, tag = _or_complete_url, _m3_complete_url, "OpenRouter→M3"
    else:
        primary, secondary, tag = _m3_complete_url, _or_complete_url, "M3→OpenRouter"

    m3_is_primary = (primary is _m3_complete_url)
    m3_is_secondary = (secondary is _m3_complete_url)

    def _url(url, messages, **kw):
        breaker = _M3_BREAKER.is_tripped()   # M3 признан мёртвым на этот набор → не дёргаем
        if m3_is_primary:
            # M3-первичный: breaker взведён (completion-preflight/2 зависания) ИЛИ health GET мёртв
            # → сразу OpenRouter, не платим idle. Выбор M3 в попапе сохранён — это авто-фолбэк.
            if breaker or not _m3_preflight_ok():
                reason = ("circuit-breaker: M3 completion мёртв на набор" if breaker
                          else "3/3 health-check провалились")
                print(f"[llm-preflight] M3 недоступна ({reason}) → прямой fallback OpenRouter",
                      flush=True)
                text2, err2 = secondary(url, messages, **kw)
                if text2:
                    return text2, None
                return None, f"M3 skip ({reason}); fallback({tag}): {err2}"
            text, err = primary(url, messages, **kw)
            if text:
                return text, err
            # M3-primary вернул ошибку: зависание — копим в breaker (2 → весь остаток на OpenRouter)
            if _is_m3_hang(err) and _M3_BREAKER.record_m3_timeout():
                print("[llm-breaker] порог зависаний M3 достигнут → breaker ВЗВЕДЁН, "
                      "остаток набора на OpenRouter", flush=True)
            err = err or "primary вернул пустой content"   # диагноз переключения всегда именуем
            text2, err2 = secondary(url, messages, **kw)
            if text2:
                print(f"[llm-fallback] {tag}: {str(err)[:120]}", flush=True)
                return text2, err2
            return None, f"{err}; fallback({tag}): {err2}"
        # OpenRouter-первичный: M3 — лишь вторичный фолбэк. При взведённом breaker вообще не
        # дёргаем мёртвый M3 (не платим idle); при живом — штатный фолбэк на M3.
        text, err = primary(url, messages, **kw)
        if text:
            return text, err
        err = err or "primary вернул пустой content"
        if m3_is_secondary and breaker:
            return None, f"{err}; M3-fallback пропущен (circuit-breaker: M3 мёртв на набор)"
        text2, err2 = secondary(url, messages, **kw)
        if text2:
            print(f"[llm-fallback] {tag}: {str(err)[:120]}", flush=True)
            return text2, err2
        if m3_is_secondary and _is_m3_hang(err2) and _M3_BREAKER.record_m3_timeout():
            print("[llm-breaker] порог зависаний M3 достигнут → breaker ВЗВЕДЁН, "
                  "остаток набора на OpenRouter", flush=True)
        return None, f"{err}; fallback({tag}): {err2}"

    def _par(tasks: list) -> list:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _call(idx_task):
            idx, (u, msgs, kw) = idx_task
            return idx, _url(u, msgs, **(kw or {}))

        if not tasks:
            return []
        results = [None] * len(tasks)
        with ThreadPoolExecutor(max_workers=min(4, len(tasks))) as ex:
            futs = [ex.submit(_call, (i, t)) for i, t in enumerate(tasks)]
            for f in as_completed(futs):
                idx, res = f.result()
                results[idx] = res
        return results

    return _url, _par


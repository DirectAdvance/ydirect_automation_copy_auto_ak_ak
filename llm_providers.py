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


# ── вызовы M3 / OpenRouter / fan-out / двусторонний фолбэк ─────────────────────────
def _m3_complete(messages: list, max_tokens: int = 400, temperature: float = 0.8,
                 top_p: float | None = None, repetition_penalty: float | None = None,
                 tries: int = 3, backoff: float = 5.0, timeout: float | None = None) -> tuple[str | None, str | None]:
    """Один вызов M3 (mlx /v1/chat/completions). → (text, error). model не шлём.
    top_p/repetition_penalty — анти-зацикливание (14B на длинном промпте иначе уходит в повтор).
    tries/backoff — ретраи на ТРАНСПОРТНОМ обрыве и «утечке» служебной ошибки в текст: mlx на M3
    под нехваткой RAM (speculative decoding) падает в процессе генерации (RemoteDisconnected),
    watchdog поднимает его за ~5с — поэтому между попытками ждём `backoff` секунд.
    timeout — переопределение таймаута запроса (для сид-прохода: быстрый фейл-фаст)."""
    import time as _time
    import requests as _rqs
    _to = float(timeout) if timeout is not None else _M3_LLM_TIMEOUT
    payload = {"messages": messages, "max_tokens": int(max_tokens), "temperature": float(temperature)}
    if top_p is not None:
        payload["top_p"] = float(top_p)
    if repetition_penalty is not None:
        payload["repetition_penalty"] = float(repetition_penalty)
    last_err = "M3 недоступна"
    n = max(1, tries)
    for attempt in range(n):
        last = (attempt == n - 1)
        _touch_running_jobs_heartbeat()   # LLM-вызов = прогресс (анти-watchdog при медленном M3)
        try:
            r = _rqs.post(f"{_M3_LLM_URL}/v1/chat/completions", json=payload, timeout=_to)
        except Exception as e:  # noqa: BLE001
            last_err = f"M3 недоступна: {str(e)[:160]}"
            if not last:
                _time.sleep(backoff)   # обрыв (RemoteDisconnected) → ждём перезапуск mlx watchdog'ом
            continue
        if r.status_code != 200:
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            if not last:
                _time.sleep(backoff)
            continue
        try:
            content = r.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError):
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
    """Как _m3_complete, но с явным URL — для fan-out на разные порты 14B/72B."""
    import time as _time
    import requests as _rqs
    _to = float(timeout) if timeout is not None else _M3_LLM_TIMEOUT
    payload = {"messages": messages, "max_tokens": int(max_tokens), "temperature": float(temperature)}
    if top_p is not None:
        payload["top_p"] = float(top_p)
    if repetition_penalty is not None:
        payload["repetition_penalty"] = float(repetition_penalty)
    last_err = f"M3 ({url}) недоступна"
    n = max(1, tries)
    for attempt in range(n):
        last = (attempt == n - 1)
        _touch_running_jobs_heartbeat()   # LLM-вызов = прогресс (анти-watchdog при медленном M3)
        try:
            r = _rqs.post(f"{url}/v1/chat/completions", json=payload, timeout=_to)
        except Exception as e:  # noqa: BLE001
            last_err = f"M3 ({url}) недоступна: {str(e)[:120]}"
            if not last:
                _time.sleep(backoff)
            continue
        if r.status_code != 200:
            last_err = f"HTTP {r.status_code}: {r.text[:160]}"
            if not last:
                _time.sleep(backoff)
            continue
        try:
            content = r.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError):
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
                     headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
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
    payload = {"model": _OPENROUTER_LLM_MODEL, "messages": messages,
               "max_tokens": int(max_tokens), "temperature": float(temperature)}
    if top_p is not None:
        payload["top_p"] = float(top_p)
    last_err = "OpenRouter: нет ответа"
    n = max(1, tries)
    for attempt in range(n):
        last = (attempt == n - 1)
        _touch_running_jobs_heartbeat()   # LLM-вызов = прогресс (анти-watchdog)
        try:
            r = _rqs.post("https://openrouter.ai/api/v1/chat/completions", json=payload,
                          headers={"Authorization": f"Bearer {key}"}, timeout=timeout or 120)
        except Exception as e:  # noqa: BLE001
            last_err = f"OpenRouter недоступен: {str(e)[:120]}"
            if not last:
                _time.sleep(backoff)
            continue
        if r.status_code != 200:
            last_err = f"OpenRouter HTTP {r.status_code}: {r.text[:160]}"
            if not last:
                _time.sleep(backoff)
            continue
        try:
            content = r.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError):
            last_err = "OpenRouter: пустой ответ"
            if not last:
                _time.sleep(backoff)
            continue
        if (content or "").strip():
            return content, None
        last_err = "OpenRouter: пустой content"
        if not last:
            _time.sleep(backoff)
    return None, last_err


def _llm_pair_for(provider: str) -> tuple:
    """(complete_url, complete_parallel) для провайдера из попапа с ДВУСТОРОННИМ фолбэком:
    m3 → при сбое OpenRouter; openrouter → при сбое M3. Переключение видно в журнале
    ([llm-fallback]) — «падение одного — переключение на другого» (Семён 03.07)."""
    provider = (provider or "").strip().lower()
    if provider == "openrouter":
        primary, secondary, tag = _or_complete_url, _m3_complete_url, "OpenRouter→M3"
    else:
        primary, secondary, tag = _m3_complete_url, _or_complete_url, "M3→OpenRouter"

    def _url(url, messages, **kw):
        text, err = primary(url, messages, **kw)
        if text:
            return text, err
        err = err or "primary вернул пустой content"   # диагноз переключения всегда именуем
        text2, err2 = secondary(url, messages, **kw)
        if text2:
            print(f"[llm-fallback] {tag}: {str(err)[:120]}", flush=True)
            return text2, err2
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


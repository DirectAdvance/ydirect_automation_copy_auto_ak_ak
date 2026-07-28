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


def _set_llm_heartbeat_job(job_id: str | None) -> None:
    """Заглушка. Воркер инъектит установку scoped heartbeat job-id для thread-pool workers."""


def _current_llm_heartbeat_job() -> str | None:
    """Заглушка. Воркер инъектит чтение текущей scoped heartbeat job-id."""
    return None


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
# Предохранитель от БЕСКОНЕЧНОГО потока. 20 минут на один content-call блокировали
# весь create-set до первой Direct-мутации, если M3 продолжал слать бесполезный стрим.
_M3_LLM_HARD_CAP = float(os.environ.get("M3_LLM_HARD_CAP", "240"))

# ── IDLE-таймаут КОНТЕНТ-вызовов (задача «убрать налог на висящий M3», 2026-07-09) ──────
# Со стримингом (E) idle = пауза МЕЖДУ токенами. Живой M3 стримит ~6.5 ток/с (гэп <1с), поэтому
# idle=30с НЕ бросит рабочий M3, только реально ВИСЯЩИЙ (0 токенов). Раньше content-вызовы висели
# 90с (14B fast) / 120с (72B repair) / 360с (14B) — до 4 обращений на РК = главный тормоз при
# мёртвом completion (health GET жив, генерация мертва — живой баг 09.07). Env-конфиг, не хардкод.
_M3_CONTENT_IDLE_TIMEOUT = float(os.environ.get("M3_CONTENT_IDLE_TIMEOUT", "30"))

# ── Circuit-breaker M3 НА НАБОР (задача «убрать налог на висящий M3», 2026-07-09) ───────
# completion-preflight: 1-токенный тест ГЕНЕРАЦИИ (не health GET) — короткий idle-таймаут.
_M3_COMPLETION_PREFLIGHT_TIMEOUT = float(os.environ.get("M3_COMPLETION_PREFLIGHT_TIMEOUT", "30"))
_M3_COMPLETION_PREFLIGHT_RETRIES = int(os.environ.get("M3_COMPLETION_PREFLIGHT_RETRIES", "2"))
_OPENROUTER_COMPLETION_PROBE_TOKENS = int(os.environ.get("OPENROUTER_COMPLETION_PROBE_TOKENS", "800"))
_M3_SOLO_COMPLETION_REQUIRED_SUCCESSES = int(os.environ.get("M3_SOLO_COMPLETION_REQUIRED_SUCCESSES", "2"))
# Быстрый GET /v1/models перед генерацией. Было 3с хардкодом: при живой, но занятой/медленной
# связке LXC→m3-relay→mlx 8086 M3 объявлялась мёртвой, и create-set уходил в OpenRouter,
# где дальше ловил empty content/429. Оставляем fail-fast, но делаем порог управляемым.
_M3_PREFLIGHT_MODELS_TIMEOUT = float(os.environ.get("M3_PREFLIGHT_MODELS_TIMEOUT", "12"))
# Сколько РЕАЛЬНЫХ зависаний M3 по ходу набора взводят breaker (весь остаток → OpenRouter).
_M3_BREAKER_TIMEOUT_THRESHOLD = int(os.environ.get("M3_BREAKER_TIMEOUT_THRESHOLD", "2"))
_SSE_ITER_CHUNK_SIZE = max(1, int(os.environ.get("LLM_SSE_ITER_CHUNK_SIZE", "1")))

# Один mlx endpoint (обычно 8086 с 72B) плохо переживает конкурентные completions из разных
# create-set job: health может быть зелёным, но второй поток ждёт первый токен дольше idle и
# получает ложный hang. Сериализуем только одинаковые URL; разные порты остаются параллельными.
_M3_ENDPOINT_LOCKS: dict[str, threading.Lock] = {}
_M3_ENDPOINT_LOCKS_GUARD = threading.Lock()
_M3_ENDPOINT_LOCK_WAIT_STEP = float(os.environ.get("M3_ENDPOINT_LOCK_WAIT_STEP", "5"))
_M3_ENDPOINT_LOCK_MAX_WAIT = float(os.environ.get("M3_ENDPOINT_LOCK_MAX_WAIT", "90"))


def _m3_endpoint_lock(url: str) -> threading.Lock:
    key = str(url or _M3_LLM_URL).rstrip("/")
    with _M3_ENDPOINT_LOCKS_GUARD:
        lock = _M3_ENDPOINT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _M3_ENDPOINT_LOCKS[key] = lock
        return lock


class _M3EndpointGuard:
    """Ожидание общей M3-очереди тоже должно тикать heartbeat текущей job."""

    def __init__(self, url: str, wait_step: float | None = None):
        self._url = str(url or _M3_LLM_URL).rstrip("/")
        self._lock = _m3_endpoint_lock(url)
        self._wait_step = max(0.001, float(wait_step or _M3_ENDPOINT_LOCK_WAIT_STEP))

    def __enter__(self):
        import time as _time
        started = _time.time()
        while not self._lock.acquire(timeout=self._wait_step):
            _touch_running_jobs_heartbeat()
            if _M3_ENDPOINT_LOCK_MAX_WAIT > 0 and (_time.time() - started) > _M3_ENDPOINT_LOCK_MAX_WAIT:
                raise TimeoutError(
                    f"M3 endpoint занят > {_M3_ENDPOINT_LOCK_MAX_WAIT:.0f}с: {self._url}"
                )
        _touch_running_jobs_heartbeat()
        return self._lock

    def __exit__(self, exc_type, exc, tb):
        self._lock.release()
        return False


def _m3_endpoint_guard(url: str, wait_step: float | None = None) -> _M3EndpointGuard:
    return _M3EndpointGuard(url, wait_step=wait_step)


def _run_with_llm_heartbeat_job(job_id: str | None, fn, *args, **kwargs):
    """ThreadPoolExecutor не наследует threading.local(); прокидываем job-id явно."""
    if job_id:
        _set_llm_heartbeat_job(job_id)
    try:
        return fn(*args, **kwargs)
    finally:
        if job_id:
            _set_llm_heartbeat_job(None)


# ── Счётчик деградации контент-пайплайна НА НАБОР (задача «видимость фолбэка», 2026-07-19) ──
# Зачем: «пустой content» от OpenRouter молча уводил генерацию на статический фолбэк, и
# шаблонность годами выглядела как «архитектура такая». Теперь каждый отказ LLM и каждое
# падение РК на статику именуются и считаются, сводка печатается в конце набора.
class _LLMDegradeStats:
    """Потокобезопасный per-run счётчик: отказы LLM по причинам + падения на статический фолбэк.
    Живёт ровно один набор (сброс в arm_m3_breaker), как и circuit-breaker'ы."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run_key = None
        self._llm_fail: dict = {}       # "openrouter:reasoning_only" -> n
        self._fallback: dict = {}       # причина падения РК на статику -> n
        self._campaigns = 0

    def reset(self, run_key) -> None:
        with self._lock:
            self._run_key = run_key
            self._llm_fail = {}
            self._fallback = {}
            self._campaigns = 0

    def record_llm_failure(self, provider: str, reason: str) -> None:
        with self._lock:
            key = f"{provider}:{reason}"
            self._llm_fail[key] = self._llm_fail.get(key, 0) + 1

    def record_campaign(self, fallback: bool, reason: str = "") -> None:
        """Одна сгенерированная РК: fallback=True — контент собран НЕ из LLM, а из корпуса/статики."""
        with self._lock:
            self._campaigns += 1
            if fallback:
                key = (reason or "неизвестно")[:120]
                self._fallback[key] = self._fallback.get(key, 0) + 1

    def snapshot(self) -> dict:
        with self._lock:
            return {"run_key": self._run_key, "campaigns": self._campaigns,
                    "fallback_total": sum(self._fallback.values()),
                    "fallback_by_reason": dict(self._fallback),
                    "llm_failures": dict(self._llm_fail)}


_LLM_DEGRADE = _LLMDegradeStats()


def record_llm_failure(provider: str, reason: str) -> None:
    """Именованный отказ LLM-вызова (не транспортная деталь, а КЛАСС причины)."""
    _LLM_DEGRADE.record_llm_failure(provider, reason)


def record_content_fallback(fallback: bool, reason: str = "") -> None:
    """Одна РК прошла генерацию: упала ли она на статический фолбэк и почему."""
    _LLM_DEGRADE.record_campaign(fallback, reason)


def llm_degrade_stats() -> dict:
    """Сводка деградации за текущий набор (для лога/сводки джобы)."""
    return _LLM_DEGRADE.snapshot()


def log_llm_degrade_summary(tag: str = "") -> dict:
    """Печатает сводку в журнал сервиса. Возвращает её же — вызывающий может положить в result."""
    snap = _LLM_DEGRADE.snapshot()
    total, fb = snap["campaigns"], snap["fallback_total"]
    if not total and not snap["llm_failures"]:
        return snap
    share = (100.0 * fb / total) if total else 0.0
    print(f"[content-degrade]{(' ' + tag) if tag else ''} РК={total}, "
          f"на статическом фолбэке={fb} ({share:.0f}%), "
          f"причины={snap['fallback_by_reason']}, отказы LLM={snap['llm_failures']}", flush=True)
    return snap


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
    """True = M3 жив (хотя бы одна из retries попыток GET /v1/models прошла за env-timeout).
    False = все упали → caller должен сразу уходить в OpenRouter, не ждать полный read-timeout.
    Добавлено 2026-07-07: интермиттентный лаг туннеля 8086 давал серию 90-480с read-timeout'ов
    вместо быстрого фолбэка, что будило watchdog."""
    import time as _time
    for attempt in range(retries):
        if _m3_llm_probe(timeout=_M3_PREFLIGHT_MODELS_TIMEOUT):
            return True
        if attempt < retries - 1:
            _time.sleep(pause)
    return False


def _m3_health_or_completion_ok(retries: int = 3, pause: float = 3.0) -> bool:
    """Health GET first; if it blinks, trust a real one-token completion probe."""
    if _m3_preflight_ok(retries=retries, pause=pause):
        return True
    if m3_completion_preflight_ok(retries=1):
        print("[llm-preflight] M3 /v1/models не ответил, но completion-probe OK", flush=True)
        return True
    return False


# ── вызовы M3 / OpenRouter / fan-out / двусторонний фолбэк ─────────────────────────
# ── общий сборщик SSE-стрима (OpenAI-совместимый чат: M3 mlx и OpenRouter) ──────────
def _consume_sse_stream(resp, hard_cap: float, started: float,
                        idle: float | None = None) -> tuple[str, bool, dict]:
    """Читает SSE-поток /v1/chat/completions, аккумулирует delta.content по чанкам.
    HEARTBEAT ТОЛЬКО НА РЕАЛЬНЫХ ТОКЕНАХ — watchdog видит живость по НАСТОЯЩИМ чанкам
    (content/reasoning), а не по SSE keep-alive комментариям (': PROCESSING'), которые
    сервер шлёт пока занят. keep-alive-only стрим → watchdog корректно определит зависание.
    → (собранный_текст, hit_cap, meta). Исключения requests (idle read-timeout / обрыв) НЕ ловит —
    их ловит вызывающий и трактует как «зависание LLM». hard_cap — предохранитель от
    бесконечного потока (не рвёт нормальную генерацию).
    idle — макс. тишина по РЕАЛЬНЫМ токенам: если сервер шлёт только keep-alive дольше idle
    секунд — бросаем TimeoutError (вызывающий трактует как «зависание»).

    meta = {"reasoning_chars", "finish_reason", "chunks"} — ДИАГНОСТИКА «пустого content».
    Reasoning-модели (напр. deepseek-v4-flash) пишут в `delta.reasoning`, а не в `delta.content`,
    и на длинном боевом промпте (5.8k симв.) съедают ВЕСЬ max_tokens рассуждением →
    `finish_reason=length`, content пуст ВСЕГДА (замер 2026-07-19: 16/16 на 280 и 2000 токенов).
    Без этого счётчика класс выглядел как безымянный «пустой content» и годами уводил
    генерацию в тихий статический фолбэк."""
    import json as _json
    import time as _time
    parts: list = []
    reasoning_chars = 0
    finish_reason = None
    chunks = 0
    hit_cap = False
    last_progress_t = started   # время последнего реального SSE-чанка (контент/reasoning)
    # decode_unicode=False → байты: декодируем UTF-8 САМИ. text/event-stream без charset requests
    # трактует как latin-1 → кириллица бьётся в мойибаке (OpenRouter, живой баг 2026-07-09).
    # Важно: default chunk_size=512 может буферизовать короткие SSE keep-alive строки и не отдавать
    # управление в цикл, пока socket всё ещё получает байты. Тогда read-timeout не срабатывает,
    # а наш idle-check не выполняется. chunk_size=1 делает keep-alive видимым для idle-предохранителя.
    for raw_b in resp.iter_lines(chunk_size=_SSE_ITER_CHUNK_SIZE, decode_unicode=False):
        if hard_cap and (_time.time() - started) > hard_cap:
            hit_cap = True
            break
        if not raw_b:
            continue
        line = (raw_b.decode("utf-8", "replace") if isinstance(raw_b, bytes) else raw_b).strip()
        if not line or line.startswith(":"):   # SSE-комментарий (OpenRouter keep-alive ": PROCESSING")
            # Стрим жив keep-alive'ами, реальных токенов нет — проверяем idle с последнего токена
            if idle and (_time.time() - last_progress_t) > idle:
                raise TimeoutError(f"SSE: keep-alive без токенов >{idle:.0f}с")
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            obj = _json.loads(data)
            choice = (obj.get("choices") or [{}])[0]
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            chunks += 1
            piece = delta.get("content")
            if piece:
                parts.append(piece)
            # Reasoning-модели кладут текст СЮДА, а не в content — считаем, чтобы «пустой
            # content» можно было отличить от «модель думала и не успела ответить».
            rz = delta.get("reasoning") or delta.get("reasoning_content")
            if isinstance(rz, str) and rz:
                reasoning_chars += len(rz)
            # Реальный SSE-чанк — обновляем heartbeat и idle-таймер.
            # НЕ вызываем на keep-alive: watchdog должен видеть реальный прогресс, не ping-pong.
            _touch_running_jobs_heartbeat()
            last_progress_t = _time.time()
        except (ValueError, KeyError, IndexError, TypeError):
            continue
    return "".join(parts), hit_cap, {"reasoning_chars": reasoning_chars,
                                     "finish_reason": finish_reason, "chunks": chunks}


def _empty_content_class(meta: dict | None) -> str:
    """КЛАСС причины пустого content (для счётчика). reasoning_only — главный живой класс:
    reasoning-модель израсходовала max_tokens на рассуждение и до content не дошла."""
    meta = meta or {}
    if int(meta.get("reasoning_chars") or 0) > 0:
        return "reasoning_only"
    if not int(meta.get("chunks") or 0):
        return "no_chunks"
    return "empty_content"


def _empty_content_reason(who: str, meta: dict | None) -> str:
    """Человекочитаемый диагноз пустого content — с конкретным действием, а не «пусто»."""
    meta = meta or {}
    rz = int(meta.get("reasoning_chars") or 0)
    fin = meta.get("finish_reason")
    if rz > 0:
        hint = (f" Смените OPENROUTER_LLM_MODEL на не-reasoning (сейчас {_OPENROUTER_LLM_MODEL})."
                if who.lower().startswith("openrouter") else "")
        return (f"{who}: модель вернула ТОЛЬКО reasoning ({rz} симв., finish={fin}), content пуст — "
                f"это reasoning-модель, max_tokens съеден рассуждением.{hint}")
    return f"{who}: пустой content (finish={fin}, чанков={meta.get('chunks')})"


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
        with _m3_endpoint_guard(_M3_LLM_URL):
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
                content, hit_cap, meta = _consume_sse_stream(r, _M3_LLM_HARD_CAP, started, idle=idle)
            except Exception as e:  # noqa: BLE001
                # тишина > idle_gap ИЛИ обрыв соединения = ЗАВИСАНИЕ (и только это) → ретрай/фолбэк
                last_err = f"M3 зависла (нет токенов > {idle:.0f}с/обрыв): {str(e)[:160]}"
                record_llm_failure("m3", "hang")
                if not last:
                    _time.sleep(backoff)
                continue
            finally:
                r.close()
            if hit_cap:
                print(f"[llm-m3] hard-cap {_M3_LLM_HARD_CAP:.0f}с — стрим обрезан предохранителем", flush=True)
            if not (content or "").strip():
                last_err = _empty_content_reason("M3", meta)
                record_llm_failure("m3", _empty_content_class(meta))
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
        with _m3_endpoint_guard(url):
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
                content, hit_cap, meta = _consume_sse_stream(r, _M3_LLM_HARD_CAP, started, idle=idle)
            except Exception as e:  # noqa: BLE001
                last_err = f"M3 ({url}) зависла (нет токенов > {idle:.0f}с/обрыв): {str(e)[:120]}"
                record_llm_failure("m3", "hang")
                if not last:
                    _time.sleep(backoff)
                continue
            finally:
                r.close()
            if hit_cap:
                print(f"[llm-m3] {url}: hard-cap {_M3_LLM_HARD_CAP:.0f}с — стрим обрезан", flush=True)
            if not (content or "").strip():
                last_err = _empty_content_reason(f"M3 ({url})", meta)
                record_llm_failure("m3", _empty_content_class(meta))
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

    heartbeat_job = _current_llm_heartbeat_job()

    def _call(idx_task):
        idx, (url, msgs, kw) = idx_task
        return idx, _run_with_llm_heartbeat_job(heartbeat_job, _m3_complete_url, url, msgs, **kw)

    if not tasks:
        return []
    unique_urls = {str((t[0] if t else "") or "").rstrip("/") for t in tasks}
    max_workers = len(tasks) if len(unique_urls) > 1 else 1
    results = [None] * len(tasks)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
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
# ⚠ ДЕФОЛТ ОБЯЗАН БЫТЬ НЕ-REASONING МОДЕЛЬЮ. Прежний дефолт `deepseek/deepseek-v4-flash` —
# reasoning-модель: на боевом промпте (5.8k симв.) она пишет ТОЛЬКО в `delta.reasoning`, весь
# max_tokens уходит в рассуждение (finish=length), `content` пуст ВСЕГДА. Замер 2026-07-19 на
# LXC101: v4-flash 10/10 пусто при max_tokens=280 и 6/6 при 2000; deepseek-chat 0/10 пусто.
# Пустой content молча уводил генерацию в статический фолбэк = та самая «шаблонность».
# Прод-юниты это уже перекрывали drop-in'ом, но `direct-copy`/`direct-slepki`/`direct-accounts`/
# `digest` override НЕ имели и работали на сломанном дефолте.
_OPENROUTER_LLM_MODEL = os.environ.get("OPENROUTER_LLM_MODEL", "deepseek/deepseek-chat")
_OPENROUTER_KEY_CACHE: dict = {}
# СТРИМ-таймауты OpenRouter (см. M3: connect / idle-между-токенами / предохранитель), задача E.
_OPENROUTER_CONNECT_TIMEOUT = float(os.environ.get("OPENROUTER_CONNECT_TIMEOUT", "15"))
_OPENROUTER_IDLE_TIMEOUT = float(os.environ.get("OPENROUTER_IDLE_TIMEOUT", "45"))
_OPENROUTER_HARD_CAP = float(os.environ.get("OPENROUTER_HARD_CAP", "240"))


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


def _openrouter_completion_probe(timeout: float = 8.0) -> bool:
    """Минимальный платёжеспособный probe completions.

    `/api/v1/key` проверяет только валидность ключа/лимит, но не гарантирует, что текущий
    completions-запрос пройдёт: live-инцидент 2026-07-25 дал `/key` OK и массовые HTTP 402
    на генерации. Запрашиваем тот же порядок max_tokens, что у тяжёлых генераций, иначе
    `max_tokens=1` проходит при остатке 134–195 токенов и даёт ложный OK.
    """
    key = _openrouter_api_key()
    if not key:
        return False
    import requests as _rqs
    payload = {
        "model": _OPENROUTER_LLM_MODEL,
        "messages": [{"role": "user", "content": "ok"}],
        "max_tokens": _OPENROUTER_COMPLETION_PROBE_TOKENS,
        "temperature": 0,
        "stream": False,
    }
    try:
        r = _rqs.post("https://openrouter.ai/api/v1/chat/completions", json=payload,
                      headers={"Authorization": f"Bearer {key}"},
                      timeout=(_OPENROUTER_CONNECT_TIMEOUT, timeout), proxies=_OR_PROXIES)
        if r.status_code == 200:
            return True
        print(f"[llm-or] completion-probe failed: HTTP {r.status_code}: {r.text[:120]}", flush=True)
        return False
    except Exception as e:  # noqa: BLE001
        print(f"[llm-or] completion-probe unavailable: {str(e)[:120]}", flush=True)
        return False


def check_content_pipeline_health(
    or_retries: int = 2,
    or_pause: float = 2.0,
    or_timeout: float = 5.0,
) -> dict:
    """Проверяет оба провайдера контент-пайплайна ПАРАЛЛЕЛЬНО.

    M3:  GET /v1/models используется только как диагностика доступности endpoint'а.
          Живым для create-set считается только реальный 1-token completion-probe:
          /v1/models OK при висящем /chat/completions не должен пропускать набор.
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

    def _check_m3() -> tuple[bool, bool]:
        models_alive = _m3_preflight_ok(retries=3, pause=3.0)
        completion_alive = m3_completion_preflight_ok(retries=1)
        if models_alive and not completion_alive:
            print("[llm-preflight] M3 /v1/models OK, но completion-probe не дал токен", flush=True)
        elif (not models_alive) and completion_alive:
            print("[llm-preflight] M3 /v1/models не ответил, но completion-probe OK", flush=True)
        return models_alive, completion_alive

    def _check_or() -> bool:
        for attempt in range(or_retries):
            if _openrouter_probe(timeout=or_timeout) and _openrouter_completion_probe(timeout=or_timeout):
                return True
            if attempt < or_retries - 1:
                _time.sleep(or_pause)
        return False

    with ThreadPoolExecutor(max_workers=2) as _ex:
        _fm3 = _ex.submit(_check_m3)
        _for = _ex.submit(_check_or)
        m3_models_alive, m3_alive = _fm3.result()
        or_alive = _for.result()
    m3_solo_stable_alive = m3_alive
    if m3_alive and not or_alive and _M3_SOLO_COMPLETION_REQUIRED_SUCCESSES > 1:
        # Если OpenRouter неплатежеспособен/недоступен, M3 остаётся единственным контуром.
        # Одного успешного 1-token probe недостаточно при флапающем completion: следующий hang
        # даст частичный набор без fallback. Поэтому перед первой Direct-мутацией требуем
        # несколько подряд успешных completion-probe и лучше блокируем чисто, чем плодим partial.
        m3_solo_stable_alive = m3_completion_preflight_ok(
            retries=_M3_SOLO_COMPLETION_REQUIRED_SUCCESSES,
            min_successes=_M3_SOLO_COMPLETION_REQUIRED_SUCCESSES,
        )
        if not m3_solo_stable_alive:
            print(
                "[llm-preflight] OpenRouter недоступен, а M3 completion не прошёл стабильный "
                f"{_M3_SOLO_COMPLETION_REQUIRED_SUCCESSES}/{_M3_SOLO_COMPLETION_REQUIRED_SUCCESSES} probe",
                flush=True,
            )
            m3_alive = False

    if m3_alive and or_alive:
        message = "M3 completion жив, OpenRouter жив"
    elif m3_alive:
        message = "M3 completion жив (OpenRouter недоступен — идём без фолбэка)"
    elif or_alive:
        if m3_models_alive:
            message = "OpenRouter жив (M3 /v1/models жив, но completion мёртв — M3 отключён)"
        else:
            message = "OpenRouter жив (M3 недоступен — фолбэк включится автоматически)"
    else:
        if m3_models_alive:
            message = (
                "M3 /v1/models жив, но completion не отвечает; "
                "OpenRouter не отвечает/не проходит completion-probe"
            )
        else:
            message = (
                "M3 не отвечает или completion мёртв; "
                "OpenRouter не отвечает/не проходит completion-probe"
            )
    return {"m3_alive": m3_alive, "m3_models_alive": m3_models_alive,
            "m3_completion_alive": m3_alive, "or_alive": or_alive,
            "m3_solo_stable_alive": m3_solo_stable_alive,
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
            content, hit_cap, meta = _consume_sse_stream(r, _OPENROUTER_HARD_CAP, started, idle=idle)
        except Exception as e:  # noqa: BLE001
            last_err = f"OpenRouter завис (нет токенов > {idle:.0f}с/обрыв): {str(e)[:120]}"
            record_llm_failure("openrouter", "hang")
            if not last:
                _time.sleep(backoff)
            continue
        finally:
            r.close()
        if hit_cap:
            print(f"[llm-or] hard-cap {_OPENROUTER_HARD_CAP:.0f}с — стрим обрезан", flush=True)
        if (content or "").strip():
            return content, None
        cls = _empty_content_class(meta)
        last_err = _empty_content_reason("OpenRouter", meta)
        record_llm_failure("openrouter", cls)
        # ГРОМКО: раньше это была немая строка «пустой content», и набор молча ехал на статику.
        print(f"[llm-or] попытка {attempt + 1}/{n}: {last_err}", flush=True)
        # РЕТРАИМ и reasoning_only тоже: класс СТОХАСТИЧЕСКИЙ, а не детерминированный —
        # замер 2026-07-19 на боевом промпте: v4-flash 6/12 и 8/12 пусто (~50-67%), т.е. повтор
        # выигрывает примерно в половине случаев. (Ранние прогоны давали серии 10/10 и 12/12 —
        # это разброс маршрутизации OpenRouter, НЕ детерминизм. Не сокращать здесь попытки.)
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

    def set_tripped(self, tripped: bool = True) -> None:
        with self._lock:
            self._tripped = bool(tripped)


_M3_BREAKER = _M3CircuitBreaker()

# Симметричный предохранитель для OpenRouter. Живой профиль (job 2b9d58d01e28): 3 × 15 с
# read-timeout за 8 мин прогона — провайдер не отвечал, но каждый вызов заново выжидал полный
# таймаут, после чего всё равно уходил на M3. Порог 1: первый ЖЕ таймаут/недоступность = мёртв
# на остаток набора, дальше сразу M3 (в отличие от M3-порога 2 — там зависание бывает разовым
# лагом туннеля 8086, а тут внешний провайдер за прокси, повтор почти всегда тот же таймаут).
# Выбор провайдера в попапе НЕ меняется — это страховка поверх, ровно как у M3-breaker.
_OR_BREAKER_TIMEOUT_THRESHOLD = int(os.environ.get("OR_BREAKER_TIMEOUT_THRESHOLD", "1"))


class _ORCircuitBreaker(_M3CircuitBreaker):
    """То же поведение, свой порог: пока ВЗВЕДЁН — OpenRouter не дёргаем, идём на M3."""

    def record_m3_timeout(self) -> bool:      # имя из базового класса; смысл — «таймаут OpenRouter»
        with self._lock:
            if self._tripped:
                return True
            self._timeouts += 1
            if self._timeouts >= _OR_BREAKER_TIMEOUT_THRESHOLD:
                self._tripped = True
            return self._tripped


_OR_BREAKER = _ORCircuitBreaker()


def arm_m3_breaker(run_key, tripped: bool = False) -> None:
    """Публичная обёртка: взвести/сбросить circuit-breaker M3 на новый набор.
    Заодно СБРАСЫВАЕТ OpenRouter-breaker — оба предохранителя живут ровно один набор
    (иначе мёртвый на прошлом наборе OpenRouter остался бы отключён навсегда)."""
    _M3_BREAKER.arm(run_key, tripped=tripped)
    _OR_BREAKER.arm(run_key, tripped=False)
    _LLM_DEGRADE.reset(run_key)   # счётчик деградации живёт ровно один набор, как и breaker'ы


def or_breaker_tripped() -> bool:
    """True = OpenRouter признан мёртвым на текущий набор (контент идёт на M3)."""
    return _OR_BREAKER.is_tripped()


def trip_or_breaker() -> None:
    """Отключить OpenRouter на остаток текущего набора после completion-preflight fail/402."""
    _OR_BREAKER.set_tripped(True)


def _is_or_dead(err) -> bool:
    """Ошибка OpenRouter = ожидание ВПУСТУЮ (idle-таймаут / недоступен), а не быстрый HTTP-отказ.
    Только такие взводят breaker: HTTP 4xx/5xx возвращается мгновенно и повтор ничего не стоит.

    ⚠ `reasoning_only` СЮДА НЕ ВХОДИТ намеренно: класс стохастический (~50-67% пусто, замер
    2026-07-19), повтор выигрывает примерно в половине случаев. Взвести breaker на него = убить
    провайдера на весь набор из-за ошибки, которая сама себя чинит ретраем."""
    low = (err or "").lower()
    return ("завис" in low) or ("недоступен" in low) or ("нет ответа" in low)


def m3_breaker_tripped() -> bool:
    """True = M3 признан мёртвым на текущий набор (весь контент идёт на OpenRouter)."""
    return _M3_BREAKER.is_tripped()


def _is_m3_hang(err) -> bool:
    """Ошибка M3-вызова = именно ЗАВИСАНИЕ (нет токенов > idle / обрыв), а не HTTP/пустой ответ.
    Только зависания копятся в breaker — транзиентный HTTP/обрыв не должен флипать набор."""
    low = (err or "").lower()
    return ("завис" in low) or ("нет токенов" in low)


def m3_completion_preflight_ok(
    retries: int | None = None,
    timeout: float | None = None,
    min_successes: int = 1,
) -> bool:
    """Реальный тест ГЕНЕРАЦИИ M3: 1-токенный completion с жёстким коротким idle-таймаутом.
    ОТЛИЧАЕТСЯ от _m3_preflight_ok (только GET /v1/models — жив ли эндпоинт): ловит «health GET
    отвечает, а /chat/completions висит» (живой баг 09.07: до 4 обращений × 90-120с впустую).
    True = M3 выдал хотя бы 1 токен за timeout в min_successes попытках.
    Вызывать ОДИН раз в начале набора (не на каждой РК)."""
    n = retries if retries is not None else _M3_COMPLETION_PREFLIGHT_RETRIES
    to = timeout if timeout is not None else _M3_COMPLETION_PREFLIGHT_TIMEOUT
    n = max(1, int(n))
    need = max(1, min(int(min_successes or 1), n))
    ok = 0
    for i in range(n):
        text, _err = _m3_complete_url(
            _M3_LLM_URLS_14B[0], [{"role": "user", "content": "ok"}],
            max_tokens=1, temperature=0.0, tries=1, backoff=0.0, timeout=to)
        if text and text.strip():
            ok += 1
        if ok >= need:
            return True
        if ok + (n - i - 1) < need:
            return False
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
        or_breaker = _OR_BREAKER.is_tripped()  # OpenRouter мёртв на этот набор → не выжидаем таймаут
        if m3_is_primary:
            # M3-первичный: breaker взведён (completion-preflight/2 зависания) ИЛИ health GET мёртв
            # → сразу OpenRouter, не платим idle. Выбор M3 в попапе сохранён — это авто-фолбэк.
            m3_ready_now = False if breaker else _m3_health_or_completion_ok()
            if breaker or not m3_ready_now:
                reason = ("circuit-breaker: M3 completion мёртв на набор" if breaker
                          else "3/3 health-check провалились")
                if not breaker:
                    _M3_BREAKER.set_tripped(True)
                print(f"[llm-preflight] M3 недоступна ({reason}) → прямой fallback OpenRouter",
                      flush=True)
                if or_breaker:      # и M3 пропущен, и OpenRouter мёртв — звать некого
                    return None, (f"M3 skip ({reason}); OpenRouter-fallback пропущен "
                                  f"(circuit-breaker: OpenRouter мёртв на набор)")
                text2, err2 = secondary(url, messages, **kw)
                if text2:
                    return text2, None
                if _is_or_dead(err2) and _OR_BREAKER.record_m3_timeout():
                    print("[llm-breaker] OpenRouter не отвечает → breaker ВЗВЕДЁН, "
                          "остаток набора без OpenRouter", flush=True)
                return None, f"M3 skip ({reason}); fallback({tag}): {err2}"
            text, err = primary(url, messages, **kw)
            if text:
                return text, err
            # M3-primary вернул ошибку: зависание — копим в breaker (2 → весь остаток на OpenRouter)
            if _is_m3_hang(err) and _M3_BREAKER.record_m3_timeout():
                print("[llm-breaker] порог зависаний M3 достигнут → breaker ВЗВЕДЁН, "
                      "остаток набора на OpenRouter", flush=True)
            err = err or "primary вернул пустой content"   # диагноз переключения всегда именуем
            if or_breaker:   # OpenRouter уже показал себя мёртвым — не выжидаем таймаут повторно
                return None, (f"{err}; OpenRouter-fallback пропущен "
                              f"(circuit-breaker: OpenRouter мёртв на набор)")
            text2, err2 = secondary(url, messages, **kw)
            if text2:
                print(f"[llm-fallback] {tag}: {str(err)[:120]}", flush=True)
                return text2, err2
            if _is_or_dead(err2) and _OR_BREAKER.record_m3_timeout():
                print("[llm-breaker] OpenRouter не отвечает → breaker ВЗВЕДЁН, "
                      "остаток набора без OpenRouter", flush=True)
            return None, f"{err}; fallback({tag}): {err2}"
        # OpenRouter-первичный: M3 — лишь вторичный фолбэк. При взведённом breaker вообще не
        # дёргаем мёртвый M3 (не платим idle); при живом — штатный фолбэк на M3.
        # Симметрично: если OpenRouter уже признан мёртвым на набор — не выжидаем его read-timeout
        # ЗАНОВО на каждой генерации, сразу идём на M3 (живой профиль: 3 × 15 с впустую).
        if or_breaker:
            reason = "circuit-breaker: OpenRouter не отвечает на набор"
            if breaker:
                return None, f"OpenRouter skip ({reason}); M3-fallback тоже пропущен (M3 мёртв)"
            text2, err2 = secondary(url, messages, **kw)
            if text2:
                return text2, None
            return None, f"OpenRouter skip ({reason}); fallback({tag}): {err2}"
        text, err = primary(url, messages, **kw)
        if text:
            return text, err
        # OpenRouter-primary не ответил: пустое ожидание (таймаут/недоступен) → взводим breaker,
        # чтобы остаток набора шёл прямо на M3 без повторного выжидания.
        if _is_or_dead(err) and _OR_BREAKER.record_m3_timeout():
            print(f"[llm-breaker] OpenRouter не отвечает ({str(err)[:80]}) → breaker ВЗВЕДЁН, "
                  "остаток набора на M3", flush=True)
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

        heartbeat_job = _current_llm_heartbeat_job()

        def _call(idx_task):
            idx, (u, msgs, kw) = idx_task
            return idx, _run_with_llm_heartbeat_job(heartbeat_job, _url, u, msgs, **(kw or {}))

        if not tasks:
            return []
        unique_urls = {str((t[0] if t else "") or "").rstrip("/") for t in tasks}
        max_workers = min(4, len(tasks))
        # Один M3-эндпоинт обслуживает запросы ПО ОДНОМУ (`_M3EndpointGuard` держит лок на URL).
        # Веер потоков на него не ускоряет ничего: один работает, остальные ждут лок и через
        # `_M3_ENDPOINT_LOCK_MAX_WAIT` падают с TimeoutError, теряя свою часть контента.
        _m3_is_the_target = m3_is_primary or (m3_is_secondary and _OR_BREAKER.is_tripped())
        if _m3_is_the_target and len(unique_urls) <= 1:
            # 4x14B fan-out is sometimes intentionally disabled, leaving only 72B on 8086.
            # Multiple concurrent prompts to that one mlx endpoint queue long enough to trip
            # the 30s idle/preflight guard, so keep true fan-out only for distinct M3 URLs.
            #
            # ⚠️ Ветка `m3_is_secondary and or_breaker` — живой инцидент 2026-07-28
            # (porg-xjxpfxby, 6 кампаний из 28): OpenRouter был primary и умер (402 + сеть),
            # breaker взвёлся, и весь веер из 4 потоков свалился фолбэком на ОДИН M3. Один
            # проходил, три ждали 90с и умирали по таймауту → `titles=0 texts=0 sitelinks=0`.
            # Пока OpenRouter жив, веер сохраняется: там параллель реально помогает.
            max_workers = 1
        results = [None] * len(tasks)
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = [ex.submit(_call, (i, t)) for i, t in enumerate(tasks)]
            for f in as_completed(futs):
                idx, res = f.result()
                results[idx] = res
        return results

    return _url, _par

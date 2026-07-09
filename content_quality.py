"""Единый контур качества контента: генерация → проверка → регенерация (тот же LLM
через ``_llm_pair_for``) → hard-fail. Flask-free (как остальные ``*_gen``/``spec_audit``).

Три механизма поверх ОДНОГО retry-хелпера :func:`retry_regen`:

* **A. min-длина заголовков** (``>=_MIN_TITLE_LEN``) — вместо механической суффикс-добивки
  теперь перегенерация. :func:`regen_titles` (``need_brand_first=False``).
* **B. марка/модель ДО первой точки** заголовка (brand-first). Тот же :func:`regen_titles`
  c ``need_brand_first=True``.
* **C. LLM-судья УТП-дублей и релевантности** сайту. :func:`judge_utp` +
  :func:`audit_and_regen_utp` (регенерация заголовков и текстов по претензиям судьи).

Общий инвариант: попытка → проверка → повтор → **hard-fail** (``ok=False``) после
``_REGEN_MAX_ATTEMPTS`` попыток. НИКАКОГО тихого суффикс-фолбэка: если LLM так и не дал
валидный набор — возвращаем ``ok=False`` и пусть вызывающая сторона заводит терминальный
issue (``SHORT_TITLES_UNFIXABLE`` / ``BRAND_NOT_FIRST_UNFIXABLE`` / ``UTP_RELEVANCE_FAILED``).

Провайдер (``m3``/``openrouter``) прокидывается снаружи; двусторонний фолбэк —
внутри ``_llm_pair_for`` (падение одного → переключение на другого).
"""
from __future__ import annotations

import json
import re

# 4 попытки = generate + 3 регена. Обоснование (см. отчёт/DOD §2.x): 3 мало для составного
# ограничения (длина+brand-first+без-дублей+кредит), 5 удваивает худшую латентность/стоимость
# на и без того медленном наборе; 4 даёт 2 запасных выстрела при ограниченной цене ~4×
# короткого title-вызова (центы на OpenRouter).
_REGEN_MAX_ATTEMPTS = 4
# ЖЁсткий минимум длины заголовка. Совпадает с audit-порогом (_TITLE_SHORT_LEN=47 → короткий
# ≤47, значит валидный минимум = 48). Держим согласованным с campaign_spec_audit._TITLE_SHORT_LEN.
_MIN_TITLE_LEN = 48


# ── LLM I/O ──────────────────────────────────────────────────────────────────────
def _llm_url_for(provider: str):
    """(complete_url, m3_url, timeout) для провайдера — пара с двусторонним фолбэком."""
    from .llm_providers import _llm_pair_for, _M3_LLM_URLS_14B, _M3_LLM_REPAIR_TIMEOUT
    _url, _ = _llm_pair_for(str(provider or "openrouter"))
    m3url = _M3_LLM_URLS_14B[0] if _M3_LLM_URLS_14B else "http://127.0.0.1:8086"
    return _url, m3url, _M3_LLM_REPAIR_TIMEOUT


def _extract_json_obj(text: str):
    """Достаём первый JSON-объект из ответа LLM (терпимо к ```json обёртке / хвосту)."""
    if not text:
        return None
    s = str(text)
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    frag = m.group(0)
    for cand in (frag, frag[: frag.rfind("}") + 1]):
        try:
            v = json.loads(cand)
            if isinstance(v, dict):
                return v
        except Exception:  # noqa: BLE001
            continue
    return None


def _llm_json(provider: str, messages: list, *, max_tokens: int = 600,
              temperature: float = 0.7):
    """Вызов LLM + извлечение JSON-объекта. Возврат (dict|None, err|None)."""
    _url, m3url, timeout = _llm_url_for(provider)
    text, err = _url(m3url, messages, max_tokens=max_tokens, temperature=temperature,
                     tries=1, timeout=timeout)
    if not text:
        return None, (err or "LLM пустой ответ")
    obj = _extract_json_obj(text)
    if obj is None:
        return None, "LLM: не распарсил JSON"
    return obj, None


# ── общий retry-хелпер ───────────────────────────────────────────────────────────
def retry_regen(*, generate, validate, max_attempts: int = _REGEN_MAX_ATTEMPTS) -> dict:
    """Единый цикл «генерация → проверка → повтор → hard-fail».

    ``generate(attempt:int, last_reason:str) -> candidate|None`` — строит новый кандидат
    (последняя претензия прокидывается в промпт следующей попытки).
    ``validate(candidate) -> (ok:bool, reason:str)`` — приёмка.

    Возврат ``{ok, value, attempts, reason}``. ``ok=False`` = hard-fail (все попытки
    исчерпаны); ``value`` при этом = последний кандидат (для диагностики), НЕ применять.
    """
    last_reason, best = "", None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            cand = generate(attempt, last_reason)
        except Exception as e:  # noqa: BLE001
            last_reason = f"generate упал: {str(e)[:120]}"
            cand = None
        if cand is None:
            last_reason = last_reason or "LLM не вернул кандидата"
            continue
        try:
            ok, reason = validate(cand)
        except Exception as e:  # noqa: BLE001
            ok, reason = False, f"validate упал: {str(e)[:120]}"
        if ok:
            return {"ok": True, "value": cand, "attempts": attempt, "reason": ""}
        if best is None:
            best = cand
        last_reason = reason or last_reason
    return {"ok": False, "value": best, "attempts": max(1, max_attempts), "reason": last_reason}


# ── парсинг/чек-хелперы ──────────────────────────────────────────────────────────
def _parse_titles(obj) -> list:
    if not isinstance(obj, dict):
        return []
    out = []
    for t in (obj.get("titles") or []):
        if isinstance(t, str) and t.strip():
            out.append(t.strip())
        elif isinstance(t, dict):
            for k in ("title", "text", "value"):
                if str(t.get(k) or "").strip():
                    out.append(str(t[k]).strip())
                    break
    return out


def _parse_texts(obj) -> list:
    if not isinstance(obj, dict):
        return []
    out = []
    for t in (obj.get("texts") or []):
        if isinstance(t, str) and t.strip():
            out.append(t.strip())
        elif isinstance(t, dict):
            for k in ("text", "body", "value"):
                if str(t.get(k) or "").strip():
                    out.append(str(t[k]).strip())
                    break
    return out


def brand_head_ok(title: str, brand: str) -> bool:
    """Марка/модель присутствует ДО первой точки заголовка (brand-first, механизм B)."""
    if not brand:
        return True
    from .text_gen import _brand_in_text
    head = str(title or "").split(".")[0]
    return _brand_in_text(head, brand)


# ── A/B: перегенерация заголовков ────────────────────────────────────────────────
def regen_titles(agent: dict, ctx: dict, *, brand: str = "", old_titles=None,
                 n: int | None = None, min_len: int = _MIN_TITLE_LEN,
                 need_brand_first: bool = False, extra_reasons=None,
                 provider: str = "openrouter",
                 max_attempts: int = _REGEN_MAX_ATTEMPTS) -> dict:
    """Перегенерировать набор из ``n`` заголовков, удовлетворяющий: длине
    (``min_len..TITLE_MAX``), опц. brand-first, без смысловых дублей. Тот же LLM (repair-промпт).
    Возврат :func:`retry_regen` (``value`` = list[str] при ``ok``)."""
    from . import ai_agents as A
    from .text_gen import _variant_norm_key
    n = int(n or A.TITLES_N)
    old_titles = list(old_titles or [])
    extra_reasons = [str(r) for r in (extra_reasons or []) if str(r or "").strip()]

    def _reasons(last_reason: str) -> list:
        r = [f"каждый заголовок {min_len}–{A.TITLE_MAX} символов; короче {min_len} — брак"]
        if need_brand_first and brand:
            r.append(f"марка «{brand}» строго в начале, ДО первой точки")
        r.append("без смысловых дублей УТП, все заголовки разные по смыслу")
        r += extra_reasons
        if last_reason:
            r.append(last_reason)
        return r

    def generate(attempt, last_reason):
        msgs = A.build_titles_repair_messages(agent, ctx, old_titles, brand=brand,
                                              reasons=_reasons(last_reason), n=n, min_len=min_len)
        obj, _err = _llm_json(provider, msgs, max_tokens=700, temperature=0.75)
        titles = _parse_titles(obj)[:n] if obj else []
        return titles or None

    def validate(titles):
        good = [t for t in titles if t]
        if len(good) < n:
            return False, f"нужно {n} заголовков, пришло {len(good)}"
        for t in good:
            length = len(t)
            if length < min_len:
                return False, f"«{t}» = {length} симв (<{min_len})"
            if length > A.TITLE_MAX:
                return False, f"«{t}» = {length} симв (>{A.TITLE_MAX})"
            if need_brand_first and not brand_head_ok(t, brand):
                return False, f"«{t}» — марка не в начале (до первой точки)"
        keys = [k for k in (_variant_norm_key(t) for t in good) if k]
        if len(set(keys)) < len(keys):
            return False, "есть смысловые дубли заголовков"
        return True, ""

    return retry_regen(generate=generate, validate=validate, max_attempts=max_attempts)


def _regen_texts(agent: dict, ctx: dict, *, brand: str = "", old_texts=None,
                 issues=None, n: int | None = None, provider: str = "openrouter",
                 max_attempts: int = 1) -> dict:
    """Перегенерация текстов (для механизма C). Приёмка: количество + длина ≤ TEXT_MAX."""
    from . import ai_agents as A
    n = int(n or A.TEXTS_N)
    old_texts = list(old_texts or [])
    issues = [str(x) for x in (issues or []) if str(x or "").strip()]

    def generate(attempt, last_reason):
        reasons = list(issues)
        if last_reason:
            reasons.append(last_reason)
        msgs = A.build_texts_repair_messages(agent, ctx, old_texts, brand=brand,
                                             reasons=reasons, n=n)
        obj, _err = _llm_json(provider, msgs, max_tokens=700, temperature=0.75)
        txts = _parse_texts(obj)[:n] if obj else []
        return txts or None

    def validate(txts):
        good = [t for t in txts if t]
        if len(good) < n:
            return False, f"нужно {n} текстов, пришло {len(good)}"
        for t in good:
            if len(t) > A.TEXT_MAX:
                return False, f"текст {len(t)}>{A.TEXT_MAX}"
        return True, ""

    return retry_regen(generate=generate, validate=validate, max_attempts=max_attempts)


# ── C: LLM-судья УТП-дублей и релевантности ──────────────────────────────────────
def judge_utp(titles, texts, site_ctx: dict, *, provider: str = "openrouter") -> dict:
    """ОДИН LLM-запрос-судья: дубли УТП + релевантность сайту. Возврат
    ``{ok:bool, issues:list, judged:bool, error?}``. При сбое инфраструктуры/парсинга —
    ``judged=False`` (**fail-open**: не роняем набор из-за недоступности судьи)."""
    from . import ai_agents as A
    msgs = A.build_utp_judge_messages(list(titles or []), list(texts or []), site_ctx or {})
    obj, err = _llm_json(provider, msgs, max_tokens=400, temperature=0.2)
    if obj is None:
        return {"ok": True, "issues": [], "judged": False, "error": err}
    ok = bool(obj.get("ok"))
    issues = [str(x) for x in (obj.get("issues") or []) if str(x or "").strip()]
    return {"ok": ok, "issues": issues, "judged": True}


def audit_and_regen_utp(agent: dict, ctx: dict, *, brand: str = "", titles=None, texts=None,
                        site_ctx: dict | None = None, provider: str = "openrouter",
                        max_attempts: int = _REGEN_MAX_ATTEMPTS) -> dict:
    """Механизм C целиком: судья → (если ok=false) регенерация заголовков+текстов по
    претензиям судьи → пере-судейство → до ``max_attempts`` → hard-fail.

    Возврат ``{ok, titles, texts, judged, attempts, issues, hard_fail}``.
    * ``judged=False`` — судья недоступен (fail-open), контент отдан как есть, ``ok=True``.
    * ``ok=False, hard_fail=True`` — судья так и не одобрил после всех попыток
      (вызывающая сторона заводит ``UTP_RELEVANCE_FAILED``). ``titles/texts`` = лучший из
      полученных (для диагностики).
    """
    from . import ai_agents as A
    titles = list(titles or [])
    texts = list(texts or [])
    site_ctx = dict(site_ctx or {})
    site_ctx.setdefault("brand", brand)

    first = judge_utp(titles, texts, site_ctx, provider=provider)
    if not first.get("judged"):
        return {"ok": True, "titles": titles, "texts": texts, "judged": False,
                "attempts": 0, "issues": [], "hard_fail": False, "error": first.get("error")}
    if first.get("ok"):
        return {"ok": True, "titles": titles, "texts": texts, "judged": True,
                "attempts": 0, "issues": [], "hard_fail": False}

    cur_t, cur_x = titles, texts
    issues = first.get("issues") or []
    attempts = 0
    for attempt in range(1, max(1, max_attempts) + 1):
        attempts = attempt
        rt = regen_titles(agent, ctx, brand=brand, old_titles=cur_t,
                          n=len(cur_t) or A.TITLES_N, need_brand_first=bool(brand),
                          extra_reasons=issues, provider=provider, max_attempts=1)
        rx = _regen_texts(agent, ctx, brand=brand, old_texts=cur_x, issues=issues,
                          n=len(cur_x) or A.TEXTS_N, provider=provider, max_attempts=1)
        cur_t = rt.get("value") or cur_t
        cur_x = rx.get("value") or cur_x
        jr = judge_utp(cur_t, cur_x, site_ctx, provider=provider)
        if not jr.get("judged"):
            return {"ok": True, "titles": cur_t, "texts": cur_x, "judged": True,
                    "attempts": attempts, "issues": issues, "hard_fail": False}
        if jr.get("ok"):
            return {"ok": True, "titles": cur_t, "texts": cur_x, "judged": True,
                    "attempts": attempts, "issues": [], "hard_fail": False}
        issues = jr.get("issues") or issues
    return {"ok": False, "titles": cur_t, "texts": cur_x, "judged": True,
            "attempts": attempts, "issues": issues, "hard_fail": True}

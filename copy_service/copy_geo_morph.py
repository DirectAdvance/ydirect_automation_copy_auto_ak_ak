"""copy_geo_morph — морфологически корректная гео-замена для копировщика РК (ФАЗА 3a, п.6).

Проблема, которую чиним: раньше при копировании кампаний старый город/область заменялись
тупым ``str.replace`` ИМЕНИТЕЛЬНОЙ формы → грамматический брак:
    "Краснодара" → "Уфаа",  "в Краснодаре" → "в Уфае".

Решение: один раз за job берём у M3 (LLM-relay нейродиректолога) полную парадигму 6 падежей
для СТАРОГО и НОВОГО города/области, строим пары old[case]→new[case] и заменяем КАЖДУЮ падежную
форму старого на СООТВЕТСТВУЮЩУЮ падежную форму нового — ПО ГРАНИЦАМ СЛОВ (regex ``\\b``, не
подстрокой) и с учётом регистра (Capitalize/lower/UPPER). Тогда "в Краснодаре"→"в Уфе", а не "Уфае".

Модуль ИЗОЛИРОВАН от blueprint (без циклических импортов). M3-клиент инъектируется как callable
``m3_complete(messages) -> (text|None, error|None)`` — тот же контракт, что ``blueprint._m3_complete``.

Фолбэк: M3 недоступен / невалидный JSON → замена ТОЛЬКО по границам слов ИМЕНИТЕЛЬНОЙ формы
(безопаснее старого substring — не создаёт "Уфаа"). Job по этой причине НЕ падает.

Кэш парадигм: в памяти процесса + на диск (JSON), ключ — нормализованное имя. Чтобы не дёргать
LLM на каждый job/строку.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Callable, Optional

CASES = ("nominative", "genitive", "dative", "accusative", "instrumental", "prepositional")


def _noop(_m: str) -> None:
    return None


def _norm(name: str) -> str:
    return (name or "").strip().lower().replace("ё", "е")


# ── Кэш парадигм: память процесса + диск ──────────────────────────────────────
_MEM_LOCK = threading.Lock()
_PARADIGM_MEM: dict[str, dict] = {}
_DISK_LOADED = False
# Персистентная папка на LXC 101 (индекс M3 стабилен, не чистится LRU, не синкается mutagen'ом).
_DISK_CACHE_PATH = os.environ.get("COPY_GEO_MORPH_CACHE") or "/opt/neuro_kontent_index/geo_morph_cache.json"


def _disk_load() -> dict:
    global _DISK_LOADED
    if _DISK_LOADED:
        return _PARADIGM_MEM
    _DISK_LOADED = True
    try:
        p = Path(_DISK_CACHE_PATH)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, dict) and all(v.get(c) for c in CASES):
                        _PARADIGM_MEM.setdefault(k, {c: v[c] for c in CASES})
    except Exception:  # noqa: BLE001 — кэш best-effort, деградируем в память
        pass
    return _PARADIGM_MEM


def _disk_save() -> None:
    try:
        p = Path(_DISK_CACHE_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(_PARADIGM_MEM, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
    except Exception:  # noqa: BLE001 — не пишется диск → работаем из памяти
        pass


# ── Запрос парадигмы у M3 ─────────────────────────────────────────────────────
def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    s = text.strip()
    # снять возможные ```json ... ``` ограждения
    s = re.sub(r"^```(?:json)?", "", s).strip()
    s = re.sub(r"```$", "", s).strip()
    # Ищем первый валидный JSON-объект через raw_decode (не жадный .*).
    # Жадный r"\{.*\}" склеивал бы несколько {...} из ответа M3.
    start = s.find("{")
    if start == -1:
        return None
    try:
        decoder = json.JSONDecoder()
        obj, _ = decoder.raw_decode(s, start)
    except (ValueError, TypeError):
        # фолбэк — оригинальный жадный способ (на случай экзотической структуры)
        m = re.search(r"\{.*\}", s, re.S)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except (ValueError, TypeError):
            return None
    return obj if isinstance(obj, dict) else None


def _extract_roots(name: str) -> list:
    """Корни слов из географического названия для санити-проверки парадигмы.

    Для каждого слова длиной ≥3 берём stem = len(w)-2 символов (минимум 2).
    Это корректно покрывает типичные падежные окончания (1–3 символа) и избегает
    ложного отказа на коротких словах: «уфа»(3) → «уф», «край»(4) → «кр»,
    «тульская»(8) → «тульск», «область»(7) → «облас».
    Корни проверяются как подстроки (после _norm) в каждой сгенерированной словоформе.
    """
    words = re.findall(r"[а-яёa-z]+", _norm(name))
    roots = []
    for w in words:
        if len(w) >= 3:
            root_len = max(2, len(w) - 2)
            roots.append(w[:root_len])
    return roots


def _validate_paradigm(obj: Optional[dict], name: Optional[str] = None) -> Optional[dict]:
    if not isinstance(obj, dict):
        return None
    out: dict[str, str] = {}
    for c in CASES:
        v = obj.get(c)
        if not isinstance(v, str):
            return None
        v = v.strip()
        # словоформа — одно-два слова, без предлогов/пояснений; отсекаем явный мусор
        if not v or len(v) > 60:
            return None
        out[c] = v
    # Корневая проверка: каждая словоформа обязана содержать корень каждого слова исходного названия.
    # Защита от LLM-галлюцинации «тульская → ульская» (потеря буквы в начале слова).
    # Если проверка не проходит — возвращаем None: парадигма отвергается, кэш не пишется,
    # _ask_m3 вернёт None, paradigm_for применит фолбэк (именительная форма без LLM).
    if name:
        roots = _extract_roots(name)
        if roots:
            for form_val in out.values():
                form_norm = _norm(form_val)
                if not all(r in form_norm for r in roots):
                    return None  # хотя бы один корень не нашёлся → отвергаем парадигму
    return out


def _ask_m3(name: str, m3_complete: Callable, log: Callable[[str], None]) -> Optional[dict]:
    system = (
        "Ты — русский лингвист-морфолог. Просклоняй географическое название по 6 падежам. "
        "Отвечай ТОЛЬКО строгим JSON без пояснений, без предлогов, без markdown."
    )
    user = (
        f'Название: "{name}".\n'
        "Верни JSON строго с ключами: "
        "nominative, genitive, dative, accusative, instrumental, prepositional.\n"
        'Значения — только сама словоформа названия в этом падеже, с заглавной буквы, БЕЗ предлога. '
        'prepositional — форма для сочетания "в …"/"о …" (например для "Уфа" это "Уфе", '
        'для "Краснодар" это "Краснодаре"). '
        'genitive — форма для "из …" (для "Уфа" это "Уфы"). '
        'Пример для "Уфа": '
        '{"nominative":"Уфа","genitive":"Уфы","dative":"Уфе","accusative":"Уфу",'
        '"instrumental":"Уфой","prepositional":"Уфе"}'
    )
    try:
        text, err = m3_complete([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])
    except Exception as e:  # noqa: BLE001
        log(f"geo-morph: M3 исключение для {name!r}: {str(e)[:120]}")
        return None
    if err or not text:
        log(f"geo-morph: M3 недоступен для {name!r}: {str(err)[:120]}")
        return None
    para = _validate_paradigm(_extract_json(text), name=name)
    if not para:
        log(f"geo-morph: невалидный JSON парадигмы для {name!r}: {str(text)[:120]}")
        return None
    return para


def paradigm_for(name: str, m3_complete: Optional[Callable], log: Callable[[str], None] = _noop) -> Optional[dict]:
    """Парадигма 6 падежей для названия. Кэш (память+диск). None → M3 недоступен/невалиден."""
    key = _norm(name)
    if not key:
        return None
    with _MEM_LOCK:
        _disk_load()
        cached = _PARADIGM_MEM.get(key)
    if cached:
        return dict(cached)
    if not m3_complete:
        return None
    para = _ask_m3(name, m3_complete, log)
    if not para:
        return None  # негатив НЕ кэшируем — следующий job повторит, когда M3 поднимется
    with _MEM_LOCK:
        _PARADIGM_MEM[key] = para
        _disk_save()
    return dict(para)


# ── Построение пар замены ─────────────────────────────────────────────────────
def _add_pair(pairs: list, seen: set, old: str, new: str) -> None:
    old = (old or "").strip()
    new = (new or "").strip()
    if not old or not new or _norm(old) == _norm(new):
        return
    key = old.lower()
    if key in seen:
        return
    seen.add(key)
    pairs.append((old, new))


def build_geo_pairs(geo_map, m3_complete: Optional[Callable] = None, log: Callable[[str], None] = _noop):
    """geo_map: list[(src_name, dst_name)] — пары гео (город/область), нормальные (им.) формы.

    Возвращает (pairs, meta):
      pairs — list[(old, new)] по всем падежам, отсортировано по длине old убыв.;
      meta  — {m3_used, m3_failed, source_forms (lowercased, для residual), pairs_count}.
    """
    pairs: list = []
    seen: set = set()
    source_forms: set = set()
    m3_used = False
    m3_failed: list = []
    for src_name, dst_name in geo_map:
        src_name = (src_name or "").strip()
        dst_name = (dst_name or "").strip()
        if not src_name or not dst_name or _norm(src_name) == _norm(dst_name):
            continue
        sp = paradigm_for(src_name, m3_complete, log)
        dp = paradigm_for(dst_name, m3_complete, log)
        if sp and dp:
            m3_used = True
            for case in CASES:
                old = (sp.get(case) or "").strip()
                new = (dp.get(case) or "").strip()
                if old and new:
                    _add_pair(pairs, seen, old, new)
                    source_forms.add(old.lower())
        else:
            if m3_complete is not None:
                m3_failed.append(src_name)
            # Фолбэк: только именительная форма, но всё равно по границам слов (см. apply_replacements).
            _add_pair(pairs, seen, src_name, dst_name)
            source_forms.add(src_name.lower())
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    meta = {
        "m3_used": m3_used,
        "m3_failed": m3_failed,
        "source_forms": sorted(source_forms, key=len, reverse=True),
        "pairs_count": len(pairs),
    }
    return pairs, meta


# ── Применение замены: границы слов + сохранение регистра ─────────────────────
def _apply_register(matched: str, new: str) -> str:
    letters = [c for c in matched if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        return new.upper()
    if matched[:1].isupper():
        return new[:1].upper() + new[1:]
    return new.lower()


def apply_replacements(text, pairs) -> tuple[str, int]:
    """Заменяет каждую форму old → new ПО ГРАНИЦАМ СЛОВ, регистр-независимо при поиске,
    но с восстановлением регистра найденного (UPPER/Capitalize/lower). Возвращает (текст, n_замен)."""
    out = str(text or "")
    if not out or not pairs:
        return out, 0
    total = 0
    for old, new in pairs:
        pat = re.compile(r"\b" + re.escape(old) + r"\b", re.IGNORECASE | re.UNICODE)
        counter = {"n": 0}

        def _sub(m, _new=new, _c=counter):
            _c["n"] += 1
            return _apply_register(m.group(0), _new)

        out = pat.sub(_sub, out)
        total += counter["n"]
    return out, total


def scan_residual(paths_texts, source_forms, target_text: str = "", limit: int = 8) -> list:
    """Ищет ОСТАВШИЕСЯ старые гео-формы (любой падеж) в payload'ах после замены.

    paths_texts: list[(name, text)]. source_forms: lowercased формы старого гео.
    target_text: строка нового гео (город+область) — формы, входящие в неё, из residual исключаем.
    """
    tgt = (target_text or "").lower()
    forms = [f for f in dict.fromkeys(source_forms) if f and f not in tgt]
    if not forms:
        return []
    hits: list = []
    for name, text in paths_texts:
        low = (text or "").lower()
        for form in forms:
            if re.search(r"\b" + re.escape(form) + r"\b", low, re.UNICODE):
                hits.append(f"{name}: {form}")
                if len(hits) >= limit:
                    return hits
    return hits

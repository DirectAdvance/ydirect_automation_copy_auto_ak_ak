"""Генерация AI-контента объявлений + слепок-контент (кэш + БД) — вынесено из blueprint.py.

Владеет кэшем сгенерированного контента (_CONTENT_CACHE/_LOCK — единый источник; blueprint
пользуется тем же объектом через ре-экспорт, мутации-словаря шарятся). Инвариант wiring-hub:
НЕ импортирует blueprint. Sibling-модули импортируются напрямую; blueprint-DI — через configure().
"""
from __future__ import annotations

import json
import os
import threading
import time

from .promo_gen import (
    _promo_extract_json, _promo_validate, _promo_ctx,
    _extract_title_candidates, _extract_text_candidates,
)
from .llm_providers import (
    _m3_complete, _llm_pair_for,
    _M3_LLM_URL_72B, _M3_LLM_URLS_14B, _M3_LLM_TIMEOUT_14B, _M3_LLM_REPAIR_TIMEOUT,
    _M3_CONTENT_IDLE_TIMEOUT,
)
from .text_norm import _bad_ad_title, _bad_ad_text, _bad_ad_sitelink
from .city_morph import _content_city, _RU_CITIES
from .text_gen import _variant_norm_key, _display_brand
from .campaign_naming import _title2_blocklist, _brand_from_coder, _brand_ct_from_coder


# ── DI: инъектятся из blueprint (заглушки падают громко, если configure не отработал) ──
def _victory_conn(*a, **k):
    raise RuntimeError("ai_content._victory_conn не инъектирован (configure)")


def _victory_conn_rw(*a, **k):
    raise RuntimeError("ai_content._victory_conn_rw не инъектирован (configure)")


def _gc_ct(*a, **k):
    raise RuntimeError("ai_content._gc_ct не инъектирован (configure)")


def _cached_campaign_content(*a, **k):
    raise RuntimeError("ai_content._cached_campaign_content не инъектирован (configure)")


def configure(deps: dict) -> None:
    """Инъекция blueprint-зависимостей (globals().update)."""
    globals().update(deps)


_CONTENT_CACHE_LOCK = threading.Lock()

_CONTENT_CACHE: dict = {}        # (agent, site_type, city, ct, brand) → generated content

def _content_cache_key(agent_key: str, site_type: str, city: str, item: dict) -> tuple:
    """Ключ контента: один валидный набор переиспользуется для того же st/ct кодера."""
    brand, ct = _brand_ct_from_coder(item)
    if not ct:
        ct = "ct0000"
    return (
        (agent_key or "").strip().lower(),
        (site_type or "").strip(),
        (city or "").strip().lower(),
        _gc_ct(ct),
        (brand or "").strip().lower(),
    )

def _content_complete(content: dict | None) -> bool:
    if not isinstance(content, dict):
        return False
    return (len(content.get("titles") or []) >= 5
            and len(content.get("texts") or []) >= 3
            and len(content.get("sitelinks") or []) >= 8)


# ── Account-level content reuse (в пределах ПРОХОДА процесса-воркера) ──────────────────
# Идея #16 (Семён): каждая марка/модель (ct+brand) генерится ОДИН раз на аккаунт и
# переиспользуется во ВСЕХ наборах/кампаниях этого аккаунта в рамках прохода. Per-набор кэш
# (_generated_content_by_key в orchestrator) сбрасывается каждый набор; этот — переживает
# набор и живёт в памяти процесса (без БД → нет проблемы вечного устаревания).
#
# Ключ = (login, agent, site_type, city, ct, brand) — login даёт account-scoping, agent (слепок)
# гарантирует, что кэш НЕ переживает смену слепка (другой слепок → другой ключ), ct+brand
# сохраняют brand-first (та же марка/модель → тот же контент, чужая марка НЕ подставится).
# Отдельный от _CONTENT_CACHE, т.к. потоковый prefetch зовёт _cached_campaign_content с
# fast_mode=True и _CONTENT_CACHE обходит — эта прослойка ловит ФИНАЛьный контент item на
# точке интеграции orchestrator (667/704), из любого источника (AI/слепок/ручной).
_ACCOUNT_CONTENT_CACHE_LOCK = threading.Lock()
_ACCOUNT_CONTENT_CACHE: dict = {}   # (login, agent, site_type, city, ct, brand) → (content, ts)


def _account_content_reuse_enabled() -> bool:
    """Тумблер (env, дефолт ON). Хук под будущий попап: можно форсить свежую генерацию,
    выставив DIRECT_CONTENT_REUSE_ACCOUNT=0."""
    return os.environ.get("DIRECT_CONTENT_REUSE_ACCOUNT", "1").strip().lower() not in (
        "0", "false", "off", "no", "")


def _account_content_ttl() -> int:
    try:
        return max(1, int(os.environ.get("DIRECT_CONTENT_REUSE_TTL_SEC", "21600")))
    except Exception:  # noqa: BLE001
        return 21600


def _account_content_max() -> int:
    try:
        return max(1, int(os.environ.get("DIRECT_CONTENT_REUSE_MAX", "2000")))
    except Exception:  # noqa: BLE001
        return 2000


def _account_cc(content: dict) -> dict:
    """Глубокая копия (изоляция от мутаций вызывающего)."""
    try:
        return json.loads(json.dumps(content, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        return dict(content or {})


def _account_content_get(login: str, base_key: tuple) -> dict | None:
    """Чтение account-кэша по (login,)+base_key. None → промах / TTL истёк / reuse выключен."""
    if not _account_content_reuse_enabled() or not base_key:
        return None
    ak = (login or "",) + tuple(base_key)
    ttl = _account_content_ttl()
    now = time.time()
    with _ACCOUNT_CONTENT_CACHE_LOCK:
        ent = _ACCOUNT_CONTENT_CACHE.get(ak)
        if not ent:
            return None
        if now - ent[1] > ttl:
            _ACCOUNT_CONTENT_CACHE.pop(ak, None)
            return None
        return _account_cc(ent[0])


def _account_content_put(login: str, base_key: tuple, content: dict | None) -> None:
    """Запись в account-кэш. Кладём ТОЛЬКО полный валидный 5/3/8 (как _CONTENT_CACHE) —
    неполные наборы не переиспользуем на аккаунт. Размер ограничен (FIFO-обрезка)."""
    if not _account_content_reuse_enabled() or not base_key:
        return
    if not _content_complete(content):
        return
    ak = (login or "",) + tuple(base_key)
    with _ACCOUNT_CONTENT_CACHE_LOCK:
        _ACCOUNT_CONTENT_CACHE[ak] = (_account_cc(content), time.time())
        cap = _account_content_max()
        extra = len(_ACCOUNT_CONTENT_CACHE) - cap
        if extra > 0:
            for k in list(_ACCOUNT_CONTENT_CACHE.keys())[:extra]:
                _ACCOUNT_CONTENT_CACHE.pop(k, None)


# ── R2-4 2026-07-10: account-pass reuse ОДНОГО набора сайтлинков (c2) + КАНОН-сумма платежа (d) ──
# Оба живут в памяти процесса на проход воркера (login-scoped, TTL = _account_content_ttl).
# Мотив (Семён, прогон e05fbc86e8ca): tp1 и tp7 имели РАЗНЫЕ быстрые ссылки + разные суммы
# платежа («Платеж от 9 000» vs «Кредит от 12 000 ₽/мес»). Сводим на аккаунт.
_ACCOUNT_SITELINKS_CACHE_LOCK = threading.Lock()
_ACCOUNT_SITELINKS_CACHE: dict = {}   # login → (sitelinks:list[dict], ts)
_ACCOUNT_PAY_CACHE_LOCK = threading.Lock()
_ACCOUNT_PAY_CACHE: dict = {}         # login → (amount_str, ts)


def _account_sitelinks_reuse_enabled() -> bool:
    """Тумблер (c2). ДЕФОЛТ OFF: переиспользование ОДНОГО набора сайтлинков на ВСЕ tp
    (tp1/tp2/tp5/tp6/tp7) — кросс-tp поведение, риск per-item гардов (pct/BU) → opt-in.
    Включается DIRECT_SITELINK_REUSE_ACCOUNT=1."""
    return os.environ.get("DIRECT_SITELINK_REUSE_ACCOUNT", "0").strip().lower() in (
        "1", "true", "on", "yes")


def _account_pay_canon_enabled() -> bool:
    """Тумблер (d). ДЕФОЛТ ON: единая сумма платежа на аккаунт — нормализация чисел через
    уже-доверенный text_gen._apply_payment_amount (та же атомарная замена, что в _coherent_payments,
    но канон общий на проход, а не per-item). Отключается DIRECT_PAY_CANON_ACCOUNT=0."""
    return os.environ.get("DIRECT_PAY_CANON_ACCOUNT", "1").strip().lower() not in (
        "0", "false", "off", "no", "")


def _account_sitelinks_get(login: str) -> list | None:
    """Согласованный набор сайтлинков аккаунта (или None: промах/TTL/выкл)."""
    if not _account_sitelinks_reuse_enabled():
        return None
    ttl = _account_content_ttl()
    now = time.time()
    with _ACCOUNT_SITELINKS_CACHE_LOCK:
        ent = _ACCOUNT_SITELINKS_CACHE.get(login or "")
        if not ent:
            return None
        if now - ent[1] > ttl:
            _ACCOUNT_SITELINKS_CACHE.pop(login or "", None)
            return None
        try:
            return json.loads(json.dumps(ent[0], ensure_ascii=False))
        except Exception:  # noqa: BLE001
            return list(ent[0])


def _account_sitelinks_put(login: str, sitelinks: list) -> None:
    """Сохранить набор сайтлинков как согласованный для аккаунта. Кладём ТОЛЬКО полный (≥8) —
    неполный набор не эталон. Первый валидный побеждает (не перезаписываем)."""
    if not _account_sitelinks_reuse_enabled():
        return
    sl = [s for s in (sitelinks or []) if isinstance(s, dict) and s.get("title")]
    if len(sl) < 8:
        return
    with _ACCOUNT_SITELINKS_CACHE_LOCK:
        if login in _ACCOUNT_SITELINKS_CACHE:
            return
        try:
            _ACCOUNT_SITELINKS_CACHE[login or ""] = (
                json.loads(json.dumps(sl[:8], ensure_ascii=False)), time.time())
        except Exception:  # noqa: BLE001
            _ACCOUNT_SITELINKS_CACHE[login or ""] = (list(sl[:8]), time.time())


def _account_sitelinks_get_or_put(login: str, candidate: list) -> tuple[list | None, bool]:
    """FIX6/#4 (2026-07-10): АТОМАРНО get-or-put под ОДНИМ локом — устраняет гонку параллельных
    каналов (`DIRECT_PARALLEL_CHANNELS=1`). Раньше канал делал get (None) → ГЕНЕРИЛ свой набор →
    put ПОЗЖЕ: каналы стартуют ~одновременно, каждый видел пустой кэш → свой набор → в кабинете
    tp1/tp2/tp5/tp7 получали РАЗНЫЕ inheritableSitelinkSet. Теперь под локом: если эталон уже есть
    (и жив по TTL) — вернуть ЕГО; иначе если `candidate` полон (≥8) — зафиксировать его эталоном и
    вернуть. Первый канал с полным набором сеет эталон, остальные берут ТОТ ЖЕ → один набор на логин.
    Возвращает (набор_для_использования | None, is_account_set:bool). is_account_set=False → эталона
    нет и candidate неполон: каллер остаётся на своём наборе (не ломаем лимиты)."""
    if not _account_sitelinks_reuse_enabled():
        return None, False
    ttl = _account_content_ttl()
    now = time.time()
    with _ACCOUNT_SITELINKS_CACHE_LOCK:
        ent = _ACCOUNT_SITELINKS_CACHE.get(login or "")
        if ent and (now - ent[1]) <= ttl:
            try:
                return json.loads(json.dumps(ent[0], ensure_ascii=False)), True
            except Exception:  # noqa: BLE001
                return list(ent[0]), True
        if ent:                                   # протух — сбрасываем, эталон пересеется ниже
            _ACCOUNT_SITELINKS_CACHE.pop(login or "", None)
        sl = [s for s in (candidate or []) if isinstance(s, dict) and s.get("title")]
        if len(sl) < 8:
            return None, False                    # candidate неполон → эталоном НЕ сеем
        try:
            stored = json.loads(json.dumps(sl[:8], ensure_ascii=False))
        except Exception:  # noqa: BLE001
            stored = list(sl[:8])
        _ACCOUNT_SITELINKS_CACHE[login or ""] = (stored, now)
        try:
            return json.loads(json.dumps(stored, ensure_ascii=False)), True
        except Exception:  # noqa: BLE001
            return list(stored), True


def _account_pay_get(login: str) -> str:
    """Канон-сумма платежа аккаунта (строка «9 000» / «12 000») или '' (промах/TTL/выкл)."""
    if not _account_pay_canon_enabled():
        return ""
    ttl = _account_content_ttl()
    now = time.time()
    with _ACCOUNT_PAY_CACHE_LOCK:
        ent = _ACCOUNT_PAY_CACHE.get(login or "")
        if not ent:
            return ""
        if now - ent[1] > ttl:
            _ACCOUNT_PAY_CACHE.pop(login or "", None)
            return ""
        return str(ent[0] or "")


def _account_pay_put(login: str, amount: str) -> None:
    """Зафиксировать канон-сумму (первая валидная 9-15к побеждает, не перезаписываем)."""
    if not _account_pay_canon_enabled() or not str(amount or "").strip():
        return
    with _ACCOUNT_PAY_CACHE_LOCK:
        if login in _ACCOUNT_PAY_CACHE:
            return
        _ACCOUNT_PAY_CACHE[login or ""] = (str(amount).strip(), time.time())


def _account_pay_unify(login: str, titles: list, texts: list, sitelinks: list) -> tuple[list, list, list]:
    """(d) Свести суммы платежа к ОДНОЙ на аккаунт-проход. Канон = первая валидная сумма
    (9-15к), увиденная в аккаунте (фиксируется), далее применяется ко всем объявлениям.
    No-op при выключенном флаге / отсутствии сумм. Формат/лимиты не трогаем (та же атомарная
    замена text_gen._apply_payment_amount, что уже используется в _coherent_payments)."""
    if not _account_pay_canon_enabled():
        return titles, texts, sitelinks
    try:
        from . import text_gen as _tg
    except Exception:  # noqa: BLE001
        return titles, texts, sitelinks
    canon = _account_pay_get(login)
    if not canon:
        flat = [str(x or "") for x in (titles or [])] + [str(x or "") for x in (texts or [])]
        for s in sitelinks or []:
            if isinstance(s, dict):
                flat += [str(s.get("title") or ""), str(s.get("description") or "")]
        vals = _tg._payment_amounts(flat)
        if vals:
            canon = vals[0]
            _account_pay_put(login, canon)
    if not canon:
        return titles, texts, sitelinks
    nt = [_tg._apply_payment_amount(t, canon) for t in (titles or [])]
    nx = [_tg._apply_payment_amount(x, canon) for x in (texts or [])]
    ns = [{"title": _tg._apply_payment_amount(s.get("title", ""), canon),
           "description": _tg._apply_payment_amount(s.get("description", ""), canon)}
          for s in (sitelinks or []) if isinstance(s, dict)]
    return nt, nx, ns

def _ai_campaign_content_for_item(login: str, slepok: str, site_type: str, city: str,
                                  item: dict, avoid: list | None = None) -> dict | None:
    """AI-first контент для конкретного st/ct; слепок используется внутри _gen_campaign_content как фолбэк."""
    if not (login and slepok and isinstance(item, dict)):
        return None
    city = _content_city(city)                            # мультигород (через запятую) → M3 без города
    try:
        from . import ai_agents as A
        agent_obj = A.get_agent(slepok)
    except Exception:  # noqa: BLE001
        agent_obj = None
    if not agent_obj:
        return None
    return _cached_campaign_content(
        login, agent_obj, (slepok or "").strip().lower(), item,
        site_type, city, avoid=avoid or [],
    )

def _ai_group_content(login: str, slepok: str, site_type: str, city: str,
                      tp_code: str, ct: str, brand: str,
                      avoid: list | None = None) -> dict | None:
    item = {
        "brand": (brand or "").strip(),
        "gc": ct,
        "ct": ct,
        "tp": tp_code,
        "campaign_type": tp_code,
        "type": tp_code,
        "name": brand or ct,
    }
    return _ai_campaign_content_for_item(login, slepok, site_type, city, item, avoid=avoid)

# ── БД-библиотека контента слепков (фолбэк при сбое M3): (слепок × тип сайта × kind) → jsonb ──
# kind='promo' → СПИСОК вариантов промо; kind='campaign' → {titles,texts,sitelinks}.
def _slepok_content_ensure(cur) -> None:
    cur.execute(
        "CREATE TABLE IF NOT EXISTS public.direct_slepok_content ("
        " slepok text NOT NULL, site_type text NOT NULL, kind text NOT NULL,"
        " content jsonb NOT NULL, source text NOT NULL DEFAULT 'slepok',"
        " updated_at timestamptz DEFAULT now(),"
        " PRIMARY KEY (slepok, site_type, kind))")

def _slepok_content_get(slepok: str, site_type: str, kind: str):
    """Контент из БД-библиотеки слепков. → list/dict или None (нет записи / БД недоступна).
    NB: используем readonly-коннекшен (read-only), поэтому _slepok_content_ensure пропускаем —
    CREATE TABLE на readonly упадёт с ReadOnlySqlTransaction и скроет результат."""
    if not (slepok and site_type):
        return None
    try:
        conn = _victory_conn()
    except Exception:  # noqa: BLE001
        return None
    raw = None
    try:
        cur = conn.cursor()
        # NB: НЕ зовём _slepok_content_ensure здесь — она делает CREATE TABLE IF NOT EXISTS,
        # что падает на readonly-коннекшене и глушит весь запрос.
        cur.execute("SELECT content FROM public.direct_slepok_content "
                    "WHERE slepok=%s AND site_type=%s AND kind=%s", (slepok, site_type, kind))
        row = cur.fetchone()
        raw = row[0] if row else None
    except Exception:  # noqa: BLE001
        raw = None
    finally:
        conn.close()
    # Нормализация: legacy-засев мог сохранить sitelinks как список строк (не dict).
    # Приводим к {title, description}, чтобы isinstance(s,dict)-фильтры в _common_sitelinks_fast
    # и аналогичных читалках не обнуляли набор → кампании не получали авто-ассеты аккаунта.
    if isinstance(raw, dict) and isinstance(raw.get("sitelinks"), list):
        sl_list = raw["sitelinks"]
        if any(not isinstance(s, dict) for s in sl_list):
            raw = dict(raw)
            raw["sitelinks"] = [
                s if isinstance(s, dict) else {"title": str(s), "description": ""}
                for s in sl_list if s
            ]
    return raw

def _slepok_content_save(slepok: str, site_type: str, kind: str, content, source: str = "slepok") -> bool:
    try:
        conn = _victory_conn_rw()
    except Exception:  # noqa: BLE001
        return False
    try:
        cur = conn.cursor()
        _slepok_content_ensure(cur)
        cur.execute(
            "INSERT INTO public.direct_slepok_content(slepok, site_type, kind, content, source, updated_at) "
            "VALUES (%s, %s, %s, %s::jsonb, %s, now()) "
            "ON CONFLICT (slepok, site_type, kind) DO UPDATE SET "
            "content = EXCLUDED.content, source = EXCLUDED.source, updated_at = now()",
            (slepok, site_type, kind, json.dumps(content, ensure_ascii=False), source))
        conn.commit()
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        conn.close()

def _gen_campaign_content(login: str, agent: dict, agent_key: str, item: dict,
                          avoid: list | None = None, ctx_override: dict | None = None,
                          fast_mode: bool = False) -> dict:
    """Ядро генерации контента ОДНОЙ РК (M3 fan-out 14B×3 + 72B-патч + фолбэк слепка). БЕЗ HTTP —
    зовётся и эндпоинтом api_ai_campaign_generate, и потоково из create_set (контент 1 РК → создание 1 РК).
    Тонкая обёртка: тело вынесено в `create_content.run_gen_campaign_content` (DI-модуль).
    → {ok, agent, login, item, brand, content:{titles,texts,sitelinks,title2?}, warnings, fallback}
      | {ok:False, error}."""
    from .create_content import run_gen_campaign_content
    # Провайдер из попапа создания (body.llm_provider → item.llm_provider): пара функций
    # с двусторонним фолбэком — «падение одного — переключение на другого» (Семён 03.07).
    # Дефолт БЕЗ провайдера → openrouter (не M3): вспомогательные ген-ы (сайтлинки/группы) без
    # явного провайдера уходили на перегруженный M3 и ЗАВИСАЛИ (#7). M3 остаётся фолбэком.
    _llm_url, _llm_par = _llm_pair_for(str((item or {}).get("llm_provider") or "openrouter"))
    return run_gen_campaign_content(
        login=login, agent=agent, agent_key=agent_key, item=item,
        avoid=avoid, ctx_override=ctx_override, fast_mode=fast_mode,
        _bad_ad_sitelink=_bad_ad_sitelink, _bad_ad_text=_bad_ad_text, _bad_ad_title=_bad_ad_title,
        _brand_from_coder=_brand_from_coder, _display_brand=_display_brand,
        _extract_text_candidates=_extract_text_candidates,
        _extract_title_candidates=_extract_title_candidates,
        _m3_complete_parallel=_llm_par, _m3_complete_url=_llm_url,
        _promo_ctx=_promo_ctx, _promo_extract_json=_promo_extract_json,
        _slepok_content_get=_slepok_content_get, _title2_blocklist=_title2_blocklist,
        _variant_norm_key=_variant_norm_key,
        _M3_LLM_REPAIR_TIMEOUT=_M3_LLM_REPAIR_TIMEOUT, _M3_LLM_TIMEOUT_14B=_M3_LLM_TIMEOUT_14B,
        _M3_CONTENT_IDLE_TIMEOUT=_M3_CONTENT_IDLE_TIMEOUT,
        _M3_LLM_URLS_14B=_M3_LLM_URLS_14B, _M3_LLM_URL_72B=_M3_LLM_URL_72B, _RU_CITIES=_RU_CITIES,
    )

def _seed_slepok_content(only_missing: bool = True, m3_timeout: float = 45.0) -> dict:
    """Засев БД-библиотеки слепков `direct_slepok_content`: на каждый (слепок × тип сайта из
    site_fit) ПРОБУЕМ M3, при неудаче берём из корпуса слепка. kind='promo' (список вариантов)
    и kind='campaign' ({titles,texts,sitelinks}). only_missing — пропускать уже заполненные.
    Request-free: вызывается из скрипта/эндпоинта. Возвращает отчёт по источникам."""
    from . import ai_agents as A
    # ВСЕ типы сайта (а не только site_fit слепка) — чтобы по КАЖДОМУ слепку был контент на любой
    # тип сайта (фолбэк сработает, какой бы слепок×тип пользователь ни выбрал; слепок адаптируется).
    site_types = list(A.SITE_TYPE_PROFILE.keys())
    # нейтральные осмысленные описания промо под тип сайта — на случай НЕ-родного слепку типа
    _neutral = {"С пробегом": ["на авто с пробегом", "на проверенные авто", "за автокредит"],
                "Мульти + БУ": ["на авто в наличии", "за автокредит", "при покупке в кредит"]}
    _neutral_new = ["на новые авто", "при покупке в кредит", "по госпрограмме"]
    rep = {"combos": 0, "promo": {"m3": 0, "slepok": 0, "skip": 0},
           "campaign": {"m3": 0, "slepok": 0, "skip": 0}}
    for a in A.agent_list():
        key = a["key"]
        agent = A.get_agent(key)
        if not agent:
            continue
        p = agent["promo"]
        for st in site_types:
            rep["combos"] += 1
            ctx = {"site_type": st, "domain": "", "salon": "", "city": ""}
            # ── PROMO: набор вариантов (M3 по разным типам + примеры слепка + нейтральные) ──
            if only_missing and _slepok_content_get(key, st, "promo"):
                rep["promo"]["skip"] += 1
            else:
                variants, src, seen = [], "slepok", set()
                for ft in (p["type"], "PROFIT", "GIFT"):
                    msgs = A.build_promo_messages(agent, ctx, force_type=ft)
                    text, err = _m3_complete(msgs, max_tokens=300, temperature=0.95,
                                             tries=1, timeout=m3_timeout)
                    raw = _promo_extract_json(text) if not err else {}
                    if raw:
                        pr, _ = _promo_validate(raw, agent, site_type=st)
                        if ft in A.PROMO_TYPES:
                            pr["type"] = ft
                            if ft == "GIFT":
                                pr["unit"] = "RUB"
                        k = (pr.get("description") or "").strip().lower()
                        if k and k not in seen:
                            seen.add(k); variants.append(pr); src = "m3"
                # добиваем примерами из корпуса слепка + нейтральными под тип сайта (для НЕ-родных комбо)
                ex_pool = list(p.get("examples") or []) + _neutral.get(st, _neutral_new)
                for ex in ex_pool:
                    if A.is_bu_site_type(st) and A._bad_for_bu(ex):
                        continue   # на б/у-сайте новоавтомобильные примеры слепка неуместны
                    pr, _ = _promo_validate({"type": p["type"], "amount": None, "unit": p["unit"],
                                             "prefix": p.get("prefix"), "description": ex},
                                            agent, site_type=st)
                    k = (pr.get("description") or "").strip().lower()
                    if k and k not in seen:
                        seen.add(k); variants.append(pr)
                if variants:
                    _slepok_content_save(key, st, "promo", variants[:8], src)
                    rep["promo"][src] += 1
            # ── CAMPAIGN: один полный комплект (заголовки/тексты/ссылки) ──
            if only_missing and _slepok_content_get(key, st, "campaign"):
                rep["campaign"]["skip"] += 1
            else:
                msgs = A.build_campaign_messages(agent, ctx, item={})
                text, err = _m3_complete(msgs, max_tokens=800, temperature=0.8,
                                         top_p=0.9, repetition_penalty=1.15, tries=1, timeout=m3_timeout)
                raw = _promo_extract_json(text) if not err else {}
                if raw:
                    content, _ = A.validate_campaign(raw, agent, site_type=st)
                    content, _ = A.assemble_campaign(content["titles"], content["texts"],
                                                     content["sitelinks"], agent, site_type=st, brand="")
                    csrc = "m3"
                else:
                    content, _ = A.assemble_campaign([], [], [], agent, site_type=st, brand="")
                    csrc = "slepok"
                _slepok_content_save(key, st, "campaign", content, csrc)
                rep["campaign"][csrc] += 1
    # Агенты с нестандартными site_type (вне SITE_TYPE_PROFILE, пример: slepok=dmp → site_type="dmp"):
    # основной цикл выше их не охватывает → в БД могли остаться строки вместо dict-сайтлинков.
    # assemble_campaign(site_type=st) корректно тянет B2B-пул: sitelink_bank_for(st) +
    # AGENT_ADS[key]["sitelinks"] — и сохраняет готовые {title,description}-dict-сайтлинки.
    # Запуск: python3 -m direct.seed_slepok_content --all  (ключ --all нужен чтобы перезаписать
    # существующую запись с плохими строками; без него only_missing=True пропустит).
    for a in A.agent_list():
        key = a["key"]
        agent = A.get_agent(key)
        if not agent:
            continue
        extra_sts = [st for st in (agent.get("site_fit") or []) if st not in site_types]
        for st in extra_sts:
            rep["combos"] += 1
            if only_missing and _slepok_content_get(key, st, "campaign"):
                rep["campaign"]["skip"] += 1
                continue
            content, _ = A.assemble_campaign([], [], [], agent, site_type=st, brand="")
            _slepok_content_save(key, st, "campaign", content, "slepok")
            rep["campaign"]["slepok"] += 1
    return rep

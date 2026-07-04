"""Генерация AI-контента объявлений + слепок-контент (кэш + БД) — вынесено из blueprint.py.

Владеет кэшем сгенерированного контента (_CONTENT_CACHE/_LOCK — единый источник; blueprint
пользуется тем же объектом через ре-экспорт, мутации-словаря шарятся). Инвариант wiring-hub:
НЕ импортирует blueprint. Sibling-модули импортируются напрямую; blueprint-DI — через configure().
"""
from __future__ import annotations

import json
import threading

from .promo_gen import (
    _promo_extract_json, _promo_validate, _promo_ctx,
    _extract_title_candidates, _extract_text_candidates,
)
from .llm_providers import (
    _m3_complete, _llm_pair_for,
    _M3_LLM_URL_72B, _M3_LLM_URLS_14B, _M3_LLM_TIMEOUT_14B, _M3_LLM_REPAIR_TIMEOUT,
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
    try:
        cur = conn.cursor()
        # NB: НЕ зовём _slepok_content_ensure здесь — она делает CREATE TABLE IF NOT EXISTS,
        # что падает на readonly-коннекшене и глушит весь запрос.
        cur.execute("SELECT content FROM public.direct_slepok_content "
                    "WHERE slepok=%s AND site_type=%s AND kind=%s", (slepok, site_type, kind))
        row = cur.fetchone()
        return row[0] if row else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        conn.close()

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
    return rep

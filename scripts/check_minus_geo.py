#!/usr/bin/env python3
"""check_minus_geo.py — убедиться, что собственный город аккаунта не попал в его минус-слова.

Читает минус-слова из трёх источников:
  1. Глобальный список (таблица direct_global_minus_words, enabled=true)
  2. Слепок-пак M3 (pack minus для заданного slepok/site_type/tp)
  3. Опционально — live shared-set аккаунта (negativekeywordsharedsets через v5 API с токеном)

Выводит: какие фразы содержат город/регион и из какого источника.
Завершение: exit 0 = PASS (ничего не нашлось), exit 1 = FAIL (есть совпадения).

Использование:
  python scripts/check_minus_geo.py --city Волгоград [--region "Волгоградская область"]
  python scripts/check_minus_geo.py --city Самара --slepok pavlov --site-type avto --tp tp2
  python scripts/check_minus_geo.py --city Краснодар --login yd-dealer123 --token TOKEN
  python scripts/check_minus_geo.py --words "волгоград купить,б/у волгоград,отзывы" --city Волгоград
"""
from __future__ import annotations

import argparse
import sys
import os

# Добавляем корень проекта в путь (direct/ — подпапка seoadvanced)
_HERE = os.path.dirname(os.path.abspath(__file__))
_DIRECT = os.path.dirname(_HERE)                     # home/seoadvanced/direct
_SEO = os.path.dirname(_DIRECT)                      # home/seoadvanced
_HOME = os.path.dirname(_SEO)                        # home
_ROOT = os.path.dirname(_HOME)                       # HomeServer_PythonProject
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _load_global_minus() -> list[str]:
    """Загрузить глобальные минус-слова из БД (direct_global_minus_words, enabled=true)."""
    try:
        from home.seoadvanced.direct.automation_runtime import _enabled_minus_words
        return _enabled_minus_words() or []
    except Exception as e:
        print(f"  [warn] global minus: не удалось загрузить из БД — {e}")
        return []


def _load_pack_minus(slepok: str, site_type: str, tp_code: str) -> list[str]:
    """Загрузить минус-слова слепка из пака M3."""
    if not slepok:
        return []
    try:
        # Для _collect_pack_minus нужны deps (kp, _SLEPOK_KEY) — используем модуль через runtime.
        from home.seoadvanced.direct.automation_runtime import _create_set_minus_module
        mod = _create_set_minus_module()
        return mod._collect_pack_minus(slepok, site_type, tp_code) or []
    except Exception as e:
        print(f"  [warn] pack minus: не удалось загрузить для slepok={slepok!r} — {e}")
        return []


def _load_shared_set_minus(login: str, token: str) -> list[str]:
    """Загрузить фразы из shared-set «Минуса общие» через v5 API."""
    if not login or not token:
        return []
    try:
        from home.seoadvanced.direct.automation_runtime import _v5_get
        jm = _v5_get("negativekeywordsharedsets", token, login, ["Id", "Name"])
        sets = (jm.get("result") or {}).get("NegativeKeywordSharedSets", [])
        if not sets:
            print("  [info] shared-sets: в аккаунте нет библиотечных наборов")
            return []
        words: list[str] = []
        # Детализируем каждый набор (по id)
        for s in sets:
            sid = s.get("Id")
            if not sid:
                continue
            jd = _v5_get("negativekeywordsharedsets", token, login,
                          ["Id", "Name", "NegativeKeywords"], selection={"Ids": [int(sid)]})
            kws = ((jd.get("result") or {}).get("NegativeKeywordSharedSets") or [{}])[0].get("NegativeKeywords") or []
            words.extend(str(w) for w in kws if w)
        return words
    except Exception as e:
        print(f"  [warn] shared-set: не удалось загрузить для login={login!r} — {e}")
        return []


def _check_source(label: str, words: list[str], city: str, region: str) -> list[tuple[str, str]]:
    """Проверить список words на совпадение с city/region.

    Возвращает список (фраза, причина) для каждого совпадения.
    Логика зеркалит _strip_account_geo из create_set_minus:
    • Длинные города (root ≥ 5) — prefix-stem matching.
    • Короткие города (root < 5) — whole-token equality по набору падежных форм
      (_city_geo_declensions), чтобы «уфе/уфы» ловились, а «уфавтодор» — нет.
    """
    from home.seoadvanced.direct.create_set_minus import (
        _city_geo_stems, _city_geo_declensions, _region_geo_stems, _OP_STRIP_CHARS,
    )
    all_stems: list[str] = []
    all_exact: set[str] = set()

    for part in city.split(","):
        p = part.strip()
        if not p:
            continue
        for s in _city_geo_stems(p):
            if s and s not in all_stems:
                all_stems.append(s)
        forms = _city_geo_declensions(p)
        if forms:
            all_exact.update(forms)
    for s in _region_geo_stems(region):
        if s and s not in all_stems:
            all_stems.append(s)

    if not all_stems and not all_exact:
        return []

    hits: list[tuple[str, str]] = []
    for phrase in words:
        for tok in phrase.lower().split():
            core = tok.strip(_OP_STRIP_CHARS).lower()
            if not core:
                continue
            matched = False
            if core in all_exact:
                hits.append((phrase, f"токен «{core}» = точная форма города [{label}]"))
                matched = True
            if not matched:
                for stem in all_stems:
                    if core.startswith(stem):
                        hits.append((phrase, f"токен «{core}» ∈ стем «{stem}» [{label}]"))
                        matched = True
                        break
            if matched:
                break
    return hits


def main() -> int:
    p = argparse.ArgumentParser(
        description="Проверить, не попал ли собственный город аккаунта в его минус-слова."
    )
    p.add_argument("--city", required=True, help="Город аккаунта (напр. Волгоград). "
                   "Мультигород: «Самара, Тольятти».")
    p.add_argument("--region", default="", help="Область (напр. «Волгоградская область»), опц.")
    p.add_argument("--slepok", default="", help="Slug слепка для чтения pack minus (напр. pavlov).")
    p.add_argument("--site-type", default="avto", help="Тип сайта (avto/moto/…), дефолт avto.")
    p.add_argument("--tp", default="tp2", help="Тип кампании для pack minus (tp1..tp7), дефолт tp2.")
    p.add_argument("--login", default="", help="Логин Яндекс.Директа для чтения shared-set.")
    p.add_argument("--token", default="", help="OAuth токен для v5 API (нужен для --login).")
    p.add_argument("--words", default="", help="Ручной список для проверки, разделённый запятыми.")
    p.add_argument("--no-global", action="store_true", help="Пропустить глобальные минус-слова из БД.")
    p.add_argument("--no-pack", action="store_true", help="Пропустить pack minus из M3.")
    args = p.parse_args()

    city = args.city.strip()
    region = args.region.strip()
    print(f"\n=== check_minus_geo  city={city!r}  region={region!r} ===\n")

    from home.seoadvanced.direct.create_set_minus import (
        _city_geo_stems, _city_geo_declensions, _region_geo_stems,
    )
    stems: list[str] = []
    exact_forms: set[str] = set()
    for part in city.split(","):
        p = part.strip()
        if not p:
            continue
        for s in _city_geo_stems(p):
            if s not in stems:
                stems.append(s)
        forms = _city_geo_declensions(p)
        if forms:
            exact_forms.update(forms)
    for s in _region_geo_stems(region):
        if s not in stems:
            stems.append(s)
    print(f"Стемы (prefix-match, длинные города): {stems}")
    if exact_forms:
        print(f"Точные формы (whole-token, короткие города): {sorted(exact_forms)}")
    print()

    all_hits: list[tuple[str, str]] = []

    # 1. Ручной список
    if args.words:
        manual = [w.strip() for w in args.words.split(",") if w.strip()]
        h = _check_source("ручной список", manual, city, region)
        all_hits.extend(h)
        print(f"[ручной список]  {len(manual)} фраз → {len(h)} совпадений")

    # 2. Глобальные минус-слова (БД)
    if not args.no_global:
        gm = _load_global_minus()
        h = _check_source("global", gm, city, region)
        all_hits.extend(h)
        print(f"[global DB]      {len(gm)} фраз → {len(h)} совпадений")

    # 3. Pack minus (M3)
    if not args.no_pack and args.slepok:
        pm = _load_pack_minus(args.slepok, args.site_type, args.tp)
        h = _check_source("pack", pm, city, region)
        all_hits.extend(h)
        print(f"[pack M3]        {len(pm)} фраз → {len(h)} совпадений")
    elif not args.no_pack and not args.slepok:
        print("[pack M3]        пропущен (укажите --slepok)")

    # 4. Live shared-set
    if args.login and args.token:
        sm = _load_shared_set_minus(args.login, args.token)
        h = _check_source("shared-set", sm, city, region)
        all_hits.extend(h)
        print(f"[shared-set v5]  {len(sm)} фраз → {len(h)} совпадений")
    else:
        print("[shared-set v5]  пропущен (укажите --login и --token)")

    print()
    if all_hits:
        print(f"FAIL — найдено {len(all_hits)} фраз с собственным гео аккаунта:\n")
        for phrase, reason in all_hits:
            print(f"  LEAK  {phrase!r}  ({reason})")
        print()
        return 1
    else:
        print("PASS — ни одна минус-фраза не содержит собственный город/регион аккаунта.")
        return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""tone_baseline.py — офлайн-базовая-линия тон-войс для всех директолог-слепков.

Для каждого (slepok, site_type):
1. Генерирует тестовый контент двумя способами (по флагу --mode):
   * corpus  — берёт AGENT_ADS[slepok] из ai_agents.py (быстро, без LLM)
   * generate — вызывает build_campaign_messages + OpenRouter (реальный промпт)
2. Оценивает score_offline() (тот же LLM-судья, что и у live-пути).
3. Выводит таблицу: slepok | site_type | voice_score | verdict | has_corpus.

Использование:
    # Только корпус (быстро, без LLM-генерации):
    python3 -m direct.tools.tone_baseline --mode corpus

    # С реальной генерацией через OpenRouter (медленнее, ≈2–3 мин на все слепки):
    python3 -m direct.tools.tone_baseline --mode generate

    # Один слепок:
    python3 -m direct.tools.tone_baseline --slepok kryuchkova --mode generate

    # На LXC101:
    /root/venv/bin/python3 -m direct.tools.tone_baseline --mode generate

Результат: таблица + JSON-файл _tone_baseline_result.json (в cwd).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

_THIS = Path(__file__).resolve()
_PKG_ROOT = _THIS.parents[2]          # .../home/seoadvanced
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))
for _p in [_PKG_ROOT, *_PKG_ROOT.parents]:
    if (_p / ".secret" / "loader.py").exists():
        if str(_p / ".secret") not in sys.path:
            sys.path.insert(0, str(_p / ".secret"))
        break

# ── Целевые слепки и их primary site_type ────────────────────────────────────────
# feed-слепки (avto_sk / sk_krs / avtolajt_bu) отмечены как feed_only — LLM tone
# применим минимально (заголовки из фида), оцениваем тексты из AGENT_ADS.
SLEPOK_SITE_TYPES: list[tuple[str, str]] = [
    # directologist slepki (tp1-tp5 с реальной LLM-генерацией)
    ("kryuchkova",  "Монобренд"),
    ("kryuchkova",  "С пробегом"),
    ("pavlov",      "Мультибренд"),
    ("karavaev",    "Монобренд"),
    ("karavaev",    "С пробегом"),
    ("gordeeva",    "Мультибренд"),
    ("gordeeva",    "С пробегом"),
    ("salamahin",   "Мультибренд"),
    ("salamahin",   "С пробегом"),
    ("zubakin",     "Монобренд"),
    ("chepelev",    "Мультибренд"),
    ("tumashenko",  "Мультибренд"),
    ("tumashenko",  "С пробегом"),
    ("kuderko",     "С пробегом"),
    ("piterkina",   "Монобренд"),
    ("terehov",     "С пробегом"),
    ("scherbakova", "Мультибренд"),
    # feed/minimal-LLM slepki (отмечены ниже в FEED_SLEPKI)
    ("avtolajt_bu", "С пробегом"),
    ("avto_sk",     "С пробегом"),
    ("sk_krs",      "С пробегом"),
]
# Слепки, где LLM-генерация заголовков минимальна (фид-кампании) — оцениваем только тексты
FEED_SLEPKI = {"avto_sk", "sk_krs"}

THRESHOLD = 60   # порог «зелёного» (совпадает с check_tone_of_voice.THRESHOLD)
ALERT_THRESHOLD = 50  # ниже — требует фикса


def _log(msg: str) -> None:
    print(f"[tone-baseline] {msg}", flush=True)


def _extract_json_block(text: str) -> dict | None:
    """Вытащить JSON из LLM-ответа (с возможными markdown-оградами)."""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:  # noqa: BLE001
                pass
    return None


def generate_via_openrouter(slepok: str, site_type: str) -> dict:
    """Сгенерировать тестовый контент через build_campaign_messages + OpenRouter.
    Возвращает {"titles": [...], "texts": [...], "sitelinks": [...], "ok": bool, "error": str|None}.
    """
    from direct import ai_agents as A
    from direct import llm_providers as LP

    agent = A.AGENTS.get(slepok)
    if not agent:
        return {"titles": [], "texts": [], "sitelinks": [], "ok": False,
                "error": f"no agent profile for slepok={slepok!r}"}

    ctx = {"site_type": site_type, "city": "", "domain": "example.ru",
           "salon": f"Автосалон ({slepok})"}
    item = {"name": "Общее", "type": "manual", "variant": "search_manual"}
    brand = ""   # нет конкретной марки — общее объявление

    messages = A.build_campaign_messages(agent, ctx, item=item, brand=brand)

    # max_tokens: 1200 обрезал ответ на середине JSON → _extract_json_block падал → тихий
    # фолбэк на corpus, и судья оценивал эталон сам против себя (тавтологические 100/100).
    content, err = LP._or_complete_url("", messages, max_tokens=4000, temperature=0.7, tries=2)
    if err or not content:
        return {"titles": [], "texts": [], "sitelinks": [], "ok": False,
                "error": err or "пустой ответ"}
    parsed = _extract_json_block(content)
    if not parsed:
        return {"titles": [], "texts": [], "sitelinks": [], "ok": False,
                "error": f"LLM вернул не-JSON: {str(content)[:200]}"}

    titles = [str(t).strip() for t in (parsed.get("titles") or []) if t]
    texts = [str(t).strip() for t in (parsed.get("texts") or []) if t]
    sitelinks = parsed.get("sitelinks") or []
    return {"titles": titles, "texts": texts, "sitelinks": sitelinks,
            "ok": bool(titles or texts), "error": None}


def corpus_content(slepok: str, site_type: str) -> dict:
    """Взять контент из AGENT_ADS (без LLM-вызова). Фильтрует по типу сайта."""
    from direct import ai_agents as A

    ads = (A.AGENT_ADS.get(slepok) or {})
    bu = A.is_bu_site_type(site_type)
    new_only = site_type in A.NEW_ONLY_SITE_TYPES

    titles = [t for t in (ads.get("titles") or [])
              if not (bu and A._bad_for_bu(t)) and not (new_only and A._bad_for_new(t))]
    texts = [t for t in (ads.get("texts") or [])
             if not (bu and A._bad_for_bu(t)) and not (new_only and A._bad_for_new(t))]
    sitelinks = [(tt, dd) for tt, dd in (ads.get("sitelinks") or [])
                 if not (bu and (A._bad_for_bu(tt) or A._bad_for_bu(dd)))
                 and not (new_only and (A._bad_for_new(tt) or A._bad_for_new(dd)))]

    return {"titles": titles, "texts": texts, "sitelinks": sitelinks,
            "ok": bool(titles or texts), "error": None}


def run_baseline(slepok_site_types: list[tuple[str, str]], *,
                 mode: str = "corpus") -> list[dict]:
    """Прогнать базовую линию для набора пар (slepok, site_type).

    mode: 'corpus' — только AGENT_ADS, 'generate' — LLM-генерация + corpus как fallback.
    """
    from direct.tools.check_tone_of_voice import score_offline, build_voice_reference

    results: list[dict] = []

    for slepok, site_type in slepok_site_types:
        _log(f"--- {slepok} / {site_type} (mode={mode}) ---")

        # ── 1. Получить контент ─────────────────────────────────────────────────
        content_source = "corpus"
        if mode == "generate" and slepok not in FEED_SLEPKI:
            _log("  генерация через OpenRouter...")
            gen = generate_via_openrouter(slepok, site_type)
            if gen["ok"]:
                titles = gen["titles"]
                texts = gen["texts"]
                content_source = "generated"
                _log(f"  → {len(titles)} заголовков, {len(texts)} текстов (generated)")
            else:
                _log(f"  генерация не удалась: {gen['error']} — фолбэк на corpus")
                c = corpus_content(slepok, site_type)
                titles = c["titles"]
                texts = c["texts"]
        else:
            c = corpus_content(slepok, site_type)
            titles = c["titles"]
            texts = c["texts"]

        if not titles and not texts:
            _log("  контент пуст (feed-слепок или пустой corpus) — score=N/A")
            results.append({
                "slepok": slepok, "site_type": site_type,
                "voice_score": None, "verdict": "no_content",
                "has_corpus": False, "content_source": content_source,
                "n_titles": 0, "n_texts": 0,
                "generic_examples": [], "in_voice_examples": [], "note": "нет контента",
            })
            continue

        # ── 2. Голос-референс ─────────────────────────────────────────────────
        vref = build_voice_reference(slepok, site_type)
        has_corpus = vref.get("has_corpus", False)

        # ── 3. Оценить ─────────────────────────────────────────────────────────
        _log(f"  оцениваем {len(titles)} загол + {len(texts)} текстов ...")
        verdict = score_offline(slepok, site_type, titles, texts)
        if verdict.get("error"):
            _log(f"  судья вернул ошибку: {verdict['error']}")
            results.append({
                "slepok": slepok, "site_type": site_type,
                "voice_score": None, "verdict": "judge_error",
                "has_corpus": has_corpus, "content_source": content_source,
                "n_titles": len(titles), "n_texts": len(texts),
                "generic_examples": [], "in_voice_examples": [],
                "note": verdict["error"],
            })
            time.sleep(1)
            continue

        score = verdict.get("voice_score", 0)
        v = verdict.get("verdict", "?")
        icon = "✅" if score >= THRESHOLD else ("⚠️" if score >= ALERT_THRESHOLD else "❌")
        _log(f"  {icon} voice_score={score}/100 verdict={v}")

        results.append({
            "slepok": slepok, "site_type": site_type,
            "voice_score": score, "verdict": v,
            "has_corpus": has_corpus, "content_source": content_source,
            "n_titles": len(titles), "n_texts": len(texts),
            "generic_examples": verdict.get("generic_examples") or [],
            "in_voice_examples": verdict.get("in_voice_examples") or [],
            "note": verdict.get("note") or "",
        })
        # Пауза между вызовами судьи (OpenRouter rate-limit)
        time.sleep(2)

    return results


def print_table(results: list[dict]) -> None:
    """Вывести таблицу результатов."""
    print("\n" + "=" * 90)
    print(f"{'Слепок':<18} {'site_type':<16} {'score':>6} {'verdict':<12} "
          f"{'corpus':>7} {'тайтлы':>7} {'тексты':>7} {'src':>10}")
    print("-" * 90)
    below50 = []
    for r in results:
        score_s = str(r["voice_score"]) if r["voice_score"] is not None else "—"
        icon = ("✅" if (r["voice_score"] or 0) >= THRESHOLD
                else ("⚠️" if (r["voice_score"] or 0) >= ALERT_THRESHOLD
                      else ("—" if r["voice_score"] is None else "❌")))
        print(f"{r['slepok']:<18} {r['site_type']:<16} {score_s:>5}{icon} "
              f"{r['verdict']:<12} {'да' if r['has_corpus'] else 'нет':>7} "
              f"{r['n_titles']:>7} {r['n_texts']:>7} {r['content_source']:>10}")
        if r["voice_score"] is not None and r["voice_score"] < ALERT_THRESHOLD:
            below50.append(r)

    print("=" * 90)
    total = len([r for r in results if r["voice_score"] is not None])
    green = len([r for r in results if (r["voice_score"] or 0) >= THRESHOLD])
    yellow = len([r for r in results if ALERT_THRESHOLD <= (r["voice_score"] or 0) < THRESHOLD])
    red = len([r for r in results if (r["voice_score"] or 0) < ALERT_THRESHOLD
               and r["voice_score"] is not None])
    print(f"\nИтого: {total} оценено | ✅ ≥{THRESHOLD}: {green} | "
          f"⚠️ 50–{THRESHOLD-1}: {yellow} | ❌ <50: {red}")

    if below50:
        print(f"\n{'='*40}")
        print(f"Требуют фикса (<{ALERT_THRESHOLD}):")
        for r in below50:
            print(f"  {r['slepok']} / {r['site_type']}: score={r['voice_score']}")
            if r.get("generic_examples"):
                print(f"    generic: {r['generic_examples'][:2]}")
            if r.get("note"):
                print(f"    судья: {r['note']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Офлайн-базовая-линия тон-войс для слепков")
    ap.add_argument("--mode", choices=["corpus", "generate"], default="corpus",
                    help="corpus=AGENT_ADS, generate=OpenRouter-генерация (default: corpus)")
    ap.add_argument("--slepok", help="оценить только этот слепок")
    ap.add_argument("--site-type", dest="site_type", help="и только этот site_type")
    ap.add_argument("--out", default="_tone_baseline_result.json",
                    help="файл для JSON-результата (default: _tone_baseline_result.json)")
    args = ap.parse_args()

    pairs: list[tuple[str, str]] = SLEPOK_SITE_TYPES
    if args.slepok:
        slepok_key = args.slepok.strip().lower()
        pairs = [(s, st) for s, st in pairs if s == slepok_key]
        if args.site_type:
            pairs = [(s, st) for s, st in pairs if st == args.site_type]
        if not pairs:
            _log(f"слепок {args.slepok!r} / site_type {args.site_type!r} не найден в списке")
            return 1

    _log(f"старт baseline: mode={args.mode}, пар={len(pairs)}")
    results = run_baseline(pairs, mode=args.mode)

    out = Path(args.out)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"JSON-результат → {out}")

    print_table(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())

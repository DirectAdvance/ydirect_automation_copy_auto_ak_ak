#!/usr/bin/env python3
"""check_tone_of_voice.py — тон-войс аудит созданного набора РК.

После создания набора (kind='set') проверяет, СГЕНЕРИРОВАН ли контент в голосе
выбранного слепка (а не как дефолтный кредитный шаблон), и шлёт краткий вердикт
Семёну в ЛИЧНЫЙ Telegram.

READ-ONLY: только читает БД (public.direct_automation_jobs, Victory) и кабинеты
Директа (v5 ads.get + cookie-Grid fallback). Никаких мутаций.

Запуск:
    python3 tools/check_tone_of_voice.py <job_id>
    python3 tools/check_tone_of_voice.py --latest              # последняя done set-джоба
    python3 tools/check_tone_of_voice.py --login porg-xxxx     # последняя джоба по логину
    # доп. флаги: --test (пометка [TEST] в telegram), --dry (не слать, только печать),
    #             --agent <slepok> --site-type <тип> (ручной оверрайд для ad-hoc проверки),
    #             --campaign <id> (повторяемо; ручной список кампаний вместо job.result)

На LXC101: /root/venv/bin/python3 -m direct.tools.check_tone_of_voice <job_id>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from pathlib import Path

# ── sys.path: сделать импортируемым пакет `direct` и .secret loader ────────────────
_THIS = Path(__file__).resolve()
_PKG_ROOT = _THIS.parents[2]          # .../home/seoadvanced  (родитель пакета `direct`)
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))
for _p in [_PKG_ROOT, *_PKG_ROOT.parents]:
    if (_p / ".secret" / "loader.py").exists():
        if str(_p / ".secret") not in sys.path:
            sys.path.insert(0, str(_p / ".secret"))
        break

# Порог: voice_score ниже него → алерт (лёгкая правка политики).
THRESHOLD = 60
# Сколько заголовков/текстов максимум отдаём судье (дешевле и достаточно для оценки).
MAX_TITLES = 30
MAX_TEXTS = 15
# 700 обрезал JSON судьи на середине при полном наборе заголовков/текстов.
_JUDGE_MAX_TOKENS = 2000
_JUDGE_RETRIES = 3
# Сколько эталонных примеров слепка кладём в промпт.
MAX_REF = 6


def _log(msg: str) -> None:
    print(f"[tone-check] {msg}", flush=True)


# ══════════════════════════════════════════════════════════════════════════════════
#  1. Чтение джобы из public.direct_automation_jobs (Victory)
# ══════════════════════════════════════════════════════════════════════════════════
def _victory_cursor():
    from loader import load_db  # type: ignore
    import psycopg2  # type: ignore
    db = load_db("victory")
    conn = psycopg2.connect(
        host=db["host"], port=db["port"], dbname=db["database"],
        user=db["user"], password=db["password"], connect_timeout=15,
    )
    return conn


def load_job(conn, *, job_id: str | None = None, latest: bool = False,
             login: str | None = None) -> dict | None:
    """Вернуть {job_id, login, agent, site_type, agency, campaign_ids, result} или None."""
    with conn.cursor() as cur:
        if job_id:
            cur.execute(
                "SELECT job_id, login, status, kind, body, result "
                "FROM public.direct_automation_jobs WHERE job_id=%s", (job_id,))
        elif login:
            cur.execute(
                "SELECT job_id, login, status, kind, body, result "
                "FROM public.direct_automation_jobs "
                "WHERE login=%s AND kind='set' AND status='done' "
                "ORDER BY updated_at DESC LIMIT 1", (login,))
        else:  # latest
            cur.execute(
                "SELECT job_id, login, status, kind, body, result "
                "FROM public.direct_automation_jobs "
                "WHERE kind='set' AND status='done' "
                "ORDER BY updated_at DESC LIMIT 1")
        row = cur.fetchone()
    if not row:
        return None
    jid, lg, status, kind, body, result = row
    body = body or {}
    result = result or {}
    campaign_ids: list[int] = []
    for r in (result.get("results") or []):
        cid = r.get("campaign_id") or r.get("id")
        if cid:
            try:
                campaign_ids.append(int(cid))
            except (TypeError, ValueError):
                pass
    # dedup сохраняя порядок
    seen: set[int] = set()
    campaign_ids = [c for c in campaign_ids if not (c in seen or seen.add(c))]
    return {
        "job_id": jid, "login": lg or body.get("login"),
        "status": status, "kind": kind,
        "agent": (body.get("agent") or "").strip().lower(),
        "site_type": body.get("site_type") or "",
        "agency": (body.get("agency") or "").strip().lower(),
        "campaign_ids": campaign_ids,
        "result": result,
    }


# ══════════════════════════════════════════════════════════════════════════════════
#  2. Чтение реально сгенерированного контента (titles/texts) — READ ONLY
# ══════════════════════════════════════════════════════════════════════════════════
def _extract_text_ad(ad: dict) -> tuple[list[str], str | None]:
    """Из v5-объявления вытащить (titles[], text). Покрывает TEXT_AD и адаптивные."""
    titles: list[str] = []
    text = None
    ta = ad.get("TextAd") or {}
    if ta:
        for k in ("Title", "Title2"):
            v = (ta.get(k) or "").strip()
            if v:
                titles.append(v)
        text = (ta.get("Text") or "").strip() or None
    return titles, text


def read_content_v5(login: str, agency: str, campaign_ids: list[int]) -> dict:
    """v5 ads.get через агентский токен. Возвращает {titles, texts, n_ads, n_campaigns, ok}.

    token_for_login может «залипнуть» на токене с пустым доступом (empty-not-error), поэтому
    перебираем все токены и берём тот, что реально отдаёт объявления для наших кампаний.
    """
    from direct.yandex_gateway import direct_tokens, v5_get  # type: ignore
    tokens = direct_tokens()
    # порядок: агентство из джобы первым, дальше остальные
    order = [agency] if agency and agency in tokens else []
    order += [a for a in tokens if a not in order]

    # если кампании не заданы — прочитать все текстовые кампании логина (для ad-hoc проверки)
    def _all_campaigns(tok: str) -> list[int]:
        cc = v5_get("campaigns", tok, login, ["Id"], criteria={}, extra={"Page": {"Limit": 100}})
        return [int(c["Id"]) for c in ((cc.get("result") or {}).get("Campaigns") or []) if c.get("Id")]

    best = {"titles": [], "texts": [], "n_ads": 0, "n_campaigns": 0, "ok": False, "agency": None}
    for ag in order:
        tok = tokens.get(ag)
        if not tok:
            continue
        cids = list(campaign_ids) or _all_campaigns(tok)
        if not cids:
            continue
        titles: list[str] = []
        texts: list[str] = []
        n_ads = 0
        seen_c: set[int] = set()
        for i in range(0, len(cids), 10):          # v5 лимит: ≤10 CampaignIds
            chunk = cids[i:i + 10]
            res = v5_get("ads", tok, login,
                         ["Id", "CampaignId", "Type", "Subtype"],
                         criteria={"CampaignIds": chunk},
                         extra={"TextAdFieldNames": ["Title", "Title2", "Text"]})
            ads = (res.get("result") or {}).get("Ads") or []
            for a in ads:
                n_ads += 1
                cid = a.get("CampaignId")
                if cid:
                    seen_c.add(cid)
                t_list, txt = _extract_text_ad(a)
                titles.extend(t_list)
                if txt:
                    texts.append(txt)
        if n_ads > best["n_ads"]:
            best = {
                "titles": _dedup(titles), "texts": _dedup(texts),
                "n_ads": n_ads, "n_campaigns": len(seen_c), "ok": n_ads > 0, "agency": ag,
            }
        # первый токен с объявлениями — обычно и есть управляющий; но продолжим если 0
        if best["ok"] and ag == order[0]:
            break
    return best


def read_content_grid(login: str, campaign_ids: list[int]) -> dict:
    """Fallback: cookie-Grid (кампании, созданные по куке, v5 не видит).

    Читает GdAdaptiveTextAd{titles bodies} и GdTextAd{title body}. Best-effort, не падает.
    """
    out = {"titles": [], "texts": [], "n_ads": 0, "n_campaigns": 0, "ok": False, "agency": "cookie-grid"}
    if not campaign_ids:
        return out
    try:
        import urllib3
        urllib3.disable_warnings()
        import requests
        from direct import campaign as cmc  # type: ignore
        cookie = cmc.pick_working_cookie(login)
        if not cookie:
            return out
        grid = "https://direct.yandex.ru/web-api/grid/api"
        sess = requests.Session()
        sess.verify = False
        csrf = {"t": None}

        def _post(op, q, v):
            h = {"Cookie": cookie, "dna-operation-name": op, "x-direct-api": "1",
                 "x-detected-locale": "ru", "Content-Type": "application/json",
                 "User-Agent": cmc.USER_AGENT, "Origin": "https://direct.yandex.ru",
                 "Referer": f"https://direct.yandex.ru/dna/grid/campaigns?ulogin={login}"}
            if csrf["t"]:
                h["x-csrf-token"] = csrf["t"]
            url = f"{grid}?operationName={op}&ulogin={login}"
            r = sess.post(url, json={"operationName": op, "query": q, "variables": v}, headers=h, timeout=40)
            if r.status_code == 403:
                m = re.search(r"_direct_csrf_token=([^;,\s]+)", r.headers.get("Set-Cookie", ""))
                t = r.cookies.get("_direct_csrf_token") or (m.group(1) if m else None)
                if t:
                    csrf["t"] = t
                    h["x-csrf-token"] = t
                    r = sess.post(url, json={"operationName": op, "query": q, "variables": v}, headers=h, timeout=40)
            return r

        q = ("query AdsRead($login:String!,$inp:GdAdsContainerInput!){client(searchBy:{login:$login})"
             "{ads(input:$inp){rowset{id campaignId __typename "
             "...on GdAdaptiveTextAd{titles bodies} ...on GdTextAd{title body}}}}}")
        titles: list[str] = []
        texts: list[str] = []
        n_ads = 0
        seen_c: set = set()
        cids = [str(c) for c in campaign_ids]
        for i in range(0, len(cids), 50):
            chunk = cids[i:i + 50]
            inp = {"filter": {"campaignIdIn": chunk},
                   "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
                   "limitOffset": {"limit": 1000, "offset": 0},
                   "orderBy": [{"order": "ASC", "field": "ID"}]}
            d = _post("AdsRead", q, {"login": login, "inp": inp}).json()
            rows = ((((d.get("data") or {}).get("client") or {}).get("ads") or {}).get("rowset") or [])
            for row in rows:
                n_ads += 1
                if row.get("campaignId"):
                    seen_c.add(row.get("campaignId"))
                for t in (row.get("titles") or []):
                    if t:
                        titles.append(str(t).strip())
                if row.get("title"):
                    titles.append(str(row.get("title")).strip())
                for b in (row.get("bodies") or []):
                    if b:
                        texts.append(str(b).strip())
                if row.get("body"):
                    texts.append(str(row.get("body")).strip())
        out.update(titles=_dedup(titles), texts=_dedup(texts),
                   n_ads=n_ads, n_campaigns=len(seen_c), ok=bool(titles or texts))
    except Exception as e:  # noqa: BLE001
        _log(f"grid fallback failed: {type(e).__name__}: {str(e)[:160]}")
    return out


def _dedup(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        x = (x or "").strip()
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def read_generated_content(login: str, agency: str, campaign_ids: list[int]) -> dict:
    """v5-first, затем cookie-Grid fallback. Возвращает dict с titles/texts + метаданными."""
    res = {"titles": [], "texts": [], "n_ads": 0, "n_campaigns": 0, "source": None, "note": ""}
    try:
        v5 = read_content_v5(login, agency, campaign_ids)
    except Exception as e:  # noqa: BLE001
        v5 = {"ok": False}
        _log(f"v5 read failed: {type(e).__name__}: {str(e)[:160]}")
    if v5.get("ok"):
        res.update(titles=v5["titles"], texts=v5["texts"], n_ads=v5["n_ads"],
                   n_campaigns=v5["n_campaigns"], source=f"v5:{v5.get('agency')}")
        return res
    grid = read_content_grid(login, campaign_ids)
    if grid.get("ok"):
        res.update(titles=grid["titles"], texts=grid["texts"], n_ads=grid["n_ads"],
                   n_campaigns=grid["n_campaigns"], source="cookie-grid")
        return res
    res["note"] = "объявления не читаются (v5=0, grid=0): черновики удалены/недоступны"
    return res


# ══════════════════════════════════════════════════════════════════════════════════
#  3. Эталон голоса слепка из ai_agents.py
# ══════════════════════════════════════════════════════════════════════════════════
def build_voice_reference(slepok: str, site_type: str = "") -> dict:
    """Собрать определение голоса с тем же site_type-фильтром, что использует генерация."""
    from direct import ai_agents as A  # type: ignore
    agent = A.AGENTS.get(slepok) or {}
    signature = A.signature_for(slepok, site_type)
    ads = A.filtered_ads_for_site(agent, site_type) if agent else {}
    ref_titles = (ads.get("titles") or [])[:MAX_REF]
    ref_texts = (ads.get("texts") or [])[:MAX_REF]
    parts = []
    if agent.get("name"):
        parts.append(f"Слепок: {agent['name']} ({slepok})")
    if site_type:
        parts.append(f"Тип сайта: {site_type}")
    st_hint = A.SITE_TYPE_PROFILE.get((site_type or "").strip(), "")
    if st_hint:
        parts.append(f"Правила типа сайта:\n{st_hint}")
    if agent.get("tagline"):
        parts.append(f"Кредо: {agent['tagline']}")
    system_text = A.system_for_site(agent, site_type) if agent else ""
    if system_text:
        parts.append(f"Определение голоса (system):\n{system_text}")
    promo = A.filtered_promo_for_site(agent, site_type) if agent else {}
    if promo:
        ex = "; ".join(promo.get("examples") or [])
        plus = "; ".join(promo.get("plus") or [])
        parts.append(f"Промо-акцент: тип={promo.get('type')} ед.={promo.get('unit')}; "
                     f"фирменные обороты: {plus}; примеры: {ex}")
    if signature:
        parts.append(f"Подпись голоса / запрет чужих голосов:\n{signature}")
    if ref_titles:
        parts.append("ЭТАЛОННЫЕ заголовки этого слепка:\n- " + "\n- ".join(ref_titles))
    if ref_texts:
        parts.append("ЭТАЛОННЫЕ тексты этого слепка:\n- " + "\n- ".join(ref_texts))
    return {
        "text": "\n\n".join(parts),
        "has_signature": bool(signature),
        "has_corpus": bool(ref_titles or ref_texts),
        "name": agent.get("name") or slepok,
    }


# ══════════════════════════════════════════════════════════════════════════════════
#  3b. OFFLINE-скоринг (без созданных кампаний, без login/cabinet)
# ══════════════════════════════════════════════════════════════════════════════════
def score_offline(slepok: str, site_type: str,
                  titles: list[str], texts: list[str]) -> dict:
    """Оценить набор заголовков+текстов против голоса слепка без живых кампаний.

    Использует тот же LLM-судья (OpenRouter), что и live-путь, но не обращается
    в кабинет Директа и не читает Victory-джобы. Возвращает тот же формат, что
    judge_voice: {voice_score, verdict, generic_examples, in_voice_examples, note}
    или {error: <str>}.

    Пример использования:
        from direct.tools.check_tone_of_voice import score_offline
        res = score_offline("kryuchkova", "Монобренд", titles, texts)
        print(res["voice_score"], res["verdict"])
    """
    voice_ref = build_voice_reference(slepok, site_type)
    if not voice_ref.get("text"):
        return {"error": f"no voice reference for slepok={slepok!r}",
                "voice_score": 0, "verdict": "generic",
                "generic_examples": [], "in_voice_examples": [], "note": ""}
    return judge_voice(voice_ref, list(titles or []), list(texts or []),
                       slepok, site_type)


# ══════════════════════════════════════════════════════════════════════════════════
#  4. LLM-судья (переиспользуем OpenRouter-клиент проекта, дешёвая модель)
# ══════════════════════════════════════════════════════════════════════════════════
_JUDGE_SYSTEM = (
    "Ты — строгий редактор-аудитор фирменного стиля (tone of voice) для контекстной рекламы. "
    "Тебе дают ЭТАЛОН голоса конкретного директолога-«слепка» и РЕАЛЬНО сгенерированные "
    "заголовки/тексты объявлений. Оцени, насколько контент написан именно В ГОЛОСЕ слепка, "
    "а не как обезличенный дефолтный кредитный шаблон («автокредит», «выгодные условия», "
    "«одобрение 98%» и т.п. без фирменных оборотов). ЖЁСТКО штрафуй, если контент неотличим "
    "от generic-шаблона и не несёт фирменных оборотов/подписи слепка; поощряй фирменные ходы. "
    "Отвечай ТОЛЬКО валидным JSON без пояснений и без markdown-ограждения."
)


def _extract_json(text: str) -> dict | None:
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
                return None
    return None


def judge_voice(voice_ref: dict, titles: list[str], texts: list[str],
                slepok: str, site_type: str) -> dict:
    """Вернуть {voice_score, verdict, generic_examples, in_voice_examples, note} или {error}."""
    from direct import llm_providers as LP  # type: ignore
    titles_s = titles[:MAX_TITLES]
    texts_s = texts[:MAX_TEXTS]
    user = (
        f"=== ЭТАЛОН ГОЛОСА СЛЕПКА «{slepok}» (site_type={site_type}) ===\n"
        f"{voice_ref['text']}\n\n"
        f"=== РЕАЛЬНО СГЕНЕРИРОВАННЫЕ ЗАГОЛОВКИ ({len(titles_s)}) ===\n"
        + "\n".join(f"- {t}" for t in titles_s) + "\n\n"
        f"=== РЕАЛЬНО СГЕНЕРИРОВАННЫЕ ТЕКСТЫ ({len(texts_s)}) ===\n"
        + "\n".join(f"- {t}" for t in texts_s) + "\n\n"
        "Верни СТРОГО такой JSON:\n"
        '{"voice_score": <int 0-100, насколько контент в голосе слепка, а не generic-шаблон>, '
        '"verdict": "in_voice"|"generic"|"mixed", '
        '"generic_examples": [<до 5 заголовков/фраз, что выглядят generic/не в голосе>], '
        '"in_voice_examples": [<до 5, что реально в голосе слепка>], '
        '"note": "<краткий комментарий на русском, 1-2 предложения>"}'
    )
    messages = [{"role": "system", "content": _JUDGE_SYSTEM},
                {"role": "user", "content": user}]
    # Судья флейкует ~40% вызовов: модель отдаёт либо пустой content, либо JSON, обрезанный
    # на max_tokens. `tries` внутри _or_complete_url ретраит только транспорт и этот класс
    # сбоя не ловит — из-за чего замер возвращал judge_error вместо цифры. Ретраим сами.
    content = ""
    last_err = ""
    for attempt in range(_JUDGE_RETRIES):
        content, err = LP._or_complete_url("", messages, max_tokens=_JUDGE_MAX_TOKENS,
                                           temperature=0.2, tries=2)
        if content and not err:
            parsed = _extract_json(content)
            if parsed and "voice_score" in parsed:
                break
            last_err = f"LLM вернул не-JSON: {str(content)[:160]}"
        else:
            last_err = err or "пустой ответ LLM"
        if attempt < _JUDGE_RETRIES - 1:
            time.sleep(2 * (attempt + 1))
    else:
        return {"error": last_err}
    try:
        parsed["voice_score"] = int(round(float(parsed["voice_score"])))
    except Exception:  # noqa: BLE001
        parsed["voice_score"] = 0
    parsed["voice_score"] = max(0, min(100, parsed["voice_score"]))
    parsed.setdefault("verdict", "mixed")
    parsed.setdefault("generic_examples", [])
    parsed.setdefault("in_voice_examples", [])
    parsed.setdefault("note", "")
    return parsed


# ══════════════════════════════════════════════════════════════════════════════════
#  5. Telegram (Direct automation bot)
# ══════════════════════════════════════════════════════════════════════════════════
def send_telegram(text: str) -> bool:
    from loader import load_telegram, send_telegram_message  # type: ignore
    tg = load_telegram("direct")
    tok = tg.get("bot_token")
    chat = tg.get("chat_id")
    if not tok or not chat:
        _log("telegram: нет bot_token/chat_id в .secret")
        return False
    return send_telegram_message(tok, chat, text)


def format_message(job: dict, content: dict, verdict: dict, voice_ref: dict, *, test: bool) -> str:
    tag = "[TEST] " if test else ""
    slepok = job.get("agent") or "?"
    name = voice_ref.get("name") or slepok
    site = job.get("site_type") or "?"
    login = job.get("login") or "?"
    jid = job.get("job_id") or "?"
    score = verdict.get("voice_score", 0)
    v = verdict.get("verdict", "?")
    icon = "✅" if score >= THRESHOLD and v == "in_voice" else ("⚠️" if score < THRESHOLD else "🟡")
    lines = [
        f"{tag}{icon} Тон-войс: {name}/{site} — score {score}/100 ({v})",
        f"login {login} · job {jid}",
        f"проверено: {content.get('n_campaigns', 0)} кампаний · "
        f"{len(content.get('titles', []))} загол. · {len(content.get('texts', []))} текстов "
        f"(source {content.get('source') or '—'})",
    ]
    note = (verdict.get("note") or "").strip()
    if score < THRESHOLD or v in ("generic", "mixed"):
        gen = [str(x) for x in (verdict.get("generic_examples") or [])][:3]
        if gen:
            lines.append("generic-примеры:")
            lines += [f"  • {g}" for g in gen]
        if note:
            lines.append(f"судья: {note}")
    else:
        if note:
            lines.append(f"судья: {note}")
        lines.append("контент в голосе слепка ✔")
    if not voice_ref.get("has_corpus"):
        lines.append("⚠️ у слепка нет эталон-корпуса AGENT_ADS — оценка по system/tagline")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════════
#  Оркестрация
# ══════════════════════════════════════════════════════════════════════════════════
def run_check(*, job_id: str | None = None, latest: bool = False, login: str | None = None,
              agent: str | None = None, site_type: str | None = None,
              campaign_ids: list[int] | None = None, test: bool = False,
              send: bool = True) -> dict:
    """Полный цикл. Возвращает dict-результат. Всегда пытается сообщить в telegram при провале."""
    conn = None
    try:
        conn = _victory_cursor()
        job = load_job(conn, job_id=job_id, latest=latest, login=login)
    except Exception as e:  # noqa: BLE001
        msg = "[TEST] " if test else ""
        msg += f"⚠️ Тон-войс: не смог прочитать джобу ({type(e).__name__}: {str(e)[:120]})"
        if send:
            send_telegram(msg)
        return {"ok": False, "error": str(e)}
    finally:
        if conn:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    if not job:
        # ad-hoc режим: джобы нет, но заданы --login + --agent → синтетическая джоба,
        # читаем все кампании логина (или переданные --campaign). Полезно для ручных проверок.
        if login and agent:
            _log("джоба не найдена — ad-hoc режим по --login/--agent")
            job = {"job_id": f"adhoc:{login}", "login": login, "status": "adhoc",
                   "kind": "set", "agent": "", "site_type": "", "agency": "",
                   "campaign_ids": [], "result": {}}
        else:
            _log("джоба не найдена")
            return {"ok": False, "error": "job not found"}

    # ручные оверрайды (ad-hoc проверка)
    if agent:
        job["agent"] = agent.strip().lower()
    if site_type:
        job["site_type"] = site_type
    if campaign_ids:
        job["campaign_ids"] = campaign_ids

    slepok = job["agent"]
    if not slepok:
        _log("в джобе нет agent (слепка) — пропуск")
        return {"ok": False, "error": "no agent in job"}

    voice_ref = build_voice_reference(slepok, job.get("site_type") or "")
    content = read_generated_content(job["login"], job["agency"], job["campaign_ids"])

    # нет контента → честно сообщить «не смог проверить»
    if not content.get("titles") and not content.get("texts"):
        tag = "[TEST] " if test else ""
        text = (f"{tag}🟡 Тон-войс: {voice_ref.get('name')}/{job.get('site_type')} — не смог проверить\n"
                f"login {job['login']} · job {job['job_id']}\n"
                f"причина: {content.get('note') or 'объявлений с текстом не найдено'}")
        if send:
            send_telegram(text)
        return {"ok": False, "error": "no content", "job": job, "content": content, "message": text}

    verdict = judge_voice(voice_ref, content["titles"], content["texts"],
                          slepok, job.get("site_type") or "")
    if verdict.get("error"):
        tag = "[TEST] " if test else ""
        text = (f"{tag}🟡 Тон-войс: {voice_ref.get('name')}/{job.get('site_type')} — LLM недоступен\n"
                f"login {job['login']} · job {job['job_id']} · "
                f"{len(content['titles'])} загол./{len(content['texts'])} текстов\n"
                f"причина: {verdict['error'][:160]}")
        if send:
            send_telegram(text)
        return {"ok": False, "error": verdict["error"], "job": job, "content": content, "message": text}

    text = format_message(job, content, verdict, voice_ref, test=test)
    sent = send_telegram(text) if send else False
    return {"ok": True, "job": job, "content_stats": {
        "n_campaigns": content["n_campaigns"], "n_titles": len(content["titles"]),
        "n_texts": len(content["texts"]), "source": content["source"]},
        "verdict": verdict, "message": text, "sent": sent,
        "alert": verdict["voice_score"] < THRESHOLD}


def main() -> int:
    ap = argparse.ArgumentParser(description="Тон-войс аудит созданного набора РК")
    ap.add_argument("job_id", nargs="?", help="job_id (kind='set')")
    ap.add_argument("--latest", action="store_true", help="последняя done set-джоба")
    ap.add_argument("--login", help="последняя done set-джоба по логину")
    ap.add_argument("--agent", help="оверрайд слепка (ad-hoc)")
    ap.add_argument("--site-type", dest="site_type", help="оверрайд site_type (ad-hoc)")
    ap.add_argument("--campaign", action="append", type=int, help="campaign_id (повторяемо)")
    ap.add_argument("--test", action="store_true", help="пометить сообщение [TEST]")
    ap.add_argument("--dry", action="store_true", help="не слать в telegram, только напечатать")
    # ── offline-режим: без кабинета, только JSON-файл с titles/texts ──
    ap.add_argument("--offline", action="store_true",
                    help="offline-скоринг: --agent + --site-type + --content-file (без кабинета)")
    ap.add_argument("--content-file", dest="content_file",
                    help="JSON {\"titles\":[...],\"texts\":[...]} для --offline")
    args = ap.parse_args()

    # ── offline-ветка: не требует job_id / login / latest ──
    if args.offline:
        if not args.agent:
            ap.error("--offline требует --agent")
        if not args.site_type:
            ap.error("--offline требует --site-type")
        titles: list[str] = []
        texts: list[str] = []
        if args.content_file:
            import pathlib as _pl
            raw = json.loads(_pl.Path(args.content_file).read_text(encoding="utf-8"))
            titles = raw.get("titles") or []
            texts = raw.get("texts") or []
        verdict = score_offline(args.agent, args.site_type, titles, texts)
        print(json.dumps(verdict, ensure_ascii=False, indent=1))
        score = verdict.get("voice_score", 0)
        icon = "✅" if score >= THRESHOLD else ("⚠️" if score >= 50 else "❌")
        print(f"\n{icon} voice_score={score}/100 | verdict={verdict.get('verdict')} | "
              f"slepok={args.agent} site_type={args.site_type} "
              f"titles={len(titles)} texts={len(texts)}")
        return 0

    if not (args.job_id or args.latest or args.login):
        ap.error("нужен job_id, либо --latest, либо --login")

    try:
        res = run_check(job_id=args.job_id, latest=args.latest, login=args.login,
                        agent=args.agent, site_type=args.site_type,
                        campaign_ids=args.campaign, test=args.test, send=not args.dry)
    except Exception as e:  # noqa: BLE001
        _log(f"FATAL: {type(e).__name__}: {e}")
        traceback.print_exc()
        try:
            tag = "[TEST] " if args.test else ""
            if not args.dry:
                send_telegram(f"{tag}⚠️ Тон-войс: сбой проверки — {type(e).__name__}: {str(e)[:120]}")
        except Exception:  # noqa: BLE001
            pass
        return 1

    print(json.dumps({k: v for k, v in res.items() if k != "job"}, ensure_ascii=False, indent=1))
    if res.get("message"):
        print("\n--- Сообщение в Telegram ---\n" + res["message"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

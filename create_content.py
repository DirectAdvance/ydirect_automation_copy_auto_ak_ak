"""Ядро генерации контента ОДНОЙ РК (M3 fan-out 14B×3 + 72B-патч + фолбэк слепка).

Вынесено из `_gen_campaign_content` (blueprint.py) БЕЗ изменения поведения: одна pure-функция
`run_gen_campaign_content(...)`. Все module-level helper'ы/константы blueprint.py инъектируются
вызовом (DI) — модуль не импортирует blueprint и не создаёт циклический импорт.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable


def run_gen_campaign_content(*, login: str, agent: dict, agent_key: str, item: dict,
                             avoid: list | None = None, ctx_override: dict | None = None,
                             fast_mode: bool = False,
                             # injected helpers (DI)
                             _bad_ad_sitelink: Callable[..., Any],
                             _bad_ad_text: Callable[..., Any],
                             _bad_ad_title: Callable[..., Any],
                             _brand_from_coder: Callable[..., Any],
                             _display_brand: Callable[..., Any],
                             _extract_text_candidates: Callable[..., Any],
                             _extract_title_candidates: Callable[..., Any],
                             _m3_complete_parallel: Callable[..., Any],
                             _m3_complete_url: Callable[..., Any],
                             _promo_ctx: Callable[..., Any],
                             _promo_extract_json: Callable[..., Any],
                             _slepok_content_get: Callable[..., Any],
                             _title2_blocklist: Callable[..., Any],
                             _variant_norm_key: Callable[..., Any],
                             # injected constants (DI)
                             _M3_LLM_REPAIR_TIMEOUT: Any,
                             _M3_LLM_TIMEOUT_14B: Any,
                             _M3_CONTENT_IDLE_TIMEOUT: Any = 30.0,
                             _M3_LLM_URLS_14B: Any,
                             _M3_LLM_URL_72B: Any,
                             _RU_CITIES: Any) -> dict:
    """Генерация контента одной РК. Идентична бывшей `_gen_campaign_content`; поведение сохранено.
    → {ok, agent, login, item, brand, content:{titles,texts,sitelinks,title2?}, warnings, fallback}
      | {ok:False, error}."""
    from . import ai_agents as A
    ctx = dict(ctx_override or {}) if isinstance(ctx_override, dict) else _promo_ctx(login)
    if not ctx:
        return {"ok": False, "error": f"аккаунт {login} не найден в БД (Авто)"}

    avoid0 = [str(a)[:80] for a in (avoid or []) if a][-8:]
    brand = _brand_from_coder(item)          # марка/модель из кодера (gc/c/code) → контент про неё
    st = (ctx.get("site_type") or "")
    salon = (ctx.get("salon") or "").strip()
    _item_sig = " ".join(str(item.get(k) or "") for k in ("type", "tp", "name", "c", "gc", "code", "campaign_type")).lower()
    title_target_n = 5 if re.search(r"(?<![a-z0-9])tp[67](?![a-z0-9])|tp[67]_", _item_sig) else A.TITLES_N
    min_t, min_x = A.TITLE_TARGET_MIN, A.TEXT_TARGET_MIN   # жёсткие пороги длины
    _brand_word = (brand.split()[0].strip() if brand else "")
    _brand_re = (re.compile(r"(?i)\b" + re.escape(_brand_word) + r"\b") if _brand_word else None)
    _TITLE_UTP_RE = re.compile(
        r"(?i)(\d|кредит|плат[её]ж|скидк|выгод|распрод|трейд-?ин|каско|подар|в наличии|господдерж|госпрограм|"
        r"одобр|акци|тест-?драйв|шин|взнос)")
    _DIRECT_CREDIT_RE = re.compile(
        r"(?i)(кредит|автокредит|плат[её]ж|/мес|взнос|одобр|решение\s+банк|банк(?:а|ов|и)?|рассроч)"
    )
    _TEXT_UTP_RE = re.compile(
        r"(?i)(\d|кредит|плат[её]ж|скидк|выгод|трейд-?ин|каско|подар|в наличии|господдерж|госпрограм|"
        r"одобр|акци|тест-?драйв|шин|взнос|документ|комплект|ключи)")
    _CTA_RE = re.compile(r"(?i)(звонит|остав(?:ь|ьте)\s+заявк|оставьте\s+контакт|запишит|приезжайт|узнайте|получите)")
    _TEXT_SHOUTY_START_RE = re.compile(
        r"(?i)^\s*(скидк\w*|выгод\w*|акци\w*|только\s+сегодня|успей(?:те)?|спешит(?:е)?|"
        r"ликвидац\w+|распродаж\w+)\b[^.]{0,32}!")

    def _has_salon(s: str) -> bool:
        return bool(salon) and len(salon) > 3 and salon.lower() in (s or "").lower()

    def _bad_num(s: str) -> bool:
        # абсурдно малый ежемесячный платёж («300 ₽/мес») — отбрасываем строку
        for m in re.finditer(r"(\d[\d\s]*)\s*₽?\s*/\s*мес", s or ""):
            n = int(re.sub(r"\D", "", m.group(1)) or 0)
            if 0 < n < 5000:
                return True
        return False

    _bu_site = A.is_bu_site_type(st)
    _new_only_site = st in A.NEW_ONLY_SITE_TYPES
    # Для БРЕНДОВОЙ кампании общий размер автопарка салона неуместен («6500+ авто», «более 6500»,
    # «огромный выбор авто») — это число по ВСЕМУ салону, а не по одной марке.
    _BIG_INV_RE = re.compile(r"(?i)(?:\b\d{3,}\s*\+|\bболее\s+\d{3,}|\bогромн\w+\s+выбор)\s*"
                             r"(?:б/?у\s+|нов\w+\s+|проверенн\w+\s+)?(?:авто|машин)")

    def _bad_inventory(s: str) -> bool:
        return bool(brand) and bool(_BIG_INV_RE.search(s or ""))

    akey = (agent_key or "").strip().lower()   # ключ слепка для фильтра чужих сигнатур

    def _ok(s: str) -> bool:
        # на сайте «С пробегом» отбрасываем новоавтомобильные строки («Новые Haval» и т.п.);
        # на новых сайтах — наоборот, отбрасываем «б/у / с пробегом»;
        # + отбрасываем строки с УНИКАЛЬНОЙ сигнатурой ДРУГОГО слепка (кросс-контаминация)
        return (not _has_salon(s) and not _bad_num(s) and not _bad_inventory(s)
                and not A.has_forbidden_claim(s)
                and not A.alien_signature(akey, s)
                and not (_bu_site and A._bad_for_bu(s))
                and not (_new_only_site and A._bad_for_new(s)))

    def _title_ok(t: str) -> bool:
        if not _ok(t):
            return False
        # Для брендовой кампании слабые общие заголовки без марки почти всегда нерелевантны.
        if _brand_re and not _brand_re.search(t or ""):
            return False
        # Заголовок должен нести продающее УТП, а не быть общим «Новые BAIC в городе».
        if not _TITLE_UTP_RE.search(t or ""):
            return False
        # Для этой задачи каждый заголовок должен явно продавать автокредит, а не просто авто/акцию.
        if not _DIRECT_CREDIT_RE.search(t or ""):
            return False
        return True

    def _text_ok(x: str) -> bool:
        if not _ok(x):
            return False
        # Для брендовой кампании текст без марки часто оказывается слишком общим и слабым.
        if _brand_re and not _brand_re.search(x or ""):
            return False
        # Текст должен содержать реальную выгоду/условие, а не общий слоган.
        if not _TEXT_UTP_RE.search(x or ""):
            return False
        # Текст тоже держим в кредитной рамке: Директ и пользователь должны понимать оффер сразу.
        if not _DIRECT_CREDIT_RE.search(x or ""):
            return False
        # В тексте нужен явный призыв к действию, как и требует промпт.
        if not _CTA_RE.search(x or ""):
            return False
        return True

    # Fan-out: 3 параллельных 14B-инстанса (titles/texts/sitelinks) → собираем, фильтруем.
    # Если какой-то раздел пришёл слишком плохим — 72B-патч одним запросом (build_campaign_messages).
    # assemble_campaign добивает остаток из корпуса агента. Ранний выход если всё хватило.
    good_t, good_x, good_sl = [], [], []
    seen_t, seen_t_norm = set(), set()
    seen_t_first: dict[str, int] = {}
    seen_t_utp: set[str] = set()
    seen_x, seen_x_norm = set(), set()
    seen_sl, seen_sl_desc = set(), set()
    accepted_examples = {"titles": [], "texts": [], "sitelinks": []}
    used_retry_titles = False
    used_retry_texts = False
    used_retry_sitelinks = False
    repair_rounds_used = 0
    reject_stats = {
        "titles": {"too_short": 0, "bad_site_fit": 0, "missing_brand": 0, "missing_utp": 0,
                    "missing_number": 0,
                    "missing_credit_angle": 0, "duplicate": 0, "same_first_word": 0, "same_utp_bucket": 0},
        "texts": {"too_short": 0, "bad_site_fit": 0, "missing_brand": 0, "missing_utp": 0,
                   "missing_cta": 0, "duplicate": 0},
        "sitelinks": {"bad_site_fit": 0, "duplicate_title": 0, "duplicate_description": 0},
    }
    reject_examples = {"titles": {}, "texts": {}, "sitelinks": {}}
    good_t2: str = ""
    last_err = None

    msgs_t  = A.build_titles_messages(agent, ctx, item=item, avoid=avoid0, brand=brand)
    msgs_x  = A.build_texts_messages(agent, ctx, item=item, brand=brand)
    msgs_sl = A.build_sitelinks_messages(agent, ctx, item=item, brand=brand)
    _kw14b = {"max_tokens": 280, "temperature": 0.72, "top_p": 0.9,
              "repetition_penalty": 1.15,
              "tries": 1 if fast_mode else 2,
              # IDLE-таймаут (пауза между токенами при стриминге E), НЕ wall-clock. Живой M3
              # стримит ~6.5 ток/с (гэп <1с) → idle=30с не рвёт рабочую генерацию, только висящий
              # M3 (0 токенов). Раньше 90с(fast)/360с висели на мёртвом completion (баг 09.07).
              "timeout": _M3_CONTENT_IDLE_TIMEOUT}
    urls14 = _M3_LLM_URLS_14B
    par_results = _m3_complete_parallel([
        (urls14[0], msgs_t,  _kw14b),
        (urls14[1 % len(urls14)], msgs_x,  dict(_kw14b, max_tokens=220)),
        (urls14[2 % len(urls14)], msgs_sl, dict(_kw14b, max_tokens=400)),
    ])
    text_t,  err_t  = par_results[0]
    text_x,  err_x  = par_results[1]
    text_sl, err_sl = par_results[2]

    raw_t  = _promo_extract_json(text_t)  if not err_t  else {}
    raw_x  = _promo_extract_json(text_x)  if not err_x  else {}
    raw_sl = _promo_extract_json(text_sl) if not err_sl else {}
    raw_titles, raw_title2 = _extract_title_candidates(raw_t)
    raw_texts = _extract_text_candidates(raw_x)

    # title2 из ответа по заголовкам (если модель добавила)
    if raw_title2:
        good_t2 = raw_title2[:A.TITLE2_MAX]

    def _site_fit_ok(s: str) -> bool:
        return (not _has_salon(s) and not _bad_num(s) and not _bad_inventory(s)
                and not A.has_forbidden_claim(s)
                and not re.search(r"(?i)\bбез\s+документ", s or "")
                and not A.alien_signature(akey, s)
                and not (_bu_site and A._bad_for_bu(s))
                and not (_new_only_site and A._bad_for_new(s)))

    def _push_example(group: str, reason: str, sample: str) -> None:
        if not sample:
            return
        bucket = reject_examples.setdefault(group, {}).setdefault(reason, [])
        if sample not in bucket and len(bucket) < 3:
            bucket.append(sample[:160])

    def _push_accepted(group: str, sample: str) -> None:
        if not sample:
            return
        bucket = accepted_examples.setdefault(group, [])
        if sample not in bucket and len(bucket) < 5:
            bucket.append(sample[:160])

    def _accept_title(t: str) -> bool:
        if not isinstance(t, str):
            return False
        if len(t) < min_t:
            reject_stats["titles"]["too_short"] += 1
            _push_example("titles", "too_short", t)
            return False
        if not _site_fit_ok(t):
            reject_stats["titles"]["bad_site_fit"] += 1
            _push_example("titles", "bad_site_fit", t)
            return False
        if _bad_ad_title(t):
            reject_stats["titles"]["bad_site_fit"] += 1
            _push_example("titles", "bad_site_fit", t)
            return False
        if _brand_re and not _brand_re.search(t or ""):
            reject_stats["titles"]["missing_brand"] += 1
            _push_example("titles", "missing_brand", t)
            return False
        if not _TITLE_UTP_RE.search(t or ""):
            reject_stats["titles"]["missing_utp"] += 1
            _push_example("titles", "missing_utp", t)
            return False
        if not re.search(r"\d", t or ""):
            reject_stats["titles"]["missing_number"] += 1
            _push_example("titles", "missing_number", t)
            return False
        if not _DIRECT_CREDIT_RE.search(t or ""):
            reject_stats["titles"]["missing_credit_angle"] += 1
            _push_example("titles", "missing_credit_angle", t)
            return False
        _tl = t.lower()
        _nk = _variant_norm_key(t)
        _fw = str(t).split()[0].lower().rstrip(".,!?") if str(t).split() else ""
        _ub = A._title_utp_bucket(t)
        if _tl in seen_t or (_nk and _nk in seen_t_norm):
            reject_stats["titles"]["duplicate"] += 1
            _push_example("titles", "duplicate", t)
            return False
        if _fw and seen_t_first.get(_fw, 0) >= 2:
            reject_stats["titles"]["same_first_word"] += 1
            _push_example("titles", "same_first_word", t)
            return False
        if _ub and _ub in seen_t_utp:
            reject_stats["titles"]["same_utp_bucket"] += 1
            _push_example("titles", "same_utp_bucket", t)
            return False
        seen_t.add(_tl)
        if _nk:
            seen_t_norm.add(_nk)
        if _fw:
            seen_t_first[_fw] = seen_t_first.get(_fw, 0) + 1
        if _ub:
            seen_t_utp.add(_ub)
        good_t.append(t)
        _push_accepted("titles", t)
        return True

    def _accept_text(x: str) -> bool:
        if not isinstance(x, str):
            return False
        if len(x) < min_x:
            reject_stats["texts"]["too_short"] += 1
            _push_example("texts", "too_short", x)
            return False
        if not _site_fit_ok(x):
            reject_stats["texts"]["bad_site_fit"] += 1
            _push_example("texts", "bad_site_fit", x)
            return False
        if _bad_ad_text(x):
            reject_stats["texts"]["bad_site_fit"] += 1
            _push_example("texts", "bad_site_fit", x)
            return False
        if _brand_re and not _brand_re.search(x or ""):
            reject_stats["texts"]["missing_brand"] += 1
            _push_example("texts", "missing_brand", x)
            return False
        if not _TEXT_UTP_RE.search(x or ""):
            reject_stats["texts"]["missing_utp"] += 1
            _push_example("texts", "missing_utp", x)
            return False
        if not _DIRECT_CREDIT_RE.search(x or ""):
            reject_stats["texts"]["missing_utp"] += 1
            _push_example("texts", "missing_utp", x)
            return False
        if not _CTA_RE.search(x or ""):
            reject_stats["texts"]["missing_cta"] += 1
            _push_example("texts", "missing_cta", x)
            return False
        if _TEXT_SHOUTY_START_RE.search(x or ""):
            reject_stats["texts"]["bad_site_fit"] += 1
            _push_example("texts", "bad_site_fit", x)
            return False
        _xl = x.lower()
        _nk = _variant_norm_key(x)
        if _xl in seen_x or (_nk and _nk in seen_x_norm):
            reject_stats["texts"]["duplicate"] += 1
            _push_example("texts", "duplicate", x)
            return False
        seen_x.add(_xl)
        if _nk:
            seen_x_norm.add(_nk)
        good_x.append(x)
        _push_accepted("texts", x)
        return True

    for t in raw_titles:
        _accept_title(t)
    for x in raw_texts:
        _accept_text(x)
    for s in (raw_sl.get("sitelinks") or []):
        if isinstance(s, dict):
            ti = (s.get("title") or "").strip()
            de = (s.get("description") or "").strip()
            tk = A._sitelink_title_key(ti)
            dk = A._sitelink_desc_key(de)
            if not (ti and _ok(ti) and _ok(de)):
                reject_stats["sitelinks"]["bad_site_fit"] += 1
                _push_example("sitelinks", "bad_site_fit", f"{ti} — {de}".strip(" —"))
            elif tk in seen_sl:
                reject_stats["sitelinks"]["duplicate_title"] += 1
                _push_example("sitelinks", "duplicate_title", f"{ti} — {de}".strip(" —"))
            elif dk and dk in seen_sl_desc:
                reject_stats["sitelinks"]["duplicate_description"] += 1
                _push_example("sitelinks", "duplicate_description", f"{ti} — {de}".strip(" —"))
            else:
                seen_sl.add(tk)
                if dk:
                    seen_sl_desc.add(dk)
                good_sl.append({"title": ti, "description": de})
                _push_accepted("sitelinks", f"{ti} — {de}".strip(" —"))

    # Точечный retry: если после первого fan-out уникальности не хватает, просим M3 догенерировать
    # именно недостающие ЗАГОЛОВКИ/БЫСТРЫЕ ССЫЛКИ с запретом на уже предложенные варианты.
    if (not fast_mode) and len(good_t) < title_target_n:
        try:
            used_retry_titles = True
            title_retry_rules = [
                f"Каждый заголовок держи ближе к верхней границе длины: {max(min_t, A.TITLE_MAX - 8)}–{A.TITLE_MAX} символов.",
                "В КАЖДОМ заголовке явно упоминай бренд кампании.",
                "В КАЖДОМ заголовке нужен кредитный угол: кредит, платёж, взнос, одобрение банка или автокредит.",
                "Не повторяй один и тот же тип оффера. Если уже была распродажа, дай другой оффер: платёж, одобрение, взнос, КАСКО, трейд-ин.",
                "Если пишешь кредитный платеж через «кредит/платеж от N ₽», N должен быть только 9 000-15 000 ₽/мес. Не пиши сотни тысяч после «кредит от».",
                "Если используешь скидку/выгоду в процентах, держи один и тот же процент во всём наборе.",
            ]
            msgs_t_retry = A.build_titles_messages(
                agent, ctx, item=item, avoid=(avoid0 + good_t), brand=brand, extra_rules=title_retry_rules)
            text_t_retry, err_t_retry = _m3_complete_url(
                urls14[0], msgs_t_retry, max_tokens=280, temperature=0.92, top_p=0.92,
                repetition_penalty=1.17, tries=1, timeout=_M3_CONTENT_IDLE_TIMEOUT)
            if not err_t_retry:
                raw_t_retry = _promo_extract_json(text_t_retry) or {}
                retry_titles, retry_title2 = _extract_title_candidates(raw_t_retry)
                if not good_t2 and retry_title2:
                    good_t2 = retry_title2[:A.TITLE2_MAX]
                for t in retry_titles:
                    if _accept_title(t) and len(good_t) >= title_target_n:
                        break
        except Exception:  # noqa: BLE001
            pass

    if (not fast_mode) and len(good_x) < A.TEXTS_N:
        try:
            used_retry_texts = True
            text_retry_rules = [
                f"Каждый текст держи ближе к верхней границе длины: {max(min_x, A.TEXT_MAX - 10)}–{A.TEXT_MAX} символов.",
                "В КАЖДОМ тексте явно упоминай бренд кампании.",
                "Нужны 2–3 законченных предложения, а не короткий лозунг.",
                "Обязательно сочетай кредитный оффер с выгодой и заверши явным CTA.",
                "Не используй формулировки про цену б/у на новых авто и не скатывайся в общий текст без бренда.",
            ]
            msgs_x_retry = A.build_texts_messages(
                agent, ctx, item=item, brand=brand, avoid=good_x, extra_rules=text_retry_rules)
            text_x_retry, err_x_retry = _m3_complete_url(
                urls14[1 % len(urls14)], msgs_x_retry, max_tokens=240,
                temperature=0.92, top_p=0.92, repetition_penalty=1.17,
                tries=1, timeout=_M3_CONTENT_IDLE_TIMEOUT)
            if not err_x_retry:
                raw_x_retry = _promo_extract_json(text_x_retry) or {}
                for x in _extract_text_candidates(raw_x_retry):
                    if _accept_text(x) and len(good_x) >= A.TEXTS_N:
                        break
        except Exception:  # noqa: BLE001
            pass

    if (not fast_mode) and len(good_sl) < A.SITELINKS_N:
        try:
            used_retry_sitelinks = True
            msgs_sl_retry = A.build_sitelinks_messages(
                agent, ctx, item=item, brand=brand,
                avoid_titles=[s.get("title") or "" for s in good_sl],
                avoid_descriptions=[s.get("description") or "" for s in good_sl],
            )
            text_sl_retry, err_sl_retry = _m3_complete_url(
                urls14[2 % len(urls14)], msgs_sl_retry, max_tokens=420,
                temperature=0.92, top_p=0.92, repetition_penalty=1.17,
                tries=1, timeout=_M3_CONTENT_IDLE_TIMEOUT)
            if not err_sl_retry:
                raw_sl_retry = _promo_extract_json(text_sl_retry) or {}
                for s in (raw_sl_retry.get("sitelinks") or []):
                    if isinstance(s, dict):
                        ti = (s.get("title") or "").strip()
                        de = (s.get("description") or "").strip()
                        tk = A._sitelink_title_key(ti)
                        dk = A._sitelink_desc_key(de)
                        if not (ti and _ok(ti) and _ok(de)):
                            reject_stats["sitelinks"]["bad_site_fit"] += 1
                            _push_example("sitelinks", "bad_site_fit", f"{ti} — {de}".strip(" —"))
                        elif tk in seen_sl:
                            reject_stats["sitelinks"]["duplicate_title"] += 1
                            _push_example("sitelinks", "duplicate_title", f"{ti} — {de}".strip(" —"))
                        elif dk and dk in seen_sl_desc:
                            reject_stats["sitelinks"]["duplicate_description"] += 1
                            _push_example("sitelinks", "duplicate_description", f"{ti} — {de}".strip(" —"))
                        else:
                            seen_sl.add(tk)
                            if dk:
                                seen_sl_desc.add(dk)
                            good_sl.append({"title": ti, "description": de})
                            _push_accepted("sitelinks", f"{ti} — {de}".strip(" —"))
                            if len(good_sl) >= A.SITELINKS_N:
                                break
        except Exception:  # noqa: BLE001
            pass

    # 72B-патч: если 14B дало < 60% нужного хоть в одном разделе — зовём 72B (полный промпт)
    need_72b = ((not fast_mode) and
                (len(good_t) < max(2, title_target_n * 3 // 5)
                 or len(good_x) < max(1, A.TEXTS_N * 3 // 5)
                 or len(good_sl) < max(3, A.SITELINKS_N * 3 // 5)))
    if need_72b:
        msgs72 = A.build_campaign_messages(agent, ctx, item=item, avoid=avoid0, brand=brand)
        text72, err72 = _m3_complete_url(_M3_LLM_URL_72B, msgs72, max_tokens=800,
                                         temperature=0.7, top_p=0.9, repetition_penalty=1.15,
                                         timeout=_M3_CONTENT_IDLE_TIMEOUT)
        if err72:
            last_err = err72
        else:
            raw72 = _promo_extract_json(text72)
            if raw72:
                c72, _ = A.validate_campaign(raw72, agent, site_type=st)
                if not good_t2 and c72.get("title2"):
                    good_t2 = c72["title2"]
                for t in c72.get("titles") or []:
                    _accept_title(t)
                for x in c72.get("texts") or []:
                    _accept_text(x)
                for s in c72.get("sitelinks") or []:
                    ti = (s.get("title") or "").strip()
                    de = (s.get("description") or "").strip()
                    tk = A._sitelink_title_key(ti)
                    dk = A._sitelink_desc_key(de)
                    if ti and _ok(ti) and _ok(de) and tk not in seen_sl and (not dk or dk not in seen_sl_desc):
                        seen_sl.add(tk)
                        if dk:
                            seen_sl_desc.add(dk)
                        good_sl.append(s)
            else:
                last_err = last_err or "72B вернул не-JSON"

    # Repair-loop: если фильтры выкинули часть контента, добираем снова до полного комплекта.
    # Это не бесконечный цикл: несколько M3-раундов + ниже deterministic fallback, который обязан
    # закрыть остаток. Так UI не получает пустые поля, даже если M3 стабильно отдаёт мусор.
    for _repair_i in range(0 if fast_mode else 3):
        need_titles = len(good_t) < title_target_n
        need_texts = len(good_x) < A.TEXTS_N
        need_sitelinks = len(good_sl) < A.SITELINKS_N
        if not (need_titles or need_texts or need_sitelinks):
            break
        repair_rounds_used += 1
        if need_titles:
            try:
                used_retry_titles = True
                title_retry_rules = [
                    f"Нужно добрать ещё {title_target_n - len(good_t)} заголовков. Верни только новые варианты.",
                    f"Каждый заголовок {max(min_t, A.TITLE_MAX - 8)}–{A.TITLE_MAX} символов, с цифрой и кредитным УТП.",
                    "Не повторяй смысл уже принятых заголовков и не меняй только одно слово.",
                    "Не используй резину/шины на 1 сезон, кредит до процента скидки, условия кредитования до суммы.",
                ]
                if brand:
                    title_retry_rules.append("В каждом заголовке явно упоминай бренд кампании.")
                msgs_t_repair = A.build_titles_messages(
                    agent, ctx, item=item, avoid=(avoid0 + good_t), brand=brand,
                    extra_rules=title_retry_rules)
                text_t_repair, err_t_repair = _m3_complete_url(
                    urls14[_repair_i % len(urls14)], msgs_t_repair,
                    max_tokens=320, temperature=0.95, top_p=0.92, repetition_penalty=1.2,
                    tries=1, timeout=_M3_CONTENT_IDLE_TIMEOUT)
                if not err_t_repair:
                    raw_t_repair = _promo_extract_json(text_t_repair) or {}
                    repair_titles, repair_title2 = _extract_title_candidates(raw_t_repair)
                    if not good_t2 and repair_title2:
                        good_t2 = repair_title2[:A.TITLE2_MAX]
                    for t in repair_titles:
                        if _accept_title(t) and len(good_t) >= title_target_n:
                            break
            except Exception:  # noqa: BLE001
                pass
        if need_texts:
            try:
                used_retry_texts = True
                text_retry_rules = [
                    f"Нужно добрать ещё {A.TEXTS_N - len(good_x)} текстов. Верни только новые варианты.",
                    f"Каждый текст {max(min_x, A.TEXT_MAX - 10)}–{A.TEXT_MAX} символов, с кредитным УТП и CTA.",
                    "Не повторяй смысл уже принятых текстов и не используй чужие марки/города.",
                ]
                if brand:
                    text_retry_rules.append("В каждом тексте явно упоминай бренд кампании.")
                msgs_x_repair = A.build_texts_messages(
                    agent, ctx, item=item, brand=brand, avoid=good_x,
                    extra_rules=text_retry_rules)
                text_x_repair, err_x_repair = _m3_complete_url(
                    urls14[(_repair_i + 1) % len(urls14)], msgs_x_repair,
                    max_tokens=260, temperature=0.95, top_p=0.92, repetition_penalty=1.2,
                    tries=1, timeout=_M3_CONTENT_IDLE_TIMEOUT)
                if not err_x_repair:
                    raw_x_repair = _promo_extract_json(text_x_repair) or {}
                    for x in _extract_text_candidates(raw_x_repair):
                        if _accept_text(x) and len(good_x) >= A.TEXTS_N:
                            break
            except Exception:  # noqa: BLE001
                pass
        if need_sitelinks:
            try:
                used_retry_sitelinks = True
                msgs_sl_repair = A.build_sitelinks_messages(
                    agent, ctx, item=item, brand=brand,
                    avoid_titles=[s.get("title") or "" for s in good_sl],
                    avoid_descriptions=[s.get("description") or "" for s in good_sl],
                )
                text_sl_repair, err_sl_repair = _m3_complete_url(
                    urls14[(_repair_i + 2) % len(urls14)], msgs_sl_repair,
                    max_tokens=440, temperature=0.95, top_p=0.92, repetition_penalty=1.2,
                    tries=1, timeout=_M3_CONTENT_IDLE_TIMEOUT)
                if not err_sl_repair:
                    raw_sl_repair = _promo_extract_json(text_sl_repair) or {}
                    for s in (raw_sl_repair.get("sitelinks") or []):
                        if not isinstance(s, dict):
                            continue
                        ti = (s.get("title") or "").strip()
                        de = (s.get("description") or "").strip()
                        tk = A._sitelink_title_key(ti)
                        dk = A._sitelink_desc_key(de)
                        if not (ti and _ok(ti) and _ok(de)) or _bad_ad_sitelink(ti, de):
                            reject_stats["sitelinks"]["bad_site_fit"] += 1
                            _push_example("sitelinks", "bad_site_fit", f"{ti} — {de}".strip(" —"))
                        elif tk in seen_sl:
                            reject_stats["sitelinks"]["duplicate_title"] += 1
                            _push_example("sitelinks", "duplicate_title", f"{ti} — {de}".strip(" —"))
                        elif dk and dk in seen_sl_desc:
                            reject_stats["sitelinks"]["duplicate_description"] += 1
                            _push_example("sitelinks", "duplicate_description", f"{ti} — {de}".strip(" —"))
                        else:
                            seen_sl.add(tk)
                            if dk:
                                seen_sl_desc.add(dk)
                            good_sl.append({"title": ti, "description": de})
                            _push_accepted("sitelinks", f"{ti} — {de}".strip(" —"))
                            if len(good_sl) >= A.SITELINKS_N:
                                break
            except Exception:  # noqa: BLE001
                pass

    if not last_err:
        last_err = "; ".join(e for e in [err_t, err_x, err_sl] if e) or None

    # Гарантия длины и количества: чего не хватило — добиваем ПОЛНЫМИ примерами из корпуса слепка.
    # Если M3 совсем не дала валидного (good пусто) — сначала пробуем БД-библиотеку слепка,
    # иначе собираем ВЕСЬ контент из код-корпуса слепка (не падаем).
    content, warns = A.assemble_campaign(good_t, good_x, good_sl, agent, site_type=st, brand=brand)

    def _final_fill_campaign_content(c: dict) -> dict:
        """Финальный гард после M3/assemble: количество, длина, бренд и запретные фразы."""
        bname = _display_brand(brand or "Авто")
        btext = bname if len(bname) <= 9 else bname.split()[0]
        if _bu_site:
            title_fillers = [
                f"Платеж от 9 000 ₽/мес. {bname} с пробегом в кредит онлайн" if brand else "Платеж от 9 000 ₽/мес. Авто с пробегом в кредит онлайн",
                f"Выгода 45% на {bname} с пробегом. Кредит от 15 банков" if brand else "Выгода 45% на авто с пробегом. Кредит от 15 банков",
                f"Трейд-ин за 1 день. {bname} с пробегом в кредит онлайн" if brand else "Трейд-ин за 1 день. Авто с пробегом в кредит онлайн",
                f"КАСКО на 1 год. {bname} с пробегом в кредит онлайн" if brand else "КАСКО на 1 год. Авто с пробегом в кредит онлайн",
                f"Решение банка за 30 минут. {bname} с пробегом онлайн" if brand else "Решение банка за 30 минут. Авто с пробегом онлайн",
                f"Кредит на {bname} с пробегом от 9 000 ₽/мес онлайн" if brand else "Кредит на авто с пробегом от 9 000 ₽/мес онлайн",
                f"{bname} с пробегом. Одобрение кредита за 30 минут" if brand else "Авто с пробегом. Одобрение кредита за 30 минут",
                f"Купить {bname} с пробегом в кредит за 1 день онлайн" if brand else "Купить авто с пробегом в кредит за 1 день онлайн",
                f"{bname} с пробегом. Кредит по 2 документам онлайн" if brand else "Авто с пробегом. Кредит по 2 документам онлайн",
                f"{bname} с пробегом. КАСКО на 1 год при кредите онлайн" if brand else "Авто с пробегом. КАСКО на 1 год при кредите онлайн",
                f"Кредит на {bname} с пробегом. Подбор от 15 банков" if brand else "Кредит на авто с пробегом. Подбор от 15 банков",
                f"{bname} с пробегом в наличии. Кредит за 30 минут" if brand else "Авто с пробегом в наличии. Кредит за 30 минут",
                f"Трейд-ин зачтём в кредит. {bname} за 1 день" if brand else "Трейд-ин зачтём в кредит. Авто за 1 день",
                f"КАСКО на 1 год при кредите на {bname}" if brand else "КАСКО на 1 год при кредите на авто",
                f"{bname} с пробегом 2025. Заявка онлайн" if brand else "Авто с пробегом 2025. Заявка онлайн",
            ]
            text_fillers = [
                f"{btext} с пробегом в наличии. Поможем оформить кредит онлайн. Оставьте заявку!" if brand else "Авто с пробегом в наличии. Поможем оформить кредит. Оставьте заявку!",
                f"{btext} с пробегом в кредит. Первый взнос 0 ₽. Узнайте условия покупки онлайн!" if brand else "Авто с пробегом в кредит. Первый взнос 0 ₽. Узнайте условия покупки онлайн!",
                f"{btext} с пробегом. Трейд-ин и кредитные условия. Оставьте заявку онлайн!" if brand else "Авто с пробегом. Трейд-ин и кредитные условия. Оставьте заявку онлайн!",
                f"{btext} с пробегом от 9 000 ₽/мес. Подберите кредит онлайн сегодня за 30 минут!" if brand else "Авто с пробегом от 9 000 ₽/мес. Подберите кредит онлайн за 30 минут!",
                f"{btext} с пробегом. КАСКО на 1 год при кредите. Оставьте заявку сегодня!" if brand else "Авто с пробегом. КАСКО на 1 год при кредите. Оставьте заявку онлайн!",
            ]
        else:
            title_fillers = [
                f"Платеж от 9 000 ₽/мес. {bname} в кредит онлайн сегодня",
                f"Выгода 45% на {bname}. Кредит от 15 банков онлайн",
                f"Трейд-ин за 1 день. {bname} в кредит онлайн сегодня",
                f"КАСКО на 1 год. {bname} в кредит при покупке онлайн",
                f"Решение банка за 30 минут. {bname} онлайн сегодня",
                f"{bname} в кредит. Одобрение онлайн за 30 минут",
                f"Купить {bname} в кредит. Платеж от 9 000 ₽/мес",
                f"Новые {bname} 2025 в кредит. Выгода до 45%",
                f"{bname} в кредит. КАСКО на 1 год при покупке онлайн",
                f"Трейд-ин зачтём в кредит. {bname} за 1 день",
                f"{bname} в кредит. Одобрение и КАСКО в день заявки",
                f"{bname} в наличии. Кредит от 15 банков онлайн сегодня",
                f"Купить {bname}. Автокредит от 15 банков онлайн",
                f"{bname} по акции 2025. Кредитное одобрение онлайн",
                f"Решение банка за 30 минут. {bname} онлайн",
                f"{bname} 2025 в кредит. Первый взнос 0 ₽ онлайн",
                f"{bname} по акции. Платеж от 9 000 ₽/мес онлайн сегодня",
                f"Кредит на {bname} 2025. Одобрение за 30 минут онлайн",
                f"{bname} с выгодой 45%. Кредит от 15 банков онлайн",
                f"{bname} в кредит. Платеж от 9 000 ₽/мес онлайн сегодня",
                f"КАСКО на 1 год при покупке {bname} в кредит",
                f"Купить {bname} в кредит. Одобрение онлайн за 30 минут",
            ] if brand else [
                "Платеж от 9 000 ₽/мес. Новое авто в кредит онлайн",
                "Выгода 45% на новый авто. Кредит от 15 банков",
                "Трейд-ин за 1 день. Новое авто в кредит онлайн",
                "КАСКО на 1 год при покупке авто в кредит",
                "Решение банка за 30 минут. Новое авто онлайн",
                "Новые авто в кредит. Одобрение онлайн за 30 минут",
                "Купить новый авто в кредит. Выгода до 45% сегодня",
                "Авто в кредит. Платеж от 9 000 ₽/мес онлайн",
                "Новые авто в кредит. Первый взнос 0 ₽ онлайн",
                "Трейд-ин зачтём в кредит. Новое авто за 1 день",
                "Авто в наличии. Кредит от 15 банков онлайн",
                "Купить авто в кредит. КАСКО на 1 год при покупке",
                "Авто по акции. Кредитное одобрение за 30 минут",
                "Кредит на новое авто. Решение от 15 банков онлайн",
                "Автокредит на новый авто. Платеж от 9 000 ₽/мес",
                "Новые авто 2025. Кредит от 15 банков онлайн",
            ]
            text_fillers = [
                f"{btext} в наличии. Поможем оформить кредит и КАСКО онлайн. Оставьте заявку!",
                f"{btext} в кредит. Первый взнос 0 ₽. Подберите условия покупки онлайн сегодня!",
                f"{btext} с выгодой по кредиту до 45%. Оставьте заявку онлайн сегодня!",
                f"Оформите {btext} в кредит без первого взноса. Оставьте заявку онлайн сегодня!",
                f"{btext} по акции 2025. Трейд-ин и кредитные условия. Узнайте выгоду онлайн!",
                f"Кредит на {btext} от 15 банков. Рассчитаем платеж и условия онлайн сегодня!",
            ] if brand else [
                "Новые авто в наличии. Поможем оформить кредит и КАСКО. Оставьте заявку!",
                "Авто в кредит. Первый взнос 0 ₽. Подберите условия покупки онлайн сегодня!",
                "Авто с выгодой по кредиту до 45%. Оставьте заявку онлайн сегодня!",
                "Оформите новый авто в кредит без первого взноса. Оставьте заявку онлайн!",
                "Новые авто по акции. Трейд-ин и кредитные условия. Узнайте выгоду онлайн!",
                "Кредит на новое авто от 15 банков. Подберите условия онлайн сегодня!",
            ]
        sitelink_fillers = [  # все заголовки ≥ SITELINK_TITLE_TARGET_MIN=22 (fix 1a, 2026-07-02)
            {"title": "Кредит за 30 минут онлайн", "description": "Подберем условия от 15 банков онлайн за 30 минут"},
            {"title": "Платеж от 9 000 ₽ в месяц", "description": "Рассчитайте платеж от 9 000 ₽/мес для вашей заявки"},
            {"title": "Автокредит от 15 банков", "description": "Сравним предложения 15 банков и подберем вариант"},
            {"title": "КАСКО на 1 год бесплатно", "description": "КАСКО на 1 год. Дарим при покупке авто в кредит"},
            {"title": "Трейд-ин за 1 рабочий день", "description": "Оценим авто за 1 день и зачтём в покупку нового"},
            {"title": "Авто в наличии сегодня", "description": "Подберем авто под бюджет от 9 000 ₽/мес онлайн"},
            {"title": "Первый взнос 0 ₽ онлайн", "description": "Первый взнос 0 ₽. Кредит оформим за 1 день онлайн"},
            {"title": "Заявка онлайн за 15 минут", "description": "Оставьте заявку и получите расчет за 15 минут"},
            {"title": "Господдержка авто 2025", "description": "Проверим доступные программы. Выгода до 30% с 2025"},
            {"title": "Подарок каждому покупателю", "description": "Расскажем о бонусах от 5 000 ₽ при оформлении авто"},
            {"title": "Консультация менеджера", "description": "Менеджер ответит за 5 минут и подскажет следующий шаг"},
        ]
        _foreign_brands, _foreign_cities = _title2_blocklist()
        _ctx_city_words = set(re.sub(r"[^\wа-яё]+", " ", (ctx.get("city") or "").lower()).split())
        _foreign_cities = (set(_foreign_cities) | set(_RU_CITIES)) - _ctx_city_words

        def _brand_ok_line(s: str) -> bool:
            return (not _brand_re) or bool(_brand_re.search(s or ""))

        def _credit_offer_ok_line(s: str) -> bool:
            return bool(_DIRECT_CREDIT_RE.search(s or ""))

        def _site_ok_line(s: str) -> bool:
            return not ((_bu_site and A._bad_for_bu(s)) or (_new_only_site and A._bad_for_new(s)))

        def _common_ok_line(s: str) -> bool:
            if _brand_re:
                return True
            words = set(re.sub(r"[^\wа-яё]+", " ", (s or "").lower()).split())
            return not A._SITELINK_VEHICLE_RE.search(s or "") and not (words & _foreign_brands) and not (words & _foreign_cities)

        def _title_quality_bucket(s: str) -> str:
            x = (s or "").lower()
            if re.search(r"трейд-?ин|обмен", x):
                return "tradein"
            if re.search(r"каско|подар|шин|комплект", x):
                return "gift"
            if re.search(r"плат[её]ж|/мес", x):
                return "payment"
            if re.search(r"взнос", x):
                return "downpay"
            if re.search(r"одобр|решение\\s+банк|15\\s+банк", x):
                return "approval"
            if re.search(r"распрод|склад|стоянк|завоз", x):
                return "sale"
            if re.search(r"скидк|выгод|акци|%", x):
                return "discount"
            if re.search(r"в\\s+наличии|2025", x):
                return "availability"
            if re.search(r"кредит|автокредит", x):
                return "credit"
            return "other"

        def _take_titles(lines: list[str]) -> list[str]:
            candidates, seen = [], set()
            for raw in list(lines or []) + title_fillers:
                s = A._clean_line(raw, A.TITLE_MAX)
                if (not s or len(s) < A.TITLE_TARGET_MIN or _bad_ad_title(s)
                        or _bad_num(s) or _has_salon(s) or _bad_inventory(s)
                        or not re.search(r"\d", s)
                        or not _brand_ok_line(s) or not _credit_offer_ok_line(s)
                        or not _site_ok_line(s) or not _common_ok_line(s)
                        or s.lower() in seen):
                    continue
                seen.add(s.lower())
                candidates.append(s)
            out: list[str] = []
            used_norm: set[str] = set()
            bucket_counts: dict[str, int] = {}
            first_counts: dict[str, int] = {}

            def _try_take(s: str, strict_bucket: bool, strict_first: bool) -> bool:
                if len(out) >= title_target_n:
                    return False
                nk = _variant_norm_key(s)
                if nk and nk in used_norm:
                    return False
                bucket = _title_quality_bucket(s)
                first = s.split()[0].lower().strip(".,") if s.split() else ""
                if strict_bucket:
                    if bucket_counts.get(bucket, 0) >= 1:
                        return False
                if strict_first:
                    if first and first_counts.get(first, 0) >= 2:
                        return False
                out.append(s)
                if nk:
                    used_norm.add(nk)
                bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
                if first:
                    first_counts[first] = first_counts.get(first, 0) + 1
                return True

            for s in candidates:
                _try_take(s, strict_bucket=True, strict_first=True)
            for s in candidates:
                _try_take(s, strict_bucket=False, strict_first=True)
                if len(out) >= title_target_n:
                    break
            for s in candidates:
                _try_take(s, strict_bucket=False, strict_first=False)
                if len(out) >= title_target_n:
                    break
            return out

        def _take_texts(lines: list[str]) -> list[str]:
            out, seen = [], set()
            for raw in list(lines or []) + text_fillers:
                s = A._clean_line(raw, A.TEXT_MAX)
                if (not s or len(s) < A.TEXT_TARGET_MIN or len(s) > A.TEXT_MAX
                        or _bad_num(s) or _has_salon(s) or _bad_inventory(s)
                        or not _brand_ok_line(s) or re.search(r"(?i)\bбез\s+документ", s)
                        or not _credit_offer_ok_line(s) or not _site_ok_line(s)
                        or not _common_ok_line(s) or _bad_ad_text(s)
                        or s.lower() in seen):
                    continue
                seen.add(s.lower())
                out.append(s)
                if len(out) >= A.TEXTS_N:
                    break
            return out

        def _take_sitelinks(lines: list[dict]) -> list[dict]:
            candidates = []
            for raw in list(lines or []) + sitelink_fillers:
                if not isinstance(raw, dict):
                    continue
                title = A._clean_line(raw.get("title") or "", A.SITELINK_TITLE_MAX, sitelink=True)
                desc = A._clean_line(raw.get("description") or "", A.SITELINK_DESC_MAX, sitelink=True)
                if (not title or not desc or len(title) < A.SITELINK_TITLE_MIN_ACCEPT  # порог приёмки (≥18); цель генерации = SITELINK_TITLE_TARGET_MIN
                        or _bad_ad_sitelink(title, desc)
                        or _bad_num(title) or _bad_num(desc) or _has_salon(title) or _has_salon(desc)
                        or _bad_inventory(title) or _bad_inventory(desc)
                        or not _site_ok_line(title) or not _site_ok_line(desc)
                        or not _common_ok_line(title) or not _common_ok_line(desc)):
                    continue
                candidates.append({"title": title, "description": desc})
            # Three-pass number-gate: ensure descriptions carry concrete figures.
            # Pass 1 (strict) : both title AND desc contain a digit.
            # Pass 2 (loose)  : at least desc contains a digit (main quality signal).
            # Pass 3 (any)    : no digit requirement — preserves existing fallback behaviour.
            _hd = lambda s: bool(re.search(r"\d", s))
            result = A._dedup_sitelinks(
                [c for c in candidates if _hd(c["title"]) and _hd(c["description"])],
                st, A.SITELINKS_N,
            )
            if len(result) < A.SITELINKS_N:
                result = A._dedup_sitelinks(
                    [c for c in candidates if _hd(c["description"])],
                    st, A.SITELINKS_N,
                )
            if len(result) < A.SITELINKS_N:
                result = A._dedup_sitelinks(candidates, st, A.SITELINKS_N)
            return result

        c = dict(c or {})
        c["titles"] = _take_titles(c.get("titles") or [])[:title_target_n]
        c["texts"] = _take_texts(c.get("texts") or [])
        c["sitelinks"] = _take_sitelinks(c.get("sitelinks") or [])
        return c

    content = _final_fill_campaign_content(content)
    if good_t2:                                            # ИИ дал title2 — подставляем (фолбэк: ""→_next_title2() при создании групп)
        content["title2"] = good_t2
    diag = {k: {rk: rv for rk, rv in v.items() if rv} for k, v in reject_stats.items()}
    diag = {k: v for k, v in diag.items() if v}
    m3_debug = {
        "raw_counts": {
            "titles_14b": len(raw_titles),
            "texts_14b": len(raw_texts),
            "sitelinks_14b": len(raw_sl.get("sitelinks") or []),
        },
        "accepted_before_assemble": {
            "titles": len(good_t),
            "texts": len(good_x),
            "sitelinks": len(good_sl),
            "title2": bool(good_t2),
        },
        "accepted_examples_before_assemble": {k: v for k, v in accepted_examples.items() if v},
        "retry_used": {
            "titles": used_retry_titles,
            "texts": used_retry_texts,
            "sitelinks": used_retry_sitelinks,
            "fallback_72b": bool(need_72b),
            "repair_rounds": repair_rounds_used,
        },
        "rejects": diag,
        "reject_examples": {k: v for k, v in reject_examples.items() if v},
    }
    if diag:
        warns = list(warns) + [f"diag M3 filter: {json.dumps(diag, ensure_ascii=False)}"]
    fallback = (not good_t and not good_x)
    if fallback:
        lib = _slepok_content_get(agent_key or "", st, "campaign")
        if isinstance(lib, dict) and (lib.get("titles") or lib.get("texts")):
            content, _lib_warns = A.assemble_campaign(
                lib.get("titles") or [],
                lib.get("texts") or [],
                lib.get("sitelinks") or [],
                agent,
                site_type=st,
                brand=brand,
            )
            content = _final_fill_campaign_content(content)
            content["titles"] = (content.get("titles") or [])[:title_target_n]
            content["texts"] = (content.get("texts") or [])[:A.TEXTS_N]
            content["sitelinks"] = (content.get("sitelinks") or [])[:A.SITELINKS_N]
            warns = [f"⚠ M3 недоступна — контент из БД-библиотеки слепка «{agent['name']}»"]
        else:
            warns = [f"⚠ M3 недоступна ({last_err or 'нет валидного ответа'}) — контент собран из слепка «{agent['name']}»"] + warns
    # ── C. LLM-судья УТП-дублей/релевантности (тот же _llm_pair_for), НА ГЕНЕРАЦИИ ──────
    # Осознанно НА ГЕНЕРАЦИИ, а не на live-чтении каждой проверки набора: +1 короткий LLM-вызов
    # на РК (~центы OpenRouter, ~2-4с), вместо повторного прогона по всему аккаунту. Судья
    # fail-open (недоступен → контент как есть); при неодобрении после ретраев — warn-маркер
    # UTP_RELEVANCE_FAILED (не роняем создание черновика, но сигнал виден). Пропуск: fast_mode,
    # item.skip_utp_judge, фолбэк-контент, пустые заголовки/тексты.
    utp_judge = None
    _titles_now = content.get("titles") or []
    _texts_now = content.get("texts") or []
    if (not fast_mode and not fallback and not item.get("skip_utp_judge")
            and _titles_now and _texts_now):
        try:
            from . import content_quality as CQ
            provider = str(item.get("llm_provider") or "openrouter")
            site_ctx = {"domain": ctx.get("domain") or "", "site_type": st,
                        "city": ctx.get("city") or "", "brand": brand}
            jr = CQ.audit_and_regen_utp(agent, ctx, brand=brand, titles=_titles_now,
                                        texts=_texts_now, site_ctx=site_ctx, provider=provider)
            utp_judge = {"judged": jr.get("judged"), "attempts": jr.get("attempts"),
                         "hard_fail": jr.get("hard_fail"), "issues": (jr.get("issues") or [])[:5]}
            if jr.get("judged") and not jr.get("hard_fail") and jr.get("attempts"):
                # судья одобрил ПОСЛЕ регенерации → прогоняем новые тексты через тот же
                # инвариант-гард (_final_fill), берём только если счётчики полные.
                cand = dict(content)
                cand["titles"] = jr.get("titles") or _titles_now
                cand["texts"] = jr.get("texts") or _texts_now
                cand = _final_fill_campaign_content(cand)
                if (len(cand.get("titles") or []) >= title_target_n
                        and len(cand.get("texts") or []) >= A.TEXTS_N):
                    if good_t2:
                        cand["title2"] = good_t2
                    content = cand
                    warns = list(warns) + [f"UTP-судья: контент перегенерирован ({jr.get('attempts')} поп.)"]
            elif jr.get("hard_fail"):
                warns = list(warns) + [
                    "⚠ UTP_RELEVANCE_FAILED: судья не одобрил дубли/релевантность после "
                    f"{jr.get('attempts')} попыток — {'; '.join((jr.get('issues') or [])[:3])}"]
        except Exception as e:  # noqa: BLE001  (fail-open: судья не должен ронять генерацию)
            utp_judge = {"error": str(e)[:160]}
    return {"ok": True, "agent": agent["name"], "login": login, "item": item,
            "brand": brand, "content": content,
            "title2": content.get("title2", ""), "utp_judge": utp_judge,
            "warnings": warns, "fallback": fallback, "m3_debug": m3_debug}

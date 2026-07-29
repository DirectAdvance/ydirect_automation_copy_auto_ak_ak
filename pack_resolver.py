"""M3 content-pack and slepok resolution service, independent from Flask wiring hub."""
from __future__ import annotations

import json
import re
import threading
import time
from flask import jsonify, request

from . import campaign as cmc
from . import kontent_pack as kp


def _missing(*_args, **_kwargs):
    raise RuntimeError("pack_resolver dependency is not configured")


_json = _missing
_ct_segment_map = _missing
_m3_llm_probe = _missing
_openrouter_probe = _missing
_openrouter_completion_probe = _missing
_touch_running_jobs_heartbeat = _missing


def configure(deps: dict) -> None:
    globals().update(deps)


# Лимиты «Уточнений» (callouts) в Яндекс.Директе.
_CALLOUT_MAX_EACH = 25            # длина одного уточнения
_CALLOUT_MAX_TOTAL_DESKTOP = 132  # суммарно на десктопе
_CALLOUT_MAX_TOTAL_MOBILE = 76    # суммарно на мобильных

_SLEPOK_KEY = {"слепок_павлов": "pavlov", "слепок_щербакова": "scherbakova",
               "слепок_крючкова": "kryuchkova", "слепок_терехов": "terehov",
               "слепок_караваев": "karavaev", "слепок_саламахин": "salamahin",
               "слепок_гордеева": "gordeeva", "слепок_зубакин": "zubakin",
               "слепок_чепелев": "chepelev", "слепок_тумашенко": "tumashenko",
               "слепок_кудерко": "kuderko", "слепок_gen_ses": "gen_ses",
               "слепок_dmp": "dmp", "слепок_питеркина": "piterkina",
               "слепок_автоск": "avto_sk", "слепок_автолайтбу": "avtolajt_bu",
               "слепок_сккрс": "sk_krs",
               # Имена после переименования 2026-07-16 (старые выше оставлены — БД/UI
               # могут отдавать их). Без этих строк «Слепок_Авто-СК» и «Слепок_sk-krs(бу)»
               # резолвились в "" → пустой ключ → пустой пак → generic-контент.
               "слепок_авто-ск": "avto_sk", "слепок_sk-krs(бу)": "sk_krs",
               "слепок_sk-krs": "sk_krs", "слепок_sk_krs": "sk_krs"}
_SLEPOK_CANONICAL = {"pavlov", "kryuchkova", "scherbakova", "terehov", "karavaev",
                      "salamahin", "gordeeva", "zubakin", "chepelev", "tumashenko",
                      "kuderko", "gen_ses", "dmp", "piterkina",
                      "avto_sk", "avtolajt_bu", "sk_krs"}


def _slepok_key_from_text(raw: str) -> str:
    """Best-effort: имя слепка/директолога из БД/UI → canonical ai_agents key."""
    s = (raw or "").strip().lower()
    if not s:
        return ""
    if s in _SLEPOK_KEY:
        return _SLEPOK_KEY[s]
    if s in _SLEPOK_CANONICAL:
        return s
    if "павлов" in s:
        return "pavlov"
    if "крючков" in s:
        return "kryuchkova"
    if "щербаков" in s:
        return "scherbakova"
    if "терехов" in s:
        return "terehov"
    if "караваев" in s:
        return "karavaev"
    if "саламахин" in s:
        return "salamahin"
    if "гордеев" in s:
        return "gordeeva"
    if "зубакин" in s:
        return "zubakin"
    if "чепелев" in s:
        return "chepelev"
    if "тумашенко" in s:
        return "tumashenko"
    if "кудерко" in s:
        return "kuderko"
    return ""


def _selected_slepok_key(raw: str) -> str:
    """Strict canonical key from the user's selected slepok field.

    Unlike ``_slepok_key_from_text`` this does not infer a slepok from an
    arbitrary surname. Auto-promo must follow the selected slepok only.
    """
    s = (raw or "").strip().lower()
    if not s:
        return ""
    if s in _SLEPOK_CANONICAL:
        return s
    label = re.sub(r"\s+", "_", s)
    return _SLEPOK_KEY.get(label, "")


# ── Статус локального индекса/зеркала контент-пака M3 ────────────────────
_M3_KONTENT_ROOT = "/opt/neuro_kontent"  # sshfs-монт папки нейродиректолога с M3


def _m3_content_status(timeout: float = 8.0) -> dict:
    """Статус контента M3 — теперь по ЛОКАЛЬНОМУ ИНДЕКСУ (структура пака закэширована локально,
    байты тянем точечно с таймаутом). НЕ ходит в sshfs → не виснет даже при перегруженной M3."""
    out = {"ok": False, "mount": "local-index", "source": "local-index",
           "coder": False, "feeds_models": 0, "detail": ""}
    res: dict = {}

    def _probe():
        try:
            res["status"] = kp.pack_status()
        except Exception as e:  # noqa: BLE001
            res["err"] = str(e)[:80]

    th = threading.Thread(target=_probe, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        out["detail"] = "индекс собирается…"
        return out
    if res.get("err"):
        out["detail"] = "недоступно: " + res["err"]
        return out
    st = res.get("status") or {}
    out["ok"] = bool(st.get("pack_root_exists"))
    out["coder"] = bool(st.get("feeds_exists"))
    out["feeds_models"] = int(st.get("feeds_models") or 0)
    out["write_ok"] = False
    out["read_only"] = True
    age = st.get("index_age_sec")
    age_txt = (f"{age // 60} мин назад" if isinstance(age, int) else "—")
    out["detail"] = (f"локальный индекс (обновлён {age_txt}) · фид-моделей {st.get('feeds_models', 0)} · "
                     f"байты из локального зеркала, M3/sshfs fallback · read-only") if out["ok"] else "индекс ещё не собран"
    return out


_M3_STATUS_CACHE: dict = {"at": 0.0, "data": None}
_M3_STATUS_TTL = 300                                      # кэш статуса M3 ~5 мин (polling 20 мин не дёргает чаще)




# Одна короткая перепроверка ИИ перед остановкой: раньше при обоих лёгших провайдерах гейт висел
# 6×10мин+1ч. Теперь: одна быстрая перепроверка (вдруг мигнуло), затем стоп до создания брака.
_M3_GATE_RECHECK_SEC = 20


def _m3_completion_gate_ok() -> bool:
    """True when M3 can actually generate at least one token."""
    try:
        from .llm_providers import m3_completion_preflight_ok  # noqa: PLC0415
    except ImportError:
        from llm_providers import m3_completion_preflight_ok  # type: ignore[no-redef]  # noqa: PLC0415
    try:
        return bool(m3_completion_preflight_ok(retries=1))
    except Exception:  # noqa: BLE001
        return False


def _m3_or_openrouter_gate_ok() -> bool:
    if _m3_llm_probe():
        return True
    if _m3_completion_gate_ok():
        print("[m3-gate] M3 health моргнул, но completion-probe OK — продолжаем", flush=True)
        return True
    if _openrouter_probe() and _openrouter_completion_probe():
        print("[m3-gate] M3 недоступен, OpenRouter жив → контент пойдёт через "
              "DeepSeek V4 Flash (платно)", flush=True)
        return True
    return False


def _m3_gate_wait(job: dict | None = None) -> bool:
    """Гейт создания РК. Провайдеров два (каскад _llm_pair_for): M3 и OpenRouter.
    - M3 жив → True.
    - M3 лёг, OpenRouter completion жив → True (фолбэк переключит генерацию на OpenRouter сам).
    - Оба недоступны → НЕ висим 6×10мин+1ч; делаем ОДНУ короткую перепроверку
      (_M3_GATE_RECHECK_SEC) на случай моргания и возвращаем False.
    True = продолжаем создание на доступном ИИ; False = джобу отменили или оба LLM недоступны."""
    if _m3_or_openrouter_gate_ok():
        return True
    # Оба провайдера легли. Короткая перепроверка, затем стоп до создания Direct-объектов.
    print(f"[m3-gate] M3 и OpenRouter недоступны — короткая перепроверка "
          f"{_M3_GATE_RECHECK_SEC} с перед остановкой создания", flush=True)
    if job is not None:
        job["note"] = "ИИ недоступен (M3+OpenRouter) — перепроверка перед остановкой создания"
    _t_end = time.time() + _M3_GATE_RECHECK_SEC
    while time.time() < _t_end:
        time.sleep(5)
        try:
            _touch_running_jobs_heartbeat()
        except Exception:  # noqa: BLE001
            pass
        if job is not None and job.get("cancel"):
            return False
    if job is not None:
        job.pop("note", None)
    if _m3_or_openrouter_gate_ok():
        print("[m3-gate] ИИ снова доступен после перепроверки — продолжаем на ИИ", flush=True)
        return True
    print("[m3-gate] ИИ по-прежнему недоступен — стоп до создания кампании", flush=True)
    return False


_COOKIES_STATUS_CACHE: dict = {"at": 0.0, "data": None}
_COOKIES_STATUS_TTL = 300.0


def _cookies_status_response():
    """Health агентских кук главпотока для бейджа в сайдбаре (под M3). {ok, alive, total, detail}.
    Probe = UacClient.link_info по куке каждого агентства (локальная → главпоток). Живость решает
    deny-лист: протухла ТОЛЬКО если получили однозначный need_reset/редирект в Паспорт; любая другая
    ошибка (в т.ч. «нет прав»/"No rights" на конкретного клиента) = сессия жива. Кэш 5 мин;
    ?force=1 — обход кэша (кнопка «⟳ Обновить»)."""
    now = time.time()
    _force = str(request.args.get("force") or "") in ("1", "true")
    cached = _COOKIES_STATUS_CACHE.get("data")
    if not _force and cached and (now - float(_COOKIES_STATUS_CACHE.get("at") or 0)) < _COOKIES_STATUS_TTL:
        out = dict(cached)
        out["cached"] = True
        return jsonify(out)
    # Probe агентской куки требует КЛИЕНТСКИЙ ulogin: до 5 клиентов на агентство из
    # local_gsheet_sites — ОДИН клиент давал ложные ✗, когда попадался отвязанный
    # (victoryagency14/porg-23yivon2, факт 2026-07-03; victorylotsofads1, факт 2026-07-06 —
    # Яндекс вернул «нет прав» ДВУМЯ разными формами: 403 {"code":54,"text":"Нет прав"} И
    # 401 {"code":0,"text":"No rights"} — allow-лист конкретных текстов не поспевает за формами).
    # Метод сменён на deny-лист: единственный ОДНОЗНАЧНЫЙ сигнал «кука мертва» — редирект в
    # Паспорт/need_reset (`{"code":null,"text":"need_reset","recoveryUrl":".../passport.yandex.ru/..."}`,
    # видели на протухшем `.secret/cookies.json`). Любая ДРУГАЯ ошибка (в т.ч. «нет прав»/"No rights"
    # на конкретного клиента) означает, что сессия АВТОРИЗОВАНА — просто нет доступа к ЭТОМУ ulogin,
    # это не признак протухания.
    # login_key в local_gsheet_sites иногда мусор ("Нет"/"Да"/пустая заглушка вместо реального
    # ulogin) — такой кандидат гарантированно даёт "чужую" ошибку (не "Нет прав"/code 54) и тратит
    # одну из немногих попыток впустую, из-за чего при неудачном порядке выборки (без ORDER BY)
    # аккаунт мог ошибочно попасть в "протухла", хотя кука рабочая. Пускаем в проверку только
    # похожие на реальный ulogin строки (латиница/цифры/дефис/подчёркивание, без кириллицы).
    _UAC_ULOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,}$")
    probe_logins: dict = {}
    try:
        conn = _victory_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT agency_account, login_key FROM public.local_gsheet_sites "
                        "WHERE coalesce(agency_account,'') NOT IN ('','None') "
                        "AND coalesce(login_key,'')<>'' ORDER BY login_key")
            for a, l in cur.fetchall():
                if not _UAC_ULOGIN_RE.match(l or ""):
                    continue
                lst = probe_logins.setdefault(a, [])
                if l not in lst and len(lst) < 5:
                    lst.append(l)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        probe_logins = {}
    parts, alive = [], 0
    accounts = tuple(getattr(cmc, "DEFAULT_COOKIE_ACCOUNTS", ()) or ())
    for acc in accounts:
        acc_ok, src = False, ""
        ulogins = probe_logins.get(acc) or [acc]
        for label, getter in (("локальная", cmc.load_cookie_local),
                              ("главпоток", cmc.fetch_cookie_glavpotok)):
            try:
                cookie = getter(acc)
            except Exception:  # noqa: BLE001
                cookie = None
            if not cookie:
                continue
            cookie_dead = False
            for ulogin in ulogins:
                try:
                    cmc.UacClient(cookie, ulogin).link_info("https://ya.ru")
                    acc_ok = True
                    break
                except Exception as pe:  # noqa: BLE001
                    body = str(getattr(pe, "body", "") or pe)
                    if "need_reset" in body or "Истек срок" in body or "Истёк срок" in body \
                            or "passport.yandex.ru/auth" in body:
                        cookie_dead = True   # ОДНОЗНАЧНЫЙ признак: сессия правда истекла
                        break
                    continue              # любая другая ошибка ("нет прав"/"No rights" и т.п. на
                                          # ЭТОГО клиента) — сессия жива, пробуем следующего ulogin
            if acc_ok:
                src = label
                break
            if not cookie_dead:
                # ни один ulogin не дал явного успеха, но и признака протухания не было —
                # кука отвечает предметно (не редиректит в Паспорт), считаем живой
                acc_ok, src = True, label
                break
        alive += 1 if acc_ok else 0
        parts.append(f"{acc}: {'✓ ' + src if acc_ok else '✗ протухла'}")
    dead = [p.split(":")[0] for p in parts if "✗" in p]
    data = {"ok": alive > 0, "alive": alive, "total": len(accounts), "dead": dead,
            "detail": " · ".join(parts), "checked_at": time.strftime("%H:%M")}
    _COOKIES_STATUS_CACHE["at"] = now
    _COOKIES_STATUS_CACHE["data"] = dict(data)
    data["cached"] = False
    return jsonify(data)


def _m3_status_response():
    """Лёгкий health M3 для ПОСТОЯННОГО индикатора в сайдбаре. {ok, detail, checked_at}. Кэш ~5 мин.
    ok = контент-индекс (sshfs) И LLM-эндпоинт живы — для генерации РК нужны ОБА (см. _m3_llm_probe).
    ?force=1 — обход кэша (кнопка «⟳ Обновить» на бейдже: без него клик возвращал старый статус)."""
    now = time.time()
    _force = str(request.args.get("force") or "") in ("1", "true")
    cached = _M3_STATUS_CACHE.get("data")
    if not _force and cached and (now - float(_M3_STATUS_CACHE.get("at") or 0)) < _M3_STATUS_TTL:
        out = dict(cached)
        out["cached"] = True
        return jsonify(out)
    st = _m3_content_status(timeout=6.0)
    content_ok = bool(st.get("ok"))
    llm_ok = _m3_llm_probe()
    detail = st.get("detail") or ""
    if content_ok and not llm_ok:
        detail = "⚠ LLM недоступна (mlx 8082 молчит) — генерация РК не пойдёт · " + detail
    from datetime import datetime
    out = {"ok": content_ok and llm_ok, "detail": detail,
           "checked_at": datetime.now().strftime("%H:%M"), "cached": False}
    _M3_STATUS_CACHE["at"] = now
    _M3_STATUS_CACHE["data"] = {k: out[k] for k in ("ok", "detail", "checked_at")}
    return jsonify(out)

def _pack_preview_response():
    """Предпросмотр: что именно мы возьмём из пака M3 для слепка×типа сайта.
    ?slepok=<key|Слепок_Имя>&site_type=<сегмент>. Read-only, ничего не создаёт."""
    raw = (request.args.get("slepok") or "").strip()
    slepok = _SLEPOK_KEY.get(raw.lower(), raw.lower())
    site_type = (request.args.get("site_type") or "").strip()
    out = {"slepok": slepok, "site_type": site_type, "tp": [],
           "totals": {"keywords": 0, "minus": 0, "callouts": 0, "images": 0,
                      "groups": 0, "groups_from_pack": 0}}
    if not slepok or not site_type:
        return jsonify({**out, "error": "slepok и site_type обязательны"})
    struct = _json("slepki_structure.json").get("directologists", [])
    dirr = next((d for d in struct if d.get("key") == slepok), None)
    if not dirr:
        return jsonify({**out, "error": f"слепок '{slepok}' не найден в структуре"})
    st = next((s for s in dirr.get("site_types", []) if s.get("name") == site_type), None)
    if not st:
        return jsonify({**out, "error": f"тип сайта '{site_type}' нет у слепка"})
    T = out["totals"]
    for tp in st.get("tp", []):
        tp_code = tp.get("code", "")
        tp_out = {"code": tp_code, "title": tp.get("title", ""), "groups": []}
        seen_ct = set()
        for grp in tp.get("groups", []):
            for it in grp.get("items", []):
                gc = it.get("gc", "")
                ct = _gc_ct(gc)
                key = (tp_code, ct)
                if key in seen_ct:        # один ct в tp читаем один раз
                    continue
                seen_ct.add(key)
                r = _pack_for_item(slepok, site_type, tp_code, gc)
                T["groups"] += 1
                if r["from"] == "pack":
                    T["groups_from_pack"] += 1
                T["keywords"] += len(r["keywords"])
                T["minus"] += len(r["minus"])
                T["callouts"] += len(r["callouts"])
                T["images"] += len(r["images"])
                tp_out["groups"].append({
                    "ct": ct, "model": r["model"], "tag": it.get("t", ""),
                    "keywords": len(r["keywords"]), "minus": len(r["minus"]),
                    "callouts": len(r["callouts"]), "images": len(r["images"]),
                    "from": r["from"],
                    "sample_kw": r["keywords"][:5],
                })
        if tp_out["groups"]:
            out["tp"].append(tp_out)
    return jsonify(out)


def _slepok_segment_counts_response():
    """Фактические счётчики групп по сегментам из живого M3-пака для слепка×типа сайта.
    ?slepok=<key>&site_type=<name>. Критерий совпадает с реальным созданием: считаем только
    ct у которых есть непустые positive-ключи в паке — ct без ключей пропускаем (как
    create_set_tp1_builders строка «if not data.get(positive): continue»)."""
    raw = (request.args.get("slepok") or "").strip()
    slepok = _SLEPOK_KEY.get(raw.lower(), raw.lower())
    site_type = (request.args.get("site_type") or "").strip()
    if not slepok or not site_type:
        return jsonify({"error": "slepok и site_type обязательны"})
    struct = _json("slepki_structure.json").get("directologists", [])
    dirr = next((d for d in struct if d.get("key") == slepok), None)
    if not dirr:
        return jsonify({"error": f"слепок '{slepok}' не найден"})
    st = next((s for s in dirr.get("site_types", []) if s.get("name") == site_type), None)
    if not st:
        return jsonify({"error": f"тип сайта '{site_type}' нет у слепка"})
    seg_map = _ct_segment_map()
    counts: dict = {}
    for tp_entry in st.get("tp", []):
        tp_code = tp_entry.get("code", "")
        m = re.match(r"^tp(\d+)$", tp_code)
        if not m:
            continue
        tpn = int(m.group(1))
        if tpn not in (1, 2, 4, 5):   # только сегментные tp (как _isSegmentTp в UI)
            continue
        pack = kp.gather(slepok, site_type, tp_code)
        seg_counts: dict = {}
        for ct, data in pack.items():
            if not data.get("positive"):
                continue               # тот же пропуск что при реальном создании
            seg = seg_map.get(ct, "Марки")
            seg_counts[seg] = seg_counts.get(seg, 0) + 1
        # pack_ok=False означает что gather вернул пустой результат (M3 недоступен или пак пуст).
        # Фронт использует это для защиты: при pack_ok=False не патчит счётчики → остаётся статика.
        counts[tp_code] = {"segs": seg_counts, "pack_ok": bool(pack)}
    return jsonify({"slepok": slepok, "site_type": site_type, "counts": counts})

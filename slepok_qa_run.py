"""slepok-qa: детерминированный прогон создания РК ПО ВСЕМ СЛЕПКАМ + PASS/FAIL отчёт.

Заменяет 15+ ручных сессий «проверка создания по слепкам». Промотировано из проверенного
_tmp_batch_driver.py (внутренние вызовы direct-приложения сохранены 1:1) + добавлен QA-вердикт.

ЗАПУСК на LXC101, WorkingDirectory=/opt/scripts/home/seoadvanced:
    DIRECT_ROLE=web /root/venv/bin/python3 -m direct.slepok_qa_run [--fast] [--only s1,s2] [--dry] [--jtimeout 5400]

Что делает per слепок × site_type:
  1. удаляет черновики тест-аккаунта → 2. /direct/api/set_plan (превью) →
  3. /direct/api/create_set_async (создание черновиков State=OFF, фон) →
  4. ждёт терминала джобы в public.direct_automation_jobs (Victory) →
  5. читает result.live_verification → вердикт PASS/WARN/FAIL.

КРИТЕРИЙ (live_verifier.verify_live_create_set):
  PASS  = live_verification.status=="pass" (0 error-issues) И result.failed==0
  WARN  = только warn-issues (0 error), failed==0
  FAIL  = есть error-issue ИЛИ failed>0 ИЛИ джоба не done

Артефакты: slepok_qa_results.json (сырьё) + slepok_qa_report.md (PASS/FAIL таблица).

ВНИМАНИЕ — аккаунт:
  По умолчанию всё гонится на ЕДИНЫЙ тест-аккаунт QA_LOGIN (как исторический батч).
  Прогон на РЕАЛЬНЫХ аккаунтах директологов — только явным маппингом slepok→login
  (env SLEPOK_QA_ACCOUNTS=/path/to/accounts.json = {"slepok_key": "porg-xxxx", ...}).
  Создаются ТОЛЬКО черновики (State=OFF); перед каждым слепком черновики аккаунта чистятся.
"""
import os, sys, json, time, argparse, subprocess

os.environ.setdefault("DIRECT_ROLE", "web")
from direct.main import create_app  # noqa: E402
from direct import blueprint as B   # noqa: E402

# --- тест-аккаунт по умолчанию (совпадает с историческим ручным прогоном) ---
QA_LOGIN = os.environ.get("SLEPOK_QA_LOGIN", "porg-vfdnaolu")
COUNTER = int(os.environ.get("SLEPOK_QA_COUNTER", "110499992"))
# Цель НЕ хардкодим: она принадлежит конкретному счётчику. Прежний дефолт (579905467) жил
# от дефолтного счётчика, и прогон на другом аккаунте падал на ВСЕХ позициях с
# «goalId … Объект не найден» (v501 4000 / DefectIds.OBJECT_NOT_FOUND). None → резолвим
# «Все формы» по COUNTER в main() тем же путём, что и UI-создание (_goal_vse_formy).
_GOAL_ENV = os.environ.get("SLEPOK_QA_GOAL")
GOAL = int(_GOAL_ENV) if _GOAL_ENV else None

VARIANTS = ["tp1_rsy", "search_test", "search_dynamic", "search_gallery",
            "rsya_gallery", "master_auto", "product_auto"]
TP_SQ = {"6": ["site", "kviz"], "7": ["site", "kviz"]}

# Порядок (Семён): авто-слепки → terehov (aon) предпоследним → gen_ses/dmp ПОСЛЕДНИМИ.
ORDER = ["pavlov", "kryuchkova", "karavaev", "gordeeva", "zubakin", "chepelev",
         "tumashenko", "kuderko", "salamahin", "scherbakova", "terehov",
         "gen_ses", "dmp",
         # Новые слепки (2026-07-16). Без них --only avto_sk молча давал пустой
         # прогон: order фильтруется по ORDER, а не по slepki_structure.json.
         "avto_sk", "avtolajt_bu", "sk_krs",
         # piterkina добавлена 2026-07-21 (была в slepki/_order.json, но отсутствовала
         # здесь → --only piterkina молча давал пустой прогон).
         "piterkina"]

# --fast: один лёгкий site_type на слепок (Монобренд = 1 бренд = мало групп = быстро).
FAST_SITETYPE = {s: "Монобренд" for s in ORDER}
FAST_SITETYPE["kuderko"] = "С пробегом"
FAST_SITETYPE["gen_ses"] = "С пробегом"
FAST_SITETYPE["dmp"] = "dmp"
# Новые слепки существуют ТОЛЬКО в «С пробегом» — дефолтный «Монобренд» дал бы пустой site_types.
FAST_SITETYPE["avto_sk"] = "С пробегом"
FAST_SITETYPE["avtolajt_bu"] = "С пробегом"
FAST_SITETYPE["sk_krs"] = "С пробегом"

# Пути артефактов — env-override. Нужен для ПАРАЛЛЕЛЬНЫХ прогонов по разным логинам:
# пути были захардкожены, и одновременные прогоны затирали результаты друг друга
# (_save открывает RESULTS_PATH на "w"). Дефолты прежние — обычный запуск не меняется.
RESULTS_PATH = os.environ.get("SLEPOK_QA_RESULTS") or os.path.join(
    os.path.dirname(__file__), "slepok_qa_results.json")
REPORT_PATH = os.environ.get("SLEPOK_QA_REPORT") or os.path.join(
    os.path.dirname(__file__), "slepok_qa_report.md")

# После done-джобы ждём отложенный автофикс (_delayed_repair_daemon_loop, ~180с)
# и только потом снимаем ФИНАЛЬНЫЙ пост-ремонтный вердикт.
# Без этой паузы delete_drafts следующего слепка отменяет repair (superseded),
# и все FAIL фиксируются ДО починки — ложные.
_REPAIR_WAIT = 210  # секунды (180с автофикс + 30с буфер)


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _load_slepki():
    # Структура разбита на per-slepok файлы (direct/slepki/) — собираем через slepki_store.
    from direct import slepki_store as _sstore
    struct = _sstore.assemble()
    return {e.get("key"): [s.get("name") for s in (e.get("site_types") or [])]
            for e in struct.get("directologists", [])}


def _load_account_map():
    """slepok_key -> login. По умолчанию все на QA_LOGIN. Override через env-файл."""
    path = os.environ.get("SLEPOK_QA_ACCOUNTS")
    override = {}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            override = json.load(fh)
    return lambda slepok: override.get(slepok, QA_LOGIN)


def _terehov_aon(plan):
    """terehov = автотаргет (aon), НЕ КС. tp1/tp4/tp5→autotarget; tp2→только autotarget-строки."""
    out = []
    for it in plan:
        t = it.get("type")
        if t == "search_test":
            if not it.get("autotarget"):
                continue
            out.append(it); continue
        if t in ("tp1_rsy", "search_dynamic", "search_gallery"):
            it["autotarget"] = True
            nm = it.get("name") or ""
            it["name"] = nm.replace(" - КС ", " - Автотаргетинг ").replace(" - КС-", " - Автотаргетинг-")
            for k in ("tp1_label", "tp4_label"):
                if it.get(k):
                    it[k] = it[k].replace(" - КС", " - Автотаргетинг")
        out.append(it)
    return out


def _save(results):
    with open(RESULTS_PATH, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)


def _read_job(job_id):
    conn = B._victory_conn()
    try:
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT job_id,status,total,done,created,failed,result,errors_log "
                    "FROM public.direct_automation_jobs WHERE job_id=%s", (job_id,))
        return cur.fetchone()
    finally:
        conn.close()


def _wait_job(job_id, jtimeout):
    terminal = ("done", "error", "cancelled", "interrupted")
    t0 = time.time()
    last_status = last_result = None
    while time.time() - t0 < jtimeout:
        row = _read_job(job_id)
        if row is None:  # purged (TTL) после терминала → last captured
            if last_result is not None:
                return last_status or "purged", last_result
            time.sleep(3); continue
        st = row["status"]
        if st != last_status:
            _log(f"    job {job_id[:8]} status={st} done={row['done']}/{row['total']} "
                 f"created={row['created']} failed={row['failed']}")
            last_status = st
        if row.get("result"):
            last_result = row["result"]
        if st in terminal:
            if not row.get("result"):
                time.sleep(5)
                row2 = _read_job(job_id)
                if row2 and row2.get("result"):
                    last_result = row2["result"]
            return st, row.get("result") or last_result
        time.sleep(6)
    return "TIMEOUT", last_result


def _verdict(entry):
    """PASS/WARN/FAIL из собранной записи site_type."""
    if entry.get("job_status") != "done":
        return "FAIL"
    if (entry.get("failed") or 0) > 0:
        return "FAIL"
    lv = entry.get("lv_status")
    if lv == "pass":
        return "PASS"
    if lv == "warn":
        return "WARN"
    if lv == "fail":
        return "FAIL"
    return "FAIL"  # нет вердикта верификатора = не подтверждено


def _worker_alive():
    try:
        out = subprocess.run(["systemctl", "is-active", "direct-create-worker.service"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() == "active"
    except Exception:
        return None  # не смогли проверить


def _write_report(results):
    rows, n_pass = [], 0
    total = 0
    fail_codes = {}
    for slepok, sd in results.items():
        for st, e in (sd.get("site_types") or {}).items():
            if "job_status" not in e:  # dry/plan-only/ошибка set_plan
                continue
            total += 1
            v = _verdict(e)
            if v == "PASS":
                n_pass += 1
            for c, meta in (e.get("issue_codes") or {}).items():
                if meta.get("severity") == "error":
                    fail_codes[c] = fail_codes.get(c, 0) + 1
            rows.append((v, slepok, st, e.get("created"), e.get("failed"),
                         e.get("lv_errors"), e.get("lv_warnings"),
                         ",".join(c for c, m in (e.get("issue_codes") or {}).items()
                                  if m.get("severity") == "error")))
    order = {"FAIL": 0, "WARN": 1, "PASS": 2}
    rows.sort(key=lambda r: (order.get(r[0], 0), r[1]))
    lines = [f"# slepok-qa отчёт ({time.strftime('%Y-%m-%d %H:%M')})", "",
             f"**Аккаунт(ы):** {QA_LOGIN} (или маппинг) · **Прогонов:** {total} · "
             f"**PASS:** {n_pass}/{total}", "",
             "| Вердикт | Слепок | site_type | created | failed | lv_err | lv_warn | error-коды |",
             "|---|---|---|---|---|---|---|---|"]
    for v, sl, st, cr, fa, le, lw, codes in rows:
        badge = {"PASS": "✅ PASS", "WARN": "🟡 WARN", "FAIL": "❌ FAIL"}.get(v, v)
        lines.append(f"| {badge} | {sl} | {st} | {cr} | {fa} | {le} | {lw} | {codes} |")
    if fail_codes:
        lines += ["", "## Топ error-кодов", ""]
        for c, n in sorted(fail_codes.items(), key=lambda x: -x[1]):
            lines.append(f"- `{c}` × {n}")
    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return total, n_pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="только set_plan (превью), без создания")
    ap.add_argument("--only", default="", help="список слепков через запятую")
    ap.add_argument("--fast", action="store_true", help="один лёгкий site_type на слепок")
    # Прогон строго по ОДНОМУ типу сайта за заход: создать → проверить ошибки → починить → повторить.
    # --fast для этого не годится (жёстко прибит к FAST_SITETYPE и не даёт выбрать нужный).
    ap.add_argument("--site-type", default="", dest="site_type",
                    help="конкретный site_type (напр. 'Мультибренд'); приоритетнее --fast")
    ap.add_argument("--jtimeout", type=int, default=5400)
    args = ap.parse_args()

    if not args.dry:
        wa = _worker_alive()
        if wa is False:
            _log("❌ ПРЕДУПРЕЖДЕНИЕ: direct-create-worker.service НЕ active — джобы застрянут в queued. "
                 "Запусти воркер и повтори (или --dry для превью).")
        elif wa is None:
            _log("⚠ не смог проверить статус direct-create-worker (нет systemctl?) — продолжаю.")

    slepki = _load_slepki()
    login_for = _load_account_map()
    order = ORDER
    if args.only:
        want = {s.strip() for s in args.only.split(",") if s.strip()}
        order = [s for s in ORDER if s in want]

    app = create_app()

    global GOAL
    if GOAL is None:
        from .blueprint_metrika import _goal_vse_formy
        with app.app_context():
            gid, gname = _goal_vse_formy(COUNTER)
        if not gid:
            _log(f"❌ у счётчика {COUNTER} не нашлась цель «Все формы» — прогон бессмысленен "
                 f"(все кампании упадут на goalId). Задай SLEPOK_QA_GOAL явно.")
            return
        GOAL = int(gid)
        _log(f"goal резолвнут по счётчику {COUNTER}: {GOAL} ({gname})")

    results = {}
    if os.path.exists(RESULTS_PATH):
        try:
            results = json.load(open(RESULTS_PATH, encoding="utf-8"))
        except Exception:
            results = {}

    with app.test_client() as cli:
        with cli.session_transaction() as sess:
            sess["logged_in"] = True
            sess["is_admin"] = True
            sess["username"] = "slepok-qa"

        for slepok in order:
            login = login_for(slepok)
            site_types = slepki.get(slepok) or []
            if args.site_type:
                if args.site_type not in site_types:
                    _log(f"  ⚠ site_type={args.site_type!r} нет у слепка {slepok} "
                         f"(есть: {site_types}) — пропускаю слепок")
                    continue
                site_types = [args.site_type]
            elif args.fast:
                fst = FAST_SITETYPE.get(slepok)
                site_types = [fst] if fst in site_types else site_types[:1]
            _log(f"=== SLEPOK {slepok} | login={login} | site_types={site_types} ===")
            results.setdefault(slepok, {"login": login, "site_types": {}})

            if not args.dry:
                with app.app_context():
                    dr = B._delete_drafts_core(login, "")
                _log(f"  delete_drafts: deleted={dr.get('deleted')} errors={dr.get('errors')}")
                results[slepok]["deleted"] = {k: dr.get(k) for k in
                                              ("deleted", "by_v5", "by_uac", "by_cookie", "errors")}
                _save(results)
                time.sleep(5)

            for st in site_types:
                _log(f"  -- site_type={st}")
                # single_feed_fallback=True — автопрогон без человека у поп-апа: если профильных
                # фидов (/yandex.xml, /yandex-used-auto.xml) на аккаунте нет, план сразу строится
                # на фолбэк-фиде FALLBACK_SINGLE_FEED_KEY. Без этого ключа /set_plan отдаёт
                # feeds=[] → в плане НЕТ product-item'ов и tp7 тихо не создаётся (см.
                # ERRORS_JOURNAL: FEED_FALLBACK_PLAN_VS_BUILDER_DESYNC). Ключ читается
                # create_set_plan.py:468 именно на этапе ПЛАНА — слать его в /create_set поздно.
                # "n": True — «без CPA»: план эмитит только CPC-вариант каждой позиции.
                # ⚠️ Флаг читается ПЛАНОМ под именем "n" (create_set_plan.py:357), а не "no_cpa":
                # тот шлётся ниже в /create_set и на состав плана НЕ влияет. Без "n" здесь план
                # всегда строил cpc+cpa (×2 кампаний) — требование Семёна «без спа кампаний»
                # молча не выполнялось.
                body_plan = {"login": login, "agent": slepok, "site_type": st,
                             "variants": VARIANTS, "tp_sq": TP_SQ, "single_feed": True,
                             "single_feed_fallback": True, "n": True,
                             "counter_id": COUNTER, "goal_id": GOAL}
                r = cli.post("/direct/api/set_plan", json=body_plan)
                if r.status_code != 200:
                    _log(f"     set_plan HTTP {r.status_code}: {r.get_data(as_text=True)[:200]}")
                    results[slepok]["site_types"][st] = {"error": f"set_plan {r.status_code}"}
                    _save(results); continue
                pj = r.get_json()
                plan = pj.get("plan") or []
                if slepok == "terehov":
                    plan = _terehov_aon(plan)
                types_ct = {}
                for it in plan:
                    types_ct[it.get("type")] = types_ct.get(it.get("type"), 0) + 1
                _log(f"     plan: {len(plan)} items {types_ct} warns={len(pj.get('warnings') or [])}")
                results[slepok]["site_types"][st] = {"plan_count": len(plan), "plan_types": types_ct,
                                                     "warnings": pj.get("warnings")}
                _save(results)
                if args.dry or not plan:
                    continue

                body_create = {"login": login, "items": plan, "agent": slepok, "site_type": st,
                               "counter_id": COUNTER, "goal_id": GOAL, "no_cpa": True,
                               "single_feed": True, "via_cookie": False, "feed_confirmed": True,
                               "llm_provider": "openrouter"}
                cr = cli.post("/direct/api/create_set_async", json=body_create)
                cj = cr.get_json() or {}
                job_id = cj.get("job_id")
                if not job_id:
                    _log(f"     create HTTP {cr.status_code}: {json.dumps(cj, ensure_ascii=False)[:200]}")
                    results[slepok]["site_types"][st]["create_error"] = cj
                    _save(results); continue
                if cj.get("existing"):
                    _log(f"     ⚠ existing job reused (busy?) job_id={job_id}")
                _log(f"     job_id={job_id} total={cj.get('total')}")
                st_status, res = _wait_job(job_id, args.jtimeout)

                # ПОСТ-РЕМОНТНАЯ ПАУЗА + ПОВТОРНАЯ ВЕРИФИКАЦИЯ
                # Отложенный автофикс срабатывает ~180с после done.
                # Ждём _REPAIR_WAIT, потом снимаем свежий live_verification.
                if st_status == "done" and isinstance(res, dict):
                    _log(f"     Пауза {_REPAIR_WAIT}с (ожидание отложенного автофикса)...")
                    time.sleep(_REPAIR_WAIT)
                    _log(f"     Повторная live_verification (пост-ремонтная)...")
                    try:
                        with app.app_context():
                            re_results = res.get("results") or []
                            new_lv = B.create_set_live_verification(login, re_results)
                        new_lvs = (new_lv.get("summary") or {}) if isinstance(new_lv, dict) else {}
                        _log(f"     Пост-ремонтная: status={new_lv.get('status') if isinstance(new_lv, dict) else '?'} "
                             f"errors={new_lvs.get('errors', 0)} warnings={new_lvs.get('warnings', 0)}")
                        res["live_verification"] = new_lv
                    except Exception as _re_err:
                        _log(f"     ⚠ Пост-ремонтная верификация не удалась: {_re_err}")

                summary, lv = {}, {}
                if isinstance(res, dict):
                    summary = {"created": res.get("created"), "failed": res.get("failed")}
                    lv = res.get("live_verification") or {}
                lvs = (lv.get("summary") or {}) if isinstance(lv, dict) else {}
                codes = {}
                for iss in (lv.get("issues") or []) if isinstance(lv, dict) else []:
                    c = iss.get("code")
                    codes.setdefault(c, {"severity": iss.get("severity"), "n": 0})
                    codes[c]["n"] += 1
                results[slepok]["site_types"][st].update({
                    "job_id": job_id, "job_status": st_status,
                    "created": summary.get("created"), "failed": summary.get("failed"),
                    "lv_errors": lvs.get("errors"), "lv_warnings": lvs.get("warnings"),
                    "lv_status": lv.get("status") if isinstance(lv, dict) else None,
                    "issue_codes": codes,
                })
                entry = results[slepok]["site_types"][st]
                _log(f"     {_verdict(entry)} status={st_status} created={summary.get('created')} "
                     f"failed={summary.get('failed')} lv_errors={lvs.get('errors')} codes={list(codes.keys())}")
                _save(results)

    _save(results)
    if not args.dry:
        total, n_pass = _write_report(results)
        _log(f"BATCH COMPLETE — PASS {n_pass}/{total}. Отчёт: {REPORT_PATH}")
    else:
        _log("DRY COMPLETE (только план, отчёт не строился)")


if __name__ == "__main__":
    main()

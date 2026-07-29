"""Create-set queue and job status routes for Direct automation."""

from __future__ import annotations

import re
import time as _time
from typing import Callable

from flask import current_app, jsonify, request, session


def register_job_routes(
    bp,
    access,
    danger_access,
    *,
    parse_number: Callable,
    metrika_goals_for: Callable,
    counter_foreign_owner: Callable,
    resolve_agency_hint: Callable,
    ensure_create_worker: Callable,
    job_new: Callable,
    create_jobs_ahead: Callable,
    create_watchdog_tick: Callable,
    jobs_purge_old: Callable,
    job_agency: Callable,
    job_db_save: Callable,
    job_db_delete: Callable,
    delete_drafts_core: Callable,
    create_jobs: dict,
    create_jobs_lock,
    create_cond,
    create_queue: list,
    job_terminal: tuple,
    job_db_last: dict,
    start_prefetch: Callable | None = None,
    role_is_web: Callable[[], bool] = lambda: False,
    job_db_get: Callable | None = None,
    job_db_ahead: Callable | None = None,
    job_db_set_status: Callable | None = None,
    job_control_set: Callable | None = None,
    job_db_web_await_feed: Callable | None = None,
    job_db_web_resolve_feed: Callable | None = None,
    job_db_active_by_login: Callable | None = None,
    job_db_list_recent: Callable | None = None,
    cancel_children: Callable | None = None,
) -> None:
    def _elapsed_for_row(row: dict, status: str | None):
        """Live elapsed for UI cards.

        created_at = когда job поставили в очередь; started_at = когда worker реально начал аккаунт.
        Для running показываем именно время выполнения текущего аккаунта, а не ожидание всей очереди.
        """
        if status in job_terminal:
            return (row.get("result") or {}).get("elapsed_seconds") if isinstance(row.get("result"), dict) else None
        if status == "running":
            anchor = row.get("started_at") or row.get("created_at")
        elif status == "awaiting_feed_decision":
            anchor = row.get("created_at")
        else:
            return None
        try:
            return max(0, int(_time.time() - anchor.timestamp())) if anchor else None
        except Exception:  # noqa: BLE001
            return None

    def _launch_prefetch(job_id: str, login: str, body: dict) -> None:
        """Фаза 1: греем queued-джобу в фоне. is_cancelled смотрит статус в
        _CREATE_JOBS (НЕ через Flask-объекты) — прогрев прекращается, как только
        джоба ушла из queued (running/cancelled/interrupted/terminal)."""
        if start_prefetch is None:
            return

        def _is_cancelled(_jid=job_id):
            with create_jobs_lock:
                jj = create_jobs.get(_jid)
                return (jj is None) or jj.get("status") != "queued" or bool(jj.get("cancel"))
        try:
            start_prefetch(login, body, is_cancelled=_is_cancelled)
        except Exception:  # noqa: BLE001 — прогрев не смеет ломать постановку джобы
            pass

    @bp.route("/api/create_set_async", methods=["POST"])
    @access
    def api_create_set_async():
        """Старт create_set в ФОНЕ — большой набор не упирается в nginx proxy_read_timeout."""
        body = dict(request.json or {})
        items = body.get("items") or []
        login = (body.get("login") or "").strip()
        agent = (body.get("agent") or "").strip()
        site_type = (body.get("site_type") or "").strip()
        if not items:
            return jsonify({"error": "items обязательны"}), 400
        mismatches: list[str] = []
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            plan_agent = str(item.get("_plan_agent") or item.get("plan_agent") or "").strip()
            plan_site_type = str(item.get("_plan_site_type") or item.get("plan_site_type") or "").strip()
            if plan_agent and plan_agent != agent:
                mismatches.append(f"#{idx + 1}: plan_agent={plan_agent!r}, request_agent={agent!r}")
            if plan_site_type and plan_site_type != site_type:
                mismatches.append(
                    f"#{idx + 1}: plan_site_type={plan_site_type!r}, request_site_type={site_type!r}")
            name = str(item.get("name") or "")
            if item.get("renamed") or re.search(r"_v\d{2}(?:\b|$)", name):
                mismatches.append(f"#{idx + 1}: versioned campaign name is forbidden: {name!r}")
            if len(mismatches) >= 5:
                break
        if mismatches:
            return jsonify({
                "error": "План устарел или не соответствует выбранному слепку/типу сайта. "
                         "Нажмите «Обновить/посчитать план» и запускайте создание только после пересчёта. "
                         + "; ".join(mismatches),
                "plan_mismatch": True,
            }), 409
        if any(isinstance(item, dict) and item.get("pre_draft_only") for item in items):
            return jsonify({
                "error": "Этот набор подготовлен только до этапа создания черновиков: "
                         "source-aware builder ещё не включён, запуск намеренно заблокирован."
            }), 409
        # Гейт ловит УСТАРЕВШИЙ клиент, а не одиночный фид как таковой. Текущий интерфейс в полном
        # прогоне принудительно шлёт single_feed=false (`automation_create.js:createDrafts`), поэтому
        # потоковый запрос с одиночным фидом из живого UI прийти НЕ может — только из протухшей
        # вкладки либо от осознанного вызова (скрипт/тестовый прогон).
        # `single_feed_confirmed` различает эти два случая: в старом коде такого поля нет, протухшая
        # вкладка его не пришлёт. Разрешено Семёном 2026-07-28 — «тестово создаём по одному фиду»:
        # до этого гейт отбивал и законный одиночный прогон вместе со стухшим планом.
        if (body.get("stream_content") and body.get("single_feed")
                and not body.get("single_feed_confirmed")):
            return jsonify({
                "error": "Потоковый полный ИИ-прогон не должен запускаться с single_feed=true. "
                         "Обновите страницу и запустите полный прогон заново: новый интерфейс "
                         "пересчитает полный план без сужения до профильного фида.",
                "stale_client_single_feed": True,
            }), 409
        if body.get("stream_content"):
            item_types = {
                str((item or {}).get("type") or (item or {}).get("variant") or "").strip()
                for item in items
                if isinstance(item, dict)
            }
            if item_types and item_types <= {"product"}:
                return jsonify({
                    "error": "Потоковый полный ИИ-прогон получил только товарные tp7/product "
                             "кампании. Это похоже на устаревший или частичный фронтовый план. "
                             "Обновите страницу и запустите полный прогон заново.",
                    "stale_client_product_only_stream": True,
                }), 409
        # Провайдер генерации из попапа (m3 | openrouter) — в КАЖДЫЙ item: генерация читает
        # item.llm_provider, а докрутки/деферреды наследуют body как есть (Семён 03.07).
        _llmp = str(body.get("llm_provider") or "").strip().lower()
        if _llmp in ("m3", "openrouter"):
            for _it in items:
                if isinstance(_it, dict):
                    _it.setdefault("llm_provider", _llmp)

        _cid_pf = parse_number(body.get("counter_id"), 0)
        if not _cid_pf:
            _mg_pf = metrika_goals_for(login)
            if _mg_pf and _mg_pf["counters"]:
                _cid_pf = _mg_pf["counters"][0]
        _owner_pf = counter_foreign_owner(_cid_pf, login) if _cid_pf else None
        if _owner_pf:
            return jsonify({"error": f"счётчик Метрики {_cid_pf} принадлежит аккаунту «{_owner_pf}», "
                                     f"а не «{login}» — Яндекс отклонит цель как «не найдена». "
                                     f"Укажите счётчик и цель самого «{login}»."}), 400

        # Гейт по metrika_alert из шага плана (create_set_plan._metrika_alert_for) — симметричен
        # feed_alert ниже, но БЕЗ подтверждения-обхода: недостающий счётчик/цель нельзя
        # «подтвердить и продолжить», только заполнить. Отбиваем ДО job_new, иначе в очереди
        # осталась бы осиротевшая задача. Клиенты без metrika_alert в body (внешний API) идут как
        # раньше — их страхует prepare_metrika в create_set_orchestrator.
        _metrika_alert = body.get("metrika_alert") or {}
        if isinstance(_metrika_alert, dict) and _metrika_alert.get("needed"):
            return jsonify({
                "error": _metrika_alert.get("error") or "укажите счётчик Метрики и цель (goal_id)",
                "metrika_alert": True,
            }), 400

        try:
            from ..create.create_set_content_preflight import create_set_pack_gap_note
            _gap_note = create_set_pack_gap_note(body)
        except Exception:  # noqa: BLE001
            _gap_note = ""
        if _gap_note:
            return jsonify({"error": _gap_note, "content_gap_preflight": True}), 409

        resolved_ag = resolve_agency_hint(login, (body.get("agency") or "").strip())
        if resolved_ag:
            body["agency"] = resolved_ag
        app = current_app._get_current_object()
        ensure_create_worker(app)

        # web-роль: постановка ТОЛЬКО в БД (status='queued', _web_posted=true). In-memory очередь
        # web-процесса пуста; исполняет worker-процесс. Прогрев (Фаза 1) — в воркере, здесь НЕ зовём.
        if role_is_web():
            pre_existing = job_db_active_by_login(login) if job_db_active_by_login else None
            saved_session = dict(session)
            job_id = job_new(len(items), login, body, saved_session, dedup_login=True)
            deduped = bool(pre_existing and pre_existing == job_id)
            if deduped:
                # Дедуп по логину ловит и ИСТИННЫЙ дубль (повторный клик на тот же слепок),
                # и ЧУЖУЮ активную джобу логина — например реактивированную фоновым ремонтом
                # уже показанного пользователю как "готово" слепка (_auto_queue_recreate_after_done).
                # Раньше оба случая молча отдавали existing=True с ЧУЖИМ job_id — новый слепок
                # тихо терялся, вызывающий код (UI/скрипт) не мог отличить "это мой job_id" от
                # "это чужая задача" (инцидент 2026-07-11: kuderko подменился job_id chepelev).
                # Сверяем agent+site_type активной джобы с текущим запросом — если они СОВПАДАЮТ,
                # это истинный дубль (безопасно вернуть existing). Если РАЗНЫЕ — это НЕ дубль,
                # логин занят чужой задачей: явно отказываем (409), а не притворяемся успехом.
                _busy_row = job_db_get(job_id) if job_db_get else None
                _busy_body = (_busy_row or {}).get("body") or {}
                if not isinstance(_busy_body, dict):
                    _busy_body = {}
                _same_task = (str(_busy_body.get("agent") or "") == str(body.get("agent") or "") and
                              str(_busy_body.get("site_type") or "") == str(body.get("site_type") or ""))
                if not _same_task:
                    return jsonify({
                        "error": (f"Аккаунт «{login}» занят другой активной задачей "
                                  f"(слепок «{_busy_body.get('agent') or '?'}», "
                                  f"{_busy_body.get('site_type') or '?'}, job_id={job_id}) — "
                                  "вероятно, идёт фоновый ремонт предыдущего набора. "
                                  "Повторите запуск через 1-2 минуты."),
                        "busy": True, "busy_job_id": job_id,
                        "busy_agent": _busy_body.get("agent"),
                        "busy_site_type": _busy_body.get("site_type"),
                    }), 409
                _resp_agency = (_busy_row or {}).get("agency") or _busy_body.get("agency") or body.get("agency") or ""
                resp = {"job_id": job_id, "total": len(items), "login": login,
                        "agency": _resp_agency,
                        "ahead": job_db_ahead(job_id) if job_db_ahead else 0,
                        "existing": True,
                        "note": "для аккаунта уже есть активная задача; дубль не создан"}
                return jsonify(resp)
            resp = {"job_id": job_id, "total": len(items), "login": login,
                    "agency": body.get("agency") or "",
                    "ahead": job_db_ahead(job_id) if job_db_ahead else 0}
            _fa = body.get("feed_alert") or {}
            if _fa.get("needed") and not body.get("feed_confirmed") and job_db_web_await_feed:
                _dl = _time.time() + 300
                job_db_web_await_feed(job_id, _dl)
                resp["awaiting_feed_decision"] = True
                resp["feed_deadline"] = _dl
                resp["seconds_left"] = 300
            return jsonify(resp)

        with create_jobs_lock:
            for _jid, _j in create_jobs.items():
                if _j.get("status") not in job_terminal and (_j.get("login") or "").strip() == login:
                    _busy_body = _j.get("body") or {}
                    if not isinstance(_busy_body, dict):
                        _busy_body = {}
                    _same_task = (str(_busy_body.get("agent") or "") == str(body.get("agent") or "") and
                                  str(_busy_body.get("site_type") or "") == str(body.get("site_type") or ""))
                    if not _same_task:
                        return jsonify({
                            "error": (f"Аккаунт «{login}» занят другой активной задачей "
                                      f"(слепок «{_busy_body.get('agent') or '?'}», "
                                      f"{_busy_body.get('site_type') or '?'}, job_id={_jid}) — "
                                      "вероятно, идёт фоновый ремонт предыдущего набора. "
                                      "Повторите запуск через 1-2 минуты."),
                            "busy": True, "busy_job_id": _jid,
                            "busy_agent": _busy_body.get("agent"),
                            "busy_site_type": _busy_body.get("site_type"),
                        }), 409
                    _resp_agency = _j.get("agency") or _busy_body.get("agency") or body.get("agency") or ""
                    return jsonify({
                        "job_id": _jid, "total": int(_j.get("total") or len(items)),
                        "login": login, "agency": _resp_agency,
                        "ahead": create_jobs_ahead(_jid),
                        "existing": True,
                        "note": "для аккаунта уже есть активная задача; дубль не создан",
                    })
        saved_session = dict(session)
        with create_jobs_lock:
            existing_ids = set(create_jobs.keys())
        job_id = job_new(len(items), login, body, saved_session, dedup_login=True)
        body["_job_id"] = job_id
        deduped = job_id in existing_ids
        # Feed alert: если нет фида и пользователь не подтвердил — ставим ожидание (5 мин)
        _feed_deadline = None
        _feed_alert = body.get("feed_alert") or {}
        if _feed_alert.get("needed") and not body.get("feed_confirmed") and not deduped:
            with create_jobs_lock:
                _j = create_jobs.get(job_id)
                if _j is not None and _j.get("status") == "queued":
                    _j["status"] = "awaiting_feed_decision"
                    _j["feed_deadline"] = _time.time() + 300
                    _feed_deadline = _j["feed_deadline"]
                    if job_id in create_queue:
                        create_queue.remove(job_id)
        # Фаза 1: новая queued-джоба (не дубль, не ждёт решения по фиду) → фоновый прогрев.
        if not deduped and _feed_deadline is None:
            _launch_prefetch(job_id, login, body)
        with create_jobs_lock:
            ahead = create_jobs_ahead(job_id)
        resp = {"job_id": job_id, "total": len(items), "login": login,
                "agency": body.get("agency") or "", "ahead": ahead}
        if deduped:
            resp["existing"] = True
            resp["note"] = "для аккаунта уже есть активная задача; дубль не создан"
        if _feed_deadline is not None:
            resp["awaiting_feed_decision"] = True
            resp["feed_deadline"] = _feed_deadline
            resp["seconds_left"] = 300
        return jsonify(resp)

    @bp.route("/api/create_set_status", methods=["GET"])
    @access
    def api_create_set_status():
        """Прогресс/результат async-джобы create_set."""
        jid = (request.args.get("job_id") or "").strip()
        # web-роль: читаем прогресс из БД (in-memory очереди у web-процесса нет; воркер флешит
        # прогресс в БД троттлингом ≤4 c через _job_db_progress).
        if role_is_web():
            row = (job_db_get(jid) if job_db_get else None)
            if not row:
                return jsonify({"error": "job не найдена (возможно, устарела)"}), 404
            body_row = row.get("body") or {}
            _status = row.get("status")
            _total = int(row.get("total") or 0)
            _fd = None
            if _status == "awaiting_feed_decision":
                try:
                    _fd = float(body_row.get("_feed_deadline")) if body_row.get("_feed_deadline") else None
                except (TypeError, ValueError):
                    _fd = None
            _res = row.get("result") if _status in job_terminal else None
            _el = _elapsed_for_row(row, _status)
            _skipped_ex = (row.get("result") or {}).get("skipped_existing", 0) if isinstance(row.get("result"), dict) else 0
            _active_ch = (row.get("result") or {}).get("_active_children") if isinstance(row.get("result"), dict) else None
            return jsonify({"status": _status, "login": row.get("login") or "",
                            "agency": row.get("agency") or "",
                            "done": int(row.get("done") or 0), "total": _total,
                            "created": int(row.get("created") or 0), "failed": int(row.get("failed") or 0),
                            "skipped_existing": int(_skipped_ex or 0),
                            "set_done": int(row.get("done") or 0), "set_total": _total,
                            "ahead": job_db_ahead(jid) if (job_db_ahead and _status == "queued") else 0,
                            "error": row.get("error"), "elapsed": _el,
                            "step": None, "stream_content": bool(body_row.get("stream_content")),
                            "repairing": bool(_active_ch),
                            "result": _res, "feed_deadline": _fd,
                            "seconds_left": max(0, int(_fd - _time.time())) if _fd else None})
        ensure_create_worker(current_app._get_current_object())
        create_watchdog_tick()
        with create_jobs_lock:
            j = create_jobs.get(jid)
            if not j:
                return jsonify({"error": "job не найдена (возможно, устарела)"}), 404
            ahead = create_jobs_ahead(jid) if j["status"] == "queued" else 0
            _fd = j.get("feed_deadline")
            _j_result = j.get("result") or {}
            return jsonify({"status": j["status"], "login": j.get("login", ""),
                            "agency": job_agency(j), "done": j["done"], "total": j["total"],
                            "created": j["created"], "failed": j["failed"],
                            "skipped_existing": int((_j_result.get("skipped_existing") if isinstance(_j_result, dict) else 0) or 0),
                            "set_done": j.get("set_done", 0), "set_total": j.get("set_total", j["total"]),
                            "ahead": ahead, "error": j["error"], "elapsed": j.get("elapsed"),
                            "step": j.get("step"),
                            "stream_content": bool(j.get("stream_content")),
                            "repairing": bool(_j_result.get("_active_children") if isinstance(_j_result, dict) else False),
                            "result": j["result"] if j["status"] in job_terminal else None,
                            "feed_deadline": _fd,
                            "seconds_left": max(0, int(_fd - _time.time())) if _fd else None})

    @bp.route("/api/create_set_feed_decision", methods=["POST"])
    @access
    def api_create_set_feed_decision():
        """Ответ пользователя на предупреждение об отсутствующем фиде.

        body: {"job_id": "...", "decision": "run_all" | "run_without_feed" | "cancel"}
          run_all         — запустить как есть (фид может появиться позже)
          run_without_feed — пропустить фид-зависимые типы (_skip_feed_types)
          cancel          — отменить джобу
        """
        data = request.json or {}
        jid = (data.get("job_id") or "").strip()
        decision = (data.get("decision") or "").strip()
        if not jid:
            return jsonify({"error": "job_id обязателен"}), 400
        if decision not in ("run_all", "run_without_feed", "cancel"):
            return jsonify({"error": "decision: run_all | run_without_feed | cancel"}), 400
        # web-роль: решение по фиду применяем прямо в БД (status flip → queued; worker заклеймит).
        if role_is_web():
            row = (job_db_get(jid) if job_db_get else None)
            if not row:
                return jsonify({"error": "job не найдена"}), 404
            if row.get("status") != "awaiting_feed_decision":
                return jsonify({"error": f"job не в статусе ожидания (статус: {row.get('status')})"}), 400
            if decision == "cancel":
                job_db_set_status(jid, "cancelled", "отменено пользователем (нет фида)")
                new_status = "cancelled"
            else:
                job_db_web_resolve_feed(jid, decision)
                new_status = "queued"
            return jsonify({"ok": True, "job_id": jid, "status": new_status})
        with create_cond:
            j = create_jobs.get(jid)
            if not j:
                return jsonify({"error": "job не найдена"}), 404
            if j.get("status") != "awaiting_feed_decision":
                return jsonify({"error": f"job не в статусе ожидания (статус: {j.get('status')})"}), 400
            if decision == "cancel":
                j["status"] = "cancelled"
                j["error"] = "отменено пользователем (нет фида)"
                j["finished_at"] = _time.time()
            else:
                _body = j.get("body") or {}
                if decision == "run_without_feed":
                    _body["_skip_feed_types"] = ["product"]   # master (tp6) фид не требует — не скипать (Семён 2026-07-11)
                else:  # run_all
                    _body.pop("_skip_feed_types", None)
                j["status"] = "queued"
                create_queue.append(jid)
                create_cond.notify_all()
            snap = dict(j)
        job_db_save(jid, snap, full=True)
        # Джоба стала queued после решения по фиду → прогрев (Фаза 1).
        if snap.get("status") == "queued":
            _launch_prefetch(jid, snap.get("login") or "", snap.get("body") or {})
        return jsonify({"ok": True, "job_id": jid, "status": snap["status"]})

    @bp.route("/api/create_jobs", methods=["GET"])
    @access
    def api_create_jobs():
        """Живая очередь создания РК — серверный источник правды."""
        active_only = request.args.get("active") in ("1", "true")
        # web-роль: очередь читаем из БД (in-memory у web-процесса нет).
        if role_is_web():
            rows = (job_db_list_recent(active_only) if job_db_list_recent else [])
            out = []
            for r in rows:
                st = r.get("status")
                if active_only and st in job_terminal:
                    continue
                _body = r.get("body") or {}
                # Любая дочерняя джоба (докрутка 152/резерв, доставка недостающих, recreate-починка)
                # НЕ рисует своей карточки — её прогресс вливается в РОДИТЕЛЬСКУЮ (Семён 2026-07-07).
                if (_body.get("_resume_of") or _body.get("_requeue_of")
                        or _body.get("_repair_parent_job_id")):
                    continue
                _total = int(r.get("total") or 0)
                _el_list = _elapsed_for_row(r, st)
                out.append({"job_id": r.get("job_id"), "status": st, "login": r.get("login") or "",
                            "agency": r.get("agency") or "",
                            "done": int(r.get("done") or 0), "total": _total,
                            "created": int(r.get("created") or 0), "failed": int(r.get("failed") or 0),
                            "set_done": int(r.get("done") or 0), "set_total": _total,
                            "kind": r.get("kind") or "set", "publish": bool(r.get("publish")),
                            "ahead": job_db_ahead(r.get("job_id")) if (job_db_ahead and st == "queued") else 0,
                            "error": r.get("error"), "elapsed": _el_list,
                            "step": None, "stream_content": bool(_body.get("stream_content")),
                            "result": r.get("result") if st in job_terminal else None})
            order = {"running": 0, "queued": 1}
            out.sort(key=lambda x: order.get(x["status"], 2))
            return jsonify({"jobs": out})
        ensure_create_worker(current_app._get_current_object())
        create_watchdog_tick()
        jobs_purge_old()
        out = []
        with create_jobs_lock:
            for jid, j in create_jobs.items():
                if active_only and j["status"] in job_terminal:
                    continue
                _jb = j.get("body") or {}   # дочерняя (докрутка/доставка/recreate) → без карточки, вливается в родителя
                if (_jb.get("_resume_of") or _jb.get("_requeue_of")
                        or _jb.get("_repair_parent_job_id")):
                    continue
                ahead = create_jobs_ahead(jid) if j["status"] == "queued" else 0
                out.append({"job_id": jid, "status": j["status"], "login": j.get("login", ""),
                            "agency": job_agency(j),
                            "done": j.get("done", 0), "total": j.get("total", 0),
                            "created": j.get("created", 0), "failed": j.get("failed", 0),
                            "set_done": j.get("set_done", 0), "set_total": j.get("set_total", j.get("total", 0)),
                            "kind": j.get("kind", "set"), "publish": bool(j.get("publish")),
                            "ahead": ahead, "error": j.get("error"), "elapsed": j.get("elapsed"),
                            "step": j.get("step"),
                            "stream_content": bool(j.get("stream_content")),
                            "result": j.get("result") if j["status"] in job_terminal else None})
        order = {"running": 0, "queued": 1}
        out.sort(key=lambda x: order.get(x["status"], 2))
        return jsonify({"jobs": out})

    @bp.route("/api/create_set_cancel", methods=["POST"])
    @access
    def api_create_set_cancel():
        """Отмена джобы создания или удаление завершённой карточки из очереди."""
        jid = ((request.json or {}).get("job_id") or "").strip()
        # web-роль: команда идёт в БД. queued/claimed/awaiting → сразу cancelled; running → control='cancel'
        # (worker остановит после текущей кампании); terminal → удаление карточки.
        if role_is_web():
            row = (job_db_get(jid) if job_db_get else None)
            if not row:
                return jsonify({"error": "job не найдена"}), 404
            st = row.get("status")
            # Каскад: отмена родителя гасит и его активные дочерние джобы (докрутка/доставка/recreate).
            _kids = cancel_children(jid) if cancel_children else 0
            if st in job_terminal:
                job_db_last.pop(jid, None)
                job_db_delete(jid)
                return jsonify({"ok": True, "status": st, "removed": True,
                                "cancelled_children": _kids, "note": "убрана из очереди"})
            if st in ("queued", "claimed", "awaiting_feed_decision"):
                # A web-posted job can already be adopted into the worker's in-memory queue while the
                # DB still shows queued/claimed. Persist the terminal UI state, and also send a
                # worker control command so the in-memory copy is removed before it can run.
                if job_control_set:
                    job_control_set(jid, "cancel")
                job_db_set_status(jid, "cancelled", "отменено пользователем")
                return jsonify({"ok": True, "status": "cancelled", "cancelled_children": _kids})
            job_control_set(jid, "cancel")               # running → остановка после текущей кампании
            return jsonify({"ok": True, "status": st, "cancelled_children": _kids,
                            "note": "остановка после текущей кампании"})
        with create_jobs_lock:
            j = create_jobs.get(jid)
            if not j:
                return jsonify({"error": "job не найдена"}), 404
            if j["status"] in job_terminal:
                create_jobs.pop(jid, None)
                job_db_last.pop(jid, None)
                job_db_delete(jid)
                _kids = cancel_children(jid) if cancel_children else 0
                return jsonify({"ok": True, "status": j["status"], "removed": True,
                                "cancelled_children": _kids, "note": "убрана из очереди"})
            j["cancel"] = True
            if j["status"] == "queued" and jid in create_queue:
                create_queue.remove(jid)
                j["status"] = "cancelled"
            snap = dict(j)
        job_db_save(jid, snap, full=True)
        _kids = cancel_children(jid) if cancel_children else 0   # каскад: гасим активные дочерние
        return jsonify({"ok": True, "status": snap["status"], "cancelled_children": _kids})

    @bp.route("/api/jobs/<job_id>/resume", methods=["POST"])
    @access
    def api_job_resume(job_id: str):
        """Возобновление джобы, прерванной рестартом сервиса."""
        jid = job_id.strip()
        if role_is_web():
            row = (job_db_get(jid) if job_db_get else None)
            if not row:
                return jsonify({"error": "job не найдена (возможно, уже убрана)"}), 404
            if row.get("status") != "interrupted":
                return jsonify({"error": f"job не прервана (статус: {row.get('status')})"}), 400
            body = row.get("body")
            login = row.get("login") or ""
            done_idx = int(row.get("done") or 0)
        else:
            with create_jobs_lock:
                j = create_jobs.get(jid)
                if not j:
                    return jsonify({"error": "job не найдена (возможно, уже убрана)"}), 404
                if j.get("status") != "interrupted":
                    return jsonify({"error": f"job не прервана (статус: {j.get('status')})"}), 400
                body = j.get("body")
                login = j.get("login") or ""
                done_idx = int(j.get("done") or 0)
        if not body:
            return jsonify({"error": "body джобы не сохранён — невозможно возобновить автоматически. "
                                     "Запустите создание вручную через форму."}), 422
        if not body.get("items"):
            return jsonify({"error": "items в body пусты — нечего создавать"}), 422
        app = current_app._get_current_object()
        ensure_create_worker(app)
        saved_session = dict(session)
        new_body = dict(body)
        new_body.pop("_job_id", None)
        new_body.pop("_resume_count", None)
        items_list = body.get("items") or []
        if body.get("single_feed") and items_list:
            from ..create.create_set_input import first_feed_items
            items_list = first_feed_items(items_list, parse_number=parse_number)
        if 0 <= done_idx < len(items_list):
            partial_name = (items_list[done_idx].get("name") or "").strip()
            if partial_name:
                force = [str(n).strip() for n in (new_body.get("_repair_force_names") or [])
                         if str(n or "").strip()]
                if partial_name not in force:
                    force.append(partial_name)
                new_body["_repair_force_names"] = force
        new_job_id = job_new(len(new_body["items"]), login, new_body, saved_session)
        if role_is_web():
            ahead = job_db_ahead(new_job_id) if job_db_ahead else 0
        else:
            with create_jobs_lock:
                ahead = create_jobs_ahead(new_job_id)
        return jsonify({"ok": True, "new_job_id": new_job_id,
                        "total": len(new_body["items"]), "login": login, "ahead": ahead})

    @bp.route("/api/jobs/<job_id>/delete_created", methods=["POST"])
    @danger_access
    def api_job_delete_created(job_id: str):
        """Удалить черновики аккаунта из прерванной/завершённой джобы."""
        jid = job_id.strip()
        with create_jobs_lock:
            j = create_jobs.get(jid)
            if not j:
                return jsonify({"error": "job не найдена (возможно, уже убрана)"}), 404
            login = j.get("login") or ""
            agency = j.get("agency") or ""
            status = j.get("status") or ""
        if not login:
            return jsonify({"error": "login не сохранён в джобе"}), 422
        if status not in ("interrupted", "error", "done", "cancelled"):
            return jsonify({"error": f"Удаление доступно только для завершённых/прерванных джоб (статус: {status})"}), 400
        try:
            result = delete_drafts_core(login, agency)
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(e)[:300]}), 500
        return jsonify({"ok": result.get("ok", True),
                        "deleted": result.get("deleted", 0),
                        "by_v5": result.get("by_v5", 0),
                        "by_uac": result.get("by_uac", 0),
                        "by_cookie": result.get("by_cookie", 0),
                        "errors": result.get("errors") or []})

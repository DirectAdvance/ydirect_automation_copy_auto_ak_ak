"""Роуты «Сверки цен» редактора контента Директа (admin-only).

Вынесено из ``routes_content_editor.py`` (D1 декомпозиции монолита) без изменения
поведения: URL-пути, имена эндпоинтов (Flask берёт имя из имени функции), декоратор
доступа, форма JSON-ответов и условия доступа сохранены 1:1. Все хендлеры — вложенные
замыкания над зависимостями, которые передаются в :func:`register_price_check_routes`.
"""

from __future__ import annotations

from typing import Callable

from flask import jsonify, request, session

from . import price_check as pc


def register_price_check_routes(
    bp,
    access,
    *,
    victory_conn: Callable,
    victory_conn_rw: Callable,
    token_for_login: Callable,
    direct_tokens: Callable,
    v5_call: Callable,
    _allowed_directologists: Callable,
    _content_full_access: Callable,
    _login_allowed_for: Callable,
    _admin_allowed: Callable,
    _jobs_exec: Callable,
    CE_JOBS_TABLE: str,
    default_status: str,
    exclude_directologs: list[str],
) -> None:
    """Регистрирует ручки «Сверки цен» на blueprint ``bp``.

    Вызывается только когда ``victory_conn_rw is not None`` (RW-доступ к БД).
    """
    try:
        pc.ensure_price_check_tables(victory_conn_rw)
        # На старте сервиса: джобы, застрявшие в 'running' (рестарт в середине) → 'interrupted'.
        pc.reconcile_stuck_jobs(victory_conn_rw)
    except Exception as e:  # noqa: BLE001
        print(f"[price-check] ensure_price_check_tables failed: {e}", flush=True)

    _pc_deps = {
        "victory_conn": victory_conn,
        "victory_conn_rw": victory_conn_rw,
        "token_for_login": token_for_login,
        "direct_tokens": direct_tokens,
        "v5_call": v5_call,
    }

    # Модель доступа «Сверки цен» = 1:1 с «Обзором»:
    #   full access (admin ИЛИ content_admin) → allowed=None → видят ВСЁ;
    #   обычный юзер → allowed=[его директологи] (пусто → нет доступов).
    # Скоуп применяется на КАЖДОМ эндпоинте: листинги режем по allowed,
    # действия по job_id — только над своими заданиями (created_by).
    def _pc_scope_deny():
        """Для мутирующих ручек: 403, если у обычного юзера нет ни одного
        директолога. Возвращает (allowed, resp|None)."""
        allowed = _allowed_directologists()
        if allowed is not None and not allowed:
            return allowed, (jsonify({"error": "Доступы не выданы"}), 403)
        return allowed, None

    def _pc_job_owned(job_id: str) -> bool:
        """True, если текущий пользователь вправе видеть/управлять заданием.
        Full access → всегда True; обычный юзер → только своё (created_by)."""
        if _content_full_access():
            return True
        owner = pc.job_created_by(victory_conn, job_id)
        if owner is None:
            return False
        return owner == (session.get("username") or "").strip()

    @bp.route("/api/content-editor/admin/pricecheck/run", methods=["POST"])
    @access
    def ce_pc_run():
        allowed, deny = _pc_scope_deny()
        if deny is not None:
            return deny
        body = request.json or {}
        logins = [str(x).strip() for x in (body.get("logins") or []) if str(x).strip()]
        directologist = (body.get("directologist") or "").strip() or None
        status = (body.get("status") or default_status).strip()
        all_active = bool(body.get("all_active"))
        if all_active:
            items = pc.active_logins(victory_conn, status=status,
                                     exclude=exclude_directologs, allowed=allowed)
        else:
            if not logins and not directologist:
                return jsonify({"error": "укажите логины или директолога"}), 400
            items = pc.logins_for(victory_conn, logins=logins or None,
                                  directologist=directologist, status=status,
                                  exclude=exclude_directologs, allowed=allowed)
        if not items:
            return jsonify({"error": "по фильтру не найдено ни одного аккаунта"}), 404
        job_id = pc.new_job_id()
        try:
            pc._job_insert(victory_conn_rw, job_id, "check",
                           (session.get("username") or "").strip(),
                           [it["login"] for it in items], {"directologist": directologist},
                           len(items))
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"не удалось создать задание: {e}"}), 500
        pc.launch_background(pc.run_check_job, _pc_deps, job_id, items)
        return jsonify({"ok": True, "job_id": job_id, "total": len(items)})

    @bp.route("/api/content-editor/admin/pricecheck/status")
    @access
    def ce_pc_status():
        pc.reconcile_stuck_jobs(victory_conn_rw)   # зависшие running → interrupted
        job_id = (request.args.get("job_id") or "").strip()
        # обычный юзер видит прогресс/ошибки (в них — логины) ТОЛЬКО своих джоб;
        # 404 на чужую — не раскрываем даже факт существования
        if not _pc_job_owned(job_id):
            return jsonify({"error": "job not found"}), 404
        row = pc.job_public(victory_conn, job_id)
        if not row:
            return jsonify({"error": "job not found"}), 404
        return jsonify(row)

    @bp.route("/api/content-editor/admin/pricecheck/jobs")
    @access
    def ce_pc_jobs():
        # full access → все задания; обычный юзер → только свои
        cb = None if _content_full_access() else (session.get("username") or "").strip()
        return jsonify({"jobs": pc.jobs_recent(victory_conn, 30, created_by=cb)})

    @bp.route("/api/content-editor/admin/pricecheck/results")
    @access
    def ce_pc_results():
        allowed = _allowed_directologists()   # None=все; []=пусто; иначе скоуп
        directologist = (request.args.get("directologist") or "").strip() or None
        login = (request.args.get("login") or "").strip() or None
        only_mismatch = (request.args.get("only_mismatch") or "1").strip() not in ("0", "false", "")
        rows = pc.diff_rows(victory_conn, directologist=directologist, login=login,
                            only_mismatch=only_mismatch, allowed=allowed)
        try:
            last_run = pc.last_check_run(victory_conn)
        except Exception:  # noqa: BLE001
            last_run = None
        return jsonify({"rows": rows, "count": len(rows), "last_run": last_run})

    @bp.route("/api/content-editor/admin/pricecheck/apply", methods=["POST"])
    @access
    def ce_pc_apply():
        allowed, deny = _pc_scope_deny()
        if deny is not None:
            return deny
        body = request.json or {}
        raw = body.get("items") or []
        items = []
        for r in raw:
            login = str((r or {}).get("login") or "").strip()
            url = str((r or {}).get("url") or "").strip()
            if not login or not url:
                continue

            def _num(v):
                try:
                    return float(v) if v not in (None, "") else None
                except (TypeError, ValueError):
                    return None

            items.append({
                "login": login, "url": url, "agency": (r or {}).get("agency") or "",
                "price_direct": _num((r or {}).get("price_direct")),
                "oldprice_direct": _num((r or {}).get("oldprice_direct")),
                "price_feed": _num((r or {}).get("price_feed")),
                "oldprice_feed": _num((r or {}).get("oldprice_feed")),
            })
        if not items:
            return jsonify({"error": "не выбрано ни одной строки для заливки"}), 400
        # Скоуп по логину: обычный юзер не может залить цены в ЧУЖОЙ аккаунт
        # (даже подставив login напрямую в payload). full access → allowed=None.
        if allowed is not None:
            for it in items:
                ok, err = _login_allowed_for(it["login"], allowed)
                if not ok:
                    return jsonify({"error": err or f"нет доступа к аккаунту {it['login']}"}), 403
        # Ставим в ОЧЕРЕДЬ (status='queued'); реальный ads.update — крон 20:00 Екб.
        try:
            job_id = pc.enqueue_apply(victory_conn_rw,
                                      (session.get("username") or "").strip(), items)
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"не удалось поставить в очередь: {e}"}), 500
        return jsonify({"ok": True, "job_id": job_id, "total": len(items), "queued": True})

    @bp.route("/api/content-editor/admin/pricecheck/queue")
    @access
    def ce_pc_queue():
        pc.reconcile_stuck_jobs(victory_conn_rw)   # зависшие running → interrupted
        # full access → вся очередь; обычный юзер → только свои заявки
        cb = None if _content_full_access() else (session.get("username") or "").strip()
        return jsonify({"jobs": pc.apply_queue_for_ui(victory_conn, 500, created_by=cb)})

    # ── Управление заданием очереди: пауза / старт / удаление ─────────────
    @bp.route("/api/content-editor/admin/pricecheck/pause", methods=["POST"])
    @access
    def ce_pc_pause():
        job_id = ((request.json or {}).get("job_id") or "").strip()
        if not job_id:
            return jsonify({"error": "job_id обязателен"}), 400
        if not _pc_job_owned(job_id):   # обычный юзер — только своё задание
            return jsonify({"error": "нет доступа к заданию"}), 403
        ok = pc.request_pause(victory_conn_rw, job_id)
        if not ok:
            return jsonify({"error": "задание не активно (уже завершено/на паузе)"}), 409
        return jsonify({"ok": True})

    @bp.route("/api/content-editor/admin/pricecheck/resume", methods=["POST"])
    @access
    def ce_pc_resume():
        job_id = ((request.json or {}).get("job_id") or "").strip()
        if not job_id:
            return jsonify({"error": "job_id обязателен"}), 400
        if not _pc_job_owned(job_id):   # обычный юзер — только своё задание
            return jsonify({"error": "нет доступа к заданию"}), 403
        ok = pc.request_resume(victory_conn_rw, job_id)
        if not ok:
            return jsonify({"error": "задание не на паузе"}), 409
        return jsonify({"ok": True})

    @bp.route("/api/content-editor/admin/pricecheck/delete", methods=["POST"])
    @access
    def ce_pc_delete():
        job_id = ((request.json or {}).get("job_id") or "").strip()
        if not job_id:
            return jsonify({"error": "job_id обязателен"}), 400
        if not _pc_job_owned(job_id):   # обычный юзер — только своё задание
            return jsonify({"error": "нет доступа к заданию"}), 403
        ok = pc.request_delete(victory_conn_rw, job_id)
        if not ok:
            # нельзя удалить активное — сначала пауза
            return jsonify({"error": "удалить можно только задание в очереди или на паузе"}), 409
        return jsonify({"ok": True, "removed": True})

    @bp.route("/api/content-editor/admin/pricecheck/start_now", methods=["POST"])
    @access
    def ce_pc_start_now():
        if not _admin_allowed():
            return jsonify({"error": "запуск заливки цен из очереди доступен только админу"}), 403
        job_id = ((request.json or {}).get("job_id") or "").strip()
        if not job_id:
            return jsonify({"error": "job_id обязателен"}), 400
        if not _pc_job_owned(job_id):   # обычный юзер — только своё задание
            return jsonify({"error": "нет доступа к заданию"}), 403
        res = pc.request_start_now(victory_conn_rw, job_id)
        if res is None:
            return jsonify({"error": "задание не в очереди (уже выполняется/завершено)"}), 409
        status, items = res
        if status == "running":
            pc.launch_background(pc.run_apply_job, _pc_deps, job_id, items)
        return jsonify({"ok": True, "status": status})

    # ── Очистка очереди: обе таблицы (content_jobs + price-check jobs) ──────
    # Удаляет только ЗАВЕРШЁННЫЕ задания (done/done_with_errors/error/cancelled)
    # старше keep_days суток. Активные (queued/running/paused) не трогает
    # НЕЗАВИСИМО от возраста — очередь не должна ломать работающие задачи.
    @bp.route("/api/content-editor/admin/queue/cleanup", methods=["POST"])
    @access
    def ce_queue_cleanup():
        if not _admin_allowed():
            return jsonify({"error": "Forbidden"}), 403
        keep_days = 3
        removed_text = _jobs_exec(
            f"DELETE FROM {CE_JOBS_TABLE} "
            "WHERE status IN ('done','done_with_errors','error','cancelled') "
            "AND created_at < now() - (%s || ' days')::interval "
            "RETURNING job_id",
            (keep_days,), "all") or []
        removed_price = pc.cleanup_old_jobs(victory_conn_rw, keep_days=keep_days)
        return jsonify({"ok": True, "removed_text": len(removed_text),
                        "removed_price": removed_price, "keep_days": keep_days})

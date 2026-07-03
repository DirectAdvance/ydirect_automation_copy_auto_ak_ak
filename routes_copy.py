"""Copy-flow routes for Direct automation."""

from __future__ import annotations

from typing import Callable

from flask import current_app, jsonify, render_template, request, session


def register_copy_routes(
    bp,
    access,
    *,
    api_campaigns_func: Callable,
    account_prefill_func: Callable,
    metrika_goals_for: Callable,
    parse_number: Callable,
    copy_default_feed_path: str,
    counter_foreign_owner: Callable,
    resolve_agency_hint: Callable,
    ensure_create_worker: Callable,
    job_new: Callable,
    copy_job_upsert: Callable,
    create_jobs_ahead: Callable,
    create_jobs: dict,
    create_jobs_lock,
    copy_jobs: dict,
    copy_jobs_lock,
    feeds_preview_func: Callable,
) -> None:
    @bp.route("/api/copy_campaigns")
    @access
    def api_copy_campaigns():
        """Список кампаний источника для вкладки «Копирование кампаний»."""
        return api_campaigns_func()

    @bp.route("/api/copy_target_prefill")
    @access
    def api_copy_target_prefill():
        """Префилл целевого аккаунта для копирования: домен/гео/счётчик/цель."""
        login = (request.args.get("login") or "").strip()
        if not login:
            return jsonify({"error": "login обязателен"}), 400
        try:
            base = account_prefill_func()
            payload = base.get_json(silent=True) if hasattr(base, "get_json") else None
            if payload and not payload.get("error"):
                payload["found"] = True
                return jsonify(payload)
        except Exception:  # noqa: BLE001
            pass
        mg = metrika_goals_for(login)
        return jsonify({
            "found": False,
            "login": login,
            "domain": "",
            "href": "",
            "site_type": "",
            "city": "",
            "region": "",
            "region_id": None,
            "region_used": "",
            "counter_id": (mg["counters"][0] if mg and mg["counters"] else None),
            "counter_options": (mg["counters"] if mg else []),
            "goal_id": (mg["goal_id"] if mg else None),
            "goal_name": ("Все формы" if mg and mg["goal_id"] else None),
            "warnings": ["аккаунт не найден в local_gsheet_sites — заполни домен/гео вручную"],
        })

    @bp.route("/api/copy_start", methods=["POST"])
    @access
    def api_copy_start():
        """Запустить выборочное копирование кампаний источник → цель."""
        body = request.json or {}
        source_login = (body.get("source_login") or "").strip()
        target_login = (body.get("target_login") or "").strip()
        selected_ids = [int(x) for x in (body.get("campaign_ids") or []) if str(x).isdigit()]
        counter_id = parse_number(body.get("counter_id"), 0)
        goal_id = parse_number(body.get("goal_id"), 0)
        target_domain = (body.get("target_domain") or "").strip()
        target_city = (body.get("target_city") or "").strip()
        target_region = (body.get("target_region") or "").strip()
        target_feed_url = (body.get("target_feed_url") or copy_default_feed_path).strip()
        if not source_login or not target_login:
            return jsonify({"error": "source_login и target_login обязательны"}), 400
        if not selected_ids:
            return jsonify({"error": "выберите хотя бы одну кампанию"}), 400
        if not counter_id:
            return jsonify({"error": "счётчик Метрики обязателен"}), 400
        if not goal_id:
            return jsonify({"error": "цель «Все формы» обязательна"}), 400
        if not target_domain:
            return jsonify({"error": "укажите домен целевого аккаунта"}), 400
        if not (target_city or target_region):
            return jsonify({"error": "укажите город или регион целевого аккаунта"}), 400
        if target_feed_url and not (
            target_feed_url.startswith("/") or target_feed_url.startswith(("http://", "https://"))
        ):
            return jsonify({"error": "целевой фид должен быть абсолютным URL или путём от корня сайта"}), 400
        owner = counter_foreign_owner(counter_id, target_login)
        if owner:
            return jsonify({
                "error": f"счётчик Метрики {counter_id} принадлежит аккаунту «{owner}», а не «{target_login}»"
            }), 400

        body = dict(body)
        body["_kind"] = "copy_campaigns"
        body["login"] = target_login
        resolved_ag = resolve_agency_hint(target_login, (body.get("agency") or "").strip())
        if resolved_ag:
            body["agency"] = resolved_ag
        app = current_app._get_current_object()
        ensure_create_worker(app)
        saved_session = dict(session)
        with create_jobs_lock:
            existing_ids = set(create_jobs.keys())
        job_id = job_new(len(selected_ids), target_login, body, saved_session, dedup_login=True)
        if job_id in existing_ids:
            with create_jobs_lock:
                ahead = create_jobs_ahead(job_id)
            return jsonify({
                "ok": True,
                "job_id": job_id,
                "total": len(selected_ids),
                "login": target_login,
                "agency": resolved_ag or "",
                "kind": "copy_campaigns",
                "ahead": ahead,
                "existing": True,
                "note": "для целевого аккаунта уже есть активная задача; дубль копирования не создан",
            })
        copy_job_upsert(job_id, status="queued", progress=0, source_login=source_login,
                        target_login=target_login, selected=len(selected_ids), total=len(selected_ids))
        with create_jobs_lock:
            ahead = create_jobs_ahead(job_id)
        return jsonify({"ok": True, "job_id": job_id, "total": len(selected_ids),
                        "login": target_login, "agency": resolved_ag or "",
                        "kind": "copy_campaigns", "ahead": ahead})

    @bp.route("/api/copy_status/<job_id>")
    @access
    def api_copy_status(job_id: str):
        with copy_jobs_lock:
            job = dict(copy_jobs.get(job_id) or {})
        if not job:
            return jsonify({"error": "job не найден"}), 404
        return jsonify(job)

    @bp.route("/api/copy_feeds_preview", methods=["POST"])
    @access
    def api_copy_feeds_preview():
        """Фиды для секции «Замена фидов»: исходные (что заменяем) + фиды целевого аккаунта (на что)."""
        body = request.json or {}
        source_login = (body.get("source_login") or "").strip()
        target_login = (body.get("target_login") or "").strip()
        if not source_login or not target_login:
            return jsonify({"error": "source_login и target_login обязательны"}), 400
        selected_ids = {int(x) for x in (body.get("campaign_ids") or []) if str(x).isdigit()}
        try:
            data = feeds_preview_func(source_login, target_login, selected_ids)
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"не удалось получить фиды: {str(e)[:200]}"}), 502
        return jsonify(data)

    @bp.route("/automation/copy")
    @access
    def copy_page():
        """Отдельная изолированная страница копирования кампаний (свой процесс
        direct-copy.service). Прогресс тянется с /api/copy_status — без общего
        рендера карточек очереди создания РК."""
        return render_template("direct/copy.html")

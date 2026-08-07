"""Copy-flow routes for Direct automation."""

from __future__ import annotations

from typing import Callable

from flask import current_app, jsonify, render_template, request, session

from ..copy_service.copy_request import default_geo_mode, parse_campaign_ids, validate_other_geo


# GeoRegionId России в справочнике GeoRegions Директа. Виджет выбора регионов
# «Прочих сфер» показывает ТОЛЬКО Россию и её вложенность (округа → области →
# города → районы города): копируем кампании российских аккаунтов, остальные
# страны в дереве — шум. Фильтр живёт здесь, а не в blueprint_metrika._geo_regions_tree,
# потому что общий справочник (_geo_load / _geo_id / _geo_is_valid_id) нужен целиком.
#
# ⚠️ ФАКТ справочника Директа (проверено 2026-07-17 живым запросом dictionaries):
# «Республика Крым» (977) и её города, включая Севастополь (959), в GeoRegions висят
# ОТДЕЛЬНЫМ корнем — ParentId=0, они НЕ потомки 225. Поэтому в корни фильтра включены
# ОБА: Россия (225) и Крым (977) — по решению Семёна 2026-07-17 гео по Крыму нужно.
# Каждый корень раскрывается в своё поддерево; если понадобится ещё регион — дописать id.
_GEO_ROOT_IDS = (225, 977)


def _geo_only_russia(rows: list) -> list:
    """Оставляет корни _GEO_ROOT_IDS (Россия 225 + Крым 977) и ВСЕХ их транзитивных
    потомков. Порядок исходного списка сохраняется; корни имеют parent_id=0 и станут
    корнями дерева на фронте. Если ни одного корня в справочнике нет — возвращает
    список как есть (лучше показать всё, чем пустое дерево)."""
    ids = {int(r.get("id")) for r in rows if r.get("id") is not None}
    roots = [rid for rid in _GEO_ROOT_IDS if rid in ids]
    if not roots:
        return rows
    children: dict = {}
    for r in rows:
        pid = int(r.get("parent_id") or 0)
        if pid:
            children.setdefault(pid, []).append(int(r["id"]))
    keep = set(roots)
    stack = list(roots)
    while stack:                       # обход всего поддерева, не только прямых детей
        for cid in children.get(stack.pop(), ()):
            if cid not in keep:
                keep.add(cid)
                stack.append(cid)
    return [r for r in rows if int(r.get("id")) in keep]


def _parse_campaign_ids(body: dict, *, as_set: bool = False):
    """Единый разбор campaign_ids из тела запроса (устраняет троекратное дублирование).
    Принимает список строк/чисел, пропускает нечисловые. as_set=True → frozenset для O(1) lookup."""
    return parse_campaign_ids(body, as_set=as_set)


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
    feeds_check_func: Callable | None = None,
    geo_regions_func: Callable | None = None,          # legacy flat list (backward compat)
    geo_regions_tree_func: Callable | None = None,     # дерево [{id,name,type,parent_id}]
    geo_validate_id_func: Callable | None = None,      # abs(id) → bool (валидный id из справочника)
    images_upload_func: Callable | None = None,
    target_campaigns_info_func: Callable | None = None,  # кол-во кампаний на цели (read-only, для UI очистки)
    repair_pending_func: Callable | None = None,  # job_id → bool: есть ли незакрытая запись в direct_delayed_repairs
    job_db_get_func: Callable | None = None,  # fallback copy_status после рестарта direct-copy.service
    copy_queue_func: Callable | None = None,  # список последних copy-джоб для вкладки «Очередь»
    metrika_pair_problem: Callable | None = None,  # (counter, goal, login) -> (kind, msg) | None
    login_allowed_func: Callable[[str], tuple[bool, str]] | None = None,  # scoped content-user guard
    archived_campaign_ids_func: Callable | None = None,  # login, ids, agency -> set[int] archived by v5
) -> None:
    def _copy_ts(value):
        if hasattr(value, "timestamp"):
            try:
                return value.timestamp()
            except Exception:  # noqa: BLE001
                return None
        return value

    def _copy_status_from_db(row: dict) -> dict:
        body = row.get("body") if isinstance(row.get("body"), dict) else {}
        campaign_ids = body.get("campaign_ids") or []
        total = int(row.get("total") or len(campaign_ids) or 0)
        done = int(row.get("done") or row.get("created") or 0)
        status = row.get("status")
        progress = 100 if status in ("done", "error", "cancelled", "interrupted") else (
            min(99, round(done * 100 / total)) if total else 0
        )
        return {
            "job_id": row.get("job_id"),
            "status": status,
            "progress": progress,
            "total": total,
            "done": done,
            "created": int(row.get("created") or 0),
            "failed": int(row.get("failed") or 0),
            "source_login": body.get("source_login"),
            "target_login": body.get("target_login") or row.get("login"),
            "selected": len(campaign_ids) or total,
            "created_at": _copy_ts(row.get("created_at")),
            "updated_at": _copy_ts(row.get("updated_at")),
            "result": row.get("result"),
            "error": row.get("error"),
            "settling": False,
        }

    def _copy_login_guard(login: str, label: str = "login"):
        login = (login or "").strip()
        if not login:
            return jsonify({"error": f"{label} обязателен"}), 400
        if login_allowed_func is None:
            return None
        ok, err = login_allowed_func(login)
        if ok:
            return None
        return jsonify({"error": err or f"{label} вне вашего доступа"}), 403

    def _copy_campaign_is_archived(row: dict) -> bool:
        state = str((row or {}).get("state") or "").upper()
        status = str((row or {}).get("status") or "").upper()
        return state == "ARCHIVED" or status == "ARCHIVED" or bool((row or {}).get("archived"))

    def _copy_campaign_id(row: dict) -> int:
        try:
            return int((row or {}).get("id") or 0)
        except (TypeError, ValueError):
            return 0

    def _copy_hide_archived_campaigns(resp):
        payload = resp.get_json(silent=True) if hasattr(resp, "get_json") else None
        if not isinstance(payload, dict) or not isinstance(payload.get("campaigns"), list):
            return resp
        rows = payload.get("campaigns") or []
        archived_ids: set[int] = set()
        if archived_campaign_ids_func is not None:
            ids = [_copy_campaign_id(c) for c in rows]
            ids = [cid for cid in ids if cid > 0]
            if ids:
                try:
                    archived_ids = {
                        int(cid) for cid in archived_campaign_ids_func(
                            (request.args.get("login") or "").strip(),
                            ids,
                            (request.args.get("agency") or "").strip(),
                        )
                    }
                except Exception as exc:  # noqa: BLE001 — основной ответ уже есть, не валим список
                    current_app.logger.warning("copy archived campaign probe failed: %s", str(exc)[:200])
        visible = [
            c for c in rows
            if _copy_campaign_id(c) not in archived_ids and not _copy_campaign_is_archived(c)
        ]
        hidden = len(rows) - len(visible)
        if not hidden:
            return resp
        payload = dict(payload)
        payload["campaigns"] = visible
        payload["archived_hidden"] = int(payload.get("archived_hidden") or 0) + hidden
        status_code = getattr(resp, "status_code", 200) or 200
        return jsonify(payload), status_code

    @bp.route("/api/copy_campaigns")
    @access
    def api_copy_campaigns():
        """Список кампаний источника для вкладки «Копирование кампаний»."""
        denied = _copy_login_guard(request.args.get("login"), "login")
        if denied is not None:
            return denied
        return _copy_hide_archived_campaigns(api_campaigns_func())

    @bp.route("/api/copy_target_prefill")
    @access
    def api_copy_target_prefill():
        """Префилл целевого аккаунта для копирования: домен/гео/счётчик/цель."""
        login = (request.args.get("login") or "").strip()
        denied = _copy_login_guard(login, "login")
        if denied is not None:
            return denied
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

    # Имя обязано начинаться с "copy_": nginx отдаёт на этот сервис (:5022) только
    # ^~ /direct/api/copy_, всё остальное /direct/ уходит в direct-create (:5020).
    @bp.route("/api/copy_geo_regions")
    @access
    def api_geo_regions():
        """Дерево GeoRegions Директа [{id,name,type,parent_id}] для виджета «Прочие сферы».
        Показывает Country / Federal district / Region / Federation subject / City / City district.
        Скрытые типы переподвешены к ближайшему показанному предку.
        Отдаётся ТОЛЬКО Россия и её вложенность (см. _geo_only_russia).
        Переиспользует кэш blueprint_metrika._geo_load — дополнительных запросов к API нет."""
        fn = geo_regions_tree_func or geo_regions_func
        if fn is None:
            return jsonify({"error": "geo_regions_func не подключена"}), 503
        try:
            rows = fn()
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"не удалось получить GeoRegions: {str(e)[:200]}"}), 502
        # legacy geo_regions_func отдаёт [{id,name}] без parent_id — фильтр по дереву
        # применим только к древовидному источнику.
        if geo_regions_tree_func is not None:
            rows = _geo_only_russia(rows)
        return jsonify({"regions": rows})

    @bp.route("/api/copy_target_campaigns")
    @access
    def api_copy_target_campaigns():
        """Снимок кампаний целевого аккаунта (read-only) для UI очистки перед копированием.
        Имя обязано начинаться с 'copy_': nginx отдаёт на этот сервис (:5022) только ^~ /direct/api/copy_.
        Возвращает {total, draft_count, non_draft_count, archivable_count, breakdown}."""
        if target_campaigns_info_func is None:
            return jsonify({"error": "target_campaigns_info_func не подключена"}), 503
        login = (request.args.get("login") or "").strip()
        denied = _copy_login_guard(login, "login")
        if denied is not None:
            return denied
        try:
            data = target_campaigns_info_func(login)
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)[:200]}), 502
        if "error" in data:
            return jsonify(data), 502
        return jsonify(data)

    @bp.route("/api/copy_images_upload", methods=["POST"])
    @access
    def api_copy_images_upload():
        """Загрузить изображения в целевой аккаунт Директа.
        multipart/form-data: images[] (files), target_login (field).
        Каждый файл: Pillow → RGB → JPEG quality=80 → adimages.add (Grid upload).
        Возвращает {results:[{name,hash}], errors:[...]}.
        При отсутствии Pillow на сервере — 503."""
        if images_upload_func is None:
            return jsonify({"error": "images_upload_func не подключена"}), 503
        target_login = (request.form.get("target_login") or "").strip()
        denied = _copy_login_guard(target_login, "target_login")
        if denied is not None:
            return denied
        files = request.files.getlist("images")
        if not files:
            return jsonify({"error": "нет файлов"}), 400
        files_data = []
        read_errors: list[dict] = []
        for f in files:
            fname = f.filename or "?"
            try:
                raw = f.read()
                if raw:
                    files_data.append((f.filename or "image.jpg", raw))
                # пустой файл — молча пропускаем, не ошибка
            except Exception as _re:  # noqa: BLE001
                # Записываем ошибку чтения явно: пользователь должен знать, какой файл пропал
                # и почему. Без этого round-robin смещается без предупреждения.
                read_errors.append({"name": fname, "error": f"чтение файла: {str(_re)[:200]}"})
        if not files_data:
            return jsonify({"error": "нет читаемых файлов", "errors": read_errors}), 400
        try:
            result = images_upload_func(target_login, files_data)
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"upload ошибка: {str(e)[:300]}"}), 502
        if read_errors:
            result["errors"] = (result.get("errors") or []) + read_errors
        code = result.pop("code", None)
        if code:
            return jsonify(result), int(code)
        return jsonify(result)

    def _copy_start_impl(body: dict, mode: str):
        """Общее тело запуска копирования источник → цель.

        Режим задаёт РОУТ (см. api_copy_start / api_copy_other_start), а не payload клиента:
        «Авто» и «Прочие сферы» физически разведены по эндпоинтам, подменить режим полем mode
        нельзя. Раньше был один /api/copy_start с дискриминатором body['mode'].
        """
        source_login = (body.get("source_login") or "").strip()
        target_login = (body.get("target_login") or "").strip()
        selected_ids = _parse_campaign_ids(body)
        counter_id = parse_number(body.get("counter_id"), 0)
        goal_id = parse_number(body.get("goal_id"), 0)
        target_domain = (body.get("target_domain") or "").strip()
        target_city = (body.get("target_city") or "").strip()
        target_region = (body.get("target_region") or "").strip()
        target_feed_url = (body.get("target_feed_url") or copy_default_feed_path).strip()
        # Дефолт geo_mode зависит от режима: у «Прочих сфер» валидны только keep/change, поэтому
        # общий дефолт 'replace' дал бы 400 на запросе без geo_mode. Для «Авто» 'replace' — прежнее.
        geo_mode = default_geo_mode(body, mode)
        if not source_login or not target_login:
            return jsonify({"error": "source_login и target_login обязательны"}), 400
        for label, login in (("source_login", source_login), ("target_login", target_login)):
            denied = _copy_login_guard(login, label)
            if denied is not None:
                return denied
        if not selected_ids:
            return jsonify({"error": "выберите хотя бы одну кампанию"}), 400
        if not counter_id:
            return jsonify({"error": "счётчик Метрики обязателен"}), 400
        if not goal_id:
            return jsonify({"error": "цель «Все формы» обязательна"}), 400
        if not target_domain:
            return jsonify({"error": "укажите домен целевого аккаунта"}), 400
        # Гео-валидация: проверяем mode и geo_mode по белым спискам перед запуском job.
        geo_error, _parsed_geo_ids = validate_other_geo(
            body, mode, geo_mode, geo_validate_id_func=geo_validate_id_func, surface="ui"
        )
        if geo_error:
            return jsonify({"error": geo_error}), 400
        if mode == "other":
            # Картинки: 'copy' — переносим картинки исходного аккаунта 1в1 (дефолт, обратная
            # совместимость со старым payload без image_mode); 'upload' — ставим загруженные
            # round-robin. В режиме 'copy' хэши игнорируем ЖЁСТКО: иначе случайно оставленный
            # image_hashes подменил бы картинки молча.
            _img_mode = (body.get("image_mode") or "copy").strip()
            if _img_mode not in ("copy", "upload"):
                return jsonify({
                    "error": f"image_mode='{_img_mode}' недопустим; допустимы: copy, upload"
                }), 400
            if _img_mode == "upload" and not (body.get("image_hashes") or []):
                return jsonify({
                    "error": "image_mode='upload': не передан ни один загруженный хэш картинки"
                }), 400
            if _img_mode == "copy":
                body["image_hashes"] = []
            body["image_mode"] = _img_mode
        else:  # mode == "auto"
            if not (target_city or target_region):
                return jsonify({"error": "укажите город или регион целевого аккаунта"}), 400
        if target_feed_url and not (
            target_feed_url.startswith("/") or target_feed_url.startswith(("http://", "https://"))
        ):
            return jsonify({"error": "целевой фид должен быть абсолютным URL или путём от корня сайта"}), 400
        # Счётчик И цель проверяем вместе: перепутанный кабинет обычно даёт чужую пару целиком.
        # «Не удалось проверить» — это НЕ «проверено и чисто»: fail-open здесь означал бы залить
        # кабинет с целью другого клиента, поэтому отвечаем 503 и просим повторить.
        pair_problem = metrika_pair_problem(counter_id, goal_id, target_login) if metrika_pair_problem else None
        if pair_problem:
            kind, message = pair_problem
            return jsonify({"error": message}), (400 if kind == "error" else 503)
        owner = counter_foreign_owner(counter_id, target_login)
        if owner:
            return jsonify({
                "error": f"счётчик Метрики {counter_id} принадлежит аккаунту «{owner}», а не «{target_login}»"
            }), 400
        # target_cleanup — очистка целевого аккаунта ДО копирования.
        # Вкладка «Авто» параметр не отправляет → дефолт 'none' → поведение не меняется.
        target_cleanup = (body.get("target_cleanup") or "none").strip()
        if target_cleanup not in ("none", "delete_drafts", "archive"):
            return jsonify({"error": f"target_cleanup='{target_cleanup}' недопустимо; допустимо: none, delete_drafts, archive"}), 400

        body = dict(body)
        # Режим форсим ИЗ РОУТА в тело джоба: _copy_run_job/постпроцесс читают body["mode"], а не
        # параметр. Без этой строки режим определялся бы полем клиента (для other спасало лишь то,
        # что copy_other.js шлёт mode:'other') — и запрос на copy_other_start с mode:'auto' в теле
        # молча ушёл бы по ветке «Авто». Теперь роут — единственный источник режима.
        body["mode"] = mode
        body["_kind"] = "copy_campaigns"
        body["login"] = target_login
        body["created_by"] = (session.get("username") or "").strip()
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

    # ⛔ ОБА роута обязаны начинаться с /api/copy_ — nginx отдаёт на :5022 только `^~ /direct/api/copy_`.
    # Роут без этого префикса молча уедет в direct-create (:5020) и вернёт 404; локальный curl на
    # 127.0.0.1:5022 такой баг МАСКИРУЕТ (так уже потеряли geo_regions).

    @bp.route("/api/copy_start", methods=["POST"])
    @access
    def api_copy_start():
        """Вкладка «Авто». Режим фиксирован роутом.

        Историческая совместимость: старый клиент мог прислать mode='other' сюда же. Такой payload
        больше НЕ принимаем — у «Прочих сфер» свой эндпоинт /api/copy_other_start.
        """
        body = request.json or {}
        _m = (body.get("mode") or "auto").strip()
        if _m != "auto":
            return jsonify({
                "error": f"mode='{_m}' не принимается этим роутом; «Прочие сферы» → /direct/api/copy_other_start"
            }), 400
        return _copy_start_impl(body, "auto")

    @bp.route("/api/copy_other_start", methods=["POST"])
    @access
    def api_copy_other_start():
        """Вкладка «Прочие сферы». Режим задаёт роут — поле mode из payload игнорируется."""
        return _copy_start_impl(request.json or {}, "other")

    @bp.route("/api/copy_status/<job_id>")
    @access
    def api_copy_status(job_id: str):
        with copy_jobs_lock:
            job = dict(copy_jobs.get(job_id) or {})
        if not job and job_db_get_func is not None:
            try:
                row = job_db_get_func(job_id)
            except Exception:  # noqa: BLE001
                row = None
            if row and row.get("kind") == "copy_campaigns":
                job = _copy_status_from_db(row)
        if not job:
            return jsonify({"error": "job не найден"}), 404
        # repair_pending: persistent добивка в direct_delayed_repairs (content_repair).
        # Проверяем только для done-статуса — в остальных случаях основная работа ещё идёт.
        # Best-effort: Victory недоступна → False (не подвешиваем поллинг).
        if job.get("status") == "done" and repair_pending_func is not None:
            try:
                job["repair_pending"] = bool(repair_pending_func(job_id))
            except Exception:  # noqa: BLE001
                job["repair_pending"] = False
        return jsonify(job)

    @bp.route("/api/copy_queue")
    @access
    def api_copy_queue():
        """Список последних copy-джоб для вкладки «Очередь» в редакторе контента.
        Читает из victoryads_direct_automation.jobs (kind='copy_campaigns') — тот же источник,
        что и UI очереди direct-create; НЕ дублирует хранение, только читает.
        Автор берётся из body.created_by, область видимости — по directologist-правам."""
        if copy_queue_func is None:
            return jsonify({"jobs": []})
        try:
            jobs = copy_queue_func()
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)[:200]}), 502
        return jsonify({"jobs": jobs})

    @bp.route("/api/copy_feeds_preview", methods=["POST"])
    @access
    def api_copy_feeds_preview():
        """Фиды для секции «Замена фидов»: исходные (что заменяем) + фиды целевого аккаунта (на что)."""
        body = request.json or {}
        source_login = (body.get("source_login") or "").strip()
        target_login = (body.get("target_login") or "").strip()
        if not source_login or not target_login:
            return jsonify({"error": "source_login и target_login обязательны"}), 400
        for label, login in (("source_login", source_login), ("target_login", target_login)):
            denied = _copy_login_guard(login, label)
            if denied is not None:
                return denied
        selected_ids = _parse_campaign_ids(body, as_set=True)
        try:
            data = feeds_preview_func(source_login, target_login, selected_ids)
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"не удалось получить фиды: {str(e)[:200]}"}), 502
        return jsonify(data)

    @bp.route("/api/copy_feeds_check", methods=["POST"])
    @access
    def api_copy_feeds_check():
        """Сухой прогон автосопоставления фидов вкладки «Прочие сферы» — только чтение.
        Показывает ДО запуска job: что будет замаплено, что не найдено, какие кампании пропустятся.
        Использует ту же функцию _feed_auto_match_one, что и job — логика не расходится."""
        if feeds_check_func is None:
            return jsonify({"error": "feeds_check_func не подключена"}), 503
        body = request.json or {}
        source_login = (body.get("source_login") or "").strip()
        target_login = (body.get("target_login") or "").strip()
        if not source_login or not target_login:
            return jsonify({"error": "source_login и target_login обязательны"}), 400
        for label, login in (("source_login", source_login), ("target_login", target_login)):
            denied = _copy_login_guard(login, label)
            if denied is not None:
                return denied
        selected_ids = _parse_campaign_ids(body, as_set=True)
        try:
            data = feeds_check_func(source_login, target_login, selected_ids)
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"ошибка проверки фидов: {str(e)[:200]}"}), 502
        if "error" in data:
            return jsonify(data), 502
        return jsonify(data)

    @bp.route("/api/copy_feed_upload", methods=["POST"])
    @access
    def api_copy_feed_upload():
        """Загрузить фид source-аккаунта в target-аккаунт (feeds.add v5) с адаптацией URL
        под целевой домен (source-домен → target_domain в ссылке фида).

        Body (JSON): {source_login, target_login, source_feed_id, target_domain}
        Returns: {ok:true, feed_id:int, name:str, url:str} | {error:str}

        ⚠️ Имя начинается с /api/copy_: nginx отдаёт на :5022 только ^~ /direct/api/copy_.
        """
        from direct.copy_service import copy_feed_upload as _cfu  # noqa: PLC0415 — lazy; модуль этой задачи
        body = request.json or {}
        source_login = (body.get("source_login") or "").strip()
        target_login = (body.get("target_login") or "").strip()
        source_feed_id_raw = body.get("source_feed_id")
        target_domain = (body.get("target_domain") or "").strip()
        if not source_login or not target_login:
            return jsonify({"error": "source_login и target_login обязательны"}), 400
        for label, login in (("source_login", source_login), ("target_login", target_login)):
            denied = _copy_login_guard(login, label)
            if denied is not None:
                return denied
        if source_feed_id_raw is None:
            return jsonify({"error": "source_feed_id обязателен"}), 400
        try:
            source_feed_id = int(source_feed_id_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "source_feed_id должен быть целым числом"}), 400
        if not target_domain:
            return jsonify({"error": "target_domain обязателен"}), 400
        try:
            result = _cfu.upload_feed_to_target(
                source_login, target_login, source_feed_id, target_domain
            )
        except RuntimeError as e:
            return jsonify({"error": str(e)[:400]}), 502
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"внутренняя ошибка: {str(e)[:300]}"}), 500
        return jsonify({"ok": True, **result})

    @bp.route("/automation/copy")
    @access
    def copy_page():
        """Отдельная изолированная страница копирования кампаний (свой процесс
        direct-copy.service). Прогресс тянется с /api/copy_status — без общего
        рендера карточек очереди создания РК."""
        return render_template("direct/copy.html")

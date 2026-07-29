# INDEX — direct

> Сгенерировано `scripts/gen_project_index.py`. Руками не править — перегенерировать.
> Назначение: найти нужный файл БЕЗ обхода дерева грепом.

Файлов в индексе: **346**

## корень проекта

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `ARCHITECTURE.md` | 41 | ARCHITECTURE — карта кластеров и связности `direct/` |  |
| `ARCHITECTURE_AUDIT_2026-07-12.md` | 4 | Direct automation: audit of the current split (2026-07-12) |  |
| `BLUEPRINT_SPLIT_PLAN.md` | 4 | План распила blueprint.py (8522 строки, 398 def/class) |  |
| `CAMPAIGN_INVARIANTS.md` | 14 | Инварианты создания кампаний (нейродиректолог) |  |
| `CLAUDE.md` | 10 | CLAUDE.md — Нейродиректолог (home/seoadvanced/direct) |  |
| `CODER.md` | 50 | Кодер — унифицированное имя кампании/группы |  |
| `CONTENT_EDITOR.md` | 20 | Content Editor Service |  |
| `CONTENT_EDITOR_COOKIE_GRID.md` | 4 | Content Editor Cookie/Grid Plan |  |
| `COOKIES_STATUS_CHECKER.md` | 7 | Чекер статуса кук (виджет "Куки: ...") |  |
| `COPY_INDEX.md` | 18 | COPY_INDEX — карта сервиса копирования кампаний (для ИИ) |  |
| `COPY_README.md` | 16 | Сервис копирования кампаний Яндекс.Директа |  |
| `CREATION_PROTECTED_RULES.md` | 10 | Защищённые правила создания РК (спец-логика «Структура слепков» → «Создание РК») |  |
| `DOD.md` | 187 | DoD — Definition of Done (создание РК нейродиректолога) |  |
| `DOD_ARCHIVE.md` | 12 | DOD_ARCHIVE — технические детали (перенесены из DOD.md 2026-07-16) |  |
| `ERRORS_JOURNAL.md` | 966 | 📒 Журнал ошибок создания кампаний — нейродиректолог |  |
| `ERRORS_JOURNAL_ARCHIVE.md` | 16 | ERRORS_JOURNAL_ARCHIVE — старые записи (переведены из ERRORS_JOURNAL.md 2026-07-16) |  |
| `EXTRACTION_PLAN.md` | 18 | EXTRACTION_PLAN.md — вынос `/direct/automation` в отдельный сервис |  |
| `INDEX.md` | 55 | INDEX — direct |  |
| `MEMORY.md` | 117 | ⚡ Дубль структуры = ТОЛЬКО внутри одного слепка И одного типа сайта (правило Семёна 2026-07-29) |  |
| `POSEVY_AUTORULES_PLAN.md` | 11 | Оптимизация уже созданных посевов (tp8/tp9/tp10) в системе автоправил — план |  |
| `README.md` | 60 | Автоматизация Директа — `direct/` |  |
| `README_ARCHIVE.md` | 8 | README_ARCHIVE — исторические разделы (перенесены из README.md 2026-07-16) |  |
| `SLEPKI_AUDIT_2026-07-12.md` | 4 | Slepki audit (2026-07-12) |  |
| `SLEPKI_REBUILD_PLAN.md` | 22 | Слепки — изменения 2026-07-14 + основа плана пересбора/дополнения |  |
| `STATE.md` | 200 | Нейродиректолог — Состояние |  |
| `STATE_ARCHIVE.md` | 929 | Нейродиректолог — STATE.md архив (сессии старше 3 дней) |  |
| `STATE_COPY_OTHER.md` | 31 | STATE — вкладка «Прочие сферы» сервиса копирования (/direct/automation/copy) |  |
| `STRUCTURE_EXCLUSIONS.md` | 25 | STRUCTURE_EXCLUSIONS — реестр НАМЕРЕННЫХ решений по структуре слепков |  |
| `UI_MAP.md` | 13 | UI-карта: seoadvanced.ru/direct/automation |  |
| `_PROPOSAL_camp_names_20260729.md` | 8 | Имена кампаний tp2–tp5 пака авто — PROPOSAL |  |
| `_PROPOSAL_dupes_20260729.md` | 3 | Схлопывание дублей «марка ⊃ марка+модель» — PROPOSAL |  |
| `_PROPOSAL_monobrand_split_20260729.md` | 7 | Разделение типа сайта «Монобренд» на «Монобренд · <Марка>» |  |
| `_PROPOSAL_samect_dupes_20260729.md` | 6 | Дубли «один ct внутри одной кампании» — PROPOSAL |  |
| `_PROPOSAL_slepki_20260729.md` | 29 | Proposal: группы слепков на решение Семёна |  |
| `__init__.py` | 1 |  |  |
| `_proposed_piterkina_delete.md` | 27 | PROPOSAL — правки слепка `piterkina` (НЕ применено) |  |
| `account_filters.py` | 1 | Единый источник WHERE-условий для выборки Авто-аккаунтов из local_gsheet_sites. | base_account_where |
| `account_service.py` | 54 | Account-facing Direct application service without blueprint imports. | configure |
| `accounts_main.py` | 7 | Standalone Flask entrypoint for the read-only Direct dashboards | create_app |
| `agent_board_bridge.py` | 16 | Bridge failed Direct content jobs to Agent Board. | ensure_content_job_agent_column, ensure_price_job_agent_column, ensure_copy_job_agent_columns, notify_content_ |
| `ai_agents.py` | 182 | ИИ-агенты «слепки директологов» для генерации контента Я.Директа. | eff_len, desc_limit, next_promo_type, is_bu_site_type, strip_used_words, alien_signature, has_forbidden_claim, |
| `ai_agents_data.py` | 93 | Данные-литералы, вынесенные из ai_agents.py (шаг 1 разукрупнения, механический перенос). |  |
| `ai_content.py` | 52 | Генерация AI-контента объявлений + слепок-контент (кэш + БД) — вынесено из blueprint.py. | configure, generate_post_ad_content |
| `ai_main.py` | 6 | Standalone Flask entrypoint for the Direct «Обучение ИИ» API only. | create_app |
| `automation_runtime.py` | 227 | Direct Automation runtime — domain wiring without Flask route registration. |  |
| `autorules_main.py` | 6 | Standalone Flask entrypoint for Direct Autorules (auto-rules engine). | create_app |
| `blueprint.py` | 13 | Flask composition root for Direct Automation. | init_direct |
| `blueprint_content_rules.py` | 13 | Правила вкладки «Контент» (public.direct_content_asset_rules) + фильтрация ассетов — | configure |
| `blueprint_metrika.py` | 13 | Метрика (счётчики/цели из public.metrika_goals) + гео-справочник Директа — | configure |
| `blueprint_targeting.py` | 39 | Классификатор ct → сегмент (Марки/Модели/Общее) + профиль таргетинга слепков — | configure, ruleset_name, ruleset_for |
| `campaign.py` | 26 | create_master_campaign.py | set_agency_resolver, fetch_cookie_glavpotok, load_cookie_local, load_cookie, load_feeds_catalog, feed_url, lis |
| `campaign_naming.py` | 16 | Кодер-имена кампаний (марка/модель из ct-кодера) + ротатор Title2 — вынесено из blueprint.py. | configure |
| `campaign_result.py` | 1 | Normalization helpers for create_set result rows. | as_int, result_name, result_id, campaign_kind, created_campaigns |
| `campaign_spec_audit.py` | 178 | Declarative per-tp campaign spec + live auditor + fixers. | configure, audit_campaign, audit_account_jobs, fix_keywords_wrong_group, fix_foreign_model_keywords, fix_sitel |
| `campaign_state_verifier.py` | 1 | Pure checks for live campaign row state/name consistency. | verify_campaign_state |
| `city_morph.py` | 14 | Морфология и фильтрация городов в контенте объявлений — вынесено из blueprint.py. | configure |
| `content_callouts_routes.py` | 15 | Content-editor callout helpers and routes. | register_callouts_routes |
| `content_dashboard_routes.py` | 16 | Read-only дашборд-роуты редактора контента Директа. | register_content_dashboards |
| `content_editor_helpers.py` | 66 | Shared helpers for the Direct content-editor modules. | ensure_jobs_table |
| `content_images_routes.py` | 72 | Роуты вкладки «Смена изображения» редактора контента Директа. | run_image_replace, register_image_routes |
| `content_jobs.py` | 3 | Postgres queue helpers for the Direct content editor. | ensure_jobs_table |
| `content_jobs_routes.py` | 3 | Content-editor job queue routes: /jobs /status /cancel. | register_jobs_routes |
| `content_main.py` | 4 | Standalone Flask entrypoint for Direct content editor only. | create_app |
| `content_price_check_routes.py` | 14 | Роуты «Сверки цен» редактора контента Директа (admin-only). | register_price_check_routes |
| `content_quality.py` | 15 | Единый контур качества контента: генерация → проверка → регенерация (тот же LLM | retry_regen, brand_head_ok, regen_titles, judge_utp, audit_and_regen_utp |
| `content_renames_routes.py` | 15 | Роуты вкладки «Смена названий» редактора контента Директа. | run_rename, register_rename_routes |
| `content_replace_routes.py` | 45 | Content-editor replace helpers and routes. | register_replace_routes |
| `content_sitelinks_routes.py` | 50 | Content-editor sitelink helpers and routes. | register_sitelinks_routes |
| `content_worker.py` | 11 | Воркер Postgres-очереди редактора контента (direct-content-worker.service). | main |
| `copy_api.py` | 30 | Программный API сервиса копирования кампаний Директа. | register_copy_api |
| `copy_asset_steps.py` | 25 | Asset/settings шаги copy-постпроцесса: callouts, sitelinks, promos, bidmods, minus-places. | source_has_network, pull_source_campaign_assets, step_age_bidmods, step_disabled_places, step_attach_callouts, |
| `copy_cleanup.py` | 11 | Инфо о кампаниях цели и очистка черновиков перед копированием. | configure |
| `copy_context.py` | 1 | Общий контекст шагов copy-постпроцесса. | CopyCtx |
| `copy_creative_steps.py` | 14 | Creative/video шаги copy-постпроцесса. | step_adaptive_creatives, step_videos |
| `copy_engine.py` | 57 | Копирование кампаний Яндекс.Директа 1:1 (обёртки-оркестрация поверх внешнего движка | configure |
| `copy_feed_upload.py` | 7 | Загрузка фида в целевой аккаунт Директа через feeds.add (v5). | upload_feed_to_target |
| `copy_feeds.py` | 6 | Фиды копирования: preview, target feed id, валидация feed-map. | configure |
| `copy_geo.py` | 15 | Гео-слой копирования: гео-замены, ремап r-кода кодера, нормализация имён/href. | configure |
| `copy_geo_morph.py` | 14 | copy_geo_morph — морфологически корректная гео-замена для копировщика РК (ФАЗА 3a, п.6). | paradigm_for, build_geo_pairs, apply_replacements, scan_residual |
| `copy_grid_read.py` | 4 | Чтение выбранных Grid-кампаний источника. | configure |
| `copy_grid_steps.py` | 19 | Grid-докрутка скопированных кампаний: callouts-мост, шаги, видео-резолвер. | configure |
| `copy_grid_unified.py` | 33 | Grid-only UnifiedCampaign copy path extracted from copy_engine. | configure |
| `copy_images.py` | 10 | Ремап картинок между кабинетами (v501 + Grid хэши). | configure |
| `copy_jobs.py` | 3 | Copy-джобы: in-memory состояние очереди копирования + зеркало в create-карточку. | configure |
| `copy_keyword_steps.py` | 13 | Шаг дозаливки ключевых фраз copy-постпроцесса. | step_keywords |
| `copy_main.py` | 14 | Standalone Flask entrypoint for the Direct campaign-copy service only. | create_app |
| `copy_metrika.py` | 6 | Подстановка счётчика/цели Метрики в стратегию копируемых кампаний. | configure |
| `copy_other.py` | 23 | Вспомогательные функции вкладки «Прочие сферы» (режим mode='other') сервиса copy_engine. |  |
| `copy_postprocess.py` | 46 | Cookie/Grid postprocess for Direct copy jobs. | configure |
| `copy_price_steps.py` | 10 | Шаг переноса adPrice для copy-постпроцесса. | step_prices |
| `copy_request.py` | 5 | Shared request validation for Direct copy UI/API routes. | parse_campaign_ids, parse_feed_map, parse_image_hashes, validate_api_campaign_ids, default_geo_mode, validate_ |
| `copy_settings_steps.py` | 22 | Сверка и исправление настроек кампаний в copy-постпроцессе. | step_fix_organic_placement, step_fix_search_campaign_invariants, step_settings_diff |
| `copy_snapshot.py` | 21 | Файловый слой поверх JSON-снапшота кабинета (0 DI): фильтр/переписывание/preflight. |  |
| `copy_step_utils.py` | 2 | Общие мелкие helper'ы для copy_steps.*. |  |
| `copy_steps.py` | 1 | Фасад шагов copy-постпроцесса. Реализация разнесена по copy_*_steps.py. |  |
| `copy_uac.py` | 30 | UAC (Мастер кампаний / Товарные) копирование: чтение и сборка. | configure |
| `copy_verify.py` | 7 | copy_verify facade. Implementation is split into source/target/diff/geo/repair modules. | run_copy_verification |
| `copy_verify_diff.py` | 25 | Profile diff logic for copy verification. | diff_profiles |
| `copy_verify_geo.py` | 10 | Geo/keyword consistency checks for copy verification. | check_geo_kw_consistency |
| `copy_verify_repair.py` | 26 | Repair facade and repair helpers for copy verification. | run_copy_repair |
| `copy_verify_source.py` | 15 | Source profile builder for copy verification. | build_source_profile |
| `copy_verify_state.py` | 1 | DI state for copy verification modules. | configure |
| `copy_verify_target.py` | 30 | Target profile builder for copy verification. | build_target_profile |
| `copy_verify_utils.py` | 8 | Shared helpers for copy_verify modules. |  |
| `create_content.py` | 72 | Ядро генерации контента ОДНОЙ РК (M3 fan-out 14B×3 + 72B-патч). | run_gen_campaign_content |
| `create_job_status.py` | 9 | Terminal status decisions for create queue jobs. | create_failed_error, terminal_status_for_job, terminal_status_for_parent_failed, has_verification_data, comput |
| `create_set_account.py` | 3 | Account/template/region preparation for Direct create_set. | prepare_create_set_account, validate_create_set_content |
| `create_set_apply_batches.py` | 28 | Campaign-level aspect batch functions for create_set finalization. | select_campaign_ids_by_tp, apply_callouts_batch, apply_promo_batch, apply_rename_batch, apply_sitelinks_batch, |
| `create_set_assets.py` | 54 | Create-set creative asset and responsive-ad helpers extracted from blueprint.py. | configure, v5_ensure_callout_pool |
| `create_set_audiences.py` | 16 | Аудитории структуры слепка (tp1/tp2/tp4/tp5) → групповые retargetings Grid. | is_search_channel, remember_account_conditions, account_conditions, forget_account_conditions, struct_audience |
| `create_set_callouts.py` | 1 | Callout result helpers for create_set. | build_callouts_note |
| `create_set_content_preflight.py` | 2 | Pre-create content pack guards for create_set jobs. | create_set_pack_gap_note |
| `create_set_context.py` | 55 | Create-set context/targeting helpers extracted from blueprint.py. | configure, dedup_name_segments, tp67_targeting_label_from_modes, tp67_clean_position_name_for_targeting, tp67_ |
| `create_set_corrections.py` | 11 | Create-set correction and bid modifier helpers extracted from blueprint.py. | configure, account_retargeting_probe |
| `create_set_deferred_status.py` | 1 | Deferred-row terminal status helpers for create_set resume flows. | parent_deferred_status_after_resume |
| `create_set_feed_builders.py` | 99 | Create-set tp3/tp5 and cookie builders extracted from blueprint.py. | configure |
| `create_set_feed_result.py` | 1 | Result helpers for feed-backed create-set paths. | shopping_cookie_success, ensure_shopping_cookie_error |
| `create_set_feeds.py` | 102 | Create-set feed/catalog/price helpers extracted from blueprint.py. | configure |
| `create_set_finalize.py` | 14 | Create-set Grid finalize helpers extracted from blueprint.py. | configure |
| `create_set_finalize_queue.py` | 10 | Асинхронная Grid-финализация набора (Задача F) — очередь + запись/replay спеков. | configure, async_finalize_enabled, FinalizeRecorder, register, get, unregister, capture_finalize, enqueue, +1 |
| `create_set_gallery.py` | 15 | tp5/tp3 «Товарная галерея» creation branches for Direct create_set. | run_create_set_gallery |
| `create_set_input.py` | 7 | Input normalization helpers for Direct create_set. | normalize_callouts, feed_key, feed_row_matches_key, feed_row_matches_single_feed, feed_row_matches_profile_fee |
| `create_set_master_product.py` | 90 | tp6/tp7 master/product item handler for create_set. | run_master_product_item |
| `create_set_metrika.py` | 2 | Metrika counter/goal preparation for Direct create_set. | prepare_metrika |
| `create_set_minus.py` | 52 | Create-set minus-keyword helpers extracted from blueprint.py. | configure, ensure_named_minus_sets, ensure_named_minus_sets_cached |
| `create_set_orchestrator.py` | 146 | Create-set orchestration for Direct automation. | create_set_response |
| `create_set_plan.py` | 120 | Create-set plan/name service extracted from blueprint.py. | configure |
| `create_set_postprocess.py` | 7 | Post-create verification and safe repair orchestration for create_set. | run_create_set_postprocess |
| `create_set_precreate.py` | 3 | Pre-create orchestration for Direct create_set. | run_create_set_precreate |
| `create_set_prefetch.py` | 31 | Prepare/warm-up helpers for queued create_set jobs. | configure, cleanup_job_cache, start_tp1_image_preupload, prepare_job, prefetch_job, start_prefetch |
| `create_set_promo.py` | 7 | Post-create promo attach/create orchestration for create_set. | attach_or_create_promo |
| `create_set_repairing.py` | 43 | Create-set live verification and repair helpers extracted from blueprint.py. | configure |
| `create_set_response.py` | 3 | Response payload helpers for create_set. | build_create_set_response |
| `create_set_resume.py` | 2 | Pure resume/skip helpers for Direct create_set. | already_in_direct, force_recreate, item_matches_result_name, items_for_result_names |
| `create_set_slepok_content.py` | 2 | Apply campaign content from direct_slepok_content to create_set items. | apply_slepok_campaign_content |
| `create_set_structure.py` | 28 | Единый контракт «Структура слепков → Создание РК 1:1» (задача 7). | camp_name_matches_group_segment, camp_name_matches_tp, filtered_camp_names_for_group, canonical_campaign_name, |
| `create_set_text.py` | 10 | tp2/tp4 текстовые кампании (Поиск / Поиск+Динамика) для Direct create_set. | run_create_set_text |
| `create_set_text_builders.py` | 47 | Create-set tp2/tp4 text campaign builders extracted from blueprint.py. | configure |
| `create_set_tp1.py` | 15 | tp1 РСЯ (ЕПК v501 network_cpa) creation branch for Direct create_set. | run_create_set_tp1 |
| `create_set_tp1_builders.py` | 202 | Create-set tp1/RSYA builders extracted from blueprint.py. | configure |
| `create_set_tp8_10.py` | 84 | Create-set engine for tp8/tp9/tp10 (Посевы) — Grid GdPostCampaign mutations. | configure, normalize_post_body_text, run_create_set_post |
| `create_set_units.py` | 2 | Direct API units-limit helpers for create_set. | is_units_exhausted, units_in_result, is_auth_error, auth_error_in_result, units_failed_names, count_created, c |
| `detect_name_segment_dupes.py` | 10 | Счётчик имён кампаний с ПОВТОРОМ сегмента по всей структуре слепков (сигнатура | run, main |
| `detect_tp67_name_socdem.py` | 5 | Детект рассинхрона «имя ⇄ socdem» на связке ПЛАН→БИЛД для tp6/tp7 (сигнатура Д7, 2026-07-19). | run, main |
| `detect_tp67_skip_struct.py` | 9 | Фикстурный детект прохода сверки по УЖЕ СУЩЕСТВУЮЩИМ tp6/tp7 (RESUME-SKIP) + предохранителя. | main |
| `direct_repository.py` | 5 | Victory PostgreSQL connection factory for Direct automation. | victory_conn, victory_conn_rw, victory_conn_rw_gate |
| `direct_v501_client.py` | 65 | Direct API v501 (Unified Performance Campaign / ЕПК). | UnifiedCampaignSpec, DirectV501Error, DirectV501Client, build_v501_client |
| `feed_models.py` | 4 | Одноразовый парсер справочника «марка → модели» из XML-фидов (YML) аккаунтов. | clean_model_name, parse_feed_bytes, build_from_domains |
| `gateway_client.py` | 8 | Тонкий клиент внутреннего Direct-брокера (`direct-gateway.service` :5025). | GatewayHTTPError, gw_cookie, gw_token, gw_tokens, gw_units_alive, gw_resolve_agency, gw_agency_override_get, g |
| `gateway_main.py` | 9 | Standalone Flask entrypoint for the internal Direct access broker (ФАЗА 1). | create_app |
| `geo_strip.py` | 26 | geo_strip.py — удаление гео-токенов (города/регионы РФ) из позитивных ключей слепков. | strip_geo_tokens, normalize_geo_lines |
| `grid_content_verifier.py` | 44 | Pure Grid content checks for non-UAC Direct campaigns. | verify_grid_content |
| `grid_create.py` | 88 | Куки-движок СОЗДАНИЯ кампаний (Grid GraphQL на куках агентства, БЕЗ баллов API). | GridCreateError, GridCreateClient, unique_keyword_ids, create_full, add_text_content_to_existing, add_shopping |
| `grid_create_payloads.py` | 18 | Чистые payload-фабрики Grid (спек → формат Grid GraphQL). | build_unified_campaign, build_adgroup, build_ad, build_shopping_ad |
| `grid_finalize.py` | 216 | Grid-докрутка ЕПК (tp1–tp5) — то, что официальный v5 API НЕ умеет, но нужно для | GridFinalizeError, get_grid_client, reset_grid_client_cache, GridClient, corrections_by_segment, apply_correct |
| `grid_read.py` | 45 | Read-only Grid helpers for Direct live verification. | GridReadError, GridReadClient |
| `job_repository.py` | 32 | PostgreSQL persistence for create/deferred/repair jobs. | configure |
| `kontent_pack.py` | 108 | Чтение контент-пака нейродиректолога с M3 (sshfs-монт /opt/neuro_kontent). | fs_call_bounded, read_bytes_bounded, isfile_bounded, realpath_bounded, listdir_bounded, isdir_bounded, refresh |
| `link_check.py` | 15 | HTTP-проверка URL объявления с фолбэком на родительский путь при 404. | strip_quiz_url, resolve_or_fallback_url, resolve_urls_batch |
| `live_verifier.py` | 19 | Read-only live verification for Direct create_set jobs. | verify_live_create_set |
| `llm_providers.py` | 64 | LLM-провайдеры нейродиректолога — вынесено из blueprint.py. | configure, record_llm_failure, record_content_fallback, llm_degrade_stats, log_llm_degrade_summary, check_cont |
| `local_result_verifier.py` | 5 | Pure checks for local create_set result/build metadata. | verify_local_result |
| `main.py` | 3 | Standalone Flask entrypoint for `/direct/*`. | create_app |
| `model_urls.py` | 10 | URL-хелперы объявлений (slug, глубокие ссылки на модели, домен-фида) — вынесено из blueprint.py. |  |
| `pack_resolver.py` | 22 | M3 content-pack and slepok resolution service, independent from Flask wiring hub. | configure |
| `precreate.py` | 13 | Pre-create planning for Direct create_set. | build_precreate_report, promo_content_lines, execute_precreate_assets |
| `price_check.py` | 90 | Сверка и заливка цен Яндекс.Директ ↔ фиды сайтов (веб-версия). | ensure_price_check_tables, mark_running, reconcile_stuck_jobs, job_public, jobs_recent, job_created_by, job_co |
| `price_check_apply_watch.py` | 4 | Watchdog ночной заливки цен ``price_check_cron apply`` (крон 20:00, LXC101). | main |
| `price_check_cron.py` | 5 | Крон-триггеры сверки/заливки цен Директ↔фиды (расписание в TZ Екатеринбурга). | run_check_all, run_apply_queue, main |
| `promo.py` | 6 | Промоакции Я.Директа через приватный grid/api (GraphQL) — публикация в кабинет клиента. | PromoClient |
| `promo_gen.py` | 13 | Генерация/валидация промоакций (ИИ-контент в стиле слепка) — вынесено из blueprint.py. | configure |
| `promotions.py` | 6 | Промоакции (скидки/акции) Я.Директа через приватный grid/api (GraphQL). | PromoClient |
| `queue_server.py` | 177 | Create-set queue lifecycle, workers, watchdog and deferred repair daemons. | configure |
| `repair_auto.py` | 35 | Orchestration helpers for post-create repair execution. | execute_next_in_place, execute_safe_post_create, execute_all_in_place, recreate_force_names, build_recreate_qu |
| `repair_common.py` | 3 | Shared imports, constants, RepairDeps and _unique_positive_ints for repair domain modules. | RepairDeps |
| `repair_content.py` | 15 | Content-domain repair executors: promo, callouts, rename, text/shopping rebuild. | execute_promo_repair, execute_callouts_repair, execute_rename_repair, execute_content_repair |
| `repair_executor.py` | 2 | Re-export facade — backward-compat hub for all importers of repair_executor. |  |
| `repair_gate.py` | 23 | Helpers for create_set repair-gate endpoints. | truthy, dict_from_jsonish, normalize_job_context, build_repair_gate_payload, executable_recreate_items, execut |
| `repair_keywords.py` | 27 | Keyword-domain repair executors: keyword repair and keywords_wrong_group (shift) fix. | execute_keywords_repair, execute_keywords_wrong_group_repair |
| `repair_media.py` | 17 | Media-domain repair executors: images, adprice, default_text, campaign invariants, images_forbidden. | execute_images_repair, execute_adprice_repair, execute_default_text_repair, execute_campaign_invariant_repair, |
| `repair_planner.py` | 24 | Repair planning for Direct post-create verification. | build_repair_plan |
| `routes_accounts.py` | 8 | Read-only account routes for Direct automation. | register_account_routes |
| `routes_ai.py` | 12 | AI and promo routes for Direct automation. | register_ai_routes |
| `routes_autorules.py` | 55 | Autorules page routes + Phase 2 API (overview, goals, balance, sensors). | register_autorules_routes |
| `routes_campaigns.py` | 1 | Campaign action routes for Direct automation. | register_campaign_routes |
| `routes_content.py` | 8 | Content-pack routes for Direct automation. | register_content_routes |
| `routes_content_editor.py` | 26 | Редактор контента Direct — массовый поиск и замена AI-текстов. | make_job_executor, register_content_editor_routes |
| `routes_copy.py` | 28 | Copy-flow routes for Direct automation. | register_copy_routes |
| `routes_create_set.py` | 5 | Create-set verification and repair routes. | register_create_set_routes |
| `routes_deferred.py` | 4 | Units and deferred-create routes for Direct automation. | register_deferred_routes |
| `routes_jobs.py` | 37 | Create-set queue and job status routes for Direct automation. | register_job_routes |
| `routes_overview.py` | 2 | Overview routes for Direct automation. | register_overview_routes |
| `routes_pack.py` | 6 | Slepok, M3 status, and pack preview routes. | register_pack_routes |
| `routes_pages.py` | 1 | Page routes for Direct automation. | register_page_routes |
| `routes_ready_logins.py` | 4 | Вкладка «Готовые логины» — реестр аккаунтов с загруженными кампаниями. | register_ready_logins_routes |
| `routes_reference.py` | 4 | Reference-data routes for the Direct automation UI. | register_reference_routes |
| `routes_set_plan.py` | 1 | Set-plan route wrapper for Direct automation. | register_set_plan_routes |
| `routes_settings.py` | 29 | Settings routes for feed allow-list and global minus places. | save_global_minus_places_payload, save_post_minus_places_payload, register_settings_routes |
| `routes_slepki_edit.py` | 32 | Роуты редактора структуры/ключей слепков (вкладка «Структура слепков» → редактируемая). | register_slepki_edit_routes |
| `routes_tags.py` | 12 | Роуты «редактируемые теги кампаний» (Вариант C — реестр в БД). | ensure_tags_tables, register_tags_routes |
| `seed_slepok_content.py` | 1 | Разовый засев БД-библиотеки контента слепков (public.direct_slepok_content). | main |
| `slepki_editor.py` | 76 | Редактор структуры слепков (вкладка «Структура слепков» → редактируемая). | configure, validate_scope, read_group_keywords, apply_edit_keywords, read_assets, apply_save_assets, read_minu |
| `slepki_main.py` | 8 | Standalone Flask entrypoint for the Direct «Структура слепков» editor only. | create_app |
| `slepki_store.py` | 9 | Хранилище структуры слепков ПО ФАЙЛАМ (разбиение монолита slepki_structure.json). | assemble, assemble_light_for_selected, invalidate, write_directologists |
| `slepki_worker_main.py` | 4 | Worker entrypoint для очереди правок СЛЕПКОВ (Фаза 2 разделения). | create_worker_app, main |
| `slepok_qa_report.md` | 1 | slepok-qa отчёт (2026-07-20 11:28) |  |
| `slepok_qa_run.py` | 22 | slepok-qa: детерминированный прогон создания РК ПО ВСЕМ СЛЕПКАМ + PASS/FAIL отчёт. | main |
| `stage_timing.py` | 6 | Пер-item пер-стадийный замер времени создания РК (профиль wall-clock). | note_progress, last_progress, current_item, set_item, clear_item, emit, stage |
| `text_gen.py` | 96 | Генерация текстов/заголовков объявлений (RSYA + Master) — вынесено из blueprint.py. | configure |
| `text_norm.py` | 26 | Анти-AI санитайзеры текста — вынесено из blueprint.py. | configure, mentions_banned_content, strip_banned_content |
| `uac_client.py` | 42 | UAC (Мастер кампаний / Товарная) клиент для Яндекс.Директа. | MasterCampaignSpec, collect_image_files, UacApiError, UacClient |
| `uac_read.py` | 9 | Read-only UAC helpers for tp6/tp7 live verification. | UacReadClient, summarize_uac_detail |
| `uac_verifier.py` | 17 | Pure UAC/tp6-tp7 invariant checks for post-create live verification. | images_create_min, configure, verify_uac_detail |
| `verification_service.py` | 8 | Orchestration layer for read-only create_set live verification. | verify_create_set_live |
| `verifier.py` | 21 | Post-create verification for Direct automation. | structure_preflight_issues, verify_create_set |
| `worker_main.py` | 4 | Worker entrypoint for the Direct create-set queue (Phase 2). | create_worker_app, main |
| `write_gate.py` | 13 | Cross-service Direct write gate. | gate_cb_should_skip, gate_cb_on_failure, gate_cb_on_success, drain_skip_count, agency_resource, ensure_table,  |
| `yandex_gateway.py` | 18 | Yandex Direct transports and agency credential resolution. | direct_tokens, v5_get, v5_units, bounded_post, v5_call, v501_call, v501_svc, v5_err, +11 |

## `_har/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `IMAGE_REPLACE_schema.md` | 16 | Замена картинки в объявлении: UAC vs РСЯ vs ЕПК (три разных API/пути) |  |
| `RESPONSIVE_AD_schema.md` | 2 | Комбинаторное объявление = RESPONSIVE_AD (замена ТГО/TextAd с 30.06) |  |

## `autorules/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `__init__.py` | 1 | Пакет direct.autorules — движок автоматических правил Яндекс.Директа. |  |
| `copy.py` | 14 | Direct Autorules — 1:1 campaign duplication within the same account. | list_campaigns, clone_campaigns_1to1 |
| `corrections.py` | 10 | Корректировки ставок по полу / возрасту / устройствам через v5 bidmodifiers API. | get_bid_modifiers, set_bid_modifiers |
| `k50_optimizer.py` | 22 | K50-style optimizer layer for Home autorules. | templates_list, template_by_key, event_from_template, preview_event |
| `placements.py` | 5 | Площадки РСЯ: Reports API для просмотра + логирование для исключения. | get_placements, log_excluded_sites |
| `queries.py` | 8 | Поисковые запросы: Reports API + добавление минус-фраз на кампанию. | get_search_queries, add_negative_phrases |
| `repository.py` | 21 | БД-слой схемы direct_autorules (PostgreSQL, БД seoadvanced). | configure, ensure_schema, rules_list, rules_get, rules_create, rules_update, rules_delete, home_accounts_list, |
| `rules_engine.py` | 9 | Движок правил: dry-run эвалюатор условий ЕСЛИ→ТО. | dry_run_rule |

## `autorules/sensors/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `__init__.py` | 1 | Сенсоры автоправил — сбор данных из Direct API v5 и БД (фаза 2). |  |
| `anomalies.py` | 5 | Сенсор: аномалии расхода и CPA — резкие отклонения day vs базовая линия. | run |
| `balance.py` | 4 | Сенсор: низкий баланс аккаунта. | run |
| `campaign_status.py` | 2 | Сенсор: остановленные / отклонённые / на модерации кампании. | run |
| `goals.py` | 5 | Сенсор: кампании без цели Метрики или без счётчика. | run |
| `minus.py` | 2 | Сенсор: кампании без минус-фраз / без минусовки на уровне кампании. | run |
| `url_check.py` | 4 | Сенсор: битые ссылки 404/5xx в объявлениях. | run |

## `deploy/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `mlx_watchdog.py` | 8 | mlx_watchdog.py — health-watchdog для mlx_lm.server на M3, живёт на LXC101. | main |

## `deploy/dropins/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `README.md` | 5 | systemd drop-in overrides (источник правды конфига сплита) |  |

## `dev/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAIMS.md` | 1 | CLAIMS.md — реестр «застолблённых» файлов (анти-коллизии двух окон) |  |

## `docs/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `UI_MAP.md` | 12 | UI-карта: seoadvanced.ru/direct/automation |  |

## `reconciler_staging/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CAMPAIGN_CREATION_SCRIPT_ERRORS_2026-07-14.md` | 6 | Проверка скриптов создания кампаний, 2026-07-14 |  |
| `CONSOLIDATED_live_vs_structure_20260713.md` | 10 | КОНСОЛИДИРОВАННАЯ КАРТА: живой репертуар tp vs структура |  |
| `ERRORS_CHECKLIST_20260713.md` | 4 | ЧЕК-ЛИСТ ОШИБОК СЛЕПКОВ (консолидированный, 2026-07-13) |  |
| `RECONCILE_PLAN_20260713.md` | 6 | ДИФФ-ПЛАН: живые факты ↔ структура (tp-set + флаг rebuild) |  |
| `SLEPKI_MISMATCHES_2026-07-14.md` | 8 | Расхождения по слепкам |  |
| `SLEPKI_RECHECK_WITH_EXAMPLES_2026-07-15.md` | 8 | Повторная проверка слепков с примерами |  |
| `SLEPKI_REMAINING_NOT_COLLECTED_2026-07-15.md` | 12 | Что еще не собрано по слепкам |  |
| `TP67_TARGETING_AUDIENCES_CHECK_2026-07-14.md` | 3 | TP6/TP7 targeting audiences check — 2026-07-14 |  |
| `__init__.py` | 1 |  |  |
| `chepelev.tp67.gaps.md` | 5 | chepelev (Чепелев Никита) — tp6/tp7 reconciler gaps |  |
| `collect_facts.py` | 12 | collect_facts.py — ФАКТИЧЕСКАЯ схема кампаний tp1-5 из живых кабинетов. | select_logins, resolve_token, fetch_campaigns, fetch_adgroups, keywords_count, parse_campaign, entity_of, grou |
| `content_gaps_scherbakova_multibrand_images.md` | 1 | Контент-гэп: нет креативов (картинок) — scherbakova / Мультибренд |  |
| `corpus_audit_tp1-5_20260713.md` | 2 | Корпусный аудит структура слепка ↔ реальные кабинеты (tp1-tp5, все статусы) |  |
| `facts_schema_pull.py` | 16 | facts_schema_pull.py — ЖИВАЯ СХЕМА tp1..tp5 из кабинетов (READ-ONLY). | core_name, norm_key, classify_entity, select_logins, resolve_token, v5_page, v5_by_campaigns, pull_account, +4 |
| `gordeeva.tp67.gaps.md` | 2 | gordeeva (Гордеева Наталья) — tp6/tp7 reconciler gaps |  |
| `grid_n055_query.py` | 10 | Grid GraphQL query for n055 (Автокредит interest) campaigns in scherbakova accounts. | pick_cookie, GridClient, main |
| `grid_n055_scan.py` | 6 | Scan scherbakova accounts for n055 (Автокредит interest) tp7 campaigns. | list_campaigns, uac_get |
| `karavaev.tp67.gaps.md` | 3 | karavaev (Караваев Михаил) — tp6/tp7 reconciler gaps |  |
| `kryuchkova.tp67.gaps.md` | 3 | kryuchkova (Крючкова Елизавета) — tp6/tp7 reconciler gaps |  |
| `kuderko.tp67.gaps.md` | 10 | kuderko (Кудерко Семен) — tp6/tp7 reconciler gaps |  |
| `live_tp_map.py` | 10 | live_tp_map.py — ЖИВАЯ карта присутствия tp1..tp7 по директологам (READ-ONLY). | select_accounts, login_site_types, read_campaigns, classify_tp15, chepelev_tp67, scherbakova_tp67, run_slepok, |
| `live_tp_scan.py` | 10 | live_tp_scan.py — ЖИВАЯ карта присутствия tp1..tp5 по директологу. | select_logins, resolve_token, scan_login, load_tp67, run |
| `pavlov.tp67.gaps.md` | 7 | pavlov (Павлов Алексей) — tp6/tp7 reconciler gaps |  |
| `piterkina.tp67.gaps.md` | 9 | piterkina (Питеркина Дарья) — tp6/tp7 reconciler gaps |  |
| `renormalize_targeting_only.py` | 14 | renormalize_targeting_only.py — пересборка staging по НОВОМУ правилу дедупа. | run |
| `run_reconciler.py` | 22 | run_reconciler.py — ОБОБЩЁННЫЙ reconciler tp6/tp7 для ЛЮБОГО директолога. | resolve_directologist, discover_accounts, run, main |
| `run_scherbakova.py` | 31 | run_scherbakova.py — пилот reconciler для scherbakova. | run |
| `salamahin.tp67.gaps.md` | 3 | salamahin (Саламахин Иван) — tp6/tp7 reconciler gaps |  |
| `scherbakova.content_gaps.md` | 4 | scherbakova — Content Gaps & Reconciler Report |  |
| `scherbakova.content_gaps.v2.md` | 6 | Reconciler Report: scherbakova tp7 — v2 (targeting-only identity) |  |
| `structure_vs_reality_map_20260713.md` | 25 | Единая карта «Структура ↔ Реальность» по всем слепкам |  |
| `terehov.tp67.gaps.md` | 4 | terehov (Терехов Евгений) — tp6/tp7 reconciler gaps |  |
| `tumashenko.tp67.gaps.md` | 4 | tumashenko (Тумашенко Евгений) — tp6/tp7 reconciler gaps |  |
| `uac_reconciler.py` | 36 | uac_reconciler.py — READ-ONLY reconciler: live UAC/Grid payload → нормализованные staging-позиции. | reconcile_account, normalize_to_positions, build_content_gaps, main |
| `verify_tp15_pull.py` | 11 | verify_tp15_pull.py — РЕАЛЬНАЯ проба таргетинга tp1..tp5 из кабинетов (READ-ONLY). | pull_account, run |
| `zubakin.tp67.gaps.md` | 2 | zubakin (Зубакин Алексей) — tp6/tp7 reconciler gaps |  |

## `scratchpad/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `_delete_feed_campaigns.py` | 10 | Удаление 26 кампаний на «чужих» фидах (не yandex-used-auto.xml) в трёх аккаунтах. | delete_epk, delete_uac, verify_remaining, run |
| `_load_minus_sets.py` | 1 | Одноразовая загрузка КАНОНИЧЕСКИХ именованных наборов минус-слов в пак (dual-write M3+DST). |  |
| `_minus_build_canon.py` | 2 | Пересобрать канон-раскладку именованных наборов минус-слов с ФИКСОМ присутствия | canon_of |
| `batch_run_dod_report.md` | 8 | Финальный DoD-прогон + live-тест редактора структуры — porg-vfdnaolu (2026-07-11) |  |
| `harvest_salamahin_gap_exact_2026_07_24.py` | 11 | Read-only exact harvester for Salamahin gaps from job 29ecf9e26bf5. | main |
| `harvest_scherbakova_gap_v5_2026_07_24.py` | 11 | Read-only v5 harvest for Scherbakova M3 gap. | main |
| `harvest_scherbakova_gap_v6_exact_2026_07_24.py` | 11 | Read-only exact harvester for Scherbakova deferred content gaps. | main |
| `investigation_feed_campaigns_audit.md` | 1 | Аудит фидов по 3 аккаунтам (read-only) — чекпоинт |  |
| `proposed_scherbakova_dfm_fix_2026-07-24.md` | 5 | Proposal: исправить DFM в `scherbakova.json` |  |
| `slepki_data_audit_2026-07-12.md` | 77 | Детальный read-only аудит слепков |  |
| `slepki_pack_profile_audit_2026-07-12.md` | 12 | Read-only аудит profile / structure / локального M3-пака |  |

## `scratchpad/m3_stage/scherbakova_dfm_topup_2026-07-24/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `REPORT.md` | 5 | Scherbakova DFM M3 top-up staging 2026-07-24 |  |

## `scratchpad/m3_stage/scherbakova_live_harvest_topup_2026-07-24/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `REPORT.md` | 13 | Scherbakova live-harvest M3 top-up staging 2026-07-24 |  |

## `scratchpad/slepki_harvest_2026-07-14/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `_collapse_recompute.py` | 57 | _collapse_recompute.py — идемпотентный дедуп+нормализация слепков (v1, 2026-07-15) | apply_rule_A_tp67, apply_rule_B_tp15, apply_rule_C_deregion, apply_rule_1_trash, apply_rule_2_domain_tk, apply |
| `_grid_interests_probe.py` | 14 | _grid_interests_probe.py — Зондирование Grid для interest-кампаний. | build_session, get_session, grid_query, find_working_agency, query_campaign_retargeting, extract_interest_goal |
| `_grid_retcond_probe.py` | 10 | _grid_retcond_probe.py — Поиск retargeting conditions через Grid для interest-кампаний. | build_session, gq, probe_campaign, main |
| `_uac_cookie_rotation.py` | 17 | _uac_cookie_rotation.py — перебор агентских кук для UAC /campaign/{id} GET. | fetch_cookie, uac_get_campaign, bootstrap_csrf_via_linkinfo, collect_target_campaigns, extract_audiences, run_ |

## `scratchpad/slepki_harvest_2026-07-14/terehov/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `harvest_terehov_monobrand_tp4.py` | 15 | Сбор ключевых слов для terehov / Монобренд / tp4 по 6 моделям Lada. | gather_keywords, write_pack_file, main |

## `scripts/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `build_slepok_structure.py` | 28 | build_slepok_structure.py — Генератор уникальных секций слепков из корпуса директологов. | scan_corpus, item_ct4, filter_items, rebuild_directologist_section, tp_item_fingerprint, count_collisions, bui |
| `extract_gen_ses_archive.py` | 10 | Build a deterministic gen_ses source manifest from Yandex Direct XLSX exports. | read_campaign, build_manifest, main |
| `slepki_preflight.py` | 18 | Preflight-проверка slepki_structure.json перед деплоем. | preflight_dict, check |

## `tests/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `test_architecture_boundaries.py` | 2 | Characterization tests for the Direct modular-monolith boundaries. | test_service_entrypoints_do_not_import_web_composition_root, test_runtime_has_no_route_registration_or_bluepri |
| `test_blueprint_images.py` | 1 |  | test_non_common_ct_fills_manual_with_selected_slepok, test_non_common_ct_uses_explicit_after_selected_slepok |
| `test_campaign_cookie_picker.py` | 3 |  | test_pick_cookie_preserves_managing_agency_rights_error, test_pick_cookie_single_explicit_account_preserves_ri |
| `test_content_ad_href_grid.py` | 6 |  | test_ad_href_textad_uses_grid_rmw_not_v5_update, test_ad_href_responsive_uses_grid_rmw_not_v501_update, test_a |
| `test_content_editor_extra_tabs_access.py` | 6 |  | test_copy_accounts_is_not_scoped_by_directologist, test_copy_accounts_requires_extra_tab_access, test_rename_r |
| `test_content_images_transport_split.py` | 21 | Разделение транспортов вкладки «Смена изображения» (tp6/tp7). | test_owned_only_from_uac_contents, test_tp6_by_name_outside_uac_is_not_owned, test_adaptive_in_uac_owned_is_bl |
| `test_content_worker_blocked_skip.py` | 1 |  | test_finish_blocked_account_skip_is_done_without_agent_board |
| `test_copy_integration_guards.py` | 51 |  | test_copy_target_feed_id_prefers_preseeded_id_maps, test_copy_terminal_status_is_error_when_campaign_failed, t |
| `test_create_auto_regressions.py` | 65 |  | test_link_check_falls_back_from_single_segment_404_to_root, test_tp7_product_filters_are_positive_only, test_t |
| `test_create_review_findings.py` | 9 |  | test_cookie_feed_success_requires_shopping_ad, test_cookie_feed_success_accepts_campaign_group_and_shopping_ad |
| `test_direct_copy_transient_retry.py` | 1 |  | test_direct_call_retries_api_1000 |
| `test_direct_repository.py` | 2 |  | test_victory_conn_retries_on_database_error |
| `test_grid_add_idempotency.py` | 33 | Ретрай Grid-мутаций не должен плодить дубли (RETRY_ON_NETWORK_LOSS_DUPLICATES_ADD). | test_transient_after_commit_does_not_recreate_groups, test_lost_response_before_commit_still_creates_groups, t |
| `test_image_pool_unique_topup.py` | 6 | Добор картинок до 5 берёт только УНИКАЛЬНЫЕ креативы (правило Семёна 2026-07-28). | test_duplicate_from_other_slepok_not_used_as_fifth, test_duplicate_does_not_stop_topup, test_six_unique_gives_ |
| `test_job_status_gate.py` | 16 | Tests for job-status gate: has_issues breakdown and terminal_status_for_job. | test_clean_done_no_breakdown, test_live_errors_trigger_has_issues, test_only_warnings_no_has_issues, test_gate |
| `test_kw_clean_minus_limit.py` | 8 | Лимит «7 слов» в ключевой фразе считается по ПОЗИТИВНЫМ словам, минус-части не в счёт. | test_positive_word_count_ignores_minus_parts, test_live_phrase_with_13_minus_words_survives_and_keeps_minus_pa |
| `test_m3_serial_when_only_target.py` | 3 | Веер потоков на ЕДИНСТВЕННЫЙ M3-эндпоинт запрещён — он там только вредит. | spy, test_m3_primary_single_url_is_serial, test_openrouter_alive_keeps_fanout, test_openrouter_dead_falls_back |
| `test_metrika_plan_alert.py` | 7 | Метрика проверяется на шаге ПЛАНА (_metrika_alert_for), а не только при создании. | test_counter_and_goal_present_no_alert, test_missing_goal_not_optional_raises_alert, test_via_cookie_no_cpa_is |
| `test_minus_audiences_round2.py` | 24 | Ревью-раунд 2 по коммитам dda2ec09 (минус-наборы) и 2a087591 (аудитории). | test_channel_from_campaign_spec_matches_cookie_path, test_channel_from_v501_mode, test_channel_fallback_by_tp_ |
| `test_minus_losses_round3.py` | 15 | Ревью-раунд 3 по коммиту f33aaef4 — две Important и три Minor. | test_losses_alone_make_job_not_green, test_annotate_writes_has_issues_from_losses_only, test_no_losses_and_cle |
| `test_minus_sets_library.py` | 16 | Именованные наборы минус-фраз слепка → библиотека минус-фраз аккаунта (v5). | test_reader_reads_named_sets_and_resolves_slepok_alias, test_reader_accepts_bare_list_and_missing_file, test_r |
| `test_perf_circuit_breakers.py` | 11 | Tests for circuit breaker patches (2026-07-27): | test_link_check_cb_opens_after_threshold, test_link_check_cb_fail_open_preserves_original_href, test_link_chec |
| `test_plan_label_collapse.py` | 3 | Фильтр `selected_pos` обязан понимать схлопнутые метки UI-дерева набора. | test_no_selection_matches_everything, test_collapsed_ui_label_matches_full_camp_name, test_full_ui_label_still |
| `test_posevy_plan_selection.py` | 5 | План Посевов (tp8/tp9/tp10) обязан слушать выбор пользователя в дереве набора. | plan_env, test_posevy_positions_mirror_structure_groups, test_only_tp10_selected_builds_four_campaigns, test_s |
| `test_post_body_normalize.py` | 20 | Тесты нормализации тела поста посевов (правила 1-11). | TestRule1Emoji, TestRule2SqueezeSpaces, TestRule3CurrencyPercent, TestRule4PaymentPeriod, TestRule5Bullet, Tes |
| `test_post_brand_offer_block.py` | 17 | Тесты детерминированного блока марочных оферов для посевов (tp8/tp9/tp10). | TestFmtPayment, TestBuildBrandOfferBlock, TestReplacePostModelList, TestBrandBlockIdempotency |
| `test_retargeting_probe_status.py` | 3 | Пустая карта условий ретаргетинга — два РАЗНЫХ случая, и их нельзя путать. | v5, test_no_token_is_reported_as_no_token, test_empty_cabinet_is_ok_not_failure, test_missing_result_key_is_ok |
| `test_review_fixes_20260728.py` | 5 | Две находки код-ревью 2026-07-28. | test_gate_reports_expected_and_shortfall, test_gate_silent_when_counts_match, test_verifier_now_sees_group_sho |
| `test_routes.py` | 80 |  | test_create_set_route_points_to_api_create_set, test_create_set_authenticated_smoke_reaches_api_create_set, te |
| `test_second_token_thread.py` | 15 | Tests for second token thread (channel A sub-threads) and atomic 152-flip. | TestASharedCookieState, TestPartitionAIndices, TestBuildCreateSetResponseTimingFields, TestTokenThreadsEnvVar, |
| `test_sitelinks_reuse_batch.py` | 13 | Реюз и батчинг наборов быстрых ссылок (sitelinks.add). | TestClientBatch, fake_v5, TestReuseByContent, TestBatchHelper |
| `test_slepki_editor.py` | 3 |  | test_tp2_keyword_preview_falls_back_to_tp1_group, test_tp67_keyword_preview_uses_group_aware_creation_reader,  |
| `test_slepki_source_manifest.py` | 4 |  | test_gen_ses_manifest_is_complete_and_matches_archive_shape, test_gen_ses_structure_references_manifest_instea |
| `test_stage_timing.py` | 10 | Tests for STAGE_TIMING per-item per-stage timing helper (direct/stage_timing.py). | TestStageDuration, TestExceptionPropagation, TestItemContext, TestOnlyInItemGate, TestNestedGridPostNotDoubleC |
| `test_struct_audiences_write_path.py` | 8 | Аудитории структуры (tp1/tp2/tp4/tp5) доезжают до payload группы Grid. | test_no_audiences_keeps_both_fields_empty, test_network_audiences_go_to_retargetings, test_search_audiences_go |
| `test_struct_ct0000_node.py` | 5 | Структурный узел ЦЕЛИКОМ на ct0000 не должен раздуваться до «весь пак». | test_ct0000_node_yields_exactly_its_own_group, test_ct0000_units_ignore_model_ct_items, test_ct0000_units_empt |
| `test_text_path_signature_contract.py` | 4 | Контракт сигнатур token/cookie-путей tp2/tp4. | test_caller_kwargs_are_discoverable, test_token_and_cookie_paths_accept_exactly_caller_kwargs, test_text_paths |
| `test_three_new_detectors.py` | 20 | Tests for three new defect detectors + WRONG_AUTOTARGET severity elevation. | TestFilterSummaryCatalogFilterHasValues, TestTP7CatalogFilterEmpty, TestAdHrefRootInsteadOfModel, TestCpaCount |
| `test_tone_voice_generation_contract.py` | 17 |  | test_assemble_campaign_live_mode_does_not_copy_agent_corpus, test_live_generation_blocks_template_fallback_whe |
| `test_tp5_feed_fanout_names.py` | 3 | Веер tp5 по фидам обязан давать РАЗЛИЧИМЫЕ имена кампаний. | test_single_feed_structural_keeps_slepok_name, test_many_feeds_structural_gets_feed_label, test_every_structur |
| `test_tp67_name_from_slepok.py` | 4 | Имя кампании tp6/tp7 берётся из слепка, а не пересобирается движком. | test_struct_name_is_used_verbatim, test_computed_tail_does_not_override_slepok_tail, test_coder_prefix_and_reg |
| `test_tp8_10_bid_mod_dem.py` | 18 | Unit-тест: _bid_mod_dem строится с campaignId после AddCampaigns. | TestBidModDemStructure, test_run_create_set_post_fails_when_post_ads_underfilled |
| `test_uac_images_soft_threshold.py` | 8 | Мягкий порог картинок tp6/tp7 (решение Семёна 2026-07-27). | test_case_a_pool_short_is_warning_not_error_and_never_recreates, test_case_b_full_pool_but_fewer_images_in_cam |
| `test_uac_number_clause_coherence.py` | 5 | Числовая добивка UAC-контента не должна давать бессмысленных склеек. | test_clause_is_whole_segment_not_bare_fragment, test_cta_never_gets_incoherent_numeric_tail, test_cta_keeps_nu |
| `test_v501_batching.py` | 28 | Тесты батчинга Direct API v501 — Этапы 1 и 2. | TestAddFeedAdsBatch, TestAddProductAdgroupsBatch, TestSetupSearchDynamicCampaign, TestSetupCombinedCampaign, T |
| `test_video_source_order.py` | 10 | Порядок источников ВИДЕО (правило Семёна 2026-07-28). | test_exact_model_in_foreign_slepok_beats_pool_brand_fallback, test_pool_brand_fallback_still_works_when_model_ |
| `test_watchdog_first_campaign_liveness.py` | 4 | Сторож «нет первой кампании» обязан смотреть на признаки работы, а не только на счётчик. | watchdog_env, test_stage_progress_keeps_job_alive, test_item_heartbeat_keeps_job_alive, test_silent_job_withou |

## `tools/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `__init__.py` | 1 |  |  |
| `tone_baseline.py` | 14 | tone_baseline.py — офлайн-базовая-линия тон-войс для всех директолог-слепков. | generate_via_openrouter, corpus_content, run_baseline, print_table, main |
| `tone_of_voice_watcher.py` | 8 | tone_of_voice_watcher.py — отвязанный watcher тон-войс проверки. | poll_once, main |


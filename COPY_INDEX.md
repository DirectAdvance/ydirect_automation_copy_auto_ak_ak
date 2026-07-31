# COPY_INDEX — карта сервиса копирования кампаний (для ИИ)

> Навигационный индекс. Механика — в `COPY_README.md`. Грабли/инциденты — в `STATE_COPY_OTHER.md`.
> Смежные доки: `docs/ARCHITECTURE.md` (слои всего пакета), `README.md` (create-flow), `ERRORS_JOURNAL.md`.

> 📦 **Свежая версия сервиса всегда лежит в GitHub:**
> **https://github.com/DirectAdvance/ydirect_automation_copy_auto_ak_ak** (ветка `main`).
> Правим локально в `home/seoadvanced`, репо — зеркало-экспорт. **Любая локальная правка сразу
> отправляется в git:** `python3 tools/copy_service_git.py export` (commit + push зоны copy).

---

## Точки входа

| Что | Где |
|-----|-----|
| Страница UI | `routes_copy.py` → `GET /direct/automation/copy` (рендерит `templates/direct/copy.html`) |
| Запуск копирования «Авто» | `routes_copy.py` → `POST /direct/api/copy_start` (только `mode=auto`) |
| Запуск копирования «Прочие сферы» | `routes_copy.py` → `POST /direct/api/copy_other_start` (mode задаёт роут) |
| Статус job'а | `routes_copy.py` → `GET /direct/api/copy_status/<job_id>` |
| Кампании источника | `routes_copy.py` → `GET /direct/api/copy_campaigns` |
| Префилл цели | `routes_copy.py` → `GET /direct/api/copy_target_prefill` |
| Фиды для замены | `routes_copy.py` → `POST /direct/api/copy_feeds_preview` |
| Сухой прогон фидов | `routes_copy.py` → `POST /direct/api/copy_feeds_check` |
| Загрузить фид в цель | `routes_copy.py` → `POST /direct/api/copy_feed_upload` |
| Загрузить картинки | `routes_copy.py` → `POST /direct/api/copy_images_upload` |
| Снимок кампаний цели | `routes_copy.py` → `GET /direct/api/copy_target_campaigns` |
| Дерево регионов | `routes_copy.py` → `GET /direct/api/copy_geo_regions` |
| Внешний API (A2) | `copy_api.py` → `POST /api/v1/copy/start`, `GET /api/v1/copy/status/<job_id>`, `GET /api/v1/copy/campaigns`, `GET /api/v1/copy/health`; строгие `campaign_ids`, `Idempotency-Key`, DB-fallback статуса |
| Сервис/порт | `direct-copy.service`, порт `127.0.0.1:5022`, entrypoint `copy_main.py` |
| Очередь | `direct_automation_jobs` (Victory, `kind='copy_campaigns'`) + in-memory `_COPY_JOBS` |
| systemd unit | `direct-copy.service` (нет отдельного worker — копировщик стартует свой поток через `_ensure_copy_worker`) |

> ⚠️ Nginx правило: `^~ /direct/api/copy_` → `:5022`. Любой новый роут copy-сервиса ОБЯЗАН начинаться с `/api/copy_`. Локальный curl на `:5022` этот баг маскирует (уже потеряли `geo_regions`, `2026-07-17`).

---

## Файлы цепочки

| Файл | Зона ответственности |
|------|---------------------|
| `copy_main.py` | entrypoint Flask-приложения `direct-copy.service`, DI-wiring |
| `routes_copy.py` | HTTP-роуты `/direct/api/copy_*` и `/direct/automation/copy`; функция `register_copy_routes` |
| `copy_api.py` | внешний API `POST /api/v1/copy/start` + `status/campaigns/health` + CORS + API-ключ; безопасный `result_summary`, `public_status/terminal`, `Idempotency-Key`; функция `register_copy_api` |
| `copy_engine.py` | главный оркестратор job'а: `_copy_run_job`, `_copy_cookie_postprocess`, `_copy_delayed_reverify`; DI-хаб через `configure(deps)` |
| `copy_snapshot.py` | preflight (`_copy_snapshot_preflight`), фильтрация снэпшота (`_copy_filter_snapshot`), ремап контекста, сидирование feed-maps |
| `copy_steps.py` | per-шаговые мутаторы: `step_keywords`, `step_prices`, `step_adaptive_creatives`, `step_videos`, `step_attach_callouts`, `step_attach_sitelinks`, `step_attach_promos`, `step_age_bidmods`, `step_disabled_places`, `step_settings_diff`, `step_fix_organic_placement`, `step_fix_search_campaign_invariants` (tp2-форсинг), `pull_source_campaign_assets`; `CopyCtx` датакласс |
| `copy_verify.py` | профайлинг и diff: `build_source_profile`, `build_target_profile`, `diff_profiles`, `run_copy_verification`, `run_copy_repair`, `check_geo_kw_consistency` |
| `copy_geo_morph.py` | LLM-морф гео-замены при копировании |
| `copy_jobs.py` | `_copy_job_upsert`, `_COPY_JOBS`, `_COPY_JOBS_LOCK` — in-memory + Victory БД |
| `copy_cleanup.py` | `_copy_target_cleanup`, `_copy_cleanup_uac_drafts` (удаление черновиков перед копированием, вкл. UAC по куки) |
| `copy_uac.py` | `_copy_uac_campaigns`, `_copy_uac_value` — копирование UAC/МК/tp6 кампаний через Grid |
| `copy_images.py` | `_copy_image_remapper`, `_copy_make_video_resolver` — ремап картинок, загрузка видео |
| `copy_feeds.py` | `_copy_auto_feed_map`, `_feed_auto_match_one` — автоподбор фидов |
| `copy_feed_upload.py` | `upload_feed_to_target` — v5 feeds.add + адаптация URL под целевой домен |
| `copy_grid_read.py` | `_copy_selected_grid_campaigns`, `_copy_is_uac_grid_row` — чтение Grid-кампаний источника |
| `copy_grid_steps.py` | Grid-шаги постпроцессинга (доп. Grid-мутации в `_copy_cookie_postprocess`) |
| `copy_metrika.py` | `_copy_apply_metrika` — перенос счётчика/цели/`PriorityGoals` на цель |
| `copy_other.py` | `_copy_auto_feed_map` и специфика «Прочих сфер» |
| `copy_geo.py` | `_copy_geo_replacements`, `_copy_ctx` — гео-пары для замены |
| `work/slepki_direktologov/scripts/direct_copy.py` | ДРУГОЙ РЕПОЗИТОРИЙ (`home/`) — загружается ЛЕНИВО через `importlib` в `copy_engine._direct_copy_module()`; содержит `phase_pull` + `phase_upload` (v5-ветка создания); фиксы (`PriorityGoals`, белый список Settings) — там |

---

## Ключевые функции

| Функция | Файл | Что делает |
|---------|------|-----------|
| `_copy_run_job` | `copy_engine.py:1337` | точка входа джоба: cleanup → pull → upload → postprocess → delayed reverify |
| `_copy_cookie_postprocess` | `copy_engine.py:701` | post-upload Grid-добивка: callouts, промо, ключи, adaptive, видео, сверка |
| `_copy_delayed_reverify` | `copy_engine.py:1235` | отложенная адаптивная пере-сверка после оседания привязок (поллинг sitelinks) |
| `_copy_target_sitelinks_ready` | `copy_engine.py:1207` | проба-индикатор: появились ли sitelinks на объявлениях цели |
| `phase_pull` | `work/.../direct_copy.py` | читает источник через v5 → `source/` снэпшот |
| `phase_upload` | `work/.../direct_copy.py` | создаёт кампании/группы/объявления в цели через v5 |
| `_copy_snapshot_preflight` | `copy_snapshot.py:223` | стоп-проверки перед копированием (домен, фид, гео) |
| `_copy_filter_snapshot` | `copy_snapshot.py:23` | оставляет из снэпшота только выбранные campaign_ids |
| `build_source_profile` | `copy_verify.py:119` | читает источник → нормализованный профиль для diff |
| `build_target_profile` | `copy_verify.py:371` | читает цель (Grid counts/edit_rows/invariants) → профиль |
| `diff_profiles` | `copy_verify.py:724` | структурный diff D1–D19 + 2 гео → список `{scope,dimension,status,...}` |
| `run_copy_verification` | `copy_verify.py:1215` | оркестратор: build → diff → summary → return dict |
| `run_copy_repair` | `copy_verify.py:1842` | авто-ремонт по результатам diff (ключи, shared_sets, shopping_filters) |
| `check_geo_kw_consistency` | `copy_verify.py:1031` | сверка: нет ли в ключах цели иностранного города |
| `step_keywords` | `copy_steps.py:825` | Grid-first (batch 1000) + v5-fallback (batch 200) заливка ключей |
| `step_adaptive_creatives` | `copy_steps.py:1049` | RMW-апдейт adaptive text ads: titles/bodies/image_hashes (до 5) |
| `step_settings_diff` | `copy_steps.py:1407` | diff Grid edit_rows source↔target → авто-починка v5-полей (report + fix) |
| `step_videos` | `copy_steps.py:1177` | скачать mp4 из Grid source → аплоуд → RMW-привязка на цели |
| `step_attach_sitelinks` | `copy_steps.py:387` | перенос быстрых ссылок с гео-морфом; вызывается **синхронно ДО verify** (copy_engine.py:825) |
| `step_attach_promos` | `copy_steps.py:497` | привязка промоакций к кампаниям цели |
| `step_prices` | `copy_price_steps.py` | цены из фида ЦЕЛИ (не источника): `Марки`/`Модели` строго по своему ключу, `Общее`/прочие СТ — минимум фида |
| `step_fix_search_campaign_invariants` | `copy_steps.py:659` | форсирует на tp2 (TEXT_CAMPAIGN): `enableCompanyInfo=False` + автотаргет EXACT_V2_MARK/WITHOUT_BRAND; вызывается ДО live_verification (copy_engine.py:1090); `keywords=grp["keywords"]` — реальные ключи (пустой список стирает ключи!) |
| `pull_source_campaign_assets` | `copy_steps.py` | читает campaign-level callout/sitelink id с УРОВНЯ КАМПАНИИ (inheritableCallouts/inheritableSitelinkSet); вызывается ДО `_copy_filter_snapshot` (copy_engine.py:1479) |
| `_copy_apply_metrika` | `copy_metrika.py` | перенос счётчика + PriorityGoals (`Operation="SET"`) через v5 |
| `_copy_uac_campaigns` | `copy_uac.py` | копирование tp6 МК через Grid/UAC (round-robin картинки) |
| `_copy_cleanup_uac_drafts` | `copy_cleanup.py` | удаление черновиков tp6 по куки (v5 их не видит) |

---

## Таблица измерений verify (D1–D19+)

| ID | Название | Статус | Источник (source) | Цель (target) |
|----|----------|--------|-------------------|---------------|
| D1 | adgroup_count | СРАВНИВАЕТСЯ | `groups.json` снэпшота | Grid `campaign_content_counts.adGroupsCount` |
| D2 | keyword_count | СРАВНИВАЕТСЯ | `keywords.json` (строки без `---`) | Grid `campaign_content_counts.keywordsCount` |
| D2b | campaign_neg_count | UNREADABLE | `campaigns.json` NegativeKeywords | не читается отдельно — только report |
| D3 | shared_set_count | СРАВНИВАЕТСЯ | `negative_sets.json` count | Grid `read_campaign_invariants.libraryMinusKeywordsIds` count |
| D4 | promo_attached | СРАВНИВАЕТСЯ | `promotions.json` has_promo | Grid `edit_rows.promoExtension` |
| D5 | adaptive_titles_count | СРАВНИВАЕТСЯ | `ads.json` adaptive ads count | Grid `campaign_content_counts.responsiveSearchAdsCount` (=adaptive_total) |
| D6 | adaptive_bodies_count | СРАВНИВАЕТСЯ | `ads.json` bodies count (`ads_with_texts`, grid_snapshot `gs.get("bodies")`) | Grid (прокси: `adaptive_total` из `campaign_content_counts`; отдельного счётчика bodies нет) |
| D7 | callout_count | СРАВНИВАЕТСЯ | `callouts.json` count | Grid `edit_rows.calloutExtensions` count |
| D8 | sitelinks_present | СРАВНИВАЕТСЯ | `sitelinks.json` has_any | Grid `edit_rows.sitelinkExtensions` has_any |
| D9 | ads_with_images | СРАВНИВАЕТСЯ | `ads.json` ImageHash not null | Grid `_enrich_adaptive_images` / v5 `ads_with_images_v5` |
| D10 | audiences | СРАВНИВАЕТСЯ* | Grid `GdRetargeting.retargetingCondition` по source campaigns | Grid `GdRetargeting.retargetingCondition` по target campaigns; `GdGridOfferRetargeting` фида игнорируется, при ошибке чтения → UNREADABLE |
| D11 | bid_modifiers | EXCLUDED | — | намеренно: наш стандарт поверх источника (`step_age_bidmods`) |
| D12 | strategy_name | СРАВНИВАЕТСЯ | `campaigns.json` BiddingStrategy | Grid `edit_rows.strategyData.strategyName` |
| D13 | ads_with_video | СРАВНИВАЕТСЯ* | `adaptive_ads_for_update` source count | target `adaptive_ads_for_update` по id_maps ads; `?` только если Grid не прочитан |
| D14 | button_cta | СРАВНИВАЕТСЯ* | `adaptive_ads_for_update` source count | target `adaptive_ads_for_update` по id_maps ads; отдельного repair-шага пока нет |
| D15 | ad_price | EXCLUDED | — | цена из фида ЦЕЛИ, не источника |
| D16 | utm_tracking | СРАВНИВАЕТСЯ* | `campaigns.json` TrackingParams (нормализовано) | Grid `CampaignsEditData.bannerHrefParams` (tri-state: None→UNREADABLE) |
| D17 | site_monitoring | СРАВНИВАЕТСЯ* | `campaigns.json` Settings.ENABLE_SITE_MONITORING | v5 `campaigns.get` Settings.ENABLE_SITE_MONITORING |
| D18 | minus_places | СРАВНИВАЕТСЯ | source ExcludedSites | target Grid `CampaignsEditData.disabledPlaces`, копируется 1в1 |
| D19 | shopping_filter_count | СРАВНИВАЕТСЯ* | `shopping_ads.json` SHOPPING_AD count | v5/v501 `ads.get` с `Type in (SHOPPING_AD, SMART_AD)` |
| D19b | listing_filter_count | СРАВНИВАЕТСЯ* | `ads.json` LISTING_AD count | v5/v501 `ads.get` с `Type=LISTING_AD`; ListingAd не должен превращаться во второй ShoppingAd |
| D19c | shopping_filter_signature | СРАВНИВАЕТСЯ* | `ShoppingAd.FeedFilterConditions.Items` | Grid `GdShoppingAd.feedFilter.conditions`; проверяет сами фильтры, не только count |
| D19d | listing_filter_signature | СРАВНИВАЕТСЯ* | фильтр `ShoppingAd` той же source-группы (ListingAd source body не отдаётся) | Grid `GdListingAd.feedFilter.conditions`; каталожное объявление должно получить тот же фильтр группы |
| geo | geo_consistency | СРАВНИВАЕТСЯ | ключи источника | ключи цели (нет ли иностранного города) |

> * D16/D17: tri-state UNREADABLE если нужное поле не пришло из Grid/v5 (fail-safe).
> * D19: UNREADABLE если v5-fallback не отработал (нет токена).

---

## Где что искать по симптому

| Симптом | Где искать |
|---------|-----------|
| Ключи не залились / недолив | `copy_steps.step_keywords` (Grid batch=1000 + v5 fallback=200); аудит addedItems — пустой = Grid не принял (баг 2026-07-16); `---autotargeting` — НЕ ключ, отсекать |
| Фид не тот / не нашёлся | `copy_request.parse_feed_map` (public/UI normalization), `copy_feeds._copy_auto_feed_map` (mode=other fallback), `copy_engine._copy_validated_feed_map` (target ownership), `copy_snapshot._copy_filter_snapshot` + `_copy_skip_unmapped_feed_campaigns`; preflight: `_copy_snapshot_preflight` |
| Preflight стоп | `copy_snapshot._copy_snapshot_preflight` — домен, фид, гео |
| МК дублируются / не удаляются | `copy_cleanup._copy_cleanup_uac_drafts` — v5 UAC не видит, удалять по куки; `_copy_is_uac_grid_row` проверяет по `"tp6_"/"tp7_"` в имени |
| Картинки от чужого сайта | `copy_uac._copy_uac_campaigns` round-robin own hashes (mode=upload); v5-ветка: `copy_steps.step_adaptive_creatives` доливка до 5 |
| PriorityGoals не перенеслись | `copy_metrika._copy_apply_metrika` второй update с `Operation="SET"` |
| Стратегия изменилась | `copy_engine.py:969` PFCMG-восстановление; НЕ добавлять MULTIPLE_GOALS в `conv_types` — даунгрейд |
| Промо не скопировалось | `copy_snapshot._copy_filter_snapshot` домен-гейт; `direct_copy.py` phase_pull — Status/State → 8000; `step_attach_promos` |
| DisplayUrlPath/«!» обрезается | `copy_steps.py:1055/1066` — обрезать только при превышении; `grid_finalize:2217/2256` `linkTail` RMW |
| Сверка (verify) ложный mismatch | `_copy_delayed_reverify` — source_grid надо пересобрать (иначе adaptive недочитан); поллинг sitelinks |
| WRONG_AUTOTARGET / COMPANY_INFO_ENABLED_LIVE на tp2 | `step_fix_search_campaign_invariants` (`copy_steps.py:659`) — форсит ДО verify; если поздно — `direct_delayed_repairs` страховка; verify это покажет в `live_verification` |
| Докрутка РСЯ / места показа | `grid_finalize.set_campaign_invariants`; для копий: `_unified_campaign_update_from_edit_row` guard `DEFAULT`/`MULTIPLE_CPA` и `OPTIMIZE_CLICKS` только без лимита/avgBid/бюджета → `_unsupported_strategy`; weekly clicks с бюджетом подтверждён HAR и пишется как `AUTOBUDGET_AVG_CLICK` |
| `full PATCH обнуляет картинки МК` | `routes_content_editor._uac_campaign_patch_payload` деривация `content_ids` из `contents`; порядок правок: картинки — ПОСЛЕДНИМИ |
| Heal-цикл завис | `copy_engine._copy_delayed_reverify` — min 1200с (`_COPY_HEAL_MIN_SEC`), 8 раундов, 200с пауза |
| Job не стартует / вечный queued | Очередь: воркер создания клеймит copy-джобы если `kind` не фильтруется → `queue_server._worker_claim_web_jobs` AND фильтр `kind<>'copy_campaigns'` |

---

## Смежные доки (не дублируются, только ссылки)

- `docs/ARCHITECTURE.md` — слои всего пакета, граф импортов, copy-кластер
- `README.md` — create-flow (tp1–tp10: основные tp1–tp7 + Посевы tp8–tp10), Grid/v5, куки главпотока
- `STATE_COPY_OTHER.md` — история инцидентов «Прочих сфер», баги 1–7, что осталось
- `ERRORS_JOURNAL.md` — root-cause всех инцидентов, «не помогло ранее»
- `CAMPAIGN_INVARIANTS.md` — 6 обязательных инвариантов (применимы и к копиям)

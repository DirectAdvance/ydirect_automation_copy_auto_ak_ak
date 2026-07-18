# ARCHITECTURE — карта кластеров и связности `direct/`

> Карта пакета `home/seoadvanced/direct/` (нейродиректолог: автосоздание кампаний
> Яндекс.Директа). Показывает **слои**, **кто что импортирует** и **где что искать**.
> Обновляется при крупном рефакторинге. Живой граф кода — MCP `code-review-graph`
> (но он индексирует ВЕСЬ репозиторий, для среза `direct/` пользуйся этим файлом).

---

## 🧭 Когда и как смотреть эту карту

| Ситуация | Что делать |
|----------|-----------|
| **Перед правкой** незнакомого места | Найди модуль в таблице «Инвентарь» → пойми его слой и зависимости, потом правь |
| **Impact-анализ** («что сломается, если тронуть X») | Раздел «Связность»: чей X — ядро (`blueprint.py`, `grid_*`, `kontent_pack`) → блwar-радиус большой; лист (`*_verifier`, `create_set_input`) → локально |
| **Куда добавить новый код** | По слою: HTTP → `routes_*`; этап create-set → `create_set_*`; проверка → `*_verifier`/`verification_service`; починка → `repair_*`; чтение Grid/UAC → `grid_read`/`uac_read` |
| **«Опять монолит»** (файл > ~1500 строк растёт) | См. раздел «blueprint.py — план разбиения»: извлекать связные кластеры в отдельные модули по устоявшемуся паттерну |
| **Не помню, где логика tpN** | Таблица «Пайплайн create-set» → per-tp builders |

**Правило (глобальный CLAUDE.md):** при 6-10k+ строк в одном файле — разносить по
bounded-context, но без фанатизма (модульный монолит с чистыми границами, НЕ
микросервисы). `blueprint.py` (11k строк) — единственный, кто это правило нарушает;
остальной пакет уже модульный.

---

## 🏛 Слои (сверху вниз — от HTTP к движкам)

```
Процессы/воркеры    main · worker_main · content_main/worker · copy_main
      │             (systemd: direct / direct-worker / direct-content / direct-copy)
      ▼
HTTP-роуты (тонкие) routes_*.py ×18  ── регистрируются в blueprint.py
      │
      ▼
ЯДРО                blueprint.py (11179)  ── сборка app + общая логика контента/
      │             очереди/AI-gate/нейминга (ЦЕЛЬ разбиения, см. ниже)
      ▼
Пайплайн create-set create_set_orchestrator → create_set_<этап>/<tpN>_builders
      │
      ▼
Движки создания     grid_create (куки-создание) · grid_finalize (докрутка ЕПК tp1-5)
      │             kontent_pack (контент-пак M3) · campaign (примитивы Директа)
      ▼
Проверка/починка    verification_service → live_verifier → *_verifier (pure)
                    repair_planner → repair_gate/auto/executor · campaign_spec_audit
Контент/AI          create_content (M3 fan-out) · ai_agents · llm-провайдеры (в ядре)
```

---

## 📦 Инвентарь модулей (по слоям)

### Процессы / воркеры
| Модуль | Строк | Роль |
|--------|------:|------|
| `main.py` | 86 | entrypoint веб (direct.service) |
| `worker_main.py` | 93 | воркер очереди создания (direct-worker.service) |
| `content_main.py` / `content_worker.py` | 122 / 190 | редактор контента (direct-content.service) |
| `copy_main.py` | 103 | копировщик кабинетов (direct-copy.service) |

### HTTP-роуты (тонкие, регистрируются `blueprint.py`)
`routes_pages · routes_overview · routes_accounts · routes_reference · routes_settings ·
routes_content · routes_content_editor(1711) · routes_ai · routes_copy · routes_jobs ·
routes_create_set · routes_deferred · routes_pack · routes_campaigns · routes_set_plan`
— тонкие обёртки над ядром/сервисами. Крупный только `routes_content_editor` (свой домен).

### Ядро
| Модуль | Строк | Роль |
|--------|------:|------|
| `blueprint.py` | **11179** | сборка Flask-app + регистрация роутов + БОЛЬШАЯ общая логика (очередь, AI-gate, нейминг, генерация текстов, склонения, минуса, добивка картинок, LLM-провайдеры). **Монолит — цель разбиения.** |
| `campaign.py` | **437** | **re-export хаб:** cookie-store + `_AGENCY_RESOLVER` + io; ре-экспортирует полный namespace (`cmc.*`) для 30+ импортёров. API-движки вынесены (см. ниже) |
| `direct_v501_client.py` | 956 | **DirectV501Client** + `UnifiedCampaignSpec` + `build_v501_client` — v501/ЕПК API (tp1–tp5) |
| `uac_client.py` | 747 | **UacClient** + `MasterCampaignSpec` + UAC-константы (`USER_AGENT`/`UTM_TEMPLATE`) + image/audience/sitelink-хелперы — UAC/Мастер/Товарка (tp6/tp7) |
| `ai_agents.py` | 2184 | AI-агенты (генерация/проверка контента) |
| `create_content.py` | 880 | ядро генерации контента одной РК (M3 fan-out) |
| `kontent_pack.py` | 1265 | чтение контент-пака с M3 (env `NEURO_PACK_MOUNT`, локальная копия / sshfs) |

### Пайплайн create-set (оркестрация → этапы → per-tp builders)
| Слой | Модули |
|------|--------|
| Оркестратор | `create_set_orchestrator` (769) |
| Этапы | `create_set_input · context · account · metrika · corrections · minus · plan · precreate · prefetch · resume · response · units · slepok_content · callouts` |
| Per-tp builders | `create_set_tp1`/`tp1_builders`(1641) · `create_set_text`/`text_builders`(tp2/tp4) · `create_set_feeds`(1289)/`feed_builders`(tp3/tp5) · `create_set_gallery` · `create_set_master_product`(tp6/tp7) · `create_set_assets` |
| Пост / промо / финализ | `create_set_postprocess · create_set_repairing · create_set_finalize · create_set_promo · promo · promotions · precreate` |

### Движки Grid / UAC
| Модуль | Строк | Роль |
|--------|------:|------|
| `grid_create.py` | 847 | куки-движок СОЗДАНИЯ (Grid GraphQL на куках агентства); ре-экспортирует payload-фабрики из `grid_create_payloads` |
| `grid_create_payloads.py` | 288 | чистые payload-фабрики: `build_unified_campaign`/`build_adgroup`/`build_ad`/`build_shopping_ad` + `_fill_titles`/`_fill_bodies`/`_dedup_keep`/`_PLATFORMS_OFF`/`_campaign_minus_kw` |
| `grid_finalize.py` | 2070 | докрутка ЕПК tp1–tp5 (то, что v5 API не умеет: картинки, фид-фильтры, места) |
| `grid_read.py` / `grid_content_verifier.py` | 389 / 171 | read-only чтение/проверка Grid |
| `uac_read.py` / `uac_verifier.py` | 166 / 114 | read-only UAC (tp6/tp7) |

### Проверка / починка
| Модуль | Строк | Роль |
|--------|------:|------|
| `verification_service.py` | 123 | оркестратор read-only проверки |
| `live_verifier.py` | 145 | live-проверка джоба |
| `verifier.py` | 309 | пост-проверка |
| `campaign_result · campaign_state_verifier · local_result_verifier · grid_content_verifier · uac_verifier` | — | pure-чекеры (без сайд-эффектов) |
| `campaign_spec_audit.py` | 1517 | декларативный spec по tp + аудитор + фиксеры (IMAGE_MISSING, FEED_FILTER_*, PLACEMENTS_WRONG, VIDEO_MISSING…) |
| `repair_planner · repair_gate · repair_auto · repair_executor` | 493/478/710/**76** | планирование → gate → авто → исполнение починки (`repair_executor` — тонкий re-export фасад) |
| `repair_common · repair_content · repair_media · repair_keywords` | 64/292/285/470 | домены ремонта (извлечены из repair_executor): deps+const / promo+callouts+rename / media+adprice+default_text / keywords |

### Копировщик
`copy_main · copy_steps(1084) · copy_geo_morph(283)` — копирование кабинетов 1:1 + морф-гео-замена
(+ ремап r-кода кодера, sitelinks, set_default_text фидов, доменно-агностичный URL — см. ERRORS_JOURNAL `COPY_LOGIN2LOGIN_GRID_BRANCH_GAPS`).

### Скрипты
`restore_shift_keywords.py`(359) · `seed_slepok_content.py`(33) — разовые/восстановление.

---

## 🔗 Связность (граф внутренних импортов)

**Ядро (большой blast-радиус) — трогать осторожно:**
- `blueprint.py` — импортируется воркерами `copy_main · content_worker · content_main ·
  worker_main` + `campaign_spec_audit · restore_shift_keywords · seed_slepok_content`.
  Сам тянет: все `routes_*`, `create_set_orchestrator/units/master_product`,
  `create_content · grid_read · uac_read · copy_steps · promo`.
- `kontent_pack.py` — импортируется `blueprint · create_set_assets · create_set_master_product`.
- `grid_finalize.py` — через `grid_read` и `routes_content_editor`.

**Оркестрация:**
- `create_set_orchestrator` → `create_set_{account,callouts,gallery,input,metrika,
  postprocess,precreate,promo,response,resume,slepok_content,text,tp1,units}`.
- `create_set_feed_builders` → `create_set_input · create_set_tp1_builders`.

**Проверка/починка (листовой слой, локальный blast):**
- `verification_service` → `campaign_result · grid_read · live_verifier · repair_planner · uac_read`.
- `live_verifier` → `campaign_result · campaign_state_verifier · grid_content_verifier ·
  local_result_verifier · repair_planner · uac_verifier`.
- `repair_executor` → `create_set_feeds · promo`; `verifier` → `repair_planner`.

Правило чтения: **`*_verifier` и `create_set_<этап>` — листья** (мало кто зависит,
правки локальны). **`blueprint`, `grid_*`, `kontent_pack`, `campaign`** — узлы-хабы
(правка тянет пере-проверку зависимых воркеров).

---

## 🧩 blueprint.py — план разбиения (задача «убрать монолит»)

`blueprint.py` = сборка app + остаточная общая логика (501 функция, 112 констант,
0 роутов — роуты уже вынесены в `routes_*`). Разбиение продолжает УСТОЯВШИЙСЯ паттерн
пакета: извлечь связный кластер → новый `direct/<concern>.py` с docstring
«extracted from blueprint.py» → `blueprint` импортирует. Gate после каждого извлечения:
`py_compile` + smoke `/direct/automation`.

### Инвариант, который НЕЛЬЗЯ ломать

`blueprint.py` = **wiring-hub**. Извлечённые модули **никогда не импортируют `blueprint`** —
они либо чистые листья, либо получают хелперы через **deps-dict / `configure()`**
(как `create_set_master_product`: `_rsya_texts = deps['_rsya_texts']`). Направление
импорта строго одностороннее: `blueprint → newmod`.

**Единственная опасность — `_bp.X` доступ по атрибуту** из 6 entrypoint-модулей
(`worker_main`, `content_main/worker`, `copy_main`, `seed_slepok_content`,
`restore_shift_keywords`): любой перенесённый символ, который они трогают, надо
**ре-экспортнуть** из blueprint (`from .newmod import X` вверху). Иначе `_bp.X` падает.
Smoke после каждого шага: `py_compile` нового модуля + `blueprint.py`, затем импорт
`direct.main / worker_main / content_main / copy_main` (все 4 `_bp.X`-поверхности).

### Таблица кластеров (LOW → HIGH риск)

| # | Модуль-цель | ~строки | Ключевые символы | Входящие извне | Шаренный state | Риск |
|---|-------------|--------:|------------------|----------------|----------------|------|
| 1 ✅ | `llm_providers.py` **(ИЗВЛЕЧЁН 2026-07-04)** | 351 строк | `_m3_complete(_url/_parallel)`, `_m3_llm_probe`, `_openrouter_api_key/_probe`, `_or_complete_url`, `_llm_pair_for`, `_strip_error_leak`, `_has_error_leak` + `_M3_LLM_*`, `_OPENROUTER_*`, `_M3_LEAK_MARKERS` | blueprint (ре-экспорт); `copy_geo_morph`/`ai_agents` косв. | `_touch_running_jobs_heartbeat` инъектится через `configure()`; `_OPENROUTER_KEY_CACHE`/`_M3_LEAK_MARKERS` переехали | **DONE** |
| 2 | `city_morph.py` | 7429,7461,7494; 7862,8080; 9033 | `_CITY_LOCATIVE`, `_RU_CITIES`, `_city_locative/_prep`, `_content_city`, `_replace_foreign_city`, `_drop_foreign_city_keywords` | `create_set_master_product` (deps) | нет (прецедент — `copy_geo_morph`) | **LOW** (ребро `_replace_foreign_city→_drop_used_car` — со-перенос) |
| 3 | `text_norm.py` (анти-AI санитайзеры) | 8228-8970 | `_sanitize_content`, `_replace_emdash/_sep_hyphen`, `_is_bad_start`, `_trim_to_word`, `_split_utp`, `_bad_ad_title/_text/_sitelink`, `_alternate_rhythm`, `_dedup_by_first_word` + `_AI_STAMP_WORDS`, `_SHORT_TITLE_POOL` | `create_set_master_product` (deps); text_gen | нет (чистые строки) | **LOW** (разблокирует #6) |
| 4 | `promo_gen.py` | 10139-10373 | `_promo_extract_json`, `_extract_title/text_candidates`, `_promo_validate/amount_steps/preview/ctx` | `ai_agents` (`_promo_validate`) | `_promo_ctx`→slepok/DB (инъекция) | **MED** |
| 5 | `campaign_naming.py` (+`model_urls.py`) | 6453,6551; 10374-10827 | `_ag_part1_map/_rev`, `_ct_for_name`, `_coder_name_real_brand`, `_build_name`, `_resolve_region`, `_tp_plan_names`; ротатор title2; url-хелперы | `create_set_plan` (`_build_name`,`_ct_for_name` via deps) | `_victory_conn` (инъекция); кэши `_AG1_*`, `_TITLE2_*` (переносятся) | **MED** |
| F | `yandex_api.py` + `db.py` (фундамент) | 4521-4600; 4655-4944 | `_victory_conn(_rw)`, `_v5_get/_call/_units`, `_v501_call/_svc`, `_direct_tokens`, `_token_for_login`, `_metrika_goals_for` | ШИРОКИЙ fan-in + `_bp.X` | нет upward-deps → нет циклов | **MED** (механически, с ре-экспорт-шимом) |
| 6 | `text_gen.py` | 7408-9216 | `_rsya_texts/_titles`, `_fill_variants`, `_diverse_text_offers`, `_fallback_master_titles`, `_fill_title`, brand/keyword-дропы + `_TITLE_PROMO_POOL`, `_RSYA_TEXT_POOL` | `create_set_master_product/tp1(_builders)/text_builders` (deps) | ротация `_TITLE_PROMO_IDX` + кэши брендов (переносятся); зависит от #2/#3 | **MED-HIGH** (только ПОСЛЕ #2/#3) |
| 7 | `ai_content.py` | 10734-11179 | `_content_cache_key/_complete`, `_ai_campaign_content_for_item`, `_ai_group_content`, `_gen_campaign_content`, `_slepok_content_ensure/get/save`, `_seed_slepok_content` | `create_content`, `create_set_orchestrator`, `seed_slepok_content` (`B._seed_slepok_content`) | `_CONTENT_CACHE`+`_CONTENT_CACHE_LOCK`+DB; ре-экспорт `_seed_slepok_content` | **HIGH** |
| 8 | `queue_server.py` (очередь/воркер/watchdog/deferred) | 168-240; 2436-3990 | `_CREATE_JOBS/_QUEUE/_COND/_JOBS_LOCK/_DRAIN`, все `_job_*`, `_jobs_db_*`, `_create_worker_loop`, `_worker_*`, `_deferred_*` | `worker_main` (`_bp._CREATE_JOBS`…), `routes_jobs`, `create_set_orchestrator/repairing` | ВСЕ `_CREATE_*` mutable-globals + потоки + Condition + DB | **HIGHEST** (переносить последним, одним куском) |

**Правила владения глобалами:** mutable-globals переносятся ВМЕСТЕ с функциями (единый
источник, не оставлять копию): `_OPENROUTER_KEY_CACHE`→llm_providers; `_AG1_*`/`_TITLE2_*`→
campaign_naming; `_TITLE_PROMO_IDX`/бренд-кэши→text_gen; `_CONTENT_CACHE(_LOCK)`→ai_content;
все `_CREATE_*`→queue_server. Константы, читаемые многими (`_EXCLUDE_DIRECTOLOGS`,
`DEFAULT_STATUS`, `_STATE_ORDER`, `_SLEPOK_KEY/_CANONICAL`) → крошечный `constants.py`
(или оставить в ядре и ре-экспортить), НЕ дублировать.

**Порядок:** 1→2→3 (safe, делать первыми) → 4 → 5 → F → 6 → 7 → 8 (последним).

> **Статус извлечения (2026-07-04):**
> - ✅ **#1 llm_providers** (351) · ✅ **#3 text_norm** (404) · ✅ **#2 city_morph** (190) ·
>   ✅ **#4 promo_gen** (267) · ✅ **#5 campaign_naming** (262) + **model_urls** (124) · ✅ **copy_engine**
>   (2229) · ✅ **#6 text_gen** (895) · ✅ **#7 ai_content** (261) — ВЫНЕСЕНЫ, import-smoke зелёный, в проде.
>   **blueprint 11198 → 6785 (−4413, ~39%; 9 модулей).**
> - ✅ **#6 text_gen** — пулы промо/текстов, ротация `_TITLE_PROMO_IDX`, дедуп/варьирование, дропы
>   бренд/модель-ключей, когерентность скидок/платежей (вкл. `_bad_credit_payment_range` — источник
>   инъекции в text_norm), фолбэк-заголовки. 7 DI (3 функции + 4 константы-пула). Ловушка stale-binding:
>   `_title2_blocklist` берётся из campaign_naming (реальный), НЕ из city_morph (там DI-stub).
> - ✅ **#7 ai_content** — `_ai_campaign_content_for_item`/`_ai_group_content`/`_gen_campaign_content`,
>   `_slepok_content_ensure/get/save`, `_seed_slepok_content`, кэш `_CONTENT_CACHE(_LOCK)` (единый объект,
>   blueprint шарит через ре-экспорт — только мутация-словаря, без reassignment). 4 DI (`_victory_conn(_rw)`,
>   `_gc_ct`, `_cached_campaign_content`). `_aic.configure` — в КОНЦЕ модуля (после def `_cached_campaign_content`).
> - ⏳ **#F yandex_api+db** — ОТЛОЖЕН: foundation-слой критически перемешан с `register_*_routes`,
>   `configure`-вызовами и 5 оставляемыми (`_LIVE_V4`/`_do_balance`/`_STATE_ORDER`/`_TOKEN_ONLY_TYPES`/
>   `_preflight_creds`) → ~20 pointwise-правок; `_bp.X`-доступ из 4 entrypoint (content_main/worker,
>   restore_shift, campaign_spec_audit) → нужен точный re-export-шим (`from .db import X`);
>   корректность v5/v501/токен-путей подтверждает ТОЛЬКО живой create-set. Делать focused-шагом
>   с прогоном РК, НЕ пачкой. `_preflight_creds` оставить (тянет grid `_block_bootstrap`);
>   co-move `_is_units_exhausted` (re-import из create_set_units).
> - ⏳ **#8 queue_server** (очередь/воркер/watchdog/deferred) — HIGHEST, переносить последним одним куском.
>   ⏳ **#F yandex_api+db** — foundation, focused-шагом с прогоном РК (см. выше). Это последние два кластера.

---

## 🗂 Слепок — четырёхслойная модель и цепочка использования

> Зафиксировано 2026-07-13 по итогам разбора багов и аудита слепков (Кудерко, Щербакова).
> DoD-чеклист — §5.c в `DOD.md`. Оперативные проверки структур — `SLEPKI_AUDIT_2026-07-12.md`.

Слепок — это не JSON-файл сам по себе, а склейка четырёх разнородных слоёв, которые должны
согласованно описывать «как выглядит хороший набор кампаний для директолога X на типе сайта Y».

| # | Слой | Файл / модуль | Типичная причина рассинхрона |
|---|------|---------------|------------------------------|
| 1 | **Structure** — «меню»: какие tp-типы, ct-сегменты, `targeting_mode`, `pricing`, `feed_role` существуют у (директолог, site_type) | `direct/slepki/<key>.json` (по файлу на слепок) + `_order.json`, собирается `slepki_store.assemble()`; монолита `slepki_structure.json` нет | Не обновлён при онбординге: пропущены МК (tp6) или Товарка (tp7); синтетические заглушки вместо реальной структуры кабинета |
| 2 | **Profile** — «шлюз»: что из structure реально показать в UI и пробросить в задание создания | `targeting_profile.json` | Не зеркалит structure (корень бага tp6/tp7 у Щербаковой/Павлова/Караваева, 2026-07-12/13) |
| 3 | **Content pack** — реальные «кирпичи» контента: ключи по ct-коду, картинки/видео по site_type-папкам, минус-слова, ToV-промпт + корпус примеров директолога | M3, `kontent_pack.py`, `/opt/neuro_content_local` | Чужие ключи в ct (пример: ct0022 у Щербаковой — ключи BAIC X40 вместо X35, исправлено 2026-07-13); пустой корпус ct → молчаливый autotarget вместо явного `blocked` |
| 4 | **Сервис-исполнитель** — код, превращающий позицию слепка в реальную кампанию через Direct API v5 (tp1/tp2/tp4/tp5) или Grid/UAC (tp6/tp7) | `create_set_plan.py`, `create_set_master_product.py`, `campaign.py` и др. | Переинтерпретирует позицию: fan-out по всем фидам вместо explicit `feed_role` (пофикшено 2026-07-13); CPC+CPA через account-level галочку конфликтует с per-position pricing новых позиций |

**Слепок «работает» только когда все четыре слоя согласованы. Баг в любом одном — результат
неверный, даже если остальные три идеальны.**

> ⚠️ **ИНВАРИАНТ (частая грабля, 2026-07-13): `targeting_profile.json` авторитетен по СОСТАВУ tp —
> не только structure.** `_slepok_profile_excludes_tp` (`blueprint_targeting.py:218`) вырезает из UI
> **И из создания** любой tp, которого нет в профиле, даже если он присутствует в
> `slepki_structure.json`. Практический вывод: **tp6/tp7 должны быть в `targeting_profile.json` у
> тех слепков, кто реально их запускает** (Терехов реально гонит tp6/tp7, но профиль их не содержал →
> позиции молча не показывались и не создавались — корень бага у Терехова/Щербаковой/Павлова/Караваева;
> tp6/tp7 добавлены в профиль 2026-07-13). Profile — не декоративное зеркало structure, а жёсткий шлюз.

### Цепочка использования

Оператор выбирает: **логин аккаунта → site_type → слепок-директолог**.

Далее:
- Сервис читает structure, отфильтрованную через profile.
- Для каждой позиции резолвит фид, вытягивает `ct → ключи / картинки / видео` из content pack.
  - **URL модели объявления** резолвится в ДВА слоя (2026-07-13, `create_set_feeds.py`): (1) `FeedOffersPreview`
    = **sample** офферов (не все) → покрывает частые модели; (2) `_auto_feed_urls()` доливает точный url
    из ПОЛНОГО raw XML фида (`home/yandex.xml`) по ключу `mark_id+folder_id` через `setdefault` (covered из
    sample не трогаются) → модель вне sample-выборки (напр. Changan UNI-T) всё равно получает точный url.
    Детали и статус — DOD 1.10.
- Генерит тексты через LLM с ToV-гейтами (`ai_agents.py`, `create_content.py`).
- Создаёт черновики через v5/Grid (`campaign.py`, `grid_create.py`, `create_set_master_product.py`).
- Фоновая докрутка сверяет с реальным кабинетом и дозаполняет пробелы (`grid_finalize.py`, delayed-repair).
- Отчитывается: job status, виджет очереди, `live_verification`.

### tp-типы (сводная таблица)

Источник истины — справочник `public.local_gsheet_naming` (Victory, `type='tp'`), сверено SQL 2026-07-09.
Диспетчеризация по `it.type → _TYPE_TO_TP` в `create_set_orchestrator.py:409`.
Полный per-tp чек-лист (настройки, галочки, контент) — §3 в `DOD.md`.

| tp | Внутр. имя диспетчера | Тип кампании | Создаётся через |
|----|-----------------------|--------------|-----------------|
| tp1 | `tp1_rsy` | РСЯ (Марки/Модели/Общее, КС + автотаргет) | v5 API |
| tp2 | `search_test` | Поиск | v5 API |
| tp3 | `rsya_gallery` | Товарная галерея (legacy) — ЕПК канал Поиск ТОЛЬКО `placementTypes=["ADV_GALLERY"]`, ShoppingAd+ListingAd без TextAd, автотаргет `search_tp2`, fan-out по фидам как tp5/tp7. Редко используется. Подробно — §3.3 `DOD.md` | `create_set_feeds`/`feed_builders` |
| tp4 | `search_dynamic` | Поиск + Динамика (`isOrganicSearchEnabled=True`) | v5 API |
| tp5 | `search_gallery` | Поиск + Товарная галерея (TextAd+ListingAd+ShoppingAd, ЕПК) | v5 API + Grid-finalize |
| tp6 | UAC (отд. ветка) | Мастер кампаний (МК). Имя = `{базовое имя} - {метка}`, метка ∈ `автотаргетинг / КС / аудитории / КС+аудитории` (без CPC/CPA). Кодер позиции: `{ct}_aon_n000_r0000_ct001_ag011_g00`. UI-бейдж вид таргетинга. | Grid/UAC (`create_set_master_product.py`) |
| tp7 | UAC (отд. ветка) | Товарные кампании (ТК), ShoppingAd/ListingAd по фиду. Имя = `{базовое имя} - {метка}`. Кодер позиции: `{ct}_aon_n000_r0000_ct010_ag001_g00`. | Grid/UAC (`create_set_master_product.py`) |
| tp8–tp11 | — | Telegram / Max / Telegram+Max / Connected TV — есть в кодере, сервис пока не создаёт | — |

### Позиция слепка — идентичность и реконструкция (2026-07-13)

> Каноны подтверждены Семёном 2026-07-13. Детали с примерами и операционные правила — §5.d `DOD.md`.

**Идентичность позиции = таргетинг.** Две кампании образуют *одну позицию слепка*, если
совпадает `targeting_mode` + keyword-corpus + audience ids + feed (`feed_id`/`feed_role`/
`feed_filters`). Цели, pricing (`cpc`/`cpa`/`crm`), медиа, тексты, лейблы — **варианты**
внутри позиции (`goal_variants`, `media_variants`, `title_variants`). Не размножать позиции
по цели или картинке.

**targeting_mode** читается из **живого UAC/Grid payload** — не из имени кампании, не из
ct-кода. UAC payload оборачивается в `{"result":{...},"success":true}` — разворачивать
перед разбором обязательно, иначе всё ошибочно читается как autotargeting.
Grid endpoint: `https://direct.yandex.ru/web-api/grid/api` (не `/json/v5/grid`).

**Метка таргетинга в имени tp6/tp7** выводится из факта (ключи/аудитории в payload):
ключи+аудитории → `КС+аудитории` · только аудитории → `аудитории` · только ключи → `КС` · ничего → `автотаргетинг`.
UI-бейдж на странице «Структура слепков» парсит метку из имени и отображает вид таргетинга (аналогично бейджу tp1/tp2); дерево раскрывается до уровня вида таргетинга.
Strategy-варианты (cpc/cpa/crm) с одинаковым таргетингом — дедуп в одну позицию слепка (не размножать по цели/pricing).

**Источник истины tp6/tp7 = живые payload'ы.** `Dim_Campaign` недосчитывает UAC
(Dim=3 / live=9; Dim=0 / live=5) — для tp6/tp7 ориентироваться на неё нельзя.
Для tp1–tp5 `Dim_Campaign` по `CampaignName ~ '^tp[1-5]_'` достаточен как дешёвый аудит.

**Reconciler** — отдельный слой между живым кабинетом и `slepki_structure.json`.
Ручная правка слепков по именам/предположениям запрещена (ошибки: чужемодельные ключи
у `scherbakova`, over-split целей у `kuderko`):

```
live UAC/Grid payload
    ↓ нормализация позиций (правило дедупа по targeting)
нормализованные позиции
    ↓ staging-артефакты → reconciler_staging/
    ↓ ревью Семёна
slepki_structure.json / targeting_profile.json
```

### Бейдж таргетинга tp1-5 (механика `tgt`) — 2026-07-13

> Аналог бейджа tp6/tp7 (см. выше «Метка таргетинга в имени tp6/tp7»), но для текстовых узлов.
> tp6/tp7 берут метку из имени кампании; tp1-5 — из ЗАПЕЧЁННОГО поля `tgt`.

**Где живёт.** У узлов tp1-tp5 в `slepki_structure.json` есть поле `t['tgt']` — на уровне
tp-узла, рядом с `code`/`title`/`groups`. Это короткая метка живого таргетинга:
`КС` · `КС+авто` · `ауд+авто` · `КС+ауд+авто` · `автотаргетинг`.

**Источник.** `reconciler_staging/verify_tp15_<slepok>.json` — снято 2026-07-13 живым
`keywords.get` + `audiencetargets.get` + `retargetinglists.get`. Агрегация = **мода** метки
по кампаниям узла. Покрыто **76 узлов**; где данных нет (Квиз / Неопределено / Мульти+БУ)
поле `tgt` отсутствует, бейдж падает на **gc-fallback**.

**⚠️ Почему НЕ из gc.** У tp1-5 `aon`/`aoff` в gc всегда `aon` на поиске/РСЯ → gc-бейдж вечно
показывал бы «автотаргетинг». Живой факт о реальном таргетинге есть только в `verify_tp15_*.json`.

**Проброс в UI (backend).** `blueprint_targeting.py:245 _slepki_structure_for_ui` делает deepcopy
структуры — `tgt` проходит в UI без правок бэкенда.

**Рендер (frontend).** `templates/direct/index.html`:
- helper `_tgtFromBaked(lbl)` + карта `_TGT_BADGE_MAP` (~1480/1489) — маппинг метки на CSS-классы
  `auto` / `manual` / `aud` / `mix`;
- в `slepkiTpTree` (~1610): `const _baked = _tgtFromBaked(t.tgt)` — берёт запечённую метку, при
  её отсутствии откатывается на gc-fallback.
- tp6/tp7 бейдж рендерится ИЗ ИМЕНИ кампании (`it.t`, `_tp67Targeting`), НЕ из `tgt` — эту ветку
  не трогали.

**Правая панель деталей таргетинга группы (клик по строке, 2026-07-13).** Помимо инлайн-бейджа,
клик по строке группы (напр. Москвич, сегмент «Марки») раскрывает **справа панель с таргетингами
этой группы из контент-пака** (ключи/аудитории/минус — то, что реально попадёт в кампанию). Бейдж
даёт краткую метку, панель — полный состав таргетинга по клику. Клик-обработчик дорабатывается
(доводка выбора строки/подсветки).

### Presence / структурные правила слепков (2026-07-13)

**Истина по составу «слепок × site_type × логины» = `public.local_gsheet_sites` (Victory), НЕ
staging `accounts_seen`** (правило Семёна). Staging-артефакт показывает лишь то, что попало в
живой харвест; окончательный вопрос «есть ли этот site_type у директолога и на каких логинах»
решается по реестру `local_gsheet_sites`.

**Применённые правки структуры 2026-07-13** (`slepki_structure.json`, backup `editbak.20260713_180416`):
- **chepelev tp4 — УДАЛЁН**: все кампании узла ARCHIVED (0 ON) → узла в структуре быть не должно.
- **kryuchkova Квиз/tp7 — ДОБАВЛЕН**.
- **scherbakova tp1 Модели-КС**: gc-код `ct0014 → ct0001` (была коллизия с Марки-КС того же слепка).

**scherbakova tp7 — 3 узла с одинаковым gc это НЕ баг.** В tp7 gc всегда `aon`; различие
таргетинга между узлами выражается меткой в `t` (бейдж таргетинга), а не через gc. Одинаковый gc
при разных метках — ожидаемо, не коллизия.

### Rebuild 2026-07-14 — per-group модель (актуально)

**1. Структура 1-в-1 (per-adgroup).**
Группа = конкретный интент (имя + состав), не схлопывание по ct. Коллизия `gc` — НОРМА: разный
состав при одном `gc` → отдельные группы. Пример: `kuderko / С пробегом / tp4` = **184 группы**
(`Рассрочка|…`, `Автокредит_БУ|…`) вместо 5 ct-агрегатов. Item структуры: `{c, t, gc, gk}`.
`gk` = slug имени группы (кириллица допустима).

**2. Пак-формат per-group.**
Путь ключей: `kontent_oktyabr/<seg>/<tp>/<ct>/keywords/<slepok>__<gk>.txt` (+ `_minus`).
`_minus_shared.txt` — отдельная библиотека слепка, инлайн-минусы групп туда НЕ складываем.
Движок: `read_keywords(..., group=gk)`. `gather()._groups` → `dict[gk → список ключей]`.
API-ключ минус-слов в payload: `NegativeKeywords.Items` (заглавная `I`!).

**3. tp6/tp7 — только Grid (v5 не отдаёт).**
Детект: id в `grid_list_campaigns`, отсутствует в v5. Контент МК — Grid `showConditions`.
Имена по префиксу `tp6_/tp7_`, **не** по Grid `__typename` (инвертирован). tp8 — не собираем.

**4. G8 гео-чистка.**
`geo_strip` убирает города из позитива **и** операторных минус-частей (`-екатеринбург`),
плюс фикс висячего предлога после вырезанного слова.

**5. M3↔101 синк — two-way.**
Pre-push `101→M3` без `--delete`: 101 = главный источник записи, записанное переживает ночной cron.

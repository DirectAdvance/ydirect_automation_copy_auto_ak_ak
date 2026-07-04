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
| `campaign.py` | 1947 | примитивы кампаний Директа |
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
| `grid_create.py` | 1021 | куки-движок СОЗДАНИЯ (Grid GraphQL на куках агентства) |
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
| `repair_planner · repair_gate · repair_auto · repair_executor` | 493/478/710/929 | планирование → gate → авто → исполнение починки |

### Копировщик
`copy_main · copy_steps(972) · copy_geo_morph(283)` — копирование кабинетов 1:1 + морф-гео-замена.

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

> Статус извлечения: обновлять галочками по мере переноса кластеров.

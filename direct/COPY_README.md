# Сервис копирования кампаний Яндекс.Директа

> Навигация: `COPY_INDEX.md` — плотный индекс функций/измерений. `STATE_COPY_OTHER.md` — история
> инцидентов и осталось. `docs/ARCHITECTURE.md:124` — позиция в слоях пакета. `ERRORS_JOURNAL.md` — root-cause.

---

## Что делает сервис

Копирование кабинета Яндекс.Директа 1:1 в один или несколько целевых аккаунтов:

- Кампании, группы, объявления, ключи, минус-слова, уточнения, быстрые ссылки, промо, картинки,
  видео — переносятся структурно с трансформацией домена, гео и фида.
- **Два режима:**
  - **Авто** — один source → один target, фид указывается явно.
  - **Прочие сферы** (`mode=other`) — source → один или несколько target'ов из другой ниши;
    фиды автоматически сопоставляются по URL/имени файла (`_copy_auto_feed_map`).
- **Все кампании создаются черновиками** (`State=OFF` / `launch=False`) — правило неотменяемое.
- Счётчик Метрики и цель заменяются на переданные (`counter_id`, `goal_id`).
- `PriorityGoals` переносятся 1:1 через v5 с `Operation="SET"` (лимит value в микро-единицах:
  5 000 000 000 = 5 000 ₽, 300 000 = 0,3 ₽).

---

## Архитектура потока

```
POST /api/copy_start
│    /api/copy_other_start       ← routes_copy.py
│
├─ _copy_run_job()               ← copy_engine.py:1337
│   │
│   ├─ _copy_snapshot_preflight  ← copy_snapshot.py:223
│   │   (стоп если нет фида, домена, гео)
│   │
│   ├─ PULL источника            ← work/.../direct_copy.phase_pull   [v5 API]
│   │   кампании/группы/объявления/ключи/минусы/callouts/sitelinks/
│   │   промо/картинки → workdir/source/*.json
│   │
│   ├─ pull_source_campaign_assets  ← copy_steps.py (copy_engine.py:1479)
│   │   (Grid: campaign-level callout/sitelink ids — inheritableCallouts/inheritableSitelinkSet)
│   │
│   ├─ validation перед записью цели
│   │   geo_region_ids / feed_map target-ownership / preflight; cleanup ещё НЕ выполнялся
│   │
│   ├─ cleanup цели               ← copy_cleanup._copy_target_cleanup
│   │   (delete_drafts / archive) + _copy_cleanup_uac_drafts (tp6 по куки), только после validation
│   │   ВАЖНО: ДО _copy_filter_snapshot, иначе campaign-level id отфильтруются
│   │
│   ├─ _copy_filter_snapshot     ← copy_snapshot.py:23
│   │   (выбрать только нужные campaign_ids, домен-гейт промо)
│   │
│   ├─ _copy_rewrite_snapshot_context  ← copy_snapshot.py:163
│   │   (замена домена, города, региона в снэпшоте)
│   │
│   ├─ Unified/UAC кампании?     ← copy_engine.py:1392
│   │   ├─ ДА (tp6/tp7, Grid typename) → _copy_grid_unified_campaigns → ГОТОВО
│   │   └─ НЕТ → дальше по v5-пути
│   │
│   ├─ UPLOAD в цель             ← work/.../direct_copy.phase_upload  [v5 API]
│   │   campaigns.add → adgroups.add → ads.add → keywords.add
│   │   → maps сохраняются в workdir/id_maps.json
│   │
│   ├─ _copy_cookie_postprocess  ← copy_engine.py:701   [Grid/куки, 0 баллов]
│   │   ├─ callouts (Grid add + attach)               ← copy_engine.py:810
│   │   ├─ step_attach_sitelinks (sync, с гео-морфом) ← copy_engine.py:825  ← ДО verify!
│   │   ├─ промо (Grid addPromoExtensions)
│   │   ├─ step_keywords         (Grid batch 1000 + v5 fallback 200)
│   │   ├─ step_adaptive_creatives  (RMW titles/bodies/images ×5)
│   │   ├─ step_prices           (из фида ЦЕЛИ)
│   │   ├─ step_videos           (скачать mp4 из Grid source → upload → RMW)
│   │   ├─ step_fix_search_campaign_invariants        ← copy_engine.py:1090  ← НОВЫЙ
│   │   │   (tp2 TEXT_CAMPAIGN: enableCompanyInfo=False + EXACT_V2_MARK/WITHOUT_BRAND)
│   │   ├─ step_age_bidmods      (−100% <18/18-24)
│   │   ├─ step_disabled_places  (baseline анти-фрод список)
│   │   ├─ step_fix_organic_placement  (organic + placementTypes)
│   │   ├─ live_verification + run_copy_verification
│   │   ├─ run_copy_repair       (ключи, shared_sets, shopping_filters)
│   │   └─ step_settings_diff    (report + авто-починка v5-полей ПОСЛЕДНИМ)
│   │
│   └─ _copy_delayed_reverify    ← copy_engine.py:1235  [фон, поток]
│       поллинг sitelinks → полная пере-сверка → перезапись job-result
│
└─ Результат → direct_automation_jobs (Victory, kind='copy_campaigns')
```

---

## Транспорты: где v5 API, где Grid

| Этап | Транспорт | Почему |
|------|-----------|--------|
| `phase_pull` — чтение источника | **v5 API** (авторитетный, стабильный тип) | Grid typename флейкует: наблюдалось «GdUnifiedCampaign» у TEXT_CAMPAIGN → крестим с v5 |
| `phase_upload` — создание кампаний/групп/объявлений | **v5 API** | стабильный контракт, нет куки-зависимости |
| tp6/tp7 МК — создание | **Grid/куки** | v5 не умеет UAC-кампании (campaigns.get Types=UNIFIED_CAMPAIGN возвращает пусто) |
| Callouts, промо, sitelinks, ключи (постпроцесс) | **Grid/куки** | не тратит Direct API units (баллы 152) |
| `step_adaptive_creatives`, `step_videos` | **Grid/куки** | RMW UpdateAdaptiveTextAds без баллов |
| Cleanup tp6 черновиков | **Grid/куки** | v5 не видит UAC-черновики |
| `step_settings_diff` авто-починка | **v5** (3 поля) | Grid пропускает их на стратегии DEFAULT |
| PriorityGoals | **v5 campaigns.update** | куки не нужны; единственный безопасный путь |

> Грабля: Grid `addedItems` может вернуть пустой список при успешном батче ключей — это НЕ ошибка Grid,
> это «не принял». Не считать addedItems как успех если он пуст — только фактически принятые.

---

## Проверка на выходе

После `phase_upload` и `_copy_cookie_postprocess` запускается структурная сверка source↔target.
Подробная таблица измерений — в `COPY_INDEX.md` (раздел «Таблица измерений verify»).

Кратко: 12 реально сравниваемых измерений (D1–D9, D12, D16, D19*), 5 честно-unreadable (D2b, D10, D13, D14, D17),
3 excluded-intentional (D11, D15, D18), + 2 гео. (* D16 — tri-state: None→UNREADABLE если bannerHrefParams не пришёл из Grid; D19 — только при наличии шоппинга, UNREADABLE без v5-токена.)

Результат пишется в `direct_automation_jobs.result` → UI его читает.

Дополнительно: `_copy_delayed_reverify` запускается в фоне — ждёт оседания привязок (поллинг sitelinks,
до 20 мин), потом делает полную пере-сверку и перезаписывает результат.

---

## Известные грабли / инварианты

| Грабля | Корень | Где зашито |
|--------|--------|-----------|
| `---autotargeting` — НЕ ключевая фраза | Директ создаёт 1 плейсхолдер на группу | Фильтровать `kw.startswith("---")` при любой сверке |
| Delayed reverify: min 1200с | Позднее вырезание ключей Яндексом | `_COPY_HEAL_MIN_SEC=1200` в `copy_engine.py` |
| 1009-лимит ключей Grid | Grid принимает max 1000 на батч | `step_keywords` batch=1000 |
| `counter_foreign_owner` | Счётчик принадлежит другому аккаунту → нельзя привязать | Проверяется в роуте до старта job'а |
| Фид должен принадлежать target | Нельзя использовать фид источника в чужом аккаунте; внешний `feed_map` нормализуется и валидируется до cleanup/upload | `copy_api.parse_feed_map` + `_copy_validated_feed_map` + `_copy_preseed_feed_maps` |
| full PATCH UAC обнуляет картинки | `_UAC_PATCH_FULL_KEYS` не содержит `content_ids` | `routes_content_editor._uac_campaign_patch_payload` деривация; порядок: картинки ПОСЛЕДНИМИ |
| `set_campaign_invariants` падает на DEFAULT/MULTIPLE_CPA | `strategyName='DEFAULT'` и `MULTIPLE_CPA` невалидны на запись; weekly `OPTIMIZE_CLICKS` с бюджетом пишется HAR-формой `AUTOBUDGET_AVG_CLICK` | guard `_unsupported_strategy` в `grid_finalize._unified_campaign_update_from_edit_row`; weekly clicks — `grid_finalize._strategy_update_payload` |
| tp6 cleanup не удаляет через v5 | `campaigns.get Types=[UNIFIED_CAMPAIGN]` → пусто | `_copy_cleanup_uac_drafts` — только Grid по куки |
| Роуты обязаны начинаться с `/api/copy_` | nginx `^~ /direct/api/copy_` → `:5022` | комментарий в `routes_copy.py:122` |

---

## Как запустить / подебажить

**Сервис:**
```
ssh proxmox-ts "pct exec 101 -- systemctl restart direct-copy.service"
ssh proxmox-ts "pct exec 101 -- journalctl -u direct-copy.service -n 100 --no-pager"
```

**Порт:** `127.0.0.1:5022` (локально на LXC 101), за nginx на `/direct/api/copy_*`.

**Логи copy-job'а:** Victory БД `public.direct_automation_jobs` (колонка `result`, НЕ `report`; `body` содержит `image_hashes`, `kind='copy_campaigns'`).

```sql
SELECT id, status, created_at, result->>'errors' FROM public.direct_automation_jobs
WHERE kind = 'copy_campaigns'
ORDER BY created_at DESC LIMIT 10;
```

**Рабочие папки job'ов на LXC101:** `/tmp/direct-copy-<job8>-*/` — внутри `source/keywords.json`,
`id_maps.json`. Годятся для доливки без пересоздания.

**Кросс-проверка v5:**
```
токен: .secret/loader.py → load_yandex_direct()["tokens"]["y-direct-victory"]["oauth_token"]
```

**Воркер:** `direct-copy.service` сам стартует copy-поток (`_ensure_copy_worker`). Отдельного
`direct-copy-worker.service` нет. Воркер создания (`direct-create-worker.service`) copy-джобы
НЕ берёт — гард по `kind` в SQL-клейме (`AND coalesce(kind,'') <> 'copy_campaigns'`).

**Внешний API (A2):**
- `POST /api/v1/copy/start` + `X-API-Key` — запуск задачи из внешнего клиента
- `GET /api/v1/copy/status/<job_id>` — статус (с CORS)
- `GET /api/v1/copy/health` — health-check API с тем же `X-API-Key`
- Зарегистрирован в `copy_api.register_copy_api()`; ключ — из конфига сервиса
- `campaign_ids` в public API валидируется строго: JSON-массив положительных ID без дублей,
  максимум 500 за запрос. Строка `"12345"` больше не превращается в `[1,2,3,4,5]`.
- `target_feed_url` во внешнем API принимает только путь от `/`; абсолютные URL не принимаются.
- `mode='other'`: `geo_mode` только `keep|change`; при `keep` поле `geo_region_ids` не попадает
  в job, при `change` `geo_region_ids` валидируется строго и mixed/non-integer элементы дают
  `400 INVALID_GEO`.
- `feed_map` нормализуется на границе API до `{source_feed_id_str: target_feed_id_int}`; мусор,
  нули и нечисловые ключи/значения отбрасываются ДО расчёта idempotency hash и постановки job.
- `image_mode='upload'` требует `image_hashes` как непустой список строк; значения trim'ятся.
  `image_mode='copy'` жёстко очищает `image_hashes`, чтобы внешний клиент случайно не подменил
  картинки при 1:1 копировании.
- `Idempotency-Key` опционален, но рекомендуется: повторный `POST` с тем же payload вернёт
  существующий `job_id`, с другим payload — `409 IDEMPOTENCY_CONFLICT`.
- Ответ `start`: `{ok:true, job_id, status_url, login, agency, total, kind, ahead, existing?, idempotent?}`.
- Ответ `status`: `{ok:true, schema_version, status, public_status, terminal, settling,
  repair_pending, progress, total, selected, result_summary?, error?, log?}`.
  Интегратор должен считать job полностью закрытой только когда `terminal=true`.
  `status='done'` при `public_status='settling'` означает, что копирование завершено, но добивка ещё идёт.
- Для browser-to-API интеграции origin внешнего сервиса должен быть в `COPY_API_CORS_ORIGINS`.

**Прогресс-счётчик и UI-статус:**
- `total` в джобе = `len(selected_ids)` — ВСЕ выбранные кампании (v5 + UAC), не только v5-снапшот
  (copy_engine.py:1488). Карточка показывает «N/total» честно.
- Статус «идёт добивка» (карточка держит `running`-вид и таймер) пока `settling=True`
  (in-memory `_copy_delayed_reverify`) ИЛИ `repair_pending=True` (persistent `direct_delayed_repairs`,
  routes_copy.py:374). Финальный рендер — только когда обе закрыты (copy_common.js:349).
- UI confirm-диалог — кастомная `uiConfirm()` (центр, токены, адаптив), не нативный `confirm()`
  (copy_auto.js:294; определена в automation.js:137).

---

## Известные проблемы / ограничения

| Проблема | Суть | Статус |
|----------|------|--------|
| Per-campaign Grid-форсинг не масштабируется | `step_fix_search_campaign_invariants` + `step_attach_sitelinks` делают синхронные Grid-запросы per-campaign. 10 камп ≈ +2 мин — ок; 170 камп → десятки минут (прогон source2 porg-mjyh6hjv, 173 камп завис на постпроцессе 40+ мин, остановлен вручную). Нужен батчинг: `set_campaign_invariants` уже принимает список; `update_unified_adgroups` — группы батчить. | Открыто; фикс валиден для ~10–30 камп |
| Прогресс застревает на ~88% при форсинге | Постпроцесс не гранулирован внутри шагов — на больших аккаунтах выглядит как зависание без log-строк | Открыто; нужна пошаговая запись прогресса |
| `step_attach_sitelinks` retry итерирует все кампании | При retry шаг перебирает все кампании (не только с ошибками) → лишние Grid-мутации | Minor-долг |
| `sitelink_ids` enrichment в `copy_snapshot` инертен | Поле обогащается, но логика чтения устарела (sitelink_ids через `_copy_filter_snapshot` может не совпасть с `campaign_sitelinks.json`) | Minor-долг |

> Прогон-эталон после фиксов (source1: porg-asfbs7qe→porg-lzjk6p5m, 10 камп, 2026-07-19):
> live errors 8→0, verify mismatch 6→0, ключи 2820=2820 (целы). 4 остаточных warning — report-only
> (1 «голая» кампания без ссылок/промо).

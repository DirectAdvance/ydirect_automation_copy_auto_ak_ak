# Нейродиректолог — Состояние

> Читать ПЕРВЫМ в начале каждой сессии. Обновлять ПОСЛЕДНИМ перед выходом.
> Ошибки создания РК: сигнатуры/решения/что-помогло — **ERRORS_JOURNAL.md** (обязателен к заполнению при фиксах).

> Архив сессий старше 3 дней — **STATE_ARCHIVE.md** (ротация 2026-07-19 (2): перенесены сессии 07-16 и старше). Правило ротации — см. CLAUDE.md.

## Сессия 2026-07-20 — безымянные кампании + счётчик «Создание РК» tp3/tp7 — ЗАВЕРШЕНО

**Задача 1 (данные):** 7 кампаний имели пустой `camp_names` → UI рисовал заглушку «Кампания «Марки»».
В гугл-таблице их не было (в исходнике пришли без имени). Заполнил по конвенции слепка, режим (КС/АТ)
определён по РЕАЛЬНОМУ контенту пака (имя группы врало: terehov-пробел 176 ключей→КС; karavaev — ключей
нет→Аудитории+АТ). Правки: terehov(32 tp4 Марка+1), karavaev(1), kryuchkova(2), piterkina(2).
Коммиты `84245e5` (nested) / `c80147f` (parent), md5 LXC101 ✅. dmp/gen_ses не трогал (спец/шаблон).

**Задача 2 (счётчик создания):** «Создание РК» показывал tp3=108 камп. Root-cause (direct_investigator,
отчёт `.claude/sdd/creation-vs-slepki-divergence-report.md`): **единый планировщик уже есть —
`create_set_structure.py:137 structure_to_campaigns`**, его зовут и `/set_plan`, и реальное создание →
создаётся ВСЕГДА верно (доказал на LXC101: terehov/Мультибренд tp3=2, tp4=«Поиск+Динамика-Марка-КС»).
Врал только preview-бейдж: tp3 падал в generic-else (группы×стратегии). Фикс `bf8d567`: tp3 считает
по camp_names + убран ×фиды (backend: 1 РК на camp_name, фиды группами внутри). Проверил `direct_verifier`
✅, backend 0 строк diff.
**ОТКАТ tp7 `1a74e87`:** ошибочно схлопнул tp7; backend реально ×оплата×фиды (`create_set_plan.py:1072`
«1 камп на группа×оплата»; `:438-462` fan-out по профильным фидам) — зависит от «под стиль сайта»(no_cpa)
и профильных фидов. Вернул пару cpc+cpa. Итог: tp3=camp_names без ×фиды; tp7=группы×оплата×фиды; md5 ✅.
automation.js — статика, рестарт не нужен (Ctrl+F5 на странице).

## Сессия 2026-07-20 — content-editor rename bulk + HAR parity — ЗАДЕПЛОЕНО

**Контекст:** сервис `/direct/automation/content`, вкладки для дневных массовых правок:
заголовки, тексты, быстрые ссылки, уточнения, ссылки, изображения, названия кампаний/групп.
Пользователь попросил добавить массовую замену фрагмента во вкладку «Смена названий» как во
вкладке «Заголовки» и проверить HAR `direct.yandex.ru.68har.har`/`69har.har` по Grid rename.

**Сделано:** `direct/content_jobs.py` вынес общие helpers очереди из `routes_content_editor.py`
без смены публичных API. Rename frontend вынесен из монолитного `content_editor.js` в
`static/direct/content_renames.js`; шаблон подключает новый script перед core JS. Во вкладке
«Смена названий» добавлены «Показать превью» и «Заменить всё»: строится preview `было → стало`
по активному подразделу (кампании или группы), затем одним `campaign_rename` job отправляется
batch payload в общую `direct_automation.content_jobs` очередь. После завершения job UI обновляет
локальные имена и сбрасывает preview.

**HAR-фикс:** `GridClient.set_campaign_names` больше не использует `_narrow_campaign_base`;
по HAR веб-морда Директа делает full-object RMW через `CampaignsEditData` → `UpdateCampaigns`,
поэтому теперь читаем полный unifiedCampaign payload, меняем только `name` и сохраняем
`biddingStategyWithPlatforms`/strategy/platforms/minus/bidModifiers и остальные поля. Это меняет
диагноз старой ошибки: стабильная `Внутренняя ошибка сервера, reqId=...` при rename была
похожа не на чистый баг Яндекса, а на непаритетный payload нашего narrow writer.
Для групп `GroupsForEditLite` теперь читает `retargetingConditionId`/`retargetingId`, а
`build_update_item` сохраняет `retargetings: [{retCondId, id}]` и `retargetingCondition: null`;
группы с ретаргетингами больше не пропускаются. Стоп остаётся только на `bidModifiers`.

**Code review:** остаточный монолит есть: `static/direct/content_editor.js` всё ещё ~4.4k строк,
`routes_content_editor.py` содержит крупные `_load_account`, `_do_replace`,
`register_content_editor_routes`, а `content_images_routes.py::_rsya_inventory` тоже большой.
Сегодня безопасно отделены job helpers и rename JS. Следующий распил без смены поведения:
`content_loaders.py` для `_load_account`/v5 pagination, `content_replace.py` для `_do_replace`,
`content_queue_routes.py` для job endpoints, затем отдельные JS-модули по links/sitelinks/images.

**Проверено локально:** `.venv/bin/python -m py_compile` по изменённым Python-модулям,
`node --check` для `content_editor.js` и `content_renames.js`, `git diff --check`,
pytest `direct/tests/test_routes.py direct/tests/test_content_images_transport_split.py
direct/tests/test_architecture_boundaries.py` — `78 passed`.

**Deploy/evidence:** Mac↔LXC101 md5 совпал для 7 изменённых runtime-файлов; remote
`py_compile` и remote `node --check` OK. 2026-07-20 16:02 +05 перезапущены и active:
`direct-content.service` PID `1334175`, `direct-content-worker.service` PID `1334174`.
HTTP-smoke: локально `:5021/direct/automation/content` → `302 /login`,
публично `https://seoadvanced.ru/direct/automation/content` → `302 /login`,
`/static/direct/content_renames.js` → `200` size `13490`; `/direct/api/content-editor/jobs`
без сессии → `401`.

**UI-fix 16:13 +05:** панель массовой замены названий сначала была под `cern-list`, поэтому
на вкладке «Группы» уезжала ниже тысяч строк. Перенесена сразу под toolbar, перед списком.
Mac↔LXC md5 шаблона совпал, `node --check` OK, `direct-content.service` перезапущен и active;
public smoke после короткого окна рестарта: `/direct/automation/content` → `302 /login`.

**UI-fix 16:19 +05:** во вкладку «Смена названий» добавлен выбор кампаний как во вкладке
«Смена изображений»: левая колонка `ceimg-side` с поиском, чекбоксами, «Выбрать всё»,
«Снять всё», «Применить фильтр», «Сбросить». Пустой выбор = весь аккаунт. Фильтр ограничивает
список кампаний, перечитывает группы только по выбранным campaign_ids и ограничивает массовую
замену активным фильтром. Mac↔LXC md5 для `content_editor.html` и `content_renames.js`
совпал; local+remote `node --check` OK; `direct-content.service` перезапущен и active.
**UI-fix 16:21 +05:** выбор кампаний теперь виден сразу до загрузки аккаунта, как в «Смена
изображений», а не скрывается до клика «Загрузить кампании». Правая колонка показывает пустое
состояние «Введите аккаунт...». `direct-content.service` active; public smoke
`/direct/automation/content?section=renames` → `302 /login`, `content_renames.js` → `200`.
**UI-fix 16:32 +05:** во вкладке «Смена названий» заменён отдельный простой input логина на
такой же визуальный блок выбора аккаунта, как во «Смене изображений»: умный поиск, 5 фильтров,
bulk-кнопки, счётчик и grid чекбоксов внутри `panel-renames`. `ceRenamesLoad()` берёт логин из
этого picker (точный логин в поиске или первый отмеченный аккаунт), старый `#cern-login` удалён.
Mac↔LXC md5 совпал, local+remote `node --check` OK, `direct-content.service` перезапущен и active;
Семён визуально проверил вкладку — стало верно.
**UI-fix 16:35 +05:** нижняя часть вкладки «Смена названий» возвращена к прежней одно-card
структуре: боковая колонка фильтра кампаний (`cern-workspace`/`cern-filter-wrap`/`cern-camps-*`)
убрана из HTML. Верхний picker аккаунтов оставлен. Mac↔LXC md5 совпал; local+remote
`node --check` OK; `direct-content.service` restarted/active, повторный public smoke после
прогрева `/direct/automation/content?section=renames` → `302 /login`.
**Queue/UI-fix 17:15 +05:** правый mini-stack теперь восстанавливает активные copy-job с сервера
через `/direct/api/copy_queue`, а не только из локального `CECOPY_JOB_ID`; `claimed` отображается
как активный запуск, copy-карточки учитываются в заголовке/очистке stack, подключён cache-bust
`20260720_queue_copy_v3`. Copy-card получил прогресс `created/total`, таймер, лог и чеклист.
Счётчик `в очереди — перед вами N` для content-job теперь считает global write lock и
`direct_automation_jobs` со статусами `queued/claimed`, а не только локальные content_jobs.
Mac↔LXC md5 по 5 runtime-файлам совпал; local+remote `py_compile`/`node --check` OK; pytest
`direct/tests/test_routes.py -k 'rename or set_campaign_names or copy'` — `4 passed`.
Перезапущен только `direct-content.service` (copy-worker не трогался), `direct-content.service` и
`direct-copy.service` active; static `content_copy.js?v=20260720_queue_copy_v3` → `200`.
Отдельный `direct_verifier` принял правку. На момент проверки active copy не было (`copy_active=0`),
поэтому reload active-copy визуально не воспроизводился.
**Live rename check 17:15 +05:** по последним зелёным job из скрина проверено не UI, а Direct API:
`ce_5694d3711f25` — 5/5 кампаний совпали с payload; `ce_c178da697364` — 150/150 групп совпали,
mismatch=0, api_errors=0. Старые job 16:37 и раньше были с архивными объектами/старым кодом и
остались ошибочными в истории, но к двум зелёным карточкам не относятся.
**UI-fix 17:27 +05:** на вкладке «Смена ссылки» убрана тихая автозагрузка `/links` при входе
в section: `ceLinksTabOpen()` теперь показывает кэш/пустое состояние, а чтение ссылок запускает
только явный клик «Показать» (`ceShowClick -> ceLinksLoad`). Во вкладке «Смена названий» picker
аккаунтов теперь подхватывает уже выбранный/загруженный общий аккаунт (`ceExactLoginMatch`,
`ceTargetLogins`, `CE.login`), если в rename-picker ещё ничего не введено; строка поиска и кнопка
«Загрузить кампании» выровнены через `cern-account-line`. Cache-bust:
`20260720_renames_account_v1`. Проверено: local+remote `node --check`, `git diff --check`,
Mac↔LXC md5 по JS/CSS/HTML совпал, static JS/CSS отдаются 200 и содержат маркеры, page smoke
`/direct/automation/content?section=renames` → `302 /login`, `direct-content.service` и
`direct-copy.service` active. Browser-control недоступен в этой среде (`agent.browsers.list()=[]`),
поэтому визуальный скрин в браузере не снят.

## Сессия 2026-07-20 — общий Direct write-gate content/copy — ЗАДЕПЛОЕНО

**Контекст:** дневное копирование аккаунта и массовые правки контента
(`/direct/automation/content`: заголовки, тексты, быстрые ссылки, уточнения, ссылки,
изображения, названия) дергают общий Direct API/куки-слой и могли идти параллельно из разных
очередей. Ночную сверку цен оставляем отдельной.

**Сделано:** добавлен `direct/write_gate.py` с lease-таблицей Victory
`public.direct_api_write_locks` и TTL `DIRECT_WRITE_GATE_TTL_SECONDS` (default 4h).
`content_worker.py` теперь перед переводом content-job в `running` берет общий lock по агентству,
пропускает занятое агентство и пробует следующие queued jobs; lock снимается в `finally` и при
ошибках claim. `queue_server.py` сохранил старый `direct_agency_active`, но после него берет тот же
`write_gate` для `copy_campaigns` и create/delete/edit jobs, поэтому content и copy/create больше
не стартуют одновременно на одном агентстве; разные агентства параллелятся. Повторный acquire
того же `job_id` запрещён до истечения lease, чтобы watchdog не запустил второй поток поверх
живого зависшего. Для copy/create/slepki добавлена чистка `direct_api_write_locks` по
`direct_automation_jobs.status NOT IN ('running','claimed','queued')`, чтобы после crash/recover
не ждать TTL 4h.

**Документация:** `CONTENT_EDITOR.md` уточняет новый gate и то, что `direct_price_check_jobs`
ночной сверки цен им не ограничивается.

**Проверено локально:** `py_compile` на Python 3.12 для `write_gate.py`, `content_worker.py`,
`queue_server.py`; `git diff --check` для затронутых файлов; `.venv/bin/python -m pytest`
`direct/tests/test_architecture_boundaries.py direct/tests/test_copy_integration_guards.py` —
`19 passed`.

**Deploy/evidence:** перед рестартом активных очередей не было:
`content_jobs=[]`, `direct_automation_jobs=[]`, `direct_deferred_creates=[]`,
`direct_delayed_repairs=[]`. Mac↔LXC101 sha256 совпал для `write_gate.py`,
`content_worker.py`, `queue_server.py`, `CONTENT_EDITOR.md`, `STATE.md`; remote
`py_compile` OK. 2026-07-20 15:39 +05 перезапущены и active:
`direct-content.service` PID `1313622`, `direct-content-worker.service` PID `1313623`,
`direct-copy.service` PID `1313624`, `direct-create.service` PID `1313625`,
`direct-create-worker.service` PID `1313644`, `direct-slepki-worker.service` PID `1313641`.
Nginx smoke: `/direct/automation/content`, `/direct/automation/copy`,
`/direct/automation` → `302 /login`. `public.direct_api_write_locks` создана; DB-only smoke:
первый acquire `True`, второй по тому же agency `False`, после release снова `True`;
после smoke `write_locks_count=0`. После рестарта активные очереди всё ещё пустые.

**Fix 16:52 +05 (инцидент очереди content/copy):** выяснилось, что per-agency lock недостаточен:
copy держал `agency:victorylotsofads1`, а content-rename шел под `agency:y-direct-victory`,
поэтому копирование и переименование могли одновременно дергать один Direct/Grid слой. Gate
переведён на единый ресурс `direct-write:global`; старые `agency:*` locks учитываются при
acquire и освобождаются при release для мягкого перехода. Smoke на Victory: первый acquire
`True`, второй с другой agency `False`, после release третий `True`; после smoke locks=0.

**Fix 16:52 +05 (архивные названия):** архивные кампании/группы не редактируем и даже не
пытаемся отправлять в Grid. `/renames/campaigns` запрашивает только `States=[ON,OFF,SUSPENDED]`,
`/renames/apply_async` повторно проверяет `campaigns.get(FieldNames=[Id,State])` по выбранным
campaign_ids и отсекает `ARCHIVED`; нижний guard в `GridClient._narrow_bases` пропускает payload
с `_archived_campaign`. Добавлен pytest `test_grid_set_campaign_names_skips_archived_campaign`.

**Fix 16:52 +05 (очередь, колонка пользователь):** copy-задачи теперь пишут `created_by` в
body (`session.username` для UI, `api` для внешнего API), `/direct/api/copy_queue` возвращает
`username`, а общий список `ceQueueLoad()` больше не ставит для copy жесткий `—`. Старые copy rows
пользователя восстановить нельзя: в их `body` нет ни `created_by`, ни `_session_snapshot`.
Mac↔LXC md5 совпал; local+remote `py_compile` OK; `node --check` OK; pytest
`direct/tests/test_routes.py -k 'rename or set_campaign_names or copy'` — `4 passed`.
Перезапущены `direct-copy.service` и `direct-content.service`; все 6 direct write/content
сервисов active, active queues=0, active locks=0.

## Сессия 2026-07-20 — tone-voice low score по всем слепкам — ЗАДЕПЛОЕНО

**Контекст:** `/direct/automation?tab=create` показывал низкий тон-войс не только для
`Гордеева/Монобренд`, а для разных слепков на общем логине `porg-ozge4ntu`.

**Сделано:** `check_tone_of_voice.py` больше не читает весь аккаунт при пустом `campaign_ids`;
ids рекурсивно берутся из result и child jobs (`_resume_of`/`_requeue_of`), а no-id job возвращает
`no content`. В prompt/corpus-фильтре и общих fallback-пулах генерации (`ai_agents.py`,
`create_content.py`, `create_set_assets.py`, `text_gen.py`, `automation_runtime.py`,
`create_set_feed_builders.py`, `create_set_master_product.py`) убраны/фильтруются generic-маркеры
`Кредит от 15 банков`, любой `15 банков`, `Купить новое авто в кредит`, `Новые авто в кредит`,
`Рассрочка`.

**Проверено:** локальный и remote `py_compile` OK; Mac↔LXC101 sha256 совпали по 8 изменённым
Python-файлам; `direct-create.service`, `direct-create-worker.service`,
`tone-of-voice-watcher.service` перезапущены и active. Dry-run `30f980bbb63b` теперь даёт
`no content` с note `не проверяю весь аккаунт`; dry-run `37fd8ad1c62f` проверяет 22 кампании
из child jobs, не весь аккаунт. Отдельный `direct_verifier` после доработок принял правку:
exact generic-маркеры отсутствуют в исполняемых fallback-строках локально и на LXC101.

**Осталось:** старые live-кампании, где generic-тексты уже созданы в Direct, кодовая правка не
переписывает автоматически: пример `37fd8ad1c62f` всё ещё `score 30/100 (mixed)` по фактическому
старому контенту. Для них нужен отдельный repair/пересоздание контента.

## Сессия 2026-07-20 — Direct copy API/HAR review для интеграции — ЗАДЕПЛОЕНО

**Контекст:** проверка сервиса `https://seoadvanced.ru/direct/automation/copy` под внешнюю
интеграцию и HAR `direct.yandex.ru.67har.har` для 1:1 Grid-формы. Живой проблемный кейс —
job `4c0c992cf213`, target `porg-lzjk6p5m`, 7 draft РК с weekly `OPTIMIZE_CLICKS`.

**Сделано:**
- `copy_request.py`: общие парсеры `parse_feed_map`/`parse_image_hashes`; строгий public API
  режим для `geo_region_ids` (`400 INVALID_GEO` на mixed/non-integer при `geo_mode=change`).
- `copy_api.py`: внешний `/api/v1/copy/start` нормализует `feed_map`, `image_hashes`,
  `geo_region_ids` до постановки job и до idempotency hash; `geo_region_ids` при `keep` не
  попадает в job; `diff_count` считает `copy_verify.results`.
- `copy_engine.py`/`copy_feeds.py`: явный `feed_map` для `mode=other` имеет приоритет над
  auto-match; target feed ownership валидируется до skip/preflight/upload; cleanup перенесён
  после source pull/preflight/geo/feed validation, но до upload.
- `copy_verify_source.py`: Direct dict/list shape для `ExcludedSites*` нормализован; пустая
  Grid adaptive-row больше не маскирует v5 `TextAd` fallback.
- `grid_finalize.py`: HAR-backed weekly `OPTIMIZE_CLICKS` пишется как
  `AUTOBUDGET_AVG_CLICK` + `avgBid` + `sum` + `budgetType=WEEKLY`; если Grid read отдаёт
  `avgBid=None`, используется UI-дефолт `100`. `_unsupported_strategy` оставлен только для
  clicks без лимита/avgBid/бюджета.

**API-вызовы/маршруты:** сверены все frontend fetch-вызовы copy-страницы: 14/16 имеют роуты
в `direct-copy.service` (`/direct/api/copy_*`), 2 исключения намеренные и уходят в основной
Direct service (`/direct/api/accounts`, `/direct/api/goal_for_counter`). Public API:
`/api/v1/copy/{start,status,health,campaigns}` зарегистрирован в `copy_main.py`.

**Проверено:** локально `py_compile` OK по изменённым модулям; pytest
`direct/tests/test_copy_integration_guards.py` — `15 passed`; `git diff --check` OK.
Mac↔LXC101 sha256 совпали по `copy_api.py`, `copy_request.py`, `grid_finalize.py`,
`tests/test_copy_integration_guards.py`; remote `py_compile` OK. `direct-copy.service`
перезапущен и active/running (`MainPID=1291432`). nginx smoke:
`/direct/automation/copy` → 302 `/login`; `/api/v1/copy/health` без ключа →
`401 AUTH_FAILED`; OPTIONS `/api/v1/copy/start` → 200. Read-only live probe по 7 weekly
campaign ids показал `unsupported=-`, `strategyName=AUTOBUDGET_AVG_CLICK`, `avgBid=100`,
`sum=300`, `budgetType=WEEKLY`. Отдельные verifier-субагенты приняли правки; найденный
ими `geo_region_ids` defect закрыт.

**Осталось:** live `UpdateCampaigns` на текущие 7 draft РК и/или новый copy-run НЕ запускались.
Перед массовым ремонтом безопасный шаг — controlled write на 1 draft РК, read-after-write
стратегии, затем остальные 6/новый прогон. Remote pytest на LXC не запускается: в `/root/venv`
нет `pytest`.

## Сессия 2026-07-19/20 — content images: Grid archive shortfall — ЗАДЕПЛОЕНО

**Контекст:** job `ce_1809d73d5dd9` на `/direct/automation/content` корректно заменила UAC-картинки,
но Grid `TextAd` выглядел как недозамена: из 450 отправленных обновились 45, старый хэш остался
на 405 объявлениях. Live-retry показал: 361 были в архивных кампаниях, оставшиеся 44 в
`705785854`/`705785910` — ad-level архив (`BannerDefectIds.Gen.CANNOT_UPDATE_ARCHIVED_AD`), а не
дефект STOPPED/товарных кампаний.

**Сделано:** `_rsya_inventory` исключает архивные кампании из write-set; `UpdateTextAds` и
`UpdateAdaptiveTextAds` режутся на чанки `_GRID_MUTATION_CHUNK=50` и пишут полный
`failed_ad_ids`; чистый `CANNOT_UPDATE_ARCHIVED_AD` в `_replace_rsya_images` считается
`ads_archived`, а не `errors`. В `CONTENT_EDITOR.md`, `DOD.md`, `ERRORS_JOURNAL.md` закреплено:
архивные кампании/объявления не изменяем, старый хэш в архиве после замены — ожидаемый остаток.

**Проверено:** локальный `py_compile` OK по `content_images_routes.py`, `grid_finalize.py`,
`tests/test_content_images_transport_split.py`; локальный pytest
`tests/test_content_images_transport_split.py` — `28 passed` (включая mixed
`CANNOT_UPDATE_ARCHIVED_AD` + другая Grid-ошибка: не прячется как архив). Mac↔LXC sha256 совпал
по изменённым коду/тесту/докам; remote `py_compile` OK; `direct-content.service` и
`direct-content-worker.service` active/running после restart (`MainPID=1061196/1061197`).
Remote pytest не запускался: в `/root/venv` нет модуля `pytest`. Отдельный read-only
`direct_verifier` был запущен; найденный им mixed-error риск исправлен и покрыт тестом.

## Сессия 2026-07-19/20 — site-type voice overrides + Павлов/Мультибренд — ЗАДЕПЛОЕНО

**Сделано:** проверены БУ-конфликты по слепкам; общий generation/tone-check путь теперь `site_type`-aware
для всех слепков через `system_for_site()`, `signature_for()`, `filtered_ads_for_site()`,
`filtered_promo_for_site()`. Отдельные overrides добавлены для найденных конфликтных пар:
`gordeeva/С пробегом`, `kuderko/С пробегом`, `tumashenko/С пробегом`; `terehov` без system override,
но promo-фильтр чистит `Гос. поддержка`. Ранее добавленный `pavlov/С пробегом` сохранён.

**Павлов/Мультибренд:** live job `34524e13b18a` со score 40 прочитан: 163/175 заголовков были generic
`Кредит от 15 банков`, 42 `Рассрочка`, 27 `Господдержка`; причина — weak/duplicated
`direct_slepok_content` + generic tp6/tp7 fallback. Добавлен сильный `pavlov/Мультибренд` signature
override и pavlov-specific tp6/tp7 fallback-пулы (`убрали наценку`, выгода в рублях, `одобрение 98%`,
`КАСКО`, `3 платежа`).

**Проверено:** локальный и remote `py_compile` OK по `ai_agents.py`, `create_set_master_product.py`,
`tools/check_tone_of_voice.py`; Mac↔LXC sha совпали по коду и `ERRORS_JOURNAL.md`; remote smoke:
для `gordeeva/kuderko/pavlov/tumashenko/terehov` на `С пробегом` `system_conflict=False`,
`promo_conflict=False`; `pavlov/Мультибренд` override есть в prompt и tone-reference. Сервисы
`direct-create`, `direct-create-worker`, `direct-content`, `direct-content-worker` active/running
после restart. Read-only verifier: PASS, findings нет.

**Ограничение:** новый LLM score не подтверждён новым созданием: старый job показывает старый контент,
а локальный OpenRouter-судья недоступен. Нужен следующий live tone-check после новой генерации.

## Сессия 2026-07-19/20 — copy-service: честный чеклист Проверки + fastlinks/CTA — ЗАДЕПЛОЕНО

**Контекст:** живой copy-прогон `ed2bbb3f67a6` (`porg-mjyh6hjv → porg-lzjk6p5m`, 23 кампании)
в UI показывал зелёную общую проверку, но чеклист содержал `?`/`—`; быстрые ссылки должны
копироваться/проверяться 1в1 независимо от уровня привязки (campaign или ad).

**Фиксы verify/UI:**
- `copy_verify_source.py`/`copy_verify_target.py`/`copy_verify_diff.py`: быстрые ссылки проверяются
  как общий факт + отдельно campaign-level и ad-level count; target v5 читает `TextAd` и
  `DynamicTextAd` `SitelinkSetId`.
- `copy_verify_target.py`: при отсутствии кэша сам дочитывает target `adaptive_ads_for_update` по
  `id_maps["ads"]`, поэтому `Видео` и `Кнопки (CTA)` больше не висят `?`, если Grid реально читается.
- `copy_common.js`: строка `Проверка` в карточке очереди теперь берёт приоритет из детального
`copy_verify` (`mismatch/missing/error` → не зелёная), а не только из `live_verification`.
- `copy_engine.py`: ожидание оседания быстрых ссылок учитывает `DynamicTextAd`, не только `TextAd`.
- `copy_verify_utils.py`/`copy_verify_source.py`/`copy_verify_target.py`/`copy_verify_diff.py`:
  добавлена 1в1-сверка `FeedFilterConditions/feedFilter` для товарных и каталожных объявлений:
  `shopping_filter_count`, `listing_filter_count`, `shopping_filter_signature`,
  `listing_filter_signature`. Target читает сами фильтры через Grid `GdShoppingAd/GdListingAd`,
  v5 остаётся только count fallback.
- `copy_verify_utils.py`/`copy_verify_source.py`/`copy_verify_target.py`/`copy_verify_diff.py`:
  закрыт D10 `audiences`: source и target читают Grid `GdRetargeting.retargetingCondition`
  по группам, `GdGridOfferRetargeting` фида игнорируется, diff сравнивает signatures через
  `id_maps["adgroups"]`. При сбое Grid чтения остаётся fail-safe `UNREADABLE`, а не ложный OK.

**Фиксы будущего copy-пайплайна CTA:**
- `create_set_feeds._grid_update_adaptive_ads/_grid_set_ad_prices`: добавлены флаги
  `apply_combo_button`/`preserve_button` с прежним default для create-set.
- `copy_creative_steps.py`/`copy_price_steps.py`: copy отключает авто-добавление стандартной
  create-set кнопки; source CTA переносится через RMW, href берётся из target-объявления, чтобы
  не тащить source-домен. Если source CTA нет, target CTA не должна добавляться.

**Документация:** `docs/UI_MAP.md` уточнён: при изменении `copy_verify`/postprocess обновлять
`_COPY_CHECKLIST`, `_COPY_CHANGELIST`, `_CV_ITEM_DIM`. `COPY_INDEX.md` обновлён: site monitoring
и minus places сверяются, disabledPlaces копируются 1в1, baseline-описание удалено; D13/D14 теперь
сверяемые при доступном Grid. Пункт UI `Фильтры товарных и каталожных объявлений` теперь покрывает
и количество объявлений, и сигнатуры фильтров D19c/D19d.

**Проверка текущей job `ed2bbb3f67a6` после live-ремонта:** `done`, `created=23/23`, `failed=0`.
Исправлено в целевом аккаунте `porg-lzjk6p5m`: очищены лишние CTA у 86 adaptive ads, добавлены
70 недостающих `ListingAd`, удалены 2 лишних `ShoppingAd`, проставлены 68 непустых
`ShoppingAd.feedFilter` из source. Финальная `copy_verify` записана в БД:
`ok=490, mismatch=0, missing=0, unreadable=0`; `button_cta=23/23 ok`,
`shopping_filter_count=19/19 ok`, `listing_filter_count=19/19 ok`,
`shopping_filter_signature=19/19 ok`, `listing_filter_signature=19/19 ok`.
`audiences=23/23 ok` (в этом прогоне реальных `GdRetargeting`-аудиторий нет с обеих сторон;
оферный ретаргетинг фида не считается аудиторией).

**Deploy/evidence:** LXC101 `/opt/scripts`; remote `py_compile` OK по изменённым Python-модулям,
remote `node --check home/seoadvanced/static/direct/copy_common.js` OK; `direct-copy.service`
active после restart, PID `1061267`. `templates/direct/copy.html` получил cache-bust
`?v=20260720_copy_filter_verify` для обновления `copy_common.js` в браузере. Mac↔LXC sha256 совпал
для изменённых verify/JS/docs/direct_copy/STATE файлов. Post-review guard в `copy_common.js`: строка
создания `created/expected` читает `live_verification` только если он есть.

## Сессия 2026-07-19/20 — Павлов/С пробегом: низкий tone-score из-за site_type mismatch — ЗАДЕПЛОЕНО

**Симптом:** `porg-ozge4ntu` jobs `b90561eebb92`/`58e889fc0c02`/`5f890155b968` (`pavlov`, `С пробегом`)
получали tone-score 35/45/25; контент выглядел шаблонным.
**Корень:** Павлов был описан как new-auto слепок без `С пробегом` в `site_fit`; БУ-генерация запрещала
new-auto лексику, но no-brand подсказка всё ещё говорила «новые авто», а tone-check сравнивал БУ-контент
с общим new-auto эталоном Павлова.
**Фикс:** `ai_agents_data.py` + `ai_agents.py` + `tools/check_tone_of_voice.py` + `tools/tone_baseline.py`:
site-type-aware `signature_for`/`filtered_ads_for_site`, БУ-safe голос Павлова, БУ-продуктовая подсказка,
tone-check строит эталон с `site_type`.
**Проверено:** Mac↔LXC sha256 совпал по 4 файлам; remote `py_compile` OK; remote sanity:
`site_fit_has_bu=True`, `voice_ref_has_override=True`, `positive_new_auto_instruction=False`,
`positive_bu_instruction=True`. Сервисы `direct-create`, `direct-create-worker`, `direct-content`,
`direct-content-worker` active; активная job `34524e13b18a` завершилась `done created=24 failed=0`.
**Ограничение:** новый LLM score не посчитан локально — OpenRouter с Mac недоступен; старые 3 job сейчас
не перечитываются (`v5=0, grid=0`, черновики удалены/недоступны). Нужен следующий live tone-check.

## Сессия 2026-07-19/20 — цель Семёна «живой прогон по всем слепкам до 0 ошибок» — В РАБОТЕ

**Задача:** создавать кампании через сервис на `porg-ozge4ntu` (метрика `109986170`) по слепку pavlov
по всем типам сайта; каждый прогон — снос черновиков → создание → проверка ошибок живьём → если
дефект, чинить КОД (не кампании) → повторять, пока не будет ошибок. Один фид, без CPA-набора.

**Дефекты Семёна на живых черновиках (Павлов/«С пробегом», джоба `9b2e040edf67`) — все закрыты,
подтверждено ЖИВЫМ чтением кабинета (не только отчётом верификатора), 3 прогона до чистоты:**
- якорь `#slN` в Href sitelinks → снимается перед отправкой в Grid/v5, не в сборке (риск дублей href
  проверен живьём — Grid принял одинаковые href, отказа нет);
- лексика «новые авто» на Б/У-сайте (было и в tp6/tp7, и в tp1-tp5 адаптивах, и «Новые {brand}» в
  шаблоне заголовка) — парный `_drop_new_car`/`_NEW_RE`, `site_type`-aware `_title_from_template`;
- домен фида и дубль суффикса в имени (`carsklad-126.site`) — план кладёт `feed_label`, билд
  ПОТРЕБЛЯЕТ его вместо пересчёта (`ecd1ae8`); tp3/tp5-cookie путь отдельно (`b956084`);
- дубль сегмента в имени (`Мастер кампаний - Мастер кампаний`, потом `ТК - Автосалон - ТК -
  Автосалон`) — не частные литералы, а ОБЩИЙ `dedup_name_segments` на склейке `group.name`+`item.t`
  (root-cause: два независимых ярлыка одной позиции пересекаются в 220+ позициях по 11 слепкам);
- **режим таргетинга tp6/tp7 выводился РЕГУЛЯРКОЙ ПО ИМЕНИ позиции** → «МК - Автотаргетинг» терял
  416 ключей / 9 аудиторий молча. Решение Семёна: режим по СОДЕРЖИМОМУ структуры, имя не влияет,
  нет ключей/аудиторий → `---autotargeting` по умолчанию. Задето 249 позиций (не 166 из первой
  диагностики — нашёлся 3-й обрыв: чтение пака без `group=`-слага брало легаси-файл);
- сверка `already_in_direct`-skip была ИНЕРТНА (id пропущенных кампаний не собирались) — оживлена,
  но **первая попытка её оживить открывала ловушку авто-удаления живых кампаний** (`UAC_NOT_DRAFT`
  → `_UAC_REPLACE_CODES` → `delete_uac` без гарда) — закрыто 3 независимых рубежа + report-only;
- транспорт LLM: reasoning-модель (`deepseek-v4-flash`) писала ответ в `delta.reasoning`, код читал
  только `delta.content` → ~50-67% пустых ответов на боевом промпте → тихий откат на статические
  шаблоны (ИСТИННАЯ причина «шаблонности контента», не архитектура — путь уже был LLM-first). Фикс —
  смена дефолтной модели на `deepseek-chat`, НЕ фан-аут на 3×14B (инстансов физически нет на M3,
  `#OFF14B` в `ka_mlx.sh` — задание было ошибочным, исполнитель обоснованно отказался).

**Регресс МЕЖДУ прогонами (важный урок):** параллельная сессия (`b35caf3`, легитимная задача
переименования «Мастер кампаний»→«МК») заодно откатила несвязанную строку (`_strip_dom_plan`) в
том же файле, приняв её за случайно попавший чужой код — домен вернулся в имена tp7. Найдено ТОЛЬКО
живой проверкой кабинета между 2-м и 3-м прогонами (детект-скрипты на плановых/старых данных этого
не ловят). Фикс `b864ee3` точечно вернул срез домена, переименование не тронул.

**Урок процесса:** из 10 отправленных на ревью правок круга — 8 вернулись с `❌` минимум раз,
некоторые по 2-3 круга. Ни одна не прошла с первого раза при этом верификатор джобы ВСЕГДА давал
`0 errors` — зелёный отчёт джобы НЕ означает чистый кабинет, нужна независимая живая проверка.

**Статус на паузу (Семён снял `/goal`):** Павлов/«С пробегом» — 3/3 прогона чисто, ЖИВЬЁМ по 9
пунктам ✅. Павлов/«Мультибренд» — прогон идёт (см. job ниже). Дальше по плану: Павлов/«Монобренд»,
затем остальные 16 слепков по очереди `_order.json`, тем же циклом.

**Открытые вопросы Семёну (не блокируют прогон):** кап 200 ключей из 416 в tp6/7 — поднимать? метка
«Автотаргетинг» в имени при реальном режиме «КС» после фикса режима — переименовывать ~249 живых
кампаний (сломает `already_in_direct`, review-first) или оставить как исторический ярлык? приоритет
«уникальность контента» vs «оценка on-voice» (судья давал 100 баллов за ДОСЛОВНОЕ совпадение с
корпусом — конфликтует с задачей уникализации, не начата).

**Известные некритичные хвосты:** `_count_audiences` в `uac_read.summarize_uac_detail` читает не то
поле payload (`ca_retargeting_condition.goals` вместо `.condition_rules[].goals`) → недосчитывает
интересы МК, на создание/верификатор не влияет; `NO_IMAGES_LIVE` не проверяет tp5 (гейт `tp==1`);
`kryuchkova` Мультибренд/tp1 (204) и «С пробегом»/tp2 (146) — потолок `_v99` суффикса `_uniq`
теоретически исчерпаем на очень больших бакетах (не введено этой сессией, не блокирует).

## Сессия 2026-07-19 — slepki: страница вечно висела на лоадере (SyntaxError) — ЗАДЕПЛОЕНО

**Симптом (жалоба Семёна):** `/direct/automation/slepki` вечно на «Загружаю структуру слепков…».
Это было **не «долго», а насмерть**: не сменялся спиннер.
**Причина:** в `templates/direct/slepki.html`, функция `_revalidateUiStructure`, `const sel` объявлен
ДВАЖДЫ в одной области видимости (стр. 304 и 311) → `SyntaxError: Identifier 'sel' has already been
declared`. Ошибка парсинга роняет ВЕСЬ inline-`<script>` (219–483), а там `ensureUiStructure` → функция
не определяется → boot `ensureUiStructure(false,…).then(initSlepki)` падает ReferenceError → спиннер
никогда не сменяется. **Проскочило потому, что прошлые сессии гоняли `node --check` только на внешнем
`slepki_ui.js`, а не на inline-скрипте Jinja-шаблона.**
**Фикс:** убрал второе `const sel` (`sel` уже объявлен выше в той же функции). Коммит `c1c53b4`.
**Проверено:** `node --check` обоих inline-блоков шаблона = RC 0 (до фикса первый блок падал); md5
Mac==LXC (`ccd1d713…`); `direct-slepki.service` restart → active. HTML отдаётся `no-store` → браузер
берёт свежий сразу, версия-bump не нужен.

## Сессия 2026-07-19 — copy speed-up: selected pull/cache/parallel fallback — ЗАДЕПЛОЕНО

**Сделано:** ускорен `/direct/automation/copy` без изменения бизнес-семантики. `step_keywords`
теперь шлёт v5 `keywords.add` batch=900 вместо 200. `direct_copy.phase_pull()` получил
`selected_campaign_ids`: основной сервис передаёт выбранные кампании сразу в `campaigns.get`, а картинки
для selected-only читаются по использованным `AdImageHashes` вместо всего аккаунта. Старый CLI-вызов
`phase_pull(src_dir, auth, login)` сохранён.

**Параллелизм:** `copy_verify_target` добирает v5 fallback по кампаниям в 2 потока и не смешивает разные
типы кампаний в одном запросе. UAC копируется с fan-out=2: сначала параллельное чтение detail отдельными
`UacReadClient`, затем осторожное создание отдельными target-клиентами.

**Кэш:** в `CopyCtx` добавлен общий `cached_source_edit_rows/cached_target_edit_rows`.
`organic_placement`, `settings_diff`, `disabled_places`, `promos` и финальный verify переиспользуют
`campaigns_edit_rows`; target-кэш инвалидируется после мутаций. `search_invariants` сначала читает
`groups_for_edit` одним batch по списку кампаний, при truncation/error откатывается на прежний
per-campaign режим; записи остались ограниченными per-campaign. Code review перед деплоем поймал риск
частичных `campaigns_edit_rows`: helper теперь дочитывает только missing-id и не выбрасывает уже
полученные строки.

**Проверено:** локально `py_compile` по 10 затронутым py-файлам, `git diff --check` в обоих nested repo,
smoke-тест кэша `_source_edit_rows/_target_edit_rows/_invalidate_target_edit_rows`, включая partial read.
На LXC101 файлы синхронизированы, remote `py_compile` OK, `direct-copy.service` перезапущен и active PID `1031724`,
маркеры изменений найдены на сервере, активных `copy_campaigns` в `public.direct_automation_jobs` нет.
Live-copy не запускался: это реальные мутации кампаний.

**Повторный деплой по команде Семёна:** 2026-07-19 22:31 +05 — те же copy-файлы и `STATE.md`
повторно синхронизированы на LXC101, remote `py_compile` OK, `direct-copy.service` restart,
active PID `1032227`; `/direct/automation/copy` отвечает `302` на login без сессии, активных
`copy_campaigns` нет.

**Фикс UI-счётчика удаления черновиков:** модалка copy показывала только v5 DRAFT (`4`) и не считала
МК/tp7, которые удаляются cookie/Grid-слоем. `copy_cleanup._copy_target_campaigns_info()` теперь
добавляет unseen Grid UAC/tp6/tp7 DRAFT в `draft_count` и отдаёт `v5_draft_count/cookie_draft_count`.
Проверено read-only на `porg-lzjk6p5m`: `total=10`, `draft_count=10`, `v5_draft_count=4`,
`cookie_draft_count=6`, breakdown `OFF/DRAFT=4`, `GRID/DRAFT=6`. Деплой LXC101, remote
`py_compile copy_cleanup.py` OK, `direct-copy.service` active PID `1033058`, активных copy-job нет.

## Сессия 2026-07-19 — copy job 2863fa0ebca3 verify + speed audit — LXC direct-copy RESTART

**Проверено:** job `2863fa0ebca3` (`porg-asfbs7qe → porg-lzjk6p5m`) в Victory DB: `done`, 10/10 создано,
`failed=0`, `elapsed_seconds=637`, cleanup удалил 172 черновика, `keywords=11280 total / 11168 via_v5 /
0 failed / 112 skipped_no_group`. Повторная live `copy_verify` после инициализации `automation_runtime`
(важно: с v5 fallback, Grid-счётчик ключей для свежих черновиков даёт ложные 2792) показала
`ok=44 mismatch=0 missing=0 unreadable=23`; ключи на 4 search-кампаниях 2820→2820. UAC часть:
6/6 кампаний созданы, `uac_copy.errors=[]`. Активных copy-job нет.

**Факт по "долго":** основной прогон занял ~10.6 мин; главный объём — 11168 ключей через v5 (TEXT_AD_GROUP
не шлём через Grid, потому что Grid ложно подтверждает addKeywords), плюс 172 удаления черновиков и
postprocess/verify. Скрин "идёт добивка" был post-done фазой: отложенная reverify стартует через 240с,
а сервис был перезапущен в 21:40:53 +05 до её первого круга. Старый `direct_delayed_repairs`
`9d5c2d534bfd` упал с note `у job нет сохранённого результата для проверки`; это не ошибка копирования,
а лишняя persistent-добивка, для новых чистых copy-job уже пропускается (`copy_no_inplace_repairs`).

**Правка:** `copy_engine._copy_delayed_reverify()` теперь получает и передаёт те же `geo_pairs`, что
использовал основной `copy_postprocess`; раньше delayed verify/repair шёл с `geo_pairs=[]`, и при
позднем keyword mismatch мог долить исходные гео-фразы вместо целевых. Локально и на LXC101:
`py_compile copy_engine.py`, `direct-copy.service` restart, active PID `1023223`; remote-файл перечитан
(`geo_pairs=_geo_pairs` в verify/repair).

## Сессия 2026-07-19 — slepki UI keyword/audience preview — ЗАДЕПЛОЕНО

**Сделано:** поправлена правая панель `/direct/automation/slepki`: `slepki_ui.js` разделяет display-имя строки и `position` для backend-матчинга tp6/tp7; аудитории ищутся по `groups`/`splits.groups` и всем `items`, не только `items[0]`; `slepki_editor.read_group_keywords()` больше не гейтит fallback по regex имени и передаёт `group` в тот же `_tp67_keywords_for`, что использует создание.
**Follow-up fast refresh:** `direct-slepki` JSON теперь отдаётся UTF-8 без `\uXXXX` (`app.json.ensure_ascii=False`, compact=True), `/api/slepki/ui_structure` на LXC уменьшен с 22.7 MB до 11.5 MB; версия API/JS в `slepki.html` bump `20260719_slepki_fastload`, чтобы браузер не держал старый `slepki_ui.js`.
**Follow-up lazy refresh:** `/direct/api/slepki/ui_structure` для отдельной страницы теперь вызывается как `light=1`: отдаёт полный только выбранный слепок + shell-записи остальных для dropdown; при выборе другого слепка UI догружает его отдельно и merge-ит в память. Для light-режима добавлены локальные `ct_segments_cache.json` и `donor_tp4_models_cache.json`, чтобы cold refresh не зависел от Victory DB; JSON API gzip-ится при `Accept-Encoding:gzip`. Замер LXC101: light first selected plain 1.52 MB / ~0.35s, gzip 92 KB / ~0.12s; `pavlov` plain 574 KB / ~0.14s, gzip 29 KB / ~0.10s. До этого страница тянула полный JSON 11.5 MB (ранее 22.7 MB до UTF-8 compact).
**Follow-up light-default:** после жалобы на hard reload в журнале LXC найден реальный старый запрос `...?v=20260719_slepki_fastload` без `light=1`, который мог снова тянуть полный JSON. `direct.slepki_main` теперь default-ит `/direct/api/slepki/ui_structure` в light-режим; full-доступ оставлен только явным `full=1`. Версия HTML/JS bump `20260719_slepki_lightdefault`. LXC smoke: старый `fastload` URL = 1 full-dir, gzip 92 KB; `full=1` = 17 dirs / 11.5 MB.
**Follow-up stuck loader:** скрин 22:36 показал зависание на "Загружаю структуру слепков..." и в journal был только `GET /direct/automation/slepki`, без последующего `/ui_structure` — проблема до/вокруг initial fetch. Первый light payload (`pavlov`) теперь встраивается прямо в HTML (`window.__SLEPKI_INITIAL__`), `ensureUiStructure()` применяет его синхронно без отдельного стартового API. HTML gzip включён: LXC page plain ~613 KB, gzip ~42 KB, API для последующих lazy-switch остаётся gzip ~29 KB. Версия bump `20260719_slepki_inlineinitial`.
**Проверено:** локально `node --check`, `py_compile`, `pytest home/seoadvanced/direct/tests` = 78 passed, `git diff --check`. На LXC101 md5 Mac==LXC для 4 файлов, `direct-slepki.service` restart, active. Flask smoke: страница 200/no-store, helper `_slepkiPositionName` есть, `pavlov/Мультибренд/tp6/мк_общие_запросы` отдаёт 69 ключей как на UI; найдены live-позиции Терехова, которые старый name-gate считал `autotarget/audience`, а новый endpoint отдаёт pack/real_library ключи.
**Проверено для lazy refresh:** локально `py_compile automation_runtime/slepki_main/slepki_store`, `node --check slepki_ui.js`, `pytest direct/tests` = 78 passed, `git diff --check`; LXC101 md5 Mac==LXC для 7 файлов (`automation_runtime.py`, `slepki_main.py`, `slepki_store.py`, `ct_segments_cache.json`, `donor_tp4_models_cache.json`, `slepki.html`, `slepki_ui.js`), `direct-slepki.service` restart, `direct-slepki/direct-slepki-worker/direct-create` active. LXC smoke с env systemd (`NEURO_PACK_MOUNT=/opt/neuro_content_local`, `DIRECT_ROLE=web`): page 200/no-store/lazyload=true; API light/gzip timings выше; `/direct/api/slepki/keywords` smoke 200.
**Ограничение:** реальный браузер Codex недоступен в этой сессии (`agent.browsers.list()=[]`), визуальный click-smoke делался через API/Flask test client, не через открытую вкладку.

## Сессия 2026-07-19 — «Смена изображений» доведена: динамический заголовок, точный логин, фид tp7 — ЗАДЕПЛОЕНО

**Продолжение сессии «Вкладка «Смена изображения»» (см. ниже) по follow-up правкам Семёна:**
- Вкладка переименована «Смена изображения» → «Смена изображений» (сайдбар, заголовок панели, JS).
- Замена изображений расширена на ВСЕ типы кампаний (включая tp2/tp4 поиск), кроме фидовых объявлений
  (`GdShoppingAd`/`GdListingAd`) — по явному исключению Семёна.
- **Отключён `execute_images_forbidden_repair`** — авто-ремонт считал картинки в поисковых кампаниях
  дефектом и вычищал их; Семён подтвердил, что это неверно. Флаг `SEARCH_IMAGES_FORBIDDEN_RULE_ENABLED = False`
  в 4 точках (`repair_media.py`, `repair_gate.py`, `repair_planner.py`, `campaign_spec_audit.py`) —
  код не удалён, обратимо.
- Фикс бага выбора аккаунта: чек-бокс на аккаунте, ушедшем из-под текущего фильтра поиска, становился
  невидимым и неснимаемым (счётчик «Аккаунтов: N» и «выбрано: N» показывали разные множества). Отмеченные-но-отфильтрованные
  аккаунты теперь закреплены сверху списка с бейджем «вне фильтра», чекбокс остаётся кликабельным.
- Левая колонка фильтра кампаний расширена ~2.5× (почти до половины ширины интерфейса), адаптивная
  (`clamp`), не фиксированные 720px.
- UI вкладки картинок собран в одну кнопку «Показать изображения» (было два шага «выбрать → показать»):
  клик грузит ТОЛЬКО инвентарь изображений, не весь контент вкладки — на остальных вкладках (Заголовки/
  Тексты/…) поведение общей кнопки «Показать» не менялось.
- Точный логин в поиске аккаунтов на вкладке изображений теперь переключает цель без клика по чекбоксу,
  если введённая строка **точно** (`===`, не подстрока) совпадает с реальным `login_key` — фикс
  сценария из скриншота (ввели `porg-3h236hpp`, целью оставался ранее отмеченный `porg-bzti5ud7`).
- Динамический заголовок по шаблону «Редактор контента - <раздел>» (H1 + `document.title`) —
  **только внутри Редактора контента** (по решению Семёна, не по всей платформе), меняется при
  переключении вкладки, эмодзи-иконка вкладки вырезается из текста заголовка.
- **Фикс tp7/товарных (ecom) кампаний в UAC PATCH:** `_uac_campaign_patch_payload` стриппинг
  «пустых» полей был завязан на `keywords is None`, что истинно и для ecom-кампаний → терялись
  `feed_id`/`listings_feed_id`/`feed_filters`/`listings_feed_filters`. Фикс — дополнительный гейт
  `not detail.get("ecom")`. Проверено СВЕРКОЙ ПОЛЕЗНОЙ НАГРУЗКИ против HAR-66 (было 35 ключей/4 пропущено
  → стало 39/0 пропущено, значения фида совпадают 1:1) — **живой записью НЕ проверено**, нужен отдельный probe.

**Проверено:** `direct_verifier`/`ui_verifier` по обеим JS-правкам (динамический заголовок, точный
логин) — ✅ принято (структурная сверка регэкспа и логики совпадения, живого скриншота не делали —
чистый JS-дифф без шаблона/CSS). Сервисы `direct-content.service`+`direct-content-worker.service`
перезапущены, оба `active`.

**Не закрыто / решение за Семёном:**
1. Живая проверка замены картинок на товарных (tp7/ecom) кампаниях — фикс есть, probe не делали.
2. `porg-pvrbl7mh`: `inheritableCallouts` CLEAR→INHERIT на объявлении `1915248839254163593` — команда
   восстановления в `ERRORS_JOURNAL.md`, ждёт решения; `organic_search_enabled` null→true на кампании
   `712714472` — вероятно необратимо, только информационно.
3. Комментарий в коде «нет пересечения Grid/UAC путей для TextAd» неточен — на 42 ключах, где хеш
   TextAd-картинки присутствует в UAC `contents`, пишут ОБА транспорта (не портит данные, один и тот
   же файл, но тратит место в двух библиотеках) — правка комментария/дедуп записи не сделаны.
4. `_grid_ads_index` падает `RemoteDisconnected` при чанке 100 на аккаунтах с ~157 кампаниями (задача #11).
5. `legs_reconcile` не покрывает UAC-only ключи (задача #12, низкий приоритет).
6. `routes_content_editor.py` — весь этот раунд правок жил в рабочем дереве вперемешку с параллельным
   рефакторингом pricecheck; закоммичен только вместе с массовым коммитом остальных изменений проекта.

## Сессия 2026-07-19 — copy-service распил + verify cache + UI buttons — LXC direct-copy RESTART

**Сделано:** безопасный code-motion copy-сервиса без смены публичных входов:
- `copy_request.py`: общий `campaign_ids` parser + `geo_mode` default + `other/change` validation для `routes_copy.py` и `copy_api.py`.
- `copy_context.py`: вынесен `CopyCtx`; `copy_steps.py` стал фасадом.
- `copy_*_steps.py`: steps разделены на `asset/keyword/creative/price/settings`.
- `copy_postprocess.py`: вынесен `_copy_cookie_postprocess` и `_copy_timed`, старые имена в `copy_engine.py` оставлены фасадами.
- `copy_grid_unified.py`: вынесен `_copy_grid_unified_campaigns` + локальные geo-helper'ы, старый вход в `copy_engine.py` оставлен фасадом.
- `copy_verify_*.py`: source/target/diff/geo/repair разнесены; `run_copy_verification()` и `run_copy_repair()` остались фасадами в `copy_verify.py`.

**Производительность:** `CopyCtx` получил `cached_adaptive_src/cached_adaptive_tgt`. `step_adaptive_creatives`,
`step_videos` и `copy_verify` используют один source adaptive snapshot. Перед `copy_verify` параллельно
читаются target Grid `counts/edit_rows/invariants/adaptive` отдельными Grid-клиентами; кэши передаются
в `run_copy_verification`. Source Grid assets (`campaign_callouts/promos/sitelinks`) теперь читаются
параллельно с v5 `phase_pull`, но с обязательным `join()` до `_copy_filter_snapshot`.

**UI:** в `copy.html` и `copy_other.html` добавлена кнопка `Что меняется` рядом с `Что проверяется`.
Модалка использует тот же механизм в `static/direct/copy_common.js`. В `docs/UI_MAP.md` добавлен
maintenance-note: `_COPY_CHECKLIST` и `_COPY_CHANGELIST` обновлять вместе с изменениями copy-кода.

**Проверено:** `py_compile` на Python 3.12 для `automation_runtime.py`, `copy_main.py`, всех новых
`copy_*` модулей и `grid_finalize.py`; `node --check static/direct/copy_common.js`; `git diff --check`;
import-smoke фасадов на системном Python с установленными зависимостями; отдельный read-only review агент.
На LXC101 файлы синхронизированы в `/opt/scripts`; `direct-copy.service` перезапущен в 21:13 +05,
активен с новым PID, `/static/direct/copy_common.js` отдаёт `showCopyChanges()` и текст
`Что меняется при копировании`.
Правка follow-up: текст `Что меняется` переведён полностью на русский (`keep/change/baseline` убраны);
`disabledPlaces` больше не baseline-override — шаг копирует площадки источника 1в1 в target, а
`copy_verify` теперь сверяет D18 `minus_places` как обычное source↔target поле.
Правка API/интеграции: public `/api/v1/copy/*` получил строгую валидацию `campaign_ids`
(JSON-массив положительных ID без дублей, max 500), `Idempotency-Key` + payload hash, `status_url`,
машинные `error_code`, DB-fallback `/status` после рестарта, `schema_version/public_status/terminal/
settling/repair_pending` и безопасный `result_summary` вместо сырого `result`. В public API
`target_feed_url` принимает только путь от `/`; абсолютные URL запрещены. CORS preflight разрешает
`Idempotency-Key`. Nginx live `/api/v1/copy/` на LXC101 получил `client_max_body_size 1m` и
`client_body_timeout 30s`; `nginx -t` и reload OK.
Правка UI очереди: в карточке job добавлена раскрываемая вкладка-кнопка `Проверки` рядом с
`Лог копирования`; внутри чеклист из `_COPY_CHECKLIST` с фактами `copy_verify` (ожидает/совпадает/
расхождение/не прочитано/отремонтировано). Модалка `Что проверяется` снова статичный справочник:
значки в ней не меняются от последнего прогона. Промоакции вынесены отдельным пунктом проверки
`Промоакции (созданы до кампаний и привязаны)`; в `Что меняется` указано, что библиотеки минус-слов
и промоакции заранее создаются в целевом аккаунте и потом привязываются к новым кампаниям.
Правка UI-status: `settling` и `repair_pending` теперь имеют разные тексты — `сверка и лечение
ключей` vs `постпроверка и ремонт контента`.
Правка performance/false-wait: `repair_auto.delayed_content_repair_request()` для `copy_campaigns`
больше не планирует persistent `content_repair`, если `inplace_actions=0`; это убирает лишнюю
post-done добивку/ожидание при чистом repair-plan. Внутренний `/direct/api/copy_status/<job_id>`
получил DB-fallback из `direct_automation_jobs`, чтобы карточка очереди могла показать финальные
проверки после рестарта `direct-copy.service`.
Факт по долгому прогону `2863fa0ebca3`: `porg-asfbs7qe→porg-lzjk6p5m`, 10 кампаний, started
21:22:04 +05, DB `done` 21:39:53 +05, `elapsed_seconds=637`, cleanup удалил 172 черновика,
keywords `11280 total / 11168 added / 112 skipped_no_group`, `copy_verify` summary
`ok=48 mismatch=0 unreadable=19`, `copy_repair.repairs=[]`. Скрин “идёт добивка” был post-done
фазой `settling/repair_pending`; `direct_delayed_repairs` для этого job ушёл в `error` с note
`у job нет сохранённого результата для проверки`. Активных `copy_campaigns` после проверки нет;
`direct-copy.service` перезапущен в 21:50 +05 и active.

**Ограничения:** живое копирование через Яндекс/куки не запускалось. `pytest test_routes.py` локально
не завершён: системный Python 3.9 падает на project syntax `dict[...] | None`, а локальный Python 3.12
не имеет `pytest/requests`. Новые `copy_*` файлы распила должны попасть в deploy/commit одним набором
с изменёнными фасадами, иначе сервис упадёт на import.

## Сессия 2026-07-19 — Ревью+фиксы сервиса slepki + массовые правки структуры (6 пунктов) — ЗАДЕПЛОЕНО (частично)

**Ревью (read-only) → 2 раунда fix → 2 раунда verify (`direct_verifier`, оба ✅ после доработки).**

**Код-фиксы (коммиты `d1e09cb`, `f2d9244`/`f94ad43`, `83482ac`/`345eaea` в обоих репо — nested `direct/` + `home/seoadvanced`):**
- Critical: `slepki_store.assemble()` отдавал общий кэш без копии → отклонённая preflight правка уезжала на диск следующей успешной. Фикс: `mutable`-флаг, default = копия.
- `save_assets`: теперь пишет ДЕЛЬТУ per-ct (не union во все ct) — раньше стирал различия (scherbakova 81 набор → 1). Модель: читаем union+карта различий, пишем только add/remove поверх своего набора ct, нетронутый ct не переписывается. Плюс бэкап fail-closed, strict-чтение на записи (нечитаемый файл не считается пустым ct).
- `_slCampSave` (карточка кампании) писал в легаси ct-агрегат, который per-group паки не читают → job ok при нулевом эффекте. Переведён на `minus_shared` (единственный канал, который движок подмешивает всегда).
- Path traversal: `_safe_token` + `validate_scope`, 12 роутов. Fuse-гейт в `kontent_pack._read_lines_opt`.
- Чистка: `staging.json`, `pavlov.json.bak`, `_tmp_*`, эндпоинт `edit_callouts`, 2 worktree.
- Тест `test_slepki_source_manifest.py` починен под per-slepok store (был 3/4, стал 4/4).
- Доки: README/ARCHITECTURE/CLAUDE.md досинхронизированы со слоем :5023+worker и UI (`slepki_ui.js`/`slepki.html`).

**Массовые правки данных (17 part-файлов, review-first: proposal → apply → verify), коммиты `b9f3c47`+`fab730c`+`8a3b4d9` (оба репо):**
1. tp6 «Мастер кампаний»→«МК», tp7 «Товарная кампания»/«Товарка»→«ТК» — везде: title, item.t, group.name, И В КОДЕ (`create_set_plan.py:92 tp_label`).
2. Хвосты имён: срезаны NUM_DASH (108) + PLUS_CHAIN (14). `+ CRM` и буквенные коды (`- DM/CR/TA/ML/OT`) — **оставлены сознательно**: срезка давала 9 коллизий имён, это различители как кузов (Sedan/Cross/Liftback, тоже не тронут).
3. terehov/tp7: удалены 2 тестовых `ml3-test` item. Слепок terehov НЕ удалён целиком (было двоякое чтение задачи — уточнили).
4. Схлопнуто 5 групп дублей по gc+gk (2 chepelev + dmp/avtolajt_bu/sk_krs). ⚠️ 3 из 5 похожи на ДЕФЕКТ ГЕНЕРАЦИИ `gk` (не учитывается гео/ct) — Семён решил схлопнуть, зная это; факты по group/gc/gk зафиксированы в `.claude/sdd/slepki-massfix-applied.md` для будущего расследования, место в коде-генераторе НЕ найдено.
5. Коллизия после переименования: 2 item в salamahin/Мультибренд/tp6 стали одинаковыми (регион был только в имени группы, не в item.t) → дедуп превью `seen_c` (create_set_structure.py) прятал один. Развели по конвенции соседа. На СОЗДАНИЕ не влияло (`_build_name` берёт имя из group.name+oblast, не из item.t) — подтверждено verify.
6. Итог: 14892→14857 items (был путь: -8 первый раунд, +33 переименования во втором не меняли count). Откат 1 правки вне скоупа (`strip_dom_plan` в `create_set_plan.py`, была случайно внесена исполнителем) — вернули к `_is_site_domain_name`.

**Задеплоено:** `direct-slepki.service` + `direct-slepki-worker.service` рестартованы дважды (16:07, 20:36), оба раза md5 Mac==LXC101 сверен ДО рестарта, логи чистые, `assemble()`=17 директологов подтверждён на сервере.

**⚠️ НЕ рестартован `direct-create.service` (:5020, создание РК) — НАМЕРЕННО.** Код `kontent_pack.py` и `create_set_plan.py` (общие с этим процессом) изменён, но живой прогон создания РК НЕ выполнялся. Пока не рестартован — старый код в проде, blast radius ограничен слепками. **Перед рестартом `direct-create` — обязателен пробный прогон создания одной РК** (тестовый аккаунт), проверить что tp1-tp7 создаются, имена/дедуп («ТК - ТК - …» не должно быть) корректны.

**Не верифицировано живьём:** UI-панель «Ассеты» под логином (различия ct видны глазами?); первый реальный `save_assets` с записью в M3 (`_ssh_write_m3_map` ни разу не выполнялась в проде, смотреть `write.callouts.m3_ok`); создание РК после правок kontent_pack/create_set_plan.

**Открыто / следующая сессия:**
- Живой прогон создания РК → затем рестарт `direct-create.service`.
- Расследовать дефект генерации `gk` (3 группы дублей в dmp/avtolajt_bu/sk_krs) — факты в `.claude/sdd/slepki-massfix-applied.md`.
- В дереве ~30 файлов чужой незакоммиченной работы (copy-сервис, content editor) — не наши, не трогали, но занимают `git status`.

**Отчёты:** `.claude/sdd/slepki-review-report.md`, `slepki-fix-report.md`(+`-2.md`), `slepki-massfix-proposal.md`, `slepki-massfix-applied.md`(+`-2.md`), `slepki-docs-drift.md` (в `home/seoadvanced/direct/.claude/sdd/`).

## Сессия 2026-07-19 — UI_MAP / nginx split / copy canonical — ЛОКАЛЬНО

**Сделано:** обновлены `docs/UI_MAP.md` и `deploy/nginx-direct-location.conf`: карта больше не говорит,
что весь JS inline в `index.html`, указывает на `static/direct/automation.js`, фиксирует canonical
copy-страницу `/direct/automation/copy` и полный nginx split для content/copy/slepki/accounts/ai/autorules.
В `static/direct/automation.js` старый `?tab=copy` теперь редиректит на `/direct/automation/copy`;
`copy` убран из SPA-вкладок `/direct/automation`.

**Проверено:** агентами и локально: `node --check home/seoadvanced/static/direct/automation.js`,
сверка локального nginx-шаблона с `nginx -T` на LXC101, `nginx -t` успешен. Продовые listeners
`:5020/:5021/:5022/:5023/:5024/:5026/:5027` и сервисы active. Продовый warning duplicate
`server_name` исправлен: backup `seoadvanced.ru.bak.ar_api_20260717_223349` перенесён из
`sites-enabled` в `sites-available`, `systemctl reload nginx`, повторный `nginx -T` чистый.

## Сессия 2026-07-19 — Вкладка «Смена изображения» (content-editor) — ЗАДЕПЛОЕНО, работает на реальных данных

**Сделано:** новая админская вкладка «Смена изображения» на `/direct/automation/content` — бэкенд
(`content_images_routes.py`: inventory / upload / preview / replace_async, тип задания `image_replace`
в `direct_automation.content_jobs`) + UI (редизайн в две колонки с модалкой). Смок на реальном
аккаунте: 54 карточки, все превью живые, гейт подтверждения перед постановкой в очередь держит.
Документация — `CONTENT_EDITOR.md`, раздел «Вкладка Смена изображения».

**Попутно закрыт класс багов `UAC_FULL_PATCH_REPLACE_DROPS_ASYMMETRIC_KEY`** — шесть потерь при
full PATCH: быстрые ссылки, уточнения, `displayHref`, `customText` кнопки, `relevance_match`,
`ca_retargeting_condition`. Пять подтверждены живой записью с откатом; `button.customText` не
подтверждён — объектов на аккаунтах нет.

**Коммиты:** `b42c5fb`, `ffaec1a`, `884492a`, `1bfb7e0`, `7862eb7`. Часть UI уехала в чужой коммит
`97fa8e1` (перехват параллельным окном).

**Сломано / требует решения:**
1. **БД Victory недоступна С LXC101** — `timeout expired` даже при 30 с; с мака та же БД отвечает
   (52/100 коннектов). Ломает вкладки «Сверка цен», «Очередь», список пользователей в админке;
   вкладка «Смена изображения» не затронута. Нужно внешнее действие: сетевой путь
   LXC101 → `103.88.240.90:5432`.
2. **`porg-pvrbl7mh` — 2 неоткаченных побочных эффекта UAC full PATCH:** объявление
   `1915248839254163593` `inheritableCallouts` `CLEAR`→`INHERIT`; кампания `712714472`
   `organic_search_enabled` `null`→`true` (вероятно необратимо). Команда восстановления — в
   `ERRORS_JOURNAL.md`.
3. **`routes_content_editor.py` не закоммичен** — наши UAC-правки перемешаны с чужим рефакторингом
   pricecheck.

**Осталось:** подпись `thumb` вместо имени картинки (в работе); `RemoteDisconnected` при чанке
100 кампаний; `legs_reconcile` не покрывает UAC-only ключи; независимой проверки редизайна не было.

## Сессия 2026-07-19 — Probe 3 «Смена изображения» на porg-pvrbl7mh — ЖИВАЯ ЗАПИСЬ, ОТКАЧЕНО

**Санкция Семёна на запись. Мутаций 4** (2 Grid `UpdateAdaptiveTextAds` + 2 UAC full PATCH),
все откачены, оба снимка ДО совпали побайтово. Прод-код не менялся; прод-путь `run_image_replace`.
**Закрыто живьём 4 из 6 полей:** `inheritableCallouts` OVERRIDE+`calloutIds`-СПИСОК (главное — форма
держалась только на интроспекции, теперь отправлена и принята: 200, `validationResult:null`),
`displayHref`/`linkTail`, `bannerPrice`/`adPrice`, `ca_retargeting_condition` (+бонусом
`relevance_match` и `inheritableSitelinkSet` — снят блокер probe 2).
**НЕ закрыто, объектов на аккаунте НЕТ:** `button.customText` (button пуст у 0/2794 адаптивных),
`creativeIds` на Grid-пути (видео у 1/2794, и оно в UAC-владеемой кампании → `update_ad_images`
по нему не вызывается никогда; сохранность видео доказана только через UAC PATCH).
**Находки:** (1) UAC full PATCH меняет ПОРЯДОК `device_types` при идентичном множестве —
сравнивать как множество, иначе ложный mismatch; (2) probe оставил 2 осиротевшие картинки в
библиотеке (удаление = лишняя мутация + баллы, решение за Семёном).
ERRORS_JOURNAL: `GRID_RMW_AD_ASSETS_WIPED` и UAC-дополнение переведены 🟡 → ✅ подтверждено живьём.
Отчёт: `.claude/sdd/probe3-pvrbl7mh-report.md`, снимки `probe3-pvrbl7mh-{before,uac-before}.json`.

## Сессия 2026-07-19 — Зонд UAC-замены картинки (708193487) — BLOCKED, мутаций 0

**Сделано:** пред-полётная сверка прод-билдера `_uac_campaign_patch_payload` с браузерным эталоном
`_har/UAC_image_replace.json` (HAR той же кампании) на реальном detail из снимка ДО. Ключей 33 vs 33,
но состав разный → сработало стоп-условие задачи, PATCH НЕ отправлялся, аккаунт равен снимку ДО.
**Найдено:** `relevance_match` — риск подтверждён как материальный (кампания `MK_AT` с active-автотаргетингом,
full PATCH обнулил бы его; write-форма в HAR вырезана, вывести нельзя); `ecom` — НОВЫЙ пропущенный ключ
(попается веткой `keywords is None`, хотя браузер шлёт). Деривация `content_ids` зелёная (5/5, позиция 5).
**Осталось:** решение Семёна — неурезанный HAR для `relevance_match` ЛИБО зонд на расходной МК ЛИБО
санкция на партиал-only путь (правка `_uac_patch_campaign_texts`, вне рамок «probe без правок»).
Коммит `abb3fcf` (только ERRORS_JOURNAL). Отчёт: `.claude/sdd/probe-porg-gcegsszl-report.md`.

## Сессия 2026-07-18 (3) — Копир кабинетов: verify до 117/117 зелёных + само-лечение — ЗАДЕПЛОЕНО

**Задача Семёна:** имитация копирования `porg-psm5h7q6 → porg-lzjk6p5m` (метрика 110106702, город→Красноярск,
delete_drafts) через `direct-copy.service` :5022; копировать до нуля ошибок, все verify-чекбоксы зелёные.
**ВЫПОЛНЕНО: run 20 (job `75f1f3e50ce1`) = ok 117/117, mismatch 0.** Живьём подтверждено: 2 ex-флейк
кампании 712881877=2677 real, 712881880=8600 ключей (= источнику).

**Исправлено 5 дефектов** (см. ERRORS_JOURNAL, сигнатуры ниже; каждый проверен фактом/live):
- **promo CSRF-cold** (`promo.py`, `ac68625`): первый grid-`add` на свежем UacClient уходил без x-csrf-token
  → «тихий null» (ни id, ни ошибок); промо самой массовой кампании всегда падало (12 расхождений). Фикс:
  `_ensure_csrf()` прогрев + retry-on-empty. НЕ про RUB/amount (опроверг live).
- **callout union-over-add** (`copy_steps.py`, `deeb10b`): кампания с 0 уточнений у источника ошибочно
  получала union из 8. union теперь только глобальный фолбэк при пустом файле связи.
- **routing v5-кросс-чек** (`copy_engine.py`, `f4d9b05`): grid-typename флейкует («13 GdUnifiedCampaign» на
  реально TEXT_CAMPAIGN) → битый CopyCamp-снапшот (EOF@305) → падение ПОСЛЕ delete_drafts. Гейт по v5-Type.
- **keyword self-heal + sitelinks-retry** (`copy_verify.py`+`copy_engine.py`, `874dff7`): 2 крупные tp2/Поиск
  кампании под-копировались (ключи не оседали, v5 add вернул truthy Id, failed=0). auto_repair не имел
  ремонтёра keyword_count/sitelinks → repairs=0. Добавлен `_repair_keywords` (live keywords.get vs source,
  дозалив ≤900 батч) + идемпотентный sitelinks-retry. В run 20 sitelinks-retry создал 3 набора, привязал 12.
  ⚠️ Диагноз агента «v5 keywords.add фантомит на поиске» ОПРОВЕРГНУТ live: add оседает; лимит API 1000/запрос.

### ⚠️ Продолжение (2026-07-19): «117/117» была МАСКОЙ — живая 1:1-проверка вскрыла вырезание ключей

Семён потребовал **живую** сверку 1:1 (не verify). Вскрылось: verify показывал зелёное, а живой v5-счёт —
недокоп. Раскрутка (все проверено фактом):
- **verify НЕ врёт**: keyword_count читает из v5 авторитетно (copy_verify.py:466 «добираем ВСЕ кампании»).
  На момент сверки ключи РЕАЛЬНО были (v5=source), **вырезаны ПОЗЖЕ**.
- **Механика**: ключи, залитые в ОКНО СОЗДАНИЯ кампании, Яндекс частично вырезает через ~10-20 мин.
  Ре-add в ОСЕВШУЮ кампанию **держится** (монитор 819: 2705 стабильно 20 мин). Размер решает: маленькие
  оседают, очень крупные (>~6k ключей) — нет.
- **Фикс `d527832`**: `_copy_delayed_reverify` после осевшей сверки гоняет цикл `repair→пауза→re-verify`
  до устойчивого 1:1; выход по 2 чистым кругам подряд И после `_COPY_HEAL_MIN_SEC`=20 мин (иначе ложная
  сходимость до позднего вырезания). + откат keyword-батча 900→200 (900 усугублял вырезание).
- **Результат (run 23, живьём)**: **11/13 кампаний — точное 1:1**. Две мега-кампании (src 9958→live 5759,
  src 8600→live 1009) НЕ добиваются: часть ключей вообще без target-группы (баг маппинга групп на крупных),
  остаток мгновенно вырезается даже при ручном ре-add. **Похоже на потолок Яндекса на большие черновики-наборы**
  (задело бы и реального клиента). Развилка A(зафиксировать 11/13)/B(расследовать мега) — Семён выбрал A: документируем.

**#23 скорость (измерено `bbdcffe` тайминг фаз):** копия ~32 мин, хоги — keywords 598с и videos 392с.
Сделано: **параллельный prefetch видео `177f0f8`** (скачивание 392→18с). keyword-батч 200→900 давал ~3× на
ключах, но усугублял вырезание → откачен. Итого чистый выигрыш скорости пока скромный (видео upload доминирует,
не download). НЕ сделано (план Части B): пайплайн create→verify→repair внахлёст (идея Семёна), API-путь ‖
cookie-путь для смешанных аккаунтов, grid-постпроцесс в параллель, параллельные keyword-батчи, видео-upload.

**Коммиты сессии:** `ac68625 deeb10b f4d9b05 874dff7 db3eb1e bbdcffe 177f0f8 d527832`.

## Сессия 2026-07-18 (2) — Создание РК: цикл по всем типам сайта pavlov + перенос проверок — ЗАДЕПЛОЕНО

**Задача Семёна:** гонять создание по слепку pavlov на `porg-ozge4ntu` (счётчик 109986170) по ВСЕМ типам
сайта, между прогонами чистить черновики, чинить код по ошибкам. Критерии: (1) нет ошибок создания/добивки;
(2) время ≤ кампаний × 1.5 мин. **ОБА ВЫПОЛНЕНЫ.**

| Тип сайта | job_id | Кампаний | Упало | Время | Норматив |
|---|---|---|---|---|---|
| Мультибренд | `8efe8b835ac6` | 32/32 | 0 | 31.2 мин | 48 |
| Монобренд | `758b62b6f979` | 27/27 | 0 | 21.8 мин | 40.5 |
| С пробегом | `12a86a597c27` | 20/20 | 0 | 15.5 мин | 30 |

Монобренд и «С пробегом» прошли С ПЕРВОГО прогона → фиксы не подгонка под Мультибренд.

**Исправлено 7 дефектов** (все найдены на живых прогонах, каждый проверен direct_verifier до деплоя):
- `slepok_qa_run.py`: цель Метрики была ЗАШИТА константой (579905467 от чужого аккаунта) → падало 32/32.
  Теперь резолвится по счётчику (`_goal_vse_formy`). Добавлена опция `--site-type` (без неё нельзя гнать по одному типу).
- **Регрессия фильтра моделей** (`text_gen.py`, `create_set_text_builders.py`, `create_set_tp1_builders.py`):
  фильтр «чужих моделей» строился по ct-уровневому имени, а группы стали per-adgroup → для `lada_granta_liftback`
  дискриминатор «лифтбек» выбрасывал ВСЕ ключи. Анти-пустой гейт обманывался спецключом `---autotargeting`
  (при заливке он пропускается). Замер: пустых групп 16 → 6 → 2. Коммит `4c75cfb`.
- **Ретраи транзиентов** (`yandex_gateway.py`): не было вообще, маркеры только англоязычные («Сервис временно
  недоступен» не подпадал). Доработка `ae42f92`: `add`-методы НЕ ретраятся после ОБРЫВА СВЯЗИ (риск дублей —
  сервер мог применить запрос), Direct-returned error ретраится для любого метода.
- **city_morph «Москвич»** (`6017114`): стем `москв` съедал марку → группы Москвича пустые. Матч теперь на уровне
  слова (`_NON_CITY_STEMS`). Регресс-проверка: 6 слепков × 5 городов, ~410k ключей, дельта только москвич.
- **Ложный `NO_ADPRICE_LIVE`** на товарке-only: гейт опирался на ключи `shopping_ads`/`listing_ads`, которых
  в live-counts НЕТ. Заменён на `adaptive_total == 0`.
- Врущая диагностика: `image_groups` считал item'ы с пустыми `image_hashes`; `images_uploaded` не инкрементился.

**Перенос проверок из копирования** (`11ab7b6` + `3c526d3`, обе проверки ✅) — 0 новых запросов/баллов
(данные уже приходили в `CampaignsEditData`, `read_campaign_invariants` их выбрасывал):
- `CALLOUTS_MISSING_LIVE` (tp1–tp5, tp6/7 не флагаются), `SITELINK_SET_MISSING_LIVE`, все report-only.
- `PROMO_MISSING` оживлён **ДВУХСТУПЕНЧАТО** (требование Семёна): ступень 1 = библиотека аккаунта
  (`bool(promos_all)` из уже сделанного v5 `promotions.get` в `attach_or_create_promo`, проброс через 4 слоя),
  ступень 2 = наличие в кампаниях. Аккаунт без промо → не флагается вообще.
  ⚠️ Первая версия была ПРОКСИ по набору и НЕ ловила главный сценарий 0/N — поймал проверяющий.

**Живая верификация новых кодов — ЗАКРЫТА фактом** (контрольный прогон `633798f99dba`, PASS 20/20):
прямое чтение кампании 712878108 → `campaign_assets_read=True`, `callout_ids`=8, `sitelink_set_id='1494624974'`,
`promo_extension_id='1914982'`. Проверки читают поля реально; молчат потому, что ассеты на месте.

**Осталось / решения Семёна:**
- Контент-пак pavlov неполон: 106 ct без картинок (у марок только Dongfeng, MG), `pavlov__jac_общее.txt` пуст.
  Решение Семёна: **в критерий приёмки НЕ засчитывать**, дособор — отдельная задача.
- Гео-фильтр не ловит «московский»/«подмосковье» (стем `москв` их не матчит) — пред-существующий пробел, в журнале.
- Чужой коммит `8326890` (`copy_engine.py:192`, параллельная сессия): убран фолбэк `or src_domain` → при
  отсутствии `_copy_source_domain` в body домен пустой. Не проверялось, не наша ветка.
- `enrich_errors` при Grid-чтении: `shopping_bodies: Validation error (FieldUndefined@[client/ads/rowset/bodies])`
  — существующая ошибка запроса, на новые проверки не влияет.

Ledger сессии: `.claude/sdd/progress.md`. Отчёты: `.claude/sdd/create-massfix-4-report.md`,
`city-morph-moscvich-report.md`, `verify-transfer-report.md`.

## Сессия 2026-07-18 — Сервис копирования: verify+repair+API+live-имитация — ЗАДЕПЛОЕНО

**Задеплоено на direct-copy.service (:5022) + закоммичено.** Коммиты: home `ecd7aa7`, work/slepki `2091729` (ветка `fix/direct-copy-negatives-images-20260717`).

- **copy_verify.py** (NEW): сверка источник↔цель 19 измерений (report-only) + 2 гео-измерения. `snapshot_transformed` различает ЕПК (замещённый снапшот → реальные метрики) и v5 (сырой → EXCLUDED, не ложный mismatch).
- **Авто-ремонт #12** (`run_copy_repair` в copy_verify.py, вшит в `_copy_cookie_postprocess`): D3 библиотеки минус-слов + D19 товарные фильтры — только ADD, идемпотентно, баллы агентства, best-effort. D10 аудитории/D14 кнопки нечинимы (нет writer у Директа: interest_ids 403, мутатора кнопки нет).
- **UI-отчёт #12** (copy_common.js/css): живой отчёт verify+repair на карточке джобы + аннотация кнопки-чеклиста. Читает `result.cookie_postprocess.copy_verify`.
- **copy_api.py** (NEW): программный API `/api/v1/copy/{start,status,campaigns}` — X-API-Key fail-closed (hmac), SSRF-guard, CORS-whitelist, geo_region_ids-валидация. Ключ `COPY_API_KEY` в `.secret/.env` на LXC101.
- **Гео #18**: инлайн-замена ключей ЕПК + нормализация тире `_REGION_ALIASES`. Проверено direct_verifier ✅.
- **Фид #16**: source нет в цели → replace/upload/skip; `copy_feed_upload.py` со сменой домена.
- **2 блокера копирования исправлены** (work/slepki `direct_copy.py`): (1) `negativekeywordsharedsets.get` FieldNames убран невалидный `"Type"` (API 8000, блокировал весь копир — также в copy_login.py, pull_directologist.py); (2) `NegativeKeywordSharedSetIds` в campaigns.add/adgroups.add обёрнут в `{"Items":[...]}` (сырой массив → 8000; давал 8/15 кампаний вместо 15/15).

**Live-имитация porg-psm5h7q6→porg-lzjk6p5m (Красноярск, delete_drafts), 4 прогона:**
- ✅ **15/15 кампаний созданы**, спот-проверка v5 подтвердила контент на цели: tp1/tp2/tp5 по 27-28 групп, 10-63 объявл, 2705-3279 ключей. Гео→Красноярск #11309, adPrice 15/15 из target-фида, метрика counter/goal, ключи 38424 через Grid (0 баллов), картинки ремаплено 118, удаление ТОЛЬКО черновиков.
- ⚠️ **ОСТАЛОСЬ 2 глубоких (по 1 неуспешной попытке фикса — НЕ добиты, не re-run вслепую):**
  1. **verify частично ненадёжен для свежих черновиков** — ЧАСТИЧНО ПОЧИНЕНО: v5-фолбэк в build_target_profile (copy_verify.py:453+) теперь триггерит на 0 КОНТЕНТА (не 0 групп) + пагинация → прогон 5: ok 36→43, **adgroup_count все 13 OK**, keyword_count 7 OK, unreadable по ключам исчез. ОСТАЛОСЬ (хвост): 6 kw-mismatch у кампаний с частичным Grid-stat-счётчиком (фолбэк для них не триггерился — Grid дал ненулевой счётчик); callouts/sitelinks/images verify через v5 НЕ добирает → нужно per-dim v5-добор (adextensions.get, adimages). Спот-проверка v5 подтверждает контент на цели — mismatch ложные.
  2. **Промо не привязываются — ROOT-CAUSE НАЙДЕН (фикс = реструктуризация #24):** промо СОЗДАЮТСЯ через grid addPromoExtensions (10 шт, ОК), но `step_attach_promos` (copy_steps.py:487) падает в `fallback_single` из-за **ID mismatch**: v5-путь (copy_engine.py:2223) ключует `maps["promotions"]` по `promotions.json.Id`, а `campaign_promos.json` пишется по `promoExtension.id` из `campaigns_edit_rows` → by_promo пуст → фолбэк (при 10 промо не привязывает). Прогон 7 подтвердил: `campaign_promos.json заполнен из source edit_rows (13)` НО `исходная связь недоступна`. **ФИКС:** реструктурировать v5-промо-блок как UAC-блок (copy_engine.py:1640) — создавать промо ИЗ edit_rows-defs, тогда maps ключуется по promoExtension.id и совпадает. Half-fix (только запись campaign_promos.json) откачен — инертен.
  3-septies. **ФИНАЛЬНЫЙ РАЗБОР остатка 32 (после run 15, ok 85/117):**
     - **keyword ROOT-CAUSE (direct_copywriter + проверка деплоя):** РЕАЛЬНЫЙ баг — Grid `addKeywords`
       для tp2/поиск TextAdGroup-групп (v5 adgroups.add, НЕ ЕПК) рапортует успех (non-empty addedItems,
       `n_added==len(rows_b)`) но **НЕ персистит ключи**. `n_added` фикс (copy_steps.py:919, из прошлой
       сессии) ПРИСУТСТВУЕТ и задеплоен, но НЕДОСТАТОЧЕН — run-15 с ним via_v5=0 (Grid не отдаёт `[]`,
       отдаёт ложный success). tp1-РСЯ-группы Grid принимает (мигрированы в ЕПК?). **ФИКС #24:** роутить
       ключи поиск/TextAdGroup-групп сразу в v5 keywords.add (Grid для них не работает) ИЛИ post-add
       re-read верификация в step_keywords. Отчёт: `.claude/sdd/copy-search-keywords-report.md`. Глубокая
       правка + тесты (РСЯ-укладка рабочая, риск регрессии) — НЕ трогать без валидации.
     - **keyword МЕХАНИКА (трассировка):** group-mapping КОРРЕКТЕН (28/28 групп→кампания 712878852), но
       каждая target-группа получила **ровно 1 ключ** (source ~96/группа). Имена — **МК-формат
       `ct00NN_aon_..._ct001_ag011`** (Master Campaign tp6), хотя source помечена tp2-Поиск. → либо
       МК-семантика (1 keyword-set/группа = target верен, source-счёт несопоставим — тогда это verify-баг
       сравнения), либо dedup гео-вариаций. **Нужна доменная экспертиза слепка (direct_slepki_master)** —
       слепая правка step_keywords сломает рабочую РСЯ-укладку. НЕ трогать без понимания МК-структуры.
     - **keyword(6)+sitelinks(2): РЕАЛЬНЫЙ баг** — ПОИСКОВЫЕ кампании (tp2/tp5) получают ~0 ключей
       (Grid И v5 читают 0, keywords_read=true → не read-баг, реально не легли), при том что step_keywords
       рапортует via_grid 38424/548 без группы. Ключи ушли не в те группы → **баг group-matching поисковых
       кампаний в step_keywords** (copy_steps.py). РСЯ (tp1) ключи получают норм. Требует копи-энджин фикса.
     - **callout(12): verify-limitation** — 50 callout-расширений associated на цели (привязаны), но v5
       (ads AdExtensions=0, campaign CalloutIds/inheritableCallouts=0) не читает Grid-привязку per-campaign.
       Копия корректна; verify нужно читать Grid-путём копира.
     - **promo(12): ВНЕШНИЙ ЕРИР-БАРЬЕР** — grid addPromoExtensions создаёт 1 из 2 промо (ЕРИР блокирует),
       attach 1/13. **Не код-фиксимо** — внешняя механика Яндекса. → литеральный «ok:117» недостижим,
       пока ЕРИР блокирует создание промо (нужно внешнее действие/обход на стороне Директа).
  3-sexies. **ИТОГ RUN 15 (pristine + все фиксы): ok 42→85/117, копия ПОЛНАЯ (15/15, все типы ad).**
     Зазеленели после фиксов: keyword/adgroup/adaptive_titles/bodies/ads_with_images/sitelinks(11)/
     shared_set. **Остаток 32 — генуинно-тяжёлый хвост:** (1) **callout(12)** — контент ЕСТЬ (50 callout-
     расширений associated на цели), но verify Grid-read (edit_rows.inheritableCallouts=0) не мапит
     per-campaign Grid-привязку → verify-read-limitation, копия корректна; (2) **promo(12)** — РЕАЛЬНЫЙ
     пробел: grid addPromoExtensions создаёт только 1 из 2 промо (ЕРИР-область), attach 1/13; (3)
     **keyword(6)** — вариативность/остаток «без группы»; (4) **sitelinks(2)** — UAC/товарка легитимно без
     sitelinks. → #24. Копия-деливерабл КОРРЕКТНА; хвост = verify-read Grid-extension + ЕРИР-промо + UAC.
  3-quinque. **RUN 14-15: имитация вскрыла 2 РЕАЛЬНЫХ бага копии (verify верно ловил, НЕ eventual-consistency):**
     из `_upload_log.json`: (1) **LISTING_AD 0/27** — `FeedFilterConditions` передавался `{"Items":[...]}`,
     а `ads.add` ждёт МАССИВ (8000 «должен содержать массив»). Фикс direct_copy.py:1454 — разворот `.Items`
     (как copy_engine shopping-путь). (2) **TEXT_AD 9/27** — после гео-замены Красноярск (длиннее Кемерово)
     Title>56 → `ads.add` 5001 → объявление не создаётся. Фикс direct_copy.py:1331 — усечение Title/Title2/
     Text к 56/30/81. Также source_grid пересобирается в delayed re-verify (иначе fallback недочитывает
     адаптивы источника). Валидация — run 15 (pristine аккаунт: удалены кампании + картинки). Промо/callout
     остаются Grid-extension verify-чтениями (#24). **Ключевой урок: gsheet-адаптивы/LISTING_AD теряются
     тихо — imitation обязательна для полноты.**
  3-quater. **RUN 14 (чистый аккаунт + все фиксы) — ПРОРЫВ: ok 35→59, keyword_count ВСЕ 13 OK.**
     Найдены и устранены 3 корня: (а) **split-регрессия** `_norm_region_alias_key` (run 13 падал —
     перенёс в copy_geo + ре-экспорт); (б) **verify batch-4001** read-баг (ads/keywords.get на смешанных
     типах TextCampaign+UAC → 4001 → 0) — починен per-campaign; (в) **тест-артефакт: 1421 орфанная
     картинка** на цели (лимит аккаунта ~1000, накоплено 12 прогонами) → каскад на text-ad/images —
     **АККАУНТ ОЧИЩЕН (1421 удалено)**. Итог run 14: 15/15, keyword/adgroup зелёные, adPrice 132/132,
     адаптивы 260/260. Остаток 58 mismatch — verify-read-хвост (копия полная): (1) SOURCE read-баги
     ads_with_images/shared_set (src читается 0); (2) Grid-extension чтения promo(12)/callout(12)
     (verify не читает Grid-привязки через v5); (3) adaptive_titles семантика (src27→tgt9); (4) sitelinks
     2 (UAC/товарка легитимно). → #24 (scoped verify-хардининг, копия уже корректна).
  3-tris. **⚠️ КОРРЕКТИРОВКА (важно): гипотеза eventual-consistency ОПРОВЕРГНУТА.** Красные
     чекбоксы — НЕ гонка индексации, а: (1) **batch-4001 read-баги verify** — `ads.get`/`keywords.get`
     с батчем всех 13 CampaignIds на смешанных типах (TextCampaign+UAC/товарка) даёт API 4001 → 0
     → ложный mismatch. **ПОЧИНЕНО per-campaign** (коммиты после 0acb23e): на осевшей run-12 ok 35→52,
     sitelinks mismatch 12→2. (2) grid-extension чтения promo(13)/callout(12) — verify их v5 не читает
     (Grid-управляемые). (3) семантика adaptive_titles (src27→tgt9). (4) **реальные пробелы копии:**
     часть кампаний run-12 получила 0 ключей (camp 712876411: 0 keywords обоими v5-методами при 27
     группах) — Grid-first ключи не легли для части кампаний, ОТДЕЛЬНЫЙ баг копии. Остаток → #24.
  3-bis. **(прогон 11-12): копия по большинству кампаний корректна + адаптивный re-verify построен.**
     Спот-проверка v5 run-11: sitelinks НА цели — camp 712876027=63 объявл/**9 sitelinks**, camp
     712876029=218 объявл/**46 sitelinks**. Контент оседает **5-10+ мин ПОСЛЕ done** (dcr-демон
     `direct-create-worker` + async-индексация). Re-verify +300с (run 11) их не увидел, спот-проверка
     позже = увидел → построен **АДАПТИВНЫЙ отложенный re-verify** (copy_engine.py `_copy_delayed_reverify`
     + `_copy_target_sitelinks_ready`): поллит цель до появления sitelinks (до 15 мин), затем гонит
     полную copy_verify и перезаписывает результат job'а (UI читает осевшее). Механизм пере-записи
     подтверждён (run 11: `copy_verify (осевший, +300s)` отработал). Валидация адаптивной версии — run 12.
  3. **verify «не все зелёные» — КОРЕНЬ ЖЕЛЕЗОБЕТОННО ДОКАЗАН (10 прогонов, 2 settle-wait):** in-job settle-wait 150с (прогон 9) И 240с (прогон 10) → ОБА `sitelinks у 0 камп`, а спот-проверка пост-джоб = 9. Причина: dcr-демон привязывает контент через **`run_after_seconds:180` ПОСЛЕ статуса `done`**, а verify бежит ДО `done` → сколько ни жди В джобе, dcr ещё не стартовал. **In-job verify архитектурно НЕ МОЖЕТ увидеть dcr-контент.** Ниже — детали (совпадает): `_copy_cookie_postprocess` (v5-путь) НЕ привязывает sitelinks в джобе — их доливает **отложенный демон `delayed_content_repair`** (`_delayed_repair_daemon_loop`, `run_at` ПОСЛЕ джобы, automation_runtime.py:378, `_run_delayed_content_repair`). verify бежит В джобе → структурно НЕ видит dcr-привязки. Прогон 9: ждали 150с — sitelinks `у 0 камп` (контента ещё нет), спот-проверка через минуты = 9 (демон отработал). **In-job settle-wait откачен как бесполезный.** ФИКС (#24): вызвать `run_copy_verification` в конце `_run_delayed_content_repair` (после демона) + записать в job. Ad-level v5-добор sitelinks/images корректен (f880ec3), просто на in-job-моменте контента нет. Промо: привязка per_source_link починена (f880ec3), verify её на своём моменте тоже не видит. ok застрял на 43 через 8 прогонов. Реальные пробелы поверх (не dcr): images 9/27 (лимит картинок аккаунта — тест-артефакт от прогонов), ~548 ключей «без группы». **Копия корректна — спот-проверка v5: 15/15, группы/ключи/объявления/sitelinks/промо на цели.**
- **#19 монолит — СДЕЛАНО (коммит 7afbfc7):** copy_engine.py **3343→1659 строк**, byte-identical AST-распил на 10 модулей (copy_jobs/geo/snapshot/images/metrika/feeds/grid_read/uac/cleanup/grid_steps) через `direct-copy-engine-refactor/dev/split_tool.py`. DI-фан-аут: `configure()` раздаёт deps суб-модулям (у каждого свой `globals().update`). Гиганты (_copy_cookie_postprocess 447, _copy_grid_unified_campaigns 470, _copy_run_job 348 + delayed re-verify) оставлены в hub — высокая DI-связность / затрагивают новый код, отдельным заходом. Проверено: импорт через automation_runtime + DI всех 10 модулей True + сервис active, page 302/api 401, ошибок импорта нет.
- **#23 перф — НЕ начато:** замер postprocess+verify ~7мин, upload ~6мин, ключи ~5мин, pull ~2-3мин из ~19мин; план — параллельные подзагрузки Grid (rate-limit-aware), отдельным заходом.
- **#24 verify-after-settle — частично (адаптивный re-verify построен, коммит 0acb23e):** остаётся cron-проход через 15-30 мин (Яндекс осел) для честно-зелёных чекбоксов + чинить 548 ключей без группы + лимит картинок аккаунта (тест-артефакт от 12 прогонов).

## Сессия 2026-07-18 — Гео-замена ключей ЕПК + честность verify-метрик — ЗАДЕПЛОЕНО (в составе сессии выше)

- **Что сделано:** 3 блока правок в `copy_engine.py` + hint-обновление `copy_verify.py`.
  1. **Minor dash**: `_norm_region_alias_key` + `_REGION_ALIASES_NORM` — нормализация en/em/figure дашей в lookup ХМАО. Источник: API может вернуть en-dash U+2013 вместо em-dash U+2014 из dict-ключа.
  2. **Snapshot обогащение**: `snap_keywords_json` аккумулятор + `NegativeKeywords` в `snap_adgroups_json` (уже гео-заменённые/отфильтрованные) + запись `keywords.json` в синтетический snapshot ЕПК-пути.
  3. **geo-честность**: `check_geo_kw_consistency(src_dir, replacements)` вызывается в конце `_copy_grid_unified_steps` → `rep["geo_kw_consistency"]`. Теперь `geo_kw_source_residual` и `geo_neg_target_blocked` показывают РЕАЛЬНЫЕ данные для ЕПК-пути.
  4. **Комментарий `skipped`**: уточнён — ключи в ЕПК гео-заменяются ИНЛАЙН в group_specs[:1237] перед create_full (step_keywords скипается чтобы не создавать дубли, а не потому что замена не нужна).
- **Верификация:** py_compile + pyflakes: 0 новых предупреждений. НЕ деплоено (по условию задачи).
- **Осталось:** live-тест реального ЕПК-копирования с geo-заменой для проверки `geo_kw_consistency` в результате.

## Сессия 2026-07-17 — Мастер-поток баллов копирования: агентский пул — КОД ГОТОВ, НЕ ДЕПЛОЕНО

- **Что сделано:** `AuthContext.headers()` в `work/slepki_direktologov/scripts/direct_copy.py:227` — добавлен `"Use-Operator-Units": "true"` в else-ветку (токен-режим). До правки `phase_pull`/`phase_upload` (campaigns.add, adgroups.add, ads.add, keywords.add, feeds, images и т.д.) сжигали пул КЛИЕНТА; после — агентский пул, как в `yandex_gateway._headers()`.
- **Затронуто:** только `direct_copy.py`. `copy_engine.py` не тронут. Фолбэк на куки при 152 сохранён (`direct_call:259-263 → switch_to_cookie`). ЕПК-ветка (grid, 0 баллов) — не затронута.
- **Верификация:** py_compile + pyflakes OK; 4 pre-existing pyflakes предупреждения, 0 новых. Live-прогон по условию задачи не требовался.
- **НЕ деплоено:** правка в `work/` (Mutagen синкает на LXC101), `direct-copy.service` рестарт нужен при деплое.

## Сессия 2026-07-17 — Автоправила Фаза 4: Правила/Корректировки/Запросы/Площадки/Журнал — ЗАДЕПЛОЕНО+верифицировано

- **Что сделано:** `rules_engine.py` (dry-run ЕСЛИ→ТО против Victory-метрик, `_AUTO_EXEC_ENABLED=False`); `corrections.py` (bidmodifiers.get/set чанками); `queries.py` (Reports SEARCH_QUERY + add_negative_phrases); `placements.py` (Reports PLACEMENT + log_excluded_sites — v5 REST не поддерживает прямое исключение, только audit_log + инструкция); `repository.py` — `audit_log_list` добавлен + `$1→%s` psycopg2-фикс; `autorules_main.py` — Phase 4 deps; `routes_autorules.py` — 8 новых маршрутов; `autorules.html` — 5 панелей + ~400 строк JS.
- **Верификация:** CRUD-тест на живой seoadvanced БД — все 6 шагов OK. py_compile + AST OK. `direct-autorules.service` active, все 6 новых маршрутов — 401 (auth), не 404/500.
- **Ограничение:** Площадки РСЯ — только log-only (v5 REST прямого исключения не поддерживает).
- **Коммит:** f331e41 (home-репо, 8 файлов фазы 4).
- **Осталось:** live-тест за Auth-куком и скриншоты UI — при следующем сеансе с аккаунтом.

## Сессия 2026-07-17 — Автоправила Фаза 3: копирование 1:1 внутри аккаунта — ЗАДЕПЛОЕНО+верифицировано

- **Что сделано:** `direct/autorules/copy.py` (новый standalone-модуль: `list_campaigns` + `clone_campaigns_1to1`, v5/v501 напрямую, State=OFF enforced). Два роута в `routes_autorules.py`: `GET /api/ar/copy/campaigns` + `POST /api/ar/copy/run`. Вкладка «Копирование» в `autorules.html` упрощена: убраны блок трансформации, замена фидов, донор — оставлен бейдж аккаунта + таблица выбора + кнопка «Копировать 1:1».
- **Тест porg-ro552oi2:** `list_campaigns` вернул 2 кампании; `clone_campaigns_1to1(..., dry_run=True)` вернул корректный preview обеих (src_id, name+суффикс, type=TEXT_CAMPAIGN, ok=None). Маршрут 401 на `/api/ar/copy/campaigns?login=...` без куки = ожидаемо (auth check, не 404). `direct-autorules.service` active, journal чистый.
- **Ограничение MVP:** копирует только оболочку кампании (стратегия, таргетинг, минус-слова) — группы и объявления НЕ копируются (задокументировано в copy.py).
- **Коммит:** в home-репо, только 3 файла этой задачи.

## Сессия 2026-07-17 — Автоправила Фаза 2: backend Обзора и Сенсоров — ЗАДЕПЛОЕНО+верифицировано

- **Что сделано:** 6 сенсоров `direct/autorules/sensors/{balance,campaign_status,url_check,minus,goals,anomalies}.py` с интерфейсом `run(login, ctx)`. API-роуты `GET /api/ar/overview`, `/api/ar/balance`, `/api/ar/goals`, `/api/ar/filter-options`, `POST /api/ar/sensors/run` в `routes_autorules.py`. DI Victory conn + direct_tokens + blueprint_metrika в `autorules_main.py`. JS вкладок Обзор+Сенсоры в `autorules.html` (реальный fetch, рендер таблицы, тумблеры, кнопка «Запустить проверку»).
- **Тест porg-ro552oi2** (cartrade196.site, Екатеринбург, y-direct-victory): balance found=1 (0 ₽ < 500 ₽), campaign_status found=2 (MODERATION+OFF), goals found=1 (нет счётчика), anomalies found=0, overview DB = 3 строки с расходом. `direct-autorules.service` active, журнал чистый.
- **Фикс в процессе теста:** balance — v5 accounts.get пуст для клиент-логина → переключил на Live v4 AccountManagement.Get (как в account_service.py). goals — invalid FieldNames TextCampaign/SmartCampaign → используем TextCampaignFieldNames в extra-параметре + DB-fallback.
- **Осталось:** конверсии по выбранной цели в Обзоре (не реализованы — требуют Reports API или Метрику; отмечено в отчёте). Сенсор minus/url_check — не тестировался на аккаунте с живыми объявлениями.
- **Коммит:** 331025c.

## Сессия 2026-07-17 — Вынос API «Обучение ИИ» в `direct-ai.service` :5026 — КОД ГОТОВ, НЕ АКТИВИРОВАНО
- **Мотив:** роуты `/direct/api/ai/*` ходят в M3/LLM (долгие блокирующие вызовы) и занимали Flask-воркеров того же процесса, что создание РК. Отдельной страницы у вкладки нет — выносится ТОЛЬКО API (UI остаётся на :5020).
- **Новый `direct/ai_main.py`** (образец accounts_main/slepki_main): :5026, `DIRECT_ROLE=web` setdefault ДО импорта automation_runtime, domain wiring only (без direct.blueprint), 8 роутов, threaded=True. **`routes_ai.py` не менялся.**
- **`blueprint.py`** — флаг `DIRECT_REGISTER_AI` вокруг register_ai_routes (:347), дефолт "1" = обратная совместимость. Новый `deploy/direct-ai.service` + `deploy/dropins/direct-create.service.d/ai.conf` (DIRECT_REGISTER_AI=0).
- **Evidence:** md5 Mac==LXC101; py_compile OK (прод-венв 3.11, локальный 3.9 не тянет синтаксис); import ai_main → ровно 8 `/direct/api/ai/*` + только nav/static; флаг: unset→8, =0→0, =1→8; AST+identity сверка всех **21** deps с blueprint = IDENTICAL, порядок совпал.
- **⚠️ Находка (важная):** ai-путь читает env в момент вызова, а прод-значения ≠ дефолтам кода → в юнит добавлен паритет: `OPENROUTER_LLM_MODEL=deepseek/deepseek-chat` (код: v4-flash), `DIRECT_SITELINK_REUSE_ACCOUNT=1` (код: 0), `NEURO_PACK_MOUNT=/opt/neuro_content_local`, `DIRECT_CONTENT_REUSE_ACCOUNT=1`. Без них вкладка молча генерила бы иначе.
- **⚠️ Дрейф репо↔прод (не чинил):** на проде 3 drop-in'а direct-create НЕ в git — `flags.conf`, `reuse.conf`, `slepki.conf`. Пересборка по dropins/README поднимет прод без них → поедет генерация и вернутся slepki-роуты. Отдельной задачей.
- **НЕ активировано** (по границам): сервис не поднят, nginx не тронут, не коммичено. Инструкция активации (порядок шагов критичен: сначала :5026 + nginx, ПОТОМ ai.conf) + откат — в `.claude/sdd/direct-ai-service-report.md`.

## Сессия 2026-07-17 — ФАЗА 1 брокера доступа `direct-gateway.service` :5025 — ЗАДЕПЛОЕНО+верифицировано (greenfield)
- **Мотив (опция C Семёна):** каждый из 6 direct-процессов порознь держит куку в `campaign._ACCOUNT_COOKIE_CACHE` и независимо долбит главпоток. Брокер = ЕДИНСТВЕННЫЙ владелец кук/токенов/главпотока/units, один probe на всех.
- **ФАЗА 1 = ТОЛЬКО greenfield.** Существующие call-sites НЕ тронуты (campaign.py/yandex_gateway.py/automation_runtime.py целы). Миграция потребителей = Фаза 2 (отдельно).
- **Новый `direct/gateway_main.py`** (образец accounts_main): standalone Flask :5025, bind СТРОГО 127.0.0.1, `DIRECT_ROLE=web` (setdefault ДО import automation_runtime). БЕЗ blueprint/nav/auth (loopback-only). 8 эндпоинтов `/gw/*`: health, cookie, token, tokens, units_alive, resolve_agency, agency_override GET/POST. Оборачивает готовые `campaign.pick_working_cookie` + `yandex_gateway.{token_for_login,direct_tokens,units_alive_for_login,resolve_agency_hint,agency_override_get/save}`. DI берётся импортом automation_runtime.
- **Новый `direct/gateway_client.py`** — тонкий клиент для Фазы 2 (call-sites ещё НЕ переключены): `gw_cookie/gw_token/gw_tokens/gw_units_alive/gw_resolve_agency/gw_agency_override_get/save`. КАЖДАЯ: HTTP-таймаут 4с на GATEWAY_URL (default http://127.0.0.1:5025, fallback-URL из DIRECT_GATEWAY_HOST/PORT) → при ЛЮБОЙ ошибке ФОЛБЭК на локальную функцию (без него рестарт gateway ронял бы create).
- **systemd `/etc/systemd/system/direct-gateway.service`** (образец direct-accounts): DIRECT_ROLE=web, HOST=127.0.0.1 PORT=5025, NEURO_PACK_MOUNT, ExecStart `-m direct.gateway_main`, enable --now, active.
- **Evidence:** py_compile Mac+LXC101 OK; md5 Mac==LXC101 (`f0cb1a0e…` main, `9f9bf716…` client). :5025 bind 127.0.0.1 (НЕ 0.0.0.0), health `{ok:true}`, 1 тред (нет recover/worker/sweep в журнале). /gw/tokens=7 агентств с токенами; /gw/token(porg-usmc4253)=реальный токен y-direct-victory; /gw/cookie=1381 симв (live-probe главпотока сработал); units_alive=true; resolve/override GET/POST round-trip OK (temp-строка вычищена). Фолбэк доказан: битый порт :5999 → те же данные (7 tokens, cookie 1381) БЕЗ новых запросов к брокеру (журнал), live :5025 → GET /gw/* 200 в журнале. /gw НЕ в nginx; снаружи `https://seoadvanced.ru/gw/health`=404. Другие 6 direct-сервисов active, файлы не менялись.
- **Осталось:** Фаза 2 — переключить call-sites на gateway_client (отдельно). Приёмка direct_verifier. НЕ коммичено.

## Сессия 2026-07-17 — Вынос САМОЙ СТРАНИЦЫ дашбордов «Обзор»/«Статистика» в direct-accounts :5024 — ЗАДЕПЛОЕНО+верифицировано
- **Мотив (продолжение прошлой сессии):** API дашбордов уже был на :5024, но панели физически жили ВКЛАДКАМИ в index.html (:5020) → правка их HTML/JS требовала рестарта создания РК. Теперь страница вынесена.
- **Новая страница `templates/direct/accounts.html`** (образец slepki.html/copy.html): обе панели (panel-accounts + panel-stats) 1:1 из index.html + под-вкладки (клиентский `showAcctSub`). Роут `accounts_main.py` → `@bp.route("/automation/accounts")` render_template. DIRECT_ROLE=web (setdefault ДО импортов) цел.
- **`static/direct/accounts_ui.js`** (самодостаточный, 913 стр) — ВЕСЬ dashboard-JS + дублирует шаренные хелперы (esc/uiConfirm/setCurAccount/loadAccounts-ПОЛНАЯ/loadAgents/AGENTS_INFO/renderAgentHint/setTopMsg/progress). index.html его НЕ подключает (redeclare-конфликт). `accounts_ui.css` — копия dashboard-стилей da-* из inline `<style>` index.html + стили под-вкладок.
- **index.html усохла 6433→5519** (−913 стр): удалены обе панели + dashboard-only JS (таблица Обзор/фильтры/сортировка/открут/баланс/блокировки + весь блок Статистика + промо ИИ + мёртвый OV_*). Табы Обзор/Статистика → ССЫЛКИ на /direct/automation/accounts. `switchToPanel` без веток accounts/stats (+ null-guard в списке панелей). Дефолт-панель теперь `create` (switchToPanel на заходе без ?tab). `?tab=accounts|stats` → JS-редирект на новую страницу.
- **КЛЮЧЕВОЕ решение по сцепке:** `loadAccounts`/`ensureAllAccounts`/`ACC_ALL`, `loadAgents`/`AGENTS_INFO`/`renderAgentHint`, `setCurAccount`/`CUR_ACCOUNT`, `esc`/`setTopMsg`/`uiConfirm` реально нужны панели СОЗДАНИЯ РК → ОСТАЛИСЬ в index.html. Чистого «всё в один static для обеих» без риска create нет → сделал самодостаточный accounts_ui.js (как copy_common.js/пролог slepki.html). В index.html `loadAccounts` заменён на СЛИМ-версию (только фетч ACC_ALL для ulogin-подсказок, без рендера таблицы). `createForStatsAccount` на дашбордах → навигация `/direct/automation?tab=create&login=`.
- **nginx** `location ^~ /direct/automation/accounts → :5024` ДО общего `/direct/`. Бэкап `.bak.acctpage_20260717_142725`, nginx -t OK, reload.
- **Evidence:** md5 Mac==LXC101 (accounts_main/accounts.html/accounts_ui.js/.css/index.html все совпали); py_compile OK Mac+LXC101; node --check обоих JS OK; Jinja compile обоих шаблонов OK; **изоляция:** restart direct-accounts → create 15600/worker 5774 НЕ изменились; restart direct-create → accounts pid 20322 НЕ изменился, страница жива; journal :5024 (pid 20322) показал `GET /direct/automation/accounts 302` = nginx рулит на :5024; static js/css 200; create/copy/slepki/accounts все 302 (auth, не 404); 0 dangling-ссылок на удалённые функции в index.html; 0 create-only ссылок в accounts_ui.js; startup без трейсбеков.
- **Осталось:** live-визуал под сессией в браузере (обе под-вкладки Обзор/Статистика рендерятся, промо ИИ, сортировка, откруты/баланс) — на direct_verifier + ui_verifier. Минор: `createForStatsAccount` кросс-страница не автозаполняет login формы создания (deep-link ?login= не читается create-панелью); мёртвый `scheduleApplyFilters` оставлен (безвреден). НЕ коммичено.

## Сессия 2026-07-17 — Вынос API дашбордов «Обзор»/«Статистика» в direct-accounts.service :5024 — ЗАДЕПЛОЕНО+верифицировано
- **Мотив:** read-only дашборды бьют в Direct API (медленно, могут ВИСНУТЬ). Раньше их зависание/деплой сидели в одном процессе с созданием РК. Вынесены в свой процесс → изоляция.
- **Новый файл `direct/accounts_main.py`** (по образцу slepki_main): :5024, `DIRECT_ROLE=web` через setdefault ДО импорта automation_runtime (иначе роль all подняла бы воркеров создания). DI берётся из `automation_runtime` (он на импорте делает `account_service.configure` + blueprint_metrika + repository — вся проводка). Регистрирует `register_account_routes` + `register_overview_routes`.
- **nginx** `/etc/nginx/sites-enabled/seoadvanced.ru`: 5 exact-match (`location =`) блоков ПЕРЕД общим `location /direct/` → :5024: `/direct/api/{overview,account_stats,balance,accounts_otkrut,statuses}`. Бэкап `.bak.accounts_20260717_134620`. Exact-match критичен: `/direct/api/accounts` (пикер создания) НЕ ловится и остаётся на :5020.
- **Граница (перепроверена по index.html):** переносимые вызываются ТОЛЬКО дашбордами. `account_assets` используется И созданием (2269 «Обновить фиды») И дашбордом (5308 Статистика) → ОСТАЁТСЯ на :5020 целиком (в move-list его нет). На :5020 остались prefill/assets/audiences/goal_for_counter/account_info/accounts.
- **systemd** `/etc/systemd/system/direct-accounts.service` (образец direct-slepki): DIRECT_ROLE=web, DIRECT_ACCOUNTS_PORT=5024, enabled+active.
- **Evidence:** py_compile Mac+LXC101 OK (md5 `bfc5a87a…` совпал); nginx routing доказан журналами werkzeug (:5024 получил overview/account_stats/accounts_otkrut; :5020 получил account_prefill/accounts); restart direct-accounts → create pid 230/worker 5774 НЕ изменились; restart direct-create → :5024 жив, /overview отвечает; :5024 threads=1 (нет create-демонов); старт без трейсбеков.
- **Осталось:** live-проверка вкладок Обзор/Статистика в браузере под сессией (проводка доказана, curl без куки даёт 401) — на direct_verifier/Семёна. Не коммичено.

## Сессия 2026-07-17 — Копия porg-mushirne→porg-jh2si7rh: фикс organic/placementTypes/promoExtension

### Что сделано
- **organic/placementTypes (712850009)**: `platforms.organic=False, platforms.gallery=False` — исправлено живым зондом. Источник: `set_campaign_organic_and_placement` не патчил `biddingStategyWithPlatforms.platforms.organic/gallery`; фикс — патчить ОБА уровня (кампанейный флаг + стратегические платформы). Верифицировано зондом: organic True→False, pts [ADV_GALLERY,SEARCH_PAGE]→[SEARCH_PAGE], стратегия AUTOBUDGET→AUTOBUDGET.
- **promoExtension (код)**: в `direct_copy.py:phase_pull` удалены невалидные FieldNames "Status"/"State" из `promotions.get` → v5 error 8000 больше не будет → snapshot.promotions заполнится. `direct-copy.service` рестартован.
- **Три новых guard**: `OPTIMIZE_CONVERSIONS+avgCpa=None → AUTOBUDGET` (не AUTOBUDGET_AVG_CPA); `DEFAULT → _unsupported_strategy`; `MULTIPLE_CPA → _unsupported_strategy`.

### Что ЗАБЛОКИРОВАНО (3/5 кампаний без safe write-enum)
- 712850007 (DEFAULT/ручные ставки): нет write-enum для DEFAULT → skipped
- 712850008 (OPTIMIZE_CLICKS): нет write-enum → skipped
- 712850299 (MULTIPLE_CPA/тёплый спрос): MULTIPLE_CPA невалиден в write → skipped
- Для этих трёх: organic=True, pts=[ADV_GALLERY,SEARCH_PAGE] — невозможно исправить без обходного write-пути.

### Текущий diff (после фиксов)
- organic DIFF: 3/5 (только заблокированные)
- placementTypes DIFF: 3/5 (те же)
- promoExtension DIFF: 4/5 (требует нового copy run после рестарта сервиса)

## Сессия 2026-07-17 — CT0000_GROUPS + ТРИ АККАУНТА — ЗАВЕРШЕНО

### Фикс CT0000_GROUPS_FALLBACK_TO_SINGLE_TOVARNAYA (тройной)
- Root-cause: (1) `_struct_items` ~1624 пропускал ct0000 → `_items=[]`; (2) fallback ~864 создавал 1 «Товарная галерея»; (3) ранний выход `_tp1_pack_groups` ~738 срабатывал на пустом паке даже без сбоя M3.
- Фикс `create_set_tp1_builders.py`: (1) ранний-выход bypass — структурная проверка ct0000+gk для обоих путей tp1/tp5; (2) фолбэк-блок без гейта `_og is not None`; (3) skip-condition допускает ct0000+gk. Финальный md5 `0f16091d40a2850db07e5f2269522060` Mac==LXC101.

### Все три аккаунта ✅ верифицированы через v501 + Grid
- **avto_sk / porg-vfdnaolu**: 10 ЕПК-кампаний — Макс×2 (1гр «ЕПК макс»), Рет×4 (1гр «ЕПК рет»), 3гр×4 (3гр «Автотаргет»/«Рет»/«Все вместе (интересы)»). Grid: 18 кампаний. ✓
- **avtolajt_bu / porg-yzw6hkyk**: tp1 1 кампания 3 группы (Купить б/у авто, Кредит, Рассрочка); tp5 4 кампании×3 группы (Макс/Lul/Все); tp7 12 кампаний (3 типа×4 фида), ГОРОД→Краснодар. ✓
- **sk_krs / porg-usmc4253**: tp1 1 кампания **1 группа** («Товары — марка модель», id 5773945086 → ShoppingAd=1, ListingAd=1, TextAd=0, гео в RegionIds группы `[10995]`); tp7 **2 кампании** (`712851249` «Товарка - ТК · Марки и модели», `712851273` «Товарка - ТК · Рендеры», обе `source=UAC`/`metaType=ECOM`), всего в аккаунте 3 РК, все DRAFT. ✓ ⚠️ Исправлено 2026-07-17 (v5+Grid+UAC): раньше тут стояло «tp7 8 кампаний (2 типа×4 фида)» — по факту 2; и «2 группы (Краснодарский край, Товары — марка модель)» — «Краснодарский край» это суффикс ИМЕНИ кампании (`tp1_cpc_site — РСЯ - Модели - Автотаргетинг - Краснодарский край`), прошлая сессия распарсила его как отдельную группу. Регион в имени ≠ группа.
- Во всех трёх: ГОРОД нет, State=DRAFT, черновики.

### Осталось убрать
- Temp-файлы LXC101: probe_avto_sk.py, verify_tp5_groups.py, probe_avtolajt.py, probe_sk_krs.py, verify_avto_sk.py, verify_avtolajt.py, verify_avtolajt2.py, verify_grid_tp7.py, check_sk_krs.py, verify_sk_krs.py — можно удалить в любое время.

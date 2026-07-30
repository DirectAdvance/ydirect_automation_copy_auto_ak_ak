# Content Editor Service

URL: `https://seoadvanced.ru/direct/automation/content`

Runtime: отдельный Flask-процесс `direct-content.service` на LXC101,
`127.0.0.1:5021`. nginx направляет сюда только:
- `GET /direct/automation/content`
- `/direct/api/content-editor/*`

Остальной Direct остаётся в `direct-create.service` на `127.0.0.1:5020`, поэтому редактор
контента можно перезапускать без остановки очереди создания РК:
`ssh proxmox-ts "pct exec 101 -- systemctl restart direct-content.service"`.

Назначение: массово исправлять контент, который генерирует ИИ на M3 для кампаний Директа:
заголовки, тексты, быстрые ссылки и уточнения. Основной сценарий — найти ошибочный фрагмент
во всех кампаниях аккаунта и заменить его одним действием.

## Транспорт

Запись идёт по типу поля — не единым способом:
- `ad_title` / `ad_title2` / `ad_text` — Grid `findAndReplaceText`;
- `sitelink_title` / `sitelink_description` / `sitelink_href` — для каждого затронутого набора
  создаётся новый `SitelinkSet`, затем перепривязка: campaign-level → Grid
  `set_campaign_sitelink_set`; ad-level обычных кампаний сначала чистится от overrides и
  переводится на новый campaign-level набор через Grid. Прямая per-ad перепривязка больше не
  является основным путём, потому что даёт частичные отказы на архивных/невалидных объявлениях.
  UAC (tp6/tp7) — PATCH `/web-api/uac/campaign/{id}` (см. ниже). Direct API не даёт безопасного
  `sitelinks.update` для изменения элемента набора in-place, поэтому замена текста/описания/URL
  быстрых ссылок работает copy-on-write: новый набор + перепривязка активных мест использования;
- `sitelink_reorder` — позиционная перестановка, см. следующий раздел;
- `sitelink_assign` («назначить этот набор всем») — создаёт/переиспользует полный набор БС,
  очищает ad-level overrides у объявлений обычных кампаний и привязывает новый набор только через
  campaign-level `inheritableSitelinkSet`; скрытый ad-level fallback запрещён;
- `callout_assign` («новые уточнения для всех кампаний») — создаёт/находит уточнения в библиотеке,
  очищает ad-level overrides и привязывает их только через campaign-level `inheritableCallouts`;
  МК/товарка пропускаются, потому уточнений у них нет;
- `callout` не редактируется in-place: если нового текста ещё нет в библиотеке, создаётся
  новый CALLOUT через `adextensions.add`, затем старый id убирается из `inheritableCallouts`
  кампаний и новый id привязывается через Grid.

Задания идут через асинхронную очередь Postgres (`direct_automation.content_jobs`,
`ensure_jobs_table`), исполняются отдельным процессом `direct-content-worker.service`
(`content_worker.py`, `make_job_executor`) — **при правке кода записи ОБЯЗАТЕЛЬНО рестартовать
и `direct-content.service` (веб), и `direct-content-worker.service` (очередь)**: воркер держит
модуль в памяти, рестарт только веба его не обновляет.
**Правка Jinja-шаблона тоже требует рестарта веба** — процесс идёт с `use_reloader=False`, иначе
отдаётся старый шаблон из памяти (наступали 2026-07-19).

### Перестановка порядка быстрых ссылок (`sitelink_reorder`)

UI: вкладка «Быстрые ссылки» → «↕️ Порядок быстрых ссылок» (`content_editor.html`, `ceReorder*`).
Позиционная перестановка (`_reorder_sitelinks`, routes_content_editor.py) — целевой порядок задаётся
массивом индексов `perm`, применяется как `result[i] = items[perm[i]]` к КАЖДОМУ набору аккаунта
(наборы короче `perm` — пропуск с отчётом, не падение).

- **Область действия** — выпадающий список над чипами: «Все наборы» (по умолчанию) или конкретный
  набор (`target_set_id`). При конкретном наборе перестановка коснётся ТОЛЬКО его; обёртка
  задания — JSON `{"perm": [...], "target_set_id": ...}` (старый формат — голый список — тоже
  разбирается, backward-compat).
- **UAC (tp6/tp7)** — синтетический набор `set_id="uac:<campaign_id>"`, запись через
  `_uac_patch_campaign_texts` (PATCH полного тела кампании — узкий patch у UAC ненадёжен,
  fallback строит full-payload по `_UAC_PATCH_FULL_KEYS`, сверено 1-в-1 с реальным браузерным
  PATCH через HAR-капture 2026-07-13).
- **Обычные кампании переводятся на campaign-level** — перед применением порядка сервис чистит
  ad-level overrides (`TextAd` и `ResponsiveAd`) и затем ставит новый набор через
  `set_campaign_sitelink_set`. Если campaign-level read-back не подтверждён, это ошибка, а не
  повод скрыто писать БС на объявления.
- **Grid strategy enum:** read-значения `DEFAULT`/`MULTIPLE_CPA` нельзя отправлять в `UpdateCampaigns`
  как есть; write-значения — `DEFAULT_`/`AUTOBUDGET_MULTIPLE_CPA`. Без этого чекбоксы БС/уточнений
  в UI Директа остаются пустыми на уровне кампании.
- Для пользователя UAC не выделяется отдельной категорией (тег/подпись — как у обычного набора);
  различие только в реализации записи.
- Верифицировано вживую 2026-07-13 на `porg-psm5h7q6` (UAC-кампания 712694743): реальный apply →
  read-back независимым GET подтвердил применение и откат.

## Endpoint'ы

- `GET /direct/automation/content` — изолированная страница редактора.
- `GET /direct/api/content-editor/accounts` — поиск аккаунтов из `local_gsheet_sites`.
- `POST /direct/api/content-editor/load` — загрузка снимка контента аккаунта.
- `POST /direct/api/content-editor/preview` — подсчёт объектов, где найден `old_text`.
- `POST /direct/api/content-editor/replace_async` — постановка в очередь замены поля (title/desc/href/text/callout).
- `POST /direct/api/content-editor/sitelinks/reorder_async` — постановка в очередь перестановки порядка (`perm`, опционально `target_set_id`).
- `GET /direct/api/content-editor/jobs` — список заданий текущего пользователя (или всех — админ).
- `POST /direct/api/content-editor/admin/queue/cleanup` — админ: удалить завершённые задания (done/error/cancelled) старше 3 суток из очереди (текст + сверка цен); активные не трогает.

## Очередь и Agent Board

- Вкладка `Очередь` открыта всем active content-editor users с выданными директологами
  (`can_queue=true`). Full-access/admin видит все content/price/copy jobs; обычный пользователь
  видит только свои content/price jobs и copy jobs, где source/target login относится к его
  directologist-scope.
- Кнопка запуска заливки цен из очереди (`pricecheck/start_now`) доступна только реальному
  `is_admin`. Frontend скрывает price controls для не-админа, backend дополнительно возвращает 403.
- Terminal `status='error'` в `direct_automation.content_jobs` и
  `public.direct_price_check_jobs` автоматически создаёт одну задачу в
  `/services/agent-board/` (`agent_board_task_id`). Задача ставится в `queued` с контекстом
  исходной job и инструкцией: воспроизвести ошибку, исправить причину, добить исходную операцию,
  проверить live и обновить md.
- Backfill для старых content-errors выполняется worker’ом на старте и затем раз в минуту; это
  нужно, чтобы уже упавшие задачи не оставались без разбора после деплоя новой логики.

### Авто-ретрай после фикса в Agent Board

По аналогии с copy (`_copy_agent_retry_daemon_loop`, `direct/core/queue_server.py`) в
`direct-content-worker.service` (`content_worker.py`) запущен демон
`_content_agent_retry_daemon_loop`: раз в `CE_AGENT_RETRY_POLL` секунд (по умолчанию 60) он
опрашивает `content_jobs_ready_for_agent_retry` (`agent_board_bridge.py`) — упавшие job, у которых
связанная Agent Board задача уже `done`, а ретрая ещё не было. Для каждой такой job демон:
1. проверяет, что на этот `login` сейчас нет активной (`queued`/`running`) content job
   (`_content_login_has_active_job`) — не плодит второй параллельный job;
2. собирает и вставляет новую `content_jobs` строку из полей упавшей (`login`, `agency`, `type`,
   `old_text`, `new_text`, `mode`, `campaign_count`, `access_directologists`) через
   `_content_retry_insert_from_failed`;
3. помечает исходную упавшую строку колонками `content_retry_job_id`/`content_retry_started_at`
   через `mark_content_retry_started`, чтобы не создать второй ретрай на ту же ошибку.

**Отличия от copy-ретрая (важно):** `content_jobs` хранит параметры как плоские колонки, а не один
`body jsonb`, и у неё нет статуса `interrupted` — ретрай **одноразовый**
(`content_retry_job_id IS NULL`, без повторной попытки при обрыве). Новая ретрай-job ставится с
`username='agent-board-auto'` и **намеренно обходит** `CE_DAILY_JOB_CAP` (так же, как copy обходит
дневной лимит для своих авто-ретраев) — это осознанное решение, не забытый баг.

## Вкладка «Обзор» (добавлена 2026-07-02)

Копия 1-в-1 вкладки «Обзор» с `/direct/automation` (сайдбар, пункт `📋 Обзор`):
таблица аккаунтов (13 колонок), умный поиск, мультивыбор статусов (дефолт «Контекст активно»),
фильтры директолог/город/салон, кнопки Обновить / Проверка блокировок / Обновить баланс / Excel,
строка итогов, сортировка. Данные fetch-ом с общих эндпоинтов blueprint'а
(`/direct/api/accounts`, `/direct/api/accounts_otkrut`, `/direct/api/balance`, `/direct/api/check_blocks`) —
backend не менялся. Отличие: кнопка «Статистика →» ведёт на `/direct/automation?tab=stats`
(своей панели статистики на этой странице нет). Код — в `templates/direct/content_editor.html`
(JS-функции без префикса `ce*`, коллизий с редактором нет).

## Вкладка «Смена изображений» (добавлена 2026-07-19, только `is_admin`)

Массовая замена ОДНОЙ выбранной картинки на загруженную, строго 1:1, в пределах аккаунта
с опциональным фильтром по кампаниям. Анализируются ВСЕ типы кампаний, включая поиск tp2/tp4 —
картинки заменяются везде, где они фактически есть. Единственное исключение — объявления по
фиду (`GdShoppingAd`/`GdListingAd`), их картинки берутся из фида и сервисом не трогаются.
Перед постановкой в очередь — **обязательный экран подтверждения** (гейт, без него задание
не ставится).

UI: одна кнопка «Показать изображения» грузит ТОЛЬКО инвентарь картинок (не весь контент
вкладки — в отличие от общей кнопки «Показать» на Заголовках/Текстах/…). Выбор аккаунта сверху
подставляется как цель автоматически; точный ввод логина в поиске переключает цель без клика
по чекбоксу (`ceExactLoginMatch()`, только при точном совпадении `login_key`).

Код — `direct/content_images_routes.py`, все ручки за `_admin_allowed()`:
- `GET  /direct/api/content-editor/images/inventory?login=&campaign_ids=` — инвентарь картинок;
- `POST /direct/api/content-editor/images/upload` — multipart `files[]`;
- `POST /direct/api/content-editor/images/preview` — что и где будет заменено;
- `POST /direct/api/content-editor/images/replace_async` → `job_id`;
- ручка отдачи временного превью загруженного файла.

Тип задания в очереди `direct_automation.content_jobs` — `image_replace`, payload JSON лежит
в `new_text` (схема БД не менялась).

### Транспорт — по типу объявления, НЕ по кампании

- **AdaptiveTextAd (РСЯ, не-UAC)** → Grid `UpdateAdaptiveTextAds`;
- **TextAd (поиск tp2/tp4 и остальные)** → Grid `UpdateTextAds`, `textBannerImageHash`
  **скаляром** (не список);
- **UAC-владеемые объявления (tp6/tp7 МК)** → UAC `PATCH /web-api/uac/campaign/{id}`,
  `content_ids` деривируются из `detail["contents"]`;
- **Владение определяется ФАКТОМ** чтения UAC-инвентарём при непустых `contents` (`_uac_owned_cids`).
  ⛔ Выводить владение из имени tp6/tp7 НЕЛЬЗЯ — это вносило регресс: UAC `list_campaigns` оказался
  подмножеством «tp6/tp7 по имени» (архивные МК, товарка).
- **Источник списка кампаний и id объявлений — Grid, не v5:** v5 `campaigns.get`/`ads.get`
  не отдают tp6/tp7/tp8.
- **Архивные кампании/объявления не изменяем.** Grid может вернуть их в общем списке и старый
  хэш может оставаться там после замены; это ожидаемый остаток, не дефект. В write-set и
  `UpdateTextAds`/`UpdateAdaptiveTextAds` попадают только неархивные кампании. Если ad-level
  архив виден только на записи (`CANNOT_UPDATE_ARCHIVED_AD`), job считает его `ads_archived`,
  а не ошибкой недозамены.
- **Двойная запись — известный, не портящий данные побочный эффект:** TextAd `bannerImage`
  не является проекцией UAC `contents` (проверено живой записью), поэтому если хеш TextAd-картинки
  присутствует и в Grid, и в `contents` UAC-владеемой кампании — пишут ОБА транспорта одним и тем же
  файлом (лишний расход места в двух библиотеках, данные не бьются). Комментарий в коде, утверждающий
  «пересечения нет», устарел и требует правки (открытый пункт).

### Ремонт-конфликт — отключён

`execute_images_forbidden_repair` (`repair_media.py`/`repair_gate.py`/`repair_planner.py`/
`campaign_spec_audit.py`) считал наличие картинок в поисковых кампаниях дефектом и вычищал их —
это неверно, подтверждено Семёном. Флаг `SEARCH_IMAGES_FORBIDDEN_RULE_ENABLED = False` в всех
четырёх точках, код не удалён (обратимо).

### Превью бесплатны

Grid: `formats[].path` → `https://direct.yandex.ru/images` + path. UAC: готовый `thumb`.
`adimages.get` не вызывается, баллы не тратятся.

### Товарные (tp7/ecom) кампании — фикс не подтверждён живьём

UAC PATCH для ecom-кампаний терял `feed_id`/`listings_feed_id`/`feed_filters`/`listings_feed_filters`
(стриппинг пустых полей был завязан на `keywords is None`, что истинно и для ecom). Фикс —
доп. гейт `not detail.get("ecom")` в `_uac_campaign_patch_payload`. Проверено сверкой payload
против HAR-66 (39/39 ключей, значения фида совпадают) — **живой записью с откатом не проверено**,
нужен отдельный probe перед тем как считать путь надёжным.

### HAR-эталоны (`_har/`)

`RSYA_image_replace.json`, `UAC_image_replace.json`, `UAC_relevance_match_retargeting.json`,
`TEXTAD_image_replace.json`, `UAC_ecom_feed_replace.json`; разбор асимметрий между ними —
`_har/IMAGE_REPLACE_schema.md`.

## Поддерживаемые типы

- `ad_title` — первый заголовок объявления.
- `ad_title2` — второй заголовок объявления.
- `ad_text` — текст объявления.
- `sitelink_title` — текст быстрой ссылки.
- `sitelink_description` — описание быстрой ссылки.
- `sitelink_href` — URL быстрой ссылки.
- `sitelink_reorder` — перестановка порядка позиций (см. раздел выше).
- `callout` — уточнение кампании.

## Инварианты

- Никакой массовой записи через `v5_call` или `v501_svc`.
- Cookie/Grid writer должен работать через существующие куки главпотока:
  `campaign.pick_working_cookie(login)`.
- При 403 нужно обновлять CSRF так же, как в `grid_finalize.GridClient`.
- Массовая операция должна возвращать количество реально изменённых объектов и список ошибок.
- Для `findAndReplaceText` `successCount` трактуется как количество обработанных ad ids. Для
  быстрых ссылок этот путь не используется: в ЕПК аккаунтах быстрые ссылки могут быть
  campaign-level assets (`inheritableSitelinkSet.assetValue`), а объявления только наследуют
  их. UI показывает usage по объявлениям, но запись выполняется по кампаниям/наборам.
- Архивные кампании исключаются из рабочей области content-editor для быстрых ссылок и картинок:
  Direct возвращает `ACTION_IN_ARCHIVED_CAMPAIGN`, поэтому старый текст/хэш в архиве считается
  ожидаемым остатком. Если нужно поменять архив, сначала вручную восстановить кампанию в Direct.
- **Тихая неполнота недопустима.** Непокрытые типы объявлений и кампании обязаны попадать
  в `skipped` поимённо и с числами. Этот дефект ловили в фиче «Смена изображения» четыре раза:
  limit 5000 без пагинации; источник кампаний из v5; двойная запись на МК; узкое окно сверки.
- **Инвентарь читается с offset-пагинацией** (`_ads_rows_paginated`): одна страница 5000
  молча теряла объявления.
- **Замена одной картинки — ПОЛНАЯ перезапись объекта:** и Grid, и UAC перезаписывают объект
  целиком, всё непереданное обнуляется (класс багов `UAC_FULL_PATCH_REPLACE_DROPS_ASYMMETRIC_KEY`
  в `ERRORS_JOURNAL.md`).

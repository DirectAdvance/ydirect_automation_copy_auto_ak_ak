# Content Editor Service

URL: `https://seoadvanced.ru/direct/automation/content`

Runtime: отдельный Flask-процесс `direct-content.service` на LXC101,
`127.0.0.1:5021`. nginx направляет сюда только:
- `GET /direct/automation/content`
- `/direct/api/content-editor/*`

Остальной Direct остаётся в `direct.service` на `127.0.0.1:5020`, поэтому редактор
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
  `set_campaign_sitelink_set`; ad-level (TextAd/DynamicTextAd) → v5 `ads.update` (`_v5_rebind_ads_sitelink_set`);
  UAC (tp6/tp7) — PATCH `/web-api/uac/campaign/{id}` (см. ниже);
- `sitelink_reorder` — позиционная перестановка, см. следующий раздел;
- `callout` не редактируется in-place: если нового текста ещё нет в библиотеке, создаётся
  новый CALLOUT через `adextensions.add`, затем старый id убирается из `inheritableCallouts`
  кампаний и новый id привязывается через Grid.

Задания идут через асинхронную очередь Postgres (`direct_automation.content_jobs`,
`ensure_jobs_table`), исполняются отдельным процессом `direct-content-worker.service`
(`content_worker.py`, `make_job_executor`) — **при правке кода записи ОБЯЗАТЕЛЬНО рестартовать
и `direct-content.service` (веб), и `direct-content-worker.service` (очередь)**: воркер держит
модуль в памяти, рестарт только веба его не обновляет.

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
- **ResponsiveAd (адаптивные объявления) не поддерживается** — v5 `ads.update` не даёт менять
  `SitelinkSetId` у ResponsiveAd (только TextAd/DynamicTextAd, `_REBIND_SUBTYPE_FIELDS`), Grid для
  этого тоже ненадёжен. Пропуск честный: `_load_account` считает `responsive_count`/
  `responsive_examples` (кампания/группа) РЕАЛЬНО по набору — превью показывает точное
  предупреждение только когда такие объявления реально есть, не для любого ad-level набора.
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

## Вкладка «Обзор» (добавлена 2026-07-02)

Копия 1-в-1 вкладки «Обзор» с `/direct/automation` (сайдбар, пункт `📋 Обзор`):
таблица аккаунтов (13 колонок), умный поиск, мультивыбор статусов (дефолт «Контекст активно»),
фильтры директолог/город/салон, кнопки Обновить / Проверка блокировок / Обновить баланс / Excel,
строка итогов, сортировка. Данные fetch-ом с общих эндпоинтов blueprint'а
(`/direct/api/accounts`, `/direct/api/accounts_otkrut`, `/direct/api/balance`, `/direct/api/check_blocks`) —
backend не менялся. Отличие: кнопка «Статистика →» ведёт на `/direct/automation?tab=stats`
(своей панели статистики на этой странице нет). Код — в `templates/direct/content_editor.html`
(JS-функции без префикса `ce*`, коллизий с редактором нет).

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

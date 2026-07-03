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

Требование сервиса: правки кампаний выполняются только через cookies/Grid, без OAuth write через
Direct API v5/v501.

Текущий срез:
- `load`/`preview` используют v5 read-only как источник снимка контента;
- usage уточнений дополняется cookie/Grid read по `GdCampaignCallouts.assetValue`;
- `replace` не выполняет OAuth-запись;
- `ad_title` / `ad_title2` / `ad_text` пишутся через Grid `findAndReplaceText`;
- `sitelink_title` / `sitelink_description` не пишутся через массовый `findAndReplaceText`:
  для каждого затронутого набора создаётся новый `SitelinkSet`, затем кампании
  перепривязываются к нему через Grid `inheritableSitelinkSet`;
- `callout` не редактируется in-place: если нового текста ещё нет в библиотеке, создаётся
  новый CALLOUT через `adextensions.add`, затем старый id убирается из `inheritableCallouts`
  кампаний и новый id привязывается через Grid;

## Endpoint'ы

- `GET /direct/automation/content` — изолированная страница редактора.
- `GET /direct/api/content-editor/accounts` — поиск аккаунтов из `local_gsheet_sites`.
- `POST /direct/api/content-editor/load` — загрузка снимка контента аккаунта.
- `POST /direct/api/content-editor/preview` — подсчёт объектов, где найден `old_text`.
- `POST /direct/api/content-editor/replace` — запись через cookie/Grid для объявлений и уточнений.

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

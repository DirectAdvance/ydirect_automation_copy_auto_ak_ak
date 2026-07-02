# Content Editor Service

URL: `https://seoadvanced.ru/direct/automation/content`

Назначение: массово исправлять контент, который генерирует ИИ на M3 для кампаний Директа:
заголовки, тексты, быстрые ссылки и уточнения. Основной сценарий — найти ошибочный фрагмент
во всех кампаниях аккаунта и заменить его одним действием.

## Транспорт

Требование сервиса: правки кампаний выполняются только через cookies/Grid, без OAuth write через
Direct API v5/v501.

Текущий безопасный срез:
- `load`/`preview` используют v5 read-only как временный источник снимка контента;
- `replace` не выполняет OAuth-запись и возвращает явную ошибку до реализации cookie/Grid writer;
- тест `test_content_editor_replace_never_writes_via_oauth_api` защищает это ограничение.

## Endpoint'ы

- `GET /direct/automation/content` — изолированная страница редактора.
- `GET /direct/api/content-editor/accounts` — поиск аккаунтов из `local_gsheet_sites`.
- `POST /direct/api/content-editor/load` — загрузка снимка контента аккаунта.
- `POST /direct/api/content-editor/preview` — подсчёт объектов, где найден `old_text`.
- `POST /direct/api/content-editor/replace` — будущая запись через cookie/Grid. OAuth write отключён.

## Поддерживаемые типы

- `ad_title` — первый заголовок объявления.
- `ad_title2` — второй заголовок объявления.
- `ad_text` — текст объявления.
- `sitelink_title` — текст быстрой ссылки.
- `callout` — уточнение кампании.

## Инварианты

- Никакой массовой записи через `v5_call` или `v501_svc`.
- Cookie/Grid writer должен работать через существующие куки главпотока:
  `campaign.pick_working_cookie(login)`.
- При 403 нужно обновлять CSRF так же, как в `grid_finalize.GridClient`.
- Массовая операция должна возвращать количество реально изменённых объектов и список ошибок.


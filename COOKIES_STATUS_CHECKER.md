# Чекер статуса кук (виджет "Куки: ...")

Виджет в шапке `/direct/automation` — кружок-индикатор + "Куки: живы X/Y" + список
протухших аккаунтов + кнопка обновления. Полностью заменил старое Tampermonkey-расширение
(`cookies_tampermonkey/*`, удалено) — источник свежих кук теперь **glavpotok.ru** (HTTP-relay).

## Backend

- `blueprint.py` → `_cookies_status_response()` (~строка 3759), маршрут `/direct/api/cookies-status`
  регистрируется в `routes_pack.py:60` (`api_cookies_status`).
- Список проверяемых аккаунтов — `campaign.py:1005` константа `DEFAULT_COOKIE_ACCOUNTS`
  (хардкод-тюпл, не БД/конфиг):
  ```
  victoryagency-direct1618440, victorylotsofads1, victoryagency14,
  y-direct-victory, victoryagencydirect, useful-call-agency
  ```
  (`useful-call-agency` добавлен коммитом `985a363`).

### Как определяется "протухла" (не по времени, а живым запросом)

Для каждого аккаунта из `DEFAULT_COOKIE_ACCOUNTS`:
1. Берём до 3 реальных `ulogin` клиентов этого агентства из `public.local_gsheet_sites`
   (если нет — пробуем "на себе", напр. `y-direct-victory`).
2. Получаем куку: `cmc.load_cookie(account)` → сперва `fetch_cookie_glavpotok` (glavpotok.ru),
   при неудаче — `load_cookie_local` (`.secret/cookies.json`, может быть протухшей).
3. Дёргаем `cmc.UacClient(cookie, ulogin).link_info("https://ya.ru")` (`campaign.py:1270`,
   `GET /web-api/uac/linkinfo`).
4. Кука жива, если запрос прошёл ИЛИ упал с `"Нет прав"` / `code 54` (сессия
   аутентифицирована, просто нет прав на этот ulogin). Любая другая ошибка → `✗ протухла`.

Результат кэшируется 5 минут (`_COOKIES_STATUS_CACHE`, `_COOKIES_STATUS_TTL=300.0`);
`?force=1` обходит кэш — именно это шлёт кнопка "Обновить".

Ответ: `{ok, alive, total, dead:[...], detail, checked_at, cached}`.

## Frontend

`templates/direct/index.html`:
- Разметка виджета — строки 476–486 (`#cookies-status-badge`, `#cookies-status-text`,
  `#cookies-status-time`, кнопка "⟳ Обновить", список `#cookies-dead-list`, кнопка
  "↻ Запросить с главпотока" `#cookies-glavpotok-btn`, скрыта пока все куки живы).
- `pollCookiesStatus(force)` (строки 2251–2275): `fetch('/direct/api/cookies-status[?force=1]')`,
  красит бейдж (`m3-ok`/`m3-down`), текст `"Куки: живы X/Y"` / `"Куки протухли (X/Y)"`,
  рисует `✗ протухли: <b>acc1</b>, <b>acc2</b>` (строка 2266).
- Автополлинг: строка 5802 — `pollCookiesStatus()` на загрузке страницы,
  затем `setInterval(pollCookiesStatus, 20*60*1000)` (каждые 20 минут).

## "Запрашиваю..." — получение свежих кук

- Клик по "↻ Запросить с главпотока" → `requestGlavpotokCookies(btn)` (строки 2302–2312):
  текст кнопки → "запрашиваю…", блокируется, вызывает `pollCookiesStatus(true)` (force).
  Сам force-пробник и есть источник свежих кук — бэкенд внутри снова дёргает
  `fetch_cookie_glavpotok`.
- `fetch_cookie_glavpotok(login)` (`campaign.py:1144-1165`): `GET {GLAVPOTOK_COOKIES_URL}/{login}`
  с `Authorization: Bearer {GLAVPOTOK_COOKIES_TOKEN}` на **glavpotok.ru** (внешний relay,
  креды через `.secret/loader.py::load_glavpotok_cookies()`), возвращает `cookie_string`.
  Это и есть замена Tampermonkey-расширения — glavpotok.ru держит залогиненную сессию
  Яндекс.Директа и отдаёт свежие куки по HTTP.
- Фоллбэк если glavpotok ничего не отдал: `load_cookie_local(account)` (`campaign.py:1168-1181`)
  читает `.secret/cookies.json` — может быть сам протухшим, используется только при
  недоступности glavpotok.
- `load_cookie(account)` (`campaign.py:1184-1189`) — общий хелпер: сначала glavpotok, потом
  локальный файл. `pick_working_cookie` (`campaign.py:1809`) — продовый picker при создании РК.
- После force-обновления `requestGlavpotokCookies` проверяет, остался ли виден
  `#cookies-dead-list`, и показывает баннер:
  - "✅ Все куки живы — свежие получены с главпотока"
  - "⚠️ Часть кук всё ещё протухла — на главпотоке нет свежей сессии... нужна переавторизация там"

## Полный flow (кнопка → экран)

1. Загрузка страницы / каждые 20 мин → `pollCookiesStatus()` авто, либо клик по бейджу/
   "⟳ Обновить" → `pollCookiesStatus(true)`.
2. JS `fetch('/direct/api/cookies-status?force=1')` → Flask `api_cookies_status`
   (`routes_pack.py:60`) → `_cookies_status_response()` (`blueprint.py:3759`).
3. Бэкенд проходит по `DEFAULT_COOKIE_ACCOUNTS`, пробует каждый через `UacClient.link_info`,
   куку берёт через glavpotok (или локальный фоллбэк), собирает
   `{ok, alive, total, dead, detail, checked_at}`.
4. JS красит бейдж и рисует список "✗ протухли: ...".
5. Если есть протухшие — появляется "↻ Запросить с главпотока" → клик →
   `requestGlavpotokCookies()` → "запрашиваю…" → повторный force-пробник (сам подтягивает
   свежие куки с glavpotok.ru) → баннер успеха/неудачи.

## Примечание

Папка `cookies_tampermonkey/` (README.md, manifest.json, popup.html, popup.js) удалена из
проекта — расширение полностью заменено связкой "бейдж в UI + glavpotok.ru relay".

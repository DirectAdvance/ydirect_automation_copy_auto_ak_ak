# UI-карта: seoadvanced.ru/direct/automation

> Цель файла: однозначно сказать, где лежит HTML/JS/API логика раздела Direct, чтобы не
> править мёртвый файл и не сверять md5 не того артефакта.

---

## Маршрут и рендеринг основной страницы

```
GET /direct/automation
    └── direct/routes_pages.py : automation()
            └── direct/automation_runtime.py : _render_page()
                    └── render_template("direct/index.html", ...)
                            └── home/seoadvanced/templates/direct/index.html
```

Точка входа Flask:
`direct/main.py:create_app()` (`direct-create.service`, порт `127.0.0.1:5020`).

`template_folder` указывает на `home/seoadvanced/templates`, поэтому боевой шаблон:

```
home/seoadvanced/templates/direct/index.html
```

Не путать с `home/seoadvanced/direct/._index.html` — это macOS AppleDouble metadata-файл.

---

## Основная страница `/direct/automation`

| Файл | За что отвечает |
|------|-----------------|
| `templates/direct/index.html` | HTML основной страницы: sidebar, панели, Jinja seed `FEEDS`/`IS_ADMIN`, подключение CSS/JS |
| `static/direct/automation.css` | Основные стили страницы `/direct/automation` |
| `static/direct/automation.js` | Общая SPA-обвязка: вкладки, overview/stats, помощь, готовые логины, lazy-load правил и контента |
| `static/direct/automation_create.js` | Создание РК: prefill аккаунта, M3/cookies/OpenRouter статусы, `createSet()`, `createDraftsFromSlepok()`, `startCreateJob()`, дерево Посевов из `posevy.json` |
| `static/direct/automation_jobs.js` | Стек задач создания: карточки, polling, cancel/resume/delete, отображение проверок/ремонта |
| `static/direct/automation_rules.js` | Глобальные правила Direct UI. Не подключается в HTML напрямую, lazy-loaded из `automation.js` |
| `static/direct/automation_content.js` | Контент M3. Не подключается в HTML напрямую, lazy-loaded из `automation.js` |
| `static/direct/slepki_ui.js` | Общие деревья структуры, используемые и на странице слепков, и во вкладке создания РК |
| `static/direct/slepki_ui.css` | Общие стили деревьев/бейджей слепков |
| `static/direct/slepki_minus_places.js` | UI минус-площадок/исключений для дерева слепков |
| `direct/routes_pages.py` | `GET /direct/automation` |
| `direct/automation_runtime.py` | `_render_page()`: собирает Jinja-контекст и рендерит `direct/index.html` |

Важно: прежняя карта была устаревшей. JavaScript больше НЕ живёт целиком inline в
`index.html`; основная логика вынесена в `/static/direct/automation*.js`.

В `index.html` остаётся маленький inline seed и список подключённых модулей:

```html
<script src="/static/direct/slepki_ui.js?..."></script>
<script src="/static/direct/slepki_minus_places.js?..."></script>
<script>
const FEEDS = {{ feeds_catalog | tojson }};
const IS_ADMIN = {{ is_admin | tojson }};
</script>
<script src="/static/direct/automation_jobs.js?..."></script>
<script src="/static/direct/automation_create.js?..."></script>
<script src="/static/direct/automation.js?..."></script>
```

`automation_rules.js` и `automation_content.js` грузятся лениво через
`ensureRulesModule()` / `ensureContentModule()` в `automation.js`.

---

## Вкладки и отдельные страницы

### SPA-вкладки внутри `/direct/automation` (`direct-create.service`, :5020)

| Вкладка | HTML | JS/API |
|---------|-----|--------|
| Обзор | `templates/direct/index.html` | `static/direct/automation.js` + `/direct/api/overview` |
| Статистика по аккаунтам | `templates/direct/index.html` | `static/direct/automation.js` + `/direct/api/account_stats` |
| Создание РК | `templates/direct/index.html` | `automation_create.js`, `automation_jobs.js`, `routes_set_plan.py`, `routes_jobs.py`, `routes_create_set.py` |
| Глобальные правила | `templates/direct/index.html` | lazy `automation_rules.js`, `routes_settings.py` |
| Контент | `templates/direct/index.html` | lazy `automation_content.js`, `routes_content.py`, `routes_pack.py` |
| Обучение ИИ | `templates/direct/index.html` | UI в `automation.js`, API может быть вынесен в `direct-ai.service` |
| Готовые логины | `templates/direct/index.html` | `routes_ready_logins.py` |
| Помощь | `templates/direct/index.html` | `routes_ai.py` |

### Отдельные страницы/сервисы

| URL | Сервис | Файлы |
|-----|--------|-------|
| `/direct/automation/content` | `direct-content.service` :5021 | `content_main.py`, `routes_content_editor.py`, `templates/direct/content_editor.html`, `static/direct/content_editor.js/css` |
| `/direct/automation/copy` | `direct-copy.service` :5022 | `copy_main.py`, `routes_copy.py`, `templates/direct/copy.html`, `templates/direct/copy_other.html`, `templates/direct/_copy_common.html`, `static/direct/copy_*.js/css` |
| `/direct/automation/slepki` | `direct-slepki.service` :5023 | `slepki_main.py`, `routes_slepki_edit.py`, `templates/direct/slepki.html`, `static/direct/slepki_ui.js/css` |
| `/direct/automation/accounts` | `direct-accounts.service` :5024 | `accounts_main.py`, `templates/direct/accounts.html`, `static/direct/accounts_ui.js` |
| `/direct/autorules` | `autorules_main.py` :5027 | `routes_autorules.py`, `templates/direct/autorules.html` |

`/direct/automation/copy` — каноническая страница копирования. Старый скрытый
`#panel-copy` в `templates/direct/index.html` является legacy-остатком прежней SPA-вкладки и не
должен быть точкой разработки copy-сервиса.

---

## Поток создания набора РК

Кнопки создания живут в `templates/direct/index.html`, обработчики — в
`static/direct/automation_create.js`, карточки и polling задач — в
`static/direct/automation_jobs.js`.

```
createSet()
    1. POST /direct/api/set_plan
       → routes_set_plan.py → create_set_plan.py

    2. POST /direct/api/create_set_async
       → routes_jobs.py
       → direct-create-worker.service
       → create_set_orchestrator.py

    3. GET /direct/api/create_set_status
       → routes_jobs.py

    4. POST /direct/api/create_set_cancel
       → routes_jobs.py
```

Серверная логика создания разбита по модулям:

- `create_set_plan.py` — план набора.
- `create_set_orchestrator.py` — оркестрация.
- `create_set_*` — этапы и per-tp builders.
- `campaign.py`, `direct_v501_client.py`, `uac_client.py` — API-клиенты.
- `grid_create.py`, `grid_finalize.py` — Grid/cookie path.

---

## Поток копирования кампаний

Копирование НЕ разрабатывать в `index.html`/`automation.js`. Рабочая страница:

```
GET /direct/automation/copy
    └── direct/copy_main.py
            └── routes_copy.py : copy_page()
                    └── render_template("direct/copy.html", ...)
```

Ключевые файлы:

| Файл | Роль |
|------|------|
| `direct/copy_main.py` | Flask entrypoint `direct-copy.service` |
| `direct/routes_copy.py` | HTTP-роуты `/direct/api/copy_*` и `/direct/automation/copy` |
| `direct/copy_api.py` | внешний API `/api/v1/copy/*` |
| `direct/copy_request.py` | общая request-валидация UI/API copy-start |
| `direct/copy_engine.py` | главный оркестратор copy-job |
| `direct/copy_postprocess.py` | cookie/Grid postprocess copy-job |
| `direct/copy_grid_unified.py` | Grid-only copy выбранных UnifiedCampaign |
| `direct/copy_steps.py` | фасад postprocess-шагов |
| `direct/copy_*_steps.py` | postprocess шаги: ключи, ассеты, креативы, цены, настройки |
| `direct/copy_verify.py` | фасад source↔target verification |
| `direct/copy_verify_*.py` | verification-разделы: source, target, diff, geo, repair |
| `templates/direct/copy.html` | страница copy-сервиса |
| `static/direct/copy_common.js` | общий JS: источник, очередь, поллинг |
| `static/direct/copy_auto.js` | вкладка «Авто» |
| `static/direct/copy_other.js` | вкладка «Прочие сферы» |

Maintenance-note: тексты модалок `Что проверяется` и `Что меняется` живут в
`static/direct/copy_common.js` (`_COPY_CHECKLIST`, `_COPY_CHANGELIST`). При изменении
copy-пайплайна, `copy_verify`, postprocess-шагов, промоакций или библиотек минус-слов
обновлять оба списка вместе с кодом. Если меняется набор фактических проверок, также
обновлять `_CV_ITEM_DIM`, чтобы вкладка очереди `Проверки` показывала статусы по тем же пунктам.
Пункт `Фильтры товарных и каталожных объявлений` обязан покрывать count и signature:
`shopping_filter_count`, `listing_filter_count`, `shopping_filter_signature`,
`listing_filter_signature`.
Фактические статусы последнего прогона не должны менять значки в модалке `Что проверяется`:
они отображаются только в карточке очереди копирования под кнопкой `Проверки`.

Nginx-инвариант: все внутренние copy API должны начинаться с `/direct/api/copy_`, иначе запрос
уйдёт в общий `direct-create.service` (:5020), а не в `direct-copy.service` (:5022).

---

## Nginx split

Локальный шаблон nginx:

```
home/seoadvanced/direct/deploy/nginx-direct-location.conf
```

Критичные маршруты:

| Prefix / exact route | Upstream |
|----------------------|----------|
| `/direct/automation/content` | `127.0.0.1:5021` |
| `/direct/api/content-editor/` | `127.0.0.1:5021` |
| `/direct/automation/copy` | `127.0.0.1:5022` |
| `/direct/api/copy_` | `127.0.0.1:5022` |
| `/api/v1/copy/` | `127.0.0.1:5022` |
| `/direct/automation/slepki` | `127.0.0.1:5023` |
| `/direct/api/slepki/` | `127.0.0.1:5023` |
| `/direct/automation/accounts` | `127.0.0.1:5024` |
| `/direct/api/overview` | `127.0.0.1:5024` |
| `/direct/api/account_stats` | `127.0.0.1:5024` |
| `/direct/api/balance` | `127.0.0.1:5024` |
| `/direct/api/accounts_otkrut` | `127.0.0.1:5024` |
| `/direct/api/statuses` | `127.0.0.1:5024` |
| `/direct/api/ai/` | `127.0.0.1:5026` |
| `/direct/api/ar/` | `127.0.0.1:5027` |
| `/direct/autorules` | `127.0.0.1:5027` |
| остальное `/direct/` | `127.0.0.1:5020` |

`direct-gateway.service` (:5025) — внутренний loopback-брокер кук/токенов. Его нельзя
проксировать наружу через nginx.

---

## Как проверять после правок

### Локально

```bash
node --check home/seoadvanced/static/direct/automation.js
node --check home/seoadvanced/static/direct/automation_create.js
node --check home/seoadvanced/static/direct/automation_jobs.js
node --check home/seoadvanced/static/direct/automation_rules.js
node --check home/seoadvanced/static/direct/automation_content.js
node --check home/seoadvanced/static/direct/slepki_ui.js
node --check home/seoadvanced/static/direct/slepki_minus_places.js
node --check home/seoadvanced/static/direct/copy_common.js
node --check home/seoadvanced/static/direct/copy_auto.js
node --check home/seoadvanced/static/direct/copy_other.js
python3.12 -m py_compile \
  home/seoadvanced/direct/main.py \
  home/seoadvanced/direct/copy_main.py \
  home/seoadvanced/direct/content_main.py \
  home/seoadvanced/direct/slepki_main.py \
  home/seoadvanced/direct/accounts_main.py \
  home/seoadvanced/direct/ai_main.py \
  home/seoadvanced/direct/autorules_main.py
```

### Nginx на LXC101

```bash
ssh proxmox-ts "pct exec 101 -- nginx -t"
ssh proxmox-ts "pct exec 101 -- nginx -T 2>/dev/null | grep -nE 'direct/(automation|api)|127\\.0\\.0\\.1:502[0-7]'"
```

### Smoke URL

Без авторизованной сессии нормальный результат — редирект на логин:

```bash
curl -I https://seoadvanced.ru/direct/automation
curl -I https://seoadvanced.ru/direct/automation/copy
```

Ожидаемо: `302 Location: /login` либо `200` при валидной сессии.

---

*Обновлено 2026-07-22: учтён split JS на `automation_create.js`/`automation_jobs.js`, lazy-модули правил/контента и актуальный чеклист проверки.*

# UI-карта: seoadvanced.ru/direct/automation

> Цель файла: однозначно сказать «где лежит какой файл» — чтобы не тратить время
> на md5-сверку не того файла и не удивляться «в index.html этого нет».

---

## Маршрут и цепочка рендеринга

```
GET /direct/automation
    └── routes_pages.py : automation()          # регистрирует декоратор @access
            └── automation_runtime.py : _render_page()
                    └── render_template("direct/index.html", ...)
                            └── home/seoadvanced/templates/direct/index.html   ← ВОТ НАСТОЯЩИЙ ШАБЛОН
```

Точка входа Flask — `direct/main.py:create_app()`, строка:
```python
template_folder=str(ROOT / "templates")   # ROOT = home/seoadvanced/
```

Маршрут зарегистрирован через `register_page_routes(bp, ...)` в `blueprint.py` (строка 47).

---

## Таблица: файл — за что отвечает

| Файл | За что отвечает | Ключевые строки |
|------|----------------|-----------------|
| `direct/routes_pages.py` | Flask route `/direct/automation` → вызов `render_page()` | строка 16-20 |
| `direct/automation_runtime.py` | `_render_page()`: собирает контекст Jinja (audiences, feeds_catalog, is_admin) и вызывает `render_template` | строки 633-641 |
| `templates/direct/index.html` | Весь HTML + **весь JavaScript** страницы (8086 строк, ~624 KB). Там кнопки, формы, SPA-логика, API-вызовы. Нет ни одного внешнего JS-файла | — |
| `templates/direct/index.html` (inline `<script>`) | Единственный блок JavaScript — начинается на **строке 1384**, продолжается до строки 8084. Содержит: `createSet()`, `createDraftsFromSlepok()`, `createDrafts()`, `startCreateJob()`, опрос статуса, отмену, ремонт | L1384–8084 |
| `direct/routes_set_plan.py` | Flask route `POST /direct/api/set_plan` — предпросмотр плана набора | строка 9 |
| `direct/create_set_plan.py` | Бизнес-логика построения плана (`_set_plan_response`, `_emit_struct`, structure_to_campaigns) | — |
| `direct/routes_jobs.py` | Flask routes: `create_set_async`, `create_set_status`, `create_set_cancel`, `create_set_feed_decision`, `create_jobs` | строки 63, 210, 280, 337, 405 |
| `direct/routes_create_set.py` | Flask routes: `create_set` (legacy), `create`, `create_set_verification`, `create_set_repair` | строки 28, 34, 40, 66 |
| `direct/create_set_orchestrator.py` | Оркестрация создания набора: `create_set_response()`, routing по v501/cookie/UAC | строка 29 |
| `direct/campaign.py` | v501 API-клиент (ЕПК, баллы/units), UAC-клиент (tp6/tp7), куки главпотока | — |
| `direct/grid_create.py` | Cookie-path: `create_full()`, `create_shopping_full()` — создание без баллов | — |
| `direct/grid_finalize.py` | Grid-докрутка: места показа, ассеты, товарка, листинги | — |
| `static/style.css` | Единственный статический ресурс, подключённый на странице | L10 в index.html |
| `static/app.js` | НЕ используется на /direct/automation | — |
| `static/work-dashboard.js` | НЕ используется на /direct/automation | — |

---

## Поток создания набора (кнопка "Создать набор РК")

```
templates/direct/index.html L717
    <button onclick="createSet()">Создать набор РК</button>

createSet()  [L4585 в index.html]
    1. POST /direct/api/set_plan        → routes_set_plan.py → create_set_plan.py._set_plan_response()
       (строит план: список кампаний для набора, предупреждения о фидах)

    2. POST /direct/api/create_set_async → routes_jobs.py:api_create_set_async()
       (ставит джобу в очередь; воркер create_set_orchestrator.create_set_response() делает реальные API-вызовы)

    3. GET  /direct/api/create_set_status  [опрос каждые ~3с]
       → routes_jobs.py:api_create_set_status()

    4. POST /direct/api/create_set_cancel  [по кнопке отмены]
       → routes_jobs.py:api_create_set_cancel()

    вспомогательные:
    - POST /direct/api/create_set_feed_decision  → routes_jobs.py  [решение по фидовому попапу]
    - GET  /direct/api/create_jobs               → routes_jobs.py  [список текущих джоб]
    - POST /direct/api/create_set_repair         → routes_create_set.py  [ремонт после проверки]
```

Также есть кнопки:
- `createAndPublish()` (L718) — тот же flow + параметр `publish:true`
- `createDraftsFromSlepok()` (L719) — flow без live-LLM, контент из БД слепков
- `createDrafts()` (L4652) — быстрый вариант из набора

---

## Где РЕАЛЬНО живёт логика создания набора (и чего нет в index.html)

Шаблон `templates/direct/index.html` содержит **только клиентскую часть** (UI-логику):
формы, кнопки, SPA-маршрутизацию и fetch()-вызовы к API.

Серверная логика разбита по модулям:
- Планирование: `create_set_plan.py` (читает структуру слепков, строит список РК)
- Оркестрация: `create_set_orchestrator.py` (выбирает путь v501/cookie/UAC)
- Создание через API Директа: `campaign.py`, `grid_create.py`, `grid_finalize.py`
- Регистрация Flask-routes: `routes_set_plan.py`, `routes_jobs.py`, `routes_create_set.py`

---

## Как проверять после деплоя

### ВАЖНО: md5 index.html НЕ гарантирует что вся логика проверена

Страница `/direct/automation` не имеет внешних JS-файлов. Логика создания набора
целиком находится в шаблоне Jinja:

```
templates/direct/index.html   ← ЕДИНСТВЕННЫЙ файл с JS-логикой
```

Если ищешь логику создания набора и не находишь её в `index.html` (как было при
описанном инциденте с md5-сверкой) — значит сравнивался **не тот файл**.

Правильный путь к шаблону:
```
home/seoadvanced/templates/direct/index.html   (NOT home/seoadvanced/direct/index.html)
```

В корне `home/seoadvanced/direct/` есть `._index.html` — это macOS AppleDouble
metadata-файл (сайдкар для xattr), не сам шаблон. Его md5 не несёт смысла.

### Чеклист деплоя

Мutagen синкает **все файлы** Mac -> LXC101 (исключение только `.secret/`). Шаблоны и
серверные `.py`-файлы синкаются автоматически.

После правок в шаблоне или серверном коде:
```bash
ssh lxc101-ts "systemctl restart direct-create direct-create-worker"
```

Smoke-тест:
```
curl -I https://seoadvanced.ru/direct/automation   # должен редиректить (302) или отдавать 200
```

Правильная сверка по файлам-источникам логики:

| Что изменилось | Файл для сверки md5 |
|----------------|---------------------|
| Кнопки/HTML/CSS на странице | `templates/direct/index.html` |
| JS-логика (createSet, set_plan flow) | `templates/direct/index.html` (L1384–8084) |
| Планирование набора | `direct/create_set_plan.py` |
| Оркестрация создания | `direct/create_set_orchestrator.py` |
| Регистрация маршрута /automation | `direct/routes_pages.py` |
| Регистрация API create_set_async/status | `direct/routes_jobs.py` |
| Flask entry point, template_folder | `direct/main.py` |

---

## Сводка: нет внешних JS-файлов

```
/static/app.js            — используется digest.service (главная seoadvanced), НЕ direct
/static/work-dashboard.js — используется work.service, НЕ direct
```

На странице `/direct/automation` нет ни одного `<script src="...">`.
Весь JavaScript — inline, в одном `<script>` блоке шаблона.

---

*Создан 2026-07-16 на основе фактического чтения кода.*
*Если структура проекта изменится (добавятся внешние JS-файлы, изменится template_folder) — обновить этот файл.*

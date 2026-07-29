# Автоматизация Директа — `direct/`

Веб-модуль для seoadvanced.ru: создание кампаний в Яндекс.Директе и **ИИ-генерация
промоакций в стиле реальных директологов** (через локальную LLM на M3).

> 🗺 **Карта пакета — [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md):** слои, инвентарь модулей,
> граф импортов, план разбиения `blueprint.py`. Смотреть ПЕРЕД правкой незнакомого места
> и для impact-анализа.

- **Маршруты:** `https://seoadvanced.ru/direct/automation`,
  `https://seoadvanced.ru/direct/automation/content` и
  `https://seoadvanced.ru/direct/automation/slepki` (структура слепков — **своя страница
  своего процесса**, не вкладка внутри `/direct/automation`).
- **Сервисы:** отдельные Flask-процессы на **LXC 101** (`192.168.0.202`), юниты — в `deploy/`:
  `direct-create.service` на `127.0.0.1:5020` обслуживает создание/управление РК
  (+ `direct-create-worker.service` — исполнитель очереди создания, без порта),
  `direct-content.service` на `127.0.0.1:5021` обслуживает только редактор контента,
  `direct-slepki.service` на `127.0.0.1:5023` — редактор структуры слепков
  (страница `/direct/automation/slepki` + 13 эндпоинтов `/direct/api/slepki/*`),
  `direct-slepki-worker.service` — исполнитель edit-очереди слепков (без порта).
- **Главный сайт:** `digest.service` остаётся на `5010`; `/direct/*` он больше не обслуживает.
- **Деплой Direct:** Mutagen синкает Mac↔LXC101 автоматически. После правок общего
  Direct — `systemctl restart direct-create.service`; после правок редактора контента —
  `systemctl restart direct-content.service`; после правок редактора слепков
  (`slepki_editor.py` / `routes_slepki_edit.py` / `slepki_ui.js`) —
  `systemctl restart direct-slepki.service` и `direct-slepki-worker.service`. Команды:
  `ssh proxmox-ts "pct exec 101 -- systemctl restart direct-create.service"`,
  `ssh proxmox-ts "pct exec 101 -- systemctl restart direct-content.service"`,
  `ssh proxmox-ts "pct exec 101 -- systemctl restart direct-slepki.service direct-slepki-worker.service"`.

Папка **самодостаточна**: `blueprint + campaign.py + promo.py + ai_agents.py + *.json`.
Нужен доступ к `.secret/loader.py` выше по дереву (куки главпотока, токены Директа/Метрики, БД).

### Разделение кода

- `routes_*.py` — Flask route-слой `/direct/*`. `blueprint.py` остаётся местом для legacy
  helper/adapter-логики, но больше не содержит `@bp.route`.
- `main.py` — entrypoint общего Direct app. В systemd он запускается как
  `direct-create.service` с `DIRECT_REGISTER_CONTENT_EDITOR=0`, чтобы не держать content editor
  в процессе создания РК.
- `content_main.py` — entrypoint отдельного content editor app: только
  `/direct/automation/content` и `/direct/api/content-editor/*`.
- `routes_create_set.py` — `/api/create_set`, legacy `/api/create`, verification/repair endpoints.
  Важно: `/direct/api/create_set` должен всегда смотреть на endpoint `direct.api_create_set`;
  это закреплено smoke-тестом `tests/test_routes.py`.
- `routes_content_editor.py` — отдельный редактор `/direct/automation/content` для массовой правки
  AI-сгенерированного контента. Direct API v5 вызывается только с валидными top-level полями:
  campaigns без subtype `TextCampaignFieldNames`, `adgroups.get`/`ads.get` батчами по 10
  `SelectionCriteria.CampaignIds`, responsive ads через `Titles`/`Texts`.
  Отдельная документация сервиса: `CONTENT_EDITOR.md` и `CONTENT_EDITOR_COOKIE_GRID.md`.
- `create_set_*.py` — вынесенные части create_set orchestration: context, feeds, minus, assets,
  text/tp1/feed builders, finalize, repairing, corrections, master-product ветка и
  `create_set_tp8_10.py` для «Посевов» (GdPostCampaign tp8/tp9/tp10).
- `precreate.py` — план и исполнение предсоздания перед upload-циклом (`precreate` report,
  promo, callouts-reuse, будущие минус-библиотеки/изображения/AI-content).
- `verifier.py` / `live_verifier.py` / `uac_verifier.py` / `grid_content_verifier.py` /
  `campaign_state_verifier.py` / `verification_service.py` — статическая и live-проверка
  результата создания, UAC-инварианты tp6/tp7, Grid content checks tp1-tp5, проверка имени/state,
  repair plan без ручного захода в интерфейс.
- `repair_planner.py` / `repair_executor.py` / `repair_gate.py` / `repair_auto.py` — scoped repair
  после создания.
- `grid_finalize.py`, `grid_create.py`, `grid_read.py`, `uac_read.py` — cookie/Grid/UAC слой,
  чтобы экономить Direct API units.

Важно: текущая приватная Grid-схема не принимает `AddCallouts`
(`UnknownType GdAddCalloutsInput`), поэтому `GridClient.add_callouts(...)` безопасно
переиспользует существующие callouts и не валит создание. Для реального создания новых callouts
нужен актуальный Grid HAR или осознанный v5 fallback.

Live verification для UAC/tp6-tp7 использует cookie/UAC detail и проверяет не только наличие
кампании, но и draft-status, Метрику/цель, tracking params/UTM, контент 5/3/8, медиа, фид для tp7
и выключенные `alternative_texts_enabled` / recommendations flags.
Эти UAC-инварианты собраны в `uac_verifier.py`, чтобы их можно было править отдельно от общего
Grid/v5 live-verifier orchestration.

Для UAC/tp6-tp7 режим оплаты сверяется с именем кампании: `*_cpc_*` должен быть `PER_CLICK`,
`*_cpa_*` должен быть `PER_CONVERSION` или `PER_ACTION`. Несовпадение даёт
`UAC_PRICING_MISMATCH` и repair через cookie/Grid без Direct API units.

Для UAC/tp6-tp7 live verification также требует заполненные регионы и выключенные Карты.
Отсутствие гео даёт `UAC_REGION_MISSING`, включённые карты — `UAC_MAPS_ENABLED`; оба случая
уходят в cookie/Grid repair без Direct API units.

Для UAC/tp6-tp7 проверяется недельный бюджет: `week_limit` должен быть больше нуля, а
`limit_period` должен быть `week`. Ошибки `UAC_BUDGET_MISSING` и
`UAC_LIMIT_PERIOD_MISMATCH` планируются как cookie/Grid repair без Direct API units. Если
`week_limit` отсутствует в detail, verifier не создаёт false-positive.

Для tp7 с конкретным модельным `ct####` live verification дополнительно требует товарный
`model`-фильтр. Vendor-only фильтр (`Производитель содержит ...`) считается ошибкой
`UAC_PRODUCT_MODEL_FILTER_MISSING`, потому что товарное объявление иначе идёт по всей марке, а не
по нужной модели.

Для tp1-tp5 Grid live verification читает имена групп. Пустые имена, `None/null/undefined` и
кодеры с голым хвостом `—` дают `ADGROUP_NAME_MISSING`; repair planner отправляет это в
`rebuild_missing_content` через cookie/Grid без Direct API units.
Эти tp1-tp5 проверки вынесены в `grid_content_verifier.py`.

Проверка live имени и архивного состояния кампании вынесена в `campaign_state_verifier.py`:
`NAME_MISMATCH` планирует `rename_campaign`, `CAMPAIGN_ARCHIVED` планирует recreate через
cookie/Grid без Direct API units.

Static verification не требует `callouts` для UAC/Post-only наборов (`tp6_`/`tp7_`/`tp8_`/`tp9_`/`tp10_`):
для tp6/tp7 достаточность проверяется UAC-detail, для Посевов — Grid/Post-result. Для смешанных
наборов и tp1-tp5 неподтвержденные callouts по-прежнему дают `CALLOUTS_NOT_CONFIRMED`.

Если UAC live verification находит плохой черновик (`UAC_NOT_DRAFT`, нет счетчика/цели/UTM,
включена персонализация или рекомендации, неполный контент), repair planner строит
`resume_or_recreate_campaign` без Direct API units. Repair gate помечает такой draft как
`uac_replace_campaigns`: перед queued recreate он должен быть удалён через cookie/UAC, чтобы
resume-skip не пропустил существующую плохую кампанию по имени.

Если UAC detail временно не прочитался (`UAC_DETAIL_SKIPPED`), repair planner сначала строит
`retry_live_verification` через cookie/Grid без Direct API units. Пересоздание допускается только
после повторной проверки, когда есть конкретный дефект кампании.

---

## Доступ (auth)

`_direct_access = _service_required_any("work", "work:direct")` (см. `auth.py`).
Админ — bypass; обычный юзер — нужен сервис-ключ `work` или `work:direct` в сессии.
Раздел зарегистрирован в `_BUILTIN_SECTIONS` (`app.py`) и в навигации (`_nav.html`).

---

## Источники данных

| Что | Откуда |
|-----|--------|
| Аккаунты (домен/салон/город/тип сайта/логин/директолог/статус) | Victory БД `public.local_gsheet_sites` (`direction='Авто'`) |
| **Счётчики Метрики + цель «Все формы»** | Victory БД `public.metrika_goals` (ключ `account_login`=`login_key`) |
| Шаблонные тексты объявлений (фолбэк) | Victory БД `public.direct_ad_templates` (по `site_type`×`kind`) |
| **Контент слепка** (campaign/promo) — фолбэк при недоступном M3 | Victory БД `public.direct_slepok_content` (`slepok`×`site_type`×`kind`) |
| **Нативные интересы** (аудитории по группам) для МК/Товарки | Victory БД `public.direct_slepok_audiences` (`slepok`×`site_type`×`tp`×`category`×`kind`→`interest_ids`) |
| **Глобальные правила** (бюджет/CPA/корректировки/фиды/минус-площадки/минус-марки) | Victory БД `direct_automation_rules`, `direct_audience_corrections`, `direct_demographic_corrections`, `direct_global_feed_rules`, `direct_global_minus_places`, `direct_global_minus_marks` |
| Структура слепков (кодеры кампаний `tpN_{cpc\|cpa}_{site\|kviz}`) | `direct/slepki/<key>.json` (по файлу на слепок) + `direct/slepki/_order.json`; собирается `slepki_store.assemble()`. Монолита `slepki_structure.json` больше нет |
| Структура Посевов | `direct/slepki/posevy.json`; create-tab и `/slepki` читают один источник через `SLEPKI`, без hardcode `1 кампания` |
| Баланс / блокировки / ассеты | Яндекс.Директ API v5 (OAuth) + Live v4 + Grid (куки) |
| Локальная LLM | mlx_lm.server на Mac M3 (через обратный SSH-туннель, см. `_M3_LLM_URL`) |

### Счётчик/цель из `metrika_goals` (важно)
- `counter_ids` (text-массив `[id, id]`) → если в таблице сайтов `counter_number` пуст,
  счётчик берём отсюда. Один → подставляем, несколько → **выпадающий список** в таблице.
- `all_forms` (bigint) → это `goal_id` цели «Все формы» для создания РК.
- Приоритет: `counter_number` (таблица сайтов) → иначе первый из `counter_ids`.
- Хелперы: `_parse_counter_ids`, `_metrika_goals_for` в `blueprint.py`.

---

## Эндпоинты

Страница: `GET /` (редирект) · `GET /automation`.

**Аккаунты/данные:** `/api/accounts` (+`mg_counters`/`mg_goal` из metrika_goals) ·
`/api/account_info` · `/api/statuses` · `/api/account_prefill` (counter/goal из metrika_goals) ·
`/api/account_assets` · `/api/balance` (POST) · `/api/check_blocks` (POST) ·
`/api/campaigns` · `/api/campaigns/stop_all` (POST) · `/api/goal_for_counter`.

**Создание РК:** `/api/feeds` · `/api/audiences` · `/api/ad_template_sites` ·
`/api/ad_templates` · `/api/set_plan` (POST, предпросмотр набора; в ответе `feed_alert`
если нет /yandex.xml) · `/api/create_set` (POST, набор черновиков) · `/api/create` (POST, одна РК) ·
`/api/create_set_feed_decision` (POST, решение по feed_alert) · `/api/minus-marks` (GET/POST,
глобальное правило «Минус марки (фид)» — выбранные марки исключаются NOT_CONTAINS-фильтром
фида во всех товарных/фид-кампаниях).

**Локальная ИИ:** `/api/ai/status` · `/api/ai/chat` (POST, чат) ·
`/api/ai/agents` · `/api/ai/promo/generate` (POST) · `/api/ai/promo/publish` (POST) ·
`/api/ai/campaign/generate` (POST, контент РК: заголовки/тексты/быстрые ссылки).

---

## ИИ-агенты «слепки директологов» (`ai_agents.py`)

Агент = стиль реального директолога (из `work/slepki_direktologov/corpus`). Генерируют
**промоакции** и **контент РК** (заголовки/тексты/быстрые ссылки); архитектура расширяема
на ключи/минус-слова.

| Агент | type | размер | акцент |
|-------|------|--------|--------|
| `Слепок_Павлов` | DISCOUNT | до 0.8–1.5 млн ₽ | кредит, «у дилера», финансы |
| `Слепок_Крючкова` | PROFIT | 40–57% | «выгода», «распродаём склад» |
| `Слепок_Щербакова` | DISCOUNT | 40–50% | «новые по цене б/у», господдержка |
| `Слепок_Терехов` | DISCOUNT | 45–63% | авто с пробегом + автокредит |

- **Site-type-aware:** `SITE_TYPE_PROFILE` меняет содержание промо под тип сайта
  (С пробегом / Монобренд / Мультибренд / Мульти+БУ / Квиз) — стиль одного агента
  отличается по типу сайта.
- `build_promo_messages(agent, ctx, avoid)` → промпт (строгий JSON). `avoid` —
  список ранее выданных описаний для «Сгенерировать снова».
- `examples` в каждом агенте — **few-shot для M3, НЕ публикуются** (реальные из корпуса).

### Лимиты промо (зашиты и валидируются на сервере)
| Поле | Ограничение |
|------|-------------|
| `type` | DISCOUNT/PROFIT/CASHBACK/GIFT/FREE/INSTALLMENT |
| `amount` | целое >0, ≤100 (PCT) / ≤1 000 000 (RUB) |
| `unit` | PCT / RUB · `prefix` TO/FROM |
| `description` | **≤25 симв. RAW** (считаются ВСЕ символы — и пробелы, и пунктуация; лимит грида Яндекса); с маленькой буквы; запрещено «закрытие автосалона» (`BANNED_SUBSTR`) |
| `promocode` | ≤16, опц. · `finishDate` — **не указываем** |

`Name` Директ собирает сам из типа+размера+описания. Дата — не задаётся (промо показывается всегда).

### Поток генерации/публикации (UI: вкладка «Статистика», колонка «Промоакции»)
1. Выбрать аккаунт → агента → **Сгенерировать** (`/api/ai/promo/generate`).
2. Превью + редактируемые поля (счётчик символов `N/25`, лимит RAW грида).
3. **🔄 Сгенерировать снова** (варьирует описание И размер — `avoid`/`avoid_amounts`,
   temp 0.85→1.05) ИЛИ **✅ Опубликовать в аккаунт** (с подтверждением).

### Публикация (`promo.py`)
- Офиц. `promotions.add` **заблокирован** (код 8000, ЕРИР) → создаём через приватный
  **grid/api `addPromoExtensions`** на куках главпотока (`cmc.UacClient`).
- ⚠ В URL grid **обязателен** `?ulogin=<login>` — иначе промо уйдёт в агентский кабинет.
- После создания — верификация офиц. `promotions.get` у клиента. Создаём **в библиотеке**
  (без привязки к РК; `PromoClient.attach` есть, но в UI пока не используется).
- В `create_set`/repair автосоздание промо использует только явно выбранный слепок (`agent`:
  canonical key или `Слепок_*`). Фамилия директолога из аккаунта не используется как fallback.
- Перед привязкой промо сверяется с контентом набора: если в промо и текстах есть проценты,
  набор процентов должен совпадать полностью. Пример `промо 30%` + `контент 45%` или
  `контент 30% и 45%` отклоняется, чтобы оффер не расходился.

### Контент РК — заголовки/тексты/быстрые ссылки (вкладка «Автоматическое создание РК»)

Агент генерит контент объявлений **под каждую РК набора** (свой комплект на кампанию).

- Поток: выбрать аккаунт → набор (мастер/товарные, авто/ручная) → **выбрать агента** →
  «Создать набор РК» (план) → на каждой РК кнопка **✨ контент** (или «для всех») →
  редактируемое превью с счётчиками символов → «Создать выбранные черновики».
- Объём на РК: **tp6/tp7 = 5 заголовков**, остальные `tp` = **7 заголовков**;
  тексты = **3**, быстрые ссылки = **8**.
- Лимиты (валидируются `ai_agents.validate_campaign`, доки Директа):
  заголовок ≤56 (слово ≤22) · текст ≤81, ≤15 знаков преп. (слово ≤23) ·
  быстрая ссылка: заголовок ≤30, описание ≤60, **без `! ? [ ]` и эмодзи**.
- Заголовки обязаны содержать цифру; M3-строки и fallback-строки без цифр отбрасываются.
- Тексты добиваются до нижнего порога `68+` символов, чтобы фронт и боевой payload не получали
  пустые/слишком короткие варианты после фильтров.
- **Быстрые ссылки → на главную** (`_norm_sitelinks` ставит href аккаунта), агент пишет
  только заголовок+описание; после фильтров есть финальная fallback-добивка до `8` ссылок.
- Few-shot — **реальные** заголовки/тексты/ссылки из корпуса (`ads.jsonl`/`sitelinks.jsonl`),
  `AGENT_ADS` в `ai_agents.py`. Не публикуются дословно — образец стиля + безопасная добивка, если
  модель вернула меньше нужного (тогда добивается из корпуса, с пометкой в `warnings`).
- `/api/create_set` принимает `titles/texts/sitelinks` в каждом `item`; нет ИИ-контента →
  builder может использовать локальные шаблоны/пак. Stream-generation не копирует
  `direct_slepok_content` как финальный live-контент; полностью мёртвый content-пайплайн
  блокируется до Direct-мутаций.

### Отладка качества M3

- Ответ `/api/ai/campaign/generate` и внутренний `_gen_campaign_content(...)` теперь возвращают
  `m3_debug`:
  - `raw_counts` — сколько сырых вариантов пришло от 14B;
  - `accepted_before_assemble` — сколько прошло ранние фильтры;
  - `accepted_examples_before_assemble` — примеры того, что прошло;
  - `retry_used` — запускались ли retry по заголовкам/текстам/ссылкам и был ли 72B fallback;
  - `rejects` — счётчики причин отбраковки;
  - `reject_examples` — до 3 реальных примеров строк на каждую причину.
- Для быстрой локальной проверки фильтров без Flask/M3/БД есть smoke-script:

```bash
python3 scripts/m3_content_filter_smoke.py
```

- Скрипт проверяет регрессии по:
  - site-fit (`новые` vs `с пробегом`);
  - обязательности бренда;
  - обязательности УТП;
  - разнообразию УТП в наборе заголовков;
  - обязательности CTA в текстах;
  - отсеву абсурдных платежей;
  - смысловому дедупу;
  - дедупу быстрых ссылок по `title`/`description`.

---

## Создание набора РК по структуре слепка (Нейродиректолог)

Кампании создаются **по структуре выбранного слепка** — на диске **17 слепков**
(`direct/slepki/*.json`, порядок в `_order.json`), включая `dmp` / `gen_ses` / `avto_sk` /
`avtolajt_bu` / `sk_krs`. Слепок «Кудерко» **удалён** из выбора — его тексты оставлены
только как эталон **полноты/длины** контента (кол-во символов), НЕ как источник содержания.

### Страница «Структура слепков» (`/direct/automation/slepki`)
Отдельная страница отдельного процесса `direct-slepki.service` (:5023):
`templates/direct/slepki.html` + `static/direct/slepki_ui.js` + `static/direct/slepki_ui.css`.
`templates/direct/index.html` только ссылается на неё и подключает тот же JS/CSS.

- Дерево кодеров `tp{1-7}_{cpc|cpa}_{site|kviz}`; каждый `tp` **разбит по типу сайта**
  (`site` / `kviz`) — отдельные секции (`t.splits` в part-файле слепка `direct/slepki/<key>.json`).
- «Прочее (без tp-схемы)» из структуры удалено.

### Вкладка «Создание РК» → блок «Кампании в наборе» (динамический)
- Чекбоксы строятся **из структуры слепка** (`renderAcSet()`), полная замена прежних
  5 статичных. По кодеру: `AC_TP_BUILD={2:[search_test], 6:[master_auto,master_manual], 7:[product_auto,product_manual]}`.
- `selectedEngineVariants()` → выбранные кодеры в engine-варианты; `tpSqMap()` → `{tp:[site/kviz]}`
  из суффиксов кодеров. `set_plan`/`create_set` принимают `agent` (slepok) и `tp_sq`.

### Что подставляется при создании (`create_set`)
| Что | Правило |
|-----|---------|
| Лендинг | `kviz` → домен + **`/quiz`**; `site` → домен |
| Нативные интересы (МК/Товарка) | из `direct_slepok_audiences` (`_slepok_audiences_for`), `audience_interest_type=short-term` |
| Промо | из **списка промо аккаунта** (`promotions.get` → `PromoClient.attach`), первый доступный |
| Контент | M3 (если доступен) → фолбэк `direct_slepok_content`/`direct_ad_templates` |
| Картинки / ключи / минус-слова | **TODO** — источник укажет пользователь позже |

### Глобальные правила → корректировки (только tp1–tp5)
При создании поисковых кампаний (`tp1–tp5`, сейчас `tp2`) автоматически применяются
корректировки из вкладки **«Глобальные правила»** (`_load_corrections` по городу аккаунта,
фолбэк на глобальные `'*'`):
- **Демография** — `DemographicsAdjustments` (возраст/пол), `bidmodifiers.add`.
- **Аудитории** — `RetargetingAdjustments`, матчинг по `_seg_key`: `geo_X → <город>_X`,
  `self_X → self_X`; вешаются только при `pct≠0` и если сегмент есть на аккаунте.
- **Бюджет + CPA по типу оплаты** — из настроек стратегии (`_rule_sets`).
- МК (tp6) / Товарка (tp7) корректировок **не получают** (по решению пользователя).
- Хелперы: `_load_corrections`, `_account_retargeting`, `_seg_key`, `_correction_bidmodifiers`,
  `_apply_corrections`. ⚠ `bidmodifiers.get` требует `Levels:['CAMPAIGN']` в SelectionCriteria.

### Глобальные правила → фиды
Во вкладке **«Глобальные правила»** есть режим **«Фиды»**: глобальный allow-list сервиса,
не привязанный к конкретному аккаунту. UI показывает чекбоксы, название фида, URL и статус.

API:
- `GET /direct/api/feed-rules`
- `POST /direct/api/feed-rules`

Хранение: `public.direct_global_feed_rules`.

При создании фидовых кампаний сервис использует только выбранные XML. Если в аккаунте есть другие
фиды, они не берутся как случайный fallback. Фильтр применяется в `_first_url_feed`, `_catalog_feed`,
`_account_model_feeds`, `_tp5_account_data`, а также cookie fallback для `tp3/tp5`.

### Глобальные правила → минус-площадки (РСЯ)
Во вкладке **«Глобальные правила»** есть режим **«Минус-площадки»**: глобальный список запрещённых
площадок РСЯ (textarea, одна площадка на строку, тёмная тема + список справа). URL сохраняются в
**нижнем регистре**.

API:
- `GET /direct/api/minus-places`
- `POST /direct/api/minus-places` (replace-all; guard: нет ключа `places` → 400; пустой при непустой
  таблице → 409 `needs_confirm`, нужен `confirm_clear=true`)

Хранение: `public.direct_global_minus_places`.

Применение: список включённых площадок (`_enabled_minus_places`) ставится в `disabledPlaces` ВСЕХ
кампаний **tp1 (РСЯ)** — и на создании (`build_unified_campaign`), и в финализации (`_finalize_rsya`).

### Инварианты создания (обязательны)
6 обязательных настроек при КАЖДОМ создании РК — см. **[CAMPAIGN_INVARIANTS.md](CAMPAIGN_INVARIANTS.md)**
(метрика+цель, UTM, персонализация ВЫКЛ, мониторинг ВКЛ, расш.гео ВЫКЛ, «Директ помогает» ВЫКЛ).
Гейт `api_create_set`: нет счётчика/цели → `400`. Эталон проверки — `porg-psm5h7q6`.

---

## Файлы

| Файл | Назначение |
|------|-----------|
| `blueprint.py` | все эндпоинты, БД, валидация промо, корректировки, набор РК, прокси к M3 |
| `ai_agents.py` | профили **4** агентов (Кудерко удалён), `SITE_TYPE_PROFILE`, лимиты, промпт-билдер |
| `promo.py` | `PromoClient` — публикация/привязка промо через grid/api |
| `campaign.py` | **re-export хаб** (437 строк): cookie-store, `_AGENCY_RESOLVER`, io; ре-экспортирует полный namespace для 30+ импортёров — API-движки вынесены в модули ниже |
| `direct_v501_client.py` | **DirectV501Client** + `UnifiedCampaignSpec` + `build_v501_client` — v501/ЕПК API (tp1–tp5 по OAuth); `ShoppingAd`/`ListingAd`/`TextAd`; `add_shopping_ad`/`add_listing_ad`/`add_text_ad` |
| `uac_client.py` | **UacClient** + `MasterCampaignSpec` + `UTM_TEMPLATE`/`USER_AGENT` + image/audience/sitelink-хелперы — UAC/Мастер/Товарка (tp6/tp7 на куках) |
| `grid_finalize.py` | Grid-докрутка ЕПК (tp1–tp5): `GridClient.finalize` (места показа, наследуемые ассеты, промо, minus-set, инварианты), `set_default_text` (тексты ShoppingAd), `apply_corrections` (корректировки v5 ПОСЛЕ Grid) |
| `grid_create.py` | **куки-движок создания/удаления без баллов** (Grid web-api): `GridCreateClient` (`add_campaign`/`add_adgroups`/`add_ads`/`add_shopping_ads`/`delete_campaigns`), оркестраторы `create_full` (tp1/2/4), `create_shopping_full` (tp3/5), in-place добивка `add_text_content_to_existing`/`add_shopping_content_to_existing`; ре-экспортирует payload-фабрики из `grid_create_payloads`. Реверс из HAR14/16/17 |
| `grid_create_payloads.py` | чистые payload-фабрики Grid: `build_unified_campaign`/`build_adgroup`/`build_ad`/`build_shopping_ad` + `_fill_titles`/`_fill_bodies`/`_dedup_keep`/`_safe_old_price`/`_PLATFORMS_OFF`/`_campaign_minus_kw` |
| `grid_read.py` | read-only Grid-снимки без баллов Direct: фактические счётчики adGroups/ads по campaign ids для live verification |
| `uac_read.py` | read-only UAC-снимки tp6/tp7 без v5 units: детали кампании через `/web-api/uac/campaign/{id}` и нормализация счётчиков titles/texts/sitelinks/media/feed |
| `verifier.py` | статическая post-create проверка результата `create_set`: resolved body-инварианты, shape, id, кодер-имена, локальный build, промо/callouts, repair_candidates |
| `campaign_result.py` | pure-нормализация строк результата `create_set`: id/name/kind, вложенные `campaigns`, dedup, классификация `tp6/tp7` как UAC |
| `live_verifier.py` | read-only live-проверка сохранённой джобы: нормализует созданные campaign ids, сверяет факт в Grid/v5, по умолчанию предпочитает Grid/cookie из-за малого остатка баллов Direct |
| `local_result_verifier.py` | pure-проверки локального результата создания до live-read: `BUILD_ERROR`, `NO_ADGROUPS_REPORTED`, `NO_ADS_REPORTED`, `SEARCH_NOT_FINALIZED`, `GRID_FINALIZE_WARN`, `NAME_HAS_NULL_TOKEN` |
| `verification_service.py` | orchestration live verification без Flask: собирает Grid list, Grid content counts, UAC details и optional v5 в единый `live_verifier` report |
| `create_set_postprocess.py` | orchestration после создания набора: static verification, Grid-first live verification, repair-gate и безопасный post-create auto-repair без Flask |
| `create_set_precreate.py` | orchestration перед созданием набора: build precreate report + execute assets (promo/callouts/minus/images/content plan) с безопасным fallback |
| `create_set_promo.py` | post-create промо: привязка precreated promo, выбор пригодного промо аккаунта или создание нового по выбранному слепку с проверкой совпадения оффера с контентом |
| `create_set_callouts.py` | pure-helper результата уточнений: подтверждает только реально подготовленные callout ids и не обещает успех, если Grid-схема не дала ids |
| `create_set_resume.py` | pure-helper resume/skip: exact и fan-out matching имён, skip уже созданных кампаний, force recreate и выбор items для delayed/deferred repair |
| `create_set_units.py` | pure-helper лимита баллов Direct: распознаёт error 152/units в top-level и nested results, считает created/skipped/failed без ложного permanent-fail |
| `create_set_response.py` | pure-builder публичного JSON-ответа `create_set`: сохраняет контракт полей `verification/live/precreate/repair/units` |
| `create_set_metrika.py` | preparation/guard Метрики перед созданием: fallback счётчика/цели, `via_cookie+no_cpa` optional mode, foreign-owner check до campaigns.add |
| `create_set_input.py` | pure-нормализация входа `create_set`: login/items/agent flags, semantic-dedup callouts, single-feed фильтр без сетевых вызовов |
| `create_set_slepok_content.py` | применение campaign-контента из `direct_slepok_content`: ротация заголовков/текстов/ссылок по РК, UTP-unify и fallback note без сетевых вызовов |
| `create_set_account.py` | preparation аккаунта перед созданием: account context, site_type override, шаблоны, href, region_ids и hard-fail без контента/шаблонов |
| `precreate.py` | чистый pre-create planning слой: описывает ресурсы, которые должны быть готовы до загрузки РК (existing names, промо, callouts, минус-библиотеки, картинки, M3 content prefetch), без Flask/БД/Direct мутаций |
| `repair_planner.py` | чистый план добивки по verification/live issues: какие действия нужны, каким транспортом (`cookie_grid` first), тратят ли Direct units |
| `repair_gate.py` | чистая нормализация repair-gate: job context, truthy/jsonish, выбор исполнимых actions |
| `repair_executor.py` | **тонкий re-export фасад** (76 строк): ре-экспортирует публичный API всех repair-доменов; retry wiring — в `blueprint.py` |
| `repair_common.py` | `RepairDeps` + const/regex + алиасы `cmc`/`gc`/`gf` — общая база всех repair-доменов |
| `repair_content.py` | `execute_promo`/`execute_callouts`/`execute_rename`/`execute_content_repair` — ремонт контента/промо/уточнений |
| `repair_media.py` | `execute_images`/`execute_adprice`/`execute_default_text`/`execute_campaign_invariant`/`execute_images_forbidden_repair` — ремонт медиа и ценовых полей |
| `repair_keywords.py` | `execute_keywords_repair` + `execute_keywords_wrong_group_repair` — ремонт ключевых слов и ошибочных групп |
| `repair_auto.py` | orchestration-слой добивки: общий порядок executor-ов для repair endpoint, безопасная post-create автодобивка `promo/callouts/rename`, preflight/decision/queue orchestration и response contracts для repair без Flask/DB |
| `kontent_pack.py` | чтение контент-пака с M3 (`/opt/neuro_kontent/kontent_oktyabr`): ключи, уточнения, картинки, видео по `(segment, tp, ct, slepok)`; батч-сбор через ssh; `videos_for_login`; **видео-пул** `/Users/Shared/agency/Video/<ct>/*.mp4` (индекс `Video\|video\|<ct>`, `videos_pool_for_ct` — до 2 роликов; фолбэк в `videos_for_ct`). Видео идут в tp6/tp7 (content_ids) и tp1 (`_tp1_video_ads`: `upload_video_creative` → `meta.creative_id` → `creativeIds` в UpdateAdaptiveTextAds). **`PACK_MOUNT` переключаем через env `NEURO_PACK_MOUNT`** — на ЛОКАЛЬНУЮ копию пака (`/opt/neuro_content_local`, собирается ночным `scripts/sync_content_m3.py`, крон 03:00 Екб; видео ≤9.9МБ, картинки q80) → днём не зависим от M3 |
| `promotions.py` | референс-копия PromoClient из skill (не используется blueprint'ом — рабочий `promo.py`) |
| `slepki/<key>.json` + `slepki/_order.json` | структура слепков ПО ФАЙЛАМ (кодеры `tpN_*`, `splits` по site/kviz). Собирается `slepki_store.assemble()`; `_json("slepki_structure.json")` перехвачен → те же данные. Монолита нет |
| `slepki_store.py` | сборка структуры из part-файлов (`assemble`, кэш по сигнатуре) + атомарная запись изменившихся частей (`write_directologists`) |
| `seed_slepok_content.py` | сидер `direct_slepok_content` (фолбэк-контент по слепкам) |
| `CAMPAIGN_INVARIANTS.md` | 6 обязательных инвариантов создания РК + статус по коду |
| `*.json` | пресеты аудиторий / фидов / аккаунтов / Grid-шаблоны |

---

## Гибрид tp1–tp5: v501 каркас + Grid-докрутка

ЕПК-кампании (tp1–tp5) создаются **двухэтапно** (проверено live 2026-06-21/22):

1. **v501 каркас** (`campaign.py::DirectV501Client`): кампания со стратегией
   `AVERAGE_CPA` (cpc) / `PAY_FOR_CONVERSION` (cpa) + группы + объявления.
2. **Grid-докрутка куками** (`grid_finalize.py::GridClient.finalize`): одна мутация
   `UpdateCampaigns` — места показа (`placementTypes` → «Ручная настройка»),
   наследуемые уточнения/быстрые ссылки на кампании, промо, библиотечный minus-set,
   инварианты.
3. **Корректировки v5** (`apply_corrections`) — строго ПОСЛЕ Grid (Grid перезаписывает
   `bidModifiers`; сначала Grid, потом `bidmodifiers.add`).

### Типы объявлений ЕПК (v501, проверено live)

| Тип | Метод | Когда |
|-----|-------|-------|
| `TextAd` | `add_text_ad(adgroup_id, title, text, href, sitelink_set_id, callout_ids)` | поисковые группы tp2/tp4/tp5 |
| `ShoppingAd` | `add_shopping_ad(adgroup_id, feed_id, collection_id)` | товарная галерея (tp3/tp5/tp7-логика) |
| `ListingAd` | `add_listing_ad(adgroup_id, feed_id, collection_id)` | «страницы каталога» фида (tp5 товарная группа) |

`collection_id` — `collectionId` из listings фида (формат `model_N`);
`FeedFilterConditions=[{Operand:"collectionId", Operator:EQUALS_ANY, Arguments:[id]}]` —
фильтрует объявление по конкретной модели из фида.

### Цена в объявлениях (adPrice) — правила (2026-07-02)

- Источник: `_account_offer_prices(login, href)` — минимум по ключу-бренду/модели со ВСЕХ фидов
  аккаунта (НЕ один defaultFeedId — у него бывало 0 офферов → цены «пропадали»).
- Сегмент «Марки» → мин. цена марки; «Модели» → цена модели (фолбэк на марку); «Общее» → мин. товар фида.
- **Марки/модели НЕТ в фиде → цена ПУСТАЯ** (правило Семёна: «Tank от 789 900 ₽» вводит в заблуждение).
- ⚠️ Grid `UpdateAdaptiveTextAds` — full-replace: ЛЮБОЙ повторный апдейт объявления БЕЗ `adPrice`
  ЗАТИРАЕТ цену (и без `creativeIds` — затирает видео). Поэтому: `ads_repaired_after_price` удалён
  из tp1 И tp2/tp4; видео-attach (Фаза 3.6) несёт `meta.ad_price_payload`. Новые мутации объявлений —
  ОБЯЗАТЕЛЬНО прокидывать оба поля.


## Куки-движок, устойчивость и когерентность (2026-06-23/24)

### Куки-движок создания (`grid_create.py`) — без баллов
При исчерпании суточных баллов Директа (error **152**) и **явном согласии пользователя через
поп-ап** (`via_cookie=True`, ставится ТОЛЬКО в `_deferred_enqueue_now` от кнопки «создать по куки»)
token-типы создаются по куке агентства через Grid web-api:
- **tp1 РСЯ** (`_create_tp1_via_cookie`), **tp2/tp4 Поиск** (`_create_text_via_cookie`) — `create_full`:
  `AddCampaigns` + `AddUnifiedAdGroups` (минуса/автотаргет — **без ключей**) + `AddKeywords` (ключи
  ЕДИНСТВЕННЫМ путём: `build_adgroup(keywords=[])`, иначе Grid дублирует фразы для групп <~140 кл) +
  `AddAdaptiveTextAds` (комбинаторное).
  После первичного `create_full` для `tp1/tp2/tp4` обязателен **post-create repair** по фактическим
  `ad_id` через `UpdateAdaptiveTextAds`: он переписывает полный payload (`titles`, `bodies`,
  `image_hashes`) и выравнивает live-черновик с тем контентом, который был рассчитан AI/slepok
  до создания. На первичный `AddAdaptiveTextAds` как на финальное состояние полагаться нельзя.
- **tp3/tp5 Товарная галерея** (`_create_shopping_via_cookie`) — `create_shopping_full`: кампания
  (`gallery`+`organic` для tp5; `network` для tp3) + группа (автотаргет) + **`AddShoppingAds`** по фиду
  (реверс из HAR17). Фид читается по куке (`_grid_feeds` агентства).
- **tp6/tp7** — UAC (куки), как и раньше.
- Гейт строгий: куки-путь НИКОГДА не включается автоматически, только из поп-апа (согласие «небезопасно»).
- При 152 куки-прогон **НЕ прерывается** на v5-only ошибках (`if _units_block and not via_cookie`).

### Post-create проверка
- Перед циклом создания `api_create_set` формирует `precreate` report из `precreate.py`: какие
  ресурсы уже подготовлены или должны быть подготовлены до загрузки кампаний. `read_existing_campaign_names`
  уже выполняется Grid-read; `ensure_callouts`, `ensure_minus_libraries`, `prefetch_images` и
  `prefetch_ai_content` отражаются как planned/active шаги с `uses_direct_units=false`.
- `ensure_callouts` выполняется до upload-цикла через Grid `add_callouts` без Direct units для выбранных
  уточнений; в `precreate.callouts` возвращаются подготовленные ids. Дальше finalize/repair пути
  переиспользуют эти уточнения из библиотеки аккаунта.
- `ensure_promo_library` выполняется guarded до upload-цикла, если весь контент items готов и
  `stream_content=false`: сначала ищется пригодное промо аккаунта, иначе создаётся промо по выбранному
  слепку. После создания кампаний этот готовый `promo_id` только привязывается. Если контент ещё
  генерируется потоком, precreate promo пропускается и остаётся прежний post-create путь, чтобы не
  рассинхронизировать оффер. В отличие от callouts, этот шаг использует v5 read `promotions.get`
  для проверки библиотеки аккаунта; в report это помечается `uses_direct_units=true`.
- `api_create_set` возвращает `verification` из `verifier.py` сразу после создания: это дешёвая
  проверка результата без обхода кабинета.
- Static verification получает body уже после fallback счётчика/цели из `metrika_goals` и проверяет:
  выбран ли слепок/agent, есть ли счётчик и цель, есть ли items, не был ли запрошен `launch=true`,
  нет ли `None/null/undefined` в названиях, есть ли локальный контент/фид-признаки и не вернул ли
  локальный build `groups=0`/`ads=0`. Последнее сразу попадает в `repair_plan` как
  `rebuild_missing_content`, без затрат Direct units.
- `/direct/api/create_set_verification?job_id=...` возвращает сохранённую проверку завершённой джобы.
- `/direct/api/create_set_verification?job_id=...&live=1` делает read-only live-сверку. По умолчанию
  используется Grid/cookie (`_grid_list_campaigns`) как основной источник факта, потому что баллов
  Direct API мало, а tp6/tp7 всё равно видны только в Grid/UAC-слое.
- Live-сверка дополнительно читает через `grid_read.py` фактические счётчики групп/объявлений для
  tp1–tp5: `NO_ADGROUPS_LIVE` / `NO_ADS_LIVE` попадают в `repair_plan` как `rebuild_missing_content`.
- Для UAC tp6/tp7 live-сверка читает `uac_read.py`: проверяет фактические счётчики `titles/texts`,
  `sitelinks`, media и наличие feed/ecom для tp7. Недобор даёт `UAC_*_MISSING` и план
  `resume_or_recreate_campaign`, потому что безопасного частичного UAC patch-контракта пока нет.
- `v5=1` включает дополнительную v5-проверку обычных tp1–tp5 (`campaigns.get`) и может тратить
  Direct API units. Без необходимости не включать.
- Оба отчёта (`verification` и live) содержат `repair_plan`: список действий для следующей добивки.
  Planner чистый и ничего не меняет в Директе; мутации выполняет отдельный executor-слой.
- После `create_set` live-сверка запускается автоматически и возвращается в поле `live_verification`.
  Рядом пишется короткий `repair_gate`: сколько действий найдено, сколько можно выполнить сейчас,
  сколько уйдёт в recreate queue/UAC replace, и какой endpoint запускать для добивки.
  Она Grid-first и не включает `v5=1`, поэтому не расходует Direct API units.
- После этой сверки `create_set` автоматически делает безопасный in-place проход только по
  идемпотентным действиям `create_or_attach_promo`, `ensure_callouts`, `rename_campaign`.
  `rebuild_missing_content` не запускается немедленно, чтобы не плодить группы из-за Grid lag.
- Если после terminal `done` в плане остался `rebuild_missing_content`, worker ставит guarded delayed
  repair в `direct_delayed_repairs`: через задержку делается повторная Grid-first live-сверка и
  content repair выполняется только если пустой контент всё ещё подтверждается. Используются cookie/Grid,
  `uses_direct_units=false`.
- После terminal `done` worker отдельно смотрит `repair_plan`: если остались
  `resume_or_recreate_campaign`, ставит scoped repair-job в обычную очередь (`via_cookie=true`,
  `launch=false`). Для `UAC_*_MISSING` перед постановкой удаляется только конкретный неполный UAC
  draft по `campaign_id`, а repair body получает `_repair_force_names`.
- `/direct/api/create_set_repair` (`POST {"job_id":"..."}`) пересчитывает Grid-first live-сверку и
  возвращает `repair_plan` как отдельный repair-gate. По умолчанию он read-only. `execute=1`
  умеет пять scoped-сценариев без Direct units: `resume_or_recreate_campaign` ставит отдельную
  repair-job в обычную очередь `create_set` (`via_cookie=true`, `launch=false`),
  `rebuild_missing_content` добавляет группы и adaptive text ads в уже существующие tp2/tp4 через Grid,
  а для tp3/tp5 добавляет товарную группу + ShoppingAd/ListingAd с vendor/model/name фильтрами,
  `create_or_attach_promo` создаёт согласованное промо через Grid по выбранному слепку и привязывает
  его к уже созданным кампаниям, а `ensure_callouts` создаёт/находит выбранные уточнения через Grid
  и узкой мутацией обновляет только `inheritableCallouts`; `rename_campaign` узко обновляет только
  `name` по id из `NAME_MISMATCH`. Остальные action-типы возвращаются как `unsupported_actions`.
  После in-place executor-ов ответ дополнительно содержит `post_repair_live_verification` и
  `remaining_repair_plan`: повторную Grid-first сверку без v5 units.
- Для `UAC_*_MISSING` repair-gate делает replace-flow: удаляет только конкретный неполный tp6/tp7
  UAC draft по `campaign_id`, затем ставит исходный item в queue с `_repair_force_names`, чтобы
  `RESUME-SKIP` не пропустил пересоздание из-за кэша Grid.

### Удаление черновиков по куке при 152
`_delete_drafts_core`: v5 `campaigns.delete` → при 152 **молча** через Grid `deleteCampaigns` (по куке,
без баллов), финал репортится с разбивкой `by_v5/by_uac/by_cookie`.

### Устойчивость: пустышки удаляются сами
- **Поштучно в прогоне:** если кампания создана, но группы/объявления не достроились —
  `_cleanup_partial` (tp1/tp5) / `_delete_partial_campaign` (tp2/tp4/tp3) удаляет недоделанную.
- **При старте (после рестарта/сбоя):** `_sweep_empty_drafts` для аккаунтов прерванных джоб удаляет
  **ЕПК-черновики с 0 групп** (фон, по куке). Безопасно: только `GdUnifiedCampaign` (UAC не трогаем —
  у них 0 grid-групп штатно) + имя с `tp` + только при старте (нет гонок с активным созданием).
- **Учёт остатка при 152:** `_units_from` — пункт, на котором кончились баллы, попадает в остаток на
  докрутку (раньше терялся: 7 пунктов → в остаток уходило 6).

### Когерентность скидок (#5/#6)
`_coherent_discounts(titles, texts)` — одно ₽-число и один %-число на кампанию (эталон = **самое частое**
значение в контенте, без выдумывания). Лечит рассинхрон заголовок/текст (в заголовке 57% → и в тексте 57%)
и почти-дубли (890/860 «выгода» → единое число → схлоп дедупом). Применяется в `_responsive_ad` (tp1/tp2)
и в блоке tp6/tp7.

### Контент: правила
- **Нейминг tp7** Товарка = `ct010` (Каталог+ТГО+Фид), не `ct009` (`_build_name`).
- **ct0000 «Общие запросы» (tp7):** автотаргет-заголовки (ключ запроса первым, до запятой —
  `_GENERIC_AT_TITLES`), без брендов; картинки с M3 из папки кодера `ct0000` по типу кампании
  (`read_images(site_type, tp, "ct0000")`, не бренд-фид).
- **Общие/аудиторные ct для картинок:** `ct0000, ct0001, ct0002, ct0003, ct0004, ct0005, ct0006,
  ct0007, ct0008, ct0009, ct0010, ct0013, ct0014` всегда используют общий пул `ct0000`, чтобы
  общие группы не получали картинки конкретных моделей.
- **Кузова для картинок:** `ct0015` седаны, `ct0016` хэтчбеки, `ct0017` кроссоверы, `ct0018`
  минивэны используют картинки строго из своей `ct`-папки.
- **Модельные tp6/tp7:** картинки по ct модели из папки слепка СВОЕГО типа → если пусто, любая папка
  этого слепка по ct (`_slepok_images_any_tp`).
- **Общие заголовки:** голые хвосты `Со скидкой`, `Скидки месяца`, `Акция` запрещены; также запрещено
  `госпрограмма/госпрограммы ... в подарок`. Общие группы добиваются до 7 заголовков после дедупа.
- **Fallback контента:** после обязательной цифры в заголовке fallback-пулы для `С пробегом`,
  новых, квизовых и мультибрендовых сайтов должны оставаться валидными сами по себе. При пустом
  ответе M3 проверочная матрица обязана давать: `tp6` = 5/3/8, `tp2` = 7/3/8
  (заголовки/тексты/быстрые ссылки), без плохих строк по текущим фильтрам.
- **Быстрые ссылки:** generic `Запишитесь на тест-драйв` запрещён; фолбэк — предметный
  `Тест-драйв 2025`; финальный fallback добивает набор до 8 ссылок.
- **AI-first + repair-loop:** для новых кампаний контент собирается в таком порядке:
  1) полный AI/M3-контент,
  2) если часть вариантов отброшена фильтрами или M3 вернул пусто/ошибку — обязательный repair-loop
  до полного набора,
  3) если stream-generation не дала полный комплект — builder fallback/локальные шаблоны; если и они
  не собрали валидный набор, item падает с content-gap.
  Цель инварианта: комбинированные объявления не должны выходить с частично пустыми заголовками,
  текстами или быстрыми ссылками и не должны массово падать до builder'ов при временном LLM-сбое.
- **Склонение города:** `-ль → -е` (Ставрополь→Ставрополе, Ярославль→Ярославле), прочие `-ь → -и` (Пермь→Перми).
- **tp1 быстрые ссылки:** нет у слепка → ИИ M3-генерация (`_ai_sitelinks`) → общие фоллбэки.
- **Товарная галерея без бренд-пака** (напр. pavlov без tp5-пака): фид-фолбэк — общая товарная группа
  по всему фиду (`collection_id=None`) с автотаргетом, чтобы tp5/tp3 не выходили пустыми.

### Потоковая генерация → создание (publish без предпросмотра)
`stream_content=True` («🚀 Создать и опубликовать»): ИИ-контент M3 генерится **поитемно** прямо перед
созданием каждой РК (`_gen_campaign_content` в цикле `create_set`), а не всей пачкой заранее. Прогресс
виден сразу, при 152/сбое уже созданные сохранены. Для боевого пути это означает: сначала AI/M3,
дальше repair/retry до полного набора, и только затем fallback из слепка/общих шаблонов. Ручной путь
с предпросмотром — батчевый (как было).


> Архив: описание миграции TextAd→ResponsiveAd (06-24), 3 фикса комбинаторного РСЯ,
> Имитация-прогон Павлова (porg-ozge4ntu), правило черновиков → [docs/archive/README_ARCHIVE.md](docs/archive/README_ARCHIVE.md)


### TODO
- **#2 минус-площадки** (РСЯ `ExcludedSites`): механизм тривиален, но НЕТ источника списка площадок
  (ни слепок, ни правила) — нужен список от пользователя.

## Локальная LLM (M3)

`_M3_LLM_URL` (env `M3_LLM_URL`, дефолт `http://127.0.0.1:8082`) — mlx_lm.server на
Mac M3 Ultra (512 ГБ). Доступ из LXC101 через обратный SSH-туннель (M3 → Victory bastion →
LXC101). Модель: **Qwen2.5-72B-Instruct-4bit** (локально в `~/llm/models/`, keep-alive
`~/llm/ka_mlx.sh`). При генерации «model» НЕ шлём (mlx берёт загруженную).

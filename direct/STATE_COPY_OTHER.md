# STATE — вкладка «Прочие сферы» сервиса копирования (/direct/automation/copy)

> 📦 **Свежая версия сервиса всегда лежит в GitHub:**
> **https://github.com/DirectAdvance/ydirect_automation_copy_auto_ak_ak** (ветка `main`).
> Правим локально в `home/seoadvanced`, репо — зеркало-экспорт. **Любая локальная правка сразу
> отправляется в git:** `python3 tools/copy_service_git.py export` (commit + push зоны copy).

> **Agent Board failed-copy исключение:** задачи из упавших `copy_campaigns` не пушат в copy-git.
> Runner выдаёт isolated checkout `/var/lib/agent-board/copy-runner/tasks/<task_id>/home/seoadvanced`,
> проверенная правка переносится в Mutagen checkout `/opt/scripts/home/seoadvanced/direct` под
> `AGENT_BOARD_COPY_PUBLISH_LOCK`. После `done` Agent Board retry-daemon `direct-copy.service`
> сам создаёт повторную `copy_campaigns` job и связывает её через `copy_retry_job_id`; руками дубль
> не создавать без отдельной причины.

Сессии 2026-07-16 → 2026-07-17.

## Сессия 2026-07-31 — copy adPrice: минимум фида для `Общее`/прочих СТ

**Сделано:** `copy_price_steps.step_prices` теперь определяет price-сегмент по имени группы и
кампании. Для `Марки`/`Модели` остаётся строгий матч по target-фиду: если марки/модели нет, цена
пустая и донорская цена не переносится. Для `Общее`/`Общие запросы`/прочих СТ передаётся сегмент
`Общее`, поэтому `_group_ad_price` ставит минимальную цену машины из target-фида.

**Проверено:** `py_compile` целевых copy/create/grid модулей OK; focused pytest по price-segment,
минимуму фида и adprice-repair — `4 passed`; `git diff --check` OK. Код закоммичен в
`neurodirectologist` ветку `ydirect_automation_copy_auto_ak_ak` (`ad03b743`) и экспортирован в
`DirectAdvance/ydirect_automation_copy_auto_ak_ak` main. Live-copy на проде на момент записи не
перепроверялся.

**Деплой/сервис:** перед рестартом `direct-copy.service` проверена очередь Victory:
`copy_campaigns` в `queued`/`claimed`/`running` = 0. LXC101 файл
`/opt/scripts/home/seoadvanced/direct/copy_service/copy_price_steps.py` содержит
`_price_segment_from_names`/`by_min_fallback`, `py_compile` на LXC OK. `direct-copy.service`
перезапущен 2026-07-31 13:43:50 +05, статус `active/running`, PID `1473270`; локальный smoke
`GET /direct/api/copy_queue` вернул ожидаемый `401 Unauthorized`.

## ✅ РЕЗУЛЬТАТ: копия 1:1 достигнута ФАКТОМ (проверено v5 API + Grid)

Источник `porg-mushirne` → `porg-jh2si7rh`, `porg-as46rje6`, `porg-r7ro6tei`.
Настройки: счётчик 110544511, цель 580623722, гео keep. Всё черновиками (State=OFF).

| Параметр | Источник | Все 3 цели |
|---|---|---|
| Кампании (tp1/tp2) | 5 | 5 |
| Мастер-кампании (tp6) | 3 | 3 |
| Группы | 84 | 84 |
| Объявления | 244 | 244 |
| Ключевые фразы (вкл. `---autotargeting`) | 1759 | 1759 |
| Минус-слова на кампаниях | 267 | 267 |
| Минус-слова на группах | 275 | 275 |
| Стратегия «тёплый спрос» | PAY_FOR_CONVERSION_MULTIPLE_GOALS, 2 цели | то же |
| Домен ссылок | need-number.ru | свой у каждого (244/244) |
| Картинки на объявлениях | 4 (1 своя) | 4, каждая СВОЯ из архива |

## 🔴 Найденные баги (все ДАВНИЕ, маскировались друг другом)

1. **Ключи не заливались вообще, а отчёт рисовал успех.**
   `copy_steps.py:817` считал `len(added or []) or len(rows_b)` → при пустом `addedItems` подставлял
   размер ОТПРАВЛЕННОГО батча: отчёт `via_grid=1396` при 0 реально залитых фраз.
   Хуже: строка выше помечала ВСЕ фразы батча как `done` → повторный прогон пропустил бы их как
   `already_done` и недолив стал бы вечным. **ИСПРАВЛЕНО**: считаем только фактически принятое;
   неполный батч не помечаем done и отдаём в v5-фолбэк.
   ⚠️ Grid `add_keywords` при этом ИСПРАВЕН — батч 1000 принимается (проверено, addedItems=1000).
   Почему в прогоне 03:22 вернулся пустой addedItems — НЕ установлено (ретроспективно не воспроизвести).

2. **`---autotargeting` — НЕ ключевая фраза, а плейсхолдер автотаргетинга.**
   Директ создаёт по одному на группу. 79 групп → 79 «ключей» в `keywords.get`.
   На этом обжёгся субагент: принял 79 автотаргетингов за успешную заливку ключей.
   ⛔ При любой сверке фраз — отсекать `Keyword.startswith('---')`.

3. **Минус-слова кампаний не копировались вообще (267 → 0), молча.**
   `direct_copy.py`: `NegativeKeywords` — КАМПАНИЙНОЕ поле (enum FieldNames), НЕ поле TextCampaign.
   Его не было в `CAMPAIGNS_FIELDS` (pull не читал), а при записи искали в `type_data` (блок
   TextCampaign), где его не бывает → условие всегда ложно, ошибки нет. Групповые минусы при этом
   копировались и МАСКИРОВАЛИ пропажу. **ИСПРАВЛЕНО** (чтение + запись на уровень кампании).

4. **Стратегия PAY_FOR_CONVERSION_MULTIPLE_GOALS: барьер — НЕ стратегия, а `PriorityGoals`.**
   Ключевые цели живут в `TextCampaign.PriorityGoals`, НЕ внутри блока стратегии
   (полей `Goals`/`BudgetType` внутри `PayForConversionMultipleGoals` НЕТ — 8000).
   Рабочий рецепт (проверен на 3 аккаунтах, куки НЕ нужны):
   ```
   campaigns.update: TextCampaign.PriorityGoals = {"Items":[{GoalId, Value,
       IsMetrikaSourceOfValue, "Operation":"SET"}]}   # только SET, ADD → 3500
   + BiddingStrategy.Search = PAY_FOR_CONVERSION_MULTIPLE_GOALS
       {"WeeklySpendLimit": ...}                      # BudgetType НЕ слать
   ```
   ⛔ Комментарий в `direct_copy.py:21` «CounterIds и PriorityGoals не переносятся» — НЕВЕРЕН,
   если счётчик у цели тот же (здесь 110544511 общий).
   ⛔ Субагент добавил `PAY_FOR_CONVERSION_MULTIPLE_GOALS` в `conv_types` (`strategy_fallback`,
   ~строка 428) → это ДАУНГРЕЙД в клики, не решение. Стратегия в кабинетах восстановлена вручную.

5. **Мастер-кампании (tp6) копились дублями: 15–18 вместо 3.**
   `cleanup=delete_drafts` их НЕ трогает (v5 их не видит вовсе — `campaigns.get` с
   Types=[UNIFIED_CAMPAIGN, SMART_CAMPAIGN] возвращает пусто даже в источнике).
   Читать/удалять только по кукам: `yandex_gateway.grid_list_campaigns(login, only_draft=False)`,
   `grid_create.GridCreateClient.delete_campaigns(ids)`. Приведено к 3 (удалено 12/12/15).

6. **Новые картинки по сайту не вставали на объявления.**
   `image_hashes` в body джоба ПЕРЕДАВАЛИСЬ (51 шт), но раскладка (`_copy_image_remapper`,
   round-robin) работает только в Grid/ЕПК-ветке. Обычные TEXT-объявления создаёт v5-ветка
   (`direct_copy.phase_upload`), она берёт `maps["images"][src_hash]`, который строится
   ПЕРЕЗАЛИВКОЙ картинки источника (одинаковый контент → тот же хеш) → на объявлениях стояла
   картинка ЧУЖОГО сайта need-number.ru, а 50 загруженных лежали в библиотеке мёртвыми.
   В кабинетах исправлено вручную. **В КОДЕ НЕ ИСПРАВЛЕНО** — см. ОСТАЛОСЬ.

7. Прежние (из прошлой сессии, исправлены): nginx `client_max_body_size` 1МБ → 256МБ;
   роут `geo_regions` → `copy_geo_regions` (⛔ любой роут copy-сервиса ОБЯЗАН начинаться с
   `/api/copy_`; локальный curl на :5022 этот баг МАСКИРУЕТ); `DIRECT_ROLE=web` → `all`;
   `today_str()` UTC → `tomorrow_str()` МСК; `BudgetType` → `strategy_sanitize()`;
   `AgeLabel` ронял весь батч `ads.add`.

## 📌 ОСТАЛОСЬ (в КОДЕ; данные в кабинетах уже верны)

1. **Картинки (баг 6)**: научить v5-ветку использовать `image_hashes` из body —
   при `mode="other"` мапить `maps["images"]` на загруженные хэши round-robin вместо перезаливки
   картинки источника. Правка в `direct_copy.phase_upload` (секция «3. Картинки», ~строка 966)
   + прокинуть `image_hashes` из `copy_engine` (рядом с `skip_keywords=True`, ~строка 2706).
2. **Стратегия (баг 4)**: заменить даунгрейд на перенос `PriorityGoals` + восстановление
   настоящей стратегии по рецепту выше. Откатить добавление MULTIPLE_GOALS в `conv_types`.
3. **Домен в «тёплом спросе»**: субагент создал 15 объявлений со ссылками на ЧУЖОЙ домен
   (need-number.ru) — замена домена не применилась к его пути создания. В кабинетах исправлено,
   причину в коде НЕ искали.
4. Мастер-кампании: `cleanup` не удаляет tp6 → при каждом прогоне +3 дубля. Чинить в
   `_copy_target_cleanup` (удалять по кукам через `grid_create.delete_campaigns`).
5. Правки НЕ закоммичены (nested-репо `home/` и `work/slepki_direktologov/`).

## 🧰 Инструменты сессии

- Токен: `.secret/loader.py` → `load_yandex_direct()["tokens"]["y-direct-victory"]["oauth_token"]`
  (⚠️ значение — dict, не строка).
- Рабочие папки джобов на LXC101: `/tmp/direct-copy-<job8>-*` (внутри `source/keywords.json`,
  `id_maps.json`) — годятся для доливки без пересоздания.
- Отчёты джобов: Victory `public.direct_automation_jobs` (колонка `result`, НЕ `report`;
  `body` содержит `image_hashes`). `psql` на Victory сломан → `ssh victory '~/venv/bin/python3 ~/pgq.py "SQL"'`.
- Скрипты сверки/починки (одноразовые, в scratchpad): kwcount/cmp/negfix/imgfix/domfix/stratfix.

## 🔧 Сессия 2026-07-17 (продолжение): микросервис, очередь, картинки, сверка

### Сделано и проверено фактом

1. **Очередь копирования отделена СТРУКТУРНО** (была — только на env `DIRECT_ROLE=all`):
   - `queue_server.py` `_worker_claim_web_jobs`: в SQL-клейм добавлен `AND coalesce(kind,'') <> 'copy_campaigns'`.
     Раньше фильтра по kind НЕ БЫЛО → воркер СОЗДАНИЯ забирал copy-джобы (факт: 7 джоб с
     `_web_posted=true` исполнены им до 03:21 UTC) → вечный `queued`.
   - тот же гард в `_jobs_db_recover` (`claimed`→`queued`).
   - `blueprint.py`: `DIRECT_REGISTER_COPY` дефолт `"1"` → `"0"` (explicit opt-in). Раньше изоляция
     держалась на drop-in `copy.conf`; без него direct-create поднял бы copy-роуты, причём
     деградированно (5 из 22 зависимостей = None).
   - ДОКАЗАНО: фиктивная copy-джоба (`_web_posted=true`) осталась `queued` 45 с при живом воркере.
   - ⛔ ВАЖНО: ветку `copy_campaigns` в `_create_worker_loop` УДАЛЯТЬ НЕЛЬЗЯ — `_ensure_copy_worker`
     запускает ТОТ ЖЕ цикл, именно она исполняет копирование в copy-процессе (аудит советовал удалить
     как «мёртвую» — это ошибка, удаление сломает копирование).

2. **Роуты «Авто» и «Прочие сферы» разведены**: `POST /api/copy_start` (авто, теперь ОТВЕРГАЕТ
   `mode='other'`) и `POST /api/copy_other_start` (прочие). Режим задаёт РОУТ, не payload.
   Тело вынесено в `_copy_start_impl(body, mode)`.

3. **Режим картинок** (`templates/direct/copy.html` + `routes_copy.py`): тумблер `other-img-mode`
   под «Проверить фиды»: `copy` (ДЕФОЛТ, 1в1 из источника) / `upload` (загружаем новые).
   Сервер: в режиме `copy` `image_hashes` жёстко обнуляется; `upload` без хэшей → 400.

4. **Сверка настроек по кукам** — `copy_steps.step_settings_diff` (report-only, 0 v5-баллов),
   вызывается ПОСЛЕДНЕЙ в `_copy_cookie_postprocess` (после всех добивок). Читает обе стороны через
   новый `grid_finalize.GridClient.campaigns_edit_rows()` (CampaignsEditData), сравнивает с явным
   списком исключений `_DIFF_SKIP_KEYS` (id/имена/даты, домен, гео, счётчики/цели, картинки,
   `strategyId`, наш стандарт: disabledPlaces/bidModifiers/disabledIps, статистика/статусы).

   **ЖИВОЙ ПРОГОН нашёл РЕАЛЬНЫЕ потери — 5 из 5 кампаний** (porg-mushirne → porg-jh2si7rh):
   | настройка | источник | копия |
   |---|---|---|
   | hasAddMetrikaTagToUrl | false | **true** |
   | hasExtendedGeoTargeting | false | **true** |
   | isAlternativeTextsEnabled | false | **true** |
   | isOrganicSearchEnabled | false | **true** |
   | placementTypes | [SEARCH_PAGE] | **+ADV_GALLERY** |
   | promoExtension | есть | **null** (промо не скопированы) |
   Копировщик эти настройки НЕ переносит — они остаются на дефолтах Директа. Часть из них наш
   live-верификатор и так помечает (ALT_TEXTS_ENABLED_LIVE / EXTENDED_GEO_ENABLED_LIVE).

## 🔧 Сессия 2026-07-17 (вечер): ручная починка 3 кабинетов + 4 НОВЫХ дефекта копировщика

Задача Семёна: проверить источник `porg-mushirne` vs 3 копии — цели, изображения из архива,
названия 1в1, весь контент. Все правки сделаны РУКАМИ по API/кукам; **в коде НЕ исправлено**.

### Найдено и починено (всё верифицировано перечитыванием)

1. **Цели (`PriorityGoals`) не переносились в 4 из 5 кампаний** на всех 3 логинах (null).
   Верной была только «тёплый спрос» — её чинили руками в прошлую сессию.
   Починено переносом 1:1 из источника → 15/15 кампаний совпадают.
   ⚠️ `Value` — МИКРО-единицы: `5000000000` = 5000 ₽, `300000` = 0,3 ₽ (не 5 ₽!).
   ⚠️ Счётчик у всех 4 кабинетов ОБЩИЙ (110544511) → номера целей те же, маппинг не нужен.
   ⚠️ В источнике «горячий спрос» имеет служебную `GoalId 12` (Вовлечённые сессии) — при 1:1 её и переносим.

2. **Картинки МК (tp6) — у всех 3 копий стояли ОДНИ И ТЕ ЖЕ 5 картинок бренд-темы источника**
   (`need-number.ru`). Хеши совпадали между кабинетами → доказательство, что это перезалив
   картинки источника (баг 6), а не раскладка своих. Визуально подтверждено: тема источника
   светло-фиолетовая, а должны быть свои (need-leads=оранжевая, need-lead=голубая, needleads=зелёная).
   Починено: залиты картинки из своей папки архива round-robin → у каждого кабинета
   **15 уникальных** (3 МК × 5), ноль пересечений с другими кабинетами и источником.
   - Путь записи: `upload_image_file` (куки, без баллов) → `routes_content_editor._uac_patch_campaign_texts(client, cid, "content_ids", ids)`.
   - ⛔ Частичный `PATCH /campaign/{id} {"content_ids": [...]}` → **HTTP 500**. Нужен full-payload фолбэк.

3. **☠️ ЛОВУШКА: full-payload PATCH ЗАТИРАЕТ картинки.** `_UAC_PATCH_FULL_KEYS` НЕ содержит
   `content_ids` → любой патч другого поля (у нас `socdem`) обнуляет картинки МК в ноль.
   Поймано только перечитыванием после правки. **Порядок правок: сначала прочие поля, картинки — ПОСЛЕДНИМИ**
   (`socdem` в FULL_KEYS есть, поэтому патч картинок его сохраняет — обратный порядок безопасен).

4. **`socdem` (возраст) не переносился в МК**: источник `age_35`, копии `age_18` — в 2 из 3 МК
   каждого кабинета (те, что `ag011`). Починено PATCH-ем 1:1 → 9/9 МК совпадают по 26 полям.

5. **`DisplayUrlPath` (отображаемая ссылка) не переносился — 229 из 244 объявлений** (`None`
   вместо `лиды-для-бизнеса`). Плюс **`Text` терял хвостовой «!»** — 75 объявлений
   (`...прямо сейчас!` → `...прямо сейчас`). Починено `ads.update` по ключу
   (кампания, группа, Title, Title2) → 687 объявлений, 0 ошибок; TEXT-контент стал 1:1.

6. **Названия — ЛОЖНАЯ ТРЕВОГА, копировать нечего.** Имена МК совпадают с источником символ
   в символ. Источник САМ назван кодером нейродиректолога (`tp6_cpa_site_ct0000_aon_...`) —
   копия просто повторила его имя.

### Состояние на конец сессии (проверено фактом)

| Что | Источник | Все 3 копии |
|---|---|---|
| Кампании / группы / объявления / ключи | 5 / 84 / 244 / 1675 | то же |
| TEXT-контент (тексты, ключи, минусы, DisplayUrlPath) | — | ✅ 1:1 |
| МК: контент по 26 полям (вкл. socdem) | — | ✅ 1:1 |
| МК: картинки | 5 (общие на 3 МК) | 15 уникальных, свой домен |
| Цели PriorityGoals | — | ✅ 1:1, 15/15 кампаний |

Скрипты сессии (scratchpad, одноразовые): `goalfix.py`, `mkimg.py`, `socdemfix.py`, `adfix.py`,
`mkdiff.py` (сверка МК), `textdiff.py` (сверка TEXT), `uacimg2.py` (картинки МК).

### ✅ ЗАШИТО В КОД И ЗАДЕПЛОЕНО (2026-07-17 вечер, коммиты 32be328 + 72fa9f8; work-репо 7989f0d)

Проверено `direct_verifier` (оба захода), задеплоено на LXC101, `direct-copy` + `direct-content`
перезапущены, smoke зелёный. ⚠️ ЖИВОГО ПРОГОНА КОПИРОВАНИЯ НЕ БЫЛО — патчи проверены статически
+ точечными живыми зондами, но не полным copy run.

| Дефект | Корень | Где чинили |
|---|---|---|
| `PriorityGoals` не переносились | читались, но их НИКТО не писал (ни `campaigns.add` — белый список `type_body`, ни `_copy_apply_metrika`) | `copy_engine._copy_apply_metrika`: 2-й update, `Operation="SET"`, гард `counter_id in CounterIds.Items` |
| Картинки МК = картинки ИСТОЧНИКА | `image_urls` из detail источника → `upload_content(source_url)` → Директ качает тот же файл → **одинаковый хеш в 3 кабинетах** | `copy_engine._copy_uac_campaigns`: при `image_mode=upload` резолв своих хешей в URL цели + round-robin `(cidx*5+k)` |
| `socdem`, `device_types`, `minus_regions`, `relevance_match_categories`, `id_time_zone` | ОДИН корень: не передавались в `MasterCampaignSpec` → дефолты датакласса | там же, чтение из `d` через `_copy_uac_value` + тип-гард + фолбэк |
| `DisplayUrlPath` терялся (229/244) | `UpdateAdaptiveTextAds` = full-replace, а RMW-чтение не читало `linkTail` | `grid_finalize:2217/2256` (читать `linkTail`→`displayHref`), `create_set_feeds:758-772` + `_apply_combo_button:818` (2-й full-replace тоже стирал) |
| Хвостовой «!» (75 объявл.) | `_trim_clean` звался БЕЗУСЛОВНО → `text_norm:108` `rstrip(" .,;:!?-")` даже у строк короче лимита | `copy_steps:1055/1066` — обрезать только при превышении. `text_norm.py` НЕ трогать (общий с create-set) |
| 1 картинка вместо 5 в комбинированных | v5 умеет ровно одно поле `AdImageHash`; доливки не было | `copy_steps.step_adaptive_creatives` — доливка до 5 из `body["image_hashes"]` |
| ☠️ full PATCH обнулял картинки МК | `_UAC_PATCH_FULL_KEYS` — белый список; ключа картинок нет → replace стирал. **`content_ids` в whitelist = no-op**: UAC отдаёт `contents` (list[dict]), а ждёт `content_ids` (list[str]) | `routes_content_editor._uac_campaign_patch_payload:606-611` — деривация `content_ids` из `contents`. ЖИВАЯ проверка: PATCH socdem → 5→5, хеши те же |
| tp6 копились дублями (+3/прогон) | `cleanup` не видел МК (v5 их не отдаёт) | `copy_engine._copy_cleanup_uac_drafts` — по кукам, строго `status=DRAFT` целевого логина |
| `hasAddMetrikaTagToUrl` / `hasExtendedGeoTargeting` / `isAlternativeTextsEnabled` | на создании закрыты `_COPY_SETTINGS_WHITELIST`, но у УЖЕ созданных копий чинить было нечем | `copy_steps.step_settings_diff` теперь «отчёт + автопочинка» через **v5** (Grid пропускает их на стратегии DEFAULT) |

### ⚖️ РЕШЕНИЕ ЗА СЕМЁНОМ: «копия 1:1» vs «наш стандарт»

`alternative_texts_enabled` / `ml_banners_enabled` / `yandex_maps_enabled` оставлены захардкоженными
`False` в `_copy_uac_campaigns` — **осознанно**: `uac_verifier.py:107-112` считает `True` дефектом
(`UAC_MAPS_ENABLED`, `UAC_ALTERNATIVE_TEXTS_ENABLED`), `create_set_master_product.py:692` ставит тот же
стандарт на пути создания. Сейчас конфликт латентный: источник создан нашим же кодером, у него эти
флаги и так `False` → 1:1 и стандарт совпадают. Если придёт источник с `True` — копия получит
стандартный `False` (МК) либо `True` от источника (TEXT-настройки, `step_settings_diff` ставит 1:1).
Поведение НЕсогласовано между ветками — нужно решение.

### ОСТАЛОСЬ

0. **ЖИВОЙ ПРОГОН КОПИРОВАНИЯ в чистый кабинет** — главное. Всё выше проверено статически.
   Что смотреть: цели 5/5; МК — свои картинки (не пересекаются с источником), `device_types`/
   `minus_regions`/`id_time_zone` 1:1; adaptive — `linkTail`='лиды-для-бизнеса' и 5 картинок;
   доля «!» как в источнике; `cleanup` не плодит дубли (3 МК, не 6); промо привязались.
1. `placementTypes` для стратегий DEFAULT/OPTIMIZE_CLICKS/MULTIPLE_CPA — нет валидного Grid
   write-enum, не гадали.
2. Расписание показов (`time_board`) МК не переносится — `MasterCampaignSpec` собирает
   `_TIME_BOARD_ALWAYS` (7×24); переносится только таймзона. `relevance_match.active=False`
   невыразим (`campaign.py:1553` всегда шлёт `active: True`).
3. `_copy_is_uac_grid_row` опирается на `"tp6_"/"tp7_"` в имени (подстроки "uac" в
   `GdUnifiedCampaign` НЕТ) → cleanup пропустит МК с нетиповым именем. Не опасно (не удалит
   лишнего), но хрупко при переименованиях.
4. Картинки TEXT-объявлений в v5-ветке (`direct_copy.phase_upload`, баг 6) — корень не тронут;
   сейчас перекрывается доливкой в `step_adaptive_creatives`.
5. **Автопочинка по итогам сверки** (решение Семёна: «отчёт + автопочинка чего может»).
   Сейчас шаг report-only. Кандидат: `grid_finalize.set_campaign_invariants()` уже умеет ставить наш
   стандарт (alt texts / extended geo / company info); промо — `csteps.step_attach_promos`.
2. **Перенос найденных настроек при копировании** (корень): `hasAddMetrikaTagToUrl`,
   `hasExtendedGeoTargeting`, `isAlternativeTextsEnabled`, `isOrganicSearchEnabled`,
   `placementTypes`, `promoExtension` — не копируются вовсе.
3. **Шкала загрузки очереди копирования** + **разрез шаблона** `copy.html` → `copy.html` (Авто) +
   `copy_other.html` + общий partial — отдано агенту `seoadvanced_designer`, результат НЕ проверен.
4. **Стратегия**: субагент добавил `grid_finalize.restore_pay_for_conversion_strategy()` (Grid-путь)
   + вызов в постпроцессе — НЕ верифицировано живым прогоном. Рабочий v5-рецепт (проще, куки не
   нужны) — см. баг 4 выше: PriorityGoals + `Operation:"SET"`.
5. Общий код: `copy_main.py:41` тянет `automation_runtime.py` (3948 стр) → в copy-процессе 39
   модулей `direct.*`. Падение/правка любого ломает оба сервиса (доказано: `_enabled_minus_places`).

## 🔧 Пункт 7 (перенос настроек, найденных сверкой) — частично

**Диагностика (direct_investigator) + проверка фактом.** 6 расхождений разобраны по природе.

### ИСПРАВЛЕНО (v5, безопасно, доказано зондом)
**Общий корень 3 полей: whitelist Settings.** `direct_copy.py` переносил `TextCampaign.Settings`
из источника с ЧЁРНЫМ списком (только REQUIRE_SERVICING/SHARED_ACCOUNT). Но `campaigns.get` отдаёт
read-only опции (`DAILY_BUDGET_ALLOWED` и др.), которых НЕТ в enum `campaigns.add` → одна такая
опция роняла весь add (8000) → fallback пересоздавал кампанию БЕЗ Settings → галочки уходили в
дефолты Директа. Заменено на БЕЛЫЙ список `_COPY_SETTINGS_WHITELIST` (фактический enum add,
полученный зондом от API). Чинит:
- `hasAddMetrikaTagToUrl` (ADD_METRICA_TAG) — зонд: NO переносится, привязка счётчика НЕ включает
  тег обратно (гипотеза про side-effect счётчика ОПРОВЕРГНУТА фактом → `_copy_apply_metrika` не трогал);
- `hasExtendedGeoTargeting` (ENABLE_AREA_OF_INTEREST_TARGETING) — зонд: NO;
- `isAlternativeTextsEnabled` (ALTERNATIVE_TEXTS_ENABLED) — в v5 ОКАЗАЛСЯ writable (вопреки
  предположению диагноста), зонд: NO.
Зонд: кампания с Settings источника создалась без ошибки, все 3 опции = NO (= источник). Удалён.

### ОСТАЛОСЬ (Grid-only / промо — НЕ трогать вслепую в общем коде)
- **isOrganicSearchEnabled** — Grid-only поля (в v5 Settings нет). Copy не зовёт finalize/инварианты.
- **placementTypes** [SEARCH_PAGE] — Grid-only. `grid.set_campaign_placement_types` в copy НЕ зовётся.
- **promoExtension** — промо выброшено домен-гейтом `_copy_filter_snapshot` (`copy_engine.py:185-190`):
  оставляет промо только если домен его Href ∈ доменов объявлений; для копии на другой домен матч не
  проходит → `snapshot.promotions=0` → промо не создаётся. Фикс: не выбрасывать промо, привязанное к
  выбранной кампании (домен всё равно переписывается при создании). Привязку умеет `step_attach_promos`.
- **⛔ set_campaign_invariants НЕПРИГОДЕН на копиях** — падает на `enum 'DEFAULT'` (доказано):
  `_narrow_bases`/`_strategy_update_payload` отдаёт HIGHEST_POSITION как strategyName='DEFAULT',
  невалидное на запись. `set_campaign_disabled_places` проходит только потому, что берёт лишь
  СЕТЕВЫЕ кампании (search-only manual-bid туда не попадает). Для Grid-инвариантов копий нужен
  узкий апдейт по шаблону disabled_places + гард strategyName in {DEFAULT,''} → _unsupported_strategy.
  Заявка для direct_fixer/direct_neyrodirektolog.

## 🔧 Задача 8 (Grid organic/placement + промо) — частично, стратегии ЦЕЛЫ

Исполнитель direct_neyrodirektolog, проверено главной сессией ФАКТОМ (v5 + Grid).

### Сделано
- **organic + placementTypes**: root-cause — `set_campaign_organic_and_placement` меняла только
  кампанейные флаги, а реально управляют `biddingStategyWithPlatforms.platforms.organic/.gallery`.
  Теперь патчатся оба уровня. Новый шаг `copy_steps.step_fix_organic_placement`, подключён в
  постпроцесс (`copy_engine.py:2104`). Исправлено на кампаниях с ПОДДЕРЖИВАЕМОЙ стратегией
  (холодные: organic True→False, placement [ADV_GALLERY,SEARCH_PAGE]→[SEARCH_PAGE]).
- **3 guard в `_unified_campaign_update_from_edit_row`** (grid_finalize.py:666/673/679): DEFAULT
  (ручные ставки), OPTIMIZE_CLICKS (макс кликов), MULTIPLE_CPA — помечаются `_unsupported_strategy`
  → узкие Grid-апдейты их ПРОПУСКАЮТ, не роняя запрос и НЕ меняя стратегию.
- **Промо**: root-cause НЕ домен-гейт, а невалидные FieldNames `Status`/`State` в `promotions.get`
  (`direct_copy.py` phase_pull) → 8000 → пустой список → snapshot.promotions=0. Удалены. Доказано
  зондом: со Status/State — ошибка, без них — промо источника 0→1 (need-number.ru).

### ✅ ГЛАВНОЕ: стратегии НЕ сломаны
v5-проверка всех 5 кампаний ПОСЛЕ работы агента: Search-стратегии совпадают с источником 1:1
(HIGHEST_POSITION, WB_MAXIMUM_CLICKS, PAY_FOR_CONVERSION_MULTIPLE_GOALS, WB_MAXIMUM_CONVERSION_RATE,
SERVING_OFF). `strategyName=AUTOBUDGET` в Grid-payload — внутреннее write-имя резолвера, НЕ смена
стратегии (проверено: реальная v5-стратегия та же).

### Осталось (заблокировано Grid write-enum — НЕ гадать)
- organic/placement на 3 кампаниях (DEFAULT/OPTIMIZE_CLICKS/MULTIPLE_CPA): нет валидного write-имени
  стратегии в Grid enum → узкий апдесит пропускает их. Только ручное изменение в интерфейсе ИЛИ
  реверс Grid write-enum ручных стратегий (отдельная экспертная задача).
- Промо: код исправлен, но для текущего porg-jh2si7rh нужен НОВЫЙ copy run (старый снэпшот без промо).

## Сессия 2026-07-31 — live copy porg-2wkbqwqe → porg-uy3huxcn: adaptive media batching

- **Прогон:** `copy_start`, 48 кампаний: 22 v5 TEXT + 26 Grid/UAC, target `autopark777.site`,
  `counter=110883157`, `goal=586896806`, job `28cba2b9d714`.
- **Итог job:** `done=48`, `created=48`, `failed=0`, но терминальный `error` из postprocess:
  `updateListingAds(feed-filter) UNAVAILABLE_FIELD` + Grid source-read gaps. Очередь перед стартом ждала
  same-agency create job ~18 мин; само выполнение от `started_at` до error ~24м50с.
- **Проверено live:** кампаний target 48 (26 grid + 22 v5), все DRAFT. В именах кампаний старого
  `Краснодар*` нет; в 26 UAC campaign names найден `r####`, wrong_r=0; v5 group names: 101/101
  с `r0002`, wrong_r=0.
- **Дефект media подтверждён:** `adaptive_creatives` подготовил 303 объявления, 315 image remap,
  78 carousel cards, но `UpdateAdaptiveTextAds` вернул `0/303`. Live-read target:
  `image_count_dist={0:49,1:291,2:12}`, дублей внутри imageHashes нет, `five_same=0`, но
  `img_mismatch=303/352`, `mc_mismatch=303/352`, `multicard_ads=0`.
- **Цены:** live target `adPrice` есть у 276/352 adaptive ads; это совпадает с postprocess
  `проставлено 276/276`, еще 76 без цены = 49 без content/images + 27 без цены по фиду.
- **Фикс:** `_grid_update_adaptive_ads` теперь бьёт adaptive full-replace на чанки по 50, пишет
  top-level/validation диагностику в journald, делает per-ad fallback, а при отказе `multicards`
  повторяет без карусели, чтобы не терять 5 основных картинок. `step_adaptive_creatives` теперь
  добавляет ошибку в rep при `updated < len(items)`, а не оставляет молчаливый `0`.
- **Тесты:** `py_compile create/create_set_feeds.py copy_service/copy_creative_steps.py`; focused pytest
  `test_copy_adaptive_creatives_remaps_multicards`, `test_grid_update_adaptive_ads_preserves_multicards`,
  `test_grid_update_adaptive_ads_chunks_large_payload`,
  `test_grid_update_adaptive_ads_falls_back_without_multicards` — 4 passed.

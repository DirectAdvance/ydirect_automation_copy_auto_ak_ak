# 📒 Журнал ошибок создания кампаний — нейродиректолог

> **Назначение:** каждая повторяющаяся ошибка создания РК фиксируется здесь: сигнатура → root-cause →
> метод решения → **помогло или нет** (проверено живым прогоном). Перед фиксом любой ошибки —
> СНАЧАЛА искать её здесь: возможно, решение уже известно или уже пробовали и не помогло.
>
> **Правило заполнения:** новая ошибка = новая запись со статусом `🟡 фикс задеплоен, ждёт прогона`.
> После живого прогона обновить: `✅ подтверждено прогоном <дата>` или `❌ не помогло → следующая гипотеза`.
> Обновляет тот, кто чинит (сессия Claude — в конце работы, вместе со STATE.md).

## Формат записи

```
### <СИГНАТУРА_ОШИБКИ> — краткое имя
- Симптом: точный текст ошибки / как выглядит для пользователя
- Где: tp, путь (token/cookie/Grid/UAC), файл:функция
- Root-cause: механика почему
- Решение: что сделали (файл, суть правки, дата)
- Статус: ✅ подтверждено прогоном <дата> | 🟡 ждёт прогона | ❌ не помогло (что дальше)
- НЕ помогло ранее: (если были неудачные попытки — обязательно, чтобы не повторять)
```

---

## Активные / недавние ошибки

### UNAVAILABLE_FIELD_LISTING_FILTER — name-фильтр «Страницы каталога» валит чанк, каталог = весь фид (D4, 2026-07-09)
- Симптом (live porg-psm5h7q6, кампания 712605238): «Страницы каталога» tp5 показывают ВЕСЬ фид вместо
  бренда. `listing_name_set=0`. В логе (если дошло) — Grid `updateListingAds` отвергал чанк
  `PerformanceFilterDefects.UNAVAILABLE_FIELD`, чанк молча терялся («chunk потерян, skip»).
- Где: `grid_finalize.py:set_listing_name_filters` (~1573); вызовы —
  `create_set_tp1_builders.py:_grid_add_listings_with_name_filters` (~873) и cookie-путь (~1634).
- Root-cause: захардкоженное условие фильтра `{"field":"name","operator":"CONTAINS_ANY",...}`. У авто-фида
  yandex.xml (AUTO_RU) поля `name` в `fieldsForUseAs` НЕТ → Grid отвергает весь чанк `UNAVAILABLE_FIELD` →
  chunking-обёртка ловит `GridFinalizeError` и `skip` (тихая потеря) → `listing_name_set=0` → листинг без
  позитивного name-фильтра → каталог = весь фид. brand/model в соседних фильтрах УЖЕ резолвятся через
  `_resolve_feed_field` (`create_set_tp1_builders.py:851-852` → mark_id/folder_id), а `name` — нет. Это
  backlog-запись H (резолвить name через fieldsForUseAs) — до сих пор не сделано.
- Решение (2026-07-09):
  - `create_set_feeds._resolve_feed_field` расширен семантикой `'name'` (`_NAME_FIELD_SYNONYMS =
    ("name","model","modification","folder_id")` — Market-фиды: `name`; AUTO_RU: текстовое имя каталога).
  - `set_listing_name_filters`: поле условия резолвится per-feed через `_resolve_feed_field(login,feed_id,
    "name")`, фолбэк `'name'` (Market/сбой резолва). НЕ терять чанк молча: при `UNAVAILABLE_FIELD`/
    `UNKNOWN_FIELD`/`INVALID_FIELD` в validationResult — лог + ретрай чанка со следующим полем-кандидатом
    (per-feed резолв → доступные текстовые поля фида из `_feed_filter_fields` → явный `'name'`);
    исчерпаны кандидаты → `GridFinalizeError` (chunking-обёртка залогирует, но теперь это редкий терминал,
    а не тихий первый отказ). Фикс внутри метода → оба пути-вызова (grid/cookie) чинятся автоматически.
- Статус: 🟡 фикс на Mac (py_compile OK; pyflakes — только штатные DI-«undefined» модуля, новых нет),
  ждёт живого прогона (единый рестарт после Фазы 1). Верификация: live tp1/tp5 с фидом — `listing_name_set>0`,
  «Страницы каталога» показывают ТОЛЬКО бренд группы; в логе при AUTO_RU — «UNAVAILABLE_FIELD → ретрай с
  полем …» вместо «chunk потерян, skip». **live не проверено.**
- НЕ помогло ранее: (запись `LISTING_NAME_FILTER_ADGROUPID_UNDEFINED`, 08.07) чинила `id` vs `adGroupId` —
  другой корень (идентификатор листинга); поле `name` там не резолвилось. fix-2 (adgroup_id→adGroupId)
  — GraphQL-схема поля не знает. Это отдельный дефект того же метода.

### DELAYED_CONTENT_REPAIR_STUCK_RUNNING — delayed content_repair зависает в running, добивка не завершается (К1, 2026-07-09)
- Симптом (Victory DB): delayed-строки `978fc858255f`/`43c6046ebc77` (kind=content_repair) застряли в
  `running` навсегда (note «повторная Grid-first проверка перед авто-добивкой»). Система #2 (spec_audit
  качества контента) НЕ отработала → brand-first/short-titles/видео-tp1/чужие-ключи не добились; карточка
  набора вечно «running».
- Где: `blueprint.py:_delayed_repair_daemon_loop` watchdog (~2038); терминалы `_run_delayed_content_repair`
  (~1776, child `dcr:{did}` через `_parent_absorb_child_progress(...,final=True)`).
- Root-cause: LLM-фиксеры (`_run_spec_audit_and_fix` → regen_titles/brand-first через `_llm_pair_for`) висли
  на мёртвом M3 (до фикса `M3_COMPLETION_HANG_CIRCUIT_BREAKER`) → весь delayed-repair цикл блокировался, строка
  не доходила до терминала. Watchdog-UPDATE `running→failed` (>30 мин) kind-агностичен и флипал БД-строку, но
  child-closure в watchdog был реализован ТОЛЬКО для `finalize_set` (задача F) → у content_repair child
  `dcr:{did}` оставался ОТКРЫТ → карточка вечно `running` даже после флипа строки в failed (осиротевший
  delayed-repair).
- Решение (2026-07-09):
  - Watchdog `_delayed_repair_daemon_loop` теперь собирает из `RETURNING` не только `finalize_set`, но и
    `content_repair*` строки (`startswith("content_repair")`, покрывает `content_repair_post_recreate`).
    Для них — тот же терминал, что у finalize_set: `_record_delayed_content_repair(..., status="failed")` +
    `_parent_absorb_child_progress(parent, f"dcr:{did}", 0,0,0, final=True)` → child закрыт, карточка
    доходит до терминала. finalize_set-блок не тронут (обрабатывается отдельно, выше).
  - Тайм-бокс: B2-бюджет `_DELAYED_REPAIR_TIME_BUDGET_SECONDS=1200` (< watchdog 1800) УЖЕ покрывает основной
    цикл content_repair (`_run_delayed_content_repair:1839`, чистый partial без reschedule при исчерпании).
    Остаточный `_run_spec_audit_and_fix` (после цикла, не под B2) теперь ограничен пофиксерно idle-таймаутом
    30с + M3 circuit-breaker'ом (`M3_COMPLETION_HANG_CIRCUIT_BREAKER`) → «один завис фиксер» больше не вешает
    весь проход, а любой остаточный застрявший `running` закрывается watchdog'ом.
- Статус: 🟡 фикс на Mac (py_compile OK, blueprint 0 новых undefined-name), ждёт живого прогона (единый
  рестарт после Фазы 1). Верификация: искусственно застрявший content_repair `running`>30 мин → watchdog
  флипает строку в failed И закрывает `dcr:{did}` (карточка терминальна, не вечный running); нормальный
  delayed-repair и finalize_set watchdog (F) не затронуты; reschedule cap не тронут. **live не проверено.**
- НЕ помогло ранее: сам по себе watchdog-UPDATE (running→failed) — флипал БД-строку, но не закрывал child
  content_repair → карточка всё равно висла running.

### M3_COMPLETION_HANG_CIRCUIT_BREAKER — висящий M3-completion = 90-120с налога на каждой РК (2026-07-09)
- Симптом (боевой прогон 09.07, job 2ec305f1c3cc): M3 completion висит — НЕ присылает ни одного
  токена. Стрим-idle-таймаут срабатывает через 90с (14B fast) / 120с (72B repair), потом фолбэк на
  OpenRouter. Health-preflight `_m3_preflight_ok` (только GET /v1/models) ПРОХОДИТ (модели-эндпоинт
  жив), а completion мёртв → до 4 обращений к M3 на РК × 90-120с впустую = главный тормоз набора.
- Где: `llm_providers.py:_llm_pair_for._url` (health-only preflight), `_m3_preflight_ok`;
  content-вызовы `create_content.py:167` (14B fan-out idle 90/360) + `:442` (72B) + repair-таймауты
  (`_M3_LLM_REPAIR_TIMEOUT=120`); set-старт гейт `create_set_orchestrator.py` (`check_content_pipeline_health`).
- Root-cause: health GET (`/v1/models`) жив ≠ `/chat/completions` жив. Гейт и preflight проверяли
  ТОЛЬКО эндпоинт-liveness, не генерацию → мёртвый completion проходил гейт и платил полный idle-
  таймаут на КАЖДОЙ РК (без circuit-breaker — повторно 90-120с снова и снова весь набор).
- Решение (2026-07-09):
  1. **Completion-preflight** (`llm_providers.m3_completion_preflight_ok`): реальный 1-токенный
     completion (`max_tokens=1`, короткий idle `M3_COMPLETION_PREFLIGHT_TIMEOUT=9с`, 2 попытки).
     Вызывается ОДИН раз на набор в set-старт гейте (`create_set_orchestrator`), не на каждой РК.
     Ловит «health GET жив, completion висит».
  2. **Circuit-breaker на набор** (`_M3CircuitBreaker`, thread-safe Lock — prefetch 3w/каналы C1):
     флаг «M3 мёртв на этот набор». Взводится: (а) completion-preflight провалился (взводится сразу
     на старте, `arm_m3_breaker(run_key, tripped=True)`); ИЛИ (б) 2 реальных зависания M3 по ходу
     набора (`M3_BREAKER_TIMEOUT_THRESHOLD=2`, только `_is_m3_hang` — «зависла/нет токенов», не
     HTTP/пустой). Пока взведён — `_llm_pair_for._url` пропускает M3-сторону (primary ИЛИ фолбэк) и
     идёт на OpenRouter, БЕЗ повторных idle. Сбрасывается на новый набор (`arm` с новым run_key=job_id).
  3. **Idle content-вызовов 90/120/360 → 30с** через env `M3_CONTENT_IDLE_TIMEOUT` (не хардкод):
     `create_content.py` 14B fan-out + 72B-патч + repair-таймауты. Со стримингом (E) idle = пауза
     МЕЖДУ токенами; живой M3 ~6.5 ток/с (гэп <1с) → 30с не рвёт рабочий M3, только висящий (0 токенов).
     Прокинут DI-параметром `_M3_CONTENT_IDLE_TIMEOUT` (ai_content → create_content), re-export в blueprint.
- Выбор провайдера в попапе СОХРАНЁН (index.html:3927-3931 `payload.llm_provider=prov`): breaker —
  СТРАХОВКА поверх выбора. M3-primary мёртв → авто-фолбэк на OpenRouter на весь набор; OpenRouter-
  primary → breaker лишь не даёт тратить idle на мёртвый M3-фолбэк. Двусторонний `_llm_pair_for`
  сохранён. Дефолт (не выбрано) не тронут — openrouter.
- Гейт-абортит теперь ТОЛЬКО когда генерировать нечем: M3-completion мёртв И OpenRouter мёртв (раньше
  смотрел any_alive по health GET → пропускал в 90с-таймауты).
- Статус: 🟡 фикс на Mac (py_compile OK, pyflakes 0 undefined-name). Изолированный тест (фейковые
  локальные SSE-серверы hang/alive, без реального M3/RK/баллов) — 6/6: (1) висящий M3 → preflight=False
  за ~6с (2×3с idle), не 90с; (2) живой M3 → True мгновенно; (3) breaker взведён + M3-primary → прямой
  OpenRouter, M3 не дёргается; (4) сброс на новый набор → M3 снова дёргается; (5) 2 зависания → breaker
  сам взводится, 3-й вызов M3 пропускает; (6) OpenRouter-primary + breaker → M3-фолбэк пропущен. НЕ
  рестартовал (идёт боевой прогон, рестарт — главная сессия после прогона). **live не проверено** —
  вступит в силу на след. рестарте.
- НЕ помогло ранее: health-only preflight (GET /v1/models) — проходит на висящем completion (сам корень).

### IMG_PREUPLOAD_SLEPOK_KEY_UNDEF — G пред-заливка картинок падала NameError, оптимизация мёртвая (2026-07-09)
- Симптом (direct-worker, боевой прогон porg-psm5h7q6, job 2ec305f1c3cc): `[img-preupload] porg-psm5h7q6:
  прогрев картинок не удался (best-effort): name '_SLEPOK_KEY' is not defined`. Best-effort → джоба не
  падает, но набор-level пред-заливка (задача G) НЕ выполняется → каждая tp1-РК грузит картинки per-РК
  (старый путь), G-оптимизация не работает.
- Где: `create_set_tp1_builders.py:_preupload_tp1_images` (строка ~1330, использует `_SLEPOK_KEY`);
  запуск — фон-поток `create_set_orchestrator.py:~509` (`from .create_set_tp1_builders import
  _preupload_tp1_images` сырьём).
- Root-cause: `_SLEPOK_KEY` (и ВСЕ прочие имена в функции: `kp`/`gf`/`_ct_segment`/
  `_creative_images_for_ct`/`_parallel_upload_images`) — DI-инъекции модуля через
  `configure(deps)`→`globals().update(deps)`. configure на `create_set_tp1_builders` ЛЕНИВЫЙ — вызывается
  только внутри blueprint-обёртки `_create_set_tp1_builder_module()` (её дёргают рабочие точки входа
  `_build_tp1_adgroups`/`_create_tp1_via_cookie`/`_tp1_pack_groups`). Соседние функции (631/1397) видят
  `_SLEPOK_KEY`, т.к. к моменту их вызова обёртка уже прогнала configure. А фон-поток импортил
  `_preupload_tp1_images` СЫРЬЁМ (в обход обёртки) и стартовал РАНЬШЕ любого tp1-вызова → globals модуля
  ещё пустые → NameError на первом же инъектируемом имени (`_SLEPOK_KEY`). Фикс только резолва
  `_SLEPOK_KEY` бесполезен — сдвинул бы краш на `kp`/`_ct_segment`; лечить надо гарантию configure.
- Решение (2026-07-09): (1) новая blueprint-обёртка `_preupload_tp1_images(*a,**k)` →
  `_create_set_tp1_builder_module()._preupload_tp1_images(...)` (байт-в-байт как рабочий сосед
  `_tp1_pack_groups`-обёртка:7431) — гарантирует configure()→все DI-глобалы до запуска; (2) имя добавлено
  в `_create_set_orchestrator_deps` names; (3) орк дёргает `deps.get('_preupload_tp1_images')` вместо
  сырого импорта (`callable`-гейт). Резолв slepok-key теперь идентичен рабочим точкам входа модуля.
- Статус: 🟡 фикс на Mac (py_compile OK; pyflakes — только штатные DI-«undefined» модуля, новых нет;
  wiring подтверждён: обёртка+deps+орк), ждёт прогона. НЕ рестартовал (идёт боевой прогон, рестарт —
  главная сессия). Верификация след. прогоном: в логе `[img-preupload] … resolved=N … кэш прогрет` вместо
  NameError; per-РК заливка попадает в прогретый кэш. **live не проверено.**
- НЕ помогло ранее: —

### API_FIRST_STREAK_NO_RESET — units_fail_streak не сбрасывался, ложный флип набора на куку (2026-07-09)
- Симптом (Codex-ревью P2, флаг DIRECT_API_FIRST=ON, прод OFF — спит): строгий флип на куку срабатывал
  по НЕпоследовательным 152. Два изолированных транзиентных 152, разделённых успешными token-созданиями,
  накапливали `ch.units_fail_streak` до порога `_API_FIRST_FLIP_STREAK=2` и флипали ВЕСЬ канал набора на
  cookie-путь — хотя замысел и комментарий = только ПОДРЯД идущие подтверждённые сбои (реальное
  исчерпание баллов).
- Где: `create_set_orchestrator.py` строгая ON-ветка флипа (~573-582, `ch.units_fail_streak += 1`).
- Root-cause: `units_fail_streak` инкрементился при каждом реальном 152, но НИКОГДА не сбрасывался, когда
  последующий item создавался успешно без нового units-маркера → счётчик считал разрозненные сбои как серию.
- Решение (2026-07-09): после while-скана результатов пункта, ДО блока флипа, добавлен
  `if _API_FIRST and not _new_units_fail: ch.units_fail_streak = 0`. `_new_units_fail=False` = пункт
  завершился без реального 152 (успех token / units-маркер с ok / не-152) → серия оборвана, счётчик в 0.
  Порядок: сброс ПЕРЕД инкрементом → на реальном 152 (`_new_units_fail=True`) сброс пропускается, инкремент
  отрабатывает штатно; после реального флипа `via_cookie=True` → счётчик уже не важен.
- Флаг/безопасность: гейт `_API_FIRST`. При OFF инкремент не исполняется, `units_fail_streak=0` всегда →
  строка-сброс no-op → поведение байт-в-байт. py_compile+pyflakes чисто (0 undefined).
- Статус: 🟡 фикс на Mac (под флагом, прод OFF), деплой отдельным шагом, ждёт прогона с DIRECT_API_FIRST=ON.
  Верификация: два транзиентных 152 через успешный item НЕ флипают набор на куку; флип только при 2 подряд
  ИЛИ units_alive=False. **live не проверено (флаг OFF в проде).**
- НЕ помогло ранее: —

### ASYNC_FINALIZE_ENQUEUE_NONE_LOST — потеря финализации при enqueue()==None (2026-07-09)
- Симптом (Codex-ревью P2, флаг DIRECT_ASYNC_FINALIZE=ON, прод OFF — спит): при ON финализ-обёртки
  (`_finalize_rsya`/`_finalize_search_via_grid`) уже пропустили inline Grid-финализацию (capture-guard
  вернул []). В done-блоке воркера, если `enqueue()` вернул None (ошибка БД / нет коннекта / ON CONFLICT),
  захваченные `_rec.specs` МОЛЧА терялись → созданные кампании оставались БЕЗ финализации (места показа,
  ассеты, кампанийные инварианты, минус-наборы), которую синхронный путь бы отработал. Джоба помечалась
  зелёной (done) без finalize_pending.
- Где: `blueprint.py` воркер, done-enqueue (~2573-2594).
- Root-cause: результат `enqueue()` проверялся только на truthy для `_parent_absorb_child_start`; ветки
  «enqueue упал» не было → specs терялись без фолбэка и без пометки.
- Решение (2026-07-09): при `enqueue()==None` — inline-replay захваченных specs прямо в воркере через
  `run_finalize_job({"result": {"specs": _rec.specs}})` (ТЕ ЖЕ реальные функции, что delayed-демон:
  `finalize_rsya`/`finalize_search_via_grid` из `csfq.configure`, идемпотентно). Если inline-replay
  отработал частично (`remaining>0`) → пометка `finalize_pending` (inline_replay + applied/remaining/failed
  + error) в result → summary НЕ зелёный, подберёт повторный проход/ручная докрутка. `remaining==0` → всё
  применено inline, зелёный корректен. Выбран inline-replay (а не только finalize_pending), т.к. при
  enqueue-None строки в `direct_delayed_repairs` нет → demon бы её не подобрал.
- Флаг/безопасность: при OFF `register` не пишет в `_RECORDERS`, `unregister` → None → `_rec is None` →
  вся новая ветка (else enqueue + блок `_finalize_inline`) пропускается → байт-в-байт. Inline-replay
  синхронно блокирует воркер, но только на редком enqueue-None пути (лучше блок, чем тихая потеря
  финализации). py_compile+pyflakes чисто (0 undefined).
- Статус: 🟡 фикс на Mac (под флагом, прод OFF), деплой отдельным шагом, ждёт прогона с DIRECT_ASYNC_FINALIZE=ON.
  Верификация: при искусственном сбое enqueue (обрыв rw-conn) финализация всё равно применяется inline;
  при частичном провале — джоба несёт finalize_pending, не зелёная. **live не проверено (флаг OFF в проде).**
- НЕ помогло ранее: —

### CAMPAIGN_INVARIANT_DOD_GAP_P0 — кампанийные галочки tp1–tp5 не проверялись/не добивались пост-аудитом (2026-07-09)
- Симптом (дыра DoD §1.c P0, не «живая» ошибка, а пробел петли): пост-аудит tp1–tp5 (Grid-путь) ловил
  только группо/объявленческие коды; кампанийные инварианты-галочки (персонализация #3, расш.гео #5,
  «Директ помогает» #6, ценовые рек., Карты/организации, yandexMaps, serpGeoWizard) НЕ верифицировались и
  НЕ добивались. У UAC tp6/tp7 покрыто `uac_verifier`; у поиска/РСЯ — нет. Дрейф шаблона (кейс J,
  захардкоженный startDate валил finalize → галочки не выставлялись) или окно async-финализации (F) на
  этих инвариантах оставались навсегда.
- Где: `grid_content_verifier.verify_grid_content` (не было campaign-level секции); чтение —
  `grid_read.campaign_content_counts` (не читало кампанийные поля); ремонт — отсутствовал.
- Root-cause: verifier читал только counts групп/объявлений; кампанийные toggle-поля НИКТО не читал и не
  чинил in-place.
- Решение (2026-07-09):
  - **Чтение (edit-view — единственная Grid read-схема с этими полями; live rowset их не отдаёт):**
    `grid_finalize.read_campaign_invariants` (CampaignsEditData) → `grid_read._enrich_campaign_invariants`
    → tri-state поля в `campaign_content_counts` (`campaign_invariants_read` gate).
  - **Детект:** campaign-level секция в `grid_content_verifier` — новые коды `ALT_TEXTS_ENABLED_LIVE`,
    `EXTENDED_GEO_ENABLED_LIVE`, `RECOMMENDATIONS_ENABLED_LIVE`, `PRICE_RECOMMENDATIONS_ENABLED_LIVE`,
    `COMPANY_INFO_ENABLED_LIVE`, `MAPS_ENABLED_LIVE`, `ORG_LIST_ENABLED_LIVE` (error, чинятся) +
    `STRATEGY_MISMATCH_LIVE` (warn, report-only). Флаг ТОЛЬКО при `campaign_invariants_read=True` И явном
    булеве (None=не прочитано → тишина — fail-safe против Grid-лага/FieldUndefined, журнал I).
  - **Ремонт (in-place, БЕЗ баллов, DRAFT, идемпотентный):** `campaign_invariant_repair` →
    `grid_finalize.set_campaign_invariants` = узкий `UpdateCampaigns` (шаблон set_campaign_disabled_places):
    RMW полного unified-payload из edit-view + override ТОЛЬКО инвариантных полей (те же константы, что
    `create_set_finalize:211-216`/`grid_finalize.finalize:280-291`). Подключён в авто-петлю
    `repair_auto.execute_all_in_place` (delayed-цикл) через planner/gate (executable_now). Read-back
    через `read_campaign_invariants`. Блик-радиус ложного детекта = один безвредный повторный
    UpdateCampaigns (НЕ удаление, в отличие от recreate-ремонтов журнала I).
- Осознанно НЕ покрыто: **#4 мониторинг сайта** (`hasSiteMonitoring`) — поля НЕТ в read-схеме Grid
  (`grid_campaigns_edit_data.graphql`, и CampaignsBroadMatch ставит None) → не детектируется, лишь
  переставляется (=True) ремонтом. **#2 UTM-на-группах** и **1.4 глоб.минус на кампании** — группо-уровень
  / нужен shared-set-id: детект без ремонта зациклил бы reschedule «до нуля» → вынесены в P1.
- Флаг/безопасность: не под флагом; синхронный create-путь НЕ исполняет invariant-repair (только delayed,
  где Grid-лаг ушёл); ремонт никогда не удаляет. UAC tp6/tp7 не тронуты (guard `tp in 1..5`).
- Статус: 🟡 фикс на Mac (py_compile+pyflakes чисто; unit-тест verifier: fail-safe на None, 7 кодов при
  всех-wrong→1 repair-кандидат, strategy report-only без repair; planner→gate→executable_now=1 подтверждён),
  деплой отдельным шагом, ждёт живого прогона. Верификация Семёна: live tp1–tp5 после набора —
  `isAlternativeTextsEnabled=False`, `hasExtendedGeoTargeting=False`, `isRecommendationsManagementEnabled=False`,
  `enableCompanyInfo=False`, `yandexMaps/serpGeoWizard=False`; при искусственном включении галочки —
  delayed-цикл её гасит через `campaign_invariant_repair`. **live не проверено.**
- НЕ помогло ранее: (журнал I/J) детект/фикс через group-level edit-view сразу после create — лаг реплики
  → ложный детект сносил хорошее. Здесь: чтение на уровне КАМПАНИИ + fail-safe None + ремонт=UpdateCampaigns
  (не удаление) → ложный детект максимум безвреден.

### TP24_TOKEN_AUTOTARGET_EDITVIEW_LAG — token-путь tp2/tp4 отдавал ok:True без корректного автотаргета (2026-07-09)
- Симптом (аудит DoD, до включения DIRECT_API_FIRST): на token-пути tp2/tp4 группы создавались v5 `adgroups.add` (без relevanceMatchCategories → дефолт Яндекса «все 5 + 3 бренда»), автотаргет добивал `_grid_set_search_autotarget` = `groups_for_edit(cid)` (edit-view с ЛАГОМ реплики) + `update_unified_adgroups`. При пустых группах/исключении молча `return 0`, вызывающий результат НЕ проверял → кампания отдавалась `ok:True` БЕЗ `EXACT_V2_MARK`/`WITHOUT_BRAND` → WRONG_AUTOTARGET (карусель, журнал J/I).
- Где: `create_set_feed_builders.py:_create_text_via_token` (шаг 3) + `_grid_set_search_autotarget` (~254); наполнение групп `create_set_text_builders.py:_build_tp2_adgroups` Фаза 1.
- Root-cause: тот же корень, что забракованный TP5_AUTOTARGET v2 — Grid не видит только что созданные v501-группы через `UpdateUnifiedAdGroups` на реплике (edit-view lag); best-effort return 0 не отличим от «всё ок».
- Решение (2026-07-09, эталон v3): **Вариант 1 — атомарный Grid** (консистентно журналу TP5_AUTOTARGET v3):
  - `_build_tp2_adgroups` Фаза 1 создаёт группы через `gc.GridCreateClient(login).add_adgroups(gc.build_adgroup(autotargeting_profile="search_tp2", keywords=[], minus_keywords=<группа>))` — relevanceMatch (EXACT_V2_MARK + WITHOUT_BRAND) ставится АТОМАРНО при создании (lag-проблемы нет). Ключи — только Фаза 2 (v5 AddKeywords), объявления — Фаза 3 (v501). Позиционный сдвиг защищён `_read_adgroup_name_to_id`. `rep["relevance_match_set"]=rep["adgroups"]`.
  - Фаза 2 autotarget-ветка: v501-спецключ `---autotargeting` больше НЕ добавляется (сбросил бы relevanceMatch в дефолт — та же грабля, что чинили для tp5, `_build_tp1_adgroups:296`).
  - `_create_text_via_token`: `_rm_set=int(build.get("relevance_match_set") or 0)` вместо вызова `_grid_set_search_autotarget`. Если Grid-группы упали → build без adgroups → кампания удаляется + defer/фолбэк (ok:True без автотаргета невозможен).
  - `_grid_set_search_autotarget` помечен УПРАЗДНЁН (не вызывается, анти-паттерн edit-view).
  - deps: в `_create_set_text_builder_deps()` добавлены `gc`/`gf` (раньше не прокидывались → Фаза 3.4 gf молча падала в except; при OFF token-путь не исполнялся).
- Флаг/безопасность: весь token-путь под DIRECT_API_FIRST (прод OFF). `_build_text_from_pack`/`_build_tp2_adgroups` вызываются ТОЛЬКО из `_create_text_via_token` → при OFF не исполняются → поведение байт-в-байт. Баллы: ключи/объявления по units (v5/v501), группы — Grid без баллов (гибрид как tp5); фолбэк на куку при 152 сохранён.
- Статус: 🟡 правки на Mac (py_compile+pyflakes OK, blueprint 0 undefined-name), деплой отдельным шагом, ждёт живого прогона. Верификация Семёна: live `relevanceMatchCategories=["EXACT_V2_MARK"]` у всех групп tp2/tp4 token-пути; `relevance_match_set == adgroups`. **live не проверено.**
- НЕ помогло ранее: (v2 tp5) пост-патч `UpdateUnifiedAdGroups` без groups_for_edit — Grid не видит свежие v501-группы на реплике. Тот же вывод для tp2/tp4 → атомарный путь.

### TP6_MANUAL_AGE_25_NOT_35 — ручной tp6 исключал только 18-24 вместо 35+ (2026-07-09)
- Симптом (пробел DoD §3.6): `create_set_master_product.py:518` ставил `age_lower="age_25"` для ручного tp6 (КС/аудитория) — исключал только брекет 18-24. DoD требует 35+ (исключить ОБА младших брекета 18-24 И 25-34).
- Root-cause: `socdem.age_lower` — пороговое поле непрерывного диапазона (`campaign.py:1084/1505`); `age_25` = диапазон стартует с 25-34 → 18-24 отсечён, 25-34 остаётся.
- Решение (2026-07-09): `age_lower=("age_18" if (targeting_mode=="autotarget" or is_product) else "age_35")`. `age_35` = диапазон с 35-44 → оба младших брекета вне охвата. Та же enum-семья, что age_18/age_25. Автотаргет-режим tp6 (age_18 by design) и tp7 (age_18, is_product) НЕ тронуты. Комментарий в `create_set_plan.py` (было «24-55+») синхронизирован на «35+».
- Статус: 🟡 прод-путь (не под флагом), фикс на Mac, ждёт прогона. Верификация Семёна: live socdem tp6-ручной = «35 и старше». **live не проверено.**
- НЕ помогло ранее: —

### ASYNC_FINALIZE_WATCHDOG_ORPHAN_CHILD — карточка виснет running при watchdog-fail finalize-строки (2026-07-09)
- Симптом (задача F, самоотметка F-агента в STATE): при DIRECT_ASYNC_FINALIZE=ON, если watchdog демона (`_delayed_repair_daemon_loop`, stuck running >30 мин → `status='failed'`) убивал застрявшую finalize-строку, child `fin:{did}` НЕ закрывался → карточка вечно `running` с невыставленными инвариантами (Карты OFF / места показа #3-#6), `finalize_pending` не снят.
- Где: `blueprint.py:_delayed_repair_daemon_loop` watchdog-UPDATE (~2044). Терминальный путь `_run_delayed_finalize:2018-2032` (снятие finalize_pending + `_parent_absorb_child_progress(final=True)`) при watchdog-fail не исполнялся.
- Root-cause: watchdog флипал строку `running→failed` в БД, но не проходил терминал закрытия child.
- Решение (2026-07-09): watchdog-UPDATE + `RETURNING id,parent_job_id,kind`; для строк `kind='finalize_set'` — снять `finalize_pending` (`_parent_update`, ставит `finalize_finished:failed`) + `_parent_absorb_child_progress(parent, f"fin:{did}", 0,0,0, final=True)` (тот же терминал, что нормальный путь). `_parent_update`/`_parent_absorb_child_progress` читают parent из БД (source of truth) → безопасны после рестарта.
- Флаг/безопасность: строки `finalize_set` существуют ТОЛЬКО при DIRECT_ASYNC_FINALIZE=ON (создаются capture-путём) → при OFF список пуст, no-op. Нормальный dcr-путь (content-repair) не тронут.
- Статус: 🟡 под флагом (прод OFF), фикс на Mac (py_compile OK, blueprint 0 undefined-name), ждёт прогона с флагом ON. **live не проверено.**
- НЕ помогло ранее: —

### SEGMENT_TP5_DEFERRED_SELF_REFERENCE — токен-докрутка сегментного tp5 самозатирается в done, tp5 теряется (2026-07-09)
- Симптом: deferred `721641cad7c1` / job `23677e1473d1` (porg-psm5h7q6, victorylotsofads1, 08.07) завершился `done` с «докручено по куке: создано 0, не создано 2». 2 сегментных tp5 (`search_gallery`, сегменты «Марки» и «Общее», Кемерово) НЕ созданы и потеряны без следа. errors_log ссылается «докрутка токеном запланирована (721641cad7c1)» — на САМУ СЕБЯ.
- Где: cookie-резюм сегментного tp5. `create_set_gallery.py:run_create_set_gallery` (NO_BRAND-ветка) → `blueprint.py:_deferred_save` (дедуп) → `create_set_orchestrator.py` (финал по `body._deferred_id`) → `blueprint.py:_resume_one_deferred`.
- Root-cause (петля из 3 звеньев): (1) резюмящаяся строка была `status='resumed'` и содержала тот же item → дедуп в `_deferred_save` (поиск по имени среди waiting/resumed) вернул ЕЁ id → self-reference, новая токен-строка НЕ создавалась; (2) финал джобы по `body._deferred_id` пометил эту же строку `done` → токен-ретрай уничтожен; (3) для тел с `_resume_via_token=True` резюм мог всё равно уйти на cookie (пустой st_token / preflight-152 форсит via_cookie) → NO_BRAND повторялся бы вечно. NB: у реальной строки `721641…` в body НЕ было `_resume_via_token` вовсе (только `_web_posted`) — т.е. первично сработало звено (1)+(2), а не (3).
- Решение (2026-07-09):
  - **Fix-3 (главный для инцидента):** `_deferred_save(..., exclude_id=None)` — дедуп исключает текущую резюмящуюся строку (`id <> exclude_id`). `create_set_gallery` при планировании токен-ретрая передаёт `exclude_id = job.body._deferred_id` и `pop("_deferred_id")` из тела новой цепочки → создаётся РЕАЛЬНАЯ новая token-строка, финал старой её не гасит.
  - **Fix-2:** `_resume_one_deferred` для `_resume_via_token` резолвит токен+баллы ДО постановки джобы. Нет токена → `bump_resume_at(1ч)`+`waiting`; баллы исчерпаны → `resume_at≈сброс`+`waiting`. Джоба ставится ТОЛЬКО при токен+баллы → воркер идёт API-путём, не cookie. Строка не помечается done несуществующим финалом.
  - **Fix-1 (defensive):** `create_set_gallery` для `_resume_via_token` сегментного tp5, попавшего на cookie-путь, возвращает `defer_keep` (не масскарад создания, не self-reference deferred_save). Финал (`create_set_orchestrator`) при `defer_keep` оставляет строку `waiting` (демон повторит токеном), а не гасит в done.
- Статус: 🟡 задеплоено на LXC101 (direct.service+direct-worker.service active, py_compile+pyflages+import OK), ждёт живого прогона. Верификация: revive `721641cad7c1` (status=waiting, resume_at=now) → должна появиться НОВАЯ token-строка (`_resume_via_token=true`), а не self-reference; после токен-прогона — 2 tp5 «Марки»/«Общее» созданы.
- НЕ помогло ранее: прошлые фиксы 06-07.07 (не требовать st_token в gallery, наследовать resume_count) — не закрывали self-reference дедупа: строка всё равно возвращала свой id и гасилась в done.

### LISTING_NAME_FILTER_ADGROUPID_UNDEFINED — set_listing_name_filters отклоняет весь чанк (2026-07-08)
- Симптом: `updateListingAds` chunk отклоняется Grid с «adGroupId not defined for GdUpdateListingAdInput»; listing_name_set=0; каталог показывает весь фид вместо бренда.
- Где: `grid_finalize.py:set_listing_name_filters` (строка ~1487); вызовы из `create_set_tp1_builders.py:_grid_add_listings_with_name_filters` (~823) и cookie-путь (~1634).
- Root-cause: `GdUpdateListingAdInput` не содержит поля `adGroupId` — только `id` (id листингового объявления). Но все пути строили item с `{"adGroupId": adgroup_id}` вместо `{"id": lid}`. Path 3 (`grid_create.py:908`) использовал `id: lid` правильно, но `shoppingAdId` не запрашивался в query → `said=None` → `lf_items` пуст → тихий 0.
- Решение (2026-07-08, fix-3):
  - `grid_finalize.py:add_listing_ads_by_shopping_ads` — добавлен `shoppingAdId` в `addedAds{id shoppingAdId}`.
  - `grid_finalize.py:set_listing_name_filters` — `else: _entry["adGroupId"]` заменён на `else: continue` (item без `id` пропускается).
  - `create_set_tp1_builders.py:_grid_add_listings_with_name_filters` — переход на shoppingAdId-матч через `listing_name_by_shop` (уже готовый `{shop_id: name_value}`); use `id: lid`.
  - `create_set_tp1_builders.py` cookie-путь (~1617) — аналогично; удалён adGroupId-фолбэк (~1631-1635).
- Статус: 🟡 фикс на Mac, ждёт деплоя + прогона. Верификация: `listing_name_set > 0` после создания tp1/tp5 с фидом; каталог показывает только нужный бренд.
- НЕ помогло ранее: fix-2 (adgroup_id → adGroupId через set_listing_name_filters) — GraphQL-схема это поле не знает.

### GEO_225_NOT_OBLAST — гео = вся Россия(225) вместо области аккаунта (2026-07-08)
- Симптом: черновики psm5h7q6 (Кемерово) все с RegionIds=[225], имена правильные «Кемеровская область».
- Где: prefill `blueprint.py:4867` + форма `index.html:654` (дефолт `value="225"`); применение `create_set_account.py:40` `or [225]`.
- Root-cause: prefill звал `_geo_id(city,region)` → брал ГОРОД (64), а на несовпадении имени `region`-колонки со словарём Директа падал в 225. Форма несла хардкод `value="225"`. Бэкенд `_account_ctx.geoid` УЖЕ давал правильную область (11282), но форма перебивала.
- Решение (2026-07-08): prefill → `_account_ctx(login).geoid` (область 11282), убран хардкод `value="225"`→`""`. Мультигород (lzjk6p5m, «6 городов») `_account_ctx` не умеет → там 225 (Семён: оставить).
- Статус: 🟡 задеплоено, ждёт прогона. Старые черновики (225) — чинить v5 `adgroups.update` за баллы.

### MINUS_WORDS_MISSING_TP5_TP3_PRODUCT — глобальные минус-слова не на tp5/tp3/товарке (2026-07-08)
- Симптом: минус-слова из «Глобальных правил» (`direct_global_minus_words`, «отзывы») не проставлены. tp1/tp2 — есть (групповой минус), tp5/tp3/товарка — нет.
- Где: `grid_create.py:build_unified_campaign` хардкод `minusKeywords:[]`; галерейные группы `minus_keywords=[]`; товарка только при `targeting_mode=='keywords'`.
- Root-cause: кампанийный минус не заполнялся для cookie-типов через unified.
- Решение (2026-07-08): `build_unified_campaign` принимает `minus_keywords`, кап через `_minus_char_budget` (20k); прокинуто из билдеров. Code-review: для campaign-mode `spec.minus_keywords=[]` (единственный путь — `_apply_campaign_direct_minus`), убран дубль; ИСПРАВЛЕН NameError `_enabled_minus_words` в create_set_feed_builders (был бы краш на tp2/tp4/tp3/tp5 при первом наборе).
- Статус: 🟡 задеплоено, ждёт прогона. Верификация: live `negativeKeywords` tp5-кампании = [«отзывы»].

### MINUS_MODELS_CT_GROUP_NOT_SKIPPED — минус-модель не убирает ct-группу (2026-07-08)
- Симптом: отмеченная минус-модель добавляла фид-фильтр, но группа по её ct всё равно создавалась. Марки — работали.
- Где: `create_set_tp1_builders.py` блок минус-фильтра (~602, ~1268).
- Root-cause: `raw_brand` = полное «BAIC U5 Plus» (с брендом) сравнивалось точным `in` с множеством голых моделей `{"u5 plus cng",...}` → никогда. Марки в БД латиница-каноника → `_brand_canon` матчил.
- Решение (2026-07-08): `_enabled_minus_model_pairs()` (mark,model), карта бренд→{модели}, точный матч модель-порции В ПРЕДЕЛАХ бренда. Марки не тронуты.
- Статус: 🟡 задеплоено, ждёт прогона.

### CATALOG_LISTING_FILTER_ZERO_V2 — tp7 каталог (ct0000) 0 страниц после фикса CONTAINS mark_* (2026-07-08)
- Симптом: «Страницы каталога» tp7 ct0000 = 0 страниц вместо всех 198+. Кампания создаётся, UAC не бросает исключение.
- Где: `create_set_master_product.py:477-481`, ветка `elif is_product and it_feed:`.
- Root-cause: предыдущий фикс (2026-07-08) включил `_tp7_listings_minus_filters` для ct0000 — функция шлёт CONTAINS(mark_* коллекции). Страницы каталога НЕ входят ни в одну mark_* коллекцию → 0 результатов. UAC принимает фильтр без ошибки (тихий неверный результат) → except-retry-без-фильтра не срабатывает.
- Решение (2026-07-08): для ветки ct0000 `it_lff = []` (без listings_feed_filters → весь каталог). ct0111 и другие нетоварные без c_brand/c_ct по-прежнему вызывают `_tp7_listings_minus_filters`.
- Статус: 🟡 фикс на Mac, ждёт деплоя + прогона. Верификация: «Страницы каталога» ct0000 = все страницы (не 0).
- НЕ помогло ранее: (1) PATCH listings_feed_filters пост-создание = MUST_BE_NULL. (2) CONTAINS(mark_*) — даёт 0 для каталога.

### SITELINK_CREDIT_DUPLICATE — дубль УТП «кредит/платёж» в быстрых ссылках (2026-07-08)
- Симптом: одновременно «Платеж от 9 000 ₽ в месяц» (реальный) и «Автокредит от 9 000 ₽/мес» (филлер) в наборе сайтлинков.
- Где: `blueprint.py:5744` `_GENERIC_SITELINK_FILLERS[0]` + `_norm_sitelinks_for_v501`.
- Root-cause: (а) `_variant_norm_key()` схлопывает числа в `#`, но НЕ дедуплит по смысловой теме — «платёж» и «автокредит» дают разные ключи; (б) филлер «Автокредит от 9 000 ₽/мес» всегда добавлялся в конец, не зная что кредит-тема уже занята реальной ссылкой.
- Решение (2026-07-08): (1) удалён «Автокредит от 9 000 ₽/мес» из `_GENERIC_SITELINK_FILLERS` (остаётся 8 филлеров — правило не нарушено); (2) добавлен семантический topic-дедуп в `_norm_sitelinks_for_v501`: кредит/платёж/взнос/рассрочк → тема "credit", не более 1 ссылки на тему. `seen_topics` добавлен рядом с `seen`.
- Статус: 🟡 фикс на Mac, ждёт деплоя + прогона. Верификация: в наборе 8 ссылок, нет двух с «кредит»/«платёж»/«взнос».
- НЕ помогло ранее: числовой дедуп `_variant_norm_key` — числа схлопываются, но смысловые темы остаются разными.

### SITELINKS_ONLY_1_OF_8 — одна быстрая ссылка вместо 8 (регрессия, 2026-07-08)
- Симптом: в кампании 1 sitelink вместо 8.
- Где: `blueprint.py:6623/6641`, `ai_agents.py:751/1578`, `create_content.py:796`.
- Root-cause: РЕГРЕССИЯ — `SITELINK_TITLE_TARGET_MIN` подняли 22→28 как цель генерации, но её же использовали жёстким порогом приёмки → филлеры (22-26) и короткие ссылки дропались.
- Решение (2026-07-08): отдельный `SITELINK_TITLE_MIN_ACCEPT=18` в 3 гейтах; code-review: source-order приоритет реальных ссылок (не сортировка по длине — она выталкивала реальные), филлеры добивают до 8.
- Статус: 🟡 задеплоено, ждёт прогона.

### TITLE_BRAND_ORDER_MISSING — марка/модель не до точки в заголовках (2026-07-08)
- Симптом: LLM генерирует заголовки «Кредит от 9 000 ₽/мес. BAIC» вместо «BAIC. Кредит от 9 000 ₽/мес» — марка оказывается ПОСЛЕ первого УТП, а не перед ним.
- Где: `ai_agents.py:build_titles_messages` — правила порядка отсутствовали.
- Root-cause: промпт имел «БРЕНД — ПОДЛЕЖАЩЕЕ» (общий принцип), но явного правила «в первом сегменте ДО точки» не было → модель произвольно ставила марку в конец.
- Решение (2026-07-08): в `build_titles_messages` после строки «БРЕНД — ПОДЛЕЖАЩЕЕ» добавлено условное правило (если `brand` задан): «✅ ПОРЯДОК В ЗАГОЛОВКЕ: марка/модель ставится ДО первой точки». Пример в промпте конкретный.
- Статус: 🟡 фикс на Mac, ждёт деплоя + прогона. Верификация: заголовки с brand содержат марку в первом сегменте.

### GLOBAL_MINUS_SHARED_SET_GAP — global_minus_words не попадают на tp2/tp4 через feed_builder_deps (2026-07-08)
- Симптом: у tp2/tp4 (cookie-путь, `_create_tp2_campaign`/`_create_tp4_campaign` в create_set_feed_builders) глобальные минус-слова («отзывы») не попадают на уровень кампании. `minus_keywords: (_DEPS.get("_enabled_minus_words") or (lambda: []))()` возвращает `[]` — `_DEPS` не содержит ключа.
- Где: `blueprint.py:_create_set_feed_builder_deps()` (строка ~7220) — ключ `_enabled_minus_words` отсутствовал в возвращаемом dict. `create_set_feed_builders.py:133` и `:315` — safe-get из `_DEPS`.
- Root-cause: при добавлении minus_keywords в cookie-путь tp2/tp4 (сессия v9) ключ был прокинут в другие dep-дикты (tp1: строка 6158, tp3/tp5: 6475/6589), но в `_create_set_feed_builder_deps()` забыли добавить. Safe-get `(lambda: [])()` молча возвращал пустой список — без ошибки, без видимого симптома.
- Решение (2026-07-08): добавлен `"_enabled_minus_words": _enabled_minus_words` в `_create_set_feed_builder_deps()` (`blueprint.py:7228`). Inline NegativeKeywords (через spec) и SharedSet (через finalize) аддитивны — конфликта нет.
- Статус: 🟡 фикс на Mac, ждёт деплоя + прогона. Верификация: tp2/tp4 cookie-кампании имеют `negativeKeywords: ["отзывы"]` в Grid после создания.
- НЕ помогло ранее: —

### TP5_AUTOTARGET_ALL_CATEGORIES — tp5 все галочки автотаргета (2026-07-08)
- Симптом: в tp5 включены все 5 категорий + 3 бренд-настройки вместо `EXACT_V2_MARK`/`WITHOUT_BRAND`.
- Где: `create_set_tp1_builders.py:_build_tp1_adgroups` (v501 adgroups.add без relevanceMatch) → дефолт Яндекса.
- Root-cause: v501 `adgroups.add` не умеет задавать `relevanceMatchCategories` → Яндекс ставит дефолт (все 5 + 3 бренда). Пост-патч через UpdateUnifiedAdGroups хрупкий: v501→Grid replication lag → группы не видны в edit-view → `relevance_match_deferred=True`.
- Решение v1 (2026-07-08, сессия v9): ужесточён шаг 4.5 — retry×3×2с. Хрупкость осталась.
- Решение v2 (2026-07-08): Фаза 1.5 (post-create UpdateUnifiedAdGroups из известных данных, без groups_for_edit). Lag не устранён — сам Grid может не видеть только что созданные группы через v501.
- Решение v3 (2026-07-08, ФИНАЛЬНЫЙ):
  - **Фаза 1 для tp5** в `create_set_tp1_builders.py:_build_tp1_adgroups` — вместо v501 `adgroups.add` используется `gc.GridCreateClient(login, cookie=grid_cookie).add_adgroups(items)` с `gc.build_adgroup(autotargeting_profile="search_tp2")`. relevanceMatch (EXACT_V2_MARK + WITHOUT_BRAND) ставится АТОМАРНО при `AddUnifiedAdGroups` — нет lag-проблемы, нет двух шагов.
  - **Фаза 1.5** упразднена полностью.
  - **Шаг 4.5** в `create_set_feed_builders.py` упразднён полностью — заменён комментарием.
  - Позиционный сдвиг защищён: при `len(ag_ids) != len(groups)` → `_read_adgroup_name_to_id` (аналог `create_full:615`).
  - Фазы 2 (keywords.add v501), 3 (ads.add v501), 3.4 (Grid repair), 4 (Shopping/Listing), 5 (корректировки) — без изменений, работают по тем же `ag_ids`.
- Статус: 🟡 фикс на Mac, ждёт деплоя + прогона. Верификация: live `relevanceMatchCategories = ["EXACT_V2_MARK"]` у всех групп tp5; `relevance_match_set > 0` в ответе.
- НЕ помогло ранее: (v1) retry×3×2с — lag непредсказуем. (v2) Фаза 1.5 без groups_for_edit — сам Grid мог не найти v501-группы через UpdateUnifiedAdGroups на реплике.
- Грабля: ключи передавать ТОЛЬКО через Фазу 2 (AddKeywords v501), НЕ через build_adgroup keywords=[фразы] — Grid AddUnifiedAdGroups дублирует их для групп <~140 ключей.

### VIDEO_NO_POOL — видео нет у части марок (НЕ баг, 2026-07-08)
- Симптом: у ~26 марок psm5h7q6 нет видео.
- Root-cause: НЕ баг. Video-пул `/Users/Shared/agency/Video/<ct>/` покрывает 4 марки (BAIC/Belgee/Haval/Москвич), аккаунт торгует ~30. Привязка (per-ct breaker + 2 ретрая + brand-fallback + «до нуля» переочередь) исправна — `video_no_pool` by design.
- Решение: код НЕ править. Наливать video-пул роликами недостающих марок (задача контента/пака).
- Статус: ✅ диагностировано (не код). `video_no_pool` теперь виден в отчёте.

### COPY_LOGIN2LOGIN_GRID_BRANCH_GAPS — 5 дефектов копировщика при login→login (2026-07-08)
- Симптом (по факту от Семёна на боевом копировании кабинета): 1) кодер группы не перекодирован Краснодар→Уфа; 2) нет быстрых ссылок и уточнений у скопированных объявлений; 3) местами URL из «подборок» (чужой/дефолтный); 4) в имени РК регион не по кодеру; 5) в фидах не проставлен текст по умолчанию.
- Где: grid-cookie ЕПК-ветка `_copy_grid_unified_campaigns` / `_copy_grid_unified_steps` (`copy_engine.py`), срабатывает при login→login (0 v5-баллов) — НЕ v5 `phase_upload`. Это урезанная параллельная реализация заливки: не портированы кодер-осведомлённость (r-код), sitelinks, `set_default_text`.
- Root-cause по пунктам:
  - 1/4: регион в кодере (`ag_part4`) зашит КОДОМ `_r0300_`, а геоморф `apply_replacements` меняет только словоформы по `\b` — код словами не задеть. Копировщик был полностью кодер-неосведомлён (`grep coder|r0000` пуст).
  - 2a: код-пути для sitelinks не было — pull не читал `inheritableSitelinkSet`, шага attach не существовало.
  - 2b: `_copy_grid_bridge_callouts` при source_grid=None делал ТИХИЙ no-op → уточнения молча терялись.
  - 5: инлайновое создание товарных пропускало `grid.set_default_text` (в отличие от «правильного» `create_shopping_content`).
  - 3: `_copy_target_href` делал наивную замену ОДНОГО домена `href.replace(src,target)` → href на чужом хосте (подборка/турбо/маркетплейс) уезжал без замены.
- Решение (2026-07-08, `copy_engine.py` + `copy_steps.py` + `blueprint.py` +1 строка DI):
  - 1/4: `_copy_target_region_code` (из DI `_resolve_region`→(r_code,oblast), пропуск `r0000`/невалида) + `_copy_remap_region_code` (regex `(?<=_)r\d{4}(?=_)`). Применён к имени РК (`_copy_normalize_campaign_name`) и группы. `_resolve_region` инъектится в `_ce.configure` (blueprint.py:7421). Один источник r-кода на РК и группу.
  - 2a: новый `step_attach_sitelinks` (copy_steps.py) + pull `inheritableSitelinkSet`→campaign_sitelinks.json; `get_sitelink_sets`→`add_sitelink_set`→`set_campaign_sitelink_set`, геоморф+домен title/href, дедуп набора в `maps['sitelinks']`.
  - 2b: при наличии callout-id и source_grid=None → `raise` → `rep['errors']` (не тихий no-op).
  - 5: `grid.set_default_text(shop_ids, feed_id, text, filters_by_ad_id)` после `add_shopping_ads`; текст из ТГО группы→бренд, фильтры vendor+минус-марки.
  - 3: `_copy_target_href` доменно-агностичный (urlsplit): свой домен/поддомен→перенос пути, чужой хост→голый target без 404-пути.
- Ревью-фиксы (/code-review, тот же деплой):
  - CONFIRMED: `zip(shop_ids, shop_items)` рассинхрон — `if x` выкидывал None из ПОЗИЦИОННОГО списка `add_shopping_ads` → vendor-фильтр на чужой товар. Fix: `_shop_pairs` спариваем id↔item ДО отброса None.
  - PLAUSIBLE: дубли sitelink-наборов при create с пустым id → sentinel `failed_sets`, не ретраим на кампанию.
- Файлы: `copy_engine.py`, `copy_steps.py`, `blueprint.py` (DI `_resolve_region`).
- Деплой: LXC101 `direct-copy.service` :5022, md5 Mac==LXC (Mutagen), рестарт active, smoke 302, remote AST OK, py_compile+pyflakes чисто.
- Статус: 🟡 фикс задеплоен, ждёт живого прогона (реальное копирование login→login с аккаунтами НЕ гоняли — код и деплой доказаны, поведение на данных нет).
- НЕ помогло ранее: —

### RECREATE_VIA_COOKIE_AT_LIVE_UNITS — recreate-добивка укатилась в куку/деферред при живых баллах (2026-07-07)
- Симптом: 5 tp5 recreate ушли по куке (и частично в отложенную докрутку) при живых агентских баллах — зря, надо было токеном сразу.
- Где: recreate-путь добивки, `repair_gate.repair_queue_body` (жёстко `via_cookie=True`) → `repair_auto.queue_recreate_repair_job` (transport «cookie_grid», units не проверялись).
- Root-cause: recreate всегда форсил cookie независимо от остатка баллов; политика «баллы первичны» не применялась.
- Решение: `queue_recreate_repair_job(units_alive=…)` — при живых баллах снимает `via_cookie` (recreate идёт токеном v501, на 152 сам падает в куку), transport=`token_v501`; `_units_alive_for_login` проброшен через repairing-deps. Параллельно: сегментный tp5 (NO_BRAND_SEGMENTS в `create_set_gallery.py` и fix_generic_fallback_group в `campaign_spec_audit.py`) при живых баллах → resume_at=now() (добивка токеном сразу, демон `_RESUME_POLL` 600→120с), а не next_units_reset. (2026-07-07)
- Статус: 🟡 фикс задеплоен?—нет, ждёт рестарта direct.service + живого прогона.
- НЕ помогло ранее: —

### FOREIGN_MODEL_KEYWORDS_IN_MODEL_GROUP — ключи чужой модели в модельной группе (2026-07-07)
- Симптом: группа tp2 «Changan CS35Plus» (gid=5770871724) получает 52 ключа «changan cs75 …» из пака ct0031. Ключи чужой модели (CS75) попадают в модельную группу, ухудшая релевантность.
- Где: `text_gen._filter_group_keywords`, сегмент «Модели»; пак контента ct0031/keywords/scherbakova.txt (68 ключей CS75).
- Root-cause: грязный пак ct0031 содержит ключи других моделей той же марки; код `_filter_group_keywords` для seg=«Модели» возвращал пул «как есть» — чужемодельные ключи не фильтровались.
- Решение (2026-07-07): защитный фильтр в `_filter_group_keywords` (новый параметр `model: str = ""`). Для seg=«Модели» при непустом model — дискриминирующие токены чужих моделей той же марки (из `brand_models_catalog.json`) дропают ключи. Новые функции: `_model_subtokens`, `_foreign_model_discriminators` (кэш на процесс). Прокинут `model=brand` в 5 колл-сайтов: `create_set_text_builders.py:355`, `create_set_tp1_builders.py:631+1252`, `create_set_repairing.py:184`, `campaign_spec_audit.py:198`.
- Файлы: `text_gen.py` (функция добавлена), 4 колл-сайта выше.
- Смоук-тест: все 5 тест-кейсов задачи пройдены (cs75→дроп, цс75→дроп, cs35plus→ок, cs35→ок, uni-k→дроп).
- Статус: ✅ чистка выполнена 2026-07-07: `fix_foreign_model_kw_psm.py` удалил 151 keyword_id из 15 групп аккаунта porg-psm5h7q6; read-back: 0 чужемодельных ключей. Код-фикс (фильтрация при генерации) на Mac, ждёт деплоя основной сессией.
- НЕ помогает: чистка самого пака ct0031 (заплатка в коде закрывает ВСЕ грязные паки, не только этот).
- Грабля: `_CT_RE = re.compile(r"\bct\d+\b")` не матчит `ct0031_aon_...` (underscore — word-char, нет \b). Фикс: убрать trailing `\b` → `re.compile(r"\bct\d+")`.

### SHORT_TITLES_48 — заголовки объявлений короче 48 символов (2026-07-07)
- Симптом: ResponsiveAd (Grid) в tp1/tp2/tp3/tp4/tp5 аккаунта porg-psm5h7q6 содержат заголовки 41-47 символов — не добивают до правила «остаток ≤8 символов». До чистки: 2879 коротких заголовков (bucket 9-15: 2754, bucket 16+: 125).
- Где: `text_gen._fill_title` (генерация), аудит `campaign_spec_audit.py` (детектор `SHORT_TITLES`).
- Root-cause: `_fill_title` использовал разделитель `. ` (2 символа) даже для заголовков на `.!?…`. Заголовок 47 символов + ". " + суффикс 8 символов = 57 > 56 → суффикс не вставлялся, заголовок оставался 47. Порог детектора `_TITLE_SHORT_LEN = 45` (≤45) не ловил 46-47 символов.
- Решение (2026-07-07):
  1. `text_gen._fill_title`: смарт-разделитель — заголовок на `.!?…` → `" "` (1 символ), иначе `". "`.
  2. `campaign_spec_audit.py`: `_TITLE_SHORT_LEN = 47` (ловит <48), tp1 trigger `n_short >= 1`, UAC trigger `not short`.
  3. `fix_short_titles_psm.py`: repair script — исправил 849 ResponsiveAd за один батч-вызов Grid.
- Финал (2026-07-07, 2-й заход): 1136 «застрявших» на 47 симв. добиты после добавления
  СВЕРХКОРОТКИХ хвостов ≤7 симв («Выгодно», «Онлайн») в ОБА банка — `ai_agents.TITLE_FILL_SUFFIXES`
  (его использует repair) и `text_gen._TITLE_TAILS` (генерация): для остатка 9 с «. » влезает
  только хвост ≤7, минимальные были 8. Read-back: **10920/10920 заголовков с остатком ≤8, нарушений 0**.
- Статус: ✅ подтверждено read-back 2026-07-07; все код-фиксы задеплоены (рестарт 2026-07-07).
- Грабля 2: банков суффиксов ДВА (text_gen._TITLE_TAILS для генерации, ai_agents.TITLE_FILL_SUFFIXES
  для repair) — пополнять оба.
- Грабля: Grid UpdateAdaptiveTextAds падает с «голых» items (только `titles`, нет `href`) если RMW-чтение в `_grid_update_adaptive_ads` выполняется ДВАЖДЫ в одном процессе (CSRF-конфликт двух GridClient). Фикс: один `gc` для RMW-чтения + update в repair script (обход через `_grid_update_responsive_direct`).

### OLD_PRICE_MISSING — половина моделей без старой цены (2026-07-07)
- Симптом: в объявлениях tp1–tp5 заполнена только новая цена; «Старая цена» пустая у 156/318 моделей psm.
- Root-cause (слои): (1) `_merge_price` предпочитал МИН-current даже без old — пара из соседнего фида затиралась; (2) в товарных фидах у многих моделей нет `<oldprice>`; (3) в авто-фиде `yandex.xml` старая цена лежит НЕ в oldprice, а = `price + max_discount` (Семён).
- Решение (2026-07-07, create_set_feeds.py): приоритет ПАРЫ в `_merge_price`; парсер `_auto_feed_discount_prices` (yandex.xml: old = price+max_discount, tries+backoff); пост-проход «годовой ключ наследует пару без-годового». Фиды перебираются ВСЕ (≥3 требование перекрыто).
- Замер psm: пары 162 → **340 из 389** (87%), без old 156 → **49**.
- Статус: ✅ подтверждено замером 2026-07-07; остаток 49 — модификаций (Largus фургон/CNG, Tiggo plug-in hybrid) нет ни в одном фиде, добавит только генератор фида сайта.

### GENERIC_FALLBACK_GROUP / NO_BRAND_SEGMENTS_AVAILABLE — одинаковые tp5 (инцидент Щербакова 2026-07-06)
- Симптом: 5 tp5-кампаний porg-psm5h7q6 идентичны — у каждой одна generic-группа ct0000 «Товарная галерея», хотя в имени кампании сегмент (Марки/Модели/Общее).
- Где: tp5, cookie-путь; `create_set_gallery.py` (создание), `campaign_spec_audit.py:465` (детект).
- Root-cause: `_create_shopping_via_cookie` НЕ поддерживает segment → для всех сегментов создавал одну generic ct0000-группу, тихо маскируя это как «успех». Cookie-путь навязывался upstream-докруткой после error 152.
- Решение (2026-07-06): guardrail в `create_set_gallery.py:66` — сегментный tp5 по куке = явный провал `NO_BRAND_SEGMENTS_AVAILABLE` + авто-план докрутки ТОКЕНОМ на сброс баллов (`_resume_via_token=True`, не зацикливается по куке). Детектор `GENERIC_FALLBACK_GROUP` в аудите (`campaign_spec_audit.py:888`).
- Статус: ✅ подтверждено 2026-07-07 (guardrail в бою: NO_BRAND→deferred→пересоздание токеном; ложный детект закрыт guard'ом живых ключей). Авто-починка УЖЕ созданных одинаковых tp5: `fix_generic_fallback_group` (campaign_spec_audit.py:1531, 2026-07-06) — DRAFT-гейт → удаление пустышки по куке → deferred с `_resume_via_token=True` на сброс баллов → пересоздание токеном с бренд-группами; дедуп деферредов по (login, item name); подключён в `_run_spec_audit_and_fix`.
- НЕ помогло ранее: бесконечный повтор докрутки по куке (та же ошибка вечно) — поэтому retry только токеном.

### Error 152 (Insufficient points / баллы Direct API)
- Симптом: создание текстовых/РСЯ РК падает `152: Not enough units`, набор создан частично.
- Где: все token-пути (v5/v501/UAC); отбойник в `blueprint.py` (deferred).
- Root-cause: суточный лимит баллов агентского токена исчерпан.
- Решение: (1) Мастер/Товарка — фолбэк на Grid/cookie без баллов (`_create_tp1_via_cookie`, `grid_create.py`); (2) остаток — deferred-докрутка с `resume_at = сброс баллов`; (3) 2026-07-06: докрутка встаёт В НАЧАЛО очереди (`_job_new(priority=True)` + `_priority` в БД-пути), не в конец.
- Статус: фолбэк ✅ давно в проде; приоритет очереди ✅ активен (рестарты 2026-07-06/07), подтверждён живыми докрутками psm.

### MAX_KEYWORDS_PER_AD_GROUP_EXCEEDED — группа оставалась с 0 ключей
- Симптом: заливка ключей отклонялась ЦЕЛОЙ пачкой, группа без ключей → NO_KEYWORDS_LIVE.
- Где: добивка ключей, `repair_executor.py`.
- Root-cause: заливали >200 ключей в группу (лимит Яндекса 200) → вся пачка reject.
- Решение (2026-07-05): кап `_KW_MAX_PER_GROUP=200`, `final_kw[:200]` + лог усечения.
- Статус: ✅ подтверждено прогоном 2026-07-05 (psm 9677 ключей, ozge 3749, zero=0).

### Ложный NO_KEYWORDS_LIVE — добивка крутилась вхолостую
- Симптом: верификация репортит «нет ключей», хотя ключи живые; авто-добивка повторяется без эффекта.
- Где: верификация, `grid_read.py`.
- Root-cause: читали `groups_for_edit.keyword_count` (edit-view Grid лагает → 0 при живых ключах).
- Решение (2026-07-05): `grid_read._show_condition_kw_counts` — реальные GdKeyword через showConditions с пагинацией; edit-view — только фолбэк; батч всех кампаний в 1 запрос.
- Статус: ✅ подтверждено (psm cid712191112 zero=0/9677; ozge cid712191085 zero=0/3749).

### Гейт delayed-repair не пускал keywords-план
- Симптом: авто-добивка «завершилась», но keyword-репейры не исполнены.
- Где: `blueprint.py::_live_plan` (delayed-repair).
- Root-cause: счётчик `cnt` учитывал только content+promo+callout+rename → план из одних keywords давал `inplace_cnt=0` → `break` до `execute_all_in_place`.
- Решение (2026-07-05): `cnt = executable_now − queued_recreate_items` (все in-place действия).
- Статус: ✅ подтверждено (авто-добивка сама исполнила 6 psm / 4 ozge keyword-репейров).

### Sitelink-hang — tp5 финализировался без сайтлинков
- Симптом: финализация tp5 зависала >170с на генерации сайтлинков, кампании без быстрых ссылок.
- Где: `blueprint.py::_ai_common_sitelinks` / `_gen_campaign_content`.
- Root-cause: item без `llm_provider` → M3-дефолт (перегружен) висел.
- Решение (2026-07-04, 17d18e9): `llm_provider=openrouter` в item + дефолт провайдера openrouter (50с/8 сайтлинков). Затем 2026-07-05: статический резерв сайтлинков + href-backfill (LLM давал href=None → Grid отбрасывал).
- Статус: ✅ подтверждено; регрессию «резерв затенял реальные v5-сайтлинки» поймали в код-ревью 2026-07-05 и убрали (eb1688c).

### cmc NameError — ВСЯ post-create добивка крашилась
- Симптом: после создания ни один дефект не чинился автоматически (8 дефектов качества РК копились).
- Где: post-create добивка, blueprint.py.
- Root-cause: NameError на `cmc` — добивка падала на входе, ничего не чинила.
- Решение (2026-07-04): фикс имени + чистый прогон без рестартов сервиса в середине джоба (рестарт для деплоя рвал прогоны — деплоить ДО прогона).
- Статус: ✅ подтверждено прогонами 2026-07-05.

### Кука: ложный «протух» на клиентских логинах (No rights/code 0)
- Симптом: живые куки помечались протухшими → зря уходили в фолбэк/reset.
- Где: проверка статуса кук, `blueprint.py::_cookies_status_response`.
- Root-cause: allow-list искал «Нет прав»/code:54, а direct*-логины возвращают английское «No rights»/code:0.
- Решение (2026-07-06): переход на deny-list (живая = НЕ содержит маркеров смерти) + пробы по клиентским логинам из БД.
- Статус: ✅ подтверждено тестом — все 6 аккаунтов живые.

### Обрезанный текст объявлений («Одобрение за 30»)
- Симптом: текст объявления обрывается на полуслове/висячем числе в конце.
- Где: усечение текстов, `text_norm.py`.
- Root-cause: усечение по лимиту длины без учёта границы слова и висячих хвостов («за 30» без «минут»).
- Решение (2026-07-06): `_trim_clean` — обрезка по слову + чистка висячих хвостов `_strip_dangling_num_tail`/`_strip_dangling_word_tail`; числовой хвост чистится только если строку обрезали мы (ревью 06.07 — «до 300 000» у нетронутой строки легитимен).
- Статус: 🟡 код в проде (md5 sync OK), подтвердить следующим прогоном.

### CALLOUTS_NOT_CREATED — уточнения не создавались при создании РК (2026-07-07)
- Симптом: precreated_callout_ids = [] у всех кампаний; при finalize attachIDs пустые → уточнения не привязаны. Пул из пака scherbakova (103 текста) не создавался при create_set.
- Где: `precreate.py::execute_precreate_assets`, путь создания callouts.
- Root-cause: `grid_client_factory(login).add_callouts()` → Grid-схема GdAddCalloutsInput не принимается (Unknown type) → исключение → callout_ids=[], callouts_note с ошибкой.
- Решение (2026-07-07):
  1. `create_set_assets.py`: добавлена `v5_ensure_callout_pool(token, login, texts, v5_call_fn, *, cap=20)` — дедуп с существующими через `adextensions.get`, создаёт недостающие через `adextensions.add` батчем (частичные ошибки пропускает), возвращает ≤cap ids.
  2. `precreate.py::execute_precreate_assets`: Grid-путь заменён на v5 — при наличии `v5_call` и `token` вызывает `v5_ensure_callout_pool`; без токена → graceful skip.
  3. `create_set_precreate.py`: добавлен параметр `v5_call`.
  4. `create_set_orchestrator.py`: берёт `_v5_call = deps.get('_v5_call')`, передаёт в `run_create_set_precreate`.
  5. `blueprint.py::_create_set_orchestrator_deps()`: добавлено `"_v5_call"` в names.
- Ремонт porg-psm5h7q6 (2026-07-07): `fix_callouts_psm.py` — создал пул из слепка scherbakova/Мультибренд (103 текста, все уже в аккаунте: 118 существующих), привязал 20 id к 21 не-UAC кампании через `GridClient.set_campaign_callouts`.
- Read-back ДО: 21/21 кампаний с calloutIds (3 шт. каждая — старый минимальный набор). ПОСЛЕ: 21/21 кампаний с calloutIds (**20 шт. каждая** — полный пул слепка). UAC/tp7: 2 кампании пропущены (не поддерживают уточнения).
- Статус: ✅ v5-путь задеплоен, ждёт рестарта. Ремонт porg-psm5h7q6 подтверждён read-back 2026-07-07.
- Грабля: `create_set_assets._dedup_callouts` вызывает `_normalize_callout_text`, которая требует `_CALLOUT_MAX_EACH` из globals-инъекции — в repair-скрипте ВНЕ blueprint-контекста не работает. Решение: `v5_ensure_callout_pool` принимает `v5_call_fn` явным параметром (нет globals-зависимости); repair-скрипт реализует свой `_simple_dedup_ids`.

### TP7_LISTING_FILTER_ZERO — listings_feed_filters.NOT_CONTAINS → 0 страниц каталога (2026-07-07)
- Симптом: блок «Страницы каталога» tp7 (ct0000) показывает «0 из 198» страниц. Кампании 712228385/712228394 аккаунт porg-psm5h7q6 (autos-kemerovo.site), оба DRAFT, оба нулевые.
- Где: tp7 (ct0000 общая), UAC `/web-api/uac/campaign/`, `create_set_feeds._tp7_listings_minus_filters`, `create_set_master_product.py:481` (fallback).
- Root-cause: UAC `listings_feed_filters.collectionId` с оператором `NOT_CONTAINS` обрабатывает его как `CONTAINS` (positive match). 7 условий AND-ятся: страница должна принадлежать всем 7 маркам одновременно — impossible → 0 страниц. API принял фильтр без ошибки при создании (тихий неверный результат). Исходный код создавал по одному `NOT_CONTAINS`-условию на каждую исключаемую марку (7 марок = 7 условий).
- Решение (2026-07-07, `create_set_feeds._tp7_listings_minus_filters`): позитивный allow-list — оператор `CONTAINS` с массивом всех бренд-уровневых (`mark_*`) коллекций фида минус исключённые марки. Тот же оператор и формат, что уже используется в брендовых tp7 (`create_set_master_product.py:474`, HAR-реверс). Границы: нет минус-марок → `[]`; после вычета allowed пуст → `[]` + warning (лучше весь каталог чем 0 страниц).
- Ремонт (2026-07-07): PATCH `listings_feed_filters` отклонён (MUST_BE_NULL) — удалены DRAFT 712228385/712228394 через `client.delete_campaign(cid)`, пересозданы через Flask `test_request_context` + `_create_set_response()` с исходными items из job `e1027cb3cc16`.
- Новые кампании: 712236037 (Автотаргетинг), 712236040 (Общая КС).
- Статус: ✅ подтверждено read-back 2026-07-07 — обе новые кампании: 1 условие, `operators={'CONTAINS'}`, allow-list 27 mark-ID (34 всего в фиде - 7 исключённых: mark_42/KNEWSTAR, mark_18/Москвич, mark_11/Omoda, mark_33/Solaris, mark_41/SOUEAST, mark_40/SWM, mark_35/XCITE; UAZ не был в фиде — пропущен).
- НЕ помогло: PATCH `/web-api/uac/campaign/{id}` с `listings_feed_filters` любого содержания — отклоняется (`DefectIds.MUST_BE_NULL`). Единственный путь ремонта: delete DRAFT + recreate.
- Грабля: `_feed_collections` без `csf.configure(bp._create_set_feeds_deps())` тихо возвращает `[]` — `NameError` на `_block_bootstrap` глотается bare `except Exception`. В repair-скрипте вне blueprint-контекста обязательно вызывать `configure()` до вызова любой csf-функции.
- Грабля 2: repair-скрипт использовал `spec` и для `importlib.util.spec_from_file_location` и для loader-модуля — коллизия имён. Переименовать в `ldr_spec`/`ldr_mod`.

### Бэклог (не ошибки прогона, из аудита полноты 2026-07-06)
- ~~Callouts (уточнения) не создаются нигде tp1–tp5~~ — ЗАКРЫТО: v5 adextensions.add + привязка (см. CALLOUTS_NOT_CREATED выше).
- ~~Кап 100 минус-фраз `_kw_clean(minus,100)`~~ — НЕ актуально (Семён 2026-07-06): минуса льются из глобальных правил (сейчас 1 слово), капируется только редкий путь минус-файлов M3-пака. Не трогать.
- `updateListingAds(name-filter): UNAVAILABLE_FIELD` — у части фидов нет поля `name` (CSV=title): резолвить поле листинг-фильтра через fieldsForUseAs как `_resolve_feed_field`.

---

## Решённые ранее (кратко, для поиска по сигнатуре)

| Сигнатура | Root-cause | Решение | Статус |
|---|---|---|---|
| `DUPLICATE_SITELINK_DESCS` | одинаковые descriptions сайтлинков | `campaign.py::_norm_sitelinks` | ✅ |
| `IMAGE_NOT_FOUND` | битые/отсутствующие картинки M3 | фикс 2026-07-03 + live добивание | ✅ |
| `FEED_NOT_EXIST` | фид не привязан/удалён | `_first_url_feed` + пофидовый feed_map | ✅ |
| UAC 400 на sitelinks | длины/формат ссылок | `_norm_sitelinks` | ✅ |
| Ложный `UAC_PRODUCT_MODEL_FILTER_MISSING` | требовали модельный фильтр для ct-«Общее» | фильтр только для сегмента «Модели» | ✅ |
| Ложный `SITELINK_MISSING` на non-unified | пустой payload у non-unified кампаний | не флагать non-unified | ✅ |
| Пустые черновики копились | partial-создания | `_sweep_empty_drafts` | ✅ |
| Дубли джобов при двойном сабмите | TOCTOU в эндпоинте | атомарный дедуп в `_job_new` | ✅ |
| Дубли tp6/tp7 при доставке остатка | UAC переименовывает live-имена, RESUME-SKIP не матчил | доставка только реально отсутствующих позиций (сверка по кабинету) | ✅ |
| NULL href сайтлинков от LLM | Grid отбрасывал ссылки без href | backfill href + уникальный #якорь | ✅ |

---

## Ошибки последнего прогона (2026-07-06, 11:49–12:59 UTC, 5 аккаунтов) — разбор

Прогон: porg-7bqj56f4 (10/14), porg-ozge4ntu (18/21), porg-asfbs7qe (cancelled 1/21),
porg-psm5h7q6 (8/14), porg-lzjk6p5m (cancelled 8/211). Добивки df7f70e7605f (0/3) и
d342e768ae87 (0/1) провалились полностью; f64fc17a3ae5 (7 tp5) зависла в `claimed`.

### A. MUST_NOT_CONTAIN_DUPLICATED_ELEMENTS — tp7 товарка не создавалась (×7, 3 акк.)
- Симптом: `[create] HTTP 400 … feedFilters[0].conditions … MUST_NOT_CONTAIN_DUPLICATED_ELEMENTS`.
- Root-cause: регрессия фичи «минус-модели» (2026-07-06): `_minus_marks_uac_conditions` генерил ПО УСЛОВИЮ НА ЗНАЧЕНИЕ → 8 марок + 78 моделей = 86 однотипных условий; UAC счёл дублями (в т.ч. J7 у jac и jaecoo — значения сравниваются case-insensitive).
- Решение (2026-07-06): ОДНО условие на поле со всеми значениями массивом + case-insensitive дедуп значений (`_minus_values_ci`), `create_set_feeds.py::_minus_marks_uac_conditions`. Семантика подтверждена докой (yard.yandex.ru filtry-v-fidah: значения внутри условия = ИЛИ; до 22 условий через И).
- Статус: ✅ подтверждено прогонами 2026-07-06/07 — товарки создаются (psm 712236037/712236040 и далее).

### B. INVALID_COLLECTION_SIZE maxSize:30 — листинг tp1/tp5 без минус-фильтра (warnings)
- Симптом: `updateListingAds(feed-filter): INVALID_COLLECTION_SIZE {maxSize:30}` → фильтр отброшен ЦЕЛИКОМ → показы по нежелательным маркам (кампания создавалась, но без минусов).
- Root-cause: тот же — 86 условий > лимита 30 у Grid.
- Решение: то же схлопывание, `_minus_marks_grid_conditions` → ≤2 условия. ⚠️ Прежний комментарий-канон «одно условие на марку, иначе другая семантика» (ревью 03.07) — ОШИБКА, исправлен в campaign_spec_audit.py.
- Статус: ✅ подтверждено 2026-07-07 — минус-условия схлопнуты, ошибок лимита в прогонах нет.

### C. «tp5 не дозаполнена: без ShoppingAd» → кампания удалена (×8, 3 акк.)
- Root-cause: `_build_tp1_from_pack` не вернул shopping_ad_ids (гипотеза: пустой M3-пак/сбой) → гейт `create_set_feed_builders.py:546` удаляет partial-кампанию.
- Статус: ✅ root-cause = Grid replication lag; ретраи почанково + defer вместо потери; главный источник (startDate шаблона) устранён — см. запись J.

### D. AddUnifiedAdGroups: CAMPAIGN_NOT_FOUND (×4)
- Root-cause: Grid не видит кампанию (replication lag после создания токеном ИЛИ уже удалена гейтом C). `grid_create.py:180`.
- Статус: ✅ ретрай ×3 при полном отказе батча (без дублей) — в прогонах 07.07 ошибка не появлялась.

### E. NO_BRAND_SEGMENTS_AVAILABLE в добивках + деферред НЕ создавался (×4)
- Симптом: guardrail корректно отказал по куке, но обещанная «докрутка токеном» НЕ планировалась — `direct_deferred_creates` пуст, tp5-сегменты терялись МОЛЧА.
- Root-cause: условие `if st_token and …` — в добивочном контексте st_token пуст; плюс `_deferred_save` глотал исключения (`except: return None` без лога).
- Решение (2026-07-06): убрано требование st_token (resume-демон сам резолвит токен через `_token_for_login`), `_def_body.pop("via_cookie")` чтобы резюм не форсил куку опять, явные маркеры «⚠️ деферред НЕ создан (причина)» в error, лог в `_deferred_save`. `create_set_gallery.py` + `blueprint.py`.
- Статус: 🟡 ждёт рестарта + прогона.

### F. Джоба зависла в `claimed` навсегда (f64fc17a3ae5, добивка Щербаковой 7 tp5)
- Root-cause (ИСТИННЫЙ, найден живой репродукцией после первого рестарта): стартовый загрузчик
  истории (blueprint.py ~716) поднимает из БД ВСЕ незавершённые джобы в `_CREATE_JOBS` как
  записи-карточки БЕЗ очереди → гейт адопта `if jid in _CREATE_JOBS: return` молча пропускал
  постановку → джоба вечно `claimed`. Воспроизводилось при КАЖДОМ рестарте воркера с queued
  web-джобой в БД.
- Решение (2026-07-06): гейт адопта проверяет РЕАЛЬНОЕ участие (`jid in _CREATE_QUEUE` или
  `status=='running'`), стале-запись перезаписывается и ставится в очередь; watchdog
  `_worker_reclaim_stuck_claimed()` (раз в 60с: claimed >5 мин и не в работе → назад в queued);
  лог ошибок адопта вместо `except: pass`.
- Статус: ✅ подтверждено живьём 2026-07-06 18:5x: до фикса джоба дважды зависла в claimed
  (в т.ч. после первого рестарта), после фикса — ушла в running.
- НЕ помогло ранее: первый вариант watchdog'а с проверкой «нет в _CREATE_JOBS» — не срабатывал,
  т.к. загрузчик истории кладёт джобу в _CREATE_JOBS (та же слепая зона, что у гейта адопта).

### G. Приоритет добивки — дыры (задача Семёна «добивка сразу, не в конец»)
- Было: приоритет только у деферред-резюма in-memory; `_queue_recreate_repair_job` (пересоздание) и `_requeue_missing_positions_once` (доставка остатка, идёт через БД) вставали В КОНЕЦ.
- Решение (2026-07-06): `priority=True` в recreate; сквозной флаг `body['_priority']` через БД-путь (`_job_new_web`), клейм воркера `ORDER BY _priority DESC, created_at`, адопт — в начало in-memory очереди. `blueprint.py`, `create_set_repairing.py`.
- Статус: 🟡 ждёт рестарта.

### I. Ложный GENERIC_FALLBACK_GROUP → авто-ремонт УДАЛЯЛ полноценные tp5 (e2e 2026-07-06 вечер)
- Симптом: чистый прогон 14/14 без ошибок, но 5 живых tp5 (35 групп/3609 ключей каждая!) исчезли — их снёс новый fix_generic_fallback_group и переочередил токеном.
- Root-cause: аудит читает группы через `groups_for_edit` — **edit-view с лагом** (тот же корень, что ложный NO_KEYWORDS_LIVE из журнала): сразу после создания видна 1 (генерик) группа → детектор бьёт ложно.
- Решение (2026-07-06): жёсткий guard в фиксере — перед удалением проверять ЖИВЫЕ ключи через `_show_condition_kw_counts` (showConditions, не edit-view); ключи есть → НЕ пустышка, skip. `campaign_spec_audit.py` (блок 2b).
- Статус: ✅ подтверждено пересозданием 5 tp5 после фикса (см. STATE). Удалённые tp5 вернулись деферредами.
- НЕ помогло ранее: детект по одному источнику groups_for_edit — любой детектор «пустоты» обязан перепроверяться по showConditions.

### J. Grid finalize: `DateDefectIds.MUST_BE_GREATER_THAN_OR_EQUAL_TO_MIN` (startDate) — КАРУСЕЛЬ tp5
- Симптом: `grid_warn: Grid finalize… campaignUpdateItems[0].startDate` — места показа/автотаргет/ассеты tp5 НЕ выставлялись → верификатор ставил `WRONG_AUTOTARGET`+`GRID_FINALIZE_WARN` → авто-recreate СНОСИЛ свежесозданные tp5 → пересоздание по куке → NO_BRAND_SEGMENTS → деферред на ночь. Карусель «создали→снесли→ночью заново».
- Root-cause (ИСТИННЫЙ, 3-я попытка): в `grid_uc_template.json` ЗАХАРДКОЖЕН `startDate: 2026-06-21` (дата съёма HAR-шаблона). До 21.06 значение было ≥ сегодня и валидация проходила; с 22.06 — каждый full-finalize отклонялся. Первые две гипотезы (лаг реплики → пустой startDate в `_narrow_campaign_base` и `_unified_campaign_update_from_edit_row`) — реальные, но ВТОРИЧНЫЕ точки; главный путь — `finalize()` из шаблона.
- Решение (2026-07-06): `finalize()` всегда ставит `uc["startDate"] = сегодня по МСК`; в двух builder-ах — фолбэк на сегодня (у unified — только для DRAFT, прошлая дата запущенной кампании легитимна).
- Статус: ✅ подтверждено контролями 2026-07-07: финализация проходит, WRONG_AUTOTARGET-карусель остановлена (58d0/e1027: 0 ошибок).
- НЕ помогло ранее: чинить только read-builder'ы — шаблонная константа оставалась главным источником. Урок: HAR-шаблоны с датами = бомба замедленного действия; даты выставлять в рантайме.

### K. INTERRUPTED_JOB_POSITIONS_LOST — позиции теряются при рестарте между delete и create_job (2026-07-08)
- Симптом: после рестарта direct.service между `delete_uac`/`delete_search_draft` и `create_job` в `queue_recreate_repair_job` — удалённые tp5/tp7 не попадали в пересоздание. Примеры: tp5×10+tp7×4 и tp7×2 у двух аккаунтов.
- Где: `blueprint.py:_jobs_db_recover` → `_bg_sweep` (reconciler не вызывался для interrupted-джоб).
- Root-cause: `_requeue_missing_positions_once` вызывался ТОЛЬКО в `_run_delayed_content_repair` (строка 1921). При рестарте interrupted-джобы проходили через `_bg_sweep` (только `_sweep_empty_drafts`), reconciler никогда не вызывался.
- Решение (2026-07-08): `blueprint.py:_jobs_db_recover` — в `_bg_sweep` добавлен вызов `_requeue_missing_positions_once` для каждой прерванной джобы (строки 757-797). После сноса пустышек (sweep) + 5с пауза → reconciler сверяет план vs. живой кабинет и ставит доставку только реально пропавших позиций. Три гейта внутри reconciler: (1) `_requeue_of` — без внучек, (2) `auto_requeue_missing` — без дублей на повторных рестартах, (3) `_job_db_active_by_login` — не конкурирует с активной джобой.
- Статус: 🟡 фикс задеплоен через Mutagen, ждёт живого прогона (рестарт сервиса НЕ выполнен, идёт живое восстановление).
- НЕ помогло ранее: —

### H. `updateListingAds(name-filter): UNAVAILABLE_FIELD` (warnings, низкий приоритет)
- Root-cause: name-фильтр листинга обращается к полю, недоступному у фида. Кампания создаётся.
- Статус: 📋 бэклог (не блокирует).

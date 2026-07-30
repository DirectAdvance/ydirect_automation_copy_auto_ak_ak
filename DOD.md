# DoD — Definition of Done (создание РК нейродиректолога)

> **Живой документ.** Правим по ходу. При создании набора — придерживаемся. После создания —
> проверяем готовые кампании по этому чек-листу, чтобы понять **что добивать**.
> Легенда статуса: ✅ готово (в коде/подтверждено) · 🟡 частично / нужно подтвердить на прогоне · ⬜ не готово.
> Ошибки создания и решения — параллельно в `ERRORS_JOURNAL.md`.
> **Ошибка/несоответствие, которое находит Семён при живой проверке кампании — фиксируется ЗДЕСЬ
> (обновляем DOD.md), а не только чинится в коде — документ должен оставаться источником правды для
> «готова ли кампания».**
>
> **Структура документа:** §1 — диагностика готового набора (`live_verification`); §2 — контент
> объявлений (общая база для всех tp: провайдеры, фолбэк, видео, тексты); §3 — по типам кампаний
> (tp1–tp11) — основной per-tp чек-лист; §4 — эксплуатация; §5 — DoD слепка; далее — процедура проверки.
> Актуально: `tp8/tp9/tp10` создаются как «Посевы» через Grid `GdPostCampaign`; `tp11` пока вне
> автоматизации.

---

## 1. Структура кампаний (проверяется по кабинету)

Главный автопроверяльщик — **`live_verification`** в результате джоба (`direct_automation_jobs.result.live_verification`).
Цель: `summary.errors = 0`. Ниже — что каждый код значит и как чинить.

### 1.a Ключевые DoD-критерии (быстрый обзор)

| # | Критерий (DoD) | Код при нарушении | Как проверить | Как добить | Статус |
|---|---|---|---|---|---|
| 1.1 | Набор создан без ошибок | `summary.errors=0` | `live_verification.summary.errors` | зависит от кода ниже | ⬜ |
| 1.2 | Автотаргет поиска = ТОЛЬКО `EXACT_V2_MARK` + `WITHOUT_BRAND` (не «все галочки») — **касается tp2, tp4, tp5** (tp1 — РСЯ, КАТЕГОРИИ не проверяем: автотаргет сетей намеренно широкий, но `isActive` обязателен — см. 1.2a; tp3 — Search/товарная галерея `ADV_GALLERY` (НЕ РСЯ), автотаргет как `search_tp2` — см. §3.3; tp6/tp7 — UAC, поддерживают `targeting_mode`: `keywords` / `audience` / `autotarget`-fallback) | `WRONG_AUTOTARGET` | Grid `groups_for_edit` → `relevanceMatch` | **АТОМАРНО при создании групп** (Grid `AddUnifiedAdGroups` profile=`search_tp2`): tp5 v3 (2026-07-08), token-путь **tp2/tp4** — 2026-07-09 (был хрупкий пост-патч `_grid_set_search_autotarget` по edit-view → упразднён); cookie-путь tp2/tp4 — `create_full`; **tp1 — 2026-07-27** (`d44236d1`, см. 1.2a). Пост-патч по `groups_for_edit` больше НЕ используется. ⚠️ **tp5: профиль `search_tp2` ставится ВСЕГДА**, независимо от планового autotarget-флага — в поисковой кампании Директа автотаргет выключить НЕЛЬЗЯ (доменный факт Семёна 2026-07-27, `create_set_tp1_builders.py:324-328`) | 🟡 |
| 1.2a | **`relevanceMatch.isActive` группы = `("_aon_" in имя_группы)`** — для tp1 РСЯ, tp2, tp4 (tp3 — по профилю `search_tp2`). ⚠️ **ИСКЛЮЧЕНИЕ tp5: `isActive=True` ВСЕГДА**, в т.ч. у групп планового `aoff` — это НОРМА, а не дефект (автотаргет на поиске не выключается; `_aon_` в имени tp5-группы корректен всегда, «чинить» его нельзя — `create_set_tp1_builders.py:87-95` не трогать). Дефект (tp1/tp2/tp4): кодер `aon` + `isActive=False` ИЛИ кодер `aoff` + `isActive=True`. **Корень и фикс (2026-07-27, `d44236d1`+`00a6745f`):** tp1-группы создавались v501 `adgroups.add`, который `relevanceMatch` не умеет → побеждал дефолт Яндекса; пост-фактум Grid `UpdateUnifiedAdGroups` («Фаза 1.5») оказался **доказанным no-op** — кампаниям `713089308` и `713089104` послали ПРОТИВОПОЛОЖНЫЕ `isActive`, живая картина вышла идентичной (84 ON / 29 SUSPENDED в обеих). Фаза 1.5 УДАЛЕНА; tp1-группы (включая «все фиды» Фазы 4a) создаются атомарным Grid `AddUnifiedAdGroups` с `relevanceMatch.isActive` при создании — как tp2/tp4/tp5. Ключи tp1 переведены на Grid `AddKeywords` тем же клиентом: смешанный транспорт (Grid-группы + v5 `keywords.add`) даёт ключи-фантомы (`DMP_TP2_KEYWORDS_LOST_MIXED_TRANSPORT`). | `WRONG_AUTOTARGET` (**авто-детектор ЕСТЬ** с 2026-07-27: `grid_read` считает `wrong_autotarget_rsya_groups`, `grid_content_verifier.py:115-124` поднимает error по tp1) | Grid `GroupsForEdit` → `relevanceMatch.isActive` (`grid_read.py:335`) | корень закрыт при создании; in-place ремонт — `keywords_repair` (`repair_planner.py:162`) | ✅ (было 600 дефектных групп из 1325 и 14 РК из 24 → стало **0 из 1325 групп / 0 из 24 кампаний** на контрольных прогонах `fbb63cc8f962`, `3f56db987ab9`, `96f76846fc68`; боевой прогон 4 аккаунтов 2026-07-28 — `wrong_autotarget_groups=0`, 0 дефектных из 12/10/10/10 РК) |
| 1.3 | Все поисковые группы С ключами | `NO_KEYWORDS_LIVE` | Grid: `keyword_count` группы | keyword_repair (докрутка); корень — Phase 2 при создании | ⬜ |
| 1.3b | **Позиция tp6/tp7 с «Автотаргетинг» в имени = ПОЛНЫЙ автотаргет в кабинете.** Блок «Аудитория» обязан быть «Подобрать оптимальную», а НЕ «Настроить вручную». Проверяется по UAC-detail кампании: у автотаргет-позиции `keywords=null`/пусто, аудиторий нет **и `minus_keywords=[]`**. Любой ручной сигнал в payload (даже ОДНО глобальное минус-слово «отзывы» при пустых keywords/audiences) флипает блок в ручной режим — это свойство UAC, а не наша валидация. Правило зависит от РЕЖИМА позиции, а не от типа кампании: одинаково для мастера tp6 и товарки tp7. | `TP67_AUTOTARGET_RENDERS_MANUAL` (авто-детектора НЕТ — читать UAC-detail: `minus_keywords` непустой при пустых keywords/audiences у позиции с «Автотаргетинг» в имени) | `UacReadClient.campaign_detail(cid)` → `keywords` / `audiences` / `minus_keywords` | `create_set_master_product.py` — `minus_keywords=[] if targeting_mode == "autotarget"`; тесты `tests/test_tp6_autotarget_audience.py` | 🟠 (2026-07-30: найдено Семёном на `porg-uy3huxcn`; tp7 закрыт ещё 2026-07-10, tp6 чинится сейчас) |
| 1.3a | **Кампания с «КС»/«ключев» в ИМЕНИ не содержит групп без ключевых слов.** Решение Семёна 2026-07-30: если создана КС-кампания, а в её группе ключей нет — это ОШИБКА, независимо от того, что источник помечает группу `---autotargeting`. Имя кампании обещает работу по ключам; группа без ключей это обещание не выполняет. Две разные причины и два разных лечения: **(а) группа помечена в паке `---autotargeting`** (per-group файл `{slepok}__{gk}.txt` = ровно `---autotargeting`) → код прав, отдавая 0; чинится ПЕРЕНОСОМ группы в структуре слепка в кампанию **только с автотаргетингом**; **(б) per-group файл несёт фразы, а в кабинете пусто** → дефект создания/докрутки, чинить кодом. ⚠️ Не путать с чистой «… - Автотаргетинг» кампанией: там 0 ключей — НОРМА. ⚠️ Проверять именно per-group файл: чтение ct-уровня отдаёт легаси-агрегат (`tp1/ct0164` без группы = 626 фраз при 0 у группы) и создаёт ложное впечатление «ключи есть». | `NO_KEYWORDS_IN_KS_CAMPAIGN` (авто-детектора пока НЕТ — считается сверкой v5 `keywords.get` по каждой группе + имя кампании) | v5 `adgroups.get`+`keywords.get` (авторитетнее Grid `keyword_count`, тот врёт нулями) + per-group файл пака | (а) правка структуры слепка; (б) `keywords_repair` | 🟠 (2026-07-30: `porg-dmwfp3dk` — 196 групп, ВСЕ по причине (а); `nqavjicg` 202, `uy3huxcn` 8, `rgwzgo57` 3) |
| 1.4 | Глоб. минус-слова («отзывы») на уровне КАМПАНИИ (tp2/tp4/tp5) | `GLOBAL_MINUS_CAMPAIGN_MISSING` | Grid unified-payload → `minusKeywords`+`libraryMinusKeywordsIds` | `_enabled_minus_words` в deps (create) + **пост-аудит** `_audit_global_minus_campaign` → `fix_global_minus_campaign` (Grid `set_campaign_minus_keywords`, in-place, D6 2026-07-09) | ✅ |
| 1.5 | Каталог tp7 (ct0000) подхватывает страницы (не 0) | — (визуально в кабинете) | UI «Страницы каталога» / фид | `it_lff=[]` для ct0000 (сделано) | ✅ |
| 1.5a | **Фильтр каталога tp7 (`listings_feed_filters`) корректен и даёт непустой результат** — `operator = EQUALS`, поле `values` присутствует (массив), счётчик страниц каталога (`feedListingsPreview`) **> 0**. Ключевое: проверять именно СЧЁТЧИК, а не только факт отправки запроса — счётчик отличает рабочий фильтр от сломанного. ⚠️ `EQUALS_ANY` — оператор только GraphQL `feedListingsPreview` (только превью); в UAC REST (`PATCH /web-api/uac/campaign/...`) применять строго `EQUALS`. Инцидент 2026-07-27 (`porg-pl6iavd5`): фильтр показывал сырое `collectionId: mark_6` и 0 страниц — слали `operator: CONTAINS` без `values`. Эталон — HAR ручного сценария: `operator: EQUALS`, `values: ["mark_6"]`, `value: "[\"mark_6\"]"`. | — (визуально в кабинете / `feedListingsPreview`) | Карточка кампании → UI «Страницы каталога»; формируется в `create_set_feeds.py:1724` | ✅ исправлено коммитом `f3be587e` (operator=EQUALS + values) | ✅ |
| 1.6 | Быстрые ссылки = 8, без смысловых дублей; слова `рассрочка`/`рассрочк*` запрещены в заголовках, текстах, быстрых ссылках и уточнениях | — | объявление в кабинете / Grid readback | source-order + topic-дедуп; **R2-4 (в1) 2026-07-10: UAC-путь (tp6/tp7) теперь тоже гонит `_dedup_sitelinks(diversify_…)` ВСЕГДА** (был под гейтом `<8` → кредит-дубль «Платеж от 9 000»+«Автокредит от 9 000» проскакивал). Опц. reuse ОДНОГО набора на аккаунт — флаг `DIRECT_SITELINK_REUSE_ACCOUNT` (дефолт OFF). **2026-07-27:** backup-филлер `Рассрочка без переплат` запрещён, заменён на нейтральную гарантию; text-shaping дополнительно вычищает `рассрочк*` перед отправкой. | 🟡 |
| 1.7 | Видео на видео-марках (BAIC/Belgee/Haval/Москвич) | `VIDEO_MISSING` | докрутка read-back `hasVideo` | brand-fallback + докрутка (сделано, 155/16 верно) | ✅ |
| 1.8 | adPrice на фидовых группах | `NO_ADPRICE_LIVE` | Grid | adprice_repair (докрутка) | 🟡 |
| 1.9 | Нет `adGroupId not defined` (listing) | — (лог воркера) | `journalctl -u direct-create-worker.service` | shoppingAdId→lid (сделано) | ✅ |
| 1.10 | **URL объявления → ПРАВИЛЬНАЯ модель группы** (не чужая модель марки, не `/quiz`, не неточная формульная). Ссылка группы «Марки» должна вести на страницу марки (`/auto/haval`), ссылка группы «Модели» (напр. ct0042 «Changan UNI-T») должна вести на url ЭТОЙ модели из фида (`/auto/changan/uni-t/i/suv-5d`), НЕ на первый оффер бренда (cs55) и НЕ на формульную без хвоста. Товарный сниппет (модель/город) — следствие правильности url. | `MODEL_URL_BRAND_FALLBACK_WRONG_MODEL` / `MODEL_GROUP_HREF_QUIZ` (авто-детектора пока НЕТ — визуально в кабинете / сверка href vs mark+folder группы) | Ссылка объявления в кабинете vs `mark_id`+`folder_id` оффера фида | **Root-cause:** `_grid_feed_offer_urls` (FeedOffersPreview = sample, не все офферы) → модель вне выборки не находит точный ключ → brand-fallback на чужую модель. **Вариант A (задеплоен 2026-07-13):** `no_brand_fallback` для сегмента «Модели» в `_feed_url_for_model` (`create_set_feeds.py:335`) → нет точного ключа → формульный `_model_page_href` (верная модель, БЕЗ хвоста `/i/suv-5d`). **Вариант B (raw XML, `_auto_feed_urls` доливка в `_account_offer_urls`, ЗАДЕПЛОЕН 2026-07-13):** точный url модели из ПОЛНОГО raw XML фида (`home/yandex.xml`) по ключу `mark_id`+`folder_id`, доливается в `_account_offer_urls` через `setdefault`. **2026-07-27:** `/quiz` запрещён для всех generated Direct объявлений и кнопок; sanitizer заменяет `/quiz` на корень сайта до link-check и перед `_combo_button`. | 🟡 (A+B задеплоены; `/quiz`-регрессии закрыты кодом; live readback после ремонта обязателен) |
| 1.11 | **БУ-сайты (`site_type='С пробегом'`) НЕ получают глобальные минус-фильтры фида по маркам/моделям**. Глобальные minus marks/models в feedFilter допустимы для новых авто, но для БУ-аккаунтов режут валидный used-car ассортимент. Позитивные фильтры конкретной марки/модели остаются. | `AUTO_USED_FEED_TP1_TP7_404_CONTENT_GUARDS` | Grid/UAC feed filters: для `С пробегом` нет `NOT_CONTAINS*` по global marks/models; бренд/модельные группы имеют only-positive фильтр | `create_set_tp1_builders._apply_global_feed_minus_for_site`; `GridClient.add_shopping_ads(apply_global_minus=False)`; `tp7` positive-only | 🟡 (код+тесты+деплой OK; live create/readback не проверено) |
| 1.12 | **Односегментная 404-посадка (`/auto`, `/catalog`) fallback'ится на корень домена**, а не остаётся 404. Таймаут/5xx/сетевая ошибка по-прежнему fail-open возвращают исходный URL. | — | `resolve_or_fallback_url('https://bucars-kuban.site/auto')` | `link_check._parent_path`: `/auto` → origin | ✅ (real smoke: `/auto` → root 200) |
| 1.13 | **Корневая ссылка — дефект ТОЛЬКО у групп сегментов «Марки»/«Модели».** Для сегмента **«Общее»** (не-брендовые `ct`: «Автокредит», «Автосалон», «Рассрочка», «Авито», «Дром», «Трейд-ин»…) href = **корень сайта — ЭТО ШТАТНО, а не дефект**. Механика: `_valid_pack_brand_name` отвергает не-марку → `real_brand=''` → `_pack_group_href` (`create_set_text_builders.py:56-58`) возвращает `site_href.rstrip('/')`; формульный deep-link по теме звать НЕЛЬЗЯ — он воскрешает `BUTTON_404_GENERIC_AVTO` (`/auto/avto`) на сотнях item'ов. Проверено `curl` (2026-07-28, `bucars-kuban.site`): корень `200`, `/catalog/avtokredit` и `/rassrochka` — **404**, страниц под эти темы на сайтах нет. Решение Семёна: оставить как есть, на раздел не переводить. | `ROOT_HREF` — только при `сегмент ∈ {Марки, Модели}` | детект обязан делить по СЕГМЕНТУ: суммарный `ROOT_HREF` без разбивки диагностически бесполезен (детекторы живут на LXC101 `/tmp/_detect_root_href.py`, `/tmp/_href2.py`, `/tmp/_detect_root_href_v2.py` — в репо их нет) | — (ложная тревога чинить не надо) | ✅ норма зафиксирована 2026-07-28 |

> **⚠️ `WRONG_AUTOTARGET` внутри джобы (`live_verification`) — НЕ приговор** (боевой прогон 2026-07-28).
> Grid между созданием групп и in-job проверкой отдаёт реплику с лагом: `porg-xjxpfxby` дал
> `errors=12`, `porg-rgwzgo57` — `errors=6`, а живой перезамер ТЕМ ЖЕ ридером
> (`GridReadClient.campaign_content_counts`) **после** отложенной добивки дал
> `wrong_autotarget_groups=0` на всех 4 аккаунтах; у `rgwzgo57` репейр отчитался «исполнено 0» —
> дефект снялся сам. **Мерить автотаргет надо ПОСЛЕ delayed repair**, а не по джобе.

### 1.c DoD «Структура слепков → Создание РК 1:1» (задача 7, согласовано 2026-07-15)

> Спека и load-bearing риски — `CREATION_PROTECTED_RULES.md`. Контракт = `create_set_plan.py:_set_plan_response`.

| # | Критерий | Как проверить | Статус |
|---|---|---|---|
| 7.1 | «Создание РК» = «Структура слепков» 1:1 по ВСЕМ обычным tp1–tp7; для пакета «Посевы» create-tab = `posevy.json` по tp8/tp9/tp10. Проверяется не только preview/plan: **фактически созданные кампании в аккаунте обязаны совпадать с деревом вкладки «Структура слепков» после применения защищённых тегов**. Missing/extra campaign, переименование, generic fallback вместо строки структуры или неполный `х3` = дефект. | live-сверка аккаунта: имена кампаний, количество кампаний, состав групп и protected-tags result должны совпасть с `_build_export_rows`/UI-деревом; для `х3` каждая tagged-строка обязана дать все 3 live-кампании (`КС`, `Автотаргетинг`, `КС + Автотаргетинг`); precreate guard `/api/create_set` отклоняет payload с `camp_key`, которого нет в UI-структуре | 🟡 (контракт `create_set_structure.structure_to_campaigns` — MATCH=True vs `_build_export_rows` на terehov/scherbakova/kuderko/pavlov tp1/tp2/tp4/tp5; **tp1/tp2/tp4/tp5 подключены**; `camp_names` нормализуются по tp+сегменту и схлопывают ordinal/year дубли с одинаковым `gc`, сохраняя все `gk`; **2026-07-26:** raw `camp_names` больше не расширяет создание: для tp1/tp2/tp4/tp5 хвосты `КС`/`Автотаргетинг`/`КС+Автотаргетинг` схлопываются в логический узел дерева и итоговый targeting; `scherbakova/Мультибренд` даёт tp2=2 и tp4=2, а не raw 6; старый `porg-4ealp4ry` показал дефект — verifier pass по созданным 22 ≠ структурный DoD pass; tp3 — builder-blocked; tp6/tp7 уже 1:1 + generic-дубли объединяются; **tp8/tp9/tp10 UI-parity done** 2026-07-22; 2026-07-25 добавлен precreate `slepok_structure_mismatch` guard до Direct-мутаций) |
| 7.1a | Запуск create принимает только свежий план от текущего выбранного слепка/типа сайта; серверные версии имён запрещены | строки `plan[]` содержат `_plan_agent/_plan_site_type`; `/api/create_set_async` возвращает 409 при mismatch или `_vNN`/`renamed`; `_set_plan_response` не отдаёт `_vNN`, а пишет warning о коллизиях | ✅ (2026-07-22: `porg-rgwzgo57` old job имел `body.agent=terehov`, хотя ожидали `scherbakova`; tp6/tp7 `terehov/Мультибренд` давали `_v01/_v02/_v03` из `_uniq()` из-за коллизий. Новый guard: stale plan и `_v01` payload отклоняются 409; план tp6/tp7 Терехова отдаёт `versioned_count=0` + warning про убранные `_vNN`) |
| 7.1b | Потоковый полный ИИ-прогон (`stream_content=true`) не имеет права наследовать старый DOM-план или `single_feed`; он пересчитывает полный свежий `PLAN` и отправляет все его строки. Product-only payload для full-run отклоняется как stale client. | UI: `createAndPublish()` → `acSelectAll(true)` + `forceFullPlan`; payload `/api/create_set_async` содержит весь `PLAN`; stale stream `item_types <= {"product"}` → 409 `stale_client_product_only_stream` | 🟡 (код 2026-07-27 на диске; live full-run не запускался после остановки worker по команде) |
| 7.2 | Кампании строятся по логическому дереву структуры для tp1–tp5; `raw camp_names` не могут размножать РК и не могут тащить группу в чужой сегмент | для tp1/tp2/tp4/tp5 `structure_to_campaigns` схлопывает raw variants в видимые узлы дерева; аудит `explicit_segment_mismatches_after=0`, `ordinal_year_dup_buckets_after=0` по active auto-слепкам | 🟡 (**tp1/tp2/tp4/tp5 done**: ветки `tp1_rsy`/`search_test`/`search_dynamic`/`search_gallery` → `structure_to_campaigns`, per-group `only_gks/only_cts` → билдеры `_build_tp1_from_pack`/`_build_text_from_pack`/`_create_tp5_single`/`_tp1_pack_groups`; dmp-split tp2 сохранён; 2026-07-26 `КС`+`Автотаргетинг` raw-варианты схлопываются по tree base с сохранением всех `gk/merged_gks/ct`; **tp3 — remaining**, builder-blocked) |
| 7.3 | tp3 — по фидовым кампаниям из структуры (не 1 РК «ТГ - Фид (товары)») | дерево tp3 == созданные РК | 🟡 (**camp_names done**: ветка `rsya_gallery` → `structure_to_campaigns`, base_name=camp_name; terehov С пробегом 13 РК / karavaev 3 / scherbakova 1 = 1:1. **При ОДНОМ фиде** (тест) fan-out даёт ровно 1 РК на camp_name → число РК = число camp_names-кампаний. Many-feeds: fan-out ×feeds на camp_name — для точного feed↔camp маппинга нужна доп-инфа в структуре, см. отчёт; fallback на старую 1-РК при пустых camp_names) |
| 7.4 | Тег «х3» (tp1) → 3 РК: КС / автотаргет / КС+автотаргет, **каждой полный бюджет**; триггер — ТЕГ из `campaign_tags`, не имя кампании и не `seg_modes` | tp1 с явным «х3» → 3 РК; tp1 с именем `КС + Автотаргетинг` без тега → 1 РК | 🟡 (`detect_protected_tags`: `х3` только из реестра `campaign_tags`; эвристика имени оставлена только для `все фиды`; х3→`X3_VARIANTS` 3 РК, каждой полный `rs["cpc_budget"]`; live не проверено) |
| 7.5 | Тег «все фиды» → все разрешённые фиды ГРУППАМИ в ОДНОЙ РК; **tp3/tp5/tp1-РСЯ** (tp7 — НЕ входит) | наличие групп-фидов в РК | 🟡 (**tp3 done**: `all_feeds` → `_create_tp3_campaign`/`_create_tp3_single` строят ОДНУ РК, ГРУППА на каждый разрешённый фид; при 1 фиде = 1 группа. **tp1-РСЯ/tp5 — deferred**: модель-групповые combined-кампании, «группа на фид» требует мульти-фид shopping в билдере; при ОДНОМ фиде (тест) — уже 1 РК/1 фид, флаг инертен, регрессии нет) |
| 7.6 | Только «х3»/«все фиды» управляют созданием; прочие теги — отображение | grep tag-чтения в create-пути | ✅ (в create-пути читаются ТОЛЬКО `X3_TAG`/`ALL_FEEDS_TAG` через `detect_protected_tags`; прочие метки не влияют) |
| 7.7 | Наборы минус слов: ОДИН общий shared-набор (2+ в структуре → слить в 1); минусы набора только tp2–tp5 | привязка `NegativeKeywordSharedSetIds` | ⬜ |
| 7.8 | Глобальные минусы (Глоб. правила, «отзывы») — ко ВСЕМ РК (все tp), кампания-уровень | `minusKeywords` кампании | 🟡 (сейчас tp2/4/5) |
| 7.9 | pack-минусы — отдельно (группы), не смешиваются с наборами/глобальными | `_collect_pack_minus` на группах | ✅ |
| 7.10 | Регион шаблонный `r0000` в структуре → реальный город на создании | `_resolve_region` по городу аккаунта | ✅ |
| 7.11 | Сохранён весь текущий функционал (черновики State=OFF, две стратегии cpc+cpa, докрутка 152, feed_alert, profile-гейтинг, процедурные добавки) | регресс-проверка после правок | ⬜ |
| 7.13 | **tp2–tp5 — БЕЗ картинок и видео** (текстовые/поисковые кампании). Картинки/видео — только tp1 РСЯ (видео-марки, DoD 1.7) и tp6/tp7 (UAC/товарка). Для tp2/tp3/tp4/tp5 объявления текстовые, image/video НЕ добавляются. Следствие: `VIDEO_NO_POOL`/`CT_SLEPOK_IMAGES_EMPTY` на tp2-tp5 — НЕ дефект (там их и не должно быть) | read-back: tp2-5 объявления без image/video | ⬜ (подтвердить в коде + подавить ложный аудит-флаг на tp2-5) |
| 7.12 | **КОНТЕНТ по тон-оф-войсу выбранного слепка** — заголовки/тексты/уточнения/быстрые ссылки генерятся по seeded-стилю ЭТОГО слепка (`create_set_slepok_content.apply_slepok_campaign_content` / `seed_slepok_content` / `ai_content` seed_slepok, `content_source`=слепок). Генерация делает до 3 полных вариантов (`DIRECT_CONTENT_TONE_VARIANTS`, cap 1..3) и выбирает максимальный `voice_score` среди complete-кандидатов; `fast_mode`/`skip_tone_voice_variants` остаются single-run. Контент созданных РК должен соответствовать стилю слепка (напр. «жёсткая складская выгода 53-57%, автокредит, взнос 0₽»), НЕ дефолт и НЕ чужой слепок. **Live stream-generation не имеет права копировать `direct_slepok_content` как финальный контент; если LLM/repair не дали полный комплект, orchestrator продолжает в builder fallback/локальные шаблоны, а не ставит весь item failed до создания. Если builder тоже не находит валидный контент/пак — item падает с конкретным content-gap.** | `m3_debug.tone_voice_selection`, live tone-check, сверка текстов кампании vs seed слепка; контрактный тест `test_live_generation_blocks_template_fallback_when_llm_is_empty`; batch-regression `porg-4ealp4ry`: stream-fail не должен давать `40/40 failed` до builder'ов | 🟡 (код есть 2026-07-24; старые РК не переписывает; live повторный create/readback не проверено) |
| 7.14 | **ИМЯ создаваемой кампании = СОГЛАСОВАННОЕ имя из «Структуры слепков»** (`item.camp_names[0]` / `item.t`) + регион. В имени НЕ должно быть метаданных фида/аккаунта: имени фида, даты фида (`… от DD.MM.YY`), домена аккаунта, listing/collection фида (`yandex-catalog-model-design-custom-name`), URL фида (`yandex.xml`, `yandex-used-auto`). Семён 2026-07-16 (porg-asfbs7qe): часть camp_names в структуре захаршены с метаданными фида (33 шт: terehov 30 / scherbakova 2 / avto_sk 1) → чистятся через унификацию имён (Google-таблица старое→новое). ⚠️ Приклейка `— {feed_name}` в `create_set_feed_builders.py:518` / `create_set_master_product.py:622` — КОРРЕКТНА (это отдельный legit-суффикс фида), НЕ трогать; чинить ТОЛЬКО базовый `name` (camp_name из структуры) | grep camp_names на `от \d\d\.\d\d`/`\.ru`/`yandex[.-]`/`catalog-model` → 0; имя созданной РК == согласованное имя структуры + регион | ⬜ (чистится унификацией #56) |

### 1.b ПОЛНЫЙ каталог кодов (100% — все 90 уникальных `issue.code`)

> Сведено с нуля по коду (grep `"code":"…"` по всем `*.py`, 2026-07-09). Разбито по модулю-источнику.
> **Severity как в коде** (verifier'ы: `error`/`warn`/`low`; spec-audit: `high`/`medium`/`low`/`info`).
> **Fix**: ✅ = есть реальный авто-фиксер рядом в коде (назван) · 🟡 = чинится пересозданием/докруткой,
> не in-place · ⬜ = не авто-фиксабельно (структурная/внешняя причина, `fixable:False` или нет фиксера).
> Статус НЕ выдуман: ✅ ставлю только там, где вижу fix-функцию.

**1.b.1 — Pre-check body / item / result (`verifier.py`, `local_result_verifier.py`, `precreate.py`)**
Проверяется на самом теле джоба и на локальном результате создания (ДО/вместо чтения кабинета).

| Код | Sev | Что значит | Где (файл:стр) | Fix |
|---|---|---|---|---|
| `BODY_SLEPOK_MISSING` | error | в body нет выбранного слепка/агента | verifier.py:153 | ⬜ (гейт на входе) |
| `BODY_COUNTER_MISSING` | warn | нет счётчика Метрики в body | verifier.py:163 | ⬜ |
| `BODY_GOAL_MISSING` | warn | нет цели в body | verifier.py:169 | ⬜ |
| `BODY_ITEMS_EMPTY` | error | в body нет items | verifier.py:176 | ⬜ |
| `BODY_LAUNCH_IGNORED_DRAFT_ONLY` | warn | запросили launch, но сервис всегда черновик | verifier.py:182 | ✅ (инвариант: launch игнорится) |
| `ITEM_NAME_EMPTY` | error | у item нет имени | verifier.py:222 | ⬜ |
| `ITEM_TYPE_EMPTY` | warn | у item нет типа | verifier.py:225 | ⬜ |
| `ITEM_NAME_HAS_NULL_TOKEN` | error | в имени item «None/null/undefined» | verifier.py:227 | ⬜ (баг генерации имени) |
| `ITEM_NOT_PROCESSED` | error | item не дошёл до создания | verifier.py:229 | 🟡 (повторный запуск) |
| `CONTENT_TITLES_MISSING_LOCAL` | warn | у item нет заголовков в контенте | verifier.py:234 | 🟡 (докрутка/regen) |
| `CONTENT_TEXTS_MISSING_LOCAL` | warn | нет текстов | verifier.py:236 | 🟡 |
| `CONTENT_TITLES_LOW` | warn | заголовков меньше нормы (7/5) | verifier.py:238 | 🟡 |
| `CONTENT_TEXTS_LOW` | warn | текстов меньше нормы (3) | verifier.py:240 | 🟡 |
| `ITEM_CONTENT_MISSING_LOCAL` | warn | у item вообще нет контента | verifier.py:242 | 🟡 |
| `ITEM_FEED_MISSING_LOCAL` | warn | фидовый item без фида | verifier.py:244 | ⬜ (нет фида в аккаунте) |
| `CONTENT_SITELINKS_LOW` | warn | быстрых ссылок <8 | verifier.py:246 | 🟡 (fix_sitelinks_missing на live) |
| `RESULT_NAME_EMPTY` | error | у результата создания нет имени | verifier.py:251 | ⬜ |
| `CODER_PREFIX_MISSING` | error | имя без tp-префикса кодера (`_TP_RE`) | verifier.py:254 | ⬜ (баг сборки имени) |
| `CODER_SHAPE_SUSPICIOUS` | warn | имя не матчит форму кодера (`_CODER_RE`) | verifier.py:256 | ⬜ |
| `NAME_HAS_NULL_TOKEN` | error | «None/null/undefined» в имени результата | verifier.py:258, local_result_verifier.py:39 | ⬜ |
| `CREATED_WITHOUT_ID` | error | создано, но без campaign_id | verifier.py:260, live_verifier.py:65 | 🟡 (пересоздание) |
| `RESULT_FAILED` | error | результат item = ошибка создания | verifier.py:262 | 🟡 (repair `failed_campaign`) |
| `BUILD_ERROR` | error | build группы/объявлений упал/skipped | verifier.py:269, local_result_verifier.py:19 | 🟡 (repair/recreate) |
| `NO_ADGROUPS_REPORTED` | error | build вернул 0 групп | verifier.py:272, local_result_verifier.py:22 | 🟡 |
| `NO_ADS_REPORTED` | error | build вернул 0 объявлений (tp5 без TextAd — не дефект, если ShoppingAd>0) | verifier.py:279, local_result_verifier.py:26 | 🟡 |
| `SEARCH_NOT_FINALIZED` | warn | поисковая финализация не подтверждена | verifier.py:281, local_result_verifier.py:30 | 🟡 (grid-finalize докрутка) |
| `SHOPPING_NOT_FINALIZED` | warn | товарная финализация с ошибкой | verifier.py:284, local_result_verifier.py:33 | 🟡 |
| `SHOPPING_COUNT_BAD` | warn | число ShoppingAd не сошлось | local_result_verifier.py:28 | 🟡 |
| `GRID_FINALIZE_WARN` | warn | Grid-finalize вернул предупреждение | verifier.py:287, local_result_verifier.py:36 | 🟡 |
| `PROMO_NOT_ATTACHED` | warn | промо не прикрепилось | verifier.py:292 | 🟡 (repair `promo_attach_or_create`) |
| `CALLOUTS_NOT_CONFIRMED` | warn | уточнения не подтверждены | verifier.py:296 | 🟡 (repair `callouts_verify`) |
| `PRECREATE_SLEPOK_MISSING` | warn | precreate промо без слепка | precreate.py:84 | ⬜ |
| `PRECREATE_CONTENT_PARTIAL` | warn | precreate сгенерил не весь контент | precreate.py:122 | 🟡 |

**1.b.2 — Состояние кампании в кабинете (`campaign_state_verifier.py`, `live_verifier.py`, `verification_service.py`)**

| Код | Sev | Что значит | Где (файл:стр) | Fix |
|---|---|---|---|---|
| `CAMPAIGN_NAME_EMPTY` | error | у кампании в кабинете пустое имя | campaign_state_verifier.py:24 | ⬜ |
| `NAME_MISMATCH` | warn | имя в кабинете ≠ ожидаемому | campaign_state_verifier.py:28 | 🟡 (rename-докрутка) |
| `CAMPAIGN_ARCHIVED` | error | кампания в архиве (не должна) | campaign_state_verifier.py:34 | ⬜ (ручное разархивирование) |
| `CREATED_WITHOUT_ID` | error | результат без id перед live-чтением | live_verifier.py:65 | 🟡 |
| `GRID_CHECK_SKIPPED` | warn | Grid-проверка пропущена | live_verifier.py:71 | — (диагностика) |
| `UAC_NOT_FOUND_IN_GRID` | error | UAC-кампания не найдена в Grid | live_verifier.py:75 | 🟡 |
| `UAC_DETAIL_SKIPPED` | warn | детали UAC не прочитаны | live_verifier.py:80 | — |
| `CAMPAIGN_NOT_FOUND_IN_GRID` | error | кампания не найдена в Grid (лаг/удалена) | live_verifier.py:92,102 | 🟡 (ретрай на лаге) |
| `LIVE_CHECK_SKIPPED` | warn | live-проверка пропущена (нет куки/доступа) | live_verifier.py:105 | — |
| `LIVE_SOURCE_ERRORS` | warn | источник live-данных вернул ошибки | verification_service.py:105 | — (диагностика) |

**1.b.3 — Grid live-контент tp1–tp5 (`grid_content_verifier.py`)** — чтение реальной кампании через Grid.

| Код | Sev | Что значит | Где (файл:стр) | Fix |
|---|---|---|---|---|
| `NO_ADGROUPS_LIVE` | error | в кабинете 0 групп | grid_content_verifier.py:61 | 🟡 (пересоздание) |
| `NO_ADS_LIVE` | error | 0 объявлений | grid_content_verifier.py:65 | ✅ create-cookie underfilled guard: `ads=0`/`rep.errors` удаляют partial DRAFT и не дают `ok:true` |
| `ADGROUP_NAME_MISSING` | error | у группы нет имени | grid_content_verifier.py:70 | 🟡 |
| `NO_KEYWORDS_LIVE` | error | поисковая группа без ключей | grid_content_verifier.py:84,97 | ✅ для новых tp2/tp4: `only_gks` прокинут в packer, search-группы без ключей пропускаются, ключи дедупятся между группами; repair остаётся для старых РК |
| `WRONG_AUTOTARGET` | error | **две разные проверки под одним кодом:** tp2/4/5 — профиль ≠ `EXACT_V2_MARK`+`WITHOUT_BRAND` (`counts.wrong_autotarget_groups`); **tp1 РСЯ** — `relevanceMatch.isActive` ≠ флагу кодера `_aon_`/`_aoff_` (`counts.wrong_autotarget_rsya_groups`, с 2026-07-27). tp5 `aoff` с `isActive=True` — НЕ дефект (§1.2a) | grid_content_verifier.py:109 (tp2/4/5) и :120 (tp1) | 🟡 (keyword_repair; корень закрыт атомарным созданием. ⚠️ in-job срабатывание может быть лагом реплики Grid — перемерять ПОСЛЕ delayed repair) |
| `DYNAMIC_PLACES_ON` | warn | динамич. места включены там, где нельзя (tp2) | grid_content_verifier.py:104 | 🟡 |
| `MINUS_PLACES_MISSING` | warn | нет минус-площадок (tp1 РСЯ) | grid_content_verifier.py:122 | 🟡 |
| `CALLOUTS_MISSING_LIVE` | warn | **tp1–tp5: у кампании не привязаны уточнения** (`inheritableCallouts.assetValue` пуст). Гейт: только tp1–tp5 — **tp6/tp7 (МК/Товарка, UAC) уточнения не поддерживают** и НЕ флагаются | grid_content_verifier.py:148 | ⬜ (report-only) |
| `SITELINK_SET_MISSING_LIVE` | warn | **tp1–tp5: у кампании не привязан НАБОР быстрых ссылок** (`inheritableSitelinkSet.assetValue` пуст). Уровень КАМПАНИИ — дополняет ad-level `SITELINK_MISSING`/`UAC_SITELINKS_MISSING` | grid_content_verifier.py:157 | ⬜ (report-only) |
| `PROMO_MISSING` | warn | **tp1–tp5: промо не прикреплено. ДВУХСТУПЕНЧАТО** — ступень 1: в БИБЛИОТЕКЕ аккаунта вообще есть промо-акции (`expected["account_has_promo"]` ← проброшенный `account_has_promo_library`, фолбэк — прокси); ступень 2: промо доехало в кампанию. Аккаунт без промо → **не флагается вообще** | grid_content_verifier.py:174 | ⬜ (report-only) |
| `NO_IMAGES_LIVE` | error | у tp1 ResponsiveAd нет картинок. При создании tp1 группы без image-пула (`Manual/<ct>` → слепок `image_slepki` → explicit assets) должны **пропускаться**, а не создаваться голыми; если объявление уже создано без картинок — добивка `fix_image_missing` | grid_content_verifier.py:185 | ✅ (create-skip + fix_image_missing) |
| `NO_ADPRICE_LIVE` | warn | нет adPrice (bannerPrice) ни на одном объявлении tp1. **Исключение: товарка-only** (`adaptive_images_read=True` и `adaptive_total==0`, напр. смарт-баннер) — `bannerPrice` есть только у адаптивных текстовых, у ShoppingAd/ListingAd цена идёт из фида → код НЕ выдаётся | grid_content_verifier.py:208 | 🟡 (adprice_repair) |
| `EMPTY_DEFAULT_TEXT_LIVE` | warn | пустой `set_default_text` у ShoppingAd | grid_content_verifier.py:219 | 🟡 |
| `ALT_TEXTS_ENABLED_LIVE` | error | tp1–tp5: персонализация (адаптивные тексты) ВКЛ (#3, должна OFF) | grid_content_verifier.py (кампания) | ✅ `campaign_invariant_repair` (grid_finalize.set_campaign_invariants) |
| `EXTENDED_GEO_ENABLED_LIVE` | error | tp1–tp5: расш.гео ВКЛ (#5, должен OFF) | grid_content_verifier.py (кампания) | ✅ `campaign_invariant_repair` |
| `RECOMMENDATIONS_ENABLED_LIVE` | error | tp1–tp5: «Директ помогает» ВКЛ (#6, должен OFF) | grid_content_verifier.py (кампания) | ✅ `campaign_invariant_repair` |
| `PRICE_RECOMMENDATIONS_ENABLED_LIVE` | error | tp1–tp5: ценовые рекомендации ВКЛ (должны OFF) | grid_content_verifier.py (кампания) | ✅ `campaign_invariant_repair` |
| `COMPANY_INFO_ENABLED_LIVE` | error | tp1–tp5: Карты/список организаций (enableCompanyInfo) ВКЛ | grid_content_verifier.py (кампания) | ✅ `campaign_invariant_repair` |
| `MAPS_ENABLED_LIVE` | error | tp1–tp5: площадка «Карты» (yandexMaps) ВКЛ | grid_content_verifier.py (кампания) | ✅ `campaign_invariant_repair` |
| `ORG_LIST_ENABLED_LIVE` | error | tp1–tp5: список организаций (serpGeoWizard) ВКЛ | grid_content_verifier.py (кампания) | ✅ `campaign_invariant_repair` |
| `STRATEGY_MISMATCH_LIVE` | warn | tp1–tp5: payForConversion ≠ pay-mode (cpc→False/cpa→True) | grid_content_verifier.py (кампания) | ⬜ (report-only; recreate, не in-place) |
| `BUILD_LIVE_MISSING` | error | **сверка build⇄кабинет:** билдер отчитался о N>0 (группы/объявления/ключи), а в кабинете **0**. Severity error в ОБЕИХ фазах | `_verify_build_vs_live` | 🟡 (rebuild/keywords_repair — совпадает с `NO_*_LIVE`, дедуп в планировщике) |
| `BUILD_LIVE_UNDERCOUNT` | warn (in-job) / error (delayed) | **сверка build⇄кабинет:** `0 < live < build`. In-job = warn, delayed = error + repair. Для новых search tp2/tp4 `build.groups` должен считаться по read-back фактических `adgroups`; попытки остаются в `groups_built` | `_verify_build_vs_live` | ✅ для новых tp2/tp4 (`porg-pl6iavd5`/`kryuchkova` job `85a34373fc9a`: `groups=16`, `groups_built=18`, live pass) |
| `GEO_MISSING_LIVE` | error | tp1–tp5: у групп пуст `regionsInfo.regionIds` (нет регионов показа) | grid_content_verifier.py (гео) | ⬜ (детект) |
| `GEO_INCONSISTENT_LIVE` | warn | tp1–tp5: у групп ОДНОЙ кампании разные наборы регионов | grid_content_verifier.py (гео) | ⬜ (report-only) |
| `UTM_MISSING_LIVE` | error | tp1–tp5: группа без UTM-метки (`trackingParams` пуст) — **DoD #2** (у tp1–tp5 метка живёт на ГРУППЕ, не на кампании) | grid_content_verifier.py (UTM) | ⬜ (детект; закрывает пробел P1 «UTM-на-группах») |
| `METRIKA_COUNTER_MISSING_LIVE` | error | tp1–tp5: не привязан счётчик Метрики (`metrikaCounters` пуст) — инвариант #1 | `_verify_campaign_spec` | ⬜ (детект) |
| `CAMPAIGN_GOAL_MISSING_LIVE` | error | tp1–tp5: нет цели НИ в `meaningfulGoals`, НИ в `strategy.goalId` (флагается только когда пусты ОБА) — инвариант #1 | `_verify_campaign_spec` | ⬜ (детект) |
| `WEEKLY_BUDGET_MISSING_LIVE` | error | tp1–tp5: недельный бюджет стратегии ≤0 (`strategy.budget.sum`) | `_verify_campaign_spec` | ⬜ (детект) |
| `BUDGET_PERIOD_UNEXPECTED_LIVE` | warn | tp1–tp5: период бюджета ≠ `WEEK` | `_verify_campaign_spec` | ⬜ (report-only) |
| `CAMPAIGN_NOT_DRAFT_LIVE` | error | tp1–tp5: `status.primaryStatus` не DRAFT — сервис публикует ТОЛЬКО черновики (DoD §3.0) | `_verify_campaign_spec` | ⬜ (детект) |
| `TIME_TARGET_MISSING_LIVE` | error | tp1–tp5: не задано расписание показов (`timeTarget.timeBoard` пуст) | `_verify_campaign_spec` | ⬜ (детект) |

> **ЭТАП 1 усиления проверок, 2026-07-18 — 0 новых обращений к API (замерено).** Все коды выше
> от `BUILD_LIVE_MISSING` до `TIME_TARGET_MISSING_LIVE` читаются из ДВУХ уже выполнявшихся запросов,
> результат которых частично выбрасывался:
> * `CampaignsEditData` (`grid_finalize.read_campaign_invariants`) — счётчик/цель/UTM-параметры/
>   бюджет/статус/расписание уже во фрагменте `UnifiedCampaign`; флаг чтения `campaign_spec_read`.
> * `GroupsForEditLite` (`grid_finalize.groups_for_edit` → `grid_read._enrich_group_targeting`) —
>   гео (`regionsInfo.regionIds`) и UTM групп (`trackingParams`); флаги `geo_read`/`group_utm_read`.
> **Охват per-group детекта расширен с tp2/4/5 на tp1/tp3** — кампании и так читались тем же
> батч-запросом, их группы просто выбрасывались. `_show_condition_kw_counts` (запрос НА КАМПАНИЮ)
> по-прежнему зовётся ТОЛЬКО для tp2/4/5, иначе охват стоил бы +1 запрос на каждую РСЯ-кампанию.
> Замер (тот же набор кампаний, baseline `git archive HEAD` vs текущий): **12 Grid-операций до = 12 после**,
> побайтово тот же per-op разрез (`KwCount` 2 = только поисковые).
> **⚠️ ЛИМИТ 10000** (`_GFE_LIMIT`, `_SC_LIMIT`): Grid отдаёт максимум 10000 строк на секцию,
> offset-пагинация за предел НЕ работает → у крупного набора `keyword_count` недосчитан. Ответ ровно
> на лимит помечается `keywords_truncated=True`, и по КЛЮЧЕВОМУ измерению такая кампания **не судится
> вовсе** (ни `NO_KEYWORDS_LIVE`, ни `BUILD_LIVE_*`) — иначе недосчёт дал бы гарантированный ложный
> «live < build» и ложный ремонт.
> **⚠️ tp1/tp3 zero-kw:** группа в режиме «полный автотаргет» создаётся БЕЗ реальных ключей —
> таргетинг живёт в `relevanceMatch`, а не в `GdKeyword` (псевдоключ `---autotargeting` больше
> НЕ шлётся вовсе, `create_set_tp1_builders.py:366-371`). Поэтому для tp1/tp3 активный
> `relevanceMatch` ГАСИТ zero-kw (по дизайну).
> **⚠️ Устарело:** «`WRONG_AUTOTARGET` для tp1 не выдаётся вовсе» — с 2026-07-27 выдаётся:
> по КАТЕГОРИЯМ tp1 действительно не судится (у РСЯ автотаргет намеренно широкий), но по
> `isActive` vs `_aon_`/`_aoff_` — судится (`counts.wrong_autotarget_rsya_groups` →
> `grid_content_verifier.py:115-124`, severity `error`). Семантика tp2/4/5 не изменена.

> **Кампанийные АССЕТЫ (`CALLOUTS_MISSING_LIVE` / `SITELINK_SET_MISSING_LIVE` / `PROMO_MISSING`),
> 2026-07-18 — 0 новых обращений к API.** Источник — тот же ответ `CampaignsEditData`, которым уже
> читаются инвариант-галочки: `grid_finalize.read_campaign_invariants` →
> `grid_read._enrich_campaign_invariants`. Поля `inheritableCallouts{assetValue}` /
> `inheritableSitelinkSet{assetValue}` / `promoExtension{id}` уже входят во фрагмент
> `UnifiedCampaign` (`grid_campaigns_edit_data.graphql`) — раньше просто выбрасывались.
> Нормализация — как в `_unified_campaign_update_from_edit_row:601-603` (в сыром rowset эти поля
> лежат под `assetValue`/`promoExtension.id`, а НЕ плоскими write-ключами).
> **Tri-state (fail-safe, тот же контракт, что у галочек):** ключ отсутствует в ответе → `None` →
> верификатор МОЛЧИТ (ложный детект дороже пропуска). Флаг чтения — `campaign_assets_read`.
> **Все три report-only (без repair-кандидата):** `campaign_invariant_repair`
> (`set_campaign_invariants`) эти поля НЕ переставляет; ремонт не выдумываем.
> **Ступень 1 промо = БИБЛИОТЕКА АККАУНТА** (`account_has_promo_library`, tri-state). Признак
> берётся из v5 `promotions.get`, который **уже выполняется в штатном потоке создания**
> (`create_set_promo.attach_or_create_promo:34`, `precreate.py:262`) → **0 новых запросов, 0 баллов**.
> Проброс: `attach_or_create_promo` → `create_set_orchestrator:1041` → `_create_set_live_verification`
> → `verification_service.verify_create_set_live` → `live_verifier.verify_live_create_set`.
> `False` (библиотека пуста) → `PROMO_MISSING` не выдаётся вообще (требование Семёна).
> `True` → флагаются ВСЕ кампании без промо, включая полный провал доставки **0/N**.
> `None` (признак не проброшен — вызов не из потока создания) → фолбэк на прокси
> `live_verifier._account_has_promo` (промо по кампаниям набора; 0/N он не ловит — на то и фолбэк).

**1.b.4 — UAC tp6/tp7 (`uac_verifier.py`)** — все толкают repair `stop_or_recreate_campaign`/`_repair(nm,cid)` = **пересоздание** (не in-place), поэтому Fix = 🟡.

| Код | Sev | Что значит | Где (файл:стр) |
|---|---|---|---|
| `UAC_NOT_DRAFT` | error | UAC-кампания не в статусе черновик | uac_verifier.py:76 |
| `UAC_PRICING_MISMATCH` | error | pricing ≠ PER_CLICK (cpc) / PER_CONVERSION (cpa) | uac_verifier.py:79,83 |
| `UAC_BUDGET_MISSING` | error | недельный бюджет ≤0 | uac_verifier.py:87 |
| `UAC_LIMIT_PERIOD_MISMATCH` | error | период лимита ≠ week | uac_verifier.py:92 |
| `UAC_COUNTER_MISSING` | error | нет счётчика Метрики | uac_verifier.py:96 |
| `UAC_GOAL_MISSING` | error | нет цели | uac_verifier.py:99 |
| `UAC_REGION_MISSING` | error | регионы не заполнены (`detail.regions≤0`) | uac_verifier.py:102 |
| `UAC_UTM_MISSING` | warn | нет `tracking_params` (UTM) | uac_verifier.py:105 |
| `UAC_MAPS_ENABLED` | error | «Карты» включены (должны OFF) | uac_verifier.py:108 |
| `UAC_ALTERNATIVE_TEXTS_ENABLED` | error | персонализация включена (должна OFF) | uac_verifier.py:111 |
| `UAC_RECOMMENDATIONS_ENABLED` | error | «Директ помогает» включён | uac_verifier.py:114 |
| `UAC_PRICE_RECOMMENDATIONS_ENABLED` | error | ценовые рекомендации включены | uac_verifier.py:117 |
| `UAC_TITLES_MISSING` | error | заголовков <5 | uac_verifier.py:120 |
| `UAC_TEXTS_MISSING` | error | текстов <3 | uac_verifier.py:124 |
| `UAC_SITELINKS_MISSING` | warn | быстрых ссылок <8 | uac_verifier.py:128 |
| `UAC_MEDIA_MISSING` | warn | нет медиа (картинки/видео) | uac_verifier.py:132 |
| `CONTENT_GAP_NO_CREATIVE` | **блок создания** | tp6/tp7 **не создаём ТОЛЬКО в вырожденном случае: 0 картинок И 0 видео** для своего `ct` (порог env `DIRECT_UAC_IMAGES_CREATE_MIN`, дефолт `1`; `0` снимает блок совсем). Единственная оставшаяся preflight-блокировка по креативам — прежняя `CONTENT_GAP_IMAGES_LOW` (`пул < 5` → не создавать) **УДАЛЕНА 2026-07-27** как основанная на неверном прочтении потолка Яндекса за минимум. | create_set_master_product.py:957-976 |
| `UAC_IMAGES_LOW` | error | **в пуле своего `ct` есть ≥5 картинок (порог `DIRECT_UAC_IMAGES_MIN`, дефолт 5), а в кампанию попало меньше** — картинки теряются по дороге. Ровно **одна** попытка пересоздания с удалением; если после неё снова меньше — кампанию ОСТАВЛЯЕМ и пишем ошибку (второй попытки не бывает: `repair_auto.auto_recreate_request` не планирует recreate для джобы с `_repair_parent_job_id`). Добор из `ct0000`/другого `ct` запрещён. Пул не прокинут (старые джобы) → прежнее строгое поведение. | uac_read.py + uac_verifier.py |
| `UAC_IMAGES_POOL_SHORT` | warn | **в пуле своего `ct` физически меньше 5 картинок и мы взяли всё, что есть** — это НЕ ошибка (решение Семёна 2026-07-27: «мне нужна кампания даже с 4 изображениями, это лучше чем вообще её не будет»). Кампания создаётся с тем, что есть; удалять/пересоздавать НЕЛЬЗЯ — код намеренно отсутствует в `repair_planner._RECREATE_CODES` и `repair_gate._UAC_REPLACE_CODES`. Чинится только добавлением картинок в `Manual/<ct>/`. | uac_verifier.py |
| `UAC_VIDEO_MISSING` | low | видео-марка (BAIC/Belgee/Haval/Москвич) с картинками, но БЕЗ видео при непустом пуле (D3-UAC 2026-07-09) | `campaign_spec_audit._audit_uac_video_missing` |
| `UAC_FEED_MISSING` | error | tp7 без фида | uac_verifier.py:135 |
| `UAC_PRODUCT_MODEL_FILTER_MISSING` | error | tp7 сегмента **«Модели»** без модельного фильтра; сегмент «Марки»/«Общее» не требует model-filter. Для AUTO_RU модельным полем считается `folder_id`/`modification`, для YML — `model*` | uac_verifier.py:138 |

**1.b.5 — Spec-audit + авто-фиксеры (`campaign_spec_audit.py`)** — соответствие слепку; у большинства ЕСТЬ реальный `fix_*`/`execute_*` (запускаются CLI `--fix` и delayed-repair оркестратором, `campaign_spec_audit.py:2152-2184`).

| Код | Sev | Что значит | Где (файл:стр) | Fix-функция |
|---|---|---|---|---|
| `KEYWORDS_WRONG_GROUP` | high | ключи попали не в свою группу | :279 | ✅ `fix_keywords_wrong_group` (:1202) |
| `FOREIGN_MODEL_KEYWORDS` | medium | ключи с токенами чужих моделей марки (**+ D8 2026-07-09: тема/«Общее»-группы** — любой марка/модель-токен `_auto_brand_tokens`) | :338 | ✅ `fix_foreign_model_keywords` (:1224) |
| `KEYWORDS_WRONG_GROUP` | high | полный сдвиг ключей в чужой ct (**+ D8: «Общее»**, не только «Модели») | `_audit_search_keywords` | ✅ `fix_keywords_wrong_group` |
| `CONTENT_TEXTS_LOW` | low | адаптивное объявление с <3 текстами (bodies) — DoD §2.4 (D9 2026-07-09) | `_audit_tp1_adaptive` | ✅ `fix_texts_low` (regen `_regen_texts` + Grid RMW) |
| `GLOBAL_MINUS_CAMPAIGN_MISSING` | warn | tp2/tp4/tp5 без глоб.минус на кампании (D6 2026-07-09) | `_audit_global_minus_campaign` | ✅ `fix_global_minus_campaign` (`set_campaign_minus_keywords`) |
| `GROUP_COUNT_BELOW_SLEPOK` | warn | модель-групп tp меньше слепка (агрегатно, D10 2026-07-09) | `_audit_group_count_vs_slepok` | ⬜ **report-only** (recreate недостающих позиций вручную/next-run) |
| `CT_SLEPOK_IMAGES_EMPTY` | warn | брендовый ct без картинок в слепке → общий пул (D5 2026-07-09, minimum) | `_audit_ct_slepok_images` | ⬜ **report-only** (наполнить слепок — контент) |
| `IMAGES_FORBIDDEN` | ⛔ **ОТМЕНЁН 2026-07-19** | ~~картинки на поисковом TextAd~~ — правило отменено решением Семёна: картинки в поиске ДОПУСТИМЫ (их ставит вкладка «Смена изображения»), детект и репейр погашены флагом `SEARCH_IMAGES_FORBIDDEN_RULE_ENABLED=False` | `_audit_search_images` | ⬜ отключено (см. ERRORS_JOURNAL «SEARCH_IMAGES_AUTOCLEAN_CANCELLED») |
| `FEED_FILTER_MISSING_GRID` | medium | фидовая группа без обязательного feed-filter. **Исключение 2026-07-24:** для `site_type='С пробегом'` глобальные минус-марки/модели НЕ являются обязательным фильтром; общие БУ-фидовые группы могут идти без global `NOT_CONTAINS`, а бренд/модельные должны иметь positive-фильтр своей марки/модели | :494 | ✅ `fix_feed_filters_grid` (:1702) |
| `LISTING_POSITIVE_FILTER_MISSING` | high | ListingAd без позитивного фильтра каталога | :506 | ✅ `fix_listing_positive_filter` (:1802). **R2-4 (б) 2026-07-10:** брендовая группа = ТОЛЬКО позитив name CONTAINS_ANY [своя марка]; убран негативный `_lad_minus_conds` (NOT_CONTAINS_ALL 8 чужих) из `_grid_add_listings_with_name_filters` (при провале позитива в кабинете оставался только негатив «не содержит knewstar,…» 176/198). Общее/ct0000 — негатив-глоб-минус как есть. ⚠️ **follow-up:** AUTO_RU фид **3537034** (yandex.xml) — поле листинга не в `fieldsForUseAs` → позитив-фильтр для этого фида невозможен текущим API |
| `PLACEMENTS_WRONG` | medium | `placementTypes` ≠ эталону tp5 | :556 | ✅ `fix_placements_wrong` (:1847) |
| `GENERIC_FALLBACK_GROUP` | high | группа собралась на generic-фолбэке | :590 | ✅ `fix_generic_fallback_group` (:1880) |
| `EXTRA_TP_NOT_IN_SLEPOK` | medium | создан tp, которого нет в слепке | :637 | ⬜ (флаг; удаление ручное) |
| `BUTTON_MISSING` | low | объявление с href без кнопки «Получить скидку» | :716 | ✅ `fix_button_missing` (:1466, RMW) |
| `BUTTON_MISSING_NO_HREF` | low | нет кнопки И нет href | :733 | ⬜ (`fixable:False` — нечего чинить) |
| `VIDEO_MISSING` | low | видео-марка без ролика | :780 | ✅ `fix_video_missing` (:1580) |
| `VIDEO_NO_POOL` | info | нет валидного видео в пуле для ct | :796 | ⬜ (`fixable:False` — внешнее) |
| `IMAGE_MISSING` | medium | нет картинки у объявления (spec) | :817 | ✅ `fix_image_missing` (:1675) |
| `SHORT_TITLES` | low | заголовок <48/56 (tp1/tp2/tp4 адаптив / UAC) | :837, :943 | ✅ `fix_short_titles` — **LLM-РЕГЕНЕРАЦИЯ** (`content_quality.regen_titles`, тот же `_llm_pair_for`), НЕ суффикс; до 4 попыток |
| `SHORT_TITLES_UNFIXABLE` | error | регенерация не дала заголовок ≥48 после 4 попыток (или слепок не восстановлен) | fix_short_titles | ⬜ (`fixable:False`, терминальный hard-fail вместо тихого суффикса) |
| `BRAND_NOT_FIRST` | low | марка/модель группы НЕ до первой точки заголовка или стоит отдельной первой фразой `{brand}.` (tp1/tp2/tp4/tp5). **R2-6 2026-07-10: ТОЛЬКО сегменты Марки/Модели** (фильтр `_ct_segment(ct) in (Марки,Модели)`); «Общее» (ct0000 И тема-cts ct0010/ct0014) исключены — иначе ложный UNFIXABLE | `_audit_brand_not_first` + `content_quality.brand_head_ok` | ✅ `fix_brand_not_first` (`regen_titles need_brand_first=True`) |
| `BRAND_NOT_FIRST_UNFIXABLE` | error | brand-first регенерация не удалась после 4 попыток | fix_brand_not_first | ⬜ (`fixable:False`, терминальный hard-fail) |
| `UTP_RELEVANCE_FAILED` | warn | LLM-судья не одобрил дубли УТП/релевантность после 4 регенераций (маркер в `warnings`/`utp_judge` генерации) | `content_quality.audit_and_regen_utp` (на генерации) | ⬜ (на генерации — warn-маркер, не блок черновика) |
| `NO_LISTING` | medium | фидовая tp5/tp7 без ListingAd | :885 | ✅ `fix_no_listing` (:1643) |
| `FEED_FILTER_MISSING_UAC` | medium | UAC-товарка без обязательных feed-фильтров. **Новый контракт 2026-07-24:** `ct0000`/общая tp7 не требует feed-filter; марочная/модельная tp7 требует только positive-фильтр по конкретной марке/модели (`vendor`/`mark_id` или `model`/`folder_id`), без global minus; `collectionId` только в `listings_feed_filters` | :988 | ✅ `fix_feed_filters_uac` (:1504) |
| `SITELINK_MISSING` | warn | нет быстрых ссылок на объявлении | :1017 | ✅ `fix_sitelinks_missing` (:1307) |
| `CALLOUTS_MISSING` | low | нет уточнений | :1042 | 🟡 (наследуемые Grid-callouts) |
| `AUDIT_ERROR` | — | сам аудит упал на кампании (внутр.) | :1173 | — (диагностика) |

---

### 1.b-off `SEARCH_IMAGES_AUTOCLEAN_CANCELLED` — автоочистка картинок в поиске ОТМЕНЕНА (2026-07-19)

> ⚠️ **Это НЕ баг и НЕ регрессия.** Увидел «поисковая кампания с `imageHashes`, аудит молчит» —
> так ЗАДУМАНО, чинить НЕ надо. Дубль записи лежит в `ERRORS_JOURNAL.md`.

- **Что было:** правило «в поиске (tp2/tp4/tp5) картинок быть не должно» → детект
  `IMAGES_FORBIDDEN` → repair_plan → `images_forbidden_repair` → Grid `UpdateAdaptiveTextAds`
  с `imageHashes=[]` (RMW). Разово так вычистили 2049 объявлений на 5 аккаунтах.
- **Почему отменено (решение Семёна, озвучено прямо 2026-07-19):** админ-вкладка «Смена
  изображения» (`/direct/automation/content`, `content_images_routes.py`) показывает и ЗАМЕНЯЕТ
  картинки во ВСЕХ НЕАРХИВНЫХ кампаниях, включая поисковые. Архивные объявления не изменяем:
  старый хэш в архиве после замены — ожидаемый остаток, не дефект DoD. Если Grid обнаружил
  архивность только на записи (`CANNOT_UPDATE_ARCHIVED_AD`), это тоже считается штатным
  пропуском. Автоочистка вступала бы с ней в цикл:
  оператор заменил картинку → отложенный репейр её снёс.
- **Что именно отключено — 4 точки, везде флаг `SEARCH_IMAGES_FORBIDDEN_RULE_ENABLED = False`:**
  1. `campaign_spec_audit.py` (флаг перед `_audit_search_images`) — детект гасится У ИСТОЧНИКА:
     функция возвращает `[]` без Grid-запроса → issue не рождается → нет action в плане → нет
     репейра. Правлены также docstring модуля и `SPEC["tp2"]/["tp4"]`.
  2. `repair_planner.py` (`_action_for_issue`, ветка `IMAGES_FORBIDDEN`) — `return None`: даже
     issue из СТАРОГО отчёта не превращается в действие.
  3. `repair_gate.py` (`executable_images_forbidden_repairs`) — возвращает `[], [], matched`
     (тот же приём, что у `executable_adprice_repairs`): СОХРАНЁННЫЕ в job-result планы прошлых
     джоб не исполняются; `images_forbidden_repair_campaigns` и вклад в `executable_now` = 0.
  4. `repair_media.py` (`execute_images_forbidden_repair`) — не-мутирующий no-op ДО построения
     куки и любых Grid-вызовов (гасит и прямой вызов из CLI `campaign_spec_audit --fix`),
     отдаёт `{"ok":True,"disabled":True,"repaired":0}`, http 200.
- **Как вернуть:** поставить флаг `True` во всех четырёх файлах. Код детекта и RMW-очистки
  сохранён целиком, ничего не удалено.
- **Не путать:** `execute_images_repair` / `campaign_images_repair` (добивка ПУСТЫХ картинок tp1,
  `IMAGE_MISSING` / `NO_IMAGES_LIVE`) — нужное поведение, НЕ трогали.
- **Открытый вопрос Семёну (НЕ менял):** `create_set_tp1_builders.py:1948` при СОЗДАНИИ по-прежнему
  не цепляет картинки поисковым (`_want_images = tp_code not in ("tp2","tp4")`) — тот же отменённый
  принцип, но на стороне создания, а не очистки. Новые tp2/tp4 рождаются без картинок (добавить
  можно вкладкой, и теперь их никто не снесёт). Менять ли — решение Семёна.

---

## 1.c Петля «создано → авто-аудит по DoD → авто-добивка» (как работает + дыры)

> Аудит созданных кампаний ОБЯЗАН идти по этому DoD: кампания создалась → если не соответствует →
> добивается автоматически. Ниже — как это реально устроено в коде (аудит 2026-07-09) и где петля рвётся.

**Петля запускается САМА (без ручного триггера), 3 стадии:**
1. **Синхронно в конце создания** (`create_set_postprocess.run_create_set_postprocess`, из `create_set_orchestrator.py`):
   `verify_create_set` (статик) → `live_verification` (live через Grid/UAC: `verification_service`→`live_verifier`→`grid_content_verifier`/`uac_verifier`/`campaign_state_verifier`) → `execute_safe_post_create` чинит СРАЗУ только безопасное in-place (promo/callouts/rename); остальное отложено (Grid-lag).
2. **При терминале `status=done`** (worker): `_auto_queue_recreate_after_done` (авто-recreate) + `_schedule_delayed_content_repair_after_done` (строка в `direct_delayed_repairs`).
3. **Демон `_delayed_repair_daemon_loop`** (poll 60с): через 180с `_run_delayed_content_repair` → свежая live-сверка → `execute_all_in_place` → `_run_spec_audit_and_fix` (14 авто-фиксеров) → ре-верифай → reschedule «до нуля» (кап 1) → `_requeue_missing_positions_once` → reconcile. Guard от ложных edit-view детектов (`_show_condition_kw_counts`, журнал I/J).

**2026-07-22 (repair_media/queue_server):** `execute_images_repair` при структурно пустом пуле картинок
ct (не upload-fail) ставит терминальный `image_no_pool` (аналог `VIDEO_NO_POOL`, `ok=False`) →
`_repair_failures_nonfixable` останавливает reschedule; transient-ошибки резолвера идут в отдельный
`resolver_fail_cts` и `image_no_pool` не получают. Watchdog-убитая `content_repair` джоба (child
`dcr:{did}`) теперь авто-реквьюится (`_delayed_content_repair_requeue_after_watchdog`, кап
`_DELAYED_REPAIR_WATCHDOG_REQUEUE_MAX=2`, backoff 300с) — раньше остаток действий терялся навсегда.

**Покрыто авто (детект + добивка):** ядро §1.b — группы/объявления/ключи/фиды/картинки/видео/сайтлинки/
автотаргет/минус-марки/каталог/промо/уточнения/имена; все UAC-коды tp6/tp7 (§1.b.4); §1.a 1.3/1.6/1.7/1.8.

**🕳 ДЫРЫ ПОКРЫТИЯ (петля не добивает):**
- ~~**P0 — кампанийные галочки tp1–tp5 НЕ проверяются пост-аудитом.**~~ ✅ **ЗАКРЫТО 2026-07-09**
  (🟡 ждёт живого прогона Семёна). Добавлена campaign-level секция live-verify в `grid_content_verifier`
  + чтение полей кампании через edit-view `CampaignsEditData` (`grid_finalize.read_campaign_invariants`
  → `grid_read._enrich_campaign_invariants` → tri-state в `campaign_content_counts`) + идемпотентный
  in-place ремонт `campaign_invariant_repair` (`grid_finalize.set_campaign_invariants` — узкий
  `UpdateCampaigns`, БЕЗ баллов, РК DRAFT), подключён в авто-петлю `execute_all_in_place` (delayed-цикл).
  **Новые issue-коды** (tp1–tp5, только при `campaign_invariants_read=True` И явном булеве — fail-safe,
  None=тишина): `ALT_TEXTS_ENABLED_LIVE` (#3), `EXTENDED_GEO_ENABLED_LIVE` (#5),
  `RECOMMENDATIONS_ENABLED_LIVE` (#6), `PRICE_RECOMMENDATIONS_ENABLED_LIVE`, `COMPANY_INFO_ENABLED_LIVE`
  (Карты/организации), `MAPS_ENABLED_LIVE` (yandexMaps), `ORG_LIST_ENABLED_LIVE` (serpGeoWizard) — все
  чинятся одним `campaign_invariant_repair`; `STRATEGY_MISMATCH_LIVE` (warn, **report-only**: cpc→
  payForConversion=False / cpa=True по коду набора; авто-правка стратегии рискованна → на recreate,
  не in-place). **Осознанно НЕ покрыто этим фиксом:** #4 мониторинг сайта (`hasSiteMonitoring`) — поля
  НЕТ в read-схеме Grid (`grid_campaigns_edit_data.graphql`) → не детектируется, лишь идемпотентно
  переставляется (=True) ремонтом при любом другом нарушении; **#2 UTM-на-группах** и **1.4 глоб.минус
  на кампании** — группо-уровневый/требует резолва shared-set-id, вынесены отдельно (см. P1, чтобы
  детект без ремонта не зациклил «до нуля»).
- ~~**1.4 глоб.минус на кампании**~~ ✅ **ЗАКРЫТО 2026-07-09 (D6):** `_audit_global_minus_campaign`
  (tp2/tp4/tp5) читает inline `minusKeywords` + `libraryMinusKeywordsIds` из Grid unified-payload;
  флаг `GLOBAL_MINUS_CAMPAIGN_MISSING` ТОЛЬКО когда shared-set пуст И inline не содержит требуемые
  слова (fail-safe: shared-set есть → молчим, содержимое дёшево не резолвим). Ремонт inline через
  `fix_global_minus_campaign` → `grid_finalize.set_campaign_minus_keywords` (узкий `UpdateCampaigns`,
  БЕЗ баллов, идемпотентно) — консистентно с детектом → без reschedule-цикла.
- **Дыры покрытия закрыты 2026-07-09 (блок К2, детекторы):** D8 foreign/wrong-kw на «Общее»/тема
  (`_audit_search_keywords` снял «только Модели» → FOREIGN_MODEL_KEYWORDS + KEYWORDS_WRONG_GROUP на
  тема-группах, дискриминатор = `_auto_brand_tokens`); D2 brand-first+short-titles на tp5
  (`audit_campaign` tp5 → `_audit_tp1_adaptive`/`_audit_brand_not_first`); D3-UAC видео-марки
  (`UAC_VIDEO_MISSING` → recreate); D9 `CONTENT_TEXTS_LOW` (<3 текстов → `fix_texts_low` regen);
  D10 `GROUP_COUNT_BELOW_SLEPOK` (агрегатное покрытие модель-ct, **report-only**); D5
  `CT_SLEPOK_IMAGES_EMPTY` (брендовый ct без картинок в слепке, **report-only**).
- **R2-6 2026-07-10 — закрыт рассинхрон audit↔repair (`KEYWORD_REPAIR_NO_PACK_SILENTLY_OK`):**
  `repair_executor.execute_keywords_repair` при КС-группе без ключей + пустом паке молча возвращал
  `ok=True, skipped` (одна ветка путала «автотаргет by-design» и «КС с пустым паком») → добивка писала
  «нет групп для keyword-repair» вместо ошибки, хотя аудит рядом флагал `NO_KEYWORDS_LIVE`. Фикс:
  `_at_by_design = "автотаргетинг" in camp_name` → AT-группа=`ok`-skip; иначе КС → `failed` («нет ключей
  от pack для этого ct»). Теперь audit и repair согласованы (пустой пак = честный failed, а не тихий ok).
  ⚠️ САМИ ключи зальются только когда пак дозаполнен (данные слепка, §5.2) — код чинит лишь диагностику.
- **P1 (остаётся):** #2 UTM-на-группах (`TrackingParams`) — не покрыт (группо-уровень, риск ложных
  детектов). `IMAGES_FORBIDDEN` ⛔ ОТМЕНЁН 2026-07-19 (см. §1.b-off), раньше — только repair_plan; live-fix
  `WRONG_AUTOTARGET`/`NO_KEYWORDS_LIVE` через `UpdateUnifiedAdGroups` хрупок (edit-view lag) —
  эскалировать на recreate / подтверждать showConditions. **2026-07-27 диагноз уточнён:** для СВЕЖИХ
  групп это не «хрупко», а **доказанный no-op** (см. §1.2a) — как средство ПЕРВИЧНОЙ установки
  автотаргета пост-фактум `UpdateUnifiedAdGroups` применять нельзя вовсе; автотаргет ставится
  атомарно при создании группы. In-place ремонт по УЖЕ отреплицировавшимся группам
  (`keywords_repair`) остаётся легальным.

**Вердикт:** петля «создано→не по DoD→добивается» — ядро + кампанийные галочки tp1–tp5 (P0) добиваются
авто (P0 закрыт кодом 2026-07-09, ждёт живого прогона). Остаток P1: UTM-на-группах, минус-на-кампании,
#4 мониторинг (нет в read-схеме Grid).

---

## 2. Контент объявлений — общая база (для всех tp)

> Здесь — правила, общие для ЛЮБОГО типа кампании: что считается готовым контентом, какие два
> провайдера генерят текст, что происходит когда ИИ недоступен, как грузим видео, требования к текстам.
> tp-специфика (сколько заголовков, нужно ли видео/adPrice) — в §3 по каждому tp.

### 2.R2-8 — 7 live-дефектов Семёна (2026-07-10 вечер, прогон af4bd7bd5a52) — 🟡 в работе

> Найдены Семёном в кабинете (Grid-кука API 403 → верификатор их не видел). Инварианты добавлены здесь.

1. **Заголовки должны ПОКАЗЫВАТЬ, что продаём АВТО, а не только финансы.** Живой факт (BAIC): все 7 УТП
   чисто финансовые (кредит/взнос/одобрение/КАСКО/трейд-ин/выгода/госпрограмма) → «смазано», не видно
   что это ПРОДАЖА АВТОМОБИЛЕЙ. Требование: часть заголовков/УТП нести авто-контекст («новый авто»,
   «авто в наличии», «новые автомобили {бренд}», «{бренд} 2025 в наличии»), сохранив кредитный угол —
   БАЛАНС, не только финансы. (Уточняет §2.x «кредитный угол обязателен» — теперь + авто-ясность.)
2. **tp7 сайтлинки — все 8 (заголовки tp7 ОК).** В tp7 быстрые ссылки заполнены не полностью (<8).
   Нужно ровно 8 с описаниями. Проверить, применяется ли %-backup-филлеры (R2-6) на UAC-пути.
3. **tp7 имя↔режим таргетинга.** Кампания в имени «Автотаргетинг», а по факту выбрана «ручная настройка
   аудитории» — рассинхрон. Автотаргет-имя → автотаргет-режим (не audience/keywords).
4. **Сайтлинки — ОДИН набор на ЛОГИН, переиспользовать во ВСЕХ кампаниях.** Сейчас различаются по
   кампаниям. Решение Семёна: генерить сайтлинки один раз на проход-логин → ставить во все tp1–tp7.
   Флаг `DIRECT_SITELINK_REUSE_ACCOUNT` — **по умолчанию 0/выключен** (`ai_content.py:168-169`):
   требование НЕ выполнится, пока флаг не включён (`=1`). Включить + проверить, что механизм реально
   шарит один набор.
5. **«Текст по умолчанию» — БЕЗ кредита/автокредита.** Товарные/каталожные объявления: default_text не
   должен упоминать кредит/автокредит/взнос/платёж/одобрение банка. Продуктовый фокус (авто/наличие/
   скидка/подбор/тест-драйв). (Отменяет прежний «кредит-угол» ДЛЯ default_text ShoppingAd — см. §2.5.)
6. **«Текст по умолчанию» — ОДИН общий** на все каталожные И товарные кампании (сейчас различается).
   Единая константа без кредита (п.5), реально переиспользуемая.
7. **Общее-ct ключи из СЛЕПКА, не генерик.** tp5 Общее-КС ct0010 «Дром» / ct0014 «Авто/Автомобили/
   Машины» получили генерик-кредитные фразы («автомобиль в кредит»…) вместо слепковых. Проверить пак
   `Мультибренд/{tp2,tp5}/{ct0010,ct0014}/keywords/scherbakova.txt` — пусто/генерик-фолбэк? Дозаполнить
   с реальных аккаунтов Щербаковой (сегменты Дром/Авто-синонимы).

### 2.0 Что считается ГОТОВЫМ контентом слепка

Слепок «готов к созданию», когда для его `(segment, tp, ct)` есть ВСЁ:

| Составляющая | Откуда | Проверка «готово» |
|---|---|---|
| **Ключи** | пак: `PACK_ROOT/{segment}/{tp}/{ct}/keywords/{slepok}.txt` (`read_keywords`). `PACK_ROOT=PACK_MOUNT/kontent_oktyabr`, а `PACK_MOUNT=$NEURO_PACK_MOUNT` — **в проде = `/opt/neuro_content_local`** (ЛОКАЛЬНАЯ копия на LXC101, синк `sync_content_m3.py`), задан в `direct-create.service`+`direct-create-worker.service` env (раньше назывались `direct.service`/`direct-worker.service`). Сам M3 в горячем пути чтения ключей НЕ участвует. sshfs-монт `/opt/neuro_kontent` — лишь код-дефолт `kontent_pack.py:31` (обратная совместимость), env его перекрывает. | не пусто |
| **Тексты** (заголовки/тексты/б.ссылки) | генерируются LLM (см. 2.0bis) из слепка; фолбэк — БД `direct_slepok_content` (kinds `promo`/`campaign`) | есть чем сгенерить/фолбэкнуть |
| **Voice/стиль** | `ai_agents.py::AGENTS[slepok]` (`get_agent` → не None) | агент есть в селекторе |
| **Картинки** | пред-залиты в img-cache по `ct` модели из папки слепка. Для tp6/tp7 **цель** — 5 image именно своего `ct` (это ПОТОЛОК Яндекса `adConstants.maxNumberOfImages=5`, а не его минимум: `POST /web-api/uac/campaigns` принят с 1 и с 3 креативами, HAR 4har/5har). Меньше 5 в пуле — **не повод не создавать**: берём всё, что есть, и пишем warn `UAC_IMAGES_POOL_SHORT` (решение Семёна 2026-07-27). Ошибка `UAC_IMAGES_LOW` — только когда пул полный, а в кампанию попало меньше. video картинкой не засчитывается; fallback на `ct0000`/другой `ct` по-прежнему **запрещён** (тихой замены быть не должно). Единственный блок создания — вообще нет креативов (0 картинок И 0 видео), порог `DIRECT_UAC_IMAGES_CREATE_MIN` (дефолт 1). | картинка резолвится и `ct` совпадает |
| **Видео** | видео-пул (см. 2.4) — до **2** роликов на `ct` (лимит подтверждён офиц. документацией Яндекса) | для видео-марок ролик есть |
| **Быстрые ссылки** | БД слепка → M3-генерация (`_ai_sitelinks`) → общие фолбэки | 8 шт, без смысловых дублей |

Если для `ct` нет своего контента → фолбэк на общую папку/группу (не падаем).
Недостающие креативы — **пропускаем** (не блокируем набор).

### 2.0bis Провайдеры генерации текста (M3 + OpenRouter) — двусторонний фолбэк

Текст (заголовки/тексты/быстрые ссылки) генерят **ДВА провайдера** с автоматическим взаимным фолбэком
(`llm_providers.py::_llm_pair_for`):

| Провайдер | Что это | Где |
|---|---|---|
| **M3** | mlx локально, порт **8086** (модели 14B/72B), через туннель на Mac | `_m3_complete_url` |
| **OpenRouter** | **DeepSeek V4 Flash**, платно (~$0.2/набор ~25 РК), через прокси **mihomo** на LXC101 | `_or_complete_url` |

- **Кто primary/secondary** — из попапа UI: поле `llm_provider` (`m3` | `openrouter`) в теле запроса
  `api_create_set` → прокидывается в КАЖДЫЙ item (`routes_jobs.py:74`, `_it.setdefault("llm_provider", …)`).
  В `_llm_pair_for`: `provider=="openrouter"` → primary=OpenRouter, secondary=M3; иначе → primary=M3,
  secondary=OpenRouter.
- **Дефолт (попап не прислал m3/openrouter)** — `openrouter` (`ai_content.py:171`, seed
  `blueprint.py:7887`): т.е. по умолчанию primary=**OpenRouter (платно)**, M3 — фолбэк. Не «M3-first» —
  несмотря на исторические названия.
- **Переключение видно в логе** — `[llm-fallback] {tag}` (`tag` = `M3→OpenRouter` или `OpenRouter→M3`),
  где `{tag}` показывает направление отказа. Плюс `[llm-preflight]` когда M3-primary отсекается
  fail-fast health-check `_m3_preflight_ok` (3×3с GET `/v1/models`) вместо ожидания полного read-timeout
  (90–480с) на мёртвом туннеле 8086.
- **Preflight платёжеспособности OpenRouter обязателен:** `/api/v1/key` недостаточен. Перед
  stream-create `check_content_pipeline_health()` должен проверить `/chat/completions` с
  `OPENROUTER_COMPLETION_PROBE_TOKENS=800`; если M3 completion-preflight мёртв и OpenRouter даёт
  402/не проходит completion-probe, набор блокируется до создания объектов (`content_pipeline_dead`).
- **M3 fan-out разрешён только по разным endpoint'ам; одинаковый M3 URL сериализуется process-wide.** Если 4×14B выключены и
  `M3_LLM_URLS_14B` свёрнут в один URL (`8086`), titles/texts/sitelinks должны выполняться
  последовательно (`max_workers=1`), а разные create-set job в одном worker-процессе должны брать
  per-endpoint lock в `_m3_complete_url`. Иначе один 72B endpoint получает параллельные запросы,
  часть клиентов ловит idle-timeout до первого токена, breaker/gate ложно переводит набор в OpenRouter.
  Ожидание этого lock тоже считается живой LLM-работой текущей job: `_m3_endpoint_guard()` обязан
  вызывать scoped heartbeat, иначе DB-watchdog видит `updated_at` stale и убивает job, которая просто
  стоит в очереди к `8086`. Если LLM-вызов выполняется через `ThreadPoolExecutor`
  (`_m3_complete_parallel()` или `_llm_pair_for(...)._par()`), текущий heartbeat job-id должен явно
  переноситься в дочерний поток через `_run_with_llm_heartbeat_job()`: `threading.local()` сам по себе
  не наследуется, и heartbeat в executor-потоке иначе становится no-op.
  Внешний content-prefetch executor (`create_set_orchestrator._prefetch_content()`) тоже должен
  отправлять `_cached_campaign_content` через heartbeat wrapper; иначе OpenRouter-primary prefetch
  активно пишет `OpenRouter→M3`, но job heartbeat всё равно теряется уровнем выше.
  Проверка: `_llm_pair_for("m3")._par` для трёх одинаковых M3 URL даёт `max_active=1`; для разных
  M3 URL и OpenRouter-primary параллельность сохраняется; concurrent calls в один `8086` не должны
  выполняться одновременно внутри процесса; `_m3_endpoint_guard()` heartbeat-тест и два теста
  propagation контекста для `_m3_complete_parallel()` / `_llm_pair_for(...)._par()` должны проходить,
  плюс source-контракт на `_content_executor.submit(_run_content_with_heartbeat, ...)`.
  **2026-07-26:** ожидание per-endpoint lock обязано быть bounded (`M3_ENDPOINT_LOCK_MAX_WAIT`,
  default 90с). Hidden AI-запросы общих siteLinks на create-stage запрещены: `_common_sitelinks_fast`
  и tp1/tp5-builders используют только готовый item-content / БД слепка / deterministic fallback,
  а `_ai_common_sitelinks` по умолчанию возвращает `[]` (`DIRECT_CREATE_AI_COMMON_SITELINKS=0`).
  Это защищает parallel create от ситуации «один поток завис в M3-stream, второй бесконечно ждёт
  M3-lock, главный поток ждёт join».
  Если `_m3_preflight_ok()` не прошёл, но
  `m3_completion_preflight_ok(retries=1)` прошёл, M3 считается живым: `check_content_pipeline_health`
  и `_llm_pair_for("m3")` не должны уходить в OpenRouter только из-за моргнувшего `/v1/models`.

### 2.0ter Фолбэк генерации при недоступности LLM (M3-гейт)

> ✅ **АКТУАЛИЗИРОВАНО (2026-07-24):** прежняя версия разрешала детерминированный фолбэк
> из слепка при недоступности обоих LLM. Для live create-set это запрещено: если M3 и
> OpenRouter completion-preflight недоступны, набор/текущий item останавливается до создания
> новых Direct-объектов.

Перед созданием КАЖДОГО пункта набора вызывается гейт `_m3_gate_wait`
(`blueprint.py:4397`, дёргается в `create_set_orchestrator.py:417`). Цепочка:

1. **M3 жив** (`_m3_llm_probe`) → генерим через M3. Возврат `True`.
1a. **M3 health моргнул, но completion-probe жив** (`m3_completion_preflight_ok(retries=1)`) →
   считаем M3 доступным и продолжаем. `GET /v1/models` не является достаточным основанием для стопа,
   если реальный `/chat/completions` выдаёт токен.
2. **M3 мёртв, но OpenRouter completion жив** (`_openrouter_probe && _openrouter_completion_probe`) →
   **НЕ паузим**; лог `[m3-gate] … контент пойдёт через DeepSeek V4 Flash (платно)`; фолбэк
   `_llm_pair_for` сам переключит. `True`.
3. **Оба мертвы** → **ОДНА короткая перепроверка** `_M3_GATE_RECHECK_SEC=20` с (на моргание туннеля,
   heartbeat внутри). Восстановился хотя бы один → лог «ИИ снова доступен» → `True` (на ИИ).
4. **После перепроверки оба всё ещё мертвы или OpenRouter не проходит completion-probe** → набор/текущий
   item останавливается до создания новых объектов (`503`/`content_pipeline_dead` на top-level gate или
   `False` из `_m3_gate_wait`). Продолжать на шаблонный fallback нельзя: fallback запрещён для create-set,
   иначе получится частичный набор без локального текстового контента.
5. **`False` возвращается при отмене джобы или мёртвом LLM-gate** (`job.cancel` либо оба
   completion-preflight после перепроверки не прошли) → в оркестраторе `_add_job_err` + `break`
   (`create_set_orchestrator.py:417-422`): штатная остановка без продолжения на запрещённый fallback.

✅ Итог: набор **не висит часами** на недоступном ИИ и не падает целиком из-за одного пустого
stream-ответа. Полностью мёртвый content-пайплайн блокируется до Direct-мутаций; частичный/пустой
ответ конкретного item отдаётся builder'у, где допустимы только штатные локальные шаблоны/пак и
явный content-gap при полном отсутствии данных.

⛔ **Live-путь (боевое создание): шаблонный фолбэк ЗАПРЕЩЁН** (`allow_corpus_fill=False` /
`allow_static_fill=False`, ERRORS_JOURNAL `TONE_VOICE_TEMPLATE_FALLBACK_IN_LIVE_CREATE` 2026-07-21).
При live create-set генерация работает ТОЛЬКО через LLM: корпус слепка (`agent["ads"]`),
статические fillers и `direct_slepok_content` как добор к слабому LLM-ответу — ЗАПРЕЩЕНЫ.
Если после генерации + retry полного комплекта заголовков/текстов нет, item получает ошибку
`шаблонный фолбэк запрещён` на уровне `run_gen_campaign_content`. Продолжение до builder'а допустимо
только когда LLM-пайплайн жив, но конкретный ответ неполный. Если M3 и OpenRouter
completion-preflight мертвы, `_m3_gate_wait` возвращает `False`, item не идёт в builder и новые
Direct-объекты не создаются. Generic-черновик не публикуется.
tp6/tp7: `create_set_master_product.py` в live-stream режиме берёт заголовки/тексты/сайтлинки
ТОЛЬКО из LLM item-контента; `_GENERIC_*`, `tpl_*`, `_fallback_master_titles` UAC-креатив не заполняют.

### 2.4 Как загружаем видео

- **Источник:** видео-пул `/Users/Shared/agency/Video/<ct>/*.mp4` (индекс `Video|video|<ct>`,
  `videos_pool_for_ct` — до 2 роликов; фолбэк `videos_for_ct`). Дневная работа — с ЛОКАЛЬНОЙ копии пака
  (`NEURO_PACK_MOUNT=/opt/neuro_content_local`), синк ночью `scripts/sync_content_m3.py` (крон 03:00 Екб,
  видео ≤9.9 МБ) → днём не зависим от M3. **155 уникальных роликов на 16 моделей** (сверено, распределение верное).
- **tp6/tp7 (UAC):** видео → `content_ids`: `upload_video_file(path)` → id в UAC-payload.
- **tp1 (РСЯ):** видео — **отложенная добивка ПОСЛЕ создания** (`_tp1_video_ads`):
  `upload_video_creative(path)` → `meta.creative_id` → `creativeIds` в `UpdateAdaptiveTextAds`.
  (⚠️ мутация объявления без `creativeIds`/`ad_price_payload` ЗАТИРАЕТ видео и цену — поэтому видео-attach
  несёт `meta.ad_price_payload`; Фаза 3.6.) Если `videos_for_ct(login,ct)` пуст, tp1 добивка пробует
  только `read_videos(site_type,"tp1",ct)` из ct-пака текущего типа сайта; если и там пусто, ролик не
  подменяется чужим.
- **Проверка (read-back):** `hasVideo` через grid. Марка видео-типа (BAIC/Belgee/Haval/Москвич)
  без ролика → код `VIDEO_MISSING` → brand-fallback + докрутка **до полного нуля**.
- **Технические лимиты Яндекса (офиц. документация, `docs/campaign-master/site.md:60-74`):**
  MP4/WebM/MOV/QT/FLV/AVI, ≤100 МБ, длительность **5–60 с**, рекомендованное соотношение сторон
  **16:9 / 1:1 / 9:16** (3:4/4:3 в рекомендациях НЕТ), мин. разрешение 360p (рек. 1080p), ≥20 к/с,
  кодеки H.264/VP6F/VP8/Theora, **до 2 видео на объявление**. В коде проверяется только НИЖНЯЯ
  граница длительности `YANDEX_VIDEO_MIN_DURATION=5.0` (верхняя 60 с кодом не форсится); размер —
  наша практика сжатия `YANDEX_VIDEO_MAX=9.9 МБ` (с запасом от 100 МБ).
- ✅ **Лимит 2 — ПОДТВЕРЖДЁН, не баг (Семён 2026-07-09):** во ВСЕХ вызовах кода `limit=2`
  (`videos_for_ct`/`videos_pool_for_ct` дефолт 2, `_tp1_video_ads limit_per_group=2`,
  `create_set_master_product.py:423`) — это ТОЧНО совпадает с офиц. лимитом Яндекса «до 2 видео».
  Прежнее предположение про «5 роликов» офиц. документацией **не подтверждается**.
- 📊 **Форматы в пуле — ФАКТ (ffprobe по всем 155 роликам на LXC101, `/opt/neuro_content_local/_video_pool/`,
  2026-07-09):** **ВСЕ 155 роликов = 1920×1080 (16:9), ноль разнообразия** — форматов 1:1 и 9:16 в пуле
  НЕТ. Длительности выборки 5.8–11.3 с (в диапазоне 5–60 с). Т.е. рекомендацию Яндекса (16:9/1:1/9:16)
  покрываем только по 16:9 — вертикальных (9:16, под мобильные плейсменты) и квадратных (1:1) нет.
  Понятия «формат/aspect/ориентация» в коде отбора нет (фильтры только размер+длительность) — но и
  выбирать не из чего, пул однороден. Разнообразие форматов = вопрос к харвесту контента (слепки-мастер),
  не к коду создания.

### 2.x Требования к текстам

| # | Критерий | Автопроверка в коде | Статус |
|---|---|---|---|
| 2.1 | Марка/модель — ДО первой точки заголовка. **ИНТЕГРАЦИЯ (Семён 2026-07-10):** бренд — в ЖИВОЙ ФРАЗЕ, не изолированным словом перед точкой. «{Бренд}.» как самостоятельная первая фраза — ДЕФЕКТ: «Belgee. Авто в наличии» (brand + точка + отдельный УТП) = ПЛОХО. НОРМА: «Belgee в наличии», «Новый Belgee», «Купить Belgee в кредит», «Belgee трейд-ин» — бренд интегрирован в первую фразу. | Генерация: `ai_agents.build_titles_messages` — правило «⛔ ЗАПРЕЩЕНО `{Бренд}.` как изолированная первая фраза». `text_gen._brand_title_set`, `_brand_first_reorder` и `create_set_assets._upgrade_credit_titles` не выпускают изолированное `{brand}.`; **аудит** `content_quality.brand_head_ok()` дополнительно проверяет `_brand_isolated_first_phrase()` и флагает `KAIYI. Кредит...` как `BRAND_NOT_FIRST`. **Сегмент-фильтр:** аудит ТОЛЬКО Марки/Модели; Общее (ct0000/ct0010/ct0014) исключены. | 🟡 (код есть, **live не проверено**) |
| 2.1b | **Вариативность захода** заголовков (R2-6 2026-07-10) — первые ~18 символов НЕ совпадают у всех 7 (одинаковый префикс `{Бренд} в {Город}.`×7 убивает комбинации Яндекса) | `create_set_assets._upgrade_credit_titles`: варианты смешаны — `{anchor}` (brand+город) / `{brand} в наличии` / «Новый {brand}» / «Купить {brand} в кредит» / `{brand} трейд-ин`. Все brand-first. Live-аудита НЕТ. | 🟡 (генерация есть, **live не проверено**; чинит только будущие прогоны) |
| 2.1c | **Авто-контекст в заголовках** (Д1 2026-07-10) — из 7 заголовков **1–2 ОБЯЗАНЫ** явно сообщать продукт («авто в наличии», «новые автомобили», «{марка} в наличии»). Запрещено ВСЕ 7 только про кредит/финансы. Остальные 5 — кредитный угол. **Авто-контекст обязан быть ИНТЕГРИРОВАННЫМ** (2.1): «{brand} в наличии. Кредит от 9 000 ₽/мес», «Новый {brand}. Выгода до 45%» — НЕ «{brand}. Авто в наличии...» (изолировано). | `ai_agents.build_titles_messages`: «1–2 из {TITLES_N} — авто-продукт». `create_set_assets._upgrade_credit_titles`: позиции 1 и 4 = `f"{brand} в наличии. Кредит от 9 000 ₽/мес"` и `f"Новый {brand}. Выгода до 45%"` (обновлено 2026-07-10). | 🟡 (код есть, **live не проверено**) |
| 2.2 | Мало свободных символов (заголовки плотные, ≥48/56) | Промпт: жёсткий минимум 48 симв. **Live-аудит** `SHORT_TITLES` (**tp1/tp2/tp4/tp5** адаптив `:837`, UAC `:943`; D2 2026-07-09: tp5 TextAd добавлен — ShoppingAd/ListingAd без titles не трогаются) → фикс = **LLM-регенерация** (`content_quality.regen_titles`, тот же `_llm_pair_for`), НЕ суффикс; 4 попытки → hard-fail `SHORT_TITLES_UNFIXABLE`. | 🟡 (код есть, регенерация+hard-fail; **live не проверено**) |
| 2.2b | **Плотность заголовков #7 → 53-56 симв.** (density-upgrade, ЗАДЕПЛОЕНО 2026-07-13). Короткие заголовки в валидном диапазоне (48-52) семантически РАСШИРЯЮТСЯ ИИ до 53-56 (не суффиксы, не обрезка) — заполняем ширину строки Директа. `TITLE_DENSE_MIN=53`; заголовки сортируются dense-first (плотные вперёд). | Density-upgrade pass в `create_content.py` (после базовой генерации) + промпт `ai_agents.build_density_upgrade_messages` (промпт-слот 53-56). LLM сам семантически расширяет короткие валидные заголовки, не добивает символами/суффиксами. Работает поверх 2.2 (сначала ≥48, потом уплотнение к 53-56). | 🟡 (код есть, **live не проверено**) |
| 2.4 | ≥3 текста на объявлении (адаптив/поиск) | **Live-аудит** `CONTENT_TEXTS_LOW` (`_audit_tp1_adaptive`, читает `bodies` GdAdaptiveTextAd, tp1/tp2/tp4/tp5; D9 2026-07-09) → фикс `fix_texts_low` = LLM-регенерация текстов (`content_quality._regen_texts`) + Grid RMW `UpdateAdaptiveTextAds` (bodies; видео/цена сохраняются). UAC (tp6/tp7 texts<3) — `UAC_TEXTS_MISSING`→recreate. Fail-safe: `bodies` не прочитан → не флагаем. | 🟡 (код есть, **live не проверено**) |
| 2.3 | УТП не дублируются, тексты релевантны сайту | Промпт: усилены запрет дублей УТП + релевантность (titles/texts). Дедуп-генерация (`_dedup_*`+`_variant_norm_key`) сохранён. **LLM-судья** (`content_quality.audit_and_regen_utp`, на генерации, +1 короткий вызов/РК): дубли+релевантность → регенерация по претензиям судьи → 4 попытки → warn-маркер `UTP_RELEVANCE_FAILED`. Fail-open при недоступности судьи. | 🟡 (код есть, судья на генерации; **live не проверено**) |
| 2.6 | Стиль формулировок (D11 2026-07-09) | Промпты `build_titles_messages`/`build_texts_messages`: (1) «Кредит на авто»→«Автокредит» — ТОЛЬКО в **заголовках** (в текстах слово «автокредит» блокируется `_BAD_AD_TEXT_RE` → там «в кредит», естественные формулировки); (2) господрограмма/выгода в **%**, а не абсолютным ₽ (рубли только для платежа «от N ₽/мес»); (3) больше **CTA** «Купить … в кредит / по госпрограмме / Оставьте заявку». Статические резервы приведены к тому же стилю: филлеры сайтлинков (`_GENERIC_SITELINK_FILLERS`, D1 — без висячего года/канцеляризмов, topic-дедуп credit≤2 сохранён) и аварийные titles/texts `assemble_campaign` (D7 — уплотнены до ≤56/≤81, источник default_text ShoppingAd). brand-first (2.1) и длина (2.2) не тронуты. | 🟡 (промпт-правила + резервы, **live не проверено**) |

> **Реализация 2.1/2.2/2.3 (2026-07-09): единый паттерн «генерация → проверка → регенерация тем же
> LLM (`_llm_pair_for`) → до 4 попыток → HARD-FAIL».** Общий контур — `content_quality.py`
> (`retry_regen` + `regen_titles`/`_regen_texts` + `judge_utp`/`audit_and_regen_utp`). Число попыток
> 4 = generate + 3 регена (баланс качество/латентность/$). SHORT_TITLES и BRAND_NOT_FIRST — на
> live-аудите (cookie/Grid/UAC-путь, где промпт-first не гарантирует); UTP-судья — на генерации
> (дешевле, чем повторный прогон по аккаунту). Прежняя суффикс-добивка `extend_title_to_max` в
> `fix_short_titles` УДАЛЕНА (не «тихий фолбэк»). Статусы держим 🟡 пока не будет живого прогона —
> ✅ только после верификации hard-fail и регенерации в бою.

### 2.5 Цены (adPrice) и UAC-бюджет/pricing

**adPrice (цена в объявлении/группе, tp1 и фидовые tp5)** — откуда берётся (`_group_ad_price`,
`create_set_feeds.py:381-400`; правило Семёна закодировано, комментарий «Правило Семёна 2026-07-02»):
- **Группа по марке/модели** (`seg='Марки'`/`'Модели'`) → **МИН цена марки** из прайс-кэша фида.
- **Марки/модели НЕТ в фиде** → цена **ПУСТАЯ `(0,0)`** (тумблер выключен) — **НЕ подставляем** чужую
  минимальную цену («Tank от 789 900 ₽» для отсутствующего товара вводит в заблуждение). Это уже так в коде
  (`:400 return pr if pr and pr[0] else (0,0)`), НЕ фолбэк на `_min_offer_price`.
- **Общая/аудиторная группа** (`seg` не `Марки`/`Модели`, `ct0000` и т.п.) → **МИН цена оффера из фида**
  `_min_offer_price` (`:388-389`) — это и есть «общий» случай, тут минимум корректен.
  > ✅ **ИСПРАВЛЕНО (Семён 2026-07-09):** прежний текст «Фолбэк (нет цены марки) → `_min_offer_price`
  > ВСЕГДА» был НЕВЕРНЫМ пересказом. Код НЕ фолбэкает брендовую группу на минимум — он это РАЗЛИЧАЕт
  > по `seg`. Правило Семёна выполняется, багом это НЕ является.
- **Проверка (live):** `NO_ADPRICE_LIVE` (warn, `grid_content_verifier.py:208`) — фидовая группа без adPrice;
  добивается `adprice_repair` (докрутка, статус 🟡). ⚠️ на tp1 мутация объявления без `ad_price_payload`
  ЗАТИРАЕТ цену (см. 2.4 про видео-attach). Все tp1 post-create Grid update картинок/текстов должны
  идти через RMW `_grid_update_adaptive_ads(..., campaign_ids=[cid])`, иначе full-replace может стереть
  `bannerPrice`/видео.
  > ✅ **ИСПРАВЛЕНО (2026-07-18):** прежняя формулировка не упоминала исключение **товарки-only**.
  > `bannerPrice` — поле ТОЛЬКО адаптивных текстовых объявлений; у ShoppingAd/ListingAd (смарт-баннер,
  > каталог) цена приходит **из фида**, поэтому код к ним неприменим. Код это учитывает:
  > `adaptive_images_read=True` и `adaptive_total==0` → `NO_ADPRICE_LIVE` НЕ выдаётся
  > (`grid_content_verifier.py:200-206`). Fail-safe: адаптивные не прочитаны → флаг не глушится.
- **Лимит:** `priceOld` показываем ТОЛЬКО если `old > current` (иначе Яндекс отвергает).

**Консистентность суммы платежа по АККАУНТУ (R2-4 (г) 2026-07-10):** `unify_utp_numbers`/`_coherent_payments`
сводят сумму «от N ₽/мес» ТОЛЬКО ВНУТРИ item (канон = первое-встреченное) → между объявлениями аккаунта
был разнобой («Платеж от 9 000» vs «Кредит от 12 000 ₽/мес»). Добавлен аккаунт-канон
`ai_content._account_pay_unify(login,…)`: первая валидная сумма 9-15к на проход воркера фиксируется и
применяется ко ВСЕМ объявлениям (той же атомарной заменой `text_gen._apply_payment_amount`). Флаг
`DIRECT_PAY_CANON_ACCOUNT` (дефолт **ON**). Wired в orchestrator (tp1/tp2/tp5) + master_product (tp6/tp7).
🟡 live не проверено.

**«Текст по умолчанию» ShoppingAd/динамики (Д5/Д6 2026-07-10, прогон af4bd7bd5a52):** ОДНА константа
`create_set_assets.SHOPPING_DEFAULT_TEXT` на ВСЕ каталожные/товарные группы tp3/tp5.
**⚠️ Кредитная лексика ЗАПРЕЩЕНА** — ShoppingAd/ListingAd (товарные объявления) не принимают
«кредит/автокредит/взнос/платёж/одобрение банка»; текст должен быть про ПРОДУКТ.
Текущее значение: `«Новые автомобили в наличии. Большой выбор. Скидки до 45% при покупке. Тест-драйв.»`
(81 симв, без кредитной лексики). Используется в:
- `create_set_gallery.py` (cookie-путь tp3/tp5) с fail-safe на `texts[0]`;
- `create_set_repairing.py:_repair_shopping_content_context` (repair-путь) с fail-safe на `texts[0]`.
✅ **Охвачено (2026-07-10):** `create_set_feed_builders.py:_create_tp5_single` тоже использует
`SHOPPING_DEFAULT_TEXT as _SDT` (строки 712–715, `texts=[_SDT]`) и ставит `_default_text = _SDT`
(строка 739) — БЕЗ кредитной лексики. Прежний hardcoded fallback «…по кредиту…» убран (fail-safe
на случай недоступного импорта — тот же текст без кредита); правка в infra-зоне больше НЕ требуется.
Live-код `EMPTY_DEFAULT_TEXT_LIVE` (§1.b.3) чинит пустой; недозаполнение (мало символов) чинит только
будущий прогон (`_campaign_default_text_repair` — STUB). 🟡 live не проверено.

**UAC (tp6/tp7) — бюджет/pricing/период** (`uac_verifier.py`, все → пересоздание при нарушении):
- `pricing`: `cpc`→**PER_CLICK**, `cpa`→**PER_CONVERSION**; иначе `UAC_PRICING_MISMATCH` (error, :79/:83).
- `week_limit > 0` (недельный бюджет), иначе `UAC_BUDGET_MISSING` (:87); период = **week**, иначе
  `UAC_LIMIT_PERIOD_MISMATCH` (:92).

---

## 3. По типам кампаний (tp1–tp11) — основной чек-лист

> Единый чек-лист «что должно стоять и быть загружено» для КАЖДОГО tp. Сведено из глобальных
> инвариантов + реального кода (`create_set_orchestrator.py`, `create_set_feed_builders.py`,
> `create_set_master_product.py`, `create_set_finalize.py`, `grid_finalize.py`, `grid_create.py`).
> Тип диспетчеризуется по `it.type` → `_TYPE_TO_TP` / post-ветке (`create_set_orchestrator.py`):
> `tp1_rsy`→tp1 · `search_test`→tp2 · `rsya_gallery`→tp3 · `search_dynamic`→tp4 · `search_gallery`→tp5 ·
> tp6/tp7 = UAC (Мастер/Товарка, отдельная ветка по куке) · `post_tp8/post_tp9/post_tp10` = Посевы.
>
> **Легенда типов** — источник истины НЕ `CODER.md` (там лишь примеры), а справочник
> `public.local_gsheet_naming` (Victory, `type='tp'`), сверено SQL 2026-07-09:
> `tp1`=РСЯ · `tp2`=Поиск · `tp3`=Товарная галерея (legacy, автотаргет НЕ трогаем) ·
> `tp4`=Поиск+Динамика · `tp5`=Поиск+Динамика+Товарная галерея (ЕПК) · `tp6`=Мастер кампаний ·
> `tp7`=Товарка (мастер/фид+каталог) · `tp8`=Telegram · **`tp9`=Max** · **`tp10`=Telegram + Max** ·
> `tp11`=Connected TV.
>
> ⚠️ **ОБНОВЛЕНО (2026-07-22):** прежняя запись «tp8/tp9/tp10 сервис не создаёт» устарела.
> `tp8/tp9/tp10` создаются отдельной post-веткой `create_set_tp8_10.py`; `tp11` остаётся только
> в кодере и вне скоупа автоматизации.
>
> `- [ ]` = проверяемый пункт готовности. 🟡 = требует подтверждения на живом прогоне (в коде не доказуемо).

### Глобальные инварианты — куда падают (API-поля, для справки)

Источник — `CAMPAIGN_INVARIANTS.md`. **Гейт метрики стоит на ШАГЕ ПЛАНА** (2026-07-27): `/set_plan`
зовёт ту же `create_set_metrika.prepare_metrika` и отдаёт `metrika_alert {needed,error,counter_id,
goal_id,metrika_note}` (`create_set_plan.py:375 _metrika_alert_for`, ответ остаётся `200`); запуск
джобы отбивается по этому алерту в `routes_jobs.py:161-172` **до** `job_new` — осиротевшей задачи в
очереди не будет. Это покрывает и **API-путь мимо формы**: фронтовые гарды
(`static/direct/automation_create.js`) срабатывают только на клик в UI, а реальный инцидент
(`METRIKA_GOAL_MISSING_VIA_API_PATH`, джоба `7fc7af30fff1`) был именно программным запуском.
Нижняя страховка `prepare_metrika` в оркестраторе создания остаётся. Оба легальных исключения
сохранены: `via_cookie+no_cpa` → `needed=False`+note; счётчик без цели → доподтягивание
`goal_vse_formy`. Сбой Метрики/БД или непроведённые коллбэки → `needed=False` (план не блокируем),
причём непроведённые коллбэки пишут ВИДИМЫЙ WARNING `direct.plan` (раз на процесс).
Чек-лист этих правил — ниже в 3.0; здесь — реальные имена полей API (v5/v501/Grid vs UAC).

| Глоб. правило | Значение | tp1–tp5 (v5/v501/Grid) | tp6/tp7 (UAC) |
|---|---|---|---|
| **#1 Метрика + цель** | ВСЕГДА | `CounterIds:[id]` + `goal_id` в стратегии | `counters` + `goals` |
| **#2 UTM-метка** | ВСЕГДА | на **группах** — `AdGroups[].TrackingParams` (`_UTM_TEMPLATE_TP1`) | на **кампании** — `tracking_params` |
| **#3 Персонализация** | ВСЕГДА **ВЫКЛ** | `ALTERNATIVE_TEXTS_ENABLED=NO` / `isAlternativeTextsEnabled=False` | `alternative_texts_enabled=False` |
| **#4 Мониторинг сайта** | ВСЕГДА **ВКЛ** | `ENABLE_SITE_MONITORING=YES` / `hasSiteMonitoring=True` | (у UAC тумблера нет — упрощённый тип) |
| **#5 Расширенный гео** | ВСЕГДА **ВЫКЛ** | `ENABLE_AREA_OF_INTEREST_TARGETING=NO` / `hasExtendedGeoTargeting=False` | (у UAC тумблера нет) |
| **#6 «Директ помогает»** | ВСЕГДА **ВЫКЛ** | `isRecommendationsManagementEnabled=False` | `recommendations_management_enabled=False` + `price_recommendations_management_enabled=False` |
| **Карты/организации** | **ВЫКЛ** | `ENABLE_COMPANY_INFO=NO` / `enableCompanyInfo=False` | UAC: «Карты» OFF (live-проверка region+карты) |
| **Глоб. минус-слова** («отзывы» и т.п.) | на уровне **КАМПАНИИ** | `_enabled_minus_words` в deps (tp2/tp4/tp5) | UAC `minus_keywords` в payload (`campaign.py:1498`) — глоб. во всех режимах; минус-КС слепка — в ручном `keywords` (см. §3.6/§3.7) |

### 3.0 Общие инварианты — обязательны на ЛЮБОМ tp

- [ ] **Только ЧЕРНОВИК** (`launch=False`) — сервис никогда не публикует.
- [ ] Каждый текстовый/UAC tp = **ПАРА кампаний** `cpc` + `cpa` — **по умолчанию, НЕ безусловный инвариант**:
      управляется галочкой `no_cpa` (`create_set_plan.py:487-489`, `pays=['tcpa'] if no_cpa else ['tcpa','cpa']`):
      снята → пара `cpc`+`cpa`; активна (`no_cpa`) → остаётся ТОЛЬКО одна кампания (`cpc`).
      Create-runtime должен принимать оба ключа (`no_cpa` и UI/plan alias `n`); иначе plan может
      показать 0 CPA, а builder tp1/tp5 создаст скрытые CPA-пары.
- [ ] #1 Метрика + `goal_id` — проверяется **на шаге плана**: `/set_plan` → `metrika_alert.needed`,
      гейт очереди `routes_jobs.py:161-172` отбивает `create_set_async` с `400` ДО `job_new`
      (см. врезку выше). Детект-запрос без живого создания: `POST /direct/api/set_plan` без `goal_id`
      → `metrika_alert.needed == true`; тот же `metrika_alert` в `create_set_async` → `400` и **ноль**
      новых строк в `direct_automation_jobs`.
- [ ] #2 UTM-метка: **tp1–tp5** на ГРУППАХ (`TrackingParams`), **tp6/tp7** на КАМПАНИИ (`tracking_params`).
- [ ] #3 Персонализация (адаптивные тексты) **ВЫКЛ**.
- [ ] #4 Мониторинг сайта **ВКЛ** (tp1–tp5; у UAC тумблера нет).
- [ ] #5 Расширенный гео **ВЫКЛ** (tp1–tp5; у UAC нет).
- [ ] #6 «Директ помогает» **ВЫКЛ** (у UAC ещё `price_recommendations_management_enabled=False`).
- [ ] Карты / список организаций **ВЫКЛ**.
- [ ] Защита от дублей (`filter_new_campaign_names` — существующие имена пропускаются).
- [ ] Имя кампании/группы несёт **кодер** (не «Марка/Марки»); регион зашит в `r`-коде, в имени не пишется.
  - **Источник кодера** — справочник `public.local_gsheet_naming` (Victory, `type='tp'/'ct'/'ag'/…`), НЕ
    `CODER.md` (там лишь примеры). Отклонение от формы кодера ловится регэкспами в `verifier.py`:
    нет tp-префикса → `CODER_PREFIX_MISSING` (`_TP_RE`), не та форма → `CODER_SHAPE_SUSPICIOUS` (`_CODER_RE`).
  - **Источник региона (tp1–tp5)** — `local_gsheet_sites.city` → JOIN
    `local_gsheet_yandex_direct_id_location` (город→Область) → **geoid ОБЛАСТИ** через словарь Директа
    (`create_set_context.py:_account_ctx`, дефолт `225`=Россия; таргет — область, не город). `r`-код
    кодера — из `_resolve_region(city)`. Для tp6/tp7 (UAC) заполненность регионов проверяет
    `UAC_REGION_MISSING` (`uac_verifier.py:102`, `detail.regions≤0`).
- [ ] **Пост-настройка провалена → позиция `failed`, не `ok`.** Если после создания кампании провалился любой шаг (финализация, обновление `relevanceMatch.isActive`, привязка бюджета/мест/ассетов, применение промо/уточнений) — позиция обязана получить `failed`, а НЕ `ok`. Известные дыры (2026-07-27): (1) `create_set_finalize.py:222` — `UpdateCampaigns` шлётся голым `_post` без ретрая транзиентов (у РСЯ-близнеца стоит `post_idempotent`); сбой → tp2/tp4 остаётся без бюджета/мест/ассетов, позиция рапортует `ok`; инцидент 712885317. (2) ~~провал «Фазы 1.5» (пост-фактум установка `relevanceMatch.isActive`) уходит в `rep["errors"]`, а вызывающий смотрит `rep["error"]`~~ — **снято 2026-07-27**: Фаза 1.5 удалена целиком, `relevanceMatch` ставится атомарно при создании группы, а падение Grid `AddUnifiedAdGroups` возвращает `rep` без `adgroups` → вызывающий зовёт `_cleanup_partial` и сносит черновик (`ok=True` с неверным автотаргетом невозможен, `create_set_tp1_builders.py:356-360`). (3) `repair_auto.py:617-624` — сбой удаления UAC-черновика ставит `queued:False` всему recreate-пакету, исправные позиции не добиваются.

### 3.1 tp1 — РСЯ (текстовые объявления в сетях)

**Настройки/галочки:**
- [ ] Канал = **РСЯ**: `Network` ВКЛ, `Search` OFF (`_PLATFORMS_RSYA`, `_finalize_rsya`).
- [ ] Стратегия: `cpc` → **AVERAGE_CPA**, `cpa` → **PAY_FOR_CONVERSION** (GoalId + WeeklySpendLimit, микро-₽).
- [ ] Минус-площадки = **голый ХОСТ** (`_place_host` → `disabledPlaces`), из вкладки минус-площадок.
- [ ] **Автотаргет группы — ставим явно** (с 2026-07-27 `d44236d1`; прежняя формулировка «не трогаем,
      РСЯ без `relevanceMatch`» БОЛЬШЕ НЕ ВЕРНА). `relevanceMatch.isActive = bool(autotarget)` из флага
      кодера (`_aon_`/`_aoff_`) выставляется **атомарно при создании группы** через Grid
      `AddUnifiedAdGroups` (`gc.build_adgroup`, дефолтная ветка: при `autotarget=True` — все 5 категорий
      + 3 бренда, при `False` — `isActive=False` с пустыми списками). Пост-патч `UpdateUnifiedAdGroups`
      по свежим группам — доказанный no-op, применять нельзя (см. 1.2a).
- [ ] **Ключи tp1 — Grid `AddKeywords` тем же клиентом**, что создавал группы (Фаза 2,
      `create_set_tp1_builders.py:362-379`), НЕ v5 `keywords.add`: смешанный транспорт даёт лаг
      репликации Grid→v5 и ключи-фантомы (LIVE=0). Псевдоключ `---autotargeting` не шлём — автотаргет
      живёт в `relevanceMatch`. Режимы: `aon` без `keep_keywords` → ключей нет (таргетинг =
      `relevanceMatch`); `aoff` → реальные ключи; `aon + keep_keywords` → «КС + Автотаргетинг».
      Группа «все фиды» (Фаза 4a, `Товарная галерея · <фид>`) идёт ТЕМ ЖЕ Grid-транспортом.

**Контент загружен:**
- [ ] **7 заголовков** + тексты (`_RA_TITLES_CAP=7`, `_fill_titles(n=7)`).
- [ ] Картинки по `ct`, **до 5 шт** (`_creative_images_for_ct`, `blueprint.py:6981`, `limit=5`). Папка
      выбирается `_image_ct_for_content` (`:6971`): общие/аудиторные `ct0000-ct0014` → маппятся на пул
      `ct0000`; кузова `ct0015-ct0018` и модельные/марочные — своя `ct`-папка. Порядок источников
      ЗАВИСИТ от типа группы:
      - **Общий/аудиторный `ct` (→ ct0000):** сначала общий Manual-пул `ct0000`, затем **добивка до 5 из
        слепка** (`read_slepok_images(ct0000)`), затем explicit-ассеты. — это и есть «общая база + добивка
        своими», о котором говорил Семён (2026-07-09) — для общих групп верно.
      - **Брендовый/модельный/кузовной `ct`:** сначала **своя `ct`-папка** (Manual `{ct}`), затем свой
        слепок `read_slepok_images(ct)`, затем **общий пул** (`read_any_slepok_images`/`read_images`) как
        добивка. Т.е. для брендовых групп порядок ОБРАТНЫЙ: своя папка первой, общий пул — фолбэк (НЕ
        «общая база + добивка своими»). Правило Семёна про «общую первой» на брендовые группы НЕ
        распространяется — код осознанно берёт свою `ct` вперёд.
- [ ] Быстрые ссылки = 8 (без смысловых дублей) + уточнения + промо.
- [ ] **adPrice** (`_group_ad_price`): брендовая группа → МИН цена марки; **марки нет в фиде → цена ПУСТАЯ
      `(0,0)`** (НЕ фолбэк на минимум); общая/`ct0000` группа → МИН цена оффера `_min_offer_price`. См. §2.5.
- [ ] **Видео** (BAIC/Belgee/Haval/Москвич): добивается **ПОСЛЕ создания** — `_tp1_video_ads`
      (`upload_video_creative` → `creativeIds` в `UpdateAdaptiveTextAds`).

**Нюансы tp1:**
- Состав: **TextAd only** → кодер группы `ct001_ag011`. С фидом (`with_shopping=True`): **TextAd + ListingAd +
  ShoppingAd** → `ct010_ag011`.
  - **Эвристика «Комби+Фид» vs чистый «Фид»** (`create_set_plan.py`, COMBI_FID_TEXTAD_MISSING 2026-07-21):
    «Комби+Фид» в имени позиции (`"комби" in name`) → `products_only=False`, `tp1_catalog=True` →
    **TextAd + ShoppingAd + ListingAd**. Чистые «Фид» / «Смарт-Баннер» БЕЗ «комби» → `products_only=True` →
    **только ShoppingAd + ListingAd** (без TextAd). `_is_combi = "комби" in _low_cn`;
    `_prod_only = (not _is_combi) and _has_feed_or_smart`.
- Товарка для tp1 — только **catalog-only** фиды (`_account_model_feeds(catalog_only=True)`), не все фиды аккаунта.
- ⚠️ Видео-attach несёт `meta.ad_price_payload`: мутация объявления без `creativeIds`/`adPrice` **ЗАТИРАЕТ** видео и цену.

### 3.2 tp2 — Поиск

**Настройки/галочки:**
- [ ] Канал = **Поиск**: `Search` ВКЛ, `Network` OFF.
- [ ] Стратегия `cpc`=AVERAGE_CPA / `cpa`=PAY_FOR_CONVERSION.
- [ ] Ключи + **автотаргет `search_tp2`**: «Целевые запросы» = ТОЛЬКО `EXACT_V2_MARK`,
      «Без упоминания бренда» = `WITHOUT_BRAND` (`grid_create.py:461-470`).
- [ ] `placementTypes = ["SEARCH_PAGE"]`.
- [ ] Динамич. места (`isOrganicSearchEnabled`) = **ВЫКЛ** (`organic=False`).
- [ ] Глоб. минус-слова на уровне **КАМПАНИИ** (Grid shared-set `libraryMinusKeywordsIds`,
      источник `_enabled_minus_words`; v5 `NegativeKeywordSharedSetIds` не дублируем).

**Контент загружен:**
- [ ] **7 заголовков** + тексты.
- [ ] **Ключи в КАЖДОЙ группе** (не пусто — иначе код `NO_KEYWORDS_LIVE`).
- [ ] Быстрые ссылки = 8 + уточнения + промо.

**Нюансы tp2:** состав = TextAd only (`ct001_ag011`). Картинок/видео/adPrice на поисковом TextAd нет.
При `only_gks/only_cts` создание обязано строить только выбранные группы структуры; не-autotarget
группа без ключей после фильтра не создаётся. Если Direct схлопывает дубли ключей между группами,
наружный `build.groups` считается по фактическому read-back `adgroups`, а исходные попытки пишутся
в `groups_built`.

### 3.3 tp3 — Товарная галерея (`rsya_gallery`, legacy)

**Настройки/галочки:**
- [ ] ЕПК, канал = **Поиск** (`Network` OFF, `Search` ON), товарная **по ВСЕМУ фиду**; фид обязателен.
- [ ] Стратегия `cpc`=AVERAGE_CPA (`search_cpa`) / `cpa`=PAY_FOR_CONVERSION (`search_payconv`), пара cpc+cpa.
- [ ] **Места показа = «Ручная настройка»** `placementTypes=["ADV_GALLERY"]` (ТОЛЬКО товарная галерея
      на поиске, без `SEARCH_PAGE`). Выставляет Grid-finalize (`_finalize_search_via_grid`,
      `placement_types=["ADV_GALLERY"]`); при создании шлётся `null`.
- [ ] `isOrganicSearchEnabled=True` (из `PLATFORMS_SEARCH.organic`).
- [ ] Автотаргет группы = **только** `EXACT_V2_MARK` + `WITHOUT_BRAND` (тот же профиль `search_tp2`,
      что и tp2/tp4/tp5) — не полный список relevance-категорий и не «не трогаем».

**Контент загружен:**
- [ ] **ShoppingAd + ListingAd** (страницы каталога), одна группа на фид (без per-brand разбивки).
- [ ] Одна кампания **на фид** (FAN-OUT как tp5).
- [ ] Уточнения / быстрые ссылки / промо — через Grid-finalize (`_finalize_search_via_grid`).

**Нюансы tp3:** legacy-тип, редко в наборах; кодер группы `ct009_ag001`. Функция-диспетчер
в `_TYPE_TO_TP` называется `rsya_gallery` (историческое имя не меняем — там только роутинг).

### 3.4 tp4 — Поиск + Динамика

Идентичен **tp2**, отличие ровно в одном:
- [ ] Динамич. места (`isOrganicSearchEnabled`) = **ВКЛ** (`organic=True`).
- [ ] `placementTypes = ["SEARCH_PAGE"]`, автотаргет `search_tp2` (как tp2).
- [ ] Остальное (7 заголовков, ключи в группах, минус-слова на кампании, TextAd only) — как tp2.
- «Динамика» = динамические места на ПОИСКЕ, **БЕЗ товарного фида** и без отдельного типа объявлений.

### 3.5 tp5 — ЕПК Поиск + Товарная галерея (`search_gallery`)

**Настройки/галочки:**
- [ ] `TEXT_CAMPAIGN`, канал = **Поиск** (`Network` OFF, `yandexMaps` OFF, список организаций OFF).
- [ ] Стратегия `cpc`(`pay="tcpa"`)=AVERAGE_CPA / `cpa`(`pay="cpa"`)=PAY_FOR_CONVERSION.
- [ ] Ключи + автотаргет `search_tp2` (`EXACT_V2_MARK` + `WITHOUT_BRAND`).
- [ ] **Места показа = «Ручная настройка»** `placementTypes=null` + platforms
      `gallery/search/organic=true`, `network/yandexMaps/serpGeoWizard=false`.
      Audit-код `PLACEMENTS_WRONG` флагает любой непустой `placementTypes` или включённую РСЯ.
      🟡 следующее live-чтение `placementTypes` рекомендуется после code-review fix 2026-07-24.
- [ ] Динамич. места `isOrganicSearchEnabled = True` (`organic=True`).
- [ ] **adPrice на фидовых группах**.
- [ ] Глоб. минус-слова на кампании (как tp2/tp4).

**Контент загружен:**
- [ ] **TextAd + ListingAd + ShoppingAd** («Т+Л+ТОВ», комбинированный) → кодер группы `ct010_ag011`
      (ИСПРАВЛЕНО 2026-07-09: прежняя запись «ct009_ag001, БЕЗ TextAd, Семён 2026-07-07» — откачена в
      коде как ошибочное промежуточное решение, `create_set_tp1_builders.py:46-51`; Семён подтвердил
      2026-07-09 — tp5 снова комбинированный, это финал).
- [ ] Автотаргет группы = **`aon`** ВСЕГДА для tp5 (независимо от autotarget-флага бренд-группы) —
      ключи/relevanceMatch ставятся отдельно (`create_set_tp1_builders.py:47-51`).
      ⛔ **Это НЕ баг и «чинить» его нельзя** (решение Семёна 2026-07-27): в поисковой кампании
      Директа автотаргет выключить невозможно в принципе, поэтому `_aon_` в имени tp5-группы
      корректен ВСЕГДА, а живое `relevanceMatch.isActive=True` у группы планового `aoff` — норма.
      Плановый autotarget-флаг у tp5 значит лишь «бренд-ключи вместо чистого автотаргетинга» и
      управляет только Фазой 2 (ключи); профиль `search_tp2` (`EXACT_V2_MARK`+`WITHOUT_BRAND`)
      применяется безусловно. Прежнее `search_tp2 if autotarget` давало дефолтные категории и было
      источником 4 живых `WRONG_AUTOTARGET` на tp5 `aoff`.
- [ ] **7 заголовков** + тексты — как tp1 (TextAd снова есть).
- [ ] Ключи в группах (84–149/группа), одна группа **на бренд** + фильтр по коллекции (per-бренд `feed_models`).
- [ ] Быстрые ссылки — на объявлении; уточнения — через **наследуемые Grid-callouts**
      (ShoppingAd уточнения напрямую не принимает — API отвергает; TextAd в той же группе быстрые
      ссылки/уточнения получает как обычно).
- [ ] Кампания на **КАЖДЫЙ разрешённый фид** (allow-list `direct_global_feed_rules`); имя несёт название фида.

**Нюансы tp5:** состав группы = TextAd+ListingAd+ShoppingAd (как tp1 `with_shopping=True`), НЕ «ShoppingAd
без TextAd». ✅ **D2 2026-07-09:** tp5-ветка `audit_campaign` теперь вызывает `_audit_tp1_adaptive`
(BUTTON_MISSING+SHORT_TITLES, `groups=None` → без VIDEO/IMAGE) и `_audit_brand_not_first` — те же
контент-аудиты, что tp1/tp2/tp4. ShoppingAd/ListingAd (без titles) при этом не флагаются
(`_audit_tp1_adaptive` режет заголовки только у `GdAdaptiveTextAd`), tp5-специфика ShoppingAd сохранена.

### 3.6 tp6 — Мастер кампаний (UAC/МК)

**Настройки/галочки:**
- [ ] UAC-кампания (`create_master_campaign`, `campaign_type="master"`) по КУКЕ.
- [ ] Пара `cpc`(`PER_CLICK`) + `cpa`(`PER_CONVERSION`) — поле `pricing` (по умолчанию; при галочке `no_cpa` — только `cpc`, см. §3.0).
- [ ] Метрика `counters` + `goals`; UTM на КАМПАНИИ (`tracking_params`).
- [ ] Персонализация `alternative_texts_enabled=False`; «Директ помогает» + price-rec **ВЫКЛ**; Карты OFF.
- [ ] Недельный бюджет `week_limit>0`; регионы заполнены.
- [ ] **Имя кампании** = `{базовое имя} - {метка таргетинга}` (тип оплаты CPC/CPA/CRM **не пишем**).
  **Базовое имя = `camp_names[0]` из слепка** (фолбэк — `item.t`/`label`, если `camp_names` пуст или
  после очистки targeting-хвоста даёт пустую строку). Метка вычисляется из ФАКТИЧЕСКОГО контента
  позиции (ключи/аудитории), **НЕ из `item.t`** (был источник протухших меток — ERRORS_JOURNAL
  `TP67_TARGETING_LABEL_DRIFT` 2026-07-20): ключи+аудитории → `КС+аудитории`; только аудитории →
  `аудитории`; только ключи → `КС`; ничего → `автотаргетинг`. Метка добавляется к базовому имени
  ОТДЕЛЬНО — `item.t` источником таргетинга не является. Примеры: «МК - Кредит - автотаргетинг», «МК - Кредит - КС».
  `/slepki` UI и XLSX-экспорт обязаны делать тот же пересчёт и для `camp_names[0]`, включая fallback
  `tp67_real_keywords.json`: старая строка `МК - Nissan - Автотаргетинг` при 183 реальных ключах
  отображается как `МК - Nissan - КС` (фикс 2026-07-23).
  Если внутри одного слепка+типа сайта несколько tp6/tp7-позиций после такого пересчёта дают одно
  фактическое имя кампании, в структуре должна остаться **одна** позиция с union-списком
  `merged_gks`; создание, `/slepki` карточка ключей и XLSX читают ключи/минуса по всем этим `gk`.
  Несколько видимых строк с одинаковым именем, но разными ключами — дефект структуры, а не разные
  кампании. Runtime-защита: `_slepok_struct_groups()` дополнительно схлопывает одинаковые видимые
  tp6/tp7-позиции в памяти, чтобы новый прогон не создал дубли даже если JSON снова пришёл с такими строками.
  (ERRORS_JOURNAL: CAMP_NAMES_CROSS_TP_LEAK 2026-07-20; TP67_TARGETING_LABEL_DRIFT 2026-07-20/2026-07-23; коммиты 447eff4 / 09830ae / 0ddbdde / DROPPED_CAT)
- [ ] **Кодер позиции** (`gc` в структуре слепка): `{сегмент_ct}_aon_n000_r0000_ct001_ag011_g00` (всегда `aon`; `ct001`=МК). Для брендового `camp_names[0]` реальный `ct` берётся из очищенного имени (`МК - Chery - КС` → `Chery` → `ct0044`); `gc=ct0000` допустим только как fallback для реально общих позиций.
**Режимы таргетинга: автотаргет vs ручная аудитория** (сверено по коду 2026-07-09,
`create_set_master_product.py:82-127,500-518` + `campaign.py:1497-1507`; `targeting_mode` ∈
`autotarget`/`keywords`/`audience`, из `it.targeting_mode` или `_tp67_targeting_mode`, при пустых
ключах/аудиториях слепка — авто-fallback на `autotarget`):

| Режим | keywords | audiences | relevance-категории | Возраст (socdem) | minus-words |
|---|---|---|---|---|---|
| **Автотаргет** (полный) | — (пусто) | — (пусто) | `_TP67_OPTIMAL_CATEGORIES` («Подобрать оптимальную») | весь socdem, без исключений | **`[]` — НЕ шлём** (иначе флип «Аудитория» → «Настроить вручную», см. ниже) |
| **Ручная — КС** (`keywords`) | `_tp67_keywords_for` слепка | — | `_TP67_RELEVANCE_CATEGORIES` | **25+** (брекет 18-24 = **-100%**) | глобальные **+ минус-КС слепка** `it_minus_keywords` |
| **Ручная — аудитория/интересы** (`audience`) | — | `_audience_objects` слепка | `_TP67_RELEVANCE_CATEGORIES` | **25+** (брекет 18-24 = **-100%**) | глобальные `_enabled_minus_words` |

- ⚠️ **Минус-слова на tp6 — ИСПРАВЛЕНО 2026-07-30, прежняя запись была НЕВЕРНА.**
  Здесь было предписано слать `minus_keywords` **ВСЕГДА, включая автотаргет** — и код это исполнял.
  Живой кабинет (Семён): `porg-uy3huxcn`,
  `tp6_cpc_site_ct0000_aon_n000_r0002_ct001_ag001_g00 — МК - Общие запросы - Автотаргетинг`
  показывал блок «Аудитория» = **«Настроить вручную»**. UAC-detail этой РК: `keywords=null`,
  аудиторий нет, `minus_keywords=["отзывы"]` — минус-слова были ЕДИНСТВЕННЫМ ручным сигналом,
  и UAC из-за них флипал блок в ручной режим. Тот же механизм с 2026-07-10 уже был описан для
  товарки (tp7), но там оговорка «tp6-мастер не тронут (рендерит верно)» оказалась ложной.
  **Правило теперь одно для обоих типов:** решает РЕЖИМ позиции, а не тип кампании —
  автотаргет → `minus_keywords=[]`; ручные режимы (`keywords`/`audience`) → глобальные
  `_enabled_minus_words`, а минус-КС слепка `it_minus_keywords` — только в `keywords`.
  Цена решения названа явно: у автотаргет-кампаний tp6 больше НЕТ глобального «отзывы» —
  это сознательный размен ради полного автотаргета (требование Семёна 2026-07-30).
- ✅ **Возраст-ограничение (обновлено 2026-07-21, решение Семёна):** tp6 ручной
  (КС/аудитория) исключает ТОЛЬКО брекет 18-24 → реальный таргетинг = **25+**;
  автотаргет — без исключений (весь socdem), tp7 — `age_18` (возраст не настраивается). Код:
  `age_lower=("age_18" if (targeting_mode=="autotarget" or is_product) else "age_25")`
  (`create_set_master_product.py`). `age_25` — та же enum-семья Яндекс-socdem,
  что `age_18`/`age_35` (границы брекетов 18/25/35/45/55), поле-порог
  `socdem.age_lower` (непрерывный диапазон до `age_inf`, `campaign.py:1084/1505`). Прод-путь (не под
  флагом); автотаргет-режим tp6 и tp7 не тронуты. Ранее было age_35 (35+, 2026-07-09 → 2026-07-21,
  исключало ОБА брекета 18-24 И 25-34). ⚠️ live не проверено (прогон Семёна).

**Контент загружен:**
- [ ] **5 заголовков** + **3 текста** (fallback добивает до 5/3, `create_set_master_product.py`).
- [ ] Заголовки плотные (≥48/56): код `SHORT_TITLES` **регенерирует короткие заголовки через LLM**
      (`content_quality.regen_titles`, `campaign_spec_audit.py:fix_short_titles`). Суффиксами больше НЕ
      добиваем; не поправилось за 4 попытки → терминальный `SHORT_TITLES_UNFIXABLE`.
- [ ] Картинки (`image_limit=5`) + видео → **`content_ids`** (`upload_image_file` / `upload_video_file`).
- [ ] Быстрые ссылки; ключи/аудитории/интересы + минус-слова по режиму таргетинга (таблица выше).
  ⚠️ **Лимит интересов (аудитории) = 30** суммарно (не 100): блок «Интересы и поисковые запросы»
  в кабинете Директа принимает максимум 30 интересов. Передавать более 30 нельзя — Яндекс отвергает
  payload. (коммит `7d7654d` 2026-07-21)

**Нюансы tp6:** кодер **целиком на КАМПАНИИ** (групп нет). Тумблеров мониторинга/расш.гео у UAC нет.
🟡 live-проверка МК-черновика на #4/#5 (нечастый кейс).

**Пост-аудит tp6 (checklist):** `SHORT_TITLES` (заголовки ≤47), `UAC_MEDIA_MISSING` (нет медиа вообще),
**`UAC_VIDEO_MISSING`** (D3-UAC 2026-07-09: видео-марка BAIC/Belgee/Haval/Москвич с картинками, но
БЕЗ видео при непустом пуле → recreate с довложением видео; fail-safe: не видео-марка / медиа не
прочитано / пул пуст → не флагаем). Create-side: `videos_for_ct(login, ct, brand_hint=c_brand)` —
brand_hint прокинут (иначе «Марки»-ct давал пустой видео-пул).

### 3.7 tp7 — Товарка (UAC)

**Настройки/галочки:**
- [ ] UAC-товарка (`campaign_type="product"`) по КУКЕ; **фид обязателен**.
- [ ] Пара `cpc` + `cpa` (по умолчанию; при галочке `no_cpa` — только `cpc`, см. §3.0); метрика `counters`+`goals`; UTM на кампании.
- [ ] Персонализация / «Директ помогает» **ВЫКЛ**; Карты OFF; `week_limit>0`; регионы заполнены.
- [ ] Кампания на **КАЖДЫЙ разрешённый фид** (allow-list `direct_global_feed_rules`).
- [ ] **Имя кампании** = `{базовое имя} - {метка таргетинга}` (тип оплаты CPC/CPA/CRM **не пишем**).
  **Базовое имя = `camp_names[0]` из слепка** (фолбэк — `item.t`/`label`, если `camp_names` пуст).
  Метка = из ФАКТИЧЕСКОГО контента позиции (как tp6; `item.t` — не источник). Тот же механизм,
  что и tp6 (ERRORS_JOURNAL: TP67_TARGETING_LABEL_DRIFT / DROPPED_CAT 2026-07-20/21).
  Примеры: «Товарная - Baic - автотаргетинг», «ТК - КС», «ТК - аудитории».
- [ ] **Кодер позиции** (`gc` в структуре слепка): `{сегмент_ct}_aon_n000_r0000_ct010_ag001_g00` (всегда `aon`; `ct010`=ТК).

**Режимы таргетинга: автотаргет vs ручная аудитория** (сверено 2026-07-09, тот же контур
`create_set_master_product.py`, `is_product=True`):

| Режим | keywords | audiences | Возраст | minus-words |
|---|---|---|---|---|
| **Автотаргет** | — | — | не применимо | **`[]` — НЕ шлём** (иначе флип «Аудитория» в ручной режим) |
| **Ручная — КС** | `_tp67_keywords_for` | — | не применимо | глобальные **+ минус-КС слепка** |
| **Ручная — аудитория/интересы** | — | `_audience_objects` | не применимо | глобальные |

- ✅ **tp7 — возрастной настройки НЕТ вообще (Семён 2026-07-09):** в товарке (`campaign_type="product"`)
  такого UI/поля не существует — код шлёт `age_lower="age_18"` в payload (`:518`) технически всегда
  (`autotarget or is_product`), но это не «возрастное ограничение tp7», а просто побочный артефакт
  общей функции-сборщика с tp6. Не документировать как настройку tp7.
- ✅ **Минус-слова на tp7 (уточнено 2026-07-10, DEFECT 3):** в **ручных** режимах (`keywords`/`audience`) —
  глоб. `_enabled_minus_words` (минус-КС слепка — только `keywords`). **В автотаргете минус-слова НЕ шлём —
  `minus_keywords=[]`** (`create_set_master_product.py:636-637`): при пустых keywords/audiences единственный
  ручной сигнал в payload флипал блок «Аудитория» товарки в «Настроить вручную» → шлём пустой, чтобы UAC
  остался «Подобрать оптимальную». НЕ путать с фид-минус-марками (`it_ff`/`it_lff`,
  «Нюансы» ниже) — это РАЗНЫЕ механизмы (минус-слова = текстовый таргетинг, фид-фильтры = отбор товаров).

**Контент загружен:**
- [ ] **5 заголовков** + **3 текста** (`SHORT_TITLES` авто-добивка, как tp6).
- [ ] Видео → **`content_ids`**. Пост-аудит `UAC_VIDEO_MISSING` (D3-UAC 2026-07-09): видео-марка
      без видео → recreate с довложением (как tp6, тот же `_audit_uac_video_missing`).
- [ ] Каталог `ct0000` подхватывает страницы (не 0) — `it_lff=[]`.

**Нюансы tp7 (feed-фильтры — positive-only!):** сверено по коду 2026-07-24
(`create_set_master_product.py` + `create_set_feeds.py`):
- **«Страницы каталога» (ListingAd, `listings_feed_filters` = `it_lff`)** для `ct0000` → **`it_lff=[]`**
  (весь каталог, БЕЗ минус-марок — иначе глобальные минус-марки обнуляли выдачу каталога = 0 страниц).
  Брендовый/модельный `ct` с найденной коллекцией → **точный позитив по `collectionId`**:
  марка = бренд-уровень (`mark_*`, например Haval → `mark_6`), модель = `model_*`.
  При отклонении UAC → ретрай без `it_lff`.
- **«Товарка» (ShoppingAd, `feed_filters` = `it_ff`)** → глобальные минус-марки/модели **НЕ
  применяются**. Фильтр ставится только если `ct` кампании содержит реальную марку или модель:
  марочная tp7 → positive по полю марки (`vendor`/`mark_id`), модельная tp7 → positive по модели
  (`model`/`folder_id`). `collectionId` в товарный `feed_filters` НЕ добавлять: UAC отклоняет
  это как `INVALID_OPERATOR`; `collectionId` допустим только в `listings_feed_filters`.
  `ct0000`/общая tp7 идёт без `feed_filters`.
- **URL tp7 Марки/Модели** обязан вести на соответствующую посадочную даже при `sq=kviz`:
  Марки → страница марки (`/auto/haval`), Модели → точный URL модели из фида или формульный
  `/auto/{brand}/{model}`; `/quiz` не используется в generated Direct объявлениях и кнопках.
  не на голый домен и не на первую/случайную модель бренда.
- ⚠️ Прежнее «глобальные минус-марки применяются ко всей tp7-товарке» **устарело**: для БУ и
  марочных/модельных кампаний это давало широкий или отрицательный фильтр вместо точного positive.

### 3.8 tp8 / tp9 / tp10 — Посевы; tp11 — вне скоупа

Сверено SQL по `public.local_gsheet_naming` (`type='tp'`, Victory) и по коду 2026-07-22.
`tp8/tp9/tp10` теперь создаются через отдельный Grid-only engine; `tp11` только определён в кодере.

| tp | Название в кодере | Статус в сервисе |
|---|---|---|
| `tp8` | Telegram | создаётся как `post_tp8`, Grid `GdPostCampaign`; platform `telegram=true`, `maxMessenger=false` |
| `tp9` | Max | создаётся как `post_tp9`; platform `telegram=false`, `maxMessenger=true` |
| `tp10` | Telegram + Max | создаётся как `post_tp10`; оба platform-флага включены |
| `tp11` | Connected TV | нет в `_TYPE_TO_TP`, нет UAC-ветки — не создаётся |

DoD для Посевов:
- структура create-tab должна строиться из `direct/slepki/posevy.json`, как `/direct/automation/slepki`,
  без hardcode `1 кампания`;
- `AddCampaigns → AddPostAdGroups → AddPostAds`, результат остаётся черновиком;
- для каждой post-группы есть картинка/креатив и контент GdPostAd;
- частичный ответ Grid не считается успехом: `AddPostAdGroups` должен вернуть все planned-группы, а
  `AddPostAds` — по одному объявлению на каждую post-группу (`groups == ads == planned_n_groups`);
  недобор фиксируется как `partial` failure и/или `BUILD_LIVE_UNDERCOUNT`, а не зелёный result;
- `button.href` / post `href` соответствует уровню кампании: марочная post-кампания (`Марки`,
  однословный `brand_label` из feed-map, например `Haval`) ведёт на страницу марки
  (`https://domain/auto/haval`), а модельная (`Модели`, например `Haval M6`) — на точный URL модели
  из фида (`/auto/haval/m6/...`). Марочная кампания НЕ должна получать первый URL модели бренда;
- body GdPostAd не должен содержать домен, URL или слово «сайт»; переход остаётся только в `button.href`;
- body GdPostAd для Посевов должен заканчиваться строкой `Подробности по телефону: +<digits>`,
  если на посадочной странице найден телефон: сначала `tel:`, затем видимый текст страницы
  (`+7 (...) ...`/`8 (...) ...`). Телефон берётся с домена текущего `button.href`, без хардкода
  по аккаунту/слепку;
- body должен использовать лимит формата: целевой диапазон `POST_BODY_MAX - 30 .. POST_BODY_MAX`
  символов после финальной нормализации. Если остаётся 60-100+ свободных символов, это дефект
  композиции/добивки, кроме случаев когда цельный безопасный абзац уже не помещается;
- после телефонной строки не допускается никакой текст: добивка лимита, УТП, трейд-ин/подарки и
  уточнения должны вставляться выше CTA/телефона. Пустые секции вроде `В наличии:` без списка
  удаляются, повторные УТП одного типа (`КАСКО`, `трейд-ин`, шины, первый взнос, одобрение) не
  дублируются отдельными строками;
- марки/модели и ключевые УТП/бонусы в body выделяются штатной разметкой `:b:...:bb:`;
  italic-wrapper `:i:...:ii:` для всего поста не используется, потому live read/edit path Grid
  нестабилен на смешанной italic+bold разметке; перед `AddPostAds` разметка нормализуется,
  чтобы не было `INVALID_MARKUP`;
- в живом тексте не допускаются голые/обрезанные маркеры разметки (`i:`, `b:`, `s:` вместо
  `:i:`, `:b:`, `:s:`), склейки вида `на:b:Tenet` и хвосты, обрезанные триммингом перед телефоном
  (`перезвоним в`, `Не упустите`, `Оставьте заявку...` без завершённой мысли);
- body должен использовать доступный лимит формата осмысленно: если остаётся большой запас, генератор
  добавляет нейтральный полезный абзац до CTA/телефона, без домена и без новых неподтверждённых
  обещаний; целевой остаток — не больше ~30 символов, если его можно закрыть целой фразой без обрезки;
- `g`-сегмент в кодере post-кампании/post-группы — это пол (`g00`=Все, `g01`=Мужчины,
  `g02`=Женщины), а не номер картинки/группы. Несколько групп одной post-кампании без gender-
  корректировки остаются `..._g00 — ... v1/v2/v3`;
- перед отправкой body проходит защиту от явных неточностей: тип сайта (новые/с пробегом), бренд,
  город, неподтверждённые гарантии и запрещённые формулировки;
- brand_label/марка в Посевах допускается только если она подтверждена кодером (`ct` → `ag_part1`) или
  видимым текстом сайта. Конкретные модели нельзя выдумывать: если нет подтверждения на сайте/в кодере,
  body использует общие формулировки `модельный ряд {brand}` / `автомобили {brand}` / `авто в наличии`;
- демографические корректировки из глобальных правил применяются в `bidModifierDemographics`;
  отрицательные значения ниже `-50%` режутся до `-50%` (лимит Grid для tp8–tp10), `pct=0` не отправляется;
  возраст `25–34` не добавляется «для примера» — только если он реально есть в правилах.

Live 2026-07-22 (`porg-uy3huxcn`, job `24864b8891d4`, тест ограничен 2 кампаниями): сайт
`autopark777.site` отдаёт `tel:+79999999991`; первые две `tp8` post-кампании созданы, но read-back
поймал дефекты старого sanitizer: телефон и bold были, однако body начинался с `i:`, часть CTA
обрезалась перед телефонной строкой, а после исправления italic+bold Grid `ads.rowset` мог отдавать
пустой HTTP 500. DoD-фикс: `_phone_from_site` устойчиво читает `tel:`, `_trim_post_body` режет по
предложениям/абзацам/словам и удаляет незавершённые CTA-хвосты, `_finalize_post_markup` оставляет
safe bold без общего italic-wrapper, `_expand_post_body_before_phone` добирает свободный лимит.

Live 2026-07-22 (`porg-uy3huxcn`, job `7edd79dd9835`, тест ограничен 2 кампаниями после v2):
созданы черновики `712986295` и `712986322`, `live_verification.status=pass`, 6/6 `GdPostAd`
прочитаны через Grid. В каждом body есть `Подробности по телефону: +79999999991`, есть
`:b:...:bb:` для моделей/УТП, нет домена/URL/слова «сайт», нет italic-wrapper, нет обрезков
`перезвоним в` / `Не упустите` перед телефоном. Свободный лимит body: 44 символа для мультибренда
и 2 символа для Tenet.

Live 2026-07-22 (`porg-xjxpfxby`, `tp810check_20260722_01`): первые две `tp8` post-кампании созданы
черновиками `712977960` и `712978055`; `live_verification.status=pass`, Grid read-back: 2 кампании,
6 post-групп, 6 объявлений, `bad_adgroup_names=0`, `bid_modifiers_present=true`.
Актуальный структурный кодер Посевов — `ct018_ag001_g00` как в `CODER.md` и `direct/slepki/posevy.json`;
демографические bid modifiers применяются отдельно и НЕ должны переписывать имя кампании/группы в `ag011`.

Live 2026-07-22 (3 завершённых job): `porg-pl6iavd5` `5662588a0358` = `done 42/42`,
`created=21`, `skipped_existing=21`, `failed=0`; `porg-xjxpfxby` `5450ecbffe7c` = `done 20/20`,
`created=20`, `failed=0`; `porg-rgwzgo57` `6a43a2150ed3` = `done 77/77`, `created=77`,
`failed=0`. По `body.items` пропущенных plan-items нет, deferred-create для этих parent jobs нет.
Остаточные live issues — не потеря структуры, а content gaps/финализация: `NO_IMAGES_LIVE`
(`porg-pl6iavd5` 14, `porg-xjxpfxby` 2, `porg-rgwzgo57` 15), один уже докрученный `NO_KEYWORDS_LIVE`
на `porg-xjxpfxby`, один `RSYA_NOT_FINALIZED` на `porg-rgwzgo57`.

---

## 4. Эксплуатация (скорость и надёжность)

| # | Критерий | Статус |
|---|---|---|
| 4.1 | Время создания приемлемое (НЕ 1+ час на 14 РК) | 🟡 (замер 59581fdd9f9d 2026-07-10: **41 мин** создание 14 РК + ~час хвост добивки [починен, см. 4.2]. Инфра уже оптимизирована: батч add, parallel-заливка картинок [воркеры 8→10 `DIRECT_IMG_UPLOAD_WORKERS`], units-probe раз-на-набор. **2026-07-27:** полный набор 26 items — **1939 с против базы 2677 с** (`3f56db987ab9` vs `69a140093e78`, −27.6 %); главный вклад дал реюз наборов быстрых ссылок по содержимому + батч (`75d64c0a`): `v501:sitelinks.add` **774 вызова / 444 с → 6 вызовов / 4.6 с**. **Главный оставшийся рычаг = LLM-генерация контента per-РК**) |
| 4.5 | **Наблюдаемость: пер-item пер-стадийные тайминги** — по любому прогону можно построить профиль wall-clock без инструментирования на ходу | ✅ (`stage_timing.py`, коммит `d666f3ba`) — см. врезку ниже |
| 4.6 | **Разбивка `has_issues` пишется на ВСЕХ терминальных статусах**, а не только на `done` | ✅ (`dc564106`; подтверждено на `3f56db987ab9` со `status=error`: `lv_errors=0`, `ver_errors=1`) — см. врезку ниже |
| 4.2 | Набор ДОЖИМАЕТСЯ — не зависает / не прерывается | 🟡 (R2-6 2026-07-10: «добивка держит родителя `running` ~час» ПОЧИНЕНА — dcr content_repair больше не absorb'ится в родителя, флаг `DIRECT_DCR_DETACH_PARENT`=ON: карточка → `done` сразу после создания+аудита, content_repair крутится демоном асинхронно. Реальные докрутки [recreate/UAC/finalize] не тронуты. Live не проверено) |
| 4.3 | Write-gate: **параллельность между агентствами**, сериализация только ВНУТРИ одного | ✅ (коммиты `c2c8b01` / `064b1d6` 2026-07-21) |
| 4.4 | Карточка job показывает время **текущего исполнения аккаунта**, а не общее ожидание в очереди | ✅ (2026-07-23: `started_at` сохраняется в `direct_automation_jobs`, list/recent jobs подмешивают `direct_agency_active.started_at`, `routes_jobs` считает `elapsed` для `running` от `started_at`; ожидание в очереди не должно увеличивать таймер исполнения. При ручной остановке job уже созданные черновики НЕ удаляются автоматически — удаление только отдельным действием.) |

> **4.3 Write-gate** — норма с 2026-07-21: очереди создания/копирования/контента пишут
> **параллельно для РАЗНЫХ агентств** (write-lock ключ = agency, не глобальный).
> Сериализация применяется ТОЛЬКО внутри одного агентства (`_CREATE_MAX_PER_AGENCY=1` +
> кросс-процессный `_agency_gate_claim`, UNIQUE по agency). Прежнее поведение —
> единый глобальный лок на весь процесс — устарело. Не путать с per-login гейтом
> `_claim_next_job` (закрывает кросс-процессный зазор внутри логина). Смотреть ОШИБОЧНЫМ
> признаком «зависание другого агентства» при работе нескольких параллельных наборов —
> нельзя: это по-проекту независимые потоки.

> **4.5 `STAGE_TIMING` — как снять профиль прогона** (`stage_timing.py`, 2026-07-27).
> Каждая стадия пишет ОДНУ строку в stdout воркера (journald), тело — валидный JSON:
> `STAGE_TIMING {"job","login","item","tp","type","ch","stage","ms","ok"}`. Контекст item'а
> живёт в `threading.local` (`set_item` в начале `_run_item`), поэтому глубокие транспортные
> стадии подхватывают `job/item/tp` сами. Покрыты **оба транспорта** — `v501:*`
> (`direct_v501_client.py:248`) и `grid:*` (`grid_create.py:144`, `grid_finalize.py:395`) — плюс
> item-уровневые `content_gen`, `wait_tp1_images`, `item_total`
> (`create_set_orchestrator.py:1087/1165/1358`). Замер НИКОГДА не меняет поведение: любая ошибка
> таймера проглатывается, исключение из блока пробрасывается, строка пишется с `"ok":false`.
> Снятие (⚠️ **`jq` на LXC101 НЕТ** — пример с `jq` в докстринге модуля неисполним; агрегировать
> питоном):
> ```
> journalctl -u direct-create-worker --since '<старт>' -o cat \
>   | grep '^STAGE_TIMING ' | sed 's/^STAGE_TIMING //'
> ```
> → сгруппировать по `stage` (`n`, `sum(ms)`) любым python-однострочником.
> ⚠️ **«Остаток» = `item_total` − Σ стадий — это ВСЁ неинструментированное** (LLM-генерация вне
> `content_gen`, работа с картинками, паузы анти-блока, ожидания). Читать его как «время
> картинок» — ошибка интерпретации.

> **4.6 `has_issues` на терминальных статусах** (`dc564106`, `annotate_job_issues`,
> `queue_server.py:2179-2190`). Разбивка считается на `done` / `error` / `cancelled` — раньше
> только на `done`, из-за чего при `failed>0` (статус уходит в `error` — а это ровно тот случай,
> где дефекты вероятнее всего) система молчала: числа были только в `live_verification.summary`
> и `verification.summary`. Статус от разбивки по-прежнему НЕ зависит. Верификации не было
> (нет `summary` ни там, ни там) → вместо лживых нулей пишется `result["has_issues_unknown"]=true`.
> **`interrupted` не покрыт СОЗНАТЕЛЬНО:** он ставится SQL-апдейтом recover/watchdog
> (`queue_server.py:244,2402`, `job_repository.py:348`) мимо `result`, верификация там не
> отрабатывала вовсе — нули были бы враньём. UI (`automation_jobs.js`) показывает разбивку и в
> карточке `error`, плюс явную строку «верификация не выполнялась».

---

## 5. DoD — Слепок (готовность нового директолога к созданию)

| # | Критерий | Как проверить |
|---|---|---|
| 5.1 | Появляется + выбирается в селекторе /direct/automation | `/api/ai/agents` содержит слепок |
| 5.1a | Активная структура хранится в per-slepok JSON, без монолита | в `direct/slepki/` нет активного `*slepki_structure*.json`; active part-файлы без ведущего `_` считаются рабочими слепками. Проверка 2026-07-22: монолита нет; `gordeeva_v1.json` — валидный active слепок, `slepki_store.assemble()` должен видеть 19 слепков и `gordeeva_v1=True` |
| 5.2 | Есть ключи в паке для **КАЖДОГО ct** (вкл. модельные) — не пусто и не seed-only | `read_keywords(segment,tp,ct,slepok)` для ВСЕХ ct слепка, вкл. tp2 И tp5. **R2-6 2026-07-10:** ОДИН отсутствующий/тонкий `keywords/{slepok}.txt` ломает свою кампанию (не «есть на уровне марки → значит ок»). Пример: scherbakova/ct0032 Changan CS55 — `tp2` файла НЕ было (0 ключей), `tp5` = 2 seed-строки. Ориентир объёма у здоровых ct: 66–788. Корень «ключи пропали» = ЭТО (данные), не регрессия кода |
| 5.3 | Есть тексты (`direct_slepok_content`) / voice (`AGENTS`) | запись в БД + `get_agent(slepok)` |
| 5.4 | Реально СОЗДАЁТ РК с контентом слепка (не generic) | прогон на аккаунте директолога |

**Известные пробелы данных слепка `scherbakova × Мультибренд` (диагностика 2026-07-10, дозаполняется
харвестом с реальных аккаунтов Щербаковой):**
- **Ключи:** `tp2/ct0032/keywords/scherbakova.txt` отсутствовал (Changan CS55), `tp5/ct0032` = 2 seed-строки
  (единственный дырявый ct из 31). → сбор с живых аккаунтов Щербаковой + запись в пак (LXC 101 `/opt/neuro_content_local` И M3-мастер).
- **Видео (`VIDEO_NO_POOL`×5, info):** в `_video_pool/` нет ct аккаунта (BAIC/Belgee/Changan) — 16 ct в пуле,
  марок Щербаковой среди них нет. Реальная пустота, НЕ фолс аудита. → наполнить пул ИЛИ принять как норму.
- **Картинки (`CT_SLEPOK_IMAGES_EMPTY`×4, warn):** scherbakova не внесена в `image_slepki.txt` ни для одного
  ct tp1/Мультибренд → работает фолбэк `read_any_slepok_images` (картинки ЕСТЬ, но не щербаковские). → дозаполнить теги.

### 5.c Расширенный DoD-чеклист слепка (зафиксировано 2026-07-13)

> Полная версия критериев готовности — охватывает все 4 слоя (структура → профиль → контент-пак →
> сервис-исполнитель). Пункты 5.1–5.4 выше — оперативные сокращения пп. 1, 3, 4 из этого списка.
> Концепция «4 слоя» и цепочка использования — в `docs/ARCHITECTURE.md`, раздел
> «Слепок — четырёхслойная модель и цепочка использования».

| # | Критерий | Как проверить |
|---|----------|---------------|
| 1 | **Structure ⊆ Profile ⊆ реально создаваемое** — ни одна позиция structure не режется молча профилем; профиль не пропускает ничего сверх structure. ⚠️ **`targeting_profile.json` АВТОРИТЕТЕН по составу tp — не только структура слепка (`direct/slepki/<key>.json`):** `_slepok_profile_excludes_tp` (`blueprint_targeting.py:218`) режет из UI **И из создания** любой tp, которого НЕТ в профиле, даже если он есть в structure. **tp6/tp7 ОБЯЗАНЫ быть в профиле у реально-запускающих их слепков** (иначе не показываются и не создаются — корень бага tp6/tp7 у Терехова/Щербаковой/Павлова/Караваева 2026-07-12/13; добавлены в профиль 2026-07-13). Частая грабля. | Сверить структуру слепка (`direct/slepki/<key>.json`) ↔ `targeting_profile.json`; preflight — список items совпадает с ожидаемым; для слепка с живыми tp6/tp7 — убедиться что оба tp есть в `targeting_profile.json` |
| 2 | **Позиция исполняется буквально** — `feed_role`/`feed_id`, `pricing`, `targeting_mode` (вкл. гибрид keywords+audience), `audience_category` уважаются сервисом как есть, не переинтерпретируются галочками / fan-out по умолчанию | Созданные кампании: фид, стратегия, тип таргетинга совпадают с позицией структуры |
| 3 | **Контент несёт голос директолога** — заголовки/тексты проверяемо отличаются между директологами на одинаковом бренде, не сваливаются в generic-кредитный шаблон | Сравнить live-заголовки двух директологов на одном ct |
| 4 | **Ключи — из корректного корпуса**, без чужемодельной протечки; если реальных ключей нет — позиция явно `blocked`, а не молча падает в autotarget | `read_keywords(segment,tp,ct,slepok)` для ВСЕХ ct слепка; cross-ct ревью токенов; `KEYWORD_REPAIR_NO_PACK_SILENTLY_OK` = 0 |
| 5 | **Картинки/видео** — 1–5 по заданной fallback-цепочке, с настоящим self-heal при временных сбоях; `VIDEO_NO_POOL` при реально пустом пуле — не ложный аудит | `CT_SLEPOK_IMAGES_EMPTY` и `VIDEO_NO_POOL` = warn, не бизнес-блок; видео-пул содержит ролики для нужных марок слепка |
| 6 | **Минус-слова подключены** — и глобальные, и собственные слепка | `GLOBAL_MINUS_CAMPAIGN_MISSING = 0` после создания; пак содержит `_minus`/`_minus_shared` для ct |
| 7 | **Фильтр фида tp1/tp3/tp5/tp7 соответствует сегменту и site_type** — товарные/каталожные объявления используют поля конкретного фида: YML → `vendor/model`, AUTO_RU/БУ → `mark_id/folder_id`. Для `С пробегом` глобальные минус-фильтры марок/моделей запрещены; для марочных/модельных кампаний нужен positive-фильтр конкретной марки/модели; для общих `ct0000` tp7 feed-filter не обязателен. DoD-гейт до создания черновика проверяет пересечение фильтра с офферами фида, мёртвые товарные группы не создаются | `FEED_FILTER_MISSING_GRID = 0` с исключением БУ-global-minus; `LISTING_POSITIVE_FILTER_MISSING = 0`; `FEED_FILTER_MISSING_UAC` не флагает `ct0000`; group_count tp1/tp3/tp5/tp7 > 0 после создания |
| 8 | **Preflight ловит дубли** — и внутри одного слепка между site_type, не только между директологами; совпадения либо осознанны и задокументированы, либо это баг | Preflight на слепке; дубли из `SLEPKI_AUDIT_2026-07-12.md` «Exact item repeats» — сверить с источником кабинета |
| 9 | **Источник истины — живой кабинет**, а не имена кампаний и не аналитическая таблица (`Dim_Campaign` недосчитывает) | `live_verification.summary.errors = 0` после прогона; не ориентироваться на счётчики BI |
| 10 | **Отчётность не врёт** — job status / errors_log / виджет докрутки отражают реальное состояние, нет тихих «14/14 чисто» при живых дефектах | Сравнить `summary.errors` из `live_verification` с кабинетом вручную |
| 11 | **`group.name` в слепке НЕ содержит зашитый реальный регион харвеста** — только `camp_names[0]` (city-агностичен) идёт в базовое имя кампании; область/регион аккаунта подставляется ОТДЕЛЬНО движком при создании (`_emit_struct`, `create_set_plan.py`). Зашитый реальный город/область в `group.name` — след мёржа нескольких региональных аккаунтов в один слепок; это ОШИБКА онбординга. Исправление: убрать регион из `group.name` (фикс 035756f — 170 групп, 7 слепков, 2026-07-21). При онбординге нового слепка — проверять `group.name` на вхождения реального города/области. | `grep -r '"name"' direct/slepki/*.json | grep -iE 'краснодар|москва|самара|уфа|...'` → 0 реальных регионов в именах групп (допустимы только токен `ГОРОД` как плейсхолдер) |
| 12 | **Сегмент кампании и группы совпадают** — в `camp_names` кампании с явным сегментом `Марки` не содержат группы `Общее/Модели`, кампании `Модели` не содержат `Марки/Общее`, кампании `Общее` не содержат `Марки/Модели`. `Авто/Автомобили/Машины` считается сегментом `Общее`. | Для UI-пака `Авто`: raw-аудит `direct/slepki/*.json` по файлам без `ui_group`/`auto=false`; для каждого item с `camp_names[]` `segment(_first_ct(gc))` должен входить в явные сегментные слова каждого имени; итог `total_issues=0`. Дополнительно проверить через `structure_to_campaigns(...)`: явные сегментные кампании не имеют item чужого сегмента. Item без поля `camp_names` проверяются отдельным fallback-аудитом, это не нарушение данного сегментного инварианта. |
| 13 | **Один таргет-профиль не размножается разными группами** — внутри одного слепка/site_type/tp и одной кампании не должно быть двух item с одинаковым `ct/a/n/r/ag/g` и одинаковой семантикой группы. Общие синонимы (`Смарт`, технический `ct0014...`, `Авто/Автомобили/Машины`; повтор `Авито/Авто Ру/Дром`) должны быть объединены в одну группу, исходные `gk` сохраняются через `merged_gks`. | Для UI-пака `Авто`: аудит по `(camp_names, ct/a/n/r/ag/g, canonical_group)` должен давать `exact_dup=0` и `canon_dup=0`; отдельно `common_in_brand=0`, `wrong_tp67_ct0000=0`, `generic_tovar=0`, `tp67_container=0`. |

### 5.a Не-авто слепки (B2B-лидоген: `dmp` и будущие) — признак `"auto": false` в структуре

Не-авто слепок продаёт НЕ авто (dmp = лиды/контакты/базы). Его нельзя сажать на авто-рельсы
(Марки/Модели, авто-контент). Признак — поле `"auto": false` у directolog в структуре слепка (`direct/slepki/<key>.json`)
(хелперы `_slepok_is_auto` / `_non_auto_slepki` / `_non_auto_site_types`). DoD для таких слепков:

| # | Критерий | Как проверить | Статус dmp (прогон 2026-07-11) |
|---|---|---|---|
| 5.a1 | Структура = реальные темы кабинета (splits), **без фейковых Марки/Модели** | UI по слепку: tp2 показывает темы, не Марки/Модели | ✅ |
| 5.a2 | **Кол-во кампаний = серверный план = кабинет**, тип оплаты по галочке «под стиль сайта» (#4) | UI-счётчик == `direct_automation_jobs.body.items` == кабинет | ✅ dmp: галочка активна → 16 РК (10 Поиск + 6 МК, cpc+cpa); снята → 8 (5+3, только cpc) |
| 5.a3 | **Заголовки — B2B** (контакты/лиды/клиенты), без авто-лексики | ads.get / UAC content | ✅ (фикс `create_content.py` `_is_dmp`) |
| 5.a4 | **ТЕКСТЫ объявлений — B2B**, без авто (кредит/трейд-ин/КАСКО/марки авто) | ads.get текст групп tp2 + UAC текст МК | ✅ `text_gen.py:_rsya_texts():644` — `if site_type == "dmp"` возвращает B2B-корпус (`AGENT_ADS["dmp"]["texts"]` + fillers) вместо авто-пула; вызов `create_set_text_builders.py:374` пробрасывает `site_type`. Сверено 2026-07-12. |
| 5.a5 | Быстрые ссылки / уточнения — B2B | sitelinks/callouts кампаний | ✅ сайтлинки B2B (фикс 2026-07-12). **Уточнения — ✅ (масштаб 2026-07-14):** 8/8 tp2 несут callouts=8, авто-подтяжка из `public.direct_slepok_callouts` при пустом body. ⚠️ Только tp2; МК tp6/tp7 (UAC/МК) callouts не поддерживают. |
| 5.a6 | **Картинки — СОБСТВЕННЫЕ B2B-креативы слепка**, не авто и не чужие | image_slepki.txt слепка + визуально в кабинете | ✅ (масштаб 2026-07-14): все 6 МК = 5 distinct картинок из dmp-пула (фиксы `DMP_IMAGES_TRUNCATE_BEFORE_PHASH_DEDUP` + `DMP_PHASH_COLLAPSES_DISTINCT_BANNERS` — дедуп ДО обрезки + pHash не схлопывает разные баннеры). Было 2/50. |
| 5.a7 | Стратегии кампаний как в кабинете (Переходы=за клики, остальное=за конверсии) | strategy кампаний | ✅ канон: «за клики»=`Search=AVERAGE_CPA` (campaign.py:97), «за конверсии»=`PAY_FOR_CONVERSION`. Идентификация-Переходы=AVERAGE_CPA — ВЕРНО (не дефект) |
| 5.a8 | Ключи групп — реальные B2B из выгрузок, авто-фильтр не режет | read_keywords по ct + text_gen bypass для не-авто site_type | ✅ bypass `_NON_AUTO_SITE_TYPES` в `_filter_group_keywords`. **2026-07-12: пак наполнен из выгрузок кабинета — 34 ct (ct0800–ct0833), 1204 ключа, вкл. ранее отсутствовавшие ct0822–ct0833.** Чтение пака: `kontent_pack.py:gather` — local-first (зеркало 101), не ssh на протухший M3 |

**Открытые дефекты dmp:** нет — масштаб 56 РК (14×4) 2026-07-14 закрыл все 7 QA-пунктов зелёными.

**Боевые инварианты dmp (масштаб 2026-07-14, полное — CODER.md §🅱️➕):**
- Автотаргет tp2 = **УЗКИЙ** (EXACT_V2_MARK/WITHOUT_BRAND), атомарно при Grid `AddUnifiedAdGroups` (`search_tp2`), НЕ пост-патчем.
- Ключи льются **тем же Grid-транспортом**, что создавал группы (`add_keywords`), НЕ v5 — иначе ключи-фантомы (лаг репликации).
- Промо = **text-only** (`amount/unit/prefix=null`), создать в кабинете заранее (`PromoClient.add`) → кампании привяжутся.
- ct-нумерация = **ct0800–ct0833** (+799 от старой); пак-папки на M3 в этой нумерации, иначе десинк.


### 5.b Техническая реализация не-авто / dmp — file:line карта → [docs/archive/DOD_ARCHIVE.md](docs/archive/DOD_ARCHIVE.md)

(перенесено 2026-07-16: таблица признака не-авто, контент-роутинг по файлам, ct-пространство dmp, 3 режима МК)

### 5.d Реконструкция и аудит структуры tp6/tp7 — каноны → [docs/archive/DOD_ARCHIVE.md](docs/archive/DOD_ARCHIVE.md)

(перенесено 2026-07-16: идентичность позиции слепка, targeting_mode из payload а не имени,
метка в имени из факта, квиз/неопределено вне скоупа, reconciler-процедура 5 шагов)

---

## Процедура проверки готового набора (по этому DoD)

1. Дождаться терминального статуса джоба (не `running`). **`error` разбирать так же тщательно, как
   `done`:** при `failed>0` статус всегда `error`, и именно там дефекты вероятнее всего; разбивка
   `result.has_issues` пишется на `done`/`error`/`cancelled` (§4.6). `has_issues_unknown=true` →
   верификация не отрабатывала, нулям верить нельзя. `interrupted` разбивки не несёт by design.
2. Прочитать `result.live_verification.summary.errors` + `issues[].code` → пункты §1.
3. По кодам issues определить, что добивать (таблица §1).
   ⚠️ `WRONG_AUTOTARGET` из джобы перемерить ПОСЛЕ отложенной добивки (врезка после таблицы §1.a):
   лаг реплики Grid даёт ложные срабатывания, которые снимаются сами.
4. Визуально в кабинете: 1.5 каталог, 1.6 ссылки, контент §2, per-tp §3.
   Корневые ссылки считать ТОЛЬКО по сегментам «Марки»/«Модели» (§1.13): «Общее» → корень штатно.
5. `journalctl -u direct-create-worker.service` на предмет 1.9 и транзиентных Яндекс-ошибок (1000/500 — не наши баги).
   Там же — профиль времени по `STAGE_TIMING` (§4.5), если прогон показался медленным.
6. Отметить в этом файле статусы ✅/🟡/⬜ по факту прогона.
</content>
</invoke>

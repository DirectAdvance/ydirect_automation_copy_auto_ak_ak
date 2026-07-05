# Инварианты создания кампаний (нейродиректолог)

> ОБЯЗАТЕЛЬНЫ при КАЖДОМ создании РК. Без них кампанию НЕ создаём —
> `create_set` валидирует эти пункты перед отправкой и отказывает, если что-то не выставлено.

| # | Правило | Значение |
|---|---------|----------|
| 1 | **Метрика + цель** | ВСЕГДА добавлены: счётчик Метрики + `goal_id` |
| 2 | **UTM-метка** | ВСЕГДА: `tp1–tp5` — на уровне **групп**; остальные (`tp6`/МК, `tp7`/Товарка, `tp8+`) — на уровне **кампании** |
| 3 | **Персонализация** | ВСЕГДА **ВЫКЛ** |
| 4 | **Мониторинг сайта** | ВСЕГДА **ВКЛ** |
| 5 | **Расширенный географический таргетинг** | ВСЕГДА **ВЫКЛ** (там, где есть тумблер) |
| 6 | **«Директ помогает»** (авто-рекомендации) | ВСЕГДА **ВЫКЛ** |

## Статус по коду (сверено на porg-psm5h7q6, 2026-06-21)

> 🔑 **«Персонализация» (Yandex Neuro Ads) = адаптивные тексты.** Отдельного «neuro»-поля
> нет (grid-интроспекция `GdUnifiedCampaign` → единственный кандидат `isAlternativeTextsEnabled`).
> Поэтому персонализация ВЫКЛ = `ALTERNATIVE_TEXTS_ENABLED=NO` (v5) / `alternative_texts_enabled=False` (UAC).

### Поиск (`tp2`, legacy): `blueprint.py::_create_search_test_campaign`
> ⚠️ `_create_search_test_campaign` использует УСТАРЕВШУЮ стратегию `HIGHEST_POSITION` — нарушение канона.
> Переделать на `search_cpa`/`search_payconv` по образцу `_create_tp5_campaign`.

**4 настройки выставляются в `Settings` (B1-фикс 2026-06-22: добавлен ENABLE_COMPANY_INFO=NO):**
- #3 Персонализация — `ALTERNATIVE_TEXTS_ENABLED=NO` ✅ проверено live
- #4 Мониторинг сайта — `ENABLE_SITE_MONITORING=YES` ✅ (был дефолт NO) проверено live
- #5 Расширенный гео — `ENABLE_AREA_OF_INTEREST_TARGETING=NO` ✅ (был дефолт **YES**!) проверено live
- **«Карты/список организаций»** — `ENABLE_COMPANY_INFO=NO` ⚠️ добавлен (B1), live не проверен отдельно для TextCampaign; аналог `enableCompanyInfo=False` Grid + `ENABLE_COMPANY_INFO=NO` в `campaign.py::create_unified_campaign` (UnifiedCampaign, проверено live 2026-06-21)
- #1 Метрика — `CounterIds:[counter_id]` (если есть) + гейт `api_create_set` (нет счётчика/цели → `400`)
- #2 UTM групп (tp1–tp5) — **поле `AdGroups[].TrackingParams`** (v5 `adgroups.add`), ✅ проверено
  эмпирически (группа с `TrackingParams` создаётся и читается обратно). Шаблон —
  `campaign.py::UTM_TEMPLATE` (макросы `{campaign_id}`/`{keyword}`/… работают). Реализовано:
  `_build_tp1_adgroups` (tp1, v501) и `_build_tp2_adgroups` (tp2/tp5, v5) — обе ставят
  `TrackingParams=_UTM_TEMPLATE_TP1` при создании групп.

### UAC (МК `tp6` / Товарка `tp7`): `campaign.py::UacClient.build_payload`
- #1 — `counters` + `goals` ✅
- #2 (кампания) — `tracking_params` (UTM) ✅
- #3 Персонализация — `alternative_texts_enabled=False` ✅ (поле есть в build_payload; grid `isAlternativeTextsEnabled`)
- #6 — `recommendations_management_enabled=False`, `price_recommendations_management_enabled=False` ✅
- #4 / #5 — ⚠️ в UAC-payload полей мониторинга/расш.гео нет. МК — упрощённый тип; вероятно этих
  тумблеров у него нет (они на обычных Поисковых). Нужна live-проверка МК-черновика (нечастый кейс).

## Гейт
`blueprint.py::api_create_set` — нет счётчика/цели → `400`, кампания НЕ создаётся (правило #1).

### ЕПК tp5 (гибрид v501 + Grid): `blueprint.py::_create_tp5_campaign`
Реализовано 2026-06-22. Порядок строгий: v501 каркас → Grid-докрутка → v5-корректировки.

- #1 Метрика — `counter_ids` через `UnifiedCampaignSpec.counter_ids`; GoalId — внутри стратегии ✅
- #2 UTM групп — `TrackingParams` на группах (`_build_tp1_adgroups`) ✅
- #3 Персонализация — `isAlternativeTextsEnabled=False` в Grid-мутации (`grid_finalize.py`) ✅
- #4 Мониторинг сайта — `hasSiteMonitoring=True` в Grid-мутации ✅
- #5 Расш.гео — `hasExtendedGeoTargeting=False` в Grid-мутации ✅
- #6 «Директ помогает» — `isRecommendationsManagementEnabled=False` в Grid-мутации ✅
- **Места показа** — ⚠️ ИСПРАВЛЕНО 2026-07-01: `placementTypes=null` (НЕ список!) + `platforms` (gallery+search+organic из `PLATFORMS_SEARCH`). Любой НЕпустой `placementTypes` Яндекс матчит с пресетом («SEARCH_PAGE» → «Поиск»), `ADV_GALLERY` игнорится → UI «Поиск». «Ручная настройка» с ТГ = `placementTypes=null`. tp5-call шлёт `placement_types=[]` (sentinel → null).
  - **create=null (2026-07-01, code-review):** `_create_shopping_via_cookie` (blueprint ~12082) при СОЗДАНИИ шлёт `placement_types=None` (эталон HAR20 tp5-create). Форс списка в AddCampaigns рискует `ORGANIC_PLACEMENT_TYPES_INVALID_COMBINATION` → падение всего create.
  - **🔴 ПРОТИВОРЕЧИЕ (открыто, требует live tp5-create+read):** этот пункт утверждает finalize=null, НО код `blueprint.py:12143` `_finalize_search_via_grid(placement_types=list(PLACEMENTS_TP5))` шлёт ЯВНЫЙ `["SEARCH_PAGE","ADV_GALLERY"]` с обратным комментом (HAR49-эталон 712024652: «null давал пресет Поиск, ADV_GALLERY не входит»). Одно из двух утверждений неверно. Разрешить живым созданием одной tp5 и чтением фактических placementTypes/UI-мест. НЕ править finalize вслепую.
- **ListingAd** — в товарной группе можно добавить `add_listing_ad(adgroup_id, feed_id, collection_id)` рядом с ShoppingAd. `FeedFilterConditions` по `collectionId` работает (проверено live 2026-06-21).

## Проверка
Эталонный аккаунт: **`porg-psm5h7q6`**. Поиск-настройки #4/#5 сверены live (PASS). tp5-гибрид — черновики 710909545/710909551 на porg-psm5h7q6. UAC #4/#5 (мониторинг/расш.гео) — нужна live-проверка МК-черновика.

## ⚠️ ГРАБЛЯ: UAC-кампании НЕВИДИМЫ в v5 (проверено 2026-06-21)
UAC «Мастер кампаний» (tp6) и «Товарка» (tp7) — приватный `/web-api/uac/` на куках — **НЕ возвращаются** v5 `campaigns.get` и **НЕ удаляются** v5 `campaigns.delete` (молчаливый no-op, `Errors:[]`, но кампания остаётся). → Проверка «аккаунт чист» только через v5 = **ЛОЖНАЯ**: UAC-черновики висят, видны лишь в интерфейсе Директа.
**Правило:** при чистке/верификации аккаунта проверять ОБА слоя — v5 (TEXT/ЕПК) **И** UAC через куки.
- Список/удаление UAC: `GET`/`DELETE` `/web-api/uac/campaign/{id}/` (куки агентства, CSRF главпотока).
- DRAFT UAC удаляется `DELETE` напрямую; `STOP` даёт `403 "still drafted"` — не нужен.

## 🔬 Проверка+починка после создания — Grid HAR-схемы (2026-07-01)

> Захвачены реальные web-api/grid операции (HAR 46–49 на porg-psm5h7q6) → verify/repair
> может ЧИТАТЬ и ЧИНИТЬ по куке БЕЗ баллов. Схемы сохранены при разборе.

**READ (проверка):**
- `GroupsForEdit` (vars: `groupsIds`,`adGroupsInput.filter.{adGroupIdIn,campaignIdIn}`) → группа:
  `keywords[{phrase}]`, `relevanceMatch{isActive,id,relevanceMatchCategories,autotargetingBrandSettings}`,
  `regionIds`, минус-слова, `trackingParams`, `bidModifiers`.
- `CampaignsEditData` → кампания: `placementTypes`, `isOrganicSearchEnabled` (=«Динамические места»),
  `disabledPlaces` (минус-площадки), `promoExtension.id`.
- `BannersQueryForEditDeprecated` → объявление: `price`/`priceOld`/`prefix`(FROM)/`currency` (adPrice).

**WRITE (починка, read-modify-write — UpdateUnifiedAdGroups заменяет ВСЕ поля группы!):**
- `UpdateUnifiedAdGroups(input:{updateItems:[GdUpdateUnifiedAdGroupItemInput]})` → ключи + автотаргет
  (шли ПОЛНОЕ тело группы из GroupsForEdit + меняй только keywords/relevanceMatch). Ответ `"success":true`.
- `UpdateCampaigns` → placementTypes / isOrganicSearchEnabled / disabledPlaces (места/динамика/минус).
- `UpdateAdaptiveTextAds` → adPrice (цена).

## 📐 Канон настроек по tp (что проверять/чинить)

| tp | Динамич.места (`isOrganicSearchEnabled`) | `placementTypes` | Автотаргет-профиль группы |
|----|------------------------------------------|------------------|---------------------------|
| tp2 (Поиск) | **ВЫКЛ** (=False) | `["SEARCH_PAGE"]` | `search_tp2` |
| tp4 (Поиск+Динамика) | ВКЛ (organic=True) | `["SEARCH_PAGE"]` | `search_tp2` |
| tp5 (Поиск+ТГ) | ВКЛ | **`null`** (Ручная+ТГ) | `search_tp2` |

- **Автотаргет `search_tp2`** = `relevanceMatchCategories:["EXACT_V2_MARK"]` (только «Целевые») +
  `autotargetingBrandSettings:["WITHOUT_BRAND"]` (только «без упоминания бренда»). Для ВСЕХ search-групп
  (tp2/tp4/tp5). Задаётся `build_adgroup(autotargeting_profile="search_tp2")`; tp5 через `create_shopping_full`
  (гейт `search AND NOT network`). tp3 (РСЯ) — НЕ трогать.
- **`isOrganicSearchEnabled = bool(platforms.organic)`** в `_finalize_search_via_grid` (шаблон
  `grid_uc_template.json` приносит True → протекал в tp2). tp5 platforms=None→PLATFORMS_SEARCH.organic=True.

## 🐞 Root-cause'ы (2026-07-01, разобраны direct_investigator)

- **tp2/tp4/tp5 без ключей:** «Общее»-группа (ct0014) несёт ТОЛЬКО модельные ключи (`auto ru monjaro`)
  → `_drop_model_keys_common` вырезал всё. Фикс: guard в `_filter_group_keywords` — seg-фильтр НЕ
  обнуляет непустой набор (`return out or kws`).
- **tp1 «все фиды»:** товарка множилась по всем enabled-фидам. Фикс: `role='catalog'` в
  `direct_global_feed_rules` + `_account_model_feeds(catalog_only=True)` ТОЛЬКО для tp1 (tp7 — все).
- **tp1 нет минус-площадок:** хранился `https://gdz.ru/` (URL), Яндекс ждёт голый ХОСТ → `_place_host`.
- **tp1 нет цены:** брендовая группа без своего оффера → `_group_ad_price` фолбэк на `_min_offer_price`.
- **tp6 сырое имя:** UAC берёт `it["name"]` дословно → regex-детект сырого слага + пересборка `_build_name`.
- **Транзиент Яндекса 500** («Внутренняя ошибка сервера») на AddUnifiedAdGroups → group=0 → hard-fail.
  Фикс: retry+backoff в `grid_create._mutate` на top-level `errors` (НЕ на validationResult — детерминизм).
- **Дубли ключей (двойной залив):** `create_full`/`add_text_content_to_existing` слали ключи И полем
  `keywords` в `AddUnifiedAdGroups`, И отдельным `AddKeywords` → Grid создавал их ДВАЖДЫ для групп
  <~140 фраз (крупные `AddUnifiedAdGroups` keywords игнорит — потому и не везде). ⚠️ **ИНВАРИАНТ:**
  ключи ЕДИНСТВЕННЫМ путём — `AddKeywords`; в `build_adgroup(keywords=[])` всегда. Сбой `AddKeywords`
  НЕ глушить (`except: pass`) — писать в `rep["errors"]` (это единственный путь; иначе кампания без
  ключей молча). Живьём: 601==601/193==193 target vs source, дублей 0 (job 24a3652c40c1).

## ⚠️ Пост-проверка `run_create_set_postprocess` (blueprint.py:~12575)
Запускается АВТО в конце `api_create_set`. Раньше `grid_read.campaign_content_counts` читал только
`{adgroups,ads,bad_adgroup_names}` → 6 дефектов проходили молча («Проверка пройдена» врала). Расширяется
enrichment'ом по HAR-схемам выше (keywords/relevanceMatch/places/dynamic/minus/promo/price) + auto-repair.

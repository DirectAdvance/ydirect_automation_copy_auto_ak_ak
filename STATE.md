# Нейродиректолог — Состояние

> Читать ПЕРВЫМ в начале каждой сессии. Обновлять ПОСЛЕДНИМ перед выходом.

## Последняя сессия: 2026-07-01 — code-review (8 углов) + 6 фиксов + git-репо

Дополнение 35: `/code-review` (high, 8 finder-углов + верификация) по правкам сессии.
12 кандидатов → 6 реальных фиксов, остальное intended/latent. Затем — вынос проекта в
отдельный git-репо `DirectAdvance/neurodirectologist`.

**6 применённых фиксов (blueprint.py, py_compile OK):**
1. `_combo_button` ~6824: кнопка вешается ТОЛЬКО на абсолютный `https?://` href (relative/пустой
   → None). Раньше relative href уходил в кнопку невалидным (старый `_homepage_url` возвращал '').
2. `_combo_fill_texts` ~7821: `return out[:cap]` — seed из items мог быть >cap → ads.add (Texts ≤3).
3. tp6/tp7 брендовый fallback заголовков ~12885: добавлен number-gate
   `_is_bad_start/_bad_ad_title/_has_number` (был только в основном цикле → заголовок без цифры
   мог проскочить в брендовой докрутке).
4. `_grid_callout_ids` fallback ~10984: инлайн-дедуп → `_dedup_callout_ids(by_text, cap=limit)` —
   одна точка правды #24 (было два расходящихся цикла семантического дедупа).
5. `_brand_title_set` ~10484: `out[:7]` → `out[:8]` — 8-й #23-шаблон «Госпрограмма» больше не
   срезается всегда.
6. **tp5 create-guard УБРАН** ~12082: `placement_types` при СОЗДАНИИ = `None` (было `PLACEMENTS_TP5`).
   Причина: guard добавлялся под ложную тревогу UI-кэша (live был корректен), а форс
   `['SEARCH_PAGE','ADV_GALLERY']` в AddCampaigns НЕ подтверждён и рискует
   `ORGANIC_PLACEMENT_TYPES_INVALID_COMBINATION` → падение ВСЕГО tp5-create. Места ставит finalize
   (`_finalize_search_via_grid`, HAR49). create=null = эталон HAR20.

**⚠️ ОТКРЫТО для live-верификации (#9/#15) — НЕ править вслепую:**
- **Противоречие docs↔code по tp5 finalize-местам:** `CAMPAIGN_INVARIANTS.md:57` говорит tp5 =
  `placementTypes=null` (любой список Яндекс матчит с пресетом «Поиск», ADV_GALLERY игнорится).
  НО `blueprint.py:12143` finalize шлёт ЯВНЫЙ `list(PLACEMENTS_TP5)` с комментом «null давал
  пресет Поиск, ADV_GALLERY не входит» (HAR49, (C review)). Взаимоисключающие утверждения.
  Разрешить ТОЛЬКО живым tp5-create+read: создать одну tp5 → прочитать фактические placementTypes
  и UI-места показа. НЕ трогать finalize до этого.
- Прежние #9/#15 (ListingAd>0, места tp5) — по-прежнему ждут чистого прогона Семёна.

**Не тронуто (intended/verified-safe):** callout coarse-bucketing (#24 by design), `_fill_title`
`lo`-параметр (latent, текущие вызовы совпадают), двойная нормализация в `_dedup_callout_ids`
(идемпотентна), хардкод typo-фиксов в `_normalize_callout_text` (прагматично). Conventions-угол = [].

## Пред. сессия: 2026-07-01 — 3 задачи (#24/#23/#26) в blueprint.py

Дополнение 34: три точечных фикса по жалобам Семёна на свежем прогоне.

**ФИКС #24 — callout reuse БЕЗ normalize/dedup (ОСАГО вместо КАСКО, два «шины»):**
- `blueprint.py` ~8128: добавлена `_dedup_callout_ids(co_map, cap=8)` — нормализует тексты через
  `_normalize_callout_text`, семантически дедупит через `_dedup_callouts`, возвращает ≤cap id.
- Три call-сайта заменены: `_assemble_tp1_assets` (~9154), `_tp5_account_data` (~12224, ~12229).
- `_grid_callout_ids` (~10981): fallback на первые N без дедупа → семантический дедуп по
  `_callout_semantic_key(_normalize_callout_text(_t))`.

**ФИКС #23 — одинаковые начала заголовков в `_brand_title_set`:**
- `blueprint.py` ~10464-10475: из 8 шаблонов 6 начинались с `{brand}{loc}.` →
  заменены на разные начала: «Новый», «Трейд-ин», «Авто», «Госпрограмма» (+ 2 оставлено на марку).
  Бренд по-прежнему в первых 1-2 словах, до первой запятой (ограничение #12 сохранено).

**ФИКС #26 — заголовки/тексты без добивки (9-12 свободных символов):**
- `blueprint.py` ~12857: в tp6/tp7-цикле строки_заголовков — добавлен `_fill_title(…, 45, 56)`
  вместо голого `_strip_credit_rate(_t)[:56]`. Теперь все пути через `_fill_title`. 
- `blueprint.py` ~7808: `_combo_fill_texts` — заменены короткие (42-61 симв) hardcoded-строки
  на `_GENERIC_TEXT_FILLERS` (76-81 симв); `[:max].rsplit` → `_trim_to_word` (не срезает
  последнее слово у коротких текстов).

**Верификация:** `py_compile blueprint.py` — OK.

## Пред. сессия: 2026-07-01 — 3 фикса code-review (Баг#5/#7/#8) в blueprint.py

Дополнение 33: три точечных фикса из code-review по подтверждённым root-cause.

**ФИКС Баг#5 — _agid_to_nv2 индексное смещение при partial add_shopping_ads:**
- `blueprint.py` ~11689: убрана итерация через `enumerate(_add_ids)` с индексной ко-адресацией.
  Новый код: `for _gsi2, _nv2 in zip(_grid_shop_items, _shop_name_vals)` — прямой zip параллельных
  массивов. adGroupId надёжен (группа создана ДО add_shopping_ads). Фильтр марки → всегда своя группа.

**ФИКС Баг#7 — пустой Href в sitelinks → весь набор теряется:**
- `blueprint.py` ~9058: `_norm_sitelinks_for_v501` — добавлен per-sitelink фолбэк:
  `sl_href = s.get("Href") or s.get("href") or s.get("url") or base`.
  При `sl_href=""` — `continue` (пропуск сломанной ссылки). Набор не пропадает если часть ссылок с href.

**ФИКС Баг#8 — targetUrl только из одного фида (vs цены со всех фидов):**
- `blueprint.py` ~7032: новая функция `_account_offer_urls(login, url)` — account-level мёрж URL
  через те же `_price_feeds_for`-ранги что и `_account_offer_prices`. Кэш `_OFFER_URL_CACHE_ACCT`
  20 мин (TTL = `_OFFER_PRICE_TTL`).
- Три call-сайта заменены: `_build_text_from_pack` (~8444), `_build_tp1_from_pack` (~10691),
  `_create_tp1_via_cookie` (~11554). Фолбэк `_model_page_href` сохранён.

**Верификация:** `py_compile blueprint.py` — OK.

## Пред. сессия: 2026-07-01 — 3 фикса: каталожные фильтры, href из фида, per-group sitelinks

Дополнение 32: три точечных фикса по подтверждённым root-cause (live).

**ФИКС 1 — каталожные ListingAd без name-фильтра (saveDraft:True → addedAds пуст):**
- `grid_finalize.py`: `add_listing_ads_by_shopping_ads` query: `shoppingAdId` → `adGroupId`
  (GdListingAd не имеет shoppingAdId, но имеет adGroupId).
- `grid_finalize.py`: `set_listing_name_filters` поддерживает `adgroup_id` как альтернативу `id`;
  если передан `adgroup_id` — шлёт `adGroupId` в updateListingAds (фильтр ставится на группу).
- `blueprint.py` v5-путь (~11162): строим `_agid_to_nv` из `listing_build_items` (по adGroupId);
  при `addedAds` пустом (saveDraft:True) — строим `_lf_items` напрямую с `adgroup_id`.
- `blueprint.py` кука-путь (~11658): аналогично, `_agid_to_nv2` из `_grid_shop_items` + `_shop_name_vals`.

**ФИКС 2 — href модели из фида, не формула:**
- `_FEED_OFFERS_Q`: добавлен `targetUrl` в previews.
- Новые функции: `_grid_feed_offer_urls(login, feed_id) → {key → url}` (кэш 20 мин),
  `_feed_url_for_model(urls, model) → str|None` (та же логика что _ad_price_for_brand).
- `_build_text_from_pack` (tp2/tp4): вычисляет `_feed_urls` перед группами, `model_href = feed_url or slugFormula`.
- `_build_tp1_from_pack` (tp1 v5): то же, `_feed_urls_tp1`.
- `_tp1_pack_groups` (cookie): добавлен параметр `feed_url_by_model: dict | None = None`.
- `_create_tp1_via_cookie`: вычисляет `_feed_url_map`, передаёт в `_pack_groups_with_retry`.

**ФИКС 3 — sitelink.Href = href группы (per-group sitelink sets):**
- `_resolve_campaign_assets`: добавляет `out["asset_sitelinks"] = asset_sl` (нормализованный шаблон).
- `_build_tp1_adgroups`: новый параметр `base_sitelinks: list | None = None`; кэш `_sl_set_cache`;
  если `ad_href ≠ campaign_href` и `base_sitelinks`: создаёт per-group sitelink set через
  `_get_or_reuse_sitelink_set` с `Href=ad_href`, кэширует по ad_href; fallback на campaign-level set.
- `_build_tp1_from_pack`: передаёт `base_sitelinks=_assets.get("asset_sitelinks")`.

**РИСК-ПРИМЕЧАНИЕ для ФИКС 3:** per-group sitelink sets создаются через `_get_or_reuse_sitelink_set`
(best-effort, ошибка → fallback на campaign-level id). Cookie-путь (grid_create.create_full) получает
sitelinks на уровне кампании — там per-group logic отдельная задача.
РИСК-ПРИМЕЧАНИЕ для ФИКС 1: `adGroupId` в `updateListingAds` — best-effort (Яндекс может не принять);
при ошибке → warning в rep (try/except уже стоит).

**Верификация:** `py_compile blueprint.py grid_finalize.py` — OK.

**Что должен проверить direct_verifier (live):**
1. Создать кампанию с фидом → убедиться что `listing_name_set > 0` в результате (ФИКС 1).
2. Группы модели/марки: проверить `g["href"]` = реальный URL из фида, не /auto/lada slug (ФИКС 2).
3. Объявление группы: sitelink.Href = href группы (не homepage) (ФИКС 3).
4. Если `updateListingAds(adGroupId)` вернул ошибку → зафиксировать response для отладки.

## Пред. сессия: 2026-07-01 — авто-recreate draft-only (фикс 10/11) + длина товарного текста (6-D)

Дополнение 31: два точечных фикса.

**ФИКС 10/11 — авто-recreate DRAFT-only для поисковых кампаний:**
- `repair_auto.py`: добавлен тип `DeleteSearchDraft`; `recreate_force_names` и
  `build_recreate_queue_body` получили параметр `search_deletions` (names удалённых поисковых РК
  добавляются в `_repair_force_names` как safety net на случай Grid-кэша); `queue_recreate_repair_job`
  получил `delete_search_draft: DeleteSearchDraft | None` — вызывает callback и возвращает ошибку
  при `failed` или `blocked_non_draft`.
- `blueprint.py`: добавлена функция `_delete_search_draft_campaigns(login, agency, delete_items)` —
  проверяет статус через `_grid_list_campaigns(only_draft=True)`, удаляет через
  `gc.GridCreateClient(login).delete_campaigns([...])`, блокирует при хотя бы одной не-DRAFT
  кампании (консервативно). `_queue_recreate_repair_job` теперь передаёт
  `delete_search_draft=_delete_search_draft_campaigns`. `_auto_queue_recreate_after_done`
  получил Draft-gate: при `requires_explicit_trigger` проверяет все кампании к удалению через Grid;
  если все DRAFT → инжектирует `_auto_recreate_with_delete=True` в snapshot.body и повторно вызывает
  `auto_recreate_request`; если не все DRAFT → блокирует (как раньше); Grid недоступен → блокирует.
- Дизайн: авто-recreate ТОЛЬКО для DRAFT (все РК создаются с `launch=False` → всегда DRAFT);
  боевые кампании защищены на двух уровнях: `_auto_queue_recreate_after_done` (проверка статуса)
  и `_delete_search_draft_campaigns` (повторная проверка перед удалением).
- Recreate переиспользует стандартный resume-механизм (set_plan + `_repair_force_names`
  пересоздаёт удалённые имена).

**ФИКС 6-D — фолбэк-текст ShoppingAd/set_default_text:**
- Старый фолбэк (49 символов): «Официальный дилер. Тест-драйв и выгодные условия.»
- Новый фолбэк (76 символов, ≥73 ≤81): «Официальный дилер. Тест-драйв и выгодные условия по кредиту. Авто в наличии.»
- Заменено во всех 7 местах в blueprint.py (grep `Официальный дилер. Тест-драйв и выгодные условия.`).
- create_content.py, ai_agents.py не затронуты.

**Верификация:** `py_compile blueprint.py repair_auto.py` — OK. Реальный recreate-джоб НЕ запускался.

**Что должен проверить direct_verifier (live):**
1. Создать набор → дождаться NO_KEYWORDS_LIVE → авто-recreate запустился для DRAFT → кампания пересоздалась.
2. Убедиться что боевая (не-DRAFT) кампания с NO_KEYWORDS_LIVE блокируется (`draft_gate` в ответе).
3. set_default_text получил текст ≥73 символов (лог `value.bodies`).

## Пред. сессия: 2026-07-01 — copy preflight + целевой фид для товарных РК

Дополнение 30: copy-flow получил cookie/Grid postprocess и Grid-first verification/auto-repair.
- После `direct_copy.phase_upload(...)` запускается `_copy_cookie_postprocess(...)`: добирает через
  Grid/куки ассеты и сущности, которые могли не создаться из-за 152/нехватки баллов или v5-ограничений.
- Добавлен `GridClient.add_keywords(...)` (`addKeywords`) и подключён fallback для source keywords,
  которых нет в `keywords_done.json`: фразы добавляются в target adgroups без Direct API units.
- Уточнения: если v5 `adextensions.add` не дал ids, postprocess создаёт/находит callouts через Grid
  и прикрепляет их на campaign-level через `UpdateCampaigns`.
- Shopping/Listing: если v501 `ads.add(ShoppingAd)` не создал товарные объявления, postprocess создаёт
  `ShoppingAd` через `GridClient.add_shopping_ads(...)`, затем создаёт каталожные `ListingAd` через
  `add_listing_ads_by_shopping_ads(...)`.
- Промо: `_copy_filter_snapshot(...)` теперь оставляет source promotions с тем же доменом, что выбранные
  объявления. Postprocess создаёт промо через `PromoClient.add(...)`/Grid. Если перенесено ровно одно
  промо, оно прикрепляется ко всем скопированным кампаниям; если промо несколько, они остаются в
  библиотеке без blind attach, потому что v5 snapshot не хранит точную source-связь promo→campaign.
- После cookie-добивки copy-flow строит create_set-like `results`, запускает Grid-first
  `_create_set_live_verification(...)`, `repair_gate` и безопасный `repair_auto.execute_safe_post_create(...)`.
  В copy job result теперь есть `cookie_postprocess`, `results`, `live_verification`, `repair_gate`,
  `auto_repair`.

**Как было:** при нехватке баллов `direct_copy` логировал failures и мог оставить target без ключей,
товарных/каталожных объявлений, уточнений или промо; финальной live-проверки и авто-исправления в copy
не было.
**Как стало:** copy после upload добирает ключи/уточнения/промо/ShoppingAd/ListingAd по куки без units,
затем проверяет результат Grid-first и запускает безопасный auto-repair там, где есть поддерживаемое
действие.

**Ограничение:** UAC/tp6/tp7 всё ещё нельзя восстановить из v5 snapshot, потому что старый pull их не
читает. Для них нужен отдельный UAC-read/copy поток; текущий preflight не даёт молча сделать плохую
копию.

**Верификация дополнения 30:** локально `py_compile blueprint.py grid_finalize.py direct_copy.py` OK;
synthetic smoke подтвердил фильтрацию source promotions по домену selected ads, сборку copy-results
из `id_maps`, замену promo href source→target и предыдущие preflight checks. Реальный copy-job НЕ
запускался, чтобы не создавать черновики.

Дополнение 29: вкладка «Копирование кампаний» усилена защитным preflight и целевым фидом.
- Backend copy-flow больше не делает blind upload сразу после `direct_copy` snapshot: после pull/filter
  запускается `_copy_snapshot_preflight(...)`, который останавливает копирование при неподдержанных
  типах (`UNIFIED_AD_CAMPAIGN`/UAC/ЕПК через старый snapshot-copy), группах без объявлений, пустом гео
  или товарных/каталожных сущностях без целевого фида.
- Перед upload добавлена `_copy_rewrite_snapshot_context(...)`: заменяет source city/region в названиях,
  текстах, ключах, sitelinks/vcards/feeds/promotions на целевой city/region, чтобы не протекали чужие
  гео-слова вроде «Краснодар» в аккаунт Уфы.
- `direct_copy.phase_upload(...)` получил optional `force_feed_url/force_feed_name`; при наличии фида
  создаёт/мапит все source feed id на целевой URL-фид. UI по умолчанию отдаёт
  `/dostup-k-rasprodazhe-live-01-b.xml` для товарных и каталожных РК.
- UI показывает поле целевого фида и preflight-сводку в статусе copy-job.

**Как было:** вкладка copy работала через старый v5 snapshot-copy: домен менялся, но UAC/tp6/tp7 не
читались, ЕПК/товарные настройки восстанавливались частично, гео-слова в именах/текстах/фразах не
заменялись, фиды брались из source snapshot.
**Как стало:** опасный snapshot-copy останавливается до upload с явными ошибками, гео snapshot
нормализуется до целевого аккаунта, а товарные фиды принудительно мапятся на целевой URL-фид.

**Верификация дополнения 29:** локально и на LXC101 `py_compile blueprint.py direct_copy.py` OK;
synthetic smoke подтвердил preflight для `UNIFIED_AD_CAMPAIGN`, групп без объявлений, shopping/feed
counts, замену `Краснодар`→`Уфа` в snapshot и сборку
`https://haval-drive-ufa.ru/dostup-k-rasprodazhe-live-01-b.xml`. Деплой LXC101: remote-файлы содержат
новые строки, `direct.service` restart active, публичный `/direct/automation` вернул `HTTP 302`,
свежий `journalctl` без traceback/500. Реальный copy-job НЕ запускался, чтобы не создавать черновики.

Дополнение 28: тело `_gen_campaign_content` (844 строки) переехало в
`create_content.py::run_gen_campaign_content(...)` по паттерну DI (как create_set_tp1/text). В
blueprint.py осталась тонкая обёртка (импорт `from .create_content import run_gen_campaign_content`
внутри функции — как остальные create_set_* модули, без циркулярного импорта).
- **DI-поверхность = 19 project-имён** (не 13 из ТЗ): symtable нашёл дополнительно `_RU_CITIES`,
  `_bad_ad_text`, `_bad_ad_title`, `_display_brand`, `_title2_blocklist`, `_variant_norm_key`.
  Итого 14 функций + 5 констант. `A` (ai_agents) импортируется в теле — не DI. stdlib json/re — просто import.
- **Верификация:** py_compile OK; pyflakes без undefined names; symtable — 0 свободных имён в
  run_gen_campaign_content (все globals = builtins + json/re); тело BYTE-IDENTICAL оригиналу
  (45711 байт, 837 строк, 0 diff); wrapper 26 kwargs == 26 kwonly-params, все pass-through key=key.
- **НЕ деплоено, сервис НЕ рестартовал** (по указанию). Следующий шаг: деплой+smoke либо продолжить рефактор.

## Пред. сессия: 2026-07-01 — ГЛАВНЫЙ ЦИКЛ api_create_set вынесен целиком (tp1/галереи/текст/product)

Дополнение 27: диспатч-цикл создания `for _ci, it in enumerate(items)` разнесён по семействам.
Из `api_create_set` вынесены ВСЕ ветки создания РК:
- **`create_set_tp1.py`** — `run_create_set_tp1(...)`: tp1_rsy/tp1_shopping РСЯ (ЕПК v501 network_cpa),
  fan-out по фидам, resume-skip пофидово, api→cookie fallback (152), корректировки ставок.
- **`create_set_gallery.py`** — `run_create_set_gallery(kind=tp5|tp3, ...)`: tp5 «Поиск+Товарная
  галерея» и tp3 «Товарная галерея РСЯ» (общий control-flow, различие — tp_code/дефолты cpa-budget/fn).
- **`create_set_text.py`** — `run_create_set_text(tp_code=tp2|tp4, ...)`: текстовые по cookie/Grid
  (#1). Историческая v5/v501-ветка (за `if True: … continue`, недостижима) при выносе ОПУЩЕНА как
  мёртвый код (в git история есть).
- **`_run_master_product_item(...)`** — tp6 МК / tp7 Товарка. Вынесен НЕ в отдельный файл, а
  module-level функцией в blueprint.py: ветка тянет 50+ module-global helper'ов и мутирует общий
  ленивый кэш `_tp7_mf` между item'ами → DI/отдельный модуль = 50+ параметров = высокий риск. Здесь
  все helper'ы резолвятся через globals модуля; в параметры (22) уходят только loop-local; кэш
  `_tp7_mf` возвращается наружу (roundtrip).

Первые три — pure-модули с DI (как `run_create_set_precreate`). call-site каждой ветки сжат до
`results.extend(...)`; тело цикла в `api_create_set` уменьшилось на ~700 строк.

**Верификация дополнения 27:**
- локально `py_compile` всех 4 файлов OK;
- synthetic smoke: tp1 **7/7**, gallery **7/7**, text **8/8** (guard/токен/куки/152-fallback/skip/дефолты
  cpa-budget/минус-режимы campaign|shared|group/defer);
- product (не юнит-тестируем — 50+ реальных deps): строгая **symtable free-var проверка** — 67
  global-ссылок функции ВСЕ резолвятся в module-globals/builtins, забытых параметров нет (главный риск
  экстракции снят статически);
- Деплой LXC101 через Tailscale (`lxc101-ts`; LAN недоступен — не дома, Mutagen синкает): md5
  Mac==LXC101 по всем 4 файлам, remote py_compile OK, серверный runtime-импорт всех модулей + blueprint
  в venv OK (`_run_master_product_item` присутствует, 22 параметра);
- очередь пустая (0 на Victory), `direct.service` restart active, smoke `automation:302`, свежие логи
  без traceback/500/NameError.

⚠ **Живой прогон НЕ делался** (создаёт реальные драфт-РК в Директе) — Семён просил «делай всё, ПОТОМ
прогон». Следующий шаг: end-to-end создание набора (tp1..tp7) на porg-psm5h7q6 + `/code-review`.

Дополнение 26: подготовка account context/templates/regions вынесена из `api_create_set` в
`create_set_account.py`. Новый модуль содержит `prepare_create_set_account(...)` и
`validate_create_set_content(...)`: грузит аккаунт, проверяет домен, применяет `site_type` override,
получает шаблоны, собирает `href`, `region_ids` и даёт hard-fail, если нет ни item-контента, ни
шаблонов.

**Как было:** `api_create_set` сам проверял аккаунт/домен, выбирал `eff_site`, грузил шаблоны,
собирал `href/region_ids` и проверял наличие контента.
**Как стало:** подготовка аккаунта и базового контекста создания изолирована; следующий вынос цикла
`tp1-tp7` будет получать уже нормализованный context.

**Верификация дополнения 26:** локально `py_compile create_set_account.py blueprint.py` OK; synthetic
smoke проверил account ok с `site_type=kviz`, `region_ids=[1,2]`, missing account `404`, empty domain
`400`, missing content/templates error. Деплой на LXC101: очереди перед рестартом пустые, md5 совпал,
remote compile OK, `direct.service` active, smoke `create_repair:401` и `automation:302`, свежие логи
без traceback/500. Серверный synthetic smoke проверил те же ветки. Реальная тестовая tp6-кампания
`711975398` на `porg-psm5h7q6` осталась `status=pass`, issues=[], repair_plan empty.

Дополнение 25: применение campaign-контента из `direct_slepok_content` вынесено из `api_create_set`
в `create_set_slepok_content.py`. Новый pure-модуль `apply_slepok_campaign_content(...)` применяет
контент только при `content_source=slepok_library`, ротирует заголовки/тексты/ссылки по РК,
не перетирает уже заданные `titles/texts/sitelinks`, вызывает `unify_utp_numbers` для согласованного
УТП и возвращает note для ответа.

**Как было:** `api_create_set` сам доставал БД-слепок, ротировал контент и вызывал UTP-unify.
**Как стало:** применение контента выбранного слепка изолировано; это упрощает контроль совпадения
оффера в контенте и промо и готовит дальнейший вынос создания `tp1-tp7`.

**Верификация дополнения 25:** локально `py_compile create_set_slepok_content.py blueprint.py` OK;
synthetic smoke проверил применение `T1 U/X1 U`, сохранение существующих `titles/texts`, добавление
missing `sitelinks` и fallback note при отсутствии записи в `direct_slepok_content`. Деплой на LXC101:
очереди перед рестартом пустые, md5 совпал, remote compile OK, `direct.service` active, smoke
`create_repair:401` и `automation:302`, свежие логи без traceback/500. Серверный synthetic smoke
проверил те же ветки. Реальная тестовая tp6-кампания `711975398` на `porg-psm5h7q6` осталась
`status=pass`, issues=[], repair_plan empty.

Дополнение 24: нормализация входа `create_set` вынесена из `api_create_set` в
`create_set_input.py`. Новый pure-модуль содержит `normalize_callouts(...)`, `first_feed_items(...)`
и `normalize_create_set_input(...)`: собирает login/items/agent/content_source/flags, semantic-dedup
уточнений и single-feed фильтр по первому `feed_id`.

**Как было:** `api_create_set` сам разбирал body, дедуплицировал уточнения и фильтровал items по
первому фиду.
**Как стало:** входной слой отделён и тестируется без Flask/Direct; начало маршрута стало меньше, а
правило single-feed и callouts dedup находится в одном месте.

**Верификация дополнения 24:** локально `py_compile create_set_input.py blueprint.py` OK; synthetic
smoke проверил semantic-dedup `['КАСКО','Трейд-ин']`, single-feed результат
`['no-feed','f1a','f1b','no-feed2']`, parsing `counter_id=1/goal_id=2/cpa=3000`. Деплой на LXC101:
очереди перед рестартом пустые, md5 совпал, remote compile OK, `direct.service` active, smoke
`create_repair:401` и `automation:302`, свежие логи без traceback/500. Серверный synthetic smoke
проверил те же ветки. Реальная тестовая tp6-кампания `711975398` на `porg-psm5h7q6` осталась
`status=pass`, issues=[], repair_plan empty.

Дополнение 23: подготовка Метрики перед `create_set` вынесена из `api_create_set` в
`create_set_metrika.py`. Новый helper `prepare_metrika(...)` делает fallback счётчика/цели из
`metrika_goals`, добирает цель через `_goal_vse_formy`, разрешает тестовый `via_cookie+no_cpa`
без Метрики и выполняет foreign-owner guard до `campaigns.add`.

**Как было:** fallback/validation Метрики и ошибка “счётчик принадлежит другому аккаунту” жили прямо
в маршруте `api_create_set`.
**Как стало:** подготовка Метрики изолирована; ошибка чужого счётчика/цели ловится до создания
кампаний, а тестовый CPC-only режим явно возвращает `metrika_note`.

**Верификация дополнения 23:** локально `py_compile create_set_metrika.py blueprint.py` OK; synthetic
smoke проверил fallback `counter=123/goal=456`, fallback цели `goal=789`, optional
`via_cookie+no_cpa`, missing counter error и foreign-owner error. Деплой на LXC101: очереди перед
рестартом пустые, md5 совпал, remote compile OK, `direct.service` active, smoke `create_repair:401`
и `automation:302`, свежие логи без traceback/500. Серверный synthetic smoke проверил те же ветки.
Реальная тестовая tp6-кампания `711975398` на `porg-psm5h7q6` осталась `status=pass`, issues=[],
repair_plan empty.

Дополнение 22: сборка публичного JSON-ответа `create_set` вынесена из `api_create_set` в
`create_set_response.py`. Новый pure-модуль `build_create_set_response(...)` сохраняет контракт
полей `created/failed/results/promo/callouts/units/precreate/verification/live_verification/repair_gate/auto_repair`.

**Как было:** `api_create_set` вручную собирал большой dict ответа в конце маршрута.
**Как стало:** контракт ответа живёт в отдельном helper-е и тестируется независимо от Flask.

**Верификация дополнения 22:** локально `py_compile create_set_response.py blueprint.py` OK;
synthetic smoke проверил полный набор ключей, `units_exhausted=True` при `units_switched`, и
`units_pending=0`, если `units_block=false`. Деплой на LXC101: очереди перед рестартом пустые, md5
совпал, remote compile OK, `direct.service` active, smoke `create_repair:401` и `automation:302`,
свежие логи без traceback/500. Серверный synthetic smoke проверил ключи ответа и units-поля. Реальная
тестовая tp6-кампания `711975398` на `porg-psm5h7q6` осталась `status=pass`, issues=[],
repair_plan empty.

Дополнение 21: обработка лимита баллов Direct/error 152 частично вынесена из `blueprint.py` в
`create_set_units.py`. Новый pure-модуль содержит `is_units_exhausted(...)`, `units_in_result(...)`,
`units_failed_names(...)`, `count_created(...)`, `count_skipped_existing(...)`, `count_failed(...)`.
Он распознаёт 152/units как в top-level error, так и во вложенных `campaigns`.

**Как было:** regexp 152 и подсчёты created/skipped/failed жили внутри `blueprint.py`.
**Как стало:** правила “152 не permanent-fail, defer не failed, skipped не newly-created” вынесены в
отдельный модуль; это снижает риск ошибок при докрутке по куке и малом остатке Direct API units.

**Верификация дополнения 21:** локально `py_compile create_set_units.py blueprint.py` OK; synthetic
smoke проверил top-level `152 not enough units`, nested `units: 0`, counts `created=1`,
`skipped=1`, `failed=1`, `units_failed_names={'units'}`. Деплой на LXC101: очереди перед рестартом
пустые, md5 совпал, remote compile OK, `direct.service` active, smoke `create_repair:401` и
`automation:302`, свежие логи без traceback/500. Серверный synthetic smoke проверил nested/top-level
units и `units_failed_names={'units','nested'}`. Реальная тестовая tp6-кампания `711975398` на
`porg-psm5h7q6` осталась `status=pass`, issues=[], repair_plan empty.

Дополнение 20: resume/skip matching вынесен из `api_create_set` в отдельный
`create_set_resume.py`. Новый pure-модуль содержит `already_in_direct(...)`, `force_recreate(...)`,
`item_matches_result_name(...)`, `items_for_result_names(...)`; все функции учитывают fan-out имена
вида `base — feed`. Этот helper используется для skip уже созданных кампаний, `_repair_force_names`,
deferred M3-докрутки и остатка после error 152.

**Как было:** exact/fan-out matching был размазан локальными функциями и list comprehension внутри
`api_create_set`.
**Как стало:** matching для resume/skip/repair общий и тестируемый отдельно; меньше риск дублей при
продолжении набора или докрутке после лимита баллов.

**Верификация дополнения 20:** локально `py_compile create_set_resume.py blueprint.py` OK; synthetic
smoke проверил exact match, fan-out existing, force recreate exact/fan-out и выбор items
`['tp5_cpc_site_y', 'tp7_cpc_site_z']`. Деплой на LXC101: очереди перед рестартом пустые, md5 совпал,
remote compile OK, `direct.service` active, smoke `create_repair:401` и `automation:302`, свежие логи
без traceback/500. Серверный synthetic smoke дал тот же matched список. Реальная тестовая
tp6-кампания `711975398` на `porg-psm5h7q6` осталась `status=pass`, issues=[], repair_plan empty.

Дополнение 19: callouts result-helper вынесен из `api_create_set` в отдельный
`create_set_callouts.py`. Новый pure-модуль `build_callouts_note(...)` формирует note для verifier
и ответа: подтверждает уточнения только когда precreate реально вернул ids; если Grid-схема не дала
ids, возвращает пустую строку и не обещает успешную подготовку.

**Как было:** `api_create_set` сам решал, какой callouts-note показывать после precreate.
**Как стало:** правило “не подтверждать уточнения без ids” находится в отдельном тестируемом helper-е.

**Верификация дополнения 19:** локально `py_compile create_set_callouts.py blueprint.py` OK;
synthetic smoke проверил `None` без callouts, `1/2` при ids и `''` при precreate-note без ids.
Деплой на LXC101: очереди перед рестартом пустые, md5 совпал, remote compile OK, `direct.service`
active, smoke `create_repair:401` и `automation:302`, свежие логи без traceback/500. Серверный
synthetic smoke дал те же результаты. Реальная тестовая tp6-кампания `711975398` на
`porg-psm5h7q6` осталась `status=pass`, issues=[], repair_plan empty.

Дополнение 18: post-create промо orchestration вынесен из `api_create_set` в отдельный
`create_set_promo.py`. Новый модуль `attach_or_create_promo(...)` делает три ветки:
привязать `precreated_promo_id`, выбрать пригодное промо из аккаунта через `promotions.get` или
создать новое промо по выбранному слепку. Проверка пригодности промо остаётся через
`_promo_usable_for_content(...)`, поэтому промо с конфликтующим оффером не привязывается к кампаниям.
После выноса `blueprint.py` уменьшился до 15791 строки.

**Как было:** `api_create_set` сам держал поиск/фильтрацию/создание/привязку промо после создания РК.
**Как стало:** endpoint передаёт результаты создания в отдельный promo-модуль; правило “промо должно
совпадать с контентом выбранного слепка” стало локализовано и тестируется отдельно.

**Верификация дополнения 18:** локально `py_compile create_set_promo.py blueprint.py promo.py` OK;
synthetic smoke проверил ветки `precreated`, `existing usable promo`, `create from slepok`: attach ids
`(55, 101, 777)` к кампаниям `(1, 2, 3)`. Деплой на LXC101: очереди перед рестартом пустые, md5
совпал, remote compile OK, `direct.service` active, smoke `create_repair:401` и `automation:302`,
свежие логи без traceback/500. Серверный synthetic smoke дал те же три ветки. Реальная тестовая
tp6-кампания `711975398` на `porg-psm5h7q6` осталась `status=pass`, issues=[], repair_plan empty.

Дополнение 17: precreate orchestration вынесен из `api_create_set` в отдельный
`create_set_precreate.py`. Новый модуль `run_create_set_precreate(...)` вызывает
`precreate.build_precreate_report(...)` и `precreate.execute_precreate_assets(...)`, нормализует
результат в `report/promo_id/promo_note/promo_skipped/callout_ids/callouts_note` и гарантирует, что
ошибка precreate не ломает создание кампаний.

**Как было:** `api_create_set` сам строил precreate report, сам запускал execute assets и сам
разворачивал результат.
**Как стало:** endpoint получает готовый precreate result из отдельного wrapper-а; precreate-логика
дальше отделяется от маршрута и проще тестируется без Flask.

**Верификация дополнения 17:** локально `py_compile create_set_precreate.py blueprint.py
precreate.py` OK; synthetic smoke проверил happy path callouts: dedup `['One','One','Two']` →
`callout_ids [100, 101]`, note `precreate: уточнения подготовлены через Grid: 2/2`. Деплой на LXC101:
очереди перед рестартом пустые, md5 совпал, remote compile OK, `direct.service` active, smoke
`create_repair:401` и `automation:302`, свежие логи без traceback/500. Серверный synthetic smoke
дал `precreate planned [100, 101]`. Реальная тестовая tp6-кампания `711975398` на `porg-psm5h7q6`
осталась `status=pass`, issues=[], repair_plan empty.

Дополнение 16: post-create orchestration вынесен из `api_create_set` в отдельный
`create_set_postprocess.py`. Новый модуль `run_create_set_postprocess(...)` собирает static
verification, Grid-first live verification, repair-gate и безопасный post-create auto-repair через
callback-и `live_verification`, `repair_deps`, `post_verify`. `blueprint.py` больше не держит этот
inline-блок внутри endpoint-а; после выноса он уменьшился до 15841 строки.

**Как было:** внутри `api_create_set` рядом с созданием кампаний лежали verification, live-read,
repair-gate и auto-repair.
**Как стало:** endpoint передаёт результаты создания в отдельный orchestration-модуль, а сам
`blueprint.py` остается ближе к routing/create-flow.

**Верификация дополнения 16:** локально `py_compile create_set_postprocess.py blueprint.py
verifier.py repair_gate.py repair_auto.py` OK; synthetic smoke проверил, что live callback вызывается,
repair-gate строится, auto-repair не запускается без executable actions. Деплой на LXC101: очереди
перед рестартом пустые, md5 совпал, remote compile OK, `direct.service` active, smoke
`create_repair:401` и `automation:302`, свежие логи без traceback/500. Серверный synthetic smoke:
`postprocess pass empty [('porg-test', 1)]`. Реальная тестовая tp6-кампания `711975398` на
`porg-psm5h7q6` осталась `status=pass`, issues=[], repair_plan empty.

Дополнение 15: нормализация результата `create_set` вынесена из `live_verifier.py` в отдельный
`campaign_result.py`. Новый pure-модуль отвечает за `as_int`, `result_name`, `result_id`,
`campaign_kind` и `created_campaigns(...)`: разворачивает вложенные `campaigns`, пропускает
`skipped/defer/ok=false`, дедуплицирует строки и классифицирует `tp6/tp7` как `uac`.
`verification_service.py` теперь импортирует `created_campaigns` напрямую из этого модуля.
После выноса `live_verifier.py` уменьшился до 145 строк.

**Как было:** `live_verifier.py` держал и нормализацию create-result, и live-сверку.
**Как стало:** нормализация результата создания стала отдельным чистым модулем; live verifier остался
тонким read-only orchestrator-ом поверх уже нормализованных кампаний.

**Верификация дополнения 15:** локально `py_compile campaign_result.py live_verifier.py
verification_service.py repair_planner.py` OK; synthetic smoke проверил вложенные `campaigns`, dedup
и классификацию `tp1 -> v5`, `tp6/tp7 -> uac`; live verifier good-case дал `status=pass`.
Деплой на LXC101: очереди перед рестартом пустые, md5 совпал, remote compile OK, `direct.service`
active, smoke `create_repair:401` и `automation:302`, свежие логи без traceback/500. Серверный
synthetic smoke дал `created [(10, 'v5'), (11, 'uac'), (13, 'uac')]`. Реальная тестовая
tp6-кампания `711975398` на `porg-psm5h7q6` осталась `status=pass`, issues=[], repair_plan empty.

Дополнение 14: локальные проверки результата создания вынесены из `live_verifier.py` в отдельный
`local_result_verifier.py`. Новый pure-модуль `verify_local_result(...)` отвечает за ошибки,
которые видны ещё до live-read кабинета: `BUILD_ERROR`, `NO_ADGROUPS_REPORTED`,
`NO_ADS_REPORTED`, `SEARCH_NOT_FINALIZED`, `SHOPPING_NOT_FINALIZED`, `GRID_FINALIZE_WARN`,
`NAME_HAS_NULL_TOKEN`. После выноса `live_verifier.py` уменьшился до 211 строк.

**Как было:** общий live verifier одновременно обходил созданные кампании, сверял Grid/v5/UAC и
держал локальные build/result проверки.
**Как стало:** локальный результат создания проверяется отдельным модулем; общий verifier занимается
orchestration и делегирует локальные, Grid-content, campaign-state и UAC проверки в scoped-файлы.

**Верификация дополнения 14:** локально `py_compile local_result_verifier.py live_verifier.py
repair_planner.py uac_verifier.py grid_content_verifier.py campaign_state_verifier.py` OK; synthetic
smoke дал `BUILD_ERROR`, `NO_ADGROUPS_REPORTED`, `NO_ADS_REPORTED`, `SEARCH_NOT_FINALIZED`,
`GRID_FINALIZE_WARN`, `NAME_HAS_NULL_TOKEN` и repair `rebuild_missing_content`. Деплой на LXC101:
очереди перед рестартом пустые, md5 совпал, remote compile OK, `direct.service` active, smoke
`create_repair:401` и `automation:302`, свежие логи без traceback/500. Серверный synthetic smoke дал
те же issue-коды и repair. Реальная тестовая tp6-кампания `711975398` на `porg-psm5h7q6` осталась
`status=pass`, issues=[], repair_plan empty.

Дополнение 13: проверки live campaign state/name вынесены из `live_verifier.py` в отдельный
`campaign_state_verifier.py`. Новый pure-модуль `verify_campaign_state(...)` отвечает за
`NAME_MISMATCH` и `CAMPAIGN_ARCHIVED`, а общий `live_verifier.py` только передаёт туда найденный
Grid/v5 row. После выноса `live_verifier.py` уменьшился до 240 строк.

**Как было:** общий live verifier держал и orchestration, и проверку имени/архивного состояния.
**Как стало:** проверка live-row состояния изолирована; rename/recreate правила можно тестировать
отдельно от UAC/Grid content checks.

**Верификация дополнения 13:** локально `py_compile campaign_state_verifier.py live_verifier.py
repair_planner.py` OK; synthetic smoke: same name/state OFF → `pass`, name mismatch →
`NAME_MISMATCH` + repair `rename_campaign`, archived → `CAMPAIGN_ARCHIVED` + repair
`resume_or_recreate_campaign`, `uses_direct_units=false`. Деплой на LXC101: очереди перед
рестартом пустые, md5 совпал, remote compile OK, `direct.service` active, smoke 401/302,
серверный synthetic smoke дал тот же результат. Реальная тестовая tp6-кампания `711975398`
на `porg-psm5h7q6` осталась `status=pass`, issues=[], repair_plan empty.

Дополнение 12: Grid content checks для tp1-tp5 вынесены из общего `live_verifier.py` в отдельный
`grid_content_verifier.py`. Новый pure-модуль `verify_grid_content(...)` проверяет фактические
счётчики Grid: `NO_ADGROUPS_LIVE`, `NO_ADS_LIVE`, `ADGROUP_NAME_MISSING`. `live_verifier.py`
теперь только передаёт туда `grid_content_counts`; после выноса он уменьшился до 246 строк.

**Как было:** общий live verifier одновременно обходил результаты, сверял Grid/v5 existence,
проверял UAC-инварианты и держал tp1-tp5 content rules.
**Как стало:** tp1-tp5 Grid content rules изолированы в отдельном модуле, как уже сделано для
UAC/tp6-tp7; менять проверки групп/объявлений можно без правки общего orchestration.

**Верификация дополнения 12:** локально `py_compile grid_content_verifier.py live_verifier.py
repair_planner.py` OK; synthetic smoke: good Grid counts → `pass`, `adgroups=0`, `ads=0`,
`bad_adgroup_names=1` → `NO_ADGROUPS_LIVE`, `NO_ADS_LIVE`, `ADGROUP_NAME_MISSING`, repair
`rebuild_missing_content`, `uses_direct_units=false`. Деплой на LXC101: очереди перед рестартом
пустые, md5 совпал, remote compile OK, `direct.service` active, smoke 401/302, серверный
synthetic smoke дал тот же результат. Реальная тестовая tp6-кампания `711975398` на
`porg-psm5h7q6` осталась `status=pass`, issues=[], repair_plan empty.

Дополнение 11: UAC/tp6-tp7 post-create правила вынесены из общего `live_verifier.py` в отдельный
`uac_verifier.py`. `live_verifier.py` теперь отвечает за общий обход результатов и источников
Grid/v5/UAC, а `uac_verifier.verify_uac_detail(...)` содержит все UAC-инварианты: draft, pricing,
budget, goals/counters, regions/maps, UTM, recommendation flags, content counts, feed/model filter.
Заодно `repair_gate._UAC_REPLACE_CODES` дополнен новыми UAC-кодами
`UAC_REGION_MISSING`, `UAC_PRICING_MISMATCH`, `UAC_BUDGET_MISSING`,
`UAC_LIMIT_PERIOD_MISMATCH`, `UAC_MAPS_ENABLED`, `UAC_PRODUCT_MODEL_FILTER_MISSING`, чтобы repair
не только планировал recreate, но и удалял конкретный плохой UAC draft перед очередью recreate.

**Как было:** UAC-инварианты росли внутри общего live verifier, а часть новых issue-кодов не была
подключена к replace-gate.
**Как стало:** UAC-правила изолированы в отдельном модуле, `live_verifier.py` уменьшился с 358 до
258 строк, а repair gate видит новые UAC-коды как `uac_replace_campaigns`.

**Верификация дополнения 11:** локально `py_compile uac_verifier.py live_verifier.py
repair_gate.py repair_planner.py` OK; synthetic smoke: good UAC → `pass`, bad UAC с
`UAC_BUDGET_MISSING` + `UAC_MAPS_ENABLED` → actionable repair, `summarize_repair_gate(...)`
показывает `uac_replace_campaigns=1`, `uses_direct_units=false`. Деплой на LXC101: очереди перед
рестартом пустые, md5 совпал, remote compile OK, `direct.service` active, smoke 401/302,
серверный synthetic smoke дал тот же результат. Реальная тестовая tp6-кампания `711975398`
на `porg-psm5h7q6` осталась `status=pass`, issues=[], repair_plan empty.

Дополнение 10: live verification для UAC/tp6-tp7 теперь проверяет недельный бюджет. На живой
тестовой кампании `711975398` UAC detail подтвердил поля `week_limit=5000.0` и
`limit_period=week`, поэтому `uac_read.summarize_uac_detail(...)` нормализует `week_limit` и
`limit_period`. `live_verifier` выдаёт `UAC_BUDGET_MISSING`, если `week_limit <= 0`, и
`UAC_LIMIT_PERIOD_MISMATCH`, если период не `week`. Если поле `week_limit` в detail отсутствует,
проверка не падает, чтобы не создавать false-positive на вариантах схемы.

**Как было:** post-create verifier не видел кампанию с нулевым недельным бюджетом или неверным
периодом бюджета.
**Как стало:** подтверждённый UAC budget detail проверяется автоматически; ошибки уходят в
`resume_or_recreate_campaign` через cookie/Grid без Direct API units.

**Верификация дополнения 10:** read-only detail на `porg-psm5h7q6`/`711975398` показал
`week_limit=5000.0`, `limit_period=week`. Локально `py_compile uac_read.py live_verifier.py
repair_planner.py` OK; synthetic smoke: `week_limit=5000, limit_period=week` → `pass`,
`week_limit=0` → `UAC_BUDGET_MISSING`, `limit_period=day` →
`UAC_LIMIT_PERIOD_MISMATCH`, отсутствующий `week_limit` → `pass`. Деплой на LXC101: очереди
перед рестартом пустые, md5 совпал, remote compile OK, `direct.service` active, smoke 401/302,
серверный synthetic smoke дал тот же результат. Реальная тестовая tp6-кампания `711975398`
осталась `status=pass`, issues=[], repair_plan empty.

Дополнение 9: live verification для UAC/tp6-tp7 теперь проверяет гео и выключенные карты.
`uac_read.summarize_uac_detail(...)` нормализует `regions` и `yandex_maps_enabled`.
`live_verifier` выдаёт `UAC_REGION_MISSING`, если в UAC detail нет регионов, и
`UAC_MAPS_ENABLED`, если включены показы/объект на Яндекс Картах. Оба кода подключены к
`repair_planner` как `resume_or_recreate_campaign` через cookie/Grid без Direct API units.

**Как было:** verifier видел контент, Метрику и UTM, но не ловил черновик без географии или с
включёнными картами, поэтому часть “галочек” оставалась ручной проверкой.
**Как стало:** отсутствие регионов и включённые карты попадают в post-create report и repair plan.

**Верификация дополнения 9:** локально `py_compile uac_read.py live_verifier.py repair_planner.py`
OK; synthetic smoke: регионы есть/карты выключены → `pass`, регионы пустые →
`UAC_REGION_MISSING`, карты включены → `UAC_MAPS_ENABLED`; repair actions
`resume_or_recreate_campaign`, `uses_direct_units=false`. Деплой на LXC101: очереди перед
рестартом пустые, md5 совпал, remote compile OK, `direct.service` active, smoke 401/302,
серверный synthetic smoke дал тот же результат. Существующая тестовая tp6-кампания `711975398`
на `porg-psm5h7q6` осталась `status=pass`, issues=[], repair_plan empty.

Дополнение 8: live verification для UAC/tp6-tp7 теперь проверяет режим оплаты по имени кампании.
`uac_read.summarize_uac_detail(...)` нормализует `pricing`, а `live_verifier` сравнивает его с
префиксом: `*_cpc_*` должен быть `PER_CLICK`, `*_cpa_*` должен быть `PER_CONVERSION` или
`PER_ACTION`. Несовпадение даёт `UAC_PRICING_MISMATCH`; `repair_planner` строит
`resume_or_recreate_campaign` через cookie/Grid, без Direct API units.

**Как было:** кампания могла называться `tp6_cpc...`, но фактически быть созданной в режиме оплаты
за конверсии, и post-create verifier этого не видел.
**Как стало:** режим оплаты сверяется автоматически по UAC detail; неправильный режим попадает в
repair plan без ручного захода в интерфейс.

**Верификация дополнения 8:** локально `py_compile uac_read.py live_verifier.py repair_planner.py`
OK; synthetic smoke: `cpc + PER_CLICK` → `pass`, `cpc + PER_CONVERSION` →
`UAC_PRICING_MISMATCH`; `cpa + PER_CONVERSION/PER_ACTION` → `pass`, `cpa + PER_CLICK` →
`UAC_PRICING_MISMATCH`; repair action `resume_or_recreate_campaign`, `uses_direct_units=false`.
Деплой на LXC101: очереди перед рестартом пустые, md5 совпал, remote compile OK,
`direct.service` active, smoke 401/302, серверный synthetic smoke дал тот же результат.
Существующая тестовая tp6-кампания `711975398` на `porg-psm5h7q6` осталась `status=pass`,
issues=[], repair_plan empty.

Дополнение 7: live verification для tp1-tp5 теперь проверяет не только количество групп/объявлений,
но и качество названий групп. `GridReadClient.campaign_content_counts(...)` читает `adGroups.name`
через cookie/Grid и возвращает `bad_adgroup_names` + примеры. `live_verifier` выдаёт
`ADGROUP_NAME_MISSING`, если имя группы пустое, содержит `None/null/undefined` или заканчивается
голым тире/длинным тире (`ct0001... —`). `repair_planner` относит этот код к
`rebuild_missing_content` без Direct API units.

**Как было:** live layer видел, что группы созданы, но не замечал, что в интерфейсе они выглядят
как кодер без человеческого хвоста (`... —`), поэтому такую ошибку приходилось искать руками.
**Как стало:** плохие имена групп попадают в post-create report как ошибка и получают repair action
на добивку/пересборку content через cookie/Grid.

**Верификация дополнения 7:** локально `py_compile grid_read.py live_verifier.py repair_planner.py`
OK; synthetic smoke: `ct0001_aoff —` → bad, `ct0001_aoff — BAIC X35` → ok; live verifier с
`bad_adgroup_names=1` вернул `ADGROUP_NAME_MISSING`, repair action `rebuild_missing_content`,
`uses_direct_units=false`. Деплой на LXC101: очереди перед рестартом пустые, md5 совпал,
remote compile OK, `direct.service` active, smoke 401/302, серверный synthetic smoke дал тот же
результат. Существующая тестовая tp6-кампания `711975398` на `porg-psm5h7q6` осталась
`status=pass`, issues=[], repair_plan empty.

Дополнение 6: live verification теперь ловит tp7 товарные кампании с конкретным `ct####`, где
товарный фильтр остался только по производителю (`vendor`) или отсутствует, а фильтр по модели
(`model`) не проставился. `uac_read.summarize_uac_detail(...)` нормализует поля UAC-фильтров
(`feed_filter_fields`, `has_model_filter`, `has_vendor_filter`, `has_collection_filter`), а
`live_verifier` выдаёт `UAC_PRODUCT_MODEL_FILTER_MISSING` для `tp7_*_ctXXXX_*`, кроме общих
`ct0000/ct0111`. `repair_planner` переводит этот код в `resume_or_recreate_campaign` через
cookie/Grid, без Direct API units.

**Как было:** post-create проверка tp7 подтверждала наличие фида, но не отличала корректный
фильтр модели от широкого vendor-only фильтра, из-за чего проблему “BAIC X35 без модели в фильтре”
можно было заметить только руками в интерфейсе.
**Как стало:** если в UAC detail у модельной tp7 есть только `vendor` или нет `model`, verifier
помечает кампанию ошибкой и строит repair plan на пересоздание/докрутку без расхода баллов.

**Верификация дополнения 6:** локально `py_compile uac_read.py live_verifier.py repair_planner.py`
OK; synthetic smoke: `model`-фильтр → `pass`, `vendor`-only → `fail` с
`UAC_PRODUCT_MODEL_FILTER_MISSING`, repair action `resume_or_recreate_campaign`,
`uses_direct_units=false`. Деплой на LXC101: очереди перед рестартом пустые, md5 совпал,
remote compile OK, `direct.service` active, smoke 401/302, серверный synthetic smoke дал тот же
результат. Существующая тестовая tp6-кампания `711975398` на `porg-psm5h7q6` осталась
`status=pass`, issues=[], repair_plan empty.

Дополнение 5: transient-сбой чтения UAC detail (`UAC_DETAIL_SKIPPED`) подключен к repair planner
как повторная read-only проверка, а не как пересоздание кампании. Это важно для экономии баллов:
если Grid/UAC detail временно не прочитался, система сначала повторяет live verification через
cookie/Grid без Direct API units.

**Как было:** `UAC_DETAIL_SKIPPED` мог остаться без repair action, и оператору приходилось
разбирать вручную, временный это сбой чтения или реальный дефект кампании.
**Как стало:** repair plan отдаёт `retry_live_verification` с `uses_direct_units=false`; только
после повторной проверки реальные UAC-инварианты превращаются в recreate/repair действия.

**Верификация дополнения 5:** локально `py_compile repair_planner.py` OK; synthetic smoke
`UAC_DETAIL_SKIPPED` вернул `retry_live_verification`, `uses_direct_units=false`. Деплой на LXC101:
md5 совпал, remote compile OK, очереди перед рестартом пустые, `direct.service` active, smoke
401/302, логи без traceback/error. На сервере synthetic `UAC_DETAIL_SKIPPED` вернул
`retry_live_verification`, а фактическая live verification тестовой UAC-кампании `711975398`
на `porg-psm5h7q6` осталась `status=pass`, issues=[], repair_plan empty.

Дополнение 4: новые UAC invariant issue-коды теперь подключены к repair planner/gate.
`repair_planner._RECREATE_CODES` включает `UAC_NOT_DRAFT`, `UAC_COUNTER_MISSING`,
`UAC_GOAL_MISSING`, `UAC_UTM_MISSING`, `UAC_ALTERNATIVE_TEXTS_ENABLED`,
`UAC_RECOMMENDATIONS_ENABLED`, `UAC_PRICE_RECOMMENDATIONS_ENABLED`. `repair_gate._UAC_REPLACE_CODES`
тоже знает эти коды, поэтому repair перед queued recreate удалит конкретный плохой UAC-драфт,
а не пропустит его по существующему имени.

**Как было:** live verification мог найти, например, отсутствующий счетчик/цель или включенную
персонализацию в UAC, но repair plan был пустым для новых issue-кодов.
**Как стало:** такие ошибки дают deduped action `resume_or_recreate_campaign` с
`uses_direct_units=false`, а repair gate показывает `uac_replace_campaigns=1` для конкретного
campaign_id.

**Верификация дополнения 4:** локально `py_compile repair_planner.py repair_gate.py` OK; smoke:
на наборе новых UAC issue-кодов planner вернул 1 deduped `resume_or_recreate_campaign`,
`executable_uac_replace_campaigns(...)` вернул campaign `711975398`, gate summary:
`queued_recreate_items=1`, `uac_replace_campaigns=1`, `uses_direct_units=false`. Деплой на LXC101:
md5 совпал, remote compile OK, очереди перед рестартом пустые, `direct.service` active, smoke
401/302, логи без traceback/error. На `porg-psm5h7q6` без новых кампаний: synthetic
`UAC_COUNTER_MISSING` строит actionable repair, фактический live verifier для `711975398` остаётся
`status=pass`, repair_plan empty.

Дополнение 3: `verifier.py` больше не создаёт `CALLOUTS_NOT_CONFIRMED` для UAC-only наборов
(`tp6_`/`tp7_`). Callouts — это campaign-level EPK/Grid asset для tp1-tp5; для Мастера/Товарки
UAC полноценность проверяется через UAC detail (titles/texts/sitelinks/media/counters/goals/UTM/
flags). Новый helper `_callouts_relevant(...)` оставляет warning для смешанных наборов и tp1-tp5,
но подавляет его для чистого tp6/tp7.

**Как было:** после успешного tp6 теста static verification мог возвращать warning/repair
`ensure_callouts`, хотя live verification уже подтверждал полноценный UAC-черновик.
**Как стало:** UAC-only набор не получает ложный callouts warning; mixed/tp1-tp5 по-прежнему
получают `CALLOUTS_NOT_CONFIRMED`, если выбранные callouts не подтверждены.

**Верификация дополнения 3:** локально `py_compile verifier.py` OK; smoke: UAC-only → `pass`,
tp1/mixed → `CALLOUTS_NOT_CONFIRMED`. Деплой на LXC101: md5 совпал, remote `py_compile` OK,
очереди перед рестартом пустые, `direct.service` active, smoke 401/302, логи без traceback/error.
На `porg-psm5h7q6` без создания новых кампаний: static verifier для `711975398`
вернул `status=pass`, issues=[], repair_plan empty; live verifier тоже `status=pass`, issues=[],
repair_plan empty.

Дополнение 2: усилена live-проверка UAC/tp6-tp7. `uac_read.summarize_uac_detail(...)` теперь
нормализует `status`, счетчики, цели и наличие tracking params, а `live_verifier` проверяет:
кампания должна быть `draft`, Метрика/цель должны быть заполнены, UTM/tracking params должны быть,
`alternative_texts_enabled`, `recommendations_management_enabled` и
`price_recommendations_management_enabled` должны быть выключены. Нарушения дают issue-коды
`UAC_NOT_DRAFT`, `UAC_COUNTER_MISSING`, `UAC_GOAL_MISSING`, `UAC_UTM_MISSING`,
`UAC_ALTERNATIVE_TEXTS_ENABLED`, `UAC_RECOMMENDATIONS_ENABLED`,
`UAC_PRICE_RECOMMENDATIONS_ENABLED`.

**Как было:** live verification для UAC проверял наличие кампании, контент 5/3/8, медиа и фид tp7,
но не проверял часть обязательных галочек/инвариантов.
**Как стало:** post-create live verification дополнительно проверяет draft-only, Метрику/цель,
UTM и ключевые UAC-инварианты без v5 units, через cookie/UAC detail.

**Верификация дополнения 2:** локально `py_compile uac_read.py live_verifier.py` OK; unit-smoke:
good UAC detail → `pass`, bad detail → все новые issue-коды. Деплой на LXC101: md5 совпал,
remote `py_compile` OK, очереди перед рестартом пустые, `direct.service` active, smoke 401/302,
логи без traceback/error. На `porg-psm5h7q6` существующий тестовый черновик `711975398`
дал summary: `status=draft`, titles=5, texts=3, sitelinks=8, content=4, counters=1, goals=1,
`has_tracking_params=true`, все recommendation/alternative flags `false`; live verification
остался `status=pass`, issues=0, repair_plan empty.

Дополнение: `api_create_set` больше не передаёт в `verifier` optimistic `callouts_note`, если
precreate не получил ни одного `precreated_callout_id`. Раньше основной ответ писал
«выбрано уточнений… вешаются именно они», даже когда live Grid вернул `{}` из-за недоступного
`AddCallouts`. Теперь при выбранных callouts и пустом id-пуле статический verifier честно отдаёт
`CALLOUTS_NOT_CONFIRMED` и repair action `ensure_callouts`, а поле ответа `precreate.callouts`
остаётся источником фактической причины.

**Как было:** callouts могли выглядеть подтверждёнными в итоговом `verification`, хотя precreate
их не создал/не нашёл.
**Как стало:** статический слой помечает callouts как неподтверждённые, если нет id; live layer
остаётся источником факта по уже созданным кампаниям.

**Верификация дополнения:** локально и на LXC101 `py_compile blueprint.py verifier.py` OK; md5
`blueprint.py` совпал после деплоя. Перед рестартом очереди `direct_automation_jobs`,
`direct_deferred_creates`, `direct_delayed_repairs` пустые; `direct.service` active; smoke:
`create_set_repair` без сессии → 401, `/direct/automation` → 302, логи без traceback/error.
Без создания новых кампаний: server-side `verify_create_set(... callouts_note='')` вернул
`status=warn` + `CALLOUTS_NOT_CONFIRMED` + repair action `ensure_callouts`; live verification
существующего тестового черновика `711975398` на `porg-psm5h7q6` остался `status=pass`,
issues=0.

`direct/precreate.py` теперь содержит не только side-effect-free `build_precreate_report(...)`,
но и `execute_precreate_assets(...)`: route `api_create_set` передает туда адаптеры Grid/v5,
а сам больше не держит большую ветку предсоздания промо/уточнений. Это уменьшает рост
`blueprint.py` и задаёт следующий seam для выноса минус-библиотек/изображений/AI-content.

Готовые `precreated_callout_ids` теперь прокидываются в `_resolve_campaign_assets`,
`_tp5_account_data`, cookie-пути tp1/tp2/tp3/tp5 и tp2/tp4 finalize, чтобы precreate ids не были
мертвой переменной. Live Grid показал, что мутация `AddCallouts` сейчас недоступна в приватной
схеме (`UnknownType GdAddCalloutsInput`), поэтому `GridClient.add_callouts(...)` безопасно
деградирует до reuse уже существующих callouts и возвращает `{}` без падения, если существующих нет.

`verifier._promo_ok(...)` исправлен: note вида `... пропущено конфликтных: N; привязано к M кампаниям`
теперь считается успешной привязкой промо, а не false-positive `PROMO_NOT_ATTACHED`.

**Как было:** precreate реально выполнялся внутри `api_create_set`, callout ids почти не
использовались дальше, verifier мог ругаться на уже привязанное промо.
**Как стало:** precreate-исполнение вынесено в модуль, готовые ids используются в downstream
ветках, Grid callouts не валят создание при неподдержанной мутации, статическая проверка промо
согласована с фактическим attach.

**Верификация:** локально `py_compile blueprint.py precreate.py verifier.py grid_finalize.py` OK.
Деплой на LXC101: md5 локальных/серверных файлов совпал, remote `py_compile` OK, очереди перед
рестартом пустые, `direct.service` active, smoke `/direct/api/create_set_repair` → 401 без сессии,
`/direct/automation` → 302, логи без traceback/error. Тестовый create_set на `porg-psm5h7q6`
создал 1 черновик UAC: `711975398` / `tp6_cpc_site_ct0021_aoff_n000_r0088_ct001_ag011_g00`;
precreate создал и привязал промо `1942631`; live verification через Grid/UAC details:
`status=pass`, issues=0, repair_plan empty. `GridClient.add_callouts(...)` на текущей схеме
проверен отдельно: не падает, возвращает `{}` при отсутствии существующих callouts.

## Последняя сессия: 2026-07-01 — guarded precreate callouts через Grid

`api_create_set` теперь до upload-цикла готовит выбранные `callouts` через
`gf.GridClient(login).add_callouts(...)`: тексты проходят `_dedup_callouts(..., cap=8)`, затем
создаются/находятся в библиотеке аккаунта без Direct API units. Результат пишется в
`precreate.callouts` (`ids/count/note/uses_direct_units=false`), а action `ensure_callouts` в
`precreate.actions` получает `status=done/skipped` и `callout_ids`.

**Как было:** выбранные уточнения создавались ближе к финализации конкретных кампаний; перед циклом
создания не было явного id-пула callouts.
**Как стало:** уточнения прогреваются до создания кампаний через Grid, поэтому последующие
finalize/repair шаги могут переиспользовать уже существующие callouts из библиотеки аккаунта.

**Верификация/уточнение 2026-07-01:** live Grid показал, что AddCallouts сейчас не поддерживается
(`UnknownType GdAddCalloutsInput`). После фикса этот шаг не падает и переиспользует только уже
существующие callouts; создание новых callouts через Grid остаётся отдельной задачей на поиск
актуальной приватной мутации или осознанный v5 fallback.

## Последняя сессия: 2026-07-01 — guarded precreate promo до upload-цикла

`api_create_set` теперь использует precreate-этап для промо с реальным эффектом, но guarded:
если `_st_token` есть, выбран `agent`, все `items` уже имеют `titles/texts` и `stream_content=false`,
до создания кампаний выполняется проверка библиотеки промо аккаунта. Пригодное промо сохраняется как
`precreated_promo_id`; если пригодного нет, создаётся промо через `_create_account_promo_from_slepok`
строго по выбранному слепку и с уже действующей сверкой процентов с контентом.

После upload-цикла post-create promo block, если `precreated_promo_id` есть, только привязывает этот
id к созданным кампаниям. Если precreate был пропущен (stream content, неполный контент, нет token),
работает прежний post-create путь. В `precreate` report action `ensure_promo_library` получает
`status=done/skipped` и `promo_id`, когда precreate promo реально отработал. Так как проверка
существующих промо делается через v5 `promotions.get`, actual report теперь честно помечает
`uses_direct_units=true` и `transport=v5_read_grid_create`.

**Как было:** промо искалось/создавалось только после создания кампаний, поэтому часть подготовки
оставалась в конце процесса.
**Как стало:** при готовом контенте промо создаётся/находится до upload-цикла, а после создания
кампаний только привязывается готовый `promo_id`; при потоковой генерации старый безопасный путь
сохранён, чтобы оффер не расходился с финальным контентом.

**Верификация:** локально `py_compile blueprint.py precreate.py` OK; изменённые участки проверены
чтением: guarded precreate расположен после bulk Grid-read existing names и до цикла `for items`,
post-create promo имеет ветку attach для `precreated_promo_id` и прежний fallback, если id нет.
Code-review gate нашёл и исправил неверный `uses_direct_units=false` для precreate promo.
Live создание кампаний не запускалось.

## Последняя сессия: 2026-07-01 — добавлен pre-create planning слой

Добавлен `direct/precreate.py`: Flask-free и side-effect-free слой, который описывает, какие ресурсы
должны быть готовы перед загрузкой кампаний. Сейчас он строит report по `body/items/account`:
`read_existing_campaign_names`, `ensure_promo_library`, `ensure_callouts`, `ensure_minus_libraries`,
`prefetch_images`, `prefetch_ai_content`; planning-дефолт не тратит Direct units, а actual
`api_create_set` позже переопределяет `uses_direct_units` для шагов, где реально был v5-read.

`api_create_set` после bulk Grid-read существующих имён кампаний вызывает
`precreate.build_precreate_report(...)` и возвращает report в поле `precreate`. Это не меняет
мутации создания: слой пока планирует/фиксирует precreate-этап и создаёт явную точку, куда дальше
будут переноситься реальные предсоздания промо/минусов/изображений из монолита.

**Как было:** часть подготовки существовала неявно внутри `blueprint.py` и не была видна в ответе
job; перед созданием нельзя было понять, какие ресурсы уже прогреты/запланированы.
**Как стало:** у create_set появился отдельный precreate report перед upload-циклом, без затрат
Direct units и без изменения текущего поведения кампаний.

**Верификация:** локально `py_compile precreate.py blueprint.py` OK; smoke
`build_precreate_report(...)` подтвердил actions `read_existing_campaign_names(done)`,
`ensure_promo_library`, `ensure_callouts`, `ensure_minus_libraries`, `prefetch_images`,
`prefetch_ai_content(active_in_create_set)`, `uses_direct_units=0`, а при неполном контенте warning
`PRECREATE_CONTENT_PARTIAL`.

## Последняя сессия: 2026-07-01 — автопромо строго по выбранному слепку и полное совпадение оффера

Добавлен `_selected_slepok_key(...)`: strict-normalizer для автопромо принимает только canonical
ключи (`pavlov/kryuchkova/scherbakova/terehov`) или явные UI-метки `Слепок_*`. Он не делает
подстрочный поиск фамилии, поэтому автопредсоздание/repair promo не может молча взять директолога
аккаунта по фамилии вместо выбранного пользователем слепка.

`_create_account_promo_from_slepok(...)` теперь использует `_selected_slepok_key(...)`; если слепок
не выбран явно, автопромо не создаётся и возвращает понятную причину. Основной `create_set` передаёт
в автопромо именно выбранный `agent`, нормализованный strict-путём.

`_promo_usable_for_content(...)` ужесточен: если в промо или в контенте есть процентные офферы,
множество процентов должно совпасть полностью. Промо `30%` больше не пройдёт на набор, где контент
содержит только `45%` или смесь `30%` и `45%`.

**Как было:** `_slepok_key_from_text(...)` мог распознать слепок по любой строке с фамилией, а
проверка промо принимала частичное пересечение процентов (`30%` проходило, если где-то в контенте
тоже был `30%`, даже при наличии `45%`).
**Как стало:** автопромо идёт только по явно выбранному слепку; процентный оффер промо и контента
должен совпасть целиком.

**Верификация:** локально `py_compile blueprint.py` OK. Импорт helper-ов локально не запускался,
потому что в локальном Python нет Flask; smoke strict-normalizer/percent-match нужно прогонять в
remote venv после деплоя.

## Последняя сессия: 2026-07-01 — static post-create audit расширен до body/build инвариантов

`verifier.py` теперь принимает optional `body` и проверяет resolved body после fallback из
`metrika_goals`: выбран ли слепок/agent, есть ли счётчик и цель, есть ли items, не пришёл ли
`launch=true` (он всё равно игнорируется, создаются только черновики). Для item-ов добавлены
проверки пустого `type`, `None/null/undefined` в названии, локального недобора content для UAC,
отсутствия feed-признаков у товарных item-ов. Для result-ов static audit теперь также смотрит
локальный `build/tp1_build/tp5_build`: `groups=0`/`ads=0` и build errors сразу дают issue-коды,
которые `repair_planner.py` уже превращает в `rebuild_missing_content` без Direct units.

`blueprint.py` передаёт в `verify_create_set(...)` не сырой request, а body с уже resolved
`items/agent/counter_id/goal_id/site_type`, чтобы не было ложной ошибки, когда счётчик/цель были
найдены серверным fallback-ом.

`repair_planner.py` теперь дедуплицирует repair actions по `action + campaign_id + name`: если одна
кампания одновременно получила `NO_ADGROUPS_REPORTED` и `NO_ADS_REPORTED`, выполняется один scoped
`rebuild_missing_content`, а не два одинаковых прохода. То же убирает дубль promo-action из пары
issue/candidate.

**Как было:** `verification` проверял в основном форму ответа, id, кодер-имена, промо/callouts;
ошибки “создали без выбранного слепка/Метрики/цели” и локальный `groups=0/ads=0` ловились только
позже live-проверкой или руками.
**Как стало:** дешёвый static audit сразу после создания видит body-инварианты и локальные build-
счётчики, а `groups=0/ads=0` уже попадает в repair plan как content repair.

**Верификация:** локально `py_compile verifier.py repair_planner.py blueprint.py` OK; smoke
`verify_create_set(...)` подтвердил: плохой body даёт `BODY_SLEPOK_MISSING`,
`BODY_COUNTER_MISSING`, `BODY_GOAL_MISSING`, `BODY_ITEMS_EMPTY`,
`BODY_LAUNCH_IGNORED_DRAFT_ONLY`; хороший resolved body проходит без BODY-ошибок; локальный
`build={'groups':0,'ads':0}` даёт `NO_ADGROUPS_REPORTED/NO_ADS_REPORTED` и repair actions
`rebuild_missing_content` с campaign id. Повторный smoke подтвердил dedupe: один promo-action на
набор и один content-repair action на кампанию при `NO_ADGROUPS+NO_ADS`.

## Последняя сессия: 2026-07-01 — guarded delayed content repair после Grid lag

Добавлен guarded delayed content repair для `rebuild_missing_content`.

`repair_auto.py` получил `delayed_content_repair_request(...)`: pure-helper планирует delayed repair
только для обычной parent job, если в `repair_plan` есть `in_place_content_repairs`; пропускает repair/
deferred jobs и тела с `_skip_auto_post_repair`/`_skip_delayed_content_repair`.

`blueprint.py` добавил таблицу `public.direct_delayed_repairs` и отдельный daemon с polling 60 сек.
После terminal `done` worker вызывает `_schedule_delayed_content_repair_after_done(...)`. Если план
содержит content repair, создаётся строка `content_repair` с задержкой 180 сек. Daemon перед мутацией
обязательно делает повторную `_create_set_live_verification(..., use_v5=False)`; если
`rebuild_missing_content` больше не подтверждается, строка помечается `skipped`. Если подтверждается,
выполняется `rex.execute_content_repair(...)`, затем post-repair live verification, а результат пишется
в `direct_delayed_repairs.result` и в parent job `result.delayed_content_repair`.

**Как было:** `rebuild_missing_content` намеренно не запускался сразу после создания из-за Grid lag;
для добивки пустых tp2/tp4/tp3/tp5 требовался ручной repair endpoint или отдельный запуск.
**Как стало:** content repair стал автоматическим, но guarded: он ждёт Grid lag, повторно проверяет
факт через Grid/cookie без v5 units и только после подтверждения добивает группы/объявления in-place.

**Верификация:** локально `py_compile blueprint.py repair_auto.py repair_gate.py repair_executor.py
verification_service.py` OK; smoke `delayed_content_repair_request(...)` подтвердил schedule для
actionable `rebuild_missing_content`, пропуск repair parent `_repair_parent_job_id`, пропуск
`_skip_delayed_content_repair`, ошибку без login (`scheduled=false`, `uses_direct_units=false`) и
`None` для плана без content repair.

## Последняя сессия: 2026-07-01 — live verification orchestration вынесен в `verification_service.py`

Добавлен `direct/verification_service.py`: Flask-free orchestration слой для read-only live
verification. Он собирает Grid campaigns через callback из blueprint, Grid content counts через
`grid_read.py`, UAC details через `uac_read.py`, optional v5 rows через token callback и передаёт всё
в `live_verifier.verify_live_create_set(...)`. Ошибки источников по-прежнему попадают в
`LIVE_SOURCE_ERRORS` и пересобирают `repair_plan`.

`blueprint._create_set_live_verification(...)` теперь тонкий adapter: готовит token callback и вызывает
`verification_service.verify_create_set_live(...)`. Внешний контракт функции не менялся, поэтому
`api_create_set`, verification endpoint, post-repair verification и repair endpoint используют тот же
путь.

**Как было:** `_create_set_live_verification(...)` в `blueprint.py` напрямую знал про `GridReadClient`,
`UacReadClient`, `DirectV501Client`, `live_verifier` и `repair_planner`.
**Как стало:** orchestration проверки вынесен в отдельный service-модуль; `blueprint.py` остался
HTTP/secret/callback adapter-ом. Grid/cookie-first поведение и экономия Direct units сохранены.

**Верификация:** локально `py_compile blueprint.py verification_service.py live_verifier.py
repair_planner.py` OK; smoke `verification_service.verify_create_set_live(...)` с fake
`GridReadClient/UacReadClient` подтвердил `checked.grid=true`, `checked.grid_content=true`,
`checked.uac_details=true`, `created_results=2`, `errors=0`; smoke с ошибкой Grid source добавил
`LIVE_SOURCE_ERRORS`; smoke `use_v5=true` без token также добавил `LIVE_SOURCE_ERRORS`.

## Последняя сессия: 2026-07-01 — no-safe-action response вынесен из endpoint

`repair_auto.py` получил `no_safe_action_response(...)`: helper формирует прежний `422` payload
для `api_create_set_repair execute=1`, когда в `repair_plan` нет действий, которые executor может
безопасно выполнить.

`api_create_set_repair` больше не собирает этот dict вручную и только возвращает
`jsonify(rauto.no_safe_action_response(...)), 422`.

**Как было:** в endpoint оставался последний ручной response-format для случая “нет безопасных
действий”.
**Как стало:** response contract вынесен в `repair_auto.py`; поведение и текст ошибки не менялись.

**Верификация:** локально `py_compile blueprint.py repair_auto.py repair_gate.py` OK; smoke
`no_safe_action_response(...)` подтвердил прежний текст ошибки, `execute=false`, сохранение
`repair_plan` и срез `unsupported_actions` до 40 элементов.

## Последняя сессия: 2026-07-01 — формат queued recreate response вынесен из endpoint

`repair_auto.py` получил `recreate_queue_failure_response(...)` и
`recreate_queue_success_response(...)`. Они формируют прежние JSON-контракты для
`api_create_set_repair execute=1`: success с `ok/execute/job_id/new_job_id/login/queued_items/ahead/
transport/uses_direct_units/deleted_uac_campaigns/unsupported_actions/repair_plan` и failure со
статусом `502` при `error`, иначе `422`.

`api_create_set_repair` теперь не собирает эти dict-и вручную, а вызывает helper-ы из `repair_auto.py`.

**Как было:** endpoint знал форму ответа queued recreate и вручную собирал success/failure payload.
**Как стало:** response contract переехал в orchestration-модуль; Flask-слой только отдаёт `jsonify`.
Поведение полей и HTTP-статусов не менялось.

**Верификация:** локально `py_compile blueprint.py repair_auto.py repair_gate.py` OK; smoke
`recreate_queue_success_response(...)` подтвердил прежний набор полей success; smoke
`recreate_queue_failure_response(...)` подтвердил `502` при `error` и `422` без `error`.

## Последняя сессия: 2026-07-01 — preflight recreate repair вынесен из endpoint

`repair_auto.py` получил `recreate_queue_preflight(...)`: чистый helper выбирает
`resume_or_recreate_campaign` items и проверяет, есть ли уже активная job по тому же login.
Возвращает `items`, `unsupported_actions` и `conflict` без мутаций.

`api_create_set_repair execute=1` теперь не содержит ручного цикла по `_CREATE_JOBS` и прямого
вызова `repair_gate.executable_recreate_items(...)`: он берёт preflight из `repair_auto.py` под
существующим lock, затем либо возвращает прежний `409`, либо ставит recreate repair как раньше.

**Как было:** repair endpoint сам выбирал recreate items и сам знал правило “если по аккаунту есть
активная job, repair не ставить”.
**Как стало:** правило вынесено в `repair_auto.py`; Flask-слой только держит lock и отдаёт
готовый conflict/queue decision. Поведение ответа и статусы не менялись.

**Верификация:** локально `py_compile blueprint.py repair_auto.py repair_gate.py` OK; smoke
`recreate_queue_preflight(...)` подтвердил: пустой план даёт `items=[]/conflict=None`; actionable
план фильтрует items до `tp7_cpc_site`; terminal job по тому же login не конфликтует; running job по
тому же login возвращает `active_job_id=run`; running job другого login не конфликтует.

## Последняя сессия: 2026-07-01 — вынос queue orchestration для recreate repair

`repair_auto.py` получил `queue_recreate_repair_job(...)`: сервисный orchestration-helper выбирает
`resume_or_recreate_campaign` items, находит UAC replacements, вызывает переданный callback удаления
конкретных неполных UAC drafts, строит repair-body через `build_recreate_queue_body(...)`, ставит job
через callback `create_job(...)` и возвращает прежний ответ (`queued/new_job_id/queued_items/ahead/
transport/uses_direct_units/deleted_uac_campaigns/unsupported_actions`).

`blueprint._queue_recreate_repair_job(...)` теперь стал тонким adapter-ом: прокидывает
`_delete_uac_repair_campaigns`, `_job_new` и `_create_jobs_ahead` в `repair_auto`, а lock вокруг
`_create_jobs_ahead` остался в Flask-слое.

**Как было:** `_queue_recreate_repair_job(...)` в `blueprint.py` содержал выбор recreate items,
UAC replace flow, сбор repair-body, постановку job и форматирование ответа.
**Как стало:** алгоритм queue recreate переехал в `repair_auto.py`; `blueprint.py` хранит только
callback wiring к реальным UAC/job/lock side effects. Поведение ручного repair endpoint и
auto-after-done не менялось.

**Верификация:** локально `py_compile blueprint.py repair_auto.py repair_gate.py` OK; pure smoke
`queue_recreate_repair_job(...)` подтвердил успешную постановку `repair-job`, фильтрацию items до
одного `tp7_cpc_site`, `via_cookie=true`, `launch=false`, anti-loop флаги `true`, force names
`[полное tp7 feed-имя, базовое item-имя]`, `ahead=3`, `transport=cookie_grid`,
`uses_direct_units=false`; smoke с failed UAC delete вернул `queued=false` и не ставил job; пустой
план вернул `reason=no_recreate_items`.

## Последняя сессия: 2026-07-01 — вынос auto-recreate decision в `repair_auto.py`

`repair_auto.py` получил чистый helper `auto_recreate_request(parent_job_id, job_snapshot)`: он
решает, можно ли после terminal `done` автоматически ставить recreate repair, пропускает repair/
deferred/anti-loop jobs, проверяет `live_verification.repair_plan`, считает `repair_gate` summary,
готовит `login/ctx/plan/saved_session` для очереди и возвращает ошибку без мутаций, если в snapshot
нет login.

`blueprint._auto_queue_recreate_after_done(...)` теперь не разбирает структуру job/result сам, а
только вызывает `repair_auto.auto_recreate_request(...)` и, если request есть, ставит repair job через
существующий `_queue_recreate_repair_job(...)`.

**Как было:** часть decision-логики auto queued recreate оставалась в `blueprint.py`: skip-условия,
разбор live verification, summary и сбор ctx.
**Как стало:** decision переехал в `repair_auto.py`; Flask-слой оставлен для постановки job и серверных
lock/DB side effects. Поведение auto recreate не менялось.

**Верификация:** локально `py_compile blueprint.py repair_auto.py repair_gate.py` OK; pure smoke
`auto_recreate_request(...)` для actionable `resume_or_recreate_campaign` вернул `login`, `ctx`,
`parent_job_id`, `saved_session` и `summary.queued_recreate_items=1`; snapshot с
`_repair_parent_job_id` вернул `None`; snapshot без login вернул `queued=false`,
`uses_direct_units=false`. Локальный smoke wrapper-а `blueprint.py` не запускался из-за отсутствия
Flask в локальном Python; проверять на remote venv.

## Последняя сессия: 2026-07-01 — вынос подготовки queued recreate в `repair_auto.py`

`repair_auto.py` расширен чистыми helper-ами `recreate_force_names(...)` и
`build_recreate_queue_body(...)`. Они строят force-name список для UAC replace и тело async
repair-job с `via_cookie=true`, `launch=false`, `_repair_parent_job_id`,
`_skip_auto_post_repair=true`, `_skip_auto_queued_repair=true`, а также вычищают старые
`_job_id/_resume_count/_deferred_id` через существующий `repair_gate.repair_queue_body(...)`.

`blueprint.py` больше не содержит `_recreate_force_names(...)`: Flask-слой оставлен для I/O
(удалить конкретный UAC draft, поставить job, посчитать ahead), а подготовка repair-body переехала
в orchestration-модуль.

**Как было:** после добавления auto queued recreate часть чистой логики снова лежала в монолитном
`blueprint.py`, из-за чего файл продолжал расти.
**Как стало:** поведение repair queue не изменилось, но подготовка recreate repair вынесена в
`repair_auto.py`; в `blueprint.py` остался только серверный orchestration-код.

**Верификация:** локально `py_compile blueprint.py repair_auto.py repair_gate.py` OK; smoke
`build_recreate_queue_body(...)` для tp7 UAC replace вернул force names `[полное tp7 feed-имя,
базовое item-имя]`, `items=[tp7,tp1]`, `via_cookie=true`, `launch=false`,
`_repair_parent_job_id=parent`, оба anti-loop флага `true` и удалённые
`_job_id/_resume_count/_deferred_id`.

## Последняя сессия: 2026-07-01 — auto queued recreate после terminal done

Добавлен общий helper `_queue_recreate_repair_job(...)`: выбирает `resume_or_recreate_campaign`
из `repair_plan`, делает UAC replace-delete только для конкретных `UAC_*_MISSING` campaign ids,
строит `repair_queue_body(..., via_cookie=true, launch=false)` и ставит repair job в обычную очередь.
`/direct/api/create_set_repair execute=1` теперь использует этот helper вместо дублированного кода.

Worker после финального `_job_db_save(..., status=done)` вызывает `_auto_queue_recreate_after_done(...)`.
Он срабатывает только для обычной parent job: пропускает repair/resume jobs и тела с
`_skip_auto_queued_repair`, чтобы не было циклов. Результат записывается в
`result.auto_queued_repair`.

**Как было:** `resume_or_recreate_campaign` и `UAC_*_MISSING` появлялись в `repair_plan`, но для
постановки repair job всё ещё нужен был отдельный execute.
**Как стало:** после завершения основной job recreate/UAC-replace repair автоматически ставится в
очередь по cookie/Grid без v5 units; неполный UAC draft удаляется только по конкретному id из плана.

**Верификация:** локально `py_compile blueprint.py repair_auto.py repair_gate.py repair_executor.py`
OK; smoke `summarize_repair_gate(...)` для `UAC_FEED_MISSING` вернул
`queued_recreate_items=1`, `uac_replace_campaigns=1`, `uses_direct_units=false`. Remote md5
local==server по `blueprint.py`, `README.md`, `STATE.md`; remote `/root/venv/bin/python3 -m
py_compile blueprint.py repair_auto.py repair_gate.py repair_executor.py` OK; remote smoke
`_auto_queue_recreate_after_done(...)` с monkeypatch `_delete_uac_repair_campaigns/_job_new`
поставил `auto-repair-job`, добавил `via_cookie=true`, `launch=false`,
`_skip_auto_post_repair=true`, `_skip_auto_queued_repair=true`, force names `[полное tp7 feed-имя,
базовое item-имя]`, и пропустил snapshot с `_repair_parent_job_id`. Перед рестартом активных
jobs/deferred не было; выполнен `systemctl restart direct.service`; `direct.service active`,
`digest.service active`; public `POST /direct/api/create_set_repair` без сессии → `401`,
`/direct/automation → 302 /login`; traceback/exception/syntax/Jinja/500 ошибок после рестарта не
найдено; очереди после рестарта пустые. Live-создание кампаний не запускалось.

## Последняя сессия: 2026-07-01 — безопасная post-create автодобивка

Добавлен `direct/repair_auto.py` — orchestration-слой для repair executor-ов. Он вынес общий порядок
in-place добивки из `blueprint.py` и используется двумя местами:
- `POST /direct/api/create_set_repair execute=1` после queued recreate ветки вызывает
  `repair_auto.execute_next_in_place(...)`, сохраняя порядок: content → promo → callouts → rename;
- финал `api_create_set` после Grid-first `live_verification` запускает
  `repair_auto.execute_safe_post_create(...)` для безопасных идемпотентных действий
  `create_or_attach_promo`, `ensure_callouts`, `rename_campaign`.

`rebuild_missing_content` и `UAC_*_MISSING` намеренно не запускаются сразу после создания: content
может дать дубли из-за Grid lag, а UAC replace удаляет draft и должен идти только через scoped
repair queue.

**Как было:** после создания скрипт только показывал `repair_gate`, а даже безопасные промо/уточнения/
rename требовали отдельного ручного execute.
**Как стало:** обычный create job сам пробует добить promo/callouts/rename без v5 units и возвращает
результат в `auto_repair`; опасные/неидемпотентные действия остаются в `repair_plan`.

**Верификация:** локально `py_compile blueprint.py repair_auto.py repair_gate.py repair_executor.py`
OK; smoke `execute_safe_post_create(...)` с monkeypatch executor-ов вызвал только
`promo/callouts/rename` и пропустил content, а `execute_next_in_place(...)` для repair endpoint
сначала выбрал content. Remote md5 local==server по `blueprint.py`, `repair_auto.py`, `README.md`,
`STATE.md`; remote `/root/venv/bin/python3 -m py_compile blueprint.py repair_auto.py repair_gate.py
repair_executor.py` OK; remote smoke `repair_auto` подтвердил тот же порядок; remote Flask
route-smoke `/direct/api/create_set_repair execute=1` с monkeypatch `_create_set_live_verification` и
`execute_content_repair` вернул `200`, прошёл через `repair_auto.execute_next_in_place(...)` и выбрал
`content_kind=text` без реальных запросов в Яндекс. Перед рестартом активных jobs/deferred не было;
выполнен `systemctl restart direct.service`; `direct.service active`, `digest.service active`; public
`POST /direct/api/create_set_repair` без сессии → `401`, `/direct/automation → 302 /login`;
traceback/exception/syntax/Jinja/500 ошибок после рестарта не найдено; очереди после рестарта пустые.
Live-создание кампаний не запускалось.

## Последняя сессия: 2026-07-01 — `repair_gate` summary в результате create_set

Финальный ответ `api_create_set` теперь вместе с `live_verification` кладёт короткий
`repair_gate` summary из чистого `repair_gate.summarize_repair_gate(...)`: количество actions,
сколько можно выполнить сейчас, сколько уйдёт в queued recreate, content/promo/callout/rename
executor-ы, сколько UAC replace campaigns, endpoint и метод для запуска добивки.

**Как было:** live verification уже строил `repair_plan`, но UI/скрипту нужно было разбирать
весь план или отдельно идти в `/direct/api/create_set_repair`, чтобы понять, есть ли исполнимая
добивка.
**Как стало:** статус job сразу содержит компактный `result.repair_gate`, не трогает кампании,
не делает запросы в Яндекс и не расходует v5 units.

**Верификация:** локально `py_compile blueprint.py repair_gate.py` OK; pure smoke
`summarize_repair_gate(...)` на плане из recreate+content+promo+callouts+rename вернул
`actions=5`, `executable_now=5`, `uac_replace_campaigns=1`, `uses_direct_units=false`. Remote md5
local==server по `blueprint.py`, `repair_gate.py`, `README.md`, `STATE.md`; remote
`/root/venv/bin/python3 -m py_compile blueprint.py repair_gate.py` OK; remote smoke
`summarize_repair_gate(...)` вернул те же счётчики. Перед рестартом активных jobs/deferred не было;
выполнен `systemctl restart direct.service`; `direct.service active`, `digest.service active`; public
`POST /direct/api/create_set_repair` без сессии → `401`, `/direct/automation → 302 /login`;
traceback/exception/syntax/Jinja/500 ошибок после рестарта не найдено; очереди после рестарта пустые.
Live-создание кампаний не запускалось.

## Последняя сессия: 2026-07-01 — UAC replace-flow для `UAC_*_MISSING`

Repair-gate теперь не просто планирует неполные tp6/tp7 как `resume_or_recreate_campaign`, а умеет
безопасный replace-flow:
- `repair_gate.executable_uac_replace_campaigns(...)` выбирает только action-ы `UAC_*_MISSING`
  с `campaign_id` и именем `tp6_`/`tp7_`;
- `api_create_set_repair execute=1` перед постановкой repair-job удаляет только эти конкретные
  UAC draft ids через `/web-api/uac/campaign/{id}/`;
- repair body получает `_repair_force_names`, чтобы `RESUME-SKIP` не пропустил пересоздание из-за
  старого имени в Grid-кэше;
- force применяется только к UAC replacements, обычные recreate repair-ы не меняют поведение.

**Как было:** `UAC_*_MISSING` попадал в `resume_or_recreate_campaign`, но существующий неполный UAC
draft мог остаться в кабинете; при repair-job `RESUME-SKIP` видел имя и пропускал item, то есть
недобор не исправлялся.
**Как стало:** для неполного tp6/tp7 сначала удаляется конкретный плохой UAC draft, затем исходный item
пересоздаётся по cookie/UAC без v5 units и без глобального удаления всех черновиков.

**Верификация:** локально `py_compile blueprint.py repair_gate.py uac_read.py live_verifier.py
repair_planner.py` OK; pure smoke `executable_uac_replace_campaigns` выбрал только `UAC_FEED_MISSING`
для `tp7_...` и проигнорировал обычный `RESULT_FAILED`; smoke `repair_queue_body` подтвердил
`via_cookie=true`, `launch=false`, `_repair_force_names=['x']` и удаление старого `_job_id`. Remote
md5 local==server по `blueprint.py`, `repair_gate.py`, `README.md`, `EXTRACTION_PLAN.md`, `STATE.md`;
remote `/root/venv/bin/python3 -m py_compile ...` OK; remote Flask route-smoke
`/direct/api/create_set_repair execute=1` с monkeypatch `_delete_uac_repair_campaigns/_job_new`
вернул `200`, передал delete только `campaign_id=777`, поставил repair body с
`_repair_force_names=[полное tp7 feed-имя, базовое item-имя]`, `via_cookie=true`, `launch=false` и
`uses_direct_units=false`, без реальных запросов в Яндекс. Перед рестартом активных jobs/deferred не
было; выполнен `systemctl restart direct.service`; `direct.service active`, `digest.service active`;
public `POST /direct/api/create_set_repair` без сессии → `401`, `/direct/automation → 302 /login`;
traceback/exception/syntax/Jinja/500 ошибок после рестарта не найдено; очереди после рестарта пустые.
Live-создание кампаний не запускалось.

## Последняя сессия: 2026-07-01 — UAC detail live verification для tp6/tp7

Добавлен `direct/uac_read.py` — read-only слой для UAC tp6/tp7 через существующий
`campaign.UacClient` и `/web-api/uac/campaign/{id}`. Он нормализует только счётчики, нужные
для live verification: `titles`, `texts`, `sitelinks`, `content/images/videos`, `has_feed` и флаги
инвариантов. `_create_set_live_verification(...)` теперь читает UAC detail только для созданных
`kind=uac` campaign ids и передаёт `uac_details` в `live_verifier.verify_live_create_set`.

`live_verifier.py` добавляет read-only проверки UAC:
- tp6/tp7: минимум 5 заголовков, 3 текста, 8 быстрых ссылок, хотя бы один media/content;
- tp7: дополнительно наличие feed/ecom.

Недобор даёт `UAC_TITLES_MISSING`, `UAC_TEXTS_MISSING`, `UAC_SITELINKS_MISSING`,
`UAC_MEDIA_MISSING`, `UAC_FEED_MISSING`. `repair_planner.py` относит эти коды к
`resume_or_recreate_campaign`, а не к in-place content patch: безопасного частичного UAC patch-контракта
пока не используем.

**Как было:** UAC live verification проверял только существование tp6/tp7 в Grid; кампания могла быть
создана с неполным payload, а verifier не видел недобор контента/фида.
**Как стало:** live verification читает фактический UAC detail и строит конкретный repair plan на
пересоздание/докрутку через cookie/UAC без v5 units.

**Верификация:** локально `py_compile blueprint.py uac_read.py live_verifier.py repair_planner.py
repair_gate.py repair_executor.py` OK; smoke `verify_live_create_set` с неполным UAC detail
`titles=2/texts=1/sitelinks=3/content=0/has_feed=false` вернул коды `UAC_*_MISSING` и action
`resume_or_recreate_campaign` для `campaign_id=777`; smoke с полным detail
`titles=5/texts=3/sitelinks=8/content=2/has_feed=true` вернул `status=pass`. Remote md5 local==server
по `blueprint.py`, `uac_read.py`, `live_verifier.py`, `repair_planner.py`, `README.md`,
`EXTRACTION_PLAN.md`, `STATE.md`; remote `/root/venv/bin/python3 -m py_compile ...` OK; remote
smoke `_create_set_live_verification` с monkeypatch `_grid_list_campaigns/UacReadClient` вернул
`checked.uac_details=true`, issues `UAC_TITLES_MISSING/UAC_TEXTS_MISSING/UAC_SITELINKS_MISSING/
UAC_MEDIA_MISSING/UAC_FEED_MISSING` и repair action `resume_or_recreate_campaign` без реальных
запросов в Яндекс. Перед рестартом активных jobs/deferred не было; выполнен `systemctl restart
direct.service`; `direct.service active`, `digest.service active`; public repair endpoint без сессии
→ `401`, `/direct/automation → 302 /login`; traceback/exception/syntax/Jinja/500 ошибок после рестарта
не найдено; очереди после рестарта пустые. Live-создание кампаний не запускалось.

## Последняя сессия: 2026-06-30 — post-repair live verification

In-place repair executor-ы (`rebuild_missing_content`, `create_or_attach_promo`, `ensure_callouts`,
`rename_campaign`) теперь после мутации сразу запускают повторную `_create_set_live_verification(...)`
с `use_v5=False` и добавляют в ответ `post_repair_live_verification` + `remaining_repair_plan`.
Очередной `resume_or_recreate_campaign` не трогается: это async job, там повторная сверка должна идти
после завершения новой job.

**Как было:** repair endpoint отвечал только результатом конкретной мутации; чтобы понять, осталось ли
что-то добивать, нужно было отдельно снова запускать repair-gate/live verification.
**Как стало:** после каждой in-place добивки ответ сразу содержит повторную Grid-first сверку и новый
остаточный план без расхода v5 units.

**Верификация:** локально `py_compile blueprint.py grid_create.py repair_gate.py repair_executor.py`
OK. Локальный import-smoke helper-а не запускался из-за отсутствия Flask в локальном окружении; remote
md5 local==server по `blueprint.py`, `README.md`, `STATE.md`; remote `/root/venv/bin/python3 -m
py_compile ...` OK; remote Flask route-smoke через production app `direct.main` с monkeypatch
подтвердил два live-вызова `[False, False]` (план до repair и post-repair сверка без v5), ответ
содержал `post_repair_live_verification.status=ok` и `remaining_repair_plan.status=empty`. Перед
рестартом активных jobs/deferred не было; выполнен `systemctl restart direct.service`;
`direct.service active`, `digest.service active`; public repair endpoint без сессии → `401`,
`/direct/automation → 302 /login`; traceback/exception/syntax/Jinja/500 ошибок после рестарта не
найдено; очереди после рестарта пустые. Live-создание кампаний не запускалось.

## Последняя сессия: 2026-06-30 — repair executor v6: content-in-place для tp3/tp5

`rebuild_missing_content` расширен на товарные `search_gallery/rsya_gallery` (tp5/tp3). Добавлен
`grid_create.add_shopping_content_to_existing(...)`: через cookie/Grid добавляет группу в существующий
`campaign_id`, создаёт ShoppingAd по фиду, затем ListingAd из ShoppingAd и ставит `name`-фильтр
листинга. Фильтры сохраняют разделение:
- ShoppingAd: `vendor` для марки + `model` для модельных ct;
- ListingAd: `name` = марка или марка+модель.

`repair_gate.executable_content_repairs(...)` теперь помечает `content_kind=shopping` для
`search_gallery/rsya_gallery`, а `repair_executor.execute_content_repair(...)` выбирает text/shopping
ветку по этому флагу. В `blueprint.py` добавлен `_repair_shopping_content_context(...)`: он берёт
`feed_id`, регион, `ct`, кодер-имя группы, vendor/model/listing_name и текст из сохранённой job/body.

**Как было:** `rebuild_missing_content` для tp3/tp5 оставался unsupported, а добивка пустой товарной
кампании требовала ручной проверки или полного пересоздания; модельный фильтр был критичным местом.
**Как стало:** пустая tp3/tp5 может добиваться in-place без Direct units: группа + ShoppingAd +
ListingAd создаются в существующей кампании, а модельные группы получают `model` на товарах и
`name=марка модель` на страницах каталога.

**Верификация:** локально `py_compile blueprint.py grid_create.py repair_gate.py repair_executor.py`
OK; smoke с fake `GridCreateClient/GridClient` подтвердил, что для `BAIC X35` ShoppingAd получает
`vendor=BAIC` и `model` со значением `X35`, `set_default_text` сохраняет оба условия, а ListingAd
получает `name`-фильтр `baic x35`; smoke `executable_content_repairs` выбрал `search_gallery` как
`content_kind=shopping`; smoke `execute_content_repair` с monkeypatch вернул `status=200`,
`repaired_campaign_ids=[222]`, `uses_direct_units=false`. Remote md5 local==server по
`blueprint.py`, `grid_create.py`, `repair_gate.py`, `repair_executor.py`, `README.md`,
`EXTRACTION_PLAN.md`, `STATE.md`; remote `/root/venv/bin/python3 -m py_compile ...` OK; remote
Flask route-smoke через production app `direct.main` с monkeypatch `_create_set_job_context`,
`_create_set_live_verification`, `execute_content_repair` вернул `200`, `repaired_campaign_ids=[222]`,
`uses_direct_units=false` и передал `content_kind=shopping` для item `search_gallery`, без реальных
запросов в Яндекс. Перед рестартом активных jobs/deferred не было; выполнен `systemctl restart
direct.service`; `direct.service active`, `digest.service active`; public repair endpoint без сессии
→ `401`, `/direct/automation → 302 /login`; traceback/exception/syntax/Jinja/500 ошибок после рестарта
не найдено; очереди после рестарта пустые. Live-создание кампаний не запускалось.

## Последняя сессия: 2026-06-30 — repair executor v5: content-in-place для tp2/tp4

`POST /direct/api/create_set_repair execute=1` получил scoped executor для `rebuild_missing_content`
по пустым tp2/tp4. Добавлен низкоуровневый `grid_create.add_text_content_to_existing(...)`: он через
Grid/cookie добавляет `AddUnifiedAdGroups` + `AddAdaptiveTextAds` в уже существующий `campaign_id`,
кампанию не пересоздаёт и Direct API units не расходует.

`repair_gate.executable_content_repairs(...)` выбирает только безопасные text items
`search_test/search_dynamic` из сохранённой job; товарные tp3/tp5 и UAC tp6/tp7 остаются unsupported
до отдельного билда. `repair_executor.execute_content_repair(...)` вызывает общий Grid-движок и
возвращает `repaired_campaign_ids`, groups/ads/ad ids и частичные ошибки. В `blueprint.py` добавлен
тонкий `_repair_text_content_context(...)`: он берёт выбранный слепок, site_type, регион, домен и
контент из сохранённой job, собирает те же M3-группы через `_pack_groups_with_retry`, что обычное
создание, и отдаёт executor-у готовые данные.

**Как было:** live-сверка уже видела `NO_ADGROUPS_LIVE/NO_ADS_LIVE` и планировала
`rebuild_missing_content`, но `execute=1` не мог исправить пустой существующий черновик; повторная
очередь могла сработать как `RESUME-SKIP` и пропустить кампанию по имени.
**Как стало:** для tp2/tp4 repair добивает группы и объявления прямо в существующую кампанию по
`campaign_id`, cookie/Grid-first и без v5 units. После этого следующим проходом repair-gate может
добить промо/уточнения/имя теми же scoped executor-ами.

**Верификация:** локально `py_compile blueprint.py grid_create.py repair_gate.py repair_executor.py`
OK; smoke с fake `GridCreateClient` подтвердил, что `add_text_content_to_existing` отправляет группы
в существующий `campaign_id=111` и создаёт объявления; smoke `executable_content_repairs` выбрал только
tp2 item; smoke `execute_content_repair` с monkeypatch вернул `status=200`,
`repaired_campaign_ids=[111]`, `uses_direct_units=false`. Remote md5 local==server по
`blueprint.py`, `grid_create.py`, `repair_gate.py`, `repair_executor.py`, `README.md`,
`EXTRACTION_PLAN.md`, `STATE.md`; remote `/root/venv/bin/python3 -m py_compile ...` OK; remote
Flask route-smoke через production app `direct.main` с monkeypatch `_create_set_job_context`,
`_create_set_live_verification`, `execute_content_repair` вернул `200`, `repaired_campaign_ids=[111]`,
`uses_direct_units=false` и передал item `search_test` в content executor, без реальных запросов
в Яндекс. Перед рестартом активных jobs/deferred не было; выполнен `systemctl restart direct.service`;
`direct.service active`, `digest.service active`; public repair endpoint без сессии → `401`,
`/direct/automation → 302 /login`; traceback/exception/syntax/Jinja/500 ошибок после рестарта не
найдено; очереди после рестарта пустые. Live-создание кампаний не запускалось.

## Последняя сессия: 2026-06-30 — live content counts через Grid

Добавлен `direct/grid_read.py` — отдельный read-only Grid слой для фактических счётчиков
`adGroups/ads` по campaign ids. `_create_set_live_verification(...)` теперь после Grid-списка кампаний
читает counts для обычных tp1–tp5 без v5 units и передаёт их в `live_verifier.verify_live_create_set`.
UAC tp6/tp7 намеренно исключены: у них структура не обязана отображаться как Grid adGroups.

`live_verifier.py` добавляет ошибки `NO_ADGROUPS_LIVE` и `NO_ADS_LIVE`, если кампания существует,
но в кабинете фактически 0 групп или 0 объявлений. `repair_planner.py` относит эти коды к
`rebuild_missing_content`, поэтому следующий executor сможет добивать контент по факту кабинета и
с конкретным `campaign_id`, а не по косвенному сохранённому `build.groups/build.ads`.

**Как было:** content-проблемы в live отчёте в основном выводились из JSON результата создания; если
сохранённый результат был неполный или неточный, план мог не увидеть реальный пустой черновик.
**Как стало:** live verification делает Grid/cookie read-only сверку фактических групп/объявлений и
строит `rebuild_missing_content` для пустых tp1–tp5 без расхода баллов Direct.

**Верификация:** локально `py_compile blueprint.py grid_read.py live_verifier.py repair_planner.py
repair_executor.py repair_gate.py grid_finalize.py` OK; unit-smoke `verify_live_create_set` с
`grid_content_counts={111:{adgroups:0, ads:0}}` вернул `status=fail`, issues
`NO_ADGROUPS_LIVE/NO_ADS_LIVE` и repair action `rebuild_missing_content` с `campaign_id=111`.
Remote/deploy verification см. ниже в текущей сессии после заливки.

## Последняя сессия: 2026-06-30 — repair executor v4: rename + вынос executors

`POST /direct/api/create_set_repair` получил scoped executor для `rename_campaign`.
Если live-проверка даёт `NAME_MISMATCH`, repair-gate выбирает пары `campaign_id -> expected name`,
а executor через cookie/Grid вызывает узкий `GridClient.set_campaign_names(...)`. Мутация отправляет
только `unifiedCampaign.id` и `name`: стратегии, площадки, промо, уточнения, минус-наборы и контент
не пересобираются. При падении пакетного обновления executor пробует кампании по одной и возвращает
`207` с `failed_campaigns`; если не обновилась ни одна — `502`.

Чтобы `blueprint.py` не раздувался дальше, in-place executor-ы вынесены в новый
`direct/repair_executor.py`: там теперь живут `execute_promo_repair`, `execute_callouts_repair` и
`execute_rename_repair`. `blueprint.py` оставляет HTTP/DB/lock/queue wiring и передаёт старые helper-ы
через `RepairDeps`, без обратного импорта большого blueprint.

**Как было:** `NAME_MISMATCH` попадал в план добивки, но `execute=1` не мог исправить потерянное/старое
название; реализации promo/callouts executor-ов временно жили внутри `blueprint.py`.
**Как стало:** имена кампаний можно добивать через Grid без расхода баллов Direct, а слой добивки
начал реально отделяться в `repair_executor.py`.

**Верификация:** локально `py_compile blueprint.py repair_gate.py repair_executor.py grid_finalize.py
verifier.py live_verifier.py repair_planner.py` OK; unit-smoke `executable_rename_campaigns` подтвердил
выбор только валидной пары id/name; локальный smoke `execute_rename_repair` с fake Grid вернул
`updated_campaign_ids=[101]`, `uses_direct_units=false`. Remote md5 local==server по
`blueprint.py`, `repair_gate.py`, `repair_executor.py`, `grid_finalize.py`, `README.md`,
`EXTRACTION_PLAN.md`; remote `/root/venv/bin/python3 -m py_compile ...` OK; remote endpoint-smoke
через production app `direct.main` с monkeypatch `_create_set_live_verification/_account_ctx/GridClient`
вернул `200`, `updated_campaign_ids=[111]`, `uses_direct_units=false`, без реальных запросов в Яндекс.
Перед рестартом активных jobs/deferred не было; выполнен `systemctl restart direct.service`;
`direct.service active`, `digest.service active`; public repair endpoint без сессии → `401`,
`/direct/automation → 302 /login`; traceback/syntax/Jinja ошибок после рестарта не найдено; очереди
после рестарта пустые. Live-создание кампаний не запускалось.

## Последняя сессия: 2026-06-30 — repair executor v3: уточнения через Grid

`POST /direct/api/create_set_repair` получил третий scoped executor для `execute=1`.
Если в плане есть `ensure_callouts`, endpoint берёт campaign ids из сохранённого terminal result job
(или конкретный `campaign_id` из action), берёт выбранные пользователем `body.callouts`, нормализует и
дедупит их теми же `_normalize_callout_text/_dedup_callouts`, создаёт/находит ассеты через
`GridClient.add_callouts(...)` и привязывает их к кампаниям через новый узкий
`GridClient.set_campaign_callouts(...)`.

Новая Grid-мутация обновляет только `inheritableCallouts`, без полного `finalize(...)`: стратегии,
площадки, минус-наборы, промо и остальные настройки кампании не пересобираются. Если пакетная мутация
падает на одной кампании, executor пробует campaign ids по одному и возвращает частичный результат
`207` с `failed_campaigns`; если ничего не обновилось — `502`.

Ограничения намеренные:
- если есть недосозданные кампании, приоритет остаётся у retry/recreate repair-job;
- если одновременно есть `create_or_attach_promo`, сначала выполняется промо-executor, а callouts
  остаются в `unsupported_actions` для следующего прохода;
- executor требует сохранённые `body.callouts`; если пользователь не выбирал уточнения, он не
  подставляет случайные уточнения аккаунта;
- `rename_campaign` и content/groups in-place пока остаются `unsupported_actions`.

**Как было:** `CALLOUTS_NOT_CONFIRMED` попадал в план добивки, но `execute=1` не умел ничего сделать
с уточнениями.
**Как стало:** выбранные уточнения можно добить через cookie/Grid без расхода баллов Direct и без
перезаписи всей кампании.

**Верификация:** локально `py_compile blueprint.py repair_gate.py grid_finalize.py verifier.py
live_verifier.py repair_planner.py` OK; unit-smoke `executable_callout_campaign_ids` подтвердил
set-level выбор всех успешных campaign ids и action-specific выбор одного id. Remote md5 local==server
по `blueprint.py`, `repair_gate.py`, `grid_finalize.py`, `README.md`, `EXTRACTION_PLAN.md`; перед
рестартом активных jobs/deferred не было; remote `/root/venv/bin/python3 -m py_compile ...` OK;
remote Flask smoke через production app `direct.main` с monkeypatch `GridClient/build_client` вернул
`200`, `attached_campaign_ids=[111]`, `callout_ids=[701,702,703]`, `uses_direct_units=false`, без
реальных запросов в Яндекс. Выполнен `systemctl restart direct.service`; `direct.service active`,
`digest.service active`; public repair endpoint без сессии → `401`, `/direct/automation → 302 /login`;
traceback/syntax/Jinja ошибок после рестарта не найдено; очереди после рестарта пустые. Live-создание
кампаний не запускалось.

## Последняя сессия: 2026-06-30 — repair executor v2: промо через Grid

`POST /direct/api/create_set_repair` получил второй scoped executor для `execute=1`.
Если в плане нет `resume_or_recreate_campaign`, но есть `create_or_attach_promo`, endpoint теперь
берёт campaign ids из сохранённого terminal result job и запускает in-place добивку промо:
создаёт согласованную промоакцию через Grid по выбранному в исходной job слепку (`body.agent`) и
привязывает её к уже созданным черновикам. v5/promotions.get в этом executor-е не вызывается:
`uses_direct_units=false`.

Ограничения намеренные:
- если есть недосозданные кампании, приоритет остаётся у retry/recreate repair-job;
- executor промо требует сохранённый `body.agent` и campaign ids;
- `ensure_callouts`, `rename_campaign`, content/groups in-place пока остаются `unsupported_actions`.

**Как было:** `execute=1` умел только поставить недосозданные кампании в повторную очередь; проблема
`PROMO_NOT_ATTACHED` оставалась только планом.
**Как стало:** отдельный случай с непривязанным промо может добиваться через Grid без создания новых РК
и без расхода баллов Direct.

**Верификация:** локально `py_compile blueprint.py repair_gate.py verifier.py live_verifier.py
repair_planner.py` OK; unit-smoke `executable_promo_campaign_ids` подтвердил сбор ids из вложенных
results и фильтр action-ов; Flask test_client smoke с monkeypatch `_execute_promo_repair`: `execute=1`
для плана `create_or_attach_promo` вернул `200`, `promo_id`, `attached_campaign_ids=[111]`,
`uses_direct_units=false`, без реальных запросов в Яндекс. Remote md5 local==server по изменённым
файлам; перед рестартом активных jobs/deferred не было; remote compile OK; выполнен `systemctl restart
direct.service`, `direct.service active`, `digest.service active`; public repair endpoint без сессии
→ `401`, `/direct/automation → 302 /login`, traceback/syntax/template ошибок в `direct.service` не
найдено; remote Flask smoke с monkeypatch также вернул `200`, `promo_id=999`,
`attached_campaign_ids=[111]`, `uses_direct_units=false`; очереди после рестарта пустые.
Live-создание кампаний не запускалось.

## Последняя сессия: 2026-06-30 — repair executor v1: scoped retry/recreate через очередь

`POST /direct/api/create_set_repair` теперь умеет не только read-only план, но и безопасный `execute=1`
для одного action-типа: `resume_or_recreate_campaign`. Executor v1 выбирает исходные `body.items`,
имя которых совпадает с action name (включая fan-out `item — feed`), и ставит отдельную repair-job в
обычную очередь `create_set`.

Ограничения v1 намеренные:
- repair-job принудительно `via_cookie=true` и `launch=false`;
- Direct units не тратятся;
- старые `_job_id`, `_resume_count`, `_deferred_id` очищаются;
- если по аккаунту уже есть активная job, repair не ставится (`409`);
- `create_or_attach_promo`, `ensure_callouts`, `rename_campaign`, content-in-place остаются
  `unsupported_actions` до отдельных scoped executor-ов.

**Как было:** repair-gate только показывал план; `execute=1` не ставил добивку в работу.
**Как стало:** для упавших/не найденных кампаний можно поставить ограниченную repair-job в ту же
очередь, где уже есть `RESUME-SKIP` по именам, поэтому существующие кампании должны пропускаться.

**Верификация:** локально `py_compile blueprint.py repair_gate.py verifier.py live_verifier.py
repair_planner.py` OK; unit-smoke `executable_recreate_items/repair_queue_body` подтвердил выбор
одного item, `via_cookie=True`, `launch=False`, очистку `_job_id`. Remote md5 local==server по
`blueprint.py`, `repair_gate.py`, `README.md`, `EXTRACTION_PLAN.md`; remote compile OK; remote
helper-smoke OK. Перед рестартом активных jobs/deferred не было. Выполнен `systemctl restart
direct.service`; `direct.service active`, `digest.service active`; public repair endpoint без сессии
→ `401`, `/direct/automation → 302 /login`, traceback/syntax/template ошибок в `direct.service` не
найдено. Authenticated Flask smoke с monkeypatch `_job_new`: `execute=1` вернул `200`, `new_job_id`,
`queued_items=1`, `uses_direct_units=false`, repair body `via_cookie=True`, `launch=False`, без
реальной постановки в очередь. Очереди после smoke пустые. Live-создание кампаний не запускалось.

## Последняя сессия: 2026-06-30 — repair_gate.py extraction

Добавлен `direct/repair_gate.py` — первый маленький вынос repair-gate логики из `blueprint.py`.
Модуль Flask/DB-free и содержит:
- `truthy(...)`: безопасный boolean-разбор (`"false"` не включает execute/v5);
- `dict_from_jsonish(...)`: нормализация dict/JSON-string для `job.result` и `job.body`;
- `normalize_job_context(...)`: общий context для verification/repair endpoints;
- `build_repair_gate_payload(...)`: стабильная форма read-only ответа repair-gate.

`blueprint.py` теперь оставляет у себя только HTTP/lock/DB/live-IO wiring, а чистые преобразования
делегирует в `repair_gate.py`.

**Как было:** часть repair-gate нормализации жила внутри большого `blueprint.py`; следующий executor
пришлось бы наращивать там же.
**Как стало:** появился отдельный тестируемый модуль для repair-gate контракта; поведение endpoint
не изменилось, но следующий scoped executor можно подключать рядом с ним, не раздувая монолит.

**Верификация:** локально `py_compile blueprint.py repair_gate.py verifier.py live_verifier.py
repair_planner.py` OK; локальный smoke `truthy/jsonish/context/payload` OK. Remote md5 local==server
по `blueprint.py` и `repair_gate.py`; remote compile OK; remote helper-smoke OK. Перед рестартом
активных jobs/deferred не было. Выполнен `systemctl restart direct.service`; `direct.service active`,
`digest.service active`; public repair endpoint без сессии → `401`, `/direct/automation → 302 /login`,
traceback/syntax/template ошибок в `direct.service` не найдено. Authenticated Flask smoke с JSON-string
`result/body`: `execute:"false"` → `200`, `execute:"true"` → `409` без второго live-вызова.
Очереди после smoke пустые. Live-создание кампаний не запускалось.

## Последняя сессия: 2026-06-30 — UI-кнопка read-only repair plan

В `templates/direct/index.html` карточка завершённой job теперь показывает кнопку `План добивки`,
если в `verification/live_verification.repair_plan` есть actions. Кнопка вызывает
`POST /direct/api/create_set_repair` с `execute:false`, получает актуальный Grid-first план и выводит
первые действия в верхний баннер. Мутаций нет: endpoint остаётся read-only, Direct units по умолчанию
не тратятся.

**Как было:** UI показывал только краткую строку `repair_plan`, но для деталей нужно было лезть в JSON
или дергать endpoint вручную.
**Как стало:** после завершения job можно прямо из карточки открыть актуальный план добивки: какие
действия нужны, через какой transport и тратят ли они Direct units.

**Верификация:** локальный Jinja-neutralized `node --check` script-блока OK; локальный `py_compile`
Direct-модулей OK; проверено наличие `showRepairPlan`, `/direct/api/create_set_repair`, `execute:false`.
Перед рестартом активных jobs/deferred не было; md5 local==server для `templates/direct/index.html`;
remote compile OK. Выполнен `systemctl restart direct.service`; `direct.service active`,
`digest.service active`; публичный `/direct/automation → 302 /login`, unauth repair endpoint → `401`;
traceback/template/syntax ошибок в `direct.service` не найдено; очереди после рестарта пустые.
Live-создание кампаний не запускалось.

## Последняя сессия: 2026-06-30 — read-only repair-gate endpoint

Добавлен backend-gate `POST /direct/api/create_set_repair`: он поднимает сохранённый terminal result job
из памяти/БД, заново делает Grid-first live-сверку через общий `_create_set_live_verification(...)` и
возвращает `repair_plan`. В v1 endpoint намеренно read-only: `execute=1` отклоняется `409`, пока не
будет включён scoped executor по отдельным безопасным action-типам.

Добавлен общий helper `_create_set_job_context(...)`, чтобы `create_set_verification` и repair-gate
одинаково читали job/result/body/agency. Boolean-параметры repair endpoint разбираются явно: строка
`"false"` не включает execute/v5.

**Как было:** план добивки был внутри `verification/live_verification`, но не было отдельного API-контракта
для будущих агентов/UI, которые должны запросить “что добивать” без запуска мутаций.
**Как стало:** есть отдельная read-only точка repair-gate; она не тратит Direct units по умолчанию,
не создаёт кампании и не может случайно выполнить `execute`, пока executor не реализован.

**Верификация:** локально `py_compile blueprint.py verifier.py live_verifier.py repair_planner.py` OK;
planner-smoke подтвердил `RESULT_FAILED/PROMO_NOT_ATTACHED/CALLOUTS_NOT_CONFIRMED/NO_ADS_REPORTED`
в cookie/Grid actions без Direct units. Remote md5 local==server по `blueprint.py`, `README.md`,
`EXTRACTION_PLAN.md`; remote compile OK; перед рестартом активных jobs/deferred не было.
Выполнен `systemctl restart direct.service`; `direct.service active`, `digest.service active`;
`/direct/api/create_set_repair` есть в `url_map`, unauth public POST → `401`, `/direct/automation →
302 /login`, traceback/syntax/template ошибок в `direct.service` не найдено. Authenticated Flask
test_client smoke: пустой `job_id` → `400`, missing job → `404`, синтетическая job с
`execute:"false"` → `200` и план, `execute:"true"` → `409`; после smoke очереди пустые.

## Последняя сессия: 2026-06-30 — auto live_verification после create_set

`api_create_set` теперь после статического `verification` автоматически запускает read-only
`_create_set_live_verification(...)` и возвращает поле `live_verification` в ответе/terminal job result.
Путь строго Grid-first и `use_v5=False`, чтобы не тратить дефицитные Direct API units. Endpoint
`/api/create_set_verification?live=1` переведён на тот же helper, чтобы логика проверки была одна.

**Как было:** live-сверку нужно было отдельно запрашивать по `job_id`.
**Как стало:** сразу после создания набор получает два отчёта: статический `verification` и фактический
`live_verification`, оба с `repair_plan`. v5 остаётся только ручной опцией endpoint (`v5=1`).

**Верификация:** локально `py_compile blueprint.py verifier.py live_verifier.py repair_planner.py`.
Перед рестартом была активная job `e7667081ce0d` (`porg-psm5h7q6`, delete_drafts_async), поэтому сервис
не перезапускался до её завершения. После завершения очереди: активных jobs/deferred нет; remote md5
local==server; remote compile OK; remote smoke подтвердил, что `_create_set_live_verification(...,
use_v5=False)` делает 1 Grid-вызов и 0 v5-вызовов. Выполнен `systemctl restart direct.service`;
`direct.service active`, `digest.service active`; route/helper smoke OK; публичный `/direct/automation →
302 /login`, verification endpoint `401`; traceback/exception в `direct.service` не найден; очереди пустые.

## Последняя сессия: 2026-06-30 — UI показывает post-create проверку и repair_plan в очереди

Карточки очереди создания в `templates/direct/index.html` теперь имеют блок `.job-verify`.
После terminal result UI показывает `live_verification` (если есть) или старый статический `verification`:
статус проверки, число ошибок/предупреждений и краткий `repair_plan` с пометкой, тратит ли добивка
Direct units. Во время `queued/running` блок скрывается, чтобы не показывать старый результат.

**Как было:** автоматическая проверка и план добивки уже возвращались API, но в интерфейсе очереди их
не было видно; нужно было смотреть JSON/логи.
**Как стало:** после завершения создания видно прямо в карточке: проверка пройдена / нужна добивка,
сколько действий запланировано и что план по умолчанию Grid/cookie без баллов Direct.

**Верификация:** локальный Jinja-neutralized `node --check` единственного script-блока OK; локальный
`py_compile blueprint.py verifier.py live_verifier.py repair_planner.py` OK; перед рестартом активных
`direct_automation_jobs` и `direct_deferred_creates` не было; md5 local==server для
`templates/direct/index.html`; remote `py_compile` OK. Выполнен `systemctl restart direct.service`;
`direct.service active`, `digest.service active`; публичный `/direct/automation → 302 /login`,
verification endpoint `401`; traceback/template/syntax ошибок в `direct.service` не найдено; очереди
после рестарта пустые. Live-создание кампаний не запускалось.

## Последняя сессия: 2026-06-30 — repair planner v1 без мутаций

Добавлен `direct/repair_planner.py`: чистый слой планирования добивки, без Flask/DB/Direct/Grid вызовов.
Он превращает `issues` и старые `repair_candidates` из `verifier.py`/`live_verifier.py` в ordered
`repair_plan.actions`: `resume_or_recreate_campaign`, `rebuild_missing_content`, `create_or_attach_promo`,
`ensure_callouts`, `rename_campaign`, `retry_live_verification`. В каждом action есть `transport` и
`uses_direct_units`; дефолт — `cookie_grid`, чтобы не тратить дефицитные баллы Direct.

`verifier.py` и `live_verifier.py` теперь добавляют `repair_plan` в ответ. Исправлен дефект:
failed result раньше попадал только в `repair_candidates`, но отчёт мог остаться `status=pass`.
Теперь failed result добавляет issue `RESULT_FAILED` с severity `error`, и статический отчёт становится
`fail`.

**Как было:** проверка говорила “есть repair_candidates”, но не давала нормального плана действий;
у failed campaign был риск `status=pass`.
**Как стало:** API сразу отдаёт машинный план добивки, все действия по умолчанию cookie/Grid-first и
не тратят v5 units; failed campaign корректно валит verification в `fail`.

**Верификация:** локально `py_compile blueprint.py verifier.py live_verifier.py repair_planner.py`;
smoke: failed result → `status=fail` + `resume_or_recreate_campaign(cookie_grid)`, promo/callouts →
соответствующие actions, live groups/ads=0 → `rebuild_missing_content`, clean pass → empty plan.
Code-review gate: проверены изменённые Direct-файлы, статический scan подтвердил, что новые verifier/planner
модули не делают сетевых/системных вызовов и не мутируют Директ. Перед рестартом активных jobs/deferred
не было. Remote md5 local==server, remote compile OK. Выполнен `systemctl restart direct.service`;
`direct.service active`, `digest.service active`; remote route/planner smoke OK; публичный
`/direct/automation → 302 /login`, verification endpoint `401`; traceback/exception в `direct.service`
не найден; очереди после рестарта пустые. Live-создание кампаний не запускалось.

## Последняя сессия: 2026-06-30 — live verifier v1, Grid-first из-за малого остатка баллов

Добавлен `direct/live_verifier.py`: read-only слой post-create проверки, отдельно от `blueprint.py`.
Он нормализует вложенные результаты `create_set` (`campaigns` fan-out), извлекает созданные campaign ids,
классифицирует `tp6/tp7` как UAC/Grid, остальные как обычные кампании, проверяет build-ошибки,
потерю имени/кодера, отсутствие кампании в live-снимке, archived/status и формирует `repair_candidates`.

Добавлен endpoint `GET /direct/api/create_set_verification?job_id=...`:
- без `live=1` отдаёт сохранённую статическую `verification` из terminal result джобы;
- с `live=1` делает read-only сверку факта;
- по умолчанию live-сверка использует Grid/cookie (`_grid_list_campaigns`) как основной источник,
  потому что баллов Direct API мало, а tp6/tp7 в v5 не видны;
- `v5=1` включает дополнительный `campaigns.get` по обычным tp1-tp5 и может тратить units.

**Как было:** после создания была только статическая проверка формы ответа. Чтобы понять, реально ли
кампания видна в кабинете, приходилось руками заходить в Директ.
**Как стало:** по job_id можно получить сохранённый отчёт и live-read-only сверку через cookie/Grid
без запуска новых кампаний и без расхода v5-баллов по умолчанию.

**Верификация:** локально `py_compile blueprint.py verifier.py live_verifier.py`; smoke
`live_verifier`: fan-out result не дублирует агрегатор без id, Grid-first pass, missing Grid → fail,
нулевые groups/ads → fail, v5-only optional pass. Live-создание кампаний не запускалось.
Code-review gate по изменённым Direct-файлам: блокеров не найдено; учтено, что дефолт проверки не тратит
v5-баллы. Перед рестартом активных `direct_automation_jobs` и `direct_deferred_creates` не было.
Remote md5 local==server, remote `py_compile` OK. Выполнен `systemctl restart direct.service`;
`direct.service active/enabled`, `digest.service active`; route smoke: новый
`/direct/api/create_set_verification` есть в `url_map`, unauth `401`, публичный
`/direct/automation → 302 /login`, публичный verification endpoint `401`; traceback/exception в
`direct.service` не найден. Terminal job result в БД сейчас нет, поэтому endpoint по реальному job_id
не проверялся.

## Последняя сессия: 2026-06-30 — post-create verifier v1

Добавлен отдельный модуль `direct/verifier.py` — первый шаг к автоматической проверке после создания РК
и разгрузке `blueprint.py`. `api_create_set` теперь возвращает поле `verification`:
`status=pass|warn|fail`, summary, issues, repair_candidates.

**Что проверяет v1 без live-обхода кабинета:** каждый item получил result; созданные кампании имеют id;
имена похожи на кодер `tp*_cpc|cpa_site|kviz...`; нет `None/null/undefined` в имени; failed results
попадают в repair_candidates; промо должно быть привязано/создано; выбранные callouts должны быть
подтверждены note; для tp6/tp7 отмечается недобор контента по titles/texts/sitelinks.

**Как будет развиваться:** следующий слой — live-verifier через v5+Grid/UAC по campaign_ids:
проверка реально заведённых объявлений, фильтров фида, промо, callouts, инвариантов кампании
(персонализация выкл, мониторинг вкл, расширенный гео выкл, директ помогает выкл), и затем repair
actions для добивки.

**Верификация:** `py_compile blueprint.py verifier.py`; unit-smoke `verify_create_set`: pass-case,
promo warn-case, bad coder fail-case, created-without-id fail-case. Активных jobs не было.
Remote md5 local==server (`blueprint.py=2ba3893b923e7d5b4b30745b8954ff43`,
`verifier.py=b991b925a410aad58ff230cce8e1c6bf`), remote compile OK, `systemctl restart direct.service`,
`direct.service active`, публичный `/direct/automation → 302 /login`, traceback/exception в логе не найден.
Live-создание кампаний не запускалось.

## Последняя сессия: 2026-06-30 — автопромо перед созданием кампаний

**Корень:** финальный блок `api_create_set` только читал уже заведённые `promotions.get`.
Если в аккаунте не было промо, результат был `в аккаунте нет промо — привязывать нечего`.
Если промо было с другим процентом, часть конфликтов ловилась, но промо без процента могло пройти
при контенте с `45%`.

**Как теперь:** после создания черновиков сервис:
1. читает промо аккаунта;
2. привязывает только пригодное промо, где процент совпадает с процентом в созданном контенте;
3. если пригодного промо нет, создаёт одно промо в библиотеке клиента через Grid по ВЫБРАННОМУ
   в создании слепку (`body.agent`, без fallback на `directologist` аккаунта);
4. если в контенте есть процент (`45%`), автопромо принудительно получает тот же `amount=45`,
   `unit=PCT`; затем промо привязывается к созданным кампаниям.

Кампании по-прежнему остаются черновиками; создаётся только реальная промоакция в библиотеке клиента,
потому что пользовательское правило — перед созданием кампаний обеспечить наличие промо и привязать её.

**Верификация:** локально `py_compile blueprint.py`; unit-smoke: `30%` vs контент `45%` отклоняется,
`AmountUnit=PCT/Amount=45` принимается, автопромо из RUB-слепка при контенте `45%` уходит в `PCT/45`.
Активных jobs перед рестартом не было. Remote md5 local==server `45999e32347260c14fb896d14ddd5ece`,
remote `py_compile` OK, `systemctl restart direct.service`, `direct.service active`,
публичный `/direct/automation → 302 /login`, traceback/exception в `direct.service` не найден.
Live-создание кампаний на `porg-psm5h7q6` не запускалось.

## Последняя сессия: 2026-06-30 — старт выноса Direct в отдельный сервис

Активные jobs перед работой проверены: в `direct_automation_jobs` нет `queued/running/active/pending`,
последние три уже `interrupted`; в `direct_deferred_creates` нет `waiting/resumed`. Останавливать было нечего.

**Фаза 1:** добавлен standalone entrypoint `direct/main.py`: регистрирует только `direct_bp`,
использует общий `FLASK_SECRET_KEY`, общие templates/static и совместимые endpoints `login`/`home`, чтобы
`auth.py` не падал на `url_for("login")` в отдельном процессе. `app.py` переключён: импорт/регистрация
`direct_bp` и `_init_direct()` убраны; вкладка в `_nav.html` оставлена на прежнем `/direct/automation`.

**Deploy/cutover:** добавлены `direct/deploy/direct.service` и `direct/deploy/nginx-direct-location.conf`.
На LXC101 установлен `/etc/systemd/system/direct.service`, запущен и включён в автозапуск; nginx получил
`location /direct/ → http://127.0.0.1:5020` перед общим `/`. Backup nginx:
`/etc/nginx/sites-available/seoadvanced.ru.bak.20260630_211541`.

**План обновлён:** `EXTRACTION_PLAN.md` теперь отражает `blueprint.py` ~14.9k строк и добавляет фазу
автопредсоздания promo-библиотеки через существующий `_seed_slepok_content(kind='promo')`/M3 по слепкам.
Публикация промо в аккаунт остаётся явным действием, потому что меняет библиотеку клиента в Директе.

**Верификация:** локально `py_compile app.py direct/main.py direct/blueprint.py`; импорт main app показал
blueprints без `direct`, standalone app — только `direct`; test_client: unauth `302 /login`, auth admin
`200 text/html`. На LXC101: remote md5 local==server по `app.py`, `direct/main.py`, `direct.service`;
`direct.service active/enabled`, `digest.service active`, `nginx -t` OK. Smoke: `5010/direct/automation → 404`,
`5020/direct/automation → 302 /login`, публичный `https://seoadvanced.ru/direct/automation → 302 /login`
и запрос виден в `journalctl -u direct.service`. Очередь после cutover: активных jobs нет.
Live-создание кампаний на `porg-psm5h7q6` не запускалось.

## Последняя сессия: 2026-06-30 — Claude agents + tp6/tp7 добор длинных текстов

Добавлены проектные Claude agents: `direct_investigator` (read-only диагностика), `direct_fixer` (маленькие scoped-патчи),
`direct_verifier` (как было/как стало, файлы/логи/БД/сервер). Лежат в `.claude/agents/`.

**tp6/tp7 тексты:** корень свободных символов — fallback `_diverse_text_offers` мог добрать короткую строку
`Первый взнос 0 ₽. КАСКО на 1 год бесплатно.` (~40 симв) после дедупа/UTP-фильтра. Фикс: `_TP67_MIN_TEXT_LEN=70`,
финальный добор tp6/tp7 отбрасывает короткие тексты и берёт длинные валидные generic fallback 76–81 симв.
Верификация локально в venv: `py_compile blueprint.py`; тест на кейсе со скрина вернул 3 текста длиной 79/81/79.

## Последняя сессия: 2026-06-30 — пост-ревью батч: товары марка+модель, цена, кнопка-изоляция, контент-банки

Финальный рестарт прошёл, md5 local==server (blueprint/grid_create/grid_finalize/ai_agents/templates/direct/index.html),
active 302. Верификация — живыми Grid/FeedOffersPreview-запросами + юнит-тестами на LXC101.

**Товары марка+модель (D):** `_model_field_values(brand, seg)` — хвост имени без марки (BAIC X35→[X35,x35]) во всех
регистрах, ТОЛЬКО для «Модели». `add_shopping_ads` (grid_finalize): conditions = vendor + model. Фид без `<model>` →
**UNKNOWN_FIELD-ретрай** без model (откат на vendor — грабля lzjk6p5m закрыта). Живой тест фид №3501259: vendor BAIC =7
офферов → +model X35 =1 оффер. Применено на токен+куки путях. Каталоги (name=марка+модель) не трогал.

**Цена (adPrice не ставилась):** 1) `_account_offer_prices` НЕ кэширует пустую карту (транзиент фида травил 20мин);
2) `_price_feeds_for`/`_grid_price_feed` нормализуют url до КОРНЯ домена (`_homepage_url`) — deep-link (/auto/baic)
сужал фиды по пути → 0 цен.

**Кнопка-изоляция (code-review #4):** кнопка «Получить скидку» вынесена в ОТДЕЛЬНЫЙ UpdateAdaptiveTextAds
(`_apply_combo_button`) ПОСЛЕ картинок/цены — её отказ (напр. на чистом Поиске) НЕ роняет батч с картинками/ценой.
Убрана из `_grid_set_ad_prices`/`_grid_update_adaptive_ads` main-payload.

**Code-review (4 угла, мультиагент) — 3 реальных фикса:** vendor case-вариант перетирался `set_default_text` →
`_vendor_filter_values` (все регистры) в ОБОИХ местах сборки фильтра; мультигород тёк через `_brand_title_set`/
`_brand_text_set` → добавлен `_content_city`; `_grid_callout_ids`→[] при несовпадении текстов → фолбэк на первые N.
Эффективность: per-login кэш `_GRID_MINUS_PACK_CACHE`/`_GRID_CALLOUTS_CACHE` (TTL 20мин) + `_MINUS_PLACES_ENSURED`.

**#4 «Авито» в текстах:** `_valid_pack_brand_name` → `_coder_name_real_brand` гард (Авито/Дром/Автосалон → ""),
иначе `_brand_text_set("Авито")` лепил «Купить Авито в кредит».
**#6 tp2/tp4 картинки:** image-блок убран из `_create_text_via_cookie` (Поиск без картинок; цена+кнопка остаются).

**Контент-банки (slepki_master): КОРЕНЬ — формат `-45%` блокировался `(?<!\d)-\d+%` в `_bad_ad_title/_bad_ad_text`** →
ВЕСЬ банк Щербаковой был мёртвый. Исправлено на `45%`. `_GENERIC_AT_TITLES`/`_GENERIC_TITLE_FILLERS`/`_GENERIC_TEXT_FILLERS`
обогащены (48-55 симв заголовки, 76-81 тексты, разные УТП без prefix-дублей) — ВСЕ проходят фильтры (проверено).

**Открыто:** #1 минус-площадки и #3 tp2-динамика — проверить на чистом ре-ране (кампании были на полу-старом коде);
#9 диагностика catalog-фейлов; слепок «С пробегом» контент-банки (не трогали); r-код мультигород (данные аккаунта).

## Последняя сессия: 2026-06-30 — банки Щербаковой: AGENT_ADS + БД direct_slepok_content

**Корень (новая находка):** Все 5 AGENT_ADS["scherbakova"]["titles"] и большинство текстов содержали
«-45%» (блокируется `(?<!\d)-\d+\s*%` в `_bad_ad_title`/`_bad_ad_text`) — банк был ПОЛНОСТЬЮ мёртвым.
В DB direct_slepok_content 5/10 заголовков и 3/5 текстов блокированы по той же причине.

**ai_agents.py:** `AGENT_ADS["scherbakova"]["titles"]` — все 5 заменены (без «-N%», без «б/у» в заголовке
— `/` blocked, без «убрали наценки» — alien-сигнатура Павлова). `AGENT_ADS["scherbakova"]["texts"]`
— тексты [0,1] заменены (были «убрали наценки» + «-45%»). Syntax OK.

**БД direct_slepok_content (scherbakova, kind=campaign, 4 new-car site_types):** `SQL OK rows=4`.
Заголовки: 10/10 проходят фильтры (49–54 chars). Тексты: 5/5 — трейд-ин без «-45%», взнос без «-45%»,
новый КАСКО как отдельный UTP. Верификация SELECT — все поля корректны.

**Для blueprint.py (описание, применить Семёну):** _GENERIC_AT_TITLES и _GENERIC_TEXT_FILLERS —
список новых строк в резюме/теле ответа сессии.

**Грабли:** «Скидка -N%» = neg-pct BLOCKED. Писать «Скидка N%» (без «до» перед числом — иначе title_re).
С пробегом: те же блоки в «Б/У авто... Скидка -45%» — НЕ исправлено (отдельный пункт).
**Рестарт:** `systemctl restart digest` на LXC 101.

## Последняя сессия: 2026-06-30 — БОЛЬШОЙ батч приёмки (19 фиксов) + фича минус-площадки + code-review

Все правки задеплоены, md5 local==server (blueprint/grid_create/grid_finalize/ai_agents/templates/direct/index.html),
сервис active, reloader OFF (рестарт явный). Верификация — живыми Grid-запросами и юнит-тестами на LXC101.

**Приёмка — корневые баги:**
- **ct-валидатор бренда** (`_coder_name_real_brand` + `_brand_ct_from_coder` ~13311): ct0009=«Авито»/ct0010=«Дром»/
  «Автосалон» — сегмент «Общее», НЕ бренд. Раньше трактовались как бренд → краш «нужен заголовок» (tp6/tp7) +
  feedFilter `model=[Дром]`→UNKNOWN_FIELD. Прогон через `_is_brand_canon` (43 марки). + `_fallback_master_titles`
  этаж-гарант (никогда не пусто) + `_tp7_product_feed_filters` гард.
- **Чужие ключи в общих группах** (`_filter_group_keywords`+`_brand_model_token_set`+`_drop_model_keys_common` ~7950):
  «Общее»-группы (Дром/Авто/ct0001) тащили модельные ключи (cityray/monjaro). Срез по 208 токенам марок+моделей.
  Применён на 3 местах сборки групп (tp1 + tp2/tp4).
- **Цены по модели** (`_offer_price_keys`+`_grid_feed_offer_prices`): нормализация имён офферов (срез года/тех.хвоста/
  «автомобиль») → модель матчится, дешёвые офферы со скидкой всплывают.
- **Vendor товаров** (`_vendor_value`+`_vendor_filter_values` ~5763): регистр vendor зависит от ФИДА (HAR42/43:
  один «Belgee», другой «baic»). CONTAINS_ANY case-sensitive → шлём ВСЕ регистры `[v,lower,Title]` во всех местах
  (`add_shopping_ads` + `set_default_text._shop_filters` ~10084 + grid-path ~7508). Каталог (name=марка+модель) — как есть.
- **Места показа tp2** (`build_unified_campaign.placement_types` + spec): tp2/tp4 → `placementTypes=['SEARCH_PAGE']`
  (без динамич. мест на поиске); динамика tp4 = organic. РСЯ/gallery не тронуты.
- **Мультигород** (`_content_city`): город через запятую → контент БЕЗ города. Применён в `_title_from_template`,
  `_replace_foreign_city`, `_ai_campaign_content_for_item`, `_brand_title_set`, `_brand_text_set`.
- **Deep-link** (`_model_page_href`): Марки (одно слово) → `/auto/{brand}` (раньше главная).
- **Кнопка «Получить скидку»** (`_combo_button`/`_homepage_url` + button в `_grid_set_ad_prices`/`_grid_update_adaptive_ads`):
  GET_DISCOUNT→главная во ВСЕ комбо-объявления (адаптивные) всех tp. Живой replay HAR-схемы → 200.
- **Куки-фолбэк v5-ассетов (директива «всё на баллах дублировать на куки»):**
  минус-набор `_grid_minus_pack_id` (MinusPhraseLibrary→libraryMinusKeywordsIds, HAR40); уточнения `_grid_callout_ids`
  (Callouts→inheritableCallouts). Оба per-login КЭШ (`_GRID_*_CACHE`, TTL 20мин) — Grid+CSRF раз на аккаунт.
- **Дедуп цифр в сайтлинках** (`_dedup_sitelinks` ai_agents.py): ≤2 на одну цифру (баг 4×«30»). + M3-промпт.
- **Диагностика tp1-куки catalog-фейлов** (rep.errors append) — на прогоне покажет точную причину.

**Фича #21 — минус-площадки РСЯ:** таблица `direct_global_minus_places`, вкладка «Минус-площадки» в Глобальных
правилах (тёмная тема + список справа), API `/api/minus-places` (URL→нижний регистр, replace-all с guard на пустой
POST/missing-key + confirm_clear), применение в `disabledPlaces` всех tp1 (build_unified_campaign + _finalize_rsya).

**Code-review (4 угла, мультиагент) — починено 3 реальных:** vendor case-перетёр `set_default_text`; мультигород-утечка
через `_brand_*_set`; `_grid_callout_ids`→[] при несовпадении текстов (теперь фолбэк на первые N). Эффективность: кэши
+ `_MINUS_PLACES_ENSURED` (DDL раз на процесс).

**Открыто:** #4 кнопка на tp2/tp4-Поиск (не верифиц. живьём — следить что батч не упал; картинок там нет by-design);
#9 диагностика (на прогоне); r-код мультигород porg-lzjk6p5m=r0134 (расхождение данных: Новосибирск в аккаунте, нет
в r0134 — правится в гугл-таблице). Слепок Щербакова: контент-банки обогащены slepki_master (ai_agents.py + БД
direct_slepok_content).

## Последняя сессия: 2026-06-30 — опечатки callouts из M3: пост-фильтр в _normalize_callout_text

- Источник: M3-генерация (в банках кода нет). Фикс: три `re.sub` в `_normalize_callout_text` (`blueprint.py:6461`):
  «латеж»→«платеж», «платед»→«платеж», «рапрода»→«распрода» (регистр сохраняется через lambda).
  Верификация: 4/4 OK, 3 контр-примера не тронуты. py_compile OK. md5 `91b8877605ded00d822df2030d8a1944`. Рестарт — за Семёном.

## Последняя сессия: 2026-06-30 — батч-фикс дедупа: #4 tp7 префиксное поглощение, #6 уточнения семантика+кап

Только КОД (очередь пуста, приёмка заморожена; верификация = детерминисто юнит-проверками, без создания).
Финальный рестарт — за Семёном (reloader подхватит синк; файл валиден — импорт `direct.blueprint` прошёл).

- **#4 tp7 почти-дубли текстов (префиксное поглощение).** Корень: дедуп текстов шёл только по
  `_variant_norm_key` (числа схлопнуты) — «…бесплатно.» vs «…бесплатно при покупке в кредит» = разные
  ключи, оба проходили. Фикс: новые `_text_norm_tokens`/`_dedup_prefix_absorb` (`blueprint.py` ~7563):
  токенное ядро (lower/ё→е/без пунктуации/₽/%); ядро одной строки — префикс другой (хвост=расширение)
  → оставляем длинную; guard min_tokens=4. Применён в tp6/tp7-сборке текстов (`blueprint.py` ~11848)
  ПЕРЕД добивкой до 3 (`_diverse_text_offers(... + _GENERIC_TEXT_FILLERS)` тоже через prefix-absorb).
  Верификация: пара-дубль → 1 (длинная); контр-пример (разные оферы) → 2.
- **#6 уточнения: семантический дедуп ценовых/складских + кап.** Корень: `_callout_semantic_key`
  (`blueprint.py` ~6469) не нормализовал «автокредит от N р/мес» (нет платеж/взнос → число делало ключ
  уникальным) и склад/склады/стоянку. Фикс: ключи `credit_monthly` (`(авто)?кредит`+`от`+руб/мес — НЕ
  трогает «кредит от 15 банков») и `free_stock` (`освобожда`+`склад|стоянк|сток`, тире `--/–/—→-`).
  Новый `_dedup_callouts(texts, cap)` + `_CALLOUT_PER_CAMPAIGN_CAP=8` — единый чокпоинт; `_ensure_callout_exts`
  и Grid `_resolve_campaign_assets` (~7461) переведены на него. Верификация: 10 ценовых→1, 5 складских→1,
  8 разных оферов→8, 12→8 (кап).
- Деплой: `py_compile`+`pyflakes`(undefined) чисто; md5 local==server `6168a06fd650b9f9102525d9a1e782a3`.
  Опечатки в банках (Pass C) НЕ трогал. grid_create/grid_finalize не менялись.

## Последняя сессия: 2026-06-30 — Pass A батча porg-24rg6hzy: #2 автотаргет tp2/tp4, #5 фиды tp7, #3 дедуп очереди

Только КОД-ФИКСЫ (приёмка заморожена — ничего не создавал, верификация = код-трасса/dry-run).

- **#2 автотаргетинг tp2/tp4 = «Целевые + без бренда».** Корень: `grid_create.build_adgroup` применял
  профиль `search_tp2` (EXACT_V2_MARK+WITHOUT_BRAND, HAR38) ТОЛЬКО внутри `if autotargeting:`. Для
  групп с реальными ключами (`autotarget=False`) relevanceMatch выходил `isActive:False`, а боевые
  «все категории» — наследие старых v501-кампаний. Фикс (`grid_create.py:307`): ветка `search_tp2`
  вынесена ПЕРЕД `elif autotargeting` → профиль ставится НЕЗАВИСИМО от флага autotarget. tp1(РСЯ)/
  tp3/tp5 (profile="") не тронуты. Путь tp2/tp4 — всегда cookie (`if True` в _TEXT_ENGINE) →
  `_create_text_via_cookie` → `create_full(network=False,search=True)` → `at_profile=search_tp2`.
  Верификация (детерминисто, LXC101): build_adgroup search_tp2 при at=True И at=False → cats=
  ['EXACT_V2_MARK'] brand=['WITHOUT_BRAND']; profile="" at=True → 5 cats+3 brand (не сломано);
  create_full: tp2/tp4 spec→at_profile='search_tp2', tp1 RSYA→''.
- **#5 tp7 размножался по ВСЕМ фидам аккаунта.** Корень: в `api_set_plan` (`blueprint.py:4404`) список
  `feeds` (источник fan-out tp7 на :4640-4641) строился из ВСЕХ фидов (v5 URL-feeds / `_grid_feeds`)
  БЕЗ фильтра, хотя tp1/tp5 уже фильтруют через `_filter_allowed_feed_rows`. Фикс: обе ветки (v5 и
  Grid) обёрнуты в `_filter_allowed_feed_rows` (тот же allow-list «Глобальных правил», не дублирую).
  Пустые правила → [] (безопасный дефолт: не плодить). Верификация (Victory read + dry-run): глоб.
  правил enabled=11; боевые yandex-belgee/changan/chery/chevrolet/datsun/dfm/exeed → DROPPED,
  остаются только разрешённые. Применится при следующем построении плана Семёном.
- **#3 один логин 3× в очереди.** Корень: pre-scan в `api_create_set_async` отпускал
  `_CREATE_JOBS_LOCK` ДО `_job_new` (TOCTOU) → два сабмита подряд вставляли обе копии. Фикс:
  `_job_new(..., dedup_login=True)` (`blueprint.py:1013`) — проверка+вставка под ОДНИМ локом;
  на дубль возвращает существующий job_id (body._job_id → существующая джоба). Эндпоинт
  (`:12071`) сохранил pre-scan (UX-сообщение) + передаёт dedup_login=True (атомарный бэкстоп).
  Внутренние постановки (докрутка/resume/delete_drafts) = dedup_login=False (намеренные).
  Верификация (LXC101, DB-save застаблен, без воркера): 2 сабмита→1 джоба, queue_len=1; после
  status=done новый сабмит разрешён; dedup_login=False даёт 2 разные джобы.

Деплой: `py_compile`+`pyflakes`(undefined) чисто; md5 local==server (blueprint
`6557028be1485ee9bda1e439a6958cf0`, grid_create `b9c4fd5db2e4c6526d39a54dee9bb297`, grid_finalize
не менялся). Финальный рестарт — за Семёном (reloader подхватит синк сам). Ничего не создавал.

## Последняя сессия: 2026-06-29 — мусорный vendor/name-фильтр на «Общее»-группах (БАГ + чистка) + ВАЖНО про reloader

### Баг и фикс кода
- Симптом (porg-24rg6hzy, камп.711761905): ShoppingAd/ListingAd групп сегмента «Общее» имели
  `vendor CONTAINS_ANY [avtokredit/trade-in/avito/drom/avto/avtoru/avtosalon]` → 0 товаров. Корень:
  call-sites строили vendor/name-фильтр КАК ТОЛЬКО есть brand, без проверки сегмента; для «Общее»
  brand = имя темы → `_vendor_value`/`_listing_name_value` транслитерили тему в мусор.
- Фикс (`blueprint.py`): (1) call-site gate в ОБОИХ путях — токен `~7221` и куки `~9641`:
  `_is_brand_seg = _g_seg in ("Марки","Модели")`; vendor/name строится только при `_is_brand_seg`.
  (2) helper-защита: новый `_known_brand_canons()` (~5650, кэш `_BRAND_CANON_SET`) — набор канонов
  РЕАЛЬНЫХ марок, выведенный из `_ct_segment_map()` (тот же классификатор Марки/Модели/Общее);
  `_is_brand_canon()` пермиссивен при пустом справочнике; `_vendor_value`/`_listing_name_value`
  возвращают значение ТОЛЬКО для реальной марки. Темы → None. Реальные марки не задеты
  (BAIC→baic, Belgee X50→belgee/belgee x50, Лада→lada; 43 марки в наборе).
- Деплой: py_compile+pyflakes чисто; md5 local==server `acb23f83ec181f0a57347dac54752a0f`; сервис active.

### Ретро-чистка (live Grid, БЕЗ systemctl)
- Механизм очистки shopping: `set_default_text(ids, feed, DefaultTexts[0])` БЕЗ `filters_by_ad_id`
  → full-update без feedFilter обнуляет `FeedFilterConditions` (текст сохраняется). `tab:"ALL"` НЕ
  существует в enum `GdFeedFilterTabInput` — клир именно через отсутствие feedFilter.
  Листинг: `updateListingAds` (full item, без feedFilter), bodies = текст shopping группы.
- Эталон 711761905: 7 junk shopping → null (целевое 1914218250040416289 avtokredit→null, текст цел),
  8 listings updated. v5-верификация: все 8 shopping `FeedFilterConditions=null`; товары видны
  (FeedOffersPreview []→офферы, фид 197; vendor=avtokredit→0 — подтверждает «0/не нашлось»).
- Sweep всего аккаунта (44 камп.): **42 shopping + 56 listings очищено, 0 ошибок**. Все was-фильтры
  только темы (avito/avto/avtokredit/avtoru/avtosalon/drom/trade-in) — НИ ОДНОЙ марки (sweep трогает
  только «Общее»-группы; «Марки»/«Модели» не читаются). Чтение фильтра: v5 `ShoppingAdFieldNames`
  включает `FeedFilterConditions`/`DefaultTexts`. LISTING_AD фильтр через v5 НЕ читается (нет группы
  полей), Grid `client.ads` rowset резолвер в этом аккаунте отдаёт HTTP500 — listing верифицирован
  успехом мутации + доказанной идентичной механикой обнуления shopping.

### ⚠️ КРИТИЧНО: digest.service запущен с Werkzeug stat-reloader → правка .py = ДЕ-ФАКТО РЕСТАРТ
- В журнале `* Restarting with stat` в момент mutagen-синка моей blueprint.py (18:27 UTC). Werkzeug
  use_reloader перезапускает Flask-ПРОЦЕСС внутри юнита (systemd `ActiveEnterTimestamp` НЕ меняется,
  PID меняется 737659→740380→740453). Это убило in-memory воркер → **вся очередь (8 джобов, вкл.
  resume-джоб porg-24rg6hzy `8bad9a37b1f0` на 85/114) стала `interrupted`**. Т.е. «правка файла без
  рестарта» в этом сетапе НЕВОЗМОЖНА — любой синк .py авто-рестартует. Семён исходил из обратного.
- Код-фикс ОДНАКО уже live (reload подхватил новый blueprint.py) → новые кампании получают фикс.
- Восстановление НЕ делал автоматически (auth-сессия Семёна нужна для resume-эндпоинта; не плодить
  конфликт/дубли при активной сессии Семёна). Джоб `8bad9a37b1f0` resumable: `POST
  /direct/api/jobs/8bad9a37b1f0/resume` (body с 114 items цел, новый код `_already_in_direct`
  пропустит 85 существующих → досоздаст ~29). Остальные 7 interrupted-джобов — отдельная
  переоркестровка батча (решение Семёна).
- Сейчас активных джобов 0, сервис active. НЕ верифицировано: фактический resume (оставлено Семёну).

## Последняя сессия: 2026-06-29 — ускорение cookie/Grid: persist-кеш imageHash на аккаунт (пункт 1)

- Корень медленного cookie-пути (py-spy PID720554, Thread-7/8 висели в `upload_image→getresponse`,
  grid_finalize.py:436/422): кеш `_uploaded_by_name` создавался ЗАНОВО на КАЖДУЮ кампанию → одни и те
  же бренд-картинки заливались в Яндекс повторно для каждой РК (~1 РК / 15 мин на account-run из 114).
- Фикс (`blueprint.py`, только кеш, БЕЗ изменения таймаутов/параллельности):
  - модульный `_GRID_IMG_HASH_CACHE: dict[(login, realpath)->hash]` + `_GRID_IMG_HASH_LOCK` (threading)
    + `_GRID_IMG_CACHE_STATS` (~5262, перед `_grid_set_ad_prices`); хелпер `_cached_upload_image(gc,login,path)`:
    под локом check кеша → miss → `gc.upload_image` ВНЕ лока → запись под локом. КЛЮЧ=realpath (НЕ
    basename: у разных брендов файлы `1.jpg` совпадают — basename перепутал бы хеши).
  - 5 call-sites `upload_image` → `_cached_upload_image`: cookie tp1 РСЯ-добивка (~9772), token tp2/tp4
    repair (~6599), token tp1 repair (~7139), cookie tp2/tp4 backfill (~9926). Внутри-кампанийный
    `_uploaded_by_name`/лимит-5/image_hashes-приоритет СОХРАНЕНЫ — глобальный кеш только ДОПОЛНЯЕТ.
  - лог-маркеры `print("[img-cache] HIT/MISS", flush=True)` (MISS=реальный аплоад всегда; HIT каждый 25-й)
    — видны в journalctl, оставлены для мониторинга.
- Верификация (ФАКТ, live):
  - (1) детерминисто — 3 кампании×2 картинки (basename совпадает `1.jpg`) → upload вызван 2 раза (НЕ 6),
    realpath-ключ даёт РАЗНЫЕ хеши одинаковому basename, повтор=hit без upload.
  - (2) REAL-DATA (реальные файлы аккаунта через `_tp1_pack_groups`, реальный GridClient):
    BAIC/Belgee/Changan → 3 РАЗНЫХ реальных Yandex-хеша (нет путаницы бренд↔картинка); 2-й проход
    (= след. кампания) = **0 реальных upload**, хеши идентичны 1-му проходу по каждому бренду.
  - (3) live-resume porg-24rg6hzy (job 8bad9a37b1f0): created 7→20 (+13 кампаний) за ~7 мин при
    **upload заморожен на 399** (0 новых аплоадов у 13 кампаний); кеш-HITS=**1225**, дублей upload=**0**
    (целостность кеша). Базлайн был ~1 РК/15 мин с переаплоадом — kратное ускорение подтверждено.
    Маркеры `[img-cache] MISS/HIT` видны в journalctl. Сервис active, 302, 0 traceback.
- Деплой: `py_compile`+`pyflakes`(undefined) чисто, md5 local==server `9356b71b...`, рестарт active, 302.
- TODO (отдельный пункт, НЕ делал): таймауты/параллельность upload_image. Job 8bad9a37b1f0 продолжает
  докручивать porg-24rg6hzy в фоне (полезная работа — досоздаёт зависший аккаунт).

## Последняя сессия: 2026-06-29 — быстрые ссылки: цифры в описаниях (слепки-мастер)

- Корень: описания быстрых ссылок генерировались/подставлялись без цифр — «сухая вода».
- Три рычага исправлены в ai_agents.py + blueprint.py:
  1. **Промпт** `build_sitelinks_messages` (ai_agents.py ~2009): добавлено явное требование цифры/УТП-маркера
     в КАЖДОМ описании. Аналог требования для заголовков, теперь распространён на описания.
  2. **Банки-фолбэки** (ai_agents.py): обогащены цифрами, исправлены длины < 45,
     удалён заблокированный «Запишитесь на тест-драйв» (был в `_bad_ad_sitelink`-стопе).
     Правки: `COMMON_SITELINK_BANK` (7/8), `sitelink_bank_for` Квиз (8/8),
     `AGENT_ADS` pavlov/kryuchkova/scherbakova/terehov (полный пересмотр), `EXAMPLE_BANK.sitelinks` (9/12).
  3. **Number-gate** `_take_sitelinks` (blueprint.py ~13991): трёхпроходный гейт
     (strict: оба поля с цифрой → loose: хотя бы desc с цифрой → fallback: любое).
- `py_compile`+`pyflakes` чисто (всё pre-existing). md5 local==server: ai_agents
  `6a49c686790a6b52f43de2a346c286b4`, blueprint `6de9eba73f0cdc7e0fa18673643f87c1`.
- `digest.service active`, `/direct/automation` 302, лог без traceback.

## Сессия: 2026-06-29 — отзывчивая «Отмена»: проверка cancel ПЕРЕД каждой fan-out кампанией

- Было: cancel проверялся только МЕЖДУ пунктами плана (api_create_set, начало пункта). Один пункт =
  fan-out на N кампаний (cpc/cpa + размножение по фидам) → если воркер засел внутри пункта (30-60с/РК),
  «отменяю…» висело до конца пункта.
- Добавлены проверки `if job/_job and …get("cancel"): break` на ГРАНИЦЕ между fan-out кампаниями
  (текущая достраивается, следующая не начинается — без битых черновиков). 6 точек:
  - `_create_tp1_via_cookie` variants cpc/cpa (~9550) — добавлен параметр `job`, прокинут `job=_job`;
  - `_create_tp1_campaign` (v5) перед cpa (~9298) — добавлен `job`, прокинут `job=_job`;
  - api_create_set tp1_rsy feed-loop (~11005) — перед каждым фидом;
  - `_create_tp5_campaign` feed-loop (~10257) + pair-loop (~10263);
  - `_create_tp3_campaign` feed-loop (~10380) + pair-loop (~10385).
  - tp6/tp7 (UAC) — 1 кампания на item, fan-out по фидам уже в ПЛАНЕ (отдельные item'ы) → покрыто
    item-level проверкой.
- Безопасность: break наружу → `_bump_item` + continue → item-level cancel → финализация; created по
  факту. Нормальный путь (без cancel) не затронут (условие False).
- Деплой+РЕСТАРТ (Семён хотел обнулить очередь): `digest.service active`, `/direct/automation` 302,
  лог без traceback, in-memory очередь обнулена. `py_compile`+`pyflakes` чисто.

## Сессия: 2026-06-29 — tp1-фид фильтры: товары=vendor, каталог=name (БЕЗ РЕСТАРТА)

- Семён («фатально»): ДВА фильтра по типу объявления (мой task-6/collectionId сломал товары):
  - **Товары (ShoppingAd)** → `vendor CONTAINS_ANY [марка]` (как до task-6; НЕ collectionId).
  - **Страницы каталога (ListingAd)** → `name CONTAINS_ANY [марка|марка+модель]` — Grid `mutation
    updateListingAds` (строчная u, полный item: permalinkWithPhone/bodies/inheritable*; HAR36).
  - ct0000/общее → без фильтра.
- КОД-ФИКС: `grid_finalize.GridClient.set_listing_name_filters` (updateListingAds name CONTAINS_ANY);
  `blueprint._vendor_value`/`_listing_name_value` (кир→лат через `_brand_canon`); откат шоп-айтемов на
  vendor (collection_id=None) + листинги через `add_listing_ads_by_shopping_ads`+name-фильтр в ОБОИХ
  путях: v5/token (`_build_tp1_adgroups` ~7165, `_create_tp1_single` ~9156) и cookie
  (`_create_tp1_via_cookie` ~9576). `_add_listing_ads_v501` остался только у tp5 (не трогал).
- Верификация НОВОГО кода end-to-end (свежая кампания porg-24rg6hzy, импорт синканутого кода БЕЗ
  рестарта): товары → `vendor ["belgee"]`, листинг → `name ["belgee"]`. Хелперы: Belgee→belgee,
  "Belgee X50"→"belgee x50", Лада→lada.
- LIVE-РЕПЕЙР существующих (БЕЗ рестарта, Grid): камп. **711718476** (psm5h7q6) — 35 товаров→vendor,
  35 листингов→name. **Belgee гр.5768894941**: товары `vendor ["belgee"]` (превью 4 оффера — ТОЛЬКО
  Belgee), каталог `name ["belgee"]` (превью 5 страниц — ТОЛЬКО Belgee, нет Lada/Nissan/Tank). ✓
  (set_default_text применяет vendor live; updateListingAds — name live.)
- ⚠ РЕСТАРТ НУЖЕН, чтобы РАБОТАЮЩИЙ сервис применял новый код к НОВЫМ кампаниям (воркер держит старый
  код в памяти). НЕ рестартовал (Семён в бою). Остальные tp1-кампании psm5h7q6 — sweep начат, прерван
  по таймауту; дочистить тем же скриптом (репейр листингов/товаров по бренду ct).

## Сессия: 2026-06-29 — проверка мест показа tp1-РСЯ: УЖЕ network-only (правок НЕ потребовалось)

- Запрос (Семён, СТРОГО БЕЗ РЕСТАРТА): tp1-РСЯ места показа = только «Рекламная сеть Яндекса»
  (network=true, search/gallery/organic/Карты/орг/Яндекс-Карты — OFF).
- ФАКТ (живое Grid-чтение, read-поля `platformGroups`/`placementTypes`/`enableCompanyInfo` —
  write `biddingStategyWithPlatforms` на чтении не отдаётся): **7/7 боевых tp1 на psm5h7q6 + свежесозданная
  на porg-24rg6hzy** → `platformGroups=['NETWORK']`, `placementTypes=[]`, `enableCompanyInfo=False`.
  Т.е. УЖЕ network-only: search/gallery/organic/serpGeoWizard/yandexMaps все false (иначе platformGroups
  содержал бы 'SEARCH' и др.). Leak от tp2/tp4 `_search_platforms` НЕТ (tp1 → `_PLATFORMS_RSYA` network-only +
  `_finalize_rsya`; код корректен).
- Единственное не-«ВЫКЛ»: `dynamicPlacesWereDisabled=False` («динамические места»). НО: dynamic places
  НЕ активны (`isEligibleForDynamicPlacesAutoOn=False`), и это поле НЕ принимается `UpdateCampaigns`
  (добавление в payload → `updated:0`, апдейт ломается). Через рабочую мутацию мест показа выключить
  нельзя; фактически не показываются.
- ИТОГ: репейр платформ НЕ нужен (уже network-only), код-правки НЕТ, рестарт НЕ делал. Если Семён
  видит иное в UI — нужен id конкретной кампании + скрин (API однозначно показывает network-only).

## Сессия: 2026-06-29 — БЛОКЕР: tp1-фид товарка не создавалась (collectionId CONTAINS_ANY) — ПОЧИНЕНО

- Симптом (боевой porg-24rg6hzy, job 58bf8b3dba0d): `tp1(куки): partial-кампания удалена — ShoppingAd/
  ListingAd не созданы` (×N), created=0. Реальная Grid-ошибка в errors_log/journalctl НЕ печаталась.
- Достал ДОСЛОВНО (инструментировал `gf.GridClient._post`, репро на porg-24rg6hzy): `AddShoppingAds`
  с 138 items вернул 1 created + 137 **null**, validationResult:
  `PerformanceFilterDefects.PerformanceFilterDefectIds.INVALID_OPERATOR @ adAddItems[N].feedFilter.
  conditions[0].operator`. Фильтр брендовой группы: `{field:collectionId, operator:CONTAINS_ANY,
  stringValue:"[\"25\"]"}`. item[0] (ct0000, filter=null) создавался — падали только брендовые с фильтром.
- КОРЕНЬ (регресс task-6, мой): перевёл брендовые группы с vendor-фильтра на **collectionId**, но
  `add_shopping_ads`/shopping_filters строили collectionId с оператором **CONTAINS_ANY** — Grid его НЕ
  принимает для collectionId (нужен **EQUALS_ANY**; vendor=CONTAINS_ANY валиден, collectionId=EQUALS_ANY).
- ФИКС: collectionId оператор CONTAINS_ANY → **EQUALS_ANY** в 3 местах:
  `grid_finalize.py:283` (add_shopping_ads), `blueprint.py:7202` (_build_tp1_adgroups shopping_filters),
  `blueprint.py:9625` (cookie-путь shopping_filters). (tp7 UAC `it_lff` ~11735 — другой API, не трогал.)
- ДОКАЗАТЕЛЬСТВО: прямой `add_shopping_ads([{collection_id:'25', feed:3501030}])` после фикса →
  SENT `{collectionId, operator:EQUALS_ANY, ["25"]}` → `addedAds=[{id:1914194420486028391}], errors=[]`
  (ShoppingAd СОЗДАН; раньше CONTAINS_ANY → INVALID_OPERATOR → null). Это тот же EQUALS_ANY, что я ранее
  подтвердил на листинге (collectionId 25). ct0000 без бренда (vendor=None/coll=None) — без фильтра, не затронут.
- Тестовые черновики удалены (test_left=0). Деплой: `py_compile`+`pyflakes` чисто, рестарт active, 302.

## Сессия: 2026-06-29 — LIVE-проверка создания tp6/tp7 (UAC) — РАБОТАЕТ

Создал ЖИВЬЁМ на безопасном `porg-24rg6hzy` (scherbakova/Мультибренд, draft, launch=False) через реальный
API set_plan→create_set: tp6 master(autotarget), tp7 product(autotarget), tp7 product(keywords).
- **created=3, failed=0** (id 711711822 / 711711866 / 711711929). Создание не сломано ни одной правкой сессии.
- autotarget-аудитория = OPTIMAL: keywords=0, socdem age_18+оба пола, relevance_match = 5 категорий (selected).
- картинки: **content_ids=5** (порог phash=10 НЕ срезал ниже 5) — дедуп ок.
- заголовки 5 / тексты 3, заполнены, с цифрами, разные УТП (банк `_GENERIC_AT_TITLES`).
- tp7 товарка: feed_id=3501015 + listings_feed_id выставлены (ecom). Бренд-ct в структуре этого аккаунта
  нет (всё ct0000) → collectionId-фильтр листинга на нём не покрывался (только ct0000-товарка).
- Тестовые черновики УДАЛЕНЫ (0 осталось). РЕСТАРТ НЕ делал (создание корректно; Семён мог быть в бою).

⚠ Находка (НЕ ломает, на след. деплой-окно): `_TP67_OPTIMAL_CATEGORIES` содержит `EXACT_MARK`/`COMPETITOR_MARK`,
которые UAC для этого типа кампании НЕ принимает → молча нормализует к валидным 5
(`ALTERNATIVE/BROADER/ACCESSORY/EXACT_V2/NARROW_MARK` = исходный `_TP67_RELEVANCE_CATEGORIES`). Итог
функционально верный (autotarget с 5 категориями), но опираться на тихую нормализацию не стоит — в
следующий рестарт привести `_TP67_OPTIMAL_CATEGORIES` к валидным enum (для этого типа optimal==те же 5).
Реальная #7-логика (keywords пусто + socdem полный age_18) РАБОТАЕТ.

## Сессия: 2026-06-29 — code-review гейт: 12 багов сессии (11 починено, 1 → слепки-мастер)

MUST-FIX (ломали создание/теряли данные):
- **#1** 152 на via_cookie-резюме терялось молча: 152-deferral гейтился `not via_cookie`. Перевёл на
  ИМЕНОВАННЫЙ сбор `_units_failed_names` (пункты с маркером 152, по имени+fan-out-префикс) — работает на
  любом пути. `_units_block` выводится из факта; `_job["done"]=len(items)`. (`blueprint.py` ~11751/11829)
- **#2** tp3-куки `_finalize_rsya(..., placement_types=…)` → TypeError (нет параметра) → finalize глушился.
  Убрал `placement_types=` из вызова tp3. Репро: tp3-куки `711710649` создалась ok=True/err=None; сигнатура
  `_finalize_rsya` без placement_types. (`blueprint.py` ~9951)
- **#3** PLACEMENTS_TP5 ушёл на tp3 вместо tp5 → tp5 терял ADV_GALLERY. Перенёс
  `placement_types=list(gf.PLACEMENTS_TP5)` в `_finalize_search_via_grid` (tp5). (`blueprint.py` ~9962)
- **#4** `_feed_collections` кэшировал пустой/сбойный результат навсегда → брендовые группы теряли
  collectionId. Кэшируем ТОЛЬКО непустое. Репро: сбойный аккаунт → [] и не закэшировано. (`blueprint.py` ~5546)
- **#5** `_already_in_direct` перематчивал fan-out tp1_rsy (одна фид-кампания → весь пункт пропущен).
  tp1_rsy исключён из item-skip; добавлен skip ПОФИДОВО по полному `nm`. (`blueprint.py` ~10918/11003)
- **#6** Бренд-матч без кир↔лат: `Лада`/`Москвич` не матчили `Lada`/`Moskvich`. Добавил `_brand_canon`
  (алиасы + транслит) + `_brand_in_name`; применил в `_brand_level_collection_id`/`_brand_collection_ids`.
  Репро: Лада→25, Lada→25, Москвич→7, чери/Chery→chery. (`blueprint.py` ~5555)
- **#7** crash-safety flip не инкрементил `resume_count` → ядовитый набор крутился вечно. Добавил
  `resume_count+1` + кап `< _RESUME_MAX`. (`blueprint.py` ~654)

SHOULD-FIX:
- **#8** форс-cookie tp2/tp4 потерял campaign/shared минуса (только групповые). Добавил best-effort v5
  campaign-direct/shared-set после cookie-создания (работает при баллах; на 152 деградирует). (`blueprint.py` ~11206)
- **#9** блокирующий backoff `_pack_groups_with_retry` (2+4с)+m3(5с) на синхронном route. retries 3→2,
  sleep→0.5с, m3 timeout 5→3с. (`blueprint.py` ~9436)
- **#10** phash порог 18→**10** (из 63 бит) — не схлопывать разные баннеры. (`campaign.py` ~1627)
- **#13** defer попадал в errors_log как ошибка (tp1) — guard `not res.get("defer")` в 3 хендлерах +
  cookie tp2/tp4. (`blueprint.py`)
LOW:
- **#12** `int.bit_count()` → `bin(x).count('1')` (портативность py<3.10). (`campaign.py` ~1662)

ПЕРЕДАНО слепки-мастеру (контент tp6/tp7):
- **#11** number-gate обходится финальными добивками (`_fallback_master_titles`/brand refill) — заголовки
  без цифры проходят для брендовых. Это та же контент-машинерия tp6/tp7, что #6 (их домен).

Деплой: `py_compile`+`pyflakes` (undefined name) чисто; md5 local==server (blueprint/campaign/grid_create);
work-стейт валиден; рестарт `digest.service active`, `/direct/automation` 302.

## Сессия: 2026-06-29 — БЛОКЕР-ФИКС runtime NameError `grid_cookie` в tp1-куки

- Симптом (логи porg-ozge4ntu, ×17): `tp1(куки): name 'grid_cookie' is not defined` — runtime NameError,
  py_compile молчал.
- Корень: `_create_tp1_via_cookie` (`blueprint.py:9448`) НЕ имеет параметра `grid_cookie`, но строка
  **9529** (добавлена в задаче catalog-pages фид-фильтров, cookie-путь) звала
  `_feed_collections(login, int(feed_id), cookie=grid_cookie)`. Регресс — классика «прокинул в вызов,
  в сигнатуру не добавил» (как было с `login`).
- Фикс (`blueprint.py:9529`): убрал `cookie=grid_cookie` → `_feed_collections(login, int(feed_id))`;
  `_feed_collections` сам берёт `pick_working_cookie(login)` — ТОТ ЖЕ источник куки, что и весь
  cookie-путь (Grid-клиенты внутри `_create_tp1_via_cookie` тоже подбирают куку через
  pick_working_cookie). Нового источника куки НЕ вводил.
- Верификация: `pyflakes` undefined — чисто; AST-проверка по ВСЕМ функциям — `grid_cookie` нигде не
  свободная переменная; runtime-репро `_create_tp1_via_cookie(... with_shopping=True, feed_id=3501080)`
  на porg-ozge4ntu — строка 9529 ПРОЙДЕНА без NameError (остаточная ошибка = партиал-клинап
  тестового несовпадения фид/слепок, не grid_cookie; тест-кампания авто-удалена). В свежем логе
  `grid_cookie` отсутствует. Деплой: `digest.service active`, `/direct/automation` 302.

## Последняя сессия: 2026-06-29 — #6 tp6/tp7 контент-качество (слепки-мастер) — СДЕЛАНО

### #6 — tp6/tp7 число в каждом заголовке/тексте + разные УТП — СДЕЛАНО, верифицировано

Что сделано (`blueprint.py`):
- `_GENERIC_AT_TITLES` (~4187): заменены 3 плохие строки (2× «Купить новый авто», 2× без цифры).
  Теперь 8 строк — все с цифрой, 8 разных первых слов (Авто/Кредит/Купить/Автокредит/Выгода/
  Трейд-ин/Новые/Одобрение), 8 разных УТП-бакетов (платёж/взнос/КАСКО/банки+срок/скидка/трейд-ин/
  наличие/одобрение). Ни одна не блокируется `_bad_ad_title`.
- `_GENERIC_TEXT_FILLERS` (~4201): заменены 2 строки: «Автокредит» → «Кредит» (автокредит
  блокировался `_bad_ad_text`); добавлен «30 минут» в трейд-ин текст. Теперь 4/4 с цифрой.
- `_GENERIC_TITLE_FILLERS` (~4178): «Оценим авто в трейд-ин. Выше рынка при кредите»
  → «Оценим авто в трейд-ин. Платеж от 9 000 ₽/мес» — добавлена цифра.
- `_fallback_master_titles` (~8093): generic — 3 строки без цифры заменены (добавлены 9 000 ₽/мес,
  2026, 9 000 ₽/мес); brand — 4 строки без цифры заменены (от 15 банков, 9 000 ₽/мес, 30 минут,
  2026/Взнос 0 ₽).
- `_brand_title_set` (~8484): «Трейд-ин до 150% цены авто» (блокировался tradein_150)
  → «Трейд-ин на авто за 30 минут» — теперь проходит фильтр и содержит цифру.
- Handler tp6/tp7 (~11378, ~11431, ~11514, ~11539): добавлен number-gate `not _has_number(_t/x)`
  в 4 точках (основной цикл заголовков, основной цикл текстов, regen-путь×2). Цифра теперь
  обязательна как в tp1–tp5.

Верификация «M3 empty» (LXC101, фильтры `_bad_ad_title`/`_bad_ad_text` + number-gate):
- ct0000 заголовки: 8/8 → OK (5 нужно)
- ct0000 тексты: 4/4 → OK (3 нужно)
- BAIC `_brand_title_set`: 8/8 → OK (5 нужно)
- BAIC `_fallback_master_titles`: 6/6 → OK
- ct0000 `_fallback_master_titles`: 6/6 → OK

ДО/ПОСЛЕ ct0000 (scherbakova/Мультибренд, tp6 или tp7, «M3 empty»):
  ДО (5 заголовков): 1) «Авто в кредит от 9 000 ₽/мес. Одобрение онлайн» ✅
                     2) «Кредит на новый авто. Первый взнос 0 ₽» ✅
                     3) «Купить новый авто. КАСКО на 1 год бесплатно» ✅
                     4) «Автокредит от 15 банков. Решение за 30 минут» ✅
                     5) «Купить новый авто. Выгода до 57% в кредит» ❌ повтор «Купить новый авто»
  ПОСЛЕ (5 заголовков): 1) «Авто в кредит от 9 000 ₽/мес. Одобрение онлайн» (платёж)
                        2) «Кредит на новый авто. Первый взнос 0 ₽» (взнос)
                        3) «Купить новый авто. КАСКО на 1 год бесплатно» (КАСКО)
                        4) «Автокредит от 15 банков. Решение за 30 минут» (банки+срок)
                        5) «Выгода до 45% при покупке. Кредит от 15 банков» (скидка)
                        → все с цифрой, все разные первые слова, без повторов ✅

  ДО (3 текста): 1) «Автокредит от 9 000 ₽/мес...» ❌ блокируется _bad_ad_text
                 2) «Первый взнос 0 ₽. КАСКО на 1 год...» ✅
                 3) «Трейд-ин выше рынка. Оценим авто онлайн...» (без цифры, попадало через пробел)
                 4) «Новые авто в наличии. Решение... 30 минут» ✅
  ПОСЛЕ (3 текста): 1) «Кредит на авто от 9 000 ₽/мес. Подберем условия от 15 банков...» (платёж)
                   2) «Первый взнос 0 ₽. КАСКО на 1 год бесплатно...» (взнос+КАСКО)
                   3) «Трейд-ин выше рынка. Оценим авто за 30 минут...» (трейд-ин+срок)
                   → все 3 с цифрой, no «автокредит», разные УТП ✅

ДО/ПОСЛЕ BAIC (брендовая группа, tp6/tp7, «M3 empty»):
  ДО: «_brand_title_set» содержал «Трейд-ин до 150% цены авто. BAIC» → блокировался tradein_150
  ПОСЛЕ: «Трейд-ин на авто за 30 минут. BAIC» → проходит, цифра 30 ✅

Деплой: `py_compile` OK; md5 local==server 8545c72cc69534e6e0d0080798c4f92e;
  `digest.service active`; `/direct/automation` → 302; лог без traceback.

---

## Предыдущая сессия: 2026-06-29 — #3 alias tp4→tp2 пак + диагностика tp6/tp7

### #3 — tp4 берёт ключи из tp2-пака (решение Семёна) — СДЕЛАНО, верифицировано live
- Правка (`blueprint.py` ~9348, `_tp1_pack_groups`): `_pack_tp = "tp2" if tp_code=="tp4" else tp_code`;
  `kp.gather(key, site_type, _pack_tp)`. Алиас ТОЛЬКО для источника пака; место показа (organic=True,
  #2), нейминг/кодер, тип Поиск+Динамика, корректировки tp4 — остаются tp4 (`tp_code` не подменён).
  Затрагивает только tp4 (tp1/tp2/tp5 не тронуты; v501 tp2/tp4 мёртв после #1-форса cookie).
- Live (psm5h7q6, scherbakova/Мультибренд, через `_tp1_pack_groups`):
  ДО (сырой пак tp4): 139 групп, 4 с 1 ключом, 15520 ключей.
  ПОСЛЕ: tp4 = **103 группы, 12294 ключей — ИДЕНТИЧНО tp2**. (1 группа с 1 ключом осталась, но она
  ТАКАЯ ЖЕ у tp2 — это свойство tp2-контента после фильтра брендо-модельных ключей, не дефект tp4.)

### tp6/tp7 контент-качество (generic-заголовки, #6) — ДИАГНОСТИКА, передано слепки-мастеру
- Корень (по коду creation-handler tp6/tp7, `blueprint.py` ~11322-11435): заголовки/тексты собираются
  из ЛОКАЛЬНЫХ fallback-БАНКОВ — `_GENERIC_AT_TITLES` (8 строк, несколько «Купить новый авто»),
  `_brand_title_set`, `_GENERIC_TITLE_FILLERS`, тексты `_GENERIC_TEXT_FILLERS` (4 строки). Пост-обработка
  есть (`_coherent_discounts`, `_trim_to_word(56)`, `_diverse_text_offers`), НО НЕ применяется
  UTP-bucket-разнообразие (`_title_utp_bucket`, 3-ступенчатый отбор) и гейт «цифра в каждом заголовке»,
  которые есть у tp1–tp5. ⇒ generic/повторы/часть без цифр.
- Это КОНТЕНТ (формулировки банков `_GENERIC_AT_TITLES`/`_GENERIC_TEXT_FILLERS`/`_brand_title_set` +
  правила разнообразия/добивки длины) = домен `direct_slepki_master` (как #6, ранее туда же). По моей
  роли контент слепков не правлю. ПЕРЕДАНО слепки-мастеру с диагнозом: (а) обогатить банки разными
  УТП-бакетами (платёж/взнос/скидка/трейд-ин/КАСКО/банки/срок/наличие), цифра в каждом, длина к 56;
  (б) при желании — подключить `_title_utp_bucket`+number-gate к tp6/tp7-сборке; (в) бренд только при ct.

### Деплой
- `py_compile`+`pyflakes` чисто; md5 local==server; рестарт `digest.service active`, `/direct/automation` 302.

## Сессия: 2026-06-29 — хвосты Михаила (#1 cookie-форс, #5 tp5-нейминг, #3 анализ)

### #1 — tp2/tp4 ВСЕГДА через cookie/Grid (решение Семёна) — СДЕЛАНО, верифицировано live
- Корень: v5/v501 relevanceMatch не умеет (404) → дефолт все 5 категорий + все бренды; только cookie
  (`build_adgroup` profile `search_tp2`) ставит EXACT_V2_MARK + WITHOUT_BRAND.
- Правка (`blueprint.py` ~11118): в `_TEXT_ENGINE`-handler (tp2/tp4) условие выбора пути заменено на
  `if True:` → ВСЕГДА `_create_text_via_cookie` (v501-ветка ниже сохранена, но недостижима для tp2/tp4).
  На cookie-пути применяются места показа (`_search_platforms`, #2), ключи/минуса/контент/корректировки.
- Live (psm5h7q6, создана tp2 `711701048`, прочитано через Grid `adGroups.relevanceMatch`):
  `relevanceMatchCategories=['EXACT_V2_MARK']`, `autotargetingBrandSettings=['WITHOUT_BRAND']` ✓
  (а не дефолтные 5 категорий + 3 бренда). Тестовая кампания удалена.

### #5 — tp5 имя группы по кодеру — СДЕЛАНО, верифицировано live
- Правка (`blueprint.py`): `_create_shopping_via_cookie` получил `ct`/`r_code`; имя группы =
  `_text_group_name(ct, r_code, "Товарная галерея")` (вместо хардкода). `_tp5_cookie_kwargs` и
  `_tp3_cookie_kwargs` прокидывают `ct=it.get("ct"), r_code=r_code_ctx`.
- Live (psm5h7q6, tp5 `711702114`): группа `ct0000_aon_n000_r0100_ct001_ag011_g00 — Товарная галерея`
  (кодер, не «Товарная галерея»). Тестовая кампания удалена.

### #3 — tp4 «1 ключ»: корень = КОНТЕНТ пака (не код), цифры
- `_tp1_pack_groups` извлекает `data.get("positive")` ИДЕНТИЧНО для tp2/tp4; разница только `tp_code`,
  передаваемый в `kp.gather(slepok, site, tp_code)` → паки tp2 и tp4 РАЗНЫЕ (by-design, per-tp).
- Замер scherbakova/Мультибренд: **tp2** = 103 группы, 22714 ключей, минимум 5/группа, групп с 1
  ключом — НЕТ. **tp4** = 139 групп, 15520 ключей, **4 группы с ровно 1 ключом**. На 70 общих ct: 68
  различаются, tp2 обычно богаче. ⇒ Корень — контент M3-пака tp4, НЕ код. Вопрос к слепкам/Семёну
  (alias tp4→tp2 пак — это контентное решение, вслепую не делал).

### Деплой
- `py_compile`+`pyflakes` чисто; md5 local==server; work-стейт валиден; рестарт `digest.service active`,
  `/direct/automation` 302.
- Открытый хвост от прошлой сессии: #6 (заголовки до 56) — отдан слепки-мастеру (не трогал).

## Сессия: 2026-06-29 — пакет Михаила (adPrice FROM, места показа, UAC-оптимал, +анализ)

### Сделано и задеплоено (HAR-точно, payload верифицирован детерминированно)
- **#4 adPrice prefix="FROM"** (HAR 29): `blueprint._grid_ad_price_payload` (~5244) и
  `grid_create.py` (~415): `prefix:None → "FROM"`. Оба места сборки adPrice. Проверка:
  `_grid_ad_price_payload(1940000,2180000)` → `{...,"prefix":"FROM","currency":"RUB"}`. Live ads.get — на
  следующем создании (действующих tp1-ads с adPrice на psm5h7q6 для re-apply не нашлось).
- **#2 места показа tp2/tp4** (HAR 33/34, UpdateCampaigns biddingStategyWithPlatforms): новый
  `_PLATFORMS_SEARCH_ONLY` + `_search_platforms(tp_code)` (~8830); `_finalize_search_via_grid` получил
  параметр `platforms`, передан в обоих tp2/tp4 путях (cookie ~9760, v501 ~11205). Различие = `organic`:
  **tp2 organic=False, tp4 organic=True**, оба `search=True, gallery=False, network=False`,
  `placementTypes=["SEARCH_PAGE"]`. Проверено: `_search_platforms('tp2'/'tp4')`. Применяется post-update.
- **#7 UAC «Подобрать оптимальную» для autotarget-only tp6/tp7** (HAR 34): `_TP67_OPTIMAL_CATEGORIES`
  = `[ALTERNATIVE,ACCESSORY,COMPETITOR,BROADER,EXACT]_MARK` (НЕ EXACT_V2/NARROW). В tp6/tp7 spec
  (~11647): при `targeting_mode=="autotarget"` → optimal-категории + `age_lower="age_18"` (keywords/
  audiences уже пустые в этом режиме). Прочие режимы — прежние. Live UAC — на следующем создании.

### НЕ доделано (точный корень, нужен HAR/решение — без правок вслепую)
- **#1 категории Поиска tp2/tp4/tp5.** ЭМПИРИЧЕСКИ (live Grid): tp2-группа psm5h7q6 камп.711632383 →
  relevanceMatch = ВСЕ 5 категорий + ВСЕ бренды (дефолт) → создана через **v501** (v5 relevanceMatch
  не умеет, 404 — blueprint:6269). Cookie-путь (`build_adgroup` profile `search_tp2`) ставит верно. Фикс
  v501-пути требует Grid **UpdateAdGroups** (group relevanceMatch post-update) — HAR этой мутации НЕТ.
  Нужен HAR сохранения смены автотаргет-категорий группы, ЛИБО решение «tp2/tp4 всегда через cookie».
- **#3 tp4 — 1 ключ.** tp2/tp4 cookie делят `_tp1_pack_groups` (один источник, различие — `tp_code`).
  Нужен разбор: зависит ли выборка ключей пака от tp_code (если tp4-сегмент пака беднее → контент-вопрос).
- **#5 tp5 имя группы.** `_create_shopping_via_cookie → create_shopping_full(group_names=["Товарная
  галерея"])` (хардкод). Фикс = пробросить ct/r_code → `_text_group_name`. Требует расширения сигнатуры.
- **#6 tp6/tp7 заголовки до 56.** Контент-добивка длины (ai_agents/слепки-сторона) — не трогал.


### Задача 7 — «Продолжить» докручивает только недостающее (resume-skip по имени)
- Корень: `api_job_resume` ставил новую джобу с тем же body (все items); докстринг врал про
  «set_plan пропустит» — в `api_create_set` skip'а НЕ было → resume гнал набор с нуля.
- Фикс (`blueprint.py` ~10650, ~10770): в начале `api_create_set` ОДИН раз bulk-читаем имена
  кампаний аккаунта (`_grid_list_campaigns`, без баллов) → `_existing_names`; в начале цикла
  `_already_in_direct(name)` (exact ИЛИ fan-out-префикс `name — feed`) → пропуск (skip-результат,
  `_bump_item` тикает heartbeat, без генерации M3). Упавшие/недойденные в Директе отсутствуют →
  создаются. `created` теперь без skipped; добавлен `skipped_existing`.
- Верификация ЖИВЬЁМ (без создания): psm5h7q6 (351 кампания), deferred-остаток 364 items →
  **skip 101 / create 263** (совпадение имён кодера подтверждено).

### Задача A — watchdog не красит «error» куки-бэкфилл
- Корень: error ставился по застою `_heartbeat`, который тикал только при `created++`; массовый
  skip (created не растёт) ложно выглядел как зависание.
- Фикс (`blueprint.py` ~456/466/742): `_heartbeat=time.time()` в `_bump_job` И `_bump_item` (каждый
  обработанный item, вкл. skip); watchdog не ставит error, если `done>=total>0`.

### Задача B — пустой M3-пак: retry + deferred вместо мгновенного fail
- Корень: `_create_tp1_via_cookie`/`_create_text_via_cookie` при пустом `_tp1_pack_groups` сразу
  возвращали permanent-fail (psm5h7q6: 57× «нет групп scherbakova/Мультибренд»).
- Фикс (`blueprint.py` ~9359): `_pack_groups_with_retry` (3× + backoff 2/4с; при пустом — лог статуса
  M3 `_m3_content_status`). Пусто после ретраев → `{"ok":False,"defer":True}`. В финале цикла defer-
  пункты (по имени, с fan-out-префиксом) → `_deferred_save` (не failed; resume_at=now, демон докрутит
  по куке; анти-цикл `_RESUME_MAX`). `failed`-счётчик исключает defer.
- M3 сейчас ЖИВ (`ok:true`) → ветка «M3 лежит» live не воспроизведена; логика по ревью.

### Задача 8 — постоянный индикатор статуса M3 в сайдбаре
- Бэкенд (`blueprint.py` ~3015): новый `/api/m3-status` (кэш 5 мин) → `{ok,detail,checked_at}`,
  переиспользует `_m3_content_status` (тот же источник, что зелёный баннер).
- Фронт (`templates/direct/index.html`): badge `#m3-status-badge` под «Копирование кампаний»
  (зелёный «M3 активен · есть доступ» / красный «M3 недоступен» + «проверено HH:MM»); `pollM3Status()`
  при загрузке + `setInterval` 20 мин; клик = ручной опрос.
- Верификация ЖИВЬЁМ: `/direct/api/m3-status` → `{ok:true, checked_at:"13:22", detail:"локальный
  индекс…фид-моделей 167"}`; badge+`pollM3Status` присутствуют в отданном HTML.

### Деплой 7/A/B/8 (вместе с задачей 6)
- `py_compile`+`pyflakes` чисто; md5 local==server; рестарт `digest.service active`,
  `/direct/automation` 302, `/login` 200, лог без traceback (кроме известного work/-инцидента, ниже).

---

## Сессия: 2026-06-29 — 152→куки бесшовно + TTL суток + crash-safety + реанимация

### Что сделано (blueprint.py)
- **Goal 1 — 152 = бесшовный переход на куки (НЕ deferred-до-полуночи).**
  - Корень: per-item inline-cookie-фолбэк после 152 БЫЛ, но внешний `_units_block` на ~10602 делал
    `break` и слал остаток в `direct_deferred_creates` с `resume_at=полночь`; демон
    `_resume_one_deferred` ждал баллов и крутил снова через **v5** (НЕ куки) → «висит без движения».
  - Фикс: в главном цикле `api_create_set` при ПЕРВОМ маркере 152 (`_units_seen`) флаг
    `via_cookie=True`, `_units_switched=True`, `_units_block=False`, **без break** — весь остаток
    набора создаётся по куке (Grid/UAC, без баллов) в той же джобе. Попап-согласие больше НЕ
    единственный путь для фоновой докрутки. Доп. note `units_switched`, `units_exhausted` теперь
    `(_units_block or _units_switched)`.
  - `_resume_one_deferred` переведён на **куки** (`via_cookie=True`, без units-гейта/ожидания полуночи).
  - `_deferred_save`: `resume_at=now()` (докрутка сразу, а не в полночь).
- **Goal 2 — `_JOB_HISTORY_TTL` 120 → 86400** (история джоб + errors_log живут сутки). recover/purge
  используют TTL как интервал — логика не сломана.
- **Goal 4 — crash-safety:** в `_jobs_db_recover` осиротевшие `status='resumed'` старше
  `_DEFERRED_STALE_HOURS=3` ч → `waiting`+`resume_at=now()` (демон подхватит по куке). Анти-цикл:
  джоба-докрутка на финале тегируется `body["_deferred_id"]` и помечает родительский остаток `done`
  (см. правку в конце `api_create_set` + `_resume_one_deferred`/`_deferred_enqueue_now`).
  `_deferred_db_init()` вызывается в начале recover (таблица до UPDATE).

### Проверка
- `py_compile` + `pyflakes` (undefined name) — чисто. Сервер `py_compile` OK, md5 local==server.
- Рестарт `digest.service` active, `/direct/automation` → 302, лог старта без traceback.
- **Goal 3 реанимация (живьём, victorylotsofads1, куки живые):**
  - ДО: `porg-lzjk6p5m`=280 кампаний (осиротело 1709 items: jobs 861bff3f550e+1b553886e7f0),
    `porg-psm5h7q6`=255 (осиротело ~946 items / 16 строк).
  - Поставил 4 остатка через `/direct/api/deferred_resume_now` (via_cookie) + демон сам подхватил
    waiting-строки. Создание идёт серийно (одно агентство).
  - ПОСЛЕ (промежуточно): `porg-psm5h7q6` 255 → 257 (+ растёт), джобы без traceback. lzjk6p5m-джобы
    в очереди за тем же агентством. Полный слив остатков идёт В ФОНЕ (часы: per-item M3-генерация).
- НЕ верифицировано: полное завершение всех ~2600 items (фоновый процесс), финальные числа смотреть
  по `_grid_list_campaigns(login)` и `direct_automation_jobs`.

### Задача 5 — дубли картинок в UAC tp6/tp7 (campaign.py)
- Симптом: в live tp7 `porg-24rg6hzy` две из 5 картинок — визуальный дубль (РАСПРОДАЖА), хотя во
  вкладке «Контент» дубли сняты.
- Корень: имена файлов = md5 содержимого (content-addressed) → байт-дублей нет, но один баннер
  пересохранён в РАЗНЫЕ файлы (разный md5, одинаковая картинка). Визуальный phash-дедуп был ТОЛЬКО
  на фронте (скрытие в UI при hamming ≤18); снятый в UI дубль оставался `enabled` в манифесте, а
  бэкенд-автоподбор `_creative_images_for_ct` дедупил лишь по пути → грузил оба → дубль в UAC.
  Подтверждено: `scherbakova/Мультибренд/tp7/ct0000` first5 имел `83e3e8…`(#1) и `b2a333ed…`(#3) с
  phash distance **0**.
- Фикс (`campaign.py`): в `collect_image_files` (единый чокпоинт загрузки UAC) добавлен 3-уровневый
  дедуп: путь → md5-содержимое → **визуальный pHash** (DCT 32×32→8×8 без DC, медиана, hamming ≤18 —
  как на фронте). Новый `_image_phash()` (Pillow+numpy, кэш по пути, fallback None без Pillow).
- Зависимости: `Pillow` поставлен в `/root/venv` + добавлен в `home/seoadvanced/requirements.txt`
  (Mutagen venv не синкает — ставить вручную при пересборке venv).
- Верификация ЖИВЬЁМ (точный чокпоинт UAC, синк+рестарт): для `porg-24rg6hzy`/scherbakova/tp7/ct0000
  `collect_image_files`: 12 кандидатов → 6 визуально-уникальных, дубль `b2a333ed…` убран, first5 = 5
  РАЗНЫХ картинок. НЕ делал живую пересборку самой tp7-кампании (не плодить лишний черновик);
  существующие дубль-черновики нужно ПЕРЕСОЗДАТЬ — новый код их не правит in-place.

### Задача 6 — пустой ФИЛЬТР у «Страницы каталога» (ListingAd) — ИСПРАВЛЕНО и задеплоено (по HAR)
- Симптом: live `porg-24rg6hzy` камп. 711638076, группа BAIC, ListingAd `1914148280725484397`
  «Страницы каталога — credit-page-01-a» — 198 страниц всех марок, фильтр пуст.
- Корень (HAR + ЖИВЬЁМ): фильтр «Страницы каталога» = **`collectionId`** (НЕ vendor — vendor для
  ListingAd даёт **5005**; прошлая гипотеза про vendor была неверной). Коллекции тянутся Grid op
  **`Listings`** (`feeds(limitOffset filter:{searchBy:$feedId})`) — двухуровневые: бренд-уровень
  (числовой id, имя «Новые автомобили BAIC …», BAIC=**25**) и модельные (`model_*`). Старый
  `_grid_feeds` отдавал listings УРЕЗАННО (`areListingsTruncated`) → код брал коллекции из пустого
  `feed_models` → для «Марки» листинг создавался БЕЗ фильтра (Grid by-shopping фильтр не копирует).
- Правки (`blueprint.py`):
  - новые `_feed_collections(login, feed_id)` (Grid op Listings, кэш), `_brand_level_collection_id`
    (марка → бренд-коллекция, id НЕ model_, max offerCount), `_feed_models_from_collections`;
  - `_build_tp1_adgroups` фаза 4: для «Марки» резолвим бренд-коллекцию (BAIC→25) → `collectionId`
    в shopping И в `listing_build_items`; фолбэк model_*; модельные — через эффективные коллекции;
    `_add_listing_ads_v501` (умеет `collectionId EQUALS_ANY`) ставит фильтр;
  - cookie/152-путь `_create_tp1_via_cookie`: та же бренд-резолюция → shopping получает `collectionId`
    (валиден для ListingAd, в отличие от vendor) → листинг-by-shopping наследует валидный фильтр.
- Путь применения: **v501 `add_listing_ad(collection_id=...)` при СОЗДАНИИ** (проверено). v501
  `ads.update` и реверс-`UpdateListingAds` фильтр НЕ меняют → чиним только на создании.
- Верификация ЖИВЬЁМ:
  - резолвер (задеплоенный код): `_feed_collections(porg-24rg6hzy,3501015)`=198, BAIC→**25**,
    Changan→7, KIA→5; `_match_collection('BAIC BJ40')`→model_100;
  - `feedListingsPreview`: без фильтра = все марки (Skoda/Faw/GAC…); `collectionId=[25]` → count=1,
    только «Новые автомобили BAIC»;
  - ДО/ПОСЛЕ на группе BAIC: было ListingAd `1914148280725484397` `feedFilter=null`; стало
    `1914180619683651680` `feedFilter = collectionId EQUALS_ANY ["25"]` (в live).
- tp5 (свой билдер, не `_build_tp1_adgroups`) тем же паттерном НЕ правил (scope = tp1). TODO для tp5.

### ИНЦИДЕНТ (не мой код): крэш-луп digest.service при рестарте
- При рестарте под задачу 6 сервис ушёл в крэш-луп: `work/blueprint.py` `_ensure_subagents._read()`
  падал на `json.loads` повреждённого `/opt/scripts/work/html_bashbort_subagent/dashboard_state.json`
  («Extra data» — лишний хвост `}\n}` от двойной записи; владелец ai-agent; 11:35). Это компонент
  Victory Direct (`work/`), не нейродиректолог; старый инстанс держал стейт в памяти, баг всплыл при
  рестарте.
- Восстановил: бэкап `…corrupt.bak` + перезапись валидной частью (`raw_decode`), chown ai-agent,
  рестарт → `digest.service active`, `/direct/automation` 302, `/login` 200. `work/_read()` стоит
  обернуть в try/except — хрупкость не моя, передать владельцу work/.

## Предыдущая сессия: 2026-06-27

### Дополнение: live-проверка token-path + feed-path 2026-06-28
- Подтверждено живьём на `porg-24rg6hzy`, что token-path больше не обязан предварительно
  грузить картинки через `v501 upload_image`: для `tp1/tp2/tp4` рабочая схема теперь
  `ads.add без image hashes -> post-create repair через Grid/куки -> adPrice -> повторный repair`.
- Причина фикса: на полном `tp1` массовый `upload_image` мог подвешивать create до стадии `ads.add`.
  После перевода картинок в post-create repair свежие кампании создаются до конца:
  `711458523` = `35 ResponsiveAd + 35 ShoppingAd + 35 ListingAd`.
- Отдельно подтверждено, что проблема `Платеж от 0 ₽/мес` шла не из payment-normalizer, а из
  `_coherent_discounts`: он брал самым частым любое рублёвое число, включая `0 ₽` первого взноса.
  Исправлено: рублёвая канонизация теперь применяется только к скидкам/выгодам/господдержке.
- Быстрый live-check после фикса:
  - `quick_tp2_ct0026_vrun08` (`711460263`) -> `1 ResponsiveAd`, live payload = `7 Titles / 3 Texts / 5 Images`;
  - `quick_tp5_ct0026_vrun03` (`711460905`) -> `1 ResponsiveAd + 1 ShoppingAd + 1 ListingAd`;
  - `tp3_cpc_site ... vrun02` (`711460446`) -> `1 ShoppingAd + 1 ListingAd`.
- Практический инвариант: если в имени кампании есть суффикс фида, проверяем через `ads.get`,
  что реально появились `SHOPPING_AD`/`LISTING_AD`, а не только feed-name в названии.

### Дополнение: рестарт сервиса и синхронизация документации
- После деплоя/repair-path сервис перезапущен повторно: `digest.service active`,
  `ActiveEnterTimestamp=Sat 2026-06-27 22:42:30 +05`.
- В проектные md-файлы синхронизированы текущие инварианты runtime:
  - cookie/Grid `tp1/tp2/tp4` обязаны проходить post-create repair через `UpdateAdaptiveTextAds`;
  - боевой порядок контента зафиксирован как `AI-first -> repair/retry -> slepok/common fallback`;
  - старые плохие draft-объявления сами не исправляются, их нужно repair-ить или пересоздавать.

### Дополнение: live-repair cookie ResponsiveAd после недозаполнения tp1/tp2
- Диагноз по `porg-psm5h7q6`, campaign `711420388` (`tp1_cpc_site — РСЯ - Марки - КС ...`):
  через `v501 ads.get + ResponsiveAdFieldNames` подтверждено, что в живом черновике лежали
  только `2 Titles`, `3 Texts`, `1 AdImage`. Это был не баг UI и не чтение через `TextAdFieldNames`.
- Причина: cookie/Grid path мог создать draft с урезанным `ResponsiveAd`, а post-step после
  `create_full` ремонтировал только картинки и только когда `image_hashes` были полностью пустыми.
  Если в аккаунте уже совпадал `1` hash, добивка остальных картинок не выполнялась вовсе.
- Исправлено:
  - `blueprint.py`: добавлен `_grid_update_adaptive_ads(...)` — post-update через
    `UpdateAdaptiveTextAds` для полного payload (`titles`, `bodies`, `image_hashes?`, `adPrice?`);
  - `_create_tp1_via_cookie(...)` теперь после `create_full` всегда делает repair пачки объявлений:
    повторно пишет ожидаемые `titles/texts`, и для картинок добирает hash-и до `5`, даже если
    уже был найден `1` reuse-hash;
  - `_create_text_via_cookie(...)` (tp2/tp4) переведён на тот же repair-path и тоже получил
    `image_map` на вход `_tp1_pack_groups(...)`.
- Live verification:
  - `digest.service` перезапущен в `2026-06-27 22:07:33 +05`, сервис `active`;
  - точечный repair живого ad `1914028586432473926` в campaign `711420388` прошёл через
    `_grid_update_adaptive_ads(...)`: было `2 Titles / 1 Image`, стало `7 Titles / 3 Images`;
  - отдельно проверено, что `GridClient.upload_image(...)` на `BAIC`-manual картинках возвращает
    новые hash-и и они принимаются в `ResponsiveAd.AdImages.Items`.
- Практический вывод:
  - для cookie/create path `tp1/tp2/tp4` больше нельзя полагаться только на первичный `AddAdaptiveTextAds`;
    обязательна post-create repair-фаза по фактическим ad-id;
  - уже созданные плохие черновики не перепишутся сами — их нужно либо repair-ить, либо пересоздавать.

### Дополнение: Direct-релевантность автокредита и запрет смешения new/БУ
- Hotfix по двум живым формулировкам:
  - `Кредит на BAIC BJ40. Кассовый взрыв до конца месяца` теперь режется стоп-листом
    (`кассовый взрыв` в заголовках/текстах запрещён);
  - `Без первоначального взноса. Кредит на BAIC BJ40` и `Без первого взноса. Кредит на BAIC BJ40`
    в `_fix_grammar()` переставляются в канон:
    `Кредит на BAIC BJ40. Без первоначального взноса` / `Кредит на BAIC BJ40. Без первого взноса`.
- Targeted-проверка прошла, повторная матрица `M3 empty`: `64/64 OK`.

- Hotfix по порядку денежных/процентных знаков:
  - в `ai_agents._clean_line()` добавлена нормализация `₽9000 -> 9000₽` и `%45 -> 45%`;
  - правило применяется ко всем строкам: заголовки, тексты, быстрые ссылки.
- Проверено targeted-примерами:
  - `Быстрое одобрение авто от ₽9000.` -> `Быстрое одобрение авто от 9000₽.`;
  - `Выгода до %45 на новые авто.` -> `Выгода до 45% на новые авто.`.
- Повторная матрица `M3 empty`: `64/64 OK`, префиксных `₽<число>` и `%<число>` не найдено.

- Hotfix по live-скрину "стало хуже":
  - `_accept_text` теперь вызывает `_bad_ad_text` на ранней приёмке M3, поэтому плохой live-ответ
    не может попасть в `good_x` до финальной добивки;
  - добавлены стопы: `Кредит до N%`, `кредит на новые авто до N%`, `Взнос отсутствует`,
    `Безопасные сделки`, `Срочно продаём`, `Позвоните за скидкой`;
  - для `ct0000`/общих кампаний финальный гард теперь режет любые конкретные чужие марки/модели
    через `_SITELINK_VEHICLE_RE` (`Peugeot`, `Chery`, `BAIC`, `CS75` и т.п.).
- Targeted-тест со строками со скрина:
  - `Кредит до 45%. Взнос отсутствует. Каско в подарок` отброшен;
  - `Кредит на новые авто до 15%. Безопасные сделки` отброшен;
  - `Срочно продаём новые Peugeot!... Позвоните за скидкой!` отброшен;
  - итоговый комплект добит до `5/3`.
- Повторная матрица `M3 empty`: `64/64 OK`.

- Уточнение правила после замечания пользователя: `Мульти + БУ` должен считаться как сайт
  про новые авто, а не как БУ. БУ-режим остаётся только для явного типа `С пробегом`.
- Исправлено:
  - `Мульти + БУ` перенесён в `NEW_ONLY_SITE_TYPES`;
  - `BU_SITE_TYPES` теперь только `{"С пробегом"}`;
  - legacy-helper `_is_bu_site(...)`, который используется в чистке ключей/ссылок при создании
    кампаний, теперь тоже считает БУ только `С пробегом`.
- Проверено локально в worst-case режиме `M3 empty`, без создания кампаний:
  `4` комбинации слепок/тип сайта × `4` ct × `4` tp = `64/64 OK`.
  Контрольные примеры:
  - `Мульти + БУ / ct0000 / tp2` отдаёт `Новое авто / Новые авто`, без `с пробегом`;
  - `Мульти + БУ / ct0033 / tp7` отдаёт `Changan ... в кредит`, без `с пробегом`;
  - `С пробегом / ct0033 / tp7` по-прежнему отдаёт `Changan ... с пробегом`.

### Дополнение: Direct-релевантность автокредита и первичный запрет смешения new/БУ
- По замечанию пользователя проверена проблема, где агрегированный список "лучших" заголовков
  смешал новые авто и авто с пробегом. Вывод: тот список был агрегирован по `ct` из разных
  типов сайта, но в боевой генерации нужно иметь hard-guard на уровне одного объявления.
- Исправлено:
  - добавлен единый `BU_SITE_TYPES` / `is_bu_site_type(...)` в `ai_agents.py`;
  - `Мульти + БУ` больше не получает смешанный банк `both`, а фильтруется как БУ-режим;
  - все проверки M3/корпуса/fallback теперь одинаково считают `С пробегом` и `Мульти + БУ`
    БУ-режимом, поэтому в одном объявлении не смешиваются `новые авто` и `с пробегом`;
  - финальный гард блокирует чужие города из базы/списка РФ, платежи меньше `5 000 ₽/мес`,
    `без переплат`, `трейд-ин до 150%+`, `Волгоград`/чужой город из корпуса;
  - заголовки и тексты теперь должны явно содержать кредитный маркер: `кредит`,
    `автокредит`, `платеж`, `/мес`, `взнос`, `одобрение`, `банк`, `рассрочка`;
  - убрано разрешение на "распродажу без кредита" в retry-промпте и M3-промптах;
  - для длинных моделей в текстах используется короткое имя бренда, если полная модель
    не помещается в лимит `81` символ (`Changan CS75` -> `Changan` в текстах).
- Ориентир по правилам Директа сверялся с официальными материалами Яндекса:
  заголовок должен сразу показывать релевантный оффер, текст раскрывает товар/услугу и CTA,
  элементы объявления должны соответствовать посадочной/офферу, быстрые ссылки не должны
  дублироваться.
- Проверено локально в worst-case режиме `M3 empty`, без создания кампаний:
  `5` комбинаций слепок/тип сайта × `4` ct × `4` tp = `80/80 OK`.
  Проверки:
  - `tp1/tp2 = 7 заголовков / 3 текста / 8 быстрых ссылок`;
  - `tp6/tp7 = 5 заголовков / 3 текста / 8 быстрых ссылок`;
  - каждый заголовок и каждый текст содержит кредитный маркер;
  - новые типы сайта не содержат `с пробегом/б/у`;
  - БУ-типы сайта не содержат `новые авто`;
  - нет смешения new+БУ внутри одного объявления;
  - нет `Волгоград`, `2 208 ₽/мес`, `без переплат`, `трейд-ин 150%+`.
- Локально `python3.12 -m py_compile home/seoadvanced/direct/ai_agents.py home/seoadvanced/direct/blueprint.py` OK.

### Дополнение: 5 повторных quality-прогонов
- По просьбе пользователя повторно прогнана та же матрица `5` раз на LXC101 в режиме `M3 empty`:
  `5 × 36 = 180` кейсов.
- Каждый прогон проверял:
  - полноту `tp2=7/3/8`, `tp6/tp7=5/3/8`;
  - diversity заголовков по UTP-bucket и первым словам;
  - нормализованные дубли;
  - согласованность процентов и платежей;
  - русские/кривые формулировки (`Новый авто`, `Первый взнос 0%`, `Кредит до 45%`,
    `условия кредитования до N ₽`, `резина/шины на 1 сезон`, `Ваш новый автомобиль ждёт`).
- Результат:
  - RUN 1: fullness `36/36`, quality `36/36`;
  - RUN 2: fullness `36/36`, quality `36/36`;
  - RUN 3: fullness `36/36`, quality `36/36`;
  - RUN 4: fullness `36/36`, quality `36/36`;
  - RUN 5: fullness `36/36`, quality `36/36`;
  - суммарно: fullness errors `0`, quality errors `0`.
- Отдельно собраны лучшие варианты по трём `ct`: `ct0000`, `ct0019 BAIC`,
  `ct0033 Changan CS75`.

### Дополнение: quality-прогон разнообразия и русских формулировок
- Повторно прогнана матрица LXC101 в worst-case режиме `M3 empty`:
  `4` слепка × `1` релевантный тип сайта × `3` ct × `3` tp = `36` кейсов.
- Добавлены проверки качества сверх полноты:
  - количество `tp2=7/3/8`, `tp6/tp7=5/3/8`;
  - разнообразие title UTP-bucket: `payment`, `discount`, `tradein`, `gift`, `support`,
    `sale/availability`, `credit`;
  - не более `2` одинаковых первых слов в наборе заголовков;
  - отсутствие нормализованных дублей заголовков;
  - согласованность платежей и процентов внутри набора;
  - стопы русских/кривых формулировок: `Кредит до 45%`, `условия кредитования до N ₽`,
    `резина/шины на 1 сезон`, `Первый взнос 0%`, `Новый авто`,
    `Ваш новый автомобиль ждёт`.
- Исправлено:
  - `_title_utp_bucket` больше не схлопывает всё кредитное в один bucket: отдельно учитываются
    `payment`, `downpay`, `gift`, `tradein`, `support`, `sale`, `discount`;
  - финальный отбор заголовков стал трёхступенчатым: сначала разные смысловые bucket и первые
    слова, затем добор с лимитом первых слов, только потом полный relax;
  - fallback-заголовки переписаны под разные первые слова и УТП (`Платеж`, `Выгода`,
    `Трейд-ин`, `КАСКО`, `Решение банка`, бренд/модель);
  - `Новый авто` заменено на `Новое авто`;
  - тексты с `Первый взнос 0%` и `Ваш новый автомобиль ждёт...` теперь бракуются.
- Финальный результат:
  - полнота: `36/36 OK`;
  - quality-check: `36/36 OK`;
  - bad titles/texts/sitelinks: `0`;
  - русские стоп-фразы: `0`;
  - local и LXC101 MD5 совпадают:
    `blueprint.py` `bfa6d91a7152b4620b3312ca59823e2b`,
    `ai_agents.py` `13a752b3d55c5d4a461b6e2f96d56a1e`,
    `templates/direct/index.html` `84beb5eab64953ea1c6a8c09ee848cb9`;
  - local/server `py_compile blueprint.py ai_agents.py` OK;
  - активных jobs за последние 12 часов не было;
  - `digest.service` перезапущен, сервис `active`, `/direct/automation` отвечает `302`,
    свежий лог запуска без traceback.

### Дополнение: матричный прогон слепков и фиксы добивки
- По запросу прогнана матрица на LXC101 в worst-case режиме, когда M3 возвращает пусто:
  `4` слепка × `1` релевантный тип сайта на слепок × `3` ct × `3` tp = `36` кейсов.
  Слепки/типы: `pavlov/Монобренд`, `kryuchkova/Мультибренд`,
  `scherbakova/Квиз`, `terehov/С пробегом`. ct: `ct0000`, `ct0019 BAIC`,
  `ct0033 Changan CS75`. tp: `tp2`, `tp6`, `tp7`.
- Первый прогон нашёл реальные дефекты:
  - брендовые fallback-и для короткой марки `BAIC` давали только `2` заголовка и `0` текстов,
    потому что часть строк была короче порогов `45/68` или попадала под старые стопы;
  - `tp2` иногда оставался на `6` заголовках вместо `7`;
  - `Квиз` и `С пробегом` после semantic/bucket-dedup быстрых ссылок могли оставаться на `7`
    ссылках вместо `8`.
- Исправлено:
  - расширены брендовые fallback-заголовки и тексты: строки теперь `45+`/`68+`, с цифрами,
    без `0%`, с запасом для коротких брендов вроде `BAIC`;
  - добавлены дополнительные общие `tp2` fallback-заголовки для добивки до `7`;
  - для `Квиз`, `С пробегом`, `Мульти + БУ` увеличен лимит `other`-bucket быстрых ссылок до `2`;
  - нейтральная ссылка `Консультация онлайн` переписана без слова `условия`, чтобы не попадать
    в переполненный `credit` bucket.
- Финальный результат матрицы на LXC101:
  - `36/36 OK`;
  - `tp2` везде `7` заголовков, `3` текста, `8` быстрых ссылок;
  - `tp6/tp7` везде `5` заголовков, `3` текста, `8` быстрых ссылок;
  - плохих заголовков/текстов/ссылок по текущим фильтрам: `0`.
- Деплой:
  - local и LXC101 MD5 совпадают:
    `blueprint.py` `5ef603a329dfb671185f2d9eddba1a95`,
    `ai_agents.py` `1d004ef6bfac94148c5a997521cf5fcc`,
    `templates/direct/index.html` `84beb5eab64953ea1c6a8c09ee848cb9`;
  - local/server `py_compile blueprint.py ai_agents.py` OK;
  - активных jobs за последние 12 часов не было;
  - `digest.service` перезапущен, сервис `active`, `/direct/automation` отвечает `302`,
    свежий лог запуска без traceback.

### Дополнение: обязательный repair-loop и полное заполнение контента
- Причина: после фильтрации M3/слепка система делала только ограниченный retry и fallback-добивку.
  Если фильтры выкидывали быстрые ссылки/заголовки, UI мог получить пустые поля. По требованию:
  объявления должны быть полностью заполнены.
- Исправлено:
  - `blueprint.py`: добавлен ограниченный `repair-loop` до `3` раундов по недостающим секциям
    (`titles/texts/sitelinks`). После каждого раунда новые варианты снова проходят те же фильтры.
    Если M3 недоступна или продолжает отдавать мусор, остаток закрывает deterministic fallback.
  - Для repair-вызовов введён короткий таймаут `M3_LLM_REPAIR_TIMEOUT=35`, чтобы обязательный
    добор не подвешивал UI на многоминутные 72B-таймауты.
  - Запрещены формулировки `резина/шины ... на 1 сезон` в заголовках и быстрых ссылках;
    fallback `Резина в подарок на 1 сезон` заменён на кредитные УТП.
  - Общий `ct0000` дополнительно фильтрует чужие марки/города; в стоп-лист быстрых ссылок добавлены
    `Peugeot/Пежо`, `Citroen/Ситроен`, `Skoda/Шкода`, `Volkswagen/Фольксваген`.
  - Глобальный fallback-банк быстрых ссылок доведён до `8` валидных ссылок после semantic-dedup:
    все описания `45+` символов и разные УТП-bucket.
- Проверено локально и на LXC101 в худшем сценарии, когда M3 возвращает пусто:
  - `tp6`: `5` заголовков, `3` текста, `8` быстрых ссылок, плохих элементов `0`;
  - `tp7`: `5` заголовков, `3` текста, `8` быстрых ссылок, плохих элементов `0`;
  - `tp2`: `7` заголовков, `3` текста, `8` быстрых ссылок, плохих элементов `0`;
  - `repair_rounds=3` фиксируется в `m3_debug.retry_used`.
- Деплой:
  - local и LXC101 MD5 совпадают:
    `blueprint.py` `a114da8a094bcf24d11c71d3d8b34bbe`,
    `ai_agents.py` `9ac8d18c19c44250d41d1c9d439171c1`,
    `templates/direct/index.html` `84beb5eab64953ea1c6a8c09ee848cb9`;
  - local/server `py_compile blueprint.py ai_agents.py` OK;
  - активных jobs за последние 12 часов не было;
  - `digest.service` перезапущен, сервис `active`, `/direct/automation` отвечает `302`,
    свежий лог запуска без traceback.

### Дополнение: правки качества контента в «Обучении ИИ» после live-проверки
- Уточнено правило количества: `tp6/tp7` всегда остаются на `5` заголовках; `tp1-tp5`
  для комбинированных/текстовых объявлений должны добиваться до `7`.
- Исправлено:
  - `templates/direct/index.html`: смена `ct/st` в «Обучении ИИ» теперь заменяет первый
    4-значный `ct`, а не добавляет новый; лишние 4-значные `ct` из поля кода удаляются.
    Это закрывает мусорный кодер вида `ct0056_ct0033_ct0000_...`.
  - `blueprint.py`: серверный парсер не вытаскивает бренд из кодера с несколькими 4-значными
    `ct`; явный общий/небрендовый `ct` (`ct0000`, кластеры/общие) даёт общий контент без марки.
  - Заголовки вида `Условия кредитования до 925 000 ₽`, `Кредит до 45% скидки` и
    `Кредит и шины на 1 сезон` теперь бракуются; fallback заменён на `Резина в подарок на 1 сезон`.
  - `ai_agents.py`: быстрые ссылки `Кредит от 9 000 ₽/мес` и `Платеж от 9 000 ₽/мес`
    считаются одним смысловым УТП (`credit_pay|9000`), поэтому вместе не проходят.
- Проверено:
  - local и LXC101 MD5 совпадают:
    `blueprint.py` `dc443efc883bc88fedf556e44059ace8`,
    `ai_agents.py` `d72d0f07cbda79aec6ada7865cc0ece6`,
    `templates/direct/index.html` `84beb5eab64953ea1c6a8c09ee848cb9`;
  - local/server `py_compile blueprint.py ai_agents.py` OK;
  - серверный sanity: плохие фразы выше бракуются, `Резина в подарок на 1 сезон` проходит,
    malformed common-кодер даёт `('', 'ct0001')`, быстрые ссылки кредит/платёж имеют один ключ
    `credit_pay|9000`;
  - активных jobs за последние 12 часов не было;
  - `digest.service` перезапущен, сервис `active`, `/direct/automation` отвечает `302`,
    свежий лог запуска без traceback.

### Дополнение: fallback-добивка контента после правила «цифра в каждом заголовке»
- Причина бага: после правила «в каждом заголовке нужна цифра» старая fallback-добивка сама себя
  вычищала. Для `С пробегом` / `tp6` фактически проходил 1 заголовок и 1 текст, остальное фронт
  показывал пустыми полями.
- Исправлено:
  - обновлена fallback-добивка для `С пробегом`;
  - обновлена fallback-добивка для новых, квизовых и мультибрендовых сайтов;
  - fallback-тексты доведены до порога длины `68+`;
  - добавлена финальная добивка быстрых ссылок до `8`, раньше её не было.
- Проверено матрицей на LXC101 в худшем сценарии, когда M3 возвращает пусто:
  `4` слепка × `5` типов сайта × `tp6/tp2` = `40/40 OK`.
- Результат проверки:
  - `tp6`: `5` заголовков, `3` текста, `8` быстрых ссылок;
  - `tp2`: `7` заголовков, `3` текста, `8` быстрых ссылок;
  - плохих заголовков/текстов/ссылок по текущим фильтрам: `0`.
- Live:
  - активных jobs не было;
  - `digest.service` перезапущен и `active`;
  - `/direct/automation` отвечает `302`;
  - свежий лог без traceback.

### БАГ-FIX: M3-заголовки в обучении ИИ и боевых кампаниях
- Разделено количество заголовков:
  - `tp6/tp7` (UAC/МК/Товарка) в боевых кампаниях остаются на 5 заголовках;
  - комбинированные/текстовые кампании (`tp1-tp5`, в т.ч. `tp2` на экране «Обучение ИИ») добиваются до 7.
- `templates/direct/index.html`: редактор контента в «Кампании в наборе» теперь показывает 5 полей
  только для `tp6/tp7`, для остальных типов — 7. Экран «Обучение ИИ» уже оставлен по правилу
  `tp6/tp7=5`, прочие `tp=7`.
- `blueprint.py`:
  - исправлен вывод, что `tp6/tp7` надо переводить на 7: это неверно; условие `tp6/tp7 → 5`
    сохранено в `_gen_campaign_content`;
  - добавлен серверный гард `_bad_credit_payment_range`: если строка содержит
    `кредит/платеж от N ₽/руб/мес`, `N` обязан быть в диапазоне `9 000–15 000`; крупные суммы
    вроде `Кредит от 925 000 ₽` отбрасываются, но `Кредит от 15 банков` не считается платежом;
  - M3-заголовки без цифр больше не принимаются (`missing_number` в диагностике фильтра), чтобы
    не проходили общие УТП без акцента;
  - финальная добивка заголовков усилена конкретными числовыми УТП: `0 ₽`, `30 минут`,
    `9 000 ₽/мес`, `15 банков`, `45%`, `1 день`.
- `ai_agents.py`: промпты для полного и fan-out режима уточнены:
  - платеж в кредитном оффере только `9 000–15 000 ₽/мес`;
  - запрещены сотни тысяч после `кредит от`;
  - в каждом заголовке нужна цифра.
- Проверено:
  - local/server MD5 совпадают: `blueprint.py` `783e6fc9f27d09047a74bdce84e46b24`,
    `ai_agents.py` `67f2c23e137a922125589d4e50daadd4`, `templates/direct/index.html`
    `9d42c348cb62ba7a9081763127ef2885`;
  - local/server `py_compile blueprint.py ai_agents.py` OK;
  - server sanity: `Кредит от 925 000 ₽` и `Платеж от 16 000 ₽/мес` бракуются,
    `Кредит от 9 000 ₽/мес`, `Платеж от 15 000 ₽/мес`, `Кредит от 15 банков` проходят;
  - активных `direct_automation_jobs` за последние 12 часов не было, выполнен `systemctl restart digest.service`;
  - после рестарта `digest.service` active, `/direct/automation` без авторизации отвечает `302`,
    свежий лог запуска без traceback.

### UI/аудит дублей tp6/tp7 в слепках
- Проверены `tp6/tp7` в `slepki_structure.json` по строгой сигнатуре фактических настроек
  (`slepok × site_type × tp × sq × mode × ct × keywords/minus|audience_ids|autotarget_categories`).
  Найдено 44 группы, где в рамках одного слепка разные названия ведут к одинаковым настройкам.
- Важное: часть дублей с `mode=keywords` имеет `pos=0/minus=0`; после предыдущего фикса такие КС-кампании
  не создаются молча, а падают с явной ошибкой `tp6/tp7 КС без ключей`.
- `templates/direct/index.html`: в дереве «Структура слепков» и в списке «Кампании в наборе» добавлен
  показ понятных подписей для пустых `item.t` в `tp6/tp7`:
  `Интересы` для аудиторных групп, иначе `Автотаргетинг`; для строк с КС — `Ключевики`.
  При этом `data-grp` для `tp6/tp7` оставлен равным исходной группе слепка, чтобы фильтр выбранных
  позиций на сервере не сломался.
- Проверено:
  - локальный и LXC101 `blueprint.py` совпадают по MD5 `303183fbb3732177864ff8e380c3147f`;
  - локальный и LXC101 `templates/direct/index.html` совпадают по MD5 `fae76ec32b87f0d975ab51d6a672c98c`;
  - маркеры предыдущего `tp6/tp7` фикса (`targeting_mode`, `keywords=it_keywords`,
    `audiences=it_audiences`, `_TP67_RELEVANCE_CATEGORIES`) на месте;
  - local/server `py_compile direct/blueprint.py` OK;
  - активных `direct_automation_jobs` за последние 12 часов не было, выполнен `systemctl restart digest.service`;
  - после рестарта `digest.service` active, `/direct/automation` без авторизации отвечает `302`, свежий лог без traceback.

### TP6/TP7 partial live reference from UAC cookies
- Собран частичный live-reference из UAC `/campaign/{id}`:
  `work/tp67_full_cookie_payload.json` содержит 361 логин, 313 ok, 48 failed, 2851 payload rows.
- `tp67_real_keywords.json` пересобран как совместимая fallback-библиотека для `tp6/tp7 КС`:
  252 keyword items, статус partial; старый файл сохранён в
  `work/tp67_real_keywords.before_partial_build.json`.
- Runtime fallback в `blueprint.py` теперь ищет реальный набор ключей не только по точному слепку,
  но и по той же позиции/ct среди доступных live UAC payload, если M3-пак текущего слепка пустой.
- План `tp6/tp7` подавляет точные дубли payload-signature до создания кампаний.
- Основной UAC cookie-подбор в `campaign.py` расширен до 6 агентств:
  `victoryagency-direct1618440`, `victorylotsofads1`, `victoryagency14`,
  `y-direct-victory`, `victoryagencydirect`, `useful-call-agency`.
- Проверено правило: если OAuth-токен агентства открывает логин, UAC-cookie нужно брать этого же
  агентства; для 177 ранее failed логинов OAuth-агентство найдено, 129 удалось добрать через UAC.
- Оставшиеся 48 логинов — не `need_reset`: OAuth-доступ есть, но web/UAC cookie-доступ возвращает
  `403/no rights` across available cookies. Детали:
  `work/tp67_failed_agency_resolution.json`, `work/tp67_extra_cookie_probe.json`,
  `work/tp67_partial_reference_audit.json`.

### БАГ-FIX: tp6/tp7 настройки строго по слепку
- По live/Grid-проверке реальных аккаунтов директологов подтверждено: в `tp6/tp7` есть разные
  режимы кампаний с названиями `КС/Ключевики`, `Автотаргетинг`, `Интересы/аудитории`; прежний код
  создавал их упрощенно: ключи не передавались, аудитории слепка вычислялись, но заменялись
  глобальным `audiences_preset`.
- `blueprint.py`:
  - план `tp6/tp7` теперь сохраняет `targeting_mode`: `keywords`, `autotarget`, `audience`;
  - название `Ключевики` больше не подменяется на `Интересы`, поэтому предпросмотр и создание
    не маскируют ручной режим;
  - `КС/Ключевики` при создании получают ключи и минус-слова из M3-пака текущего слепка
    (`slepok × site_type × tp × ct`); если ключей нет, кампания не создается с явной ошибкой;
  - `Интересы/аудитории` получают id только из `public.direct_slepok_audiences` текущего
    `slepok × site_type × tp`; глобальный `audiences_preset.json` используется только как справочник
    метаданных id для UAC object-shape;
  - если для audience-кампании нет аудиторий в рамках этого же слепка, кампания не создается с
    явной ошибкой;
  - `Автотаргетинг` создается без keywords/audiences, но с профилем категорий как в live Grid:
    `EXACT_V2_MARK`, `ACCESSORY_MARK`, `BROADER_MARK`, `ALTERNATIVE_MARK`, `NARROW_MARK`;
  - старые/не найденные схемы `MK_RA/TK_RA` не являются источником настроек и не поддерживаются
    в новом генераторе;
  - правило кодера `_aon_` для `tp6/tp7` сохранено.
- Важное ограничение: все fallback-и по аудиториям остаются внутри текущего
  `slepok × site_type × tp`; кросс-слепковый мерж не используется.
- Деплой:
  - локальный и LXC101 `blueprint.py` совпадают по MD5 `56f1ee380e0d0af5bf4c0b999cc22c93`;
  - local/server `py_compile` OK, server helper sanity OK;
  - выполнен `systemctl restart digest.service`;
  - после рестарта `digest.service` active, `/direct/automation` отвечает `302` на логин, свежий лог
    запуска без traceback.

## Последняя сессия: 2026-06-25 (день, 6-я сессия)

### Документация и проверка DB после сообщения `could not write init file`
- Обновлены MD-файлы под live-состояние:
  - `README.md`: глобальные правила фидов, API `/direct/api/feed-rules`, хранение
    `public.direct_global_feed_rules`, фильтрация фидов при создании, правила картинок по `ct`,
    фильтр generic быстрых ссылок.
  - `CODER.md`: `tp1` без автотаргетинга теперь документирован как `aoff`, `tp2/tp4` остаются `aon`;
    фидовые кампании мультиплицируются только по разрешённым глобальными правилами фидам;
    добавлено правило картинок `ct0000-ct0014` → общий пул `ct0000`, `ct0015-ct0018` → своя папка.
- Пользователь показал ошибку job:
  `connection to server at "103.88.240.90", port 5432 failed: FATAL: could not write init file`.
  Проверка с LXC101:
  - `df -h` на LXC101 нормальный (`/` 43%, `/dev/shm` свободен);
  - `pg_isready -h 103.88.240.90 -p 5432` → `accepting connections`;
  - полноценный `_victory_conn(); select 1` из `/opt/scripts` → `(1,)`.
  Вывод: это был кратковременный/инфраструктурный сбой PostgreSQL на стороне Victory DB, не ошибка
  конкретного аккаунта или генерации; на момент проверки подключение восстановилось.

### M3-проверка заголовков и картинок
- По просьбе пользователя прогнана генерация через M3 без создания черновиков.
- Найдены и исправлены проблемы:
  - M3 продолжал писать `без документов`; добавлен жёсткий серверный запрет в `_bad_ad_title`
    и ранний фильтр `_gen_campaign_content`.
  - Заголовки с дефисом-разделителем (`BAIC - ...`) и `-45%` теперь считаются плохими.
  - `_creative_images_for_ct(..., ct0014, ...)` правильно мапит общую группу в `ct0000`, но мог
    вернуть 0 картинок; добавлен fallback `Manual/ct0000` → `kp.feed_images_for_segment(5)`.
  - `ai_agents.assemble_campaign` теперь чистит уже принятые M3-строки через `_clean_line` перед
    финальным отбором, чтобы не проходили тексты >81 и дефисы-разделители.
  - После M3-фильтра добавлена безопасная брендовая/общая добивка до 5 заголовков и 3 текстов.
- Контрольный прогон на `porg-psm5h7q6`, `scherbakova`, `ct0019 BAIC`:
  - 5 заголовков, 3 текста;
  - `bad_titles=[]`, `missing_BAIC=[]`;
  - нет заголовков >56, текстов >81, коротких заголовков <45 и коротких текстов <68;
  - длины текстов: 68, 69, 73;
  - общие картинки `ct0014 → ct0000`: 5 изображений; брендовые `ct0019`: 3 изображения.
- Файлы `blueprint.py` и `ai_agents.py` скопированы на LXC101, но `digest.service` не рестартовался
  в этой проверке, чтобы не прерывать активную очередь создания. Для live UI нужен рестарт.

### Финал после рестарта: правки активны в live
- Пользователь запросил рестарт; выполнен `systemctl restart digest.service`.
- Проверки после рестарта:
  - `digest.service` active, новый PID `366020`;
  - `GET http://127.0.0.1:5010/login` → `200`;
  - server sanity:
    - `_image_ct_for_content("ct0014")` → `ct0000`;
    - `_tp1_group_name(..., autotarget=False)` → `ct0014_aoff_...`;
    - `_bad_ad_sitelink("Запишитесь на тест-драйв", ...)` → `True`;
    - глобальных правил фидов в БД: `14`.
- Важно: активные на момент рестарта in-memory очереди были прерваны, но уже созданные черновики
  старым кодом не ремонтировались по прежнему правилу пользователя.

### Дополнение: последние live-правки перед рестартом
- `blueprint.py`:
  - `tp5` cookie/fallback-путь (`_create_shopping_via_cookie`) теперь дописывает название фида
    в имя кампании (`— feed_name`), включая случай, когда `feed_name` определяется по `feed_id`
    через Grid. Это закрывает баг: товарное объявление использует `credit-page-01-a`, а кампания
    называлась без фида.
  - `tp1` group name теперь отражает фактический автотаргетинг: `autotarget=False` → `aoff`,
    `autotarget=True` → `aon`. Ранее `tp1 - КС` мог называться `aon` при выключенном автотаргетинге.
    Для `tp2/tp4` сохранено ранее согласованное правило `_text_group_name(...)` → всегда `aon`.
  - Картинки по `ct`:
    - общие/аудиторные `ct0000, ct0001, ct0002, ct0003, ct0004, ct0005, ct0006, ct0007,
      ct0008, ct0009, ct0010, ct0013, ct0014` берут общий пул картинок `ct0000`;
    - кузова `ct0015, ct0016, ct0017, ct0018` берут картинки строго из своей папки `ct`;
    - модельные/марочные `ct` берут свои model/brand картинки.
  - Общие заголовки:
    - убраны голые хвосты `Со скидкой`, `Скидки месяца`, `Акция`;
    - запрещено `госпрограмма/госпрограммы ... в подарок`;
    - общие группы добиваются до 7 заголовков после дедупликации.
  - Быстрые ссылки: generic `Запишитесь на тест-драйв` теперь считается плохой ссылкой;
    фолбэк заменён на более предметный `Тест-драйв 2025`.

### Дополнение: глобальные правила фидов и retry tp1 ResponsiveAd
- Согласовано: правила фидов глобальные для сервиса, не привязаны к аккаунту.
- `blueprint.py`:
  - добавлен дефолтный глобальный allow-list фидов:
    `credit-page-01-a.xml`, `dostup-k-rasprodazhe-01-a.xml`, `dostup-k-rasprodazhe-01-b.xml`,
    `dostup-k-rasprodazhe-live-01-b.xml`, `dostup-k-rasprodazhe-live-01-c.xml`,
    `yandex-catalog-model-color.xml`, `yandex-catalog-model-design-custom-name.xml`,
    `yandex-catalog-new.xml`, `yandex.xml`, `yandex_auto_ext_preview.xml`,
    `yandex_auto_ext_preview_benefit.xml`, `yandex_auto_preview.xml`,
    `zabronirovat-01-a.xml`, `zabronirovat-01-b.xml`.
  - новый API `/direct/api/feed-rules` GET/POST хранит правила в
    `public.direct_global_feed_rules`.
  - выбор фидов при создании (`_first_url_feed`, `_catalog_feed`, `_account_model_feeds`,
    `_tp5_account_data`, cookie fallback tp3/tp5) теперь фильтруется по глобальному allow-list.
    Если фид аккаунта не совпал с выбранным XML, случайный другой фид не берётся.
  - `_v5_err` теперь показывает `error_detail`, если Яндекс его вернул.
  - `tp1 ResponsiveAd`: при top-level `Некорректный запрос` добавлен retry batch без
    `SitelinkSetId`, затем без `SitelinkSetId` и `AdImageHashes`, чтобы не удалять всю tp1
    из-за неподдержанного опционального поля.
- `templates/direct/index.html`:
  - во вкладку `Глобальные правила` добавлен режим `Фиды`;
  - отображаются чекбоксы, название, URL и статус `не проверен`;
  - кнопка `Сохранить` сохраняет выбранные фиды через `/direct/api/feed-rules`.
- Диагноз job `porg-ozge4ntu` `85a62a930939`:
  - статус на момент проверки: `running`, `done=1/32`, `created=0`, `failed=2`;
  - ошибки: две tp1 не дозаполнены на `ads.add(tp1 ResponsiveAd) Некорректный запрос`
    после создания групп/ключей/загрузки картинок;
  - это не ошибка фидов, а отказ Яндекса на payload `ResponsiveAd`.

### Дополнение: правки генерации новых кампаний после согласования ТЗ
- Старые уже созданные черновики не ремонтировались по требованию пользователя.
- `blueprint.py`:
  - `tp1` РСЯ: для групп сегментов `Марки`/`Модели` первый заголовок теперь принудительно ставится
    с собственной маркой/моделью после всех фильтров и дедупликации.
  - `tp1` РСЯ: короткие тексты добиваются вторым/третьим УТП в лимите 81 символа; контрольный пример
    на сервере стал 77 символов без обрезанного хвоста.
  - Уточнения: добавлена общая нормализация `ОСАГО` → `КАСКО` для callouts из формы и из M3-пака
    перед созданием ассетов.
  - Промо: `_promo_validate` теперь выкидывает технические числа в описании промо вроде `11212`
    и заменяет описание на дефолт стиля.
  - `tp6/tp7`: для Haval добавлен явный warning `video_missing: Haval`, если не найдено ни модельное,
    ни аккаунтное видео.
- Деплой: `blueprint.py`, `grid_create.py`, `campaign.py` скопированы на LXC101 в
  `/opt/scripts/home/seoadvanced/direct/`.
- Проверки:
  - local/server `py_compile` OK.
  - server sanity: `ОСАГО на год бесплатно` → `КАСКО на год бесплатно`; первый `_rsya_titles("BAIC", ...)`
    содержит `BAIC`; короткий `_rsya_texts(...)` добит до 77 символов.
- Важно: `digest.service` запущен как `/root/venv/bin/python3 app.py`, `use_reloader=False`,
  `ExecReload` в systemd нет. До рестарта live-процесс не подхватывает Python/HTML-правки.
  Рестарт выполнен в финале этой сессии, см. блок выше.

### Что сделано
- **Диагноз очереди `porg-ozge4ntu` job `79f5c5b4cb7f`**
  - Очередь завершилась `31/32`, `failed=1`.
  - Не создана одна РК:
    `tp6_cpc_site_ct0000_aon_n000_r0117_ct001_ag001_g00 — Мастер кампаний - Конкуренты Интересы - Ставропольский край`.
  - Причина: временный транспортный обрыв UAC API Яндекса:
    `Connection aborted / RemoteDisconnected('Remote end closed connection without response')`.
- **БАГ-FIX: UAC transport retry**
  - `campaign.py` `UacClient._request(...)`, `upload_image_file(...)`, `upload_video_file(...)`
    теперь делают до 3 попыток на `ConnectionError/Timeout`.
  - Multipart-файлы открываются заново на каждой попытке, чтобы retry не отправлял уже прочитанный file handle.
- **БАГ-FIX: tp7 имя фида**
  - `api_set_plan` теперь дописывает название фида в `tp7` всегда, даже если фид один.
  - На создании UAC есть подстраховка: если `item.feed_name` есть, но отсутствует в `display_name`,
    он добавляется перед отправкой в UAC.
- **БАГ-FIX: tp6/tp7 ct0000 картинки и быстрые ссылки**
  - Для `ct0000` UAC больше не используется fallback `feed_images_for_segment(5)`, потому что он может
    подмешать модельные/бэушные картинки из `_image_store/feeds`. Берутся только `ct0000` картинки
    нужного `site_type/tp`.
  - Добавлен `_bad_ad_sitelink(...)`: режет быстрые ссылки вида `Скидка до 57%`,
    `Госпрограммы до -57%`; платежи не режет по сумме.
  - UAC-sitelinks теперь фильтруются и добиваются нейтральными `_GENERIC_SITELINK_FILLERS`.
  - `_GENERIC_TEXT_FILLERS[0]`: платеж поднят с `8 000` до `9 000` руб., чтобы не расходиться
    с креативом/оффером.
  - Добавлен `_coherent_payments(...)`: один ежемесячный платеж на всю tp6/tp7 UAC-кампанию
    (заголовки + тексты + быстрые ссылки). Пример: текст `8 000`, ссылка `6 900` → ссылка станет `8 000`;
    если первым встретился `6 900`, остальные места станут `6 900`.
    После согласования правило уточнено: канон платежа берётся в порядке
    `заголовки → тексты → быстрые ссылки`; креативы новых авто без платежа, б/у-креативы могут иметь `от 9000`.
  - Процентные быстрые ссылки больше не считаются браком сами по себе; при сборке UAC они отбрасываются,
    если процентный оффер уже есть в заголовках кампании.
  - Автопривязка промо теперь смотрит содержимое промо и контент набора: мусор вроде
    `Скидка 50% 11212`, кешбэк и конфликт процента промо с процентом в контенте не привязываются.
  - `tp2/tp4` search-only cookie/Grid автотаргетинг: для Поиска выставляется профиль как в UI:
    только `EXACT_V2_MARK` (`Целевые запросы`) и `WITHOUT_BRAND`
    (`Запросы без упоминания вашего бренда или брендов конкурентов`). Старый payload включал все
    категории и brand settings.
  - `_sanitize_content(...)` чистит оборванные числовые хвосты после обрезки, например
    `Одобрение за 30`.
- **Деплой без рестарта**
  - `blueprint.py` и `campaign.py` скопированы на LXC101 в `/opt/scripts/home/seoadvanced/direct/`.
  - Синтаксис OK на LXC: `py_compile blueprint.py campaign.py`.
  - Сервис НЕ перезапускался; уже созданные плохие UAC-черновики не исправляются in-place.

### Важно
- Текущая job стартовала до части патчей, поэтому её уже созданные UAC-черновики могут содержать старые
  картинки/быстрые ссылки/тексты. Надёжный путь — удалить и пересоздать плохие `tp6/tp7` черновики.

---

## Предыдущая сессия: 2026-06-25 (день, 5-я сессия)

### Что сделано
- **БАГ-FIX: porg-ozge4ntu / tp1 контент и ассеты**
  - Исправлена генерация заголовков РСЯ:
    - для `Марки`/`Модели` заголовки обязаны содержать свою марку/модель;
    - общие заголовки типа `Авито`, `Автокредит/кредит`, slash-темы, `Низкая ставка`,
      `Первый взнос 0₽`, `скидка/выгода до N%/N руб` отбрасываются;
    - `Changan UNI-S/CS55Plus` и подобные slash-модели приводятся к display-safe виду.
  - Для `Общее` в tp1 картинки больше не берутся из модельных ct: только `ct0000`, если он есть;
    иначе кампания остается без модельной картинки.
  - Тексты РСЯ фильтруют непроверяемые `скидка/выгода до N%/N руб`, старые `57%` больше не проходят.
  - Live repair без рестарта для `porg-ozge4ntu`: обновлено 372 объявления в кампаниях
    `711048239`, `711048520`, `711049606`, `711050162`, `711050422`.
    Проверка после Grid-update: `bad_after_count=0`, `general_with_images=0`.
- **БАГ-FIX: корректировки после Grid**
  - Причина: Grid не принимает отрицательные корректировки (`-100`) в `percent`, поэтому они
    пропускались. Добавлен v5 fallback `_apply_corrections(...)` после Grid-finalize для tp1/tp2/tp4.
  - Live repair без рестарта на пяти tp1-кампаниях `porg-ozge4ntu`: применилось по `3` корректировки
    на каждую кампанию, ошибок v5 нет.
- **БАГ-FIX: tp2/tp4 нейминг групп**
  - `_text_group_name(...)` теперь всегда строит кодер с `_aon_`.
  - Cookie-сборка `_tp1_pack_groups(...)` тоже больше не переводит `tp4` в `_aoff_`.
  - Dry-run: `tp2` sample names идут через `_aon_`; `tp4` у `pavlov` сейчас пустой M3-пак, но правило
    в коде глобальное.
- **БАГ-FIX: tp6/tp7 модельные UAC-кампании считались общими**
  - Причина: план мог построить имя с `ct0000`, когда структура давала `Lada Granta - Ключевики`
    и точный lookup модели не совпадал.
  - `_ct_for_name(...)` теперь матчится устойчиво: exact, base до ` - `, затем нормализованное
    вхождение самой длинной модели.
  - План tp6/tp7 явно передает `coder_ct`/`coder_brand`; `_brand_ct_from_coder(...)` берет `coder_ct`
    и `ct` приоритетно.
  - Для модельных tp6/tp7 заголовки строятся от `_brand_title_set(...)` и обязаны содержать свою
    марку/модель; картинки берутся по этому же ct.
  - Dry-run на LXC: `Lada Granta - Ключевики → ct0183`, `Haval Jolion - Ключевики → ct0119`;
    для Lada Granta сгенерированы модельные заголовки и 5 картинок из `ct0183`.
- **Техническое**
  - `grid_finalize.GridClient.update_ad_images(..., allow_empty_images=True)` добавлен для явной
    очистки imageHashes при repair общих объявлений; старые вызовы без флага работают как раньше.
  - Синтаксис OK: `py_compile blueprint.py grid_finalize.py grid_create.py`.
  - Сервис НЕ перезапускался.

### Важно
- Уже созданные плохие `tp6/tp7` UAC-черновики через скрытый UAC API безопаснее пересоздать, чем
  править in-place: новый код уже создает их с правильным `ct`/картинками/текстами.

---

## Предыдущая сессия: 2026-06-25 (день, 4-я сессия)

### Что сделано
- **БАГ-FIX: tp1 via_cookie создавался без быстрых ссылок из item/ИИ**
  - Диагноз по скрину porg-psm5h7q6 / campaign 711041403: это `tp1_cpc_site — РСЯ`, т.е. куки/Grid-путь.
    Обычный v5-путь передавал `it.sitelinks` в `_create_tp1_campaign`, а `_create_tp1_via_cookie` вообще
    не принимал `sitelinks`, поэтому `_resolve_campaign_assets` мог брать только БД/слепок и терял ссылки из превью.
  - Фикс (`blueprint.py`): добавлен параметр `sitelinks` в `_create_tp1_via_cookie`, он передаётся в
    `_resolve_campaign_assets`; caller при `via_cookie` прокидывает `it["sitelinks"]`.
  - Синтаксис OK: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m py_compile home/seoadvanced/direct/blueprint.py`.
  - Live проверено на `porg-psm5h7q6` через `/api/create_set` (`via_cookie=true`, `no_cpa=true`):
    `tp1` campaign `711043312` создан с `sitelink_set=1489408566`, `callouts=4`, `promo=true`.
- **БАГ-FIX: в группе одной марки появлялся текст про чужую марку + не применялись картинки**
  - Диагноз по скрину: группа `BAIC`, но текст содержал `LADA`; `_rsya_texts` не знал текущую марку группы
    и пропускал брендовые тексты из общего item/ИИ.
  - Фикс (`blueprint.py`): добавлен фильтр чужих марок (`_drop_foreign_brand_mentions`) и брендовая добивка
    текстов (`_brand_text_set`); `_rsya_texts(..., brand=...)` прокинут в v5 и via_cookie tp1-сборку.
  - Фикс заголовков (`blueprint.py`): `_rsya_titles` теперь тоже фильтрует чужие марки; для общей группы
    удаляются все известные марки из пула слепка/ИИ. Финальная проверка `tp2` campaign `711044050`:
    `groups=103`, `ads=103`, `bad_lada_non_lada=0`.
  - Фикс картинок (`blueprint.py`): post-update через `UpdateAdaptiveTextAds` теперь отправляет реальные
    `titles`/`bodies` группы, а не пустые массивы; fallback `suggest_images` тоже обновляет объявления
    с их текущими текстами.
  - Live проверка `tp1` campaign `711043312`: `groups=35`, `ads=35`, `ads_with_images=35`,
    `lada_in_non_lada_ads=0`; группа `BAIC` содержит BAIC-заголовки, не содержит `LADA`, imageHash есть.
- **БАГ-FIX: cookie-path падал на корректировках и длинных slash-словах**
  - `_grid_bid_modifiers` (`blueprint.py`): Grid `bidModifier*` принимает только положительный `percent`;
    отрицательные/нулевые корректировки теперь пропускаются. Это разблокировало `tp2-tp5` на AddCampaigns.
  - `_dedup_keep` (`grid_create.py`): перед `AddAdaptiveTextAds` длинные slash-слова (`Авто/Автомобили/Машины`)
    нормализуются в слова через пробел, иначе Яндекс валил весь batch объявлений.
  - Live прогон `tp1-tp5` на `porg-psm5h7q6`, marker `codex0625-144429`: `created=5`, `failed=0`.
    Созданы `tp1=711043312`, `tp3=711043365`, `tp4=711043376`, `tp5=711043404`; `tp2` после финального
    фикса создан как `711044050`.
  - `tp3/tp5` cookie-path: убрана лишняя зависимость финализации от `goal_id`; в `via_cookie+no_cpa`
    теперь тоже докручиваются уточнения/быстрые ссылки/промо с `goalId=0`.
    Live проверка marker `codex0625-shopfinal-151119`: `tp3=711044245`, `tp5=711044255`,
    у обоих `callouts=4`, `promo=true`, `sitelink_set=1488371146`, `corrections=0`.
- **LIVE QUEUE: точечная докрутка без рестарта**
  - Во время live job `24caaea0ac0f` (`porg-psm5h7q6`) один пункт упал старой ошибкой
    `tp1(куки): пак M3 пуст/недоступен — групп нет`.
  - Проверка Grid показала, что отсутствовала только `tp1_cpc_site — РСЯ - Модели - КС - Кемеровская область (Кузбасс)`.
  - Без рестарта сервиса создана точечная докрутка через `api_create_set`: campaign `711051173`,
    `groups=150`, `ads=150`, `callouts=8`, `promo=true`, `sitelink_set=1488371146`, `images_applied=2`.
  - Важно: старый live-счётчик `failed=1` в уже запущенной in-memory job не очищается без отмены/рестарта,
    но фактическая недостающая кампания создана.
- **ТЕСТОВЫЙ АККАУНТ БЕЗ МЕТРИКИ**
  - `porg-psm5h7q6` в `local_gsheet_sites` есть, но `counter_number=""`, `_metrika_goals_for` возвращает
    `{"counters": [], "goal_id": null}`.
  - `api_create_set` теперь разрешает только явный тестовый/куки-режим `via_cookie && no_cpa` без Метрики:
    создаются CPC-only черновики, в ответе `metrika_note`. Обычные режимы по-прежнему требуют счётчик и цель.

---

## Предыдущая сессия: 2026-06-25 (день, 3-я сессия)

### Что сделано
- **БАГ-FIX: DUPLICATE_SITELINK_DESCS — все tp7 (product) кампании porg-ozge4ntu падают с 400**
  - Диагноз: `errors_log` в `direct_automation_jobs` содержит только `DUPLICATE_SITELINK_DESCS`.
    Все ~700 product-items упали; первые 18 (tp1/tp2/tp4/tp5) создались нормально (60 кампаний).
  - Фикс-1 (`campaign.py` `create_master_campaign`): retry без sitelinks при HTTP 400 + DUPLICATE_SITELINK_DESCS.
    Sitelinks не критичны для черновика — кампания создаётся, ссылки добавляются позже.
  - Фикс-2 (`campaign.py` `_norm_sitelinks`): дедуп по description после обрезки (seen_descs set),
    в дополнение к существующему дедупу по title — превентивная защита от повторного срабатывания.
  - Синтаксис OK. Mutagen синкнет. Сервис НЕ перезапускался (отдельная задача).
  - Удалён временный файл `home/seoadvanced/_tmp_diag_porg.py`.

---

## Предыдущая сессия: 2026-06-25 (утро, 2-я сессия)

### Что сделано
- **БАГ-FIX: FEED_NOT_EXIST — 408 из 718 кампаний porg-ozge4ntu (фиды 3501305/3501306 в ERROR)**
  - `campaign.py` `create_master_campaign`: при HTTP 400 + FEED_NOT_EXIST → retry без feed_id/listings_feed_id/ecom-полей; успешный retry добавляет bad feed в `_dead_feed_ids` (module-level set).
  - `grid_finalize.py` `add_shopping_ads`: при FEED_NOT_EXIST в validationResult → retry без feedId/feedFilter (товарка создаётся без фильтра, а не падает).
  - `grid_create.py` `add_shopping_ads`: аналогичный retry для куки-пути создания.
  - `blueprint.py` `_first_url_feed`: пропускает фиды из `cmc._dead_feed_ids` (чтобы не передавать мёртвый feed_id в следующие кампании).
  - Синтаксис OK всех 4 файлов. Сервис НЕ перезапускался (активный job). Mutagen синкнет.
- **ФИЧА: Картинки в tp1 РСЯ на куки-пути (новые аккаунты без истории изображений)**
  - `grid_finalize.GridClient` — добавлены три метода (реверс HAR-25):
    - `suggest_images(campaign_id)` — SuggestImages Grid-query → список imageHash
    - `upload_image(image_path)` — multipart POST /web-api/image/upload → hash или None
    - `update_ad_images(ad_items)` — UpdateAdaptiveTextAds mutation → число обновлённых
  - `grid_create.create_full` — добавлен `ad_ids` в возвращаемый dict (список id объявлений)
  - `blueprint._create_tp1_via_cookie` — после create_full+finalize: если _img_map пуст
    (новый аккаунт) → suggest_images → fallback upload_image из M3-пака → update_ad_images.
    Если _img_map не пуст (старый аккаунт) — картинки уже ставятся через build_ad при create_full.
    Всё в try/except: ошибка картинок НЕ роняет кампанию.
  - Задеплоено, синтаксис OK на всех трёх файлах, digest.service active.

### Открытые баги / TODO
- [ ] DUPLICATE_SITELINK_DESCS: НЕ ВЕРИФИЦИРОВАНО live — нужен рестарт + новый прогон porg-ozge4ntu
- [ ] FEED_NOT_EXIST retry: НЕ ВЕРИФИЦИРОВАНО live — нужен рестарт сервиса и новый прогон porg-ozge4ntu
- [ ] tp7 (мастер кампании): имя кампании содержит "tp7" — нужно проверить нейминг
- [ ] БАГ-2 text="в к": наблюдать в новых кампаниях — если повторится, проверить HAR
- [ ] Картинки tp1 куки-путь: НЕ ВЕРИФИЦИРОВАНО live (нужна реальная кампания нового аккаунта)

### Текущее состояние сервиса
- digest.service на LXC 101 (192.168.0.202) — перезапущен 2026-06-25, синтаксис OK
- Фикс AGE_0_17 + ассеты на куки-пути — активны
- Картинки tp1 (куки): suggest_images → upload_image → update_ad_images — активны

---

## Предыдущая сессия: 2026-06-27

### Что сделано
- **БАГ-FIX: боевой `tp1/tp2` продолжал тянуть старые слепковые заголовки/тексты/быстрые ссылки**
  - Диагноз: экран `Обучение ИИ` и `tp6/tp7` уже использовали `_gen_campaign_content`, а live пути
    `tp1/tp2` и cookie/Grid path собирали контент первично из `_rsya_titles/_rsya_texts` и
    campaign assets из слепка. Поэтому на боевых кампаниях оставались старые формулировки и
    появлялась несогласованность вида `КАСКО бесплатно` vs `% экономии`.
  - В `blueprint.py` добавлены хелперы:
    - `_ai_campaign_content_for_item(...)`
    - `_ai_group_content(...)`
    - `_ai_common_sitelinks(...)`
  - Новый порядок источников для `tp1/tp2/tp5`:
    1. сначала полный AI-контент (`_gen_campaign_content` + repair/retry),
    2. если AI не дал валидный набор, только тогда внутренний слепковый фолбэк,
    3. локальные `_rsya_titles/_rsya_texts` остаются последним запасным путём.
  - Переведены на AI-first:
    - `_build_text_from_pack(...)`
    - `_build_tp1_from_pack(...)`
    - `_tp1_pack_groups(...)` (cookie/Grid path)
  - Campaign-level sitelinks для live путей теперь тоже берутся AI-first, а не из старого слепкового
    набора:
    - `_create_tp1_campaign(...)`
    - `_create_tp1_via_cookie(...)`
    - `_create_tp24_via_cookie(...)`
    - `tp2/tp4` finalize внутри `api_create_set`
    - `tp5` Grid finalize
  - Цель фикса: чтобы боевые `tp1/tp2/tp5` использовали тот же контентный контур, что и
    `Обучение ИИ`, и не расходились по УТП между заголовками, текстами и быстрыми ссылками.
- **БАГ-FIX: cookie/Grid path падал с `NameError: name 'login' is not defined`**
  - Причина: в `_tp1_pack_groups(...)` был добавлен AI-first вызов `_ai_group_content(login, ...)`,
    но `login` не был передан в сигнатуру функции и в её вызовы.
  - Исправлено: `_tp1_pack_groups(login, ...)` теперь принимает `login` явно; обновлены вызовы из
    `_create_tp1_via_cookie(...)` и `_create_text_via_cookie(...)`.
- **ФИКС reuse контента для парных кампаний внутри одного набора**
  - Для одинакового ключа `(agent, site_type, city, ct, brand)` теперь после первой удачной
    генерации/подстановки контента следующий item в том же наборе получает ТОЧНО тот же
    `titles/texts/sitelinks/title2` без новой генерации.
  - Это закрывает сценарий с галочкой «Под стиль сайта»: первая кампания генерирует контент,
    парная копия берёт уже готовый набор.

### Проверка
- `python3.12 -m py_compile home/seoadvanced/direct/blueprint.py home/seoadvanced/direct/ai_agents.py` — OK.
- `python3 -m pyflakes home/seoadvanced/direct/blueprint.py` — новых `undefined name` после фикса нет;
  остались только старые предупреждения `unused`/`f-string` вне этого изменения.

### Где смотреть баги
- БД: таблица `direct_automation_jobs`, колонка `errors_log`
- Логи: `ssh lxc101 "journalctl -u digest.service -n 50"`

### Архитектура (кратко)
- blueprint.py: оркестратор; tp1–tp7 типы кампаний
- campaign.py: API создания
- grid_create.py / grid_finalize.py: Grid API (без баллов)
- kontent_pack.py: M3 контент (заголовки/тексты/ссылки)
- Куки: glavpotok_cookies.py → .secret/.env (DIRECT_COOKIE_*)
- Баллы исчерпаны (152) → переход на Grid API автоматически

---

## Сессия 2026-07-01 — фикс tp1 fan-out по лендинг-фидам (НЕ задеплоено)

**Симптом:** live porg-psm5h7q6 (scherbakova, Мультибренд) tp1-товарка множилась по ВСЕМ enabled-фидам
(8 лендинг + catalog); на лендингах model-ListingAd пуст → tp1 ЖЁСТКО удаляла кампанию → ~20 фейлов.
tp7 те же лендинги переживал (whole-feed graceful).

**Корень:** `_account_model_feeds` (fan-out источник tp1) не отличал catalog- от landing-фидов; hard-fail
`_cleanup_partial` при пустом ShoppingAd/ListingAd.

**Фикс (blueprint.py, только он; create_set_tp1.py НЕ менял — DI через лямбду):**
1. Схема: `_feed_rules_ensure` — идемпотентный DO-блок ADD COLUMN role text DEFAULT 'catalog' +
   одноразовый backfill role='landing' (zabronirovat/dostup-k-rasprodazhe/credit-page/yandex.xml).
   Backfill гоняется ТОЛЬКО при первом создании колонки (information_schema) — ручные правки UI не трёт.
2. `_global_feed_rules` SELECT +role; новый `_catalog_feed_keys()`.
3. `_account_model_feeds(..., catalog_only=False)` — при True фильтр по role='catalog' (reuse `_feed_row_allowed`).
4. Call-site tp1 (blueprint ~12408): `account_model_feeds=lambda _l,_a:_account_model_feeds(_l,_a,catalog_only=True)`.
   tp7/product (11979/11246) — без флага (все enabled).
5. Bug2 graceful: v5 (ShoppingAd/ListingAd пусто) и cookie-path — НЕ удалять кампанию, warning + оставить
   РСЯ/товарку. Hard-fail остаётся только «группы не созданы».
6. api_feed_rules_post — принимает role (UPDATE только если поле пришло, старый UI не трёт роль).

**Верифицировано локально:** py_compile OK; pyflakes no undefined; synthetic (stub flask/requests) —
tp7 видит все 5 модельных фидов, tp1 catalog_only=2; fan-out=2 catalog-кампании, лендинги отброшены.
Backfill-превью на Victory (read-only): 8 enabled landing→'landing', 2 catalog→'catalog'. ✓

**Follow-up:** UI-тоггл роли в панели Глобальных правил (фронт HTML/JS) — backend готов (GET отдаёт role,
POST принимает), виджет не добавлен. НЕ задеплоено — ждёт приёмки Семёна + рестарт digest.service.

### Поправка к сессии 2026-07-01 (требование Семёна: ТОЛЬКО точный feed_key)

Backfill и весь матчинг роли переведены с `ILIKE '%...%'` (substring) на ТОЧНОЕ равенство feed_key:
- Введена константа `_CATALOG_FEED_KEYS` (6 точных имён каталог-фидов) — единый источник правды.
- `_feed_rules_ensure`: убран `DO $$`+ILIKE; теперь `information_schema`-guard → `ALTER ADD COLUMN role
  DEFAULT 'landing'` → `UPDATE SET role='catalog' WHERE feed_key = ANY(%s)` (точный список).
- `_catalog_feed_keys()`: default роли 'landing'; fallback = `_CATALOG_FEED_KEYS` (точный), не подстрока.
- `_account_model_feeds(catalog_only=True)` матчит через `_feed_row_allowed` (set-intersection точных
  ключей) — подстроки нет. `api_feed_rules_post` — точное `feed_key=%s`.
- Проверено: `yandex.xml` (landing) НЕ путается с `yandex-catalog-*` (catalog) при точном матче.
- grep по blueprint.py: substring-матчинга роли фидов НЕТ (остались только несвязанные `domain ILIKE`
  в поиске сайтов и `_PRICE_FEED_PREFS` для цен — не про роль).
- Backfill-превью Victory (точный ANY): enabled 2 catalog + 8 landing; disabled 4 catalog. ✓


---
## Дополнение 28 — фикс «все фиды в tp1» ЗАДЕПЛОЕН + верифицирован (2026-07-01 10:24)

**Что сделано (прод LXC101, direct.service PID нов., рестарт 10:20):**
- Backend `blueprint.py`: `role` (catalog/landing) в `direct_global_feed_rules`, EXACT-match backfill
  по `_CATALOG_FEED_KEYS` (НЕ substring), `_catalog_feed_keys()`, `_account_model_feeds(catalog_only=True)`
  ТОЛЬКО для tp1 (tp7 берёт все enabled). Bug2 graceful ×2: пустой ShoppingAd/ListingAd → кампания
  остаётся РСЯ + warning (было hard-delete).
- **UI-баг найден и исправлен:** тоггл роли был влеплен в МЁРТВУЮ копию `direct/index.html`; реально
  отдаётся `templates/direct/index.html` (через render_template, template_folder=../templates).
  Портировал тоггл в служебный файл (node --check OK, md5 Mac==LXC101).
- Backfill применён и ПРОВЕРЕН в Victory: catalog=6 (2 enabled: model-design-custom-name, catalog-new),
  landing=8 (все enabled). `_catalog_feed_keys()` для tp1 = 2 enabled catalog-фида.

**Осталось (действие Семёна):** перепрогон porg-psm5h7q6 — 1 клик «Создать» в UI (форма помнит конфиг).
set_plan дедупит → пропустит ~67 живых, до-создаст ~20 товарных на 2 каталог-фидах (или graceful→РСЯ).
Ожидание: failed→0, tp1 «Модели» только на catalog-фиде, «Общее»/«Марки»/tp7 не тронуты.
Body прошлой джобы потерян (рестарт стёр in-memory + TTL стёр БД-строку) → resume-эндпоинтом не поднять.

**Мелочь на потом:** удалить мёртвую `direct/index.html` (не отдаётся, только путает копии).

---
## Дополнение 29 — verify/repair расширены на 6 «молчаливых» дефектов (2026-07-01, НЕ задеплоено)

**Сделано (5 файлов, py_compile+pyflakes OK, standalone-логика ✓; live Grid НЕ проверялся):**
- `grid_read.campaign_content_counts`: 3 guarded enrichment-запроса (settings/keywords/adPrice),
  каждый в своём try — при сбое схемы core-счётчики (adgroups/ads/bad_names) НЕ ломаются, поле=None,
  причина в `enrich_errors`. Новые ключи: keywords_count, disabled_places, is_organic_search_enabled,
  promo_extension_id, has_ad_price/ad_price_count + флаги *_read (отличить «прочитано и пусто» от «не прочитано»).
- `grid_content_verifier`: +5 кодов, все gated на `*_read`/`is not None` (нет ложняков при None):
  NO_KEYWORDS_LIVE (tp2/4 ads>0 & kw==0, report-only), DYNAMIC_PLACES_ON (tp2 organic=True, report-only),
  MINUS_PLACES_MISSING (tp1 disabled_places==[], report-only), PRICE_MISSING (tp1 has_ad_price==False, report-only),
  PROMO_MISSING (settings_read & promo пусто → auto-repair). Опц. `expected` (minus_places/expects_price/expects_promo)
  переопределяет tp-инварианты; сейчас live_verifier его не прокидывает (follow-up).
- `campaign_state_verifier`: +CAMPAIGN_NAME_EMPTY (actual_name пусто, вкл. UAC) → rename_campaign repair.
- `repair_planner`: CAMPAIGN_NAME_EMPTY→rename_campaign, PROMO_MISSING→create_or_attach_promo. Report-only
  4 кода НЕ мапятся (остаются в issues). Auto-repair оба уже исполняются в execute_safe_post_create.

**⚠️ Схемы Grid-read (GdUnifiedCampaign.disabledPlaces/isOrganicSearchEnabled/promoExtensionId,
keywords(GdKeywordsContainerInput), GdAdaptiveTextAd.adPrice) НЕ верифицированы вживую** — имена взяты
из mutation-input (grid_uc_template.json/grid_create). Пока схема не подтверждена → *_read=False → чеки
дремлют (нет ложняков). Надо: прогнать GridReadClient на реальном логине, сверить *_read/enrich_errors.

**Осталось дописать (report-only, исполнителей нет):** keyword-repair (NO_KEYWORDS_LIVE),
minus-places setter (MINUS_PLACES_MISSING), adPrice setter (PRICE_MISSING), organic-off setter (DYNAMIC_PLACES_ON).
Плюс прокинуть per-item `expected` из blueprint → verification_service → live_verifier для точности.
Деплой (рестарт digest.service) делает Семён.


---
## Дополнение 29 — 6 багов ночного прогона + verify/repair + вынос content (2026-07-01 12:18)

**ЗАДЕПЛОЕНО (direct.service PID 820023, 3 рестарта, финал 12:17):**

Фаза 1 — 6 багов porg-psm5h7q6 (все с root-cause через direct_investigator):
- B1 tp6 имя: `_run_master_product_item` (~12022) — при сыром слаге (regex `^tp[67]_cp[ac]_...`) пересборка через `_build_name`. Идемпотентно.
- B2 tp1 минус-площадки: `_place_host` — URL→голый хост (Яндекс ждёт домен); нормализация на чтении (`_enabled_minus_places`) + сохранении (`api_minus_places_post`). `https://gdz.ru/`→`gdz.ru`.
- B3 tp1 цена: `_group_ad_price` — фолбэк `_min_offer_price` для брендовых групп без своего оффера → «от X».
- B4 tp2 динамич.места: `_finalize_search_via_grid` — `isOrganicSearchEnabled=bool(platforms.organic)` (шаблон протекал True). tp2 OFF, tp4/tp5 ON.
- B5 промо: create_set_promo.py `created_ids` = id|campaign_id (GAP B); проброс `precreated_promo_id` в куки-путь tp2/tp4 (GAP A, +param в create_set_text.py).
- B6 tp2/tp4 ключи: `_drop_foreign_city_keywords` no-op при пустом own_city (иначе резал все гео-ключи).
Смоук на LXC101: B2/B3/B6 подтверждены фактически. B1/B4/B5 — ждут прогона (нужен Grid-контекст).

Bug7 — verify/repair (почему автопочинка не ловила): расширен grid_read.campaign_content_counts (enrichment под guard: keywords/disabledPlaces/isOrganic/promo/adPrice + *_read флаги + enrich_errors). 6 чек-пунктов: CAMPAIGN_NAME_EMPTY (auto-repair rename), 5 report-only. PROMO_MISSING РАЗЖАЛОВАН в dormant report-only (риск дубль-промо при непроверенной read-схеме). ⚠️ Grid-read имена полей НЕ подтверждены live — первый прогон покажет enrich_errors; чеки dormant пока *_read=False.

Фаза 2 — вынос `_gen_campaign_content` (844 стр) → create_content.py::run_gen_campaign_content (19 DI). Byte-identical, 0 несвязанных имён, 26 params без свопов, import OK. `_run_master_product_item`(56 DI)/`api_create_set`(60 DI) — НЕ выносил (цена/риск, план откладывает).

Code-review (4 фикса, раньше): graceful→warnings, role coercion safe-default, INSERT role по членству, _FEED_RULES_ENSURED guard.

**ОСТАЛОСЬ:** перепрогон porg-psm5h7q6 (клик Семёна) → подтвердить B1/B4/B5 + enrich_errors (верны ли Grid-read схемы). Потом: включить report-only детекции + дописать repair-исполнители (keyword/minus/adPrice/organic setter) + expects_promo плюмбинг.

---
## Дополнение 30 — AUTO-REPAIR ключей/автотаргета поисковых групп (check→fix по куке, БЕЗ баллов) (2026-07-01, НЕ задеплоено)

**Сделано (8 файлов, py_compile+pyflakes OK, standalone-плюминг ✓; live Grid НЕ проверялся — нет доступа):**
Механизм разблокирован реальным HAR (UpdateUnifiedAdGroups + GroupsForEdit из браузера Семёна).
- `grid_finalize.GridClient`: `groups_for_edit(campaign_id: int|list) -> list[dict]` (облегчённый
  GroupsForEdit по campaignIdIn: adGroup-поля + keywords(showConditions) + retargetings-presence),
  `build_update_item(grp,*,keywords,relevance_match)` (round-trip 23 полей = ТОЧНО как HAR write:
  regions/минус-слова/трекинг/аудитория сохраняются как прочитано; bidModifiers={}+useBidModifiers=True
  как build_adgroup), `update_unified_adgroups(items)->list[int]` (+ретрай на транзиент/403 _post_json_retry).
- `repair_executor.execute_keywords_repair(login,ctx,campaign_ids,deps)`: только tp2/4/5-группы;
  идемпотентно (skip если ≥1 ключ И автотаргет=EXACT_V2_MARK/WITHOUT_BRAND); SKIP групп с
  bid_modifiers_present/retargetings_present (не теряем непустое); ключи пересчитывает через новый
  dep `group_keywords_context` (RepairDeps +поле). Пустой пересчёт → сохраняет старые ключи, чинит только автотаргет.
- `blueprint._repair_keywords_group_context`: pack=kp.gather(slepok,site_type,tp_code)→pos[ct]→
  `_filter_group_keywords(pos,seg,brand,city,site_type)` (guard #6 уже есть). Прокинут в `_repair_deps()`.
- Детекция: `grid_read._enrich_group_targeting` (4-я guarded-обогатилка) читает GroupsForEdit, ставит
  per-campaign `search_zero_kw_groups`/`wrong_autotarget_groups`/`groups_edit_read` ТОЛЬКО для tp2/4/5
  (tp1 РСЯ никогда не флагается). `grid_content_verifier`: коды NO_KEYWORDS_LIVE + WRONG_AUTOTARGET →
  repair-candidate kind `keywords_repair` (fallback на агрегат keywords_count когда per-group read недоступен).
- Маршрутизация: `repair_planner` NO_KEYWORDS_LIVE/WRONG_AUTOTARGET + candidate → action `keywords_repair`;
  `repair_gate.executable_keywords_repairs` + в summarize (keyword_repair_campaigns/executable_now);
  `repair_auto`: вызов в execute_safe_post_create (авто, идемпотентно — не создаёт сущностей) И execute_next_in_place.

**⚠️ НЕ верифицировано live (внешняя причина — нет куки/Grid из этой сессии):** реальная схема
GroupsForEditLite-ответа и приём UpdateUnifiedAdGroups Яндексом. Round-trip build_update_item сверен
с HAR write (23 поля совпали). Первый прогон покажет enrich_errors (group_targeting) — если схема не та,
чек дремлет (groups_edit_read=False → fallback report-only, ложняков нет). Деплой/рестарт делает Семён.

---
## Дополнение 31 — копирование UAC/tp6/tp7 в copy-flow через cookie/UAC (2026-07-01, ЗАДЕПЛОЕНО после рестарта direct.service)

**Сделано:**
- `copy campaigns` теперь отдельно подхватывает выбранные Grid/UAC-кампании, которые не видны в Direct v5
  (`typename` содержит UAC или имя `tp6_`/`tp7_`), читает source detail через `/web-api/uac/campaign/{id}` и
  создаёт черновик в целевом логине через `campaign.UacClient.create_master_campaign(..., launch=False)`.
- Для UAC перед созданием подставляются целевые: домен/URL, гео, счётчик Метрики, цель, бюджет/CPA,
  а для tp7/товарных UAC — целевой feed_id из `id_maps` обычного копирования или первый доступный URL-фид
  целевого аккаунта. Если feed_id нет, товарная UAC не превращается молча в обычный мастер — ошибка уйдёт в лог.
- Нормализация source detail стала tolerant к браузерным форматам: titles/texts/keywords могут быть строками
  или dict-объектами; быстрые ссылки переписываются на целевой домен; медиа пробуются по URL из detail, если
  UAC отдаёт reusable image/video URLs. Account-scoped content_id напрямую не переиспользуется.
- Созданные UAC результаты идут в общий live verification с `kind="uac"`, поэтому после копирования сервис
  читает UAC-detail, проверяет наличие в Grid и отдаёт repair/report вместе с обычными кампаниями.
- UI статуса копирования показывает отдельный счётчик `UAC: N создано / M warn`.

**Проверено локально:** `py_compile` для `blueprint.py`, `grid_finalize.py`, `direct_copy.py`; smoke UAC helpers
для распаковки title/text dict-форматов, переписывания sitelink href, извлечения media URL и нормализации target href.

**Ограничение:** реальное создание UAC/tp6/tp7 не прогонялось автоматически, чтобы не создавать черновики без команды.
Первый боевой прогон должен подтвердить, какие поля source UAC detail Яндекс реально отдаёт для медиа/фильтров.

---
## Дополнение 32 — copy-flow: фикс зависания queued при отсутствии python-dotenv (2026-07-01, ЗАДЕПЛОЕНО)

**Инцидент:** первый запуск копирования `porg-mjyh6hjv → porg-si7rw3ua` (`copy-b5e54b42ae8b`) упал до `pull`
на импорте `direct_copy.py`: в серверном `/root/venv` не было `dotenv`. Из-за того что модуль грузился до
`try`, UI оставался в `queued 0%` вместо явного `error`. До создания `workdir`/upload поток не дошёл.

**Фикс:**
- `direct_copy.py`: добавлен маленький fallback `dotenv_values` для `.env` без зависимости `python-dotenv`.
- `blueprint._copy_run_job`: загрузка `direct_copy.py` перенесена внутрь `try`, теперь стартовые ошибки пишут job
  в `status=error`, а не оставляют вечный `queued`.

**Проверено:** локальный и серверный `py_compile`, серверный import smoke `direct_copy.py` (`server direct_copy import ok True`),
`direct.service` перезапущен и активен. Старый in-memory job после рестарта закономерно отдаёт 404; копирование нужно
запустить заново.

---
## Дополнение 33 — copy-flow переведён в общую очередь создания + portable temp (2026-07-01, ЗАДЕПЛОЕНО)

**Инцидент:** повторный запуск `copy-625bd375d66d` упал до `pull` на `tempfile.mkdtemp(dir="/private/tmp")`:
на LXC нет `/private/tmp`. До Direct API/Grid upload поток не дошёл, кампании не создавались.

**Фикс:**
- `blueprint._copy_run_job`: временная папка теперь через `tempfile.gettempdir()` с `mkdir(parents=True)`, весь старт
  включая temp/import находится внутри `try`, ранние ошибки пишут `status=error`.
- `copy_start` больше не создаёт отдельный `threading.Thread`; ставит `_kind="copy_campaigns"` через общий `_job_new`.
  Copy-job видна в `/direct/api/create_jobs`, учитывает общий worker pool, лимит по агентству, cancel/clear.
- `_copy_job_upsert/_copy_job_log` зеркалят progress/status/result в карточку общей очереди (`kind=copy_campaigns`).
- Frontend после `/copy_start` добавляет copy-job в плавающий стек «Очередь создания» и показывает текст
  `копирование РК`, параллельно оставляя старый detailed copy-status на вкладке.

**Проверено локально:** `py_compile blueprint.py direct_copy.py`; smoke `_copy_mirror_create_job` (50% → done=5/10,
done result → created/failed). Серверный деплой/compile см. следующий рестарт direct.service.

---
## Дополнение 34 — copy-flow: стопор на неверное гео/неполный snapshot и серверная карточка очереди (2026-07-01)

**Инцидент:** боевой прогон `porg-mjyh6hjv → porg-si7rw3ua` дошёл до `upload`, хотя `GeoRegionId` для
`Уфа / Башкортостан, республика` не был найден. Из-за этого в целевой логин были созданы только shell-черновики
кампаний со старым гео `Краснодарский край`; группы/объявления ещё не были созданы.

**Фикс:**
- `copy-flow` теперь останавливается до upload, если выбранный набор не совпал со snapshot
  (`selected - UAC/tp6/tp7 != v5 campaigns`).
- Целевой `GeoRegionId` сначала берётся из общего словаря `GeoRegions` модуля, затем из `direct_copy`; если гео
  задано, но id не найден — upload запрещён.
- Гео-замена получила fallback для кейса источника вне БД: `Краснодарский край`, `Краснодарского края`,
  `Краснодар` заменяются на целевые регион/город; после замены snapshot сканируется, и остатки старого гео
  блокируют upload.
- Copy-job теперь сохраняет зеркальный статус в `public.direct_automation_jobs`, а фронтенд после `copy_start`
  сразу перечитывает серверную очередь, чтобы карточка копирования не терялась из общего стека.

**Ручная очистка инцидента:** 23 ошибочных shell-черновика в `porg-si7rw3ua` удалены через v5 `campaigns.delete`;
повторный `campaigns.get` по этим ID вернул пустой список. По `id_maps`/логу adgroups/ads созданы не были.

---
## Дополнение 35 — copy-flow: tp7 в середине имени + copy-status в общей панели (2026-07-01)

**Инцидент:** повторный прогон `6d9061a4308f` остановился до upload с корректной ошибкой
`snapshot неполный: выбрано 24 ... в v5 snapshot 23`. Отсутствующая кампания — `710994334`:
`Копия ХАВАЛ tp7_cpc_site...`; Grid видит её как `GdTextCampaign`, а v5 `campaigns.get` её не отдаёт.

**Фикс:**
- `_copy_is_uac_grid_row` теперь считает `tp6_`/`tp7_` UAC/cookie-веткой в любой части имени, а не только
  когда имя начинается с `tp6_`/`tp7_`. Для этого набора ожидается `23 v5 snapshot + 1 tp7 cookie`.
- Общая панель очереди для `kind=copy_campaigns` теперь poll-ит `/direct/api/copy_status/<job_id>`, а не
  `/create_set_status`; карточка не должна удаляться по ложному 404.
- В карточке общей очереди copy-flow показывается `progress` и последняя строка copy-log (`pull`, `snapshot`,
  `upload`, `verification`), а не только счётчик `done/total`.

**Проверка:** прогон `6d9061a4308f` остановился до upload; новых кампаний в целевом логине этим прогоном не создавалось.


---
## Дополнение 30 — большой батч фиксов porg-прогона (2026-07-01 17:39)

ЗАДЕПЛОЕНО (direct.service PID 834501). 18 задач закрыто.
СТРУКТУРА: cap ключей min(200, 9800//n_групп) — все влезают в лимит 10000 (было 17 пустых);
tp5 места=SEARCH_PAGE+ADV_GALLERY (null давал Поиск); товарка F(чанкинг по 50)+G(расцепить
set_default_text от листингов); данные пака scherbakova tp2 (CityRay ct0010->ct0100, Monjaro
ct0014->ct0104); ретрай Яндекс-500.
КОНТЕНТ: заголовки brand-first; _fill_title порог hi-8=48 (свободно <=8); _TITLE_TAILS longest-first;
вычищен _SHORT_TITLE_POOL+фильтр _alternate_rhythm; ЦЕНА _merge_price (чистый min current, НЕ приоритет
скидки)+год-стрип; сайтлинки ai_agents (Тест-драйв-запись, госпрограммы/скидка 30, промо 45);
callouts pavlov БД (ОСАГО->КАСКО, дубль-шины); картинки bu-фильтр; _bad_ad_sitelink тест-драйв онлайн;
товарный дефолт-текст 76 симв.
ФИКС 15: каталог-фильтр по adGroupId; href из ФИДА (targetUrl + _feed_url_for_model, фолбэк формула);
per-group сайтлинки Href=href группы (куки-путь отдельно).
РЕЗЮМЕ: api_job_resume удаляет последнюю частичную+v5-фолбэк+_vNN. RECREATE: keyword-repair отключён
(UpdateUnifiedAdGroups no-op ПОДТВЕРЖДЕНО); NO_KEYWORDS->recreate DRAFT-only.

КРИТИЧНО: UpdateUnifiedAdGroups НЕ пишет ключи в существующую группу (no-op) -> keyword-repair только
пересозданием. Лимит 10000 ключей/кампанию.
ОСТАЛОСЬ: тест-прогон -> live-сверка всего. Куки-путь per-group сайтлинков доработать.
